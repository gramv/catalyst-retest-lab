"""AGENT_RESEARCH_REPORT_V3 intake, REVIEW_DOSSIER_V3 and the V3 review lifetime.

Fixture evidence only: per-test disposable PostgreSQL databases, a mock Jev transport that
keeps every outbound request, a fixture universe and the fixture paper venue. No provider,
broker or owner-ledger contact.
"""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from catalyst_lab.agent_identity import agent_cycle_id
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import SKEPTIC, SKEPTIC_V2, digest, encoded, strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_V3_SHA256, MUSE_GUIDELINES_V3_VERSION
from catalyst_lab.muse_reports import ResearchReportRejected
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_dossier import RATIONALE_BUDGET_BYTES, STATE_BUDGET_BYTES
from catalyst_lab.research_dossier_v3 import DOSSIER_VERSION_V3, FIELD_MAP
from catalyst_lab.research_ranking import QUALITY, QUALITY_V2
from catalyst_lab.research_report_v3 import (
    PICK_CODES,
    REPORT_SCHEMA_V3,
    ResearchCapabilityUnavailable,
    UniverseSnapshot,
    V3Intake,
    declared_agent,
)
from catalyst_lab.research_schedule import ResearchSchedule
from catalyst_lab.research_selection_b1 import SelectionRule
from catalyst_lab.research_selection_b2 import B2_POLICY
from catalyst_lab.system_check import LiveQuote
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_research_cycle import quality_response, response
from tests.test_selection_b1 import classify, no_sleep, replay_script

NOW = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)  # 08:30 in New York.
AGENT = "claude"
SCHEDULE = ResearchSchedule("America/New_York", ("08:00",), 60)
UNIVERSE = frozenset({f"C{i:02}/USD" for i in range(40)} | {"BTC/USD", "ETH/USD", "PAXG/USD"})
POLICY = CyclePolicy(10, 10, 15, 60, 30)  # V2's 60-second review window by default.
CONFIDENCE_MARKER = "V3-CONFIDENCE-BASIS-MARKER"
SIGNAL_MARKER = "V3SIGNALMARKER"
ROUTE = "/api/v1/lab/research-reports"
LEGACY = "fixture-v3-legacy-muse-token-abcdefghijklmnopqrstuvwx"
STATUS = "fixture-v3-status-token-abcdefghijklmnopqrstuvwxyzab"
OPERATOR = "fixture-v3-operator-token-abcdefghijklmnopqrstuvwxy"
AGENTS = {"claude": "fixture-v3-claude-agent-token-abcdefghijklmnopqrst",
          "instinct": "fixture-v3-instinct-agent-token-abcdefghijklmnopq"}


# --- Fixture report builders ------------------------------------------------------------------

def hourly_bars(count=20, now=NOW):
    return [
        {"bar_id": f"b-{i:02}", "started_at": (now - timedelta(hours=count + 1 - i)).isoformat(),
         "open": "100.20", "high": "101.00", "low": "99.50", "close": "100.40",
         "volume": str(10 + i)}
        for i in range(count)
    ]


def technical(count=20, now=NOW, **changes):
    value = {
        "schema_version": "MUSE_OBSERVED_TECHNICALS_V1", "provider": "LAB_FIXTURE",
        "venue": "FIXTURE_VENUE", "feed": "FIXTURE_HOURLY",
        "source_url": "https://market.example/bars", "retrieved_at": now.isoformat(),
        "timeframe_seconds": 3600, "bars": hourly_bars(count, now),
        "quote": {"observed_at": now.isoformat(), "bid": "100.40", "ask": "100.44"},
    }
    value.update(changes)
    return value


def source(source_id="src-1", now=NOW, excerpt=None, **changes):
    value = {
        "source_id": source_id, "url": f"https://news.example/{source_id}",
        "excerpt": excerpt or "The exchange lists the token for spot trading from today.",
        "published_at": (now - timedelta(hours=3)).isoformat(),
        "retrieved_at": (now - timedelta(minutes=2)).isoformat(),
    }
    value.update(changes)
    return value


def rationale(source_ids=("src-1",), bar_ids=("b-19",)):
    claims = []
    if source_ids:
        claims.append({"claim_id": "C1", "kind": "CATALYST",
                       "text": "The exchange listing starts today.",
                       "supported_by": {"source_ids": list(source_ids), "bar_ids": []}})
    if bar_ids:
        claims.append({"claim_id": "C2", "kind": "TECHNICAL",
                       "text": "The last completed hourly bar closed at 100.40.",
                       "supported_by": {"source_ids": [], "bar_ids": list(bar_ids)}})
    return {
        "claims": claims,
        "why_now": "The listing was announced three hours before the run.",
        "why_these_levels": "Entry retests the hourly low area; the stop sits under it.",
        "why_over_peers": "Ranked above C39/USD, whose catalyst has no primary source.",
        "what_would_change_my_mind": "A delisting notice or an hourly close under 95.",
        "known_risks": ["Thin weekend volume."],
        "agent_confidence": {"level": "MEDIUM",
                             "basis": CONFIDENCE_MARKER + " one primary source."},
    }


