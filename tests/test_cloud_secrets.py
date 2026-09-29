"""The owner's secret generator against a fake ``railway`` CLI (package cloud).

The fake records every argv and stores what arrives on stdin; nothing reaches Railway.
"""

import argparse
import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud_secrets.py"
SPEC = importlib.util.spec_from_file_location("cloud_secrets_fixture", SCRIPT)
cloud_secrets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud_secrets)

FAKE = """#!{python}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_RAILWAY_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
path = os.environ["FAKE_RAILWAY_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {{}}
service = argv[argv.index("--service") + 1]
if argv[:2] == ["variable", "list"] and "--json" in argv:
    print(json.dumps(state.get(service, {{}})))
elif argv[:2] == ["variable", "set"] and "--stdin" in argv and "--skip-deploys" in argv:
    state.setdefault(service, {{}})[argv[2]] = sys.stdin.read()
    json.dump(state, open(path, "w"))
    print("Set variables " + argv[2])
else:
    sys.exit(3)
"""


@pytest.fixture
def railway(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "railway"
    fake.parent.mkdir()
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o700)
    state, log = tmp_path / "railway-state.json", tmp_path / "railway-argv.log"
    monkeypatch.setenv("FAKE_RAILWAY_STATE", str(state))
    monkeypatch.setenv("FAKE_RAILWAY_LOG", str(log))
    return fake, state, log


