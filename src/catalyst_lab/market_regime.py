"""``MARKET_REGIME_V1``: a deterministic market-condition tag of each New York day and of each
trade at its entry (package learning-measure, 2026-10-02; owner: "yes, pull those two fixes
forward", plan ``docs/TRADING-QUALITY-PLAN.md`` section 6, L4).

**A measurement definition, never a trading rule. Paper trading only.** Code only, no model and
no LLM. It reads Alpaca's public, keyless 1-hour crypto bars (``public_crypto_bars``, the
reader the nightly jobs already use) after the fact and appends two immutable record kinds to
``lab.managed_events`` (no setup): ``MARKET_REGIME`` (key ``market-regime:<day>``) and
``TRADE_REGIME`` (key ``trade-regime:<setup_id>``). Nothing here places an order, reads a live
quote, gates an entry or reaches Jev; the scorecard and the weekly review group results by it.

**The day** (``[00:00, 24:00)`` America/New_York). A coin's daily close is the close of its last
1-hour bar that starts inside the day; a day without one has no close (never filled in).

- ``btc_trend``: Bitcoin's close against the mean of its last 20 and last 50 daily closes
  (the day included): ``UP`` above both, ``DOWN`` below both, else ``MIXED``; ``UNKNOWN``
  without all 50 closes.
- ``btc_volatility``: Bitcoin's realized volatility, the population standard deviation of its
  last 20 daily simple returns, ranked among the same measure on each of the 90 days ending
  with the day (``percentile`` = the mid-rank: the share of those values below the day's plus
  half the share equal to it, the day's own included): ``LOW`` at or below 1/3, ``HIGH`` above
  2/3, else ``NORMAL``; ``UNKNOWN`` with fewer than 60 ranked days.
- ``alt_breadth``: the share of the day's research universe (every coin but Bitcoin) whose
  close is above the mean of its last 20 closes: ``BROAD`` at 50% or more, else ``NARROW``;
  ``UNKNOWN`` with fewer than 5 coins measured (a coin without all 20 closes is unmeasured).
- ``selloff``: each hour's median 1-hour return (close over open of the hour's bar) across the
  universe coins with a bar that hour (at least 5); the day's lowest is its biggest median-coin
  drop. ``SELLOFF`` at or below -2%, else ``NO_SELLOFF``; ``UNKNOWN`` with fewer than 12 hours
  measured.
- ``tag``: ``<btc_trend>/<btc_volatility>/<alt_breadth>/<selloff>``.

**The trade at entry** (its first buy fill). The reference hour is the last hour bar that had
completed at entry (``[h, h + 1 h)`` with ``h + 1 h`` at or before the entry):

- ``btc_1h``: Bitcoin's return over that bar; ``btc_4h``: from the open of the bar three hours
  earlier to that bar's close; ``median_coin_1h``: the median of the universe coins' returns over
  that bar (at least 5 coins).
- 1-hour buckets: ``DOWN_2+`` at or below -2%, ``DOWN`` at or below -0.5%, ``FLAT`` under 0.5%,
  ``UP`` under 2%, else ``UP_2+``; 4-hour buckets the same at 1% and 4% (``DOWN_4+`` ...
  ``UP_4+``); ``UNKNOWN`` when a bar is missing.
- ``day_tag``: the entry day's tag (ex post: it includes the rest of that day);
  ``prior_day_tag``: the previous day's (known at entry).

Fail-closed: a day or trade is recorded once, only when every bar read succeeded; an unknown
component is ``UNKNOWN``, never guessed. A missed night is caught up: each run records every
day of the last 40 since the ledger's first research universe, and tags every trade entered in
them (the backfill of days and trades recorded before this version existed).
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market import NY
from catalyst_lab.pick_outcomes import parse_bars
from catalyst_lab.repository import json_safe
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3

D = Decimal
REGIME_VERSION = "MARKET_REGIME_V1"
REGIME_EVENT = "MARKET_REGIME"
TRADE_REGIME_EVENT = "TRADE_REGIME"
TIMEZONE = "America/New_York"
BTC = "BTC/USD"
TIMEFRAME = "1Hour"
BAR_SOURCE = "ALPACA_PUBLIC_CRYPTO_BARS_V1BETA3_US"
# --- The version's definitions (MARKET_REGIME_V1) ----------------------------------------------
TREND_SHORT_DAYS, TREND_LONG_DAYS = 20, 50
VOL_RETURN_DAYS = 20
VOL_RANK_DAYS, VOL_RANK_MIN = 90, 60
BREADTH_DAYS = 20
BREADTH_BROAD_AT = D("0.5")
MIN_COINS = 5
SELLOFF_AT_PCT = D("-2")
SELLOFF_MIN_HOURS = 12
ONE_HOUR_EDGES = (D("0.5"), D("2"))
FOUR_HOUR_EDGES = (D("1"), D("4"))
UNKNOWN = "UNKNOWN"
BACKFILL_DAYS = 40
BTC_HISTORY_DAYS = VOL_RANK_DAYS + VOL_RETURN_DAYS  # Closes back to day - 109.
COIN_HISTORY_DAYS = BREADTH_DAYS
HOUR = timedelta(hours=1)
PCT = D("0.0001")
PRICE = D("0.00000001")
LIMITATIONS = (
    "1-hour bars from Alpaca's public crypto feed (US venues): a coin with no trade in an hour "
    "has no bar that hour; returns are the bar's close over its open.",
    "The day tag uses the whole New York day, so on a trade it includes what happened after "
    "the entry; prior_day_tag is the one known at entry.",
    "The universe is the research universe recorded before the day ended (about 33 coins); a "
    "different universe would give different breadth and median figures.",
)


class RegimeUnavailable(RuntimeError):
    """Nothing can be recorded now (nothing stored); ``str()`` is a code."""


def _q(value, step=PCT):
    if value is None:
        return None
    rounded = value.quantize(step, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


def _pct(new, old):
    with localcontext() as context:
        context.prec = 40
        return (new - old) / old * 100


def _mean(values):
    with localcontext() as context:
        context.prec = 40
        return sum(values, D(0)) / len(values)


def median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    middle = n // 2
    if n % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 40
        return (ordered[middle - 1] + ordered[middle]) / 2


def pstdev(values):
    with localcontext() as context:
        context.prec = 40
        average = sum(values, D(0)) / len(values)
        return (sum(((v - average) ** 2 for v in values), D(0)) / len(values)).sqrt()


def bucket(value, edges):
    """``DOWN_<big>+`` / ``DOWN`` / ``FLAT`` / ``UP`` / ``UP_<big>+`` of a percent return:
    at or below -big, at or below -small, under small, under big, else; ``UNKNOWN`` when
    ``value`` is None."""
    small, big = edges
    if value is None:
        return UNKNOWN
    if value <= -big:
        return f"DOWN_{big}+"
    if value <= -small:
        return "DOWN"
    if value < small:
        return "FLAT"
    if value < big:
        return "UP"
    return f"UP_{big}+"


def bucket_order(edges):
    small, big = edges
    return (f"DOWN_{big}+", "DOWN", "FLAT", "UP", f"UP_{big}+", UNKNOWN)


# --- Pure measures over parsed 1-hour bars -------------------------------------------------


def daily_closes(bars):
    """``{New York day: close}``: the close of the last bar starting inside each day."""
    closes = {}
    for bar in bars:  # Ascending (parse_bars).
        closes[bar.start.astimezone(NY).date()] = bar.close
    return closes


def _window(closes, day, count):
    values = [closes.get(day - timedelta(days=n)) for n in range(count)]
    return None if any(v is None for v in values) else values


def btc_trend(closes, day):
    long = _window(closes, day, TREND_LONG_DAYS)
    if long is None:
        return {"label": UNKNOWN, "close": None, "sma_20": None, "sma_50": None}
    close, short_mean, long_mean = long[0], _mean(long[:TREND_SHORT_DAYS]), _mean(long)
    if close > short_mean and close > long_mean:
        label = "UP"
    elif close < short_mean and close < long_mean:
        label = "DOWN"
    else:
        label = "MIXED"
    return {"label": label, "close": str(close), "sma_20": str(_q(short_mean, PRICE)),
            "sma_50": str(_q(long_mean, PRICE))}


def realized_vol(closes, day):
    """Population stdev (percent) of the 20 daily returns ending ``day``, or None."""
    window = _window(closes, day, VOL_RETURN_DAYS + 1)
    if window is None:
        return None
    returns = [_pct(window[n], window[n + 1]) for n in range(VOL_RETURN_DAYS)]
    return pstdev(returns)


def btc_volatility(closes, day):
    today = realized_vol(closes, day)
    ranked = [v for v in (realized_vol(closes, day - timedelta(days=n))
                          for n in range(VOL_RANK_DAYS)) if v is not None]
    if today is None or len(ranked) < VOL_RANK_MIN:
        return {"label": UNKNOWN, "realized_vol_20d_pct": _q(today), "percentile": None,
                "ranked_days": len(ranked)}
    label, percentile = vol_bucket(today, ranked)
    return {"label": label, "realized_vol_20d_pct": _q(today), "percentile": _q(percentile),
            "ranked_days": len(ranked)}


def vol_bucket(today, ranked):
    """``(LOW | NORMAL | HIGH, mid-rank percentile)`` of ``today`` among ``ranked``."""
    twice = 2 * sum(1 for v in ranked if v < today) + sum(1 for v in ranked if v == today)
    n = len(ranked)  # The percentile is twice / (2n); exact integer comparisons decide.
    label = "LOW" if 3 * twice <= 2 * n else "HIGH" if 3 * twice > 4 * n else "NORMAL"
    return label, D(twice) / (2 * n)


def alt_breadth(coin_closes, day, universe):
    above, measured, unmeasured = 0, 0, []
    for symbol in sorted(s for s in universe if s != BTC):
        window = _window(coin_closes.get(symbol) or {}, day, BREADTH_DAYS)
        if window is None:
            unmeasured.append(symbol)
            continue
        measured += 1
        above += window[0] > _mean(window)
    if measured < MIN_COINS:
        return {"label": UNKNOWN, "above": above, "measured": measured, "share": None,
                "unmeasured": unmeasured}
    share = D(above) / measured
    return {"label": "BROAD" if share >= BREADTH_BROAD_AT else "NARROW", "above": above,
            "measured": measured, "share": _q(share), "unmeasured": unmeasured}


def hour_index(coin_bars):
    """``{symbol: {bar start: Bar}}``."""
    return {symbol: {bar.start: bar for bar in bars} for symbol, bars in coin_bars.items()}


def median_hour_return(indexed, universe, start):
    """``(median percent return, coins)`` of the universe's bars starting at ``start``."""
    values = [_pct(bar.close, bar.open) for symbol in universe
              if (bar := (indexed.get(symbol) or {}).get(start)) is not None]
    return (median(values), len(values)) if len(values) >= MIN_COINS else (None, len(values))


