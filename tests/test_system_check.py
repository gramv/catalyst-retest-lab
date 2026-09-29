"""SYSTEM_CHECK_V1 and RESEARCH_RUN_SUPERSESSION_V1 for report-V3 picks (package system-check).

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue, a mock
Jev transport, a fixture universe and a mock Alpaca market-data transport behind the real
read-only market source. No broker, provider, network or owner-ledger contact.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest

from catalyst_lab import system_check as sc
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.broker_budget import RESEARCH, current_priority
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_runtime import (
    PERMANENT_ADMISSION_REFUSALS,
    ManagedRuntime,
    engineering_runtime_policy,
)
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_ranking import QUALITY
from catalyst_lab.research_schedule import ResearchSchedule
from catalyst_lab.scan_sources import AlpacaMarketSource, SourcePolicy
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_managed_runtime import Research, reviewed_cycle
from tests.test_research_cycle import quality_response, response
from tests.test_research_report_v3 import cycle_of, pick, report_v3, v3_intake
from tests.test_selection_b1 import classify, no_sleep

T0 = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)
CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
SOURCE_POLICY = SourcePolicy("iex", 3, 50, 10000, 10, 5)
# Hourly UTC runs with a 59-minute grace: two consecutive slots both accept a report now.
HOURLY = ResearchSchedule("UTC", tuple(f"{h:02}:00" for h in range(24)), 59)
QUOTES = "/v1beta3/crypto/us/latest/quotes"


# --- Pure checks at their boundaries -------------------------------------------------------------

def record(levels=None, agent="100", slot=T0):
    """The V3 fields of a selected packet the check reads."""
    return {
        "market": "CRYPTO", "symbol": "AAA/USD", "report_schema_version": sc.REPORT_SCHEMA_V3,
        "run_slot": slot.isoformat(),
        "levels": levels or {"entry_trigger": "99.5", "max_entry_price": "100", "stop": "98",
                             "target": "104"},
        "state": {"agent_current_price": agent, "agent_price_at": T0.isoformat()},
    }


def quote(bid, ask, *, last=None, source=sc.STREAM_SOURCE, at=T0, read_at=T0):
    return sc.LiveQuote("AAA/USD", D(bid), D(ask), at, source, read_at,
                        D(last) if last is not None else None,
                        at if last is not None else None, "7" if last is not None else None)


@pytest.mark.parametrize(("mid", "matches"), [
    ("105", True), ("105.000000000001", False),  # Exactly 5% above passes; just above fails.
    ("95", True), ("94.999999999999", False),    # Exactly 5% below passes; just below fails.
    ("100", True),
])
def test_price_mismatch_boundary_is_exactly_five_percent(mid, matches):
    assert sc.price_matches(D(mid), D("100")) is matches


@pytest.mark.parametrize(("entry", "kind"), [
    ("99.8", "IMMEDIATE"), ("99.799999999999", "PULLBACK"),   # 0.2% below the mid
    ("100.2", "IMMEDIATE"), ("100.200000000001", "BREAKOUT"),  # 0.2% above the mid
    ("100", "IMMEDIATE"), ("95", "PULLBACK"), ("101", "BREAKOUT"),
])
def test_entry_type_boundary_is_zero_point_two_percent_of_the_mid(entry, kind):
    assert sc.entry_type(D(entry), D("100")) == kind


@pytest.mark.parametrize(("stop", "ok"), [
    ("98", True), ("98.000000000001", False), ("97.99", True),  # Exactly 2% passes.
])
def test_stop_distance_boundary_is_exactly_two_percent_of_max_entry(stop, ok):
    assert sc.stop_distance_ok(D("100"), D(stop)) is ok
    assert sc.MINIMUM_CRYPTO_STOP_FRACTION == D("0.02")


def test_evaluate_records_every_check_and_names_the_first_failure():
    passed = sc.evaluate(record(), quote("104.99", "105.01"), now=T0)  # Mid exactly +5%.
    assert (passed["result"], passed["code"], passed["entry_type"]) == ("PASSED", None, "PULLBACK")
    assert passed["checks"] == {"stop_distance": "PASS", "price_match": "PASS",
                                "stop_not_hit": "PASS", "entry_type_traded": "PASS"}
    assert passed["price_deviation_fraction"] == "0.050000000000"
    assert passed["stop_distance_fraction"] == "0.020000000000"  # Exactly 2% passes.
    assert passed["entry_offset_fraction"] == "-0.052380952381"
    assert passed["live"] == {
        "quote_source": "ALPACA_STREAM", "bid": "104.99", "ask": "105.01", "mid": "105.00",
        "quote_at": T0.isoformat(), "read_at": T0.isoformat(), "quote_age_seconds": "0.0",
        "last": None, "last_at": None, "last_trade_id": None, "last_source": None}
    assert (passed["version"], passed["minimum_stop_fraction"], passed["price_mismatch_fraction"],
            passed["entry_type_band_fraction"]) == ("SYSTEM_CHECK_V1", "0.02", "0.05", "0.002")
    over = sc.evaluate(record(), quote("104.99", "105.010000000002"), now=T0)
    assert (over["result"], over["code"]) == ("REFUSED", "PRICE_MISMATCH")
    assert over["checks"]["price_match"] == "FAIL" and over["checks"]["stop_not_hit"] == "PASS"
    # A breakout also far from the agent's price: both recorded, the first in order names it.
    both = sc.evaluate(record(agent="90"), quote("99.00", "99.02"), now=T0)
    assert both["code"] == "PRICE_MISMATCH" and both["entry_type"] == "BREAKOUT"
    assert both["checks"]["entry_type_traded"] == "FAIL"


@pytest.mark.parametrize(("bid", "last", "hit"), [
    ("98.00", None, True),     # The bid at the stop.
    ("98.01", None, False),
    ("98.01", "98.00", True),  # The last trade at the stop.
    ("98.01", "98.01", False),
])
def test_stop_already_hit_by_the_bid_or_the_last_trade(bid, last, hit):
    levels = {"entry_trigger": "98.15", "max_entry_price": "100", "stop": "98", "target": "104"}
    ask = str(D(bid) + D("0.02"))
    result = sc.evaluate(record(levels, agent="98.05"), quote(bid, ask, last=last), now=T0)
    assert result["entry_type"] == "IMMEDIATE"  # 98.15 is within 0.2% of the mid.
    assert result["checks"]["stop_not_hit"] == ("FAIL" if hit else "PASS")
    assert result["code"] == ("STOP_ALREADY_HIT" if hit else None)
    if last is not None:
        assert result["live"]["last"] == last and result["live"]["last_source"] == "ALPACA_STREAM"


def test_levels_alone_refuse_a_short_stop_and_otherwise_wait_for_a_live_price():
    short = record({"entry_trigger": "99.5", "max_entry_price": "100", "stop": "98.000000000001",
                    "target": "104"})
    refused = sc.evaluate(short, None, now=T0)
    assert (refused["result"], refused["code"]) == ("REFUSED", "STOP_DISTANCE_BELOW_MINIMUM")
    assert refused["checks"] == {"stop_distance": "FAIL", "price_match": "NOT_EVALUATED",
                                 "stop_not_hit": "NOT_EVALUATED",
                                 "entry_type_traded": "NOT_EVALUATED"}
    assert refused["live"] is None and "live_price_attempts" not in refused
    attempts = [{"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_MISSING"}]
    waiting = sc.evaluate(record(), None, now=T0, attempts=attempts)
    assert (waiting["result"], waiting["code"]) == ("RETRY", "LIVE_PRICE_UNAVAILABLE")
    assert waiting["live_price_attempts"] == attempts
    assert waiting["checks"]["stop_distance"] == "PASS"


def test_codes_are_classified_permanent_or_transient():
    assert sc.SYSTEM_CHECK_REFUSALS == {"PRICE_MISMATCH", "STOP_ALREADY_HIT",
                                        "BREAKOUT_NOT_ENABLED", "STOP_DISTANCE_BELOW_MINIMUM"}
    assert sc.SYSTEM_CHECK_REFUSALS | {"SUPERSEDED_BY_NEW_RESEARCH"} <= PERMANENT_ADMISSION_REFUSALS
    assert "LIVE_PRICE_UNAVAILABLE" not in PERMANENT_ADMISSION_REFUSALS
    refusal = sc.AdmissionRefused("PRICE_MISMATCH", {"system_check": {"code": "PRICE_MISMATCH"}})
    assert str(refusal) == "PRICE_MISMATCH" and isinstance(refusal, ValueError)


# --- The live-price reader ------------------------------------------------------------------

class MarketData:
    """Alpaca crypto latest quotes behind the real read-only market source (MockTransport)."""

    def __init__(self):
        self.quotes, self.requests, self.status = {}, [], 200

    def handle(self, request):
        assert request.method == "GET" and request.url.host == "data.alpaca.markets"
        assert request.url.path == QUOTES
        self.requests.append(request.url.params["symbols"])
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "fixture refusal"})
        rows = {}
        for symbol in request.url.params["symbols"].split(","):
            if symbol in self.quotes:
                bid, ask, at = self.quotes[symbol]
                rows[symbol] = {"t": at.isoformat().replace("+00:00", "Z"), "bp": float(bid),
                                "ap": float(ask), "bs": 1, "as": 1}
        return httpx.Response(200, json={"quotes": rows})


@pytest.fixture
def feed():
    now, data = [T0], MarketData()
    source = AlpacaMarketSource(CREDENTIALS, SOURCE_POLICY, lambda: now[0],
                                transport=httpx.MockTransport(data.handle))
    yield SimpleNamespace(now=now, data=data, source=source,
                          reader=sc.LivePriceReader(source, clock=lambda: now[0]))
    source.close()


def stream_row(bid="100.49", ask="100.51", *, quote_at=T0, last=None, trade_at=T0):
    row = {"bid": bid, "ask": ask, "quote_at": quote_at.isoformat(), "feed_healthy": True,
           "data_provider": "ALPACA", "data_feed": "CRYPTO_US"}
    if last is not None:
        row.update(trade_price=last, trade_at=trade_at.isoformat(), trade_id="42")
    return row


def test_a_stream_quote_up_to_five_seconds_old_is_live_and_costs_no_rest_read(feed):
    feed.now[0] = T0 + timedelta(seconds=5)
    live = feed.reader.read("AAA/USD", stream_row(last="100.50", trade_at=T0 - timedelta(hours=1)))
    assert (live.source, live.bid, live.ask, live.quote_at) == (
        "ALPACA_STREAM", D("100.49"), D("100.51"), T0)
    assert (live.last, live.last_at, live.last_trade_id) == (
        D("100.50"), T0 - timedelta(hours=1), "42")  # The last trade, whatever its age.
    assert live.read_at == T0 + timedelta(seconds=5) and feed.data.requests == []


def test_a_stale_stream_quote_costs_one_rest_read_fresh_by_its_read_time(feed):
    feed.data.quotes["AAA/USD"] = ("100.47", "100.53", T0 - timedelta(seconds=40))
    feed.now[0] = T0 + timedelta(seconds=5, microseconds=1)
    live = feed.reader.read("AAA/USD", stream_row(last="100.52"))
    assert feed.data.requests == ["AAA/USD"] and feed.reader.rest_reads == 1
    assert (live.source, live.bid, live.ask) == ("ALPACA_REST_LATEST_QUOTE", D("100.47"),
                                                 D("100.53"))
    assert live.quote_at == T0 - timedelta(seconds=40) and live.read_at == feed.now[0]
    assert live.last == D("100.52")  # The stream's last trade still counts.
    assert live.evidence()["quote_age_seconds"] == "45.000001"
    # Reused, not re-read, for five seconds after the read (another packet, same symbol).
    feed.reader.new_tick()
    feed.now[0] += timedelta(seconds=4)
    assert feed.reader.read("AAA/USD", None).quote_at == live.quote_at
    assert feed.data.requests == ["AAA/USD"]


def test_rest_reads_are_bounded_per_tick_and_per_symbol(feed):
    for symbol in ("AAA/USD", "BBB/USD"):
        feed.data.quotes[symbol] = ("10", "10.01", T0)
    feed.reader.new_tick()
    assert feed.reader.read("AAA/USD", None).source == "ALPACA_REST_LATEST_QUOTE"
    with pytest.raises(sc.LivePriceUnavailable) as caught:
        feed.reader.read("BBB/USD", None)
    assert caught.value.attempts == [
        {"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_MISSING", "quote_at": None},
        {"source": "ALPACA_REST_LATEST_QUOTE", "code": "REST_READ_LIMIT_THIS_TICK"}]
    feed.reader.new_tick()  # The next tick reads the other symbol.
    assert feed.reader.read("BBB/USD", None).bid == D("10")
    assert feed.data.requests == ["AAA/USD", "BBB/USD"]


def test_a_failed_rest_read_waits_five_seconds_before_the_next(feed):
    feed.data.status = 429
    feed.reader.new_tick()
    with pytest.raises(sc.LivePriceUnavailable) as caught:
        feed.reader.read("AAA/USD", stream_row(quote_at=T0 - timedelta(seconds=6)))
    assert caught.value.attempts == [
        {"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_STALE",
         "quote_at": (T0 - timedelta(seconds=6)).isoformat()},
        {"source": "ALPACA_REST_LATEST_QUOTE", "code": "SCAN_HTTP_429",
         "attempted_at": T0.isoformat()}]
    feed.reader.new_tick()
    feed.now[0] = T0 + timedelta(seconds=4)
    with pytest.raises(sc.LivePriceUnavailable) as caught:
        feed.reader.read("AAA/USD", None)
    assert caught.value.attempts[-1] == {
        "source": "ALPACA_REST_LATEST_QUOTE", "code": "REST_RETRY_WAIT",
        "last_code": "SCAN_HTTP_429", "attempted_at": T0.isoformat()}
    assert feed.data.requests == ["AAA/USD"]
    feed.data.status, feed.data.quotes["AAA/USD"] = 200, ("100", "100.02", T0)
    feed.now[0] = T0 + timedelta(seconds=5)
    feed.reader.new_tick()
    assert feed.reader.read("AAA/USD", None).bid == D("100")
    assert feed.data.requests == ["AAA/USD", "AAA/USD"]


@pytest.mark.parametrize(("row", "code"), [
    (None, "MISSING_OR_INVALID_QUOTES"),
    (("100.02", "100", T0), "REST_QUOTE_INVALID"),  # Crossed.
    (("100", "100.02", T0 + timedelta(seconds=1)), "REST_QUOTE_IN_FUTURE"),
])
def test_unusable_rest_quotes_are_unavailable_never_a_price(feed, row, code):
    if row is not None:
        feed.data.quotes["AAA/USD"] = row
    with pytest.raises(sc.LivePriceUnavailable) as caught:
        feed.reader.read("AAA/USD", {"bid": "1", "ask": "0.5", "quote_at": T0.isoformat()})
    assert [a["code"] for a in caught.value.attempts] == ["STREAM_QUOTE_INVALID", code]


def test_the_rest_read_is_declared_a_research_read():
    seen = []

    def latest(market, symbols, kind):
        seen.append((market, symbols, kind, current_priority()))
        raise RuntimeError("fixture transport fault")

    reader = sc.LivePriceReader(SimpleNamespace(_latest=latest), clock=lambda: T0)
    with pytest.raises(sc.LivePriceUnavailable) as caught:
        reader.read("AAA/USD", None)
    assert seen == [("CRYPTO", ("AAA/USD",), "quotes", RESEARCH)]
    assert caught.value.attempts[-1]["code"] == "RuntimeError"


# --- Admission end to end (real V3 intake, mock Jev, admission SQL, fake venue) ---------------

def two_slots(now):
    """An older and a newer hourly slot that both accept a report generated ``now``."""
    latest = HOURLY.latest_at_or_before(now)
    if now - latest >= timedelta(minutes=1):
        return latest, latest + timedelta(hours=1)
    return latest - timedelta(hours=1), latest


def publish_v3(mx, picks, *, run_slot, agent_id="claude"):
    """Report V3 through intake, the mock Jev and publication; the selected packets."""
    engine, venue, receipts = mx

    def approve(request):
        body = strict_json(request.content)
        quality = set(body["questions"]) == set(QUALITY.questions)
        return httpx.Response(200, json=quality_response() if quality else response())

    reviewer = JevReviewer(
        receipts, ReliabilityPolicy("SYSTEM_CHECK_FIXTURE", 10, 2, 0.01, 1000, 30),
        transport=httpx.MockTransport(approve), key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now, sleep=no_sleep,
    )
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    raw = report_v3(picks, now=venue.now, agent_id=agent_id, run_slot=run_slot.isoformat(),
                    valid_until=(venue.now + timedelta(minutes=30)).isoformat())
    result = cycle.start_report(raw, max_seconds=86400, v3=v3_intake(
        universe={p["symbol"] for p in picks}, schedule=HOURLY, now=venue.now))
    assert result["contender_count"] == len(picks)
    asyncio.run(cycle.tick(cycle_of(raw)))
    selected = cycle.approved_packets(cycle_of(raw))
    assert [p["symbol"] for p in selected] == [p["symbol"] for p in picks]
    for packet in selected:
        classify(engine, packet["symbol"])
    return {p["symbol"]: p for p in selected}


def v3_pick(index, symbol, now, *, agent="100.50", levels=None):
    value = pick(index, symbol, now=now, agent_current_price=agent)
    if levels is not None:
        value["levels"] = levels
    return value


def at_price(bid, ask, *, at, last=None):
    """A live-quote callable standing in for the runtime's reader; records its calls."""
    calls = []

    def read(symbol):
        calls.append(symbol)
        return sc.LiveQuote(symbol, D(bid), D(ask), at, sc.STREAM_SOURCE, at,
                            D(last) if last else None, at if last else None, "9" if last else None)

    read.calls = calls
    return read


