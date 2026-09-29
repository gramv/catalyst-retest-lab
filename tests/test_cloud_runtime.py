"""Railway trader and ops entrypoints (package cloud).

Disposable PostgreSQL only (the suite's cluster for the executor lease, a Railway-shaped cluster
for ops), Unix-domain sockets instead of TCP, mock HTTP for the status and the notifier.
"""

import hashlib
import json
import os
import socket
import stat
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from catalyst_lab import cloud_provision, cloud_runtime, localdb
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import system_event
from catalyst_lab.executor_lease import AccountExecutorLease
from catalyst_lab.ledger_ops import LedgerOpsError
from catalyst_lab.managed_ops import verify_checkpoint
from catalyst_lab.repository import Repository
from tests import cloud_fixtures
from tests.test_cloud_config import SECRET_VALUES, ops_env, trader_env

IDENTITY = hashlib.sha256(b"fixture-cloud-paper-account").hexdigest()
RELEASE = {"release_commit": "c" * 40, "source_sha256": "d" * 64}
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)


def release():
    return dict(RELEASE)


@pytest.fixture
def short_dir():
    # Unix-domain socket paths must stay short (macOS: 104 bytes).
    path = Path(tempfile.mkdtemp(prefix="cloud-rt-", dir="/tmp"))
    yield path
    for current, directories, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(current, name))
        for name in directories:
            os.rmdir(os.path.join(current, name))
    os.rmdir(path)


def unix_listener(path):
    def listen(host, port):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.listen(16)
        listen.calls.append((host, port))
        listen.sock = sock
        return sock

    listen.calls = []
    return listen


def get(path, route):
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), timeout=5) as client:
        return client.get("http://trader" + route)


class Closer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def fake_app(runtime):
    return SimpleNamespace(state=SimpleNamespace(managed_runtime=runtime, research_context=None))


def lease(cluster):
    return AccountExecutorLease(
        RiskRepository(localdb.connection_url(cluster, "catalyst_risk")), lambda: IDENTITY)


