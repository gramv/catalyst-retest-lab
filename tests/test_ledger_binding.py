"""One paper account per ledger (plan 2.2): lab.ledger_account_binding. Fixtures only."""

import hashlib
import json
from decimal import Decimal as D

import httpx
import psycopg
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import system_event
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.repository import Repository
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import ManagedVenue


def expected_hash(account_id):
    identity = hashlib.sha256(("ALPACA_PAPER:" + account_id).encode()).hexdigest()
    return hashlib.sha256(("LEDGER_ACCOUNT_BINDING_V1:" + identity).encode()).hexdigest()


@pytest.fixture
def unbound(er):
    """The managed controller on a fresh ledger, before any reconciliation."""
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = ManagedVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )

    def build():
        return ManagedExecution(risk, broker, policy=engineering_execution_policy(),
                                clock=lambda: venue.now, review_store=reviews)

    yield build, venue, risk
    broker.close()


def bindings(repo):
    with repo.connect() as conn:
        return conn.execute("SELECT * FROM lab.ledger_account_binding").fetchall()


def test_first_clean_reconciliation_binds_the_ledger_once(unbound, er):
    build, venue, risk = unbound
    engine = build()
    assert bindings(er) == []  # Construction alone binds nothing.
    assert engine.reconcile()["clean"]
    [row] = bindings(er)
    assert row["venue"] == "ALPACA_PAPER" and row["binding_version"] == "LEDGER_ACCOUNT_BINDING_V1"
    assert row["account_hash"] == expected_hash(venue.account_id)
    owner = Repository(er.database_url.replace("user=catalyst_app", "user=lab_owner"))
    with owner.connect() as conn:
        run = conn.execute("SELECT * FROM lab.reconciliation_runs WHERE event_seq=%s",
                           (row["reconciliation_seq"],)).fetchone()
        audited = conn.execute(
            "SELECT lab.research_audit_matches('LEDGER_ACCOUNT_BINDING',to_jsonb(b),b.event_seq) "
            "AS ok FROM lab.ledger_account_binding b"
        ).fetchone()
    assert run["clean"] and run["startup"] and audited["ok"]
    for _ in range(2):
        assert engine.reconcile()["clean"]
    assert len(bindings(er)) == 1
    exported = json.dumps(er.export_events(), default=str)
    assert venue.account_id not in exported  # Only the hash is ever persisted.
    assert verify_events(er.export_events())["valid"]


def test_another_account_is_refused_at_construction_and_at_first_reconciliation(unbound, er):
    build, venue, risk = unbound
    first, second = build(), build()  # Both constructed before any binding exists.
    assert first.reconcile()["clean"]
    venue.account_id = "another-paper-account"
    with er.connect() as conn:
        runs = conn.execute("SELECT count(*) AS n FROM lab.reconciliation_runs").fetchone()["n"]
    with pytest.raises(ValueError, match="^LEDGER_ACCOUNT_BINDING_MISMATCH$"):
        second.reconcile()
    assert second.reconciled_at is None
    with er.connect() as conn:  # The refused reconciliation wrote nothing.
        assert conn.execute(
            "SELECT count(*) AS n FROM lab.reconciliation_runs"
        ).fetchone()["n"] == runs
    with pytest.raises(ValueError, match="^LEDGER_ACCOUNT_BINDING_MISMATCH$"):
        build()
    venue.account_id = "fixture-paper-account"
    assert build().reconcile()["clean"]
    assert [r["account_hash"] for r in bindings(er)] == [expected_hash("fixture-paper-account")]


def test_unclean_reconciliation_does_not_bind(unbound, er):
    build, venue, _ = unbound
    venue.inventory["UNKNOWN/USD"] = D(1)
    assert build().reconcile()["clean"] is False
    assert bindings(er) == []


def test_binding_is_append_only_risk_role_only_and_needs_a_clean_reconciliation(unbound, er):
    build, venue, risk = unbound
    engine = build()
    assert engine.reconcile()["clean"]
    [row] = bindings(er)
    insert = """INSERT INTO lab.ledger_account_binding(venue,account_hash,binding_version,
        reconciliation_seq) VALUES('ALPACA_PAPER',%s,'LEDGER_ACCOUNT_BINDING_V1',%s)"""
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(insert, ("0" * 64, row["reconciliation_seq"]))
    with risk.connect() as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, ("0" * 64, row["reconciliation_seq"]))
    owner = Repository(er.database_url.replace("user=catalyst_app", "user=lab_owner"))
    for statement in ("UPDATE lab.ledger_account_binding SET account_hash=repeat('0',64)",
                      "DELETE FROM lab.ledger_account_binding"):
        with owner.connect() as conn, pytest.raises(psycopg.errors.RaiseException):
            conn.execute(statement)
    with risk.connect() as conn:  # An unclean reconciliation can never justify a binding.
        unclean = system_event(risk, conn, "LAB_FIXTURE_UNCLEAN_RECONCILIATION", {})["seq"]
        conn.execute("""INSERT INTO lab.reconciliation_runs VALUES(%s,gen_random_uuid(),
            current_date,clock_timestamp(),clock_timestamp(),true,false,'[]','{}')""",
                     (unclean,))
    for repo, seq in ((risk, unclean), (owner, row["reconciliation_seq"])):
        with repo.connect() as conn, pytest.raises(
            psycopg.errors.RaiseException, match="CLEAN_RECONCILIATION_REQUIRED_FOR_BINDING"
        ):
            conn.execute(insert, ("1" * 64, seq))
    assert len(bindings(er)) == 1