def never(symbol):
    raise AssertionError("no live price may be read here")


def rows(engine, kind):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (kind,)).fetchall()


def event_count(engine):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM lab.managed_events").fetchone()["n"]


def setup_of(engine, receipt_id):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT * FROM lab.managed_setups WHERE receipt_id=%s",
                            (receipt_id,)).fetchone()


def state(engine, sid):
    return engine._load(sid)[1]


DEFAULT = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
TIGHT = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "97.5", "target": "106"}
SHORT = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "98.2", "target": "104"}
# symbol: (agent price, levels, bid, ask, last, refusal or None, entry type)
CASES = {
    "PUL/USD": ("100.50", DEFAULT, "100.49", "100.51", None, None, "PULLBACK"),
    "IMM/USD": ("100.10", DEFAULT, "100.09", "100.11", None, None, "IMMEDIATE"),
    "BRK/USD": ("99.50", DEFAULT, "99.49", "99.51", None, "BREAKOUT_NOT_ENABLED", "BREAKOUT"),
    "MIS/USD": ("100.50", DEFAULT, "106.00", "106.02", None, "PRICE_MISMATCH", "PULLBACK"),
    "BID/USD": ("98.00", TIGHT, "97.50", "97.60", None, "STOP_ALREADY_HIT", "BREAKOUT"),
    "LST/USD": ("98.00", TIGHT, "97.60", "97.70", "97.50", "STOP_ALREADY_HIT", "BREAKOUT"),
    "STP/USD": ("100.50", SHORT, "100.49", "100.51", None, "STOP_DISTANCE_BELOW_MINIMUM", None),
}


