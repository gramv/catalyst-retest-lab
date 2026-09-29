"""scripts/set_private_secret.py against private configurations under pytest's temporary dir.

No provider, broker, service or owner directory: every file is created under ``tmp_path``, the
prompt is injected, and the only credential loader exercised is ``jev_secrets`` reading the file
the script wrote.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from catalyst_lab import jev_secrets
from catalyst_lab.managed_ops import config_template, load_private_config, role_token
from tests.test_managed_ops import v1_config, write_config

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set_private_secret.py"
SPEC = importlib.util.spec_from_file_location("set_private_secret_fixture", SCRIPT)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)

KEY_ID = "PKFIXTURE000000000001"
SECRET = "fixture-alpaca-secret-never-display"
TYPESAFE = "fixture-typesafe-key-not-real-0123456789"
VALUES = (KEY_ID, SECRET, TYPESAFE)


@pytest.fixture(autouse=True)
def restore_umask():
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


@pytest.fixture
def config(tmp_path):
    cfg = config_template(tmp_path)
    cfg["environment"]["TYPESAFE_ENV_FILE"] = str(tmp_path / "secrets" / "typesafe.env")
    return write_config(tmp_path / "private.json", cfg), cfg


def answers(*values):
    supplied = iter(values)
    return lambda label: next(supplied)


def run(path, *args, prompt=None):
    return script.main(["--config", str(path), *args], prompt=prompt or answers())


def output(capsys):
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert not any(value in text for value in VALUES)
    return json.loads(captured.out.strip().splitlines()[-1])


def mode(path):
    return stat.S_IMODE(Path(path).lstat().st_mode)


def test_alpaca_keys_are_written_into_the_config_and_never_printed(config, capsys):
    path, cfg = config
    assert run(path, "alpaca", prompt=answers(KEY_ID, SECRET)) == 0
    assert output(capsys) == {"secret": "alpaca", "written": str(path), "replaced": False}
    loaded = load_private_config(path)
    assert mode(path) == 0o600
    env = loaded["environment"]
    assert (env["APCA_API_KEY_ID"], env["APCA_API_SECRET_KEY"]) == (KEY_ID, SECRET)
    untouched = {k: v for k, v in env.items() if not k.startswith("APCA_")}
    assert untouched == {k: v for k, v in cfg["environment"].items() if not k.startswith("APCA_")}
    assert {k: v for k, v in loaded.items() if k != "environment"} == {
        k: v for k, v in cfg.items() if k != "environment"
    }
    written = path.read_bytes()
    assert run(path, "alpaca", prompt=answers("PKFIXTURE000000000002", SECRET)) == 2
    assert output(capsys) == {"refused": "SECRET_ALREADY_PRESENT_USE_REPLACE"}
    assert path.read_bytes() == written
    assert run(path, "alpaca", "--replace", prompt=answers("PKFIXTURE000000000002", SECRET)) == 0
    assert output(capsys)["replaced"] is True
    assert load_private_config(path)["environment"]["APCA_API_KEY_ID"] == "PKFIXTURE000000000002"
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]  # No temp files left.


@pytest.mark.parametrize(
    "key_id,secret,refusal",
    [
        ("AKNOTAPAPERKEY000001", SECRET, "ALPACA_PAPER_KEY_SHAPE_REJECTED"),
        (KEY_ID, "has a space", "SECRET_MUST_BE_NON_EMPTY_WITHOUT_WHITESPACE"),
        (KEY_ID, "", "SECRET_MUST_BE_NON_EMPTY_WITHOUT_WHITESPACE"),
        (KEY_ID, "non-ascii-é", "SECRET_MUST_BE_PRINTABLE_ASCII"),
    ],
)
def test_alpaca_refusals_leave_the_config_byte_identical(config, capsys, key_id, secret, refusal):
    path, _ = config
    before = path.read_bytes()
    assert run(path, "alpaca", prompt=answers(key_id, secret)) == 2
    assert output(capsys) == {"refused": refusal}
    assert path.read_bytes() == before and mode(path) == 0o600


def test_typesafe_file_is_exactly_what_jev_secrets_reads(config, capsys, monkeypatch):
    path, cfg = config
    target = Path(cfg["environment"]["TYPESAFE_ENV_FILE"])
    assert run(path, "typesafe", prompt=answers(TYPESAFE)) == 0
    assert output(capsys) == {"secret": "typesafe", "written": str(target), "replaced": False}
    assert mode(target) == 0o600 and mode(target.parent) == 0o700
    assert target.read_text() == f"TYPESAFE_API_KEY={TYPESAFE}\n"
    for name in ("CATALYST_ENVIRONMENT", "RAILWAY_ENVIRONMENT_ID", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TYPESAFE_ENV_FILE", str(target))
    assert jev_secrets.typesafe_key() == TYPESAFE
    assert run(path, "typesafe", prompt=answers("fixture-typesafe-key-rotated-0123")) == 2
    assert output(capsys) == {"refused": "SECRET_ALREADY_PRESENT_USE_REPLACE"}
    assert jev_secrets.typesafe_key() == TYPESAFE
    rotated = "fixture-typesafe-key-rotated-0123"
    assert run(path, "typesafe", "--replace", prompt=answers(rotated)) == 0
    assert output(capsys)["replaced"] is True
    assert jev_secrets.typesafe_key() == rotated and mode(target) == 0o600


def test_typesafe_needs_the_configured_absolute_file_and_refuses_symlinks(config, capsys,
                                                                          tmp_path):
    path, cfg = config
    for value in (None, "REQUIRED", "relative/typesafe.env"):
        env = {k: v for k, v in cfg["environment"].items() if k != "TYPESAFE_ENV_FILE"}
        if value is not None:
            env["TYPESAFE_ENV_FILE"] = value
        other = write_config(tmp_path / f"cfg-{len(str(value))}.json",
                             {**cfg, "environment": env})
        assert run(other, "typesafe", prompt=answers(TYPESAFE)) == 2
        assert output(capsys) == {"refused": "TYPESAFE_ENV_FILE_NOT_CONFIGURED"}
    assert run(path, "typesafe", prompt=answers("short")) == 2
    assert output(capsys) == {"refused": "TYPESAFE_KEY_TOO_SHORT"}
    target = Path(cfg["environment"]["TYPESAFE_ENV_FILE"])
    target.parent.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("TYPESAFE_API_KEY=" + "x" * 8 + "\n")
    target.symlink_to(elsewhere)
    for flags in ((), ("--replace",)):
        assert run(path, "typesafe", *flags, prompt=answers(TYPESAFE)) == 2
        assert output(capsys) == {"refused": "SECRET_TARGET_MUST_BE_A_REGULAR_FILE"}
    assert target.is_symlink() and TYPESAFE not in elsewhere.read_text()


def test_role_tokens_are_generated_owner_only_distinct_and_never_printed(config, capsys):
    path, cfg = config
    tokens = {}
    for role in ("muse", "status", "operator"):
        assert run(path, "token", role, "--generate") == 0
        result = output(capsys)
        target = Path(cfg[role]["token_file"])
        assert result == {"secret": f"{role}-token", "written": str(target), "replaced": False,
                          "generated": True}
        tokens[role] = role_token(target)
        assert len(tokens[role]) >= 32 and mode(target) == 0o600
        captured = json.dumps(result)
        assert tokens[role] not in captured
    assert len(set(tokens.values())) == 3
    assert run(path, "token", "muse", "--generate") == 2
    assert output(capsys) == {"refused": "SECRET_ALREADY_PRESENT_USE_REPLACE"}
    assert role_token(cfg["muse"]["token_file"]) == tokens["muse"]
    assert run(path, "token", "muse", "--replace", prompt=answers("too-short")) == 2
    assert output(capsys) == {"refused": "ROLE_TOKEN_TOO_SHORT"}
    assert role_token(cfg["muse"]["token_file"]) == tokens["muse"]
    typed = "fixture-typed-muse-token-abcdefghijklmnopqrstuvwxyz"
    assert run(path, "token", "muse", "--replace", prompt=answers(typed)) == 0
    assert output(capsys)["replaced"] is True
    assert role_token(cfg["muse"]["token_file"]) == typed


def test_check_reports_presence_and_protection_only(config, capsys):
    path, cfg = config
    assert run(path, "check") == 0
    assert output(capsys) == {
        "alpaca": "ABSENT", "typesafe": "ABSENT_OR_UNPROTECTED",
        "muse-token": "ABSENT_OR_INVALID", "status-token": "ABSENT_OR_INVALID",
        "operator-token": "ABSENT_OR_INVALID",
    }
    assert run(path, "alpaca", prompt=answers(KEY_ID, SECRET)) == 0
    assert run(path, "typesafe", prompt=answers(TYPESAFE)) == 0
    for role in ("muse", "status", "operator"):
        assert run(path, "token", role, "--generate") == 0
    capsys.readouterr()
    assert run(path, "check") == 0
    assert output(capsys) == {
        "alpaca": "PRESENT", "typesafe": "PRESENT", "muse-token": "PRESENT",
        "status-token": "PRESENT", "operator-token": "PRESENT",
    }
    target = Path(cfg["environment"]["TYPESAFE_ENV_FILE"])
    target.chmod(0o644)
    assert run(path, "check") == 0
    assert output(capsys)["typesafe"] == "UNPROTECTED"
    target.chmod(0o600)
    target.write_text("TYPESAFE_API_KEY=one\nTYPESAFE_API_KEY=two\n")
    assert run(path, "check") == 0
    assert output(capsys)["typesafe"] == "INVALID"


def test_prompt_requires_an_interactive_terminal(config, capsys, monkeypatch):
    path, _ = config
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert script.main(["--config", str(path), "typesafe"]) == 2
    assert output(capsys) == {"refused": "INTERACTIVE_TERMINAL_REQUIRED"}


def test_relative_and_v1_configurations_are_refused(config, capsys, tmp_path):
    assert script.main(["--config", "private.json", "check"]) == 2
    assert output(capsys) == {"refused": "ABSOLUTE_CONFIG_PATH_REQUIRED"}
    v1 = write_config(tmp_path / "v1.json", v1_config(tmp_path))
    assert run(v1, "check") == 2
    assert output(capsys) == {"refused": "PRIVATE_CONFIG_V2_REQUIRED"}
    unreadable = tmp_path / "loose.json"
    unreadable.write_text("{}")  # Mode 0644: the loader refuses it as a whole, content unread.
    assert run(unreadable, "check") == 2
    assert output(capsys) == {"refused": "INVALID_PRIVATE_LAUNCH_CONFIGURATION"}
