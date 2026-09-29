"""Keyless public crypto bar fetch: mocked HTTP only, never a real network call."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from catalyst_lab.public_crypto_bars import (
    CRYPTO_BARS_PATH,
    PublicCryptoBarError,
    PublicCryptoBarReader,
    _PublicBarsTransport,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=1)


def page(rows, *, next_token=None, symbol="BTC/USD"):
    return httpx.Response(200, json={"bars": {symbol: rows}, "next_page_token": next_token})


def test_fetches_a_single_page_and_sends_no_credential_header():
    seen = {}

    def handle(request):
        seen["headers"] = dict(request.headers)
        seen["url"] = request.url
        return page([{"t": START.isoformat(), "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}])

    reader = PublicCryptoBarReader(transport=httpx.MockTransport(handle))
    rows = reader.minute_bars("BTC/USD", START, END)
    reader.close()
    assert len(rows) == 1
    assert seen["url"].host == "data.alpaca.markets"
    assert seen["url"].path == CRYPTO_BARS_PATH
    assert "authorization" not in {k.lower() for k in seen["headers"]}
    assert "apca-api-key-id" not in {k.lower() for k in seen["headers"]}


def test_pages_until_next_page_token_is_absent():
    calls = []

    def handle(request):
        token = request.url.params.get("page_token")
        calls.append(token)
        if token is None:
            return page([{"t": START.isoformat(), "o": "1", "h": "1", "l": "1", "c": "1",
                          "v": "1"}], next_token="tok-2")
        return page([{"t": (START + timedelta(minutes=1)).isoformat(), "o": "2", "h": "2",
                      "l": "2", "c": "2", "v": "2"}])

    reader = PublicCryptoBarReader(transport=httpx.MockTransport(handle))
    rows = reader.minute_bars("BTC/USD", START, END)
    reader.close()
    assert len(rows) == 2 and calls == [None, "tok-2"]


def test_a_repeated_page_token_is_a_pagination_cycle():
    def handle(request):
        return page([], next_token="loop")

    reader = PublicCryptoBarReader(transport=httpx.MockTransport(handle), max_pages=5)
    with pytest.raises(PublicCryptoBarError, match="BAR_PAGINATION_CYCLE"):
        reader.minute_bars("BTC/USD", START, END)
    reader.close()


def test_pagination_has_a_bound():
    counter = {"n": 0}

    def handle(request):
        counter["n"] += 1
        return page([], next_token=f"tok-{counter['n']}")

    reader = PublicCryptoBarReader(transport=httpx.MockTransport(handle), max_pages=3)
    with pytest.raises(PublicCryptoBarError, match="BAR_PAGINATION_LIMIT"):
        reader.minute_bars("BTC/USD", START, END)
    reader.close()


def test_an_unexpected_symbol_in_the_response_is_refused():
    def handle(request):
        return page([{"t": START.isoformat(), "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}],
                    symbol="ETH/USD")

    reader = PublicCryptoBarReader(transport=httpx.MockTransport(handle))
    with pytest.raises(PublicCryptoBarError, match="UNEXPECTED_BAR_RESPONSE"):
        reader.minute_bars("BTC/USD", START, END)
    reader.close()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_a_non_200_status_is_refused(status):
    reader = PublicCryptoBarReader(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    )
    with pytest.raises(PublicCryptoBarError, match=f"PUBLIC_BAR_HTTP_{status}"):
        reader.minute_bars("BTC/USD", START, END)
    reader.close()


def test_invalid_json_is_refused():
    reader = PublicCryptoBarReader(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
    )
    with pytest.raises(PublicCryptoBarError, match="PUBLIC_BAR_INVALID_JSON"):
        reader.minute_bars("BTC/USD", START, END)
    reader.close()


def test_a_connection_error_is_sanitized():
    def handle(request):
        raise httpx.ConnectError("boom")

    reader = PublicCryptoBarReader(transport=httpx.MockTransport(handle))
    with pytest.raises(PublicCryptoBarError, match="PUBLIC_BAR_CONNECTION_ERROR"):
        reader.minute_bars("BTC/USD", START, END)
    reader.close()


@pytest.mark.parametrize("symbol", ["btc/usd", "BTC-USD", "BTC/EUR", "", "x" * 20 + "/USD"])
def test_invalid_symbols_are_refused(symbol):
    reader = PublicCryptoBarReader(transport=httpx.MockTransport(lambda r: page([])))
    with pytest.raises(ValueError, match="INVALID_CRYPTO_SYMBOL"):
        reader.minute_bars(symbol, START, END)
    reader.close()


def test_a_naive_or_backwards_window_is_refused():
    reader = PublicCryptoBarReader(transport=httpx.MockTransport(lambda r: page([])))
    with pytest.raises(ValueError, match="INVALID_BAR_WINDOW"):
        reader.minute_bars("BTC/USD", START.replace(tzinfo=None), END)
    with pytest.raises(ValueError, match="INVALID_BAR_WINDOW"):
        reader.minute_bars("BTC/USD", END, START)
    reader.close()


# --- The transport itself: only this one public route, and never a credential header ----------


def test_transport_refuses_any_other_host_scheme_or_path():
    transport = _PublicBarsTransport(httpx.MockTransport(lambda r: page([])))
    bad = [
        httpx.Request("GET", "http://data.alpaca.markets" + CRYPTO_BARS_PATH),
        httpx.Request("GET", "https://paper-api.alpaca.markets" + CRYPTO_BARS_PATH),
        httpx.Request("GET", "https://data.alpaca.markets/v2/assets"),
        httpx.Request("POST", "https://data.alpaca.markets" + CRYPTO_BARS_PATH),
        httpx.Request("GET", "https://user:pw@data.alpaca.markets" + CRYPTO_BARS_PATH),
    ]
    for request in bad:
        with pytest.raises(PublicCryptoBarError, match="PUBLIC_BAR_GET_ENDPOINT_NOT_ALLOWED"):
            transport.handle_request(request)


def test_transport_refuses_a_credential_shaped_header_even_if_supplied():
    transport = _PublicBarsTransport(httpx.MockTransport(lambda r: page([])))
    request = httpx.Request(
        "GET", "https://data.alpaca.markets" + CRYPTO_BARS_PATH,
        headers={"APCA-API-KEY-ID": "PKFIXTURE"},
    )
    with pytest.raises(PublicCryptoBarError, match="PUBLIC_BAR_READ_MUST_STAY_KEYLESS"):
        transport.handle_request(request)


def test_invalid_page_size_and_max_pages_are_rejected():
    with pytest.raises(ValueError, match="INVALID_BAR_PAGE_SIZE"):
        PublicCryptoBarReader(page_size=0)
    with pytest.raises(ValueError, match="INVALID_BAR_MAX_PAGES"):
        PublicCryptoBarReader(max_pages=0)
