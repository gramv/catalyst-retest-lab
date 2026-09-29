"""AGENT_SELECTION_RATIONALE_V1 intake, review input and storage.

Fixture evidence only: disposable PostgreSQL, a mock Jev transport and a mock paper
venue. No provider, broker or owner-ledger contact.
"""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import INSUFFICIENT, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.muse_codex import REPORT_SCHEMA
from catalyst_lab.muse_guidelines import (
    MUSE_GUIDELINES,
    MUSE_GUIDELINES_SHA256,
    MUSE_GUIDELINES_V1,
    MUSE_GUIDELINES_V1_SHA256,
    MUSE_GUIDELINES_V2,
    MUSE_GUIDELINES_V2_SHA256,
    MUSE_GUIDELINES_VERSION,
)
from catalyst_lab.muse_reports import RATIONALE_VERSION, ResearchReportRejected, parse_report
from catalyst_lab.muse_worker import MuseSpool, MuseWorker
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle, ResearchThesis
from catalyst_lab.research_dossier import (
    RATIONALE_BUDGET_BYTES,
    STATE_BUDGET_BYTES,
    DossierRejected,
    compile_review_dossier,
    encoded_bytes,
)
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_app import TOKEN, auth
from tests.test_managed_execution import er as er
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import pristine_cluster as pristine_cluster
from tests.test_muse_worker_http_cycle import ClientApi, Provider
from tests.test_research_cycle import NOW, response
from tests.test_research_cycle import runtime as runtime
from tests.test_review_dossier import CONFIDENCE_MARKER, cycle_id_of, item, report

ROUTE = "/api/v1/lab/research-reports"
PREFIX = "items[0].selection_rationale"


def client_for(cycle):
    return TestClient(create_managed_app(
        cycle, cycle.store, api_token=TOKEN, runtime_status=lambda: {},
        report_submit=lambda raw: asyncio.to_thread(cycle.start_report, raw, max_seconds=300),
    ))


def at_limit(value):
    """A rationale with every field exactly at its maximum size."""
    value["claims"] = [
        {"claim_id": f"C{n}", "kind": "CATALYST", "text": "c" * 300,
         "supported_by": {"source_ids": ["issuer"], "bar_ids": []}}
        for n in range(8)
    ]
    value.update(why_now="n" * 500, why_these_levels="l" * 500, why_over_peers="p" * 500,
                 what_would_change_my_mind="m" * 300, known_risks=["r" * 120] * 5)
    value["agent_confidence"]["basis"] = "b" * 200


LIMIT_CASES = {
    "no_claims": (lambda r: r.update(claims=[]), ".claims", "TOO_SHORT"),
    "nine_claims": (
        lambda r: r.update(claims=[{**r["claims"][0], "claim_id": f"C{n}"} for n in range(9)]),
        ".claims", "TOO_LONG",
    ),
    "claim_text": (lambda r: r["claims"][0].update(text="x" * 301), ".claims[0].text",
                   "STRING_TOO_LONG"),
    "claim_kind": (lambda r: r["claims"][0].update(kind="HUNCH"), ".claims[0].kind",
                   "LITERAL_ERROR"),
    "claim_id": (lambda r: r["claims"][0].update(claim_id="C:1"), ".claims[0].claim_id",
                 "STRING_PATTERN_MISMATCH"),
    "duplicate_claim": (lambda r: r["claims"][1].update(claim_id="C1"), "",
                        "DUPLICATE_CLAIM_ID"),
    "why_now": (lambda r: r.update(why_now="x" * 501), ".why_now", "STRING_TOO_LONG"),
    "why_these_levels": (lambda r: r.update(why_these_levels="x" * 501), ".why_these_levels",
                         "STRING_TOO_LONG"),
    "why_over_peers": (lambda r: r.update(why_over_peers="x" * 501), ".why_over_peers",
                       "STRING_TOO_LONG"),
    "change_my_mind": (lambda r: r.update(what_would_change_my_mind="x" * 301),
                       ".what_would_change_my_mind", "STRING_TOO_LONG"),
    "risk_count": (lambda r: r.update(known_risks=["risk"] * 6), ".known_risks", "TOO_LONG"),
    "risk_text": (lambda r: r.update(known_risks=["x" * 121]), ".known_risks[0]",
                  "STRING_TOO_LONG"),
    "confidence_level": (lambda r: r["agent_confidence"].update(level="CERTAIN"),
                         ".agent_confidence.level", "LITERAL_ERROR"),
    "confidence_basis": (lambda r: r["agent_confidence"].update(basis="x" * 201),
                         ".agent_confidence.basis", "STRING_TOO_LONG"),
    "confidence_missing": (lambda r: r.pop("agent_confidence"), ".agent_confidence", "MISSING"),
    "extra_field": (lambda r: r.update(win_probability=0.9), ".win_probability",
                    "EXTRA_FORBIDDEN"),
}