def pick(index=0, symbol=None, *, kind="BOTH", now=NOW, **changes):
    news, chart = kind in {"NEWS", "BOTH"}, kind in {"CHART", "BOTH"}
    value = {
        "signal_id": f"{SIGNAL_MARKER}-{index:02}",
        "symbol": symbol or f"C{index:02}/USD",
        "kind": kind,
        "agent_current_price": "100.50",
        "agent_price_at": (now - timedelta(minutes=1)).isoformat(),
        "levels": {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
                   "target": "111"},
        "stated_reward_risk": "2.13",
        "reasoning": {
            "thesis": f"THESIS-{index:02}: the listing adds a new source of spot demand "
                      "(inference from the cited notice).",
            "why_now": "WHY-NOW: the notice is three hours old and trading starts today.",
            "why_these_levels": "LEVELS: entry and stop sit at the hourly range lows.",
            "risks": "RISKS: listings are often priced in within hours.",
            "invalidation": f"INVALIDATION-{index:02}: an hourly close below 95.",
        },
        "selection_rationale": rationale(("src-1",) if news else (), ("b-19",) if chart else ()),
        "sources": [source(now=now)] if news else [],
        "agent_confidence": "0.62",
    }
    if chart:
        value["technical_evidence"] = technical(now=now)
    value.update(changes)
    return value


def agent_block(agent_id=AGENT):
    return {"agent_id": agent_id, "agent_version": "fixture-1",
            "guidelines_version": MUSE_GUIDELINES_V3_VERSION,
            "guidelines_sha256": MUSE_GUIDELINES_V3_SHA256, "run_id": str(uuid4())}


def report_v3(picks, *, now=NOW, agent_id=AGENT, skipped=None, **fields):
    slot = SCHEDULE.latest_at_or_before(now)
    raw = {
        "schema_version": REPORT_SCHEMA_V3,
        "report_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "valid_until": min(now + timedelta(hours=20), SCHEDULE.validity_limit(slot)).isoformat(),
        "run_slot": SCHEDULE.local(slot).isoformat(),
        "context_as_of": (now - timedelta(minutes=10)).isoformat(),
        "agent": agent_block(agent_id),
        "picks": picks,
        "skipped": [{"symbol": "C39/USD", "reason": "No primary source for the catalyst."}]
        if skipped is None else skipped,
    }
    raw.update(fields)
    return raw


def v3_intake(universe=UNIVERSE, schedule=SCHEDULE, *, reads=None, now=NOW):
    def snapshot():
        if reads is not None:
            reads.append(1)
        return UniverseSnapshot(frozenset(universe), now, "LAB_FIXTURE_UNIVERSE")
    return V3Intake(schedule, snapshot)


def instant(text):
    """Canonical JSON writes a UTC offset as Z; the instant is what was sent."""
    return datetime.fromisoformat(text)


def normalized_bars(bars):
    return [{**bar, "started_at": instant(bar["started_at"])} for bar in bars]


def cycle_of(raw):
    return agent_cycle_id(raw["agent"]["agent_id"], raw["report_id"])


def events(cycle, cycle_id, kind=None):
    rows = cycle.outputs(cycle_id, limit=1000)
    return [r["body"] for r in rows if kind is None or r["kind"] == kind]


@pytest.fixture
def lab(er):
    """A research cycle on its own disposable database with a recording mock Jev."""
    now, calls, reply = [NOW], [], [response()]

    async def provider(request):
        body = strict_json(request.content)
        calls.append(body)
        await asyncio.sleep(0)
        quality = set(body["questions"]) == set(QUALITY.questions)
        return httpx.Response(200, json=quality_response() if quality else reply[0])

    reviewer = JevReviewer(
        JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev")),
        ReliabilityPolicy("RESEARCH_V3_FIXTURE", 10, 1, 0.25, 10000, 30),
        key_provider=lambda: "fixture-no-provider-secret",
        transport=httpx.MockTransport(provider),
        clock=lambda: now[0],
    )
    cycle = ResearchCycle(
        RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk")),
        reviewer, POLICY, clock=lambda: now[0],
    )
    return SimpleNamespace(cycle=cycle, now=now, calls=calls, reply=reply)


# --- Accepted end to end ----------------------------------------------------------------------

