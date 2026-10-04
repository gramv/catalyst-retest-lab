"""``AFTER_EXIT_PATH_V1`` and ``MANAGEMENT_CHANGE_CONTEXT_V1``: nightly, record-only facts about
each closed trade and each replayed management decision (package learning-loop2, 2026-10-03;
plan ``docs/TRADING-QUALITY-PLAN.md`` section 6, L4 and L6; owner direction 2026-10-03, the
deferred L-items (a) and (e)).

**Measurement only, paper trading only.** Nothing here places an order, reads a live quote,
gates an entry, changes a rule or reaches Jev. Alpaca's public, keyless crypto bars are read
after the fact; each record is one immutable ``lab.managed_events`` row (no setup), appended
once by key. No migration.

* **``AFTER_EXIT_PATH``** (key ``after-exit-path:<setup_id>``): what the price did after a
  closed trade's exit. The exit is the trade's last sell fill (``exit_at``) at its average sell
  price; for each horizon (``+1h``, ``+4h``, ``+8h``, ``+24h``) the 1-minute bars that start in
  ``[exit_at, exit_at + h)`` give the high, the low and the close (the last bar's close), each
  as a percent of the exit price and in R against it. R uses the trade's official-R scale:
  the admitted max entry minus the admitted initial stop -- the plan's stop for a
  ``CRYPTO_TRADE_PLAN_V1`` setup (``trade_plan.initial_levels``), the packet's otherwise. A
  trade is recorded once the 24 hours (plus the bar buffer) have passed; a horizon without a
  bar is ``NO_BARS`` (never filled in). ``ENGINEERING_TEST`` setups are never recorded.
* **``MANAGEMENT_CHANGE_CONTEXT``** (key ``management-change-context:<source_event_seq>``):
  for each recorded ``UNCHANGED_PLAN_REPLAY`` / ``DAY_REVIEW_DECISION_REPLAY``, where the
  decision happened: the price at the decision (the open of the first 1-minute bar at or after
  it, within 15 minutes), the new stop's distance below that price (a stop raise), the hours
  since the first entry fill, and the trade's ``MARKET_REGIME_V1`` day tag. The weekly review
  splits management value (actual official R minus the counterfactual) by these buckets.

Fail-closed: a bar read that fails leaves the trade or change for the next run (the step's
code is ``TRADE_PATHS_BARS_UNAVAILABLE``); data that cannot be read is counted ``invalid`` and
never guessed.
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab import trade_plan
from catalyst_lab.learning_replays import DAY_REPLAY_EVENT, REPLAY_EVENT, counterfactual_r
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market_regime import recorded_trade_regimes
from catalyst_lab.pick_outcomes import BAR_FETCH_BUFFER, ShadowDataError, parse_bars
from catalyst_lab.repository import json_safe
from catalyst_lab.result_dimensions import CELL_MINIMUM, regime_value
from catalyst_lab.unchanged_plan import setup_level_changes

D = Decimal
PATH_VERSION = "AFTER_EXIT_PATH_V1"
PATH_EVENT = "AFTER_EXIT_PATH"
CONTEXT_VERSION = "MANAGEMENT_CHANGE_CONTEXT_V1"
CONTEXT_EVENT = "MANAGEMENT_CHANGE_CONTEXT"
HORIZONS = ((1, "+1h"), (4, "+4h"), (8, "+8h"), (24, "+24h"))
FULL = timedelta(hours=24)
LOOKBACK = timedelta(days=40)
MAX_PATHS_PER_RUN = 100
MAX_CONTEXTS_PER_RUN = 200
PRICE_WITHIN = timedelta(minutes=15)
BAR_SOURCE = "ALPACA_PUBLIC_CRYPTO_BARS_V1BETA3_US"
BARS_UNAVAILABLE = "TRADE_PATHS_BARS_UNAVAILABLE"
# Buckets of MANAGEMENT_CHANGE_CONTEXT_V1 (the 10-01 finding: raises sat ~1.1% under price).
DISTANCE_EDGES = ((D(1), "0-1%"), (D(2), "1-2%"), (D(3), "2-3%"))
DISTANCE_ORDER = ("0-1%", "1-2%", "2-3%", "3%+", "NO_STOP_CHANGE", "UNKNOWN")
TIME_EDGES = ((D(1), "0-1h"), (D(4), "1-4h"), (D(8), "4-8h"))
TIME_ORDER = ("0-1h", "1-4h", "4-8h", "8h+", "UNKNOWN")
RECOVERY_R = D(1)  # A stop-out whose next-24h high reached the exit + 1R "recovered".
FOUR = D("0.0001")
LIMITATIONS = (
    "1-minute bars from Alpaca's public crypto feed: a minute without a trade has no bar.",
    "R after the exit is measured from the average exit price on the trade's official-R scale "
    "(admitted max entry minus admitted initial stop); fees are not part of it.",
)


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


def _ratio(numerator, denominator):
    with localcontext() as context:
        context.prec = 40
        return numerator / denominator


def _mean(values):
    values = list(values)
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return _q(sum(values, D(0)) / len(values))


# --- Pure measures --------------------------------------------------------------------------------


def path_after_exit(bars, exit_at, exit_price, risk_per_coin):
    """``{label: {...}}`` per horizon from ascending 1-minute ``bars`` (pure)."""
    result = {}
    for hours, label in HORIZONS:
        inside = [b for b in bars if exit_at <= b.start < exit_at + timedelta(hours=hours)]
        if not inside:
            result[label] = {"status": "NO_BARS"}
            continue
        values = {"high": max(b.high for b in inside), "low": min(b.low for b in inside),
                  "close": inside[-1].close}
        entry = {"status": "MEASURED", "bars": len(inside)}
        for name, price in values.items():
            entry[f"{name}_price"] = format(price, "f")
            entry[f"{name}_pct"] = _q(_ratio(price - exit_price, exit_price) * 100)
            entry[f"{name}_r"] = _q(_ratio(price - exit_price, risk_per_coin))
        result[label] = entry
    return result


def distance_bucket(price, new_stop):
    """The new stop's distance below the price at the decision, in percent, bucketed."""
    if new_stop is None:
        return "NO_STOP_CHANGE"
    if price is None or price <= 0:
        return "UNKNOWN"
    distance = _ratio(price - new_stop, price) * 100
    for edge, label in DISTANCE_EDGES:
        if distance < edge:
            return label
    return "3%+"


