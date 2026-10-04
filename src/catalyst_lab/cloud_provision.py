"""One-off, owner-run provisioning of the cloud ledger (package cloud, 2026-09-27).

    python -m catalyst_lab.cloud_provision initial            # an empty Railway Postgres only
    python -m catalyst_lab.cloud_provision rotate-passwords   # new verifiers for changed passwords
    python -m catalyst_lab.cloud_provision migrate --expect-current 24 --target 25 \\
        --backup <reference>                                  # the guarded schema migration

Runs inside Railway as a temporary ``provision`` service on the private network (see
docs/RAILWAY-DEPLOYMENT.md), never on a runtime service. Inputs are environment variables only:
``MIGRATION_DATABASE_URL`` (the Postgres service's superuser connection) and, for ``initial``
and ``rotate-passwords``, one generated password per cloud login role
(``*_DATABASE_PASSWORD``, distinct, URL-safe, at least 32 characters); ``migrate`` needs no
password. Output is one JSON object of names, counts and hashes: never a password, verifier,
connection string or driver message.

``initial`` creates the ``catalyst_lab`` database (C collation, as initdb --no-locale gives a
local ledger) and, in ONE transaction, refuses anything but an empty database and a cluster
without the lab roles, applies schema.sql and every numbered migration as the NOLOGIN superuser
``lab_owner`` (so ownership matches a local ledger and the restore drill accepts cloud backups),
creates the read-only ``catalyst_backup`` role for the ops service's pg_dump (USAGE on
``lab``, SELECT on its tables and sequences, and the same for every later one through
lab_owner's default privileges: exactly what pg_dump of the ledger reads, and nothing outside
``lab``, so not ``pg_authid``), grants LOGIN plus a SCRAM verifier to exactly the roles the
cloud services use, sets every other lab role NOLOGIN, checks that no login role can read
``pg_authid``, and appends the audited ``CLOUD_LEDGER_PROVISIONED``
event: the cloud equivalent of the LEDGER.json ACCOUNT_LEDGER marker, which the trader requires
at startup. Statement logging is suppressed on this connection; passwords are sent only as
SCRAM verifiers. ``initial`` never migrates an existing ledger: a provisioned cloud ledger moves
only through ``migrate`` below (a Mac ledger only through the owner's ``ledger-migrate``), each
with a backup first.

``rotate-passwords`` replaces the verifier of every cloud login role from the current variables
(an unchanged password stays valid), appends ``CLOUD_ROLE_PASSWORDS_ROTATED`` and changes
nothing else. The runtime services then need a redeploy to read the new variables.

``migrate`` (package cloud-migrate, 2026-09-28) is ``catalyst-lab ledger-migrate`` for the cloud
ledger, with the same guards and a fresh backup named first. It changes nothing unless:

- the backup reference (``ledger_ops.BACKUP_REFERENCE``, printed by ``cloud_entry backup`` in
  the ops shell) is at ``--expect-current``, at most ``BACKUP_MAX_AGE`` old, and names an event
  of this ledger's own chain (its sequence and hash) no newer than the backup;
- the ledger is the provisioned cloud account ledger at exactly ``--expect-current``;
- every file ``--expect-current``+1 .. ``--target`` is in this release, and ``--target`` is the
  release's own schema (``config.SCHEMA_VERSION``: a release runs on no other).

Then, in ONE transaction holding the audit lock (``lab.stamp_event``'s, so no append interleaves;
lock and statement waits bounded), it applies those files in order as ``lab_owner`` and checks
that the schema recorded the target, that no audit event was removed or rewritten, that a new
role stays NOLOGIN, that no login role became broad, that ``lab_owner`` still owns everything and
that ``catalyst_backup`` can still read every relation (the daily backup). It appends the audited
``CLOUD_LEDGER_MIGRATED`` event (the files' hashes, the backup reference) and commits. Any
refusal or failure before the commit rolls back every file; a second connection then verifies
the committed version and event.
"""

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab.config import SCHEMA_VERSION, STRATEGY_VERSION
from catalyst_lab.ledger_ops import (
    OWNER_ROLE,
    LedgerOpsError,
    _check_ownership,
    expected_roles,
    parse_backup_reference,
)
from catalyst_lab.managed_ops import code_version

