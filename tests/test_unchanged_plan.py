"""Unchanged-plan replay: pure boundary tests plus one end-to-end wiring check."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.pick_outcomes import DATA_INCOMPLETE, HOLD_HORIZON, STOP, TARGET, parse_bars
from catalyst_lab.unchanged_plan import (
    STOP_RAISE,
    TARGET_RAISE,
    LevelChange,
    replay_unchanged_plan,
    stop_target_changes,
    unchanged_plan_comparisons,
)
from tests.test_execution import er as er  # noqa: F401 -- ``mx`` needs this fixture in scope
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx, observation, packet  # noqa: F401

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def bar(minute, o, h, low, c, base=T0):
    return {"t": (base + timedelta(minutes=minute)).isoformat(), "o": o, "h": h, "l": low,
            "c": c, "v": "1"}


def stop_raise(old_stop="95", old_target="111", new_stop="99", at=T0):
    return LevelChange(at=at, change_kind=STOP_RAISE, old_stop=D(old_stop),
                       old_target=D(old_target), new_stop=D(new_stop), new_target=D(old_target),
                       source_event_seq=42)


def target_raise(old_stop="95", old_target="111", new_target="120", at=T0):
    return LevelChange(at=at, change_kind=TARGET_RAISE, old_stop=D(old_stop),
                       old_target=D(old_target), new_stop=D(old_stop), new_target=D(new_target),
                       source_event_seq=43)


def test_a_stop_raise_that_actually_helped_shows_a_positive_r_difference():
    # Original stop 95: price dips to 96 (would NOT have stopped out on the original plan),
    # then keeps running to the original target 111. The raised stop (99, not replayed here)
    # is what actually happened; the unchanged plan rides it out to the target.
    change = stop_raise()
    bars = parse_bars([
        bar(0, "100", "100.2", "96", "100"),
        bar(1, "105", "112", "104", "111"),
    ])
    outcome = replay_unchanged_plan(
        D("100.10"), change, bars, initial_stop=D("95"), hold_deadline=T0 + HOLD_HORIZON,
        actual_r=D("0.5"),
    )
    assert outcome.exit_reason == TARGET
    assert outcome.exit_price == D("111")
    assert outcome.data_complete is True
    assert outcome.r_difference == D("0.5") - outcome.unchanged_net_r
    assert outcome.unchanged_gross_r == (D("111") - D("100.10")) / (D("100.10") - D("95"))


def test_a_stop_raise_that_actually_hurt_the_unchanged_plan_would_have_reached_target():
    # The raised stop got hit (actual_r is a loss); the ORIGINAL stop was far enough away that
    # the unchanged plan would have survived the dip and still be running (data incomplete: we
    # only supply bars up to the dip).
    change = stop_raise(new_stop="99")
    bars = parse_bars([bar(0, "100", "100.1", "98.5", "99")])  # dips to 98.5: below new (99),
    # above original (95) -- the actual trade stopped out here, the unchanged plan keeps going.
    outcome = replay_unchanged_plan(
        D("100.10"), change, bars, initial_stop=D("95"), hold_deadline=T0 + HOLD_HORIZON,
        actual_r=D("-1"),
    )
    assert outcome.exit_reason == DATA_INCOMPLETE
    assert outcome.unchanged_net_r is None
    assert outcome.r_difference is None  # Can't diff against an unknown counterfactual.


def test_a_target_raise_that_actually_helped():
    # Original target 111 would have exited there; the actually-applied raise (120, not
    # replayed) let the trade run further for a better real result.
    change = target_raise()
    bars = parse_bars([bar(0, "105", "112", "104", "111")])
    outcome = replay_unchanged_plan(
        D("100.10"), change, bars, initial_stop=D("95"), hold_deadline=T0 + HOLD_HORIZON,
        actual_r=D("3.0"),
    )
    assert outcome.exit_reason == TARGET
    assert outcome.exit_price == D("111")
    assert outcome.r_difference == D("3.0") - outcome.unchanged_net_r
    assert outcome.r_difference > 0  # The raise actually helped versus the original plan.


def test_a_target_raise_that_actually_hurt():
    # The raised target (never reached) meant the trade rode a reversal back to the stop; the
    # unchanged (original, lower) target would have been hit first for a smaller, but locked
    # in, win.
    change = target_raise(new_target="150")
    bars = parse_bars([bar(0, "105", "112", "104", "111")])  # hits the ORIGINAL target 111
    outcome = replay_unchanged_plan(
        D("100.10"), change, bars, initial_stop=D("95"), hold_deadline=T0 + HOLD_HORIZON,
        actual_r=D("-1"),
    )
    assert outcome.exit_reason == TARGET
    assert outcome.r_difference == D("-1") - outcome.unchanged_net_r
    assert outcome.r_difference < 0  # The raise actually hurt versus the original plan.


def test_same_bar_ambiguity_is_resolved_as_the_stop_and_counted():
    change = stop_raise()
    bars = parse_bars([bar(0, "100", "112", "94", "100")])  # low<=95 and high>=111, one bar
    outcome = replay_unchanged_plan(D("100.10"), change, bars, initial_stop=D("95"),
                                    hold_deadline=T0 + HOLD_HORIZON)
    assert outcome.exit_reason == STOP and outcome.same_bar_ambiguous is True


def test_a_24_hour_exit_replay_and_no_actual_r_leaves_the_difference_unknown():
    change = stop_raise()
    deadline = T0 + HOLD_HORIZON
    bars = parse_bars([
        bar(0, "100", "100.2", "99.9", "100"),
        {"t": deadline.isoformat(), "o": "103", "h": "103.2", "l": "102.9", "c": "103", "v": "1"},
    ])
    outcome = replay_unchanged_plan(D("100.10"), change, bars, initial_stop=D("95"),
                                    hold_deadline=deadline)
    assert outcome.exit_price == D("103")
    assert outcome.actual_r is None and outcome.r_difference is None


def test_a_later_change_is_measured_in_the_trades_own_r():
    """A second raise walks the levels in force before it (stop 99) but is measured in the
    trade's own R: (exit - max entry) / (max entry - admitted stop), official_r's denominator,
    not the raised stop's much smaller risk."""
    change = stop_raise(old_stop="99", new_stop="100")
    bars = parse_bars([bar(0, "100", "100.2", "99.5", "100"),
                       bar(1, "105", "112", "104", "111")])
    outcome = replay_unchanged_plan(D("100.10"), change, bars, initial_stop=D("95"),
                                    hold_deadline=T0 + HOLD_HORIZON)
    assert outcome.exit_reason == TARGET and outcome.original_stop == D("99")
    assert outcome.initial_stop == D("95")
    assert outcome.unchanged_gross_r == (D("111") - D("100.10")) / (D("100.10") - D("95"))
    assert outcome.to_dict()["initial_stop"] == "95"


