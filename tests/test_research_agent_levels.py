"""research_agent.levels: the level rules the real Jev accepted on 2026-09-27, at their
boundaries. Pure and offline: no I/O anywhere in this module or these tests.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from research_agent import levels
from research_agent.market import Bar

D = Decimal
START = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)


def make_series(lows, highs, timeframe="4h"):
    """``{timeframe: [Bar, ...]}`` with the given lows/highs, oldest first. Open and
    close are set equal to each bar's low; levels.py never reads them."""
    assert len(lows) == len(highs)
    return {timeframe: [
        Bar(started_at=START + timedelta(hours=4 * i), open=D(str(lo)), high=D(str(hi)),
           low=D(str(lo)), close=D(str(lo)), volume=D(10))
        for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))
    ]}


INC = D("0.01")

# The hand-verified rule-A scenario used across several tests: a deep window-low at
# index 5 (the rule-A stop reference), a held shallower pivot at index 15 exactly 5%
# below mid=100 (the entry), and the window's true peak at index 10 (the target) --
# not at the last bar.
BASE_LOWS = [100] * 20
BASE_HIGHS = [101] * 20
BASE_LOWS[5], BASE_HIGHS[5] = 90, 90.5
BASE_LOWS[15], BASE_HIGHS[15] = 95, 95.5
BASE_HIGHS[10] = 130
MID = D(100)


def base_setup(overrides=None):
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    for index, (lo, hi) in (overrides or {}).items():
        lows[index], highs[index] = lo, hi
    series = make_series(lows, highs)
    return levels.find_setup(series, mid=MID, increment=INC, rules=("A",),
                             timeframes=("4h",), windows=(20,))


def test_rule_a_finds_the_hand_verified_setup():
    setup, tried = base_setup()
    assert setup is not None, tried
    assert setup.rule == "A" and setup.timeframe == "4h" and setup.window == 20
    assert setup.entry_index == 15 and setup.stop_index == 5 and setup.target_index == 10
    assert setup.entry == D("95.00")
    assert setup.max_entry == D("95.15")  # 95 * 1.0015 = 95.1425, rounded UP to 0.01
    assert setup.stop == D("89.64")  # 90 * 0.996 = 89.64 exactly, rounded DOWN
    assert setup.target == D("130")
    assert setup.reward_risk == (setup.target - setup.max_entry) / (setup.max_entry - setup.stop)
    assert setup.reward_risk >= D(2)


# --- Held low: no later cited bar may have traded below the entry -------------------------

def test_entry_pivot_that_is_later_broken_is_not_a_candidate():
    # Bar 17's low (93) trades below the index-15 pivot's low (95): no longer held. Bar
    # 17 itself is also a held pivot, but 7% below mid is outside the band, so rule A
    # now has no in-band candidate at all (only the index-5 and index-17 pivots are held,
    # and both sit outside the 0.6-6% band).
    setup, tried = base_setup({17: (93, 93.5)})
    assert setup is None
    assert any("no held higher low" in reason for reason in tried)


def test_entry_pivot_untouched_afterward_remains_a_candidate():
    # Sanity check on the harness itself: touching a bar BEFORE the pivot (rule A only
    # cares about the window's absolute low for the stop, not held-ness before entry)
    # must not break the held condition.
    setup, _ = base_setup({2: (93, 93.5)})  # Before the pivot; lower than the pivot too.
    assert setup is not None and setup.entry_index == 15


# --- The 0.6%-6% entry band, at its edges --------------------------------------------------

def test_entry_exactly_at_the_lower_band_edge_is_accepted():
    # 0.6% below mid=100 is 99.4; make it a held pivot (neighbors >= 99.4, nothing after
    # it lower) and keep the deep low at index 5 as the sole stop reference.
    setup, _ = base_setup({15: (99.4, 99.9)})
    assert setup is not None and setup.entry == D("99.40")


def test_entry_just_inside_the_lower_band_edge_is_accepted():
    setup, _ = base_setup({15: (99.39, 99.89)})
    assert setup is not None and setup.entry == D("99.39")


