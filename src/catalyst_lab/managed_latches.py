"""Per-cause, per-setup runtime latches for the managed protection loop.

A latch only gates new entries (``ManagedRuntime.ready``). Mechanical protection
keeps running every tick whatever is latched, and nothing here submits, cancels
or amends an order. The one durable consequence, the persistent-failure halt, is
written by the runtime through the existing ``_latch_execution_halt`` path.

Clearing rules (K is ``LatchPolicy.clear_after_clean_ticks``):

* PROTECTION_TICK_FAILED, transient (broker 5xx, database connection, gate race):
  K consecutive clean protection ticks and a clean reconciliation that completed
  after the last failure.
* PROTECTION_TICK_FAILED, invariant (widening refused, conflicting execution and
  anything unclassified, which fails closed): only an audited
  OPERATOR_PROTECTION_LATCH_CLEARED event recorded after the latch was recorded
  (``managed_ops clear-protection-latch``).
* MANAGED_PROTECTION_PERSISTENT_FAILURE: a protection failure lasting longer than
  ``persistent_failure_seconds`` while a position is open or an entry is working.
  Durable execution halt plus a critical status flag; operator clear only.
* REST_DEGRADED: rate limits and timeouts. Blocks entries, never latches
  protection, clears after K ticks without a REST failure once Retry-After passed.
* RUNTIME_AUDIT_UNAVAILABLE: a successful audit write and a clean reconciliation,
  both after the last failure.
* ACCOUNT_SAFETY_TICK_FAILED: the next clean account-safety tick.

Every set and clear is appended as a managed event under a deterministic key built
from the runtime, cause, scope and episode, so a persistent failure writes one
event rather than one per tick, and a retried write can never duplicate or diverge.
A latch is released only after its clear event is durable.
"""

import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
import psycopg

from catalyst_lab.broker_budget import BrokerRateLimited
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.market import MarketDataError

PROTECTION = "PROTECTION_TICK_FAILED"
PERSISTENT = "MANAGED_PROTECTION_PERSISTENT_FAILURE"
AUDIT = "RUNTIME_AUDIT_UNAVAILABLE"
REST = "REST_DEGRADED"
ACCOUNT_SAFETY = "ACCOUNT_SAFETY_TICK_FAILED"
CAUSES = (PERSISTENT, PROTECTION, AUDIT, REST, ACCOUNT_SAFETY)
ERROR_ORDER = (PERSISTENT, PROTECTION, AUDIT, REST)  # The legacy single ``error`` field.

RUNTIME_SCOPE, TRIGGER_SCOPE = "RUNTIME", "TRIGGERS"
TRANSIENT, INVARIANT, REST_CATEGORY = "TRANSIENT", "INVARIANT", "REST"
AUDIT_CATEGORY, HALT_CATEGORY = "AUDIT", "DURABLE_HALT"
PROTECTION_PHASES = frozenset({"TICK", "TRIGGER", "MANAGE"})
WORKING_STATES = frozenset({"ENTRY_PENDING", "ORDER_SUBMITTED", "OPEN"})

SET_EVENT, CLEARED_EVENT = "RUNTIME_LATCH_SET", "RUNTIME_LATCH_CLEARED"
OPERATOR_CLEAR_EVENT = "OPERATOR_PROTECTION_LATCH_CLEARED"

REST_CODES = frozenset({"ALPACA_HTTP_429", "ALPACA_CONNECTION_ERROR"})
TRANSIENT_BROKER_CODES = frozenset(
    {"ALPACA_HTTP_500", "ALPACA_HTTP_502", "ALPACA_HTTP_503", "ALPACA_HTTP_504",
     "ALPACA_INVALID_JSON"}
)
# Gate refusals of a decision that raced its own five-second expiry or a newer state
# revision. Nothing was sent; the next tick decides again from fresh broker state.
TRANSIENT_GATE_CODES = frozenset({"VALID_UNUSED_RISK_DECISION_REQUIRED", "STALE_POSITION_REVISION"})

