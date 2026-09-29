import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT, SKEPTIC
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.research_cycle import ResearchThesis
from catalyst_lab.scan_sources import AlpacaMarketSource, AlpacaScanSource
from tests.test_managed_app import TOKEN, auth, body
from tests.test_research_cycle import NOW, fixture_asset, response
from tests.test_research_cycle import runtime as runtime


def test_external_report_reaches_jev_without_quotes_bars_or_scanner(runtime, repo, monkeypatch):
    cycle, _, calls, _ = runtime
    monkeypatch.setattr(
        AlpacaScanSource, "collect_and_scan", lambda *_: pytest.fail("No app discovery scanner")
    )
    assert not hasattr(AlpacaMarketSource, "collect_and_scan")
    raw = body()
    result = cycle.start_report(raw, max_seconds=300)
    assert cycle.approved_packets(result["cycle_id"]) == []
    decisions = asyncio.run(cycle.tick(result["cycle_id"]))
    assert len(decisions) == len(calls) == 1
    state = calls[0]["state"]
    assert state["levels"] == raw["items"][0]["levels"]
    assert state["technical_context"]["analysis"] == raw["items"][0]["technical_analysis"]
    assert state["technical_context"]["execution_checks"] == "PENDING_INDEPENDENT_APP_VALIDATION"
    selected = cycle.approved_packets(result["cycle_id"])
    assert len(selected) == 1 and selected[0]["research_origin"] == "EXTERNAL_MUSE"
    assert cycle.reviewer.store.verify(selected[0]["receipt_id"])["valid"]
    assert verify_events(repo.export_events())["valid"]


def test_muse_followup_reaches_jev_and_retains_proposed_levels_and_original_expiry(runtime):
    cycle, clock, calls, reply = runtime
    reply[0] = response(unsupported=INSUFFICIENT)
    started = cycle.start_report(body(), max_seconds=300)
    first = asyncio.run(cycle.tick(started["cycle_id"]))[0]
    assert first["disposition"] == "NEEDS_REVIEW" and first["evidence_tasks"]
    clock[0] += timedelta(seconds=1)
    revised = cycle.submit_evidence(
        started["cycle_id"],
        "US_STOCKS:TESTA",
        revision=2,
        sources=fixture_asset().news,
        thesis=ResearchThesis(
            "New source resolves the revenue relationship.",
            "Issuer withdraws availability.",
            "Issuer sells this product.",
        ),
    )
    reply[0] = response()
    assert asyncio.run(cycle.tick(started["cycle_id"]))[0]["disposition"] == "APPROVED"
    assert len(calls) == 2 and len(cycle.approved_packets(started["cycle_id"])) == 1
    assert revised["expires_at"] == started["expires_at"]
    assert revised["levels"] == body()["items"][0]["levels"]


def test_report_retry_cannot_duplicate_packets_or_model_votes(runtime):
    cycle, _, calls, _ = runtime
    raw = body()
    with ThreadPoolExecutor(max_workers=2) as workers:
        replies = list(workers.map(lambda _: cycle.start_report(raw, max_seconds=300), range(2)))
    assert sorted(r["idempotent_replay"] for r in replies) == [False, True]
    asyncio.run(cycle.tick(raw["report_id"]))
    asyncio.run(cycle.tick(raw["report_id"]))
    assert len(calls) == 1
    assert sum(e["kind"] == "RESEARCH_PACKET" for e in cycle.outputs(raw["report_id"])) == 1


