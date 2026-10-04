"""Private fixture files, mock HTTP and disposable audit ledger; no live service."""

import copy
import importlib.util
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import managed_ops
from catalyst_lab.execution import system_event
from catalyst_lab.managed_app import AppSettings
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_ops import (
    RotatingLog,
    code_version,
    config_template,
    export_checkpoint,
    launch_component,
    launcher_directory,
    load_private_config,
    muse_heartbeat_alarm,
    package_directory,
    package_sha256,
    preflight,
    release_alarms,
    render_supervisor,
    restore_audit_copy,
    run_logged,
    status_alarms,
    verify_checkpoint,
    wait_for_database,
    watchdog_once,
)
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.muse_worker import MuseSpool
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

TOKEN = "fixture-private-muse-token-abcdefghijklmnopqrstuvwxyz"
STATUS_TOKEN = "fixture-private-status-token-abcdefghijklmnopqrstuvwxyz"
OPERATOR_TOKEN = "fixture-private-operator-token-abcdefghijklmnopqrstuvwxyz"
COMMIT = "a" * 40
ORDER = ("ledger", "app", "muse", "watchdog", "audit", "backup")


def private_file(path, text):
    path = Path(path)
    path.write_text(text)
    path.chmod(0o600)
    return path


def fake_release(root):
    """The layout scripts/build_release.py produces, with a tiny stand-in package."""
    release = root / "release"
    package = release / "src" / "catalyst_lab"
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text('"""Fixture release package."""\n')
    (package / "managed_ops.py").write_text("VALUE = 1\n")
    (package / "migrations" / "001_fixture.sql").write_text("SELECT 1;\n")
    (release / "scripts").mkdir()
    (release / "scripts" / "run_managed_private.py").write_text("# fixture launcher\n")
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "venv" / "bin" / "python").write_text("# fixture interpreter\n")
    digest = package_sha256(package)
    (release / "release.json").write_text(json.dumps({
        "commit": COMMIT, "source_sha256": digest, "built_at": "2026-09-24T00:00:00+00:00",
        "python": {"implementation": "CPython", "version": "3.12.7"},
    }))
    return release, package, digest


def package_of(config):
    return Path(config["release"]["directory"]) / "src" / "catalyst_lab"


def write_config(path, config):
    return private_file(path, json.dumps(config))


@pytest.fixture
def ops(tmp_path):
    release, _, digest = fake_release(tmp_path)
    config = config_template(tmp_path)
    config["release"] = {"directory": str(release), "source_sha256": digest}
    config["environment"]["APCA_API_SECRET_KEY"] = "fixture-secret-never-display"
    config["environment"]["MANAGED_DATABASE_URL"] = "host=/nonexistent-fixture dbname=fixture"
    path = write_config(tmp_path / "private.json", config)
    for role, token in (("muse", TOKEN), ("status", STATUS_TOKEN), ("operator", OPERATOR_TOKEN)):
        private_file(config[role]["token_file"], token)
    return path, config


def launcher(config, **overrides):
    """launch_component bound to the fixture release and an always-ready database."""
    return partial(launch_component, package_dir=package_of(config),
                   probe=lambda dsn: True, **overrides)


def v1_config(root):
    """The retired single-token layout (private config version 1)."""
    token = str(root / "muse-token")
    config = config_template(root)
    watchdog = {**config["watchdog"], "token_file": token}
    return {"version": 1, "environment": {**config["environment"], "MANAGED_API_TOKEN": TOKEN},
            "muse": {**config["muse"], "token_file": token}, "watchdog": watchdog,
            "audit": config["audit"]}


def test_private_config_refuses_permissions_symlinks_and_environment_injection(ops, tmp_path):
    path, config = ops
    assert load_private_config(path) == config
    path.chmod(0o644)
    with pytest.raises(ValueError, match="INVALID_PRIVATE_LAUNCH_CONFIGURATION"):
        load_private_config(path)
    path.chmod(0o600)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_private_config(link)
    config["environment"]["PYTHONPATH"] = "/untrusted"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_private_config(path)


def test_private_config_rejects_shell_and_remote_token_destination(ops):
    path, original = ops
    for mutate in (lambda c: c["muse"].update(command=["sh", "-c", "echo secret"]),
                   lambda c: c["watchdog"].update(api="https://example.org")):
        config = copy.deepcopy(original)
        mutate(config)
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError):
            load_private_config(path)


def test_v2_config_requires_exact_sections_separate_token_files_and_bounded_values(ops):
    path, original = ops
    assert load_private_config(path)["config_version"] == 2
    for mutate in (
        lambda c: c.pop("notify"),
        lambda c: c["notify"].update(url="https://example.org"),
        lambda c: c.update(version=1),
        lambda c: c.update(config_version=3),
        lambda c: c["status"].update(token_file=c["muse"]["token_file"]),
        lambda c: c["operator"].update(token_file=c["status"]["token_file"]),
        lambda c: c["environment"].update(MANAGED_API_TOKEN=TOKEN),
        lambda c: c["watchdog"].update(token_file=c["status"]["token_file"]),
        lambda c: c["release"].update(source_sha256="abc123"),
        lambda c: c["release"].update(directory="relative/release"),
        lambda c: c["ledger"].update(marker_file="/private/ledger/marker.json"),
        lambda c: c["ledger"].update(pg_ctl="/usr/local/bin/postgres"),
        lambda c: c["ledger"].update(wait_seconds=0),
        lambda c: c["logs"].update(max_bytes=1024),
        lambda c: c["backup"].update(retention_days=0),
    ):
        config = copy.deepcopy(original)
        mutate(config)
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match="INVALID_PRIVATE_LAUNCH_CONFIGURATION"):
            load_private_config(path)


def test_v1_config_loads_deprecated_but_cannot_render_or_launch(tmp_path):
    path = write_config(tmp_path / "v1.json", v1_config(tmp_path))
    loaded = load_private_config(path)
    assert loaded["version"] == 1 and loaded["deprecation"].startswith("PRIVATE_CONFIG_V1_")
    report = preflight(loaded)
    assert report["config_version"] == 1 and report["deprecation"] == loaded["deprecation"]
    assert report["token_separation"] == {"mode": "SINGLE_SHARED_TOKEN_V1", "separated": False}
    assert "CONFIG_VERSION_2_REQUIRED" in report["invalid_configuration_names"]
    assert not report["configuration_complete"] and not report["ready_to_trade"]
    with pytest.raises(ValueError, match="PRIVATE_CONFIG_V2_REQUIRED"):
        render_supervisor(path, loaded, tmp_path / "plists")
    assert not (tmp_path / "plists").exists()
    calls = []
    with pytest.raises(ValueError, match="PRIVATE_CONFIG_V2_REQUIRED"):
        launch_component(path, "app", execute=lambda *args: calls.append(args))
    assert calls == []


def test_launcher_uses_direct_argv_and_does_not_give_muse_broker_credentials(ops, monkeypatch):
    path, config = ops
    calls = []
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "inherited-do-not-pass")
    monkeypatch.setattr(os, "umask", lambda _: None)
    launch = launcher(config)
    launch(path, "muse", execute=lambda *args: calls.append(args))
    executable, argv, env = calls[0]
    assert executable == argv[0] and "catalyst_lab.muse_worker" in argv
    assert "--command=--sandbox" in argv and "--command=read-only" in argv
    assert "APCA_API_SECRET_KEY" not in env and "UNRELATED_SECRET" not in env
    assert not {"MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN", "MANAGED_OPERATOR_TOKEN"} & set(env)
    assert "MANAGED_DATABASE_URL" not in env
    assert TOKEN not in json.dumps(argv)
    launch(path, "app", execute=lambda *args: calls.append(args))
    _, argv, env = calls[1]
    assert env["APCA_API_SECRET_KEY"] == config["environment"]["APCA_API_SECRET_KEY"]
    assert (env["MANAGED_API_TOKEN"], env["MANAGED_STATUS_TOKEN"],
            env["MANAGED_OPERATOR_TOKEN"]) == (TOKEN, STATUS_TOKEN, OPERATOR_TOKEN)
    assert "UNRELATED_SECRET" not in env
    assert not {TOKEN, STATUS_TOKEN, OPERATOR_TOKEN} & set(argv)
    logs = Path(config["logs"]["directory"])
    for name in ("app.log", "muse.log"):
        text = (logs / name).read_text()
        assert "LAUNCHER START" in text and f"release_commit={COMMIT}" in text
        assert not any(secret in text for secret in (TOKEN, STATUS_TOKEN, "fixture-secret"))


