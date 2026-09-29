"""Ledger backup and restore drill (plan 4.6). Disposable /tmp clusters and LAB_FIXTURE rows only.

No owner ledger is opened: every source cluster is created here, and every drill restores into
its own disposable cluster inside a private /tmp directory that must be empty again afterwards.
"""

import hashlib
import inspect
import json
import os
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from catalyst_lab import ledger_ops, localdb
from catalyst_lab.config import SCHEMA_VERSION
from catalyst_lab.jev_contract import encoded
from catalyst_lab.jev_store import JevStore
from catalyst_lab.ledger_ops import LedgerOpsError
from catalyst_lab.repository import Repository
from tests.conftest import NOW
from tests.test_jev_review import Clock, build, request, response_for, run
from tests.test_localdb_guard import LEDGER_VERSION, build_ledger, disposable_root, mark, running
from tests.test_repository_hygiene import FIXTURE_MARKERS, PATTERNS

PLAN_ROLES = frozenset({
    "catalyst_app", "catalyst_risk", "catalyst_jev", "catalyst_review",
    "catalyst_review_operator", "catalyst_reporting", "catalyst_operator", "catalyst_public",
})
# Roles created after the schema-13 ledger: catalyst_operator (015), catalyst_public (024).
LATER_ROLES = frozenset({"catalyst_operator", "catalyst_public"})
PASSED_CHECKS = {
    "dump_sha256": "MATCH",
    "roles_sha256": "MATCH",
    "roles_script": "ALLOWLISTED_NO_PASSWORDS",
    "roles": "PRESENT_AND_RESTRICTED",
    "role_logins": "PASSED",
    "app_role": "PASSED",
    "repository_check_role": "PASSED",
    "ownership": "LAB_OWNER_ONLY",
    "schema_version": "MATCH",
    "table_counts": "MATCH",
    "audit_head": "MATCH",
    "audit_chain": "VERIFIED",
    "triggers": "PRESENT_AND_ENABLED",
    "append_only_probe": "UPDATE_AND_DELETE_REFUSED",
    "append_continuity_probe": "CHAINED_FROM_RESTORED_HEAD_ROLLED_BACK",
}


@pytest.fixture(scope="module")
def source():
    """One migrated disposable cluster; tests only append LAB_FIXTURE history to it."""
    with tempfile.TemporaryDirectory(prefix="catalyst-backup-", dir="/tmp") as directory:
        root = Path(directory) / "ledger"
        localdb.start(root)
        try:
            yield root
        finally:
            localdb.stop(root)


@pytest.fixture
def ledger(source, raw, evidence, policy):
    """Audited fixture history: a candidate lifecycle plus a Jev request, receipt and judgments."""
    repo = Repository(localdb.connection_url(source))
    candidate = repo.submit(raw, NOW, lambda c, now: evidence, policy)["candidate_id"]
    with repo.connect() as conn:
        repo.transition(conn, candidate, "VALIDATED", "WATCHING", {"reason": "LAB_FIXTURE"})
        repo.transition(conn, candidate, "WATCHING", "TRIGGER_CONFIRMED", {"reason": "LAB_FIXTURE"})
    timing, body = Clock(), encoded(response_for()).encode()
    reviewer = build(JevStore(localdb.connection_url(source, "catalyst_jev")), timing,
                     lambda req: httpx.Response(200, content=body))
    assert run(reviewer, request(timing)).status == "RECORDED"
    return source


@pytest.fixture
def scratch():
    """A short private /tmp root for drill clusters (Unix-socket path limit)."""
    with tempfile.TemporaryDirectory(prefix="catalyst-drill-", dir="/tmp") as directory:
        yield Path(directory)


def owner(root: Path):
    return psycopg.connect(localdb.connection_url(root, "lab_owner"))


