"""Operator flatten execution (migration 019) on disposable PostgreSQL with the fake paper venue.

``managed_ops operator flatten-all`` records a durable request (migration 015); the running app's
account-safety tick executes it through the daily halt's cancel-then-close path, where every
cancel and every close has its own exact one-use five-second authorization, and appends each
outcome to ``lab.operator_flatten_completions``. Every request, order, fill, halt and
reconciliation below is a labelled local fixture (``ManagedVenue``/``CountingVenue``, LAB_FIXTURE
rows): none of it is broker, provider or deployment evidence, and no owner ledger is touched.
"""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from catalyst_lab import localdb
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.config import SCHEMA_VERSION
from catalyst_lab.managed_account_safety import (
    FLATTEN_EXECUTED,
    OPERATOR_FLATTEN,
    ManagedAccountSafety,
)
from catalyst_lab.managed_app import create_application
from catalyst_lab.managed_ops import FLATTEN_PENDING_ALARM_SECONDS, status_alarms, watchdog_once
from catalyst_lab.managed_ops import main as managed_ops
from catalyst_lab.managed_service import STATUS_FIELDS
from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
from catalyst_lab.repository import Repository
from tests.test_broker_budget import bx as bx
from tests.test_broker_budget import governed as governed
from tests.test_broker_budget import open_position
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_app import TOKEN as APP_TOKEN
from tests.test_managed_app import runtime as runtime
from tests.test_managed_app import settings as settings
from tests.test_managed_app import source_factory as source_factory
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_managed_ops import healthy, heartbeat
from tests.test_managed_ops import ops as ops
from tests.test_operator_controls import audit_state, populate_schema14, start_cluster_at
from tests.test_position_monitor import opened

CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
MIGRATION = Path(localdb.__file__).with_name("migrations") / "019_operator_flatten.sql"
REASON = "Operator flatten fixture: protection latched with a position open"


def with_role(repo, role):
    return repo.database_url.replace("user=catalyst_risk", "user=" + role).replace(
        "user=catalyst_app", "user=" + role
    )


def request_flatten(repo, reason=REASON):
    """The audited 015 request, exactly as the operator CLI records it."""
    with Repository(with_role(repo, "catalyst_operator")).connect() as conn:
        request_id = conn.execute(
            "SELECT lab.operator_request_flatten_all(%s) AS id", (reason,)
        ).fetchone()["id"]
        return conn.execute(
            "SELECT * FROM lab.operator_list_flattens(true) WHERE request_id=%s", (request_id,)
        ).fetchone()["request_seq"]


def rows(repo, statement, *params):
    with repo.connect() as conn:
        return conn.execute(statement, params).fetchall()


def completions(repo):
    return rows(repo, "SELECT * FROM lab.operator_flatten_completions ORDER BY event_seq")


def pending(repo):
    return [r["event_seq"] for r in rows(
        repo, "SELECT event_seq FROM lab.pending_operator_flatten_requests ORDER BY event_seq"
    )]


def executed(repo):
    return [r["body"] for r in rows(
        repo, "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
        FLATTEN_EXECUTED,
    )]


def halts(repo):
    return [r["reason"] for r in rows(repo, "SELECT reason FROM lab.execution_halts")]


def state(engine, sid):
    return engine._load(sid)[1]


def flatten_decisions(repo, since):
    """Every cancel and close authorized since the request, with its one claim."""
    return rows(
        repo,
        """SELECT d.decision_id,d.setup_id,d.action,d.method,d.path,d.payload,d.created_at,
          d.expires_at,(SELECT count(*) FROM lab.managed_claims c
            WHERE c.decision_id=d.decision_id) AS claims,
          EXISTS(SELECT 1 FROM lab.managed_events x WHERE x.kind='BROKER_REJECTED'
            AND x.body->>'decision_id'=d.decision_id::text) AS refused
        FROM lab.managed_risk_decisions d WHERE d.outcome='APPROVED'
        AND d.action IN ('CANCEL','EXIT') AND d.event_seq>%s ORDER BY d.event_seq""",
        since,
    )