def test_entry_just_outside_the_lower_band_edge_is_rejected():
    # 99.41 is 0.59% below mid: outside the 0.6% minimum discount.
    setup, _tried = base_setup({15: (99.41, 99.91)})
    assert setup is None


def test_entry_exactly_at_the_upper_band_edge_is_accepted():
    # 6% below mid=100 is 94.
    setup, _ = base_setup({15: (94, 94.5)})
    assert setup is not None and setup.entry == D("94.00")


def test_entry_just_outside_the_upper_band_edge_is_rejected():
    # 93.99 is 6.01% below mid: outside the 6% maximum discount, and it is now the
    # window's overall low too, so there is no other candidate either.
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    lows[15], highs[15] = 93.99, 94.49
    lows[5], highs[5] = 100, 101  # Remove the old deep low so 93.99 is the only extreme.
    setup, _ = levels.find_setup(make_series(lows, highs), mid=MID, increment=INC,
                                 rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is None


# --- Minimum 2% stop distance, at the edge -------------------------------------------------

def test_stop_distance_exactly_two_percent_is_accepted():
    # Solve for a window-low L such that (max_entry - 0.996L) / max_entry == 2% exactly,
    # with entry fixed at 95 (max_entry = 95.15 as in the base scenario).
    max_entry = D("95.15")
    stop = (max_entry * D("0.98")).quantize(INC)
    window_low = (stop / D("0.996")).quantize(INC)
    setup, _ = base_setup({5: (window_low, window_low + 1)})
    assert setup is not None
    assert (setup.max_entry - setup.stop) / setup.max_entry >= D("0.02")


def test_stop_distance_just_under_two_percent_is_rejected():
    max_entry = D("95.15")
    # A window-low just high enough that the 0.4% buffer lands the stop distance under 2%.
    stop = (max_entry * D("0.985")).quantize(INC)
    window_low = (stop / D("0.996")).quantize(INC)
    setup, tried = base_setup({5: (window_low, window_low + 1)})
    assert setup is None
    assert any("no held higher low" in reason for reason in tried)


# --- Minimum 2R reward:risk, at the edge ---------------------------------------------------

def test_reward_risk_exactly_two_is_accepted():
    max_entry, stop = D("95.15"), D("89.64")
    need = max_entry + 2 * (max_entry - stop)
    target_high = need.quantize(INC)
    setup, _ = base_setup({10: (target_high - 1, target_high)})
    assert setup is not None and setup.target == target_high
    assert setup.reward_risk >= D(2)


def test_reward_risk_just_under_two_is_rejected():
    max_entry, stop = D("95.15"), D("89.64")
    need = max_entry + 2 * (max_entry - stop)
    target_high = (need - INC).quantize(INC)  # One increment short of 2R.
    setup, _ = base_setup({10: (target_high - 1, target_high)})
    assert setup is None


# --- Target: never the latest bar's high, never with a higher high in the window ----------

def test_target_refused_when_the_latest_bar_makes_the_window_high():
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    highs[19] = 150  # The most recent bar now makes a fresh, higher window high.
    setup, tried = levels.find_setup(make_series(lows, highs), mid=MID, increment=INC,
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is None
    assert any("most recent bar" in reason for reason in tried)


def test_target_is_the_true_window_maximum_not_a_lower_pivot():
    """The bug the real Jev caught on 2026-09-27 (BTC/PEPE, LEVELS_SUPPORTED_BY_BARS_NO
    for "a higher high in the same window"): the original script could pick a nearby
    pivot high while an even higher bar sat elsewhere in the window. Add such a higher
    bar (not at the pivot, not at the last bar) and check the target moves to it."""
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    highs[3] = 500  # Far higher than the index-10 pivot (130), but not the last bar.
    setup, _ = levels.find_setup(make_series(lows, highs), mid=MID, increment=INC,
                                 rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is not None
    assert setup.target_index == 3 and setup.target == D("500")


def test_a_tied_latest_bar_high_is_also_refused():
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    highs[19] = 130  # Ties the index-10 window high exactly.
    setup, tried = levels.find_setup(make_series(lows, highs), mid=MID, increment=INC,
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is None
    assert any("most recent bar" in reason for reason in tried)


# --- Price-increment rounding --------------------------------------------------------------

def test_increment_rounding_uses_a_coarser_grid():
    coarse_setup, _ = levels.find_setup(
        make_series(BASE_LOWS, BASE_HIGHS), mid=MID, increment=D("0.5"),
        rules=("A",), timeframes=("4h",), windows=(20,),
    )
    assert coarse_setup is not None
    assert coarse_setup.entry == D("95.0")  # 95 rounds down to a multiple of 0.5 unchanged.
    assert coarse_setup.max_entry == D("95.5")  # 95.1425 rounds UP to the next 0.5.
    assert coarse_setup.stop == D("89.5")  # 89.64 rounds DOWN to the next 0.5.


def test_round_price_rejects_a_non_positive_increment():
    with pytest.raises(levels.LevelsError):
        levels.round_price(D(100), D(0))


# --- Rule B: a held lower pivot for the stop, tried only when rule A finds nothing ---------

def test_rule_b_uses_a_held_lower_pivot_and_states_where_the_structure_starts():
    # A window low so deep that rule A's stop is too far away for a 2R target (its
    # required target grows with the stop distance); a much closer held pivot lets
    # rule B reach the SAME target with a valid, tighter stop.
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    lows[5], highs[5] = 50, 50.5  # Very deep: rule A's stop distance makes 2R unreachable.
    lows[8], highs[8] = 92, 92.5  # A held, much closer lower pivot below the entry (95).
    series = make_series(lows, highs)
    setup_a, _ = levels.find_setup(series, mid=MID, increment=INC, rules=("A",),
                                   timeframes=("4h",), windows=(20,))
    assert setup_a is None  # Confirms rule A alone cannot solve this window.
    setup, tried = levels.find_setup(series, mid=MID, increment=INC, rules=("A", "B"),
                                     timeframes=("4h",), windows=(20,))
    assert setup is not None and setup.rule == "B", tried
    assert setup.entry_index == 15 and setup.stop_index == 8
    assert setup.stop == (D("92") * D("0.996")).quantize(INC)
    assert setup.reward_risk >= D(2)


def test_rule_b_is_never_tried_before_rule_a_across_every_timeframe():
    # Rule A succeeds on 6h but not 4h (no bars at all); with the default rule order (A
    # across every timeframe before B), the 6h rule-A setup must win over any rule-B one.
    setup, _ = levels.find_setup(
        {"4h": [], "6h": make_series(BASE_LOWS, BASE_HIGHS)["4h"]},
        mid=MID, increment=INC, timeframes=("4h", "6h"),
    )
    assert setup is not None and setup.rule == "A" and setup.timeframe == "6h"


# --- Timeframe/window search order and "too few bars" ---------------------------------------

def test_too_few_bars_is_reported_and_the_next_combination_is_tried():
    series = {"4h": make_series(BASE_LOWS, BASE_HIGHS)["4h"][:10], "6h": [],
             "1d": [], "2h": [], "1h": make_series(BASE_LOWS, BASE_HIGHS)["4h"]}
    setup, tried = levels.find_setup(series, mid=MID, increment=INC)
    assert any("only 10 completed bars" in reason for reason in tried)
    assert setup is not None and setup.timeframe == "1h"  # 1h is tried last but is present.


def test_no_qualifying_setup_reports_every_combination_tried():
    empty = {tf: [] for tf in levels.TIMEFRAME_ORDER}
    setup, tried = levels.find_setup(empty, mid=MID, increment=INC)
    assert setup is None
    assert len(tried) == len(levels.RULES) * len(levels.TIMEFRAME_ORDER) * len(levels.WINDOWS)


# --- Research profiles: DAILY_V1 (the default, unchanged) and INTRADAY_V1 -------------------

def profile_setup(overrides, profile, timeframes=("4h",)):
    """The base scenario with ``overrides``, its bars filed under every one of
    ``timeframes``, searched with ``profile`` (its own timeframes, windows and band)."""
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    for index, (lo, hi) in overrides.items():
        lows[index], highs[index] = lo, hi
    bars = make_series(lows, highs)["4h"]
    return levels.find_setup({timeframe: bars for timeframe in timeframes}, mid=MID,
                             increment=INC, profile=profile)


def test_the_default_profile_is_daily_v1_exactly_as_before():
    profile = levels.DEFAULT_PROFILE
    assert profile is levels.DAILY_V1 and profile.name == "DAILY_V1"
    assert profile.timeframes == ("4h", "6h", "1d", "2h", "1h") == levels.TIMEFRAME_ORDER
    assert profile.windows == (20, 24, 30) == levels.WINDOWS
    assert (profile.entry_band_min, profile.entry_band_max) == (D("0.006"), D("0.06"))
    assert (profile.hourly_lookback_hours, profile.daily_lookback_days) == (300, 60)
    assert profile.band_text == "0.6-6%" and profile.timeframes_text == "4h/6h/1d/2h/1h"
    # "intraday" is the current intraday profile; the first one stays selectable.
    assert levels.PROFILES == {"daily": levels.DAILY_V1, "intraday": levels.INTRADAY_V2,
                               "intraday-v1": levels.INTRADAY_V1}
    assert set(levels.PROFILES_BY_NAME) == {"DAILY_V1", "INTRADAY_V1", "INTRADAY_V2"}


def test_the_intraday_profile_tries_shorter_timeframes_first_with_a_nearer_band():
    profile = levels.INTRADAY_V1
    assert profile.name == "INTRADAY_V1"
    assert profile.timeframes == ("1h", "2h", "4h")
    assert (profile.entry_band_min, profile.entry_band_max) == (D("0.003"), D("0.03"))
    assert profile.windows == levels.WINDOWS  # The windows are the app-accepted ones.
    assert profile.band_text == "0.3-3%" and profile.timeframes_text == "1h/2h/4h"
    # No daily bars, so no daily candles; enough 1-hour candles for 30 four-hour bars plus
    # the bucket still forming, within Coinbase's single-call cap.
    assert profile.daily_lookback_days == 0
    assert 4 * max(profile.windows) + 4 <= profile.hourly_lookback_hours <= 300


def test_a_setup_half_a_percent_below_the_mid_qualifies_intraday_but_not_daily():
    # Entry 99.50: 0.5% below mid=100, inside 0.3%-3% but under DAILY_V1's 0.6% minimum.
    intraday, _ = profile_setup({15: (99.5, 100)}, levels.INTRADAY_V1)
    assert intraday is not None and intraday.entry == D("99.50")
    assert intraday.max_entry == D("99.65")  # 99.5 x 1.0015 = 99.64925, rounded UP.
    assert intraday.stop == D("89.64")  # Rule A: 0.4% under the window low, unchanged.
    assert (intraday.max_entry - intraday.stop) / intraday.max_entry >= levels.MIN_STOP_DISTANCE
    assert intraday.reward_risk >= levels.MIN_REWARD_RISK
    daily, tried = profile_setup({15: (99.5, 100)}, levels.DAILY_V1)
    assert daily is None
    assert ("4h/20 rule A: no held higher low 0.6-6% below price with a stop >=2% away and a "
            "2R cited target") in tried


def test_a_setup_four_percent_below_the_mid_qualifies_daily_but_not_intraday():
    daily, _ = profile_setup({15: (96, 96.5)}, levels.DAILY_V1)
    assert daily is not None and daily.entry == D("96.00") and daily.timeframe == "4h"
    intraday, tried = profile_setup({15: (96, 96.5)}, levels.INTRADAY_V1)
    assert intraday is None
    assert ("4h/20 rule A: no held higher low 0.3-3% below price with a stop >=2% away and a "
            "2R cited target") in tried


@pytest.mark.parametrize(("entry_low", "qualifies"), [
    (99.7, True),  # Exactly 0.3% below mid=100: the band's near edge is inclusive.
    (99.71, False),  # 0.29%: too close to price.
    (97, True),  # Exactly 3%: the far edge is inclusive.
    (96.99, False),  # 3.01%: too far for the intraday band.
])
def test_the_intraday_band_edges(entry_low, qualifies):
    setup, _ = profile_setup({15: (entry_low, entry_low + 0.5)}, levels.INTRADAY_V1)
    assert (setup is not None) is qualifies
    if qualifies:
        assert setup.entry == D(str(entry_low)).quantize(INC)


def test_intraday_takes_the_1h_setup_where_daily_takes_the_4h_one():
    # The same qualifying bars (entry 2% below: inside both bands) filed as 1h and 4h: each
    # profile keeps the first timeframe in its own order.
    both = ("1h", "4h")
    intraday, tried = profile_setup({15: (98, 98.5)}, levels.INTRADAY_V1, timeframes=both)
    assert intraday is not None and intraday.timeframe == "1h" and tried == []
    daily, _ = profile_setup({15: (98, 98.5)}, levels.DAILY_V1, timeframes=both)
    assert daily is not None and daily.timeframe == "4h"


def test_intraday_reports_every_combination_in_its_own_order():
    setup, tried = levels.find_setup({}, mid=MID, increment=INC, profile=levels.INTRADAY_V1)
    assert setup is None
    assert tried == [f"{timeframe}/{n}: only 0 completed bars"
                     for _rule in levels.RULES for timeframe in ("1h", "2h", "4h")
                     for n in (20, 24, 30)]


def test_find_setup_without_a_profile_is_daily_v1():
    for overrides in ({}, {15: (99.5, 100)}, {15: (96, 96.5)}, {17: (93, 93.5)}):
        lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
        for index, (lo, hi) in overrides.items():
            lows[index], highs[index] = lo, hi
        series = {"4h": make_series(lows, highs)["4h"], "1h": make_series(lows, highs)["4h"]}
        assert (levels.find_setup(series, mid=MID, increment=INC)
                == levels.find_setup(series, mid=MID, increment=INC, profile=levels.DAILY_V1))


def test_market_data_for_the_daily_profile_covers_intraday_but_not_the_reverse():
    assert levels.DAILY_V1.fetch_covers(levels.INTRADAY_V1)
    assert not levels.INTRADAY_V1.fetch_covers(levels.DAILY_V1)
    assert levels.INTRADAY_V1.fetch_covers(levels.INTRADAY_V1)
    # INTRADAY_V2 reads exactly the daily profile's market data, both ways.
    assert levels.DAILY_V1.fetch_covers(levels.INTRADAY_V2)
    assert levels.INTRADAY_V2.fetch_covers(levels.DAILY_V1)
    assert not levels.INTRADAY_V1.fetch_covers(levels.INTRADAY_V2)


# --- INTRADAY_V2: every daily timeframe, shortest first, entries 0.3%-6% below the mid ----

def test_intraday_v2_is_the_daily_search_shortest_first_with_a_nearer_band_edge():
    profile = levels.INTRADAY_V2
    assert profile.name == "INTRADAY_V2"
    assert profile.timeframes == ("1h", "2h", "4h", "6h", "1d")
    assert set(profile.timeframes) == set(levels.DAILY_V1.timeframes)
    assert (profile.entry_band_min, profile.entry_band_max) == (D("0.003"), D("0.06"))
    assert profile.windows == levels.DAILY_V1.windows
    assert (profile.hourly_lookback_hours, profile.daily_lookback_days) == (
        levels.DAILY_V1.hourly_lookback_hours, levels.DAILY_V1.daily_lookback_days)
    assert profile.band_text == "0.3-6%" and profile.timeframes_text == "1h/2h/4h/6h/1d"


def test_intraday_v2_prefers_a_1h_setup_when_one_qualifies():
    # The same qualifying bars (entry 4% below: inside DAILY_V1's and V2's bands, outside
    # V1's) filed as 1h, 4h and 1d: V2 keeps the 1-hour one, DAILY_V1 its 4-hour one.
    every = ("1h", "4h", "1d")
    v2, tried = profile_setup({15: (96, 96.5)}, levels.INTRADAY_V2, timeframes=every)
    assert v2 is not None and v2.timeframe == "1h" and tried == []
    daily, _ = profile_setup({15: (96, 96.5)}, levels.DAILY_V1, timeframes=every)
    assert daily is not None and daily.timeframe == "4h"
    v1, _ = profile_setup({15: (96, 96.5)}, levels.INTRADAY_V1, timeframes=every)
    assert v1 is None


def test_intraday_v2_takes_a_close_1h_entry_the_daily_profile_cannot():
    # A 1-hour entry 0.5% below price (under DAILY_V1's 0.6% edge) beside a daily-bar one 5%
    # below: V2 keeps the close 1-hour entry; DAILY_V1 can only take the far daily-bar one.
    lows, highs = list(BASE_LOWS), list(BASE_HIGHS)
    lows[15], highs[15] = 99.5, 100
    near = make_series(lows, highs)["4h"]
    lows[15], highs[15] = 95, 95.5
    far = make_series(lows, highs)["4h"]
    series = {"1h": near, "1d": far}
    v2, _ = levels.find_setup(series, mid=MID, increment=INC, profile=levels.INTRADAY_V2)
    assert v2.timeframe == "1h" and v2.entry == D("99.50")
    daily, _ = levels.find_setup(series, mid=MID, increment=INC, profile=levels.DAILY_V1)
    assert daily.timeframe == "1d" and daily.entry == D("95.00")


def _random_series(rng, timeframes):
    """A random walk of 30 bars per timeframe, each ending near 100 (seeded, reproducible)."""
    series = {}
    for timeframe in timeframes:
        price, bars = 100 * (1 + rng.uniform(-0.15, 0.15)), []
        for index in range(30):
            step = rng.gauss(0, 0.02)
            low = min(price, price * (1 + step)) * (1 - abs(rng.gauss(0, 0.01)))
            high = max(price, price * (1 + step)) * (1 + abs(rng.gauss(0, 0.01)))
            bars.append(Bar(started_at=START + timedelta(hours=4 * index),
                            open=D(f"{price:.4f}"), high=D(f"{high:.4f}"),
                            low=D(f"{low:.4f}"), close=D(f"{price * (1 + step):.4f}"),
                            volume=D(10)))
            price *= 1 + step
        series[timeframe] = bars
    return series


def test_intraday_v2_finds_a_setup_for_every_coin_the_daily_profile_finds():
    """The coverage claim, on 400 seeded random markets: same windows, same rules, the same
    timeframes and a band that holds DAILY_V1's, so whatever DAILY_V1 qualifies V2 does too
    (and V1's finds as well). Not vacuous: the daily profile finds setups on many of them."""
    rng = random.Random(20260928)
    counts = {"daily": 0, "v2": 0, "v1": 0, "v2_1h": 0}
    for _ in range(400):
        series = _random_series(rng, levels.DAILY_V1.timeframes)
        mid = D(f"{float(series['1h'][-1].close) * rng.uniform(0.99, 1.08):.4f}")
        found = {name: levels.find_setup(series, mid=mid, increment=D("0.0001"),
                                         profile=profile)[0]
                 for name, profile in (("daily", levels.DAILY_V1), ("v2", levels.INTRADAY_V2),
                                       ("v1", levels.INTRADAY_V1))}
        if found["daily"] is not None or found["v1"] is not None:
            assert found["v2"] is not None
        counts["daily"] += found["daily"] is not None
        counts["v1"] += found["v1"] is not None
        counts["v2"] += found["v2"] is not None
        counts["v2_1h"] += found["v2"] is not None and found["v2"].timeframe == "1h"
    assert counts["daily"] >= 40 and counts["v2"] >= counts["daily"] >= counts["v1"]
    assert counts["v2_1h"] > 0


# --- range_position / change_over (skip-reason helpers) ------------------------------------

def test_range_position_and_change_over():
    lows = [100, 90, 100, 100, 100, 110]
    series = make_series(lows, lows)["4h"]  # highs == lows: an exact [90, 110] range.
    assert levels.range_position(series, mid=D(100)) == D(50)  # Midpoint of [90, 110].
    assert levels.change_over(series, 1, mid=D(121)) == D(10)  # 121 vs close 110: +10%.
    assert levels.change_over(series, 99, mid=D(100)) is None
