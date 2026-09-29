import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy, ReviewResult
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_review import (
    CONTEXT_VERSION,
    EXIT_POLICY,
    MANAGED_COHORT,
    QUESTION_VERSION,
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
)
from tests import managed_dossier_fixtures as fx

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
POLICY = ManagedPolicy(5, 10, 5, 20, 4)


def ident():
    return str(uuid4())


@pytest.fixture
def snapshot():
    return PositionSnapshot(
        candidate_id=ident(), position_id=ident(), lifecycle_id=ident(), context_revision=1,
        market="US", symbol="TEST", strategy_version="CATALYST_RETEST_V1",
        exit_policy=EXIT_POLICY, cohort=MANAGED_COHORT,
        thesis="Fixture product demand supports the observed advance.",
        disproof="Original source retracts the product agreement.",
        original_evidence_ids=(ident(),), original_fill_price=D("100"), filled_qty=D("10"),
        remaining_qty=D("10"), opened_at=NOW - timedelta(minutes=5),
        hard_exit_at=NOW + timedelta(hours=3), quote_id=ident(), bid=D("105"), ask=D("105.01"),
        quote_at=NOW, data_provider="LAB_FIXTURE", data_feed="FIXTURE", feed_healthy=True,
        tick_size=D("0.05"),
        stop=ProtectiveOrder(ident(), "STOP", D("98"), D("10"), "ACTIVE", 1, "BROKER"),
        target=ProtectiveOrder(ident(), "TARGET", D("108"), D("10"), "ACTIVE", 1, "BROKER"),
        state="OPEN", news_revision=1, snapshot_at=NOW,
    )


@pytest.fixture
def bars():
    return (
        CompletedBar(ident(), NOW - timedelta(minutes=3), NOW - timedelta(minutes=2),
                     D("104"), D("110.02"), D("102.02"), D("104"), D("5000"),
                     "LAB_FIXTURE", "FIXTURE"),
        CompletedBar(ident(), NOW - timedelta(minutes=2), NOW - timedelta(minutes=1),
                     D("104"), D("111.02"), D("103.04"), D("104.50"), D("6000"),
                     "LAB_FIXTURE", "FIXTURE"),
    )


@pytest.fixture
def news():
    excerpt = "Fixture only: The issuer confirms the agreement remains in force."
    return (NewsEvidence(ident(), 1, "fixture-original-source", excerpt, NOW,
                         digest(excerpt)),)


def context(snapshot, bars=(), news=()):
    return build_managed_context(snapshot, bars, news, POLICY, now=NOW,
                                 review_deadline=NOW + timedelta(seconds=10))


def result(ctx, action="HOLD", stop="KEEP", target="KEEP", thesis="INTACT"):
    selected = {"action": action, "stop_option": stop, "target_option": target,
                "thesis_status": thesis}
    answers = {
        name: {"type": "choice", "choice": selected[name], "confidence": 0.9,
               "probabilities": {k: float(k == selected[name]) for k in question["criteria"]}}
        for name, question in managed_questions(ctx).questions.items()
    }
    return ReviewResult(ident(), "RECORDED", None, (ident(),), answers)


def test_context_is_frozen_hashed_and_uses_observed_tick_rounded_levels(snapshot, bars, news):
    ctx = context(snapshot, bars, news)
    assert ctx.context_hash == digest(ctx.context_json)
    options = ctx.state["eligible_options"]
    assert [x["price"] for x in options["stop"]] == ["103.00", "102.00"]
    assert [x["price"] for x in options["target"]] == ["111.05", "110.05"]
    assert options["stop"][0]["observation_id"] == bars[-1].observation_id
    assert ctx.state["original_evidence_ids"] == list(snapshot.original_evidence_ids)
    copied = ctx.state
    copied["thesis"] = "mutated"
    assert ctx.state["thesis"] == snapshot.thesis
    with pytest.raises(FrozenInstanceError):
        ctx.context_hash = "forged"
    with pytest.raises(ValueError, match="CONTEXT_HASH"):
        ManagedContext(ctx.context_json + " ", ctx.context_hash)


def test_provider_payload_excludes_broker_and_position_identifiers(snapshot, bars):
    ctx = context(snapshot, bars)
    payload = encoded(ctx.state)
    for value in (snapshot.position_id, snapshot.candidate_id, snapshot.lifecycle_id,
                  snapshot.quote_id, snapshot.stop.order_id, snapshot.target.order_id):
        assert value not in payload
    assert ctx.identity["position_id"] == snapshot.position_id
    assert "account_id" not in payload and "secret" not in payload


