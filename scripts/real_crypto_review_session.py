"""Real crypto research + bounded public Alpaca observation; no order capability.

This does not enable a crypto strategy, fabricate fills, or reuse US DAY brackets.
All market requests are unauthenticated GETs to the fixed market-data host.
"""

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from real_world_review_session import PageText, broker_observer_snapshot, save

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import JEV_MODEL
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

DATA = "https://data.alpaca.markets/v1beta3/crypto/us/"
SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD")
SOURCES = {
    "BTC/USD": (
        "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm",
        "Inflation remains elevated. Today's policy action will support a timelier return "
        "to the Committee's 2 percent goal.",
        "2026-09-16",
        "MACRO",
        "Bitcoin is exposed to macro liquidity conditions. Recent policy tightening is "
        "adverse context; the supplied evidence does not establish a fresh bullish Bitcoin "
        "catalyst or show that current momentum will persist.",
        "Indirect macro risk transmission only; the central bank makes no claim about Bitcoin.",
    ),
    "ETH/USD": (
        "https://blog.ethereum.org/2026/09/07/protocol-priorities",
        "Specifications and prototypes are underway, aimed at I*, where decoupled consensus "
        "is the leading headliner candidate.",
        "2026-09-07",
        "PRODUCT",
        "Ethereum's protocol team is developing faster finality. This is prospective "
        "engineering work, not a completed mainnet deployment or evidence of a near-term "
        "ETH demand shock. A long setup needs additional evidence.",
        "ETH is Ethereum's native asset. Engineering plans do not guarantee token demand.",
    ),
    "SOL/USD": (
        "https://solana.com/news/solana-changelog-september-18-2026",
        "Transaction V1 Rent Reduction to 5080 lamports per byte Slot time reduction to 250ms",
        "2026-09-19",
        "PRODUCT",
        "The mainnet changelog reports transaction and slot-time improvements. These may "
        "improve network usability, but the excerpt does not establish new token purchases, "
        "an unanticipated catalyst, or undervaluation.",
        "SOL is the network's native asset; operational improvements alone do not establish "
        "investment returns.",
    ),
}


async def market_snapshot(client):
    result = {"received_at": None, "data_provider": "ALPACA", "data_feed": "CRYPTO_US"}
    for kind in ("quotes", "trades"):
        response = await client.get(DATA + "latest/" + kind, params={"symbols": ",".join(SYMBOLS)})
        response.raise_for_status()
        result[kind] = response.json()[kind]
        result[kind + "_response_sha256"] = hashlib.sha256(response.content).hexdigest()
    now = datetime.now(UTC)
    result["received_at"] = now.isoformat()
    checks = {}
    for symbol in SYMBOLS:
        quote, trade = result["quotes"][symbol], result["trades"][symbol]
        bid, ask = Decimal(str(quote["bp"])), Decimal(str(quote["ap"]))
        if not (0 < bid <= ask):
            raise RuntimeError("INVALID_CRYPTO_QUOTE")
        spread = (ask - bid) / ((ask + bid) / 2) * 10000
        def age(stamp):
            return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
        quote_age, trade_age = age(quote["t"]), age(trade["t"])
        checks[symbol] = {
            "spread_bps": str(spread),
            "quote_age_seconds": quote_age,
            "trade_age_seconds": trade_age,
            "within_US_5s_quote_diagnostic": 0 <= quote_age <= 5,
            "within_US_10bps_spread_diagnostic": spread <= 10,
            "execution_eligible": False,
            "blocker": "CRYPTO_EXECUTION_NOT_IMPLEMENTED",
        }
    result["checks"] = checks
    return result


