"""Disposable local browser fixture. No real credentials, model calls or broker imports."""

import argparse
import asyncio
import tempfile
from pathlib import Path

import httpx
import uvicorn
from research_report_proof import fixture_response, packet

from catalyst_lab import localdb
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()
root = Path(tempfile.mkdtemp(prefix="catalyst-ui-proof-", dir="/tmp"))
localdb.start(root)
settings = ReviewSettings(
    localdb.connection_url(root, "catalyst_review"),
    "fixture-write-" + "w" * 40,
    "fixture-read-" + "r" * 40,
    "local",
    Gate1Inputs(APPROVED_GATE1),
    "MUSE_JEV_ACTIVE_V1",
)
reports = ResearchReports(settings.database_url, settings.gate1)
worker = ReviewWorker(
    WorkerSettings(
        localdb.connection_url(root, "catalyst_jev"), settings.gate1, "ui-fixture", 4, 0.1
    ),
    key_provider=lambda: "fixture-only-credential",
    transport=httpx.MockTransport(fixture_response),
)


async def seed():
    for market in ("US_STOCKS", "CRYPTO", "INDIA"):
        data = packet(market, 3)
        reports.submit(data)
        await worker.tick()
    worker.runtime.heartbeat("STOPPED")


asyncio.run(seed())
try:
    uvicorn.run(
        create_review_app(settings, credential_provider=lambda: "fixture-only-credential"),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )
finally:
    localdb.stop(root)