def test_a_change_after_the_stop_passed_the_entry_still_replays():
    """Once a raise has lifted the stop to the max entry or above, the next change's replay
    still has a positive denominator (the admitted risk); it used to refuse
    NONPOSITIVE_RISK_DENOMINATOR, which dropped the whole trade from the nightly replays."""
    change = stop_raise(old_stop="100.50", new_stop="101")
    bars = parse_bars([bar(0, "101", "101.2", "100.4", "100.6")])  # Trades through 100.50.
    outcome = replay_unchanged_plan(D("100.10"), change, bars, initial_stop=D("95"),
                                    hold_deadline=T0 + HOLD_HORIZON)
    assert outcome.exit_reason == STOP and outcome.exit_price == D("100.50")
    assert outcome.unchanged_gross_r == (D("100.50") - D("100.10")) / (D("100.10") - D("95"))
    assert outcome.unchanged_gross_r > 0


def test_level_change_rejects_invalid_original_levels():
    with pytest.raises(ValueError):
        LevelChange(at=T0, change_kind=STOP_RAISE, old_stop=D("100"), old_target=D("90"))


# --- Wiring to today's maintenance mechanism (fixture ledger) -----------------------------------


class FakeBars:
    """Deterministic canned bars for the setup_id-driven wiring test."""

    def __init__(self, rows):
        self.rows = rows

    def minute_bars(self, symbol, start, end):
        return [r for r in self.rows if start <= datetime.fromisoformat(r["t"]) < end]