def live(root: Path) -> dict:
    """Head, count and per-table counts, read without any ledger_ops code."""
    with owner(root) as conn:
        seq, head = conn.execute(
            "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        tables = [row[0] for row in conn.execute(
            """SELECT table_schema || '.' || table_name FROM information_schema.tables
            WHERE table_schema = 'lab' AND table_type = 'BASE TABLE'"""
        )]
        counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                  for name in tables}
    return {"audit_seq": seq, "audit_head": head, "event_count": counts["lab.trade_events"],
            "table_counts": counts}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def manifest_of(directory) -> dict:
    return json.loads((Path(directory) / ledger_ops.MANIFEST).read_text())


def rewrite_manifest(directory, manifest: dict) -> None:
    # Rewriting the existing file keeps its 0600 mode: only the content is forged.
    (Path(directory) / ledger_ops.MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


def test_backup_manifest_matches_the_live_ledger(ledger, tmp_path):
    before = live(ledger)
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    directory = Path(result["backup"])
    manifest = manifest_of(directory)
    assert manifest["audit_head"] == before["audit_head"] == result["audit_head"]
    assert (manifest["audit_seq"], manifest["event_count"]) == (
        before["audit_seq"], before["event_count"]
    )
    assert manifest["table_counts"] == before["table_counts"]
    assert manifest["table_counts"]["lab.candidates"] >= 1
    assert manifest["table_counts"]["lab.jev_receipts"] >= 1
    assert manifest["schema_version"] == SCHEMA_VERSION
    with owner(ledger) as conn:
        identifier = conn.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
    assert manifest["cluster_system_identifier"] == identifier
    assert manifest["dump_sha256"] == sha256(directory / ledger_ops.DUMP)
    assert manifest["roles_sha256"] == sha256(directory / ledger_ops.ROLES)
    assert result["manifest_sha256"] == sha256(directory / ledger_ops.MANIFEST)
    assert set(manifest["tool_versions"]) == {"pg_dump", "pg_dumpall", "server"}
    taken = datetime.strptime(manifest["taken_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert directory.name == taken.strftime("%Y%m%dT%H%M%SZ")
    assert mode(tmp_path / "backups") == mode(directory) == 0o700
    assert sorted(os.listdir(directory)) == sorted(ledger_ops.BACKUP_FILES)
    assert {mode(directory / name) for name in ledger_ops.BACKUP_FILES} == {0o600}
    assert result["broker_requests"] == 0 and result["owner_ledger"] is None
    assert live(ledger) == before  # The backup only read the ledger.
    verified = ledger_ops.verify_manifest(
        directory, expected_head=before["audit_head"],
        expected_manifest_sha256=result["manifest_sha256"],
    )
    assert verified["result"] == "MATCH" and verified["restored"] is False
    assert verified["independent_head_verified"] and verified["independent_manifest_verified"]


def test_head_and_counts_come_from_the_same_snapshot_as_the_dump(
    ledger, tmp_path, scratch, monkeypatch
):
    """Appends committed while the backup runs reach neither the manifest nor the dump."""
    before = live(ledger)
    read_state, dump_database = ledger_ops._snapshot_state, ledger_ops._dump_database

    def append(note):
        with Repository(localdb.connection_url(ledger)).connect() as conn:
            Repository.append_event(conn, "SYSTEM_EVENT", {"kind": "LAB_FIXTURE", "note": note})

    def state_after_append(conn):
        append("committed after the snapshot export")
        return read_state(conn)

    def dump_after_append(*args):
        append("committed before pg_dump starts")
        return dump_database(*args)

    monkeypatch.setattr(ledger_ops, "_snapshot_state", state_after_append)
    monkeypatch.setattr(ledger_ops, "_dump_database", dump_after_append)
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    monkeypatch.undo()
    after = live(ledger)
    assert after["event_count"] == before["event_count"] + 2
    manifest = manifest_of(result["backup"])
    assert (manifest["audit_head"], manifest["event_count"]) == (
        before["audit_head"], before["event_count"]
    )
    assert manifest["table_counts"] == before["table_counts"]
    # The restored dump holds exactly the snapshot: every count and the head match the manifest.
    report = ledger_ops.drill(result["backup"], scratch)
    assert report["result"] == "PASSED" and report["event_count"] == before["event_count"]
    assert list(scratch.iterdir()) == []


def test_drill_restores_into_a_disposable_cluster_and_passes_every_check(
    ledger, tmp_path, scratch, capsys
):
    before = live(ledger)
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    ledger_ops.main([
        "drill", "--backup", result["backup"], "--scratch-root", str(scratch),
        "--expected-head", before["audit_head"],
        "--expected-manifest-sha256", result["manifest_sha256"],
    ])
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "AUDIT_AND_LEDGER_RESTORE_DRILL" and report["result"] == "PASSED"
    assert "ready_to_trade" not in json.dumps(report).lower()
    assert report["checks"] == PASSED_CHECKS
    assert set(report["roles_verified"]) == PLAN_ROLES
    assert (report["audit_head"], report["audit_seq"], report["event_count"]) == (
        before["audit_head"], before["audit_seq"], before["event_count"]
    )
    assert report["tables_verified"] == len(before["table_counts"])
    assert report["triggers_verified"] > len(before["table_counts"])
    assert report["drill_cluster_system_identifier"] != report["source_cluster_system_identifier"]
    assert report["independent_head_verified"] and report["independent_manifest_verified"]
    assert report["restore_order"] == ledger_ops.RESTORE_ORDER
    assert report["scratch_destroyed"] and report["broker_requests"] == 0
    assert list(scratch.iterdir()) == []
    assert running(ledger) and live(ledger) == before  # The source was never touched.


def test_archive_creates_triggers_only_after_all_table_data(ledger, tmp_path):
    """Why stored hashes survive: no BEFORE INSERT trigger exists while rows are loaded."""
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    dump = Path(result["backup"]) / ledger_ops.DUMP
    listing = subprocess.run(
        [localdb.pg_binary("pg_restore"), "--list", str(dump)],
        capture_output=True, check=True, text=True,
    ).stdout.splitlines()
    entries = [line.split() for line in listing if line and not line.startswith(";")]
    data = [i for i, parts in enumerate(entries) if parts[3:5] == ["TABLE", "DATA"]]
    triggers = [i for i, parts in enumerate(entries) if parts[3] == "TRIGGER"]
    assert ["lab", "trade_events"] in [entries[i][5:7] for i in data]
    assert ["lab", "trade_events", "stamp_event"] in [entries[i][4:7] for i in triggers]
    assert data and triggers and max(data) < min(triggers)


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("catalyst_lab.dump", "BACKUP_DUMP_HASH_MISMATCH"),
        ("roles.sql", "BACKUP_ROLES_HASH_MISMATCH"),
    ],
)
def test_tampered_backup_fails_verification_and_drill(ledger, tmp_path, scratch, name, code):
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    flip_byte(Path(result["backup"]) / name)
    with pytest.raises(LedgerOpsError, match=f"^{code}$"):
        ledger_ops.verify_manifest(result["backup"])
    with pytest.raises(LedgerOpsError, match=f"^{code}$"):
        ledger_ops.drill(result["backup"], scratch)
    assert list(scratch.iterdir()) == []
    with pytest.raises(SystemExit) as refused:
        ledger_ops.main(["verify-manifest", "--backup", result["backup"]])
    assert json.loads(refused.value.code) == {
        "command": "verify-manifest", "result": "REFUSED", "code": code
    }


@pytest.mark.parametrize(
    ("field", "code"),
    [("table_counts", "DRILL_TABLE_COUNT_MISMATCH"), ("audit_head", "DRILL_AUDIT_HEAD_MISMATCH")],
)
def test_manifest_that_disagrees_with_the_restore_fails_the_drill(
    ledger, tmp_path, scratch, field, code
):
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    manifest = manifest_of(result["backup"])
    original_head = manifest["audit_head"]
    if field == "table_counts":
        manifest["table_counts"]["lab.candidates"] += 1
    else:
        manifest["audit_head"] = "f" * 64
    rewrite_manifest(result["backup"], manifest)
    assert ledger_ops.verify_manifest(result["backup"])["result"] == "MATCH"  # Hashes still match.
    with pytest.raises(LedgerOpsError, match=f"^{code}$") as refused:
        ledger_ops.drill(result["backup"], scratch)
    if field == "table_counts":
        assert refused.value.detail == ["lab.candidates"]
    assert list(scratch.iterdir()) == []
    # Independently retained values catch the forged manifest before anything is restored.
    with pytest.raises(LedgerOpsError, match="^INDEPENDENT_MANIFEST_MISMATCH$"):
        ledger_ops.drill(
            result["backup"], scratch, expected_manifest_sha256=result["manifest_sha256"]
        )
    if field == "audit_head":
        with pytest.raises(LedgerOpsError, match="^INDEPENDENT_HEAD_MISMATCH$"):
            ledger_ops.verify_manifest(result["backup"], expected_head=original_head)
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("\\! touch {marker}", "BACKUP_ROLES_SCRIPT_UNEXPECTED"),
        ("CREATE ROLE intruder; COPY (SELECT 1) TO PROGRAM 'touch {marker}';",
         "BACKUP_ROLES_SCRIPT_UNEXPECTED"),
        ("ALTER ROLE catalyst_app WITH LOGIN PASSWORD 'lab-fixture';",
         "BACKUP_ROLES_CONTAIN_PASSWORD"),
    ],
)
def test_roles_script_outside_the_allowlist_is_never_executed(
    ledger, tmp_path, scratch, line, code
):
    """Even with a matching forged manifest hash, psql never sees an unexpected statement."""
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    directory, marker = Path(result["backup"]), tmp_path / "executed"
    roles = directory / ledger_ops.ROLES
    forged = roles.read_bytes() + (line.format(marker=marker) + "\n").encode()
    roles.write_bytes(forged)
    manifest = manifest_of(directory)
    manifest["roles_sha256"] = hashlib.sha256(forged).hexdigest()
    rewrite_manifest(directory, manifest)
    with pytest.raises(LedgerOpsError, match=f"^{code}$"):
        ledger_ops.verify_manifest(directory)
    with pytest.raises(LedgerOpsError, match=f"^{code}$"):
        ledger_ops.drill(directory, scratch)
    assert not marker.exists() and list(scratch.iterdir()) == []