def test_app_launch_refuses_shared_or_missing_role_tokens(ops):
    path, config = ops
    private_file(config["operator"]["token_file"], STATUS_TOKEN)
    calls = []
    with pytest.raises(ValueError, match="ROLE_TOKENS_NOT_SEPARATED"):
        launcher(config)(path, "app", execute=lambda *args: calls.append(args))
    os.unlink(config["operator"]["token_file"])
    with pytest.raises(ValueError, match="PRIVATE_ROLE_TOKEN_INVALID"):
        launcher(config)(path, "app", execute=lambda *args: calls.append(args))
    assert calls == []


AGENT_TOKEN = "fixture-private-agent-token-abcdefghijklmnopqrstuvwxyz"


def test_v2_agents_section_is_optional_exact_and_one_file_per_agent(ops, tmp_path):
    path, original = ops
    assert original["agents"] == []  # The template lists no agent credentials.
    entry = {"agent_id": "instinct", "token_file": str(tmp_path / "instinct-token")}
    for agents in (None, [entry], [entry, {"agent_id": "grogbot",
                                           "token_file": str(tmp_path / "grogbot-token")}]):
        config = copy.deepcopy(original)
        if agents is None:
            config.pop("agents")  # A v2 file written before the section existed still loads.
        else:
            config["agents"] = agents
        write_config(path, config)
        assert load_private_config(path) == config
    for mutate in (
        lambda c: c.update(agents={}),
        lambda c: c.update(agents=[{"agent_id": "instinct"}]),
        lambda c: c.update(agents=[{**entry, "scopes": ["reports:write"]}]),
        lambda c: c.update(agents=[{**entry, "agent_id": "Instinct"}]),
        lambda c: c.update(agents=[entry, {**entry, "token_file": str(tmp_path / "other")}]),
        lambda c: c.update(agents=[{**entry, "token_file": "relative/instinct-token"}]),
        lambda c: c.update(agents=[{**entry, "token_file": c["muse"]["token_file"]}]),
        lambda c: c.update(agents=[entry, {"agent_id": "grogbot",
                                           "token_file": entry["token_file"]}]),
    ):
        config = copy.deepcopy(original)
        mutate(config)
        write_config(path, config)
        with pytest.raises(ValueError, match="INVALID_PRIVATE_LAUNCH_CONFIGURATION"):
            load_private_config(path)


def test_app_launch_injects_separate_agent_tokens_never_into_muse_or_argv(
    ops, tmp_path, monkeypatch
):
    path, config = ops
    token_file = tmp_path / "instinct-token"
    config["agents"] = [{"agent_id": "instinct", "token_file": str(token_file)}]
    write_config(path, config)
    private_file(token_file, AGENT_TOKEN)
    monkeypatch.setattr(os, "umask", lambda _: None)
    calls = []
    launch = launcher(config)
    launch(path, "app", execute=lambda *args: calls.append(args))
    launch(path, "muse", execute=lambda *args: calls.append(args))
    (_, app_argv, app_env), (_, muse_argv, muse_env) = calls
    assert json.loads(app_env["MANAGED_AGENT_TOKENS_JSON"]) == {"instinct": AGENT_TOKEN}
    assert "MANAGED_AGENT_TOKENS_JSON" not in muse_env
    assert AGENT_TOKEN not in json.dumps([app_argv, muse_argv])
    logs = Path(config["logs"]["directory"])
    assert not any(AGENT_TOKEN in (logs / name).read_text() for name in ("app.log", "muse.log"))
    report = preflight(load_private_config(path))
    assert report["token_separation"]["agents"] == {"instinct": "PRIVATE_VALID"}
    assert report["token_separation"]["distinct_values"] is True
    assert AGENT_TOKEN not in json.dumps(report)
    private_file(token_file, TOKEN)  # The legacy Muse token reused as an agent credential.
    with pytest.raises(ValueError, match="AGENT_TOKENS_NOT_SEPARATED"):
        launch(path, "app", execute=lambda *args: calls.append(args))
    assert "ROLE_TOKENS_NOT_SEPARATED" in preflight(
        load_private_config(path))["invalid_configuration_names"]
    os.unlink(token_file)
    with pytest.raises(ValueError, match="PRIVATE_ROLE_TOKEN_INVALID"):
        launch(path, "app", execute=lambda *args: calls.append(args))
    assert "agents.instinct.token_file" in preflight(
        load_private_config(path))["missing_configuration_names"]
    assert len(calls) == 2


def test_release_hash_or_location_mismatch_refuses_every_launch(ops, tmp_path):
    path, config = ops
    package = package_of(config)
    calls = []
    run = partial(launch_component, execute=lambda *args: calls.append(args),
                  probe=lambda dsn: True)
    # Identical code outside the configured release directory (a working copy) is refused.
    elsewhere = tmp_path / "working-copy" / "catalyst_lab"
    shutil.copytree(package, elsewhere)
    with pytest.raises(ValueError, match="RELEASE_PATH_MISMATCH"):
        run(path, "app", package_dir=elsewhere)
    # So is the interpreter's own package when it is not the configured release.
    with pytest.raises(ValueError, match="RELEASE_HASH_MISMATCH"):
        run(path, "muse")
    # Code that drifted after the build is refused for every component, before anything runs.
    (package / "managed_ops.py").write_text("VALUE = 2\n")
    for component in ORDER:
        with pytest.raises(ValueError, match="RELEASE_HASH_MISMATCH"):
            run(path, component, package_dir=package)
    (package / "managed_ops.py").write_text("VALUE = 1\n")
    os.unlink(Path(config["release"]["directory"]) / "release.json")
    with pytest.raises(ValueError, match="RELEASE_METADATA_MISSING"):
        run(path, "app", package_dir=package)
    assert calls == []
    log = (Path(config["logs"]["directory"]) / "app.log").read_text()
    assert "REFUSED" in log and "code=RELEASE_PATH_MISMATCH" in log
    assert "code=RELEASE_HASH_MISMATCH" in log and TOKEN not in log


def test_database_wait_uses_bounded_exponential_backoff():
    clock, sleeps, probes = [0.0], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def probe(dsn):
        probes.append(dsn)
        clock[0] += 0.5  # A probe takes time too; it counts against the bound.
        return False

    with pytest.raises(ValueError, match="DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT"):
        wait_for_database("fixture-dsn", max_seconds=120, probe=probe, sleep=sleep,
                          clock=lambda: clock[0])
    assert sleeps[:5] == [1, 2, 4, 8, 15] and max(sleeps) <= 15
    assert clock[0] <= 120 + 0.5 and len(probes) == len(sleeps) + 1
    answers = iter([False, False, True])
    ready = wait_for_database("fixture-dsn", max_seconds=120, probe=lambda dsn: next(answers),
                              sleep=lambda seconds: None, clock=lambda: 0.0)
    assert ready["database"] == "READY" and ready["attempts"] == 3


def test_app_and_muse_wait_for_the_database_before_starting(ops):
    path, config = ops
    config["ledger"]["wait_seconds"] = 5
    write_config(path, config)
    clock, calls, probed = [0.0], [], []

    def probe(dsn):
        probed.append(dsn)
        return False

    for component in ("app", "muse"):
        with pytest.raises(ValueError, match="DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT"):
            launch_component(path, component, execute=lambda *args: calls.append(args),
                             package_dir=package_of(config), probe=probe,
                             sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                             monotonic=lambda: clock[0])
    assert calls == [] and set(probed) == {config["environment"]["MANAGED_DATABASE_URL"]}
    log = (Path(config["logs"]["directory"]) / "muse.log").read_text()
    assert "code=DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT" in log and "nonexistent" not in log
    # An unset DSN is refused outright instead of probing libpq's default socket.
    config["environment"]["MANAGED_DATABASE_URL"] = "REQUIRED_CATALYST_RISK_CONNECTION"
    write_config(path, config)
    probed.clear()
    with pytest.raises(ValueError, match="MANAGED_DATABASE_URL_REQUIRED"):
        launch_component(path, "muse", execute=lambda *args: calls.append(args),
                         package_dir=package_of(config), probe=probe)
    assert probed == [] and calls == []


