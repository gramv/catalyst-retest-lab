"""MARKET_REALITY_V1: the New York day's movers and the forward-window outlook grades.

Fixture evidence only: canned bars (no network), per-test disposable PostgreSQL databases.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.market_reality import (
    RealityUnavailable,
    actual_direction,
    bucket_of,
    grade_outlook,
    measure_day,
    mover_calls,
    record_day,
    select_movers,
    window_stats,
)
from catalyst_lab.pick_outcomes import parse_bars
from tests.learning_fixtures import (
    NOW,
    FakeBars,
    bearer,
    coin,
    hour_rows,
    learning,  # noqa: F401 -- fixture
    minute_rows,
    outlook,
    skipped,
)
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_research_report_v3 import AGENTS

T0 = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)  # 00:00 on 2026-09-27 in New York.


def bar(at, o, h, low, c):
    return {"t": at.isoformat(), "o": str(o), "h": str(h), "l": str(low), "c": str(c), "v": "1"}


# --- Pure measures ------------------------------------------------------------------------------


def test_a_rising_day_starts_its_move_at_the_last_low_before_the_high():
    bars = parse_bars(minute_rows(T0, [100, 99, 98, 101, 105, 104]))
    stats = window_stats(bars, T0, T0 + timedelta(days=1))
    assert (stats.return_pct, stats.max_up_pct, stats.max_down_pct) == (D(4), D(5), D(-2))
    # Bars 2 and 3 both reach the low of 98: the move starts at the later one.
    assert stats.move_start_at == T0 + timedelta(minutes=3)
    assert stats.move_extreme_at == T0 + timedelta(minutes=4)
    assert stats.bar_count == 6 and stats.first_bar_at == T0


def test_a_falling_day_starts_its_move_at_the_last_high_before_the_low():
    bars = parse_bars(minute_rows(T0, [100, 101, 102, 97, 95, 96]))
    stats = window_stats(bars, T0, T0 + timedelta(days=1))
    assert stats.return_pct == D(-4)
    assert stats.move_start_at == T0 + timedelta(minutes=3)  # Highs of 102 at bars 2 and 3.
    assert window_stats(bars, T0 + timedelta(days=1), T0 + timedelta(days=2)) is None


def test_the_window_excludes_bars_before_its_start():
    bars = parse_bars(minute_rows(T0, [50, 100, 101, 102]))
    stats = window_stats(bars, T0 + timedelta(minutes=1), T0 + timedelta(minutes=10))
    # The first bar inside opens at 50 (the previous close) in this fixture's construction.
    assert stats.open == D(50) and stats.bar_count == 3
    later = window_stats(bars, T0 + timedelta(minutes=2), T0 + timedelta(minutes=10))
    assert later.open == D(100) and later.return_pct == D(2)


@pytest.mark.parametrize(("value", "direction"), [
    ("1.4999", "FLAT"), ("-1.4999", "FLAT"), ("1.5", "UP"), ("-1.5", "DOWN"), ("0", "FLAT"),
])
def test_actual_direction_is_flat_under_one_and_a_half_percent(value, direction):
    assert actual_direction(D(value)) == direction


def test_calibration_buckets_are_half_open_except_the_last():
    assert [bucket_of(D(v)) for v in ("0", "0.2", "0.5999", "0.6", "0.8", "1")] == [
        "0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0", "0.8-1.0"]


def test_movers_are_the_top_five_each_side_and_every_big_move():
    returns = {"A": 8, "B": 3, "C": 2, "D": 1, "E": "0.5", "F": "0.2", "G": 0, "H": "-0.1",
               "I": -6, "J": -1, "K": -2, "L": -3}
    measured = []
    for symbol, value in returns.items():
        close = D(100) + D(str(value))
        [stat_bar] = parse_bars([bar(T0, 100, max(100, close), min(100, close), close)])
        measured.append((symbol + "/USD", window_stats([stat_bar], T0, T0 + timedelta(1))))
    movers = select_movers(measured)
    assert [m["symbol"][0] for m in movers] == list("ABCDEHJKLI")
    by = {m["symbol"][0]: m for m in movers}
    assert by["A"]["top_up"] and by["A"]["big_move"] and not by["B"]["big_move"]
    assert by["I"]["top_down"] and by["I"]["big_move"] and by["H"]["top_down"]
    assert "F/USD" not in {m["symbol"] for m in movers}  # Sixth riser, under 5%.


def graded_body(received):
    body = outlook(coins=[
        coin("BTC/USD", "UP", "0.7"), coin("ETH/USD", "FLAT", "0.5"),
        coin("SOL/USD", "DOWN", "0.65"), skipped("DOGE/USD"),
        coin("XRP/USD", "UP", "0.9"), coin("ADA/USD", "UP", "0.3"),
    ])
    body.update(received_at=received.isoformat(), window_start=received.isoformat(),
                window_end=(received + timedelta(hours=24)).isoformat(),
                grading_day="2026-09-27")
    return {"event_seq": 41, "body": body}


def forward_bars(received, final, *, before=None):
    """Bars inside the window from 100 to ``final``; ``before`` adds a bar before the window."""
    rows = [bar(received + timedelta(minutes=1), 100, max(100, final), min(100, final), 100),
            bar(received + timedelta(hours=20), 100, max(100, final), min(100, final), final)]
    if before is not None:
        rows.insert(0, bar(received - timedelta(minutes=5), before, before, before, before))
    return parse_bars(rows)


def test_an_outlook_is_graded_only_on_its_forward_window():
    received = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)
    bars = {
        "BTC/USD": forward_bars(received, D(102), before=D(50)),  # The earlier crash is unseen.
        "ETH/USD": forward_bars(received, D(106)),
        "SOL/USD": forward_bars(received, D("100.5")),
        "DOGE/USD": forward_bars(received, D(93)),
        "XRP/USD": forward_bars(received, D("94.5")),
    }
    grade = grade_outlook(graded_body(received), bars)
    assert (grade["compared"], grade["hits"], grade["hit_rate"]) == (4, 1, "0.2500")
    assert (grade["skipped"], grade["unmeasured"]) == (1, 1)
    assert grade["outlook_event_seq"] == 41 and grade["window_start"] == received.isoformat()
    assert [m["symbol"] for m in grade["misses"]] == ["ETH/USD", "DOGE/USD", "XRP/USD"]
    assert [m["outlook_direction"] for m in grade["misses"]] == ["FLAT", "SKIPPED", "UP"]
    assert [f["symbol"] for f in grade["false_alarms"]] == ["SOL/USD", "XRP/USD"]
    calibration = {row["bucket"]: row for row in grade["calibration"]}
    assert calibration["0.6-0.8"] == {"bucket": "0.6-0.8", "count": 2, "hits": 1,
                                      "hit_rate": "0.5000", "mean_confidence": "0.6750",
                                      "confidence_sum": "1.35"}
    assert calibration["0.8-1.0"]["count"] == 1 and calibration["0.0-0.2"]["count"] == 0
    assert calibration["0.0-0.2"]["hit_rate"] is None
    btc = next(c for c in grade["coins"] if c["symbol"] == "BTC/USD")
    assert (btc["return_pct"], btc["actual"], btc["hit"]) == ("2.0000", "UP", True)
    assert grade["market_calls"]["btc"]["hit"] is True
    assert grade["market_calls"]["eth"] == {
        "symbol": "ETH/USD", "direction": "FLAT", "confidence": "0.5", "return_pct": "6.0000",
        "actual": "UP", "hit": False}


def test_a_mover_counts_against_the_latest_outlook_covering_its_start():
    day_start = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
    start = day_start + timedelta(hours=7)  # 07:00 in New York.
    big = {"symbol": "SOL/USD", "return_pct": "7.0000", "move_start_at": start.isoformat(),
           "big_move": True}
    small = {**big, "return_pct": "3.0000", "big_move": False}

    def seen(hours_before, direction, seq):
        return {"event_seq": seq, "outlook_id": f"o{seq}",
                "received_at": start - timedelta(hours=hours_before),
                "directions": {"SOL/USD": direction}}

    outlooks = {
        "claude": [seen(23, "FLAT", 1), seen(-1, "UP", 2)],  # The later one came after the move.
        "grogbot": [seen(4, "UP", 3)],
        "instinct": [seen(25, "UP", 4)],  # Its window ended before the move started.
        "muse": [seen(2, "DOWN", 5), seen(1, "SKIPPED", 6)],
    }
    calls, missed = mover_calls(big, outlooks)
    assert calls["claude"] == {"outlook_id": "o1", "direction": "FLAT"}
    assert calls["instinct"] is None and calls["muse"]["direction"] == "SKIPPED"
    assert missed == ["claude", "instinct", "muse"]
    assert mover_calls(small, outlooks)[1] == []  # Under 5%: a mover, never a miss.


# --- The recorded day ---------------------------------------------------------------------------


def universe_bars(received, day):
    """Minute bars over the outlook's window and the day, hourly volume over the lookback."""
    day_start, day_end = day_bounds(day)
    minutes = {
        "BTC/USD": minute_rows(received, [100, 101, 102]) + minute_rows(day_start, [102, 104]),
        "ETH/USD": minute_rows(received, [100, 98]) + minute_rows(day_start, [98, 90, 91]),
        "SOL/USD": minute_rows(received, [100, 107]) + minute_rows(day_start, [107, 108]),
        "DOGE/USD": [],  # No trades at all: unmeasured, never guessed.
    }
    lookback, _ = day_bounds(day - timedelta(days=7))
    hours = {symbol: hour_rows(lookback, 7 * 24, volume="10") + hour_rows(day_start, 24,
                                                                           volume="20")
             for symbol in minutes}
    return FakeBars(minutes=minutes, hours=hours)