def test_pullbacks_and_immediates_are_admitted_every_refusal_is_final_and_recorded(mx):
    engine, venue, _ = mx
    now = venue.now
    old, _ = two_slots(now)
    selected = publish_v3(mx, [v3_pick(i, symbol, now, agent=agent, levels=levels)
                               for i, (symbol, (agent, levels, *_)) in enumerate(CASES.items())],
                          run_slot=old)
    for symbol, (agent, _, bid, ask, last, code, kind) in CASES.items():
        packet, live = selected[symbol], at_price(bid, ask, at=now, last=last)
        if code is None:
            sid = engine.admit(packet, live_quote=live)
            current = state(engine, sid)
            assert current["state"] == "WATCHING" and current["entry_type"] == kind
            check = current["system_check"]
            assert (check["result"], check["code"], check["entry_type"]) == ("PASSED", None, kind)
            assert check["live"]["bid"] == bid and check["live"]["ask"] == ask
            assert check["agent_current_price"] == agent
            assert set(check["checks"].values()) == {"PASS"}
            continue
        with pytest.raises(sc.AdmissionRefused) as caught:
            engine.admit(packet, live_quote=live)
        assert caught.value.code == code, symbol
        assert setup_of(engine, packet["receipt_id"]) is None  # No setup, no symbol slot.
        evidence = caught.value.details["system_check"]
        assert (evidence["result"], evidence["code"], evidence["entry_type"]) == (
            "REFUSED", code, kind)
        if code == "STOP_DISTANCE_BELOW_MINIMUM":
            assert live.calls == [] and evidence["live"] is None  # Levels alone refuse it.
            assert evidence["stop_distance_fraction"] == "0.018981018981"
        else:
            assert live.calls == [symbol] and evidence["live"]["bid"] == bid
            assert evidence["live"]["last"] == last
    refusals = {r["body"]["symbol"]: r for r in rows(engine, "SYSTEM_CHECK_REFUSED")}
    assert set(refusals) == {s for s, case in CASES.items() if case[5]}
    for symbol, row in refusals.items():
        body = row["body"]
        assert row["setup_id"] is None and row["idempotency_key"] == (
            "system-check-refused:" + selected[symbol]["receipt_id"])
        assert body["reason"] == CASES[symbol][5] and body["run_slot"] == selected[symbol][
            "run_slot"]
        assert body["selection_event_seq"] == selected[symbol]["selection_event_seq"]
        assert body["system_check"]["code"] == body["reason"]
    # Final for the receipt: a later, passing price never admits it and records nothing more.
    before = event_count(engine)
    with pytest.raises(sc.AdmissionRefused, match="^PRICE_MISMATCH$"):
        engine.admit(selected["MIS/USD"], live_quote=never)
    assert event_count(engine) == before
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_missing_live_price_is_transient_and_records_nothing(mx):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    packet = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)["AAA/USD"]

    def unavailable(symbol):
        raise sc.LivePriceUnavailable([{"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_STALE"}])

    def broken(symbol):
        raise RuntimeError("reader defect")

    before = event_count(engine)
    for reader, attempts in (
        (unavailable, [{"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_STALE"}]),
        (broken, [{"source": "LIVE_PRICE_READER", "code": "RuntimeError"}]),
        (None, [{"source": "NONE", "code": "LIVE_PRICE_NOT_WIRED"}]),
    ):
        with pytest.raises(sc.AdmissionRefused) as caught:
            engine.admit(packet, live_quote=reader)
        evidence = caught.value.details["system_check"]
        assert caught.value.code == "LIVE_PRICE_UNAVAILABLE" and evidence["result"] == "RETRY"
        assert evidence["live_price_attempts"] == attempts
    assert event_count(engine) == before and setup_of(engine, packet["receipt_id"]) is None
    sid = engine.admit(packet, live_quote=at_price("100.49", "100.51", at=venue.now))
    assert state(engine, sid)["entry_type"] == "PULLBACK"


