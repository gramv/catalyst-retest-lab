"""External Muse worker with a durable SQLite outbox and no broker/database credentials.

Run as a service it submits AGENT_RESEARCH_REPORT_V2 reports whose ``agent`` block names
``--agent-id``/``--agent-version`` (defaults: the legacy identity ``muse`` /
``LEGACY_UNDECLARED``) and the embedded guidelines version and SHA-256. The agent block is
added here, never by the model. A ``MuseWorker`` built without an agent ID keeps submitting
unversioned legacy reports.
"""

import argparse
import fcntl
import json
import os
import re
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from catalyst_lab.agent_identity import (
    AGENT_ID,
    AGENT_VERSION,
    LEGACY_AGENT_ID,
    LEGACY_AGENT_VERSION,
    REPORT_SCHEMA_V2,
)
from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_SHA256, MUSE_GUIDELINES_VERSION

ALLOWED = (
    "/api/v1/lab/research-reports",
    "/api/v1/lab/outputs",
    "/api/v1/lab/cycles/",
    "/api/v1/lab/positions",
)


class MuseSpool:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("PRIVATE_SPOOL_PATH_REQUIRED")
        self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,method TEXT NOT NULL,
            path TEXT NOT NULL,body TEXT,created_at TEXT NOT NULL,deadline TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT,last_error TEXT);
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,market TEXT,
            started_at TEXT NOT NULL,outcome TEXT NOT NULL,detail TEXT);
          CREATE TABLE IF NOT EXISTS provider_jobs(id TEXT PRIMARY KEY,request TEXT NOT NULL,
            result TEXT,started_at TEXT NOT NULL,completed_at TEXT);
          CREATE TABLE IF NOT EXISTS cycles(cycle_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,expires_at TEXT,done INTEGER NOT NULL DEFAULT 0);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(cycles)")}
        if "expires_at" not in columns:
            self.db.execute("ALTER TABLE cycles ADD COLUMN expires_at TEXT")
        if "done" not in columns:
            self.db.execute("ALTER TABLE cycles ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    def installation_id(self):
        candidate = str(uuid4())
        self.db.execute("INSERT OR IGNORE INTO state VALUES('installation_id',?)", (candidate,))
        self.db.commit()
        return self.get("installation_id")

    def enqueue(self, method, path, body, deadline, *, item_id=None):
        item_id = item_id or str(uuid4())
        encoded = (
            json.dumps(body, sort_keys=True, separators=(",", ":")) if body is not None else None
        )
        self.db.execute(
            "INSERT OR IGNORE INTO outbox(id,method,path,body,created_at,deadline) "
            "VALUES(?,?,?,?,?,?)",
            (item_id, method, path, encoded, datetime.now(UTC).isoformat(), deadline.isoformat()),
        )
        self.db.commit()
        return item_id

    def pending(self):
        return self.db.execute(
            "SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY created_at"
        ).fetchall()

    def result(self, item_id, *, delivered=False, error=None):
        self.db.execute(
            "UPDATE outbox SET attempts=attempts+1,delivered_at=CASE WHEN ? THEN ? "
            "ELSE delivered_at END,last_error=? WHERE id=?",
            (delivered, datetime.now(UTC).isoformat(), error, item_id),
        )
        self.db.commit()

    def log(self, kind, outcome, *, market=None, detail=None):
        self.db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?)",
            (str(uuid4()), kind, market, datetime.now(UTC).isoformat(), outcome, detail),
        )
        self.db.commit()

    def provider_once(self, job_id, request, provider):
        encoded_request = json.dumps(request, sort_keys=True, separators=(",", ":"))
        row = self.db.execute("SELECT * FROM provider_jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            if row["request"] != encoded_request:
                raise ValueError("PROVIDER_JOB_IDEMPOTENCY_MISMATCH")
            if row["result"] is not None:
                return json.loads(row["result"])
            else:
                raise RuntimeError("PROVIDER_JOB_INTERRUPTED_NO_REROLL")
        self.db.execute(
            "INSERT INTO provider_jobs(id,request,started_at) VALUES(?,?,?)",
            (job_id, encoded_request, datetime.now(UTC).isoformat()),
        )
        self.db.commit()
        result = provider.invoke(request)
        encoded_result = json.dumps(result, sort_keys=True, separators=(",", ":"))
        self.db.execute(
            "UPDATE provider_jobs SET result=?,completed_at=? WHERE id=?",
            (encoded_result, datetime.now(UTC).isoformat(), job_id),
        )
        self.db.commit()
        return result

    def fail_job(self, job_id, reason):
        self.set("failed-job:" + job_id, reason)

    def job_failed(self, job_id):
        return self.get("failed-job:" + job_id) is not None

    def remember_cycle(self, cycle_id, expires_at=None):
        self.db.execute(
            "INSERT INTO cycles(cycle_id,first_seen_at,expires_at,done) VALUES(?,?,?,0) "
            "ON CONFLICT(cycle_id) DO UPDATE SET expires_at="
            "COALESCE(excluded.expires_at,cycles.expires_at)",
            (cycle_id, datetime.now(UTC).isoformat(), expires_at),
        )
        self.db.commit()

    def cycles(self, now=None):
        if now is not None:
            self.db.execute(
                "UPDATE cycles SET done=1 WHERE done=0 "
                "AND expires_at IS NOT NULL AND expires_at<=?",
                (now.isoformat(),),
            )
            self.db.commit()
        return [
            row["cycle_id"]
            for row in self.db.execute("SELECT * FROM cycles WHERE done=0 ORDER BY cycle_id")
        ]

    def finish_cycle(self, cycle_id, outcome):
        self.db.execute("UPDATE cycles SET done=1 WHERE cycle_id=?", (cycle_id,))
        self.db.commit()
        self.log("EVIDENCE_CYCLE", outcome, detail=cycle_id)


class MuseHttp:
    def __init__(self, base_url, token, *, timeout=15, opener=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("LOOPBACK_MUSE_API_REQUIRED")
        self.base, self.token, self.timeout, self.opener = (
            base_url.rstrip("/"),
            token,
            timeout,
            opener
            or build_opener(
                type(
                    "NoRedirect",
                    (HTTPRedirectHandler,),
                    {"redirect_request": lambda *args, **kwargs: None},
                )()
            ).open,
        )

    def call(self, method, path, body=None):
        target = urlsplit(path)
        plain = target.path
        position_news = re.fullmatch(r"/api/v1/lab/positions/[^/]+/news", plain)
        cycle_outputs = re.fullmatch(r"/api/v1/lab/cycles/[^/]+/outputs", plain)
        cycle_write = re.fullmatch(
            r"/api/v1/lab/cycles/[^/]+/(evidence|evidence-tasks/claim)", plain
        )
        paging_query = set(parse_qs(target.query)) <= {"after", "limit"}
        valid = (
            (method == "POST" and plain == "/api/v1/lab/research-reports")
            or (method == "GET" and plain == "/api/v1/lab/outputs" and paging_query)
            or (method == "GET" and plain == "/api/v1/lab/positions")
            or (method in {"GET", "POST"} and position_news and not target.query)
            or (method == "GET" and cycle_outputs and paging_query)
            or (method == "POST" and cycle_write and not target.query)
        )
        if not valid or target.scheme or target.netloc or ".." in plain:
            raise ValueError("MUSE_ENDPOINT_NOT_ALLOWED")
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ValueError("MUSE_RESPONSE_TOO_LARGE")
                return json.loads(raw or b"{}")
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in {408, 409, 429}:
                raise ValueError("MUSE_REQUEST_REJECTED") from None
            raise RuntimeError("MUSE_REQUEST_UNCERTAIN") from None
        except (URLError, TimeoutError):
            raise RuntimeError("MUSE_REQUEST_UNCERTAIN") from None


class MuseWorker:
    def __init__(
        self,
        spool,
        api,
        provider,
        *,
        claimant="codex-muse",
        report_valid_seconds=1800,
        max_attempts=5,
        clock=lambda: datetime.now(UTC),
        agent_id=None,
        agent_version=LEGACY_AGENT_VERSION,
    ):
        if agent_id is not None and (
            not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id)
            or not isinstance(agent_version, str) or not AGENT_VERSION.fullmatch(agent_version)
        ):
            raise ValueError("VALID_AGENT_IDENTITY_REQUIRED")
        self.spool, self.api, self.provider = spool, api, provider
        self.claimant, self.report_valid_seconds = claimant, report_valid_seconds
        self.max_attempts, self.clock = max_attempts, clock
        self.agent_id, self.agent_version = agent_id, agent_version

    def _agent_block(self, installation, job_id):
        """The report's agent block (added by the worker, never by the model)."""
        return {
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "guidelines_version": MUSE_GUIDELINES_VERSION,
            "guidelines_sha256": MUSE_GUIDELINES_SHA256,
            "run_id": str(uuid5(NAMESPACE_URL, f"catalyst-muse-run:{installation}:{job_id}")),
        }

    def _own_cycle(self, body):
        """Cycles recorded for another agent are not this worker's evidence work."""
        agent = body.get("agent")
        if isinstance(agent, dict) and agent.get("agent_id"):
            return agent["agent_id"] == (self.agent_id or LEGACY_AGENT_ID)
        # An unattributed legacy cycle is answered only through the legacy credential.
        return (self.agent_id or LEGACY_AGENT_ID) == LEGACY_AGENT_ID

    def prepare_report(self, market):
        sequence = int(self.spool.get("report_sequence", "0")) + 1
        request = {
            "job": "market_research",
            "market": market,
            "target_contenders": "20-30_without_padding",
        }
        job_id = f"report-job:{market}:{sequence}"
        try:
            result = self.spool.provider_once(job_id, request, self.provider)
        except RuntimeError as exc:
            if str(exc) != "PROVIDER_JOB_INTERRUPTED_NO_REROLL":
                raise
            self.spool.set("report_sequence", sequence)
            self.spool.log("RESEARCH", "INTERRUPTED_NO_REROLL", market=market)
            return None
        if result.get("kind") == "no_op":
            self.spool.set("report_sequence", sequence)
            self.spool.log(
                "RESEARCH",
                "NO_OP",
                market=market,
                detail=str(result.get("reason", "NO_QUALIFIED_RESEARCH"))[:300],
            )
            return None
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            self.spool.fail_job(job_id, "MUSE_PROVIDER_REPORT_INVALID")
            self.spool.set("report_sequence", sequence)
            self.spool.log("RESEARCH", "INVALID_PROVIDER_RESULT", market=market)
            return None
        completed = self.clock()
        installation = self.spool.installation_id()
        report_id = str(uuid5(NAMESPACE_URL, f"catalyst-muse:{installation}:{market}:{sequence}"))
        payload = {
            "report_id": report_id,
            "generated_at": completed.isoformat(),
            "valid_until": (completed + timedelta(seconds=self.report_valid_seconds)).isoformat(),
            "items": items,
        }
        if self.agent_id is not None:
            payload["schema_version"] = REPORT_SCHEMA_V2
            payload["agent"] = self._agent_block(installation, job_id)
        self.spool.enqueue(
            "POST",
            "/api/v1/lab/research-reports",
            payload,
            completed + timedelta(seconds=self.report_valid_seconds),
            item_id="report:" + report_id,
        )
        self.spool.set("report_sequence", sequence)
        self.spool.log("RESEARCH", "PREPARED", market=market, detail=report_id)
        return report_id

    def scheduled_research(self, market, interval_seconds):
        if market not in {"US_STOCKS", "CRYPTO"} or not 60 <= interval_seconds <= 86400:
            raise ValueError("VALID_RESEARCH_SCHEDULE_REQUIRED")
        now = self.clock()
        key = "next_research:" + market
        due = self.spool.get(key)
        if due and now < datetime.fromisoformat(due):
            return None
        result = self.prepare_report(market)
        self.spool.set(key, (now + timedelta(seconds=interval_seconds)).isoformat())
        return result

    def us_research_window_open(self, *, start_hour=8, end_hour=17):
        local = self.clock().astimezone(ZoneInfo("America/New_York"))
        return local.weekday() < 5 and start_hour <= local.hour < end_hour

    def flush(self):
        now = self.clock()
        for row in self.spool.pending():
            if datetime.fromisoformat(row["deadline"]) <= now:
                self.spool.result(row["id"], delivered=True, error="EXPIRED_UNSENT")
                self.spool.log("OUTBOX", "EXPIRED", detail=row["id"])
                continue
            if row["attempts"] >= self.max_attempts:
                continue
            try:
                self.api.call(
                    row["method"], row["path"], json.loads(row["body"]) if row["body"] else None
                )
                self.spool.result(row["id"], delivered=True)
                self.spool.log("OUTBOX", "DELIVERED", detail=row["id"])
            except RuntimeError:
                self.spool.result(row["id"], error="UNCERTAIN_HTTP")
            except ValueError:
                self.spool.result(row["id"], delivered=True, error="REJECTED")
                self.spool.log("OUTBOX", "REJECTED", detail=row["id"])

    def poll(self):
        cursor = int(self.spool.get("cursor", "0"))
        while True:
            result = self.api.call("GET", f"/api/v1/lab/outputs?after={cursor}&limit=10")
            events = result.get("items", result.get("events", []))
            for event in events:
                body = event.get("body", {})
                cycle_id = body.get("cycle_id")
                if not cycle_id:
                    continue
                if event.get("kind") == "RESEARCH_STARTED" and not self._own_cycle(body):
                    self.spool.set("foreign-cycle:" + cycle_id, "1")
                    continue
                if event.get("kind") in {"RESEARCH_STARTED", "RESEARCH_EVIDENCE_TASK"} and (
                    self.spool.get("foreign-cycle:" + cycle_id) is None
                ):
                    self.spool.remember_cycle(cycle_id, body.get("expires_at"))
            next_cursor = int(result.get("next_cursor", cursor))
            self.spool.set("cursor", next_cursor)
            if not events or next_cursor <= cursor:
                break
            cursor = next_cursor

        for cycle_id in self.spool.cycles(self.clock()):
            try:
                self._poll_cycle(cycle_id)
            except ValueError:
                self.spool.finish_cycle(cycle_id, "PERMANENT_REJECT")
            except RuntimeError:
                self.spool.log("EVIDENCE_CYCLE", "TRANSIENT_FAILURE", detail=cycle_id)

    def _poll_cycle(self, cycle_id):
        seen_tasks = set()
        cycle_events, cycle_cursor = [], 0
        while True:
            suffix = f"?after={cycle_cursor}&limit=10"
            cycle = self.api.call("GET", f"/api/v1/lab/cycles/{cycle_id}/outputs{suffix}")
            page = cycle.get("items", cycle.get("events", []))
            cycle_events.extend(page)
            next_cycle_cursor = int(cycle.get("next_cursor", cycle_cursor))
            if not page or next_cycle_cursor <= cycle_cursor:
                break
            cycle_cursor = next_cycle_cursor
        packets = {
            e["body"].get("item_key"): e["body"]
            for e in cycle_events
            if e.get("kind") == "RESEARCH_PACKET"
        }
        while True:
            claimed = self.api.call(
                "POST",
                f"/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim",
                {"claimant": self.claimant, "lease_seconds": 300, "limit": 10},
            )
            tasks = claimed.get("tasks", [])
            if not tasks:
                break
            fresh = [task for task in tasks if task.get("task_id") not in seen_tasks]
            if not fresh:
                break
            for task in fresh:
                seen_tasks.add(task["task_id"])
                try:
                    self._prepare_evidence(cycle_id, task, packets)
                except (KeyError, RuntimeError, TypeError, ValueError):
                    self.spool.fail_job("evidence-job:" + task["task_id"], "TERMINAL_FAILURE")
                    self.spool.log("EVIDENCE", "TERMINAL_FAILURE", detail=task["task_id"])
            if len(tasks) < 10:
                break

    def _prepare_evidence(self, cycle_id, task, packets):
        job_id = "evidence-job:" + task["task_id"]
        if self.spool.job_failed(job_id):
            return
        request = {
            "job": "evidence_followup",
            "task": task,
            "packet": packets.get(task["item_key"]),
        }
        answer = self.spool.provider_once(job_id, request, self.provider)
        if answer.get("kind") == "no_op":
            self.spool.fail_job(job_id, "NO_OP")
            return
        required = {"sources", "thesis", "disproof", "economic_relationship"}
        if not isinstance(answer, dict) or not required <= answer.keys():
            raise ValueError("MUSE_PROVIDER_FOLLOWUP_INVALID")
        body = {
            "task_id": task["task_id"],
            "item_key": task["item_key"],
            "revision": task["revision"] + 1,
            "sources": answer["sources"],
            "thesis": answer["thesis"],
            "disproof": answer["disproof"],
            "economic_relationship": answer["economic_relationship"],
            "technical_facts": answer.get("technical_facts"),
        }
        deadline = datetime.fromisoformat(task["expires_at"])
        self.spool.enqueue(
            "POST",
            f"/api/v1/lab/cycles/{cycle_id}/evidence",
            body,
            deadline,
            item_id="evidence:" + task["task_id"],
        )

    def refresh_position_news(self):
        positions = self.api.call("GET", "/api/v1/lab/positions").get("items", [])
        for position in positions:
            if position.get("state", {}).get("state") != "OPEN":
                continue
            setup = position["setup_id"]
            context = self.api.call("GET", f"/api/v1/lab/positions/{setup}/news")
            lifecycle = context["lifecycle_id"]
            revision = int(context.get("news_revision", context.get("revision", 0)))
            bucket = int(self.clock().timestamp()) // 300
            request = {"job": "position_news", "position": position, "news_context": context}
            job_id = f"news-job:{setup}:{lifecycle}:{revision}:{bucket}"
            if self.spool.job_failed(job_id):
                continue
            try:
                answer = self.spool.provider_once(job_id, request, self.provider)
            except (RuntimeError, TypeError, ValueError):
                self.spool.fail_job(job_id, "TERMINAL_FAILURE")
                self.spool.log("NEWS", "TERMINAL_FAILURE", detail=setup)
                continue
            if answer.get("kind") == "no_op":
                continue
            if not isinstance(answer.get("sources"), list) or not answer["sources"]:
                self.spool.fail_job(job_id, "MUSE_PROVIDER_NEWS_INVALID")
                self.spool.log("NEWS", "TERMINAL_FAILURE", detail=setup)
                continue
            news_id = str(uuid5(NAMESPACE_URL, f"{setup}:{lifecycle}:{revision}:{bucket}"))
            body = {
                "news_id": news_id,
                "lifecycle_id": lifecycle,
                "expected_news_revision": revision,
                "sources": answer["sources"],
            }
            self.spool.enqueue(
                "POST",
                f"/api/v1/lab/positions/{setup}/news",
                body,
                self.clock() + timedelta(minutes=30),
                item_id="news:" + news_id,
            )


def read_token(path):
    target = Path(path)
    if target.is_symlink():
        raise ValueError("PRIVATE_TOKEN_FILE_REQUIRED")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > 4096
        ):
            raise ValueError("PRIVATE_TOKEN_FILE_REQUIRED")
        try:
            value = os.read(descriptor, 4097).decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ValueError("PRIVATE_TOKEN_FILE_REQUIRED") from None
    finally:
        os.close(descriptor)
    if not value:
        raise ValueError("PRIVATE_TOKEN_FILE_REQUIRED")
    return value


