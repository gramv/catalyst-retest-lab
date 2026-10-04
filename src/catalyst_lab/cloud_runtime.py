"""Railway entrypoints (package cloud, 2026-09-27): the trader, the ops service and the jobs.

    python -m catalyst_lab.cloud_runtime trader
    python -m catalyst_lab.cloud_runtime ops
    python -m catalyst_lab.cloud_runtime jobs        # the nightly cron (package learning-app)
    python -m catalyst_lab.cloud_runtime status      # the owner's checks, inside the ops
    python -m catalyst_lab.cloud_runtime positions   # container (GET-only, status token)
    python -m catalyst_lab.cloud_runtime scorecard [--day YYYY-MM-DD] [--full]   # ops shell,
    python -m catalyst_lab.cloud_runtime market-review [--day YYYY-MM-DD]       # read-only
    python -m catalyst_lab.cloud_runtime weekly-review [--week-end YYYY-MM-DD] [--now]
    python -m catalyst_lab.cloud_runtime clear-protection-latch --reason "..."  # trader shell
    python -m catalyst_lab.cloud_runtime exclude-from-stats --day YYYY-MM-DD --reason CODE  # trader
    python -m catalyst_lab.cloud_runtime promote-strategy --strategy ID --history-report - \
        --owner-ruling "..." [--owner-override-reason "..."] < results.json  # trader shell
    python -m catalyst_lab.cloud_runtime demote-strategy --strategy ID --reason "..." \
        --owner-ruling "..."                                                  # trader shell
    python -m catalyst_lab.cloud_runtime backup    # ops shell, through cloud_entry backup

``catalyst_lab.cloud_entry`` starts these as the non-root runtime user. Both require
``CATALYST_ENVIRONMENT=railway`` and take their configuration from environment variables only
(``catalyst_lab.cloud_config``).

trader  Today's ``app`` component: every managed worker loop plus the API. On start it records
        the start for the crash-loop breaker on the Railway volume, validates the whole
        configuration, and binds its port at once with a small health responder, so Railway's
        deployment healthcheck passes while the trader waits for the database, checks the cloud
        ledger identity (``CLOUD_LEDGER_PROVISIONED``) and waits, for as long as it takes, for
        the account executor lease. ``/health`` never claims trading readiness; readiness stays
        in the token-protected status. Once the lease is held, the same listening socket is
        handed to the app. Exit codes: a lost lease exits 75 (Railway's ON_FAILURE policy
        restarts it); a tripped crash-loop breaker exits 0 on purpose ("stay stopped"); a
        refused configuration exits 2; a startup that keeps failing for ten minutes exits 1.
ops     The watchdog every 30 seconds (the same alarm rules as the Mac watchdog), an audit
        checkpoint every hour and a verified, pruned pg_dump backup every day, all written to
        the Railway volume, with alerts through the existing notify path. Its own checks
        (package ops-alarms): the nightly jobs' scorecard (LEARNING_JOBS_MISSED) and the
        ledger's size (DATABASE_SIZE_HIGH) every five minutes, the ops volume's free space
        (OPS_VOLUME_LOW) every tick. It never holds a broker, Jev or trader credential.
jobs    The nightly learning jobs (``learning_jobs``): shadow outcomes, maintenance replays, the
        market reality, the scorecard and, after each week, the weekly review, each step
        independent and logged with its code as it ends; then it exits 0 when every step
        ended OK, else 1 (a failed or skipped step, an unreachable ledger; a refused
        configuration exits 2). It holds only the trader's ledger connection (catalyst_risk,
        by reference), never a broker or Jev key, and keeps no state. A Railway cron job must
        exit when done and a run still going when the next is due makes Railway skip that one,
        so a watchdog ends the process after ``JOBS_HARD_SECONDS`` whatever it is doing, with
        exit 1. Its restart policy is NEVER: a failed run waits for the next night.
"""

import argparse
import http.server
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from catalyst_lab import cloud_config
from catalyst_lab.cloud_config import CloudConfigError
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.managed_ops import (
    LIMIT,
    REFUSAL_CODE,
    _private_directory,
    atomic_private_json,
    crash_loop_guard,
    ledger_alarms,
    ops_volume_alarms,
    private_bytes,
    scorecard_due_day,
    status_alarms,
)

CRASH_LOOP_ANCHOR = "cloud-launch"  # launcher-state/ sits beside it, on the volume.
EXIT_TRIPPED = 0  # Railway leaves an exit-0 deployment "Completed": the breaker's stop.
EXIT_REFUSED = 2
EXIT_STARTUP_FAILED = 1
STARTUP_RETRY_SECONDS = 600
LEASE_LOG_SECONDS = 60
HEALTH_STARTING = {"status": "ok", "alpaca": "paper", "surface": "PRIVATE_ENGINEERING_LAB",
                   "supervised": True, "phase": "STARTING"}
# The Mac watchdog's bounds (managed_ops.config_template), unchanged; Muse is not deployed.
WATCHDOG = {"interval_seconds": 30, "timeout_seconds": 3, "tick_max_age_seconds": 15,
            "reconciliation_max_age_seconds": 90, "research_max_age_seconds": 180}
AUDIT_EVERY = timedelta(hours=1)
BACKUP_EVERY = timedelta(days=1)
AUDIT_RETRY = timedelta(minutes=5)
BACKUP_RETRY = timedelta(minutes=30)
HEARTBEAT_EVERY = timedelta(minutes=10)
# The ledger check (the nightly scorecard and the database size, package ops-alarms): one
# read-only connection every five minutes, and at once when the due day changes.
LEDGER_CHECK_EVERY = timedelta(minutes=5)


def log(component, event, **fields):
    """One code-only line to the Railway log: never a value, URL, DSN or driver text."""
    details = " ".join(f"{k}={v}" for k, v in sorted(fields.items()) if v is not None)
    print(f"{datetime.now(UTC).isoformat()} {component.upper()} {event} {details}".rstrip(),
          flush=True)