def test_crash_loop_breaker_trips_on_fifth_start_in_ten_minutes_and_resets(ops):
    path, config = ops
    calls = []

    def start(at):
        return launcher(config, clock=lambda: at)(
            path, "app", execute=lambda *args: calls.append(args) or 1)

    for at in (1000, 1010, 1020, 1030):
        assert start(at) == 1  # The child crashed; launchd would restart it.
    tripped = start(1040)
    assert tripped["tripped"] and tripped["starts_in_window"] == 5 and len(calls) == 4
    assert tripped["alarms"] == ["CRASH_LOOP_BREAKER_TRIPPED", "CRASH_LOOP_APP"]
    state = launcher_directory(path)
    alarm = state / "crash-loop-app.json"
    assert json.loads(alarm.read_text())["component"] == "app"
    for file in (alarm, state / "app.starts.json"):
        assert file.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700
    assert {"CRASH_LOOP_BREAKER_TRIPPED", "CRASH_LOOP_APP"} <= set(
        release_alarms(config, None, config_path=path))
    # Other components keep their own count; a kickstart inside the window stays tripped.
    assert launcher(config, clock=lambda: 1050)(
        path, "muse", execute=lambda *args: calls.append(args) or 0) == 0
    assert start(1300)["tripped"] and len(calls) == 5
    # Ten quiet minutes after the last start, the next start runs and clears the alarm.
    assert start(1300 + 601) == 1 and len(calls) == 6
    assert not alarm.exists() and release_alarms(config, None, config_path=path) == []


def test_crash_loop_breaker_counts_starts_even_with_an_unreadable_config(ops):
    path, _ = ops
    path.chmod(0o644)  # The launcher cannot load it, so it fails on every start.
    for at in (0, 10, 20, 30):
        with pytest.raises(ValueError, match="INVALID_PRIVATE_LAUNCH_CONFIGURATION"):
            launch_component(path, "watchdog", clock=lambda at=at: at)
    assert launch_component(path, "watchdog", clock=lambda: 40)["tripped"]


def test_launcher_script_exit_codes(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_managed_private.py"
    spec = importlib.util.spec_from_file_location("run_managed_private_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["run", "--config", "/fixture.json", "--component", "app"])
    monkeypatch.setattr(module, "private_standard_streams", lambda: None)
    monkeypatch.setattr(module, "launch_component", lambda *a: {"tripped": True})
    assert module.main() is None  # Tripped breaker: exit 0, so launchd stops restarting.
    monkeypatch.setattr(module, "launch_component", lambda *a: 3)
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert exited.value.code == 3

    def refused(*args):
        raise ValueError("RELEASE_HASH_MISMATCH")

    monkeypatch.setattr(module, "launch_component", refused)
    with pytest.raises(SystemExit, match="PRIVATE_COMPONENT_FAILED"):
        module.main()


def test_rotating_log_is_owner_only_and_rotates_by_size(tmp_path):
    directory = tmp_path / "logs"
    log = RotatingLog(directory / "app.log", max_bytes=100, backups=2)
    for index in range(10):
        log.write(b"%02d" % index + b"x" * 43)
    log.close()
    names = sorted(path.name for path in directory.iterdir())
    assert names == ["app.log", "app.log.1", "app.log.2"]
    for name in names:
        assert (directory / name).stat().st_size <= 100
        assert (directory / name).stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "app.log").read_bytes().startswith(b"08")  # Oldest output dropped.
    permissive = directory / "old.log"
    permissive.write_text("earlier\n")
    permissive.chmod(0o644)
    RotatingLog(permissive, max_bytes=100, backups=2).close()
    assert permissive.stat().st_mode & 0o777 == 0o600
    (directory / "linked.log").symlink_to(permissive)
    with pytest.raises(OSError):
        RotatingLog(directory / "linked.log", max_bytes=100, backups=2)


def test_supervised_child_output_goes_to_rotating_private_log(tmp_path):
    log = RotatingLog(tmp_path / "logs" / "muse.log", max_bytes=65536, backups=3)
    code = run_logged([sys.executable, "-c",
                       "import sys; sys.stdout.write('o' * 100000); sys.stdout.flush(); "
                       "sys.stderr.write('STDERR-MARKER'); sys.exit(3)"],
                      {"PATH": os.environ["PATH"]}, log)
    log.close()
    assert code == 3
    # Oldest first: muse.log.N … muse.log.1, then the current muse.log.
    files = sorted((tmp_path / "logs").iterdir(),
                   key=lambda path: -int(path.suffix[1:]) if path.suffix[1:].isdigit() else 0)
    assert "muse.log.1" in {path.name for path in files} and len(files) <= 4
    assert all(path.stat().st_size <= 65536 for path in files)
    text = b"".join(path.read_bytes() for path in files)
    assert text.count(b"o" * 100000) == 1 and b"STDERR-MARKER" in text
    assert text.rstrip().endswith(b"CHILD_EXITED code=3")
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


def test_launchd_sigterm_is_forwarded_as_the_components_shutdown_signal(tmp_path):
    log_path = tmp_path / "logs" / "ledger.log"
    log = RotatingLog(log_path, max_bytes=65536, backups=1)
    child = ("import signal, sys, time\n"
             "signal.signal(signal.SIGINT, lambda *a: sys.exit(42))\n"
             "print('READY', flush=True)\n"
             "time.sleep(20)\n")

    def terminate_when_ready():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if log_path.exists() and b"READY" in log_path.read_bytes():
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.05)

    before = signal.getsignal(signal.SIGTERM)
    threading.Thread(target=terminate_when_ready, daemon=True).start()
    code = run_logged([sys.executable, "-c", child], {"PATH": os.environ["PATH"]}, log,
                      term_signal=signal.SIGINT)
    log.close()
    assert code == 42  # SIGTERM arrived as SIGINT: PostgreSQL's fast shutdown for the ledger.
    assert signal.getsignal(signal.SIGTERM) == before


def test_private_standard_streams_tightens_launchd_output_files(tmp_path):
    output = tmp_path / "bootstrap.log"
    output.write_text("")
    output.chmod(0o644)
    with output.open("a") as stream:
        subprocess.run([sys.executable, "-c",
                        "from catalyst_lab.managed_ops import private_standard_streams\n"
                        "private_standard_streams()"],
                       stdout=stream, stderr=stream, check=True)
    assert output.stat().st_mode & 0o777 == 0o600


def test_ledger_runs_foreground_postgres_only_for_a_marked_account_ledger(ops, tmp_path):
    path, config = ops
    root, binaries = tmp_path / "ledger", tmp_path / "postgresql" / "bin"
    root.mkdir()
    binaries.mkdir(parents=True)
    config["ledger"].update(data_directory=str(root / "postgres"),
                            socket_directory=str(root / "socket"),
                            pg_ctl=str(binaries / "pg_ctl"),
                            marker_file=str(root / "LEDGER.json"))
    write_config(path, config)
    calls = []
    launch = launcher(config)
    with pytest.raises(ValueError, match="LEDGER_ACCOUNT_MARKER_REQUIRED"):
        launch(path, "ledger", execute=lambda *args: calls.append(args))
    private_file(root / "LEDGER.json", json.dumps({"role": "ARCHIVED"}))
    with pytest.raises(ValueError, match="LEDGER_ACCOUNT_MARKER_REQUIRED"):
        launch(path, "ledger", execute=lambda *args: calls.append(args))
    assert calls == []
    private_file(root / "LEDGER.json", json.dumps({"role": "ACCOUNT_LEDGER"}))
    launch(path, "ledger", execute=lambda *args: calls.append(args))
    [(executable, argv, env)] = calls
    assert executable == str(binaries / "postgres")
    assert argv == [str(binaries / "postgres"), "-D", str(root / "postgres"),
                    "-k", str(root / "socket")]
    assert not {"APCA_API_SECRET_KEY", "MANAGED_DATABASE_URL", "MANAGED_API_TOKEN"} & set(env)


