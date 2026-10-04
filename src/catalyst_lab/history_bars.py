"""Keyless public bar history with a disk cache, for the history tester (``HISTORY_TEST_V1``,
package strategy-c2).

**Read-only market data. No credential, no account, no order.** Two public sources:

* ``alpaca``: Alpaca's public crypto bars (``public_crypto_bars.PublicCryptoBarReader``, the same
  keyless reader the strategy shadow uses; its transport allows only that one GET route).
* ``synthetic`` (package oss-packaging): ``sample_market.SyntheticBarReader``'s generated sample
  bars, for tutorials and tests. No network; never cached (they are generated on demand), read
  even with ``offline=True``. Not market data: the history tester labels such a run and its
  promotion check never passes.
* ``coinbase``: Coinbase Exchange's public product candles
  (``GET https://api.exchange.coinbase.com/products/<X>-USD/candles``; at most 300 candles a
  request; reference https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles).
  ``CoinbaseCandleReader``'s transport allows only that GET route and refuses any
  credential-shaped header, as the Alpaca reader does.

Bars are cached as JSON under ``~/.local/share/catalyst-history-cache/<source>/<timeframe>/
<SYMBOL>/`` -- one file per UTC month of 1-hour bars and per UTC day of 1-minute bars -- and only
once the chunk has ended (a chunk still in progress is read but never cached). Requests are
spaced by ``min_interval`` seconds and retried with a growing pause on a rate limit, a server
error or a connection error. ``offline=True`` reads the cache only and fails on a miss
(``HISTORY_CACHE_MISS``): the tests and a re-run never touch the network.

Rows are kept in Alpaca's shape (``t``/``o``/``h``/``l``/``c``/``v``, prices as decimal strings)
and parsed by ``pick_outcomes.parse_bars`` (fail-closed on a malformed row).
"""

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx

from catalyst_lab.pick_outcomes import parse_bars
from catalyst_lab.public_crypto_bars import PublicCryptoBarError, PublicCryptoBarReader

DEFAULT_CACHE_DIR = Path.home() / ".local/share/catalyst-history-cache"
SOURCES = ("alpaca", "coinbase", "synthetic")
SYNTHETIC = "synthetic"
SOURCE_LABELS = {
    "alpaca": "ALPACA_PUBLIC_V1BETA3_CRYPTO_US_BARS",
    "coinbase": "COINBASE_EXCHANGE_PUBLIC_CANDLES",
    # Package oss-packaging: generated sample bars (sample_market), never market data.
    SYNTHETIC: "SYNTHETIC_SAMPLE_BARS_V1",
}
DEFAULT_MIN_INTERVAL = {"alpaca": 0.35, "coinbase": 0.15, SYNTHETIC: 0.0}
RETRIES = 6
CACHE_FORMAT = "HISTORY_BAR_CACHE_V1"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
COINBASE_HOST = "api.exchange.coinbase.com"
COINBASE_MAX_CANDLES = 300
COINBASE_GRANULARITY = {"1Min": 60, "1Hour": 3600}
_COINBASE_PATH = re.compile(r"^/products/[A-Z0-9]{1,16}-USD/candles$")
_SYMBOL = re.compile(r"^([A-Z0-9]{1,16})/USD$")
_ROW_KEYS = frozenset("tohlcv")


class HistoryBarError(Exception):
    """Sanitized machine reason (no response body or header)."""


class _CoinbaseTransport(httpx.BaseTransport):
    """Allows exactly the public candles GET route and refuses credential-shaped headers."""

    def __init__(self, delegate):
        self.delegate = delegate

    def handle_request(self, request):
        url = request.url
        if (request.method != "GET" or url.scheme != "https" or url.host != COINBASE_HOST
                or url.port not in {None, 443} or url.userinfo
                or not _COINBASE_PATH.fullmatch(url.path)):
            raise HistoryBarError("COINBASE_CANDLES_GET_ENDPOINT_NOT_ALLOWED")
        if any(name.lower() in {"authorization", "cb-access-key", "cb-access-sign",
                                "cb-access-passphrase", "cb-access-timestamp"}
               for name in request.headers.keys()):
            raise HistoryBarError("COINBASE_CANDLES_READ_MUST_STAY_KEYLESS")
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


