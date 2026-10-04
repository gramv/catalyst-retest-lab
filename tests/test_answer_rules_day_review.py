"""CRYPTO_24H_REVIEW_V2 on the fixture venue and the session harness (package answer-rules).

DAY_REVIEW_ANSWER_RULE_V2 end to end: the WIF-shaped answer continues with the stop raised and
the target kept (a setup that recorded V1 exits on the same answer), EXIT ignores the option
answers, the discussion round reads its final answer by V2, a restart recovers the answer and
reads it by V2, and JEV_DAY_REVIEW_CONTEXT_V2 carries the maintenance history. Then the session
harness runs consecutive minute reviews (``execute --maintenance-minutes``), and its review
clock holds still while the agent answers (the round-5 answer windows).

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars, a
mock Jev transport answering with exact distributions, the real agent routes in process and
the SESSION_SIMULATION harness. No broker, provider, network or owner-ledger contact.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from catalyst_lab import crypto_holding as ch
from catalyst_lab import day_review as dr
from catalyst_lab.account_risk import JEV_MANAGED_ARM
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT
from tests.day_review_fixtures import (
    answer,
    answer_body,
    at_request,
    at_review,
    decisions,
    events,
    kit,
    maintain,
    managed_arm,
    market_sells,
    open_trade,
    pending,
    run,
    sell_to_close,
    state,
    stop_orders,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_trade_plan_admission as pre_trade_plan_admission
from tests.maintenance_fixtures import quote
from tests.maintenance_fixtures import v1_admission as v1_admission
from tests.test_agent_research_session import make_session as make_session
from tests.test_agent_research_session import session_script, submit
from tests.test_day_review_session import agent
from tests.test_day_review_session import answer as agent_answer
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

pytestmark = pytest.mark.usefixtures("managed_arm", "pre_trade_plan_admission")
_ = managed_arm

# WIF's 24-hour review answer of 2026-09-28 (jev-1.13.0, round 5), on the fixture's options:
# CONTINUE 0.60, stop_option S1 0.55, target_option KEEP 0.44 = T1 0.44 (the tie that made V1
# exit a trade both sides wanted to continue).
WIF_REVIEW = {
    "decision": {"CONTINUE": 0.6, "EXIT": 0.36, INSUFFICIENT: 0.04},
    "stop_option": {"first": 0.55, "KEEP": 0.34, INSUFFICIENT: 0.01},
    "target_option": {"KEEP": 0.44, "first": 0.44, INSUFFICIENT: 0.02},
    "trade_reason": {"INTACT": 0.56, "WEAKENED": 0.28, "BROKEN": 0.12, INSUFFICIENT: 0.04},
    "agent_case": {"DOES_NOT_HOLD": 0.68, "HOLDS": 0.29, INSUFFICIENT: 0.03},
}


def decision(engine, sid):
    [body] = events(engine, dr.DECISION, sid)
    return body


def the_request(engine, sid):
    [body] = events(engine, dr.REQUESTED, sid)
    return body


def agent_continues_then_jev_answers(mt, k, sid, *, agent="CONTINUE", bid="106"):
    at_request(mt, sid)
    run(mt, k, bid=bid)
    request = the_request(mt[0], sid)
    assert answer(k, request["review_id"], answer_body(agent)).status_code == 200
    at_review(mt, sid)
    run(mt, k, bid=bid)
    return request


def test_v2_continues_the_wif_answer_with_the_stop_raised_and_the_target_kept(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["holding_policy"] == ch.CRYPTO_24H_REVIEW_V2.record()
    k = kit(mt)
    assert len(maintain(mt, k, bid="106")) == 1  # One V2 maintenance review at +1R: held.
    k.jev.exact = {"DAY_REVIEW": dict(WIF_REVIEW)}
    request = agent_continues_then_jev_answers(mt, k, sid)
    assert request["policy_id"] == "CRYPTO_24H_REVIEW_V2"
    [sent] = k.jev.of("DAY_REVIEW")
    assert sent["state"]["context_version"] == "JEV_DAY_REVIEW_CONTEXT_V2"
    [earlier] = sent["state"]["review_history"]  # The maintenance review, as history.
    assert (earlier["trigger_reasons"], earlier["outcome"]) == (["R_MILESTONE"], "HELD")
    assert "`review_history`" in sent["questions"]["trade_reason"]["instructions"]
    [jev_request] = events(engine, dr.JEV_REQUEST, sid)
    assert jev_request["context"]["policy"] == ch.CRYPTO_24H_REVIEW_V2.record()
    assert jev_request["context"]["identity"]["holding_policy_id"] == "CRYPTO_24H_REVIEW_V2"
    [result] = events(engine, dr.JEV_RESULT, sid)
    # Usable: the transport still flags the tied answer, the rule reads it.
    assert (result["status"], result["code"], result["review_status"]) == (
        "ANSWERED", None, "NEEDS_REVIEW")
    assert result["answer"]["answer_rule"] == "DAY_REVIEW_ANSWER_RULE_V2"
    assert result["answer"]["option_use"]["target"] == {
        "choice": "KEEP", "raise_to": None, "code": "TARGET_OPTION_NOT_USABLE",
        "reason": "TIED"}
    assert result["chosen"]["stop"]["option_id"] == "S1" and result["chosen"]["target"] is None
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["level_change"], body["policy_id"]) == (
        "CONTINUE", "AGREED", "APPLIED", "CRYPTO_24H_REVIEW_V2")
    assert body["stop"] == {"old": "95", "new": "103.00", "option_id": "S1",
                            "bases": ["SWING_LOW_15M"]}
    assert body["target"] == {"old": "111", "new": None, "option_id": None, "bases": None}
    assert body["levels_after"] == {"stop": "103.00", "target": "111"}
    assert body["measurement"]["new_stop"] == "103.00"
    assert body["measurement"]["new_target"] is None
    current = state(mt, sid)
    assert current["continuations"] == 1 and not current.get("exit_requested")
    assert current["stop_replace"]["change_id"] == request["review_id"]
    [old] = stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "106"))
    [amend] = decisions(engine, sid, "AMEND")
    assert (amend["method"], amend["reason"], amend["claimed"]) == ("PATCH", "REPLACE_STOP", True)
    assert amend["payload"] == {"stop_price": "103.00", "limit_price": "102.99"}
    assert market_sells(mt, "SOL/USD") == []
    status = k.day.status(engine.store.active())
    assert status["policy_id"] == "CRYPTO_24H_REVIEW_V2"
    assert status["open_trades_by_policy"] == {"CRYPTO_24H_REVIEW_V2": 1}
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_setup_that_recorded_v1_exits_on_the_same_answer(mt, v1_admission):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["holding_policy"] == ch.CRYPTO_24H_REVIEW.record()
    k = kit(mt)
    k.jev.exact = {"DAY_REVIEW": dict(WIF_REVIEW)}
    request = agent_continues_then_jev_answers(mt, k, sid)
    assert request["policy_id"] == "CRYPTO_24H_REVIEW_V1"
    [sent] = k.jev.of("DAY_REVIEW")
    assert sent["state"]["context_version"] == "JEV_DAY_REVIEW_CONTEXT_V1"
    assert "review_history" not in sent["state"]
    [result] = events(engine, dr.JEV_RESULT, sid)
    assert (result["status"], result["code"]) == ("UNUSABLE", "UNCERTAIN_JUDGMENT")
    assert "answer_rule" not in result["answer"]  # V1's record.
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["policy_id"]) == (
        "EXIT", "JEV_ANSWER_UNUSABLE", "CRYPTO_24H_REVIEW_V1")
    assert state(mt, sid)["exit_requested"] == "DAY_REVIEW_EXIT"
    sell_to_close(mt, sid, "SOL/USD", "106")
    assert state(mt, sid)["state"] == "CLOSED"


def test_under_v2_exit_ignores_the_option_answers(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.answer(decision="EXIT", trade_reason="WEAKENED", stop_option="first",
                 target_option="first")  # V1 would refuse this as contradictory.
    agent_continues_then_jev_answers(mt, k, sid, agent="EXIT", bid="104")
    [result] = events(engine, dr.JEV_RESULT, sid)
    assert (result["status"], result["code"]) == ("ANSWERED", None)
    assert result["answer"]["option_use"]["stop"]["reason"] == "NOT_RAISED_BY_DECISION"
    assert result["chosen"] == {"stop": None, "target": None}
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["exit_requested"]) == (
        "EXIT", "AGREED", "DAY_REVIEW_EXIT")


@pytest.mark.parametrize("exact,code", [
    ({"decision": {"CONTINUE": 0.45, "EXIT": 0.45}}, "UNCERTAIN_JUDGMENT"),
    ({"decision": {INSUFFICIENT: 0.7}}, "UNCERTAIN_JUDGMENT"),
    ({"decision": {"CONTINUE": 0.7}, "trade_reason": {"BROKEN": 0.6}},
     "CONTRADICTORY_REVIEW_ANSWERS"),
])
def test_under_v2_an_unclear_decision_or_a_broken_reason_still_exits(mt, exact, code):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.exact = {"DAY_REVIEW": exact}
    agent_continues_then_jev_answers(mt, k, sid)
    [result] = events(engine, dr.JEV_RESULT, sid)
    assert (result["status"], result["code"]) == ("UNUSABLE", code)
    body = decision(engine, sid)
    assert (body["outcome"], body["code"]) == ("EXIT", "JEV_ANSWER_UNUSABLE")


def test_the_discussion_round_reads_the_final_answer_by_v2(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.exact = {"DAY_REVIEW": {"decision": {"EXIT": 0.7}, "trade_reason": {"WEAKENED": 0.6},
                                  "agent_case": {"DOES_NOT_HOLD": 0.6}},
                   "DAY_REVIEW_FINAL": dict(WIF_REVIEW)}
    request = agent_continues_then_jev_answers(mt, k, sid, bid="106")
    assert events(engine, dr.DECISION, sid) == []  # Disagreement: one discussion round.
    [item] = pending(k)
    assert item["round"] == "DISCUSSION"
    reply = answer(k, request["review_id"], answer_body())
    assert reply.status_code == 200 and reply.json()["round"] == "DISCUSSION"
    run(mt, k, bid="106")
    first, final = k.jev.of("DAY_REVIEW")
    assert final["state"]["review"]["round"] == "FINAL"
    assert final["state"]["discussion"]["your_first_answer"]["decision"]["choice"] == "EXIT"
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["level_change"]) == (
        "CONTINUE", "AGREED_AFTER_DISCUSSION", "APPLIED")
    assert body["levels_after"] == {"stop": "103.00", "target": "111"}
    assert [r["status"] for r in body["jev"]["results"]] == ["ANSWERED", "ANSWERED"]


def test_a_v2_answer_recovered_after_a_restart_is_read_by_v2(mt, monkeypatch):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.exact = {"DAY_REVIEW": dict(WIF_REVIEW)}
    at_request(mt, sid)
    run(mt, k, bid="106")
    request = the_request(engine, sid)
    assert answer(k, request["review_id"], answer_body("CONTINUE")).status_code == 200
    at_review(mt, sid)

    def crash(*args, **kwargs):
        raise RuntimeError("fixture crash after the provider answered")

    monkeypatch.setattr(k.day, "_record_result", crash)
    run(mt, k, bid="106")
    assert events(engine, dr.JEV_RESULT, sid) == [] and events(engine, dr.DECISION, sid) == []
    restarted = kit(mt, jev=k.jev)  # A new process: same ledger, nothing in memory.
    run(mt, restarted, bid="106")
    run(mt, restarted, bid="106")
    assert len(k.jev.of("DAY_REVIEW")) == 1  # The recorded answer; never a second vote.
    [result] = events(engine, dr.JEV_RESULT, sid)
    assert result["status"] == "ANSWERED"
    assert result["answer"]["answer_rule"] == "DAY_REVIEW_ANSWER_RULE_V2"
    body = decision(engine, sid)
    assert (body["outcome"], body["code"], body["levels_after"]) == (
        "CONTINUE", "AGREED", {"stop": "103.00", "target": "111"})


# --- The session harness ------------------------------------------------------------------------


def session_report(now):
    from tests.test_research_report_v3 import pick, report_v3

    return report_v3([pick(0, "SOL/USD", kind="BOTH", now=now)], now=now,
                     agent_id=session_script.AGENT_ID, skipped=[])


def test_a_session_runs_consecutive_minute_reviews_on_a_maintained_trade(make_session,
                                                                          monkeypatch):
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda setup_id, pct: JEV_MANAGED_ARM)
    fixture = session_script.FixtureScript({"SOL/USD": {"maintenance": "HOLD"}})
    session = make_session(selection_rule="TOPK", topk_k=5, fixture=fixture)
    config = session._configuration()
    # From package jev-budget sessions admit CRYPTO_MAINTENANCE_V3; the session's guard stays
    # NORMAL (the session ledger spends cents), which is V2's per-minute cadence.
    assert config["maintenance_policy"] == {**config["maintenance_policy"],
                                            "policy_id": "CRYPTO_MAINTENANCE_V3",
                                            "review_bar_seconds": 60}
    submit(session, session_report(datetime.now(UTC)))
    session.tick()
    for bad in (0, 11, "3"):
        with pytest.raises(session_script.SessionRefused, match="MAINTENANCE_MINUTES_INVALID"):
            session.execute(simulate_prints=True, maintenance_minutes=bad)
    executed = session.execute(simulate_prints=True, hold_open=True, maintenance_minutes=4)
    [life] = executed["lifecycles"]
    review = life["maintenance_review"]
    assert (review["outcome"], review["action"], review["policy_id"]) == (
        "HELD", "HOLD", "CRYPTO_MAINTENANCE_V3")
    minutes = review["minute_reviews"]
    assert [m["minute"] for m in minutes] == [1, 2, 3]
    assert all(m["outcome"] == "HELD" and m["trigger_reasons"] == ["BAR_1M"]
               and m["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V5"
               and m["jev_calls"] == 1 for m in minutes), minutes
    assert [m["review_history"] for m in minutes] == [1, 2, 3]  # Jev sees what it judged.
    assert life["held_open"] and life["final_state"] == "OPEN"
    text = session_script.render_execute(json.loads(json.dumps(executed, default=str)))
    assert "minute +1" in text and "minute +3" in text and "history 3" in text
    assert executed["jev_calls"]["used"] >= 4
    with session.repo.connect() as conn:
        rows = conn.execute("""SELECT body FROM lab.managed_events
            WHERE kind='MAINTENANCE_DECISION' ORDER BY event_seq""").fetchall()
    assert [r["body"]["outcome"] for r in rows] == ["HELD"] * 4
    assert {r["body"]["answer_rule"] for r in rows} == {"MAINTENANCE_ANSWER_RULE_V2"}


def test_the_review_clock_holds_while_the_agent_answers_so_no_window_closes(make_session,
                                                                          monkeypatch):
    """Round 5 of 2026-09-27: the harness's review clock followed the wall clock, so DOT's
    request, recorded at ``review --at review`` a second or two before its own T, closed before
    the agent could answer, and Jev decided alone. The clock now stays where a step put it."""
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda setup_id, pct: JEV_MANAGED_ARM)
    wall, real = [timedelta(0)], session_script._utcnow
    monkeypatch.setattr(session_script, "_utcnow", lambda: real() + wall[0])
    fixture = session_script.FixtureScript({
        "SOL/USD": {"day_review": {"decision": "CONTINUE"}},
        "XRP/USD": {"day_review": {"decision": "CONTINUE"}}})
    session = make_session(selection_rule="TOPK", topk_k=5, fixture=fixture)
    from tests.test_research_report_v3 import pick, report_v3

    now = datetime.now(UTC)
    submit(session, report_v3([pick(i, symbol, kind="BOTH", now=now)
                               for i, symbol in enumerate(("SOL/USD", "XRP/USD"))],
                              now=now, agent_id=session_script.AGENT_ID, skipped=[]))
    session.tick()
    simulate = session._simulate

    def five_seconds_apart(setup, *args, **kwargs):
        record = simulate(setup, *args, **kwargs)
        wall[0] += timedelta(seconds=5)  # The next trade fills 5 s later (T 5 s later).
        return record

    monkeypatch.setattr(session, "_simulate", five_seconds_apart)
    executed = session.execute(simulate_prints=True, hold_open=True)
    assert all(life["held_open"] for life in executed["lifecycles"])
    # Before any review step the review clock is the wall clock.
    assert abs(session.review_now() - session_script._utcnow()) < timedelta(seconds=1)
    first = session.review(at="request")  # 30 min before SOL's T: only SOL is asked.
    assert [item["symbol"] for item in first["pending"]] == ["SOL/USD"]
    assert first["review_clock_held"] is True
    client = agent(session)
    [sol] = client.get("/api/v1/lab/reviews").json()["items"]
    assert client.post(sol["answer_route"], json=agent_answer("CONTINUE")).status_code == 200
    at_t = session.review(at="review")  # SOL's T: Jev decides it; XRP is asked now.
    by_symbol = {trade["symbol"]: trade for trade in at_t["trades"]}
    assert by_symbol["SOL/USD"]["decisions"][0]["code"] == "AGREED"
    [xrp] = at_t["pending"]
    held = session.review_now()
    assert xrp["symbol"] == "XRP/USD" and datetime.fromisoformat(xrp["answer_due_at"]) - held \
        < timedelta(seconds=10)  # XRP's answer window: seconds of review time.
    wall[0] += timedelta(minutes=10)  # The agent takes ten minutes of wall time to answer.
    assert session.review_now() == held  # The review clock waited (it followed the wall).
    [listed] = client.get("/api/v1/lab/reviews").json()["items"]
    assert (listed["symbol"], listed["round"]) == ("XRP/USD", "FIRST")
    reply = client.post(listed["answer_route"], json=agent_answer("CONTINUE"))
    assert reply.status_code == 200, reply.text  # Round 5: the window had closed (DOT).
    assert datetime.fromisoformat(reply.json()["received_at"]) == held
    last = session.review(at="review")  # XRP's T: agreed, not Jev alone.
    xrp_trade = next(t for t in last["trades"] if t["symbol"] == "XRP/USD")
    [xrp_decision] = xrp_trade["decisions"]
    assert (xrp_decision["outcome"], xrp_decision["code"]) == ("CONTINUE", "AGREED")
    assert "held until the next --at/--minutes" in session_script.render_review(
        json.loads(json.dumps(last, default=str)))
    # Only --at and --minutes move it, forward only; a plain review step does not.
    moved = session.review_now()
    session.review()
    assert session.review_now() == moved
    session.review(minutes=3)
    assert session.review_now() == moved + timedelta(minutes=3)
    with pytest.raises(session_script.SessionRefused, match="REVIEW_BID_INVALID"):
        session.review(minutes=5, bid="-1")  # Refused before the clock moves.
    assert session.review_now() == moved + timedelta(minutes=3)
