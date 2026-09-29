"""Record Muse's disposition of the completed broad research pass; no model call."""

import json
from datetime import UTC, datetime

from crypto_broad_research import OUT, ROOT
from real_world_review_session import broker_observer_snapshot, save

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.repository import Repository
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs

FOLLOWUPS = {
    "INJ/USD": "Measure usage of the new INJ-denominated integration and compare the "
    "launch with prior disclosures; resolve the inconsistent aggregate price range.",
    "UNI/USD": "Establish whether Arc usage creates additional UNI value capture; "
    "deployment alone is insufficient.",
    "SUI/USD": "Establish the token-economic effect of gasless settlement; user adoption "
    "does not by itself establish incremental SUI demand.",
    "ONDO/USD": "Establish an ONDO-token economic connection to the subsidiary's "
    "distribution access and compare earlier disclosures.",
    "FIL/USD": "Verify governance adoption, actual release/activation and the delta "
    "from previously disclosed incentive plans.",
}


def main():
    prior = json.loads((OUT / "summary.json").read_text())
    report = json.loads((OUT / "review-result.json").read_text())
    localdb.start(ROOT)
    try:
        repo = Repository(localdb.connection_url(ROOT))
        repo.check_role()
        if verify_events(repo.export_events()) != prior["audit"]:
            raise ValueError("CHECKPOINT_CHANGED_OR_ALREADY_CLOSED_OUT")
        decisions = []
        for item in report["items"]:
            symbol = item["symbol"]
            if symbol in FOLLOWUPS:
                action, reason = "RESEARCH_FOLLOWUP_ONLY", FOLLOWUPS[symbol]
            elif item["recorded_disposition"] == "REJECTED":
                action, reason = (
                    "REJECT_CURRENT_PACKET",
                    "Jev rejected this packet; preserve rejection.",
                )
            else:
                action = "REJECT_CURRENT_PACKET"
                reason = (
                    "The supplied case relies on an older disclosure, roadmap, or established "
                    "mechanism without a demonstrated fresh token-demand development. "
                    "Muse rejects this packet; not a claim about all possible trades in the asset."
                )
            decision = {
                "symbol": symbol,
                "item_id": item["item_id"],
                "receipt_id": item["receipt_id"],
                "recorded_jev_disposition": item["recorded_disposition"],
                "muse_action": action,
                "reason": reason,
                "review_owner": "CODEX_ACTING_AS_MUSE",
                "requires_owner_approval": False,
                "authorizes_entry": False,
                "original_report_not_extended": True,
                "record_purpose": "ENGINEERING_TEST",
                "cohort": "JEV_ENGINEERING_TEST",
            }
            with repo.connect() as conn:
                system_event(repo, conn, "MUSE_RESEARCH_DISPOSITION", decision)
            decisions.append(decision)
        reports = ResearchReports(
            localdb.connection_url(ROOT, "catalyst_review"), Gate1Inputs(APPROVED_GATE1)
        )
        expired = reports.report(report["report"]["report_id"])
        assert all(i["status"] == "EXPIRED" for i in expired["items"])
        events = repo.export_events()
        checkpoint = verify_events(events)
        assert checkpoint["valid"]
        broker = broker_observer_snapshot(ROOT.parent / "runtime")
        save(OUT / "muse-dispositions.json", decisions)
        save(OUT / "expired-readback.json", expired)
        save(OUT / "closeout-audit.json", events)
        save(OUT / "broker-closeout.json", broker)
        summary = {
            "completed_at": datetime.now(UTC),
            "previous_audit": prior["audit"],
            "audit": checkpoint,
            "muse_research_followups": list(FOLLOWUPS),
            "approved_research_picks": [],
            "additional_provider_calls": 0,
            "broker_mutations": 0,
            "owner_review_required": False,
            "all_original_reports_expired": True,
        }
        save(OUT / "closeout-summary.json", summary)
        print(
            json.dumps(
                {
                    "audit": checkpoint,
                    "muse_followups": len(FOLLOWUPS),
                    "approved_picks": 0,
                    "broker_mutations": 0,
                }
            )
        )
    finally:
        localdb.stop(ROOT)


if __name__ == "__main__":
    main()
