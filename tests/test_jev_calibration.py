"""JEV_CALIBRATION_V1 (package jev-b3): each Jev probability next to its outcome.

Pure tests of the forecasts, the breakout baseline, the outcome walk, the reliability and Brier
math (hand-checked numbers), the ranking lift and the threshold notes; ledger tests of the
joins (top-K picks including vetoed, not-ranked and unselected ones; V5 and older maintenance
reviews), pending windows, idempotency and catch-up, the nightly step, the scorecard line and
the weekly review section and its text. Fixture evidence only: disposable per-test databases,
the fixture paper venue, canned public bars, synthetic ledger events and a mock Jev transport.
No broker, provider, network or owner-ledger contact; no real TypeSafe call.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from catalyst_lab import jev_calibration as jc
from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_jobs import calibration_step
from catalyst_lab.learning_summary import review_summary, scorecard_summary
from catalyst_lab.market import NY
from catalyst_lab.pick_outcomes import parse_bars
from catalyst_lab.scorecard import compute_scorecard
from catalyst_lab.weekly_review import compute_review, last_completed_week_end
from tests.learning_fixtures import FakeBars, close_attributed, minute_rows, shadow, v3_cycle
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401

T0 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)


def choice(chosen, **probabilities):
    return {"type": "choice", "choice": chosen, "confidence": 0.5,
            "probabilities": dict(probabilities)}


INSUFFICIENT = "Insufficient evidence"


# --- Forecasts ----------------------------------------------------------------------------------


def test_choice_forecasts_sum_the_favourable_labels_of_each_known_question():
    answers = {
        "already_priced": choice("MEDIUM", LOW=0.3, MEDIUM=0.45, HIGH=0.2,
                                 **{INSUFFICIENT: 0.05}),
        "levels_supported_by_bars": choice("YES", YES=0.7, NO=0.2, **{INSUFFICIENT: 0.1}),
        "verdict": choice("APPROVE", APPROVE=0.6, REJECT=0.3, NEEDS_REVIEW=0.1),
        "evidence_support": {"type": "score", "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
        "unknown_question": choice("YES", YES=1.0),
    }
    forecasts = {f["question"]: f for f in jc.choice_forecasts(answers, "BOTH_PICK_QUESTIONS_V2")}
    assert set(forecasts) == {"already_priced", "levels_supported_by_bars", "verdict"}
    assert D(forecasts["already_priced"]["p"]) == D("0.75")  # LOW + MEDIUM pass.
    assert forecasts["already_priced"]["favourable"] == ["LOW", "MEDIUM"]
    assert D(forecasts["levels_supported_by_bars"]["p"]) == D("0.7")
    assert D(forecasts["verdict"]["p"]) == D("0.6")
    assert all(f["event"] == jc.SELECTION_EVENT and f["version"] == "BOTH_PICK_QUESTIONS_V2"
               for f in forecasts.values())
    quality = jc.choice_forecasts(
        {"quality_category": choice("STRONG", STRONG=0.55, ADEQUATE=0.3, WEAK=0.15)}, "Q")
    assert D(quality[0]["p"]) == D("0.55")
    assert jc.choice_forecasts({"verdict": choice("APPROVE", APPROVE=1.5)}, "V") == []
    assert jc.choice_forecasts(None, "V") == []


def test_v5_and_older_maintenance_forecasts():
    v5 = {"policy_id": "CRYPTO_MAINTENANCE_V5",
          "answers": {"invalidation_met": {"p": "0.9", "verdict": "YES"},
                      "news_contradicts": {"p": "0.05", "verdict": "NO"}},
          "verdicts": {"invalidation_met": {"effect": "CONFIRMED", "streak": 0}}}
    forecasts = {f["question"]: f for f in jc.v5_forecasts(v5)}
    assert forecasts["invalidation_met"]["p"] == "0.9"
    assert forecasts["invalidation_met"]["effect"] == "CONFIRMED"
    assert forecasts["news_contradicts"]["effect"] is None
    assert all(f["event"] == jc.STOP_EVENT for f in forecasts.values())
    v4 = {"policy_id": "CRYPTO_MAINTENANCE_V4", "action": "HOLD",
          "answers": {"action": {"choice": "HOLD", "top_p": "0.7"}}}
    judged, source = jc.action_forecast(
        v4, choice("HOLD", HOLD=0.7, FLAG_EARLY_EXIT=0.2, RAISE_STOP=0.1))
    assert source == "RECEIPT_JUDGMENT" and D(judged[0]["p"]) == D("0.2")
    assert judged[0]["question"] == "action:FLAG_EARLY_EXIT"
    assert judged[0]["version"] == "CRYPTO_MAINTENANCE_V4"
    assert jc.action_forecast(v4, None) == ([], "UNAVAILABLE")  # HOLD: P(FLAG) unknown.
    flagged = {**v4, "answers": {"action": {"choice": "FLAG_EARLY_EXIT", "top_p": "0.62"}}}
    summary, source = jc.action_forecast(flagged, None)
    assert source == "DECISION_SUMMARY_TOP_P" and summary[0]["p"] == "0.62"


# --- The breakout baseline ----------------------------------------------------------------------


def hour_bars(end, *, prior_high="100", day_close="101", day_volume="2.5", hours=192,
              prior_volume="1"):
    rows = []
    start = end - timedelta(hours=hours)
    for h in range(hours):
        at = start + timedelta(hours=h)
        in_day = at >= end - timedelta(hours=24)
        price = D(day_close) if in_day else D(prior_high)
        rows.append({"t": at.isoformat(), "o": str(price), "h": str(price), "l": str(price),
                     "c": str(price), "v": day_volume if in_day else prior_volume})
    return parse_bars(rows)


def test_the_breakout_baseline_needs_a_close_above_the_7_day_high_on_double_volume():
    at = T0 + timedelta(minutes=37)  # Floored to 14:00: the bars up to 14:00 are complete.
    flagged = jc.breakout_flag(hour_bars(T0), at)
    assert flagged["status"] == "COMPUTED" and flagged["flag"] is True
    assert flagged["volume_ratio"] == "2.5000"  # 24 x 2.5 / (168 x 1 / 7).
    assert (flagged["day_bars"], flagged["prior_bars"]) == (24, 168)
    assert jc.breakout_flag(hour_bars(T0, day_volume="1.9"), at)["flag"] is False
    assert jc.breakout_flag(hour_bars(T0, day_close="100"), at)["flag"] is False  # Not above.
    assert jc.breakout_flag(hour_bars(T0, day_volume="2"), at)["flag"] is True  # 2x counts.
    short = jc.breakout_flag(hour_bars(T0, hours=100), at)
    assert (short["status"], short["flag"]) == ("INSUFFICIENT_BARS", None)
    # A bar still forming at the pick's time is never read.
    forming = parse_bars([{"t": T0.isoformat(), "o": "90", "h": "90", "l": "90", "c": "90",
                           "v": "1000"}])
    assert jc.breakout_flag(hour_bars(T0) + forming, at) == flagged


# --- The maintenance outcome walk --------------------------------------------------------------


def path_bars(start, closes):
    return jc._Bars(parse_bars(minute_rows(start, closes)))


def test_the_outcome_walk_r_path_event_first_touch_and_exit_against_hold():
    entry, risk = D("100.10"), D("5.10")
    # Flat at entry for 90 minutes, then 94 (below the plan stop 95) for the rest of the day.
    bars = path_bars(T0, ["100.10"] * 90 + ["94"] * 1500)
    outcome = jc.maintenance_outcome(bars, T0, entry=entry, risk=risk, plan_stop=D("95"),
                                     plan_target=D("111"), confirmed={"question": "q"})
    assert outcome["event"] is True and outcome["event_exit_reason"] == "STOP"
    assert outcome["first_touch_plan"]["reason"] == "STOP" and outcome["data_complete"]
    four = outcome["r_path"]["4h"]
    assert four["complete"] and four["bars"] == 240
    assert four["max_r"] == D("0.0000") and four["min_r"] == D("-1.1961")  # (94-100.1)/5.1.
    confirmed = outcome["confirmed"]
    assert confirmed["exit_at_decision_r"] == D("0.0000")
    assert confirmed["hold_to_plan_r"] == D("-1.0000")  # The plan stop at 95.
    assert confirmed["exit_minus_hold_r"] == D("1.0000")
    # Up to +1R (105.20) first: the event is false; holding reaches the target later.
    rising = path_bars(T0, ["100.10"] * 10 + ["105.30"] * 30 + ["111.5"] * 1500)
    up = jc.maintenance_outcome(rising, T0, entry=entry, risk=risk, plan_stop=D("95"),
                                plan_target=D("111"), confirmed=None)
    assert up["event"] is False and up["first_touch_plan"]["reason"] == "TARGET"
    assert "confirmed" not in up
    # Neither within 24 hours: false, and the plan walk ends at the 24-hour mark.
    flat = jc.maintenance_outcome(path_bars(T0, ["100.10"] * 1500), T0, entry=entry, risk=risk,
                                  plan_stop=D("95"), plan_target=D("111"), confirmed=None)
    assert flat["event"] is False and flat["first_touch_plan"]["reason"] == "HOLD_24H_EXIT"
    # Bars ending before the horizon: unknown, never guessed.
    short = jc.maintenance_outcome(path_bars(T0, ["100.10"] * 300), T0, entry=entry, risk=risk,
                                   plan_stop=D("95"), plan_target=D("111"), confirmed={})
    assert short["event"] is None and not short["data_complete"]
    assert short["r_path"]["24h"]["complete"] is False and short["r_path"]["4h"]["complete"]
    assert short["confirmed"]["exit_minus_hold_r"] is None
    # A bar reaching both the stop and +1R counts as the stop (counted as ambiguous).
    both = parse_bars([{"t": T0.isoformat(), "o": "100.1", "h": "106", "l": "94", "c": "100",
                        "v": "1"}] + minute_rows(T0 + timedelta(minutes=1), ["100"] * 1500))
    ambiguous = jc.maintenance_outcome(jc._Bars(both), T0, entry=entry, risk=risk,
                                       plan_stop=D("95"), plan_target=D("111"), confirmed=None)
    assert ambiguous["event"] is True and ambiguous["same_bar_ambiguous"] is True


# --- Reliability and Brier (hand-checked) -------------------------------------------------------


def test_reliability_bins_and_brier_scores_match_hand_computation():
    pairs = [(D("0.9"), True), (D("0.8"), True), (D("0.7"), False), (D("0.2"), False),
             (D("0.1"), True)]
    table, base, brier, base_brier, skill = jc.reliability(pairs)
    # Base rate 3/5; Brier (0.01 + 0.04 + 0.49 + 0.04 + 0.81) / 5 = 0.278; base-rate Brier
    # 0.6 x 0.4 = 0.24; skill 1 - 0.278 / 0.24 = -0.158333...
    assert base == D("0.6") and brier == D("0.278") and base_brier == D("0.24")
    assert jc._q(skill) == D("-0.1583")
    assert [row["count"] for row in table] == [1, 1, 0, 1, 2]
    assert (table[0]["mean_p"], table[0]["observed"]) == (D("0.1000"), D("1.0000"))
    assert (table[1]["mean_p"], table[1]["observed"]) == (D("0.2000"), D("0.0000"))
    assert table[2]["mean_p"] is None and table[2]["observed"] is None
    assert (table[4]["mean_p"], table[4]["observed"]) == (D("0.8500"), D("1.0000"))
    assert [row["bin"] for row in table] == ["0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1"]
    # p = 1 falls in the last bin; a perfect forecaster scores 0; no outcomes, no scores.
    table, *_ = jc.reliability([(D("1"), True)])
    assert table[4]["count"] == 1
    assert jc.reliability([(D("1"), True), (D("0"), False)])[2] == 0
    assert jc.reliability([])[1:] == (None, None, None, None)
    # Every outcome the same: the base-rate Brier is 0 and the skill undefined.
    assert jc.reliability([(D("0.5"), True)] * 3)[4] is None


def record(source, forecasts, **outcome):
    return {"source": source, "forecasts": forecasts, "outcome": outcome,
            "source_key": str(uuid4())}


def test_question_tables_group_by_question_and_version_with_minimums():
    selection = [record(jc.SELECTION, [{"question": "verdict", "version": "CHART_V1",
                                        "p": "0.6"}], data_complete=True, triggered=True,
                        event=i % 2 == 0) for i in range(40)]
    selection.append(record(jc.SELECTION, [{"question": "verdict", "version": "CHART_V1",
                                            "p": "0.6"}], data_complete=True, triggered=False,
                            event=None))
    maintenance = [record(jc.MAINTENANCE, [{"question": "invalidation_met",
                                            "version": "CRYPTO_MAINTENANCE_V5", "p": "0.85",
                                            "event_value": True}]) for _ in range(29)]
    maintenance.append(record(jc.MAINTENANCE, [{"question": "invalidation_met",
                                                "version": "CRYPTO_MAINTENANCE_V5", "p": "0.85",
                                                "event_value": None}]))
    tables = {(t["source"], t["question"]): t for t in jc.question_tables(selection + maintenance)}
    verdict = tables[(jc.SELECTION, "verdict")]
    assert (verdict["stated"], verdict["with_outcome"], verdict["untriggered"]) == (41, 40, 1)
    assert verdict["status"] == "MEASURED" and verdict["base_rate"] == D("0.5000")
    assert verdict["brier"] == D("0.2600")  # 0.5 x 0.16 + 0.5 x 0.36.
    assert verdict["event"] == jc.SELECTION_EVENT
    invalidation = tables[(jc.MAINTENANCE, "invalidation_met")]
    assert invalidation["status"] == "NOT_ENOUGH_DATA"  # 29 < 30.
    assert (invalidation["with_outcome"], invalidation["data_incomplete"]) == (29, 1)
    assert invalidation["brier_skill"] is None  # Every outcome true.


# --- Ranking lift --------------------------------------------------------------------------------


def pick_record(cycle, item, *, status, jev_rank, agent_rank, net_r, triggered=True,
                complete=True, flag=False, ratio="1", mech_status="COMPUTED", k=2, size=4):
    return {"source": jc.SELECTION, "source_key": f"{cycle}:{item}", "cycle_id": cycle,
            "item_key": item, "k": k, "cycle_picks": size,
            "ranking": {"status": status, "jev_rank": jev_rank, "agent_rank": agent_rank},
            "outcome": {"data_complete": complete, "triggered": triggered,
                        "net_r": None if net_r is None else str(net_r)},
            "mechanical": {"status": mech_status, "flag": flag, "volume_ratio": ratio},
            "forecasts": []}


def cycle_records(cycle="c1", **changes):
    return [
        pick_record(cycle, "A", status="RANKED", jev_rank=1, agent_rank=3, net_r="1.0",
                    flag=True, ratio="2"),
        pick_record(cycle, "B", status="RANKED", jev_rank=2, agent_rank=1, net_r="-1.0"),
        pick_record(cycle, "C", status="VETOED", jev_rank=None, agent_rank=2, net_r="0.5"),
        pick_record(cycle, "D", status="NOT_RANKED", jev_rank=None, agent_rank=4, net_r=None,
                    triggered=False, flag=True, ratio="3", **changes),
    ]


def test_ranking_lift_against_the_agent_random_and_mechanical_on_the_same_cycles():
    lift = jc.ranking_lift(cycle_records())
    # Jev's top 2 = A, B (0); the agent's = B, C (-0.25); all four (unfilled D = 0) 0.125;
    # the baseline's = D (flag, ratio 3), A (flag, ratio 2): 0.5.
    assert lift["cycles"] == 1 and lift["cycles_with_more_picks_than_k"] == 1
    assert lift["jev_top_k"] == {"picks": 2, "mean_net_r_per_pick": D("0.0000"),
                                 "triggered": 2, "mean_net_r_triggered": D("0.0000")}
    assert lift["agent_top_k"]["mean_net_r_per_pick"] == D("-0.2500")
    assert lift["random_k_expected"]["mean_net_r_per_pick"] == D("0.1250")
    assert lift["random_k_expected"]["mean_net_r_triggered"] == D("0.1667")
    assert (lift["lift_vs_agent"], lift["lift_vs_random"]) == (D("0.2500"), D("-0.1250"))
    assert lift["status"] == "NOT_ENOUGH_DATA" and lift["minimum"] == 40
    mechanical = lift["mechanical"]
    assert mechanical["cycles"] == 1
    assert mechanical["mechanical_top_k"]["mean_net_r_per_pick"] == D("0.5000")
    assert mechanical["lift_vs_mechanical"] == D("-0.5000")
    # The baseline is omitted when a pick lacks it; an incomplete cycle is left out entirely.
    omitted = jc.ranking_lift(cycle_records(mech_status="INSUFFICIENT_BARS"))
    assert omitted["mechanical"]["omitted"] is True and omitted["cycles"] == 1
    assert jc.ranking_lift(cycle_records()[:3])["cycles"] == 0  # One pick not recorded yet.
    unknown = cycle_records()
    unknown[1]["outcome"] = {"data_complete": False, "triggered": True, "net_r": None}
    assert jc.ranking_lift(unknown)["cycles"] == 0
    # K overridden (the threshold notes): K = 1 is Jev's rank 1 and the agent's first.
    one = jc.ranking_lift(cycle_records(), k=1)
    assert one["jev_top_k"]["mean_net_r_per_pick"] == D("1.0000")
    assert one["agent_top_k"]["mean_net_r_per_pick"] == D("-1.0000")


def v5_record(p, event, *, question="invalidation_met"):
    return {"source": jc.MAINTENANCE, "policy_id": "CRYPTO_MAINTENANCE_V5",
            "source_key": str(uuid4()), "outcome": {},
            "forecasts": [{"question": question, "p": p, "event_value": event}]}


def test_thresholds_suggestion_reports_the_current_rule_and_alternatives_only():
    records = ([v5_record("0.95", True)] * 3 + [v5_record("0.85", False)] * 2
               + [v5_record("0.75", True)] + [v5_record("0.15", False)] * 4
               + [v5_record("0.25", True)])
    notes = jc.thresholds_suggestion(records, jc.ranking_lift(records))
    invalidation = notes["maintenance"][0]
    rows = {row["rule"]: row for row in invalidation["rules"]}
    assert rows["YES_AT_P>=0.80"]["answers"] == 5 and rows["YES_AT_P>=0.80"]["current"]
    assert rows["YES_AT_P>=0.80"]["event_rate"] == D("0.6000")
    assert rows["YES_AT_P>=0.70"]["answers"] == 6 and rows["YES_AT_P>=0.90"]["answers"] == 3
    assert rows["NO_AT_P<=0.20"]["answers"] == 4 and rows["NO_AT_P<=0.20"]["event_rate"] == 0
    assert rows["NO_AT_P<=0.30"]["event_rate"] == D("0.2000")
    assert invalidation["status"] == "NOT_ENOUGH_DATA" and invalidation["answers"] == 11
    assert notes["maintenance"][1]["question"] == "news_contradicts"
    assert notes["current"] == {"yes": "0.80", "no": "0.20",
                                "answer_rule": "MAINTENANCE_ANSWER_RULE_V3",
                                "k": "RECORDED_PER_CYCLE"}
    assert [row["k"] for row in notes["k_alternatives"]] == [3, 5, 10]
    assert "Record-only" in notes["text"][-1]
    assert notes["text"][0].startswith("invalidation_met (CRYPTO_MAINTENANCE_V5, 11 reviews")


# --- Selection joins on the ledger ---------------------------------------------------------------


def flat_hours(end, hours=192, price="100", volume="10"):
    start = end - timedelta(hours=hours)
    return [{"t": (start + timedelta(hours=h)).isoformat(), "o": price, "h": price, "l": price,
             "c": price, "v": volume} for h in range(hours)]


def breakout_hours(end):
    rows = flat_hours(end, hours=168, volume="1")
    start = end - timedelta(hours=24)
    return [*[{**r, "t": (datetime.fromisoformat(r["t"]) - timedelta(hours=24)).isoformat()}
              for r in rows],
            *[{"t": (start + timedelta(hours=h)).isoformat(), "o": "101", "h": "101",
               "l": "101", "c": "101", "v": "3"} for h in range(24)]]


def review_events(store, cycle_id, item_key, answers, quality=None,
                  version="CHART_PICK_QUESTIONS_V1", disposition="RANKABLE"):
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_DECISION", {
            "cycle_id": cycle_id, "item_key": item_key, "revision": 1,
            "request_id": str(uuid4()), "receipt_ids": [str(uuid4())], "answers": answers,
            "disposition": disposition, "reason": None,
            "selection_policy": "JEV_TOP_K_SELECTION_V2", "question_set_version": version,
            "pick_kind": "CHART"}, key=f"research:{cycle_id}:{item_key}:1:decision")
        if quality is not None:
            store.event(conn, "RESEARCH_QUALITY", {
                "cycle_id": cycle_id, "item_key": item_key, "revision": 1,
                "request_id": str(uuid4()), "receipt_ids": [str(uuid4())],
                "quality_policy": "MUSE_JEV_COMPARATIVE_QUALITY_V3", "status": "SCORED",
                "answers": quality}, key=f"research:{cycle_id}:{item_key}:1:quality")


LEVELS = {"entry_trigger": "50", "max_entry_price": "50.05", "stop": "47", "target": "56"}


def selection_ledger(mx):  # noqa: F811
    """A top-K cycle of four picks: the traded top pick (BTC), a ranked unselected pick (ETH),
    a vetoed pick (SOL) and a not-ranked pick without a review (DOGE)."""
    engine, venue, _ = mx
    store = engine.store
    sid, raw = close_attributed(mx, "BTC/USD")
    slot = venue.now - timedelta(days=2)
    extra = [
        {"item_key": "CRYPTO:ETH/USD", "symbol": "ETH/USD", "kind": "CHART",
         "status": "RANKED", "rank": 2, "levels": LEVELS, "why_over_peers": "x", "price": "50"},
        {"item_key": "CRYPTO:SOL/USD", "symbol": "SOL/USD", "kind": "CHART",
         "status": "VETOED", "rank": None, "levels": LEVELS, "why_over_peers": "x",
         "price": "50", "veto_reasons": ["SETUP_ALREADY_BROKEN_YES"]},
        {"item_key": "CRYPTO:DOGE/USD", "symbol": "DOGE/USD", "kind": "CHART",
         "status": "NOT_RANKED", "rank": None, "levels": LEVELS, "why_over_peers": "x",
         "price": "50", "reason": "REVIEW_DEADLINE_PASSED"},
    ]
    cycle_id = v3_cycle(store, raw, run_slot=slot, extra_picks=extra)
    review_events(store, cycle_id, raw["item_key"], {
        "levels_supported_by_bars": choice("YES", YES=0.7, NO=0.2, **{INSUFFICIENT: 0.1}),
        "verdict": choice("APPROVE", APPROVE=0.6, REJECT=0.3, NEEDS_REVIEW=0.1)},
        quality={"quality_category": choice("STRONG", STRONG=0.5, ADEQUATE=0.3, WEAK=0.2)})
    review_events(store, cycle_id, "CRYPTO:ETH/USD", {
        "levels_supported_by_bars": choice("YES", YES=0.55, NO=0.35, **{INSUFFICIENT: 0.1}),
        "verdict": choice("REJECT", APPROVE=0.3, REJECT=0.6, NEEDS_REVIEW=0.1)})
    review_events(store, cycle_id, "CRYPTO:SOL/USD", {
        "setup_already_broken": choice("YES", YES=0.8, NO=0.15, **{INSUFFICIENT: 0.05}),
        "verdict": choice("REJECT", APPROVE=0.1, REJECT=0.8, NEEDS_REVIEW=0.1)},
        disposition="VETOED")
    shadow(store, cycle_id, raw["item_key"], net_r="0.8", symbol="BTC/USD")
    shadow(store, cycle_id, "CRYPTO:ETH/USD", net_r="-1.05", symbol="ETH/USD", selected=False)
    shadow(store, cycle_id, "CRYPTO:SOL/USD", net_r=None, triggered=False,
           outcome="NEVER_TRIGGERED_VALIDITY_EXPIRED", symbol="SOL/USD", selected=False)
    end = slot.replace(minute=0, second=0, microsecond=0)
    reader = FakeBars(hours={"BTC/USD": breakout_hours(end), "ETH/USD": flat_hours(end),
                             "SOL/USD": flat_hours(end), "DOGE/USD": flat_hours(end)})
    return store, sid, raw, cycle_id, reader


def calibration_rows(store, source=None):
    return jc.calibration_records(store.repo, source=source)


def test_every_top_k_pick_is_joined_to_its_shadow_outcome_once(mx):  # noqa: F811
    engine, venue, _ = mx
    store, sid, raw, cycle_id, reader = selection_ledger(mx)
    now = venue.now
    before = jc.day_counts(store.repo, now - timedelta(days=1), now + timedelta(days=1))
    assert before["selection"] == {"stated": 3, "outcomes_recorded": 0, "outcomes_pending": 3}
    summary = jc.record_calibration(store, reader, now=now)
    assert summary["selection_recorded"] == 3 and summary["selection_pending"] == 1  # DOGE.
    records = {r["item_key"]: r for r in calibration_rows(store, jc.SELECTION)}
    top = records[raw["item_key"]]
    assert top["calibration_version"] == "JEV_CALIBRATION_V1" and top["cycle_picks"] == 4
    assert top["ranking"]["rank_bucket"] == "TOP_K" and top["ranking"]["jev_rank"] == 1
    forecasts = {f["question"]: D(f["p"]) for f in top["forecasts"]}
    assert forecasts == {"levels_supported_by_bars": D("0.7"), "verdict": D("0.6"),
                         "quality_category": D("0.5")}
    assert top["outcome"]["basis"] == "PICK_SHADOW_OUTCOME_V1_NET_R"
    assert top["outcome"]["event"] is True and top["outcome"]["net_r"] == "0.8"
    traded = top["outcome"]["traded"]
    assert traded["setup_id"] == str(sid) and traded["fees_status"] == "VERIFIED"
    assert traded["official_r"] is not None
    assert top["mechanical"]["flag"] is True and top["mechanical"]["status"] == "COMPUTED"
    unselected = records["CRYPTO:ETH/USD"]
    assert unselected["ranking"]["rank_bucket"] == "NOT_SELECTED"
    assert unselected["outcome"]["event"] is False and unselected["mechanical"]["flag"] is False
    assert unselected["outcome"]["traded"] is None
    vetoed = records["CRYPTO:SOL/USD"]
    assert vetoed["ranking"]["veto_reasons"] == ["SETUP_ALREADY_BROKEN_YES"]
    assert vetoed["outcome"]["event"] is None and vetoed["outcome"]["triggered"] is False
    assert {f["question"] for f in vetoed["forecasts"]} == {"setup_already_broken", "verdict"}
    assert D(next(f["p"] for f in vetoed["forecasts"]
                  if f["question"] == "setup_already_broken")) == D("0.15")  # P(NO) passes.
    # Catch-up: the not-ranked pick's shadow arrives later; nothing is recorded twice.
    shadow(store, cycle_id, "CRYPTO:DOGE/USD", net_r="0.4", symbol="DOGE/USD", selected=False)
    again = jc.record_calibration(store, reader, now=now)
    assert (again["selection_recorded"], again["already_recorded"]) == (1, 3)
    late = next(r for r in calibration_rows(store) if r["item_key"] == "CRYPTO:DOGE/USD")
    assert late["forecasts"] == [] and late["ranking"]["status"] == "NOT_RANKED"
    assert late["ranking"]["not_ranked_reason"] == "REVIEW_DEADLINE_PASSED"
    assert jc.record_calibration(store, reader, now=now)["selection_recorded"] == 0
    assert len(calibration_rows(store)) == 4
    after = jc.day_counts(store.repo, now - timedelta(days=1), now + timedelta(days=1))
    assert after["selection"] == {"stated": 3, "outcomes_recorded": 3, "outcomes_pending": 0}
    assert after["records_to_date"] == 4
    # The weekly section: the cycle is complete, so the lift is measured on it.
    section = jc.calibration_section(store.repo, until=now + timedelta(days=1))
    assert section["records"] == {"selection": 4, "maintenance": 0}
    assert section["pending"] == {"selection": 0, "maintenance": 0}
    lift = section["ranking_lift"]
    assert lift["cycles"] == 1 and lift["jev_top_k"]["picks"] == 2  # K = 10: both ranked.
    assert lift["jev_top_k"]["mean_net_r_per_pick"] == "-0.1250"  # (0.8 - 1.05) / 2.
    assert lift["random_k_expected"]["mean_net_r_per_pick"] == "0.0375"  # Unfilled SOL is 0.
    assert lift["mechanical"]["cycles"] == 1
    verdicts = [q for q in section["questions"] if q["question"] == "verdict"]
    assert verdicts[0]["with_outcome"] == 2 and verdicts[0]["untriggered"] == 1
    assert verdicts[0]["status"] == "NOT_ENOUGH_DATA"


def test_a_bar_failure_leaves_the_pick_for_the_next_run_and_fails_the_step(mx):  # noqa: F811
    engine, venue, _ = mx
    store, _sid, _raw, _cycle, reader = selection_ledger(mx)
    reader.fail = {"ETH/USD"}
    code, details = calibration_step(store, reader, venue.now)
    assert code == "JEV_CALIBRATION_BARS_UNAVAILABLE"
    assert details["bar_fetch_failed"] == 1 and details["selection_recorded"] == 2
    reader.fail = set()
    code, details = calibration_step(store, reader, venue.now)
    assert code is None and details["selection_recorded"] == 1
    with store.transaction() as conn:
        rows = conn.execute("SELECT count(*) AS n FROM lab.managed_events WHERE kind=%s",
                            (jc.RECORD_EVENT,)).fetchone()
    assert rows["n"] == 3


# --- Maintenance joins on the ledger -------------------------------------------------------------


def decision(store, sid, at, *, policy, answers, outcome="HELD", action="HOLD", **extra):
    request_id = str(uuid4())
    body = {"policy_id": policy, "lifecycle_id": "fixture-lifecycle", "request_id": request_id,
            "context_hash": "c" * 64, "receipt_ids": extra.pop("receipt_ids", []),
            "requested_at": at.isoformat(), "answered_at": at.isoformat(),
            "decided_at": at.isoformat(), "trigger_reasons": ["BAR_15M"],
            "review_status": "RECORDED", "entry": "100.10", "risk_per_coin": "5.10",
            "qty": "1", "answers": answers, "outcome": outcome, "action": action, **extra}
    with store.transaction() as conn:
        store.event(conn, "MAINTENANCE_DECISION", body, setup_id=sid,
                    key="maintenance-decision:" + request_id)
    return request_id


def test_v5_and_older_reviews_are_joined_to_their_price_path_once(mx):  # noqa: F811
    engine, venue, _ = mx
    store = engine.store
    sid, _raw = close_attributed(mx, "BTC/USD")
    t = venue.now - timedelta(days=3)
    confirmed = decision(
        store, sid, t, policy="CRYPTO_MAINTENANCE_V5", outcome="FLAGGED",
        action="FLAG_EARLY_EXIT",
        answers={"invalidation_met": {"p": "0.9", "verdict": "YES"}},
        verdicts={"invalidation_met": {"p": "0.9", "verdict": "YES", "effect": "CONFIRMED",
                                       "streak": 0}},
        flags=[{"question": "invalidation_met", "flag_id": "flag-1", "created": True}])
    held = decision(store, sid, t + timedelta(hours=1), policy="CRYPTO_MAINTENANCE_V5",
                    answers={"invalidation_met": {"p": "0.1", "verdict": "NO"},
                             "news_contradicts": {"p": "0.5", "verdict": "UNCERTAIN"}},
                    verdicts={"invalidation_met": {"effect": "RESET", "streak": 0}})
    v4_flag = decision(store, sid, t, policy="CRYPTO_MAINTENANCE_V4", outcome="FLAGGED",
                       action="FLAG_EARLY_EXIT", flag_id="flag-2",
                       answers={"action": {"choice": "FLAG_EARLY_EXIT", "top_p": "0.62"}})
    v3_hold = decision(store, sid, t, policy="CRYPTO_MAINTENANCE_V3",
                       answers={"action": {"choice": "HOLD", "top_p": "0.71"}})
    recent = decision(store, sid, venue.now - timedelta(hours=1), policy="CRYPTO_MAINTENANCE_V5",
                      answers={"invalidation_met": {"p": "0.3", "verdict": "UNCERTAIN"}})
    decision(store, sid, t, policy="CRYPTO_MAINTENANCE_V5", outcome="FAILED", action=None,
             answers=None)  # No answer: nothing to calibrate.
    with store.transaction() as conn:
        store.event(conn, "EXIT_FLAG_RESOLVED", {"flag_id": "flag-1", "outcome": "NO_ANSWER_EXIT"},
                    setup_id=sid, key="exit-flag-resolved:flag-1")
    # Flat at entry for 90 minutes, then below the stop (95) for the rest of the window.
    reader = FakeBars(minutes={"BTC/USD": minute_rows(t, ["100.10"] * 90 + ["94"] * 1600)})
    summary = jc.record_calibration(store, reader, now=venue.now)
    assert summary["maintenance_recorded"] == 4 and summary["maintenance_pending"] == 1
    records = {r["request_id"]: r for r in calibration_rows(store, jc.MAINTENANCE)}
    assert set(records) == {confirmed, held, v4_flag, v3_hold}
    first = records[confirmed]
    assert first["policy_id"] == "CRYPTO_MAINTENANCE_V5" and first["setup_id"] == str(sid)
    assert first["levels"] == {"entry": "100.10", "risk_per_coin": "5.10", "plan_stop": "95",
                               "plan_target": "111"}
    [forecast] = first["forecasts"]
    assert (forecast["p"], forecast["effect"], forecast["event_value"]) == ("0.9", "CONFIRMED",
                                                                           True)
    assert first["outcome"]["event"] is True
    assert first["outcome"]["r_path"]["4h"]["min_r"] == "-1.1961"
    exit_line = first["outcome"]["confirmed"]
    assert exit_line["flag_id"] == "flag-1" and exit_line["flag_resolution"] == "NO_ANSWER_EXIT"
    assert exit_line["exit_minus_hold_r"] == "1.0000"
    second = records[held]
    assert {f["question"] for f in second["forecasts"]} == {"invalidation_met",
                                                           "news_contradicts"}
    assert second["outcome"]["r_path"]["4h"]["max_r"] == "0.0000"
    assert "confirmed" not in second["outcome"]
    older = records[v4_flag]
    assert older["probability_source"] == "DECISION_SUMMARY_TOP_P"
    assert older["forecasts"][0]["question"] == "action:FLAG_EARLY_EXIT"
    assert older["outcome"]["confirmed"]["flag_id"] == "flag-2"
    assert records[v3_hold]["forecasts"] == []
    assert records[v3_hold]["probability_source"] == "UNAVAILABLE"
    # Rerun: nothing twice; the recent review completes a day later.
    again = jc.record_calibration(store, reader, now=venue.now)
    assert (again["maintenance_recorded"], again["already_recorded"]) == (0, 4)
    reader.minutes["BTC/USD"] += minute_rows(t + timedelta(minutes=1690),
                                             ["94"] * (4 * 1440))
    later = jc.record_calibration(store, reader, now=venue.now + timedelta(days=1))
    assert later["maintenance_recorded"] == 1
    assert recent in {r["request_id"] for r in calibration_rows(store, jc.MAINTENANCE)}
    # The weekly section reads them: questions by version and the confirmed exits.
    section = jc.calibration_section(store.repo, until=venue.now + timedelta(days=2))
    keys = {(q["question"], q["version"]) for q in section["questions"]}
    assert keys == {("invalidation_met", "CRYPTO_MAINTENANCE_V5"),
                    ("news_contradicts", "CRYPTO_MAINTENANCE_V5"),
                    ("action:FLAG_EARLY_EXIT", "CRYPTO_MAINTENANCE_V4")}
    exits = {(e["version"], e["question"]): e for e in section["confirmed_exits"]}
    assert exits[("CRYPTO_MAINTENANCE_V5", "invalidation_met")]["count"] == 1
    assert exits[("CRYPTO_MAINTENANCE_V4", "action:FLAG_EARLY_EXIT")]["exits_that_saved_r"] == 1
    assert section["ranking_lift"]["cycles"] == 0
    assert section["ranking_lift"]["mechanical"]["omitted"] is True


def test_reviews_of_engineering_setups_and_old_reviews_are_not_read(mx, monkeypatch):  # noqa: F811
    engine, venue, _ = mx
    store = engine.store
    sid, _ = close_attributed(mx, "BTC/USD")
    t = venue.now - timedelta(days=3)
    decision(store, sid, t, policy="CRYPTO_MAINTENANCE_V5",
             answers={"invalidation_met": {"p": "0.1", "verdict": "NO"}})
    reader = FakeBars(minutes={"BTC/USD": minute_rows(t, ["100.10"] * 1600)})
    monkeypatch.setattr(jc, "is_engineering", lambda _record: True)
    assert jc.record_calibration(store, reader, now=venue.now)["maintenance_recorded"] == 0
    monkeypatch.undo()
    # Beyond the 40-day lookback (by the ledger's own recorded time) nothing is read.
    far = venue.now + timedelta(days=41)
    assert jc.record_calibration(store, reader, now=far)["maintenance_recorded"] == 0


# --- The scorecard line and the weekly review --------------------------------------------------


def test_the_scorecard_has_a_jev_line_and_the_weekly_review_a_calibration_section(mx):  # noqa: F811
    engine, venue, _ = mx
    store, _sid, _raw, cycle_id, reader = selection_ledger(mx)
    with store.repo.connect() as conn:
        stated_at = conn.execute(
            """SELECT recorded_at FROM lab.managed_events WHERE kind='RESEARCH_DECISION'
            AND body->>'cycle_id'=%s ORDER BY event_seq LIMIT 1""", (cycle_id,),
        ).fetchone()["recorded_at"]
    day = stated_at.astimezone(NY).date()
    _, day_end = day_bounds(day)
    pending = compute_scorecard(store.repo, day, now=day_end + timedelta(hours=1))
    line = pending["windows"]["1d"]["overall"]["jev"]
    assert line["selection"] == {"stated": 3, "outcomes_recorded": 0, "outcomes_pending": 3}
    assert line["maintenance"]["stated"] == 0 and line["records_to_date"] == 0
    assert "jev" not in pending["windows"]["7d"]["overall"]
    jc.record_calibration(store, reader, now=venue.now)
    body = compute_scorecard(store.repo, day, now=day_end + timedelta(hours=1))
    assert body["windows"]["1d"]["overall"]["jev"]["selection"]["outcomes_recorded"] == 3
    text, _ = scorecard_summary(body, recorded=False)
    assert ("Jev probabilities (day): selection 3 stated, 3 with outcomes, 0 pending; "
            "maintenance 0 stated, 0 with outcomes, 0 pending; 3 calibration records to "
            "date.") in text
    week_end = last_completed_week_end(day_end + timedelta(days=8))
    review = compute_review(store.repo, week_end, now=day_bounds(week_end)[1] + timedelta(
        hours=1))
    section = review["jev_calibration"]
    assert section["calibration_version"] == "JEV_CALIBRATION_V1"
    assert section["events"]["SHADOW_NET_R_24H_POSITIVE"].startswith("The pick's")
    assert section["bins"] == ["0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1"]
    text, data = review_summary(review, recorded=False)
    assert "jev_calibration" in data
    assert "Jev calibration (JEV_CALIBRATION_V1): 3 pick and 0 review records" in text
    assert ("selection verdict (CHART_PICK_QUESTIONS_V1) -> SHADOW_NET_R_24H_POSITIVE: "
            "NOT_ENOUGH_DATA, 2 with outcomes") in text
    assert "Ranking lift (NOT_ENOUGH_DATA, 0 complete cycles)" in text  # DOGE still pending.
    assert "mechanical baseline omitted" in text
    assert "Record-only: nothing here changes a threshold or K" in text
    json.dumps(review)  # The recorded body stays plain JSON.


@pytest.mark.parametrize("p", ["-0.1", "1.01", "nan"])
def test_out_of_range_probabilities_are_never_forecasts(p):
    assert jc.v5_forecasts({"policy_id": "CRYPTO_MAINTENANCE_V5",
                            "answers": {"invalidation_met": {"p": p}}}) == []
