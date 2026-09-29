from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import digest
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.repository import json_safe
from catalyst_lab.setup_scan import NewsEvidence
from tests.test_managed_execution import admit_enter, observation
from tests.test_managed_execution import er as er
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import pristine_cluster as pristine_cluster

TOKEN = "fixture-private-lab-token-never-used-outside-tests"
NOW = datetime(2026, 9, 19, 18, tzinfo=UTC)


class FixtureCycle:
    def __init__(self, repo, store):
        self.repo, self.store = repo, store
        self.followups = []

    def outputs(self, cycle_id, *, after, limit):
        return [e for e in self.store.outputs(after, limit)
                if e["body"].get("cycle_id") == cycle_id]

    def submit_evidence(self, cycle_id, item_key, *, revision, sources, thesis):
        self.followups.append((cycle_id, item_key, revision, sources, thesis))
        return {"revision": revision, "evidence_hash": digest(sources[0]["excerpt"]),
                "expires_at": NOW + timedelta(minutes=1)}


@pytest.fixture
def surface(cluster):
    repo = RiskRepository(localdb.connection_url(cluster, "catalyst_risk"))
    store = ManagedStore(repo)
    cycle = FixtureCycle(repo, store)
    app = create_managed_app(cycle, store, api_token=TOKEN, runtime_status=lambda: {
        "worker_state": "STOPPED", "management_review_enabled": False,
        "trade_stream_connected": False, "account_id": "must-not-appear",
        "secret": "must-not-appear",
    })
    return TestClient(app), cycle, store


def auth():
    return {"Authorization": "Bearer " + TOKEN}


def test_independent_measurement_endpoint_requires_auth_and_existing_position(mx):
    engine, _, _ = mx
    sid, _ = admit_enter(mx)
    cycle = FixtureCycle(engine.repo, engine.store)
    app = create_managed_app(cycle, engine.store, api_token=TOKEN, runtime_status=lambda: {})
    client = TestClient(app)
    path = f"/api/v1/lab/positions/{sid}/measurement"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=auth())
    assert response.status_code == 200
    assert response.json()["cohort"] == "JEV_MANAGED_PAPER_V1"
    measurement = response.json()["measurement"]
    assert measurement["test_r"] is None and measurement["net_pnl"] is None
    assert measurement["sample_count"] == 0 and measurement["limitations"]
    assert client.get(f"/api/v1/lab/positions/{uuid4()}/measurement",
                      headers=auth()).status_code == 422


def test_dashboard_is_static_shell_without_secrets_or_autonomy_claim(surface):
    client, _, _ = surface
    page = client.get("/")
    assert page.status_code == 200
    assert "PAPER TRADING — SIMULATED. Not real money." in page.text
    assert "SUPERVISED" in page.text and "ENGINEERING TEST" in page.text
    assert TOKEN not in page.text
    assert "JEV_MANAGED_PAPER_V1" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "no-store" == page.headers["cache-control"]
    script = client.get("/lab.js").text
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script
    assert "management_review_enabled === true" in script
    assert client.get("/lab.css").status_code == 200