def test_prune_keeps_the_newest_verified_backup_and_skips_unverifiable(ledger, tmp_path, capsys):
    destination = tmp_path / "backups"
    now = datetime.now(UTC).replace(microsecond=0)
    made = {
        days: Path(ledger_ops.backup(ledger, destination, now=now - timedelta(days=days))["backup"])
        for days in (50, 40, 30, 20, 1)
    }
    names = {days: path.name for days, path in made.items()}
    for days in (50, 1):  # Old and newest-by-name copies that no longer verify.
        flip_byte(made[days] / ledger_ops.DUMP)
    (destination / "notes").mkdir(mode=0o700)
    (destination / "20990101T000000Z").symlink_to(made[20])
    dry = ledger_ops.prune(destination, 14, dry_run=True, now=now)
    assert dry["would_delete"] == [names[40], names[30]] and dry["deleted"] == []
    assert all(path.is_dir() for path in made.values())
    ledger_ops.main(["prune", "--destination", str(destination), "--dry-run"])
    assert json.loads(capsys.readouterr().out)["would_delete"] == [names[40], names[30]]
    result = ledger_ops.prune(destination, now=now)
    assert result["deleted"] == [names[40], names[30]]
    # Older than the retention, yet kept: it is the newest backup that still verifies.
    assert result["kept"] == [names[20]] and result["newest_verified"] == names[20]
    assert result["skipped_unverifiable"] == [names[50], names[1]]
    assert result["ignored"] == ["20990101T000000Z", "notes"]
    assert sorted(os.listdir(destination)) == sorted(
        [names[50], names[20], names[1], "notes", "20990101T000000Z"]
    )
    assert ledger_ops.prune(destination, now=now)["deleted"] == []
    with pytest.raises(LedgerOpsError, match="^RETENTION_DAYS_INVALID$"):
        ledger_ops.prune(destination, 0)