async def research_packet(client):
    sources = {}
    for symbol, (url, excerpt, publication_date, _, _, _) in SOURCES.items():
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        parser = PageText()
        parser.feed(response.text)
        text = re.sub(r"\s+", " ", " ".join(parser.parts))
        if excerpt not in text:
            raise RuntimeError("SOURCE_EXCERPT_NOT_FOUND_" + symbol.replace("/", "_"))
        sources[symbol] = {
            "source": {
                "source_id": "original-source",
                "url": url,
                "excerpt": excerpt,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "published_at": None,
            },
            "publication_date": publication_date,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
        }
    now = datetime.now(UTC)
    end = now.replace(second=0, microsecond=0)
    response = await client.get(
        DATA + "bars",
        params={
            "symbols": ",".join(SYMBOLS),
            "timeframe": "1Min",
            "start": (end - timedelta(hours=1)).isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
        },
    )
    response.raise_for_status()
    history = response.json()
    if history.get("next_page_token"):
        raise RuntimeError("INCOMPLETE_CRYPTO_RESEARCH_HISTORY")
    snapshot = await market_snapshot(client)
    items, references = [], {}
    for symbol in SYMBOLS:
        # Zero-trade/zero-volume quote-derived bars are not called printed trades.
        bars = [
            b
            for b in history["bars"][symbol]
            if b["n"] > 0
            and b["v"] > 0
            and datetime.fromisoformat(b["t"].replace("Z", "+00:00")) < end
        ]
        if not bars:
            raise RuntimeError("NO_COMPLETED_TRADED_BARS")
        bars.sort(key=lambda b: b["t"])
        levels = {
            "entry_trigger": str(bars[-1]["c"]),
            "max_entry_price": str(max(bars[-1]["c"], snapshot["quotes"][symbol]["ap"])),
            "stop": str(min(b["l"] for b in bars)),
            "target": str(max(b["h"] for b in bars)),
        }
        references[symbol] = {
            "levels": levels,
            "reference_only": True,
            "method": "LAST_TRADED_BAR_CLOSE_CURRENT_ASK_TRADED_HOUR_LOW_HIGH",
            "crypto_strategy_defined": False,
        }
        _, _, _, catalyst, thesis, economic = SOURCES[symbol]
        excerpt = json.dumps(
            {
                "data_provider": "ALPACA",
                "data_feed": "CRYPTO_US",
                "symbol": symbol,
                "quote": snapshot["quotes"][symbol],
                "trade": snapshot["trades"][symbol],
                "last_completed_traded_bar": bars[-1],
                "completed_traded_bars_in_window": len(bars),
            },
            separators=(",", ":"),
        )
        items.append(
            {
                "signal_id": "TEST-CRYPTO-2026-09-19-" + symbol.replace("/", "-"),
                "symbol": symbol,
                "direction": "LONG",
                "catalyst": catalyst,
                "thesis": thesis,
                "disproof": "Conditional disproof: the original source retracts the cited "
                "development or further evidence refutes the economic connection. "
                "No such retraction is asserted here.",
                "economic_relationship": economic,
                "levels": levels,
                "sources": [
                    sources[symbol]["source"],
                    {
                        "source_id": "alpaca-current-market",
                        "url": DATA + "latest/quotes",
                        "excerpt": excerpt,
                        "retrieved_at": snapshot["received_at"],
                        "published_at": None,
                    },
                ],
            }
        )
    now, rid = datetime.now(UTC), uuid4()
    packet = {
        "submission_id": str(uuid4()),
        "report_id": str(rid),
        "report_key": "TEST-CRYPTO-REAL-WORLD-2026-09-19",
        "revision": 1,
        "market": "CRYPTO",
        "timeframe": "LIVE_RESEARCH_OBSERVATION",
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
        "items": items,
    }
    return packet, {"sources": sources, "history": history, "references": references}, snapshot


