"""Package answer-rules as pure rules: CRYPTO_MAINTENANCE_V2 and CRYPTO_24H_REVIEW_V2.

The versions' exact records and dispatch (every V1 record, reader, context and question set
unchanged and still read back), MAINTENANCE_ANSWER_RULE_V2 and DAY_REVIEW_ANSWER_RULE_V2 as unit
tables (every action or decision against every option state, beside V1's reading of the same
answers), the V2 minute cadence and in-flight rule, JEV_MANAGED_POSITION_CONTEXT_V5 /
QUESTIONS_V5 and JEV_DAY_REVIEW_CONTEXT_V2 / QUESTIONS_V2 with their pinned templates. No
database, broker or provider.
"""

from datetime import timedelta
from decimal import Decimal as D
from itertools import product

import pytest

from catalyst_lab import crypto_holding as ch
from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import day_review as dr
from catalyst_lab import day_review_dossier as drd
from catalyst_lab import maintenance_dossier as md
from catalyst_lab.account_risk import FIXED_EXIT_ARM, JEV_MANAGED_ARM
from catalyst_lab.jev_contract import INSUFFICIENT, digest, encoded
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable, encoded_bytes
from catalyst_lab.managed_review import ManagedContext
from tests.test_maintenance_rules import NOW, pick, production_inputs, series

V1_MAINTENANCE = {
    "policy_id": "CRYPTO_MAINTENANCE_V1", "context_version": "JEV_MANAGED_POSITION_CONTEXT_V4",
    "question_version": "JEV_MANAGED_POSITION_QUESTIONS_V4", "review_bar_seconds": 900,
    "near_target_fraction": "0.005", "near_stop_fraction": "0.005",
    "min_review_interval_seconds": 60, "answer_max_age_seconds": 60,
    "review_deadline_seconds": 10, "stop_bid_margin": "0.005", "swing_low_price_margin": "0.01",
    "max_stop_options": 5, "max_target_options": 5, "swing_span": 2,
    "benchmark_symbol": "BTC/USD", "benchmark_shock_fraction": "0.03",
    "benchmark_window_seconds": 900, "exit_flag_version": "EARLY_EXIT_FLAG_V1",
}
V2_MAINTENANCE = {
    **V1_MAINTENANCE, "policy_id": "CRYPTO_MAINTENANCE_V2",
    "context_version": "JEV_MANAGED_POSITION_CONTEXT_V5",
    "question_version": "JEV_MANAGED_POSITION_QUESTIONS_V5", "review_bar_seconds": 60,
    "answer_rule": "MAINTENANCE_ANSWER_RULE_V2",
}
V1_REVIEW = {
    "policy_id": "CRYPTO_24H_REVIEW_V1", "review_interval_seconds": 86400,
    "request_lead_seconds": 1800, "agent_reply_seconds": 900, "jev_answer_seconds": 1800,
    "review_window_seconds": 4500, "deadline_grace_seconds": 300,
    "jev_call_deadline_seconds": 60, "jev_retry_seconds": 60, "exit_reason": "DAY_REVIEW_EXIT",
    "deadline_exit_reason": "DAY_REVIEW_DEADLINE_EXIT",
    "context_version": "JEV_DAY_REVIEW_CONTEXT_V1",
    "question_version": "JEV_DAY_REVIEW_QUESTIONS_V1",
    "answer_version": "AGENT_REVIEW_ANSWER_V1", "early_exit_version": "EARLY_EXIT_AGREEMENT_V1",
}
V2_REVIEW = {
    **V1_REVIEW, "policy_id": "CRYPTO_24H_REVIEW_V2",
    "context_version": "JEV_DAY_REVIEW_CONTEXT_V2",
    "question_version": "JEV_DAY_REVIEW_QUESTIONS_V2",
    "answer_rule": "DAY_REVIEW_ANSWER_RULE_V2",
}
V3_CRYPTO = {"market": "CRYPTO", "report_schema_version": "AGENT_RESEARCH_REPORT_V3"}


# --- The versions ------------------------------------------------------------------------------


