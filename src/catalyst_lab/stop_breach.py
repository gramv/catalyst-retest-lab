"""``CRYPTO_STOP_BREACH_V2``: what counts as a breached stop before the stop-limit fallback sells
a crypto position at market (owner, 2026-09-29: "you take the right decision. And fix all these
issues.").

V1, every crypto setup admitted before this version (``ManagedExecution.manage``, unchanged):
the first fresh bid at or below the stop records ``stop_breached_at``, which is never cleared,
and ``plan_crypto_recovery`` cancels the native stop-limit and sells at market
(``STOP_LIMIT_NOT_FILLED``) 2 s later if the position is still open. Alpaca's stop-limit
triggers on trades; on the thin crypto pairs the bid touched the stop without a triggering
trade, and all five live closes to 2026-09-29 (LTC, UNI and GRT among them) were this market
sell.

V2, a crypto setup whose admission recorded ``stop_breach_version``:
1. Breach evidence is either
   a. ``TRADE_PRINT``: a trade print at or below the stop, at most ``PRINT_MAX_AGE_SECONDS``
      old when the protection pass evaluates it, on a healthy feed, and traded no earlier than
      the pass that first measured the current stop (``stop_since``); or
   b. ``BID_HELD``: the fresh bid at or below the stop for ``BID_HOLD_SECONDS``. The first fresh
      bid at or below the stop starts a candidate mark; a fresh quote with the bid above the
      stop clears it; a stale quote neither starts nor clears it; the breach is established by
      a fresh bid still at or below the stop once the mark is ``BID_HOLD_SECONDS`` old.
   Freshness is V1's: a healthy feed and a quote at most 5 s old. The protection pass samples
   the latest print and quote about once a second.
2. The pass that establishes the breach records ``stop_breached_at`` (its own time) and
   ``stop_breach_evidence`` (which evidence, its time and price) and appends
   ``STOP_BREACH_ESTABLISHED``. The fallback then sells at market if the position is still open
   ``FALLBACK_SECONDS`` after ``stop_breached_at`` (``PROTECTION_POLICY``; V1 waits 2 s). The
   exit reason is still ``STOP_LIMIT_NOT_FILLED``.
3. Marks belong to the stop they were measured against (``stop_breach_marks.stop``). A changed
   stop, such as a maintenance raise, discards them, an established breach included, and the
   next pass starts measuring the new stop.
4. Marks are measured only while the broker reports a position.

Everything else is unchanged: the native stop-limit, the target, time and review exits, and the
exact one-use authorization of every cancel and sell.

V3 (``CRYPTO_STOP_BREACH_V3``; owner approval 2026-09-29 of the operating session's plan, "use
Coinbase's public market prices as the reference for crypto entry triggers and stop breaches,
while orders stay on Alpaca paper"), a crypto setup admitted under ``CRYPTO_COINBASE_TRIGGER_V1``
(admission records both; ``coinbase_trigger.py``):
1. While the Coinbase feed is healthy for the coin (``coinbase_feed``'s health rule), the breach
   evidence is ``COINBASE_PRINT``: a Coinbase print at or below the stop, at most
   ``REFERENCE_PRINT_MAX_AGE_SECONDS`` old when the protection pass evaluates it, and traded no
   earlier than the pass that first measured the current stop (``stop_since``, as V2). Alpaca's
   bid or prints alone establish nothing.
2. While it is unhealthy (disconnected, unacknowledged, no heartbeat within
   ``coinbase_feed.HEARTBEAT_MAX_AGE_SECONDS``, a skewed clock, a trade-tape gap, or no feed),
   V2's Alpaca evidence applies exactly (``TRADE_PRINT`` or ``BID_HELD``, recorded with
   ``fallback: true``): protection never stops for a feed outage. The held-bid mark is measured
   only during the fallback: the first pass that finds Coinbase healthy again clears it.
3. Everything else is V2's: the marks and their stop, the fallback's 5 s and the market sell
   (``STOP_LIMIT_NOT_FILLED``), and the record, whose body adds ``fallback`` and ``reference``
   (the Coinbase feed's health at the establishing pass).

V4 (``CRYPTO_STOP_BREACH_V4``; owner approval 2026-10-02 of docs/TRADING-QUALITY-PLAN.md A5,
package trade-plan): the native protective stop-limit's limit price is
``LIMIT_CUSHION_FRACTION`` (0.5%) below the native stop (the desired stop raised to the coin's
price grid), rounded down to the grid, never closer than one increment below the native stop
and never below one increment (``crypto_execution.stop_limit_price``); before V4 it is one
increment below the stop. On 2026-10-02 the one-tick limit filled 3 of 12 triggered stops; the
market fallback then slipped 0.7-1.9%. Every crypto setup admitted from this version on records
the exact ``stop_limit_policy`` beside its detection version (V2, or V3 with the Coinbase
reference): detection is unchanged (a print, or the bid held 15 s, then the 5 s fallback, or
V3's Coinbase evidence), and setups admitted before it keep their one-tick limit. The limit
belongs to every protection order the setup sends: the first stop-limit, a re-protection, and
each raised stop (PATCH replace or cancel-then-place), so a raise moves both prices up.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from catalyst_lab import coinbase_feed
from catalyst_lab.crypto_execution import CryptoProtectionPolicy
from catalyst_lab.strategies import core

STOP_BREACH_VERSION = "CRYPTO_STOP_BREACH_V2"
PRINT_MAX_AGE_SECONDS = 5
BID_HOLD_SECONDS = 15
FALLBACK_SECONDS = 5
# Snapshot and quote ages as V1's policy; only the stop-limit timeout differs.
PROTECTION_POLICY = CryptoProtectionPolicy(Decimal(5), Decimal(5), Decimal(FALLBACK_SECONDS))

TRADE_PRINT, BID_HELD = "TRADE_PRINT", "BID_HELD"
ESTABLISHED_EVENT = "STOP_BREACH_ESTABLISHED"
ESTABLISHED_KEY = "stop-breach-established:"  # + setup, lifecycle and the marks' stop_since.


def admission_fields(packet):
    """The state field admission records for a crypto setup; nothing for a stock."""
    if packet.get("market") != "CRYPTO":
        return {}
    return {"stop_breach_version": STOP_BREACH_VERSION}


def active(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("stop_breach_version") == STOP_BREACH_VERSION


def _seconds(later, earlier):
    return Decimal(str((later - earlier).total_seconds()))


def _print_evidence(marks, observation, stop, now):
    """``TRADE_PRINT`` evidence from the observation's print, or None. A missing or unreadable
    print is no evidence: the bid can still establish the breach."""
    if not observation or not observation.get("feed_healthy"):
        return None
    try:
        price = Decimal(str(observation["trade_price"]))
        traded = datetime.fromisoformat(observation["trade_at"])
        age = _seconds(now, traded)
        since = datetime.fromisoformat(marks["stop_since"])
        if (not price.is_finite() or not core.reaches_stop(price, stop)
                or not 0 <= age <= PRINT_MAX_AGE_SECONDS
                or traded < since):
            return None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return {
        "breach_evidence": TRADE_PRINT,
        "evidence_at": observation["trade_at"],
        "evidence_price": str(price),
        "trade_id": observation.get("trade_id"),
        "print_age_seconds": age,
    }


def evaluate(state, *, observation, bid, now):
    """One protection pass over an open position of this version: ``(changes, breach)``.

    ``changes`` are the state fields to record (empty when nothing changed) and ``breach`` the
    ``STOP_BREACH_ESTABLISHED`` body when this pass establishes the breach. ``bid`` is the fresh
    bid exactly as the pass computes it for V1 (None without a fresh quote); ``observation`` is
    the pass's market observation (its print, when present, is the latest one).
    """
    stop = Decimal(str(state["stop"]))
    marks = state.get("stop_breach_marks")
    changes = {}
    if not isinstance(marks, dict) or marks.get("stop") is None or (
            Decimal(str(marks["stop"])) != stop):
        # The first measurement of this stop: earlier marks, and any breach of an earlier stop,
        # are discarded.
        marks = {"stop": state["stop"], "stop_since": now.isoformat(),
                 "bid_since": None, "bid": None, "bid_quote_at": None}
        changes["stop_breach_marks"] = marks
        if state.get("stop_breached_at") is not None or state.get("stop_breach_evidence"):
            changes.update(stop_breached_at=None, stop_breach_evidence=None)
    elif state.get("stop_breached_at"):
        return {}, None  # Established for this stop: final.
    evidence = _print_evidence(marks, observation, stop, now)
    if evidence is None and bid is not None:
        if core.reaches_stop(bid, stop):
            if marks["bid_since"] is None:
                marks = {**marks, "bid_since": now.isoformat(), "bid": str(bid),
                         "bid_quote_at": observation.get("quote_at")}
                changes["stop_breach_marks"] = marks
            held = _seconds(now, datetime.fromisoformat(marks["bid_since"]))
            if held >= BID_HOLD_SECONDS:
                evidence = {
                    "breach_evidence": BID_HELD,
                    "evidence_at": observation.get("quote_at"),
                    "evidence_price": str(bid),
                    "held_since": marks["bid_since"],
                    "held_seconds": held,
                    "first_bid": marks["bid"],
                }
        elif marks["bid_since"] is not None:
            marks = {**marks, "bid_since": None, "bid": None, "bid_quote_at": None}
            changes["stop_breach_marks"] = marks
    if evidence is None:
        return changes, None
    breach = {
        "version": STOP_BREACH_VERSION,
        "lifecycle_id": state.get("lifecycle_id"),
        "stop": state["stop"],
        "stop_since": marks["stop_since"],
        **evidence,
        "established_at": now.isoformat(),
        "fallback_seconds": FALLBACK_SECONDS,
        "fallback_at": (now + timedelta(seconds=FALLBACK_SECONDS)).isoformat(),
    }
    changes.update(stop_breached_at=now.isoformat(), stop_breach_evidence=breach)
    return changes, breach


def established_key(setup_id, breach):
    return f"{ESTABLISHED_KEY}{setup_id}:{breach['lifecycle_id']}:{breach['stop_since']}"


# --- CRYPTO_STOP_BREACH_V3 (Coinbase as the reference market) ---------------------------------

STOP_BREACH_VERSION_V3 = "CRYPTO_STOP_BREACH_V3"
REFERENCE_PRINT_MAX_AGE_SECONDS = 5
COINBASE_PRINT = "COINBASE_PRINT"
# A V2 breach body's frame; V3 keeps its evidence fields and writes its own frame around them.
_V2_FRAME = frozenset({"version", "lifecycle_id", "stop", "stop_since", "established_at",
                       "fallback_seconds", "fallback_at"})


def v3_admission_fields():
    """The state field admission records for a setup of ``CRYPTO_COINBASE_TRIGGER_V1``."""
    return {"stop_breach_version": STOP_BREACH_VERSION_V3}


def active_v3(state):
    """Whether a setup's state was admitted under V3."""
    return isinstance(state, dict) and state.get("stop_breach_version") == STOP_BREACH_VERSION_V3


