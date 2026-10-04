"""Package exec-d (plan phase D): ``CRYPTO_STOP_EXECUTION_V1``, ``CRYPTO_MAKER_ENTRY_V1``, their
switch and ``EXECUTION_QUALITY_V1`` as pure rules. No database, broker or network: fixture
evidence only.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import coinbase_feed as cf
from catalyst_lab import execution_quality as eq
from catalyst_lab import execution_setting as es
from catalyst_lab import maker_entry as me
from catalyst_lab import stop_execution as se
from catalyst_lab.crypto_execution import (
    CryptoAsset,
    CryptoOrder,
    CryptoProtectionPolicy,
    CryptoSnapshot,
    build_collared_exit,
    collar_price,
    plan_crypto_recovery,
)
from catalyst_lab.result_dimensions import dimensions

T0 = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)
ASSET = CryptoAsset("BTC/USD", "0.0001", "0.0001", "0.01")
STOP = D("95")


def printed(price, seconds_ago, trade_id):
    traded = T0 - timedelta(seconds=seconds_ago)
    return cf.ReferencePrint(D(str(price)), traded, traded, str(trade_id))


def view(*prints, healthy=True, code=None):
    ordered = tuple(sorted(prints, key=lambda p: p.traded_at))
    return cf.ReferenceView("BTC-USD", T0, healthy, code, connected=healthy,
                            acknowledged=healthy, heartbeat_at=T0, heartbeat_received_at=T0,
                            bid=D("94.9"), ask=D("95.1"), quote_at=T0,
                            last_print=ordered[-1] if ordered else None, prints=ordered)


def marks(since_seconds_ago=20):
    return {"stop": "95", "stop_since": (T0 - timedelta(seconds=since_seconds_ago)).isoformat(),
            "bid_since": None, "bid": None, "bid_quote_at": None}


def state(**extra):
    return {"stop": "95", "lifecycle_id": "L", "state": "OPEN",
            "stop_breach_version": "CRYPTO_STOP_BREACH_V3",
            se.FIELD: se.CRYPTO_STOP_EXECUTION.record(), "stop_breach_marks": marks(), **extra}


# --- CRYPTO_STOP_EXECUTION_V1: the confirmation --------------------------------------------------

@pytest.mark.parametrize(("prints", "expected"), [
    ([], None),
    ([(95, 1, 1)], None),                                  # One print, 1 s: not yet.
    ([(95, 2.9, 1)], None),                                # One print, 2.9 s: not yet.
    ([(95, 3, 1)], se.DWELL),                              # Held 3 s without recovery.
    ([(95, 1, 1), (94.9, 0.5, 2)], se.PRINTS),             # Two trades at or below.
    ([(95, 1, 1), (95, 0.5, 1)], None),                    # One trade id twice is one trade.
    ([(95, 4, 1), (95.4, 2, 2)], None),                    # A print above: recovered.
    ([(95, 4, 1), (95.4, 3, 2), (94.9, 1, 3)], None),      # A new run of one, 1 s old.
    ([(95, 6, 1), (95.4, 5, 2), (94.9, 4, 3), (94.8, 0.2, 4)], se.PRINTS),
    ([(95, 9, 1), (94.9, 8, 2)], se.DWELL),                # Latest print over 5 s: dwell.
    ([(95, 11, 1)], None),                                 # Older than the 10 s window.
    ([(95.01, 4, 1), (95.02, 1, 2)], None),                # Above the stop.
])
def test_the_reference_run_confirms_by_two_prints_or_a_three_second_dwell(prints, expected):
    found = se.reference_confirmation(view(*(printed(*p) for p in prints)), marks(), STOP, T0)
    assert (found["confirmation"] if found else None) == expected
    if found:
        assert found["fallback"] is False and found["exit_path"] == se.EXIT_PATH_EMULATED


def test_prints_before_the_stop_was_first_measured_never_count():
    found = se.reference_confirmation(
        view(printed(95, 5, 1), printed(94.9, 4, 2)), marks(since_seconds_ago=3), STOP, T0)
    assert found is None


def test_the_confirmation_records_the_run_its_latency_and_the_last_print_above():
    found = se.reference_confirmation(
        view(printed(96, 6, 1), printed(95, 2, 2), printed("94.90", 1, 3)), marks(), STOP, T0)
    assert found["confirmation"] == se.PRINTS and found["print_count"] == 2
    assert [p["trade_id"] for p in found["prints"]] == ["2", "3"]
    assert found["evidence_price"] == "94.90" and found["confirm_latency_seconds"] == D("2.0")
    assert found["last_above"]["price"] == "96"


def test_evaluate_requests_the_apps_exit_and_alpacas_bid_alone_decides_nothing():
    current = state()
    changes, breach = se.evaluate(current, observation={"feed_healthy": True, "quote_at":
                                                        T0.isoformat()},
                                  bid=D("90"), reference=view(printed(96, 1, 1)), now=T0)
    assert breach is None and "exit_requested" not in changes
    changes, breach = se.evaluate(current, observation={"feed_healthy": True}, bid=D("95.2"),
                                  reference=view(printed(95, 1, 1), printed(94.8, 0.5, 2)),
                                  now=T0)
    assert breach["version"] == se.VERSION and breach["fallback"] is False
    assert changes["exit_requested"] == se.EXIT_REASON
    assert changes["stop_breached_at"] == T0.isoformat()
    assert breach["alpaca"]["bid"] == "95.2" and breach["reference"]["healthy"] is True
    assert breach["reference"]["last_print"]["price"] == "94.8"
    # Another exit already requested is never overwritten; an established breach is final.
    changes, _ = se.evaluate({**current, "exit_requested": "TARGET_EXIT"},
                             observation={}, bid=None,
                             reference=view(printed(95, 1, 1), printed(94.8, 0.5, 2)), now=T0)
    assert "exit_requested" not in changes
    assert se.evaluate({**current, "stop_breached_at": T0.isoformat()}, observation={},
                       bid=None, reference=view(printed(94, 4, 9)), now=T0) == ({}, None)


def test_an_unhealthy_reference_falls_back_to_v3s_alpaca_evidence_without_an_app_exit():
    current = state()
    unhealthy = view(healthy=False, code=cf.HEARTBEAT_STALE)
    observation = {"feed_healthy": True, "trade_price": "94.95", "trade_at": T0.isoformat(),
                   "quote_at": T0.isoformat()}
    changes, breach = se.evaluate(current, observation=observation, bid=D("95.1"),
                                  reference=unhealthy, now=T0)
    assert breach["fallback"] is True and breach["breach_evidence"] == "TRADE_PRINT"
    assert breach["exit_path"] == se.EXIT_PATH_FALLBACK and breach["fallback_seconds"] == 5
    assert "exit_requested" not in changes and not se.emulated({**current, **changes})
    # No feed at all: the same fail-safe.
    _, breach = se.evaluate(current, observation=observation, bid=D("95.1"), reference=None,
                            now=T0)
    assert breach["reference"]["code"] == cf.FEED_NOT_CONFIGURED and breach["fallback"] is True


def test_a_stop_change_discards_marks_and_breach():
    current = state(stop="96", stop_breached_at=T0.isoformat(),
                    stop_breach_evidence={"version": se.VERSION, "fallback": False})
    changes, breach = se.evaluate(current, observation={}, bid=None, reference=view(), now=T0)
    assert breach is None
    assert changes["stop_breach_marks"]["stop"] == "96"
    assert changes["stop_breached_at"] is None and changes["stop_breach_evidence"] is None


def test_the_policy_record_is_exact():
    record = se.CRYPTO_STOP_EXECUTION.record()
    assert record == {
        "policy_id": "CRYPTO_STOP_EXECUTION_V1", "confirm_prints": 2, "dwell_seconds": 3,
        "confirm_window_seconds": 10, "print_max_age_seconds": 5, "collar_fraction": "0.01",
        "collar_wait_seconds": 3, "time_in_force": "ioc",
        "detection": "COINBASE_PRINTS_CONFIRMED_ELSE_CRYPTO_STOP_BREACH_V3"}
    assert se.active({se.FIELD: record}) and not se.active({}) and not se.active(None)
    with pytest.raises(ValueError, match="EXPLICIT_STOP_EXECUTION_POLICY_REQUIRED"):
        se.recorded({se.FIELD: {**record, "collar_fraction": "0.05"}})
    with pytest.raises(ValueError, match="EXPLICIT_STOP_EXECUTION_POLICY_REQUIRED"):
        se.recorded({se.FIELD: {**record, "extra": 1}})
    assert se.admission_fields(True, True) == {se.FIELD: record}
    assert se.admission_fields(False, True) == se.admission_fields(True, False) == {}


def test_the_collar_reference_is_the_latest_coinbase_print_and_never_a_stale_feed():
    assert se.collar_reference(view(printed(95, 2, 1), printed("94.7", 1, 2)), {}, T0)[0] == (
        D("94.7"))
    assert se.collar_reference(view(), {"evidence_price": "94.9"}, T0)[0] == D("94.9")
    assert se.collar_reference(view(printed(95, 1, 1), healthy=False), {}, T0) is None
    assert se.collar_reference(None, {"evidence_price": "94.9"}, T0) is None


# --- The collared IOC and the planner -------------------------------------------------------------

def test_the_collar_is_one_percent_under_the_reference_floored_to_the_grid():
    assert collar_price(ASSET, D("94.90"), se.COLLAR_FRACTION) == D("93.95")  # 93.951
    assert collar_price(ASSET, D("0.01"), se.COLLAR_FRACTION) == D("0.01")  # Never below a tick.
    payload = build_collared_exit(ASSET, D("1.23456"), D("93.95"), operation_key="k")
    assert {k: payload[k] for k in ("side", "type", "time_in_force", "limit_price", "qty")} == {
        "side": "sell", "type": "limit", "time_in_force": "ioc", "limit_price": "93.95",
        "qty": "1.2345"}
    assert "stop_price" not in payload


def snapshot(orders=(), qty="2", available=None, revision="7"):
    reserved = sum((o.remaining for o in orders if o.role != "ENTRY"), D(0))
    return CryptoSnapshot(revision, T0, D(qty),
                          D(available) if available is not None else D(qty) - reserved,
                          tuple(orders), True, (), (), D("95.5"), T0, None)


POLICY = CryptoProtectionPolicy(D(5), D(5), D(5))


def plan(snap, **extra):
    return plan_crypto_recovery(
        ASSET, snap, POLICY, now=T0, operation_key="setup", stop=STOP, stop_limit=D("94.52"),
        target=D("111"), exit_requested=True, exit_deadline=T0 + timedelta(hours=1), **extra)


def order(role, status="new", oid=None, qty="2", filled="0", **prices):
    return CryptoOrder(oid or role.lower(), "cl-" + (oid or role.lower()), role, D(qty),
                       D(filled), status, True, **prices)


def test_the_planner_cancels_entries_and_protection_before_the_collar():
    entry = order("ENTRY", "partially_filled", qty="3", filled="2")
    protect = order("PROTECT", stop_price=D("95"), limit_price=D("94.52"))
    first = plan(snapshot([entry, protect], available="0"),
                 exit_limit=(D("93.95"), "collar-key", se.COLLAR_REASON))
    assert first.state == "CANCELING"
    assert {(p.method, p.path) for p in first.proposals} == {
        ("DELETE", "/v2/orders/entry"), ("DELETE", "/v2/orders/protect")}
    # A cancel in flight is waited for; no sell is proposed while the buy may still work.
    pending = plan(snapshot([order("ENTRY", "pending_cancel", qty="3", filled="2")]),
                   exit_limit=(D("93.95"), "collar-key", se.COLLAR_REASON))
    assert pending.state == "CANCELING" and pending.proposals == ()
    ready = plan(snapshot(), exit_limit=(D("93.95"), "collar-key", se.COLLAR_REASON))
    assert ready.state == "EXIT_REQUIRED" and ready.reason == "AUTHORIZED_EXIT"
    [collar] = ready.proposals
    assert (collar.action, collar.method, collar.reason) == (
        "CRYPTO_EXIT", "POST", se.COLLAR_REASON)
    assert (collar.payload["type"], collar.payload["time_in_force"]) == ("limit", "ioc")
    assert collar.payload["limit_price"] == "93.95" and collar.payload["qty"] == "2"


def test_the_planner_waits_on_or_cancels_a_working_collar_then_sells_at_market():
    working = order("EXIT", oid="collar", limit_price=D("93.95"))
    assert plan(snapshot([working])).state == "EXIT_WORKING"
    cancel = plan(snapshot([working]), cancel_exit=se.COLLAR_CANCEL_REASON)
    assert (cancel.state, cancel.reason) == ("CANCELING", se.COLLAR_CANCEL_REASON)
    assert [(p.method, p.path) for p in cancel.proposals] == [("DELETE", "/v2/orders/collar")]
    after = plan(snapshot([order("EXIT", "canceled", oid="collar", limit_price=D("93.95"))],
                          qty="1.5"))
    [market] = after.proposals
    assert (market.payload["type"], market.payload["time_in_force"], market.payload["qty"]) == (
        "market", "gtc", "1.5")


def test_the_planners_defaults_are_unchanged():
    [market] = plan(snapshot()).proposals
    assert market.payload["type"] == "market" and market.reason == "AUTHORIZED_EXIT"


# --- CRYPTO_MAKER_ENTRY_V1 ------------------------------------------------------------------------

@pytest.mark.parametrize(("bid", "ask", "limit", "basis"), [
    ("99.95", "100.02", "99.95", me.BASIS_BID),        # At the bid: rests.
    ("99.957", "100.02", "99.95", me.BASIS_BID),       # Floored to the grid.
    ("100.05", "100.08", "100", me.BASIS_TRIGGER),     # The bid is above the trigger.
    ("100", "100.01", "100", me.BASIS_TRIGGER),
    (None, "100.01", "100", me.BASIS_NO_BID),
    ("100.02", "100.01", "100", me.BASIS_NO_BID),      # A crossed book is no bid.
    ("-1", "100.01", "100", me.BASIS_NO_BID),
])
def test_the_maker_entry_price(bid, ask, limit, basis):
    price, evidence = me.entry_limit(D("0.01"), entry_trigger=D("100"), max_entry=D("100.10"),
                                     observation={"bid": bid, "ask": ask})
    assert price == D(limit) and evidence["basis"] == basis
    assert evidence["post_only"] == me.POST_ONLY and price <= D("100.10")


def test_the_maker_price_never_exceeds_max_entry():
    price, _ = me.entry_limit(D("0.01"), entry_trigger=D("100.10"), max_entry=D("100.05"),
                              observation={})
    assert price == D("100.05")


def test_maker_admission_is_switched_and_for_crypto_pullbacks_only():
    crypto = {"market": "CRYPTO"}
    record = me.CRYPTO_MAKER_ENTRY.record()
    assert me.admission_fields(True, crypto, "PULLBACK") == {me.FIELD: record}
    assert me.admission_fields(False, crypto, "PULLBACK") == {}
    assert me.admission_fields(True, crypto, "IMMEDIATE") == {}
    assert me.admission_fields(True, {"market": "US_STOCKS"}, "PULLBACK") == {}
    assert me.active({me.FIELD: record}) and not me.active({})
    with pytest.raises(ValueError, match="EXPLICIT_MAKER_ENTRY_POLICY_REQUIRED"):
        me.recorded({me.FIELD: {**record, "maker_fee_rate": "0.001"}})


# --- The switch -----------------------------------------------------------------------------------

def test_the_switch_is_off_unless_named_exactly():
    assert es.from_env({}) == es.OFF and es.OFF.record() == {"stop_execution": None,
                                                             "maker_entry": None}
    both = es.parse('{"stop_execution": "CRYPTO_STOP_EXECUTION_V1", '
                    '"maker_entry": "CRYPTO_MAKER_ENTRY_V1"}')
    assert both == es.CryptoExecutionSetting(True, True)
    assert es.parse('{"stop_execution": null, "maker_entry": null}') == es.OFF
    for text in ('{}', '{"stop_execution": true, "maker_entry": null}',
                 '{"stop_execution": null}', '[]', 'nope',
                 '{"stop_execution": "CRYPTO_STOP_EXECUTION_V2", "maker_entry": null}',
                 '{"stop_execution": null, "maker_entry": null, "x": 1}'):
        with pytest.raises(ValueError, match=es.INVALID):
            es.parse(text)
    with pytest.raises(ValueError):
        es.CryptoExecutionSetting(1, False)


# --- EXECUTION_QUALITY_V1 ------------------------------------------------------------------------

def fill(fill_id, side, qty, price, order_id, seconds=0):
    return {"fill_id": fill_id, "side": side, "qty": D(qty), "price": D(price),
            "broker_order_id": order_id, "filled_at": T0 + timedelta(seconds=seconds),
            "event_seq": seconds}


def test_fee_rates_classify_maker_and_taker_and_the_exit_path_and_slippage():
    fills = [fill("b1", "buy", "1", "100", "entry"), fill("b2", "buy", "1", "100", "entry", 1),
             fill("s1", "sell", "1.2", "94.20", "collar", 5),
             fill("s2", "sell", "0.8", "93.70", "market", 6)]
    costs = {"b1": {"fee_usd": D("0.15")}, "b2": {"fee_usd": D("0.15")},
             "s1": {"fee_usd": D("0.282600")}, "s2": {"fee_usd": D("0.18740")}}
    kinds = {"collar": eq.COLLAR_IOC, "market": eq.MARKET}
    result = eq.summarize_fills(fills, costs, kinds, {"stop": "95",
                                                      "reason": se.EXIT_REASON,
                                                      se.FIELD: se.CRYPTO_STOP_EXECUTION.record()},
                                planned_filled_risk=D("10.2"))
    assert result["entry_fee_rate"] == D("0.0015") and result["entry_liquidity"] == eq.MAKER
    assert result["exit_path"] == "COLLAR_IOC+MARKET" and result["stop_exit"] is True
    exit_average = (D("1.2") * D("94.20") + D("0.8") * D("93.70")) / 2
    assert result["exit_average"] == exit_average
    assert result["stop_slippage_fraction"] == ((D(95) - exit_average) / 95).quantize(eq.SIX)
    assert result["stop_slippage_r"] == ((D(95) - exit_average) * 2 / D("10.2")).quantize(eq.SIX)
    assert result["versions"]["stop_execution"] == se.VERSION
    assert result["versions"]["entry_execution"] == eq.NONE


@pytest.mark.parametrize(("fees", "expected"), [
    (("0.25", "0.25"), eq.TAKER), (("0.15", "0.25"), eq.MIXED), (("0.20", "0.20"), eq.MAKER),
    ((None, "0.15"), eq.MIXED), ((None, None), eq.UNKNOWN)])
def test_entry_liquidity(fees, expected):
    fills = [fill("b1", "buy", "1", "100", "e"), fill("b2", "buy", "1", "100", "e", 1)]
    costs = {f"b{i + 1}": {"fee_usd": D(v) if v else None} for i, v in enumerate(fees)}
    result = eq.summarize_fills(fills, costs, {}, {"stop": "95", "reason": "TARGET_EXIT"})
    assert result["entry_liquidity"] == expected
    assert result["stop_exit"] is False and result["stop_slippage_fraction"] is None
    assert result["exit_path"] == eq.NONE


def test_order_kinds_from_decisions():
    assert eq.kind_of("PROTECT", {}) == eq.kind_of("AMEND", {}) == eq.NATIVE_STOP_LIMIT
    assert eq.kind_of("EXIT", {"type": "market"}) == eq.MARKET
    assert eq.kind_of("EXIT", {"type": "limit", "time_in_force": "ioc"}) == eq.COLLAR_IOC
    assert eq.kind_of("EXIT", {"type": "limit", "time_in_force": "gtc"}) == eq.LIMIT_EXIT
    assert eq.kind_of("ENTRY", {"type": "limit"}) == "UNKNOWN"


def test_the_scorecard_dimension_compares_execution_versions():
    def item(stop_exec, entry_exec, liquidity, path, slip, rate):
        return {"r_net": None, "win": None, "regime": None, "versions": {},
                "execution": {"versions": {"stop_execution": stop_exec,
                                           "entry_execution": entry_exec},
                              "entry_liquidity": liquidity, "exit_path": path,
                              "stop_exit": slip is not None, "stop_slippage_fraction": slip,
                              "stop_slippage_r": None, "entry_fee_rate": rate}}

    items = [item("NONE", "NONE", eq.TAKER, "MARKET", D("0.012"), D("0.0025")),
             item("NONE", "NONE", eq.TAKER, "NATIVE_STOP_LIMIT", D("0.004"), D("0.0025")),
             item(se.VERSION, me.VERSION, eq.MAKER, "COLLAR_IOC", D("0.006"), D("0.0015")),
             item(se.VERSION, me.VERSION, eq.UNKNOWN, "NONE", None, None),
             {"r_net": None, "win": None, "regime": None, "versions": {}}]  # Not measured.
    section = dimensions(items)["execution"]
    old = section["by_execution_versions"]["stop_execution=NONE|entry_execution=NONE"]
    new = section["by_execution_versions"][
        f"stop_execution={se.VERSION}|entry_execution={me.VERSION}"]
    assert (old["trades"], old["maker_share"], old["mean_stop_slippage_fraction"]) == (
        2, D(0), D("0.008"))
    assert old["exit_paths"] == {"MARKET": 1, "NATIVE_STOP_LIMIT": 1}
    assert (new["trades"], new["maker_share"], new["maker_share_count"]) == (2, D(1), 1)
    assert new["mean_entry_fee_rate"] == D("0.0015") and new["entry_fee_rate_count"] == 1
    assert new["stop_exits"] == 1 and new["mean_stop_slippage_fraction"] == D("0.006")
    assert section["liquidity_split_fee_rate"] == "0.0020"


def test_a_bid_at_or_under_the_stop_is_never_the_maker_price():
    price, evidence = me.entry_limit(D("0.01"), entry_trigger=D("100"), max_entry=D("100.10"),
                                     observation={"bid": "95", "ask": "95.10"}, stop=D("95"))
    assert price == D("100") and evidence["basis"] == me.BASIS_NO_BID
