"""Holding policies of a report-V3 crypto setup (plan 4.4, 4.6.4 and 4.6.6).

``CRYPTO_24H_HOLD_V1`` (package crypto-size-hold, 2026-09-27). Owner decisions relayed for plan
phase 4b: hold up to 24 hours, then the agent and Jev decide whether to continue; that review
was a later phase, so until it existed the position exits at 24 hours. No midnight close: a
setup under this policy has no New York entry cutoff and no midnight flatten. Entries are
allowed only until the setup's own expiry, which is the next research run (the pick's validity,
and ``RESEARCH_RUN_SUPERSESSION_V1`` retires it when the next run's shortlist goes live). Its
hard exit is 24 hours after the first recorded buy fill, including partial fills and time the
runtime was offline; a later fill never restarts or extends it. Its exit reason is
``HOLD_24H_EXIT``.

``CRYPTO_24H_REVIEW_V1`` (package day-review, plan phase 6, 2026-09-27). The 24-hour
continue-or-exit review (plan 4.6.4) replaces ``CRYPTO_24H_HOLD_V1``'s forced exit for the
maintained arm only: a report-V3 crypto setup whose randomized arm is ``JEV_MANAGED`` records
this policy at admission; the 30% ``FIXED_EXIT`` control arm (plan 4.6.6) keeps
``CRYPTO_24H_HOLD_V1`` and exits at 24 hours. T (``day_review_at``) is 24 hours after the first
buy fill, then every 24 hours after a continue (T + 24 h, T + 48 h, ...); the review itself is
``trade_review.DayReviews`` (rules in ``day_review``). A trade under this policy has no forced
exit at T: it continues only on a recorded CONTINUE and exits on a recorded EXIT
(``DAY_REVIEW_EXIT``). Its ``hard_exit_at`` is a fail-safe, T plus the longest a review can take
(Jev 30 minutes, one 15-minute discussion round, Jev 30 minutes again) plus 5 minutes' grace:
if no review has decided by then (the review component is not running), the protection loop
sells at market with ``DAY_REVIEW_DEADLINE_EXIT``; the fail-safe never renames an exit already
requested. A continue moves both T and the fail-safe 24 hours on.

``CRYPTO_24H_REVIEW_V2`` (package answer-rules, 2026-09-27) is ``CRYPTO_24H_REVIEW_V1`` with the
answer rule ``DAY_REVIEW_ANSWER_RULE_V2`` (``day_review.read_review_answer_v2``: ``decision``
decides; an option answer raises a level only when it is the unique most probable offered
option, otherwise that level stays) and the context ``JEV_DAY_REVIEW_CONTEXT_V2`` with questions
``JEV_DAY_REVIEW_QUESTIONS_V2`` (``day_review_dossier``: V1 plus the trade's last 5 maintenance
reviews, named as history). The numbers, clock, agreement and discussion round are V1's.
Admission records V2 in the maintained arm from that package on (``ADMITTED_REVIEW``); a setup
that recorded V1 keeps V1's context, questions and reader. ``CryptoReviewPolicyV2`` is a
``CryptoReviewPolicy``, so every rule of the clock and the fail-safe below applies to both.

``CRYPTO_WINDOW_REVIEW_V1`` and ``CRYPTO_WINDOW_HOLD_V1`` (package review-window, 2026-09-28;
owner direction of 2026-09-28, the first window 4 hours). The trade window is a setting,
``MANAGED_CRYPTO_WINDOW_JSON`` (``CryptoWindowSetting``: exactly ``{"version":
"CRYPTO_WINDOW_REVIEW_V1", "window_minutes": N}``, N an integer from 60 to 1,440 in steps of 15).
While it is set, admission records the window versions instead of ``CRYPTO_24H_REVIEW_V2`` and
``CRYPTO_24H_HOLD_V1``, and the window in seconds as the state field ``holding_window_seconds``
beside ``holding_policy``; without it admission records exactly what it recorded before.
``CRYPTO_WINDOW_REVIEW_V1`` is ``CRYPTO_24H_REVIEW_V2`` (context, answer rule, request lead,
discussion round, Jev windows, grace, exit reasons) with the window as its
``review_interval_seconds`` and the questions ``JEV_DAY_REVIEW_QUESTIONS_V3`` (V2's texts with
the review's horizon in the window's words, ``day_review_dossier``): T is the first fill plus
the window, and a continue moves T and the fail-safe on by the window. ``CRYPTO_WINDOW_HOLD_V1``
is ``CRYPTO_24H_HOLD_V1`` with the window as ``max_hold_seconds``; its exit reason stays
``HOLD_24H_EXIT`` (the existing code, so no new stored code). Each record carries its own
window and a state's ``holding_window_seconds`` must equal it, so a trade keeps the window it
started with whatever the setting says later.

Scope: a crypto setup admitted from an ``AGENT_RESEARCH_REPORT_V3`` packet (the scope of
``SYSTEM_CHECK_V1``), whatever the selection rule. The policy is recorded in the setup's WATCHING
state at admission (``holding_policy``) and read back from that state only, so a setup keeps the
policy it was admitted under; every other setup keeps ``CRYPTO_NY_DAY_PAPER_V1`` (or no day
policy) exactly as before. Stops, targets, the daily halt and operator flatten close a trade at
any time under either policy.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from catalyst_lab.jev_contract import strict_json

HOLD_POLICY_ID = "CRYPTO_24H_HOLD_V1"
HOLD_SECONDS = 86400
HOLD_EXIT_REASON = "HOLD_24H_EXIT"

# CRYPTO_24H_REVIEW_V1 (package day-review): the version names the review uses.
REVIEW_POLICY_ID = "CRYPTO_24H_REVIEW_V1"
REVIEW_INTERVAL_SECONDS = 86400  # T is 24 hours after the first fill, then every 24 hours.
REQUEST_LEAD_SECONDS = 1800  # The agent is asked 30 minutes before T.
AGENT_REPLY_SECONDS = 900  # The discussion round: the agent answers once more within 15 min.
JEV_ANSWER_SECONDS = 1800  # Jev unable to answer within 30 minutes: exit.
REVIEW_WINDOW_SECONDS = JEV_ANSWER_SECONDS + AGENT_REPLY_SECONDS + JEV_ANSWER_SECONDS  # 4,500.
DEADLINE_GRACE_SECONDS = 300  # The fail-safe follows the review window by 5 minutes.
JEV_CALL_DEADLINE_SECONDS = 60  # Each Jev attempt's deadline (never past its window).
JEV_RETRY_SECONDS = 60  # A failed Jev attempt is retried after a minute, inside its window.
REVIEW_EXIT_REASON = "DAY_REVIEW_EXIT"
REVIEW_DEADLINE_EXIT_REASON = "DAY_REVIEW_DEADLINE_EXIT"
DAY_REVIEW_CONTEXT_VERSION = "JEV_DAY_REVIEW_CONTEXT_V1"
DAY_REVIEW_QUESTION_VERSION = "JEV_DAY_REVIEW_QUESTIONS_V1"
AGENT_REVIEW_ANSWER_VERSION = "AGENT_REVIEW_ANSWER_V1"
EARLY_EXIT_AGREEMENT_VERSION = "EARLY_EXIT_AGREEMENT_V1"
JEV_MANAGED_ARM = "JEV_MANAGED"  # account_risk.JEV_MANAGED_ARM (no import: no cycle).
# CRYPTO_24H_REVIEW_V2 (package answer-rules): DAY_REVIEW_ANSWER_RULE_V2, context and questions V2.
REVIEW_V2_POLICY_ID = "CRYPTO_24H_REVIEW_V2"
DAY_REVIEW_ANSWER_RULE_V2 = "DAY_REVIEW_ANSWER_RULE_V2"
DAY_REVIEW_CONTEXT_V2_VERSION = "JEV_DAY_REVIEW_CONTEXT_V2"
DAY_REVIEW_QUESTION_V2_VERSION = "JEV_DAY_REVIEW_QUESTIONS_V2"
# CRYPTO_WINDOW_REVIEW_V1 / CRYPTO_WINDOW_HOLD_V1 (package review-window): the window setting.
WINDOW_ENV = "MANAGED_CRYPTO_WINDOW_JSON"
WINDOW_REVIEW_POLICY_ID = "CRYPTO_WINDOW_REVIEW_V1"
WINDOW_HOLD_POLICY_ID = "CRYPTO_WINDOW_HOLD_V1"
DAY_REVIEW_QUESTION_V3_VERSION = "JEV_DAY_REVIEW_QUESTIONS_V3"
WINDOW_FIELD = "holding_window_seconds"  # The state field beside ``holding_policy``.
WINDOW_SETTING_KEYS = frozenset({"version", "window_minutes"})
WINDOW_MINUTES_MIN, WINDOW_MINUTES_MAX, WINDOW_MINUTES_STEP = 60, 1440, 15
REVIEW_POLICY_IDS = (REVIEW_POLICY_ID, REVIEW_V2_POLICY_ID, WINDOW_REVIEW_POLICY_ID)


def _aware(first_fill_at):
    if not isinstance(first_fill_at, datetime) or first_fill_at.tzinfo is None:
        raise ValueError("AWARE_CRYPTO_FILL_TIMESTAMP_REQUIRED")
    return first_fill_at


@dataclass(frozen=True)
class CryptoHoldPolicy:
    policy_id: str
    max_hold_seconds: int
    exit_reason: str

    def __post_init__(self):
        if (
            self.policy_id != HOLD_POLICY_ID
            or type(self.max_hold_seconds) is not int
            or self.max_hold_seconds != HOLD_SECONDS
            or self.exit_reason != HOLD_EXIT_REASON
        ):
            raise ValueError("EXPLICIT_CRYPTO_HOLD_POLICY_REQUIRED")

    def exit_at(self, first_fill_at):
        """The hard exit: ``max_hold_seconds`` after the first buy fill; no midnight flatten."""
        if not isinstance(first_fill_at, datetime) or first_fill_at.tzinfo is None:
            raise ValueError("AWARE_CRYPTO_FILL_TIMESTAMP_REQUIRED")
        return first_fill_at + timedelta(seconds=self.max_hold_seconds)

    def record(self):
        return asdict(self)


CRYPTO_24H_HOLD = CryptoHoldPolicy(HOLD_POLICY_ID, HOLD_SECONDS, HOLD_EXIT_REASON)


@dataclass(frozen=True)
class CryptoReviewPolicy:
    """The exact ``CRYPTO_24H_REVIEW_V1`` numbers; a state can never carry altered ones."""

    policy_id: str
    review_interval_seconds: int
    request_lead_seconds: int
    agent_reply_seconds: int
    jev_answer_seconds: int
    review_window_seconds: int
    deadline_grace_seconds: int
    jev_call_deadline_seconds: int
    jev_retry_seconds: int
    exit_reason: str
    deadline_exit_reason: str
    context_version: str
    question_version: str
    answer_version: str
    early_exit_version: str

    def __post_init__(self):
        if asdict(self) != _REVIEW_VALUES or any(
            type(getattr(self, name)) is not int for name in _REVIEW_INTEGERS
        ):
            raise ValueError("EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED")

    def review_at(self, first_fill_at, continuations=0):
        """T of the next review: one interval after the first fill, then every interval (24
        hours under the 24-hour versions, the window under ``CRYPTO_WINDOW_REVIEW_V1``)."""
        if type(continuations) is not int or continuations < 0:
            raise ValueError("CONTINUATION_COUNT_REQUIRED")
        return _aware(first_fill_at) + timedelta(
            seconds=self.review_interval_seconds * (continuations + 1))

    def exit_at(self, first_fill_at, continuations=0):
        """The fail-safe: T plus the review window plus the grace (see the module)."""
        return self.review_at(first_fill_at, continuations) + timedelta(
            seconds=self.review_window_seconds + self.deadline_grace_seconds)

    def record(self):
        return asdict(self)


_REVIEW_VALUES = {
    "policy_id": REVIEW_POLICY_ID,
    "review_interval_seconds": REVIEW_INTERVAL_SECONDS,
    "request_lead_seconds": REQUEST_LEAD_SECONDS,
    "agent_reply_seconds": AGENT_REPLY_SECONDS,
    "jev_answer_seconds": JEV_ANSWER_SECONDS,
    "review_window_seconds": REVIEW_WINDOW_SECONDS,
    "deadline_grace_seconds": DEADLINE_GRACE_SECONDS,
    "jev_call_deadline_seconds": JEV_CALL_DEADLINE_SECONDS,
    "jev_retry_seconds": JEV_RETRY_SECONDS,
    "exit_reason": REVIEW_EXIT_REASON,
    "deadline_exit_reason": REVIEW_DEADLINE_EXIT_REASON,
    "context_version": DAY_REVIEW_CONTEXT_VERSION,
    "question_version": DAY_REVIEW_QUESTION_VERSION,
    "answer_version": AGENT_REVIEW_ANSWER_VERSION,
    "early_exit_version": EARLY_EXIT_AGREEMENT_VERSION,
}
_REVIEW_INTEGERS = tuple(k for k, v in _REVIEW_VALUES.items() if isinstance(v, int))
CRYPTO_24H_REVIEW = CryptoReviewPolicy(**_REVIEW_VALUES)


@dataclass(frozen=True)
class CryptoReviewPolicyV2(CryptoReviewPolicy):
    """The exact ``CRYPTO_24H_REVIEW_V2`` record: V1's numbers, its own ``policy_id``, context
    and questions V2 and the answer rule ``DAY_REVIEW_ANSWER_RULE_V2``."""

    answer_rule: str

    def __post_init__(self):
        if asdict(self) != _REVIEW_V2_VALUES or any(
            type(getattr(self, name)) is not int for name in _REVIEW_INTEGERS
        ):
            raise ValueError("EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED")


_REVIEW_V2_VALUES = {**_REVIEW_VALUES, "policy_id": REVIEW_V2_POLICY_ID,
                     "context_version": DAY_REVIEW_CONTEXT_V2_VERSION,
                     "question_version": DAY_REVIEW_QUESTION_V2_VERSION,
                     "answer_rule": DAY_REVIEW_ANSWER_RULE_V2}
CRYPTO_24H_REVIEW_V2 = CryptoReviewPolicyV2(**_REVIEW_V2_VALUES)
# The review version admission records in the maintained arm (package answer-rules: V2).
ADMITTED_REVIEW = CRYPTO_24H_REVIEW_V2


# --- CRYPTO_WINDOW_REVIEW_V1 and CRYPTO_WINDOW_HOLD_V1 (package review-window) ------------------


def window_seconds_allowed(seconds):
    """Whether ``seconds`` is an allowed window: an integer 60 to 1,440 minutes in 15-minute
    steps."""
    return (type(seconds) is int
            and WINDOW_MINUTES_MIN * 60 <= seconds <= WINDOW_MINUTES_MAX * 60
            and seconds % (WINDOW_MINUTES_STEP * 60) == 0)


@dataclass(frozen=True)
class CryptoWindowReviewPolicy(CryptoReviewPolicyV2):
    """The exact ``CRYPTO_WINDOW_REVIEW_V1`` record: ``CRYPTO_24H_REVIEW_V2``'s with its own
    ``policy_id``, the questions ``JEV_DAY_REVIEW_QUESTIONS_V3`` and an allowed window as
    ``review_interval_seconds``. A ``CryptoReviewPolicyV2``, so V2's answer rule reads it."""

    def __post_init__(self):
        if not window_seconds_allowed(self.review_interval_seconds) or asdict(self) != {
            **_WINDOW_REVIEW_VALUES, "review_interval_seconds": self.review_interval_seconds,
        } or any(type(getattr(self, name)) is not int for name in _REVIEW_INTEGERS):
            raise ValueError("EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED")

    @property
    def window_seconds(self):
        return self.review_interval_seconds


