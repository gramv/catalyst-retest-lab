"""``EARLY_EXIT_FLAG_V1``: early-exit flags between 24-hour reviews (plan 4.6.3).

Either side can start an early-exit review: Jev flags the reason for the trade as broken in a
maintenance review (``CRYPTO_MAINTENANCE_V1``, package maintenance), or the proposing agent
posts a flag with its reasons and news (plan phase 6, a later package). A flag never changes the
trade: it keeps its stop and target until the flag is resolved. The other side is asked at once
and has 15 minutes (``ANSWER_WINDOW_SECONDS``); both saying exit means a market sell, anything
else keeps the trade. Asking the other side and deciding is phase 6; this module is the record
both phases share, so phase 6 consumes and resolves flags without changing the Jev side:

* ``raise_exit_flag``: one append-only ``EXIT_FLAG_RAISED`` (who raised it, why, the evidence,
  e.g. the review request and its receipts, the levels and quote at the flag). While a flag of
  the same side is pending for the trade's lifecycle, a new one is not added: the pending flag is
  returned (a repeated answer reaffirms it; the maintenance decision records that).
* ``pending_exit_flags``: flags without a resolution whose trade is still open in the flag's
  lifecycle, oldest first (what phase 6 answers, and what the runtime status alarms on).
* ``resolve_exit_flag``: one append-only ``EXIT_FLAG_RESOLVED`` per flag (``EXIT_AGREED``,
  ``EXIT_NOT_AGREED``, ``NO_ANSWER_IN_TIME``, ``LIFECYCLE_ENDED`` or, for a
  ``CRYPTO_MAINTENANCE_V5`` invalidation flag left unanswered, ``NO_ANSWER_EXIT``) with the other
  side's answer. ``EXIT_AGREED`` and ``NO_ANSWER_EXIT`` also request the exit (state
  ``exit_requested`` = ``EARLY_EXIT_AGREED``) in the same transaction while the trade is open
  in that lifecycle and not already exiting; the protection loop then cancels the resting
  stop-limit and sells at market, each under its own one-use five-second authorization. A
  second resolution is refused.

Callers pass an open ledger transaction (``ManagedStore.transaction``: the shared advisory lock).
"""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from catalyst_lab.crypto_maintenance import EXIT_FLAG_VERSION

FLAG_EVENT = "EXIT_FLAG_RAISED"
RESOLUTION_EVENT = "EXIT_FLAG_RESOLVED"
SIDES = frozenset({"JEV", "AGENT"})
# NO_ANSWER_EXIT (CRYPTO_MAINTENANCE_V5, package jev-b1): a flag whose reasons say it exits when
# unanswered (``if_unanswered`` EXIT: a confirmed invalidation) had no answer by its deadline.
NO_ANSWER_EXIT = "NO_ANSWER_EXIT"
OUTCOMES = frozenset({"EXIT_AGREED", "EXIT_NOT_AGREED", "NO_ANSWER_IN_TIME", "LIFECYCLE_ENDED",
                      NO_ANSWER_EXIT})
EXIT_OUTCOMES = frozenset({"EXIT_AGREED", NO_ANSWER_EXIT})  # Both request the market sell.
EARLY_EXIT_REASON = "EARLY_EXIT_AGREED"
ANSWER_WINDOW_SECONDS = 900
_PENDING_SQL = """
SELECT e.event_seq,e.setup_id,e.body,e.recorded_at FROM lab.managed_events e
JOIN lab.managed_states t ON t.setup_id=e.setup_id
WHERE e.kind=%(flag)s
  AND (%(setup)s::uuid IS NULL OR e.setup_id=%(setup)s::uuid)
  AND (%(lifecycle)s::text IS NULL OR e.body->>'lifecycle_id'=%(lifecycle)s::text)
  AND (%(side)s::text IS NULL OR e.body->>'side'=%(side)s::text)
  AND t.body->>'state'='OPEN' AND t.body->>'lifecycle_id'=e.body->>'lifecycle_id'
  AND NOT EXISTS(SELECT 1 FROM lab.managed_events r WHERE r.kind=%(resolution)s
                 AND r.body->>'flag_id'=e.body->>'flag_id')
ORDER BY e.event_seq
"""


def _text(value):
    return value.isoformat() if isinstance(value, datetime) else value


def flag_id_for(key):
    return str(uuid5(NAMESPACE_URL, "exit-flag:" + key))


