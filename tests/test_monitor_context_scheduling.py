"""Bounded monitoring dossier and durable event-driven review scheduling."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.jev_contract import encoded
from catalyst_lab.managed_review import (
    CONTEXT_VERSION_V2,
    build_managed_context,
    evaluate_managed_result,
    managed_questions,
)
from catalyst_lab.position_monitor import engineering_monitor_trigger_policy
from catalyst_lab.position_news import PositionNewsService
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_managed_review import POLICY, ident, result
from tests.test_managed_review import bars as bars
from tests.test_managed_review import snapshot as snapshot
from tests.test_position_monitor import bars as position_bars
from tests.test_position_monitor import context_from_ledger, monitor, opened
from tests.test_position_news import auth, client_for, packet


def run(watcher, sid, quote, completed, **kwargs):
    return asyncio.run(watcher.review(
        sid, quote, completed, fresh_observation=lambda: quote, **kwargs
    ))


def test_near_target_has_one_durable_review_per_protection_context(mx):
    _, venue, _ = mx
    sid = opened(mx)
    watcher, calls = monitor(mx, "HOLD")
    watcher.trigger_policy = engineering_monitor_trigger_policy()
    completed = position_bars(mx)
    run(watcher, sid, observation(mx, bid="108", ask="108.01"), completed)
    venue.now += timedelta(seconds=1)
    assert run(watcher, sid, observation(mx, bid="109", ask="109.01"), completed) is None
    venue.now += timedelta(seconds=4)
    run(watcher, sid, observation(mx, bid="109", ask="109.01"), completed)
    assert len(calls) == 2
    assert calls[-1]["state"]["review_trigger"]["reasons"] == ["NEAR_TARGET"]
    assert D(calls[-1]["state"]["computed_position"]["target_progress_fraction"]) > D(".8")
    # Price retreat, a new process, and another approach do not buy a new vote.
    restarted, new_calls = monitor(mx, "HOLD")
    restarted.trigger_policy = engineering_monitor_trigger_policy()
    for bid in ("108", "109", "110"):
        venue.now += timedelta(seconds=5)
        assert run(restarted, sid, observation(mx, bid=bid, ask=str(D(bid) + D(".01"))),
                   completed) is None
    assert new_calls == []


def test_coalesced_new_bar_news_and_near_target_preserve_original_sources(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    watcher, calls = monitor(mx, "HOLD")
    watcher.trigger_policy = engineering_monitor_trigger_policy()
    run(watcher, sid, observation(mx, bid="108", ask="108.01"), position_bars(mx))
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    venue.now += timedelta(seconds=5)
    source = packet(service, sid, venue.now)
    source["sources"][0]["published_at"] = None
    service.submit(sid, source)
    run(watcher, sid, observation(mx, bid="109", ask="109.01"), position_bars(mx))
    state = calls[-1]["state"]
    assert len(calls) == 2
    assert set(state["review_trigger"]["reasons"]) == {
        "COMPLETED_BAR", "MATERIAL_NEWS", "NEAR_TARGET"
    }
    assert state["context_version"] == CONTEXT_VERSION_V2  # The monitor fixture runs V2.
    assert str(sid) not in str(state)
    assert state["news"][0]["published_at"] is None
    assert state["news"][0]["url"] == source["sources"][0]["url"]
    assert state["news"][0]["stance"] == "ADVERSE"
    assert state["original_news"][0]["source_id"] == "fixture-release"
    assert state["original_news"][0]["origin"] == "ORIGINAL_RESEARCH"
    dossier = state["dossier"]
    assert dossier["original_research"]["thesis"] == state["thesis"]
    assert dossier["selection_judgment"]["eligibility"]["answers"]["verdict"]["choice"] == "APPROVE"
    assert any(row["kind"] == "MANAGED_JEV_JUDGMENT" for row in dossier["management_history"])
    assert dossier["sampled_excursions"]["sample_count"] >= 1
    assert dossier["evidence_manifest"]["included_current_source_hashes"][0] == (
        source["sources"][0]["content_hash"]
    )
    assert D(state["computed_position"]["remaining_loss_to_stop_from_entry"]) > 0


def test_material_acknowledged_protection_change_triggers_after_coalescing_interval(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    watcher, calls = monitor(mx, "TIGHTEN_STOP")
    watcher.trigger_policy = engineering_monitor_trigger_policy()
    completed = position_bars(mx)
    quote = observation(mx, bid="108", ask="108.01")
    run(watcher, sid, quote, completed)
    engine.manage(sid, quote)
    engine.manage(sid, quote)
    assert run(watcher, sid, quote, completed) is None
    venue.now += timedelta(seconds=5)
    hold, hold_calls = monitor(mx, "HOLD")
    hold.trigger_policy = engineering_monitor_trigger_policy()
    run(hold, sid, observation(mx, bid="108", ask="108.01"), completed)
    assert len(calls) == len(hold_calls) == 1
    assert hold_calls[0]["state"]["review_trigger"]["reasons"] == [
        "POSITION_OR_PROTECTION_CHANGED"
    ]
    assert hold_calls[0]["state"]["stop"]["price"] == "102"


def test_concurrent_near_target_ticks_claim_one_request(mx):
    _, _, _ = mx
    sid = opened(mx)
    watcher, calls = monitor(mx, "HOLD")
    watcher.trigger_policy = engineering_monitor_trigger_policy()
    completed = position_bars(mx)
    quote = observation(mx, bid="109", ask="109.01")

    async def concurrent():
        return await asyncio.gather(*(
            watcher.review(sid, quote, completed, fresh_observation=lambda: quote)
            for _ in range(3)
        ))

    outcomes = asyncio.run(concurrent())
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("symbol", ["SPY", "BTC/USD"])
def test_target_touch_during_model_request_preserves_original_exit(mx, symbol):
    engine, _, _ = mx
    sid = opened(mx, symbol)
    old_target = engine._load(sid)[1]["target"]
    watcher, calls = monitor(mx, "EXTEND_TARGET")
    quote = observation(mx, bid="109", ask="109.01")
    result = asyncio.run(watcher.review(
        sid, quote, position_bars(mx),
        fresh_observation=lambda: observation(
            mx, bid=old_target, ask=str(D(old_target) + D(".01"))
        ),
    ))
    assert result.receipt_ids and len(calls) == 1
    assert engine._load(sid)[1]["target"] == old_target
    context = context_from_ledger(engine, sid)
    fresh = watcher.snapshot(
        sid, observation(mx, bid=old_target, ask=str(D(old_target) + D(".01")))
    )
    assert evaluate_managed_result(context, result, fresh, now=watcher.now()).reason == (
        "MECHANICAL_EXIT_HAS_PRIORITY"
    )


def test_structural_target_uses_only_provenance_bound_completed_price(snapshot, bars):
    structural = replace(
        bars[0], observation_id=ident(),
        starts_at=snapshot.snapshot_at - timedelta(hours=3),
        ends_at=snapshot.snapshot_at - timedelta(hours=2), high=D("120.02"),
    )
    ctx = build_managed_context(
        snapshot, (), (), POLICY, now=snapshot.snapshot_at,
        review_deadline=snapshot.snapshot_at + timedelta(seconds=10), structural_bars=(structural,),
    )
    assert ctx.state["eligible_options"]["stop"] == []
    option = ctx.state["eligible_options"]["target"][0]
    assert option["price"] == "120.05"
    assert option["basis"] == "structural_completed_bar_high"
    assert option["data_provider"] == structural.data_provider
    decision = evaluate_managed_result(
        ctx, result(ctx, action="EXTEND_TARGET", target="TARGET_1"), snapshot,
        now=snapshot.snapshot_at,
    )
    assert decision.proposed_target == D("120.05")
    assert decision.proposed_stop is None
    for invalid in (
        replace(structural, ends_at=snapshot.snapshot_at + timedelta(seconds=1)),
        replace(structural, data_feed="UNRELATED_RESEARCH_VENUE"),
    ):
        with pytest.raises(ValueError, match="COMPLETED_BAR_REQUIRED|BAR_PROVENANCE_MISMATCH"):
            build_managed_context(
                snapshot, (), (), POLICY, now=snapshot.snapshot_at,
                review_deadline=snapshot.snapshot_at + timedelta(seconds=10),
                structural_bars=(invalid,),
            )
    assert managed_questions(ctx).version == "JEV_MANAGED_POSITION_QUESTIONS_V2"


def test_http_null_publication_and_minimal_source_schema_are_preserved(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    raw = packet(service, sid, venue.now)
    raw["sources"][0] = {k: v for k, v in raw["sources"][0].items() if k in {
        "source_id", "url", "excerpt", "published_at", "retrieved_at"
    }}
    raw["sources"][0]["published_at"] = None
    client = client_for(engine, service)
    route = f"/api/v1/lab/positions/{sid}/news"
    response = client.post(route, headers=auth(), json=raw)
    assert response.status_code == 200
    assert client.post(route, headers=auth(), json=raw).json()["idempotent_replay"]
    recorded = client.get(route, headers=auth()).json()["sources"][0]
    assert recorded["published_at"] is None and len(recorded["content_hash"]) == 64


def test_expired_pending_context_is_consumed_without_same_evidence_reroll(mx, monkeypatch):
    engine, venue, _ = mx
    sid = opened(mx)
    watcher, calls = monitor(mx, "HOLD")
    watcher.trigger_policy = engineering_monitor_trigger_policy()
    completed = position_bars(mx)
    original = engine.accept_management

    def crash(*args):
        raise RuntimeError("FIXTURE_CRASH_AFTER_RECEIPT")

    monkeypatch.setattr(engine, "accept_management", crash)
    with pytest.raises(RuntimeError, match="FIXTURE_CRASH"):
        run(watcher, sid, observation(mx, bid="109", ask="109.01"), completed)
    monkeypatch.setattr(engine, "accept_management", original)
    venue.now += timedelta(seconds=11)
    restarted, replay_calls = monitor(mx, "HOLD")
    restarted.trigger_policy = engineering_monitor_trigger_policy()
    assert run(restarted, sid, observation(mx, bid="109", ask="109.01"), completed) is None
    assert len(calls) == 1 and replay_calls == []


def test_v3_production_window_near_target_and_news_coalesce_within_budget(mx):
    """V3 profile with the runtime's 60-bar window as recent and structural bars."""
    from catalyst_lab.managed_runtime import engineering_monitor_policy
    from tests import managed_dossier_fixtures as fx

    engine, venue, _ = mx
    sid = opened(mx)
    policy = engineering_monitor_policy()
    watcher, calls = monitor(mx, "HOLD", policy=policy)
    watcher.trigger_policy = engineering_monitor_trigger_policy()
    window = fx.monitor_bars(venue.now)
    run(watcher, sid, observation(mx, bid="108", ask="108.01"), window, structural_bars=window)
    venue.now += timedelta(seconds=1)
    assert run(watcher, sid, observation(mx, bid="109", ask="109.01"), window,
               structural_bars=window) is None  # Inside the coalescing interval.
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    venue.now += timedelta(seconds=4)
    source = packet(service, sid, venue.now)
    service.submit(sid, source)
    run(watcher, sid, observation(mx, bid="109", ask="109.01"), window, structural_bars=window)
    assert len(calls) == 2
    first, second = (call["state"] for call in calls)
    assert first["review_trigger"]["reasons"] == ["INITIAL_OPEN_CONTEXT"]
    assert set(second["review_trigger"]["reasons"]) == {"MATERIAL_NEWS", "NEAR_TARGET"}
    assert second["computed_position"]["regime"] == "NEAR"
    for state in (first, second):
        assert len(encoded(state).encode()) <= policy.state_byte_budget
        assert len(state["bars"]["rows"]) == 60
    adverse = second["news"]["current"][0]
    assert adverse["stance"] == "ADVERSE" and adverse["source_id"] == "issuer-new"
    assert adverse["excerpt"] == source["sources"][0]["excerpt"]
    # The research source is still current evidence: sent once there, and recorded, not
    # repeated, for the original list.
    assert [n["source_id"] for n in second["news"]["current"]] == ["issuer-new",
                                                                    "fixture-release"]
    assert second["news"]["original"] == []
    context = context_from_ledger(engine, sid)
    same = [e for e in context.data["manifest"]["entries"] if e["section"] == "news.original"]
    assert [e["reason"] for e in same] == ["SAME_EXCERPT_IN_CURRENT"]
    assert second["selection_judgments"]["eligibility"]["verdict"]["choice"] == "APPROVE"
    assert [row["action"] for row in second["management_history"]] == ["HOLD"]
    assert context.data["retained"]["news_sources"][source["sources"][0]["content_hash"]][
        "kind"] == "POSITION_NEWS"
    venue.now += timedelta(seconds=5)
    assert run(watcher, sid, observation(mx, bid="109", ask="109.01"), window,
               structural_bars=window) is None  # Unchanged evidence never buys another vote.
    assert len(calls) == 2