@pytest.mark.parametrize("case", sorted(LIMIT_CASES))
def test_rationale_schema_and_size_limits_reject_the_item_with_field_paths(case):
    mutate, path, code = LIMIT_CASES[case]
    raw = report([item(0, bars=20, rationale_bars=("bar-10",)), item(1)])
    mutate(raw["items"][0]["selection_rationale"])
    first, second = parse_report(raw).items
    assert first.code == "INVALID_RESEARCH_ITEM" and second.code is None
    assert {"path": PREFIX + path, "code": code} in first.errors
    assert all("x" * 120 not in encoded(error) for error in first.errors)  # No echoed values.


def test_rationale_at_every_limit_is_accepted_by_schema_but_not_by_its_byte_ceiling():
    raw = report([item(0)])
    at_limit(raw["items"][0]["selection_rationale"])
    [parsed] = parse_report(raw).items
    assert parsed.code is None
    with pytest.raises(DossierRejected, match="^DOSSIER_OVER_BUDGET$") as caught:
        compile_review_dossier(parsed, now=NOW)
    [error] = caught.value.errors
    assert error["path"] == PREFIX and error["budget_bytes"] == RATIONALE_BUDGET_BYTES
    assert error["bytes"] > RATIONALE_BUDGET_BYTES  # Rejected whole, never shortened.


@pytest.mark.parametrize(
    ("mutate", "path", "code"),
    [
        (lambda i: i["selection_rationale"]["claims"][0]["supported_by"].update(
            source_ids=["missing-source"]),
         ".claims[0].supported_by.source_ids[0]", "RATIONALE_REFERENCE_UNKNOWN"),
        (lambda i: i["selection_rationale"]["claims"][2]["supported_by"].update(
            bar_ids=["bar-99"]),
         ".claims[2].supported_by.bar_ids[0]", "RATIONALE_REFERENCE_UNKNOWN"),
        (lambda i: i.pop("technical_evidence"),
         ".claims[2].supported_by.bar_ids[0]", "RATIONALE_REFERENCE_UNKNOWN"),
        (lambda i: i["selection_rationale"]["claims"][0].update(
            supported_by={"source_ids": [], "bar_ids": []}),
         ".claims[0].supported_by", "CLAIM_SUPPORT_REQUIRED"),
        (lambda i: i["selection_rationale"]["claims"][0]["supported_by"].update(
            source_ids=["issuer", "issuer"]),
         ".claims[0].supported_by.source_ids[1]", "DUPLICATE_RATIONALE_REFERENCE"),
    ],
    ids=["orphan_source", "orphan_bar", "bar_without_observations", "no_citation", "repeat"],
)
def test_orphan_rationale_references_are_422_with_the_field_path(runtime, mutate, path, code):
    cycle, _, calls, _ = runtime
    client = client_for(cycle)
    raw = report([item(0, bars=20, rationale_bars=("bar-10",))])
    mutate(raw["items"][0])
    reply = client.post(ROUTE, json=raw, headers=auth())
    assert reply.status_code == 422
    assert reply.json() == {
        "detail": "SELECTION_RATIONALE_INVALID",
        "item_results": [{
            "index": 0, "signal_id": "TEST-DOSSIER-00", "status": "REJECTED",
            "code": "SELECTION_RATIONALE_INVALID",
            "errors": [{"path": PREFIX + path, "code": code}],
        }],
    }
    assert "missing-source" not in reply.text and "bar-99" not in reply.text
    assert cycle.outputs(cycle_id_of(raw)) == [] and calls == []
    # Beside a valid sibling the same item is rejected alone and the report is recorded.
    raw = report([item(0, bars=20, rationale_bars=("bar-10",)), item(1)])
    mutate(raw["items"][0])
    reply = client.post(ROUTE, json=raw, headers=auth())
    assert reply.status_code == 202
    assert [r["status"] for r in reply.json()["item_results"]] == ["REJECTED", "ACCEPTED"]
    assert reply.json()["item_results"][0]["errors"] == [{"path": PREFIX + path, "code": code}]


