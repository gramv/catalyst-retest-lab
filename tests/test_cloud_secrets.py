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


# --- Adding a research agent without replacing another (package agent-api) --------------------

NEW_AGENT = "dots-agent"


def add_args(railway, agent_file, token_file, *keep, add=NEW_AGENT, dry_run=False, rotate=None,
             agent_id="muse"):
    return argparse.Namespace(agent_token_file=str(agent_file), agent_id=agent_id,
                              rotate=rotate, environment=None, railway=str(railway[0]),
                              dry_run=dry_run, add_agent=add,
                              token_file=None if token_file is None else str(token_file),
                              keep_agent=[f"{a}={p}" for a, p in keep] or None)


def add(railway, tmp_path, *args, **kwargs):
    out = io.StringIO()
    report = cloud_secrets.run(add_args(railway, *args, **kwargs), home=tmp_path / "home",
                               out=out)
    return report, out.getvalue()


def deployed(railway, private_dir, tmp_path):
    """The first deployment's secrets: Muse's token in Railway and in its local file."""
    muse_file = private_dir / "muse-agent-token"
    run(railway, muse_file, tmp_path)
    return muse_file


def agent_tokens(railway):
    return json.loads(stored(railway)["trader"]["MANAGED_AGENT_TOKENS_JSON"])


def token_of(path):
    return path.read_text().removesuffix("\n")


def test_adding_an_agent_writes_the_union_and_keeps_muses_token(railway, private_dir, tmp_path,
                                                                 capsys):
    from catalyst_lab.agent_identity import validated_agent_tokens

    muse_file = deployed(railway, private_dir, tmp_path)
    muse_token, before = token_of(muse_file), stored(railway)
    new_file = private_dir / "dots-agent-token"
    report, printed = add(railway, tmp_path, muse_file, new_file)
    tokens = agent_tokens(railway)
    assert sorted(tokens) == ["dots-agent", "muse"]
    assert tokens["muse"] == muse_token and tokens["dots-agent"] == token_of(new_file)
    assert token_of(muse_file) == muse_token  # Muse's own file is never touched.
    assert stat.S_IMODE(new_file.stat().st_mode) == 0o600
    assert validated_agent_tokens(tokens) == tokens  # The app's own rules accept the union.
    # Only the agent variable changed; every other secret is as the first run left it.
    after = stored(railway)
    assert {k: v for k, v in after["trader"].items() if k != "MANAGED_AGENT_TOKENS_JSON"} == {
        k: v for k, v in before["trader"].items() if k != "MANAGED_AGENT_TOKENS_JSON"}
    assert after["ops"] == before["ops"]
    assert (report["applied"], report["new_agent_token"], report["agents"]) == (
        True, "GENERATE", ["dots-agent", "muse"])
    # The summary (IDs only) was printed before the change, then the applied report.
    first, _, second = printed.partition("}\n{")
    assert '"applied": false' in first and '"applied": true' in second
    argv_log = railway[2].read_text()
    sets = [json.loads(line) for line in argv_log.splitlines()
            if json.loads(line)[:2] == ["variable", "set"]]
    assert sets[-1][:3] == ["variable", "set", "MANAGED_AGENT_TOKENS_JSON"]
    captured = capsys.readouterr()
    for token in tokens.values():
        assert token not in printed and token not in argv_log
        assert token not in captured.out and token not in captured.err


def test_the_dry_run_shows_agent_ids_only_and_changes_nothing(railway, private_dir, tmp_path):
    muse_file = deployed(railway, private_dir, tmp_path)
    before, log_lines = stored(railway), len(railway[2].read_text().splitlines())
    new_file = private_dir / "dots-agent-token"
    report, printed = add(railway, tmp_path, muse_file, new_file, dry_run=True)
    assert report == {
        "mode": "ADD_AGENT", "dry_run": True, "applied": False, "service": "trader",
        "variable": "MANAGED_AGENT_TOKENS_JSON", "agents": ["dots-agent", "muse"],
        "new_agent": "dots-agent", "new_agent_token": "GENERATE",
        "new_agent_token_file": str(new_file)}
    assert json.loads(printed) == report
    assert not new_file.exists() and stored(railway) == before
    new_lines = railway[2].read_text().splitlines()[log_lines:]
    assert new_lines and all(json.loads(line)[:2] == ["variable", "list"] for line in new_lines)
    assert token_of(muse_file) not in printed


