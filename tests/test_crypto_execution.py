"""Venue-specific planning tests. No test can perform network I/O or place an order."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.crypto_execution import (
    CryptoAsset,
    CryptoExecutionError,
    CryptoOrder,
    CryptoProtectionPolicy,
    CryptoSnapshot,
    MutationProposal,
    build_limit_entry,
    build_market_exit,
    build_stop_limit,
    off_grid_levels,
    plan_crypto_recovery,
)

NOW = datetime(2026, 9, 19, 20, 0, tzinfo=UTC)
ASSET = CryptoAsset("BTC/USD", D("0.0001"), D("0.0001"), D("0.01"))
POLICY = CryptoProtectionPolicy(D(5), D(5), D(2))


def order(role="PROTECT", qty="0.1", filled="0", status="new", **kwargs):
    return CryptoOrder(
        broker_id="broker-" + role.lower(),
        client_order_id="client-" + role.lower(),
        role=role,
        qty=D(qty),
        filled_qty=D(filled),
        status=status,
        owned=True,
        stop_price=D(90) if role == "PROTECT" else None,
        limit_price=D(89) if role == "PROTECT" else None,
        **kwargs,
    )


def snap(**kwargs):
    baseline = CryptoSnapshot(
        revision="reconciliation-001",
        observed_at=NOW,
        position_qty=D("0.1"),
        qty_available=D("0.1"),
        orders=(),
        reconciled=True,
        uncertain_client_ids=(),
        lookup_complete=(),
        bid=D(100),
        quote_at=NOW,
        stop_breached_at=None,
    )
    return replace(baseline, **kwargs)


def plan(snapshot=None, **kwargs):
    inputs = {
        "now": NOW,
        "operation_key": "lifecycle-001",
        "stop": D(90),
        "stop_limit": D(89),
        "target": D(120),
        "exit_requested": False,
        "exit_deadline": NOW + timedelta(hours=2),
    }
    inputs.update(kwargs)
    return plan_crypto_recovery(ASSET, snapshot or snap(), POLICY, **inputs)


def test_metadata_required_and_fractional_grid_comes_from_asset():
    raw = {
        "class": "crypto",
        "status": "active",
        "tradable": True,
        "fractionable": True,
        "symbol": "ETH/USD",
        "min_order_size": "0.0001",
        "min_trade_increment": "0.0001",
        "price_increment": "0.01",
    }
    asset = CryptoAsset.from_broker(raw)
    assert asset.quantity("0.123456") == D("0.1234")
    with pytest.raises(CryptoExecutionError, match="METADATA"):
        CryptoAsset.from_broker({**raw, "tradable": False})
    del raw["min_trade_increment"]
    with pytest.raises(CryptoExecutionError, match="METADATA"):
        CryptoAsset.from_broker(raw)


def test_crypto_entry_is_simple_gtc_limit_with_fractional_qty_and_stable_id():
    first = build_limit_entry(ASSET, D("0.001234"), D("100.12"), operation_key="entry-01")
    second = build_limit_entry(ASSET, D("0.001234"), D("100.12"), operation_key="entry-01")
    assert first == second
    assert first["qty"] == "0.0012"
    assert first["limit_price"] == "100.12"
    assert first["type"] == "limit" and first["time_in_force"] == "gtc"
    assert "order_class" not in first and "stop_loss" not in first


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "0", "-1", 0.1, True])
def test_bad_quantity_fails_closed(bad):
    with pytest.raises(CryptoExecutionError):
        build_limit_entry(ASSET, bad, D(100), operation_key="entry")


def test_broker_minimum_price_precision_and_stop_relationship():
    with pytest.raises(CryptoExecutionError, match="MINIMUM"):
        build_market_exit(ASSET, D("0.00001"), operation_key="exit")
    with pytest.raises(CryptoExecutionError, match="INCREMENT"):
        build_limit_entry(ASSET, D(1), D("100.001"), operation_key="entry")
    with pytest.raises(CryptoExecutionError, match="ABOVE_STOP"):
        build_stop_limit(ASSET, D(1), D(90), D(91), operation_key="stop")


def test_only_fixed_paper_endpoint_proposals_allowed():
    proposal = plan().proposals[0]
    assert proposal.endpoint == PAPER_ENDPOINT
    with pytest.raises(CryptoExecutionError, match="PAPER_ENDPOINT"):
        replace(proposal, endpoint=PAPER_ENDPOINT.replace("paper-", ""))
    with pytest.raises(CryptoExecutionError, match="MUTATION"):
        MutationProposal("CRYPTO_CANCEL", "DELETE", "/v2/positions", {}, "test")


def test_unfilled_entry_waits_without_stop_or_cancel():
    p = plan(snap(position_qty=D(0), qty_available=D(0), orders=(order("ENTRY"),)))
    assert p.state == "ENTRY_WORKING" and not p.proposals


def test_partial_fill_protected_using_net_broker_inventory_and_remainder_canceled():
    p = plan(
        snap(
            position_qty=D("0.0399"),
            qty_available=D("0.0399"),
            orders=(order("ENTRY", filled="0.04", status="partially_filled"),),
        )
    )
    assert [x.action for x in p.proposals] == ["CRYPTO_PROTECT", "CRYPTO_CANCEL"]
    assert p.proposals[0].payload["qty"] == "0.0399"  # Fee reduced received asset.
    assert p.proposals[0].payload["stop_price"] == "90"


def test_late_entry_fill_adds_only_uncovered_stop_quantity():
    p = plan(
        snap(
            position_qty=D("0.08"),
            qty_available=D("0.04"),
            orders=(order(qty="0.04"), order("ENTRY", filled="0.08", status="canceled")),
        )
    )
    assert p.proposals[0].payload["qty"] == "0.04"
    assert p.residual_qty == D("0.04")


def test_working_stop_never_duplicates_inventory_reservation():
    p = plan(snap(qty_available=D(0), orders=(order(),)))
    assert p.state == "PROTECTED" and not p.proposals


def test_pending_entry_cancel_does_not_leave_partial_inventory_unprotected():
    p = plan(snap(orders=(order("ENTRY", filled="0.1", qty="0.2", status="pending_cancel"),)))
    assert p.state == "PROTECTION_REQUIRED"
    assert len(p.proposals) == 1 and p.proposals[0].action == "CRYPTO_PROTECT"


def test_target_cancels_protection_before_exit_and_waits_terminal_confirmation():
    current = snap(qty_available=D(0), orders=(order(),), bid=D(120))
    p = plan(current)
    assert p.state == "CANCELING" and p.reason == "TARGET_EXIT"
    assert all(x.method == "DELETE" for x in p.proposals)
    pending = plan(replace(current, orders=(order(status="pending_cancel"),)))
    assert pending.state == "CANCELING" and not pending.proposals
    # Price retreats while cancellation completes: persisted exit_requested retains exit intent.
    final = plan(snap(orders=(order(status="canceled"),)), exit_requested=True)
    assert final.state == "EXIT_REQUIRED" and final.proposals[0].payload["type"] == "market"


def test_cancel_fill_race_uses_reconciled_residual_and_does_not_reverse():
    p = plan(
        snap(
            position_qty=D("0.06"),
            qty_available=D("0.06"),
            orders=(order(filled="0.04", status="canceled"),),
        ),
        exit_requested=True,
    )
    assert p.proposals[0].payload["qty"] == "0.06"
    flat = plan(
        snap(position_qty=D(0), qty_available=D(0), orders=(order(filled="0.1", status="filled"),)),
        exit_requested=True,
    )
    assert flat.state == "FLAT" and not flat.proposals


def test_stop_limit_gap_exits_after_explicit_grace_via_cancel_reconcile():
    current = snap(
        qty_available=D(0),
        orders=(order(),),
        bid=D(85),
        stop_breached_at=NOW - timedelta(seconds=2),
    )
    p = plan(current)
    assert p.reason == "STOP_LIMIT_NOT_FILLED" and p.proposals[0].method == "DELETE"
    after_cancel = replace(current, qty_available=D("0.1"), orders=(order(status="canceled"),))
    p = plan(after_cancel)
    assert p.proposals[0].payload["type"] == "market"


def test_protection_rejection_flattens_instead_of_leaving_naked():
    p = plan(snap(orders=(order(status="rejected"),)))
    assert p.reason == "PROTECTION_REJECTED" and p.proposals[0].action == "CRYPTO_EXIT"


def test_stop_tightening_requires_cancel_confirmation_and_never_patch():
    current = snap(qty_available=D(0), orders=(order(),))
    p = plan(current, stop=D(95), stop_limit=D(94))
    assert p.reason == "TIGHTEN_STOP" and p.proposals[0].method == "DELETE"
    p = plan(snap(orders=(order(status="canceled"),)), stop=D(95), stop_limit=D(94))
    assert p.proposals[0].payload["stop_price"] == "95"
    p = plan(current, stop=D(85), stop_limit=D(84))
    assert p.state == "HALTED" and p.reason == "STOP_WIDENING_REFUSED"


def test_unknown_submission_requires_lookup_and_cannot_manufacture_new_client_id():
    unknown = snap(uncertain_client_ids=("client-protect",))
    assert plan(unknown).reason == "UNKNOWN_SUBMISSION_LOOKUP_REQUIRED"
    absent = replace(unknown, lookup_complete=("client-protect",))
    assert plan(absent).reason == "UNKNOWN_NOT_LOCATED_RETRY_ORIGINAL_REQUEST_ONLY"
    found = replace(absent, qty_available=D(0), orders=(order(),))
    assert plan(found).state == "PROTECTED" and not plan(found).proposals


def test_old_broker_snapshot_blocks_all_mutations():
    p = plan(snap(observed_at=NOW - timedelta(seconds=6)))
    assert p.state == "RECONCILE_REQUIRED" and not p.proposals


def test_old_quote_cannot_trigger_target_but_does_not_disable_protection():
    p = plan(snap(bid=D(130), quote_at=NOW - timedelta(seconds=6)))
    assert p.state == "PROTECTION_REQUIRED"


def test_time_deadline_is_explicit_not_equity_calendar():
    p = plan(snap(), exit_deadline=NOW)
    assert p.reason == "TIME_EXIT" and p.proposals[0].payload["time_in_force"] == "gtc"


def test_working_exit_never_submits_second_sell():
    p = plan(snap(qty_available=D(0), orders=(order("EXIT"),)), exit_requested=True)
    assert p.state == "EXIT_WORKING" and not p.proposals


def test_partial_exit_terminal_retry_sells_only_remaining_inventory():
    p = plan(
        snap(
            position_qty=D("0.03"),
            qty_available=D("0.03"),
            orders=(order("EXIT", filled="0.07", status="canceled"),),
        ),
        exit_requested=True,
    )
    assert p.proposals[0].payload["qty"] == "0.03"


def test_dust_is_not_claimed_as_flat():
    p = plan(snap(position_qty=D("0.00001"), qty_available=D("0.00001")), exit_requested=True)
    assert p.state == "HALTED" and p.residual_qty == D("0.00001")


def test_reversal_unknown_order_or_oversubscription_halts():
    assert plan(snap(position_qty=D(-1), qty_available=D(0))).reason == "UNEXPECTED_SHORT_POSITION"
    assert plan(snap(orders=(replace(order(), owned=False),))).reason == "UNEXPLAINED_BROKER_ORDER"
    assert (
        plan(snap(qty_available=D(0), orders=(order(qty="0.2"),))).reason
        == "SELL_RESERVATION_EXCEEDS_POSITION"
    )


# A price-only PATCH: Alpaca creates a new order naming the old one in ``replaces``, and a
# snapshot can list both until the old one turns ``replaced`` (UNI, 2026-09-28 15:31 UTC: the
# doubled reservation cancelled the new stop-limit and left the position without a stop order).
def raised_pair(old_status="new", new_status="new"):
    old = replace(order(status=old_status), broker_id="stop-old", client_order_id="client-old",
                  stop_price=D(85), limit_price=D(84))
    new = replace(order(status=new_status), broker_id="stop-new", client_order_id="client-new",
                  replaces="stop-old")
    return old, new


@pytest.mark.parametrize("old_status", ["new", "pending_replace"])
def test_a_replacement_in_flight_is_one_reservation_not_two(old_status):
    old, new = raised_pair(old_status)
    both = plan(snap(qty_available=D(0), orders=(old, new)))
    alone = plan(snap(qty_available=D(0), orders=(new,)))
    assert (both.state, both.reason, both.proposals) == (alone.state, alone.reason, alone.proposals)
    assert both.reason != "SELL_RESERVATION_EXCEEDS_POSITION"
    assert not any(p.method == "DELETE" for p in both.proposals)


def test_two_unlinked_stop_orders_still_exceed_the_position():
    other = replace(order(), broker_id="stop-other", client_order_id="client-other")
    p = plan(snap(qty_available=D(0), orders=(order(), other)))
    assert (p.state, p.reason) == ("CANCELING", "SELL_RESERVATION_EXCEEDS_POSITION")


# A rejected replacement is left out: any rejected protective order already takes the
# PROTECTION_REJECTED exit, with or without a replacement link.
@pytest.mark.parametrize("new_status", ["canceled", "expired"])
def test_a_replacement_that_is_no_longer_active_supersedes_nothing(new_status):
    old, new = raised_pair(new_status=new_status)
    for mode in ("CANCEL", "PATCH"):
        with_dead = plan(snap(qty_available=D(0), orders=(old, new)), replace_stop=mode)
        old_only = plan(snap(qty_available=D(0), orders=(old,)), replace_stop=mode)
        assert (with_dead.state, with_dead.reason) == (old_only.state, old_only.reason)
        assert [x.path for x in with_dead.proposals] == [x.path for x in old_only.proposals]


def test_no_proposal_exposes_dispatch_or_credential_capability():
    p = plan().proposals[0]
    assert not hasattr(p, "dispatch") and not hasattr(p, "risk_decision_id")
    assert not hasattr(p, "credentials")


def test_stale_sell_reservation_after_flatten_is_canceled_not_allowed_to_reverse():
    p = plan(snap(position_qty=D(0), qty_available=D(0), orders=(order(),)))
    assert p.state == "CANCELING"
    assert p.proposals[0].method == "DELETE"
    assert not any(x.method == "POST" for x in p.proposals)


# Off-grid levels (e.g. a legacy research stop with 12 decimals) must never stop the
# planner: the target is app-managed, exits come first, and only the native stop-limit
# is raised to the broker grid.
OFF_GRID = {"stop": D("90.005"), "stop_limit": D("89.995"), "target": D("120.005")}


def test_grid_helpers_raise_to_next_increment_and_name_off_grid_levels():
    assert str(ASSET.price_at_or_above(D("90.005"))) == "90.01"
    assert str(ASSET.price_at_or_above(D("90.01"))) == "90.01"
    assert str(ASSET.price_at_or_above(D("90.010"))) == "90.010"  # On grid: unchanged.
    assert str(ASSET.price_at_or_above(D("0.001"))) == "0.01"
    whole_dollar = CryptoAsset("BTC/USD", D("0.0001"), D("0.0001"), D("1"))
    assert str(whole_dollar.price_at_or_above(D("64123.000000000001"))) == "64124"
    levels = {"entry_trigger": "100", "stop": "95.005", "target": "111.000000000001"}
    assert off_grid_levels(D("0.01"), levels) == {
        "stop": D("95.005"),
        "target": D("111.000000000001"),
    }
    assert off_grid_levels("0.01", {"max_entry_price": "100.10"}) == {}
    with pytest.raises(CryptoExecutionError, match="EXACT_DECIMAL"):
        off_grid_levels("0.01", {"stop": 95.005})


def test_off_grid_stop_is_raised_to_the_grid_and_both_values_recorded():
    p = plan(**OFF_GRID)
    assert p.state == "PROTECTION_REQUIRED" and p.reason == "UNCOVERED_BROKER_INVENTORY"
    payload = p.proposals[0].payload
    assert (payload["stop_price"], payload["limit_price"]) == ("90.01", "90.00")
    assert p.details == {
        "stop_snapped_to_grid": True,
        "requested_stop": D("90.005"),
        "requested_stop_limit": D("89.995"),
        "price_increment": D("0.01"),
        "native_stop": D("90.01"),
        "native_stop_limit": D("90.00"),
    }
    assert str(p.details["native_stop_limit"]) == "90.00"


def test_on_grid_levels_record_no_grid_details():
    assert plan().details == {}
    assert plan(snap(qty_available=D(0), orders=(order(),))).details == {}
    assert plan(snap(), exit_deadline=NOW).details == {}


@pytest.mark.parametrize("raw", ["90.001", "90.0099", "90.000000000001", "90.009999999999"])
def test_raised_stop_is_the_next_grid_price_never_looser(raw):
    requested = D(raw)
    payload = plan(stop=requested, stop_limit=requested - D("0.01")).proposals[0].payload
    native = D(payload["stop_price"])
    assert requested < native < requested + D("0.01") and native % D("0.01") == 0
    assert D(payload["limit_price"]) == native - D("0.01")


def test_only_off_grid_limit_is_raised_and_on_grid_stop_is_unchanged():
    p = plan(stop=D(90), stop_limit=D("89.995"))
    assert (p.proposals[0].payload["stop_price"], p.proposals[0].payload["limit_price"]) == (
        "90",
        "90.00",
    )
    assert p.details["requested_stop"] == p.details["native_stop"] == D(90)
    # The stop itself did not move, so an existing breach keeps today's behaviour.
    assert plan(snap(bid=D(90)), stop=D(90), stop_limit=D("89.995")).state == (
        "PROTECTION_REQUIRED"
    )


def test_off_grid_levels_never_block_time_target_stop_grace_or_authorized_exits():
    time_exit = plan(snap(), exit_deadline=NOW, **OFF_GRID)
    assert time_exit.reason == "TIME_EXIT" and time_exit.proposals[0].payload["type"] == "market"
    # The app-managed target is compared with the quote and never grid-checked.
    assert plan(snap(bid=D("120.004")), **OFF_GRID).reason == "UNCOVERED_BROKER_INVENTORY"
    target = plan(snap(qty_available=D(0), orders=(order(),), bid=D("120.005")), **OFF_GRID)
    assert target.state == "CANCELING" and target.reason == "TARGET_EXIT"
    breached = snap(
        qty_available=D(0),
        orders=(order(),),
        bid=D(85),
        stop_breached_at=NOW - timedelta(seconds=2),
    )
    assert plan(breached, **OFF_GRID).reason == "STOP_LIMIT_NOT_FILLED"
    authorized = plan(snap(), exit_requested=True, **OFF_GRID)
    assert authorized.state == "EXIT_REQUIRED" and authorized.reason == "AUTHORIZED_EXIT"
    dust = plan(
        snap(position_qty=D("0.00001"), qty_available=D("0.00001")), exit_requested=True, **OFF_GRID
    )
    assert dust.state == "HALTED" and dust.reason == "RESIDUAL_BELOW_BROKER_MINIMUM"


def test_raised_native_stop_is_recognised_never_churned_or_widened():
    native = replace(order(), stop_price=D("90.01"), limit_price=D("90.00"))
    p = plan(snap(qty_available=D(0), orders=(native,)), **OFF_GRID)
    assert p.state == "PROTECTED" and not p.proposals and p.details["stop_snapped_to_grid"]
    lower = plan(snap(qty_available=D(0), orders=(order(),)), **OFF_GRID)
    assert lower.reason == "TIGHTEN_STOP" and lower.proposals[0].method == "DELETE"
    higher = replace(order(), stop_price=D("90.02"), limit_price=D("90.01"))
    assert plan(snap(qty_available=D(0), orders=(higher,)), **OFF_GRID).reason == (
        "STOP_WIDENING_REFUSED"
    )


def test_raised_stop_at_or_above_fresh_bid_halts_instead_of_raising_or_triggering():
    p = plan(snap(bid=D("90.008")), **OFF_GRID)
    assert p.state == "HALTED" and p.reason == "CRYPTO_STOP_UNSNAPPABLE"
    assert not p.proposals and p.residual_qty == D("0.1")
    assert p.details["native_stop"] == D("90.01") and p.details["bid"] == D("90.008")
    assert p.details["target"] == D("120.005")
    assert plan(snap(bid=D("90.01")), **OFF_GRID).reason == "CRYPTO_STOP_UNSNAPPABLE"
    assert plan(snap(bid=D("90.02")), **OFF_GRID).state == "PROTECTION_REQUIRED"
    # A stale quote cannot show an immediate trigger, so protection is not withheld.
    stale = snap(bid=D("90.008"), quote_at=NOW - timedelta(seconds=6))
    assert plan(stale, **OFF_GRID).state == "PROTECTION_REQUIRED"
    # A raised stop at or above the target is never sent either.
    tight = {"stop": D("119.995"), "stop_limit": D("119.985"), "target": D(120)}
    assert plan(stale, **tight).reason == "CRYPTO_STOP_UNSNAPPABLE"


def test_unsnappable_stop_cancels_working_entry_and_keeps_existing_lower_stop():
    working = order("ENTRY", filled="0.1", qty="0.2", status="partially_filled")
    p = plan(snap(bid=D("90.008"), orders=(working,)), **OFF_GRID)
    assert p.reason == "CRYPTO_STOP_UNSNAPPABLE"
    assert [(x.method, x.reason) for x in p.proposals] == [("DELETE", "CANCEL_REMAINING_ENTRY")]
    kept = plan(snap(qty_available=D(0), orders=(order(),), bid=D("90.008")), **OFF_GRID)
    assert kept.state == "HALTED" and kept.reason == "CRYPTO_STOP_UNSNAPPABLE"
    assert not kept.proposals  # The working lower native stop is not canceled.
