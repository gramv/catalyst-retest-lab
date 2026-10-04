"""Closed-trade results by market regime and by recorded rule version (package learning-measure,
2026-10-02; plan ``docs/TRADING-QUALITY-PLAN.md`` section 6, L1/L4/L5 in part).

**A measurement definition, never a trading rule.** Pure functions shared by the daily scorecard
and the weekly review. An item is one closed, non-engineering trade:
``{"r_net": Decimal | None, "win": bool | None, "regime": TRADE_REGIME body | None,
"versions": {field: version}}``.

- **Net R only over fee-verified trades.** ``r_net`` is the official R after verified,
  non-fixture fees, else None. Every cell reports ``closed`` (all trades) beside
  ``r_net_count`` (those with verified fees) and the mean, sum and win rate of net R over the
  verified ones only; wins and losses count every trade by gross P&L, as the scorecard does.
- **Minimum sample.** A cell with fewer than ``CELL_MINIMUM`` (30, the plan's "each version
  runs at least 30 trades before it is judged", the weekly review's per-arm minimum) verified
  trades has ``status`` ``NOT_ENOUGH_DATA``; at or above it ``MEASURED``. The figures are shown
  either way, with their counts.
- **Regime** (``MARKET_REGIME_V1``, ``market_regime``): the entry day's tag and its four parts,
  the previous day's tag (known at entry) and the at-entry return buckets. A trade without a
  ``TRADE_REGIME`` record is ``UNTAGGED``.
- **Versions**: every rule version a trade's setup recorded (``recorded_versions``: a state or
  admission field named ``*_version``, ``*_policy`` or ``*_policy_id`` whose value, or whose
  record's ``policy_id``, reads like ``NAME_V<n>``), each field its own grouping; and the phase-A
  set (``A_VERSION_FIELDS``: risk policy, trade plan, maintenance, entry pacing, stop breach)
  as one key, alone and per day tag. A version not recorded on a trade reads ``NONE``; a version
  that does not exist yet simply appears once trades record it.
- **Strategy** (``STRATEGY_REGISTRY_V1``, package strategy-c1): ``by_strategy`` groups by the
  item's ``strategy_id`` (a trade admitted before the registry, or without one, is
  ``PULLBACK_V1``). These are paper trades; shadow results are reported apart
  (``strategy_shadow.strategy_section``).
"""

import re
from collections import Counter
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from catalyst_lab.execution_quality import section as execution_section
from catalyst_lab.strategies import DEFAULT_STRATEGY_ID

D = Decimal
CELL_MINIMUM = 30
UNTAGGED = "UNTAGGED"
UNKNOWN = "UNKNOWN"
NONE = "NONE"
DAY_PARTS = ("btc_trend", "btc_volatility", "alt_breadth", "selloff")
REGIME_DIMENSIONS = ("day_tag", *DAY_PARTS, "prior_day_tag", "btc_1h", "btc_4h",
                     "median_coin_1h")
# (name, the substring a recorded field's name must contain). A1 JEV_MANAGED_RISK_V4 is the
# risk policy; A2 CRYPTO_ENTRY_PACING_V1, A3 CRYPTO_TRADE_PLAN_V1, A4 CRYPTO_MAINTENANCE_V4 and
# A5 CRYPTO_STOP_BREACH_V4 are read from whichever field records them. A5 is the stop-limit
# cushion, recorded as ``stop_limit_policy`` (``stop_breach_version`` is the older detection
# timing, V2/V3, which A5 leaves unchanged); A3 nests its policy in ``trade_plan.policy``.
A_VERSION_FIELDS = (("risk_policy", "risk_policy"), ("trade_plan", "trade_plan"),
                    ("maintenance", "maintenance"), ("entry_pacing", "pacing"),
                    ("stop_limit", "stop_limit"))
_VERSION_VALUE = re.compile(r"^[A-Z][A-Z0-9_]*_V[0-9]+$")
_VERSION_FIELD = ("_version", "_policy", "_policy_id")
# State records that carry their version in a nested ``policy`` record (CRYPTO_TRADE_PLAN_V1).
_NESTED_POLICY_FIELDS = ("trade_plan",)
FOUR = D("0.0001")