def test_a_v3_report_becomes_blind_v3_dossiers_reviewed_under_the_configured_rule(lab):
    raw = report_v3([pick(0), pick(1, kind="NEWS"), pick(2, kind="CHART")])
    reads = []
    result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake(reads=reads))
    cycle_id = cycle_of(raw)
    assert reads == [1]  # One universe snapshot per new report.
    assert result["status"] == "MUSE_REPORT_RECORDED" and result["cycle_id"] == cycle_id
    assert (result["contender_count"], result["submitted_count"], result["rejected_count"]) == (
        3, 3, 0)
    assert result["report_schema_version"] == REPORT_SCHEMA_V3
    assert result["run_slot"] == raw["run_slot"] and result["skipped_count"] == 1
    assert result["review_validity"] == "PACKET_EXPIRY" and result["trade_authorized"] is False
    assert (result["agent_id"], result["research_origin"]) == (AGENT, "EXTERNAL_RESEARCH_AGENT")
    assert [r["status"] for r in result["item_results"]] == ["ACCEPTED"] * 3
    [started] = events(lab.cycle, cycle_id, "RESEARCH_STARTED")
    assert started["report_schema_version"] == REPORT_SCHEMA_V3
    assert started["dossier_version"] == DOSSIER_VERSION_V3
    assert started["selection_policy"] == "MUSE_JEV_RESEARCH_SELECTION_V2"
    assert started["research_schedule"] == SCHEDULE.as_dict()
    assert started["universe"] == {"source": "LAB_FIXTURE_UNIVERSE",
                                   "fetched_at": NOW.isoformat(),
                                   "symbols": sorted(UNIVERSE)}
    assert started["report"]["picks"][0]["reasoning"]["thesis"].startswith("THESIS-00")
    assert started["report_hash"] == digest(encoded(started["report"]))
    packets = events(lab.cycle, cycle_id, "RESEARCH_PACKET")
    dossiers = events(lab.cycle, cycle_id, "RESEARCH_DOSSIER")
    assert [p["symbol"] for p in packets] == ["C00/USD", "C01/USD", "C02/USD"]
    assert [p["rank"] for p in packets] == [1, 2, 3]
    for packet, dossier, submitted in zip(packets, dossiers, raw["picks"], strict=True):
        state = packet["state"]
        assert packet["market"] == "CRYPTO" and packet["item_key"] == "CRYPTO:" + packet["symbol"]
        assert packet["expires_at"] == datetime.fromisoformat(raw["valid_until"]).isoformat()
        assert packet["review_validity"] == "PACKET_EXPIRY"
        assert packet["report_schema_version"] == REPORT_SCHEMA_V3
        assert packet["agent"]["agent_id"] == AGENT and packet["agent_confidence"] == "0.62"
        assert packet["selection_rationale"]["agent_confidence"]["level"] == "MEDIUM"
        assert packet["levels"] == state["levels"] == submitted["levels"]
        # Every price, the stated reward-to-risk and the reasoning, exactly as sent.
        for key in ("kind", "agent_current_price", "stated_reward_risk"):
            assert state[key] == submitted[key]
        assert instant(state["agent_price_at"]) == instant(submitted["agent_price_at"])
        for origin, target in FIELD_MAP.items():
            assert state[target] == submitted["reasoning"][origin.split(".")[1]]
        assert instant(state["valid_until"]) == instant(raw["valid_until"])
        assert "economic_relationship" not in state and "catalyst" not in state
        assert state["rationale"]["status"] == "UNVERIFIED_PROPOSER_CLAIMS"
        assert state["rationale"]["claims"] == submitted["selection_rationale"]["claims"]
        # Blind: no agent identity, confidence or signal ID in the reviewed state.
        text = encoded(state)
        for hidden in (AGENT, CONFIDENCE_MARKER, SIGNAL_MARKER, "agent_confidence", "0.62"):
            assert hidden not in text
        assert packet["evidence_hash"] == digest(text) == dossier["manifest"]["state_sha256"]
        assert dossier["manifest"]["dossier_version"] == DOSSIER_VERSION_V3
        assert dossier["manifest"]["field_map"] == FIELD_MAP
        assert dossier["manifest"]["truncated"] == []
        assert dossier["manifest"]["state_bytes"] <= STATE_BUDGET_BYTES
    assert [p["state"]["sources"] != [] for p in packets] == [True, True, False]
    bars = [p["state"]["technical_context"].get("observed_facts") for p in packets]
    assert bars[1] is None
    for facts in (bars[0], bars[2]):
        # Every submitted bar reaches the reviewer, with code-computed descriptive metrics.
        assert normalized_bars(facts["observations"]["bars"]) == normalized_bars(
            raw["picks"][0]["technical_evidence"]["bars"])
        assert facts["bar_selection"] == "ALL_SUBMITTED_BARS"
        assert facts["metrics"]["last_completed_close"] == "100.40"
    asyncio.run(lab.cycle.tick(cycle_id))
    skeptic = [c for c in lab.calls if set(c["questions"]) == set(SKEPTIC.questions)]
    assert sorted(encoded(c["state"]) for c in skeptic) == sorted(
        encoded(p["state"]) for p in packets)
    for call in lab.calls:
        text = encoded(call)
        assert AGENT not in text and CONFIDENCE_MARKER not in text and SIGNAL_MARKER not in text
    decisions = events(lab.cycle, cycle_id, "RESEARCH_DECISION")
    assert [d["disposition"] for d in decisions] == ["APPROVED"] * 3
    chosen = lab.cycle.approved_packets(cycle_id)
    assert len(chosen) == 3
    for packet in chosen:
        assert packet["review_valid_until"] == packet["expires_at"]  # The V3 review lifetime.
        assert packet["thesis"] == packet["state"]["thesis"]
        assert packet["disproof"] == packet["state"]["disproof"]
        assert packet["sources"] == packet["state"]["sources"]
        assert "economic_relationship" not in packet  # Never invented for a V3 state.
        assert packet["strategy_version"] == "CRYPTO_STRUCTURAL_RETEST_TEST_V1"


def test_the_review_lasts_until_the_packet_expiry_not_the_v2_window(lab):
    from tests.test_review_dossier import item as v2_item
    from tests.test_review_dossier import report as v2_report

    own = (NOW + timedelta(hours=2)).isoformat()
    raw = report_v3([pick(0), pick(1, valid_until=own)])
    lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    v2 = v2_report([v2_item(0, now=NOW)], now=NOW)
    lab.cycle.start_report(v2, max_seconds=300)
    [short] = [p for p in events(lab.cycle, cycle_of(raw), "RESEARCH_PACKET")
               if p["symbol"] == "C01/USD"]
    assert short["expires_at"] == datetime.fromisoformat(own).isoformat()
    # Two minutes later V2's 60-second review window has closed; the V3 picks remain open.
    lab.now[0] = NOW + timedelta(minutes=2)
    asyncio.run(lab.cycle.tick(cycle_of(raw)))
    asyncio.run(lab.cycle.tick(cycle_of(v2)))
    assert [d["disposition"] for d in events(lab.cycle, cycle_of(raw), "RESEARCH_DECISION")] == [
        "APPROVED", "APPROVED"]
    assert [d["disposition"] for d in events(lab.cycle, cycle_of(v2), "RESEARCH_DECISION")] == [
        "EXPIRED"]
    assert len(lab.cycle.approved_packets(cycle_of(raw))) == 2
    # Past a pick's own valid_until it is no longer live; its sibling still is.
    lab.now[0] = NOW + timedelta(hours=3)
    assert [p["symbol"] for p in lab.cycle.approved_packets(cycle_of(raw))] == ["C00/USD"]


