"""``CRYPTO_ENTRY_WORKING_LIMIT_V1``: how long an unfilled crypto entry works, and what ends it
before it fills (six-day operation, owner mandate 2026-09-28: engine defects are fixed as named
versions).

Evidence (2026-09-29). An ONDO/USD setup triggered at 13:44:02 UTC and the app sent a limit buy at
the max entry M = 0.5163734 for 1,832 coins. Alpaca's ask was 0.5154 at the trigger and later sat
at 0.5122-0.5126 with about 92k coins offered, below the limit, yet Alpaca's paper engine had not
filled the order more than an hour later; Alpaca's ONDO market printed no trade in that time. All
8 earlier live entries filled within about a second.

Before this version (every crypto setup admitted before it, unchanged): an entry with nothing
filled keeps working (``plan_crypto_recovery``: ``ENTRY_WORKING`` / ``AWAIT_FIRST_FILL``) until the
entry window closes (the packet's expiry or the crypto entry cutoff, ``ENTRY_EXPIRED``), up to
about a day for a report-V3 pick, and nothing cancels it when the price trades through the stop,
so a late fill can land far from the plan or below the stop.

Under this version (admission records ``entry_working_version`` on every new crypto setup):

1. Fill window. ``entry_acknowledged_at`` is the app's clock when it records the broker's
   acknowledgement of the entry order (``BROKER_ACK``); for an entry whose response was lost, when
   reconciliation locates it. An entry with nothing filled ``ENTRY_FILL_WINDOW_SECONDS`` (300)
   after it is cancelled: ``ENTRY_NOT_FILLED``.
2. Stop crossed before the fill. While the entry works with nothing filled, the evidence the
   setup's own stop rule reads, at or below the stop and measured from the acknowledgement,
   cancels it at once: ``STOP_CROSSED_BEFORE_FILL``. Under ``CRYPTO_STOP_BREACH_V3`` that is a
   Coinbase print at or below the stop while Coinbase is healthy for the coin, and V2's Alpaca
   evidence while it is not (``fallback: true``); under ``CRYPTO_STOP_BREACH_V2`` an Alpaca print
   at or below the stop (``TRADE_PRINT``) or Alpaca's fresh bid at or below it for 15 s
   (``BID_HELD``). ``stop_breach.evaluate`` / ``evaluate_v3`` decide, on this version's own marks
   (``entry_stop_marks``), never the position's. The stop is checked before the window.
3. The decision is final for the lifecycle. The pass that makes it appends
   ``ENTRY_WORKING_CANCEL`` (reason, order age, evidence) and records ``entry_working_cancel``;
   from then on the protection plan cancels the working entry (``CANCELING`` with the reason)
   under its own exact one-use authorization, again on a later pass if a cancel is refused or
   not claimed. Once the broker shows the entry closed with nothing filled and no position, the
   setup is ``CLOSED`` with the reason and its risk reservation is released, as every closed
   setup's (``BROKER_AND_ORDERS_FLAT``). It never returns to WATCHING: a setup's entry client
   order ID is derived from the setup, so a second entry would need a new identity; a later
   research run can pick the coin again as a new setup.
4. Partial fill. A filled quantity is kept and protected exactly as before. Under
   ``CRYPTO_PARTIAL_ENTRY_V1`` the rest keeps working as that rule says, but no longer than the
   fill window: at the window it is cancelled, ``ENTRY_REMAINDER_NOT_FILLED``
   (``PARTIAL_ENTRY_REMAINDER_CANCEL`` and ``ENTRY_WORKING_CANCEL``). Without that rule the rest is
   cancelled at once, as before.
5. A fill that races the cancel is the broker's: the next pass sees the position and protects it
   as any fill (the native stop-limit; no exit is requested), and an entry remainder still working
   is cancelled (under ``CRYPTO_PARTIAL_ENTRY_V1`` with the decision's reason).

Everything else is unchanged: the entry order (a limit at M), its authorization, expiry,
revocation, halts and every exit.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from catalyst_lab import stop_breach

ENTRY_WORKING_VERSION = "CRYPTO_ENTRY_WORKING_LIMIT_V1"
# Alpaca's forum reports paper fill delays of 50-260 s; 8 of 8 live entries filled in about 1 s.
ENTRY_FILL_WINDOW_SECONDS = 300

ENTRY_NOT_FILLED = "ENTRY_NOT_FILLED"
STOP_CROSSED_BEFORE_FILL = "STOP_CROSSED_BEFORE_FILL"
ENTRY_REMAINDER_NOT_FILLED = "ENTRY_REMAINDER_NOT_FILLED"
REASONS = frozenset({ENTRY_NOT_FILLED, STOP_CROSSED_BEFORE_FILL, ENTRY_REMAINDER_NOT_FILLED})

CANCEL_EVENT = "ENTRY_WORKING_CANCEL"
CANCEL_KEY = "entry-working-cancel:"  # + setup and lifecycle: one decision per lifecycle.
# A breach body's frame (``stop_breach``): before the fill there is no position and no fallback
# sale, so the record keeps the evidence and names the stop rule that read it.
_BREACH_FRAME = frozenset({"version", "lifecycle_id", "established_at", "fallback_seconds",
                           "fallback_at"})


def admission_fields(packet):
    """The state field admission records for a crypto setup; nothing for a stock."""
    if packet.get("market") != "CRYPTO":
        return {}
    return {"entry_working_version": ENTRY_WORKING_VERSION}


def active(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("entry_working_version") == ENTRY_WORKING_VERSION


def acknowledgement_fields(state, now):
    """The field recorded with the entry's acknowledgement, once, for a setup of this version."""
    if active(state) and not state.get("entry_acknowledged_at"):
        return {"entry_acknowledged_at": now.isoformat()}
    return {}