def test_v1_maintenance_is_unchanged_and_v2_is_exact_and_admitted_in_the_maintained_arm():
    assert cm.CRYPTO_MAINTENANCE.record() == V1_MAINTENANCE  # V1, byte for byte.
    assert cm.CRYPTO_MAINTENANCE_V2.record() == V2_MAINTENANCE
    for change in ({"review_bar_seconds": 900}, {"answer_rule": "MAINTENANCE_ANSWER_RULE_V1"},
                   {"context_version": "JEV_MANAGED_POSITION_CONTEXT_V4"},
                   {"question_version": "JEV_MANAGED_POSITION_QUESTIONS_V4"},
                   {"min_review_interval_seconds": 30}, {"stop_bid_margin": "0.004"}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
            cm.MaintenancePolicyV2(**{**V2_MAINTENANCE, **change})
    with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
        cm.policy_from_record({**V2_MAINTENANCE, "review_bar_seconds": 900})
    with pytest.raises(TypeError):  # V1 never carries an answer rule.
        cm.policy_from_record({**V1_MAINTENANCE, "answer_rule": "MAINTENANCE_ANSWER_RULE_V2"})
    with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
        cm.policy_from_record(None)
    v1 = cm.recorded_policy({"maintenance_policy": V1_MAINTENANCE})
    v2 = cm.recorded_policy({"maintenance_policy": V2_MAINTENANCE})
    assert type(v1) is cm.MaintenancePolicy and v1 == cm.CRYPTO_MAINTENANCE
    assert type(v2) is cm.MaintenancePolicyV2 and v2 == cm.CRYPTO_MAINTENANCE_V2 != v1
    assert cm.active({"maintenance_policy": V1_MAINTENANCE})
    assert cm.active({"maintenance_policy": V2_MAINTENANCE}) and not cm.active({})
    assert cm.recorded_policy_id({"maintenance_policy": V2_MAINTENANCE}) == (
        "CRYPTO_MAINTENANCE_V2")
    # Admission recorded V2 in the maintained arm from package answer-rules; from package
    # jev-budget it records V3 (V2 plus the budget's cadence; tests/test_jev_budget_rules.py).
    # The control arm and other setups as before.
    assert cm.ADMITTED_MAINTENANCE == cm.CRYPTO_MAINTENANCE_V3
    assert cm.admission_fields(V3_CRYPTO, JEV_MANAGED_ARM) == {
        "maintenance_policy": cm.CRYPTO_MAINTENANCE_V3.record(),
        "partial_entry_policy": cm.CRYPTO_PARTIAL_ENTRY.record()}
    assert cm.admission_fields(V3_CRYPTO, FIXED_EXIT_ARM) == {
        "partial_entry_policy": cm.CRYPTO_PARTIAL_ENTRY.record()}
    # Each recorded version keeps its own reader.
    assert md.answer_reader(V1_MAINTENANCE) is md.read_answer
    assert md.answer_reader(V2_MAINTENANCE) is md.read_answer_v2
    with pytest.raises(ValueError):
        md.answer_reader({**V1_MAINTENANCE, "policy_id": "CRYPTO_MAINTENANCE_V3"})


def test_v1_review_is_unchanged_and_v2_is_exact_and_admitted_in_the_maintained_arm():
    assert ch.CRYPTO_24H_REVIEW.record() == V1_REVIEW
    assert ch.CRYPTO_24H_REVIEW_V2.record() == V2_REVIEW
    for change in ({"answer_rule": "DAY_REVIEW_ANSWER_RULE_V1"}, {"jev_answer_seconds": 3600},
                   {"context_version": "JEV_DAY_REVIEW_CONTEXT_V1"},
                   {"review_interval_seconds": 86400.0}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED"):
            ch.CryptoReviewPolicyV2(**{**V2_REVIEW, **change})
    first = NOW
    v1_state = {"holding_policy": V1_REVIEW, "continuations": 1}
    v2_state = {"holding_policy": V2_REVIEW, "continuations": 1}
    v1, v2 = ch.recorded_hold_policy(v1_state), ch.recorded_hold_policy(v2_state)
    assert type(v1) is ch.CryptoReviewPolicy and type(v2) is ch.CryptoReviewPolicyV2
    assert ch.review_active(v1_state) and ch.review_active(v2_state)
    # The clock, the fail-safe and the exit reasons are V1's under V2.
    assert ch.hard_exit_deadline(v2, first, v2_state) == ch.hard_exit_deadline(
        v1, first, v1_state) == first + timedelta(hours=48, minutes=80)
    assert ch.review_fields(v2, first, v2_state) == ch.review_fields(v1, first, v1_state)
    assert ch.time_exit_reason(v2_state) == "DAY_REVIEW_DEADLINE_EXIT"
    assert ch.admission_policy(True, JEV_MANAGED_ARM) == ch.CRYPTO_24H_REVIEW_V2
    assert ch.admission_policy(True, FIXED_EXIT_ARM) == ch.CRYPTO_24H_HOLD
    assert ch.admission_policy(False, JEV_MANAGED_ARM) is None
    assert dr.review_reader(V1_REVIEW) is dr.read_review_answer
    assert dr.review_reader(V2_REVIEW) is dr.read_review_answer_v2
    with pytest.raises(ValueError):
        dr.review_reader({**V1_REVIEW, "policy_id": "CRYPTO_24H_REVIEW_V3"})


# --- Answers with exact distributions -----------------------------------------------------------


def choice(labels, top, *, tie=None, p=0.6):
    """A choice answer whose reported choice is ``top``: the unique most probable label, or
    tied with ``tie`` (0.45 each)."""
    probabilities = {label: 0.0 for label in labels}
    if tie is None:
        probabilities[top] = p
        rest = [label for label in labels if label != top]
        for label in rest:
            probabilities[label] = round((1 - p) / len(rest), 6)
    else:
        probabilities[top] = probabilities[tie] = 0.45
        rest = [label for label in labels if label not in {top, tie}]
        for label in rest:
            probabilities[label] = round(0.1 / len(rest), 6)
    return {"type": "choice", "choice": top, "confidence": 0.5, "probabilities": probabilities}


STATES = ("UNIQUE", "KEEP", "TIE", "INSUFFICIENT", "UNKNOWN")
REASONS = {"UNIQUE": None, "KEEP": "KEEP", "TIE": "TIED", "INSUFFICIENT": "INSUFFICIENT_EVIDENCE",
           "UNKNOWN": "UNKNOWN_OPTION"}
OFFERED = {"stop": [{"option_id": "S1"}, {"option_id": "S2"}], "target": [{"option_id": "T1"}]}
FIRST = {"stop": "S1", "target": "T1"}


def option_answer(kind, state):
    """The answer to ``stop_option``/``target_option`` in one of STATES. UNKNOWN names an ID the
    options passed to the reader never offered (only possible with an altered context)."""
    labels = ["KEEP", *(o["option_id"] for o in OFFERED[kind]), INSUFFICIENT,
              "S9" if kind == "stop" else "T9"]
    first = FIRST[kind]
    return {"UNIQUE": lambda: choice(labels, first), "KEEP": lambda: choice(labels, "KEEP"),
            "TIE": lambda: choice(labels, first, tie="KEEP"),
            "INSUFFICIENT": lambda: choice(labels, INSUFFICIENT),
            "UNKNOWN": lambda: choice(labels, labels[-1])}[state]()


def maintenance_answers(action, stop, target, *, reason="INTACT", action_tie=None):
    return {
        "trade_reason": choice(["INTACT", "WEAKENED", "BROKEN", INSUFFICIENT], reason),
        "action": choice([*cm.ACTIONS, INSUFFICIENT], action, tie=action_tie),
        "stop_option": option_answer("stop", stop),
        "target_option": option_answer("target", target),
    }


RAISED = {"HOLD": (), "RAISE_STOP": ("stop",), "RAISE_TARGET": ("target",),
          "RAISE_STOP_AND_TARGET": ("stop", "target"), "FLAG_EARLY_EXIT": ()}
V1_CONSISTENT = {"HOLD": ("KEEP", "KEEP"), "RAISE_STOP": ("UNIQUE", "KEEP"),
                 "RAISE_TARGET": ("KEEP", "UNIQUE"), "RAISE_STOP_AND_TARGET": ("UNIQUE", "UNIQUE")}


@pytest.mark.parametrize("action,stop,target", list(product(cm.ACTIONS, STATES, STATES)))
def test_maintenance_answer_rule_v2_every_action_against_every_option_state(action, stop, target):
    answers = maintenance_answers(action, stop, target)
    v2 = md.read_answer_v2(answers, OFFERED)
    assert v2.code is None and v2.answer_rule == "MAINTENANCE_ANSWER_RULE_V2"
    assert (v2.action, v2.trade_reason) == (action, "INTACT")  # As Jev gave them.
    states = {"stop": stop, "target": target}
    for kind in ("stop", "target"):
        use = v2.option_use[kind]
        assert use["choice"] == answers[kind + "_option"]["choice"]
        if kind in RAISED[action]:
            expected = FIRST[kind] if states[kind] == "UNIQUE" else None
            assert v2.option_to_raise(kind) == use["raise_to"] == expected
            assert use["reason"] == REASONS[states[kind]]
            assert use["code"] == (None if expected else kind.upper() + "_OPTION_NOT_USABLE")
        else:  # An answer the action does not use is recorded, never a contradiction.
            assert v2.option_to_raise(kind) is None and use["raise_to"] is None
            assert (use["reason"], use["code"]) == ("NOT_RAISED_BY_ACTION", None)
    usable_raise = any(states[k] == "UNIQUE" for k in RAISED[action])
    assert v2.changes == (bool(RAISED[action]) and usable_raise)
    assert v2.held_code == ("NO_USABLE_OPTION" if RAISED[action] and not usable_raise else None)
    assert v2.flagged == (action == "FLAG_EARLY_EXIT")
    # V1 reads the same answers by its consistency rules, unchanged: usable only when the
    # options match the action exactly (a flag needs a BROKEN reason, never here).
    v1 = md.read_answer(answers, OFFERED)
    assert (v1.code is None) == (V1_CONSISTENT.get(action) == (stop, target))
    if v1.code is None:  # Where V1 could use the answer, both raise the same levels.
        for kind in ("stop", "target"):
            raised = md.option_to_raise(v1, kind)  # V1: the answer as given (KEEP: none).
            assert (None if raised == "KEEP" else raised) == v2.option_to_raise(kind)


@pytest.mark.parametrize("given,code", [
    (maintenance_answers(INSUFFICIENT, "UNIQUE", "UNIQUE"), "UNCERTAIN_JUDGMENT"),
    (maintenance_answers("RAISE_STOP", "UNIQUE", "KEEP", action_tie="HOLD"),
     "UNCERTAIN_JUDGMENT"),
    (maintenance_answers("HOLD", "KEEP", "KEEP", action_tie="RAISE_STOP_AND_TARGET"),
     "UNCERTAIN_JUDGMENT"),
])
def test_an_uncertain_action_changes_nothing_under_v2_as_under_v1(given, code):
    v2 = md.read_answer_v2(given, OFFERED)
    assert v2.code == code and not v2.changes and not v2.flagged and v2.held_code is None
    assert v2.option_use == {} and v2.option_to_raise("stop") is None
    assert md.read_answer(given, OFFERED).code == code


def test_under_v2_the_trade_reason_is_informational():
    held = md.read_answer_v2(maintenance_answers("HOLD", "UNIQUE", "KEEP", reason="BROKEN"),
                             OFFERED)
    assert held.code is None and not held.changes and not held.flagged
    flagged = md.read_answer_v2(maintenance_answers("FLAG_EARLY_EXIT", "UNIQUE", "UNIQUE"),
                                OFFERED)
    assert flagged.flagged and flagged.trade_reason == "INTACT"  # Recorded, not required.
    for reason in (INSUFFICIENT, "WEAKENED"):
        raised = md.read_answer_v2(
            maintenance_answers("RAISE_STOP", "UNIQUE", "KEEP", reason=reason), OFFERED)
        assert raised.changes and raised.option_to_raise("stop") == "S1"
    tied = maintenance_answers("RAISE_STOP", "UNIQUE", "KEEP")
    tied["trade_reason"] = choice(["INTACT", "WEAKENED", "BROKEN", INSUFFICIENT], "INTACT",
                                  tie="BROKEN")
    assert md.read_answer_v2(tied, OFFERED).changes
    assert md.read_answer(tied, OFFERED).code == "UNCERTAIN_JUDGMENT"  # V1: any tie.
    # V1's rules for the same cases, unchanged.
    assert md.read_answer(maintenance_answers("HOLD", "KEEP", "KEEP", reason="BROKEN"),
                          OFFERED).code == "CONTRADICTORY_MANAGEMENT_ANSWERS"
    assert md.read_answer(maintenance_answers("FLAG_EARLY_EXIT", "KEEP", "KEEP"),
                          OFFERED).code == "CONTRADICTORY_MANAGEMENT_ANSWERS"


def test_another_question_set_is_invalid_under_v2():
    answers = maintenance_answers("HOLD", "KEEP", "KEEP")
    del answers["target_option"]
    assert md.read_answer_v2(answers, OFFERED).code == "INVALID_MANAGEMENT_ANSWER"
    assert md.read_answer_v2(None, OFFERED).code == "INVALID_MANAGEMENT_ANSWER"
    extra = {**maintenance_answers("HOLD", "KEEP", "KEEP"), "decision": choice(["A", "B"], "A")}
    assert md.read_answer_v2(extra, OFFERED).code == "INVALID_MANAGEMENT_ANSWER"


# --- DAY_REVIEW_ANSWER_RULE_V2 -----------------------------------------------------------------


def review_answers(decision, stop, target, *, reason="INTACT", reason_tie=None,
                   decision_tie=None, agent_case="HOLDS", agent_tie=None, agent_asked=True):
    answers = {
        "trade_reason": choice(["INTACT", "WEAKENED", "BROKEN", INSUFFICIENT], reason,
                               tie=reason_tie),
        "decision": choice(["CONTINUE", "EXIT", INSUFFICIENT], decision, tie=decision_tie),
        "stop_option": option_answer("stop", stop),
        "target_option": option_answer("target", target),
    }
    if agent_asked:
        answers["agent_case"] = choice(["HOLDS", "DOES_NOT_HOLD", INSUFFICIENT], agent_case,
                                       tie=agent_tie)
    return answers


@pytest.mark.parametrize("decision,stop,target",
                         list(product(("CONTINUE", "EXIT"), STATES, STATES)))
def test_day_review_answer_rule_v2_every_decision_against_every_option_state(
        decision, stop, target):
    answers = review_answers(decision, stop, target)
    v2 = dr.read_review_answer_v2(answers, OFFERED, agent_asked=True)
    assert v2.usable and v2.code is None and v2.answer_rule == "DAY_REVIEW_ANSWER_RULE_V2"
    assert (v2.decision, v2.stop_option, v2.target_option) == (
        decision, answers["stop_option"]["choice"], answers["target_option"]["choice"])
    states = {"stop": stop, "target": target}
    for kind in ("stop", "target"):
        use = v2.option_use[kind]
        if decision == "EXIT":  # Option answers ignored: never a contradiction.
            assert dr.option_to_raise(v2, kind) is None
            assert (use["reason"], use["code"]) == ("NOT_RAISED_BY_DECISION", None)
            continue
        expected = FIRST[kind] if states[kind] == "UNIQUE" else None
        assert dr.option_to_raise(v2, kind) == use["raise_to"] == expected
        assert use["reason"] == REASONS[states[kind]]
        # KEEP keeps a level by choice; a tie, Insufficient evidence or an unknown ID keeps
        # it because the answer cannot be used (never an exit).
        assert use["code"] == (None if states[kind] in {"UNIQUE", "KEEP"}
                               else kind.upper() + "_OPTION_NOT_USABLE")
    # The decision table is V1's: agreement continues, a usable answer is never an exit.
    assert dr.first_round(v2, decision, addressee="agent") == (decision, dr.AGREED)
    assert dr.first_round(v2, None, addressee=None) == (decision, dr.NO_AGENT_JEV_ALONE)
    # V1's reading of the same answers, unchanged.
    v1 = dr.read_review_answer(answers, OFFERED, agent_asked=True)
    clean = {"UNIQUE", "KEEP"}
    v1_usable = (stop in clean and target in clean) and (
        decision == "CONTINUE" or (stop, target) == ("KEEP", "KEEP"))
    assert v1.usable == v1_usable
    if not v1.usable:
        assert dr.first_round(v1, decision, addressee="agent") == (
            dr.EXIT, dr.JEV_ANSWER_UNUSABLE)


@pytest.mark.parametrize("change,code", [
    ({"decision": INSUFFICIENT}, "UNCERTAIN_JUDGMENT"),
    ({"decision_tie": "EXIT"}, "UNCERTAIN_JUDGMENT"),  # CONTINUE = EXIT.
    ({"reason": "BROKEN"}, "CONTRADICTORY_REVIEW_ANSWERS"),  # V1's one rule that stays.
])
def test_an_unclear_decision_or_a_continue_with_a_broken_reason_is_unusable_and_exits(
        change, code):
    decision = change.pop("decision", "CONTINUE")
    answers = review_answers(decision, "UNIQUE", "TIE", **change)
    v2 = dr.read_review_answer_v2(answers, OFFERED, agent_asked=True)
    assert (v2.usable, v2.code) == (False, code)
    assert dr.option_to_raise(v2, "stop") is None and v2.option_use == {}
    assert dr.first_round(v2, "CONTINUE", addressee="agent") == (dr.EXIT,
                                                                 dr.JEV_ANSWER_UNUSABLE)
    assert dr.final_round(v2, "CONTINUE") == (dr.EXIT, dr.JEV_ANSWER_UNUSABLE)


def test_the_reason_and_the_agents_case_are_otherwise_informational():
    cases = [
        review_answers("CONTINUE", "UNIQUE", "KEEP", reason="BROKEN", reason_tie="INTACT"),
        review_answers("CONTINUE", "UNIQUE", "KEEP", reason=INSUFFICIENT),
        review_answers("EXIT", "KEEP", "KEEP", reason="BROKEN"),
        review_answers("CONTINUE", "UNIQUE", "KEEP", agent_case=INSUFFICIENT),
        review_answers("CONTINUE", "UNIQUE", "KEEP", agent_tie="DOES_NOT_HOLD"),
    ]
    for answers in cases:
        v2 = dr.read_review_answer_v2(answers, OFFERED, agent_asked=True)
        assert v2.usable, answers
    alone = review_answers("CONTINUE", "UNIQUE", "TIE", agent_asked=False)
    v2 = dr.read_review_answer_v2(alone, OFFERED, agent_asked=False)
    assert v2.usable and v2.agent_case is None and dr.option_to_raise(v2, "stop") == "S1"
    missing = dr.read_review_answer_v2(alone, OFFERED, agent_asked=True)
    assert missing.code == "INVALID_REVIEW_ANSWER"
    assert dr.read_review_answer_v2({}, OFFERED, agent_asked=False).code == (
        "INVALID_REVIEW_ANSWER")


def test_a_v2_answer_record_reads_back_as_v2_and_a_v1_record_as_v1():
    v2 = dr.read_review_answer_v2(review_answers("CONTINUE", "UNIQUE", "TIE"), OFFERED,
                                  agent_asked=True)
    again = dr.answer_from_record(v2.record())
    assert type(again) is dr.JevReviewAnswerV2 and again == v2
    assert again.record()["option_use"]["target"]["reason"] == "TIED"
    v1 = dr.read_review_answer(review_answers("CONTINUE", "UNIQUE", "KEEP"), OFFERED,
                               agent_asked=True)
    assert "answer_rule" not in v1.record()  # V1's record keeps exactly its fields.
    assert type(dr.answer_from_record(v1.record())) is dr.JevAnswer
    # The early-exit reader is the same under both versions.
    flag = {"trade_reason": choice(["INTACT", "WEAKENED", "BROKEN", INSUFFICIENT], "WEAKENED"),
            "agent_case": choice(["HOLDS", "DOES_NOT_HOLD", INSUFFICIENT], "DOES_NOT_HOLD"),
            "exit_now": choice(["EXIT", "STAY", INSUFFICIENT], "STAY")}
    assert dr.read_flag_answer(flag).decision == "STAY"


# --- The V2 cadence -----------------------------------------------------------------------------


def due(now, *, served=None, last=None, triggers=(), bar_seconds=60, opened=None):
    return cm.due_reasons(
        now=now, opened_at=opened or now - timedelta(hours=1), served_bar_end=served,
        unserved_triggers=list(triggers), news_revision=None, served_news_revision=None,
        shocks=[], last_requested_at=last, bar_seconds=bar_seconds)


def test_v2_reviews_every_completed_minute_with_v1s_floor_and_near_target_exception():
    now = NOW.replace(second=5, microsecond=0)
    minute = cm.floor_time(now, 60)
    assert due(now, served=minute - timedelta(minutes=1),
               last=now - timedelta(seconds=61)) == (["BAR_1M"], False)
    assert due(now, served=minute, last=now - timedelta(seconds=61)) == ([], False)
    # The floor: never inside a minute of the last request, except near the target.
    assert due(now, served=minute - timedelta(minutes=1),
               last=now - timedelta(seconds=59)) == ([], False)
    assert due(now, served=minute - timedelta(minutes=1), last=now - timedelta(seconds=59),
               triggers=[("NEAR_TARGET", "111")]) == (["BAR_1M", "NEAR_TARGET"], True)
    # V1's default cadence is unchanged: a completed 15-minute bar, reason BAR_15M.
    quarter = cm.floor_time(now, 900)
    assert cm.due_reasons(now=now, opened_at=now - timedelta(hours=1),
                          served_bar_end=quarter - timedelta(minutes=15),
                          unserved_triggers=[], news_revision=None, served_news_revision=None,
                          shocks=[], last_requested_at=None) == (["BAR_15M"], False)
    assert due(now, served=quarter - timedelta(minutes=15), bar_seconds=900) == (
        ["BAR_15M"], False)
    assert cm.TIMEFRAMES["1Min"] == 60 and cm.BARS_1M == 60 and cm.REVIEW_HISTORY == 5


def test_a_minute_that_completes_while_a_review_is_in_flight_is_skipped():
    boundary = NOW.replace(second=0, microsecond=0)
    before, after = boundary - timedelta(seconds=3), boundary + timedelta(seconds=4)
    assert cm.in_flight_at(boundary, requested_at=before, decided_at=after)
    assert cm.in_flight_at(boundary, requested_at=before, decided_at=boundary)
    assert cm.in_flight_at(boundary, requested_at=before, decided_at=None)  # Undecided.
    assert not cm.in_flight_at(boundary, requested_at=before,
                               decided_at=boundary - timedelta(seconds=1))
    assert not cm.in_flight_at(boundary, requested_at=boundary, decided_at=after)
    assert not cm.in_flight_at(boundary, requested_at=None, decided_at=None)
    assert (cm.MINUTE_SKIPPED_EVENT, cm.REVIEW_SKIPPED_IN_FLIGHT) == (
        "MAINTENANCE_REVIEW_SKIPPED", "REVIEW_SKIPPED_IN_FLIGHT")


# --- JEV_MANAGED_POSITION_CONTEXT_V5 and QUESTIONS_V5 --------------------------------------------


def minute_bars(count=70, base="105.5"):
    return series(count, seconds=60, base=D(base), spread=D("0.1"))


def history(count=7):
    """Earlier maintenance reviews as ``trade_maintenance.review_history`` returns them."""
    items = []
    for i in range(count):
        items.append({
            "requested_at": (NOW - timedelta(minutes=count - i)).isoformat(),
            "trigger_reasons": ["BAR_1M"],
            "answers": {"action": {"choice": "HOLD", "top_p": "0.39"},
                        "trade_reason": {"choice": "INTACT", "top_p": "0.51"},
                        "stop_option": {"choice": "S1", "top_p": "0.49"},
                        "target_option": {"choice": "KEEP", "top_p": "0.41"}},
            "outcome": "HELD", "code": None,
            "option_prices": {"stop": {"S1": f"100.{i:02d}"}, "target": {"T1": "112.35"}},
        })
    items[-1]["answers"] = None  # A failed review: no answers.
    items[-1].update(outcome="FAILED", code="REVIEW_DEADLINE_EXCEEDED")
    return items


def small_inputs():
    from tests.test_maintenance_rules import changes, news

    return {**production_inputs(), "pick": pick(long=False), "news": news(1),
            "changes": changes(2)}


def test_the_v5_context_is_v4_plus_the_minute_bars_and_the_review_history():
    inputs = small_inputs()
    v4 = md.compile_maintenance_state(**inputs)
    v5 = md.compile_maintenance_state_v5(bars_1m=minute_bars(), history=history(), **inputs)
    assert v5.manifest["budget_steps"] == [] and v4.manifest["budget_steps"] == []
    state = v5.state
    assert state["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V5"
    rows = state["price_action"]["bars_1m"]
    assert len(rows["rows"]) == 60 and rows["omitted_rows"] == 0  # The last hour of 70.
    assert rows["columns"] == "end_age_minutes|open|high|low|close|volume"
    assert rows["rows"][-1].startswith("0|")  # The newest completed minute (30 s ago).
    assert rows["rows"][0].startswith("59|")
    shown = state["review_history"]
    assert len(shown) == 5 and state["review_history_omitted"] == 2  # The last five.
    assert [r["minutes_ago"] for r in shown] == [5, 4, 3, 2, 1]  # Oldest first.
    assert shown[0] == {"minutes_ago": 5, "trigger_reasons": ["BAR_1M"], "outcome": "HELD",
                        "code": None, "action": {"choice": "HOLD", "top_p": "0.39"},
                        "trade_reason": {"choice": "INTACT", "top_p": "0.51"},
                        "stop_option": {"choice": "S1", "top_p": "0.49", "price": "100.02"},
                        "target_option": {"choice": "KEEP", "top_p": "0.41"}}
    assert shown[-1]["outcome"] == "FAILED" and shown[-1]["action"] is None
    # Every V4 section is V4's, byte for byte.
    rest = {k: v for k, v in state.items() if k not in {
        "context_version", "review_history", "review_history_omitted", "price_action"}}
    assert encoded(rest) == encoded({k: v for k, v in v4.state.items() if k not in {
        "context_version", "price_action"}})
    assert {k: v for k, v in state["price_action"].items() if k != "bars_1m"} == (
        v4.state["price_action"])
    entries = {e["section"]: e for e in v5.manifest["entries"]}
    assert entries["price_action.bars_1m"]["kept_count"] == 60
    assert entries["review_history"]["total_count"] == 7
    assert v5.manifest["dossier_version"] == "MAINTENANCE_DOSSIER_V2"
    again = md.compile_maintenance_state_v5(bars_1m=minute_bars(), history=history(), **inputs)
    assert encoded(again.state) == encoded(state)  # Deterministic.


def test_the_v5_ladder_drops_the_oldest_minute_rows_first_and_never_an_option():
    inputs = production_inputs()  # Long reasoning, eight 1,200-character news items.
    compiled = md.compile_maintenance_state_v5(bars_1m=minute_bars(), history=history(),
                                               **inputs)
    steps = compiled.manifest["budget_steps"]
    assert steps[0] == "OMIT_1M_BAR" and encoded_bytes(compiled.state) <= md.STATE_BYTE_BUDGET
    assert compiled.manifest["budget_step_count"] >= 45  # All 45 droppable minute rows first.
    assert len(compiled.state["price_action"]["bars_1m"]["rows"]) == 15
    assert [o["price"] for o in compiled.state["options"]["stop"]] == [
        o["price"] for o in inputs["options"]["stop"]]
    with pytest.raises(ContextBudgetUnsatisfiable) as caught:
        md.compile_maintenance_state_v5(budget=4_000, bars_1m=minute_bars(), history=history(),
                                        **inputs)
    assert "OMIT_REVIEW_HISTORY" in caught.value.manifest["budget_steps"]
    kept = {e["section"]: e for e in caught.value.manifest["entries"]}["review_history"]
    assert kept["kept_count"] == 2  # The newest two always stay.


# The one sentence of the option questions V4 and the review's V1 that is not true under V2's
# rules, and what V5 and the review's V2 say instead (package answer-rules).
V1_OPTION_SENTENCE = ("All questions are independent; code refuses an inconsistent combination "
                      "and re-checks every level against the live price before applying it.")
V5_OPTION_SENTENCE = ("All questions are independent. Code uses an option only when your "
                      "maintenance action raises that level, and re-checks every level against "
                      "the live price before applying it.")
REVIEW_V2_OPTION_SENTENCE = ("All questions are independent. Code uses an option only when you "
                             "choose CONTINUE, and re-checks every level against the live price "
                             "before applying it.")


def context_for(policy, options, *, state=None):
    state = state or md.compile_maintenance_state(**{**small_inputs(), "options": options}).state
    data = {"context_version": policy["context_version"], "identity": {}, "state": state,
            "options": options, "policy": policy, "expires_at": NOW.isoformat()}
    raw = encoded(data)
    return ManagedContext(raw, digest(raw))


def test_the_v5_questions_are_v4s_with_the_minute_bars_and_the_history_named():
    options = production_inputs()["options"]
    v4 = md.questions_for(context_for(V1_MAINTENANCE, options))
    v5 = md.questions_for(context_for(V2_MAINTENANCE, options))
    assert (v4.version, v5.version) == ("JEV_MANAGED_POSITION_QUESTIONS_V4",
                                        "JEV_MANAGED_POSITION_QUESTIONS_V5")
    assert v4.template_hash == md.maintenance_questions(context_for(V1_MAINTENANCE,
                                                                    options)).template_hash
    q4, q5 = v4.questions, v5.questions
    assert set(q4) == set(q5) and all(q4[k]["criteria"] == q5[k]["criteria"] for k in q4)
    assert q4["action"] == q5["action"]
    text = q5["trade_reason"]["instructions"]
    assert "`price_action.bars_1m`" in text and "`review_history`" in text
    assert "`price_action.bars_1m`" not in q4["trade_reason"]["instructions"]
    # The option questions differ from V4's by exactly one sentence: V2's rule, not V1's.
    for kind in ("stop_option", "target_option"):
        old = q4[kind]["instructions"]
        assert old.count(V1_OPTION_SENTENCE) == 1 and old.endswith(V1_OPTION_SENTENCE)
        assert q5[kind]["instructions"] == old.replace(V1_OPTION_SENTENCE, V5_OPTION_SENTENCE)
        assert "inconsistent" not in q5[kind]["instructions"]
    # The pin: with no options offered the template is exactly this; a text change is a new
    # question version.
    assert md.QUESTIONS_V5_TEMPLATE_SHA256 == md.questions_v5_template_hash() == (
        "412aa2363d907b50c646a9b593f27b4ffc4f99811904f4a68302b6bc7cce0233")


def test_the_recorded_version_picks_its_context():
    inputs = small_inputs()
    v4 = md.compile_state_for(cm.CRYPTO_MAINTENANCE, bars_1m=minute_bars(), history=history(),
                              **inputs)
    assert v4.state["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V4"
    assert "bars_1m" not in v4.state["price_action"] and "review_history" not in v4.state
    assert encoded(v4.state) == encoded(md.compile_maintenance_state(**inputs).state)
    v5 = md.compile_state_for(cm.CRYPTO_MAINTENANCE_V2, bars_1m=minute_bars(), history=[],
                              **inputs)
    assert v5.state["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V5"
    assert v5.state["review_history"] == [] and v5.state["review_history_omitted"] == 0


# --- JEV_DAY_REVIEW_CONTEXT_V2 and QUESTIONS_V2 --------------------------------------------------


def review_inputs():
    inputs = small_inputs()
    return {**inputs, "review": {"round": "FIRST", "review_number": 1, "continuations": 0,
                                 "hours_in_trade": 24, "agent_answer_status": "NO_ANSWER"},
            "agent_answer": None, "discussion": None,
            "trigger": {"reasons": ["DAY_REVIEW"], "review_number": 1, "round": "FIRST"}}


def test_the_day_review_context_v2_is_v1_plus_the_review_history():
    v1 = drd.compile_day_review_state(**review_inputs())
    v2 = drd.compile_day_review_state(context_version="JEV_DAY_REVIEW_CONTEXT_V2",
                                      history=history(), **review_inputs())
    assert v1.state["context_version"] == "JEV_DAY_REVIEW_CONTEXT_V1"
    assert "review_history" not in v1.state
    assert v2.state["context_version"] == "JEV_DAY_REVIEW_CONTEXT_V2"
    assert len(v2.state["review_history"]) == 5 and v2.state["review_history_omitted"] == 2
    rest = {k: v for k, v in v2.state.items()
            if k not in {"context_version", "review_history", "review_history_omitted"}}
    assert encoded(rest) == encoded({k: v for k, v in v1.state.items()
                                     if k != "context_version"})
    assert "review_history" in {e["section"] for e in v2.manifest["entries"]}
    for bad in ({"context_version": "JEV_DAY_REVIEW_CONTEXT_V2"},
                {"history": history()}, {"context_version": "JEV_DAY_REVIEW_CONTEXT_V3",
                                         "history": []}):
        with pytest.raises(ValueError, match="DAY_REVIEW_CONTEXT_VERSION_INVALID"):
            drd.compile_day_review_state(**bad, **review_inputs())


def test_the_day_review_questions_v2_name_the_history_and_are_pinned():
    options = production_inputs()["options"]
    for status in ("ANSWERED", "NO_ANSWER"):
        state = {"as_of": NOW.isoformat(),
                 "trade": {"bid": "106.02", "entry": "100.10", "risk_per_coin": "5.10"},
                 "review": {"agent_answer_status": status}}
        v1 = drd.review_questions_for(context_for(V1_REVIEW, options, state=state))
        v2 = drd.review_questions_for(context_for(V2_REVIEW, options, state=state))
        assert (v1.version, v2.version) == ("JEV_DAY_REVIEW_QUESTIONS_V1",
                                            "JEV_DAY_REVIEW_QUESTIONS_V2")
        q1, q2 = v1.questions, v2.questions
        assert set(q1) == set(q2) and all(q1[k]["criteria"] == q2[k]["criteria"] for k in q1)
        assert all(q1[k] == q2[k] for k in q1
                   if k not in {"trade_reason", "stop_option", "target_option"})
        assert "`review_history`" in q2["trade_reason"]["instructions"]
        assert "`review_history`" not in q1["trade_reason"]["instructions"]
        # The option questions differ from V1's by exactly one sentence: V2's rule.
        for kind in ("stop_option", "target_option"):
            old = q1[kind]["instructions"]
            assert old.count(V1_OPTION_SENTENCE) == 1
            assert q2[kind]["instructions"] == old.replace(V1_OPTION_SENTENCE,
                                                           REVIEW_V2_OPTION_SENTENCE)
            assert "inconsistent" not in q2[kind]["instructions"]
    assert drd.QUESTIONS_V2_TEMPLATE_SHA256 == drd.questions_v2_template_hashes() == {
        "ANSWERED": "e898940cd0bd5d1a6d94b623523075cad3fca6a1e621b5d9911e462e2b08d90c",
        "NO_ANSWER": "89cf92cb531a15890f17bc15f8a56b8c72affcb5b72406e05d3c27a8647f0fb4"}
    # The early-exit questions are the same under both versions.
    assert drd.EARLY_EXIT_QUESTIONS.version == "JEV_EARLY_EXIT_QUESTIONS_V1"
