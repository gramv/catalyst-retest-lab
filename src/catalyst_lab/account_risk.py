"""Versioned account-risk policies (migration 016) and the one SQL account-risk check.

Every checker asks ``lab.account_risk_failure`` the same question: the frozen V1 engine, the
managed engine and both reservation triggers. Policy rows are immutable and inserted only by
migrations or explicit owner steps; Python reads them and never keeps its own copy of a number.
A rule change is a new, separately named row; no historical setup is re-evaluated under it.

Migration 022 (package crypto-size-hold) adds market terms: a MANAGED policy may size one market
in equity slices (``EQUITY_SLICE_RISK_CAPPED_V1``, ``JEV_MANAGED_RISK_V3`` crypto). The terms
are rows of ``lab.account_risk_market_terms`` written with their policy row; ``slice_size``
below is the Python mirror of ``lab.slice_sizing_failure``: it returns the largest quantity on
the coin's increment that the SQL check accepts and the available cash can pay for.

Migration 026 (package risk-pacing, ``JEV_MANAGED_RISK_V4``) adds daily loss limits to a
policy's definition: rows of ``lab.account_risk_daily_limits`` written with their policy row.
``DailyLimits`` is that row; ``daily_hard_loss_pct`` gives the engine's daily halt threshold
(the policy's hard limit, else the 3% every earlier policy has always used) and
``soft_limit_reached`` the V4 no-new-entries limit. The SQL account-risk check is unchanged:
V4's 2% CRYPTO cap is a row value that ``lab.account_risk_failure`` already sums over every
open CRYPTO reservation (all coins one cluster).

Migration 031 (package plugin-c3, ``JEV_MANAGED_RISK_V5``) adds per-strategy open-risk caps to a
policy's definition: rows of ``lab.account_risk_strategy_caps`` (by strategy source) written with
their policy row. ``strategy_cap_failure`` is the Python mirror of ``lab.strategy_risk_failure``:
every open reservation of the entry's strategy plus the entry above ``cap_pct`` x equity is
``STRATEGY_RISK_CAP`` (a capacity reason: the setup waits, as for the market cap). A policy
without a row for the strategy's source (every policy before V5, and research picks under V5)
has no per-strategy cap, so its answers are unchanged.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal, localcontext

VENUE = "ALPACA_PAPER"
FROZEN_V1_POLICY_ID = "CATALYST_RETEST_V1"
LEGACY_MANAGED_POLICY_ID = "MUSE_JEV_MANAGED_TEST_V1"
MANAGED_RISK_V2_POLICY_ID = "JEV_MANAGED_RISK_V2"
# Migration 022: 10% equity slices capped at 0.5% planned risk; 5% crypto; three per sector.
MANAGED_RISK_V3_POLICY_ID = "JEV_MANAGED_RISK_V3"
EQUITY_SLICE_SIZING = "EQUITY_SLICE_RISK_CAPPED_V1"
# Migration 026: V3 with a 2% crypto cap and daily loss limits in the policy (hard 3%, soft 2%).
MANAGED_RISK_V4_POLICY_ID = "JEV_MANAGED_RISK_V4"
# Migration 031: V4 plus a per-strategy open-risk cap (a mechanical plug-in at most 0.5% of equity).
MANAGED_RISK_V5_POLICY_ID = "JEV_MANAGED_RISK_V5"
STRATEGY_RISK_CAP = "STRATEGY_RISK_CAP"
# A setup's strategy source (``lab.managed_setup_strategy_source``): a promoted strategy's signal
# (STRATEGY_PAPER_PATH_V1's selection policy) is MECHANICAL; every other setup a research pick.
MECHANICAL_SOURCE, RESEARCH_SOURCE = "MECHANICAL", "RESEARCH_REPORT"
STRATEGY_SIGNAL_SELECTION_POLICY = "STRATEGY_SIGNAL_SELECTION_V1"
DEFAULT_SETUP_STRATEGY = "PULLBACK_V1"
# The daily halt of every policy without a daily-limits row (the engine's literal before V4).
LEGACY_DAILY_HARD_LOSS_PCT = Decimal(".03")
HARD_LOSS_ACTION, SOFT_LOSS_ACTION = "CANCEL_AND_FLATTEN", "NO_NEW_ENTRIES"
# The soft limit: a system event once per policy and New York day (the day's latch), then one
# wait event per setup and UTC minute for every trigger it holds back.
SOFT_LIMIT_EVENT = "DAILY_SOFT_LOSS_LIMIT"
SOFT_LIMIT_WAIT_EVENT = "DAILY_SOFT_LOSS_ENTRY_WAIT"
SOFT_LIMIT_REASON = "DAILY_SOFT_LOSS_LIMIT"
# Binding constraints of a slice-sized entry (the decision context's ``binding_constraint``).
NOTIONAL_BINDING, RISK_BINDING, CAPITAL_BINDING = "NOTIONAL", "RISK", "CAPITAL"
STOP_DISTANCE_BELOW_MINIMUM = "STOP_DISTANCE_BELOW_MINIMUM"
_SIZING_PRECISION = 80  # Floor divisions are exact at this precision for any sane inputs.
# Capacity rejections say the account is full right now; the setup itself is still valid.
CAPACITY_REASONS = frozenset(
    {"CORRELATION_LIMIT", "MAX_OPEN_PLANNED_RISK", "MARKET_RISK_CAP", "INSUFFICIENT_BUYING_POWER",
     STRATEGY_RISK_CAP}
)
FIXED_EXIT_ARM = "FIXED_EXIT"
JEV_MANAGED_ARM = "JEV_MANAGED"
ARM_METHOD = "SHA256_SETUP_ID_MOD_100_V1"
# Written by an owner bucket import for a crypto symbol it did not list (managed_classification).
UNLISTED_CRYPTO_THEME = "CRYPTO_UNLISTED"


@dataclass(frozen=True)
class MarketTerms:
    """One ``lab.account_risk_market_terms`` row (migration 022): a market its policy sizes in
    equity slices. The per-trade risk cap is the policy row's ``risk_pct``."""

    policy_id: str
    market: str
    sizing_method: str
    notional_pct: Decimal
    max_per_theme: int
    min_stop_fraction: Decimal
    owner_ruling_ref: str

    @classmethod
    def from_row(cls, row):
        terms = cls(
            row["policy_id"],
            row["market"],
            row["sizing_method"],
            Decimal(row["notional_pct"]),
            row["max_per_theme"],
            Decimal(row["min_stop_fraction"]),
            row["owner_ruling_ref"],
        )
        if terms.sizing_method != EQUITY_SLICE_SIZING:  # The only method the engine implements.
            raise ValueError("RISK_POLICY_SIZING_METHOD_UNSUPPORTED")
        return terms

    def evidence(self):
        return {
            "market": self.market,
            "sizing_method": self.sizing_method,
            "notional_pct": self.notional_pct,
            "max_per_theme": self.max_per_theme,
            "min_stop_fraction": self.min_stop_fraction,
            "owner_ruling_ref": self.owner_ruling_ref,
        }


