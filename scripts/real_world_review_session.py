"""Bounded, real-source weekend review exercise. No broker mutation capability.

Uses the existing account observer read APIs, a separate private PostgreSQL cluster,
the actual authenticated review HTTP API and the pinned real TypeSafe worker.
Old daily bars are research context only and never manufactured into live prints.
"""

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import JEV_MODEL
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

SOURCES = {
    "AAPL": {
        "url": "https://www.apple.com/newsroom/2026/09/"
        "get-ready-to-experience-iphone-18-pro-the-new-apple-watch-lineup-and-airpods-5/",
        "excerpt": "will be available in stores and online worldwide beginning Friday, "
        "September 18.",
        "publication_date": "2026-09-11",
        "catalyst": "PRODUCT",
        "thesis": "Apple's announced product availability supports a product-cycle watchlist "
        "hypothesis. Incremental demand or a sales surprise is not established by this "
        "previously disclosed schedule. No new sales evidence is supplied.",
        "economic_relationship": "Apple sells the products described in its own release.",
    },
    "NVDA": {
        "url": "https://nvidianews.nvidia.com/news/"
        "nvidia-expands-ai-infrastructure-capacity-in-partnership-with-australias-data-center-ecosystem",
        "excerpt": "The participating providers will operate the AI factories, with NVIDIA "
        "delivering the DSX platform, accelerated computing, networking, software and ecosystem "
        "support.",
        "publication_date": "2026-09-09",
        "catalyst": "PARTNERSHIP",
        "thesis": "Australian infrastructure expansion could support NVIDIA platform demand. "
        "The previously disclosed partnership alone does not establish incremental booked "
        "revenue or an unpriced near-term surprise; no new order evidence is supplied.",
        "economic_relationship": "NVIDIA supplies the platform and computing ecosystem; "
        "partners operate the factories. Announced capacity is not booked revenue.",
    },
    "LEN": {
        "url": "https://newsroom.lennar.com/2026-09-16-Lennar-Reports-Third-Quarter-2026-Results",
        "excerpt": "New orders decreased 9%, to 20,879 homes, compared to prior year",
        "publication_date": "2026-09-16",
        "catalyst": "EARNINGS",
        "thesis": "A post-earnings long reversal is only a research hypothesis. Declining "
        "orders weaken the long case, and no evidence of a demand turnaround is supplied. "
        "A price decline alone does not establish undervaluation or a recovery catalyst.",
        "economic_relationship": "Lennar builds and sells the homes in its own results.",
    },
}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(json_safe(data), f, indent=2, sort_keys=True)
        f.write("\n")


def broker_observer_snapshot(owner_root):
    # Authentication remains in memory and is never included in exported observations.
    token = (owner_root / "muse-token").read_text().strip()
    with httpx.Client(
        base_url="http://127.0.0.1:8765",
        trust_env=False,
        timeout=5,
        headers={"Authorization": "Bearer " + token},
    ) as client:
        data = {}
        for name in ("market-data", "execution", "risk"):
            response = client.get("/api/v1/" + name)
            response.raise_for_status()
            data[name] = response.json()
    recon = data["execution"]["last_reconciliation"]
    if not recon["clean"] or recon["discrepancies"]:
        raise RuntimeError("BROKER_RECONCILIATION_NOT_CLEAN")
    age = (datetime.now(UTC) - datetime.fromisoformat(recon["completed_at"])).total_seconds()
    if not 0 <= age <= 60:
        raise RuntimeError("BROKER_RECONCILIATION_STALE")
    if (
        recon["broker_snapshot"]["positions"]
        or recon["broker_snapshot"]["orders"]
        or data["execution"]["positions"]
        or data["risk"]["reservations"]
    ):
        raise RuntimeError("EXPOSURE_NOT_ZERO")
    if data["market-data"]["session_open"]:
        raise RuntimeError("THIS_RUNNER_IS_CLOSED_SESSION_RESEARCH_ONLY")
    return data


