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
"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

VENUE = "ALPACA_PAPER"
FROZEN_V1_POLICY_ID = "CATALYST_RETEST_V1"
LEGACY_MANAGED_POLICY_ID = "MUSE_JEV_MANAGED_TEST_V1"
MANAGED_RISK_V2_POLICY_ID = "JEV_MANAGED_RISK_V2"
# Migration 022: 10% equity slices capped at 0.5% planned risk; 5% crypto; three per sector.
MANAGED_RISK_V3_POLICY_ID = "JEV_MANAGED_RISK_V3"
EQUITY_SLICE_SIZING = "EQUITY_SLICE_RISK_CAPPED_V1"
# Binding constraints of a slice-sized entry (the decision context's ``binding_constraint``).
NOTIONAL_BINDING, RISK_BINDING, CAPITAL_BINDING = "NOTIONAL", "RISK", "CAPITAL"
STOP_DISTANCE_BELOW_MINIMUM = "STOP_DISTANCE_BELOW_MINIMUM"
_SIZING_PRECISION = 80  # Floor divisions are exact at this precision for any sane inputs.
# Capacity rejections say the account is full right now; the setup itself is still valid.
CAPACITY_REASONS = frozenset(
    {"CORRELATION_LIMIT", "MAX_OPEN_PLANNED_RISK", "MARKET_RISK_CAP", "INSUFFICIENT_BUYING_POWER"}
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

    @classmethod
    def from_row(cls, row, terms=()):
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
        return evidence


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
    policy = AccountRiskPolicy.from_row(row, terms)
    if engine is not None and policy.engine != engine:
        raise ValueError("RISK_POLICY_ENGINE_MISMATCH")
    return policy


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


_BINDING = {
    "CORRELATION_UNKNOWN": "CORRELATION",
    "CORRELATION_LIMIT": "CORRELATION",
    "MAX_OPEN_PLANNED_RISK": "ACCOUNT_RISK_CAP",
    "MARKET_RISK_CAP": "MARKET_RISK_CAP",
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