def test_backup_component_backs_up_the_marked_ledger_then_prunes(ops, monkeypatch, capsys):
    from catalyst_lab import ledger_ops

    path, config = ops
    calls = []

    def fake_backup(root, destination, *, allow_owner_ledger=False, now=None):
        calls.append(("backup", Path(root), Path(destination), allow_owner_ledger))
        return {"backup": str(Path(destination) / "2026-09-24T120000Z"),
                "manifest_sha256": "f" * 64}

    def fake_verify(backup_dir, *, expected_head=None, expected_manifest_sha256=None):
        calls.append(("verify", Path(backup_dir), expected_manifest_sha256))
        return {"result": "VERIFIED"}

    def fake_prune(destination, retention_days=14, *, dry_run=False, now=None):
        calls.append(("prune", Path(destination), retention_days, dry_run))
        return {"deleted": [], "kept": ["2026-09-24T120000Z"]}

    monkeypatch.setattr(ledger_ops, "backup", fake_backup)
    monkeypatch.setattr(ledger_ops, "verify_manifest", fake_verify)
    monkeypatch.setattr(ledger_ops, "prune", fake_prune)
    root = Path(config["ledger"]["marker_file"]).parent
    destination = Path(config["backup"]["directory"])
    written = destination / "2026-09-24T120000Z"
    expected = {"backup": {"backup": str(written), "manifest_sha256": "f" * 64},
                "verify": {"result": "VERIFIED"},
                "prune": {"deleted": [], "kept": ["2026-09-24T120000Z"]}}
    assert launcher(config)(path, "backup") == expected
    # The ledger root comes from the marker; the owner-ledger flag is deliberate; the new backup is
    # verified against its own manifest hash; then retention.
    assert calls == [("backup", root, destination, True), ("verify", written, "f" * 64),
                     ("prune", destination, 14, False)]
    monkeypatch.setattr(sys, "argv", ["managed_ops", "backup", "--config", str(path)])
    managed_ops.main()
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == expected

    def refusing(*args, **kwargs):
        raise ledger_ops.LedgerOpsError("LEDGER_CLUSTER_NOT_RUNNING")

    monkeypatch.setattr(ledger_ops, "backup", refusing)
    with pytest.raises(ValueError, match="^LEDGER_CLUSTER_NOT_RUNNING$"):
        launcher(config)(path, "backup")
    with pytest.raises(SystemExit, match="PRIVATE_OPERATIONS_FAILED: LEDGER_CLUSTER_NOT_RUNNING"):
        managed_ops.main()
    # A layout ledger_ops cannot derive from the marker file is refused before any call.
    calls.clear()
    other = write_config(path.parent / "other-layout.json", {
        **config, "ledger": {**config["ledger"], "data_directory": "/REQUIRED/elsewhere/postgres"}})
    with pytest.raises(ValueError, match="LEDGER_LAYOUT_UNSUPPORTED_FOR_BACKUP"):
        launcher(config)(other, "backup")
    assert calls == []


def test_supervisor_renders_the_release_in_start_order_without_secrets(ops, tmp_path):
    path, config = ops
    release = Path(config["release"]["directory"])
    result = render_supervisor(path, config, tmp_path / "review")
    assert result["installed"] is False and result["started"] is False
    labels = ["local.catalyst.paper." + component for component in ORDER]
    assert result["start_order"] == labels and result["release_commit"] == COMMIT
    assert [Path(file).name for file in result["files"]] == [label + ".plist" for label in labels]
    assert result["backup"] == "DAILY_LEDGER_OPS_BACKUP"
    for file, component in zip(result["files"], ORDER, strict=True):
        raw = Path(file).read_bytes()
        plist = plistlib.loads(raw)
        for secret in (TOKEN, STATUS_TOKEN, OPERATOR_TOKEN, "fixture-secret", "nonexistent"):
            assert secret.encode() not in raw
        assert "EnvironmentVariables" not in plist and "launchctl" not in json.dumps(plist)
        assert Path(file).stat().st_mode & 0o777 == 0o600
        assert plist["Umask"] == 0o077 and plist["WorkingDirectory"] == str(release)
        assert plist["ProgramArguments"] == [
            str(release / "venv" / "bin" / "python"),
            str(release / "scripts" / "run_managed_private.py"),
            "--config", str(path), "--component", component]
        assert plist["StandardErrorPath"] == str(
            Path(config["logs"]["directory"]) / f"{component}.launchd.log")
        assert plist["StandardOutPath"] == plist["StandardErrorPath"] != "/dev/null"
        if component in {"audit", "backup"}:
            assert not plist["RunAtLoad"] and "KeepAlive" not in plist
            assert plist["StartInterval"] == (3600 if component == "audit" else 86400)
        else:
            assert plist["KeepAlive"] == {"SuccessfulExit": False} and plist["RunAtLoad"]
        expected = "Standard" if component in {"ledger", "app"} else "Background"
        assert plist["ProcessType"] == expected
    assert Path(config["logs"]["directory"]).stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="RENDER_DESTINATION_EXISTS"):
        render_supervisor(path, config, tmp_path / "review")
    stale = copy.deepcopy(config)
    stale["release"]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="RELEASE_NOT_VERIFIED"):
        render_supervisor(path, stale, tmp_path / "review-2")
    (package_of(config) / "managed_ops.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="RELEASE_NOT_VERIFIED"):
        render_supervisor(path, config, tmp_path / "review-3")
    assert not (tmp_path / "review-2").exists() and not (tmp_path / "review-3").exists()


def test_code_version_hashes_the_imported_package_not_a_checkout(tmp_path):
    release, package, digest = fake_release(tmp_path)
    assert code_version(package) == {"source_sha256": digest, "release_commit": COMMIT,
                                     "release_metadata": "MATCH"}
    copied = tmp_path / "elsewhere" / "catalyst_lab"
    shutil.copytree(package, copied)
    assert code_version(copied) == {"source_sha256": digest, "release_commit": None,
                                    "release_metadata": "ABSENT"}
    (copied / "__pycache__").mkdir()
    (copied / "__pycache__" / "managed_ops.cpython-312.pyc").write_bytes(b"cache")
    (copied / "notes.txt").write_text("not code")
    assert package_sha256(copied) == digest
    (copied / "extra.py").symlink_to(copied / "managed_ops.py")
    assert package_sha256(copied) != digest
    (release / "release.json").write_text(json.dumps({
        "commit": COMMIT, "source_sha256": "0" * 64, "built_at": "x", "python": {}}))
    assert code_version(package)["release_commit"] is None
    assert code_version(package)["release_metadata"] == "MISMATCH"
    (release / "release.json").write_text("{not json")
    assert code_version(package)["release_metadata"] == "INVALID"
    live = code_version()
    assert live["source_sha256"] == package_sha256(package_directory())
    assert (package_directory() / "managed_ops.py").is_file()


def test_template_is_v2_and_loads(tmp_path):
    config = config_template(tmp_path)
    assert config["config_version"] == 2 and config["notify"] == {}
    assert "MANAGED_API_TOKEN" not in config["environment"]
    assert len({config[role]["token_file"] for role in ("muse", "status", "operator")}) == 3
    assert config["logs"]["max_bytes"] == 20 * 1024 * 1024
    assert config["ledger"]["wait_seconds"] == 120
    path = write_config(tmp_path / "template.json", config)
    assert load_private_config(path) == config


def healthy(now, code_version=None):
    return {"worker_state": "RUNNING", "last_protection_tick": now.isoformat(),
            "last_reconciliation_at": now.isoformat(), "last_cycle_at": now.isoformat(),
            "trade_stream_connected": True, "research_healthy": True,
            "executor_ownership": "EXCLUSIVE",
            "account_safety_healthy": True, "market_streams": {"US": False, "CRYPTO": True},
            "required_market_streams": ["CRYPTO"], "error_code": None,
            "code_version": code_version}


def heartbeat(config, now):
    """A Muse spool with MuseSpool's own schema and one fresh heartbeat run."""
    path = config["muse"]["spool"]
    MuseSpool(path).db.close()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO runs(id,kind,started_at,outcome) VALUES(?,?,?,?)",
                           (str(uuid4()), "HEARTBEAT", now.isoformat(), "RUNNING"))
    Path(path).chmod(0o600)