async def run(args):
    typesafe_key()
    out = args.output.resolve()
    # macOS Unix sockets have a 103-byte path limit including the PostgreSQL filename.
    root = Path.home() / ".local/share/catalyst-retest-lab/crypto-20260919"
    owner = Path.home() / ".local/share/catalyst-retest-lab/runtime"
    if root.exists() and not args.resume_before_intake:
        raise RuntimeError("SESSION_EXISTS_NO_REVIEW_REROLL")
    before = broker_observer_snapshot(owner)
    save(out / "broker-before.json", before)
    localdb.start(root)
    repo = Repository(localdb.connection_url(root))
    repo.check_role()
    if args.resume_before_intake:
        # Resume only a failed harness setup. Never repeat a submitted research packet/vote.
        with Repository(localdb.connection_url(root, "catalyst_jev")).connect() as conn:
            attempted = conn.execute(
                "SELECT (SELECT count(*) FROM lab.research_reports) + "
                "(SELECT count(*) FROM lab.jev_requests) AS n"
            ).fetchone()["n"]
        if attempted:
            localdb.stop(root)
            raise RuntimeError("EXISTING_RESEARCH_CANNOT_BE_RERUN")
    settings = ReviewSettings(
        localdb.connection_url(root, "catalyst_review"),
        secrets.token_urlsafe(40),
        secrets.token_urlsafe(40),
        "local",
        Gate1Inputs(APPROVED_GATE1),
        "MUSE_JEV_ACTIVE_V1",
    )
    # Private JSON contains only the scoped local UI token, never the provider key.
    save(root / "ui-access.json", {"read_token": settings.read_token})
    reports = ResearchReports(settings.database_url, settings.gate1)
    worker = ReviewWorker(
        WorkerSettings(
            localdb.connection_url(root, "catalyst_jev"),
            settings.gate1,
            "crypto-real-world-supervised",
            3,
            0.1,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_review_app(settings),
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    observations = []
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("REVIEW_HTTP_STARTUP_FAILED")
        async with httpx.AsyncClient(
            timeout=15, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            packet, provenance, snapshot = await research_packet(client)
            save(out / "source-provenance.json", provenance)
            save(out / "submitted-report.json", packet)

            def record(observation):
                observations.append(observation)
                with repo.connect() as conn:
                    system_event(
                        repo,
                        conn,
                        "CRYPTO_RESEARCH_MARKET_OBSERVATION",
                        {
                            "record_purpose": "ENGINEERING_TEST",
                            "cohort": "JEV_ENGINEERING_TEST",
                            "authorizes_entry": False,
                            **observation,
                        },
                    )

            record(snapshot)
            response = await client.post(
                f"http://127.0.0.1:{args.port}/api/v1/research-reports",
                json=packet,
                headers={"Authorization": "Bearer " + settings.write_token},
            )
            if response.status_code != 201:
                raise RuntimeError("RESEARCH_HTTP_INTAKE_REJECTED")
            save(out / "intake.json", response.json())
            for _ in range(100):
                await worker.tick()
                report = reports.report(packet["report_id"])
                if all(i["recorded_disposition"] for i in report["items"]):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("REVIEW_DEADLINE_NOT_RESOLVED")
            save(out / "review-result.json", report)
            worker.runtime.heartbeat("STOPPED")
            print(
                json.dumps(
                    {
                        "phase": "REAL_CRYPTO_REVIEW_COMPLETE",
                        "model": JEV_MODEL,
                        "items": [
                            {
                                "symbol": i["symbol"],
                                "decision": i["recorded_disposition"],
                                "reason": i["reason"],
                            }
                            for i in report["items"]
                        ],
                        "broker_mutations": 0,
                    }
                ),
                flush=True,
            )
            end = time.monotonic() + args.observe_seconds
            while time.monotonic() < end:
                await asyncio.sleep(min(5, end - time.monotonic()))
                record(await market_snapshot(client))
        save(out / "market-observations.json", observations)
        save(out / "expired-readback.json", reports.report(packet["report_id"]))
        save(out / "broker-after.json", broker_observer_snapshot(owner))
        store = worker.store
        with store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts ORDER BY event_seq"
            ).fetchall()
        with repo.connect() as conn:
            counts = {
                t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                for t in ("candidates", "orders", "fills", "risk_decisions")
            }
        events = repo.export_events()
        summary = {
            "completed_at": datetime.now(UTC),
            "mode": "REAL_CRYPTO_DATA_REAL_JEV_NO_EXECUTION",
            "model": JEV_MODEL,
            "cohort": "JEV_ENGINEERING_TEST",
            "database_directory": str(root),
            "execution_blocker": "CRYPTO_EXECUTION_NOT_IMPLEMENTED",
            "broker_mutations": 0,
            "execution_counts": counts,
            "provider_latency": store.metrics("ENGINEERING_TEST"),
            "receipt_verifications": [store.verify(r["receipt_id"]) for r in receipts],
            "audit": verify_events(events),
            "snapshot_batches": len(observations),
            "market_observations": len(observations) * len(SYMBOLS),
            "markets": {},
        }
        for symbol in SYMBOLS:
            summary["markets"][symbol] = {
                "first_quote": observations[0]["quotes"][symbol],
                "last_quote": observations[-1]["quotes"][symbol],
                "unique_quote_timestamps": len({o["quotes"][symbol]["t"] for o in observations}),
                "unique_trade_ids": len({o["trades"][symbol]["i"] for o in observations}),
                "stale_quote_observations": sum(
                    not o["checks"][symbol]["within_US_5s_quote_diagnostic"] for o in observations
                ),
            }
        assert summary["audit"]["valid"] and not any(counts.values())
        assert all(r["valid"] for r in summary["receipt_verifications"])
        save(out / "audit.json", events)
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
    parser.add_argument("--resume-before-intake", action="store_true")
    args = parser.parse_args()
    if not 90 <= args.observe_seconds <= 300:
        raise SystemExit("Observation must be bounded to 90–300 seconds")
    asyncio.run(run(args))
