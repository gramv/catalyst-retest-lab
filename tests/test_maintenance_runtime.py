"""CRYPTO_MAINTENANCE_V1 around the protection planner, the runtime, exit flags and the bar
source (package maintenance). Fixture evidence only: per-test databases, the fake paper venue,
mock transports; no broker, provider, network or owner-ledger contact."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import exit_flags
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.crypto_execution import (
    CryptoAsset,
    CryptoExecutionError,
    CryptoOrder,
    CryptoProtectionPolicy,
    CryptoSnapshot,
    MutationProposal,
    native_stop_levels,
    plan_crypto_recovery,
)
from catalyst_lab.managed_ops import maintenance_alarms
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.managed_service import STATE_FIELDS, STATUS_FIELDS
from catalyst_lab.scan_sources import AlpacaMarketSource, SourcePolicy
from tests.maintenance_fixtures import (
    admit,
    bodies,
    entry_order,
    fill,
    maintainer,
    open_trade,
    quote,
    stop_orders,
)
from tests.maintenance_fixtures import mt as mt
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import observation, packet
from tests.test_managed_runtime import Research, Source

NOW = datetime(2026, 9, 27, 15, 7, 30, tzinfo=UTC)
ASSET = CryptoAsset("SOL/USD", D("0.0001"), D("0.0001"), D("0.01"))


# --- The protection planner's new parameters (defaults: every other setup, unchanged) ------------

def snapshot(*orders, qty="10", available="0", bid="106", breached=None):
    return CryptoSnapshot("7", NOW, D(qty), D(available), tuple(orders), True, (), (), D(bid),
                          NOW, breached)


def plan(snap, **options):
    return plan_crypto_recovery(
        ASSET, snap, CryptoProtectionPolicy(D(5), D(5), D(2)), now=NOW, operation_key="op",
        stop=D("103"), stop_limit=D("102.99"), target=D("111"), exit_requested=False,
        exit_deadline=NOW + timedelta(hours=20), **options,
    )


def test_a_raised_stop_is_replaced_in_place_only_when_asked_and_otherwise_cancelled():
    stop = CryptoOrder("stop-1", "c1", "PROTECT", D("10"), D("0"), "new", True, D("95"),
                       D("94.99"))
    today = plan(snapshot(stop))
    assert (today.state, today.reason) == ("CANCELING", "TIGHTEN_STOP")
    assert [(p.action, p.method) for p in today.proposals] == [("CRYPTO_CANCEL", "DELETE")]
    patched = plan(snapshot(stop), replace_stop="PATCH")
    assert (patched.state, patched.reason) == ("REPLACING", "REPLACE_STOP")
    [amend] = patched.proposals
    assert (amend.action, amend.method, amend.path, amend.payload, amend.reason) == (
        "CRYPTO_AMEND", "PATCH", "/v2/orders/stop-1",
        {"stop_price": "103", "limit_price": "102.99"}, "REPLACE_STOP")
    pending = CryptoOrder("stop-1", "c1", "PROTECT", D("10"), D("0"), "pending_replace", True,
                          D("95"), D("94.99"))
    waiting = plan(snapshot(pending), replace_stop="PATCH")
    assert waiting.reason == "REPLACE_STOP" and waiting.proposals == ()
    with pytest.raises(CryptoExecutionError, match="INVALID_STOP_REPLACE_MODE"):
        plan(snapshot(stop), replace_stop="BOTH")


def test_a_retained_entry_is_not_cancelled_until_an_exit_or_the_caller_says_so():
    entry = CryptoOrder("entry-1", "e1", "ENTRY", D("20"), D("10"), "partially_filled", True,
                        None, D("100.10"))
    stop = CryptoOrder("stop-1", "c1", "PROTECT", D("10"), D("0"), "new", True, D("103"),
                       D("102.99"))
    today = plan(snapshot(entry, stop))
    assert [p.reason for p in today.proposals] == ["CANCEL_REMAINING_ENTRY"]
    retained = plan(snapshot(entry, stop), retain_entry=True)
    assert (retained.reason, retained.proposals) == ("NATIVE_STOP_LIMIT_PRESENT", ())
    named = plan(snapshot(entry, stop), entry_cancel_reason="PARTIAL_ENTRY_TIMEOUT")
    assert [p.reason for p in named.proposals] == ["PARTIAL_ENTRY_TIMEOUT"]
    at_target = plan(snapshot(entry, stop, bid="111"), retain_entry=True)
    assert at_target.reason == "TARGET_EXIT"  # An exit cancels the rest whatever the caller.
    assert [p.reason for p in at_target.proposals] == ["CANCEL_REMAINING_ENTRY", "TARGET_EXIT"]
    uncovered = snapshot(entry, qty="10", available="10")
    protect = plan(uncovered, retain_entry=True)
    assert [p.action for p in protect.proposals] == ["CRYPTO_PROTECT"]
    first = plan(uncovered, protect_after_entries=True)
    assert (first.reason, [p.action for p in first.proposals]) == (
        "CANCEL_ENTRY_BEFORE_PROTECT", ["CRYPTO_CANCEL"])


def test_only_the_maintenance_amend_may_patch_a_crypto_order():
    path = "/v2/orders/abc-1"
    MutationProposal("CRYPTO_AMEND", "PATCH", path, {"stop_price": "1", "limit_price": "0.99"},
                     "REPLACE_STOP")
    for action, method, payload in (
        ("CRYPTO_CANCEL", "PATCH", {"stop_price": "1"}),
        ("CRYPTO_AMEND", "POST", {"stop_price": "1"}),
        ("CRYPTO_AMEND", "PATCH", {"qty": "1"}),
        ("CRYPTO_AMEND", "PATCH", {}),
    ):
        with pytest.raises(CryptoExecutionError, match="INVALID_CRYPTO_MUTATION"):
            MutationProposal(action, method, path if method != "POST" else "/v2/orders",
                             payload, "X")
    assert native_stop_levels(ASSET, D("103.005")) == (D("103.01"), D("103.00"))
    assert native_stop_levels(ASSET, D("0.01")) == (D("0.01"), D("0.01"))


# --- The PATCH gate at the execution layer --------------------------------------------------------

def test_a_crypto_price_replace_is_authorized_for_a_maintained_stop_only(mt):
    engine, venue, _ = mt
    older = engine.admit(packet(mt, "OLD/USD"))
    engine.observe_trigger(older, observation(mt))
    order = entry_order(mt, "OLD/USD")
    fill(mt, order, order["qty"])
    engine.manage(older, quote(mt, "100.20"))
    [stop] = stop_orders(mt, "OLD/USD")
    with pytest.raises(ValueError, match="OWNED_PROTECTION_REQUIRED"):
        engine._authorize_mutation(older, "AMEND", "PATCH", "/v2/orders/" + stop["id"],
                                   {"stop_price": "96", "limit_price": "95.99"}, reason="X")
    maintained = open_trade(mt, "SOL/USD")
    [own] = stop_orders(mt, "SOL/USD")
    # The desired stop is still 95: a replace to 96 is stale and sends nothing.
    assert engine._authorize_mutation(maintained, "AMEND", "PATCH", "/v2/orders/" + own["id"],
                                      {"stop_price": "96", "limit_price": "95.99"},
                                      reason="X") is None
    assert not any(m == "PATCH" for m, _, _ in venue.calls)


# --- Exit flags: the record phase 6 consumes ------------------------------------------------------

def test_exit_flags_are_append_only_per_side_and_resolved_once(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    lifecycle = engine._load(sid)[1]["lifecycle_id"]
    with engine.store.transaction() as conn:
        with pytest.raises(ValueError, match="EXIT_FLAG_SIDE_INVALID"):
            exit_flags.raise_exit_flag(engine.store, conn, setup_id=sid, lifecycle_id=lifecycle,
                                       side="OWNER", raised_by={}, reasons={}, evidence={},
                                       raised_at=venue.now, reference="x")
        agent, created = exit_flags.raise_exit_flag(
            engine.store, conn, setup_id=sid, lifecycle_id=lifecycle, side="AGENT",
            raised_by={"agent_id": "fixture-agent"}, reasons={"news": "adverse"},
            evidence={}, raised_at=venue.now, reference="agent-1")
        again, repeated = exit_flags.raise_exit_flag(
            engine.store, conn, setup_id=sid, lifecycle_id=lifecycle, side="AGENT",
            raised_by={"agent_id": "fixture-agent"}, reasons={}, evidence={},
            raised_at=venue.now, reference="agent-2")
        assert created and not repeated and again["body"]["flag_id"] == agent["body"]["flag_id"]
        assert [r["body"]["side"] for r in exit_flags.pending_exit_flags(conn)] == ["AGENT"]
        assert exit_flags.pending_exit_flags(conn, side="JEV") == []
        assert exit_flags.pending_exit_flags(conn, lifecycle_id="00000000-0000-0000-0000-"
                                                               "000000000000") == []
        with pytest.raises(ValueError, match="EXIT_FLAG_OUTCOME_INVALID"):
            exit_flags.resolve_exit_flag(engine.store, conn, flag_id=agent["body"]["flag_id"],
                                         outcome="MAYBE", answered_by={}, answer={},
                                         resolved_at=venue.now)
        with pytest.raises(ValueError, match="EXIT_FLAG_MISSING"):
            exit_flags.resolve_exit_flag(engine.store, conn, flag_id="missing",
                                         outcome="EXIT_AGREED", answered_by={}, answer={},
                                         resolved_at=venue.now)
        resolution = exit_flags.resolve_exit_flag(
            engine.store, conn, flag_id=agent["body"]["flag_id"], outcome="EXIT_NOT_AGREED",
            answered_by={"side": "JEV"}, answer={"exit": False}, resolved_at=venue.now)
        assert resolution["body"]["exit_requested"] is None
        assert exit_flags.pending_exit_flags(conn) == []
    assert not engine._load(sid)[1].get("exit_requested")  # Not agreed: the trade stays.


# --- The runtime ---------------------------------------------------------------------------------

def runtime(mt, **options):
    engine, venue, _ = mt
    run = ManagedRuntime(engine, Research(), Source(),
                         AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret"),
                         engineering_runtime_policy(), clock=lambda: venue.now,
                         reviewer_heartbeat=lambda: True, **options)
    return run


def test_bitcoin_is_subscribed_while_a_maintained_setup_is_active_and_feeds_the_window(mt):
    engine, venue, _ = mt
    run = runtime(mt)
    older = engine.admit(packet(mt, "OLD/USD"))
    assert run._desired_symbols("CRYPTO") == {"OLD/USD"}  # No maintained setup: no Bitcoin.
    admit(mt, "SOL/USD")
    assert run._desired_symbols("CRYPTO") == {"OLD/USD", "SOL/USD", "BTC/USD"}
    assert older
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"OLD/USD", "SOL/USD", "BTC/USD"}
    at = venue.now.isoformat()
    run.market_message("CRYPTO", {"T": "q", "S": "BTC/USD", "bp": "60000", "ap": "60010",
                                  "t": at})
    run.market_message("CRYPTO", {"T": "t", "S": "BTC/USD", "p": "60004", "t": at, "i": 7})
    run.market_message("CRYPTO", {"T": "q", "S": "SOL/USD", "bp": "100", "ap": "100.01",
                                  "t": at})
    assert run.benchmark.samples() == 1  # One second of Bitcoin: low, high and last.
    run.market_gap("CRYPTO", "FIXTURE_GAP")
    assert run.benchmark.samples() == 0


class Recorder:
    management_reviews = "ENABLED"

    def __init__(self):
        self.passes = []

    async def run_pass(self, setups, observe, *, benchmark=None, benchmark_ready=False):
        self.passes.append(([s["symbol"] for s in setups], [observe(s) for s in setups],
                            benchmark_ready))
        return []

    def status(self, active):
        return {"pending_exit_flags": 0, "failing_reviews": [], "open_trades": 1}


def test_maintained_trades_go_to_maintenance_and_every_other_position_to_todays_monitor(mt):
    engine, venue, _ = mt
    older = engine.admit(packet(mt, "OLD/USD"))
    engine.observe_trigger(older, observation(mt))
    order = entry_order(mt, "OLD/USD")
    fill(mt, order, order["qty"])
    engine.manage(older, quote(mt, "100.20"))
    maintained = open_trade(mt, "SOL/USD")
    monitored = []

    async def monitor(setup, observed, fresh):
        monitored.append(setup["symbol"])

    recorder = Recorder()
    run = runtime(mt, monitor_tick=monitor, maintenance=recorder)
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"OLD/USD", "SOL/USD", "BTC/USD"}
    at = venue.now.isoformat()
    for symbol in ("OLD/USD", "SOL/USD"):
        run.market_message("CRYPTO", {"T": "q", "S": symbol, "bp": "101", "ap": "101.02",
                                      "t": at})
    asyncio.run(run._position_pass())
    assert monitored == ["OLD/USD"]
    [(symbols, rows, ready)] = recorder.passes
    assert symbols == ["SOL/USD"] and ready is True
    assert rows[0]["bid"] == "101" and rows[0]["quote_received_at"] == venue.now
    assert run.status()["trade_maintenance"]["open_trades"] == 1
    assert maintained


def test_without_maintenance_a_maintained_trade_is_never_reviewed_by_another_version(mt):
    engine, venue, _ = mt
    sid = open_trade(mt, "SOL/USD")
    monitored = []

    async def monitor(setup, observed, fresh):
        monitored.append(setup["symbol"])

    run = runtime(mt, monitor_tick=monitor)
    for _ in range(2):
        asyncio.run(run._position_pass())
    assert monitored == []
    [skipped] = bodies(engine, "POSITION_REVIEW_SKIPPED", sid)
    assert skipped["reason"] == "MAINTENANCE_NOT_CONFIGURED"
    assert run.status()["trade_maintenance"] is None


def test_the_maintenance_setting_must_match_the_runtime_switch(mt):
    kit = maintainer(mt, reviews="DISABLED")
    with pytest.raises(ValueError, match="MANAGEMENT_REVIEWS_SETTING_MISMATCH"):
        runtime(mt, maintenance=kit.maintenance)
    assert runtime(mt, maintenance=kit.maintenance, management_reviews="DISABLED")


def test_the_status_and_watchdog_report_flags_and_failing_reviews_and_fail_closed():
    assert "trade_maintenance" in STATUS_FIELDS
    assert {"maintenance_policy", "partial_entry_policy", "stop_replace",
            "partial_entry_retained_at", "partial_entry_cancel"} <= STATE_FIELDS
    assert maintenance_alarms(None) == []
    assert maintenance_alarms({"pending_exit_flags": 0, "failing_reviews": []}) == []
    assert maintenance_alarms({"pending_exit_flags": 2, "failing_reviews": [{"code": "X"}]}) == [
        "EXIT_FLAG_PENDING", "MAINTENANCE_REVIEW_FAILING"]
    for broken in ({"available": False}, "nope", {"pending_exit_flags": "1"},
                   {"pending_exit_flags": 0, "failing_reviews": None}):
        assert "MAINTENANCE_STATUS_UNAVAILABLE" in maintenance_alarms(broken)


def test_the_factory_wires_maintenance_to_the_position_source(monkeypatch):
    from catalyst_lab.trade_maintenance import TradeMaintenance
    from tests.test_managed_runtime import (
        test_factory_wires_actual_second_jev_role_with_separate_market_source as factory,
    )

    built = {}
    original = ManagedRuntime.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        built["run"] = self

    monkeypatch.setattr(ManagedRuntime, "__init__", capture)
    factory(monkeypatch)
    run = built["run"]
    assert isinstance(run.maintenance, TradeMaintenance)
    assert run.maintenance is run.trade_maintenance
    assert run.maintenance.bars is run.monitor_tick.source  # The read-only position source.
    assert run.maintenance.management_reviews == run.management_reviews


# --- The bar source -------------------------------------------------------------------------------

def test_fifteen_minute_and_hourly_bars_are_get_only_aligned_and_completed():
    seen = []
    now = datetime(2026, 9, 27, 15, 7, 30, tzinfo=UTC)

    def handle(request):
        seen.append(request)
        start = datetime.fromisoformat(request.url.params["start"])
        rows = [{"t": (start + timedelta(minutes=15 * i)).isoformat().replace("+00:00", "Z"),
                 "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3} for i in range(200)]
        return httpx.Response(200, json={"bars": {"SOL/USD": rows}, "next_page_token": None})

    source = AlpacaMarketSource(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
                                SourcePolicy("iex", 3, 50, 10000, 10, 5), lambda: now,
                                transport=httpx.MockTransport(handle))
    bars, issues = source.timeframe_bars("CRYPTO", "SOL/USD", timeframe="15Min", count=96)
    assert issues == ()
    [request] = seen
    assert request.method == "GET" and request.url.path == "/v1beta3/crypto/us/bars"
    assert request.url.params["timeframe"] == "15Min"
    assert datetime.fromisoformat(request.url.params["start"]) == (
        datetime(2026, 9, 27, 15, 0, tzinfo=UTC) - timedelta(minutes=15 * 101))
    assert all(b.end_at - b.start_at == timedelta(minutes=15) for b in bars)
    assert bars[-1].end_at == datetime(2026, 9, 27, 15, 0, tzinfo=UTC)  # 15:00-15:15 is open.
    for bad in ({"market": "US", "timeframe": "15Min"}, {"market": "CRYPTO", "timeframe": "5Min"}):
        with pytest.raises(ValueError, match="UNSUPPORTED_TIMEFRAME_BAR_SOURCE"):
            source.timeframe_bars(bad["market"], "SOL/USD", timeframe=bad["timeframe"], count=4)
    source.close()


def test_one_minute_maintenance_bars_are_get_only_and_completed():
    """CRYPTO_MAINTENANCE_V2 (package answer-rules): the last 60 completed 1-minute bars through
    the same read-only route; the open minute is never returned."""
    seen = []
    now = datetime(2026, 9, 27, 15, 7, 30, tzinfo=UTC)

    def handle(request):
        seen.append(request)
        start = datetime.fromisoformat(request.url.params["start"])
        rows = [{"t": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
                 "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3} for i in range(80)]
        return httpx.Response(200, json={"bars": {"SOL/USD": rows}, "next_page_token": None})

    source = AlpacaMarketSource(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
                                SourcePolicy("iex", 3, 50, 10000, 10, 5), lambda: now,
                                transport=httpx.MockTransport(handle))
    bars, issues = source.timeframe_bars("CRYPTO", "SOL/USD", timeframe="1Min", count=60)
    assert issues == ()
    [request] = seen
    assert request.method == "GET" and request.url.path == "/v1beta3/crypto/us/bars"
    assert request.url.params["timeframe"] == "1Min"
    assert datetime.fromisoformat(request.url.params["start"]) == (
        datetime(2026, 9, 27, 15, 7, tzinfo=UTC) - timedelta(minutes=65))
    assert all(b.end_at - b.start_at == timedelta(minutes=1) for b in bars)
    assert bars[-1].end_at == datetime(2026, 9, 27, 15, 7, tzinfo=UTC)  # 15:07-15:08 is open.
    source.close()


def test_one_minute_position_bars_are_requested_exactly_as_before():
    seen = []
    now = datetime(2026, 9, 27, 15, 7, 30, tzinfo=UTC)

    def handle(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"bars": {"SOL/USD": []}, "next_page_token": None})

    source = AlpacaMarketSource(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
                                SourcePolicy("iex", 3, 50, 10000, 10, 5), lambda: now,
                                transport=httpx.MockTransport(handle))
    source.completed_bars("CRYPTO", "SOL/USD", SimpleNamespace(bar_seconds=60, lookback_bars=60))
    assert seen == [{"symbols": "SOL/USD", "timeframe": "1Min",
                     "start": (datetime(2026, 9, 27, 15, 7, tzinfo=UTC)
                               - timedelta(minutes=65)).isoformat(),
                     "end": now.isoformat(), "limit": "10000", "sort": "asc"}]
    source.close()


def test_the_maintained_setups_record_nothing_new_for_other_setups(mt):
    engine, venue, _ = mt
    older = engine.admit(packet(mt, "OLD/USD"))
    engine.observe_trigger(older, observation(mt))
    order = entry_order(mt, "OLD/USD")
    fill(mt, order, order["qty"])
    for _ in range(3):
        engine.manage(older, quote(mt, "100.20"))
    for kind in ("MAINTENANCE_OPENED", "MAINTENANCE_ENTRY_COMPLETED", "PARTIAL_ENTRY_RETAINED",
                 "STOP_REPLACED", "MAINTENANCE_TRIGGER"):
        assert not bodies(engine, kind, older)
    assert set(engine._load(older)[1]) & {"maintenance_policy", "partial_entry_policy",
                                          "stop_replace"} == set()
    assert cm.active(engine._load(older)[1]) is False


def test_the_runtime_reviews_through_maintenance_and_its_protection_tick_replaces_the_stop(mt):
    from datetime import timedelta as delta

    engine, venue, _ = mt
    sid = open_trade(mt, "SOL/USD")
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    run = runtime(mt, maintenance=kit.maintenance)
    run.connected = run.research_healthy = True
    assert run.reconcile_once()
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"SOL/USD", "BTC/USD"}
    venue.now += delta(seconds=1)
    at = venue.now.isoformat()
    run.market_message("CRYPTO", {"T": "q", "S": "SOL/USD", "bp": "106", "ap": "106.03", "t": at})
    run.market_message("CRYPTO", {"T": "t", "S": "SOL/USD", "p": "106", "t": at, "i": 5})
    asyncio.run(run._position_pass())  # The research loop's position pass: +1R reviewed.
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["stop"]["new"]) == ("APPLIED", "103.00")
    [old] = stop_orders(mt, "SOL/USD")
    run.execution_once()  # The protection tick replaces the stop-limit (one claimed PATCH).
    assert old["status"] == "replaced"
    [new] = stop_orders(mt, "SOL/USD")
    assert new["stop_price"] == "103.00"
    run.execution_once()
    [replaced] = bodies(engine, "STOP_REPLACED", sid)
    assert replaced["path"] == "PATCH_REPLACE" and run.error is None


@pytest.mark.parametrize("listed_as", ["new", "pending_replace"])
def test_a_replace_still_in_flight_never_cancels_the_new_stop(mt, listed_as):
    """A replace still in flight keeps the new stop-limit (regression guard for the flow).

    Live, on 2026-09-28 at 15:31 UTC (UNI), the app's snapshot listed the old stop-limit as open
    beside its replacement, and the planner cancelled both: the old one's cancel was refused, the
    new one's went through. This fixture flow does not reach that snapshot; the planner-level
    tests in test_crypto_execution reproduce it (they fail without the ``replaces`` rule)."""
    from datetime import timedelta as delta

    engine, venue, _ = mt
    venue.slow_replace = listed_as
    sid = open_trade(mt, "SOL/USD")
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    run = runtime(mt, maintenance=kit.maintenance)
    run.connected = run.research_healthy = True
    assert run.reconcile_once()
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"SOL/USD", "BTC/USD"}
    venue.now += delta(seconds=1)
    at = venue.now.isoformat()
    run.market_message("CRYPTO", {"T": "q", "S": "SOL/USD", "bp": "106", "ap": "106.03", "t": at})
    run.market_message("CRYPTO", {"T": "t", "S": "SOL/USD", "p": "106", "t": at, "i": 5})
    asyncio.run(run._position_pass())
    [old] = stop_orders(mt, "SOL/USD")
    run.execution_once()  # The PATCH: the old order is still listed.
    assert old["status"] == listed_as
    new = venue.orders[old["replaced_by"]]
    for _ in range(3):
        assert run.reconcile_once()
        run.execution_once()  # Ticks while both are listed.
    assert new["status"] == "new" and new["stop_price"] == "103.00"
    assert not any(method == "DELETE" for method, _, _ in venue.calls)
    old["status"] = "replaced"  # The broker completes the replace.
    run.execution_once()
    assert [o["id"] for o in stop_orders(mt, "SOL/USD")] == [new["id"]]
    [replaced] = bodies(engine, "STOP_REPLACED", sid)
    assert replaced["path"] == "PATCH_REPLACE" and run.error is None
