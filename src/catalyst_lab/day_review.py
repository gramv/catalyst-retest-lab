"""The 24-hour review and early exits: versions, agent answers, Jev answers and decisions.

Plan ``docs/CRYPTO-AGENT-LOOP.md`` 4.6.3 (early exit, both sides) and 4.6.4 (the 24-hour
review), owner-approved 2026-09-26; plan phase 6, package day-review (2026-09-27). Named
versions under the owner's 2026-09-24 ruling; every earlier version keeps its definition.

* ``CRYPTO_24H_REVIEW_V1`` (``crypto_holding``): the holding policy of a report-V3 crypto trade
  in the maintained (``JEV_MANAGED``) arm, recorded at admission. The numbers live there.
* ``AGENT_REVIEW_ANSWER_V1``: the agent's answer to a 24-hour review (both rounds) and to a Jev
  early-exit flag: CONTINUE or EXIT, what changed, what it expects in the next 24 hours, what
  would prove it wrong, an optional suggested stop and target (review only) and 0-8 sources.
* ``AGENT_EXIT_FLAG_V1``: the agent's own early-exit flag (the same reasons and sources).
* ``EARLY_EXIT_AGREEMENT_V1``: either side flags; the other side is asked at once and has 15
  minutes; both say exit, the trade sells at market (``EARLY_EXIT_AGREED``); otherwise, or with
  no answer in time, it stays with its stop and target.
* ``JEV_DAY_REVIEW_QUESTIONS_V1`` / ``JEV_EARLY_EXIT_QUESTIONS_V1`` (``day_review_dossier``):
  what Jev answers, read here by code.
* ``DAY_REVIEW_ANSWER_RULE_V2`` (package answer-rules, 2026-09-27; the answer rule of
  ``CRYPTO_24H_REVIEW_V2``): ``decision`` decides; an option answer raises a level only when it
  is the unique most probable offered option; ``read_review_answer_v2``. V1's reader
  (``read_review_answer``) is unchanged and reads every review of a setup that recorded
  ``CRYPTO_24H_REVIEW_V1``; the early-exit reader is the same under both.

This module is pure: no clock, database, broker or provider access. ``trade_review`` applies it.
"""

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid5

from catalyst_lab.crypto_holding import (
    AGENT_REVIEW_ANSWER_VERSION,
    DAY_REVIEW_ANSWER_RULE_V2,
    EARLY_EXIT_AGREEMENT_VERSION,
    JEV_CALL_DEADLINE_SECONDS,
    JEV_RETRY_SECONDS,
    CryptoReviewPolicyV2,
    review_policy_from_record,
)
from catalyst_lab.crypto_maintenance import EXIT_FLAG_VERSION, KEEP
from catalyst_lab.exit_flags import ANSWER_WINDOW_SECONDS, EARLY_EXIT_REASON
from catalyst_lab.jev_contract import INSUFFICIENT, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.maintenance_dossier import (
    INSUFFICIENT_EVIDENCE,
    TIED,
    answer_choice,
    answer_summary,
    brief,
    top_answer,
    usable_option,
)

AGENT_EXIT_FLAG_VERSION = "AGENT_EXIT_FLAG_V1"
EARLY_EXIT_CONTEXT_VERSION = "JEV_EARLY_EXIT_CONTEXT_V1"
EARLY_EXIT_QUESTION_VERSION = "JEV_EARLY_EXIT_QUESTIONS_V1"

CONTINUE, EXIT, STAY = "CONTINUE", "EXIT", "STAY"
DECISIONS = (CONTINUE, EXIT)
# Rounds: the agent answers FIRST and, in a discussion, DISCUSSION; Jev answers FIRST and FINAL.
FIRST, DISCUSSION, FINAL = "FIRST", "DISCUSSION", "FINAL"
INTACT, WEAKENED, BROKEN = "INTACT", "WEAKENED", "BROKEN"
HOLDS, DOES_NOT_HOLD = "HOLDS", "DOES_NOT_HOLD"

