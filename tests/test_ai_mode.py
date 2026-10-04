"""``AI_MODE_SETTING_V1`` (package oss-packaging): ``NO_AI_MODE_V1`` runs without any AI judge,
and every half-configured AI mode is refused at startup.

Fixture evidence only: configuration maps, the Railway profile's validator, the runtime builder's
first check, and one admission on a disposable PostgreSQL with the fixture paper venue (no
network, no broker, no Jev).
"""

import json
from pathlib import Path

import httpx
import pytest

from catalyst_lab import ai_mode
from catalyst_lab.account_risk import FIXED_EXIT_ARM, MANAGED_RISK_V2_POLICY_ID
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.cloud_config import CloudConfigError, trader_config
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_ops import environment_findings
from catalyst_lab.managed_store import ManagedAuthorizationGate
from tests.test_cloud_config import places as places  # noqa: F401 -- fixture
from tests.test_cloud_config import trader_env
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_engineering import classify, enroll, selected, url
from tests.test_managed_execution import ManagedVenue

CHECKOUT = Path(__file__).resolve().parents[1]
DEFAULTS = json.loads((CHECKOUT / "deploy/local/managed-defaults.json").read_text())["environment"]
FIXTURE_ENGINE = {
    "MANAGED_DATABASE_URL": "host=/tmp/fixture dbname=catalyst_lab user=catalyst_risk",
    "JEV_WORKER_DATABASE_URL": "host=/tmp/fixture dbname=catalyst_lab user=catalyst_jev",
    "APCA_API_KEY_ID": "PKFIXTURE000000000001",
    "APCA_API_SECRET_KEY": "fixture-no-real-provider-secret-value",
    "MANAGED_API_TOKEN": "fixture-local-muse-token-abcdefghijklmnopqrstuvwxyz",
}


def no_ai(**changes):
    env = {**DEFAULTS, **FIXTURE_ENGINE, **changes}
    return {k: v for k, v in env.items() if v is not None}


# --- The setting -------------------------------------------------------------------------------


def test_absent_means_the_reference_deployment_and_unknown_values_are_refused():
    assert ai_mode.mode_from_env({}) == ai_mode.JEV_AI_MODE == "JEV_AI_MODE_V1"
    assert ai_mode.mode_from_env({"CATALYST_AI_MODE": ""}) == ai_mode.JEV_AI_MODE
    assert ai_mode.findings({"CATALYST_AI_MODE": "JEV_AI_MODE_V1"}) == []
    for value in ("NONE", "no_ai_mode_v1", "NO_AI_MODE_V2", " NO_AI_MODE_V1"):
        with pytest.raises(ai_mode.AiModeError, match="AI_MODE_UNKNOWN"):
            ai_mode.require({"CATALYST_AI_MODE": value})


def test_the_local_defaults_are_a_complete_no_ai_configuration():
    env = no_ai()
    assert env["CATALYST_AI_MODE"] == ai_mode.NO_AI_MODE
    assert ai_mode.require(env) == ai_mode.NO_AI_MODE
    assert environment_findings(env) == ([], [])
    # Nothing in the defaults turns a judge on.
    assert not set(DEFAULTS) & ai_mode.NO_AI_NOT_REQUIRED
    assert DEFAULTS["MANAGED_MANAGEMENT_REVIEWS"] == "DISABLED"


