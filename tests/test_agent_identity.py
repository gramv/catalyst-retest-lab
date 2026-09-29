"""Agent identity on research reports, per-agent credentials and attribution (plan 1.3 and
the attribution data of 1.8, minimal form: no registry, no migration, no new route).

Fixture evidence only: disposable PostgreSQL, a mock Jev transport that keeps the exact
outbound request bytes, and a mock paper venue. No provider, broker or owner-ledger contact.
"""

import asyncio
import copy
import json
import sys
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from catalyst_lab import muse_worker, research_dossier
from catalyst_lab.agent_identity import (
    AGENT_CYCLE_NAMESPACE,
    ATTRIBUTED,
    LEGACY_UNATTRIBUTED,
    agent_cycle_id,
)
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import INSUFFICIENT, encoded, strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_app import AppSettings, build_app_from_env
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_funnel import CLAIM_SUPPORT_NOTE, research_funnel
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_SHA256, MUSE_GUIDELINES_VERSION
from catalyst_lab.muse_reports import ResearchReportRejected, parse_report
from catalyst_lab.muse_worker import MuseSpool, MuseWorker
from catalyst_lab.position_news import PositionNewsService
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_ranking import QUALITY
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_analytics import close
from tests.test_managed_execution import er as er
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_managed_execution import pristine_cluster as pristine_cluster
from tests.test_managed_service import FixtureCycle
from tests.test_muse_worker import Api
from tests.test_muse_worker import Provider as ScriptedProvider
from tests.test_research_cycle import NOW, POLICY, quality_response, response
from tests.test_review_dossier import (
    CONFIDENCE_MARKER,
    cycle_id_of,
    fixture_agent,
    item,
    report,
)
from tests.test_selection_rationale import LEGACY_REPORT_HASH, legacy_body

ROUTE = "/api/v1/lab/research-reports"
LEGACY = "fixture-legacy-muse-token-abcdefghijklmnopqrstuvwxyz"
STATUS = "fixture-agent-status-token-abcdefghijklmnopqrstuvwxyz"
OPERATOR = "fixture-agent-operator-token-abcdefghijklmnopqrstuvwx"
AGENTS = {
    "instinct": "fixture-instinct-agent-token-abcdefghijklmnopqrst",
    "grogbot": "fixture-grogbot-agent-token-abcdefghijklmnopqrstu",
}
TOKENS = {"legacy": LEGACY, **AGENTS}


@pytest.fixture
def lab(er):
    """A research cycle on its own disposable database (so no other test's ledger is touched)
    whose mock Jev transport keeps every exact outbound request."""
    now, sent, reply = [NOW], [], [response()]

    async def provider(request):
        sent.append(request)
        questions = strict_json(request.content)["questions"]
        await asyncio.sleep(0)
        quality = set(questions) == set(QUALITY.questions)
        return httpx.Response(200, json=quality_response() if quality else reply[0])

    reviewer = JevReviewer(
        JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev")),
        ReliabilityPolicy("AGENT_IDENTITY_FIXTURE", 10, 1, 0.25, 10000, 30),
        key_provider=lambda: "fixture-no-provider-secret",
        transport=httpx.MockTransport(provider),
        clock=lambda: now[0],
    )
    cycle = ResearchCycle(
        RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk")),
        reviewer, POLICY, clock=lambda: now[0],
    )
    return SimpleNamespace(cycle=cycle, now=now, sent=sent, reply=reply)


def client(cycle, *, separated=True, position_news=None):
    return TestClient(create_managed_app(
        cycle, cycle.store, api_token=LEGACY, runtime_status=lambda: {},
        report_submit=lambda raw: asyncio.to_thread(cycle.start_report, raw, max_seconds=300),
        status_token=STATUS if separated else None,
        operator_token=OPERATOR if separated else None,
        agent_tokens=AGENTS, position_news=position_news,
    ))


def bearer(token):
    return {"Authorization": "Bearer " + token}


def recorded(cycle, cycle_id, kind):
    return [e["body"] for e in cycle.outputs(cycle_id, limit=1000) if e["kind"] == kind]


def requests_of(cycle, cycle_id):
    with cycle.reviewer.store.connect() as conn:
        return conn.execute(
            """SELECT evidence_identity,request_json FROM lab.jev_requests
            WHERE evidence_identity->>'cycle_id'=%s""", (cycle_id,),
        ).fetchall()


def ledger_head(repo):
    with repo.connect() as conn:
        return conn.execute(
            "SELECT coalesce(max(event_seq),0) AS seq FROM lab.managed_events"
        ).fetchone()["seq"]


def test_v2_report_needs_an_agent_block_and_a_legacy_report_may_not_carry_one(lab):
    web = client(lab.cycle)
    missing = report([item(0)])
    del missing["agent"]
    reply = web.post(ROUTE, json=missing, headers=bearer(LEGACY))
    assert reply.status_code == 422
    assert reply.json() == {"detail": "INVALID_MUSE_REPORT",
                            "errors": [{"path": "agent", "code": "AGENT_IDENTITY_REQUIRED"}]}
    with pytest.raises(ResearchReportRejected, match="^INVALID_MUSE_REPORT$"):
        parse_report(missing)  # The intake contract itself, not only the HTTP check.
    legacy = report([item(0)], v2=False)
    legacy["agent"] = fixture_agent()
    reply = web.post(ROUTE, json=legacy, headers=bearer(LEGACY))
    assert reply.status_code == 422
    assert reply.json()["errors"] == [{"path": "agent", "code": "AGENT_IDENTITY_REQUIRES_V2"}]
    for raw in (missing, legacy):
        assert lab.cycle.outputs(raw["report_id"]) == []
        assert lab.cycle.outputs(agent_cycle_id("muse", raw["report_id"])) == []
    assert lab.sent == []


