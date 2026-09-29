"""Owner-ledger guard. Disposable /tmp clusters and LAB_FIXTURE audit events only."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.cli import main
from catalyst_lab.repository import Repository

NEWEST = max(localdb.migration_files())
# An older ledger: real migration 014 (indexes, functions and grants only) is still pending.
LEDGER_VERSION = 13
DDL_ONLY = 14
# Fixture migrations numbered 014 in a temporary copy of the migrations directory.
APPEND_EVENT = """
INSERT INTO lab.trade_events(strategy_version, event_type, payload_json)
VALUES ('CATALYST_RETEST_V1', 'SYSTEM_EVENT', '{"kind": "LAB_FIXTURE_MIGRATION_NOTE"}');
INSERT INTO lab.schema_migrations(version) VALUES (14);
"""
REWRITE_HEAD = """
ALTER TABLE lab.trade_events DISABLE TRIGGER immutable_rows;
UPDATE lab.trade_events SET event_hash = repeat('f', 64)
WHERE seq = (SELECT max(seq) FROM lab.trade_events);
ALTER TABLE lab.trade_events ENABLE TRIGGER immutable_rows;
INSERT INTO lab.schema_migrations(version) VALUES (14);
"""


def running(root: Path) -> bool:
    status = subprocess.run(
        [localdb.pg_binary("pg_ctl"), "-D", str(root / "postgres"), "status"],
        capture_output=True,
    )
    return status.returncode == 0


@contextmanager
def disposable_root(child: str | None = None):
    # A short private /tmp path keeps the Unix-domain socket below the platform limit.
    with tempfile.TemporaryDirectory(prefix="catalyst-guard-", dir="/tmp") as directory:
        root = Path(directory) / child if child else Path(directory)
        try:
            yield root
        finally:
            if (root / "postgres" / "PG_VERSION").exists() and running(root):
                localdb.stop(root)


def migrations_through(directory: Path, version: int, extra_sql: str | None = None) -> Path:
    """Real migrations up to `version`, plus an optional fixture migration numbered after it."""
    directory.mkdir()
    for number, path in localdb.migration_files().items():
        if number <= version:
            shutil.copy(path, directory)
    if extra_sql is not None:
        (directory / f"{version + 1:03d}_lab_fixture.sql").write_text(extra_sql)
    return directory


def build_ledger(root: Path) -> None:
    """Initialize a cluster at LEDGER_VERSION holding real, fixture-labeled audit events."""
    with tempfile.TemporaryDirectory() as partial, pytest.MonkeyPatch.context() as patch:
        partial_migrations = migrations_through(Path(partial) / "m", LEDGER_VERSION)
        patch.setattr(localdb, "MIGRATIONS", partial_migrations)
        localdb.start(root)
    with Repository(localdb.connection_url(root)).connect() as conn:
        for index in range(3):
            Repository.append_event(conn, "SYSTEM_EVENT", {"kind": "LAB_FIXTURE", "index": index})


def schema_version(root: Path) -> int:
    with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
        return conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]


def audit_head(root: Path) -> tuple[str | None, int]:
    with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
        return conn.execute(
            "SELECT (SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1),"
            " count(*) FROM lab.trade_events"
        ).fetchone()


def mark(root: Path, role: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(root / localdb.LEDGER_MARKER, flags, 0o600), "w") as marker:
        json.dump({"role": role}, marker)


def ledger_migrate(root: Path, current: int, target: int, manifest: Path) -> list[str]:
    return [
        "catalyst-lab",
        "ledger-migrate",
        "--local-dir",
        str(root),
        "--expect-current",
        str(current),
        "--target",
        str(target),
        "--backup-manifest",
        str(manifest),
    ]


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "backup-manifest.json"
    path.write_text(json.dumps({"purpose": "LAB_FIXTURE"}))
    return path


@pytest.fixture
def cli_env(monkeypatch):
    # dev-init exports settings with setdefault; undo restores the absent variables afterwards.
    for name in ("DATABASE_URL", "MUSE_API_TOKEN"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture(scope="module")
def ledger():
    """Shared v13 ledger. Every test using it must leave it unchanged and running."""
    with disposable_root() as root:
        build_ledger(root)
        yield root


def test_fresh_cluster_applies_every_migration_privately():
    with disposable_root("cluster") as root:
        root.mkdir()
        os.chmod(root, 0o755)
        localdb.start(root)
        assert schema_version(root) == NEWEST
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "socket").stat().st_mode) == 0o700
        localdb.stop(root)
        localdb.start(root)  # An up-to-date existing cluster restarts without an owner step.
        assert running(root) and schema_version(root) == NEWEST


def test_existing_ledger_with_pending_migration_requires_owner_step():
    with disposable_root() as root:
        build_ledger(root)
        with pytest.raises(RuntimeError, match="^PENDING_MIGRATION_REQUIRES_OWNER_STEP$"):
            localdb.start(root)
        assert running(root) and schema_version(root) == LEDGER_VERSION
        localdb.stop(root)
        with pytest.raises(RuntimeError, match="^PENDING_MIGRATION_REQUIRES_OWNER_STEP$"):
            localdb.start(root)
        assert not running(root)  # Started only to read the version, then left as found.
        localdb.start(root, allow_pending=True)
        assert schema_version(root) == NEWEST


def test_dev_init_cannot_migrate_an_existing_ledger(ledger, cli_env):
    cli_env.setattr("sys.argv", ["catalyst-lab", "dev-init", "--local-dir", str(ledger)])
    with pytest.raises(RuntimeError, match="^PENDING_MIGRATION_REQUIRES_OWNER_STEP$"):
        main()
    assert running(ledger) and schema_version(ledger) == LEDGER_VERSION
    assert not (ledger / "muse-token").exists()


@pytest.mark.parametrize("role", sorted(localdb.PROTECTED_LEDGER_ROLES))
def test_owner_marker_blocks_initdb(role):
    with disposable_root() as root:
        mark(root, role)
        with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
            localdb.start(root)
        assert [path.name for path in root.iterdir()] == [localdb.LEDGER_MARKER]


def test_owner_marker_blocks_migration(ledger):
    mark(ledger, "ACCOUNT_LEDGER")
    try:
        with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
            localdb.start(ledger, allow_pending=True)
    finally:
        (ledger / localdb.LEDGER_MARKER).unlink()
    assert schema_version(ledger) == LEDGER_VERSION


@pytest.mark.parametrize(
    "content", [None, "", "not json", "[]", '{"role": "DEV"}', '{"role": ["ARCHIVED"]}']
)
def test_unreadable_or_unknown_marker_fails_closed(content):
    with disposable_root() as root:
        marker = root / localdb.LEDGER_MARKER
        if content is None:
            marker.symlink_to(root / "absent.json")  # A dangling link is still a marker.
        else:
            marker.write_text(content)
        with pytest.raises(RuntimeError, match="^OWNER_LEDGER_MARKER_INVALID$"):
            localdb.start(root)
        assert not (root / "postgres").exists()


def test_duplicate_migration_numbers_are_refused_before_any_change(tmp_path, monkeypatch):
    duplicates = migrations_through(tmp_path / "m", LEDGER_VERSION, "SELECT 1;")
    shutil.copy(
        duplicates / f"{LEDGER_VERSION + 1:03d}_lab_fixture.sql",
        duplicates / f"{LEDGER_VERSION + 1:03d}_lab_fixture_copy.sql",
    )
    monkeypatch.setattr(localdb, "MIGRATIONS", duplicates)
    with disposable_root() as root:
        with pytest.raises(RuntimeError, match="^DUPLICATE_MIGRATION_VERSION$"):
            localdb.start(root)
        assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("current", "target", "code"),
    [
        (LEDGER_VERSION - 1, LEDGER_VERSION + 1, "LEDGER_VERSION_MISMATCH"),
        (LEDGER_VERSION + 1, LEDGER_VERSION + 1, "LEDGER_TARGET_NOT_AHEAD"),
        (LEDGER_VERSION, 999, "LEDGER_MIGRATION_FILES_MISSING"),
    ],
)
def test_ledger_migrate_refuses_unexpected_versions(
    ledger, manifest, monkeypatch, current, target, code
):
    before = audit_head(ledger)
    monkeypatch.setattr("sys.argv", ledger_migrate(ledger, current, target, manifest))
    with pytest.raises(SystemExit, match=f"refused: {code}$"):
        main()
    assert schema_version(ledger) == LEDGER_VERSION and audit_head(ledger) == before


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "empty", "other_owner"])
def test_ledger_migrate_requires_an_owned_backup_manifest(
    ledger, manifest, tmp_path, monkeypatch, kind
):
    path, code = tmp_path / "candidate-manifest", "BACKUP_MANIFEST_INVALID"
    if kind == "missing":
        code = "BACKUP_MANIFEST_MISSING"
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(manifest)
    elif kind == "empty":
        path.touch()
    else:
        path = manifest
        monkeypatch.setattr(localdb.os, "getuid", lambda: manifest.stat().st_uid + 1)
    monkeypatch.setattr("sys.argv", ledger_migrate(ledger, LEDGER_VERSION, NEWEST, path))
    with pytest.raises(SystemExit, match=f"refused: {code}$"):
        main()
    assert schema_version(ledger) == LEDGER_VERSION


def test_ledger_migrate_never_migrates_an_archived_ledger(ledger, manifest, monkeypatch):
    mark(ledger, "ARCHIVED")
    monkeypatch.setattr("sys.argv", ledger_migrate(ledger, LEDGER_VERSION, NEWEST, manifest))
    try:
        with pytest.raises(SystemExit, match="refused: OWNER_LEDGER_ARCHIVED$"):
            main()
    finally:
        (ledger / localdb.LEDGER_MARKER).unlink()
    assert schema_version(ledger) == LEDGER_VERSION


def test_ledger_migrate_never_starts_or_initializes_a_cluster(ledger, manifest):
    request = {"expect_current": LEDGER_VERSION, "target": NEWEST, "backup_manifest": manifest}
    with disposable_root() as empty:
        with pytest.raises(RuntimeError, match="^LEDGER_CLUSTER_NOT_FOUND$"):
            localdb.migrate_ledger(empty, **request)
        assert list(empty.iterdir()) == []
    localdb.stop(ledger)
    try:
        with pytest.raises(RuntimeError, match="^LEDGER_CLUSTER_NOT_RUNNING$"):
            localdb.migrate_ledger(ledger, **request)
        assert not running(ledger)
    finally:
        localdb.run_pg(
            "pg_ctl", "-D", ledger / "postgres", "-l", ledger / "postgres.log", "-w", "start"
        )
    assert schema_version(ledger) == LEDGER_VERSION


def test_migration_that_rewrites_audit_history_rolls_back(ledger, manifest, tmp_path, monkeypatch):
    head, count = audit_head(ledger)
    fixture = migrations_through(tmp_path / "m", LEDGER_VERSION, REWRITE_HEAD)
    monkeypatch.setattr(localdb, "MIGRATIONS", fixture)
    with pytest.raises(RuntimeError, match="^AUDIT_HISTORY_REWRITTEN$"):
        localdb.migrate_ledger(
            ledger, expect_current=LEDGER_VERSION, target=DDL_ONLY, backup_manifest=manifest
        )
    assert schema_version(ledger) == LEDGER_VERSION and audit_head(ledger) == (head, count)
    exported = Repository(localdb.connection_url(ledger)).export_events()
    assert verify_events(exported, head)["event_count"] == count


def test_ddl_only_migration_leaves_audit_head_unchanged(manifest, monkeypatch, capsys):
    with disposable_root() as root:
        build_ledger(root)
        head, count = audit_head(root)
        mark(root, "ACCOUNT_LEDGER")  # The explicit owner step still serves the account ledger.
        monkeypatch.setattr("sys.argv", ledger_migrate(root, LEDGER_VERSION, DDL_ONLY, manifest))
        main()
        result = json.loads(capsys.readouterr().out)
        assert (result["before"], result["after"]) == (LEDGER_VERSION, DDL_ONLY)
        assert result["audit_head_before"] == result["audit_head_after"] == head
        # Earlier migrations may seed audited rows; the three fixture events are on top.
        assert result["event_count"] == result["event_count_before"] == count >= 3
        assert result["backup_manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
        assert result["broker_requests"] == 0 and "warning" not in result
        assert schema_version(root) == DDL_ONLY and audit_head(root) == (head, count)
        exported = Repository(localdb.connection_url(root)).export_events()
        assert verify_events(exported, head)["event_count"] == count
        with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
            localdb.start(root)


def test_migration_that_appends_an_audit_event_is_flagged(manifest, tmp_path, monkeypatch):
    with disposable_root() as root:
        build_ledger(root)
        head, count = audit_head(root)
        fixture = migrations_through(tmp_path / "m", LEDGER_VERSION, APPEND_EVENT)
        monkeypatch.setattr(localdb, "MIGRATIONS", fixture)
        result = localdb.migrate_ledger(
            root, expect_current=LEDGER_VERSION, target=DDL_ONLY, backup_manifest=manifest
        )
        assert result["warning"] == "AUDIT_HEAD_CHANGED_BY_MIGRATION"
        assert result["audit_head_before"] == head != result["audit_head_after"]
        assert (result["event_count_before"], result["event_count"]) == (count, count + 1)
        exported = Repository(localdb.connection_url(root)).export_events()
        assert verify_events(exported, result["audit_head_after"])["event_count"] == count + 1


def test_dev_init_defaults_to_the_development_directory(cli_env):
    with tempfile.TemporaryDirectory(prefix="catalyst-guard-", dir="/tmp") as data_home:
        cli_env.setenv("XDG_DATA_HOME", data_home)
        cli_env.setattr("sys.argv", ["catalyst-lab", "dev-init"])
        root = Path(data_home).resolve() / "catalyst-retest-lab" / "dev"
        try:
            main()
            assert schema_version(root) == NEWEST
            assert stat.S_IMODE((root / "muse-token").stat().st_mode) == 0o600
        finally:
            if running(root):
                localdb.stop(root)