DATABASE = "catalyst_lab"
BACKUP_ROLE = "catalyst_backup"
PUBLIC_ROLE = "catalyst_public"
# The roles the cloud services log in as, and the variable holding each one's password.
LOGIN_ROLES = {
    "catalyst_risk": "RISK_DATABASE_PASSWORD",        # trader: account risk and execution
    "catalyst_jev": "JEV_DATABASE_PASSWORD",          # trader: Jev review worker
    "catalyst_app": "APP_DATABASE_PASSWORD",          # ops: audit checkpoints, daily head
    "catalyst_operator": "OPERATOR_DATABASE_PASSWORD",  # ops (owner via railway ssh): halts
    BACKUP_ROLE: "BACKUP_DATABASE_PASSWORD",          # ops: daily pg_dump
    PUBLIC_ROLE: "PUBLIC_DATABASE_PASSWORD",          # experiment page (migration 024)
}
PASSWORD = re.compile(r"[A-Za-z0-9._~-]{32,256}")  # URL-safe: the DSN templates embed it as is.
LOCK = 719172026  # lab.stamp_event() and ledger migrations take the same transaction lock.
LEDGER_EVENT = "CLOUD_LEDGER_PROVISIONED"
ROTATION_EVENT = "CLOUD_ROLE_PASSWORDS_ROTATED"
MIGRATION_EVENT = "CLOUD_LEDGER_MIGRATED"
PROVISIONER = "CLOUD_PROVISION_V1"
MIGRATOR = "CLOUD_MIGRATE_V1"
# The backup named for a migration: at most this old when the migration starts (a restore loses
# what the ledger recorded after it), and the ops container's clock may differ from this one's
# by up to CLOCK_SKEW.
BACKUP_MAX_AGE = timedelta(hours=2)
CLOCK_SKEW = timedelta(minutes=5)
# The running trader keeps writing while a migration waits: no wait of the migration's session
# (the audit lock, a table lock) is unbounded, and neither is one statement.
MIGRATION_LOCK_TIMEOUT = "15s"
MIGRATION_STATEMENT_TIMEOUT = "300s"
# Server-side TCP keepalives for every session of the ledger database: a trader that vanishes
# without closing its connection (host loss) releases the executor lease in about a minute, not
# after the kernel default of two hours. Unix-socket sessions ignore them.
KEEPALIVES = (("tcp_keepalives_idle", 30), ("tcp_keepalives_interval", 10),
              ("tcp_keepalives_count", 3))
CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
QUIET = ("SET LOCAL log_statement='none'", "SET LOCAL log_min_error_statement='panic'",
         "SET LOCAL log_min_duration_statement=-1", "SET LOCAL log_duration=off")
# What pg_dump of the ledger reads, and only that (package cloud-hardening): every relation of
# the ledger is in lab (schema.sql and the migrations create nothing else but pgcrypto's
# functions in public), pg_dump needs SELECT to lock a table and read its rows or a sequence's
# state, and lab_owner's default privileges extend the same grants to every later table and
# sequence. No predefined role: pg_read_all_data would also read pg_authid's SCRAM verifiers.
BACKUP_GRANTS = (
    "GRANT USAGE ON SCHEMA lab TO {role}",
    "GRANT SELECT ON ALL TABLES IN SCHEMA lab TO {role}",
    "GRANT SELECT ON ALL SEQUENCES IN SCHEMA lab TO {role}",
    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA lab GRANT SELECT ON TABLES TO {role}",
    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA lab GRANT SELECT ON SEQUENCES TO {role}",
)


class ProvisionError(RuntimeError):
    """A refusal code plus role or variable names; never a value."""

    def __init__(self, code, names=()):
        super().__init__(code)
        self.code = code
        self.names = tuple(sorted(names))


def latest_version():
    return max(localdb.migration_files())


def lab_roles(version=None):
    """Roles schema.sql and the numbered migrations up to ``version`` create."""
    return expected_roles(latest_version() if version is None else version)


def login_roles(version=None):
    """The cloud login roles that exist at this code's schema (catalyst_public from 024 on)."""
    available = lab_roles(version) | {BACKUP_ROLE}
    return {role: name for role, name in LOGIN_ROLES.items() if role in available}


