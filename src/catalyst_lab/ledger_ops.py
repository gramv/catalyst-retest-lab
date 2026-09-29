"""Ledger backup, restore drill and retention (plan package 4.6). Local PostgreSQL tools only.

backup           A roles-only dump without passwords plus a custom-format dump of catalyst_lab
                 taken from an exported snapshot. The audit head, event count, schema version and
                 every table's row count are read inside that same snapshot and written, with both
                 file hashes, to manifest.json (mode 0600) in a new mode-0700 directory named by
                 UTC time. The source cluster is only read and is never started or migrated.
drill            Restores a backup into a disposable private-socket cluster, checks roles, the app
                 role, ownership, schema version, table counts, triggers and the hash chain
                 against the manifest, then destroys the cluster. It proves that a restore works;
                 it says nothing about broker state or trading readiness.
verify-manifest  Recomputes both file hashes without restoring.
prune            Deletes verified backups older than the retention, never the newest verified
                 one, and skips every directory it cannot verify.

Restore order: the roles script first, then one pg_restore run as lab_owner, the owner of every
table. pg_restore creates the tables, loads the data and only then creates indexes, constraints
and triggers (its post-data section), so no BEFORE INSERT trigger (stamp_event, audit_jev, the
guards) fires on a restored row and every stored hash survives unchanged. --disable-triggers is
therefore unnecessary; it only applies to data-only restores. The drill then proves that every
archived trigger exists and is enabled, that the immutability triggers refuse UPDATE and DELETE,
and that a rolled-back probe append chains from the restored head.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import IsolationLevel, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.config import SCHEMA_VERSION, STRATEGY_VERSION
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.repository import Repository, json_safe

FORMAT = "CATALYST_LEDGER_BACKUP_V1"
BACKUP_SCOPE = "DATABASE_BACKUP_ROLES_AND_CATALYST_LAB"
DRILL_SCOPE = "AUDIT_AND_LEDGER_RESTORE_DRILL"
VERIFY_SCOPE = "LEDGER_BACKUP_MANIFEST_VERIFICATION"
PRUNE_SCOPE = "LEDGER_BACKUP_RETENTION"
OWNER_ROLE = "lab_owner"
APP_ROLE = "catalyst_app"
DATABASE = "catalyst_lab"
MANIFEST, DUMP, ROLES = "manifest.json", "catalyst_lab.dump", "roles.sql"
BACKUP_FILES = frozenset({MANIFEST, DUMP, ROLES})
MANIFEST_KEYS = frozenset({
    "format", "scope", "taken_at", "cluster_system_identifier", "schema_version", "audit_head",
    "audit_seq", "event_count", "table_counts", "dump_sha256", "roles_sha256", "tool_versions",
})
NAME_FORMAT = "%Y%m%dT%H%M%SZ"
TAKEN_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BACKUP_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z")
# One written and verified backup, in one line: its directory name (its UTC time), schema
# version, audit sequence, audit head and manifest SHA-256. The ops service's owner command
# ``cloud_runtime backup`` prints it; the guarded cloud migration (``cloud_provision migrate
# --backup``) checks it against the live ledger. cloud_entry and .railway/railway.ts repeat this
# shape (tests/test_cloud_migrate.py keeps the copies equal).
BACKUP_REFERENCE = re.compile(
    r"([0-9]{8}T[0-9]{6}Z)\.([1-9][0-9]{0,3})\.([1-9][0-9]{0,18})\.([0-9a-f]{64})\.([0-9a-f]{64})"
)
RETENTION_DAYS = 14
TEXT_LIMIT = 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
ZERO_HASH = "0" * 64
# localdb.start's initdb arguments and private-socket settings. The drill cannot call
# localdb.start: it would apply migrations to the database being restored.
INITDB_ARGS = (
    "-U", OWNER_ROLE, "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8",
    "--no-locale",
)
SOCKET_CONF = (
    "\nlisten_addresses = ''\nport = 55437\n"
    "unix_socket_directories = '{socket}'\n"
    "unix_socket_permissions = 0700\n"
)
SOCKET_PATH_LIMIT = 103  # macOS sun_path holds 104 bytes including the terminator.
RESTORE_ORDER = "ROLES_SCRIPT_THEN_PG_RESTORE_TABLES_DATA_THEN_TRIGGERS_AS_LAB_OWNER"
CORE_TRIGGERS = frozenset(
    ("lab", "trade_events", name)
    for name in ("stamp_event", "immutable_rows", "immutable_truncate")
)
MUTATION_REFUSED = "append-only relation: mutation forbidden"
PROBE_KIND = "LAB_FIXTURE_RESTORE_DRILL_PROBE"
TABLES = """SELECT n.nspname::text, c.relname::text FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p') AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_' ORDER BY 1, 2"""
# The same fields Repository.check_role requires to be false for the service role.
APP_ROLE_CHECK = """SELECT current_user::text, rolsuper, rolcreaterole, rolcreatedb,
    has_table_privilege(current_user, 'lab.trade_events', 'UPDATE'),
    has_table_privilege(current_user, 'lab.trade_events', 'DELETE'),
    has_table_privilege(current_user, 'lab.trade_events', 'TRUNCATE'),
    has_schema_privilege(current_user, 'lab', 'CREATE')
    FROM pg_roles WHERE rolname = current_user"""
ROLE_DDL = re.compile(r"^\s*CREATE ROLE\s+([a-z_][a-z0-9_]*)", re.IGNORECASE | re.MULTILINE)
# pg_dumpall --roles-only output is executed by psql as the drill's superuser, so only these
# statement shapes are accepted: no psql meta-command (except the guard lines written by newer
# pg_dumpall versions), no password and no second statement on a line.
_IDENT = r'(?:[a-z_][a-z0-9_$]*|"(?:[^"\\;\n]|"")+")'
_ATTRIBUTE = (
    r"(?:NO)?(?:SUPERUSER|INHERIT|CREATEROLE|CREATEDB|LOGIN|REPLICATION|BYPASSRLS)"
    r"|CONNECTION LIMIT -?[0-9]+|VALID UNTIL '[0-9A-Za-z:+. -]+'"
)
_GRANT_OPTION = r"(?:ADMIN|INHERIT|SET) (?:OPTION|TRUE|FALSE)"
CREATE_ROLE = re.compile(rf"CREATE ROLE ({_IDENT});")
OWNER_ATTRIBUTES = re.compile(rf"ALTER ROLE {OWNER_ROLE} WITH(?: (?:{_ATTRIBUTE}))+;")
ROLE_SCRIPT_LINES = (
    re.compile(r""),
    re.compile(r"--[^\\]*"),
    re.compile(r"SET [a-z_]+ = (?:'[A-Za-z0-9_-]*'|[A-Za-z0-9_]+);"),
    CREATE_ROLE,
    re.compile(rf"ALTER ROLE {_IDENT} WITH(?: (?:{_ATTRIBUTE}))+;"),
    re.compile(
        rf"GRANT {_IDENT} TO {_IDENT}(?: WITH {_GRANT_OPTION}(?:, {_GRANT_OPTION})*)?"
        rf"(?: GRANTED BY {_IDENT})?;"
    ),
    re.compile(r"\\(?:un)?restrict [A-Za-z0-9]+"),
)


class LedgerOpsError(RuntimeError):
    """A refusal code. Details name files, tables, roles or a local PostgreSQL tool's error text;
    trust authentication on a private socket means no credential is ever in play."""

    def __init__(self, code: str, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def expected_roles(schema_version: int) -> frozenset[str]:
    """Roles created by schema.sql (version 1) and every numbered migration up to the version."""
    sources = [Path(localdb.__file__).with_name("schema.sql")]
    sources += [path for version, path in sorted(localdb.migration_files().items())
                if version <= schema_version]
    return frozenset(
        name.lower() for path in sources for name in ROLE_DDL.findall(path.read_text())
    )


def role_script(raw: bytes) -> tuple[bytes, frozenset[str]]:
    """Validate a roles dump; return it without the bootstrap role's lines, plus its roles.

    A cluster initialized with localdb's initdb arguments already has lab_owner, the superuser
    the drill restores as, so its CREATE ROLE and ALTER ROLE lines are dropped: the restore role
    keeps its bootstrap attributes. On a local ledger that ALTER line repeats those attributes;
    a cloud ledger (package cloud) keeps lab_owner NOLOGIN, which must not reach the drill.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LedgerOpsError("BACKUP_ROLES_SCRIPT_UNEXPECTED") from None
    kept, roles = [], set()
    for number, line in enumerate(text.split("\n"), 1):
        if "PASSWORD" in line.upper():
            raise LedgerOpsError("BACKUP_ROLES_CONTAIN_PASSWORD", f"line {number}")
        if not any(pattern.fullmatch(line) for pattern in ROLE_SCRIPT_LINES):
            raise LedgerOpsError("BACKUP_ROLES_SCRIPT_UNEXPECTED", f"line {number}")
        created = CREATE_ROLE.fullmatch(line)
        if created:
            name = created.group(1)
            name = name[1:-1].replace('""', '"') if name.startswith('"') else name
            roles.add(name)
            if name == OWNER_ROLE:
                continue
        if OWNER_ATTRIBUTES.fullmatch(line):
            continue
        kept.append(line)
    return "\n".join(kept).encode(), frozenset(roles)


