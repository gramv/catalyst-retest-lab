"""STRATEGY_REGISTRY_V1 and the decision-core seed (package strategy-c1, 2026-10-03).

Fixture evidence only: pure functions on hand-built bars, per-test disposable PostgreSQL
databases, a mock Jev transport and the fixture paper venue. No broker, provider, network or
owner-ledger contact.
"""

import asyncio
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

import tests.test_research_report_v3 as report_tests
from catalyst_lab import coinbase_trigger, crypto_trigger, pick_outcomes, strategies, system_check
from catalyst_lab import trade_plan as tp
from catalyst_lab.crypto_maintenance import HOURLY_RANGE_BARS, HOURLY_RANGE_MIN_BARS
from catalyst_lab.managed_execution import crypto_trigger_rules
from catalyst_lab.pick_outcomes import Bar
from catalyst_lab.research_report_v3 import parse_report_v3
from catalyst_lab.result_dimensions import dimensions
from catalyst_lab.strategies import core
from catalyst_lab.strategies.base import SHADOW, Strategy
from catalyst_lab.system_check import LiveQuote
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_research_report_v3 import (
    SCHEDULE,
    cycle_of,
    events,
    lab,  # noqa: F401 -- fixture
    pick,
    report_v3,
    v3_intake,
    v3_on_venue,
)
from tests.test_selection_b1 import classify

H = timedelta(hours=1)
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def bar(start, o, h, low, c, v="10"):
    return Bar(start=start, open=D(str(o)), high=D(str(h)), low=D(str(low)), close=D(str(c)),
               volume=D(str(v)))


def flat_hours(start, count, price="100", volume="10"):
    return [bar(start + n * H, price, price, price, price, volume) for n in range(count)]


# --- The registry and the interface ------------------------------------------------------------


def test_the_registry_holds_the_two_named_strategies():
    assert sorted(strategies.REGISTRY) == ["BREAKOUT_7D_VOL2X_V1", "PULLBACK_V1"]
    assert strategies.REGISTRY_VERSION == "STRATEGY_REGISTRY_V1"
    assert strategies.DEFAULT_STRATEGY_ID == "PULLBACK_V1"
    pullback, breakout = strategies.get("PULLBACK_V1"), strategies.get("BREAKOUT_7D_VOL2X_V1")
    assert pullback.live and pullback.accepts_reports()
    assert not breakout.live and not breakout.accepts_reports() and breakout.stage == SHADOW
    assert strategies.mechanical() == [breakout]
    assert strategies.mechanical(strategies.LIVE_PAPER) == []
    for unknown in ("NOPE_V1", "pullback_v1", None, 7):
        with pytest.raises(ValueError, match="^STRATEGY_NOT_REGISTERED$"):
            strategies.get(unknown)
    with pytest.raises(ValueError, match="^DUPLICATE_STRATEGY_ID$"):
        strategies._registry(pullback, pullback)
    assert [r["strategy_id"] for r in strategies.records()] == sorted(strategies.REGISTRY)


@pytest.mark.parametrize("strategy", list(strategies.REGISTRY.values()),
                         ids=list(strategies.REGISTRY))
def test_every_registered_strategy_conforms_to_the_interface(strategy):
    record = strategy.record()
    assert record["strategy_id"] == f"{strategy.name}_V{strategy.version}"
    assert set(record["entry_types"]) <= core.ENTRY_TYPES and record["entry_types"]
    assert strategy.plan_rules == tp.TRADE_PLAN_VERSION
    assert strategy.trigger_rule and strategy.description
    assert strategy.admission_fields() == {"strategy_id": strategy.strategy_id,
                                           "strategy_version": strategy.version}
    if strategy.live:
        assert strategy.live_trigger is not None
    if strategies.MECHANICAL in strategy.sources:
        proposals = strategy.signals(breakout_series(), {
            "symbol": "AAA/USD", "since": T0, "until": T0 + 400 * H})
        assert proposals and all(p["strategy_id"] == strategy.strategy_id for p in proposals)
        assert set(proposals[0]) >= {"strategy_id", "symbol", "signal_at", "entry_type",
                                     "reference_price", "hourly_range", "facts"}
        assert proposals[0]["entry_type"] in strategy.entry_types


