"""``CRYPTO_STOP_BREACH_V2`` (owner, 2026-09-29): the stop-limit fallback sells at market only
after a print at or below the stop, or a bid held there for 15 s, and 5 s later.

The fixture venue's setups are admitted at entry trigger 100, max entry 100.10, stop 95 and
target 111; the native stop-limit rests at stop 95, limit 94.99. The default observation prints
at 100 with the bid at 99.99. Disposable PostgreSQL and the fake paper venue only.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import stop_breach
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_service import STATE_FIELDS
from tests.maintenance_fixtures import pre_trade_plan_admission as pre_trade_plan_admission
from tests.test_crypto_trigger import rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import admit_enter, observation, packet
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_system_check import market as market  # noqa: F401
from tests.test_system_check import stream, system_runtime

# Every setup here is admitted as before package trade-plan (CRYPTO_MAINTENANCE_V3 or earlier,
# the one-tick stop-limit); the new versions are tests/test_trade_plan_*.py.
pytestmark = pytest.mark.usefixtures("pre_trade_plan_admission")

BID_AT_STOP = {"bid": "95", "ask": "95.02"}  # A fresh quote at the stop; the print stays at 100.


def opened(mx, symbol="BTC/USD"):
    """Admitted under the current versions, filled, and protected by its native stop-limit."""
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, symbol)
    [entry] = [o for o in venue.orders_of("buy") if o["symbol"] == symbol]
    assert engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    [stop] = [o for o in venue.orders_of("sell", "stop_limit") if o["symbol"] == symbol]
    return sid, stop


def tick(mx, sid, seconds=0, **overrides):
    """One protection pass ``seconds`` later on a fresh observation (print and quote now)."""
    engine, venue, _ = mx
    venue.now += timedelta(seconds=seconds)
    return engine.manage(sid, observation(mx, **overrides))


def marks_history(engine, sid):
    """Each distinct ``stop_breach_marks`` value the setup's states recorded, in order."""
    history = []
    for row in rows(engine, "STATE", sid):
        marks = row["body"].get("stop_breach_marks")
        if marks and (not history or history[-1] != marks):
            history.append(marks)
    return history


def market_sells(venue):
    return venue.orders_of("sell", "market")


def test_admission_records_the_version_for_crypto_setups_only(mx):
    engine, _, _ = mx
    crypto, stock = engine.admit(packet(mx, "BTC/USD")), engine.admit(packet(mx, "SPY"))
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    assert state(engine, crypto)["stop_breach_version"] == stop_breach.STOP_BREACH_VERSION
    assert state(engine, v3)["stop_breach_version"] == stop_breach.STOP_BREACH_VERSION
    assert "stop_breach_version" not in state(engine, stock)
    assert {"stop_breach_version", "stop_breach_marks", "stop_breached_at",
            "stop_breach_evidence"} <= STATE_FIELDS


