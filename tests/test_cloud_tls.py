"""TLS to a Railway-like Postgres and the HOME the entrypoint gives libpq (package cloud).

Every cloud DSN says ``sslmode=require`` and Railway's Postgres serves TLS. libpq 18 (the libpq
in psycopg's wheel, and PostgreSQL 18's pg_dump) takes the home directory from ``HOME``, checks
``$HOME/.postgresql/postgresql.crt`` during a TLS connection and fails it when that path cannot
be searched (libpq 14, this Mac's pg_dump, reads the passwd database instead). The container
starts as root with ``HOME=/root`` (mode 0700): a component that kept that HOME after the drop
to uid 10001 could not open a single database connection. These tests connect over real TLS to
a disposable cluster (127.0.0.1 only; never an owner ledger).
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest

from catalyst_lab import cloud_entry, cloud_provision, ledger_ops
from tests import cloud_fixtures

NOW = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory")


@pytest.fixture(scope="module")
def service():
    try:
        cluster = cloud_fixtures.start(cloud_fixtures.new_root(), tls=True)
    except RuntimeError as exc:  # OPENSSL_REQUIRED
        pytest.skip(str(exc))
    try:
        yield cluster
    finally:
        cloud_fixtures.stop(cluster)


@pytest.fixture(autouse=True)
def absent_home(tmp_path, monkeypatch):
    """No certificate of this Mac's user can take part: HOME is a path that does not exist."""
    monkeypatch.setenv("HOME", str(tmp_path / "home-absent"))


@pytest.fixture
def root_home(tmp_path):
    """What /root is to uid 10001: a directory that exists and cannot be searched."""
    home = tmp_path / "root-home"
    home.mkdir()
    home.chmod(0)
    yield home
    home.chmod(0o700)


def reads_home():
    return psycopg.pq.version() >= 180000  # Verified for 18 (the locked psycopg's libpq).


def test_the_service_accepts_tls_only(service):
    with psycopg.connect(service.admin_url, connect_timeout=5) as conn:
        assert conn.pgconn.ssl_in_use
    plain = service.admin_url.replace("sslmode=require", "sslmode=disable")
    assert plain != service.admin_url
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(plain, connect_timeout=5)


def test_libpq_refuses_tls_while_home_cannot_be_searched(service, root_home, monkeypatch):
    if not reads_home():
        pytest.skip("this libpq does not read HOME")
    monkeypatch.setenv("HOME", str(root_home))
    with pytest.raises(psycopg.OperationalError, match="certificate file"):
        psycopg.connect(service.admin_url, connect_timeout=5)


def run(argv, env):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180)


def test_the_root_entrypoint_gives_the_provisioner_a_home_libpq_accepts(
        service, root_home, tmp_path, monkeypatch):
    passwords = cloud_fixtures.cloud_passwords(cloud_provision.login_roles())
    environ = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root_home),
               "MIGRATION_DATABASE_URL": service.admin_url, **passwords}
    child = [sys.executable, "-m", "catalyst_lab.cloud_provision", "initial"]
    if reads_home():
        # Without the entrypoint: the provisioner keeps root's HOME and cannot connect.
        kept = run(child, environ)
        assert kept.returncode != 0
        assert "CLOUD_PROVISION_REFUSED: PROVISION_DATABASE_UNAVAILABLE" in kept.stderr
    # Through the entrypoint's root branch (the drop itself is recorded, not made): the
    # component runs with the runtime user's home, /nonexistent in the image.
    home = tmp_path / "nonexistent"
    runs = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cloud_entry, "drop_privileges", lambda: runs.append("drop"))
    monkeypatch.setattr(cloud_entry.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    cloud_entry.main(["provision", "initial"], environ=environ,
                     execve=lambda path, argv, env: runs.append((argv, env, run(argv, env))))
    monkeypatch.undo()
    assert runs[0] == "drop"
    argv, env, done = runs[1]
    assert argv == child and env == {**environ, "HOME": str(home)}
    assert done.returncode == 0, done.stderr[-300:]
    result = json.loads(done.stdout)
    assert result["result"] == "PROVISIONED" and "catalyst_backup" in result["login_roles"]
    assert not home.exists()
    # The daily backup over the same TLS, with the same HOME: pg_dump and pg_dumpall carry
    # sslmode=require in their connection string (plain TCP is rejected by this service).
    monkeypatch.setenv("HOME", str(home))
    taken = ledger_ops.backup_database(
        service.url("catalyst_backup", passwords["BACKUP_DATABASE_PASSWORD"]),
        tmp_path / "volume" / "backups", now=NOW)
    assert taken["result"] == "WRITTEN"
    backup = tmp_path / "volume" / "backups" / "20260927T040000Z"
    assert ledger_ops.verify_manifest(
        backup, expected_manifest_sha256=taken["manifest_sha256"])["result"] == "MATCH"
    for role, name in (("catalyst_risk", "RISK_DATABASE_PASSWORD"),
                       ("catalyst_jev", "JEV_DATABASE_PASSWORD"),
                       ("catalyst_app", "APP_DATABASE_PASSWORD"),
                       ("catalyst_operator", "OPERATOR_DATABASE_PASSWORD")):
        with psycopg.connect(service.url(role, passwords[name]), connect_timeout=5) as conn:
            assert conn.pgconn.ssl_in_use
            assert conn.execute("SELECT current_user::text").fetchone()[0] == role