# Ledger events (lab.managed_events, append-only, each with the trade's setup_id).
REQUESTED = "DAY_REVIEW_REQUESTED"
AGENT_ANSWER = "DAY_REVIEW_AGENT_ANSWER"
JEV_REQUEST = "DAY_REVIEW_JEV_REQUEST"
JEV_RESULT = "DAY_REVIEW_JEV_RESULT"
DISCUSSION_OPENED = "DAY_REVIEW_DISCUSSION"
DECISION = "DAY_REVIEW_DECISION"
FLAG_RAISED = "EXIT_FLAG_RAISED"  # exit_flags (EARLY_EXIT_FLAG_V1).
FLAG_RESOLVED = "EXIT_FLAG_RESOLVED"  # exit_flags.
FLAG_ASKED = "EXIT_FLAG_ASKED"
FLAG_AGENT_ANSWER = "EXIT_FLAG_AGENT_ANSWER"
FLAG_JEV_REQUEST = "EXIT_FLAG_JEV_REQUEST"
FLAG_JEV_RESULT = "EXIT_FLAG_JEV_RESULT"
EARLY_EXIT_DECISION = "EARLY_EXIT_DECISION"
REVIEW_KINDS = (REQUESTED, AGENT_ANSWER, JEV_REQUEST, JEV_RESULT, DISCUSSION_OPENED, DECISION)
FLAG_KINDS = (FLAG_RAISED, FLAG_ASKED, FLAG_AGENT_ANSWER, FLAG_JEV_REQUEST, FLAG_JEV_RESULT,
              FLAG_RESOLVED, EARLY_EXIT_DECISION)

# Jev results.
ANSWERED, UNUSABLE, FAILED = "ANSWERED", "UNUSABLE", "FAILED"

# 24-hour review outcomes and codes.
DISCARDED = "DISCARDED"
AGREED = "AGREED"
AGREED_AFTER_DISCUSSION = "AGREED_AFTER_DISCUSSION"
DISAGREED_AFTER_DISCUSSION = "DISAGREED_AFTER_DISCUSSION"
AGENT_SILENT_JEV_ALONE = "AGENT_SILENT_JEV_ALONE"
NO_AGENT_JEV_ALONE = "NO_AGENT_JEV_ALONE"
DISCUSSION_NO_AGENT_REPLY = "DISCUSSION_NO_AGENT_REPLY"
JEV_UNAVAILABLE = "JEV_UNAVAILABLE"
JEV_ANSWER_UNUSABLE = "JEV_ANSWER_UNUSABLE"
JEV_REVIEWS_DISABLED = "JEV_REVIEWS_DISABLED"
REVIEW_WINDOW_EXCEEDED = "REVIEW_WINDOW_EXCEEDED"
POSITION_CLOSED_DURING_REVIEW = "POSITION_CLOSED_DURING_REVIEW"
EXIT_IN_PROGRESS_DURING_REVIEW = "EXIT_IN_PROGRESS_DURING_REVIEW"

# Early-exit resolutions (exit_flags.OUTCOMES) and codes.
EXIT_AGREED, EXIT_NOT_AGREED = "EXIT_AGREED", "EXIT_NOT_AGREED"
NO_ANSWER_IN_TIME, LIFECYCLE_ENDED = "NO_ANSWER_IN_TIME", "LIFECYCLE_ENDED"
BOTH_SIDES_FLAGGED = "BOTH_SIDES_FLAGGED"
NO_ACTIVE_AGENT = "NO_ACTIVE_AGENT"

# Who answers for the trade (plan 4.6.7): the proposing agent, else the active research agent.
PROPOSING_AGENT, ACTIVE_RESEARCH_AGENT = "PROPOSING_AGENT", "ACTIVE_RESEARCH_AGENT"

# The measurement hook (plan 4.6.8): one record per decision, mapped directly to the results
# package's LevelChange (change kinds as named there).
CONTINUE_EXIT_DECISION, EARLY_EXIT = "CONTINUE_EXIT_DECISION", "EARLY_EXIT"

TEXT_LIMITS = {"what_changed": 600, "next_24h": 600, "proves_wrong": 400}
MAX_SOURCES = 8
MAX_PRICE_CHARS = 40
_SENSITIVE = re.compile(r"(?i)\b(?:authorization\s*:|bearer\s+|APCA_API_|TYPESAFE_API_KEY)")


@dataclass(frozen=True)
class EarlyExitAgreementPolicy:
    """The exact ``EARLY_EXIT_AGREEMENT_V1`` numbers and names."""

    policy_id: str
    answer_window_seconds: int
    flag_version: str
    agent_flag_version: str
    answer_version: str
    context_version: str
    question_version: str
    exit_reason: str
    jev_call_deadline_seconds: int
    jev_retry_seconds: int

    def __post_init__(self):
        if asdict(self) != _EARLY_EXIT_VALUES or any(
            type(getattr(self, k)) is not int
            for k in ("answer_window_seconds", "jev_call_deadline_seconds", "jev_retry_seconds")
        ):
            raise ValueError("EXPLICIT_EARLY_EXIT_POLICY_REQUIRED")

    def record(self):
        return asdict(self)


