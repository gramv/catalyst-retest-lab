"""``CRYPTO_GAP_RESUME_V1``: a pending crypto setup survives a market-stream gap or a restart.

Owner-approved plan ``docs/CRYPTO-AGENT-LOOP.md`` section 5 ("Alpaca stream drops or the app
restarts: resume from the ledger; fetch fills missed while down; resting stop-limits stayed at
Alpaca; pending setups resume only if price did not reach the entry meanwhile") and plan phase 8
(reliability), package gap-resume, 2026-09-27. A named version under the owner's 2026-09-24
ruling. It applies to setups admitted under ``CRYPTO_ALPACA_TRIGGER_V1`` (a crypto setup from a
report-V3 packet); admission records ``gap_resume_version`` in the WATCHING state and everything
here keys off that state field. Every other setup keeps today's revocation on a gap
(``DATA_FEED_FAILURE``) exactly.

1. A market gap (the stream dropped, or a runtime started) no longer revokes such a setup. It
   is held (``GAP_RESUME_PENDING``): no trigger is evaluated for it and no entry can follow
   until its one check. Its queued prints are consumed unevaluated (``GAP_CHECK_PENDING``); the
   check reads them from the ledger.
2. The unobserved window starts at the gap: in process, the time the runtime recorded the gap
   (``RUNTIME_MARKET_GAP``); after a restart, the previous runtime's last recorded observation
   of the market (``gap_resume.as_of`` of its last ``RUNTIME_HEARTBEAT``, or the start of the
   gap it reported open then). It is never later than an earlier gap still open for the setup
   (an unresolved ``GAP_RESUME_PENDING``) or than the setup's earliest queued print that was
   never evaluated, and never earlier than the setup's admission.
3. The check runs once the market stream is back with the setup's symbol acknowledged, the
   trade-updates stream connected and a clean reconciliation of this process, and only after
   the minute in which the stream came back has closed plus ``BAR_SETTLE_SECONDS``. It reads
   Alpaca's completed one-minute bars (GET, the runtime's read-only market source, the tick's one
   market-data REST read) from the minute of the window's start to the last minute that closed
   ``BAR_SETTLE_SECONDS`` ago, and every print the stream delivered for the setup since the
   window's start (the tail after the last bar was watched live by the stream).
4. Decision, exact Decimal comparisons of the lowest traded price (bar lows and prints): bars
   unavailable, incomplete, malformed or not covering the window: ``DATA_FEED_FAILURE``
   (revoked, fail closed, as today); else a price at or below the stop:
   ``STOP_TRADED_DURING_GAP`` (invalidated); else a price at or below the entry trigger:
   ``ENTRY_REACHED_DURING_GAP`` (revoked: the price reached the entry while the system could
   not act; no chasing); else WATCHING resumes (``GAP_RESUMED``). A setup whose entry window
   closes while it is held expires exactly as today.

Minute bars are trade bars: an ask that touched the entry without a trade at or below it is
not visible in them. The first minute of the window is read whole (a minute cannot be split),
so a trade earlier in that minute counts: the rule can only end more setups, never fewer.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from catalyst_lab import crypto_trigger

D = Decimal

GAP_RESUME_VERSION = "CRYPTO_GAP_RESUME_V1"
BAR_SECONDS = 60
# A minute bar is read only once its minute closed this long ago (Alpaca publishes a crypto
# minute bar shortly after the minute; a bar not yet published would hide that minute's trades).
BAR_SETTLE_SECONDS = 30
# The status reports this and the watchdog raises GAP_RESUME_CHECK_OVERDUE when a check has been
# pending longer (a healthy reconnect resolves one in about two minutes).
CHECK_OVERDUE_SECONDS = 300
BAR_SOURCE = "ALPACA_CRYPTO_US_BARS_1MIN"

PENDING_EVENT = "GAP_RESUME_PENDING"
RESUMED_EVENT = "GAP_RESUMED"
RESUMED = "RESUMED"
STOP_TRADED_DURING_GAP = "STOP_TRADED_DURING_GAP"
ENTRY_REACHED_DURING_GAP = "ENTRY_REACHED_DURING_GAP"
DATA_FEED_FAILURE = "DATA_FEED_FAILURE"
# A queued print of a held setup is consumed unevaluated with this reason; the check reads it.
PRINT_CONSUMED_REASON = "GAP_CHECK_PENDING"

# How a restart bounded the window (the pending record's ``basis``).
PREVIOUS_HEARTBEAT = "PREVIOUS_RUNTIME_HEARTBEAT"
NO_PREVIOUS_HEARTBEAT = "NO_PREVIOUS_RUNTIME_HEARTBEAT"
HEARTBEAT_UNUSABLE = "PREVIOUS_HEARTBEAT_WITHOUT_OBSERVATION_RECORD"
LEDGER_UNAVAILABLE = "LEDGER_UNAVAILABLE"
RUNTIME_GAP = "RUNTIME_MARKET_GAP"


def applies(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("gap_resume_version") == GAP_RESUME_VERSION


def admission_fields(packet):
    """The state field admission records: every setup of ``CRYPTO_ALPACA_TRIGGER_V1`` (a crypto
    setup from a report-V3 packet) is admitted under this version; every other setup records
    nothing and keeps today's revocation on a gap."""
    if not crypto_trigger.applies(packet):
        return {}
    return {"gap_resume_version": GAP_RESUME_VERSION}


