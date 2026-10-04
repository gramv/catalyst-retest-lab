"""``CRYPTO_MAKER_ENTRY_V1``: a pullback entry rests as a limit at or below the entry trigger, so it
can fill as maker (plan docs/TRADING-QUALITY-PLAN.md section 5 move 4 "maker limit entries where
the setup allows", phase D; owner, 2026-10-02: "B, C, D looks good").

**Paper trading only.** Evidence: package C2's history test puts fees (-0.19R a trade) and
slippage (-0.12R) above the gross edge (+0.07R) at Alpaca's tier-1 crypto fees, 0.25% taker and
0.15% maker. Today's entry is a limit at the max entry M sent on the confirmed touch; with the
ask at or below M it is marketable, so it fills as taker.

Venue facts (checked in this repository): docs/CRYPTO-EXECUTION-CONTRACT.md lists Alpaca crypto
market, limit and stop-limit orders with GTC/IOC durations; no repository document, adapter or
fixture names a post-only flag for crypto, and Alpaca's paper venue reports no liquidity flag on
a fill. So this version sends no post-only flag (an unverified field could be refused or
ignored) and cannot force maker: it prices the entry so it rests, and measures the result from
the fee Alpaca charges (``execution_quality``: a fee rate at or under the midpoint 0.20% is
maker, above it taker, none recorded unknown).

Scope: a report-V3 crypto setup whose system check classed its entry ``PULLBACK``, admitted on an
engine where this version is switched on (``execution_setting``; default off). Admission records
the exact policy as ``entry_execution_policy``. Every other setup is unchanged.

1. **When.** On the confirmed touch, exactly where today's entry is placed
   (``ManagedExecution.authorize_entry``): the trigger version, the stale-print and pause rules,
   ``CRYPTO_ENTRY_PACING_V1`` and the soft loss limit (all checked when the order would be
   placed), the risk cap and the reservation are unchanged. Not before the touch: a resting order
   placed while WATCHING would bypass the trigger's confirmation, the pacing gate and the
   invalidation rules, and would hold a risk reservation for hours.
2. **Price.** The lower of the entry trigger and Alpaca's fresh bid in the trigger's
   observation, rounded down to the price grid (never above M, never below one increment): at
   the bid or under it, the order joins the book rather than taking the ask. Without a usable
   bid (missing, not positive, a crossed book, or at or under the stop) the price is the entry
   trigger (``ENTRY_TRIGGER_NO_BID``), which can still be marketable.
3. **Size and risk.** Unchanged: quantity sized on M - stop (the plan's stop under
   ``CRYPTO_TRADE_PLAN_V1``), the reservation's max entry is M, so a lower price only lowers the
   real risk; official R keeps its denominator.
4. **Working.** ``CRYPTO_ENTRY_WORKING_LIMIT_V1``'s 300 s fill window, its stop-crossed cancel
   and ``CRYPTO_PARTIAL_ENTRY_V1/V2``'s partial-fill rules apply unchanged, as does the planner's
   protection-before-cancel ordering (no sell while the buy works).
5. **Record.** The entry decision's context carries ``entry_execution`` (the price, its basis,
   the trigger, M, bid and ask); the measurement reads the fill's fee rate.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext

VERSION = "CRYPTO_MAKER_ENTRY_V1"
FIELD = "entry_execution_policy"
PULLBACK = "PULLBACK"
PRICE_RULE = "MIN_ENTRY_TRIGGER_AND_BID_FLOOR_TO_GRID"
POST_ONLY = "NOT_SENT_UNVERIFIED_ON_ALPACA_CRYPTO"
MAKER_FEE_RATE = Decimal("0.0015")
TAKER_FEE_RATE = Decimal("0.0025")
BASIS_BID = "ALPACA_BID"
BASIS_TRIGGER = "ENTRY_TRIGGER"
BASIS_NO_BID = "ENTRY_TRIGGER_NO_BID"


@dataclass(frozen=True)
class MakerEntryPolicy:
    policy_id: str
    entry_types: tuple
    price_rule: str
    post_only: str
    maker_fee_rate: str
    taker_fee_rate: str

    def __post_init__(self):
        if isinstance(self.entry_types, list):
            object.__setattr__(self, "entry_types", tuple(self.entry_types))
        if (self.policy_id, self.entry_types, self.price_rule, self.post_only,
                self.maker_fee_rate, self.taker_fee_rate) != (
                VERSION, (PULLBACK,), PRICE_RULE, POST_ONLY, str(MAKER_FEE_RATE),
                str(TAKER_FEE_RATE)):
            raise ValueError("EXPLICIT_MAKER_ENTRY_POLICY_REQUIRED")

    def record(self):
        value = asdict(self)
        value["entry_types"] = list(self.entry_types)
        return value


CRYPTO_MAKER_ENTRY = MakerEntryPolicy(VERSION, (PULLBACK,), PRICE_RULE, POST_ONLY,
                                      str(MAKER_FEE_RATE), str(TAKER_FEE_RATE))


def admission_fields(enabled, packet, entry_type):
    """The field admission records: the policy, only on an engine with the version switched on,
    for a crypto setup whose system check classed the entry PULLBACK."""
    if not enabled or packet.get("market") != "CRYPTO" or entry_type != PULLBACK:
        return {}
    return {FIELD: CRYPTO_MAKER_ENTRY.record()}


def recorded(state):
    value = state.get(FIELD) if isinstance(state, dict) else None
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("EXPLICIT_MAKER_ENTRY_POLICY_REQUIRED")
    try:
        return MakerEntryPolicy(**value)
    except TypeError:
        raise ValueError("EXPLICIT_MAKER_ENTRY_POLICY_REQUIRED") from None


def active(state):
    return recorded(state) is not None


def _decimal(value):
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result > 0 else None


def entry_limit(price_increment, *, entry_trigger, max_entry, observation, stop=None):
    """``(limit, evidence)``: the maker entry's limit price and how it was chosen. A bid at or
    below ``stop`` is never used (the entry would rest under its own stop)."""
    increment = Decimal(str(price_increment))
    trigger, m = Decimal(str(entry_trigger)), Decimal(str(max_entry))
    observation = observation if isinstance(observation, dict) else {}
    bid, ask = _decimal(observation.get("bid")), _decimal(observation.get("ask"))
    usable = bid is not None and (ask is None or bid < ask) and (
        stop is None or bid > Decimal(str(stop)))
    if usable and bid < trigger:
        raw, basis = bid, BASIS_BID
    else:
        raw, basis = trigger, BASIS_TRIGGER if usable else BASIS_NO_BID
    with localcontext() as context:
        context.prec = 80
        lots = (min(raw, m) / increment).to_integral_value(rounding=ROUND_FLOOR)
    limit = max(increment, lots * increment)
    return limit, {
        "version": VERSION, "limit_price": str(limit), "basis": basis,
        "entry_trigger": str(trigger), "max_entry_price": str(m),
        "bid": str(bid) if bid is not None else None,
        "ask": str(ask) if ask is not None else None,
        "post_only": POST_ONLY,
    }


__all__ = ["CRYPTO_MAKER_ENTRY", "FIELD", "MAKER_FEE_RATE", "TAKER_FEE_RATE", "VERSION",
           "active", "admission_fields", "entry_limit", "recorded"]