class CoinbaseCandleReader:
    """Coinbase public candles in Alpaca's row shape, ascending, over ``[start, end)``."""

    def __init__(self, *, transport=None, timeout_seconds=15):
        self._client = httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False,
            transport=_CoinbaseTransport(transport or httpx.HTTPTransport()))

    def close(self):
        self._client.close()

    def bars(self, symbol, start, end, timeframe, *, pause=None):
        match = _SYMBOL.fullmatch(symbol) if isinstance(symbol, str) else None
        if match is None or timeframe not in COINBASE_GRANULARITY:
            raise ValueError("INVALID_COINBASE_REQUEST")
        step = COINBASE_GRANULARITY[timeframe]
        product = match.group(1) + "-USD"
        rows, cursor = {}, start
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(seconds=step * COINBASE_MAX_CANDLES))
            if pause is not None:
                pause()
            page = self._get(product, cursor, chunk_end, step)
            for candle in page:
                if not isinstance(candle, list) or len(candle) < 6:
                    raise HistoryBarError("UNEXPECTED_CANDLE_RESPONSE")
                at = datetime.fromtimestamp(int(candle[0]), UTC)
                if cursor <= at < chunk_end:
                    low, high, open_, close, volume = (D(str(v)) for v in candle[1:6])
                    rows[at] = {"t": at.isoformat(), "o": str(open_), "h": str(high),
                                "l": str(low), "c": str(close), "v": str(volume)}
            cursor = chunk_end
        return [rows[k] for k in sorted(rows)]

    def _get(self, product, start, end, granularity):
        # Coinbase's end is inclusive; ask up to one step less so chunks do not overlap.
        params = {"granularity": granularity, "start": start.isoformat(),
                  "end": (end - timedelta(seconds=granularity)).isoformat()}
        try:
            response = self._client.get(f"https://{COINBASE_HOST}/products/{product}/candles",
                                        params=params)
        except httpx.HTTPError:
            raise HistoryBarError("COINBASE_CONNECTION_ERROR") from None
        if response.status_code == 404:
            return []  # Not listed (yet): no candles.
        if response.status_code != 200:
            raise HistoryBarError(f"COINBASE_HTTP_{response.status_code}")
        try:
            page = json.loads(response.content, parse_float=D)
        except (ValueError, UnicodeError):
            raise HistoryBarError("COINBASE_INVALID_JSON") from None
        if not isinstance(page, list):
            raise HistoryBarError("UNEXPECTED_CANDLE_RESPONSE")
        return page


def _retryable(code):
    return any(part in code for part in ("HTTP_429", "HTTP_5", "CONNECTION_ERROR"))


def _month_start(at):
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(at):
    return (at.replace(day=28) + timedelta(days=4)).replace(day=1)


def _day_start(at):
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def _within(rows, start, end):
    def at(row):
        return datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
    return [row for row in rows if start <= at(row) < end]


