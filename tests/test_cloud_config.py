"""Railway runtime profile: configuration from variables only (package cloud)."""

import json
from pathlib import Path

import pytest

from catalyst_lab import cloud_config
from catalyst_lab.cloud_config import CloudConfigError, ops_config, trader_config

CHECKOUT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((CHECKOUT / "deploy/private-paper.example.json").read_text())["environment"]
SECRET_VALUES = {
    "APCA_API_KEY_ID": "PKFIXTURE000000000001",
    "APCA_API_SECRET_KEY": "fixture-no-real-provider-secret-value",
    "TYPESAFE_API_KEY": "fixture-typesafe-key-never-a-real-one-000",
    "MANAGED_API_TOKEN": "fixture-cloud-muse-token-abcdefghijklmnopqrstuvwxyz",
    "MANAGED_STATUS_TOKEN": "fixture-cloud-status-token-abcdefghijklmnopqrstuvwxyz",
    "MANAGED_OPERATOR_TOKEN": "fixture-cloud-operator-token-abcdefghijklmnopqrstuvwxyz",
}
AGENT_TOKEN = "fixture-cloud-muse-agent-token-abcdefghijklmnopqrstuvwxyz"
RISK_PASSWORD = "fixture-risk-password-abcdefghijklmnopqrstuvwxyz"
JEV_PASSWORD = "fixture-jev-password-abcdefghijklmnopqrstuvwxyz"


def dsn(role, password, host="postgres.railway.internal", **extra):
    query = "&".join(f"{k}={v}" for k, v in {"sslmode": "require", **extra}.items())
    return f"postgresql://{role}:{password}@{host}:5432/catalyst_lab?{query}"


def trader_env(root, **overrides):
    env = {k: v for k, v in EXAMPLE.items()
           if k not in {"TYPESAFE_ENV_FILE", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
                        "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL"}}
    env.update(SECRET_VALUES, CATALYST_ENVIRONMENT="railway", CATALYST_STATE_DIR=str(root),
               PORT=env["MANAGED_HTTP_PORT"],
               MANAGED_AGENT_TOKENS_JSON=json.dumps({"muse": AGENT_TOKEN}),
               RISK_DATABASE_PASSWORD=RISK_PASSWORD, JEV_DATABASE_PASSWORD=JEV_PASSWORD,
               MANAGED_DATABASE_URL=dsn("catalyst_risk", RISK_PASSWORD),
               JEV_WORKER_DATABASE_URL=dsn("catalyst_jev", JEV_PASSWORD),
               HOME="/home/catalyst", PATH="/usr/bin", RAILWAY_SERVICE_NAME="trader")
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


APP_PASSWORD = "fixture-app-password-abcdefghijklmnopqrstuvwxyz"
BACKUP_PASSWORD = "fixture-backup-password-abcdefghijklmnopqrstuvwxyz"


