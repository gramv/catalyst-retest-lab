"""Package exec-d flows: ``CRYPTO_STOP_EXECUTION_V1`` and ``CRYPTO_MAKER_ENTRY_V1`` through the
engine on per-test disposable PostgreSQL, the fake paper venue (``ManagedVenue`` behind the real
``ManagedPaperBroker`` and its database-backed one-use gate), the real ``CoinbaseFeed`` fed
Coinbase-shaped messages at the test clock, and a mock Jev transport.

Fixture evidence only: no broker, provider, network or owner-ledger contact. The setups are the
report-V3 fixture pick (entry trigger 100, max entry 100.10, stop 95, target 111).
"""

from datetime import timedelta
from decimal import Decimal as D

from catalyst_lab import execution_quality as eq
from catalyst_lab import maker_entry as me
from catalyst_lab import stop_breach
from catalyst_lab import stop_execution as se
from catalyst_lab.audit import verify_events
from catalyst_lab.execution_setting import CryptoExecutionSetting
from tests.test_coinbase_reference import coinbase as coinbase  # noqa: F401
from tests.test_coinbase_reference import market_sells, opened_v3, tick
from tests.test_crypto_trigger import entry_decision, rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation
from tests.test_system_check import at_price, publish_v3, two_slots, v3_pick

STOP_EXEC = CryptoExecutionSetting(stop_execution=True)
MAKER = CryptoExecutionSetting(maker_entry=True)


def opened(mx, coinbase, *, partial=None):
    """A report-V3 Coinbase pick admitted under CRYPTO_STOP_EXECUTION_V1, entered, filled (or
    ``partial`` filled) and protected by its native stop-limit."""
    engine, venue, _ = mx
    engine.crypto_execution = STOP_EXEC
    if partial is None:
        sid, stop = opened_v3(mx, coinbase)
        assert se.active(state(engine, sid))
        return sid, stop
    from catalyst_lab import coinbase_feed as cf
    from tests.test_coinbase_reference import reference_observation

    coinbase.connect(cf.product_id("BTC/USD"))
    [sid] = v3_setups(mx, ["BTC/USD"]).values()
    coinbase.trade("BTC-USD", "99.98")
    assert engine.observe_trigger(sid, reference_observation(
        mx, coinbase, sid, quote=("99.99", "100.01")))["outcome"] == "APPROVED"
    [entry] = venue.orders_of("buy")
    assert engine.ingest(venue.fill(entry["id"], partial))
    return sid, entry


def mutations(venue):
    return [c for c in venue.calls if c[0] in {"POST", "DELETE", "PATCH"}]


def assert_every_mutation_authorized(engine, venue):
    """Every POST/DELETE/PATCH the venue saw crossed the gate with a claimed, approved, one-use
    decision of at most five seconds."""
    with engine.repo.connect() as conn:
        claimed = conn.execute(
            """SELECT d.method, d.outcome, d.expires_at - d.created_at AS ttl
            FROM lab.managed_claims c JOIN lab.managed_risk_decisions d USING(decision_id)"""
        ).fetchall()
    assert len(claimed) == len(mutations(venue))
    assert all(r["outcome"] == "APPROVED" and r["ttl"] <= timedelta(seconds=5) for r in claimed)
    assert sorted(r["method"] for r in claimed) == sorted(c[0] for c in mutations(venue))


def collars(venue):
    return [o for o in venue.orders_of("sell", "limit") if o.get("time_in_force") == "ioc"]


def breach_by_two_prints(mx, coinbase, sid, *, low="94.90"):
    engine, venue, _ = mx
    venue.now += timedelta(seconds=1)
    coinbase.beat()
    coinbase.trade("BTC-USD", "94.98")
    coinbase.trade("BTC-USD", low)
    return engine.manage(sid, observation(mx, trade_price="95.20", bid="95.10", ask="95.15"))


def decisions(engine, sid, action=None):
    with engine.repo.connect() as conn:
        found = conn.execute(
            "SELECT * FROM lab.managed_risk_decisions WHERE setup_id=%s ORDER BY event_seq",
            (sid,)).fetchall()
    return [d for d in found if action is None or d["action"] == action]


# --- CRYPTO_STOP_EXECUTION_V1 ---------------------------------------------------------------------

