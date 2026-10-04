"""Package learning-measure (2026-10-02): net R over fee-verified trades only, results by
MARKET_REGIME_V1 and by recorded rule version, late fee settlements, and the weekly review's
reading of recorded policy records.

Fixture evidence only: the fixture paper venue on per-test disposable PostgreSQL databases. No
broker, Jev, network or owner ledger.
"""

from datetime import timedelta
from decimal import Decimal as D

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_summary import review_summary, scorecard_summary
from catalyst_lab.managed_analytics import import_fill_cost_correction
from catalyst_lab.market import NY
from catalyst_lab.market_regime import REGIME_EVENT, TRADE_REGIME_EVENT, regime_key
from catalyst_lab.result_dimensions import (
    a_version_key,
    a_versions,
    cell,
    dimensions,
    recorded_versions,
    regime_value,
    unverified_reason,
)
from catalyst_lab.scorecard import compute_scorecard, record_scorecard
from catalyst_lab.weekly_review import compute_review, last_completed_week_end
from tests.learning_fixtures import close_attributed
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_analytics import correction, fills
from tests.test_managed_execution import mx as mx  # noqa: F401

MAINTENANCE_RECORD = {"policy_id": "CRYPTO_MAINTENANCE_V3", "review_bar_seconds": 60}
TAG = "DOWN/HIGH/NARROW/SELLOFF"


# --- Pure: versions, cells, dimensions -----------------------------------------------------------


def test_recorded_versions_read_strings_and_policy_records_only():
    state = {"risk_policy_id": "JEV_MANAGED_RISK_V3", "maintenance_policy": MAINTENANCE_RECORD,
             "holding_policy": {"policy_id": "CRYPTO_WINDOW_REVIEW_V1", "window": 3},
             "stop_breach_version": "CRYPTO_STOP_BREACH_V3", "arm": "JEV_MANAGED",
             "crypto_day_policy": {"flat": "x"}, "trigger_version": "not a version",
             "entry_pacing_version": "CRYPTO_ENTRY_PACING_V1"}
    record = {"risk_policy_id": "OLD_V1"}
    versions = recorded_versions(record, state)
    assert versions == {
        "entry_pacing_version": "CRYPTO_ENTRY_PACING_V1",
        "holding_policy": "CRYPTO_WINDOW_REVIEW_V1",
        "maintenance_policy": "CRYPTO_MAINTENANCE_V3", "risk_policy_id": "JEV_MANAGED_RISK_V3",
        "stop_breach_version": "CRYPTO_STOP_BREACH_V3"}
    assert a_versions(versions) == {
        "risk_policy": "JEV_MANAGED_RISK_V3", "trade_plan": "NONE",
        "maintenance": "CRYPTO_MAINTENANCE_V3", "entry_pacing": "CRYPTO_ENTRY_PACING_V1",
        "stop_limit": "NONE"}
    old = a_version_key({"risk_policy_id": "JEV_MANAGED_RISK_V3"})
    assert old == ("risk_policy=JEV_MANAGED_RISK_V3|trade_plan=NONE|maintenance=NONE|"
                   "entry_pacing=NONE|stop_limit=NONE")


def test_phase_a_versions_are_read_from_the_fields_the_packages_record():
    # The exact admission records of the phase-A packages (not hand-written field names).
    from catalyst_lab import crypto_maintenance, regime_gate, stop_breach, trade_plan
    state = {"risk_policy_id": "JEV_MANAGED_RISK_V4",
             "maintenance_policy": crypto_maintenance.ADMITTED_MAINTENANCE.record(),
             "stop_breach_version": stop_breach.STOP_BREACH_VERSION,
             stop_breach.STOP_LIMIT_FIELD: stop_breach.CRYPTO_STOP_BREACH_V4.record(),
             regime_gate.STATE_FIELD: regime_gate.VERSION,
             trade_plan.FIELD: {"policy": trade_plan.CRYPTO_TRADE_PLAN.record(),
                                "stop": "1.0", "target": "2.0"}}
    assert a_versions(recorded_versions({}, state)) == {
        "risk_policy": "JEV_MANAGED_RISK_V4", "trade_plan": "CRYPTO_TRADE_PLAN_V1",
        "maintenance": crypto_maintenance.ADMITTED_MAINTENANCE.policy_id,
        "entry_pacing": "CRYPTO_ENTRY_PACING_V1", "stop_limit": "CRYPTO_STOP_BREACH_V4"}


