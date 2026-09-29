import copy
import json
from pathlib import Path

import pytest

from catalyst_lab.jev_secrets import CredentialUnavailable, typesafe_key
from catalyst_lab.review_config import (
    APPROVED_GATE1,
    APPROVED_GATE1_V2,
    Gate1Inputs,
    ReviewConfigurationError,
    ReviewSettings,
)

KEY = "local-test-fixture-not-a-provider-key"
V1_PINNED = {  # JEV_LIVE_REVIEW_POLICY_V1 as approved; V2 must never move it.
    "version": "JEV_LIVE_REVIEW_POLICY_V1", "question_timeout_seconds": 3,
    "batch_timeout_seconds": 3, "overall_review_deadline_seconds": 10,
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TYPESAFE_API_KEY",
        "TYPESAFE_ENV_FILE",
        "RAILWAY_ENVIRONMENT_ID",
        "RISK_DATABASE_URL",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_READ_ONLY_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "CATALYST_ENVIRONMENT": "railway",
        "REVIEW_DATABASE_URL": "fixture-url-not-used",
        "REVIEW_WRITE_TOKEN": "w" * 40,
        "REVIEW_READ_TOKEN": "r" * 40,
        "JEV_RESEARCH_POLICY_ID": "MUSE_JEV_ACTIVE_V1",
        "JEV_REVIEW_POLICY_JSON": json.dumps(APPROVED_GATE1),
        "TYPESAFE_API_KEY": KEY,
    }.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.mark.parametrize("field", list(APPROVED_GATE1))
def test_every_gate1_field_required(field):
    values = copy.deepcopy(APPROVED_GATE1)
    del values[field]
    with pytest.raises(ReviewConfigurationError, match="GATE1_INPUTS_MISSING_OR_UNAPPROVED"):
        Gate1Inputs(values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_attempts", True),
        ("overall_review_deadline_seconds", 20),
        ("retry_http_statuses", [429, 500, 529]),
    ],
)
def test_unapproved_changes_rejected(field, value):
    values = copy.deepcopy(APPROVED_GATE1)
    values[field] = value
    with pytest.raises(ReviewConfigurationError):
        Gate1Inputs(values)


def test_v2_is_v1_with_only_the_version_and_the_two_eight_second_budgets():
    """JEV_LIVE_REVIEW_POLICY_V2 (owner ruling 2026-09-28): V1's exact values except the
    version, question_timeout_seconds 8 and batch_timeout_seconds 8; the overall deadline, the
    retries, the breaker and the evidence ages are V1's."""
    assert {k: APPROVED_GATE1[k] for k in V1_PINNED} == V1_PINNED
    changed = {k for k in APPROVED_GATE1 if APPROVED_GATE1[k] != APPROVED_GATE1_V2[k]}
    assert changed == {"version", "question_timeout_seconds", "batch_timeout_seconds"}
    assert set(APPROVED_GATE1_V2) == set(APPROVED_GATE1)
    assert APPROVED_GATE1_V2["version"] == "JEV_LIVE_REVIEW_POLICY_V2"
    assert APPROVED_GATE1_V2["question_timeout_seconds"] == 8
    assert APPROVED_GATE1_V2["batch_timeout_seconds"] == 8
    assert APPROVED_GATE1_V2["overall_review_deadline_seconds"] == 10
    for policy in (APPROVED_GATE1, APPROVED_GATE1_V2):  # Exactly the approved values, detached.
        values = copy.deepcopy(policy)
        accepted = Gate1Inputs(values)
        values["max_attempts"] = 99
        assert accepted.values == policy


@pytest.mark.parametrize("field", list(APPROVED_GATE1_V2))
def test_every_v2_field_required(field):
    values = copy.deepcopy(APPROVED_GATE1_V2)
    del values[field]
    with pytest.raises(ReviewConfigurationError, match="GATE1_INPUTS_MISSING_OR_UNAPPROVED"):
        Gate1Inputs(values)


@pytest.mark.parametrize("base,field,value", [
    # A label never borrows the other policy's budgets, and no budget in between is approved.
    (APPROVED_GATE1, "batch_timeout_seconds", 8),
    (APPROVED_GATE1, "question_timeout_seconds", 8),
    (APPROVED_GATE1_V2, "batch_timeout_seconds", 3),
    (APPROVED_GATE1_V2, "question_timeout_seconds", 3),
    (APPROVED_GATE1_V2, "batch_timeout_seconds", 5),
    (APPROVED_GATE1_V2, "batch_timeout_seconds", 8.0),
    (APPROVED_GATE1_V2, "overall_review_deadline_seconds", 20),
    (APPROVED_GATE1_V2, "evidence_max_age_seconds", 120),
    (APPROVED_GATE1_V2, "max_attempts", True),
    (APPROVED_GATE1_V2, "version", "JEV_LIVE_REVIEW_POLICY_V3"),
])
def test_unapproved_v2_variants_rejected(base, field, value):
    values = copy.deepcopy(base)
    values[field] = value
    with pytest.raises(ReviewConfigurationError, match="GATE1_INPUTS_MISSING_OR_UNAPPROVED"):
        Gate1Inputs(values)


