import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, SKEPTIC, digest, strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle, ResearchThesis
from catalyst_lab.research_ranking import QUALITY
from catalyst_lab.setup_scan import (
    CompletedBar,
    NewsEvidence,
    QuoteSnapshot,
    ScanAsset,
    SetupScanner,
    engineering_scan_policy,
)

NOW = datetime(2026, 9, 18, 18, 0, tzinfo=UTC)
POLICY = CyclePolicy(10, 10, 15, 60, 30)


@pytest.mark.parametrize("policy", [None, "legacy-unknown", []])
def test_malformed_historical_policy_is_a_terminal_validation_error(runtime, policy):
    cycle, _, _, _ = runtime
    with pytest.raises(ValueError, match="RESEARCH_POLICY_UNAVAILABLE"):
        cycle._cycle([{"kind": "RESEARCH_STARTED", "body": {"policy": policy}}])


def fixture_asset(name="asset-00"):
    bars = []
    for i in range(60):
        close = D("100") + D(i) / 10
        bars.append(
            CompletedBar(
                NOW - timedelta(minutes=60 - i),
                NOW - timedelta(minutes=59 - i),
                close - D("0.05"),
                close + D("0.2"),
                close - D("0.2"),
                close,
                D("2000") if i >= 55 else D("1000"),
                "FIXTURE",
                "TEST_FEED",
                f"bar-{name}-{i}",
                True,
            )
        )
    for index, low in ((20, "100"), (44, "103.9"), (55, "105")):
        bars[index] = replace(bars[index], low=D(low))
    for index, high in ((30, "112"), (48, "110")):
        bars[index] = replace(bars[index], high=D(high))
    excerpt = "Fixture issuer reports a new product is available to paying customers."
    news = NewsEvidence(
        "issuer-source",
        "https://issuer.example/announcement",
        excerpt,
        NOW - timedelta(hours=1),
        NOW - timedelta(seconds=10),
        digest(excerpt),
        True,
        True,
        "NEW_FACT",
        "SUPPORTS",
    )
    return ScanAsset(
        name,
        "TEST" + name[-2:],
        "US",
        tuple(bars),
        QuoteSnapshot(D("105.99"), D("106.01"), NOW, "FIXTURE", "TEST_FEED", "quote-1", True),
        (news,),
        True,
        D("0.01"),
        D("1"),
        D("1"),
        NOW.replace(hour=13, minute=30),
        NOW.replace(hour=20),
    )