def assert_audited(repo):
    """Every completion row equals its hash-chained audit event, and the chain verifies.

    Read in a UTC session, as every writer and verifier of audited rows is (timestamps)."""
    owner = Repository(with_role(repo, "lab_owner"))
    with owner.connect() as conn:
        row = conn.execute(
            """SELECT count(*) AS n,count(*) FILTER (WHERE lab.research_audit_matches(
            'OPERATOR_FLATTEN_COMPLETIONS',to_jsonb(t),t.event_seq)) AS ok
            FROM lab.operator_flatten_completions t"""
        ).fetchone()
    assert row["n"] == row["ok"]
    assert verify_events(owner.export_events())["valid"]


def fill_closes(venue, ingest):
    """Fill every working market-sell close in full, delivered like a trade update."""
    for order in venue.orders_of("sell", "market"):
        if order["status"] == "new":
            ingest(venue.fill(order["id"], order["qty"], price="100"))


def market_sells(venue):
    return [json.loads(content) for method, path, content in venue.calls
            if method == "POST" and path == "/v2/orders"
            and json.loads(content)["type"] == "market"]


@pytest.fixture
def safety(mx):
    """The shared-account coordinator over the ungoverned fixture venue."""
    engine, venue, _ = mx
    client = RiskAuthorizedPaperClient(
        CREDENTIALS, AuthorizationGate(engine.repo, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    yield ManagedAccountSafety(engine, client)
    client.close()


def drive(mx, safety, *, ticks=8):
    """Account-safety tick, then each active setup's controller, as one runtime tick does."""
    engine, venue, _ = mx
    for _ in range(ticks):
        safety.tick()
        for setup in engine.store.active():
            engine.manage(setup["setup_id"], observation(mx))
        fill_closes(venue, engine.ingest)
        venue.now += timedelta(seconds=1)


# --- Nothing without a request -------------------------------------------------------------


def test_nothing_runs_without_a_pending_request(bx, governed):
    engine, venue, _ = bx
    run = governed
    crypto, stock = open_position(bx, "BTC/USD"), open_position(bx, "SPY")
    before = len(venue.calls)
    for _ in range(5):
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert halts(engine.repo) == [] and completions(engine.repo) == []
    assert executed(engine.repo) == [] and pending(engine.repo) == []
    assert state(engine, crypto).get("exit_requested") is None
    assert state(engine, stock).get("exit_requested") is None
    assert not any(method in {"POST", "DELETE"} for method, _, _ in venue.calls[before:])
    assert run.status()["operator_flatten"] == {
        "pending_count": 0, "oldest_pending_request_seq": None,
        "oldest_pending_requested_at": None, "residual": [],
    }
    assert run.error is None


# --- The wired flatten ----------------------------------------------------------------------


def test_flatten_all_cancels_then_closes_with_one_claim_each_and_completes(
    bx, governed, capsys, monkeypatch
):
    engine, venue, _ = bx
    run = governed
    crypto, stock = open_position(bx, "BTC/USD"), open_position(bx, "SPY")
    watching = engine.admit(packet(bx, "ETH/USD"))
    monkeypatch.setenv("OPERATOR_DATABASE_URL", with_role(engine.repo, "catalyst_operator"))
    result = managed_ops(["operator", "flatten-all", "--reason", REASON])
    assert json.loads(capsys.readouterr().out) == result
    seq = result["request_seq"]
    # The 015 shape, the new broker action and the new request_seq field.
    assert set(result) == {"action", "mode", "flatten_request_id", "recorded", "broker_action",
                           "request_seq"}
    assert result["recorded"] and result["mode"] == "PAPER_ONLY"
    assert result["broker_action"] == "PENDING_ACCOUNT_SAFETY_TICK"
    assert pending(engine.repo) == [seq] and halts(engine.repo) == []  # Nothing until a tick.

    run.execution_once()  # Halt, exit requests, and this tick's cancels.
    assert halts(engine.repo) == [OPERATOR_FLATTEN]
    assert state(engine, watching)["state"] == "INVALIDATED"
    assert state(engine, watching)["reason"] == OPERATOR_FLATTEN
    for sid in (crypto, stock):
        assert state(engine, sid)["exit_requested"] == OPERATOR_FLATTEN
    assert not venue.orders_of("sell", "market")  # Cancels first; no close yet.
    with pytest.raises(ValueError, match="RISK_HALT"):
        engine.admit(packet(bx, "SOL/USD"))
    for _ in range(6):
        venue.now += timedelta(seconds=1)
        run.execution_once()
        fill_closes(venue, run.ingest)
    assert pending(engine.repo) == [] and run.error is None
    assert state(engine, crypto)["state"] == state(engine, stock)["state"] == "CLOSED"
    assert all(qty == 0 for qty in venue.inventory.values())
    assert not rows(engine.repo, "SELECT 1 FROM lab.managed_active_reservations")

    decisions = flatten_decisions(engine.repo, seq)
    cancels = [d for d in decisions if d["action"] == "CANCEL"]
    closes = [d for d in decisions if d["action"] == "EXIT"]
    assert len(cancels) == 3 and len(closes) == 2  # Crypto stop-limit; both stock legs.
    assert all(d["claims"] == 1 and not d["refused"] for d in decisions)
    assert all((d["expires_at"] - d["created_at"]).total_seconds() <= 5 for d in decisions)
    deletes = [path for method, path, _ in venue.calls if method == "DELETE"]
    assert sorted(deletes) == sorted(d["path"] for d in cancels)  # One DELETE per claim.
    assert len(market_sells(venue)) == 2  # One POST per close claim.
    for symbol in ("BTC/USD", "SPY"):
        order = [i for i, (method, path, content) in enumerate(venue.calls)
                 if method == "POST" and content and json.loads(content)["symbol"] == symbol
                 and json.loads(content)["type"] == "market"]
        cancelled = [i for i, (method, path, _) in enumerate(venue.calls) if method == "DELETE"
                     and venue.orders[path.rsplit("/", 1)[1]]["symbol"] == symbol]
        assert len(order) == 1 and cancelled and max(cancelled) < order[0]  # In order.

    [completion] = completions(engine.repo)
    assert completion["outcome"] == "COMPLETED" and completion["residual_codes"] == []
    assert completion["request_seq"] == seq and str(completion["runtime_id"]) == run.runtime_id
    assert (completion["cancels_attempted"], completion["cancels_acknowledged"]) == (3, 3)
    assert (completion["closes_attempted"], completion["closes_acknowledged"]) == (2, 2)
    [event] = executed(engine.repo)
    assert event["request_seq"] == seq and event["outcome"] == "COMPLETED"
    assert event["cancels"] == {"attempted": 3, "acknowledged": 3}
    assert event["closes"] == {"attempted": 2, "acknowledged": 2}
    assert event["residual"] == [] and event["exposure"]["broker_read"] == "FRESH"

    # Completed: later ticks write nothing and touch nothing; entries stay blocked.
    calls = len(venue.calls)
    for _ in range(3):
        venue.now += timedelta(seconds=1)
        run.execution_once()
    assert len(completions(engine.repo)) == 1 and len(executed(engine.repo)) == 1
    assert not any(m in {"POST", "DELETE"} for m, _, _ in venue.calls[calls:])
    assert halts(engine.repo) == [OPERATOR_FLATTEN]
    assert run.status()["operator_flatten"]["pending_count"] == 0
    assert_audited(engine.repo)


def test_refused_close_stays_pending_is_retried_after_its_backoff_and_latches_nothing(
    bx, governed
):
    engine, venue, _ = bx
    run = governed
    sid = open_position(bx, "BTC/USD")
    seq = request_flatten(engine.repo)
    venue.reject_market_sells = 1
    run.execution_once()  # Halt and exit request; the stop-limit is cancelled.
    run.execution_once()  # The close is refused (422).
    [refused] = [d for d in flatten_decisions(engine.repo, seq) if d["action"] == "EXIT"]
    assert refused["refused"] and not venue.orders_of("sell", "market")
    assert pending(engine.repo) == [seq] and completions(engine.repo) == []
    run.execution_once()  # PARTIAL history; the close waits out its one-second backoff.
    [partial] = completions(engine.repo)
    assert partial["outcome"] == "PARTIAL" and pending(engine.repo) == [seq]
    assert partial["residual_codes"] == ["BROKER_REFUSED", "MANAGED_EXPOSURE_OPEN"]
    assert (partial["closes_attempted"], partial["closes_acknowledged"]) == (1, 0)
    # The refusal itself recorded the retry's new revision (plan phase 0, 2026-09-26).
    assert state(engine, sid)["exit_retry_of"] == str(refused["decision_id"])
    assert not venue.orders_of("sell", "market")
    venue.now += timedelta(seconds=1)  # The first retry is due one second after the refusal.
    run.execution_once()
    [close] = venue.orders_of("sell", "market")  # A fresh ID under a fresh authorization.
    run.ingest(venue.fill(close["id"], close["qty"], price="100"))
    for _ in range(3):
        venue.now += timedelta(seconds=1)
        run.execution_once()
    assert pending(engine.repo) == [] and state(engine, sid)["state"] == "CLOSED"
    partial, completed = completions(engine.repo)
    assert completed["outcome"] == "COMPLETED" and completed["residual_codes"] == []
    assert (completed["closes_attempted"], completed["closes_acknowledged"]) == (2, 1)
    closes = [d for d in flatten_decisions(engine.repo, seq) if d["action"] == "EXIT"]
    assert [d["refused"] for d in closes] == [True, False]
    assert all(d["claims"] == 1 for d in closes)
    ids = [d["payload"]["client_order_id"] for d in closes]
    assert len(set(ids)) == 2 and [s["client_order_id"] for s in market_sells(venue)] == ids
    # Refusals are history, not latches: nothing is latched and nothing blocks protection.
    assert run.error is None and run.latches.public()["active"] == []
    assert [e["outcome"] for e in executed(engine.repo)] == ["PARTIAL", "COMPLETED"]
    assert_audited(engine.repo)


def test_unknown_close_response_is_looked_up_never_resent(mx, safety, monkeypatch):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    seq = request_flatten(engine.repo)
    safety.tick()
    engine.manage(sid, observation(mx))  # The stop-limit is cancelled.
    venue.timeout_next_post = True  # The broker accepts the close; the response is lost.
    monkeypatch.setattr(engine.broker, "order_by_client_id", lambda client_id: None)
    engine.manage(sid, observation(mx))
    engine.manage(sid, observation(mx))  # The client ID is not found: recovery stays pending.
    safety.tick()
    [partial] = completions(engine.repo)
    assert partial["outcome"] == "PARTIAL" and pending(engine.repo) == [seq]
    assert partial["residual_codes"] == [
        "AUTHORIZATION_UNRESOLVED", "BROKER_RESPONSE_UNKNOWN", "MANAGED_EXPOSURE_OPEN"
    ]
    safety.tick()
    assert len(completions(engine.repo)) == 1  # A lasting condition is written once.
    monkeypatch.undo()  # The lookup succeeds on a later tick.
    drive(mx, safety, ticks=3)
    assert pending(engine.repo) == [] and state(engine, sid)["state"] == "CLOSED"
    assert len(market_sells(venue)) == 1  # Recovered by its client ID, never resent.
    assert [c["outcome"] for c in completions(engine.repo)] == ["PARTIAL", "COMPLETED"]
    assert_audited(engine.repo)


def test_rate_limited_flatten_records_failed_history_without_a_sticky_latch(bx, governed):
    engine, venue, _ = bx
    run = governed
    sid = open_position(bx, "BTC/USD")
    seq = request_flatten(engine.repo)
    run.execution_once()  # Halt, exit request and the stop-limit cancel.
    venue.now += timedelta(seconds=5)  # The shared snapshot is due for a refresh.
    venue.rate_limited_until = venue.now + timedelta(seconds=8)
    for _ in range(8):
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert run.error == "REST_DEGRADED" and pending(engine.repo) == [seq]
    failed = completions(engine.repo)
    assert failed and {c["outcome"] for c in failed} == {"FAILED"}
    assert {"ALPACA_HTTP_429"} <= {code for c in failed for code in c["residual_codes"]}
    assert len(failed) <= 3  # One row per failure code and runtime, not one per tick.
    for _ in range(40):  # After the storm the close goes out and the latch clears itself.
        run.execution_once()
        fill_closes(venue, run.ingest)
        venue.now += timedelta(seconds=1)
        if not pending(engine.repo) and run.error is None:
            break
    assert pending(engine.repo) == [] and state(engine, sid)["state"] == "CLOSED"
    assert completions(engine.repo)[-1]["outcome"] == "COMPLETED"
    assert run.error is None and not run.latches.blocking()
    assert len(market_sells(venue)) == 1
    assert_audited(engine.repo)


def test_exposure_no_ledger_owns_is_reported_never_touched_and_blocks_completion(mx, safety):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    venue.inventory["UNKNOWN"] = D(2)  # A position neither engine owns (fixture).
    seq = request_flatten(engine.repo)
    drive(mx, safety, ticks=5)
    assert state(engine, sid)["state"] == "CLOSED"  # Everything owned is flat.
    [partial] = completions(engine.repo)
    assert partial["outcome"] == "PARTIAL" and partial["residual_codes"] == [
        "UNOWNED_BROKER_POSITION"]
    assert executed(engine.repo)[-1]["exposure"]["unowned_positions"] == ["UNKNOWN"]
    assert pending(engine.repo) == [seq] and venue.inventory["UNKNOWN"] == D(2)
    assert not any(content and json.loads(content).get("symbol") == "UNKNOWN"
                   for method, _, content in venue.calls if method == "POST")
    venue.inventory.pop("UNKNOWN")  # The operator closes it outside the app.
    drive(mx, safety, ticks=1)
    assert pending(engine.repo) == []
    assert [c["outcome"] for c in completions(engine.repo)] == ["PARTIAL", "COMPLETED"]
    assert_audited(engine.repo)


def test_crypto_dust_the_controller_cannot_sell_leaves_the_request_pending(mx, safety):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    for order in venue.orders_of("sell"):
        order["status"] = "canceled"  # Broker-side cancellation (fixture).
    venue.inventory["BTC/USD"] = D("0.00001")  # Below the broker's minimum order size.
    seq = request_flatten(engine.repo)
    drive(mx, safety, ticks=3)
    assert state(engine, sid)["state"] == "OPEN"
    [partial] = completions(engine.repo)
    assert partial["outcome"] == "PARTIAL"
    assert partial["residual_codes"] == ["MANAGED_PROTECTION_HALTED"]
    assert pending(engine.repo) == [seq] and not market_sells(venue)
    assert set(halts(engine.repo)) == {
        OPERATOR_FLATTEN, "MANAGED_CRYPTO_RESIDUAL_BELOW_BROKER_MINIMUM"}


def test_second_request_while_pending_is_coalesced_into_the_same_flatten(mx, safety):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    first = request_flatten(engine.repo, "First operator flatten request (fixture)")
    safety.tick()
    engine.manage(sid, observation(mx))  # The stop-limit cancel belongs to the first only.
    second = request_flatten(engine.repo, "Second request while the first is pending")
    assert pending(engine.repo) == [first, second]  # Recorded, never refused.
    drive(mx, safety, ticks=4)
    assert pending(engine.repo) == [] and state(engine, sid)["state"] == "CLOSED"
    by_request = {c["request_seq"]: c for c in completions(engine.repo)}
    assert set(by_request) == {first, second}
    assert all(c["outcome"] == "COMPLETED" for c in by_request.values())
    assert by_request[first]["finished_at"] == by_request[second]["finished_at"]  # One pass.
    assert (by_request[first]["cancels_attempted"], by_request[first]["closes_attempted"]) == (
        1, 1)
    assert (by_request[second]["cancels_attempted"], by_request[second]["closes_attempted"]) == (
        0, 1)
    assert halts(engine.repo) == [OPERATOR_FLATTEN]  # One halt for both.
    assert len(market_sells(venue)) == 1 and len(executed(engine.repo)) == 2
    assert_audited(engine.repo)


def test_entries_stay_blocked_until_the_operator_releases_the_flatten_halt(
    mx, safety, capsys, monkeypatch
):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    request_flatten(engine.repo)
    safety.tick()
    operator = Repository(with_role(engine.repo, "catalyst_operator"))
    with operator.connect() as conn:
        [flatten_halt] = conn.execute("SELECT * FROM lab.operator_list_halts()").fetchall()
    assert flatten_halt["reason"] == OPERATOR_FLATTEN
    assert flatten_halt["release_requirement"] == "RECONCILIATION"
    assert engine.reconcile()["clean"]  # A clean reconciliation after the halt, mid-flatten.
    with operator.connect() as conn:
        [listed] = conn.execute("SELECT * FROM lab.operator_list_halts()").fetchall()
        assert listed["release_blockers"] == ["OPERATOR_FLATTEN_PENDING"]
        with pytest.raises(psycopg.errors.RaiseException, match="OPERATOR_FLATTEN_PENDING"):
            conn.execute("SELECT lab.operator_release_halt(%s,%s)",
                         (flatten_halt["halt_id"], "Released in the middle of a flatten"))
    with operator.connect() as conn, pytest.raises(
        psycopg.errors.RaiseException, match="NO_ACTIVE_OPERATOR_PAUSE"
    ):
        conn.execute("SELECT lab.operator_resume('Resume never releases a flatten halt')")
    drive(mx, safety, ticks=4)
    assert pending(engine.repo) == [] and state(engine, sid)["state"] == "CLOSED"
    with pytest.raises(ValueError, match="RISK_HALT"):  # Completed, and still blocked.
        engine.admit(packet(mx, "ETH/USD"))
    assert engine.reconcile()["clean"]
    monkeypatch.setenv("OPERATOR_DATABASE_URL", with_role(engine.repo, "catalyst_operator"))
    released = managed_ops(["operator", "release-halt", str(flatten_halt["halt_id"]),
                            "--reason", "Flatten completed and the account reconciles clean"])
    capsys.readouterr()
    assert released["released"] and released["halt_reason"] == OPERATOR_FLATTEN
    assert halts(engine.repo) == []
    assert engine.admit(packet(mx, "ETH/USD"))


def test_operator_status_and_list_halts_show_pending_and_completed_flattens(
    mx, safety, capsys, monkeypatch
):
    engine, _, _ = mx
    monkeypatch.setenv("OPERATOR_DATABASE_URL", with_role(engine.repo, "catalyst_operator"))
    recorded = managed_ops(["operator", "flatten-all", "--reason", "Flat-account fixture"])
    status = managed_ops(["operator", "status"])
    assert status["pending_flatten_count"] == 1 and status["active_count"] == 0
    [row] = status["flattens"]
    assert row["request_seq"] == recorded["request_seq"] and row["pending"]
    assert row["completion_rows"] == 0 and row["last_outcome"] is None
    listing = managed_ops(["operator", "list-halts"])
    assert listing["halts"] == [] and listing["pending_flatten_count"] == 1
    assert [f["request_id"] for f in listing["flattens"]] == [recorded["flatten_request_id"]]
    safety.tick()  # Nothing is open: the first tick confirms flat and completes.
    status = managed_ops(["operator", "status"])
    assert status["pending_flatten_count"] == 0 and status["active_count"] == 1
    [halt] = status["active_halts"]
    assert halt["reason"] == OPERATOR_FLATTEN and halt["release_requirement"] == "RECONCILIATION"
    [row] = status["flattens"]
    assert not row["pending"] and row["last_outcome"] == "COMPLETED"
    assert row["completed_at"] and row["last_residual_codes"] == []
    assert (row["cancels_attempted"], row["closes_attempted"]) == (0, 0)
    assert managed_ops(["operator", "list-halts"])["flattens"] == []
    history = managed_ops(["operator", "list-halts", "--all"])["flattens"]
    assert [f["last_outcome"] for f in history] == ["COMPLETED"]
    capsys.readouterr()


# --- Watchdog and status --------------------------------------------------------------------


def test_watchdog_alarms_flatten_pending_after_sixty_seconds(ops):
    path, config = ops
    now = datetime.now(UTC)
    digest = config["release"]["source_sha256"]

    def status(age=None, **flatten):
        body = healthy(now, digest)
        if age is not None:
            flatten.setdefault("oldest_pending_requested_at",
                               (now - timedelta(seconds=age)).isoformat())
        if flatten:
            body["operator_flatten"] = {"pending_count": 1, "oldest_pending_request_seq": 9,
                                        "residual": [], **flatten}
        return body

    policy = config["watchdog"]
    assert FLATTEN_PENDING_ALARM_SECONDS == 60
    assert "FLATTEN_PENDING" not in status_alarms(status(), now, policy)  # Older app.
    assert "FLATTEN_PENDING" not in status_alarms(
        status(oldest_pending_requested_at=None, pending_count=0), now, policy)
    assert "FLATTEN_PENDING" not in status_alarms(status(59), now, policy)
    assert "FLATTEN_PENDING" not in status_alarms(status(60), now, policy)
    assert "FLATTEN_PENDING" in status_alarms(status(61), now, policy)
    assert "FLATTEN_PENDING" in status_alarms(status(-5), now, policy)  # Future: fail closed.
    assert "FLATTEN_PENDING" in status_alarms(
        status(oldest_pending_requested_at="not-a-time"), now, policy)
    heartbeat(config, now)
    result = watchdog_once(config, now=now, config_path=path, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=status(61))))
    assert result["alarms"] == ["FLATTEN_PENDING"]


