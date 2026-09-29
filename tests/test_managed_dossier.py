"""Size-capped position dossier (plan 3.2): the V2 defect, the V3 budget, replay.

Fixture evidence only: synthetic inputs (tests/managed_dossier_fixtures.py), disposable
PostgreSQL clusters and mock provider transports. No provider, broker or owner ledger.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest

import catalyst_lab.managed_review as managed_review
from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy, ReviewResult, _privacy_check
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_dossier import (
    NEWS_EXCERPT_CHARS,
    ContextBudgetUnsatisfiable,
    encoded_bytes,
    plain,
)
from catalyst_lab.managed_measurement import managed_measurement, sampled_excursions
from catalyst_lab.managed_review import (
    CONTEXT_VERSION,
    CONTEXT_VERSION_V2,
    EXIT_POLICY,
    LEGACY_QUESTION_VERSION,
    MANAGED_COHORT,
    QUESTION_VERSION,
    QUESTION_VERSION_V2,
    CompletedBar,
    ManagedContext,
    ManagedPolicy,
    NewsEvidence,
    PositionSnapshot,
    ProtectiveOrder,
    build_managed_context,
    evaluate_managed_result,
    managed_questions,
    review_managed_position,
    verify_managed_receipts,
)
from catalyst_lab.managed_runtime import engineering_monitor_policy
from catalyst_lab.position_monitor import review_bar_window, review_bars_event
from tests import managed_dossier_fixtures as fx
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation
from tests.test_managed_execution import mx as mx
from tests.test_position_monitor import bars as one_bar
from tests.test_position_monitor import context_from_ledger, monitor, opened

ADVERSE = {"ADVERSE", "WITHDRAWN"}
V2_CONTEXT_PIN = "efce3d3bd61f848d5231a3623bde593c6f7fd5352bb9f964592e59421e33c4fb"
V2_QUESTIONS_PIN = "a21d09d7d9c438bc72da54a5b4bf20a2f3e1ce4a4211ce76ab55c964608e1bed"
V1_QUESTIONS_PIN = "8422cc963d209f46959252d070c10b93440f0fe3224b0bc33534386140c59b54"


def build(policy=fx.V3_PRODUCTION_POLICY, *, snapshot=None, bars=None, structural=None,
          news=None, dossier=None, now=fx.NOW):
    """The runtime's call shape: one bar window passed as both recent and structural."""
    window = fx.bars(64) if bars is None else bars
    current, original = fx.news() if news is None else news
    return build_managed_context(
        snapshot or fx.snapshot(), window, current, policy, now=now,
        review_deadline=now + timedelta(seconds=10),
        structural_bars=window if structural is None else structural,
        original_news=original, dossier=fx.v3_dossier() if dossier is None else dossier,
        trigger=fx.trigger(),
    )


def at_budget(budget, **changes):
    return build(replace(fx.V3_PRODUCTION_POLICY, state_byte_budget=budget), **changes)


def entries(context_or_manifest, section=None):
    manifest = context_or_manifest if isinstance(context_or_manifest, dict) \
        else context_or_manifest.data["manifest"]
    return [e for e in manifest["entries"] if section is None or e["section"] == section]


def light(*, long_texts=False, history=2):
    """Short texts, so the full 64-bar window fits without any budget reduction."""
    dossier = fx.v3_dossier()
    research = dossier["original_research"]
    if not long_texts:
        research["economic_relationship"] = "Fixture supply agreement drives unit revenue."
        research["technical_context"]["analysis"] = "Fixture retest held on rising volume."
    research["rationale"] = {
        "status": "UNVERIFIED_PROPOSER_CLAIMS",
        "claims": [{"claim_id": "c1", "kind": "CATALYST", "text": "Fixture agreement signed.",
                    "supported_by": {"source_ids": ["source-original-0"], "bar_ids": []}}],
        "why_now": "Fixture filing today.", "why_these_levels": "Fixture retest level.",
        "why_over_peers": "Fixture only.", "what_would_change_my_mind": "Fixture retraction.",
        "known_risks": [],
    }
    dossier["management_history"] = fx.history(history)
    current = tuple(
        fx.news_item(f"light-{i}", stance, origin="CURRENT_EVIDENCE",
                     length=1200 if long_texts and i == 0 else 160)
        for i, stance in enumerate(("SUPPORTS", "ADVERSE"))
    )
    snapshot = fx.snapshot(thesis="Fixture thesis: agreement lifts demand.",
                           disproof="Fixture disproof: agreement withdrawn.")
    return {"snapshot": snapshot, "news": (current, ()), "dossier": dossier}


# -- the defect and the fix -------------------------------------------------------------