def test_jev_state_carries_the_rationale_but_never_the_agent_confidence(runtime):
    cycle, _, calls, _ = runtime
    raw = report([item(0, bars=64, rationale_bars=("bar-61",))])
    submitted = raw["items"][0]["selection_rationale"]
    cycle.start_report(raw, max_seconds=300)
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    [call] = calls
    seen = call["state"]["rationale"]
    assert seen["status"] == "UNVERIFIED_PROPOSER_CLAIMS"
    assert [(c["claim_id"], c["kind"], c["text"]) for c in seen["claims"]] == [
        (c["claim_id"], c["kind"], c["text"]) for c in submitted["claims"]
    ]
    assert seen["claims"][1]["supported_by"] == {"source_ids": ["issuer"], "bar_ids": []}
    for field in ("why_now", "why_these_levels", "why_over_peers",
                  "what_would_change_my_mind", "known_risks"):
        assert seen[field] == submitted[field]
    assert "agent_confidence" not in seen
    assert "agent_confidence" not in encoded(call) and CONFIDENCE_MARKER not in encoded(call)
    events = cycle.outputs(cycle_id_of(raw))
    packet = next(e["body"] for e in events if e["kind"] == "RESEARCH_PACKET")
    manifest = next(e["body"]["manifest"] for e in events if e["kind"] == "RESEARCH_DOSSIER")
    # The packet body keeps the full block, confidence included, for later analytics.
    stored = packet["selection_rationale"]
    assert stored["schema_version"] == RATIONALE_VERSION
    assert stored["agent_confidence"] == submitted["agent_confidence"]
    assert {k: v for k, v in stored.items() if k not in {"schema_version", "claims"}} == {
        k: v for k, v in submitted.items() if k != "claims"
    }
    # The manifest records the rationale's bytes inside its reserved ceiling.
    assert manifest["rationale"]["bytes"] == encoded_bytes(seen) <= RATIONALE_BUDGET_BYTES
    assert manifest["rationale"]["budget_bytes"] == RATIONALE_BUDGET_BYTES
    assert manifest["rationale"]["cited_source_ids"] == ["issuer"]
    assert manifest["rationale"]["cited_bar_ids"] == ["bar-61"]
    assert manifest["sections"]["rationale"]["bytes"] == manifest["rationale"]["bytes"]
    assert manifest["state_bytes"] <= STATE_BUDGET_BYTES


def test_rationale_edit_changes_evidence_hash_and_cannot_reuse_a_report(runtime):
    cycle, _, calls, _ = runtime
    raw = report([item(0)])
    first = cycle.start_report(raw, max_seconds=300)["item_results"][0]["evidence_hash"]
    edited = copy.deepcopy(raw)
    edited["items"][0]["selection_rationale"]["why_now"] += " Revised timing."
    confident = copy.deepcopy(raw)
    confident["items"][0]["selection_rationale"]["agent_confidence"]["level"] = "HIGH"
    for changed in (edited, confident):  # Both are bound by the stored report hash.
        with pytest.raises(ValueError, match="^REPORT_IDEMPOTENCY_CONTENT_MISMATCH$"):
            cycle.start_report(changed, max_seconds=300)
    edited["report_id"], confident["report_id"] = str(uuid4()), str(uuid4())
    second = cycle.start_report(edited, max_seconds=300)["item_results"][0]["evidence_hash"]
    third = cycle.start_report(confident, max_seconds=300)["item_results"][0]["evidence_hash"]
    # A rationale edit is new review input; the confidence alone never is.
    assert second != first and third == first
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    asyncio.run(cycle.tick(cycle_id_of(edited)))
    assert [digest(encoded(call["state"])) for call in calls] == [first, second]


