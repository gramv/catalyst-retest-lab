"""Unattended safety (plan phase 0, 2026-09-26): a lost executor lease, refused closes, halt
status and the Muse job alarm.

Disposable PostgreSQL, the fake paper venue (``ManagedVenue``), fake clocks, stub entry points
and a temporary Muse spool only: FIXTURE EVIDENCE ONLY, never broker, provider or supervisor
acceptance. No network, provider or owner ledger is touched.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import managed_app, managed_runtime
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.executor_lease import AccountExecutorLease, FencedAuthorizationGate
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_account_safety import ManagedAccountSafety
from catalyst_lab.managed_app import AppSettings, create_application
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_execution import (
    EXIT_REFUSAL_ALARM_THRESHOLD,
    EXIT_RETRY_BASE_SECONDS,
    EXIT_RETRY_CAP_SECONDS,
    ManagedExecution,
    engineering_execution_policy,
    exit_retry_delay,
)
from catalyst_lab.managed_ops import (
    execution_halt_alarms,
    exit_refusal_alarms,
    muse_heartbeat_alarm,
    muse_spool_status,
    status_alarms,
)
from catalyst_lab.managed_runtime import (
    EXECUTOR_OWNERSHIP_LOST_EXIT_CODE,
    ManagedRuntime,
    engineering_runtime_policy,
)
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.muse_worker import MuseSpool
from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import ManagedVenue, admit_enter, observation
from tests.test_managed_execution import mx as mx

CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
IDENTITY = hashlib.sha256(b"fixture-unattended-safety-paper-account").hexdigest()
TOKEN = "fixture-unattended-safety-status-token-not-used-outside-tests"
TARGET = {"trade_price": "111", "bid": "111", "ask": "111.01"}  # The fixture target is 111.
GAP = {"trade_price": "90", "bid": "90", "ask": "90.01"}  # Through the fixture stop at 95.
WATCHDOG = {"tick_max_age_seconds": 60, "reconciliation_max_age_seconds": 60,
            "research_max_age_seconds": 60}


def events(engine, kind, sid=None):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind=%s
            AND (%s::uuid IS NULL OR setup_id=%s::uuid) ORDER BY event_seq""",
            (kind, sid, sid),
        ).fetchall()
    return [row["body"] for row in rows]


def mutations(venue):
    return [call for call in venue.calls if call[0] in {"POST", "DELETE", "PATCH"}]


def open_position(mx, symbol):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, symbol)
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == symbol)
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "OPEN"
    return sid


# --- 1. A lost executor lease ends the process ------------------------------------------------


class Process:
    """One app process as build_runtime_from_env wires it: its own lease, and every broker
    mutation (managed and legacy clients) behind a lease-fenced authorization gate."""

    def __init__(self, er, venue):
        self.risk = RiskRepository(
            er.database_url.replace("user=catalyst_app", "user=catalyst_risk")
        )
        self.reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
        self.lease = AccountExecutorLease(self.risk, lambda: IDENTITY)
        self.broker = ManagedPaperBroker(CREDENTIALS, FencedAuthorizationGate(
            ManagedAuthorizationGate(self.risk, clock=lambda: venue.now), self.lease
        ), transport=httpx.MockTransport(venue.handle))
        self.legacy = RiskAuthorizedPaperClient(CREDENTIALS, FencedAuthorizationGate(
            AuthorizationGate(self.risk, clock=lambda: venue.now), self.lease
        ), transport=httpx.MockTransport(venue.handle))
        self.engine = ManagedExecution(
            self.risk, self.broker, policy=engineering_execution_policy(),
            clock=lambda: venue.now, review_store=self.reviews,
        )
        self.run = ManagedRuntime(
            self.engine, SimpleNamespace(repo=self.risk),
            SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")), CREDENTIALS,
            engineering_runtime_policy(), clock=lambda: venue.now,
            reviewer_heartbeat=lambda: True, executor_lease=self.lease,
            account_safety=ManagedAccountSafety(self.engine, self.legacy),
        )
        self.run._selected_packets = lambda: []
        self.fatal = []
        self.run.on_fatal = self.fatal.append  # The entry point's exit hook, recorded.
        self.lease.acquire()
        assert self.engine.reconcile()["clean"]
        self.mx = (self.engine, venue, self.reviews)

    def drop_lock(self):
        """What a PostgreSQL restart or a terminated backend does to the lease session."""
        with self.lease.mutex:
            self.lease.connection.execute("SELECT pg_advisory_unlock_all()")

    def close(self):
        self.lease.close()
        self.broker.close()
        self.legacy.close()