def test_watchdog_requires_auth_uses_fresh_required_streams_and_persists_private_status(ops):
    path, config = ops
    now = datetime.now(UTC)
    heartbeat(config, now)
    digest = config["release"]["source_sha256"]

    def respond(request):
        assert request.method == "GET" and request.url.path == "/api/v1/lab/status"
        assert request.headers["Authorization"] == "Bearer " + STATUS_TOKEN
        return httpx.Response(200, json=healthy(now, digest))

    result = watchdog_once(config, transport=httpx.MockTransport(respond), now=now,
                           config_path=path)
    assert result["alarms"] == []
    alarm = Path(config["watchdog"]["alarm_file"])
    assert json.loads(alarm.read_text()) == result
    assert alarm.stat().st_mode & 0o777 == 0o600
    assert TOKEN not in alarm.read_text() and STATUS_TOKEN not in alarm.read_text()


def test_watchdog_alarms_when_the_running_app_is_not_the_configured_release(ops):
    _, config = ops
    now = datetime.now(UTC)
    heartbeat(config, now)
    result = watchdog_once(config, now=now, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=healthy(now, "f" * 64))))
    assert result["alarms"] == ["RELEASE_CODE_MISMATCH"]
    assert release_alarms(config, healthy(now, None)) == ["RELEASE_CODE_MISMATCH"]


def test_watchdog_alarms_are_redacted_on_stalls_loss_provider_errors_and_muse_death(ops):
    _, config = ops
    now = datetime.now(UTC)
    state = healthy(now - timedelta(seconds=200), config["release"]["source_sha256"])
    state.update(worker_state="STOPPED", trade_stream_connected=False, research_healthy=False,
                 account_safety_healthy=False, market_streams={"CRYPTO": False},
                 error_code=TOKEN)
    result = watchdog_once(config, now=now, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=state)))
    assert set(result["alarms"]) == {
        "WORKER_STOPPED", "PROTECTION_TICK_STALE", "RECONCILIATION_STALE", "RESEARCH_TICK_STALE",
        "BROKER_STREAM_LOST", "RESEARCH_UNHEALTHY", "ACCOUNT_SAFETY_UNHEALTHY",
        "CRYPTO_MARKET_STREAM_LOST", "WORKER_REPORTED_ERROR", "MUSE_HEARTBEAT_STALE_OR_UNAVAILABLE",
    }
    assert TOKEN not in json.dumps(result)
    failed = watchdog_once(config, now=now, transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text=TOKEN)))
    assert "STATUS_UNAVAILABLE" in failed["alarms"] and TOKEN not in json.dumps(failed)


def test_v1_watchdog_still_reads_status_with_its_single_token(tmp_path):
    config = load_private_config(write_config(tmp_path / "v1.json", v1_config(tmp_path)))
    private_file(config["watchdog"]["token_file"], TOKEN)
    now = datetime.now(UTC)
    seen = []

    def respond(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=healthy(now))

    watchdog_once(config, transport=httpx.MockTransport(respond), now=now)
    assert seen == ["Bearer " + TOKEN]


def test_watchdog_rejects_future_timestamp_and_missing_stream_requirements(ops):
    _, config = ops
    from catalyst_lab.managed_ops import CLOCK_SKEW_TOLERANCE_SECONDS

    now = datetime.now(UTC)
    # Beyond the clock-skew allowance: a tick within it is fresh (see the test at the end).
    status = healthy(now + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS + 1))
    status.pop("required_market_streams")
    status.pop("executor_ownership")
    alarms = status_alarms(status, now, config["watchdog"])
    assert "PROTECTION_TICK_STALE" in alarms and "MARKET_STREAM_REQUIREMENTS_UNAVAILABLE" in alarms
    assert "EXECUTOR_OWNERSHIP_UNAVAILABLE" in alarms


@pytest.mark.parametrize("state", ["OPEN", "HALF_OPEN"])
def test_open_jev_breaker_alarms_and_clears_once_closed(ops, state):
    """Package jev-breaker: JEV_BREAKER_OPEN fires for either non-CLOSED breaker state and
    disappears once the breaker reports CLOSED again; an older/unconfigured status
    (no jev_breaker field at all) must never raise it."""
    _, config = ops
    now = datetime.now(UTC)
    status = healthy(now)
    status["jev_breaker"] = {"state": state, "epoch": 2, "blocked_until": now.isoformat()}
    assert "JEV_BREAKER_OPEN" in status_alarms(status, now, config["watchdog"])
    status["jev_breaker"] = {"state": "CLOSED", "epoch": 2, "blocked_until": None}
    assert "JEV_BREAKER_OPEN" not in status_alarms(status, now, config["watchdog"])
    del status["jev_breaker"]
    assert "JEV_BREAKER_OPEN" not in status_alarms(status, now, config["watchdog"])


def test_the_scorecard_is_due_from_seven_utc_on_the_day_the_nightly_run_records():
    """Package ops-alarms: the jobs run at 05:30 UTC (after the New York midnight all year) and
    record the previous New York day; from 07:00 UTC that day's DAILY_SCORECARD_V1 is due,
    and until 07:00 UTC the check stays on the day the run before recorded."""
    from datetime import date
    from zoneinfo import ZoneInfo

    from catalyst_lab.learning_jobs import previous_days
    from catalyst_lab.managed_ops import LEARNING_JOBS_DUE_AFTER, scorecard_due_day

    assert LEARNING_JOBS_DUE_AFTER == timedelta(hours=7)
    for month in (7, 12):  # EDT and EST.
        run = datetime(2026, month, 15, 5, 30, tzinfo=UTC)
        recorded = date(2026, month, 14)
        assert previous_days(run)[-1] == recorded  # The day this run records.
        assert scorecard_due_day(run) == recorded - timedelta(days=1)
        assert scorecard_due_day(run.replace(hour=6, minute=59, second=59)) == (
            recorded - timedelta(days=1))
        assert scorecard_due_day(run.replace(hour=7, minute=0)) == recorded
        assert scorecard_due_day(run.replace(hour=23, minute=59)) == recorded
        assert scorecard_due_day(run.replace(day=16, hour=3)) == recorded
        assert scorecard_due_day(run.replace(day=16, hour=7)) == recorded + timedelta(days=1)
    # Any time zone: 03:00 UTC on the 29th is 23:00 on the 28th in New York.
    late = datetime(2026, 9, 29, 3, 0, tzinfo=UTC).astimezone(ZoneInfo("America/New_York"))
    assert scorecard_due_day(late) == date(2026, 9, 27)


def test_ledger_alarms_raise_a_missed_scorecard_and_a_large_ledger_and_fail_closed():
    """LEARNING_JOBS_MISSED while the due scorecard is not recorded; DATABASE_SIZE_HIGH from
    3 GB (60% of the 5 GB Postgres volume); a fact the ops watchdog could not read is
    LEDGER_CHECK_FAILED, never healthy."""
    from catalyst_lab.managed_ops import DATABASE_SIZE_HIGH_BYTES, ledger_alarms

    assert DATABASE_SIZE_HIGH_BYTES == 3_000_000_000

    def facts(recorded=True, size=211_000_000, **extra):
        return {"checked_at": "2026-09-29T07:00:00+00:00", "scorecard_day": "2026-09-28",
                "scorecard_recorded": recorded, "database_bytes": size, **extra}

    assert ledger_alarms(facts()) == []
    assert ledger_alarms(facts(recorded=False)) == ["LEARNING_JOBS_MISSED"]
    assert ledger_alarms(facts(size=DATABASE_SIZE_HIGH_BYTES - 1)) == []
    assert ledger_alarms(facts(size=DATABASE_SIZE_HIGH_BYTES)) == ["DATABASE_SIZE_HIGH"]
    assert ledger_alarms(facts(recorded=False, size=4 * 10**9)) == [
        "LEARNING_JOBS_MISSED", "DATABASE_SIZE_HIGH"]
    size_unread = facts(code="INSUFFICIENTPRIVILEGE")
    del size_unread["database_bytes"]
    assert ledger_alarms(size_unread) == ["LEDGER_CHECK_FAILED"]
    size_unread["scorecard_recorded"] = False
    assert ledger_alarms(size_unread) == ["LEARNING_JOBS_MISSED", "LEDGER_CHECK_FAILED"]
    for broken in (None, "nope", {"code": "OPERATIONALERROR"}, facts(recorded=None),
                   facts(recorded="yes"), facts(size=True), facts(size="211"), facts(size=-1),
                   facts(size=3.5e9)):
        assert ledger_alarms(broken) == ["LEDGER_CHECK_FAILED"], broken


