"""``RISK_SESSION_BASELINE_V2``: the managed engine's New York day-start equity (package baseline).

Owner approval 2026-10-03 ("yes, go with option 1"); docs/packages/baseline.md. Under
``JEV_MANAGED_RISK_V4`` a New York session's day-start equity is the broker account ``equity``
read at the first clean reconciliation at or after New York midnight for that session date
(source ``ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2``), recorded once as an appended
``RISK_SESSION_BASELINE_V2`` managed event. Alpaca's ``last_equity`` follows the US stock
calendar and is not rolled on weekends and holidays, so it is no basis for a 24/7 crypto day.

No migration: ``lab.risk_sessions`` keeps its row exactly as before (migration 006 admits only
``ALPACA_LAST_EQUITY`` rows from a startup reconciliation, and ``lab.daily_risk_halts`` references
the row), so the row stays the honest record of the ``last_equity`` observation, and frozen V1,
the US admission path and policies before V4 keep reading it. A V4 engine reads the V2 event.

A session whose row was written without a V2 baseline (a release before this package, or an
engine under an earlier policy that day) is re-baselined once, at the V4 engine's first clean
reconciliation, with a ``RISK_SESSION_BASELINE_CORRECTED`` event. A soft-limit latch recorded
before the V2 baseline gets one decision at the next account check against the new basis:
``DAILY_SOFT_LOSS_LIMIT_WITHDRAWN`` when the day's P&L is above the soft limit, else
``DAILY_SOFT_LOSS_LIMIT_KEPT``. A hard halt (``lab.daily_risk_halts``) is never lifted.
"""

from datetime import datetime, time
from decimal import Decimal as D

from catalyst_lab.account_risk import MANAGED_RISK_V4_POLICY_ID, SOFT_LIMIT_EVENT, soft_limit_key
from catalyst_lab.market import NY

BASELINE_RULE = "RISK_SESSION_BASELINE_V2"
BASELINE_SOURCE = "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2"
LEGACY_SOURCE = "ALPACA_LAST_EQUITY"
BASELINE_EVENT = "RISK_SESSION_BASELINE_V2"
CORRECTED_EVENT = "RISK_SESSION_BASELINE_CORRECTED"
WITHDRAWN_EVENT = "DAILY_SOFT_LOSS_LIMIT_WITHDRAWN"
KEPT_EVENT = "DAILY_SOFT_LOSS_LIMIT_KEPT"
CORRECTION_REASON = "ALPACA_LAST_EQUITY_NOT_ROLLED"
OWNER_APPROVAL_REF = 'owner approval 2026-10-03 "yes, go with option 1"; package baseline'
# The policies whose engine measures the day from this rule; V3 and older keep the row.
BASELINE_V2_POLICIES = frozenset({MANAGED_RISK_V4_POLICY_ID})


def uses_baseline_v2(policy):
    return policy.policy_id in BASELINE_V2_POLICIES


def baseline_key(day):
    return f"risk-session-baseline-v2:{day.isoformat()}"


def corrected_key(day):
    return f"risk-session-baseline-corrected:{day.isoformat()}"


def latch_decision_key(latch_seq):
    """One decision (withdrawn or kept) per soft latch recorded before the V2 baseline."""
    return f"daily-soft-loss-limit-correction:{latch_seq}"


def _event(conn, key):
    return conn.execute(
        "SELECT * FROM lab.managed_events WHERE idempotency_key=%s", (key,)
    ).fetchone()


def baseline_event(conn, day):
    return _event(conn, baseline_key(day))


def session_baseline(conn, policy, day):
    """The day-start basis the engine's policy measures ``day`` from, or None (no clean
    reconciliation has recorded it yet: the caller fails closed).

    ``{"day_start_equity", "source", "baseline_event_seq"}``; ``baseline_event_seq`` is None
    for the ``lab.risk_sessions`` row every policy before V4 uses.
    """
    if uses_baseline_v2(policy):
        row = baseline_event(conn, day)
        if not row:
            return None
        return {"day_start_equity": D(str(row["body"]["day_start_equity"])),
                "source": BASELINE_SOURCE, "baseline_event_seq": row["event_seq"]}
    row = conn.execute(
        "SELECT day_start_equity,source FROM lab.risk_sessions WHERE session_date=%s", (day,)
    ).fetchone()
    if not row:
        return None
    return {"day_start_equity": row["day_start_equity"], "source": row["source"],
            "baseline_event_seq": None}


