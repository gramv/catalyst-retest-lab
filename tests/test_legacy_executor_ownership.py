"""Frozen runtime ownership compatibility without starting provider connections."""

import hashlib
import threading

import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.api import create_app
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.config import Settings
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.executor_lease import AccountExecutorLease, FencedAuthorizationGate
from catalyst_lab.runtime import MarketRuntime, paper_account_identity
from catalyst_lab.watcher import Watcher
from tests.conftest import NOW, TOKEN
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

IDENTITY = hashlib.sha256(b"ALPACA_PAPER:fixture-only-account").hexdigest()


class Adapter:
    feed = "iex"
    closed = False

    def _get(self, path):
        assert path == "account"
        return {"id": "fixture-only-account"}

    def close(self):
        self.closed = True


class QuietRuntime(MarketRuntime):
    def _rest_loop(self):
        self.stop_event.wait()

    def _stream_loop(self):
        self.stop_event.wait()


class QuietMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.threads = []

    def ready(self):
        return False

    def start(self):
        pass

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=0)

    def status(self):
        return {"workers_alive": all(thread.is_alive() for thread in self.threads)}


def executor(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    adapter = Adapter()
    lease = AccountExecutorLease(risk, lambda: paper_account_identity(adapter))
    return QuietRuntime(adapter, Watcher(er), clock=lambda: NOW,
                        broker_monitor=QuietMonitor(), executor_lease=lease)


def test_legacy_and_managed_ownership_use_same_account_key_and_refuse_overlap(er):
    legacy = executor(er)
    managed = AccountExecutorLease(legacy.executor_lease.repo, lambda: IDENTITY)
    assert paper_account_identity(legacy.adapter) == IDENTITY
    legacy.start()
    try:
        assert legacy.lease is None  # No second observer connection competing with itself.
        assert legacy.status()["executor_ownership"] == "EXCLUSIVE"
        with pytest.raises(SubmissionDisabled, match="ACCOUNT_EXECUTOR_ALREADY_RUNNING"):
            managed.acquire()
    finally:
        legacy.stop()
    managed.acquire()
    managed.close()
    assert legacy.adapter.closed
    with pytest.raises(RuntimeError, match="RESTART_REQUIRES_NEW_INSTANCE"):
        legacy.start()


def test_lost_legacy_ownership_stops_watch_and_risk_loops_without_reacquisition(er):
    legacy = executor(er)
    legacy.start()
    legacy.executor_lease.connection.close()
    try:
        with pytest.raises(SubmissionDisabled, match="EXECUTOR_OWNERSHIP_LOST"):
            legacy._heartbeat()
        assert legacy.stop_event.is_set() and legacy.broker_monitor.stop_event.is_set()
        assert not legacy.ownership_ready()
        assert legacy.status()["error"] == "EXECUTOR_OWNERSHIP_LOST"
        with pytest.raises(SubmissionDisabled, match="EXECUTOR_OWNERSHIP_LOST"):
            legacy.executor_lease.acquire()
    finally:
        legacy.stop()


def test_shutdown_retains_lease_and_client_until_all_risk_threads_stop(er):
    legacy = executor(er)
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    legacy.broker_monitor.threads.append(stuck)
    stuck.start()
    legacy.start()
    challenger = AccountExecutorLease(legacy.executor_lease.repo, lambda: IDENTITY)
    try:
        legacy.stop()
        assert stuck.is_alive()
        assert not legacy.adapter.closed
        assert legacy.status()["error"] == "EXECUTOR_SHUTDOWN_INCOMPLETE"
        legacy.executor_lease.assert_owned()
        with pytest.raises(SubmissionDisabled, match="ALREADY_RUNNING"):
            challenger.acquire()
    finally:
        release.set()
        stuck.join(timeout=1)
        legacy.stop()
    assert legacy.adapter.closed and legacy.executor_lease.lost
    challenger.acquire()
    challenger.close()


def test_frozen_api_configures_fenced_transport_with_same_runtime_lease(er, monkeypatch):
    monkeypatch.setattr(AlpacaCredentials, "from_env", lambda: AlpacaCredentials(
        "PKLEGACYFIXTURE", "fixture-only-no-provider-credential"
    ))
    settings = Settings(
        er.database_url, TOKEN, read_only_market_data=True,
        risk_database_url=er.database_url.replace("user=catalyst_app", "user=catalyst_risk"),
    )
    app = create_app(settings, clock=lambda: NOW)
    runtime = app.state.market_runtime
    try:
        gate = runtime.adapter._client._transport.gate
        assert isinstance(gate, FencedAuthorizationGate)
        assert gate.lease is runtime.executor_lease
        assert not runtime.ownership_ready()
        assert runtime.risk_runtime is not None
    finally:
        runtime.stop()