def selloff(indexed, universe, day):
    start, end = day_bounds(day)
    hours, worst = 0, None
    at = start
    while at < end:
        value, coins = median_hour_return(indexed, universe, at)
        if value is not None:
            hours += 1
            if worst is None or value < worst[0]:
                worst = (value, at, coins)
        at += HOUR
    if hours < SELLOFF_MIN_HOURS:
        return {"label": UNKNOWN, "worst_hour_median_return_pct": _q(worst[0]) if worst else None,
                "worst_hour_start": worst[1] if worst else None, "hours_measured": hours}
    return {"label": "SELLOFF" if worst[0] <= SELLOFF_AT_PCT else "NO_SELLOFF",
            "worst_hour_median_return_pct": _q(worst[0]), "worst_hour_start": worst[1],
            "worst_hour_coins": worst[2], "hours_measured": hours}


def day_regime(day, btc_bars, coin_bars, universe):
    """The day's components and tag (pure). ``coin_bars``: ``{symbol: [Bar]}`` of the
    universe (Bitcoin included); ``btc_bars`` reach back ``BTC_HISTORY_DAYS``."""
    return day_regime_from(day, daily_closes(btc_bars),
                           {symbol: daily_closes(bars) for symbol, bars in coin_bars.items()},
                           hour_index(coin_bars), universe)