@pytest.fixture
def process(er):
    venue = ManagedVenue()
    started = [Process(er, venue)]
    yield started[0], venue, lambda: started.append(Process(er, venue)) or started[-1]
    for each in started:
        each.close()


def test_lost_lease_ends_the_runtime_before_any_broker_mutation(process):
    owner, venue, successor = process
    engine, run = owner.engine, owner.run
    sid = open_position(owner.mx, "BTC/USD")
    [stop] = venue.orders_of("sell", "stop_limit")
    safety_ticks = []
    tick = run.account_safety.tick
    run.account_safety.tick = lambda: safety_ticks.append(1) or tick()
    # The bid is at the target: an owned tick would cancel the stop-limit and close.
    run.observations[("CRYPTO", "BTC/USD")] = observation(owner.mx, **TARGET)
    owner.drop_lock()
    sent = len(mutations(venue))
    run.execution_once()
    assert run.exit_code == EXECUTOR_OWNERSHIP_LOST_EXIT_CODE == 75
    assert run.stop_event.is_set() and owner.fatal == [75]
    assert not safety_ticks and len(mutations(venue)) == sent and stop["status"] == "new"
    assert events(engine, "RUNTIME_EXECUTOR_OWNERSHIP_LOST") == [{
        "runtime_id": run.runtime_id, "code": "EXECUTOR_OWNERSHIP_LOST",
        "action": "PROCESS_EXIT", "exit_code": 75, "detected_at": venue.now.isoformat(),
    }]
    for _ in range(3):  # Terminal: later ticks do nothing and record nothing.
        run.execution_once()
    assert len(mutations(venue)) == sent and owner.fatal == [75] and not safety_ticks
    assert len(events(engine, "RUNTIME_EXECUTOR_OWNERSHIP_LOST")) == 1
    assert not run.ready() and run.status()["executor_ownership"] == "UNHELD"
    with pytest.raises(SubmissionDisabled, match="EXECUTOR_OWNERSHIP_LOST"):
        owner.lease.acquire()  # Never re-acquired in-process.
    # The supervisor's new process starts normally: lease, reconciliation, then protection.
    new = successor()
    assert new.run.reconcile_once()
    new.run.observations[("CRYPTO", "BTC/USD")] = observation(new.mx, **TARGET)
    new.run.execution_once()
    assert stop["status"] == "canceled" and new.run.exit_code is None
    assert engine._load(sid)[1]["exit_requested"] == "TARGET_EXIT"
    assert verify_events(engine.repo.export_events())["valid"]


def test_lease_lost_inside_a_tick_sends_nothing_and_ends_the_runtime(process):
    owner, venue, _ = process
    engine, run = owner.engine, owner.run
    sid = open_position(owner.mx, "BTC/USD")
    [stop] = venue.orders_of("sell", "stop_limit")
    tick = run.account_safety.tick

    def safety_tick():
        result = tick()
        owner.drop_lock()  # Lost after the tick's own ownership check passed.
        return result

    run.account_safety.tick = safety_tick
    run.observations[("CRYPTO", "BTC/USD")] = observation(owner.mx, **TARGET)
    sent = len(mutations(venue))
    run.execution_once()
    # The controller tried to cancel the stop-limit; the lease-fenced gate refused the send.
    assert len(mutations(venue)) == sent and stop["status"] == "new"
    [refused] = events(engine, "AUTHORIZATION_NOT_CLAIMED", sid)
    assert refused["action"] == "CANCEL" and refused["code"] == "EXECUTOR_OWNERSHIP_LOST"
    assert run.exit_code == 75 and run.stop_event.is_set() and owner.fatal == [75]
    assert len(events(engine, "RUNTIME_EXECUTOR_OWNERSHIP_LOST")) == 1
    run.execution_once()
    assert len(mutations(venue)) == sent and owner.fatal == [75]


