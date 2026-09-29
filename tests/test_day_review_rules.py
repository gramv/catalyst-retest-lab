"""The day-review versions, rules and wiring (package day-review, plan 4.6.3 and 4.6.4).

Pure rules: CRYPTO_24H_REVIEW_V1 and EARLY_EXIT_AGREEMENT_V1's exact records and scope, the
agent's answers and flags (AGENT_REVIEW_ANSWER_V1, AGENT_EXIT_FLAG_V1), how code reads Jev's
answers (JEV_DAY_REVIEW_QUESTIONS_V1, JEV_EARLY_EXIT_QUESTIONS_V1), the decision table, the
measurement hook and the contexts' budget ladder; then the runtime, app, status and watchdog
wiring. Fixture evidence only.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from catalyst_lab import crypto_holding as ch
from catalyst_lab import day_review as dr
from catalyst_lab.crypto_holding import (
    CRYPTO_24H_HOLD,
    CRYPTO_24H_REVIEW,
    CryptoReviewPolicy,
    admission_policy,
    hard_exit_deadline,
    recorded_hold_policy,
    review_active,
    review_fields,
    time_exit_reason,
)
from catalyst_lab.day_review_dossier import (
    EARLY_EXIT_QUESTIONS,
    compile_day_review_state,
    compile_early_exit_state,
    day_review_questions,
)
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable, encoded_bytes
from catalyst_lab.managed_ops import day_review_alarms
from catalyst_lab.managed_review import ManagedContext
from catalyst_lab.managed_service import STATE_FIELDS, STATUS_FIELDS
from tests.maintenance_fixtures import choice_answer
from tests.test_managed_app import runtime as runtime
from tests.test_managed_app import settings as settings
from tests.test_managed_app import source_factory as source_factory

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


# --- The versions --------------------------------------------------------------------------------


def test_the_review_hold_is_exact_and_read_back_only_from_the_state():
    assert CRYPTO_24H_REVIEW.record() == {
        "policy_id": "CRYPTO_24H_REVIEW_V1", "review_interval_seconds": 86400,
        "request_lead_seconds": 1800, "agent_reply_seconds": 900, "jev_answer_seconds": 1800,
        "review_window_seconds": 4500, "deadline_grace_seconds": 300,
        "jev_call_deadline_seconds": 60, "jev_retry_seconds": 60,
        "exit_reason": "DAY_REVIEW_EXIT", "deadline_exit_reason": "DAY_REVIEW_DEADLINE_EXIT",
        "context_version": "JEV_DAY_REVIEW_CONTEXT_V1",
        "question_version": "JEV_DAY_REVIEW_QUESTIONS_V1",
        "answer_version": "AGENT_REVIEW_ANSWER_V1", "early_exit_version": "EARLY_EXIT_AGREEMENT_V1",
    }
    for field, value in (("request_lead_seconds", 1799), ("review_interval_seconds", 86400.0),
                         ("exit_reason", "HOLD_24H_EXIT"), ("jev_answer_seconds", 3600),
                         ("question_version", "JEV_DAY_REVIEW_QUESTIONS_V2")):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED"):
            CryptoReviewPolicy(**{**CRYPTO_24H_REVIEW.record(), field: value})
    first = datetime(2026, 11, 1, 3, 30, tzinfo=UTC)  # 23:30 New York, the night DST ends.
    assert CRYPTO_24H_REVIEW.review_at(first) == first + timedelta(hours=24)  # Elapsed time.
    assert CRYPTO_24H_REVIEW.review_at(first, 2) == first + timedelta(hours=72)
    assert CRYPTO_24H_REVIEW.exit_at(first, 1) == first + timedelta(hours=48, minutes=80)
    with pytest.raises(ValueError, match="AWARE_CRYPTO_FILL_TIMESTAMP_REQUIRED"):
        CRYPTO_24H_REVIEW.review_at(first.replace(tzinfo=None))
    with pytest.raises(ValueError, match="CONTINUATION_COUNT_REQUIRED"):
        CRYPTO_24H_REVIEW.review_at(first, -1)
    reviewed = {"holding_policy": CRYPTO_24H_REVIEW.record(), "continuations": 1}
    held = {"holding_policy": CRYPTO_24H_HOLD.record()}
    assert recorded_hold_policy(reviewed) == CRYPTO_24H_REVIEW and review_active(reviewed)
    assert recorded_hold_policy(held) == CRYPTO_24H_HOLD and not review_active(held)
    assert recorded_hold_policy({}) is None and not review_active({})
    assert hard_exit_deadline(CRYPTO_24H_REVIEW, first, reviewed) == first + timedelta(
        hours=48, minutes=80)
    assert hard_exit_deadline(CRYPTO_24H_HOLD, first, reviewed) == first + timedelta(hours=24)
    assert review_fields(CRYPTO_24H_REVIEW, first, reviewed) == {
        "day_review_at": (first + timedelta(hours=48)).isoformat(), "continuations": 1}
    assert review_fields(CRYPTO_24H_HOLD, first, held) == {}  # The hold's states unchanged.
    # The fail-safe never renames an exit already requested; the hold keeps HOLD_24H_EXIT.
    assert time_exit_reason(reviewed) == "DAY_REVIEW_DEADLINE_EXIT"
    assert time_exit_reason({**reviewed, "exit_requested": "DAY_REVIEW_EXIT"}) == (
        "DAY_REVIEW_EXIT")
    assert time_exit_reason({**held, "exit_requested": "EARLY_EXIT_AGREED"}) == "HOLD_24H_EXIT"
    assert time_exit_reason({}) == "TIME_EXIT"


def test_the_review_is_the_maintained_arms_hold_and_the_control_arm_keeps_the_24_hour_exit():
    # From package answer-rules the maintained arm records CRYPTO_24H_REVIEW_V2 (V1's record
    # plus its answer rule); V1 is unchanged and still read back (tests/test_answer_rules).
    assert admission_policy(True, "JEV_MANAGED") == ch.CRYPTO_24H_REVIEW_V2
    assert admission_policy(True, "JEV_MANAGED") != CRYPTO_24H_REVIEW
    assert admission_policy(True, "FIXED_EXIT") == CRYPTO_24H_HOLD
    assert admission_policy(False, "JEV_MANAGED") is None
    assert admission_policy(False, "FIXED_EXIT") is None
    from catalyst_lab.account_risk import JEV_MANAGED_ARM

    assert ch.JEV_MANAGED_ARM == JEV_MANAGED_ARM


def test_the_early_exit_agreement_is_exact():
    assert dr.EARLY_EXIT_AGREEMENT.record() == {
        "policy_id": "EARLY_EXIT_AGREEMENT_V1", "answer_window_seconds": 900,
        "flag_version": "EARLY_EXIT_FLAG_V1", "agent_flag_version": "AGENT_EXIT_FLAG_V1",
        "answer_version": "AGENT_REVIEW_ANSWER_V1",
        "context_version": "JEV_EARLY_EXIT_CONTEXT_V1",
        "question_version": "JEV_EARLY_EXIT_QUESTIONS_V1", "exit_reason": "EARLY_EXIT_AGREED",
        "jev_call_deadline_seconds": 60, "jev_retry_seconds": 60,
    }
    with pytest.raises(ValueError, match="EXPLICIT_EARLY_EXIT_POLICY_REQUIRED"):
        replace(dr.EARLY_EXIT_AGREEMENT, answer_window_seconds=1800)
    assert dr.review_id_for("s", "l", 1) == dr.review_id_for("s", "l", 1) != dr.review_id_for(
        "s", "l", 2)
    with pytest.raises(ValueError, match="REVIEW_NUMBER_REQUIRED"):
        dr.review_id_for("s", "l", 0)


# --- The agent's answers and flags ---------------------------------------------------------------


def body(**changes):
    value = {"schema_version": "AGENT_REVIEW_ANSWER_V1",
             "answer_id": "5b0a6f5e-4c55-4f39-9b41-0e2f8e7c3a10", "decision": "CONTINUE",
             "what_changed": "Volume held.", "next_24h": "A retest of the high.",
             "proves_wrong": "An hourly close below 100."}
    value.update(changes)
    return value


def check(raw, allowed=True, agent="claude"):
    return dr.validate_review_answer(raw, agent_id=agent, now=NOW, suggestions_allowed=allowed)


def test_an_agent_answer_is_checked_and_canonical():
    answer = check(body(suggested_stop="103.10", suggested_target="112.5"))
    assert answer == {**body(), "suggested_stop": "103.10", "suggested_target": "112.5",
                      "sources": []}
    assert check(body(decision="EXIT"))["decision"] == "EXIT"
    assert check(body(what_changed="x" * 600, next_24h="y" * 600, proves_wrong="z" * 400))
    refusals = {
        "REVIEW_ANSWER_FIELDS_INVALID": [{**body(), "agent_confidence": "0.9"},
                                         {k: v for k, v in body().items() if k != "decision"},
                                         "not an object"],
        "REVIEW_ANSWER_SCHEMA_REQUIRED": [body(schema_version="AGENT_REVIEW_ANSWER_V2")],
        "REVIEW_DECISION_INVALID": [body(decision="HOLD")],
        "ANSWER_ID_INVALID": [body(answer_id="not-a-uuid")],
        "ANSWER_TEXT_INVALID": [body(what_changed="x" * 601), body(proves_wrong="z" * 401),
                                body(next_24h=" "), body(what_changed=7)],
        "SUGGESTED_LEVEL_INVALID": [body(suggested_stop=103.1), body(suggested_stop="-1"),
                                    body(suggested_target="NaN"), body(suggested_stop="abc")],
        "SUGGESTED_LEVELS_NOT_ALLOWED": [body(decision="EXIT", suggested_stop="103")],
        "AGENT_IDENTITY_IN_ANSWER": [body(what_changed="Claude sees strength."),
                                     body(next_24h="per CLAUDE's run")],
        "SENSITIVE_EVIDENCE_REJECTED": [body(what_changed="Mail owner@example.test now."),
                                        body(proves_wrong="Bearer abcdefghijklmnop"),
                                        body(next_24h="0x" + "a" * 40)],
        "SOURCE_COUNT_INVALID": [body(sources=[{}] * 9), body(sources="none")],
    }
    for code, raws in refusals.items():
        for raw in raws:
            with pytest.raises(ValueError, match=f"^{code}$"):
                check(raw)
    with pytest.raises(ValueError, match="^SUGGESTED_LEVELS_NOT_ALLOWED$"):
        check(body(suggested_target="112"), allowed=False)  # An answer to a flag.
    assert check(body(what_changed="claudes view"))  # Not the agent's name (a longer word).
    named = [{"source_id": "Claude-notes-1", "url": "https://example.org/a", "excerpt": "e",
              "retrieved_at": NOW.isoformat()}]
    with pytest.raises(ValueError, match="^AGENT_IDENTITY_IN_ANSWER$"):
        check(body(sources=named))  # A source ID the agent wrote names it.
    quoted = [{**named[0], "source_id": "src-1", "excerpt": "Claude said so."}]
    assert check(body(sources=quoted))["sources"][0]["excerpt"] == "Claude said so."
    long = [{"source_id": f"s{i}", "url": "https://example.org/a", "excerpt": "e" * 1200,
             "retrieved_at": NOW.isoformat()} for i in range(8)]
    assert len(check(body(sources=long))["sources"]) == 8
    with pytest.raises(ValueError, match="^EVIDENCE_TOO_LONG$"):
        check(body(sources=long, what_changed="w" * 600, next_24h="n" * 600,
                   proves_wrong="p" * 400))  # The whole answer stays within 12,000 bytes.


def test_an_agent_flag_is_checked_and_canonical():
    raw = {"schema_version": "AGENT_EXIT_FLAG_V1",
           "flag_ref": "0f1e2d3c-4b5a-4968-8776-655443322110",
           "lifecycle_id": "7c3d7e1a-2b8f-4d6e-9a1b-3c5d7e9f1a2b",
           "what_changed": "Withdrawals paused.", "next_24h": "Selling pressure.",
           "proves_wrong": "Withdrawals resume."}
    assert dr.validate_exit_flag(raw, agent_id="claude", now=NOW) == {**raw, "sources": []}
    for broken, code in (({**raw, "decision": "EXIT"}, "EXIT_FLAG_FIELDS_INVALID"),
                         ({**raw, "schema_version": "AGENT_EXIT_FLAG_V2"},
                          "EXIT_FLAG_SCHEMA_REQUIRED"),
                         ({**raw, "lifecycle_id": "x"}, "POSITION_LIFECYCLE_INVALID"),
                         ({**raw, "flag_ref": 5}, "FLAG_REF_INVALID"),
                         ({**raw, "what_changed": "claude says so"}, "AGENT_IDENTITY_IN_ANSWER")):
        with pytest.raises(ValueError, match=f"^{code}$"):
            dr.validate_exit_flag(broken, agent_id="claude", now=NOW)


# --- Jev's answers, read by code -----------------------------------------------------------------


OPTIONS = {"stop": [{"option_id": "S1", "price": "103.00"}],
           "target": [{"option_id": "T1", "price": "112.35"}]}


def answers(labels, questions):
    return {name: choice_answer(question, labels[name]) for name, question in questions.items()}


def review_questions(status="ANSWERED"):
    data = {"state": {"as_of": NOW.isoformat(),
                      "trade": {"bid": "106", "entry": "100.10", "risk_per_coin": "5.10"},
                      "review": {"agent_answer_status": status}},
            "options": {"stop": [{**OPTIONS["stop"][0], "kind": "stop", "bases": ["BREAKEVEN"],
                                  "bar_end": None}],
                        "target": [{**OPTIONS["target"][0], "kind": "target",
                                    "bases": ["HIGH_24H"], "bar_end": None}]},
            "identity": {}, "expires_at": NOW.isoformat()}
    text = encoded(data)
    return day_review_questions(ManagedContext(text, digest(text))).questions


GOOD = {"trade_reason": "INTACT", "agent_case": "HOLDS", "decision": "CONTINUE",
        "stop_option": "S1", "target_option": "KEEP"}


def test_code_reads_the_review_answer_and_refuses_what_it_cannot_use():
    questions = review_questions()
    usable = dr.read_review_answer(answers(GOOD, questions), OPTIONS, agent_asked=True)
    assert usable.usable and (usable.decision, usable.stop_option) == ("CONTINUE", "S1")
    assert usable.summary["decision"] == {"choice": "CONTINUE", "top_p": "0.8"}
    cases = [
        ({"decision": "EXIT"}, "CONTRADICTORY_REVIEW_ANSWERS"),  # EXIT needs KEEP, KEEP.
        ({"trade_reason": "BROKEN"}, "CONTRADICTORY_REVIEW_ANSWERS"),  # CONTINUE if broken?
        ({"agent_case": "Insufficient evidence"}, "UNCERTAIN_JUDGMENT"),
    ]
    for change, code in cases:
        result = dr.read_review_answer(answers({**GOOD, **change}, questions), OPTIONS,
                                       agent_asked=True)
        assert (result.usable, result.code) == (False, code)
    exit_all = {**GOOD, "decision": "EXIT", "stop_option": "KEEP", "trade_reason": "BROKEN"}
    assert dr.read_review_answer(answers(exit_all, questions), OPTIONS, agent_asked=True).usable
    unknown = dr.read_review_answer(answers(GOOD, questions), {"stop": [], "target": []},
                                    agent_asked=True)
    assert unknown.code == "UNKNOWN_OPTION"
    alone = review_questions("NO_ANSWER")
    assert "agent_case" not in alone
    assert dr.read_review_answer(answers(GOOD, alone), OPTIONS, agent_asked=False).usable
    missing = dr.read_review_answer(answers(GOOD, alone), OPTIONS, agent_asked=True)
    assert missing.code == "INVALID_REVIEW_ANSWER"
    tied = answers(GOOD, questions)
    tied["decision"]["probabilities"] = {k: 0.5 if k in {"CONTINUE", "EXIT"} else 0.0
                                         for k in tied["decision"]["probabilities"]}
    assert dr.read_review_answer(tied, OPTIONS, agent_asked=True).code == "UNCERTAIN_JUDGMENT"


def test_code_reads_the_early_exit_answer():
    questions = EARLY_EXIT_QUESTIONS.questions
    assert set(questions) == {"trade_reason", "agent_case", "exit_now"}
    exit_now = {"trade_reason": "WEAKENED", "agent_case": "HOLDS", "exit_now": "EXIT"}
    assert dr.read_flag_answer(answers(exit_now, questions)).decision == "EXIT"
    stay = dr.read_flag_answer(answers({**exit_now, "exit_now": "STAY",
                                        "trade_reason": "BROKEN"}, questions))
    assert stay.code == "CONTRADICTORY_EARLY_EXIT_ANSWERS"
    assert dr.read_flag_answer({}).code == "INVALID_EARLY_EXIT_ANSWER"


def test_the_question_texts_are_fixed():
    """A change to any question text is a new question-set version (template hashes pinned)."""
    assert EARLY_EXIT_QUESTIONS.version == "JEV_EARLY_EXIT_QUESTIONS_V1"
    assert EARLY_EXIT_QUESTIONS.stage == "TRACKING"
    assert EARLY_EXIT_QUESTIONS.template_hash == (
        "2858affeea9b1ceb0373d6fb442f2f958abe7983fa809d7e1a3df72d89198ad5")
    for status, expected in (
        ("ANSWERED", "3d20d6339d3ea3751d174752f762a36c954bbdd24347f33de0c38e15e5523592"),
        ("NO_ANSWER", "5ce7fa0ed83cadbc34a550dd4853f0989e4cda1a23aa3f9a184e94ee99ce7021"),
    ):
        data = {"state": {"as_of": "2026-09-27T12:00:00+00:00",
                          "trade": {"bid": "106", "entry": "100.10", "risk_per_coin": "5.10"},
                          "review": {"agent_answer_status": status}},
                "options": {"stop": [], "target": []}, "identity": {},
                "expires_at": "2026-09-27T12:01:00+00:00"}
        text = encoded(data)
        question_set = day_review_questions(ManagedContext(text, digest(text)))
        assert question_set.version == "JEV_DAY_REVIEW_QUESTIONS_V1"
        assert question_set.template_hash == expected
    decision = review_questions()["decision"]["instructions"]
    for text in ("continue for another 24 hours", "one discussion round", "still disagree",
                 "both say CONTINUE", "decides alone"):
        assert text in decision, text


# --- Decisions and the measurement hook ----------------------------------------------------------


def jev(decision, code=None):
    return dr.JevAnswer(decision, "INTACT", None, "KEEP", "KEEP", code, {})


def test_the_decision_table():
    assert dr.first_round(jev("CONTINUE", "UNCERTAIN_JUDGMENT"), "CONTINUE",
                          addressee="a") == ("EXIT", "JEV_ANSWER_UNUSABLE")
    assert dr.first_round(jev("CONTINUE"), None, addressee="a") == (
        "CONTINUE", "AGENT_SILENT_JEV_ALONE")
    assert dr.first_round(jev("EXIT"), None, addressee=None) == ("EXIT", "NO_AGENT_JEV_ALONE")
    assert dr.first_round(jev("EXIT"), "EXIT", addressee="a") == ("EXIT", "AGREED")
    assert dr.first_round(jev("CONTINUE"), "CONTINUE", addressee="a") == ("CONTINUE", "AGREED")
    assert dr.first_round(jev("EXIT"), "CONTINUE", addressee="a") is None  # Discussion.
    assert dr.final_round(jev("CONTINUE"), "CONTINUE") == ("CONTINUE", "AGREED_AFTER_DISCUSSION")
    assert dr.final_round(jev("EXIT"), "EXIT") == ("EXIT", "AGREED_AFTER_DISCUSSION")
    assert dr.final_round(jev("EXIT"), "CONTINUE") == ("EXIT", "DISAGREED_AFTER_DISCUSSION")
    assert dr.final_round(jev("CONTINUE"), "EXIT") == ("EXIT", "DISAGREED_AFTER_DISCUSSION")
    assert dr.final_round(jev("CONTINUE", "X"), "CONTINUE") == ("EXIT", "JEV_ANSWER_UNUSABLE")
    assert dr.flag_resolution(True) == "EXIT_AGREED"
    assert dr.flag_resolution(False) == "EXIT_NOT_AGREED"
    assert dr.measurement(change_kind="CONTINUE_EXIT_DECISION", at="t",
                          levels_before={"stop": "95", "target": "111"},
                          levels_after={"stop": "103.00", "target": "111"}, exited=False,
                          quote={"bid": "106"}, continuations_after=1) == {
        "change_kind": "CONTINUE_EXIT_DECISION", "at": "t", "old_stop": "95",
        "old_target": "111", "new_stop": "103.00", "new_target": None,
        "actually_exited": False, "quote": {"bid": "106"}, "continuations_after": 1}


# --- The contexts' budget ladder -----------------------------------------------------------------


def inputs(now=NOW):
    from tests.maintenance_fixtures import bars_series

    bars = {
        "bars_15m": bars_series(now, 96, seconds=900, base=106),
        "bars_1h": bars_series(now, 168, seconds=3600, base=106),
        "btc_15m": bars_series(now, 96, seconds=900, base=60000, symbol="BTC/USD"),
        "btc_1h": bars_series(now, 168, seconds=3600, base=60000, symbol="BTC/USD"),
    }
    from decimal import Decimal as D

    trade = {"entry": D("100.10"), "qty": D("9.8"), "initial_stop": D("95"),
             "initial_target": D("111"), "max_entry": D("100.10"), "risk": D("5.10"),
             "stop": D("95"), "target": D("111"), "bid": D("106"), "ask": D("106.03"),
             "quote_at": now, "opened_at": (now - timedelta(hours=24)).isoformat(),
             "review_at": now.isoformat(), "best_bid": D("107"), "worst_bid": D("99"),
             "milestone": 1}
    pick = {"kind": "BOTH", "thesis": "THESIS " * 100, "disproof": "DISPROOF " * 40,
            "why_now": "why " * 100, "why_these_levels": "levels " * 80, "risks": "risk " * 100}
    return {"now": now, "symbol": "SOL/USD", "pick": pick, "selection": {}, "trade": trade,
            "changes": [], "news": [], "trigger": {"reasons": ["DAY_REVIEW"]}, **bars}


def agent_answer(sources=3):
    return {"decision": "CONTINUE", "what_changed": "w" * 600, "next_24h": "n" * 600,
            "proves_wrong": "p" * 400, "suggested_stop": None, "suggested_target": None,
            "sources": [{"source_id": f"s{i}", "excerpt": "e" * 1200, "content_hash": f"h{i}",
                         "published_at": NOW.isoformat()} for i in range(sources)]}


def test_the_review_context_drops_the_agents_sources_first_and_never_its_reasons():
    review = {"round": "FIRST", "agent_answer_status": "ANSWERED"}
    options = {"stop": [], "target": []}
    roomy = compile_day_review_state(review=review, agent_answer=agent_answer(),
                                     discussion=None, options=options, **inputs())
    assert len(roomy.state["agent_answer"]["sources"]) == 3
    assert all(len(s["excerpt"]) == 300 for s in roomy.state["agent_answer"]["sources"])
    size = encoded_bytes(roomy.state)
    tight = compile_day_review_state(budget=size - 200, review=review,
                                     agent_answer=agent_answer(), discussion=None,
                                     options=options, **inputs())
    assert tight.manifest["budget_steps"][0] == "OMIT_AGENT_SOURCE"
    assert encoded_bytes(tight.state) <= size - 200
    agent = tight.state["agent_answer"]
    assert (agent["what_changed"], agent["proves_wrong"]) == ("w" * 600, "p" * 400)
    again = compile_day_review_state(budget=size - 200, review=review,
                                     agent_answer=agent_answer(), discussion=None,
                                     options=options, **inputs())
    assert encoded(again.state) == encoded(tight.state)  # Deterministic.
    with pytest.raises(ContextBudgetUnsatisfiable):
        compile_day_review_state(budget=2_000, review=review, agent_answer=agent_answer(),
                                 discussion=None, options=options, **inputs())
    final = compile_day_review_state(
        review={**review, "round": "FINAL"}, agent_answer=agent_answer(0),
        discussion={"your_first_answer": {"decision": {"choice": "EXIT", "top_p": "0.80"}},
                    "agent_reply": agent_answer(1)}, options=options, **inputs())
    assert final.state["discussion"]["your_first_answer"]["decision"]["choice"] == "EXIT"
    assert final.state["discussion"]["agent_reply"]["what_changed"] == "w" * 600
    flag = compile_early_exit_state(review={"round": "FIRST"}, agent_flag=agent_answer(1),
                                    **inputs())
    assert "options" not in flag.state and flag.state["context_version"] == (
        "JEV_EARLY_EXIT_CONTEXT_V1")
    assert not any(e.get("section") == "options" for e in flag.manifest["entries"])
    assert flag.manifest["state_sha256"] == digest(encoded(flag.state))


# --- Wiring: the runtime, the app, the status and the watchdog ------------------------------------


def test_the_status_fields_the_alarm_and_its_fail_closed_reading():
    assert "day_reviews" in STATUS_FIELDS
    assert {"day_review_at", "continuations", "holding_policy"} <= STATE_FIELDS
    assert day_review_alarms(None) == []
    assert day_review_alarms({"failing_reviews": []}) == []
    assert day_review_alarms({"failing_reviews": [{"code": "HTTP_503"}]}) == [
        "DAY_REVIEW_JEV_FAILING"]
    for broken in ({"available": False}, "nope", {"failing_reviews": None}):
        assert day_review_alarms(broken) == ["DAY_REVIEW_STATUS_UNAVAILABLE"]


class Recorder:
    management_reviews = "ENABLED"

    def __init__(self):
        self.passes = []
        self.agents = None

    async def run_pass(self, setups, observe):
        self.passes.append([s["symbol"] for s in setups])
        return 0

    def status(self, active):
        return {"failing_reviews": [], "open_trades": 1}


def test_the_runtime_runs_the_reviews_and_reports_them(monkeypatch):
    from tests.test_managed_runtime import runtime

    run = runtime()
    recorder = Recorder()
    run.day_reviews = recorder
    asyncio.run(run._day_review_pass())
    assert recorder.passes == [[s["symbol"] for s in run.execution.store.active()
                                if s["state"]["state"] == "OPEN"]]
    assert run.status()["day_reviews"] == {"failing_reviews": [], "open_trades": 1}

    def broken(active):
        raise RuntimeError("fixture")

    recorder.status = broken
    assert run.status()["day_reviews"] == {"available": False}
    run.day_reviews = None
    assert run.status()["day_reviews"] is None


def test_the_review_setting_must_match_the_runtime_switch():
    from catalyst_lab.alpaca import AlpacaCredentials
    from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
    from tests.test_managed_runtime import Execution, Research, Source

    recorder = Recorder()
    recorder.management_reviews = "DISABLED"
    with pytest.raises(ValueError, match="MANAGEMENT_REVIEWS_SETTING_MISMATCH"):
        ManagedRuntime(Execution(), Research(), Source(),
                       AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret"),
                       engineering_runtime_policy(), clock=lambda: NOW,
                       reviewer_heartbeat=lambda: True, day_reviews=recorder)


def test_the_factory_wires_the_reviews_to_the_position_source(monkeypatch):
    from catalyst_lab.managed_runtime import ManagedRuntime
    from catalyst_lab.trade_review import DayReviews
    from tests.test_managed_runtime import (
        test_factory_wires_actual_second_jev_role_with_separate_market_source as factory,
    )

    built = {}
    original = ManagedRuntime.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        built["run"] = self

    monkeypatch.setattr(ManagedRuntime, "__init__", capture)
    factory(monkeypatch)
    run = built["run"]
    assert isinstance(run.day_reviews, DayReviews)
    assert run.day_reviews.bars is run.monitor_tick.source  # The read-only position source.
    assert run.day_reviews.management_reviews == run.management_reviews
    assert run.day_reviews.agents is None  # The app sets the configured agents at start.


def test_the_app_gives_the_runtime_its_configured_agents_and_serves_the_routes(
        runtime, settings, source_factory):
    from fastapi.testclient import TestClient

    from catalyst_lab.managed_app import configured_agents, create_application
    from tests.test_managed_app import TOKEN

    claude = "fixture-claude-agent-token-" + "c" * 20
    settings = replace(settings, agent_tokens={"claude": claude})
    assert configured_agents(settings) == frozenset({"muse", "claude"})
    recorder = Recorder()
    runtime.day_reviews = recorder
    app = create_application(runtime, settings, source_factory=source_factory[0])
    assert recorder.agents == frozenset({"muse", "claude"})
    client = TestClient(app)
    for token in (claude, TOKEN):
        reply = client.get("/api/v1/lab/reviews", headers={"Authorization": "Bearer " + token})
        assert reply.status_code == 200 and reply.json()["items"] == []
        assert reply.json()["request_lead_seconds"] == 1800
    missing = client.post("/api/v1/lab/reviews/00000000-0000-4000-8000-000000000000/answer",
                          json=body(), headers={"Authorization": "Bearer " + claude})
    assert (missing.status_code, missing.json()["detail"]) == (404, "REVIEW_NOT_FOUND")
    assert client.get("/api/v1/lab/reviews").status_code == 401


# --- MUSE_RESEARCH_GUIDELINES_V5 -----------------------------------------------------------------


def test_guidelines_v5_is_v4_plus_the_reviews():
    from pathlib import Path

    from catalyst_lab.muse_guidelines import (
        MUSE_GUIDELINES_V4,
        MUSE_GUIDELINES_V4_SHA256,
        MUSE_GUIDELINES_V5,
        MUSE_GUIDELINES_V5_REVIEWS,
        MUSE_GUIDELINES_V5_SHA256,
        MUSE_GUIDELINES_V5_VERSION,
    )

    root = Path(__file__).resolve().parents[1]
    assert MUSE_GUIDELINES_V4_SHA256 == (
        "7d635a48104d2bfdcc37f809fbfec546d3428ed1b8eafe03ad47ffcc6fa854e6")  # Unchanged.
    assert MUSE_GUIDELINES_V5 == MUSE_GUIDELINES_V4 + MUSE_GUIDELINES_V5_REVIEWS
    assert MUSE_GUIDELINES_V5_VERSION == "MUSE_RESEARCH_GUIDELINES_V5"
    assert all(ord(c) < 128 for c in MUSE_GUIDELINES_V5_REVIEWS)
    reviews = " ".join(MUSE_GUIDELINES_V5_REVIEWS.split())
    for text in ("GET /api/v1/lab/reviews", "30 minutes before T", "AGENT_REVIEW_ANSWER_V1",
                 "decision CONTINUE or EXIT", "what_changed", "next_24h", "proves_wrong",
                 "Jev decides alone", "round DISCUSSION", "within 15 minutes",
                 "the trade exits", "AGENT_EXIT_FLAG_V1", "/exit-flag", "EXIT_FLAG",
                 "never your name, token or confidence", "12,000 bytes"):
        assert text in reviews, text
    document = (root / "docs" / "MUSE-GUIDELINES.md").read_text()
    runtime = document.split("<!-- runtime-guidelines-v5:start -->", 1)[1].split(
        "<!-- runtime-guidelines-v5:end -->", 1)[0]
    assert runtime == MUSE_GUIDELINES_V5_REVIEWS
    assert MUSE_GUIDELINES_V5_SHA256 in document
    # Package learning-app: the context serves MUSE_RESEARCH_GUIDELINES_V6 (V5 byte for byte plus
    # the learning section) from 2026-09-28; tests/test_learning_guidelines.py checks it.