def item(r_net, *, win=None, tag=TAG, versions=None, regime=True):
    return {"r_net": None if r_net is None else D(r_net),
            "win": (D(r_net) > 0 if r_net is not None else None) if win is None else win,
            "regime": {"day_tag": tag, "btc_1h": "FLAT"} if regime else None,
            "versions": versions or {}}


def test_a_cell_reports_net_r_over_verified_trades_beside_every_closed_one():
    members = [item("1"), item("-0.5"), item(None, win=True)]
    result = cell(members)
    assert (result["closed"], result["r_net_count"], result["fees_unverified"]) == (3, 2, 1)
    assert result["mean_r_net"] == D("0.2500") and result["sum_r_net"] == D("0.5000")
    assert (result["wins"], result["win_rate"]) == (2, D("0.6667"))  # gross, every trade
    assert result["r_net_win_rate"] == D("0.5000")
    assert result["status"] == "NOT_ENOUGH_DATA"
    assert cell([item("0.1")] * 29)["status"] == "NOT_ENOUGH_DATA"
    assert cell([item("0.1")] * 30)["status"] == "MEASURED"
    assert cell([item("0.1")] * 30 + [item(None)] * 5, 30)["status"] == "MEASURED"
    assert cell([item("0.1")] * 29 + [item(None)] * 5)["status"] == "NOT_ENOUGH_DATA"
    empty = cell([item(None)])
    assert empty["mean_r_net"] is None and empty["r_net_win_rate"] is None


def test_dimensions_group_by_regime_parts_versions_and_both():
    new = {"risk_policy_id": "JEV_MANAGED_RISK_V4", "trade_plan_version": "CRYPTO_TRADE_PLAN_V1"}
    old = {"risk_policy_id": "JEV_MANAGED_RISK_V3"}
    items = [item("1", versions=new), item("-1", versions=old),
             item("0.5", versions=old, tag="UP/LOW/BROAD/NO_SELLOFF"),
             item(None, versions=old, regime=False)]
    result = dimensions(items)
    assert result["minimum"] == 30 and result["tagged"] == 3
    regime = result["by_regime"]
    assert set(regime["day_tag"]) == {TAG, "UP/LOW/BROAD/NO_SELLOFF", "UNTAGGED"}
    assert regime["btc_trend"]["DOWN"]["closed"] == 2 and regime["selloff"]["SELLOFF"][
        "mean_r_net"] == D("0")
    assert regime["btc_1h"]["FLAT"]["closed"] == 3 and regime["btc_4h"]["UNKNOWN"]["closed"] == 3
    assert regime["prior_day_tag"]["UNKNOWN"]["closed"] == 3
    fields = result["by_version_field"]
    assert fields["trade_plan_version"] == {
        "CRYPTO_TRADE_PLAN_V1": cell([items[0]]), "NONE": cell(items[1:])}
    assert set(fields["risk_policy_id"]) == {"JEV_MANAGED_RISK_V3", "JEV_MANAGED_RISK_V4"}
    by_a = result["by_a_versions_and_day_tag"]
    assert set(by_a[a_version_key(old)]) == {TAG, "UP/LOW/BROAD/NO_SELLOFF", "UNTAGGED"}
    assert list(by_a[a_version_key(new)]) == [TAG]
    assert all(c["status"] == "NOT_ENOUGH_DATA" for c in result["by_a_versions"].values())
    assert regime_value({"day_tag": "UP/LOW"}, "btc_trend") == "UNKNOWN"  # malformed tag


