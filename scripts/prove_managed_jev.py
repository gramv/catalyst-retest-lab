"""Real Jev calls on clearly labelled synthetic contexts; never a broker request.

OWNER-RUN EVIDENCE. Without --fixture-provider this script sends real requests to the
TypeSafe provider with the owner's credential, loaded through the approved local loader.
It was NOT run against the provider by the agent that wrote it; the owner runs it and
keeps the JSON as gate G4 evidence. Run it from the repository root (it imports fixture
helpers from tests/) with a new, short, isolated cluster directory, for example:

    PYTHONPATH=. ./run python scripts/prove_managed_jev.py \\
        --root /tmp/managed-provider-g4 --output <private dir>/g4-proof.json \\
        --production-calls 30

Always: one SKEPTIC research review and one TRACKING review of the V3 management
dossier that the position monitor builds from a runtime-sized 60-bar window.

--production-calls N (N >= 30; gate G4): N production-sized synthetic V3 contexts, each
built at send time: 64 one-minute bars passed as recent and structural bars, near-limit
thesis and research text, six 1,200-character news items (two adverse), a full
rationale, eight history events, five stop and five target options, alternating US stock
and crypto shapes. Per call it records the state bytes and budget steps, every receipt's
outcome and latency, receipt-binding verification and the bounded decision (never
applied). In total: p50/p95 latency (nearest rank, final-attempt receipt latency of
VALID calls), the overflow count (contexts that could not be compiled within the state
budget or the 12,000-byte cap) and the invalid-response count (every invalid receipt,
including the one further attempt a review makes after an invalid reply). G4 asks for at
least 30 calls with zero overflows and zero invalid responses.

--fixture-provider swaps the provider for a local mock transport and a fixture key (no
credential is read) to check this harness only; its output is labelled
LOCAL_FIXTURE_TRANSPORT and is never provider evidence. In every mode all broker
behaviour is a local fixture: no Alpaca request, no order, no model action applied.
"""

import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, SKEPTIC, encoded
from catalyst_lab.managed_dossier import DOSSIER_VERSION, ContextBudgetUnsatisfiable
from catalyst_lab.managed_review import (
    CONTEXT_VERSION,
    QUESTION_VERSION,
    evaluate_managed_result,
    managed_questions,
    verify_managed_receipts,
)
from catalyst_lab.managed_runtime import engineering_monitor_policy
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings
from tests import managed_dossier_fixtures as fx
from tests.test_managed_execution import mx, observation
from tests.test_position_monitor import context_from_ledger, monitor, opened

MIN_PRODUCTION_CALLS = 30
FIXTURE_KEY = "fixture-no-provider-credential-harness-only"
CREDENTIAL_SLOT = "managed-provider-contract-proof"


def percentile(values, q):
    """Nearest-rank percentile of the observed values; None when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


FIXTURE_CHOICES = ("HOLD", "INTACT", "KEEP", "NO", "LOW", "APPROVE")


def fixture_answers(request):
    """Local harness transport: a consistent HOLD/INTACT/KEEP (or SKEPTIC NO/LOW/APPROVE)."""
    questions = json.loads(request.content)["questions"]
    answers = {}
    for name, question in questions.items():
        chosen = next((k for k in FIXTURE_CHOICES if k in question["criteria"]),
                      next(k for k in question["criteria"] if k != INSUFFICIENT))
        answers[name] = {
            "type": "choice", "choice": chosen, "confidence": 1.0,
            "probabilities": {k: float(k == chosen) for k in question["criteria"]},
        }
    return httpx.Response(200, json={"model": JEV_MODEL, "answers": answers,
                                     "usage": {"input_tokens": 0, "output_tokens": 0}})


def review_worker(database_url, *, fixture=False, transport=None):
    """The Gate 1 worker; real mode loads the owner's key through the approved loader."""
    settings = WorkerSettings(database_url, Gate1Inputs(APPROVED_GATE1), CREDENTIAL_SLOT, 1, 1)
    if not fixture:
        return ReviewWorker(settings)
    return ReviewWorker(
        settings, key_provider=lambda: FIXTURE_KEY,
        transport=transport or httpx.MockTransport(fixture_answers),
    )