def _instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("INVALID_TIMESTAMP")
    return result


def _text(value):
    return value.isoformat() if isinstance(value, datetime) else value


def floor_minute(at):
    return at.replace(second=0, microsecond=0)


def ceil_minute(at):
    floor = floor_minute(at)
    return floor if floor == at else floor + timedelta(seconds=BAR_SECONDS)


def due_at(stream_back_at):
    """The check is due once the minute in which the stream came back has closed and settled."""
    return ceil_minute(stream_back_at) + timedelta(seconds=BAR_SETTLE_SECONDS)


def bar_window(window_start, now):
    """``(start, end)`` of the minute bars a check at ``now`` reads: from the minute of the
    window's start to the last minute that closed at least ``BAR_SETTLE_SECONDS`` ago."""
    return floor_minute(window_start), floor_minute(now - timedelta(seconds=BAR_SETTLE_SECONDS))


def window_start(admitted_at, bound, *, open_start=None, unconsumed_print_at=None):
    """The unobserved window's start: the gap's ``bound`` (None: unknown, so the admission),
    never later than an open earlier gap or an unevaluated queued print, never before the
    admission."""
    admitted = _instant(admitted_at)
    if bound is None:
        return admitted
    start = min(x for x in (bound, open_start, unconsumed_print_at) if x is not None)
    return max(admitted, start)


# --- The restart bound: the previous runtime's last recorded observation -----------------------

def restart_bound(row, market, now):
    """``(bound, basis)`` for ``market`` from the previous runtime's last RUNTIME_HEARTBEAT.

    Its ``gap_resume`` section was taken under the runtime lock at ``as_of``: when the market
    was observed then (connected, subscriptions acknowledged, no unprocessed gap) the bound is
    ``as_of``; when a gap was open, the start of that gap (``unobserved_since``); when that start
    was unknown, or the heartbeat carries no such record (an older release), or none exists, the
    bound is None and the window starts at the setup's admission.
    """
    if row is None:
        return None, {"basis": NO_PREVIOUS_HEARTBEAT}
    body = row["body"] if isinstance(row.get("body"), dict) else {}
    basis = {"basis": PREVIOUS_HEARTBEAT, "heartbeat_event_seq": row.get("event_seq"),
             "heartbeat_runtime_id": body.get("runtime_id")}
    try:
        section = body["gap_resume"]
        as_of = _instant(section["as_of"])
        info = section["markets"][market]
        observed = info["observed"]
        since = info.get("unobserved_since")
        since = _instant(since) if since is not None else None
        if type(observed) is not bool:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return None, {**basis, "basis": HEARTBEAT_UNUSABLE}
    basis.update(as_of=as_of.isoformat(), observed=observed,
                 unobserved_since=_text(since))
    if as_of > now:
        return None, {**basis, "basis": HEARTBEAT_UNUSABLE}
    if observed:
        return as_of, basis
    return (min(as_of, since) if since is not None else None), basis