def test_unverified_reasons():
    def m(*sources, official=None, reconciled=True):
        return {"official_r": official, "inventory_reconciled_with_costs": reconciled,
                "cost_evidence": [{"source": s} for s in sources]}
    assert unverified_reason(m("ALPACA_PAPER_ACTIVITY", official="1"), False) is None
    assert unverified_reason(m("ALPACA_PAPER_ACTIVITY", official="1"), True) == "FIXTURE_SOURCE"
    assert unverified_reason(m("ALPACA_PAPER_ACTIVITY", "UNKNOWN"), False) == "NO_FEE_EVIDENCE"
    assert unverified_reason(m("INVALID_CORRECTION"), False) == "INVALID_COST_EVIDENCE"
    assert unverified_reason(m("FILL_EVENT", reconciled=False), False) == \
        "INVENTORY_NOT_RECONCILED"
    assert unverified_reason(m("FILL_EVENT"), False) == "NO_RISK_DENOMINATOR"


# --- The scorecard on a ledger --------------------------------------------------------------------


def tag_trade(store, sid, day, *, tag=TAG):
    with store.transaction() as conn:
        store.event(conn, REGIME_EVENT, {"regime_version": "MARKET_REGIME_V1",
                                         "day": day.isoformat(), "tag": tag},
                    key=regime_key(day))
        store.event(conn, TRADE_REGIME_EVENT, {
            "regime_version": "MARKET_REGIME_V1", "setup_id": str(sid), "day_tag": tag,
            "prior_day_tag": None, "btc_1h": "DOWN_2+", "btc_4h": "DOWN",
            "median_coin_1h": "DOWN_2+", "btc_1h_return_pct": "-2.1000",
            "btc_4h_return_pct": "-3.0000", "median_coin_1h_return_pct": "-2.4000"},
            key=f"trade-regime:{sid}")


def add_versions(store, sid, **versions):
    with store.transaction() as conn:
        store.transition(conn, sid, "CLOSED", **versions)


def verify_fees(engine, venue, sid):
    for fill in fills(engine, sid):
        import_fill_cost_correction(engine.store, correction(
            fill, venue.now, fee_usd="0.05", source="BROKER_STATEMENT"), recorded_at=venue.now)


def test_the_scorecard_reports_net_r_only_over_verified_trades_with_regime_and_versions(mx):  # noqa: F811
    engine, venue, _ = mx
    verified, _ = close_attributed(mx, "BTC/USD")
    pending, _ = close_attributed(mx, "ETH/USD", verified=False)
    day = venue.now.astimezone(NY).date()
    tag_trade(engine.store, verified, day)
    add_versions(engine.store, verified, maintenance_policy=MAINTENANCE_RECORD,
                 trade_plan_version="CRYPTO_TRADE_PLAN_V1")
    body = compute_scorecard(engine.store.repo, day,
                             now=day_bounds(day)[1] + timedelta(hours=1))
    overall = body["windows"]["1d"]["overall"]
    results = overall["results"]
    assert (results["trades_closed"], results["r_net_count"], results["fees_unverified"]) == (
        2, 1, 1)
    assert results["r_net_scope"] == "FEE_VERIFIED_TRADES_ONLY"
    assert results["fees_verified_share"] == "0.5000"
    assert results["fees_unverified_by_reason"] == {"NO_FEE_EVIDENCE": 1}
    assert results["gross_r_count"] == 2 and results["mean_gross_r"] is not None
    lines = {line["setup_id"]: line for line in overall["trades"]}
    done, waiting = lines[str(verified)], lines[str(pending)]
    assert done["r_net"] is not None and done["fees_status"] == "VERIFIED"
    assert waiting["r_net"] is None and waiting["gross_r"] is not None
    assert waiting["fees_status"] == "NO_FEE_EVIDENCE"
    assert done["regime"]["day_tag"] == TAG and done["regime"]["btc_1h"] == "DOWN_2+"
    assert waiting["regime"] is None
    assert done["a_versions"]["maintenance"] == "CRYPTO_MAINTENANCE_V3"
    assert done["a_versions"]["trade_plan"] == "CRYPTO_TRADE_PLAN_V1"
    assert overall["regime"]["tag"] == TAG
    dims = overall["dimensions"]
    assert dims["by_regime"]["day_tag"][TAG]["r_net_count"] == 1
    assert dims["by_regime"]["day_tag"]["UNTAGGED"] == {
        **dims["by_regime"]["day_tag"]["UNTAGGED"], "closed": 1, "r_net_count": 0,
        "status": "NOT_ENOUGH_DATA"}
    assert dims["by_version_field"]["trade_plan_version"]["NONE"]["closed"] == 1
    assert body["method"]["r_net_scope"] == "FEE_VERIFIED_TRADES_ONLY"
    assert body["method"]["dimension_cell_minimum"] == 30
    text, _ = scorecard_summary(body, recorded=False)
    assert "over 1 of 2 closed (fee-verified only; 1 awaiting fee evidence: NO_FEE_EVIDENCE 1)" \
        in text
    assert f"Regime (day): {TAG}" in text
    assert "NOT_ENOUGH_DATA in every cell (minimum 30 fee-verified trades; 1 trades tagged)" \
        in text