_EARLY_EXIT_VALUES = {
    "policy_id": EARLY_EXIT_AGREEMENT_VERSION,
    "answer_window_seconds": ANSWER_WINDOW_SECONDS,  # 15 minutes, EARLY_EXIT_FLAG_V1's.
    "flag_version": EXIT_FLAG_VERSION,
    "agent_flag_version": AGENT_EXIT_FLAG_VERSION,
    "answer_version": AGENT_REVIEW_ANSWER_VERSION,
    "context_version": EARLY_EXIT_CONTEXT_VERSION,
    "question_version": EARLY_EXIT_QUESTION_VERSION,
    "exit_reason": EARLY_EXIT_REASON,
    "jev_call_deadline_seconds": JEV_CALL_DEADLINE_SECONDS,
    "jev_retry_seconds": JEV_RETRY_SECONDS,
}
EARLY_EXIT_AGREEMENT = EarlyExitAgreementPolicy(**_EARLY_EXIT_VALUES)


def review_id_for(setup_id, lifecycle_id, number):
    """The review of one trade lifecycle's ``number``-th 24-hour period (1 at the first T)."""
    if type(number) is not int or number < 1:
        raise ValueError("REVIEW_NUMBER_REQUIRED")
    return str(uuid5(NAMESPACE_URL, f"day-review:{setup_id}:{lifecycle_id}:{number}"))


# --- Agent answers (AGENT_REVIEW_ANSWER_V1) and flags (AGENT_EXIT_FLAG_V1) ------------------------


def _uuid(value, code):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(code) from None


def _text(raw, key):
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > TEXT_LIMITS[key]:
        raise ValueError("ANSWER_TEXT_INVALID")
    return value