@dataclass(frozen=True)
class DailyLimits:
    """One ``lab.account_risk_daily_limits`` row (migration 026): fractions of the New York
    day's starting equity. ``soft_loss_pct`` is None for a policy without a soft limit."""

    policy_id: str
    hard_loss_pct: Decimal
    hard_action: str
    soft_loss_pct: Decimal | None
    soft_action: str | None
    owner_ruling_ref: str

    @classmethod
    def from_row(cls, row):
        limits = cls(
            row["policy_id"],
            Decimal(row["hard_loss_pct"]),
            row["hard_action"],
            None if row["soft_loss_pct"] is None else Decimal(row["soft_loss_pct"]),
            row["soft_action"],
            row["owner_ruling_ref"],
        )
        if limits.hard_action != HARD_LOSS_ACTION or (
            limits.soft_loss_pct is not None and limits.soft_action != SOFT_LOSS_ACTION
        ):  # The only actions the engine implements.
            raise ValueError("RISK_POLICY_DAILY_ACTION_UNSUPPORTED")
        return limits

    def evidence(self):
        return {
            "hard_loss_pct": self.hard_loss_pct,
            "hard_action": self.hard_action,
            "soft_loss_pct": self.soft_loss_pct,
            "soft_action": self.soft_action,
            "owner_ruling_ref": self.owner_ruling_ref,
        }


