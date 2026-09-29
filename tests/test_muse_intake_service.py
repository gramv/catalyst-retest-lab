import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from scripts import serve_muse_intake
from tests.test_managed_app import TOKEN, auth, body
from tests.test_research_cycle import NOW, POLICY
from tests.test_research_cycle import runtime as runtime


def test_separate_intake_feeds_existing_worker_without_loading_provider_or_executor(
    cluster, runtime, monkeypatch
):
    class Clock:
        @staticmethod
        def now(_):
            return NOW

    monkeypatch.setattr(serve_muse_intake, "datetime", Clock)
    token_file = cluster / "api-token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    client = TestClient(serve_muse_intake.build_intake(cluster, 8768, POLICY, 300))
    cycle, _, calls, _ = runtime  # Already-created independent review worker.
    raw = body()
    assert client.post("/api/v1/lab/research-reports", json=raw).status_code == 401
    response = client.post("/api/v1/lab/research-reports", json=raw, headers=auth())
    assert response.status_code == 202 and calls == []
    assert not response.json()["trade_authorized"]
    assert client.post("/api/v1/lab/research-scans", headers=auth(), json={}).status_code == 404
    assert not hasattr(serve_muse_intake.ResearchIntake, "tick")
    assert not hasattr(serve_muse_intake.ResearchIntake, "approved_packets")
    asyncio.run(cycle.tick(raw["report_id"]))
    assert len(calls) == 1 and len(cycle.approved_packets(raw["report_id"])) == 1
    outputs = client.get(response.json()["polling_url"], headers=auth()).json()["items"]
    assert any(e["kind"] == "RESEARCH_SELECTED" for e in outputs)
    assert TOKEN not in json.dumps(outputs)
    with cycle.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_setups").fetchone()["n"] == 0


def test_separate_intake_fails_closed_on_invalid_configuration(cluster):
    (cluster / "api-token").write_text(TOKEN)
    (cluster / "api-token").chmod(0o644)
    with pytest.raises(ValueError, match="PRIVATE_TOKEN_FILE_REQUIRED"):
        serve_muse_intake.build_intake(cluster, 8768, POLICY, 300)
    with pytest.raises(ValueError, match="VALID_LOCAL_WORKER_PORT_REQUIRED"):
        serve_muse_intake.build_intake(cluster, 80, POLICY, 300)
    with pytest.raises(ValueError, match="EXPLICIT_REPORT_DEADLINE_REQUIRED"):
        serve_muse_intake.build_intake(cluster, 8768, POLICY, 0)


def test_http_claim_nullable_evidence_followup_replays_and_rereviews(runtime):
    from catalyst_lab.jev_contract import INSUFFICIENT
    from catalyst_lab.managed_service import create_managed_app
    from catalyst_lab.research_cycle import ResearchIntake
    from tests.test_research_cycle import response

    cycle, now, calls, reply = runtime
    intake = ResearchIntake(cycle.repo, cycle.policy, clock=lambda: now[0])
    client = TestClient(create_managed_app(
        intake, intake.store, api_token=TOKEN, runtime_status=lambda: {},
        report_submit=lambda raw: asyncio.to_thread(intake.start_report, raw, max_seconds=300),
    ))
    report = body()
    report["items"][0]["sources"][0]["published_at"] = None
    result = client.post("/api/v1/lab/research-reports", json=report, headers=auth())
    assert result.status_code == 202
    cycle_id = result.json()["cycle_id"]
    reply[0] = response(verdict="NEEDS_REVIEW", unsupported=INSUFFICIENT)
    asyncio.run(cycle.tick(cycle_id))
    claim_path = f"/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim"
    claim = {"claimant": "fixture-muse", "lease_seconds": 30, "limit": 10}
    assert client.post(claim_path, json=claim).status_code == 401
    task = client.post(claim_path, json=claim, headers=auth()).json()["tasks"][0]
    assert client.post(claim_path, json=claim, headers=auth()).json()["tasks"] == []
    source = {**report["items"][0]["sources"][0],
              "source_id": "material-correction", "url": "https://issuer.example/correction",
              "excerpt": "Fixture source clarifies direct product revenue and limitations."}
    revised = {"task_id": task["task_id"], "item_key": task["item_key"],
               "revision": task["revision"] + 1, "sources": [source],
               "thesis": "Fixture source now provides direct support.",
               "disproof": "The issuer withdraws the product.",
               "economic_relationship": "Documented direct sales."}
    path = f"/api/v1/lab/cycles/{cycle_id}/evidence"
    recorded = client.post(path, json=revised, headers=auth())
    assert recorded.status_code == 200, recorded.text
    assert not recorded.json()["trade_authorized"]
    replay = client.post(path, json=revised, headers=auth())
    assert replay.status_code == 200 and replay.json() == recorded.json()
    reply[0] = response()
    asyncio.run(cycle.tick(cycle_id))
    assert len(calls) == 2 and cycle.approved_packets(cycle_id)[0]["revision"] == 2
