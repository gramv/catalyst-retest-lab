"""One supervised Muse research follow-up; never an executable crypto proposal.

Retains the prior expired report and appends the analyst's dispositions. Only the
materially expanded SOL source comparison gets another recorded Jev assessment.
There is no broker transport, admission bridge, expiry extension, or reroll path.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from real_crypto_review_session import research_packet
from real_world_review_session import PageText, broker_observer_snapshot, save

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import JEV_MODEL
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

PRIOR_SOURCES = (
    (
        "previous-activation-announcement",
        "https://solana.com/news/solana-changelog-september-10-2026",
        "The network has decided to delay activation to Epoch 1035 to give more time "
        "for users to prepare.",
        "2026-09-10",
    ),
    (
        "earlier-mainnet-progress",
        "https://solana.com/news/solana-changelog-august-27-2026",
        "V1 Transactions are coming soon to Solana.",
        "2026-08-28",
    ),
)
FOLLOWUP = "CRYPTO_MUSE_FOLLOWUP_STARTED"


async def fetch_comparison(client):
    sources, provenance = [], []
    for source_id, url, excerpt, publication_date in PRIOR_SOURCES:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        page = PageText()
        page.feed(response.text)
        content = re.sub(r"\s+", " ", " ".join(page.parts))
        if excerpt not in content:
            raise RuntimeError("PRIOR_DISCLOSURE_EXCERPT_NOT_FOUND")
        source = {
            "source_id": source_id,
            "url": url,
            "excerpt": excerpt,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "published_at": None,
        }
        sources.append(source)
        provenance.append({
            "source": source,
            "publication_date": publication_date,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
        })
    return sources, provenance


async def main():
    typesafe_key()  # Existing credential loader; never export/log the value.
    project = Path(__file__).resolve().parents[1]
    previous_dir = project / "artifacts/crypto-real-world-2026-09-19"
    out = project / "artifacts/crypto-muse-followup-2026-09-19"
    root = Path.home() / ".local/share/catalyst-retest-lab/crypto-20260919"
    owner = Path.home() / ".local/share/catalyst-retest-lab/runtime"
    original = json.loads((previous_dir / "submitted-report.json").read_text())
    prior_checkpoint = json.loads((previous_dir / "summary.json").read_text())["audit"]
    if not (root / "postgres/PG_VERSION").exists():
        raise RuntimeError("EXISTING_RESEARCH_LEDGER_REQUIRED")
    before = broker_observer_snapshot(owner)
    localdb.start(root)  # Isolated research ledger only, never the account runtime DB.
    worker = None
    worker_stopped = False
    try:
        repo = Repository(localdb.connection_url(root))
        repo.check_role()
        start_events = repo.export_events()
        checkpoint = verify_events(start_events)
        if not checkpoint["valid"] or checkpoint != prior_checkpoint:
            raise RuntimeError("LEDGER_CHANGED_OR_FOLLOWUP_ALREADY_ATTEMPTED")
        gate = Gate1Inputs(APPROVED_GATE1)
        reports = ResearchReports(localdb.connection_url(root, "catalyst_review"), gate)
        original_readback = reports.report(original["report_id"])
        if not all(i["status"] == "EXPIRED" for i in original_readback["items"]):
            raise RuntimeError("ORIGINAL_REPORT_NOT_EXPIRED")
        save(out / "broker-before.json", before)
        save(out / "original-expired-readback.json", original_readback)

        def record(kind, payload):
            with repo.connect() as conn:
                system_event(repo, conn, kind, {
                    "record_purpose": "ENGINEERING_TEST",
                    "cohort": "JEV_ENGINEERING_TEST",
                    "authorizes_entry": False,
                    "original_report_id": original["report_id"],
                    "original_valid_until": original["valid_until"],
                    **payload,
                })

        record(FOLLOWUP, {
            "researcher": "CODEX_ACTING_AS_MUSE",
            "reason": "OWNER_REQUESTED_RESEARCHER_FOLLOWUP",
            "original_remains_expired": True,
        })
        # Fetch again to retain exact retrieval times. Previously read pages are not
        # relabeled as fresh, and source changes fail excerpt verification.
        async with httpx.AsyncClient(
            timeout=15, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            additions, comparison_provenance = await fetch_comparison(client)
            packet, provenance, snapshot = await research_packet(client)
        save(out / "source-provenance.json", {
            **provenance, "additional_prior_disclosures": comparison_provenance,
        })
        save(out / "current-market.json", snapshot)
        dispositions = [
            {
                "symbol": "BTC/USD", "muse_decision": "REJECT_RESEARCH",
                "reason": "NO_SUPPORTED_BULLISH_CATALYST",
                "explanation": "The policy statement describes tightening and persistent "
                "inflation. This packet contains no fresh, supported bullish Bitcoin catalyst.",
                "sources": [provenance["sources"]["BTC/USD"]["source"]],
                "review_owner": "CODEX_ACTING_AS_MUSE", "requires_owner_approval": False,
            },
            {
                "symbol": "ETH/USD", "muse_decision": "REJECT_RESEARCH",
                "reason": "ROADMAP_IS_NOT_DEPLOYMENT_OR_DEMAND_EVIDENCE",
                "explanation": "Specifications and prototypes are prospective work; the "
                "packet does not establish a fresh deployment or incremental ETH demand.",
                "sources": [provenance["sources"]["ETH/USD"]["source"]],
                "review_owner": "CODEX_ACTING_AS_MUSE", "requires_owner_approval": False,
            },
        ]
        for decision in dispositions:
            record("MUSE_RESEARCH_DISPOSITION", decision)

        sol = next(item for item in packet["items"] if item["symbol"] == "SOL/USD")
        sol["sources"].extend(additions)
        sol["thesis"] = (
            "The mainnet changelog confirms delivery of a previously announced transaction "
            "upgrade and lists slot-time and rent improvements. Prior disclosures announced "
            "the transaction upgrade and delayed its activation. Delivery is an implementation "
            "milestone for SOL research; the supplied sources do not establish incremental "
            "token demand or that the milestone was a market surprise."
        )
        sol["disproof"] = (
            "A source correction or evidence that the announced mainnet feature is inactive "
            "would refute the implementation claim. No such correction is asserted."
        )
        sol["signal_id"] = "TEST-CRYPTO-POST-EXPIRY-EVIDENCE-SOL-2026-09-19"
        now = datetime.now(UTC)
        packet.update({
            "submission_id": str(uuid4()), "report_id": str(uuid4()),
            "report_key": "TEST-CRYPTO-POST-EXPIRY-EVIDENCE-2026-09-19",
            "generated_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=60)).isoformat(),
            "timeframe": "POST_EXPIRY_RESEARCH_ONLY", "items": [sol],
        })
        # This is a new NON-AUTHORIZING source assessment, not a candidate revision
        # or extension of the old deadline. Preserve the relationship in the ledger.
        record("MUSE_MATERIAL_EVIDENCE_FOLLOWUP", {
            "assessment_report_id": packet["report_id"],
            "symbol": "SOL/USD", "new_source_ids": [s["source_id"] for s in additions],
            "new_source_hashes": [hashlib.sha256(s["excerpt"].encode()).hexdigest()
                                  for s in additions],
            "original_remains_expired": True,
            "execution_blocker": "CRYPTO_EXECUTION_NOT_IMPLEMENTED",
        })
        worker = ReviewWorker(WorkerSettings(
            localdb.connection_url(root, "catalyst_jev"), gate,
            "crypto-muse-evidence-followup", 1, 0.1,
        ))
        save(out / "submitted-report.json", packet)
        intake = reports.submit(packet)
        save(out / "intake.json", intake)
        if not intake["accepted"]:
            raise RuntimeError("FOLLOWUP_INTAKE_REJECTED")
        for _ in range(100):
            await worker.tick()
            report = reports.report(packet["report_id"])
            if all(i["recorded_disposition"] for i in report["items"]):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("FOLLOWUP_REVIEW_DID_NOT_RESOLVE")
        save(out / "review-result.json", report)
        result = report["items"][0]
        dispositions.append({
            "symbol": "SOL/USD", "muse_decision": "NO_TRADE",
            "jev_research_disposition": result["recorded_disposition"],
            "jev_reason": result["reason"],
            "explanation": "Original setup expired. New source assessment is research-only. "
            "Crypto execution/protection is unimplemented; no order can be authorized.",
            "review_owner": "CODEX_ACTING_AS_MUSE", "requires_owner_approval": False,
        })
        record("MUSE_RESEARCH_DISPOSITION", dispositions[-1])
        worker.runtime.heartbeat("STOPPED")
        worker_stopped = True
        with worker.store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts WHERE event_seq>%s ORDER BY event_seq",
                (checkpoint["event_count"],),
            ).fetchall()
        with repo.connect() as conn:
            counts = {t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                      for t in ("candidates", "orders", "fills", "risk_decisions")}
        after = broker_observer_snapshot(owner)
        events = repo.export_events()
        summary = {
            "completed_at": datetime.now(UTC), "model": JEV_MODEL,
            "mode": "REAL_MUSE_EVIDENCE_FOLLOWUP_NO_EXECUTION",
            "research_dispositions": dispositions, "broker_mutations": 0,
            "execution_counts": counts, "original_report_remains_expired": True,
            "receipt_verifications": [worker.store.verify(r["receipt_id"]) for r in receipts],
            "previous_audit": checkpoint, "audit": verify_events(events),
        }
        assert summary["audit"]["valid"] and not any(counts.values())
        assert summary["receipt_verifications"]
        assert all(r["valid"] for r in summary["receipt_verifications"])
        save(out / "muse-dispositions.json", dispositions)
        save(out / "broker-after.json", after)
        save(out / "audit.json", events)
        save(out / "summary.json", summary)
        print(json.dumps(json_safe(summary)), flush=True)
    finally:
        if worker is not None and not worker_stopped:
            worker.runtime.heartbeat("STOPPED")
        localdb.stop(root)


if __name__ == "__main__":
    asyncio.run(main())
