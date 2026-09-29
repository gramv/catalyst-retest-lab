import asyncio
from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient

from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.muse_worker import MuseSpool, MuseWorker
from tests.test_managed_app import TOKEN, auth, body
from tests.test_research_cycle import NOW, response
from tests.test_research_cycle import runtime as runtime
from tests.test_technical_evidence import evidence


class ClientApi:
    def __init__(self, client):
        self.client = client

    def call(self, method, path, body=None):
        result = self.client.request(method, path, json=body, headers=auth())
        if result.status_code >= 400:
            raise ValueError("MUSE_REQUEST_REJECTED")
        return result.json()


class Provider:
    def __init__(self, now, replies):
        self.now, self.replies, self.calls = now, list(replies), []

    def invoke(self, request, schema=None):
        self.calls.append(request)
        if request["job"] == "market_research":
            self.now[0] += timedelta(seconds=70)
        return self.replies.pop(0)


def test_worker_http_report_followup_rereview_and_restart(runtime, tmp_path):
    cycle, now, jev_calls, jev_reply = runtime
    cycle.policy = replace(cycle.policy, require_technical_evidence=True)
    client = TestClient(
        create_managed_app(
            cycle,
            cycle.store,
            api_token=TOKEN,
            runtime_status=lambda: {},
            report_submit=lambda raw: asyncio.to_thread(cycle.start_report, raw, max_seconds=300),
        )
    )
    contender = body()["items"][0]
    contender["levels"] = {
        "entry_trigger": "100",
        "max_entry_price": "100.1",
        "stop": "95",
        "target": "112",
    }
    technical = evidence()
    technical["retrieved_at"] = NOW.isoformat()
    technical["quote"]["observed_at"] = NOW.isoformat()
    for index, bar in enumerate(technical["bars"]):
        bar["started_at"] = (NOW - timedelta(minutes=20 - index)).isoformat()
    contender["technical_evidence"] = technical

    followup_source = {
        **contender["sources"][0],
        "source_id": "issuer-material-update",
        "url": "https://issuer.example/material-update",
        "excerpt": "Issuer confirms the product is available to paying customers.",
        "published_at": None,
        "retrieved_at": (NOW + timedelta(seconds=70)).isoformat(),
    }
    provider = Provider(
        now,
        [
            {"kind": "report", "reason": None, "items": [contender]},
            {
                "kind": "followup",
                "reason": None,
                "sources": [followup_source],
                "thesis": "The material issuer update supports the retest hypothesis.",
                "disproof": "The issuer withdraws the product.",
                "economic_relationship": "Customers purchase the issuer's product.",
                "technical_facts": None,
            },
        ],
    )
    spool_path = tmp_path / "muse.db"
    worker = MuseWorker(MuseSpool(spool_path), ClientApi(client), provider, clock=lambda: now[0])
    cycle_id = worker.prepare_report("US_STOCKS")
    worker.flush()
    assert cycle_id and not worker.spool.pending()

    jev_reply[0] = response(verdict="NEEDS_REVIEW", unsupported="INSUFFICIENT")
    asyncio.run(cycle.tick(cycle_id))
    worker.poll()
    prepared = worker.spool.pending()[0]["body"]
    worker.flush()
    assert not worker.spool.pending()

    restarted = MuseWorker(
        MuseSpool(spool_path), ClientApi(client), Provider(now, []), clock=lambda: now[0]
    )
    restarted.poll()
    restarted.flush()
    assert prepared
    jev_reply[0] = response()
    asyncio.run(cycle.tick(cycle_id))
    selected = cycle.approved_packets(cycle_id)
    assert len(selected) == 1 and selected[0]["revision"] == 2
    assert all(len(source["content_hash"]) == 64 for source in selected[0]["state"]["sources"])
    assert len(jev_calls) == 2
