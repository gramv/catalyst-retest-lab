"""research_agent.market: Coinbase candle aggregation and the completed-bar cut.

Offline only: httpx.MockTransport stands in for Coinbase's public API, matching the
app's own test convention (a fixture Alpaca market-data source behind MockTransport,
tests/test_research_context.py). No real network access anywhere in this file.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from research_agent import market

T0 = int(datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC).timestamp())  # a round UTC hour
HOUR = 3600


def row(hours_after_t0, low, high, *, close=None, open_=None, volume="1"):
    t = T0 + hours_after_t0 * HOUR
    return [t, str(low), str(high), str(open_ if open_ is not None else low),
            str(close if close is not None else high), volume]


# --- aggregate(): the completed-bar cut, never the wall clock ------------------------------

def test_a_bucket_completed_exactly_at_retrieval_time_is_kept():
    # Two complete 4-hour buckets: hours [0,4) and [4,8).
    rows = [row(h, 100, 101) for h in range(8)]
    retrieved_at = datetime.fromtimestamp(T0 + 8 * HOUR, UTC)  # exactly when bucket 2 ends
    bars = market.aggregate(rows, 4 * HOUR, retrieved_at=retrieved_at)
    assert [bar.started_at for bar in bars] == [
        datetime.fromtimestamp(T0, UTC), datetime.fromtimestamp(T0 + 4 * HOUR, UTC),
    ]


def test_a_bucket_still_forming_at_retrieval_time_is_excluded():
    rows = [row(h, 100, 101) for h in range(8)]
    # One hour short of the second bucket's end: it is still forming right now.
    retrieved_at = datetime.fromtimestamp(T0 + 7 * HOUR, UTC)
    bars = market.aggregate(rows, 4 * HOUR, retrieved_at=retrieved_at)
    assert [bar.started_at for bar in bars] == [datetime.fromtimestamp(T0, UTC)]


def test_a_bucket_missing_an_hour_is_excluded_even_when_old_enough():
    # Bucket [0,4) is missing hour 2: only 3 of its 4 hourly candles are present.
    rows = [row(h, 100, 101) for h in (0, 1, 3)] + [row(h, 100, 101) for h in range(4, 8)]
    retrieved_at = datetime.fromtimestamp(T0 + 30 * HOUR, UTC)  # far in the future either way
    bars = market.aggregate(rows, 4 * HOUR, retrieved_at=retrieved_at)
    assert [bar.started_at for bar in bars] == [datetime.fromtimestamp(T0 + 4 * HOUR, UTC)]


def test_aggregate_high_low_open_close_across_the_bucket():
    rows = [
        row(0, 100, 102, open_=101, close=101.5),
        row(1, 99, 103, open_=101.5, close=100),
        row(2, 98, 101, open_=100, close=99.5),
        row(3, 99.5, 104, open_=99.5, close=103),
    ]
    retrieved_at = datetime.fromtimestamp(T0 + 4 * HOUR, UTC)
    [bar] = market.aggregate(rows, 4 * HOUR, retrieved_at=retrieved_at)
    assert bar.open == Decimal("101") and bar.close == Decimal("103")
    assert bar.low == Decimal("98") and bar.high == Decimal("104")


def test_aggregate_rejects_a_timeframe_that_is_not_an_hourly_multiple():
    with pytest.raises(market.MarketDataError):
        market.aggregate([], 1800, retrieved_at=datetime.now(UTC))


# --- daily_bars(): same completed-bar rule on Coinbase's own daily candles -----------------

def test_daily_bar_completed_exactly_at_retrieval_time_is_kept():
    day_row = [T0, "100", "110", "101", "108", "5"]
    retrieved_at = datetime.fromtimestamp(T0 + 86_400, UTC)
    assert len(market.daily_bars([day_row], retrieved_at=retrieved_at)) == 1


def test_daily_bar_still_forming_is_excluded():
    day_row = [T0, "100", "110", "101", "108", "5"]
    retrieved_at = datetime.fromtimestamp(T0 + 86_400 - 1, UTC)
    assert market.daily_bars([day_row], retrieved_at=retrieved_at) == []


# --- series()/all_series(): dispatch to the right aggregation ------------------------------

def test_series_dispatches_1d_to_daily_bars_and_others_to_aggregate():
    raw_coin = {
        "candles_1h": [row(h, 100, 101) for h in range(8)],
        "candles_1d": [[T0, "100", "110", "101", "108", "5"]],
    }
    retrieved_at = datetime.fromtimestamp(T0 + 86_400, UTC)
    result = market.all_series(raw_coin, retrieved_at=retrieved_at, timeframes=("2h", "4h", "1d"))
    assert len(result["2h"]) == 4 and len(result["4h"]) == 2 and len(result["1d"]) == 1


def test_series_rejects_an_unknown_timeframe():
    with pytest.raises(market.MarketDataError):
        market.series({"candles_1h": [], "candles_1d": []}, "3h", retrieved_at=datetime.now(UTC))


def test_daily_bars_from_data_fetched_without_daily_candles_are_a_clean_error():
    raw_coin = {"candles_1h": [row(h, 100, 101) for h in range(8)]}  # fetch_coinbase(days=0)
    retrieved_at = datetime.fromtimestamp(T0 + 8 * HOUR, UTC)
    with pytest.raises(market.MarketDataError, match="NO_DAILY_CANDLES_FETCHED"):
        market.series(raw_coin, "1d", retrieved_at=retrieved_at)
    bars = market.all_series(raw_coin, retrieved_at=retrieved_at, timeframes=("1h", "2h", "4h"))
    assert [len(bars[tf]) for tf in ("1h", "2h", "4h")] == [8, 4, 2]


# --- Bar (de)serialization round-trip -------------------------------------------------------

def test_bar_json_round_trip():
    bar = market.Bar(started_at=datetime.fromtimestamp(T0, UTC), open=Decimal("1.5"),
                     high=Decimal("2"), low=Decimal("1"), close=Decimal("1.75"),
                     volume=Decimal("10.25"))
    assert market.bar_from_json(market.bar_to_json(bar)) == bar


def test_bar_id_format():
    bar = market.Bar(started_at=datetime(2026, 9, 26, 20, 0, tzinfo=UTC), open=Decimal(1),
                     high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=Decimal(0))
    assert bar.bar_id("SOL", "4h") == "SOL-4h-20260926T2000Z"


# --- fetch_coinbase(): the network layer, entirely mocked ----------------------------------

def _handler(responses):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for suffix, body in responses.items():
            if path.endswith(suffix):
                return httpx.Response(200, json=body) if body is not None else httpx.Response(404)
        return httpx.Response(404)
    return handle


def test_fetch_coinbase_records_an_online_products_market_data_and_excludes_others():
    now = datetime.fromtimestamp(T0 + 8 * HOUR, UTC)
    hourly = [row(h, 100, 101) for h in range(8)]
    daily = [[T0 - 86_400, "100", "110", "101", "108", "5"]]
    responses = {
        "/products/SOL-USD": {"status": "online", "trading_disabled": False,
                              "quote_increment": "0.01"},
        "/products/SOL-USD/candles?granularity=3600&start": hourly,
        "/products/SOL-USD/ticker": {"bid": "100.1", "ask": "100.2", "time": now.isoformat()},
        "/products/OFFLINE-USD": {"status": "delisted", "trading_disabled": True},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "SOL-USD/candles" in url and "granularity=3600" in url:
            return httpx.Response(200, json=hourly)
        if "SOL-USD/candles" in url and "granularity=86400" in url:
            return httpx.Response(200, json=daily)
        if url.endswith("/products/SOL-USD/ticker"):
            return httpx.Response(200, json=responses["/products/SOL-USD/ticker"])
        if url.endswith("/products/SOL-USD"):
            return httpx.Response(200, json=responses["/products/SOL-USD"])
        if url.endswith("/products/OFFLINE-USD"):
            return httpx.Response(200, json=responses["/products/OFFLINE-USD"])
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    result = market.fetch_coinbase(["SOL", "OFFLINE"], client=client, now=now, sleep=0)
    assert result["retrieved_at"] == now.isoformat()
    assert "SOL" in result["coinbase"] and result["coinbase"]["SOL"]["quote_increment"] == "0.01"
    assert result["excluded"]["OFFLINE"] == "NO_ONLINE_COINBASE_USD_PRODUCT"


def test_fetch_coinbase_excludes_a_coin_missing_any_of_its_market_data():
    now = datetime.fromtimestamp(T0 + 8 * HOUR, UTC)

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/products/GAP-USD"):
            return httpx.Response(200, json={"status": "online", "trading_disabled": False,
                                             "quote_increment": "0.01"})
        if "GAP-USD/candles" in url:
            return httpx.Response(200, json=[])  # No candles at all.
        if url.endswith("/products/GAP-USD/ticker"):
            return httpx.Response(200, json={"bid": "1", "ask": "1", "time": now.isoformat()})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    result = market.fetch_coinbase(["GAP"], client=client, now=now, sleep=0)
    assert result["excluded"]["GAP"] == "INCOMPLETE_COINBASE_MARKET_DATA"
    assert result["coinbase"] == {}


def _recording_client(now, seen):
    """A mocked Coinbase for SOL that records every request it answers."""
    hourly = [row(h, 100, 101) for h in range(8)]
    daily = [[T0 - 86_400, "100", "110", "101", "108", "5"]]

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        url = str(request.url)
        if url.endswith("/products/SOL-USD"):
            return httpx.Response(200, json={"status": "online", "trading_disabled": False,
                                             "quote_increment": "0.01"})
        if "SOL-USD/candles" in url:
            granularity = request.url.params["granularity"]
            return httpx.Response(200, json=hourly if granularity == "3600" else daily)
        if url.endswith("/products/SOL-USD/ticker"):
            return httpx.Response(200, json={"bid": "100.1", "ask": "100.2",
                                             "time": now.isoformat()})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handle))


def _candle_calls(seen):
    return {url.params["granularity"]: url for url in seen if url.path.endswith("/candles")}


def test_fetch_coinbase_for_the_intraday_profile_skips_the_daily_candles():
    """INTRADAY_V1 (1h, 2h and 4h bars, all from 1-hour candles): a week of 1-hour candles,
    and no daily request at all; a coin is complete without daily candles."""
    now = datetime.fromtimestamp(T0 + 8 * HOUR + 600, UTC)
    seen = []
    result = market.fetch_coinbase(["SOL"], client=_recording_client(now, seen), now=now,
                                   sleep=0, hours=168, days=0)
    calls = _candle_calls(seen)
    assert list(calls) == ["3600"]  # No granularity=86400 request.
    span = (datetime.fromisoformat(calls["3600"].params["end"])
            - datetime.fromisoformat(calls["3600"].params["start"]))
    assert span.total_seconds() == 168 * HOUR
    assert list(result["coinbase"]["SOL"]) == ["quote_increment", "candles_1h", "ticker"]
    assert result["excluded"] == {}


def test_fetch_coinbase_by_default_asks_for_what_it_always_did():
    now = datetime.fromtimestamp(T0 + 8 * HOUR + 600, UTC)
    seen = []
    result = market.fetch_coinbase(["SOL"], client=_recording_client(now, seen), now=now,
                                   sleep=0)
    calls = _candle_calls(seen)
    assert sorted(calls) == ["3600", "86400"]
    span = (datetime.fromisoformat(calls["3600"].params["end"])
            - datetime.fromisoformat(calls["3600"].params["start"]))
    assert span.total_seconds() == market.HOURLY_LOOKBACK_HOURS * HOUR == 300 * HOUR
    assert calls["86400"].params["start"] == "2026-07-29T00:00:00Z"  # 60 days before now.
    assert list(result["coinbase"]["SOL"]) == ["quote_increment", "candles_1h", "candles_1d",
                                               "ticker"]


def test_fetch_coinbase_never_touches_the_live_alpaca_endpoint():
    """Paper-only hard rule: this module talks to Coinbase's public API only. Derive
    the forbidden literal from the app's own paper constant; never spell it out."""
    from catalyst_lab.config import PAPER_ENDPOINT
    live_endpoint = PAPER_ENDPOINT.replace("paper-", "")
    source = (market.__file__,)
    for path in source:
        with open(path, encoding="utf-8") as handle:
            assert live_endpoint not in handle.read()