def test_the_ops_volume_alarms_below_twenty_percent_free_and_fails_closed():
    from catalyst_lab.managed_ops import OPS_VOLUME_MIN_FREE_PERCENT, ops_volume_alarms

    assert OPS_VOLUME_MIN_FREE_PERCENT == 20
    total = 4096 * 1024 * 1024  # The ops volume's 4 GB (sizeMB 4096 in .railway/railway.ts).
    fifth = -(-total // 5)  # The least free space that is 20% of it.
    assert ops_volume_alarms({"total_bytes": total, "free_bytes": total}) == []
    assert ops_volume_alarms({"total_bytes": total, "free_bytes": fifth}) == []
    assert ops_volume_alarms({"total_bytes": total, "free_bytes": fifth - 1}) == [
        "OPS_VOLUME_LOW"]
    assert ops_volume_alarms({"total_bytes": total, "free_bytes": 0}) == ["OPS_VOLUME_LOW"]
    for broken in (None, {"code": "FILENOTFOUNDERROR"}, {"total_bytes": 0, "free_bytes": 0},
                   {"total_bytes": total, "free_bytes": total + 1},
                   {"total_bytes": total, "free_bytes": -1},
                   {"total_bytes": str(total), "free_bytes": total}):
        assert ops_volume_alarms(broken) == ["OPS_VOLUME_LOW"], broken


def test_fresh_muse_heartbeat_does_not_hide_exhausted_delivery_or_failed_work(ops):
    _, config = ops
    now = datetime.now(UTC)
    heartbeat(config, now)
    old = (now - timedelta(seconds=700)).isoformat()
    path = config["muse"]["spool"]
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO outbox(id,method,path,created_at,deadline,attempts) "
                     "VALUES('fixture-item','POST','/fixture',?,?,5)",
                     (old, (now + timedelta(seconds=300)).isoformat()))
        # A current provider job: no later job or research run since it started.
        conn.execute("INSERT INTO provider_jobs(id,request,started_at) "
                     "VALUES('report-job:CRYPTO:1','{}',?)", (old,))
        conn.execute("INSERT INTO runs(id,kind,started_at,outcome) VALUES(?,?,?,?)",
                     (str(uuid4()), "OUTBOX", now.isoformat(), TOKEN))
    assert set(muse_heartbeat_alarm(path, now, 600)) == {
        "MUSE_DELIVERY_EXHAUSTED", "MUSE_DELIVERY_STALE", "MUSE_WORK_FAILED",
    }
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE outbox SET delivered_at=?", (now.isoformat(),))
        conn.execute("UPDATE provider_jobs SET result=?,completed_at=?", (TOKEN, now.isoformat()))
        conn.execute("INSERT INTO runs(id,kind,started_at,outcome) VALUES(?,?,?,?)",
                     (str(uuid4()), "RESEARCH", now.isoformat(), "NO_OP"))
    assert muse_heartbeat_alarm(path, now, 600) == []


def test_muse_pending_result_deadline_alarms_before_generic_age_bound(ops):
    _, config = ops
    now = datetime.now(UTC)
    heartbeat(config, now)
    with sqlite3.connect(config["muse"]["spool"]) as conn:
        conn.execute("INSERT INTO outbox(id,method,path,created_at,deadline,attempts) "
                     "VALUES('fixture-item','POST','/fixture',?,?,1)",
                     (now.isoformat(), (now - timedelta(seconds=1)).isoformat()))
    assert muse_heartbeat_alarm(config["muse"]["spool"], now, 600) == ["MUSE_DELIVERY_STALE"]


def test_preflight_is_explicit_about_unverified_runtime_and_never_echoes_secrets(ops, tmp_path):
    _, config = ops
    result = preflight(config, tmp_path)
    assert result["schema_actual"] is None and result["database_check"] == "NOT_REQUESTED"
    assert not result["broker_checked"] and not result["ready_to_trade"]
    assert not result["configuration_complete"]
    for secret in (TOKEN, STATUS_TOKEN, OPERATOR_TOKEN, "fixture-secret"):
        assert secret not in json.dumps(result)


def test_preflight_reports_release_hash_token_separation_and_ledger_marker(
    ops, tmp_path, monkeypatch
):
    path, config = ops
    report = preflight(load_private_config(path))
    assert report["config_version"] == 2 and report["deprecation"] is None
    assert report["token_separation"] == {
        "mode": "ROLE_TOKENS_V2", "distinct_files": True, "muse": "PRIVATE_VALID",
        "status": "PRIVATE_VALID", "operator": "PRIVATE_VALID", "distinct_values": True,
        "separated": True}
    # Run from this working copy, the configured release cannot be verified.
    assert report["release"]["launch_check"] == "RELEASE_HASH_MISMATCH"
    assert "RELEASE_NOT_VERIFIED" in report["invalid_configuration_names"]
    assert report["ledger"]["marker"] == "ABSENT" and not report["ledger"]["account_ledger_marked"]
    assert "ledger.marker_file" in report["missing_configuration_names"]
    # Run from the release itself, the hash, commit and metadata agree.
    monkeypatch.setattr(managed_ops, "package_directory", lambda: package_of(config).resolve())
    root = tmp_path / "ledger"
    root.mkdir()
    config["ledger"]["marker_file"] = str(root / "LEDGER.json")
    private_file(root / "LEDGER.json", json.dumps({"role": "ACCOUNT_LEDGER"}))
    private_file(config["operator"]["token_file"], STATUS_TOKEN)
    report = preflight(config)
    release = report["release"]
    assert release["launch_check"] == "VERIFIED" and release["hash_match"]
    assert release["imported_release_commit"] == COMMIT
    assert release["imported_source_sha256"] == config["release"]["source_sha256"]
    assert report["code"]["release_commit"] == COMMIT
    assert report["ledger"]["marker"] == "ACCOUNT_LEDGER"
    assert report["token_separation"]["separated"] is False
    invalid = set(report["invalid_configuration_names"])
    assert "ROLE_TOKENS_NOT_SEPARATED" in invalid
    assert not {"RELEASE_NOT_VERIFIED", "LEDGER_ACCOUNT_MARKER_REQUIRED"} & invalid
    assert not report["ready_to_trade"] and not report["configuration_complete"]
    for secret in (TOKEN, STATUS_TOKEN, OPERATOR_TOKEN):
        assert secret not in json.dumps(report)


def test_optional_liquidity_and_monitor_policy_are_validated_as_exact_decimals(ops, tmp_path):
    path, config = ops
    key = "MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON"
    liquidity = {"policy_id": "CRYPTO_LIQUIDITY_PAPER_V1", "lookback_bars": 60,
                 "minimum_dollar_volume": "1000000", "maximum_participation": "0.01",
                 "assumed_round_trip_cost_bps": "50", "maximum_cost_to_risk": "0.25"}
    monitor = {"policy_id": "JEV_MONITOR_SCHEDULING_ENGINEERING_V1",
               "minimum_interval_seconds": 5, "near_target_progress_fraction": "0.8"}
    config["environment"][key] = json.dumps(liquidity)
    config["environment"]["MANAGED_MONITOR_TRIGGER_POLICY_JSON"] = json.dumps(monitor)
    path.write_text(json.dumps(config))
    loaded = load_private_config(path)
    result = preflight(loaded, tmp_path)
    assert key not in result["invalid_configuration_names"]
    assert "MANAGED_MONITOR_TRIGGER_POLICY_JSON" not in result["invalid_configuration_names"]
    liquidity["maximum_participation"] = "0.010000000000000000000001"
    loaded["environment"][key] = json.dumps(liquidity)
    assert key in preflight(loaded, tmp_path)["invalid_configuration_names"]
    monitor["near_target_progress_fraction"] = "1"
    loaded["environment"]["MANAGED_MONITOR_TRIGGER_POLICY_JSON"] = json.dumps(monitor)
    assert "MANAGED_MONITOR_TRIGGER_POLICY_JSON" in preflight(
        loaded, tmp_path)["invalid_configuration_names"]