def test_runtime_status_reports_the_pending_flatten_through_the_status_route(
    bx, governed, runtime, settings, source_factory
):
    engine, venue, _ = bx
    open_position(bx, "BTC/USD")
    seq = request_flatten(engine.repo)
    governed.execution_once()  # Exits requested and the stop-limit cancelled: in progress.
    flatten = governed.status()["operator_flatten"]
    assert flatten["pending_count"] == 1 and flatten["oldest_pending_request_seq"] == seq
    assert datetime.fromisoformat(flatten["oldest_pending_requested_at"]).tzinfo is not None
    assert flatten["residual"] == ["MANAGED_EXPOSURE_OPEN"]
    assert pending(engine.repo) == [seq] and completions(engine.repo) == []
    assert "operator_flatten" in STATUS_FIELDS
    runtime.status = lambda: {"workers_alive": True, "operator_flatten": flatten}
    with TestClient(create_application(runtime, settings, source_factory=source_factory[0])) as c:
        reply = c.get("/api/v1/lab/status", headers={"Authorization": "Bearer " + APP_TOKEN})
    assert reply.json()["operator_flatten"] == flatten


# --- The completion table -------------------------------------------------------------------


def test_completion_history_is_append_only_and_written_by_the_risk_role_only(mx, safety):
    engine, _, _ = mx
    seq = request_flatten(engine.repo)
    safety.tick()
    [row] = completions(engine.repo)
    assert row["outcome"] == "COMPLETED"
    owner = with_role(engine.repo, "lab_owner")
    for statement in ("UPDATE lab.operator_flatten_completions SET outcome='FAILED'",
                      "DELETE FROM lab.operator_flatten_completions",
                      "TRUNCATE lab.operator_flatten_completions"):
        with psycopg.connect(owner) as conn, pytest.raises(
            psycopg.errors.RaiseException, match="append-only"
        ):
            conn.execute(statement)
    insert = sql.SQL("""INSERT INTO lab.operator_flatten_completions(request_seq,runtime_id,
        started_at,finished_at,cancels_attempted,cancels_acknowledged,closes_attempted,
        closes_acknowledged,residual_codes,outcome)
        VALUES({seq},{runtime},now(),now(),0,0,0,0,'{{FIXTURE_CODE}}','PARTIAL')""").format(
        seq=sql.Literal(seq), runtime=sql.Literal(str(uuid4())))
    for role in ("catalyst_app", "catalyst_review", "catalyst_jev", "catalyst_operator"):
        with psycopg.connect(with_role(engine.repo, role)) as conn, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            conn.execute(insert)
    with psycopg.connect(owner) as conn, pytest.raises(
        psycopg.errors.RaiseException, match="FLATTEN_COMPLETION_ROLE_REQUIRED"
    ):
        conn.execute(insert)
    with psycopg.connect(with_role(engine.repo, "catalyst_risk")) as conn, pytest.raises(
        psycopg.errors.RaiseException, match="FLATTEN_ALREADY_COMPLETED"
    ):
        conn.execute(insert)
    assert completions(engine.repo) == [row]
    assert_audited(engine.repo)


