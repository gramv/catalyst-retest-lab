"""Trade maintenance on the fixture venue (package maintenance, plan 4.6): opening and partial
entries (CRYPTO_PARTIAL_ENTRY_V1), the review cadence, options, checks, stop replacement,
target raises, exit flags and the edge cases of CRYPTO_MAINTENANCE_V1.

Fixture evidence only: disposable per-test databases, the fake paper venue, a scripted bar
source and a mock Jev transport. No broker, provider, network or owner-ledger contact.
"""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import entry_working, exit_flags
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import encoded
from catalyst_lab.maintenance_dossier import STATE_BYTE_BUDGET
from catalyst_lab.managed_dossier import encoded_bytes
from catalyst_lab.managed_ops import maintenance_alarms, status_alarms
from catalyst_lab.managed_review import ManagedContext
from tests.maintenance_fixtures import (
    Bars,
    admit,
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
    to_boundary,
    trigger,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_trade_plan_admission as pre_trade_plan_admission
from tests.maintenance_fixtures import v1_admission as v1_admission
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import observation, packet

# Every setup here is admitted as before package trade-plan (CRYPTO_MAINTENANCE_V3 or earlier,
# the one-tick stop-limit); the new versions are tests/test_trade_plan_*.py.
pytestmark = pytest.mark.usefixtures("pre_trade_plan_admission")


def state(mt, sid):
    return mt[0]._load(sid)[1]


# --- Admission scope -----------------------------------------------------------------------------

def test_admission_records_maintenance_in_the_managed_arm_and_partial_entry_in_both(
        mt, monkeypatch):
    engine = mt[0]
    maintained = admit(mt, "SOL/USD")
    recorded = state(mt, maintained)
    assert recorded["arm"] == "JEV_MANAGED"
    # CRYPTO_MAINTENANCE_V2 from package answer-rules (V1 plus its answer rule), V3 from package
    # jev-budget (V2 plus the monthly budget's cadence).
    assert recorded["maintenance_policy"] == cm.CRYPTO_MAINTENANCE_V3.record()
    assert recorded["partial_entry_policy"] == cm.CRYPTO_PARTIAL_ENTRY.record()
    older = engine.admit(packet(mt, "OLD/USD"))  # A report-V2 crypto pick: unchanged.
    assert "maintenance_policy" not in state(mt, older)
    assert "partial_entry_policy" not in state(mt, older)
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    control = admit(mt, "ETH/USD")
    assert state(mt, control)["arm"] == "FIXED_EXIT"
    assert "maintenance_policy" not in state(mt, control)  # The control arm (plan 4.6.6).
    # Both arms open the same way (plan 4.6.1): the control arm keeps the partial-entry rule.
    assert state(mt, control)["partial_entry_policy"] == cm.CRYPTO_PARTIAL_ENTRY.record()


# --- Opening (plan 4.6.1) ------------------------------------------------------------------------

def test_a_full_fill_is_protected_at_once_and_the_open_is_recorded(mt):
    engine, venue, _ = mt
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    first_fill = venue.now
    fill(mt, order, order["qty"])
    engine.manage(sid, quote(mt, "100.20"))
    [stop] = stop_orders(mt, "SOL/USD")
    assert (stop["stop_price"], stop["limit_price"], stop["qty"]) == ("95", "94.99", order["qty"])
    engine.manage(sid, quote(mt, "100.20"))
    [opened] = bodies(engine, "MAINTENANCE_OPENED", sid)
    risk = D("5.10")
    assert D(opened["avg_entry"]) == D("100.10") and D(opened["filled_qty"]) == D(order["qty"])
    assert (opened["initial_stop"], opened["initial_target"], opened["max_entry"]) == (
        "95", "111", "100.10")
    assert D(opened["risk_per_coin"]) == risk
    # (T - M) / (M - S) and (T - E) / R: 10.90 / 5.10, exact to 80 digits in the record.
    assert D(opened["reward_risk_at_max_entry"]).quantize(D("0.000001")) == D("2.137255")
    assert D(opened["target_r_from_entry"]).quantize(D("0.000001")) == D("2.137255")
    assert opened["first_24h_review_at"] == (first_fill + timedelta(hours=24)).isoformat()
    [completed] = bodies(engine, "MAINTENANCE_ENTRY_COMPLETED", sid)
    assert completed["outcome"] == "FILLED" and completed["cancel_reason"] is None
    engine.manage(sid, quote(mt, "100.20"))
    assert len(bodies(engine, "MAINTENANCE_OPENED", sid)) == 1  # Once per lifecycle.


def test_a_partial_fill_is_protected_at_once_and_the_rest_works_ten_minutes(mt, monkeypatch):
    # Admitted before CRYPTO_ENTRY_WORKING_LIMIT_V1, which ends the rest at its 300 s fill window
    # (tests/test_entry_working.py); this setup keeps the partial-entry rule's ten minutes.
    monkeypatch.setattr(entry_working, "admission_fields", lambda packet: {})
    engine, venue, _ = mt
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    first_fill = venue.now
    fill(mt, order, "5")
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert order["status"] == "partially_filled"  # The rest keeps working.
    [first] = stop_orders(mt, "SOL/USD")
    assert (first["qty"], first["stop_price"]) == ("5", "95")  # The filled part, at once.
    [retained] = bodies(engine, "PARTIAL_ENTRY_RETAINED", sid)
    assert retained["deadline"] == (first_fill + timedelta(minutes=10)).isoformat()
    assert not bodies(engine, "MAINTENANCE_ENTRY_COMPLETED", sid)
    venue.now = first_fill + timedelta(minutes=4)
    fill(mt, order, "10")
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert sorted(o["qty"] for o in stop_orders(mt, "SOL/USD")) == ["10", "5"]
    venue.now = first_fill + timedelta(minutes=9, seconds=59)
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert order["status"] == "partially_filled"
    venue.now = first_fill + timedelta(minutes=10)
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert order["status"] == "canceled"  # Exactly ten minutes after the first fill.
    [cancel] = [d for d in decisions(engine, sid, "CANCEL")]
    assert cancel["reason"] == "PARTIAL_ENTRY_TIMEOUT" and cancel["claimed"]
    assert cancel["path"] == "/v2/orders/" + order["id"] and cancel["method"] == "DELETE"
    assert state(mt, sid)["partial_entry_cancel"] == "PARTIAL_ENTRY_TIMEOUT"
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    [completed] = bodies(engine, "MAINTENANCE_ENTRY_COMPLETED", sid)
    assert completed["outcome"] == "REMAINDER_CANCELLED"
    assert D(completed["filled_qty"]) == D(15)
    assert completed["cancel_reason"] == "PARTIAL_ENTRY_TIMEOUT"
    assert verify_events(engine.repo.export_events())["valid"]


@pytest.mark.parametrize("bid,ask,reason", [
    ("100.10", "100.11", "PARTIAL_ENTRY_ABOVE_MAX_ENTRY"),
    ("95", "95.05", "PARTIAL_ENTRY_AT_OR_BELOW_STOP"),
])
def test_the_rest_of_a_partial_entry_is_cancelled_when_price_leaves_the_zone(mt, bid, ask, reason):
    engine, venue, _ = mt
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    fill(mt, order, "5")
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert order["status"] == "partially_filled"
    venue.now += timedelta(minutes=2)
    engine.manage(sid, quote(mt, bid, ask))
    assert order["status"] == "canceled"
    assert [d["reason"] for d in decisions(engine, sid, "CANCEL")] == [reason]
    [event] = bodies(engine, "PARTIAL_ENTRY_REMAINDER_CANCEL", sid)
    assert (event["reason"], event["bid"], event["ask"]) == (reason, bid, ask)


def test_a_protection_refused_while_the_rest_works_cancels_the_rest_then_protects(mt):
    engine, venue, _ = mt
    venue.refuse_stop_while_buy_open = True
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    fill(mt, order, "5")
    engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert not stop_orders(mt, "SOL/USD")  # The venue refused the stop-limit.
    [refused] = bodies(engine, "PARTIAL_ENTRY_PROTECTION_REFUSED", sid)
    assert refused["fallback"] == "CANCEL_ENTRY_REMAINDER_THEN_PROTECT"
    current = state(mt, sid)
    assert current["partial_entry_cancel"] == "PARTIAL_ENTRY_PROTECTION_REFUSED"
    assert not current.get("exit_requested")  # Not today's PROTECTION_REJECTED flatten.
    plan = engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert (plan.state, plan.reason) == ("CANCELING", "CANCEL_ENTRY_BEFORE_PROTECT")
    assert order["status"] == "canceled" and not stop_orders(mt, "SOL/USD")
    plan = engine.manage(sid, quote(mt, "100.05", "100.08"))
    assert plan.state == "PROTECTION_REQUIRED"
    [stop] = stop_orders(mt, "SOL/USD")
    assert stop["qty"] == "5" and not venue.orders_of("sell", "market")
    assert [d["reason"] for d in decisions(engine, sid, "CANCEL")] == [
        "PARTIAL_ENTRY_PROTECTION_REFUSED"]


# The fixture venue refuses a sell stop-limit while a buy of the coin is open, as Alpaca paper's
# wash-trade guard did live (CRV 2026-09-30 00:15 UTC, PEPE 19:15 UTC).
ABOVE_M = ("100.08", "100.11")  # A fresh quote with the ask above the max entry (100.10).


def test_a_remainder_cancelled_at_the_first_pass_is_cancelled_before_protection_is_sent(mt):
    """PEPE/USD, 2026-09-30 19:15 UTC (setup 0c8755e4): a partial fill, and the next pass finds
    the ask above M. That pass sent the stop-limit with the remainder's cancel; the venue refused
    the sell while the buy worked, and the refusal flattened the filled part at market. Now the
    remainder is cancelled first and the stop-limit follows once the entry is gone: nothing is
    refused and nothing is sold."""
    engine, venue, _ = mt
    venue.refuse_stop_while_buy_open = True
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    fill(mt, order, "5")
    plan = engine.manage(sid, quote(mt, *ABOVE_M))
    assert (plan.state, plan.reason) == ("CANCELING", "CANCEL_ENTRY_BEFORE_PROTECT")
    assert [d["reason"] for d in decisions(engine, sid, "CANCEL")] == [
        "PARTIAL_ENTRY_ABOVE_MAX_ENTRY"]
    assert order["status"] == "canceled" and not decisions(engine, sid, "PROTECT")
    venue.now += timedelta(seconds=1)
    plan = engine.manage(sid, quote(mt, *ABOVE_M))
    assert (plan.state, plan.reason) == ("PROTECTION_REQUIRED", "UNCOVERED_BROKER_INVENTORY")
    [stop] = stop_orders(mt, "SOL/USD")
    assert (stop["qty"], stop["stop_price"], stop["limit_price"]) == ("5", "95", "94.99")
    plan = engine.manage(sid, quote(mt, *ABOVE_M))
    assert (plan.state, plan.reason) == ("PROTECTED", "NATIVE_STOP_LIMIT_PRESENT")
    current = state(mt, sid)
    assert (current["state"], current.get("exit_requested"), current["partial_entry_cancel"]) == (
        "OPEN", None, "PARTIAL_ENTRY_ABOVE_MAX_ENTRY")
    assert not venue.orders_of("sell", "market") and not bodies(engine, "BROKER_REJECTED", sid)
    [completed] = bodies(engine, "MAINTENANCE_ENTRY_COMPLETED", sid)
    assert (completed["outcome"], completed["cancel_reason"]) == (
        "REMAINDER_CANCELLED", "PARTIAL_ENTRY_ABOVE_MAX_ENTRY")
    assert verify_events(engine.repo.export_events())["valid"]


def test_no_protection_is_sent_while_the_remainder_cancel_is_in_flight(mt):
    engine, venue, _ = mt
    venue.refuse_stop_while_buy_open = True
    venue.defer_cancel = True  # The cancel is accepted; the order stays pending_cancel.
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    fill(mt, order, "5")
    plan = engine.manage(sid, quote(mt, *ABOVE_M))
    assert (plan.reason, order["status"]) == ("CANCEL_ENTRY_BEFORE_PROTECT", "pending_cancel")
    for _ in range(3):
        venue.now += timedelta(seconds=1)
        plan = engine.manage(sid, quote(mt, *ABOVE_M))
        assert (plan.state, plan.reason, plan.proposals) == (
            "CANCELING", "CANCEL_ENTRY_BEFORE_PROTECT", ())
    assert len(decisions(engine, sid, "CANCEL")) == 1 and not decisions(engine, sid, "PROTECT")
    order["status"] = "canceled"  # The broker completes the cancel.
    venue.now += timedelta(seconds=1)
    assert engine.manage(sid, quote(mt, *ABOVE_M)).state == "PROTECTION_REQUIRED"
    [stop] = stop_orders(mt, "SOL/USD")
    assert stop["qty"] == "5" and not venue.orders_of("sell", "market")
    assert state(mt, sid).get("exit_requested") is None


def test_the_app_watched_stop_covers_the_position_until_the_stop_limit_rests(mt):
    """While the remainder's cancel is in flight the position has no stop-limit at the broker; the
    setup's stop rule (here CRYPTO_STOP_BREACH_V2) still sells it: a print at the stop establishes
    the breach and the fallback sells at market 5 s later, once the entry is gone."""
    engine, venue, _ = mt
    venue.refuse_stop_while_buy_open = True
    venue.defer_cancel = True
    sid = admit(mt, "SOL/USD")
    assert state(mt, sid)["stop_breach_version"] == "CRYPTO_STOP_BREACH_V2"
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    fill(mt, order, "5")
    assert engine.manage(sid, quote(mt, *ABOVE_M)).reason == "CANCEL_ENTRY_BEFORE_PROTECT"
    venue.now += timedelta(seconds=1)
    assert engine.manage(sid, quote(mt, "94.90", "95.10")).reason == "CANCEL_ENTRY_BEFORE_PROTECT"
    [breach] = bodies(engine, "STOP_BREACH_ESTABLISHED", sid)
    assert breach["breach_evidence"] == "TRADE_PRINT"
    venue.now += timedelta(seconds=5)
    plan = engine.manage(sid, quote(mt, "94.90", "95.10"))
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_LIMIT_NOT_FILLED")
    assert state(mt, sid)["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    order["status"] = "canceled"
    plan = engine.manage(sid, quote(mt, "94.90", "95.10"))
    assert plan.state == "EXIT_REQUIRED"
    [sell] = venue.orders_of("sell", "market")
    assert sell["qty"] == "5" and not decisions(engine, sid, "PROTECT")


@pytest.mark.parametrize("cancel_in_flight", [False, True])
def test_a_protection_refused_while_an_entry_worked_is_sent_again_and_never_flattens(
        mt, monkeypatch, cancel_in_flight):
    """The refusal is judged on the broker view its POST was authorized on, not on the state when
    the refusal is handled. Replayed with PEPE's plan of 2026-09-30 (the stop-limit and the
    remainder's cancel in one plan: the planner's sequencing for setups without the rule): the
    state already records the remainder's cancel when the refusal arrives, which took the
    PROTECTION_REJECTED flatten. Now the refusal records PARTIAL_ENTRY_PROTECTION_REFUSED with the
    entry orders the POST saw, the recorded cancel reason stays, and protection is sent again
    under a fresh client order ID once the entry is gone. With the cancel in flight, the next
    plan's stop-limit (sent while the buy is pending_cancel) is refused and retried the same way."""
    from catalyst_lab import crypto_execution

    planner = crypto_execution.plan_crypto_recovery

    def one_plan(*args, retain_entry=None, **options):
        return planner(*args, **options)

    monkeypatch.setattr(crypto_execution, "plan_crypto_recovery", one_plan)
    engine, venue, _ = mt
    venue.refuse_stop_while_buy_open = True
    venue.defer_cancel = cancel_in_flight
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    fill(mt, order, "5")
    plan = engine.manage(sid, quote(mt, *ABOVE_M))
    assert [p.action for p in plan.proposals] == ["CRYPTO_PROTECT", "CRYPTO_CANCEL"]
    current = state(mt, sid)
    assert current.get("exit_requested") is None  # Not the PROTECTION_REJECTED flatten.
    assert current["partial_entry_cancel"] == "PARTIAL_ENTRY_ABOVE_MAX_ENTRY"  # Recorded once.
    [refused] = bodies(engine, "PARTIAL_ENTRY_PROTECTION_REFUSED", sid)
    assert refused["entry_orders"] == [{"id": order["id"], "status": "partially_filled"}]
    if cancel_in_flight:
        assert order["status"] == "pending_cancel"
        venue.now += timedelta(seconds=1)
        engine.manage(sid, quote(mt, *ABOVE_M))
        second = bodies(engine, "PARTIAL_ENTRY_PROTECTION_REFUSED", sid)[1]
        assert second["entry_orders"] == [{"id": order["id"], "status": "pending_cancel"}]
        assert state(mt, sid).get("exit_requested") is None
        order["status"] = "canceled"
    assert order["status"] == "canceled" and not stop_orders(mt, "SOL/USD")
    venue.now += timedelta(seconds=1)
    engine.manage(sid, quote(mt, *ABOVE_M))
    [stop] = stop_orders(mt, "SOL/USD")
    *refused_ids, sent = [d["payload"]["client_order_id"]
                          for d in decisions(engine, sid, "PROTECT")]
    assert stop["client_order_id"] == sent and sent not in refused_ids
    assert len(refused_ids) == (2 if cancel_in_flight else 1)
    assert stop["qty"] == "5" and not venue.orders_of("sell", "market")
    assert state(mt, sid).get("exit_requested") is None


@pytest.mark.parametrize("partial", [False, True])
def test_a_protection_refused_with_no_entry_working_still_flattens(mt, partial):
    """CRYPTO_PARTIAL_ENTRY_V1 keeps today's PROTECTION_REJECTED flatten for a refusal while no
    entry order works: the venue refuses every stop-limit, after a full fill or after the rest of
    a partial fill was cancelled first."""
    engine, venue, _ = mt
    sid = admit(mt, "SOL/USD")
    trigger(mt, sid)
    order = entry_order(mt, "SOL/USD")
    if partial:
        fill(mt, order, "5")
        assert engine.manage(sid, quote(mt, *ABOVE_M)).reason == "CANCEL_ENTRY_BEFORE_PROTECT"
        assert order["status"] == "canceled"
        venue.now += timedelta(seconds=1)
    else:
        fill(mt, order, order["qty"])
    venue.reject_protection = True
    engine.manage(sid, quote(mt, *ABOVE_M))
    assert len(bodies(engine, "BROKER_REJECTED", sid)) == 1
    assert not bodies(engine, "PARTIAL_ENTRY_PROTECTION_REFUSED", sid)
    assert state(mt, sid)["exit_requested"] == "PROTECTION_REJECTED"
    venue.now += timedelta(seconds=1)
    assert engine.manage(sid, quote(mt, *ABOVE_M)).state == "EXIT_REQUIRED"
    [sell] = venue.orders_of("sell", "market")
    assert D(sell["qty"]) == (D(5) if partial else D(order["qty"]))


def test_older_setups_keep_todays_partial_fill_and_the_control_arm_opens_like_the_managed(
        mt, monkeypatch):
    # Admitted before CRYPTO_ENTRY_WORKING_LIMIT_V1 (its fill window would end the control arm's
    # rest at 300 s on any pass in between: tests/test_entry_working.py).
    monkeypatch.setattr(entry_working, "admission_fields", lambda packet: {})
    engine, venue, _ = mt
    older = engine.admit(packet(mt, "OLD/USD"))
    engine.observe_trigger(older, observation(mt))
    order = entry_order(mt, "OLD/USD")
    fill(mt, order, "1")
    engine.manage(older, quote(mt, "100.05", "100.08"))
    assert order["status"] == "canceled"  # Cancelled at once, as today.
    [cancel] = decisions(engine, older, "CANCEL")
    assert cancel["reason"] == "CANCEL_REMAINING_ENTRY"
    assert not bodies(engine, "PARTIAL_ENTRY_RETAINED", older)
    assert not bodies(engine, "MAINTENANCE_OPENED", older)
    # The control arm opens exactly as the maintained arm does (plan 4.6.1 is how every trade
    # opens): the rest of the entry works ten minutes. It is never maintained (plan 4.6.6).
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    control = admit(mt, "CTL/USD")
    trigger(mt, control)
    order = entry_order(mt, "CTL/USD")
    first_fill = venue.now
    fill(mt, order, "1")
    engine.manage(control, quote(mt, "100.05", "100.08"))
    assert order["status"] == "partially_filled"
    [retained] = bodies(engine, "PARTIAL_ENTRY_RETAINED", control)
    assert retained["deadline"] == (first_fill + timedelta(minutes=10)).isoformat()
    venue.now = first_fill + timedelta(minutes=10)
    engine.manage(control, quote(mt, "100.05", "100.08"))
    assert order["status"] == "canceled"
    [cancel] = decisions(engine, control, "CANCEL")
    assert cancel["reason"] == "PARTIAL_ENTRY_TIMEOUT"
    assert not bodies(engine, "MAINTENANCE_OPENED", control)
    assert "maintenance_policy" not in state(mt, control)


# --- The review cadence (plan 4.6.2) --------------------------------------------------------------

def requests(engine, setup_id=None):
    return bodies(engine, "POSITION_REVIEW_REQUEST", setup_id)


def outcomes(engine, setup_id=None):
    return [(b["outcome"], b["code"]) for b in bodies(engine, "MAINTENANCE_DECISION", setup_id)]


def test_a_review_runs_at_every_completed_fifteen_minute_bar_and_only_then(mt, v1_admission):
    # CRYPTO_MAINTENANCE_V1 (15-minute cadence, context V4); V2: test_answer_rules_flows.py.
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.prices.set("SOL/USD", "101")
    opened = venue.now
    boundary = cm.floor_time(opened, 900) + timedelta(seconds=900)
    venue.now = boundary - timedelta(seconds=2)
    assert run_pass(mt, kit) == [] and not kit.jev.calls  # No bar has completed since open.
    venue.now = boundary + timedelta(seconds=1)
    [first] = run_pass(mt, kit)
    [request] = requests(engine, sid)
    assert request["request_id"] == first
    assert request["trigger"]["reasons"] == ["BAR_15M"]
    assert request["trigger"]["served"]["bar_end"] == boundary.isoformat()
    assert len(kit.jev.calls) == 1 and outcomes(engine, sid) == [("HELD", None)]
    venue.now = boundary + timedelta(minutes=5)
    assert run_pass(mt, kit) == []  # The bar was served.
    venue.now = boundary + timedelta(minutes=15, seconds=1)
    assert len(run_pass(mt, kit)) == 1
    assert [r["trigger"]["reasons"] for r in requests(engine, sid)] == [["BAR_15M"], ["BAR_15M"]]
    [judgment, _] = bodies(engine, "MANAGED_JEV_JUDGMENT", sid)
    assert judgment["policy_id"] == "CRYPTO_MAINTENANCE_V1" and judgment["action"] == "HOLD"
    # 15-minute bars once per completed bar, 1-hour bars once per hour (coin and Bitcoin).
    fifteen = [c for c in kit.bars.calls if c[2] == "15Min"]
    assert sorted(c[1] for c in fifteen) == ["BTC/USD", "BTC/USD", "SOL/USD", "SOL/USD"]
    hourly = [c for c in kit.bars.calls if c[2] == "1Hour"]
    assert {c[1] for c in hourly} == {"BTC/USD", "SOL/USD"} and len(hourly) in {2, 4}
    assert {c[3] for c in fifteen} == {96} and {c[3] for c in hourly} == {168}


def test_each_r_milestone_triggers_one_review_the_first_time_it_is_reached(mt, v1_admission):
    # CRYPTO_MAINTENANCE_V1 (15-minute cadence, context V4); V2: test_answer_rules_flows.py.
    engine, venue, _ = mt
    sid = open_trade(mt)  # 30 s into a bar: the next minutes need no bar review.
    kit = maintainer(mt)
    kit.prices.set("SOL/USD", "105.19")  # Entry 100.10, R 5.10: +1R is 105.20.
    assert run_pass(mt, kit) == []
    kit.prices.set("SOL/USD", "105.20")
    assert len(run_pass(mt, kit)) == 1
    [trigger_event] = bodies(engine, "MAINTENANCE_TRIGGER", sid)
    assert (trigger_event["trigger"], trigger_event["level"]) == ("R_MILESTONE", "1")
    assert requests(engine, sid)[-1]["trigger"]["reasons"] == ["R_MILESTONE"]
    venue.now += timedelta(seconds=61)
    kit.prices.set("SOL/USD", "103")
    assert run_pass(mt, kit) == []
    kit.prices.set("SOL/USD", "106")  # +1R again: not the first time.
    venue.now += timedelta(seconds=61)
    assert run_pass(mt, kit) == []
    kit.prices.set("SOL/USD", "115.40")  # +3R at once: +2R and +3R are both first reached.
    venue.now += timedelta(seconds=61)
    assert len(run_pass(mt, kit)) == 1
    assert [b["level"] for b in bodies(engine, "MAINTENANCE_TRIGGER", sid)
            if b["trigger"] == "R_MILESTONE"] == ["1", "2", "3"]


def test_near_the_target_a_review_runs_within_the_minute_near_the_stop_it_waits(mt, v1_admission):
    # CRYPTO_MAINTENANCE_V1 (15-minute cadence, context V4); V2: test_answer_rules_flows.py.
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    to_boundary(mt)
    kit.prices.set("SOL/USD", "101")
    assert len(run_pass(mt, kit)) == 1  # A bar review.
    start = venue.now
    venue.now = start + timedelta(seconds=10)
    kit.prices.set("SOL/USD", "95.40")  # Within 0.5% of the stop (95.475).
    assert run_pass(mt, kit) == []  # Recorded, but inside the minute.
    assert [b["trigger"] for b in bodies(engine, "MAINTENANCE_TRIGGER", sid)] == ["NEAR_STOP"]
    venue.now = start + timedelta(seconds=59)
    kit.prices.set("SOL/USD", "97")
    assert run_pass(mt, kit) == []
    venue.now = start + timedelta(seconds=60)
    assert len(run_pass(mt, kit)) == 1  # Due once the minute has passed, price moved or not.
    assert requests(engine, sid)[-1]["trigger"]["reasons"] == ["NEAR_STOP"]
    near = venue.now + timedelta(seconds=5)
    venue.now = near
    kit.prices.set("SOL/USD", "110.45")  # Within 0.5% of the target (110.445).
    assert len(run_pass(mt, kit)) == 1  # Five seconds after the last review.
    last = requests(engine, sid)[-1]["trigger"]
    assert "NEAR_TARGET" in last["reasons"] and last["near_target_exempt"] is True
    venue.now = near + timedelta(seconds=5)
    assert run_pass(mt, kit) == []  # Once per target level.


def test_agent_news_about_the_coin_triggers_a_review(mt):
    from catalyst_lab.jev_contract import digest
    from catalyst_lab.position_news import PositionNewsService

    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.prices.set("SOL/USD", "101")
    assert run_pass(mt, kit) == []
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    context = service.context(sid)
    excerpt = "Synthetic fixture: the exchange delisted the coin's main pair (test only)."
    service.submit(sid, {
        "news_id": str(uuid4()), "lifecycle_id": context["lifecycle_id"],
        "expected_news_revision": context["news_revision"],
        "sources": [{"source_id": "fixture-news", "url": "https://example.org/news",
                     "excerpt": excerpt, "published_at": venue.now.isoformat(),
                     "retrieved_at": venue.now.isoformat(), "content_hash": digest(excerpt),
                     "primary_source": True, "asset_relevant": True, "novelty": "NEW_FACT",
                     "stance": "ADVERSE"}],
    })
    assert len(run_pass(mt, kit)) == 1
    [request] = requests(engine, sid)
    assert request["trigger"]["reasons"] == ["AGENT_NEWS"]
    [item] = request["context"]["state"]["news_since_entry"]
    assert item["excerpt"] == excerpt and item["stance"] == "ADVERSE"
    assert kit.jev.calls[0]["state"]["news_since_entry"][0]["excerpt"] == excerpt


def test_a_bitcoin_shock_reviews_every_maintained_trade_nearest_to_its_stop_first(mt, v1_admission):
    # CRYPTO_MAINTENANCE_V1 (15-minute cadence, context V4); V2: test_answer_rules_flows.py.
    engine, venue, _ = mt
    fresh_bar(mt)
    far, near = admit_many(mt, ["FAR/USD", "NEAR/USD"])
    for sid, symbol in ((far, "FAR/USD"), (near, "NEAR/USD")):
        trigger(mt, sid)
        order = entry_order(mt, symbol)
        fill(mt, order, order["qty"])
        engine.manage(sid, quote(mt, "100.20"))
        engine.manage(sid, quote(mt, "100.20"))
    kit = maintainer(mt)
    venue.now += timedelta(minutes=1)  # The move below happens while both trades are open.
    kit.prices.set("FAR/USD", "104")
    kit.prices.set("NEAR/USD", "96.50")  # 1.55% above its stop; FAR is 8.65% above.
    window = cm.BenchmarkWindow()
    window.observe(D("60000"), venue.now - timedelta(minutes=10))
    window.observe(D("58201"), venue.now - timedelta(seconds=2))  # 2.998% down: no shock.
    assert run_pass(mt, kit, benchmark=window) == []
    window.observe(D("58200"), venue.now - timedelta(seconds=1))  # Exactly 3% within 15 min.
    assert len(run_pass(mt, kit, benchmark=window)) == 2
    [shock] = bodies(engine, "MAINTENANCE_BTC_SHOCK")
    assert (shock["symbol"], shock["direction"], shock["reference_price"], shock["price"]) == (
        "BTC/USD", "DOWN", "60000", "58200")
    reviewed = requests(engine)
    assert [r["context"]["identity"]["position_id"] for r in reviewed] == [str(near), str(far)]
    assert [r["trigger"]["priority_rank"] for r in reviewed] == [1, 2]
    assert all(r["trigger"]["reasons"] == ["BTC_SHOCK"] for r in reviewed)
    assert len(kit.jev.calls) == 2
    venue.now += timedelta(minutes=2)
    assert run_pass(mt, kit, benchmark=window) == []  # One move is one shock.


# --- Options, checks and applying at the venue (plan 4.6.2) --------------------------------------

def reviewed_at(mt, kit, bid="106"):
    """One review of the SOL/USD trade at ``bid`` (+1R reached at 105.20 triggers it)."""
    mt[1].now += timedelta(seconds=1)
    kit.prices.set("SOL/USD", bid)
    return run_pass(mt, kit)


def mutations(venue):
    return [(method, path) for method, path, _ in venue.calls if method != "GET"]


def test_a_stop_raise_is_checked_applied_and_replaces_the_resting_stop_limit_by_patch(
        mt, v1_admission):
    # CRYPTO_MAINTENANCE_V1 (15-minute cadence, context V4); V2: test_answer_rules_flows.py.
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    before = mutations(venue)
    [request_id] = reviewed_at(mt, kit)
    sent = kit.jev.calls[0]
    assert sent["state"]["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V4"
    assert [(o["option_id"], o["price"], o["bases"]) for o in sent["state"]["options"]["stop"]] \
        == [("S1", "103.00", ["SWING_LOW_15M"]), ("S2", "102.51", ["SWING_LOW_15M"]),
            ("S3", "101.33", ["SWING_LOW_1H"]), ("S4", "100.10", ["BREAKEVEN"])]
    assert [o["price"] for o in sent["state"]["options"]["target"]] == [
        "112.35", "113.20", "114.30"]
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"], decision["action"]) == (
        "APPLIED", None, "RAISE_STOP")
    assert decision["stop"] == {"old": "95", "new": "103.00", "option_id": "S1",
                                "bases": ["SWING_LOW_15M"]}
    assert decision["target"]["new"] is None
    assert decision["quote"]["bid"] == "106" and decision["quote"]["ask"] == "106.03"
    assert decision["receipt_ids"] and decision["answered_at"] and decision["decided_at"]
    assert decision["trigger_reasons"] == ["R_MILESTONE"]
    assert decision["levels_after"] == {"stop": "103.00", "target": "111"}
    assert decision["stop_replace_path"] == "PATCH_REPLACE"
    current = state(mt, sid)
    assert current["stop"] == "103.00" and current["stop_replace"]["path"] == "PATCH_REPLACE"
    assert mutations(venue) == before  # Maintenance itself never touches the broker.
    [old] = stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "106"))
    [amend] = decisions(engine, sid, "AMEND")
    assert (amend["method"], amend["path"], amend["reason"], amend["claimed"]) == (
        "PATCH", "/v2/orders/" + old["id"], "REPLACE_STOP", True)
    assert amend["payload"] == {"stop_price": "103.00", "limit_price": "102.99"}
    assert old["status"] == "replaced"
    [new] = stop_orders(mt, "SOL/USD")
    assert (new["stop_price"], new["limit_price"], new["qty"]) == ("103.00", "102.99", old["qty"])
    engine.manage(sid, quote(mt, "106"))
    [replaced] = bodies(engine, "STOP_REPLACED", sid)
    assert (replaced["path"], replaced["from_stop"], replaced["to_stop"], replaced["change_id"]) \
        == ("PATCH_REPLACE", "95", "103.00", request_id)
    assert replaced["orders"] == [new["id"]]
    assert state(mt, sid)["stop_replace"] is None
    engine.manage(sid, quote(mt, "106"))
    assert len(decisions(engine, sid, "AMEND")) == 1 and len(stop_orders(mt, "SOL/USD")) == 1
    assert engine.reconcile()["clean"]  # The replacement order is owned.
    assert verify_events(engine.repo.export_events())["valid"]