def _price(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_PRICE_CHARS:
        raise ValueError("SUGGESTED_LEVEL_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("SUGGESTED_LEVEL_INVALID") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("SUGGESTED_LEVEL_INVALID")
    return format(number, "f")


def _sources(raw, now):
    from catalyst_lab.position_news import _sources as position_sources

    value = raw.get("sources", [])
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        raise ValueError("SOURCE_COUNT_INVALID")
    return position_sources(value, now) if value else []


def identity_leaks(canonical, agent_id):
    """Agent-written fields naming the agent (whole word, any case): the three texts and each
    source's ID. Jev never sees the agent's identity. Source excerpts and URLs are third-party
    text and are not checked (as report V3's rule)."""
    if not agent_id:
        return []
    word = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(agent_id) + r"(?![A-Za-z0-9_])",
                      re.IGNORECASE)
    leaks = [key for key in TEXT_LIMITS if word.search(canonical[key])]
    leaks += [f"sources[{i}].source_id" for i, source in enumerate(canonical["sources"])
              if word.search(source["source_id"])]
    return leaks


def _checked(canonical, agent_id):
    if identity_leaks(canonical, agent_id):
        raise ValueError("AGENT_IDENTITY_IN_ANSWER")
    serialized = encoded(canonical)
    _privacy_check(serialized)
    if _SENSITIVE.search(serialized):
        raise ValueError("SENSITIVE_EVIDENCE_REJECTED")
    return canonical


def validate_review_answer(raw, *, agent_id, now, suggestions_allowed):
    """``AGENT_REVIEW_ANSWER_V1`` canonical form, or ValueError with a code.

    ``suggestions_allowed``: a 24-hour review answer may suggest a stop and a target with
    CONTINUE; an answer to an early-exit flag may not.
    """
    allowed = {"schema_version", "answer_id", "decision", *TEXT_LIMITS, "suggested_stop",
               "suggested_target", "sources"}
    if not isinstance(raw, dict) or set(raw) - allowed or not {
        "schema_version", "answer_id", "decision", *TEXT_LIMITS
    } <= set(raw):
        raise ValueError("REVIEW_ANSWER_FIELDS_INVALID")
    if raw["schema_version"] != AGENT_REVIEW_ANSWER_VERSION:
        raise ValueError("REVIEW_ANSWER_SCHEMA_REQUIRED")
    if raw["decision"] not in DECISIONS:
        raise ValueError("REVIEW_DECISION_INVALID")
    stop, target = _price(raw.get("suggested_stop")), _price(raw.get("suggested_target"))
    if (stop is not None or target is not None) and (
        not suggestions_allowed or raw["decision"] != CONTINUE
    ):
        raise ValueError("SUGGESTED_LEVELS_NOT_ALLOWED")
    canonical = {
        "schema_version": AGENT_REVIEW_ANSWER_VERSION,
        "answer_id": _uuid(raw["answer_id"], "ANSWER_ID_INVALID"),
        "decision": raw["decision"],
        **{key: _text(raw, key) for key in TEXT_LIMITS},
        "suggested_stop": stop,
        "suggested_target": target,
        "sources": _sources(raw, now),
    }
    return _checked(canonical, agent_id)


def validate_exit_flag(raw, *, agent_id, now):
    """``AGENT_EXIT_FLAG_V1`` canonical form, or ValueError with a code."""
    required = {"schema_version", "flag_ref", "lifecycle_id", *TEXT_LIMITS}
    if not isinstance(raw, dict) or set(raw) - required - {"sources"} or not required <= set(
            raw):
        raise ValueError("EXIT_FLAG_FIELDS_INVALID")
    if raw["schema_version"] != AGENT_EXIT_FLAG_VERSION:
        raise ValueError("EXIT_FLAG_SCHEMA_REQUIRED")
    canonical = {
        "schema_version": AGENT_EXIT_FLAG_VERSION,
        "flag_ref": _uuid(raw["flag_ref"], "FLAG_REF_INVALID"),
        "lifecycle_id": _uuid(raw["lifecycle_id"], "POSITION_LIFECYCLE_INVALID"),
        **{key: _text(raw, key) for key in TEXT_LIMITS},
        "sources": _sources(raw, now),
    }
    return _checked(canonical, agent_id)


# --- Jev's answers, read by code -----------------------------------------------------------------


@dataclass(frozen=True)
class JevAnswer:
    """Jev's answer to a 24-hour review or an early-exit flag, and whether code may use it.

    ``decision`` is CONTINUE or EXIT (review) or EXIT or STAY (flag); ``code`` is None for a
    usable answer, else why it is not (the trade then exits at a review, stays at a flag).
    """

    decision: str | None
    trade_reason: str | None
    agent_case: str | None
    stop_option: str | None
    target_option: str | None
    code: str | None
    summary: dict

    @property
    def usable(self):
        return self.code is None

    def record(self):
        return asdict(self)


def _summary(answers):
    return {name: {"choice": value.get("choice"),
                   "top_p": brief(Decimal(str(max(value["probabilities"].values()))), 2)}
            for name, value in sorted(answers.items())}


def _uncertain(answers):
    return any(value.get("choice") == INSUFFICIENT or sum(
        p == max(value["probabilities"].values()) for p in value["probabilities"].values()
    ) != 1 for value in answers.values())


def read_review_answer(answers, options, *, agent_asked):
    """``JEV_DAY_REVIEW_QUESTIONS_V1``: uncertainty or a contradiction makes the answer
    unusable (the trade then exits). EXIT needs both options KEEP; CONTINUE needs a reason
    that is not BROKEN; a chosen option must be one the context offered."""
    expected = {"trade_reason", "decision", "stop_option", "target_option"}
    if agent_asked:
        expected.add("agent_case")
    choice = {name: (answers.get(name) or {}).get("choice") for name in sorted(expected)}

    def result(code=None):
        return JevAnswer(choice["decision"], choice["trade_reason"], choice.get("agent_case"),
                         choice["stop_option"], choice["target_option"], code,
                         _summary(answers) if isinstance(answers, dict) else {})

    if not isinstance(answers, dict) or set(answers) != expected:
        return result("INVALID_REVIEW_ANSWER")
    if _uncertain(answers):
        return result("UNCERTAIN_JUDGMENT")
    if choice["decision"] not in DECISIONS:
        return result("INVALID_REVIEW_ANSWER")
    if choice["decision"] == EXIT and (choice["stop_option"], choice["target_option"]) != (
            KEEP, KEEP):
        return result("CONTRADICTORY_REVIEW_ANSWERS")
    if choice["decision"] == CONTINUE and choice["trade_reason"] == BROKEN:
        return result("CONTRADICTORY_REVIEW_ANSWERS")
    for kind in ("stop", "target"):
        chosen = choice[kind + "_option"]
        if chosen != KEEP and chosen not in {o["option_id"] for o in options[kind]}:
            return result("UNKNOWN_OPTION")
    return result()


def read_flag_answer(answers):
    """``JEV_EARLY_EXIT_QUESTIONS_V1``: EXIT or STAY; STAY with a BROKEN reason contradicts."""
    expected = {"trade_reason", "agent_case", "exit_now"}
    choice = {name: (answers.get(name) or {}).get("choice") for name in sorted(expected)}

    def result(code=None):
        return JevAnswer(choice["exit_now"], choice["trade_reason"], choice["agent_case"],
                         None, None, code,
                         _summary(answers) if isinstance(answers, dict) else {})

    if not isinstance(answers, dict) or set(answers) != expected:
        return result("INVALID_EARLY_EXIT_ANSWER")
    if _uncertain(answers):
        return result("UNCERTAIN_JUDGMENT")
    if choice["exit_now"] not in {EXIT, STAY}:
        return result("INVALID_EARLY_EXIT_ANSWER")
    if choice["exit_now"] == STAY and choice["trade_reason"] == BROKEN:
        return result("CONTRADICTORY_EARLY_EXIT_ANSWERS")
    return result()


# --- DAY_REVIEW_ANSWER_RULE_V2 (CRYPTO_24H_REVIEW_V2, package answer-rules) ----------------------
#
# The review's questions are asked together over the same state, so none can see another's
# answer; ``stop_option``/``target_option`` are the levels "if the trade continues". Under V2
# ``decision`` decides and code consumes an option answer only on a continue, only when it is
# the unique most probable offered option (the pinned TypeSafe guidance, "Compose and verify":
# code consumes the applicable answers and ignores uncertainty on unused branches). One V1 rule
# stays: CONTINUE while the reason is uniquely BROKEN is a contradiction (unusable: exit).

NOT_RAISED_BY_DECISION = "NOT_RAISED_BY_DECISION"
OPTION_NOT_USABLE = {"stop": "STOP_OPTION_NOT_USABLE", "target": "TARGET_OPTION_NOT_USABLE"}


@dataclass(frozen=True)
class JevReviewAnswerV2:
    """A 24-hour review answer read by ``DAY_REVIEW_ANSWER_RULE_V2``.

    V1's fields (the answers as given, ``code`` None when usable, ``summary``) plus
    ``answer_rule``, ``raise_stop_to``/``raise_target_to`` (the option IDs a continue raises the
    levels to; None: that level stays) and ``option_use`` (per level: the answer as given, the
    option used and why not, with ``code`` ``STOP_OPTION_NOT_USABLE``/``TARGET_OPTION_NOT_USABLE``
    for a tied, Insufficient-evidence or unknown option answer on a continue)."""

    decision: str | None
    trade_reason: str | None
    agent_case: str | None
    stop_option: str | None
    target_option: str | None
    code: str | None
    summary: dict
    answer_rule: str
    raise_stop_to: str | None
    raise_target_to: str | None
    option_use: dict

    @property
    def usable(self):
        return self.code is None

    def record(self):
        return asdict(self)


def read_review_answer_v2(answers, options, *, agent_asked):
    """``DAY_REVIEW_ANSWER_RULE_V2`` for ``JEV_DAY_REVIEW_QUESTIONS_V1``:

    * ``decision`` Insufficient evidence or tied: ``UNCERTAIN_JUDGMENT`` (unusable: exit, as
      V1); another question set: ``INVALID_REVIEW_ANSWER``.
    * EXIT: usable; the option answers are recorded and never used (no contradiction).
    * CONTINUE with ``trade_reason`` BROKEN as its unique most probable answer:
      ``CONTRADICTORY_REVIEW_ANSWERS`` (V1's rule, kept). Otherwise usable: each level rises to
      its option answer only when that is the unique most probable answer and an offered
      option; KEEP keeps it; a tie, Insufficient evidence or an unknown ID keeps it too
      (``option_use`` records why) and never makes the answer unusable.
    * ``trade_reason`` (except that one case) and ``agent_case`` are informational.
    """
    expected = {"trade_reason", "decision", "stop_option", "target_option"}
    if agent_asked:
        expected.add("agent_case")
    valid = isinstance(answers, dict) and set(answers) == expected
    choice = {name: answer_choice(answers, name) for name in sorted(expected)}

    def result(code=None, raise_to=None, use=None):
        raise_to = raise_to or {}
        return JevReviewAnswerV2(
            choice["decision"], choice["trade_reason"], choice.get("agent_case"),
            choice["stop_option"], choice["target_option"], code,
            answer_summary(answers) if isinstance(answers, dict) else {},
            DAY_REVIEW_ANSWER_RULE_V2, raise_to.get("stop"), raise_to.get("target"), use or {})

    if not valid:
        return result("INVALID_REVIEW_ANSWER")
    decision, why = top_answer(answers["decision"])
    if why in {TIED, INSUFFICIENT_EVIDENCE}:
        return result("UNCERTAIN_JUDGMENT")
    if why is not None or decision not in DECISIONS:
        return result("INVALID_REVIEW_ANSWER")
    if decision == EXIT:
        return result(None, None, {
            kind: {"choice": choice[kind + "_option"], "raise_to": None, "code": None,
                   "reason": NOT_RAISED_BY_DECISION} for kind in ("stop", "target")})
    reason, reason_why = top_answer(answers["trade_reason"])
    if reason == BROKEN and reason_why is None:
        return result("CONTRADICTORY_REVIEW_ANSWERS")
    raise_to, use = {}, {}
    for kind in ("stop", "target"):
        chosen, why = usable_option(answers[kind + "_option"],
                                    {o["option_id"] for o in options[kind]})
        raise_to[kind] = chosen
        use[kind] = {"choice": choice[kind + "_option"], "raise_to": chosen,
                     "code": OPTION_NOT_USABLE[kind] if why not in {None, KEEP} else None,
                     "reason": why}
    return result(None, raise_to, use)


def review_reader(policy):
    """The reader of a 24-hour review: ``read_review_answer`` for a request whose context
    records ``CRYPTO_24H_REVIEW_V1``, ``read_review_answer_v2`` for ``CRYPTO_24H_REVIEW_V2``.
    ``policy`` is the context's exact policy record; anything else raises."""
    if isinstance(review_policy_from_record(policy), CryptoReviewPolicyV2):
        return read_review_answer_v2
    return read_review_answer


def option_to_raise(answer, kind):
    """The option ID a review answer raises ``kind`` to: V1's option answer as given (V1's
    ``chosen`` record, unchanged), V2's usable option (None: the level stays)."""
    if isinstance(answer, JevReviewAnswerV2):
        return answer.raise_stop_to if kind == "stop" else answer.raise_target_to
    return getattr(answer, kind + "_option")


def answer_from_record(record):
    """A recorded ``JevAnswer.record()`` (or ``JevReviewAnswerV2.record()``, recognised by its
    ``answer_rule``) back as the answer it records."""
    if record.get("answer_rule") == DAY_REVIEW_ANSWER_RULE_V2:
        return JevReviewAnswerV2(**record)
    return JevAnswer(**record)


# --- Decisions -----------------------------------------------------------------------------------


def first_round(jev, agent_decision, *, addressee):
    """``(outcome, code)`` after Jev's first answer, or None when a discussion round follows.

    Jev unusable: exit. No agent answer: Jev decides alone. Agreement: done.
    """
    if not jev.usable:
        return EXIT, JEV_ANSWER_UNUSABLE
    if agent_decision is None:
        return jev.decision, AGENT_SILENT_JEV_ALONE if addressee else NO_AGENT_JEV_ALONE
    if agent_decision == jev.decision:
        return jev.decision, AGREED
    return None


def final_round(jev, agent_decision):
    """``(outcome, code)`` after Jev's final answer: agreement, else exit (a disputed trade does
    not get another day)."""
    if not jev.usable:
        return EXIT, JEV_ANSWER_UNUSABLE
    if agent_decision == jev.decision:
        return jev.decision, AGREED_AFTER_DISCUSSION
    return EXIT, DISAGREED_AFTER_DISCUSSION


def flag_resolution(other_side_exit):
    """Both sides say exit: EXIT_AGREED; the other side says stay/continue: EXIT_NOT_AGREED."""
    return EXIT_AGREED if other_side_exit else EXIT_NOT_AGREED


def measurement(*, change_kind, at, levels_before, levels_after, exited, quote, **extra):
    """The unchanged-plan hook of one decision (plan 4.6.8), shaped as the results package's
    ``LevelChange``: the time, the levels in force (old) and the new ones (null unless raised),
    whether the trade exited, and the bid/ask/last at the decision."""
    old_stop, old_target = levels_before["stop"], levels_before["target"]
    new_stop = levels_after["stop"] if levels_after["stop"] != old_stop else None
    new_target = levels_after["target"] if levels_after["target"] != old_target else None
    return {
        "change_kind": change_kind, "at": at, "old_stop": old_stop, "old_target": old_target,
        "new_stop": new_stop, "new_target": new_target, "actually_exited": exited,
        "quote": quote, **extra,
    }