def _tool_env() -> dict[str, str]:
    # Explicit connection strings only: inherited PG* variables (service files, PGOPTIONS,
    # PGDATABASE, ...) must never redirect or reconfigure a dump or a restore.
    return {key: value for key, value in os.environ.items() if not key.startswith("PG")}


def _run(name: str, *args, code: str, stdout=subprocess.PIPE, stdin: bytes | None = None,
         extra_env: dict | None = None) -> bytes:
    try:
        binary = localdb.pg_binary(name)
    except RuntimeError:
        raise LedgerOpsError("POSTGRES_TOOL_MISSING", name) from None
    # ``extra_env`` carries only PGPASSWORD for a password DSN (backup_database): the password
    # never appears in an argv, where any process listing could read it.
    result = subprocess.run(
        [binary, *map(str, args)], input=stdin, stdout=stdout, stderr=subprocess.PIPE,
        env={**_tool_env(), **(extra_env or {})}, check=False,
    )
    if result.returncode:
        # PostgreSQL tool output stays local; trust auth means no password is ever involved.
        raise LedgerOpsError(code, result.stderr.decode(errors="replace").strip()[-500:])
    return result.stdout or b""


def _version(name: str) -> str:
    return _run(name, "--version", code="POSTGRES_TOOL_MISSING").decode().strip()


def _maintenance_url(root: Path) -> str:
    return localdb.connection_url(root, OWNER_ROLE).replace(
        f"dbname={DATABASE}", "dbname=postgres"
    )


def _refuse_marker(directory: Path, code: str) -> None:
    if os.path.lexists(Path(directory) / localdb.LEDGER_MARKER):
        raise LedgerOpsError(code)