def test_lost_lease_stops_every_worker_loop(process):
    owner, venue, _ = process
    run = owner.run
    open_position(owner.mx, "BTC/USD")

    def no_network(*args, **kwargs):
        raise ConnectionError("FIXTURE_STREAMS_ARE_OFFLINE")

    run.connector = no_network
    run.start()
    try:
        # Eight loops since fees-net-r added fee import; every one must stop on a lost lease.
        assert len(run.threads) == 8 and all(thread.is_alive() for thread in run.threads)
        # A healthy runtime reports its workers alive (status() once expected seven loops).
        assert run.expected_workers == 8 and run.status()["workers_alive"] is True
        assert not run.stop_event.wait(1.5)  # Running normally, still owned.
        owner.drop_lock()
        assert run.stop_event.wait(10)
        for thread in run.threads:
            thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in run.threads)
        assert run.status()["workers_alive"] is False
        assert run.exit_code == 75 and owner.fatal == [75]
    finally:
        run.stop()
    assert len(events(owner.engine, "RUNTIME_EXECUTOR_OWNERSHIP_LOST")) == 1


class StubRuntime:
    """An entry point's runtime whose protection loop finds its lease gone."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.exit_code = None
        self.on_fatal = None
        self.owned_resources = ()
        self.stopped = 0

    def lose_lease(self):
        self.exit_code = EXECUTOR_OWNERSHIP_LOST_EXIT_CODE
        self.stop_event.set()
        self.on_fatal(self.exit_code)

    def start(self):
        self.lose_lease()

    def stop(self):
        self.stopped += 1


@pytest.fixture
def hard_exit(monkeypatch):
    """The hard-deadline exit, recorded instead of ending the test process."""
    calls = []
    monkeypatch.setattr(managed_runtime, "_hard_exit", calls.append)
    monkeypatch.setattr(managed_runtime, "FATAL_EXIT_GRACE_SECONDS", 0.05)
    return calls


def test_worker_process_exits_non_zero_after_a_lost_lease(monkeypatch, hard_exit):
    stub = StubRuntime()
    monkeypatch.setattr(managed_runtime, "build_runtime_from_env", lambda: stub)
    monkeypatch.setattr(managed_runtime.signal, "signal", lambda *args: None)
    with pytest.raises(SystemExit) as exited:
        managed_runtime.main()
    assert exited.value.code == 75 and stub.stopped == 1
    stub.fatal_exit_timer.join(2)
    assert hard_exit == [75]  # A hung shutdown still ends with the same status.


def test_app_process_stops_its_server_and_exits_non_zero_after_a_lost_lease(
    monkeypatch, hard_exit
):
    stub = StubRuntime()
    application = SimpleNamespace(state=SimpleNamespace(managed_runtime=stub))
    monkeypatch.setattr(managed_app, "build_app_from_env",
                        lambda: (application, SimpleNamespace(port=8799)))
    configs, servers = [], []

    class Server:
        def __init__(self, config):
            self.should_exit, self.started = False, False
            servers.append(self)

        def run(self):
            self.started = True
            stub.lose_lease()  # While serving, the protection loop finds the lease gone.
            assert self.should_exit  # The fatal hook started uvicorn's graceful shutdown.

    monkeypatch.setattr("uvicorn.Config", lambda app, **kwargs: configs.append((app, kwargs)))
    monkeypatch.setattr("uvicorn.Server", Server)
    with pytest.raises(SystemExit) as exited:
        managed_app.main()
    assert exited.value.code == 75 and len(servers) == 1
    assert configs == [(application, {"host": "127.0.0.1", "port": 8799, "access_log": False,
                                      "log_level": "warning"})]
    stub.fatal_exit_timer.join(2)
    assert hard_exit == [75]


def test_app_that_never_started_still_exits_with_uvicorns_startup_failure(monkeypatch):
    application = SimpleNamespace(state=SimpleNamespace(managed_runtime=StubRuntime()))
    monkeypatch.setattr(managed_app, "build_app_from_env",
                        lambda: (application, SimpleNamespace(port=8799)))
    monkeypatch.setattr("uvicorn.Config", lambda app, **kwargs: None)
    monkeypatch.setattr("uvicorn.Server", lambda config: SimpleNamespace(
        run=lambda: None, started=False, should_exit=False))
    with pytest.raises(SystemExit) as exited:
        managed_app.main()
    assert exited.value.code == 3


# --- 2. Refused closes: backoff, fresh ID per retry, alarm at five ---------------------------


def test_backoff_doubles_from_one_second_to_a_sixty_second_cap():
    assert (EXIT_RETRY_BASE_SECONDS, EXIT_RETRY_CAP_SECONDS, EXIT_REFUSAL_ALARM_THRESHOLD) == (
        1, 60, 5)
    assert [exit_retry_delay(n) for n in range(1, 10)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert exit_retry_delay(10**6) == 60
    for invalid in (0, -1, 1.0, True, None):
        with pytest.raises(ValueError, match="EXIT_REFUSAL_COUNT_REQUIRED"):
            exit_retry_delay(invalid)


def status_runtime(mx):
    engine, venue, _ = mx
    return ManagedRuntime(
        engine, SimpleNamespace(repo=engine.repo),
        SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")), CREDENTIALS,
        engineering_runtime_policy(), clock=lambda: venue.now, reviewer_heartbeat=lambda: True,
    )


def close_decisions(engine, sid):
    """Every authorized close of the setup with its claims and broker outcome, in order."""
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT d.decision_id,d.payload,d.context,d.created_at,d.expires_at,
              (SELECT count(*) FROM lab.managed_claims c WHERE c.decision_id=d.decision_id)
                AS claims,
              EXISTS(SELECT 1 FROM lab.managed_events x WHERE x.kind='BROKER_REJECTED'
                AND x.body->>'decision_id'=d.decision_id::text) AS refused,
              EXISTS(SELECT 1 FROM lab.managed_events a
                WHERE a.idempotency_key='ack:'||d.decision_id::text) AS accepted
            FROM lab.managed_risk_decisions d WHERE d.setup_id=%s AND d.action='EXIT'
            AND d.outcome='APPROVED' ORDER BY d.event_seq""",
            (sid,),
        ).fetchall()


