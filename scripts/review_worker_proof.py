"""Disposable subprocess/supervisor proof. No broker code or credentials are loaded."""

import argparse
import asyncio
import json
import multiprocessing
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from research_report_proof import fixture_response, packet, write_private

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.jev_store import JevStore
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings


def child(root, provider, stop, entered):
    async def fake(request):
        if b"CRASH_BOUNDARY_FIXTURE" in request.content:
            entered.set()
            await asyncio.sleep(30)
        return fixture_response(request)

    async def run():
        w = ReviewWorker(
            WorkerSettings(
                localdb.connection_url(Path(root), "catalyst_jev"),
                Gate1Inputs(APPROVED_GATE1),
                "local-proof",
                4,
                0.05,
            ),
            key_provider=typesafe_key
            if provider == "typesafe"
            else lambda: "fixture-only-credential",
            transport=None if provider == "typesafe" else httpx.MockTransport(fake),
        )
        event = asyncio.Event()

        async def receive_stop():
            while not stop.is_set():
                await asyncio.sleep(0.05)
            event.set()

        listener = asyncio.create_task(receive_stop())
        try:
            await w.run(event)
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    try:
        asyncio.run(run())
    except Exception:
        raise SystemExit(
            "Isolated review proof child failed; inspect its private database"
        ) from None


def wait_until(check, seconds=12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("PROOF_CONDITION_TIMEOUT")


def perform(provider):
    if provider == "typesafe":
        typesafe_key()
    root = Path(tempfile.mkdtemp(prefix="catalyst-worker-proof-", dir="/tmp"))
    children = []
    report = {
        "provider": provider,
        "market_evidence": "SYNTHETIC_ENGINEERING_FIXTURE",
        "cohort": "JEV_ENGINEERING_TEST",
        "broker_requests": 0,
        "steps": [],
        "database_directory": str(root),
    }
    context = multiprocessing.get_context("spawn")

    def launch():
        stop, entered = context.Event(), context.Event()
        process = context.Process(target=child, args=(str(root), provider, stop, entered))
        process.start()
        children.append((process, stop))
        return process, stop, entered

    try:
        localdb.start(root)
        settings = ReviewSettings(
            localdb.connection_url(root, "catalyst_review"),
            "proof-write-" + uuid4().hex,
            "proof-read-" + uuid4().hex,
            "local",
            Gate1Inputs(APPROVED_GATE1),
            "MUSE_JEV_ACTIVE_V1",
        )
        reports = ResearchReports(settings.database_url, settings.gate1)
        store = JevStore(localdb.connection_url(root, "catalyst_jev"))
        repo = Repository(localdb.connection_url(root))
        with TestClient(
            create_review_app(settings, credential_provider=lambda: "fixture-api-startup-check")
        ) as api:
            writer = {"Authorization": "Bearer " + settings.write_token}
            reader = {"Authorization": "Bearer " + settings.read_token}
            process, stop, entered = launch()
            wait_until(lambda: any(w["status"] == "RUNNING" for w in reports.workers()["workers"]))
            submitted = []
            for market in ("US_STOCKS", "CRYPTO", "INDIA"):
                data = packet(market, 3)
                result = api.post("/api/v1/research-reports", json=data, headers=writer)
                result.raise_for_status()
                submitted.append(data["report_id"])
                done = wait_until(
                    lambda report_id=data["report_id"]: (
                        (r if all(i["recorded_disposition"] for i in r["items"]) else None)
                        if (r := reports.report(report_id))
                        else None
                    )
                )
                report["steps"].append(
                    {
                        "market": market,
                        "counts": done["counts"],
                        "report_id": data["report_id"],
                        "worker_process_pid": process.pid,
                    }
                )
            if provider == "fixture":
                crash = packet("US_STOCKS", 1)
                crash["items"][0]["symbol"] = "CRASHTEST"
                crash["items"][0]["thesis"] = "CRASH_BOUNDARY_FIXTURE"
                crash["valid_until"] = (datetime.now(UTC) + timedelta(seconds=3)).isoformat()
                api.post("/api/v1/research-reports", json=crash, headers=writer).raise_for_status()
                if not entered.wait(2):
                    raise RuntimeError("CRASH_BOUNDARY_NOT_REACHED")
                process.kill()  # Only this disposable, broker-incapable child PID.
                process.join(3)
                assert process.exitcode is not None
                report["crash_process_exit_code"] = process.exitcode
            else:
                stop.set()
                process.join(5)
                assert process.exitcode == 0
            process, stop, entered = launch()
            wait_until(
                lambda: sum(w["status"] == "RUNNING" for w in reports.workers()["workers"]) >= 1
            )
            if provider == "fixture":
                done = wait_until(
                    lambda: (
                        (r if r["items"][0]["recorded_disposition"] else None)
                        if (r := reports.report(crash["report_id"]))
                        else None
                    )
                )
                report["unknown_inflight_recovery"] = done["items"][0]["reason"]
                with store.connect() as conn:
                    requests = conn.execute(
                        "SELECT count(*) AS n FROM lab.jev_requests "
                        "WHERE evidence_identity->>'report_id'=%s",
                        (crash["report_id"],),
                    ).fetchone()["n"]
                assert requests == 1 and done["items"][0]["recorded_disposition"] == "NEEDS_REVIEW"
            # A fresh report demonstrates continued queue consumption after real process restart.
            data = packet("INDIA", 1)
            data["items"][0]["symbol"] = "NEWRESTART"
            api.post("/api/v1/research-reports", json=data, headers=writer).raise_for_status()
            done = wait_until(
                lambda: (
                    (r if r["items"][0]["recorded_disposition"] else None)
                    if (r := reports.report(data["report_id"]))
                    else None
                )
            )
            report["restart_result"] = done["counts"]
            report["restart_process_pid"] = process.pid
            cursor = 0
            delivered = []
            while True:
                page = api.get(
                    f"/api/v1/research-output?after_seq={cursor}&limit=2", headers=reader
                ).json()
                delivered += page["events"]
                if page["next_cursor"] == cursor:
                    break
                cursor = page["next_cursor"]
            assert len({e["event_seq"] for e in delivered}) == len(delivered)
            report["durable_output_events"] = len(delivered)
            report["output_cursor"] = cursor
            stop.set()
            process.join(5)
            assert process.exitcode == 0
        with store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts ORDER BY event_seq"
            ).fetchall()
            request_count = conn.execute("SELECT count(*) AS n FROM lab.jev_requests").fetchone()[
                "n"
            ]
        report["receipt_verifications"] = [store.verify(r["receipt_id"]) for r in receipts]
        report["request_count"] = request_count
        report["receipt_count"] = len(receipts)
        report["latency"] = store.metrics("ENGINEERING_TEST")
        with repo.connect() as conn:
            report["execution_counts"] = {
                t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                for t in ("candidates", "orders", "fills", "risk_decisions")
            }
        assert not any(report["execution_counts"].values())
        events = repo.export_events()
        report["audit"] = verify_events(events)
        write_private(root / "audit.json", events)
        write_private(root / "proof.json", report)
        return json_safe(report)
    finally:
        for process, stop in children:
            if process.is_alive():
                stop.set()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(3)
        if (root / "postgres" / "postmaster.pid").exists():
            localdb.stop(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("fixture", "typesafe"), required=True)
    args = parser.parse_args()
    result = perform(args.provider)
    print(json.dumps({k: v for k, v in result.items() if k != "receipt_verifications"}, indent=2))