def test_backup_directory_holds_no_credentials(ledger, tmp_path):
    with owner(ledger) as conn:
        conn.execute("ALTER ROLE catalyst_reporting PASSWORD 'lab-fixture-role-password'")
    try:
        result = ledger_ops.backup(ledger, tmp_path / "backups")
    finally:
        with owner(ledger) as conn:
            conn.execute("ALTER ROLE catalyst_reporting PASSWORD NULL")
    directory = Path(result["backup"])
    contents = {
        name: (directory / name).read_bytes().decode("latin-1")
        for name in sorted(ledger_ops.BACKUP_FILES)
    }
    # The custom-format dump is compressed; scan its SQL rendering as well as its raw bytes.
    contents["catalyst_lab.dump as SQL"] = subprocess.run(
        [localdb.pg_binary("pg_restore"), "--file=-", str(directory / ledger_ops.DUMP)],
        capture_output=True, check=True,
    ).stdout.decode()
    findings = [
        f"{kind} in {name}"
        for name, text in contents.items()
        for kind, pattern in PATTERNS.items()
        for match in pattern.finditer(text)
        if not (kind == "bearer_token"
                and any(m in match.group(0).lower() for m in FIXTURE_MARKERS))
    ]
    assert not findings
    for text in contents.values():
        assert "SCRAM-SHA-256$" not in text and "lab-fixture-role-password" not in text
    assert "PASSWORD" not in contents["roles.sql"].upper()
    assert "CREATE ROLE catalyst_reporting;" in contents["roles.sql"]
    assert "host=" not in contents["manifest.json"] and str(ledger) not in contents["manifest.json"]