def _reference_health(reference):
    if isinstance(reference, coinbase_feed.ReferenceView):
        return reference.health()
    return {"provider": coinbase_feed.PROVIDER, "product_id": None, "healthy": False,
            "code": coinbase_feed.FEED_NOT_CONFIGURED}


def _coinbase_evidence(reference, marks, stop, now):
    """``COINBASE_PRINT`` evidence: the most recent retained Coinbase print at or below the
    stop, at most 5 s old and traded no earlier than ``stop_since``; None without one."""
    try:
        since = datetime.fromisoformat(marks["stop_since"])
    except (KeyError, TypeError, ValueError):
        return None
    found = reference.latest_print_at_or_below(
        stop, traded_from=max(since, now - timedelta(seconds=REFERENCE_PRINT_MAX_AGE_SECONDS)),
        traded_to=now)
    if found is None:
        return None
    return {
        "breach_evidence": COINBASE_PRINT,
        "fallback": False,
        "evidence_at": found.traded_at.isoformat(),
        "evidence_price": str(found.price),
        "trade_id": found.trade_id,
        "received_at": found.received_at.isoformat(),
        "print_age_seconds": _seconds(now, found.traded_at),
    }


def evaluate_v3(state, *, observation, bid, reference, now):
    """One protection pass over an open position of V3: ``(changes, breach)`` as ``evaluate``.

    ``reference`` is the coin's ``coinbase_feed.ReferenceView`` at ``now`` (an unavailable view
    without a feed); ``observation`` and ``bid`` are Alpaca's, exactly as V2 reads them, for
    the fallback.
    """
    stop = Decimal(str(state["stop"]))
    marks = state.get("stop_breach_marks")
    changes = {}
    if not isinstance(marks, dict) or marks.get("stop") is None or (
            Decimal(str(marks["stop"])) != stop):
        # The first measurement of this stop: earlier marks, and any breach of an earlier stop,
        # are discarded (as V2).
        marks = {"stop": state["stop"], "stop_since": now.isoformat(),
                 "bid_since": None, "bid": None, "bid_quote_at": None}
        changes["stop_breach_marks"] = marks
        if state.get("stop_breached_at") is not None or state.get("stop_breach_evidence"):
            changes.update(stop_breached_at=None, stop_breach_evidence=None)
    elif state.get("stop_breached_at"):
        return {}, None  # Established for this stop: final.
    if isinstance(reference, coinbase_feed.ReferenceView) and reference.healthy is True:
        if marks.get("bid_since") is not None:
            # A held-bid mark belongs to the fallback; Alpaca's bid no longer decides.
            marks = {**marks, "bid_since": None, "bid": None, "bid_quote_at": None}
            changes["stop_breach_marks"] = marks
        evidence = _coinbase_evidence(reference, marks, stop, now)
    else:
        fallback_changes, fallback = evaluate(
            {**state, "stop_breach_marks": marks, "stop_breached_at": None,
             "stop_breach_evidence": None},
            observation=observation, bid=bid, now=now)
        if "stop_breach_marks" in fallback_changes:
            marks = fallback_changes["stop_breach_marks"]
            changes["stop_breach_marks"] = marks
        evidence = None if fallback is None else {
            **{k: v for k, v in fallback.items() if k not in _V2_FRAME}, "fallback": True}
    if evidence is None:
        return changes, None
    breach = {
        "version": STOP_BREACH_VERSION_V3,
        "lifecycle_id": state.get("lifecycle_id"),
        "stop": state["stop"],
        "stop_since": marks["stop_since"],
        **evidence,
        "reference": _reference_health(reference),
        "established_at": now.isoformat(),
        "fallback_seconds": FALLBACK_SECONDS,
        "fallback_at": (now + timedelta(seconds=FALLBACK_SECONDS)).isoformat(),
    }
    changes.update(stop_breached_at=now.isoformat(), stop_breach_evidence=breach)
    return changes, breach