def test_packet_expiry_is_bounded_by_the_configured_report_maximum(lab):
    raw = report_v3([pick(0)])
    result = lab.cycle.start_report(raw, max_seconds=1800, v3=v3_intake())
    expected = (NOW + timedelta(seconds=1800)).isoformat()
    assert result["expires_at"] == expected
    [packet] = events(lab.cycle, cycle_of(raw), "RESEARCH_PACKET")
    assert packet["expires_at"] == expected and result["item_results"][0]["expires_at"] == expected


# --- Per-pick refusals: each its own code, siblings proceed ------------------------------------

def refused_picks(now=NOW):
    later = (now + timedelta(minutes=1)).isoformat()
    unknown = pick(7)
    unknown["selection_rationale"]["claims"][0]["supported_by"]["source_ids"] = ["nope"]
    over = pick(12, technical_evidence=technical(64), sources=[
        source(f"s{n}", excerpt="Exchange notice text. " * 54) for n in range(5)])
    over["selection_rationale"] = rationale(("s0",), ("b-63",))
    return [
        ("INVALID_RESEARCH_ITEM", pick(1, qty="3")),
        ("PRICE_NOT_POSITIVE", pick(2, levels={"entry_trigger": "100", "max_entry_price": "0",
                                               "stop": "-1", "target": "111"})),
        ("SYMBOL_NOT_IN_UNIVERSE", pick(3, "USDT/USD")),
        ("NEWS_SOURCES_REQUIRED", pick(4, kind="NEWS", sources=[],
                                       technical_evidence=technical(),
                                       selection_rationale=rationale((), ("b-19",)))),
        ("TECHNICAL_EVIDENCE_REQUIRED", pick(5, kind="CHART", sources=[source()],
                                             technical_evidence=None,
                                             selection_rationale=rationale(("src-1",), ()))),
        ("CITATION_UNRESOLVED", unknown),
        ("PICK_VALIDITY_INVALID", pick(8, valid_until=(now + timedelta(days=2)).isoformat())),
        ("AGENT_PRICE_TIME_INVALID", pick(9, agent_price_at=later)),
        ("INVALID_SOURCE_EVIDENCE", pick(10, sources=[source(retrieved_at=later)])),
        ("FUTURE_TECHNICAL_EVIDENCE", pick(11, technical_evidence=technical(now=now + timedelta(
            minutes=1)))),
        ("DOSSIER_OVER_BUDGET", over),
        ("AGENT_IDENTITY_IN_PICK", pick(13, reasoning={
            **pick(13)["reasoning"], "risks": "Claude may be wrong about the listing."})),
    ]


def test_every_pick_refusal_has_its_own_code_and_siblings_proceed(lab):
    refused = refused_picks()
    raw = report_v3([pick(0)] + [value for _, value in refused] + [pick(20, kind="CHART")])
    result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    codes = [r.get("code") for r in result["item_results"]]
    assert codes == [None] + [code for code, _ in refused] + [None]
    # Every documented pick code is exercised here once, in the order picks are checked.
    assert sorted(set(codes) - {None}) == sorted(PICK_CODES)
    assert (result["contender_count"], result["rejected_count"]) == (2, len(refused))
    details = {r["code"]: r["errors"] for r in result["item_results"] if r["status"] == "REJECTED"}
    assert details["INVALID_RESEARCH_ITEM"] == [{"path": "picks[1].qty",
                                                  "code": "EXTRA_FORBIDDEN"}]
    assert details["PRICE_NOT_POSITIVE"] == [
        {"path": "picks[2].levels.max_entry_price", "code": "PRICE_NOT_POSITIVE"},
        {"path": "picks[2].levels.stop", "code": "PRICE_NOT_POSITIVE"}]
    assert details["SYMBOL_NOT_IN_UNIVERSE"] == [{"path": "picks[3].symbol",
                                                   "code": "SYMBOL_NOT_IN_UNIVERSE"}]
    assert details["CITATION_UNRESOLVED"] == [{
        "path": "picks[6].selection_rationale.claims[0].supported_by.source_ids[0]",
        "code": "RATIONALE_REFERENCE_UNKNOWN"}]
    assert details["PICK_VALIDITY_INVALID"] == [{"path": "picks[7].valid_until",
                                                  "code": "PICK_VALID_UNTIL_AFTER_REPORT"}]
    assert details["AGENT_PRICE_TIME_INVALID"] == [{"path": "picks[8].agent_price_at",
                                                     "code": "AGENT_PRICE_AFTER_REPORT"}]
    assert details["AGENT_IDENTITY_IN_PICK"] == [{"path": "picks[12].reasoning.risks",
                                                   "code": "AGENT_IDENTITY_IN_PICK"}]
    [over] = details["DOSSIER_OVER_BUDGET"]
    assert over["path"] == "picks[11]" and over["bytes"] > STATE_BUDGET_BYTES
    assert "Claude may" not in json.dumps(result)  # Paths and codes, never submitted text.
    cycle_id = cycle_of(raw)
    rejected = events(lab.cycle, cycle_id, "RESEARCH_ITEM_REJECTED_AT_INTAKE")
    assert [(r["index"], r["code"]) for r in rejected] == [
        (i + 1, code) for i, (code, _) in enumerate(refused)]
    assert rejected[0]["item_sha256"] == digest(encoded(json_safe(raw["picks"][1])))
    assert [p["symbol"] for p in events(lab.cycle, cycle_id, "RESEARCH_PACKET")] == [
        "C00/USD", "C20/USD"]
    asyncio.run(lab.cycle.tick(cycle_id))
    assert len(lab.calls) == 2  # Only the accepted siblings reach the reviewer.