def day_regime_from(day, btc_closes, coin_closes, indexed, universe):
    """``day_regime`` on precomputed inputs (``daily_closes`` of Bitcoin and of each coin, and
    ``hour_index`` of the coins' bars), for a caller that tags many days from the same bars
    (the history tester, package strategy-c2)."""
    trend = btc_trend(btc_closes, day)
    volatility = btc_volatility(btc_closes, day)
    breadth = alt_breadth(coin_closes, day, universe)
    drop = selloff(indexed, universe, day)
    tag = "/".join(part["label"] for part in (trend, volatility, breadth, drop))
    return {"btc_trend": trend, "btc_volatility": volatility, "alt_breadth": breadth,
            "selloff": drop, "tag": tag}


def trade_regime(entry_at, btc_bars, coin_bars, universe):
    """A trade's at-entry components (pure; the day tags are added by the caller)."""
    entry_at = _aware(entry_at)
    reference = entry_at.replace(minute=0, second=0, microsecond=0) - HOUR
    btc = {bar.start: bar for bar in btc_bars}
    last, first = btc.get(reference), btc.get(reference - 3 * HOUR)
    one = _pct(last.close, last.open) if last else None
    four = _pct(last.close, first.open) if last and first else None
    coin_median, coins = median_hour_return(hour_index(coin_bars), universe, reference)
    return {"reference_hour_start": reference,
            "btc_1h_return_pct": _q(one), "btc_1h": bucket(one, ONE_HOUR_EDGES),
            "btc_4h_return_pct": _q(four), "btc_4h": bucket(four, FOUR_HOUR_EDGES),
            "median_coin_1h_return_pct": _q(coin_median),
            "median_coin_1h": bucket(coin_median, ONE_HOUR_EDGES), "median_coin_count": coins}


