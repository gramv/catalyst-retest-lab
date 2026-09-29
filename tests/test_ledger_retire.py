"""Retiring a Mac ledger after the move to the cloud (package cloud). Temp directories only.

The executor checks of ``ledger-retire`` (package cloud-hardening) run against a disposable
cluster started in a temp directory for this module; the "ledger" is a separate temp directory
holding the marker, whose socket directory is that cluster's. Never an owner ledger.
"""

import hashlib
import importlib.util
import json
import socket
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from catalyst_lab import cli, executor_lease, localdb
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.managed_ops import load_private_config, preflight
from catalyst_lab.managed_runtime import ManagedRuntime
from tests.test_managed_ops import launcher, write_config
from tests.test_managed_ops import ops as ops  # noqa: F401  (fixture)

NOW = datetime(2026, 9, 27, 21, 30, tzinfo=UTC)


def marker(root, document=None):
    root.mkdir(parents=True, exist_ok=True)
    path = root / localdb.LEDGER_MARKER
    path.write_text(json.dumps(document or {"role": "ACCOUNT_LEDGER", "note": "owner text"}))
    path.chmod(0o600)
    return path


def test_retire_keeps_the_old_marker_adds_two_keys_and_writes_atomically(tmp_path):
    root = tmp_path / "ledger"
    path = marker(root)
    result = localdb.retire_ledger(root, "MOVED_TO_CLOUD", now=NOW)
    document = json.loads(path.read_text())
    assert document == {"role": "ACCOUNT_LEDGER", "note": "owner text",
                        "retired_at": NOW.isoformat(), "retired_reason": "MOVED_TO_CLOUD"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not path.is_symlink()
    assert sorted(p.name for p in root.iterdir()) == [localdb.LEDGER_MARKER]  # No temp left.
    assert result["kept_keys"] == ["note", "role"] and result["broker_requests"] == 0
    assert result["database_changes"] == 0
    assert localdb.ledger_retirement(root) == {"retired_at": NOW.isoformat(),
                                               "retired_reason": "MOVED_TO_CLOUD"}
    # The role is unchanged: the ledger stays protected against dev-init and readable.
    assert localdb.ledger_role(root) == "ACCOUNT_LEDGER"
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_PROTECTED"):
        localdb.start(root)
    assert not (root / "postgres").exists()
    # A second retirement never rewrites the first timestamp.
    with pytest.raises(RuntimeError, match="LEDGER_ALREADY_RETIRED"):
        localdb.retire_ledger(root, "MOVED_TO_CLOUD")
    assert json.loads(path.read_text())["retired_at"] == NOW.isoformat()


@pytest.mark.parametrize("reason", ["", "moved to cloud", "MOVED TO CLOUD", "X", None, "A" * 65])
def test_retire_requires_a_reason_code(tmp_path, reason):
    root = tmp_path / "ledger"
    path = marker(root)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="LEDGER_RETIREMENT_REASON_INVALID"):
        localdb.retire_ledger(root, reason)
    assert path.read_bytes() == before


def test_retire_refuses_unmarked_invalid_or_symlinked_markers(tmp_path):
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_MARKER_MISSING"):
        localdb.retire_ledger(tmp_path / "absent", "MOVED_TO_CLOUD")
    bad = tmp_path / "bad"
    marker(bad, {"role": "SOMETHING_ELSE"})
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_MARKER_INVALID"):
        localdb.retire_ledger(bad, "MOVED_TO_CLOUD")
    real = marker(tmp_path / "real")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / localdb.LEDGER_MARKER).symlink_to(real)
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_MARKER_INVALID"):
        localdb.retire_ledger(linked, "MOVED_TO_CLOUD")
    assert "retired_at" not in json.loads(real.read_text())


def test_half_written_or_malformed_retirement_fails_closed(tmp_path):
    root = tmp_path / "ledger"
    marker(root, {"role": "ACCOUNT_LEDGER", "retired_reason": "MOVED_TO_CLOUD"})
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_MARKER_INVALID"):
        localdb.ledger_retirement(root)
    marker(root, {"role": "ACCOUNT_LEDGER", "retired_at": "yesterday",
                  "retired_reason": "MOVED_TO_CLOUD"})
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_MARKER_INVALID"):
        localdb.ledger_retirement(root)
    assert localdb.ledger_retirement(tmp_path / "unmarked") is None


