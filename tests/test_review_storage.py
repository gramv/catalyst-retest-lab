import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import SKEPTIC, digest
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_storage import EvidenceSubmission, ReviewStorage, bound_review_args
from tests.clock import fixture_session
from tests.test_jev_review import FIXTURE_KEY, response_for

WRITE = "fixture-review-write-token-not-real-" + "w" * 32
READ = "fixture-review-read-token-not-real-" + "r" * 32


@pytest.fixture
def review_settings(cluster):
    return ReviewSettings(
        localdb.connection_url(cluster, "catalyst_review"),
        WRITE,
        READ,
        "local",
        Gate1Inputs(copy.deepcopy(APPROVED_GATE1)),
        "MUSE_JEV_ACTIVE_V1",
    )


@pytest.fixture
def storage(review_settings):
    return ReviewStorage(review_settings.database_url)


@pytest.fixture
def candidate(repo, raw, evidence, policy):
    now, opens, closes = fixture_session(hours=1)
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    fixture = replace(
        evidence,
        observed_at=now,
        quote_timestamp=now,
        session_date=day,
        reconciled_session=day,
        official_open=opens,
        official_close=closes,
    )
    row = repo.submit(raw, now, lambda c, n: fixture, policy)
    assert row["state"] == "VALIDATED", row
    return row["candidate_id"]


def packet(candidate, revision=1, excerpt="The issuer announced a new product in its release."):
    return {
        "candidate_id": str(candidate),
        "revision": revision,
        "sources": [
            {
                "source_id": "release",
                "url": "https://example.com/issuer-release",
                "excerpt": excerpt,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "published_at": None,
            },
        ],
    }


def review(bundle, cluster, *, verdict="REJECT"):
    jev = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    args = bound_review_args(jev, bundle["bundle_hash"], request_id=uuid4(), question_set=SKEPTIC)
    evaluator = JevReviewer(
        jev,
        ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 1, 0.25, 100000, 30),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=response_for(verdict=verdict))
        ),
        key_provider=lambda: FIXTURE_KEY,
    )
    result = asyncio.run(evaluator.jev_review(**args))
    jev.verify(result.receipt_ids[0])
    with jev.connect() as conn:
        row = conn.execute(
            "SELECT decision_id FROM lab.ai_decisions WHERE receipt_id=%s AND question=%s",
            (result.receipt_ids[0], "verdict"),
        ).fetchone()
    return row["decision_id"], args


def intent(bundle, decision):
    return {
        "intent_id": str(uuid4()),
        "evidence_bundle_hash": bundle["bundle_hash"],
        "ai_decision_id": str(decision),
        "strategy_version": "CATALYST_RETEST_V1",
    }


def test_hash_addressed_evidence_and_server_owned_context(
    storage, candidate, review_settings, cluster
):
    storage.check_role()
    result = storage.store_evidence(packet(candidate), review_settings)
    evidence = storage.evidence(result["bundle_hash"])
    assert evidence["record_json"]["context"]["research_policy_id"] == "MUSE_JEV_ACTIVE_V1"
    assert evidence["cohort"] == "JEV_ACTIVE_V1"
    assert evidence["record_json"]["sources"][0]["excerpt_hash"] == digest(
        "The issuer announced a new product in its release."
    )
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        text, content_hash = conn.execute(
            "SELECT record_json::text,bundle_hash FROM lab.evidence_bundles WHERE bundle_hash=%s",
            (result["bundle_hash"],),
        ).fetchone()
        assert digest(text) == content_hash


def test_revision_append_and_unchanged_rework_refused(storage, candidate, review_settings):
    first = storage.store_evidence(packet(candidate), review_settings)
    with pytest.raises(psycopg.errors.UniqueViolation):
        storage.store_evidence(packet(candidate, 2), review_settings)
    second = storage.store_evidence(
        packet(candidate, 2, "A source correction changes the announcement."), review_settings
    )
    assert second["bundle_hash"] != first["bundle_hash"]
    assert storage.evidence(first["bundle_hash"])["revision"] == 1
    assert storage.evidence(second["bundle_hash"])["revision"] == 2


def test_source_excerpt_preserved_exactly(storage, candidate, review_settings):
    excerpt = "  Issuer release:\nThe announcement includes a qualification.\n"
    result = storage.store_evidence(packet(candidate, excerpt=excerpt), review_settings)
    source = storage.evidence(result["bundle_hash"])["record_json"]["sources"][0]
    assert source["excerpt"] == excerpt
    assert source["excerpt_hash"] == digest(excerpt)


@pytest.mark.parametrize("timestamp", [None, "tomorrow"])
def test_database_rejects_untyped_retrieval_time(storage, candidate, timestamp):
    from psycopg.types.json import Jsonb

    sources = packet(candidate)["sources"]
    sources[0]["retrieved_at"] = timestamp
    sources[0]["excerpt_hash"] = digest(sources[0]["excerpt"])
    with storage.connect() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(
            "SELECT * FROM lab.store_review_evidence(%s,1,%s,%s,60,60)",
            (candidate, Jsonb(sources), "MUSE_JEV_ACTIVE_V1"),
        )