@pytest.mark.parametrize("changes,code,names", [
    ({"TYPESAFE_API_KEY": "fixture-typesafe-key-never-a-real-one-000"},
     "NO_AI_MODE_JEV_CREDENTIAL_PRESENT", ["TYPESAFE_API_KEY"]),
    ({"TYPESAFE_ENV_FILE": "/tmp/fixture.env"},
     "NO_AI_MODE_JEV_CREDENTIAL_PRESENT", ["TYPESAFE_ENV_FILE"]),
    ({"MANAGED_MANAGEMENT_REVIEWS": "ENABLED"},
     "NO_AI_MODE_MANAGEMENT_REVIEWS_NOT_DISABLED", ["MANAGED_MANAGEMENT_REVIEWS"]),
    ({"MANAGED_MANAGEMENT_REVIEWS": None},
     "NO_AI_MODE_MANAGEMENT_REVIEWS_NOT_DISABLED", ["MANAGED_MANAGEMENT_REVIEWS"]),
    ({"MANAGED_SELECTION_RULE": "JEV_TOP_K_SELECTION_V2"},
     "NO_AI_MODE_JEV_SELECTION_CONFIGURED", ["MANAGED_SELECTION_RULE"]),
    ({"MANAGED_TOPK_SELECTION_JSON": '{"k": 10}'},
     "NO_AI_MODE_JEV_SELECTION_CONFIGURED", ["MANAGED_TOPK_SELECTION_JSON"]),
    ({"JEV_MONTHLY_BUDGET_USD": "50"},
     "NO_AI_MODE_JEV_BUDGET_CONFIGURED", ["JEV_MONTHLY_BUDGET_USD"]),
    ({"MANAGED_AGENT_TOKENS_JSON": '{"muse": "fixture-agent-token-abcdefghijklmnopqrstuv"}'},
     "NO_AI_MODE_RESEARCH_AGENTS_CONFIGURED", ["MANAGED_AGENT_TOKENS_JSON"]),
    ({"MANAGED_RESEARCH_SCHEDULE_JSON": '{"timezone": "UTC", "runs": ["08:00"]}'},
     "NO_AI_MODE_RESEARCH_SCHEDULE_CONFIGURED", ["MANAGED_RESEARCH_SCHEDULE_JSON"]),
])
def test_a_half_configured_no_ai_mode_is_refused_by_name(changes, code, names):
    env = no_ai(**changes)
    with pytest.raises(ai_mode.AiModeError) as refused:
        ai_mode.require(env)
    assert refused.value.code == code and set(names) <= set(refused.value.names)
    # Values are never part of a refusal.
    for value in changes.values():
        if value:
            assert value not in str(refused.value) and value not in refused.value.names
    missing, invalid = environment_findings(env)
    assert set(names) <= set(missing) | set(invalid)


def test_empty_agent_tokens_are_not_a_research_agent():
    assert ai_mode.require(no_ai(MANAGED_AGENT_TOKENS_JSON="{}")) == ai_mode.NO_AI_MODE
    assert ai_mode.require(no_ai(MANAGED_AGENT_TOKENS_JSON="[]")) == ai_mode.NO_AI_MODE


def test_the_record_names_the_version():
    record = ai_mode.record(ai_mode.NO_AI_MODE)
    assert record["setting_version"] == "AI_MODE_SETTING_V1"
    assert "STRATEGY_SIGNAL_SELECTION_V1" in record["selection"]
    assert "FIXED_EXIT" in record["maintenance"]


# --- The Railway profile ------------------------------------------------------------------------


def no_ai_trader_env(root, **overrides):
    """The cloud trader's variables in NO_AI_MODE_V1: no TypeSafe key, no agent tokens, no Jev
    selection, budget or schedule; management reviews disabled."""
    drop = dict.fromkeys(sorted(ai_mode.NO_AI_NOT_REQUIRED))
    settings = {"CATALYST_AI_MODE": "NO_AI_MODE_V1", "MANAGED_MANAGEMENT_REVIEWS": "DISABLED",
                **drop, **overrides}
    return trader_env(root, **settings)


def test_the_cloud_trader_accepts_a_complete_no_ai_configuration(tmp_path, places):  # noqa: F811
    config = trader_config(no_ai_trader_env(tmp_path), **places)
    assert config.port == 8780


def test_the_reference_cloud_trader_is_unchanged(tmp_path, places):  # noqa: F811
    trader_config(trader_env(tmp_path), **places)  # JEV_AI_MODE_V1 by absence.
    trader_config(trader_env(tmp_path, CATALYST_AI_MODE="JEV_AI_MODE_V1"), **places)
    with pytest.raises(CloudConfigError, match="CLOUD_SECRET_MISSING"):
        trader_config(trader_env(tmp_path, TYPESAFE_API_KEY=None), **places)
    with pytest.raises(CloudConfigError, match="AGENT_TOKENS_INVALID"):
        trader_config(trader_env(tmp_path, MANAGED_AGENT_TOKENS_JSON="{}"), **places)


