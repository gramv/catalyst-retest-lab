"""Package trade-plan on the fixture venue (owner approval 2026-10-02, plan A3-A5; migration 027):
CRYPTO_TRADE_PLAN_V1 admission, sizing, protection and measurement end to end, with
JEV_MANAGED_RISK_V4 (026) and CRYPTO_ENTRY_PACING_V1; CRYPTO_MAINTENANCE_V4's guarded stop
raises; CRYPTO_STOP_BREACH_V4's limit cushion; and setups under the earlier versions behaving as
before.

Fixture evidence only: per-test disposable PostgreSQL databases built from the real migrations
(schema 27, so the reservation guard reads lab.managed_planned_stop), the fake paper venue,
scripted bars and a mock Jev transport. No broker, provider, network or owner-ledger contact.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_holding, stop_breach, trade_plan
from catalyst_lab import crypto_maintenance as cm
from catalyst_lab.account_risk import JEV_MANAGED_ARM, MANAGED_RISK_V4_POLICY_ID
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_execution import ManagedExecution
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.system_check import AdmissionRefused
from tests.maintenance_fixtures import (
    DEFAULT,
    Bars,
    bodies,
    decisions,
    entry_order,
    fill,
    fresh_bar,
    maintainer,
    open_trade,
    publish,
    quote,
    run_pass,
    stop_orders,
    trigger,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission
from tests.test_account_risk_policy import owner
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import observation

V4 = MANAGED_RISK_V4_POLICY_ID
# CRYPTO_MAINTENANCE_V4's own behaviour: admission as before package jev-b1 (V5 is
# tests/test_jev_b1_*.py).
pytestmark = pytest.mark.usefixtures("pre_jev_b1_admission")


def engine_of(mt, **options):
    engine, venue, reviews = mt
    built = ManagedExecution(engine.repo, engine.broker, policy=engine.policy,
                             clock=engine.now, review_store=reviews, **options)
    assert built.reconcile()["clean"]
    return built, venue, reviews


def wide_bars(venue, spread="2"):
    """The maintenance fixture's bars with SOL/USD's 1-hour bars 4.00 high to low (HR 4)."""
    bars = Bars(venue)
    bars.spec[("SOL/USD", "1Hour")] = {"base": D("106"), "spread": D(spread)}
    return bars


def plan_admit(mt, bars, symbol="SOL/USD", *, levels=DEFAULT, hourly_range=None):
    from tests.test_system_check import at_price

    engine, venue, _ = mt
    selected = publish(mt, [symbol], levels=levels)
    reader = hourly_range or trade_plan.HourlyRangeReader(bars, clock=lambda: venue.now)
    return engine.admit(selected[symbol], live_quote=at_price("100.49", "100.51", at=venue.now),
                        hourly_range=reader)


def state(mt, sid):
    return mt[0]._load(sid)[1]


@pytest.fixture
def planned(mt):
    """The managed engine under JEV_MANAGED_RISK_V4 (equity slices, 2% crypto cap; the deploy
    default) on the schema-27 ledger: CRYPTO_TRADE_PLAN_V1 is active."""
    built = engine_of(mt, risk_policy_id=V4, trade_plan_enabled=True)
    assert built[0].trade_plan_active
    return built


# --- Activation -----------------------------------------------------------------------------


@pytest.mark.trade_plan
def test_on_a_schema_27_ledger_an_engine_plans_by_default(mt, er):
    from catalyst_lab.config import SCHEMA_VERSION

    with mt[0].repo.connect() as conn:
        version = conn.execute("SELECT max(version) AS v FROM lab.schema_migrations").fetchone()
    # 028/029 (page views), 030 (selection) and 031 (plugin-c3) keep 027's function.
    assert version["v"] == SCHEMA_VERSION == 31
    assert engine_of(mt)[0].trade_plan_active is True  # Follows the ledger (027's function).
    assert engine_of(mt, trade_plan_enabled=True)[0].trade_plan_active is True
    assert engine_of(mt, trade_plan_enabled=False)[0].trade_plan_active is False
    with pytest.raises(ValueError, match="EXPLICIT_TRADE_PLAN_SETTING_REQUIRED"):
        engine_of(mt, trade_plan_enabled="yes")
    # A ledger without 027's function: the default does not plan, and requiring it fails.
    with owner(er).connect() as conn:
        conn.execute("DROP FUNCTION lab.managed_planned_stop(uuid)")
    assert engine_of(mt)[0].trade_plan_active is False
    with pytest.raises(ValueError, match="TRADE_PLAN_MIGRATION_REQUIRED"):
        engine_of(mt, trade_plan_enabled=True)


@pytest.mark.trade_plan
def test_the_default_engine_on_a_27_ledger_admits_a_v3_pick_on_its_plan(mt):
    engine, venue, reviews = engine_of(mt, risk_policy_id=V4)
    fresh_bar(mt)
    sid = plan_admit((engine, venue, reviews), wide_bars(venue))
    assert state(mt, sid)["trade_plan"]["stop"] == "92.00"


def test_the_earlier_tests_default_admits_on_research_levels(mt):
    assert trade_plan.ADMITTED is None and mt[0].trade_plan_active is False


def test_without_the_plan_a_v3_pick_is_admitted_on_its_research_levels_as_before(mt):
    engine, venue, _ = mt
    fresh_bar(mt)
    sid = plan_admit(mt, wide_bars(venue))
    current = state(mt, sid)
    assert (current["stop"], current["target"]) == ("95", "111")
    assert "trade_plan" not in current
    # Package review-window's setting still applies to the setups outside the plan.
    assert current["holding_policy"] == crypto_holding.admission_policy(
        True, current["arm"]).record()


# --- CRYPTO_TRADE_PLAN_V1 end to end ------------------------------------------------------------


def test_a_planned_trade_widens_its_stop_keeps_its_dollar_risk_and_is_measured_on_the_plan(
        planned):
    engine, venue, _ = planned
    fresh_bar(planned)
    sid = plan_admit(planned, wide_bars(venue))
    current = state(planned, sid)
    plan = current["trade_plan"]
    # HR 4: stop 100 - 8 = 92 below the research 95; the 1.5R cap 112 is above the target 111.
    assert (plan["hourly_range"], plan["stop"], plan["stop_basis"]) == (
        "4", "92.00", "HOURLY_RANGE_FLOOR")
    assert (plan["target"], plan["target_basis"], plan["target_cap"]) == (
        "111.00", "RESEARCH_TARGET", "112.00")
    assert plan["research_levels"] == DEFAULT
    assert plan["hourly_range_evidence"]["bars"] == 24
    assert (current["stop"], current["target"]) == ("92.00", "111.00")
    # The packet keeps the research levels the reviews bound.
    assert engine.store.active()[0]["record_json"]["levels"] == DEFAULT
    # The version pins a 24-hour window in both arms.
    window = trade_plan.CRYPTO_TRADE_PLAN.window_setting()
    assert current["holding_policy"] == crypto_holding.admission_policy(
        True, current["arm"], window).record()
    assert current["holding_window_seconds"] == 86400
    # A4 and A5 ride along.
    assert current["stop_limit_policy"] == stop_breach.CRYPTO_STOP_BREACH_V4.record()
    decision = trigger(planned, sid)
    # 0.5% of $10,000 over max entry 100.10 - plan stop 92 = 8.10 a coin: 6.1728 coins, where
    # the research stop (5.10 a coin) would have bought 9.8039 for the same $50.
    assert D(decision["payload"]["qty"]) == D("6.1728")
    assert decision["context"]["sizing"]["planned_risk"] == str(D("6.1728") * D("8.10"))
    assert decision["context"]["binding_constraint"] == "RISK"
    with engine.repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.managed_reservations WHERE setup_id=%s",
                           (sid,)).fetchone()
    assert row["planned_risk"] == row["budget"] == D("6.1728") * D("8.10") <= D(50)
    order = entry_order(planned, "SOL/USD")
    fill(planned, order, order["qty"])
    engine.manage(sid, quote(planned, "100.20"))
    engine.manage(sid, quote(planned, "100.20"))
    [stop] = stop_orders(planned, "SOL/USD")
    # The plan's stop with CRYPTO_STOP_BREACH_V4's limit: 92 x 0.995 = 91.54.
    assert (stop["stop_price"], stop["limit_price"]) == ("92.00", "91.54")
    measured = managed_measurement(engine.repo, sid, as_of=venue.now)
    assert D(measured["initial_planned_risk"]) == D("6.1728") * D("8.10")
    # Official R's denominator: the filled quantity x (max entry - the plan's stop).
    assert D(measured["planned_filled_risk"]) == D(order["qty"]) * D("8.10")
    opened = bodies(engine, "MAINTENANCE_OPENED", sid)
    if opened:  # The maintained arm records the plan's levels as the trade's initial levels.
        assert (opened[0]["initial_stop"], opened[0]["initial_target"]) == ("92.00", "111.00")
        assert D(opened[0]["risk_per_coin"]) == D("8.10")
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_tight_research_stop_on_a_quiet_coin_keeps_its_stop_and_its_target_is_capped(planned):
    engine, venue, _ = planned
    fresh_bar(planned)
    levels = {**DEFAULT, "stop": "98"}  # 2.1% below the max entry; HR 1 keeps it.
    sid = plan_admit(planned, wide_bars(venue, "0.5"), levels=levels)
    current = state(planned, sid)
    assert (current["stop"], current["trade_plan"]["stop_basis"]) == ("98.00", "RESEARCH_STOP")
    assert (current["target"], current["trade_plan"]["target_basis"]) == ("103.00", "PLAN_CAP")


def test_no_hourly_range_is_a_transient_refusal_retried_until_admitted(planned):
    engine, venue, _ = planned
    fresh_bar(planned)
    bars = wide_bars(venue)
    bars.fail = True
    from tests.test_system_check import at_price

    selected = publish(planned, ["SOL/USD"])["SOL/USD"]
    reader = trade_plan.HourlyRangeReader(bars, clock=lambda: venue.now)
    with pytest.raises(AdmissionRefused, match="HOURLY_RANGE_UNAVAILABLE") as caught:
        engine.admit(selected, live_quote=at_price("100.49", "100.51", at=venue.now),
                     hourly_range=reader)
    assert caught.value.details["trade_plan"]["code"] == "SCAN_HTTP_503"
    assert caught.value.details["system_check"]["result"] == "PASSED"
    assert engine.store.active() == [] and bodies(engine, "SYSTEM_CHECK_REFUSED") == []
    with pytest.raises(AdmissionRefused, match="HOURLY_RANGE_UNAVAILABLE"):
        engine.admit(selected, live_quote=at_price("100.49", "100.51", at=venue.now))
    bars.fail = False
    venue.now += timedelta(seconds=61)
    sid = engine.admit(selected, live_quote=at_price("100.49", "100.51", at=venue.now),
                       hourly_range=reader)
    assert state(planned, sid)["trade_plan"]["stop"] == "92.00"


def test_a_plan_whose_target_cannot_clear_the_max_entry_is_refused_once_for_good(planned):
    engine, venue, _ = planned
    fresh_bar(planned)
    levels = {"entry_trigger": "100", "max_entry_price": "104", "stop": "98", "target": "116"}
    from tests.test_system_check import at_price

    selected = publish(planned, ["SOL/USD"], levels=levels)["SOL/USD"]
    reader = trade_plan.HourlyRangeReader(wide_bars(venue, "0.05"), clock=lambda: venue.now)
    for _ in range(2):
        with pytest.raises(AdmissionRefused, match="TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY"):
            engine.admit(selected, live_quote=at_price("100.49", "100.51", at=venue.now),
                         hourly_range=reader)
    [refused] = bodies(engine, "SYSTEM_CHECK_REFUSED")
    assert refused["reason"] == "TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY"
    assert refused["system_check"]["trade_plan"]["target"] == "103.00"
    assert engine.store.active() == []


def test_a_window_setting_does_not_change_a_planned_setups_24_hours(mt):
    window = crypto_holding.parse_window_setting(
        '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}')
    engine, venue, reviews = engine_of(mt, risk_policy_id=V4, crypto_window=window,
                                       trade_plan_enabled=True)
    fresh_bar(mt)
    sid = plan_admit((engine, venue, reviews), wide_bars(venue))
    assert state((engine, venue, reviews), sid)["holding_window_seconds"] == 86400


def test_a_setup_outside_the_plan_reserves_on_its_packet_stop_under_the_new_guard(mt):
    built = engine_of(mt, risk_policy_id=V4, trade_plan_enabled=False)
    engine, venue, _ = built
    fresh_bar(built)
    sid = plan_admit(built, wide_bars(venue))
    decision = trigger(built, sid)
    assert D(decision["payload"]["qty"]) == D("9.8039")
    with engine.repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.managed_reservations WHERE setup_id=%s",
                           (sid,)).fetchone()
        planned_stop = conn.execute("SELECT lab.managed_planned_stop(%s) AS s",
                                    (sid,)).fetchone()["s"]
    assert planned_stop == D(95) and row["planned_risk"] == D("9.8039") * D("5.10")


def test_the_guard_never_reserves_a_planned_setup_on_less_than_its_plan_risk(planned):
    engine, venue, _ = planned
    fresh_bar(planned)
    sid = plan_admit(planned, wide_bars(venue))
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT lab.managed_planned_stop(%s) AS s",
                            (sid,)).fetchone()["s"] == D("92.00")


# --- CRYPTO_MAINTENANCE_V4 ------------------------------------------------------------------


def test_no_stop_is_offered_or_raised_before_the_trade_reaches_1r(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["maintenance_policy"] == cm.CRYPTO_MAINTENANCE_V4.record()
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="KEEP")
    venue.now = cm.floor_time(venue.now, 60) + timedelta(seconds=61)
    kit.prices.set("SOL/USD", "104")  # +0.76R: entry 100.10, R 5.10.
    run_pass(mt, kit)
    [request] = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    assert request["context"]["options"]["stop"] == []
    guards = request["context"]["basis"]["raise_guards"]
    assert guards["last_stop_raise_at"] is None and D(guards["hourly_range"]) > 0
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert decision["outcome"] != "APPLIED" and state(mt, sid)["stop"] == "95"


def test_after_1r_only_stops_two_hourly_ranges_below_the_bid_are_offered_and_spaced(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    venue.now += timedelta(seconds=1)
    kit.prices.set("SOL/USD", "106")
    run_pass(mt, kit)
    [request] = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    hr = D(request["context"]["basis"]["raise_guards"]["hourly_range"])
    floor = D(106) - 2 * hr  # About 102.09 on the fixture bars.
    offered = [(o["price"], o["bases"]) for o in request["context"]["options"]["stop"]]
    assert offered == [("101.33", ["SWING_LOW_1H"]), ("100.61", ["BREAKEVEN_AFTER_FEES"])]
    assert all(D(price) <= floor for price, _ in offered)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["stop"]["new"]) == ("APPLIED", "101.33")
    raised_at = state(mt, sid)["stop_raised_at"]
    assert raised_at == decision["decided_at"] or raised_at
    # The raise is sent as V4's stop-limit: 101.33 x 0.995 = 100.82.
    [old] = stop_orders(mt, "SOL/USD")
    engine.manage(sid, quote(mt, "106"))
    [amend] = decisions(engine, sid, "AMEND")
    assert amend["payload"] == {"stop_price": "101.33", "limit_price": "100.82"}
    engine.manage(sid, quote(mt, "106"))
    assert bodies(engine, "STOP_REPLACED", sid) and state(mt, sid)["stop_replace"] is None
    # A minute later, higher: the swing lows now sit below the floor, but the 15-minute
    # spacing offers nothing.
    venue.now += timedelta(seconds=70)
    kit.prices.set("SOL/USD", "110")
    kit.jev.answer("HOLD")
    run_pass(mt, kit)
    second = bodies(engine, "POSITION_REVIEW_REQUEST", sid)[-1]
    assert second["context"]["options"]["stop"] == []
    assert second["context"]["basis"]["raise_guards"]["last_stop_raise_at"] == raised_at
    assert state(mt, sid)["stop"] == "101.33"
    # Fifteen minutes after the raise the 15-minute swing lows are offered again.
    venue.now = cm.floor_time(venue.now, 60) + timedelta(minutes=16, seconds=1)
    kit.prices.set("SOL/USD", "110")
    kit.jev.answer("RAISE_STOP", stop="first")
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]
    run_pass(mt, kit)
    third = bodies(engine, "POSITION_REVIEW_REQUEST", sid)[-1]
    assert [o["price"] for o in third["context"]["options"]["stop"]][:2] == ["103.00", "102.51"]
    assert bodies(engine, "MAINTENANCE_DECISION", sid)[-1]["stop"]["new"] == "103.00"
    assert state(mt, sid)["stop_raised_at"] == venue.now.isoformat()


def test_a_raise_inside_two_hourly_ranges_of_the_bid_at_decision_time_is_refused(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    kit.jev.hook = lambda body: kit.prices.set("SOL/USD", "104")  # Falls while Jev answers.
    venue.now += timedelta(seconds=1)
    kit.prices.set("SOL/USD", "106")
    run_pass(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", "STOP_INSIDE_HOURLY_RANGE")
    assert state(mt, sid)["stop"] == "95" and "stop_raised_at" not in state(mt, sid)


def test_a_v3_trade_keeps_v3s_unguarded_options(mt, monkeypatch):
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V3)
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    venue.now += timedelta(seconds=1)
    kit.prices.set("SOL/USD", "106")
    run_pass(mt, kit)
    [request] = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    assert "raise_guards" not in request["context"]["basis"]
    assert [(o["price"], o["bases"]) for o in request["context"]["options"]["stop"]] == [
        ("103.00", ["SWING_LOW_15M"]), ("102.51", ["SWING_LOW_15M"]),
        ("101.33", ["SWING_LOW_1H"]), ("100.10", ["BREAKEVEN"])]
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["stop"]["new"]) == ("APPLIED", "103.00")
    assert "stop_raised_at" not in state(mt, sid)


# --- CRYPTO_STOP_BREACH_V4 ----------------------------------------------------------------------


def test_a_new_crypto_trade_rests_its_stop_limit_half_a_percent_below_the_stop(mt):
    sid = open_trade(mt)
    assert state(mt, sid)["stop_limit_policy"] == stop_breach.CRYPTO_STOP_BREACH_V4.record()
    # Detection stays V2's (no Coinbase reference on this engine).
    assert state(mt, sid)["stop_breach_version"] == "CRYPTO_STOP_BREACH_V2"
    [stop] = stop_orders(mt, "SOL/USD")
    assert (stop["stop_price"], stop["limit_price"]) == ("95", "94.52")  # 95 x 0.995 = 94.525.


def test_a_setup_admitted_before_v4_keeps_its_one_tick_limit(mt, monkeypatch):
    monkeypatch.setattr(stop_breach, "ADMITTED_STOP_LIMIT", None)
    sid = open_trade(mt)
    assert "stop_limit_policy" not in state(mt, sid)
    [stop] = stop_orders(mt, "SOL/USD")
    assert (stop["stop_price"], stop["limit_price"]) == ("95", "94.99")


def test_a_planned_trade_in_the_maintained_arm_never_raises_its_target_above_the_cap(
        planned, monkeypatch):
    from catalyst_lab import managed_execution

    monkeypatch.setattr(managed_execution, "assign_arm", lambda sid, pct: JEV_MANAGED_ARM)
    engine, venue, _ = planned
    fresh_bar(planned)
    bars = wide_bars(venue, "0.5")  # HR 1: stop 98 kept, target capped at 103.00.
    sid = plan_admit(planned, bars, levels={**DEFAULT, "stop": "98"})
    trigger(planned, sid)
    order = entry_order(planned, "SOL/USD")
    fill(planned, order, order["qty"])
    engine.manage(sid, quote(planned, "100.20"))
    engine.manage(sid, quote(planned, "100.20"))
    assert state(planned, sid)["target"] == "103.00"
    kit = maintainer(planned, bars=bars)
    kit.jev.answer("RAISE_TARGET", target="KEEP")
    venue.now = cm.floor_time(venue.now, 60) + timedelta(seconds=61)
    kit.prices.set("SOL/USD", "101")
    run_pass(planned, kit)
    [request] = bodies(engine, "POSITION_REVIEW_REQUEST", sid)
    assert request["context"]["basis"]["raise_guards"]["target_cap"] == "103.00"
    assert request["context"]["options"]["target"] == []  # Every high is above the cap.
    assert state(planned, sid)["target"] == "103.00"


# --- The runtime's wiring -------------------------------------------------------------------


def plan_runtime(built, symbols, bars):
    """The managed runtime over ``built`` with the fixture market (latest quotes) and its
    hourly-range reader reading ``bars`` within the tick's market-data budget."""
    import httpx

    from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
    from catalyst_lab.scan_sources import AlpacaMarketSource
    from tests.test_managed_runtime import Research
    from tests.test_system_check import CREDENTIALS, SOURCE_POLICY, MarketData

    engine, venue, _ = built
    data = MarketData()
    source = AlpacaMarketSource(CREDENTIALS, SOURCE_POLICY, lambda: venue.now,
                                transport=httpx.MockTransport(data.handle))
    run = ManagedRuntime(engine, Research(), source, CREDENTIALS, engineering_runtime_policy(),
                         clock=lambda: venue.now, reviewer_heartbeat=lambda: True)
    run.hourly_ranges = trade_plan.HourlyRangeReader(bars, clock=lambda: venue.now,
                                                     spend=run.live_prices.spend_read)
    run.connected = run.research_healthy = True
    assert run.reconcile_once()
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = set(symbols)
    assert run.ready()
    return run


