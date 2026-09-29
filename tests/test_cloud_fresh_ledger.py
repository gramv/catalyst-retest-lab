"""A fresh cloud ledger meeting a paper account that already has history (package cloud).

The Mac ledger traded this account before; the cloud ledger starts empty. Its first startup
reconciliation must refuse entries while the account holds a position or an open order the new
ledger does not know, and must not be blocked by the account's closed, historical orders.
Disposable PostgreSQL and the managed tests' mock paper venue only.
"""

from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from tests.test_execution import er as er  # noqa: F401  (fixture)
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401  (fixture)
from tests.test_managed_execution import ManagedVenue, observation, packet


@pytest.fixture
def fresh(er):  # noqa: F811
    """A never-reconciled ledger: no setups, no orders, no account binding."""
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = ManagedVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(risk, broker, policy=engineering_execution_policy(),
                              clock=lambda: venue.now, review_store=reviews)
    yield engine, venue, reviews
    broker.close()


def mac_era_order(venue, symbol, status, *, side="buy", kind="limit"):
    """An order the Mac ledger placed; the cloud ledger has no record of it."""
    order = {"id": str(uuid4()), "client_order_id": "mac-era-" + uuid4().hex[:16],
             "symbol": symbol, "qty": "1", "filled_qty": "1" if status == "filled" else "0",
             "side": side, "type": kind, "time_in_force": "gtc", "limit_price": "10.00",
             "status": status, "updated_at": venue.now.isoformat(), "legs": [],
             "asset_class": "crypto" if "/" in symbol else "us_equity"}
    venue.orders[order["id"]] = order
    return order


def bound(engine):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM lab.ledger_account_binding"
                            ).fetchone()["n"] == 1


def test_an_unknown_open_position_refuses_entries_on_a_fresh_ledger(fresh):
    engine, venue, _ = fresh
    venue.inventory["ETH/USD"] = D("0.5")  # Left from the Mac era.
    result = engine.reconcile()
    assert not result["clean"] and engine.reconciled_at is None and not bound(engine)
    assert {m["type"] for m in result["mismatches"]} == {"POSITION_QUANTITY_MISMATCH"}
    with engine.repo.connect() as conn:
        halts = [r["reason"] for r in conn.execute("SELECT reason FROM lab.execution_halts")]
    assert halts == ["MANAGED_UNEXPLAINED_BROKER_POSITION"]
    with pytest.raises(ValueError, match="RISK_HALT"):
        engine.admit(packet(fresh, "BTC/USD"))
    assert not venue.orders_of("buy")


def test_an_unknown_open_order_keeps_the_fresh_ledger_unreconciled_until_it_is_gone(fresh):
    engine, venue, _ = fresh
    stale = mac_era_order(venue, "SOL/USD", "new")
    result = engine.reconcile()
    assert not result["clean"] and engine.reconciled_at is None and not bound(engine)
    assert result["mismatches"] == [{"type": "UNKNOWN_ORDER", "order_id": stale["id"]}]
    sid = engine.admit(packet(fresh, "BTC/USD"))
    decision = engine.observe_trigger(sid, observation(fresh))
    assert decision["outcome"] != "APPROVED"
    with engine.repo.connect() as conn:
        state = conn.execute("""SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind='STATE' ORDER BY event_seq DESC LIMIT 1""", (sid,)).fetchone()["body"]
    assert state["state"] == "RISK_REJECTED"
    assert state["reason"] == "STARTUP_RECONCILIATION_REQUIRED"
    assert venue.orders_of("buy") == [stale]  # Nothing was sent for the refused entry.
    # Once the owner cancels it at the broker, the next reconciliation is clean and binds.
    stale["status"] = "canceled"
    assert engine.reconcile()["clean"] and bound(engine)
    sid = engine.admit(packet(fresh, "BTC/USD"))
    assert engine.observe_trigger(sid, observation(fresh))["outcome"] == "APPROVED"
    assert len(venue.orders_of("buy")) == 2


def test_closed_historical_orders_never_block_a_fresh_ledger(fresh):
    engine, venue, _ = fresh
    for symbol, status in (("BTC/USD", "filled"), ("ETH/USD", "canceled"),
                           ("SOL/USD", "expired"), ("AVAX/USD", "rejected")):
        mac_era_order(venue, symbol, status)
    result = engine.reconcile()
    assert result == {"clean": True, "mismatches": []} and bound(engine)
    sid = engine.admit(packet(fresh, "BTC/USD"))
    decision = engine.observe_trigger(sid, observation(fresh))
    assert decision["outcome"] == "APPROVED"
    assert [o["status"] for o in venue.orders_of("buy") if not o["client_order_id"].startswith(
        "mac-era-")] == ["new"]