def test_deploy_example_has_explicit_valid_policies_but_required_private_inputs(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    config = json.loads((checkout / "deploy/private-paper.example.json").read_text())
    private = tmp_path / "example.json"
    private.write_text(json.dumps(config))
    private.chmod(0o600)
    config = load_private_config(private)
    result = preflight(config, checkout)
    assert result["invalid_configuration_names"] == []
    assert not result["configuration_complete"] and not result["ready_to_trade"]
    assert result["config_version"] == 2
    assert {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
            "TYPESAFE_ENV_FILE", "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL",
            "MANAGED_API_TOKEN", "muse.token_file", "status.token_file", "operator.token_file",
            "release.directory", "release.source_sha256", "ledger.marker_file",
            "ledger.pg_ctl"} <= set(result["missing_configuration_names"])
    env = config["environment"]
    assert env["MANAGED_HTTP_PORT"] == "8780" and env["MANAGED_US_CLASSIFICATIONS_JSON"] == "[]"
    assert "MANAGED_API_TOKEN" not in env and config["notify"] == {}
    cycle = json.loads(env["MANAGED_CYCLE_POLICY_JSON"])
    assert cycle["review_validity_seconds"] == 1800 and cycle["max_packet_age_seconds"] == 60
    assert cycle["require_technical_evidence"] is True
    day = json.loads(env["MANAGED_CRYPTO_DAY_POLICY_JSON"])
    assert day["entry_cutoff_minutes_before_midnight"] == 10
    assert day["flatten_minutes_before_midnight"] == 5
    assert day["max_hold_seconds"] == 86400
    # Top-K activated 2026-09-27 after the real-Jev runs (artifacts/real-jev-topk-2026-09-27);
    # V2 the same day after the live news-stale check (artifacts/news-stale-check-2026-09-27).
    assert env["MANAGED_SELECTION_RULE"] == "JEV_TOP_K_SELECTION_V2"
    assert json.loads(env["MANAGED_TOPK_SELECTION_JSON"]) == {"k": 10}
    assert "MANAGED_SELECTION_QUALITY_FLOOR" not in env


def role_client(**tokens):
    repo = SimpleNamespace(require_same_database=lambda other: None)
    store = SimpleNamespace(repo=repo, outputs=lambda after, limit, exclude_kinds=(): [])
    cycle = SimpleNamespace(repo=repo, outputs=lambda cycle_id, after, limit: [],
                            claim_evidence_tasks=lambda cycle_id, **body: [])

    async def submit(payload):
        return {"status": "FIXTURE_REPORT_RECORDED"}

    # The stubs have no database: a handler that reaches one answers 500, never 401/403.
    return TestClient(create_managed_app(
        cycle, store, api_token=TOKEN, runtime_status=lambda: {"worker_state": "RUNNING"},
        report_submit=submit, **tokens), raise_server_exceptions=False)


def test_three_tokens_map_to_three_roles_and_status_cannot_post():
    client = role_client(status_token=STATUS_TOKEN, operator_token=OPERATOR_TOKEN)
    cycle, setup = uuid4(), uuid4()

    def call(method, path, token, body=None):
        return client.request(method, path, headers={"Authorization": "Bearer " + token},
                              json=body).status_code

    claim = {"claimant": "muse", "lease_seconds": 60, "limit": 1}
    # Muse: exactly the routes its HTTP client allows (reports, evidence, news, output feeds).
    muse_routes = [
        ("POST", "/api/v1/lab/research-reports", {"report_id": "fixture"}),
        ("GET", "/api/v1/lab/outputs", None),
        ("GET", f"/api/v1/lab/cycles/{cycle}/outputs", None),
        ("POST", f"/api/v1/lab/cycles/{cycle}/evidence-tasks/claim", claim),
        ("POST", f"/api/v1/lab/cycles/{cycle}/evidence", {}),
        ("GET", "/api/v1/lab/positions", None),
        ("GET", f"/api/v1/lab/positions/{setup}/news", None),
        ("POST", f"/api/v1/lab/positions/{setup}/news", {}),
    ]
    for method, path, body in muse_routes:
        assert call(method, path, TOKEN, body) not in {401, 403}, path
    assert call("POST", "/api/v1/lab/research-reports", TOKEN, {"report_id": "x"}) == 202
    for path in ("/api/v1/lab/status", "/api/v1/lab/setups", "/api/v1/lab/cycles",
                 "/api/v1/lab/results", "/api/v1/lab/analytics/daily"):
        assert call("GET", path, TOKEN) == 403
    # Status: every authenticated GET, never a POST.
    assert call("GET", "/api/v1/lab/status", STATUS_TOKEN) == 200
    assert call("GET", "/api/v1/lab/outputs", STATUS_TOKEN) == 200
    for method, path, body in muse_routes:
        if method == "POST":
            assert call(method, path, STATUS_TOKEN, body) == 403, path
    # Operator: reserved, accepted by no route yet.
    for method, path, body in muse_routes + [("GET", "/api/v1/lab/status", None)]:
        assert call(method, path, OPERATOR_TOKEN, body) == 403, path
    assert call("GET", "/api/v1/lab/status", "x" * 48) == 401
    assert client.get("/api/v1/lab/status").status_code == 401
    assert client.get("/health").status_code != 401  # Liveness stays unauthenticated.


def test_single_legacy_token_keeps_full_access_and_role_tokens_must_differ():
    client = role_client()
    headers = {"Authorization": "Bearer " + TOKEN}
    assert client.get("/api/v1/lab/status", headers=headers).status_code == 200
    assert client.post("/api/v1/lab/research-reports", headers=headers,
                       json={"report_id": "x"}).status_code == 202
    for tokens in ({"status_token": STATUS_TOKEN},
                   {"status_token": TOKEN, "operator_token": OPERATOR_TOKEN},
                   {"status_token": STATUS_TOKEN, "operator_token": STATUS_TOKEN},
                   {"status_token": "short", "operator_token": OPERATOR_TOKEN}):
        with pytest.raises(ValueError, match="SEPARATE_ROLE_TOKENS_REQUIRED"):
            role_client(**tokens)
    settings = AppSettings(8799, TOKEN, 300, (), CLASSIFICATION_POLICY, ())
    assert settings.status_token is None and settings.operator_token is None
    separated = replace(settings, status_token=STATUS_TOKEN, operator_token=OPERATOR_TOKEN)
    assert STATUS_TOKEN not in repr(separated) and OPERATOR_TOKEN not in repr(separated)
    for status, operator in ((TOKEN, OPERATOR_TOKEN), (STATUS_TOKEN, None)):
        with pytest.raises(ValueError, match="SEPARATE_ROLE_TOKENS_REQUIRED"):
            replace(settings, status_token=status, operator_token=operator)


def test_disposable_audit_export_independent_head_and_offline_evidence_restore(er, tmp_path):
    with er.connect() as conn:
        system_event(er, conn, "OPS_FIXTURE", {"source": "DISPOSABLE_TEST"})
    exported = export_checkpoint(er, tmp_path / "export")
    proof = verify_checkpoint(exported["file"], expected_head=exported["head_hash"])
    assert proof["valid"] and proof["independent_head_verified"]
    recovered = tmp_path / "recovered" / "audit.jsonl"
    restored = restore_audit_copy(exported["file"], recovered, expected_head=exported["head_hash"])
    assert restored["head_hash"] == exported["head_hash"]
    assert Path(exported["file"]).read_bytes() == recovered.read_bytes()
    assert restored["scope"] == "AUDIT_EVIDENCE_ONLY_NOT_DATABASE_BACKUP"
    with pytest.raises(ValueError):
        verify_checkpoint(recovered, expected_head="0" * 64)
    recovered.write_bytes(recovered.read_bytes()[:-3])
    with pytest.raises(ValueError, match="AUDIT_FILE_HASH_MISMATCH"):
        verify_checkpoint(recovered)


def test_preflight_schema_check_uses_only_disposable_repository(ops, er, tmp_path):
    _, config = ops
    result = preflight(config, tmp_path, repository=er)
    assert result["database_check"] == "MATCH"
    assert result["schema_actual"] == result["schema_expected"]


# --- Off-host alerts (plan 4.2): the watchdog hook. Notifier/payload/state-machine unit tests
# live in tests/test_notify.py; these cover only the wiring into watchdog_once/preflight. ---


def notify_config(config, tmp_path, **overrides):
    """The ``ops``/template config with a populated, real notify section (plan 4.2)."""
    config = copy.deepcopy(config)
    ping_url_file = tmp_path / "healthchecks-ping-url"
    private_file(ping_url_file, "https://hc-ping.com/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    config["notify"] = {
        "provider": "HEALTHCHECKS", "ping_url_file": str(ping_url_file),
        "allowed_hosts": ["hc-ping.com"], "reminder_minutes": 30, "daily_head_hour_utc": 6,
        "timeout_seconds": 5, "local_notification": False,
    }
    config["notify"].update(overrides)
    return config


def test_v2_config_accepts_populated_notify_section_and_round_trips(ops, tmp_path):
    path, config = ops
    config = notify_config(config, tmp_path)
    path.write_text(json.dumps(config))
    assert load_private_config(path)["notify"] == config["notify"]


def test_watchdog_once_pings_healthchecks_success_url_when_status_is_clean(ops, tmp_path):
    path, config = ops
    config = notify_config(config, tmp_path)
    now = datetime.now(UTC)
    heartbeat(config, now)
    posts = []

    def respond(request):
        if request.url.host == "hc-ping.com":
            posts.append(request)
            return httpx.Response(200)
        return httpx.Response(200, json=healthy(now, config["release"]["source_sha256"]))

    result = watchdog_once(config, transport=httpx.MockTransport(respond), now=now,
                           config_path=path)
    assert result["alarms"] == []
    assert len(posts) == 1
    assert posts[0].url.path == "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert json.loads(posts[0].content) == {"alarms": [], "at": now.isoformat()}
    state_file = Path(config["watchdog"]["alarm_file"]).with_suffix(".notify-state.json")
    assert json.loads(state_file.read_text())["last_alarms"] == []
    assert state_file.stat().st_mode & 0o777 == 0o600


def test_watchdog_once_forwards_new_alarm_codes_to_fail_endpoint_with_no_evidence(ops, tmp_path):
    path, config = ops
    config = notify_config(config, tmp_path)
    now = datetime.now(UTC)
    heartbeat(config, now)
    bad_status = healthy(now, config["release"]["source_sha256"])
    bad_status.update(worker_state="STOPPED")
    posts = []

    def respond(request):
        if request.url.host == "hc-ping.com":
            posts.append(request)
            return httpx.Response(200)
        return httpx.Response(200, json=bad_status)

    result = watchdog_once(config, transport=httpx.MockTransport(respond), now=now,
                           config_path=path)
    assert "WORKER_STOPPED" in result["alarms"] and "NOTIFY_FAILED" not in result["alarms"]
    assert len(posts) == 1 and posts[0].url.path.endswith("/fail")
    sent = json.loads(posts[0].content)
    assert sent["alarms"] == result["alarms"]
    assert TOKEN not in json.dumps(sent) and STATUS_TOKEN not in json.dumps(sent)


def test_watchdog_once_reports_notify_failed_without_a_halt_or_other_change(ops, tmp_path):
    path, config = ops
    config = notify_config(config, tmp_path)
    now = datetime.now(UTC)
    heartbeat(config, now)

    def respond(request):
        if request.url.host == "hc-ping.com":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=healthy(now, config["release"]["source_sha256"]))

    result = watchdog_once(config, transport=httpx.MockTransport(respond), now=now,
                           config_path=path)
    assert result["alarms"] == ["NOTIFY_FAILED"]
    assert result["mode"] == "PAPER_ONLY" and result["scope"] == "LOCAL_STATUS_WATCHDOG"