def passwords_from_env(environ, roles):
    """``{role: password}`` for ``roles``; distinct, URL-safe, at least 32 characters."""
    missing = [roles[role] for role in roles if not environ.get(roles[role])]
    if missing:
        raise ProvisionError("PROVISION_PASSWORD_MISSING", missing)
    passwords = {role: environ[name] for role, name in roles.items()}
    invalid = [roles[role] for role, value in passwords.items() if not PASSWORD.fullmatch(value)]
    if invalid:
        raise ProvisionError("PROVISION_PASSWORD_INVALID", invalid)
    if len(set(passwords.values())) != len(passwords):
        raise ProvisionError("DISTINCT_DATABASE_PASSWORDS_REQUIRED", roles.values())
    return passwords


def _connect(url, *, dbname=None, autocommit=False):
    parts = conninfo_to_dict(url)
    if dbname is not None:
        parts["dbname"] = dbname
    try:
        return psycopg.connect(make_conninfo(**parts), autocommit=autocommit, connect_timeout=10)
    except psycopg.OperationalError:
        raise ProvisionError("PROVISION_DATABASE_UNAVAILABLE") from None


def role_url(admin_url, role, password):
    """The admin connection's host, port and TLS settings, as ``role`` on the ledger."""
    parts = conninfo_to_dict(admin_url)
    parts.update(user=role, password=password, dbname=DATABASE)
    return make_conninfo(**parts)


def _quiet(conn):
    for statement in QUIET:
        conn.execute(statement)


def _set_password(conn, role, password, *, login):
    verifier = conn.pgconn.encrypt_password(password.encode(), role.encode(),
                                            b"scram-sha-256").decode()
    statement = "ALTER ROLE {} LOGIN PASSWORD {}" if login else "ALTER ROLE {} PASSWORD {}"
    conn.execute(sql.SQL(statement).format(sql.Identifier(role), sql.Literal(verifier)))


def _existing_roles(conn, names):
    return sorted(r[0] for r in conn.execute(
        "SELECT rolname::text FROM pg_roles WHERE rolname = ANY(%s)", (sorted(names),)))


def _append(conn, payload):
    """One SYSTEM_EVENT on the audit chain, inserted as catalyst_app like every other append."""
    conn.execute("SET LOCAL ROLE catalyst_app")
    seq, event_hash = conn.execute(
        """INSERT INTO lab.trade_events(strategy_version, event_type, payload_json)
        VALUES (%s, 'SYSTEM_EVENT', %s) RETURNING seq, event_hash""",
        (STRATEGY_VERSION, Jsonb(payload)),
    ).fetchone()
    conn.execute("RESET ROLE")
    return seq, event_hash


def _check_login_roles(conn, roles):
    """No login role may alter schema, bypass grants, rewrite the audit chain or read the
    cluster's password verifiers (``pg_authid``, e.g. through ``pg_read_all_data``)."""
    broad = []
    for role in sorted(roles):
        row = conn.execute(
            """SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls,
                has_schema_privilege(%(r)s::name, 'lab', 'CREATE'),
                has_table_privilege(%(r)s::name, 'lab.trade_events', 'UPDATE'),
                has_table_privilege(%(r)s::name, 'lab.trade_events', 'DELETE'),
                has_table_privilege(%(r)s::name, 'lab.trade_events', 'TRUNCATE'),
                has_table_privilege(%(r)s::name, 'pg_catalog.pg_authid', 'SELECT'),
                has_any_column_privilege(%(r)s::name, 'pg_catalog.pg_authid', 'SELECT')
            FROM pg_roles WHERE rolname = %(r)s""", {"r": role}).fetchone()
        if row is None or any(row):
            broad.append(role)
    if broad:
        raise ProvisionError("PROVISION_ROLE_TOO_BROAD", broad)


def _verify_logins(admin_url, passwords):
    """Each password actually authenticates (SCRAM) as its role on the ledger database."""
    failed = []
    for role, password in sorted(passwords.items()):
        try:
            with psycopg.connect(role_url(admin_url, role, password), connect_timeout=10) as conn:
                if conn.execute("SELECT current_user::text").fetchone()[0] != role:
                    raise ValueError
        except (psycopg.Error, ValueError):
            failed.append(role)
    if failed:
        raise ProvisionError("PROVISION_LOGIN_CHECK_FAILED", failed)
    return sorted(passwords)


def _release():
    identity = code_version()
    return {"release_commit": identity["release_commit"],
            "source_sha256": identity["source_sha256"]}