def test_an_existing_private_token_file_is_reused_and_a_rerun_is_idempotent(
        railway, private_dir, tmp_path):
    muse_file = deployed(railway, private_dir, tmp_path)
    new_file = private_dir / "dots-agent-token"
    fixture_token = "fixture-dots-agent-token-" + "d" * 40
    new_file.write_text(fixture_token + "\n")
    new_file.chmod(0o600)
    report, _ = add(railway, tmp_path, muse_file, new_file)
    assert report["new_agent_token"] == "REUSE_LOCAL_FILE"
    assert agent_tokens(railway)["dots-agent"] == fixture_token == token_of(new_file)
    value = stored(railway)["trader"]["MANAGED_AGENT_TOKENS_JSON"]
    again, _ = add(railway, tmp_path, muse_file, new_file)  # The same union again.
    assert again["new_agent_token"] == "REUSE_LOCAL_FILE"
    assert stored(railway)["trader"]["MANAGED_AGENT_TOKENS_JSON"] == value
    # A third agent keeps both others when both are listed.
    third = private_dir / "third-agent-token"
    add(railway, tmp_path, muse_file, third, ("dots-agent", new_file), add="third-agent")
    tokens = agent_tokens(railway)
    assert sorted(tokens) == ["dots-agent", "muse", "third-agent"]
    assert tokens["dots-agent"] == fixture_token and tokens["muse"] == token_of(muse_file)


def test_rotating_muses_token_keeps_the_agents_listed(railway, private_dir, tmp_path):
    muse_file = deployed(railway, private_dir, tmp_path)
    new_file = private_dir / "dots-agent-token"
    add(railway, tmp_path, muse_file, new_file)
    dots, old_muse = token_of(new_file), token_of(muse_file)
    out = io.StringIO()
    rotation = argparse.Namespace(**{**vars(args(railway, muse_file, "MANAGED_AGENT_TOKENS_JSON")),
                                     "keep_agent": [f"dots-agent={new_file}"]})
    report = cloud_secrets.run(rotation, home=tmp_path / "home", out=out)
    tokens = agent_tokens(railway)
    assert report["secrets"]["MANAGED_AGENT_TOKENS_JSON"] == {
        "action": "ROTATE", "services": ["trader"], "agents": ["dots-agent", "muse"]}
    assert tokens["dots-agent"] == dots and tokens["muse"] == token_of(muse_file) != old_muse
    assert dots not in out.getvalue() and tokens["muse"] not in out.getvalue()
    # Listing agents is refused when the agent variable is not being rotated.
    status_only = argparse.Namespace(**{**vars(args(railway, muse_file, "MANAGED_STATUS_TOKEN")),
                                        "keep_agent": [f"dots-agent={new_file}"]})
    with pytest.raises(cloud_secrets.Refused, match="^KEEP_AGENT_ONLY_WITH_ADD_AGENT_OR_ROTATE"):
        cloud_secrets.run(status_only, home=tmp_path / "home", out=io.StringIO())


def test_invalid_ids_duplicates_and_missing_files_are_refused_before_any_change(
        railway, private_dir, tmp_path):
    muse_file = deployed(railway, private_dir, tmp_path)
    new_file = private_dir / "dots-agent-token"
    shared = private_dir / "shared-token"
    shared.write_text(token_of(muse_file) + "\n")  # The same token under another agent.
    shared.chmod(0o600)
    readable = private_dir / "readable-token"
    readable.write_text("fixture-readable-agent-token-" + "r" * 40 + "\n")
    readable.chmod(0o644)
    short = private_dir / "short-token"
    short.write_text("too-short\n")
    short.chmod(0o600)
    before, log = stored(railway), railway[2].read_text()
    refusals = {
        "AGENT_ID_INVALID": [dict(add=bad) for bad in ("Dots", "d", "1dots", "dots agent",
                                                       "x" * 33, "dots/agent")]
        + [dict(agent_id="Muse")],
        "AGENT_ALREADY_LISTED muse": [dict(add="muse")],
        "AGENT_ID_DUPLICATE other": [dict(keep=(("other", shared), ("other", readable)))],
        "AGENT_TOKEN_FILE_MISSING other": [dict(keep=(("other", private_dir / "absent"),))],
        "AGENT_TOKEN_FILE_NOT_PRIVATE": [dict(keep=(("other", readable),)),
                                         dict(token_file=readable)],
        "AGENT_TOKEN_FILE_INVALID other": [dict(keep=(("other", short),))],
        "AGENT_TOKENS_NOT_DISTINCT": [dict(keep=(("other", shared),)), dict(token_file=shared)],
        "AGENT_TOKEN_FILE_SHARED": [dict(token_file=muse_file)],
        "TOKEN_FILE_REQUIRED": [dict(token_file=None)],
        "ADD_AGENT_WITH_ROTATE_REFUSED": [dict(rotate=["MANAGED_AGENT_TOKENS_JSON"])],
        "KEEP_AGENT_INVALID": [dict(raw_keep=["dots"]), dict(raw_keep=["Bad=/x"])],
        "AGENT_TOKEN_FILE_MUST_BE_ABSOLUTE": [dict(token_file="relative/token")],
        "AGENT_TOKEN_FILE_LOCATION_FORBIDDEN": [
            dict(token_file=cloud_secrets.REPOSITORY / "agent-token")],
    }
    for code, cases in refusals.items():
        for case in cases:
            keep = case.pop("keep", ())
            raw_keep = case.pop("raw_keep", None)
            token_file = case.pop("token_file", new_file)
            parsed = add_args(railway, muse_file, token_file, *keep, **case)
            if raw_keep is not None:
                parsed.keep_agent = raw_keep
            with pytest.raises(cloud_secrets.Refused) as refused:
                cloud_secrets.run(parsed, home=tmp_path / "home", out=io.StringIO())
            assert str(refused.value).startswith(code), (code, str(refused.value))
    assert not new_file.exists() and stored(railway) == before
    assert railway[2].read_text() == log  # Refused before Railway was even asked.
    # Without the first deployment's variable there is nothing to add to.
    railway[1].write_text("{}")
    with pytest.raises(cloud_secrets.Refused,
                       match="^AGENT_TOKENS_VARIABLE_MISSING trader MANAGED_AGENT_TOKENS_JSON"):
        add(railway, tmp_path, muse_file, new_file)
    assert not new_file.exists() and stored(railway) == {}
    with pytest.raises(cloud_secrets.Refused, match="^TOKEN_FILE_ONLY_WITH_ADD_AGENT$"):
        cloud_secrets.run(add_args(railway, muse_file, new_file, add=None),
                          home=tmp_path / "home", out=io.StringIO())


