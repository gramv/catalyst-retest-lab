from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_app import (
    AppSettings,
    build_app_from_env,
    create_application,
)
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.scan_sources import SourcePolicy

NOW = datetime(2026, 9, 18, 18, tzinfo=UTC)
TOKEN = "fixture-managed-app-token-not-used-outside-tests"


class ClosingClient:
    def __init__(self):
        self.closed = False
        self.policy = SourcePolicy("iex", 5, 50, 1000, 2, 5)

    def close(self):
        self.closed = True

    def asset(self, symbol):
        return {
            "symbol": symbol,
            "class": "crypto" if "/" in symbol else "us_equity",
            "status": "active",
            "tradable": True,
        }


@pytest.fixture
def runtime(cluster):
    repo = RiskRepository(localdb.connection_url(cluster, "catalyst_risk"))
    store = ManagedStore(repo)
    reviewer = JevReviewer(
        JevStore(localdb.connection_url(cluster, "catalyst_jev")),
        ReliabilityPolicy("MANAGED_APP_FIXTURE", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-key",
        transport=httpx.MockTransport(
            lambda r: pytest.fail("Report intake must not synchronously call Jev")
        ),
        clock=lambda: NOW,
    )
    research = ResearchCycle(repo, reviewer, CyclePolicy(10, 10, 15, 60, 30), clock=lambda: NOW)
    runtime = SimpleNamespace(
        execution=SimpleNamespace(repo=repo, store=store, broker=ClosingClient()),
        research=research,
        now=lambda: NOW,
        credentials=object(),
        source=ClosingClient(),
        started=0,
        stopped=0,
    )

    def start():
        runtime.started += 1

    def stop():
        runtime.stopped += 1

    runtime.start, runtime.stop = start, stop
    runtime.status = lambda: {
        "workers_alive": runtime.started > runtime.stopped,
        "position_jev_configured": True,
        "entry_ready": False,
        "research_healthy": True,
        "trade_updates_connected": False,
    }
    return runtime


@pytest.fixture
def settings():
    return AppSettings(
        8799,
        TOKEN,
        300,
        (),
        CLASSIFICATION_POLICY,
        (
            {"ticker": "TESTA", "sector": "FIXTURE_A", "theme": "FIXTURE_A"},
            {"ticker": "TESTB", "sector": "FIXTURE_B", "theme": "FIXTURE_B"},
        ),
    )


@pytest.fixture
def source_factory():
    sources = []

    class Source:
        def __init__(self, *args, **kwargs):
            pytest.fail("Report intake must not create a discovery scanner")

    return Source, sources


def body():
    return {
        "report_id": str(uuid4()),
        "generated_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
        "items": [{
            "signal_id": "TEST-EXTERNAL-MUSE-A",
            "market": "US_STOCKS", "symbol": "TESTA", "direction": "LONG",
            "catalyst": "PRODUCT", "thesis": "New availability supports a retest hypothesis.",
            "disproof": "The issuer withdraws the product.",
            "economic_relationship": "The issuer sells the announced product.",
            "technical_analysis": "Muse observed support and resistance using external price data.",
            "levels": {"entry_trigger": "106", "max_entry_price": "106.1",
                       "stop": "104", "target": "111"},
            "sources": [{"source_id": "issuer", "url": "https://issuer.example/product",
                         "excerpt": "Fixture issuer launches a new product for paying customers.",
                         "published_at": (NOW - timedelta(hours=1)).isoformat(),
                         "retrieved_at": NOW.isoformat()}],
        }],
    }


def auth():
    return {"Authorization": "Bearer " + TOKEN}


def test_external_muse_report_bypasses_scanner_and_preserves_worker_lifecycle(
    runtime, settings, source_factory
):
    factory, sources = source_factory
    app = create_application(runtime, settings, source_factory=factory)
    with TestClient(app) as client:
        assert runtime.started == 1 and runtime.stopped == 0
        status = client.get("/api/v1/lab/status", headers=auth()).json()
        assert status["worker_state"] == "RUNNING" and not status["entry_ready"]
        raw = body()
        response = client.post("/api/v1/lab/research-reports", headers=auth(), json=raw)
        assert response.status_code == 202
        assert response.json()["contender_count"] == 1
        assert response.json()["trade_authorized"] is False
        events = client.get(response.json()["polling_url"], headers=auth()).json()["items"]
        packet = next(e["body"] for e in events if e["kind"] == "RESEARCH_PACKET")
        assert packet["levels"] == raw["items"][0]["levels"]
        # One neutral label for every proposer (plan 1.3); the reviewer never learns who.
        assert packet["state"]["technical_context"]["origin"] == "EXTERNAL_RESEARCH_AGENT"
        assert "all_technical_eligibility_checks_passed" not in str(packet)
        assert not any("SCAN" in e["kind"] for e in events)
        assert not sources and not runtime.source.closed
        assert client.post("/api/v1/lab/research-scans", headers=auth(), json={}).status_code == 404
        replay = client.post("/api/v1/lab/research-reports", headers=auth(), json=raw)
        assert replay.json()["idempotent_replay"] is True
        raw["items"][0]["thesis"] += " Changed."
        mismatch = client.post("/api/v1/lab/research-reports", headers=auth(), json=raw)
        assert mismatch.status_code == 422
        assert mismatch.json()["detail"] == "REPORT_IDEMPOTENCY_CONTENT_MISMATCH"
    assert runtime.stopped == 1 and runtime.source.closed and runtime.execution.broker.closed


def test_status_exposes_jev_breaker_and_todays_call_count(runtime, settings, source_factory):
    """Package jev-breaker: the two new fields must survive both the mapping in
    create_application.status() and the managed_service.STATUS_FIELDS allowlist that
    filters the actual HTTP response."""
    factory, _ = source_factory
    base = runtime.status
    runtime.status = lambda: {
        **base(),
        "jev_breaker": {"state": "OPEN", "epoch": 2, "blocked_until": "2026-09-26T00:00:30Z"},
        "jev_calls_today": {"attempts": 4, "receipts": 6},
    }
    app = create_application(runtime, settings, source_factory=factory)
    with TestClient(app) as client:
        status = client.get("/api/v1/lab/status", headers=auth()).json()
        assert status["jev_breaker"] == {
            "state": "OPEN", "epoch": 2, "blocked_until": "2026-09-26T00:00:30Z",
        }
        assert status["jev_calls_today"] == {"attempts": 4, "receipts": 6}


@pytest.mark.parametrize("extra", ["qty", "risk_pct", "execution_url", "jev_approved"])
def test_muse_report_rejects_execution_authority(runtime, settings, source_factory, extra):
    client = TestClient(create_application(runtime, settings, source_factory=source_factory[0]))
    raw = body()
    raw["items"][0][extra] = "not-allowed"
    response = client.post("/api/v1/lab/research-reports", headers=auth(), json=raw)
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_MUSE_REPORT"


def test_report_transport_limits_auth_and_sanitized_errors(runtime, settings, source_factory):
    client = TestClient(create_application(runtime, settings, source_factory=source_factory[0]))
    route = "/api/v1/lab/research-reports"
    assert client.post(route, json=body()).status_code == 401
    assert client.post(route, headers=auth(), content="x" * 1048577).status_code == 413
    assert client.post(route, headers={**auth(), "X-Large": "x" * 16385},
                       json=body()).status_code == 431
    assert client.post(route, headers=auth(),
                       content='{ "report_id": 1, "report_id": 2 }').status_code == 422
    raw = body()
    raw["items"][0]["technical_analysis"] = "Contact owner@example.test."
    reply = client.post(route, headers=auth(), json=raw)
    assert reply.status_code == 422 and "owner@example" not in reply.text


def test_explicit_env_required_before_constructing_runtime(monkeypatch):
    names = [
        "MANAGED_HTTP_PORT",
        "MANAGED_API_TOKEN",
        "MANAGED_CRYPTO_CLASSIFICATIONS_JSON",
        "MANAGED_REPORT_MAX_SECONDS",
        "MANAGED_CLASSIFICATION_POLICY",
        "MANAGED_US_CLASSIFICATIONS_JSON",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    calls = []
    with pytest.raises(ValueError, match="REQUIRED_MANAGED_APP_CONFIGURATION"):
        build_app_from_env(runtime_builder=lambda: calls.append(True))
    assert not calls
    values = {
        "MANAGED_HTTP_PORT": "8799",
        "MANAGED_API_TOKEN": TOKEN,
        "MANAGED_CRYPTO_CLASSIFICATIONS_JSON": "[]",
        "MANAGED_REPORT_MAX_SECONDS": "300",
        "MANAGED_CLASSIFICATION_POLICY": CLASSIFICATION_POLICY,
        "MANAGED_US_CLASSIFICATIONS_JSON": encoded(
            [{"ticker": "TESTA", "sector": "FIXTURE_A", "theme": "FIXTURE_A"}]
        ),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = AppSettings.from_env()
    assert settings.port == 8799 and TOKEN not in repr(settings)
    for name in names:
        with monkeypatch.context() as missing:
            missing.delenv(name)
            with pytest.raises(ValueError, match="REQUIRED_MANAGED_APP_CONFIGURATION"):
                AppSettings.from_env()


def test_cli_binds_only_loopback_without_access_logs(monkeypatch, settings):
    from catalyst_lab import managed_app

    captured, served = [], []
    application = object()
    monkeypatch.setattr(managed_app, "build_app_from_env", lambda: (application, settings))
    monkeypatch.setattr("uvicorn.Config", lambda app, **kwargs: captured.append((app, kwargs)))

    class Server:  # main() keeps the server so a lost executor lease can stop it (phase 0).
        def __init__(self, config):
            self.started = self.should_exit = False

        def run(self):
            served.append(True)
            self.started = True

    monkeypatch.setattr("uvicorn.Server", Server)
    managed_app.main()
    assert served == [True]
    assert captured == [
        (
            application,
            {"host": "127.0.0.1", "port": 8799, "access_log": False, "log_level": "warning"},
        )
    ]


def test_classifications_are_imported_before_worker_start(runtime, settings, source_factory):
    observed = []
    original_start = runtime.start

    def start():
        with runtime.execution.repo.connect() as conn:
            rows = conn.execute("SELECT ticker FROM lab.current_classifications").fetchall()
        observed.extend(row["ticker"] for row in rows)
        original_start()

    runtime.start = start
    app = create_application(runtime, settings, source_factory=source_factory[0])
    with TestClient(app):
        assert {"TESTA", "TESTB"} <= set(observed)
        assert app.state.classification_checkpoint["configured"] == 2


def test_explicit_crypto_universe_has_shared_server_budget(runtime, settings, source_factory):
    configured = replace(
        settings,
        crypto_symbols=("BTC/USD", "ETH/USD"),
        us_mapping=(),
    )
    app = create_application(runtime, configured, source_factory=source_factory[0])
    with TestClient(app):
        with runtime.execution.repo.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lab.current_classifications WHERE ticker IN ('BTC/USD','ETH/USD')"
            ).fetchall()
        assert {row["ticker"] for row in rows} == {"BTC/USD", "ETH/USD"}
        assert all(row["sector"] == "CRYPTO" and row["theme"] == "CRYPTO_SHARED" for row in rows)


def test_crypto_metadata_discovery_uses_separate_readonly_source(runtime, settings):
    created = []

    class Source:
        def __init__(self, credentials, policy, *, clock):
            self.closed = False
            created.append(self)

        def _metadata(self, market):
            assert market == "CRYPTO"
            return {
                "BTC/USD": {"class": "crypto", "status": "active", "tradable": True},
                "ETH/BTC": {"class": "crypto", "status": "active", "tradable": True},
            }, []

        def close(self):
            self.closed = True

    configured = replace(
        settings, crypto_symbols=None, us_mapping=()
    )
    app = create_application(runtime, configured, source_factory=Source)
    with TestClient(app):
        assert len(created) == 1 and created[0].closed
        assert app.state.classification_checkpoint["configured"] == 1
        assert not runtime.source.closed


def test_invalid_classifications_and_policy_fail_before_runtime(settings):
    with pytest.raises(ValueError, match="CRYPTO_CLASSIFICATION_SYMBOL_INVALID"):
        replace(settings, crypto_symbols=("bad",))
    with pytest.raises(ValueError, match="REQUIRED_MANAGED_APP_CONFIGURATION_INVALID"):
        replace(settings, classification_policy="unknown")


def test_classification_conflict_prevents_worker_start(runtime, settings, source_factory):
    from catalyst_lab.managed_classification import initialize_managed_classifications

    initialize_managed_classifications(
        runtime.execution.repo,
        runtime.execution.broker,
        policy_id=CLASSIFICATION_POLICY,
        us_mapping=({"ticker": "CLASH", "sector": "OTHER", "theme": "OTHER"},),
        crypto_symbols=(),
    )
    configured = replace(
        settings,
        us_mapping=({"ticker": "CLASH", "sector": "FIXTURE", "theme": "FIXTURE"},),
    )
    app = create_application(runtime, configured, source_factory=source_factory[0])
    with pytest.raises(ValueError, match="EXISTING_CLASSIFICATION_CONFLICT"), TestClient(app):
        pass
    assert runtime.started == 0
    assert runtime.source.closed and runtime.execution.broker.closed


def test_app_closes_separate_monitor_source_and_all_owned_resources(
    runtime, settings, source_factory
):
    monitor_source = ClosingClient()
    runtime.owned_resources = (runtime.source, monitor_source, runtime.execution.broker)
    app = create_application(runtime, settings, source_factory=source_factory[0])
    with TestClient(app):
        assert not monitor_source.closed
    assert monitor_source.closed and runtime.source.closed and runtime.execution.broker.closed