def failure_code(exc):
    text = exc.message() if isinstance(exc, CloudConfigError) else str(exc)
    first = text.split(" ", 1)[0]
    if isinstance(exc, CloudConfigError) or REFUSAL_CODE.fullmatch(first):
        return text if isinstance(exc, CloudConfigError) else first
    return type(exc).__name__.upper()


def state_directory(environ):
    """The state directory for the breaker, before the full validation (which may fail)."""
    raw = environ.get(cloud_config.STATE_DIR, "")
    return Path(os.path.normpath(raw)) if raw and Path(raw).is_absolute() else None


def breaker(environ, component, now):
    """The Mac launcher's crash-loop breaker, with its state on the Railway volume."""
    state = state_directory(environ)
    try:
        if state is None:
            raise ValueError
        _private_directory(state)  # Owner-only, as cloud_entry left it on the volume.
    except (OSError, ValueError):
        return {"tripped": True, "alarms": ["CRASH_LOOP_STATE_UNAVAILABLE"]}
    return crash_loop_guard(state / CRASH_LOOP_ANCHOR, component, now=now)


# --- trader -------------------------------------------------------------------------------


def listening_socket(host, port):
    """Bound and listening. ``::`` is dual stack (IPv4-mapped too); loopback stays IPv4."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


class _StartingHandler(http.server.BaseHTTPRequestHandler):
    server_version = "catalyst"
    sys_version = ""

    def _reply(self, code, body):
        raw = json.dumps(body, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 (http.server naming)
        if self.path.split("?", 1)[0] == "/health":
            self._reply(200, HEALTH_STARTING)
        else:
            self._reply(503, {"detail": "TRADER_STARTING"})

    do_HEAD = do_GET

    def do_POST(self):  # noqa: N802
        self._reply(503, {"detail": "TRADER_STARTING"})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def log_message(self, format, *args):  # No access log, as the app itself.
        pass


class StartingHealth:
    """Answers on the trader's socket until the app takes it over; the socket stays open."""

    def __init__(self, listener):
        self.listener = listener
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StartingHandler,
                                                      bind_and_activate=False)
        self.server.socket.close()
        self.server.socket = listener
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="starting-health",
                                       kwargs={"poll_interval": 0.2}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=10)


def verify_cloud_ledger(database_url):
    """The ledger must be a provisioned cloud account ledger (CLOUD_LEDGER_PROVISIONED)."""
    import psycopg

    from catalyst_lab.cloud_provision import ledger_identity

    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return ledger_identity(conn)


def close_unserved(app):
    """Close what an app that will never serve opened (its lifespan would have done it)."""
    runtime = getattr(getattr(app, "state", None), "managed_runtime", None)
    context = getattr(getattr(app, "state", None), "research_context", None)
    resources = [*getattr(runtime, "owned_resources", ()), getattr(runtime, "source", None),
                 getattr(getattr(runtime, "execution", None), "broker", None), context]
    seen = set()
    for resource in resources:
        if resource is not None and id(resource) not in seen and hasattr(resource, "close"):
            seen.add(id(resource))
            try:
                resource.close()
            except Exception:
                pass


def wait_for_lease(runtime, *, sleep, monotonic, component="trader"):
    """Retry the account executor lease until it is free; never trade without it.

    A second trader (an overlapping deployment, a stale session) keeps the lease held: this
    one waits, answering /health, and takes over only when the other is gone.
    """
    from catalyst_lab.execution import SubmissionDisabled

    delay, attempts, logged = 1.0, 0, None
    while True:
        try:
            runtime.acquire_ownership()
            return attempts
        except SubmissionDisabled as exc:
            if str(exc) != "ACCOUNT_EXECUTOR_ALREADY_RUNNING":
                raise
        attempts += 1
        if logged is None or monotonic() - logged >= LEASE_LOG_SECONDS:
            log(component, "EXECUTOR_LEASE_WAITING", code="ACCOUNT_EXECUTOR_ALREADY_RUNNING",
                attempts=attempts)
            logged = monotonic()
        sleep(delay)
        delay = min(delay * 2, 30.0)


def start_trader_app(config, *, builder, wait_database, check_ledger, sleep, monotonic):
    """Database, ledger identity, app construction and the lease; failures retried for ten
    minutes from the first attempt.

    The lease wait itself has no limit: while another executor holds the lease, ``wait_for_lease``
    keeps waiting and nothing ends it early. Its time still counts toward the ten minutes,
    because the budget is measured from the first attempt: a failure other than a held lease
    after a long wait (the database dropping, say) ends startup at once (exit 1), and Railway's
    ON_FAILURE policy restarts the trader with a fresh budget.
    """
    started, delay = monotonic(), 1.0
    while True:
        app = None
        try:
            wait_database(config.database_url, max_seconds=config.database_wait_seconds)
            identity = check_ledger(config.database_url)
            app, settings = builder()
            runtime = app.state.managed_runtime
            waited = wait_for_lease(runtime, sleep=sleep, monotonic=monotonic)
            return app, settings, {"ledger_id": identity.get("ledger_id"),
                                   "lease_wait_attempts": waited}
        except Exception as exc:
            if app is not None:
                close_unserved(app)
            code = failure_code(exc)
            if monotonic() - started >= STARTUP_RETRY_SECONDS:
                raise RuntimeError(code) from None
            log("trader", "STARTUP_RETRY", code=code, retry_in_seconds=int(delay))
            sleep(delay)
            delay = min(delay * 2, 60.0)


