"""Early exits from both sides (plan 4.6.3), EARLY_EXIT_AGREEMENT_V1 (package day-review).

The agent raises a flag over its own route and Jev is asked at once; Jev flags a trade in a
maintenance review (phase 5) and the agent is asked at once. Both say exit: a market sell.
Otherwise, or with no answer in 15 minutes, the trade stays with its stop and target. Hard exits
end pending flags; only the agent that answers for the trade may flag or answer; the control
arm and trades admitted before the version are untouched.

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars, a
mock Jev transport and the real agent routes in process.
"""

import json
from datetime import datetime, timedelta

import pytest

from catalyst_lab import crypto_holding
from catalyst_lab import day_review as dr
from catalyst_lab.audit import verify_events
from tests.day_review_fixtures import (
    AGENTS,
    answer_body,
    bearer,
    decisions,
    events,
    flag_body,
    kit,
    maintain,
    managed_arm,
    market_sells,
    move_to,
    open_trade,
    pending,
    run,
    sell_to_close,
    source,
    state,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission
from tests.maintenance_fixtures import quote
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

# Jev's side of the early exit here is CRYPTO_MAINTENANCE_V4's FLAG_EARLY_EXIT action:
# admission as before package jev-b1 (V5's flags: tests/test_jev_b1_flows.py).
pytestmark = pytest.mark.usefixtures("managed_arm", "pre_jev_b1_admission")
_ = managed_arm


def raise_flag(k, sid, body, token=AGENTS["claude"]):
    return k.client.post(f"/api/v1/lab/positions/{sid}/exit-flag", json=body,
                         headers=bearer(token))


def answer_flag(k, flag_id, body, token=AGENTS["claude"]):
    return k.client.post(f"/api/v1/lab/exit-flags/{flag_id}/answer", json=body,
                         headers=bearer(token))


def the_decision(engine, sid):
    [body] = events(engine, dr.EARLY_EXIT_DECISION, sid)
    return body


def jev_flags(mt, k, sid):
    """A maintenance review at +1R in which Jev flags the trade (FLAG_EARLY_EXIT, BROKEN)."""
    k.jev.maintenance = {"trade_reason": "BROKEN", "action": "FLAG_EARLY_EXIT",
                         "stop_option": "KEEP", "target_option": "KEEP"}
    mt[1].now += timedelta(seconds=1)
    maintain(mt, k, bid="106")
    [flag] = events(mt[0], dr.FLAG_RAISED, sid)
    assert flag["side"] == "JEV"
    return flag


# --- The agent flags --------------------------------------------------------------------------


def test_an_agent_flag_jev_agrees_with_sells_at_market(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    lifecycle = state(mt, sid)["lifecycle_id"]
    body = flag_body(lifecycle, sources=[source(
        mt, "Synthetic fixture: the exchange paused withdrawals of the coin.")])
    reply = raise_flag(k, sid, body)
    assert reply.status_code == 200, reply.text
    ack = reply.json()
    assert (ack["status"], ack["trade_authorized"], ack["position_modified"]) == (
        "EXIT_FLAG_RAISED", False, False)
    assert ack["answer_due_at"] == (venue.now + timedelta(minutes=15)).isoformat()
    [flag] = events(engine, dr.FLAG_RAISED, sid)
    assert (flag["side"], flag["raised_by"]["agent_id"], flag["flag_version"]) == (
        "AGENT", "claude", "EARLY_EXIT_FLAG_V1")
    assert flag["reasons"]["what_changed"].startswith("Synthetic fixture: the exchange")
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    replay = raise_flag(k, sid, body)  # An exact retry returns the stored flag.
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    changed = raise_flag(k, sid, {**body, "next_24h": "Synthetic fixture: another view."})
    assert (changed.status_code, changed.json()["detail"]) == (
        409, "IDEMPOTENCY_CONTENT_MISMATCH")
    run(mt, k, bid="104")  # Jev is asked at once.
    [sent] = k.jev.of("EARLY_EXIT")
    assert set(sent["questions"]) == {"trade_reason", "agent_case", "exit_now"}
    assert sent["state"]["context_version"] == "JEV_EARLY_EXIT_CONTEXT_V1"
    assert "options" not in sent["state"]
    agent_flag = sent["state"]["agent_flag"]
    assert agent_flag["what_changed"].startswith("Synthetic fixture: the exchange")
    assert agent_flag["sources"][0]["excerpt"].startswith("Synthetic fixture: the exchange")
    assert "claude" not in json.dumps(sent).lower()
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"], decision["exit_requested"]) == (
        "EXIT_AGREED", None, "EARLY_EXIT_AGREED")
    assert decision["jev"]["answer"]["decision"] == "EXIT" and decision["jev"]["receipt_ids"]
    measurement = decision["measurement"]
    assert (measurement["change_kind"], measurement["old_stop"], measurement["old_target"],
            measurement["actually_exited"], measurement["early_exit_outcome"]) == (
        "EARLY_EXIT", "95", "111", True, "EXIT_AGREED")
    assert measurement["quote"]["bid"] == "104"
    [resolved] = events(engine, dr.FLAG_RESOLVED, sid)
    assert (resolved["outcome"], resolved["exit_requested"]) == (
        "EXIT_AGREED", "EARLY_EXIT_AGREED")
    sell_to_close(mt, sid, "SOL/USD", "104")
    final = state(mt, sid)
    assert (final["state"], final["reason"]) == ("CLOSED", "EARLY_EXIT_AGREED")
    assert all(d["claimed"] for d in decisions(engine, sid) if d["action"] in {"CANCEL", "EXIT"})
    assert len(market_sells(mt, "SOL/USD")) == 1
    assert verify_events(engine.repo.export_events())["valid"]


def test_an_agent_flag_jev_does_not_agree_with_keeps_the_trade(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.flag = {"trade_reason": "INTACT", "agent_case": "DOES_NOT_HOLD", "exit_now": "STAY"}
    lifecycle = state(mt, sid)["lifecycle_id"]
    assert raise_flag(k, sid, flag_body(lifecycle)).status_code == 200
    run(mt, k, bid="104")
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["measurement"]["actually_exited"]) == (
        "EXIT_NOT_AGREED", False)
    current = state(mt, sid)
    assert (current["stop"], current["target"], current.get("exit_requested")) == (
        "95", "111", None)
    engine.manage(sid, quote(mt, "104"))
    assert market_sells(mt, "SOL/USD") == []
    # A new flag may follow once the last one is resolved.
    venue.now += timedelta(minutes=1)
    assert raise_flag(k, sid, flag_body(lifecycle)).json()["status"] == "EXIT_FLAG_RAISED"


def test_an_agent_flag_jev_cannot_answer_in_fifteen_minutes_keeps_the_trade(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.status = {"EARLY_EXIT": 503}
    lifecycle = state(mt, sid)["lifecycle_id"]
    raised = raise_flag(k, sid, flag_body(lifecycle)).json()
    run(mt, k, bid="104")
    run(mt, k, bid="104")
    assert len(k.jev.of("EARLY_EXIT")) == 1  # Retried after a minute, inside the window.
    venue.now += timedelta(seconds=61)
    run(mt, k, bid="104")
    assert len(k.jev.of("EARLY_EXIT")) == 2
    assert events(engine, dr.EARLY_EXIT_DECISION, sid) == []
    move_to(mt, datetime.fromisoformat(raised["answer_due_at"]))
    run(mt, k, bid="104")
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"]) == ("NO_ANSWER_IN_TIME", "JEV_UNAVAILABLE")
    assert not state(mt, sid).get("exit_requested")


def test_reviews_disabled_ends_an_agent_flag_without_an_answer(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt, reviews="DISABLED")
    assert raise_flag(k, sid, flag_body(state(mt, sid)["lifecycle_id"])).status_code == 200
    run(mt, k, bid="104")
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"]) == ("NO_ANSWER_IN_TIME",
                                                        "JEV_REVIEWS_DISABLED")
    assert k.jev.calls == [] and not state(mt, sid).get("exit_requested")


# --- Jev flags ---------------------------------------------------------------------------------


def test_a_jev_flag_the_agent_agrees_with_sells_at_market(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    flag = jev_flags(mt, k, sid)
    assert pending(k) == []  # Not asked until the day-review pass.
    run(mt, k, bid="106")
    [asked] = events(engine, dr.FLAG_ASKED, sid)
    assert asked["addressee"] == {"agent_id": "claude", "basis": "PROPOSING_AGENT"}
    assert asked["answer_due_at"] == flag["answer_due_at"]
    [item] = pending(k)
    assert (item["kind"], item["flag_id"], item["raised_by"]) == (
        "EXIT_FLAG", flag["flag_id"], "JEV")
    assert item["jev_reasons"]["trade_reason"] == "BROKEN"
    assert item["trade"]["levels"] == {"stop": "95", "target": "111"}
    assert pending(k, AGENTS["instinct"]) == []
    refused = answer_flag(k, flag["flag_id"], answer_body("EXIT"), AGENTS["instinct"])
    assert (refused.status_code, refused.json()["detail"]) == (403, "AGENT_IDENTITY_MISMATCH")
    suggested = answer_flag(k, flag["flag_id"], answer_body("CONTINUE", suggested_stop="100"))
    assert (suggested.status_code, suggested.json()["detail"]) == (
        422, "SUGGESTED_LEVELS_NOT_ALLOWED")
    reply = answer_flag(k, flag["flag_id"], answer_body("EXIT"))
    assert reply.status_code == 200 and reply.json()["status"] == "EXIT_FLAG_ANSWER_RECORDED"
    assert pending(k) == []
    run(mt, k, bid="106")
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["sides"], decision["agent"]["decision"]) == (
        "EXIT_AGREED", ["JEV"], "EXIT")
    assert decision["agent"]["agent_id"] == "claude"
    assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"
    sell_to_close(mt, sid, "SOL/USD", "106")
    assert state(mt, sid)["reason"] == "EARLY_EXIT_AGREED"


@pytest.mark.parametrize("agent", ["CONTINUE", None])
def test_a_jev_flag_the_agent_rejects_or_ignores_keeps_the_trade(mt, agent):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    flag = jev_flags(mt, k, sid)
    run(mt, k, bid="106")
    if agent is not None:
        assert answer_flag(k, flag["flag_id"], answer_body(agent)).status_code == 200
        run(mt, k, bid="106")
        outcome = "EXIT_NOT_AGREED"
    else:
        move_to(mt, datetime.fromisoformat(flag["answer_due_at"]) - timedelta(seconds=1))
        run(mt, k, bid="106")
        assert events(engine, dr.EARLY_EXIT_DECISION, sid) == []
        move_to(mt, datetime.fromisoformat(flag["answer_due_at"]))
        late = answer_flag(k, flag["flag_id"], answer_body("EXIT"))
        assert (late.status_code, late.json()["detail"]) == (
            409, "EXIT_FLAG_ANSWER_WINDOW_CLOSED")
        run(mt, k, bid="106")
        outcome = "NO_ANSWER_IN_TIME"
    decision = the_decision(engine, sid)
    assert decision["outcome"] == outcome and decision["measurement"]["actually_exited"] is False
    current = state(mt, sid)
    assert (current["stop"], current["target"], current.get("exit_requested")) == (
        "95", "111", None)
    assert market_sells(mt, "SOL/USD") == []


def test_when_both_sides_flag_the_trade_both_have_said_exit(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    jev_flags(mt, k, sid)
    run(mt, k, bid="106")
    assert raise_flag(k, sid, flag_body(state(mt, sid)["lifecycle_id"])).status_code == 200
    run(mt, k, bid="106")
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"], decision["sides"]) == (
        "EXIT_AGREED", "BOTH_SIDES_FLAGGED", ["AGENT", "JEV"])
    assert len(decision["resolved"]) == 2 and len(events(engine, dr.FLAG_RESOLVED, sid)) == 2
    assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"
    assert k.jev.of("EARLY_EXIT") == []  # Nothing left to ask.


# --- Hard exits, ownership and scope -----------------------------------------------------------


def test_a_hard_exit_ends_a_pending_flag(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    k.jev.status = {"EARLY_EXIT": 503}
    assert raise_flag(k, sid, flag_body(state(mt, sid)["lifecycle_id"])).status_code == 200
    run(mt, k, bid="106")
    engine.manage(sid, quote(mt, "111"))  # The target is reached: a hard exit.
    run(mt, k, bid="111")
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"], decision["measurement"]) == (
        "LIFECYCLE_ENDED", "EXIT_IN_PROGRESS_DURING_REVIEW", None)
    assert state(mt, sid)["exit_requested"] == "TARGET_EXIT"
    refused = raise_flag(k, sid, flag_body(state(mt, sid)["lifecycle_id"]))
    assert (refused.status_code, refused.json()["detail"]) == (409, "EXIT_IN_PROGRESS")


def test_only_the_agent_that_answers_for_the_trade_may_flag_it(mt, monkeypatch):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    lifecycle = state(mt, sid)["lifecycle_id"]
    refused = raise_flag(k, sid, flag_body(lifecycle), AGENTS["instinct"])
    assert (refused.status_code, refused.json()["detail"]) == (403, "AGENT_IDENTITY_MISMATCH")
    wrong = raise_flag(k, sid, flag_body("00000000-0000-4000-8000-000000000000"))
    assert (wrong.status_code, wrong.json()["detail"]) == (409, "POSITION_LIFECYCLE_MISMATCH")
    named = raise_flag(k, sid, flag_body(lifecycle, what_changed="Claude thinks it is over."))
    assert (named.status_code, named.json()["detail"]) == (422, "AGENT_IDENTITY_IN_ANSWER")
    missing = raise_flag(k, "00000000-0000-4000-8000-000000000001", flag_body(lifecycle))
    assert (missing.status_code, missing.json()["detail"]) == (404, "POSITION_NOT_FOUND")
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    control = open_trade(mt, "ETH/USD")
    refused = raise_flag(k, control, flag_body(state(mt, control)["lifecycle_id"]))
    assert (refused.status_code, refused.json()["detail"]) == (409, "EARLY_EXIT_NOT_AVAILABLE")
    assert events(engine, dr.FLAG_RAISED) == []


def test_a_trade_admitted_before_the_version_keeps_todays_pending_jev_flag(mt, monkeypatch):
    """A maintained trade admitted under CRYPTO_24H_HOLD_V1 (as before this package): Jev's
    flag stays pending until the trade closes, nobody is asked, nothing resolves it."""
    engine, venue, _ = mt
    monkeypatch.setattr("catalyst_lab.managed_execution.crypto_holding.admission_policy",
                        lambda v3, arm, window=None: crypto_holding.CRYPTO_24H_HOLD if v3 else None)
    sid = open_trade(mt)
    assert state(mt, sid)["holding_policy"] == crypto_holding.CRYPTO_24H_HOLD.record()
    assert "maintenance_policy" in state(mt, sid)
    k = kit(mt)
    jev_flags(mt, k, sid)
    for _ in range(2):
        run(mt, k, bid="106")
    venue.now += timedelta(minutes=16)
    run(mt, k, bid="106")
    assert events(engine, dr.FLAG_ASKED) == [] and events(engine, dr.FLAG_RESOLVED) == []
    assert events(engine, dr.EARLY_EXIT_DECISION) == [] and pending(k) == []
    assert k.maintenance.status(engine.store.active())["pending_exit_flags"] == 1
    refused = raise_flag(k, sid, flag_body(state(mt, sid)["lifecycle_id"]))
    assert (refused.status_code, refused.json()["detail"]) == (409, "EARLY_EXIT_NOT_AVAILABLE")
    move_to(mt, datetime.fromisoformat(state(mt, sid)["hard_exit_at"]))
    engine.manage(sid, quote(mt, "106"))
    assert state(mt, sid)["exit_requested"] == "HOLD_24H_EXIT"  # Today's 24-hour exit.
