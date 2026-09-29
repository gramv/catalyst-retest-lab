"""Supervised report-interface proof. Synthetic market evidence, optional real Jev.

Creates a fresh private PostgreSQL cluster, calls the actual authenticated ASGI routes,
reviews three reports, exports receipts/audit evidence, and stops the cluster. No broker
client, candidate, order or risk engine is constructed. Does not start a background loop.
"""

import argparse
import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import JEV_MODEL, SKEPTIC
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.jev_store import JevStore
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReports, review_report
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app

CASES = (
    (
        "SUPPORTED",
        "The release supports a new paid commercial product launch.",
        "FICTIONAL ENGINEERING EVIDENCE: The issuer announces its first commercial product "
        "launch and signed paying customer contracts. Prior disclosures described a prototype "
        "only and explicitly stated there were no paying customers. The source confirms the "
        "issuer sells and earns revenue from this product. No trading profitability is asserted.",
    ),
    (
        "CONTRADICTED",
        "The product is launching with paid customer orders.",
        "FICTIONAL ENGINEERING EVIDENCE: The issuer has withdrawn the product, canceled all "
        "customer contracts and explicitly says it will not launch. This contradicts the "
        "proposed launch thesis. No trading profitability is asserted.",
    ),
    (
        "INCOMPLETE",
        "A product announcement will generate material new sales.",
        "FICTIONAL ENGINEERING EVIDENCE: An unsourced summary mentions a product. The "
        "original release, prior disclosures, order terms, economic connection and sales "
        "evidence are unavailable. The proposed sales consequence is not established.",
    ),
)


def packet(market, count):
    now, rid = datetime.now(UTC), uuid4()
    items = []
    for index in range(count):
        label, thesis, excerpt = CASES[index % len(CASES)]
        items.append(
            {
                "signal_id": f"TEST-{rid.hex[:8]}-{index}",
                "symbol": f"LAB{index}",
                "direction": "LONG",
                "catalyst": "ENGINEERING_TEST",
                "thesis": thesis,
                "disproof": "Original source refutes the claimed launch or paid contracts.",
                "economic_relationship": "Claimed issuer revenue from its own product.",
                "levels": {
                    "entry_trigger": "100",
                    "max_entry_price": "100.15",
                    "stop": "99",
                    "target": "103",
                },
                "sources": [
                    {
                        "source_id": label,
                        "url": "https://example.com/engineering-fixture",
                        "excerpt": excerpt,
                        "retrieved_at": now.isoformat(),
                        "published_at": None,
                    }
                ],
            }
        )
    return {
        "submission_id": str(uuid4()),
        "report_id": str(rid),
        "report_key": "TEST-LOCAL-REPORT-" + rid.hex,
        "revision": 1,
        "market": market,
        "timeframe": "INTRADAY",
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=30)).isoformat(),
        "items": items,
    }