def test_a_second_trader_waits_for_the_lease_answering_health_then_takes_the_socket(
        cluster, short_dir, monkeypatch):
    monkeypatch.chdir(short_dir)
    old = lease(cluster)
    old.acquire()  # The trader of the previous deployment still holds the account.
    new = lease(cluster)
    runtime = SimpleNamespace(acquire_ownership=new.acquire, owned_resources=(),
                              source=Closer(), execution=SimpleNamespace(broker=Closer()))
    served, result = [], {}
    socket_path = short_dir / "trader.sock"
    listen = unix_listener(socket_path)

    def serve(app, settings, *, host, sockets):
        served.append((app, host, sockets))

    env = trader_env(short_dir / "volume")
    thread = threading.Thread(target=lambda: result.update(code=cloud_runtime.trader(
        env, listen=listen, builder=lambda: (fake_app(runtime), SimpleNamespace(port=8780)),
        wait_database=lambda dsn, max_seconds: {"database": "READY"},
        check_ledger=lambda dsn: {"ledger_id": "fixture-ledger"}, serve=serve,
        verify_release=release, sleep=lambda s: time.sleep(0.02))), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        health = get(socket_path, "/health")
        assert health.status_code == 200 and health.json() == cloud_runtime.HEALTH_STARTING
        assert "trading_ready" not in health.text and "entry_ready" not in health.text
        status = get(socket_path, "/api/v1/lab/status")
        assert status.status_code == 503 and status.json() == {"detail": "TRADER_STARTING"}
        time.sleep(0.3)
        assert not served and new.connection is None  # Still waiting: never two executors.
        old.close()  # The previous deployment stops; its lease is released.
        thread.join(timeout=10)
        assert result == {"code": 0}
        assert len(served) == 1 and served[0][2] == [listen.sock] and served[0][1] == "127.0.0.1"
        assert listen.sock.fileno() != -1  # The same listening socket, handed over open.
        assert new.connection is not None and not new.lost
        assert listen.calls == [("127.0.0.1", 8780)]
    finally:
        old.close()
        new.close()
        thread.join(timeout=5)


def test_the_crash_loop_breaker_state_lives_on_the_volume_and_stops_the_fifth_start(
        short_dir, monkeypatch, capsys):
    monkeypatch.chdir(short_dir)
    volume = short_dir / "volume"
    env = trader_env(volume, APCA_API_SECRET_KEY=None)  # Refused on every start.
    codes = [cloud_runtime.trader(env, clock=lambda at=at: at, verify_release=release)
             for at in (1000, 1010, 1020, 1030, 1040)]
    assert codes == [2, 2, 2, 2, 0]  # Refusals restart (exit 2); the breaker stops (exit 0).
    state = volume / "launcher-state"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    for name in ("trader.starts.json", "crash-loop-trader.json"):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
    printed = capsys.readouterr().out
    assert "REFUSED code=CLOUD_SECRET_MISSING APCA_API_SECRET_KEY" in printed
    assert "CRASH_LOOP_BREAKER_STOP alarms=CRASH_LOOP_BREAKER_TRIPPED,CRASH_LOOP_TRADER" in printed
    # A new container (a restart) reads the same file: still stopped inside the window ...
    assert cloud_runtime.trader(env, clock=lambda: 1100, verify_release=release) == 0
    # ... and runs again after ten quiet minutes.
    assert cloud_runtime.trader(env, clock=lambda: 1100 + 601, verify_release=release) == 2
    assert not (state / "crash-loop-trader.json").exists()
    for value in SECRET_VALUES.values():
        assert value not in printed


def test_without_a_state_directory_the_breaker_fails_closed(short_dir, monkeypatch):
    monkeypatch.chdir(short_dir)
    env = trader_env(short_dir, CATALYST_STATE_DIR=None)
    assert cloud_runtime.trader(env, verify_release=release) == 0  # Cannot count: stays down.


def test_startup_failures_retry_for_ten_minutes_then_exit_one(short_dir, monkeypatch, capsys):
    monkeypatch.chdir(short_dir)
    env = trader_env(short_dir / "volume")
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    attempts = []

    def unavailable(dsn, max_seconds):
        attempts.append(max_seconds)
        raise ValueError("DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT")

    listen = unix_listener(short_dir / "t.sock")
    code = cloud_runtime.trader(env, listen=listen, wait_database=unavailable,
                                sleep=sleep, monotonic=lambda: now[0], verify_release=release,
                                builder=lambda: pytest.fail("never built"))
    assert code == 1 and len(attempts) > 5 and sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(sleeps) == 60.0 and listen.sock.fileno() == -1  # Closed on the way out.
    printed = capsys.readouterr().out
    assert "STARTUP_RETRY code=DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT" in printed
    assert "STARTUP_FAILED code=DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT" in printed


def test_a_failed_build_closes_what_it_opened_and_a_missing_ledger_identity_is_retried(
        short_dir, monkeypatch):
    monkeypatch.chdir(short_dir)
    env = trader_env(short_dir / "volume")
    closer = Closer()
    runtime = SimpleNamespace(acquire_ownership=lambda: (_ for _ in ()).throw(
        RuntimeError("BROKER_ACCOUNT_IDENTITY_REQUIRED")), owned_resources=(closer,),
        source=Closer(), execution=SimpleNamespace(broker=Closer()))
    identities = iter([cloud_provision.ProvisionError("CLOUD_LEDGER_IDENTITY_REQUIRED")])

    def check(dsn):
        error = next(identities, None)
        if error is not None:
            raise error
        return {"ledger_id": "fixture"}

    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    code = cloud_runtime.trader(
        env, listen=unix_listener(short_dir / "b.sock"),
        wait_database=lambda dsn, max_seconds: None, check_ledger=check,
        builder=lambda: (fake_app(runtime), SimpleNamespace(port=8780)),
        sleep=sleep, monotonic=lambda: now[0], verify_release=release,
        serve=lambda *a, **k: pytest.fail("never served without the lease"))
    assert code == 1 and closer.closed


def test_an_unsealed_image_is_refused(short_dir, monkeypatch, capsys):
    monkeypatch.chdir(short_dir)

    def unsealed():
        raise ValueError("RELEASE_IDENTITY_UNVERIFIED")

    env = trader_env(short_dir / "volume")
    assert cloud_runtime.trader(env, verify_release=unsealed) == 2
    assert "REFUSED code=RELEASE_IDENTITY_UNVERIFIED" in capsys.readouterr().out


def test_the_public_socket_is_dual_stack_and_loopback_stays_ipv4():
    try:
        sock = cloud_runtime.listening_socket("::", 0)
    except OSError:
        pytest.skip("IPv6 is unavailable on this host")
    try:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    finally:
        sock.close()
    loopback = cloud_runtime.listening_socket("127.0.0.1", 0)
    try:
        assert loopback.family == socket.AF_INET and loopback.getsockname()[0] == "127.0.0.1"
    finally:
        loopback.close()


# --- ops ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger():
    cluster = cloud_fixtures.start(cloud_fixtures.new_root())
    passwords = cloud_fixtures.cloud_passwords(cloud_provision.login_roles())
    try:
        cloud_provision.provision(cluster.admin_url, passwords)
        yield cluster, passwords
    finally:
        cloud_fixtures.stop(cluster)


def healthy_status(now, **overrides):
    stamp = now.isoformat()
    return {"worker_state": "RUNNING", "last_protection_tick": stamp,
            "last_reconciliation_at": stamp, "last_cycle_at": stamp,
            "trade_stream_connected": True, "research_healthy": True,
            "account_safety_healthy": True, "executor_ownership": "EXCLUSIVE",
            "required_market_streams": [], "market_streams": {}, "error_code": None,
            "code_version": RELEASE["source_sha256"], "release_commit": RELEASE["release_commit"],
            **overrides}


class StatusServer:
    """The trader's status and positions routes and the notifier's ping host, as one mock
    transport."""

    def __init__(self, status, positions=None):
        self.status, self.positions, self.requests = status, positions, []

    def handler(self, request):
        self.requests.append(request)
        if request.url.host == "127.0.0.1":
            assert request.method == "GET" and request.url.path in {
                "/api/v1/lab/status", "/api/v1/lab/positions"}
            if request.headers.get("authorization") != "Bearer " + SECRET_VALUES[
                    "MANAGED_STATUS_TOKEN"]:
                return httpx.Response(401)
            if self.status is None:
                raise httpx.ConnectError("trader down")
            if request.url.path == "/api/v1/lab/positions":
                return httpx.Response(200, json=self.positions)
            return httpx.Response(200, json=self.status)
        assert request.url.host == "hc-ping.com" and request.method == "POST"
        return httpx.Response(200)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def loop_for(ledger, volume, places, status, **extra):
    from catalyst_lab.cloud_config import ops_config

    cluster, passwords = ledger
    env = ops_env(volume,
                  AUDIT_DATABASE_URL=cluster.url("catalyst_app",
                                                 passwords["APP_DATABASE_PASSWORD"]),
                  BACKUP_DATABASE_URL=cluster.url("catalyst_backup",
                                                  passwords["BACKUP_DATABASE_PASSWORD"]),
                  **extra)
    config = ops_config(env, **places)
    server = StatusServer(status)
    return cloud_runtime.OpsLoop(config, release(), transport=server.transport), server


@pytest.fixture
def places(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "image").mkdir()
    return {"cwd": tmp_path / "work", "release_root": tmp_path / "image"}


def test_ops_writes_checkpoint_and_backup_to_the_volume_on_schedule(ledger, tmp_path, places):
    volume = tmp_path / "volume"
    loop, server = loop_for(ledger, volume, places, healthy_status(NOW))
    first = loop.tick(NOW)
    assert first["alarms"] == [] and first["scope"] == "CLOUD_OPS_WATCHDOG"
    assert first["audit"]["result"] == "WRITTEN" and first["audit"]["mode"] == "FULL"
    assert first["backup"]["result"] == "MATCH"
    backup = volume / "backups" / first["backup"]["backup"]
    assert sorted(p.name for p in backup.iterdir()) == ["catalyst_lab.dump", "manifest.json",
                                                         "roles.sql"]
    proof = verify_checkpoint(volume / "audit-checkpoints")
    assert proof["valid"] and proof["head_hash"] == first["audit"]["head_hash"]
    assert first["backup"]["audit_head"] == first["audit"]["head_hash"]
    for name in ("alarms.json", "ops-schedule.json"):
        assert stat.S_IMODE((volume / name).stat().st_mode) == 0o600
    alarm_file = json.loads((volume / "alarms.json").read_text())
    assert alarm_file["alarms"] == [] and alarm_file["release_commit"] == RELEASE["release_commit"]
    # Ten minutes later nothing is due; after an hour, an incremental checkpoint of new events.
    server.status = healthy_status(NOW + timedelta(minutes=10))
    quiet = loop.tick(NOW + timedelta(minutes=10))
    assert quiet["audit"] is None and quiet["backup"] is None
    cluster, passwords = ledger
    app = Repository(cluster.url("catalyst_app", passwords["APP_DATABASE_PASSWORD"]))
    with app.connect() as conn:
        system_event(app, conn, "LAB_FIXTURE_OPS_EVENT", {"n": 1})
    later = NOW + timedelta(minutes=61)
    server.status = healthy_status(later)
    hourly = loop.tick(later)
    assert hourly["audit"]["mode"] == "INCREMENTAL" and hourly["audit"]["result"] == "WRITTEN"
    assert hourly["backup"] is None
    assert verify_checkpoint(volume / "audit-checkpoints")["checkpoint_count"] == 2
    next_day = loop.tick(NOW + timedelta(days=1, minutes=1))
    assert next_day["backup"]["result"] == "MATCH"
    for request in server.requests:
        assert request.url.host == "127.0.0.1"  # No notifier configured: no other traffic.


def test_ops_alarms_follow_the_mac_watchdog_rules(ledger, tmp_path, places):
    volume = tmp_path / "volume"
    loop, server = loop_for(ledger, volume, places, None)
    down = loop.tick(NOW)
    assert "STATUS_UNAVAILABLE" in down["alarms"]
    server.status = healthy_status(NOW, code_version="e" * 64, executor_ownership=None,
                                   execution_halts={"available": True,
                                                    "kinds": ["OPERATOR_PAUSE"]})
    later = NOW + timedelta(minutes=1)
    server.status["last_protection_tick"] = later.isoformat()
    server.status["last_reconciliation_at"] = later.isoformat()
    server.status["last_cycle_at"] = later.isoformat()
    alarms = loop.tick(later)["alarms"]
    assert {"RELEASE_CODE_MISMATCH", "EXECUTOR_OWNERSHIP_UNAVAILABLE", "EXECUTION_HALT_ACTIVE",
            "HALT_OPERATOR_PAUSE"} <= set(alarms)


def test_a_failed_backup_alarms_until_a_retry_succeeds(ledger, tmp_path, places):
    volume = tmp_path / "volume"
    loop, server = loop_for(ledger, volume, places, healthy_status(NOW))
    real = loop.backup
    calls = []

    def failing(url, destination, now=None):
        calls.append(now)
        raise LedgerOpsError("BACKUP_DUMP_FAILED", "pg_dump said something private")

    loop.backup = failing
    first = loop.tick(NOW)
    assert first["backup"] == {"result": "FAILED", "code": "BACKUP_DUMP_FAILED"}
    assert "BACKUP_FAILED" in first["alarms"] and "private" not in json.dumps(first)
    soon = NOW + timedelta(minutes=5)
    server.status = healthy_status(soon)
    again = loop.tick(soon)
    assert again["backup"] is None and "BACKUP_FAILED" in again["alarms"] and len(calls) == 1
    loop.backup = real
    retry = NOW + timedelta(minutes=31)
    server.status = healthy_status(retry)
    recovered = loop.tick(retry)
    assert recovered["backup"]["result"] == "MATCH" and "BACKUP_FAILED" not in recovered["alarms"]


def test_ops_alerts_through_the_notifier_with_the_secret_url_variable(ledger, tmp_path, places):
    section = {"provider": "HEALTHCHECKS", "allowed_hosts": ["hc-ping.com"],
               "reminder_minutes": 30, "daily_head_hour_utc": NOW.hour, "timeout_seconds": 5}
    url = "https://hc-ping.com/00000000-fixture-0000-0000-000000000000"
    volume = tmp_path / "volume"
    loop, server = loop_for(ledger, volume, places, healthy_status(NOW),
                            CLOUD_NOTIFY_JSON=json.dumps(section), NOTIFY_PING_URL=url)
    loop.tick(NOW)
    pings = [r for r in server.requests if r.url.host == "hc-ping.com"]
    assert [str(r.url) for r in pings] == [url]
    body = json.loads(pings[0].content)
    assert set(body) == {"alarms", "at", "audit_seq", "audit_head_hash", "release_commit"}
    assert body["alarms"] == [] and body["release_commit"] == RELEASE["release_commit"]
    server.status = None
    later = NOW + timedelta(minutes=1)
    loop.tick(later)
    failed = [r for r in server.requests if r.url.host == "hc-ping.com"][-1]
    assert str(failed.url) == url + "/fail"
    assert "STATUS_UNAVAILABLE" in json.loads(failed.content)["alarms"]
    stored = "".join(p.read_text() for p in volume.glob("*.json"))
    assert url not in stored and SECRET_VALUES["MANAGED_STATUS_TOKEN"] not in stored


def test_ops_main_refuses_trading_secrets_and_runs_until_stopped(ledger, tmp_path, monkeypatch,
                                                                capsys):
    monkeypatch.chdir(tmp_path)
    cluster, passwords = ledger
    env = ops_env(tmp_path / "volume",
                  AUDIT_DATABASE_URL=cluster.url("catalyst_app",
                                                 passwords["APP_DATABASE_PASSWORD"]),
                  BACKUP_DATABASE_URL=cluster.url("catalyst_backup",
                                                  passwords["BACKUP_DATABASE_PASSWORD"]))
    held = {**env, "APCA_API_SECRET_KEY": "fixture-must-not-be-on-ops-000000000000"}
    assert cloud_runtime.ops(held, verify_release=release) == 2
    assert "OPS_MUST_NOT_HOLD_TRADING_SECRETS APCA_API_SECRET_KEY" in capsys.readouterr().out
    server = StatusServer(None)
    assert cloud_runtime.ops(env, verify_release=release, transport=server.transport,
                             max_ticks=1) == 0
    printed = capsys.readouterr().out
    assert "OPS STARTING" in printed and "OPS STOPPED ticks=1" in printed
    assert json.loads((tmp_path / "volume" / "alarms.json").read_text())["audit"]["result"] == (
        "WRITTEN")


def test_the_owner_status_command_prints_the_checklist_fields_only(capsys):
    body = healthy_status(NOW, entry_ready=True, account_id="must-not-appear")
    server = StatusServer(body)
    env = {"MANAGED_STATUS_API": "http://127.0.0.1:8780",
           "MANAGED_STATUS_TOKEN": SECRET_VALUES["MANAGED_STATUS_TOKEN"]}
    assert cloud_runtime.status(env, transport=server.transport) == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)
    assert shown["executor_ownership"] == "EXCLUSIVE" and shown["entry_ready"] is True
    assert set(shown) == set(cloud_runtime.STATUS_KEYS)
    assert "must-not-appear" not in printed
    assert SECRET_VALUES["MANAGED_STATUS_TOKEN"] not in printed
    server.status = None
    assert cloud_runtime.status(env, transport=server.transport) == 1
    assert cloud_runtime.status({}, transport=server.transport) == 2


