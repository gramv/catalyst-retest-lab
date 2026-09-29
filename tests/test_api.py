from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.api import create_app
from catalyst_lab.config import MUSE_SCOPES, Settings
from tests.conftest import NOW, TOKEN


def test_health_reports_local_phase_honestly(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["alpaca"] == "paper"
    assert health["strategy_version"] == "CATALYST_RETEST_V1"
    assert not health["broker_connected"] and not health["trading_enabled"]


def test_bad_rr_is_immutable_rejection_visible_in_log(client, raw):
    response = client.post("/api/v1/candidates", json=raw | {"target": "101"})
    assert response.status_code == 201
    record = response.json()
    assert record["state"] == "REJECTED"
    assert record["failed_rule"] == "MIN_REWARD_RISK"
    candidate_id = record["candidate_id"]
    assert client.get(f"/api/v1/candidates/{candidate_id}").json() == record
    assert candidate_id in {
        r["candidate_id"] for r in client.get("/api/v1/candidates").json()["items"]
    }
    events = client.get(f"/api/v1/candidates/{candidate_id}/events").json()["items"]
    assert [
        e["payload_json"]["to_state"] for e in events if e["event_type"] == "STATE_TRANSITION"
    ] == ["RECEIVED", "VALIDATING", "REJECTED"]


def test_good_candidate_validates_without_orders(client, raw, repo):
    record = client.post("/api/v1/candidates", json=raw).json()
    assert record["state"] == "VALIDATED"
    assert record["evidence_source"] == "LAB_FIXTURE"
    with repo.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.orders WHERE candidate_id=%s",
                (record["candidate_id"],),
            ).fetchone()["n"]
            == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM lab.trades").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"size_shares": 1000},
        {"override": True},
        {"market": "IN"},
        {"strategy_version": "OTHER"},
        {"stop": "NaN"},
        {"thesis": ""},
        {"expires_at": "2026-09-18T16:00:00"},
        {"data_feed": "SIP"},
    ],
)
def test_schema_violations_persist_reasons(client, raw, changes):
    record = client.post("/api/v1/candidates", json=raw | changes).json()
    assert record["state"] == "REJECTED"
    assert record["failed_rule"] == "INVALID_SCHEMA"
    assert record["payload_json"] == raw | changes


def test_duplicate_signal_and_ticker_day(client, raw):
    assert client.post("/api/v1/candidates", json=raw).json()["state"] == "VALIDATED"
    second = client.post("/api/v1/candidates", json=raw).json()
    assert second["failed_rule"] == "DUPLICATE_SIGNAL_ID"
    third = client.post(
        "/api/v1/candidates", json=raw | {"signal_id": raw["signal_id"] + "-2"}
    ).json()
    assert third["failed_rule"] == "TICKER_ALREADY_ATTEMPTED"
    assert second["candidate_id"] != third["candidate_id"]


def test_rejected_attempt_also_consumes_ticker_day(client, raw):
    assert (
        client.post("/api/v1/candidates", json=raw | {"target": "101"}).json()["state"]
        == "REJECTED"
    )
    retry = client.post("/api/v1/candidates", json=raw | {"signal_id": raw["signal_id"] + "-retry"})
    assert retry.json()["failed_rule"] == "TICKER_ALREADY_ATTEMPTED"


def test_auth_and_permissions(client, raw):
    assert MUSE_SCOPES == {"candidate:create", "candidate:read", "analytics:read"}
    assert (
        client.post("/api/v1/candidates", json=raw, headers={"Authorization": ""}).status_code
        == 401
    )
    assert (
        client.get("/api/v1/analytics", headers={"Authorization": "Bearer bad"}).status_code == 401
    )
    routes = {route.path for route in client.app.routes}
    assert not routes & {"/execute", "/place-order", "/override", "/api/v1/orders"}
    assert client.post("/api/v1/execute").status_code == 404


def test_execution_diagnostics_are_scoped_and_read_only(client):
    assert client.get("/api/v1/execution", headers={"Authorization": ""}).status_code == 401
    result = client.get("/api/v1/execution")
    assert result.status_code == 200 and result.json()["trading_enabled"] is False
    assert client.post("/api/v1/execution", json={"trading_enabled": True}).status_code == 405