def test_a_strategy_record_refuses_an_incomplete_or_inconsistent_declaration():
    base = strategies.PULLBACK_V1
    for changes, code in (
        ({"strategy_id": "PULLBACK_V2"}, "STRATEGY_ID_MUST_BE_NAME_V_VERSION"),
        ({"strategy_id": "PULLBACK"}, "STRATEGY_ID_MUST_BE_NAME_V_VERSION"),
        ({"entry_types": frozenset({"SHORT"})}, "STRATEGY_ENTRY_TYPES_INVALID"),
        ({"plan_rules": "MY_PLAN_V1"}, "STRATEGY_PLAN_RULES_UNKNOWN"),
        ({"stage": "LIVE_MONEY"}, "STRATEGY_STAGE_INVALID"),
        ({"sources": frozenset()}, "STRATEGY_SOURCES_INVALID"),
        ({"live_trigger": None}, "LIVE_STRATEGY_NEEDS_LIVE_TRIGGER"),
        ({"sources": frozenset({"MECHANICAL"})}, "MECHANICAL_STRATEGY_NEEDS_SIGNALS_AND_SIMULATE"),
    ):
        with pytest.raises(ValueError, match=f"^{code}$"):
            replace(base, **changes)
    assert isinstance(replace(base), Strategy)


# --- PULLBACK_V1 is today's behaviour --------------------------------------------------------


def test_pullback_dispatches_exactly_as_before():
    def before(state):  # managed_execution.crypto_trigger_rules before the registry.
        if coinbase_trigger.active(state):
            return coinbase_trigger
        if crypto_trigger.active(state):
            return crypto_trigger
        return None

    for state in ({}, {"trigger_version": "CRYPTO_ALPACA_TRIGGER_V1"},
                  {"trigger_version": "CRYPTO_COINBASE_TRIGGER_V1"},
                  {"trigger_version": "CRYPTO_ALPACA_TRIGGER_V1", "strategy_id": "PULLBACK_V1"},
                  {"state": "WATCHING", "stop": "95"}):
        assert crypto_trigger_rules(state) is before(state)
    # A state naming a strategy that is not live, or none registered, is refused (fail-closed).
    with pytest.raises(ValueError, match="^STRATEGY_NOT_LIVE$"):
        crypto_trigger_rules({"strategy_id": "BREAKOUT_7D_VOL2X_V1"})
    with pytest.raises(ValueError, match="^STRATEGY_NOT_REGISTERED$"):
        crypto_trigger_rules({"strategy_id": "GONE_V1"})


def test_pullback_entry_types_and_admission_fields():
    assert system_check.TRADED_ENTRY_TYPES == frozenset({"PULLBACK", "IMMEDIATE"})
    v3 = {"market": "CRYPTO", "report_schema_version": "AGENT_RESEARCH_REPORT_V3"}
    assert strategies.traded_entry_types(v3) == system_check.TRADED_ENTRY_TYPES
    assert strategies.admission_fields(v3) == {"strategy_id": "PULLBACK_V1",
                                               "strategy_version": 1}
    assert strategies.admission_fields({**v3, "strategy_id": "PULLBACK_V1"}) == (
        strategies.admission_fields(v3))
    for other in ({"market": "CRYPTO"}, {**v3, "market": "US_STOCKS"}, {}):
        assert strategies.admission_fields(other) == {}  # Every other setup: unchanged.
    with pytest.raises(ValueError, match="^STRATEGY_NOT_LIVE$"):
        strategies.admission_fields({**v3, "strategy_id": "BREAKOUT_7D_VOL2X_V1"})
    assert strategies.strategy_id_of({}, {"strategy_id": "X_V1"}) == "X_V1"
    assert strategies.strategy_id_of(None, {}) == "PULLBACK_V1"