def test_stop_target_changes_reads_management_plan_authorized_and_the_prior_state(mx):  # noqa: F811
    engine, venue, reviews = mx
    raw = packet(mx, levels={
        "entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111",
    })
    sid = engine.admit(raw)
    # Directly record the OPEN state and an amendment as the running engine's own
    # ``accept_management`` would, so this test stays focused on the reader, not the full
    # authorization/dispatch machinery already covered by tests/test_managed_execution.py.
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "OPEN", stop="95", target="111", qty="1",
                                lifecycle_id="fixture-lifecycle")
        engine.store.event(conn, "MANAGEMENT_PLAN_AUTHORIZED",
                           {"stop": "99", "target": "111", "context_hash": "x",
                            "receipt_ids": [], "expires_at": venue.now.isoformat()},
                           setup_id=sid)
        engine.store.transition(conn, sid, "OPEN", stop="99", target="111", qty="1",
                                lifecycle_id="fixture-lifecycle",
                                amendment_previous_stop="95", amendment_previous_target="111")
    changes = stop_target_changes(engine.repo, sid)
    assert len(changes) == 1
    assert changes[0].change_kind == STOP_RAISE
    assert (changes[0].old_stop, changes[0].new_stop) == (D("95"), D("99"))
    assert changes[0].old_target == D("111")


def test_unchanged_plan_comparisons_wires_fills_amendment_and_bars_together(mx):  # noqa: F811
    engine, venue, reviews = mx
    raw = packet(mx, levels={
        "entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111",
    })
    sid = engine.admit(raw)
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "APPROVED"
    entry = venue.orders_of("buy")[0]
    first_fill_at = venue.now
    assert engine.ingest(venue.fill(entry["id"], "1"))
    engine.manage(sid, observation(mx))  # Places the resting stop/target protection.
    with engine.store.transaction() as conn:
        state = engine.store.state(conn, sid)
        engine.store.event(conn, "MANAGEMENT_PLAN_AUTHORIZED",
                           {"stop": "99", "target": state["target"], "context_hash": "x",
                            "receipt_ids": [], "expires_at": venue.now.isoformat()},
                           setup_id=sid)
        engine.store.transition(conn, sid, "OPEN", stop="99", target=state["target"],
                                qty=state["qty"], lifecycle_id=state["lifecycle_id"],
                                amendment_previous_stop="95",
                                amendment_previous_target=state["target"])
    deadline = first_fill_at + HOLD_HORIZON
    bars = FakeBars([
        {"t": (venue.now + timedelta(minutes=1)).isoformat(), "o": "105", "h": "112",
         "l": "104", "c": "111", "v": "1"},
    ])
    comparisons = unchanged_plan_comparisons(engine.repo, bars, sid, now=deadline)
    assert len(comparisons) == 1
    outcome = comparisons[0]
    assert outcome.change_kind == STOP_RAISE
    assert outcome.original_stop == D("95")
    assert outcome.exit_reason == TARGET  # The original 111 target is what the fixture bar hits.
    assert outcome.actual_r is None  # The position never closed in this fixture: unknown, not 0.


def test_a_widened_level_is_never_read_as_a_raise(mx):  # noqa: F811
    engine, venue, reviews = mx
    raw = packet(mx, levels={
        "entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111",
    })
    sid = engine.admit(raw)
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "OPEN", stop="95", target="111", qty="1",
                                lifecycle_id="fixture-lifecycle")
        engine.store.event(conn, "MANAGEMENT_PLAN_AUTHORIZED",
                           {"stop": "95", "target": "111", "context_hash": "x",
                            "receipt_ids": [], "expires_at": venue.now.isoformat()},
                           setup_id=sid)
    assert stop_target_changes(engine.repo, sid) == []
