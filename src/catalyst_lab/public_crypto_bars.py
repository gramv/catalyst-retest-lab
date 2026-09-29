"""Keyless, read-only fetch of Alpaca's public crypto minute bars (``v1beta3``).

Alpaca's crypto market-data bars are published without requiring an account or an API key
(unlike every other read in this codebase, which goes through the owner's paper credentials
and ``AlpacaMarketSource``/``AlpacaReadOnly``). The pick shadow-outcome job (package results,
plan 4.8) deliberately keeps this one dependency-free: it is a deterministic, after-the-fact,
offline job that the owner -- or a daily scheduled run -- can run without provisioning any
broker secret. This module sends no credential of any kind and is restricted, by its own
transport, to exactly the one public GET route it needs.

Reference: https://docs.alpaca.markets/us/reference/cryptobars-1 (checked 2026-09-27; the same
route this codebase's authenticated ``scan_sources.CRYPTO_PATHS["bars"]`` already uses).
"""

import json
import re
from datetime import datetime
from decimal import Decimal as D

import httpx

from catalyst_lab.alpaca import DATA_ENDPOINT

CRYPTO_BARS_PATH = "/v1beta3/crypto/us/bars"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_PAGE_SIZE = 10000
DEFAULT_MAX_PAGES = 50
# The bar sizes a caller may ask for: minutes (shadow outcomes, replays, the day's reality) and
# hours (the reality's 7-day volume, package learning-app).
TIMEFRAMES = frozenset({"1Min", "1Hour"})
_SYMBOL = re.compile(r"^[A-Z0-9]{1,16}/USD$")


class PublicCryptoBarError(Exception):
    """Sanitized machine reason; no response body, header or exception repr crosses this."""


class _PublicBarsTransport(httpx.BaseTransport):
    """Allows exactly one public GET route and refuses to send any credential-shaped header.

    This is a keyless endpoint by design; a header that looks like a credential is refused
    rather than silently dropped, so a caller cannot be quietly upgraded into an authenticated,
    budget-consuming, account-scoped read by mistake.
    """

    def __init__(self, delegate):
        self.delegate = delegate

    def handle_request(self, request):
        url = request.url
        if (
            request.method != "GET"
            or url.scheme != "https"
            or url.host != "data.alpaca.markets"
            or url.port not in {None, 443}
            or url.userinfo
            or url.path != CRYPTO_BARS_PATH
        ):
            raise PublicCryptoBarError("PUBLIC_BAR_GET_ENDPOINT_NOT_ALLOWED")
        if any(name.lower() in {"authorization", "apca-api-key-id", "apca-api-secret-key"}
               for name in request.headers.keys()):
            raise PublicCryptoBarError("PUBLIC_BAR_READ_MUST_STAY_KEYLESS")
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


class PublicCryptoBarReader:
    """Deterministic, paginated fetch of one crypto symbol's completed 1-minute bars.

    Every instance uses one ``httpx.Client`` with no default headers at all (there is no
    credential to attach); tests inject ``transport`` (``httpx.MockTransport``) so no real
    network is ever exercised.
    """

    def __init__(self, *, transport=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                page_size=DEFAULT_PAGE_SIZE, max_pages=DEFAULT_MAX_PAGES):
        if type(page_size) is not int or not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError("INVALID_BAR_PAGE_SIZE")
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("INVALID_BAR_MAX_PAGES")
        self.page_size, self.max_pages = page_size, max_pages
        self._client = httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False,
            transport=_PublicBarsTransport(transport or httpx.HTTPTransport()),
        )

    def close(self):
        self._client.close()

    def minute_bars(self, symbol, start, end):
        """Ascending raw 1-minute bar rows (Alpaca's own ``t``/``o``/``h``/``l``/``c``/``v``
        keys, unparsed) for ``symbol`` over ``[start, end)``.

        ``start`` and ``end`` must be aware datetimes with ``start < end``. Returns a plain
        list of dicts; ``catalyst_lab.pick_outcomes.parse_bars`` validates and normalizes them
        into ``Bar`` tuples -- this reader's only job is the HTTP call and its pagination.
        """
        return self.bars(symbol, start, end, "1Min")

    def bars(self, symbol, start, end, timeframe):
        """``minute_bars`` for any size in ``TIMEFRAMES`` (``1Hour`` rows also carry Alpaca's
        ``vw``, the bar's volume-weighted price, when it publishes one)."""
        if timeframe not in TIMEFRAMES:
            raise ValueError("INVALID_BAR_TIMEFRAME")
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError("INVALID_CRYPTO_SYMBOL")
        if (
            not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.tzinfo is None or end.tzinfo is None or end <= start
        ):
            raise ValueError("INVALID_BAR_WINDOW")
        params = {
            "symbols": symbol, "timeframe": timeframe, "start": start.isoformat(),
            "end": end.isoformat(), "limit": self.page_size, "sort": "asc",
        }
        rows, seen_tokens = [], set()
        for _ in range(self.max_pages):
            page = self._get(dict(params))
            bars = page.get("bars")
            if not isinstance(bars, dict) or set(bars) - {symbol}:
                raise PublicCryptoBarError("UNEXPECTED_BAR_RESPONSE")
            page_rows = bars.get(symbol, [])
            if not isinstance(page_rows, list):
                raise PublicCryptoBarError("UNEXPECTED_BAR_RESPONSE")
            rows.extend(page_rows)
            token = page.get("next_page_token")
            if not token:
                return rows
            if not isinstance(token, str) or token in seen_tokens:
                raise PublicCryptoBarError("BAR_PAGINATION_CYCLE")
            seen_tokens.add(token)
            params["page_token"] = token
        raise PublicCryptoBarError("BAR_PAGINATION_LIMIT")

    def _get(self, params):
        try:
            response = self._client.get(DATA_ENDPOINT + CRYPTO_BARS_PATH, params=params)
        except httpx.HTTPError:
            raise PublicCryptoBarError("PUBLIC_BAR_CONNECTION_ERROR") from None
        if response.status_code != 200:
            raise PublicCryptoBarError(f"PUBLIC_BAR_HTTP_{response.status_code}")
        try:
            page = json.loads(response.content, parse_float=D)
        except (ValueError, UnicodeError):
            raise PublicCryptoBarError("PUBLIC_BAR_INVALID_JSON") from None
        if not isinstance(page, dict):
            raise PublicCryptoBarError("UNEXPECTED_BAR_RESPONSE")
        return page


__all__ = ["CRYPTO_BARS_PATH", "PublicCryptoBarError", "PublicCryptoBarReader", "TIMEFRAMES"]
