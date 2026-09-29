"""Supervised app-owned research worker. Has no broker/risk connection or import."""

import argparse
import asyncio
import json
import math
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from catalyst_lab.jev_review import JevReviewer
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.jev_store import JevStore
from catalyst_lab.research_reports import finish_report_item, report_review_args
from catalyst_lab.review_config import Gate1Inputs
from catalyst_lab.review_runtime import Gate1Runtime, database_clock_sample


class WorkerJevStore(JevStore):
    """Bound DB waits too; unavailable persistence cannot stall a worker indefinitely."""

    def __init__(self, database_url, gate1):
        super().__init__(database_url)
        self.connection_seconds = gate1.values["batch_timeout_seconds"]
        self.statement_ms = self.connection_seconds * 1000
        self.transaction_ms = gate1.values["overall_review_deadline_seconds"] * 1000

    def connect(self):
        return psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=self.connection_seconds,
            options=f"-c timezone=UTC -c statement_timeout={self.statement_ms} "
            f"-c lock_timeout={self.statement_ms} "
            f"-c idle_in_transaction_session_timeout={self.transaction_ms}",
        )


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str = field(repr=False)
    gate1: Gate1Inputs
    credential_slot: str
    max_inflight: int
    poll_seconds: float

    def __post_init__(self):
        if (
            not self.database_url
            or type(self.max_inflight) is not int
            or not 1 <= self.max_inflight <= 50
            or not math.isfinite(self.poll_seconds)
            or not 0 < self.poll_seconds <= self.gate1.values["heartbeat_period_seconds"]
        ):
            raise ValueError("EXPLICIT_WORKER_SETTINGS_REQUIRED")

    @classmethod
    def from_env(cls):
        if any(
            os.environ.get(name)
            for name in ("RISK_DATABASE_URL", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
        ):
            raise ValueError("BROKER_CONFIGURATION_FORBIDDEN_ON_REVIEW_WORKER")
        try:
            return cls(
                os.environ["JEV_WORKER_DATABASE_URL"],
                Gate1Inputs(json.loads(os.environ["JEV_REVIEW_POLICY_JSON"])),
                os.environ["JEV_CREDENTIAL_SLOT"],
                int(os.environ["JEV_WORKER_MAX_INFLIGHT"]),
                float(os.environ["JEV_WORKER_POLL_SECONDS"]),
            )
        except (KeyError, ValueError, TypeError):
            raise ValueError("REQUIRED_WORKER_CONFIGURATION_MISSING_OR_INVALID") from None


class ReviewWorker:
    def __init__(self, settings, *, key_provider=typesafe_key, transport=None, clock_health=None):
        self.settings = settings
        self.store = WorkerJevStore(settings.database_url, settings.gate1)
        self.store.check_role()
        # Load once at worker startup through the approved secret route. Fail before claiming jobs.
        key = key_provider()
        if not key or not isinstance(key, str) or any(c.isspace() for c in key):
            raise ValueError("MISSING_TYPESAFE_CREDENTIAL")
        self.worker_id = uuid4()
        self.runtime = Gate1Runtime(
            self.store,
            settings.gate1,
            credential_slot=settings.credential_slot,
            worker_id=self.worker_id,
            clock_health=clock_health or (lambda: database_clock_sample(self.store)),
        )
        self.reviewer = JevReviewer(
            self.store,
            self.runtime.policy,
            key_provider=lambda: key,
            transport=transport,
            runtime=self.runtime,
        )
        self._last_heartbeat = -math.inf
        self._last_health = None

    def heartbeat(self):
        now = datetime.now(UTC)
        status = "RUNNING" if self.runtime.healthy_clock(now) else "CLOCK_UNHEALTHY"
        if (
            time.monotonic() - self._last_heartbeat
            >= self.settings.gate1.values["heartbeat_period_seconds"]
            or status != self._last_health
        ):
            self.runtime.heartbeat(status)
            self._last_heartbeat, self._last_health = time.monotonic(), status
        return status == "RUNNING"

    def expire_jobs(self):
        with self.store.connect() as conn:
            rows = conn.execute("""SELECT i.item_id FROM lab.research_report_items i
              WHERE NOT EXISTS(SELECT 1 FROM lab.research_outcomes o WHERE o.item_id=i.item_id)
              AND (clock_timestamp()>=i.review_deadline OR i.revision<
                (SELECT max(revision) FROM lab.research_reports WHERE report_id=i.report_id))
              ORDER BY i.event_seq LIMIT 200""").fetchall()
        for row in rows:
            finish_report_item(self.store, row["item_id"])

    async def process(self, item_id):
        args = report_review_args(self.store, item_id)
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT request_id FROM lab.jev_requests "
                "WHERE evidence_identity->>'research_item_id'=%s",
                (str(item_id),),
            ).fetchone()
        if existing:
            # Crash recovery never makes a fresh provider vote. Recover only retained valid bytes.
            with self.store.connect() as conn:
                receipt = conn.execute(
                    "SELECT receipt_id FROM lab.jev_receipts WHERE request_id=%s "
                    "AND outcome='VALID' ORDER BY attempt DESC LIMIT 1",
                    (existing["request_id"],),
                ).fetchone()
            if receipt:
                self.store.project_receipt(receipt["receipt_id"])
        else:
            await self.reviewer.jev_review(**args)
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT lab.deliver_review_job(%s,%s,false) AS result", (self.worker_id, item_id)
            ).fetchone()["result"]

    async def tick(self):
        healthy = self.heartbeat()
        self.expire_jobs()
        if not healthy:
            return []
        with self.store.connect() as conn:
            if conn.execute("SELECT lab.review_halted() AS stopped").fetchone()["stopped"]:
                return []
        if self.runtime.probe_due(datetime.now(UTC)):
            await self.runtime.probe(self.reviewer, datetime.now(UTC))
        claimed = []
        for _ in range(self.settings.max_inflight):
            with self.store.connect() as conn:
                item = conn.execute(
                    "SELECT lab.claim_review_job(%s,%s) AS item",
                    (self.worker_id, self.runtime.scope_id),
                ).fetchone()["item"]
            if item is None:
                break
            claimed.append(item)
        return await asyncio.gather(*(self.process(item) for item in claimed))

    async def run(self, stop):
        async def pulse():
            while not stop.is_set():
                self.heartbeat()
                try:
                    await asyncio.wait_for(
                        stop.wait(), self.settings.gate1.values["heartbeat_period_seconds"]
                    )
                except TimeoutError:
                    pass

        task = asyncio.create_task(pulse())
        failed = False
        try:
            while not stop.is_set():
                await self.tick()
                if task.done():
                    task.result()  # Heartbeat failure cannot become a silent background exception.
                try:
                    await asyncio.wait_for(stop.wait(), self.settings.poll_seconds)
                except TimeoutError:
                    pass
        except Exception:
            failed = True
            self.runtime.heartbeat("FAILED")
            raise
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not failed:
                self.runtime.heartbeat("STOPPED")


async def serve(settings):
    worker = ReviewWorker(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await worker.run(stop)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--halt", action="store_true")
    group.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if args.halt or args.resume:
            with JevStore(os.environ["REVIEW_OPERATOR_DATABASE_URL"]).connect() as conn:
                role = conn.execute("SELECT current_user AS role").fetchone()["role"]
                if role != "catalyst_review_operator":
                    raise ValueError("REVIEW_OPERATOR_ROLE_REQUIRED")
                conn.execute("SELECT lab.review_operator_halt(%s)", (args.halt,))
        else:
            asyncio.run(serve(WorkerSettings.from_env()))
    except Exception:
        # DSNs, server responses, evidence and credentials never enter process logs.
        raise SystemExit(
            "Review worker stopped: configuration, credential or durable runtime failure"
        ) from None


if __name__ == "__main__":
    main()