PLATFORMS = ("RAILWAY", "LOCAL_COMPOSE")


def provision(admin_url, environ, *, now=None, platform="RAILWAY"):
    """Create and provision the cloud ledger on an empty database; see the module docstring.

    ``platform`` is recorded in ``CLOUD_LEDGER_PROVISIONED``: ``RAILWAY`` (the reference
    deployment) or ``LOCAL_COMPOSE`` (package oss-packaging: the disposable ledger of the local
    Docker stack, ``local_stack provision``; the same roles, grants and checks)."""
    if platform not in PLATFORMS:
        raise ProvisionError("PROVISION_PLATFORM_UNKNOWN")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    version = latest_version()
    roles = login_roles(version)
    passwords = passwords_from_env(environ, roles)
    lab = lab_roles(version)
    with _connect(admin_url, autocommit=True) as admin:
        if not admin.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]:
            raise ProvisionError("PROVISION_SUPERUSER_REQUIRED")
        clashing = _existing_roles(admin, lab | {OWNER_ROLE, BACKUP_ROLE})
        if clashing:
            raise ProvisionError("PROVISION_ROLES_ALREADY_EXIST", clashing)
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone()
        if not exists:
            # The C collation of a local ledger (initdb --no-locale). From PostgreSQL 15 the
            # provider is named too, so an ICU-initialized server still gives libc "C".
            provider = ("LOCALE_PROVIDER libc " if int(admin.execute(
                "SHOW server_version_num").fetchone()[0]) >= 150000 else "")
            try:
                admin.execute(sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' " + provider
                    + "LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(DATABASE)))
            except psycopg.errors.DuplicateDatabase:
                pass  # A concurrent provisioner won; the emptiness check below decides.
    with _connect(admin_url, dbname=DATABASE) as conn:
        _quiet(conn)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        if conn.execute("SELECT to_regnamespace('lab') IS NOT NULL").fetchone()[0]:
            raise ProvisionError("PROVISION_LAB_SCHEMA_EXISTS")
        occupied = conn.execute(
            """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\\_%'""").fetchone()[0]
        schemas = conn.execute(
            """SELECT count(*) FROM pg_namespace WHERE nspname NOT IN
              ('public', 'pg_catalog', 'information_schema') AND nspname NOT LIKE 'pg\\_%'"""
        ).fetchone()[0]
        if occupied or schemas:
            raise ProvisionError("PROVISION_DATABASE_NOT_EMPTY")
        clashing = _existing_roles(conn, lab | {OWNER_ROLE, BACKUP_ROLE})
        if clashing:
            raise ProvisionError("PROVISION_ROLES_ALREADY_EXIST", clashing)
        # Everything below is owned by lab_owner, a NOLOGIN superuser (a local ledger's owner is
        # its initdb superuser of the same name): no one can log in as the schema owner.
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN SUPERUSER").format(
            sql.Identifier(OWNER_ROLE)))
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
        conn.execute(Path(localdb.__file__).with_name("schema.sql").read_text())
        for _, path in sorted(localdb.migration_files().items()):
            conn.execute(path.read_text())
        conn.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        ).format(sql.Identifier(BACKUP_ROLE)))
        for statement in BACKUP_GRANTS:
            conn.execute(sql.SQL(statement).format(role=sql.Identifier(BACKUP_ROLE),
                                                   owner=sql.Identifier(OWNER_ROLE)))
        conn.execute("RESET ROLE")
        schema = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        if schema != version:
            raise ProvisionError("PROVISION_SCHEMA_VERSION_MISMATCH")
        present = set(_existing_roles(conn, set(LOGIN_ROLES)))
        skipped = sorted(set(LOGIN_ROLES) - present)
        if set(roles) - present:
            raise ProvisionError("PROVISION_ROLE_MISSING", set(roles) - present)
        nologin = sorted(lab - set(roles))
        for role in nologin:
            conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
        for role, password in sorted(passwords.items()):
            _set_password(conn, role, password, login=True)
        for name, value in KEEPALIVES:
            conn.execute(sql.SQL("ALTER DATABASE {} SET {} = {}").format(
                sql.Identifier(DATABASE), sql.Identifier(name), sql.Literal(value)))
        _check_login_roles(conn, set(roles) | lab)
        try:
            _check_ownership(conn)
        except RuntimeError:
            raise ProvisionError("PROVISION_OWNERSHIP_UNEXPECTED") from None
        ledger_id = conn.execute("SELECT gen_random_uuid()::text").fetchone()[0]
        payload = {
            "kind": LEDGER_EVENT, "role": "ACCOUNT_LEDGER", "ledger_id": ledger_id,
            "platform": platform, "database": DATABASE, "schema_version": schema,
            "login_roles": sorted(roles), "nologin_roles": nologin,
            "skipped_roles": skipped, "owner_role": OWNER_ROLE, "owner_login": False,
            "provisioned_at": now.isoformat(), "provisioner": PROVISIONER, **_release(),
        }
        seq, event_hash = _append(conn, payload)
    logins = _verify_logins(admin_url, passwords)
    return {"result": "PROVISIONED", "database": DATABASE, "schema_version": schema,
            "ledger_id": ledger_id, "audit_seq": seq, "audit_head": event_hash,
            "login_roles": logins, "nologin_roles": nologin, "skipped_roles": skipped,
            "notes": (["CATALYST_PUBLIC_ROLE_ABSENT_SKIPPED"] if PUBLIC_ROLE in skipped else []),
            "broker_requests": 0}