def test_unchanged_rationale_cannot_buy_another_vote(runtime):
    cycle, clock, calls, reply = runtime
    reply[0] = response(unsupported=INSUFFICIENT)
    raw = report([item(0)])
    cycle.start_report(raw, max_seconds=300)
    [decision] = asyncio.run(cycle.tick(cycle_id_of(raw)))
    assert decision["disposition"] == "NEEDS_REVIEW"
    assert cycle.start_report(raw, max_seconds=300)["idempotent_replay"]
    assert asyncio.run(cycle.tick(cycle_id_of(raw))) == [] and len(calls) == 1
    packet = next(e["body"] for e in cycle.outputs(cycle_id_of(raw))
                  if e["kind"] == "RESEARCH_PACKET")
    thesis = ResearchThesis(*(packet["state"][k] for k in (
        "thesis", "disproof", "economic_relationship")))
    clock[0] += timedelta(seconds=1)
    # Unchanged sources and rationale are not new evidence: no revision, no vote.
    with pytest.raises(ValueError, match="^MATERIAL_NEW_SOURCE_EVIDENCE_REQUIRED$"):
        cycle.submit_evidence(cycle_id_of(raw), packet["item_key"], revision=2,
                              sources=packet["state"]["sources"], thesis=thesis,
                              task_id=decision["evidence_task_id"])
    assert len(calls) == 1
    # New source content is a new revision that carries the rationale unchanged.
    source = {**raw["items"][0]["sources"][0], "source_id": "issuer-update",
              "url": "https://issuer.example/update",
              "excerpt": "Issuer confirms first paid deliveries of the product."}
    revised = cycle.submit_evidence(
        cycle_id_of(raw), packet["item_key"], revision=2,
        sources=[*packet["state"]["sources"], source], thesis=thesis,
        task_id=decision["evidence_task_id"],
    )
    assert revised["state"]["rationale"] == packet["state"]["rationale"]
    assert revised["selection_rationale"] == packet["selection_rationale"]
    assert revised["evidence_hash"] != packet["evidence_hash"]
    reply[0] = response()
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    assert len(calls) == 2 and calls[1]["state"] == revised["state"]


LEGACY_REPORT_HASH = "9ac48d4ba6c04d43c89fc4d1fca0eaaf15014ca85c1a46f256854e6864593853"


def legacy_body():
    """A fixed unversioned report whose hash was computed with the pre-1.2 intake code."""
    bars = [
        {"bar_id": f"bar-{i:02}", "started_at": (NOW - timedelta(minutes=21 - i)).isoformat(),
         "open": "105.5", "high": "106.2", "low": "104.5", "close": "105.8",
         "volume": str(1000 + i)}
        for i in range(20)
    ]
    bars[5]["high"], bars[0]["low"], bars[18]["open"] = "111", "104", "106"
    return {
        "report_id": "5b1f3c2e-0d4a-4c1e-9d8b-6f2a7e9c1a01",
        "generated_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
        "items": [{
            "signal_id": "TEST-LEGACY-HASH", "market": "US_STOCKS", "symbol": "TESTA",
            "direction": "LONG", "catalyst": "PRODUCT",
            "thesis": "  New availability supports a retest hypothesis.  ",
            "disproof": "The issuer withdraws the product.",
            "economic_relationship": "The issuer sells the announced product.",
            "technical_analysis": "Observed support and resistance from external bars.",
            "levels": {"entry_trigger": "106", "max_entry_price": "106.10",
                       "stop": "104", "target": "111"},
            "sources": [{"source_id": "issuer", "url": "https://issuer.example/product",
                         "excerpt": "Fixture issuer launches a new product for paying customers.",
                         "published_at": None, "retrieved_at": NOW.isoformat()}],
            "technical_evidence": {
                "schema_version": "MUSE_OBSERVED_TECHNICALS_V1", "provider": "LAB_FIXTURE",
                "venue": "FIXTURE", "feed": "FIXTURE_MINUTES",
                "source_url": "https://market.example/bars", "retrieved_at": NOW.isoformat(),
                "timeframe_seconds": 60, "bars": bars, "quote": None,
                "level_references": {
                    "entry_trigger": {"bar_id": "bar-18", "field": "open", "rationale": "Retest"},
                    "stop": {"bar_id": "bar-00", "field": "low", "rationale": "Support"},
                    "target": {"bar_id": "bar-05", "field": "high", "rationale": "Resistance"},
                },
            },
        }, {
            "signal_id": "TEST-LEGACY-HASH-B", "market": "CRYPTO", "symbol": "BTC/USD",
            "direction": "LONG", "catalyst": "LISTING", "thesis": "B thesis.",
            "disproof": "B disproof.", "economic_relationship": "B link.",
            "technical_analysis": "B analysis.",
            "levels": {"entry_trigger": "106", "max_entry_price": "106.1",
                       "stop": "104", "target": "111"},
            "sources": [{"source_id": "b", "url": "https://issuer.example/b",
                         "excerpt": "B excerpt.", "published_at": None,
                         "retrieved_at": NOW.isoformat()}],
        }],
    }