def test_cli_refuses_an_owner_ledger_unless_explicitly_allowed(source, tmp_path, capsys):
    destination = tmp_path / "backups"
    marked = tmp_path / "owner-ledger"
    marked.mkdir(mode=0o700)
    mark(marked, "ACCOUNT_LEDGER")

    def refusal(argv):
        with pytest.raises(SystemExit) as refused:
            ledger_ops.main(argv)
        return json.loads(refused.value.code)["code"]

    backup = ["backup", "--destination", str(destination), "--root"]
    assert refusal([*backup, str(marked)]) == "OWNER_LEDGER_REQUIRES_ALLOW_FLAG"
    (marked / localdb.LEDGER_MARKER).write_text("not json")
    assert refusal([*backup, str(marked), "--allow-owner-ledger"]) == "OWNER_LEDGER_MARKER_INVALID"
    assert not destination.exists()
    # The flag is the only path to a marked ledger; here a disposable running cluster.
    mark(source, "ARCHIVED")
    try:
        assert refusal([*backup, str(source)]) == "OWNER_LEDGER_REQUIRES_ALLOW_FLAG"
        assert not destination.exists()
        ledger_ops.main([*backup, str(source), "--allow-owner-ledger"])
    finally:
        (source / localdb.LEDGER_MARKER).unlink()
    written = json.loads(capsys.readouterr().out)
    assert written["result"] == "WRITTEN" and written["owner_ledger"] == "ARCHIVED"
    assert ledger_ops.verify_manifest(written["backup"])["result"] == "MATCH"