@pytest.mark.parametrize("version", ["V1", "V2"])
def test_a_momentary_bid_touch_that_recovers_sells_under_v1_only(mx, monkeypatch, version):
    """The live closes' case: the bid touches the stop without a trade and recovers."""
    engine, venue, _ = mx
    if version == "V1":  # Admitted before the version: nothing recorded, V1's fallback.
        monkeypatch.setattr(stop_breach, "admission_fields", lambda packet: {})
    sid, stop = opened(mx)
    tick(mx, sid, 1, **BID_AT_STOP)
    tick(mx, sid, 1)  # The next quote is back above the stop.
    for _ in range(25):  # Past V1's 2 s and V2's 15 + 5 s.
        plan = tick(mx, sid, 1)
    current = state(engine, sid)
    if version == "V1":
        assert "stop_breach_version" not in current and "stop_breach_marks" not in current
        assert stop["status"] == "canceled" and len(market_sells(venue)) == 1
        assert current["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
        assert current["stop_breached_at"] == (venue.now - timedelta(seconds=26)).isoformat()
        assert not rows(engine, stop_breach.ESTABLISHED_EVENT, sid)
        return
    assert plan.state == "PROTECTED" and stop["status"] == "new" and not market_sells(venue)
    assert current.get("exit_requested") is None and current.get("stop_breached_at") is None
    marks = marks_history(engine, sid)
    touched = (venue.now - timedelta(seconds=26)).isoformat()
    # Created at the open, marked by the touch, cleared by the next fresh quote; nothing since.
    assert [m["bid_since"] for m in marks] == [None, touched, None]
    assert marks[1]["bid"] == "95" and current["stop_breach_marks"] == marks[-1]
    assert not rows(engine, stop_breach.ESTABLISHED_EVENT, sid)


def test_a_print_at_the_stop_with_the_stop_limit_unfilled_sells_at_market_after_5_s(mx):
    engine, venue, _ = mx
    sid, stop = opened(mx)
    venue.now += timedelta(seconds=5)
    printed_at = venue.now - timedelta(seconds=2)  # Delivered two seconds after the trade.
    engine.manage(sid, observation(mx, trade_price="95", trade_at=printed_at.isoformat(),
                                   trade_id="t-95", bid="95.20", ask="95.25"))
    established = venue.now
    current = state(engine, sid)
    assert current["stop_breached_at"] == established.isoformat()
    evidence = current["stop_breach_evidence"]
    assert evidence == {
        "version": "CRYPTO_STOP_BREACH_V2",
        "lifecycle_id": current["lifecycle_id"],
        "stop": "95",
        "stop_since": current["stop_breach_marks"]["stop_since"],
        "breach_evidence": "TRADE_PRINT",
        "evidence_at": printed_at.isoformat(),
        "evidence_price": "95",
        "trade_id": "t-95",
        "print_age_seconds": "2.0",
        "established_at": established.isoformat(),
        "fallback_seconds": 5,
        "fallback_at": (established + timedelta(seconds=5)).isoformat(),
    }
    [event] = rows(engine, stop_breach.ESTABLISHED_EVENT, sid)
    assert event["body"] == evidence
    assert event["idempotency_key"] == (
        f"stop-breach-established:{sid}:{current['lifecycle_id']}:{evidence['stop_since']}")
    # The bid is back above the stop and the stop-limit has not filled: it keeps protecting.
    plan = tick(mx, sid, 4)
    assert plan.state == "PROTECTED" and stop["status"] == "new"
    plan = tick(mx, sid, 1)  # Five seconds after the breach, the position is still open.
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_LIMIT_NOT_FILLED")
    assert stop["status"] == "canceled"
    assert state(engine, sid)["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    plan = tick(mx, sid)
    assert (plan.state, plan.reason) == ("EXIT_REQUIRED", "STOP_LIMIT_NOT_FILLED")
    [close] = market_sells(venue)
    assert engine.ingest(venue.fill(close["id"], close["qty"], price="99.90"))
    tick(mx, sid)
    closed = state(engine, sid)
    assert (closed["state"], closed["reason"]) == ("CLOSED", "STOP_LIMIT_NOT_FILLED")
    assert len(rows(engine, stop_breach.ESTABLISHED_EVENT, sid)) == 1
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_filled_stop_limit_needs_no_fallback(mx):
    engine, venue, _ = mx
    sid, stop = opened(mx)
    tick(mx, sid, 1, trade_price="94.99", bid="94.98", ask="95")
    assert state(engine, sid)["stop_breach_evidence"]["breach_evidence"] == "TRADE_PRINT"
    assert engine.ingest(venue.fill(stop["id"], stop["qty"], price="94.99"))
    tick(mx, sid, 5, trade_price="94.99", bid="94.98", ask="95")
    closed = state(engine, sid)
    assert closed["state"] == "CLOSED" and closed["reason"] == "BROKER_EXIT"
    assert not market_sells(venue)


def test_a_bid_held_at_or_below_the_stop_for_15_s_is_the_breach(mx):
    engine, venue, _ = mx
    sid, stop = opened(mx)
    tick(mx, sid, 1, bid="94.90", ask="94.95")
    held_since = venue.now
    marks = state(engine, sid)["stop_breach_marks"]
    assert (marks["bid_since"], marks["bid"], marks["bid_quote_at"]) == (
        held_since.isoformat(), "94.90", held_since.isoformat())
    # A stale quote (6 s old) neither clears nor restarts the mark, whatever its bid.
    venue.now += timedelta(seconds=7)
    engine.manage(sid, observation(mx, bid="96", ask="96.05",
                                   quote_at=(venue.now - timedelta(seconds=6)).isoformat()))
    assert state(engine, sid)["stop_breach_marks"]["bid_since"] == held_since.isoformat()
    plan = tick(mx, sid, 7, bid="94.90", ask="94.95")  # 14 s: not yet.
    assert plan.state == "PROTECTED" and state(engine, sid).get("stop_breached_at") is None
    tick(mx, sid, 1, bid="94.85", ask="94.90")  # 15 s.
    current = state(engine, sid)
    assert current["stop_breached_at"] == venue.now.isoformat()
    evidence = current["stop_breach_evidence"]
    assert {k: evidence[k] for k in ("breach_evidence", "evidence_at", "evidence_price",
                                     "held_since", "held_seconds", "first_bid", "stop")} == {
        "breach_evidence": "BID_HELD", "evidence_at": venue.now.isoformat(),
        "evidence_price": "94.85", "held_since": held_since.isoformat(), "held_seconds": "15.0",
        "first_bid": "94.90", "stop": "95"}
    assert [e["body"] for e in rows(engine, stop_breach.ESTABLISHED_EVENT, sid)] == [evidence]
    assert tick(mx, sid, 4, bid="94.85", ask="94.90").state == "PROTECTED"
    plan = tick(mx, sid, 1, bid="94.85", ask="94.90")
    assert plan.reason == "STOP_LIMIT_NOT_FILLED" and stop["status"] == "canceled"
    tick(mx, sid, 0, bid="94.85", ask="94.90")
    assert len(market_sells(venue)) == 1


def test_a_stop_raise_discards_the_marks_of_the_old_stop(mx):
    engine, venue, _ = mx
    sid, _ = opened(mx)
    tick(mx, sid, 1, trade_price="95", bid="95.10", ask="95.15")
    breached = state(engine, sid)
    assert breached["stop_breach_evidence"]["stop"] == "95"
    # Two seconds later the price is back at 98 and the stop is raised to 96 (as a maintenance
    # raise records it; this setup replaces its stop-limit by cancel-then-place).
    venue.now += timedelta(seconds=2)
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "OPEN", stop="96")
    tick(mx, sid, 0, bid="98", ask="98.05")
    raised = state(engine, sid)
    assert raised["stop_breached_at"] is None and raised["stop_breach_evidence"] is None
    assert raised["stop_breach_marks"] == {"stop": "96", "stop_since": venue.now.isoformat(),
                                           "bid_since": None, "bid": None, "bid_quote_at": None}
    for _ in range(6):  # Past five seconds after the old stop's breach: nothing sells.
        tick(mx, sid, 1, bid="98", ask="98.05")
    assert state(engine, sid).get("exit_requested") is None and not market_sells(venue)
    [native] = [o for o in venue.orders_of("sell", "stop_limit") if o["status"] == "new"]
    assert (native["stop_price"], native["limit_price"]) == ("96", "95.99")
    # The new stop is measured afresh: a bid at 96 starts its own 15 s.
    tick(mx, sid, 1, bid="96", ask="96.05")
    since = venue.now
    assert tick(mx, sid, 14, bid="96", ask="96.05").state == "PROTECTED"
    tick(mx, sid, 1, bid="95.99", ask="96.02")
    events = [e["body"] for e in rows(engine, stop_breach.ESTABLISHED_EVENT, sid)]
    assert [(e["stop"], e["breach_evidence"]) for e in events] == [
        ("95", "TRADE_PRINT"), ("96", "BID_HELD")]
    assert events[1]["held_since"] == since.isoformat()
    assert tick(mx, sid, 5, bid="95.99", ask="96.02").reason == "STOP_LIMIT_NOT_FILLED"


def test_the_runtime_hands_a_streamed_print_through_the_stop_to_the_protection_pass(mx, market):
    engine, venue, _ = mx
    sid, stop = opened(mx)
    run = system_runtime(mx, market, ["BTC/USD"])
    venue.now += timedelta(seconds=1)
    stream(run, "BTC/USD", "95.20", "95.25", at=venue.now)
    run.market_message("CRYPTO", {"T": "t", "S": "BTC/USD", "p": "95", "i": 7,
                                  "t": (venue.now - timedelta(seconds=1)).isoformat()})
    run.execution_once()
    evidence = state(engine, sid)["stop_breach_evidence"]
    assert (evidence["breach_evidence"], evidence["evidence_price"], evidence["trade_id"]) == (
        "TRADE_PRINT", "95", "7")
    assert stop["status"] == "new"
    venue.now += timedelta(seconds=5)
    stream(run, "BTC/USD", "95.20", "95.25", at=venue.now)
    run.execution_once()
    assert state(engine, sid)["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    assert stop["status"] == "canceled" and run.error is None


def test_a_print_before_the_first_quote_after_a_gap_is_no_fresh_quote(mx, market):
    """A market gap empties the runtime's observations. When the coin's first message after it
    is a trade, the observation carries a print and no quote: the protection pass reads that as
    no fresh quote (the native stop keeps protecting), not as a failure that latches."""
    engine, venue, _ = mx
    sid, stop = opened(mx)
    run = system_runtime(mx, market, ["BTC/USD"])
    venue.now += timedelta(seconds=1)
    run.market_message("CRYPTO", {"T": "t", "S": "BTC/USD", "p": "100", "i": 7,
                                  "t": venue.now.isoformat()})
    [setup] = [s for s in engine.store.active() if s["setup_id"] == sid]
    seen_row = run.observation(setup)
    assert seen_row["trade_price"] == "100" and "quote_at" not in seen_row
    run.execution_once()
    assert run.error is None and not run.latches.blocking()
    current = state(engine, sid)
    assert current["state"] == "OPEN" and current.get("exit_requested") is None
    assert stop["status"] == "new" and not market_sells(venue)


# --- The rule itself (``stop_breach.evaluate``, pure) ---------------------------------------


def v2_state(**fields):
    return {"stop_breach_version": stop_breach.STOP_BREACH_VERSION, "stop": "95",
            "lifecycle_id": "L", "state": "OPEN", **fields}


def seen(now, **overrides):
    return {"trade_price": "100", "trade_at": now.isoformat(), "trade_id": "t",
            "bid": "99.99", "ask": "100.01", "quote_at": now.isoformat(), "feed_healthy": True,
            **overrides}


def test_the_first_measurement_records_the_stop_and_when_it_was_first_measured(mx):
    now = mx[1].now
    changes, breach = stop_breach.evaluate(v2_state(), observation=seen(now), bid=D("99.99"),
                                           now=now)
    assert breach is None and changes == {"stop_breach_marks": {
        "stop": "95", "stop_since": now.isoformat(), "bid_since": None, "bid": None,
        "bid_quote_at": None}}


@pytest.mark.parametrize("change,counts", [
    ({"trade_price": "95"}, True),  # At the stop.
    ({"trade_price": "95.01"}, False),  # Above it.
    ({"age": 5}, True),  # Five seconds old at evaluation.
    ({"age": 5.001}, False),
    ({"before_stop": True}, False),  # Traded before this stop was first measured.
    ({"feed_healthy": False}, False),
    ({"trade_at": "not a time"}, False),  # Unreadable: no evidence, and no fault.
    ({"trade_price": None}, False),
])
def test_what_a_print_proves(mx, change, counts):
    now = mx[1].now
    change = dict(change)
    traded = now - timedelta(seconds=change.pop("age", 1))
    since = now - timedelta(seconds=10)
    if change.pop("before_stop", False):
        since = traded + timedelta(milliseconds=1)  # This stop was first measured just after.
    marks = {"stop": "95", "stop_since": since.isoformat(), "bid_since": None, "bid": None,
             "bid_quote_at": None}
    observed = seen(now, **{"trade_price": "94", "trade_at": traded.isoformat(), **change})
    changes, breach = stop_breach.evaluate(v2_state(stop_breach_marks=marks),
                                           observation=observed, bid=None, now=now)
    assert (breach is not None) == counts
    if counts:
        assert breach["breach_evidence"] == "TRADE_PRINT" and changes["stop_breached_at"]
    else:
        assert changes == {}


def test_a_stale_quote_neither_starts_nor_clears_and_a_breach_is_final(mx):
    now = mx[1].now
    since = now - timedelta(seconds=10)
    marks = {"stop": "95", "stop_since": since.isoformat(), "bid_since": None, "bid": None,
             "bid_quote_at": None}
    # No fresh quote (bid None): nothing starts...
    assert stop_breach.evaluate(v2_state(stop_breach_marks=marks), observation=seen(now),
                                bid=None, now=now) == ({}, None)
    held = {**marks, "bid_since": since.isoformat(), "bid": "94", "bid_quote_at": None}
    # ...and nothing clears.
    assert stop_breach.evaluate(v2_state(stop_breach_marks=held), observation=seen(now),
                                bid=None, now=now) == ({}, None)
    # A fresh bid above the stop clears the candidate.
    changes, _ = stop_breach.evaluate(v2_state(stop_breach_marks=held), observation=seen(now),
                                      bid=D("95.01"), now=now)
    assert changes == {"stop_breach_marks": marks}
    # Once established for this stop, nothing more is measured.
    final = v2_state(stop_breach_marks=held, stop_breached_at=since.isoformat(),
                     stop_breach_evidence={"breach_evidence": "BID_HELD"})
    assert stop_breach.evaluate(final, observation=seen(now, trade_price="90"), bid=D("90"),
                                now=now) == ({}, None)


def test_a_changed_stop_discards_a_candidate_and_a_breach(mx):
    now = mx[1].now
    since = now - timedelta(seconds=30)
    held = {"stop": "95", "stop_since": since.isoformat(), "bid_since": since.isoformat(),
            "bid": "94", "bid_quote_at": since.isoformat()}
    raised = v2_state(stop="96", stop_breach_marks=held, stop_breached_at=since.isoformat(),
                      stop_breach_evidence={"breach_evidence": "BID_HELD"})
    changes, breach = stop_breach.evaluate(raised, observation=seen(now), bid=D("95.50"),
                                           now=now)
    assert breach is None and changes["stop_breached_at"] is None
    assert changes["stop_breach_evidence"] is None
    # The bid at 95.50 is at or below the new stop: its own mark starts now, not 30 s ago.
    assert changes["stop_breach_marks"] == {
        "stop": "96", "stop_since": now.isoformat(), "bid_since": now.isoformat(),
        "bid": "95.50", "bid_quote_at": now.isoformat()}
    # The same stop written differently is the same stop.
    same = v2_state(stop="95.0", stop_breach_marks={**held, "bid_since": None})
    assert stop_breach.evaluate(same, observation=seen(now), bid=D("99"), now=now) == ({}, None)