def test_legacy_report_is_stored_with_null_rationale_and_keeps_its_replay_hash(runtime):
    cycle, _, calls, _ = runtime
    raw = legacy_body()
    # Recorded pre-change reports keep replaying: the canonical form is unchanged.
    assert parse_report(raw).report_hash == LEGACY_REPORT_HASH
    result = cycle.start_report(raw, max_seconds=300)
    assert result["contender_count"] == 2 and result["rejected_count"] == 0
    events = cycle.outputs(raw["report_id"])
    started = next(e["body"] for e in events if e["kind"] == "RESEARCH_STARTED")
    assert started["report_hash"] == LEGACY_REPORT_HASH
    assert started["report_schema_version"] is None and "schema_version" not in started["report"]
    assert all("selection_rationale" not in i for i in started["report"]["items"])
    packets = [e["body"] for e in events if e["kind"] == "RESEARCH_PACKET"]
    assert [p["selection_rationale"] for p in packets] == [None, None]  # Never synthesized.
    assert [p["state"]["rationale"] for p in packets] == [None, None]
    asyncio.run(cycle.tick(raw["report_id"]))
    assert [call["state"]["rationale"] for call in calls] == [None, None]
    assert cycle.start_report(raw, max_seconds=300)["idempotent_replay"]


def test_v2_report_requires_the_rationale_and_legacy_bodies_may_carry_it(runtime):
    cycle, _, _, _ = runtime
    raw = report([item(0), item(1)])
    del raw["items"][1]["selection_rationale"]
    result = cycle.start_report(raw, max_seconds=300)
    assert result["contender_count"] == 1
    assert result["item_results"][1] == {
        "index": 1, "signal_id": "TEST-DOSSIER-01", "status": "REJECTED",
        "code": "SELECTION_RATIONALE_REQUIRED",
        "errors": [{"path": "items[1].selection_rationale",
                    "code": "SELECTION_RATIONALE_REQUIRED"}],
    }
    single = report([item(0, with_rationale=False)])
    with pytest.raises(ResearchReportRejected, match="^SELECTION_RATIONALE_REQUIRED$"):
        cycle.start_report(single, max_seconds=300)
    assert cycle.outputs(cycle_id_of(single)) == []
    legacy = report([item(0)], v2=False)
    cycle.start_report(legacy, max_seconds=300)
    packet = next(e["body"] for e in cycle.outputs(legacy["report_id"])
                  if e["kind"] == "RESEARCH_PACKET")
    assert packet["state"]["rationale"]["claims"] and packet["selection_rationale"]


@pytest.mark.parametrize("field", [
    ("claims", 0, "text"), ("why_now",), ("why_over_peers",), ("known_risks", 0),
    ("agent_confidence", "basis"),
])
def test_credential_like_rationale_text_refuses_the_whole_report(runtime, field):
    cycle, _, calls, _ = runtime
    raw = report([item(0), item(1)])
    target = raw["items"][1]["selection_rationale"]
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = "Ask analyst@example.test for the unpublished figures."
    reply = client_for(cycle).post(ROUTE, json=raw, headers=auth())
    assert reply.status_code == 422
    path = "items[1].selection_rationale" + "".join(
        f"[{key}]" if isinstance(key, int) else f".{key}" for key in field
    )
    assert reply.json() == {
        "detail": "SENSITIVE_EVIDENCE_REJECTED",
        "errors": [{"path": path, "code": "SENSITIVE_EVIDENCE_REJECTED"}],
    }
    assert "analyst@example" not in reply.text
    assert cycle.outputs(cycle_id_of(raw)) == [] and calls == []


def test_privacy_screen_ignores_the_review_cap_for_whole_reports():
    # The whole-report screen reuses jev_review's patterns on bodies far over its cap.
    raw = report([item(n, excerpts=(1200,) * 6) for n in range(3)])
    raw["items"][2]["selection_rationale"]["why_now"] = "Token sk-" + "a" * 24
    with pytest.raises(ResearchReportRejected, match="^SENSITIVE_EVIDENCE_REJECTED$") as caught:
        parse_report(raw)
    assert encoded_bytes(raw) > 3 * 12_000 / 2
    assert caught.value.errors == (
        {"path": "items[2].selection_rationale.why_now", "code": "SENSITIVE_EVIDENCE_REJECTED"},
    )


