import copy
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.repository import Repository
from tests.conftest import NOW


def test_audit_chain_and_payload_tamper(repo, raw, evidence, policy):
    repo.submit(raw, NOW, lambda c, now: evidence, policy)
    rows = repo.export_events()
    verified = verify_events(rows)
    assert verified["valid"]
    tampered = copy.deepcopy(rows)
    tampered[-1]["payload_json"] = {"fake": "profit"}
    with pytest.raises(ValueError, match="Envelope mismatch"):
        verify_events(tampered)
    with pytest.raises(ValueError, match="independently retained head"):
        verify_events(rows[:-1], verified["head_hash"])


@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE lab.trade_events SET event_type = 'FORGED'",
        "DELETE FROM lab.trade_events",
        "TRUNCATE lab.trade_events",
        "ALTER TABLE lab.trade_events DISABLE TRIGGER ALL",
        "DROP TABLE lab.trade_events CASCADE",
        "UPDATE lab.candidates SET ticker = 'FAKE'",
        "DELETE FROM lab.validation_decisions",
        "SET ROLE lab_owner",
    ],
)
def test_application_role_cannot_mutate_history(repo, operation):
    with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(operation)


def test_application_role_cannot_write_trade_projection(repo):
    with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("INSERT INTO lab.trades(trade_id) VALUES (gen_random_uuid())")


@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE lab.trade_events SET event_type = 'FORGED'",
        "DELETE FROM lab.trade_events",
        "TRUNCATE lab.trade_events",
    ],
)
def test_trigger_blocks_owner_mutation_too(cluster, repo, raw, evidence, policy, operation):
    repo.submit(raw, NOW, lambda c, now: evidence, policy)
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(operation + " CASCADE" if operation.startswith("TRUNCATE") else operation)


def test_startup_rejects_owner_credentials(cluster):
    repo = Repository(localdb.connection_url(cluster, "lab_owner"))
    with pytest.raises(RuntimeError, match="restricted"):
        repo.check_role()


def test_db_assigns_hashes_ignoring_supplied_values(repo):
    with repo.connect() as conn:
        row = conn.execute("""
            INSERT INTO lab.trade_events(seq, event_id, strategy_version, event_type,
                payload_json, created_at, previous_hash, event_hash, event_body)
            VALUES (-1, gen_random_uuid(), 'CATALYST_RETEST_V1', 'SYSTEM_EVENT',
                '{"kind":"TEST"}', '2000-01-01', 'fake', 'fake', 'fake')
            RETURNING seq, previous_hash, event_hash, event_body, created_at
        """).fetchone()
        assert row["seq"] > 0 and len(row["event_hash"]) == 64
        assert row["created_at"].year != 2000
        assert row["event_body"] != "fake"
    assert verify_events(repo.export_events())["valid"]


def test_corrections_are_new_events(repo, raw, evidence, policy):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    rows = repo.export_events()
    original = rows[-1]
    with repo.connect() as conn:
        repo.append_event(
            conn,
            "CORRECTION",
            {"note": "Fixture annotation only"},
            record["candidate_id"],
            original["event_id"],
        )
    after = repo.export_events()
    assert after[-2] == original
    assert after[-1]["correction_of"] == original["event_id"]
    assert verify_events(after)["valid"]


def test_db_rejects_execution_without_atomic_risk_authorization(repo, raw, evidence, policy):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    with repo.connect() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="atomic risk decision"):
            repo.transition(conn, record["candidate_id"], "VALIDATED", "ORDER_SUBMITTED")


def test_atomic_rollback_preserves_chain(repo, raw, evidence, policy):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    before = repo.export_events()
    with pytest.raises(RuntimeError):
        with repo.connect() as conn:
            repo.append_event(
                conn, "SYSTEM_EVENT", {"kind": "ROLLBACK_TEST"}, record["candidate_id"]
            )
            raise RuntimeError("simulate process failure before commit")
    assert repo.export_events() == before


def test_concurrent_duplicates_only_one_passes_and_chain_does_not_fork(repo, raw, evidence, policy):
    def submit(_):
        return repo.submit(raw, NOW, lambda c, now: evidence, policy)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert [r["state"] for r in results].count("VALIDATED") == 1
    assert [r["failed_rule"] for r in results].count("DUPLICATE_SIGNAL_ID") == 7
    assert len({r["candidate_id"] for r in results}) == 8
    assert verify_events(repo.export_events())["valid"]


def test_concurrent_direct_event_inserts_do_not_fork(repo):
    def append(i):
        with repo.connect() as conn:
            return repo.append_event(conn, "SYSTEM_EVENT", {"kind": "CONCURRENT_TEST", "i": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(16)))
    assert len({r["seq"] for r in results}) == 16
    assert verify_events(repo.export_events())["valid"]


def test_new_repository_view_retains_claims_and_history(repo, raw, evidence, policy):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    fresh = Repository(repo.database_url)
    fresh.check_role()
    assert fresh.get_candidate(record["candidate_id"]) == record
    assert fresh.submit(raw, NOW, lambda c, now: evidence, policy)["failed_rule"] == (
        "DUPLICATE_SIGNAL_ID"
    )
