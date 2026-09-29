"""Protection failure isolation (plan package 3.1): disposable PostgreSQL, fake venue only.

One setup's fault, an account-data problem, a rejected amendment or a refused
authorization must never stop another setup's protection, exit an open position, latch
the runtime or reach the broker late. Fixtures only: no real broker, provider or network.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_latches import PROTECTION
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.market import NY
from tests.clock import ny_midnight
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import ManagedVenue, observation, packet
from tests.test_managed_hardening import accept_amendment
from tests.test_managed_runtime import runtime as fake_runtime
from tests.test_position_monitor import bars, monitor, opened

JNLC = {"id": "fixture-journal", "activity_type": "JNLC", "net_amount": "25", "date": "fixture"}


class IsolationVenue(ManagedVenue):
    """ManagedVenue plus rejected price amendments and configurable capital activities."""

    def __init__(self):
        super().__init__()
        self.reject_patch = False
        self.activities = []

    def handle(self, request):
        if request.url.host == "paper-api.alpaca.markets":
            if request.method == "PATCH" and self.reject_patch:
                self.calls.append((request.method, request.url.path, request.content))
                return httpx.Response(422, json={"message": "fixture amendment rejection"})
            if request.method == "GET" and request.url.path == "/v2/account/activities":
                self.calls.append((request.method, request.url.path, request.content))
                return httpx.Response(200, json=self.activities)
        return super().handle(request)


@pytest.fixture
def fx(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = IsolationVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(
        risk, broker, policy=engineering_execution_policy(), clock=lambda: venue.now,
        review_store=reviews,
    )
    assert engine.reconcile()["clean"]
    yield engine, venue, reviews
    broker.close()


def bodies(engine, kind, sid=None):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            "SELECT setup_id,body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
            (kind,),
        ).fetchall()
    return [row["body"] for row in rows if sid is None or row["setup_id"] == sid]


def event_count(engine):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM lab.managed_events").fetchone()["n"]


def mutations(venue):
    return [(method, path) for method, path, _ in venue.calls if method != "GET"]


def live_runtime(fx, symbols):
    engine, venue, _ = fx
    run = ManagedRuntime(
        engine,
        SimpleNamespace(),
        SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        engine.broker.credentials,
        engineering_runtime_policy(),
        clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True,
    )
    run._selected_packets = lambda: []
    run.connected = run.research_healthy = True
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = set(symbols)
    assert run.reconcile_once()
    return run


def record_manage(engine, monkeypatch):
    managed, original = [], engine.manage

    def manage(setup_id, observation=None):
        managed.append(setup_id)
        return original(setup_id, observation)

    monkeypatch.setattr(engine, "manage", manage)
    return managed


def print_trade(run, venue, symbol, trade_id):
    stamp = venue.now.isoformat()
    run.market_message("CRYPTO", {"T": "q", "S": symbol, "bp": "99.99", "ap": "100.01",
                                  "t": stamp})
    run.market_message("CRYPTO", {"T": "t", "S": symbol, "p": "100", "i": trade_id, "t": stamp})


# --- Trigger and market-gap isolation --------------------------------------------------


def test_one_setups_trigger_exception_leaves_the_others_triggered_and_managed(fx, monkeypatch):
    engine, venue, _ = fx
    held = opened(fx, "SOL/USD")
    broken = engine.admit(packet(fx, "BTC/USD"))
    healthy = engine.admit(packet(fx, "ETH/USD"))
    run = live_runtime(fx, {"BTC/USD", "ETH/USD", "SOL/USD"})
    original = engine.observe_trigger

    def observe_trigger(setup_id, observed):
        if setup_id == broken:
            raise RuntimeError("fixture evaluation fault with free text")
        return original(setup_id, observed)

    monkeypatch.setattr(engine, "observe_trigger", observe_trigger)
    managed = record_manage(engine, monkeypatch)
    print_trade(run, venue, "BTC/USD", 1)
    print_trade(run, venue, "ETH/USD", 2)
    run.execution_once()

    [failure] = bodies(engine, "RUNTIME_TRIGGER_FAILURE", broken)
    assert failure["setup_id"] == str(broken) and failure["code"] == "RuntimeError"
    assert bodies(engine, "MARKET_PRINT_CONSUMED", broken) == [
        {"print_event_seq": failure["print_event_seq"], "reason": "TRIGGER_EVALUATION_FAILED"}
    ]
    state = engine._load(broken)[1]
    assert state["state"] == "INVALIDATED"
    assert state["revocation_reason"] == "TRIGGER_EVALUATION_FAILED"
    assert engine._load(healthy)[1]["state"] == "ORDER_SUBMITTED"  # Its print still ran.
    assert [o["symbol"] for o in venue.orders_of("buy") if o["status"] == "new"] == ["ETH/USD"]
    assert held in managed and healthy in managed  # Protection ran for every active setup.
    assert run._pending_trades() == []
    assert not run.latches.has(PROTECTION) and run.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def test_market_gap_revocation_is_isolated_per_setup_and_retried(fx, monkeypatch):
    engine, venue, _ = fx
    held = opened(fx, "SOL/USD")
    stuck = engine.admit(packet(fx, "BTC/USD"))
    other = engine.admit(packet(fx, "ETH/USD"))
    run = live_runtime(fx, {"BTC/USD", "ETH/USD", "SOL/USD"})
    run.market_gap("CRYPTO", "FIXTURE_CONNECTION_LOST")
    original = engine.revoke

    def revoke(setup_id, reason):
        if setup_id == stuck:
            raise RuntimeError("fixture revocation fault")
        return original(setup_id, reason)

    monkeypatch.setattr(engine, "revoke", revoke)
    managed = record_manage(engine, monkeypatch)
    run.execution_once()
    assert engine._load(other)[1]["state"] == "INVALIDATED"
    assert engine._load(stuck)[1]["state"] == "WATCHING"
    assert "CRYPTO" in run.market_gaps and not run.ready()  # Entries stay blocked.
    assert held in managed  # The protection pass still ran.
    assert run.latches.has(PROTECTION, "TRIGGERS")
    monkeypatch.setattr(engine, "revoke", original)
    run.execution_once()
    assert engine._load(stuck)[1]["state"] == "INVALIDATED"
    assert "CRYPTO" not in run.market_gaps


def test_monitor_failures_carry_code_and_setup_and_are_coalesced():
    run = fake_runtime(monitor_tick=None)
    run.execution.setups[0]["state"]["state"] = "OPEN"
    run.observation = lambda setup: {"bid": "99.99", "ask": "100.01"}

    async def failing(setup, observed, fresh):
        raise ValueError("BARS_UNAVAILABLE_FIXTURE")

    run.monitor_tick = failing
    for _ in range(5):
        asyncio.run(run._position_pass())
    notices = [body for kind, body in run.execution.events
               if kind == "POSITION_REVIEW_UNAVAILABLE"]
    assert notices == [{
        "runtime_id": run.runtime_id, "reason": "MONITOR_TICK_FAILED",
        "setup_id": "setup-1", "code": "BARS_UNAVAILABLE_FIXTURE",
    }]


# --- Account data problems are never exit reasons --------------------------------------


def test_jnlc_journal_blocks_entries_and_amendments_while_protection_continues(fx):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    waiting = engine.admit(packet(fx, "ETH/USD"))
    stop = venue.orders_of("sell", "stop_limit")[0]
    venue.activities = [JNLC]
    for _ in range(3):
        engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    state = engine._load(sid)[1]
    assert state["account_risk"] == "UNEVALUABLE:UNSUPPORTED_CAPITAL_ACTIVITY"
    assert state.get("exit_requested") is None and stop["status"] == "new"
    day = venue.now.astimezone(NY).date().isoformat()
    assert bodies(engine, "ACCOUNT_RISK_UNEVALUABLE") == [{
        "code": "UNSUPPORTED_CAPITAL_ACTIVITY", "session_date": day,
        "blocks": ["ENTRY", "MODEL_AMENDMENT"], "mechanical_protection": "CONTINUES",
    }]  # One event per code and New York day, not one per tick.
    # Entries: the strict account snapshot still refuses the entry authorization.
    with pytest.raises(ValueError, match="UNSUPPORTED_CAPITAL_ACTIVITY"):
        engine.observe_trigger(waiting, observation(fx))
    assert not [o for o in venue.orders_of("buy") if o["symbol"] == "ETH/USD"]
    # Model amendments: a genuine review is recorded but never authorizes a plan.
    watcher, _ = monitor(fx, "TIGHTEN_STOP")
    observed = observation(fx, bid="108", ask="108.01")
    result = asyncio.run(
        watcher.review(sid, observed, bars(fx), fresh_observation=lambda: observed)
    )
    assert result.status == "RECORDED" and bodies(engine, "MANAGED_JEV_JUDGMENT", sid)
    assert not bodies(engine, "MANAGEMENT_PLAN_AUTHORIZED", sid)
    assert D(engine._load(sid)[1]["stop"]) == D(95)
    # Mechanical protection: the local target still exits the position.
    touched = observation(fx, bid="111", ask="111.01")
    assert engine.manage(sid, touched).reason == "TARGET_EXIT"
    assert stop["status"] == "canceled"
    engine.manage(sid, touched)
    assert venue.orders_of("sell", "market")
    assert engine._load(sid)[1]["exit_requested"] == "TARGET_EXIT"
    # Evidence recovers: the next evaluable tick clears the account-risk flag.
    venue.activities = []
    engine.manage(sid, touched)
    assert engine._load(sid)[1]["account_risk"] is None


def test_unevaluable_account_risk_defers_an_accepted_plan_until_it_expires(fx):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    accept_amendment(fx, sid, "TIGHTEN_STOP")
    stop = venue.orders_of("sell", "stop_limit")[0]
    venue.activities = [JNLC]
    plan = engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    assert plan.state == "PROTECTED" and stop["status"] == "new"
    assert not bodies(engine, "MANAGEMENT_STARTED", sid)
    venue.now += timedelta(seconds=11)
    engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    assert bodies(engine, "MANAGEMENT_EXPIRED", sid)[-1]["reason"] == "REVIEW_EXPIRED"
    state = engine._load(sid)[1]
    assert D(state["stop"]) == D(95) and state["amendment_expires_at"] is None
    assert len(venue.orders_of("sell", "stop_limit")) == 1


def test_a_deferred_plan_never_blocks_re_protection_at_the_level_in_force(fx):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    accept_amendment(fx, sid, "TIGHTEN_STOP")  # Desired 102, in force 95 until it starts.
    venue.activities = [JNLC]
    venue.orders_of("sell", "stop_limit")[0]["status"] = "canceled"  # Protection lost.
    plan = engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    assert plan.state == "PROTECTION_REQUIRED"
    stops = [o for o in venue.orders_of("sell", "stop_limit") if o["status"] == "new"]
    assert [D(o["stop_price"]) for o in stops] == [D(95)]
    assert not bodies(engine, "MANAGEMENT_STARTED", sid)
    assert engine.manage(sid, observation(fx, bid="108", ask="108.01")).state == "PROTECTED"


def test_missing_baseline_at_new_york_midnight_never_exits_open_crypto(fx):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    stop = venue.orders_of("sell", "stop_limit")[0]
    tomorrow = venue.now.astimezone(NY).date() + timedelta(days=1)
    venue.now = ny_midnight(tomorrow) + timedelta(seconds=30)
    for _ in range(3):
        plan = engine.manage(sid, observation(fx))
        assert plan.state == "PROTECTED"
    state = engine._load(sid)[1]
    assert state["account_risk"] == "UNEVALUABLE:STARTUP_RECONCILIATION_REQUIRED"
    assert state.get("exit_requested") is None and stop["status"] == "new"
    assert not venue.orders_of("sell", "market")
    [notice] = bodies(engine, "ACCOUNT_RISK_UNEVALUABLE")
    assert notice["session_date"] == tomorrow.isoformat()
    assert engine.reconcile()["clean"]  # Records the new day's baseline.
    engine.manage(sid, observation(fx))
    assert engine._load(sid)[1]["account_risk"] is None
    assert engine._load(sid)[1].get("exit_requested") is None


# --- Amendment rejection, authorization refusal, read-only review --------------------


def test_rejected_amendment_reverts_to_acknowledged_levels_without_exit_or_retry(fx):
    engine, venue, _ = fx
    sid = opened(fx, "SPY")
    observed = accept_amendment(fx, sid, "TIGHTEN_STOP")
    assert D(engine._load(sid)[1]["stop"]) == D(102)
    venue.reject_patch = True
    for _ in range(3):
        engine.manage(sid, observed)
    assert [m for m, _ in mutations(venue)] == ["POST", "PATCH"]  # Entry, one amendment.
    [rejected] = bodies(engine, "AMENDMENT_REJECTED", sid)
    assert rejected["reason"].startswith("BROKER_")  # The transport's classified reason.
    assert rejected["requested"] == {"stop_price": "102"}
    assert rejected["reverted_to"] == {"stop": "95", "target": "111"}
    state = engine._load(sid)[1]
    assert state.get("exit_requested") is None
    assert D(state["stop"]) == D(95) and D(state["target"]) == D(111)
    assert state["amendment_expires_at"] is None and state["amendment_context_hash"] is None
    assert not venue.orders_of("sell", "market")
    assert all(o["status"] in {"held", "new"} for o in venue.orders_of("sell"))


def test_refused_authorization_is_recorded_without_latch_and_retried(fx, monkeypatch):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    stop = venue.orders_of("sell", "stop_limit")[0]
    run = live_runtime(fx, {"BTC/USD"})
    with engine.store.transaction() as conn:
        current = engine.store.state(conn, sid)
        engine.store.transition(conn, sid, current["state"], hard_exit_at=venue.now.isoformat())
    original, raced = engine.broker.mutate, []

    def racing(method, path, payload, decision_id):
        # A review accepted concurrently: the state revision moves before the claim.
        if not raced:
            raced.append(decision_id)
            with engine.store.transaction() as conn:
                latest = engine.store.state(conn, sid)
                engine.store.transition(conn, sid, latest["state"], fixture_concurrent_review=1)
        return original(method, path, payload, decision_id)

    monkeypatch.setattr(engine.broker, "mutate", racing)
    run.execution_once()
    [refused] = bodies(engine, "AUTHORIZATION_NOT_CLAIMED", sid)
    assert refused == {"decision_id": str(raced[0]), "action": "CANCEL",
                       "code": "STALE_POSITION_REVISION"}
    assert run.error is None and not run.latches.blocking()
    assert stop["status"] == "new"
    run.execution_once()  # A fresh decision for the same cancel.
    assert stop["status"] == "canceled"
    run.execution_once()
    assert venue.orders_of("sell", "market")
    assert run.error is None and not run.latches.blocking()
    assert verify_events(engine.repo.export_events())["valid"]


def test_refused_entry_authorization_is_final_and_releases_its_reservation(fx, monkeypatch):
    engine, venue, _ = fx
    sid = engine.admit(packet(fx, "BTC/USD"))
    original = engine.broker.mutate

    def racing(method, path, payload, decision_id):
        with engine.store.transaction() as conn:
            latest = engine.store.state(conn, sid)
            engine.store.transition(conn, sid, latest["state"], fixture_concurrent_write=1)
        return original(method, path, payload, decision_id)

    monkeypatch.setattr(engine.broker, "mutate", racing)
    decision = engine.observe_trigger(sid, observation(fx))  # Never raises.
    assert decision["outcome"] == "APPROVED"
    state = engine._load(sid)[1]
    assert state["state"] == "RISK_REJECTED" and state["reason"] == "STALE_POSITION_REVISION"
    assert not venue.orders_of("buy")
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()
        assert not conn.execute("SELECT 1 FROM lab.managed_claims").fetchone()


def test_review_snapshot_never_dispatches_acknowledges_or_writes(fx):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    setup, state = engine._load(sid)
    position = next(p for p in venue._position_rows() if p["symbol"] == "BTC/USD")
    payload = {
        "symbol": "BTC/USD", "qty": position["qty"], "side": "sell", "type": "market",
        "time_in_force": "gtc", "client_order_id": "cl-fixture-" + uuid4().hex,
    }
    with engine.store.transaction() as conn:  # A crash between decision and dispatch.
        unsent = engine._decision(
            conn, setup, state, "EXIT", payload, {"reason": "FIXTURE_CRASH"},
            equity=D(0), reason="FIXTURE_CRASH_BEFORE_DISPATCH",
        )
    watcher, _ = monitor(fx)
    before, calls = event_count(engine), len(venue.calls)
    for _ in range(3):
        try:
            watcher.snapshot(sid, observation(fx, bid="108", ask="108.01"))
        except ValueError as exc:
            assert str(exc) == "POSITION_NOT_READY_FOR_REVIEW"
    assert event_count(engine) == before  # No acknowledgement, link, expiry or ingest.
    assert all(method == "GET" for method, _, _ in venue.calls[calls:])
    assert not venue.orders_of("sell", "market")
    with engine.repo.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM lab.managed_claims WHERE decision_id=%s", (unsent["decision_id"],)
        ).fetchone()


# --- Amendment expiry and recovery of never-sent changes -------------------------------


@pytest.mark.parametrize(
    "action,kept,reverted,patches",
    [
        ("EXTEND_TARGET", {"stop": "95"}, {"target": "111"}, 0),
        ("TIGHTEN_AND_EXTEND", {"stop": "102"}, {"target": "111"}, 1),
    ],
)
def test_started_amendment_not_dispatched_by_its_deadline_expires_and_reverts(
    fx, action, kept, reverted, patches
):
    engine, venue, _ = fx
    sid = opened(fx, "SPY")
    accept_amendment(fx, sid, action)
    # Ask at or above the new target: the extension waits (it would fill at once).
    wide = observation(fx, bid="108", ask="116")
    engine.manage(sid, wide)
    assert len(bodies(engine, "MANAGEMENT_STARTED", sid)) == 1
    assert [m for m, _ in mutations(venue)].count("PATCH") == patches
    venue.now += timedelta(seconds=11)
    engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    [expired] = bodies(engine, "MANAGEMENT_EXPIRED", sid)
    assert expired["reason"] == "NOT_DISPATCHED" and expired["reverted_to"] == reverted
    state = engine._load(sid)[1]
    assert {k: str(D(state[k])) for k in kept} == kept
    assert {k: str(D(state[k])) for k in reverted} == reverted
    assert state["amendment_expires_at"] is None
    engine.manage(sid, wide)  # The stale extension never reaches the broker later.
    assert [m for m, _ in mutations(venue)].count("PATCH") == patches
    assert not venue.orders_of("sell", "market")


def stock_stop_leg(venue):
    return next(o for o in venue.orders_of("sell", "stop") if o["status"] == "held")


def desire_stop(engine, sid, stop):
    with engine.store.transaction() as conn:
        current = engine.store.state(conn, sid)
        return engine.store.transition(conn, sid, current["state"], stop=stop)


def test_never_sent_patch_is_expired_so_the_same_change_can_be_sent(fx):
    engine, venue, _ = fx
    sid = opened(fx, "SPY")
    leg = stock_stop_leg(venue)
    state = desire_stop(engine, sid, "96")
    setup = engine._load(sid)[0]
    with engine.store.transaction() as conn:  # A crash between decision and dispatch.
        unsent = engine._decision(
            conn, setup, state, "AMEND", {"stop_price": "96"}, {"reason": "FIXTURE"},
            equity=D(0), method="PATCH", path="/v2/orders/" + leg["id"],
            reason="JEV_BOUNDED_AMENDMENT",
        )
    venue.now = max(venue.now, unsent["expires_at"] + timedelta(seconds=1))
    engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    assert bodies(engine, "UNSENT_AUTHORIZATION_EXPIRED", sid) == [
        {"decision_id": str(unsent["decision_id"])}
    ]
    assert [m for m, _ in mutations(venue)] == ["POST", "PATCH"]  # Sent once, freshly.
    assert leg["status"] == "replaced"
    engine.manage(sid, observation(fx, bid="108", ask="108.01"))
    assert [m for m, _ in mutations(venue)] == ["POST", "PATCH"]
    assert len(bodies(engine, "UNSENT_AUTHORIZATION_EXPIRED", sid)) == 1


def test_claimed_but_unacknowledged_patch_is_recovered_never_resent(fx):
    engine, venue, _ = fx
    sid = opened(fx, "SPY")
    leg = stock_stop_leg(venue)
    state = desire_stop(engine, sid, "96")
    setup = engine._load(sid)[0]
    with engine.store.transaction() as conn:
        claimed = engine._decision(
            conn, setup, state, "AMEND", {"stop_price": "96"}, {"reason": "FIXTURE"},
            equity=D(0), method="PATCH", path="/v2/orders/" + leg["id"],
            reason="JEV_BOUNDED_AMENDMENT",
        )
    # Claimed and sent, then the process died before recording the acknowledgement.
    engine.broker.mutate("PATCH", claimed["path"], claimed["payload"], claimed["decision_id"])
    for _ in range(2):
        assert engine.manage(sid, observation(fx, bid="108", ask="108.01")) == "PROTECTED"
    assert [m for m, _ in mutations(venue)] == ["POST", "PATCH"]
    with engine.repo.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
            ("ack:" + str(claimed["decision_id"]),),
        ).fetchone()
    assert not bodies(engine, "UNSENT_AUTHORIZATION_EXPIRED", sid)
    live = [o for o in venue.orders_of("sell", "stop") if o["status"] not in {"replaced"}]
    assert [o["stop_price"] for o in live] == ["96"]


# --- Coalescing -------------------------------------------------------------------------


def test_six_hundred_unchanged_ticks_write_one_protection_plan(fx):
    engine, venue, _ = fx
    sid = opened(fx, "BTC/USD")
    assert [b["state"] for b in bodies(engine, "PROTECTION_PLAN", sid)] == [
        "PROTECTION_REQUIRED"
    ]
    assert engine.manage(sid, observation(fx)).state == "PROTECTED"
    before = event_count(engine)
    for _ in range(599):
        assert engine.manage(sid, observation(fx)).state == "PROTECTED"
    assert event_count(engine) == before  # Unchanged ticks append nothing at all.
    assert [b["state"] for b in bodies(engine, "PROTECTION_PLAN", sid)] == [
        "PROTECTION_REQUIRED", "PROTECTED"
    ]
    engine.manage(sid, observation(fx, bid="111", ask="111.01"))  # A changed plan is new.
    assert [b["reason"] for b in bodies(engine, "PROTECTION_PLAN", sid)][-1] == "TARGET_EXIT"