@pytest.mark.parametrize("overrides,code", [
    ({"TYPESAFE_API_KEY": "fixture-typesafe-key-never-a-real-one-000"},
     "NO_AI_MODE_JEV_CREDENTIAL_PRESENT"),
    ({"MANAGED_MANAGEMENT_REVIEWS": "ENABLED"}, "NO_AI_MODE_MANAGEMENT_REVIEWS_NOT_DISABLED"),
    ({"MANAGED_SELECTION_RULE": "JEV_TOP_K_SELECTION_V2"}, "NO_AI_MODE_JEV_SELECTION_CONFIGURED"),
    ({"JEV_MONTHLY_BUDGET_USD": "50"}, "NO_AI_MODE_JEV_BUDGET_CONFIGURED"),
    ({"CATALYST_AI_MODE": "NO_AI"}, "AI_MODE_UNKNOWN"),
])
def test_the_cloud_trader_refuses_a_half_configured_ai_mode(tmp_path, places, overrides,  # noqa: F811
                                                            code):
    with pytest.raises(CloudConfigError) as refused:
        trader_config(no_ai_trader_env(tmp_path, **overrides), **places)
    assert refused.value.code == code


def test_the_runtime_builder_refuses_a_half_configured_mode_before_anything_else(monkeypatch):
    from catalyst_lab.managed_runtime import build_runtime_from_env

    key = "fixture-typesafe-" + "key-never-a-real-one-000"  # Assembled: the hygiene scan.
    for name, value in no_ai(TYPESAFE_API_KEY=key).items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ai_mode.AiModeError, match="NO_AI_MODE_JEV_CREDENTIAL_PRESENT"):
        build_runtime_from_env()


# --- Admission: every setup takes the FIXED_EXIT arm ---------------------------------------------


def engine_for(er, mode):  # noqa: F811
    risk = RiskRepository(url(er, "catalyst_risk"))
    venue = ManagedVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(
        risk, broker, policy=engineering_execution_policy(), clock=lambda: venue.now,
        review_store=JevStore(url(er, "catalyst_jev")), risk_policy_id=MANAGED_RISK_V2_POLICY_ID,
        ai_mode=mode,
    )
    assert engine.reconcile()["clean"]
    return engine, broker


def test_no_ai_mode_admits_the_setup_in_the_fixed_exit_arm(er):  # noqa: F811
    engine, broker = engine_for(er, ai_mode.NO_AI_MODE)
    try:
        classify(engine)
        sid = engine.admit(selected(engine.repo, enroll(engine.repo)))
        _, state = engine._load(sid)
        assert state["arm"] == FIXED_EXIT_ARM and state["arm_method"] == "NO_AI_MODE_V1"
        assert state["risk_policy_id"] == MANAGED_RISK_V2_POLICY_ID
    finally:
        broker.close()


def test_the_reference_mode_keeps_the_randomized_arm(er):  # noqa: F811
    from catalyst_lab.account_risk import ARM_METHOD, assign_arm

    engine, broker = engine_for(er, None)
    try:
        classify(engine)
        sid = engine.admit(selected(engine.repo, enroll(engine.repo)))
        _, state = engine._load(sid)
        assert state["arm"] == assign_arm(sid, 30) and state["arm_method"] == ARM_METHOD
    finally:
        broker.close()


def test_an_unknown_mode_is_refused_by_the_engine():
    with pytest.raises(ValueError, match="AI_MODE_UNKNOWN"):
        ManagedExecution(None, None, policy=engineering_execution_policy(), clock=None,
                         review_store=None, ai_mode="NO_AI")


def test_no_ai_strategies_scan_the_fixed_alpaca_pairs():
    from catalyst_lab.history_test import universe_for
    from catalyst_lab.strategy_paper import StrategySignalSource

    assert ai_mode.strategy_universe() == universe_for("alpaca")
    assert len(ai_mode.strategy_universe()) == 33
    source = StrategySignalSource(None, None, ("X_V1",), None, None,
                                  universe=ai_mode.strategy_universe)
    assert source.symbols(None, None) == universe_for("alpaca")
    assert ai_mode.record(ai_mode.NO_AI_MODE)["strategy_universe"] == (
        "ALPACA_CRYPTO_SECTORS_V1 pairs")