def test_a_pick_expired_at_intake_and_mixed_kind_gaps_have_exact_paths(lab):
    # Generated 40 seconds ago; the first pick's own validity ended 10 seconds ago.
    raw = report_v3([
        pick(0, valid_until=(NOW - timedelta(seconds=10)).isoformat()),
        pick(1, sources=[], technical_evidence=None),
        pick(2),
    ], generated_at=(NOW - timedelta(seconds=40)).isoformat())
    result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    first, second, _ = result["item_results"]
    assert first["errors"] == [{"path": "picks[0].valid_until", "code": "PICK_EXPIRED_AT_INTAKE"}]
    assert second["code"] == "NEWS_SOURCES_REQUIRED" and second["errors"] == [
        {"path": "picks[1].sources", "code": "NEWS_SOURCES_REQUIRED"},
        {"path": "picks[1].technical_evidence", "code": "TECHNICAL_EVIDENCE_REQUIRED"}]


def test_a_report_without_an_acceptable_pick_is_refused_whole_and_stores_nothing(lab):
    raw = report_v3([pick(0, "USDT/USD"), pick(1, qty="1")])
    with pytest.raises(ResearchReportRejected) as caught:
        lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    assert caught.value.code == "SYMBOL_NOT_IN_UNIVERSE"
    assert [r["code"] for r in caught.value.item_results] == [
        "SYMBOL_NOT_IN_UNIVERSE", "INVALID_RESEARCH_ITEM"]
    assert events(lab.cycle, cycle_of(raw)) == []
    raw = report_v3([pick(1, qty="1")])
    with pytest.raises(ResearchReportRejected, match="^INVALID_MUSE_REPORT$"):
        lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())


def test_the_identity_screen_reads_agent_text_but_not_third_party_excerpts_or_urls(lab):
    quoted = pick(0, sources=[source(
        excerpt="Partners integrating Claude and other assistants expand the network.",
        url="https://claude.example/notice")])
    signal = pick(1, signal_id="claude-run-01", agent_confidence="0.9")
    other = pick(2, reasoning={**pick(2)["reasoning"], "thesis": "Instinct flagged this."})
    named = pick(3, selection_rationale={**rationale(), "why_over_peers": "CLAUDE ranks it."})
    word = pick(4, reasoning={**pick(4)["reasoning"],
                              "risks": "Claudette-style risk, claudes, claude_bot."})
    raw = report_v3([quoted, signal, other, named, word])
    result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    assert [r["status"] for r in result["item_results"]] == [
        "ACCEPTED", "ACCEPTED", "ACCEPTED", "REJECTED", "ACCEPTED"]
    assert result["item_results"][3]["errors"] == [{
        "path": "picks[3].selection_rationale.why_over_peers", "code": "AGENT_IDENTITY_IN_PICK"}]


def test_all_bars_are_reviewed_and_an_over_budget_pick_is_refused_not_truncated(lab):
    fits = pick(0, technical_evidence=technical(40))
    fits["selection_rationale"] = rationale(("src-1",), ("b-39",))
    accented = pick(1)
    accented["reasoning"]["thesis"] = "é" * 1000  # 1,000 characters, 6,000 escaped bytes.
    accented["reasoning"]["why_now"] = "é" * 600
    heavy = pick(2)
    heavy["selection_rationale"]["claims"] = [
        {"claim_id": f"C{n}", "kind": "CATALYST", "text": "é" * 300,
         "supported_by": {"source_ids": ["src-1"], "bar_ids": []}} for n in range(2)]
    raw = report_v3([fits, accented, heavy])
    result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    accepted, over, rationale_over = result["item_results"]
    assert accepted["status"] == "ACCEPTED" and accepted["dossier_bytes"] <= STATE_BUDGET_BYTES
    [packet] = events(lab.cycle, cycle_of(raw), "RESEARCH_PACKET")
    observed = packet["state"]["technical_context"]["observed_facts"]["observations"]["bars"]
    assert [bar["bar_id"] for bar in observed] == [f"b-{i:02}" for i in range(40)]
    [dossier] = events(lab.cycle, cycle_of(raw), "RESEARCH_DOSSIER")
    assert dossier["manifest"]["bars"]["omitted_bar_ids"] == []
    assert over["code"] == "DOSSIER_OVER_BUDGET" and over["errors"][0]["bytes"] > 11000
    assert rationale_over["code"] == "DOSSIER_OVER_BUDGET"
    assert rationale_over["errors"][0] == {
        "path": "picks[2].selection_rationale", "code": "DOSSIER_OVER_BUDGET",
        "bytes": rationale_over["errors"][0]["bytes"], "budget_bytes": RATIONALE_BUDGET_BYTES}
    assert rationale_over["errors"][0]["bytes"] > RATIONALE_BUDGET_BYTES


# --- Whole-report refusals ----------------------------------------------------------------------

def slot_shift(hours):
    return lambda raw: raw.update(run_slot=(
        datetime.fromisoformat(raw["run_slot"]) + timedelta(hours=hours)).isoformat())


