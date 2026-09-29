"""research_agent.token: bearer-token loading. No token value in this file is real."""

import pytest

from research_agent import token

FAKE_TOKEN = "fixture-not-a-real-token-abcdefghijklmno"  # noqa: S105 - a test fixture, not a secret


def test_load_token_from_file(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text(FAKE_TOKEN + "\n")
    path.chmod(0o600)  # The kit reads owner-only files only.
    env = {token.TOKEN_FILE_ENV: str(path)}
    assert token.load_token(env=env) == FAKE_TOKEN


def test_load_token_from_env_var():
    env = {token.TOKEN_ENV: FAKE_TOKEN}
    assert token.load_token(env=env) == FAKE_TOKEN


def test_file_takes_precedence_over_env_var(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("from-the-file")
    path.chmod(0o600)  # The kit reads owner-only files only.
    env = {token.TOKEN_FILE_ENV: str(path), token.TOKEN_ENV: "from-the-env-var"}
    assert token.load_token(env=env) == "from-the-file"


def test_missing_both_sources_raises_without_leaking_anything():
    with pytest.raises(token.TokenUnavailable) as excinfo:
        token.load_token(env={})
    assert token.TOKEN_FILE_ENV in str(excinfo.value)
    assert token.TOKEN_ENV in str(excinfo.value)


def test_unreadable_token_file_raises(tmp_path):
    missing = tmp_path / "does-not-exist.txt"
    with pytest.raises(token.TokenUnavailable, match="RESEARCH_AGENT_TOKEN_FILE_UNREADABLE"):
        token.load_token(env={token.TOKEN_FILE_ENV: str(missing)})


def test_empty_token_file_raises(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("   \n")
    path.chmod(0o600)  # The kit reads owner-only files only.
    with pytest.raises(token.TokenUnavailable, match="RESEARCH_AGENT_TOKEN_FILE_EMPTY"):
        token.load_token(env={token.TOKEN_FILE_ENV: str(path)})


def test_blank_env_var_is_treated_as_absent():
    with pytest.raises(token.TokenUnavailable):
        token.load_token(env={token.TOKEN_ENV: "   "})


def test_load_token_never_prints_the_token(tmp_path, capsys):
    path = tmp_path / "token.txt"
    path.write_text(FAKE_TOKEN)
    path.chmod(0o600)  # The kit reads owner-only files only.
    token.load_token(env={token.TOKEN_FILE_ENV: str(path)})
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out and FAKE_TOKEN not in captured.err
