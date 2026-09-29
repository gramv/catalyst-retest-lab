"""The owner's guarded cloud migration (``cloud_provision migrate``) and the ops shell's fresh
backup it names (``cloud_runtime backup``), package cloud-migrate.

A disposable Railway-shaped cluster (SCRAM on a private socket, ``log_statement = 'all'``)
provisioned at schema 24, exactly as the live cloud ledger was: no Railway, no network, no
owner ledger. The success case is the owner's whole flow, 24 to 25 with the real migration 025;
every refusal leaves the ledger exactly as it was.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from catalyst_lab import cloud_entry, cloud_provision, cloud_runtime, ledger_ops, localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.config import SCHEMA_VERSION
from catalyst_lab.execution import system_event
from catalyst_lab.jev_store import JevStore
from catalyst_lab.repository import Repository
from catalyst_lab.review_config import APPROVED_GATE1_V2, Gate1Inputs
from catalyst_lab.review_runtime import ClockSample, Gate1Runtime
from tests import cloud_fixtures
from tests.test_cloud_config import ops_env

REAL_FILES = localdb.migration_files()
MIGRATION_025 = REAL_FILES[25]
NOW = datetime(2026, 9, 28, 5, 14, tzinfo=UTC)


@pytest.fixture(scope="module")
def service():
    cluster = cloud_fixtures.start(cloud_fixtures.new_root())
    try:
        yield cluster
    finally:
        cloud_fixtures.stop(cluster)


def files_through(version):
    return {v: p for v, p in REAL_FILES.items() if v <= version}


@pytest.fixture
def ledger(service, monkeypatch):
    """The cloud ledger as provisioned on 2026-09-28 (schema 24), with some history."""
    service.reset()
    secrets = cloud_fixtures.cloud_passwords(cloud_provision.login_roles(24))
    with monkeypatch.context() as patch:
        patch.setattr(localdb, "migration_files", lambda: files_through(24))
        assert cloud_provision.provision(service.admin_url, secrets, now=NOW)[
            "schema_version"] == 24
    append(service, secrets, 3)
    return service, secrets


@pytest.fixture
def places(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "image").mkdir()
    return {"cwd": tmp_path / "work", "release_root": tmp_path / "image"}


def append(cluster, secrets, count):
    app = Repository(cluster.url("catalyst_app", secrets["APP_DATABASE_PASSWORD"]))
    with app.connect() as conn:  # The normal audited way, as the running trader appends.
        for index in range(count):
            system_event(app, conn, "LAB_FIXTURE_CLOUD_MIGRATE", {"index": index})


def state(cluster):
    """What any refusal must leave exactly as it was."""
    with cluster.admin("catalyst_lab") as conn:
        return conn.execute("""SELECT (SELECT max(version) FROM lab.schema_migrations),
            (SELECT count(*) FROM lab.trade_events),
            (SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1),
            (SELECT count(*) FROM lab.trade_events WHERE payload_json->>'kind'=%s),
            to_regprocedure('lab.store_research_report_v1(jsonb,jsonb)') IS NULL,
            (SELECT count(*) FROM pg_class WHERE relname LIKE 'fixture%%')""",
                            (cloud_provision.MIGRATION_EVENT,)).fetchone()


def live_reference(cluster, *, taken_at=None, schema=24, seq=None, head=None):
    """A reference naming the live head, for the refusals that need no real dump."""
    with cluster.admin("catalyst_lab") as conn:
        live_seq, live_head = conn.execute(
            "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()
    name = (taken_at or datetime.now(UTC)).astimezone(UTC).strftime(ledger_ops.NAME_FORMAT)
    return f"{name}.{schema}.{seq or live_seq}.{head or live_head}.{'a' * 64}"


def refused(ledger, code, reference, *, expect=24, target=25, now=None, names=None):
    cluster, _ = ledger
    before = state(cluster)
    with pytest.raises(cloud_provision.ProvisionError) as refusal:
        cloud_provision.migrate(cluster.admin_url, expect_current=expect, target=target,
                                backup=reference, now=now)
    assert refusal.value.code == code
    if names is not None:
        assert refusal.value.names == tuple(names)
    assert state(cluster) == before  # Nothing applied, nothing appended.


# --- The owner's flow ---------------------------------------------------------------------


def test_the_owner_backs_up_on_ops_then_migrates_24_to_25(ledger, tmp_path, places, capsys):
    cluster, secrets = ledger
    admin_before = state(cluster)
    assert admin_before[:1] == (24,)
    volume = tmp_path / "volume"
    env = ops_env(volume,
                  AUDIT_DATABASE_URL=cluster.url("catalyst_app", secrets["APP_DATABASE_PASSWORD"]),
                  BACKUP_DATABASE_URL=cluster.url("catalyst_backup",
                                                  secrets["BACKUP_DATABASE_PASSWORD"]))
    # Step 1, ops shell: one fresh backup exactly like the daily one, and its reference.
    assert cloud_runtime.backup_now(env, **places) == 0
    backup = json.loads(capsys.readouterr().out)
    reference = backup["migration_backup_reference"]
    assert backup["result"] == "MATCH" and backup["schema_version"] == 24
    assert reference == ".".join(str(backup[k]) for k in (
        "backup", "schema_version", "audit_seq", "audit_head", "manifest_sha256"))
    directory = volume / "backups" / backup["backup"]
    assert ledger_ops.verify_manifest(
        directory, expected_head=backup["audit_head"],
        expected_manifest_sha256=backup["manifest_sha256"])["result"] == "MATCH"
    append(cluster, secrets, 2)  # The trader keeps writing while the owner prepares the step.

    # Step 2, the provision service: the guarded migration with that reference. (A fresh
    # ledger's sequence has no gap, so its event count is its head's sequence.)
    _, count_before, head_before, _, _, _ = state(cluster)
    cloud_provision.main(["migrate", "--expect-current", "24", "--target", "25",
                          "--backup", reference], {"MIGRATION_DATABASE_URL": cluster.admin_url})
    printed = capsys.readouterr().out
    result = json.loads(printed)
    assert (result["result"], result["before"], result["after"]) == ("MIGRATED", 24, 25)
    assert result["migrations"] == [{
        "version": 25, "file": "025_jev_review_policy_v2.sql",
        "sha256": hashlib.sha256(MIGRATION_025.read_bytes()).hexdigest()}]
    assert result["backup"] == {
        "backup": backup["backup"], "taken_at": datetime.strptime(
            backup["backup"], ledger_ops.NAME_FORMAT).replace(tzinfo=UTC).isoformat(),
        "schema_version": 24, "audit_seq": backup["audit_seq"],
        "audit_head": backup["audit_head"], "manifest_sha256": backup["manifest_sha256"]}
    assert result["events_after_backup"] == 2
    assert (result["event_count_before"], result["audit_head_before"]) == (count_before,
                                                                           head_before)
    assert result["event_count"] == count_before + 1 and result["broker_requests"] == 0
    assert result["nologin_roles_added"] == []
    for secret in [cluster.admin_password, *secrets.values()]:
        assert secret not in printed

    # The migrated ledger: one appended, hash-chained CLOUD_LEDGER_MIGRATED event and nothing
    # else; the identity, the roles, the ownership and the backup role's reach unchanged.
    app_url = cluster.url("catalyst_app", secrets["APP_DATABASE_PASSWORD"])
    events = Repository(app_url).export_events()
    assert verify_events(events)["valid"] and len(events) == count_before + 1
    assert events[-1]["event_hash"] == result["audit_head"]
    assert events[-1]["seq"] == result["audit_seq"]
    assert events[-2]["event_hash"] == head_before  # The old head, unchanged at its sequence.
    payload = events[-1]["payload_json"]
    assert payload["kind"] == "CLOUD_LEDGER_MIGRATED" and payload["migrator"] == "CLOUD_MIGRATE_V1"
    with psycopg.connect(app_url) as conn:
        identity = cloud_provision.ledger_identity(conn)
    assert payload["ledger_id"] == identity["ledger_id"] == result["ledger_id"]
    assert (payload["from_version"], payload["to_version"]) == (24, 25)
    assert payload["backup"] == result["backup"] and payload["migrations"] == result["migrations"]
    with cluster.admin("catalyst_lab") as conn:
        cloud_provision._check_ownership(conn)
        cloud_provision._check_login_roles(conn, set(identity["login_roles"])
                                           | cloud_provision.lab_roles(25))
        cloud_provision._check_backup_grants(conn)
    # The trader's roles accept the ledger at this release's schema, and V2 registers there.
    assert SCHEMA_VERSION == 25
    RiskRepository(cluster.url("catalyst_risk", secrets["RISK_DATABASE_PASSWORD"])).check_role()
    store = JevStore(cluster.url("catalyst_jev", secrets["JEV_DATABASE_PASSWORD"]))
    scope = Gate1Runtime(
        store, Gate1Inputs(dict(APPROVED_GATE1_V2)), credential_slot="paper-railway",
        worker_id="00000000-0000-4000-8000-000000000025",
        clock_health=lambda: ClockSample(0, 0, datetime.now(UTC))).scope_id
    assert scope == hashlib.sha256(
        b"TYPESAFE:jev-1.13.0:paper-railway:JEV_LIVE_REVIEW_POLICY_V2").hexdigest()

    # The same step again is refused and changes nothing.
    again = state(cluster)
    with pytest.raises(SystemExit) as second:
        cloud_provision.main(["migrate", "--expect-current", "24", "--target", "25",
                              "--backup", reference],
                             {"MIGRATION_DATABASE_URL": cluster.admin_url})
    assert str(second.value.code) == "CLOUD_MIGRATION_REFUSED: LEDGER_VERSION_MISMATCH"
    assert state(cluster) == again

    # The next daily backup of the migrated ledger restores in the local drill, at schema 25.
    later = ledger_ops.backup_database(
        cluster.url("catalyst_backup", secrets["BACKUP_DATABASE_PASSWORD"]),
        tmp_path / "after", now=datetime.now(UTC) + timedelta(seconds=2))
    drill = ledger_ops.drill(later["backup"], "/tmp",
                             expected_manifest_sha256=later["manifest_sha256"])
    assert drill["result"] == "PASSED" and drill["schema_version"] == 25
    assert drill["checks"]["repository_check_role"] == "PASSED"


def test_a_new_role_from_a_migration_stays_nologin(ledger, tmp_path, monkeypatch):
    """As provisioning left every role the cloud does not log in as; the event names it."""
    fixture = tmp_path / "025_fixture_role.sql"
    fixture.write_text(MIGRATION_025.read_text() + "\nCREATE ROLE catalyst_fixture_reader LOGIN "
                       "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\n")
    monkeypatch.setattr(localdb, "migration_files", lambda: {**files_through(24), 25: fixture})
    cluster, _ = ledger
    result = cloud_provision.migrate(cluster.admin_url, expect_current=24, target=25,
                                     backup=live_reference(cluster))
    assert result["nologin_roles_added"] == ["catalyst_fixture_reader"]
    with cluster.admin("catalyst_lab") as conn:
        assert conn.execute("SELECT rolcanlogin FROM pg_roles "
                            "WHERE rolname='catalyst_fixture_reader'").fetchone() == (False,)


# --- Refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize("expect,target,change,code", [
    (24, 25, lambda r: None, "MIGRATION_BACKUP_REFERENCE_REQUIRED"),
    (24, 25, lambda r: "", "MIGRATION_BACKUP_REFERENCE_REQUIRED"),
    (24, 25, lambda r: r + "0", "MIGRATION_BACKUP_REFERENCE_INVALID"),
    (24, 25, lambda r: r.upper(), "MIGRATION_BACKUP_REFERENCE_INVALID"),
    (24, 25, lambda r: "20261399T000000Z" + r[16:], "MIGRATION_BACKUP_REFERENCE_INVALID"),
    (24, 25, lambda r: "/data/backups/" + r, "MIGRATION_BACKUP_REFERENCE_INVALID"),
    (25, 25, lambda r: r, "LEDGER_TARGET_NOT_AHEAD"),
    (24, 26, lambda r: r, "LEDGER_MIGRATION_FILES_MISSING"),        # An unknown target.
    (23, 24, lambda r: r.replace(".24.", ".23.", 1), "MIGRATION_TARGET_NOT_THIS_RELEASE"),
    (24, 25, lambda r: r.replace(".24.", ".23.", 1), "BACKUP_SCHEMA_MISMATCH"),
])
def test_refusals_before_any_change(ledger, expect, target, change, code):
    refused(ledger, code, change(live_reference(ledger[0])), expect=expect, target=target)


def test_a_wrong_expected_version_is_refused(ledger):
    """The ledger is at 24: a step written for 23 (with a backup at 23) must not run."""
    refused(ledger, "LEDGER_VERSION_MISMATCH", live_reference(ledger[0], schema=23), expect=23)


def test_the_backup_must_name_this_ledgers_own_recent_history(ledger):
    cluster, _ = ledger
    with cluster.admin("catalyst_lab") as conn:
        seq, created = conn.execute("SELECT seq, created_at FROM lab.trade_events "
                                    "ORDER BY seq DESC LIMIT 1").fetchone()
    refused(ledger, "BACKUP_HEAD_NOT_IN_LEDGER", live_reference(cluster, head="b" * 64))
    refused(ledger, "BACKUP_HEAD_NOT_IN_LEDGER", live_reference(cluster, seq=seq + 1))
    # A head written well after the backup's own time cannot be in that backup.
    refused(ledger, "BACKUP_REFERENCE_INCONSISTENT",
            live_reference(cluster, taken_at=created - timedelta(minutes=10)))
    taken = datetime.now(UTC).replace(microsecond=0)
    reference = live_reference(cluster, taken_at=taken)
    refused(ledger, "BACKUP_TOO_OLD", reference,
            now=taken + cloud_provision.BACKUP_MAX_AGE + timedelta(seconds=1))
    refused(ledger, "BACKUP_TAKEN_IN_THE_FUTURE", reference,
            now=taken - cloud_provision.CLOCK_SKEW - timedelta(seconds=1))


def test_only_a_superuser_on_a_provisioned_cloud_ledger_migrates(ledger):
    cluster, secrets = ledger
    reference = live_reference(cluster)
    app_url = cluster.url("catalyst_app", secrets["APP_DATABASE_PASSWORD"], "railway")
    before = state(cluster)
    with pytest.raises(cloud_provision.ProvisionError, match="PROVISION_SUPERUSER_REQUIRED"):
        cloud_provision.migrate(app_url, expect_current=24, target=25, backup=reference)
    assert state(cluster) == before
    cluster.reset()  # An empty database is not a provisioned cloud ledger.
    with cluster.admin() as conn:
        conn.execute("CREATE DATABASE catalyst_lab")
    with pytest.raises(cloud_provision.ProvisionError, match="CLOUD_LEDGER_IDENTITY_REQUIRED"):
        cloud_provision.migrate(cluster.admin_url, expect_current=24, target=25,
                                backup=reference)


def test_a_held_audit_lock_times_out_without_a_change(ledger, monkeypatch):
    cluster, _ = ledger
    monkeypatch.setattr(cloud_provision, "MIGRATION_LOCK_TIMEOUT", "1s")
    with cluster.admin("catalyst_lab") as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (cloud_provision.LOCK,))
        refused(ledger, "MIGRATION_LOCK_TIMEOUT", live_reference(cluster))
        holder.execute("SELECT pg_advisory_unlock(%s)", (cloud_provision.LOCK,))


FAULTY = {
    # A statement fails after earlier ones succeeded: every file of the batch rolls back.
    "fails": ("CREATE TABLE lab.fixture_partial(id int);\nSELECT 1/0;\n"
              "INSERT INTO lab.schema_migrations(version) VALUES(25);\n", None),
    "unrecorded": ("CREATE TABLE lab.fixture_unrecorded(id int);\n",
                   "LEDGER_TARGET_NOT_RECORDED"),
    "rewrites": ("ALTER TABLE lab.trade_events DISABLE TRIGGER immutable_rows;\n"
                 "UPDATE lab.trade_events SET event_hash = repeat('f', 64)\n"
                 " WHERE seq = (SELECT max(seq) FROM lab.trade_events);\n"
                 "ALTER TABLE lab.trade_events ENABLE TRIGGER immutable_rows;\n"
                 "INSERT INTO lab.schema_migrations(version) VALUES(25);\n",
                 "AUDIT_HISTORY_REWRITTEN"),
    "app_owns": ("CREATE TABLE lab.fixture_owned(id int);\n"
                 "ALTER TABLE lab.fixture_owned OWNER TO catalyst_app;\n"
                 "INSERT INTO lab.schema_migrations(version) VALUES(25);\n",
                 "PROVISION_OWNERSHIP_UNEXPECTED"),
    "broad": ("GRANT CREATE ON SCHEMA lab TO catalyst_app;\n"
              "INSERT INTO lab.schema_migrations(version) VALUES(25);\n",
              "PROVISION_ROLE_TOO_BROAD"),
    "unreadable": ("CREATE TABLE lab.fixture_hidden(id int);\n"
                   "REVOKE SELECT ON lab.fixture_hidden FROM catalyst_backup;\n"
                   "INSERT INTO lab.schema_migrations(version) VALUES(25);\n",
                   "MIGRATION_BACKUP_GRANTS_INCOMPLETE"),
}


@pytest.mark.parametrize("case", sorted(FAULTY))
def test_a_faulty_migration_file_changes_nothing(ledger, tmp_path, monkeypatch, capsys, case):
    text, code = FAULTY[case]
    fixture = tmp_path / "025_lab_fixture.sql"
    fixture.write_text(text)
    monkeypatch.setattr(localdb, "migration_files", lambda: {**files_through(24), 25: fixture})
    cluster, _ = ledger
    reference = live_reference(cluster)
    if code is not None:
        refused(ledger, code, reference)
        return
    before = state(cluster)
    with pytest.raises(SystemExit) as failed:
        cloud_provision.main(["migrate", "--expect-current", "24", "--target", "25",
                              "--backup", reference],
                             {"MIGRATION_DATABASE_URL": cluster.admin_url})
    assert str(failed.value.code) == "CLOUD_MIGRATION_FAILED SQLSTATE 22012"  # Code only.
    assert state(cluster) == before and "fixture" not in capsys.readouterr().out


def test_a_new_cloud_login_role_is_refused(ledger, tmp_path, monkeypatch):
    """Only provisioning sets a login role's password: a migration never makes one."""
    fixture = tmp_path / "025_lab_fixture.sql"
    fixture.write_text("CREATE ROLE catalyst_fixture_login NOLOGIN;\n"
                       "INSERT INTO lab.schema_migrations(version) VALUES(25);\n")
    monkeypatch.setattr(localdb, "migration_files", lambda: {**files_through(24), 25: fixture})
    monkeypatch.setitem(cloud_provision.LOGIN_ROLES, "catalyst_fixture_login",
                        "FIXTURE_DATABASE_PASSWORD")
    refused(ledger, "MIGRATION_NEW_LOGIN_ROLE", live_reference(ledger[0]),
            names=["catalyst_fixture_login"])