CLEAR_RULES = {
    (PROTECTION, TRANSIENT): "K_CLEAN_TICKS_AND_CLEAN_RECONCILIATION",
    (PROTECTION, INVARIANT): "OPERATOR_CLEAR_PROTECTION_LATCH",
    (PERSISTENT, HALT_CATEGORY): "OPERATOR_CLEAR_PROTECTION_LATCH",
    (REST, REST_CATEGORY): "K_CLEAN_TICKS_AFTER_RETRY_AFTER",
    (AUDIT, AUDIT_CATEGORY): "AUDIT_WRITE_AND_CLEAN_RECONCILIATION",
}
_RANK = {TRANSIENT: 0, INVARIANT: 1}


@dataclass(frozen=True)
class LatchPolicy:
    clear_after_clean_ticks: int = 10
    persistent_failure_seconds: int = 30

    def __post_init__(self):
        if (
            type(self.clear_after_clean_ticks) is not int
            or not 3 <= self.clear_after_clean_ticks <= 10
            or type(self.persistent_failure_seconds) is not int
            or not 5 <= self.persistent_failure_seconds <= 300
        ):
            raise ValueError("EXPLICIT_LATCH_POLICY_REQUIRED")


def classify_failure(exc):
    """REST, TRANSIENT or INVARIANT. Anything not known to be transient fails closed."""
    text = str(exc)
    if isinstance(exc, BrokerRateLimited):
        return REST_CATEGORY
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError)):
        return REST_CATEGORY
    if isinstance(exc, MarketDataError):
        if text in REST_CODES:
            return REST_CATEGORY
        if text in TRANSIENT_BROKER_CODES:
            return TRANSIENT
        return INVARIANT
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, ConnectionError)):
        return TRANSIENT
    if isinstance(exc, SubmissionDisabled) and text in TRANSIENT_GATE_CODES:
        return TRANSIENT
    return INVARIANT


def position_possibly_open(state):
    """An open position, or an entry order that may fill while protection is failing."""
    if not isinstance(state, dict):
        return False
    if state.get("state") in WORKING_STATES:
        return True
    try:
        return Decimal(str(state.get("qty") or "0")) > 0
    except (InvalidOperation, ValueError):
        return True  # Unreadable quantity: assume exposure, fail closed.


@dataclass
class TickRecord:
    """What one protection tick observed; the runtime fills it, the book consumes it."""

    protection_failed: bool = False
    rest_failed: bool = False
    failed_scopes: dict = field(default_factory=dict)
    clean_scopes: set = field(default_factory=set)
    setups: dict = field(default_factory=dict)
    active_loaded: bool = False
    completed: bool = False


@dataclass
class Latch:
    cause: str
    scope: str
    episode: int
    category: str
    code: str
    exception_class: str
    phase: str
    first_failure_at: str
    last_failure_mark: int
    setup_id: object = None
    failures: int = 1
    sets: list = field(default_factory=list)
    clear: dict | None = None
    halt_pending: bool = False

    @property
    def operator_only(self):
        return self.cause == PERSISTENT or (self.cause == PROTECTION and self.category == INVARIANT)

    def public(self):
        return {
            "cause": self.cause,
            "scope": self.scope,
            "episode": self.episode,
            "category": self.category,
            "code": self.code,
            "exception_class": self.exception_class,
            "phase": self.phase,
            "first_failure_at": self.first_failure_at,
            "failures": self.failures,
            "clears_by": self.clear_rule(),
            "state": "CLEARING" if self.clear is not None else "LATCHED",
            "recorded": all(entry["written"] for entry in self.sets),
            "durable_halt_pending": self.halt_pending,
        }

    def clear_rule(self):
        if self.cause == ACCOUNT_SAFETY:
            return "CLEAN_ACCOUNT_SAFETY_TICK"
        return CLEAR_RULES[(self.cause, self.category)]


@dataclass(frozen=True)
class PendingEvent:
    kind: str
    key: str
    body: dict
    setup_id: object
    latch: Latch
    entry: dict | None