def ledger_identity(conn):
    """The one CLOUD_LEDGER_PROVISIONED payload of a cloud ledger (the trader checks this)."""
    rows = conn.execute(
        """SELECT payload_json FROM lab.trade_events WHERE event_type = 'SYSTEM_EVENT'
        AND payload_json->>'kind' = %s ORDER BY seq""", (LEDGER_EVENT,)).fetchall()
    payloads = [row[0] if not isinstance(row, dict) else row["payload_json"] for row in rows]
    if len(payloads) != 1 or payloads[0].get("role") != "ACCOUNT_LEDGER":
        raise ProvisionError("CLOUD_LEDGER_IDENTITY_REQUIRED")
    return payloads[0]


def rotate_passwords(admin_url, environ, *, now=None):
    """New SCRAM verifiers from the current password variables; nothing else changes."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    with _connect(admin_url, dbname=DATABASE) as conn:
        _quiet(conn)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        if not conn.execute("SELECT to_regnamespace('lab') IS NOT NULL").fetchone()[0]:
            raise ProvisionError("CLOUD_LEDGER_IDENTITY_REQUIRED")
        identity = ledger_identity(conn)
        roles = {role: LOGIN_ROLES[role] for role in identity["login_roles"]}
        passwords = passwords_from_env(environ, roles)
        for role, password in sorted(passwords.items()):
            _set_password(conn, role, password, login=False)
        seq, event_hash = _append(conn, {
            "kind": ROTATION_EVENT, "ledger_id": identity["ledger_id"],
            "roles": sorted(passwords), "rotated_at": now.isoformat(),
            "provisioner": PROVISIONER, **_release()})
    return {"result": "ROTATED", "ledger_id": identity["ledger_id"],
            "roles": _verify_logins(admin_url, passwords), "audit_seq": seq,
            "audit_head": event_hash, "broker_requests": 0}


def _audit_head(conn):
    """``(seq, event_hash, count)`` of the audit chain's head (``None`` twice when empty)."""
    seq, head = conn.execute(
        "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
    ).fetchone() or (None, None)
    return seq, head, conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0]


def _migration_plan(expect_current, target, backup, now):
    """Every check that needs no database: the reference, the versions and the files."""
    if not backup:
        raise ProvisionError("MIGRATION_BACKUP_REFERENCE_REQUIRED")
    try:
        reference = parse_backup_reference(backup)
    except LedgerOpsError:
        raise ProvisionError("MIGRATION_BACKUP_REFERENCE_INVALID") from None
    if type(expect_current) is not int or type(target) is not int or expect_current < 1:
        raise ProvisionError("LEDGER_VERSION_MISMATCH")
    if target <= expect_current:
        raise ProvisionError("LEDGER_TARGET_NOT_AHEAD")
    files = localdb.migration_files()
    if any(version not in files for version in range(expect_current + 1, target + 1)):
        raise ProvisionError("LEDGER_MIGRATION_FILES_MISSING")
    if target != SCHEMA_VERSION:
        # This release runs on its own schema only; a ledger left anywhere else would refuse
        # both this release and the one before it.
        raise ProvisionError("MIGRATION_TARGET_NOT_THIS_RELEASE")
    if reference["schema_version"] != expect_current:
        raise ProvisionError("BACKUP_SCHEMA_MISMATCH")
    if reference["taken_at"] > now + CLOCK_SKEW:
        raise ProvisionError("BACKUP_TAKEN_IN_THE_FUTURE")
    if now - reference["taken_at"] > BACKUP_MAX_AGE:
        raise ProvisionError("BACKUP_TOO_OLD")
    sources = []
    for version in range(expect_current + 1, target + 1):
        raw = files[version].read_bytes()  # Read once: the bytes applied are the bytes hashed.
        sources.append((version, files[version].name, raw.decode("utf-8"),
                        hashlib.sha256(raw).hexdigest()))
    return reference, sources


