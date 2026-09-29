"""The 24-hour review (plan 4.6.4) on the fixture venue (package day-review).

CRYPTO_24H_REVIEW_V1 for the maintained arm: the request at T - 30 min, the agent's answer over
its own route, Jev at T, agreement, one discussion round, the timeouts, continue (a new 24-hour
plan, levels through phase 5's checks) and exit (a market sell), hard exits, restarts, agent
routing, and the control arm and older setups unchanged.

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars, a
mock Jev transport and the real agent routes in process. No broker, provider, network or
owner-ledger contact.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_holding
from catalyst_lab import day_review as dr
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_ops import day_review_alarms, status_alarms
from tests.day_review_fixtures import (
    AGENTS,
    LEGACY,
    STATUS,
    answer,
    answer_body,
    at_request,
    at_review,
    bearer,
    decisions,
    events,
    kit,
    managed_arm,
    market_sells,
    move_to,
    open_trade,
    pending,
    post_news,
    review_at,
    run,
    sell_to_close,
    source,
    state,
    stop_orders,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import quote
from tests.maintenance_fixtures import v1_admission as v1_admission
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

pytestmark = pytest.mark.usefixtures("managed_arm")
_ = managed_arm


def decision(engine, sid):
    [body] = events(engine, dr.DECISION, sid)
    return body


def the_request(engine, sid):
    [body] = events(engine, dr.REQUESTED, sid)
    return body


# --- The request at T - 30 minutes ---------------------------------------------------------------


def test_the_request_reaches_the_proposing_agent_thirty_minutes_before_t(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    first_fill = datetime.fromisoformat(state(mt, sid)["opened_at"])
    assert review_at(mt, sid) == first_fill + timedelta(hours=24)
    post_news(mt, sid, "Synthetic fixture: the coin's main exchange listed a new pair.",
              stance="SUPPORTS")
    at_request(mt, sid, seconds=-2)  # Not yet 30 minutes before T.
    run(mt, k, bid="106")
    assert events(engine, dr.REQUESTED, sid) == [] and pending(k) == []
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    assert request["review_number"] == 1 and request["continuations"] == 0
    assert request["addressee"] == {"agent_id": "claude", "basis": "PROPOSING_AGENT"}
    assert request["review_at"] == review_at(mt, sid).isoformat()
    assert request["agent_answer_due_at"] == request["review_at"]
    trade = request["trade"]
    assert (trade["stop"], trade["target"], trade["bid"]) == ("95", "111", "106")
    assert D(trade["entry"]) == D("100.10") and D(trade["pnl_r"]) == D("1.16")
    assert [n["excerpt"] for n in request["news_since_entry"]] == [
        "Synthetic fixture: the coin's main exchange listed a new pair."]
    assert request["options"]["stop"][0]["option_id"] == "S1"
    assert request["original_pick"]["thesis"].startswith("THESIS-00")
    [item] = pending(k)
    assert (item["kind"], item["review_id"], item["round"]) == (
        "DAY_REVIEW", request["review_id"], "FIRST")
    assert item["answer_due_at"] == request["review_at"]
    assert item["answer_route"] == f"/api/v1/lab/reviews/{request['review_id']}/answer"
    # Another agent's token, the status token: nothing to see, nothing to answer.
    assert pending(k, AGENTS["instinct"]) == [] and pending(k, STATUS) == []
    refused = answer(k, request["review_id"], answer_body(), AGENTS["instinct"])
    assert (refused.status_code, refused.json()["detail"]) == (403, "AGENT_IDENTITY_MISMATCH")
    assert k.client.post(f"/api/v1/lab/reviews/{request['review_id']}/answer",
                         json=answer_body(), headers=bearer(STATUS)).status_code == 403
    assert answer(k, request["review_id"], answer_body(), LEGACY).status_code == 403
    run(mt, k, bid="106")
    assert len(events(engine, dr.REQUESTED, sid)) == 1  # Once per review.


# --- Agreement ---------------------------------------------------------------------------------


def test_agent_and_jev_agree_to_continue_and_the_raised_levels_start_a_new_24_hour_plan(
        mt, v1_admission):
    # CRYPTO_24H_REVIEW_V1 (context V1); V2's continue: tests/test_answer_rules_flows.py.
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(decision="CONTINUE", stop_option="first", target_option="first")
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    reply = answer(k, request["review_id"], answer_body(
        suggested_stop="103.10", suggested_target="112.50",
        sources=[source(mt, "Synthetic fixture: exchange inflows fell for a second day.",
                        stance="SUPPORTS")]))
    assert reply.status_code == 200, reply.text
    ack = reply.json()
    assert (ack["status"], ack["round"], ack["decision"], ack["trade_authorized"]) == (
        "REVIEW_ANSWER_RECORDED", "FIRST", "CONTINUE", False)
    [recorded] = events(engine, dr.AGENT_ANSWER, sid)
    assert recorded["agent_id"] == "claude" and recorded["answer"]["suggested_stop"] == "103.10"
    assert pending(k) == []  # Answered.
    venue.now += timedelta(minutes=10)
    run(mt, k, bid="106")
    assert k.jev.of("DAY_REVIEW") == []  # Jev answers at T, not before.
    at_review(mt, sid)
    engine.manage(sid, quote(mt, "106"))
    run(mt, k, bid="106")
    [sent] = k.jev.of("DAY_REVIEW")
    text = json.dumps(sent)
    assert sent["state"]["context_version"] == "JEV_DAY_REVIEW_CONTEXT_V1"
    assert set(sent["questions"]) == {"trade_reason", "agent_case", "decision", "stop_option",
                                      "target_option"}
    agent_answer = sent["state"]["agent_answer"]
    assert (agent_answer["decision"], agent_answer["suggested_stop"]) == ("CONTINUE", "103.10")
    assert agent_answer["sources"][0]["excerpt"].startswith("Synthetic fixture: exchange")
    # Jev never sees the agent's identity or confidence.
    assert "claude" not in text.lower() and "confidence" not in text.lower()
    assert sent["state"]["review"] == {"round": "FIRST", "review_number": 1,
                                       "continuations": 0, "hours_in_trade": 24,
                                       "agent_answer_status": "ANSWERED"}
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["level_change"]) == (
        "CONTINUE", "AGREED", "APPLIED")
    assert body["stop"] == {"old": "95", "new": "103.00", "option_id": "S1",
                            "bases": ["SWING_LOW_15M"]}
    assert body["target"]["new"] == "112.35" and body["levels_after"] == {
        "stop": "103.00", "target": "112.35"}
    assert body["agent"]["answers"]["FIRST"]["decision"] == "CONTINUE"
    assert body["jev"]["results"][0]["receipt_ids"]
    measurement = body["measurement"]
    assert (measurement["change_kind"], measurement["old_stop"], measurement["new_stop"],
            measurement["old_target"], measurement["new_target"],
            measurement["actually_exited"], measurement["continuation_decision"]) == (
        "CONTINUE_EXIT_DECISION", "95", "103.00", "111", "112.35", False, "CONTINUE")
    assert measurement["quote"]["bid"] == "106" and measurement["quote"]["ask"] == "106.03"
    current = state(mt, sid)
    next_at = datetime.fromisoformat(body["review_at"]) + timedelta(hours=24)
    assert current["continuations"] == 1 and current["day_review_at"] == next_at.isoformat()
    assert current["hard_exit_at"] == (next_at + timedelta(minutes=80)).isoformat()
    assert current["stop_replace"]["change_id"] == request["review_id"]
    assert market_sells(mt, "SOL/USD") == []
    # The protection loop replaces the resting stop-limit under its own authorization.
    [old] = stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "106"))
    [amend] = decisions(engine, sid, "AMEND")
    assert (amend["method"], amend["reason"], amend["claimed"]) == ("PATCH", "REPLACE_STOP", True)
    assert amend["payload"] == {"stop_price": "103.00", "limit_price": "102.99"}
    engine.manage(sid, quote(mt, "106"))
    assert not state(mt, sid).get("stop_replace")
    # The fail-safe moved with T: no exit at the old one.
    move_to(mt, datetime.fromisoformat(body["review_at"]) + timedelta(minutes=81))
    engine.manage(sid, quote(mt, "106"))
    assert not state(mt, sid).get("exit_requested")
    # The next review: T + 24 hours, review number 2, the continuation recorded.
    at_request(mt, sid)
    run(mt, k, bid="106")
    second = events(engine, dr.REQUESTED, sid)[-1]
    assert (second["review_number"], second["continuations"]) == (2, 1)
    assert second["review_at"] == next_at.isoformat()
    assert verify_events(engine.repo.export_events())["valid"]


def test_agent_and_jev_agree_to_exit_and_the_trade_sells_at_market(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(decision="EXIT", trade_reason="WEAKENED")
    at_request(mt, sid)
    run(mt, k, bid="104")
    request = the_request(engine, sid)
    assert answer(k, request["review_id"], answer_body("EXIT")).status_code == 200
    at_review(mt, sid)
    run(mt, k, bid="104")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["exit_requested"]) == (
        "EXIT", "AGREED", "DAY_REVIEW_EXIT")
    assert body["measurement"]["actually_exited"] is True
    assert body["measurement"]["new_stop"] is None and body["measurement"]["new_target"] is None
    assert state(mt, sid)["exit_requested"] == "DAY_REVIEW_EXIT"
    close = sell_to_close(mt, sid, "SOL/USD", "104")
    final = state(mt, sid)
    assert (final["state"], final["reason"]) == ("CLOSED", "DAY_REVIEW_EXIT")
    [exit_decision] = decisions(engine, sid, "EXIT")
    assert exit_decision["claimed"] and exit_decision["payload"]["client_order_id"] == close[
        "client_order_id"]
    assert all(d["claimed"] for d in decisions(engine, sid, "CANCEL"))
    run(mt, k, bid="104")  # Nothing more: one decision, one sale.
    assert len(events(engine, dr.DECISION, sid)) == 1 and len(market_sells(mt, "SOL/USD")) == 1


# --- The discussion round ------------------------------------------------------------------------


def disagree(mt, k, sid, *, agent="CONTINUE", jev="EXIT"):
    k.jev.answer(decision=jev, trade_reason="WEAKENED" if jev == "EXIT" else "INTACT",
                 agent_case="DOES_NOT_HOLD")
    at_request(mt, sid)
    run(mt, k, bid="104")
    request = the_request(mt[0], sid)
    assert answer(k, request["review_id"], answer_body(agent)).status_code == 200
    at_review(mt, sid)
    run(mt, k, bid="104")
    return request


def test_disagreement_opens_one_discussion_round_and_an_agreement_then_continues(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    request = disagree(mt, k, sid)
    assert events(engine, dr.DECISION, sid) == []
    [opened] = events(engine, dr.DISCUSSION_OPENED, sid)
    assert opened["reply_due_at"] == (venue.now + timedelta(minutes=15)).isoformat()
    [item] = pending(k)
    assert (item["round"], item["answer_due_at"]) == ("DISCUSSION", opened["reply_due_at"])
    assert item["jev_first_answer"]["answers"]["decision"]["choice"] == "EXIT"
    assert item["jev_first_answer"]["meanings"]["decision"].startswith("Exit now")
    assert item["your_first_answer"]["decision"] == "CONTINUE"
    k.jev.final = {"trade_reason": "INTACT", "agent_case": "HOLDS", "decision": "CONTINUE",
                   "stop_option": "KEEP", "target_option": "KEEP"}
    venue.now += timedelta(minutes=5)
    reply = answer(k, request["review_id"], answer_body(
        what_changed="Synthetic fixture: the drop was one exchange's outage, now resolved."))
    assert reply.status_code == 200 and reply.json()["round"] == "DISCUSSION"
    run(mt, k, bid="104")
    first, final = k.jev.of("DAY_REVIEW")
    assert final["state"]["review"]["round"] == "FINAL"
    discussion = final["state"]["discussion"]
    assert discussion["your_first_answer"]["decision"]["choice"] == "EXIT"
    assert discussion["agent_reply"]["what_changed"].startswith("Synthetic fixture: the drop")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("CONTINUE", "AGREED_AFTER_DISCUSSION")
    assert [r["round"] for r in body["jev"]["results"]] == ["FIRST", "FINAL"]
    assert sorted(body["agent"]["answers"]) == ["DISCUSSION", "FIRST"]
    assert state(mt, sid)["continuations"] == 1


def test_still_disagreeing_after_the_discussion_exits(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    request = disagree(mt, k, sid)
    assert answer(k, request["review_id"], answer_body()).status_code == 200
    run(mt, k, bid="104")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "DISAGREED_AFTER_DISCUSSION")
    assert state(mt, sid)["exit_requested"] == "DAY_REVIEW_EXIT"


def test_an_agent_silent_in_the_discussion_round_exits_after_fifteen_minutes(mt):
    engine, venue, _ = mt
    sid = open_trade(mt, bid="104")
    k = kit(mt)
    request = disagree(mt, k, sid, agent="EXIT", jev="CONTINUE")
    [opened] = events(engine, dr.DISCUSSION_OPENED, sid)
    move_to(mt, datetime.fromisoformat(opened["reply_due_at"]) - timedelta(seconds=1))
    run(mt, k, bid="104")
    assert events(engine, dr.DECISION, sid) == []
    move_to(mt, datetime.fromisoformat(opened["reply_due_at"]))
    late = answer(k, request["review_id"], answer_body("EXIT"))
    assert (late.status_code, late.json()["detail"]) == (409, "REVIEW_ANSWER_WINDOW_CLOSED")
    run(mt, k, bid="104")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "DISCUSSION_NO_AGENT_REPLY")
    assert len(k.jev.of("DAY_REVIEW")) == 1  # No final Jev call without a reply.


# --- Timeouts ------------------------------------------------------------------------------------


def test_a_silent_agent_leaves_the_decision_to_jev_alone(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    at_review(mt, sid, seconds=0)
    late = answer(k, request["review_id"], answer_body())
    assert (late.status_code, late.json()["detail"]) == (409, "REVIEW_ANSWER_WINDOW_CLOSED")
    run(mt, k, bid="106")
    [sent] = k.jev.of("DAY_REVIEW")
    assert "agent_case" not in sent["questions"]  # Nothing of the agent's to judge.
    assert sent["state"]["agent_answer"] is None
    assert sent["state"]["review"]["agent_answer_status"] == "NO_ANSWER"
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("CONTINUE", "AGENT_SILENT_JEV_ALONE")
    assert body["agent"]["answers"] == {}


def test_jev_down_for_thirty_minutes_exits_and_raises_the_alarm_meanwhile(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.status = {"DAY_REVIEW": 503}
    at_request(mt, sid)
    run(mt, k, bid="106")
    at_review(mt, sid)
    t = review_at(mt, sid)
    run(mt, k, bid="106")
    results = events(engine, dr.JEV_RESULT, sid)
    assert [(r["status"], r["code"]) for r in results] == [("FAILED", "HTTP_503")]
    status = k.day.status(engine.store.active())
    assert [f["code"] for f in status["failing_reviews"]] == ["HTTP_503"]
    assert status["reviews_in_progress"][0]["phase"] == "AWAITING_JEV"
    assert day_review_alarms(status) == ["DAY_REVIEW_JEV_FAILING"]
    assert "DAY_REVIEW_JEV_FAILING" in status_alarms({"day_reviews": status}, venue.now, {
        "tick_max_age_seconds": 1, "reconciliation_max_age_seconds": 1,
        "research_max_age_seconds": 1})
    venue.now += timedelta(seconds=30)
    run(mt, k, bid="106")
    assert len(k.jev.of("DAY_REVIEW")) == 1  # Retried after a minute, not every pass.
    venue.now = t + timedelta(minutes=2)
    run(mt, k, bid="106")
    assert len(k.jev.of("DAY_REVIEW")) == 2
    move_to(mt, t + timedelta(minutes=30) - timedelta(seconds=1))
    run(mt, k, bid="106")
    assert events(engine, dr.DECISION, sid) == []
    move_to(mt, t + timedelta(minutes=30))
    run(mt, k, bid="106")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "JEV_UNAVAILABLE")
    assert state(mt, sid)["exit_requested"] == "DAY_REVIEW_EXIT"
    assert day_review_alarms(k.day.status(engine.store.active())) == []


@pytest.mark.parametrize("labels,code,v1_only", [
    ({"decision": "CONTINUE", "trade_reason": "BROKEN"}, "CONTRADICTORY_REVIEW_ANSWERS", False),
    # V1's rule only: under CRYPTO_24H_REVIEW_V2 (package answer-rules) EXIT ignores the option
    # answers (tests/test_answer_rules_flows.py).
    ({"decision": "EXIT", "stop_option": "first"}, "CONTRADICTORY_REVIEW_ANSWERS", True),
    ({"decision": "Insufficient evidence"}, "UNCERTAIN_JUDGMENT", False),
])
def test_an_answer_code_cannot_use_exits(mt, request, labels, code, v1_only):
    if v1_only:
        request.getfixturevalue("v1_admission")
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(**labels)
    at_request(mt, sid)
    run(mt, k, bid="106")
    at_review(mt, sid)
    run(mt, k, bid="106")
    [result] = events(engine, dr.JEV_RESULT, sid)
    assert (result["status"], result["code"]) == ("UNUSABLE", code)
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "JEV_ANSWER_UNUSABLE")


def test_the_fail_safe_exits_when_no_review_decides_and_never_before(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    t = review_at(mt, sid)
    move_to(mt, t)  # No forced exit at 24 hours under the review.
    engine.manage(sid, quote(mt, "106"))
    assert not state(mt, sid).get("exit_requested")
    move_to(mt, t + timedelta(minutes=80) - timedelta(seconds=1))
    engine.manage(sid, quote(mt, "106"))
    assert not state(mt, sid).get("exit_requested")
    move_to(mt, t + timedelta(minutes=80))
    sell_to_close(mt, sid, "SOL/USD", "106")
    final = state(mt, sid)
    assert (final["state"], final["reason"]) == ("CLOSED", "DAY_REVIEW_DEADLINE_EXIT")
    assert [d["reason"] for d in decisions(engine, sid, "EXIT")] == ["DAY_REVIEW_DEADLINE_EXIT"]


# --- Level checks on a continue ------------------------------------------------------------------


def test_a_level_change_the_checks_refuse_keeps_the_levels_and_the_trade_continues(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(stop_option="first")  # S1 = 103.00 with the bid at 106.

    def dropped(kind, body):
        if kind == "DAY_REVIEW":  # The bid touches the new stop while Jev answers.
            venue.now += timedelta(seconds=2)
            engine.manage(sid, quote(mt, "102.90"))

    k.jev.hook = dropped
    at_request(mt, sid)
    run(mt, k, bid="106")
    at_review(mt, sid)
    run(mt, k, bid="106")
    body = decision(engine, sid)
    assert (body["outcome"], body["level_change"], body["level_change_code"]) == (
        "CONTINUE", "REFUSED", "STOP_LEVEL_CROSSED")
    assert body["levels_after"] == {"stop": "95", "target": "111"}
    assert body["measurement"]["new_stop"] is None
    assert state(mt, sid)["continuations"] == 1 and not state(mt, sid).get("stop_replace")


def test_each_continue_counts_and_moves_t_and_the_fail_safe_24_hours_on(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    first_t = review_at(mt, sid)
    for number in (1, 2):
        at_request(mt, sid)
        run(mt, k, bid="106")
        at_review(mt, sid)
        run(mt, k, bid="106")  # The agent stays silent; Jev continues alone.
        body = events(engine, dr.DECISION, sid)[-1]
        assert (body["review_number"], body["outcome"], body["continuations_after"]) == (
            number, "CONTINUE", number)
    current = state(mt, sid)
    assert current["continuations"] == 2
    assert current["day_review_at"] == (first_t + timedelta(hours=48)).isoformat()
    assert current["hard_exit_at"] == (first_t + timedelta(hours=48, minutes=80)).isoformat()
    hold = crypto_holding.recorded_hold_policy(current)
    first_fill = datetime.fromisoformat(current["opened_at"])
    assert hold.exit_at(first_fill, 2).isoformat() == current["hard_exit_at"]
    engine.manage(sid, quote(mt, "106"))  # The protection loop keeps the moved clock.
    assert state(mt, sid)["hard_exit_at"] == current["hard_exit_at"]


# --- Hard exits (plan 4.6.5) and closed trades ---------------------------------------------------


def hit_stop(mt, sid):
    engine, venue, _ = mt
    [stop] = stop_orders(mt, "SOL/USD")
    engine.ingest(venue.fill(stop["id"], stop["qty"], price="94.99"))
    engine.manage(sid, quote(mt, "94.50"))
    assert state(mt, sid)["state"] == "CLOSED"


@pytest.mark.parametrize("hard_exit,code", [
    ("TARGET", "EXIT_IN_PROGRESS_DURING_REVIEW"),
    ("STOP", "POSITION_CLOSED_DURING_REVIEW"),
    ("DAILY_RISK_HALT", "EXIT_IN_PROGRESS_DURING_REVIEW"),
    ("OPERATOR_FLATTEN", "EXIT_IN_PROGRESS_DURING_REVIEW"),
])
def test_a_hard_exit_discards_the_pending_review(mt, hard_exit, code):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    assert answer(k, request["review_id"], answer_body()).status_code == 200
    if hard_exit == "TARGET":
        engine.manage(sid, quote(mt, "111"))  # The bid reached the target.
        assert state(mt, sid)["exit_requested"] == "TARGET_EXIT"
    elif hard_exit == "STOP":
        hit_stop(mt, sid)
    else:  # The account-safety tick's own transition (daily halt, operator flatten).
        with engine.store.transaction() as conn:
            engine.store.transition(conn, sid, "OPEN", exit_requested=hard_exit)
    run(mt, k, bid="106")
    at_review(mt, sid)
    run(mt, k, bid="106")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["measurement"]) == ("DISCARDED", code, None)
    assert k.jev.of("DAY_REVIEW") == [] and pending(k) == []
    late = answer(k, request["review_id"], answer_body())
    assert late.status_code in {404, 409}
    assert state(mt, sid).get("exit_requested") != "DAY_REVIEW_EXIT"


def test_a_trade_closed_while_jev_answers_is_discarded(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.hook = lambda kind, body: hit_stop(mt, sid) if kind == "DAY_REVIEW" else None
    at_request(mt, sid)
    run(mt, k, bid="106")
    at_review(mt, sid)
    run(mt, k, bid="106")
    run(mt, k, bid="106")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("DISCARDED", "POSITION_CLOSED_DURING_REVIEW")
    assert state(mt, sid)["reason"] != "DAY_REVIEW_EXIT"


# --- Restarts: resume from the ledger, never a second vote or a second sale ----------------------


def test_a_restart_after_jev_answered_uses_the_recorded_receipt(mt, monkeypatch):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(decision="EXIT", trade_reason="WEAKENED")
    at_request(mt, sid)
    run(mt, k, bid="104")
    at_review(mt, sid)

    def crash(*args, **kwargs):
        raise RuntimeError("fixture crash after the provider answered")

    monkeypatch.setattr(k.day, "_record_result", crash)
    run(mt, k, bid="104")
    assert len(events(engine, dr.JEV_REQUEST, sid)) == 1
    assert events(engine, dr.JEV_RESULT, sid) == [] and events(engine, dr.DECISION, sid) == []
    restarted = kit(mt, jev=k.jev)  # A new process: same ledger, nothing in memory.
    run(mt, restarted, bid="104")
    run(mt, restarted, bid="104")
    assert len(k.jev.of("DAY_REVIEW")) == 1  # The recorded answer; never a second vote.
    [result] = events(engine, dr.JEV_RESULT, sid)
    assert result["status"] == "ANSWERED" and result["answer"]["decision"] == "EXIT"
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "AGENT_SILENT_JEV_ALONE")
    sell_to_close(mt, sid, "SOL/USD", "104")
    again = kit(mt, jev=k.jev)  # Another restart after the sale.
    run(mt, again, bid="104")
    assert len(market_sells(mt, "SOL/USD")) == 1 and len(events(engine, dr.DECISION, sid)) == 1
    assert state(mt, sid)["reason"] == "DAY_REVIEW_EXIT"


def test_a_restart_before_the_provider_call_retries_once_the_attempt_lapsed(mt, monkeypatch):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    at_request(mt, sid)
    run(mt, k, bid="106")
    at_review(mt, sid)

    async def crash(**kwargs):
        raise RuntimeError("fixture crash before the provider call")

    monkeypatch.setattr(k.reviewer, "jev_review", crash)
    run(mt, k, bid="106")
    [asked] = events(engine, dr.JEV_REQUEST, sid)
    restarted = kit(mt, jev=k.jev)
    run(mt, restarted, bid="106")
    assert events(engine, dr.JEV_RESULT, sid) == []  # Its deadline has not passed yet.
    expires = datetime.fromisoformat(asked["expires_at"])
    move_to(mt, expires + timedelta(seconds=5))
    run(mt, restarted, bid="106")
    [lapsed] = events(engine, dr.JEV_RESULT, sid)
    assert (lapsed["status"], lapsed["code"]) == ("FAILED", "REQUEST_INTERRUPTED")
    move_to(mt, venue.now + timedelta(seconds=60))
    run(mt, restarted, bid="106")
    assert len(k.jev.of("DAY_REVIEW")) == 1
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("CONTINUE", "AGENT_SILENT_JEV_ALONE")


# --- Who answers (plan 4.6.7) --------------------------------------------------------------------


def test_the_active_research_agent_answers_when_the_proposing_agent_is_gone(mt):
    from tests.day_review_fixtures import publish

    engine, venue, _ = mt
    sid = open_trade(mt)
    publish(mt, ["ETH/USD"], agent_id="instinct")  # Instinct's report arrived last.
    k = kit(mt, agents=frozenset({"instinct", "muse"}))  # Claude's credential is gone.
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    assert request["addressee"] == {"agent_id": "instinct", "basis": "ACTIVE_RESEARCH_AGENT"}
    assert pending(k) == []
    [item] = pending(k, AGENTS["instinct"])
    assert item["review_id"] == request["review_id"]
    assert "addressee" not in item["request"]
    assert answer(k, request["review_id"], answer_body(), AGENTS["instinct"]).status_code == 200
    at_review(mt, sid)
    run(mt, k, bid="106")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("CONTINUE", "AGREED")
    assert body["agent"]["answers"]["FIRST"]["agent_id"] == "instinct"


def test_with_no_research_agent_jev_decides_alone(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt, agents=frozenset())
    k.jev.answer(decision="EXIT", trade_reason="WEAKENED")
    at_request(mt, sid)
    run(mt, k, bid="106")
    assert the_request(engine, sid)["addressee"] == {"agent_id": None,
                                                     "basis": "NO_ACTIVE_AGENT"}
    assert pending(k) == [] and pending(k, LEGACY) == []
    at_review(mt, sid)
    run(mt, k, bid="106")
    [sent] = k.jev.of("DAY_REVIEW")
    assert sent["state"]["review"]["agent_answer_status"] == "NO_AGENT"
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "NO_AGENT_JEV_ALONE")


def test_reviews_disabled_asks_jev_nothing_and_the_trade_exits_at_t(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt, reviews="DISABLED")
    at_request(mt, sid)
    run(mt, k, bid="106")
    assert the_request(engine, sid)["review_number"] == 1  # The agent is still asked.
    at_review(mt, sid)
    run(mt, k, bid="106")
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "JEV_REVIEWS_DISABLED")
    assert k.jev.calls == []


# --- The control arm and every older setup keep today's behaviour --------------------------------


def test_the_control_arm_and_older_setups_are_never_reviewed(mt, monkeypatch):
    from tests.test_managed_execution import observation, packet

    engine, venue, _ = mt
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    control = open_trade(mt)
    assert state(mt, control)["holding_policy"] == crypto_holding.CRYPTO_24H_HOLD.record()
    assert "day_review_at" not in state(mt, control) and "continuations" not in state(mt, control)
    older = engine.admit(packet(mt, "OLD/USD"))  # A report-V2 crypto pick: no holding policy.
    assert engine.observe_trigger(older, observation(mt))["outcome"] == "APPROVED"
    order = next(o for o in venue.orders_of("buy") if o["symbol"] == "OLD/USD")
    engine.ingest(venue.fill(order["id"], order["qty"], price="100"))
    engine.manage(older, observation(mt))
    assert "holding_policy" not in state(mt, older)
    k = kit(mt)
    t = datetime.fromisoformat(state(mt, control)["hard_exit_at"])
    for at in (t - timedelta(minutes=29), t - timedelta(seconds=1)):
        move_to(mt, at)
        run(mt, k, bid="106")
    assert events(engine, dr.REQUESTED) == [] and k.jev.calls == []
    assert k.day.status(engine.store.active())["open_trades"] == 0
    move_to(mt, t)
    engine.manage(control, quote(mt, "106"))
    assert state(mt, control)["exit_requested"] == "HOLD_24H_EXIT"  # The fixed 24-hour exit.
    assert events(engine, dr.DECISION) == []


# --- What the agent reads: the research context and the size of Jev's input -------------------


def test_the_research_context_lists_the_callers_pending_reviews(mt):
    from fastapi.testclient import TestClient

    from catalyst_lab.managed_service import create_managed_app
    from tests.day_review_fixtures import OPERATOR
    from tests.test_research_context import Feeds, service_for

    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    at_request(mt, sid)
    run(mt, k, bid="106")
    feeds = Feeds(lambda: venue.now)
    context, _ = service_for(engine.repo, lambda: venue.now, feeds)
    context.reviews = k.service
    client = TestClient(create_managed_app(
        _cycle(mt, k), engine.store, api_token=LEGACY, runtime_status=lambda: {},
        status_token=STATUS,
        operator_token=OPERATOR, agent_tokens=AGENTS, research_context=context,
        trade_reviews=k.service))
    mine = client.get("/api/v1/lab/research-context", headers=bearer(AGENTS["claude"])).json()
    [item] = mine["pending_reviews"]
    assert (item["kind"], item["setup_id"], item["round"]) == ("DAY_REVIEW", str(sid), "FIRST")
    [trade] = mine["open_trades"]
    assert trade["review_at"] == review_at(mt, sid).isoformat() and trade["continuations"] == 0
    theirs = client.get("/api/v1/lab/research-context",
                        headers=bearer(AGENTS["instinct"])).json()
    assert theirs["pending_reviews"] == []


def _cycle(mt, k):
    from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle

    return ResearchCycle(mt[0].repo, k.reviewer, CyclePolicy(10, 10, 15, 60, 30),
                         clock=lambda: mt[1].now)


def test_jevs_input_stays_under_the_budget_with_production_size_news_and_answers(mt):
    from catalyst_lab.managed_dossier import encoded_bytes

    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    for batch in range(2):
        for i in range(4):
            post_news(mt, sid, (f"Synthetic fixture item {batch}-{i}: " + "exchange flows and "
                                "funding shifted while the listing news spread. " * 30)[:1000],
                      stance="ADVERSE" if i % 2 else "SUPPORTS")
            venue.now += timedelta(seconds=1)
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    long = "Synthetic fixture: " + "the hourly structure held while volume rose. " * 13
    body = answer_body(what_changed=long[:600], next_24h=long[:600], proves_wrong=long[:400],
                       suggested_stop="103.10", suggested_target="112.50",
                       sources=[source(mt, (f"Synthetic fixture source {i}: " + "flows " * 250)
                                       [:1000]) for i in range(6)])
    assert answer(k, request["review_id"], body).status_code == 200
    at_review(mt, sid)
    run(mt, k, bid="106")
    [sent] = k.jev.of("DAY_REVIEW")
    assert encoded_bytes(sent["state"]) <= 11_000
    news = sent["state"]["news_since_entry"]
    assert news and all("source_id" not in item and item["ref"] for item in news)
    [asked] = events(engine, dr.JEV_REQUEST, sid)
    manifest = asked["context"]["manifest"]
    assert manifest["within_budget"] and manifest["state_bytes"] == encoded_bytes(sent["state"])
    agent = sent["state"]["agent_answer"]
    shown = [e for e in manifest["entries"] if e["section"] == "agent_answer.sources"]
    assert len(shown) == 3 and len(agent["sources"]) + agent["sources_omitted"] == 6
    assert all(len(src["excerpt"]) <= 300 for src in agent["sources"])
    assert (agent["what_changed"], agent["next_24h"], agent["proves_wrong"]) == (
        long[:600], long[:600], long[:400])  # The agent's reasons are never shortened.
    assert sent["state"]["original_pick"]["thesis"].startswith("THESIS-00")
    assert decision(engine, sid)["outcome"] == "CONTINUE"


# --- The measurement hook (plan 4.6.8) and a reply at the deadline -------------------------------


def test_every_decision_is_a_level_change_record_for_the_unchanged_plan_replay(mt):
    from catalyst_lab.trade_review import decision_records
    from tests.day_review_fixtures import flag_body

    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(stop_option="first")
    at_request(mt, sid)
    run(mt, k, bid="106")
    at_review(mt, sid)
    run(mt, k, bid="106")  # Continue, the stop raised to 103.00.
    engine.manage(sid, quote(mt, "106"))
    engine.manage(sid, quote(mt, "106"))
    k.jev.flag = {"trade_reason": "INTACT", "agent_case": "DOES_NOT_HOLD", "exit_now": "STAY"}
    flag = k.client.post(f"/api/v1/lab/positions/{sid}/exit-flag",
                         json=flag_body(state(mt, sid)["lifecycle_id"]),
                         headers=bearer(AGENTS["claude"]))
    assert flag.status_code == 200
    run(mt, k, bid="106")  # Jev does not agree: the trade stays.
    k.jev.answer(decision="EXIT", trade_reason="WEAKENED", stop_option="KEEP")
    at_request(mt, sid)
    run(mt, k, bid="107")
    at_review(mt, sid)
    run(mt, k, bid="107")  # The second review exits.
    records = decision_records(engine.repo, sid)
    assert [(r["change_kind"], r["outcome"], r["actually_exited"]) for r in records] == [
        ("CONTINUE_EXIT_DECISION", "CONTINUE", False), ("EARLY_EXIT", "EXIT_NOT_AGREED", False),
        ("CONTINUE_EXIT_DECISION", "EXIT", True)]
    first, early, second = records
    assert (first["old_stop"], first["new_stop"], first["old_target"], first["new_target"]) == (
        "95", "103.00", "111", None)
    assert first["quote"]["bid"] == "106" and first["review_number"] == 1
    assert (early["old_stop"], early["new_stop"]) == ("103.00", None) and early["flag_id"]
    assert (second["old_stop"], second["review_number"], second["quote"]["bid"]) == (
        "103.00", 2, "107")
    assert all(datetime.fromisoformat(r["at"]) for r in records)
    assert early["jev"]["answer"]["decision"] == "STAY" and early["agent"] is None
    assert second["jev"]["results"][0]["receipt_ids"] and second["agent"]["answers"] == {}


def test_a_discussion_reply_recorded_at_the_deadline_is_never_overruled(mt):
    from catalyst_lab.trade_review import load_views

    engine, venue, _ = mt
    sid = open_trade(mt, bid="104")
    k = kit(mt)
    request = disagree(mt, k, sid)
    with engine.repo.connect() as conn:
        stale = load_views(conn, [sid])[0][request["review_id"]]  # Before the reply.
    assert answer(k, request["review_id"], answer_body()).status_code == 200
    [setup] = [s for s in engine.store.active() if s["setup_id"] == sid]
    k.day._decide(setup, None, venue.now, stale, dr.EXIT, dr.DISCUSSION_NO_AGENT_REPLY)
    assert events(engine, dr.DECISION, sid) == []  # The reply stands; Jev answers it.
    run(mt, k, bid="104")
    assert decision(engine, sid)["code"] == "DISAGREED_AFTER_DISCUSSION"
    assert len(k.jev.of("DAY_REVIEW")) == 2


def test_missing_bars_are_a_failed_attempt_retried_after_a_minute(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    at_request(mt, sid)
    k.bars.fail = True  # The request still goes out, without options.
    run(mt, k, bid="106")
    assert the_request(engine, sid)["options"] is None
    at_review(mt, sid)
    run(mt, k, bid="106")
    [failed] = events(engine, dr.JEV_RESULT, sid)
    assert (failed["status"], failed["code"], failed["request_id"]) == (
        "FAILED", "COMPLETED_BARS_UNAVAILABLE", None)
    assert k.jev.calls == []
    k.bars.fail = False
    venue.now += timedelta(seconds=30)
    run(mt, k, bid="106")
    assert k.jev.calls == []  # Inside the minute.
    venue.now += timedelta(seconds=31)
    run(mt, k, bid="106")
    assert len(k.jev.of("DAY_REVIEW")) == 1
    assert decision(engine, sid)["outcome"] == "CONTINUE"
