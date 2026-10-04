"""research_agent.evidence: the facts behind a pick (method v8, 2026-10-02). Pure, offline."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from research_agent import evidence

NOW = datetime(2026, 10, 2, 23, 27, tzinfo=UTC)


def daily_rows(closes, *, end=NOW):
    """Coinbase daily candle rows, newest first, the last one still forming at ``end``."""
    day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for index, close in enumerate(reversed(closes)):
        start = day - timedelta(days=index)
        rows.append([int(start.timestamp()), close - 1, close + 1, close, close, 10])
    return rows


def hourly_rows(closes, *, end=NOW):
    """Coinbase hourly candle rows, newest first; the newest starts in the current hour."""
    hour = end.replace(minute=0, second=0, microsecond=0)
    rows = []
    for index, close in enumerate(reversed(closes)):
        start = hour - timedelta(hours=index)
        rows.append([int(start.timestamp()), close - 0.5, close + 0.5, close, close, 10])
    return rows


def test_trend_is_the_mid_against_the_average_of_twenty_completed_daily_closes():
    closes = [100] * 25 + [110]  # Today's (forming) bar is excluded by daily_bars.
    raw = {"candles_1d": daily_rows(closes)}
    assert evidence.trend_vs_average(raw, "105", retrieved_at=NOW) == D("0.05")
    assert evidence.trend_vs_average(raw, "95", retrieved_at=NOW) == D("-0.05")
    # Fewer than 20 completed days, no candles, or no usable mid: None.
    assert evidence.trend_vs_average({"candles_1d": daily_rows([100] * 15)}, "105",
                                     retrieved_at=NOW) is None
    assert evidence.trend_vs_average({}, "105", retrieved_at=NOW) is None
    assert evidence.trend_vs_average(raw, None, retrieved_at=NOW) is None
    assert evidence.trend_vs_average(raw, "0", retrieved_at=NOW) is None
    assert evidence.trend_vs_average({"candles_1d": [[1, 2]]}, "105", retrieved_at=NOW) is None


def test_two_hour_move_uses_the_ticker_against_the_close_two_hours_back():
    # Oldest first: ..., the 20:00 bar closes 100, 21:00 closes 104, 22:00 closes 102 (the
    # last completed one at 23:27), and the 23:00 bar (101) is still forming.
    raw = {"candles_1h": hourly_rows([100, 100, 100, 104, 102, 101]), "ticker": {"price": "103"}}
    # The latest completed bar that started at or before 21:27 is the 21:00 bar: close 104.
    assert evidence.two_hour_move(raw, retrieved_at=NOW) == D("103") / D("104") - 1
    # Without a ticker price, the last completed close (102) stands in for the latest price.
    assert evidence.two_hour_move({"candles_1h": raw["candles_1h"]}, retrieved_at=NOW) == (
        D("102") / D("104") - 1)
    assert evidence.two_hour_move({"candles_1h": [[1, 2]]}, retrieved_at=NOW) is None
    assert evidence.two_hour_move({"candles_1h": hourly_rows([100])}, retrieved_at=NOW) is None
    assert evidence.two_hour_move({}, retrieved_at=NOW) is None


def test_market_state_is_the_median_of_every_coin_but_btc_plus_btc_itself():
    def coin(*closes, price):
        return {"candles_1h": hourly_rows(list(closes)), "ticker": {"price": price}}

    market = {"retrieved_at": NOW.isoformat(), "coinbase": {
        "BTC": coin(100, 100, 100, 100, 100, 100, price="97"),  # -3%
        "SOL": coin(100, 100, 100, 100, 100, 100, price="99"),  # -1%
        "ADA": coin(100, 100, 100, 100, 100, 100, price="95"),  # -5%
        "DOT": coin(100, 100, 100, 100, 100, 100, price="103"),  # +3%
        "NEW": {"candles_1h": hourly_rows([100]), "ticker": {"price": "1"}},  # no history
    }}
    state = evidence.market_state(market)
    assert state == {"median_2h": D("-0.01"), "btc_2h": D("-0.03"), "coins": 3}
    assert evidence.in_selloff(state, D("0.02")) is True  # BTC crossed it
    assert evidence.in_selloff({"median_2h": D("-0.01"), "btc_2h": D("-0.019")}, D("0.02")) is False
    assert evidence.in_selloff({"median_2h": D("-0.02"), "btc_2h": None}, D("0.02")) is True
    assert evidence.in_selloff({"median_2h": None, "btc_2h": None}, D("0.02")) is False
    assert evidence.market_state({"coinbase": {}}) == {"median_2h": None, "btc_2h": None,
                                                        "coins": 0}


def test_fill_history_counts_the_contexts_closed_and_open_trades_of_the_coin():
    context = {
        "recent_outcomes": {"window_days": 7, "closed_trades": [
            {"symbol": "SOL/USD", "closed_at": "2026-10-01T10:00:00+00:00"},
            {"symbol": "SOL/USD", "closed_at": "2026-10-02T19:24:17+00:00"},
            {"symbol": "ADA/USD", "closed_at": "2026-10-02T18:44:18+00:00"}]},
        "open_trades": [{"symbol": "SOL/USD", "state": "OPEN"},
                        {"symbol": "LINK/USD", "state": "WATCHING"}],
    }
    assert evidence.fill_history(context, "SOL/USD") == (3, "2026-10-02T19:24:17+00:00", 7)
    assert evidence.fill_history(context, "ADA/USD") == (1, "2026-10-02T18:44:18+00:00", 7)
    assert evidence.fill_history(context, "LINK/USD") == (0, None, 7)  # Watching is no fill.
    assert evidence.fill_history({"recent_outcomes": {}}, "SOL/USD") == (0, None, None)


def test_alpaca_volume_reads_the_universe_row():
    context = {"universe": {"coins": [
        {"symbol": "SOL/USD", "volume_24h": {"base": "300", "usd": "35918.42"}},
        {"symbol": "POL/USD", "volume_24h": {"base": "0", "usd": "0"}},
        {"symbol": "OLD/USD"}]}}
    assert evidence.alpaca_volume_usd(context, "SOL/USD") == D("35918.42")
    assert evidence.alpaca_volume_usd(context, "POL/USD") == 0
    assert evidence.alpaca_volume_usd(context, "OLD/USD") is None
    assert evidence.alpaca_volume_usd(context, "NEW/USD") is None
    assert evidence.alpaca_volume_usd({}, "SOL/USD") is None


def test_the_evidence_sentence_states_the_facts():
    text = evidence.sentence(trend=D("0.049"), volume=D("35918.42"), fills=2, window=7,
                             state={"median_2h": D("0.0102")})
    assert text == ("Evidence: +4.9% vs its 20-day average; Alpaca 24 h volume $35,918; filled "
                    "on Alpaca 2 times in the last 7 days; the market's median coin +1.02% in "
                    "2 h.")
    assert evidence.sentence(trend=None, volume=None, fills=0, window=None, state=None) == (
        "Evidence: no 20-day daily history; no fill on Alpaca.")
    assert evidence.sentence(trend=D("-0.02"), volume=D(0), fills=1, window=7,
                             state={"median_2h": None}) == (
        "Evidence: -2.0% vs its 20-day average; Alpaca 24 h volume $0; filled on Alpaca 1 "
        "time in the last 7 days.")