def thresholds():
    return {
        "trend_days": [TREND_SHORT_DAYS, TREND_LONG_DAYS], "vol_return_days": VOL_RETURN_DAYS,
        "vol_rank_days": VOL_RANK_DAYS, "vol_rank_min": VOL_RANK_MIN,
        "vol_buckets": "LOW <= 1/3 < NORMAL <= 2/3 < HIGH", "breadth_days": BREADTH_DAYS,
        "breadth_broad_at": str(BREADTH_BROAD_AT), "min_coins": MIN_COINS,
        "selloff_at_pct": str(SELLOFF_AT_PCT), "selloff_min_hours": SELLOFF_MIN_HOURS,
        "one_hour_edges_pct": [str(v) for v in ONE_HOUR_EDGES],
        "four_hour_edges_pct": [str(v) for v in FOUR_HOUR_EDGES],
    }


# --- Ledger reads ------------------------------------------------------------------------------


def regime_key(day):
    return f"market-regime:{day.isoformat()}"


def trade_regime_key(setup_id):
    return f"trade-regime:{setup_id}"


def recorded_regimes(conn, days=None):
    """``{day: body}`` of the recorded ``MARKET_REGIME`` events (of ``days`` when given)."""
    rows = conn.execute(
        "SELECT body FROM lab.managed_events WHERE kind=%s AND setup_id IS NULL ORDER BY event_seq",
        (REGIME_EVENT,),
    ).fetchall()
    wanted = None if days is None else {d.isoformat() for d in days}
    return {row["body"]["day"]: row["body"] for row in rows
            if wanted is None or row["body"]["day"] in wanted}


def recorded_trade_regimes(repository, setup_ids=None):
    """``{setup_id: body}`` of the recorded ``TRADE_REGIME`` events."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind=%s AND setup_id IS NULL
            ORDER BY event_seq""", (TRADE_REGIME_EVENT,),
        ).fetchall()
    wanted = None if setup_ids is None else {str(s) for s in setup_ids}
    return {row["body"]["setup_id"]: row["body"] for row in rows
            if wanted is None or row["body"]["setup_id"] in wanted}


