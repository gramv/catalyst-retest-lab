"""``STRATEGY_SHADOW_V1``: mechanical strategies forward-tested in shadow (package strategy-c1,
2026-10-03; plan docs/TRADING-QUALITY-PLAN.md section 5, moves 2 and 5; guide
docs/STRATEGY-PLUGINS.md).

**No orders, no broker calls, no Jev.** For every registered mechanical strategy at stage
``SHADOW`` (today ``BREAKOUT_7D_VOL2X_V1``) the nightly jobs run (``learning_jobs``, inside the
``shadow_outcomes`` step) does two things on Alpaca's public, keyless crypto bars:

1. **Signals.** Over the coins of the latest recorded research universe (the tradable Alpaca USD
   pairs a report-V3 intake or an outlook recorded; ``market_reality.recorded_universe``), the
   strategy's ``signals`` runs on completed 1-hour bars and every signal whose time falls in the
   last ``LOOKBACK`` (40 days; the first run is the backfill) is appended once as a
   ``STRATEGY_SHADOW_SIGNAL`` event (key: strategy, coin, signal time). The rule is stateless,
   so a rerun or a later run finds the same signals and records nothing twice.
2. **Outcomes.** Each recorded signal whose entry window plus 24-hour hold has passed is
   simulated once on 1-minute bars with the strategy's ``simulate`` -- the decision core's
   marketable fill, CRYPTO_TRADE_PLAN_V1's stop and target and the shared exit walk
   (``strategies.core``), with ``PICK_SHADOW_OUTCOME_V1``'s fee assumption (Alpaca tier-1 taker,
   0.25% on both legs) -- and appended as a ``STRATEGY_SHADOW_OUTCOME`` event. A signal recorded
   since package strategy-c2 carries ``HALF_SPREAD_PLUS_VOLATILITY_V1``'s slippage estimate;
   its outcome adds ``net_r_after_slippage`` (the history tester's cost model, one code path).

A coin whose bars cannot be read is counted and retried on the next run (the step fails with
``STRATEGY_SHADOW_BARS_UNAVAILABLE``, the other coins still proceed). Outcomes are capped per run
(``MAX_OUTCOMES``); the rest wait for the next run.

``strategy_section`` reports the results per strategy for the scorecard and the weekly review:
``live_paper`` (closed paper trades by their stamped strategy, official R after verified fees) and
``shadow`` (simulated outcomes), always labelled apart: shadow R is a 1-minute-bar approximation
with an assumed fee, never a fill.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from catalyst_lab import strategies
from catalyst_lab.market_reality import recorded_universe
from catalyst_lab.pick_outcomes import (
    BAR_FETCH_BUFFER,
    FEE_ASSUMPTION,
    METHOD_LABEL,
    TAKER_FEE_TIER1,
    ShadowDataError,
    parse_bars,
)
from catalyst_lab.repository import json_safe
from catalyst_lab.result_dimensions import CELL_MINIMUM, grouped
from catalyst_lab.strategies import core

D = Decimal
SHADOW_VERSION = "STRATEGY_SHADOW_V1"
SIGNAL_EVENT = "STRATEGY_SHADOW_SIGNAL"
OUTCOME_EVENT = "STRATEGY_SHADOW_OUTCOME"
LOOKBACK = timedelta(days=40)
HOLD = timedelta(hours=24)
MAX_OUTCOMES = 250
BAR_SOURCE = "ALPACA_PUBLIC_V1BETA3_CRYPTO_US_BARS"
BARS_UNAVAILABLE = "STRATEGY_SHADOW_BARS_UNAVAILABLE"
SHADOW_LABEL = ("SHADOW: simulated on Alpaca's public 1-minute bars with an assumed taker fee; "
                "no order was placed.")
LIVE_LABEL = ("LIVE_PAPER: closed paper-account trades by their recorded strategy; R after "
              "verified fees only.")
TRADED = frozenset({core.STOP, core.TARGET, core.HOLD_EXIT})
FOUR = D("0.0001")


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return parsed.astimezone(UTC)


def _q(value):
    if value is None:
        return None
    rounded = value.quantize(FOUR, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def signal_key(strategy_id, symbol, signal_at):
    return f"strategy-shadow-signal:{strategy_id}:{symbol}:{_aware(signal_at).isoformat()}"


def outcome_key(strategy_id, symbol, signal_at):
    return f"strategy-shadow-outcome:{strategy_id}:{symbol}:{_aware(signal_at).isoformat()}"


def ready_at(signal_at):
    """When a signal's outcome is knowable: the fill window, the hold and the bar buffer."""
    return _aware(signal_at) + core.ENTRY_FILL_WITHIN + HOLD + BAR_FETCH_BUFFER


def _keys(conn):
    return {row["idempotency_key"] for row in conn.execute(
        "SELECT idempotency_key FROM lab.managed_events WHERE kind IN (%s,%s)",
        (SIGNAL_EVENT, OUTCOME_EVENT)).fetchall()}


def _record(store, kind, body, key):
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                        (key,)).fetchone():
            return False
        store.event(conn, kind, body, key=key)
    return True


