"""Frozen Phase 4 percentages, worst-case entry sizing and the broker buying-power check.

No broker access: callers pass the account evidence they already read.
"""

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

RISK_PCT = Decimal("0.01")
MAX_OPEN_RISK_PCT = Decimal("0.02")
DAILY_HALT_PCT = Decimal("0.03")
# Alpaca margin framework: 1 (cash-like), 2 (Reg T) or 4 (pattern day trader intraday).
BROKER_MULTIPLIERS = frozenset({Decimal(1), Decimal(2), Decimal(4)})


@dataclass(frozen=True)
class Sizing:
    qty: int
    budget: Decimal
    planned_risk: Decimal
    notional: Decimal


def size_entry(equity: Decimal, maximum: Decimal, stop: Decimal, *, available=None) -> Sizing:
    numbers = [equity, maximum, stop]
    if any(not isinstance(n, Decimal) or not n.is_finite() or n <= 0 for n in numbers):
        raise ValueError("Finite positive equity and levels are required")
    if stop >= maximum:
        raise ValueError("Stop must be below the maximum entry")
    capital = equity if available is None else min(equity, available)
    if not capital.is_finite() or capital < 0:
        raise ValueError("Available unlevered capital must be finite and nonnegative")
    budget = RISK_PCT * equity
    risk_qty = int((budget / (maximum - stop)).to_integral_value(rounding=ROUND_FLOOR))
    cash_qty = int((capital / maximum).to_integral_value(rounding=ROUND_FLOOR))
    qty = min(risk_qty, cash_qty)
    return Sizing(qty, budget, qty * (maximum - stop), qty * maximum)


@dataclass(frozen=True)
class BuyingPower:
    """Outcome of the pre-trade buying-power check; ``failure`` is None when it passes."""

    failure: str | None
    allowed: Decimal | None
    evidence: dict = field(default_factory=dict)


def broker_amount(value):
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def buying_power_check(account, *, market, notional, equity, multiple=Decimal(1)):
    """Check an already-sized entry against the broker's own buying power; never resize.

    US stocks may use ``min(buying_power, multiple x equity)``, where ``multiple`` is the
    policy's intraday multiple for a day position (1 otherwise). Crypto is cash-only:
    ``non_marginable_buying_power``, and the account's crypto trading must be ACTIVE. Missing or
    invalid fields, or a multiplier outside {1, 2, 4}, are BUYING_POWER_EVIDENCE_UNAVAILABLE. The
    broker already nets open orders into these figures, so pending orders are not subtracted.
    """
    if market not in {"US_STOCKS", "CRYPTO"}:
        raise ValueError("UNSUPPORTED_BUYING_POWER_MARKET")
    if any(
        not isinstance(n, Decimal) or not n.is_finite() or n <= 0 for n in (notional, equity)
    ) or not isinstance(multiple, Decimal) or not multiple.is_finite() or multiple < 1:
        raise ValueError("INVALID_BUYING_POWER_INPUT")
    account = account if isinstance(account, dict) else {}
    broker_multiplier = broker_amount(account.get("multiplier"))
    evidence = {
        "market": market,
        "notional": notional,
        "multiplier": account.get("multiplier"),
        "policy_multiple": multiple if market == "US_STOCKS" else Decimal(1),
    }
    if broker_multiplier not in BROKER_MULTIPLIERS:
        return BuyingPower("BUYING_POWER_EVIDENCE_UNAVAILABLE", None, evidence)
    if market == "CRYPTO":
        status = account.get("crypto_status")
        available = broker_amount(account.get("non_marginable_buying_power"))
        evidence.update(source="non_marginable_buying_power", crypto_status=status)
        if not isinstance(status, str) or available is None:
            return BuyingPower("BUYING_POWER_EVIDENCE_UNAVAILABLE", None, evidence)
        if status != "ACTIVE":
            return BuyingPower("CRYPTO_ACCOUNT_NOT_ACTIVE", None, evidence)
        allowed = available
    else:
        available = broker_amount(account.get("buying_power"))
        evidence.update(source="buying_power")
        if available is None:
            return BuyingPower("BUYING_POWER_EVIDENCE_UNAVAILABLE", None, evidence)
        allowed = min(available, multiple * equity)
    evidence.update(broker_amount=available, allowed=allowed)
    if notional > allowed:
        return BuyingPower("INSUFFICIENT_BUYING_POWER", allowed, evidence)
    return BuyingPower(None, allowed, evidence)