def test_the_owner_positions_command_shows_stops_and_replacements_only(capsys):
    raised = {"change_id": "fixture-change", "stop": "101.5", "path": "PATCH_REPLACE"}
    item = {"setup_id": "8d7c3f7e-0000-4000-8000-000000000001", "symbol": "ETH/USD",
            "open_qty": "0.5", "quantity_source": "BROKER_POSITION",
            "opened_at": NOW.isoformat(), "agent_id": "muse", "gross_pnl_usd": None,
            "state": {"state": "OPEN", "stop": "100", "target": "120", "arm": "JEV_MANAGED",
                      "stop_replace": raised, "maintenance_policy": "CRYPTO_MAINTENANCE_V2",
                      "qty": "0.5", "revision": 7}}
    server = StatusServer(healthy_status(NOW), positions={"cohort": "fixture",
                                                          "items": [item],
                                                          "next_cursor": None})
    env = {"MANAGED_STATUS_API": "http://127.0.0.1:8780",
           "MANAGED_STATUS_TOKEN": SECRET_VALUES["MANAGED_STATUS_TOKEN"]}
    assert cloud_runtime.positions(env, transport=server.transport) == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)["open_positions"]
    assert shown == [{"setup_id": item["setup_id"], "symbol": "ETH/USD", "open_qty": "0.5",
                      "quantity_source": "BROKER_POSITION", "opened_at": NOW.isoformat(),
                      "state": {"state": "OPEN", "stop": "100", "target": "120",
                                "arm": "JEV_MANAGED", "stop_replace": raised,
                                "maintenance_policy": "CRYPTO_MAINTENANCE_V2"}}]
    assert SECRET_VALUES["MANAGED_STATUS_TOKEN"] not in printed
    assert [(r.method, r.url.path, r.url.query) for r in server.requests] == [
        ("GET", "/api/v1/lab/positions", b"limit=50")]
    server.status = None
    assert cloud_runtime.positions(env, transport=server.transport) == 1
    assert cloud_runtime.positions({}, transport=server.transport) == 2