_WINDOW_REVIEW_VALUES = {**_REVIEW_V2_VALUES, "policy_id": WINDOW_REVIEW_POLICY_ID,
                         "question_version": DAY_REVIEW_QUESTION_V3_VERSION}


@dataclass(frozen=True)
class CryptoWindowHoldPolicy(CryptoHoldPolicy):
    """The exact ``CRYPTO_WINDOW_HOLD_V1`` record: ``CRYPTO_24H_HOLD_V1``'s with its own
    ``policy_id`` and an allowed window as ``max_hold_seconds``; the exit reason stays
    ``HOLD_24H_EXIT``."""

    def __post_init__(self):
        if (self.policy_id != WINDOW_HOLD_POLICY_ID
                or not window_seconds_allowed(self.max_hold_seconds)
                or self.exit_reason != HOLD_EXIT_REASON):
            raise ValueError("EXPLICIT_CRYPTO_HOLD_POLICY_REQUIRED")

    @property
    def window_seconds(self):
        return self.max_hold_seconds


@dataclass(frozen=True)
class CryptoWindowSetting:
    """``MANAGED_CRYPTO_WINDOW_JSON``: the window versions admission records while it is set.
    ``version`` names the maintained arm's review (``CRYPTO_WINDOW_REVIEW_V1``; its control arm
    is ``CRYPTO_WINDOW_HOLD_V1``); ``window_minutes`` is their window."""

    version: str
    window_minutes: int

    def __post_init__(self):
        if self.version != WINDOW_REVIEW_POLICY_ID:
            raise ValueError("CRYPTO_WINDOW_VERSION_UNKNOWN")
        if type(self.window_minutes) is not int or not window_seconds_allowed(
                self.window_minutes * 60):
            raise ValueError("CRYPTO_WINDOW_MINUTES_INVALID")

    @property
    def seconds(self):
        return self.window_minutes * 60

    def review_policy(self):
        return CryptoWindowReviewPolicy(**{**_WINDOW_REVIEW_VALUES,
                                           "review_interval_seconds": self.seconds})

    def hold_policy(self):
        return CryptoWindowHoldPolicy(WINDOW_HOLD_POLICY_ID, self.seconds, HOLD_EXIT_REASON)

    def record(self):
        return asdict(self)


