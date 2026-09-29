"""The one-off cloud provisioner against a disposable Railway-shaped PostgreSQL (package cloud).

Private Unix socket, SCRAM authentication, ``log_statement = 'all'``: no TCP, no Railway, no
owner ledger.
"""

import json
from datetime import UTC, datetime

import psycopg
import pytest

from catalyst_lab import cloud_provision, localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.managed_ops import readonly_audit_repository
from catalyst_lab.repository import Repository
from tests import cloud_fixtures

NOW = datetime(2026, 9, 27, 22, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def service():
    cluster = cloud_fixtures.start(cloud_fixtures.new_root())
    try:
        yield cluster
    finally:
        cloud_fixtures.stop(cluster)


@pytest.fixture
def pg(service):
    service.reset()
    return service


@pytest.fixture
def secrets_env():
    return cloud_fixtures.cloud_passwords(cloud_provision.login_roles())


def provisioned(pg, secrets_env):
    return cloud_provision.provision(pg.admin_url, secrets_env, now=NOW)


def can_login(pg, role, secret):
    try:
        with psycopg.connect(pg.url(role, secret), connect_timeout=5) as conn:
            return conn.execute("SELECT current_user::text").fetchone()[0] == role
    except psycopg.OperationalError:
        return False


def test_empty_service_is_provisioned_in_one_audited_step(pg, secrets_env):
    result = provisioned(pg, secrets_env)
    version = max(localdb.migration_files())
    roles = cloud_provision.login_roles()
    assert result["result"] == "PROVISIONED" and result["schema_version"] == version
    assert result["login_roles"] == sorted(roles)
    assert set(result["nologin_roles"]) == cloud_provision.lab_roles() - set(roles)
    assert result["broker_requests"] == 0
    for role, name in roles.items():
        assert can_login(pg, role, secrets_env[name]), role
        assert not can_login(pg, role, "x" * 40)
    for role in result["nologin_roles"]:
        assert not can_login(pg, role, secrets_env["APP_DATABASE_PASSWORD"])
    with pg.admin("catalyst_lab") as conn:
        owner = conn.execute(
            "SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname='lab_owner'").fetchone()
        assert owner == (True, False)  # The schema owner exists, but nobody can log in as it.
        assert conn.execute(
            "SELECT datcollate, datctype FROM pg_database WHERE datname='catalyst_lab'"
        ).fetchone() == ("C", "C")
        # The backup role reads the ledger's relations only: no predefined role, and every
        # relation of the database is in lab (so pg_dump never meets one it cannot read).
        assert not conn.execute("""SELECT pg_has_role('catalyst_backup', 'pg_read_all_data',
            'MEMBER')""").fetchone()[0]
        outside = conn.execute("""SELECT n.nspname::text, c.relname::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p','S','m','f')
            AND n.nspname NOT IN ('lab', 'information_schema') AND n.nspname !~ '^pg_'
            """).fetchall()
        assert outside == []
        unreadable = conn.execute("""SELECT c.relname::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'lab'
            AND c.relkind IN ('r','p','S') AND NOT has_table_privilege(
            'catalyst_backup', c.oid, 'SELECT')""").fetchall()
        assert unreadable == []
        defaults = {row[0] for row in conn.execute("""SELECT d.defaclobjtype::text
            FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace
            WHERE n.nspname = 'lab' AND pg_get_userbyid(d.defaclrole) = 'lab_owner'
            AND array_to_string(d.defaclacl, ',') LIKE '%catalyst_backup=r/%'""")}
        assert defaults == {"r", "S"}  # Later tables and sequences are readable too.
        settings = conn.execute("""SELECT setconfig FROM pg_db_role_setting s
            JOIN pg_database d ON d.oid = s.setdatabase WHERE d.datname='catalyst_lab'
            AND s.setrole = 0""").fetchone()[0]
        assert set(settings) == {"tcp_keepalives_idle=30", "tcp_keepalives_interval=10",
                                 "tcp_keepalives_count=3"}
    # The restricted services' own role checks pass on the provisioned ledger.
    app_url = pg.url("catalyst_app", secrets_env["APP_DATABASE_PASSWORD"])
    Repository(app_url).check_role()
    readonly_audit_repository(app_url).check_role()
    RiskRepository(pg.url("catalyst_risk", secrets_env["RISK_DATABASE_PASSWORD"])).check_role()
    # The ledger identity is one audited SYSTEM_EVENT on an intact chain.
    events = Repository(app_url).export_events()
    assert verify_events(events)["valid"] and len(events) == result["audit_seq"]
    identity = [e for e in events if e["payload_json"].get("kind") == "CLOUD_LEDGER_PROVISIONED"]
    assert len(identity) == 1 and identity[0]["event_type"] == "SYSTEM_EVENT"
    payload = identity[0]["payload_json"]
    assert payload["role"] == "ACCOUNT_LEDGER" and payload["ledger_id"] == result["ledger_id"]
    assert payload["provisioned_at"] == NOW.isoformat() and payload["owner_login"] is False
    assert identity[0]["event_hash"] == result["audit_head"]
    with psycopg.connect(app_url) as conn:
        assert cloud_provision.ledger_identity(conn)["ledger_id"] == result["ledger_id"]


def test_passwords_never_reach_the_server_log_or_the_output(pg, secrets_env, capsys):
    before = pg.log.read_text() if pg.log.exists() else ""
    cloud_provision.main(["initial"], {"MIGRATION_DATABASE_URL": pg.admin_url, **secrets_env})
    printed = capsys.readouterr().out
    written = pg.log.read_text()[len(before):]
    assert "CREATE DATABASE" in written  # The server really logs every statement here.
    for secret in [*secrets_env.values(), pg.admin_password]:
        assert secret not in written and secret not in printed
    assert "SCRAM-SHA-256$" not in written and "PASSWORD" not in printed.upper()
    assert json.loads(printed)["result"] == "PROVISIONED"


def test_a_provisioned_or_occupied_database_is_refused_and_left_unchanged(pg, secrets_env):
    provisioned(pg, secrets_env)
    with pytest.raises(cloud_provision.ProvisionError) as refused:
        provisioned(pg, secrets_env)
    assert refused.value.code == "PROVISION_ROLES_ALREADY_EXIST"
    pg.reset()
    with pg.admin() as conn:
        conn.execute("CREATE DATABASE catalyst_lab")
    with pg.admin("catalyst_lab") as conn:
        conn.execute("CREATE TABLE public.someone_elses (id int)")
    with pytest.raises(cloud_provision.ProvisionError, match="PROVISION_DATABASE_NOT_EMPTY"):
        provisioned(pg, secrets_env)
    with pg.admin("catalyst_lab") as conn:
        conn.execute("DROP TABLE public.someone_elses")
        conn.execute("CREATE SCHEMA lab")
    with pytest.raises(cloud_provision.ProvisionError, match="PROVISION_LAB_SCHEMA_EXISTS"):
        provisioned(pg, secrets_env)
    with pg.admin("catalyst_lab") as conn:  # The refused transactions created nothing.
        assert not conn.execute("""SELECT count(*) FROM pg_roles
            WHERE rolname LIKE 'catalyst%' OR rolname = 'lab_owner'""").fetchone()[0]


def test_an_empty_leftover_database_from_a_failed_run_is_accepted(pg, secrets_env):
    with pg.admin() as conn:
        conn.execute("CREATE DATABASE catalyst_lab")
    assert provisioned(pg, secrets_env)["result"] == "PROVISIONED"


@pytest.mark.parametrize("change,code", [
    (lambda env: env.pop("RISK_DATABASE_PASSWORD"), "PROVISION_PASSWORD_MISSING"),
    (lambda env: env.update(JEV_DATABASE_PASSWORD="short"), "PROVISION_PASSWORD_INVALID"),
    (lambda env: env.update(APP_DATABASE_PASSWORD="has space " * 5),
     "PROVISION_PASSWORD_INVALID"),
    (lambda env: env.update(APP_DATABASE_PASSWORD="p@ss/word" * 5),
     "PROVISION_PASSWORD_INVALID"),
    (lambda env: env.update(BACKUP_DATABASE_PASSWORD=env["OPERATOR_DATABASE_PASSWORD"]),
     "DISTINCT_DATABASE_PASSWORDS_REQUIRED"),
])
def test_passwords_must_be_present_distinct_and_url_safe(pg, secrets_env, change, code):
    change(secrets_env)
    with pytest.raises(cloud_provision.ProvisionError) as refused:
        provisioned(pg, secrets_env)
    assert refused.value.code == code
    assert all("secret" not in name.lower() for name in refused.value.names)
    with pg.admin() as conn:
        assert not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname='catalyst_lab'").fetchone()


def test_only_a_superuser_connection_provisions(pg, secrets_env):
    with pg.admin() as conn:
        conn.execute("CREATE ROLE not_admin LOGIN CREATEDB PASSWORD 'fixture-not-admin-pw-1234567'")
    try:
        url = pg.url("not_admin", "fixture-not-admin-pw-1234567", "railway")
        with pytest.raises(cloud_provision.ProvisionError, match="PROVISION_SUPERUSER_REQUIRED"):
            cloud_provision.provision(url, secrets_env)
    finally:
        with pg.admin() as conn:
            conn.execute("DROP ROLE not_admin")


def test_password_rotation_replaces_only_verifiers_and_is_audited(pg, secrets_env):
    first = provisioned(pg, secrets_env)
    old = secrets_env["RISK_DATABASE_PASSWORD"]
    rotated = {**secrets_env, "RISK_DATABASE_PASSWORD": cloud_fixtures.password()}
    result = cloud_provision.rotate_passwords(pg.admin_url, rotated, now=NOW)
    assert result["result"] == "ROTATED" and result["ledger_id"] == first["ledger_id"]
    assert not can_login(pg, "catalyst_risk", old)
    assert can_login(pg, "catalyst_risk", rotated["RISK_DATABASE_PASSWORD"])
    assert can_login(pg, "catalyst_jev", secrets_env["JEV_DATABASE_PASSWORD"])  # Unchanged.
    app_url = pg.url("catalyst_app", secrets_env["APP_DATABASE_PASSWORD"])
    events = Repository(app_url).export_events()
    assert verify_events(events)["valid"] and events[-1]["seq"] == result["audit_seq"]
    assert events[-1]["payload_json"]["kind"] == "CLOUD_ROLE_PASSWORDS_ROTATED"
    assert first["audit_seq"] < result["audit_seq"]
    with pytest.raises(cloud_provision.ProvisionError, match="PROVISION_PASSWORD_MISSING"):
        cloud_provision.rotate_passwords(
            pg.admin_url, {k: v for k, v in rotated.items() if k != "APP_DATABASE_PASSWORD"})


def test_rotation_refuses_a_database_that_was_never_provisioned(pg, secrets_env):
    with pg.admin() as conn:
        conn.execute("CREATE DATABASE catalyst_lab")
    with pytest.raises(cloud_provision.ProvisionError, match="CLOUD_LEDGER_IDENTITY_REQUIRED"):
        cloud_provision.rotate_passwords(pg.admin_url, secrets_env)


def test_catalyst_public_is_skipped_until_its_migration_exists(pg, secrets_env, monkeypatch,
                                                              tmp_path):
    result = provisioned(pg, secrets_env)
    if "catalyst_public" in cloud_provision.lab_roles():
        pytest.skip("migration 024 is merged: catalyst_public is provisioned like any role")
    assert result["skipped_roles"] == ["catalyst_public"]
    assert result["notes"] == ["CATALYST_PUBLIC_ROLE_ABSENT_SKIPPED"]
    # With a migration that creates the role NOLOGIN (as package experiment-page's 024 does),
    # it becomes a login role and its password becomes required.
    pg.reset()
    files = dict(localdb.migration_files())
    version = max(files) + 1
    extra = tmp_path / f"{version:03d}_fixture_public_role.sql"
    extra.write_text("CREATE ROLE catalyst_public NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                     "NOREPLICATION;\nGRANT USAGE ON SCHEMA lab TO catalyst_public;\n"
                     f"INSERT INTO lab.schema_migrations(version) VALUES({version});\n")
    monkeypatch.setattr(localdb, "migration_files", lambda: {**files, version: extra})
    monkeypatch.setattr(cloud_provision.localdb, "migration_files",
                        lambda: {**files, version: extra})
    with pytest.raises(cloud_provision.ProvisionError) as refused:
        provisioned(pg, secrets_env)
    assert refused.value.code == "PROVISION_PASSWORD_MISSING"
    assert refused.value.names == ("PUBLIC_DATABASE_PASSWORD",)
    secrets_env["PUBLIC_DATABASE_PASSWORD"] = cloud_fixtures.password()
    result = provisioned(pg, secrets_env)
    assert "catalyst_public" in result["login_roles"] and result["skipped_roles"] == []
    assert can_login(pg, "catalyst_public", secrets_env["PUBLIC_DATABASE_PASSWORD"])


def test_cli_refusals_name_codes_and_variables_only(pg, secrets_env):
    with pytest.raises(SystemExit, match="CLOUD_PROVISION_REFUSED: MIGRATION_DATABASE_URL"):
        cloud_provision.main(["initial"], dict(secrets_env))
    env = {"MIGRATION_DATABASE_URL": pg.admin_url, **secrets_env}
    env.pop("OPERATOR_DATABASE_PASSWORD")
    with pytest.raises(SystemExit) as refused:
        cloud_provision.main(["initial"], env)
    assert str(refused.value.code) == (
        "CLOUD_PROVISION_REFUSED: PROVISION_PASSWORD_MISSING OPERATOR_DATABASE_PASSWORD")
    wrong = {"MIGRATION_DATABASE_URL": pg.url("postgres", "wrong-password-" * 3, "railway"),
             **secrets_env}
    with pytest.raises(SystemExit) as failed:
        cloud_provision.main(["initial"], wrong)
    assert str(failed.value.code) == "CLOUD_PROVISION_REFUSED: PROVISION_DATABASE_UNAVAILABLE"


def test_a_login_role_that_can_read_pg_authid_is_refused(pg, secrets_env):
    """The review's finding, reproduced: pg_read_all_data reads the SCRAM verifiers; the
    provisioner's role check refuses any login role that can."""
    provisioned(pg, secrets_env)
    backup = pg.url("catalyst_backup", secrets_env["BACKUP_DATABASE_PASSWORD"])
    with psycopg.connect(backup) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT rolpassword FROM pg_catalog.pg_authid")
    with pg.admin("catalyst_lab") as admin:
        admin.execute("GRANT pg_read_all_data TO catalyst_backup")
        with psycopg.connect(backup) as conn:  # What the membership used to allow.
            assert conn.execute("""SELECT count(*) FROM pg_catalog.pg_authid
                WHERE rolpassword LIKE 'SCRAM-SHA-256$%'""").fetchone()[0] >= 6
        with pytest.raises(cloud_provision.ProvisionError) as refused:
            cloud_provision._check_login_roles(admin, {"catalyst_backup", "catalyst_app"})
        assert (refused.value.code, refused.value.names) == (
            "PROVISION_ROLE_TOO_BROAD", ("catalyst_backup",))
        admin.execute("REVOKE pg_read_all_data FROM catalyst_backup")
        cloud_provision._check_login_roles(admin, set(cloud_provision.login_roles()))