def _owner_ledger(root: Path, allow_owner_ledger: bool) -> str | None:
    """Any LEDGER.json needs the explicit flag; with it, the marker must still be valid."""
    if not os.path.lexists(root / localdb.LEDGER_MARKER):
        return None
    if not allow_owner_ledger:
        raise LedgerOpsError("OWNER_LEDGER_REQUIRES_ALLOW_FLAG")
    return localdb.ledger_role(root)


def _private_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise LedgerOpsError("BACKUP_DIRECTORY_MISSING") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise LedgerOpsError("PRIVATE_OWNER_DIRECTORY_REQUIRED")
    return path


def _create_private(path: Path) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _open_private(path: Path) -> int:
    """A regular file owned by this user, mode 0600, one link, not a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise LedgerOpsError("BACKUP_FILE_NOT_PRIVATE", path.name) from None
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise LedgerOpsError("BACKUP_FILE_NOT_PRIVATE", path.name)
    return fd


def _read_private(path: Path, limit: int = TEXT_LIMIT) -> bytes:
    with os.fdopen(_open_private(path), "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise LedgerOpsError("BACKUP_FILE_TOO_LARGE", path.name)
    return raw


def _sha256_private(path: Path, copy_to: Path | None = None) -> str:
    """Stream a private file through SHA-256, optionally into a new private copy."""
    digest = hashlib.sha256()
    with contextlib.ExitStack() as stack:
        source = stack.enter_context(os.fdopen(_open_private(path), "rb"))
        copy = stack.enter_context(os.fdopen(_create_private(copy_to), "wb")) if copy_to else None
        while chunk := source.read(1 << 20):
            digest.update(chunk)
            if copy:
                copy.write(chunk)
        if copy:
            copy.flush()
            os.fsync(copy.fileno())
    return digest.hexdigest()


def _write_exclusive(path: Path, data: bytes) -> None:
    with os.fdopen(_create_private(path), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _run_to_file(path: Path, name: str, *args, code: str, extra_env: dict | None = None) -> None:
    fd = _create_private(path)
    try:
        _run(name, *args, code=code, stdout=fd, extra_env=extra_env)
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(target: Path) -> None:
    """Remove a partial backup directory this call created, and nothing else."""
    for name in BACKUP_FILES:
        with contextlib.suppress(FileNotFoundError):
            (target / name).unlink()
    with contextlib.suppress(OSError):
        target.rmdir()


def _ledger_state(conn) -> dict:
    """Schema version, audit head and count, and every user table's row count."""
    version = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
    head = conn.execute(
        "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    counts = {}
    for schema, name in conn.execute(TABLES).fetchall():
        query = sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(name)
        )
        counts[f"{schema}.{name}"] = conn.execute(query).fetchone()[0]
    return {
        "schema_version": version,
        "audit_seq": head[0] if head else None,
        "audit_head": head[1] if head else None,
        "event_count": conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0],
        "table_counts": counts,
    }


def _snapshot_state(conn) -> dict:
    return {
        **_ledger_state(conn),
        "cluster_system_identifier": conn.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0],
        "server_version": conn.execute("SHOW server_version").fetchone()[0],
    }


def _dump_database(owner_url: str, snapshot: str, path: Path, extra_env=None) -> None:
    """pg_dump imports the exported snapshot, so it holds exactly what the manifest counted."""
    _run_to_file(
        path, "pg_dump", "--format=custom", f"--snapshot={snapshot}", "--no-password",
        "--lock-wait-timeout=60000", f"--dbname={owner_url}", code="BACKUP_DUMP_FAILED",
        extra_env=extra_env,
    )


def _dump_roles(root: Path, path: Path) -> None:
    _dump_roles_from("postgres", _maintenance_url(root), path)


def _dump_roles_from(database: str, url: str, path: Path, extra_env=None) -> None:
    # --no-role-passwords reads pg_roles instead of pg_authid: no password hash is ever written.
    _run_to_file(
        path, "pg_dumpall", "--roles-only", "--no-role-passwords", "--no-password",
        f"--database={database}", f"--dbname={url}",
        code="BACKUP_ROLES_DUMP_FAILED", extra_env=extra_env,
    )


def backup(root, destination, *, allow_owner_ledger: bool = False, now=None) -> dict:
    """Back up a running cluster read-only into a new timestamped directory; never overwrite."""
    root, destination = Path(root), Path(destination)
    owner_ledger = _owner_ledger(root, allow_owner_ledger)
    _refuse_marker(destination, "BACKUP_DESTINATION_IS_OWNER_LEDGER")
    if not (root / "postgres" / "PG_VERSION").is_file():
        raise LedgerOpsError("LEDGER_CLUSTER_NOT_FOUND")
    parent = _private_directory(destination, create=True)
    owner_url = localdb.connection_url(root, OWNER_ROLE)
    try:
        conn = psycopg.connect(owner_url, connect_timeout=5)
    except psycopg.OperationalError:
        raise LedgerOpsError("LEDGER_CLUSTER_NOT_RUNNING") from None
    result = _take_backup(
        conn, parent, now,
        dump=lambda snapshot, path: _dump_database(owner_url, snapshot, path),
        roles=lambda path: _dump_roles(root, path),
    )
    return {**result, "owner_ledger": owner_ledger}