def parse_window_setting(raw):
    """``MANAGED_CRYPTO_WINDOW_JSON`` exactly: a JSON object with exactly ``version`` and
    ``window_minutes``. Anything else raises ``ValueError`` with its code:
    ``CRYPTO_WINDOW_SETTING_INVALID`` (not one JSON object, a duplicate key),
    ``CRYPTO_WINDOW_SETTING_KEYS``, ``CRYPTO_WINDOW_VERSION_UNKNOWN`` or
    ``CRYPTO_WINDOW_MINUTES_INVALID``."""
    try:
        value = strict_json(raw)
    except (TypeError, ValueError):
        raise ValueError("CRYPTO_WINDOW_SETTING_INVALID") from None
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_WINDOW_SETTING_INVALID")
    if set(value) != WINDOW_SETTING_KEYS:
        raise ValueError("CRYPTO_WINDOW_SETTING_KEYS")
    return CryptoWindowSetting(value["version"], value["window_minutes"])


def window_setting_from_env(environ):
    """The window setting, or None when ``MANAGED_CRYPTO_WINDOW_JSON`` is absent (admission then
    records the 24-hour versions exactly as before). A present value is parsed strictly; an
    empty one is refused."""
    if WINDOW_ENV not in environ:
        return None
    return parse_window_setting(environ[WINDOW_ENV])