def trader(environ=None, *, clock=time.time, sleep=time.sleep, monotonic=time.monotonic,
           listen=listening_socket, builder=None, wait_database=None, check_ledger=None,
           serve=None, verify_release=None):
    """The Railway trader; returns an exit status (``serve`` raises SystemExit itself)."""
    from catalyst_lab import managed_app, managed_ops
    from catalyst_lab.cloud_release import verify_image_release

    environ = os.environ if environ is None else environ
    tripped = breaker(environ, "trader", clock())
    if tripped["tripped"]:
        log("trader", "CRASH_LOOP_BREAKER_STOP", alarms=",".join(tripped["alarms"]))
        return EXIT_TRIPPED
    try:
        config = cloud_config.trader_config(environ)
        release = (verify_release or verify_image_release)()
    except (CloudConfigError, ValueError) as exc:
        log("trader", "REFUSED", code=failure_code(exc))
        return EXIT_REFUSED
    try:
        listener = listen(config.runtime.bind_host, config.port)
    except OSError:
        log("trader", "REFUSED", code="CLOUD_PORT_UNAVAILABLE", port=config.port)
        return EXIT_STARTUP_FAILED
    health = StartingHealth(listener).start()
    log("trader", "STARTING", release_commit=release["release_commit"],
        bind=config.runtime.bind_host, port=config.port)
    try:
        # The public domain's /health answers from memory (managed_service.create_managed_app).
        default_builder = partial(managed_app.build_app_from_env, health_check_database=False)
        app, settings, facts = start_trader_app(
            config, builder=builder or default_builder,
            wait_database=wait_database or managed_ops.wait_for_database,
            check_ledger=check_ledger or verify_cloud_ledger, sleep=sleep, monotonic=monotonic)
    except BaseException as exc:
        health.stop()
        listener.close()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            log("trader", "STOPPED_BEFORE_SERVING")
            return 0
        log("trader", "STARTUP_FAILED", code=failure_code(exc))
        return EXIT_STARTUP_FAILED
    health.stop()  # The listening socket stays open: queued connections reach the app.
    log("trader", "SERVING", ledger_id=facts["ledger_id"],
        lease_wait_attempts=facts["lease_wait_attempts"])
    (serve or managed_app.serve)(app, settings, host=config.runtime.bind_host,
                                 sockets=[listener])
    return 0


# --- ops ----------------------------------------------------------------------------------