def test_the_runtime_admits_a_planned_pick_and_retries_a_missing_range_without_declining(
        planned):
    from tests.test_system_check import publish_v3, stream, two_slots, v3_pick

    engine, venue, _ = planned
    old, _ = two_slots(venue.now)
    packet = publish_v3(planned, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)["AAA/USD"]
    bars = Bars(venue)
    bars.fail = True
    run = plan_runtime(planned, ["AAA/USD"], bars)
    stream(run, "AAA/USD", at=venue.now)
    run.execution_once()
    assert engine.store.active() == []
    [refused] = bodies(engine, "RUNTIME_ADMISSION_REFUSED")
    assert (refused["reason"], refused["selection_event_seq"]) == (
        "HOURLY_RANGE_UNAVAILABLE", packet["selection_event_seq"])
    assert refused["trade_plan"]["code"] == "SCAN_HTTP_503"
    assert bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []  # Transient: never declined.
    bars.fail = False
    venue.now += timedelta(seconds=61)  # The reader's retry wait.
    assert run.reconcile_once()
    stream(run, "AAA/USD", at=venue.now)
    run.execution_once()
    [setup] = engine.store.active()
    plan = setup["state"]["trade_plan"]
    assert setup["state"]["stop"] == plan["stop"] and plan["hourly_range_evidence"]["bars"] == 24
    assert [c[2] for c in bars.calls] == ["1Hour", "1Hour"]
    assert run.error is None


