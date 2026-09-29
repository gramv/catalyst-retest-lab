"""Supervised engineering proof in a new private DB. Never starts a broker or worker.

Run: ./run python scripts/step4_local_proof.py --provider fixture
Use --provider typesafe only with the owner's supported local credential loader.
"""

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.domain import Evidence, Policy
from catalyst_lab.jev_contract import SKEPTIC
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.jev_store import JevStore
from catalyst_lab.repository import Repository
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_storage import bound_review_args


async def perform(provider, case="incomplete_disclosure"):
    # A fixture key is only dependency injection for this isolated engineering harness.
    # It never masquerades as successful provider authentication.
    key_provider = typesafe_key if provider == "typesafe" else lambda: "fixture-no-provider-call"
    key_provider()  # Real provider mode fails before creating anything if the key is absent.
    root = Path(tempfile.mkdtemp(prefix="catalyst-step4-proof-", dir="/tmp"))
    localdb.start(root)
    report = {
        "provider": provider,
        "engineering_case": case,
        "engineering_only": True,
        "market_data": "SYNTHETIC_LAB_FIXTURE",
        "network_broker_requests": 0,
        "strategy_version": "CATALYST_RETEST_V1",
        "cohort": "JEV_ENGINEERING_TEST",
        "railway_deployment": "DEFERRED_BY_OWNER",
        "steps": [],
    }
    try:
        repo = Repository(localdb.connection_url(root))
        now = datetime.now(UTC)
        session = now.astimezone(ZoneInfo("America/New_York")).date()
        raw = {
            "strategy_version": "CATALYST_RETEST_V1",
            "signal_id": "TEST-STEP4-" + uuid4().hex,
            "market": "US",
            "ticker": "LAB",
            "entry_trigger": "100",
            "max_entry_price": "100.15",
            "stop": "99",
            "target": "103",
            "catalyst": "ENGINEERING_TEST",
            "thesis": "Engineering fixture only: a fictional issuer announced a product.",
            "disproof": "The fictional source does not support the stated claim.",
        }
        market = Evidence(
            "LAB_FIXTURE",
            now,
            session,
            now - timedelta(hours=1),
            now + timedelta(hours=1),
            "LAB_FIXTURE",
            "LAB",
            "us_equity",
            True,
            "LAB_FIXTURE",
            "IEX_FIXTURE",
            now,
            D("99.99"),
            D("100.01"),
            D("100000000"),
            True,
            session,
            False,
            "LAB_FIXTURE",
            "LAB_FIXTURE",
            frozenset(),
            frozenset(),
            D("10000"),
            D("10000"),
            D("0"),
            D("0"),
            D("0"),
            False,
        )
        candidate = repo.submit(
            raw, now, lambda c, n: market, Policy(D("5"), D("10"), D("20000000"), D("5"))
        )
        candidate_id = candidate["candidate_id"]
        report["candidate_id"] = candidate_id
        report["steps"].append(
            {
                "step": "EXISTING_CANDIDATE_FIXTURE",
                "status": candidate["state"],
                "broker_order": False,
            }
        )
        write = "fixture-write-" + uuid4().hex
        read = "fixture-read-" + uuid4().hex
        settings = ReviewSettings(
            localdb.connection_url(root, "catalyst_review"),
            write,
            read,
            "local",
            Gate1Inputs(APPROVED_GATE1),
            "MUSE_JEV_ACTIVE_V1",
        )
        with TestClient(create_review_app(settings, credential_provider=key_provider)) as client:
            writer = {"Authorization": f"Bearer {write}"}
            reader = {"Authorization": f"Bearer {read}"}
            report["health"] = client.get("/health").json()
            evidence = {
                "candidate_id": candidate_id,
                "revision": 1,
                "sources": [
                    {
                        "source_id": "fixture-release",
                        "url": "https://example.com/engineering-fixture",
                        "excerpt": (
                            "ENGINEERING FIXTURE, NOT REAL NEWS: "
                            "Fictional LAB explicitly withdrew its product announcement. "
                            "The release says the product will not launch and all customer "
                            "orders were canceled. The claimed launch is refuted."
                        ) if case == "source_withdrawal" else (
                            "ENGINEERING FIXTURE, NOT REAL NEWS: "
                            "Fictional LAB announced a product. "
                            "No revenue, orders, guidance or economic benefit was disclosed."
                        ),
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "published_at": None,
                    }
                ],
            }
            response = client.post("/api/v1/evidence-bundles", json=evidence, headers=writer)
            response.raise_for_status()
            bundle = response.json()
            report["steps"].append(
                {"step": "MUSE_EVIDENCE_POST", "http_status": response.status_code, **bundle}
            )
            store = JevStore(localdb.connection_url(root, "catalyst_jev"))
            args = bound_review_args(
                store, bundle["bundle_hash"], request_id=uuid4(), question_set=SKEPTIC
            )
            args["identity"]["provider_evidence"] = (
                "REAL_TYPESAFE_ENGINEERING_CALL"
                if provider == "typesafe"
                else "SYNTHETIC_NO_NETWORK"
            )
            fixture_answers = {
                name: {
                    "type": "choice",
                    "choice": "REJECT" if name == "verdict" else "Insufficient evidence",
                    "confidence": 1.0,
                    "probabilities": {
                        option: float(
                            option == ("REJECT" if name == "verdict" else "Insufficient evidence")
                        )
                        for option in question["criteria"]
                    },
                }
                for name, question in SKEPTIC.questions.items()
            }
            transport = (
                None
                if provider == "typesafe"
                else httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "model": "jev-1.13.0",
                            "answers": fixture_answers,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    )
                )
            )
            adapter = JevReviewer(
                store,
                ReliabilityPolicy("ENGINEERING_LOCAL_PROOF_V1", 10, 1, 0.25, 3, 30),
                transport=transport,
                key_provider=key_provider,
            )
            review = await adapter.jev_review(**args)
            with store.connect() as conn:
                decision = conn.execute(
                    "SELECT decision_id,answer_json FROM lab.ai_decisions "
                    "WHERE receipt_id=%s AND question=%s",
                    (review.receipt_ids[-1], "verdict"),
                ).fetchone()
            verification = store.verify(review.receipt_ids[-1])
            report["steps"].append(
                {
                    "step": "JEV_REVIEW",
                    "status": review.status,
                    "provider_evidence": args["identity"]["provider_evidence"],
                    "receipt": verification,
                    "verdict": decision["answer_json"]["choice"],
                }
            )
            entry = {
                "intent_id": str(uuid4()),
                "evidence_bundle_hash": bundle["bundle_hash"],
                "ai_decision_id": str(decision["decision_id"]),
                "strategy_version": "CATALYST_RETEST_V1",
            }
            saved = client.post("/api/v1/entry-intents", json=entry, headers=writer)
            saved.raise_for_status()
            report["steps"].append(
                {"step": "MUSE_STORE_INTENT", "http_status": saved.status_code, **saved.json()}
            )
            report["negative_checks"] = {
                "repeat_intent": client.post(
                    "/api/v1/entry-intents", json=entry, headers=writer
                ).status_code,
                "intent_get": client.get("/api/v1/entry-intents", headers=reader).status_code,
                "candidate_post_to_review_service": client.post(
                    "/api/v1/candidates", json={}, headers=writer
                ).status_code,
                "wrong_bundle_binding": client.post(
                    "/api/v1/entry-intents",
                    json={**entry, "intent_id": str(uuid4()), "evidence_bundle_hash": "a" * 64},
                    headers=writer,
                ).status_code,
            }
            observations = client.get(
                "/api/v1/review-observations?cohort=JEV_ENGINEERING_TEST", headers=reader
            ).json()
            report["observations"] = observations
        with repo.connect() as conn:
            report["exposure_check"] = {
                name: conn.execute(f"SELECT count(*) AS n FROM lab.{name}").fetchone()["n"]
                for name in ("orders", "fills", "risk_decisions")
            }
        events = repo.export_events()
        report["audit_chain"] = verify_events(events)
        report["final_candidate_state"] = repo.get_candidate(candidate_id)["state"]
        report["test_scope"] = (
            "In-process HTTP/ASGI + real disposable PostgreSQL; no broker network"
        )
        report["judgments_authorize_entry"] = False
        out = Path(__file__).parents[1] / "docs/step4-local-proof.json"
        out.write_text(json.dumps(report, indent=2, default=str) + "\n")
        export = root / "step4-events.jsonl"
        export.write_text("".join(json.dumps(event) + "\n" for event in events))
        export.chmod(0o600)
        print(
            json.dumps(
                {
                    "report": str(out),
                    "event_export": str(export),
                    "audit_chain": report["audit_chain"],
                    "provider": provider,
                    "exposure_check": report["exposure_check"],
                    "judgments_authorize_entry": False,
                },
                indent=2,
            )
        )
    finally:
        localdb.stop(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["fixture", "typesafe"], required=True)
    parser.add_argument(
        "--case", choices=["incomplete_disclosure", "source_withdrawal"],
        default="incomplete_disclosure",
    )
    args = parser.parse_args()
    asyncio.run(perform(args.provider, args.case))