def test_watchdog_once_with_empty_notify_section_is_unchanged_and_creates_no_state_file(
    ops, tmp_path
):
    path, config = ops
    assert config["notify"] == {}
    now = datetime.now(UTC)
    heartbeat(config, now)
    result = watchdog_once(config, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=healthy(now, config["release"]["source_sha256"]))
    ), now=now, config_path=path)
    assert result["alarms"] == []
    state_file = Path(config["watchdog"]["alarm_file"]).with_suffix(".notify-state.json")
    assert not state_file.exists()


def test_watchdog_once_includes_audit_head_once_daily_via_injected_repository(ops, tmp_path, er):
    path, config = ops
    config = notify_config(config, tmp_path, daily_head_hour_utc=6)
    now = datetime(2026, 9, 24, 6, 0, 0, tzinfo=UTC)
    heartbeat(config, now)
    with er.connect() as conn:
        system_event(er, conn, "OPS_FIXTURE", {"source": "NOTIFY_DAILY_HEAD_FIXTURE"})
    head = er.export_events()[-1]
    posts = []

    def respond(request):
        if request.url.host == "hc-ping.com":
            posts.append(request)
            return httpx.Response(200)
        return httpx.Response(200, json=healthy(now, config["release"]["source_sha256"]))

    watchdog_once(config, transport=httpx.MockTransport(respond), now=now, config_path=path,
                 repository=er)
    sent = json.loads(posts[0].content)
    assert sent["audit_seq"] == head["seq"] and sent["audit_head_hash"] == head["event_hash"]


def test_preflight_reports_notify_not_configured_configured_and_invalid(ops, tmp_path):
    path, config = ops
    assert preflight(config)["notify"] == {"state": "NOT_CONFIGURED", "configured": False}
    good = notify_config(config, tmp_path)
    report = preflight(good)["notify"]
    assert report == {"state": "CONFIGURED", "configured": True, "provider": "HEALTHCHECKS",
                      "local_notification": False}
    broken = notify_config(config, tmp_path)
    broken["notify"]["allowed_hosts"] = ["some-other-host.example"]
    result = preflight(broken)
    assert result["notify"] == {"state": "INVALID", "configured": True}
    assert "NOTIFY_CONFIGURATION_INVALID" in result["invalid_configuration_names"]


def test_ticks_a_few_seconds_after_the_watchdog_clock_are_fresh():
    """The watchdog takes ``now`` before its status read while the trader's loops tick every
    second, on Railway on another host's clock: a tick up to CLOCK_SKEW_TOLERANCE_SECONDS after
    ``now`` is fresh (the first Railway run raised these alarms on about four reads in ten)."""
    from catalyst_lab.managed_ops import CLOCK_SKEW_TOLERANCE_SECONDS

    now = datetime(2026, 9, 28, 5, 35, 5, tzinfo=UTC)
    policy = {"tick_max_age_seconds": 15, "reconciliation_max_age_seconds": 90,
              "research_max_age_seconds": 180}
    stale = {"PROTECTION_TICK_STALE", "RECONCILIATION_STALE", "RESEARCH_TICK_STALE"}

    def alarms(offset_seconds):
        at = (now + timedelta(seconds=offset_seconds)).isoformat()
        return stale & set(status_alarms({"worker_state": "RUNNING", "last_protection_tick": at,
                                          "last_reconciliation_at": at, "last_cycle_at": at},
                                         now, policy))

    assert alarms(0.4) == set()  # Ticked while the status was being built.
    assert alarms(CLOCK_SKEW_TOLERANCE_SECONDS) == set()
    assert alarms(-14) == set()
    assert alarms(CLOCK_SKEW_TOLERANCE_SECONDS + 1) == stale  # Far ahead: never trusted.
    assert alarms(-16) == {"PROTECTION_TICK_STALE"}
    assert alarms(-181) == stale