@pytest.mark.parametrize(
    "change",
    [
        "future",
        "stale",
        "expired",
        "future_source",
        "empty",
        "oversize",
        "duplicate",
        "quantity",
        "secret",
    ],
)
def test_invalid_reports_do_not_create_research_events(runtime, change):
    cycle, _, calls, _ = runtime
    raw = body()
    if change == "future":
        raw["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "stale":
        raw["generated_at"] = (NOW - timedelta(seconds=61)).isoformat()
    elif change == "expired":
        raw["generated_at"] = (NOW - timedelta(seconds=2)).isoformat()
        raw["valid_until"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "future_source":
        raw["items"][0]["sources"][0]["retrieved_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "empty":
        raw["items"] = []
    elif change == "oversize":
        raw["items"] *= 31
    elif change == "duplicate":
        raw["items"] *= 2
    elif change == "quantity":
        raw["items"][0]["quantity"] = 1
    else:
        raw["items"][0]["technical_analysis"] = "Email owner@example.test."
    with pytest.raises(ValueError):
        cycle.start_report(raw, max_seconds=300)
    assert cycle.outputs(raw["report_id"]) == [] and calls == []


def test_unknown_publication_time_is_retained_without_fabricated_freshness(runtime):
    cycle, _, calls, _ = runtime
    raw = body()
    raw["items"][0]["sources"][0]["published_at"] = None
    cycle.start_report(raw, max_seconds=300)
    asyncio.run(cycle.tick(raw["report_id"]))
    assert calls[0]["state"]["sources"][0]["published_at"] is None


def test_india_is_reviewed_but_execution_scope_remains_research_only(runtime):
    cycle, _, _, _ = runtime
    raw = body()
    raw["items"][0]["market"] = "INDIA"
    cycle.start_report(raw, max_seconds=300)
    asyncio.run(cycle.tick(raw["report_id"]))
    selected = cycle.approved_packets(raw["report_id"])[0]
    assert selected["execution_scope"] == "RESEARCH_ONLY"
    assert selected["strategy_version"] == "INDIA_RESEARCH_ONLY"


def test_report_deadline_is_bounded_from_generation_not_refreshable_by_delivery(runtime):
    cycle, _, _, _ = runtime
    raw = body()
    raw["generated_at"] = (NOW - timedelta(seconds=10)).isoformat()
    result = cycle.start_report(raw, max_seconds=30)
    assert result["expires_at"] == (NOW + timedelta(seconds=20)).isoformat()


def thirty_items():
    raw = body()
    template = raw["items"][0]
    raw["items"] = [
        {**copy.deepcopy(template), "signal_id": f"TEST-MUSE-{i:02}", "symbol": f"TST{i:02}"}
        for i in range(30)
    ]
    return raw


def http_client(cycle):
    return TestClient(create_managed_app(
        cycle, cycle.store, api_token=TOKEN, runtime_status=lambda: {},
        report_submit=lambda raw: asyncio.to_thread(cycle.start_report, raw, max_seconds=300),
    ))


def test_one_bad_item_of_thirty_leaves_twenty_nine_packets_and_an_indexed_rejection(runtime):
    cycle, _, calls, _ = runtime
    raw = thirty_items()
    raw["items"][17]["quantity"] = 5  # An execution override is refused for that item only.
    reply = http_client(cycle).post("/api/v1/lab/research-reports", json=raw, headers=auth())
    assert reply.status_code == 202
    result = reply.json()
    assert (result["contender_count"], result["rejected_count"], result["submitted_count"]) == (
        29, 1, 30
    )
    assert result["item_results"][17] == {
        "index": 17, "signal_id": "TEST-MUSE-17", "status": "REJECTED",
        "code": "INVALID_RESEARCH_ITEM",
        "errors": [{"path": "items[17].quantity", "code": "EXTRA_FORBIDDEN"}],
    }
    accepted = [r for r in result["item_results"] if r["status"] == "ACCEPTED"]
    assert [r["index"] for r in accepted] == [i for i in range(30) if i != 17]
    events = cycle.outputs(raw["report_id"], limit=1000)
    packets = [e["body"] for e in events if e["kind"] == "RESEARCH_PACKET"]
    assert len(packets) == 29 and "US_STOCKS:TST17" not in {p["item_key"] for p in packets}
    assert [p["rank"] for p in packets] == [i + 1 for i in range(30) if i != 17]
    [rejection] = [e["body"] for e in events if e["kind"] == "RESEARCH_ITEM_REJECTED_AT_INTAKE"]
    assert (rejection["index"], rejection["signal_id"], rejection["code"]) == (
        17, "TEST-MUSE-17", "INVALID_RESEARCH_ITEM"
    )
    started = next(e["body"] for e in events if e["kind"] == "RESEARCH_STARTED")
    # Content that failed validation is kept only as a hash inside the stored report.
    assert started["report"]["items"][17] == {"invalid_item_sha256": rejection["item_sha256"]}
    assert started["item_results"] == result["item_results"]
    replay = cycle.start_report(raw, max_seconds=300)
    assert replay["idempotent_replay"] and replay["item_results"] == result["item_results"]
    asyncio.run(cycle.tick(raw["report_id"]))
    reviewed = [c for c in calls if set(c["questions"]) == set(SKEPTIC.questions)]
    assert sorted(c["state"]["symbol"] for c in reviewed) == [
        f"TST{i:02}" for i in range(30) if i != 17
    ]


def test_envelope_errors_name_field_paths_without_values(runtime):
    cycle, _, calls, _ = runtime
    raw = thirty_items()
    raw["items"].append(copy.deepcopy(raw["items"][0]))
    del raw["valid_until"]
    raw["report_key"] = "SECRETLESS-BUT-UNEXPECTED-VALUE"
    reply = http_client(cycle).post("/api/v1/lab/research-reports", json=raw, headers=auth())
    assert reply.status_code == 422
    assert reply.json() == {"detail": "INVALID_MUSE_REPORT", "errors": [
        {"path": "valid_until", "code": "MISSING"},
        {"path": "items", "code": "TOO_LONG"},
        {"path": "report_key", "code": "EXTRA_FORBIDDEN"},
    ]}
    assert "UNEXPECTED-VALUE" not in reply.text
    with pytest.raises(ValueError, match="^INVALID_MUSE_REPORT$"):
        cycle.start_report({**body(), "items": [{"thesis": float("nan")}]}, max_seconds=300)
    assert cycle.outputs(raw["report_id"]) == [] and calls == []


def test_credential_pattern_refuses_the_whole_report_with_zero_events(runtime):
    cycle, _, calls, _ = runtime
    raw = thirty_items()
    raw["items"][5]["thesis"] = "Vendor key sk-live" + "a1b2c3d4" * 3 + " confirms access."
    reply = http_client(cycle).post("/api/v1/lab/research-reports", json=raw, headers=auth())
    assert reply.status_code == 422
    assert reply.json() == {
        "detail": "SENSITIVE_EVIDENCE_REJECTED",
        "errors": [{"path": "items[5].thesis", "code": "SENSITIVE_EVIDENCE_REJECTED"}],
    }
    assert "a1b2c3d4" not in reply.text
    assert cycle.outputs(raw["report_id"]) == [] and calls == []
    with cycle.repo.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM lab.managed_events WHERE body->>'cycle_id'=%s",
            (raw["report_id"],),
        ).fetchone()["n"] == 0