def backup_database(database_url: str, destination, *, now=None) -> dict:
    """The same backup over a password DSN (the Railway ops service, package cloud).

    The role only reads (``catalyst_backup``, a member of ``pg_read_all_data``). The password is
    handed to pg_dump and pg_dumpall in their environment only; their argv carries the DSN
    without it. The archive, roles script and manifest have exactly the format ``backup``
    writes, so ``verify-manifest``, ``prune`` and the restore ``drill`` accept them unchanged.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    destination = Path(destination)
    _refuse_marker(destination, "BACKUP_DESTINATION_IS_OWNER_LEDGER")
    try:
        parts = conninfo_to_dict(database_url)
    except psycopg.Error:
        raise LedgerOpsError("BACKUP_DATABASE_URL_INVALID") from None
    if parts.get("dbname") != DATABASE:
        raise LedgerOpsError("BACKUP_DATABASE_UNEXPECTED")
    password = parts.pop("password", None)
    tool_url = make_conninfo(**parts)
    tool_env = {"PGPASSWORD": password} if password else None
    parent = _private_directory(destination, create=True)
    try:
        conn = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        raise LedgerOpsError("BACKUP_DATABASE_UNAVAILABLE") from None
    result = _take_backup(
        conn, parent, now,
        dump=lambda snapshot, path: _dump_database(tool_url, snapshot, path, tool_env),
        roles=lambda path: _dump_roles_from(DATABASE, tool_url, path, tool_env),
    )
    return {**result, "owner_ledger": None}


def _take_backup(conn, parent: Path, now, *, dump, roles) -> dict:
    """One REPEATABLE READ snapshot: the manifest's counts and the dump see the same rows."""
    with conn:
        taken = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        target = parent / taken.strftime(NAME_FORMAT)
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            raise LedgerOpsError("BACKUP_DESTINATION_EXISTS") from None
        try:
            os.chmod(target, 0o700)
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            # The first statement fixes the transaction snapshot: the state read below and the
            # dump (which imports it) see the same committed rows, while writers carry on.
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            state = _snapshot_state(conn)
            dump(snapshot, target / DUMP)
            roles(target / ROLES)
            conn.rollback()
            roles_raw = _read_private(target / ROLES)
            role_script(roles_raw)  # No password and only the allowlisted statement shapes.
            manifest = {
                "format": FORMAT,
                "scope": BACKUP_SCOPE,
                "taken_at": taken.strftime(TAKEN_AT_FORMAT),
                "cluster_system_identifier": state["cluster_system_identifier"],
                "schema_version": state["schema_version"],
                "audit_head": state["audit_head"],
                "audit_seq": state["audit_seq"],
                "event_count": state["event_count"],
                "table_counts": state["table_counts"],
                "dump_sha256": _sha256_private(target / DUMP),
                "roles_sha256": hashlib.sha256(roles_raw).hexdigest(),
                "tool_versions": {
                    "pg_dump": _version("pg_dump"),
                    "pg_dumpall": _version("pg_dumpall"),
                    "server": state["server_version"],
                },
            }
            raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            _write_exclusive(target / MANIFEST, raw)
            _fsync_directory(target)
            _fsync_directory(parent)
        except BaseException:
            _discard(target)
            raise
    return {
        "scope": BACKUP_SCOPE,
        "result": "WRITTEN",
        "backup": str(target),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "taken_at": manifest["taken_at"],
        "schema_version": manifest["schema_version"],
        "audit_head": manifest["audit_head"],
        "audit_seq": manifest["audit_seq"],
        "event_count": manifest["event_count"],
        "tables": len(manifest["table_counts"]),
        "broker_requests": 0,
    }


def _manifest_valid(manifest) -> bool:
    def count(value, minimum=0):
        return type(value) is int and value >= minimum

    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        return False
    try:
        datetime.strptime(manifest["taken_at"], TAKEN_AT_FORMAT)
    except (TypeError, ValueError):
        return False
    head, seq = manifest["audit_head"], manifest["audit_seq"]
    counts, tools = manifest["table_counts"], manifest["tool_versions"]
    identifier = manifest["cluster_system_identifier"]
    return bool(
        manifest["format"] == FORMAT
        and manifest["scope"] == BACKUP_SCOPE
        and isinstance(identifier, str) and re.fullmatch(r"[0-9]+", identifier)
        and count(manifest["schema_version"], 1)
        and count(manifest["event_count"])
        and ((head is None and seq is None)
             or (isinstance(head, str) and HEX64.fullmatch(head) and count(seq, 1)))
        and isinstance(counts, dict)
        and all(isinstance(key, str) and count(value) for key, value in counts.items())
        and counts.get("lab.trade_events") == manifest["event_count"]
        and all(isinstance(manifest[key], str) and HEX64.fullmatch(manifest[key])
                for key in ("dump_sha256", "roles_sha256"))
        and isinstance(tools, dict) and all(isinstance(value, str) for value in tools.values())
    )


def _load_backup(directory: Path) -> tuple[dict, str]:
    """A private backup directory holding exactly the three files and a well-formed manifest."""
    _private_directory(directory)
    if set(os.listdir(directory)) != BACKUP_FILES:
        raise LedgerOpsError("BACKUP_FILES_UNEXPECTED")
    raw = _read_private(directory / MANIFEST)
    try:
        manifest = strict_json(raw)
    except ValueError:
        raise LedgerOpsError("BACKUP_MANIFEST_INVALID") from None
    if not _manifest_valid(manifest):
        raise LedgerOpsError("BACKUP_MANIFEST_INVALID")
    return manifest, hashlib.sha256(raw).hexdigest()


def _check_anchors(manifest, manifest_sha256, expected_head, expected_manifest_sha256) -> None:
    """Independently retained values; the manifest alone cannot vouch for itself."""
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != manifest_sha256:
        raise LedgerOpsError("INDEPENDENT_MANIFEST_MISMATCH")
    if expected_head is not None and expected_head != manifest["audit_head"]:
        raise LedgerOpsError("INDEPENDENT_HEAD_MISMATCH")


