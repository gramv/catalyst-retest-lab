"""AFTER_EXIT_PATH_V1, MANAGEMENT_CHANGE_CONTEXT_V1 and TRADED_LEVELS_V1 (package
learning-loop2): after-exit price paths, the management split and replays on the plan's traded
levels.

Fixture evidence only: the fixture paper venue on per-test disposable PostgreSQL databases,
hand-built bars. No broker, Jev, network or owner ledger.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from catalyst_lab import trade_paths as tp
from catalyst_lab import trade_plan
from catalyst_lab.learning_replays import (
    DAY_REPLAY_EVENT,
    REPLAY_EVENT,
    counterfactual_r,
    scale_of,
    traded_basis,
)
from catalyst_lab.pick_outcomes import TAKER_FEE_TIER1, parse_bars, r_values, traded_levels
from catalyst_lab.unchanged_plan import traded_scale
from tests.learning_fixtures import FakeBars, close_attributed, minute_rows
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401

T0 = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)
LEVELS = {"entry_trigger": "100", "max_entry_price": "100.5", "stop": "98", "target": "106"}
PLAN = {"policy": trade_plan.CRYPTO_TRADE_PLAN.record(), "stop": "96.5", "target": "103.25",
        "target_cap": "103.25"}


# --- Pure ---------------------------------------------------------------------------------------


def test_the_path_after_an_exit_in_percent_and_r():
    # Exit at 100 with 2 per coin of risk: up to 103 in the first hour, down to 96 by 4 hours.
    closes = [101, 103] + [100] * 120 + [96] * 120 + [99] * 60
    bars = parse_bars(minute_rows(T0, closes))
    path = tp.path_after_exit(bars, T0, D(100), D(2))
    one = path["+1h"]
    assert one["status"] == "MEASURED" and one["bars"] == 60
    assert (one["high_pct"], one["high_r"]) == (D("3.0000"), D("1.5000"))
    assert one["close_r"] == D("0.0000") and one["low_r"] == D("0.0000")
    four = path["+4h"]
    assert four["low_pct"] == D("-4.0000") and four["low_r"] == D("-2.0000")
    assert four["close_price"] == "96"
    assert path["+8h"]["close_price"] == "99" and path["+8h"]["bars"] == 302
    assert path["+24h"]["high_r"] == D("1.5000")
    assert tp.path_after_exit([], T0, D(100), D(2))["+1h"] == {"status": "NO_BARS"}


def test_the_decision_buckets():
    assert tp.distance_bucket(D(100), D("99.2")) == "0-1%"
    assert tp.distance_bucket(D(100), D(99)) == "1-2%"
    assert tp.distance_bucket(D(100), D("97.5")) == "2-3%"
    assert tp.distance_bucket(D(100), D(90)) == "3%+"
    assert tp.distance_bucket(D(100), None) == "NO_STOP_CHANGE"
    assert tp.distance_bucket(None, D(90)) == "UNKNOWN"
    assert [tp.time_bucket(D(h)) for h in ("0.5", "1", "4", "7.9", "8")] == [
        "0-1h", "1-4h", "4-8h", "4-8h", "8h+"]
    assert tp.time_bucket(None) == "UNKNOWN"


def test_the_after_exit_summary_leaves_out_excluded_trades():
    def path(sid, kind, high_r, close_r):
        return {"setup_id": sid, "exit_kind": kind, "horizons": {
            "+24h": {"status": "MEASURED", "high_r": high_r, "close_r": close_r},
            "+1h": {"status": "NO_BARS"}}}
    paths = [path("a", "STOP", "1.2", "0.5"), path("b", "STOP", "0.4", "-0.2"),
             path("c", "TARGET", "0.3", "0.1"), path("x", "STOP", "3", "3")]
    summary = tp.after_exit_summary(paths, excluded={"x"}, minimum=2)
    assert summary["trades"] == 3 and summary["excluded_trades"] == 1
    stop = summary["by_exit_kind"]["STOP"]
    assert (stop["trades"], stop["high_at_least_1r"], stop["high_at_least_1r_share"]) == (
        2, 1, D("0.5000"))
    assert stop["mean_close_r_24h"] == D("0.1500") and stop["status"] == "MEASURED"
    assert summary["by_exit_kind"]["TARGET"]["status"] == "NOT_ENOUGH_DATA"


# --- TRADED_LEVELS_V1 ---------------------------------------------------------------------------


def test_the_traded_scale_is_the_plans_stop_and_research_levels_are_kept_beside():
    assert traded_scale(LEVELS, {"trade_plan": PLAN}) == (D("100.5"), D("96.5"))
    assert traded_scale(LEVELS, {}) == (D("100.5"), D(98))
    assert traded_basis(LEVELS, {"trade_plan": PLAN}) == {
        "r_basis": "TRADE_PLAN_STOP", "research_levels": {
            "max_entry_price": "100.5", "stop": "98", "target": "106"}}
    assert traded_basis(LEVELS, {})["r_basis"] == "ADMITTED_PACKET_STOP"
    assert traded_levels(LEVELS, {"trade_plan": PLAN}) == {**LEVELS, "stop": "96.5",
                                                          "target": "103.25"}
    assert traded_levels(LEVELS, {}) is None and traded_levels(LEVELS, None) is None
    # An altered policy record is never read as a plan.
    assert traded_levels(LEVELS, {"trade_plan": {**PLAN, "policy": {"x": 1}}}) is None


def test_a_replay_recorded_on_the_research_stop_is_read_on_the_traded_scale():
    """Before TRADED_LEVELS_V1 a CRYPTO_TRADE_PLAN_V1 setup's replay was priced on the research
    stop (98) while its official R uses the plan's (96.5): the reader rebases it from the
    recorded exit price; the record is unchanged."""
    _, old_net = r_values(D("100.5"), D(98), D(103))
    body = {"unchanged_net_r": str(old_net), "initial_stop": "98", "exit_price": "103"}
    basis = scale_of({"levels": LEVELS}, {"trade_plan": PLAN})
    assert basis == {"entry": D("100.5"), "stop": D("96.5")}
    rebased = counterfactual_r(REPLAY_EVENT, body, basis)
    _, expected = r_values(D("100.5"), D("96.5"), D(103), fee_rate=TAKER_FEE_TIER1)
    assert rebased == expected and rebased < old_net
    assert counterfactual_r(REPLAY_EVENT, body) == old_net  # No basis: as recorded.
    # A replay recorded on the traded scale (r_basis present) is read as recorded.
    assert counterfactual_r(REPLAY_EVENT, {**body, "r_basis": "TRADE_PLAN_STOP"},
                            basis) == old_net
    same = {"alternative_net_r": "0.3", "initial_stop": "96.5", "exit_price": "103"}
    assert counterfactual_r(DAY_REPLAY_EVENT, same, basis) == D("0.3")
    assert scale_of({}, {}) is None


# --- The nightly step on a ledger -----------------------------------------------------------------


def exit_of(engine, sid):
    with engine.store.repo.connect() as conn:
        return conn.execute(
            """SELECT max(filled_at) FILTER(WHERE side='sell') AS exit_at,
            min(filled_at) FILTER(WHERE side='buy') AS entry_at,
            sum(qty*price) FILTER(WHERE side='sell')/sum(qty) FILTER(WHERE side='sell')
              AS exit_price FROM lab.managed_fills WHERE setup_id=%s""", (sid,)).fetchone()


def test_after_exit_paths_record_once_after_24_hours_on_the_official_r_scale(mx):  # noqa: F811
    engine, venue, _ = mx
    sid, raw = close_attributed(mx, "BTC/USD")
    fill = exit_of(engine, sid)
    exit_at = fill["exit_at"].astimezone(UTC)
    reader = FakeBars(minutes={"BTC/USD": minute_rows(exit_at, [111, 112, 110] + [109] * 60)})
    early = tp.record_paths(engine.store, reader, now=exit_at + timedelta(hours=23))
    assert early["not_yet_ready"] == 1 and early["recorded"] == 0
    now = exit_at + timedelta(hours=24, minutes=10)
    summary = tp.record_paths(engine.store, reader, now=now)
    assert summary["recorded"] == 1
    assert tp.record_paths(engine.store, reader, now=now)["already_recorded"] == 1
    [body] = tp.recorded_paths(engine.store.repo)
    assert body["path_version"] == "AFTER_EXIT_PATH_V1" and body["setup_id"] == str(sid)
    assert body["exit_kind"] == "TARGET"
    risk = D(raw["levels"]["max_entry_price"]) - D(raw["levels"]["stop"])
    assert D(body["r_scale"]["risk_per_coin"]) == risk
    assert body["r_scale"]["basis"] == "ADMITTED_PACKET_STOP"
    exit_price = D(body["exit_price"])
    assert D(str(body["horizons"]["+1h"]["high_r"])) == ((D(112) - exit_price) / risk).quantize(
        D("0.0001"))
    assert body["horizons"]["+4h"]["close_price"] == "109"


def test_a_failed_read_leaves_the_trade_for_the_next_run(mx):  # noqa: F811
    engine, _venue, _ = mx
    sid, _ = close_attributed(mx, "BTC/USD")
    exit_at = exit_of(engine, sid)["exit_at"].astimezone(UTC)
    code, details = tp.record_trade_paths(engine.store, FakeBars(fail={"BTC/USD"}),
                                          now=exit_at + timedelta(days=1, hours=1))
    assert code == "TRADE_PATHS_BARS_UNAVAILABLE" and details["path_failed"] == 1
    assert tp.recorded_paths(engine.store.repo) == []


def test_decision_context_and_the_management_split(mx):  # noqa: F811
    engine, _venue, _ = mx
    sid, _ = close_attributed(mx, "BTC/USD")
    fill = exit_of(engine, sid)
    entry_at = fill["entry_at"].astimezone(UTC)
    at = entry_at + timedelta(hours=2)
    with engine.store.transaction() as conn:
        engine.store.event(conn, DAY_REPLAY_EVENT, {
            "setup_id": str(sid), "symbol": "BTC/USD", "decision": "CONTINUE", "at":
            at.isoformat(), "old_stop": "100", "new_stop": "105", "alternative_net_r": "0.2",
            "initial_stop": "98", "exit_price": "104", "source_event_seq": 424242},
            key="day-review-decision-replay:424242")
    reader = FakeBars(minutes={"BTC/USD": minute_rows(at, [110, 110])})
    summary = tp.record_contexts(engine.store, reader, now=at + timedelta(days=2))
    assert summary["recorded"] == 1
    assert tp.record_contexts(engine.store, reader, now=at + timedelta(days=2))["recorded"] == 0
    context = tp.recorded_contexts(engine.store.repo)[424242]
    assert context["decision_type"] == "DAY_REVIEW_CONTINUE"
    assert context["price_at_decision"] == "110"
    assert D(str(context["new_stop_distance_pct"])) == D("4.5455")  # (110 - 105) / 110.
    assert context["distance_bucket"] == "3%+" and context["time_bucket"] == "1-4h"
    assert context["day_tag"] == "UNTAGGED"
    trades = {str(sid): {"r_net": D("0.5"), "r_basis": None}}
    split = tp.management_split(engine.store.repo, trades, minimum=1)
    cell = split["by_stop_distance"]["DAY_REVIEW_CONTINUE"]["3%+"]
    assert cell == {"changes": 1, "mean_r_difference": D("0.3000"), "helped": 1, "hurt": 0,
                    "status": "MEASURED"}
    assert split["by_time_in_trade"]["DAY_REVIEW_CONTINUE"]["1-4h"]["changes"] == 1
    # STATS_EXCLUSION_V1: an excluded trade leaves the split.
    assert tp.management_split(engine.store.repo, trades, excluded={str(sid)})["changes"] == 0


def test_scorecard_buckets_give_net_r_per_resolved_pick_and_traded_plan_figures():
    """Package learning-loop2 (L2 and TRADED_LEVELS_V1): a bucket's net R per resolved pick
    (an untriggered pick counts 0 R) for the lessons' ranking, and the selection groups' figures
    on the admitted setups' traded levels beside the research levels'."""
    from types import SimpleNamespace

    from catalyst_lab.scorecard import bucket_line, selection

    def pick(net, *, triggered=True, traded=None, bucket="TOP_K"):
        outcome = {"data_complete": True, "triggered": triggered,
                   "net_r": net if triggered else None}
        record = SimpleNamespace(ranking_status="RANKED", selected=bucket == "TOP_K",
                                 selection_status="SELECTED", replacement_for=None)
        return SimpleNamespace(setup=None, shadow=outcome, record=record, traded_shadow=None
                               if traded is None else {"data_complete": True, "net_r": traded})

    picks = [pick("1"), pick("-0.5"), pick(None, triggered=False), pick(None, triggered=False)]
    line = bucket_line(picks)
    assert (line["shadow_resolved"], line["sum_shadow_r_net"],
            line["shadow_r_net_per_resolved_pick"]) == (4, D("0.5000"), D("0.1250"))
    assert line["mean_shadow_r_net"] == D("0.2500")  # Per triggered pick, as before.
    groups = selection([pick("1", traded="1.4"), pick("-1", traded="-0.8")])
    selected = groups["selected"]
    assert selected["traded_plan_r_count"] == 2
    assert selected["mean_traded_plan_shadow_r_net"] == D("0.3000")
    assert selected["mean_shadow_r_net"] == D("0.0000")