def time_bucket(hours):
    if hours is None:
        return "UNKNOWN"
    for edge, label in TIME_EDGES:
        if hours < edge:
            return label
    return "8h+"


# --- Ledger reads ---------------------------------------------------------------------------------


def closed_with_exit(conn, since):
    """Non-engineering closed managed trades whose last sell fill is at or after ``since``."""
    rows = conn.execute(
        """SELECT s.setup_id, s.symbol, s.record_json, t.body AS state,
        (SELECT min(f.filled_at) FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
         AND f.side='buy') AS entry_at,
        (SELECT max(f.filled_at) FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
         AND f.side='sell') AS exit_at,
        (SELECT sum(f.qty*f.price)/nullif(sum(f.qty),0) FROM lab.managed_fills f
         WHERE f.setup_id=s.setup_id AND f.side='sell') AS exit_price
        FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
        WHERE s.cohort=%s AND t.body->>'state'='CLOSED' ORDER BY s.event_seq""", (COHORT,),
    ).fetchall()
    return [row for row in rows if not is_engineering(row["record_json"])
            and row["exit_at"] is not None and row["entry_at"] is not None
            and row["exit_at"] >= since]


def risk_basis(record_json, state):
    """``(max entry, initial stop, basis)``: official R's scale (the plan's stop where the
    setup was admitted under CRYPTO_TRADE_PLAN_V1). Raises on unreadable levels."""
    levels = (record_json or {})["levels"]
    traded = trade_plan.initial_levels(levels, state or {})
    entry, stop = D(str(traded["max_entry_price"])), D(str(traded["stop"]))
    if entry - stop <= 0:
        raise ShadowDataError("NONPOSITIVE_RISK_DENOMINATOR")
    basis = "TRADE_PLAN_STOP" if trade_plan.active(state or {}) else "ADMITTED_PACKET_STOP"
    return entry, stop, basis


def _recorded_keys(conn, kind):
    return {row["idempotency_key"] for row in conn.execute(
        "SELECT idempotency_key FROM lab.managed_events WHERE kind=%s", (kind,)).fetchall()}


def path_key(setup_id):
    return f"after-exit-path:{setup_id}"


def context_key(source_event_seq):
    return f"management-change-context:{source_event_seq}"