def fetch(symbol):
    definition = SOURCES[symbol]
    with httpx.Client(
        timeout=20, follow_redirects=True, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        response = client.get(definition["url"])
        response.raise_for_status()
        retrieved = datetime.now(UTC).isoformat()
        parser = PageText()
        parser.feed(response.text)
        text = re.sub(r"\s+", " ", " ".join(parser.parts))
        if definition["excerpt"] not in text:
            raise RuntimeError("ORIGINAL_SOURCE_EXCERPT_NOT_FOUND_" + symbol)
        source_hash = hashlib.sha256(response.content).hexdigest()
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
        response = client.get(url, params={"range": "1mo", "interval": "1d"})
        response.raise_for_status()
        market_retrieved = datetime.now(UTC).isoformat()
        chart = response.json()["chart"]["result"][0]
        quotes = chart["indicators"]["quote"][0]
        bars = [
            {
                "timestamp": datetime.fromtimestamp(stamp, UTC).isoformat(),
                **{k: values[index] for k, values in quotes.items()},
            }
            for index, stamp in enumerate(chart["timestamp"])
        ]
        if any(row.get(k) is None for row in bars for k in ("close", "high", "low")):
            raise RuntimeError("INCOMPLETE_RESEARCH_BARS_" + symbol)

        # Explicit analyst reference geometry, not a new coded strategy or a live signal.
        # Do not move targets/stops to force the frozen reward/risk test to pass.
        def cents(value):
            return Decimal(str(value)).quantize(Decimal("0.01"))

        last = bars[-1]
        trigger = cents(last["close"])
        maximum = (trigger * Decimal("1.0015")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        stop, target = cents(last["low"]), cents(max(row["high"] for row in bars))
        levels = {
            "entry_trigger": str(trigger),
            "max_entry_price": str(maximum),
            "stop": str(stop),
            "target": str(target),
        }
        geometry = {
            "method": "ANALYST_REFERENCE_ONLY_LAST_CLOSE_LAST_LOW_OBSERVED_MONTH_HIGH",
            "maximum_buffer": "0.15 percent, rounded up to cents",
            "levels": levels,
            "reward_risk_at_M": str((target - maximum) / (maximum - stop)),
            "levels_valid": stop < trigger <= maximum < target,
            "minimum_2R_passes": target - maximum >= 2 * (maximum - stop),
            "execution_eligible": False,
            "reason": "SESSION_CLOSED_AND_NO_CURRENT_ALPACA_QUOTE_OR_PRINT",
        }
        market_excerpt = json.dumps(
            {
                "provider": "YAHOO_FINANCE",
                "symbol": symbol,
                "granularity": "1d",
                "last_completed_bar": last,
                "observed_month_high": max(row["high"] for row in bars),
            },
            separators=(",", ":"),
        )
        item = {
            "signal_id": "TEST-REAL-WORLD-2026-09-19-" + symbol,
            "symbol": symbol,
            "direction": "LONG",
            "catalyst": definition["catalyst"],
            "thesis": definition["thesis"],
            "disproof": "Conditional future disproof: an original-source correction "
            "refutes the stated economic relationship or development. This is not "
            "a claim that such a correction has occurred.",
            "economic_relationship": definition["economic_relationship"],
            "levels": levels,
            "sources": [
                {
                    "source_id": "issuer-original",
                    "url": definition["url"],
                    "excerpt": definition["excerpt"],
                    "retrieved_at": retrieved,
                    "published_at": None,
                },
                {
                    "source_id": "historical-daily-bars",
                    "url": url,
                    "excerpt": market_excerpt,
                    "retrieved_at": market_retrieved,
                    "published_at": last["timestamp"],
                },
            ],
        }
        return item, {
            "symbol": symbol,
            "source_url": definition["url"],
            "source_publication_date": definition["publication_date"],
            "publication_time_unknown": True,
            "source_retrieved_at": retrieved,
            "source_response_sha256": source_hash,
            "market_url": url,
            "market_params": {"range": "1mo", "interval": "1d"},
            "market_retrieved_at": market_retrieved,
            "bars": bars,
            "market_response_sha256": hashlib.sha256(response.content).hexdigest(),
            "geometry": geometry,
        }


async def run(args):
    typesafe_key()  # Approved mode-0600 dotenv loader; never logged or exported.
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    root = Path.home() / ".local/share/catalyst-retest-lab/real-world-2026-09-19"
    if root.exists():
        raise RuntimeError("SESSION_ALREADY_EXISTS_NO_REVIEW_REROLL")
    owner = Path.home() / ".local/share/catalyst-retest-lab/runtime"
    before = broker_observer_snapshot(owner)
    save(out / "broker-before.json", before)
    owner_repo = Repository(localdb.connection_url(owner))
    with owner_repo.connect() as conn:
        calendar = conn.execute(
            "SELECT * FROM lab.current_exchange_sessions WHERE opens_at>clock_timestamp() "
            "ORDER BY session_date LIMIT 1"
        ).fetchone()
        schema = conn.execute(
            "SELECT max(version) AS version FROM lab.schema_migrations"
        ).fetchone()
    save(
        out / "owner-preflight.json",
        {
            "audit": verify_events(owner_repo.export_events()),
            "next_session": calendar,
            "schema": schema,
            "owner_database_migrated": False,
        },
    )
    localdb.start(root)
    settings = ReviewSettings(
        localdb.connection_url(root, "catalyst_review"),
        secrets.token_urlsafe(40),
        secrets.token_urlsafe(40),
        "local",
        Gate1Inputs(APPROVED_GATE1),
        "MUSE_JEV_ACTIVE_V1",
    )
    # UI read credential is separate from the provider secret; never shown in screenshots.
    fd = os.open(root / "read-token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(settings.read_token)
    reports = ResearchReports(settings.database_url, settings.gate1)
    worker = ReviewWorker(
        WorkerSettings(
            localdb.connection_url(root, "catalyst_jev"),
            settings.gate1,
            "real-world-supervised-local",
            3,
            0.1,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_review_app(settings),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("REVIEW_HTTP_STARTUP_FAILED")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            evidence = list(pool.map(fetch, SOURCES))
        now, rid = datetime.now(UTC), uuid4()
        packet = {
            "submission_id": str(uuid4()),
            "report_id": str(rid),
            "report_key": "TEST-REAL-WORLD-2026-09-19",
            "revision": 1,
            "market": "US_STOCKS",
            "timeframe": "CLOSED_SESSION_RESEARCH",
            "generated_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=60)).isoformat(),
            "items": [item for item, _ in evidence],
        }
        save(out / "sources-and-prices.json", [source for _, source in evidence])
        save(out / "submitted-report.json", packet)
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False) as client:
            response = client.post(
                "/api/v1/research-reports",
                json=packet,
                headers={"Authorization": "Bearer " + settings.write_token},
            )
            if response.status_code != 201:
                raise RuntimeError("RESEARCH_HTTP_INTAKE_REJECTED")
            save(out / "intake.json", response.json())
            for _ in range(100):
                await worker.tick()
                report = reports.report(rid)
                if all(item["recorded_disposition"] for item in report["items"]):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("REVIEW_DEADLINE_NOT_RESOLVED")
            response = client.get(
                "/api/v1/research-reports/" + str(rid),
                headers={"Authorization": "Bearer " + settings.read_token},
            )
            response.raise_for_status()
            save(out / "review-result.json", response.json())
        worker.runtime.heartbeat("STOPPED")
        print(
            json.dumps(
                {
                    "phase": "REAL_REVIEW_COMPLETE",
                    "model": JEV_MODEL,
                    "items": [
                        {
                            "symbol": x["symbol"],
                            "decision": x["recorded_disposition"],
                            "reason": x["reason"],
                        }
                        for x in report["items"]
                    ],
                    "ui": f"http://127.0.0.1:{args.port}",
                    "broker_mutations": 0,
                }
            ),
            flush=True,
        )
        # Bounded observation covers two normal 45-second reconciliations and expiry.
        end = time.monotonic() + args.observe_seconds
        while time.monotonic() < end:
            await asyncio.sleep(min(5, end - time.monotonic()))
        after = broker_observer_snapshot(owner)
        save(out / "broker-after.json", after)
        save(out / "expired-readback.json", reports.report(rid))
        save(out / "durable-outputs.json", reports.outputs(0, 200))
        store = worker.store
        with store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts ORDER BY event_seq"
            ).fetchall()
        with Repository(localdb.connection_url(root)).connect() as conn:
            counts = {
                t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                for t in ("candidates", "orders", "fills", "risk_decisions")
            }
        events = Repository(localdb.connection_url(root)).export_events()
        save(out / "audit.json", events)
        with owner_repo.connect() as conn:
            reconciliations = conn.execute(
                "SELECT * FROM lab.reconciliation_runs WHERE completed_at >= %s ORDER BY event_seq",
                (before["execution"]["last_reconciliation"]["completed_at"],),
            ).fetchall()
        summary = {
            "completed_at": datetime.now(UTC).isoformat(),
            "mode": "REAL_SOURCE_REAL_JEV_CLOSED_SESSION",
            "model": JEV_MODEL,
            "cohort": "JEV_ENGINEERING_TEST",
            "database_directory": str(root),
            "source_count": 6,
            "report_id": str(rid),
            "broker_mutations": 0,
            "execution_counts": counts,
            "provider_latency": store.metrics("ENGINEERING_TEST"),
            "receipt_verifications": [store.verify(row["receipt_id"]) for row in receipts],
            "audit": verify_events(events),
            "owner_audit_after": verify_events(owner_repo.export_events()),
            "next_session": calendar,
            "reconciliations": reconciliations,
            "no_quote_or_trade_fabrication": True,
            "owner_database_migrated": False,
            "no_active_trade_to_monitor": True,
        }
        assert summary["audit"]["valid"] and not any(counts.values())
        assert all(item["status"] != "SELECTED" for item in reports.report(rid)["items"])
        save(out / "summary.json", summary)
        print(json.dumps(json_safe(summary)), flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        localdb.stop(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--observe-seconds", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 90 <= args.observe_seconds <= 300:
        raise SystemExit("Observation must be explicitly bounded to 90–300 seconds")
    asyncio.run(run(args))