@dataclass(frozen=True)
class AccountRiskPolicy:
    policy_id: str
    engine: str
    risk_pct: Decimal
    account_cap_pct: Decimal
    market_caps: dict
    max_per_sector: int
    max_per_theme: int
    sector_limited_markets: tuple
    leverage_allowed: bool
    intraday_buying_power_multiple: Decimal
    capacity_cooldown_seconds: int | None
    fixed_exit_arm_pct: int
    owner_ruling_ref: str
    # {market: MarketTerms} (migration 022); empty for every policy written before it.
    market_terms: dict = field(default_factory=dict)
    # Migration 026; None for every policy written before it (the daily halt stays 3%).
    daily_limits: DailyLimits | None = None
    # Migration 031: {strategy source: cap fraction}; empty for every policy written before it.
    strategy_caps: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row, terms=(), daily=None, strategy_caps=()):
        caps = json.loads(row["market_caps_json"], parse_float=Decimal, parse_int=Decimal)
        return cls(
            row["policy_id"],
            row["engine"],
            Decimal(row["risk_pct"]),
            Decimal(row["account_cap_pct"]),
            {market: Decimal(value) for market, value in sorted(caps.items())},
            row["max_per_sector"],
            row["max_per_theme"],
            tuple(row["sector_limited_markets"]),
            row["leverage_allowed"],
            Decimal(row["intraday_buying_power_multiple"]),
            row["capacity_cooldown_seconds"],
            row["fixed_exit_arm_pct"],
            row["owner_ruling_ref"],
            {t.market: t for t in (MarketTerms.from_row(r) for r in terms)},
            DailyLimits.from_row(daily) if daily else None,
            {r["strategy_source"]: Decimal(r["cap_pct"]) for r in strategy_caps},
        )

    def market_cap(self, market):
        """A market absent from the row may hold no planned risk at all."""
        return self.market_caps.get(market, Decimal(0))

    def terms(self, market):
        """The market's slice terms, or None when the market keeps the row's fixed budget."""
        return self.market_terms.get(market)

    def theme_limit(self, market):
        """Open trades allowed per theme in ``market``: the terms' limit, else the row's."""
        terms = self.terms(market)
        return terms.max_per_theme if terms is not None else self.max_per_theme

    def daily_hard_loss_pct(self):
        """The daily halt's loss fraction: the policy's hard limit, else the legacy 3%."""
        if self.daily_limits is None:
            return LEGACY_DAILY_HARD_LOSS_PCT
        return self.daily_limits.hard_loss_pct

    def daily_soft_loss_pct(self):
        """The no-new-entries loss fraction, or None for a policy without a soft limit."""
        return None if self.daily_limits is None else self.daily_limits.soft_loss_pct

    def stock_multiple(self, day_position):
        """Intraday buying-power multiple for a US stock entry; crypto is always cash-only."""
        if self.leverage_allowed and day_position:
            return self.intraday_buying_power_multiple
        return Decimal(1)

    def evidence(self):
        evidence = {
            "policy_id": self.policy_id,
            "engine": self.engine,
            "risk_pct": self.risk_pct,
            "account_cap_pct": self.account_cap_pct,
            "market_caps": dict(self.market_caps),
            "max_per_sector": self.max_per_sector,
            "max_per_theme": self.max_per_theme,
            "sector_limited_markets": list(self.sector_limited_markets),
            "leverage_allowed": self.leverage_allowed,
            "intraday_buying_power_multiple": self.intraday_buying_power_multiple,
            "capacity_cooldown_seconds": self.capacity_cooldown_seconds,
            "fixed_exit_arm_pct": self.fixed_exit_arm_pct,
            "owner_ruling_ref": self.owner_ruling_ref,
        }
        if self.market_terms:  # Policies without terms keep their earlier evidence exactly.
            evidence["market_terms"] = {
                market: terms.evidence() for market, terms in sorted(self.market_terms.items())
            }
        if self.daily_limits is not None:  # Likewise: only policies with daily limits.
            evidence["daily_limits"] = self.daily_limits.evidence()
        if self.strategy_caps:  # Likewise: only policies with strategy caps (V5 on).
            evidence["strategy_caps"] = dict(sorted(self.strategy_caps.items()))
        return evidence

    def strategy_cap(self, source):
        """The open-risk cap fraction of one strategy of ``source``, or None (no such cap)."""
        return self.strategy_caps.get(source)


