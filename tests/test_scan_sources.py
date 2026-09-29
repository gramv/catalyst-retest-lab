from dataclasses import fields, replace
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.scan_sources import (
    AlpacaScanSource,
    ScanReadOnlyTransport,
    ScanSourceError,
    ScanUniverse,
    SourcePolicy,
)
from catalyst_lab.setup_scan import engineering_scan_policy
from tests.test_setup_scan import NOW
from tests.test_setup_scan import asset as asset_fixture


@pytest.fixture
def example():
    return asset_fixture.__wrapped__()


def source_policy(**overrides):
    return replace(SourcePolicy("iex", 3, 50, 10000, 10, 5), **overrides)


class MarketFixture:
    def __init__(self, example):
        self.example = example
        self.requests = []
        self.override = None
        self.crypto_rows = [
            {"id": "btc-id", "symbol": "BTC/USD", "class": "crypto", "status": "active",
             "tradable": True, "price_increment": "0.01", "min_trade_increment": "0.000001",
             "min_order_size": "0.00001"},
        ]

    def bars(self):
        return [{"t": b.start_at.isoformat(), "o": str(b.open), "h": str(b.high),
                 "l": str(b.low), "c": str(b.close), "v": str(b.volume)}
                for b in self.example.bars]

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        assert request.headers["APCA-API-KEY-ID"] == "PKSCANSOURCEFIXTURE"
        if self.override:
            response = self.override(request)
            if response:
                return response
        path = request.url.path
        params = request.url.params
        if path == "/v2/assets":
            if params["asset_class"] == "crypto":
                return httpx.Response(200, json=self.crypto_rows)
            return httpx.Response(200, json=[{
                "id": "stock-id", "symbol": "TEST", "class": "us_equity", "status": "active",
                "tradable": True,
            }])
        if path == "/v2/calendar":
            return httpx.Response(200, json=[{
                "date": NOW.date().isoformat(), "open": "09:30", "close": "16:00",
            }])
        symbols = params["symbols"].split(",")
        if path.endswith("/bars"):
            return httpx.Response(200, json={"bars": {s: self.bars() for s in symbols},
                                              "next_page_token": None})
        if "quotes" in path:
            return httpx.Response(200, json={"quotes": {s: {
                "bp": "105.99", "ap": "106.01", "t": NOW.isoformat(),
            } for s in symbols}})
        if "trades" in path:
            return httpx.Response(200, json={"trades": {s: {
                "i": 1234, "p": "106", "s": "1.25", "t": NOW.isoformat(),
            } for s in symbols}})
        raise AssertionError("Unexpected fixture route")


def collect(example, universe=None, handler=None, config=None, clock=lambda: NOW):
    fixture = handler or MarketFixture(example)
    source = AlpacaScanSource(
        AlpacaCredentials("PKSCANSOURCEFIXTURE", "fixture-secret-only"),
        config or source_policy(), clock, transport=httpx.MockTransport(fixture),
    )
    try:
        result = source.collect_and_scan(
            engineering_scan_policy(), universe or ScanUniverse(("TEST",), True, None),
            {("US", "TEST"): example.news, ("CRYPTO", "BTC/USD"): example.news},
        )
    finally:
        source.close()
    return result, fixture


def test_real_endpoint_contract_assembles_stock_and_crypto_scan(example):
    result, fixture = collect(example)
    assert len(result.assets) == 2
    assert len(result.batch.contenders) == 2
    stock, crypto = result.assets
    assert stock.quantity_increment == 1
    assert stock.price_increment == D("0.01")
    assert stock.quote.feed == "iex"
    assert stock.session_close.hour == 16  # Exchange-local calendar offset preserved.
    assert crypto.quantity_increment == D("0.000001")
    assert crypto.minimum_order_size == D("0.00001")
    assert crypto.quote.feed == "CRYPTO_US"
    assert crypto.session_open is None and crypto.session_close is None
    assert result.stock_feed_limitation == "IEX_ONLY_NOT_CONSOLIDATED"
    assert len(result.latest_trades) == 2
    assert result.universe_complete
    assert not result.source_issues
    assert {r.url.host for r in fixture.requests} == {
        "paper-api.alpaca.markets", "data.alpaca.markets",
    }
    assert all(len(r.response_hash) == 64 for r in result.receipts)
    assert "fixture-secret-only" not in repr(result.to_dict())
    assert "PKSCANSOURCEFIXTURE" not in repr(result.to_dict())