def test_a_refused_pick_spends_no_symbol_slot(mx):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    breakout = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now, agent="99.50")], run_slot=old)
    with pytest.raises(sc.AdmissionRefused, match="^BREAKOUT_NOT_ENABLED$"):
        engine.admit(breakout["AAA/USD"], live_quote=at_price("99.49", "99.51", at=venue.now))
    # Another agent's pick of the same coin in the same run still gets the slot.
    other = publish_v3(mx, [v3_pick(1, "AAA/USD", venue.now)], run_slot=old, agent_id="instinct")
    sid = engine.admit(other["AAA/USD"], live_quote=at_price("100.49", "100.51", at=venue.now))
    assert [s["setup_id"] for s in engine.store.active()] == [sid]


def test_v2_admission_reads_no_live_price_and_is_unchanged(mx):
    engine, _, _ = mx
    cycle, cycle_id = reviewed_cycle(mx, 1)
    packet = cycle.approved_packets(cycle_id)[0]
    assert "report_schema_version" not in packet
    classify(engine, packet["symbol"])
    # A 1.2% stop, which the V3 check would refuse, is admitted for V2 exactly as before.
    sid = engine.admit(packet, live_quote=never)
    current = state(engine, sid)
    assert current["state"] == "WATCHING"
    assert "system_check" not in current and "entry_type" not in current
    assert rows(engine, "SYSTEM_CHECK_REFUSED") == []


