"""Keyless, read-only public crypto market data for the public page (package public-page-v3).

The page service reads Alpaca's public crypto bars and latest quotes itself (server side), with
no credential of any kind, through a transport that allows exactly two GET routes on the public
market-data host and refuses any credential-shaped header (as ``public_crypto_bars``). Every
read is timeout-bounded and cached; a failure is cached briefly too, so a down feed costs one
attempt per minute, and the page then omits what needed it (never fake data).

Caches (per process): latest quotes ``QUOTE_TTL`` (15 s), live bars ``LIVE_BARS_TTL`` (5 min),
bars of a window that ended more than an hour ago ``CLOSED_BARS_TTL`` (24 h; those bars no
longer change). At most ``MAX_ENTRIES`` cached reads.
"""

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx

from catalyst_lab.alpaca import DATA_ENDPOINT

BARS_PATH = "/v1beta3/crypto/us/bars"
QUOTES_PATH = "/v1beta3/crypto/us/latest/quotes"
TIMEFRAMES = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600}
TIMEOUT_SECONDS = 4
QUOTE_TTL = 15
LIVE_BARS_TTL = 300
CLOSED_BARS_TTL = 86400
FAILURE_TTL = 60
MAX_ENTRIES = 400
MAX_PAGES = 4
PAGE_SIZE = 10000
_SYMBOL = re.compile(r"^[A-Z0-9]{1,16}/USD$")


class PublicMarketError(Exception):
    """Sanitized machine reason; no response body, header or exception text crosses it."""


class _KeylessTransport(httpx.BaseTransport):
    def __init__(self, delegate):
        self.delegate = delegate

    def handle_request(self, request):
        url = request.url
        if (request.method != "GET" or url.scheme != "https"
                or url.host != "data.alpaca.markets" or url.port not in {None, 443}
                or url.userinfo or url.path not in {BARS_PATH, QUOTES_PATH}):
            raise PublicMarketError("PUBLIC_MARKET_ROUTE_NOT_ALLOWED")
        if any(name.lower() in {"authorization", "apca-api-key-id", "apca-api-secret-key",
                                "cookie"} for name in request.headers.keys()):
            raise PublicMarketError("PUBLIC_MARKET_READ_MUST_STAY_KEYLESS")
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