def test_the_trade_plan_math_is_the_core_and_unchanged():
    rng = random.Random(20261003)
    for _ in range(300):
        t = D(rng.randint(100, 100000)) / 100
        m = t + D(rng.randint(0, 50)) / 100
        s = t - t * D(rng.randint(21, 200)) / 1000
        p = m + D(rng.randint(1, 5000)) / 100
        hr = t * D(rng.randint(0, 60)) / 1000
        levels = {"entry_trigger": str(t), "max_entry_price": str(m), "stop": str(s),
                  "target": str(p)}
        try:
            got = tp.plan(levels, hourly_range=hr, range_evidence={}, increment="0.01")
        except tp.PlanRefused as exc:
            got = exc.code
        floor = t - 2 * hr  # The formula before the core (exact at these sizes).
        stop = (min(s, floor) // D("0.01")) * D("0.01")
        if stop <= 0 or m - stop < D("0.02") * m:
            assert got in (tp.STOP_NOT_POSITIVE, tp.STOP_DISTANCE_BELOW_MINIMUM)
            continue
        cap = t + D("1.5") * (t - stop)
        target = (min(p, cap) // D("0.01")) * D("0.01")
        if target <= m:
            assert got == tp.TARGET_NOT_ABOVE_MAX_ENTRY
            continue
        assert (D(got["stop"]), D(got["target"]), D(got["range_floor"])) == (stop, target, floor)
        assert got["stop_basis"] == ("RESEARCH_STOP" if s <= floor else "HOURLY_RANGE_FLOOR")
    assert (core.HOURLY_RANGE_HOURS, core.HOURLY_RANGE_MIN_BARS) == (
        HOURLY_RANGE_BARS, HOURLY_RANGE_MIN_BARS)
    assert (core.STOP_RANGE_MULTIPLE, core.TARGET_R_MULTIPLE, core.MIN_STOP_FRACTION) == (
        tp.STOP_RANGE_MULTIPLE, tp.TARGET_R_MULTIPLE, tp.MIN_STOP_FRACTION)


def test_the_shadow_simulations_and_the_live_trigger_share_the_core():
    assert pick_outcomes.ShadowDataError is core.ShadowDataError
    assert pick_outcomes.ExitWalk is core.ExitWalk
    assert (pick_outcomes.STOP, pick_outcomes.TARGET, pick_outcomes.HOLD_EXIT,
            pick_outcomes.DATA_INCOMPLETE) == (core.STOP, core.TARGET, core.HOLD_EXIT,
                                               core.DATA_INCOMPLETE)
    minute = timedelta(minutes=1)
    bars = [bar(T0, 100, 100, 99, 99.5), bar(T0 + minute, 99.5, 112, 99.5, 111)]
    walk = pick_outcomes.walk_to_exit(D(95), D(111), bars, hold_deadline=T0 + 24 * H)
    assert walk == core.walk_to_exit(D(95), D(111), bars, hold_deadline=T0 + 24 * H)
    assert (walk.reason, walk.price) == ("TARGET", D(111))
    assert pick_outcomes.r_values(D(100), D(95), D(110)) == core.r_values(
        D(100), D(95), D(110), fee_rate=D("0.0025"))
    # PULLBACK_V1's touch: at or below the level, the stop checked first.
    assert core.touches_entry(D(100), D(100)) and not core.touches_entry(D("100.01"), D(100))
    assert core.reaches_stop(D(95), D(95)) and not core.reaches_stop(D("95.01"), D(95))
    assert core.pullback_bar_event(bar(T0, 100, 100, 95, 96), D(100), D(95)) == core.STOP_FIRST
    assert core.pullback_bar_event(bar(T0, 101, 101, 100, 100.5), D(100), D(95)) == core.TRIGGER
    assert core.pullback_bar_event(bar(T0, 101, 101, 100.01, 101), D(100), D(95)) is None
    simulated = pick_outcomes.simulate_pick(
        {"entry_trigger": "100", "max_entry_price": "100.1", "stop": "95", "target": "111"},
        [bar(T0, 101, 101, 100, 100.5)] + [bar(T0 + n * minute, 100.5, 111, 100.5, 110)
                                           for n in range(1, 3)],
        window_start=T0, window_end=T0 + H)
    assert (simulated.outcome, simulated.fill_price) == ("TARGET", D("100.1"))


# --- BREAKOUT_7D_VOL2X_V1's rule on hand-built bars ----------------------------------------------

SIGNAL_INDEX = 300  # Enough history: 216 hours are needed before the first evaluated bar.


def breakout_series(*, volume="250", close="101", high=None, after=10, extra=()):
    """Flat 100 / volume 10 hours, a breakout bar at ``SIGNAL_INDEX`` (open 100), then flat at
    its close. ``extra``: (index, bar fields) replacing later bars."""
    bars = flat_hours(T0, SIGNAL_INDEX)
    start = T0 + SIGNAL_INDEX * H
    hi = high or max(D(close), D(100))
    bars.append(bar(start, 100, hi, min(D(close), D(100)), close, volume))
    bars += flat_hours(start + H, after, price=close)
    for index, fields in extra:
        bars[index] = bar(T0 + index * H, *fields)
    return bars


def signals(bars, since=None, until=None):
    return core.breakout_signals(bars, since=since or T0, until=until or T0 + 1000 * H)


def test_a_close_above_the_7_day_high_on_exactly_twice_the_volume_is_a_signal():
    # The day's 24 bars: 23 x 10 + 250 = 480 = 2 x the 7-day mean (7 x 240 / 7 = 240): passes.
    [found] = signals(breakout_series())
    assert found["signal_at"] == (T0 + (SIGNAL_INDEX + 1) * H).isoformat()
    assert (found["high_7d"], found["close"], found["volume_24h"],
            found["baseline_volume_7d"], found["volume_ratio"]) == (
        "100", "101", "480", "1680", "2.0000")
    # One unit of volume less is below 2x: no signal.
    assert signals(breakout_series(volume="249.99")) == []
    # A close equal to the 7-day high is not above it (even on huge volume).
    assert signals(breakout_series(close="100", high="100", volume="5000")) == []
    # A high above the prior high with a close at it is not a breakout either.
    assert signals(breakout_series(close="100", high="102", volume="5000")) == []
    facts = core.breakout_facts(core._Series(breakout_series(volume="249.99")), SIGNAL_INDEX)
    assert facts["close_above_high"] is True and facts["volume_ok"] is False


def test_only_the_first_breakout_in_24_hours_is_a_signal_and_windows_bound_it():
    # A second, bigger breakout 5 hours later is part of the same move: no second signal.
    later = SIGNAL_INDEX + 5
    bars = breakout_series(after=40, extra=[(later, (101, 103, 101, 103, "900"))]
                           + [(i, (103, 103, 103, 103)) for i in range(later + 1, 341)])
    assert [f["signal_at"] for f in signals(bars)] == [(T0 + (SIGNAL_INDEX + 1) * H).isoformat()]
    # 24 quiet hours later a new breakout is a new signal.
    again = SIGNAL_INDEX + 30
    bars = breakout_series(after=40, extra=[(again, (101, 104, 101, 104, "900"))]
                           + [(i, (104, 104, 104, 104)) for i in range(again + 1, 341)])
    assert len(signals(bars)) == 2
    # Signals are reported by their time in (since, until] only.
    at = T0 + (SIGNAL_INDEX + 1) * H
    assert signals(breakout_series(), since=at) == []
    assert len(signals(breakout_series(), since=at - H, until=at)) == 1
    assert signals(breakout_series(), until=at - H) == []


def test_too_few_bars_is_never_a_signal():
    bars = breakout_series()
    assert signals(bars[150:]) != []  # 150 bars of the prior 7 days are enough (120+).
    assert signals(bars[200:]) == []  # 100 are not.
    sparse = [b for i, b in enumerate(bars) if i >= SIGNAL_INDEX - 24 or i % 2 == 0]
    assert signals(sparse) == []  # 7-day windows with fewer than 120 bars.
    assert core.breakout_facts(core._Series(bars[200:]), SIGNAL_INDEX - 200)["status"] == (
        "INSUFFICIENT_BARS")


def test_the_breakout_plugin_proposes_and_simulates_on_the_core():
    proposal = strategies.BREAKOUT_7D_VOL2X_V1.signals(
        breakout_series(), {"symbol": "AAA/USD", "since": T0, "until": T0 + 1000 * H})[0]
    signal_at = T0 + (SIGNAL_INDEX + 1) * H
    # Hourly range: 23 flat bars and the breakout bar's 1.00 over 24.
    assert abs(D(proposal["hourly_range"]) - D(1) / 24) < D("1e-25")
    assert proposal["hourly_range_bars"] == 24
    minute = timedelta(minutes=1)
    rising = [bar(signal_at + n * minute, D(101) + n, D(101) + n + D("0.5"), D(101) + n,
                  D(101) + n + D("0.5")) for n in range(5)]
    result = strategies.BREAKOUT_7D_VOL2X_V1.simulate(proposal, rising, fee_rate=D("0.0025"))
    plan = result["plan"]
    # The 2% stop is lower than entry - 2 x range: it stands; target 1.5R from the fill.
    assert (result["fill_price"], plan["stop"], plan["target"]) == (
        D(101), D("98.98"), D("104.03"))
    assert (result["outcome"], result["exit_price"], result["gross_r"]) == (
        "TARGET", D("104.03"), D("1.5"))
    assert result["net_r"] == D("1.5") - D("0.0025") * (D(101) + D("104.03")) / D("2.02")
    # A wide hourly range sets the stop instead (CRYPTO_TRADE_PLAN_V1's floor).
    wide = {**proposal, "hourly_range": "2"}
    plan = strategies.BREAKOUT_7D_VOL2X_V1.simulate(wide, rising, fee_rate=D(0))["plan"]
    assert (plan["stop"], plan["stop_basis"], plan["target"]) == (
        D(97), "HOURLY_RANGE_FLOOR", D(107))
    # No print within 15 minutes: not filled; bars ending early: data incomplete, no R.
    late = [replace(b, start=b.start + timedelta(minutes=15)) for b in rising]
    assert strategies.BREAKOUT_7D_VOL2X_V1.simulate(proposal, late, fee_rate=D(0))[
        "outcome"] == core.NOT_FILLED
    flat = [bar(signal_at + n * minute, 101, 101, 101, 101) for n in range(5)]
    short = strategies.BREAKOUT_7D_VOL2X_V1.simulate(proposal, flat, fee_rate=D(0))
    assert (short["outcome"], short["net_r"]) == (core.DATA_INCOMPLETE, None)


# --- The report contract and admission -----------------------------------------------------------


def test_a_report_may_name_a_registered_live_strategy_and_absent_is_unchanged():
    raw = report_v3([pick(0), pick(1, strategy_id="PULLBACK_V1"),
                     pick(2, strategy_id="NOPE_V1"), pick(3, strategy_id="BREAKOUT_7D_VOL2X_V1"),
                     pick(4, strategy_id="")])
    intake = parse_report_v3(raw, schedule=SCHEDULE)
    plain, named, unknown, shadow, empty = intake.picks
    assert plain.code is None and "strategy_id" not in plain.canonical  # Pre-registry form.
    assert named.code is None and named.canonical["strategy_id"] == "PULLBACK_V1"
    assert (unknown.code, list(unknown.errors)) == ("INVALID_RESEARCH_ITEM", [
        {"path": "picks[2].strategy_id", "code": "STRATEGY_NOT_REGISTERED"}])
    assert (shadow.code, list(shadow.errors)) == ("INVALID_RESEARCH_ITEM", [
        {"path": "picks[3].strategy_id", "code": "STRATEGY_NOT_OPEN_TO_REPORTS"}])
    assert empty.code == "INVALID_RESEARCH_ITEM"
    # The canonical report (and so its hash) of a pick without the field is the pre-registry one.
    assert "strategy_id" not in intake.canonical["picks"][0]


def test_intake_keeps_the_declared_strategy_on_the_packet_never_in_the_review(lab):  # noqa: F811
    raw = report_v3([pick(0), pick(1, strategy_id="PULLBACK_V1")])
    result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    assert [r["status"] for r in result["item_results"]] == ["ACCEPTED", "ACCEPTED"]
    packets = {p["signal_id"]: p for p in events(lab.cycle, cycle_of(raw), "RESEARCH_PACKET")}
    first, second = packets[pick(0)["signal_id"]], packets[pick(1)["signal_id"]]
    assert "strategy_id" not in first and second["strategy_id"] == "PULLBACK_V1"
    assert "strategy_id" not in second["state"]  # Jev reviews the pick, not the label.
    assert strategies.for_packet(first) is strategies.for_packet(second)


def test_admission_stamps_the_strategy_on_report_v3_crypto_setups(mx, monkeypatch):  # noqa: F811
    engine, venue, _ = mx
    original = report_tests.pick
    monkeypatch.setattr(report_tests, "pick", lambda i, symbol, now: original(
        i, symbol, now=now, **({"strategy_id": "PULLBACK_V1"} if i == 0 else {})))
    cycle, cycle_id, _ = v3_on_venue(mx, ["AAA/USD", "BBB/USD", "CCC/USD"])
    asyncio.run(cycle.tick(cycle_id))
    chosen = {p["symbol"]: p for p in cycle.approved_packets(cycle_id)}
    assert chosen["AAA/USD"]["strategy_id"] == "PULLBACK_V1"  # Declared.
    assert "strategy_id" not in chosen["BBB/USD"]  # Absent: the default.

    def admit(packet):
        classify(engine, packet["symbol"])
        live = LiveQuote(packet["symbol"], D("100.49"), D("100.51"), venue.now,
                         "ALPACA_STREAM", venue.now)
        return engine.admit(packet, live_quote=lambda _symbol: live)

    states = {s: engine._load(admit(chosen[s]))[1] for s in ("AAA/USD", "BBB/USD")}
    assert {s: (v["strategy_id"], v["strategy_version"]) for s, v in states.items()} == {
        "AAA/USD": ("PULLBACK_V1", 1), "BBB/USD": ("PULLBACK_V1", 1)}
    assert all(v["entry_type"] == "PULLBACK" for v in states.values())
    # A packet naming a strategy that is not live (intake refuses it) is never admitted: a label
    # changed after selection breaks the durable selection binding, and the system check
    # applies the named strategy's entry types (BREAKOUT only) to the pick's own entry type.
    shadow = {**chosen["CCC/USD"], "strategy_id": "BREAKOUT_7D_VOL2X_V1"}
    with pytest.raises(ValueError, match="^DURABLE_SELECTION_BINDING_REQUIRED$"):
        admit(shadow)
    live = LiveQuote("CCC/USD", D("100.49"), D("100.51"), venue.now, "ALPACA_STREAM",
                     venue.now)
    checked = system_check.evaluate(shadow, live, now=venue.now)
    assert (checked["result"], checked["code"], checked["entry_type"]) == (
        "REFUSED", "BREAKOUT_NOT_ENABLED", "PULLBACK")
    assert system_check.evaluate(chosen["CCC/USD"], live, now=venue.now)["result"] == "PASSED"


def test_dimensions_group_trades_by_strategy():
    def item(r, strategy=None):
        value = {"r_net": D(r), "win": D(r) > 0, "regime": None, "versions": {}}
        return value if strategy is None else {**value, "strategy_id": strategy}

    result = dimensions([item("1"), item("-1", "PULLBACK_V1"), item("2", "OTHER_V1")])
    assert set(result["by_strategy"]) == {"PULLBACK_V1", "OTHER_V1"}
    assert result["by_strategy"]["PULLBACK_V1"]["closed"] == 2
    assert result["by_strategy"]["OTHER_V1"]["mean_r_net"] == D(2)
    assert result["by_strategy"]["PULLBACK_V1"]["status"] == "NOT_ENOUGH_DATA"

