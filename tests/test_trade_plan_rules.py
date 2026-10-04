"""Package trade-plan (owner approval 2026-10-02, docs/TRADING-QUALITY-PLAN.md A3-A5): the pure
rules of CRYPTO_TRADE_PLAN_V1, CRYPTO_MAINTENANCE_V4 and CRYPTO_STOP_BREACH_V4.

No database, broker, provider or network: exact Decimal inputs and fixture bars only.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_holding, stop_breach, trade_plan
from catalyst_lab import crypto_maintenance as cm
from catalyst_lab.crypto_execution import (
    CryptoAsset,
    CryptoExecutionError,
    native_stop_levels,
    stop_limit_price,
)
from catalyst_lab.scan_sources import SourceIssue
from catalyst_lab.setup_scan import CompletedBar

NOW = datetime(2026, 10, 3, 14, 30, tzinfo=UTC)
LEVELS = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "98", "target": "111"}


def asset(increment="0.01"):
    return CryptoAsset.from_broker({
        "symbol": "SOL/USD", "class": "crypto", "tradable": True, "status": "active",
        "fractionable": True,
        "min_order_size": "0.0001", "min_trade_increment": "0.0001",
        "price_increment": increment,
    })


def hour_bars(count, *, now=NOW, rng=D("1.5"), base=D("100"), ranges=None, skip=()):
    """``count`` completed 1-hour bars ending at the last hour boundary before ``now``, newest
    last; ``ranges`` maps bars back from the newest to their high - low."""
    last_end = cm.floor_time(now, 3600)
    bars = []
    for back in range(count - 1, -1, -1):
        if back in skip:
            continue
        end = last_end - timedelta(hours=back)
        width = D(str((ranges or {}).get(back, rng)))
        bars.append(CompletedBar(end - timedelta(hours=1), end, base, base + width / 2,
                                 base - width / 2, base, D(1), "LAB_FIXTURE", "FIXTURE",
                                 f"LAB_FIXTURE:{end.isoformat()}", True))
    return bars


# --- CRYPTO_STOP_BREACH_V4: the stop-limit's limit -------------------------------------------


def test_the_v4_record_is_exact_and_admission_records_it_for_crypto_only():
    assert stop_breach.CRYPTO_STOP_BREACH_V4.record() == {
        "policy_id": "CRYPTO_STOP_BREACH_V4", "limit_cushion_fraction": "0.005",
        "limit_rounding": "FLOOR_TO_PRICE_INCREMENT"}
    assert stop_breach.ADMITTED_STOP_LIMIT is stop_breach.CRYPTO_STOP_BREACH_V4
    assert stop_breach.limit_admission_fields({"market": "CRYPTO"}) == {
        "stop_limit_policy": stop_breach.CRYPTO_STOP_BREACH_V4.record()}
    assert stop_breach.limit_admission_fields({"market": "US_STOCKS"}) == {}
    for altered in ({"limit_cushion_fraction": "0.01"}, {"policy_id": "CRYPTO_STOP_BREACH_V5"},
                    {"limit_rounding": "HALF_EVEN"}):
        with pytest.raises(ValueError, match="EXPLICIT_STOP_LIMIT_POLICY_REQUIRED"):
            stop_breach.limit_cushion({"stop_limit_policy": {
                **stop_breach.CRYPTO_STOP_BREACH_V4.record(), **altered}})
    with pytest.raises(ValueError, match="EXPLICIT_STOP_LIMIT_POLICY_REQUIRED"):
        stop_breach.limit_cushion({"stop_limit_policy": {
            **stop_breach.CRYPTO_STOP_BREACH_V4.record(), "extra": 1}})
    # Every setup admitted before V4 recorded nothing: the one-tick limit.
    assert stop_breach.limit_cushion({"stop_breach_version": "CRYPTO_STOP_BREACH_V3"}) is None
    assert stop_breach.limit_cushion(
        {"stop_limit_policy": stop_breach.CRYPTO_STOP_BREACH_V4.record()}) == D("0.005")


@pytest.mark.parametrize("increment,stop,v1,v4", [
    ("0.01", "100", ("100", "99.99"), ("100", "99.50")),
    # An off-grid stop is raised to the grid first; the cushion is taken from the native stop.
    ("0.01", "100.004", ("100.01", "100.00"), ("100.01", "99.50")),
    ("0.01", "8.69508", ("8.70", "8.69"), ("8.70", "8.65")),
    # The cushion is under one increment: one increment below the native stop.
    ("0.01", "1.00", ("1.00", "0.99"), ("1.00", "0.99")),
    ("0.000001", "0.000150", ("0.000150", "0.000149"), ("0.000150", "0.000149")),
    ("0.00000001", "0.00004242", ("0.00004242", "0.00004241"), ("0.00004242", "0.00004220")),
    # Never below one increment.
    ("0.01", "0.01", ("0.01", "0.01"), ("0.01", "0.01")),
    ("0.5", "2343.7", ("2344.0", "2343.5"), ("2344.0", "2332.0")),
])
def test_native_stop_levels_one_tick_before_v4_and_half_a_percent_floored_under_v4(
        increment, stop, v1, v4):
    a = asset(increment)
    assert native_stop_levels(a, D(stop)) == tuple(D(v) for v in v1)
    assert native_stop_levels(a, D(stop), D("0.005")) == tuple(D(v) for v in v4)
    native, limit = native_stop_levels(a, D(stop), D("0.005"))
    # The plan's geometry (stop_limit <= stop) holds and both prices are on the grid.
    assert limit <= native and limit % D(increment) == 0 and native % D(increment) == 0
    # The plan's own derivation (manage's stop_limit, then raised to the grid) is the same.
    assert a.price_at_or_above(stop_limit_price(a, D(stop), D("0.005"))) == limit


def test_the_v4_limit_never_rises_above_one_tick_below_and_never_falls_with_a_higher_stop():
    a = asset("0.01")
    previous = None
    for cents in range(100, 2000, 7):
        stop = D(cents) / 100
        native, limit = native_stop_levels(a, stop, D("0.005"))
        assert limit <= native - D("0.01") or native == D("0.01")
        assert native - limit <= native * D("0.005") + D("0.01")
        if previous is not None:
            assert limit >= previous  # A raised stop never lowers its limit.
        previous = limit


def test_an_invalid_cushion_is_refused():
    for cushion in (D(0), D(1), D("-0.01")):
        with pytest.raises(CryptoExecutionError):
            stop_limit_price(asset(), D(100), cushion)


# --- The hourly range ----------------------------------------------------------------------------


def test_the_hourly_range_is_the_mean_high_minus_low_of_the_last_24_completed_hours():
    bars = hour_bars(30, ranges={0: "3", 23: "2.5", 24: "40"})  # The 25th bar back is too old.
    value, evidence = cm.hourly_range(bars, NOW)
    q = D("1e-20")
    assert value.quantize(q) == ((D("1.5") * 22 + D(3) + D("2.5")) / 24).quantize(q)
    assert evidence["bars"] == 24 and evidence["min_bars"] == 20
    assert evidence["first_bar_start"] == (cm.floor_time(NOW, 3600) - timedelta(hours=24)
                                           ).isoformat()
    assert evidence["hourly_range"] == str(value)


def test_a_bar_still_forming_and_short_bars_are_ignored():
    bars = hour_bars(24)
    forming = CompletedBar(cm.floor_time(NOW, 3600), cm.floor_time(NOW, 3600) + timedelta(hours=1),
                           D(100), D(200), D(1), D(100), D(1), "LAB_FIXTURE", "FIXTURE", "F", True)
    minute = CompletedBar(NOW - timedelta(minutes=5), NOW - timedelta(minutes=4), D(100), D(500),
                          D(1), D(100), D(1), "LAB_FIXTURE", "FIXTURE", "M", True)
    assert cm.hourly_range([*bars, forming, minute], NOW)[0] == D("1.5")


@pytest.mark.parametrize("missing,available", [(4, True), (5, False)])
def test_at_least_twenty_of_the_twenty_four_hours_are_required(missing, available):
    bars = hour_bars(24, skip=set(range(missing)))
    value, evidence = cm.hourly_range(bars, NOW)
    assert (value is not None) is available
    assert evidence["bars"] == 24 - missing
    if not available:
        assert evidence["hourly_range"] is None


# --- CRYPTO_TRADE_PLAN_V1: the planned levels -------------------------------------------------


def test_the_plan_record_is_exact_and_pins_a_24_hour_window():
    assert trade_plan.CRYPTO_TRADE_PLAN.record() == {
        "policy_id": "CRYPTO_TRADE_PLAN_V1",
        "hourly_range_basis": "MEAN_HIGH_MINUS_LOW_OF_COMPLETED_1H_BARS_LAST_24H",
        "hourly_range_bars": 24, "hourly_range_min_bars": 20, "stop_range_multiple": "2",
        "target_r_multiple": "1.5", "entry_basis": "ENTRY_TRIGGER", "min_stop_fraction": "0.02",
        "window_minutes": 1440, "rounding": "STOP_AND_TARGET_FLOOR_TO_PRICE_INCREMENT"}
    with pytest.raises(ValueError, match="EXPLICIT_TRADE_PLAN_POLICY_REQUIRED"):
        trade_plan.TradePlanPolicy(**{**trade_plan.CRYPTO_TRADE_PLAN.record(),
                                      "window_minutes": 240})
    window = trade_plan.CRYPTO_TRADE_PLAN.window_setting()
    for arm, kind, policy_id in (("JEV_MANAGED", "review", "CRYPTO_WINDOW_REVIEW_V1"),
                                 ("FIXED_EXIT", "hold", "CRYPTO_WINDOW_HOLD_V1")):
        hold = crypto_holding.admission_policy(True, arm, window)
        assert hold.policy_id == policy_id and hold.window_seconds == 86400, kind
        assert crypto_holding.window_fields(hold) == {"holding_window_seconds": 86400}


def plan(levels=LEVELS, hr="1.5", increment="0.01"):
    return trade_plan.plan(levels, hourly_range=D(hr), range_evidence={"bars": 24},
                           increment=D(increment))


def test_a_stop_inside_two_hourly_ranges_is_widened_and_the_target_capped_at_one_and_a_half_r():
    record = plan()
    # Entry 100 - 2 x 1.5 = 97 (below the research stop 98); target 100 + 1.5 x 3 = 104.50.
    assert (record["stop"], record["stop_basis"]) == ("97.00", "HOURLY_RANGE_FLOOR")
    assert (record["target"], record["target_basis"], record["target_cap"]) == (
        "104.50", "PLAN_CAP", "104.50")
    assert record["research_levels"] == LEVELS
    assert record["policy"] == trade_plan.CRYPTO_TRADE_PLAN.record()
    assert record["range_floor"] == "97.0" and record["hourly_range"] == "1.5"
    assert D(record["risk_per_coin"]) == D("3.10") and D(record["entry_risk"]) == D(3)
    assert D(record["target_r_from_entry"]) == D("1.5")
    assert record["hourly_range_evidence"] == {"bars": 24}


def test_a_wider_research_stop_and_a_nearer_research_target_are_kept():
    levels = {**LEVELS, "stop": "95", "target": "106"}
    record = plan(levels, hr="1")
    assert (record["stop"], record["stop_basis"]) == ("95.00", "RESEARCH_STOP")
    # Cap 100 + 1.5 x 5 = 107.5 is above the research target 106.
    assert (record["target"], record["target_basis"], record["target_cap"]) == (
        "106.00", "RESEARCH_TARGET", "107.50")


def test_the_stop_and_the_target_are_rounded_down_to_the_grid():
    record = plan(hr="1.234", increment="0.05")
    # 100 - 2.468 = 97.532 -> 97.50 (a lower, wider stop); 100 + 1.5 x 2.5 = 103.75 on grid.
    assert (record["stop"], record["target"]) == ("97.50", "103.75")
    record = plan(hr="1.2345", increment="0.01")
    # 100 - 2.469 = 97.531 -> 97.53; cap 100 + 1.5 x 2.47 = 103.705 -> 103.70 (down).
    assert (record["stop"], record["target"], record["target_cap"]) == (
        "97.53", "103.70", "103.70")


def test_a_zero_hourly_range_keeps_the_research_levels_up_to_the_cap():
    record = plan(hr="0")
    assert (record["stop"], record["stop_basis"]) == ("98.00", "RESEARCH_STOP")
    assert record["target"] == "103.00"  # 100 + 1.5 x 2.


def test_the_two_percent_minimum_applies_to_the_stop_that_is_used():
    # A research stop under 2% of the max entry is refused even when the range barely widens
    # it (the system check refuses it first at admission; the plan checks its own stop again).
    with pytest.raises(trade_plan.PlanRefused, match="STOP_DISTANCE_BELOW_MINIMUM") as caught:
        plan({**LEVELS, "stop": "99"}, hr="0.3")
    assert caught.value.evidence["stop"] == "99.00" and caught.value.evidence[
        "stop_distance_fraction"]
    # The same pick on a coin whose range is wide enough is planned at 2 hourly ranges.
    record = plan({**LEVELS, "stop": "99"}, hr="1.2")
    assert record["stop"] == "97.60" and D(record["stop_distance_fraction"]) >= D("0.02")


def test_a_target_at_or_below_the_max_entry_and_a_stop_at_or_below_zero_are_refused():
    with pytest.raises(trade_plan.PlanRefused, match="TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY"):
        plan({"entry_trigger": "100", "max_entry_price": "110", "stop": "96", "target": "140"},
             hr="0")
    with pytest.raises(trade_plan.PlanRefused, match="TRADE_PLAN_STOP_NOT_POSITIVE"):
        plan({"entry_trigger": "1", "max_entry_price": "1.01", "stop": "0.9", "target": "2"},
             hr="0.6")
    assert {trade_plan.TARGET_NOT_ABOVE_MAX_ENTRY, trade_plan.STOP_NOT_POSITIVE} <= (
        __import__("catalyst_lab.system_check").system_check.PERMANENT_REFUSALS)


def test_invalid_inputs_are_refused():
    for bad in ({**LEVELS, "stop": "100"}, {**LEVELS, "target": "100"}):
        with pytest.raises(ValueError, match="INVALID_TRADE_PLAN_INPUT"):
            plan(bad)
    with pytest.raises(ValueError, match="INVALID_TRADE_PLAN_INPUT"):
        plan(hr="-1")


def test_initial_levels_are_the_plans_for_its_setups_and_the_packets_for_every_other():
    record = plan()
    decimals = {k: D(v) for k, v in LEVELS.items()}
    assert trade_plan.initial_levels(decimals, {}) is decimals
    assert trade_plan.initial_levels(LEVELS, {"stop": "97.00"}) is LEVELS
    planned = trade_plan.initial_levels(decimals, {"trade_plan": record})
    assert planned == {**decimals, "stop": D("97.00"), "target": D("104.50")}
    assert trade_plan.initial_levels(LEVELS, {"trade_plan": record}) == {
        **LEVELS, "stop": "97.00", "target": "104.50"}
    assert trade_plan.target_cap({"trade_plan": record}) == D("104.50")
    assert trade_plan.target_cap({}) is None
    altered = {**record, "policy": {**record["policy"], "target_r_multiple": "3"}}
    with pytest.raises(ValueError, match="EXPLICIT_TRADE_PLAN_POLICY_REQUIRED"):
        trade_plan.initial_levels(decimals, {"trade_plan": altered})


# --- The runtime's hourly-range reader ----------------------------------------------------------


class HourSource:
    def __init__(self, clock):
        self.clock, self.calls, self.issue, self.count = clock, [], None, 30

    def timeframe_bars(self, market, symbol, *, timeframe, count):
        self.calls.append((market, symbol, timeframe, count))
        if self.issue:
            return (), (SourceIssue(market, symbol, self.issue, "/v1beta3/crypto/us/bars"),)
        return tuple(hour_bars(self.count, now=self.clock())), ()


def test_the_reader_reuses_a_range_until_the_next_hour_and_waits_a_minute_after_a_failure():
    now = [NOW]
    source = HourSource(lambda: now[0])
    budget = {"left": 10}

    def spend():
        if budget["left"] <= 0:
            return False
        budget["left"] -= 1
        return True

    reader = trade_plan.HourlyRangeReader(source, clock=lambda: now[0], spend=spend)
    value, evidence = reader("SOL/USD")
    assert value == D("1.5") and evidence["bars"] == 24
    assert source.calls == [("CRYPTO", "SOL/USD", "1Hour", 30)]
    now[0] += timedelta(minutes=20)
    assert reader("SOL/USD")[0] == D("1.5") and len(source.calls) == 1  # Same hour: reused.
    now[0] = cm.floor_time(now[0], 3600) + timedelta(hours=1, seconds=1)
    source.issue = "SCAN_HTTP_503"
    with pytest.raises(trade_plan.HourlyRangeUnavailable) as caught:
        reader("SOL/USD")
    assert caught.value.code == "SCAN_HTTP_503" and len(source.calls) == 2
    now[0] += timedelta(seconds=59)
    with pytest.raises(trade_plan.HourlyRangeUnavailable) as caught:
        reader("SOL/USD")
    assert caught.value.code == "HOURLY_RANGE_RETRY_WAIT" and len(source.calls) == 2
    now[0] += timedelta(seconds=1)
    source.issue, source.count = None, 19  # Too few hours.
    with pytest.raises(trade_plan.HourlyRangeUnavailable) as caught:
        reader("SOL/USD")
    assert caught.value.code == "HOURLY_BARS_TOO_FEW" and caught.value.evidence["bars"] == 19
    budget["left"] = 0
    now[0] += timedelta(minutes=2)
    with pytest.raises(trade_plan.HourlyRangeUnavailable) as caught:
        reader("SOL/USD")
    assert caught.value.code == "REST_READ_LIMIT_THIS_TICK" and len(source.calls) == 3


# --- CRYPTO_MAINTENANCE_V4: the record and the guards -------------------------------------------


def test_the_v4_record_is_v3_plus_the_guards_and_new_trades_record_it():
    v4 = cm.CRYPTO_MAINTENANCE_V4.record()
    assert v4 == {**cm.CRYPTO_MAINTENANCE_V3.record(), "policy_id": "CRYPTO_MAINTENANCE_V4",
                  "raise_min_r": "1", "raise_range_multiple": "2",
                  "raise_spacing_seconds": 900, "breakeven_fee_fraction": "0.0025",
                  "breakeven_basis": "ENTRY_PLUS_ROUND_TRIP_TAKER_FEES",
                  "target_cap": "CRYPTO_TRADE_PLAN_V1_TARGET_CAP"}
    # Admitted from package trade-plan until package jev-b1, which admits V5 (V4's guards).
    assert cm.ADMITTED_MAINTENANCE is cm.CRYPTO_MAINTENANCE_V5
    assert cm.policy_from_record(v4) == cm.CRYPTO_MAINTENANCE_V4
    assert cm.guarded(cm.CRYPTO_MAINTENANCE_V4)  # V3's spend-guard cadence is kept.
    for broken in ({**v4, "raise_spacing_seconds": 60}, {**v4, "breakeven_fee_fraction": "0"},
                   {k: v for k, v in v4.items() if k != "raise_min_r"}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
            cm.policy_from_record(broken)
    # The earlier versions have no guards.
    for older in (cm.CRYPTO_MAINTENANCE, cm.CRYPTO_MAINTENANCE_V2, cm.CRYPTO_MAINTENANCE_V3):
        assert cm.raise_guards(older, hourly_range=D(1), last_raise_at=None) is None
    assert cm.MAINTENANCE_VERSIONS[-2:] == ("CRYPTO_MAINTENANCE_V4", "CRYPTO_MAINTENANCE_V5")


def test_breakeven_is_the_entry_plus_round_trip_fees_rounded_up():
    assert cm.breakeven_price(D("100.10"), D("0.01")) == D("100.10")  # V1-V3.
    # 100.10 x 1.0025 / 0.9975 = 100.6018... -> 100.61.
    assert cm.breakeven_price(D("100.10"), D("0.01"), D("0.0025")) == D("100.61")
    sold = D("100.61") * (1 - D("0.0025"))
    assert sold >= D("100.10") * (1 + D("0.0025"))  # A sell there nets the buy's cost.
    assert cm.breakeven_price(D("0.2429"), D("0.0001"), D("0.0025")) == D("0.2442")


SWING_15M = {"base": D("105.5"), "spread": D("0.2"), "lows": {3: "103.004", 9: "102.51"}}


def fifteen(now=NOW):
    from tests.maintenance_fixtures import bars_series

    return bars_series(now, 96, seconds=900, **SWING_15M)


def options(*, best_bid="106", guards="v4", hr="1.5", last=None, bid="106", stop="95",
            entry="100.10", risk="5.10"):
    g = None if guards is None else cm.RaiseGuards(
        D(hr) if hr is not None else None, last, None)
    return [(o.price, o.bases) for o in cm.stop_options(
        bars_15m=fifteen(), bars_1h=hour_bars(72, base=D("106"), ranges={6: "5"}), now=NOW,
        bid=D(bid), entry=D(entry), current_stop=D(stop), best_bid=D(best_bid), risk=D(risk),
        increment=D("0.01"), guards=g)]


def test_v3_options_are_unchanged_and_v4_offers_nothing_inside_two_hourly_ranges():
    v3 = options(guards=None)
    assert (D("103.00"), ("SWING_LOW_15M",)) in v3 and (D("100.10"), ("BREAKEVEN",)) in v3
    assert (D("103.50"), ("SWING_LOW_1H",)) in v3
    v4 = options()
    # Bid 106 - 2 x 1.5 = 103: the 103.00 swing low is allowed (at the floor), 103.50 is not.
    assert all(price <= D(103) for price, _ in v4)
    assert (D("103.00"), ("SWING_LOW_15M",)) in v4
    assert (D("100.61"), ("BREAKEVEN_AFTER_FEES",)) in v4
    assert not any("BREAKEVEN" in bases for _, bases in v4)
    tighter = options(hr="1.8")  # Floor 102.4: 103.00 is no longer offered.
    assert D("103.00") not in [p for p, _ in tighter] and D("102.51") not in [
        p for p, _ in tighter]


@pytest.mark.parametrize("change,empty", [
    ({"best_bid": "105.19"}, True),  # Entry 100.10 + R 5.10 = 105.20 not reached.
    ({"best_bid": "105.20"}, False),
    ({"last": NOW - timedelta(seconds=899)}, True),
    ({"last": NOW - timedelta(seconds=900)}, False),
    ({"hr": None}, True),
])
def test_v4_offers_no_stop_before_1r_within_15_minutes_or_without_the_range(change, empty):
    assert (options(**change) == []) is empty


def check(**overrides):
    values = {
        "old_stop": D(95), "new_stop": D("101.00"), "old_target": D(111), "new_target": None,
        "bid": D(106), "min_bid": D(105), "max_bid": D(106), "answered_at": NOW, "now": NOW,
        "increment": D("0.01"),
        "guards": cm.RaiseGuards(D("1.5"), None, D("107.50")), "best_bid": D(106),
        "entry": D("100.10"), "risk": D("5.10"),
    }
    values.update(overrides)
    return cm.check_change(**values)


@pytest.mark.parametrize("overrides,code", [
    ({}, None),
    ({"new_stop": D("103.00")}, None),  # Exactly two hourly ranges below the bid.
    ({"new_stop": D("103.01")}, "STOP_INSIDE_HOURLY_RANGE"),
    ({"best_bid": D("105.19")}, "STOP_RAISE_BEFORE_1R"),
    ({"best_bid": D("105.20")}, None),
    ({"guards": cm.RaiseGuards(D("1.5"), NOW - timedelta(seconds=899), None)},
     "STOP_RAISE_TOO_SOON"),
    ({"guards": cm.RaiseGuards(D("1.5"), NOW - timedelta(seconds=900), None)}, None),
    ({"guards": cm.RaiseGuards(None, None, None)}, "HOURLY_RANGE_UNAVAILABLE"),
    # V3's checks keep their codes and come first.
    ({"new_stop": D("94")}, "STOP_NOT_ABOVE_CURRENT"),
    ({"new_stop": D("105.60"), "best_bid": D(100), "min_bid": D(106)}, "STOP_TOO_CLOSE_TO_BID"),
    # Targets: V3's rules, then the plan's cap.
    ({"new_stop": None, "new_target": D("107.50"), "old_target": D("106.5")}, None),
    ({"new_stop": None, "new_target": D("107.51"), "old_target": D("106.5")},
     "TARGET_ABOVE_PLAN_CAP"),
    ({"new_stop": None, "new_target": D("107.51"), "old_target": D("106.5"),
      "guards": cm.RaiseGuards(D("1.5"), None, None)}, None),
    ({"new_stop": None, "new_target": D("110"), "old_target": D("106.5"), "best_bid": D(100)},
     "TARGET_ABOVE_PLAN_CAP"),
    ({"new_stop": None, "new_target": D("105")}, "TARGET_NOT_ABOVE_PRICE"),
])
def test_v4_checks_before_applying(overrides, code):
    assert check(**overrides) == code


def test_without_guards_check_change_is_v3s():
    assert check(guards=None, best_bid=None, entry=None, risk=None, min_bid=D(106),
                 new_stop=D("105.40")) is None  # 0.57% below the bid: V3 allows it.
    assert check(guards=None, new_stop=None, new_target=D(130)) is None


def test_target_options_never_offer_above_the_plan_cap():
    from tests.maintenance_fixtures import bars_series

    hours = bars_series(NOW, 168, seconds=3600, base=D("106"), spread=D("0.9"),
                        highs={30: "113.2", 60: "114.3"})
    found = [o.price for o in cm.target_options(
        bars_15m=fifteen(), bars_1h=hours, now=NOW, bid=D(106), current_target=D("107"),
        increment=D("0.01"))]
    assert D("113.20") in found
    capped = [o.price for o in cm.target_options(
        bars_15m=fifteen(), bars_1h=hours, now=NOW, bid=D(106), current_target=D("107"),
        increment=D("0.01"), target_cap=D("107.50"))]
    assert all(p <= D("107.50") for p in capped)


def test_guards_round_trip_through_a_request_basis():
    guards = cm.RaiseGuards(D("1.25"), NOW, D("104.5"))
    assert cm.RaiseGuards.from_record(guards.record()) == guards
    assert cm.RaiseGuards.from_record(cm.RaiseGuards(None, None).record()) == cm.RaiseGuards(
        None, None, None)
    assert cm.last_stop_raise({"stop_raised_at": NOW.isoformat()}) == NOW
    assert cm.last_stop_raise({}) is None