def test_production_sized_v2_context_overflows_the_review_cap(monkeypatch):
    """The verified defect: at runtime size every V2 review fails EVIDENCE_TOO_LONG."""
    measured = []
    check = managed_review._privacy_check

    def recording(text):
        measured.append(len(text.encode()))
        return check(text)

    monkeypatch.setattr(managed_review, "_privacy_check", recording)
    window = fx.bars(64)
    current, original = fx.news()
    common = {"now": fx.NOW, "review_deadline": fx.NOW + timedelta(seconds=10),
              "trigger": fx.trigger()}
    # The monitor splits the runtime window: the newest 20 recent, the rest structural.
    with pytest.raises(ValueError, match="EVIDENCE_TOO_LONG"):
        build_managed_context(
            fx.snapshot(), window[-20:], current, fx.V2_PRODUCTION_POLICY,
            structural_bars=window[:-20], original_news=original, dossier=fx.v2_dossier(),
            **common,
        )
    assert measured[-1] > 3 * 12_000
    # The bar window alone, with one-line texts and nothing else, already overflows.
    with pytest.raises(ValueError, match="EVIDENCE_TOO_LONG"):
        build_managed_context(
            fx.snapshot(thesis="Short.", disproof="Short."), window[-20:], (),
            fx.V2_PRODUCTION_POLICY, structural_bars=window[:-20], **common,
        )
    per_bar = sum(len(encoded(managed_review._json(asdict(b)))) for b in window) / len(window)
    assert per_bar > 240
    # The runtime passes one window as both lists; V2 refuses the overlap outright.
    with pytest.raises(ValueError, match="DUPLICATE_BAR"):
        build_managed_context(
            fx.snapshot(), window[-20:], (), fx.V2_PRODUCTION_POLICY,
            structural_bars=window, **common,
        )


def test_runtime_call_shape_fails_under_v2_and_succeeds_under_v3(mx):
    """PositionMonitorLoop passes one 60-bar window as both lists; V2 cannot send it."""
    engine, venue, _ = mx
    sid = opened(mx)
    quote = observation(mx, bid="108", ask="108.01")
    window = fx.monitor_bars(venue.now)
    retired, v2_calls = monitor(mx, "HOLD", policy=fx.V2_PRODUCTION_POLICY)
    with pytest.raises(ValueError, match="EVIDENCE_TOO_LONG"):
        asyncio.run(retired.review(sid, quote, window, fresh_observation=lambda: quote,
                                   structural_bars=window))
    assert v2_calls == []
    current, v3_calls = monitor(mx, "HOLD", policy=engineering_monitor_policy())
    result = asyncio.run(current.review(sid, quote, window, fresh_observation=lambda: quote,
                                        structural_bars=window))
    assert result.status == "RECORDED" and len(v3_calls) == 1
    assert len(encoded(v3_calls[0]["state"]).encode()) <= 11_000


@pytest.mark.parametrize("count", [60, 64])
def test_production_sized_v3_context_fits_the_budget(count):
    ctx = build(bars=fx.bars(count))
    state_json = encoded(ctx.state)
    size = len(state_json.encode())
    manifest = ctx.data["manifest"]
    assert size <= manifest["state_byte_budget"] == 11_000 < manifest["jev_state_cap_bytes"]
    assert manifest["state_bytes"] == size and manifest["within_budget"]
    assert manifest["state_sha256"] == digest(state_json)
    _privacy_check(state_json)
    assert ctx.data["context_version"] == ctx.state["context_version"] == CONTEXT_VERSION
    assert ctx.data["policy"]["state_byte_budget"] == 11_000
    assert {len(v) for v in ctx.state["eligible_options"].values()} == {5}
    assert managed_questions(ctx).version == QUESTION_VERSION
    # Thesis and disproof are sent once and never truncated.
    assert state_json.count(encoded(ctx.state["thesis"])) == 1
    assert ctx.state["thesis"] == fx.snapshot().thesis
    assert ctx.state["disproof"] == fx.snapshot().disproof


def test_bars_are_column_encoded_once_with_the_structural_subset_marked():
    window = fx.bars(64)
    ctx = build(bars=window, **light())
    table = ctx.state["bars"]
    assert (table["provider"], table["feed"], table["bar_seconds"]) == ("ALPACA", "iex", 60)
    assert table["columns"] == "ref|end_age_seconds|open|high|low|close|volume"
    assert len(table["rows"]) == 64 and table["omitted_rows"] == 0
    refs = [row.split("|")[0] for row in table["rows"]]
    assert refs == [f"S{i:02d}" for i in range(1, 45)] + [f"R{i:02d}" for i in range(1, 21)]
    bars_entry = entries(ctx, "bars")[0]
    assert sorted(bars_entry["refs"].values()) == sorted(b.observation_id for b in window)
    assert bars_entry["structural_refs"] == refs[:44]
    last = window[-1]  # Ended on the minute, 30 s before the review.
    assert table["rows"][-1] == "|".join(["R20", "30", *(plain(getattr(last, k)) for k in (
        "open", "high", "low", "close", "volume"))])
    per_row = encoded_bytes(table["rows"]) / len(table["rows"])
    v2_per_bar = sum(len(encoded(managed_review._json(asdict(b)))) for b in window) / 64
    assert per_row < 48 and v2_per_bar > 5 * per_row
    state_json = encoded(ctx.state)
    assert not any(b.observation_id in state_json for b in window)  # Binding data stays out.
    assert ctx.data["manifest"]["budget_steps"] == []


