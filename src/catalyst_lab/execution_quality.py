"""``EXECUTION_QUALITY_V1``: per-trade execution quality, and its summary by execution version
(package exec-d, 2026-10-03; plan docs/TRADING-QUALITY-PLAN.md phase D).

**A measurement definition, never a trading rule. Paper trading only.** Read-only over the
ledger: fills (``lab.managed_fills``, with their resolved fee evidence), the broker
acknowledgements and order links of the setup's own authorized decisions, and the setup's state.

Per trade (``trade_execution``):

- ``entry_fee_rate`` / ``exit_fee_rate``: verified fee USD over notional of the buy / sell fills
  (null unless every such fill has its fee).
- ``entry_liquidity``: each buy fill is ``MAKER`` when its fee rate is at or under
  ``LIQUIDITY_SPLIT`` (0.20%, the midpoint of Alpaca's tier-1 crypto maker 0.15% and taker
  0.25%), ``TAKER`` above it, ``UNKNOWN`` without a fee; the trade's is the common value, or
  ``MIXED``. Alpaca's paper venue reports no liquidity flag, so the fee is the evidence.
- ``exit_path``: the kinds of the orders whose fills sold the position, in fill order:
  ``NATIVE_STOP_LIMIT`` (the protective stop-limit or a replacement of it), ``COLLAR_IOC``
  (``CRYPTO_STOP_EXECUTION_V1``'s collared IOC limit), ``MARKET`` (any market close),
  ``LIMIT_EXIT`` (another limit sell), ``UNKNOWN``; joined by ``+``; ``NONE`` without a sell.
- ``stop_exit``: whether the close reason is a stop (``STOP_REASONS``); for a stop exit
  ``stop_slippage_fraction`` = (stop - average exit) / stop (positive: sold under the stop) and
  ``stop_slippage_r`` = (stop - average exit) x sold quantity / the official R denominator. The
  stop is the one in force at the close (the state's).
- ``versions``: the setup's stop execution, entry execution and stop-limit versions (``NONE``
  when not recorded), so the scorecard can compare them.
"""

from collections import Counter
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab import maker_entry, stop_breach, stop_execution
from catalyst_lab.managed_analytics import resolved_fill_costs

D = Decimal
METHOD = "EXECUTION_QUALITY_V1"
LIQUIDITY_SPLIT = (maker_entry.MAKER_FEE_RATE + maker_entry.TAKER_FEE_RATE) / 2
MAKER, TAKER, MIXED, UNKNOWN, NONE = "MAKER", "TAKER", "MIXED", "UNKNOWN", "NONE"
NATIVE_STOP_LIMIT, COLLAR_IOC, MARKET, LIMIT_EXIT = (
    "NATIVE_STOP_LIMIT", "COLLAR_IOC", "MARKET", "LIMIT_EXIT")
STOP_REASONS = frozenset({"BROKER_EXIT", "STOP_LIMIT_NOT_FILLED",
                          "STOP_CROSSED_DURING_REPLACE", stop_execution.EXIT_REASON})
SIX = D("0.000001")