def pending_exit_flags(conn, *, setup_id=None, lifecycle_id=None, side=None):
    """Unresolved flags of trades still open in the flag's lifecycle, oldest first."""
    return conn.execute(_PENDING_SQL, {
        "flag": FLAG_EVENT, "resolution": RESOLUTION_EVENT,
        "setup": str(setup_id) if setup_id is not None else None,
        "lifecycle": str(lifecycle_id) if lifecycle_id is not None else None, "side": side,
    }).fetchall()


def raise_exit_flag(store, conn, *, setup_id, lifecycle_id, side, raised_by, reasons, evidence,
                    raised_at, reference, question=None):
    """``(row, created)``: one EXIT_FLAG_RAISED, or the side's pending flag for the lifecycle.

    ``reference`` makes the flag idempotent (the Jev side uses its review request ID);
    ``raised_by`` names the raiser (Jev: the request, receipts and context hash; an agent: its
    agent ID), ``reasons`` why, ``evidence`` the levels, quote and anything else at the flag.
    ``question`` (``CRYPTO_MAINTENANCE_V5``: ``invalidation_met`` or ``news_contradicts``, also
    recorded as ``reasons.question``): only a pending flag of the same question is returned, so a
    confirmed invalidation is raised (and exits when unanswered) even while a news flag waits.
    """
    if side not in SIDES:
        raise ValueError("EXIT_FLAG_SIDE_INVALID")
    pending = pending_exit_flags(conn, setup_id=setup_id, lifecycle_id=lifecycle_id, side=side)
    if question is not None:
        pending = [row for row in pending
                   if (row["body"].get("reasons") or {}).get("question") == question]
    if pending:
        return pending[0], False
    key = f"exit-flag:{setup_id}:{lifecycle_id}:{side}:{reference}"
    raised = raised_at if isinstance(raised_at, datetime) else datetime.fromisoformat(raised_at)
    row = store.event(conn, FLAG_EVENT, {
        "flag_version": EXIT_FLAG_VERSION,
        "flag_id": flag_id_for(key),
        "setup_id": str(setup_id),
        "lifecycle_id": str(lifecycle_id),
        "side": side,
        "raised_by": raised_by,
        "reasons": reasons,
        "evidence": evidence,
        "raised_at": _text(raised),
        "answer_due_at": (raised + timedelta(seconds=ANSWER_WINDOW_SECONDS)).isoformat(),
        "trade_levels_unchanged": True,
    }, setup_id=setup_id, key=key)
    return row, True


def resolve_exit_flag(store, conn, *, flag_id, outcome, answered_by, answer, resolved_at):
    """One EXIT_FLAG_RESOLVED; ``EXIT_AGREED`` requests the market sell (see the module)."""
    if outcome not in OUTCOMES:
        raise ValueError("EXIT_FLAG_OUTCOME_INVALID")
    flag = conn.execute(
        "SELECT * FROM lab.managed_events WHERE kind=%s AND body->>'flag_id'=%s",
        (FLAG_EVENT, str(flag_id)),
    ).fetchone()
    if flag is None:
        raise ValueError("EXIT_FLAG_MISSING")
    if conn.execute(
        "SELECT 1 FROM lab.managed_events WHERE kind=%s AND body->>'flag_id'=%s",
        (RESOLUTION_EVENT, str(flag_id)),
    ).fetchone():
        raise ValueError("EXIT_FLAG_ALREADY_RESOLVED")
    body = flag["body"]
    setup_id = flag["setup_id"]
    state = store.state(conn, setup_id)
    open_now = state.get("state") == "OPEN" and state.get("lifecycle_id") == body["lifecycle_id"]
    exit_requested = None
    if outcome in EXIT_OUTCOMES and open_now and not state.get("exit_requested"):
        exit_requested = EARLY_EXIT_REASON
    row = store.event(conn, RESOLUTION_EVENT, {
        "flag_version": EXIT_FLAG_VERSION,
        "flag_id": body["flag_id"],
        "setup_id": str(setup_id),
        "lifecycle_id": body["lifecycle_id"],
        "outcome": outcome,
        "answered_by": answered_by,
        "answer": answer,
        "resolved_at": _text(resolved_at),
        "trade_open": open_now,
        "exit_requested": exit_requested,
    }, setup_id=setup_id, key="exit-flag-resolved:" + body["flag_id"])
    if exit_requested:
        store.transition(conn, setup_id, state["state"], exit_requested=exit_requested)
    return row
