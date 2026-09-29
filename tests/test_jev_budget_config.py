"""The monthly Jev budget's settings (package jev-budget): the deploy example carries them, the
Railway profile requires them and refuses a missing or invalid one by name, the Mac preflight
requires the budget whenever Jev reviews open trades, and the runtime factory refuses to start
without it. Configuration only: no database, broker, provider or network.
"""

import json
from pathlib import Path

import pytest

from catalyst_lab import cloud_config
from catalyst_lab.cloud_config import trader_config
from catalyst_lab.managed_ops import ENV_NAMES, OPTIONAL_ENV, environment_findings
from catalyst_lab.managed_runtime import build_runtime_from_env
from tests.test_cloud_config import places as places
from tests.test_cloud_config import refused, trader_env
from tests.test_managed_runtime import configure_env

CHECKOUT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((CHECKOUT / "deploy/private-paper.example.json").read_text())["environment"]
NAMES = ("JEV_MONTHLY_BUDGET_USD", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD",
         "JEV_BYTES_PER_TOKEN")
INVALID = {"JEV_MONTHLY_BUDGET_USD": ("0", "-50", "50.001", "5e1", "fifty", "100001"),
           "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD": ("0", "0.0420001", "1001", "free"),
           "JEV_BYTES_PER_TOKEN": ("0", "0.9", "11", "3.0001", "three")}


def test_the_deploy_example_carries_the_owners_budget_and_the_published_price():
    # Owner, 2026-09-28: $70 a month in total, Jev at most $50 of it (Railway: $15 alert, $20
    # hard limit). TypeSafe's published jev-1.13 price; one token per three request bytes.
    assert {name: EXAMPLE[name] for name in NAMES} == {
        "JEV_MONTHLY_BUDGET_USD": "50", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD": "0.042",
        "JEV_BYTES_PER_TOKEN": "3"}
    assert set(NAMES) <= ENV_NAMES and set(NAMES) <= OPTIONAL_ENV
    assert set(NAMES) <= cloud_config.CLOUD_REQUIRED_OPTIONAL
    assert set(NAMES) <= cloud_config.TRADER_NAMES
    assert not set(NAMES) & cloud_config.OPS_NAMES  # The ops service reads the trader's status.


@pytest.mark.parametrize("name", NAMES)
def test_the_cloud_refuses_a_missing_or_invalid_budget_setting_by_name(tmp_path, places, name):
    assert trader_config(trader_env(tmp_path), **places).port == 8780
    for absent in (None, "", "REQUIRED_JEV_BUDGET"):
        error = refused(trader_config, trader_env(tmp_path, **{name: absent}), places)
        assert (error.code, error.names) == ("CLOUD_ENGINE_SETTING_MISSING", (name,))
    for value in INVALID[name]:
        error = refused(trader_config, trader_env(tmp_path, **{name: value}), places)
        assert (error.code, error.names) == ("CLOUD_ENGINE_SETTING_INVALID", (name,))
        assert value not in error.message()
    typo = trader_env(tmp_path, JEV_MONTHLY_BUDGET="50")
    error = refused(trader_config, typo, places)
    assert (error.code, error.names) == ("CLOUD_UNKNOWN_ENGINE_VARIABLE", ("JEV_MONTHLY_BUDGET",))


def test_the_mac_preflight_requires_the_budget_when_jev_reviews_open_trades():
    env = {k: v for k, v in EXAMPLE.items() if not v.startswith(("REQUIRED", "/REQUIRED"))}
    assert not set(NAMES) & set(sum(environment_findings(env), []))
    without = {k: v for k, v in env.items() if k not in NAMES}
    assert not set(NAMES) & set(sum(environment_findings(without), []))  # DISABLED: optional.
    enabled = {**without, "MANAGED_MANAGEMENT_REVIEWS": "ENABLED"}
    missing, invalid = environment_findings(enabled)
    assert "JEV_MONTHLY_BUDGET_USD" in missing and not set(NAMES) & set(invalid)
    assert missing == sorted(missing)
    # With the budget, the price and bytes per token may be left to their defaults.
    missing, _ = environment_findings({**enabled, "JEV_MONTHLY_BUDGET_USD": "50"})
    assert not set(NAMES) & set(missing)
    for name, values in INVALID.items():
        for value in values:
            _, invalid = environment_findings({**env, name: value})
            assert name in invalid, (name, value)


def fresh_env(monkeypatch):
    """The runtime tests' environment (reviews ENABLED, the example's budget of 50) and no other
    budget setting."""
    configure_env(monkeypatch)
    for name in NAMES[1:]:
        monkeypatch.delenv(name, raising=False)
    for key in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_the_runtime_refuses_to_start_reviews_without_a_valid_budget(monkeypatch):
    fresh_env(monkeypatch)
    with pytest.raises(ValueError, match="credentials are required"):
        build_runtime_from_env()  # Past the configuration: it is complete.
    for name, value in (("JEV_MONTHLY_BUDGET_USD", None), ("JEV_MONTHLY_BUDGET_USD", "0"),
                        ("JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD", "free"),
                        ("JEV_BYTES_PER_TOKEN", "0")):
        fresh_env(monkeypatch)
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match="REQUIRED_MANAGED_CONFIGURATION_MISSING_OR_INVALID"):
            build_runtime_from_env()
    # Reviews DISABLED send Jev nothing: no budget is needed (on a Mac; the cloud requires it).
    fresh_env(monkeypatch)
    monkeypatch.delenv("JEV_MONTHLY_BUDGET_USD")
    monkeypatch.setenv("MANAGED_MANAGEMENT_REVIEWS", "DISABLED")
    with pytest.raises(ValueError, match="credentials are required"):
        build_runtime_from_env()
    monkeypatch.setenv("CATALYST_ENVIRONMENT", "railway")
    with pytest.raises(ValueError, match="REQUIRED_MANAGED_CONFIGURATION_MISSING_OR_INVALID"):
        build_runtime_from_env()