@pytest.mark.parametrize("payload", [
    "Owner contact example@invalid.test", "api_key: ts_01234567890123456789",
    "Wallet 0x0123456789012345678901234567890123456789",
])
def test_sensitive_free_text_never_reaches_provider(snapshot, payload):
    with pytest.raises(ValueError, match="SENSITIVE_EVIDENCE"):
        context(replace(snapshot, thesis=payload))


@pytest.mark.parametrize("action,stop,target,expected_stop,expected_target", [
    ("HOLD", "KEEP", "KEEP", None, None),
    ("TIGHTEN_STOP", "STOP_1", "KEEP", D("103.00"), None),
    ("EXTEND_TARGET", "KEEP", "TARGET_2", None, D("110.05")),
    ("TIGHTEN_AND_EXTEND", "STOP_2", "TARGET_1", D("102.00"), D("111.05")),
])
def test_explicit_bounded_actions(snapshot, bars, action, stop, target,
                                 expected_stop, expected_target):
    ctx = context(snapshot, bars)
    decision = evaluate_managed_result(ctx, result(ctx, action, stop, target), snapshot, now=NOW)
    assert decision.action == action
    assert decision.proposed_stop == expected_stop
    assert decision.proposed_target == expected_target
    assert decision.requires_risk_authorization == (action != "HOLD")
    assert decision.expires_at <= snapshot.hard_exit_at


@pytest.mark.parametrize("action,stop,target", [
    ("HOLD", "STOP_1", "KEEP"), ("TIGHTEN_STOP", "KEEP", "KEEP"),
    ("EXTEND_TARGET", "STOP_1", "TARGET_1"),
    ("TIGHTEN_AND_EXTEND", "STOP_1", "KEEP"),
])
def test_independent_contradictory_answers_cannot_amend(snapshot, bars, action, stop, target):
    ctx = context(snapshot, bars)
    decision = evaluate_managed_result(ctx, result(ctx, action, stop, target), snapshot, now=NOW)
    assert decision.reason == "CONTRADICTORY_MANAGEMENT_ANSWERS"
    assert decision.action == "NEEDS_REVIEW"
    assert decision.proposed_stop is decision.proposed_target is None


@pytest.mark.parametrize("question", ["action", "stop_option", "target_option", "thesis_status"])
def test_insufficient_evidence_always_available_and_never_amends(snapshot, bars, question):
    ctx = context(snapshot, bars)
    reviewed = result(ctx)
    answer = reviewed.answers[question]
    assert INSUFFICIENT in answer["probabilities"]
    answer["choice"] = INSUFFICIENT
    answer["probabilities"] = {k: float(k == INSUFFICIENT) for k in answer["probabilities"]}
    decision = evaluate_managed_result(ctx, reviewed, snapshot, now=NOW)
    assert decision.reason == "UNCERTAIN_JUDGMENT"
    assert not decision.requires_risk_authorization


def test_thesis_refutation_alert_does_not_create_early_exit(snapshot, bars):
    ctx = context(snapshot, bars)
    decision = evaluate_managed_result(ctx, result(ctx, thesis="REFUTED"), snapshot, now=NOW)
    assert decision.reason == "THESIS_REFUTED_EARLY_EXIT_DISABLED"
    assert decision.action == "NEEDS_REVIEW"


@pytest.mark.parametrize("change,reason", [
    ({"state": "CLOSED"}, "POSITION_NOT_OPEN"),
    ({"lifecycle_id": "00000000-0000-0000-0000-000000000001"}, "POSITION_CONTEXT_SUPERSEDED"),
    ({"context_revision": 2}, "POSITION_CONTEXT_SUPERSEDED"),
    ({"news_revision": 2}, "POSITION_CONTEXT_SUPERSEDED"),
    ({"hard_exit_at": NOW + timedelta(hours=4)}, "POSITION_CONTEXT_SUPERSEDED"),
    ({"feed_healthy": False}, "DATA_FEED_FAILURE"),
    ({"quote_at": NOW - timedelta(seconds=6)}, "STALE_MARKET_CONTEXT"),
    ({"bid": D("97.90"), "ask": D("98.00")}, "MECHANICAL_EXIT_HAS_PRIORITY"),
    ({"bid": D("108.10"), "ask": D("108.15")}, "MECHANICAL_EXIT_HAS_PRIORITY"),
])
def test_late_or_superseded_context_preserves_existing_protection(snapshot, bars, change, reason):
    ctx = context(snapshot, bars)
    reviewed = result(ctx, "TIGHTEN_STOP", "STOP_1")
    decision = evaluate_managed_result(ctx, reviewed, replace(snapshot, **change), now=NOW)
    assert decision.reason == reason
    assert decision.action == "NEEDS_REVIEW"
    assert snapshot.stop.price == D("98")


