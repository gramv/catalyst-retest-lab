"""research_agent.context: GET /api/v1/lab/research-context and the offline fallback.

Offline only: httpx.MockTransport stands in for both the app and Alpaca's public quotes
endpoint. No real network access anywhere in this file, and the agent's token is never
sent anywhere but the ``Authorization`` header of the one request that needs it.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from research_agent import context

D = Decimal

REAL_CONTEXT = {
    "context_version": "RESEARCH_CONTEXT_V1",
    "as_of": "2026-09-27T12:00:00+00:00",
    "schedule": {"current_run_slot": "2026-09-27T08:00:00-04:00",
                "current_run_valid_until_limit": "2026-09-28T08:00:00-04:00"},
    "report_format": {"guidelines_version": "MUSE_RESEARCH_GUIDELINES_V3",
                      "guidelines_sha256": "a" * 64},
    "universe": {"count": 1, "coins": [
        {"symbol": "SOL/USD", "bid": "100.10", "ask": "100.20",
         "quote_at": "2026-09-27T11:59:58+00:00", "price_increment": "0.01",
         "min_order_size": "0.01", "quantity_increment": "0.01"},
    ], "excluded": {}, "issues": []},
    "open_trades": [], "pending_reviews": [], "recent_outcomes": {}, "trade_authorized": False,
}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- fetch_context: the real endpoint, bearer auth, fail-closed ---------------------------

def test_fetch_context_sends_the_bearer_token_and_returns_the_body():
    seen = {}

    def handle(request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json=REAL_CONTEXT)

    result = context.fetch_context("https://app.example", "secret-token-123",
                                    client=_client(handle))
    assert result == REAL_CONTEXT
    assert seen["auth"] == "Bearer secret-token-123"
    assert seen["path"] == context.CONTEXT_ROUTE


def test_fetch_context_raises_on_non_200():
    client = _client(lambda request: httpx.Response(503))
    with pytest.raises(context.ContextError, match="HTTP_503"):
        context.fetch_context("https://app.example", "t", client=client)


def test_fetch_context_raises_on_unexpected_shape():
    client = _client(lambda request: httpx.Response(200, json={"context_version": "WRONG"}))
    with pytest.raises(context.ContextError):
        context.fetch_context("https://app.example", "t", client=client)


def test_fetch_context_raises_on_invalid_json():
    client = _client(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(context.ContextError):
        context.fetch_context("https://app.example", "t", client=client)


def test_fetch_context_raises_on_connection_error():
    def handle(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(context.ContextError, match="CONNECTION_ERROR"):
        context.fetch_context("https://app.example", "t", client=_client(handle))


# --- offline_universe: Alpaca public quotes only, fresh and non-stablecoin ----------------

def test_offline_universe_keeps_only_fresh_non_stablecoin_quotes():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    fresh = datetime.fromtimestamp(now.timestamp() - 60, UTC).isoformat()
    stale = datetime.fromtimestamp(now.timestamp() - 7200, UTC).isoformat()
    seen_symbols = {}

    def handle(request):
        seen_symbols["requested"] = request.url.params.get("symbols")
        quotes = {"SOL/USD": {"bp": "100.1", "ap": "100.2", "t": fresh},
                 "OLD/USD": {"bp": "5", "ap": "5.1", "t": stale}}
        return httpx.Response(200, json={"quotes": quotes})

    result = context.offline_universe(client=_client(handle), candidates=("SOL", "USDC", "OLD"),
                                       now=now)
    assert "USDC" not in seen_symbols["requested"]  # Never even requested.
    assert result["context_version"] == context.OFFLINE_CONTEXT_VERSION
    assert result["as_of"] == now.isoformat()
    assert set(result["universe"]) == {"SOL/USD"}
    assert result["universe"]["SOL/USD"]["bid"] == D("100.1")
    assert result["universe"]["SOL/USD"]["ask"] == D("100.2")
    assert "hours old" in result["excluded"]["OLD/USD"]


def test_offline_universe_excludes_unparseable_quote_time_and_price():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    def handle(request):
        quotes = {"BAD/USD": {"bp": "1", "ap": "1.1", "t": "not-a-time"},
                 "NOPRICE/USD": {"bp": "not-a-number", "ap": "1.1", "t": now.isoformat()}}
        return httpx.Response(200, json={"quotes": quotes})

    result = context.offline_universe(client=_client(handle), candidates=("BAD", "NOPRICE"),
                                       now=now)
    assert result["excluded"]["BAD/USD"] == "UNPARSEABLE_QUOTE_TIME"
    assert result["excluded"]["NOPRICE/USD"] == "UNPARSEABLE_QUOTE_PRICE"


def test_offline_universe_raises_on_http_error():
    client = _client(lambda request: httpx.Response(500))
    with pytest.raises(context.ContextError, match="HTTP_500"):
        context.offline_universe(client=client, candidates=("SOL",))


# --- Shape-agnostic accessors: the real context and the offline fallback ------------------

def test_is_offline_distinguishes_the_two_shapes():
    assert context.is_offline(REAL_CONTEXT) is False
    offline = {"context_version": context.OFFLINE_CONTEXT_VERSION, "universe": {}}
    assert context.is_offline(offline) is True


def test_coin_symbols_and_coin_quote_on_the_real_shape():
    assert context.coin_symbols(REAL_CONTEXT) == ["SOL/USD"]
    quote = context.coin_quote(REAL_CONTEXT, "SOL/USD")
    assert quote["bid"] == D("100.10") and quote["ask"] == D("100.20")
    assert quote["price_increment"] == D("0.01")
    assert context.mid_price(quote) == D("100.15")
    assert context.coin_quote(REAL_CONTEXT, "NOPE/USD") is None


def test_coin_symbols_and_coin_quote_on_the_offline_shape():
    offline = {"context_version": context.OFFLINE_CONTEXT_VERSION,
              "universe": {"SOL/USD": {"bid": D("100"), "ask": D("100.2"),
                                       "quote_at": "2026-09-27T11:59:58+00:00"}}}
    assert context.coin_symbols(offline) == ["SOL/USD"]
    quote = context.coin_quote(offline, "SOL/USD")
    assert quote["price_increment"] is None  # The offline fallback never has one.
    assert context.mid_price(quote) == D("100.1")


def test_mid_price_is_none_without_both_sides():
    assert context.mid_price({"bid": D("1"), "ask": None}) is None
    assert context.mid_price(None) is None


def test_offline_universe_leaves_out_names_alpaca_refuses_whole_requests_for():
    """Alpaca answers the whole request 400 for one symbol outside ^[A-Z]+x?/[A-Z]+$
    (live, 2026-09-27: 1INCH/USD), so such candidates are never sent."""
    seen = {}

    def handler(request):
        seen["symbols"] = request.url.params["symbols"].split(",")
        return httpx.Response(200, json={"quotes": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    context.offline_universe(client=client, candidates=("BTC", "1INCH", "ETH", "USDC"))
    assert seen["symbols"] == ["BTC/USD", "ETH/USD"]


def test_fetch_context_accepts_v2_and_lessons_of_reads_its_lessons():
    v2 = {**REAL_CONTEXT, "context_version": "RESEARCH_CONTEXT_V2",
          "lessons": {"lessons_version": "RESEARCH_LESSONS_V1", "recent_days": []}}
    client = _client(lambda request: httpx.Response(200, json=v2))
    fetched = context.fetch_context("https://app.example", "t", client=client)
    assert context.lessons_of(fetched)["lessons_version"] == "RESEARCH_LESSONS_V1"
    assert context.lessons_of(REAL_CONTEXT) is None  # V1: no lessons.
    assert context.lessons_of({**v2, "lessons": None}) is None  # The status credential's view.


def test_usable_lessons_tells_available_lessons_from_none_and_from_unavailable():
    lessons = {"lessons_version": "RESEARCH_LESSONS_V1", "available": True, "recent_days": []}
    assert context.usable_lessons({"lessons": lessons}) == (lessons, None)
    older = {"lessons_version": "RESEARCH_LESSONS_V1", "recent_days": []}  # Before 65b5e17.
    assert context.usable_lessons({"lessons": older}) == (older, None)
    assert context.usable_lessons({"lessons": None}) == (None, "NO_LESSONS")
    assert context.usable_lessons(REAL_CONTEXT) == (None, "NO_LESSONS")
    unavailable = {"lessons_version": "RESEARCH_LESSONS_V1", "available": False,
                   "code": "LESSONS_UNAVAILABLE"}
    assert context.usable_lessons({"lessons": unavailable}) == (None, "LESSONS_UNAVAILABLE")