def test_a_port_in_use_is_a_startup_failure_not_a_traceback(short_dir, monkeypatch, capsys):
    monkeypatch.chdir(short_dir)

    def taken(host, port):
        raise OSError("address already in use")

    code = cloud_runtime.trader(trader_env(short_dir / "volume"), listen=taken,
                                verify_release=release)
    assert code == 1
    assert "REFUSED code=CLOUD_PORT_UNAVAILABLE port=8780" in capsys.readouterr().out


def test_the_trader_shell_latch_clear_takes_only_the_traders_own_login(ledger, capsys):
    """Refusals of ``cloud_runtime clear-protection-latch`` name codes only; the DSN and the
    password are never printed (the release semantics: tests/test_managed_runtime_latches.py)."""
    cluster, passwords = ledger
    reason = "Owner cleared the latch after checking the Alpaca Paper orders by hand."
    cases = [
        ({}, "CLOUD_LATCH_CLEAR_REFUSED: CLOUD_SECRET_MISSING MANAGED_DATABASE_URL"),
        ({"MANAGED_DATABASE_URL": cluster.url("catalyst_app", passwords[
            "APP_DATABASE_PASSWORD"])},
         "CLOUD_LATCH_CLEAR_REFUSED: CLOUD_DATABASE_URL_INVALID MANAGED_DATABASE_URL"),
        ({"MANAGED_DATABASE_URL": cluster.url("catalyst_risk", "x" * 40)},
         "CLOUD_LATCH_CLEAR_FAILED: OPERATIONALERROR"),
    ]
    for environ, expected in cases:
        assert cloud_runtime.clear_protection_latch(reason, environ) in (1, 2)
        assert capsys.readouterr().out.strip() == expected
    environ = {"MANAGED_DATABASE_URL": cluster.url("catalyst_risk",
                                                   passwords["RISK_DATABASE_PASSWORD"])}
    assert cloud_runtime.clear_protection_latch(reason, environ) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["kind"] == "OPERATOR_PROTECTION_LATCH_CLEARED"
    for secret in passwords.values():
        assert secret not in printed
    with pytest.raises(SystemExit):
        cloud_runtime.main(["clear-protection-latch"])  # --reason is required.
    with pytest.raises(SystemExit):
        cloud_runtime.main(["status", "--reason", reason])