def test_migration_of_a_retired_ledger_is_refused_before_anything_else(tmp_path):
    root = tmp_path / "ledger"
    marker(root)
    localdb.retire_ledger(root, "MOVED_TO_CLOUD", now=NOW)
    with pytest.raises(RuntimeError, match="OWNER_LEDGER_RETIRED"):
        localdb.migrate_ledger(root, expect_current=20, target=23,
                               backup_manifest=tmp_path / "missing.json")


def retired_config(ops, tmp_path):
    path, config = ops
    root = tmp_path / "account-ledger"
    marker(root)
    config["ledger"].update(data_directory=str(root / "postgres"),
                            socket_directory=str(root / "socket"),
                            marker_file=str(root / localdb.LEDGER_MARKER))
    write_config_path = path.with_name("retired.json")
    write_config(write_config_path, config)
    return write_config_path, config, root


@pytest.mark.parametrize("component", ["app", "muse"])
def test_launcher_refuses_app_and_muse_on_a_retired_ledger(ops, tmp_path, component):
    path, config, root = retired_config(ops, tmp_path)
    started = []

    def run(at):
        return launcher(config, clock=lambda: at)(
            path, component, execute=lambda *args: started.append(args) or 0)

    assert run(1000) == 0 and len(started) == 1  # Before retirement the component launches.
    if component == "app":  # It re-reads this marker once it holds the executor lease.
        assert started[0][2]["CATALYST_LEDGER_MARKER"] == config["ledger"]["marker_file"]
    localdb.retire_ledger(root, "MOVED_TO_CLOUD", now=NOW)
    with pytest.raises(ValueError, match="LEDGER_RETIRED"):
        run(1010)
    assert len(started) == 1
    log = Path(config["logs"]["directory"]) / f"{component}.log"
    assert "REFUSED" in log.read_text() and "code=LEDGER_RETIRED" in log.read_text()


def test_preflight_reports_the_retired_ledger(ops, tmp_path):
    path, config, root = retired_config(ops, tmp_path)
    before = preflight(load_private_config(path))
    assert before["ledger"]["retired"] is False
    assert "LEDGER_RETIRED" not in before["invalid_configuration_names"]
    localdb.retire_ledger(root, "MOVED_TO_CLOUD", now=NOW)
    after = preflight(load_private_config(path))
    assert after["ledger"]["retired"] is True and after["ledger"]["marker"] == "ACCOUNT_LEDGER"
    assert "LEDGER_RETIRED" in after["invalid_configuration_names"]