def recorded_regime(repository, day):
    with repository.connect() as conn:
        return conn.execute(
            "SELECT event_seq, body FROM lab.managed_events WHERE idempotency_key=%s",
            (regime_key(day),),
        ).fetchone()


def first_universe_day(conn):
    """The New York day of the ledger's first recorded research universe, or None."""
    from catalyst_lab.learning_intake import OUTLOOK_EVENT

    row = conn.execute(
        """SELECT min(CASE WHEN jsonb_typeof(body->'universe'->'symbols')='array'
          THEN (body->'universe'->>'fetched_at')::timestamptz END) AS at
        FROM lab.managed_events WHERE (kind='RESEARCH_STARTED'
          AND body->>'report_schema_version'=%s) OR kind=%s""",
        (REPORT_SCHEMA_V3, OUTLOOK_EVENT),
    ).fetchone()
    return None if row["at"] is None else row["at"].astimezone(NY).date()


def entered_trades(conn, start, end):
    """Non-engineering managed setups whose first buy fill is in ``[start, end)``."""
    rows = conn.execute(
        """SELECT s.setup_id, s.symbol, s.record_json, min(f.filled_at) AS entry_at
        FROM lab.managed_setups s JOIN lab.managed_fills f USING(setup_id)
        WHERE s.cohort=%s AND f.side='buy' GROUP BY s.setup_id, s.symbol, s.record_json
        HAVING min(f.filled_at) >= %s AND min(f.filled_at) < %s ORDER BY min(f.filled_at)""",
        (COHORT, start, end),
    ).fetchall()
    return [row for row in rows if not is_engineering(row["record_json"])]


def _fetch(bar_reader, symbol, start, end):
    try:
        return parse_bars(bar_reader.bars(symbol, start, end, TIMEFRAME))
    except Exception as exc:  # noqa: BLE001 -- any read or parse failure records nothing.
        raise RegimeUnavailable("REGIME_BARS_UNAVAILABLE") from exc


def _store_once(store, kind, body, key):
    with store.transaction() as conn:
        found = conn.execute("SELECT event_seq FROM lab.managed_events WHERE idempotency_key=%s",
                             (key,)).fetchone()
        if found is not None:
            return "ALREADY_RECORDED"
        store.event(conn, kind, body, key=key)
    return "RECORDED"