def _store_once(store, kind, body, key):
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                        (key,)).fetchone():
            return False
        store.event(conn, kind, body, key=key)
    return True


def exit_reason(state):
    return (state or {}).get("reason") or (state or {}).get("exit_requested")


# --- The nightly step -----------------------------------------------------------------------------


def record_paths(store, reader, *, now, limit=MAX_PATHS_PER_RUN):
    """Every closed trade of the last 40 days whose 24 hours after the exit have passed,
    recorded once; counts by outcome."""
    from catalyst_lab.scorecard import exit_kind

    now = _aware(now)
    summary = {"recorded": 0, "already_recorded": 0, "not_yet_ready": 0, "failed": 0,
               "invalid": 0, "deferred": 0}
    with store.repo.connect() as conn:
        trades = closed_with_exit(conn, now - LOOKBACK)
        done = _recorded_keys(conn, PATH_EVENT)
    work = []
    for row in trades:
        if path_key(row["setup_id"]) in done:
            summary["already_recorded"] += 1
        elif now < _aware(row["exit_at"]) + FULL + BAR_FETCH_BUFFER:
            summary["not_yet_ready"] += 1
        else:
            work.append(row)
    summary["deferred"] = max(len(work) - limit, 0)
    for row in work[:limit]:
        exit_at = _aware(row["exit_at"])
        try:
            entry, stop, basis = risk_basis(row["record_json"], row["state"])
            exit_price = D(str(row["exit_price"]))
        except (KeyError, TypeError, ValueError, InvalidOperation, ShadowDataError):
            summary["invalid"] += 1
            continue
        try:
            bars = parse_bars(reader.minute_bars(row["symbol"], exit_at,
                                                 exit_at + FULL + BAR_FETCH_BUFFER))
        except Exception:  # noqa: BLE001 -- a read or parse failure: retried next run.
            summary["failed"] += 1
            continue
        reason = exit_reason(row["state"])
        body = json_safe({
            "path_version": PATH_VERSION, "setup_id": str(row["setup_id"]),
            "symbol": row["symbol"], "entry_at": _aware(row["entry_at"]), "exit_at": exit_at,
            "exit_price": format(exit_price, "f"), "exit_reason": reason,
            "exit_kind": exit_kind(reason), "arm": (row["state"] or {}).get("arm"),
            "r_scale": {"max_entry": format(entry, "f"), "initial_stop": format(stop, "f"),
                        "risk_per_coin": format(entry - stop, "f"), "basis": basis},
            "horizons": path_after_exit(bars, exit_at, exit_price, entry - stop),
            "bars": {"source": BAR_SOURCE, "timeframe": "1Min", "fetch_start": exit_at,
                     "fetch_end": exit_at + FULL + BAR_FETCH_BUFFER},
            "computed_at": now, "limitations": list(LIMITATIONS),
        })
        if _store_once(store, PATH_EVENT, body, path_key(row["setup_id"])):
            summary["recorded"] += 1
        else:
            summary["already_recorded"] += 1
    return summary


def _replays(conn, since_seq=0):
    return conn.execute(
        """SELECT event_seq, kind, body FROM lab.managed_events WHERE kind IN (%s, %s)
        AND setup_id IS NULL AND event_seq > %s ORDER BY event_seq""",
        (REPLAY_EVENT, DAY_REPLAY_EVENT, since_seq),
    ).fetchall()


def _source_seq(kind, body):
    return body.get("source_event_seq")


