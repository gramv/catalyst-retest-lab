"""Operator ENGINEERING_TEST enrollment (plan 0.10, owner ruling R6 option B).

Fixture evidence only: disposable PostgreSQL, the fake paper venue of
tests.test_managed_execution and fake Jev transports. No broker, provider, service or
owner-ledger contact. Every broker mutation below still crosses the exact one-use
five-second risk authorization.
"""

import asyncio
import json
import re
import tempfile
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab.account_risk import (
    ARM_METHOD,
    MANAGED_RISK_V2_POLICY_ID,
    AccountRiskPolicy,
    assign_arm,
)
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import halt, system_event
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_analytics import managed_daily_rollups, paginated_managed_results
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_engineering import (
    ENGINEERING_PURPOSE,
    ENGINEERING_SELECTION_POLICY,
    ENROLLED_KIND,
    OPERATOR_ROLE,
    enrollment_request,
    is_engineering,
    operator_database_url,
)
from catalyst_lab.managed_engineering import main as engineering_cli
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_funnel import research_funnel
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.managed_ops import REQUIRED_ENV, config_template, load_private_config
from catalyst_lab.managed_runtime import (
    ManagedRuntime,
    PositionMonitorLoop,
    build_runtime_from_env,
    engineering_runtime_policy,
)
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.position_monitor import PositionMonitor
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.scan_sources import BarContextWindow
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import ManagedVenue, observation, packet
from tests.test_managed_service import TOKEN, FixtureCycle
from tests.test_position_monitor import bars, monitor, opened

LEVELS = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
LEVEL_KEYS = ("entry_trigger", "max_entry_price", "stop", "target")
REASON = "Supervised first managed trade rehearsal (LAB_FIXTURE)"
# Assembled at run time, so the repository hygiene scan never sees a credential-shaped literal.
PASTED_BEARER = "Bearer " + "q7" * 20
MIGRATIONS = Path(localdb.__file__).with_name("migrations")
# The one block migration 017 inserts in front of migration 014's function body.
ENGINEERING_BRANCH = (
    " IF packet->>'selection_policy'='MANAGED_ENGINEERING_ENROLLMENT_V1' THEN\n"
    "  RETURN lab.managed_engineering_failure(packet);\n"
    " END IF;\n"
)


def url(repo, role):
    return re.sub(r"user=\w+", "user=" + role, repo.database_url)


def as_role(repo, role):
    return Repository(url(repo, role))


@pytest.fixture
def ex(er):
    """``mx`` under the approved JEV_MANAGED_RISK_V2 row, as the 0.10 launch configures it."""
    risk = RiskRepository(url(er, "catalyst_risk"))
    reviews = JevStore(url(er, "catalyst_jev"))
    venue = ManagedVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(
        risk, broker, policy=engineering_execution_policy(), clock=lambda: venue.now,
        review_store=reviews, risk_policy_id=MANAGED_RISK_V2_POLICY_ID,
    )
    assert engine.reconcile()["clean"]
    yield engine, venue, reviews
    broker.close()


def classify(engine, symbol="BTC/USD", *, sector="CRYPTO", theme="CRYPTO_L1"):
    """A server-owned classification, as the app's startup import writes it."""
    with engine.store.transaction() as conn:
        event = system_event(engine.repo, conn, "CLASSIFICATION_IMPORTED",
                             {"ticker": symbol, "source": "LAB_FIXTURE"})
        conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                     (event["seq"], symbol, sector, theme, "LAB_FIXTURE"))


def enroll(repo, *, signal=None, symbol="BTC/USD", levels=None, minutes=60, reason=REASON,
           role=OPERATOR_ROLE):
    levels = levels or LEVELS
    signal = signal or "TEST-FIXTURE-" + uuid4().hex[:10].upper()
    with as_role(repo, role).connect() as conn:
        return conn.execute(
            "SELECT lab.operator_enroll_managed_engineering(%s,%s,%s,%s,%s,%s,%s,%s) AS r",
            (signal, symbol, *(D(levels[k]) for k in LEVEL_KEYS), minutes, reason),
        ).fetchone()["r"]


def selected(repo, result):
    """The packet exactly as ManagedRuntime._selected_packets hands it to admission."""
    with repo.connect() as conn:
        row = conn.execute(
            """SELECT event_seq,body->'packet' AS packet FROM lab.managed_events
            WHERE event_seq=%s AND kind='RESEARCH_SELECTED'""",
            (result["selection_event_seq"],),
        ).fetchone()
    return {**row["packet"], "selection_event_seq": row["event_seq"]}


def review_failure(repo, candidate):
    with repo.connect() as conn:
        return conn.execute(
            "SELECT lab.managed_review_failure(%s) AS reason", (Jsonb(json_safe(candidate)),)
        ).fetchone()["reason"]


def events(repo, kind):
    with repo.connect() as conn:
        return conn.execute(
            "SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq", (kind,)
        ).fetchall()


def count(repo, relation):
    with repo.connect() as conn:
        return conn.execute(
            sql.SQL("SELECT count(*) AS n FROM lab.{}").format(sql.Identifier(relation))
        ).fetchone()["n"]


def event_count(repo):
    return verify_events(repo.export_events())["event_count"]


def status(repo):
    with as_role(repo, OPERATOR_ROLE).connect() as conn:
        return conn.execute("SELECT * FROM lab.operator_managed_engineering_status()").fetchall()


def refused(code):
    return pytest.raises(psycopg.errors.RaiseException, match=code)


def insert_setup(engine, record, *, receipt_id=None):
    """A direct catalyst_risk insert, bypassing ManagedExecution.admit on purpose."""
    with engine.store.transaction() as conn:
        conn.execute(
            """INSERT INTO lab.managed_setups(setup_id,cycle_id,revision,symbol,market,
            strategy_version,policy_id,cohort,receipt_id,evidence_hash,expires_at,record_json)
            VALUES(%s,%s,1,%s,'CRYPTO','CRYPTO_STRUCTURAL_RETEST_TEST_V1',
            'MUSE_JEV_MANAGED_TEST_V1','JEV_MANAGED_PAPER_V1',%s,%s,%s,%s)""",
            (uuid4(), record.get("cycle_id", str(uuid4())), record.get("symbol", "BTC/USD"),
             receipt_id, record.get("evidence_hash", "fixture"),
             record.get("expires_at", (engine.now() + timedelta(hours=1)).isoformat()),
             Jsonb(json_safe(record))),
        )


def open_position(ex, sid, *, fill_price="100.10"):
    """The acknowledged-print trigger, the authorized entry, its fill and the protective stop."""
    engine, venue, _ = ex
    decision = engine.observe_trigger(sid, observation(ex))
    assert decision["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy")
                 if o["client_order_id"] == decision["payload"]["client_order_id"])
    assert engine.ingest(venue.fill(entry["id"], entry["qty"], price=fill_price))
    engine.manage(sid, observation(ex))
    assert engine._load(sid)[1]["state"] == "OPEN"
    [stop] = [o for o in venue.orders_of("sell", "stop_limit") if o["symbol"] == entry["symbol"]]
    assert stop["status"] == "new" and D(stop["stop_price"]) == D(LEVELS["stop"])
    return decision, entry


