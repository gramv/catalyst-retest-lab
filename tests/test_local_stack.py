"""The local Docker stack (``LOCAL_STACK_V1``, package oss-packaging): ``compose.yaml``'s rules,
the image recipe, and every ``local_stack`` component against a disposable, password-protected
PostgreSQL shaped like the compose ``db`` service.

Fixture and local-PostgreSQL evidence only: no Docker, no network, no broker, no AI. The
simulated venue reads ``sample_market``'s generated bars. A Docker run of the same stack is
separate evidence (docs/packages/oss-packaging.md).
"""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from catalyst_lab import cloud_provision, local_stack, strategies
from catalyst_lab.config import PAPER_ENDPOINT, SCHEMA_VERSION
from catalyst_lab.local_stack import LocalStackError
from tests import cloud_fixtures
from tests.compose_yaml import ComposeYamlError, loads

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = loads((ROOT / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]
APP_SERVICES = ("secrets", "provision", "trader", "page", "jobs", "catalyst", "trader-alpaca")
EXAMPLES = ROOT / "examples" / "strategies"
NOW = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
PAPER_KEY = {"APCA_API_KEY_ID": "PKFIXTURE000000000001",
             "APCA_API_SECRET_KEY": "fixture-no-real-provider-secret-value"}


def mounts(service):
    """``[(source, target, read_only, subpath)]`` of a service's volumes, short or long form."""
    out = []
    for item in SERVICES[service].get("volumes", []):
        if isinstance(item, str):
            parts = item.split(":")
            out.append((parts[0], parts[1], parts[2:] == ["ro"], None))
        else:
            out.append((item["source"], item["target"], item.get("read_only", False),
                        (item.get("volume") or {}).get("subpath")))
    return out


# --- compose.yaml -------------------------------------------------------------------------------


def test_compose_parses_in_the_strict_subset_and_names_every_service():
    assert COMPOSE["name"] == "catalyst-local"
    assert set(SERVICES) == {"db", *APP_SERVICES}
    assert set(COMPOSE["volumes"]) == {"pgdata", "admin-secret", "role-secrets", "history"}
    with pytest.raises(ComposeYamlError):
        loads("services:\n  a: &x\n    image: \"b\"\n")
    with pytest.raises(ComposeYamlError):
        loads("services:\n  a:\n    image: unquoted\n")


def test_every_app_container_is_the_local_image_hardened_and_never_root():
    for name in APP_SERVICES:
        service = SERVICES[name]
        assert service["build"] == {"context": ".", "dockerfile": "Dockerfile.local"}, name
        assert service["image"] == "catalyst-lab-local:dev"
        assert service["cap_drop"] == ["ALL"] and service["security_opt"] == [
            "no-new-privileges:true"]
        assert service["read_only"] is (name != "secrets"), name
        assert "user" not in service  # The image's USER 10001 applies.
    recipe = "\n".join(line for line in (ROOT / "Dockerfile.local").read_text().splitlines()
                        if not line.lstrip().startswith("#"))
    assert "USER 10001:10001" in recipe and "ARG " not in recipe
    assert "COPY . " not in recipe and ".env" not in recipe
    copies = [line.split()[1] for line in recipe.splitlines() if line.startswith("COPY ")]
    assert copies == ["deploy/requirements.managed.txt", "src", "deploy/local", "examples"]
    ignore = (ROOT / "Dockerfile.local.dockerignore").read_text().splitlines()
    rules = [line for line in ignore if line and not line.startswith("#")]
    assert rules[0] == "*" and "**/.env" in rules and "**/.env.*" in rules


def test_only_secrets_db_and_provision_see_the_superuser_password():
    seen = {name for name in SERVICES for source, *_ in mounts(name) if source == "admin-secret"}
    assert seen == {"secrets", "db", "provision"}
    assert SERVICES["db"]["environment"] == {
        "POSTGRES_PASSWORD_FILE": "/run/catalyst-admin/postgres-password"}
    whole = {name for name in SERVICES for source, _, _, sub in mounts(name)
             if source == "role-secrets" and sub is None}
    assert whole == {"secrets", "provision"}
    roles = {name: sorted(sub for source, _, ro, sub in mounts(name)
                          if source == "role-secrets" and sub is not None and ro)
             for name in SERVICES}
    assert roles["trader"] == ["risk"] and roles["jobs"] == ["risk"]
    assert roles["page"] == ["public"]
    assert roles["catalyst"] == ["app", "risk"]
    assert roles["trader-alpaca"] == ["jev", "risk", "trader-tokens"]
    for name in ("trader", "page", "jobs", "catalyst", "trader-alpaca"):
        for _, target, _, sub in mounts(name):
            if sub is not None:
                assert target == f"/run/catalyst-roles/{sub}"


def test_nothing_but_the_page_is_published_and_only_on_loopback():
    published = {name: SERVICES[name]["ports"] for name in SERVICES if "ports" in SERVICES[name]}
    assert published == {"page": ["127.0.0.1:8080:8080"]}


def test_no_key_by_default_and_the_broker_profile_is_opt_in():
    text = (ROOT / "compose.yaml").read_text()
    assert PAPER_ENDPOINT.replace("paper-", "") not in text
    for name, service in SERVICES.items():
        for key, value in (service.get("environment") or {}).items():
            assert not key.startswith(("APCA_", "ALPACA_")) and "TYPESAFE" not in key, name
            assert isinstance(value, str)
        if name != "trader-alpaca":
            assert "env_file" not in service
    assert SERVICES["trader"]["environment"]["CATALYST_VENUE"] == "simulated"
    alpaca = SERVICES["trader-alpaca"]
    assert alpaca["profiles"] == ["alpaca-paper"] and SERVICES["catalyst"]["profiles"] == ["tools"]
    assert alpaca["env_file"] == [{"path": ".env.paper", "required": True}]
    assert alpaca["environment"]["CATALYST_VENUE"] == "alpaca-paper"
    assert ".env.*" in (ROOT / ".gitignore").read_text().splitlines()  # .env.paper is ignored.
    example = (ROOT / "deploy/local/alpaca-paper.env.example").read_text().splitlines()
    assignments = [line for line in example if line and not line.startswith("#")]
    assert assignments == ["APCA_API_KEY_ID=", "APCA_API_SECRET_KEY="]


def test_the_start_order_and_the_healthchecks():
    assert SERVICES["db"]["depends_on"] == {
        "secrets": {"condition": "service_completed_successfully"}}
    assert SERVICES["provision"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    for name in ("trader", "page", "jobs", "catalyst", "trader-alpaca"):
        assert SERVICES[name]["depends_on"] == {
            "provision": {"condition": "service_completed_successfully"}}, name
    assert SERVICES["db"]["healthcheck"]["test"][0] == "CMD-SHELL"
    for name, port in (("trader", 8081), ("page", 8080), ("trader-alpaca", 8780)):
        test = SERVICES[name]["healthcheck"]["test"]
        assert test[:5] == ["CMD", "python", "-m", "catalyst_lab.local_stack", "health"]
        assert test[5] == f"http://127.0.0.1:{port}/health"
    for name in ("secrets", "provision", "jobs"):
        assert SERVICES[name]["restart"] == "no"
    for name in ("secrets", "provision", "trader", "page", "jobs"):
        assert SERVICES[name]["command"][:3] == ["python", "-m", "catalyst_lab.local_stack"]
    assert SERVICES["catalyst"]["entrypoint"] == ["catalyst-lab"]


def test_the_plugin_folders_exist_in_the_image_and_the_checkout():
    for name in ("trader", "jobs", "catalyst", "trader-alpaca"):
        folders = SERVICES[name]["environment"]["CATALYST_STRATEGY_PLUGINS_DIR"].split(":")
        assert folders == ["/app/examples/strategies", "/my-strategies"]
    assert (ROOT / "my-strategies").is_dir()
    assert not list((ROOT / "my-strategies").glob("*.py"))


# --- secrets and settings (no database) ----------------------------------------------------------


@pytest.fixture
def secret_env(tmp_path):
    env = {"CATALYST_ADMIN_SECRET_DIR": str(tmp_path / "admin"),
           "CATALYST_ROLES_SECRET_DIR": str(tmp_path / "roles")}
    return env


def test_secrets_are_generated_once_distinct_and_private(secret_env):
    first = local_stack.generate_secrets(secret_env)
    assert first["result"] == "SECRETS_READY" and first["kept"] == 0
    roles = Path(secret_env["CATALYST_ROLES_SECRET_DIR"])
    values = {}
    for role, variable in cloud_provision.LOGIN_ROLES.items():
        path = roles / local_stack.ROLE_FOLDERS[role] / variable
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        values[variable] = local_stack.read_secret(path)
    for name in local_stack.TRADER_TOKENS:
        values[name] = local_stack.read_secret(roles / local_stack.TOKEN_FOLDER / name)
    admin = Path(secret_env["CATALYST_ADMIN_SECRET_DIR"]) / local_stack.ADMIN_FILE
    values["admin"] = local_stack.read_secret(admin)
    assert len(set(values.values())) == len(values)
    assert all(cloud_provision.PASSWORD.fullmatch(v) for v in values.values())
    again = local_stack.generate_secrets(secret_env)
    assert again["created"] == [] and again["kept"] == len(values)
    assert local_stack.read_secret(admin) == values["admin"]  # Never overwritten.


def test_a_symlinked_or_empty_secret_is_refused(secret_env, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x" * 40)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(LocalStackError, match="LOCAL_STACK_SECRET_MISSING"):
        local_stack.read_secret(link)
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(LocalStackError, match="LOCAL_STACK_SECRET_INVALID"):
        local_stack.read_secret(empty)


def test_the_simulated_venue_refuses_any_trading_credential(secret_env):
    for name in ("APCA_API_KEY_ID", "TYPESAFE_API_KEY", "APCA_API_BASE_URL"):
        with pytest.raises(LocalStackError) as refused:
            local_stack.trader({**secret_env, name: "fixture-value-0000"}, check_only=True)
        assert refused.value.code == "SIMULATED_VENUE_REFUSES_CREDENTIALS"
        assert refused.value.names == (name,)
    checked = local_stack.trader({**secret_env, "CATALYST_SIM_BARS": "synthetic"},
                                 check_only=True)
    assert checked["venue"] == "SIMULATED_VENUE_V1"
    assert checked["bars"] == "SYNTHETIC_SAMPLE_BARS_V1"
    with pytest.raises(LocalStackError, match="LOCAL_STACK_VENUE_UNKNOWN"):
        local_stack.trader({"CATALYST_VENUE": "live"}, check_only=True)
    with pytest.raises(LocalStackError, match="LOCAL_STACK_SIM_BARS_UNKNOWN"):
        local_stack.trader({"CATALYST_SIM_BARS": "cached"}, check_only=True)


def test_simulated_symbols():
    assert local_stack.sim_symbols({}, "public") == list(local_stack.DEFAULT_SIM_SYMBOLS)
    assert local_stack.sim_symbols({}, "synthetic") == ["BTC/USD", "DOGE/USD", "ETH/USD",
                                                        "SOL/USD"]
    assert local_stack.sim_symbols({"CATALYST_SIM_SYMBOLS": "eth/usd, BTC/USD"}, "public") == [
        "BTC/USD", "ETH/USD"]
    for raw, bars in (("ETH", "public"), ("ETH/EUR", "public"), ("LINK/USD", "synthetic")):
        with pytest.raises(LocalStackError, match="LOCAL_STACK_SIM_SYMBOLS_INVALID"):
            local_stack.sim_symbols({"CATALYST_SIM_SYMBOLS": raw}, bars)


def test_next_pass_is_ninety_seconds_after_the_hour():
    at = datetime(2026, 9, 1, 12, 0, 30, tzinfo=UTC)
    assert local_stack.next_pass(at) == datetime(2026, 9, 1, 12, 1, 30, tzinfo=UTC)
    assert local_stack.next_pass(at + timedelta(minutes=2)) == datetime(
        2026, 9, 1, 13, 1, 30, tzinfo=UTC)


def managed_env(secret_env, **changes):
    env = {**secret_env, "CATALYST_DB_HOST": "/tmp/fixture-socket", "CATALYST_DB_PORT": "5432",
           "CATALYST_VENUE": "alpaca-paper", **PAPER_KEY, **changes}
    return {k: v for k, v in env.items() if v is not None}


def test_the_managed_environment_is_the_no_ai_defaults_with_the_users_key(secret_env):
    local_stack.generate_secrets(secret_env)
    engine = local_stack.managed_environment(managed_env(secret_env))
    assert engine["CATALYST_AI_MODE"] == "NO_AI_MODE_V1"
    assert engine["MANAGED_MANAGEMENT_REVIEWS"] == "DISABLED"
    assert engine["MANAGED_STRATEGIES_JSON"] == "[]"
    assert "user=catalyst_risk" in engine["MANAGED_DATABASE_URL"]
    assert "user=catalyst_jev" in engine["JEV_WORKER_DATABASE_URL"]
    assert "TYPESAFE_API_KEY" not in engine
    summary = local_stack.managed_summary(engine)
    assert summary["ai_mode"] == "NO_AI_MODE_V1" and summary["venue"] == "ALPACA_PAPER"
    assert not any(v in json.dumps(summary) for v in PAPER_KEY.values())
    # An engine override from the user's .env.paper wins over the defaults.
    listed = local_stack.managed_environment(managed_env(
        secret_env, MANAGED_STRATEGIES_JSON='["EXAMPLE_MA_CROSS_V1"]'))
    assert listed["MANAGED_STRATEGIES_JSON"] == '["EXAMPLE_MA_CROSS_V1"]'


@pytest.mark.parametrize("changes,code", [
    ({"APCA_API_KEY_ID": None}, "LOCAL_STACK_ENGINE_SETTINGS_INVALID"),
    ({"APCA_API_KEY_ID": "AKLIVELOOKING000001"}, "ALPACA_PAPER_CREDENTIALS_INVALID"),
    ({"APCA_API_BASE_URL": "https://example.invalid"}, "LOCAL_STACK_SETTING_NOT_ALLOWED"),
    ({"MANAGED_DATABASE_URL": "host=elsewhere"}, "LOCAL_STACK_SETTING_NOT_ALLOWED"),
    ({"MANAGED_API_TOKEN": "x" * 40}, "LOCAL_STACK_SETTING_NOT_ALLOWED"),
    ({"CATALYST_ENVIRONMENT": "railway"}, "LOCAL_STACK_SETTING_NOT_ALLOWED"),
    ({"TYPESAFE_API_KEY": "fixture-typesafe-key-never-a-real-one-000"},
     "NO_AI_MODE_JEV_CREDENTIAL_PRESENT"),
    ({"MANAGED_MANAGEMENT_REVIEWS": "ENABLED"}, "NO_AI_MODE_MANAGEMENT_REVIEWS_NOT_DISABLED"),
    ({"CATALYST_AI_MODE": "JEV_AI_MODE_V1", "MANAGED_RISK_POLICY_ID": "lower"},
     "LOCAL_STACK_ENGINE_SETTINGS_INVALID"),
])
def test_the_managed_environment_refuses_by_name(secret_env, changes, code):
    local_stack.generate_secrets(secret_env)
    with pytest.raises(LocalStackError) as refused:
        local_stack.managed_environment(managed_env(secret_env, **changes))
    assert refused.value.code == code
    for value in (*PAPER_KEY.values(), *(v for v in changes.values() if v)):
        assert value not in str(refused.value) and value not in refused.value.names


# --- every component against a disposable PostgreSQL ----------------------------------------------


@pytest.fixture(scope="module")
def service():
    cluster = cloud_fixtures.start(cloud_fixtures.new_root())
    try:
        yield cluster
    finally:
        cloud_fixtures.stop(cluster)


@pytest.fixture
def stack(service, tmp_path):
    """The compose stack's environment on the disposable cluster: the cluster's superuser
    password in the admin secret folder (as the db service reads it), role secrets generated."""
    service.reset()
    env = {"CATALYST_ADMIN_SECRET_DIR": str(tmp_path / "admin"),
           "CATALYST_ROLES_SECRET_DIR": str(tmp_path / "roles"),
           "CATALYST_DB_HOST": service.socket, "CATALYST_DB_PORT": str(service.port)}
    admin = tmp_path / "admin"
    admin.mkdir()
    (admin / local_stack.ADMIN_FILE).write_text(service.admin_password + "\n")
    local_stack.generate_secrets(env)
    return env


@pytest.fixture
def registry():
    saved, loaded = dict(strategies.REGISTRY), strategies._PLUGINS["loaded"]
    yield strategies.REGISTRY
    strategies.REGISTRY.clear()
    strategies.REGISTRY.update(saved)
    strategies._PLUGINS["loaded"] = loaded


def test_provision_creates_a_local_compose_ledger_once(stack, service):
    first = local_stack.provision_ledger(stack)
    assert first["result"] == "PROVISIONED" and first["platform"] == "LOCAL_COMPOSE"
    assert first["schema_version"] == SCHEMA_VERSION
    again = local_stack.provision_ledger(stack)
    assert again == {"result": "ALREADY_PROVISIONED", "platform": "LOCAL_COMPOSE",
                     "ledger_id": first["ledger_id"], "schema_version": SCHEMA_VERSION}
    identity = local_stack.local_identity(local_stack.role_url(stack, "catalyst_risk"))
    assert identity["platform"] == "LOCAL_COMPOSE" and identity["role"] == "ACCOUNT_LEDGER"
    # The app containers' roles log in with their generated passwords; none is a superuser.
    for role in ("catalyst_risk", "catalyst_app", "catalyst_public", "catalyst_jev"):
        with psycopg.connect(local_stack.role_url(stack, role)) as conn:
            row = conn.execute("""SELECT current_user::text, rolsuper FROM pg_roles
                WHERE rolname = current_user""").fetchone()
        assert row == (role, False)
    # The page's role reads only the public views (the page's own startup check).
    from catalyst_lab import experiment_page

    with experiment_page.connect(local_stack.role_url(stack, "catalyst_public")) as conn:
        experiment_page.verify_role(conn)


def test_a_ledger_that_is_not_the_local_stacks_is_refused(stack, service):
    # Provisioned by the cloud path (platform RAILWAY): the local stack must not adopt it.
    passwords = {variable: local_stack.read_secret(local_stack.role_password_file(stack, role))
                 for role, variable in cloud_provision.LOGIN_ROLES.items()}
    cloud_provision.provision(service.admin_url, passwords)
    with pytest.raises(LocalStackError, match="LOCAL_STACK_LEDGER_NOT_LOCAL"):
        local_stack.provision_ledger(stack)
    with pytest.raises(LocalStackError, match="LOCAL_STACK_LEDGER_NOT_LOCAL"):
        local_stack.simulated_venue({**stack, "CATALYST_SIM_BARS": "synthetic"}, passes=1,
                                    serve_health=False)


def test_the_simulated_venue_records_shadow_signals_and_never_an_order(stack, registry,
                                                                        capsys):
    local_stack.provision_ledger(stack)
    env = {**stack, "CATALYST_SIM_BARS": "synthetic", "CATALYST_SIM_SYMBOLS": "BTC/USD,ETH/USD",
           "CATALYST_STRATEGY_PLUGINS_DIR": str(EXAMPLES)}
    strategies._PLUGINS["loaded"] = None
    health = local_stack.simulated_venue(env, clock=lambda: NOW, sleep=lambda s: None,
                                         passes=1, serve_health=False)
    assert health["status"] == "OK" and health["passes"] == 1
    assert health["orders"] == "NONE_SIMULATED_VENUE" and health["symbols"] == ["BTC/USD",
                                                                               "ETH/USD"]
    assert {"BREAKOUT_7D_VOL2X_V1", "EXAMPLE_MA_CROSS_V1", "EXAMPLE_DONCHIAN_BREAKOUT_V1",
            "EXAMPLE_RSI_REVERSION_V1"} <= set(health["strategies"])
    assert health["last"]["signals"] > 0 and health["last"]["outcomes"] > 0
    url = local_stack.role_url(stack, "catalyst_app")
    with psycopg.connect(url) as conn:
        kinds = dict(conn.execute("""SELECT kind, count(*) FROM lab.managed_events
            GROUP BY kind""").fetchall())
        sources = {row[0] for row in conn.execute("""SELECT body->'bars'->>'source'
            FROM lab.managed_events WHERE kind = 'STRATEGY_SHADOW_SIGNAL'""")}
        orders = sum(conn.execute(f"SELECT count(*) FROM lab.{table}").fetchone()[0]
                     for table in ("managed_setups", "managed_risk_decisions",
                                   "managed_reservations", "managed_fills"))
    assert kinds["STRATEGY_SHADOW_SIGNAL"] > 0 and kinds["STRATEGY_SHADOW_OUTCOME"] > 0
    assert sources == {"SYNTHETIC_SAMPLE_BARS_V1"} and orders == 0
    # A second pass at the same time records nothing twice.
    again = local_stack.simulated_venue(env, clock=lambda: NOW, sleep=lambda s: None,
                                        passes=1, serve_health=False)
    assert again["last"]["signals"] == 0 and again["last"]["outcomes"] == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert lines[0]["event"] == "SIMULATED_VENUE_STARTED" and lines[0]["orders"] == "NONE"
    # The owner's scorecard shows the shadow cells.
    out = []

    class Out:
        def write(self, text):
            out.append(text)

    assert local_stack.scorecard(env, now=NOW + timedelta(days=1), out=Out()) == 0
    printed = "".join(out)
    assert "Strategies in shadow" in printed and "EXAMPLE_MA_CROSS_V1" in printed


def test_promotion_on_the_local_ledger_refuses_a_synthetic_history_report(stack, registry,
                                                                          tmp_path):
    from argparse import Namespace

    local_stack.provision_ledger(stack)
    strategies.load_plugins(folder=EXAMPLES, entry_points=False, strict=True)
    report = tmp_path / "results.json"
    report.write_text(json.dumps({"inputs": {"source": "SYNTHETIC_SAMPLE_BARS_V1"},
                                  "strategies": {"EXAMPLE_MA_CROSS_V1": {}}}))
    args = Namespace(command="promote-strategy", strategy="EXAMPLE_MA_CROSS_V1",
                     history_report=str(report), owner_ruling="LOCAL TEST RULING",
                     owner_override_reason="a fixture override that must not help")
    with pytest.raises(LocalStackError, match="PROMOTION_HISTORY_REPORT_SYNTHETIC"):
        local_stack.strategy_ladder(stack, args, out=open(os.devnull, "w"))


def test_the_alpaca_paper_profile_checks_its_configuration_on_the_local_ledger(stack):
    local_stack.provision_ledger(stack)
    env = {**stack, "CATALYST_VENUE": "alpaca-paper", **PAPER_KEY}
    summary = local_stack.trader(env, check_only=True)
    assert summary["venue"] == "ALPACA_PAPER" and summary["ai_mode"] == "NO_AI_MODE_V1"
    assert summary["ledger_id"]
    started = {}
    process = {}
    local_stack.alpaca_paper(env, build=lambda **kw: (started.update(kw) or "app", "settings"),
                             serve=lambda app, settings, host: started.update(host=host),
                             process_environ=process)
    assert started == {"health_check_database": False, "host": "127.0.0.1"}
    assert process["CATALYST_AI_MODE"] == "NO_AI_MODE_V1"
    assert "CATALYST_AI_MODE" not in os.environ  # The test process is untouched.


def test_the_jobs_run_once_on_the_local_ledger(stack, registry):
    local_stack.provision_ledger(stack)
    strategies._PLUGINS["loaded"] = None
    results = local_stack.jobs({**stack, "CATALYST_SIM_BARS": "synthetic"}, now=NOW)
    assert [r.name for r in results][0] == "shadow_outcomes"
    assert results[0].result == "OK"


def test_the_health_command(monkeypatch):
    assert local_stack.health("http://127.0.0.1:9/health") == 1
    with pytest.raises(SystemExit) as code:
        local_stack.main(["health", "http://127.0.0.1:9/health"], environ={})
    assert code.value.code == 1


def test_main_reports_a_refusal_as_a_code(capsys):
    with pytest.raises(SystemExit) as code:
        local_stack.main(["trader", "--check"], environ={"CATALYST_VENUE": "live"})
    assert code.value.code == 2
    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == "REFUSED" and line["code"] == "LOCAL_STACK_VENUE_UNKNOWN"