async def production_series(worker, calls, *, clock=lambda: datetime.now(UTC),
                            pause_seconds=0.0):
    """Send ``calls`` production-sized synthetic V3 contexts and summarise them (G4).

    Each context is compiled at send time, so its ten-second review deadline is real.
    Answers are checked against the stored receipt bytes and evaluated against a fresh
    synthetic quote; nothing is applied.
    """
    rows, latencies, walls, sizes = [], [], [], []
    outcomes, decisions, steps = Counter(), Counter(), Counter()
    overflow = invalid = verified = sent = 0
    for index in range(calls):
        market = "CRYPTO" if index % 2 else "US"
        now = clock()
        row = {"index": index, "market": market}
        try:
            context = fx.production_context(seed=index, now=now, market=market)
        except ContextBudgetUnsatisfiable as exc:
            overflow += 1
            rows.append({**row, "overflow": exc.code, "smallest_state_bytes": exc.state_bytes})
            continue
        state_bytes = len(encoded(context.state).encode())
        sizes.append(state_bytes)
        steps.update(context.data["manifest"]["budget_steps"])
        worker.heartbeat()
        request_id = str(uuid4())
        started = time.monotonic()
        try:
            result = await worker.reviewer.jev_review(
                request_id=request_id, identity=context.identity, state=context.state,
                question_set=managed_questions(context), expires_at=context.expires_at,
                purpose="ENGINEERING_TEST",
            )
        except ValueError as exc:
            if str(exc) != "EVIDENCE_TOO_LONG":
                raise
            overflow += 1
            rows.append({**row, "overflow": "EVIDENCE_TOO_LONG", "state_bytes": state_bytes})
            continue
        sent += 1
        walls.append((time.monotonic() - started) * 1000)
        checked = verify_managed_receipts(
            worker.store, context, result, expected_request_id=request_id
        )
        with worker.store.connect() as conn:
            receipts = conn.execute(
                """SELECT attempt,outcome,http_status,latency_ms,error_code
                FROM lab.jev_receipts WHERE request_id=%s ORDER BY attempt""",
                (request_id,),
            ).fetchall()
        final = receipts[-1]["outcome"] if receipts else "NO_RECEIPT"
        outcomes[final] += 1
        invalid += sum(r["outcome"] == "INVALID_RESPONSE" for r in receipts)
        verified += checked.status == "RECORDED"
        if final == "VALID":
            latencies.append(float(receipts[-1]["latency_ms"]))
        evaluated_at = clock()
        fresh = replace(fx.snapshot(market=market, seed=index, now=now),
                        quote_at=evaluated_at, snapshot_at=evaluated_at)
        decision = evaluate_managed_result(context, checked, fresh, now=evaluated_at)
        decisions[decision.action] += 1
        rows.append({
            **row, "request_id": request_id, "state_bytes": state_bytes,
            "budget_steps": context.data["manifest"]["budget_steps"],
            "context_hash": context.context_hash, "status": result.status,
            "reason": result.reason, "receipts": receipts,
            "verified": checked.status == "RECORDED", "decision": decision.action,
            "decision_reason": decision.reason, "wall_ms": walls[-1],
        })
        if pause_seconds:
            await asyncio.sleep(pause_seconds)
    return {
        "calls_requested": calls, "calls_sent": sent, "overflow_count": overflow,
        "invalid_response_count": invalid, "verified_receipts": verified,
        "final_outcomes": dict(sorted(outcomes.items())),
        "decisions_not_applied": dict(sorted(decisions.items())),
        "latency_ms": {"basis": "FINAL_ATTEMPT_RECEIPT_LATENCY_OF_VALID_CALLS",
                       "count": len(latencies), "p50": percentile(latencies, 50),
                       "p95": percentile(latencies, 95),
                       "max": max(latencies) if latencies else None},
        "wall_ms": {"p50": percentile(walls, 50), "p95": percentile(walls, 95)},
        "state_bytes": {"min": min(sizes) if sizes else None,
                        "p50": percentile(sizes, 50), "max": max(sizes) if sizes else None},
        "budget_steps": dict(sorted(steps.items())),
        "g4_production_dossier_criteria_met": (
            calls >= MIN_PRODUCTION_CALLS and sent == calls and overflow == 0
            and invalid == 0 and verified == calls
        ),
        "calls": rows,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", type=Path, required=True,
                        help="new, short, isolated managed-provider-* cluster directory")
    parser.add_argument("--output", type=Path, required=True, help="mode-0600 JSON proof")
    parser.add_argument("--production-calls", type=int, default=0,
                        help=f"production-sized V3 calls; 0 or at least {MIN_PRODUCTION_CALLS}")
    parser.add_argument("--pause-seconds", type=float, default=0.25,
                        help="pause between production calls")
    parser.add_argument("--fixture-provider", action="store_true",
                        help="local mock transport and fixture key; harness check only")
    args = parser.parse_args()
    if 0 < args.production_calls < MIN_PRODUCTION_CALLS or args.production_calls < 0:
        raise SystemExit(f"--production-calls must be 0 or at least {MIN_PRODUCTION_CALLS}")
    root = args.root.expanduser().resolve()
    if root.exists() or not root.name.startswith("managed-provider-"):
        raise SystemExit("A new isolated managed-provider-* directory is required")
    localdb.start(root)
    generator = mx.__wrapped__(Repository(localdb.connection_url(root)))
    try:
        components = next(generator)
        engine, venue, reviews = components
        sid = opened(components, "BTC/USD")  # Mock paper HTTP only.
        policy = engineering_monitor_policy()
        watcher, _ = monitor(components, policy=policy)
        quote = observation(components, bid="108", ask="108.01")
        window = fx.monitor_bars(venue.now)
        asyncio.run(watcher.review(
            sid, quote, window, fresh_observation=lambda: quote, structural_bars=window,
        ))
        context = context_from_ledger(engine, sid)
        worker = review_worker(reviews.database_url, fixture=args.fixture_provider)
        worker.heartbeat()
        setup = engine._load(sid)[0]
        research_state = dict(setup["record_json"]["state"])
        research_state["evidence_kind"] = "SYNTHETIC_ENGINEERING_FIXTURE_NOT_A_REAL_OPPORTUNITY"
        calls = []

        async def evaluate():
            for stage, state, identity, questions in (
                ("SKEPTIC", research_state, {"proof_id": str(uuid4())}, SKEPTIC),
                ("TRACKING", context.state, context.identity, managed_questions(context)),
            ):
                worker.heartbeat()
                result = await worker.reviewer.jev_review(
                    request_id=uuid4(),
                    identity=identity,
                    state=state,
                    question_set=questions,
                    expires_at=datetime.now(UTC) + timedelta(seconds=10),
                    purpose="ENGINEERING_TEST",
                )
                for receipt in result.receipt_ids:
                    worker.store.verify(receipt)
                with worker.store.connect() as conn:
                    rows = conn.execute(
                        "SELECT receipt_id,outcome,latency_ms,error_code FROM lab.jev_receipts "
                        "WHERE request_id=%s ORDER BY attempt",
                        (result.request_id,),
                    ).fetchall()
                calls.append(
                    {
                        "stage": stage,
                        "status": result.status,
                        "reason": result.reason,
                        "answers": result.answers,
                        "receipts": rows,
                        "state_bytes": len(encoded(state).encode()),
                    }
                )

        asyncio.run(evaluate())
        series = asyncio.run(production_series(
            worker, args.production_calls, pause_seconds=args.pause_seconds,
        )) if args.production_calls else None
        proof = {
            "proof_kind": "REAL_JEV_SYNTHETIC_CONTEXT_ONLY" if not args.fixture_provider
            else "LOCAL_FIXTURE_TRANSPORT_HARNESS_CHECK_NOT_PROVIDER_EVIDENCE",
            "provider_mode": "LOCAL_FIXTURE_TRANSPORT" if args.fixture_provider
            else "REAL_TYPESAFE_PROVIDER",
            "model": JEV_MODEL,
            "context_version": CONTEXT_VERSION,
            "question_version": QUESTION_VERSION,
            "dossier_version": DOSSIER_VERSION,
            "state_byte_budget": policy.state_byte_budget,
            "external_broker_requests": 0,
            "real_paper_orders": 0,
            "model_actions_applied": False,
            "calls": calls,
            "production_series": series,
            "audit": verify_events(engine.repo.export_events()),
            "created_at": datetime.now(UTC),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(json_safe(proof), indent=2) + "\n")
        os.chmod(args.output, 0o600)
        summary = {k: v for k, v in proof.items() if k != "production_series"}
        if series:
            summary["production_summary"] = {k: v for k, v in series.items() if k != "calls"}
        print(json.dumps(json_safe(summary), indent=2))
    except Exception:
        raise SystemExit(
            "MANAGED_PROVIDER_PROOF_FAILED; no provider error text or credential emitted"
        ) from None
    finally:
        generator.close()
        localdb.stop(root)


if __name__ == "__main__":
    main()