def drive_closes(mx, sid, seen, run, *, seconds):
    """One controller tick a second; the fake clock time of every close sent and whether the
    runtime status reported the repeated-refusal alarm after that tick."""
    engine, venue, _ = mx
    sent, alarmed = [], []
    for _ in range(seconds):
        before = len(venue.calls)
        engine.manage(sid, observation(mx, **seen))
        sent += [venue.now for method, _, content in venue.calls[before:]
                 if method == "POST" and json.loads(content)["type"] == "market"]
        alarmed.append((len(events(engine, "EXIT_REFUSED", sid)),
                        run.status()["exit_refusal_alarms"]))
        if any(o["status"] == "new" for o in venue.orders_of("sell", "market")):
            break  # The venue accepted a close.
        venue.now += timedelta(seconds=1)
    return sent, alarmed


def assert_retries(engine, sid, sent, refusals):
    """The shared contract: waits 1, 2, 4 … s; a fresh ID, one new revision and exactly one
    one-use five-second authorization per attempt; the last attempt accepted."""
    gaps = [(later - earlier).total_seconds()
            for earlier, later in zip(sent, sent[1:], strict=False)]
    assert gaps == [exit_retry_delay(n) for n in range(1, refusals + 1)]
    closes = close_decisions(engine, sid)
    assert len(closes) == refusals + 1
    assert [c["refused"] for c in closes] == [True] * refusals + [False]
    assert closes[-1]["accepted"]
    assert len({c["payload"]["client_order_id"] for c in closes}) == refusals + 1
    assert all(c["claims"] == 1 for c in closes)
    assert all(0 < (c["expires_at"] - c["created_at"]).total_seconds() <= 5 for c in closes)
    revisions = [c["context"]["state_revision"] for c in closes]
    assert revisions == list(range(revisions[0], revisions[0] + refusals + 1))
    bodies = events(engine, "EXIT_REFUSED", sid)
    assert [b["refusals"] for b in bodies] == list(range(1, refusals + 1))
    assert [b["retry_delay_seconds"] for b in bodies] == gaps
    assert [b["decision_id"] for b in bodies] == [str(c["decision_id"]) for c in closes[:-1]]
    return closes


