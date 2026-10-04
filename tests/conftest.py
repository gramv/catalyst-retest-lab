import socket
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.api import create_app
from catalyst_lab.config import Settings
from catalyst_lab.domain import Evidence, Policy
from catalyst_lab.repository import Repository

NOW = datetime.fromisoformat("2026-09-18T10:00:00-04:00")
TOKEN = "test-only-token-never-used-outside-tests-123456"


@pytest.fixture(autouse=True)
def isolate_provider_credentials(monkeypatch, tmp_path):
    """Tests must never consume the owner's real .env or injected provider key."""
    monkeypatch.setenv("TYPESAFE_ENV_FILE", str(tmp_path / "missing-fixture.env"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.setenv("CATALYST_ENVIRONMENT", "local")


@pytest.fixture(autouse=True)
def pre_trade_plan_ledger_default(request, monkeypatch):
    """Package trade-plan: on a schema-27 ledger an engine plans report-V3 crypto picks
    (CRYPTO_TRADE_PLAN_V1). Tests written before it admit on research levels, as their engines
    always did; a test marked ``trade_plan`` (or passing ``trade_plan_enabled=True``) gets the
    plan."""
    if request.node.get_closest_marker("trade_plan") is None:
        from catalyst_lab import trade_plan

        monkeypatch.setattr(trade_plan, "ADMITTED", None)


@pytest.fixture(autouse=True)
def no_external_test_connections(monkeypatch):
    """Tests use private Unix-socket PostgreSQL and fake broker transports only."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guard(method):
        def connect(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                raise AssertionError("External TCP connections are prohibited in tests")
            return method(sock, address)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(original_connect_ex))


@pytest.fixture(scope="session")
def cluster():
    # A short private path also stays below the Unix-domain socket path limit.
    with tempfile.TemporaryDirectory(prefix="catalyst-test-", dir="/tmp") as directory:
        root = Path(directory)
        localdb.start(root)
        try:
            yield root
        finally:
            localdb.stop(root)


@pytest.fixture
def repo(cluster):
    return Repository(localdb.connection_url(cluster))


@pytest.fixture
def raw():
    suffix = uuid4().hex[:10].upper()
    return {
        "strategy_version": "CATALYST_RETEST_V1",
        "signal_id": f"2026-09-18-{suffix}",
        "market": "US",
        "ticker": "T" + suffix,
        "entry_trigger": "100.00",
        "max_entry_price": "100.15",
        "stop": "99.00",
        "target": "102.45",
        "catalyst": "EARNINGS",
        "thesis": "Fixture only",
        "disproof": "Loss of level",
    }


@pytest.fixture
def policy():
    return Policy(D("5"), D("10"), D("20000000"), D("5"))


@pytest.fixture
def evidence(raw):
    return Evidence(
        source="LAB_FIXTURE",
        observed_at=NOW,
        session_date=NOW.date(),
        official_open=NOW.replace(hour=9, minute=30),
        official_close=NOW.replace(hour=16),
        calendar_provider="LAB_FIXTURE",
        ticker=raw["ticker"],
        asset_class="us_equity",
        tradable=True,
        data_provider="LAB_FIXTURE",
        data_feed="IEX_FIXTURE",
        quote_timestamp=NOW - timedelta(seconds=1),
        bid=D("99.99"),
        ask=D("100.01"),
        average_daily_dollar_volume=D("100000000"),
        feed_healthy=True,
        reconciled_session=NOW.date(),
        unexplained_positions=False,
        sector="Technology",
        theme="Semiconductors",
        open_sectors=frozenset(),
        open_themes=frozenset(),
        equity=D("5000"),
        start_of_day_equity=D("5000"),
        realized_pnl_today=D("0"),
        open_unrealized_pnl=D("0"),
        open_planned_risk=D("0"),
        daily_halted=False,
    )


@pytest.fixture
def client(cluster, evidence, policy):
    app = create_app(
        Settings(localdb.connection_url(cluster), TOKEN),
        provider=lambda candidate, now: replace(evidence, ticker=candidate.ticker),
        policy=policy,
        clock=lambda: NOW,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as test_client:
        yield test_client
