"""Dedicated local PostgreSQL cluster, private Unix socket, no TCP listener."""

import errno
import hashlib
import json
import os
import re
import shutil
import socket as sockets
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).with_name("migrations")
# Owner-written mode-0600 marker. A marked ledger is never initialized or migrated implicitly.
LEDGER_MARKER = "LEDGER.json"
PROTECTED_LEDGER_ROLES = frozenset({"ACCOUNT_LEDGER", "ARCHIVED"})
# A retired marker keeps its role and every earlier key and adds these two (package cloud,
# 2026-09-27): the ledger stays protected and readable, but no executor starts against it.
RETIREMENT_KEYS = ("retired_at", "retired_reason")
RETIREMENT_REASON = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
MARKER_LIMIT = 65536
# ledger-retire proves the Mac executor is stopped (package cloud-hardening). Every executor of
# a ledger holds this session advisory lock in its database for its whole life
# (executor_lease.LEGACY_EXECUTOR_LOCK; the V1 market observer takes the same one).
EXECUTOR_LOCK = 719172027
# The Mac app's database logins (MANAGED_DATABASE_URL, JEV_WORKER_DATABASE_URL): a session of
# either means an app is running or starting.
APP_ROLES = ("catalyst_risk", "catalyst_jev")
# MANAGED_HTTP_PORT of the private config v2 (deploy/private-paper.example.json), on loopback.
MAC_APP_PORT = 8780


def pg_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"PostgreSQL executable required: {name}")
    return path