def test_the_cli_takes_its_three_options_only_in_migrate_mode(ledger, capsys):
    cluster, _ = ledger
    env = {"MIGRATION_DATABASE_URL": cluster.admin_url}
    before = state(cluster)
    for argv in (["migrate", "--expect-current", "24", "--target", "25"],  # No backup named.
                 ["migrate", "--target", "25", "--backup", live_reference(cluster)],
                 ["initial", "--target", "25"],
                 ["rotate-passwords", "--backup", live_reference(cluster)]):
        with pytest.raises(SystemExit) as usage:
            cloud_provision.main(argv, env)
        assert usage.value.code == 2, argv
    capsys.readouterr()
    with pytest.raises(SystemExit) as missing:
        cloud_provision.main(["migrate", "--expect-current", "24", "--target", "25",
                              "--backup", live_reference(cluster)], {})
    assert str(missing.value.code) == (
        "CLOUD_MIGRATION_REFUSED: MIGRATION_DATABASE_URL_REQUIRED")
    assert state(cluster) == before


# --- The ops shell's backup ---------------------------------------------------------------


def test_the_ops_backup_command_refuses_root_and_a_wrong_service(ledger, tmp_path, places,
                                                                 capsys):
    cluster, secrets = ledger
    env = ops_env(tmp_path / "volume",
                  AUDIT_DATABASE_URL=cluster.url("catalyst_app", secrets["APP_DATABASE_PASSWORD"]),
                  BACKUP_DATABASE_URL=cluster.url("catalyst_backup",
                                                  secrets["BACKUP_DATABASE_PASSWORD"]))
    assert cloud_runtime.backup_now(env, euid=lambda: 0, **places) == 2
    assert capsys.readouterr().out == "CLOUD_BACKUP_REFUSED: CLOUD_BACKUP_AS_ROOT_REFUSED\n"
    held = {**env, "APCA_API_SECRET_KEY": "fixture-must-not-be-on-ops-000000000000"}
    assert cloud_runtime.backup_now(held, **places) == 2
    assert "OPS_MUST_NOT_HOLD_TRADING_SECRETS" in capsys.readouterr().out
    wrong = {**env, "BACKUP_DATABASE_URL": cluster.url("catalyst_backup", "not-it-" * 6)}
    assert cloud_runtime.backup_now(wrong, **places) == 1
    printed = capsys.readouterr().out
    assert printed == "CLOUD_BACKUP_FAILED: BACKUP_DATABASE_UNAVAILABLE\n"
    assert not (tmp_path / "volume" / "backups").exists() or not any(
        (tmp_path / "volume" / "backups").iterdir())