def test_admission_refuses_a_pick_from_a_superseded_run_before_any_live_read(mx):
    engine, venue, _ = mx
    old, new = two_slots(venue.now)
    older = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)["AAA/USD"]
    newer = publish_v3(mx, [v3_pick(1, "BBB/USD", venue.now)], run_slot=new)["BBB/USD"]
    with pytest.raises(sc.AdmissionRefused) as caught:
        engine.admit(older, live_quote=never)
    assert caught.value.code == "SUPERSEDED_BY_NEW_RESEARCH"
    assert caught.value.details == {
        "run_slot": older["run_slot"], "superseded_by_run_slot": new.isoformat(),
        "supersession_rule": "RESEARCH_RUN_SUPERSESSION_V1"}
    assert setup_of(engine, older["receipt_id"]) is None
    assert engine.admit(newer, live_quote=at_price("100.49", "100.51", at=venue.now))


# --- The runtime: live price, retries, declines and retirement -------------------------------

@pytest.fixture
def market(mx):
    data = MarketData()
    source = AlpacaMarketSource(CREDENTIALS, SOURCE_POLICY, lambda: mx[1].now,
                                transport=httpx.MockTransport(data.handle))
    yield SimpleNamespace(data=data, source=source)
    source.close()


def system_runtime(mx, market, symbols):
    engine, venue, _ = mx
    run = ManagedRuntime(engine, Research(), market.source, CREDENTIALS,
                         engineering_runtime_policy(), clock=lambda: venue.now,
                         reviewer_heartbeat=lambda: True)
    run.connected = run.research_healthy = True
    assert run.reconcile_once()
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = set(symbols)
    assert run.ready()
    return run