def run_pg(name: str, *args):
    result = subprocess.run([pg_binary(name), *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        # Command contains no credentials; PostgreSQL output stays local.
        raise RuntimeError(f"{name} failed: {result.stderr.strip()}")


def connection_url(root: Path, role="catalyst_app") -> str:
    # libpq keyword DSN supports spaces in this project's path.
    socket = str((root / "socket").resolve()).replace("\\", "\\\\").replace("'", "\\'")
    return f"host='{socket}' port=55437 dbname=catalyst_lab user={role}"


def migration_files() -> dict[int, Path]:
    """Numbered migrations by version; two files sharing a number are ambiguous."""
    files: dict[int, Path] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        version = int(path.name.split("_")[0])
        if version in files:
            raise RuntimeError("DUPLICATE_MIGRATION_VERSION")
        files[version] = path
    return files


def ledger_role(root: Path) -> str | None:
    """Role in the owner's marker, or None when absent. An unreadable marker fails closed."""
    marker = root / LEDGER_MARKER
    if not os.path.lexists(marker):
        return None
    try:
        role = json.loads(marker.read_text())["role"]
    except (OSError, ValueError, TypeError, KeyError):
        role = None
    if not isinstance(role, str) or role not in PROTECTED_LEDGER_ROLES:
        raise RuntimeError("OWNER_LEDGER_MARKER_INVALID")
    return role


def _marker_document(marker: Path) -> dict:
    """The whole marker object: a regular file of this user, never a symlink, fail closed."""
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise RuntimeError("OWNER_LEDGER_MARKER_MISSING") from None
    except OSError:
        raise RuntimeError("OWNER_LEDGER_MARKER_INVALID") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError
            raw = stream.read(MARKER_LIMIT + 1)
        if len(raw) > MARKER_LIMIT:
            raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("role") not in PROTECTED_LEDGER_ROLES:
            raise ValueError
        return document
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError("OWNER_LEDGER_MARKER_INVALID") from None


def ledger_retirement(root: Path) -> dict | None:
    """``{"retired_at", "retired_reason"}`` of a retired marker, None when not retired or
    unmarked. A marker with only one of the two keys, or with malformed values, fails closed."""
    marker = Path(root) / LEDGER_MARKER
    if not os.path.lexists(marker):
        return None
    document = _marker_document(marker)
    present = [key for key in RETIREMENT_KEYS if key in document]
    if not present:
        return None
    try:
        retired_at = datetime.fromisoformat(document["retired_at"])
        reason = document["retired_reason"]
        if retired_at.tzinfo is None or not RETIREMENT_REASON.fullmatch(reason):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("OWNER_LEDGER_MARKER_INVALID") from None
    return {"retired_at": document["retired_at"], "retired_reason": reason}


def retire_ledger(root: Path, reason: str, *, now: datetime | None = None) -> dict:
    """Mark a local ledger RETIRED in its LEDGER.json, keeping every earlier key (the marker
    write of ``retire_stopped_ledger``, which the owner's ``catalyst-lab ledger-retire`` runs).

    Adds ``retired_at`` and ``retired_reason`` and replaces the marker atomically (a new 0600
    file in the same directory, fsync, rename, directory fsync). It touches no database and no
    broker. Afterwards the launcher refuses the app and Muse with LEDGER_RETIRED; the ledger
    stays protected (dev-init, migration) and its cluster can still be started for reading,
    backups and exports.
    """
    if not isinstance(reason, str) or not RETIREMENT_REASON.fullmatch(reason):
        raise RuntimeError("LEDGER_RETIREMENT_REASON_INVALID")
    root = Path(root)
    marker = root / LEDGER_MARKER
    document = _marker_document(marker)
    if any(key in document for key in RETIREMENT_KEYS):
        raise RuntimeError("LEDGER_ALREADY_RETIRED")
    retired_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    updated = {**document, "retired_at": retired_at, "retired_reason": reason}
    directory = marker.parent
    fd, temporary = tempfile.mkstemp(prefix=".LEDGER-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(updated, indent=2, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return {"marker": str(marker), "role": updated["role"], "retired_at": retired_at,
            "retired_reason": reason, "kept_keys": sorted(document), "broker_requests": 0,
            "database_changes": 0}


def app_port_bound(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something is bound to the Mac app's loopback port, by binding it (no connection
    is made). SO_REUSEADDR lets a stopped app's TIME_WAIT leftovers through, never a listener."""
    probe = sockets.socket(sockets.AF_INET, sockets.SOCK_STREAM)
    try:
        probe.setsockopt(sockets.SOL_SOCKET, sockets.SO_REUSEADDR, 1)
        probe.bind((host, port))
        return False
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return True
        raise RuntimeError("LEDGER_APP_PORT_UNVERIFIED") from None
    finally:
        probe.close()


def retire_stopped_ledger(root: Path, reason: str, *, app_port: int = MAC_APP_PORT,
                          now: datetime | None = None, port_bound=app_port_bound) -> dict:
    """``catalyst-lab ledger-retire``: retire the ledger only once its executor is proven stopped.

    Fails closed at each step (package cloud-hardening). The ledger's own cluster must be
    running (``LEDGER_CLUSTER_NOT_RUNNING``: this never starts it); on a session of the owner's
    socket, the executor lock every executor holds must be free (``LEDGER_EXECUTOR_RUNNING``),
    no session of the app's logins may exist (``LEDGER_APP_SESSIONS_PRESENT``: an app starting
    up) and nothing may be bound to the app's port (``LEDGER_APP_PORT_IN_USE``). The lock is
    then held while the marker is written, so no executor can start in between, and must still
    be held afterwards (``LEDGER_EXECUTOR_LOCK_LOST``). It changes no database row and calls no
    broker. Once retired, the launcher refuses to start the app or Muse (``LEDGER_RETIRED``).
    """
    root = Path(root)
    if not isinstance(reason, str) or not RETIREMENT_REASON.fullmatch(reason):
        raise RuntimeError("LEDGER_RETIREMENT_REASON_INVALID")
    document = _marker_document(root / LEDGER_MARKER)
    if any(key in document for key in RETIREMENT_KEYS):
        raise RuntimeError("LEDGER_ALREADY_RETIRED")
    try:
        conn = psycopg.connect(connection_url(root, "lab_owner"), autocommit=True,
                               connect_timeout=5, application_name="catalyst-ledger-retire")
    except psycopg.OperationalError:
        raise RuntimeError("LEDGER_CLUSTER_NOT_RUNNING") from None
    held = """SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted
        AND pid = pg_backend_pid() AND classid::bigint = 0 AND objid::bigint = %s
        AND objsubid = 1"""
    with conn:
        try:
            locked = conn.execute("SELECT pg_try_advisory_lock(%s)",
                                  (EXECUTOR_LOCK,)).fetchone()[0]
        except psycopg.Error:
            raise RuntimeError("LEDGER_EXECUTOR_CHECK_FAILED") from None
        if not locked:
            raise RuntimeError("LEDGER_EXECUTOR_RUNNING")
        try:
            try:
                sessions = conn.execute(
                    """SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid()
                    AND usename::text = ANY(%s)""", (list(APP_ROLES),)).fetchone()[0]
            except psycopg.Error:
                raise RuntimeError("LEDGER_EXECUTOR_CHECK_FAILED") from None
            if sessions:
                raise RuntimeError("LEDGER_APP_SESSIONS_PRESENT")
            if port_bound(app_port):
                raise RuntimeError("LEDGER_APP_PORT_IN_USE")
            result = retire_ledger(root, reason, now=now)
            try:
                still = conn.execute(held, (EXECUTOR_LOCK,)).fetchone()[0] == 1
            except psycopg.Error:
                still = False
            if not still:  # Retired, but the proof lapsed: check the Mac by hand.
                raise RuntimeError("LEDGER_EXECUTOR_LOCK_LOST")
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (EXECUTOR_LOCK,))
            except psycopg.Error:
                pass  # Closing the session releases it as well.
    return {**result, "executor_stopped": {
        "executor_lock": "FREE_THEN_HELD_WHILE_RETIRING", "app_sessions": 0,
        "app_port": app_port, "app_port_bound": False}}


def start(root: Path, *, allow_pending: bool = False) -> None:
    """Start a local cluster; a fresh one receives the schema and every migration.

    An existing cluster with pending migrations is refused unless allow_pending: owner ledgers
    change schema only through migrate_ledger. A LEDGER.json marker refuses before any change.
    """
    if ledger_role(root) is not None:
        raise RuntimeError("OWNER_LEDGER_PROTECTED")
    migrations = migration_files()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    socket = root / "socket"
    socket.mkdir(exist_ok=True, mode=0o700)
    data = root / "postgres"
    first_start = not (data / "PG_VERSION").exists()
    if first_start:
        run_pg(
            "initdb",
            "-D",
            data,
            "-U",
            "lab_owner",
            "--auth-local=trust",
            "--auth-host=reject",
            "--encoding=UTF8",
            "--no-locale",
        )
        socket_setting = str(socket.resolve()).replace("'", "''")
        with (data / "postgresql.conf").open("a") as f:
            f.write(
                f"\nlisten_addresses = ''\nport = 55437\n"
                f"unix_socket_directories = '{socket_setting}'\n"
                "unix_socket_permissions = 0700\n"
            )
    status = subprocess.run([pg_binary("pg_ctl"), "-D", str(data), "status"], capture_output=True)
    started_here = bool(status.returncode)
    if started_here:
        run_pg("pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "start")
    if first_start:
        owner_url = connection_url(root, "lab_owner")
        with psycopg.connect(
            owner_url.replace("dbname=catalyst_lab", "dbname=postgres"), autocommit=True
        ) as conn:
            conn.execute("CREATE DATABASE catalyst_lab")
        with psycopg.connect(owner_url) as conn:
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
    with psycopg.connect(connection_url(root, "lab_owner")) as conn:
        current = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        pending = [migrations[version] for version in sorted(migrations) if version > current]
        refused = bool(pending) and not (first_start or allow_pending)
        if not refused:
            for migration in pending:
                conn.execute(migration.read_text())
    if refused:
        if started_here:
            stop(root)  # Leave the unmigrated ledger stopped, as it was found.
        raise RuntimeError("PENDING_MIGRATION_REQUIRES_OWNER_STEP")


def stop(root: Path):
    run_pg("pg_ctl", "-D", root / "postgres", "-m", "fast", "-w", "stop")


def _backup_manifest_sha256(path: Path) -> str:
    try:
        info = path.lstat()  # A symlink is not accepted in place of the manifest itself.
    except OSError:
        raise RuntimeError("BACKUP_MANIFEST_MISSING") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_size:
        raise RuntimeError("BACKUP_MANIFEST_INVALID")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_head(conn) -> tuple[int | None, str | None, int]:
    seq, head = conn.execute(
        "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
    ).fetchone() or (None, None)
    return seq, head, conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0]


def migrate_ledger(root: Path, *, expect_current: int, target: int, backup_manifest: Path) -> dict:
    """Owner step: apply migrations expect_current+1..target in one owner transaction.

    Connects to an already running cluster only; it never initializes or starts one. Any refusal
    or failure rolls back every migration in the batch.
    """
    if ledger_role(root) == "ARCHIVED":
        raise RuntimeError("OWNER_LEDGER_ARCHIVED")
    if ledger_retirement(root) is not None:
        raise RuntimeError("OWNER_LEDGER_RETIRED")
    manifest_sha256 = _backup_manifest_sha256(backup_manifest)
    if target <= expect_current:
        raise RuntimeError("LEDGER_TARGET_NOT_AHEAD")
    migrations = migration_files()
    if any(version not in migrations for version in range(2, target + 1)):
        raise RuntimeError("LEDGER_MIGRATION_FILES_MISSING")
    if not (root / "postgres" / "PG_VERSION").exists():
        raise RuntimeError("LEDGER_CLUSTER_NOT_FOUND")
    try:
        conn = psycopg.connect(connection_url(root, "lab_owner"))
    except psycopg.OperationalError as exc:
        raise RuntimeError("LEDGER_CLUSTER_NOT_RUNNING") from exc
    with conn:
        # lab.stamp_event() takes this lock per append: no writer interleaves with the batch.
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        before = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        if before != expect_current:
            raise RuntimeError("LEDGER_VERSION_MISMATCH")
        head_seq, head_before, count_before = _audit_head(conn)
        for version in range(expect_current + 1, target + 1):
            conn.execute(migrations[version].read_text())
        after = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        if after != target:
            raise RuntimeError("LEDGER_TARGET_NOT_RECORDED")
        _, head_after, count_after = _audit_head(conn)
        rewritten = count_after < count_before
        if head_seq is not None:
            kept = conn.execute(
                "SELECT event_hash FROM lab.trade_events WHERE seq = %s", (head_seq,)
            ).fetchone()
            rewritten = rewritten or kept is None or kept[0] != head_before
        if rewritten:
            raise RuntimeError("AUDIT_HISTORY_REWRITTEN")
    result = {
        "before": before,
        "after": after,
        "audit_head_before": head_before,
        "audit_head_after": head_after,
        "event_count": count_after,
        "event_count_before": count_before,
        "backup_manifest_sha256": manifest_sha256,
        "broker_requests": 0,
    }
    if (head_after, count_after) != (head_before, count_before):
        # Appending is permitted history; a DDL-only batch must leave both values unchanged.
        result["warning"] = "AUDIT_HEAD_CHANGED_BY_MIGRATION"
    return result