def test_parallel_same_revision_has_one_record(storage, candidate, review_settings):
    def submit(i):
        try:
            return storage.store_evidence(
                packet(candidate, 1, f"Issuer release revision text {i}."), review_settings
            )
        except psycopg.Error:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(x is not None for x in results) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("deadline", "2030-01-01T00:00:00Z"),
        ("record_purpose", "STRATEGY"),
        ("policy_id", "override"),
        ("size_shares", 10),
        ("spread_bps", 1),
        ("win_probability", 0.99),
    ],
)
def test_no_caller_context_or_computed_number_fields(candidate, field, value):
    with pytest.raises(ValidationError):
        EvidenceSubmission.model_validate({**packet(candidate), field: value})


@pytest.mark.parametrize(
    "timestamp", ["tomorrow", "Sep 19 at close", 1234567890, "2026-09-19", "2026-09-19T10:00:00"]
)
def test_timestamps_require_rfc3339(candidate, timestamp):
    data = packet(candidate)
    data["sources"][0]["retrieved_at"] = timestamp
    with pytest.raises(ValidationError):
        EvidenceSubmission.model_validate(data)


@pytest.mark.parametrize("offset", [-61, 30])
def test_stale_and_future_retrieval_rejected(storage, candidate, review_settings, offset):
    data = packet(candidate)
    data["sources"][0]["retrieved_at"] = (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()
    with pytest.raises(psycopg.Error):
        storage.store_evidence(data, review_settings)


def test_intent_is_inert_even_for_rejected_review(
    storage, candidate, review_settings, cluster, repo
):
    bundle = storage.store_evidence(packet(candidate), review_settings)
    decision, _ = review(bundle, cluster, verdict="REJECT")
    before = repo.get_candidate(candidate)["state"]
    data = intent(bundle, decision)
    result = storage.store_intent(data)
    assert result["status"] == "STORED_ONLY_NOT_AUTHORIZED" and not result["authorizes_entry"]
    assert repo.get_candidate(candidate)["state"] == before
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        stored = conn.execute(
            "SELECT storage_status,cohort FROM lab.entry_intents WHERE intent_id=%s",
            (data["intent_id"],),
        ).fetchone()
        assert stored == ("STORED_ONLY_NOT_AUTHORIZED", "JEV_ACTIVE_V1")
        assert (
            conn.execute(
                "SELECT count(*) FROM lab.orders WHERE candidate_id=%s", (candidate,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM lab.risk_decisions WHERE candidate_id=%s", (candidate,)
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        storage.store_intent(data)
    with storage.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM lab.entry_intents")


def test_mismatched_bundle_decision_rejected(storage, candidate, review_settings, cluster):
    first = storage.store_evidence(packet(candidate), review_settings)
    decision, _ = review(first, cluster)
    second = storage.store_evidence(
        packet(candidate, 2, "The source explicitly corrected the release."), review_settings
    )
    with pytest.raises(psycopg.errors.RaiseException, match="INTENT_BINDING_MISMATCH"):
        storage.store_intent(intent(second, decision))


def test_raw_receipt_cannot_claim_different_bundle_context(
    storage, candidate, review_settings, cluster
):
    bundle = storage.store_evidence(packet(candidate), review_settings)
    jev = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    args = bound_review_args(jev, bundle["bundle_hash"], request_id=uuid4(), question_set=SKEPTIC)
    args["identity"]["context_hash"] = "a" * 64
    evaluator = JevReviewer(
        jev,
        ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 1, 0.25, 100000, 30),
        transport=httpx.MockTransport(lambda r: pytest.fail("No provider call")),
        key_provider=lambda: FIXTURE_KEY,
    )
    with pytest.raises(psycopg.errors.RaiseException, match="REVIEW_EVIDENCE_BINDING_MISMATCH"):
        asyncio.run(evaluator.jev_review(**args))


@pytest.mark.parametrize("table", ["review_contexts", "evidence_bundles", "entry_intents"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_new_tables_immutable_even_as_owner(
    storage, candidate, review_settings, cluster, table, operation
):
    bundle = storage.store_evidence(packet(candidate), review_settings)
    decision, _ = review(bundle, cluster)
    storage.store_intent(intent(bundle, decision))
    statement = {
        "UPDATE": f"UPDATE lab.{table} SET cohort=cohort",
        "DELETE": f"DELETE FROM lab.{table}",
        "TRUNCATE": f"TRUNCATE lab.{table} CASCADE",
    }[operation]
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(statement)


def test_new_records_extend_valid_chain(storage, candidate, review_settings, cluster, repo):
    bundle = storage.store_evidence(packet(candidate), review_settings)
    decision, _ = review(bundle, cluster)
    storage.store_intent(intent(bundle, decision))
    chain = verify_events(repo.export_events())
    assert chain["valid"] and chain["event_count"] > 0


def test_cohort_separation(storage, candidate, review_settings, cluster):
    bundle = storage.store_evidence(packet(candidate), review_settings)
    review(bundle, cluster)
    active = storage.observations(cohort="JEV_ACTIVE_V1", after_seq=0, limit=200)
    assert active["items"] and all(r["cohort"] == "JEV_ACTIVE_V1" for r in active["items"])
    assert not active["strategy_results"]
    engineering = storage.observations(cohort="JEV_ENGINEERING_TEST", after_seq=0, limit=200)
    assert not engineering["items"]


def test_http_scope_no_intent_reader_no_admission(storage, candidate, review_settings):
    with TestClient(
        create_review_app(review_settings, credential_provider=lambda: FIXTURE_KEY)
    ) as c:
        assert c.get("/health").json()["judgments_authorize_entry"] is False
        assert c.post("/api/v1/evidence-bundles", json=packet(candidate)).status_code == 401
        assert (
            c.post(
                "/api/v1/evidence-bundles",
                json=packet(candidate),
                headers={"Authorization": f"Bearer {READ}"},
            ).status_code
            == 401
        )
        posted = c.post(
            "/api/v1/evidence-bundles",
            json=packet(candidate),
            headers={"Authorization": f"Bearer {WRITE}"},
        )
        assert posted.status_code == 201, posted.text
        assert (
            c.get("/api/v1/entry-intents", headers={"Authorization": f"Bearer {READ}"}).status_code
            == 405
        )
        assert c.post("/api/v1/candidates", json={}).status_code == 404
        assert c.post("/api/v1/execute", json={}).status_code == 404
        assert (
            c.get(
                "/api/v1/review-observations", headers={"Authorization": f"Bearer {READ}"}
            ).status_code
            == 422
        )
        for field in ("size_shares", "expires_at", "action"):
            bad = {**packet(candidate), field: FIXTURE_KEY}
            response = c.post(
                "/api/v1/evidence-bundles", json=bad, headers={"Authorization": f"Bearer {WRITE}"}
            )
            assert response.status_code == 422 and FIXTURE_KEY not in response.text


def test_restricted_role_cannot_write_receipts_or_trading(storage, cluster):
    storage.check_role()
    for table in (
        "candidates",
        "orders",
        "risk_decisions",
        "trade_events",
        "ai_decisions",
        "jev_receipts",
    ):
        with storage.connect() as conn:
            assert not conn.execute(
                "SELECT has_table_privilege(current_user,%s,%s) AS allowed",
                (f"lab.{table}", "INSERT"),
            ).fetchone()["allowed"]
    JevStore(localdb.connection_url(cluster, "catalyst_jev")).check_role()


def test_unknown_candidate_has_no_context(storage, review_settings):
    with pytest.raises(psycopg.errors.RaiseException, match="SERVER_CONTEXT_UNAVAILABLE"):
        storage.store_evidence(packet(uuid4()), review_settings)


def test_payload_has_no_execution_fields_or_natural_date_fields(candidate):
    data = packet(candidate)
    data["sources"][0]["spread_bps"] = 1
    with pytest.raises(ValidationError):
        EvidenceSubmission.model_validate(data)
    data = packet(candidate)
    data["sources"][0]["excerpt"] = "Contact owner@example.com with this credential."
    with pytest.raises(ValidationError):
        EvidenceSubmission.model_validate(data)


def test_schema_provision_keeps_narrow_roles_and_existing_chain(cluster, storage, repo):
    from catalyst_lab.config import SCHEMA_VERSION
    from catalyst_lab.review_deploy import provision

    before = verify_events(repo.export_events())
    assert (
        provision(
            localdb.connection_url(cluster, "lab_owner"),
            review_password="fixture-review-" + "r" * 40,
            jev_password="fixture-jev-" + "j" * 40,
            operator_password="fixture-operator-" + "o" * 40,
        )
        == SCHEMA_VERSION
    )
    storage.check_role()
    JevStore(localdb.connection_url(cluster, "catalyst_jev")).check_role()
    assert verify_events(repo.export_events()) == before
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        assert conn.execute(
            "SELECT bool_and(rolpassword LIKE 'SCRAM-SHA-256$%') FROM pg_authid "
            "WHERE rolname IN ('catalyst_review','catalyst_jev','catalyst_review_operator')"
        ).fetchone()[0]


def test_expired_bundle_loader_refuses_review(storage, candidate, review_settings, cluster):
    bundle = storage.store_evidence(packet(candidate), review_settings)
    jev = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    with pytest.raises(ValueError, match="EVIDENCE_BUNDLE_EXPIRED"):
        bound_review_args(
            jev,
            bundle["bundle_hash"],
            request_id=uuid4(),
            question_set=SKEPTIC,
            now=datetime.fromisoformat(bundle["deadline"]),
        )