def exit_at_target(ex, sid, *, target="111"):
    """The mechanical target exit: stop canceled, market close filled, CLOSED."""
    engine, venue, _ = ex
    symbol = engine._load(sid)[0]["symbol"]
    reached = observation(ex, trade_price=target, bid=target, ask=str(D(target) + D(".01")))
    engine.manage(sid, reached)
    engine.manage(sid, reached)
    [stop] = [o for o in venue.orders_of("sell", "stop_limit") if o["symbol"] == symbol]
    [close] = [o for o in venue.orders_of("sell", "market") if o["symbol"] == symbol]
    assert stop["status"] == "canceled"
    assert engine.ingest(venue.fill(close["id"], close["qty"], price=target))
    engine.manage(sid, reached)
    assert engine._load(sid)[1]["state"] == "CLOSED"


class BarSource:
    """Records completed-bar requests; a withheld review must make none."""

    def __init__(self):
        self.calls = []

    def completed_bars(self, *args):
        self.calls.append(args)
        return (), ()


def disabled_monitor(watcher):
    return PositionMonitor(watcher.execution, watcher.reviewer, policy=watcher.policy,
                           review_seconds=10, clock=watcher.now, management_reviews="DISABLED")


# --- Enrollment: one audited operator event and one packet, never Jev ----------------------


def test_operator_enrollment_writes_one_audited_event_and_one_packet_without_jev(ex):
    engine, _, _ = ex
    classify(engine)
    before = event_count(engine.repo)
    result = enroll(engine.repo, signal="TEST-MANAGED-FIXTURE1")
    assert result["signal_id"] == "TEST-MANAGED-FIXTURE1" and result["symbol"] == "BTC/USD"
    assert result["operator_role"] == OPERATOR_ROLE and result["execution_scope"] == "PAPER_ONLY"
    assert result["purpose"] == ENGINEERING_PURPOSE
    assert result["jev_review"] == "NONE_ENGINEERING_TEST"
    assert result["grid_check"] == "DEFERRED_TO_ADMISSION_LIVE_BROKER_CHECK"
    [enrollment] = events(engine.repo, ENROLLED_KIND)
    [selection] = events(engine.repo, "RESEARCH_SELECTED")
    assert (enrollment["event_seq"], selection["event_seq"]) == (
        result["enrollment_event_seq"], result["selection_event_seq"])
    assert str(selection["event_id"]) == result["packet_id"]
    assert enrollment["setup_id"] is None and selection["setup_id"] is None
    body = enrollment["body"]
    assert body["levels"] == LEVELS and body["reason"] == REASON
    assert body["operator_role"] == OPERATOR_ROLE and body["market"] == "CRYPTO"
    assert body["selection_policy"] == ENGINEERING_SELECTION_POLICY
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=59) < expires - enrollment["recorded_at"] <= timedelta(minutes=60)
    candidate = selected(engine.repo, result)
    assert candidate["receipt_id"] is None and "quality_receipt_id" not in candidate
    assert candidate["enrollment_event_seq"] == enrollment["event_seq"]
    assert candidate["selection_policy"] == ENGINEERING_SELECTION_POLICY
    assert candidate["purpose"] == ENGINEERING_PURPOSE and is_engineering(candidate)
    assert candidate["execution_scope"] == "PAPER_ONLY" and candidate["market"] == "CRYPTO"
    assert (candidate["levels"], candidate["expires_at"], candidate["cycle_id"]) == (
        body["levels"], body["expires_at"], body["cycle_id"])
    assert candidate["sources"] == [] and "state" not in candidate and "agent" not in candidate
    assert review_failure(engine.repo, candidate) is None
    # Exactly the two audited rows; no Jev request, receipt or judgment exists for them.
    assert event_count(engine.repo) == before + 2
    assert count(engine.repo, "jev_requests") == count(engine.repo, "jev_receipts") == 0
    with as_role(engine.repo, "lab_owner").connect() as conn:
        audited = conn.execute(
            """SELECT bool_and(lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(e),
            e.event_seq)) AS ok FROM lab.managed_events e WHERE e.event_seq=ANY(%s)""",
            ([enrollment["event_seq"], selection["event_seq"]],),
        ).fetchone()
    assert audited["ok"] and verify_events(engine.repo.export_events())["valid"]
    [row] = status(engine.repo)
    assert row["active"] and row["setup_id"] is None and row["signal_id"] == result["signal_id"]


@pytest.mark.parametrize("role", ["catalyst_app", "catalyst_risk", "catalyst_jev",
                                  "catalyst_review", "catalyst_review_operator"])
def test_application_roles_cannot_enroll(ex, role):
    engine, _, _ = ex
    classify(engine)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        enroll(engine.repo, role=role)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), \
            as_role(engine.repo, role).connect() as conn:
        conn.execute("SELECT * FROM lab.operator_managed_engineering_status()")
    assert events(engine.repo, ENROLLED_KIND) == []


def test_only_the_operator_function_writes_enrollments_packets_or_receipt_free_setups(ex):
    engine, _, _ = ex
    classify(engine)
    legit = selected(engine.repo, enroll(engine.repo))
    stored = {k: v for k, v in legit.items() if k != "selection_event_seq"}
    # The risk role inserts managed events for the app, but never these.
    with refused("ENGINEERING_ENROLLMENT_OPERATOR_ONLY"), engine.store.transaction() as conn:
        engine.store.event(conn, ENROLLED_KIND, {"signal_id": "TEST-FORGED-EVENT"})
    for forged in (
        stored,
        {**stored, "selection_policy": "MUSE_JEV_RESEARCH_SELECTION_V2"},
        {k: v for k, v in stored.items() if k != "purpose"},
    ):
        with refused("ENGINEERING_ENROLLMENT_OPERATOR_ONLY"), \
                engine.store.transaction() as conn:
            engine.store.event(conn, "RESEARCH_SELECTED", {"packet": forged})
    insert = """INSERT INTO lab.managed_events(event_id,idempotency_key,kind,body)
        VALUES(%s,%s,%s,%s)"""
    for role in ("catalyst_app", OPERATOR_ROLE):  # Neither has a table grant.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), \
                as_role(engine.repo, role).connect() as conn:
            conn.execute(insert, (uuid4(), "forged-" + uuid4().hex, ENROLLED_KIND, Jsonb({})))
    # The owner's own login is not the operator login either.
    with refused("ENGINEERING_ENROLLMENT_OPERATOR_ONLY"):
        enroll(engine.repo, role="lab_owner")
    # A receipt-free setup needs the enrollment policy and an exact, valid binding.
    with refused("ENGINEERING_ENROLLMENT_REQUIRED"):
        insert_setup(engine, {**legit, "selection_policy": "MUSE_JEV_RESEARCH_SELECTION_V2"})
    with refused("SELECTION_INTEGRITY_FAILURE"):
        insert_setup(engine, {**legit, "levels": {**LEVELS, "target": "112"}})
    with refused("ENGINEERING_SETUP_BINDING_FAILURE"):
        _mismatched_columns(engine, legit)
    # A Jev-reviewed setup can never carry the engineering purpose (migration 017 CHECK).
    reviewed = packet(ex, "ETH/USD")
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_setup(engine, {**reviewed, "purpose": ENGINEERING_PURPOSE},
                     receipt_id=reviewed["receipt_id"])
    assert count(engine.repo, "managed_setups") == 0
    assert len(events(engine.repo, ENROLLED_KIND)) == 1