def admission_policy(v3_crypto, arm, window=None):
    """The holding policy admission records: none outside report-V3 crypto; the review
    (``ADMITTED_REVIEW``, V2 from package answer-rules) in the maintained (``JEV_MANAGED``) arm;
    ``CRYPTO_24H_HOLD_V1`` in the control arm. With ``window`` (a ``CryptoWindowSetting``):
    ``CRYPTO_WINDOW_REVIEW_V1`` and ``CRYPTO_WINDOW_HOLD_V1`` with its window."""
    if not v3_crypto:
        return None
    if window is not None:
        return window.review_policy() if arm == JEV_MANAGED_ARM else window.hold_policy()
    return ADMITTED_REVIEW if arm == JEV_MANAGED_ARM else CRYPTO_24H_HOLD


def review_policy_from_record(record):
    """``CRYPTO_24H_REVIEW_V1``, ``_V2`` or ``CRYPTO_WINDOW_REVIEW_V1`` from its exact record (a
    state's ``holding_policy`` or a review request context's ``policy``); anything else is
    refused."""
    if not isinstance(record, dict):
        raise ValueError("EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED")
    if record.get("policy_id") == WINDOW_REVIEW_POLICY_ID:
        return CryptoWindowReviewPolicy(**record)
    if record.get("policy_id") == REVIEW_V2_POLICY_ID:
        return CryptoReviewPolicyV2(**record)
    return CryptoReviewPolicy(**record)