def test_mixed_bar_durations_show_each_start_and_structural_targets_stay_eligible():
    window = fx.bars(20)
    hourly = replace(window[0], observation_id=fx.ident("hourly-structural"),
                     starts_at=fx.NOW - timedelta(hours=3), ends_at=fx.NOW - timedelta(hours=2),
                     high=D("110.02"))
    ctx = build(bars=window, structural=(hourly,), **light())
    table = ctx.state["bars"]
    assert table["bar_seconds"] is None
    assert table["columns"] == "ref|start_age_seconds|end_age_seconds|open|high|low|close|volume"
    assert table["rows"][0].split("|")[:3] == ["S01", "10800", "7200"]  # Ages, not clocks.
    target = ctx.state["eligible_options"]["target"][0]
    assert (target["option_id"], target["price"], target["bar"]) == ("TARGET_1", "110.02", "S01")
    criteria = managed_questions(ctx).questions["target_option"]["criteria"]["TARGET_1"]
    assert criteria.startswith("Observed high of structural completed bar S01; code-rounded "
                               "eligible level 110.02, 465 ticks (2.45 R) above the bid")


def test_v3_context_is_byte_identical_across_runs_and_input_order():
    first = build()
    assert build().context_json == first.context_json
    window = fx.bars(64)
    assert build(bars=tuple(reversed(window)), structural=window[::2] + window[1::2]) \
        .context_json == first.context_json
    # Fresh interpreters with different hash seeds produce the same bytes.
    assert fx.production_context().context_hash == first.context_hash
    code = "from tests import managed_dossier_fixtures as f; print(f.production_context()" \
        ".context_hash)"
    hashes = {
        subprocess.run(
            [sys.executable, "-c", code], check=True, capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip() for seed in ("1", "2")
    }
    assert hashes == {first.context_hash}


# -- budget ladder invariants -----------------------------------------------------------


def test_adverse_news_is_never_dropped_before_supporting_news():
    current, original = fx.news()  # Six items at the 1,200-character limit, two adverse.
    medium = {**light(), "news": (current, original)}
    adverse = {n.content_hash for n in (*current, *original) if n.stance in ADVERSE}
    supporting = {n.content_hash for n in (*current, *original)} - adverse
    outcomes = []
    for budget in range(12_000, 999, -250):
        try:
            ctx = at_budget(budget, **medium)
        except ContextBudgetUnsatisfiable as exc:
            assert exc.budget == budget < exc.state_bytes and not exc.manifest["within_budget"]
            outcomes.append(None)
            continue
        shown = {e["sha256"]: e for e in entries(ctx) if e["section"].startswith("news.")
                 and e["status"] != "OMITTED"}
        assert adverse <= set(shown), budget  # Adverse and withdrawn evidence is always sent.
        if any(shown[h]["kept_chars"] < min(NEWS_EXCERPT_CHARS, shown[h]["total_chars"])
               for h in adverse):
            assert not supporting & set(shown), budget  # ...and shortened only when alone.
        steps = ctx.data["manifest"]["budget_steps"]
        if "OMIT_SUPPORTING_NEWS" in steps:
            assert steps.index("REDUCE_SUPPORTING_NEWS") < steps.index("OMIT_SUPPORTING_NEWS")
        outcomes.append(len(shown))
    assert outcomes[0] == 6 and None in outcomes  # Full at 12,000; unsatisfiable at the end.
    fitted = [n for n in outcomes if n is not None]
    assert fitted == sorted(fitted, reverse=True)  # The ladder is monotonic.


def test_adverse_and_withdrawn_items_are_selected_first():
    items = [fx.news_item(f"s{i}", "SUPPORTS", origin="CURRENT_EVIDENCE", length=150)
             for i in range(4)]
    items += [fx.news_item("late-adverse", "ADVERSE", origin="CURRENT_EVIDENCE", length=150),
              fx.news_item("late-withdrawn", "WITHDRAWN", origin="CURRENT_EVIDENCE", length=150)]
    base = light()
    ctx = build(snapshot=base["snapshot"], dossier=base["dossier"], news=(tuple(items), ()))
    sent = ctx.state["news"]["current"]
    assert [n["stance"] for n in sent] == ["ADVERSE", "WITHDRAWN", "SUPPORTS", "SUPPORTS"]
    assert [n["source_id"] for n in sent[2:]] == ["source-s0", "source-s1"]
    limited = [e for e in entries(ctx, "news.current") if e.get("reason") == "SECTION_LIMIT"]
    assert {e["source_id"] for e in limited} == {"source-s2", "source-s3"}
    assert all(e["kept_chars"] == 0 for e in limited)


@pytest.mark.parametrize("variant", ["maximal", "medium"])
def test_options_are_never_truncated_or_dropped(variant):
    inputs = {} if variant == "maximal" else {**light(), "news": fx.news()}
    reference = at_budget(12_000, **inputs)
    options = reference.state["eligible_options"]
    assert {len(rows) for rows in options.values()} == {5}
    ids = {kind: {o["option_id"] for o in rows} for kind, rows in options.items()}
    backing = {o["bar"] for rows in options.values() for o in rows}
    fitted = 0
    for budget in range(12_000, 999, -250):
        try:
            ctx = at_budget(budget, **inputs)
        except ContextBudgetUnsatisfiable as exc:
            assert entries(exc.manifest, "eligible_options")[0]["count"] == 10
            continue
        fitted += 1
        assert ctx.state["eligible_options"] == options
        assert backing <= {row.split("|")[0] for row in ctx.state["bars"]["rows"]}
        questions = managed_questions(ctx).questions
        for kind in ("stop", "target"):
            assert set(questions[kind + "_option"]["criteria"]) == {
                "KEEP", INSUFFICIENT, *ids[kind]
            }
    assert fitted >= (5 if variant == "maximal" else 20)


def test_unsatisfiable_budget_raises_with_the_smallest_attempt():
    with pytest.raises(ContextBudgetUnsatisfiable) as caught:
        at_budget(2_000)
    exc = caught.value
    assert str(exc) == exc.code == "CONTEXT_BUDGET_UNSATISFIABLE"
    assert exc.budget == 2_000 < exc.state_bytes == exc.manifest["state_bytes"]
    assert exc.manifest["budget_steps"][-1] == "REDUCE_ADVERSE_NEWS"
    assert entries(exc.manifest, "thesis")[0]["status"] == "INCLUDED"


def test_truncation_carries_hash_markers_and_full_hashes_in_the_manifest():
    inputs = light(long_texts=True)
    research = inputs["dossier"]["original_research"]
    ctx = build(**inputs)
    assert ctx.data["manifest"]["budget_steps"] == []  # Section caps only, no reduction.
    sent = ctx.state["original_research"]
    economic, analysis = research["economic_relationship"], research["technical_context"][
        "analysis"]
    assert len(economic) > 900 and len(analysis) > 3900
    assert economic.startswith(sent["economic_relationship"])
    assert sent["economic_relationship_truncated"] == {
        "kept_chars": 300, "total_chars": len(economic), "sha256_prefix": digest(economic)[:16],
    }
    assert analysis.startswith(sent["technical"]["analysis"])
    assert sent["technical"]["analysis_truncated"]["total_chars"] == len(analysis)
    assert sent["technical"]["code_computed_at_selection"]["sma_5"] == "102.968"
    assert "last_bar_age_seconds" not in sent["technical"]["code_computed_at_selection"]
    assert entries(ctx, "original_research.economic_relationship")[0] == {
        "section": "original_research.economic_relationship", "status": "TRUNCATED",
        "sha256": digest(economic), "total_chars": len(economic), "kept_chars": 300,
        "reason": "SECTION_CAP",
    }
    long_item = inputs["news"][0][0]
    news = next(n for n in ctx.state["news"]["current"] if n["source_id"] == long_item.source_id)
    assert len(news["excerpt"]) == NEWS_EXCERPT_CHARS
    assert news["excerpt_truncated"] == {
        "kept_chars": NEWS_EXCERPT_CHARS, "total_chars": len(long_item.excerpt),
        "sha256_prefix": long_item.content_hash[:16],
    }


def test_manifest_covers_every_input_hash():
    dossier = fx.v3_dossier(confidence=True)
    current, original = fx.news()
    extra = fx.news_item("fifth", "SUPPORTS", origin="CURRENT_EVIDENCE")
    window = fx.bars(64)
    snapshot = fx.snapshot()
    ctx = build(snapshot=snapshot, bars=window, news=((*current, extra), original),
                dossier=dossier)
    manifest = encoded(ctx.data["manifest"])
    research = dossier["original_research"]
    review_rationale = {k: v for k, v in research["rationale"].items()
                        if k != "agent_confidence"}
    expected = [
        digest(snapshot.thesis), digest(snapshot.disproof),
        digest(research["economic_relationship"]),
        digest(research["technical_context"]["analysis"]),
        research["technical_context"]["observed_facts"]["observations_hash"],
        digest(encoded(review_rationale)),
        *(n.content_hash for n in (*current, extra, *original)),
        *(b.observation_id for b in window),
        dossier["selection_judgment"]["eligibility"]["receipt_id"],
        dossier["selection_judgment"]["quality"]["receipt_id"],
    ]
    missing = [value for value in expected if value not in manifest]
    assert not missing
    covered = {seq for e in entries(ctx, "management_history") for seq in e["merged_seqs"]}
    assert covered == {row["event_seq"] for row in dossier["management_history"]}
    assert {e["status"] for e in entries(ctx)} <= {
        "INCLUDED", "TRUNCATED", "OMITTED", "ABSENT", "PARTIAL",
    }
    assert all(e["kept_chars"] <= e["total_chars"] for e in entries(ctx) if "kept_chars" in e)
    assert entries(ctx, "news.current")[-1]["reason"] == "SECTION_LIMIT"


def test_agent_confidence_and_identity_never_reach_jev():
    dossier = fx.v3_dossier(confidence=True)
    dossier["original_research"].update(
        agent={"agent_id": "fixture-agent-grogbot", "run_id": "fixture-run"},
        research_origin="EXTERNAL_MUSE", selection_rationale={"agent_confidence": "HIGH"},
    )
    ctx = build(dossier=dossier)
    state_json = encoded(ctx.state)
    for marker in ("agent_confidence", "Analytics-only fixture basis", "fixture-agent-grogbot",
                   "research_origin", "EXTERNAL_MUSE", "why_over_peers", "known_risks"):
        assert marker not in state_json
    omitted = entries(ctx, "original_research.rationale.agent_confidence")[0]
    assert omitted["status"] == "OMITTED"
    assert omitted["reason"] == "ANALYTICS_ONLY_NEVER_REVIEW_INPUT"
    rationale = ctx.state["original_research"]["rationale"]
    assert rationale["status"] == "UNVERIFIED_PROPOSER_CLAIMS"
    assert rationale["claims"][0]["sources"] == ["source-original-0"]
    assert rationale["claims"][1]["bars"] == ["research-bar-1"]
    assert set(rationale) >= {"why_now", "why_these_levels", "what_would_change_my_mind"}


def test_selection_judgments_history_and_position_are_compact():
    ctx = build(**light())
    judgments = ctx.state["selection_judgments"]
    assert judgments["eligibility"]["verdict"] == {"choice": "APPROVE", "top_p": "0.91"}
    assert judgments["quality"]["evidence_quality"] == {"choice": "2", "top_p": "0.71"}
    assert ctx.state["management_history"] == [
        {"seq": 1000, "action": "HOLD", "reason": None, "stop": None, "target": None},
        {"seq": 1001, "action": "REVIEW_OBSOLETE", "reason": "FIXTURE", "stop": None,
         "target": None},
    ]
    folded = build(**light(history=8))  # Eight events, the last four rows.
    assert folded.state["management_history"] == [
        {"seq": 1010, "action": "TIGHTEN_STOP", "reason": None, "stop": "101.25",
         "target": "108.4"},  # A judgment and its authorized plan (seq 1011) are one row.
        {"seq": 1020, "action": "HOLD", "reason": None, "stop": None, "target": None},
        {"seq": 1021, "action": "REVIEW_OBSOLETE", "reason": "FIXTURE", "stop": None,
         "target": None},
        {"seq": 1030, "action": "TIGHTEN_STOP", "reason": None, "stop": "103.25",
         "target": "108.4"},
    ]
    older = [e for e in entries(folded, "management_history") if e.get("reason")]
    assert [e["merged_seqs"] for e in older] == [[1000], [1001]]
    computed = ctx.state["computed_position"]
    assert computed["initial_risk_per_unit"] == "1.9"
    assert computed["distance_to_stop_ticks"] == "417" and computed["regime"] == "CLEAR"
    assert computed["time_bucket"] == "OVER_50_PCT_LEFT"
    assert computed["calculation_version"] == "POSITION_METRICS_V2"
    assert ctx.state["position"]["initial_stop"] == "101.2"


# -- policy, versions and replay --------------------------------------------------------


def test_v3_policy_requires_an_explicit_budget_and_stored_v2_policies_still_load():
    for bad in (
        {"context_version": CONTEXT_VERSION},
        {"context_version": CONTEXT_VERSION, "state_byte_budget": 12_001},
        {"context_version": CONTEXT_VERSION, "state_byte_budget": 999},
        {"context_version": CONTEXT_VERSION, "state_byte_budget": True},
        {"state_byte_budget": 11_000},
        {"context_version": "JEV_MANAGED_POSITION_CONTEXT_V9", "state_byte_budget": 11_000},
    ):
        with pytest.raises(ValueError, match="EXPLICIT_MANAGED_POLICY"):
            ManagedPolicy(5, 60, 5, 20, 4, **bad)
    stored = {"quote_max_age_seconds": 5, "context_max_age_seconds": 60, "max_options": 5,
              "max_bars": 20, "max_news": 4, "max_structural_bars": 64, "max_history": 8}
    assert ManagedPolicy(**stored).context_version == CONTEXT_VERSION_V2
    assert engineering_monitor_policy() == fx.V3_PRODUCTION_POLICY


def pinned_v2_context():
    """The exact V2 context the pre-3.2 builder produced (hash pinned from 794247a)."""
    now = fx.NOW.replace(second=0)

    def uid(name):
        return str(uuid5(NAMESPACE_URL, "pin:" + name))

    snap = PositionSnapshot(
        candidate_id=uid("c"), position_id=uid("p"), lifecycle_id=uid("l"), context_revision=3,
        market="US", symbol="TEST", strategy_version="CATALYST_RETEST_V1",
        exit_policy=EXIT_POLICY, cohort=MANAGED_COHORT,
        thesis="Fixture product demand supports the observed advance.",
        disproof="Original source retracts the product agreement.",
        original_evidence_ids=(uid("e1"),), original_fill_price=D("100"), filled_qty=D("10"),
        remaining_qty=D("10"), opened_at=now - timedelta(minutes=5),
        hard_exit_at=now + timedelta(hours=3), quote_id=uid("q"), bid=D("105"),
        ask=D("105.01"), quote_at=now, data_provider="LAB_FIXTURE", data_feed="FIXTURE",
        feed_healthy=True, tick_size=D("0.05"),
        stop=ProtectiveOrder(uid("s"), "STOP", D("98"), D("10"), "ACTIVE", 1, "BROKER"),
        target=ProtectiveOrder(uid("t"), "TARGET", D("108"), D("10"), "ACTIVE", 1, "BROKER"),
        state="OPEN", news_revision=1, snapshot_at=now,
    )
    recent = (
        CompletedBar(uid("b1"), now - timedelta(minutes=3), now - timedelta(minutes=2),
                     D("104"), D("110.02"), D("102.02"), D("104"), D("5000"), "LAB_FIXTURE",
                     "FIXTURE"),
        CompletedBar(uid("b2"), now - timedelta(minutes=2), now - timedelta(minutes=1),
                     D("104"), D("111.02"), D("103.04"), D("104.50"), D("6000"), "LAB_FIXTURE",
                     "FIXTURE"),
    )
    structural = (
        CompletedBar(uid("b0"), now - timedelta(hours=3), now - timedelta(hours=2),
                     D("104"), D("120.02"), D("101"), D("104"), D("5000"), "LAB_FIXTURE",
                     "FIXTURE"),
    )
    excerpt = "Fixture only: The issuer confirms the agreement remains in force."
    news = (NewsEvidence(uid("n1"), 1, "fixture-original-source", excerpt, now,
                         digest(excerpt), url="https://issuer.example/a",
                         published_at=now - timedelta(minutes=9), primary_source=True,
                         asset_relevant=True, novelty="NEW_FACT", stance="SUPPORTS",
                         origin="CURRENT_EVIDENCE"),)
    original = (NewsEvidence(uid("n2"), 1, "fixture-release", excerpt + " Original.", now,
                             digest(excerpt + " Original."), origin="ORIGINAL_RESEARCH"),)
    dossier = {
        "original_research": {
            "thesis": snap.thesis, "disproof": snap.disproof,
            "economic_relationship": "Fixture relationship.", "catalyst": "PRODUCT",
            "technical_context": None, "technical_facts": None, "levels": {"stop": "95"},
        },
        "selection_judgment": {"eligibility": None, "quality": None},
        "management_history": [
            {"event_seq": 7, "kind": "MANAGED_JEV_JUDGMENT", "decision": {"action": "HOLD"}}
        ],
        "sampled_excursions": {"sample_count": 0},
        "evidence_manifest": {"x": 1},
    }
    return build_managed_context(
        snap, recent, news, ManagedPolicy(5, 60, 5, 20, 4), now=now,
        review_deadline=now + timedelta(seconds=10), structural_bars=structural,
        original_news=original, dossier=dossier,
        trigger={"policy_id": "LEGACY_COMPLETED_BAR_NEWS_V1",
                 "reasons": ["COMPLETED_BAR_OR_NEWS"], "dedup_key": "k"},
    )


def test_v2_builder_and_replayed_question_sets_are_byte_identical_to_before():
    ctx = pinned_v2_context()
    assert ctx.data["context_version"] == CONTEXT_VERSION_V2
    assert "context_version" not in ctx.data["policy"]  # Stored V2 policies had no such key.
    assert ctx.context_hash == V2_CONTEXT_PIN
    questions = managed_questions(ctx)
    assert (questions.version, questions.template_hash) == (QUESTION_VERSION_V2,
                                                            V2_QUESTIONS_PIN)
    legacy = {k: v for k, v in ctx.data.items() if k != "context_version"}
    legacy = ManagedContext(encoded(legacy), digest(encoded(legacy)))
    questions = managed_questions(legacy)
    assert (questions.version, questions.template_hash) == (LEGACY_QUESTION_VERSION,
                                                            V1_QUESTIONS_PIN)


def reviewer_for(store, answers_for, calls=None):
    def provider(request):
        if calls is not None:
            calls.append(request.content)
        questions = json.loads(request.content)["questions"]
        return httpx.Response(200, json={"model": JEV_MODEL, "answers": {
            name: {"type": "choice", "choice": answers_for[name], "confidence": 0.9,
                   "probabilities": {k: float(k == answers_for[name])
                                     for k in question["criteria"]}}
            for name, question in questions.items()
        }, "usage": {"input_tokens": 20, "output_tokens": 10}})

    return JevReviewer(
        store, ReliabilityPolicy("MANAGED_DOSSIER_FIXTURE", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-provider-credential",
        transport=httpx.MockTransport(provider), clock=lambda: fx.NOW,
    )


def test_v3_receipt_binding_verifies_and_fails_on_tampering(cluster):
    snapshot = fx.snapshot()
    ctx = build(snapshot=snapshot)
    store = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    chosen = {"thesis_status": "INTACT", "action": "TIGHTEN_STOP", "stop_option": "STOP_2",
              "target_option": "KEEP"}
    sent = []
    request_id = str(uuid4())
    decision = asyncio.run(review_managed_position(
        reviewer_for(store, chosen, sent), ctx, request_id=request_id,
        purpose="ENGINEERING_TEST", current_snapshot=lambda: snapshot, clock=lambda: fx.NOW,
    ))
    assert decision.action == "TIGHTEN_STOP"
    assert decision.proposed_stop == D(ctx.state["eligible_options"]["stop"][1]["price"])
    with store.connect() as conn:
        stored = conn.execute(
            "SELECT request_json,evidence_identity FROM lab.jev_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()
    assert stored["request_json"].encode() == sent[0]
    assert stored["evidence_identity"]["managed_context_hash"] == ctx.context_hash
    assert b"eligible_options" in sent[0] and b'"manifest"' not in sent[0]
    assert b'"retained"' not in sent[0]  # Only the state and questions reach the provider.
    with store.connect() as conn:
        raw = conn.execute("SELECT response_bytes FROM lab.jev_receipts WHERE request_id=%s",
                           (request_id,)).fetchone()["response_bytes"]
    genuine = ReviewResult(request_id, "RECORDED", None, decision.receipt_ids,
                           json.loads(bytes(raw))["answers"])
    assert verify_managed_receipts(store, ctx, genuine) == genuine  # Still verifies.
    for tamper in ("state", "manifest", "retained"):
        data = ctx.data
        if tamper == "state":
            data["state"]["thesis"] = "Tampered thesis."
        elif tamper == "manifest":
            data["manifest"]["state_bytes"] = 1
        else:
            data["retained"]["research_packet"]["setup_event_seq"] = 1
        forged_json = encoded(data)
        forged = ManagedContext(forged_json, digest(forged_json))
        assert verify_managed_receipts(store, forged, genuine).reason == (
            "RECEIPT_INTEGRITY_FAILED"
        ), tamper
    hold = {k: dict(v) for k, v in genuine.answers.items()}
    hold["action"]["choice"] = "HOLD"
    assert verify_managed_receipts(store, ctx, replace(genuine, answers=hold)).reason == (
        "RECEIPT_INTEGRITY_FAILED"
    )
    decided = evaluate_managed_result(ctx, genuine, snapshot, now=fx.NOW)
    assert decided.proposed_stop == decision.proposed_stop


def test_stored_v2_judgment_replays_after_the_v3_upgrade(mx, monkeypatch):
    engine, venue, _ = mx
    sid = opened(mx)
    v2_watcher, calls = monitor(mx, "TIGHTEN_STOP")  # The pre-3.2 V2 profile.
    quote = observation(mx, bid="108", ask="108.01")
    original = engine.accept_management

    def crash(*args):
        raise RuntimeError("FIXTURE_PROCESS_DIED_AFTER_RECEIPT")

    monkeypatch.setattr(engine, "accept_management", crash)
    with pytest.raises(RuntimeError, match="FIXTURE_PROCESS_DIED"):
        asyncio.run(v2_watcher.review(sid, quote, one_bar(mx), fresh_observation=lambda: quote))
    monkeypatch.setattr(engine, "accept_management", original)
    stored = context_from_ledger(engine, sid)
    assert stored.data["context_version"] == CONTEXT_VERSION_V2
    assert managed_questions(stored).version == QUESTION_VERSION_V2
    upgraded, new_calls = monitor(mx, "HOLD", policy=engineering_monitor_policy())
    upgraded.recover_pending(sid, fresh_observation=lambda: quote)
    assert new_calls == [] and len(calls) == 1  # The stored vote is consumed, not re-asked.
    assert D(engine._load(sid)[1]["stop"]) == D("102")
    with engine.repo.connect() as conn:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM lab.managed_events WHERE setup_id=%s ORDER BY event_seq", (sid,)
        ).fetchall()]
    assert "POSITION_REVIEW_RECOVERED" in kinds and "MANAGEMENT_PLAN_AUTHORIZED" in kinds
    assert verify_events(engine.repo.export_events())["valid"]


# -- monitor, runtime and measurement integration -------------------------------------


def test_unsatisfiable_budget_skips_the_review_once_per_lifecycle(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    tiny = replace(engineering_monitor_policy(), state_byte_budget=1_000)
    watcher, calls = monitor(mx, "TIGHTEN_STOP", policy=tiny)
    for _ in range(2):
        window = fx.monitor_bars(venue.now)
        quote = observation(mx, bid="108", ask="108.01")
        assert asyncio.run(watcher.review(
            sid, quote, window, fresh_observation=lambda q=quote: q, structural_bars=window,
        )) is None
        venue.now += timedelta(minutes=1)
    assert calls == []
    with engine.repo.connect() as conn:
        rows = conn.execute(
            """SELECT kind,body FROM lab.managed_events WHERE setup_id=%s AND kind IN
            ('POSITION_REVIEW_SKIPPED','POSITION_REVIEW_REQUEST')""", (sid,),
        ).fetchall()
    assert [r["kind"] for r in rows] == ["POSITION_REVIEW_SKIPPED"]
    body = rows[0]["body"]
    assert body["reason"] == "CONTEXT_BUDGET_UNSATISFIABLE"
    assert body["state_byte_budget"] == 1_000 < body["smallest_state_bytes"]
    assert body["lifecycle_id"] == engine._load(sid)[1]["lifecycle_id"]
    assert not body["manifest"]["within_budget"]
    engine.manage(sid, observation(mx, bid="108", ask="108.01"))
    assert not any(method == "PATCH" for method, _, _ in venue.calls)


def test_runtime_bar_window_persists_every_option_bar():
    from catalyst_lab.managed_runtime import PositionMonitorLoop
    from catalyst_lab.scan_sources import BarContextWindow

    window = fx.bars(60)
    events, received = [], []
    execution = SimpleNamespace(_event=lambda *args: events.append(args))

    async def review(setup_id, observation, bars, *, fresh_observation, structural_bars):
        received.append((bars, structural_bars))

    policy = engineering_monitor_policy()
    loop = PositionMonitorLoop(
        SimpleNamespace(execution=execution, policy=policy, review=review),
        SimpleNamespace(completed_bars=lambda *args: (window, ())),
        BarContextWindow(60, 60), clock=lambda: fx.NOW,
    )
    setup = {"setup_id": str(uuid4()), "market": "US_STOCKS", "symbol": "SYNT"}
    asyncio.run(loop(setup, {"bid": "105.37"}, lambda: {"bid": "105.37"}))
    kind, body, setup_id, key = events[0]
    assert kind == "POSITION_REVIEW_BARS" and len(body["bars"]) == 60  # Previously 20.
    assert (body, key) == review_bars_event(review_bar_window(window, window, policy), setup_id)
    assert received == [(window, window)]
    persisted = {b["observation_id"] for b in body["bars"]}
    sources = entries(build(bars=window), "eligible_options")[0]["sources"]
    assert {s["observation_id"] for s in sources.values()} <= persisted
    assert any(s["bar"].startswith("S") for s in sources.values())  # Older than 20 bars.


def load_proof_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "prove_managed_jev.py"
    spec = importlib.util.spec_from_file_location("prove_managed_jev_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("reply", ["valid", "invalid"])
def test_g4_proof_harness_with_a_local_fixture_transport(mx, reply):
    """The owner-run G4 series, exercised here only through a local mock transport."""
    script = load_proof_script()
    assert script.percentile(list(range(1, 21)), 50) == 10
    assert script.percentile(list(range(1, 21)), 95) == 19 and script.percentile([], 95) is None
    transport = None if reply == "valid" else httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"not a provider response"))
    worker = script.review_worker(mx[2].database_url, fixture=True, transport=transport)
    summary = asyncio.run(script.production_series(worker, 2))
    assert summary["calls_sent"] == 2 and summary["overflow_count"] == 0
    assert summary["state_bytes"]["max"] <= fx.V3_PRODUCTION_POLICY.state_byte_budget
    assert [row["market"] for row in summary["calls"]] == ["US", "CRYPTO"]
    assert not summary["g4_production_dossier_criteria_met"]  # Fewer than 30 calls.
    if reply == "valid":
        assert summary["invalid_response_count"] == 0 and summary["verified_receipts"] == 2
        assert summary["final_outcomes"] == {"VALID": 2}
        assert summary["decisions_not_applied"] == {"HOLD": 2}
        assert summary["latency_ms"]["count"] == 2 and summary["latency_ms"]["p95"] is not None
    else:
        # An invalid provider body gets one further attempt (2026-09-25), each with its own
        # receipt and each counted: the first call records two invalid receipts; the second
        # call's retry is refused because its first invalid reply was the Gate 1 breaker's
        # third consecutive failure (threshold 3).
        assert summary["invalid_response_count"] == 3 and summary["verified_receipts"] == 0
        assert summary["final_outcomes"] == {"CIRCUIT_OPEN": 1, "INVALID_RESPONSE": 1}
        assert [[r["outcome"] for r in row["receipts"]] for row in summary["calls"]] == [
            ["INVALID_RESPONSE", "INVALID_RESPONSE"], ["INVALID_RESPONSE", "CIRCUIT_OPEN"]]
        assert summary["decisions_not_applied"] == {"NEEDS_REVIEW": 2}
        assert summary["latency_ms"]["p50"] is None


def test_sql_excursion_aggregate_matches_managed_measurement(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    half = D(entry["qty"]) // 2
    engine.ingest(venue.fill(entry["id"], str(half), price="100"))
    first_fill = venue.now
    venue.now += timedelta(seconds=30)
    engine.ingest(venue.fill(entry["id"], str(D(entry["qty"]) - half), price="100.07"))
    lifecycle = engine._load(sid)[1]["lifecycle_id"]
    samples = [
        (-5, "99", half, lifecycle, "FIXTURE"),  # Before the first fill: excluded.
        (3, "101.5", half, lifecycle, "FIXTURE"),  # One-fill average.
        (12, "98.25", half, lifecycle, "IEX"),
        (40, "102.75", D(entry["qty"]), lifecycle, "FIXTURE"),  # Two-fill average.
        (41, "not-a-number", D(entry["qty"]), lifecycle, "FIXTURE"),  # Malformed.
        (44, "103", D(entry["qty"]), str(uuid4()), "FIXTURE"),  # Other lifecycle.
        (47, "99.95", D(entry["qty"]), lifecycle, "FIXTURE"),
        (900, "150", D(entry["qty"]), lifecycle, "FIXTURE"),  # After as_of: excluded.
    ]
    with engine.store.transaction() as conn:
        for offset, price, qty, cycle, feed in samples:
            engine.store.event(conn, "POSITION_MARKET_SNAPSHOT", {
                "lifecycle_id": cycle, "price": price, "position_qty": str(qty),
                "market_data_timestamp": (first_fill + timedelta(seconds=offset)).isoformat(),
                "data_provider": "LAB_FIXTURE", "data_feed": feed,
            }, setup_id=sid)
    as_of = first_fill + timedelta(seconds=60)
    python = managed_measurement(engine.repo, sid, as_of=as_of)
    sql = sampled_excursions(engine.repo, sid, lifecycle, as_of=as_of)
    for key in ("sampling_method", "sample_count", "first_sample_at", "last_sample_at",
                "max_observation_gap_seconds", "observed_mfe_pnl", "observed_mae_pnl",
                "provenance"):
        assert sql[key] == python[key], key
    assert sql["sample_count"] == 4 and sql["max_observation_gap_seconds"] == 28
    assert sql["first_event_seq"] < sql["last_event_seq"]
    empty = sampled_excursions(engine.repo, uuid4(), lifecycle, as_of=as_of)
    assert empty["sample_count"] == 0 and empty["observed_mfe_pnl"] is None