FAILING_SET = """#!{python}
import json, os, sys
argv = sys.argv[1:]
state = json.load(open(os.environ["FAKE_RAILWAY_STATE"]))
service = argv[argv.index("--service") + 1]
if argv[:2] == ["variable", "list"]:
    print(json.dumps(state.get(service, {{}})))
else:
    value = sys.stdin.read()
    print("Error: refused " + value, file=sys.stderr)
    print("the value begins " + value[2:12], file=sys.stderr)
    sys.exit(1)
"""


def test_a_failed_set_names_no_token_and_the_rerun_reuses_the_new_file(railway, private_dir,
                                                                        tmp_path, capsys):
    muse_file = deployed(railway, private_dir, tmp_path)
    failing = tmp_path / "bin" / "railway-failing"
    failing.write_text(FAILING_SET.format(python=sys.executable))
    failing.chmod(0o700)
    new_file = private_dir / "dots-agent-token"
    parsed = add_args(railway, muse_file, new_file)
    parsed.railway = str(failing)
    with pytest.raises(cloud_secrets.Refused) as refused:
        cloud_secrets.run(parsed, home=tmp_path / "home", out=io.StringIO())
    message = str(refused.value)
    assert message.startswith("RAILWAY_VARIABLE_SET_FAILED trader MANAGED_AGENT_TOKENS_JSON")
    new_token, muse_token = token_of(new_file), token_of(muse_file)
    captured = capsys.readouterr()
    for token in (new_token, muse_token):
        for piece in (token, token[:8], token[-8:]):
            assert piece not in message and piece not in captured.out + captured.err
    assert agent_tokens(railway) == {"muse": muse_token}  # Unchanged on Railway.
    report, _ = add(railway, tmp_path, muse_file, new_file)  # The working CLI: reuse the file.
    assert report["new_agent_token"] == "REUSE_LOCAL_FILE"
    assert agent_tokens(railway) == {"muse": muse_token, "dots-agent": new_token}


def test_the_script_keeps_agent_identitys_rules():
    from catalyst_lab.agent_identity import (
        AGENT_ID_PATTERN,
        MAX_AGENT_TOKENS,
        valid_token,
    )

    assert cloud_secrets.MAX_AGENTS == MAX_AGENT_TOKENS
    assert "^" + cloud_secrets.AGENT_ID.pattern + "$" == AGENT_ID_PATTERN
    for value in ("x" * 31, "x" * 32, "x" * 31 + " ", "a\tb" * 20, "fixture-" * 5, "", None, 7):
        assert cloud_secrets.valid_token(value) == valid_token(value), value


def test_main_parses_the_add_agent_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(cloud_secrets, "run", lambda parsed, **_: seen.update(args=parsed))
    cloud_secrets.main(["--agent-token-file", "/abs/private/muse-agent-token",
                        "--add-agent", "dots-agent", "--token-file", "/abs/private/dots",
                        "--keep-agent", "other=/abs/private/other", "--dry-run"])
    parsed = seen["args"]
    assert (parsed.add_agent, parsed.token_file, parsed.keep_agent, parsed.agent_id,
            parsed.dry_run) == ("dots-agent", "/abs/private/dots",
                                ["other=/abs/private/other"], "muse", True)