def fill_close(mx, sid, price):
    engine, venue, _ = mx
    [close] = [o for o in venue.orders_of("sell", "market") if o["status"] == "new"]
    engine.ingest(venue.fill(close["id"], close["qty"], price=price))
    engine.manage(sid, observation(mx))
    return engine._load(sid)[1]


def test_refused_crypto_target_exit_backs_off_alarms_at_five_and_completes(mx):
    engine, venue, _ = mx
    sid = open_position(mx, "BTC/USD")
    [stop] = venue.orders_of("sell", "stop_limit")
    run = status_runtime(mx)
    venue.reject_market_sells = EXIT_REFUSAL_ALARM_THRESHOLD + 1
    engine.manage(sid, observation(mx, **TARGET))  # TARGET_EXIT: the stop-limit goes first.
    assert stop["status"] == "canceled" and not venue.orders_of("sell", "market")
    assert engine._load(sid)[1]["exit_requested"] == "TARGET_EXIT"
    sent, alarmed = drive_closes(mx, sid, TARGET, run, seconds=90)
    closes = assert_retries(engine, sid, sent, EXIT_REFUSAL_ALARM_THRESHOLD + 1)
    # The durable alarm is written once, at the fifth consecutive refusal.
    [alarm] = events(engine, "EXIT_REFUSAL_ALARM", sid)
    assert alarm["refusals"] == 5 and alarm["threshold"] == 5
    assert alarm["alarm"] == "EXIT_REFUSED_REPEATEDLY" and alarm["exit_requested"] == "TARGET_EXIT"
    assert alarm["decision_id"] == str(closes[4]["decision_id"])
    # Status and watchdog: on from the fifth refusal until the venue accepts a close.
    for refusals, rows in alarmed[:-1]:
        assert bool(rows) == (refusals >= 5)
    _, [row] = next(item for item in alarmed if item[1])
    assert (row["setup_id"], row["market"], row["exit_requested"]) == (
        str(sid), "CRYPTO", "TARGET_EXIT")
    assert exit_refusal_alarms([row]) == ["EXIT_REFUSED_REPEATEDLY",
                                          "EXIT_REFUSED_REPEATEDLY_CRYPTO"]
    assert alarmed[-1][1] == [] and run.status()["exit_refusal_alarms"] == []
    assert engine._load(sid)[1]["exit_refusals"] == 0  # The accepted close ended the streak.
    state = fill_close(mx, sid, "111")
    assert (state["state"], state["reason"]) == ("CLOSED", "TARGET_EXIT")
    assert verify_events(engine.repo.export_events())["valid"]