def load_policy(conn, policy_id, *, engine=None):
    """The immutable row for ``policy_id`` with its market terms; unknown ids and the wrong
    engine fail closed."""
    if not isinstance(policy_id, str):
        raise ValueError("RISK_POLICY_UNKNOWN")
    row = conn.execute(
        """SELECT *,market_caps::text AS market_caps_json FROM lab.account_risk_policies
        WHERE policy_id=%s""",
        (policy_id,),
    ).fetchone()
    if not row:
        raise ValueError("RISK_POLICY_UNKNOWN")
    terms = conn.execute(
        "SELECT * FROM lab.account_risk_market_terms WHERE policy_id=%s ORDER BY market",
        (policy_id,),
    ).fetchall()
    daily = conn.execute(
        "SELECT * FROM lab.account_risk_daily_limits WHERE policy_id=%s", (policy_id,)
    ).fetchone()
    caps = ()
    if conn.execute("SELECT to_regclass('lab.account_risk_strategy_caps') IS NOT NULL AS present"
                    ).fetchone()["present"]:
        # Before migration 031 no policy can have a strategy cap (V5 is written by 031).
        caps = conn.execute(
            """SELECT * FROM lab.account_risk_strategy_caps WHERE policy_id=%s
            ORDER BY strategy_source""", (policy_id,)
        ).fetchall()
    policy = AccountRiskPolicy.from_row(row, terms, daily, caps)
    if engine is not None and policy.engine != engine:
        raise ValueError("RISK_POLICY_ENGINE_MISMATCH")
    return policy


def daily_loss_threshold(day_start_equity, fraction):
    """The day's P&L at or below which a limit of ``fraction`` binds (a negative amount)."""
    return -fraction * day_start_equity


def soft_limit_reached(policy, total_pnl, day_start_equity):
    """Whether the day's realized plus unrealized P&L has reached the policy's soft limit;
    always False for a policy without one (every policy before V4)."""
    fraction = policy.daily_soft_loss_pct()
    return fraction is not None and total_pnl <= daily_loss_threshold(day_start_equity, fraction)


def soft_limit_key(policy_id, session_date):
    return f"daily-soft-loss-limit:{policy_id}:{session_date.isoformat()}"


def soft_wait_key(setup_id, now):
    minute = now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    return f"daily-soft-loss-entry-wait:{setup_id}:{minute}"


@dataclass(frozen=True)
class SliceSizing:
    """An entry sized in equity slices (``EQUITY_SLICE_RISK_CAPPED_V1``).

    ``qty`` is the largest multiple of ``increment`` with qty x M within the slice, qty x (M - S)
    within the per-trade risk cap and qty x M within ``available`` cash; ``policy_qty`` is the
    same without the cash cap, so a zero size can be told apart: capital-limited (a capacity
    deferral) or a slice or risk cap below one increment (terminal ZERO_SHARE_SIZE).
    """

    qty: Decimal
    policy_qty: Decimal
    planned_risk: Decimal
    notional: Decimal
    binding: str
    notional_cap: Decimal
    risk_cap: Decimal
    available: Decimal
    increment: Decimal
    evidence: dict