def record(conn, store, *, policy, now, account, positions, reconciliation_seq, prior_row,
           last_clean_day, cohort):
    """At a clean reconciliation: the day's V2 baseline, once (an existing one is kept), plus
    the correction event when the day's ``lab.risk_sessions`` row predates it. Returns the
    baseline event or None for a policy before V4."""
    if not uses_baseline_v2(policy):
        return None
    day = now.astimezone(NY).date()
    existing = baseline_event(conn, day)
    if existing:
        return existing
    equity = D(str(account["equity"]))
    if equity <= 0:
        raise ValueError("ACCOUNT_EQUITY_REQUIRED")
    midnight = datetime.combine(day, time(0), tzinfo=NY)
    # Running across midnight: this process's previous clean reconciliation was on an earlier
    # New York date. Otherwise (a start, or a session whose row an earlier process wrote) the
    # day is measured from this mid-session reconciliation.
    mid_session = prior_row is not None or last_clean_day is None or last_clean_day >= day
    open_positions = [
        {"symbol": p.get("symbol"), "qty": str(p.get("qty")),
         "unrealized_pl": str(p.get("unrealized_pl"))}
        for p in positions
    ]
    session_row = conn.execute(
        "SELECT day_start_equity,source,event_seq FROM lab.risk_sessions WHERE session_date=%s",
        (day,),
    ).fetchone()
    body = {
        "rule": BASELINE_RULE, "source": BASELINE_SOURCE, "session_date": day,
        "day_start_equity": equity, "observed_at": now,
        "seconds_after_ny_midnight": int((now - midnight).total_seconds()),
        "started_mid_session": mid_session, "reconciliation_seq": reconciliation_seq,
        "risk_policy_id": policy.policy_id,
        # Positions open now carry their unrealized P&L into the basis: the day measures the
        # change since this observation (since midnight for a process running across it).
        "open_positions": open_positions,
        "open_positions_unrealized_pl": sum(
            (D(str(p.get("unrealized_pl") or 0)) for p in positions), D(0)),
        "alpaca_last_equity": account.get("last_equity"),
        "risk_session_row": session_row and {
            "day_start_equity": session_row["day_start_equity"],
            "source": session_row["source"], "event_seq": session_row["event_seq"]},
        "owner_approval_ref": OWNER_APPROVAL_REF, "cohort": cohort,
    }
    event = store.event(conn, BASELINE_EVENT, body, key=baseline_key(day))
    if prior_row is not None:
        latch = soft_latch(conn, policy, day)[0]
        halted = conn.execute(
            "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s", (day,)
        ).fetchone() is not None
        store.event(conn, CORRECTED_EVENT, {
            "session_date": day, "rule": BASELINE_RULE, "reason": CORRECTION_REASON,
            "old_basis": {"day_start_equity": prior_row["day_start_equity"],
                          "source": prior_row["source"], "event_seq": prior_row["event_seq"]},
            "new_basis": {"day_start_equity": equity, "source": BASELINE_SOURCE,
                          "baseline_event_seq": event["event_seq"]},
            "risk_policy_id": policy.policy_id,
            "soft_latch_event_seq": latch["event_seq"] if latch else None,
            "soft_latch": "DECIDED_AT_NEXT_ACCOUNT_CHECK" if latch else "NONE",
            "hard_halt": "UNCHANGED" if halted else "NONE",
            "owner_approval_ref": OWNER_APPROVAL_REF, "cohort": cohort,
        }, key=corrected_key(day))
    return event


def soft_latch(conn, policy, day):
    """``(latch_event_or_None, key_for_the_next_latch)``: the day's soft latch still in force.
    A withdrawn latch is followed by a fresh one under a key naming the withdrawal."""
    base = soft_limit_key(policy.policy_id, day)
    key = base
    while True:
        latch = _event(conn, key)
        if not latch or latch["kind"] != SOFT_LIMIT_EVENT:
            return None, key
        decision = _event(conn, latch_decision_key(latch["event_seq"]))
        if decision and decision["kind"] == WITHDRAWN_EVENT:
            key = f"{base}:after-withdrawal:{decision['event_seq']}"
            continue
        return latch, key


def pending_decision(conn, latch, baseline):
    """Whether ``latch`` was recorded before the V2 basis it is now measured against and has
    no decision yet."""
    return (latch is not None and baseline is not None
            and baseline["baseline_event_seq"] is not None
            and latch["event_seq"] < baseline["baseline_event_seq"]
            and not _event(conn, latch_decision_key(latch["event_seq"])))


def decide(conn, store, *, policy, day, latch, baseline, total, threshold, reached, cohort):
    """The one decision for a latch recorded before the V2 basis: True when it stays latched."""
    correction = _event(conn, corrected_key(day))
    store.event(conn, KEPT_EVENT if reached else WITHDRAWN_EVENT, {
        "session_date": day, "risk_policy_id": policy.policy_id,
        "latch_event_seq": latch["event_seq"],
        "latch_basis": {k: latch["body"].get(k)
                        for k in ("day_start_equity", "threshold", "total_pnl")},
        "new_basis": {"day_start_equity": baseline["day_start_equity"],
                      "source": baseline["source"],
                      "baseline_event_seq": baseline["baseline_event_seq"]},
        "total_pnl": total, "threshold": threshold,
        "reason": "RISK_SESSION_BASELINE_CORRECTED",
        "correction_event_seq": correction["event_seq"] if correction else None,
        "owner_approval_ref": OWNER_APPROVAL_REF, "cohort": cohort,
    }, key=latch_decision_key(latch["event_seq"]))
    return reached
