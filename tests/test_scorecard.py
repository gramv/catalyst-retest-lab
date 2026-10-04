"""DAILY_SCORECARD_V1 and the recorded maintenance replays (package learning-app).

Fixture evidence only: the fixture paper venue, a mock Jev, canned bars and per-test disposable
PostgreSQL databases. No broker, provider, network or owner-ledger contact.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_replays import (
    DAY_REPLAY_EVENT,
    REPLAY_EVENT,
    day_decision_replay,
    record_replays,
)
from catalyst_lab.market import NY
from catalyst_lab.pick_outcomes import parse_bars
from catalyst_lab.scorecard import (
    SCORECARD_EVENT,
    ScorecardUnavailable,
    compute_scorecard,
    distance_bucket,
    exit_kind,
    notable_reasons,
    record_scorecard,
    rule_bucket,
    timeframe_bucket,
)
from tests.learning_fixtures import FakeBars, agent_block, close_attributed, shadow, v3_cycle
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401

# --- Classification -----------------------------------------------------------------------------


@pytest.mark.parametrize(("price", "entry", "bucket"), [
    ("100", "99.5", "0-1%"), ("100", "99", "1-2%"), ("100", "101.5", "1-2%"),
    ("100", "97.01", "2-3%"), ("100", "97", "3%+"), (None, "97", "UNKNOWN"), ("0", "1", "UNKNOWN"),
])
def test_distance_to_entry_buckets(price, entry, bucket):
    assert distance_bucket(price, entry) == bucket


def test_timeframes_rules_exits_and_notability():
    assert [timeframe_bucket(v) for v in (3600, "14400", 86400, 900, None)] == [
        "1h", "4h", "1d", "OTHER", "NONE"]
    assert rule_bucket("Chosen among rule-A 4-hour setups.", None) == "A"
    assert rule_bucket("rule-B", "rule-B structure") == "B"
    assert rule_bucket("rule-A then rule-B", None) == "UNDECLARED"
    assert rule_bucket("no rule named", "overrule-A") == "UNDECLARED"
    assert [exit_kind(r) for r in ("BROKER_EXIT", "TARGET_EXIT", "EARLY_EXIT_AGREED",
                                   "DAY_REVIEW_EXIT", "HOLD_24H_EXIT", "DAILY_RISK_HALT")] == [
        "STOP", "TARGET", "EARLY_EXIT", "TWENTY_FOUR_HOUR_EXIT", "TWENTY_FOUR_HOUR_EXIT", "OTHER"]
    assert notable_reasons("STOP", D("-1")) == ["STOP"]
    assert notable_reasons("TARGET", D("2.1")) == ["WIN_OVER_1_5R"]
    assert notable_reasons("TARGET", D("1.5")) == []
    assert notable_reasons("TWENTY_FOUR_HOUR_EXIT", None) == ["TWENTY_FOUR_HOUR_EXIT"]


# --- DAY_REVIEW_DECISION_REPLAY_V1 --------------------------------------------------------------


AT = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)


def rows(*bars):
    return parse_bars([{"t": (AT + timedelta(minutes=m)).isoformat(), "o": o, "h": h, "l": low,
                        "c": c, "v": "1"} for m, o, h, low, c in bars])


def test_a_continue_is_compared_with_exiting_at_the_decision():
    decision = {"outcome": "CONTINUE", "at": AT.isoformat(), "old_stop": "95",
                "old_target": "111"}
    record = day_decision_replay(decision, rows((1, "104", "105", "103", "104")),
                                 entry_price=D("100.10"), initial_stop=D("95"))
    assert record["alternative"] == "EXIT_AT_DECISION" and record["data_complete"]
    assert record["exit_price"] == "104" and record["exit_at"] == (AT + timedelta(minutes=1)
                                                                   ).isoformat()
    gross = (D("104") - D("100.10")) / (D("100.10") - D("95"))
    assert D(record["alternative_gross_r"]) == gross
    late = day_decision_replay(decision, rows((16, "104", "105", "103", "104")),
                               entry_price=D("100.10"), initial_stop=D("95"))
    assert late["exit_reason"] == "DATA_INCOMPLETE" and not late["data_complete"]


def test_an_exit_is_compared_with_24_more_hours_on_the_levels_in_force():
    decision = {"outcome": "EXIT", "at": AT.isoformat(), "old_stop": "99", "old_target": "115"}
    stopped = day_decision_replay(decision, rows((5, "103", "104", "98", "99")),
                                  entry_price=D("100.10"), initial_stop=D("95"))
    assert (stopped["alternative"], stopped["exit_reason"], stopped["exit_price"]) == (
        "CONTINUE_24H_ON_LEVELS_IN_FORCE", "STOP", "99")
    held = day_decision_replay(decision, rows((5, "103", "104", "102", "103"),
                                              (24 * 60, "106", "107", "105", "106")),
                               entry_price=D("100.10"), initial_stop=D("95"))
    assert (held["exit_reason"], held["exit_price"]) == ("HOLD_24H_EXIT", "106")
    open_ended = day_decision_replay(decision, rows((5, "103", "104", "102", "103")),
                                     entry_price=D("100.10"), initial_stop=D("95"))
    assert not open_ended["data_complete"]


# --- The recorded day ---------------------------------------------------------------------------


def scored_day(mx):  # noqa: F811
    """A closed, verified trade of agent claude with its research, shadow outcomes, a
    post-mortem and two replays; returns (store, setup_id, day)."""
    engine, venue, _ = mx
    sid, raw = close_attributed(mx, "BTC/USD")
    day = venue.now.astimezone(NY).date()
    day_start, _ = day_bounds(day)
    vetoed = {"item_key": "CRYPTO:ETH/USD", "symbol": "ETH/USD", "kind": "NEWS",
              "status": "VETOED", "veto_reasons": ["NEWS_STALE_YES"],
              "levels": {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
                         "target": "111"},
              "why_over_peers": "rule-B pullback", "timeframe_seconds": None, "price": "104"}
    cycle_id = v3_cycle(engine.store, raw, run_slot=day_start + timedelta(minutes=1),
                        extra_picks=[vetoed], rejected=["DOSSIER_OVER_BUDGET",
                                                        "INVALID_SOURCE_EVIDENCE"])
    shadow(engine.store, cycle_id, raw["item_key"], net_r="1.2")
    shadow(engine.store, cycle_id, "CRYPTO:ETH/USD", net_r="-1.05", outcome="STOP",
           symbol="ETH/USD", selected=False)
    with engine.store.transaction() as conn:
        engine.store.event(conn, "POST_MORTEM", {
            "schema_version": "POST_MORTEM_V1", "note_id": str(uuid4()),
            "agent": agent_block("claude"),
            "items": [{"subject_key": f"TRADE:{sid}", "cause": "MARKET_WIDE",
                       "knowable_before_move": False}]})
        engine.store.event(conn, REPLAY_EVENT, {
            "method": "UNCHANGED_PLAN_REPLAY_V1", "setup_id": str(sid), "symbol": "BTC/USD",
            "change_kind": "STOP_RAISE", "change_at": venue.now.isoformat(),
            "unchanged_net_r": "0.5", "data_complete": True})
        engine.store.event(conn, DAY_REPLAY_EVENT, {
            "method": "DAY_REVIEW_DECISION_REPLAY_V1", "setup_id": str(sid),
            "decision": "CONTINUE", "at": venue.now.isoformat(), "alternative_net_r": "0.25",
            "data_complete": True})
    return engine.store, str(sid), day


def test_the_scorecard_records_the_day_once_with_counts_beside_every_rate(mx):  # noqa: F811
    store, sid, day = scored_day(mx)
    _, day_end = day_bounds(day)
    with pytest.raises(ScorecardUnavailable, match="SCORECARD_DAY_NOT_OVER"):
        compute_scorecard(store.repo, day, now=day_end - timedelta(seconds=1))
    now = day_end + timedelta(hours=1, minutes=30)
    status, seq = record_scorecard(store, day, now=now)
    assert status == "RECORDED"
    assert record_scorecard(store, day, now=now) == ("ALREADY_RECORDED", seq)
    with store.repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.managed_events WHERE event_seq=%s",
                           (seq,)).fetchone()
    assert row["kind"] == SCORECARD_EVENT and row["setup_id"] is None
    assert row["idempotency_key"] == f"daily-scorecard:{day.isoformat()}"
    body = row["body"]
    assert body["scorecard_version"] == "DAILY_SCORECARD_V1" and body["day"] == day.isoformat()
    assert set(body["windows"]) == {"1d", "7d", "30d"}
    one = body["windows"]["1d"]
    overall = one["overall"]
    funnel = overall["funnel"]
    assert (funnel["cycles"], funnel["picks_sent"], funnel["accepted"],
            funnel["rejected_at_intake"]) == (1, 4, 2, 2)
    assert funnel["rejection_codes"] == {"DOSSIER_OVER_BUDGET": 1, "INVALID_SOURCE_EVIDENCE": 1}
    assert (funnel["ranked"], funnel["vetoed"], funnel["selected"], funnel["admitted"]) == (
        1, 1, 1, 1)
    assert funnel["veto_reasons"] == {"NEWS_STALE_YES": 1}
    assert (funnel["triggered"], funnel["filled"], funnel["closed"], funnel["open"]) == (
        1, 1, 1, 0)
    results = overall["results"]
    assert (results["trades_closed"], results["wins"], results["losses"]) == (1, 1, 0)
    assert results["win_rate"] == "1.0000" and results["r_net_count"] == 1
    assert D(results["mean_r_net"]) > D("1.5") and results["fees_unverified"] == 0
    assert results["exit_reasons"] == {"TARGET_EXIT": 1}
    selection = overall["selection"]
    assert selection["selected"]["mean_shadow_r_net"] == "1.2000"
    assert selection["vetoed"]["mean_shadow_r_net"] == "-1.0500"
    assert selection["passed"]["picks"] == 0
    assert selection["selected_minus_passed_mean_shadow_r_net"] is None  # Nothing passed on.
    assert set(overall["fill_rate_by_distance"]) == {"0-1%", "3%+"}  # JSONB orders keys.
    assert overall["fill_rate_by_distance"]["0-1%"]["fill_rate"] == "1.0000"
    assert overall["results_by_timeframe"]["4h"]["picks"] == 1
    assert overall["results_by_timeframe"]["NONE"]["shadow_hit_rate"] == "0.0000"
    assert set(overall["results_by_rule"]) == {"A", "B"}
    assert overall["results_by_kind"]["NEWS"]["mean_shadow_r_net"] == "-1.0500"
    assert overall["results_by_sector"]["LARGE_CAP_L1"]["picks"] == 2
    assert overall["dossier_size_rejections"] == {"picks_sent": 4, "rejected": 1,
                                                  "rate": "0.2500"}
    assert overall["excerpt_drop_rate"]["dropped"] == 1
    assert overall["stale_news_vetoes"] == {"news_picks_ranked": 1, "vetoed": 1,
                                            "rate": "1.0000"}
    causes = overall["causes"]
    assert (causes["notable_trades"], causes["with_post_mortem"]) == (1, 1)
    assert causes["by_cause"] == {"MARKET_WIDE": 1}
    assert causes["knowable_before_move"] == {"true": 0, "false": 1, "null": 0}
    assert causes["items"][0]["notable_reasons"] == ["WIN_OVER_1_5R"]
    maintenance = overall["maintenance"]
    actual = D(results["mean_r_net"])
    assert maintenance["STOP_RAISE"]["changes"] == 1
    assert D(maintenance["STOP_RAISE"]["mean_r_difference"]) == actual - D("0.5")
    assert maintenance["DAY_REVIEW_CONTINUE"]["helped"] == 1
    assert maintenance["TARGET_RAISE"] == {"method": "UNCHANGED_PLAN_REPLAY_V1", "changes": 0,
                                           "r_difference_count": 0, "mean_r_difference": None,
                                           "helped": 0, "hurt": 0}
    assert len(maintenance["items"]) == 2
    arms = {k: v for k, v in overall["arms"].items() if isinstance(v, dict)}
    assert sum(arm["closed"] for arm in arms.values()) == 1
    assert overall["arms"]["managed_minus_control_mean_r_net"] is None  # One arm only.
    assert overall["costs"]["fees"]["verified_trades"] == 1
    assert overall["costs"]["jev"]["estimate_source"] == "JEV_SPEND_METER_V1_DEFAULTS"
    [trade] = overall["trades"]
    assert trade["setup_id"] == sid and trade["arm"] in {"JEV_MANAGED", "FIXED_EXIT", None}
    assert trade["post_mortem"]["cause"] == "MARKET_WIDE"
    claude = one["agents"]["claude"]
    assert claude["funnel"] == funnel and claude["results"] == results
    assert "maintenance" not in claude and "arms" not in claude and "costs" not in claude
    assert "trades" not in claude and "items" not in claude["causes"]
    assert body["windows"]["30d"]["overall"]["results"]["trades_closed"] == 1
    assert "items" not in body["windows"]["7d"]["overall"]["maintenance"]


def test_the_nightly_replays_measure_every_change_in_the_trades_own_r(mx):  # noqa: F811
    """Three raises, the last made after the stop had passed the max entry (100.10): each is
    recorded, walked on the levels in force before it and measured against the admitted stop
    (95). Before 2026-09-29 the later ones used the raised stop as the risk, and the third
    refused NONPOSITIVE_RISK_DENOMINATOR, so the trade's replays were never recorded."""
    engine, venue, _ = mx
    sid, _ = close_attributed(mx, "BTC/USD")
    change_at = venue.now
    steps = [("95", "99"), ("99", "100.50"), ("100.50", "101")]
    with engine.store.transaction() as conn:
        for minute, (before, after) in enumerate(steps):
            engine.store.event(conn, "MAINTENANCE_DECISION", {
                "outcome": "APPLIED", "action": "RAISE_STOP",
                "decided_at": (change_at + timedelta(minutes=minute)).isoformat(),
                "levels_before": {"stop": before, "target": "111"},
                "levels_after": {"stop": after, "target": "111"}}, setup_id=sid)
    bars = FakeBars(minutes={"BTC/USD": [
        {"t": (change_at + timedelta(minutes=5)).isoformat(), "o": "104", "h": "112",
         "l": "103", "c": "111", "v": "1"}]})
    summary = record_replays(engine.store, bars, now=change_at + timedelta(hours=1))
    assert (summary["recorded"], summary["failed"], summary["invalid"]) == (3, 0, 0)
    with engine.repo.connect() as conn:
        recorded = [r["body"] for r in conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
            (REPLAY_EVENT,)).fetchall()]
    assert [r["original_stop"] for r in recorded] == ["95", "99", "100.50"]
    one_r = D("111") - D("100.10")
    for record in recorded:
        assert record["initial_stop"] == "95" and record["exit_reason"] == "TARGET"
        assert D(record["unchanged_gross_r"]) == one_r / (D("100.10") - D("95"))


