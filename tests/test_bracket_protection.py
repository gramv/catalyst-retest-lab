"""Rule A bracket-leg protection for both engines (plan packages 2.5 and 3.1).

The pure classifier is checked over every parent/stop/take-profit status combination; the
frozen V1 safety check and the managed stock controller are then driven through the fake
paper venues on disposable PostgreSQL. Fixtures only: no real broker, no network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from itertools import product

import pytest

from catalyst_lab.broker_ledger import (
    ACTIVE_LEG_STATUSES,
    ENTRY_NOT_FILLED,
    LEG_MISMATCH,
    PROTECTED,
    TRANSITION_GRACE_SECONDS,
    TRANSITIONING,
    UNPROTECTED,
    classify_bracket,
    within_transition_grace,
)
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation
from tests.test_managed_execution import mx as mx
from tests.test_risk import candidate_factory as candidate_factory
from tests.test_risk import execution_setup as execution_setup
from tests.test_risk import risk_setup as risk_setup

LEG_STATUSES = (
    "new", "accepted", "partially_filled", "accepted_for_bidding", "held", "pending_new",
    "pending_replace", "pending_cancel", "canceled", "filled", "expired", "rejected",
    "replaced", "done_for_day", "stopped", "suspended", "calculated",
)
PARENT_STATUSES = ("filled", "partially_filled", "new", "accepted", "canceled", "expired")
# The rule as written in the plan, independent of the implementation's constants.
STOP_COVERS = {"new", "accepted", "partially_filled", "accepted_for_bidding", "held"}
TARGET_COVERS = {"new", "accepted", "partially_filled", "accepted_for_bidding"}
WAITS = {"pending_new", "pending_replace", "accepted", "held"}


def leg(role, status, *, qty="10", filled="0", side="sell", price=None, order_id=None):
    field = "stop_price" if role == "STOP" else "limit_price"
    return role, {
        "id": order_id or f"{role.lower()}-{status}", "side": side, "qty": qty,
        "filled_qty": filled, "status": status,
        field: price or ("95" if role == "STOP" else "111"),
    }


def classify(parent_status, stop_status, target_status, **options):
    return classify_bracket(
        {"id": "parent", "status": parent_status},
        [leg("STOP", stop_status), leg("TARGET", target_status)],
        position_qty=options.pop("qty", "10"),
        stop_prices={"95"},
        target_prices={"111.00"},
        **options,
    )


def expected(parent_status, stop_status, target_status):
    if parent_status != "filled":
        return UNPROTECTED
    stop_ok, target_ok = stop_status in STOP_COVERS, target_status in TARGET_COVERS
    if stop_ok and target_ok:
        return PROTECTED
    if (stop_ok or stop_status in WAITS) and (target_ok or target_status in WAITS):
        return TRANSITIONING
    return UNPROTECTED


def test_every_status_combination_follows_rule_a():
    combinations = list(product(PARENT_STATUSES, LEG_STATUSES, LEG_STATUSES))
    wrong = []
    for parent_status, stop_status, target_status in combinations:
        result = classify(parent_status, stop_status, target_status)
        if result.status != expected(parent_status, stop_status, target_status):
            wrong.append((parent_status, stop_status, target_status, result.status))
        elif result.status == PROTECTED and (
            result.stop["status"], result.target["status"]
        ) != (stop_status, target_status):
            wrong.append((parent_status, stop_status, target_status, "WRONG_COVERING_LEG"))
    assert len(combinations) == 6 * 17 * 17 and wrong == []
    protected = {
        (stop, target) for _, stop, target in combinations
        if classify("filled", stop, target).status == PROTECTED
    }
    assert ("held", "new") in protected and ("held", "held") not in protected
    assert len(protected) == 5 * 4  # Stop: four active states or held; target: four active.


def test_named_cases_from_the_plan():
    full_fill = classify("filled", "held", "new")
    assert full_fill.status == PROTECTED and full_fill.reason == "RULE_A_BRACKET"
    both_held = classify("filled", "held", "held")
    assert both_held.status == TRANSITIONING and both_held.reason == "TARGET_LEG_HELD"
    partial = classify("partially_filled", "held", "held")
    assert partial.status == UNPROTECTED and partial.reason == ENTRY_NOT_FILLED
    replacing = classify("filled", "pending_replace", "new")
    assert replacing.status == TRANSITIONING and replacing.reason == "STOP_LEG_PENDING_REPLACE"
    gone = classify("filled", "canceled", "new")
    assert gone.status == UNPROTECTED and gone.reason == "STOP_LEG_MISSING"
    assert ACTIVE_LEG_STATUSES == frozenset(TARGET_COVERS)


@pytest.mark.parametrize(
    "change",
    [
        {"side": "buy"},
        {"qty": "9"},
        {"filled": "2"},
        {"price": "94.99"},
    ],
)
def test_a_live_leg_with_the_wrong_side_quantity_or_price_is_unprotected(change):
    for role, other in (("STOP", "TARGET"), ("TARGET", "STOP")):
        result = classify_bracket(
            {"status": "filled"},
            [leg(role, "new", **change), leg(other, "new")],
            position_qty="10", stop_prices={"95"}, target_prices={"111"},
        )
        assert result.status == UNPROTECTED and result.reason == LEG_MISMATCH


def test_remaining_quantity_may_exceed_a_reduced_position_and_prices_come_from_a_set():
    result = classify_bracket(
        {"status": "filled"},
        [leg("STOP", "held", qty="10", price="96"), leg("TARGET", "partially_filled",
                                                       qty="10", filled="4")],
        position_qty="6", stop_prices={"95", "96"}, target_prices={"111"},
    )
    assert result.status == PROTECTED


def test_an_unowned_transitioning_leg_gives_no_grace_and_absent_parent_is_unprotected():
    unowned = classify("filled", "pending_replace", "new", known_ids={"target-new"})
    assert unowned.status == UNPROTECTED and unowned.reason == "STOP_LEG_MISSING"
    missing = classify_bracket(None, [], position_qty="1", stop_prices=(), target_prices=())
    assert missing.status == UNPROTECTED and missing.reason == ENTRY_NOT_FILLED
    with pytest.raises(ValueError, match="POSITIVE_POSITION_REQUIRED"):
        classify("filled", "held", "new", qty="0")


def test_a_replacement_leg_covers_its_role_while_the_old_leg_is_pending_replace():
    result = classify_bracket(
        {"status": "filled"},
        [leg("STOP", "pending_replace", order_id="old"),
         leg("STOP", "new", price="102", order_id="replacement"),
         leg("TARGET", "new")],
        position_qty="10", stop_prices={"95", "102"}, target_prices={"111"},
    )
    assert result.status == PROTECTED and result.stop["id"] == "replacement"


def test_transition_grace_is_the_risk_ttl_and_fails_closed():
    start = datetime(2026, 9, 24, 14, tzinfo=UTC)
    assert TRANSITION_GRACE_SECONDS == 5
    assert within_transition_grace(start, start)
    assert within_transition_grace(start, start + timedelta(seconds=4.999))
    assert not within_transition_grace(start, start + timedelta(seconds=5))
    assert not within_transition_grace(start, start - timedelta(seconds=1))
    assert not within_transition_grace(None, start)


# --- Frozen V1 safety check (RiskSafety.check_protection) -----------------------------


def v1_filled(execution_setup, candidate_factory, *, stop="held", target="new"):
    fake, _, dispatcher, _ = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    root = fake.orders[fake.root_ids[0]]
    fake.fill_entry(root["id"])
    for item in root["legs"]:
        item["status"] = stop if item["type"] == "stop" else target
    fake.drain(dispatcher)  # The fill event runs check_protection.
    return cid, root


def v1_exits(er):
    with er.connect() as conn:
        return conn.execute(
            "SELECT reason FROM lab.risk_exit_requests ORDER BY event_seq"
        ).fetchall()


def test_v1_full_fill_with_stop_held_is_protected(er, execution_setup, candidate_factory):
    fake, _, _, safety = execution_setup
    cid, root = v1_filled(execution_setup, candidate_factory)
    assert [leg["status"] for leg in root["legs"]] == ["held", "new"]  # The fake's full fill.
    fake.now += timedelta(seconds=60)
    safety.check_protection(cid)
    assert not v1_exits(er) and fake.positions
    assert er.get_candidate(cid)["state"] == "OPEN"
    assert not any(o["type"] == "market" for o in fake.orders.values())


@pytest.mark.parametrize("stop,target", [("held", "held"), ("pending_replace", "new"),
                                         ("new", "pending_new")])
def test_v1_transitioning_legs_wait_for_the_grace_then_flatten(
    er, execution_setup, candidate_factory, stop, target
):
    fake, _, dispatcher, safety = execution_setup
    cid, _ = v1_filled(execution_setup, candidate_factory, stop=stop, target=target)
    fake.now += timedelta(seconds=TRANSITION_GRACE_SECONDS - 1)
    safety.check_protection(cid)
    assert not v1_exits(er) and fake.positions  # Inside the grace: no exit, no flatten.
    fake.now += timedelta(seconds=1)
    safety.check_protection(cid)
    assert [row["reason"] for row in v1_exits(er)] == ["PROTECTION_FAILURE"]
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {} and er.get_candidate(cid)["state"] == "CLOSED"
    with er.connect() as conn:
        failure = conn.execute(
            "SELECT payload_json FROM lab.system_events "
            "WHERE event_type='PROTECTIVE_EXIT_FAILURE'"
        ).fetchone()["payload_json"]
    assert failure["classification"].endswith(("_HELD", "_PENDING_REPLACE", "_PENDING_NEW"))


@pytest.mark.parametrize("stop,target", [("canceled", "new"), ("held", "canceled"),
                                         ("rejected", "new")])
def test_v1_missing_leg_flattens_without_grace(er, execution_setup, candidate_factory,
                                               stop, target):
    fake, _, dispatcher, safety = execution_setup
    cid, _ = v1_filled(execution_setup, candidate_factory, stop=stop, target=target)
    assert [row["reason"] for row in v1_exits(er)] == ["PROTECTION_FAILURE"]
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {} and er.get_candidate(cid)["state"] == "CLOSED"


def test_v1_partial_fill_flattens_at_once(er, execution_setup, candidate_factory):
    fake, _, dispatcher, safety = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    root = fake.orders[fake.root_ids[0]]
    fake.fill_entry(root["id"], qty=3)
    assert [leg["status"] for leg in root["legs"]] == ["held", "held"]
    fake.drain(dispatcher)  # The partial-fill event runs check_protection: no grace.
    assert [row["reason"] for row in v1_exits(er)] == ["PROTECTION_FAILURE"]
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {} and er.get_candidate(cid)["state"] == "CLOSED"
    closes = [o for o in fake.orders.values() if o["type"] == "market"]
    assert len(closes) == 1 and closes[0]["qty"] == "3"


# --- Managed stock controller (ManagedExecution._manage_stock) ------------------------


def managed_filled(mx, *, stop="held", target="new"):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    for item in entry["legs"]:
        item["status"] = stop if item["type"] == "stop" else target
    return sid, entry


def managed_bodies(engine, sid, kind):
    with engine.repo.connect() as conn:
        return [row["body"] for row in conn.execute(
            "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind=%s ORDER BY event_seq",
            (sid, kind),
        ).fetchall()]


def test_managed_full_fill_with_stop_held_is_protected(mx):
    engine, venue, _ = mx
    sid, entry = managed_filled(mx)
    for _ in range(3):
        assert engine.manage(sid, observation(mx)) == "PROTECTED"
        venue.now += timedelta(seconds=10)
    state = engine._load(sid)[1]
    assert state["state"] == "OPEN" and state.get("exit_requested") is None
    assert [leg["status"] for leg in entry["legs"]] == ["held", "new"]
    assert not any(method == "DELETE" for method, _, _ in venue.calls)
    assert not venue.orders_of("sell", "market")


def test_managed_protection_with_a_print_and_no_quote_is_left_unchanged(mx):
    """After a market gap the runtime's observation can hold a print and no quote: the bracket is
    then left as it is, like a stale quote."""
    engine, venue, _ = mx
    sid, entry = managed_filled(mx)
    now = venue.now.isoformat()
    print_only = {"trade_price": "100", "trade_at": now, "trade_id": "7", "feed_healthy": True,
                  "data_provider": "ALPACA", "data_feed": "iex", "retrieved_at": now}
    assert engine.manage(sid, print_only) == "STALE_QUOTE_PROTECTION_UNCHANGED"
    assert [leg["status"] for leg in entry["legs"]] == ["held", "new"]
    assert not any(method in {"DELETE", "PATCH"} for method, _, _ in venue.calls)
    assert engine.manage(sid, observation(mx)) == "PROTECTED"


@pytest.mark.parametrize("stop,target", [("held", "held"), ("pending_replace", "new"),
                                         ("new", "pending_new")])
def test_managed_transitioning_legs_wait_for_the_grace_then_flatten(mx, stop, target):
    engine, venue, _ = mx
    sid, entry = managed_filled(mx, stop=stop, target=target)
    assert engine.manage(sid, observation(mx)) == "PROTECTION_TRANSITIONING"
    venue.now += timedelta(seconds=TRANSITION_GRACE_SECONDS - 1)
    assert engine.manage(sid, observation(mx)) == "PROTECTION_TRANSITIONING"
    # Inside the grace: no mutation of any kind, only the original entry POST.
    assert [method for method, _, _ in venue.calls if method != "GET"] == ["POST"]
    assert engine._load(sid)[1].get("exit_requested") is None
    [notice] = managed_bodies(engine, sid, "PROTECTION_TRANSITIONING")
    assert notice["grace_seconds"] == TRANSITION_GRACE_SECONDS
    venue.now += timedelta(seconds=1)
    assert engine.manage(sid, observation(mx)) == "CANCELING"
    assert engine._load(sid)[1]["exit_requested"] == "INCOMPLETE_BRACKET_PROTECTION"
    for item in entry["legs"]:
        item["status"] = "canceled"  # The fake cancels only non-pending legs itself.
    assert engine.manage(sid, observation(mx)) == "EXIT_PENDING"
    close = venue.orders_of("sell", "market")[0]
    assert D(close["qty"]) == D(entry["qty"])


def test_managed_transition_that_resolves_inside_the_grace_never_exits(mx):
    engine, venue, _ = mx
    sid, entry = managed_filled(mx, stop="pending_replace", target="new")
    assert engine.manage(sid, observation(mx)) == "PROTECTION_TRANSITIONING"
    assert engine._load(sid)[1]["protection_transitioning_since"]
    venue.now += timedelta(seconds=2)
    entry["legs"][0]["status"] = "held"
    assert engine.manage(sid, observation(mx)) == "PROTECTED"
    assert engine._load(sid)[1]["protection_transitioning_since"] is None
    venue.now += timedelta(seconds=30)
    assert engine.manage(sid, observation(mx)) == "PROTECTED"
    assert not venue.orders_of("sell", "market")


@pytest.mark.parametrize("stop,target", [("canceled", "new"), ("held", "canceled"),
                                         ("rejected", "new")])
def test_managed_missing_leg_flattens_without_grace(mx, stop, target):
    engine, venue, _ = mx
    sid, _ = managed_filled(mx, stop=stop, target=target)
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["exit_requested"] == "INCOMPLETE_BRACKET_PROTECTION"
    engine.manage(sid, observation(mx))
    assert venue.orders_of("sell", "market")


def test_managed_partial_fill_flattens_at_once(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], "1"))
    assert [leg["status"] for leg in entry["legs"]] == ["held", "held"]
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["exit_requested"] == "PARTIAL_ENTRY_SAFETY_FLATTEN"
    assert entry["status"] == "canceled"
    engine.manage(sid, observation(mx))
    assert venue.orders_of("sell", "market")[0]["qty"] == "1"


def test_managed_unauthorized_leg_price_is_unprotected_at_once(mx):
    engine, venue, _ = mx
    sid, entry = managed_filled(mx)
    entry["legs"][0]["stop_price"] = "90"  # Not a price this setup ever authorized.
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["exit_requested"] == "INCOMPLETE_BRACKET_PROTECTION"