def recorded_hold_policy(state):
    """The holding policy recorded at admission, or None for a setup admitted without one. A
    window version's record and the state's ``holding_window_seconds`` must agree, and no other
    policy's state may carry that field."""
    recorded = (state or {}).get("holding_policy")
    if not recorded:
        return None
    if recorded.get("policy_id") in REVIEW_POLICY_IDS:
        hold = review_policy_from_record(recorded)
    elif recorded.get("policy_id") == WINDOW_HOLD_POLICY_ID:
        hold = CryptoWindowHoldPolicy(**recorded)
    else:
        hold = CryptoHoldPolicy(**recorded)
    window, expected = state.get(WINDOW_FIELD), getattr(hold, "window_seconds", None)
    if (window is not None or expected is not None) and (
            type(window) is not int or window != expected):
        raise ValueError("EXPLICIT_CRYPTO_WINDOW_REQUIRED")
    return hold


def window_fields(hold):
    """The state field admission records beside a window version's ``holding_policy``: its
    window in seconds. None for any other policy, so their states stay exactly as before."""
    window = getattr(hold, "window_seconds", None)
    return {WINDOW_FIELD: window} if window is not None else {}


def recorded_window_seconds(state):
    """The window a setup recorded at admission (seconds), or None when it recorded no window
    version (the 24-hour versions, older setups)."""
    return getattr(recorded_hold_policy(state), "window_seconds", None)


