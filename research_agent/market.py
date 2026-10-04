"""Coinbase public candles: 1-hour bars aggregated to 2h/4h/6h, plus daily bars, and 5-minute
candles as fetched for the answers to the app's reviews and exit flags (``fetch_candles``,
``completed_bars``; ``answers``).

Coinbase Exchange's public ``/products/{id}/candles`` needs no key. Every function that
looks at bars here is pure and takes ``retrieved_at`` as data: a bar (or an aggregated
bucket) counts as complete only when it *ended* at or before ``retrieved_at`` — the
instant the caller actually fetched the data, never ``datetime.now()`` read again later.
Building a report from a bar that was still forming when it was fetched is exactly the
``INCOMPLETE_OBSERVED_BAR`` refusal the app's own report-V3 schema raises
(``catalyst_lab.research_report_v3.PickTechnicalEvidence``), so this module never gives
``levels`` or ``build`` a bar that would fail it.

Only the network layer (``fetch_coinbase`` and the other ``fetch_*`` calls) touches a clock,
exactly once per call, and returns that instant in its result for every later step to reuse.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

COINBASE_BASE = "https://api.exchange.coinbase.com"
# Coinbase timeframes this package builds; 1-hour bars are the raw feed, 2h/4h/6h are
# aggregated from them and 1d comes from Coinbase's daily candles. Which of them levels.py
# tries, in which order, is its research profile's (DAILY_V1: 4h, 6h, 1d, 2h, 1h;
# INTRADAY_V2: 1h, 2h, 4h, 6h, 1d; INTRADAY_V1: 1h, 2h, 4h, which needs no daily candles).
TIMEFRAMES = {"1h": 3600, "2h": 7200, "4h": 14_400, "6h": 21_600, "1d": 86_400}
HOURLY_LOOKBACK_HOURS = 300  # Coinbase caps a single candles call near 300 points.
DAILY_LOOKBACK_DAYS = 60
REQUEST_SLEEP_SECONDS = 0.15  # Between-request pacing against Coinbase's public API.
# The granularities Coinbase's candles route serves; 5-minute candles are what the answers to
# the app's reviews and exit flags read (``answers``, MUSE_ANSWER_RULES_V1).
CANDLE_GRANULARITIES = frozenset({60, 300, 900, 3600, 21_600, 86_400})
FIVE_MINUTE_SECONDS = 300


class MarketDataError(Exception):
    """A sanitized, code-like failure; never a provider response body or traceback."""


@dataclass(frozen=True)
class Bar:
    """One completed OHLCV bar. ``started_at`` is the bar's open time, timezone-aware."""

    started_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def bar_id(self, coin, timeframe):
        return f"{coin}-{timeframe}-{self.started_at.strftime('%Y%m%dT%H%M')}Z"


def bar_to_json(bar):
    """A ``Bar`` as plain JSON (Decimals as strings, ``started_at`` as RFC3339)."""
    return {"started_at": bar.started_at.isoformat(), "open": str(bar.open),
            "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
            "volume": str(bar.volume)}


def bar_from_json(data):
    return Bar(started_at=datetime.fromisoformat(data["started_at"]), open=Decimal(data["open"]),
              high=Decimal(data["high"]), low=Decimal(data["low"]),
              close=Decimal(data["close"]), volume=Decimal(data["volume"]))


def _decimal_row(row):
    """One Coinbase candle ``[time, low, high, open, close, volume]`` -> typed fields."""
    t, lo, hi, op, cl, vol = row
    try:
        return int(t), Decimal(str(lo)), Decimal(str(hi)), Decimal(str(op)), Decimal(
            str(cl)
        ), Decimal(str(vol))
    except (InvalidOperation, TypeError, ValueError):
        raise MarketDataError("INVALID_CANDLE_ROW") from None


def aggregate(candles_1h, seconds, *, retrieved_at):
    """1-hour candles bucketed into ``seconds``-wide bars, completed ones only.

    A bucket is kept only when it has every one of its hourly candles (no gap) and it
    ended at or before ``retrieved_at``. Mirrors the aggregation the real Jev accepted
    on 2026-09-27 (research3/levels2.py's ``agg``), generalized to any bar width.
    """
    if seconds % 3600:
        raise MarketDataError("TIMEFRAME_NOT_HOURLY_MULTIPLE")
    cutoff = int(retrieved_at.astimezone(UTC).timestamp())
    hours_per_bucket = seconds // 3600
    buckets: dict[int, list] = {}
    for row in candles_1h:
        t, lo, hi, op, cl, vol = _decimal_row(row)
        bucket_start = t - (t % seconds)
        buckets.setdefault(bucket_start, []).append((t, lo, hi, op, cl, vol))
    bars = []
    for start in sorted(buckets):
        group = sorted(buckets[start])
        if len(group) < hours_per_bucket or start + seconds > cutoff:
            continue  # Incomplete hour coverage, or still forming as of retrieval.
        bars.append(Bar(
            started_at=datetime.fromtimestamp(start, UTC),
            open=group[0][3], close=group[-1][4],
            high=max(row[2] for row in group), low=min(row[1] for row in group),
            volume=sum((row[5] for row in group), Decimal(0)),
        ))
    return bars