def heartbeat_loop(spool_path, stop, interval=60):
    while not stop.wait(interval):
        db = sqlite3.connect(spool_path, timeout=5)
        try:
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    "HEARTBEAT",
                    None,
                    datetime.now(UTC).isoformat(),
                    "RUNNING",
                    None,
                ),
            )
            db.commit()
        finally:
            db.close()


def lane_loop(spool_path, api_factory, provider_factory, stop, kind, action, interval,
              worker_options=None):
    worker = MuseWorker(MuseSpool(spool_path), api_factory(), provider_factory(),
                        **(worker_options or {}))
    while not stop.is_set():
        try:
            action(worker)
        except Exception:
            worker.spool.log(kind, "SANITIZED_FAILURE")
        stop.wait(interval)


def service_lanes(spool_path, api_factory, provider_factory, stop, us_interval, crypto_interval,
                  *, worker_options=None):
    def research(worker):
        if worker.us_research_window_open():
            worker.scheduled_research("US_STOCKS", us_interval)
        worker.scheduled_research("CRYPTO", crypto_interval)

    specifications = (
        ("delivery", lambda worker: worker.flush(), 1),
        ("research", research, 5),
        ("evidence", lambda worker: worker.poll(), 5),
        ("news", lambda worker: worker.refresh_position_news(), 60),
    )
    lanes = [
        threading.Thread(
            target=lane_loop,
            args=(spool_path, api_factory, provider_factory, stop, kind, action, interval,
                  worker_options),
            name="muse-" + kind,
            daemon=True,
        )
        for kind, action, interval in specifications
    ]
    for lane in lanes:
        lane.start()
    return lanes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--spool", required=True)
    parser.add_argument("--command", action="append", required=True)
    parser.add_argument("--us-interval-seconds", type=int, default=1800)
    parser.add_argument("--crypto-interval-seconds", type=int, default=1800)
    parser.add_argument("--once", action="store_true")
    # The agent this worker reports as; the token must be that agent's credential (the
    # legacy Muse token acts for ``muse``).
    parser.add_argument("--agent-id", default=LEGACY_AGENT_ID)
    parser.add_argument("--agent-version", default=LEGACY_AGENT_VERSION)
    args = parser.parse_args()
    if not AGENT_ID.fullmatch(args.agent_id) or not AGENT_VERSION.fullmatch(args.agent_version):
        parser.error("--agent-id must match [a-z][a-z0-9_-]{1,31} and --agent-version "
                     "[A-Za-z0-9][A-Za-z0-9._+-]{0,31}")
    identity = {"agent_id": args.agent_id, "agent_version": args.agent_version}
    from catalyst_lab.muse_codex import CommandProvider

    spool = MuseSpool(args.spool)
    lock_path = Path(args.spool).with_suffix(".lock")
    lock = os.open(
        lock_path,
        os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    if not stat.S_ISREG(os.fstat(lock).st_mode):
        os.close(lock)
        raise ValueError("PRIVATE_WORKER_LOCK_REQUIRED")
    os.fchmod(lock, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("MUSE_WORKER_ALREADY_RUNNING") from None
        token = read_token(args.token_file)

        def api_factory():
            return MuseHttp(args.api, token)

        def provider_factory():
            return CommandProvider(args.command)

        worker = MuseWorker(spool, api_factory(), provider_factory(), **identity)
        worker.spool.log("HEARTBEAT", "RUNNING")
        heartbeat_stop = threading.Event()
        heartbeat = None
        if not args.once:
            heartbeat = threading.Thread(
                target=heartbeat_loop,
                args=(args.spool, heartbeat_stop),
                name="muse-heartbeat",
                daemon=True,
            )
            heartbeat.start()
        if args.once:
            worker.flush()
            worker.poll()
            worker.refresh_position_news()
            if worker.us_research_window_open():
                worker.scheduled_research("US_STOCKS", args.us_interval_seconds)
            worker.flush()
            worker.scheduled_research("CRYPTO", args.crypto_interval_seconds)
            worker.flush()
        else:
            lanes = service_lanes(
                args.spool,
                api_factory,
                provider_factory,
                heartbeat_stop,
                args.us_interval_seconds,
                args.crypto_interval_seconds,
                worker_options=identity,
            )
            while not heartbeat_stop.wait(1):
                pass
            for lane in lanes:
                lane.join(timeout=2)
    finally:
        if "heartbeat_stop" in locals():
            heartbeat_stop.set()
        if "lanes" in locals():
            for lane in lanes:
                lane.join(timeout=2)
        if "heartbeat" in locals() and heartbeat is not None:
            heartbeat.join(timeout=2)
        # Leave the process-owned descriptor held if a timed-out provider lane
        # is still alive. OS process exit releases it; an in-process caller cannot
        # hand off the spool while an old lane can still append or deliver.
        if not any(lane.is_alive() for lane in locals().get("lanes", ())):
            os.close(lock)


if __name__ == "__main__":
    main()
