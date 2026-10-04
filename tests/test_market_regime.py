"""MARKET_REGIME_V1: the day's regime tag and each trade's at-entry tag (package
learning-measure, 2026-10-02).

Fixture evidence only: canned 1-hour bars (no network), per-test disposable PostgreSQL databases,
the fixture paper venue. No broker, Jev or owner ledger.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.market import NY
from catalyst_lab.market_regime import (
    BTC,
    REGIME_EVENT,
    TRADE_REGIME_EVENT,
    RegimeUnavailable,
    alt_breadth,
    btc_trend,
    btc_volatility,
    bucket,
    daily_closes,
    day_regime,
    hour_index,
    median,
    record_regimes,
    recorded_trade_regimes,
    selloff,
    trade_regime,
    vol_bucket,
)
from catalyst_lab.pick_outcomes import Bar
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from tests.learning_fixtures import FakeBars, close_attributed
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401

DAY = date(2026, 10, 1)
ALTS = ("ADA/USD", "DOGE/USD", "ETH/USD", "LINK/USD", "SOL/USD", "UNI/USD")


def bar(at, open_, close):
    open_, close = D(str(open_)), D(str(close))
    return Bar(start=at, open=open_, high=max(open_, close), low=min(open_, close), close=close,
               volume=D(1))


def daily(closes, *, last=DAY):
    """One bar per New York day at 16:00, the last on ``last``; ``closes`` oldest first."""
    days = [last - timedelta(days=n) for n in range(len(closes) - 1, -1, -1)]
    return [bar(day_bounds(d)[0] + timedelta(hours=16), c, c) for d, c in zip(days, closes,
                                                                                strict=True)]


# --- Pure measures -------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "label"), [
    ("-2", "DOWN_2+"), ("-1.9999", "DOWN"), ("-0.5", "DOWN"), ("-0.4999", "FLAT"), ("0", "FLAT"),
    ("0.4999", "FLAT"), ("0.5", "UP"), ("1.9999", "UP"), ("2", "UP_2+"), (None, "UNKNOWN"),
])
def test_one_hour_buckets_and_their_edges(value, label):
    assert bucket(None if value is None else D(value), (D("0.5"), D("2"))) == label


@pytest.mark.parametrize(("value", "label"), [
    ("-4", "DOWN_4+"), ("-1", "DOWN"), ("-0.99", "FLAT"), ("1", "UP"), ("4", "UP_4+"),
])
def test_four_hour_buckets_and_their_edges(value, label):
    assert bucket(D(value), (D("1"), D("4"))) == label


def test_median_of_odd_and_even_counts():
    assert median([D(3), D(1), D(2)]) == D(2)
    assert median([D(4), D(1), D(2), D(3)]) == D("2.5")
    assert median([]) is None


def test_daily_close_is_the_last_bar_of_the_new_york_day():
    start, end = day_bounds(DAY)
    closes = daily_closes([bar(start, 1, 2), bar(end - timedelta(hours=1), 2, 3),
                           bar(end, 3, 9)])
    assert closes == {DAY: D(3), DAY + timedelta(days=1): D(9)}


def test_btc_trend_up_down_mixed_and_unknown():
    rising = [100 + n for n in range(50)]
    assert btc_trend(daily_closes(daily(rising)), DAY)["label"] == "UP"
    assert btc_trend(daily_closes(daily(rising[::-1])), DAY)["label"] == "DOWN"
    # Up for 45 days, then a dip below the 20-day mean that stays above the 50-day one.
    mixed = [100 + n for n in range(45)] + [140, 139, 138, 137, 134]
    trend = btc_trend(daily_closes(daily(mixed)), DAY)
    assert trend["label"] == "MIXED"
    assert D(trend["sma_50"]) < D("134") < D(trend["sma_20"])
    assert btc_trend(daily_closes(daily(rising[1:])), DAY)["label"] == "UNKNOWN"  # 49 closes


def test_volatility_buckets_use_the_mid_rank_with_exact_thirds():
    ranked = [D(n) for n in range(1, 61)]  # 60 values
    assert vol_bucket(D(20), ranked)[0] == "LOW"  # (19 + 0.5) / 60 = 0.325
    assert vol_bucket(D(21), ranked)[0] == "NORMAL"  # 0.3417
    assert vol_bucket(D(40), ranked)[0] == "NORMAL"  # 0.6583
    assert vol_bucket(D(41), ranked)[0] == "HIGH"  # 0.675
    flat = [D(1)] * 60
    assert vol_bucket(D(1), flat) == ("NORMAL", D("0.5"))  # Ties do not make a calm day HIGH.
    # Exactly one third: 2 * 20 = 40 of 2 * 60 -> LOW; exactly two thirds of 3 -> NORMAL.
    assert vol_bucket(D("20.5"), ranked)[0] == "LOW"
    assert vol_bucket(D(2), [D(1), D(2), D(3)])[0] == "NORMAL"


def test_btc_volatility_from_closes():
    calm = [D(100) + (n % 2) for n in range(90)]  # +-1% swings
    wild = [D(100) + 10 * (n % 2) for n in range(21)]  # +-10% swings
    closes = daily_closes(daily(calm + wild))
    result = btc_volatility(closes, DAY)
    assert result["label"] == "HIGH" and result["ranked_days"] == 90
    calm_end = daily_closes(daily(wild * 4 + calm[:26]))
    assert btc_volatility(calm_end, DAY)["label"] == "LOW"
    short = daily_closes(daily(calm[:79]))  # 79 closes: 59 ranked days
    assert btc_volatility(short, DAY)["label"] == "UNKNOWN"


def test_alt_breadth_counts_coins_above_their_20_day_mean():
    up, down = [D(100 + n) for n in range(20)], [D(120 - n) for n in range(20)]
    closes = {s: daily_closes(daily(up if i < 3 else down)) for i, s in enumerate(ALTS)}
    closes[BTC] = daily_closes(daily(down))  # Bitcoin is not an alt.
    result = alt_breadth(closes, DAY, (BTC, *ALTS))
    assert (result["label"], result["above"], result["measured"]) == ("BROAD", 3, 6)
    closes["ADA/USD"] = daily_closes(daily(up[1:]))  # 19 closes: unmeasured
    result = alt_breadth(closes, DAY, (BTC, *ALTS))
    assert (result["label"], result["share"], result["unmeasured"]) == (
        "NARROW", D("0.4000"), ["ADA/USD"])
    assert alt_breadth({}, DAY, ALTS)["label"] == "UNKNOWN"


def hourly(day, returns_by_hour, *, coins=ALTS, base=D(100)):
    """For each hour of ``day`` a bar per coin returning ``returns_by_hour[h]`` percent."""
    start, end = day_bounds(day)
    result = {coin: [] for coin in coins}
    at, h = start, 0
    while at < end:
        pct = D(str(returns_by_hour.get(h, "0.1")))
        for coin in coins:
            result[coin].append(bar(at, base, base * (1 + pct / 100)))
        at += timedelta(hours=1)
        h += 1
    return result


def test_selloff_at_minus_two_percent_and_the_minimum_hours():
    indexed = hour_index(hourly(DAY, {5: "-2"}))
    result = selloff(indexed, ALTS, DAY)
    assert result["label"] == "SELLOFF" and result["worst_hour_median_return_pct"] == D("-2")
    assert result["worst_hour_start"] == day_bounds(DAY)[0] + timedelta(hours=5)
    assert selloff(hour_index(hourly(DAY, {5: "-1.9999"})), ALTS, DAY)["label"] == "NO_SELLOFF"
    four = hour_index(hourly(DAY, {5: "-3"}, coins=ALTS[:4]))  # 4 coins: no median
    assert selloff(four, ALTS, DAY)["label"] == "UNKNOWN"
    start, _ = day_bounds(DAY)
    eleven = {c: [b for b in bars if b.start < start + timedelta(hours=11)]
              for c, bars in hourly(DAY, {}).items()}
    assert selloff(hour_index(eleven), ALTS, DAY)["hours_measured"] == 11
    assert selloff(hour_index(eleven), ALTS, DAY)["label"] == "UNKNOWN"


def test_day_regime_tag_joins_the_four_parts():
    btc = daily([100 + n for n in range(110)])
    coins = hourly(DAY, {3: "-2.5"}, base=D(130))
    for coin in ALTS:
        coins[coin] = daily([100 + n for n in range(19)], last=DAY - timedelta(days=1)) + \
            coins[coin]
    regime = day_regime(DAY, btc, coins, (BTC, *ALTS))
    parts = regime["tag"].split("/")
    assert (parts[0], parts[2], parts[3]) == ("UP", "BROAD", "SELLOFF")
    assert parts[1] == regime["btc_volatility"]["label"]
    assert regime["btc_trend"]["label"] == "UP" and regime["alt_breadth"]["label"] == "BROAD"


def test_trade_regime_uses_the_last_completed_hour():
    start, _ = day_bounds(DAY)
    btc = [bar(start + timedelta(hours=h), 100, 100 + h) for h in range(8)]
    coins = hourly(DAY, {6: "-2.5"})
    entry = start + timedelta(hours=7, minutes=30)  # The 06:00 bar is the last completed one.
    result = trade_regime(entry, btc, coins, ALTS)
    assert result["reference_hour_start"] == start + timedelta(hours=6)
    assert result["btc_1h_return_pct"] == D("6.0000") and result["btc_1h"] == "UP_2+"
    assert result["btc_4h"] == "UP_4+"  # 100 (03:00 open) to 106
    assert (result["median_coin_1h"], result["median_coin_count"]) == ("DOWN_2+", 6)
    on_the_hour = trade_regime(start + timedelta(hours=7), btc, coins, ALTS)
    assert on_the_hour["reference_hour_start"] == start + timedelta(hours=6)
    early = trade_regime(start + timedelta(hours=2, minutes=1), btc, coins, ALTS)
    assert early["btc_4h"] == "UNKNOWN" and early["btc_1h"] == "UP"  # 01:00 bar: 100 to 101


# --- The ledger: record, backfill, idempotency ----------------------------------------------------


def record_universe(store, at, symbols):
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_STARTED", {
            "cycle_id": f"fixture-universe-{at.isoformat()}",
            "report_schema_version": REPORT_SCHEMA_V3,
            "universe": {"symbols": list(symbols), "fetched_at": at.isoformat(),
                         "source": "LAB_FIXTURE_UNIVERSE"}},
            key=f"fixture-universe:{at.isoformat()}")


def hour_rows(symbol_start, end, *, slope="0.001"):
    rows, at, price = [], symbol_start, D(100)
    while at < end:
        close = price * (1 + D(slope))
        rows.append({"t": at.isoformat(), "o": str(price), "h": str(max(price, close)),
                     "l": str(min(price, close)), "c": str(close), "v": "1"})
        price, at = close, at + timedelta(hours=1)
    return rows


def regime_events(store, kind):
    with store.repo.connect() as conn:
        return conn.execute("SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (kind,)).fetchall()


def test_record_regimes_backfills_days_and_trades_once(mx):  # noqa: F811
    engine, venue, _ = mx
    sid, _raw = close_attributed(mx, "BTC/USD")
    with engine.repo.connect() as conn:
        entry_at = conn.execute("SELECT min(filled_at) AS at FROM lab.managed_fills WHERE "
                                "setup_id=%s AND side='buy'", (sid,)).fetchone()["at"]
    entry_day = entry_at.astimezone(NY).date()
    # The universe is recorded inside the entry's own New York day, so the previous day never
    # becomes measurable (just after New York midnight, ``entry_at - 1 h`` fell on the day
    # before and that day was recorded too; jev-b3, 2026-10-03).
    record_universe(engine.store, max(entry_at - timedelta(hours=1), day_bounds(entry_day)[0]),
                    (BTC, *ALTS))
    start = day_bounds(entry_day - timedelta(days=120))[0]
    end = day_bounds(entry_day + timedelta(days=5))[1]
    reader = FakeBars(hours={s: hour_rows(start, end) for s in (BTC, *ALTS)})
    now = day_bounds(entry_day)[1] + timedelta(hours=2)
    details = record_regimes(engine.store, reader, now=now)
    assert details["days"] == {entry_day.isoformat(): "RECORDED"}
    assert details["trades"] == {"RECORDED": 1, "ALREADY_RECORDED": 0}
    [day] = regime_events(engine.store, REGIME_EVENT)
    assert day["body"]["regime_version"] == "MARKET_REGIME_V1"
    tag_parts = day["body"]["tag"].split("/")
    assert (tag_parts[0], tag_parts[2], tag_parts[3]) == ("UP", "BROAD", "NO_SELLOFF")
    assert day["body"]["universe"]["count"] == 7
    tag = recorded_trade_regimes(engine.repo)[str(sid)]
    assert tag["day_tag"] == day["body"]["tag"] and tag["prior_day_tag"] is None
    assert tag["btc_1h"] == "FLAT" and tag["median_coin_1h"] == "FLAT"  # +0.1% an hour
    assert tag["btc_4h"] == "FLAT" and tag["entry_at"] == entry_at.isoformat()
    # A rerun records nothing; three nights later the missed days are caught up, the trade
    # is never tagged twice and no recorded event changes.
    again = record_regimes(engine.store, reader, now=now)
    assert again["days"] == {} and again["days_already_recorded"] == 1
    assert again["trades"] == {"RECORDED": 0, "ALREADY_RECORDED": 0}
    later = record_regimes(engine.store, reader, now=now + timedelta(days=3))
    assert sorted(later["days"].values()) == ["RECORDED"] * 3
    assert len(regime_events(engine.store, REGIME_EVENT)) == 4
    assert len(regime_events(engine.store, TRADE_REGIME_EVENT)) == 1
    assert regime_events(engine.store, REGIME_EVENT)[0]["body"] == day["body"]


def test_a_failed_bar_read_records_nothing(mx):  # noqa: F811
    engine, venue, _ = mx
    sid, _raw = close_attributed(mx, "BTC/USD")
    at = venue.now
    record_universe(engine.store, at - timedelta(hours=1), (BTC, *ALTS))
    reader = FakeBars(hours={}, fail={"SOL/USD"})
    now = day_bounds(at.astimezone(NY).date())[1] + timedelta(hours=2)
    with pytest.raises(RegimeUnavailable, match="REGIME_BARS_UNAVAILABLE"):
        record_regimes(engine.store, reader, now=now)
    assert regime_events(engine.store, REGIME_EVENT) == []
    assert regime_events(engine.store, TRADE_REGIME_EVENT) == []


def test_days_before_the_first_universe_are_not_measured(mx):  # noqa: F811
    engine, venue, _ = mx
    reader = FakeBars()
    now = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
    assert record_regimes(engine.store, reader, now=now) == {
        "days": {}, "trades": {"RECORDED": 0, "ALREADY_RECORDED": 0},
        "days_already_recorded": 0}
    assert reader.calls == []
    # Bars that are empty leave every part UNKNOWN; the day is still recorded once.
    record_universe(engine.store, now - timedelta(days=2), (BTC, *ALTS))
    details = record_regimes(engine.store, reader, now=now)
    assert list(details["days"].values()) == ["RECORDED", "RECORDED"]
    tags = {r["body"]["tag"] for r in regime_events(engine.store, REGIME_EVENT)}
    assert tags == {"UNKNOWN/UNKNOWN/UNKNOWN/UNKNOWN"}
