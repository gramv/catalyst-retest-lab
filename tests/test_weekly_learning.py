"""LEARNING_LOOP_WEEKLY_V1 (package learning-loop2): the weekly speed -- patterns, strategy fit
by regime with minimum samples, shadow vs live, missed-tradeable totals and PROPOSE-only items
that are never applied.

Fixture evidence only: pure records and the fixture paper venue on per-test disposable
PostgreSQL databases. No broker, Jev, network or owner ledger.
"""

from datetime import timedelta
from decimal import Decimal as D

from catalyst_lab import strategies
from catalyst_lab import weekly_learning as wl
from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_summary import review_summary
from catalyst_lab.market import NY
from catalyst_lab.stats_exclusion import record_exclusion
from catalyst_lab.weekly_review import compute_review, last_completed_week_end
from tests.learning_fixtures import close_attributed
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401

UP = "UP/HIGH/BROAD/NO_SELLOFF"
OFF = "UP/HIGH/BROAD/SELLOFF"


def trade(r_net, tag, strategy="PULLBACK_V1"):
    return {"r_net": D(r_net), "win": D(r_net) > 0, "regime": {"day_tag": tag},
            "strategy_id": strategy, "versions": {}}


def shadow(net, tag, strategy="BREAKOUT_7D_VOL2X_V1", slip=None):
    result = {"outcome": "TARGET" if D(net) > 0 else "STOP", "net_r": net, "gross_r": net}
    if slip is not None:
        result["net_r_after_slippage"] = slip
    return (strategy, {"day_tag": tag}, result)


# --- Strategy fit by regime: cells and minimums ---------------------------------------------------


def test_strategy_regime_cells_report_counts_and_need_their_minimum():
    trades = [trade("0.4", UP)] * 30 + [trade("-0.5", OFF)] * 29
    sims = [shadow("0.3", UP)] * 5 + [shadow("-1", OFF)]
    fit = wl.strategy_fit(trades, sims)
    pull = fit["PULLBACK_V1"]
    up = pull["live_paper"]["day_tag"][UP]
    assert (up["closed"], up["r_net_count"], up["mean_r_net"], up["status"]) == (
        30, 30, D("0.4000"), "MEASURED")
    off = pull["live_paper"]["selloff"]["SELLOFF"]
    assert off["r_net_count"] == 29 and off["status"] == "NOT_ENOUGH_DATA"
    assert pull["live_paper"]["btc_trend"]["UP"]["closed"] == 59
    assert pull["shadow"]["day_tag"] == {}  # No shadow outcome for the pullback strategy.
    breakout = fit["BREAKOUT_7D_VOL2X_V1"]
    assert breakout["live_paper_all"]["closed"] == 0
    cell = breakout["shadow"]["day_tag"][UP]
    assert (cell["trades"], cell["mean_net_r"], cell["win_rate"], cell["status"]) == (
        5, D("0.3000"), D("1.0000"), "NOT_ENOUGH_DATA")
    assert breakout["shadow"]["selloff"]["SELLOFF"]["mean_net_r"] == D("-1.0000")
    pair = wl.shadow_vs_live(fit)["BREAKOUT_7D_VOL2X_V1"]
    assert pair["shadow"]["trades"] == 6 and pair["live_paper"]["closed"] == 0
    untagged = wl.strategy_fit([{**trade("1", UP), "regime": None}], [])
    assert set(untagged["PULLBACK_V1"]["live_paper"]["day_tag"]) == {"UNTAGGED"}


def test_shadow_items_take_the_regime_of_the_signal_day_before_the_weeks_end():
    from datetime import UTC, datetime

    until = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
    outcomes = [{"strategy_id": "S_V1", "signal_at": "2026-10-01T15:00:00+00:00",
                 "result": {"outcome": "TARGET", "net_r": "1"}},
                {"strategy_id": "S_V1", "signal_at": "2026-10-05T15:00:00+00:00",
                 "result": {}}]
    items = wl.shadow_items(outcomes, {"2026-10-01": UP}, until)
    assert items == [("S_V1", {"day_tag": UP}, {"outcome": "TARGET", "net_r": "1"})]
    assert wl.shadow_items(outcomes[:1], {}, until)[0][1] is None