def _verify_files(directory: Path, manifest: dict, copy_dump_to: Path | None = None) -> bytes:
    if _sha256_private(directory / DUMP, copy_dump_to) != manifest["dump_sha256"]:
        raise LedgerOpsError("BACKUP_DUMP_HASH_MISMATCH")
    roles_raw = _read_private(directory / ROLES)
    if hashlib.sha256(roles_raw).hexdigest() != manifest["roles_sha256"]:
        raise LedgerOpsError("BACKUP_ROLES_HASH_MISMATCH")
    return roles_raw


def verify_manifest(backup_dir, *, expected_head=None, expected_manifest_sha256=None) -> dict:
    """Recompute both file hashes against the manifest; nothing is restored."""
    directory = Path(backup_dir)
    manifest, manifest_sha256 = _load_backup(directory)
    _check_anchors(manifest, manifest_sha256, expected_head, expected_manifest_sha256)
    role_script(_verify_files(directory, manifest))
    return {
        "scope": VERIFY_SCOPE,
        "result": "MATCH",
        "backup": str(directory),
        "manifest_sha256": manifest_sha256,
        "dump_sha256": manifest["dump_sha256"],
        "roles_sha256": manifest["roles_sha256"],
        "taken_at": manifest["taken_at"],
        "schema_version": manifest["schema_version"],
        "audit_head": manifest["audit_head"],
        "audit_seq": manifest["audit_seq"],
        "event_count": manifest["event_count"],
        "independent_head_verified": expected_head is not None,
        "independent_manifest_verified": expected_manifest_sha256 is not None,
        "restored": False,
    }


def backup_reference(taken: dict) -> str:
    """The one-line reference (``BACKUP_REFERENCE``) of a backup ``backup`` or
    ``backup_database`` wrote; an empty ledger has no head to name, so no reference."""
    name = Path(taken["backup"]).name
    reference = ".".join(str(part) for part in (
        name, taken["schema_version"], taken["audit_seq"], taken["audit_head"],
        taken["manifest_sha256"]))
    if taken["audit_head"] is None or not BACKUP_REFERENCE.fullmatch(reference):
        raise LedgerOpsError("BACKUP_REFERENCE_UNAVAILABLE")
    return reference