def test_provider_schema_and_v2_guidelines_require_the_rationale():
    contender = REPORT_SCHEMA["properties"]["items"]["items"]
    assert contender["properties"]["selection_rationale"] == {
        "$ref": "#/$defs/SelectionRationale"
    }
    assert "selection_rationale" in contender["required"]
    definitions = REPORT_SCHEMA["$defs"]
    assert set(definitions["SelectionRationale"]["required"]) == {
        "claims", "why_now", "why_these_levels", "why_over_peers",
        "what_would_change_my_mind", "known_risks", "agent_confidence",
    }
    assert definitions["RationaleClaim"]["properties"]["kind"]["enum"] == [
        "CATALYST", "NOVELTY", "ECONOMIC_LINK", "TECHNICAL", "RISK",
    ]
    assert set(definitions["AgentConfidence"]["required"]) == {"level", "basis"}
    assert MUSE_GUIDELINES_VERSION == "MUSE_RESEARCH_GUIDELINES_V2"
    assert MUSE_GUIDELINES == MUSE_GUIDELINES_V2 and MUSE_GUIDELINES_SHA256 == (
        MUSE_GUIDELINES_V2_SHA256
    )
    assert MUSE_GUIDELINES_V2.startswith(MUSE_GUIDELINES_V1)
    # The recorded V1 hash (docs/MUSE-US-STOCKS-2026-09-20.md) still verifies.
    assert MUSE_GUIDELINES_V1_SHA256 == (
        "0c04bbeae02f363d6aea050d26ced803af9a93d3664bc755b863a2d973166482"
    )
    assert "selection_rationale" in MUSE_GUIDELINES_V2
    assert "agent_confidence" in MUSE_GUIDELINES_V2
    assert f"{STATE_BUDGET_BYTES:,} bytes" in MUSE_GUIDELINES_V2
    assert f"rationale under {RATIONALE_BUDGET_BYTES:,}" in MUSE_GUIDELINES_V2


def test_worker_forwards_provider_rationale_to_jev_unchanged(runtime, tmp_path):
    cycle, now, calls, _ = runtime
    contender = item(0, bars=20, rationale_bars=("bar-10",))
    provider = Provider(now, [{"kind": "report", "reason": None, "items": [contender]}])
    worker = MuseWorker(MuseSpool(tmp_path / "muse.db"), ClientApi(client_for(cycle)),
                        provider, clock=lambda: now[0])
    report_id = worker.prepare_report("US_STOCKS")
    worker.flush()
    assert report_id and not worker.spool.pending()
    asyncio.run(cycle.tick(report_id))
    [call] = calls
    assert call["state"]["rationale"]["why_now"] == contender["selection_rationale"]["why_now"]
    assert CONFIDENCE_MARKER not in encoded(call)


def test_rationale_dossier_packets_pass_admission_sql_and_stay_on_the_setup(mx):
    engine, venue, receipts = mx
    now = venue.now

    def provider(request):
        return httpx.Response(200, json=response())

    reviewer = JevReviewer(
        receipts, ReliabilityPolicy("RATIONALE_FIXTURE", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(provider), key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now,
    )
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    raw = report([
        item(0, bars=64, rationale_bars=("bar-61",), now=now),
        item(1, bars=20, rationale_bars=("bar-10",), now=now, market="CRYPTO"),
    ], now=now)
    cycle.start_report(raw, max_seconds=300)
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    chosen = cycle.approved_packets(cycle_id_of(raw))
    assert len(chosen) == 2
    with engine.repo.connect() as conn:
        assert [
            conn.execute("SELECT lab.managed_review_failure(%s) AS reason",
                         (Jsonb(json_safe(p)),)).fetchone()["reason"]
            for p in chosen
        ] == [None, None]
    crypto = next(p for p in chosen if p["market"] == "CRYPTO")
    with engine.store.transaction() as conn:
        event = system_event(engine.repo, conn, "CLASSIFICATION_IMPORTED",
                             {"ticker": crypto["symbol"], "source": "RATIONALE_FIXTURE"})
        conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                     (event["seq"], crypto["symbol"], crypto["symbol"], crypto["symbol"],
                      "RATIONALE_FIXTURE"))
    setup_id = engine.admit(crypto)
    with engine.repo.connect() as conn:
        record = conn.execute("SELECT record_json FROM lab.managed_setups WHERE setup_id=%s",
                              (setup_id,)).fetchone()["record_json"]
    # Position review (plan 3.2) can read why the trade was taken from the setup record.
    assert record["state"]["rationale"] == crypto["state"]["rationale"]
    assert record["state"]["rationale"]["what_would_change_my_mind"]
    assert record["selection_rationale"]["agent_confidence"]["level"] == "MEDIUM"
    assert record["state"] == crypto["state"]
    assert "agent_confidence" not in encoded(record["state"])
