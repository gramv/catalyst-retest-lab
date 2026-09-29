"""Pure shadow-outcome simulation: no database, no network, no broker."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.pick_outcomes import (
    DATA_INCOMPLETE,
    FEE_ASSUMPTION,
    HOLD_EXIT,
    HOLD_HORIZON,
    NEVER_TRIGGERED_EXPIRED,
    NEVER_TRIGGERED_STOP_FIRST,
    SHADOW_METHOD_VERSION,
    STOP,
    TARGET,
    ShadowDataError,
    parse_bars,
    r_values,
    simulate_pick,
    walk_to_exit,
)

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
LEVELS = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}


def bar(minute, o, h, low, c, v="1000"):
    return {"t": (T0 + timedelta(minutes=minute)).isoformat(), "o": o, "h": h, "l": low, "c": c,
            "v": v}


def window(hours=1):
    return T0, T0 + timedelta(hours=hours)


# --- parse_bars -----------------------------------------------------------------------------


def test_parse_bars_sorts_and_validates():
    bars = parse_bars([bar(1, "100", "101", "99", "100.5"), bar(0, "99", "100", "98", "99.5")])
    assert [b.start for b in bars] == [T0, T0 + timedelta(minutes=1)]


def test_parse_bars_rejects_a_malformed_row():
    with pytest.raises(ShadowDataError):
        parse_bars([{"t": T0.isoformat(), "o": "100", "h": "99", "l": "98", "c": "99.5"}])  # h<o


def test_parse_bars_rejects_duplicate_or_unordered_timestamps():
    with pytest.raises(ShadowDataError):
        parse_bars([bar(0, "100", "101", "99", "100"), bar(0, "100", "101", "99", "100")])


def test_parse_bars_rejects_naive_timestamps():
    with pytest.raises(ShadowDataError):
        parse_bars([{"t": "2026-01-01T00:00:00", "o": "1", "h": "1", "l": "1", "c": "1", "v": "0"}])


# --- walk_to_exit (post-trigger) -------------------------------------------------------------


def test_walk_hits_stop():
    bars = parse_bars([bar(0, "100", "100.2", "99.9", "100"), bar(1, "96", "96", "94", "94")])
    walk = walk_to_exit(D("95"), D("111"), bars, hold_deadline=T0 + HOLD_HORIZON)
    assert (walk.reason, walk.price, walk.ambiguous) == (STOP, D("95"), False)


def test_walk_hits_target():
    bars = parse_bars([bar(0, "100", "100.2", "99.9", "100"), bar(1, "110", "112", "109", "111")])
    walk = walk_to_exit(D("95"), D("111"), bars, hold_deadline=T0 + HOLD_HORIZON)
    assert (walk.reason, walk.price) == (TARGET, D("111"))


def test_walk_resolves_a_same_bar_stop_and_target_ambiguity_as_the_stop():
    bars = parse_bars([bar(0, "100", "112", "94", "100")])  # low<=95 and high>=111, one bar
    walk = walk_to_exit(D("95"), D("111"), bars, hold_deadline=T0 + HOLD_HORIZON)
    assert (walk.reason, walk.price, walk.ambiguous) == (STOP, D("95"), True)


def test_walk_exits_at_the_24_hour_hold_deadline_using_the_next_bars_open():
    deadline = T0 + timedelta(hours=24)
    bars = parse_bars([
        bar(0, "100", "100.2", "99.9", "100"),
        {"t": deadline.isoformat(), "o": "103", "h": "103.2", "l": "102.9", "c": "103", "v": "1"},
    ])
    walk = walk_to_exit(D("95"), D("111"), bars, hold_deadline=deadline)
    assert (walk.reason, walk.price, walk.at) == (HOLD_EXIT, D("103"), deadline)


def test_walk_is_incomplete_when_bars_run_out_before_the_hold_deadline():
    bars = parse_bars([bar(0, "100", "100.2", "99.9", "100")])  # nothing else, 24h horizon far off
    walk = walk_to_exit(D("95"), D("111"), bars, hold_deadline=T0 + HOLD_HORIZON)
    assert walk.reason == DATA_INCOMPLETE
    assert walk.price is None and walk.at is None


# --- simulate_pick (trigger + exit) ----------------------------------------------------------


def test_never_triggered_expires_with_validity():
    start, end = window()
    bars = parse_bars([bar(m, "105", "106", "104", "105") for m in range(0, 60, 10)])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == NEVER_TRIGGERED_EXPIRED
    assert sim.triggered is False
    assert sim.gross_r is None and sim.net_r is None


def test_stop_hit_before_any_valid_trigger_invalidates():
    start, end = window()
    bars = parse_bars([bar(0, "105", "106", "94", "105")])  # low reaches the stop first
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == NEVER_TRIGGERED_STOP_FIRST
    assert sim.triggered is False


def test_a_bar_exactly_at_window_end_is_excluded_a_bar_just_before_it_still_triggers():
    start, end = window()
    late = end - timedelta(seconds=1)
    bars = parse_bars([
        {"t": end.isoformat(), "o": "105", "h": "106", "l": "99.99", "c": "105", "v": "1"},
    ])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == NEVER_TRIGGERED_EXPIRED  # the only bar sits at window_end, excluded

    bars_before = parse_bars([
        {"t": late.isoformat(), "o": "105", "h": "106", "l": "99.99", "c": "105", "v": "1"},
        bar(120, "95.5", "95.6", "95.4", "95.5"),  # a later bar so the walk resolves (stop hit)
    ])
    sim2 = simulate_pick(LEVELS, bars_before, window_start=start, window_end=end)
    assert sim2.triggered is True and sim2.trigger_at == late


def test_triggered_then_stop_computes_gross_and_net_r_at_max_entry():
    start, end = window()
    bars = parse_bars([
        bar(0, "100", "100.2", "99.9", "100"),  # touches entry_trigger=100
        bar(1, "94", "94.5", "93", "94"),        # touches stop=95
    ])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == STOP
    assert sim.fill_price == D("100.10")
    gross, net = r_values(D("100.10"), D("95"), D("95"))
    assert sim.gross_r == gross == D("-1")
    assert sim.net_r == net
    assert net < gross  # the fee assumption always costs something


def test_triggered_then_target_hit():
    start, end = window()
    bars = parse_bars([
        bar(0, "100", "100.2", "99.9", "100"),
        bar(1, "110", "112", "109", "111"),
    ])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == TARGET
    assert sim.exit_price == D("111")
    assert sim.gross_r == (D("111") - D("100.10")) / (D("100.10") - D("95"))


def test_triggered_then_24_hour_exit():
    start, end = T0, T0 + timedelta(hours=2)
    deadline = T0 + HOLD_HORIZON
    bars = parse_bars([
        bar(0, "100", "100.2", "99.9", "100"),
        {"t": deadline.isoformat(), "o": "103", "h": "103.2", "l": "102.9", "c": "103", "v": "1"},
    ])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == HOLD_EXIT
    assert sim.exit_at == deadline
    assert sim.data_complete is True


def test_same_bar_ambiguity_is_counted():
    start, end = window()
    bars = parse_bars([
        bar(0, "100", "100.2", "99.9", "100"),
        bar(1, "100", "112", "94", "100"),  # same minute: low<=stop and high>=target
    ])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == STOP
    assert sim.same_bar_ambiguous is True
    assert any("same minute" in note for note in sim.limitations)


def test_data_incomplete_when_bars_end_before_the_hold_deadline():
    start, end = window()
    bars = parse_bars([bar(0, "100", "100.2", "99.9", "100")])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.outcome == DATA_INCOMPLETE
    assert sim.data_complete is False
    assert sim.gross_r is None and sim.net_r is None


def test_invalid_levels_are_rejected():
    with pytest.raises(ShadowDataError):
        simulate_pick(
            {"entry_trigger": "100", "max_entry_price": "99", "stop": "95", "target": "111"},
            [], window_start=T0, window_end=T0 + timedelta(hours=1),
        )  # max_entry below entry_trigger


def test_method_and_fee_assumption_are_labelled_plainly():
    start, end = window()
    bars = parse_bars([bar(m, "105", "106", "104", "105") for m in range(0, 60, 10)])
    sim = simulate_pick(LEVELS, bars, window_start=start, window_end=end)
    assert sim.method == SHADOW_METHOD_VERSION
    assert sim.fee_assumption == FEE_ASSUMPTION
    body = sim.to_dict()
    assert body["method_label"].startswith("1-MINUTE-BAR APPROXIMATION")


# --- r_values --------------------------------------------------------------------------------


def test_r_values_are_decimal_and_fee_only_affects_net():
    gross, net = r_values(D("100.10"), D("95"), D("111"))
    assert isinstance(gross, D) and isinstance(net, D)
    expected_gross = (D("111") - D("100.10")) / (D("100.10") - D("95"))
    assert gross == expected_gross
    fee = D("0.0025") * (D("100.10") + D("111")) / (D("100.10") - D("95"))
    assert net == expected_gross - fee


def test_r_values_rejects_nonpositive_risk():
    with pytest.raises(ShadowDataError):
        r_values(D("95"), D("100"), D("90"))  # stop above entry: not a valid long risk
