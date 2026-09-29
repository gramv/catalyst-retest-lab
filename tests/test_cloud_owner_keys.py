"""The owner's key copier against a fake ``railway`` CLI (fixture keys only; nothing reaches
Railway, Alpaca or TypeSafe)."""

import argparse
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud_owner_keys.py"
SPEC = importlib.util.spec_from_file_location("cloud_owner_keys_fixture", SCRIPT)
owner_keys = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_keys)

PAPER_ID = "PKFIXTUREPAPERKEY0001"
PAPER_SECRET = "fixture-paper-secret-0123456789abcdefghijkl"
TYPESAFE = "ts-fixture-key-0123456789abcdefghijklmnop"

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


def private_config(tmp_path, *, key_id=PAPER_ID, secret=PAPER_SECRET, typesafe=TYPESAFE):
    typesafe_file = tmp_path / "typesafe.env"
    typesafe_file.write_text(f"TYPESAFE_API_KEY={typesafe}\n")
    config = tmp_path / "private.json"
    environment = {"TYPESAFE_ENV_FILE": str(typesafe_file), "CATALYST_ENVIRONMENT": "local"}
    if key_id is not None:
        environment["APCA_API_KEY_ID"] = key_id
    if secret is not None:
        environment["APCA_API_SECRET_KEY"] = secret
    config.write_text(json.dumps({"config_version": 2, "environment": environment}))
    return config


def args(railway, config, env_file, *, replace=(), dry_run=False):
    return argparse.Namespace(config=str(config), env_file=str(env_file), railway=str(railway[0]),
                              replace=list(replace) or None, dry_run=dry_run)


def sent(railway):
    state = railway[1]
    return json.loads(state.read_text()).get("trader", {}) if state.exists() else {}


def test_the_three_keys_reach_the_trader_on_stdin_and_are_never_printed(railway, tmp_path):
    out = io.StringIO()
    config = private_config(tmp_path)
    report = owner_keys.run(args(railway, config, tmp_path / "missing.env"), out=out)
    assert sent(railway) == {"APCA_API_KEY_ID": PAPER_ID, "APCA_API_SECRET_KEY": PAPER_SECRET,
                             "TYPESAFE_API_KEY": TYPESAFE}
    assert {k: v["action"] for k, v in report["keys"].items()} == dict.fromkeys(
        owner_keys.KEYS, "SET")
    assert report["keys"]["TYPESAFE_API_KEY"]["source"] == str(tmp_path / "typesafe.env")
    printed = out.getvalue() + railway[2].read_text()  # The output and every argv.
    for value in (PAPER_ID, PAPER_SECRET, TYPESAFE):
        assert value not in printed


def test_missing_keys_fall_back_to_the_env_file(railway, tmp_path):
    config = private_config(tmp_path, key_id=None, secret=None)
    env_file = tmp_path / ".env"
    env_file.write_text(f"# local\nexport APCA_API_KEY_ID=\"{PAPER_ID}\"\n"
                        f"APCA_API_SECRET_KEY='{PAPER_SECRET}'\nOTHER=1\n")
    report = owner_keys.run(args(railway, config, env_file), out=io.StringIO())
    assert sent(railway)["APCA_API_KEY_ID"] == PAPER_ID
    assert sent(railway)["APCA_API_SECRET_KEY"] == PAPER_SECRET
    assert report["keys"]["APCA_API_KEY_ID"]["source"] == str(env_file)


@pytest.mark.parametrize("change, code", [
    ({"key_id": None}, "KEY_NOT_FOUND APCA_API_KEY_ID"),
    ({"key_id": "REQUIRED_PAPER_ALPACA_KEY_ID"}, "KEY_IS_A_PLACEHOLDER APCA_API_KEY_ID"),
    ({"key_id": "AKLIVEKEYNOTPAPER0001"}, "ALPACA_KEY_NOT_PAPER APCA_API_KEY_ID"),
    ({"secret": "has a space"}, "KEY_FORMAT_INVALID APCA_API_SECRET_KEY"),
    ({"typesafe": ""}, "KEY_FORMAT_INVALID TYPESAFE_API_KEY"),
])
def test_a_bad_or_missing_key_refuses_before_anything_is_sent(railway, tmp_path, change, code):
    config = private_config(tmp_path, **change)
    with pytest.raises(owner_keys.Refused, match=f"^{code}$"):
        owner_keys.run(args(railway, config, tmp_path / "missing.env"), out=io.StringIO())
    assert sent(railway) == {}


def test_existing_keys_are_skipped_unless_replaced_and_dry_run_sends_nothing(railway, tmp_path):
    config = private_config(tmp_path)
    dry = owner_keys.run(args(railway, config, tmp_path / "x.env", dry_run=True),
                         out=io.StringIO())
    assert sent(railway) == {} and dry["keys"]["APCA_API_KEY_ID"]["action"] == "SET"
    owner_keys.run(args(railway, config, tmp_path / "x.env"), out=io.StringIO())
    again = owner_keys.run(args(railway, config, tmp_path / "x.env"), out=io.StringIO())
    assert {v["action"] for v in again["keys"].values()} == {"SKIP"}
    replaced = owner_keys.run(args(railway, config, tmp_path / "x.env",
                                   replace=["TYPESAFE_API_KEY"]), out=io.StringIO())
    assert replaced["keys"]["TYPESAFE_API_KEY"]["action"] == "REPLACE"
    with pytest.raises(owner_keys.Refused, match="^REPLACE_NAME_UNKNOWN MANAGED_API_TOKEN$"):
        owner_keys.run(args(railway, config, tmp_path / "x.env",
                            replace=["MANAGED_API_TOKEN"]), out=io.StringIO())