@pytest.mark.parametrize("path", [
    "status", "outputs", "cycles", "setups", "positions", "results",
])
def test_all_private_surfaces_require_bearer_auth(surface, path):
    client, _, _ = surface
    assert client.get("/api/v1/lab/" + path).status_code == 401
    assert client.get("/api/v1/lab/" + path,
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    reply = client.get("/api/v1/lab/" + path, headers=auth())
    assert reply.status_code == 200
    assert TOKEN not in reply.text


def test_health_and_runtime_distinguish_service_from_worker(surface):
    client, _, _ = surface
    health = client.get("/health").json()
    assert health["alpaca"] == "paper" and health["supervised"] is True
    assert "worker_state" not in health
    status = client.get("/api/v1/lab/status", headers=auth()).json()
    assert status["worker_state"] == "STOPPED" and not status["management_review_enabled"]
    assert "account_id" not in status and "secret" not in status


def test_research_and_trade_outputs_use_durable_cursor(surface):
    client, _, store = surface
    cycle_id = str(uuid4())
    with store.transaction() as conn:
        event = store.event(conn, "RESEARCH_STARTED", {
            "cycle_id": cycle_id, "contender_count": 20,
            "expires_at": NOW + timedelta(minutes=1),
        })
    response = client.get(f"/api/v1/lab/cycles/{cycle_id}/outputs", headers=auth()).json()
    assert response["items"][0]["event_seq"] == event["event_seq"]
    assert response["next_cursor"] == event["event_seq"]
    empty = client.get(f"/api/v1/lab/cycles/{cycle_id}/outputs?after={event['event_seq']}",
                       headers=auth()).json()
    assert empty["items"] == [] and empty["next_cursor"] == event["event_seq"]
    cycles = client.get("/api/v1/lab/cycles", headers=auth()).json()["items"]
    assert cycles[0]["cycle_id"] == cycle_id and cycles[0]["contender_count"] == 20


def test_scanner_exclusions_are_not_presented_as_jev_rejections(surface):
    client, _, store = surface
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_STARTED", {
            "cycle_id": str(uuid4()), "contender_count": 1,
            "expires_at": NOW + timedelta(minutes=1),
            "scan": {"decisions": [
                {"symbol": "BTC/USD", "disposition": "REJECTED",
                 "reasons": ["BAR_GAP_OR_ORDER_INVALID", "NEWS_EVIDENCE_REQUIRED"]},
                {"symbol": "ETH/USD", "disposition": "NEEDS_EVIDENCE",
                 "reasons": ["NEWS_EVIDENCE_REQUIRED"]},
                {"symbol": "SOL/USD", "disposition": "CONTENDER", "reasons": []},
            ]},
        })
    scan = client.get("/api/v1/lab/cycles", headers=auth()).json()["items"][0]["scan"]
    assert scan["screened"] == 3
    assert scan["technical_exclusions"] == 1 and scan["needs_evidence"] == 1
    assert scan["items"][0]["reasons"] == [
        "BAR_GAP_OR_ORDER_INVALID", "NEWS_EVIDENCE_REQUIRED"
    ]
    assert "jev" not in str(scan).lower()


def packet():
    text = "Fixture original-source update with relevant economic evidence."
    source = NewsEvidence("fixture", "https://issuer.example/update", text, NOW, NOW,
                          digest(text), True, True, "NEW_FACT", "SUPPORTS")
    return {"item_key": "US:fixture", "revision": 2, "sources": json_safe([asdict(source)]),
            "thesis": "Updated evidence supports the retest hypothesis.",
            "disproof": "Issuer withdraws the evidence.", "economic_relationship": "Product sales."}


def test_bounded_evidence_endpoint_passes_typed_source_revision_to_cycle(surface):
    client, cycle, _ = surface
    cycle_id = str(uuid4())
    response = client.post(f"/api/v1/lab/cycles/{cycle_id}/evidence", headers=auth(), json=packet())
    assert response.status_code == 200
    assert response.json()["trade_authorized"] is False
    assert response.json()["status"] == "EVIDENCE_RECORDED"
    call = cycle.followups[0]
    assert call[:3] == (cycle_id, "US:fixture", 2)
    assert isinstance(call[3][0], dict)
    assert datetime.fromisoformat(call[3][0]["retrieved_at"]).tzinfo is not None


@pytest.mark.parametrize("extra", ["quantity", "broker_payload", "risk_pct", "expires_at"])
def test_evidence_endpoint_rejects_execution_or_deadline_overrides(surface, extra):
    client, cycle, _ = surface
    body = {**packet(), extra: "override"}
    response = client.post(f"/api/v1/lab/cycles/{uuid4()}/evidence", headers=auth(), json=body)
    assert response.status_code == 422 and not cycle.followups


def test_evidence_endpoint_auth_duplicates_size_and_secret_handling(surface):
    client, cycle, _ = surface
    path = f"/api/v1/lab/cycles/{uuid4()}/evidence"
    assert client.post(path, json=packet()).status_code == 401
    assert client.post(path, headers=auth(),
                       content='{"revision":2,"revision":3}').status_code == 422
    assert client.post(path, headers=auth(), content="x" * 32769).status_code == 413
    body = {**packet(), "thesis": "Contact person@example.test for approval."}
    reply = client.post(path, headers=auth(), json=body)
    assert reply.status_code == 422 and "person@example" not in reply.text
    assert not cycle.followups