def test_an_invalid_reviews_switch_refuses_the_trader_at_once(short_dir, monkeypatch, capsys):
    """package cloud-hardening: exit 2 before binding or building anything, naming the variable
    (it used to pass the profile and fail every startup for ten minutes at a time)."""
    monkeypatch.chdir(short_dir)
    env = trader_env(short_dir / "volume", MANAGED_MANAGEMENT_REVIEWS="DISABLE")
    code = cloud_runtime.trader(env, verify_release=release,
                                listen=lambda *a: pytest.fail("never bound"),
                                builder=lambda: pytest.fail("never built"))
    assert code == 2
    printed = capsys.readouterr().out
    assert "REFUSED code=CLOUD_ENGINE_SETTING_INVALID MANAGED_MANAGEMENT_REVIEWS" in printed
    assert "STARTUP_RETRY" not in printed


def test_the_trader_builds_its_app_with_a_database_free_health(short_dir, monkeypatch):
    from catalyst_lab import managed_app

    monkeypatch.chdir(short_dir)
    runtime = SimpleNamespace(acquire_ownership=lambda: None, owned_resources=(),
                              source=Closer(), execution=SimpleNamespace(broker=Closer()))
    built, served = [], []

    def build(**kwargs):
        built.append(kwargs)
        return fake_app(runtime), SimpleNamespace(port=8780)

    monkeypatch.setattr(managed_app, "build_app_from_env", build)
    code = cloud_runtime.trader(
        trader_env(short_dir / "volume"), listen=unix_listener(short_dir / "h.sock"),
        wait_database=lambda dsn, max_seconds: None, check_ledger=lambda dsn: {"ledger_id": "x"},
        serve=lambda app, settings, *, host, sockets: served.append(app), verify_release=release)
    assert code == 0 and len(served) == 1
    assert built == [{"health_check_database": False}]  # The public /health never reads the DB.