def test_refused_crypto_stop_limit_fallback_backs_off_alarms_at_five_and_completes(mx):
    """The dangerous case: the stop-limit is already cancelled when the market sell is
    refused, so only the controller's retries still close the position."""
    engine, venue, _ = mx
    sid = open_position(mx, "BTC/USD")
    [stop] = venue.orders_of("sell", "stop_limit")
    run = status_runtime(mx)
    venue.reject_market_sells = EXIT_REFUSAL_ALARM_THRESHOLD
    engine.manage(sid, observation(mx, **GAP))  # The bid gaps through the stop.
    venue.now += timedelta(seconds=2)
    engine.manage(sid, observation(mx, **GAP))  # The stop-limit did not fill: cancel it.
    assert stop["status"] == "canceled"
    assert engine._load(sid)[1]["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    sent, alarmed = drive_closes(mx, sid, GAP, run, seconds=60)
    assert_retries(engine, sid, sent, EXIT_REFUSAL_ALARM_THRESHOLD)
    [alarm] = events(engine, "EXIT_REFUSAL_ALARM", sid)
    assert alarm["refusals"] == 5 and alarm["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    for refusals, rows in alarmed[:-1]:
        assert bool(rows) == (refusals >= 5)
    assert alarmed[-1][1] == []
    state = fill_close(mx, sid, "90")
    assert (state["state"], state["reason"]) == ("CLOSED", "STOP_LIMIT_NOT_FILLED")


def test_refused_stock_time_exit_backs_off_alarms_at_five_and_completes(mx):
    engine, venue, _ = mx
    sid = open_position(mx, "SPY")
    run = status_runtime(mx)
    venue.now = datetime.fromisoformat(engine._load(sid)[1]["hard_exit_at"])
    venue.reject_market_sells = EXIT_REFUSAL_ALARM_THRESHOLD
    legs = [o for o in venue.orders_of("sell") if o["status"] in {"new", "held"}]
    engine.manage(sid, observation(mx))  # TIME_EXIT: both bracket legs are cancelled first.
    assert engine._load(sid)[1]["exit_requested"] == "TIME_EXIT"
    assert len(legs) == 2 and all(leg["status"] == "canceled" for leg in legs)
    revisions_before = len(events(engine, "STATE", sid))
    sent, alarmed = drive_closes(mx, sid, {}, run, seconds=60)
    assert_retries(engine, sid, sent, EXIT_REFUSAL_ALARM_THRESHOLD)
    # One STATE revision per refusal and one for the accepted close ending the streak: none
    # per tick while waiting (a new market sell used to go out every tick).
    assert len(events(engine, "STATE", sid)) - revisions_before == (
        EXIT_REFUSAL_ALARM_THRESHOLD + 1)
    [alarm] = events(engine, "EXIT_REFUSAL_ALARM", sid)
    assert alarm["refusals"] == 5 and alarm["exit_requested"] == "TIME_EXIT"
    for refusals, rows in alarmed[:-1]:
        assert bool(rows) == (refusals >= 5)
    _, [row] = next(item for item in alarmed if item[1])
    assert row["market"] == "US_STOCKS" and alarmed[-1][1] == []
    assert exit_refusal_alarms([row]) == ["EXIT_REFUSED_REPEATEDLY",
                                          "EXIT_REFUSED_REPEATEDLY_US_STOCKS"]
    state = fill_close(mx, sid, "100")
    assert (state["state"], state["reason"]) == ("CLOSED", "TIME_EXIT")


def test_a_spent_close_id_gets_one_new_revision_never_a_resend(mx):
    """A close accepted and then cancelled by the broker left its ID spent: the next close
    waits for one new revision instead of a reuse (a stock close used to get one per tick)."""
    engine, venue, _ = mx
    sid = open_position(mx, "SPY")
    venue.now = datetime.fromisoformat(engine._load(sid)[1]["hard_exit_at"])
    engine.manage(sid, observation(mx))  # Legs cancelled.
    engine.manage(sid, observation(mx))  # The close is sent and accepted.
    [first] = venue.orders_of("sell", "market")
    first["status"] = "canceled"  # Broker-side cancellation (fixture), nothing filled.
    assert engine.manage(sid, observation(mx)) == "EXIT_RETRY_PENDING"
    assert len(venue.orders_of("sell", "market")) == 1
    engine.manage(sid, observation(mx))
    [_, second] = venue.orders_of("sell", "market")
    assert second["client_order_id"] != first["client_order_id"]
    closes = close_decisions(engine, sid)
    assert [c["claims"] for c in closes] == [1, 1] and not any(c["refused"] for c in closes)
    assert engine._load(sid)[1]["exit_retry_of"] == str(closes[0]["decision_id"])
    assert not events(engine, "EXIT_REFUSED", sid)


# --- 3. Halts in status and the watchdog; the Muse job alarm ---------------------------------


def status_client(mx):
    run = status_runtime(mx)
    settings = AppSettings(8799, TOKEN, 300, (), CLASSIFICATION_POLICY, ())
    client = TestClient(create_application(run, settings))  # No lifespan: nothing starts.
    return run, client


def http_status(client):
    response = client.get("/api/v1/lab/status", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    return response.json()


def test_halts_refusing_entries_are_in_status_and_raise_the_watchdog_alarm(mx):
    engine, venue, _ = mx
    run, client = status_client(mx)
    now = venue.now
    status = http_status(client)
    assert status["execution_halts"] == {
        "available": True, "count": 0, "kinds": [], "oldest_halt_seq": None}
    assert status["exit_refusal_alarms"] == []
    assert not {"EXECUTION_HALT_ACTIVE", "EXIT_REFUSED_REPEATEDLY"} & set(
        status_alarms(status, now, WATCHDOG))
    sid = open_position(mx, "BTC/USD")
    venue.inventory["UNKNOWN/USD"] = D(1)  # Broker inventory no ledger owns (fixture).
    assert engine.reconcile()["clean"] is False  # Latches an execution halt.
    del venue.inventory["UNKNOWN/USD"]
    venue.equity = "9700"  # A 3% loss on the day.
    engine.manage(sid, observation(mx))  # The daily risk halt.
    status = http_status(client)
    halts = status["execution_halts"]
    assert halts["available"] is True and halts["count"] == 2
    assert halts["kinds"] == ["DAILY_RISK_HALT", "MANAGED_UNEXPLAINED_BROKER_POSITION"]
    assert isinstance(halts["oldest_halt_seq"], int)
    alarms = status_alarms(status, now, WATCHDOG)
    assert {"EXECUTION_HALT_ACTIVE", "HALT_DAILY_RISK_HALT",
            "HALT_MANAGED_UNEXPLAINED_BROKER_POSITION"} <= set(alarms)
    assert run.status()["execution_halts"] == halts  # The HTTP route passes it unchanged.


def test_halt_status_fails_closed_and_codes_stay_bounded(mx, monkeypatch):
    engine, _, _ = mx
    run = status_runtime(mx)

    def unavailable():
        raise OSError("FIXTURE_LEDGER_UNAVAILABLE")

    monkeypatch.setattr(engine.repo, "connect", unavailable)
    assert run._execution_halt_status() == {"available": False, "count": None, "kinds": []}
    assert execution_halt_alarms(run._execution_halt_status()) == [
        "EXECUTION_HALT_STATUS_UNAVAILABLE"]
    assert execution_halt_alarms(None) == []  # An older app without the field.
    for malformed in ("halted", {"available": True}, {"available": True, "kinds": "X"}):
        assert execution_halt_alarms(malformed) == ["EXECUTION_HALT_STATUS_UNAVAILABLE"]
    assert execution_halt_alarms({"available": True, "kinds": ["OPERATOR_PAUSE", "X" * 64,
                                                              "lower_case", 7]}) == [
        "EXECUTION_HALT_ACTIVE", "HALT_OPERATOR_PAUSE", "HALT_KIND_UNRECOGNIZED",
        "HALT_KIND_UNRECOGNIZED", "HALT_KIND_UNRECOGNIZED"]
    assert exit_refusal_alarms(None) == [] and exit_refusal_alarms([]) == []
    assert exit_refusal_alarms("broken") == ["EXIT_REFUSED_REPEATEDLY"]


def spool(tmp_path, now):
    """A real Muse spool (``MuseSpool``'s own schema) with a fresh heartbeat."""
    path = tmp_path / "muse" / "spool.sqlite3"
    MuseSpool(path).db.close()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO runs(id,kind,started_at,outcome) VALUES(?,?,?,?)",
                   (str(uuid4()), "HEARTBEAT", now.isoformat(), "RUNNING"))
    return path


def job(path, job_id, started, *, completed=None):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO provider_jobs(id,request,result,started_at,completed_at) "
                   "VALUES(?,?,?,?,?)", (job_id, "{}", "{}" if completed else None,
                                         started.isoformat(),
                                         completed.isoformat() if completed else None))


def run_row(path, kind, outcome, at):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO runs(id,kind,started_at,outcome) VALUES(?,?,?,?)",
                   (str(uuid4()), kind, at.isoformat(), outcome))