class LatchBook:
    """Thread-safe latch state. Pure bookkeeping: callers perform every write."""

    def __init__(self, policy, runtime_id):
        if not isinstance(policy, LatchPolicy):
            raise ValueError("EXPLICIT_LATCH_POLICY_REQUIRED")
        self.policy, self.runtime_id = policy, runtime_id
        self._lock = threading.Lock()
        self._mark = 0
        self._active = {}
        self._closing = []
        self._episodes = {}
        self._unrecovered = {}
        self.protection_clean_ticks = 0
        self.rest_clean_ticks = 0
        self._reconciled_mark = 0
        self._audit_mark = 0
        self._safety_mark = 0

    def _next(self):
        self._mark += 1
        return self._mark

    # --- Recording ----------------------------------------------------------

    def record(self, cause, scope, category, code, exception_class, phase, now, *,
               setup_id=None):
        """Set or extend a latch; blocking takes effect before this returns."""
        if cause not in CAUSES:
            raise ValueError("UNKNOWN_LATCH_CAUSE")
        with self._lock:
            mark = self._next()
            if phase in PROTECTION_PHASES:
                self.protection_clean_ticks = 0
            if category == REST_CATEGORY:
                self.rest_clean_ticks = 0
            key = (cause, scope)
            latch = self._active.get(key)
            if latch is None:
                episode = self._episodes.get(key, 0) + 1
                self._episodes[key] = episode
                latch = Latch(cause, scope, episode, category, code, exception_class, phase,
                              now.isoformat(), mark, setup_id)
                self._active[key] = latch
                self._add_set(latch)
            else:
                latch.failures += 1
                latch.last_failure_mark = mark
                if _RANK.get(category, -1) > _RANK.get(latch.category, -1):
                    latch.category, latch.code = category, code
                    latch.exception_class, latch.phase = exception_class, phase
                    self._add_set(latch)
            if cause == PROTECTION and phase in {"TICK", "MANAGE"}:
                self._unrecovered.setdefault(scope, now)
            return latch

    def _add_set(self, latch):
        body = {
            "cause": latch.cause,
            "scope": latch.scope,
            "episode": latch.episode,
            "category": latch.category,
            "code": latch.code,
            "exception_class": latch.exception_class,
            "phase": latch.phase,
            "first_failure_at": latch.first_failure_at,
            "clears_by": latch.clear_rule(),
            "clear_after_clean_ticks": self.policy.clear_after_clean_ticks,
            "persistent_failure_seconds": self.policy.persistent_failure_seconds,
        }
        key = (
            f"runtime-latch:{self.runtime_id}:{latch.cause}:{latch.scope}:"
            f"{latch.episode}:SET:{latch.category}"
        )
        latch.sets.append({"key": key, "body": body, "written": False, "seq": None})

    def note_clean_reconciliation(self):
        with self._lock:
            self._reconciled_mark = self._next()

    def note_audit_success(self):
        with self._lock:
            self._audit_mark = self._next()

    def note_safety_success(self):
        with self._lock:
            self._safety_mark = self._next()

    def finish_tick(self, tick, now, open_scopes):
        """Advance clean-tick counters and escalate persistent protection failures."""
        with self._lock:
            if not tick.protection_failed:
                self.protection_clean_ticks += 1
            if not tick.rest_failed:
                self.rest_clean_ticks += 1
            for scope in tick.clean_scopes:
                self._unrecovered.pop(scope, None)
            escalated = []
            for scope, setup_id in tick.failed_scopes.items():
                since = self._unrecovered.get(scope)
                if since is None or (now - since).total_seconds() <= (
                    self.policy.persistent_failure_seconds
                ):
                    continue
                exposed = scope in open_scopes or (scope == RUNTIME_SCOPE and bool(open_scopes))
                if not exposed or (PERSISTENT, scope) in self._active:
                    continue
                source = self._active.get((PROTECTION, scope))
                episode = self._episodes.get((PERSISTENT, scope), 0) + 1
                self._episodes[(PERSISTENT, scope)] = episode
                latch = Latch(
                    PERSISTENT, scope, episode, HALT_CATEGORY,
                    source.code if source else "PROTECTION_TICK_FAILED",
                    source.exception_class if source else "UNKNOWN",
                    source.phase if source else "MANAGE",
                    since.isoformat(), self._next(), setup_id, halt_pending=True,
                )
                self._active[(PERSISTENT, scope)] = latch
                self._add_set(latch)
                escalated.append(latch)
            return escalated

    # --- Clearing -----------------------------------------------------------

    def close_due(self, cooldown_remaining=0.0):
        """Freeze the clear event of every latch whose automatic rule is satisfied."""
        k = self.policy.clear_after_clean_ticks
        with self._lock:
            for key, latch in list(self._active.items()):
                if latch.operator_only or not all(entry["written"] for entry in latch.sets):
                    continue  # The set event must precede the clear in the audit log.
                last = latch.last_failure_mark
                if latch.cause == PROTECTION:
                    due = self.protection_clean_ticks >= k and self._reconciled_mark > last
                elif latch.cause == REST:
                    due = self.rest_clean_ticks >= k and cooldown_remaining <= 0
                elif latch.cause == AUDIT:
                    due = self._audit_mark > last and self._reconciled_mark > last
                else:
                    due = self._safety_mark > last
                if due:
                    self._close(key, latch, latch.clear_rule(), None)

    def operator_clear_floor(self):
        """Lowest recorded set-event sequence an operator clear must follow, if any."""
        with self._lock:
            seqs = [
                latch.sets[-1]["seq"]
                for latch in self._active.values()
                if latch.operator_only and not latch.halt_pending
                and latch.sets[-1]["written"] and latch.sets[-1]["seq"] is not None
            ]
            return min(seqs) if seqs else None

    def operator_clear(self, operator_event_seq):
        """Release operator-only latches recorded before the operator's audited event."""
        with self._lock:
            released = []
            for key, latch in list(self._active.items()):
                last = latch.sets[-1]
                if (
                    latch.operator_only
                    and not latch.halt_pending
                    and last["written"]
                    and last["seq"] is not None
                    and last["seq"] < operator_event_seq
                ):
                    self._close(key, latch, "OPERATOR_CLEAR_PROTECTION_LATCH", operator_event_seq)
                    released.append(latch)
            return released

    def _close(self, key, latch, rule, operator_event_seq):
        del self._active[key]
        body = {
            "cause": latch.cause,
            "scope": latch.scope,
            "episode": latch.episode,
            "category": latch.category,
            "cleared_by": rule,
        }
        if operator_event_seq is not None:
            body["operator_event_seq"] = operator_event_seq
        latch.clear = {
            "key": (
                f"runtime-latch:{self.runtime_id}:{latch.cause}:{latch.scope}:"
                f"{latch.episode}:CLEARED"
            ),
            "body": body,
        }
        self._closing.append(latch)

    # --- Durable writes -----------------------------------------------------

    def halts_pending(self):
        with self._lock:
            return [latch for latch in self._active.values() if latch.halt_pending]

    def halt_recorded(self, latch):
        with self._lock:
            latch.halt_pending = False

    def next_event(self):
        """The oldest unwritten set event, else the first clear whose sets are written."""
        with self._lock:
            for latch in list(self._active.values()) + self._closing:
                if latch.halt_pending:
                    continue  # Record the escalation only once its halt is durable.
                for entry in latch.sets:
                    if not entry["written"]:
                        return PendingEvent(SET_EVENT, entry["key"], entry["body"],
                                            latch.setup_id, latch, entry)
            for latch in self._closing:
                if all(entry["written"] for entry in latch.sets):
                    return PendingEvent(CLEARED_EVENT, latch.clear["key"], latch.clear["body"],
                                        latch.setup_id, latch, None)
            return None

    def event_written(self, item, row):
        with self._lock:
            self._audit_mark = self._next()
            if item.entry is not None:
                item.entry["written"] = True
                seq = row.get("event_seq") if isinstance(row, dict) else None
                item.entry["seq"] = seq if isinstance(seq, int) else None
            elif item.latch in self._closing:
                self._closing.remove(item.latch)

    # --- Views --------------------------------------------------------------

    def _all(self):
        return list(self._active.values()) + list(self._closing)

    def blocking(self):
        with self._lock:
            return bool(self._active or self._closing)

    def error(self):
        with self._lock:
            causes = {latch.cause for latch in self._all()}
        return next((cause for cause in ERROR_ORDER if cause in causes), None)

    def critical(self):
        with self._lock:
            return any(latch.cause == PERSISTENT for latch in self._all())

    def has(self, cause, scope=None):
        with self._lock:
            return any(
                latch.cause == cause and (scope is None or latch.scope == scope)
                for latch in self._all()
            )

    def public(self):
        with self._lock:
            latches = sorted(
                (latch.public() for latch in self._all()),
                key=lambda row: (row["cause"], row["scope"], row["episode"]),
            )
            return {
                "active": latches,
                "protection_clean_ticks": self.protection_clean_ticks,
                "rest_clean_ticks": self.rest_clean_ticks,
                "clear_after_clean_ticks": self.policy.clear_after_clean_ticks,
                "persistent_failure_seconds": self.policy.persistent_failure_seconds,
            }
