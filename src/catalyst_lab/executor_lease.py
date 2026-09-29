"""Exclusive executor ownership held for the process lifetime and at every send.

All executors for an account must use its one shared ledger. The legacy observer
lock is also acquired so old and managed executors cannot overlap in that ledger.
Ownership loss is terminal for this instance; reacquisition requires a new process
and startup reconciliation. ``lost_code`` names a loss (never a normal ``close``), so
the managed runtime can end its process and let the supervisor start a new one.
This does not claim broker-side distributed fencing.
"""

import hashlib
import re
import threading
from contextlib import contextmanager
from uuid import uuid4

from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.execution import SubmissionDisabled

LEGACY_EXECUTOR_LOCK = 719172027


class AccountExecutorLease:
    def __init__(self, repository, identity_provider):
        self.repo, self.identity_provider = repository, identity_provider
        self.owner_id = str(uuid4())
        self.connection = None
        self.keys = ()
        self.lost = False
        self.lost_code = None  # Set only when a held lock is found missing, never by close().
        self.mutex = threading.RLock()

    def acquire(self):
        with self.mutex:
            if self.lost:
                raise SubmissionDisabled("EXECUTOR_OWNERSHIP_LOST")
            if self.connection is not None:
                self.assert_owned()
                return
            identity = self.identity_provider()
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise ValueError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
            key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], signed=True)
            conn = self.repo.connect()
            conn.autocommit = True
            try:
                for lock_key in sorted({LEGACY_EXECUTOR_LOCK, key}):
                    if not conn.execute(
                        "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,)
                    ).fetchone()["acquired"]:
                        raise SubmissionDisabled("ACCOUNT_EXECUTOR_ALREADY_RUNNING")
                self.keys = tuple(sorted({LEGACY_EXECUTOR_LOCK, key}))
                self.connection = conn
                self.assert_owned()
            except Exception:
                conn.close()
                self.connection = None
                raise

    def assert_owned(self):
        with self.mutex:
            if self.lost or self.connection is None:
                raise SubmissionDisabled("EXECUTOR_OWNERSHIP_REQUIRED")
            try:
                # Inspect, do not reacquire: a lost lock must never silently resume trading.
                rows = self.connection.execute(
                    """SELECT classid::bigint AS hi,objid::bigint AS lo FROM pg_locks
                    WHERE locktype='advisory' AND pid=pg_backend_pid()
                    AND granted AND objsubid=1"""
                ).fetchall()
                held = {(r["hi"] << 32) | r["lo"] for r in rows}
                if any((key & ((1 << 64) - 1)) not in held for key in self.keys):
                    raise ValueError
            except Exception:
                self.lost = True
                self.lost_code = "EXECUTOR_OWNERSHIP_LOST"
                self.connection.close()
                self.connection = None
                raise SubmissionDisabled("EXECUTOR_OWNERSHIP_LOST") from None

    @contextmanager
    def dispatch_guard(self):
        # Prevent a local shutdown/release between authorization and the HTTP send.
        with self.mutex:
            self.assert_owned()
            yield

    def close(self):
        with self.mutex:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            self.lost = True


class FencedAuthorizationGate(AuthorizationGate):
    """Wrap either existing gate without broadening its exact-request authority."""

    def __init__(self, delegate, lease):
        if not isinstance(delegate, AuthorizationGate):
            raise ValueError("DATABASE_AUTHORIZATION_REQUIRED")
        self.delegate, self.lease = delegate, lease
        self.repo, self.now = delegate.repo, delegate.now

    def dispatch_guard(self):
        return self.lease.dispatch_guard()

    def claim(self, request):
        self.lease.assert_owned()
        decision = self.delegate.claim(request)
        # Authorization may wait for the independent risk transaction lock. Check
        # the session again after that wait and immediately before transport send.
        self.lease.assert_owned()
        return decision
