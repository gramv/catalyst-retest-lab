"""The ops service's daily backup of a provisioned cloud ledger, and the local restore drill.

A disposable Railway-shaped cluster (SCRAM on a private socket); the backup runs as the read-only
``catalyst_backup`` role over a password DSN, exactly like the Railway ops service.
"""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from catalyst_lab import cloud_provision, ledger_ops
from catalyst_lab.execution import system_event
from catalyst_lab.repository import Repository
from tests import cloud_fixtures

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def ledger():
    cluster = cloud_fixtures.start(cloud_fixtures.new_root())
    secrets = cloud_fixtures.cloud_passwords(cloud_provision.login_roles())
    try:
        result = cloud_provision.provision(cluster.admin_url, secrets, now=NOW)
        app = Repository(cluster.url("catalyst_app", secrets["APP_DATABASE_PASSWORD"]))
        with app.connect() as conn:  # Some history, appended the normal audited way.
            for index in range(3):
                system_event(app, conn, "LAB_FIXTURE_CLOUD_BACKUP", {"index": index})
        secrets["_identity_seq"] = result["audit_seq"]
        yield cluster, secrets
    finally:
        cloud_fixtures.stop(cluster)


def backup_url(ledger):
    cluster, secrets = ledger
    return cluster.url("catalyst_backup", secrets["BACKUP_DATABASE_PASSWORD"])


def test_cloud_backup_verifies_and_restores_in_the_local_drill(ledger, tmp_path):
    destination = tmp_path / "volume" / "backups"
    taken = ledger_ops.backup_database(backup_url(ledger), destination, now=NOW)
    assert taken["result"] == "WRITTEN" and taken["owner_ledger"] is None
    # The migrations' own audited rows, the ledger identity, then the three fixture events.
    assert taken["event_count"] == ledger[1]["_identity_seq"] + 3
    assert taken["broker_requests"] == 0
    backup = destination / "20260927T030000Z"
    assert sorted(p.name for p in backup.iterdir()) == ["catalyst_lab.dump", "manifest.json",
                                                         "roles.sql"]
    assert all((backup / name).stat().st_mode & 0o777 == 0o600 for name in os.listdir(backup))
    roles = (backup / "roles.sql").read_text()
    assert "PASSWORD" not in roles.upper() and "CREATE ROLE catalyst_backup;" in roles
    cluster, secrets = ledger
    for secret in [*(v for k, v in secrets.items() if k.endswith("PASSWORD")),
                   cluster.admin_password]:
        for name in os.listdir(backup):
            assert secret.encode() not in (backup / name).read_bytes()
    verified = ledger_ops.verify_manifest(backup,
                                          expected_manifest_sha256=taken["manifest_sha256"])
    assert verified["result"] == "MATCH"
    drill = ledger_ops.drill(backup, "/tmp", expected_head=taken["audit_head"],
                             expected_manifest_sha256=taken["manifest_sha256"])
    assert drill["result"] == "PASSED" and drill["checks"]["ownership"] == "LAB_OWNER_ONLY"
    assert drill["checks"]["append_continuity_probe"].startswith("CHAINED_FROM_RESTORED_HEAD")
    assert drill["scratch_destroyed"] is True


def test_cloud_backup_passes_its_password_only_through_the_environment(
        ledger, tmp_path, monkeypatch):
    calls = []
    real = ledger_ops.subprocess.run

    def spy(argv, **kwargs):
        calls.append((argv, kwargs.get("env") or {}))
        return real(argv, **kwargs)

    monkeypatch.setattr(ledger_ops.subprocess, "run", spy)
    ledger_ops.backup_database(backup_url(ledger), tmp_path / "b", now=NOW + timedelta(hours=1))
    _, secrets = ledger
    password = secrets["BACKUP_DATABASE_PASSWORD"]
    assert calls and not any(password in str(arg) for argv, _ in calls for arg in argv)
    # The two tool runs that connect get the password in their environment; nothing else does
    # (the --version probes, for example).
    connecting = [(argv, env) for argv, env in calls
                  if any(str(arg).startswith("--dbname=") for arg in argv)]
    assert sorted(os.path.basename(argv[0]) for argv, _ in connecting) == ["pg_dump",
                                                                            "pg_dumpall"]
    assert all(env.get("PGPASSWORD") == password for _, env in connecting)
    assert not any("PGPASSWORD" in env for argv, env in calls
                   if not any(str(arg).startswith("--dbname=") for arg in argv))


def test_cloud_backup_prune_and_refusals(ledger, tmp_path):
    destination = tmp_path / "backups"
    for day in range(3):
        ledger_ops.backup_database(backup_url(ledger), destination,
                                   now=NOW - timedelta(days=20 - day))
    pruned = ledger_ops.prune(destination, 14, now=NOW)
    assert len(pruned["deleted"]) == 2 and pruned["kept"] == [pruned["newest_verified"]]
    cluster, secrets = ledger
    wrong_db = cluster.url("catalyst_backup", secrets["BACKUP_DATABASE_PASSWORD"], "railway")
    with pytest.raises(ledger_ops.LedgerOpsError, match="BACKUP_DATABASE_UNEXPECTED"):
        ledger_ops.backup_database(wrong_db, tmp_path / "x")
    bad = cluster.url("catalyst_backup", "not-the-password-" * 3)
    with pytest.raises(ledger_ops.LedgerOpsError, match="BACKUP_DATABASE_UNAVAILABLE"):
        ledger_ops.backup_database(bad, tmp_path / "y")
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / "LEDGER.json").write_text(json.dumps({"role": "ACCOUNT_LEDGER"}))
    with pytest.raises(ledger_ops.LedgerOpsError, match="BACKUP_DESTINATION_IS_OWNER_LEDGER"):
        ledger_ops.backup_database(backup_url(ledger), marked)


def test_backup_role_reads_the_ledger_writes_nothing_and_never_reads_pg_authid(ledger):
    import psycopg
    from psycopg import sql

    with psycopg.connect(backup_url(ledger), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0] > 3
        relations = conn.execute("""SELECT c.relname::text, c.relkind::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'lab' AND c.relkind IN ('r', 'p', 'S')""").fetchall()
        assert len(relations) > 20
        for name, kind in relations:  # Every table and sequence pg_dump reads.
            column = sql.SQL("last_value" if kind == "S" else "1")
            conn.execute(sql.SQL("SELECT {} FROM lab.{} LIMIT 1").format(
                column, sql.Identifier(name)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("""INSERT INTO lab.trade_events(strategy_version, event_type,
                payload_json) VALUES ('CATALYST_RETEST_V1', 'SYSTEM_EVENT', '{}')""")
        # The cluster's password verifiers stay out of reach (pg_read_all_data reached them).
        for query in ("SELECT rolpassword FROM pg_catalog.pg_authid",
                      "SELECT count(*) FROM pg_catalog.pg_authid"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
        assert conn.execute("""SELECT pg_has_role(current_user, 'pg_read_all_data', 'MEMBER')
            OR has_table_privilege('pg_catalog.pg_authid', 'SELECT')""").fetchone()[0] is False
