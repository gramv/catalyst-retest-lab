"""Unexpected inventory requires durable operator reconciliation, never automatic resume."""

from decimal import Decimal as D

import pytest

from catalyst_lab.managed_execution import ManagedExecution
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_position_monitor import opened


def restart(engine):
    return ManagedExecution(
        engine.repo,
        engine.broker,
        policy=engine.policy,
        clock=engine.now,
        review_store=engine.review_store,
    )


def test_unexplained_inventory_halt_survives_clean_snapshot_and_restart(mx):
    engine, venue, _ = mx
    venue.inventory["UNKNOWN"] = D(2)
    assert not engine.reconcile()["clean"]
    venue.inventory.clear()  # Only the fixture changes external inventory, not the app.
    new = restart(engine)
    assert new.reconcile()["clean"]
    with pytest.raises(ValueError, match="RISK_HALT"):
        new.admit(packet(mx, "BTC/USD"))
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT reason FROM lab.execution_halts").fetchone()["reason"] == (
            "MANAGED_UNEXPLAINED_BROKER_POSITION"
        )
    assert not venue.orders_of("buy")


def test_reversal_cancels_residual_exits_and_latches_halt_without_inventing_cover_order(mx):
    engine, venue, _ = mx
    sid = opened(mx, "SPY")
    venue.inventory["SPY"] = D(-1)  # Broker race fixture: unexpected short remains.
    before = len(venue.orders)
    engine.manage(sid, observation(mx))
    assert venue.inventory["SPY"] == D(-1) and len(venue.orders) == before
    assert all(o["status"] in {"filled", "canceled"} for o in venue.orders.values())
    new = restart(engine)
    new.reconcile()
    with engine.repo.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM lab.execution_halts WHERE reason='MANAGED_UNEXPECTED_SHORT_POSITION'"
        ).fetchone()
        assert conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()
    assert engine._load(sid)[1]["state"] != "CLOSED"


@pytest.mark.parametrize("available", ["-1", "1000000"])
def test_crypto_invalid_available_halts_account_and_preserves_recovery(mx, monkeypatch, available):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    position_rows = venue._position_rows

    def invalid():
        return [{**row, "qty_available": available} for row in position_rows()]

    monkeypatch.setattr(venue, "_position_rows", invalid)
    plan = engine.manage(sid, observation(mx))
    assert plan.state == "HALTED" and plan.reason == "INVALID_BROKER_AVAILABLE_QUANTITY"
    with engine.repo.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM lab.execution_halts "
            "WHERE reason='MANAGED_CRYPTO_INVALID_BROKER_AVAILABLE_QUANTITY'"
        ).fetchone()
        assert conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()
    assert engine._load(sid)[1]["state"] == "OPEN"
    monkeypatch.setattr(venue, "_position_rows", position_rows)
    new = restart(engine)
    assert new.reconcile()["clean"]
    assert new.manage(sid, observation(mx)).state == "PROTECTED"
    with pytest.raises(ValueError, match="RISK_HALT"):
        new.admit(packet(mx, "ETH/USD"))
    # Account entry block does not cancel existing native protection.
    assert venue.orders_of("sell", "stop_limit")[0]["status"] == "new"


@pytest.mark.parametrize(
    "mutation,reason",
    [("missing", "PROTECTION_PRICE_UNKNOWN"), ("higher", "STOP_WIDENING_REFUSED")],
)
def test_crypto_unusable_protection_halts_without_canceling_a_better_stop(mx, mutation, reason):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    protection = venue.orders_of("sell", "stop_limit")[0]
    if mutation == "missing":
        protection.pop("limit_price")
    else:
        protection["stop_price"] = "96"
    result = engine.manage(sid, observation(mx))
    assert result.state == "HALTED" and result.reason == reason
    assert protection["status"] == "new"
    new = restart(engine)
    with pytest.raises(ValueError, match="RISK_HALT"):
        new.admit(packet(mx, "ETH/USD"))
    assert new._load(sid)[1]["protection_state"] == "HALTED"


def test_crypto_dust_is_not_flat_and_halt_does_not_release_risk(mx):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    for order in venue.orders_of("sell"):
        order["status"] = "canceled"
    venue.inventory["BTC/USD"] = D("0.00001")
    result = engine.manage(sid, observation(mx))
    assert result.state == "HALTED"
    assert result.reason == "UNPROTECTED_RESIDUAL_BELOW_BROKER_MINIMUM"
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()
    assert engine._load(sid)[1]["state"] == "OPEN"
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "OPEN", exit_requested="TIME_EXIT")
    assert restart(engine).manage(sid, observation(mx)).reason == "RESIDUAL_BELOW_BROKER_MINIMUM"