def stream(run, symbol, bid="100.49", ask="100.51", *, at):
    run.market_message("CRYPTO", {"T": "q", "S": symbol, "bp": bid, "ap": ask,
                                  "t": at.isoformat()})


def bodies(engine, kind):
    return [r["body"] for r in rows(engine, kind)]


def test_a_tick_without_v3_work_adds_no_ledger_read():
    from tests.test_managed_runtime import ready, runtime

    run = runtime()  # A watching V2-style setup and no selection: nothing a run could retire.

    def connect():
        raise AssertionError("NO_LEDGER_READ_EXPECTED")

    run.execution.repo = SimpleNamespace(connect=connect)
    ready(run)
    run.execution_once()
    assert run.error is None and run.execution.managed and not run.latches.blocking()


def test_runtime_admits_on_a_fresh_stream_quote_without_a_rest_read(mx, market):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    packet = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)["AAA/USD"]
    run = system_runtime(mx, market, ["AAA/USD"])
    stream(run, "AAA/USD", at=venue.now - timedelta(seconds=5))
    run.execution_once()
    [setup] = engine.store.active()
    assert str(setup["receipt_id"]) == packet["receipt_id"]
    check = setup["state"]["system_check"]
    assert check["live"]["quote_source"] == "ALPACA_STREAM" and market.data.requests == []
    assert setup["state"]["entry_type"] == "PULLBACK" and run.error is None


