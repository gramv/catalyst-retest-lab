"""``DAILY_BRIEF_V1`` and ``MISSED_TRADEABLE_V1``: the daily speed of the learning loop -- observe
and explain (package learning-loop2, 2026-10-03; owner direction of 2026-10-03, "the learning
loop must include overall learning -- how the market did today, what moved, why, what we
missed -- and adjust the strategy", with the agreed correction: **two speeds**).

**The daily speed observes and explains; it may change only research attention.** Nothing here
places an order, changes a trading rule, a Jev question, a threshold, sizing or a stop, or
reaches Jev. Its ``research_focus`` items are research-side attention only (which sectors,
catalysts and setups to look at first); they never filter, cap or rank picks, and they never
narrow the universe. Rule changes belong to the weekly speed (``weekly_learning``), and even
there they are only proposed.

Paper trading only. Alpaca's public, keyless crypto bars; immutable ``lab.managed_events`` rows
(no setup); no migration.

**``DAILY_BRIEF``** (key ``daily-brief:<day>``), one per New York day, recorded by the nightly
jobs after ``MARKET_REALITY_V1`` and the scorecard of that day (catch-up: the previous day and
the two before it). Sections:

* ``market``: the regime words (``MARKET_REGIME_V1``'s tag and parts), Bitcoin's and Ether's
  day, breadth (coins up, down and flat by ``MARKET_REALITY_V1``'s 1.5% rule, the median coin's
  return) and the sell-off hours (each New York hour's median 1-hour return across the
  universe, ``market_regime.median_hour_return``; hours at or below -2% are sell-off hours).
* ``movers``: the recorded movers with their sector, the sector clusters (two or more movers
  of one sector moving the same way), the pre-move facts computed from the hourly bars before
  each move started (24-hour return, distance from the 7-day high and low, 24-hour volume
  against the 7-day mean: facts that were public before the move), and **why**: the latest
  accepted post-mortem of the mover (cause, ``knowable_before_move``, the knowability class of
  the kit's rule) or ``PENDING_AGENT_POST_MORTEM``. The brief never writes a cause itself.
* ``missed``: per mover, whether an outlook missed it (``missed_by``), whether it was knowable
  (from the post-mortem), whether we picked or traded it, and **whether it was tradeable**: each
  registered mechanical strategy's own ``signals`` and ``simulate`` (the shared decision core:
  the marketable fill, CRYPTO_TRADE_PLAN_V1's stop and target and the exit walk, with
  ``PICK_SHADOW_OUTCOME_V1``'s assumed taker fee) run on the mover's own bars of that day. A
  signal whose 24-hour hold has not passed is ``PENDING`` here; the day's final figures are the
  ``MISSED_TRADEABLE`` record. ``PULLBACK_V1`` needs a research agent's levels: for a mover we
  picked, the pick's shadow outcome stands in; for a mover nobody picked it is
  ``NOT_DETERMINABLE_WITHOUT_RESEARCH_LEVELS`` (never invented).
* ``ours``: how our picks and trades did against the market (the day's scorecard: the funnel,
  closed trades and net R over fee-verified trades, without ``STATS_EXCLUSION_V1`` trades,
  their count beside), the picked coins' mean day return against the universe median.
* ``research_focus``: tomorrow's attention suggestions with their evidence, each
  ``RESEARCH_ATTENTION_ONLY``.
* ``agent_queue``: what still needs an agent (post-mortems with web research and citations),
  and ``computed_in_cloud``: what the jobs did without one.
* ``text``: the same, in plain lines.

**``MISSED_TRADEABLE``** (key ``missed-tradeable:<day>``): the final missed-tradeable figures of
a day's movers, recorded once every signal's fill window and 24-hour hold have passed (the second
night after the day). A mover is ``missed_tradeable`` when a mechanical strategy's simulated
entry on it completed with net R above zero and no paper trade on that coin was entered that day.
The weekly review totals them by regime. Up to ``MISSED_BACKFILL_PER_RUN`` older days of the last
``MISSED_LOOKBACK_DAYS`` are caught up per run.

Fail-closed: a day is recorded only when its reality is recorded and every bar read succeeded;
a day before the ledger's first research universe can never be briefed (not a failure).
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab import strategies
from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market import NY
from catalyst_lab.market_reality import FLAT_PCT, recorded_reality, recorded_universe, sector_of
from catalyst_lab.market_regime import (
    SELLOFF_AT_PCT,
    hour_index,
    median,
    median_hour_return,
    recorded_regimes,
)
from catalyst_lab.pick_outcomes import (
    BAR_FETCH_BUFFER,
    FEE_ASSUMPTION,
    SHADOW_EVENT_KIND,
    TAKER_FEE_TIER1,
    ShadowDataError,
    parse_bars,
)
from catalyst_lab.repository import json_safe
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from catalyst_lab.stats_exclusion import excluded_setups
from catalyst_lab.strategies import core

D = Decimal
BRIEF_VERSION = "DAILY_BRIEF_V1"
BRIEF_EVENT = "DAILY_BRIEF"
MISSED_VERSION = "MISSED_TRADEABLE_V1"
MISSED_EVENT = "MISSED_TRADEABLE"
TIMEZONE = "America/New_York"
SPEED = "DAILY_OBSERVE_AND_EXPLAIN"
FOCUS_SCOPE = "RESEARCH_ATTENTION_ONLY"
HOUR = timedelta(hours=1)
HOLD = timedelta(hours=24)
# Hourly history before the day: the breakout rule's own need (freshness, day and baseline) plus
# one day, which also covers the 7-day pre-move facts.
HOUR_HISTORY = (core.BREAKOUT_HISTORY_HOURS + 24) * HOUR
PRE_MOVE_DAYS = 7
CLUSTER_MIN = 2
MAX_FOCUS = 8
MISSED_LOOKBACK_DAYS = 14
MISSED_BACKFILL_PER_RUN = 4
POST_MORTEM_LOOKBACK_DAYS = 7
PULLBACK_UNDETERMINED = "NOT_DETERMINABLE_WITHOUT_RESEARCH_LEVELS"
PENDING_POST_MORTEM = "PENDING_AGENT_POST_MORTEM"
TRADED = frozenset({core.STOP, core.TARGET, core.HOLD_EXIT})
FOUR = D("0.0001")
BAR_SOURCE = "ALPACA_PUBLIC_CRYPTO_BARS_V1BETA3_US"
LIMITATIONS = (
    "Simulated entries are 1-minute-bar approximations with an assumed taker fee on both legs; "
    "no order was placed and a real fill could differ.",
    "PULLBACK_V1 needs a research agent's entry, stop and target: on a mover nobody picked its "
    "outcome cannot be computed and is not guessed.",
    "Causes come only from accepted post-mortems (an agent's cited web research); the brief "
    "never writes a reason of its own.",
    "research_focus items are attention suggestions for the research side only; they change no "
    "rule, filter or ranking.",
)
COMPUTED_IN_CLOUD = (
    "MARKET_REALITY_V1: every coin's day, the movers, the outlook grades",
    "MARKET_REGIME_V1: the day tag and each trade's at-entry tag",
    "DAILY_BRIEF_V1: market summary, sell-off hours, sector clusters, pre-move facts, "
    "missed-tradeable simulation, our results against the market, research focus",
    "AFTER_EXIT_PATH_V1 and MANAGEMENT_CHANGE_CONTEXT_V1: after-exit paths and decision context",
)
NEEDS_AGENT = (
    "POST_MORTEM_V1: the cause of each mover and notable trade, with cited sources and "
    "knowable_before_move (web research and judgment)",
    "MARKET_OUTLOOK_V1: the morning outlook (a forecast must be written before the day)",
    "The research checklist's NEWS/EVENT patterns (they rest on the post-mortems' factors)",
)


class BriefUnavailable(RuntimeError):
    """Nothing can be recorded for the day now; ``str()`` is a code."""


NOT_APPLICABLE = frozenset({"BRIEF_UNIVERSE_UNAVAILABLE", "MISSED_UNIVERSE_UNAVAILABLE",
                            "MISSED_NOT_READY"})


def _q(value, step=FOUR):
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
    values = list(values)
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return sum(values, D(0)) / len(values)


def _num(value):
    try:
        number = D(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def mechanical_strategies():
    """Every registered strategy with its own signals and simulation (any stage), by id."""
    return [s for _, s in sorted(strategies.REGISTRY.items())
            if strategies.MECHANICAL in s.sources and s.signals and s.simulate]


# --- Pure pieces ------------------------------------------------------------------------------


def hour_medians(coin_bars, universe, day):
    """Each hour of New York ``day``: the median 1-hour return across ``universe`` (at least
    ``MIN_COINS`` coins with a bar), as ``[{hour_start, median_return_pct, coins}]``."""
    indexed = hour_index(coin_bars)
    start, end = day_bounds(day)
    rows, at = [], start
    while at < end:
        value, coins = median_hour_return(indexed, universe, at)
        rows.append({"hour_start": at, "new_york_hour": at.astimezone(NY).strftime("%H:00"),
                     "median_return_pct": _q(value), "coins": coins})
        at += HOUR
    return rows


def selloff_hours(rows):
    return [r for r in rows if r["median_return_pct"] is not None
            and r["median_return_pct"] <= SELLOFF_AT_PCT]


def breadth(coins):
    """Coins up, down and flat on the day (MARKET_REALITY_V1's 1.5% rule) and the median."""
    returns = [D(str(c["return_pct"])) for c in coins if c.get("status") == "MEASURED"]
    up = sum(1 for r in returns if r >= FLAT_PCT)
    down = sum(1 for r in returns if r <= -FLAT_PCT)
    return {"measured": len(returns), "up": up, "down": down, "flat": len(returns) - up - down,
            "positive": sum(1 for r in returns if r > 0),
            "median_return_pct": _q(median(returns)) if returns else None}


def sector_clusters(movers):
    """Sectors with ``CLUSTER_MIN`` or more movers moving the same way."""
    groups = {}
    for mover in movers:
        direction = "UP" if D(str(mover["return_pct"])) > 0 else "DOWN"
        groups.setdefault((mover.get("sector") or "UNKNOWN", direction), []).append(mover)
    return [{"sector": sector, "direction": direction, "coins": len(members),
             "symbols": sorted(m["symbol"] for m in members),
             "mean_return_pct": _q(_mean(D(str(m["return_pct"])) for m in members))}
            for (sector, direction), members in sorted(groups.items())
            if len(members) >= CLUSTER_MIN]


def pre_move_facts(hour_bars, move_start_at):
    """Facts known before the move began, from the hourly bars that ended at or before the
    hour of ``move_start_at``: the 24-hour return, the close against the 7-day high and low, and
    the 24-hour volume against the 7-day daily mean (None where bars are missing)."""
    cutoff = _aware(move_start_at).replace(minute=0, second=0, microsecond=0)
    before = [b for b in hour_bars if b.start + HOUR <= cutoff]
    if not before:
        return {"status": "NO_BARS_BEFORE_MOVE", "as_of": cutoff}
    last = before[-1]
    week = [b for b in before if b.start >= cutoff - PRE_MOVE_DAYS * 24 * HOUR]
    day = [b for b in before if b.start >= cutoff - 24 * HOUR]
    day_ago = next((b for b in before if b.start >= cutoff - 25 * HOUR), None)
    high, low = max(b.high for b in week), min(b.low for b in week)
    week_volume = sum((b.volume for b in week), D(0))
    day_volume = sum((b.volume for b in day), D(0))
    with localcontext() as context:
        context.prec = 40
        daily_mean = week_volume / PRE_MOVE_DAYS if week else D(0)
        ratio = day_volume / daily_mean if daily_mean > 0 else None
    return {"status": "MEASURED", "as_of": cutoff, "close": format(last.close, "f"),
            "return_24h_pct": _q(_pct(last.close, day_ago.open)) if day_ago else None,
            "below_7d_high_pct": _q(_pct(high, last.close)) if last.close > 0 else None,
            "above_7d_low_pct": _q(_pct(last.close, low)) if low > 0 else None,
            "volume_24h_vs_7d_mean": _q(ratio), "hour_bars_7d": len(week)}


def knowability(note, missed_by):
    """The kit's knowability class of a mover's post-mortem (``research_agent.postmortem``):
    pending without a note; then from ``knowable_before_move``."""
    if note is None:
        return PENDING_POST_MORTEM
    knowable = note.get("knowable_before_move")
    if knowable is True:
        return "KNOWABLE_AND_MISSED" if missed_by else "KNOWABLE_AND_EXPECTED"
    return "KNOWABLE_NOT_ACTIONABLE" if knowable is False else "KNOWABILITY_UNKNOWN"


def simulated(result):
    """A simulation's figures for a record (Decimals rounded, plan levels kept)."""
    plan = result.get("plan") or {}
    out = {"outcome": result.get("outcome"),
           "fill_price": format(result["fill_price"], "f")
           if result.get("fill_price") is not None else None,
           "fill_at": result.get("fill_at"), "exit_at": result.get("exit_at"),
           "exit_price": format(result["exit_price"], "f")
           if result.get("exit_price") is not None else None,
           "gross_r": _q(result.get("gross_r")), "net_r": _q(result.get("net_r")),
           "net_r_after_slippage": _q(result.get("net_r_after_slippage")),
           "same_bar_ambiguous": result.get("same_bar_ambiguous")}
    if plan:
        out["plan"] = {k: format(plan[k], "f") if isinstance(plan[k], D) else plan[k]
                       for k in ("stop", "target", "stop_basis", "target_basis") if k in plan}
    if result.get("code"):
        out["code"] = result["code"]
    return out


def tradeable(strategy, symbol, hour_bars, day, *, now, minute_bars_for):
    """``strategy``'s entries on ``symbol`` during New York ``day`` and their outcomes:
    ``[{signal_at, entry_type, status, result}]``. ``status`` is ``PENDING`` until the fill
    window and the hold have passed (``minute_bars_for`` is then not called), else
    ``SIMULATED``. Pure but for ``minute_bars_for(signal_at, end) -> [Bar]``."""
    start, end = day_bounds(day)
    rows = []
    for proposal in strategy.signals(hour_bars, {"symbol": symbol, "since": start,
                                                 "until": end}):
        signal_at = _aware(proposal["signal_at"])
        ready = signal_at + core.ENTRY_FILL_WITHIN + HOLD + BAR_FETCH_BUFFER
        row = {"signal_at": signal_at, "entry_type": proposal.get("entry_type"),
               "reference_price": proposal.get("reference_price"), "ready_at": ready}
        if now < ready:
            rows.append({**row, "status": "PENDING", "result": None})
            continue
        try:
            result = strategy.simulate(proposal, minute_bars_for(signal_at, ready),
                                       fee_rate=TAKER_FEE_TIER1)
        except ShadowDataError as exc:
            rows.append({**row, "status": "INVALID", "result": {"code": str(exc)}})
            continue
        rows.append({**row, "status": "SIMULATED", "result": simulated(result)})
    return rows


def mover_tradeable_summary(by_strategy, traded_today):
    """``(had_entry, complete, best net R, missed_tradeable)`` of one mover's simulations."""
    results = [r["result"] for rows in by_strategy.values() for r in rows
               if r["status"] == "SIMULATED" and r["result"]]
    filled = [r for r in results if r.get("outcome") in TRADED]
    pending = any(r["status"] == "PENDING" for rows in by_strategy.values() for r in rows)
    nets = [D(str(r["net_r"])) for r in filled if r.get("net_r") is not None]
    best = max(nets) if nets else None
    return {"signals": sum(len(rows) for rows in by_strategy.values()),
            "simulated_entries": len(filled), "pending": pending,
            "best_net_r": _q(best),
            "missed_tradeable": bool(best is not None and best > 0 and not traded_today)}


def focus_items(*, clusters, selloffs, regime, knowable_before, pending_queue,
                strategy_hits):
    """Tomorrow's research attention (RESEARCH_ATTENTION_ONLY), each with its evidence."""
    items = []
    for cluster in sorted(clusters, key=lambda c: (-c["coins"], c["sector"])):
        items.append({"kind": "SECTOR_ATTENTION", "scope": FOCUS_SCOPE,
                      "text": f"Watch {cluster['sector']}: {cluster['coins']} coins moved "
                              f"{cluster['direction'].lower()} together "
                              f"({', '.join(cluster['symbols'])}).",
                      "evidence": {k: cluster[k] for k in ("sector", "direction", "coins",
                                                           "mean_return_pct")}})
    if knowable_before:
        items.append({"kind": "CATALYST_ATTENTION", "scope": FOCUS_SCOPE,
                      "text": f"{knowable_before} mover(s) of the last "
                              f"{POST_MORTEM_LOOKBACK_DAYS} days had a cited cause public before "
                              "the move began: check scheduled events, listings and unlocks "
                              "earlier in the morning run.",
                      "evidence": {"knowable_before_move": knowable_before,
                                   "days": POST_MORTEM_LOOKBACK_DAYS}})
    for strategy_id, hit in sorted(strategy_hits.items()):
        if not hit["movers_with_signal"]:
            continue
        mean = hit["mean_net_r"]
        items.append({"kind": "SETUP_ATTENTION", "scope": FOCUS_SCOPE,
                      "text": f"{strategy_id} signalled on {hit['movers_with_signal']} of "
                              f"{hit['movers']} movers ({hit['simulated']} simulated, mean net R "
                              f"{mean if mean is not None else 'pending'}): note that setup in "
                              "research; trading it needs a named version and the owner's yes.",
                      "evidence": hit})
    if selloffs:
        worst = min(selloffs, key=lambda r: r["median_return_pct"])
        items.append({"kind": "MARKET_CONTEXT", "scope": FOCUS_SCOPE,
                      "text": f"{len(selloffs)} sell-off hour(s); the worst at "
                              f"{worst['new_york_hour']} New York (median coin "
                              f"{worst['median_return_pct']}%): say in the outlook how the "
                              "picks would fare in a repeat.",
                      "evidence": {"hours": [r["new_york_hour"] for r in selloffs],
                                   "worst_median_return_pct": worst["median_return_pct"]}})
    trend = ((regime or {}).get("btc_trend") or {}).get("label")
    if trend == "DOWN":
        items.append({"kind": "MARKET_CONTEXT", "scope": FOCUS_SCOPE,
                      "text": "Bitcoin closed below its 20- and 50-day means: weigh each "
                              "pick's own trend in its rationale.",
                      "evidence": {"btc_trend": trend}})
    if pending_queue:
        items.append({"kind": "AGENT_QUEUE", "scope": FOCUS_SCOPE,
                      "text": f"{pending_queue} post-mortem(s) await an agent's cited "
                              "research (movers and notable trades).",
                      "evidence": {"pending": pending_queue}})
    return items[:MAX_FOCUS]


# --- Ledger reads ---------------------------------------------------------------------------------


def latest_post_mortems(conn):
    rows = conn.execute(
        """SELECT body->'items' AS items, body->>'note_id' AS note_id FROM lab.managed_events
        WHERE kind='POST_MORTEM' AND setup_id IS NULL ORDER BY event_seq""").fetchall()
    latest = {}
    for row in rows:
        for item in row["items"] or []:
            latest[item["subject_key"]] = {
                "cause": item.get("cause"), "knowable_before_move":
                item.get("knowable_before_move"), "note_id": row["note_id"],
                "sources": len(item.get("sources") or [])}
    return latest


def picks_on(conn, start, end):
    """``{symbol: [pick]}``: report-V3 packets whose window overlaps ``[start, end)``, with
    their latest shadow outcome (research levels) when recorded."""
    rows = conn.execute(
        """SELECT body->>'cycle_id' AS cycle_id, body->>'item_key' AS item_key,
        (body->>'revision')::int AS revision, body->>'symbol' AS symbol,
        body->>'created_at' AS created_at, body->>'run_slot' AS run_slot,
        body->'levels' AS levels
        FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
        AND body->>'report_schema_version'=%s
        AND (CASE WHEN body ? 'created_at' THEN (body->>'created_at')::timestamptz END) < %s
        AND (CASE WHEN body ? 'expires_at' THEN (body->>'expires_at')::timestamptz END) > %s
        ORDER BY event_seq""", (REPORT_SCHEMA_V3, end, start)).fetchall()
    shadows = {}
    for row in conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq""",
            (SHADOW_EVENT_KIND,)).fetchall():
        pick = row["body"]["pick"]
        shadows[(pick.get("cycle_id"), pick.get("item_key"))] = row["body"]["outcome"]
    found = {}
    for row in rows:
        outcome = shadows.get((row["cycle_id"], row["item_key"]))
        found.setdefault(row["symbol"], []).append({
            "cycle_id": row["cycle_id"], "item_key": row["item_key"],
            "run_slot": row["run_slot"],
            "shadow": None if outcome is None else {
                "outcome": outcome.get("outcome"), "triggered": outcome.get("triggered"),
                "net_r": outcome.get("net_r"), "data_complete": outcome.get("data_complete")}})
    return found


def entered_on(conn, start, end):
    """``{symbol: [setup_id]}``: non-engineering managed trades first filled in the window."""
    rows = conn.execute(
        """SELECT s.setup_id, s.symbol, s.record_json, min(f.filled_at) AS entry_at
        FROM lab.managed_setups s JOIN lab.managed_fills f USING(setup_id)
        WHERE s.cohort=%s AND f.side='buy' GROUP BY s.setup_id, s.symbol, s.record_json
        HAVING min(f.filled_at) >= %s AND min(f.filled_at) < %s""", (COHORT, start, end),
    ).fetchall()
    found = {}
    for row in rows:
        if not is_engineering(row["record_json"]):
            found.setdefault(row["symbol"], []).append(str(row["setup_id"]))
    return found


def brief_key(day):
    return f"daily-brief:{day.isoformat()}"


def missed_key(day):
    return f"missed-tradeable:{day.isoformat()}"


def recorded_brief(repository, day=None):
    with repository.connect() as conn:
        if day is None:
            return conn.execute(
                """SELECT event_seq, body FROM lab.managed_events WHERE kind=%s AND setup_id
                IS NULL ORDER BY body->>'day' DESC, event_seq DESC LIMIT 1""", (BRIEF_EVENT,),
            ).fetchone()
        return conn.execute(
            "SELECT event_seq, body FROM lab.managed_events WHERE idempotency_key=%s",
            (brief_key(day),)).fetchone()


def recorded_missed(repository, day=None):
    with repository.connect() as conn:
        if day is None:
            return conn.execute(
                "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                (MISSED_EVENT,)).fetchall()
        return conn.execute(
            "SELECT event_seq, body FROM lab.managed_events WHERE idempotency_key=%s",
            (missed_key(day),)).fetchone()


# --- The shared mover evaluation ---------------------------------------------------------------


def _reality_or_refusal(repository, day, prefix):
    row = recorded_reality(repository, day)
    if row is not None:
        return row["body"]
    with repository.connect() as conn:
        universe = recorded_universe(conn, day_bounds(day)[1])
    raise BriefUnavailable(f"{prefix}_UNIVERSE_UNAVAILABLE" if universe is None
                           else f"{prefix}_REALITY_NOT_RECORDED")


def _fetch(reader, symbol, start, end, timeframe, code):
    try:
        return parse_bars(reader.bars(symbol, start, end, timeframe))
    except Exception as exc:  # noqa: BLE001 -- any read or parse failure records nothing.
        raise BriefUnavailable(code) from exc


def evaluate_movers(repository, reader, day, reality, *, now, code):
    """Each recorded mover with its sector, pre-move facts, picks, trades, post-mortem and the
    mechanical strategies' simulated entries; and the hourly bars of every universe coin."""
    start, end = day_bounds(day)
    universe = sorted(set((reality.get("universe") or {}).get("symbols") or []))
    with repository.connect() as conn:
        from catalyst_lab.market_reality import sector_map

        classified = sector_map(conn)
        notes = latest_post_mortems(conn)
        picks = picks_on(conn, start, end)
        entered = entered_on(conn, start, end)
    mover_symbols = {m["symbol"] for m in reality.get("movers") or []}
    hours = {}
    for symbol in universe:
        fetch_start = start - HOUR_HISTORY if symbol in mover_symbols else start
        hours[symbol] = _fetch(reader, symbol, fetch_start, end, "1Hour", code)
    for symbol in mover_symbols - set(universe):
        hours[symbol] = _fetch(reader, symbol, start - HOUR_HISTORY, end, "1Hour", code)
    mechanical = mechanical_strategies()

    def minute_bars_for(symbol):
        def fetch(signal_at, ready):
            return _fetch(reader, symbol, signal_at, ready, "1Min", code)
        return fetch

    movers = []
    for mover in reality.get("movers") or []:
        symbol = mover["symbol"]
        sector = sector_of(symbol, classified)
        by_strategy = {s.strategy_id: tradeable(s, symbol, hours.get(symbol) or [], day,
                                                now=now, minute_bars_for=minute_bars_for(symbol))
                       for s in mechanical}
        traded_today = entered.get(symbol) or []
        note = notes.get(f"MOVER:{day.isoformat()}:{symbol}")
        own_picks = picks.get(symbol) or []
        movers.append(json_safe({
            "symbol": symbol, "sector": sector, "return_pct": mover["return_pct"],
            "move_start_at": mover.get("move_start_at"), "top_up": mover.get("top_up"),
            "top_down": mover.get("top_down"), "big_move": mover.get("big_move"),
            "missed_by": mover.get("missed_by") or [],
            "pre_move": pre_move_facts(hours.get(symbol) or [], mover["move_start_at"])
            if mover.get("move_start_at") else {"status": "NO_MOVE_START"},
            "why": {"post_mortem": note, "knowability": knowability(
                note, mover.get("missed_by"))},
            "picked": {"picks": len(own_picks), "items": own_picks[:5]},
            "traded": {"entered": len(traded_today), "setup_ids": traded_today},
            "pullback": "PICKED" if own_picks else PULLBACK_UNDETERMINED,
            "strategies": by_strategy,
            "tradeable": mover_tradeable_summary(by_strategy, traded_today),
        }))
    return movers, hours, universe, mechanical


def strategy_hits(movers, mechanical):
    hits = {}
    for strategy in mechanical:
        rows = [(m, m["strategies"].get(strategy.strategy_id) or []) for m in movers]
        simulated_rows = [r["result"] for _, rs in rows for r in rs
                          if r["status"] == "SIMULATED" and r["result"]
                          and r["result"].get("net_r") is not None]
        nets = [D(str(r["net_r"])) for r in simulated_rows]
        hits[strategy.strategy_id] = {
            "stage": strategy.stage, "movers": len(movers),
            "movers_with_signal": sum(1 for _, rs in rows if rs),
            "signals": sum(len(rs) for _, rs in rows), "simulated": len(nets),
            "pending": sum(1 for _, rs in rows for r in rs if r["status"] == "PENDING"),
            "positive": sum(1 for v in nets if v > 0), "mean_net_r": _q(_mean(nets))}
    return hits


# --- MISSED_TRADEABLE_V1 ------------------------------------------------------------------------


def missed_ready_at(day):
    return day_bounds(day)[1] + core.ENTRY_FILL_WITHIN + HOLD + BAR_FETCH_BUFFER


def compute_missed(repository, reader, day, *, now):
    now = _aware(now)
    if now < missed_ready_at(day):
        raise BriefUnavailable("MISSED_NOT_READY")
    reality = _reality_or_refusal(repository, day, "MISSED")
    movers, _hours, _universe, mechanical = evaluate_movers(
        repository, reader, day, reality, now=now, code="MISSED_BARS_UNAVAILABLE")
    tag = (reality.get("regime") or {}).get("tag")
    with repository.connect() as conn:
        regime = recorded_regimes(conn, [day]).get(day.isoformat())
    tag = (regime or {}).get("tag") or tag or "UNKNOWN"
    lines = [{"symbol": m["symbol"], "sector": m["sector"], "return_pct": m["return_pct"],
              "big_move": m["big_move"], "picked": m["picked"]["picks"],
              "entered": m["traded"]["entered"], "pullback": m["pullback"],
              "strategies": m["strategies"], **m["tradeable"]} for m in movers]
    nets = [D(str(r["result"]["net_r"])) for m in movers for rows in m["strategies"].values()
            for r in rows if r["status"] == "SIMULATED" and r["result"]
            and r["result"].get("net_r") is not None]
    return json_safe({
        "missed_version": MISSED_VERSION, "day": day.isoformat(), "timezone": TIMEZONE,
        "day_tag": tag, "regime_parts": {k: ((regime or {}).get(k) or {}).get("label")
                                         for k in ("btc_trend", "btc_volatility",
                                                   "alt_breadth", "selloff")},
        "strategies": [s.strategy_id for s in mechanical], "fee_assumption": FEE_ASSUMPTION,
        "movers": lines,
        "totals": {"movers": len(lines),
                   "with_signal": sum(1 for line in lines if line["signals"]),
                   "simulated_entries": len(nets),
                   "positive_entries": sum(1 for v in nets if v > 0),
                   "mean_net_r": _q(_mean(nets)),
                   "sum_net_r": _q(sum(nets, D(0))) if nets else None,
                   "missed_tradeable": sum(1 for line in lines if line["missed_tradeable"]),
                   "picked": sum(1 for line in lines if line["picked"]),
                   "entered": sum(1 for line in lines if line["entered"])},
        "definition": "missed_tradeable: a mechanical strategy's simulated entry on the mover "
                      "completed with net R above zero and no paper trade on the coin was "
                      "entered that day",
        "computed_at": now, "limitations": list(LIMITATIONS),
    })


def _store_once(store, kind, body, key):
    with store.transaction() as conn:
        found = conn.execute("SELECT event_seq FROM lab.managed_events WHERE idempotency_key=%s",
                             (key,)).fetchone()
        if found is not None:
            return "ALREADY_RECORDED", found["event_seq"]
        row = store.event(conn, kind, body, key=key)
    return "RECORDED", row["event_seq"]


def record_missed(store, reader, day, *, now):
    existing = recorded_missed(store.repo, day)
    if existing is not None:
        return "ALREADY_RECORDED", existing["event_seq"]
    return _store_once(store, MISSED_EVENT, compute_missed(store.repo, reader, day, now=now),
                       missed_key(day))


def missed_days(now):
    """The days a run tries: ready ones of the last ``MISSED_LOOKBACK_DAYS``, oldest first."""
    today = _aware(now).astimezone(NY).date()
    return [today - timedelta(days=n) for n in range(MISSED_LOOKBACK_DAYS, 0, -1)
            if missed_ready_at(today - timedelta(days=n)) <= _aware(now)]


def missed_step(store, reader, now):
    """The nightly step: up to ``MISSED_BACKFILL_PER_RUN`` unrecorded ready days, oldest first;
    ``(code, {day: status})``."""
    details, code, attempted = {}, None, 0
    with store.repo.connect() as conn:
        done = {row["idempotency_key"] for row in conn.execute(
            "SELECT idempotency_key FROM lab.managed_events WHERE kind=%s",
            (MISSED_EVENT,)).fetchall()}
    for day in missed_days(now):
        if missed_key(day) in done:
            continue
        if attempted >= MISSED_BACKFILL_PER_RUN:
            details[day.isoformat()] = "DEFERRED"
            continue
        try:
            status, _ = record_missed(store, reader, day, now=now)
        except BriefUnavailable as exc:
            status = str(exc)
            if status not in NOT_APPLICABLE:
                code = code or status
        # A day that can never be measured (no universe) is refused before any bar read and
        # does not use up the run's budget; a recorded or failed day does.
        attempted += status not in NOT_APPLICABLE
        details[day.isoformat()] = status
    return code, details


# --- DAILY_BRIEF_V1 -----------------------------------------------------------------------------


def ours_section(repository, day, reality, excluded):
    """Our picks and trades on the day against the market (from the day's scorecard)."""
    from catalyst_lab.scorecard import recorded_scorecard

    row = recorded_scorecard(repository, day)
    coins = {c["symbol"]: c for c in reality.get("coins") or [] if c.get("status") == "MEASURED"}
    start, end = day_bounds(day)
    with repository.connect() as conn:
        picked = sorted({r["symbol"] for r in conn.execute(
            """SELECT body->>'symbol' AS symbol FROM lab.managed_events
            WHERE kind='RESEARCH_PACKET' AND body->>'report_schema_version'=%s
            AND (CASE WHEN body ? 'run_slot' THEN (body->>'run_slot')::timestamptz END) >= %s
            AND (CASE WHEN body ? 'run_slot' THEN (body->>'run_slot')::timestamptz END) < %s""",
            (REPORT_SCHEMA_V3, start, end)).fetchall()})
    returns = [D(str(coins[s]["return_pct"])) for s in picked if s in coins]
    universe = [D(str(c["return_pct"])) for c in coins.values()]
    market = {"picked_coins": len(picked), "picked_measured": len(returns),
              "picked_mean_day_return_pct": _q(_mean(returns)),
              "universe_median_day_return_pct": _q(median(universe)) if universe else None,
              "btc_return_pct": (reality.get("factors") or {}).get("btc_return_pct")}
    if row is None:
        return {"scorecard": "NOT_RECORDED", "against_market": market}
    overall = row["body"]["windows"]["1d"]["overall"]
    funnel = overall.get("funnel") or {}
    trades = overall.get("trades") or []
    kept = [t for t in trades if t.get("setup_id") not in excluded]
    nets = [D(str(t["r_net"])) for t in kept if t.get("r_net") is not None]
    return json_safe({
        "scorecard": "RECORDED", "scorecard_event_seq": row["event_seq"],
        "funnel": {k: funnel.get(k) for k in ("picks_sent", "accepted", "vetoed", "selected",
                                              "admitted", "triggered", "filled", "closed")},
        "trades_closed": len(trades), "excluded_trades": len(trades) - len(kept),
        "r_net_count": len(nets), "mean_r_net": _q(_mean(nets)),
        "sum_r_net": _q(sum(nets, D(0))) if nets else None,
        "r_net_scope": "FEE_VERIFIED_TRADES_ONLY_WITHOUT_STATS_EXCLUSIONS",
        "trades": [{"symbol": t.get("symbol"), "exit_kind": t.get("exit_kind"),
                    "r_net": t.get("r_net"), "gross_r": t.get("gross_r"),
                    "excluded": t.get("setup_id") in excluded} for t in trades],
        "against_market": market,
    })


def market_section(reality, regime, hour_rows):
    factors = reality.get("factors") or {}
    parts = {k: ((regime or {}).get(k) or {}).get("label")
             for k in ("btc_trend", "btc_volatility", "alt_breadth", "selloff")}
    selloffs = selloff_hours(hour_rows)
    measured = [r for r in hour_rows if r["median_return_pct"] is not None]
    worst = sorted(measured, key=lambda r: r["median_return_pct"])[:3]
    return json_safe({
        "regime_tag": (regime or {}).get("tag") or "NOT_RECORDED", "regime_parts": parts,
        "btc": factors.get("btc"), "eth": factors.get("eth"),
        "breadth": breadth(reality.get("coins") or []),
        "sectors": factors.get("sectors") or [],
        "total_volume_vs_7d_avg": factors.get("total_volume_vs_7d_avg"),
        "hours": hour_rows, "selloff_hours": [r["new_york_hour"] for r in selloffs],
        "selloff_at_pct": str(SELLOFF_AT_PCT), "worst_hours": worst,
        "words": market_words(factors, parts, breadth(reality.get("coins") or []), selloffs),
    })


TREND_WORDS = {"UP": "above its 20- and 50-day means", "DOWN": "below its 20- and 50-day means",
               "MIXED": "between its 20- and 50-day means"}
VOL_WORDS = {"LOW": "low", "NORMAL": "normal", "HIGH": "high"}


def market_words(factors, parts, spread, selloffs):
    """One plain sentence about the day from the recorded numbers (no reasons)."""
    btc, eth = factors.get("btc_return_pct"), factors.get("eth_return_pct")
    trend = TREND_WORDS.get(parts.get("btc_trend"), "with an unknown trend")
    vol = VOL_WORDS.get(parts.get("btc_volatility"), "unknown")
    text = (f"Bitcoin {btc if btc is not None else 'n/a'}%, Ether "
            f"{eth if eth is not None else 'n/a'}%; Bitcoin {trend}, volatility {vol}; "
            f"{spread['up']} coins up, {spread['down']} down, {spread['flat']} flat of "
            f"{spread['measured']} (median {spread['median_return_pct']}%)")
    if selloffs:
        text += f"; sell-off hour(s) at {', '.join(r['new_york_hour'] for r in selloffs)} " \
                "New York"
    return text + "."


def compute_brief(repository, reader, day, *, now):
    """The ``DAILY_BRIEF_V1`` body of New York ``day`` (not recorded; ``record_brief``)."""
    now = _aware(now)
    if now < day_bounds(day)[1] + BAR_FETCH_BUFFER:
        raise BriefUnavailable("BRIEF_DAY_NOT_OVER")
    reality = _reality_or_refusal(repository, day, "BRIEF")
    movers, hours, universe, mechanical = evaluate_movers(
        repository, reader, day, reality, now=now, code="BRIEF_BARS_UNAVAILABLE")
    with repository.connect() as conn:
        regime = recorded_regimes(conn, [day]).get(day.isoformat())
        excluded = set(excluded_setups(conn))
        notes = latest_post_mortems(conn)
    hour_rows = hour_medians({s: hours.get(s) or [] for s in universe}, universe, day)
    clusters = sector_clusters(movers)
    hits = strategy_hits(movers, mechanical)
    since = day - timedelta(days=POST_MORTEM_LOOKBACK_DAYS - 1)
    knowable_before = 0
    for key, note in notes.items():
        parts = key.split(":")
        if (len(parts) == 3 and parts[0] == "MOVER" and parts[1] >= since.isoformat()
                and parts[1] <= day.isoformat() and note.get("knowable_before_move") is True):
            knowable_before += 1
    pending = [m["symbol"] for m in movers if m["why"]["knowability"] == PENDING_POST_MORTEM]
    previous = recorded_missed(repository, day - timedelta(days=1))
    market = market_section(reality, regime, hour_rows)
    focus = focus_items(clusters=clusters, selloffs=selloff_hours(hour_rows), regime=regime,
                        knowable_before=knowable_before, pending_queue=len(pending),
                        strategy_hits=hits)
    body = json_safe({
        "brief_version": BRIEF_VERSION, "speed": SPEED, "day": day.isoformat(),
        "timezone": TIMEZONE, "computed_at": now,
        "reality_event": "MARKET_REALITY_V1", "market": market,
        "movers": {"count": len(movers), "items": movers, "sector_clusters": clusters,
                   "cluster_min": CLUSTER_MIN},
        "missed": {
            "outlook_misses": sorted(m["symbol"] for m in movers if m["missed_by"]),
            "knowable_and_missed": sorted(m["symbol"] for m in movers
                                          if m["why"]["knowability"] == "KNOWABLE_AND_MISSED"),
            "pending_post_mortem": sorted(pending),
            "tradeable_by_strategy": hits,
            "missed_tradeable_so_far": sorted(m["symbol"] for m in movers
                                              if m["tradeable"]["missed_tradeable"]),
            "final_record": "MISSED_TRADEABLE_V1 after every hold has passed",
            "previous_day_final": None if previous is None else {
                "day": previous["body"]["day"], "day_tag": previous["body"]["day_tag"],
                **previous["body"]["totals"],
                "missed_tradeable_symbols": [line["symbol"] for line in
                                             previous["body"]["movers"]
                                             if line["missed_tradeable"]]},
            "pullback_rule": PULLBACK_UNDETERMINED + " for movers nobody picked",
        },
        "ours": ours_section(repository, day, reality, excluded),
        "research_focus": focus,
        "agent_queue": {"pending_post_mortems": sorted(pending), "needs_agent": list(NEEDS_AGENT),
                        "computed_in_cloud": list(COMPUTED_IN_CLOUD)},
        "fee_assumption": FEE_ASSUMPTION,
        "strategies": [s.strategy_id for s in mechanical],
        "bars": {"source": BAR_SOURCE, "hour_history_hours": int(
            HOUR_HISTORY.total_seconds() // 3600)},
        "limitations": list(LIMITATIONS),
    })
    body["text"] = brief_text(body)
    return body


def brief_text(body):
    """The brief as plain lines (also the owner command's first part)."""
    market, missed, ours = body["market"], body["missed"], body["ours"]
    lines = [f"Daily brief {body['day']} ({body['brief_version']}, observe and explain; "
             "research attention only).",
             f"Market: {market['regime_tag']}. {market['words']}"]
    for cluster in body["movers"]["sector_clusters"]:
        lines.append(f"Cluster: {cluster['sector']} {cluster['direction']} "
                     f"({', '.join(cluster['symbols'])}, mean {cluster['mean_return_pct']}%).")
    for mover in body["movers"]["items"]:
        best = mover["tradeable"]["best_net_r"]
        lines.append(
            f"Mover {mover['symbol']} {mover['return_pct']}% ({mover['sector']}): why "
            f"{mover['why']['knowability']}"
            + (f" ({mover['why']['post_mortem']['cause']})" if mover['why']['post_mortem']
               else "")
            + f"; outlook missed by {', '.join(mover['missed_by']) or 'none'}; picked "
            f"{mover['picked']['picks']}, entered {mover['traded']['entered']}; mechanical "
            f"signals {mover['tradeable']['signals']}"
            + (f", best net R {best}" if best is not None else "")
            + (" (pending hold)" if mover["tradeable"]["pending"] else "")
            + (" MISSED TRADEABLE" if mover["tradeable"]["missed_tradeable"] else "") + ".")
    prev = missed.get("previous_day_final")
    if prev:
        lines.append(f"Final missed-tradeable {prev['day']} ({prev['day_tag']}): "
                     f"{prev['missed_tradeable']} of {prev['movers']} movers, "
                     f"{prev['simulated_entries']} simulated entries, mean net R "
                     f"{prev['mean_net_r']}.")
    if ours.get("scorecard") == "RECORDED":
        lines.append(f"Ours: {ours['funnel'].get('picks_sent')} picks sent, "
                     f"{ours['funnel'].get('selected')} selected, {ours['funnel'].get('filled')} "
                     f"filled; {ours['trades_closed']} closed ({ours['excluded_trades']} "
                     f"excluded), mean net R {ours['mean_r_net']} over {ours['r_net_count']}.")
    against = ours["against_market"]
    lines.append(f"Picked coins' mean day {against['picked_mean_day_return_pct']}% vs the "
                 f"universe median {against['universe_median_day_return_pct']}% (BTC "
                 f"{against['btc_return_pct']}%).")
    for item in body["research_focus"]:
        lines.append(f"Focus ({item['kind']}): {item['text']}")
    lines.append(f"Waiting for an agent: {len(body['agent_queue']['pending_post_mortems'])} "
                 "mover post-mortem(s).")
    return lines


def record_brief(store, reader, day, *, now):
    existing = recorded_brief(store.repo, day)
    if existing is not None:
        return "ALREADY_RECORDED", existing["event_seq"]
    return _store_once(store, BRIEF_EVENT, compute_brief(store.repo, reader, day, now=now),
                       brief_key(day))


def agent_view(body):
    """The brief as a research agent may see it: market data, movers, the missed-tradeable
    simulations and the focus; never the account's picks, trades or another agent's notes."""
    if body is None:
        return None
    movers = [{k: m.get(k) for k in ("symbol", "sector", "return_pct", "move_start_at",
                                     "big_move", "pre_move", "tradeable")}
              | {"knowability": m["why"]["knowability"]}
              for m in body["movers"]["items"]]
    missed = body["missed"]
    return {"brief_version": body["brief_version"], "day": body["day"],
            "market": {k: body["market"].get(k) for k in (
                "regime_tag", "regime_parts", "words", "breadth", "selloff_hours", "sectors")},
            "movers": movers, "sector_clusters": body["movers"]["sector_clusters"],
            "tradeable_by_strategy": missed["tradeable_by_strategy"],
            "previous_day_missed_tradeable": missed.get("previous_day_final"),
            "research_focus": body["research_focus"],
            "pending_post_mortems": body["agent_queue"]["pending_post_mortems"],
            "use": "Research attention only: change emphasis, never coverage or a trading rule."}


__all__ = [
    "BRIEF_EVENT", "BRIEF_VERSION", "BriefUnavailable", "MISSED_EVENT", "MISSED_VERSION",
    "NOT_APPLICABLE", "agent_view", "breadth", "brief_text", "compute_brief", "compute_missed",
    "focus_items", "hour_medians", "knowability", "missed_days", "missed_step",
    "pre_move_facts", "record_brief", "record_missed", "recorded_brief", "recorded_missed",
    "sector_clusters", "selloff_hours", "tradeable",
]