# --- CRYPTO_STOP_BREACH_V4 (the stop-limit's limit cushion) -----------------------------------

STOP_LIMIT_VERSION_V4 = "CRYPTO_STOP_BREACH_V4"
LIMIT_CUSHION_FRACTION = Decimal("0.005")
LIMIT_ROUNDING = "FLOOR_TO_PRICE_INCREMENT"
STOP_LIMIT_FIELD = "stop_limit_policy"


@dataclass(frozen=True)
class StopLimitPolicy:
    """The exact ``CRYPTO_STOP_BREACH_V4`` record; a state can never carry altered numbers."""

    policy_id: str
    limit_cushion_fraction: str
    limit_rounding: str

    def __post_init__(self):
        if (self.policy_id, self.limit_cushion_fraction, self.limit_rounding) != (
                STOP_LIMIT_VERSION_V4, str(LIMIT_CUSHION_FRACTION), LIMIT_ROUNDING):
            raise ValueError("EXPLICIT_STOP_LIMIT_POLICY_REQUIRED")

    @property
    def cushion(self):
        return Decimal(self.limit_cushion_fraction)

    def record(self):
        return asdict(self)


CRYPTO_STOP_BREACH_V4 = StopLimitPolicy(STOP_LIMIT_VERSION_V4, str(LIMIT_CUSHION_FRACTION),
                                        LIMIT_ROUNDING)