# --- With JEV_MANAGED_RISK_V4 and CRYPTO_ENTRY_PACING_V1 (packages risk-pacing and trade-plan) --


def test_v4s_two_percent_crypto_cap_counts_each_planned_trade_at_its_plan_risk(planned):
    """Four planned trades at the plan's $50 (0.5% of $10,000, on the wider stop) fill the 2%
    cap; the fifth waits for capacity. Had the guard reserved on the research stop, each would
    have counted $31.48 (6.1728 x 5.10) and two more trades ($300 at real risk) would have fit."""
    from tests.test_system_check import at_price

    engine, venue, _ = planned
    fresh_bar(planned)
    symbols = ["AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD", "EEE/USD"]
    bars = Bars(venue)
    for symbol in symbols:
        bars.spec[(symbol, "1Hour")] = {"base": D("106"), "spread": D("2")}
    reader = trade_plan.HourlyRangeReader(bars, clock=lambda: venue.now)
    selected = publish(planned, symbols)
    sids = [engine.admit(selected[s], live_quote=at_price("100.49", "100.51", at=venue.now),
                         hourly_range=reader) for s in symbols]
    outcomes = [engine.observe_trigger(sid, observation(planned, trade_price="100", bid="100.09",
                                                        ask="100.10")) for sid in sids]
    assert [o["outcome"] for o in outcomes] == ["APPROVED"] * 4 + ["REJECTED"]
    assert outcomes[-1]["reason"] == "MARKET_RISK_CAP"
    assert state(planned, sids[-1])["state"] == "WATCHING"  # A capacity wait, not an end.
    with engine.repo.connect() as conn:
        rows = conn.execute("""SELECT r.planned_risk,lab.managed_planned_stop(r.setup_id) AS s
            FROM lab.managed_reservations r ORDER BY r.setup_id""").fetchall()
    assert len(rows) == 4
    assert all(r["s"] == D("92.00") and r["planned_risk"] == D("6.1728") * D("8.10")
               for r in rows)
    assert sum(r["planned_risk"] for r in rows) <= D(200)
    assert all(o["context"]["risk_policy_id"] == V4 for o in outcomes)


def test_the_pacing_gate_holds_a_planned_trigger_then_it_enters_on_the_plan(planned):
    from catalyst_lab import regime_gate
    from tests.test_regime_gate import paced, refeed, waits

    engine, venue, _ = planned
    fresh_bar(planned)
    pacing = paced(planned, moves=("-0.03", "-0.03", "-0.03"))  # A falling market.
    sid = plan_admit(planned, wide_bars(venue))
    current = state(planned, sid)
    assert current["trade_plan"]["stop"] == "92.00" and regime_gate.active(current)
    held = engine.observe_trigger(sid, observation(planned, trade_price="100", bid="100.09",
                                                   ask="100.10"))
    assert held is None and len(waits(engine, sid)) == 1
    assert decisions(engine, sid) == []  # No decision, reservation or order while held.
    venue.now += timedelta(minutes=2)
    refeed(planned, pacing)
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]
    decision = engine.observe_trigger(sid, observation(planned, trade_price="100", bid="100.09",
                                                       ask="100.10"))
    assert decision["outcome"] == "APPROVED" and D(decision["payload"]["qty"]) == D("6.1728")