def response(*, verdict="APPROVE", stale="NO", unsupported="NO", priced="LOW"):
    selected = {
        "verdict": verdict,
        "news_stale": stale,
        "unsupported_inference": unsupported,
        "already_priced": priced,
    }
    answers = {
        name: {
            "type": "choice",
            "choice": selected[name],
            "confidence": 0.8,
            "probabilities": {k: float(k == selected[name]) for k in question["criteria"]},
        }
        for name, question in SKEPTIC.questions.items()
    }
    return {
        "model": JEV_MODEL,
        "answers": answers,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def quality_response(score=2):
    answers = {
        name: {
            "type": "score",
            "score": score,
            "legend": {str(i): level for i, level in enumerate(question["criteria"])},
            "confidence": 1.0,
            "probabilities": {str(i): float(i == score) for i in range(3)},
        }
        for name, question in QUALITY.questions.items()
    }
    return {
        "model": JEV_MODEL,
        "answers": answers,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture
def runtime(cluster):
    current = [NOW]
    calls = []
    reply = [response()]

    async def provider(request):
        body = strict_json(request.content)
        calls.append(body)
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json=quality_response()
            if set(body["questions"]) == set(QUALITY.questions)
            else reply[0],
        )

    reviewer = JevReviewer(
        JevStore(localdb.connection_url(cluster, "catalyst_jev")),
        ReliabilityPolicy("RESEARCH_CYCLE_FIXTURE", 10, 1, 0.25, 10000, 30),
        key_provider=lambda: "fixture-no-provider-secret",
        transport=httpx.MockTransport(provider),
        clock=lambda: current[0],
    )
    cycle = ResearchCycle(
        RiskRepository(localdb.connection_url(cluster, "catalyst_risk")),
        reviewer,
        POLICY,
        clock=lambda: current[0],
    )
    return cycle, current, calls, reply


def begin(runtime, count=1, *, assets=None):
    cycle = runtime[0]
    assets = assets or [fixture_asset(f"asset-{i:02}") for i in range(count)]
    batch = SetupScanner(engineering_scan_policy()).scan(assets, NOW)
    cycle_id = str(uuid4())
    theses = {
        a.market + ":" + a.asset_id: ResearchThesis(
            "New availability may support the observed retest.",
            "Issuer retracts availability.",
            "Customers purchase the issuer's product; demand is a hypothesis, "
            "not a profit forecast.",
        )
        for a in assets
    }
    cycle.start(cycle_id, batch, assets, theses, expires_at=NOW + timedelta(minutes=5))
    return cycle_id, assets, theses


def test_twenty_contenders_to_at_most_ten_with_every_receipt_and_event(runtime, repo):
    cycle, _, calls, _ = runtime
    cycle_id, _, _ = begin(runtime, 20)
    assert cycle.approved_packets(cycle_id) == []
    results = asyncio.run(cycle.tick(cycle_id))
    assert len(results) == 20 and len(calls) == 40
    assert all(r["disposition"] == "APPROVED" and r["receipt_ids"] for r in results)
    chosen = cycle.approved_packets(cycle_id)
    assert len(chosen) == 10
    assert [p["quality_rank"] for p in chosen] == list(range(1, 11))
    assert all(p["market"] == "US_STOCKS" for p in chosen)
    assert all(p["levels"]["stop"] == "103.9" for p in chosen)
    assert len({p["receipt_id"] for p in chosen}) == 10
    events = cycle.outputs(cycle_id)
    for packet in chosen:
        selected = next(r for r in events if r["event_seq"] == packet["selection_event_seq"])
        assert selected["kind"] == "RESEARCH_SELECTED"
        assert selected["body"]["packet"] == {
            k: v for k, v in packet.items() if k != "selection_event_seq"
        }
    assert asyncio.run(cycle.tick(cycle_id)) == []
    assert cycle.approved_packets(cycle_id) == chosen
    assert len(calls) == 40


def is_quality(call):
    return set(call["questions"]) == set(QUALITY.questions)


def staged_cycle(runtime, *, contenders, first_wave):
    """Approve ``first_wave`` items, request evidence for the rest, then publish.

    A smaller in-flight limit is part of the stored cycle policy, so the later
    contenders are reviewed on the second tick with the evidence-request reply.
    """
    base, now, calls, reply = runtime
    cycle = ResearchCycle(
        base.repo, base.reviewer, replace(POLICY, max_inflight=first_wave), clock=lambda: now[0]
    )
    cycle_id, assets, theses = begin((cycle,), contenders)
    reply[0] = response()
    assert len(asyncio.run(cycle.tick(cycle_id))) == first_wave
    assert cycle.approved_packets(cycle_id) == []  # Undecided contenders hold publication.
    reply[0] = response(unsupported=INSUFFICIENT)
    pending = asyncio.run(cycle.tick(cycle_id))
    assert {d["disposition"] for d in pending} == {"NEEDS_REVIEW"}
    first = cycle.approved_packets(cycle_id)
    assert len(first) == first_wave
    assert all(p["quality_required"] is False and p["selection_limit"] == 10 for p in first)
    assert not any(is_quality(call) for call in calls)
    return cycle, cycle_id, assets, theses, pending, first


def late_evidence(runtime, cycle, cycle_id, assets, theses, pending):
    _, now, _, reply = runtime
    now[0] += timedelta(seconds=1)
    for decision in pending:
        key = decision["item_key"]
        excerpt = f"Fixture issuer confirms paying customer contracts for {key}."
        source = replace(
            assets[0].news[0],
            source_id="contract-source",
            url="https://issuer.example/contracts",
            excerpt=excerpt,
            content_hash=digest(excerpt),
            retrieved_at=now[0],
        )
        cycle.submit_evidence(
            cycle_id,
            key,
            revision=2,
            sources=(source,),
            thesis=theses[key],
            task_id=decision["evidence_task_id"],
        )
    reply[0] = response()


def selected_events(cycle, cycle_id):
    return [
        (e["event_seq"], e["body"])
        for e in cycle.outputs(cycle_id, limit=1000)
        if e["kind"] == "RESEARCH_SELECTED"
    ]


def test_late_approvals_are_add_only_and_ranked_only_for_remaining_slots(runtime, repo):
    _, _, calls, _ = runtime
    cycle, cycle_id, assets, theses, pending, first = staged_cycle(
        runtime, contenders=11, first_wave=8
    )
    published = selected_events(cycle, cycle_id)
    late_evidence(runtime, cycle, cycle_id, assets, theses, pending)
    # Pending revisions hold new publication, never the selections already published.
    assert cycle.approved_packets(cycle_id) == first
    assert [d["disposition"] for d in asyncio.run(cycle.tick(cycle_id))] == ["APPROVED"] * 3
    late_keys = sorted(d["item_key"] for d in pending)
    quality = [call for call in calls if is_quality(call)]
    assert sorted(f"US:asset-{c['state']['candidate']['symbol'][-2:]}" for c in quality) == (
        late_keys
    )
    chosen = cycle.approved_packets(cycle_id)
    assert chosen[:8] == first
    added = chosen[8:]
    assert [p["item_key"] for p in added] == late_keys[:2]
    assert [p["quality_rank"] for p in added] == [1, 2]
    assert all(
        p["revision"] == 2
        and p["quality_required"] is True
        and p["quality_candidate_count"] == 3
        and p["selection_limit"] == 10
        for p in added
    )
    # The first eight packet bodies are untouched and nothing is published twice.
    assert selected_events(cycle, cycle_id)[:8] == published
    assert len(selected_events(cycle, cycle_id)) == 10
    assert asyncio.run(cycle.tick(cycle_id)) == []
    assert cycle.approved_packets(cycle_id) == chosen
    assert len(quality) == len([call for call in calls if is_quality(call)]) == 3
    assert verify_events(repo.export_events())["valid"]


def test_full_cycle_never_reranks_published_selections_for_a_late_approval(runtime):
    _, _, calls, _ = runtime
    cycle, cycle_id, assets, theses, pending, first = staged_cycle(
        runtime, contenders=11, first_wave=10
    )
    published = selected_events(cycle, cycle_id)
    late_evidence(runtime, cycle, cycle_id, assets, theses, pending)
    assert asyncio.run(cycle.tick(cycle_id))[0]["disposition"] == "APPROVED"
    # Eleven approvals exceed the limit, but no slot remains: no ranking, no rewrite.
    assert cycle.approved_packets(cycle_id) == first
    assert selected_events(cycle, cycle_id) == published
    assert not any(is_quality(call) for call in calls)


def test_evidence_task_claim_resolve_and_null_publication(runtime, repo):
    cycle, now, _, reply = runtime
    reply[0] = response(unsupported=INSUFFICIENT)
    cycle_id, assets, theses = begin(runtime)
    decision = asyncio.run(cycle.tick(cycle_id))[0]
    tasks = cycle.claim_evidence_tasks(cycle_id, "fixture-muse", lease_seconds=30)
    assert len(tasks) == 1 and tasks[0]["task_id"] == decision["evidence_task_id"]
    assert cycle.claim_evidence_tasks(cycle_id, "other-muse", lease_seconds=30) == []
    now[0] += timedelta(seconds=1)
    source = replace(
        assets[0].news[0],
        published_at=None,
        source_id="undated-source",
        excerpt="Issuer confirms a specific customer contract and termination terms.",
    )
    source = replace(source, content_hash=digest(source.excerpt))
    packet = cycle.submit_evidence(
        cycle_id,
        "US:asset-00",
        revision=2,
        sources=(source,),
        thesis=theses["US:asset-00"],
        task_id=tasks[0]["task_id"],
        technical_facts={
            "observed_at": now[0].isoformat(),
            "timeframe": "1MIN",
            "summary": "Completed bars still show the reported retest geometry.",
            "facts": ["The completed-bar pivot remains unchanged."],
            "source_ids": ["undated-source"],
        },
    )
    assert packet["state"]["sources"][0]["published_at"] is None
    assert packet["state"]["technical_context"]["muse_verified_facts"]["facts"]
    assert cycle.claim_evidence_tasks(cycle_id, "fixture-muse") == []
    replay = cycle.submit_evidence(
        cycle_id,
        "US:asset-00",
        revision=2,
        sources=(source,),
        thesis=theses["US:asset-00"],
        task_id=tasks[0]["task_id"],
        technical_facts={
            "observed_at": now[0].isoformat(),
            "timeframe": "1MIN",
            "summary": "Completed bars still show the reported retest geometry.",
            "facts": ["The completed-bar pivot remains unchanged."],
            "source_ids": ["undated-source"],
        },
    )
    assert replay["evidence_hash"] == packet["evidence_hash"]
    assert verify_events(repo.export_events())["valid"]


@pytest.mark.parametrize(
    "answers,disposition",
    [
        ({"verdict": "REJECT"}, "REJECTED"),
        ({"stale": "YES"}, "NEEDS_REVIEW"),
        ({"unsupported": "YES"}, "NEEDS_REVIEW"),
        ({"priced": "HIGH"}, "NEEDS_REVIEW"),
        ({"verdict": "NEEDS_REVIEW"}, "NEEDS_REVIEW"),
        ({"stale": INSUFFICIENT}, "NEEDS_REVIEW"),
    ],
)
def test_no_forced_quota_or_confidence_override(runtime, answers, disposition):
    cycle, _, _, reply = runtime
    reply[0] = response(**answers)
    cycle_id, _, _ = begin(runtime)
    decisions = asyncio.run(cycle.tick(cycle_id))
    assert decisions[0]["disposition"] == disposition
    assert cycle.approved_packets(cycle_id) == []
    if disposition == "NEEDS_REVIEW":
        assert decisions[0]["evidence_tasks"]
        assert all(t["required_fields"] for t in decisions[0]["evidence_tasks"])


def test_explicit_reject_dominates_independent_uncertainty_direct_and_recovered(runtime):
    cycle, now, calls, reply = runtime
    reply[0] = response(verdict="REJECT", priced=INSUFFICIENT)

    direct_cycle_id, _, _ = begin(runtime)
    direct = asyncio.run(cycle.tick(direct_cycle_id))[0]
    assert direct["disposition"] == "REJECTED"
    assert direct["reason"] == "JEV_REJECTED"
    assert direct["evidence_tasks"] == []
    assert cycle.approved_packets(direct_cycle_id) == []

    recovered_cycle_id, _, _ = begin(runtime)
    packet, claim = cycle._claim(recovered_cycle_id)[0]
    interrupted = asyncio.run(
        cycle.reviewer.jev_review(
            request_id=claim["request_id"],
            identity=cycle._identity(packet),
            state=packet["state"],
            question_set=SKEPTIC,
            expires_at=datetime.fromisoformat(claim["deadline"]),
            purpose="ENGINEERING_TEST",
        )
    )
    assert interrupted.status == "NEEDS_REVIEW"
    assert interrupted.reason == "UNCERTAIN_JUDGMENT"
    now[0] += timedelta(seconds=16)
    recovered = asyncio.run(cycle.tick(recovered_cycle_id))[0]
    assert len(calls) == 2
    assert recovered["disposition"] == "REJECTED"
    assert recovered["reason"] == "JEV_REJECTED"
    assert recovered["evidence_tasks"] == []
    assert cycle.approved_packets(recovered_cycle_id) == []


def test_uncertain_result_answers_must_match_bound_receipt(runtime):
    cycle, _, _, reply = runtime
    reply[0] = response(verdict="REJECT", priced=INSUFFICIENT)
    cycle_id, _, _ = begin(runtime)
    original = cycle.reviewer.jev_review

    async def forged(**kwargs):
        actual = await original(**kwargs)
        assert actual.status == "NEEDS_REVIEW" and actual.reason == "UNCERTAIN_JUDGMENT"
        return replace(actual, answers=response(verdict="REJECT")["answers"])

    cycle.reviewer.jev_review = forged
    decision = asyncio.run(cycle.tick(cycle_id))[0]
    assert decision["disposition"] == "NEEDS_REVIEW"
    assert decision["reason"] == "RECEIPT_INTEGRITY_FAILED"
    assert cycle.approved_packets(cycle_id) == []


def test_invalid_provider_distribution_remains_blocked(runtime):
    cycle, _, _, reply = runtime
    reply[0] = response()
    # Sum 1.1: beyond JEV_RESPONSE_PRECISION_V1's two-decimal rounding allowance.
    reply[0]["answers"]["verdict"]["probabilities"]["REJECT"] = 0.1
    cycle_id, _, _ = begin(runtime)
    decision = asyncio.run(cycle.tick(cycle_id))[0]
    assert decision["disposition"] == "NEEDS_REVIEW"
    assert decision["reason"] == "INVALID_PROVIDER_RESPONSE"
    assert decision["answers"] == {}
    assert cycle.approved_packets(cycle_id) == []


def test_material_followup_preserves_all_receipts_and_original_deadline(runtime):
    cycle, now, calls, reply = runtime
    reply[0] = response(unsupported=INSUFFICIENT)
    cycle_id, assets, theses = begin(runtime)
    first = asyncio.run(cycle.tick(cycle_id))[0]
    assert first["disposition"] == "NEEDS_REVIEW"
    now[0] += timedelta(seconds=1)
    old = assets[0].news[0]
    excerpt = "Fixture issuer confirms paying customer contracts and discloses cancellation terms."
    new = replace(
        old,
        source_id="new-original-source",
        url="https://issuer.example/contracts",
        excerpt=excerpt,
        content_hash=digest(excerpt),
        retrieved_at=now[0],
    )
    packet = cycle.submit_evidence(
        cycle_id, "US:asset-00", revision=2, sources=(old, new), thesis=theses["US:asset-00"]
    )
    assert packet["expires_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert cycle.approved_packets(cycle_id) == []
    reply[0] = response()
    second = asyncio.run(cycle.tick(cycle_id))[0]
    assert second["disposition"] == "APPROVED"
    assert len(calls) == 2 and set(first["receipt_ids"]).isdisjoint(second["receipt_ids"])
    assert cycle.approved_packets(cycle_id)[0]["revision"] == 2
    decisions = [e for e in cycle.outputs(cycle_id) if e["kind"] == "RESEARCH_DECISION"]
    assert len(decisions) == 2 and decisions[0]["body"] == first | {"cycle_id": cycle_id}


def test_no_unchanged_packet_reroll_even_with_reworded_thesis_or_new_timestamps(runtime):
    cycle, now, _, _ = runtime
    cycle_id, assets, _ = begin(runtime)
    asyncio.run(cycle.tick(cycle_id))
    now[0] += timedelta(seconds=1)
    source = replace(assets[0].news[0], source_id="renamed", retrieved_at=now[0])
    with pytest.raises(ValueError, match="MATERIAL_NEW_SOURCE"):
        cycle.submit_evidence(
            cycle_id,
            "US:asset-00",
            revision=2,
            sources=(source,),
            thesis=ResearchThesis("Reworded bullish argument", "Disproof", "Link"),
        )
    assert cycle.approved_packets(cycle_id)[0]["revision"] == 1


def test_new_evidence_immediately_invalidates_previous_selection(runtime):
    cycle, _, _, _ = runtime
    cycle_id, assets, theses = begin(runtime)
    asyncio.run(cycle.tick(cycle_id))
    first = cycle.approved_packets(cycle_id)[0]
    excerpt = "Fixture correction withdraws the original announcement."
    correction = replace(
        assets[0].news[0],
        excerpt=excerpt,
        content_hash=digest(excerpt),
        source_id="correction",
        url="https://issuer.example/correction",
    )
    cycle.submit_evidence(
        cycle_id, "US:asset-00", revision=2, sources=(correction,), thesis=theses["US:asset-00"]
    )
    assert cycle.approved_packets(cycle_id) == []
    rows = cycle.outputs(cycle_id, after=first["selection_event_seq"])
    assert [r["kind"] for r in rows] == ["RESEARCH_SUPERSEDED", "RESEARCH_PACKET"]


def test_expiry_does_not_extend_for_research(runtime):
    cycle, now, calls, _ = runtime
    cycle_id, assets, theses = begin(runtime)
    now[0] += timedelta(minutes=5)
    with pytest.raises(ValueError, match="ORIGINAL_CYCLE_DEADLINE"):
        cycle.submit_evidence(
            cycle_id,
            "US:asset-00",
            revision=2,
            sources=assets[0].news,
            thesis=theses["US:asset-00"],
        )
    result = asyncio.run(cycle.tick(cycle_id))[0]
    assert result["disposition"] == "EXPIRED" and not calls
    assert cycle.approved_packets(cycle_id) == []


def test_concurrent_ticks_claim_each_item_once(runtime):
    cycle, _, calls, _ = runtime
    cycle_id, _, _ = begin(runtime, 3)

    async def together():
        return await asyncio.gather(cycle.tick(cycle_id), cycle.tick(cycle_id))

    results = asyncio.run(together())
    assert sorted(map(len, results)) == [0, 3]
    assert len(calls) == 3


def test_restart_recovers_retained_receipt_without_second_vote(runtime):
    cycle, now, calls, _ = runtime
    cycle_id, _, _ = begin(runtime)
    packet, claim = cycle._claim(cycle_id)[0]
    # Simulate process loss after the adapter commits its receipt but before the
    # coordinator records a disposition.
    asyncio.run(
        cycle.reviewer.jev_review(
            request_id=claim["request_id"],
            identity=cycle._identity(packet),
            state=packet["state"],
            question_set=SKEPTIC,
            expires_at=datetime.fromisoformat(claim["deadline"]),
            purpose="ENGINEERING_TEST",
        )
    )
    assert len(calls) == 1
    now[0] += timedelta(seconds=16)
    restarted = ResearchCycle(cycle.repo, cycle.reviewer, POLICY, clock=lambda: now[0])
    decisions = asyncio.run(restarted.tick(cycle_id))
    assert len(calls) == 1
    assert decisions[0]["disposition"] == "APPROVED"
    assert len(restarted.approved_packets(cycle_id)) == 1


def test_restart_does_not_retry_unknown_interrupted_request(runtime):
    cycle, now, calls, _ = runtime
    cycle_id, _, _ = begin(runtime)
    packet, claim = cycle._claim(cycle_id)[0]
    from catalyst_lab.jev_contract import encoded

    request_json = encoded(
        {"model": JEV_MODEL, "state": packet["state"], "questions": SKEPTIC.questions}
    )
    cycle.reviewer.store.start(
        claim["request_id"],
        cycle._identity(packet),
        SKEPTIC,
        digest(encoded(packet["state"])),
        request_json,
        datetime.fromisoformat(claim["deadline"]),
        "ENGINEERING_TEST",
    )
    now[0] += timedelta(seconds=16)
    decision = asyncio.run(cycle.tick(cycle_id))[0]
    assert decision["reason"] == "INTERRUPTED_REVIEW"
    assert not calls and cycle.approved_packets(cycle_id) == []


def test_obsolete_response_retained_but_never_selected(runtime):
    cycle, _, _, _ = runtime
    cycle_id, assets, theses = begin(runtime)
    packet, claim = cycle._claim(cycle_id)[0]
    excerpt = "Fixture original-source update changes relevant agreement conditions."
    source = replace(assets[0].news[0], excerpt=excerpt, content_hash=digest(excerpt))
    cycle.submit_evidence(
        cycle_id, "US:asset-00", revision=2, sources=(source,), thesis=theses["US:asset-00"]
    )
    stale = asyncio.run(cycle._review(packet, claim))
    assert stale["disposition"] == "SUPERSEDED" and stale["receipt_ids"]
    assert cycle.approved_packets(cycle_id) == []


def test_foreign_receipt_or_mutated_answer_cannot_authorize_selection(runtime):
    cycle, _, _, _ = runtime
    cycle_id, _, _ = begin(runtime)
    original = cycle.reviewer.jev_review

    async def forged(**kwargs):
        actual = await original(**kwargs)
        return replace(actual, request_id=str(uuid4()))

    cycle.reviewer.jev_review = forged
    decision = asyncio.run(cycle.tick(cycle_id))[0]
    assert decision["reason"] == "RECEIPT_INTEGRITY_FAILED"
    assert cycle.approved_packets(cycle_id) == []


def test_outputs_cursor_survives_new_coordinator(runtime):
    cycle, now, _, _ = runtime
    cycle_id, _, _ = begin(runtime)
    first = cycle.outputs(cycle_id)
    cursor = first[-1]["event_seq"]
    asyncio.run(cycle.tick(cycle_id))
    restarted = ResearchCycle(cycle.repo, cycle.reviewer, POLICY, clock=lambda: now[0])
    later = restarted.outputs(cycle_id, after=cursor)
    assert [e["kind"] for e in later] == ["RESEARCH_CLAIM", "RESEARCH_DECISION"]
    assert restarted.outputs(cycle_id, after=later[-1]["event_seq"]) == []


def test_scanner_evidence_cannot_be_swapped_after_screening(runtime):
    cycle = runtime[0]
    asset = fixture_asset()
    batch = SetupScanner(engineering_scan_policy()).scan([asset], NOW)
    changed = replace(asset, symbol="OTHER")
    with pytest.raises(ValueError, match="SCANNER_ASSET_BINDING_MISMATCH"):
        cycle.start(
            uuid4(),
            batch,
            [changed],
            {"US:asset-00": ResearchThesis("T", "D", "E")},
            expires_at=NOW + timedelta(minutes=1),
        )


def test_crypto_and_india_preserve_market_and_execution_scope(runtime):
    asset = fixture_asset()
    crypto = replace(
        asset,
        market="CRYPTO",
        symbol="TEST/USD",
        quantity_increment=D("0.001"),
        minimum_order_size=D("0.001"),
        session_open=None,
        session_close=None,
    )
    india = replace(asset, market="INDIA", symbol="TESTIN")
    cycle_id, _, _ = begin(runtime, assets=[crypto, india])
    asyncio.run(runtime[0].tick(cycle_id))
    selected = runtime[0].approved_packets(cycle_id)
    assert {p["market"] for p in selected} == {"CRYPTO", "INDIA"}
    assert next(p for p in selected if p["market"] == "INDIA")["execution_scope"] == "RESEARCH_ONLY"