def _q(value, step=SIX):
    if value is None:
        return None
    rounded = value.quantize(step, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def order_kinds(conn, setup_id):
    """``{broker_order_id: kind}`` for the setup's own orders (its acknowledged decisions, PATCH
    replacements and linked orders)."""
    rows = conn.execute(
        """SELECT e.body->>'action' AS action, e.body->'order'->>'id' AS order_id,
        d.payload FROM lab.managed_events e
        LEFT JOIN lab.managed_risk_decisions d ON d.decision_id::text=e.body->>'decision_id'
        WHERE e.setup_id=%s AND e.kind='BROKER_ACK'""",
        (setup_id,),
    ).fetchall()
    links = conn.execute(
        """SELECT body->>'broker_order_id' AS order_id, body->>'role' AS role
        FROM lab.managed_events WHERE setup_id=%s AND kind='BROKER_ORDER_LINK'""",
        (setup_id,),
    ).fetchall()
    found = {}
    for row in links:
        if row["role"] == "PROTECT":
            found[row["order_id"]] = NATIVE_STOP_LIMIT
        elif row["role"] == "EXIT":
            found[row["order_id"]] = UNKNOWN
    for row in rows:
        if not row["order_id"]:
            continue
        found[row["order_id"]] = kind_of(row["action"], row["payload"] or {})
    return found


def kind_of(action, payload):
    """The kind of a sell order from its decision's action and payload."""
    if action in {"PROTECT", "AMEND"}:
        return NATIVE_STOP_LIMIT
    if action != "EXIT":
        return UNKNOWN
    kind = payload.get("type")
    if kind == "market":
        return MARKET
    if kind == "limit":
        return COLLAR_IOC if payload.get("time_in_force") == "ioc" else LIMIT_EXIT
    if kind == "stop_limit":
        return NATIVE_STOP_LIMIT
    return UNKNOWN


def liquidity(rate):
    if rate is None:
        return UNKNOWN
    return MAKER if rate <= LIQUIDITY_SPLIT else TAKER


def _rate(rows, costs):
    notional = sum((r["qty"] * r["price"] for r in rows), D(0))
    fees = [costs.get(r["fill_id"], {}).get("fee_usd") for r in rows]
    if not rows or not notional or any(f is None for f in fees):
        return None
    with localcontext() as context:
        context.prec = 40
        return sum(fees, D(0)) / notional


def versions(state):
    state = state if isinstance(state, dict) else {}
    stop_exec = (state.get(stop_execution.FIELD) or {}).get("policy_id") if isinstance(
        state.get(stop_execution.FIELD), dict) else None
    entry_exec = (state.get(maker_entry.FIELD) or {}).get("policy_id") if isinstance(
        state.get(maker_entry.FIELD), dict) else None
    limit = state.get(stop_breach.STOP_LIMIT_FIELD)
    return {
        "stop_execution": stop_exec or NONE,
        "entry_execution": entry_exec or NONE,
        "stop_limit": (limit or {}).get("policy_id") if isinstance(limit, dict) else NONE,
        "stop_breach": state.get("stop_breach_version") or NONE,
    }


def summarize_fills(fills, costs, kinds, state, *, planned_filled_risk=None):
    """The per-trade fields from the setup's fills (pure)."""
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    per_buy = []
    for fill in buys:
        fee = costs.get(fill["fill_id"], {}).get("fee_usd")
        notional = fill["qty"] * fill["price"]
        per_buy.append(liquidity(fee / notional if fee is not None and notional else None))
    entry_liquidity = (per_buy[0] if len(set(per_buy)) == 1 else MIXED) if per_buy else UNKNOWN
    path = []
    for fill in sorted(sells, key=lambda f: (f["filled_at"], f.get("event_seq") or 0)):
        kind = kinds.get(fill["broker_order_id"], UNKNOWN)
        if kind not in path:
            path.append(kind)
    sold = sum((f["qty"] for f in sells), D(0))
    exit_average = (sum((f["qty"] * f["price"] for f in sells), D(0)) / sold) if sold else None
    reason = state.get("reason") or state.get("exit_requested")
    stop = _num(state.get("stop"))
    stop_exit = reason in STOP_REASONS
    slippage = slippage_r = None
    if stop_exit and stop and exit_average is not None:
        with localcontext() as context:
            context.prec = 40
            slippage = (stop - exit_average) / stop
            risk = _num(planned_filled_risk)
            slippage_r = (stop - exit_average) * sold / risk if risk else None
    return {
        "method": METHOD,
        "entry_fee_rate": _q(_rate(buys, costs)),
        "entry_liquidity": entry_liquidity,
        "entry_fills": len(buys),
        "exit_fee_rate": _q(_rate(sells, costs)),
        "exit_path": "+".join(path) if path else NONE,
        "exit_average": exit_average,
        "exit_reason": reason,
        "stop": state.get("stop"),
        "stop_exit": stop_exit,
        "stop_slippage_fraction": _q(slippage),
        "stop_slippage_r": _q(slippage_r),
        "versions": versions(state),
    }


def trade_execution(conn, setup_id, state, *, planned_filled_risk=None):
    """The trade's execution quality (``summarize_fills``) read from the ledger."""
    fills = conn.execute(
        "SELECT * FROM lab.managed_fills WHERE setup_id=%s ORDER BY filled_at,event_seq",
        (setup_id,),
    ).fetchall()
    costs = resolved_fill_costs(conn, fills)
    return summarize_fills(fills, costs, order_kinds(conn, setup_id), state or {},
                           planned_filled_risk=planned_filled_risk)


def exit_summary(conn, setup_id, state):
    """``STOP_EXECUTION_RESULT``'s body: the trade's execution quality plus, for an emulated
    stop, the breach and the collar, and the exit's slippage against the reference print."""
    summary = trade_execution(conn, setup_id, state)
    breach = state.get("stop_breach_evidence") if isinstance(
        state.get("stop_breach_evidence"), dict) else None
    collar = state.get(stop_execution.COLLAR_FIELD) if isinstance(
        state.get(stop_execution.COLLAR_FIELD), dict) else None
    reference = _num((collar or {}).get("reference_price"))
    exit_average = summary["exit_average"]
    vs_reference = None
    if reference and exit_average is not None:
        with localcontext() as context:
            context.prec = 40
            vs_reference = _q((reference - exit_average) / reference)
    return {
        **summary,
        "version": stop_execution.VERSION,
        "lifecycle_id": state.get("lifecycle_id"),
        "breach": None if breach is None else {
            k: breach.get(k) for k in ("version", "breach_evidence", "confirmation", "fallback",
                                       "established_at", "evidence_price", "print_count",
                                       "confirm_latency_seconds", "exit_path")},
        "collar": collar,
        "reference_slippage_fraction": vs_reference,
    }


def _mean(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return _q(sum(values, D(0)) / len(values))


def section(items):
    """The scorecard's ``execution`` dimension: per execution-version key, the entry fee rate
    and maker share, the exit paths, and the stop slippage of stop exits; every figure with its
    count. ``items`` carry ``execution`` (``trade_execution``) or none (not measured)."""
    groups = {}
    for item in items:
        quality = item.get("execution")
        if not isinstance(quality, dict):
            continue
        v = quality.get("versions") or {}
        key = (f"stop_execution={v.get('stop_execution', NONE)}|"
               f"entry_execution={v.get('entry_execution', NONE)}")
        groups.setdefault(key, []).append(quality)
    result = {}
    for key in sorted(groups):
        members = groups[key]
        liquidity_counts = Counter(m["entry_liquidity"] for m in members)
        known = liquidity_counts[MAKER] + liquidity_counts[TAKER]
        stops = [m for m in members if m.get("stop_exit")]
        slips = [_num(m.get("stop_slippage_fraction")) for m in stops]
        slips_r = [_num(m.get("stop_slippage_r")) for m in stops]
        rates = [_num(m.get("entry_fee_rate")) for m in members]
        result[key] = {
            "trades": len(members),
            "entry_liquidity": dict(sorted(liquidity_counts.items())),
            "maker_share": _q(D(liquidity_counts[MAKER]) / known) if known else None,
            "maker_share_count": known,
            "mean_entry_fee_rate": _mean(rates),
            "entry_fee_rate_count": sum(1 for r in rates if r is not None),
            "exit_paths": dict(sorted(Counter(m["exit_path"] for m in members).items())),
            "stop_exits": len(stops),
            "mean_stop_slippage_fraction": _mean(slips),
            "stop_slippage_count": sum(1 for s in slips if s is not None),
            "mean_stop_slippage_r": _mean(slips_r),
            "stop_slippage_r_count": sum(1 for s in slips_r if s is not None),
        }
    return {"method": METHOD, "liquidity_split_fee_rate": str(LIQUIDITY_SPLIT),
            "by_execution_versions": result}


__all__ = ["COLLAR_IOC", "LIQUIDITY_SPLIT", "MARKET", "METHOD", "NATIVE_STOP_LIMIT",
           "STOP_REASONS", "exit_summary", "kind_of", "liquidity", "order_kinds", "section",
           "summarize_fills", "trade_execution", "versions"]