class RestQuoteBars(Bars):
    """The fixture bars plus a REST latest quote (bid 106, ask 106.03) one second old."""

    def _latest(self, market, symbols, kind):
        self.quote_reads.append((market, tuple(symbols), kind))
        at = self.venue.now - timedelta(seconds=1)
        return {s: SimpleNamespace(bid=D("106"), ask=D("106.03"), timestamp=at)
                for s in symbols}, []


def test_a_slow_answer_reads_its_own_quote_and_its_stop_raise_still_applies(mt, v1_admission):
    """2026-09-28: with the stream quiet (its last quote older than 5 s), the request's quote
    came from the pass's one REST read. An answer slower than that read's 5 s reuse window
    found no read left and recorded no quote, so a stop raise was refused
    CURRENT_QUOTE_UNAVAILABLE. The decision now reads its own quote."""
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, bars=RestQuoteBars(venue))
    kit.jev.answer("RAISE_STOP", stop="first")
    kit.jev.hook = lambda body: setattr(venue, "now", venue.now + timedelta(seconds=6))
    venue.now += timedelta(seconds=1)
    quiet = venue.now - timedelta(seconds=30)

    def quiet_stream(setup):  # The stream's last quote arrived 30 s ago.
        return {"bid": "106", "ask": "106.03", "quote_at": quiet.isoformat(),
                "quote_received_at": quiet, "trade_price": "106", "trade_at": quiet.isoformat(),
                "trade_id": "fixture", "feed_healthy": True, "data_provider": "LAB_FIXTURE",
                "data_feed": "FIXTURE"}

    kit.prices = quiet_stream
    run_pass(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"], decision["action"]) == (
        "APPLIED", None, "RAISE_STOP")
    assert decision["quote"]["quote_source"] == "ALPACA_REST_LATEST_QUOTE"
    assert decision["quote"]["bid"] == "106"
    assert state(mt, sid)["stop"] == "103.00"
    # One REST read for the request, one for the decision six seconds later.
    assert [kind for _, _, kind in kit.bars.quote_reads] == ["quotes", "quotes"]