def test_the_backup_reference_is_one_shape_everywhere():
    """ledger_ops defines it; cloud_entry (standard library only) and railway.ts repeat it."""
    from pathlib import Path

    pattern = ledger_ops.BACKUP_REFERENCE.pattern
    assert re.sub(r"[()]", "", pattern) == cloud_entry.BACKUP_REFERENCE.pattern
    spec = (Path(__file__).resolve().parents[1] / ".railway" / "railway.ts").read_text()
    assert "/^" + re.sub(r"[()]", "", pattern) + "$/" in spec.replace("\n  ", "")
    taken = {"backup": "/data/backups/20260929T010203Z", "schema_version": 24,
             "audit_seq": 1234, "audit_head": "c" * 64, "manifest_sha256": "d" * 64}
    reference = ledger_ops.backup_reference(taken)
    assert reference == f"20260929T010203Z.24.1234.{'c' * 64}.{'d' * 64}"
    parsed = ledger_ops.parse_backup_reference(reference)
    assert parsed["taken_at"] == datetime(2026, 9, 29, 1, 2, 3, tzinfo=UTC)
    assert {k: parsed[k] for k in ("backup", "schema_version", "audit_seq")} == {
        "backup": "20260929T010203Z", "schema_version": 24, "audit_seq": 1234}
    with pytest.raises(ledger_ops.LedgerOpsError, match="BACKUP_REFERENCE_UNAVAILABLE"):
        ledger_ops.backup_reference({**taken, "audit_head": None, "audit_seq": None})


