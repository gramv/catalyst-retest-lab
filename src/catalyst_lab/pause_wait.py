"""``OPERATOR_PAUSE_ENTRY_WAIT_V1``: a trigger during an operator pause waits instead of ending the
setup (owner, 2026-09-29: "you take the right decision. And fix all these issues.").

Before this version (every setup admitted before it, unchanged): ``authorize_entry`` turns any
unreleased ``lab.execution_halts`` row into the terminal ``RISK_HALT`` and the setup becomes
``RISK_REJECTED``, so a pick whose trigger fires during an ``OPERATOR_PAUSE`` (migration 015, an
entry-only pause) is lost for good, although admission retries a pick while halted.

Under this version (admission records ``pause_wait_version`` on every new setup), a trigger of a
WATCHING setup while every unreleased halt is an ``OPERATOR_PAUSE``, and no daily-loss halt is
recorded for the New York day, is not evaluated: ``authorize_entry`` records one
``OPERATOR_PAUSE_ENTRY_WAIT`` per setup and UTC minute and returns before any broker read, risk
decision, reservation or order. The setup keeps WATCHING, so a trigger after ``operator resume``
is evaluated afresh, like any trigger.

Unchanged: any other halt, alone or beside a pause, is the terminal ``RISK_HALT``; the
daily-loss halt stays terminal and cancels and flattens (the account-safety tick records it
before any trigger of the pass is evaluated); admission, the dispatch gate and every exit.
"""

from datetime import UTC

PAUSE_WAIT_VERSION = "OPERATOR_PAUSE_ENTRY_WAIT_V1"
PAUSE_REASON = "OPERATOR_PAUSE"
WAIT_EVENT = "OPERATOR_PAUSE_ENTRY_WAIT"
WAIT_KEY = "operator-pause-entry-wait:"  # + setup and UTC minute: at most one per minute.
TRIGGER_FIELDS = ("trade_price", "trade_at", "trade_id", "bid", "ask", "quote_at",
                  "quote_read_at")


def admission_fields(packet):
    """The state field admission records for every new setup."""
    return {"pause_wait_version": PAUSE_WAIT_VERSION}


def active(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("pause_wait_version") == PAUSE_WAIT_VERSION


def pause_halts(conn):
    """The unreleased halts when there is at least one and every one is an operator pause;
    otherwise ``()``."""
    rows = conn.execute(
        "SELECT event_seq,reason FROM lab.execution_halts ORDER BY event_seq"
    ).fetchall()
    return rows if rows and all(r["reason"] == PAUSE_REASON for r in rows) else ()


def wait_key(setup_id, now):
    minute = now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    return f"{WAIT_KEY}{setup_id}:{minute}"


def wait_body(halts, observation, now):
    return {
        "reason": PAUSE_REASON,
        "version": PAUSE_WAIT_VERSION,
        "halt_ids": [h["event_seq"] for h in halts],
        "trigger": {k: observation.get(k) for k in TRIGGER_FIELDS if k in (observation or {})},
        "waited_at": now.isoformat(),
    }