def slice_size(policy, terms, *, equity, max_entry, stop, available, increment):
    """The Python mirror of ``lab.slice_sizing_failure``: exact floor divisions, no rounding up.

    Raises ``ValueError("STOP_DISTANCE_BELOW_MINIMUM")`` for a stop closer than the terms'
    minimum fraction of M, as the SQL check refuses it, and ``INVALID_SLICE_SIZING_INPUT`` for
    non-positive or non-finite numbers.
    """
    numbers = (equity, max_entry, stop, increment)
    if (
        not isinstance(terms, MarketTerms)
        or terms.sizing_method != EQUITY_SLICE_SIZING
        or any(not isinstance(n, Decimal) or not n.is_finite() or n <= 0 for n in numbers)
        or not isinstance(available, Decimal)
        or not available.is_finite()
        or stop >= max_entry
    ):
        raise ValueError("INVALID_SLICE_SIZING_INPUT")
    with localcontext() as context:
        context.prec = _SIZING_PRECISION
        distance = max_entry - stop
        if distance < terms.min_stop_fraction * max_entry:
            raise ValueError(STOP_DISTANCE_BELOW_MINIMUM)
        notional_cap = equity * terms.notional_pct
        risk_cap = equity * policy.risk_pct
        cash = max(Decimal(0), available)
        slice_lots = notional_cap // (max_entry * increment)
        risk_lots = risk_cap // (distance * increment)
        cash_lots = cash // (max_entry * increment)
        policy_lots = min(slice_lots, risk_lots)
        lots = min(policy_lots, cash_lots)
        qty, policy_qty = lots * increment, policy_lots * increment
        planned, notional = qty * distance, qty * max_entry
    if cash_lots < policy_lots:
        binding = CAPITAL_BINDING
    elif risk_lots < slice_lots:
        binding = RISK_BINDING
    else:
        binding = NOTIONAL_BINDING
    evidence = {
        "method": EQUITY_SLICE_SIZING,
        "equity": equity,
        "notional_pct": terms.notional_pct,
        "risk_pct": policy.risk_pct,
        "min_stop_fraction": terms.min_stop_fraction,
        "notional_cap": notional_cap,
        "risk_cap": risk_cap,
        "available_cash": available,
        "increment": increment,
        "slice_qty": slice_lots * increment,
        "risk_qty": risk_lots * increment,
        "cash_qty": cash_lots * increment,
        "qty": qty,
        "notional": notional,
        "planned_risk": planned,
        "binding": binding,
    }
    return SliceSizing(qty, policy_qty, planned, notional, binding, notional_cap, risk_cap,
                       available, increment, evidence)


def slice_sizing_failure(conn, policy_id, market, equity, qty, max_entry, stop):
    """NULL, or why ``lab.slice_sizing_failure`` refuses this size (the trigger's own check)."""
    return conn.execute(
        "SELECT lab.slice_sizing_failure(%s,%s,%s,%s,%s,%s) AS reason",
        (policy_id, market, equity, qty, max_entry, stop),
    ).fetchone()["reason"]


def account_risk_failure(conn, policy_id, market, sector, theme, equity, budget, *, venue=VENUE):
    """NULL, or CORRELATION_UNKNOWN | CORRELATION_LIMIT | MAX_OPEN_PLANNED_RISK | MARKET_RISK_CAP.

    Must run under the shared risk lock in the transaction that would insert the reservation;
    the reservation triggers ask the identical question, so they cannot disagree with it.
    """
    return conn.execute(
        "SELECT lab.account_risk_failure(%s,%s,%s,%s,%s,%s,%s) AS reason",
        (policy_id, venue, market, sector, theme, equity, budget),
    ).fetchone()["reason"]


def setup_strategy(record_json):
    """``(strategy_id, source)`` of a setup's admitted packet, as ``lab.managed_setup_strategy``
    and ``lab.managed_setup_strategy_source`` read it."""
    record = record_json if isinstance(record_json, dict) else {}
    strategy_id = record.get("strategy_id") or DEFAULT_SETUP_STRATEGY
    source = (MECHANICAL_SOURCE if record.get("selection_policy")
              == STRATEGY_SIGNAL_SELECTION_POLICY else RESEARCH_SOURCE)
    return strategy_id, source


