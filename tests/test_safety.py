from pathlib import Path

import pytest

from catalyst_lab.config import PAPER_ENDPOINT, Settings


@pytest.mark.parametrize("name", ["ALPACA_BASE_URL", "APCA_API_BASE_URL"])
def test_other_endpoints_abort_startup(monkeypatch, name):
    monkeypatch.setenv(name, "https://unexpected.example")
    with pytest.raises(ValueError, match="fixed Alpaca Paper"):
        Settings.from_env()


def test_source_contains_no_prohibited_endpoint():
    # Construct the prohibited string so the test does not introduce it into the source tree.
    prohibited = PAPER_ENDPOINT.replace("paper-", "")
    root = Path(__file__).resolve().parents[1]
    for directory in ("src", "tests", "docs"):
        for path in (root / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                # Bytes, so binary files (the dashboard's self-hosted fonts) are checked too.
                assert prohibited.encode() not in path.read_bytes(), path


def test_no_broker_keys_required(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "host=private-local-socket")
    monkeypatch.setenv("MUSE_API_TOKEN", "test-token-at-least-32-characters-long")
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    assert Settings.from_env().database_url == "host=private-local-socket"
