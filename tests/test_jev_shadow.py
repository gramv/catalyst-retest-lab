"""Deterministic synthetic shadow proofs: no network, model call or broker mutation."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.jev_shadow import (
    ShadowCase,
    ShadowEvidence,
    ShadowObservation,
    ShadowPolicy,
    ShadowStore,
    evaluate,
    export_metrics,
    prequalify,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def policy(**changes):
    return replace(ShadowPolicy(D(150), D(10000), D(".01"), D(20), D(".25"),
                                D(0), D(5), 0), **changes)


def case(**changes):
    return replace(ShadowCase(
        case_id="fixture-1", symbol="TEST/USD", venue="FIXTURE", as_of=NOW, decision_at=NOW,
        expires_at=NOW + timedelta(seconds=30), exit_deadline=NOW + timedelta(minutes=5),
        entry_trigger=D(100), max_entry_price=D(101), stop=D(95), target=D(114),
        quantity=D(1), bid=D("99.99"), ask=D("100.01"), quote_at=NOW,
        observed_dollar_volume=D(100000), liquidity_at=NOW, policy=policy(),
        evidence=(ShadowEvidence("level-1", NOW, NOW, '{"fixture":true}'),),
        level_evidence_ids=("level-1",), jev_decision="ACCEPT", jev_policy_version="FIXTURE_V1",
        jev_receipt_hash="fixture-receipt-hash", session_id="2026-09-20", event_id="event-1",
    ), **changes)


def tick(second=1, **changes):
    return replace(ShadowObservation(
        observation_id=f"tick-{second}", case_id="fixture-1", symbol="TEST/USD", venue="FIXTURE",
        kind="tick", at=NOW + timedelta(seconds=second),
        received_at=NOW + timedelta(seconds=second), trade_price=D(100), bid=D("99.99"),
        ask=D("100.01"), quote_at=NOW + timedelta(seconds=second),
        bid_size=D(1), ask_size=D(1),
    ), **changes)


def bar(second=10, **changes):
    return replace(ShadowObservation(
        observation_id=f"bar-{second}", case_id="fixture-1", symbol="TEST/USD", venue="FIXTURE",
        kind="bar", at=NOW + timedelta(seconds=second),
        received_at=NOW + timedelta(seconds=second), start_at=NOW + timedelta(seconds=3),
        open=D(101), high=D(114), low=D(100), close=D(113), volume=D(1000),
    ), **changes)


def run(observations, c=None, second=20):
    return evaluate(c or case(), observations, as_of=NOW + timedelta(seconds=second))


def winning_path():
    return [tick(), tick(2), tick(3, trade_price=D(115), bid=D(115), ask=D("115.01"))]


def test_pure_crypto_trigger_is_reused(monkeypatch):
    from catalyst_lab.managed_execution import ManagedExecution
    original = ManagedExecution._trigger_failure
    calls = []

    def wrapped(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(ManagedExecution, "_trigger_failure", wrapped)
    result = run(winning_path())
    assert calls and calls[0][0] is None
    assert result.baseline.state == "CLOSED"
    assert result.treatment == result.baseline
    assert result.baseline.entry_notional == D("100.01")
    assert result.baseline.exit_notional == D(114)
    assert result.baseline.net_pnl == D("13.882995")
    assert result.baseline.net_r == D("13.882995") / 6


def test_rejected_eligible_case_gets_identical_counterfactual_path():
    accepted = run(winning_path())
    rejected = run(winning_path(), case(jev_decision="REJECT"))
    assert rejected.baseline == accepted.baseline
    assert rejected.treatment is None
    metrics = export_metrics([rejected])["cohorts"]["ENGINEERING"]
    assert metrics["eligible_cases"] == 1
    assert D(metrics["mean_treatment_minus_baseline_net_r"]) < 0
    assert metrics["jev_filtered"]["case_count"] == 0


def test_model_service_failure_is_not_scored_as_rejection():
    result = run(winning_path(), case(jev_decision="SERVICE_FAILURE"))
    assert result.baseline.state == "CLOSED" and result.treatment is None
    assert export_metrics([result])["cohorts"]["ENGINEERING"]["paired_known_closed_cases"] == 0


def test_untriggered_bars_never_substitute_for_prints():
    result = run([bar(low=D(99))])
    assert result.baseline.state == "UNTRIGGERED"
    assert result.baseline.entered_quantity == 0
    assert run([], second=31).baseline.reason == "EXPIRED_UNTRIGGERED"


def test_trigger_does_not_imply_fill_and_gap_above_limit_stays_unfilled():
    result = run([tick(), tick(2, trade_price=D(102), bid=D(102), ask=D("102.01"))], second=31)
    assert result.baseline.state == "UNFILLED"
    assert result.baseline.reason == "EXPIRED_UNFILLED"
    assert result.baseline.entry_notional == 0


def test_unknown_displayed_liquidity_never_gets_fabricated_fill():
    assert run([tick(), tick(2, ask_size=None)]).baseline.state == "UNFILLED"


def test_trigger_quote_cannot_fill_and_latency_is_honored():
    assert run([tick()]).baseline.state == "UNFILLED"
    delayed = case(policy=policy(entry_latency_ms=2000))
    assert run([tick(), tick(2)], delayed).baseline.state == "UNFILLED"
    assert run([tick(), tick(2), tick(3)], delayed).baseline.state == "OPEN"


def test_no_future_quote_print_or_case_evidence():
    with pytest.raises(ValueError, match="FUTURE_CASE_EVIDENCE"):
        case(quote_at=NOW + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="FUTURE_CASE_EVIDENCE"):
        case(evidence=(ShadowEvidence("level-1", NOW + timedelta(seconds=1), NOW, '{}'),))
    with pytest.raises(ValueError, match="TIMELINE"):
        tick(received_at=NOW)
    with pytest.raises(ValueError, match="TIMELINE"):
        tick(quote_at=NOW + timedelta(seconds=2))
    assert run(winning_path(), second=1).baseline.state == "UNFILLED"
    assert run(winning_path(), second=2).baseline.state == "OPEN"


def test_future_evidence_is_not_visible_until_received():
    obs = tick(2, received_at=NOW + timedelta(seconds=4))
    assert run([tick(), obs], second=3).baseline.state == "UNFILLED"
    assert run([tick(), obs], second=4).baseline.state == "OPEN"


@pytest.mark.parametrize(("changes", "reason"), [
    ({"target": D(110)}, "MIN_REWARD_RISK"),
    ({"stop": D(100)}, "INVALID_LEVELS"),
    ({"quote_at": NOW - timedelta(seconds=6)}, "STALE_QUOTE"),
    ({"liquidity_at": NOW - timedelta(seconds=91)}, "STALE_LIQUIDITY"),
    ({"ask": D(151)}, "PRICE_CEILING"),
    ({"observed_dollar_volume": D(100)}, "CRYPTO_VENUE_LIQUIDITY_INSUFFICIENT"),
    ({"quantity": D(1000)}, "CRYPTO_VENUE_PARTICIPATION_LIMIT"),
    ({"quantity": D(0)}, "ZERO_QUANTITY"),
    ({"level_evidence_ids": ("not-in-packet",)}, "LEVEL_PROVENANCE_MISSING"),
    ({"policy": policy(assumed_round_trip_cost_bps=D(1000))},
     "CRYPTO_ASSUMED_COST_EXCEEDS_POLICY"),
])
def test_prequalification_failures_are_retained_and_block_both_arms(changes, reason):
    c = case(**changes)
    assert reason in prequalify(c).reasons
    result = run(winning_path(), c)
    assert result.baseline.state == "INELIGIBLE"
    assert result.treatment.state == "INELIGIBLE"
    assert result.baseline.entered_quantity == 0


def test_unknown_fees_are_unknown_net_and_cannot_be_zero_cost_profit():
    result = run(winning_path(), case(policy=policy(fee_bps_per_side=None)))
    assert result.baseline.state == "CLOSED"
    assert result.baseline.gross_pnl == D("13.99")
    assert result.baseline.net_pnl is result.baseline.net_r is result.baseline.assumed_fees is None
    metrics = export_metrics([result])["cohorts"]["ENGINEERING"]["rules_only"]
    assert metrics["unknown_fee_closed_cases"] == 1
    assert metrics["mean_net_r_known_closed_subset"] is None


def test_partial_fills_and_partial_exit_use_available_quote_sizes():
    result = run([tick(), tick(2, ask_size=D(".3")),
                  tick(3, ask_size=D(".2")),
                  tick(4, trade_price=D(114), bid=D(114), ask=D("114.01"), bid_size=D(".1")),
                  tick(5, trade_price=D(114), bid=D(114), ask=D("114.01"), bid_size=D(".4"))])
    assert result.baseline.state == "CLOSED"
    assert result.baseline.entered_quantity == result.baseline.exited_quantity == D(".5")
    assert result.baseline.remaining_quantity == 0


def test_repeated_quote_snapshot_cannot_supply_liquidity_twice():
    result = run([tick(), tick(2, ask_size=D(".3")),
                  tick(3, ask_size=D(".3"), quote_at=NOW + timedelta(seconds=2))])
    assert result.baseline.entered_quantity == D(".3")


def test_both_exit_levels_in_bar_remain_ambiguous_without_pnl():
    result = run([tick(), tick(2), bar(low=D(94), high=D(115))])
    assert result.baseline.state == "AMBIGUOUS"
    assert result.baseline.reason == "BAR_TOUCHED_STOP_AND_TARGET"
    assert result.baseline.net_r is None


def test_bar_overlapping_entry_cannot_order_entry_and_exit():
    result = run([tick(), tick(2), bar(start_at=NOW + timedelta(seconds=1))])
    assert result.baseline.state == "AMBIGUOUS"
    assert result.baseline.reason == "BAR_OVERLAPS_ENTRY"


def test_single_stop_bar_gaps_use_open_below_stop_with_slippage():
    result = run([tick(), tick(2), bar(open=D(90), low=D(89), high=D(94), close=D(91))])
    assert result.baseline.state == "CLOSED"
    assert result.baseline.exit_notional == D(90)
    assert result.baseline.reason == "STOP_BAR_MODEL"


def test_bar_volume_bounds_partial_exits():
    result = run([tick(), tick(2), bar(volume=D(10))])
    assert result.baseline.state == "OPEN"
    assert result.baseline.exited_quantity == D(".10")


def test_stale_observation_is_neither_trigger_nor_fill():
    assert run([tick(1, received_at=NOW + timedelta(seconds=7))]).baseline.state == "UNTRIGGERED"
    result = run([tick(), tick(2, received_at=NOW + timedelta(seconds=8))])
    assert result.baseline.state == "UNFILLED"


def test_stop_or_chase_before_entry_invalidates_without_orders():
    result = run([tick(trade_price=D(94))])
    assert result.baseline.state == "UNTRIGGERED"
    assert result.baseline.reason == "STOP_TRADED_BEFORE_TRIGGER"
    result = run([tick(bid=D(102), ask=D("102.01"))])
    assert result.baseline.reason == "PRICE_BEYOND_MAX_ENTRY"


def test_expiry_prevents_late_entry_and_missing_time_exit_does_not_invent_close():
    assert run([tick(), tick(31)], second=32).baseline.state == "UNFILLED"
    result = run([tick(), tick(2)], second=301)
    assert result.baseline.state == "OPEN"
    assert result.baseline.reason == "EXIT_EVIDENCE_MISSING"
    result = run([tick(), tick(2), tick(301)], second=302)
    assert result.baseline.state == "CLOSED"
    assert result.baseline.reason == "TIME_EXIT"


def test_duplicate_and_order_checks_are_deterministic():
    assert run([tick(), tick(), tick(2)]).baseline == run([tick(), tick(2)]).baseline
    with pytest.raises(ValueError, match="ID_CONFLICT"):
        run([tick(), tick(ask_size=D(4))])
    with pytest.raises(ValueError, match="OUT_OF_ORDER"):
        run([tick(2), tick(1)])
    with pytest.raises(ValueError, match="MISMATCH_OR_PREDECISION"):
        run([tick(-1)])


def test_store_restarts_deduplicates_and_freezes_cases(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    clock = lambda: NOW + timedelta(minutes=10)  # noqa: E731
    with ShadowStore(path, clock=clock) as store:
        assert store.register(case()) is True
        assert store.register(case()) is False
        with pytest.raises(ValueError, match="IMMUTABLE_EVENT_CONFLICT"):
            store.register(case(jev_decision="REJECT"))
        for observation in winning_path():
            assert store.append(observation)
            assert not store.append(observation)
        original = store.evaluate("fixture-1", as_of=NOW + timedelta(seconds=20))
        head = store.verify()
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            store.conn.execute("DELETE FROM shadow_events")
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            store.conn.execute("UPDATE shadow_events SET kind='CASE'")
    with ShadowStore(path, clock=clock) as store:
        assert store.verify() == head
        assert store.evaluate("fixture-1", as_of=NOW + timedelta(seconds=20)) == original
        with pytest.raises(ValueError, match="OUT_OF_ORDER"):
            store.append(tick(2, observation_id="out-of-order"))
        assert len(store.observations("fixture-1")) == 3
        assert store.export_metrics(as_of=NOW + timedelta(seconds=20))["portfolio_return"] is None
    assert path.stat().st_mode & 0o777 == 0o600


def test_store_validates_case_binding_and_future_receipt(tmp_path):
    with ShadowStore(tmp_path / "shadow.sqlite3", clock=lambda: NOW) as store:
        store.register(case())
        with pytest.raises(ValueError, match="FUTURE_OBSERVATION"):
            store.append(tick())
        with pytest.raises(ValueError, match="UNKNOWN_SHADOW_CASE"):
            store.append(tick(0, case_id="other"))


def test_case_roundtrip_has_stable_hash_and_frozen_evidence():
    c = case()
    raw = c.to_dict()
    assert ShadowCase.from_dict(raw) == c
    assert ShadowCase.from_dict(raw).case_hash == c.case_hash
    raw["evidence"][0]["content_json"] = '{}'
    assert c.evidence[0].content_json == '{"fixture":true}'
    assert ShadowCase.from_dict(raw).case_hash != c.case_hash


def test_metrics_separate_engineering_and_prospective_cases():
    first = run(winning_path())
    second = replace(first, case_id="prospective-1", cohort="PROSPECTIVE")
    result = export_metrics([first, second])
    assert set(result["cohorts"]) == {"ENGINEERING", "PROSPECTIVE"}
    assert result["cohorts"]["ENGINEERING"]["cases"] == 1
    assert result["portfolio_drawdown"] is None
    with pytest.raises(ValueError, match="DUPLICATE_METRIC"):
        export_metrics([first, first])


def test_target_partial_remainder_cannot_fill_below_target_limit():
    result = run([tick(), tick(2),
                  tick(3, trade_price=D(114), bid=D(114), ask=D("114.01"), bid_size=D(".4")),
                  tick(4, bid_size=D(1))])
    assert result.baseline.state == "OPEN"
    assert result.baseline.exited_quantity == D(".4")
    assert result.baseline.remaining_quantity == D(".6")


def test_venue_binding_and_overlapping_bars_fail_closed():
    with pytest.raises(ValueError, match="MISMATCH_OR_PREDECISION"):
        run([tick(venue="DIFFERENT_VENUE")])
    with pytest.raises(ValueError, match="OVERLAPPING_BAR"):
        run([bar(high=D(113)), bar(11)])


def test_stop_before_any_fill_cancels_even_without_displayed_size():
    result = run([tick(), tick(2, trade_price=D(94), bid=D(94), ask=D("94.01"), ask_size=None),
                  tick(3)])
    assert result.baseline.state == "UNFILLED"
    assert result.baseline.reason == "STOP_BEFORE_LIMIT_FILL"


def test_unknown_cost_subset_is_null_not_zero_in_metrics():
    result = run(winning_path(), case(policy=policy(fee_bps_per_side=None)))
    arm = export_metrics([result])["cohorts"]["ENGINEERING"]["rules_only"]
    assert arm["net_pnl_known_subset"] is None
    assert arm["assumed_fees_known_closed_subset"] is None
    assert arm["fill_rate_of_triggered"] == "1"


def test_prospective_observations_must_follow_durable_freeze(tmp_path):
    now = [NOW + timedelta(seconds=2)]
    with ShadowStore(tmp_path / "shadow.sqlite3", clock=lambda: now[0]) as store:
        store.register(case(cohort="PROSPECTIVE"))
        now[0] = NOW + timedelta(seconds=4)
        with pytest.raises(ValueError, match="PRECEDES_CASE_FREEZE"):
            store.append(tick(1))
        assert store.append(tick(3))
        with pytest.raises(ValueError, match="BEFORE_PROSPECTIVE_CASE_FREEZE"):
            store.evaluate("fixture-1", as_of=NOW + timedelta(seconds=1))
        assert store.evaluate("fixture-1", as_of=now[0]).baseline.state == "UNFILLED"