def _mismatched_columns(engine, legit):
    """The packet binds, but the setup's own columns disagree with it."""
    with engine.store.transaction() as conn:
        conn.execute(
            """INSERT INTO lab.managed_setups(setup_id,cycle_id,revision,symbol,market,
            strategy_version,policy_id,cohort,receipt_id,evidence_hash,expires_at,record_json)
            VALUES(%s,%s,1,%s,'CRYPTO','CRYPTO_STRUCTURAL_RETEST_TEST_V1',
            'MUSE_JEV_MANAGED_TEST_V1','JEV_MANAGED_PAPER_V1',NULL,%s,%s,%s)""",
            (uuid4(), legit["cycle_id"], legit["symbol"], legit["evidence_hash"],
             datetime.fromisoformat(legit["expires_at"]) + timedelta(hours=1),
             Jsonb(json_safe(legit))),
        )


# --- Input rules: the database refuses, and the CLI refuses the same inputs first ------------

REFUSALS = [
    ({"symbol": "SPY"}, "ENGINEERING_ENROLLMENT_CRYPTO_ONLY"),
    ({"symbol": "btc/usd"}, "ENGINEERING_ENROLLMENT_CRYPTO_ONLY"),
    # 111 - 100.10 = 10.90 >= 2 x 5.10 passes; a 110 target (9.90 < 10.20) does not.
    ({"levels": {**LEVELS, "target": "110"}}, "MIN_REWARD_RISK"),
    ({"levels": {**LEVELS, "stop": "100.5"}}, "ENGINEERING_LEVELS_INVALID"),
    ({"levels": {**LEVELS, "max_entry_price": "99"}}, "ENGINEERING_LEVELS_INVALID"),
    ({"minutes": 0}, "ENGINEERING_EXPIRY_INVALID"),
    ({"minutes": 241}, "ENGINEERING_EXPIRY_INVALID"),
    ({"reason": "too short"}, "OPERATOR_REASON_TOO_SHORT"),
    ({"reason": "   "}, "OPERATOR_REASON_REQUIRED"),
    ({"signal": "ENG-FIXTURE-1"}, "ENGINEERING_SIGNAL_ID_INVALID"),
    ({"signal": "test-lowercase-1"}, "ENGINEERING_SIGNAL_ID_INVALID"),
]


@pytest.mark.parametrize("change,code", REFUSALS + [({"symbol": "ETH/USD"}, "CORRELATION_UNKNOWN")])
def test_enrollment_input_refusals_in_the_database(ex, change, code):
    engine, _, _ = ex
    classify(engine)
    with refused(code):
        enroll(engine.repo, **change)
    assert events(engine.repo, ENROLLED_KIND) == []


def request_kwargs(change):
    levels = change.get("levels", LEVELS)
    return {"symbol": change.get("symbol", "BTC/USD"), "entry_trigger": levels["entry_trigger"],
            "max_entry": levels["max_entry_price"], "stop": levels["stop"],
            "target": levels["target"], "expires_minutes": change.get("minutes", 60),
            "reason": change.get("reason", REASON), "signal_id": change.get("signal")}