def _q(value):
    if value is None:
        return None
    rounded = value.quantize(FOUR, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _mean(values):
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return _q(sum(values, D(0)) / len(values))


def recorded_versions(*sources):
    """``{field: version}`` from the given records (later ones win), sorted by field."""
    found = {}
    for source in sources:
        for key, value in (source or {}).items() if isinstance(source, dict) else ():
            if key in _NESTED_POLICY_FIELDS and isinstance(value, dict):
                key, value = f"{key}_policy", (value.get("policy") or {}).get("policy_id")
            elif not isinstance(key, str) or not key.endswith(_VERSION_FIELD):
                continue
            elif isinstance(value, dict):
                value = value.get("policy_id")
            if isinstance(value, str) and _VERSION_VALUE.match(value):
                found[key] = value
    return dict(sorted(found.items()))


def a_versions(versions):
    """The phase-A versions of a trade: ``{name: version or NONE}``."""
    result = {}
    for name, needle in A_VERSION_FIELDS:
        matches = sorted(v for k, v in (versions or {}).items() if needle in k)
        result[name] = "+".join(matches) if matches else NONE
    return result


def a_version_key(versions):
    return "|".join(f"{name}={value}" for name, value in a_versions(versions).items())


def regime_value(regime, dimension):
    if regime is None:
        return UNTAGGED
    if dimension in DAY_PARTS:
        parts = (regime.get("day_tag") or "").split("/")
        return parts[DAY_PARTS.index(dimension)] if len(parts) == len(DAY_PARTS) else UNKNOWN
    return regime.get(dimension) or UNKNOWN


def cell(items, minimum=CELL_MINIMUM):
    values = [i["r_net"] for i in items if i["r_net"] is not None]
    wins = sum(1 for i in items if i["win"] is True)
    known = sum(1 for i in items if i["win"] is not None)
    return {
        "closed": len(items), "r_net_count": len(values),
        "fees_unverified": len(items) - len(values),
        "mean_r_net": _mean(values),
        "sum_r_net": _q(sum(values, D(0))) if values else None,
        "r_net_win_rate": _q(D(sum(1 for v in values if v > 0)) / len(values))
        if values else None,
        "wins": wins, "win_rate": _q(D(wins) / known) if known else None,
        "status": "MEASURED" if len(values) >= minimum else "NOT_ENOUGH_DATA",
    }


def grouped(items, key, minimum=CELL_MINIMUM):
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return {name: cell(groups[name], minimum) for name in sorted(groups)}


def strategy_of(item):
    """An item's strategy: its ``strategy_id``, else ``strategies.DEFAULT_STRATEGY_ID``."""
    return item.get("strategy_id") or DEFAULT_STRATEGY_ID


def dimensions(items, minimum=CELL_MINIMUM):
    """Every grouping of ``items`` (see the module docstring)."""
    fields = sorted({field for item in items for field in item["versions"]})
    by_a = {}
    for item in items:
        by_a.setdefault(a_version_key(item["versions"]), []).append(item)
    return {
        "minimum": minimum, "r_net_scope": "FEE_VERIFIED_TRADES_ONLY",
        "tagged": sum(1 for i in items if i["regime"] is not None),
        "by_regime": {dimension: grouped(items, lambda i, d=dimension: regime_value(
            i["regime"], d), minimum) for dimension in REGIME_DIMENSIONS},
        "by_version_field": {field: grouped(items, lambda i, f=field: i["versions"].get(
            f, NONE), minimum) for field in fields},
        "by_a_versions": grouped(items, lambda i: a_version_key(i["versions"]), minimum),
        # STRATEGY_REGISTRY_V1: the trade's stamped strategy (unstamped: PULLBACK_V1).
        "by_strategy": grouped(items, strategy_of, minimum),
        # EXECUTION_QUALITY_V1 (package exec-d): execution quality by execution versions.
        "execution": execution_section(items),
        "by_a_versions_and_day_tag": {
            key: grouped(members, lambda i: regime_value(i["regime"], "day_tag"), minimum)
            for key, members in sorted(by_a.items())},
    }


def regime_line(regime):
    """A trade's compact regime for a per-trade line, or None when untagged."""
    if regime is None:
        return None
    return {key: regime.get(key) for key in (
        "day_tag", "prior_day_tag", "btc_1h", "btc_4h", "median_coin_1h",
        "btc_1h_return_pct", "btc_4h_return_pct", "median_coin_1h_return_pct")}


def unverified_reason(measurement, fixture_tainted):
    """Why a closed trade has no net R: ``FIXTURE_SOURCE``, ``NO_FEE_EVIDENCE`` (a fill without
    any fee record: on the live account usually Alpaca's daily fee batch not posted yet, or a
    fee row left ambiguous), ``INVALID_COST_EVIDENCE``, ``INVENTORY_NOT_RECONCILED`` or
    ``NO_RISK_DENOMINATOR``; None when it has one."""
    if fixture_tainted:
        return "FIXTURE_SOURCE"
    if measurement.get("official_r") is not None:
        return None
    sources = Counter(item.get("source") for item in measurement.get("cost_evidence") or [])
    if sources["UNKNOWN"]:
        return "NO_FEE_EVIDENCE"
    if sources["INVALID_CORRECTION"]:
        return "INVALID_COST_EVIDENCE"
    if measurement.get("inventory_reconciled_with_costs") is False:
        return "INVENTORY_NOT_RECONCILED"
    return "NO_RISK_DENOMINATOR"


__all__ = ["A_VERSION_FIELDS", "CELL_MINIMUM", "REGIME_DIMENSIONS", "a_version_key", "a_versions",
           "cell", "dimensions", "grouped", "recorded_versions", "regime_line", "regime_value",
           "strategy_of", "unverified_reason"]