def record_regimes(store, bar_reader, *, now):
    """Record every missing ``MARKET_REGIME`` of the last ``BACKFILL_DAYS`` New York days that
    have ended (from the ledger's first research universe) and tag every trade entered in them;
    ``{"days": {day: status}, "trades": {status: count}}``.

    Raises ``RegimeUnavailable`` (nothing recorded) when a bar read fails."""
    from catalyst_lab.market_reality import recorded_universe

    now = _aware(now)
    yesterday = now.astimezone(NY).date() - timedelta(days=1)
    earliest = yesterday - timedelta(days=BACKFILL_DAYS - 1)
    with store.repo.connect() as conn:
        first = first_universe_day(conn)
        recorded = recorded_regimes(conn)
        window_start, _ = day_bounds(earliest)
        _, window_end = day_bounds(yesterday)
        trades = entered_trades(conn, window_start, window_end)
        tagged = {row["body"]["setup_id"] for row in conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind=%s AND setup_id IS NULL",
            (TRADE_REGIME_EVENT,)).fetchall()}
        candidates = [] if first is None else [
            earliest + timedelta(days=n) for n in range(BACKFILL_DAYS)
            if earliest + timedelta(days=n) >= first]
        universes = {}
        for day in candidates:
            row = recorded_universe(conn, day_bounds(day)[1])
            symbols = sorted(set(((row["universe"] or {}).get("symbols") or []) if row else []))
            universes[day] = (symbols, row["event_seq"] if row else None)
    pending = [d for d in candidates if d.isoformat() not in recorded and universes[d][0]]
    untagged = [t for t in trades if str(t["setup_id"]) not in tagged]
    details = {"days": {d.isoformat(): "REGIME_UNIVERSE_UNAVAILABLE" for d in candidates
                        if not universes[d][0]},
               "trades": {"RECORDED": 0, "ALREADY_RECORDED": 0},
               "days_already_recorded": sum(1 for d in candidates if d.isoformat() in recorded)}
    if not pending and not untagged:
        return details
    entry_days = [_aware(t["entry_at"]).astimezone(NY).date() for t in untagged]
    span = pending + entry_days
    low, high = min(span), max(span)
    btc_start, _ = day_bounds(low - timedelta(days=BTC_HISTORY_DAYS))
    coin_start, _ = day_bounds(low - timedelta(days=COIN_HISTORY_DAYS))
    _, fetch_end = day_bounds(high)
    symbols = sorted({s for d in set(span) for s in universes.get(d, ([], None))[0]} | {BTC})
    btc_bars = _fetch(bar_reader, BTC, btc_start, fetch_end)
    coin_bars = {BTC: [b for b in btc_bars if b.start >= coin_start]}
    for symbol in symbols:
        if symbol != BTC:
            coin_bars[symbol] = _fetch(bar_reader, symbol, coin_start, fetch_end)
    computed = {}
    for day in pending:
        universe = universes[day][0]
        parts = day_regime(day, btc_bars, {s: coin_bars.get(s, []) for s in universe},
                           universe)
        start, end = day_bounds(day)
        body = json_safe({
            "regime_version": REGIME_VERSION, "day": day.isoformat(), "timezone": TIMEZONE,
            "window": {"start": start, "end": end}, "computed_at": now, **parts,
            "universe": {"count": len(universe), "symbols": universe,
                         "recorded_event_seq": universes[day][1]},
            "bars": {"source": BAR_SOURCE, "timeframe": TIMEFRAME, "btc_fetch_start": btc_start,
                     "coin_fetch_start": coin_start, "fetch_end": fetch_end},
            "thresholds": thresholds(), "limitations": list(LIMITATIONS),
        })
        details["days"][day.isoformat()] = _store_once(store, REGIME_EVENT, body,
                                                      regime_key(day))
        computed[day.isoformat()] = body
    tags = {**{d: b["tag"] for d, b in recorded.items()},
            **{d: b["tag"] for d, b in computed.items()}}
    for trade, entry_day in zip(untagged, entry_days, strict=True):
        universe = universes.get(entry_day, ([], None))[0]
        if not universe:
            with store.repo.connect() as conn:
                row = recorded_universe(conn, day_bounds(entry_day)[1])
            universe = sorted(set(((row["universe"] or {}).get("symbols") or []) if row else []))
        at_entry = trade_regime(trade["entry_at"], btc_bars,
                                {s: coin_bars.get(s, []) for s in universe}, universe)
        body = json_safe({
            "regime_version": REGIME_VERSION, "setup_id": str(trade["setup_id"]),
            "symbol": trade["symbol"], "entry_at": _aware(trade["entry_at"]),
            "entry_day": entry_day.isoformat(), **at_entry,
            "day_tag": tags.get(entry_day.isoformat()),
            "prior_day_tag": tags.get((entry_day - timedelta(days=1)).isoformat()),
            "universe_count": len(universe), "computed_at": now,
        })
        status = _store_once(store, TRADE_REGIME_EVENT, body,
                             trade_regime_key(trade["setup_id"]))
        details["trades"][status] += 1
    return details


__all__ = [
    "BACKFILL_DAYS", "REGIME_EVENT", "REGIME_VERSION", "RegimeUnavailable", "TRADE_REGIME_EVENT",
    "alt_breadth", "bucket", "bucket_order", "btc_trend", "btc_volatility", "daily_closes",
    "day_regime", "median", "record_regimes", "recorded_regime", "recorded_regimes",
    "recorded_trade_regimes", "selloff", "trade_regime",
]