def window_ends_at(state):
    """When the fill window ends, or None before the acknowledgement."""
    acknowledged = state.get("entry_acknowledged_at") if isinstance(state, dict) else None
    if not acknowledged:
        return None
    return datetime.fromisoformat(acknowledged) + timedelta(seconds=ENTRY_FILL_WINDOW_SECONDS)


def decided(state):
    """The reason this version ended the setup's entry, or None."""
    if not active(state):
        return None
    decision = state.get("entry_working_cancel")
    return decision.get("reason") if isinstance(decision, dict) else None


def remainder_reason(state, now):
    """Why ``CRYPTO_PARTIAL_ENTRY_V1``'s working remainder is cancelled under this version, or None:
    the reason of a decision already recorded (it ended the whole entry), else
    ``ENTRY_REMAINDER_NOT_FILLED`` once the fill window has ended."""
    if not active(state):
        return None
    recorded = decided(state)
    if recorded:
        return recorded
    ends = window_ends_at(state)
    return ENTRY_REMAINDER_NOT_FILLED if ends is not None and now >= ends else None


def partial_entry_fields(state):
    """What ``CRYPTO_PARTIAL_ENTRY_V1``'s records add for a setup of this version: when the fill
    window ends (the rest works until the earlier of that and the rule's own deadline)."""
    if not active(state):
        return {}
    ends = window_ends_at(state)
    return {"fill_window_ends_at": ends.isoformat() if ends else None}


def marks(state):
    """The entry's stop marks: the recorded ones for the current stop, else a first measurement
    from the acknowledgement (``stop_breach``'s shape; not recorded until it changes)."""
    recorded = state.get("entry_stop_marks")
    if isinstance(recorded, dict) and recorded.get("stop") is not None and (
            Decimal(str(recorded["stop"])) == Decimal(str(state["stop"]))):
        return recorded
    return {"stop": state["stop"], "stop_since": state["entry_acknowledged_at"],
            "bid_since": None, "bid": None, "bid_quote_at": None}


def stop_crossed(state, *, observation, bid, reference, now):
    """One pass of the stop check for an entry that has filled nothing: ``(marks, evidence)``.

    ``state`` carries ``entry_acknowledged_at``. The setup's stop rule decides on this version's
    marks: ``stop_breach.evaluate_v3`` under ``CRYPTO_STOP_BREACH_V3`` (``reference`` is the coin's
    Coinbase view), else ``stop_breach.evaluate`` (V2's Alpaca evidence; every setup of this
    version records V2 or V3). ``observation`` and ``bid`` are Alpaca's, exactly as the protection
    pass reads them for an open position. ``evidence`` is None, or the evidence and the stop rule
    that established it.
    """
    current = marks(state)
    view = {
        "stop": state["stop"],
        "lifecycle_id": state.get("lifecycle_id"),
        "stop_breach_version": state.get("stop_breach_version"),
        "stop_breach_marks": current,
        "stop_breached_at": None,
        "stop_breach_evidence": None,
    }
    if stop_breach.active_v3(state):
        changes, breach = stop_breach.evaluate_v3(
            view, observation=observation, bid=bid, reference=reference, now=now)
    else:
        changes, breach = stop_breach.evaluate(view, observation=observation, bid=bid, now=now)
    current = changes.get("stop_breach_marks", current)
    if breach is None:
        return current, None
    return current, {"stop_rule": breach["version"],
                     **{k: v for k, v in breach.items() if k not in _BREACH_FRAME}}


def cancel_body(state, reason, *, order_ids, filled_qty, remaining_qty, now, evidence=None):
    """The ``ENTRY_WORKING_CANCEL`` body: the reason, the order's age and the evidence."""
    acknowledged = state.get("entry_acknowledged_at")
    ends = window_ends_at(state)
    age = None
    if acknowledged:
        age = Decimal(str((now - datetime.fromisoformat(acknowledged)).total_seconds()))
    return {
        "version": ENTRY_WORKING_VERSION,
        "reason": reason,
        "lifecycle_id": state.get("lifecycle_id"),
        "entry_order_ids": list(order_ids),
        "acknowledged_at": acknowledged,
        "fill_window_seconds": ENTRY_FILL_WINDOW_SECONDS,
        "window_ends_at": ends.isoformat() if ends else None,
        "order_age_seconds": age,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "stop": state.get("stop"),
        "stop_evidence": evidence,
        "decided_at": now.isoformat(),
    }


def cancel_key(setup_id, state):
    return f"{CANCEL_KEY}{setup_id}:{state.get('lifecycle_id')}"


def decision(reason, now):
    """The state's record of the decision."""
    return {"reason": reason, "decided_at": now.isoformat()}