def test_the_review_service_accepts_either_approved_policy(env):
    assert ReviewSettings.from_env().gate1.values == APPROVED_GATE1
    env.setenv("JEV_REVIEW_POLICY_JSON", json.dumps(APPROVED_GATE1_V2))
    assert ReviewSettings.from_env().gate1.values == APPROVED_GATE1_V2


def test_railway_startup_requires_key(env):
    env.delenv("TYPESAFE_API_KEY")
    with pytest.raises(CredentialUnavailable, match="MISSING_TYPESAFE_CREDENTIAL"):
        ReviewSettings.from_env()


def test_production_never_opens_env_or_keychain(env):
    from catalyst_lab import jev_secrets

    env.setattr(jev_secrets, "_local_env_key", lambda: pytest.fail("production read .env"))
    env.setattr(jev_secrets.ctypes, "CDLL", lambda *a: pytest.fail("production read Keychain"))
    assert ReviewSettings.from_env().environment == "railway"


def test_railway_detected_even_if_local_flag(env):
    env.setenv("RAILWAY_ENVIRONMENT_ID", "fixture-service")
    env.setenv("CATALYST_ENVIRONMENT", "local")
    with pytest.raises(ReviewConfigurationError, match="RAILWAY_ENVIRONMENT_REQUIRED"):
        ReviewSettings.from_env()


def test_private_env_precedes_injected_key_locally(env, tmp_path):
    env.setenv("CATALYST_ENVIRONMENT", "local")
    p = tmp_path / ".env"
    p.write_text("TYPESAFE_API_KEY=" + KEY + "-from-file\n")
    p.chmod(0o600)
    assert typesafe_key() == KEY + "-from-file"


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o400])
def test_env_requires_exact_0600(env, tmp_path, mode):
    env.setenv("CATALYST_ENVIRONMENT", "local")
    p = tmp_path / ".env"
    p.write_text("TYPESAFE_API_KEY=" + KEY + "\n")
    p.chmod(mode)
    with pytest.raises(CredentialUnavailable, match="MODE_0600"):
        typesafe_key()


def test_present_empty_env_does_not_fall_back(env, tmp_path):
    env.setenv("CATALYST_ENVIRONMENT", "local")
    p = tmp_path / ".env"
    p.write_text("TYPESAFE_API_KEY=\n")
    p.chmod(0o600)
    with pytest.raises(CredentialUnavailable, match="MISSING_TYPESAFE_CREDENTIAL"):
        typesafe_key()


def test_env_symlink_refused(env, tmp_path):
    env.setenv("CATALYST_ENVIRONMENT", "local")
    p = tmp_path / "private"
    p.write_text("TYPESAFE_API_KEY=" + KEY + "\n")
    p.chmod(0o600)
    (tmp_path / ".env").symlink_to(p)
    with pytest.raises(CredentialUnavailable):
        typesafe_key()


def test_missing_all_credentials_fails_closed(env):
    from catalyst_lab import jev_secrets

    env.setenv("CATALYST_ENVIRONMENT", "local")
    env.delenv("TYPESAFE_API_KEY")
    env.setattr(jev_secrets.sys, "platform", "linux")
    with pytest.raises(CredentialUnavailable, match="MISSING_TYPESAFE_CREDENTIAL"):
        typesafe_key()


@pytest.mark.parametrize("field", ["RISK_DATABASE_URL", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"])
def test_review_deployment_refuses_broker_configuration(env, field):
    env.setenv(field, "fixture-only")
    with pytest.raises(ReviewConfigurationError, match="BROKER_CONFIGURATION_FORBIDDEN"):
        ReviewSettings.from_env()


def test_names_only_example_and_container_excludes_secrets():
    root = Path(__file__).parents[1]
    lines = (root / ".env.example").read_text().splitlines()
    assert lines and all(line.endswith("=") and line.count("=") == 1 for line in lines)
    assert "TYPESAFE_API_KEY=" in lines
    docker = (root / "Dockerfile.review").read_text()
    assert "COPY . " not in docker and ".env" not in docker
    config = json.loads((root / "railway.json").read_text())
    assert config["deploy"]["startCommand"] == "python -m catalyst_lab.review_service"
    assert config["deploy"]["healthcheckPath"] == "/health"