def record_signals(store, reader, strategy, symbols, *, now, lookback, keys, summary,
                   bar_source=BAR_SOURCE):
    until = now.replace(minute=0, second=0, microsecond=0)  # Completed hourly bars only.
    since = until - lookback
    # The bars the strategy declares it needs (the breakout's 216 hours by default).
    fetch_start = since - strategy.history_hours * core.HOUR
    for symbol in symbols:
        try:
            bars = parse_bars(reader.bars(symbol, fetch_start, until, "1Hour"))
        except Exception:  # noqa: BLE001 -- one coin's read or parse failure; retried next run.
            summary["signal_fail"] += 1
            continue
        for proposal in strategy.signals(bars, {"symbol": symbol, "since": since,
                                                "until": until}):
            key = signal_key(strategy.strategy_id, symbol, proposal["signal_at"])
            if key in keys:
                summary["signals_seen"] += 1
                continue
            body = json_safe({
                "shadow_version": SHADOW_VERSION, "registry_version": strategies.REGISTRY_VERSION,
                "strategy_id": strategy.strategy_id, "strategy_stage": strategy.stage,
                "symbol": symbol, "signal_at": proposal["signal_at"], "proposal": proposal,
                "bars": {"source": bar_source, "timeframe": "1Hour",
                         "fetch_start": fetch_start, "fetch_end": until},
                "orders": "NONE_SHADOW_ONLY",
            })
            if _record(store, SIGNAL_EVENT, body, key):
                summary["signals"] += 1
            keys.add(key)


def pending_signals(repository, keys, *, now):
    """Recorded signals without an outcome whose outcome is knowable now, oldest first."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT event_seq, body FROM lab.managed_events WHERE kind=%s
            ORDER BY body->>'signal_at', event_seq""", (SIGNAL_EVENT,)).fetchall()
    return [row for row in rows
            if outcome_key(row["body"]["strategy_id"], row["body"]["symbol"],
                           row["body"]["signal_at"]) not in keys
            and ready_at(row["body"]["signal_at"]) <= now]


def record_outcome(store, reader, row, *, now, summary):
    body = row["body"]
    try:
        strategy = strategies.get(body["strategy_id"])
    except ValueError:
        summary["outcomes_unknown"] += 1
        return
    signal_at = _aware(body["signal_at"])
    try:
        bars = parse_bars(reader.minute_bars(body["symbol"], signal_at, ready_at(signal_at)))
    except Exception:  # noqa: BLE001 -- retried on the next run.
        summary["outcome_fail"] += 1
        return
    try:
        result = strategy.simulate(body["proposal"], bars, fee_rate=TAKER_FEE_TIER1)
    except ShadowDataError:
        summary["outcomes_invalid"] += 1
        return
    outcome = json_safe({
        "shadow_version": SHADOW_VERSION, "strategy_id": strategy.strategy_id,
        "symbol": body["symbol"], "signal_at": body["signal_at"],
        "signal_event_seq": row["event_seq"], "method_label": METHOD_LABEL,
        "fee_assumption": FEE_ASSUMPTION, "plan_rules": strategy.plan_rules,
        "result": result, "data_complete": result["outcome"] != core.DATA_INCOMPLETE,
        "computed_at": now, "orders": "NONE_SHADOW_ONLY",
    })
    if _record(store, OUTCOME_EVENT, outcome,
               outcome_key(strategy.strategy_id, body["symbol"], body["signal_at"])):
        summary["outcomes"] += 1


def run_strategy_shadow(store, reader, *, now, lookback=LOOKBACK, max_outcomes=MAX_OUTCOMES,
                        symbols=None, bar_source=BAR_SOURCE):
    """One pass: new signals of every shadow strategy, then the outcomes now knowable.

    ``symbols`` (package oss-packaging, the local simulated venue): the coins to scan instead of
    the latest recorded research universe; ``bar_source`` names the reader's bars in each signal
    (``sample_market``'s synthetic bars are labelled as such). Both default to the nightly
    jobs' behaviour.

    Returns ``(code, summary)``: ``code`` is ``BARS_UNAVAILABLE`` when any read failed (the
    rest still recorded), else None."""
    now = _aware(now)
    summary = Counter()
    # Drop-in plug-ins (package plugin-c3): a configured folder and installed entry points join
    # the registry before the shadow strategies are read; a failing plug-in is counted only.
    plugin_errors = strategies.ensure_plugins_loaded().errors
    if plugin_errors:
        summary["plugin_errors"] = len(plugin_errors)
    with store.repo.connect() as conn:
        universe = recorded_universe(conn, now) if symbols is None else None
        keys = _keys(conn)
    if symbols is None:
        symbols = sorted(set(((universe["universe"] or {}).get("symbols") or [])
                             if universe else []))
    else:
        symbols = sorted(set(symbols))
    summary["coins"] = len(symbols)
    for strategy in strategies.mechanical(strategies.SHADOW):
        if symbols:
            record_signals(store, reader, strategy, symbols, now=now, lookback=lookback,
                           keys=keys, summary=summary, bar_source=bar_source)
    pending = pending_signals(store.repo, keys, now=now)
    for row in pending[:max_outcomes]:
        record_outcome(store, reader, row, now=now, summary=summary)
    summary["outcomes_deferred"] = max(len(pending) - max_outcomes, 0)
    details = {k: summary[k] for k in (
        "coins", "signals", "signals_seen", "signal_fail",
        "outcomes", "outcome_fail", "outcomes_invalid",
        "outcomes_unknown", "outcomes_deferred")}
    if not symbols:
        details["universe"] = "STRATEGY_SHADOW_UNIVERSE_UNAVAILABLE"
    failed = summary["signal_fail"] or summary["outcome_fail"]
    return (BARS_UNAVAILABLE if failed else None), details


