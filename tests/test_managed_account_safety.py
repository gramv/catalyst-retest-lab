"""Shared fake paper account, real disposable DB, exact gates on both cohorts."""

import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.managed_account_safety import ManagedAccountSafety
from catalyst_lab.market import NY
from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
from catalyst_lab.risk import RiskEngine, RiskPolicy
from catalyst_lab.risk_dispatch import RiskDispatcher
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_managed_operator_halts import restart
from tests.test_position_monitor import opened


@pytest.fixture
def legacy(mx, er, raw, evidence, policy):
    managed, venue, _ = mx
    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        AuthorizationGate(managed.repo, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    risk = RiskEngine(managed.repo, client, RiskPolicy(), ready=lambda: True,
                      clock=lambda: venue.now)
    dispatcher = RiskDispatcher(risk)

    def create(ticker="LEGACY", *, fill=True):
        now = venue.now
        day = now.astimezone(NY).date()
        session = risk.session()
        body = {**raw, "ticker": ticker, "signal_id": str(uuid4()),
                "entry_trigger": "100", "max_entry_price": "100.10",
                "stop": "95", "target": "111"}
        ev = replace(evidence, ticker=ticker, observed_at=now, session_date=day,
                     official_open=session.opens, official_close=session.closes,
                     quote_timestamp=now - timedelta(seconds=1), reconciled_session=day)
        row = er.submit(body, now, lambda *_: ev, policy)
        assert row["state"] == "VALIDATED", row
        cid = row["candidate_id"]
        with er.connect() as conn:
            er.transition(conn, cid, "VALIDATED", "WATCHING", {"reason": "LAB_FIXTURE"})
            er.transition(conn, cid, "WATCHING", "TRIGGER_CONFIRMED", {"reason": "LAB_FIXTURE"})
        risk.classify(ticker, ticker, ticker, "LAB_FIXTURE")
        decision = dispatcher.enter(cid)
        assert decision["decision"] == "APPROVED", decision
        entry = next(o for o in venue.orders_of("buy") if o["symbol"] == ticker)
        if fill:
            dispatcher.ingest(venue.fill(entry["id"], entry["qty"]), now)
        return cid, entry

    yield client, create
    client.close()


@pytest.mark.parametrize("mixed", [False, True])
def test_daily_halt_covers_legacy_only_and_mixed_exposure_after_restart(mx, legacy, mixed):
    engine, venue, _ = mx
    client, create = legacy
    sid = opened(mx, "BTC/USD") if mixed else None
    cid, _ = create()
    venue.equity = "9700"
    safety = ManagedAccountSafety(engine, client)
    assert safety.tick() == "DAILY_RISK_HALT"
    legacy_exit = [o for o in venue.orders_of("sell", "market") if o["symbol"] == "LEGACY"]
    assert len(legacy_exit) == 1
    if sid:
        assert engine._load(sid)[1]["exit_requested"] == "DAILY_RISK_HALT"
        engine.manage(sid, observation(mx))
        engine.manage(sid, observation(mx))
    # Restart while an exit is working: recover the original ID, never submit another.
    restarted = ManagedAccountSafety(restart(engine), client)
    restarted.tick()
    assert len([o for o in venue.orders_of("sell", "market") if o["symbol"] == "LEGACY"]) == 1
    for order in list(venue.orders_of("sell", "market")):
        event = venue.fill(order["id"], order["qty"])
        if not restarted.ingest(event):
            engine.ingest(event)
    restarted.tick()
    if sid:
        engine.manage(sid, observation(mx))
    assert all(qty == 0 for qty in venue.inventory.values())
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()
        assert not conn.execute("SELECT 1 FROM lab.pending_risk_exits").fetchone()
        legacy_decisions = conn.execute(
            "SELECT d.*,c.risk_decision_id AS claimed FROM lab.risk_decisions d "
            "JOIN lab.authorization_claims c USING(risk_decision_id) "
            "WHERE d.action IN ('CANCEL','FLATTEN')"
        ).fetchall()
        assert legacy_decisions
        assert all((d["expires_at"] - d["decided_at"]).total_seconds() <= 5
                   for d in legacy_decisions)
    assert engine.repo.get_candidate(cid)["state"] == "CLOSED"


def test_daily_exit_waits_for_terminal_cancel_and_keeps_protection_ownership(mx, legacy):
    engine, venue, _ = mx
    client, create = legacy
    _, entry = create(fill=False)
    venue.defer_cancel = True
    venue.equity = "9700"
    safety = ManagedAccountSafety(engine, client)
    safety.tick()
    assert not venue.orders_of("sell", "market")
    # Late entry fill after cancellation started is projected by the account coordinator.
    assert safety.ingest(venue.fill(entry["id"], entry["qty"]))
    assert not venue.orders_of("sell", "market")
    venue.defer_cancel = False
    for order in venue.orders.values():
        if order["status"] == "pending_cancel":
            order["status"] = "canceled"
    ManagedAccountSafety(restart(engine), client).tick()
    exits = venue.orders_of("sell", "market")
    assert len(exits) == 1 and D(exits[0]["qty"]) == venue.inventory["LEGACY"]


def test_unknown_order_is_not_adopted_or_canceled_by_legacy_exit(mx, legacy):
    engine, venue, _ = mx
    client, create = legacy
    _, entry = create()
    unknown = copy.deepcopy(entry["legs"][0])
    unknown.update(id=str(uuid4()), client_order_id=uuid4().hex, qty="1", status="new")
    venue.orders[unknown["id"]] = unknown
    venue.equity = "9700"
    safety = ManagedAccountSafety(engine, client)
    safety.tick()
    assert unknown["status"] == "new" and not venue.orders_of("sell", "market")
    with engine.repo.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM lab.execution_halts WHERE reason='ACCOUNT_EXIT_UNEXPLAINED_ORDER'"
        ).fetchone()
        assert conn.execute("SELECT 1 FROM lab.active_reservations").fetchone()


def test_unknown_legacy_entry_keeps_exit_request_and_reservation_until_original_id_recovers(
    mx, legacy, monkeypatch
):
    engine, venue, _ = mx
    client, create = legacy
    lookup = client.order_by_client_id
    monkeypatch.setattr(client, "order_by_client_id", lambda _: None)
    venue.timeout_next_post = True
    _, entry = create(fill=False)
    venue.equity = "9700"
    safety = ManagedAccountSafety(engine, client)
    safety.tick()
    restarted = ManagedAccountSafety(restart(engine), client)
    restarted.tick()
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT 1 FROM lab.active_reservations").fetchone()
        assert conn.execute("SELECT 1 FROM lab.pending_risk_exits").fetchone()
        assert not conn.execute("SELECT 1 FROM lab.risk_exit_completions").fetchone()
    assert entry["status"] == "new"
    assert len(venue.orders_of("buy")) == 1
    monkeypatch.setattr(client, "order_by_client_id", lookup)
    restarted.tick()
    assert entry["status"] == "canceled"
    assert len(venue.orders_of("buy")) == 1