def test_paginated_bars_include_later_symbols_and_discard_open_minute(example):
    fixture = MarketFixture(example)

    def pagination(request):
        if request.url.path.endswith("/bars"):
            params = request.url.params
            rows = fixture.bars()
            if "page_token" not in params:
                return httpx.Response(200, json={"bars": {"TEST": rows[:30]},
                                                 "next_page_token": "second-page"})
            unfinished = dict(rows[-1], t=NOW.isoformat())
            return httpx.Response(200, json={"bars": {"TEST": rows[30:] + [unfinished]},
                                             "next_page_token": None})
    fixture.override = pagination
    result, _ = collect(example, ScanUniverse(("TEST",), False, None), fixture)
    assert len(result.assets[0].bars) == 60
    assert len(result.batch.contenders) == 1
    pages = [r for r in result.receipts if r.route.endswith("/bars")]
    assert len(pages) == 2
    assert pages[1].parameters["page_token"] == "second-page"
    assert pages[0].parameters["adjustment"] == "raw"
    assert pages[0].parameters["feed"] == "iex"


@pytest.mark.parametrize("mode", ["cycle", "limit"])
def test_pagination_failure_cannot_use_partial_complete_looking_history(example, mode):
    fixture = MarketFixture(example)

    def pagination(request):
        if request.url.path.endswith("/bars"):
            token = "same-token" if mode == "cycle" else str(len(fixture.requests))
            return httpx.Response(200, json={"bars": {"TEST": fixture.bars()},
                                             "next_page_token": token})
    fixture.override = pagination
    result, _ = collect(example, ScanUniverse(("TEST",), False, None), fixture,
                        source_policy(max_pages=2))
    assert not result.batch.contenders
    reasons = result.batch.decisions[0].reasons
    expected = "BAR_PAGINATION_CYCLE" if mode == "cycle" else "BAR_PAGINATION_LIMIT"
    assert expected in reasons


def test_missing_requested_symbol_is_not_silently_dropped(example):
    result, _ = collect(example, ScanUniverse(("TEST", "MISSING"), False, None))
    assert len(result.batch.decisions) == 2
    missing = next(d for d in result.batch.decisions if d.symbol == "MISSING")
    assert missing.disposition == "REJECTED"
    assert "ASSET_METADATA_UNAVAILABLE" in missing.reasons


def test_crypto_discovery_reports_excluded_pairs_and_untradable_assets(example):
    fixture = MarketFixture(example)
    fixture.crypto_rows += [
        dict(fixture.crypto_rows[0], id="cross-pair", symbol="ETH/BTC"),
        dict(fixture.crypto_rows[0], id="disabled", symbol="NOPE/USD", tradable=False),
    ]
    result, _ = collect(example, ScanUniverse((), True, None), fixture)
    assert len(result.assets) == 1
    assert {e.reason for e in result.universe_exclusions} == {
        "NON_USD_PAIR_OUTSIDE_SCAN_SCOPE", "ASSET_NOT_TRADABLE",
    }


def test_crypto_does_not_guess_missing_precision(example):
    fixture = MarketFixture(example)
    del fixture.crypto_rows[0]["price_increment"]
    result, _ = collect(example, ScanUniverse((), True, None), fixture)
    assert not result.batch.contenders
    assert "CRYPTO_PRECISION_UNAVAILABLE" in result.batch.decisions[0].reasons
    assert result.assets[0].price_increment == 0


def test_data_gap_and_invalid_bar_kept_as_visible_reason(example):
    fixture = MarketFixture(example)

    def corrupt(request):
        if request.url.path.endswith("/bars"):
            rows = fixture.bars()
            rows[20]["o"] = "not-a-price"
            return httpx.Response(200, json={"bars": {"TEST": rows}, "next_page_token": None})
    fixture.override = corrupt
    result, _ = collect(example, ScanUniverse(("TEST",), False, None), fixture)
    assert not result.batch.contenders
    assert "INVALID_BAR_ROW" in result.batch.decisions[0].reasons
    assert "INSUFFICIENT_COMPLETED_BARS" in result.batch.decisions[0].reasons