def test_transport_bounds(client):
    assert client.post("/api/v1/candidates", content="x" * 65537).status_code == 413
    assert client.post("/api/v1/candidates", content='{"stop": NaN}').status_code == 400
    assert client.post("/api/v1/candidates", content="{").status_code == 400
    assert client.post("/api/v1/candidates", json=[]).json()["failed_rule"] == "INVALID_SCHEMA"


def test_no_broker_provider_fails_closed(cluster, raw, repo):
    app = create_app(Settings(localdb.connection_url(cluster), TOKEN), clock=lambda: NOW)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        record = client.post("/api/v1/candidates", json=raw).json()
        assert record["failed_rule"] == "DATA_FEED_FAILURE"
        with repo.connect() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM lab.system_events "
                    "WHERE event_type = 'DATA_FEED_FAILURE'"
                ).fetchone()["n"]
                > 0
            )


def test_provider_exception_is_not_exposed(cluster, raw, policy):
    def broken_provider(candidate, now):
        raise RuntimeError("private-provider-secret")

    app = create_app(
        Settings(localdb.connection_url(cluster), TOKEN),
        provider=broken_provider,
        policy=policy,
        clock=lambda: NOW,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        response = client.post("/api/v1/candidates", json=raw)
        assert "private-provider-secret" not in response.text
        assert response.json()["failed_rule"] == "DATA_FEED_FAILURE"


def test_research_fixtures_do_not_become_headline_trades(client, raw):
    client.post("/api/v1/candidates", json=raw)
    result = client.get("/api/v1/analytics").json()
    assert result["execution_source"] == "ALPACA_PAPER" and result["market"] == "US"
    assert result["trade_count"] == 0
    assert "SIMULATED" in result["label"]


def test_different_day_allows_new_ticker_attempt(repo, raw, evidence, policy):
    from datetime import timedelta

    repo.submit(raw, NOW, lambda c, now: evidence, policy)
    tomorrow = NOW + timedelta(days=3)
    next_evidence = replace(
        evidence,
        session_date=tomorrow.date(),
        observed_at=tomorrow,
        quote_timestamp=tomorrow,
        reconciled_session=tomorrow.date(),
        official_open=tomorrow.replace(hour=9, minute=30),
        official_close=tomorrow.replace(hour=16),
    )
    result = repo.submit(
        raw | {"signal_id": raw["signal_id"] + "-next"},
        tomorrow,
        lambda c, now: next_evidence,
        policy,
    )
    assert result["state"] == "VALIDATED"


def test_read_only_runtime_reports_admission_gate_without_fabricating_evidence(cluster, raw):
    from catalyst_lab.validation import EvidenceUnavailable

    class Runtime:
        started = stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def status(self, private=False):
            result = {"broker_connected": True, "execution_enabled": False}
            if private:
                result["account"] = {"equity": "10000"}
            return result

        def validation_evidence(self, candidate, now):
            raise EvidenceUnavailable("STARTUP_RECONCILIATION_REQUIRED", "Reconciliation pending")

    runtime = Runtime()
    app = create_app(
        Settings(localdb.connection_url(cluster), TOKEN), market_runtime=runtime, clock=lambda: NOW
    )
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        assert runtime.started
        health = client.get("/health").json()
        assert health["broker_connected"] and not health["trading_enabled"]
        assert "account" not in health["market_data"]
        assert client.get("/api/v1/market-data").json()["account"]["equity"] == "10000"
        assert client.get("/api/v1/market-data", headers={"Authorization": ""}).status_code == 401
        record = client.post("/api/v1/candidates", json=raw).json()
        assert record["failed_rule"] == "STARTUP_RECONCILIATION_REQUIRED"
        bad = raw | {
            "ticker": raw["ticker"] + "X",
            "signal_id": raw["signal_id"] + "X",
            "target": "101",
        }
        assert (
            client.post("/api/v1/candidates", json=bad).json()["failed_rule"] == "MIN_REWARD_RISK"
        )
    assert runtime.stopped
