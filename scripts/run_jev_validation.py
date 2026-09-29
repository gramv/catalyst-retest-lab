"""Bounded, pre-registered real-Jev diagnostic; no broker or runtime activation."""

import argparse
import asyncio
import base64
import json
import os
import random
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, SKEPTIC, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.jev_validation import (
    VALIDATION_POLICY,
    compose_diagnostic,
    diagnostic_questions,
    reconcile_references,
    score_results,
    validate_split,
    verify_blind_binding,
)
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_cycle import selection_disposition


def private_write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(json_safe(value), indent=2) + "\n")


async def run(args):
    cases = json.loads(args.cases.read_text())
    validate_split(cases)
    provenance = json.loads(args.review_provenance.read_text())
    independent, reviewed_states = verify_blind_binding(
        cases, args.blind_cases.read_bytes(), args.independent_labels.read_bytes(), provenance,
    )
    references = reconcile_references(cases, independent, reviewed_states=reviewed_states)
    if args.output.exists() or args.database_root.exists():
        raise ValueError("NEW_RUN_DIRECTORIES_REQUIRED_NO_UNCHANGED_VOTE_RERUN")
    args.output.mkdir(parents=True, mode=0o700)
    run_id = str(uuid4())
    arms = ["legacy", "diagnostic"] if args.arm == "both" else [args.arm]
    order = [(arm, case) for case in cases for arm in arms]
    random.Random(8092026).shuffle(order)
    templates = {
        case["case_id"]: {arm: (SKEPTIC if arm == "legacy"
                              else diagnostic_questions(case["state"])) for arm in arms}
        for case in cases
    }
    policy = ReliabilityPolicy("JEV_DIAGNOSTIC_BOUNDED_V1", 10, 1, .25, 3, 30)
    manifest = {
        "run_id": run_id, "frozen_at": datetime.now(UTC), "model": JEV_MODEL,
        "policy": VALIDATION_POLICY, "execution_authority": False,
        "provenance": "SYNTHETIC_ENGINEERING", "human_validated": False,
        "case_count": len(cases), "planned_evaluations": len(order),
        "blind_review_provenance": provenance,
        "references_hash": digest(encoded(references)), "cases_hash": digest(encoded(cases)),
        "reliability_policy": asdict(policy),
        "order": [{"arm": arm, "case_id": c["case_id"]} for arm, c in order],
        "templates": {key: {arm: {"hash": q.template_hash, "questions": q.questions}
                            for arm, q in by_arm.items()} for key, by_arm in templates.items()},
        "evaluation_rules": {
            "no_tuning_after_results": True, "one_evaluation_per_case_and_arm": True,
            "heldout_family_split": True, "no_automatic_promotion": True,
            "disputed_references_excluded_and_counted": True,
            "provider_failures_not_semantic_successes": True,
            "legacy_comparison": "Different original rubric; diagnostic comparison only.",
        },
    }
    private_write(args.output / "manifest.json", manifest)
    private_write(args.output / "references.json", references)
    private_write(args.output / "cases.json", cases)
    results = []
    start = time.monotonic()
    localdb.start(args.database_root)
    try:
        store = JevStore(localdb.connection_url(args.database_root, "catalyst_jev"))
        reviewer = JevReviewer(store, policy)
        for index, (arm, case) in enumerate(order, 1):
            if time.monotonic() - start > 900:
                raise TimeoutError("BOUNDED_VALIDATION_TIME_EXCEEDED")
            while store.circuit_open(datetime.now(UTC), policy.failure_threshold,
                                     policy.cooldown_seconds):
                if time.monotonic() - start > 900:
                    raise TimeoutError("BOUNDED_VALIDATION_TIME_EXCEEDED")
                await asyncio.sleep(1)
            request_id = str(uuid5(UUID(run_id), arm + ":" + case["case_id"]))
            result = await reviewer.jev_review(
                request_id=request_id,
                identity={"validation_run_id": run_id, "case_id": case["case_id"],
                          "arm": arm, "state_hash": digest(encoded(case["state"])),
                          "non_authorizing": True},
                state=case["state"], question_set=templates[case["case_id"]][arm],
                expires_at=datetime.now(UTC) + timedelta(seconds=10),
                purpose="ENGINEERING_TEST",
            )
            valid = bool(result.receipt_ids and result.answers)
            for receipt_id in result.receipt_ids:
                if not store.verify(receipt_id)["valid"]:
                    valid = False
            if arm == "diagnostic":
                decision = compose_diagnostic(case["state"], result.answers, receipt_valid=valid)
                predicted = {"NEEDS_EVIDENCE": INSUFFICIENT}.get(
                    decision["disposition"], decision["disposition"],
                )
            else:
                disposition, reason = selection_disposition(result)
                decision = {"disposition": disposition, "reason": reason,
                            "execution_authority": False}
                predicted = {"APPROVED": "SUPPORTED", "REJECTED": "CONTRADICTED"}.get(
                    disposition, INSUFFICIENT,
                ) if valid else "PROVIDER_FAILURE"
            row = {"case_id": case["case_id"], "arm": arm, "request_id": request_id,
                   "receipt_ids": result.receipt_ids, "provider_valid": valid,
                   "provider_status": result.status, "provider_reason": result.reason,
                   "answers": result.answers, "decision": decision, "predicted_group": predicted}
            results.append(row)
            private_write(args.output / f"result-{index:03}.json", row)
            print(json.dumps({"completed": index, "planned": len(order), "arm": arm,
                              "case_id": case["case_id"], "valid": valid,
                              "result": predicted}), flush=True)
    finally:
        try:
            private_write(args.output / "results.json", results)
            private_write(args.output / "metrics.json", score_results(
                references, results, planned_arms=arms,
            ))
            repo = Repository(localdb.connection_url(args.database_root))
            events = repo.export_events()
            private_write(args.output / "audit-events.json", events)
            private_write(args.output / "audit-checkpoint.json", verify_events(events))
            with repo.connect() as conn:
                receipts = conn.execute("""SELECT q.request_id,q.request_json,
                    r.receipt_id,r.outcome,r.actual_model,r.http_status,r.error_code,
                    r.latency_ms,r.response_hash,r.response_bytes
                    FROM lab.jev_requests q JOIN lab.jev_receipts r USING(request_id)
                    ORDER BY r.event_seq""").fetchall()
                counts = {table: conn.execute("SELECT count(*) AS n FROM lab." + table)
                          .fetchone()["n"] for table in
                          ("orders", "fills", "risk_decisions", "managed_setups",
                           "managed_risk_decisions", "managed_fills")}
            for receipt in receipts:
                raw = receipt.pop("response_bytes")
                receipt["response_bytes_base64"] = (
                    base64.b64encode(bytes(raw)).decode() if raw else None
                )
                try:
                    receipt["response"] = json.loads(bytes(raw)) if raw else None
                except (ValueError, UnicodeDecodeError):
                    receipt["response"] = None
            private_write(args.output / "provider-receipts.json", receipts)
            private_write(args.output / "execution-counts.json", counts)
        finally:
            localdb.stop(args.database_root)
            private_write(args.output / "stopped.json", {
                "at": datetime.now(UTC), "completed": len(results),
                "planned": len(order), "session_stopped": True,
            })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--independent-labels", type=Path, required=True)
    parser.add_argument("--blind-cases", type=Path, required=True)
    parser.add_argument("--review-provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument("--arm", choices=["legacy", "diagnostic", "both"], default="diagnostic")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