def test_http_failure_sanitized_and_no_missing_approval_implied(example):
    fixture = MarketFixture(example)

    def fail(request):
        if "quotes" in request.url.path:
            return httpx.Response(403, json={"message": "do-not-log-provider-error-body"})
    fixture.override = fail
    result, _ = collect(example, ScanUniverse(("TEST",), False, None), fixture)
    assert not result.batch.contenders
    assert "SCAN_HTTP_403" in result.batch.decisions[0].reasons
    assert "do-not-log-provider-error-body" not in repr(result)
    assert any(r.http_status == 403 for r in result.receipts)


def test_discovery_outage_is_not_reported_as_no_opportunities(example):
    fixture = MarketFixture(example)
    fixture.override = lambda request: httpx.Response(529) if (
        request.url.path == "/v2/assets"
    ) else None
    result, _ = collect(example, ScanUniverse((), True, None), fixture)
    assert result.universe_complete is False
    assert result.source_issues[0].code == "SCAN_HTTP_529"
    assert len(result.batch.decisions) == 0
    assert not result.batch.target_minimum_met


def test_no_news_is_not_filled_by_provider_or_scanner(example):
    example = replace(example, news=())
    result, _ = collect(example, ScanUniverse(("TEST",), False, None))
    assert result.batch.decisions[0].disposition == "NEEDS_EVIDENCE"
    assert result.assets[0].news == ()


def test_empty_calendar_marks_stock_closed_crypto_untouched(example):
    fixture = MarketFixture(example)
    fixture.override = lambda request: httpx.Response(200, json=[]) if (
        request.url.path == "/v2/calendar"
    ) else None
    result, _ = collect(example, handler=fixture)
    assert "EXCHANGE_CLOSED" in result.batch.decisions[0].reasons
    assert result.batch.decisions[1].disposition == "CONTENDER"


def test_quotes_expire_over_collection_latency(example):
    fixture = MarketFixture(example)
    def slow_clock():
        return NOW + timedelta(seconds=6) if len(fixture.requests) >= 5 else NOW
    result, _ = collect(example, ScanUniverse(("TEST",), False, None), fixture, clock=slow_clock)
    assert "STALE_OR_FUTURE_QUOTE" in result.batch.decisions[0].reasons


def test_redirects_never_follow_to_other_host(example):
    fixture = MarketFixture(example)
    fixture.override = lambda request: httpx.Response(
        302, headers={"Location": "https://other.example/credential-collector"}
    )
    result, _ = collect(example, ScanUniverse((), True, None), fixture)
    assert len(fixture.requests) == 1
    assert "SCAN_HTTP_302" == result.source_issues[0].code


@pytest.mark.parametrize(("method", "url"), [
    ("POST", PAPER_ENDPOINT + "/v2/assets"),
    ("GET", PAPER_ENDPOINT + "/v2/orders"),
    ("GET", PAPER_ENDPOINT.replace("paper-", "") + "/v2/assets"),
    ("GET", "https://data.alpaca.markets/v1beta3/crypto/eu-1/bars"),
    ("GET", "https://user:pass@data.alpaca.markets/v2/stocks/bars"),
])
def test_transport_structurally_refuses_mutations_and_other_destinations(method, url):
    sent = []
    transport = ScanReadOnlyTransport(httpx.MockTransport(lambda request: sent.append(request)))
    request = httpx.Request(method, url, headers={"APCA-API-KEY-ID": "PKSCANSOURCEFIXTURE"})
    with pytest.raises(ScanSourceError, match="SCAN_GET_ENDPOINT_NOT_ALLOWED"):
        transport.handle_request(request)
    assert not sent


def test_source_policy_has_no_silent_values():
    from dataclasses import MISSING

    assert all(f.default is MISSING and f.default_factory is MISSING for f in fields(SourcePolicy))
    with pytest.raises(TypeError):
        SourcePolicy()
    with pytest.raises(ValueError, match="SCAN_EXPLICIT_STOCK_FEED_REQUIRED"):
        source_policy(stock_feed="unspecified")
