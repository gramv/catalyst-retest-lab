"""research_agent.technicals: pure 1-hour-bar measurements on hand-built candles.

Offline only; no network and no clock reads anywhere in this file.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from research_agent import technicals

D = Decimal
T0 = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)


def rows(start, closes, *, volumes=None, first_open=None, highs=None, lows=None):
    """Coinbase rows [time, low, high, open, close, volume], each open the previous close."""
    out, previous = [], first_open if first_open is not None else closes[0]
    for index, close in enumerate(closes):
        high = highs[index] if highs else max(previous, close)
        low = lows[index] if lows else min(previous, close)
        volume = volumes[index] if volumes else 10
        t = int((start + timedelta(hours=index)).timestamp())
        out.append([t, str(low), str(high), str(previous), str(close), str(volume)])
        previous = close
    return out


def bars(start, closes, **kwargs):
    retrieved_at = start + timedelta(hours=len(closes))
    return technicals.complete_hourly_bars(rows(start, closes, **kwargs), retrieved_at=retrieved_at)


def test_complete_hourly_bars_drops_duplicates_and_bars_still_forming():
    raw = rows(T0, [100, 101, 102])
    raw.append(list(raw[1]))  # Returned twice by two overlapping requests.
    found = technicals.complete_hourly_bars(raw, retrieved_at=T0 + timedelta(hours=2, minutes=59))
    assert [bar.close for bar in found] == [D(100), D(101)]  # Hour 3 had not ended.
    assert found[1].volume == D(10)  # The duplicate did not double the volume.


def test_window_move_return_excursions_and_volume():
    window = bars(T0, [100, 96, 104, 110], first_open=100, highs=[101, 100, 105, 112],
                  lows=[99, 95, 96, 104])
    move = technicals.window_move(window)
    assert move.return_pct == D(10)
    assert move.up_excursion_pct == D(12) and move.down_excursion_pct == D(-5)
    assert move.volume == D(40)


def test_half_move_is_the_first_close_past_half_of_the_move():
    window = bars(T0, [100, 100, 104, 106, 110, 110], first_open=100)
    half = technicals.find_half_move(window, reference=D(100), final=D(110))
    assert (half.index, half.basis) == (3, "CLOSE")  # 106 is the first close >= 105.


def test_half_move_of_a_fall_and_of_no_move():
    falling = bars(T0, [100, 99, 94, 90], first_open=100)
    half = technicals.find_half_move(falling, reference=D(100), final=D(90))
    assert half.index == 2  # 94 is the first close <= 95.
    assert technicals.find_half_move(falling, reference=D(100), final=D(100)) is None


def test_half_move_falls_back_to_the_extreme_for_an_exit_inside_a_bar():
    # A stop at 95 filled on a wick: no close reached 97.5, the bar-2 low did.
    window = bars(T0, [100, 99, 99], first_open=100, lows=[99, 98, 95])
    half = technicals.find_half_move(window, reference=D(100), final=D(95))
    assert (half.index, half.basis) == (2, "EXTREME")


def test_move_start_is_the_trough_before_the_high_as_the_app_measures_it():
    """MARKET_REALITY_V1: for a rise, the bar of the lowest low at or before the bar of the
    day's high, the latest such bar on a tie."""
    window = bars(T0, [100, 97, 99, 97, 104, 108, 103], first_open=100,
                  lows=[99, 96, 97, 96, 97, 104, 102], highs=[101, 100, 99, 99, 104, 109, 108])
    start = technicals.find_move_start(window, rising=True)
    assert (start.index, start.basis) == (3, "TROUGH_BEFORE_HIGH")  # 96 at bars 1 and 3.


def test_move_start_of_a_fall_is_the_peak_before_the_low():
    window = bars(T0, [100, 103, 101, 95, 96], first_open=100,
                  highs=[101, 104, 102, 101, 97], lows=[99, 100, 100, 94, 95])
    start = technicals.find_move_start(window, rising=False)
    assert (start.index, start.basis) == (1, "PEAK_BEFORE_LOW")
    assert technicals.find_move_start([], rising=True) is None


def test_volume_ratio_uses_nominal_hours_and_needs_a_traded_baseline():
    day = bars(T0, [100] * 24, volumes=[30] * 24)
    week = bars(T0 - timedelta(days=7), [100] * 168, volumes=[10] * 168)
    assert technicals.volume_ratio(day, 24, week, 168) == D(3)
    silent = bars(T0 - timedelta(days=7), [100] * 168, volumes=[0] * 168)
    assert technicals.volume_ratio(day, 24, silent, 168) is None


def test_technical_state_tags_a_volume_spike_near_the_high_in_an_uptrend():
    # 168 bars rising 100 -> 120 (first open 100); the last 24 trade 4x the earlier volume.
    closes = [100 + D(20) * (index + 1) / 168 for index in range(168)]
    history = bars(T0, closes, first_open=100, volumes=[10] * 144 + [40] * 24)
    state = technicals.technical_state(history, as_of=T0 + timedelta(days=7))
    assert state["bars_7d"] == 168 and state["bars_24h"] == 24
    assert state["return_7d_pct"] == "20.00"
    assert D(state["volume_ratio_24h_vs_7d"]) >= 2
    assert {"VOLUME_SPIKE", "NEAR_7D_HIGH", "TREND_UP_7D"} <= set(state["tags"])
    assert "NEAR_7D_LOW" not in state["tags"] and "TREND_DOWN_7D" not in state["tags"]


def test_technical_state_only_reads_bars_that_ended_by_the_instant():
    history = bars(T0, [100] * 48 + [150])  # The 150 bar starts exactly at as_of.
    state = technicals.technical_state(history, as_of=T0 + timedelta(hours=48))
    assert state["close"] == "100" and state["bars_7d"] == 48


def test_technical_state_without_bars_says_so_and_sets_no_tag():
    state = technicals.technical_state([], as_of=T0)
    assert state["tags"] == [] and state["unavailable"] == "NO_COMPLETED_BARS_BEFORE_THIS_INSTANT"


def test_plain_and_pct_text():
    assert technicals.plain(D("1E-9")) == "0.000000001" and technicals.plain(D("120.0")) == "120"
    assert technicals.pct_text(D("12.345")) == "12.35" and technicals.pct_text(None) is None