def _number(value):
    try:
        number = D(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def parse_bar(row):
    """``{"t", "o", "h", "l", "c", "v"}`` with Decimals, or None for a malformed row."""
    if not isinstance(row, dict) or not isinstance(row.get("t"), str):
        return None
    try:
        at = datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    values = {k: _number(row.get(k)) for k in ("o", "h", "l", "c")}
    if any(v is None or v <= 0 for v in values.values()):
        return None
    volume = _number(row.get("v"))
    return {"t": at, **values, "v": volume if volume is not None and volume >= 0 else D(0)}


class PublicMarketData:
    """Cached keyless reads. ``transport`` (tests: ``httpx.MockTransport``) and ``clock``
    (monotonic seconds) are injectable; production uses the real public host."""

    def __init__(self, *, transport=None, timeout=TIMEOUT_SECONDS, clock=time.monotonic,
                 now=None):
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                                    transport=_KeylessTransport(transport or httpx.HTTPTransport()))
        self.clock = clock
        self.now = now or (lambda: datetime.now(UTC))
        self.lock = threading.Lock()
        self.cache = {}  # key -> (expires_at, value or PublicMarketError)
        self.requests = 0

    def close(self):
        self._client.close()

    def _cached(self, key, ttl, read):
        with self.lock:
            hit = self.cache.get(key)
            if hit is not None and hit[0] > self.clock():
                if isinstance(hit[1], PublicMarketError):
                    raise hit[1]
                return hit[1]
        try:
            value = read()
        except PublicMarketError as exc:
            with self.lock:
                self.cache[key] = (self.clock() + FAILURE_TTL, exc)
            raise
        with self.lock:
            if len(self.cache) >= MAX_ENTRIES:
                for old in sorted(self.cache, key=lambda k: self.cache[k][0])[:MAX_ENTRIES // 4]:
                    del self.cache[old]
            self.cache[key] = (self.clock() + ttl, value)
        return value

    def _get(self, path, params):
        self.requests += 1
        try:
            response = self._client.get(DATA_ENDPOINT + path, params=params)
        except httpx.HTTPError:
            raise PublicMarketError("PUBLIC_MARKET_CONNECTION_ERROR") from None
        if response.status_code != 200:
            raise PublicMarketError(f"PUBLIC_MARKET_HTTP_{response.status_code}")
        try:
            page = json.loads(response.content, parse_float=D)
        except (ValueError, UnicodeError):
            raise PublicMarketError("PUBLIC_MARKET_INVALID_JSON") from None
        if not isinstance(page, dict):
            raise PublicMarketError("PUBLIC_MARKET_UNEXPECTED_RESPONSE")
        return page

    def bars(self, symbol, start, end, timeframe):
        """Ascending parsed bars of ``symbol`` over ``[start, end)``; the window is aligned to
        the bar size so repeated reads share one cache entry."""
        if timeframe not in TIMEFRAMES:
            raise ValueError("INVALID_BAR_TIMEFRAME")
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError("INVALID_CRYPTO_SYMBOL")
        step = TIMEFRAMES[timeframe]
        start = datetime.fromtimestamp(int(start.timestamp()) // step * step, UTC)
        end = datetime.fromtimestamp(-(-int(end.timestamp()) // step) * step, UTC)
        if end <= start:
            raise ValueError("INVALID_BAR_WINDOW")
        closed = end < self.now() - timedelta(hours=1)
        key = ("bars", symbol, timeframe, start.isoformat(), end.isoformat())

        def read():
            params = {"symbols": symbol, "timeframe": timeframe, "start": start.isoformat(),
                      "end": end.isoformat(), "limit": PAGE_SIZE, "sort": "asc"}
            rows = []
            for _ in range(MAX_PAGES):
                page = self._get(BARS_PATH, dict(params))
                data = page.get("bars")
                if not isinstance(data, dict) or set(data) - {symbol}:
                    raise PublicMarketError("PUBLIC_MARKET_UNEXPECTED_RESPONSE")
                chunk = data.get(symbol) or []
                if not isinstance(chunk, list):
                    raise PublicMarketError("PUBLIC_MARKET_UNEXPECTED_RESPONSE")
                rows.extend(chunk)
                token = page.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
            parsed = [b for b in (parse_bar(r) for r in rows) if b is not None]
            return sorted({b["t"]: b for b in parsed}.values(), key=lambda b: b["t"])

        return self._cached(key, CLOSED_BARS_TTL if closed else LIVE_BARS_TTL, read)

    def latest_quotes(self, symbols):
        """``{symbol: {"bid", "ask", "at"}}`` for the valid symbols (cached 15 s)."""
        wanted = tuple(sorted({s for s in symbols if isinstance(s, str) and _SYMBOL.fullmatch(s)}))
        if not wanted:
            return {}

        def read():
            page = self._get(QUOTES_PATH, {"symbols": ",".join(wanted)})
            quotes = page.get("quotes")
            if not isinstance(quotes, dict):
                raise PublicMarketError("PUBLIC_MARKET_UNEXPECTED_RESPONSE")
            out = {}
            for symbol, quote in quotes.items():
                if symbol not in wanted or not isinstance(quote, dict):
                    continue
                bid, ask = _number(quote.get("bp")), _number(quote.get("ap"))
                try:
                    at = datetime.fromisoformat(str(quote.get("t")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if bid is not None and bid > 0:
                    out[symbol] = {"bid": bid, "ask": ask if ask and ask > 0 else None,
                                   "at": at.astimezone(UTC)}
            return out

        return self._cached(("quotes", wanted), QUOTE_TTL, read)


__all__ = ["BARS_PATH", "QUOTES_PATH", "TIMEFRAMES", "PublicMarketData", "PublicMarketError",
           "parse_bar"]