def fixture_response(request):
    state = json.loads(request.content)["state"]
    case = state["sources"][0]["source_id"]
    labels = {
        "news_stale": "NO",
        "already_priced": "LOW",
        "unsupported_inference": "NO",
        "verdict": "APPROVE",
    }
    if case == "CONTRADICTED":
        labels.update(verdict="REJECT", unsupported_inference="YES")
    elif case == "INCOMPLETE":
        labels = {name: "Insufficient evidence" for name in labels}
    return httpx.Response(
        200,
        json={
            "model": JEV_MODEL,
            "answers": {
                name: {
                    "type": "choice",
                    "choice": labels[name],
                    "confidence": 0.8,
                    "probabilities": {
                        key: float(key == labels[name]) for key in question["criteria"]
                    },
                }
                for name, question in SKEPTIC.questions.items()
            },
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    )


def write_private(path, value):
    content = json.dumps(json_safe(value), indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


async def perform(provider, count):
    credential = typesafe_key if provider == "typesafe" else lambda: "fixture-no-network-credential"
    credential()  # Fail before setup; the value is not printed or included in artifacts.
    root = Path(tempfile.mkdtemp(prefix="catalyst-report-proof-", dir="/tmp"))
    result = {
        "created_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "model": JEV_MODEL,
        "market_evidence": "SYNTHETIC_ENGINEERING_FIXTURE",
        "cohort": "JEV_ENGINEERING_TEST",
        "transport": "AUTHENTICATED_ASGI_TESTCLIENT",
        "execution_enabled": False,
        "authorizes_entry": False,
        "reports": [],
        "reliability_scope": "SUPERVISED_SINGLE_ATTEMPT_NOT_GATE1_WORKER_ACCEPTANCE",
        "database_directory": str(root),
    }
    try:
        localdb.start(root)
        settings = ReviewSettings(
            localdb.connection_url(root, "catalyst_review"),
            "fixture-write-" + uuid4().hex,
            "fixture-read-" + uuid4().hex,
            "local",
            Gate1Inputs(APPROVED_GATE1),
            "MUSE_JEV_ACTIVE_V1",
        )
        reports = ResearchReports(settings.database_url, settings.gate1)
        store = JevStore(localdb.connection_url(root, "catalyst_jev"))
        reviewer = JevReviewer(
            store,
            ReliabilityPolicy("RESEARCH_REPORT_PROOF_V1", 3, 1, 0.25, 3, 30),
            transport=None if provider == "typesafe" else httpx.MockTransport(fixture_response),
            key_provider=credential,
        )
        repo = Repository(localdb.connection_url(root))
        with TestClient(create_review_app(settings, credential_provider=credential)) as client:
            for market in ("US_STOCKS", "CRYPTO", "INDIA"):
                data = packet(market, count)
                url = "/api/v1/research-reports"
                response = client.post(
                    url, json=data, headers={"Authorization": "Bearer " + settings.write_token}
                )
                response.raise_for_status()
                reviewed = await review_report(
                    reports, reviewer, data["report_id"], 1, max_inflight=4
                )
                readback = client.get(
                    url + "/" + data["report_id"],
                    headers={"Authorization": "Bearer " + settings.read_token},
                )
                readback.raise_for_status()
                assert readback.json() == reviewed
                duplicate = client.post(
                    url, json=data, headers={"Authorization": "Bearer " + settings.write_token}
                )
                assert duplicate.status_code == 409
                result["reports"].append(reviewed)
        with store.connect() as conn:
            receipts = conn.execute("SELECT * FROM lab.jev_receipts ORDER BY event_seq").fetchall()
            requests = conn.execute("SELECT * FROM lab.jev_requests ORDER BY event_seq").fetchall()
        result["receipt_verifications"] = [store.verify(r["receipt_id"]) for r in receipts]
        result["latency"] = store.metrics("ENGINEERING_TEST")
        with repo.connect() as conn:
            result["execution_counts"] = {
                table: conn.execute(f"SELECT count(*) AS n FROM lab.{table}").fetchone()["n"]
                for table in ("candidates", "orders", "fills", "risk_decisions")
            }
        assert not any(result["execution_counts"].values())
        events = repo.export_events()
        result["audit"] = verify_events(events)
        assert result["audit"]["valid"]
        # Exact request text and provider response bytes remain available inside the stopped DB.
        # The exported event envelopes also bind those bytes; credentials never enter a receipt.
        write_private(root / "audit-export.json", events)
        result["audit_export"] = str(root / "audit-export.json")
        result["request_count"] = len(requests)
        result["receipt_count"] = len(receipts)
        write_private(root / "proof.json", result)
        return result
    finally:
        if (root / "postgres" / "postmaster.pid").exists():
            localdb.stop(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("fixture", "typesafe"), required=True)
    parser.add_argument("--items-per-market", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.items_per_market <= 30:
        parser.error("items-per-market must be from 1 through 30")
    outcome = asyncio.run(perform(args.provider, args.items_per_market))
    print(
        json.dumps(
            {
                "provider": outcome["provider"],
                "model": outcome["model"],
                "cohort": outcome["cohort"],
                "reports": [
                    {"market": r["report"]["market"], "counts": r["counts"]}
                    for r in outcome["reports"]
                ],
                "latency": outcome["latency"],
                "audit": outcome["audit"],
                "execution_counts": outcome["execution_counts"],
                "proof": str(Path(outcome["database_directory"]) / "proof.json"),
            },
            indent=2,
        )
    )