def ops_env(root, **overrides):
    env = {
        "CATALYST_ENVIRONMENT": "railway", "CATALYST_STATE_DIR": str(root),
        "MANAGED_STATUS_TOKEN": SECRET_VALUES["MANAGED_STATUS_TOKEN"],
        "MANAGED_STATUS_API": "http://127.0.0.1:8780",
        "APP_DATABASE_PASSWORD": APP_PASSWORD, "BACKUP_DATABASE_PASSWORD": BACKUP_PASSWORD,
        "AUDIT_DATABASE_URL": dsn("catalyst_app", APP_PASSWORD),
        "BACKUP_DATABASE_URL": dsn("catalyst_backup", BACKUP_PASSWORD),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def places(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "image").mkdir()
    return {"cwd": tmp_path / "work", "release_root": tmp_path / "image"}


def refused(function, env, places):
    with pytest.raises(CloudConfigError) as caught:
        function(env, **places)
    text = caught.value.message() + str(caught.value)
    for value in [*SECRET_VALUES.values(), AGENT_TOKEN, RISK_PASSWORD, JEV_PASSWORD]:
        assert value not in text
    return caught.value


def test_a_complete_trader_environment_is_accepted_and_redacted(tmp_path, places):
    config = trader_config(trader_env(tmp_path / "volume"), **places)
    assert config.port == int(EXAMPLE["MANAGED_HTTP_PORT"])
    assert config.runtime.bind_host == "127.0.0.1" and not config.runtime.on_railway
    summary = json.dumps(cloud_config.summary(config)) + repr(config)
    for value in [*SECRET_VALUES.values(), AGENT_TOKEN, RISK_PASSWORD]:
        assert value not in summary


def test_on_railway_the_trader_binds_dual_stack_and_needs_its_state_on_the_volume(tmp_path,
                                                                                 places):
    env = trader_env(Path("/data"), RAILWAY_ENVIRONMENT_ID="fixture-environment",
                     RAILWAY_VOLUME_MOUNT_PATH="/data")
    config = trader_config(env, **places)
    assert config.runtime.on_railway and config.runtime.bind_host == "::"
    missing_volume = {k: v for k, v in env.items() if k != "RAILWAY_VOLUME_MOUNT_PATH"}
    assert refused(trader_config, missing_volume, places).code == "CLOUD_VOLUME_REQUIRED"
    elsewhere = {**env, "CATALYST_STATE_DIR": "/tmp/not-the-volume"}
    assert refused(trader_config, elsewhere, places).code == "CLOUD_STATE_DIR_NOT_ON_VOLUME"


def test_railway_mode_is_explicit(tmp_path, places):
    for value in (None, "local", "production"):
        env = trader_env(tmp_path, CATALYST_ENVIRONMENT=value)
        assert refused(trader_config, env, places).code == "RAILWAY_MODE_REQUIRED"


@pytest.mark.parametrize("where", ["cwd", "release_root"])
def test_a_dotenv_file_in_the_working_directory_or_image_refuses_startup(tmp_path, places,
                                                                          where):
    (places[where] / ".env").write_text("TYPESAFE_API_KEY=never-read\n")
    assert refused(trader_config, trader_env(tmp_path), places).code == "DOTENV_PRESENT"
    assert refused(ops_config, ops_env(tmp_path), places).code == "DOTENV_PRESENT"


def test_a_local_key_file_setting_refuses_startup(tmp_path, places):
    env = trader_env(tmp_path, TYPESAFE_ENV_FILE="/tmp/typesafe.env")
    error = refused(trader_config, env, places)
    assert error.code == "TYPESAFE_ENV_FILE_FORBIDDEN" and error.names == ("TYPESAFE_ENV_FILE",)


@pytest.mark.parametrize("name", cloud_config.TRADER_SECRETS)
def test_every_missing_or_placeholder_secret_is_refused_by_name(tmp_path, places, name):
    error = refused(trader_config, trader_env(tmp_path, **{name: None}), places)
    assert error.code == "CLOUD_SECRET_MISSING" and error.names == (name,)
    error = refused(trader_config, trader_env(tmp_path, **{name: "REQUIRED_SECRET"}), places)
    assert error.code == "CLOUD_SECRET_MISSING" and error.names == (name,)


def test_engine_settings_are_validated_like_the_private_config(tmp_path, places):
    error = refused(trader_config, trader_env(tmp_path, MANAGED_SELECTION_RULE=None), places)
    assert error.code == "CLOUD_ENGINE_SETTING_MISSING"
    assert error.names == ("MANAGED_SELECTION_RULE",)
    error = refused(trader_config, trader_env(tmp_path, MANAGED_CYCLE_POLICY_JSON="{}"), places)
    assert error.code == "CLOUD_ENGINE_SETTING_INVALID"
    placeholder = trader_env(tmp_path, MANAGED_RISK_POLICY_ID="REQUIRED_POLICY")
    assert refused(trader_config, placeholder, places).code == "CLOUD_ENGINE_SETTING_MISSING"
    typo = trader_env(tmp_path, MANAGED_SELECTION_RULES="JEV_TOP_K_SELECTION_V2")
    error = refused(trader_config, typo, places)
    assert error.code == "CLOUD_UNKNOWN_ENGINE_VARIABLE"
    assert error.names == ("MANAGED_SELECTION_RULES",)


@pytest.mark.parametrize("value", ["DISABLE", "enabled", "Enabled", "TRUE", "true", "1",
                                   "ON", " ENABLED", "ENABLED ", "ENABLED,DISABLED"])
def test_the_management_reviews_switch_is_exactly_enabled_or_disabled(tmp_path, places, value):
    """package cloud-hardening: a typo refuses the trader at once (exit 2), naming the variable,
    instead of passing the profile and failing every startup for ten minutes at a time."""
    env = trader_env(tmp_path, MANAGED_MANAGEMENT_REVIEWS=value)
    error = refused(trader_config, env, places)
    assert (error.code, error.names) == ("CLOUD_ENGINE_SETTING_INVALID",
                                         ("MANAGED_MANAGEMENT_REVIEWS",))
    for accepted in ("ENABLED", "DISABLED"):
        env = trader_env(tmp_path, MANAGED_MANAGEMENT_REVIEWS=accepted)
        assert trader_config(env, **places).port == 8780


def test_the_mac_preflight_names_an_invalid_reviews_switch_too():
    from catalyst_lab.managed_ops import environment_findings

    env = {k: v for k, v in EXAMPLE.items() if not v.startswith("REQUIRED")}
    assert "MANAGED_MANAGEMENT_REVIEWS" not in environment_findings(env)[1]
    env["MANAGED_MANAGEMENT_REVIEWS"] = "DISABLE"
    assert "MANAGED_MANAGEMENT_REVIEWS" in environment_findings(env)[1]
    env["MANAGED_MANAGEMENT_REVIEWS"] = "REQUIRED"  # A placeholder stays "missing", as before.
    missing, invalid = environment_findings(env)
    assert "MANAGED_MANAGEMENT_REVIEWS" in missing and "MANAGED_MANAGEMENT_REVIEWS" not in invalid


WINDOW = "MANAGED_CRYPTO_WINDOW_JSON"
BAD_WINDOWS = ("not json", "{}", '{"version": "CRYPTO_WINDOW_REVIEW_V1"}',
               '{"version": "CRYPTO_WINDOW_REVIEW_V2", "window_minutes": 240}',
               '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 250}',
               '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240.0}',
               '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 30}',
               '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240, "x": 1}')


def test_the_trade_window_is_required_in_the_cloud_and_parsed_strictly(tmp_path, places):
    """Package review-window: the example's window (240 minutes) passes; a dropped one refuses
    the trader by name (it would silently fall back to the 24-hour versions), and so does an
    invalid one."""
    env = trader_env(tmp_path)
    assert json.loads(env[WINDOW]) == {"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}
    assert WINDOW in cloud_config.CLOUD_REQUIRED_OPTIONAL
    assert trader_config(env, **places).port == 8780
    for dropped in (None, ""):
        error = refused(trader_config, trader_env(tmp_path, **{WINDOW: dropped}), places)
        assert (error.code, error.names) == ("CLOUD_ENGINE_SETTING_MISSING", (WINDOW,))
    for value in BAD_WINDOWS:
        error = refused(trader_config, trader_env(tmp_path, **{WINDOW: value}), places)
        assert (error.code, error.names) == ("CLOUD_ENGINE_SETTING_INVALID", (WINDOW,)), value
    for minutes in (60, 120, 1440):
        value = json.dumps({"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": minutes})
        assert trader_config(trader_env(tmp_path, **{WINDOW: value}), **places)


def test_the_mac_preflight_checks_the_trade_window_the_same_way():
    """The watchdog's and preflight's checks (managed_ops.environment_findings): optional on a
    Mac (absent: the 24-hour versions), exactly as startup parses it when present."""
    from catalyst_lab.managed_ops import ENV_NAMES, OPTIONAL_ENV, environment_findings

    assert WINDOW in ENV_NAMES and WINDOW in OPTIONAL_ENV
    env = {k: v for k, v in EXAMPLE.items() if not v.startswith("REQUIRED")}
    missing, invalid = environment_findings(env)
    assert WINDOW not in missing and WINDOW not in invalid
    missing, invalid = environment_findings({k: v for k, v in env.items() if k != WINDOW})
    assert WINDOW not in missing and WINDOW not in invalid
    for value in BAD_WINDOWS:
        _, invalid = environment_findings({**env, WINDOW: value})
        assert invalid.count(WINDOW) == 1, value


def test_role_and_agent_tokens_must_be_long_and_separate(tmp_path, places):
    status = SECRET_VALUES["MANAGED_STATUS_TOKEN"]
    same = trader_env(tmp_path, MANAGED_OPERATOR_TOKEN=status)
    assert refused(trader_config, same, places).code == "ROLE_TOKENS_NOT_SEPARATED"
    short = trader_env(tmp_path, MANAGED_API_TOKEN="too-short")
    assert refused(trader_config, short, places).code == "CLOUD_TOKEN_INVALID"
    for agents in ("{}", json.dumps({"muse": status}), json.dumps({"Bad Id": AGENT_TOKEN}),
                   "not json"):
        env = trader_env(tmp_path, MANAGED_AGENT_TOKENS_JSON=agents)
        assert refused(trader_config, env, places).code == "AGENT_TOKENS_INVALID"


def test_only_paper_credentials_and_endpoints(tmp_path, places):
    live_shaped = trader_env(tmp_path, APCA_API_KEY_ID="AKFIXTURE000000000001")
    assert refused(trader_config, live_shaped, places).code == "ALPACA_PAPER_CREDENTIALS_INVALID"
    redirected = trader_env(tmp_path, APCA_API_BASE_URL="https://example.invalid")
    assert refused(trader_config, redirected, places).code == "ALPACA_PAPER_CREDENTIALS_INVALID"


def test_role_dsns_name_the_restricted_role_the_ledger_and_tls(tmp_path, places):
    wrong_role = trader_env(tmp_path, MANAGED_DATABASE_URL=dsn("postgres", RISK_PASSWORD))
    error = refused(trader_config, wrong_role, places)
    assert error.code == "CLOUD_DATABASE_URL_INVALID" and error.names == ("MANAGED_DATABASE_URL",)
    other_db = trader_env(tmp_path, MANAGED_DATABASE_URL=dsn("catalyst_risk", RISK_PASSWORD)
                          .replace("/catalyst_lab", "/railway"))
    assert refused(trader_config, other_db, places).code == "CLOUD_DATABASE_URL_INVALID"
    plain = trader_env(tmp_path, JEV_WORKER_DATABASE_URL=dsn("catalyst_jev", JEV_PASSWORD)
                       .replace("sslmode=require", "sslmode=prefer"))
    assert refused(trader_config, plain, places).code == "CLOUD_DATABASE_SSL_REQUIRED"
    # A private socket (the local proof) needs no TLS off Railway; on Railway every DSN does.
    socket_dsn = ("host=/tmp/cloud-proof/socket port=55437 dbname=catalyst_lab "
                  f"user=catalyst_risk password={RISK_PASSWORD}")
    assert trader_config(trader_env(tmp_path, MANAGED_DATABASE_URL=socket_dsn), **places)
    on_railway = trader_env(Path("/data"), MANAGED_DATABASE_URL=socket_dsn,
                            RAILWAY_ENVIRONMENT_ID="fixture", RAILWAY_VOLUME_MOUNT_PATH="/data")
    assert refused(trader_config, on_railway, places).code == "CLOUD_DATABASE_SSL_REQUIRED"


def test_the_listening_port_is_the_app_port(tmp_path, places):
    assert refused(trader_config, trader_env(tmp_path, PORT="9999"), places).code == (
        "CLOUD_PORT_MISMATCH")
    assert refused(trader_config, trader_env(tmp_path, PORT=None), places).code == (
        "CLOUD_PORT_REQUIRED")


def test_ops_holds_no_trading_secret_and_reads_status_privately(tmp_path, places):
    config = ops_config(ops_env(tmp_path), **places)
    assert config.status_api == "http://127.0.0.1:8780" and config.notify == {}
    assert SECRET_VALUES["MANAGED_STATUS_TOKEN"] not in repr(config)
    for name in ("APCA_API_SECRET_KEY", "TYPESAFE_API_KEY", "MANAGED_OPERATOR_TOKEN",
                 "RISK_DATABASE_PASSWORD"):
        env = ops_env(tmp_path, **{name: "fixture-value-that-must-not-be-here-000000"})
        error = refused(ops_config, env, places)
        assert error.code == "OPS_MUST_NOT_HOLD_TRADING_SECRETS" and error.names == (name,)
    on_railway = {"RAILWAY_ENVIRONMENT_ID": "fixture", "RAILWAY_VOLUME_MOUNT_PATH": "/data",
                  "CATALYST_STATE_DIR": "/data"}
    private = ops_env(tmp_path, MANAGED_STATUS_API="http://trader.railway.internal:8080",
                      **on_railway)
    assert ops_config(private, **places).status_api == "http://trader.railway.internal:8080"
    for url in ("http://127.0.0.1:8080", "https://trader.up.railway.app",
                "http://trader.railway.internal:8080/api", "http://evil.example:8080"):
        env = ops_env(tmp_path, MANAGED_STATUS_API=url, **on_railway)
        assert refused(ops_config, env, places).code == "CLOUD_STATUS_API_INVALID"
    assert refused(ops_config, ops_env(tmp_path, AUDIT_DATABASE_URL=None), places).code == (
        "CLOUD_SECRET_MISSING")
    bad_role = ops_env(tmp_path, BACKUP_DATABASE_URL=dsn("catalyst_app", "x" * 40))
    assert refused(ops_config, bad_role, places).code == "CLOUD_DATABASE_URL_INVALID"


def test_ops_notify_uses_the_secret_url_variable(tmp_path, places):
    section = {"provider": "HEALTHCHECKS", "allowed_hosts": ["hc-ping.com"],
               "reminder_minutes": 30, "daily_head_hour_utc": 6, "timeout_seconds": 5}
    url = "https://hc-ping.com/00000000-fixture-0000-0000-000000000000"
    config = ops_config(ops_env(tmp_path, CLOUD_NOTIFY_JSON=json.dumps(section),
                                NOTIFY_PING_URL=url), **places)
    assert config.notify == section and url not in repr(config)
    for broken in ({"CLOUD_NOTIFY_JSON": json.dumps(section)},
                   {"NOTIFY_PING_URL": url},
                   {"CLOUD_NOTIFY_JSON": json.dumps(section),
                    "NOTIFY_PING_URL": "https://evil.example/x"},
                   {"CLOUD_NOTIFY_JSON": json.dumps({**section, "local_notification": True}),
                    "NOTIFY_PING_URL": url}):
        error = refused(ops_config, ops_env(tmp_path, **broken), places)
        assert error.code == "CLOUD_NOTIFY_INVALID" and url not in error.message()
