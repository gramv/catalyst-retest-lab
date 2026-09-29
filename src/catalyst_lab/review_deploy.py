"""Owner-only database bootstrap, separate from the unprivileged Railway web service."""

import os
from pathlib import Path

import psycopg
from psycopg import sql


def provision(database_url, *, review_password, jev_password, operator_password):
    passwords = (review_password, jev_password, operator_password)
    if min(map(len, passwords)) < 32 or len(set(passwords)) != 3:
        raise ValueError("DISTINCT_DATABASE_PASSWORDS_REQUIRED")
    with psycopg.connect(database_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        exists = conn.execute("SELECT to_regclass('lab.schema_migrations')").fetchone()[0]
        if exists is None:
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
        current = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        for migration in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            if int(migration.name.split("_")[0]) > current:
                conn.execute(migration.read_text())
        # DDL cannot parameterize passwords; quote with psycopg's SQL literal, never a shell.
        # The dedicated connection must suppress statement logging while handling credentials.
        conn.execute("SET LOCAL log_statement='none'")
        conn.execute("SET LOCAL log_min_error_statement='panic'")
        conn.execute("SET LOCAL log_min_duration_statement=-1")
        conn.execute("SET LOCAL log_duration=off")
        for role, password in (
            ("catalyst_review", review_password),
            ("catalyst_jev", jev_password),
            ("catalyst_review_operator", operator_password),
        ):
            verifier = conn.pgconn.encrypt_password(
                password.encode(), role.encode(), b"scram-sha-256"
            ).decode()
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(verifier)
                )
            )
        return conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]


def main():
    try:
        version = provision(
            os.environ["MIGRATION_DATABASE_URL"],
            review_password=os.environ["REVIEW_DATABASE_PASSWORD"],
            jev_password=os.environ["JEV_DATABASE_PASSWORD"],
            operator_password=os.environ["REVIEW_OPERATOR_DATABASE_PASSWORD"],
        )
    except Exception:
        # Connection errors can contain passwords/DSNs; never include exception text.
        raise SystemExit(
            "Database provisioning failed; check restricted migration configuration"
        ) from None
    print(f"Review database provisioned at schema {version}; restricted roles retained.")


if __name__ == "__main__":
    main()
