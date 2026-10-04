""".railway/railway.ts: evaluated with Node against a minimal stand-in of ``railway/iac``.

The stand-in records each call's input exactly as the file wrote it, so these tests check the
file's own declarations (and that the Python cloud profile accepts them), not Railway itself.
The real SDK evaluation and a strict type check against its published types are recorded in
artifacts/cloud-2026-09-27/. Skipped where Node 22 or newer is not installed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from catalyst_lab import cloud_config, cloud_provision
from catalyst_lab.audit import credential_findings

CHECKOUT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((CHECKOUT / "deploy/private-paper.example.json").read_text())["environment"]
STUB = """
export const defineRailway = (program) => program;
export const project = (name, definition) =>
  ({ name, ...definition, resources: (definition.resources ?? []).flat() });
export const preserve = () => ({ type: "preserve" });
export const volume = (name, config = {}) => ({ address: `volume.${name}`, name, config });
export const postgres = (name) => ({ address: `database.${name}`, name });
export const service = (name, config = {}) => ({ address: `service.${name}`, name, config });
"""
RUNNER = """
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[2]).href);
const project = await mod.default({ environment: "production" });
process.stdout.write(JSON.stringify(project));
"""
EXTERNAL = {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY"}
PRESERVED = {"type": "preserve"}


def node():
    path = shutil.which("node")
    if not path:
        pytest.skip("node is not installed")
    version = subprocess.run([path, "--version"], capture_output=True, text=True).stdout
    if int(version.strip().lstrip("v").split(".")[0]) < 22:
        pytest.skip("the Railway CLI evaluates railway.ts with Node 22 or newer")
    return path


def evaluate(tmp_path, provision=None, spec=None, extra_env=None):
    """The CLI's evaluation shape: node --experimental-strip-types, ESM, the file's own dir.
    ``spec`` replaces the checkout's railway.ts text (an owner's edit of it); ``extra_env`` is
    the owner's shell (the migration's CATALYST_MIGRATE_* values)."""
    root = tmp_path / "checkout"
    (root / ".railway").mkdir(parents=True)
    (root / "deploy").mkdir()
    shutil.copy(CHECKOUT / ".railway" / "railway.ts", root / ".railway" / "railway.ts")
    if spec is not None:
        (root / ".railway" / "railway.ts").write_text(spec)
    shutil.copy(CHECKOUT / ".railway" / "package.json", root / ".railway" / "package.json")
    shutil.copy(CHECKOUT / "deploy" / "private-paper.example.json", root / "deploy")
    stub = root / ".railway" / "node_modules" / "railway"
    (stub / "dist").mkdir(parents=True)
    (stub / "package.json").write_text(json.dumps(
        {"name": "railway", "type": "module", "exports": {"./iac": "./dist/iac.js"}}))
    (stub / "dist" / "iac.js").write_text(STUB)
    (root / "run.mjs").write_text(RUNNER)
    env = {"PATH": "/usr/bin:/bin", **(extra_env or {})}
    if provision is not None:
        env["CATALYST_PROVISION"] = provision
    result = subprocess.run(
        [node(), "--experimental-strip-types", "--disable-warning=ExperimentalWarning",
         str(root / "run.mjs"), str(root / ".railway" / "railway.ts")],
        capture_output=True, text=True, env=env, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])  # Node's message precedes its stack trace.
    project = json.loads(result.stdout)
    return {item["address"]: item for item in project["resources"]}, project


def service_env(resources, name):
    return resources["service." + name]["config"]["env"]


def resolve(value, secrets):
    """Railway's reference substitution, for the values a deployment would see."""
    domains = {"postgres": "postgres.railway.internal", "trader": "trader.railway.internal"}

    def one(match):
        ref = match.group(1)
        if "." in ref:
            service, variable = ref.split(".", 1)
            if variable == "RAILWAY_PRIVATE_DOMAIN":
                return domains[service]
            return secrets[variable]
        return secrets[ref]

    return re.sub(r"\$\{\{([A-Za-z0-9_.]+)\}\}", one, value)