def test_missed_tradeable_totals_by_regime():
    def record(tag, missed, entries, total):
        return {"day_tag": tag, "regime_parts": {"btc_trend": tag.split("/")[0]},
                "totals": {"movers": 5, "with_signal": 2, "simulated_entries": entries,
                           "positive_entries": missed, "sum_net_r": total,
                           "missed_tradeable": missed}}
    result = wl.missed_by_regime([record(UP, 6, 6, "3.0"), record(OFF, 4, 4, "-0.4"),
                                  record(UP, 0, 0, None)])
    assert result["all"]["missed_tradeable"] == 10 and result["all"]["status"] == "MEASURED"
    assert result["all"]["mean_net_r"] == D("0.2600")
    up = result["by_day_tag"][UP]
    assert (up["days"], up["missed_tradeable"], up["status"]) == (2, 6, "NOT_ENOUGH_DATA")
    assert result["by_part"]["btc_trend"]["UP"]["days"] == 3


def test_patterns_reach_their_minimum_at_ten_occurrences():
    paths = [{"setup_id": str(i), "exit_kind": "STOP", "horizons": {
        "+24h": {"status": "MEASURED", "high_r": "1.2" if i < 10 else "0.2"}}}
        for i in range(12)]
    notes = [{"subject_key": f"MOVER:2026-10-0{i % 3 + 1}:C{i}/USD", "cause": "COIN_NEWS",
              "knowable_before_move": i < 4} for i in range(10)]
    briefs = [{"movers": {"sector_clusters": [{"sector": "L1", "direction": "UP"}]}}] * 3
    regimes = [{"tag": UP}] * 11
    found = wl.patterns(paths=paths, notes=notes, briefs=briefs, missed=[], regimes=regimes,
                        excluded=frozenset({"11"}))
    reached = {p["pattern"]: p for p in found["reached"]}
    assert reached["AFTER_EXIT_RECOVERY_STOP_1R_24H"]["occurrences"] == 10
    assert reached["AFTER_EXIT_RECOVERY_STOP_1R_24H"]["base"] == 11  # One excluded.
    assert reached["MOVER_CAUSE:COIN_NEWS"]["share"] == D("1.0000")
    assert reached[f"REGIME_DAY:{UP}"]["occurrences"] == 11
    below = {p["pattern"]: p for p in found["below_minimum"]}
    assert below["MOVER_KNOWABLE_BEFORE_MOVE"]["occurrences"] == 4
    assert below["SECTOR_CLUSTER:L1:UP"]["status"] == "BELOW_MINIMUM"


# --- Proposals: named evidence, never applied -----------------------------------------------------