def test_backup_never_overwrites_and_refuses_unsafe_destinations(ledger, tmp_path):
    moment = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    first = Path(ledger_ops.backup(ledger, tmp_path / "backups", now=moment)["backup"])
    hashes = {name: sha256(first / name) for name in ledger_ops.BACKUP_FILES}
    with pytest.raises(LedgerOpsError, match="^BACKUP_DESTINATION_EXISTS$"):
        ledger_ops.backup(ledger, tmp_path / "backups", now=moment)
    assert {name: sha256(first / name) for name in ledger_ops.BACKUP_FILES} == hashes
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    with pytest.raises(LedgerOpsError, match="^PRIVATE_OWNER_DIRECTORY_REQUIRED$"):
        ledger_ops.backup(ledger, shared)
    assert list(shared.iterdir()) == []
    beside_ledger = tmp_path / "beside"
    beside_ledger.mkdir(mode=0o700)
    mark(beside_ledger, "ACCOUNT_LEDGER")
    with pytest.raises(LedgerOpsError, match="^BACKUP_DESTINATION_IS_OWNER_LEDGER$"):
        ledger_ops.backup(ledger, beside_ledger)
    with pytest.raises(LedgerOpsError, match="^LEDGER_CLUSTER_NOT_FOUND$"):
        ledger_ops.backup(tmp_path / "no-cluster", tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_drill_restores_a_pre_migration_ledger_and_backup_never_starts_one(tmp_path, scratch):
    """The owner's maintenance window backs up the older schema before migrating it."""
    with disposable_root() as root:
        build_ledger(root)
        result = ledger_ops.backup(root, tmp_path / "backups")
        localdb.stop(root)
        with pytest.raises(LedgerOpsError, match="^LEDGER_CLUSTER_NOT_RUNNING$"):
            ledger_ops.backup(root, tmp_path / "backups")
        assert not running(root)
        assert os.listdir(tmp_path / "backups") == [Path(result["backup"]).name]
    report = ledger_ops.drill(result["backup"], scratch)
    assert report["result"] == "PASSED" and report["schema_version"] == LEDGER_VERSION
    assert report["checks"]["repository_check_role"] == (
        f"NOT_APPLICABLE_SCHEMA_{LEDGER_VERSION}_CODE_{SCHEMA_VERSION}"
    )
    assert set(report["roles_verified"]) == PLAN_ROLES - LATER_ROLES
    assert list(scratch.iterdir()) == []


def test_drill_refuses_unsafe_scratch_roots_before_any_cluster(ledger, tmp_path):
    result = ledger_ops.backup(ledger, tmp_path / "backups")
    marked = tmp_path / "owner-ledger"
    marked.mkdir(mode=0o700)
    mark(marked, "ACCOUNT_LEDGER")
    with pytest.raises(LedgerOpsError, match="^DRILL_SCRATCH_IS_OWNER_LEDGER$"):
        ledger_ops.drill(result["backup"], marked)
    assert os.listdir(marked) == [localdb.LEDGER_MARKER]
    deep = tmp_path / ("d" * 90)
    deep.mkdir()
    with pytest.raises(LedgerOpsError, match="^DRILL_SCRATCH_PATH_TOO_LONG$"):
        ledger_ops.drill(result["backup"], deep)
    assert list(deep.iterdir()) == []
    with pytest.raises(LedgerOpsError, match="^INDEPENDENT_HEAD_MISMATCH$"):
        ledger_ops.drill(result["backup"], tmp_path, expected_head="0" * 64)


def test_expected_roles_follow_the_numbered_migrations():
    assert ledger_ops.expected_roles(max(localdb.migration_files())) == PLAN_ROLES
    assert ledger_ops.expected_roles(LEDGER_VERSION) == PLAN_ROLES - LATER_ROLES
    assert ledger_ops.expected_roles(1) == {"catalyst_app"}


def test_drill_cluster_uses_the_localdb_initdb_and_socket_settings():
    started = inspect.getsource(localdb.start)
    assert all(f'"{argument}"' in started for argument in ledger_ops.INITDB_ARGS)
    for setting in ("listen_addresses = ''", "port = 55437", "unix_socket_permissions = 0700"):
        assert setting in started and setting in ledger_ops.SOCKET_CONF
