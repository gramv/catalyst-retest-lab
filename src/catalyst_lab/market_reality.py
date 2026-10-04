"""``MARKET_REALITY_V1``: what the research universe actually did on one New York day, and the
grades of the agents' outlooks whose forward windows ended that day (package learning-app,
2026-09-28; plan ``docs/LEARNING-LOOP-PLAN.md`` section 5c, the coordinator's shared
definitions of 2026-09-28).

**Paper trading only; a measurement, never a rule.** Read-only apart from one immutable
``MARKET_REALITY`` event per day (``lab.managed_events``, no setup, key
``market-reality:<day>``). It fetches Alpaca's *public* crypto bars (``public_crypto_bars``, no
key) after the fact; it places no order, reads no live quote and sends nothing to Jev.

**The day.** ``[00:00, 24:00)`` America/New_York (23 or 25 hours on a daylight-saving change).
For every coin of the research universe (the universe the latest report-V3 intake or outlook
recorded before the day ended, plus every coin of an outlook graded that day), from 1-minute
bars: the return (the day's first open to its last close), the largest up and down excursions
from that open, and where the move started (``move_start_at``: for a rising coin the bar of
the lowest low at or before the bar of the day's high, for a falling coin the bar of the highest
high at or before the bar of the day's low, the latest such bar on a tie); from 1-hour bars: the
day's volume against the 7 previous days' daily average, in base units and in USD (volume ×
Alpaca's VWAP, else the close). **Movers**: the five largest risers, the five largest fallers
and every coin with |return| ≥ 5%. **Factors**: Bitcoin's and Ether's day, each crypto sector's
mean return (the coins' classifications) and the universe's total USD volume against its 7-day
average; Muse supplies macro and stock-market factors in its outlook.

**Grades.** Each ``MARKET_OUTLOOK_V1`` is graded once, here, on the day its ``grading_day``
names (the first nightly run after its forward window ``[received_at, received_at + 24 h)``
has fully elapsed), never on a move that happened before it was written. Per coin, the forward
return runs from the first 1-minute open at or after ``received_at`` to the last close before
the window ends. Actual direction FLAT when |return| < 1.5%, else UP or DOWN; a hit when the
outlook's direction equals it, whatever the confidence (SKIPPED and unmeasured coins are not
compared); calibration by confidence bucket; a miss at |return| ≥ 5% while the outlook said
FLAT, the opposite direction or SKIPPED; a false alarm for UP or DOWN with confidence ≥ 0.6
and |return| < 1.5% or the opposite move. A mover ``was_miss`` for an agent when |day return| ≥
5% and the agent's latest outlook recorded at or before the mover's ``move_start_at`` whose
24-hour window covers it said FLAT, the opposite direction or SKIPPED, or no such outlook
exists (``missed_by``); an outlook written after the move began never counts for or against it.

**Regime** (package learning-measure, 2026-10-02): ``regime`` carries the day's recorded
``MARKET_REGIME_V1`` (``market_regime``; the nightly regime step runs before this one), or
``NOT_RECORDED``. Days recorded before then have none; their regime is its own record.

Fail-closed: a day is recorded only once it has ended (plus the bar buffer), only when every
coin's bars could be fetched (a coin with no trades has no bars and is recorded as unmeasured,
not guessed), and never twice.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab.learning_intake import OUTLOOK_EVENT, day_bounds
from catalyst_lab.managed_classification import ALPACA_CRYPTO_SECTOR_OF, DEFAULT_CRYPTO_BUCKET
from catalyst_lab.market import NY
from catalyst_lab.market_regime import REGIME_VERSION, recorded_regimes
from catalyst_lab.pick_outcomes import BAR_FETCH_BUFFER, ShadowDataError, parse_bars
from catalyst_lab.repository import json_safe
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3

D = Decimal
REALITY_VERSION = "MARKET_REALITY_V1"
REALITY_EVENT = "MARKET_REALITY"
TIMEZONE = "America/New_York"
# --- The version's thresholds (MARKET_REALITY_V1) ----------------------------------------------
FLAT_PCT = D("1.5")  # |return| below this is FLAT.
BIG_MOVE_PCT = D("5")  # |return| at or above this is a big move: a mover, and a miss if called
CONFIDENT_AT = D("0.6")  # wrong. A false alarm needs a directional call at least this confident.
MOVERS_EACH_SIDE = 5
VOLUME_LOOKBACK_DAYS = 7
HORIZON = timedelta(hours=24)
CALIBRATION_BUCKETS = (
    ("0.0-0.2", D("0"), D("0.2")), ("0.2-0.4", D("0.2"), D("0.4")),
    ("0.4-0.6", D("0.4"), D("0.6")), ("0.6-0.8", D("0.6"), D("0.8")),
    ("0.8-1.0", D("0.8"), D("1")),
)
OUTLOOK_AGENT_LOOKBACK = timedelta(days=30)
BAR_SOURCE = "ALPACA_PUBLIC_CRYPTO_BARS_V1BETA3_US"
PRICE_TIMEFRAME, VOLUME_TIMEFRAME = "1Min", "1Hour"
PCT = D("0.0001")
RATIO = D("0.0001")
LIMITATIONS = (
    "1-minute and 1-hour bars from Alpaca's public crypto feed: a coin with no trades in a "
    "minute has no bar, and a coin with no bars at all is unmeasured, never guessed.",
    "Returns run from the first bar's open to the last bar's close; excursions are the bars' "
    "highs and lows against that open.",
    "Macro and stock-market factors are not measured here; the agent's outlook supplies them.",
)


class RealityUnavailable(RuntimeError):
    """The day cannot be recorded now (nothing stored); ``str()`` is a code."""


def _q(value, step=PCT):
    """Rounded for the record (half-even), never negative zero; exact values decide."""
    if value is None:
        return None
    rounded = value.quantize(step, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _plain(value):
    """A price as a fixed-point string (``1E-8`` reads ``0.00000001``)."""
    return format(value, "f") if value is not None else None


def _pct(numerator, denominator):
    with localcontext() as context:
        context.prec = 40
        return numerator / denominator * 100


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


# --- Pure measures -------------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowStats:
    open: Decimal
    close: Decimal
    return_pct: Decimal  # Exact; stored quantized.
    high: Decimal
    low: Decimal
    max_up_pct: Decimal
    max_down_pct: Decimal
    move_start_at: datetime
    move_extreme_at: datetime
    bar_count: int
    first_bar_at: datetime
    last_bar_at: datetime

    def record(self):
        return json_safe({
            "open": _plain(self.open), "close": _plain(self.close),
            "return_pct": _q(self.return_pct), "high": _plain(self.high),
            "low": _plain(self.low), "max_up_pct": _q(self.max_up_pct),
            "max_down_pct": _q(self.max_down_pct), "move_start_at": self.move_start_at,
            "move_extreme_at": self.move_extreme_at, "bar_count": self.bar_count,
            "first_bar_at": self.first_bar_at, "last_bar_at": self.last_bar_at,
        })


def window_stats(bars, start, end):
    """The move of ``bars`` (ascending ``pick_outcomes.Bar``) inside ``[start, end)``: the first
    open to the last close, the excursions from that open and where the move started (see the
    module docstring). None when no bar starts inside the window."""
    inside = [bar for bar in bars if start <= bar.start < end]
    if not inside:
        return None
    first, last = inside[0], inside[-1]
    high = max(bar.high for bar in inside)
    low = min(bar.low for bar in inside)
    if last.close >= first.open:
        extreme = next(i for i, bar in enumerate(inside) if bar.high == high)
        before = inside[:extreme + 1]
        trough = min(bar.low for bar in before)
        start_index = max(i for i, bar in enumerate(before) if bar.low == trough)
    else:
        extreme = next(i for i, bar in enumerate(inside) if bar.low == low)
        before = inside[:extreme + 1]
        peak = max(bar.high for bar in before)
        start_index = max(i for i, bar in enumerate(before) if bar.high == peak)
    return WindowStats(
        open=first.open, close=last.close, return_pct=_pct(last.close - first.open, first.open),
        high=high, low=low, max_up_pct=_pct(high - first.open, first.open),
        max_down_pct=_pct(low - first.open, first.open),
        move_start_at=inside[start_index].start, move_extreme_at=inside[extreme].start,
        bar_count=len(inside), first_bar_at=first.start, last_bar_at=last.start,
    )


def actual_direction(return_pct):
    if abs(return_pct) < FLAT_PCT:
        return "FLAT"
    return "UP" if return_pct > 0 else "DOWN"


OPPOSITE = {"UP": "DOWN", "DOWN": "UP"}


def parse_volume_bars(rows):
    """``(start, base volume, USD notional)`` of raw hourly rows (``vw`` when positive, else the
    close); a malformed row raises ``ShadowDataError`` (fail-closed, never skipped)."""
    result = []
    for row in rows:
        try:
            start = _aware(str(row["t"]).replace("Z", "+00:00"))
            volume = D(str(row["v"]))
            price = row.get("vw")
            price = D(str(price)) if price not in (None, 0, "0") else D(str(row["c"]))
            if not volume.is_finite() or volume < 0 or not price.is_finite() or price <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ShadowDataError("INVALID_VOLUME_BAR_ROW") from exc
        result.append((start, volume, volume * price))
    return result


def volume_stats(hours, day_start, day_end, lookback_start):
    day = [(v, usd) for at, v, usd in hours if day_start <= at < day_end]
    prior = [(v, usd) for at, v, usd in hours if lookback_start <= at < day_start]
    base, usd = sum((v for v, _ in day), D(0)), sum((u for _, u in day), D(0))
    prior_base = sum((v for v, _ in prior), D(0)) / VOLUME_LOOKBACK_DAYS
    prior_usd = sum((u for _, u in prior), D(0)) / VOLUME_LOOKBACK_DAYS
    with localcontext() as context:
        context.prec = 40
        ratio = base / prior_base if prior_base > 0 else None
    return {"base": base, "usd": usd, "prior_daily_base": prior_base,
            "prior_daily_usd": prior_usd, "vs_7d_avg": ratio, "hour_bars": len(day),
            "prior_hour_bars": len(prior)}


def bucket_of(confidence):
    for label, low, high in CALIBRATION_BUCKETS:
        if low <= confidence < high or (high == D("1") and confidence == high):
            return label
    return None


def empty_calibration():
    return {label: {"count": 0, "hits": 0, "confidence_sum": D(0)}
            for label, _, _ in CALIBRATION_BUCKETS}


def calibration_rows(buckets):
    rows = []
    for label, _, _ in CALIBRATION_BUCKETS:
        entry = buckets[label]
        count = entry["count"]
        rows.append({
            "bucket": label, "count": count, "hits": entry["hits"],
            "hit_rate": _q(D(entry["hits"]) / count, RATIO) if count else None,
            "mean_confidence": _q(entry["confidence_sum"] / count, RATIO) if count else None,
            # Exact, so windows of several grades can be summed (lessons).
            "confidence_sum": entry["confidence_sum"],
        })
    return rows


def grade_outlook(outlook, minute_bars):
    """The forward-window grade of one recorded outlook (its ``MARKET_OUTLOOK`` event body plus
    ``event_seq``). ``minute_bars``: ``{symbol: [Bar]}`` covering the window."""
    body = outlook["body"]
    start, end = _aware(body["window_start"]), _aware(body["window_end"])
    buckets = empty_calibration()
    coins, misses, false_alarms = [], [], []
    compared = hits = skipped = unmeasured = 0
    for coin in body["coins"]:
        symbol, direction = coin["symbol"], coin["direction"]
        confidence = D(str(coin["confidence"])) if coin.get("confidence") is not None else None
        stats = window_stats(minute_bars.get(symbol) or [], start, end)
        actual = actual_direction(stats.return_pct) if stats else None
        hit = None
        if direction == "SKIPPED":
            skipped += 1
        elif stats is None:
            unmeasured += 1
        else:
            compared += 1
            hit = direction == actual
            hits += hit
            label = bucket_of(confidence)
            buckets[label]["count"] += 1
            buckets[label]["hits"] += hit
            buckets[label]["confidence_sum"] += confidence
        entry = {"symbol": symbol, "direction": direction, "confidence": coin.get("confidence"),
                 "return_pct": _q(stats.return_pct) if stats else None, "actual": actual,
                 "hit": hit, "move_start_at": stats.move_start_at if stats else None}
        coins.append(entry)
        if stats is None:
            continue
        brief = {"symbol": symbol, "return_pct": entry["return_pct"],
                 "move_start_at": stats.move_start_at, "outlook_direction": direction,
                 "outlook_confidence": coin.get("confidence")}
        if abs(stats.return_pct) >= BIG_MOVE_PCT and (
                direction in {"FLAT", "SKIPPED"} or OPPOSITE.get(direction) == actual):
            misses.append(brief)
        if direction in OPPOSITE and confidence >= CONFIDENT_AT and actual != direction:
            false_alarms.append(brief)
    market_calls = {}
    for name, symbol in (("btc", "BTC/USD"), ("eth", "ETH/USD")):
        call = body["market"][name]
        stats = window_stats(minute_bars.get(symbol) or [], start, end)
        actual = actual_direction(stats.return_pct) if stats else None
        market_calls[name] = {
            "symbol": symbol, "direction": call["direction"], "confidence": call["confidence"],
            "return_pct": _q(stats.return_pct) if stats else None, "actual": actual,
            "hit": call["direction"] == actual if stats else None,
        }
    return json_safe({
        "agent_id": body["agent"]["agent_id"], "agent_version": body["agent"]["agent_version"],
        "outlook_id": body["outlook_id"], "outlook_event_seq": outlook["event_seq"],
        "received_at": body["received_at"], "window_start": body["window_start"],
        "window_end": body["window_end"], "grading_day": body["grading_day"],
        "compared": compared, "hits": hits,
        "hit_rate": _q(D(hits) / compared, RATIO) if compared else None,
        "skipped": skipped, "unmeasured": unmeasured,
        "calibration": calibration_rows(buckets), "misses": misses,
        "false_alarms": false_alarms, "market_calls": market_calls, "coins": coins,
    })


def mover_calls(mover, outlooks):
    """``(calls, missed_by)`` for one mover: per agent, the latest outlook recorded at or before
    its ``move_start_at`` whose 24-hour window covers it (None when there is none)."""
    start = _aware(mover["move_start_at"])
    calls, missed = {}, []
    for agent_id, rows in sorted(outlooks.items()):
        covering = [row for row in rows
                    if row["received_at"] <= start < row["received_at"] + HORIZON]
        latest = max(covering, key=lambda row: (row["received_at"], row["event_seq"]),
                     default=None)
        direction = latest["directions"].get(mover["symbol"]) if latest else None
        calls[agent_id] = None if latest is None else {
            "outlook_id": latest["outlook_id"], "direction": direction}
        if mover["big_move"]:
            actual = "UP" if D(mover["return_pct"]) > 0 else "DOWN"
            if direction in {None, "FLAT", "SKIPPED"} or OPPOSITE.get(direction) == actual:
                missed.append(agent_id)
    return calls, missed


def select_movers(measured):
    """Top risers and fallers and every big move of ``measured`` (``[(symbol, WindowStats)]``),
    sorted by return, highest first (ties by symbol)."""
    ranked = sorted(measured, key=lambda item: (-item[1].return_pct, item[0]))
    up = [s for s, stats in ranked if stats.return_pct > 0][:MOVERS_EACH_SIDE]
    down = [s for s, stats in sorted(measured, key=lambda item: (item[1].return_pct, item[0]))
            if stats.return_pct < 0][:MOVERS_EACH_SIDE]
    movers = []
    for symbol, stats in ranked:
        big = abs(stats.return_pct) >= BIG_MOVE_PCT
        if symbol in up or symbol in down or big:
            movers.append({"symbol": symbol, "return_pct": _q(stats.return_pct),
                           "move_start_at": stats.move_start_at, "top_up": symbol in up,
                           "top_down": symbol in down, "big_move": big})
    return movers


# --- Ledger reads --------------------------------------------------------------------------------


def reality_key(day):
    return f"market-reality:{day.isoformat()}"


def recorded_universe(conn, before):
    """The latest universe a report-V3 intake or an outlook recorded, read before ``before``
    (by the universe's own ``fetched_at``, the instant the tradable list was read)."""
    return conn.execute(
        """SELECT universe, event_seq, kind FROM (
          SELECT body->'universe' AS universe, event_seq, kind,
          CASE WHEN jsonb_typeof(body->'universe'->'symbols')='array'
            THEN (body->'universe'->>'fetched_at')::timestamptz END AS fetched_at
          FROM lab.managed_events WHERE (kind='RESEARCH_STARTED'
            AND body->>'report_schema_version'=%s) OR kind=%s) recorded
        WHERE fetched_at < %s ORDER BY fetched_at DESC, event_seq DESC LIMIT 1""",
        (REPORT_SCHEMA_V3, OUTLOOK_EVENT, before),
    ).fetchone()


def outlooks_to_grade(conn, day):
    return conn.execute(
        """SELECT event_seq, body FROM lab.managed_events
        WHERE kind=%s AND setup_id IS NULL AND body->>'grading_day'=%s ORDER BY event_seq""",
        (OUTLOOK_EVENT, day.isoformat()),
    ).fetchall()


def covering_outlooks(conn, start, end):
    """Every outlook received in ``[start, end)``: agent, id, time and each coin's direction."""
    rows = conn.execute(
        """SELECT event_seq, body->'agent'->>'agent_id' AS agent_id,
        body->>'outlook_id' AS outlook_id, body->>'received_at' AS received_at,
        (SELECT jsonb_object_agg(c->>'symbol', c->>'direction')
         FROM jsonb_array_elements(body->'coins') c) AS directions
        FROM lab.managed_events WHERE kind=%s AND setup_id IS NULL
        AND (CASE WHEN kind=%s THEN (body->>'received_at')::timestamptz END) >= %s
        AND (CASE WHEN kind=%s THEN (body->>'received_at')::timestamptz END) < %s
        ORDER BY event_seq""",
        (OUTLOOK_EVENT, OUTLOOK_EVENT, start, OUTLOOK_EVENT, end),
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["agent_id"], []).append({
            "event_seq": row["event_seq"], "outlook_id": row["outlook_id"],
            "received_at": _aware(row["received_at"]), "directions": row["directions"] or {},
        })
    return grouped


def outlook_agents(conn, end):
    """Agents with an outlook received in the 30 days before ``end``."""
    rows = conn.execute(
        """SELECT DISTINCT body->'agent'->>'agent_id' AS agent_id FROM lab.managed_events
        WHERE kind=%s AND setup_id IS NULL
        AND (CASE WHEN kind=%s THEN (body->>'received_at')::timestamptz END) >= %s
        AND (CASE WHEN kind=%s THEN (body->>'received_at')::timestamptz END) < %s""",
        (OUTLOOK_EVENT, OUTLOOK_EVENT, end - OUTLOOK_AGENT_LOOKBACK, OUTLOOK_EVENT, end),
    ).fetchall()
    return sorted(row["agent_id"] for row in rows if row["agent_id"])


def sector_map(conn):
    """``{symbol: sector}`` from the crypto classifications (the theme of a CRYPTO row)."""
    rows = conn.execute(
        "SELECT ticker, theme FROM lab.current_classifications WHERE sector='CRYPTO'"
    ).fetchall()
    return {row["ticker"]: row["theme"] for row in rows}


def sector_of(symbol, classified):
    theme = classified.get(symbol)
    if theme and theme not in {"CRYPTO_SHARED", "CRYPTO_UNLISTED"}:
        return theme
    return ALPACA_CRYPTO_SECTOR_OF.get(symbol, DEFAULT_CRYPTO_BUCKET)


def recorded_reality(repository, day):
    with repository.connect() as conn:
        return conn.execute(
            "SELECT event_seq, body, recorded_at FROM lab.managed_events WHERE idempotency_key=%s",
            (reality_key(day),),
        ).fetchone()


# --- The measurement -----------------------------------------------------------------------------


def measure_day(repository, bar_reader, day, *, now):
    """The ``MARKET_REALITY_V1`` body of New York ``day`` (not recorded; see ``record_day``).

    Raises ``RealityUnavailable`` (nothing to record now) when the day has not ended plus the
    bar buffer, when no universe was ever recorded, or when any coin's bars could not be read.
    """
    day_start, day_end = day_bounds(day)
    now = _aware(now)
    if now < day_end + BAR_FETCH_BUFFER:
        raise RealityUnavailable("REALITY_DAY_NOT_OVER")
    lookback_start, _ = day_bounds(day - timedelta(days=VOLUME_LOOKBACK_DAYS))
    with repository.connect() as conn:
        universe_row = recorded_universe(conn, day_end)
        graded = outlooks_to_grade(conn, day)
        covering = covering_outlooks(conn, day_start - HORIZON, day_end)
        agents = outlook_agents(conn, day_end)
        classified = sector_map(conn)
        regime = recorded_regimes(conn, [day]).get(day.isoformat())
    universe = (universe_row["universe"] or {}) if universe_row else {}
    symbols = set(universe.get("symbols") or [])
    for outlook in graded:
        symbols.update(coin["symbol"] for coin in outlook["body"]["coins"])
        symbols.update({"BTC/USD", "ETH/USD"})
    if not symbols:
        raise RealityUnavailable("REALITY_UNIVERSE_UNAVAILABLE")
    fetch_start = min([day_start, *(_aware(o["body"]["window_start"]) for o in graded)])
    minutes, hours, failed = {}, {}, []
    for symbol in sorted(symbols):
        try:
            minutes[symbol] = parse_bars(bar_reader.bars(symbol, fetch_start, day_end,
                                                         PRICE_TIMEFRAME))
            hours[symbol] = parse_volume_bars(bar_reader.bars(symbol, lookback_start, day_end,
                                                              VOLUME_TIMEFRAME))
        except Exception:  # noqa: BLE001 -- any read or parse failure keeps the day unrecorded.
            failed.append(symbol)
    if failed:
        raise RealityUnavailable("REALITY_BARS_UNAVAILABLE")
    coins, measured, unmeasured, volumes = [], [], [], {}
    for symbol in sorted(symbols):
        stats = window_stats(minutes[symbol], day_start, day_end)
        sector = sector_of(symbol, classified)
        if stats is None:
            unmeasured.append(symbol)
            coins.append({"symbol": symbol, "status": "NO_BARS", "sector": sector})
            continue
        volume = volumes[symbol] = volume_stats(hours[symbol], day_start, day_end,
                                                lookback_start)
        measured.append((symbol, stats))
        coins.append(json_safe({
            "symbol": symbol, "status": "MEASURED", "sector": sector, **stats.record(),
            "volume_base": _plain(volume["base"]),
            "volume_usd": _q(volume["usd"], D("0.01")),
            "volume_7d_avg_base": _plain(_q(volume["prior_daily_base"], D("0.00000001"))),
            "volume_vs_7d_avg": _q(volume["vs_7d_avg"], RATIO),
            "volume_hour_bars": volume["hour_bars"],
            "volume_prior_hour_bars": volume["prior_hour_bars"],
        }))
    movers = select_movers(measured)
    for mover in movers:
        calls, missed = mover_calls(mover, covering)
        mover["calls"], mover["missed_by"] = calls, missed
    by_symbol = dict(measured)
    sectors = {}
    for symbol, stats in measured:
        sectors.setdefault(sector_of(symbol, classified), []).append(stats.return_pct)
    day_usd = sum((volume["usd"] for volume in volumes.values()), D(0))
    prior_usd = sum((volume["prior_daily_usd"] for volume in volumes.values()), D(0))

    def headline(symbol):
        stats = by_symbol.get(symbol)
        return None if stats is None else {
            "return_pct": _q(stats.return_pct), "max_up_pct": _q(stats.max_up_pct),
            "max_down_pct": _q(stats.max_down_pct), "move_start_at": stats.move_start_at}

    factors = {
        "btc": headline("BTC/USD"), "eth": headline("ETH/USD"),
        "btc_return_pct": _q(by_symbol["BTC/USD"].return_pct) if "BTC/USD" in by_symbol else None,
        "eth_return_pct": _q(by_symbol["ETH/USD"].return_pct) if "ETH/USD" in by_symbol else None,
        "sectors": [{"sector": name, "coins": len(values),
                     "mean_return_pct": _q(sum(values, D(0)) / len(values))}
                    for name, values in sorted(sectors.items())],
        "total_volume_usd": _q(day_usd, D("0.01")),
        "total_volume_vs_7d_avg": _q(day_usd / prior_usd, RATIO) if prior_usd > 0 else None,
        "classification_source": "LAB_CURRENT_CLASSIFICATIONS_ELSE_ALPACA_CRYPTO_SECTORS_V1",
    }
    grades = [grade_outlook(outlook, minutes) for outlook in graded]
    return json_safe({
        "reality_version": REALITY_VERSION, "day": day.isoformat(), "timezone": TIMEZONE,
        "window": {"start": day_start, "end": day_end}, "computed_at": now,
        "thresholds": {
            "flat_pct": str(FLAT_PCT), "big_move_pct": str(BIG_MOVE_PCT),
            "false_alarm_confidence": str(CONFIDENT_AT), "movers_each_side": MOVERS_EACH_SIDE,
            "volume_lookback_days": VOLUME_LOOKBACK_DAYS,
            "outlook_horizon_hours": int(HORIZON.total_seconds() // 3600),
            "calibration_buckets": [label for label, _, _ in CALIBRATION_BUCKETS],
        },
        "universe": {
            "source": universe.get("source"), "fetched_at": universe.get("fetched_at"),
            "recorded_by": universe_row["kind"] if universe_row else None,
            "recorded_event_seq": universe_row["event_seq"] if universe_row else None,
            "symbols": sorted(symbols), "count": len(symbols),
        },
        "bars": {"source": BAR_SOURCE, "price_timeframe": PRICE_TIMEFRAME,
                 "volume_timeframe": VOLUME_TIMEFRAME, "price_fetch_start": fetch_start,
                 "volume_fetch_start": lookback_start, "fetch_end": day_end},
        "measured_count": len(measured), "unmeasured": unmeasured,
        "coins": coins, "movers": movers,
        "mover_share": _q(D(len(movers)) / len(measured), RATIO) if measured else None,
        "factors": factors, "outlook_agents": agents, "grades": grades,
        "regime": regime_record(regime), "limitations": list(LIMITATIONS),
    })


def regime_record(regime):
    """The day's recorded ``MARKET_REGIME_V1`` (package learning-measure, 2026-10-02; the nightly
    regime step runs first), without its universe list, or a ``NOT_RECORDED`` marker."""
    if regime is None:
        return {"regime_version": REGIME_VERSION, "status": "NOT_RECORDED"}
    keep = ("regime_version", "tag", "btc_trend", "btc_volatility", "alt_breadth", "selloff",
            "computed_at")
    return {"status": "RECORDED", **{key: regime.get(key) for key in keep}}


def record_day(store, bar_reader, day, *, now):
    """Measure ``day`` and append its ``MARKET_REALITY`` event once; ``(status, event_seq)``."""
    existing = recorded_reality(store.repo, day)
    if existing is not None:
        return "ALREADY_RECORDED", existing["event_seq"]
    body = measure_day(store.repo, bar_reader, day, now=now)
    with store.transaction() as conn:
        found = conn.execute("SELECT event_seq FROM lab.managed_events WHERE idempotency_key=%s",
                             (reality_key(day),)).fetchone()
        if found is not None:
            return "ALREADY_RECORDED", found["event_seq"]
        row = store.event(conn, REALITY_EVENT, body, key=reality_key(day))
    return "RECORDED", row["event_seq"]


def new_york_day(instant):
    return _aware(instant).astimezone(NY).date()


__all__ = [
    "BIG_MOVE_PCT", "CALIBRATION_BUCKETS", "CONFIDENT_AT", "FLAT_PCT", "MOVERS_EACH_SIDE",
    "REALITY_EVENT", "REALITY_VERSION", "RealityUnavailable", "WindowStats", "actual_direction",
    "grade_outlook", "measure_day", "mover_calls", "new_york_day", "record_day",
    "recorded_reality", "select_movers", "window_stats",
]