@pytest.mark.parametrize("path", ["orders", "execute", "policy", "resume", "size", "override"])
def test_no_raw_broker_or_policy_mutation_routes(surface, path):
    client, _, _ = surface
    assert client.post("/api/v1/lab/" + path, headers=auth(), json={}).status_code == 404


def test_results_exclude_frozen_baseline_and_do_not_fabricate_metrics(surface):
    client, _, _ = surface
    body = client.get("/api/v1/lab/results", headers=auth()).json()
    assert body["cohort"] == "JEV_MANAGED_PAPER_V1" and body["baseline_included"] is False
    assert body["items"] == []


def test_crypto_asset_fee_is_not_reported_as_an_open_dust_position(mx):
    engine, venue, _ = mx
    setup_id, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], "1", fee_qty="0.0001"))
    engine.manage(setup_id, observation(mx))
    engine.manage(setup_id, observation(mx, bid="111", ask="111.01"))
    engine.manage(setup_id, observation(mx, bid="111", ask="111.01"))
    exit_order = venue.orders_of("sell", "market")[0]
    assert exit_order["qty"] == "0.9999"
    engine.ingest(venue.fill(exit_order["id"], exit_order["qty"], price="111"))
    engine.manage(setup_id, observation(mx))
    assert engine._load(setup_id)[1]["state"] == "CLOSED"
    cycle = FixtureCycle(engine.repo, engine.store)
    client = TestClient(create_managed_app(
        cycle, engine.store, api_token=TOKEN, runtime_status=lambda: {"worker_state": "STOPPED"},
    ))
    assert client.get("/api/v1/lab/positions", headers=auth()).json()["items"] == []
    closed = client.get("/api/v1/lab/results", headers=auth()).json()["items"][0]
    assert closed["gross_fill_balance"] == "0.0001"
    assert Decimal(closed["open_qty"]) == 0 and closed["quantity_source"] == "BROKER_POSITION"
    assert closed["gross_pnl_usd"] is not None
    assert closed["net_pnl_usd"] is None and not closed["fees_complete"]
    assert closed["measurement"]["test_r"] is not None
    assert closed["measurement"]["net_pnl"] is None
    assert not closed["measurement"]["fees_verified"]
    assert closed["measurement"]["limitations"]


@pytest.mark.parametrize("has_timestamp", [True, False])
def test_late_inventory_event_cannot_phantom_reopen_closed_dashboard_position(mx, has_timestamp):
    engine, venue, _ = mx
    setup_id, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], "1"))
    engine.manage(setup_id, observation(mx))
    engine.manage(setup_id, observation(mx, bid="111", ask="111.01"))
    engine.manage(setup_id, observation(mx, bid="111", ask="111.01"))
    exit_order = venue.orders_of("sell", "market")[0]
    engine.ingest(venue.fill(exit_order["id"], exit_order["qty"], price="111"))
    engine.manage(setup_id, observation(mx))
    assert engine._load(setup_id)[1]["state"] == "CLOSED"
    old_inventory = {"qty": "1", "symbol": "BTC/USD", "broker_event_id": "fixture-delayed"}
    if has_timestamp:
        old_inventory["occurred_at"] = (venue.now - timedelta(seconds=1)).isoformat()
    with engine.store.transaction() as conn:
        engine.store.event(conn, "BROKER_POSITION", old_inventory, setup_id=setup_id)
    client = TestClient(create_managed_app(
        FixtureCycle(engine.repo, engine.store), engine.store,
        api_token=TOKEN, runtime_status=lambda: {},
    ))
    assert client.get("/api/v1/lab/positions", headers=auth()).json()["items"] == []
    closed = client.get("/api/v1/lab/results", headers=auth()).json()["items"]
    assert len(closed) == 1 and closed[0]["state"]["state"] == "CLOSED"
    assert Decimal(closed[0]["open_qty"]) == 0
    assert closed[0]["measurement"]["test_r"] is not None
