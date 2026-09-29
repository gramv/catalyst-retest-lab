"""A disposable PostgreSQL cluster shaped like Railway's Postgres service (package cloud).

Railway's service has a ``postgres`` superuser with a generated password, a ``railway`` database
and password (SCRAM) authentication for every role. This fixture reproduces that on a private
Unix socket under /tmp (no TCP, like every other test cluster): initdb with ``-U postgres`` and a
password file, ``scram-sha-256`` for every local connection, and ``log_statement = 'all'`` so a
test can prove the provisioner keeps passwords out of the server log. It never touches an owner
ledger and is destroyed after use.

``start(root, tls=True)`` also serves TLS, as Railway's ``postgres-ssl`` image does: libpq never
uses TLS over a Unix socket, so that variant listens on 127.0.0.1 only (a free port), accepts TCP
only as ``hostssl`` with SCRAM (plain TCP is rejected) and its URLs carry ``sslmode=require``
like every cloud DSN. Its certificate is a throwaway self-signed one made with ``openssl``.
"""

import secrets
import shutil
import socket as sockets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from catalyst_lab import localdb

PORT = 55437


def password():
    return secrets.token_urlsafe(48)


@dataclass
class RailwayLikePostgres:
    root: Path
    admin_password: str
    port: int = PORT
    tls: bool = False

    @property
    def socket(self):
        return str((self.root / "socket").resolve())

    @property
    def log(self):
        return self.root / "postgres.log"

    def url(self, role, secret, dbname="catalyst_lab"):
        if self.tls:
            return make_conninfo(host="127.0.0.1", port=self.port, dbname=dbname, user=role,
                                 password=secret, sslmode="require")
        return make_conninfo(host=self.socket, port=self.port, dbname=dbname, user=role,
                             password=secret)

    @property
    def admin_url(self):
        return self.url("postgres", self.admin_password, "railway")

    def admin(self, dbname="railway"):
        return psycopg.connect(self.url("postgres", self.admin_password, dbname),
                               autocommit=True)

    def reset(self):
        """Back to an empty service: no ledger database, no lab roles."""
        with self.admin() as conn:
            conn.execute("DROP DATABASE IF EXISTS catalyst_lab WITH (FORCE)")
            roles = [r[0] for r in conn.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'catalyst%' "
                "OR rolname = 'lab_owner' ORDER BY rolname = 'lab_owner', rolname")]
            for role in roles:
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def free_port():
    with sockets.socket(sockets.AF_INET, sockets.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_tls(data):
    """A throwaway self-signed server certificate, and TCP only as hostssl with SCRAM."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("OPENSSL_REQUIRED")
    subprocess.run([openssl, "req", "-new", "-x509", "-days", "2", "-nodes",
                    "-newkey", "rsa:2048", "-subj", "/CN=localhost",
                    "-keyout", str(data / "server.key"), "-out", str(data / "server.crt")],
                   check=True, capture_output=True)
    (data / "server.key").chmod(0o600)
    hba = data / "pg_hba.conf"
    # First match wins: TLS with SCRAM on 127.0.0.1; initdb's host lines still reject the rest.
    hba.write_text("hostssl all all 127.0.0.1/32 scram-sha-256\n"
                   "hostnossl all all all reject\n" + hba.read_text())


def start(root, *, tls=False):
    root = Path(root)
    (root / "socket").mkdir(mode=0o700)
    secret = password()
    pwfile = root / "pwfile"
    pwfile.write_text(secret + "\n")
    pwfile.chmod(0o600)
    data = root / "postgres"
    subprocess.run([localdb.pg_binary("initdb"), "-D", str(data), "-U", "postgres",
                    "--auth-local=scram-sha-256", "--auth-host=reject", f"--pwfile={pwfile}",
                    "--encoding=UTF8", "--locale=C"],
                   check=True, capture_output=True)
    pwfile.unlink()
    port = free_port() if tls else PORT
    listen = "127.0.0.1" if tls else ""
    if tls:
        _serve_tls(data)
    socket = str((root / "socket").resolve()).replace("'", "''")
    with (data / "postgresql.conf").open("a") as conf:
        conf.write(f"\nlisten_addresses = '{listen}'\nport = {port}\n"
                   f"unix_socket_directories = '{socket}'\nunix_socket_permissions = 0700\n"
                   "password_encryption = 'scram-sha-256'\nlog_statement = 'all'\n"
                   + ("ssl = on\n" if tls else ""))
    subprocess.run([localdb.pg_binary("pg_ctl"), "-D", str(data), "-l",
                    str(root / "postgres.log"), "-w", "start"], check=True, capture_output=True)
    cluster = RailwayLikePostgres(root, secret, port=port, tls=tls)
    with cluster.admin("postgres") as conn:
        conn.execute("CREATE DATABASE railway")
    return cluster


def stop(cluster):
    subprocess.run([localdb.pg_binary("pg_ctl"), "-D", str(cluster.root / "postgres"), "-m",
                    "fast", "-w", "stop"], capture_output=True)
    shutil.rmtree(cluster.root, ignore_errors=True)


def new_root():
    # Short, private and outside every owner directory; the socket path stays short.
    return Path(tempfile.mkdtemp(prefix="cloud-pg-", dir="/tmp"))


def cloud_passwords(roles):
    """One distinct generated password per role variable, as the owner's script creates."""
    return {name: password() for name in roles.values()}