def test_fresh_quote_does_not_revoke_but_rechecks_eligible_price(snapshot, bars):
    ctx = context(snapshot, bars)
    reviewed = result(ctx, "TIGHTEN_STOP", "STOP_1")
    newer = replace(snapshot, quote_id=ident(), quote_at=NOW + timedelta(seconds=1),
                    snapshot_at=NOW + timedelta(seconds=1), bid=D("104"), ask=D("104.05"))
    assert evaluate_managed_result(ctx, reviewed, newer,
                                   now=NOW + timedelta(seconds=1)).action == "TIGHTEN_STOP"
    newer = replace(newer, bid=D("102.50"), ask=D("102.55"))
    assert evaluate_managed_result(ctx, reviewed, newer,
                                   now=NOW + timedelta(seconds=1)).reason == (
        "STOP_OPTION_NO_LONGER_ELIGIBLE"
    )


def test_exact_expiry_and_hard_deadline_are_code_enforced(snapshot, bars):
    snapshot = replace(snapshot, hard_exit_at=NOW + timedelta(seconds=3))
    ctx = context(snapshot, bars)
    assert ctx.expires_at == snapshot.hard_exit_at
    assert evaluate_managed_result(ctx, result(ctx), snapshot,
                                   now=ctx.expires_at).reason == "REVIEW_EXPIRED"


@pytest.mark.parametrize("state", ["REJECTED", "MISSING", "PENDING_REPLACE", "CANCELED"])
def test_unresolved_protection_goes_to_mechanical_recovery(snapshot, state):
    with pytest.raises(ValueError, match="PROTECTION_NOT_ACKNOWLEDGED"):
        context(replace(snapshot, stop=replace(snapshot.stop, status=state)))


def test_partial_fill_race_changes_quantity_and_blocks_amendment(snapshot, bars):
    ctx = context(snapshot, bars)
    current = replace(snapshot, remaining_qty=D("5"),
                      stop=replace(snapshot.stop, remaining_qty=D("5"), revision=2),
                      target=replace(snapshot.target, remaining_qty=D("5"), revision=2))
    decision = evaluate_managed_result(ctx, result(ctx), current, now=NOW)
    assert decision.reason == "POSITION_CONTEXT_SUPERSEDED"


def test_crypto_uses_native_stop_and_durable_mechanical_target(snapshot, bars):
    crypto = replace(snapshot, market="CRYPTO", symbol="BTC/USD", filled_qty=D("0.01"),
                     remaining_qty=D("0.01"),
                     stop=replace(snapshot.stop, remaining_qty=D("0.01")),
                     target=replace(snapshot.target, source="MECHANICAL_LOCAL",
                                    remaining_qty=D("0.01")))
    ctx = context(crypto, bars)
    assert ctx.state["target"]["source"] == "MECHANICAL_LOCAL"
    assert evaluate_managed_result(ctx, result(ctx), crypto, now=NOW).action == "HOLD"
    with pytest.raises(ValueError, match="PROTECTION_NOT_ACKNOWLEDGED"):
        context(replace(crypto, stop=replace(crypto.stop, source="MECHANICAL_LOCAL")))
    with pytest.raises(ValueError, match="PROTECTION_NOT_ACKNOWLEDGED"):
        context(replace(snapshot, target=replace(snapshot.target, source="MECHANICAL_LOCAL")))


def test_no_bar_no_invented_trail_or_target(snapshot):
    ctx = context(snapshot)
    assert ctx.state["eligible_options"] == {"stop": [], "target": []}
    assert set(managed_questions(ctx).questions["stop_option"]["criteria"]) == {
        "KEEP", INSUFFICIENT,
    }


