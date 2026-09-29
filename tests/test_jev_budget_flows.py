"""CRYPTO_MAINTENANCE_V3 on the fixture venue (package jev-budget): each tier's routine cadence,
events between routine reviews, nothing sent when exhausted while protection runs, setups that
recorded V2 untouched, the guard's facts in each request and the meter reading them back, the
runtime status, the heartbeat and the watchdog.

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars, a
mock Jev transport, a stub guard where a test sets the tier and the real guard where it reads
the ledger. No broker, provider, network or owner-ledger contact.
"""

from datetime import timedelta
from decimal import Decimal as D

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import jev_budget as jb
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_ops import status_alarms
from catalyst_lab.managed_runtime import ManagedRuntime
from tests.maintenance_fixtures import (
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
    spend_guard,
    stop_orders,
    trigger,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import v2_admission as v2_admission
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_budget_meter import call

WATCHDOG = {"tick_max_age_seconds": 15, "reconciliation_max_age_seconds": 90,
            "research_max_age_seconds": 180}


class StubGuard:
    """A spend guard whose tier the test sets (the real guard's tiers are tested against the
    ledger in tests/test_jev_budget_meter.py)."""

    def __init__(self, tier=jb.NORMAL):
        self.tier, self.asked = tier, 0

    def evaluate(self, now):
        self.asked += 1
        if self.tier not in jb.TIERS:
            return jb.unavailable(now, "FIXTURE_GUARD_UNAVAILABLE")
        return jb.GuardDecision(self.tier, "FIXTURE", now, tier_event_seq=4242, tier_since=now)


def state(mt, sid):
    return mt[0]._load(sid)[1]


def requests(engine, sid):
    return bodies(engine, "POSITION_REVIEW_REQUEST", sid)


def at(mt, kit, base, seconds, bid="101"):
    """One pass at ``base`` + ``seconds`` with the coin at ``bid`` (101: no milestone)."""
    mt[1].now = base + timedelta(seconds=seconds)
    kit.prices.set("SOL/USD", bid)
    return run_pass(mt, kit)


def bar_start(mt):
    """The 15-minute (hence 5-minute and minute) boundary the fixture's trade opened after."""
    return cm.floor_time(mt[1].now, 900)


def identities(mt, request_ids):
    with mt[2].connect() as conn:
        return [conn.execute("SELECT evidence_identity FROM lab.jev_requests WHERE request_id=%s",
                             (r,)).fetchone()["evidence_identity"] for r in request_ids]


# --- Cadence by tier -----------------------------------------------------------------------------


def test_normal_reviews_every_completed_minute_and_records_the_guard(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["maintenance_policy"] == cm.CRYPTO_MAINTENANCE_V3.record()
    kit = maintainer(mt, guard=StubGuard(jb.NORMAL))
    base = bar_start(mt)
    [first] = at(mt, kit, base, 61)
    [second] = at(mt, kit, base, 121)
    assert at(mt, kit, base, 150) == []
    reasons = [r["trigger"]["reasons"] for r in requests(engine, sid)]
    assert reasons == [["BAR_1M"], ["BAR_1M"]]
    facts = requests(engine, sid)[0]["trigger"]["spend_guard"]
    assert facts == {"version": "JEV_SPEND_GUARD_V1", "tier": "NORMAL", "tier_event_seq": 4242,
                     "review_bar_seconds": 60, "routine_weight": 1}
    request = requests(engine, sid)[0]
    assert request["context"]["identity"]["spend_guard"] == facts
    assert request["context"]["policy"] == cm.CRYPTO_MAINTENANCE_V3.record()
    assert request["context"]["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V5"
    assert [i["spend_guard"] for i in identities(mt, [first, second])] == [facts, {
        **facts, "tier_event_seq": 4242}]
    # What Jev reads is V2's context: the guard's facts are never in the state it is sent.
    assert all("spend_guard" not in str(sent["state"]) for sent in kit.jev.calls)
    decision = bodies(engine, "MAINTENANCE_DECISION", sid)[-1]
    assert (decision["policy_id"], decision["answer_rule"], decision["outcome"]) == (
        "CRYPTO_MAINTENANCE_V3", "MAINTENANCE_ANSWER_RULE_V2", "HELD")
    assert verify_events(engine.repo.export_events())["valid"]


def test_throttled_reviews_every_five_completed_minutes(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, guard=StubGuard(jb.THROTTLED))
    base = bar_start(mt)
    for minute in (1, 2, 3, 4):  # Completed minutes that are not 5-minute bars.
        assert at(mt, kit, base, 60 * minute + 1) == []
    [first] = at(mt, kit, base, 301)
    for minute in (6, 7, 8, 9):
        assert at(mt, kit, base, 60 * minute + 1) == []
    [second] = at(mt, kit, base, 601)
    triggers = [r["trigger"] for r in requests(engine, sid)]
    assert [t["reasons"] for t in triggers] == [["BAR_5M"], ["BAR_5M"]]
    assert [t["served"]["bar_end"] for t in triggers] == [
        (base + timedelta(seconds=300)).isoformat(), (base + timedelta(seconds=600)).isoformat()]
    assert triggers[0]["spend_guard"] == {"version": "JEV_SPEND_GUARD_V1", "tier": "THROTTLED",
                                          "tier_event_seq": 4242, "review_bar_seconds": 300,
                                          "routine_weight": 5}
    assert [i["spend_guard"]["routine_weight"] for i in identities(mt, [first, second])] == [5, 5]
    assert len(kit.jev.calls) == 2


def test_tight_reviews_every_fifteen_completed_minutes(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, guard=StubGuard(jb.TIGHT))
    base = bar_start(mt)
    for minute in (1, 5, 10, 14):
        assert at(mt, kit, base, 60 * minute + 1) == []
    [first] = at(mt, kit, base, 901)
    assert at(mt, kit, base, 1201) == []
    [request] = requests(engine, sid)
    assert request["trigger"]["reasons"] == ["BAR_15M"]
    assert request["trigger"]["spend_guard"]["routine_weight"] == 15
    assert identities(mt, [first])[0]["spend_guard"]["review_bar_seconds"] == 900


def test_a_tier_change_takes_effect_at_the_next_routine_bar(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    guard = StubGuard(jb.THROTTLED)
    kit = maintainer(mt, guard=guard)
    base = bar_start(mt)
    [_] = at(mt, kit, base, 301)  # THROTTLED: the 5-minute bar.
    guard.tier = jb.NORMAL  # The budget allows every minute again.
    assert at(mt, kit, base, 330) == []  # That minute was served.
    [_] = at(mt, kit, base, 361)
    guard.tier = jb.TIGHT
    assert at(mt, kit, base, 421) == [] and at(mt, kit, base, 601) == []
    [_] = at(mt, kit, base, 901)
    assert [r["trigger"]["reasons"] for r in requests(engine, sid)] == [
        ["BAR_5M"], ["BAR_1M"], ["BAR_15M"]]


def test_events_still_review_at_once_when_throttled_or_tight(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)  # Entry 100.10, stop 95, target 111: +1R 105.20, +2R 110.30.
    guard = StubGuard(jb.THROTTLED)
    kit = maintainer(mt, guard=guard)
    base = bar_start(mt)
    [_] = at(mt, kit, base, 301)  # The routine 5-minute review.
    [milestone] = at(mt, kit, base, 400, bid="106")  # +1R between bars: at once.
    guard.tier = jb.TIGHT
    # +2R and within 0.5% of the target 20 s later: near the target skips the one-minute floor.
    [near] = at(mt, kit, base, 420, bid="110.50")
    reasons = [r["trigger"]["reasons"] for r in requests(engine, sid)]
    assert reasons == [["BAR_5M"], ["R_MILESTONE"], ["R_MILESTONE", "NEAR_TARGET"]]
    weights = [i["spend_guard"]["routine_weight"] for i in identities(mt, [milestone, near])]
    assert weights == [1, 1]  # Event reviews stand for themselves.
    assert requests(engine, sid)[-1]["trigger"]["near_target_exempt"] is True
    assert [r["trigger"]["spend_guard"]["tier"] for r in requests(engine, sid)] == [
        "THROTTLED", "THROTTLED", "TIGHT"]


# --- Exhausted ------------------------------------------------------------------------------------


def test_exhausted_sends_nothing_records_the_facts_and_resumes_with_them(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    guard = StubGuard(jb.EXHAUSTED)
    kit = maintainer(mt, guard=guard)
    base = bar_start(mt)
    for seconds in (61, 121, 181, 301, 901):
        assert at(mt, kit, base, seconds, bid="106") == []  # +1R and every bar: nothing sent.
    assert kit.jev.calls == [] and requests(engine, sid) == []
    assert not [c for c in kit.bars.calls if c[1] == "SOL/USD"]  # Not even a bar fetched.
    [milestone] = bodies(engine, "MAINTENANCE_TRIGGER", sid)  # The market fact is kept.
    assert (milestone["trigger"], milestone["level"]) == ("R_MILESTONE", "1")
    [skipped] = bodies(engine, "POSITION_REVIEW_SKIPPED", sid)  # Once per lifecycle, episode.
    assert (skipped["reason"], skipped["policy_id"], skipped["code"]) == (
        "JEV_BUDGET_EXHAUSTED", "CRYPTO_MAINTENANCE_V3", None)
    assert skipped["spend_guard"] == {"version": "JEV_SPEND_GUARD_V1", "tier": "EXHAUSTED",
                                      "tier_event_seq": 4242, "review_bar_seconds": None}
    # Next month (or a raised budget): the missed milestone is reviewed at once.
    guard.tier = jb.NORMAL
    [_] = at(mt, kit, base, 961, bid="106")
    assert requests(engine, sid)[0]["trigger"]["reasons"] == ["BAR_1M", "R_MILESTONE"]
    assert len(kit.jev.calls) == 1


def test_exhausted_leaves_protection_the_stop_and_the_target_untouched(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, guard=StubGuard(jb.EXHAUSTED))
    base = bar_start(mt)
    [resting] = stop_orders(mt, "SOL/USD")
    assert at(mt, kit, base, 61, bid="110.50") == []  # Near the target: still nothing sent.
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    assert stop_orders(mt, "SOL/USD") == [resting]  # The broker's stop-limit keeps resting.
    engine.manage(sid, quote(mt, "111"))  # The target: the app sells without Jev.
    assert state(mt, sid)["exit_requested"] == "TARGET_EXIT"
    engine.manage(sid, quote(mt, "111"))
    [close] = venue.orders_of("sell", "market")
    assert D(close["qty"]) == D(entry_order(mt, "SOL/USD")["qty"])
    assert [d["reason"] for d in decisions(engine, sid, "EXIT")] == ["AUTHORIZED_EXIT"]
    assert kit.jev.calls == []


def test_without_a_guard_or_when_it_cannot_decide_v3_trades_wait(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt, guard=None)
    base = bar_start(mt)
    assert at(mt, kit, base, 61) == [] and at(mt, kit, base, 121) == []
    [skipped] = bodies(engine, "POSITION_REVIEW_SKIPPED", sid)
    assert (skipped["reason"], skipped["code"]) == ("JEV_BUDGET_UNAVAILABLE",
                                                    "JEV_SPEND_GUARD_NOT_CONFIGURED")
    broken = StubGuard("NOT_A_TIER")
    kit = maintainer(mt, kit.jev, kit.bars, guard=broken)
    assert at(mt, kit, base, 181) == [] and kit.jev.calls == []
    codes = [b["code"] for b in bodies(engine, "POSITION_REVIEW_SKIPPED", sid)]
    assert codes == ["JEV_SPEND_GUARD_NOT_CONFIGURED", "FIXTURE_GUARD_UNAVAILABLE"]

    class Raising:
        def evaluate(self, now):
            raise RuntimeError("fixture")

    kit = maintainer(mt, kit.jev, kit.bars, guard=Raising())
    assert at(mt, kit, base, 241) == [] and kit.jev.calls == []
    assert bodies(engine, "POSITION_REVIEW_SKIPPED", sid)[-1]["code"] == "RuntimeError"


# --- Setups that recorded V1 or V2 ---------------------------------------------------------------


def test_a_v2_setup_keeps_every_minute_whatever_the_budget(mt, v2_admission):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["maintenance_policy"] == cm.CRYPTO_MAINTENANCE_V2.record()
    guard = StubGuard(jb.EXHAUSTED)
    kit = maintainer(mt, guard=guard)
    base = bar_start(mt)
    for minute in (1, 2, 3):
        [_] = at(mt, kit, base, 60 * minute + 1)
    assert [r["trigger"]["reasons"] for r in requests(engine, sid)] == [["BAR_1M"]] * 3
    assert all("spend_guard" not in r["trigger"] and "spend_guard" not in r["context"]["identity"]
               for r in requests(engine, sid))
    assert guard.asked == 0  # Only V3 trades ask the guard.


def test_v2_and_v3_trades_in_one_pass_follow_their_own_versions(mt, monkeypatch):
    from tests.test_system_check import at_price

    engine, venue, _ = mt
    fresh_bar(mt)
    selected = publish(mt, ["FAR/USD", "NEAR/USD"])
    live = at_price("100.49", "100.51", at=venue.now)
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V2)
    v2 = engine.admit(selected["FAR/USD"], live_quote=live)
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V3)
    v3 = engine.admit(selected["NEAR/USD"], live_quote=live)
    for sid, symbol in ((v2, "FAR/USD"), (v3, "NEAR/USD")):
        trigger(mt, sid)
        order = entry_order(mt, symbol)
        fill(mt, order, order["qty"])
        engine.manage(sid, quote(mt, "100.20"))
        engine.manage(sid, quote(mt, "100.20"))
    guard = StubGuard(jb.THROTTLED)
    kit = maintainer(mt, guard=guard)
    base = bar_start(mt)
    for seconds in (61, 121, 181, 241, 301):
        venue.now = base + timedelta(seconds=seconds)
        kit.prices.set("FAR/USD", "101")
        kit.prices.set("NEAR/USD", "101")
        run_pass(mt, kit)
    assert [r["trigger"]["reasons"] for r in requests(engine, v2)] == [["BAR_1M"]] * 5
    assert [r["trigger"]["reasons"] for r in requests(engine, v3)] == [["BAR_5M"]]
    assert guard.asked == 5  # Once per pass, for the V3 trade.


# --- The real guard, end to end -------------------------------------------------------------------


def test_the_real_guard_exhausted_by_real_spend_stops_v3_reviews_and_alerts(mt):
    engine, venue, store = mt
    sid = open_trade(mt)
    dollar = jb.SpendConfig(D("1"), D("3"), D("3"))  # One dollar per million request bytes.
    guard = spend_guard(mt, dollar, refresh_seconds=0)
    kit = maintainer(mt, guard=guard)
    base = bar_start(mt)
    [_] = at(mt, kit, base, 61)  # Cents of spend: NORMAL, reviewed every minute.
    assert guard.evaluate(venue.now).tier == jb.NORMAL
    ledger = type("Ledger", (), {"jev": store})()
    call(ledger, venue.now, 980_000)  # 98% of the budget spent: the reserve is reached.
    assert at(mt, kit, base, 121) == []
    decision = guard.evaluate(venue.now)
    assert (decision.tier, decision.rule) == (jb.EXHAUSTED, "MONTH_TO_DATE_AT_RESERVE")
    tiers = [b["tier"] for b in bodies(engine, jb.TIER_EVENT)]
    assert tiers == ["NORMAL", "EXHAUSTED"]
    [skipped] = bodies(engine, "POSITION_REVIEW_SKIPPED", sid)
    assert skipped["spend_guard"]["tier_event_seq"] == decision.tier_event_seq
    assert len(kit.jev.calls) == 1
    # The status the owner reads, and the watchdog's alert for the change.
    status = guard.status(venue.now)
    assert (status["tier"], status["budget_usd"], status["routine_review_seconds"]) == (
        "EXHAUSTED", "1", None)
    assert D(status["month_to_date_usd"]) >= D("0.98")
    alarms = status_alarms({"jev_budget": status}, venue.now, WATCHDOG)
    assert "JEV_BUDGET_EXHAUSTED" in alarms
    later = status_alarms({"jev_budget": status}, venue.now + timedelta(minutes=31), WATCHDOG)
    assert "JEV_BUDGET_EXHAUSTED" not in later
    assert verify_events(engine.repo.export_events())["valid"]


def test_throttled_reviews_feed_the_meter_their_per_minute_weight(mt):
    engine, venue, store = mt
    sid = open_trade(mt)
    kit = maintainer(mt, guard=StubGuard(jb.THROTTLED))
    base = bar_start(mt)
    reviewed = at(mt, kit, base, 301) + at(mt, kit, base, 400, bid="106") + at(
        mt, kit, base, 601)
    assert len(reviewed) == 3
    with store.connect() as conn:
        sizes = [conn.execute("SELECT octet_length(request_json) AS n FROM lab.jev_requests "
                              "WHERE request_id=%s", (r,)).fetchone()["n"] for r in reviewed]
    meter = jb.SpendMeter(engine.repo)
    meter.refresh(venue.now)
    snap = meter.snapshot(venue.now, jb.SpendConfig(D("50")))
    assert snap.month_by_kind["MAINTENANCE"] == (3, sum(sizes))
    # The pick's own selection review is in the window too, at weight 1.
    other = sum(size for kind, (_, size) in snap.month_by_kind.items() if kind != "MAINTENANCE")
    assert other > 0 and snap.window_request_bytes == sum(sizes) + other
    assert snap.window_weighted_request_bytes == 5 * sizes[0] + sizes[1] + 5 * sizes[2] + other
    assert [r["trigger"]["reasons"] for r in requests(engine, sid)] == [
        ["BAR_5M"], ["R_MILESTONE"], ["BAR_5M"]]


# --- Runtime status and heartbeat -----------------------------------------------------------------


class StatusGuard:
    def __init__(self, tier="NORMAL", fail=False):
        self.tier, self.fail, self.month_to_date = tier, fail, "1.2345"

    def status(self, now):
        if self.fail:
            raise RuntimeError("fixture")
        return {"version": jb.VERSION, "available": True, "tier": self.tier,
                "tier_since": now.isoformat(), "month_to_date_usd": self.month_to_date,
                "budget_usd": "50"}


def test_the_runtime_status_carries_the_budget_and_the_heartbeat_ignores_spend():
    from tests.test_managed_runtime import runtime

    guard = StatusGuard()
    run = runtime(spend_guard=guard)
    status = run.status()
    assert status["jev_budget"]["tier"] == "NORMAL"
    signature = ManagedRuntime._heartbeat_signature(status)
    guard.month_to_date = "2.0000"  # Spend moves with every call: not a status change.
    assert ManagedRuntime._heartbeat_signature(run.status()) == signature
    guard.tier = "THROTTLED"  # A tier change is.
    assert ManagedRuntime._heartbeat_signature(run.status()) != signature
    guard.fail = True
    assert run.status()["jev_budget"] == {"version": "JEV_SPEND_GUARD_V1", "available": False,
                                          "tier": "UNAVAILABLE"}
    assert runtime().status()["jev_budget"] is None  # No guard configured.


def test_the_owner_status_commands_show_spend_projection_budget_and_tier(capsys):
    """``GET /api/v1/lab/status`` and ``cloud_runtime status`` carry the guard's section."""
    import json

    from catalyst_lab import cloud_runtime
    from catalyst_lab.managed_service import STATUS_FIELDS
    from tests.test_cloud_config import SECRET_VALUES
    from tests.test_cloud_runtime import NOW, StatusServer, healthy_status

    section = {"version": "JEV_SPEND_GUARD_V1", "available": True, "tier": "THROTTLED",
               "tier_since": NOW.isoformat(), "budget_usd": "50", "month": "2026-09",
               "month_to_date_usd": "31.2500", "projection_usd": "44.1000",
               "projection_per_minute_usd": "52.7000", "routine_review_seconds": 300}
    assert "jev_budget" in STATUS_FIELDS
    server = StatusServer({**healthy_status(NOW), "jev_budget": section})
    env = {"MANAGED_STATUS_API": "http://127.0.0.1:8780",
           "MANAGED_STATUS_TOKEN": SECRET_VALUES["MANAGED_STATUS_TOKEN"]}
    assert cloud_runtime.status(env, transport=server.transport) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["jev_budget"] == section