ENVELOPE = {
    "run slot not scheduled": (slot_shift(1), [{"path": "run_slot",
                                                "code": "RUN_SLOT_NOT_SCHEDULED"}]),
    "run slot in the future": (slot_shift(24), [{"path": "run_slot",
                                                 "code": "RUN_SLOT_IN_FUTURE"}]),
    # Answering yesterday's run: valid only until today's run plus the 60-minute grace.
    "valid after next run": (
        lambda raw: (slot_shift(-24)(raw),
                     raw.update(valid_until=(NOW + timedelta(hours=1)).isoformat())),
        [{"path": "valid_until", "code": "REPORT_VALIDITY_AFTER_NEXT_RUN"}]),
    "valid over 24 hours": (
        lambda raw: raw.update(valid_until=(NOW + timedelta(hours=25)).isoformat()),
        [{"path": "valid_until", "code": "REPORT_VALIDITY_OVER_24_HOURS"},
         {"path": "valid_until", "code": "REPORT_VALIDITY_AFTER_NEXT_RUN"}]),
    "valid before generated": (
        lambda raw: raw.update(valid_until=NOW.isoformat()),
        [{"path": "valid_until", "code": "INVALID_REPORT_EXPIRY"}]),
    "context after report": (
        lambda raw: raw.update(context_as_of=(NOW + timedelta(seconds=1)).isoformat()),
        [{"path": "context_as_of", "code": "CONTEXT_AFTER_REPORT"}]),
    "agent missing": (lambda raw: raw.pop("agent"),
                      [{"path": "agent", "code": "AGENT_IDENTITY_REQUIRED"}]),
    "no picks": (lambda raw: raw.update(picks=[]), [{"path": "picks", "code": "TOO_SHORT"}]),
    "31 picks": (lambda raw: raw.update(picks=[pick(i) for i in range(31)]),
                 [{"path": "picks", "code": "TOO_LONG"}]),
    "201 skipped": (lambda raw: raw.update(skipped=[
        {"symbol": f"S{i:03}/USD", "reason": "No setup."} for i in range(201)]),
        [{"path": "skipped", "code": "TOO_LONG"}]),
    "skipped reason too long": (lambda raw: raw["skipped"][0].update(reason="x" * 201),
                                [{"path": "skipped[0].reason", "code": "STRING_TOO_LONG"}]),
    "skipped twice": (lambda raw: raw.update(skipped=[
        {"symbol": "C39/USD", "reason": "No setup."}, {"symbol": "C39/USD", "reason": "Again."}]),
        [{"path": "skipped[1].symbol", "code": "DUPLICATE_SKIPPED_SYMBOL"}]),
    "skipped and picked": (lambda raw: raw.update(skipped=[
        {"symbol": "C00/USD", "reason": "Changed my mind."}]),
        [{"path": "skipped[0].symbol", "code": "SKIPPED_SYMBOL_ALSO_PICKED"}]),
    "unknown envelope field": (lambda raw: raw.update(size_usd="1000"),
                               [{"path": "size_usd", "code": "EXTRA_FORBIDDEN"}]),
    "missing run slot": (lambda raw: raw.pop("run_slot"),
                         [{"path": "run_slot", "code": "MISSING"}]),
    "naive timestamp": (lambda raw: raw.update(generated_at="2026-09-26T12:30:00"),
                        [{"path": "generated_at", "code": "RFC3339_TIMESTAMP_REQUIRED"}]),
}


@pytest.mark.parametrize("case", sorted(ENVELOPE))
def test_envelope_rules_refuse_the_whole_report_with_paths_and_codes(lab, case):
    mutate, errors = ENVELOPE[case]
    raw = report_v3([pick(0), pick(1)])
    mutate(raw)
    reads = []
    with pytest.raises(ResearchReportRejected) as caught:
        lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake(reads=reads))
    assert (caught.value.code, list(caught.value.errors)) == ("INVALID_MUSE_REPORT", errors)
    assert reads == []  # A refused envelope never reads the universe.
    if "agent" in raw:
        assert events(lab.cycle, cycle_of(raw)) == []


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda raw: raw["picks"].append(pick(1, raw["picks"][0]["symbol"])),
     "DUPLICATE_SYMBOL_IN_REPORT"),
    (lambda raw: raw["picks"].append(pick(1, signal_id=raw["picks"][0]["signal_id"])),
     "DUPLICATE_SIGNAL_IN_REPORT"),
    (lambda raw: raw["picks"][0]["reasoning"].update(risks="Mail ops@example.com"),
     "SENSITIVE_EVIDENCE_REJECTED"),
    (lambda raw: raw["picks"][0]["reasoning"].update(
        thesis="Contract 0x" + "ab" * 20 + " is the token."), "SENSITIVE_EVIDENCE_REJECTED"),
], ids=["duplicate_symbol", "duplicate_signal", "email", "contract_address"])
def test_duplicates_and_sensitive_content_refuse_the_whole_report(lab, mutate, code):
    raw = report_v3([pick(0)])
    mutate(raw)
    with pytest.raises(ResearchReportRejected) as caught:
        lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    assert caught.value.code == code
    assert events(lab.cycle, cycle_of(raw)) == []