class BarCache:
    """Cached, polite, keyless bar reads for one source."""

    def __init__(self, source, *, cache_dir=DEFAULT_CACHE_DIR, reader=None, offline=False,
                 min_interval=None, now=None, sleep=time.sleep, clock=time.monotonic):
        if source not in SOURCES:
            raise ValueError("HISTORY_SOURCE_UNKNOWN")
        self.source, self.offline = source, offline
        self.root = Path(cache_dir) / source
        self.min_interval = (DEFAULT_MIN_INTERVAL[source] if min_interval is None
                             else min_interval)
        self.now = now or datetime.now(UTC)
        self._sleep, self._clock, self._last = sleep, clock, None
        self._reader = reader
        self.stats = {"cache_hits": 0, "cache_writes": 0, "requests": 0, "retries": 0}

    def _reader_for(self):
        if self._reader is None and self.source == SYNTHETIC:
            from catalyst_lab.sample_market import SyntheticBarReader

            self._reader = SyntheticBarReader(now=self.now)
        if self._reader is None:
            if self.offline:
                raise HistoryBarError("HISTORY_CACHE_MISS")
            self._reader = (PublicCryptoBarReader() if self.source == "alpaca"
                            else CoinbaseCandleReader())
        return self._reader

    def close(self):
        if self._reader is not None and hasattr(self._reader, "close"):
            self._reader.close()

    def _pause(self):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.stats["requests"] += 1

    def _fetch(self, symbol, start, end, timeframe):
        reader = self._reader_for()
        for attempt in range(RETRIES):
            try:
                if self.source == "coinbase":
                    return reader.bars(symbol, start, end, timeframe, pause=self._pause)
                self._pause()
                return reader.bars(symbol, start, end, timeframe)
            except (PublicCryptoBarError, HistoryBarError) as exc:
                if not _retryable(str(exc)) or attempt == RETRIES - 1:
                    raise HistoryBarError(str(exc)) from None
                self.stats["retries"] += 1
                self._sleep(min(60, 2 ** (attempt + 1)))
        raise HistoryBarError("HISTORY_FETCH_FAILED")  # pragma: no cover

    def _path(self, timeframe, symbol, label):
        return self.root / timeframe / symbol.replace("/", "-") / f"{label}.json"

    def _chunk(self, timeframe, symbol, start, end, label):
        if self.source == SYNTHETIC:  # Generated on demand; nothing to fetch or cache.
            return self._reader_for().bars(symbol, start, end, timeframe)
        path = self._path(timeframe, symbol, label)
        if path.exists():
            self.stats["cache_hits"] += 1
            return _within(json.loads(path.read_text())["rows"], start, end)
        if self.offline:
            raise HistoryBarError("HISTORY_CACHE_MISS")
        complete_end = self.now - timedelta(minutes=10)
        fetch_end = min(end, complete_end.replace(second=0, microsecond=0))
        if fetch_end <= start:
            return []
        # A source's end may be inclusive: keep only the chunk's own bars, or a bar would
        # appear in two chunks.
        rows = _within([{k: str(v) for k, v in row.items() if k in _ROW_KEYS}
                        for row in self._fetch(symbol, start, fetch_end, timeframe)],
                       start, end)
        if end <= complete_end:  # Only a chunk that has ended is cached.
            path.parent.mkdir(parents=True, exist_ok=True)
            body = {"format": CACHE_FORMAT, "source": SOURCE_LABELS[self.source],
                    "symbol": symbol, "timeframe": timeframe, "start": start.isoformat(),
                    "end": end.isoformat(), "rows": rows}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, separators=(",", ":")))
            os.replace(tmp, path)
            self.stats["cache_writes"] += 1
        return rows

    def hourly(self, symbol, start, end):
        """Parsed 1-hour bars starting in ``[start, end)`` (monthly chunks)."""
        rows, at = [], _month_start(start)
        while at < end:
            nxt = _next_month(at)
            rows.extend(self._chunk("1Hour", symbol, at, nxt, at.strftime("%Y-%m")))
            at = nxt
        return [b for b in parse_bars(rows) if start <= b.start < end]

    def minutes(self, symbol, start, end):
        """Parsed 1-minute bars starting in ``[start, end)`` (daily chunks)."""
        rows, at = [], _day_start(start)
        while at < end:
            rows.extend(self._chunk("1Min", symbol, at, at + DAY, at.strftime("%Y-%m-%d")))
            at += DAY
        return [b for b in parse_bars(rows) if start <= b.start < end]


def write_cache_rows(cache_dir, source, timeframe, symbol, label, rows, start, end):
    """Writes one cache file (fixtures and tests)."""
    path = Path(cache_dir) / source / timeframe / symbol.replace("/", "-") / f"{label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": CACHE_FORMAT, "source": SOURCE_LABELS[source],
                                "symbol": symbol, "timeframe": timeframe,
                                "start": start.isoformat(), "end": end.isoformat(),
                                "rows": rows}))
    return path


__all__ = ["BarCache", "CoinbaseCandleReader", "DEFAULT_CACHE_DIR", "HistoryBarError",
           "SOURCES", "SOURCE_LABELS", "write_cache_rows"]