@pytest.fixture
def private_dir(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory


def args(railway, agent_file, *rotate, dry_run=False):
    return argparse.Namespace(agent_token_file=str(agent_file), agent_id="muse",
                              rotate=list(rotate) or None, environment=None,
                              railway=str(railway[0]), dry_run=dry_run)


def test_the_agent_token_is_muses_by_default(monkeypatch):
    # Muse is the one research agent and the only outside caller: no flag is needed, and the
    # ID is exactly the one the identity screens match a report's agent against.
    seen = {}
    monkeypatch.setattr(cloud_secrets, "run", lambda parsed, **_: seen.update(args=parsed))
    cloud_secrets.main(["--agent-token-file", "/abs/private/muse-agent-token"])
    assert seen["args"].agent_id == "muse"


def run(railway, agent_file, tmp_path, *rotate, dry_run=False):
    out = io.StringIO()
    report = cloud_secrets.run(args(railway, agent_file, *rotate, dry_run=dry_run),
                               home=tmp_path / "home", out=out)
    return report, out.getvalue()


def stored(railway):
    return json.loads(railway[1].read_text())


def test_first_run_generates_every_secret_into_railway_without_printing_any(
        railway, private_dir, tmp_path, capsys):
    agent_file = private_dir / "muse-agent-token"
    report, printed = run(railway, agent_file, tmp_path)
    assert {v["action"] for v in report["secrets"].values()} == {"GENERATE"}
    state = stored(railway)
    assert set(state) == {"trader", "ops"}
    assert set(state["trader"]) == {"RISK_DATABASE_PASSWORD", "JEV_DATABASE_PASSWORD",
                                    "MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN",
                                    "MANAGED_OPERATOR_TOKEN", "MANAGED_AGENT_TOKENS_JSON"}
    assert set(state["ops"]) == {"APP_DATABASE_PASSWORD", "BACKUP_DATABASE_PASSWORD",
                                 "OPERATOR_DATABASE_PASSWORD", "PUBLIC_DATABASE_PASSWORD",
                                 "MANAGED_STATUS_TOKEN"}
    assert state["trader"]["MANAGED_STATUS_TOKEN"] == state["ops"]["MANAGED_STATUS_TOKEN"]
    values = [v for service in state.values() for k, v in service.items()
              if k != "MANAGED_AGENT_TOKENS_JSON"]
    assert len(set(values)) == len(values) - 1  # Only the shared status token repeats.
    assert all(len(v) >= 64 and v.replace("-", "").replace("_", "").isalnum() for v in values)
    agent = json.loads(state["trader"]["MANAGED_AGENT_TOKENS_JSON"])
    assert list(agent) == ["muse"]
    assert agent_file.read_text() == agent["muse"] + "\n"
    assert stat.S_IMODE(agent_file.stat().st_mode) == 0o600
    argv_log = railway[2].read_text()
    captured = capsys.readouterr()
    for value in [*values, agent["muse"]]:
        assert value not in argv_log and value not in printed
        assert value not in captured.out and value not in captured.err


def test_second_run_skips_everything_and_changes_nothing(railway, private_dir, tmp_path):
    agent_file = private_dir / "muse-agent-token"
    run(railway, agent_file, tmp_path)
    before, token = stored(railway), agent_file.read_text()
    report, _ = run(railway, agent_file, tmp_path)
    assert {v["action"] for v in report["secrets"].values()} == {"SKIP"}
    assert stored(railway) == before and agent_file.read_text() == token


def test_rotation_replaces_one_secret_everywhere_it_lives(railway, private_dir, tmp_path):
    agent_file = private_dir / "muse-agent-token"
    run(railway, agent_file, tmp_path)
    before = stored(railway)
    report, _ = run(railway, agent_file, tmp_path, "MANAGED_STATUS_TOKEN",
                    "MANAGED_AGENT_TOKENS_JSON")
    after = stored(railway)
    assert report["secrets"]["MANAGED_STATUS_TOKEN"]["action"] == "ROTATE"
    assert after["trader"]["MANAGED_STATUS_TOKEN"] == after["ops"]["MANAGED_STATUS_TOKEN"]
    assert after["trader"]["MANAGED_STATUS_TOKEN"] != before["trader"]["MANAGED_STATUS_TOKEN"]
    assert after["trader"]["RISK_DATABASE_PASSWORD"] == before["trader"][
        "RISK_DATABASE_PASSWORD"]
    agent = json.loads(after["trader"]["MANAGED_AGENT_TOKENS_JSON"])
    assert agent_file.read_text() == agent["muse"] + "\n"
    assert stat.S_IMODE(agent_file.stat().st_mode) == 0o600
    assert sorted(p.name for p in private_dir.iterdir()) == ["muse-agent-token"]


def test_a_partially_set_secret_is_refused_before_anything_changes(railway, private_dir,
                                                                   tmp_path):
    railway[1].write_text(json.dumps({"trader": {"MANAGED_STATUS_TOKEN": "x" * 64}}))
    with pytest.raises(cloud_secrets.Refused, match="SECRET_PARTIALLY_SET MANAGED_STATUS_TOKEN"):
        run(railway, private_dir / "muse-agent-token", tmp_path)
    assert stored(railway) == {"trader": {"MANAGED_STATUS_TOKEN": "x" * 64}}
    assert not (private_dir / "muse-agent-token").exists()
    # The agent token counts its local file too: a missing file is a partial secret.
    agent_file = private_dir / "muse-agent-token"
    railway[1].write_text("{}")
    run(railway, agent_file, tmp_path)
    agent_file.unlink()
    with pytest.raises(cloud_secrets.Refused, match="MANAGED_AGENT_TOKENS_JSON"):
        run(railway, agent_file, tmp_path)
    with pytest.raises(cloud_secrets.Refused, match="ROTATE_NAME_UNKNOWN APCA_API_KEY_ID"):
        run(railway, agent_file, tmp_path, "APCA_API_KEY_ID")


def test_dry_run_reads_names_only(railway, private_dir, tmp_path):
    report, _ = run(railway, private_dir / "muse-agent-token", tmp_path, dry_run=True)
    assert report["dry_run"] and not railway[1].exists()
    assert all(json.loads(line)[:2] == ["variable", "list"]
               for line in railway[2].read_text().splitlines())


def test_the_agent_file_stays_outside_the_repo_documents_and_icloud(railway, tmp_path,
                                                                     private_dir):
    home = tmp_path / "home"
    for place in (cloud_secrets.REPOSITORY / "agent-token",
                  home / "Documents" / "agent-token",
                  home / "Library" / "Mobile Documents" / "x" / "agent-token"):
        with pytest.raises(cloud_secrets.Refused, match="AGENT_TOKEN_FILE_LOCATION_FORBIDDEN"):
            cloud_secrets.agent_file_path(str(place), home=home)
    with pytest.raises(cloud_secrets.Refused, match="AGENT_TOKEN_FILE_MUST_BE_ABSOLUTE"):
        cloud_secrets.agent_file_path("relative/token", home=home)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(cloud_secrets.Refused, match="AGENT_TOKEN_FILE_DIRECTORY_NOT_PRIVATE"):
        cloud_secrets.agent_file_path(str(shared / "token"), home=home)
    readable = private_dir / "readable"
    readable.write_text("x")
    readable.chmod(0o644)
    with pytest.raises(cloud_secrets.Refused, match="AGENT_TOKEN_FILE_NOT_PRIVATE"):
        cloud_secrets.agent_file_state(readable)


def test_the_catalog_matches_the_railway_spec_and_the_provisioner():
    from catalyst_lab.cloud_provision import LOGIN_ROLES

    passwords = {s.name for s in cloud_secrets.CATALOG if s.name.endswith("_DATABASE_PASSWORD")}
    assert passwords == set(LOGIN_ROLES.values())
    spec = (Path(__file__).resolve().parents[1] / ".railway" / "railway.ts").read_text()
    for secret in cloud_secrets.CATALOG:
        for service, name in secret.targets:
            assert f"{name}: preserve()" in spec, (service, name)
    assert os.environ.get("RAILWAY_TOKEN") is None  # Never needed by tests.


def test_a_refused_set_shows_the_cli_reason_but_never_the_value():
    """First cloud run (2026-09-28): a bare exit code could not be diagnosed. The CLI's words
    are shown; any line holding the value, or an 8-character piece of it, is withheld."""
    value = "Zq3-abcdefghijklmnopqrstuvwxyz0123456789_ABCDEFGHIJKLMNOPQRSTUVW"
    result = argparse.Namespace(
        returncode=1, stdout="",
        stderr="Error: variable rejected by the API (400)\n"
               f"received value {value}\n"
               f"prefix {value[5:13]} only\n"
               "warning: Config as Code (railway.json / railway.toml) is deprecated.\n")
    reason = cloud_secrets.cli_reason(result, value)
    assert "Error: variable rejected by the API (400)" in reason
    assert value not in reason and value[5:13] not in reason
    assert reason.count("<a line withheld: it may contain the value>") == 2
    assert "Config as Code" not in reason
    assert cloud_secrets.cli_reason(argparse.Namespace(returncode=1, stdout="", stderr=""),
                                    value) == ""