MALFORMED = {
    "uppercase_id": ("agent_id", "Instinct", "STRING_PATTERN_MISMATCH"),
    "one_character_id": ("agent_id", "i", "STRING_PATTERN_MISMATCH"),
    "long_id": ("agent_id", "i" * 33, "STRING_PATTERN_MISMATCH"),
    "digit_first_id": ("agent_id", "9instinct", "STRING_PATTERN_MISMATCH"),
    "spaced_id": ("agent_id", "in stinct", "STRING_PATTERN_MISMATCH"),
    "numeric_id": ("agent_id", 7, "STRING_TYPE"),
    "long_version": ("agent_version", "v" * 33, "STRING_PATTERN_MISMATCH"),
    "spaced_version": ("agent_version", "1.0 beta", "STRING_PATTERN_MISMATCH"),
    "empty_guidelines": ("guidelines_version", "", "STRING_PATTERN_MISMATCH"),
    "short_hash": ("guidelines_sha256", "a" * 63, "STRING_PATTERN_MISMATCH"),
    "uppercase_hash": ("guidelines_sha256", "A" * 64, "STRING_PATTERN_MISMATCH"),
    "run_id": ("run_id", "run-seven", "UUID_PARSING"),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_agent_fields_are_422_before_the_credential_check(lab, case):
    field, value, code = MALFORMED[case]
    raw = report([item(0)], agent={**fixture_agent("instinct"), field: value})
    for token in (AGENTS["instinct"], AGENTS["grogbot"], LEGACY):
        reply = client(lab.cycle).post(ROUTE, json=raw, headers=bearer(token))
        assert reply.status_code == 422
        assert reply.json() == {"detail": "INVALID_MUSE_REPORT",
                                "errors": [{"path": "agent." + field, "code": code}]}
        if isinstance(value, str) and len(value) > 3:
            assert value not in reply.text  # Paths and codes only, never submitted values.
    assert lab.cycle.outputs(agent_cycle_id("instinct", raw["report_id"])) == []
    assert lab.cycle.outputs(raw["report_id"]) == [] and lab.sent == []


@pytest.mark.parametrize(("mutate", "path", "code"), [
    (lambda a: a.update(scope="reports:write"), "agent.scope", "EXTRA_FORBIDDEN"),
    (lambda a: a.pop("run_id"), "agent.run_id", "MISSING"),
    (lambda a: a.pop("guidelines_sha256"), "agent.guidelines_sha256", "MISSING"),
], ids=["extra_key", "missing_run", "missing_hash"])
def test_agent_block_has_exact_keys(lab, mutate, path, code):
    raw = report([item(0)], agent=fixture_agent("instinct"))
    mutate(raw["agent"])
    reply = client(lab.cycle).post(ROUTE, json=raw, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 422 and reply.json()["errors"] == [{"path": path, "code": code}]
    raw["agent"] = "instinct"
    reply = client(lab.cycle).post(ROUTE, json=raw, headers=bearer(AGENTS["instinct"]))
    assert reply.json()["errors"] == [{"path": "agent", "code": "MODEL_TYPE"}]


MISMATCHES = {
    "names_another_agent": ("instinct", "grogbot"),
    "scoped_agent_sends_legacy_body": ("instinct", None),
    "legacy_token_names_an_agent": ("legacy", "instinct"),
    "scoped_agent_claims_to_be_muse": ("grogbot", "muse"),
}


@pytest.mark.parametrize("separated", [True, False], ids=["role_tokens", "single_token"])
@pytest.mark.parametrize("case", sorted(MISMATCHES))
def test_report_agent_must_be_the_credentials_agent(lab, case, separated):
    credential, declared = MISMATCHES[case]
    raw = report([item(0)], v2=declared is not None,
                 agent=fixture_agent(declared) if declared else None)
    reply = client(lab.cycle, separated=separated).post(
        ROUTE, json=raw, headers=bearer(TOKENS[credential])
    )
    assert reply.status_code == 403 and reply.json() == {"detail": "AGENT_IDENTITY_MISMATCH"}
    assert lab.cycle.outputs(cycle_id_of(raw)) == [] and lab.sent == []


def test_legacy_token_and_legacy_body_are_accepted_and_legacy_unattributed(lab):
    before = ledger_head(lab.cycle.repo)
    web = client(lab.cycle)
    raw = report([item(0)], v2=False)
    reply = web.post(ROUTE, json=raw, headers=bearer(LEGACY))
    assert reply.status_code == 202
    body = reply.json()
    # Legacy cycle IDs stay the report ID; the identity is never inferred as Muse.
    assert body["cycle_id"] == raw["report_id"]
    assert (body["agent_id"], body["agent_version"], body["research_origin"]) == (
        None, None, "EXTERNAL_MUSE"
    )
    [started] = recorded(lab.cycle, raw["report_id"], "RESEARCH_STARTED")
    assert started["agent"] is None and started["report_schema_version"] is None
    assert "agent" not in started["report"]
    [stored] = recorded(lab.cycle, raw["report_id"], "RESEARCH_PACKET")
    assert stored["agent"] is None and stored["research_origin"] == "EXTERNAL_MUSE"
    asyncio.run(lab.cycle.tick(raw["report_id"]))
    [request] = requests_of(lab.cycle, raw["report_id"])
    assert not {"agent_id", "agent_version"} & set(request["evidence_identity"])
    [row] = research_funnel(lab.cycle.repo, after=before)["items"]
    assert (row["attribution"], row["agent_id"], row["agent_version"],
            row["guidelines_sha256"]) == (LEGACY_UNATTRIBUTED, None, None, None)
    # The legacy token is also the identity ``muse`` for V2 reports.
    v2 = report([item(0)], agent=fixture_agent("muse", "LEGACY_UNDECLARED"))
    reply = web.post(ROUTE, json=v2, headers=bearer(LEGACY))
    assert reply.status_code == 202 and reply.json()["cycle_id"] == agent_cycle_id(
        "muse", v2["report_id"]
    )
    assert (reply.json()["agent_id"], reply.json()["agent_version"]) == (
        "muse", "LEGACY_UNDECLARED"
    )


def test_two_agents_reusing_a_report_id_get_separate_cycles(lab):
    web = client(lab.cycle)
    report_id = str(uuid4())
    submitted, cycles = {}, {}
    for name in ("instinct", "grogbot"):
        raw = report([item(0)], agent=fixture_agent(name))
        raw["report_id"] = report_id
        reply = web.post(ROUTE, json=raw, headers=bearer(AGENTS[name]))
        assert reply.status_code == 202 and reply.json()["agent_id"] == name
        submitted[name], cycles[name] = raw, reply.json()["cycle_id"]
    legacy = report([item(0)], v2=False)
    legacy["report_id"] = report_id
    legacy_cycle = web.post(ROUTE, json=legacy, headers=bearer(LEGACY)).json()["cycle_id"]
    assert cycles == {name: agent_cycle_id(name, report_id) for name in cycles}
    assert legacy_cycle == report_id and len({*cycles.values(), legacy_cycle}) == 3
    for name, raw in submitted.items():
        [started] = recorded(lab.cycle, cycles[name], "RESEARCH_STARTED")
        assert started["agent"] == raw["agent"]
        # Each agent's retry replays its own cycle; nothing crosses over.
        again = web.post(ROUTE, json=raw, headers=bearer(AGENTS[name])).json()
        assert again["idempotent_replay"] and again["cycle_id"] == cycles[name]
    # Identical content is identical review input whoever proposed it.
    hashes = {recorded(lab.cycle, cycle_id, "RESEARCH_PACKET")[0]["evidence_hash"]
              for cycle_id in (*cycles.values(), legacy_cycle)}
    assert len(hashes) == 1
    # The derivation is pinned: recorded V2 cycle IDs must stay reproducible.
    assert AGENT_CYCLE_NAMESPACE == uuid5(
        NAMESPACE_URL, "urn:catalyst-retest-lab:agent-research-cycle:v1"
    )
    assert agent_cycle_id("instinct", "00000000-0000-4000-8000-000000000001") == (
        "9f9d3864-8db7-5af8-affe-b763f3e98601"
    )


def test_agent_fields_are_recorded_but_never_in_the_state_or_any_jev_request(lab):
    agent = fixture_agent("instinct", "AGENT-VERSION-MARKER-7")
    raw = report([item(i) for i in range(11)], agent=agent)
    reply = client(lab.cycle).post(ROUTE, json=raw, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 202
    cycle_id = reply.json()["cycle_id"]
    asyncio.run(lab.cycle.tick(cycle_id))  # Eleven approvals for ten slots: QUALITY runs too.
    selected = lab.cycle.approved_packets(cycle_id)
    assert len(selected) == 10 and all(p["agent"] == agent for p in selected)
    kinds = Counter(
        "QUALITY" if set(strict_json(r.content)["questions"]) == set(QUALITY.questions)
        else "SKEPTIC" for r in lab.sent
    )
    assert kinds == {"SKEPTIC": 11, "QUALITY": 11}
    [started] = recorded(lab.cycle, cycle_id, "RESEARCH_STARTED")
    assert started["agent"] == agent and started["research_origin"] == "EXTERNAL_RESEARCH_AGENT"
    packets = recorded(lab.cycle, cycle_id, "RESEARCH_PACKET")
    assert len(packets) == 11 and all(p["agent"] == agent for p in packets)
    markers = [agent[k] for k in (
        "agent_id", "agent_version", "guidelines_version", "guidelines_sha256", "run_id"
    )]
    for stored in packets:
        assert "agent" not in stored["state"]
        assert not any(marker in encoded(stored["state"]) for marker in markers)
    for request in lab.sent:  # The exact bytes, URL and headers sent to the provider.
        sent = request.content + str(request.url).encode() + b"".join(
            value for _, value in request.headers.raw
        )
        assert not any(marker.encode() in sent for marker in markers)
        assert b'"agent' not in request.content and CONFIDENCE_MARKER.encode() not in sent
    rows = requests_of(lab.cycle, cycle_id)
    assert len(rows) == 22
    assert {(r["evidence_identity"]["agent_id"], r["evidence_identity"]["agent_version"])
            for r in rows} == {("instinct", "AGENT-VERSION-MARKER-7")}
    assert sum("quality_policy" in r["evidence_identity"] for r in rows) == 11
    assert not any(marker in r["request_json"] for r in rows for marker in markers)


def test_origin_label_is_one_neutral_value_for_every_proposer(lab):
    web = client(lab.cycle)
    states, origins = [], []
    for credential, raw in (
        ("instinct", report([item(0)], agent=fixture_agent("instinct"))),
        ("grogbot", report([item(0)], agent=fixture_agent("grogbot"))),
        ("legacy", report([item(0)], agent=fixture_agent("muse"))),
        ("legacy", report([item(0)], v2=False)),
    ):
        reply = web.post(ROUTE, json=raw, headers=bearer(TOKENS[credential]))
        [stored] = recorded(lab.cycle, reply.json()["cycle_id"], "RESEARCH_PACKET")
        states.append(stored["state"])
        origins.append(stored["research_origin"])
    assert {s["technical_context"]["origin"] for s in states} == {"EXTERNAL_RESEARCH_AGENT"}
    assert all(state == states[0] for state in states)  # The reviewer cannot tell who.
    # The packet body keeps the provenance category outside the reviewed state.
    assert origins == ["EXTERNAL_RESEARCH_AGENT"] * 3 + ["EXTERNAL_MUSE"]


def test_recorded_legacy_report_keeps_its_hash_label_replay_and_review_binding(
    lab, monkeypatch
):
    # The canonical legacy form is unchanged: the 1.2b pinned hash still holds.
    assert parse_report(legacy_body()).report_hash == LEGACY_REPORT_HASH
    raw = legacy_body()
    raw["report_id"] = str(uuid4())  # The session ledger is shared; keep this cycle unique.
    with monkeypatch.context() as earlier_code:
        # Recorded as the pre-1.3 intake did, with the former constant review label.
        earlier_code.setattr(research_dossier, "REVIEW_ORIGIN", "EXTERNAL_MUSE_RESEARCH")
        first = lab.cycle.start_report(copy.deepcopy(raw), max_seconds=300)
    stored = recorded(lab.cycle, raw["report_id"], "RESEARCH_PACKET")
    assert {p["state"]["technical_context"]["origin"] for p in stored} == {
        "EXTERNAL_MUSE_RESEARCH"
    }
    replay = lab.cycle.start_report(copy.deepcopy(raw), max_seconds=300)
    assert replay["idempotent_replay"] and replay["cycle_id"] == first["cycle_id"]
    assert replay["item_results"] == first["item_results"]  # Stored hashes, never recompiled.
    assert recorded(lab.cycle, raw["report_id"], "RESEARCH_PACKET") == stored
    decisions = asyncio.run(lab.cycle.tick(raw["report_id"]))
    assert [d["disposition"] for d in decisions] == ["APPROVED", "APPROVED"]
    assert sorted(encoded(strict_json(r.content)["state"]) for r in lab.sent) == sorted(
        encoded(p["state"]) for p in stored
    )
    assert len(lab.cycle.approved_packets(raw["report_id"])) == 2  # Receipts still bind.
    # The same content under a new report ID is new intake with the neutral label.
    fresh = copy.deepcopy(raw)
    fresh["report_id"] = str(uuid4())
    result = lab.cycle.start_report(fresh, max_seconds=300)
    assert {p["state"]["technical_context"]["origin"]
            for p in recorded(lab.cycle, fresh["report_id"], "RESEARCH_PACKET")} == {
        "EXTERNAL_RESEARCH_AGENT"
    }
    assert result["item_results"][0]["evidence_hash"] != first["item_results"][0]["evidence_hash"]


def test_funnel_groups_by_agent_version_and_guidelines_with_a_legacy_bucket(lab):
    before = ledger_head(lab.cycle.repo)
    web = client(lab.cycle)
    other_hash = "ab" * 32

    def agent(name, version, digest=MUSE_GUIDELINES_SHA256):
        return {**fixture_agent(name, version), "guidelines_sha256": digest}

    submissions = [
        ("legacy", report([item(0), item(1)], v2=False)),
        ("instinct", report([item(0), item(1, with_rationale=False)],
                            agent=agent("instinct", "1.0.0"))),
        ("instinct", report([item(0)], agent=agent("instinct", "1.0.0"))),
        ("instinct", report([item(0)], agent=agent("instinct", "2.0.0"))),
        ("grogbot", report([item(0), item(1), item(2)],
                           agent=agent("grogbot", "1.0.0", other_hash))),
    ]
    for credential, raw in submissions:
        reply = web.post(ROUTE, json=raw, headers=bearer(TOKENS[credential]))
        assert reply.status_code == 202
        asyncio.run(lab.cycle.tick(reply.json()["cycle_id"]))
        lab.cycle.approved_packets(reply.json()["cycle_id"])
    page = research_funnel(lab.cycle.repo, after=before, limit=50)
    assert [row["attribution"] for row in page["items"]] == [LEGACY_UNATTRIBUTED] + [
        ATTRIBUTED
    ] * 4
    groups = {(g["agent_id"], g["agent_version"], g["guidelines_sha256"]): g
              for g in page["agent_groups"]}
    muse_hash = MUSE_GUIDELINES_SHA256
    assert list(groups) == [
        (None, None, None), ("grogbot", "1.0.0", other_hash),
        ("instinct", "1.0.0", muse_hash), ("instinct", "2.0.0", muse_hash),
    ]
    legacy = groups[(None, None, None)]
    assert legacy["attribution"] == LEGACY_UNATTRIBUTED  # One bucket, never counted as Muse.
    assert (legacy["cycle_count"], legacy["contender_count"], legacy["rationale_packet_count"]) == (
        1, 2, 2
    )
    first = groups[("instinct", "1.0.0", muse_hash)]
    assert (first["cycle_count"], first["contender_count"], first["intake_rejected_count"]) == (
        2, 2, 1
    )
    assert first["latest_revision_outcomes"] == {"APPROVED": 2} and first["selected_count"] == 2
    assert first["review_receipt_count"] == 2 and first["market_counts"] == {"US": 2}
    assert groups[("grogbot", "1.0.0", other_hash)]["contender_count"] == 3
    assert groups[("instinct", "2.0.0", muse_hash)]["cycle_count"] == 1
    for group in groups.values():
        assert group["rationale_claim_support_rate"] is None
        assert group["rationale_claim_support_note"] == CLAIM_SUPPORT_NOTE
    # Read-only over the existing route: status credential yes, agent credentials no.
    route = f"/api/v1/lab/analytics/research?after={before}&limit=50"
    reply = web.get(route, headers=bearer(STATUS))
    assert reply.status_code == 200
    assert reply.json()["agent_groups"] == json.loads(json.dumps(page["agent_groups"]))
    assert web.get(route, headers=bearer(AGENTS["instinct"])).status_code == 403


def test_evidence_and_claims_only_reach_cycles_of_the_credentials_agent(lab):
    web = client(lab.cycle)
    lab.reply[0] = response(unsupported=INSUFFICIENT)  # Every item gets an evidence task.
    cycles = {}
    for name, credential, raw in (
        ("instinct", "instinct", report([item(0)], agent=fixture_agent("instinct"))),
        ("grogbot", "grogbot", report([item(0)], agent=fixture_agent("grogbot"))),
        ("muse", "legacy", report([item(0)], agent=fixture_agent("muse"))),
        ("legacy", "legacy", report([item(0)], v2=False)),
    ):
        reply = web.post(ROUTE, json=raw, headers=bearer(TOKENS[credential]))
        cycles[name] = reply.json()["cycle_id"]
        asyncio.run(lab.cycle.tick(cycles[name]))
    claim = {"claimant": "fixture-agent", "lease_seconds": 60, "limit": 5}
    owners = {"instinct": {"instinct"}, "grogbot": {"grogbot"}, "legacy": {"muse", "legacy"}}
    for credential, allowed in owners.items():
        for name, cycle_id in cycles.items():
            reply = web.post(f"/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim",
                             json=claim, headers=bearer(TOKENS[credential]))
            if name in allowed:
                assert reply.status_code == 200 and len(reply.json()["tasks"]) == 1
            else:
                assert reply.status_code == 403
                assert reply.json() == {"detail": "AGENT_IDENTITY_MISMATCH"}
    path = f"/api/v1/lab/cycles/{cycles['instinct']}/evidence"
    for credential in ("grogbot", "legacy"):  # Refused before the body is even read.
        reply = web.post(path, json={}, headers=bearer(TOKENS[credential]))
        assert reply.status_code == 403 and reply.json() == {"detail": "AGENT_IDENTITY_MISMATCH"}
    assert web.post(path, json={}, headers=bearer(AGENTS["instinct"])).status_code == 422
    # A cycle that does not exist is left to the cycle's own refusal.
    missing = f"/api/v1/lab/cycles/{uuid4()}/evidence-tasks/claim"
    reply = web.post(missing, json=claim, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 422 and reply.json() == {"detail": "RESEARCH_CYCLE_MISSING"}


def attributed(mx, symbol, agent):
    """The fixture selection re-published with an agent block, as start_report records it."""
    engine = mx[0]
    body = {k: v for k, v in packet(mx, symbol).items() if k != "selection_event_seq"}
    body["agent"] = agent
    with engine.store.transaction() as conn:
        event = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": copy.deepcopy(body)})
    return {**body, "selection_event_seq": event["event_seq"]}


def test_setup_position_and_result_payloads_carry_the_agent(mx):
    engine, venue, _ = mx
    agent = fixture_agent("instinct", "3.1.0")
    web = TestClient(create_managed_app(
        FixtureCycle(engine.repo, engine.store), engine.store, api_token=LEGACY,
        runtime_status=lambda: {}, agent_tokens=AGENTS,
        position_news=PositionNewsService(engine.store, clock=lambda: venue.now),
    ))
    sid = engine.admit(attributed(mx, "BTC/USD", agent))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    entry = venue.orders_of("buy")[-1]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    expected = {"attribution": ATTRIBUTED, "agent_id": "instinct", "agent_version": "3.1.0"}

    def fields(row):
        return {k: row[k] for k in expected}

    [setup] = [s for s in web.get("/api/v1/lab/setups", headers=bearer(LEGACY)).json()["items"]
               if s["setup_id"] == str(sid)]
    assert fields(setup) == expected
    [position] = web.get("/api/v1/lab/positions", headers=bearer(LEGACY)).json()["items"]
    assert position["setup_id"] == str(sid) and fields(position) == expected
    # Position news goes only to the proposing agent's setup (ownership before content).
    news = f"/api/v1/lab/positions/{sid}/news"
    for credential in ("grogbot", "legacy"):
        reply = web.post(news, json={}, headers=bearer(TOKENS[credential]))
        assert reply.status_code == 403 and reply.json() == {"detail": "AGENT_IDENTITY_MISMATCH"}
    reply = web.post(news, json={}, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 422 and reply.json() == {"detail": "POSITION_NEWS_FIELDS_INVALID"}
    # The route screens the posting agent's own ID out of the sources Jev will read.
    named = {"news_id": str(uuid4()), "lifecycle_id": str(uuid4()), "expected_news_revision": 1,
             "sources": [{"source_id": "instinct-note-1", "url": "https://example.com/a",
                          "excerpt": "A plain third-party sentence.", "published_at": None,
                          "retrieved_at": venue.now.isoformat()}]}
    reply = web.post(news, json=named, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 422 and reply.json() == {"detail": "AGENT_IDENTITY_IN_NEWS"}
    reached = observation(mx, trade_price="111", bid="111", ask="111.01")
    engine.manage(sid, reached)
    engine.manage(sid, reached)
    target = venue.orders_of("sell", "market")[-1]
    engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
    engine.manage(sid, reached)
    assert engine._load(sid)[1]["state"] == "CLOSED"
    legacy_sid = close(mx, "SPY")
    unattributed = {"attribution": LEGACY_UNATTRIBUTED, "agent_id": None, "agent_version": None}
    results = {r["setup_id"]: r for r in
               web.get("/api/v1/lab/results", headers=bearer(LEGACY)).json()["items"]}
    assert fields(results[str(sid)]) == expected
    assert fields(results[str(legacy_sid)]) == unattributed
    history = {r["setup_id"]: r for r in
               web.get("/api/v1/lab/history/results", headers=bearer(LEGACY)).json()["items"]}
    assert fields(history[str(sid)]) == expected
    assert fields(history[str(legacy_sid)]) == unattributed


def test_v2_packets_keep_the_agent_through_admission_sql_onto_the_setup(mx):
    engine, venue, receipts = mx
    now = venue.now
    reviewer = JevReviewer(
        receipts, ReliabilityPolicy("AGENT_IDENTITY_FIXTURE", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response())),
        key_provider=lambda: FIXTURE_KEY, clock=lambda: venue.now,
    )
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    agent = fixture_agent("grogbot", "0.9.1")
    raw = report([item(0, bars=20, rationale_bars=("bar-10",), now=now, market="CRYPTO")],
                 now=now, agent=agent)
    cycle_id = cycle.start_report(raw, max_seconds=300)["cycle_id"]
    asyncio.run(cycle.tick(cycle_id))
    [chosen] = cycle.approved_packets(cycle_id)
    with engine.repo.connect() as conn:
        failure = conn.execute("SELECT lab.managed_review_failure(%s) AS reason",
                               (Jsonb(json_safe(chosen)),)).fetchone()["reason"]
    assert failure is None and chosen["agent"] == agent
    with engine.store.transaction() as conn:
        event = system_event(engine.repo, conn, "CLASSIFICATION_IMPORTED",
                             {"ticker": chosen["symbol"], "source": "AGENT_IDENTITY_FIXTURE"})
        conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                     (event["seq"], chosen["symbol"], chosen["symbol"], chosen["symbol"],
                      "AGENT_IDENTITY_FIXTURE"))
    setup_id = engine.admit(chosen)
    with engine.repo.connect() as conn:
        record = conn.execute("SELECT record_json FROM lab.managed_setups WHERE setup_id=%s",
                              (setup_id,)).fetchone()["record_json"]
    assert record["agent"] == agent and "agent" not in record["state"]


def test_worker_adds_the_agent_block_itself_and_legacy_mode_is_unchanged(tmp_path):
    spool = MuseSpool(tmp_path / "agent.db")
    provider = ScriptedProvider([{"kind": "report", "items": [item(0)]}])
    worker = MuseWorker(spool, Api(), provider, clock=lambda: NOW,
                        agent_id="instinct", agent_version="2.1.0")
    report_id = worker.prepare_report("CRYPTO")
    body = json.loads(spool.pending()[0]["body"])
    assert body["report_id"] == report_id and body["schema_version"] == "AGENT_RESEARCH_REPORT_V2"
    run_id = uuid5(NAMESPACE_URL,
                   f"catalyst-muse-run:{spool.installation_id()}:report-job:CRYPTO:1")
    assert body["agent"] == {
        "agent_id": "instinct", "agent_version": "2.1.0",
        "guidelines_version": MUSE_GUIDELINES_VERSION,
        "guidelines_sha256": MUSE_GUIDELINES_SHA256, "run_id": str(run_id),
    }
    assert body["items"] == [item(0)]  # The model's output is forwarded unchanged.
    assert provider.calls == [{"job": "market_research", "market": "CRYPTO",
                               "target_contenders": "20-30_without_padding"}]
    legacy_spool = MuseSpool(tmp_path / "legacy.db")
    MuseWorker(legacy_spool, Api(), ScriptedProvider([{"kind": "report", "items": [item(0)]}]),
               clock=lambda: NOW).prepare_report("CRYPTO")
    assert not {"schema_version", "agent"} & set(json.loads(legacy_spool.pending()[0]["body"]))
    for identity in ({"agent_id": "Instinct"}, {"agent_id": "instinct", "agent_version": "v" * 33},
                     {"agent_id": "instinct", "agent_version": None}):
        with pytest.raises(ValueError, match="^VALID_AGENT_IDENTITY_REQUIRED$"):
            MuseWorker(spool, Api(), provider, **identity)


def test_worker_follows_only_its_own_cycles(tmp_path):
    def started(cycle_id, agent_id):
        agent = fixture_agent(agent_id) if agent_id else None
        return {"kind": "RESEARCH_STARTED", "body": {"cycle_id": cycle_id, "agent": agent}}

    feed = [started("c-instinct", "instinct"), started("c-grogbot", "grogbot"),
            started("c-muse", "muse"), started("c-legacy", None),
            {"kind": "RESEARCH_EVIDENCE_TASK", "body": {"cycle_id": "c-grogbot"}},
            {"kind": "RESEARCH_EVIDENCE_TASK", "body": {"cycle_id": "c-unseen"}}]
    remembered = {}
    for agent_id in ("instinct", "muse", None):
        api = Api()
        api.responses[("GET", "/api/v1/lab/outputs?after=0&limit=10")] = {
            "items": feed, "next_cursor": len(feed),
        }
        spool = MuseSpool(tmp_path / f"{agent_id}.db")
        MuseWorker(spool, api, ScriptedProvider([]), clock=lambda: NOW,
                   agent_id=agent_id).poll()
        remembered[agent_id] = spool.cycles()
    # A task whose start was never seen is claimed once; the server's 403 then retires it.
    assert remembered == {"instinct": ["c-instinct", "c-unseen"],
                          "muse": ["c-legacy", "c-muse", "c-unseen"],
                          None: ["c-legacy", "c-muse", "c-unseen"]}


class TokenApi:
    def __init__(self, web, token):
        self.web, self.token = web, token

    def call(self, method, path, body=None):
        result = self.web.request(method, path, json=body, headers=bearer(self.token))
        if result.status_code >= 400:
            raise ValueError("MUSE_REQUEST_REJECTED")
        return result.json()


def test_worker_report_reaches_intake_as_its_agent_and_a_wrong_token_fails_closed(
    lab, tmp_path
):
    web = client(lab.cycle)
    for credential in ("instinct", "grogbot"):
        spool = MuseSpool(tmp_path / f"{credential}.db")
        worker = MuseWorker(spool, TokenApi(web, AGENTS[credential]),
                            ScriptedProvider([{"kind": "report", "items": [item(0)]}]),
                            clock=lambda: lab.now[0], agent_id="instinct",
                            agent_version="2.1.0")
        report_id = worker.prepare_report("US_STOCKS")
        worker.flush()
        assert not spool.pending()
        cycle_id = agent_cycle_id("instinct", report_id)
        started = recorded(lab.cycle, cycle_id, "RESEARCH_STARTED")
        if credential == "instinct":
            [body] = started
            assert body["agent"]["agent_id"] == "instinct"
            assert body["agent"]["guidelines_sha256"] == MUSE_GUIDELINES_SHA256
            assert body["report_schema_version"] == "AGENT_RESEARCH_REPORT_V2"
        else:  # Another agent's credential: 403, recorded as rejected, nothing stored.
            assert started == []
            outcomes = [row["outcome"] for row in spool.db.execute(
                "SELECT outcome FROM runs WHERE kind='OUTBOX'")]
            assert outcomes == ["REJECTED"]


def test_worker_cli_defaults_to_the_legacy_identity_and_refuses_invalid_ids(
    monkeypatch, tmp_path
):
    captured = []

    class Started(Exception):
        pass

    def fake_worker(*args, **kwargs):
        captured.append(kwargs)
        raise Started

    monkeypatch.setattr(muse_worker, "MuseWorker", fake_worker)
    monkeypatch.setattr(muse_worker, "read_token", lambda path: "t" * 40)
    base = ["muse_worker", "--api", "http://127.0.0.1:8780", "--token-file",
            str(tmp_path / "token"), "--command", "codex", "--once"]
    monkeypatch.setattr(sys, "argv", [*base, "--spool", str(tmp_path / "a.db")])
    with pytest.raises(Started):
        muse_worker.main()
    monkeypatch.setattr(sys, "argv", [*base, "--spool", str(tmp_path / "b.db"),
                                      "--agent-id", "instinct", "--agent-version", "2.1.0"])
    with pytest.raises(Started):
        muse_worker.main()
    assert captured == [{"agent_id": "muse", "agent_version": "LEGACY_UNDECLARED"},
                        {"agent_id": "instinct", "agent_version": "2.1.0"}]
    for flag, value in (("--agent-id", "Instinct"), ("--agent-version", "1.0 beta")):
        monkeypatch.setattr(sys, "argv", [*base, "--spool", str(tmp_path / "c.db"), flag, value])
        with pytest.raises(SystemExit):
            muse_worker.main()
    assert len(captured) == 2 and not (tmp_path / "c.db").exists()


def test_app_settings_carry_agent_tokens_privately_and_from_the_launcher_environment(
    monkeypatch, lab
):
    # What create_application needs without starting a lifespan (no worker, no broker).
    runtime = SimpleNamespace(
        research=lab.cycle, now=lambda: NOW, status=lambda: {},
        execution=SimpleNamespace(repo=lab.cycle.repo, store=lab.cycle.store, broker=None),
    )
    settings = AppSettings(8799, LEGACY, 300, (), CLASSIFICATION_POLICY, (),
                           agent_tokens=AGENTS)
    assert dict(settings.agent_tokens) == AGENTS
    assert not any(token in repr(settings) for token in AGENTS.values())
    for bad in ({"Instinct": AGENTS["instinct"]}, {"instinct": LEGACY},
                {"instinct": "short-token"}, {"i": AGENTS["instinct"]},
                {"instinct": AGENTS["instinct"], "grogbot": AGENTS["instinct"]}):
        with pytest.raises(ValueError, match="^SEPARATE_AGENT_TOKENS_REQUIRED$"):
            replace(settings, agent_tokens=bad)
    with pytest.raises(ValueError, match="^SEPARATE_AGENT_TOKENS_REQUIRED$"):
        replace(settings, status_token=AGENTS["grogbot"], operator_token=OPERATOR)
    environment = {
        "MANAGED_HTTP_PORT": "8799", "MANAGED_API_TOKEN": LEGACY,
        "MANAGED_CRYPTO_CLASSIFICATIONS_JSON": "[]", "MANAGED_REPORT_MAX_SECONDS": "300",
        "MANAGED_CLASSIFICATION_POLICY": CLASSIFICATION_POLICY,
        "MANAGED_US_CLASSIFICATIONS_JSON": "[]", "MANAGED_STATUS_TOKEN": STATUS,
        "MANAGED_OPERATOR_TOKEN": OPERATOR, "MANAGED_AGENT_TOKENS_JSON": json.dumps(AGENTS),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    app, loaded = build_app_from_env(runtime_builder=lambda: runtime)
    assert dict(loaded.agent_tokens) == AGENTS
    web = TestClient(app)
    raw = report([item(0)], agent=fixture_agent("instinct"))
    assert web.post(ROUTE, json=raw, headers=bearer(AGENTS["grogbot"])).status_code == 403
    reply = web.post(ROUTE, json=raw, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 202 and reply.json()["agent_id"] == "instinct"
    assert web.get("/api/v1/lab/status", headers=bearer(AGENTS["instinct"])).status_code == 403
    monkeypatch.setenv("MANAGED_AGENT_TOKENS_JSON", '{"instinct": ')
    with pytest.raises(ValueError, match="^SEPARATE_AGENT_TOKENS_REQUIRED$"):
        build_app_from_env(runtime_builder=lambda: runtime)


def test_position_news_naming_the_posting_agent_is_refused_before_anything_is_stored():
    """Position sources reach Jev's maintenance and 24-hour-review contexts, and Jev never sees
    the agent: an agent-written source field naming the posting agent (whole word, any case)
    is refused AGENT_IDENTITY_IN_NEWS; excerpts and URLs are third-party text."""
    from datetime import UTC, datetime

    from catalyst_lab.position_news import AGENT_IDENTITY_IN_NEWS

    class Reached(Exception):
        pass

    class Store:
        def transaction(self):
            raise Reached  # Past the screen: the service would now read the ledger.

    now = datetime.now(UTC)
    service = PositionNewsService(Store(), clock=lambda: now)

    def news(source_id, excerpt="A plain third-party sentence."):
        return {"news_id": str(uuid4()), "lifecycle_id": str(uuid4()),
                "expected_news_revision": 1,
                "sources": [{"source_id": source_id, "url": "https://example.com/news/instinct",
                             "excerpt": excerpt, "published_at": None,
                             "retrieved_at": now.isoformat()}]}

    for named in ("instinct-note-1", "INSTINCT", "news.Instinct"):
        with pytest.raises(ValueError, match=f"^{AGENT_IDENTITY_IN_NEWS}$"):
            service.submit(str(uuid4()), news(named), agent_id="instinct")
    # Not the agent: another word, a longer word, third-party text, or no agent to screen.
    for body, agent in ((news("instinctive-1"), "instinct"), (news("note-1"), "instinct"),
                        (news("note-2", "Instinct Capital said so."), "instinct"),
                        (news("instinct-note-1"), None)):
        with pytest.raises(Reached):
            service.submit(str(uuid4()), body, agent_id=agent)