def test_only_a_current_stuck_provider_job_raises_muse_work_failed(tmp_path):
    now = datetime.now(UTC)
    path = spool(tmp_path, now)
    ago = lambda **delta: now - timedelta(**delta)  # noqa: E731
    # Three failed jobs, each left without a result forever, none of them current:
    job(path, "report-job:CRYPTO:1", ago(hours=2))  # Its lane logged the failure, moved on.
    run_row(path, "research", "SANITIZED_FAILURE", ago(hours=2) + timedelta(seconds=30))
    run_row(path, "RESEARCH", "INTERRUPTED_NO_REROLL", ago(hours=2) + timedelta(seconds=35))
    job(path, "evidence-job:task-1", ago(hours=1))  # Marked failed by the worker.
    worker_spool = MuseSpool(path)
    worker_spool.fail_job("evidence-job:task-1", "TERMINAL_FAILURE")
    worker_spool.db.close()
    job(path, "news-job:a:b:1:1", ago(minutes=50))  # A later news job superseded it.
    job(path, "news-job:a:b:1:2", ago(minutes=40), completed=ago(minutes=39))
    run_row(path, "RESEARCH", "NO_OP", ago(minutes=5))  # The latest work succeeded.
    assert muse_spool_status(path, now, 600) == ([], {"stuck": 0, "failed": 3})
    job(path, "report-job:CRYPTO:2", ago(minutes=2))  # Current and running: no alarm.
    assert muse_spool_status(path, now, 600) == ([], {"stuck": 0, "failed": 3})
    later = now + timedelta(minutes=9)  # The same job is now 11 minutes without a result.
    run_row(path, "HEARTBEAT", "RUNNING", later)
    assert muse_spool_status(path, later, 600) == (
        ["MUSE_WORK_FAILED"], {"stuck": 1, "failed": 3})
    assert muse_heartbeat_alarm(path, later, 600) == ["MUSE_WORK_FAILED"]
    run_row(path, "research", "SANITIZED_FAILURE", later)  # Its lane gave up on it.
    run_row(path, "RESEARCH", "PREPARED", later + timedelta(seconds=1))
    assert muse_spool_status(path, later + timedelta(seconds=5), 600) == (
        [], {"stuck": 0, "failed": 4})


def test_an_unreadable_spool_keeps_its_fail_closed_alarm(tmp_path):
    now = datetime.now(UTC)
    path = spool(tmp_path, now)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE state")
    assert muse_spool_status(path, now, 600) == (["MUSE_HEARTBEAT_STALE_OR_UNAVAILABLE"], None)
