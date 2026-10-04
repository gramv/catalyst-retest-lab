"""STRATEGY_SHADOW_V1: the mechanical strategies' shadow signals and outcomes (package
strategy-c1, 2026-10-03).

Fixture evidence only: per-test disposable PostgreSQL databases and canned public bars. No
broker, provider, network or owner-ledger contact; nothing here can place an order.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

from catalyst_lab import strategy_shadow as ss
from catalyst_lab.learning_jobs import run_jobs
from catalyst_lab.scorecard import compute_scorecard
from catalyst_lab.weekly_review import compute_review
from tests.learning_fixtures import FakeBars, learning  # noqa: F401 -- fixture
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401

H = timedelta(hours=1)
M = timedelta(minutes=1)
NOW = datetime(2026, 10, 3, 5, 30, tzinfo=UTC)
UNTIL = NOW.replace(minute=0)  # 05:00: the last completed hour.
FETCH_START = UNTIL - ss.LOOKBACK - 216 * H
OLD_SIGNAL_BAR = datetime(2026, 9, 20, 10, tzinfo=UTC)  # Signal at 11:00 (07:00 New York).
RECENT_SIGNAL_BAR = UNTIL - 2 * H  # Signal at 04:00: its outcome is not knowable yet.


def row(start, o, h, low, c, v="10"):
    return {"t": start.isoformat(), "o": str(o), "h": str(h), "l": str(low), "c": str(c),
            "v": str(v)}


def hours(breakouts):
    """Flat hourly bars from the fetch start to ``UNTIL``; at each ``(bar start, close)`` a
    breakout on 2.5x volume, then flat at its close."""
    rows, price, start = [], D(100), FETCH_START
    marks = dict(breakouts)
    while start < UNTIL:
        if start in marks:
            close = D(marks[start])
            rows.append(row(start, price, close, price, close, "400"))
            price = close
        else:
            rows.append(row(start, price, price, price, price))
        start += H
    return rows


def minutes_to_target(signal_at):
    """A marketable fill at 101 then a climb through the 1.5R target (104.03)."""
    return [row(signal_at + n * M, D(101) + n, D(101) + n + D("0.5"), D(101) + n,
                D(101) + n + D("0.5")) for n in range(6)]


def universe(store, symbols, at=NOW - timedelta(days=1)):
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_STARTED", {
            "cycle_id": str(uuid4()), "report_schema_version": "AGENT_RESEARCH_REPORT_V3",
            "universe": {"source": "LAB_FIXTURE_UNIVERSE", "fetched_at": at.isoformat(),
                         "symbols": sorted(symbols)}}, key="fixture-universe")


def events(store, kind):
    with store.repo.connect() as conn:
        return [r["body"] for r in conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
            (kind,)).fetchall()]


def reader():
    old_at = OLD_SIGNAL_BAR + H
    return FakeBars(
        hours={"AAA/USD": hours([(OLD_SIGNAL_BAR, "101"), (RECENT_SIGNAL_BAR, "102")]),
               "BBB/USD": hours([(OLD_SIGNAL_BAR, "101")]),
               "QQQ/USD": hours([])},
        minutes={"AAA/USD": minutes_to_target(old_at)})  # BBB/USD: no print after its signal.


def test_signals_and_outcomes_are_recorded_once_with_a_40_day_backfill(learning):  # noqa: F811
    store = learning.store
    universe(store, ["AAA/USD", "BBB/USD", "QQQ/USD"])
    bars = reader()
    code, details = ss.run_strategy_shadow(store, bars, now=NOW)
    assert code is None
    assert (details["coins"], details["signals"], details["outcomes"]) == (3, 3, 2)
    signals = events(store, ss.SIGNAL_EVENT)
    assert [(s["symbol"], s["signal_at"]) for s in signals] == [
        ("AAA/USD", (OLD_SIGNAL_BAR + H).isoformat()),
        ("AAA/USD", (RECENT_SIGNAL_BAR + H).isoformat()),
        ("BBB/USD", (OLD_SIGNAL_BAR + H).isoformat())]
    assert all(s["strategy_id"] == "BREAKOUT_7D_VOL2X_V1" and s["strategy_stage"] == "SHADOW"
               and s["orders"] == "NONE_SHADOW_ONLY" for s in signals)
    first = signals[0]["proposal"]
    assert first["facts"]["volume_ratio"] == "2.6250"  # (23 x 10 + 400) x 7 / 1680.
    # The hourly bars asked for cover the 40 days and the 216 hours of history before them.
    assert {(c[0], c[1], c[2], c[3]) for c in bars.calls if c[1] == "1Hour"} == {
        (s, "1Hour", FETCH_START, UNTIL) for s in ("AAA/USD", "BBB/USD", "QQQ/USD")}
    outcomes = {o["symbol"]: o for o in events(store, ss.OUTCOME_EVENT)}
    aaa, bbb = outcomes["AAA/USD"], outcomes["BBB/USD"]
    result = aaa["result"]
    assert (result["outcome"], D(result["fill_price"]), D(result["gross_r"])) == (
        "TARGET", D(101), D("1.5"))
    assert (D(result["plan"]["stop"]), D(result["plan"]["target"])) == (D("98.98"), D("104.03"))
    assert D(aaa["result"]["net_r"]) < D("1.5")  # The assumed taker fee on both legs.
    assert aaa["fee_assumption"]["version"] == "ALPACA_CRYPTO_TIER1_TAKER_BOTH_LEGS_V1"
    assert bbb["result"]["outcome"] == "NOT_FILLED_NO_PRINT"
    # A rerun records nothing new; the recent signal still waits for its window to pass.
    code, again = ss.run_strategy_shadow(store, bars, now=NOW)
    assert code is None and (again["signals"], again["signals_seen"], again["outcomes"]) == (
        0, 3, 0)
    assert len(events(store, ss.SIGNAL_EVENT)) == 3 and len(events(store, ss.OUTCOME_EVENT)) == 2
    # Once its window and hold have passed, the recent signal is simulated (here: no bars yet
    # after it, so no fill), exactly once.
    later = NOW + timedelta(days=2)
    code, last = ss.run_strategy_shadow(store, bars, now=later)
    assert last["outcomes"] == 1 and len(events(store, ss.OUTCOME_EVENT)) == 3


def test_a_failed_read_is_counted_retried_and_never_blocks_the_others(learning):  # noqa: F811
    store = learning.store
    universe(store, ["AAA/USD", "BBB/USD"])
    bars = reader()
    bars.fail = {"BBB/USD"}
    code, details = ss.run_strategy_shadow(store, bars, now=NOW)
    assert code == ss.BARS_UNAVAILABLE and details["signal_fail"] == 1
    assert {s["symbol"] for s in events(store, ss.SIGNAL_EVENT)} == {"AAA/USD"}
    bars.fail = set()
    code, details = ss.run_strategy_shadow(store, bars, now=NOW)
    assert code is None and details["signals"] == 1  # BBB/USD's signal on the retry.


def test_no_universe_means_no_signals(learning):  # noqa: F811
    code, details = ss.run_strategy_shadow(learning.store, reader(), now=NOW)
    assert code is None and details["universe"] == "STRATEGY_SHADOW_UNIVERSE_UNAVAILABLE"
    assert events(learning.store, ss.SIGNAL_EVENT) == []


def test_the_nightly_shadow_step_runs_the_strategies_after_the_picks(learning):  # noqa: F811
    universe(learning.store, ["AAA/USD", "BBB/USD"])
    results = {r.name: r for r in run_jobs(learning.store, reader(), now=NOW)}
    step = results["shadow_outcomes"]
    assert step.result == "OK", (step.code, step.details)
    assert (step.details["recorded"], step.details["strat_signals"],
            step.details["strat_outcomes"]) == (0, 3, 2)
    again = {r.name: r for r in run_jobs(learning.store, reader(), now=NOW)}
    assert again["shadow_outcomes"].details["strat_signals_seen"] == 3


def test_the_scorecard_and_the_weekly_review_report_shadow_and_paper_apart(learning):  # noqa: F811
    store = learning.store
    universe(store, ["AAA/USD", "BBB/USD"])
    ss.run_strategy_shadow(store, reader(), now=NOW)
    body = compute_scorecard(store.repo, date(2026, 9, 20), now=NOW)
    section = body["windows"]["1d"]["overall"]["strategies"]
    assert section["registry_version"] == "STRATEGY_REGISTRY_V1"
    assert section["labels"]["shadow"].startswith("SHADOW:")
    assert section["labels"]["live_paper"].startswith("LIVE_PAPER:")
    cell = section["shadow"]["BREAKOUT_7D_VOL2X_V1"]
    assert (cell["label"], cell["signals"], cell["outcomes"], cell["trades"]) == (
        "SHADOW", 2, 2, 1)
    assert cell["outcome_counts"] == {"NOT_FILLED_NO_PRINT": 1, "TARGET": 1}
    assert D(cell["mean_gross_r"]) == D("1.5") and cell["status"] == "NOT_ENOUGH_DATA"
    assert section["live_paper"] == {}  # No paper trades closed that day.
    assert section["ladder"]["BREAKOUT_7D_VOL2X_V1"] == {
        "stage": "SHADOW", "sources": ["MECHANICAL"], "shadow_trades": 1,
        "shadow_minimum": 30, "shadow_minimum_met": False}
    assert section["ladder"]["PULLBACK_V1"]["stage"] == "LIVE_PAPER"
    assert body["windows"]["1d"]["overall"]["dimensions"]["by_strategy"] == {}
    # A day without signals has an empty shadow section.
    quiet = compute_scorecard(store.repo, date(2026, 9, 10), now=NOW)
    assert quiet["windows"]["1d"]["overall"]["strategies"]["shadow"] == {}
    review = compute_review(store.repo, date(2026, 9, 27), now=NOW)
    weekly = review["strategies"]["shadow"]["BREAKOUT_7D_VOL2X_V1"]
    assert (weekly["signals"], weekly["trades"], weekly["pending"]) == (2, 1, 0)
    assert review["dimensions"]["by_strategy"] == {}