class OpsLoop:
    """One watchdog tick at a time; audit checkpoints and backups when they are due."""

    def __init__(self, config, release, *, transport=None, audit_repository=None,
                 backup=None, disk_usage=shutil.disk_usage):
        state = _private_directory(config.runtime.state_dir)
        self.config, self.release, self.transport = config, release, transport
        self.alarm_file = state / "alarms.json"
        self.schedule_file = state / "ops-schedule.json"
        self.checkpoints = state / "audit-checkpoints"
        self.backups = state / "backups"
        self.audit_repository = audit_repository
        self.backup = backup
        self.disk_usage = disk_usage
        self.ledger = None  # The latest ledger check (``check_ledger``).
        self.last_alarms, self.last_logged = None, None

    def _repository(self):
        from catalyst_lab.managed_ops import readonly_audit_repository

        return self.audit_repository or readonly_audit_repository(
            self.config.audit_database_url)

    def read_status(self):
        return self.read("/api/v1/lab/status")

    def read(self, route):
        """GET-only, bounded, status token; any failure is simply 'nothing read'."""
        try:
            timeout = WATCHDOG["timeout_seconds"]
            deadline = time.monotonic() + timeout
            with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                with client.stream("GET", self.config.status_api + route,
                                   headers={"Authorization":
                                            "Bearer " + self.config.status_token}) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > LIMIT or time.monotonic() > deadline:
                            raise ValueError
            status = strict_json(bytes(body))
            return status if isinstance(status, dict) else None
        except Exception:
            return None  # Deliberately discard URL, token and exception details.

    def _schedule(self):
        try:
            value = strict_json(private_bytes(self.schedule_file))
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}  # Missing or damaged: everything is due, which is always safe.

    @staticmethod
    def _due(schedule, key, now, every):
        def at(name):
            try:
                value = datetime.fromisoformat(schedule[name])
                return value if value.tzinfo is not None else None
            except (KeyError, TypeError, ValueError):
                return None

        retry = at(key + "_retry_at")
        if retry is not None and now < retry:
            return False
        last = at(key + "_at")
        return last is None or not timedelta(0) <= now - last < every

    def run_audit(self, now):
        from catalyst_lab.managed_ops import export_checkpoint

        repository = self._repository()
        repository.check_role()
        result = export_checkpoint(repository, self.checkpoints, now=now)
        return {"result": "WRITTEN" if result["written"] else "NO_NEW_EVENTS",
                "mode": result["mode"], "event_count": result["event_count"],
                "head_hash": result["head_hash"]}

    def run_backup(self, now):
        from catalyst_lab import ledger_ops

        taken = (self.backup or ledger_ops.backup_database)(
            self.config.backup_database_url, self.backups, now=now)
        verified = ledger_ops.verify_manifest(
            taken["backup"], expected_manifest_sha256=taken["manifest_sha256"])
        pruned = ledger_ops.prune(self.backups, self.config.backup_retention_days, now=now)
        return {"result": verified["result"], "backup": Path(taken["backup"]).name,
                "manifest_sha256": taken["manifest_sha256"], "audit_head": taken["audit_head"],
                "event_count": taken["event_count"], "deleted": pruned["deleted"]}

    def _scheduled(self, schedule, key, now, every, retry, job, alarm):
        if not self._due(schedule, key, now, every):
            return None
        try:
            outcome = job(now)
        except Exception as exc:
            schedule[key + "_retry_at"] = (now + retry).isoformat()
            schedule[key + "_failed"] = failure_code(exc)
            log("ops", alarm, code=schedule[key + "_failed"])
            return {"result": "FAILED", "code": schedule[key + "_failed"]}
        schedule.update({key + "_at": now.isoformat(), key + "_retry_at": None,
                         key + "_failed": None})
        return outcome

    def _audit_head(self):
        try:
            with self._repository().connect() as conn:
                row = conn.execute(
                    "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
            return (row["seq"], row["event_hash"]) if row is not None else None
        except Exception:
            return None  # Best effort: the dead-man ping must still go out.

    def check_ledger(self, now):
        """The due day's ``DAILY_SCORECARD_V1`` (the jobs' key, ``scorecard_key``) and the
        ledger's ``pg_database_size``, over the read-only catalyst_app connection (the size
        needs only CONNECT on the database, which its login holds). A read that fails leaves
        its fact out, with the failure's code: ``ledger_alarms`` then fails closed."""
        from catalyst_lab.scorecard import scorecard_key

        day = scorecard_due_day(now)
        facts = {"checked_at": now.isoformat(), "scorecard_day": day.isoformat()}
        try:
            with self._repository().connect() as conn:
                row = conn.execute(
                    "SELECT event_seq FROM lab.managed_events WHERE idempotency_key=%s",
                    (scorecard_key(day),)).fetchone()
                facts["scorecard_recorded"] = row is not None
                facts["database_bytes"] = conn.execute(
                    "SELECT pg_database_size(current_database()) AS size").fetchone()["size"]
        except Exception as exc:
            facts["code"] = failure_code(exc)
        return facts

    def _ledger_check(self, now):
        """The latest ledger check, repeated once it is ``LEDGER_CHECK_EVERY`` old or the due
        day has changed (a failed one too: an unreachable ledger costs one connect timeout)."""
        try:
            age = now - datetime.fromisoformat(self.ledger["checked_at"])
            current = (self.ledger["scorecard_day"] == scorecard_due_day(now).isoformat()
                       and timedelta(0) <= age < LEDGER_CHECK_EVERY)
        except (KeyError, TypeError, ValueError):
            current = False
        if not current:
            self.ledger = self.check_ledger(now)
        return self.ledger

    def ops_volume(self):
        """The ops volume's usage, by ``shutil.disk_usage`` of its mount path (the state
        directory, which the configuration requires on the volume)."""
        try:
            usage = self.disk_usage(self.config.runtime.state_dir)
            return {"total_bytes": usage.total, "free_bytes": usage.free}
        except Exception as exc:
            return {"code": failure_code(exc)}

    def tick(self, now=None):
        from catalyst_lab import notify

        now = now or datetime.now(UTC)
        status = self.read_status()
        alarms = status_alarms(status, now, WATCHDOG)
        if isinstance(status, dict) and status.get("code_version") != self.release[
                "source_sha256"]:
            alarms.append("RELEASE_CODE_MISMATCH")  # trader and ops must run one release.
        schedule = self._schedule()
        audit = self._scheduled(schedule, "audit", now, AUDIT_EVERY, AUDIT_RETRY,
                                self.run_audit, "AUDIT_CHECKPOINT_FAILED")
        backup = self._scheduled(schedule, "backup", now, BACKUP_EVERY, BACKUP_RETRY,
                                 self.run_backup, "BACKUP_FAILED")
        if schedule.get("audit_failed"):
            alarms.append("AUDIT_CHECKPOINT_FAILED")
        if schedule.get("backup_failed"):
            alarms.append("BACKUP_FAILED")
        ledger, volume = self._ledger_check(now), self.ops_volume()
        alarms += ledger_alarms(ledger) + ops_volume_alarms(volume)
        atomic_private_json(self.schedule_file, schedule)
        section = self.config.notify
        if section:
            head = (self._audit_head()
                    if notify.due_for_daily_head(section, self.alarm_file, now) else None)
            extra, _ = notify.watchdog_tick(
                section, alarms=sorted(set(alarms)), now=now, alarm_file=self.alarm_file,
                audit_head=head, release_commit=self.release["release_commit"],
                transport=self.transport, ping_url=self.config.notify_ping_url)
            alarms += extra
        result = {"checked_at": now.isoformat(), "alarms": sorted(set(alarms)),
                  "mode": "PAPER_ONLY", "scope": "CLOUD_OPS_WATCHDOG",
                  "release_commit": self.release["release_commit"],
                  "audit": audit, "backup": backup, "ledger": ledger, "ops_volume": volume}
        atomic_private_json(self.alarm_file, result)
        if (result["alarms"] != self.last_alarms or audit or backup or self.last_logged is None
                or now - self.last_logged >= HEARTBEAT_EVERY):
            log("ops", "TICK", alarms=",".join(result["alarms"]) or "NONE",
                audit=(audit or {}).get("result"), backup=(backup or {}).get("result"))
            self.last_alarms, self.last_logged = result["alarms"], now
        return result


def ops(environ=None, *, clock=time.time, stop=None, transport=None, verify_release=None,
        loop_factory=OpsLoop, max_ticks=None):
    """The Railway ops service; returns an exit status."""
    from catalyst_lab.cloud_release import verify_image_release

    environ = os.environ if environ is None else environ
    tripped = breaker(environ, "ops", clock())
    if tripped["tripped"]:
        log("ops", "CRASH_LOOP_BREAKER_STOP", alarms=",".join(tripped["alarms"]))
        return EXIT_TRIPPED
    try:
        config = cloud_config.ops_config(environ)
        release = (verify_release or verify_image_release)()
    except (CloudConfigError, ValueError) as exc:
        log("ops", "REFUSED", code=failure_code(exc))
        return EXIT_REFUSED
    stop = stop or threading.Event()
    loop = loop_factory(config, release, transport=transport)
    log("ops", "STARTING", release_commit=release["release_commit"],
        notify="CONFIGURED" if config.notify else "NOT_CONFIGURED")
    ticks = 0
    while not stop.is_set():
        loop.tick()
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        stop.wait(WATCHDOG["interval_seconds"])
    log("ops", "STOPPED", ticks=ticks)
    return 0


# --- jobs (package learning-app) ------------------------------------------------------------

JOBS_SOFT_SECONDS = 20 * 60  # A step not started by then is skipped (JOB_TIME_LIMIT).
JOBS_HARD_SECONDS = 30 * 60  # The process ends here whatever it is doing.
JOBS_DATABASE_WAIT_SECONDS = 120
# A step FAILED or SKIPPED (an unreachable ledger fails every step) or the hard stop: Railway
# shows a failed run, and its restart policy NEVER keeps it from repeating (package ops-alarms).
EXIT_JOBS_FAILED = 1


def jobs_store(database_url):
    """The managed store over the trader's catalyst_risk connection, once the ledger answers."""
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.managed_ops import wait_for_database
    from catalyst_lab.managed_store import ManagedStore

    wait_for_database(database_url, max_seconds=JOBS_DATABASE_WAIT_SECONDS)
    return ManagedStore(RiskRepository(database_url))


def _step_fields(details):
    """A step's details as short ``key=value`` log fields (counts and codes only)."""
    return {str(key).replace("-", "").replace(":", "")[:24]: value
            for key, value in (details or {}).items()
            if isinstance(value, (int, str)) and not isinstance(value, bool)}


def jobs(environ=None, *, now=None, verify_release=None, store_factory=None,
         reader_factory=None, monotonic=time.monotonic, hard_exit=os._exit,
         hard_seconds=JOBS_HARD_SECONDS):
    """The Railway cron ``jobs``: every learning step once, then exit 0 when every step ended
    OK, else ``EXIT_JOBS_FAILED`` (see the module)."""
    from catalyst_lab.cloud_release import verify_image_release
    from catalyst_lab.learning_jobs import STEPS, StepResult, run_jobs
    from catalyst_lab.public_crypto_bars import PublicCryptoBarReader

    environ = os.environ if environ is None else environ
    try:
        config = cloud_config.jobs_config(environ)
        release = (verify_release or verify_image_release)()
    except (CloudConfigError, ValueError) as exc:
        log("jobs", "REFUSED", code=failure_code(exc))
        return EXIT_REFUSED

    def expire():
        log("jobs", "TIME_LIMIT_EXIT", seconds=hard_seconds)
        hard_exit(EXIT_JOBS_FAILED)

    def logged(result):  # Each step's line as it ends: the hard stop keeps the finished ones.
        log("jobs", "STEP", step=result.name, result=result.result, code=result.code,
            **_step_fields(result.details))

    watchdog = threading.Timer(hard_seconds, expire)
    watchdog.daemon = True
    watchdog.start()
    log("jobs", "STARTING", release_commit=release["release_commit"])
    started = monotonic()
    try:
        try:
            store = (store_factory or jobs_store)(config.database_url)
        except Exception as exc:
            code = failure_code(exc)
            results = [StepResult(name, "FAILED", code) for name in STEPS]
            for result in results:
                logged(result)
        else:
            reader = (reader_factory or PublicCryptoBarReader)()
            try:
                results = run_jobs(store, reader, now=now or datetime.now(UTC),
                                   deadline=started + JOBS_SOFT_SECONDS, monotonic=monotonic,
                                   on_result=logged)
            finally:
                reader.close()
    finally:
        watchdog.cancel()
    failed = sum(r.result != "OK" for r in results)
    log("jobs", "DONE", failed=failed, seconds=int(monotonic() - started))
    return EXIT_JOBS_FAILED if failed else 0


# --- the owner's learning commands (ops shell, read-only) ---------------------------------------


def _learning_ledger(environ, out):
    """The ops service's ``AUDIT_DATABASE_URL`` (catalyst_app: SELECT on the lab tables, no
    insert on any managed table), opened with READ ONLY transactions."""
    from catalyst_lab.managed_ops import readonly_audit_repository

    try:
        url = cloud_config.database_url(environ, "AUDIT_DATABASE_URL",
                                        on_railway=bool(environ.get("RAILWAY_ENVIRONMENT_ID")))
    except CloudConfigError as exc:
        print("CLOUD_LEARNING_REFUSED: " + exc.message(), file=out)
        return None
    return readonly_audit_repository(url)


def _yesterday(now):
    from catalyst_lab.market import NY

    return now.astimezone(NY).date() - timedelta(days=1)


def _print_learning(out, text, data):
    print(text, file=out)
    print(json.dumps(data, indent=1, sort_keys=True), file=out)


def scorecard(day=None, environ=None, *, out=None, repository=None, now=None, full=False):
    """The owner's scorecard (``railway ssh --service ops -- python -m
    catalyst_lab.cloud_runtime scorecard [--day YYYY-MM-DD] [--full]``): the recorded
    ``DAILY_SCORECARD_V1`` of the day (default: yesterday in New York), else a read-only
    preview computed now and marked ``recorded: false``. A plain summary, then JSON."""
    from catalyst_lab.learning_summary import scorecard_summary
    from catalyst_lab.scorecard import compute_scorecard, recorded_scorecard

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    repo = repository or _learning_ledger(environ, out)
    if repo is None:
        return EXIT_REFUSED
    now = now or datetime.now(UTC)
    day = day or _yesterday(now)
    try:
        row = recorded_scorecard(repo, day)
        body = row["body"] if row else compute_scorecard(repo, day, now=now)
    except Exception as exc:
        print("CLOUD_LEARNING_UNAVAILABLE: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    text, data = scorecard_summary(body, recorded=row is not None,
                                   event_seq=row["event_seq"] if row else None)
    _print_learning(out, text, body if full else data)
    return 0


def market_review(day=None, environ=None, *, out=None, repository=None, now=None,
                  reader_factory=None):
    """The owner's market review (``... cloud_runtime market-review [--day YYYY-MM-DD]``): the
    recorded ``MARKET_REALITY_V1`` of the day (default: yesterday), else a read-only preview
    from Alpaca's public bars (no key), marked ``recorded: false``."""
    from catalyst_lab.learning_summary import reality_summary
    from catalyst_lab.market_reality import measure_day, recorded_reality
    from catalyst_lab.public_crypto_bars import PublicCryptoBarReader

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    repo = repository or _learning_ledger(environ, out)
    if repo is None:
        return EXIT_REFUSED
    now = now or datetime.now(UTC)
    day = day or _yesterday(now)
    try:
        row = recorded_reality(repo, day)
        if row is not None:
            body = row["body"]
        else:
            reader = (reader_factory or PublicCryptoBarReader)()
            try:
                body = measure_day(repo, reader, day, now=now)
            finally:
                reader.close()
    except Exception as exc:
        print("CLOUD_LEARNING_UNAVAILABLE: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    text, data = reality_summary(body, recorded=row is not None,
                                 event_seq=row["event_seq"] if row else None)
    _print_learning(out, text, data)
    return 0


def weekly_review(week_end=None, environ=None, *, out=None, repository=None, now=None,
                  preview=False):
    """The owner's weekly review (``... cloud_runtime weekly-review [--week-end YYYY-MM-DD]
    [--now]``): the recorded ``WEEKLY_REVIEW_V1`` (the latest, or the week ending that Sunday);
    ``--now`` (or no recorded review) computes the latest finished week read-only, marked
    ``recorded: false``. Verdicts, intervals and any proposal drafts."""
    from catalyst_lab.learning_summary import review_summary
    from catalyst_lab.weekly_review import (
        compute_review,
        last_completed_week_end,
        recorded_review,
    )

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    repo = repository or _learning_ledger(environ, out)
    if repo is None:
        return EXIT_REFUSED
    now = now or datetime.now(UTC)
    try:
        row = None if preview else recorded_review(repo, week_end)
        body = row["body"] if row else compute_review(
            repo, week_end or last_completed_week_end(now), now=now)
    except Exception as exc:
        print("CLOUD_LEARNING_UNAVAILABLE: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    text, data = review_summary(body, recorded=row is not None,
                                event_seq=row["event_seq"] if row else None)
    _print_learning(out, text, data)
    return 0


def daily_brief(day=None, environ=None, *, out=None, repository=None, now=None,
                reader_factory=None):
    """The owner's daily brief (``... cloud_runtime daily-brief [--day YYYY-MM-DD]``,
    package learning-loop2): the recorded ``DAILY_BRIEF_V1`` of the day (default: yesterday),
    else a read-only preview from the recorded reality and Alpaca's public bars (no key),
    marked ``recorded: false``. The plain lines, then JSON."""
    from catalyst_lab.daily_brief import compute_brief, recorded_brief
    from catalyst_lab.public_crypto_bars import PublicCryptoBarReader

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    repo = repository or _learning_ledger(environ, out)
    if repo is None:
        return EXIT_REFUSED
    now = now or datetime.now(UTC)
    day = day or _yesterday(now)
    try:
        row = recorded_brief(repo, day)
        if row is not None:
            body = row["body"]
        else:
            reader = (reader_factory or PublicCryptoBarReader)()
            try:
                body = compute_brief(repo, reader, day, now=now)
            finally:
                reader.close()
    except Exception as exc:
        print("CLOUD_LEARNING_UNAVAILABLE: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    head = "recorded" if row is not None else "preview, not recorded"
    text = "\n".join([f"({head})", *body["text"]])
    _print_learning(out, text, {"recorded": row is not None,
                                "event_seq": row["event_seq"] if row else None,
                                "research_focus": body["research_focus"],
                                "missed": body["missed"]})
    return 0


STATUS_KEYS = ("worker_state", "executor_ownership", "entry_ready", "last_reconciliation_at",
               "trade_stream_connected", "market_streams", "management_review_enabled",
               "error_code", "schema_version", "release_commit", "execution_halts",
               "operator_flatten", "jev_breaker", "trade_maintenance", "day_reviews",
               "jev_budget")
# The owner's view of each open position while Jev maintains it: its stop and target, and
# ``stop_replace`` while a raised stop is still being replaced at the broker.
POSITION_KEYS = ("setup_id", "symbol", "open_qty", "quantity_source", "opened_at")
POSITION_STATE_KEYS = ("state", "arm", "stop", "target", "stop_replace", "protection_state",
                       "maintenance_policy", "day_review_at", "hard_exit_at",
                       "holding_window_seconds", "exit_requested", "exit_reason", "last_error")


def _owner_reader(environ, transport, out):
    """The ops service's status token and trader URL, for the owner's GET-only commands."""
    try:
        api = cloud_config.status_api(environ.get("MANAGED_STATUS_API", ""),
                                      on_railway=bool(environ.get("RAILWAY_ENVIRONMENT_ID")))
        token = environ["MANAGED_STATUS_TOKEN"]
    except (CloudConfigError, KeyError):
        print("CLOUD_STATUS_REFUSED: MANAGED_STATUS_API and MANAGED_STATUS_TOKEN required",
              file=out)
        return None
    config = type("StatusOnly", (), {"status_api": api, "status_token": token})()
    loop = OpsLoop.__new__(OpsLoop)
    loop.config, loop.transport = config, transport
    return loop


def status(environ=None, *, transport=None, out=None):
    """The owner's status check from inside the ops container (``railway ssh --service ops --
    python -m catalyst_lab.cloud_runtime status``): the supervised-day fields, GET-only, with
    the ops service's own status token, which is never printed. ``jev_budget`` (package
    jev-budget) shows the month's Jev spend, the projections, the budget and the tier."""
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    loop = _owner_reader(environ, transport, out)
    if loop is None:
        return EXIT_REFUSED
    body = loop.read_status()
    if body is None:
        print("CLOUD_STATUS_UNAVAILABLE", file=out)
        return EXIT_STARTUP_FAILED
    print(json.dumps({key: body.get(key) for key in STATUS_KEYS}, indent=1, sort_keys=True),
          file=out)
    return 0


def positions(environ=None, *, transport=None, out=None):
    """The open positions as the trader holds them (``railway ssh --service ops -- python -m
    catalyst_lab.cloud_runtime positions``): stop, target, a raised stop still being replaced
    at the broker (``stop_replace``), the maintenance version and the next 24-hour review.
    GET-only, with the ops service's status token, which is never printed."""
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    loop = _owner_reader(environ, transport, out)
    if loop is None:
        return EXIT_REFUSED
    body = loop.read("/api/v1/lab/positions?limit=50")
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        print("CLOUD_STATUS_UNAVAILABLE", file=out)
        return EXIT_STARTUP_FAILED
    shown = []
    for item in items:
        if not isinstance(item, dict):
            continue
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        shown.append({**{key: item.get(key) for key in POSITION_KEYS},
                      "state": {key: state[key] for key in POSITION_STATE_KEYS if key in state}})
    print(json.dumps({"open_positions": shown}, indent=1, sort_keys=True), file=out)
    return 0


def clear_protection_latch(reason, environ=None, *, out=None, repository=None):
    """The owner's clear of an operator-only protection latch, in the trader's shell
    (``railway ssh --service trader -- python -m catalyst_lab.cloud_runtime
    clear-protection-latch --reason "..."``), over the trader's own ``MANAGED_DATABASE_URL``.

    Exactly ``managed_ops clear-protection-latch`` (package cloud-hardening): a reason of 10 to
    500 characters, one audited ``OPERATOR_PROTECTION_LATCH_CLEARED`` event (``operator``:
    ``RAILWAY_TRADER_SHELL``), idempotent for a retried command, nothing else changed. The
    running trader releases the operator-only latches recorded before it; a durable execution
    halt still needs ``operator release-halt`` on ops. No broker call; the DSN, the password
    and driver text are never printed.
    """
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.managed_ops import clear_protection_latch as clear

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    try:
        url = cloud_config.database_url(environ, "MANAGED_DATABASE_URL",
                                        on_railway=bool(environ.get("RAILWAY_ENVIRONMENT_ID")))
    except CloudConfigError as exc:
        print("CLOUD_LATCH_CLEAR_REFUSED: " + exc.message(), file=out)
        return EXIT_REFUSED
    try:
        repo = repository or RiskRepository(url)
        repo.check_role()
        result = clear(None, reason, repository=repo, operator="RAILWAY_TRADER_SHELL")
    except ValueError as exc:
        code = str(exc)
        print("CLOUD_LATCH_CLEAR_REFUSED: " + (code if REFUSAL_CODE.fullmatch(code)
                                               else "INVALID"), file=out)
        return EXIT_REFUSED
    except Exception as exc:
        print("CLOUD_LATCH_CLEAR_FAILED: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    print(json.dumps({"action": "clear-protection-latch", "mode": "PAPER_ONLY", **result},
                     sort_keys=True), file=out)
    return 0


def exclude_from_stats(day, reason, environ=None, *, out=None, repository=None, now=None):
    """The owner's ``STATS_EXCLUSION_V1`` record (package public-page-v3), in the trader's
    shell (``railway ssh --service trader -- python -m catalyst_lab.cloud_runtime
    exclude-from-stats --day 2026-10-02 --reason UNMONITORED_OPERATION_2026-10-02``), over the
    trader's own ``MANAGED_DATABASE_URL`` (the role that appends managed events; the ops
    service's ledger login is read-only).

    One audited ``STATS_EXCLUSION`` event per day, idempotent; no trade, fill, fee or event
    changes. Refused for a day without trades, a day not over and a day with an open trade. No
    broker call; the DSN, the password and driver text are never printed."""
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.managed_store import ManagedStore
    from catalyst_lab.stats_exclusion import record_exclusion

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    try:
        url = cloud_config.database_url(environ, "MANAGED_DATABASE_URL",
                                        on_railway=bool(environ.get("RAILWAY_ENVIRONMENT_ID")))
    except CloudConfigError as exc:
        print("CLOUD_STATS_EXCLUSION_REFUSED: " + exc.message(), file=out)
        return EXIT_REFUSED
    try:
        repo = repository or RiskRepository(url)
        repo.check_role()
        result = record_exclusion(ManagedStore(repo), day, reason,
                                  now=now or datetime.now(UTC))
    except ValueError as exc:
        code = str(exc)
        print("CLOUD_STATS_EXCLUSION_REFUSED: " + (code if REFUSAL_CODE.fullmatch(code)
                                                   else "INVALID"), file=out)
        return EXIT_REFUSED
    except Exception as exc:
        print("CLOUD_STATS_EXCLUSION_FAILED: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    print(json.dumps({"action": "exclude-from-stats", "mode": "PAPER_ONLY", **result},
                     sort_keys=True), file=out)
    return 0