def test_the_nightly_replays_record_only_complete_counterfactuals(mx):  # noqa: F811
    engine, venue, _ = mx
    sid, _ = close_attributed(mx, "BTC/USD")
    change_at = venue.now
    with engine.store.transaction() as conn:
        engine.store.event(conn, "MAINTENANCE_DECISION", {
            "outcome": "APPLIED", "action": "RAISE_STOP", "decided_at": change_at.isoformat(),
            "levels_before": {"stop": "95", "target": "111"},
            "levels_after": {"stop": "99", "target": "111"}}, setup_id=sid)
        engine.store.event(conn, "DAY_REVIEW_DECISION", {
            "outcome": "EXIT", "code": "AGREED", "measurement": {
                "change_kind": "CONTINUE_EXIT_DECISION", "at": change_at.isoformat(),
                "old_stop": "95", "old_target": "111", "new_stop": None, "new_target": None,
                "actually_exited": True}}, setup_id=sid)
    bars = FakeBars(minutes={"BTC/USD": [
        {"t": (change_at + timedelta(minutes=2)).isoformat(), "o": "104", "h": "112",
         "l": "103", "c": "111", "v": "1"}]})
    # An hour later the raise's counterfactual is already final (the old target traded two
    # minutes after it); the exit's 24 more hours have not passed, so that one waits.
    early = record_replays(engine.store, bars, now=change_at + timedelta(hours=1))
    assert (early["recorded"], early["pending"]) == (1, 1)
    later = change_at + timedelta(hours=25)
    summary = record_replays(engine.store, bars, now=later)
    assert (summary["recorded"], summary["already_recorded"], summary["failed"]) == (1, 1, 0)
    assert record_replays(engine.store, bars, now=later)["already_recorded"] == 2
    with engine.repo.connect() as conn:
        recorded = conn.execute(
            """SELECT kind, body, setup_id FROM lab.managed_events WHERE kind IN (%s, %s)
            ORDER BY event_seq""", (REPLAY_EVENT, DAY_REPLAY_EVENT)).fetchall()
    assert [r["kind"] for r in recorded] == [REPLAY_EVENT, DAY_REPLAY_EVENT]
    assert all(r["setup_id"] is None and r["body"]["setup_id"] == str(sid) for r in recorded)
    level, day = recorded[0]["body"], recorded[1]["body"]
    assert level["change_kind"] == "STOP_RAISE" and level["exit_reason"] == "TARGET"
    assert "actual_r" not in level and "r_difference" not in level  # Read live, never frozen.
    assert day["decision"] == "EXIT" and day["exit_reason"] == "TARGET"
    assert day["alternative"] == "CONTINUE_24H_ON_LEVELS_IN_FORCE"
    failing = FakeBars(fail={"BTC/USD"})
    assert record_replays(engine.store, failing, now=later)["failed"] == 1