def review_active(state):
    """Whether a setup was admitted under a review version (``CRYPTO_24H_REVIEW_V1``, ``_V2`` or
    ``CRYPTO_WINDOW_REVIEW_V1``)."""
    return isinstance(recorded_hold_policy(state), CryptoReviewPolicy)


def continuations(state):
    value = (state or {}).get("continuations") or 0
    if type(value) is not int or value < 0:
        raise ValueError("CONTINUATION_COUNT_REQUIRED")
    return value


def hard_exit_deadline(hold, first_fill_at, state):
    """``hard_exit_at`` for the first fill: the hold's own time under a hold version (24 hours,
    or its window), the fail-safe of the current review under a review version (its
    continuation count comes from the state)."""
    if isinstance(hold, CryptoReviewPolicy):
        return hold.exit_at(first_fill_at, continuations(state))
    return hold.exit_at(first_fill_at)


def review_fields(hold, first_fill_at, state):
    """State fields the review policy keeps beside ``hard_exit_at`` (none under the hold, so
    its states stay exactly as before): T and the continuation count."""
    if not isinstance(hold, CryptoReviewPolicy):
        return {}
    count = continuations(state)
    return {"day_review_at": hold.review_at(first_fill_at, count).isoformat(),
            "continuations": count}


def time_exit_reason(state):
    """The exit reason once a setup's ``hard_exit_at`` has passed."""
    hold = recorded_hold_policy(state)
    if isinstance(hold, CryptoReviewPolicy):
        # The fail-safe never renames an exit already requested (a review exit, a stop...).
        return (state or {}).get("exit_requested") or hold.deadline_exit_reason
    return hold.exit_reason if hold is not None else "TIME_EXIT"