def strategy_ladder(action, strategy_id, *, owner_ruling, reason=None, history_report=None,
                    owner_override_reason=None, environ=None, out=None, repository=None,
                    now=None, stdin=None):
    """The owner's ``STRATEGY_PROMOTION_V1`` / ``STRATEGY_DEMOTION_V1`` (package plugin-c3) in the
    trader's shell, over the trader's own ``MANAGED_DATABASE_URL`` (the role that appends managed
    events). ``history_report`` is a path in the container or ``-`` (the results.json on
    standard input). No broker call; the DSN and driver text are never printed."""
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.managed_store import ManagedStore
    from catalyst_lab.strategy_paper import demote, promote

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    try:
        url = cloud_config.database_url(environ, "MANAGED_DATABASE_URL",
                                        on_railway=bool(environ.get("RAILWAY_ENVIRONMENT_ID")))
    except CloudConfigError as exc:
        print("CLOUD_STRATEGY_LADDER_REFUSED: " + exc.message(), file=out)
        return EXIT_REFUSED
    try:
        store = ManagedStore(repository or RiskRepository(url))
        store.repo.check_role()
        stamp = now or datetime.now(UTC)
        if action == "promote-strategy":
            if history_report == "-":
                report = (stdin or sys.stdin.buffer).read()
            elif history_report:
                report = Path(history_report)
            else:
                raise ValueError("PROMOTION_HISTORY_REPORT_REQUIRED")
            result = promote(store, strategy_id, history_report=report,
                             owner_ruling_ref=owner_ruling,
                             owner_override_reason=owner_override_reason,
                             operator="RAILWAY_OPS_SHELL", now=stamp)
        else:
            result = demote(store, strategy_id, reason=reason, owner_ruling_ref=owner_ruling,
                            operator="RAILWAY_OPS_SHELL", now=stamp)
    except ValueError as exc:
        code = str(exc)
        print("CLOUD_STRATEGY_LADDER_REFUSED: " + (code if REFUSAL_CODE.fullmatch(code)
                                                   else "INVALID"), file=out)
        return EXIT_REFUSED
    except Exception as exc:
        print("CLOUD_STRATEGY_LADDER_FAILED: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    print(json.dumps(result, sort_keys=True), file=out)
    return 0


def backup_now(environ=None, *, out=None, now=None, take=None, cwd=None, release_root=None,
               euid=os.geteuid):
    """The owner's fresh backup before a guarded cloud migration (package cloud-migrate):
    ``railway ssh --service ops -- python -m catalyst_lab.cloud_entry backup``, which drops the
    root shell to the ops service's own user before this runs.

    Exactly the daily backup (``ledger_ops.backup_database`` as ``catalyst_backup`` into the ops
    volume's ``backups/<UTC time>``), verified against its manifest, then printed with its
    ``migration_backup_reference`` (``ledger_ops.BACKUP_REFERENCE``), the value ``cloud_provision
    migrate --backup`` takes. Names, counts and hashes only; the retention prunes it like any
    other backup (never the newest). Root is refused: its files would be unreadable to ops.
    """
    from catalyst_lab import ledger_ops

    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    try:
        if euid() == 0:
            raise CloudConfigError("CLOUD_BACKUP_AS_ROOT_REFUSED")
        config = cloud_config.ops_config(environ, cwd=cwd, release_root=release_root)
        backups = _private_directory(config.runtime.state_dir) / "backups"
    except (CloudConfigError, ValueError) as exc:
        print("CLOUD_BACKUP_REFUSED: " + failure_code(exc), file=out)
        return EXIT_REFUSED
    try:
        taken = (take or ledger_ops.backup_database)(config.backup_database_url, backups,
                                                     now=now)
        verified = ledger_ops.verify_manifest(
            taken["backup"], expected_manifest_sha256=taken["manifest_sha256"])
        reference = ledger_ops.backup_reference(taken)
    except Exception as exc:
        print("CLOUD_BACKUP_FAILED: " + failure_code(exc), file=out)
        return EXIT_STARTUP_FAILED
    print(json.dumps({
        "result": verified["result"], "backup": Path(taken["backup"]).name,
        "taken_at": taken["taken_at"], "schema_version": taken["schema_version"],
        "audit_seq": taken["audit_seq"], "audit_head": taken["audit_head"],
        "event_count": taken["event_count"], "manifest_sha256": taken["manifest_sha256"],
        "migration_backup_reference": reference, "broker_requests": 0,
    }, sort_keys=True), file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m catalyst_lab.cloud_runtime",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("component", choices=("trader", "ops", "jobs", "status", "positions",
                                              "scorecard", "market-review", "weekly-review",
                                              "daily-brief",
                                              "clear-protection-latch", "backup",
                                              "exclude-from-stats", "promote-strategy",
                                              "demote-strategy"))
    parser.add_argument("--reason", help="clear-protection-latch: why (10 to 500 characters); "
                        "exclude-from-stats: the reason code")
    parser.add_argument("--day", type=date.fromisoformat,
                        help="scorecard, market-review: the New York day (default yesterday); "
                        "exclude-from-stats: the day to leave out of the statistics")
    parser.add_argument("--week-end", type=date.fromisoformat,
                        help="weekly-review: the Sunday ending the week (default the latest)")
    parser.add_argument("--now", action="store_true",
                        help="weekly-review: compute the latest finished week, not recorded")
    parser.add_argument("--full", action="store_true",
                        help="scorecard: print the whole recorded body")
    parser.add_argument("--strategy", help="promote-strategy, demote-strategy: the strategy id")
    parser.add_argument("--history-report",
                        help="promote-strategy: the history test's results.json, or - (stdin)")
    parser.add_argument("--owner-ruling", help="promote-strategy, demote-strategy: the owner's "
                        "ruling reference")
    parser.add_argument("--owner-override-reason",
                        help="promote-strategy: only if the owner insists past the ladder")
    args = parser.parse_args(argv)
    if args.component in {"promote-strategy", "demote-strategy"}:
        if not args.strategy or not args.owner_ruling or (
                args.component == "demote-strategy" and args.reason is None):
            parser.error(f"{args.component} requires --strategy and --owner-ruling"
                         + (" and --reason" if args.component == "demote-strategy" else ""))
        return strategy_ladder(args.component, args.strategy, owner_ruling=args.owner_ruling,
                               reason=args.reason, history_report=args.history_report,
                               owner_override_reason=args.owner_override_reason)
    if args.component == "clear-protection-latch":
        if args.reason is None:
            parser.error("clear-protection-latch requires --reason")
        return clear_protection_latch(args.reason)
    if args.component == "exclude-from-stats":
        if args.reason is None or args.day is None:
            parser.error("exclude-from-stats requires --day and --reason")
        return exclude_from_stats(args.day, args.reason)
    if args.reason is not None:
        parser.error("--reason is only for clear-protection-latch")
    if args.day is not None and args.component not in {"scorecard", "market-review",
                                                       "daily-brief"}:
        parser.error("--day is only for scorecard, market-review and daily-brief")
    if (args.week_end is not None or args.now) and args.component != "weekly-review":
        parser.error("--week-end and --now are only for weekly-review")
    if args.full and args.component != "scorecard":
        parser.error("--full is only for scorecard")
    if args.component == "status":
        return status()
    if args.component == "positions":
        return positions()
    if args.component == "backup":
        return backup_now()
    if args.component == "scorecard":
        return scorecard(args.day, full=args.full)
    if args.component == "market-review":
        return market_review(args.day)
    if args.component == "daily-brief":
        return daily_brief(args.day)
    if args.component == "weekly-review":
        return weekly_review(args.week_end, preview=args.now)
    if args.component == "jobs":
        return jobs()
    if args.component == "ops":
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        return ops(stop=stop)

    def interrupt(*_):
        raise KeyboardInterrupt  # PID 1 ignores SIGTERM without a handler.

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupt)  # uvicorn installs its own once it serves.
    return trader()


if __name__ == "__main__":
    sys.exit(main())