# The stop-limit version admission records for a crypto setup (package trade-plan: V4). None
# records nothing, as every setup admitted before it.
ADMITTED_STOP_LIMIT = CRYPTO_STOP_BREACH_V4


def limit_admission_fields(packet):
    """The ``stop_limit_policy`` admission records for a crypto setup; nothing for a stock."""
    if packet.get("market") != "CRYPTO" or ADMITTED_STOP_LIMIT is None:
        return {}
    return {STOP_LIMIT_FIELD: ADMITTED_STOP_LIMIT.record()}


def recorded_stop_limit(state):
    """The ``CRYPTO_STOP_BREACH_V4`` record a setup's state carries, or None (every setup
    admitted before it). An altered record is refused."""
    recorded = state.get(STOP_LIMIT_FIELD) if isinstance(state, dict) else None
    if recorded is None:
        return None
    if not isinstance(recorded, dict):
        raise ValueError("EXPLICIT_STOP_LIMIT_POLICY_REQUIRED")
    try:
        return StopLimitPolicy(**recorded)
    except TypeError:
        raise ValueError("EXPLICIT_STOP_LIMIT_POLICY_REQUIRED") from None


def limit_cushion(state):
    """The stop-limit cushion a setup recorded (``Decimal("0.005")`` under V4), or None: the
    one-increment limit of every setup admitted before V4."""
    policy = recorded_stop_limit(state)
    return policy.cushion if policy is not None else None