def deployed_env(env, secrets):
    resolved = {k: secrets[k] if v == PRESERVED else resolve(v, secrets) for k, v in env.items()
                if v != PRESERVED or k in secrets}
    return {**resolved, "RAILWAY_ENVIRONMENT_ID": "fixture-environment",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data"}


@pytest.fixture
def secrets():
    values = {name: f"fixture-{name.lower().replace('_', '-')}-0123456789abcdefghijklmnop"
              for name in ("RISK_DATABASE_PASSWORD", "JEV_DATABASE_PASSWORD",
                           "APP_DATABASE_PASSWORD", "BACKUP_DATABASE_PASSWORD",
                           "OPERATOR_DATABASE_PASSWORD", "PUBLIC_DATABASE_PASSWORD",
                           "MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN", "MANAGED_OPERATOR_TOKEN",
                           "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY")}
    values.update(APCA_API_KEY_ID="PKFIXTURE000000000001",
                  MANAGED_AGENT_TOKENS_JSON=json.dumps(
                      {"muse": "fixture-muse-agent-token-0123456789abcdefghijkl"}))
    return values


def test_four_services_one_database_two_volumes_and_no_provisioner_by_default(tmp_path):
    resources, project = evaluate(tmp_path)
    assert project["name"] == "catalyst-retest-lab"
    # Package learning-app added the jobs cron service beside the three running services.
    assert set(resources) == {"database.postgres", "volume.trader-state", "volume.ops-state",
                              "service.trader", "service.ops", "service.experiment",
                              "service.jobs"}
    for mode in ("initial", "rotate-passwords"):
        with_provision, _ = evaluate(tmp_path / mode, provision=mode)
        config = with_provision["service.provision"]["config"]
        assert config["start"] == f"python -m catalyst_lab.cloud_entry provision {mode}"
        assert config["deploy"] == {"restartPolicyType": "NEVER"}
    with pytest.raises(RuntimeError, match="CATALYST_PROVISION must be"):
        evaluate(tmp_path / "bogus", provision="drop-database")


MIGRATE_ENV = {"CATALYST_MIGRATE_EXPECT_CURRENT": "24", "CATALYST_MIGRATE_TARGET": "25",
               "CATALYST_MIGRATE_BACKUP": f"20260929T010203Z.24.1234.{'c' * 64}.{'d' * 64}"}


def test_the_migration_provisioner_takes_the_owners_three_values_and_no_password(tmp_path):
    """Package cloud-migrate (RAILWAY-DEPLOYMENT.md 7.8): `CATALYST_PROVISION=migrate` plus the
    two versions and the backup reference from the owner's shell; the start command carries
    them, the entrypoint accepts exactly that command, and the service gets the admin
    connection only. Nothing else in the project changes with the mode."""
    from catalyst_lab import cloud_entry

    resources, _ = evaluate(tmp_path / "migrate", provision="migrate", extra_env=MIGRATE_ENV)
    config = resources["service.provision"]["config"]
    assert config["start"] == (
        "python -m catalyst_lab.cloud_entry provision migrate --expect-current 24 --target 25 "
        "--backup " + MIGRATE_ENV["CATALYST_MIGRATE_BACKUP"])
    assert config["env"] == {"MIGRATION_DATABASE_URL": "${{postgres.DATABASE_URL}}?sslmode=require"}
    assert config["deploy"] == {"restartPolicyType": "NEVER"} and config["replicas"] == 1
    assert config["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile.managed"}
    component, argv = cloud_entry.command(config["start"].split()[3:])
    assert component == "provision" and argv[2:] == [
        "catalyst_lab.cloud_provision", "migrate", "--expect-current", "24", "--target", "25",
        "--backup", MIGRATE_ENV["CATALYST_MIGRATE_BACKUP"]]
    plain, _ = evaluate(tmp_path / "plain", extra_env=MIGRATE_ENV)  # Ignored without the mode.
    assert "service.provision" not in plain
    assert {k: v for k, v in resources.items() if k != "service.provision"} == plain
    for index, broken in enumerate((
            {**MIGRATE_ENV, "CATALYST_MIGRATE_BACKUP": ""},
            {**MIGRATE_ENV, "CATALYST_MIGRATE_BACKUP": "/data/backups/20260929T010203Z"},
            {**MIGRATE_ENV, "CATALYST_MIGRATE_TARGET": "25 --expect-current 1"},
            {**MIGRATE_ENV, "CATALYST_MIGRATE_EXPECT_CURRENT": "024"},
            {k: v for k, v in MIGRATE_ENV.items() if k != "CATALYST_MIGRATE_EXPECT_CURRENT"})):
        with pytest.raises(RuntimeError, match="CATALYST_PROVISION=migrate needs"):
            evaluate(tmp_path / f"broken-{index}", provision="migrate", extra_env=broken)


def test_either_approved_review_policy_passes_the_cloud_profile(tmp_path, secrets):
    """The deploy example stays on JEV_LIVE_REVIEW_POLICY_V1 until the owner-run migration 025
    and its release are in; the switch (RAILWAY-DEPLOYMENT.md 7.8, step 7) is then one committed
    change of this variable, which the spec and the cloud profile accept either way. Anything
    between V1 and V2 is refused at startup."""
    from catalyst_lab.review_config import APPROVED_GATE1, APPROVED_GATE1_V2

    resources, _ = evaluate(tmp_path)
    env = service_env(resources, "trader")
    assert json.loads(env["JEV_REVIEW_POLICY_JSON"]) in (APPROVED_GATE1, APPROVED_GATE1_V2)
    places = {"cwd": tmp_path, "release_root": tmp_path}
    for policy in (APPROVED_GATE1, APPROVED_GATE1_V2):
        switched = {**env, "JEV_REVIEW_POLICY_JSON": json.dumps(policy)}
        cloud_config.trader_config(deployed_env(switched, secrets), **places)
    between = {**env, "JEV_REVIEW_POLICY_JSON": json.dumps(
        {**APPROVED_GATE1_V2, "batch_timeout_seconds": 5})}
    with pytest.raises(cloud_config.CloudConfigError) as refused:
        cloud_config.trader_config(deployed_env(between, secrets), **places)
    assert (refused.value.code, refused.value.names) == (
        "CLOUD_ENGINE_SETTING_INVALID", ("MANAGED_APP_OR_REVIEW_CONFIGURATION",))


def test_trader_is_one_executor_with_its_volume_health_and_restart_semantics(tmp_path):
    resources, _ = evaluate(tmp_path)
    trader = resources["service.trader"]["config"]
    assert trader["replicas"] == 1 and trader["healthcheck"] == "/health"
    # Railway's default restart policy (ON_FAILURE, 10 retries) is left undeclared: Railway
    # stores a default as null, and a declared default plans as a change on every apply.
    assert trader["deploy"] == {"restartPolicyMaxRetries": 100, "overlapSeconds": 0,
                                "drainingSeconds": 30}
    for name in ("trader-state", "ops-state"):  # Pinned: an unset region plans destructively.
        assert resources[f"volume.{name}"]["config"]["region"] == "us-east4-eqdc4a"
    assert trader["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile.managed"}
    assert trader["start"] == "python -m catalyst_lab.cloud_entry trader"
    assert trader["volumeMounts"]["/data"]["address"] == "volume.trader-state"
    ops = resources["service.ops"]["config"]
    assert ops["replicas"] == 1 and ops["deploy"] == trader["deploy"]
    assert ops["volumeMounts"]["/data"]["address"] == "volume.ops-state"
    assert "healthcheck" not in ops  # A worker: no port, no deployment healthcheck.


def test_trader_engine_settings_are_the_deploy_example_with_four_cloud_overrides(tmp_path):
    env = service_env(evaluate(tmp_path)[0], "trader")
    # Jev monitors every trade every minute in the cloud (owner decision); the Mac example
    # keeps its staging value. The guide's quick switch (section 9) commits DISABLED in
    # railway.ts, so the file may hold either setting, and only those two.
    overrides = {"CATALYST_ENVIRONMENT": "railway", "MANAGED_HTTP_PORT": "8080",
                 "JEV_CREDENTIAL_SLOT": "paper-railway"}
    assert EXAMPLE["MANAGED_MANAGEMENT_REVIEWS"] == "DISABLED"
    assert env["MANAGED_MANAGEMENT_REVIEWS"] in {"ENABLED", "DISABLED"}
    for name, value in EXAMPLE.items():
        if name in {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_ENV_FILE",
                    "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL",
                    "MANAGED_MANAGEMENT_REVIEWS"}:
            continue
        assert env[name] == overrides.get(name, value), name
    assert "TYPESAFE_ENV_FILE" not in env and env["PORT"] == env["MANAGED_HTTP_PORT"]


@pytest.mark.parametrize("setting", ["ENABLED", "DISABLED"])
def test_either_reviews_setting_in_railway_ts_passes_the_spec_and_the_cloud_profile(
        tmp_path, secrets, setting):
    """Section 9's quick switch edits exactly this literal; the suite and the trader's own
    startup checks accept both values, and only those (package cloud-hardening)."""
    text = (CHECKOUT / ".railway" / "railway.ts").read_text()
    current = re.findall(r'MANAGED_MANAGEMENT_REVIEWS: "(ENABLED|DISABLED)",', text)
    assert len(current) == 1
    spec = text.replace(f'MANAGED_MANAGEMENT_REVIEWS: "{current[0]}",',
                        f'MANAGED_MANAGEMENT_REVIEWS: "{setting}",')
    resources, _ = evaluate(tmp_path, spec=spec)
    env = service_env(resources, "trader")
    assert env["MANAGED_MANAGEMENT_REVIEWS"] == setting
    places = {"cwd": tmp_path, "release_root": tmp_path}
    cloud_config.trader_config(deployed_env(env, secrets), **places)


def test_every_secret_is_preserved_and_no_literal_looks_like_a_credential(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cloud_secrets_spec", CHECKOUT / "scripts" / "cloud_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resources, _ = evaluate(tmp_path)
    preserved, literals = set(), []
    for address, item in resources.items():
        if not address.startswith("service."):
            continue
        for name, value in item["config"].get("env", {}).items():
            if value == PRESERVED:
                preserved.add((address.removeprefix("service."), name))
            else:
                literals.append(value)
    generated = {target for secret in module.CATALOG for target in secret.targets}
    external = {("trader", name) for name in EXTERNAL}
    notify = {("ops", "CLOUD_NOTIFY_JSON"), ("ops", "NOTIFY_PING_URL")}
    assert preserved == generated | external | notify
    assert credential_findings("\n".join(literals)) == []
    for value in literals:  # Secrets appear only as ${{references}}, never as values.
        for match in re.findall(r":([^@:/]+)@", value):
            assert match.startswith("${{") and match.endswith("}}")


def test_the_spec_passes_the_python_cloud_profile(tmp_path, secrets):
    resources, _ = evaluate(tmp_path)
    places = {"cwd": tmp_path, "release_root": tmp_path}
    trader = cloud_config.trader_config(
        deployed_env(service_env(resources, "trader"), secrets), **places)
    assert trader.runtime.bind_host == "::" and trader.port == 8080
    ops = cloud_config.ops_config(deployed_env(service_env(resources, "ops"), secrets), **places)
    assert ops.status_api == "http://trader.railway.internal:8080"
    assert ops.operator_database_url and ops.backup_retention_days == 14
    jobs = cloud_config.jobs_config(deployed_env(service_env(resources, "jobs"), secrets),
                                    **places)
    assert jobs.on_railway and "catalyst_risk" in jobs.database_url


def test_jobs_is_a_nightly_cron_with_only_the_traders_ledger_connection(tmp_path, secrets):
    """Package learning-app: one run a night at 05:30 UTC (after midnight in New York all
    year), never restarted on failure, no volume, no port, and no broker or Jev key."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from psycopg.conninfo import conninfo_to_dict

    resources, _ = evaluate(tmp_path)
    jobs = resources["service.jobs"]["config"]
    assert jobs["start"] == "python -m catalyst_lab.cloud_entry jobs"
    assert jobs["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile.managed"}
    # NEVER: a failed run exits 1 (package ops-alarms) and waits for the next night; Railway's
    # default ON_FAILURE would restart it at once, up to ten times.
    assert jobs["deploy"] == {"cronSchedule": "30 5 * * *", "restartPolicyType": "NEVER"}
    assert jobs["replicas"] == 1 and "healthcheck" not in jobs and "volumeMounts" not in jobs
    assert jobs["env"] == {
        "CATALYST_ENVIRONMENT": "railway",
        "MANAGED_DATABASE_URL": "postgresql://catalyst_risk:${{trader.RISK_DATABASE_PASSWORD}}"
                                "@${{postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/catalyst_lab"
                                "?sslmode=require",
    }
    parts = conninfo_to_dict(resolve(jobs["env"]["MANAGED_DATABASE_URL"], secrets))
    assert (parts["user"], parts["password"], parts["sslmode"]) == (
        "catalyst_risk", secrets["RISK_DATABASE_PASSWORD"], "require")
    new_york = ZoneInfo("America/New_York")
    for day in (datetime(2026, 7, 1, 5, 30, tzinfo=UTC), datetime(2026, 12, 1, 5, 30, tzinfo=UTC)):
        local = day.astimezone(new_york)
        assert local.date() == day.date() and local.hour in {0, 1}  # 01:30 EDT, 00:30 EST.
    places = {"cwd": tmp_path, "release_root": tmp_path}
    deployed = deployed_env(jobs["env"], secrets)
    cloud_config.jobs_config(deployed, **places)
    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY"):
        with pytest.raises(cloud_config.CloudConfigError) as refused:
            cloud_config.jobs_config({**deployed, name: secrets[name]}, **places)
        assert refused.value.code == "JOBS_MUST_NOT_HOLD_TRADING_SECRETS"


def test_experiment_gets_only_its_read_only_connection(tmp_path):
    from psycopg.conninfo import conninfo_to_dict

    resources, _ = evaluate(tmp_path)
    experiment = resources["service.experiment"]["config"]
    assert list(experiment["env"]) == ["EXPERIMENT_DATABASE_URL",
                                       "EXPERIMENT_RESEARCH_SCHEDULE_JSON"]
    # "Next run" follows the engine's own schedule by reference, never a second copy.
    assert experiment["env"]["EXPERIMENT_RESEARCH_SCHEDULE_JSON"] == (
        "${{trader.MANAGED_RESEARCH_SCHEDULE_JSON}}")
    assert "MANAGED_RESEARCH_SCHEDULE_JSON" in resources["service.trader"]["config"]["env"]
    parts = conninfo_to_dict(resolve(experiment["env"]["EXPERIMENT_DATABASE_URL"],
                                     {"PUBLIC_DATABASE_PASSWORD": "x" * 40}))
    assert (parts["user"], parts["dbname"], parts["sslmode"]) == (
        "catalyst_public", "catalyst_lab", "require")
    assert experiment["build"]["dockerfilePath"] == "Dockerfile.experiment"
    assert experiment["replicas"] == 1 and experiment["healthcheck"] == "/health"
    assert "$PORT" not in experiment["start"]  # Exec form: no shell would expand it.
    for service in ("ops", "experiment"):
        names = set(service_env(resources, service))
        assert not names & (EXTERNAL | {"MANAGED_API_TOKEN", "MANAGED_OPERATOR_TOKEN",
                                        "MANAGED_AGENT_TOKENS_JSON", "RISK_DATABASE_PASSWORD"})


def test_the_provisioner_reads_each_password_where_it_lives(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cloud_secrets_spec2", CHECKOUT / "scripts" / "cloud_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = {name: service for secret in module.CATALOG
            for service, name in secret.targets if name.endswith("_DATABASE_PASSWORD")}
    resources, _ = evaluate(tmp_path, provision="initial")
    env = resources["service.provision"]["config"]["env"]
    assert env["MIGRATION_DATABASE_URL"] == "${{postgres.DATABASE_URL}}?sslmode=require"
    assert set(env) - {"MIGRATION_DATABASE_URL"} == set(cloud_provision.LOGIN_ROLES.values())
    for name in cloud_provision.LOGIN_ROLES.values():
        assert env[name] == "${{" + home[name] + "." + name + "}}"


def test_the_sdk_pin_and_the_image_postgres_client_agree():
    package = json.loads((CHECKOUT / ".railway" / "package.json").read_text())
    assert package["dependencies"] == {"railway": "3.11.0"}  # Exact: its postgres() is :18.
    dockerfile = (CHECKOUT / "Dockerfile.managed").read_text()
    assert "ARG PG_MAJOR=18" in dockerfile
    assert "postgres-ssl:18" in (CHECKOUT / ".railway" / "railway.ts").read_text()


def test_the_trader_gets_the_monthly_jev_budget_from_the_example(tmp_path, secrets):
    """Package jev-budget: the budget and its price estimate reach the trader through the
    example's derivation (no cloud override), and the cloud profile requires them."""
    resources, _ = evaluate(tmp_path)
    env = service_env(resources, "trader")
    budget = {name: env[name] for name in ("JEV_MONTHLY_BUDGET_USD",
                                           "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD",
                                           "JEV_BYTES_PER_TOKEN")}
    assert budget == {"JEV_MONTHLY_BUDGET_USD": "50",
                      "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD": "0.042",
                      "JEV_BYTES_PER_TOKEN": "3"}
    assert budget == {name: EXAMPLE[name] for name in budget}
    places = {"cwd": tmp_path, "release_root": tmp_path}
    deployed = deployed_env(env, secrets)
    cloud_config.trader_config(deployed, **places)
    for name in budget:
        with pytest.raises(cloud_config.CloudConfigError) as refused:
            cloud_config.trader_config({k: v for k, v in deployed.items() if k != name},
                                       **places)
        assert (refused.value.code, refused.value.names) == ("CLOUD_ENGINE_SETTING_MISSING",
                                                             (name,))
    assert not set(budget) & set(service_env(resources, "ops"))  # Ops reads the status.


def test_the_trader_gets_the_trade_window_from_the_example(tmp_path, secrets):
    """Package review-window: MANAGED_CRYPTO_WINDOW_JSON reaches the trader through the
    example's derivation (no cloud override, no railway.ts change needed), and the cloud
    profile requires it."""
    resources, _ = evaluate(tmp_path)
    env = service_env(resources, "trader")
    name = "MANAGED_CRYPTO_WINDOW_JSON"
    assert env[name] == EXAMPLE[name]
    assert json.loads(env[name]) == {"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}
    places = {"cwd": tmp_path, "release_root": tmp_path}
    deployed = deployed_env(env, secrets)
    cloud_config.trader_config(deployed, **places)
    with pytest.raises(cloud_config.CloudConfigError) as refused:
        cloud_config.trader_config({k: v for k, v in deployed.items() if k != name}, **places)
    assert (refused.value.code, refused.value.names) == ("CLOUD_ENGINE_SETTING_MISSING",
                                                         (name,))
    assert name not in service_env(resources, "ops")  # Only the trader admits trades.


def test_the_cloud_trader_applies_no_crypto_liquidity_gate(tmp_path):
    """Owner decision 2026-09-25 (CONTRACT-RESOLUTIONS): the crypto liquidity gate is not applied
    on the Alpaca paper venue. The first Railway deploy (2026-09-28) carried it over from the
    deploy example, required it, and refused every entry it saw."""
    from catalyst_lab import cloud_config

    example = json.loads((CHECKOUT / "deploy" / "private-paper.example.json").read_text())
    assert "MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON" not in example["environment"]
    assert "MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON" not in cloud_config.CLOUD_REQUIRED_OPTIONAL
    resources, _ = evaluate(tmp_path)
    assert "MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON" not in service_env(resources, "trader")