def test_a_refused_replace_falls_back_to_cancel_then_place(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed_at(mt, kit)
    venue.reject_patch = True
    [old] = stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "106"))  # The replace is refused: fall back.
    [fallback] = bodies(engine, "STOP_REPLACE_FALLBACK", sid)
    assert (fallback["from_path"], fallback["to_path"]) == ("PATCH_REPLACE", "CANCEL_THEN_PLACE")
    current = state(mt, sid)
    assert current["stop"] == "103.00"  # The raised stop stands.
    assert current["stop_replace"]["path"] == "CANCEL_THEN_PLACE"
    assert current["stop_replace"]["patch_refused"]["reason"]
    engine.manage(sid, quote(mt, "106"))
    assert old["status"] == "canceled"
    [cancel] = decisions(engine, sid, "CANCEL")
    assert (cancel["reason"], cancel["claimed"]) == ("TIGHTEN_STOP", True)
    engine.manage(sid, quote(mt, "106"))
    [new] = stop_orders(mt, "SOL/USD")
    assert (new["stop_price"], new["limit_price"]) == ("103.00", "102.99")
    engine.manage(sid, quote(mt, "106"))
    [replaced] = bodies(engine, "STOP_REPLACED", sid)
    assert replaced["path"] == "CANCEL_THEN_PLACE" and replaced["patch_refused"]
    assert not venue.orders_of("sell", "market")
    assert engine.reconcile()["clean"]