def test_the_day_is_recorded_once_with_movers_factors_and_grades(learning):  # noqa: F811
    reply = learning.client.post("/api/v1/lab/market-outlooks", json=outlook(),
                                 headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202 and reply.json()["grading_day"] == "2026-09-27"
    day = date(2026, 9, 27)
    now = datetime(2026, 9, 28, 5, 30, tzinfo=UTC)  # The cron: 01:30 in New York.
    reader = universe_bars(NOW, day)
    status, seq = record_day(learning.store, reader, day, now=now)
    assert status == "RECORDED"
    assert record_day(learning.store, reader, day, now=now) == ("ALREADY_RECORDED", seq)
    with learning.store.repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.managed_events WHERE event_seq=%s",
                           (seq,)).fetchone()
    assert row["kind"] == "MARKET_REALITY" and row["setup_id"] is None
    assert row["idempotency_key"] == "market-reality:2026-09-27"
    body = row["body"]
    assert body["reality_version"] == "MARKET_REALITY_V1" and body["day"] == "2026-09-27"
    assert body["regime"] == {"regime_version": "MARKET_REGIME_V1", "status": "NOT_RECORDED"}
    assert body["universe"]["symbols"] == ["BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD"]
    assert body["universe"]["recorded_by"] == "MARKET_OUTLOOK"
    assert body["measured_count"] == 3 and body["unmeasured"] == ["DOGE/USD"]
    coins = {c["symbol"]: c for c in body["coins"]}
    assert coins["DOGE/USD"]["status"] == "NO_BARS"
    assert coins["ETH/USD"]["return_pct"] == "-7.1429"  # 98 to 91.
    assert coins["BTC/USD"]["volume_vs_7d_avg"] == "2.0000"  # 20 an hour against 10.
    assert coins["BTC/USD"]["volume_usd"] == "48000.00"
    assert [m["symbol"] for m in body["movers"]] == ["BTC/USD", "SOL/USD", "ETH/USD"]
    eth = body["movers"][2]
    assert eth["big_move"] and eth["missed_by"] == ["claude"]  # FLAT, then a -7% move.
    assert eth["calls"]["claude"]["direction"] == "FLAT"
    assert body["outlook_agents"] == ["claude"]
    assert body["factors"]["btc_return_pct"] == "1.9608" and body["factors"]["eth"]
    assert body["factors"]["total_volume_vs_7d_avg"] == "2.0000"
    [grade] = body["grades"]
    assert grade["agent_id"] == "claude" and grade["grading_day"] == "2026-09-27"
    # The window: 08:30 on the 26th for 24 hours. ETH fell 9% in it (called FLAT) and SOL rose
    # 8% (called DOWN): two misses; SOL is also a confident false alarm.
    assert grade["window_start"] == NOW.isoformat()
    assert [m["symbol"] for m in grade["misses"]] == ["ETH/USD", "SOL/USD"]
    assert [m["symbol"] for m in grade["false_alarms"]] == ["SOL/USD"]
    assert (grade["skipped"], grade["compared"], grade["hits"]) == (1, 3, 1)


def test_a_day_is_not_recorded_early_partially_or_without_a_universe(learning):  # noqa: F811
    day = date(2026, 9, 27)
    _, day_end = day_bounds(day)
    with pytest.raises(RealityUnavailable, match="REALITY_UNIVERSE_UNAVAILABLE"):
        measure_day(learning.store.repo, FakeBars(), day, now=day_end + timedelta(hours=1))
    reply = learning.client.post("/api/v1/lab/market-outlooks", json=outlook(),
                                 headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202
    with pytest.raises(RealityUnavailable, match="REALITY_DAY_NOT_OVER"):
        measure_day(learning.store.repo, FakeBars(), day, now=day_end + timedelta(minutes=4))
    failing = universe_bars(NOW, day)
    failing.fail = {"SOL/USD"}
    with pytest.raises(RealityUnavailable, match="REALITY_BARS_UNAVAILABLE"):
        record_day(learning.store, failing, day, now=day_end + timedelta(hours=1))
    with learning.store.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_events "
                            "WHERE kind='MARKET_REALITY'").fetchone()["n"] == 0