def test_a_stale_stream_quote_costs_exactly_one_rest_read_then_admits(mx, market):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)
    run = system_runtime(mx, market, ["AAA/USD"])
    stream(run, "AAA/USD", at=venue.now - timedelta(seconds=6))  # Unchanged for six seconds.
    market.data.quotes["AAA/USD"] = ("100.49", "100.51", venue.now - timedelta(seconds=6))
    for _ in range(3):
        run.execution_once()
    assert market.data.requests == ["AAA/USD"]
    [setup] = engine.store.active()
    live = setup["state"]["system_check"]["live"]
    assert live["quote_source"] == "ALPACA_REST_LATEST_QUOTE" and live["quote_age_seconds"] == "6.0"
    assert bodies(engine, "RUNTIME_ADMISSION_REFUSED") == []


def test_no_live_price_is_retried_each_tick_and_never_declined(mx, market):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    packet = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)["AAA/USD"]
    run = system_runtime(mx, market, ["AAA/USD"])
    market.data.status = 503
    run.execution_once()
    run.execution_once()  # Retried; the REST read waits its five seconds.
    assert market.data.requests == ["AAA/USD"]
    [refused] = bodies(engine, "RUNTIME_ADMISSION_REFUSED")
    seq = packet["selection_event_seq"]
    assert (refused["reason"], refused["selection_event_seq"]) == ("LIVE_PRICE_UNAVAILABLE", seq)
    evidence = refused["system_check"]
    assert evidence["result"] == "RETRY" and [a["code"] for a in
                                              evidence["live_price_attempts"]] == [
        "STREAM_QUOTE_MISSING", "SCAN_HTTP_503"]
    assert bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []
    assert bodies(engine, "SYSTEM_CHECK_REFUSED") == []
    assert [p["selection_event_seq"] for p in run._selected_packets()] == [seq]
    assert engine.store.active() == [] and run.error is None
    stream(run, "AAA/USD", at=venue.now)  # The quote arrives: the next tick admits.
    run.execution_once()
    [setup] = engine.store.active()
    assert str(setup["receipt_id"]) == packet["receipt_id"]
    assert market.data.requests == ["AAA/USD"]


def test_a_permanent_refusal_is_declined_with_its_evidence_and_never_retried(mx, market):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    packet = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now, agent="99.50")],
                        run_slot=old)["AAA/USD"]
    run = system_runtime(mx, market, ["AAA/USD"])
    stream(run, "AAA/USD", "99.49", "99.51", at=venue.now)
    for _ in range(3):
        run.execution_once()
    seq = packet["selection_event_seq"]
    [recorded] = bodies(engine, "SYSTEM_CHECK_REFUSED")
    [refused] = bodies(engine, "RUNTIME_ADMISSION_REFUSED")
    [declined] = bodies(engine, "RESEARCH_ADMISSION_DECLINED")
    assert refused == {"runtime_id": run.runtime_id, "selection_event_seq": seq,
                       "reason": "BREAKOUT_NOT_ENABLED", "system_check": recorded["system_check"]}
    assert declined == {"cycle_id": packet["cycle_id"], "item_key": packet["item_key"],
                        "revision": 1, "receipt_id": packet["receipt_id"],
                        "selection_event_seq": seq, "reason": "BREAKOUT_NOT_ENABLED",
                        "system_check": recorded["system_check"]}
    assert recorded["system_check"]["entry_type"] == "BREAKOUT"
    assert recorded["system_check"]["live"]["bid"] == "99.49"
    assert run._selected_packets() == [] and "AAA/USD" not in run._desired_symbols("CRYPTO")
    assert engine.store.active() == [] and run.error is None