def previous_heartbeat(conn, runtime_id):
    """The newest RUNTIME_HEARTBEAT written by any other runtime (ledger order), or None."""
    return conn.execute(
        """SELECT event_seq,body FROM lab.managed_events
        WHERE setup_id IS NULL AND kind='RUNTIME_HEARTBEAT'
        AND body->>'runtime_id' IS DISTINCT FROM %s ORDER BY event_seq DESC LIMIT 1""",
        (runtime_id,),
    ).fetchone()


def open_pending_start(conn, setup_id):
    """The window start of the setup's gap still open in the ledger: its GAP_RESUME_PENDING
    records after its last GAP_RESUMED (an earlier runtime held it and never checked it)."""
    rows = conn.execute(
        """SELECT kind,body FROM lab.managed_events WHERE setup_id=%s
        AND kind IN (%s,%s) ORDER BY event_seq DESC""",
        (setup_id, PENDING_EVENT, RESUMED_EVENT),
    ).fetchall()
    starts = []
    for row in rows:
        if row["kind"] == RESUMED_EVENT:
            break
        try:
            starts.append(_instant(row["body"]["window_start"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(starts) if starts else None


def earliest_unconsumed_print(conn, setup_id):
    """The trade time of the setup's earliest queued print that was never consumed."""
    return conn.execute(
        """SELECT min((e.body->>'trade_at')::timestamptz) AS at FROM lab.managed_events e
        WHERE e.setup_id=%s AND e.kind='MARKET_PRINT' AND NOT EXISTS(
            SELECT 1 FROM lab.managed_events c
            WHERE c.idempotency_key='market-consumed:'||e.event_seq::text)""",
        (setup_id,),
    ).fetchone()["at"]


def recorded_prints(conn, setup_id):
    """Every print the stream delivered for the setup (MARKET_PRINT bodies, ledger order)."""
    return [
        {"event_seq": row["event_seq"], **(row["body"] if isinstance(row["body"], dict) else {})}
        for row in conn.execute(
            """SELECT event_seq,body FROM lab.managed_events
            WHERE setup_id=%s AND kind='MARKET_PRINT' ORDER BY event_seq""",
            (setup_id,),
        ).fetchall()
    ]


# --- The decision (pure) ------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    decision: str
    evidence: dict

    @property
    def reason(self):
        return None if self.decision == RESUMED else self.decision


def _bar_problem(bar, start, end):
    """Why a returned bar cannot count toward the window (None when it can)."""
    try:
        values = [D(str(getattr(bar, k))) for k in ("open", "high", "low", "close")]
        begins, ends = _instant(bar.start_at), _instant(bar.end_at)
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        return "INVALID_BAR"
    if (any(not v.is_finite() or v <= 0 for v in values) or values[2] > values[1]
            or not values[2] <= min(values[0], values[3]) or max(values[0], values[3]) > values[1]):
        return "INVALID_BAR"
    if floor_minute(begins) != begins or ends - begins != timedelta(seconds=BAR_SECONDS):
        return "BAR_NOT_ONE_MINUTE"
    if begins < start or ends > end:
        return "BAR_OUTSIDE_WINDOW"
    return None


def _bars_digest(bars):
    rows = [[_text(b.start_at), str(b.open), str(b.high), str(b.low), str(b.close),
             str(b.volume)] for b in bars]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def decide(levels, *, window, bars, issues, prints, checked_at, context=None):
    """The check's verdict and its evidence.

    ``levels`` holds ``entry_trigger`` and ``stop``; ``window`` the unobserved window
    (``start``) and the bars read (``bars_start``, ``bars_end``, ``stream_back_at``); ``bars`` the
    completed one-minute bars the source returned; ``issues`` its source issues (anything here
    means the bars are unavailable or incomplete); ``prints`` the setup's recorded prints.
    """
    t, s = D(str(levels["entry_trigger"])), D(str(levels["stop"]))
    start, bars_start, bars_end = (_instant(window[k]) for k in ("start", "bars_start",
                                                                  "bars_end"))
    codes = sorted({str(getattr(issue, "code", issue)) for issue in issues})
    problems, seen, lows = [], set(), []
    for bar in bars:
        problem = _bar_problem(bar, bars_start, bars_end)
        if problem is None and bar.start_at in seen:
            problem = "DUPLICATE_BAR"
        if problem is not None:
            problems.append(problem)
            continue
        seen.add(bar.start_at)
        lows.append((D(str(bar.low)), bar.start_at))
    if not bars_start < bars_end:
        problems.append("WINDOW_NOT_COVERED")
    considered = []
    for printed in prints:
        try:
            at = _instant(printed["trade_at"])
            price = D(str(printed["trade_price"]))
            if not price.is_finite() or price <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, ArithmeticError):
            problems.append("INVALID_RECORDED_PRINT")
            continue
        if start <= at <= checked_at:
            considered.append((price, at, printed.get("trade_id"), printed.get("event_seq")))
    lowest_low = min(lows, key=lambda item: (item[0], item[1])) if lows else None
    lowest_print = min(considered, key=lambda item: (item[0], item[1])) if considered else None
    candidates = [x[0] for x in (lowest_low, lowest_print) if x is not None]
    lowest = min(candidates) if candidates else None
    if codes or problems:
        decision = DATA_FEED_FAILURE
    elif lowest is not None and lowest <= s:
        decision = STOP_TRADED_DURING_GAP
    elif lowest is not None and lowest <= t:
        decision = ENTRY_REACHED_DURING_GAP
    else:
        decision = RESUMED
    evidence = {
        "version": GAP_RESUME_VERSION,
        "decision": decision,
        "checked_at": checked_at.isoformat(),
        "window": {
            "start": start.isoformat(),
            "end": checked_at.isoformat(),
            "bars_start": bars_start.isoformat(),
            "bars_end": bars_end.isoformat(),
            "stream_back_at": _text(window.get("stream_back_at")),
            "bar_settle_seconds": BAR_SETTLE_SECONDS,
        },
        "levels": {"entry_trigger": str(t), "stop": str(s)},
        "bars": {
            "source": BAR_SOURCE,
            "timeframe": "1Min",
            "count": len(lows),
            "first_at": _text(min(seen)) if seen else None,
            "last_at": _text(max(seen)) if seen else None,
            "lowest_low": str(lowest_low[0]) if lowest_low else None,
            "lowest_low_at": _text(lowest_low[1]) if lowest_low else None,
            "sha256": _bars_digest(sorted(bars, key=lambda b: _text(b.start_at)))
            if bars and not problems else None,
            "issues": codes,
            "problems": sorted(set(problems)),
        },
        "prints": {
            "count": len(considered),
            "lowest": str(lowest_print[0]) if lowest_print else None,
            "lowest_at": _text(lowest_print[1]) if lowest_print else None,
            "lowest_trade_id": lowest_print[2] if lowest_print else None,
            "lowest_event_seq": lowest_print[3] if lowest_print else None,
        },
        "lowest": str(lowest) if lowest is not None else None,
        **({"gap": context} if context else {}),
    }
    return Verdict(decision, evidence)


# --- Ledger writes (append-only; only while the setup is still WATCHING) -----------------------

def record_pending(store, setup_id, body, key):
    """The setup's GAP_RESUME_PENDING, once per hold; False when it is no longer WATCHING."""
    with store.transaction() as conn:
        if conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
        ).fetchone():
            return True
        if store.state(conn, setup_id).get("state") != "WATCHING":
            return False
        store.event(conn, PENDING_EVENT, body, setup_id=setup_id, key=key)
    return True


def record_verdict(execution, setup_id, verdict, key):
    """Apply one check's verdict under the shared lock, only while the setup is still WATCHING:
    GAP_RESUMED (keyed); an INVALIDATED revision carrying ``reason`` and ``gap_resume`` (the
    stop traded), as the trigger's own stop invalidation; or a keyed REVOKE through the revoke
    path (the entry was reached, or the bars were unusable). Returns whether it was applied."""
    store = execution.store
    if verdict.decision in {ENTRY_REACHED_DURING_GAP, DATA_FEED_FAILURE}:
        return execution.revoke(setup_id, verdict.decision, watching_only=True,
                                details={"gap_resume": verdict.evidence}, key=key)
    with store.transaction() as conn:
        if conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
        ).fetchone():
            return True
        if store.state(conn, setup_id).get("state") != "WATCHING":
            return False
        if verdict.decision == STOP_TRADED_DURING_GAP:
            store.transition(conn, setup_id, "INVALIDATED", reason=verdict.decision,
                             gap_resume=verdict.evidence)
        else:
            store.event(conn, RESUMED_EVENT, verdict.evidence, setup_id=setup_id, key=key)
    return True