def test_bid_noise_and_a_single_print_never_breach_then_two_prints_exit_through_the_collar(
        mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened(mx, coinbase)
    current = state(engine, sid)
    assert current[se.FIELD] == se.CRYPTO_STOP_EXECUTION.record()
    assert current["stop_breach_version"] == "CRYPTO_STOP_BREACH_V3"  # Detection fallback.
    # Alpaca's bid at and under the stop for 20 s while Coinbase trades above: nothing.
    for second in range(20):
        venue.now += timedelta(seconds=1)
        coinbase.beat()
        coinbase.trade("BTC-USD", "99.40")
        plan = engine.manage(sid, observation(mx, trade_price="94.80",
                                              bid="94.90" if second % 2 else "95", ask="99.6"))
    assert plan.state == "PROTECTED" and stop["status"] == "new"
    # One Coinbase print at the stop, then a print above it: a whipsaw, not a breach.
    tick(mx, coinbase, sid, 1)
    coinbase.trade("BTC-USD", "95")
    tick(mx, coinbase, sid, 0, bid="95.30", ask="95.40")
    coinbase.trade("BTC-USD", "95.40")
    for _ in range(5):
        plan = tick(mx, coinbase, sid, 1, bid="95.30", ask="95.40")
    assert plan.state == "PROTECTED" and state(engine, sid).get("stop_breached_at") is None
    assert not rows(engine, stop_breach.ESTABLISHED_EVENT, sid)
    # Two Coinbase prints at or below the stop: confirmed at once; the app requests its exit and
    # cancels the native stop-limit (it reserves the inventory) under its own authorization.
    plan = breach_by_two_prints(mx, coinbase, sid)
    breached = state(engine, sid)
    assert breached["exit_requested"] == se.EXIT_REASON
    evidence = breached["stop_breach_evidence"]
    assert (evidence["version"], evidence["confirmation"], evidence["print_count"],
            evidence["fallback"]) == (se.VERSION, se.PRINTS, 2, False)
    assert evidence["alpaca"]["bid"] == "95.10"  # Alpaca's book was still above the stop.
    [event] = rows(engine, stop_breach.ESTABLISHED_EVENT, sid)
    assert event["body"] == evidence
    assert (plan.state, plan.reason) == ("CANCELING", "AUTHORIZED_EXIT")
    assert stop["status"] == "canceled" and not collars(venue) and not market_sells(venue)
    # Next pass: the collar, an IOC limit 1% under the latest Coinbase print (94.90 -> 93.95).
    plan = tick(mx, coinbase, sid, 1, bid="94.80", ask="94.95")
    assert (plan.state, plan.reason) == ("EXIT_REQUIRED", "AUTHORIZED_EXIT")
    [collar] = collars(venue)
    assert (collar["limit_price"], collar["qty"]) == ("93.95", stop["qty"])
    recorded = state(engine, sid)[se.COLLAR_FIELD]
    assert (recorded["status"], recorded["limit_price"], recorded["reference_price"],
            recorded["client_order_id"]) == ("PLANNED", "93.95", "94.90",
                                             collar["client_order_id"])
    [collar_event] = rows(engine, se.COLLAR_EVENT, sid)
    assert collar_event["body"]["reference"]["healthy"] is True
    [exit_decision] = decisions(engine, sid, "EXIT")
    assert exit_decision["reason"] == se.COLLAR_REASON
    assert engine.ingest(venue.fill(collar["id"], collar["qty"], price="94.20"))
    tick(mx, coinbase, sid)
    closed = state(engine, sid)
    assert (closed["state"], closed["reason"]) == ("CLOSED", se.EXIT_REASON)
    [result] = rows(engine, se.RESULT_EVENT, sid)
    body = result["body"]
    assert body["exit_path"] == eq.COLLAR_IOC and body["stop_exit"] is True
    assert D(body["stop_slippage_fraction"]) == ((D(95) - D("94.20")) / 95).quantize(eq.SIX)
    assert D(body["reference_slippage_fraction"]) == (
        (D("94.90") - D("94.20")) / D("94.90")).quantize(eq.SIX)
    assert body["breach"]["confirmation"] == se.PRINTS
    assert not market_sells(venue)
    assert_every_mutation_authorized(engine, venue)
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_thin_coin_breaches_on_a_three_second_dwell(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened(mx, coinbase)
    tick(mx, coinbase, sid, 1)
    coinbase.trade("BTC-USD", "94.95")
    for _ in range(2):
        tick(mx, coinbase, sid, 1, bid="95.20", ask="95.30")
    assert state(engine, sid).get("stop_breached_at") is None and stop["status"] == "new"
    tick(mx, coinbase, sid, 1, bid="95.20", ask="95.30")  # 3 s, no print above.
    evidence = state(engine, sid)["stop_breach_evidence"]
    assert (evidence["confirmation"], evidence["confirm_latency_seconds"]) == (se.DWELL, "3.0")
    assert stop["status"] == "canceled"


def test_an_unfilled_collar_falls_back_to_market(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened(mx, coinbase)
    breach_by_two_prints(mx, coinbase, sid)
    tick(mx, coinbase, sid, 1)
    [collar] = collars(venue)
    collar["status"] = "canceled"  # IOC: nothing at or above the collar, the venue cancels it.
    plan = tick(mx, coinbase, sid, 1)
    assert plan.state == "EXIT_REQUIRED"
    [market] = market_sells(venue)
    assert market["qty"] == stop["qty"] and len(collars(venue)) == 1  # One collar, never two.
    assert engine.ingest(venue.fill(market["id"], market["qty"], price="93.10"))
    tick(mx, coinbase, sid)
    [result] = rows(engine, se.RESULT_EVENT, sid)
    assert result["body"]["exit_path"] == eq.MARKET
    assert_every_mutation_authorized(engine, venue)


def test_a_partly_filled_collar_sells_the_rest_at_market(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened(mx, coinbase)
    breach_by_two_prints(mx, coinbase, sid)
    tick(mx, coinbase, sid, 1)
    [collar] = collars(venue)
    half = (D(collar["qty"]) / 2).quantize(D("0.0001"))
    assert engine.ingest(venue.fill(collar["id"], str(half), price="94.10"))
    collar["status"] = "canceled"  # IOC: the rest is cancelled.
    tick(mx, coinbase, sid, 1)
    [market] = market_sells(venue)
    assert D(market["qty"]) == D(collar["qty"]) - half
    assert engine.ingest(venue.fill(market["id"], market["qty"], price="93.50"))
    tick(mx, coinbase, sid)
    [result] = rows(engine, se.RESULT_EVENT, sid)
    assert result["body"]["exit_path"] == "COLLAR_IOC+MARKET"


def test_a_collar_still_working_after_its_wait_is_cancelled_then_market(mx, coinbase):
    engine, venue, _ = mx
    sid, _ = opened(mx, coinbase)
    breach_by_two_prints(mx, coinbase, sid)
    tick(mx, coinbase, sid, 1)
    [collar] = collars(venue)
    assert tick(mx, coinbase, sid, 1).state == "EXIT_WORKING"  # 1 s: it may still fill.
    plan = tick(mx, coinbase, sid, 2)  # 3 s after it was planned.
    assert (plan.state, plan.reason) == ("CANCELING", se.COLLAR_CANCEL_REASON)
    assert collar["status"] == "canceled" and not market_sells(venue)
    tick(mx, coinbase, sid, 1)
    [market] = market_sells(venue)
    assert market["qty"] == collar["qty"]
    cancels = [d for d in decisions(engine, sid, "CANCEL") if d["reason"] ==
               se.COLLAR_CANCEL_REASON]
    assert len(cancels) == 1
    assert_every_mutation_authorized(engine, venue)


def test_a_stale_reference_falls_back_to_the_native_stop_limit_then_market(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened(mx, coinbase)
    coinbase.disconnect()
    tick(mx, coinbase, sid, 1, beat=False, trade_price="94.95", bid="95.10", ask="95.20")
    current = state(engine, sid)
    evidence = current["stop_breach_evidence"]
    assert (evidence["fallback"], evidence["breach_evidence"], evidence["exit_path"]) == (
        True, "TRADE_PRINT", se.EXIT_PATH_FALLBACK)
    assert current.get("exit_requested") is None and stop["status"] == "new"
    plan = tick(mx, coinbase, sid, 4, beat=False, bid="95.10", ask="95.20")
    assert plan.state == "PROTECTED"  # The native stop-limit works its 5 s.
    plan = tick(mx, coinbase, sid, 1, beat=False, bid="95.10", ask="95.20")
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_LIMIT_NOT_FILLED")
    tick(mx, coinbase, sid, 0, beat=False, bid="95.10", ask="95.20")
    assert len(market_sells(venue)) == 1 and not collars(venue)
    assert state(engine, sid).get(se.COLLAR_FIELD) is None


def test_a_reference_gone_stale_before_the_exit_sells_at_market_without_a_collar(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened(mx, coinbase)
    breach_by_two_prints(mx, coinbase, sid)
    assert stop["status"] == "canceled"
    plan = tick(mx, coinbase, sid, 4, beat=False)  # Coinbase silent: no collar off it.
    assert plan.state == "EXIT_REQUIRED" and not collars(venue)
    assert len(market_sells(venue)) == 1
    skipped = state(engine, sid)[se.COLLAR_FIELD]
    assert (skipped["status"], skipped["reason"]) == ("SKIPPED", se.REFERENCE_UNHEALTHY_AT_EXIT)
    [event] = rows(engine, se.COLLAR_EVENT, sid)
    assert event["body"]["reference"]["healthy"] is False


def test_no_sell_is_sent_while_the_entry_buy_still_works(mx, coinbase):
    """Alpaca refuses a sell while a buy of the coin works (wash-trade guard): with a partly
    filled entry still working (or its cancel in flight) the emulated exit cancels it first and
    sends the collar only once the broker shows it closed."""
    engine, venue, _ = mx
    sid, entry = opened(mx, coinbase, partial="0.5")
    venue.defer_cancel = True  # Cancels stay pending until the test closes them.
    tick(mx, coinbase, sid, 1)
    breach_by_two_prints(mx, coinbase, sid)
    assert state(engine, sid)["exit_requested"] == se.EXIT_REASON
    for _ in range(3):
        tick(mx, coinbase, sid, 1)
        assert entry["status"] == "pending_cancel"
        assert not collars(venue) and not market_sells(venue)
    venue.defer_cancel = False
    for order in venue.orders.values():
        if order["status"] == "pending_cancel":
            order["status"] = "canceled"
    tick(mx, coinbase, sid, 1)
    [collar] = collars(venue)
    assert D(collar["qty"]) == D("0.5")
    assert_every_mutation_authorized(engine, venue)


def test_setups_admitted_without_the_switch_keep_v3_exactly(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened_v3(mx, coinbase)  # The switch is off (default).
    current = state(engine, sid)
    assert se.FIELD not in current and me.FIELD not in current
    tick(mx, coinbase, sid, 1)
    coinbase.trade("BTC-USD", "95")  # One print: V3's breach, and the native stop-limit's 5 s.
    tick(mx, coinbase, sid, 0, bid="95.20", ask="95.25")
    current = state(engine, sid)
    assert current["stop_breach_evidence"]["version"] == "CRYPTO_STOP_BREACH_V3"
    assert current.get("exit_requested") is None and stop["status"] == "new"
    tick(mx, coinbase, sid, 5, bid="95.20", ask="95.25")
    tick(mx, coinbase, sid, 0, bid="95.20", ask="95.25")
    assert len(market_sells(venue)) == 1 and not collars(venue)


# --- CRYPTO_MAKER_ENTRY_V1 ------------------------------------------------------------------------

def maker_setups(mx, symbols, *, live=("100.49", "100.51"), agent="100.50"):
    engine, venue, _ = mx
    engine.crypto_execution = MAKER
    old, _ = two_slots(venue.now)
    selected = publish_v3(mx, [v3_pick(i, s, venue.now, agent=agent)
                               for i, s in enumerate(symbols)], run_slot=old)
    quote = at_price(*live, at=venue.now)
    return {s: engine.admit(selected[s], live_quote=quote) for s in symbols}


def test_a_maker_entry_rests_at_the_bid_with_the_same_size_and_reservation(mx):
    engine, venue, _ = mx
    setups = maker_setups(mx, ["AAA/USD"])
    sid = setups["AAA/USD"]
    current = state(engine, sid)
    assert current["entry_type"] == "PULLBACK"
    assert current[me.FIELD] == me.CRYPTO_MAKER_ENTRY.record()
    decision = engine.observe_trigger(sid, observation(mx, trade_price="100", bid="99.95",
                                                       ask="100.02"))
    assert decision["outcome"] == "APPROVED"
    [entry] = venue.orders_of("buy")
    assert (entry["type"], entry["time_in_force"], entry["limit_price"]) == (
        "limit", "gtc", "99.95")
    assert "post_only" not in entry  # Not sent: unverified on Alpaca crypto.
    context = decision["context"]["entry_execution"]
    assert (context["basis"], context["bid"], context["max_entry_price"]) == (
        me.BASIS_BID, "99.95", "100.10")
    with engine.repo.connect() as conn:
        reservation = conn.execute(
            "SELECT * FROM lab.managed_active_reservations WHERE setup_id=%s", (sid,)).fetchone()
    # Sized and reserved on max entry M exactly as before: the lower price lowers real risk.
    assert reservation["max_entry"] == D("100.10") and reservation["qty"] == D(entry["qty"])
    assert reservation["planned_risk"] == D(entry["qty"]) * (D("100.10") - D("95"))
    assert_every_mutation_authorized(engine, venue)


def test_a_bid_above_the_trigger_rests_at_the_trigger(mx):
    engine, venue, _ = mx
    sid = maker_setups(mx, ["AAA/USD"])["AAA/USD"]
    decision = engine.observe_trigger(sid, observation(mx, trade_price="100", bid="100.05",
                                                       ask="100.08"))
    assert decision["outcome"] == "APPROVED"
    assert venue.orders_of("buy")[0]["limit_price"] == "100"
    assert decision["context"]["entry_execution"]["basis"] == me.BASIS_TRIGGER


def test_maker_entries_are_for_pullbacks_and_the_switch_only(mx):
    engine, venue, _ = mx
    sid = maker_setups(mx, ["AAA/USD"], live=("99.99", "100.01"), agent="100.00")["AAA/USD"]
    current = state(engine, sid)
    assert current["entry_type"] == "IMMEDIATE" and me.FIELD not in current
    engine.crypto_execution = CryptoExecutionSetting()
    [off] = v3_setups(mx, ["BBB/USD"]).values()
    assert me.FIELD not in state(engine, off)
    decision = engine.observe_trigger(off, observation(mx, trade_price="100", bid="99.95",
                                                       ask="100.02"))
    [entry] = [o for o in venue.orders_of("buy") if o["symbol"] == "BBB/USD"]
    assert entry["limit_price"] == "100.10" and "entry_execution" not in decision["context"]


def test_entry_pacing_holds_the_maker_entry_then_prices_it_at_placement(mx):
    from tests.test_regime_gate import paced, refeed, waits

    engine, venue, _ = mx
    pacing = paced(mx, moves=("-0.03", "-0.03", "-0.03"))  # A falling market: entries wait.
    sid = maker_setups(mx, ["AAA/USD"])["AAA/USD"]
    assert engine.observe_trigger(sid, observation(mx, trade_price="100", bid="99.95",
                                                   ask="100.02")) is None
    assert len(waits(engine, sid)) == 1 and not venue.orders and decisions(engine, sid) == []
    venue.now += timedelta(minutes=2)
    refeed(mx, pacing)
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]
    decision = engine.observe_trigger(sid, observation(mx, trade_price="99.98", bid="99.90",
                                                       ask="99.97"))
    assert decision["outcome"] == "APPROVED"
    assert venue.orders_of("buy")[0]["limit_price"] == "99.90"  # The bid when placed.
    assert decision["context"]["entry_execution"]["bid"] == "99.90"


def test_a_partly_filled_maker_entry_follows_the_partial_and_window_rules(mx):
    engine, venue, _ = mx
    sid = maker_setups(mx, ["AAA/USD"])["AAA/USD"]
    engine.observe_trigger(sid, observation(mx, trade_price="100", bid="99.95", ask="100.02"))
    [entry] = venue.orders_of("buy")
    half = (D(entry["qty"]) / 2).quantize(D("0.0001"))
    assert engine.ingest(venue.fill(entry["id"], str(half), price="99.95"))
    for _ in range(3):
        engine.manage(sid, observation(mx))
        venue.now += timedelta(seconds=1)
    # The filled part is protected; the rest never works beyond the 300 s window.
    venue.now += timedelta(seconds=300)
    for _ in range(3):
        engine.manage(sid, observation(mx))
        venue.now += timedelta(seconds=1)
    [stop] = [o for o in venue.orders_of("sell", "stop_limit") if o["status"] == "new"]
    assert D(stop["qty"]) == half and entry["status"] == "canceled"
    assert_every_mutation_authorized(engine, venue)


def test_the_maker_fill_fee_measures_maker_liquidity(mx):
    engine, venue, _ = mx
    sid = maker_setups(mx, ["AAA/USD"])["AAA/USD"]
    engine.observe_trigger(sid, observation(mx, trade_price="100", bid="99.95", ask="100.02"))
    [entry] = venue.orders_of("buy")
    update = venue.fill(entry["id"], entry["qty"], price="99.95")
    notional = D(entry["qty"]) * D("99.95")
    update.update(fee=str((notional * D("0.0015")).quantize(D("0.000001"))),
                  fee_currency="USD")
    assert engine.ingest(update)
    with engine.repo.connect() as conn:
        quality = eq.trade_execution(conn, sid, state(engine, sid))
    assert quality["entry_liquidity"] == eq.MAKER
    assert quality["entry_fee_rate"] == D("0.0015")
    assert quality["versions"]["entry_execution"] == me.VERSION


def test_older_entries_still_send_max_entry(mx):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    engine.observe_trigger(sid, observation(mx, trade_price="100", bid="99.95", ask="100.02"))
    assert venue.orders_of("buy")[0]["limit_price"] == "100.10"
    [decision], _ = entry_decision(engine, sid)
    assert "entry_execution" not in decision["context"]


# --- The switch in configuration ------------------------------------------------------------

def test_the_deploy_example_keeps_both_versions_off_and_preflight_checks_the_switch():
    import json
    from pathlib import Path

    from catalyst_lab import execution_setting
    from catalyst_lab.managed_ops import OPTIONAL_ENV, environment_findings

    root = Path(__file__).resolve().parents[1]
    env = json.loads((root / "deploy/private-paper.example.json").read_text())["environment"]
    assert execution_setting.from_env(env) == execution_setting.OFF
    assert execution_setting.ENV in OPTIONAL_ENV
    _, invalid = environment_findings({**env, execution_setting.ENV: '{"stop_execution": 1}'})
    assert execution_setting.ENV in invalid
    switched = '{"stop_execution": "CRYPTO_STOP_EXECUTION_V1", "maker_entry": null}'
    _, invalid = environment_findings({**env, execution_setting.ENV: switched})
    assert execution_setting.ENV not in invalid
    assert execution_setting.from_env({execution_setting.ENV: switched}) == STOP_EXEC


def test_an_engine_refuses_an_unchecked_switch(mx):
    import pytest

    from catalyst_lab.managed_execution import ManagedExecution

    engine, _, reviews = mx
    with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_EXECUTION_SETTING_REQUIRED"):
        ManagedExecution(engine.repo, engine.broker, policy=engine.policy, clock=engine.now,
                         review_store=reviews, crypto_execution={"stop_execution": True})


def test_a_recorded_collar_never_sent_is_offered_again_with_its_id_then_market(mx, coinbase,
                                                                                monkeypatch):
    engine, venue, _ = mx
    sid, _ = opened(mx, coinbase)
    breach_by_two_prints(mx, coinbase, sid)
    original = engine._authorize_mutation
    skipped = []

    def drop_exit(setup_id, action, method, path, payload, *, reason):
        if action == "EXIT" and len(skipped) < 2:
            skipped.append(payload["client_order_id"])
            return None  # As when the setup moved under the lock: nothing authorized or sent.
        return original(setup_id, action, method, path, payload, reason=reason)

    monkeypatch.setattr(engine, "_authorize_mutation", drop_exit)
    tick(mx, coinbase, sid, 1)
    recorded = state(engine, sid)[se.COLLAR_FIELD]
    assert recorded["status"] == "PLANNED" and not collars(venue)
    tick(mx, coinbase, sid, 1)  # Offered again: the same ID and price.
    assert skipped == [recorded["client_order_id"]] * 2
    assert state(engine, sid)[se.COLLAR_FIELD] == recorded
    tick(mx, coinbase, sid, 2)  # Its wait has ended unsent: the market sell, no collar.
    assert not collars(venue) and len(market_sells(venue)) == 1
    assert_every_mutation_authorized(engine, venue)