def record_contexts(store, reader, *, now, limit=MAX_CONTEXTS_PER_RUN):
    """A ``MANAGEMENT_CHANGE_CONTEXT`` for every recorded replay without one; counts."""
    now = _aware(now)
    summary = {"recorded": 0, "already_recorded": 0, "failed": 0, "invalid": 0,
               "deferred": 0}
    with store.repo.connect() as conn:
        replays = _replays(conn)
        done = _recorded_keys(conn, CONTEXT_EVENT)
    todo = [r for r in replays if _source_seq(r["kind"], r["body"]) is not None
            and context_key(_source_seq(r["kind"], r["body"])) not in done]
    summary["already_recorded"] = len(replays) - len(todo)
    summary["deferred"] = max(len(todo) - limit, 0)
    regimes = recorded_trade_regimes(store.repo, {r["body"].get("setup_id") for r in todo})
    changes_of, entries = {}, {}
    for replay in todo[:limit]:
        body, seq = replay["body"], _source_seq(replay["kind"], replay["body"])
        setup_id, symbol = body.get("setup_id"), body.get("symbol")
        try:
            at = _aware(body.get("change_at") or body.get("at"))
            if setup_id not in changes_of:
                changes_of[setup_id] = {c.source_event_seq: c
                                        for c in setup_level_changes(store.repo, setup_id)}
                with store.repo.connect() as conn:
                    entries[setup_id] = conn.execute(
                        """SELECT min(filled_at) AS at FROM lab.managed_fills
                        WHERE setup_id=%s AND side='buy'""", (setup_id,)).fetchone()["at"]
        except (KeyError, TypeError, ValueError):
            summary["invalid"] += 1
            continue
        change = changes_of[setup_id].get(seq)
        if replay["kind"] == DAY_REPLAY_EVENT:
            new_stop = body.get("new_stop")
            new_stop = D(str(new_stop)) if new_stop not in (None, "") else None
            decision = "DAY_REVIEW_" + str(body.get("decision"))
        else:
            new_stop = change.new_stop if change is not None else None
            decision = body.get("change_kind")
        old_stop = body.get("original_stop") or body.get("old_stop")
        if new_stop is not None and old_stop is not None and new_stop == D(str(old_stop)):
            new_stop = None  # The stop did not move (a target raise).
        try:
            bars = parse_bars(reader.minute_bars(symbol, at, at + PRICE_WITHIN))
        except Exception:  # noqa: BLE001 -- retried next run.
            summary["failed"] += 1
            continue
        price = bars[0].open if bars else None
        entry_at = entries.get(setup_id)
        hours = (_ratio(D(str((at - _aware(entry_at)).total_seconds())), D(3600))
                 if entry_at is not None else None)
        regime = regimes.get(setup_id)
        record = json_safe({
            "context_version": CONTEXT_VERSION, "setup_id": setup_id, "symbol": symbol,
            "replay_kind": replay["kind"], "replay_event_seq": replay["event_seq"],
            "source_event_seq": seq, "decision_type": decision, "at": at,
            "price_at_decision": format(price, "f") if price is not None else None,
            "price_status": "MEASURED" if price is not None else "NO_BAR_WITHIN_15_MINUTES",
            "new_stop": format(new_stop, "f") if new_stop is not None else None,
            "new_stop_distance_pct": _q(_ratio(price - new_stop, price) * 100)
            if price is not None and new_stop is not None else None,
            "distance_bucket": distance_bucket(price, new_stop),
            "hours_in_trade": _q(hours), "time_bucket": time_bucket(hours),
            "day_tag": regime_value(regime, "day_tag"),
            "computed_at": now,
        })
        if _store_once(store, CONTEXT_EVENT, record, context_key(seq)):
            summary["recorded"] += 1
        else:
            summary["already_recorded"] += 1
    return summary


def record_trade_paths(store, reader, *, now):
    """The nightly step: both record kinds; ``(code, details)``."""
    paths = record_paths(store, reader, now=now)
    contexts = record_contexts(store, reader, now=now)
    details = {f"path_{k}": v for k, v in paths.items()}
    details.update({f"ctx_{k}": v for k, v in contexts.items()})
    failed = paths["failed"] or contexts["failed"]
    return (BARS_UNAVAILABLE if failed else None), details


# --- Reading (the weekly review and the brief) ----------------------------------------------------