def strategy_cap_failure(policy, source, held, budget, equity):
    """The Python mirror of ``lab.strategy_risk_failure`` on its inputs: None, or
    ``STRATEGY_RISK_CAP`` when the strategy's open planned risk ``held`` plus ``budget`` is above
    the policy's cap for ``source`` x ``equity`` (exactly the cap passes). None for a source
    without a cap. Exact Decimal comparison."""
    cap = policy.strategy_cap(source)
    if cap is None:
        return None
    with localcontext() as context:
        context.prec = _SIZING_PRECISION
        return STRATEGY_RISK_CAP if held + budget > cap * equity else None


def strategy_open_risk(conn, record_json, *, venue=VENUE):
    """The open planned risk of the setup's strategy on the venue (the SQL check's ``held``)."""
    strategy_id, _ = setup_strategy(record_json)
    return conn.execute(
        """SELECT coalesce(sum(r.budget),0) AS held FROM lab.managed_active_reservations r
        JOIN lab.managed_setups s USING(setup_id)
        JOIN lab.managed_risk_decisions d ON d.decision_id=r.decision_id
        WHERE coalesce(d.context->>'venue','ALPACA_PAPER')=%s
        AND coalesce(nullif(s.record_json->>'strategy_id',''),%s)=%s""",
        (venue, DEFAULT_SETUP_STRATEGY, strategy_id),
    ).fetchone()["held"]


def strategy_risk_failure(conn, policy_id, record_json, equity, budget, *, venue=VENUE):
    """NULL, or ``STRATEGY_RISK_CAP`` (or an input/policy code) from ``lab.strategy_risk_failure``:
    the one SQL per-strategy check the reservation trigger also asks."""
    from psycopg.types.json import Jsonb

    from catalyst_lab.repository import json_safe

    return conn.execute(
        "SELECT lab.strategy_risk_failure(%s,%s,%s,%s,%s) AS reason",
        (policy_id, venue, Jsonb(json_safe(record_json)), equity, budget),
    ).fetchone()["reason"]


_BINDING = {
    "CORRELATION_UNKNOWN": "CORRELATION",
    "CORRELATION_LIMIT": "CORRELATION",
    "MAX_OPEN_PLANNED_RISK": "ACCOUNT_RISK_CAP",
    "MARKET_RISK_CAP": "MARKET_RISK_CAP",
    STRATEGY_RISK_CAP: "STRATEGY_RISK_CAP",
    "INSUFFICIENT_BUYING_POWER": "BUYING_POWER",
    "BUYING_POWER_EVIDENCE_UNAVAILABLE": "BUYING_POWER",
    "CRYPTO_ACCOUNT_NOT_ACTIVE": "BUYING_POWER",
    "ZERO_SHARE_SIZE": "RISK",
}


def binding_constraint(reason):
    """The sizing/risk constraint behind a refusal, for the decision context.

    None when no such constraint refused the entry (approved entries record RISK or CAPITAL/CASH
    from sizing instead; other refusals, such as stale evidence, bind no constraint).
    """
    return _BINDING.get(reason) if reason else None


def classification_known(classification):
    return bool(
        classification
        and classification.get("sector")
        and classification.get("theme")
        and not (
            classification["sector"] == "CRYPTO"
            and classification["theme"] == UNLISTED_CRYPTO_THEME
        )
    )


def arm_bucket(setup_id):
    """0-99 from SHA-256 of the canonical setup UUID text; independent of any research input."""
    return int(hashlib.sha256(str(setup_id).encode()).hexdigest(), 16) % 100


def assign_arm(setup_id, fixed_exit_pct):
    if type(fixed_exit_pct) is not int or not 0 <= fixed_exit_pct <= 100:
        raise ValueError("INVALID_FIXED_EXIT_SHARE")
    return FIXED_EXIT_ARM if arm_bucket(setup_id) < fixed_exit_pct else JEV_MANAGED_ARM