def test_a_new_run_retires_only_the_previous_runs_untriggered_picks(mx, market):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    previous = publish_v3(mx, [v3_pick(i, symbol, now) for i, symbol in enumerate(
        ("AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD"))], run_slot=old)
    live = at_price("100.49", "100.51", at=now)
    watching = engine.admit(previous["AAA/USD"], live_quote=live)
    opened = engine.admit(previous["CCC/USD"], live_quote=live)
    working = engine.admit(previous["DDD/USD"], live_quote=live)
    touch = observation(mx, trade_price="100", bid="99.99", ask="100.01")
    assert engine.observe_trigger(opened, touch)["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == "CCC/USD")
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(opened, observation(mx))
    assert state(engine, opened)["state"] == "OPEN"
    assert engine.observe_trigger(working, touch)["outcome"] == "APPROVED"
    working_state = state(engine, working)["state"]
    assert working_state not in {"WATCHING", "OPEN", "CLOSED", "INVALIDATED"}
    # V2: a watching setup and a selection not yet admitted.
    v2_cycle, v2_cycle_id = reviewed_cycle(mx, 1)
    v2_packet = v2_cycle.approved_packets(v2_cycle_id)[0]
    classify(engine, v2_packet["symbol"])
    v2_setup = engine.admit(v2_packet)
    v2_pending_cycle, v2_pending_id = reviewed_cycle(mx, 3)
    v2_pending = v2_pending_cycle.approved_packets(v2_pending_id)[0]
    run = system_runtime(mx, market, ["AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD", "EEE/USD"])
    run._retire_superseded_research()  # No newer run yet: nothing to retire.
    assert bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []
    newer = publish_v3(mx, [v3_pick(9, "EEE/USD", now)], run_slot=new)["EEE/USD"]
    before = event_count(engine)
    run._retire_superseded_research()
    # One REVOKE (with its INVALIDATED revision) and one decline, nothing else.
    assert event_count(engine) == before + 3
    retired = state(engine, watching)
    assert (retired["state"], retired["revoked"], retired["revocation_reason"]) == (
        "INVALIDATED", True, "SUPERSEDED_BY_NEW_RESEARCH")
    [revoke] = [r for r in rows(engine, "REVOKE") if r["setup_id"] == watching]
    assert revoke["body"] == {"reason": "SUPERSEDED_BY_NEW_RESEARCH",
                              "run_slot": previous["AAA/USD"]["run_slot"],
                              "superseded_by_run_slot": new.isoformat(),
                              "supersession_rule": "RESEARCH_RUN_SUPERSESSION_V1"}
    assert revoke["idempotency_key"] == f"research:superseded:setup:{watching}"
    [declined] = bodies(engine, "RESEARCH_ADMISSION_DECLINED")
    pending = previous["BBB/USD"]
    assert declined == {"cycle_id": pending["cycle_id"], "item_key": pending["item_key"],
                        "revision": 1, "receipt_id": pending["receipt_id"],
                        "selection_event_seq": pending["selection_event_seq"],
                        "reason": "SUPERSEDED_BY_NEW_RESEARCH", "run_slot": pending["run_slot"],
                        "superseded_by_run_slot": new.isoformat(),
                        "supersession_rule": "RESEARCH_RUN_SUPERSESSION_V1"}
    # The open position, the working entry and V2 are untouched.
    assert state(engine, opened)["state"] == "OPEN" and not state(engine, opened).get("revoked")
    assert state(engine, working)["state"] == working_state
    assert not state(engine, working).get("revoked")
    assert state(engine, v2_setup)["state"] == "WATCHING"
    assert not state(engine, v2_setup).get("revoked")
    assert [p["selection_event_seq"] for p in run._selected_packets()] == [
        v2_pending["selection_event_seq"], newer["selection_event_seq"]]
    # Idempotent: later passes and ticks retire nothing again.
    run._retire_superseded_research()
    assert event_count(engine) == before + 3
    stream(run, "EEE/USD", at=now)
    run.execution_once()
    admitted = {s["symbol"] for s in engine.store.active()}
    assert admitted == {"CCC/USD", "DDD/USD", v2_packet["symbol"], "EEE/USD"}
    assert len(rows(engine, "REVOKE")) == 1
    assert len(bodies(engine, "RESEARCH_ADMISSION_DECLINED")) == 1
    assert run.error is None
    assert verify_events(engine.repo.export_events())["valid"]
