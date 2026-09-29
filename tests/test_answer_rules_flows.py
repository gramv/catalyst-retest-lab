"""CRYPTO_MAINTENANCE_V2 on the fixture venue (package answer-rules): the answer rule, the minute
cadence, the in-flight skip, the bound on a pass's Jev calls and context V5, end to end; a
setup that recorded V1 keeps V1's reading. From package jev-budget admission records
CRYPTO_MAINTENANCE_V3; these tests admit under V2 (``v2_admission``, as every setup admitted
before that package recorded), which keeps exactly this behaviour whatever the budget.

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars and a
mock Jev transport answering with exact distributions (``shaped_answer``; the round-5 answer is
a faithful copy of the real one on the fixture's options). No broker, provider, network or
owner-ledger contact.
"""

from datetime import timedelta

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT
from catalyst_lab.unchanged_plan import TARGET_RAISE, maintenance_level_changes
from tests.maintenance_fixtures import (
    admit_many,
    bodies,
    decisions,
    entry_order,
    fill,
    fresh_bar,
    maintainer,
    open_trade,
    quote,
    run_pass,
    stop_orders,
    trigger,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import v1_admission as v1_admission
from tests.maintenance_fixtures import v2_admission as v2_admission
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

# WIF's maintenance answer of 2026-09-27 (jev-1.13.0, round 5), on the fixture's options: HOLD
# 0.39 with stop_option S1 0.49 against KEEP 0.41 -- refused by V1 as contradictory.
ROUND_5 = {
    "action": {"HOLD": 0.39, "RAISE_STOP_AND_TARGET": 0.26, "RAISE_STOP": 0.14,
               "FLAG_EARLY_EXIT": 0.12, "RAISE_TARGET": 0.08, INSUFFICIENT: 0.01},
    "stop_option": {"first": 0.49, "KEEP": 0.41, INSUFFICIENT: 0.02},
    "target_option": {"KEEP": 0.41, "last": 0.32, "first": 0.17, INSUFFICIENT: 0.02},
    "trade_reason": {"INTACT": 0.51, "WEAKENED": 0.26, "BROKEN": 0.22, INSUFFICIENT: 0.01},
}


@pytest.fixture(autouse=True)
def admitted_under_v2(v2_admission):
    """Package answer-rules' V2, as recorded by setups admitted before package jev-budget (a
    test that also asks for ``v1_admission`` admits under V1)."""


def state(mt, sid):
    return mt[0]._load(sid)[1]


def reviewed(mt, kit, bid="106", seconds=1, symbol="SOL/USD"):
    """One pass ``seconds`` on at ``bid`` (+1R is 105.20: the first pass after the open is an
    R_MILESTONE review)."""
    mt[1].now += timedelta(seconds=seconds)
    kit.prices.set(symbol, bid)
    return run_pass(mt, kit)


def the_decision(engine, sid):
    return bodies(engine, "MAINTENANCE_DECISION", sid)[-1]


# --- The answer rule -----------------------------------------------------------------------------


def test_v2_holds_the_round_5_answer_and_shows_it_in_the_next_minutes_history(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["maintenance_policy"] == cm.CRYPTO_MAINTENANCE_V2.record()
    kit = maintainer(mt)
    kit.jev.exact = dict(ROUND_5)
    [request_id] = reviewed(mt, kit)
    [request] = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    context = request["context"]
    assert context["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V5"
    assert context["policy"] == cm.CRYPTO_MAINTENANCE_V2.record()
    assert context["identity"]["maintenance_policy_id"] == "CRYPTO_MAINTENANCE_V2"
    assert request["trigger"]["policy_id"] == "CRYPTO_MAINTENANCE_V2"
    assert len(context["state"]["price_action"]["bars_1m"]["rows"]) == 60
    assert context["state"]["review_history"] == []
    [sent] = kit.jev.calls
    assert sent["state"] == context["state"]
    assert "`review_history`" in sent["questions"]["trade_reason"]["instructions"]
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"], decision["action"]) == ("HELD", None, "HOLD")
    assert (decision["policy_id"], decision["answer_rule"]) == (
        "CRYPTO_MAINTENANCE_V2", "MAINTENANCE_ANSWER_RULE_V2")
    assert decision["option_use"]["stop"] == {"choice": "S1", "raise_to": None, "code": None,
                                              "reason": "NOT_RAISED_BY_ACTION"}
    assert decision["answers"]["action"] == {"choice": "HOLD", "top_p": "0.39"}
    assert decision["review_status"] == "RECORDED"
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    assert not state(mt, sid).get("stop_replace") and not decisions(engine, sid, "AMEND")
    [judgment] = bodies(engine, "MANAGED_JEV_JUDGMENT", sid)
    assert (judgment["policy_id"], judgment["outcome"]) == ("CRYPTO_MAINTENANCE_V2", "HELD")
    assert {b["policy_id"] for b in bodies(engine, "MAINTENANCE_TRIGGER", sid)} == {
        "CRYPTO_MAINTENANCE_V2"}
    assert bodies(engine, "MAINTENANCE_OPENED", sid)[0]["policy_id"] == "CRYPTO_MAINTENANCE_V2"
    # A minute later (the next completed minute and V1's floor): Jev sees what it judged.
    kit.jev.exact = {}
    [second] = reviewed(mt, kit, seconds=60)
    last = bodies(engine, "POSITION_REVIEW_REQUEST", sid)[-1]
    assert last["trigger"]["reasons"] == ["BAR_1M"]
    [earlier] = last["context"]["state"]["review_history"]
    assert earlier == {
        "minutes_ago": 1, "trigger_reasons": ["R_MILESTONE"], "outcome": "HELD", "code": None,
        "action": {"choice": "HOLD", "top_p": "0.39"},
        "trade_reason": {"choice": "INTACT", "top_p": "0.51"},
        "stop_option": {"choice": "S1", "top_p": "0.49", "price": "103.00"},
        "target_option": {"choice": "KEEP", "top_p": "0.41"}}
    assert kit.jev.calls[-1]["state"]["review_history"] == [earlier]
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_setup_that_recorded_v1_still_refuses_hold_with_s1(mt, v1_admission):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["maintenance_policy"] == cm.CRYPTO_MAINTENANCE.record()
    kit = maintainer(mt)
    kit.jev.exact = dict(ROUND_5)
    reviewed(mt, kit)
    [request] = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    assert request["context"]["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V4"
    assert request["context"]["policy"] == cm.CRYPTO_MAINTENANCE.record()
    assert "bars_1m" not in request["context"]["state"]["price_action"]
    assert "review_history" not in request["context"]["state"]
    [sent] = kit.jev.calls
    assert "`review_history`" not in sent["questions"]["trade_reason"]["instructions"]
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"], decision["policy_id"]) == (
        "REFUSED", "CONTRADICTORY_MANAGEMENT_ANSWERS", "CRYPTO_MAINTENANCE_V1")
    assert "answer_rule" not in decision and "option_use" not in decision  # V1's body.
    [judgment] = bodies(engine, "MANAGED_JEV_JUDGMENT", sid)
    assert judgment["policy_id"] == "CRYPTO_MAINTENANCE_V1"


def test_v2_raises_only_the_usable_level(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.exact = {"action": {"RAISE_STOP_AND_TARGET": 0.7},
                     "stop_option": {"first": 0.45, "KEEP": 0.45},  # Tied: the stop stays.
                     "target_option": {"first": 0.8}}
    reviewed(mt, kit)
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"], decision["action"]) == (
        "APPLIED", None, "RAISE_STOP_AND_TARGET")
    assert decision["target"] == {"old": "111", "new": "112.35", "option_id": "T1",
                                  "bases": ["HIGH_24H"]}
    assert decision["stop"] == {"old": "95", "new": None, "option_id": None, "bases": None}
    assert decision["option_use"]["stop"] == {
        "choice": "S1", "raise_to": None, "code": "STOP_OPTION_NOT_USABLE", "reason": "TIED"}
    assert decision["levels_after"] == {"stop": "95", "target": "112.35"}
    assert not state(mt, sid).get("stop_replace")
    [change] = maintenance_level_changes(engine.repo, sid)
    assert change.change_kind == TARGET_RAISE  # The level that moved, not the action's name.


@pytest.mark.parametrize("exact,why", [
    ({"action": {"RAISE_STOP": 0.7}, "stop_option": {"KEEP": 0.8}}, {"stop": "KEEP"}),
    ({"action": {"RAISE_TARGET": 0.7}, "target_option": {INSUFFICIENT: 0.8}},
     {"target": "INSUFFICIENT_EVIDENCE"}),
    ({"action": {"RAISE_STOP_AND_TARGET": 0.7}, "stop_option": {"first": 0.4, "last": 0.4},
      "target_option": {"KEEP": 0.45, "first": 0.45}}, {"stop": "TIED", "target": "TIED"}),
])
def test_under_v2_a_raise_with_no_usable_option_is_held(mt, exact, why):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.exact = exact
    reviewed(mt, kit)
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"]) == ("HELD", "NO_USABLE_OPTION")
    for kind, reason in why.items():
        assert decision["option_use"][kind]["reason"] == reason
        assert decision["option_use"][kind]["code"] == kind.upper() + "_OPTION_NOT_USABLE"
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    assert maintenance_level_changes(engine.repo, sid) == []


def test_under_v2_a_flag_is_raised_whatever_the_reason(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("FLAG_EARLY_EXIT", reason="INTACT")  # V1 would refuse this.
    [request_id] = reviewed(mt, kit)
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["trade_reason"], decision["flag_created"]) == (
        "FLAGGED", "INTACT", True)
    [flag] = bodies(engine, "EXIT_FLAG_RAISED", sid)
    assert flag["side"] == "JEV" and flag["reasons"]["trade_reason"] == "INTACT"
    assert flag["raised_by"]["request_id"] == request_id
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")


@pytest.mark.parametrize("action", [INSUFFICIENT, "tie"])
def test_under_v2_an_uncertain_action_changes_nothing(mt, action):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.exact = {"action": {INSUFFICIENT: 0.7} if action == INSUFFICIENT
                     else {"HOLD": 0.4, "RAISE_STOP": 0.4}, "stop_option": {"first": 0.8}}
    reviewed(mt, kit)
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", "UNCERTAIN_JUDGMENT")
    assert decision["review_status"] == "NEEDS_REVIEW"
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")


def test_a_v2_stop_raise_is_applied_and_replaced_by_patch_under_its_own_authorization(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    # The stop answer is unique; the target answer tied (unused by RAISE_STOP: no matter).
    kit.jev.exact = {"action": {"RAISE_STOP": 0.6}, "stop_option": {"first": 0.55},
                     "target_option": {"KEEP": 0.4, "first": 0.4}}
    [request_id] = reviewed(mt, kit)
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["stop"]["new"]) == ("APPLIED", "103.00")
    assert decision["option_use"]["target"]["reason"] == "NOT_RAISED_BY_ACTION"
    assert decision["review_status"] == "NEEDS_REVIEW"  # The transport's whole-answer flag.
    [old] = stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "106"))
    [amend] = decisions(engine, sid, "AMEND")
    assert (amend["method"], amend["reason"], amend["claimed"]) == ("PATCH", "REPLACE_STOP", True)
    assert amend["payload"] == {"stop_price": "103.00", "limit_price": "102.99"}
    engine.manage(sid, quote(mt, "106"))
    [replaced] = bodies(engine, "STOP_REPLACED", sid)
    assert (replaced["to_stop"], replaced["change_id"]) == ("103.00", request_id)
    assert state(mt, sid)["stop_replace"] is None and engine.reconcile()["clean"]


def test_a_v2_answer_recovered_after_a_crash_is_read_by_v2(mt, monkeypatch):
    from catalyst_lab.trade_maintenance import TradeMaintenance

    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.exact = dict(ROUND_5)

    def crash(*_args, **_kwargs):
        raise RuntimeError("SIMULATED_CRASH_AFTER_RECEIPT")

    monkeypatch.setattr(TradeMaintenance, "decide", crash)
    reviewed(mt, kit)
    assert not bodies(engine, "MAINTENANCE_DECISION", sid)
    monkeypatch.undo()
    restarted = maintainer(mt, kit.jev, kit.bars)
    assert reviewed(mt, restarted, seconds=2) == []  # Recovery: the recorded answer.
    decision = the_decision(engine, sid)
    assert (decision["outcome"], decision["answer_rule"]) == ("HELD",
                                                              "MAINTENANCE_ANSWER_RULE_V2")
    assert len(kit.jev.calls) == 1


# --- The minute cadence, the in-flight skip and the bound ---------------------------------------


def test_v2_reviews_every_completed_minute_and_never_inside_a_minute(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)  # 30 s into a minute.
    kit = maintainer(mt)
    assert reviewed(mt, kit, bid="101", seconds=10) == []  # No minute completed since open.
    [first] = reviewed(mt, kit, bid="101", seconds=21)  # The next minute completed (+1 s).
    assert bodies(engine, "POSITION_REVIEW_REQUEST", sid)[-1]["trigger"]["reasons"] == [
        "BAR_1M"]
    assert reviewed(mt, kit, bid="101", seconds=30) == []  # The minute was served.
    [second] = reviewed(mt, kit, bid="101", seconds=30)  # The next one, exactly 60 s later.
    assert reviewed(mt, kit, bid="101", seconds=59) == []  # V1's floor: inside the minute.
    [third] = reviewed(mt, kit, bid="101", seconds=1)
    requests = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    assert [r["trigger"]["reasons"] for r in requests] == [["BAR_1M"]] * 3
    assert [len(r["context"]["state"]["review_history"]) for r in requests] == [0, 1, 2]
    assert [r["outcome"] for r in bodies(engine, "MAINTENANCE_DECISION", sid)] == ["HELD"] * 3
    one_minute = [c for c in kit.bars.calls if c[2] == "1Min"]
    assert {c[1] for c in one_minute} == {"SOL/USD"} and {c[3] for c in one_minute} == {60}
    assert len(one_minute) == 3  # One GET per completed minute and coin (cached per minute).


def test_a_minute_that_completes_while_the_review_is_in_flight_is_skipped_and_recorded(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    boundary = cm.floor_time(venue.now, 60) + timedelta(seconds=60)
    venue.now = boundary - timedelta(seconds=10)

    def slow(_body):  # Jev answers after the minute completes.
        venue.now = boundary + timedelta(seconds=4)

    kit.jev.hook = slow
    kit.prices.set("SOL/USD", "106")
    [in_flight] = run_pass(mt, kit)  # +1R: asked 10 s before the minute ends.
    kit.jev.hook = None
    assert the_decision(engine, sid)["decided_at"] == (boundary + timedelta(seconds=4)
                                                       ).isoformat()
    venue.now = boundary + timedelta(seconds=5)
    assert run_pass(mt, kit) == []  # The minute is skipped, never reviewed late.
    [skipped] = bodies(engine, "MAINTENANCE_REVIEW_SKIPPED", sid)
    assert skipped == {
        "policy_id": "CRYPTO_MAINTENANCE_V2", "lifecycle_id": state(mt, sid)["lifecycle_id"],
        "code": "REVIEW_SKIPPED_IN_FLIGHT", "bar_end": boundary.isoformat(),
        "in_flight_request_id": in_flight,
        "in_flight_requested_at": (boundary - timedelta(seconds=10)).isoformat(),
        "in_flight_decided_at": (boundary + timedelta(seconds=4)).isoformat(),
        "recorded_at": (boundary + timedelta(seconds=5)).isoformat()}
    venue.now = boundary + timedelta(seconds=50)
    assert run_pass(mt, kit) == []  # Still that minute: served by the skip.
    assert len(bodies(engine, "MAINTENANCE_REVIEW_SKIPPED", sid)) == 1  # Recorded once.
    venue.now = boundary + timedelta(seconds=61)
    [next_minute] = run_pass(mt, kit)  # The next completed minute is reviewed as usual.
    last = bodies(engine, "POSITION_REVIEW_REQUEST", sid)[-1]
    assert last["trigger"]["reasons"] == ["BAR_1M"]
    assert last["trigger"]["served"]["bar_end"] == (boundary + timedelta(seconds=60)
                                                    ).isoformat()
    assert len(kit.jev.calls) == 2


def open_two(mt):
    """FAR/USD and NEAR/USD open, protected and past their entries (V2)."""
    engine, venue, _ = mt
    fresh_bar(mt)
    far, near = admit_many(mt, ["FAR/USD", "NEAR/USD"])
    for sid, symbol in ((far, "FAR/USD"), (near, "NEAR/USD")):
        trigger(mt, sid)
        order = entry_order(mt, symbol)
        fill(mt, order, order["qty"])
        engine.manage(sid, quote(mt, "100.20"))
        engine.manage(sid, quote(mt, "100.20"))
    return far, near


def test_a_pass_bounds_its_jev_calls_and_the_trade_nearest_its_stop_goes_first(mt):
    engine, venue, _ = mt
    far, near = open_two(mt)
    kit = maintainer(mt, max_inflight=1)  # One call at a time: the second trade waits.
    venue.now = cm.floor_time(venue.now, 60) + timedelta(seconds=61)
    kit.prices.set("FAR/USD", "104")
    kit.prices.set("NEAR/USD", "96.50")  # 1.55% above its stop; FAR is 8.65% above.
    assert len(run_pass(mt, kit)) == 2
    with engine.repo.connect() as conn:
        rows = conn.execute(
            """SELECT event_seq,kind,setup_id,body FROM lab.managed_events
            WHERE kind IN ('POSITION_REVIEW_REQUEST','MAINTENANCE_DECISION')
            ORDER BY event_seq""").fetchall()
    order = [(r["kind"], r["setup_id"]) for r in rows]
    # NEAR is asked and decided before FAR's request is even recorded (its deadline starts
    # only when its turn comes).
    assert order == [("POSITION_REVIEW_REQUEST", near), ("MAINTENANCE_DECISION", near),
                     ("POSITION_REVIEW_REQUEST", far), ("MAINTENANCE_DECISION", far)]
    requests = [r["body"] for r in rows if r["kind"] == "POSITION_REVIEW_REQUEST"]
    assert [r["trigger"]["priority_rank"] for r in requests] == [1, 2]
    assert [b["outcome"] for b in bodies(engine, "MAINTENANCE_DECISION")] == ["HELD", "HELD"]
    with pytest.raises(ValueError, match="MAINTENANCE_MAX_INFLIGHT_INVALID"):
        maintainer(mt, max_inflight=0)
    status = kit.maintenance.status(engine.store.active())
    assert status["policy_id"] == "CRYPTO_MAINTENANCE_V2"
    assert status["open_trades_by_policy"] == {"CRYPTO_MAINTENANCE_V2": 2}