def completed_bars(rows, seconds, *, retrieved_at):
    """Coinbase candle rows of one granularity (``seconds`` wide, as fetched: no aggregation) as
    ``Bar``s, oldest first: one per start time (a row returned twice counts once) and only those
    that had ended at or before ``retrieved_at``, the instant they were fetched. The candle still
    forming then, which Coinbase returns too, is never one of them."""
    cutoff = int(retrieved_at.astimezone(UTC).timestamp())
    unique = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 6:
            raise MarketDataError("INVALID_CANDLE_ROW")
        parsed = _decimal_row(row)
        unique[parsed[0]] = parsed
    return [
        Bar(started_at=datetime.fromtimestamp(t, UTC), open=op, high=hi, low=lo, close=cl,
            volume=vol)
        for t, lo, hi, op, cl, vol in sorted(unique.values())
        if t + seconds <= cutoff
    ]


def daily_bars(candles_1d, *, retrieved_at):
    """Coinbase daily candles, completed ones only (started at or before yesterday UTC)."""
    cutoff = int(retrieved_at.astimezone(UTC).timestamp())
    rows = sorted(_decimal_row(row) for row in candles_1d)
    return [
        Bar(started_at=datetime.fromtimestamp(t, UTC), open=op, high=hi, low=lo, close=cl,
            volume=vol)
        for t, lo, hi, op, cl, vol in rows
        if t + TIMEFRAMES["1d"] <= cutoff
    ]


def series(raw_coin, timeframe, *, retrieved_at):
    """The completed bars of one timeframe for one coin's raw Coinbase payload.

    ``raw_coin`` is one value of ``fetch_coinbase(...)["coinbase"]`` (or an equivalent
    fixture with ``candles_1h``/``candles_1d``). Pure; ``retrieved_at`` must be the same
    instant the caller recorded as the data's fetch time, not a fresh clock read.
    """
    if timeframe not in TIMEFRAMES:
        raise MarketDataError("UNKNOWN_TIMEFRAME")
    if timeframe == "1d":
        if raw_coin.get("candles_1d") is None:
            # Fetched without daily candles (fetch_coinbase(days=0): the intraday profile).
            raise MarketDataError("NO_DAILY_CANDLES_FETCHED")
        return daily_bars(raw_coin["candles_1d"], retrieved_at=retrieved_at)
    return aggregate(raw_coin["candles_1h"], TIMEFRAMES[timeframe], retrieved_at=retrieved_at)


def all_series(raw_coin, *, retrieved_at, timeframes=("1h", "2h", "4h", "6h", "1d")):
    """``{timeframe: [Bar, ...]}`` for every requested timeframe, completed bars only."""
    return {timeframe: series(raw_coin, timeframe, retrieved_at=retrieved_at)
            for timeframe in timeframes}


# --- Network layer: Coinbase's public REST API, no key --------------------------------------