def recorded_paths(repository, *, until=None):
    with repository.connect() as conn:
        rows = conn.execute("SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (PATH_EVENT,)).fetchall()
    return [row["body"] for row in rows
            if until is None or _aware(row["body"]["exit_at"]) < until]


def after_exit_summary(paths, excluded=frozenset(), minimum=CELL_MINIMUM):
    """By exit kind: how far price went after the exit (+24h high and close in R), and how
    many stop-outs "recovered" (+24h high at least 1R above the exit). Trades of
    ``STATS_EXCLUSION_V1`` days are left out (counted)."""
    kept = [p for p in paths if p["setup_id"] not in excluded]
    groups = {}
    for path in kept:
        groups.setdefault(path.get("exit_kind") or "OTHER", []).append(path)
    by_kind = {}
    for kind in sorted(groups):
        members = groups[kind]
        day = [p["horizons"].get("+24h") or {} for p in members]
        measured = [h for h in day if h.get("status") == "MEASURED"]
        highs = [D(str(h["high_r"])) for h in measured]
        closes = [D(str(h["close_r"])) for h in measured]
        recovered = sum(1 for v in highs if v >= RECOVERY_R)
        by_kind[kind] = {
            "trades": len(members), "measured_24h": len(measured),
            "mean_high_r_24h": _mean(highs), "mean_close_r_24h": _mean(closes),
            "high_at_least_1r": recovered,
            "high_at_least_1r_share": _q(D(recovered) / len(measured)) if measured else None,
            "close_above_exit": sum(1 for v in closes if v > 0),
            "by_horizon_mean_close_r": {
                label: _mean(D(str(p["horizons"][label]["close_r"])) for p in members
                             if (p["horizons"].get(label) or {}).get("status") == "MEASURED")
                for _, label in HORIZONS},
            "status": "MEASURED" if len(measured) >= minimum else "NOT_ENOUGH_DATA",
        }
    return {"version": PATH_VERSION, "trades": len(kept),
            "excluded_trades": len(paths) - len(kept), "minimum": minimum,
            "recovery_rule": "+24h high at least 1R above the exit", "by_exit_kind": by_kind}


def recorded_contexts(repository):
    with repository.connect() as conn:
        rows = conn.execute("SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (CONTEXT_EVENT,)).fetchall()
    return {row["body"]["source_event_seq"]: row["body"] for row in rows}


def management_split(repository, trades, excluded=frozenset(), minimum=CELL_MINIMUM):
    """Management value (actual official R minus the replayed counterfactual's net R) by
    decision type x distance of the new stop below price, x time in trade, x day tag
    (``MANAGEMENT_CHANGE_CONTEXT_V1``); ``trades``: ``{setup_id: {r_net, r_basis?}}`` of the
    evidence, without the ``STATS_EXCLUSION_V1`` trades in ``excluded``."""
    contexts = recorded_contexts(repository)
    with repository.connect() as conn:
        replays = _replays(conn)
    rows = []
    for replay in replays:
        body = replay["body"]
        trade = trades.get(body.get("setup_id"))
        if trade is None or body.get("setup_id") in excluded or trade.get("r_net") is None:
            continue
        counterfactual = counterfactual_r(replay["kind"], body, trade.get("r_basis"))
        if counterfactual is None:
            continue
        context = contexts.get(_source_seq(replay["kind"], body)) or {}
        rows.append({"decision": context.get("decision_type") or (
            "DAY_REVIEW_" + str(body.get("decision")) if replay["kind"] == DAY_REPLAY_EVENT
            else body.get("change_kind")),
            "distance": context.get("distance_bucket") or "NO_CONTEXT",
            "time": context.get("time_bucket") or "NO_CONTEXT",
            "day_tag": context.get("day_tag") or "NO_CONTEXT",
            "difference": trade["r_net"] - counterfactual})

    def cells(key, order=None):
        grouped = {}
        for row in rows:
            grouped.setdefault((row["decision"], key(row)), []).append(row["difference"])
        result = {}
        for (decision, value), values in sorted(grouped.items(), key=lambda kv: (
                kv[0][0], order.index(kv[0][1]) if order and kv[0][1] in order else 99,
                kv[0][1])):
            result.setdefault(decision, {})[value] = {
                "changes": len(values), "mean_r_difference": _mean(values),
                "helped": sum(1 for v in values if v > 0),
                "hurt": sum(1 for v in values if v < 0),
                "status": "MEASURED" if len(values) >= minimum else "NOT_ENOUGH_DATA"}
        return result

    return {"version": CONTEXT_VERSION, "minimum": minimum, "changes": len(rows),
            "effect": "ACTUAL_OFFICIAL_R_MINUS_COUNTERFACTUAL_NET_R (positive = the decision "
                      "helped)",
            "by_stop_distance": cells(lambda r: r["distance"], DISTANCE_ORDER),
            "by_time_in_trade": cells(lambda r: r["time"], TIME_ORDER),
            "by_day_tag": cells(lambda r: r["day_tag"])}


__all__ = [
    "CONTEXT_EVENT", "CONTEXT_VERSION", "HORIZONS", "PATH_EVENT", "PATH_VERSION",
    "after_exit_summary", "distance_bucket", "management_split", "path_after_exit",
    "record_contexts", "record_paths", "record_trade_paths", "recorded_paths", "risk_basis",
    "time_bucket",
]