def test_launcher_script_names_the_retired_refusal(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_managed_private.py"
    spec = importlib.util.spec_from_file_location("run_managed_private_retired", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["run", "--config", "/fixture.json", "--component", "app"])
    monkeypatch.setattr(module, "private_standard_streams", lambda: None)

    def refused(*args):
        raise ValueError("LEDGER_RETIRED")

    monkeypatch.setattr(module, "launch_component", refused)
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert str(exited.value.code) == "PRIVATE_COMPONENT_FAILED: LEDGER_RETIRED"

    def leaky(*args):
        raise ValueError("connection to /private/path failed with secret")

    monkeypatch.setattr(module, "launch_component", leaky)
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert str(exited.value.code) == "PRIVATE_COMPONENT_FAILED"


# --- ledger-retire proves the Mac executor is stopped (package cloud-hardening) -------------

IDENTITY = hashlib.sha256(b"fixture-retire-paper-account").hexdigest()


@pytest.fixture(scope="module")
def mac_cluster():
    """A disposable running cluster: the Mac ledger's database in these tests."""
    with tempfile.TemporaryDirectory(prefix="retire-pg-", dir="/tmp") as directory:
        root = Path(directory)
        localdb.start(root)
        try:
            yield root
        finally:
            localdb.stop(root)


def running_ledger(tmp_path, cluster):
    """A marked ledger directory whose socket directory is the running cluster's."""
    root = tmp_path / "ledger"
    marker(root)
    (root / "socket").symlink_to(cluster / "socket")
    return root


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def no_app_sessions(cluster):
    """Closed sessions leave pg_stat_activity asynchronously: wait for them to go."""
    deadline = time.monotonic() + 10
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner"), autocommit=True) as conn:
        while conn.execute("""SELECT count(*) FROM pg_stat_activity
                WHERE usename::text = ANY(%s)""", (list(localdb.APP_ROLES),)).fetchone()[0]:
            assert time.monotonic() < deadline, "app sessions did not end"
            time.sleep(0.05)


def mac_lease(cluster):
    return executor_lease.AccountExecutorLease(
        RiskRepository(localdb.connection_url(cluster, "catalyst_risk")), lambda: IDENTITY)


def refused(root, code, **kwargs):
    before = (root / localdb.LEDGER_MARKER).read_bytes()
    with pytest.raises(RuntimeError, match=f"^{code}$"):
        localdb.retire_stopped_ledger(root, "MOVED_TO_CLOUD", **kwargs)
    assert (root / localdb.LEDGER_MARKER).read_bytes() == before  # Nothing was retired.


def test_the_executor_lock_is_the_one_every_executor_holds():
    assert localdb.EXECUTOR_LOCK == executor_lease.LEGACY_EXECUTOR_LOCK
    runtime = Path(executor_lease.__file__).with_name("runtime.py").read_text()
    assert f"pg_try_advisory_lock({localdb.EXECUTOR_LOCK})" in runtime  # The V1 observer.


def test_retire_refuses_while_the_ledger_cluster_is_not_running(tmp_path):
    root = tmp_path / "ledger"
    marker(root)
    (root / "socket").mkdir(mode=0o700)  # No postmaster behind it.
    refused(root, "LEDGER_CLUSTER_NOT_RUNNING", app_port=free_port())


def test_retire_refuses_while_the_mac_executor_holds_its_lease(tmp_path, mac_cluster):
    root = running_ledger(tmp_path, mac_cluster)
    lease = mac_lease(mac_cluster)
    lease.acquire()  # The Mac app, running.
    try:
        refused(root, "LEDGER_EXECUTOR_RUNNING", app_port=free_port())
    finally:
        lease.close()
    no_app_sessions(mac_cluster)


@pytest.mark.parametrize("role", localdb.APP_ROLES)
def test_retire_refuses_while_an_app_is_connected_or_starting(tmp_path, mac_cluster, role):
    root = running_ledger(tmp_path, mac_cluster)
    # An app between its launch and its lease: connected, not yet holding the lock.
    with psycopg.connect(localdb.connection_url(mac_cluster, role)):
        refused(root, "LEDGER_APP_SESSIONS_PRESENT", app_port=free_port())
    no_app_sessions(mac_cluster)


def test_retire_refuses_while_the_app_port_is_bound(tmp_path, mac_cluster):
    root = running_ledger(tmp_path, mac_cluster)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert localdb.app_port_bound(port) is True  # The real bind probe, no connection.
        refused(root, "LEDGER_APP_PORT_IN_USE", app_port=port)
    finally:
        listener.close()
    assert localdb.app_port_bound(port) is False


def test_retire_holds_the_executor_lock_while_it_writes_the_marker(tmp_path, mac_cluster,
                                                                   monkeypatch):
    root = running_ledger(tmp_path, mac_cluster)
    no_app_sessions(mac_cluster)
    real, seen = localdb.retire_ledger, {}

    def retire_while_an_executor_tries_to_start(*args, **kwargs):
        starting = mac_lease(mac_cluster)
        with pytest.raises(SubmissionDisabled, match="ACCOUNT_EXECUTOR_ALREADY_RUNNING"):
            starting.acquire()
        starting.close()
        seen["blocked"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(localdb, "retire_ledger", retire_while_an_executor_tries_to_start)
    result = localdb.retire_stopped_ledger(root, "MOVED_TO_CLOUD", app_port=free_port(),
                                           now=NOW)
    assert seen == {"blocked": True}
    assert result["retired_reason"] == "MOVED_TO_CLOUD" and result["broker_requests"] == 0
    assert result["executor_stopped"]["executor_lock"] == "FREE_THEN_HELD_WHILE_RETIRING"
    assert localdb.ledger_retirement(root)["retired_reason"] == "MOVED_TO_CLOUD"
    after = mac_lease(mac_cluster)
    after.acquire()  # Released afterwards (the launcher now refuses the app instead).
    after.close()
    no_app_sessions(mac_cluster)


def test_a_lost_lock_during_the_write_is_reported(tmp_path, mac_cluster, monkeypatch):
    root = running_ledger(tmp_path, mac_cluster)
    real = localdb.retire_ledger

    def retire_and_lose_the_session(*args, **kwargs):
        result = real(*args, **kwargs)
        with psycopg.connect(localdb.connection_url(mac_cluster, "lab_owner"),
                             autocommit=True) as admin:
            admin.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE application_name = 'catalyst-ledger-retire'""")
        return result

    monkeypatch.setattr(localdb, "retire_ledger", retire_and_lose_the_session)
    with pytest.raises(RuntimeError, match="^LEDGER_EXECUTOR_LOCK_LOST$"):
        localdb.retire_stopped_ledger(root, "MOVED_TO_CLOUD", app_port=free_port())
    assert localdb.ledger_retirement(root) is not None  # Retired; the owner checks the Mac.


def mac_runtime(cluster, marker_path):
    """The two things ManagedRuntime.acquire_ownership uses: the lease and the marker."""
    runtime = ManagedRuntime.__new__(ManagedRuntime)
    runtime.executor_lease, runtime.ledger_marker = mac_lease(cluster), marker_path
    return runtime


def test_an_app_that_takes_the_lease_after_retirement_stops_at_once(tmp_path, mac_cluster):
    """The race ledger-retire alone cannot see: an app already past the launcher's check but
    holding no session and no port yet. It takes the lease only after the marker is written,
    re-reads the marker, releases the lease and stops (LEDGER_RETIRED)."""
    root = running_ledger(tmp_path, mac_cluster)
    running = mac_runtime(mac_cluster, root / localdb.LEDGER_MARKER)
    running.acquire_ownership()  # An active ledger: the app holds the lease.
    assert running.executor_lease.connection is not None
    running.executor_lease.close()
    no_app_sessions(mac_cluster)
    localdb.retire_stopped_ledger(root, "MOVED_TO_CLOUD", app_port=free_port(), now=NOW)
    starting = mac_runtime(mac_cluster, root / localdb.LEDGER_MARKER)
    with pytest.raises(SubmissionDisabled, match="LEDGER_RETIRED"):
        starting.acquire_ownership()
    assert starting.executor_lease.connection is None  # Released at once.
    no_app_sessions(mac_cluster)
    # An unreadable marker fails closed the same way.
    broken = tmp_path / "broken"
    marker(broken).write_text("{not json")
    with pytest.raises(SubmissionDisabled, match="LEDGER_RETIRED"):
        mac_runtime(mac_cluster, broken / localdb.LEDGER_MARKER).acquire_ownership()
    no_app_sessions(mac_cluster)
    # No marker configured (the Railway trader): nothing is read, the lease is simply held.
    cloud = mac_runtime(mac_cluster, None)
    cloud.acquire_ownership()
    assert cloud.executor_lease.connection is not None
    cloud.executor_lease.close()
    no_app_sessions(mac_cluster)


def test_cli_ledger_retire_needs_an_explicit_directory_and_reason(tmp_path, monkeypatch, capsys,
                                                                  mac_cluster):
    root = running_ledger(tmp_path, mac_cluster)
    no_app_sessions(mac_cluster)
    monkeypatch.setattr(sys, "argv", ["catalyst-lab", "ledger-retire", "--reason",
                                      "MOVED_TO_CLOUD"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "requires --ledger-dir and --reason" in capsys.readouterr().err
    argv = ["catalyst-lab", "ledger-retire", "--ledger-dir", str(root), "--reason",
            "MOVED_TO_CLOUD", "--app-port", str(free_port())]
    lease = mac_lease(mac_cluster)
    lease.acquire()
    monkeypatch.setattr(sys, "argv", argv)
    try:
        with pytest.raises(SystemExit, match="ledger-retire refused: LEDGER_EXECUTOR_RUNNING"):
            cli.main()
    finally:
        lease.close()
    no_app_sessions(mac_cluster)
    cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["retired_reason"] == "MOVED_TO_CLOUD" and printed["role"] == "ACCOUNT_LEDGER"
    assert printed["executor_stopped"]["app_port_bound"] is False
    with pytest.raises(SystemExit, match="ledger-retire refused: LEDGER_ALREADY_RETIRED"):
        cli.main()