def _check_backup_grants(conn):
    """The daily backup reads every relation of ``lab``; a migration must keep it that way."""
    unreadable = [row[0] for row in conn.execute(
        """SELECT c.relname::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'lab' AND c.relkind IN ('r', 'p', 'S')
          AND NOT has_table_privilege(%s::name, c.oid, 'SELECT')""", (BACKUP_ROLE,))]
    if unreadable:
        raise ProvisionError("MIGRATION_BACKUP_GRANTS_INCOMPLETE", unreadable)


def migrate(admin_url, *, expect_current, target, backup, now=None):
    """Migrate the provisioned cloud ledger ``expect_current`` -> ``target`` in one audited
    transaction, with a fresh backup named first; see the module docstring."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    reference, sources = _migration_plan(expect_current, target, backup, now)
    with _connect(admin_url, dbname=DATABASE) as conn:
        if not conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]:
            raise ProvisionError("PROVISION_SUPERUSER_REQUIRED")
        _quiet(conn)
        conn.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(
            sql.Literal(MIGRATION_LOCK_TIMEOUT)))
        conn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(
            sql.Literal(MIGRATION_STATEMENT_TIMEOUT)))
        try:
            return _migrate_locked(conn, admin_url, expect_current, target, reference, sources,
                                   now)
        except psycopg.errors.LockNotAvailable:
            raise ProvisionError("MIGRATION_LOCK_TIMEOUT") from None
        except psycopg.errors.QueryCanceled:
            raise ProvisionError("MIGRATION_STATEMENT_TIMEOUT") from None


def _migrate_locked(conn, admin_url, expect_current, target, reference, sources, now):
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
    if not conn.execute("SELECT to_regnamespace('lab') IS NOT NULL").fetchone()[0]:
        raise ProvisionError("CLOUD_LEDGER_IDENTITY_REQUIRED")
    identity = ledger_identity(conn)
    before = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
    if before != expect_current:
        raise ProvisionError("LEDGER_VERSION_MISMATCH")
    # The named backup holds this ledger's own history up to its head: that event is in this
    # chain, unchanged, and not newer than the backup itself.
    backup_row = conn.execute(
        "SELECT event_hash, created_at FROM lab.trade_events WHERE seq = %s",
        (reference["audit_seq"],)).fetchone()
    if backup_row is None or backup_row[0] != reference["audit_head"]:
        raise ProvisionError("BACKUP_HEAD_NOT_IN_LEDGER")
    if backup_row[1] > reference["taken_at"] + CLOCK_SKEW:
        raise ProvisionError("BACKUP_REFERENCE_INCONSISTENT")
    after_backup = conn.execute("SELECT count(*) FROM lab.trade_events WHERE seq > %s",
                                (reference["audit_seq"],)).fetchone()[0]
    head_seq, head_before, count_before = _audit_head(conn)
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
    for _, _, text, _ in sources:
        conn.execute(text)
    conn.execute("RESET ROLE")
    after = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
    if after != target:
        raise ProvisionError("LEDGER_TARGET_NOT_RECORDED")
    _, _, count_after = _audit_head(conn)
    kept = conn.execute("SELECT event_hash FROM lab.trade_events WHERE seq = %s",
                        (head_seq,)).fetchone()
    if count_after < count_before or kept is None or kept[0] != head_before:
        raise ProvisionError("AUDIT_HISTORY_REWRITTEN")
    # Roles a new file created stay NOLOGIN, as provisioning leaves every role the cloud does not
    # log in as; a new cloud login role needs a password, which only provisioning sets.
    added = sorted(lab_roles(target) - lab_roles(expect_current))
    if set(added) & set(LOGIN_ROLES):
        raise ProvisionError("MIGRATION_NEW_LOGIN_ROLE", set(added) & set(LOGIN_ROLES))
    for role in added:
        conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
    _check_login_roles(conn, set(identity["login_roles"]) | lab_roles(target))
    try:
        _check_ownership(conn)
    except RuntimeError:
        raise ProvisionError("PROVISION_OWNERSHIP_UNEXPECTED") from None
    _check_backup_grants(conn)
    named = {"backup": reference["backup"],
             "taken_at": reference["taken_at"].isoformat(),
             "schema_version": reference["schema_version"],
             "audit_seq": reference["audit_seq"], "audit_head": reference["audit_head"],
             "manifest_sha256": reference["manifest_sha256"]}
    migrations = [{"version": version, "file": name, "sha256": digest}
                  for version, name, _, digest in sources]
    seq, event_hash = _append(conn, {
        "kind": MIGRATION_EVENT, "ledger_id": identity["ledger_id"], "database": DATABASE,
        "from_version": before, "to_version": after, "migrations": migrations,
        "backup": named, "events_after_backup": after_backup,
        "audit_seq_before": head_seq, "audit_head_before": head_before,
        "event_count_before": count_before, "nologin_roles_added": added,
        "migrated_at": now.isoformat(), "migrator": MIGRATOR, **_release()})
    conn.commit()
    # A second session sees the committed schema and the event where the append left them.
    with _connect(admin_url, dbname=DATABASE) as check:
        check.execute("SET TRANSACTION READ ONLY")
        version = check.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        recorded = check.execute(
            "SELECT event_hash, payload_json->>'kind' FROM lab.trade_events WHERE seq = %s",
            (seq,)).fetchone()
        count = check.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0]
    if version != target or recorded is None or tuple(recorded) != (event_hash, MIGRATION_EVENT):
        raise ProvisionError("MIGRATION_NOT_VERIFIED")
    return {"result": "MIGRATED", "database": DATABASE, "ledger_id": identity["ledger_id"],
            "before": before, "after": version, "migrations": migrations, "backup": named,
            "events_after_backup": after_backup, "audit_head_before": head_before,
            "event_count_before": count_before, "audit_seq": seq, "audit_head": event_hash,
            "event_count": count, "nologin_roles_added": added, "broker_requests": 0}


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m catalyst_lab.cloud_provision",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("initial", "rotate-passwords", "migrate"))
    parser.add_argument("--expect-current", type=int,
                        help="migrate: the cloud ledger's schema version now")
    parser.add_argument("--target", type=int, help="migrate: this release's schema version")
    parser.add_argument("--backup",
                        help="migrate: the fresh backup's reference (cloud_runtime backup, ops)")
    args = parser.parse_args(argv)
    migrating = args.mode == "migrate"
    options = (args.expect_current, args.target, args.backup)
    if migrating and None in options:
        parser.error("migrate requires --expect-current, --target and --backup")
    if not migrating and any(value is not None for value in options):
        parser.error("--expect-current, --target and --backup are only for migrate")
    prefix = "CLOUD_MIGRATION" if migrating else "CLOUD_PROVISION"
    try:
        admin_url = environ.get("MIGRATION_DATABASE_URL")
        if not admin_url:
            raise ProvisionError("MIGRATION_DATABASE_URL_REQUIRED")
        if args.mode == "initial":
            result = provision(admin_url, environ)
        elif migrating:
            result = migrate(admin_url, expect_current=args.expect_current, target=args.target,
                             backup=args.backup)
        else:
            result = rotate_passwords(admin_url, environ)
    except ProvisionError as exc:
        raise SystemExit(prefix + "_REFUSED: " + exc.code + (
            " " + ",".join(exc.names) if exc.names else "")) from None
    except Exception as exc:
        # Driver messages can carry connection details; only this project's codes are shown.
        code = getattr(getattr(exc, "diag", None), "sqlstate", None)
        raise SystemExit(prefix + "_FAILED" + (
            " SQLSTATE " + code if isinstance(code, str) and code.isalnum() else "")) from None
    print(json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
