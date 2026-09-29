"""REVIEW_DOSSIER_V1: the code-built selection-Jev input and its byte budget.

Fixture evidence only: disposable PostgreSQL, a mock Jev transport, no broker calls.
"""

import asyncio
import copy
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from catalyst_lab.agent_identity import agent_cycle_id
from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_SHA256, MUSE_GUIDELINES_VERSION
from catalyst_lab.muse_reports import REPORT_SCHEMA_V2, parse_report
from catalyst_lab.repository import json_safe
from catalyst_lab.research_dossier import (
    JEV_STATE_CAP_BYTES,
    RATIONALE_BUDGET_BYTES,
    STATE_BUDGET_BYTES,
    DossierRejected,
    compile_review_dossier,
    encoded_bytes,
    review_wrappers,
    wrapper_overhead_bytes,
)
from catalyst_lab.research_evidence import canonical_sources
from catalyst_lab.research_ranking import QUALITY, QUALITY_POLICY
from catalyst_lab.technical_evidence import TechnicalEvidence
from tests.test_managed_app import body
from tests.test_research_cycle import NOW
from tests.test_research_cycle import runtime as runtime

ROOT = Path(__file__).resolve().parents[1]
CONFIDENCE_MARKER = "CONFIDENCE-BASIS-MARKER"


def level_bars(count):
    """Bars whose fields equal body()'s levels: target high 111, stop low 104, entry open 106."""
    return {"target": 5, "stop": count - 20, "entry_trigger": count - 2}


def observed_bars(count=64, now=NOW):
    bars = [
        {
            "bar_id": f"bar-{i:02}",
            "started_at": (now - timedelta(minutes=count + 1 - i)).isoformat(),
            "open": "105.5", "high": "106.2", "low": "104.5", "close": "105.8",
            "volume": str(1000 + i),
        }
        for i in range(count)
    ]
    index = level_bars(count)
    bars[index["target"]]["high"] = "111"
    bars[index["stop"]]["low"] = "104"
    bars[index["entry_trigger"]]["open"] = "106"
    return bars


def technical(count=64, now=NOW):
    index = level_bars(count)
    return {
        "schema_version": "MUSE_OBSERVED_TECHNICALS_V1", "provider": "LAB_FIXTURE",
        "venue": "FIXTURE", "feed": "FIXTURE_MINUTES",
        "source_url": "https://market.example/bars", "retrieved_at": now.isoformat(),
        "timeframe_seconds": 60, "bars": observed_bars(count, now),
        "quote": {"observed_at": now.isoformat(), "bid": "105.99", "ask": "106.01"},
        "level_references": {
            "entry_trigger": {"bar_id": f"bar-{index['entry_trigger']:02}", "field": "open",
                              "rationale": "Retest of the completed-bar opening print."},
            "stop": {"bar_id": f"bar-{index['stop']:02}", "field": "low",
                     "rationale": "Completed-bar support low."},
            "target": {"bar_id": f"bar-{index['target']:02}", "field": "high",
                       "rationale": "Prior completed-bar high."},
        },
    }


def rationale(bar_ids=()):
    claims = [
        {"claim_id": "C1", "kind": "CATALYST",
         "text": "The issuer launched the product for paying customers.",
         "supported_by": {"source_ids": ["issuer"], "bar_ids": []}},
        {"claim_id": "C2", "kind": "ECONOMIC_LINK",
         "text": "Paid availability turns the launch into product revenue.",
         "supported_by": {"source_ids": ["issuer"]}},
    ]
    if bar_ids:
        claims.append({"claim_id": "C3", "kind": "TECHNICAL",
                       "text": "Support held on the cited completed bar.",
                       "supported_by": {"bar_ids": list(bar_ids)}})
    return {
        "claims": claims,
        "why_now": "The launch was published one hour before the report was generated.",
        "why_these_levels": "Entry, stop and target are the cited open, low and high.",
        "why_over_peers": "Ranked above TESTB, whose catalyst lacks a primary source.",
        "what_would_change_my_mind": "An issuer withdrawal or a completed bar under the stop.",
        "known_risks": ["Thin volume on the cited bars."],
        "agent_confidence": {"level": "MEDIUM",
                             "basis": CONFIDENCE_MARKER + " direct source, thin volume."},
    }