@pytest.mark.parametrize("change,code", REFUSALS + [
    ({"levels": {**LEVELS, "stop": "9.5e1"}}, "ENGINEERING_LEVELS_INVALID"),
    ({"reason": "Rehearsal with a pasted " + PASTED_BEARER}, "OPERATOR_REASON_CREDENTIAL_SHAPED"),
    ({"reason": "Rehearsal reason\nwith a second line"}, "OPERATOR_REASON_INVALID"),
])
def test_cli_rules_refuse_the_same_inputs_before_connecting(change, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        enrollment_request(**request_kwargs(change))


def test_cli_request_carries_exact_decimals_and_a_generated_test_signal():
    request = enrollment_request(**request_kwargs({}))
    assert request["levels"]["max_entry_price"] == D("100.10") and request["symbol"] == "BTC/USD"
    assert re.fullmatch(r"TEST-MANAGED-[0-9A-F]{12}", request["signal_id"])
    assert request["reason"] == REASON and request["expires_minutes"] == 60


def test_enrollment_refuses_halts_pending_exposure_and_a_second_active_enrollment(ex):
    engine, venue, _ = ex
    classify(engine)
    classify(engine, "ETH/USD", theme="CRYPTO_L2")
    # A normal managed setup already watching the symbol: one active setup per crypto symbol.
    engine.admit(packet(ex, "SOL/USD"))
    classify(engine, "SOL/USD", theme="CRYPTO_L3")
    with refused("ACTIVE_SYMBOL_ALREADY_MANAGED"):
        enroll(engine.repo, symbol="SOL/USD")
    first = enroll(engine.repo, signal="TEST-ONE-AT-A-TIME")
    with refused("ENGINEERING_SIGNAL_ALREADY_ENROLLED"):
        enroll(engine.repo, signal="TEST-ONE-AT-A-TIME")
    with refused("ENGINEERING_TEST_ALREADY_ACTIVE"):  # One engineering test at a time.
        enroll(engine.repo, symbol="ETH/USD")
    assert [r["enrollment_event_seq"] for r in status(engine.repo) if r["active"]] == [
        first["enrollment_event_seq"]]
    with engine.repo.connect() as conn:
        halt(engine.repo, conn, "TEST_FIXTURE_HALT", {"fixture": True})
    with refused("RISK_HALT"):
        enroll(engine.repo, symbol="ETH/USD")
    assert len(events(engine.repo, ENROLLED_KIND)) == 1


# --- The review SQL itself refuses what the operator guard would block ----------------------


def forged(repo, body, candidate, *, audited=True, enrollment_seq=None):
    """Owner fixture forgery with the operator guard disabled (``audited=False`` also skips the
    audit trigger): proves lab.managed_review_failure refuses it on its own."""
    owner = as_role(repo, "lab_owner")
    with owner.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        conn.execute("ALTER TABLE lab.managed_events DISABLE TRIGGER guard_engineering_enrollment")
        if body is not None:
            insert = """INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,
                body{}) VALUES(%s,NULL,%s,%s,%s{}) RETURNING event_seq"""
            values = [uuid4(), "managed-engineering-enrollment:" + body["signal_id"],
                      ENROLLED_KIND, Jsonb(body)]
            if audited:
                row = conn.execute(insert.format("", ""), values).fetchone()
            else:
                conn.execute("ALTER TABLE lab.managed_events DISABLE TRIGGER audit_jev")
                unrelated = system_event(owner, conn, "LAB_FIXTURE_UNRELATED", {"fixture": True})
                row = conn.execute(insert.format(",event_seq", ",%s"),
                                   [*values, unrelated["seq"]]).fetchone()
                conn.execute("ALTER TABLE lab.managed_events ENABLE TRIGGER audit_jev")
            enrollment_seq = row["event_seq"]
        stored = {**candidate, "enrollment_event_seq": enrollment_seq}
        row = conn.execute(
            """INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,body)
            VALUES(%s,NULL,%s,'RESEARCH_SELECTED',%s) RETURNING event_seq""",
            (uuid4(), f"managed-engineering-selected:{enrollment_seq}",
             Jsonb({"packet": stored})),
        ).fetchone()
        conn.execute("ALTER TABLE lab.managed_events ENABLE TRIGGER guard_engineering_enrollment")
    return {**stored, "selection_event_seq": row["event_seq"]}


def forged_pair(body, candidate, *, body_changes=None, packet_changes=None):
    signal, cycle = "TEST-FORGED-" + uuid4().hex[:8].upper(), str(uuid4())
    new_body = {**body, "signal_id": signal, "cycle_id": cycle, **(body_changes or {})}
    new_packet = {k: v for k, v in candidate.items() if k != "selection_event_seq"}
    new_packet.update(signal_id=signal, cycle_id=cycle, item_key="ENGINEERING_TEST:" + signal)
    new_packet.update(packet_changes or {})
    return new_body, new_packet


def test_forged_engineering_packets_are_refused_by_the_review_sql(ex):
    engine, venue, _ = ex
    classify(engine)
    legit = selected(engine.repo, enroll(engine.repo))
    [enrolled] = events(engine.repo, ENROLLED_KIND)
    body = enrolled["body"]
    past = (venue.now - timedelta(minutes=1)).isoformat()
    cases = {
        "missing enrollment": (None, forged_pair(body, legit)[1], {"enrollment_seq": 10**12},
                               "ENGINEERING_ENROLLMENT_BINDING_FAILURE"),
        "wrong levels": (*forged_pair(body, legit, packet_changes={
            "levels": {**LEVELS, "target": "112"}}), {}, "ENGINEERING_ENROLLMENT_BINDING_FAILURE"),
        "not the operator": (*forged_pair(body, legit, body_changes={
            "operator_role": "catalyst_risk"}), {}, "ENGINEERING_ENROLLMENT_BINDING_FAILURE"),
        "carries a receipt": (*forged_pair(body, legit, packet_changes={
            "receipt_id": str(uuid4())}), {}, "ENGINEERING_ENROLLMENT_BINDING_FAILURE"),
        "not a TEST- signal": (*forged_pair(body, legit, body_changes={
            "signal_id": "ENG-FORGED-1"}, packet_changes={"signal_id": "ENG-FORGED-1"}), {},
            "ENGINEERING_ENROLLMENT_BINDING_FAILURE"),
        "audit mismatch": (*forged_pair(body, legit), {"audited": False},
                           "ENGINEERING_ENROLLMENT_BINDING_FAILURE"),
        "expired": (*forged_pair(body, legit, body_changes={"expires_at": past},
                                 packet_changes={"expires_at": past}), {},
                    "ENGINEERING_ENROLLMENT_EXPIRED"),
    }
    for name, (forged_body, candidate, options, code) in cases.items():
        stored = forged(engine.repo, forged_body, candidate, **options)
        assert review_failure(engine.repo, stored) == code, name
        with pytest.raises(ValueError):  # admit refuses it (Python or the same SQL).
            engine.admit(stored)
    # A packet that is not the stored one fails the selection binding first.
    assert review_failure(engine.repo, {**legit, "levels": {**LEVELS, "stop": "96"}}) == (
        "SELECTION_INTEGRITY_FAILURE")
    # One engineering setup at a time: a valid second enrollment waits for the first to end.
    first = engine.admit(legit)
    second = forged(engine.repo, *forged_pair(body, legit))
    assert review_failure(engine.repo, second) == "ENGINEERING_TEST_ALREADY_ACTIVE"
    with pytest.raises(ValueError, match="ENGINEERING_TEST_ALREADY_ACTIVE"):
        engine.admit(second)
    engine.revoke(first, "LAB_FIXTURE_ENDED")
    assert review_failure(engine.repo, second) is None
    assert count(engine.repo, "managed_setups") == 1 and not venue.orders


# --- Every other policy path is migration 014's, statement for statement --------------------


def function_source(path, header):
    text = path.read_text()
    body = text.index("AS $$", text.index(header)) + len("AS $$")
    return text[body:text.index("$$;", body)]


def test_non_engineering_review_path_is_migration_014_byte_for_byte(ex):
    engine, _, _ = ex
    original = function_source(MIGRATIONS / "014_managed_completion.sql",
                               "CREATE FUNCTION lab.managed_review_failure(packet jsonb)")
    replaced = function_source(
        MIGRATIONS / "017_engineering_enrollment.sql",
        "CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb)",
    )
    with as_role(engine.repo, "lab_owner").connect() as conn:
        functions = {row["proname"]: row for row in conn.execute(
            """SELECT proname,prosrc,prosecdef,provolatile,proconfig,
            pg_get_function_result(oid) AS result FROM pg_proc WHERE oid IN
            ('lab.managed_review_failure_before_b1(jsonb)'::regprocedure,
             'lab.managed_review_failure_v13(jsonb)'::regprocedure)""").fetchall()}
        grants = conn.execute("""SELECT
            has_function_privilege('catalyst_risk','lab.managed_review_failure(jsonb)',
              'EXECUTE') AS risk,
            has_function_privilege('catalyst_app','lab.managed_review_failure(jsonb)',
              'EXECUTE') AS app""").fetchone()
    current, v13 = (functions["managed_review_failure_before_b1"],
                    functions["managed_review_failure_v13"])
    assert current["prosrc"] == replaced
    assert replaced.count(ENGINEERING_BRANCH) == 1
    assert replaced.replace(ENGINEERING_BRANCH, "") == original
    assert replaced.startswith(original[:original.index("BEGIN\n") + 6] + ENGINEERING_BRANCH)
    # Same header as 013/014: SECURITY DEFINER, volatile, pinned search path, text result.
    for field in ("prosecdef", "provolatile", "proconfig", "result"):
        assert current[field] == v13[field], field
    assert grants["risk"] and not grants["app"]


def test_v2_and_legacy_admission_still_verify_their_jev_receipts(ex, monkeypatch):
    from tests.test_managed_runtime import reviewed_cycle

    engine, _, reviews = ex
    cycle, cycle_id = reviewed_cycle(ex, 3)
    v2 = cycle.approved_packets(cycle_id)[0]
    assert v2["selection_policy"] == "MUSE_JEV_RESEARCH_SELECTION_V2"
    classify(engine, v2["symbol"], theme="CRYPTO_V2_FIXTURE")
    legacy = packet(ex, "ETH/USD")  # The pre-V2 packet shape (no selection policy).
    classify(engine)
    engineering = selected(engine.repo, enroll(engine.repo))
    assert [review_failure(engine.repo, p) for p in (v2, legacy, engineering)] == [None] * 3
    verified, original = [], reviews.verify

    def spy(receipt_id, **options):
        verified.append(str(receipt_id))
        return original(receipt_id, **options)

    monkeypatch.setattr(reviews, "verify", spy)
    for candidate in (v2, legacy, engineering):
        engine.admit(candidate)
    # Each reviewed packet's receipt is verified exactly as before; the enrollment has none.
    assert verified == [str(v2["receipt_id"]), str(legacy["receipt_id"])]
    for changed, code in (
        ({"receipt_id": None}, "RECEIPT_MISSING"),
        ({"receipt_id": str(uuid4())}, "RECEIPT_MISSING"),
        ({"selection_policy": ENGINEERING_SELECTION_POLICY,
          "enrollment_event_seq": engineering["enrollment_event_seq"]},
         "APP_REVIEWED_PACKET_REQUIRED"),
    ):
        with pytest.raises(ValueError, match=code):
            engine.admit({**v2, **changed})
    # Relabelling a reviewed packet as an enrollment binds to nothing it could claim.
    relabeled = {**v2, "selection_policy": ENGINEERING_SELECTION_POLICY, "receipt_id": None,
                 "enrollment_event_seq": engineering["enrollment_event_seq"]}
    assert review_failure(engine.repo, relabeled) == "SELECTION_INTEGRITY_FAILURE"
    with engine.repo.connect() as conn:
        setups = conn.execute("""SELECT receipt_id,record_json->>'purpose' AS purpose
            FROM lab.managed_setups ORDER BY event_seq""").fetchall()
    assert [(s["receipt_id"] is None, s["purpose"]) for s in setups] == [
        (False, None), (False, None), (True, ENGINEERING_PURPOSE)]


# --- The full lifecycle through the paper venue fixture ------------------------------------


def test_engineering_setup_runs_every_normal_gate_to_a_closed_target_exit(ex):
    engine, venue, _ = ex
    classify(engine)
    result = enroll(engine.repo, signal="TEST-MANAGED-LIFECYCLE")
    candidate = selected(engine.repo, result)
    sid = engine.admit(candidate)
    repeated = event_count(engine.repo)
    assert engine.admit(candidate) == sid and event_count(engine.repo) == repeated
    setup, state = engine._load(sid)
    assert setup["receipt_id"] is None and is_engineering(setup["record_json"])
    assert setup["record_json"]["enrollment_event_seq"] == result["enrollment_event_seq"]
    assert state["state"] == "WATCHING" and state["receipt_id"] is None
    # The configured account-risk row and the randomized arm are fixed at admission.
    assert state["risk_policy_id"] == MANAGED_RISK_V2_POLICY_ID
    assert state["arm"] == assign_arm(sid, 30) and state["arm_method"] == ARM_METHOD
    # Admission read the live broker price grid, as for any crypto packet.
    [metadata] = events(engine.repo, "CRYPTO_ASSET_METADATA")
    assert metadata["body"]["symbol"] == "BTC/USD"
    assert [row["active"] for row in status(engine.repo)] == [True]
    # Only an acknowledged print at or below the trigger triggers.
    assert engine.observe_trigger(sid, observation(ex, trade_price="100.01")) is None
    assert not venue.orders
    decision, entry = open_position(ex, sid)
    assert entry["limit_price"] == "100.10" and entry["time_in_force"] == "gtc"
    assert decision["context"]["risk_policy_id"] == MANAGED_RISK_V2_POLICY_ID
    exit_at_target(ex, sid)
    with engine.repo.connect() as conn:
        reservation = conn.execute(
            "SELECT * FROM lab.managed_reservations WHERE setup_id=%s", (sid,)).fetchone()
        claimed = conn.execute(
            """SELECT d.* FROM lab.managed_claims c JOIN lab.managed_risk_decisions d
            USING(decision_id) WHERE d.setup_id=%s""", (sid,)).fetchall()
        remaining = conn.execute(
            "SELECT count(*) AS n FROM lab.account_risk_reservations").fetchone()["n"]
    assert reservation["budget"] == D(50)  # 0.5% of 10,000 under JEV_MANAGED_RISK_V2.
    assert reservation["planned_risk"] <= D(50) and remaining == 0
    # Every broker mutation (entry, stop, stop cancel, market exit) used its own one-use,
    # five-second risk decision.
    mutations = [call for call in venue.calls if call[0] in {"POST", "DELETE", "PATCH"}]
    assert len(mutations) == len(claimed) >= 4
    assert all(d["outcome"] == "APPROVED" and d["method"] in {"POST", "DELETE"}
               and d["expires_at"] - d["created_at"] <= timedelta(seconds=5) for d in claimed)
    assert venue._position_rows() == [] and engine.reconcile()["clean"]
    measured = managed_measurement(engine.repo, sid, as_of=venue.now)
    assert D(measured["gross_realized_pnl"]) == (D("111") - D("100.10")) * D(entry["qty"])
    assert count(engine.repo, "jev_requests") == count(engine.repo, "jev_receipts") == 0
    assert verify_events(engine.repo.export_events())["valid"]
    [row] = status(engine.repo)
    assert not row["active"] and row["setup_state"] == "CLOSED" and row["setup_id"] == sid
    # One engineering test at a time, not one ever: the next enrollment is accepted.
    assert enroll(engine.repo)["enrollment_event_seq"] > result["selection_event_seq"]


def test_jev_never_reviews_an_engineering_position_even_with_reviews_enabled(ex):
    engine, venue, _ = ex
    classify(engine)
    sid = engine.admit(selected(engine.repo, enroll(engine.repo)))
    open_position(ex, sid)
    watcher, calls = monitor(ex, "TIGHTEN_STOP")
    assert watcher.reviews_enabled
    observed = observation(ex, bid="108", ask="108.01")
    for _ in range(2):
        assert asyncio.run(watcher.review(
            sid, observed, bars(ex), fresh_observation=lambda: observed)) is None
    source = BarSource()
    loop = PositionMonitorLoop(watcher, source, BarContextWindow(60, 60), clock=lambda: venue.now)
    [setup] = engine.store.active()
    assert asyncio.run(loop(setup, observed, lambda: observed)) is None
    assert calls == [] and source.calls == [] and count(engine.repo, "jev_requests") == 0
    [skipped] = events(engine.repo, "POSITION_REVIEW_SKIPPED")
    assert skipped["body"] == {"reason": "ENGINEERING_TEST",
                               "lifecycle_id": engine._load(sid)[1]["lifecycle_id"]}
    exit_at_target(ex, sid)  # Protection and mechanical exits are the normal ones.


def test_reporting_excludes_it_while_account_risk_and_reconciliation_include_it(ex):
    engine, venue, _ = ex
    classify(engine, sector="SHARED", theme="SHARED")
    engineering = engine.admit(selected(engine.repo, enroll(engine.repo)))
    strategy = engine.admit(packet(ex, "ETH/USD", sector="SHARED"))
    open_position(ex, engineering)
    with engine.repo.connect() as conn:
        [exposure] = conn.execute("SELECT * FROM lab.account_risk_reservations").fetchall()
    assert exposure["reference_id"] == engineering and exposure["market"] == "CRYPTO"
    assert exposure["policy_id"] == MANAGED_RISK_V2_POLICY_ID
    assert engine.reconcile()["clean"]  # The engineering position is explained, not unknown.
    # Its exposure counts: the same theme is capacity-deferred under JEV_MANAGED_RISK_V2.
    deferred = engine.observe_trigger(strategy, observation(ex))
    assert (deferred["outcome"], deferred["reason"]) == ("REJECTED", "CORRELATION_LIMIT")
    assert engine._load(strategy)[1]["state"] == "WATCHING"
    exit_at_target(ex, engineering)
    venue.now += timedelta(seconds=61)  # Past the V2 capacity cooldown.
    assert engine.reconcile()["clean"]
    open_position(ex, strategy)
    exit_at_target(ex, strategy)
    rollup = managed_daily_rollups(engine.repo)
    [item] = rollup["items"]
    assert item["setup_count"] == item["closed_count"] == 1
    assert rollup["engineering_count"] == 1
    assert rollup["engineering_scope"] == "ENGINEERING_TEST_SETUPS_EXCLUDED_FROM_AGGREGATES"
    gross = managed_measurement(engine.repo, strategy, as_of=venue.now)["gross_realized_pnl"]
    assert D(item["gross_known_sum_usd"]) == D(gross)
    assert managed_daily_rollups(engine.repo, end_date="2000-01-01")["engineering_count"] == 0
    flags = {str(engineering): True, str(strategy): False}
    page = paginated_managed_results(engine.repo)["items"]
    assert {str(i["setup_id"]): i["engineering"] for i in page} == flags
    client = TestClient(create_managed_app(
        FixtureCycle(engine.repo, engine.store), engine.store, api_token=TOKEN,
        runtime_status=lambda: {},
    ))
    headers = {"Authorization": "Bearer " + TOKEN}
    for path in ("setups", "results", "history/results"):
        items = client.get("/api/v1/lab/" + path, headers=headers).json()["items"]
        assert {str(i["setup_id"]): i["engineering"] for i in items} == flags, path
    daily = client.get("/api/v1/lab/analytics/daily", headers=headers).json()
    assert daily["engineering_count"] == 1 and len(daily["items"]) == 1
    measured = client.get(f"/api/v1/lab/positions/{engineering}/measurement",
                          headers=headers).json()
    assert measured["engineering"] and measured["execution_quality"]["engineering"]
    # Research conversion never counts it, even under a started cycle of the same ID.
    with engine.store.transaction() as conn:
        started = engine.store.event(conn, "RESEARCH_STARTED", {
            "cycle_id": str(engine._load(engineering)[0]["cycle_id"]), "contender_count": 0})
    [cycle] = research_funnel(engine.repo, after=started["event_seq"] - 1)["items"]
    assert cycle["admitted_count"] == cycle["entered_count"] == 0


def test_admitted_engineering_packet_is_not_offered_again_and_adds_no_events(ex, monkeypatch):
    from tests.test_managed_runtime import Research, counted_admissions, db_runtime

    engine, _, _ = ex
    classify(engine)
    candidate = selected(engine.repo, enroll(engine.repo))
    run = db_runtime(ex, Research(), "BTC/USD")
    attempts = counted_admissions(engine, monkeypatch)
    seq = candidate["selection_event_seq"]
    assert [p["selection_event_seq"] for p in run._selected_packets()] == [seq]
    run.execution_once()
    assert attempts == [seq]
    [setup] = engine.store.active()
    assert setup["receipt_id"] is None and run._selected_packets() == []
    before = event_count(engine.repo)
    for _ in range(3):
        run.execution_once()
    assert attempts == [seq] and event_count(engine.repo) == before
    assert events(engine.repo, "RUNTIME_ADMISSION_REFUSED") == [] and run.error is None


# --- Staging switch MANAGED_MANAGEMENT_REVIEWS (plan 0.10 runs DISABLED) --------------------


def test_disabled_switch_withholds_every_review_and_keeps_protection(ex):
    engine, venue, _ = ex
    sid = opened(ex, "BTC/USD")  # An ordinary Jev-selected position.
    watcher, calls = monitor(ex, "TIGHTEN_STOP")
    disabled = disabled_monitor(watcher)
    assert watcher.reviews_enabled and not disabled.reviews_enabled
    assert not watcher.review_withheld(sid)
    observed = observation(ex, bid="108", ask="108.01")
    for _ in range(2):
        assert asyncio.run(disabled.review(
            sid, observed, bars(ex), fresh_observation=lambda: observed)) is None
    source = BarSource()
    loop = PositionMonitorLoop(disabled, source, BarContextWindow(60, 60),
                               clock=lambda: venue.now)
    [setup] = engine.store.active()
    assert asyncio.run(loop(setup, observed, lambda: observed)) is None
    assert calls == [] and source.calls == []
    [skipped] = events(engine.repo, "POSITION_REVIEW_SKIPPED")  # Once per lifecycle.
    assert skipped["body"] == {"reason": "MANAGEMENT_REVIEWS_DISABLED",
                               "lifecycle_id": engine._load(sid)[1]["lifecycle_id"]}
    assert count(engine.repo, "jev_requests") == 1  # The selection review only.
    exit_at_target(ex, sid)  # Protection and mechanical exits are untouched.


def test_management_review_setting_is_required_and_exact(ex):
    watcher, _ = monitor(ex)
    arguments = {"policy": watcher.policy, "review_seconds": 10, "clock": watcher.now}
    for value in (None, "", "enabled", "Disabled", "OFF", True):
        with pytest.raises(ValueError, match="MANAGEMENT_REVIEWS_SETTING_REQUIRED"):
            PositionMonitor(watcher.execution, watcher.reviewer, management_reviews=value,
                            **arguments)
    with pytest.raises(TypeError):  # No default: every construction names the setting.
        PositionMonitor(watcher.execution, watcher.reviewer, **arguments)


def test_runtime_status_never_reports_reviews_while_disabled(ex):
    from tests.test_managed_runtime import Research, Source

    engine, venue, _ = ex
    watcher, _ = monitor(ex)

    def build(monitor_tick, **setting):
        return ManagedRuntime(
            engine, Research(), Source(), AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only"),
            engineering_runtime_policy(), clock=lambda: venue.now,
            reviewer_heartbeat=lambda: True, monitor_tick=monitor_tick, **setting,
        )

    def loop(position_monitor):
        return PositionMonitorLoop(position_monitor, BarSource(), BarContextWindow(60, 60),
                                   clock=lambda: venue.now)

    disabled = build(loop(disabled_monitor(watcher)), management_reviews="DISABLED")
    assert disabled.status()["position_jev_configured"] is False
    assert disabled.status()["management_reviews"] == "DISABLED"
    assert not disabled.management_reviews_active()
    enabled = build(loop(watcher), management_reviews="ENABLED")
    assert enabled.status()["position_jev_configured"] is True
    for monitor_tick, setting in ((loop(disabled_monitor(watcher)), {}),  # ENABLED default
                                  (loop(watcher), {"management_reviews": "DISABLED"})):
        with pytest.raises(ValueError, match="MANAGEMENT_REVIEWS_SETTING_MISMATCH"):
            build(monitor_tick, **setting)
    with pytest.raises(ValueError, match="MANAGEMENT_REVIEWS_SETTING_REQUIRED"):
        build(loop(disabled_monitor(watcher)), management_reviews="OFF")
    assert build(None, management_reviews="ENABLED").status()["position_jev_configured"] is False


def factory(monkeypatch, setting, *, captured=None):
    """build_runtime_from_env with fixture collaborators, as in test_managed_runtime.
    ``captured`` (a dict) receives the keyword arguments the engine was built with."""
    import catalyst_lab.authorization as auth
    import catalyst_lab.managed_broker as broker_module
    import catalyst_lab.managed_execution as execution_module
    import catalyst_lab.managed_store as store_module
    import catalyst_lab.research_cycle as cycles
    import catalyst_lab.review_worker as workers
    import catalyst_lab.risk as risk_module
    import catalyst_lab.scan_sources as sources
    from tests.test_managed_runtime import NOW, Execution, Research, Source, configure_env

    configure_env(monkeypatch)
    if setting is None:
        monkeypatch.delenv("MANAGED_MANAGEMENT_REVIEWS")
    else:
        monkeypatch.setenv("MANAGED_MANAGEMENT_REVIEWS", setting)
    monkeypatch.setattr(AlpacaCredentials, "from_env",
                        lambda: AlpacaCredentials("PKFACTORYFIXTURE", "fixture-secret-only"))
    fake_repo = SimpleNamespace(check_role=lambda: None, require_same_database=lambda _: None,
                                connect=lambda: nullcontext(None))
    monkeypatch.setattr(auth, "RiskRepository", lambda *_, **__: fake_repo)
    frozen = AccountRiskPolicy("CATALYST_RETEST_V1", "FROZEN_V1", D("0.01"), D("0.02"),
                               {"US_STOCKS": D("0.02")}, 1, 1, ("US_STOCKS",), False, D(1),
                               None, 0, "LAB_FIXTURE")
    monkeypatch.setattr(risk_module, "load_policy", lambda *_, **__: frozen)
    monkeypatch.setattr(workers, "ReviewWorker", lambda _: SimpleNamespace(
        store=object(), reviewer=SimpleNamespace(store=object()), heartbeat=lambda: True,
        runtime=SimpleNamespace()))
    monkeypatch.setattr(store_module, "ManagedAuthorizationGate",
                        lambda repo: auth.AuthorizationGate(repo))
    monkeypatch.setattr(broker_module, "ManagedPaperBroker", lambda *_, **__: object())
    execution = Execution()
    execution.repo, execution.now = fake_repo, lambda: NOW
    monkeypatch.setattr(execution_module, "ManagedExecution",
                        lambda *_, **options: (
                            {} if captured is None else captured).update(options) or execution)
    monkeypatch.setattr(cycles, "ResearchCycle", lambda *_, **__: Research())
    monkeypatch.setattr(sources, "AlpacaMarketSource", lambda *args: Source())
    return build_runtime_from_env()


def test_launch_setting_selects_reviews_and_is_part_of_the_configuration(monkeypatch):
    disabled = factory(monkeypatch, "DISABLED")
    assert disabled.management_reviews == disabled.position_monitor.management_reviews
    assert disabled.status()["position_jev_configured"] is False
    enabled = factory(monkeypatch, "ENABLED")
    assert enabled.position_monitor.reviews_enabled
    assert enabled.status()["position_jev_configured"] is True
    assert enabled.configuration_hash != disabled.configuration_hash


@pytest.mark.parametrize("setting", [None, "", "enabled", "Disabled", "OFF", "true"])
def test_launch_refuses_a_missing_or_invalid_management_review_setting(monkeypatch, setting):
    with pytest.raises(ValueError, match="REQUIRED_MANAGED_CONFIGURATION_MISSING_OR_INVALID"):
        factory(monkeypatch, setting)


def test_private_configuration_requires_the_setting_and_the_example_disables_reviews(tmp_path):
    assert "MANAGED_MANAGEMENT_REVIEWS" in REQUIRED_ENV
    assert config_template(tmp_path)["environment"]["MANAGED_MANAGEMENT_REVIEWS"] == "REQUIRED"
    example = json.loads((Path(__file__).resolve().parents[1] / "deploy" /
                          "private-paper.example.json").read_text())
    assert example["environment"]["MANAGED_MANAGEMENT_REVIEWS"] == "DISABLED"


# --- Migration 017 on a populated schema-16 ledger ------------------------------------------


def test_populated_schema16_ledger_migrates_through_017_to_020_with_audit_intact():
    from tests.test_account_risk_policy import populate_reservations
    from tests.test_operator_controls import audit_state, populate_schema14, start_cluster_at

    with tempfile.TemporaryDirectory(prefix="catalyst-017-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 16)
            populate_schema14(root)  # Halts, reconciliation runs, managed events, receipts.
            populate_reservations(root)  # A frozen and a receipt-bearing managed setup.
            before, audited, halts, version = audit_state(root)
            assert version == 16 and before["valid"]
            assert all(n == ok for n, ok in audited.values()) and audited["managed_setups"][0]
            owner = Repository(localdb.connection_url(root, "lab_owner"))

            def setup_rows():
                with owner.connect() as conn:
                    return conn.execute("""SELECT to_jsonb(s) AS row FROM lab.managed_setups s
                        ORDER BY event_seq""").fetchall()

            rows = setup_rows()
            with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
                conn.execute((MIGRATIONS / "017_engineering_enrollment.sql").read_text())
                # The code requires the current schema, so 018 (selection rule B1), 019
                # (operator flatten), 020 (selection rule B2) and 021 (selection rule top-K),
                # all DDL-only, are applied too; the enrollment checks below then run under the
                # current code.
                conn.execute((MIGRATIONS / "018_selection_b1.sql").read_text())
                conn.execute((MIGRATIONS / "019_operator_flatten.sql").read_text())
                conn.execute((MIGRATIONS / "020_selection_b2.sql").read_text())
                conn.execute((MIGRATIONS / "021_selection_topk.sql").read_text())
            after, audited_after, halts_after, version_after = audit_state(root)
            # DDL only: the same events, head and hashes; every historical row still verifies.
            assert version_after == 21 and after == before
            # 022 (JEV_MANAGED_RISK_V3), the current schema, appends exactly its two audited
            # policy rows and nothing else; the checks below then run under the current code.
            from tests.test_crypto_size_hold import apply_migration_022

            apply_migration_022(root)
            assert audit_state(root)[3] == 22
            # 023 (selection rule top-K V2), 024 (public experiment views) and 025 (Jev review
            # policy V2), all DDL-only, complete the current schema.
            with owner.connect() as conn:
                conn.execute((MIGRATIONS / "023_selection_topk_v2.sql").read_text())
                conn.execute((MIGRATIONS / "024_public_experiment.sql").read_text())
                conn.execute((MIGRATIONS / "025_jev_review_policy_v2.sql").read_text())
            # 026 (JEV_MANAGED_RISK_V4) appends exactly its three audited policy rows.
            from tests.test_risk_v4 import apply_migration_026

            apply_migration_026(root)
            # 027 (the trade plan's planned-stop guard) adds no row.
            from tests.test_trade_plan_migration import apply_migration_027

            apply_migration_027(root)
            # 028, 029 (public page views) and 030 (JEV_TOP_K_SELECTION_V3) add no row.
            from tests.test_selection_topk_v3 import apply_migrations_after_027

            apply_migrations_after_027(root)  # And 031 (package plugin-c3, four policy rows).
            assert audit_state(root)[3] == 31
            assert {t: v for t, v in audited_after.items() if t in audited} == audited
            assert audited_after["operator_flatten_completions"] == (0, 0)
            assert halts_after == halts
            assert setup_rows() == rows  # Dropping NOT NULL changed no row's to_jsonb.
            with owner.connect() as conn:
                nullable = conn.execute("""SELECT is_nullable FROM information_schema.columns
                    WHERE table_schema='lab' AND table_name='managed_setups'
                    AND column_name='receipt_id'""").fetchone()["is_nullable"]
                grants = conn.execute("""SELECT
                    has_function_privilege('catalyst_operator',%(f)s,'EXECUTE') AS operator,
                    has_function_privilege('catalyst_risk',%(f)s,'EXECUTE') AS risk""",
                    {"f": "lab.operator_enroll_managed_engineering(text,text,numeric,numeric,"
                          "numeric,numeric,integer,text)"}).fetchone()
            assert nullable == "YES" and grants["operator"] and not grants["risk"]
            # The migrated ledger enrolls through the operator login once its historical halt
            # is released through the audited operator path, and still verifies.
            risk = RiskRepository(localdb.connection_url(root, "catalyst_risk"))
            classify(SimpleNamespace(repo=risk, store=ExecutionStore(risk)))
            with refused("RISK_HALT"):
                enroll(risk)
            [held] = halts
            with as_role(risk, OPERATOR_ROLE).connect() as conn:
                conn.execute("SELECT lab.operator_release_halt(%s,%s)",
                             (held["event_seq"], "Fixture release after clean reconciliation"))
            result = enroll(risk)
            assert result["operator_role"] == OPERATOR_ROLE
            assert review_failure(risk, selected(risk, result)) is None
            assert verify_events(owner.export_events())["valid"]
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def ExecutionStore(repository):  # noqa: N802 - a ManagedStore for the classification helper.
    from catalyst_lab.managed_store import ManagedStore

    return ManagedStore(repository)


# --- The operator CLI --------------------------------------------------------------------


def private_config(tmp_path, database_url):
    config = config_template(tmp_path)
    config["environment"]["MANAGED_DATABASE_URL"] = database_url
    path = tmp_path / "private.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


def cli_arguments(path, **changes):
    options = {"--symbol": "BTC/USD", "--entry-trigger": "100", "--max-entry": "100.10",
               "--stop": "95", "--target": "111", "--expires-minutes": "30",
               "--reason": REASON} | changes
    return ["enroll", "--config", str(path), *[v for pair in options.items() for v in pair]]


def test_cli_enrolls_prints_sequences_and_never_a_credential(ex, tmp_path, capsys, monkeypatch):
    engine, _, _ = ex
    classify(engine)
    monkeypatch.delenv("OPERATOR_DATABASE_URL", raising=False)
    secret = "fixture-password-never-printed"
    path = private_config(tmp_path, url(engine.repo, "catalyst_risk") + " password=" + secret)
    config = load_private_config(path)
    derived = operator_database_url(config)
    assert "user=catalyst_operator" in derived and secret not in derived
    result = engineering_cli(cli_arguments(path))
    assert json.loads(capsys.readouterr().out) == result
    assert result["action"] == "enroll" and result["mode"] == "PAPER_ONLY"
    assert result["enrollment_event_seq"] < result["selection_event_seq"]
    assert result["packet_id"] and result["signal_id"].startswith("TEST-MANAGED-")
    assert result["operator_role"] == OPERATOR_ROLE and result["levels"] == LEVELS
    shown = engineering_cli(["status", "--config", str(path)])
    [row] = shown["enrollments"]
    assert shown["active_count"] == 1 and row["active"] and row["reason"] == REASON
    for arguments, code in (
        (cli_arguments(path), "ENGINEERING_TEST_ALREADY_ACTIVE"),
        (cli_arguments(path, **{"--symbol": "SPY"}), "ENGINEERING_ENROLLMENT_CRYPTO_ONLY"),
        (cli_arguments(path, **{"--target": "110"}), "MIN_REWARD_RISK"),
        (cli_arguments(path, **{"--expires-minutes": "600"}), "ENGINEERING_EXPIRY_INVALID"),
        (["status", "--config", str(path), "--database-url", url(engine.repo, "catalyst_app")],
         "OPERATOR_ROLE_REQUIRED"),
    ):
        with pytest.raises(SystemExit) as failure:
            engineering_cli(arguments)
        assert str(failure.value) == "ENGINEERING_ENROLLMENT_REFUSED: " + code
    loose = tmp_path / "loose.json"
    loose.write_text(path.read_text())
    loose.chmod(0o644)
    with pytest.raises(SystemExit) as failure:
        engineering_cli(["status", "--config", str(loose)])
    assert str(failure.value) == (
        "ENGINEERING_ENROLLMENT_REFUSED: INVALID_PRIVATE_LAUNCH_CONFIGURATION")
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "host=" not in captured.out + captured.err and "/tmp" not in captured.out
    assert len(events(engine.repo, ENROLLED_KIND)) == 1


# --- Broker price grid: recorded metadata at enrollment, else the live check at admission ---


def test_off_grid_levels_are_refused_at_admission_then_at_enrollment(ex):
    engine, venue, _ = ex
    classify(engine)
    off_grid = {**LEVELS, "entry_trigger": "100.005", "max_entry_price": "100.105"}
    deferred = enroll(engine.repo, levels=off_grid)  # No recorded broker metadata yet.
    assert deferred["grid_check"] == "DEFERRED_TO_ADMISSION_LIVE_BROKER_CHECK"
    candidate = selected(engine.repo, deferred)
    for _ in range(2):  # Final for this enrollment, recorded once, no second broker read.
        with pytest.raises(ValueError, match="CRYPTO_LEVEL_OFF_PRICE_GRID"):
            engine.admit(candidate)
    [refusal] = events(engine.repo, "CRYPTO_ADMISSION_REFUSED")
    assert refusal["idempotency_key"] == (
        f"crypto-admission-refused:engineering:{deferred['enrollment_event_seq']}")
    assert refusal["body"]["receipt_id"] is None
    assert sum(1 for c in venue.calls if c[1] == "/v2/assets/BTC/USD") == 1
    [row] = status(engine.repo)
    assert not row["active"] and row["final_refusal"] == "CRYPTO_LEVEL_OFF_PRICE_GRID"
    with refused("CRYPTO_LEVEL_OFF_PRICE_GRID"):  # Now checked against recorded metadata.
        enroll(engine.repo, levels=off_grid)
    accepted = enroll(engine.repo)
    assert accepted["grid_check"] == "RECORDED_BROKER_METADATA"
    assert count(engine.repo, "managed_setups") == 0 and not venue.orders