def test_the_entrypoint_passes_exactly_the_migration_and_backup_shapes(capsys):
    import sys

    reference = f"20260929T010203Z.24.1234.{'c' * 64}.{'d' * 64}"
    argv = ["provision", "migrate", "--expect-current", "24", "--target", "25",
            "--backup", reference]
    assert cloud_entry.command(argv) == ("provision", [
        sys.executable, "-m", "catalyst_lab.cloud_provision", *argv[1:]])
    assert cloud_entry.command(["backup"]) == ("backup", [
        sys.executable, "-m", "catalyst_lab.cloud_runtime", "backup"])
    assert "backup" not in cloud_entry.STATEFUL  # It never chowns: ops prepared its volume.
    for bad in (argv[:-1], argv + ["--now"], [*argv[:3], "024", *argv[4:]],
                [*argv[:5], "25; rm", *argv[6:]], [*argv[:-1], reference + "0"],
                [*argv[:-1], "../" + reference], ["provision", "migrate"],
                ["backup", "--now"], [*argv[:1], argv[1], argv[4], argv[5], argv[2], argv[3],
                                      argv[6], argv[7]]):
        with pytest.raises(SystemExit) as exited:
            cloud_entry.command(bad)
        assert exited.value.code == 2, bad
    assert capsys.readouterr().err.count("CLOUD_ENTRY_REFUSED: ") == 9