def test_stale_future_and_expired_reports_are_refused(lab):
    stale = report_v3([pick(0)], generated_at=(NOW - timedelta(seconds=61)).isoformat())
    stale["context_as_of"] = (NOW - timedelta(minutes=5)).isoformat()
    stale["picks"][0]["agent_price_at"] = (NOW - timedelta(minutes=2)).isoformat()
    with pytest.raises(ValueError, match="^RESEARCH_REPORT_STALE_OR_FUTURE$"):
        lab.cycle.start_report(stale, max_seconds=86400, v3=v3_intake())
    future = report_v3([pick(0)], generated_at=(NOW + timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError, match="^RESEARCH_REPORT_STALE_OR_FUTURE$"):
        lab.cycle.start_report(future, max_seconds=86400, v3=v3_intake())
    lab.now[0] = NOW + timedelta(seconds=30)
    expired = report_v3([pick(0)], valid_until=(NOW + timedelta(seconds=20)).isoformat())
    with pytest.raises(ValueError, match="^RESEARCH_REPORT_EXPIRED$"):
        lab.cycle.start_report(expired, max_seconds=86400, v3=v3_intake())
    for raw in (stale, future, expired):
        assert events(lab.cycle, cycle_of(raw)) == []


def test_exact_retry_replays_without_a_universe_read_and_changed_content_is_refused(lab):
    raw = report_v3([pick(0), pick(1, "USDT/USD")])
    first = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
    reads = []
    lab.now[0] = NOW + timedelta(minutes=5)  # Past the 60-second report age: still a replay.
    again = lab.cycle.start_report(copy.deepcopy(raw), max_seconds=86400,
                                   v3=v3_intake(reads=reads))
    assert again == {**first, "idempotent_replay": True} and reads == []
    changed = copy.deepcopy(raw)
    changed["picks"][0]["agent_confidence"] = "0.9"
    with pytest.raises(ValueError, match="^REPORT_IDEMPOTENCY_CONTENT_MISMATCH$"):
        lab.cycle.start_report(changed, max_seconds=86400, v3=v3_intake())


def test_missing_capabilities_answer_unavailable_and_store_nothing(lab):
    raw = report_v3([pick(0)])
    for v3, code in (
        (None, "RESEARCH_V3_INTAKE_NOT_CONFIGURED"),
        (v3_intake(schedule=None), "RESEARCH_SCHEDULE_NOT_CONFIGURED"),
        (V3Intake(SCHEDULE, lambda: None), "RESEARCH_UNIVERSE_UNAVAILABLE"),
    ):
        with pytest.raises(ResearchCapabilityUnavailable) as caught:
            lab.cycle.start_report(raw, max_seconds=86400, v3=v3)
        assert caught.value.code == code
    assert events(lab.cycle, cycle_of(raw)) == []
    with pytest.raises(ValueError, match="^EXPLICIT_REPORT_DEADLINE_REQUIRED$"):
        lab.cycle.start_report(raw, max_seconds=86401, v3=v3_intake())


def test_v2_and_legacy_reports_never_read_the_v3_dependencies(lab):
    from tests.test_review_dossier import item as v2_item
    from tests.test_review_dossier import report as v2_report

    reads = []
    result = lab.cycle.start_report(v2_report([v2_item(0, now=NOW)], now=NOW), max_seconds=300,
                                    v3=v3_intake(reads=reads))
    assert reads == [] and "report_schema_version" not in result and "run_slot" not in result
    assert set(result["item_results"][0]) == {"index", "signal_id", "status", "item_key",
                                              "revision", "evidence_hash", "dossier_bytes"}


# --- HTTP ---------------------------------------------------------------------------------------

def client(cycle, v3, **kwargs):
    return TestClient(create_managed_app(
        cycle, cycle.store, api_token=LEGACY, runtime_status=lambda: {},
        report_submit=lambda raw: asyncio.to_thread(
            cycle.start_report, raw, max_seconds=86400, v3=v3),
        status_token=STATUS, operator_token=OPERATOR, agent_tokens=AGENTS, **kwargs,
    ))


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_http_v3_intake_binds_the_agent_and_maps_refusals(lab):
    web = client(lab.cycle, v3_intake())
    raw = report_v3([pick(0), pick(1, "USDT/USD")])
    reply = web.post(ROUTE, json=raw, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202
    body = reply.json()
    assert body["report_schema_version"] == REPORT_SCHEMA_V3 and body["agent_id"] == AGENT
    assert [r["status"] for r in body["item_results"]] == ["ACCEPTED", "REJECTED"]
    # Another agent's credential, or the legacy one, cannot submit for this agent.
    other = report_v3([pick(0)])
    for token in (AGENTS["instinct"], LEGACY):
        reply = web.post(ROUTE, json=other, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    assert events(lab.cycle, cycle_of(other)) == []
    for token in (STATUS, OPERATOR):
        assert web.post(ROUTE, json=other, headers=bearer(token)).status_code == 403
    missing = report_v3([pick(0)])
    missing.pop("agent")
    reply = web.post(ROUTE, json=missing, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 422 and reply.json() == {
        "detail": "INVALID_MUSE_REPORT",
        "errors": [{"path": "agent", "code": "AGENT_IDENTITY_REQUIRED"}]}
    slot = report_v3([pick(0)])
    slot_shift(1)(slot)
    reply = web.post(ROUTE, json=slot, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 422 and reply.json()["errors"] == [
        {"path": "run_slot", "code": "RUN_SLOT_NOT_SCHEDULED"}]
    refused = report_v3([pick(0, "USDT/USD")])
    reply = web.post(ROUTE, json=refused, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 422 and reply.json()["detail"] == "SYMBOL_NOT_IN_UNIVERSE"
    assert "USDT" not in reply.text  # Paths and codes only.
    # The legacy credential may report V3 for its own agent, ``muse``.
    muse = report_v3([pick(0)], agent_id="muse")
    assert web.post(ROUTE, json=muse, headers=bearer(LEGACY)).status_code == 202
    assert declared_agent(muse)["agent_id"] == "muse"


def test_http_v3_without_schedule_or_universe_is_503_with_nothing_stored(lab):
    raw = report_v3([pick(0)])
    for v3, code in ((v3_intake(schedule=None), "RESEARCH_SCHEDULE_NOT_CONFIGURED"),
                     (V3Intake(SCHEDULE, lambda: None), "RESEARCH_UNIVERSE_UNAVAILABLE")):
        reply = client(lab.cycle, v3).post(ROUTE, json=raw, headers=bearer(AGENTS["claude"]))
        assert (reply.status_code, reply.json()) == (503, {"detail": code})
    assert events(lab.cycle, cycle_of(raw)) == []
    context = client(lab.cycle, v3_intake()).get("/api/v1/lab/research-context",
                                                 headers=bearer(AGENTS["claude"]))
    assert (context.status_code, context.json()) == (
        503, {"detail": "RESEARCH_CONTEXT_NOT_CONFIGURED"})


# --- End to end through admission, under the default rule and under B2 -------------------------

def v3_on_venue(mx, picks_by_symbol, *, rule=None, provider=None, agent_id=AGENT):
    engine, venue, receipts = mx
    now = venue.now
    calls = []

    def default(request):
        body = strict_json(request.content)
        calls.append(body)
        quality = set(body["questions"]) == set(QUALITY.questions)
        return httpx.Response(200, json=quality_response() if quality else response())

    reviewer = JevReviewer(
        receipts, ReliabilityPolicy("RESEARCH_V3_VENUE_FIXTURE", 10, 2, 0.01, 1000, 30),
        transport=httpx.MockTransport(provider or default), key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now, sleep=no_sleep,
    )
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now, selection=rule)
    if rule is not None and rule.floored:
        cycle.record_selection_rule(runtime_id=str(uuid4()))
    picks = [pick(i, symbol, now=now) for i, symbol in enumerate(picks_by_symbol)]
    raw = report_v3(picks, now=now, agent_id=agent_id)
    result = cycle.start_report(raw, max_seconds=86400, v3=v3_intake(
        universe=set(picks_by_symbol), now=now))
    assert result["contender_count"] == len(picks)
    return cycle, cycle_of(raw), calls


def admission_failures(engine, chosen):
    with engine.repo.connect() as conn:
        return [conn.execute("SELECT lab.managed_review_failure(%s) AS reason",
                             (Jsonb(json_safe(p)),)).fetchone()["reason"] for p in chosen]


def test_v3_selection_crosses_admission_sql_and_its_entry_is_authorized(mx):
    engine, venue, _ = mx
    cycle, cycle_id, calls = v3_on_venue(mx, ["AAA/USD", "BBB/USD"])
    asyncio.run(cycle.tick(cycle_id))
    chosen = cycle.approved_packets(cycle_id)
    assert [p["symbol"] for p in chosen] == ["AAA/USD", "BBB/USD"]
    assert all(set(c["questions"]) == set(SKEPTIC.questions) for c in calls)
    assert admission_failures(engine, chosen) == [None, None]
    packet = chosen[0]
    classify(engine, packet["symbol"])
    # Package system-check: a V3 packet is admitted only with a live price (a pullback here:
    # entry 100 is 0.5% under the mid 100.50, the agent's own price).
    live = LiveQuote(packet["symbol"], D("100.49"), D("100.51"), venue.now, "ALPACA_STREAM",
                     venue.now)
    sid = engine.admit(packet, live_quote=lambda symbol: live)
    assert engine._load(sid)[1]["entry_type"] == "PULLBACK"
    trigger = D(packet["levels"]["entry_trigger"])
    risk = engine.observe_trigger(sid, observation(
        mx, trade_price=str(trigger), bid=str(trigger - D(".01")), ask=str(trigger + D(".01"))))
    assert risk["outcome"] == "APPROVED"  # Dispatch re-ran lab.managed_review_failure.
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == packet["symbol"])
    assert D(entry["limit_price"]) == D(packet["levels"]["max_entry_price"])
    with engine.repo.connect() as conn:
        record = conn.execute("SELECT record_json FROM lab.managed_setups WHERE setup_id=%s",
                              (sid,)).fetchone()["record_json"]
    assert record["state"] == packet["state"] and record["agent"]["agent_id"] == AGENT
    assert record["report_schema_version"] == REPORT_SCHEMA_V3
    assert "agent_confidence" not in encoded(record["state"])


def test_v3_cycles_follow_a_configured_b2_rule_to_admission(mx):
    engine, _, _ = mx
    calls = []
    script = {"AAA/USD": ("NO", "LOW", "NO", "YES", "SUPPORTED", "APPROVE"),
              "BBB/USD": ("NO", "LOW", "NO", "NO", "SUPPORTED", "APPROVE")}
    provider = replay_script.scripted_provider({}, {"AAA/USD": "STRONG"}, calls, script)
    cycle, cycle_id, _ = v3_on_venue(mx, list(script), rule=SelectionRule(B2_POLICY, "ADEQUATE"),
                                     provider=provider)
    asyncio.run(cycle.tick(cycle_id))
    asyncio.run(cycle.tick(cycle_id))
    chosen = cycle.approved_packets(cycle_id)
    assert [p["symbol"] for p in chosen] == ["AAA/USD"]
    assert chosen[0]["selection_policy"] == B2_POLICY
    assert chosen[0]["question_set_version"] == SKEPTIC_V2.version
    skeptic = [c for c in calls if set(c["questions"]) == set(SKEPTIC_V2.questions)]
    assert len(skeptic) == 2 and all("rationale" in c["state"] for c in skeptic)
    assert any(set(c["questions"]) == set(QUALITY_V2.questions) for c in calls)
    assert admission_failures(engine, chosen) == [None]