def parse_backup_reference(text) -> dict:
    """A ``BACKUP_REFERENCE`` as its parts; ``taken_at`` is the backup's UTC time."""
    match = BACKUP_REFERENCE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise LedgerOpsError("BACKUP_REFERENCE_INVALID")
    name, schema_version, audit_seq, audit_head, manifest_sha256 = match.groups()
    try:
        taken_at = datetime.strptime(name, NAME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise LedgerOpsError("BACKUP_REFERENCE_INVALID") from None
    return {"backup": name, "taken_at": taken_at, "schema_version": int(schema_version),
            "audit_seq": int(audit_seq), "audit_head": audit_head,
            "manifest_sha256": manifest_sha256}


def _archive_toc(dump: Path) -> frozenset[tuple[str, str, str]]:
    """The archived database name must be catalyst_lab; returns every archived trigger."""
    listing = _run("pg_restore", "--list", "--create", dump, code="BACKUP_DUMP_UNREADABLE")
    databases, triggers = [], set()
    for line in listing.decode().splitlines():
        parts = line.split()
        if line.startswith(";") or len(parts) < 4:
            continue
        if parts[3] == "DATABASE" and len(parts) == 7 and parts[4] == "-":
            databases.append(parts[5])
        elif parts[3] == "TRIGGER":
            if len(parts) != 8:  # "<id>; <oids> TRIGGER <schema> <table> <name> <owner>"
                raise LedgerOpsError("BACKUP_DUMP_TOC_UNEXPECTED")
            triggers.add((parts[4], parts[5], parts[6]))
    if databases != [DATABASE]:
        raise LedgerOpsError("BACKUP_DUMP_DATABASE_UNEXPECTED")
    return frozenset(triggers)


def _start_scratch_cluster(cluster: Path) -> None:
    cluster.mkdir(mode=0o700)
    socket = cluster / "socket"
    socket.mkdir(mode=0o700)
    data = cluster / "postgres"
    _run("initdb", "-D", data, *INITDB_ARGS, code="DRILL_CLUSTER_INIT_FAILED")
    with (data / "postgresql.conf").open("a") as conf:
        conf.write(SOCKET_CONF.format(socket=str(socket.resolve()).replace("'", "''")))
    _run(
        "pg_ctl", "-D", data, "-l", cluster / "postgres.log", "-w", "start",
        code="DRILL_CLUSTER_START_FAILED",
    )


def _destroy(work: Path, cluster: Path) -> bool:
    """Stop the disposable cluster and delete it; a server that will not stop is left alone."""
    data = cluster / "postgres"
    if (data / "postmaster.pid").exists():
        for mode in ("fast", "immediate"):
            try:
                _run("pg_ctl", "-D", data, "-m", mode, "-w", "stop", code="DRILL_STOP_FAILED")
                break
            except LedgerOpsError:
                continue
        else:
            return False
    shutil.rmtree(work, ignore_errors=True)
    return not os.path.lexists(work)


def _events(conn) -> Iterator[dict]:
    """Rows shaped exactly like Repository.export_events, streamed from a server cursor."""
    with conn.cursor(name="ledger_drill_events", row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM lab.trade_events ORDER BY seq")
        for row in cursor:
            item = json_safe(row)
            item["created_at"] = row["created_at"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            yield item


def _compare_state(state: dict, manifest: dict) -> None:
    if state["schema_version"] != manifest["schema_version"]:
        raise LedgerOpsError("DRILL_SCHEMA_VERSION_MISMATCH")
    restored, recorded = state["table_counts"], manifest["table_counts"]
    if restored != recorded:
        differing = sorted(
            name for name in restored.keys() | recorded.keys()
            if restored.get(name) != recorded.get(name)
        )
        raise LedgerOpsError("DRILL_TABLE_COUNT_MISMATCH", differing)
    keys = ("audit_head", "audit_seq", "event_count")
    if any(state[key] != manifest[key] for key in keys):
        raise LedgerOpsError("DRILL_AUDIT_HEAD_MISMATCH")


def _verify_chain(conn, manifest: dict) -> dict:
    try:
        proof = verify_events(_events(conn), manifest["audit_head"])
    except (KeyError, ValueError) as exc:
        raise LedgerOpsError("DRILL_AUDIT_CHAIN_INVALID", str(exc)) from None
    if (proof["event_count"], proof["head_hash"]) != (
        manifest["event_count"], manifest["audit_head"] or ZERO_HASH
    ):
        raise LedgerOpsError("DRILL_AUDIT_CHAIN_INVALID")
    return proof


def _check_roles(conn, schema_version: int, dumped: frozenset[str]):
    """Every role exists; no application role can alter schema or the audit log."""
    expected = expected_roles(schema_version)
    rows = {
        row[0]: row[1:] for row in conn.execute(
            """SELECT rolname::text, rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication
                   OR rolbypassrls, rolcanlogin FROM pg_roles"""
        ).fetchall()
    }
    missing = sorted((expected | dumped) - rows.keys())
    if missing:
        raise LedgerOpsError("DRILL_ROLE_MISSING", missing)
    broad = []
    for role in sorted(expected):
        privileges = conn.execute(
            """SELECT has_schema_privilege(%(r)s::name, 'lab', 'CREATE'),
                has_table_privilege(%(r)s::name, 'lab.trade_events', 'UPDATE'),
                has_table_privilege(%(r)s::name, 'lab.trade_events', 'DELETE'),
                has_table_privilege(%(r)s::name, 'lab.trade_events', 'TRUNCATE')""",
            {"r": role},
        ).fetchone()
        if rows[role][0] or any(privileges):
            broad.append(role)
    if broad:
        raise LedgerOpsError("DRILL_ROLE_TOO_BROAD", broad)
    return sorted(expected), sorted(role for role in expected if rows[role][1])


def _check_ownership(conn) -> None:
    """Application credentials must not own the schema, its relations or its functions."""
    owners = {row[0] for row in conn.execute(
        """SELECT pg_get_userbyid(nspowner)::text FROM pg_namespace WHERE nspname = 'lab'
        UNION SELECT pg_get_userbyid(c.relowner)::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'lab'
        UNION SELECT pg_get_userbyid(p.proowner)::text FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'lab'"""
    ).fetchall()}
    if owners != {OWNER_ROLE}:
        raise LedgerOpsError("DRILL_OWNERSHIP_UNEXPECTED", sorted(owners - {OWNER_ROLE}))


def _check_triggers(conn, archived: frozenset[tuple[str, str, str]]) -> int:
    rows = conn.execute(
        """SELECT n.nspname::text, c.relname::text, t.tgname::text, t.tgenabled::text
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal"""
    ).fetchall()
    restored = {tuple(row[:3]) for row in rows}
    if restored != archived or not CORE_TRIGGERS <= restored:
        differing = (restored ^ archived) | (CORE_TRIGGERS - restored)
        raise LedgerOpsError("DRILL_TRIGGERS_MISMATCH", sorted(".".join(t) for t in differing))
    disabled = sorted(".".join(row[:3]) for row in rows if row[3] != "O")
    if disabled:
        raise LedgerOpsError("DRILL_TRIGGER_DISABLED", disabled)
    return len(restored)


def _probe_append_only(conn, seq: int | None) -> str:
    """UPDATE and DELETE of the restored head must be refused; each attempt is rolled back."""
    if seq is None:
        return "NOT_APPLICABLE_EMPTY_LEDGER"
    for statement in (
        "UPDATE lab.trade_events SET event_type = event_type WHERE seq = %s",
        "DELETE FROM lab.trade_events WHERE seq = %s",
    ):
        refused = False
        try:
            conn.execute(statement, (seq,))
        except psycopg.Error as exc:
            refused = exc.diag.message_primary == MUTATION_REFUSED
        finally:
            conn.rollback()
        if not refused:
            raise LedgerOpsError("DRILL_APPEND_ONLY_NOT_ENFORCED")
    return "UPDATE_AND_DELETE_REFUSED"


def _check_login(cluster: Path, role: str) -> None:
    try:
        with psycopg.connect(localdb.connection_url(cluster, role), connect_timeout=5) as conn:
            if conn.execute("SELECT current_user::text").fetchone()[0] == role:
                return
    except psycopg.Error:
        pass
    raise LedgerOpsError("DRILL_ROLE_LOGIN_FAILED", role)


def _check_app_role(cluster: Path, schema_version: int) -> str:
    url = localdb.connection_url(cluster, APP_ROLE)
    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            row = conn.execute(APP_ROLE_CHECK).fetchone()
            version = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
    except psycopg.Error:
        raise LedgerOpsError("DRILL_APP_ROLE_CHECK_FAILED") from None
    if row[0] != APP_ROLE or any(row[1:]) or version != schema_version:
        raise LedgerOpsError("DRILL_APP_ROLE_CHECK_FAILED")
    if schema_version != SCHEMA_VERSION:
        # A pre-migration backup (the owner's step-4 case): the code's version check cannot pass.
        return f"NOT_APPLICABLE_SCHEMA_{schema_version}_CODE_{SCHEMA_VERSION}"
    try:
        Repository(url).check_role()
    except RuntimeError:
        raise LedgerOpsError("DRILL_APP_ROLE_CHECK_FAILED") from None
    return "PASSED"


def _probe_continuity(cluster: Path, manifest: dict) -> str:
    """A rolled-back app-role append must chain from the restored head."""
    try:
        with psycopg.connect(localdb.connection_url(cluster, APP_ROLE), connect_timeout=5) as conn:
            seq, previous = conn.execute(
                """INSERT INTO lab.trade_events(strategy_version, event_type, payload_json)
                VALUES (%s, 'SYSTEM_EVENT', %s) RETURNING seq, previous_hash""",
                (STRATEGY_VERSION, Jsonb({"kind": PROBE_KIND})),
            ).fetchone()
            conn.rollback()  # Never committed, even in the disposable copy.
    except psycopg.Error:
        raise LedgerOpsError("DRILL_APPEND_CONTINUITY_FAILED") from None
    if (seq, previous) != ((manifest["audit_seq"] or 0) + 1, manifest["audit_head"] or ZERO_HASH):
        raise LedgerOpsError("DRILL_APPEND_CONTINUITY_FAILED")
    return "CHAINED_FROM_RESTORED_HEAD_ROLLED_BACK"


def _restore_and_verify(cluster, dump, script, dumped_roles, archived_triggers, manifest):
    admin = _maintenance_url(cluster)
    with psycopg.connect(admin, connect_timeout=5) as conn:
        identifier = conn.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
    if identifier == manifest["cluster_system_identifier"]:
        raise LedgerOpsError("DRILL_NOT_ISOLATED")
    _run(
        "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction", "--no-password",
        "-d", admin, "-f", "-", code="DRILL_ROLES_RESTORE_FAILED", stdin=script,
    )
    _run(
        "pg_restore", "--exit-on-error", "--create", "--no-password", f"--dbname={admin}", dump,
        code="DRILL_DATABASE_RESTORE_FAILED",
    )
    owner_url = localdb.connection_url(cluster, OWNER_ROLE)
    with psycopg.connect(owner_url, connect_timeout=5, options="-c timezone=UTC") as conn:
        _compare_state(_ledger_state(conn), manifest)
        _verify_chain(conn, manifest)
        roles, logins = _check_roles(conn, manifest["schema_version"], dumped_roles)
        _check_ownership(conn)
        triggers = _check_triggers(conn, archived_triggers)
        conn.rollback()
        append_only = _probe_append_only(conn, manifest["audit_seq"])
    for role in logins:
        _check_login(cluster, role)
    repository_check = _check_app_role(cluster, manifest["schema_version"])
    continuity = _probe_continuity(cluster, manifest)
    checks = {
        "dump_sha256": "MATCH",
        "roles_sha256": "MATCH",
        "roles_script": "ALLOWLISTED_NO_PASSWORDS",
        "roles": "PRESENT_AND_RESTRICTED",
        "role_logins": "PASSED",
        "app_role": "PASSED",
        "repository_check_role": repository_check,
        "ownership": "LAB_OWNER_ONLY",
        "schema_version": "MATCH",
        "table_counts": "MATCH",
        "audit_head": "MATCH",
        "audit_chain": "VERIFIED",
        "triggers": "PRESENT_AND_ENABLED",
        "append_only_probe": append_only,
        "append_continuity_probe": continuity,
    }
    return checks, {"identifier": identifier, "roles": roles, "triggers": triggers}


def drill(backup_dir, scratch_root, *, expected_head=None, expected_manifest_sha256=None) -> dict:
    """Restore into a disposable cluster, verify it against the manifest, then destroy it."""
    directory = Path(backup_dir)
    manifest, manifest_sha256 = _load_backup(directory)
    _check_anchors(manifest, manifest_sha256, expected_head, expected_manifest_sha256)
    _refuse_marker(Path(scratch_root), "DRILL_SCRATCH_IS_OWNER_LEDGER")
    try:
        scratch = Path(scratch_root).resolve(strict=True)
    except OSError:
        raise LedgerOpsError("DRILL_SCRATCH_ROOT_MISSING") from None
    if not scratch.is_dir():
        raise LedgerOpsError("DRILL_SCRATCH_ROOT_MISSING")
    work = Path(tempfile.mkdtemp(prefix="ledger-drill-", dir=scratch))
    cluster = work / "cluster"
    try:
        if len(str(cluster / "socket" / ".s.PGSQL.55437").encode()) > SOCKET_PATH_LIMIT:
            raise LedgerOpsError("DRILL_SCRATCH_PATH_TOO_LONG")
        dump = work / DUMP  # The bytes restored are exactly the bytes hashed.
        script, dumped_roles = role_script(_verify_files(directory, manifest, dump))
        archived_triggers = _archive_toc(dump)
        _start_scratch_cluster(cluster)
        checks, found = _restore_and_verify(
            cluster, dump, script, dumped_roles, archived_triggers, manifest
        )
    finally:
        destroyed = _destroy(work, cluster)
    if not destroyed:
        raise LedgerOpsError("DRILL_SCRATCH_NOT_DESTROYED", str(work))
    return {
        "scope": DRILL_SCOPE,
        "result": "PASSED",
        "backup": str(directory),
        "manifest_sha256": manifest_sha256,
        "taken_at": manifest["taken_at"],
        "schema_version": manifest["schema_version"],
        "audit_head": manifest["audit_head"],
        "audit_seq": manifest["audit_seq"],
        "event_count": manifest["event_count"],
        "source_cluster_system_identifier": manifest["cluster_system_identifier"],
        "drill_cluster_system_identifier": found["identifier"],
        "independent_head_verified": expected_head is not None,
        "independent_manifest_verified": expected_manifest_sha256 is not None,
        "restore_order": RESTORE_ORDER,
        "checks": checks,
        "roles_verified": found["roles"],
        "tables_verified": len(manifest["table_counts"]),
        "triggers_verified": found["triggers"],
        "scratch_destroyed": True,
        "broker_requests": 0,
        "limits": "Disposable restore only: broker positions, orders, reservations and running "
                  "sessions are outside this drill.",
    }


def _delete_backup(directory: Path) -> None:
    for name in BACKUP_FILES:
        (directory / name).unlink()
    directory.rmdir()


def prune(destination, retention_days: int = RETENTION_DAYS, *, dry_run: bool = False,
          now=None) -> dict:
    """Delete verified backups older than the retention; never the newest verified backup.

    Only timestamp-named directories whose manifest and both file hashes verify are candidates;
    anything else is reported and left untouched.
    """
    if type(retention_days) is not int or retention_days < 1:
        raise LedgerOpsError("RETENTION_DAYS_INVALID")
    parent = Path(destination)
    _refuse_marker(parent, "BACKUP_DESTINATION_IS_OWNER_LEDGER")
    _private_directory(parent)
    cutoff = (now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=retention_days)
    verified, unverifiable, ignored = [], [], []
    for entry in sorted(parent.iterdir()):
        if not BACKUP_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
            ignored.append(entry.name)
            continue
        try:
            manifest, _ = _load_backup(entry)
            _verify_files(entry, manifest)
            taken = datetime.strptime(manifest["taken_at"], TAKEN_AT_FORMAT).replace(tzinfo=UTC)
            if taken.strftime(NAME_FORMAT) != entry.name:
                raise LedgerOpsError("BACKUP_NAME_MISMATCH")
        except LedgerOpsError:
            unverifiable.append(entry.name)
            continue
        verified.append((taken, entry))
    newest = max(verified, key=lambda item: item[0], default=None)
    expired = [entry for taken, entry in verified if taken < cutoff and entry != newest[1]]
    if not dry_run:
        for entry in expired:
            _delete_backup(entry)
    return {
        "scope": PRUNE_SCOPE,
        "retention_days": retention_days,
        "cutoff": cutoff.strftime(TAKEN_AT_FORMAT),
        "dry_run": dry_run,
        "deleted": [] if dry_run else [entry.name for entry in expired],
        "would_delete": [entry.name for entry in expired] if dry_run else [],
        "kept": [entry.name for _, entry in verified if entry not in expired],
        "newest_verified": newest[1].name if newest else None,
        "skipped_unverifiable": unverifiable,
        "ignored": ignored,
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m catalyst_lab.ledger_ops", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_backup = commands.add_parser("backup", help="read-only backup of a running cluster")
    run_backup.add_argument("--root", type=Path, required=True,
                            help="cluster directory holding postgres/ and socket/")
    run_backup.add_argument("--destination", type=Path, required=True,
                            help="private backups directory; each backup gets a new subdirectory")
    run_backup.add_argument("--allow-owner-ledger", action="store_true",
                            help="required when --root carries LEDGER.json (owner only)")
    run_drill = commands.add_parser("drill", help="restore into a disposable cluster and verify")
    run_drill.add_argument("--backup", type=Path, required=True)
    run_drill.add_argument("--scratch-root", type=Path, default=Path("/tmp"),
                           help="short path for the disposable cluster's socket (default /tmp)")
    verify = commands.add_parser("verify-manifest", help="recompute hashes without restoring")
    verify.add_argument("--backup", type=Path, required=True)
    for command in (run_drill, verify):
        command.add_argument("--expected-head", help="independently retained audit head")
        command.add_argument("--expected-manifest-sha256",
                             help="manifest SHA-256 recorded when the backup was taken")
    run_prune = commands.add_parser("prune", help="apply the retention to verified backups")
    run_prune.add_argument("--destination", type=Path, required=True)
    run_prune.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    run_prune.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup(args.root, args.destination,
                            allow_owner_ledger=args.allow_owner_ledger)
        elif args.command == "prune":
            result = prune(args.destination, args.retention_days, dry_run=args.dry_run)
        else:
            anchors = {"expected_head": args.expected_head,
                       "expected_manifest_sha256": args.expected_manifest_sha256}
            if args.command == "drill":
                result = drill(args.backup, args.scratch_root, **anchors)
            else:
                result = verify_manifest(args.backup, **anchors)
    except (RuntimeError, OSError, psycopg.Error) as exc:
        code = exc.code if isinstance(exc, LedgerOpsError) else str(exc)
        failure = {"command": args.command, "result": "REFUSED",
                   "code": code if CODE.fullmatch(code) else "LEDGER_OPS_FAILED"}
        if isinstance(exc, LedgerOpsError) and exc.detail:
            failure["detail"] = exc.detail
        raise SystemExit(json.dumps(failure, sort_keys=True)) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