def test_populated_schema18_ledger_migrates_ddl_only_and_keeps_its_pending_request():
    with tempfile.TemporaryDirectory(prefix="catalyst-019-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 18)
            populate_schema14(root)  # Halts, reconciliation runs, managed events, receipts.
            with Repository(localdb.connection_url(root, "catalyst_operator")).connect() as conn:
                request_id = conn.execute(
                    "SELECT lab.operator_request_flatten_all('Recorded before migration 019')"
                    " AS id"
                ).fetchone()["id"]
            owner = Repository(localdb.connection_url(root, "lab_owner"))

            def request_rows():
                with owner.connect() as conn:
                    return conn.execute("""SELECT to_jsonb(r) AS row
                        FROM lab.operator_flatten_requests r ORDER BY event_seq""").fetchall()

            requests = request_rows()
            before, audited, held, version = audit_state(root)
            assert version == 18 and before["valid"] and all(n == ok for n, ok in audited.values())
            assert audited["operator_flatten_requests"] == (1, 1) and len(held) == 1
            with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
                conn.execute(MIGRATION.read_text())
                # The code requires the current schema, so 020 (selection rule B2) and 021
                # (selection rule top-K), both DDL-only, are applied too; the checks below then
                # run under the current code.
                conn.execute(MIGRATION.with_name("020_selection_b2.sql").read_text())
                conn.execute(MIGRATION.with_name("021_selection_topk.sql").read_text())
            after, audited_after, held_after, version_after = audit_state(root)
            # DDL only: the same events, head and count; every historical row still verifies.
            assert version_after == 21 and after == before
            # 022 (JEV_MANAGED_RISK_V3), the current schema, appends exactly its two audited
            # policy rows and nothing else; the checks below then run under the current code.
            from tests.test_crypto_size_hold import apply_migration_022

            apply_migration_022(root)
            # 023 (selection rule top-K V2), 024 (public experiment views) and 025 (Jev review
            # policy V2) are DDL-only.
            with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
                conn.execute(MIGRATION.with_name("023_selection_topk_v2.sql").read_text())
                conn.execute(MIGRATION.with_name("024_public_experiment.sql").read_text())
                conn.execute(MIGRATION.with_name("025_jev_review_policy_v2.sql").read_text())
            # 026 (JEV_MANAGED_RISK_V4) appends exactly its three audited rows.
            from tests.test_risk_v4 import V4_AUDIT_EVENTS, apply_migration_026

            apply_migration_026(root)
            # 027 (the trade plan's planned-stop guard) adds no row.
            from tests.test_trade_plan_migration import apply_migration_027

            apply_migration_027(root)
            # 028, 029 (public page views) and 030 (JEV_TOP_K_SELECTION_V3) add no row.
            from tests.test_selection_topk_v3 import apply_migrations_after_027

            apply_migrations_after_027(root)
            after, _, _, version_after = audit_state(root)
            # apply_migrations_after_027 ends with 031's four audited V5 rows (package plugin-c3).
            assert version_after == SCHEMA_VERSION == 31 and after["valid"]
            assert after["event_count"] == before["event_count"] + 2 + len(V4_AUDIT_EVENTS) + 4
            assert {t: v for t, v in audited_after.items() if t in audited} == audited
            assert audited_after["operator_flatten_completions"] == (0, 0)
            assert held_after == held and request_rows() == requests  # The 015 row, exactly.
            with owner.connect() as conn:
                waiting = conn.execute(
                    "SELECT request_id FROM lab.pending_operator_flatten_requests"
                ).fetchall()
            assert [r["request_id"] for r in waiting] == [request_id]
            with Repository(localdb.connection_url(root, "catalyst_operator")).connect() as conn:
                [listed] = conn.execute("SELECT * FROM lab.operator_list_flattens()").fetchall()
            assert listed["pending"] and listed["completion_rows"] == 0
            RiskRepository(localdb.connection_url(root, "catalyst_risk")).check_role()
            with Repository(localdb.connection_url(root, "catalyst_review")).connect() as conn:
                events = conn.execute("SELECT count(*) AS n FROM lab.trade_events").fetchone()
                visible = conn.execute("SELECT count(*) AS n FROM lab.execution_halts").fetchone()
            assert events["n"] == after["event_count"] and visible["n"] == len(held)
            assert verify_events(owner.export_events())["valid"]
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def test_fixture_venue_close_refusal_knob_refuses_only_market_sells(mx):
    """The test double's knob (tests/test_managed_execution.py) refuses closes, nothing else."""
    engine, venue, _ = mx
    venue.reject_market_sells = 1
    sid = opened(mx, "BTC/USD")  # Entry and stop-limit protection are unaffected.
    assert venue.orders_of("sell", "stop_limit") and venue.reject_market_sells == 1
    assert D(venue.inventory["BTC/USD"]) > 0 and state(engine, sid)["state"] == "OPEN"
