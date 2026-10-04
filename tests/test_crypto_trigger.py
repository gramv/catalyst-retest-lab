"""CRYPTO_ALPACA_TRIGGER_V1 (package crypto-trigger, plan phase 4a).

A crypto setup admitted from a report-V3 packet records ``trigger_version`` and triggers on a
print or a fresh ask at or below its entry trigger, confirmed on a fresh ask at or below its max
entry within a 1% spread; freshness is by read time. Every other setup keeps today's trigger.

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue, a mock
Jev transport, a fixture universe and Alpaca market data behind an httpx MockTransport and the
real read-only market source. No broker, provider, network or owner-ledger contact.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_trigger as ct
from catalyst_lab import system_check as sc
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_service import STATE_FIELDS
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_replacement import PASSING, ladder, runtime_for
from tests.test_system_check import (
    at_price,
    publish_v3,
    stream,
    stream_row,
    system_runtime,
    two_slots,
    v3_pick,
)
from tests.test_system_check import feed as feed
from tests.test_system_check import market as market

T0 = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)
ADMITTED = T0 - timedelta(minutes=5)
LEVELS = {"entry_trigger": D("100"), "max_entry_price": D("100.10"), "stop": D("95")}
# (bid, ask): (ask - bid) / mid exactly 1% (100 bps) and exactly 1.01%, each ask at or below
# the entry trigger 100, so each is a quote touch.
EXACT_1PCT = ("99.0025", "99.9975")
OVER_1PCT = ("98.977626", "99.982374")
TOUCH = ("99.95", "99.99")  # A quote touch with a 4 bps spread.


# --- The rules at their boundaries (pure) ---------------------------------------------------

def seen(*, trade=None, trade_at=T0, bid=None, ask=None, quote_at=T0, read_at=None,
         source=None, healthy=True, **extra):
    """An observation: an optional print and an optional quote with its read time."""
    value = {"feed_healthy": healthy, "data_provider": "LAB_FIXTURE", "data_feed": "FIXTURE"}
    if trade is not None:
        value.update(trade_price=trade, trade_at=trade_at.isoformat(), trade_id="t-1")
    if bid is not None:
        value.update(bid=bid, ask=ask, quote_at=quote_at.isoformat())
        if read_at is not None:
            value.update(quote_read_at=read_at.isoformat(), quote_source=source)
    return {**value, **extra}


def verdict(observed, now=T0):
    return ct.evaluate(LEVELS, observed, now=now, admitted_at=ADMITTED)


def outcome(observed, now=T0):
    result = verdict(observed, now)
    return result.outcome, result.reason, result.touch


def test_a_print_at_or_below_the_trigger_touches_and_is_confirmed_on_a_fresh_quote():
    confirmed = verdict(seen(trade="100", bid="99.99", ask="100.01"))
    assert (confirmed.outcome, confirmed.reason, confirmed.touch) == ("CONFIRM", None, "PRINT")
    evidence = confirmed.evidence
    assert (evidence["version"], evidence["touch"], evidence["bid"], evidence["ask"]) == (
        "CRYPTO_ALPACA_TRIGGER_V1", "PRINT", "99.99", "100.01")
    assert (evidence["last"], evidence["last_trade_id"]) == ("100", "t-1")  # The print.
    assert evidence["spread_bps"] == "2.0000" and evidence["max_spread_bps"] == "100"
    assert evidence["print"] == {"price": "100", "at": T0.isoformat(), "trade_id": "t-1",
                                 "age_seconds": "0.0"}
    assert (evidence["quote_read_basis"], evidence["quote_fresh"]) == ("QUOTE_TIMESTAMP", True)
    # A print above the trigger is no touch, and the ask above it is none either.
    assert outcome(seen(trade="100.01", bid="100.00", ask="100.02")) == ("NO_TOUCH", None, None)


def test_a_fresh_ask_at_or_below_the_trigger_touches_without_any_print():
    assert outcome(seen(bid="99.95", ask="100")) == ("CONFIRM", None, "QUOTE")
    assert outcome(seen(bid="99.95", ask="100.000001")) == ("NO_TOUCH", None, None)
    last = verdict(seen(bid="99.95", ask="100", last_price="100.40", last_at=T0.isoformat(),
                        last_trade_id="42")).evidence
    assert (last["print"], last["last"], last["last_trade_id"]) == (None, "100.40", "42")


@pytest.mark.parametrize(("quote", "expected", "bps"), [
    (EXACT_1PCT, ("CONFIRM", None, "QUOTE"), "100.0000"),                 # Exactly 1% passes.
    (OVER_1PCT, ("WAIT", "SPREAD_ABOVE_MAXIMUM", "QUOTE"), "101.0000"),  # 1.01% waits.
])
def test_the_spread_cap_is_exactly_one_percent_of_the_mid(quote, expected, bps):
    bid, ask = quote
    result = verdict(seen(bid=bid, ask=ask))
    assert (result.outcome, result.reason, result.touch) == expected
    assert result.evidence["spread_bps"] == bps
    assert ct.MAX_SPREAD_BPS == D("100")
    assert ct.spread_within_cap(D(bid), D(ask)) is (expected[0] == "CONFIRM")
    # A print touch waits on the same spread; a wide spread never invalidates.
    assert outcome(seen(trade="100", bid=bid, ask=ask))[:2] == expected[:2]


def test_freshness_is_by_read_time_never_by_the_quotes_own_timestamp():
    old = T0 - timedelta(minutes=10)  # An unchanged quote on a thin coin: still current.
    for source in (sc.STREAM_SOURCE, sc.REST_SOURCE):
        fresh = verdict(seen(bid="99.95", ask="99.99", quote_at=old,
                             read_at=T0 - timedelta(seconds=5), source=source))
        assert (fresh.outcome, fresh.touch) == ("CONFIRM", "QUOTE")
        assert (fresh.evidence["quote_age_seconds"], fresh.evidence["quote_read_age_seconds"]) == (
            "600.0", "5.0")
        assert fresh.evidence["quote_read_basis"] == (
            "STREAM_RECEIPT" if source == sc.STREAM_SOURCE else "REST_READ")
    other = verdict(seen(bid="99.95", ask="99.99", quote_at=old, read_at=T0))  # No source.
    assert (other.outcome, other.evidence["quote_read_basis"]) == ("CONFIRM", "RECORDED_READ_TIME")
    stale = seen(bid="99.95", ask="99.99", quote_at=old,
                 read_at=T0 - timedelta(seconds=5, microseconds=1), source=sc.STREAM_SOURCE)
    assert outcome(stale) == ("NO_TOUCH", None, None)  # No fresh quote, no quote touch.
    assert verdict(stale).evidence["quote_code"] == "QUOTE_NOT_FRESH"
    # A print touch without a fresh quote waits for the next touch.
    assert outcome({**stale, **seen(trade="100")}) == ("WAIT", "FRESH_QUOTE_UNAVAILABLE", "PRINT")
    # Without a recorded read time the quote's own time stands in (it can only be earlier).
    assert outcome(seen(bid="99.95", ask="99.99", quote_at=T0 - timedelta(seconds=5)))[0] == (
        "CONFIRM")
    assert outcome(seen(bid="99.95", ask="99.99", quote_at=T0 - timedelta(seconds=6)))[0] == (
        "NO_TOUCH")
    # A quote stamped after its read, or read in the future, is never fresh.
    after = verdict(seen(bid="99.95", ask="99.99", quote_at=T0, read_at=T0 - timedelta(seconds=1),
                         source=sc.STREAM_SOURCE))
    assert (after.outcome, after.evidence["quote_code"]) == ("NO_TOUCH", "QUOTE_AFTER_READ")
    ahead = seen(bid="99.95", ask="99.99", quote_at=T0, read_at=T0 + timedelta(seconds=1),
                 source=sc.REST_SOURCE)
    assert outcome(ahead) == ("NO_TOUCH", None, None)


def test_stop_invalidations_come_before_any_touch():
    assert outcome(seen(trade="95")) == ("INVALIDATE", "STOP_TRADED_BEFORE_TRIGGER", "PRINT")
    assert outcome(seen(trade="94", bid="99.95", ask="99.99"))[1] == "STOP_TRADED_BEFORE_TRIGGER"
    # A fresh bid at the stop, even with the ask touching, or with no touch at all.
    assert outcome(seen(bid="95", ask="95.50")) == (
        "INVALIDATE", "STOP_QUOTED_BEFORE_TRIGGER", "QUOTE")
    assert outcome(seen(bid="94.99", ask="101")) == (
        "INVALIDATE", "STOP_QUOTED_BEFORE_TRIGGER", "QUOTE")
    assert outcome(seen(bid="95.01", ask="101")) == ("NO_TOUCH", None, None)
    # A stale bid at the stop invalidates nothing.
    assert outcome(seen(bid="94", ask="101", quote_at=T0 - timedelta(seconds=6))) == (
        "NO_TOUCH", None, None)
    assert outcome(seen(bid="99.95", ask="99.99", healthy=False)) == (
        "INVALIDATE", "DATA_FEED_FAILURE", None)


def test_a_touch_above_max_entry_invalidates_unless_the_spread_is_above_the_cap():
    assert outcome(seen(trade="100", bid="100.09", ask="100.11")) == (
        "INVALIDATE", "PRICE_BEYOND_MAX_ENTRY", "PRINT")
    assert outcome(seen(trade="100", bid="100.09", ask="100.10")) == ("CONFIRM", None, "PRINT")
    # As today, a spread above the cap waits before the max-entry check: 1.2% here.
    assert outcome(seen(trade="100", bid="99.00", ask="100.20")) == (
        "WAIT", "SPREAD_ABOVE_MAXIMUM", "PRINT")


def test_prints_before_admission_ahead_of_now_or_older_than_five_seconds():
    before = verdict(seen(trade="94", trade_at=ADMITTED - timedelta(seconds=1)))
    assert (before.outcome, before.reason) == ("IGNORED", "TRIGGER_BEFORE_ADMISSION")
    ahead = verdict(seen(trade="100", trade_at=T0 + timedelta(seconds=1), bid="99.99",
                         ask="100.01"))
    assert (ahead.outcome, ahead.reason) == ("IGNORED", "STALE_MARKET_OBSERVATION")
    quote = {"bid": "100.00", "ask": "100.02"}
    assert outcome(seen(trade="100", trade_at=T0 - timedelta(seconds=5), **quote))[0] == "CONFIRM"
    assert outcome(seen(trade="100", trade_at=T0 - timedelta(seconds=5, microseconds=1),
                        **quote)) == ("NO_TOUCH", None, None)
    # A stale print at the stop still invalidates, as today.
    assert outcome(seen(trade="95", trade_at=T0 - timedelta(minutes=3)))[1] == (
        "STOP_TRADED_BEFORE_TRIGGER")
    with pytest.raises(ValueError, match="INVALID_MARKET_EVIDENCE"):
        verdict(seen(trade="not-a-price"))


def test_the_decision_quote_time_is_the_read_time_for_this_version_only():
    observed = seen(trade="100", bid="99.99", ask="100.01", quote_at=T0 - timedelta(minutes=3),
                    read_at=T0 - timedelta(seconds=2), source=sc.REST_SOURCE)
    assert ct.decision_quote({"state": "WATCHING"}, observed) == {
        "quote_at": observed["quote_at"]}  # Every other setup: unchanged.
    assert ct.decision_quote({"trigger_version": ct.CRYPTO_TRIGGER_VERSION}, observed) == {
        "quote_at": (T0 - timedelta(seconds=2)).isoformat(),
        "quote_exchange_at": (T0 - timedelta(minutes=3)).isoformat(),
        "quote_read_at": (T0 - timedelta(seconds=2)).isoformat(),
        "quote_read_basis": "REST_READ", "quote_source": sc.REST_SOURCE,
        "trigger_version": "CRYPTO_ALPACA_TRIGGER_V1"}


def test_the_version_applies_to_report_v3_crypto_packets_only():
    v3 = {"report_schema_version": sc.REPORT_SCHEMA_V3, "market": "CRYPTO"}
    assert ct.applies(v3)
    assert not ct.applies({**v3, "market": "US_STOCKS"})
    assert not ct.applies({"market": "CRYPTO"})  # V2, B1, B2, engineering and legacy packets.
    assert ct.active({"trigger_version": "CRYPTO_ALPACA_TRIGGER_V1"})
    assert not ct.active({"state": "WATCHING"}) and not ct.active(None)
    assert "trigger_version" in STATE_FIELDS  # Listed by /setups, /positions and /results.


# --- The shared reader, by read time ----------------------------------------------------------

def test_the_reader_by_read_time_uses_the_stream_receipt_and_shares_the_rest_budget(feed):
    old = T0 - timedelta(minutes=10)
    row = stream_row("100.49", "100.51", quote_at=old, last="100.50", trade_at=old)
    feed.now[0] = T0
    live = feed.reader.read("AAA/USD", row, by_read_time=True,
                            received_at=T0 - timedelta(seconds=5))
    assert (live.source, live.quote_at, live.read_at, live.last) == (
        "ALPACA_STREAM", old, T0 - timedelta(seconds=5), D("100.50"))
    assert feed.data.requests == []
    # The system check keeps reading the same row by its own timestamp: stale there.
    feed.data.quotes["AAA/USD"] = ("100.47", "100.53", old)
    feed.reader.new_tick()
    assert feed.reader.read("AAA/USD", row).source == "ALPACA_REST_LATEST_QUOTE"
    assert feed.data.requests == ["AAA/USD"] and feed.reader.rest_attempted_at("AAA/USD") == T0
    # Received just over five seconds ago: the tick's one REST read, shared with admission.
    feed.reader.new_tick()
    feed.data.quotes["BBB/USD"] = ("10", "10.01", old)
    late = T0 - timedelta(seconds=5, microseconds=1)
    rest = feed.reader.read("BBB/USD", row, by_read_time=True, received_at=late)
    assert (rest.source, rest.read_at, rest.quote_at) == ("ALPACA_REST_LATEST_QUOTE", T0, old)
    with pytest.raises(sc.LivePriceUnavailable) as caught:
        feed.reader.read("CCC/USD", row, by_read_time=True, received_at=late)
    assert caught.value.attempts == [
        {"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_STALE", "quote_at": old.isoformat(),
         "received_at": late.isoformat()},
        {"source": "ALPACA_REST_LATEST_QUOTE", "code": "REST_READ_LIMIT_THIS_TICK"}]
    with pytest.raises(sc.LivePriceUnavailable) as caught:  # A quote stamped after its receipt.
        feed.reader.read("CCC/USD", stream_row(quote_at=T0), by_read_time=True,
                         received_at=T0 - timedelta(seconds=1))
    assert caught.value.attempts[0]["code"] == "STREAM_QUOTE_INVALID"
    assert feed.data.requests == ["AAA/USD", "BBB/USD"]


# --- The controller (disposable PostgreSQL, fake paper venue) -----------------------------------

def v3_setups(mx, symbols, *, levels=None):
    """Report-V3 picks through intake, the mock Jev and publication, all admitted at a
    passing live price (a pullback: entry 100, max entry 100.10, stop 95, target 111)."""
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    selected = publish_v3(mx, [v3_pick(i, symbol, venue.now, levels=levels)
                               for i, symbol in enumerate(symbols)], run_slot=old)
    live = at_price("100.49", "100.51", at=venue.now)
    return {symbol: engine.admit(selected[symbol], live_quote=live) for symbol in symbols}


def rows(engine, kind, setup_id=None):
    with engine.repo.connect() as conn:
        found = conn.execute(
            "SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq", (kind,)
        ).fetchall()
    return [r for r in found if setup_id is None or r["setup_id"] == setup_id]


def event_count(engine):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM lab.managed_events").fetchone()["n"]


def state(engine, sid):
    return engine._load(sid)[1]


def entry_decision(engine, sid):
    with engine.repo.connect() as conn:
        decision = conn.execute(
            """SELECT * FROM lab.managed_risk_decisions WHERE setup_id=%s AND action='ENTRY'
            ORDER BY created_at""", (sid,)).fetchall()
        claimed = [bool(conn.execute("SELECT 1 FROM lab.managed_claims WHERE decision_id=%s",
                                     (d["decision_id"],)).fetchone()) for d in decision]
    return decision, claimed


def quote_only(mx, bid, ask, *, quote_at=None, read_at=None, source=sc.STREAM_SOURCE):
    """A quote observation as the runtime builds it (no print), read ``read_at``."""
    now = mx[1].now
    return {"feed_healthy": True, "data_provider": "ALPACA", "data_feed": "CRYPTO_US",
            "bid": bid, "ask": ask, "quote_at": (quote_at or now).isoformat(),
            "quote_read_at": (read_at or now).isoformat(), "quote_source": source}


def test_admission_records_the_version_for_report_v3_crypto_setups_only(mx):
    engine, _, _ = mx
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    assert state(engine, v3)["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    assert state(engine, v3)["entry_type"] == "PULLBACK"
    v2 = engine.admit(packet(mx, "BTC/USD"))
    assert state(engine, v2)["state"] == "WATCHING" and "trigger_version" not in state(engine, v2)


def test_a_print_touch_triggers_with_its_evidence(mx):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    decision = engine.observe_trigger(sid, observation(mx, trade_price="100", bid="99.99",
                                                       ask="100.01"))
    assert decision["outcome"] == "APPROVED"
    [entry] = venue.orders_of("buy")
    assert (entry["symbol"], entry["type"], D(entry["limit_price"])) == (
        "AAA/USD", "limit", D("100.10"))  # The order stays a limit at max entry.
    [confirmed] = rows(engine, "TRIGGER_CONFIRMED", sid)
    body = confirmed["body"]
    assert body["trade_price"] == "100" and body["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    evidence = body["crypto_trigger"]
    assert (evidence["touch"], evidence["bid"], evidence["ask"], evidence["last"]) == (
        "PRINT", "99.99", "100.01", "100")
    assert (evidence["spread_bps"], evidence["quote_fresh"]) == ("2.0000", True)
    assert evidence["quote_read_at"] == venue.now.isoformat()
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_quote_touch_triggers_without_a_print_and_is_authorized_by_its_read_time(mx):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    exchange, read = venue.now - timedelta(minutes=3), venue.now - timedelta(seconds=4)
    touched = quote_only(mx, *TOUCH, quote_at=exchange, read_at=read, source=sc.REST_SOURCE)
    decision = engine.observe_trigger(sid, touched)
    assert decision["outcome"] == "APPROVED"
    [row], [claimed] = entry_decision(engine, sid)
    assert claimed and row["decision_id"] == decision["decision_id"]
    context = row["context"]
    # The gate's five-second check reads ``quote_at``: the read time, not the 3-minute stamp.
    assert (context["quote_at"], context["quote_read_at"], context["quote_exchange_at"]) == (
        read.isoformat(), read.isoformat(), exchange.isoformat())
    assert (context["quote_source"], context["trigger_version"]) == (
        "ALPACA_REST_LATEST_QUOTE", "CRYPTO_ALPACA_TRIGGER_V1")
    [confirmed] = rows(engine, "TRIGGER_CONFIRMED", sid)
    assert "trade_price" not in confirmed["body"] and "trade_at" not in confirmed["body"]
    evidence = confirmed["body"]["crypto_trigger"]
    assert (evidence["touch"], evidence["quote_source"], evidence["quote_read_basis"]) == (
        "QUOTE", "ALPACA_REST_LATEST_QUOTE", "REST_READ")
    assert (evidence["quote_at"], evidence["quote_read_age_seconds"]) == (
        exchange.isoformat(), "4.0")
    assert len(venue.orders_of("buy")) == 1


def test_the_gate_still_bounds_the_read_time_at_five_seconds(mx, monkeypatch):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    dispatch = engine.dispatch

    def late(decision):  # Two seconds pass between the decision and its claim.
        venue.now += timedelta(seconds=2)
        return dispatch(decision)

    monkeypatch.setattr(engine, "dispatch", late)
    touched = quote_only(mx, *TOUCH, quote_at=venue.now - timedelta(seconds=30),
                         read_at=venue.now - timedelta(seconds=4))
    assert engine.observe_trigger(sid, touched)["outcome"] == "APPROVED"
    [_], [claimed] = entry_decision(engine, sid)
    assert not claimed and not venue.orders  # Six seconds after the read: refused.
    [refused] = rows(engine, "AUTHORIZATION_NOT_CLAIMED", sid)
    assert refused["body"]["code"] == "ENTRY_AUTHORIZATION_REVOKED"


def test_exactly_one_percent_passes_just_above_waits_and_a_later_touch_triggers(mx):
    engine, venue, _ = mx
    exact, over = v3_setups(mx, ["AAA/USD", "BBB/USD"]).values()
    assert engine.observe_trigger(exact, quote_only(mx, *EXACT_1PCT))["outcome"] == "APPROVED"
    before = event_count(engine)
    assert engine.observe_trigger(over, quote_only(mx, *OVER_1PCT)) is None
    assert engine.observe_trigger(over, quote_only(mx, *OVER_1PCT)) is None  # Same minute.
    assert state(engine, over)["state"] == "WATCHING"
    [wait] = rows(engine, "CRYPTO_TRIGGER_WAIT", over)
    assert event_count(engine) == before + 1  # One wait, nothing else.
    assert (wait["body"]["reason"], wait["body"]["touch"], wait["body"]["trigger_version"]) == (
        "SPREAD_ABOVE_MAXIMUM", "QUOTE", "CRYPTO_ALPACA_TRIGGER_V1")
    assert wait["body"]["crypto_trigger"]["spread_bps"] == "101.0000"
    minute = venue.now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    assert wait["idempotency_key"] == f"crypto-trigger-wait:{over}:SPREAD_ABOVE_MAXIMUM:{minute}"
    venue.now += timedelta(seconds=30)
    assert engine.observe_trigger(over, quote_only(mx, *TOUCH))["outcome"] == "APPROVED"
    assert {o["symbol"] for o in venue.orders_of("buy")} == {"AAA/USD", "BBB/USD"}


def test_stops_and_a_price_beyond_max_entry_invalidate_with_their_evidence(mx):
    engine, venue, _ = mx
    setups = v3_setups(mx, ["PRT/USD", "BID/USD", "MAX/USD", "WID/USD"])
    cases = {
        "PRT/USD": (observation(mx, trade_price="95"), "STOP_TRADED_BEFORE_TRIGGER"),
        "BID/USD": (quote_only(mx, "95", "101"), "STOP_QUOTED_BEFORE_TRIGGER"),
        "MAX/USD": (observation(mx, trade_price="100", bid="100.10", ask="100.11"),
                    "PRICE_BEYOND_MAX_ENTRY"),
    }
    for symbol, (observed, reason) in cases.items():
        assert engine.observe_trigger(setups[symbol], observed) is None
        current = state(engine, setups[symbol])
        assert (current["state"], current["reason"]) == ("INVALIDATED", reason), symbol
        evidence = current["crypto_trigger"]
        assert evidence["version"] == "CRYPTO_ALPACA_TRIGGER_V1"
        assert evidence["bid"] == observed.get("bid")
        assert current["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    assert {s: state(engine, setups[s])["crypto_trigger"]["touch"] for s in cases} == {
        "PRT/USD": "PRINT", "BID/USD": "QUOTE", "MAX/USD": "PRINT"}
    assert state(engine, setups["BID/USD"])["crypto_trigger"]["quote_read_at"] == (
        venue.now.isoformat())
    # A spread above 1% at a touch never invalidates, even with the ask above max entry.
    wide = setups["WID/USD"]
    assert engine.observe_trigger(wide, observation(
        mx, trade_price="100", bid="99.00", ask="100.20")) is None
    assert state(engine, wide)["state"] == "WATCHING"
    assert [r["body"]["reason"] for r in rows(engine, "CRYPTO_TRIGGER_WAIT", wide)] == [
        "SPREAD_ABOVE_MAXIMUM"]
    assert not venue.orders
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_touch_gone_stale_under_the_lock_waits_without_a_decision(mx):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    stale = observation(mx, trade_price="100", bid="99.99", ask="100.01",
                        quote_read_at=(venue.now - timedelta(seconds=6)).isoformat(),
                        quote_source=sc.STREAM_SOURCE)
    assert engine.authorize_entry(sid, stale, None) is None
    decisions, _ = entry_decision(engine, sid)
    assert decisions == [] and not venue.orders
    assert state(engine, sid)["state"] == "WATCHING"
    assert rows(engine, "TRIGGER_CONFIRMED", sid) == []
    [wait] = rows(engine, "CRYPTO_TRIGGER_WAIT", sid)
    assert (wait["body"]["reason"], wait["body"]["touch"]) == ("FRESH_QUOTE_UNAVAILABLE", "PRINT")
    # No touch at all under the lock: still no decision.
    assert engine.authorize_entry(sid, observation(mx, trade_price="102"), None) is None
    assert [r["body"]["reason"] for r in rows(engine, "CRYPTO_TRIGGER_WAIT", sid)] == [
        "FRESH_QUOTE_UNAVAILABLE", "TOUCH_NOT_CURRENT"]
    assert entry_decision(engine, sid)[0] == []


def test_setups_of_the_old_version_keep_todays_trigger(mx):
    engine, venue, _ = mx
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    v2 = engine.admit(packet(mx, "BTC/USD"))
    # A 25 bps spread: today's 10 bps cap waits; this version's 1% cap confirms.
    wide = {"trade_price": "100", "bid": "99.75", "ask": "100.00"}
    assert engine.observe_trigger(v2, observation(mx, **wide)) is None
    assert state(engine, v2)["state"] == "WATCHING" and not venue.orders
    assert engine.observe_trigger(v3, observation(mx, **wide))["outcome"] == "APPROVED"
    assert engine.observe_trigger(v2, observation(mx))["outcome"] == "APPROVED"
    [v2_confirmed] = rows(engine, "TRIGGER_CONFIRMED", v2)
    assert v2_confirmed["body"] == observation(mx)  # The body is the observation, as today.
    [v2_decision], _ = entry_decision(engine, v2)
    assert "quote_read_at" not in v2_decision["context"]
    assert v2_decision["context"]["quote_at"] == observation(mx)["quote_at"]


# --- The runtime: quote-driven evaluation, prints, bounded reads, end to end -------------------

def receive(run, venue, symbol, bid, ask, *, stamped, at):
    """A stream quote stamped ``stamped`` by the exchange and received by the runtime ``at``."""
    now = venue.now
    venue.now = at
    try:
        stream(run, symbol, bid, ask, at=stamped)
    finally:
        venue.now = now


def test_end_to_end_a_top_k_pick_is_admitted_quote_touched_authorized_claimed_and_filled(
    mx, market,
):
    engine, venue, _ = mx
    cycle, _ = ladder(mx)
    run = runtime_for(mx, market, cycle, ["A1/USD"])
    start = venue.now
    stream(run, "A1/USD", *PASSING, at=start)
    run.execution_once()  # Admission: SYSTEM_CHECK_V1 on the fresh stream quote.
    [setup] = engine.store.active()
    sid = setup["setup_id"]
    assert (setup["state"]["state"], setup["state"]["trigger_version"]) == (
        "WATCHING", "CRYPTO_ALPACA_TRIGGER_V1")
    assert setup["record_json"]["selection_policy"] == "JEV_TOP_K_SELECTION_V1"
    assert entry_decision(engine, sid)[0] == [] and market.data.requests == []
    # Ten seconds later the ask comes down to the trigger. The quote's own stamp is the
    # admission quote's (it last changed then); the runtime receives it now.
    received = start + timedelta(seconds=10)
    receive(run, venue, "A1/USD", *TOUCH, stamped=start, at=received)
    venue.now = start + timedelta(seconds=14)  # Received four seconds ago: still fresh.
    run.execution_once()
    [decision], [claimed] = entry_decision(engine, sid)
    assert decision["outcome"] == "APPROVED" and claimed  # Claimed at 4 s old, within 5 s.
    assert (decision["context"]["quote_at"], decision["context"]["quote_exchange_at"]) == (
        received.isoformat(), start.isoformat())
    assert decision["context"]["quote_source"] == "ALPACA_STREAM"
    [confirmed] = rows(engine, "TRIGGER_CONFIRMED", sid)
    evidence = confirmed["body"]["crypto_trigger"]
    assert (evidence["touch"], evidence["quote_read_basis"], evidence["quote_read_age_seconds"],
            evidence["quote_age_seconds"]) == ("QUOTE", "STREAM_RECEIPT", "4.0", "14.0")
    assert (evidence["bid"], evidence["ask"]) == TOUCH
    assert market.data.requests == []  # The stream quote was fresh by its receipt.
    [entry] = venue.orders_of("buy")
    assert (entry["symbol"], D(entry["limit_price"])) == ("A1/USD", D("100.10"))
    engine.ingest(venue.fill(entry["id"], entry["qty"], price="99.99"))
    engine.manage(sid, observation(mx, trade_price="99.99", bid="99.98", ask="100.00"))
    assert state(engine, sid)["state"] == "OPEN"
    assert venue.orders_of("sell", "stop_limit")  # Protected at once.
    assert run.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_stale_stream_quote_costs_one_rest_read_and_no_fresh_quote_means_no_trigger(
    mx, market,
):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    run = system_runtime(mx, market, ["AAA/USD"])
    start = venue.now
    receive(run, venue, "AAA/USD", *TOUCH, stamped=start - timedelta(seconds=40),
            at=start - timedelta(seconds=6))  # Received six seconds ago: not fresh.
    market.data.status = 503
    before = event_count(engine)
    run.execution_once()
    assert market.data.requests == ["AAA/USD"]  # One REST read, which failed.
    assert event_count(engine) == before and state(engine, sid)["state"] == "WATCHING"
    venue.now = start + timedelta(seconds=4)
    run.execution_once()  # A failed read waits five seconds.
    assert market.data.requests == ["AAA/USD"] and event_count(engine) == before
    market.data.status = 200
    market.data.quotes["AAA/USD"] = (*TOUCH, start - timedelta(minutes=3))  # Unchanged since.
    venue.now = start + timedelta(seconds=5)
    run.execution_once()
    assert market.data.requests == ["AAA/USD", "AAA/USD"]
    [decision], [claimed] = entry_decision(engine, sid)
    assert decision["outcome"] == "APPROVED" and claimed
    assert (decision["context"]["quote_at"], decision["context"]["quote_exchange_at"]) == (
        venue.now.isoformat(), (start - timedelta(minutes=3)).isoformat())
    assert decision["context"]["quote_source"] == "ALPACA_REST_LATEST_QUOTE"
    assert rows(engine, "TRIGGER_CONFIRMED", sid)[0]["body"]["crypto_trigger"]["touch"] == "QUOTE"
    assert run.error is None


def test_a_print_touch_without_a_fresh_quote_waits_where_the_old_version_is_revoked(
    mx, market,
):
    engine, venue, _ = mx
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    v2 = engine.admit(packet(mx, "BTC/USD"))
    run = system_runtime(mx, market, ["AAA/USD", "BTC/USD"])
    market.data.status = 503  # No quote from REST either.
    for trade_id, symbol in enumerate(("AAA/USD", "BTC/USD")):
        run.market_message("CRYPTO", {"T": "t", "S": symbol, "p": "100", "i": trade_id,
                                      "t": venue.now.isoformat()})
    run.execution_once()
    assert run._pending_trades() == []
    assert state(engine, v3)["state"] == "WATCHING" and not state(engine, v3).get("revoked")
    [wait] = rows(engine, "CRYPTO_TRIGGER_WAIT", v3)
    assert (wait["body"]["reason"], wait["body"]["touch"]) == ("FRESH_QUOTE_UNAVAILABLE", "PRINT")
    assert [a["code"] for a in wait["body"]["crypto_trigger"]["quote_attempts"]] == [
        "STREAM_QUOTE_MISSING", "SCAN_HTTP_503"]
    [consumed] = rows(engine, "MARKET_PRINT_CONSUMED", v3)
    assert consumed["body"]["reason"] == "TRIGGER_CHECKED"
    # Today's trigger, unchanged: a quote-less print revokes the old-version setup.
    old = state(engine, v2)
    assert (old["state"], old["revocation_reason"]) == ("INVALIDATED", "DATA_FEED_FAILURE")
    assert [r["body"]["reason"] for r in rows(engine, "MARKET_PRINT_CONSUMED", v2)] == [
        "QUOTE_UNAVAILABLE_AT_PRINT"]
    assert not venue.orders


def test_a_fresh_bid_at_the_stop_invalidates_whenever_the_stream_is_ready(mx, market):
    engine, venue, _ = mx
    touch, stop = v3_setups(mx, ["TCH/USD", "STP/USD"]).values()
    run = system_runtime(mx, market, ["TCH/USD", "STP/USD"])
    run.connected = False  # Trade updates down: entries are not ready.
    assert not run.ready()
    stream(run, "TCH/USD", *TOUCH, at=venue.now)
    stream(run, "STP/USD", "95", "96", at=venue.now)
    run.execution_once()
    assert state(engine, touch)["state"] == "WATCHING"  # No entry while not ready.
    assert entry_decision(engine, touch)[0] == []
    invalidated = state(engine, stop)
    assert (invalidated["state"], invalidated["reason"]) == (
        "INVALIDATED", "STOP_QUOTED_BEFORE_TRIGGER")
    assert invalidated["crypto_trigger"]["quote_source"] == "ALPACA_STREAM"
    run.connected = True
    assert run.reconcile_once() and run.ready()
    run.execution_once()
    assert entry_decision(engine, touch)[0][0]["outcome"] == "APPROVED"


def test_many_watching_setups_share_one_rest_read_per_tick_in_rotation(
    mx, market, monkeypatch,
):
    engine, venue, _ = mx
    symbols = [f"R{i:02}/USD" for i in range(10)]
    setups = v3_setups(mx, symbols)
    run = system_runtime(mx, market, symbols)
    for symbol in symbols:  # Thin coins: no stream quote, REST says the ask is above the trigger.
        market.data.quotes[symbol] = ("100.49", "100.51", venue.now - timedelta(minutes=5))
    calls = []
    observe = engine.observe_trigger
    monkeypatch.setattr(engine, "observe_trigger",
                        lambda sid, observed: calls.append(sid) or observe(sid, observed))
    before, start, reads = event_count(engine), venue.now, []
    for second in range(20):
        venue.now = start + timedelta(seconds=second)
        seen_before = len(market.data.requests)
        run.execution_once()
        reads.append(len(market.data.requests) - seen_before)
    assert reads == [1] * 20  # One REST read per tick, shared by every setup.
    assert Counter(market.data.requests) == dict.fromkeys(symbols, 2)  # In rotation.
    for symbol in symbols:
        first, second = [i for i, s in enumerate(market.data.requests) if s == symbol]
        assert second - first == 10  # Never twice within five seconds.
    assert calls == [] and event_count(engine) == before  # Nothing to act on, nothing written.
    assert all(state(engine, sid)["state"] == "WATCHING" for sid in setups.values())
    # The touch arrives on the stream for the last symbol: evaluated at once, without REST.
    stream(run, symbols[-1], *TOUCH, at=venue.now)
    run.execution_once()
    assert calls == [setups[symbols[-1]]]
    assert entry_decision(engine, setups[symbols[-1]])[0][0]["outcome"] == "APPROVED"
    assert run.error is None


def test_nothing_is_read_for_a_tick_without_a_setup_of_this_version(mx, market, monkeypatch):
    engine, venue, _ = mx
    v2 = engine.admit(packet(mx, "BTC/USD"))
    run = system_runtime(mx, market, ["BTC/USD"])
    stream(run, "BTC/USD", *TOUCH, at=venue.now)  # A quote touch, which today's trigger ignores.
    calls = []
    monkeypatch.setattr(engine, "observe_trigger",
                        lambda sid, observed: calls.append(sid))
    for _ in range(3):
        run.execution_once()
    assert calls == [] and market.data.requests == []
    assert state(engine, v2)["state"] == "WATCHING" and not venue.orders


def test_a_print_touch_is_confirmed_on_one_rest_read_when_the_stream_quote_is_stale(
    mx, market,
):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    run = system_runtime(mx, market, ["AAA/USD"])
    start = venue.now
    receive(run, venue, "AAA/USD", "100.49", "100.51", stamped=start - timedelta(seconds=30),
            at=start - timedelta(seconds=20))
    market.data.quotes["AAA/USD"] = ("99.99", "100.01", start - timedelta(seconds=90))
    run.market_message("CRYPTO", {"T": "t", "S": "AAA/USD", "p": "100", "i": 7,
                                  "t": start.isoformat()})
    run.execution_once()
    assert market.data.requests == ["AAA/USD"]  # The print's one read; the pass reuses it.
    [decision], [claimed] = entry_decision(engine, sid)
    assert decision["outcome"] == "APPROVED" and claimed
    evidence = rows(engine, "TRIGGER_CONFIRMED", sid)[0]["body"]["crypto_trigger"]
    assert (evidence["touch"], evidence["quote_source"], evidence["print"]["trade_id"]) == (
        "PRINT", "ALPACA_REST_LATEST_QUOTE", "7")
    assert (evidence["bid"], evidence["ask"], evidence["quote_read_at"]) == (
        "99.99", "100.01", start.isoformat())
    assert decision["context"]["quote_at"] == start.isoformat()
    assert [r["body"]["reason"] for r in rows(engine, "MARKET_PRINT_CONSUMED", sid)] == [
        "TRIGGER_CHECKED"]


def test_admission_and_the_trigger_share_the_ticks_one_rest_read(mx, market):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    selected = publish_v3(mx, [v3_pick(0, "WAT/USD", venue.now),
                               v3_pick(1, "NEW/USD", venue.now)], run_slot=old)
    watching = engine.admit(selected["WAT/USD"],
                            live_quote=at_price("100.49", "100.51", at=venue.now))
    run = system_runtime(mx, market, ["WAT/USD", "NEW/USD"])
    stale = venue.now - timedelta(minutes=2)
    for symbol in ("WAT/USD", "NEW/USD"):  # No stream quote; REST has an old, current quote.
        market.data.quotes[symbol] = ("100.49", "100.51", stale)
    start = venue.now
    run.execution_once()  # Admission reads first and spends the tick's one read.
    assert market.data.requests == ["NEW/USD"]
    admitted = {s["symbol"]: s["state"] for s in engine.store.active()}
    assert admitted["NEW/USD"]["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    assert admitted["NEW/USD"]["system_check"]["live"]["quote_source"] == (
        "ALPACA_REST_LATEST_QUOTE")
    venue.now = start + timedelta(seconds=1)
    run.execution_once()  # The watching setup's turn; NEW/USD reuses its read of a second ago.
    assert market.data.requests == ["NEW/USD", "WAT/USD"]
    assert state(engine, watching)["state"] == "WATCHING" and run.error is None


def test_a_quote_less_print_above_the_trigger_is_quiet_for_this_version_only(mx, market):
    engine, venue, _ = mx
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    v2 = engine.admit(packet(mx, "BTC/USD"))
    run = system_runtime(mx, market, ["AAA/USD", "BTC/USD"])
    for trade_id, symbol in enumerate(("AAA/USD", "BTC/USD")):  # No quote yet for either.
        run.market_message("CRYPTO", {"T": "t", "S": symbol, "p": "100.40", "i": trade_id,
                                      "t": venue.now.isoformat()})
    assert rows(engine, "MARKET_PRINT", v3) == []  # Coalesced into the minute's summary.
    [queued] = rows(engine, "MARKET_PRINT", v2)  # Today: evaluated alone, then revoked.
    run.execution_once()
    assert state(engine, v3)["state"] == "WATCHING"
    assert state(engine, v2)["revocation_reason"] == "DATA_FEED_FAILURE"
    run._flush_print_summaries(force=True)
    [summary] = rows(engine, "MARKET_PRINT_SUMMARY")
    assert summary["body"]["setup_ids"] == [str(v3)] and summary["body"]["count"] == 1


def test_a_stream_message_stamped_up_to_three_seconds_ahead_of_our_clock_is_current(mx, market):
    """2026-09-29: Alpaca stamps stream messages on its servers and the container's clock ran a
    little behind them; with no tolerance every such message ended the crypto stream session
    (INVALID_MARKET_TIMESTAMP, 18 drops in 22 minutes). Up to 3 s ahead is current; beyond is
    refused as before."""
    engine, venue, _ = mx
    v3_setups(mx, ["AAA/USD"])
    run = system_runtime(mx, market, ["AAA/USD"])
    ahead = venue.now + timedelta(seconds=2)
    run.market_message("CRYPTO", {"T": "t", "S": "AAA/USD", "p": "100", "i": 7,
                                  "t": ahead.isoformat()})
    too_far = venue.now + timedelta(seconds=4)
    with pytest.raises(ValueError, match="INVALID_MARKET_TIMESTAMP"):
        run.market_message("CRYPTO", {"T": "t", "S": "AAA/USD", "p": "100", "i": 8,
                                      "t": too_far.isoformat()})