def _get(client, url, params=None):
    try:
        response = client.get(url, params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise MarketDataError(f"COINBASE_CONNECTION_ERROR: {type(exc).__name__}") from None
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise MarketDataError(f"COINBASE_HTTP_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise MarketDataError("COINBASE_INVALID_JSON") from None


def fetch_coinbase(coins, *, client=None, timeout=30.0, sleep=REQUEST_SLEEP_SECONDS,
                    hours=HOURLY_LOOKBACK_HOURS, days=DAILY_LOOKBACK_DAYS, now=None):
    """Product info, 1h and 1d candles, and the ticker for each of ``coins``' USD market.

    ``coins`` are base tickers ("SOL", not "SOL/USD"). ``client`` is an injectable
    ``httpx.Client`` (tests pass one built on ``httpx.MockTransport``; a real client is
    opened and closed when omitted). ``now`` is the fetch instant recorded as
    ``retrieved_at`` in the result (a fresh ``datetime.now(UTC)`` when omitted) — every
    later bar-completeness check in this package reuses that one instant, never a new
    clock read. Coins with no online, tradable Coinbase USD product are listed in
    ``excluded`` with a reason, not silently dropped.

    ``hours`` of 1-hour candles are fetched (at most Coinbase's ~300 per call) and ``days``
    of daily candles; ``days=0`` skips the daily call entirely and leaves ``candles_1d`` out
    of each coin's record (a profile with no daily bars needs none: ``levels.INTRADAY_V1``).
    """
    now = now or datetime.now(UTC)
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    coinbase, excluded = {}, {}
    try:
        for coin in sorted(set(coins)):
            product = _get(client, f"{COINBASE_BASE}/products/{coin}-USD")
            time.sleep(sleep)
            if not product or product.get("status") != "online" or product.get(
                "trading_disabled"
            ):
                excluded[coin] = "NO_ONLINE_COINBASE_USD_PRODUCT"
                continue
            end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            hourly = _get(client, f"{COINBASE_BASE}/products/{coin}-USD/candles",
                          {"granularity": 3600, "start": (end - timedelta(hours=hours)).isoformat(),
                           "end": end.isoformat()})
            time.sleep(sleep)
            daily = None
            if days:
                daily = _get(client, f"{COINBASE_BASE}/products/{coin}-USD/candles",
                             {"granularity": 86_400,
                              "start": (now - timedelta(days=days)).date().isoformat()
                              + "T00:00:00Z",
                              "end": now.isoformat()})
                time.sleep(sleep)
            ticker = _get(client, f"{COINBASE_BASE}/products/{coin}-USD/ticker")
            time.sleep(sleep)
            if not hourly or (days and not daily) or not ticker:
                excluded[coin] = "INCOMPLETE_COINBASE_MARKET_DATA"
                continue
            record = {"quote_increment": product["quote_increment"], "candles_1h": hourly}
            if days:
                record["candles_1d"] = daily
            record["ticker"] = ticker
            coinbase[coin] = record
    finally:
        if owns_client:
            client.close()
    return {"retrieved_at": now.isoformat(), "coinbase": coinbase, "excluded": excluded}


def fetch_hourly_span(coins, *, start, end, client=None, timeout=30.0,
                      sleep=REQUEST_SLEEP_SECONDS, now=None):
    """Coinbase 1-hour candles for each of ``coins``' USD market over ``[start, end)``.

    For the evening review (``movers``) and post-mortem price windows, which need a span
    fixed by a calendar day or a trade rather than "the last 300 hours". The span is asked
    for in pieces of at most ``HOURLY_LOOKBACK_HOURS`` (Coinbase's per-call cap), and the
    rows of all pieces are kept as returned; callers drop duplicates and incomplete bars
    (``technicals.complete_hourly_bars``). ``now`` is recorded as ``retrieved_at`` exactly
    as in ``fetch_coinbase``: the one instant every later completeness check reuses. A coin
    with no Coinbase USD candles is listed in ``excluded``, never silently dropped.
    """
    now = now or datetime.now(UTC)
    if end <= start:
        raise MarketDataError("EMPTY_CANDLE_SPAN")
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    coinbase, excluded = {}, {}
    piece = timedelta(hours=HOURLY_LOOKBACK_HOURS)
    try:
        for coin in sorted(set(coins)):
            rows, missing = [], False
            piece_start = start
            while piece_start < end:
                piece_end = min(piece_start + piece, end)
                found = _get(client, f"{COINBASE_BASE}/products/{coin}-USD/candles",
                             {"granularity": 3600, "start": piece_start.isoformat(),
                              "end": piece_end.isoformat()})
                time.sleep(sleep)
                if found is None:
                    missing = True
                    break
                if not isinstance(found, list):
                    raise MarketDataError("COINBASE_UNEXPECTED_CANDLES_SHAPE")
                rows.extend(found)
                piece_start = piece_end
            if missing:
                excluded[coin] = "NO_COINBASE_USD_CANDLES"
                continue
            coinbase[coin] = {"candles_1h": rows}
    finally:
        if owns_client:
            client.close()
    return {"retrieved_at": now.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "coinbase": coinbase, "excluded": excluded}


def fetch_candles(coin, *, seconds, start, end, client=None, timeout=30.0,
                  sleep=REQUEST_SLEEP_SECONDS, now=None):
    """Coinbase's candles of one granularity (``seconds``, one of ``CANDLE_GRANULARITIES``) for
    ``coin``'s USD market over ``[start, end]``, rows exactly as returned (newest first, the one
    still forming included), in one call: at most 300 candles, Coinbase's cap.

    For the answers to the app's reviews and exit flags (``answers``: the last hour of 5-minute
    candles for one trade's coin). ``now`` is recorded as ``retrieved_at`` exactly as in
    ``fetch_coinbase``: the one instant every later completeness check (``completed_bars``)
    reuses. A coin with no Coinbase USD market (404) raises ``MarketDataError``
    (``NO_COINBASE_USD_CANDLES``), as does any other failure; nothing is ever made up.
    """
    now = now or datetime.now(UTC)
    if seconds not in CANDLE_GRANULARITIES:
        raise MarketDataError("UNKNOWN_GRANULARITY")
    if end <= start or (end - start).total_seconds() / seconds > HOURLY_LOOKBACK_HOURS:
        raise MarketDataError("CANDLE_SPAN_INVALID")
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        rows = _get(client, f"{COINBASE_BASE}/products/{coin}-USD/candles",
                    {"granularity": seconds, "start": start.isoformat(),
                     "end": end.isoformat()})
        time.sleep(sleep)
    finally:
        if owns_client:
            client.close()
    if rows is None:
        raise MarketDataError("NO_COINBASE_USD_CANDLES")
    if not isinstance(rows, list):
        raise MarketDataError("COINBASE_UNEXPECTED_CANDLES_SHAPE")
    return {"retrieved_at": now.isoformat(), "product": f"{coin}-USD", "granularity": seconds,
            "start": start.isoformat(), "end": end.isoformat(), "candles": rows}