@pytest.mark.parametrize("change,reason", [
    ({"ends_at": NOW + timedelta(seconds=1)}, "COMPLETED_BAR_REQUIRED"),
    ({"data_feed": "OTHER"}, "BAR_PROVENANCE_MISMATCH"),
    ({"low": D("106")}, "INVALID_BAR_GEOMETRY"),
])
def test_bar_eligibility_is_computed_not_asked_of_jev(snapshot, bars, change, reason):
    with pytest.raises(ValueError, match=reason):
        context(snapshot, (replace(bars[0], **change),))


@pytest.mark.parametrize("updates", [
    {"exit_policy": "BASELINE"}, {"cohort": "BASELINE"}, {"market": "INDIA"},
])
def test_managed_policy_cannot_mutate_baseline_or_indian_research(snapshot, updates):
    with pytest.raises(ValueError, match="MANAGED_PAPER_POLICY_REQUIRED"):
        context(replace(snapshot, **updates))


def test_official_adapter_records_exact_tracking_receipt_and_binds_context(
    cluster, repo, snapshot, bars, news,
):
    ctx = context(snapshot, bars, news)
    reviewed = result(ctx, "TIGHTEN_AND_EXTEND", "STOP_1", "TARGET_1")
    raw = (" \n" + encoded({"model": JEV_MODEL, "answers": reviewed.answers,
                           "usage": {"input_tokens": 20, "output_tokens": 10}}) + "\n").encode()
    sent = []

    def provider(request):
        sent.append(request.content)
        return httpx.Response(200, content=raw)

    store = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    reviewer = JevReviewer(
        store, ReliabilityPolicy("MANAGED_FIXTURE_ONLY", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-provider-credential",
        transport=httpx.MockTransport(provider),
        clock=lambda: NOW,
    )
    request_id = ident()
    decision = asyncio.run(review_managed_position(
        reviewer, ctx, request_id=request_id, purpose="ENGINEERING_TEST",
        current_snapshot=lambda: snapshot, clock=lambda: NOW,
    ))
    assert decision.action == "TIGHTEN_AND_EXTEND"
    assert len(sent) == 1 and len(decision.receipt_ids) == 1
    with store.connect() as conn:
        stored = conn.execute(
            """SELECT q.request_json,q.evidence_identity,r.response_bytes,q.stage
            FROM lab.jev_requests q JOIN lab.jev_receipts r USING(request_id)
            WHERE q.request_id=%s""", (request_id,),
        ).fetchone()
    assert stored["request_json"].encode() == sent[0]
    assert bytes(stored["response_bytes"]) == raw
    assert stored["evidence_identity"]["managed_context_hash"] == ctx.context_hash
    assert stored["stage"] == "TRACKING"
    assert verify_events(repo.export_events())["valid"]


def test_provider_outage_keeps_protection_and_records_receipt(cluster, snapshot, bars):
    ctx = context(snapshot, bars)
    store = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    reviewer = JevReviewer(
        store, ReliabilityPolicy("MANAGED_OUTAGE_FIXTURE", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-provider-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(529)), clock=lambda: NOW,
    )
    decision = asyncio.run(review_managed_position(
        reviewer, ctx, request_id=ident(), purpose="ENGINEERING_TEST",
        current_snapshot=lambda: snapshot, clock=lambda: NOW,
    ))
    assert decision.action == "NEEDS_REVIEW" and decision.receipt_ids
    assert decision.proposed_stop is decision.proposed_target is None
    assert not decision.requires_risk_authorization
    assert snapshot.stop.price == D("98") and snapshot.target.price == D("108")


def test_tampered_answer_cannot_reuse_valid_receipt(cluster, snapshot, bars):
    ctx = context(snapshot, bars)
    reviewed = result(ctx)
    response = {"model": JEV_MODEL, "answers": reviewed.answers,
                "usage": {"input_tokens": 20, "output_tokens": 10}}
    store = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    reviewer = JevReviewer(
        store, ReliabilityPolicy("MANAGED_TAMPER_FIXTURE", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-provider-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        clock=lambda: NOW,
    )
    original = reviewer.jev_review

    async def forged(**kwargs):
        actual = await original(**kwargs)
        changed = result(ctx, "TIGHTEN_STOP", "STOP_1")
        return replace(actual, answers=changed.answers)

    reviewer.jev_review = forged
    decision = asyncio.run(review_managed_position(
        reviewer, ctx, request_id=ident(), purpose="ENGINEERING_TEST",
        current_snapshot=lambda: snapshot, clock=lambda: NOW,
    ))
    assert decision.reason == "RECEIPT_INTEGRITY_FAILED"
    assert not decision.requires_risk_authorization


def production(snapshot):
    """The runtime's shape: a 64-bar one-minute window passed as recent and structural."""
    window = fx.bars(64, now=NOW)
    return replace(snapshot, data_provider=fx.PROVIDER, data_feed=fx.FEED), window


def production_context(snapshot, window, news=()):
    return build_managed_context(
        snapshot, window, news, fx.V3_PRODUCTION_POLICY, now=NOW,
        review_deadline=NOW + timedelta(seconds=10), structural_bars=window,
        dossier=fx.v3_dossier(), trigger=fx.trigger(),
    )


@pytest.mark.parametrize("action,stop,target", [
    ("HOLD", "KEEP", "KEEP"), ("TIGHTEN_STOP", "STOP_1", "KEEP"),
    ("EXTEND_TARGET", "KEEP", "TARGET_5"), ("TIGHTEN_AND_EXTEND", "STOP_5", "TARGET_1"),
])
def test_production_window_v3_keeps_the_bounded_action_contract(snapshot, action, stop,
                                                                target):
    snapshot, window = production(snapshot)
    ctx = production_context(snapshot, window)
    assert len(encoded(ctx.state).encode()) <= fx.V3_PRODUCTION_POLICY.state_byte_budget
    options = ctx.state["eligible_options"]
    assert [len(options["stop"]), len(options["target"])] == [5, 5]
    assert all(D(o["price"]) % snapshot.tick_size == 0 for k in options for o in options[k])
    decision = evaluate_managed_result(ctx, result(ctx, action, stop, target), snapshot, now=NOW)
    prices = {o["option_id"]: D(o["price"]) for k in options for o in options[k]}
    assert decision.action == action
    assert decision.proposed_stop == prices.get(stop) and decision.proposed_target == prices.get(
        target)
    assert evaluate_managed_result(ctx, result(ctx, thesis="REFUTED"), snapshot,
                                   now=NOW).reason == "THESIS_REFUTED_EARLY_EXIT_DISABLED"
    contradictory = result(ctx, "HOLD", "STOP_1", "KEEP")
    assert evaluate_managed_result(ctx, contradictory, snapshot, now=NOW).reason == (
        "CONTRADICTORY_MANAGEMENT_ANSWERS"
    )


def test_production_window_v3_receipt_binding_through_the_official_adapter(cluster, snapshot,
                                                                            news):
    snapshot, window = production(snapshot)
    ctx = production_context(snapshot, window, news)
    reviewed = result(ctx, "TIGHTEN_AND_EXTEND", "STOP_2", "TARGET_2")
    raw = encoded({"model": JEV_MODEL, "answers": reviewed.answers,
                   "usage": {"input_tokens": 20, "output_tokens": 10}}).encode()
    sent = []

    def provider(request):
        sent.append(request.content)
        return httpx.Response(200, content=raw)

    store = JevStore(localdb.connection_url(cluster, "catalyst_jev"))
    reviewer = JevReviewer(
        store, ReliabilityPolicy("MANAGED_V3_FIXTURE_ONLY", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-provider-credential",
        transport=httpx.MockTransport(provider), clock=lambda: NOW,
    )
    request_id = ident()
    decision = asyncio.run(review_managed_position(
        reviewer, ctx, request_id=request_id, purpose="ENGINEERING_TEST",
        current_snapshot=lambda: snapshot, clock=lambda: NOW,
    ))
    assert decision.action == "TIGHTEN_AND_EXTEND" and len(decision.receipt_ids) == 1
    with store.connect() as conn:
        stored = conn.execute(
            """SELECT q.request_json,q.evidence_identity,q.stage,q.question_set_version
            FROM lab.jev_requests q WHERE q.request_id=%s""", (request_id,),
        ).fetchone()
    assert stored["request_json"].encode() == sent[0]
    assert stored["evidence_identity"]["managed_context_hash"] == ctx.context_hash
    assert (stored["stage"], stored["question_set_version"]) == ("TRACKING", QUESTION_VERSION)
    assert ctx.state["context_version"] == CONTEXT_VERSION
    assert len(sent[0]) > len(encoded(ctx.state))  # State plus questions; state within budget.