def item(index=0, *, bars=None, rationale_bars=(), excerpts=None, with_rationale=True,
         now=NOW, market="US_STOCKS"):
    value = copy.deepcopy(body()["items"][0])
    value["signal_id"] = f"TEST-DOSSIER-{index:02}"
    value["symbol"] = f"DSR{index:02}" if market == "US_STOCKS" else f"DSR{index:02}/USD"
    value["market"] = market
    for source in value["sources"]:
        source["retrieved_at"] = now.isoformat()
        source["published_at"] = (now - timedelta(hours=1)).isoformat()
    if excerpts:
        value["sources"] += [
            {"source_id": f"filing-{n}", "url": f"https://issuer.example/filing/{n}",
             "excerpt": "Filing text. " * (size // 13) + "x" * (size % 13),
             "published_at": None, "retrieved_at": now.isoformat()}
            for n, size in enumerate(excerpts)
        ]
    if bars:
        value["technical_evidence"] = technical(bars, now)
    if with_rationale:
        value["selection_rationale"] = rationale(rationale_bars)
    return value


def fixture_agent(agent_id="muse", version="FIXTURE-1"):
    """An AGENT_RESEARCH_REPORT_V2 agent block; ``muse`` is what the legacy token acts for."""
    return {"agent_id": agent_id, "agent_version": version,
            "guidelines_version": MUSE_GUIDELINES_VERSION,
            "guidelines_sha256": MUSE_GUIDELINES_SHA256, "run_id": str(uuid4())}


def report(items, *, v2=True, now=NOW, agent=None):
    raw = {
        "report_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=5)).isoformat(),
        "items": items,
    }
    if v2:
        raw["schema_version"] = REPORT_SCHEMA_V2
        raw["agent"] = agent or fixture_agent()
    return raw


def cycle_id_of(raw):
    """A V2 cycle is keyed by agent and report ID; a legacy cycle by the report ID."""
    agent = raw.get("agent")
    return agent_cycle_id(agent["agent_id"], raw["report_id"]) if agent else raw["report_id"]


def dossier_of(raw, index=0, *, now=NOW):
    parsed = parse_report(raw).items[index]
    assert parsed.code is None, (parsed.code, parsed.errors)
    return compile_review_dossier(parsed, now=now)


def fingerprint():
    """Hash of one fixed dossier and manifest; compared across interpreter hash seeds."""
    raw = report([item(0, bars=64, rationale_bars=("bar-61",), excerpts=(900, 700))])
    raw["report_id"] = "7a3cbe9e-38c2-4f52-9e0c-9d6c1c0f0a11"
    dossier = dossier_of(raw)
    return digest(encoded(dossier.state)) + ":" + digest(encoded(dossier.manifest))


def test_state_budget_leaves_room_for_every_measured_review_wrapper(runtime):
    cycle, _, calls, _ = runtime
    # research_dossier mirrors jev_review's cap: a state one byte over it is refused.
    _privacy_check("x" * JEV_STATE_CAP_BYTES)
    with pytest.raises(ValueError, match="^EVIDENCE_TOO_LONG$"):
        _privacy_check("x" * (JEV_STATE_CAP_BYTES + 1))
    # Eleven approvals exceed the ten open slots, so the QUALITY wrapper is exercised too.
    raw = report([item(i, bars=20, rationale_bars=("bar-10",)) for i in range(11)])
    cycle.start_report(raw, max_seconds=300)
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    packets = [
        e["body"] for e in cycle.outputs(cycle_id_of(raw)) if e["kind"] == "RESEARCH_PACKET"
    ]
    measured = {}
    for call in calls:
        name = QUALITY_POLICY if set(call["questions"]) == set(QUALITY.questions) else (
            SKEPTIC.version
        )
        dossier = call["state"]["candidate"] if name == QUALITY_POLICY else call["state"]
        packet = next(p for p in packets if p["state"] == dossier)
        expected = review_wrappers(packet["state"])[name]
        if name == QUALITY_POLICY:
            expected = {**expected, "muse_rank": packet["rank"]}
        assert call["state"] == expected
        overhead = encoded_bytes(call["state"]) - encoded_bytes(dossier)
        measured[name] = max(measured.get(name, 0), overhead)
        # The reviewer never receives the agent's own confidence, on any path.
        assert CONFIDENCE_MARKER not in encoded(call) and "agent_confidence" not in encoded(call)
    assert measured == {SKEPTIC.version: 0, QUALITY_POLICY: 29} == {
        name: encoded_bytes(state) - encoded_bytes({})
        for name, state in review_wrappers({}).items()
    }
    assert wrapper_overhead_bytes() == 29
    # Budget = cap minus the largest wrapper, rounded down with headroom for a later
    # wrapper (for example a second-stage review that also carries first-stage answers).
    assert STATE_BUDGET_BYTES + wrapper_overhead_bytes() < JEV_STATE_CAP_BYTES
    assert JEV_STATE_CAP_BYTES - wrapper_overhead_bytes() - STATE_BUDGET_BYTES == 971
    assert RATIONALE_BUDGET_BYTES == 3_000 and STATE_BUDGET_BYTES == 11_000


def test_sixty_four_bars_no_longer_consume_the_review_budget():
    small = dossier_of(report([item(0, bars=20, rationale_bars=("bar-17",),
                                    excerpts=(1200,) * 5)]))
    large = dossier_of(report([item(0, bars=64, rationale_bars=("bar-61",),
                                    excerpts=(1200,) * 5)]))
    facts = large.state["technical_context"]["observed_facts"]
    included = ["bar-05", "bar-44", "bar-61", "bar-62"]  # target, stop, claim, entry
    assert [bar["bar_id"] for bar in facts["observations"]["bars"]] == included
    assert facts["observations"]["bar_count"] == 64
    assert large.manifest["bars"]["included_bar_ids"] == included
    assert large.manifest["bars"]["omitted_bar_ids"] == [
        f"bar-{i:02}" for i in range(64) if f"bar-{i:02}" not in included
    ]
    # 6,059 excerpt characters (the fixture's source plus five 1,200-character filings)
    # reach the reviewer untruncated, with room left for the rationale.
    assert sum(len(s["excerpt"]) for s in large.state["sources"]) == 6059
    assert large.manifest["state_bytes"] <= STATE_BUDGET_BYTES
    assert abs(large.manifest["state_bytes"] - small.manifest["state_bytes"]) < 100
    # Metrics and the observation hash still cover every submitted bar.
    levels = parse_report(report([item(0)])).items[0].contender.levels
    full = TechnicalEvidence.model_validate(technical(64)).computed(now=NOW, levels=levels)
    assert facts["metrics"] == full["metrics"]
    assert facts["observations_hash"] == full["observations_hash"]
    # The previous representation embedded all 64 bars and exceeded Jev's hard cap.
    embedded = {**large.state, "technical_context": {
        **large.state["technical_context"], "observed_facts": full,
    }}
    assert encoded_bytes(embedded) > JEV_STATE_CAP_BYTES


@pytest.mark.parametrize("excerpts", [(1134,) * 6 + (1137,), (1200,) * 6 + (741,)])
def test_plan_maximum_excerpts_with_64_bars_is_rejected_not_truncated(excerpts):
    # 8,000 excerpt characters (the fixture's 59-character source plus these) is the
    # schema maximum. Excerpts plus canonical per-source metadata alone exceed 9,600
    # bytes, so with technical evidence the dossier cannot fit; it is never truncated.
    raw = report([item(0, bars=64, excerpts=excerpts, with_rationale=False)], v2=False)
    parsed = parse_report(raw).items[0]
    assert parsed.code is None
    assert sum(len(s.excerpt) for s in parsed.contender.sources) == 8000
    sources = json_safe(canonical_sources(parsed.contender.sources, now=NOW))
    assert encoded_bytes(sources) > 9600
    with pytest.raises(DossierRejected, match="^DOSSIER_OVER_BUDGET$") as caught:
        compile_review_dossier(parsed, now=NOW)
    [error] = caught.value.errors
    assert error["path"] == "items[0]" and error["budget_bytes"] == STATE_BUDGET_BYTES
    assert error["bytes"] > STATE_BUDGET_BYTES


def test_dossier_bytes_are_identical_across_runs_and_serializations():
    raw = report([item(0, bars=64, rationale_bars=("bar-61",), excerpts=(900, 700))])
    first = dossier_of(copy.deepcopy(raw))
    second = dossier_of(json.loads(json.dumps(raw)))
    reordered = json.loads(json.dumps(raw, sort_keys=True))
    source = reordered["items"][0]["sources"][0]
    reordered["items"][0]["sources"][0] = dict(reversed(source.items()))
    third = dossier_of(reordered)
    assert encoded(first.state) == encoded(second.state) == encoded(third.state)
    assert first.manifest == second.manifest == third.manifest
    assert first.evidence_hash == first.manifest["state_sha256"] == digest(encoded(first.state))
    seeds = {
        subprocess.run(
            [sys.executable, "-c",
             "from tests.test_review_dossier import fingerprint; print(fingerprint())"],
            cwd=ROOT, env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True, text=True, check=True, timeout=120,
        ).stdout.strip()
        for seed in ("1", "2")
    }
    assert seeds == {fingerprint()}


def test_non_ascii_text_is_budgeted_as_the_escaped_bytes_jev_receives():
    accented = item(0)
    accented["thesis"] = "Lancement confirmé " + "é" * 900  # 919 characters
    dossier = dossier_of(report([accented]))
    text = encoded(dossier.state)
    assert "\\u00e9" in text and "é" not in text
    assert dossier.manifest["state_bytes"] == len(text.encode()) == encoded_bytes(dossier.state)
    assert dossier.manifest["sections"]["thesis"]["bytes"] == 2 + 18 + 6 * 901
    # Raw UTF-8 would undercount what the reviewer is sent by 4 bytes per "é".
    utf8 = json.dumps(dossier.state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(utf8.encode()) == dossier.manifest["state_bytes"] - 4 * 901
    assert encoded_bytes("😀") == 14  # Astral characters travel as a 12-byte surrogate pair.
    # Three narrative fields of 1,000 characters each pass the character limits but
    # are 18,000 escaped bytes: the item is over budget, not truncated.
    over = item(0)
    for field in ("thesis", "disproof", "economic_relationship"):
        over[field] = "é" * 1000
    with pytest.raises(DossierRejected, match="^DOSSIER_OVER_BUDGET$") as caught:
        dossier_of(report([over]))
    assert caught.value.errors[0]["bytes"] > 18_000


def test_over_budget_item_is_rejected_by_index_while_siblings_are_reviewed(runtime):
    cycle, _, calls, _ = runtime
    items = [item(i) for i in range(3)]
    for field in ("thesis", "disproof", "economic_relationship"):
        items[1][field] = "é" * 1000
    raw = report(items)
    result = cycle.start_report(raw, max_seconds=300)
    assert (result["contender_count"], result["rejected_count"], result["submitted_count"]) == (
        2, 1, 3
    )
    assert [r["status"] for r in result["item_results"]] == ["ACCEPTED", "REJECTED", "ACCEPTED"]
    rejected = result["item_results"][1]
    assert rejected["code"] == "DOSSIER_OVER_BUDGET" and rejected["signal_id"] == "TEST-DOSSIER-01"
    [error] = rejected["errors"]
    assert error["path"] == "items[1]" and error["bytes"] > STATE_BUDGET_BYTES
    assert "é" not in json.dumps(result, ensure_ascii=False)  # Paths and codes, no values.
    events = cycle.outputs(cycle_id_of(raw))
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "RESEARCH_STARTED"
    assert kinds.count("RESEARCH_PACKET") == kinds.count("RESEARCH_DOSSIER") == 2
    [rejection] = [e["body"] for e in events if e["kind"] == "RESEARCH_ITEM_REJECTED_AT_INTAKE"]
    assert (rejection["index"], rejection["signal_id"], rejection["code"]) == (
        1, "TEST-DOSSIER-01", "DOSSIER_OVER_BUDGET"
    )
    assert rejection["item_sha256"] == digest(encoded(json_safe(items[1])))
    packets = [e["body"] for e in events if e["kind"] == "RESEARCH_PACKET"]
    assert [p["rank"] for p in packets] == [1, 3]  # Report order is kept, never renumbered.
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    assert len(calls) == 2


def test_reviewer_receives_the_stored_dossier_that_the_manifest_describes(runtime):
    cycle, _, calls, _ = runtime
    raw = report([item(0, bars=64, rationale_bars=("bar-61",), excerpts=(900, 700))])
    cycle.start_report(raw, max_seconds=300)
    asyncio.run(cycle.tick(cycle_id_of(raw)))
    events = cycle.outputs(cycle_id_of(raw))
    packet = next(e["body"] for e in events if e["kind"] == "RESEARCH_PACKET")
    recorded = next(e["body"] for e in events if e["kind"] == "RESEARCH_DOSSIER")
    started = next(e["body"] for e in events if e["kind"] == "RESEARCH_STARTED")
    [call] = calls
    assert call["state"] == packet["state"]
    manifest = recorded["manifest"]
    assert manifest["dossier_version"] == "REVIEW_DOSSIER_V1"
    assert manifest["state_sha256"] == packet["evidence_hash"] == recorded["evidence_hash"]
    assert manifest["state_sha256"] == digest(encoded(call["state"]))
    assert manifest["state_bytes"] == encoded_bytes(call["state"]) <= STATE_BUDGET_BYTES
    assert manifest["sections"] == {
        key: {"bytes": encoded_bytes(value), "sha256": digest(encoded(value))}
        for key, value in call["state"].items()
    }
    assert manifest["truncated"] == []
    omitted = {entry["field"]: entry for entry in manifest["omitted"]}
    assert set(omitted) == {
        "direction", "signal_id", "technical_evidence.bars",
        "selection_rationale.agent_confidence",
    }
    assert omitted["technical_evidence.bars"]["count"] == 60
    assert manifest["bars"]["included_bar_ids"] == ["bar-05", "bar-44", "bar-61", "bar-62"]
    assert [s["excerpt"] for s in call["state"]["sources"]] == [
        s["excerpt"] for s in raw["items"][0]["sources"]
    ]
    assert [s["excerpt_chars"] for s in manifest["sources"]] == [59, 900, 700]
    # The full report, all 64 bars included, stays bound by the started event's hash.
    assert recorded["report_hash"] == started["report_hash"]
    assert len(started["report"]["items"][0]["technical_evidence"]["bars"]) == 64
    assert started["dossier_version"] == "REVIEW_DOSSIER_V1"