# --- Reporting (read-only) ------------------------------------------------------------------------


def shadow_rows(repository, *, start=None, end):
    """Recorded outcomes whose signal time is in ``[start, end)`` and the signals still pending
    there, by strategy."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT kind, body FROM lab.managed_events WHERE kind IN (%s,%s)
            ORDER BY event_seq""", (SIGNAL_EVENT, OUTCOME_EVENT)).fetchall()
    outcomes, signals = {}, {}
    for row in rows:
        at = _aware(row["body"]["signal_at"])
        if (start is not None and at < start) or at >= end:
            continue
        key = (row["body"]["strategy_id"], row["body"]["symbol"], at)
        (outcomes if row["kind"] == OUTCOME_EVENT else signals)[key] = row["body"]
    return outcomes, signals


def shadow_cell(outcomes, signals, minimum=CELL_MINIMUM):
    """One strategy's shadow figures: counts by outcome, then gross and net R over the
    simulated trades with a complete walk (wins by gross R above zero)."""
    results = [o["result"] for o in outcomes]
    traded = [r for r in results if r.get("outcome") in TRADED and r.get("net_r") is not None]
    gross = [D(str(r["gross_r"])) for r in traded]
    net = [D(str(r["net_r"])) for r in traded]

    def mean(values):
        if not values:
            return None
        with localcontext() as context:
            context.prec = 40
            return _q(sum(values, D(0)) / len(values))

    wins = sum(1 for v in gross if v > 0)
    done = {o["_key"] for o in outcomes}
    # HALF_SPREAD_PLUS_VOLATILITY_V1 (strategy-c2): the outcomes whose signal carried a slippage
    # estimate (signals recorded before it have none and are not counted here).
    slipped = [D(str(r["net_r_after_slippage"])) for r in traded
               if r.get("net_r_after_slippage") is not None]
    return {
        "label": "SHADOW", "signals": len(signals), "outcomes": len(outcomes),
        "pending": sum(1 for k in signals if k not in done),
        "outcome_counts": dict(sorted(Counter(r.get("outcome") for r in results).items())),
        "trades": len(traded), "mean_gross_r": mean(gross), "mean_net_r": mean(net),
        "sum_net_r": _q(sum(net, D(0))) if net else None,
        "slippage_trades": len(slipped), "mean_net_r_after_slippage": mean(slipped),
        "win_rate": _q(D(wins) / len(gross)) if gross else None,
        "status": "MEASURED" if len(traded) >= minimum else "NOT_ENOUGH_DATA",
    }


def shadow_cells(repository, *, start=None, end, minimum=CELL_MINIMUM):
    outcomes, signals = shadow_rows(repository, start=start, end=end)
    cells = {}
    for strategy in sorted({k[0] for k in outcomes} | {k[0] for k in signals}):
        mine = [{**body, "_key": key} for key, body in outcomes.items() if key[0] == strategy]
        cells[strategy] = shadow_cell(mine, [k for k in signals if k[0] == strategy], minimum)
    return cells


def strategy_section(repository, items, *, start=None, end, minimum=CELL_MINIMUM):
    """Results per strategy: ``live_paper`` (``items``: closed trades as ``result_dimensions``
    reads them, with ``strategy_id``) and ``shadow``, labelled apart, plus each registered
    strategy's stage and its distance to the ladder's shadow minimum."""
    shadow = shadow_cells(repository, start=start, end=end, minimum=minimum)
    ladder = {}
    for record in strategies.records():
        cell = shadow.get(record["strategy_id"])
        trades = cell["trades"] if cell else 0
        ladder[record["strategy_id"]] = {
            "stage": record["stage"], "sources": record["sources"],
            "shadow_trades": trades, "shadow_minimum": minimum,
            "shadow_minimum_met": trades >= minimum if record["stage"] == strategies.SHADOW
            else None,
        }
    return {
        "registry_version": strategies.REGISTRY_VERSION, "shadow_version": SHADOW_VERSION,
        "labels": {"live_paper": LIVE_LABEL, "shadow": SHADOW_LABEL},
        "live_paper": grouped(list(items), lambda i: i.get("strategy_id")
                              or strategies.DEFAULT_STRATEGY_ID, minimum),
        "shadow": shadow, "ladder": ladder,
    }


__all__ = ["LOOKBACK", "OUTCOME_EVENT", "SHADOW_VERSION", "SIGNAL_EVENT", "outcome_key",
           "ready_at", "run_strategy_shadow", "shadow_cells", "signal_key", "strategy_section"]