def test_fees_posted_after_a_day_was_scored_show_in_the_next_days_scorecard(mx):  # noqa: F811
    engine, venue, _ = mx
    sid, _ = close_attributed(mx, "BTC/USD", verified=False)
    day = venue.now.astimezone(NY).date()
    status, _ = record_scorecard(engine.store, day, now=day_bounds(day)[1] + timedelta(hours=1))
    assert status == "RECORDED"
    verify_fees(engine, venue, sid)  # Alpaca's daily batch posts after the 05:30 UTC run.
    following = day + timedelta(days=1)
    body = compute_scorecard(engine.store.repo, following,
                             now=day_bounds(following)[1] + timedelta(hours=1))
    late = body["windows"]["1d"]["overall"]["late_fee_settlements"]
    assert late["count"] == 1 and late["still_unverified"] == []
    [entry] = late["items"]
    assert entry["setup_id"] == str(sid) and entry["scored_day"] == day.isoformat()
    assert D(entry["r_net"]) > 0
    text, _ = scorecard_summary(body, recorded=False)
    assert "Fees settled since their day was scored: 1 trades" in text


# --- The weekly review ------------------------------------------------------------------------


def test_the_weekly_review_reads_policy_records_and_reports_fee_coverage(mx):  # noqa: F811
    """A maintained trade's state keeps its maintenance policy as a record (the live ledger's
    33 of 48 closed trades on 2026-10-02): counting it as a dict raised TypeError, so the first
    week with trades (ending 2026-10-04) would have failed its review."""
    engine, venue, _ = mx
    verified, _ = close_attributed(mx, "BTC/USD")
    pending, _ = close_attributed(mx, "ETH/USD", verified=False)
    add_versions(engine.store, verified, arm="JEV_MANAGED",
                 maintenance_policy=MAINTENANCE_RECORD,
                 holding_policy={"policy_id": "CRYPTO_WINDOW_REVIEW_V1"})
    add_versions(engine.store, pending, arm="JEV_MANAGED",
                 maintenance_policy=MAINTENANCE_RECORD)
    tag_trade(engine.store, verified, venue.now.astimezone(NY).date())
    week_end = last_completed_week_end(venue.now + timedelta(days=7))
    _, until = day_bounds(week_end)
    body = compute_review(engine.store.repo, week_end, now=until + timedelta(hours=1))
    management = next(t for t in body["tests"] if t["test_id"] == "MANAGEMENT_VALUE")
    assert management["current_version"] == "CRYPTO_MAINTENANCE_V3"
    assert body["fee_verification"]["all_before_week_end"] == {
        "closed": 2, "fee_verified": 1, "fees_unverified": 1}
    assert body["fee_verification"]["week"]["closed"] == 2
    dims = body["dimensions"]
    assert dims["by_regime"]["day_tag"][TAG]["status"] == "NOT_ENOUGH_DATA"
    assert dims["by_version_field"]["maintenance_policy"]["CRYPTO_MAINTENANCE_V3"][
        "closed"] == 2
    text, data = review_summary(body, recorded=False)
    assert "Net R rests on fee-verified trades only: 1 of 2 closed to date" in text
    assert "NOT_ENOUGH_DATA in every cell" in text and data["fee_verification"]