def test_while_no_stop_rests_the_app_sells_at_market_if_the_new_stop_is_crossed(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed_at(mt, kit)
    venue.reject_patch = True
    engine.manage(sid, quote(mt, "106"))
    engine.manage(sid, quote(mt, "106"))  # The old stop-limit is cancelled: a gap.
    assert not stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "102.99"))  # At or below the raised stop.
    assert state(mt, sid)["exit_requested"] == "STOP_CROSSED_DURING_REPLACE"
    [crossed] = bodies(engine, "STOP_CROSSED_DURING_REPLACE", sid)
    assert crossed["bid"] == "102.99" and crossed["to_stop"] == "103.00"
    [close] = venue.orders_of("sell", "market")
    assert D(close["qty"]) == D(entry_order(mt, "SOL/USD")["qty"])  # The whole position.
    [exit_decision] = decisions(engine, sid, "EXIT")
    assert exit_decision["claimed"] and exit_decision["reason"] == "AUTHORIZED_EXIT"


def test_a_target_raise_is_instant_and_the_target_sells_the_whole_position(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_TARGET", target="first")
    before = mutations(venue)
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert decision["outcome"] == "APPLIED"
    assert decision["target"] == {"old": "111", "new": "112.35", "option_id": "T1",
                                  "bases": ["HIGH_24H"]}
    assert state(mt, sid)["target"] == "112.35" and state(mt, sid).get("stop_replace") is None
    assert mutations(venue) == before
    engine.manage(sid, quote(mt, "111.50"))  # Above the old target: no exit.
    assert not state(mt, sid).get("exit_requested") and not venue.orders_of("sell", "market")
    engine.manage(sid, quote(mt, "112.35"))  # The raised target is reached.
    assert state(mt, sid)["exit_requested"] == "TARGET_EXIT"
    engine.manage(sid, quote(mt, "112.35"))
    [close] = venue.orders_of("sell", "market")
    assert D(close["qty"]) == D(entry_order(mt, "SOL/USD")["qty"])  # No partial sale.


def test_both_levels_are_raised_together_or_not_at_all(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP_AND_TARGET", stop="last", target="last")
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert decision["outcome"] == "APPLIED"
    assert (decision["stop"]["new"], decision["target"]["new"]) == ("100.10", "114.30")
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("100.10", "114.30")


def move_during_answer(mt, kit, sid, *, bid, manage_bid=None):
    """While Jev answers, the market moves (and, with ``manage_bid``, the protection loop sees
    that price once, recording its per-second sample)."""
    def hook(_body):
        mt[1].now += timedelta(seconds=2)
        if manage_bid is not None:
            mt[0].manage(sid, quote(mt, manage_bid))
        kit.prices.set("SOL/USD", bid)

    kit.jev.hook = hook


@pytest.mark.parametrize("bid,manage_bid,code", [
    ("103.40", None, "STOP_TOO_CLOSE_TO_BID"),  # 103.00 is only 0.39% below 103.40.
    ("106", "102.95", "STOP_LEVEL_CROSSED"),  # The bid reached 103.00 meanwhile.
])
def test_a_stop_raise_that_fails_a_check_is_refused_and_the_trade_keeps_its_levels(
        mt, bid, manage_bid, code):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    move_during_answer(mt, kit, sid, bid=bid, manage_bid=manage_bid)
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", code)
    assert decision["stop"]["new"] == "103.00" and decision["quote"]["bid"] == bid
    assert (state(mt, sid)["stop"], state(mt, sid).get("stop_replace")) == ("95", None)
    engine.manage(sid, quote(mt, bid))
    assert not decisions(engine, sid, "AMEND") and len(stop_orders(mt, "SOL/USD")) == 1


@pytest.mark.parametrize("script,code", [
    ({"action": "RAISE_STOP", "stop": "KEEP"}, "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    ({"action": "HOLD", "reason": "BROKEN"}, "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    ({"action": "Insufficient evidence"}, "UNCERTAIN_JUDGMENT"),
])
def test_an_uncertain_or_inconsistent_answer_changes_nothing(mt, v1_admission, script, code):
    """CRYPTO_MAINTENANCE_V1's consistency rules (a setup that recorded V1); V2's reading of
    the same answers: tests/test_answer_rules_flows.py."""
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer(script["action"], stop=script.get("stop", "KEEP"),
                   reason=script.get("reason", "INTACT"))
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", code)
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")


def test_an_answer_recovered_after_a_crash_is_refused_once_a_minute_old(mt, monkeypatch):
    from catalyst_lab.trade_maintenance import TradeMaintenance

    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")

    def crash(*_args, **_kwargs):
        raise RuntimeError("SIMULATED_CRASH_AFTER_RECEIPT")

    monkeypatch.setattr(TradeMaintenance, "decide", crash)
    reviewed_at(mt, kit)
    assert not bodies(engine, "MAINTENANCE_DECISION", sid)  # The receipt exists; no decision.
    monkeypatch.undo()
    venue.now += timedelta(seconds=61)
    restarted = maintainer(mt, kit.jev, kit.bars)
    restarted.prices.set("SOL/USD", "106")
    assert run_pass(mt, restarted) == []  # Recovery answers; it never asks for a second vote.
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", "ANSWER_TOO_OLD")
    assert len(kit.jev.calls) == 1 and state(mt, sid)["stop"] == "95"


# --- Early exit flag, Jev side (plan 4.6.3) -------------------------------------------------------

def test_a_jev_exit_flag_is_recorded_and_alerted_and_the_trade_keeps_its_levels(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("FLAG_EARLY_EXIT", reason="BROKEN")
    [request_id] = reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["trade_reason"], decision["flag_created"]) == (
        "FLAGGED", "BROKEN", True)
    [flag] = bodies(engine, "EXIT_FLAG_RAISED", sid)
    assert flag["flag_id"] == decision["flag_id"] and flag["side"] == "JEV"
    assert flag["flag_version"] == "EARLY_EXIT_FLAG_V1"
    assert flag["raised_by"]["request_id"] == request_id and flag["raised_by"]["receipt_ids"]
    assert flag["reasons"]["trade_reason"] == "BROKEN"
    assert flag["evidence"]["levels"] == {"stop": "95", "target": "111"}
    assert flag["evidence"]["quote"]["bid"] == "106"
    assert flag["answer_due_at"] == (venue.now + timedelta(minutes=15)).isoformat()
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    assert not state(mt, sid).get("exit_requested")
    status = kit.maintenance.status(engine.store.active())
    assert status["pending_exit_flags"] == 1 and status["exit_flags"][0]["side"] == "JEV"
    assert maintenance_alarms(status) == ["EXIT_FLAG_PENDING"]
    assert "EXIT_FLAG_PENDING" in status_alarms({"trade_maintenance": status}, venue.now, {
        "tick_max_age_seconds": 1, "reconciliation_max_age_seconds": 1,
        "research_max_age_seconds": 1})
    to_boundary(mt)
    [second] = run_pass(mt, kit)  # Still broken: the pending flag is reaffirmed, not repeated.
    assert bodies(engine, "MAINTENANCE_DECISION", sid)[-1]["flag_created"] is False
    assert len(bodies(engine, "EXIT_FLAG_RAISED", sid)) == 1
    # Phase 6 resolves it through the same helpers: both sides agree, so the trade exits.
    with engine.store.transaction() as conn:
        [pending] = exit_flags.pending_exit_flags(conn, setup_id=sid)
        exit_flags.resolve_exit_flag(
            engine.store, conn, flag_id=pending["body"]["flag_id"], outcome="EXIT_AGREED",
            answered_by={"side": "AGENT", "agent_id": "fixture-agent"},
            answer={"exit": True}, resolved_at=venue.now,
        )
    with engine.store.transaction() as conn, pytest.raises(ValueError,
                                                           match="EXIT_FLAG_ALREADY_RESOLVED"):
        exit_flags.resolve_exit_flag(engine.store, conn, flag_id=pending["body"]["flag_id"],
                                     outcome="EXIT_NOT_AGREED", answered_by={}, answer={},
                                     resolved_at=venue.now)
    assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"
    assert kit.maintenance.status(engine.store.active())["pending_exit_flags"] == 0
    engine.manage(sid, quote(mt, "106"))
    engine.manage(sid, quote(mt, "106"))
    [close] = venue.orders_of("sell", "market")
    assert decisions(engine, sid, "EXIT")[0]["claimed"] and close["type"] == "market"


# --- Edge cases (plan 4.6.7) ---------------------------------------------------------------------

def test_price_gapping_through_the_stop_during_a_review_discards_the_review(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    move_during_answer(mt, kit, sid, bid="94.80", manage_bid="94.80")
    [request_id] = reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("DISCARDED", "STOP_CROSSED_DURING_REVIEW")
    [obsolete] = bodies(engine, "POSITION_REVIEW_OBSOLETE", sid)
    assert (obsolete["request_id"], obsolete["reason"]) == (request_id,
                                                           "STOP_CROSSED_DURING_REVIEW")
    assert state(mt, sid)["stop"] == "95"  # Nothing applied; the stop fires on its own.
    venue.now += timedelta(seconds=5)  # CRYPTO_STOP_BREACH_V2: 5 s after the print at 94.80.
    engine.manage(sid, quote(mt, "94.80"))
    assert state(mt, sid)["exit_requested"] == "STOP_LIMIT_NOT_FILLED"


def test_a_review_answered_after_the_stop_closed_the_trade_is_discarded(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_TARGET", target="first")

    def stopped_out(_body):
        [stop] = stop_orders(mt, "SOL/USD")
        engine.ingest(venue.fill(stop["id"], stop["qty"], price="94.99"))
        engine.manage(sid, quote(mt, "94.50"))

    kit.jev.hook = stopped_out
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == (
        "DISCARDED", "POSITION_CLOSED_DURING_REVIEW")
    assert state(mt, sid)["state"] == "CLOSED" and state(mt, sid)["target"] == "111"


def test_jev_unavailable_keeps_the_levels_and_raises_the_alarm_until_a_review_succeeds(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.status = 503
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("FAILED", "HTTP_503")
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    status = kit.maintenance.status(engine.store.active())
    assert [f["code"] for f in status["failing_reviews"]] == ["HTTP_503"]
    assert maintenance_alarms(status) == ["MAINTENANCE_REVIEW_FAILING"]
    engine.manage(sid, quote(mt, "106"))  # Protection is unaffected.
    assert len(stop_orders(mt, "SOL/USD")) == 1
    kit.jev.status = 200
    to_boundary(mt)
    assert len(run_pass(mt, kit)) == 1  # The next scheduled review (breaker recovery aside).
    assert kit.maintenance.status(engine.store.active())["failing_reviews"] == []


def test_management_reviews_disabled_sends_nothing(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, reviews="DISABLED")
    kit.prices.set("SOL/USD", "110.50")  # Near the target, +1R and +2R.
    assert run_pass(mt, kit) == []
    to_boundary(mt)
    assert run_pass(mt, kit) == []
    assert kit.jev.calls == [] and kit.bars.calls == []
    [skipped] = bodies(engine, "POSITION_REVIEW_SKIPPED", sid)
    assert skipped["reason"] == "MANAGEMENT_REVIEWS_DISABLED"
    assert not requests(engine, sid) and not bodies(engine, "MAINTENANCE_TRIGGER", sid)
    assert len(stop_orders(mt, "SOL/USD")) == 1  # Protection runs either way.


def test_the_control_arm_is_never_maintained(mt, monkeypatch):
    engine, venue, _ = mt
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.prices.set("SOL/USD", "110.50")
    to_boundary(mt)
    assert run_pass(mt, kit) == [] and kit.jev.calls == []
    assert not bodies(engine, "MAINTENANCE_TRIGGER", sid)
    assert not bodies(engine, "MAINTENANCE_OPENED", sid)
    assert "maintenance_policy" not in state(mt, sid)


def test_the_request_state_stays_under_the_budget_with_the_news_at_production_size(mt):
    from catalyst_lab.jev_contract import digest
    from catalyst_lab.position_news import PositionNewsService

    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    for batch in range(2):
        context = service.context(sid)
        sources = []
        for i in range(8):
            excerpt = (f"Synthetic fixture item {batch}-{i}: " + "exchange flows and funding "
                       "rates shifted while the listing news spread across venues. " * 30)[:1000]
            sources.append({
                "source_id": f"fixture-{batch}-{i}", "url": f"https://example.org/{batch}/{i}",
                "excerpt": excerpt, "published_at": venue.now.isoformat(),
                "retrieved_at": venue.now.isoformat(), "content_hash": digest(excerpt),
                "primary_source": True, "asset_relevant": True, "novelty": "NEW_FACT",
                "stance": "ADVERSE" if i % 4 == 0 else "SUPPORTS"})
        service.submit(sid, {"news_id": str(uuid4()), "lifecycle_id": context["lifecycle_id"],
                             "expected_news_revision": context["news_revision"],
                             "sources": sources})
        venue.now += timedelta(seconds=1)
    kit.prices.set("SOL/USD", "101")
    assert len(run_pass(mt, kit)) == 1
    [request] = requests(engine, sid)
    sent = kit.jev.calls[0]["state"]
    assert encoded_bytes(sent) <= STATE_BYTE_BUDGET
    manifest = request["context"]["manifest"]
    assert manifest["within_budget"] and manifest["state_bytes"] == encoded_bytes(sent)
    assert len(sent["news_since_entry"]) == 4
    assert [n["stance"] for n in sent["news_since_entry"]][:2] == ["ADVERSE", "ADVERSE"]
    context = ManagedContext(encoded(request["context"]), request["context_hash"])
    assert context.state == sent


def test_a_target_the_bid_already_passed_while_jev_answered_is_refused(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_TARGET", target="first")  # T1 = 112.35.
    move_during_answer(mt, kit, sid, bid="112.40")  # No protection tick ran meanwhile.
    reviewed_at(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", "TARGET_NOT_ABOVE_PRICE")
    assert decision["target"]["new"] == "112.35" and state(mt, sid)["target"] == "111"


def test_an_open_breaker_keeps_every_level_and_the_next_review_after_recovery_succeeds(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, failure_threshold=1)  # One provider failure opens it for 30 s.
    kit.jev.status = 503
    to_boundary(mt)
    kit.prices.set("SOL/USD", "101")
    run_pass(mt, kit)
    assert len(kit.jev.calls) == 1
    kit.jev.status = 200
    venue.now += timedelta(seconds=5)
    kit.prices.set("SOL/USD", "110.45")  # Near the target: reviewed within the minute.
    assert len(run_pass(mt, kit)) == 1
    assert len(kit.jev.calls) == 1  # The open breaker sent nothing.
    assert outcomes(engine, sid) == [("FAILED", "HTTP_503"), ("FAILED", "CIRCUIT_OPEN")]
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    status = kit.maintenance.status(engine.store.active())
    assert maintenance_alarms(status) == ["MAINTENANCE_REVIEW_FAILING"]
    to_boundary(mt)  # Past the 30-second cooldown: the next scheduled review goes out.
    kit.prices.set("SOL/USD", "108")
    assert len(run_pass(mt, kit)) == 1 and len(kit.jev.calls) == 2
    assert outcomes(engine, sid)[-1] == ("HELD", None)
    assert maintenance_alarms(kit.maintenance.status(engine.store.active())) == []