def test_proposal_rules_name_their_evidence_and_are_never_applied():
    trades = [trade("0.3", UP)] * 30 + [trade("-0.4", OFF)] * 30
    sims = [shadow("0.2", UP, slip="0.1")] * 30
    fit = wl.strategy_fit(trades, sims)
    management = {"by_stop_distance": {"STOP_RAISE": {
        "0-1%": {"status": "MEASURED", "mean_r_difference": D("-0.3"), "changes": 31},
        "3%+": {"status": "NOT_ENOUGH_DATA", "mean_r_difference": D("-2"), "changes": 2}}}}
    after = {"by_exit_kind": {"STOP": {"status": "MEASURED", "high_at_least_1r_share": "0.6",
                                       "high_at_least_1r": 18, "measured_24h": 30}}}
    missed = {"all": {"missed_tradeable": 12, "mean_net_r": D("0.25")}}
    items = wl.proposals(fit, management, after, missed)
    ids = [i["proposal_id"] for i in items]
    assert "STRATEGY_REGIME_GATE:PULLBACK_V1:selloff:SELLOFF" in ids
    assert "SHADOW_PROMOTION_REVIEW:BREAKOUT_7D_VOL2X_V1" in ids
    assert "MANAGEMENT_BUCKET:STOP_RAISE:by_stop_distance:0-1%" in ids
    assert "MANAGEMENT_BUCKET:STOP_RAISE:by_stop_distance:3%+" not in ids  # Below minimum.
    assert "STOP_RULE_REVIEW" in ids and "MISSED_TRADEABLE_REVIEW" in ids
    for item in items:
        assert item["status"] == "PROPOSED_NOT_APPLIED"
        assert item["requires"] == ["HISTORY_TEST", "NAMED_VERSION_IN_CONTRACT_RESOLUTIONS",
                                    "OWNER_APPROVAL"]
        assert "owner's yes" in item["text"] and item["evidence"]
    promotion = next(i for i in items if i["rule"] == "SHADOW_PROMOTION_REVIEW")
    assert "0.1000" in promotion["text"]  # After slippage, with 30 such trades.
    # Nothing was applied: the registry is unchanged.
    assert strategies.get("BREAKOUT_7D_VOL2X_V1").stage == strategies.SHADOW
    # Without measured evidence there is no proposal.
    thin = wl.strategy_fit([trade("-1", OFF)] * 5, [shadow("1", UP)] * 5)
    assert wl.proposals(thin, {}, {}, {"all": {"missed_tradeable": 3}}) == []


def test_a_regime_gate_needs_the_other_values_to_hold_up():
    losing_everywhere = [trade("-0.3", UP)] * 30 + [trade("-0.4", OFF)] * 30
    assert wl.proposals(wl.strategy_fit(losing_everywhere, []), {}, {}, {}) == []


# --- The weekly review on a ledger ----------------------------------------------------------------


def test_the_weekly_review_carries_the_learning_section_without_excluded_trades(mx):  # noqa: F811
    engine, venue, _ = mx
    sid, _ = close_attributed(mx, "BTC/USD")
    day = venue.now.astimezone(NY).date()
    _, day_end = day_bounds(day)
    assert record_exclusion(engine.store, day, "OUTLIER_DAY",
                            now=day_end + timedelta(hours=1))["status"] == "RECORDED"
    week_end = last_completed_week_end(venue.now + timedelta(days=7))
    _, until = day_bounds(week_end)
    body = compute_review(engine.store.repo, week_end, now=until + timedelta(hours=1))
    learning = body["learning"]
    assert learning["learning_version"] == "LEARNING_LOOP_WEEKLY_V1"
    assert learning["speed"] == "WEEKLY_DECIDE_PROPOSE_ONLY"
    assert learning["stats_exclusions"]["excluded_trades"] == 1
    fit = learning["strategy_fit_by_regime"]["strategies"]
    assert fit == {} or fit["PULLBACK_V1"]["live_paper_all"]["closed"] == 0
    assert learning["proposals"] == []
    assert learning["missed_tradeable_by_regime"]["all"]["days"] == 0
    management = next(t for t in body["tests_excluding_exclusions"]
                      if t["test_id"] == "MANAGEMENT_VALUE")
    assert management["verdict"] == "NOT_ENOUGH_DATA"
    assert [t["test_id"] for t in body["tests"]] == [
        t["test_id"] for t in body["tests_excluding_exclusions"]]
    assert str(sid) not in str(learning["after_exit"])
    text, data = review_summary(body, recorded=False)
    assert "Learning (LEARNING_LOOP_WEEKLY_V1, propose only)" in text
    assert "No proposal: no measured cell meets a proposal rule." in text
    assert data["learning"]["speed"] == "WEEKLY_DECIDE_PROPOSE_ONLY"
