"""Close attribution: the entry window governs only an entry that never filled.

Fixture reproduction of the real-broker defect recorded on 2026-09-25 in
``artifacts/first-managed-trade-2026-09-25/`` (setup f059e98a-c2a3-499b-a325-d877ff5151cf):
an ENGINEERING_TEST crypto setup filled inside its 60-minute entry window, stayed OPEN past
it, and was emptied by the protective STOP_LIMIT_NOT_FILLED exit; on the tick that saw zero
quantity the entry-expiry rule relabelled the exit request ENTRY_EXPIRED and the setup closed
under that reason. Disposable PostgreSQL and the fake paper venue only: fixture evidence, not
broker acceptance.
"""

import re
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import psycopg
import pytest
from psycopg.rows import dict_row

from catalyst_lab.acceptance_evidence import collect_acceptance_evidence
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_analytics import managed_daily_rollups, paginated_managed_results
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_engineering import classify, enroll, selected
from tests.test_managed_engineering import ex as ex
from tests.test_managed_execution import admit_enter, observation, packet
from tests.test_managed_execution import mx as mx

WINDOW = timedelta(minutes=1)  # A short entry window (the packet's expires_at).
GAP = {"trade_price": "94", "bid": "94", "ask": "94.01"}  # Through the stop at 95.


def setup_events(engine, sid, kind="STATE"):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT event_seq,body FROM lab.managed_events
            WHERE setup_id=%s AND kind=%s ORDER BY event_seq""",
            (sid, kind),
        ).fetchall()


def first_fill_seq(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT min(event_seq) AS seq FROM lab.managed_fills WHERE setup_id=%s", (sid,)
        ).fetchone()["seq"]


def active_reservation(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM lab.managed_active_reservations WHERE setup_id=%s", (sid,)
        ).fetchone()


def closed_reason(engine, sid):
    state = engine._load(sid)[1]
    assert state["state"] == "CLOSED", state
    assert not active_reservation(engine, sid)
    return state["reason"]


def entry_of(venue, decision):
    return next(
        o for o in venue.orders_of("buy")
        if o["client_order_id"] == decision["payload"]["client_order_id"]
    )


def short_window(mx, symbol="BTC/USD"):
    engine, venue, _ = mx
    return engine.admit(packet(mx, symbol, expires_at=venue.now + WINDOW))


def open_past_window(mx, symbol="BTC/USD"):
    """Triggered, filled and protected inside a one-minute entry window, then still OPEN and
    unexited after the window has closed."""
    engine, venue, _ = mx
    sid = short_window(mx, symbol)
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "APPROVED"
    entry = entry_of(venue, decision)
    assert engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    venue.now += 2 * WINDOW
    engine.manage(sid, observation(mx))
    state = engine._load(sid)[1]
    assert state["state"] == "OPEN" and state.get("exit_requested") is None
    return sid


def market_close(engine, venue, *, price="100"):
    [close] = [o for o in venue.orders_of("sell", "market") if o["status"] == "new"]
    assert engine.ingest(venue.fill(close["id"], close["qty"], price=price))
    return close


# --- Reproduction of the 2026-09-25 real-broker defect ------------------------------------


def test_protective_stop_exit_after_the_entry_window_closes_with_its_exit_reason(ex):
    engine, venue, _ = ex
    classify(engine)
    # Enrollment 2's shape: ENGINEERING_TEST, BTC/USD, a 60-minute entry window.
    sid = engine.admit(selected(engine.repo, enroll(engine.repo, minutes=60)))
    setup = engine._load(sid)[0]
    decision = engine.observe_trigger(sid, observation(ex))
    assert decision["outcome"] == "APPROVED"
    entry = entry_of(venue, decision)
    # Alpaca takes the crypto fee in the asset: the position is smaller than the order.
    fee = (D(entry["qty"]) * D("0.0025")).quantize(D("0.0001"))
    assert engine.ingest(venue.fill(entry["id"], entry["qty"], price="100.05", fee_qty=str(fee)))
    engine.manage(sid, observation(ex))
    [stop] = venue.orders_of("sell", "stop_limit")
    assert engine._load(sid)[1]["state"] == "OPEN" and stop["status"] == "new"

    # The entry window closes while the position is OPEN under its native stop-limit.
    venue.now = setup["expires_at"] + WINDOW
    engine.manage(sid, observation(ex))
    state = engine._load(sid)[1]
    assert state["state"] == "OPEN" and state.get("exit_requested") is None
    assert stop["status"] == "new"

    # The price gaps through the stop; the stop-limit triggers but does not fill. After the
    # grace the controller cancels it and sends an authorized market exit (CRYPTO_STOP_BREACH_V2:
    # the print through the stop is the breach, and the grace is 5 s).
    engine.manage(sid, observation(ex, **GAP))
    venue.now += timedelta(seconds=5)
    engine.manage(sid, observation(ex, **GAP))
    assert stop["status"] == "canceled"
    assert engine._load(sid)[1]["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    engine.manage(sid, observation(ex, **GAP))
    [close] = venue.orders_of("sell", "market")
    # Two market fills bring the quantity to zero, as at the broker.
    first = D("0.0100")
    assert engine.ingest(venue.fill(close["id"], str(first), price="93.95"))
    engine.manage(sid, observation(ex, **GAP))
    assert engine._load(sid)[1]["state"] == "OPEN"
    assert engine.ingest(venue.fill(close["id"], str(D(close["qty"]) - first), price="93.90"))
    engine.manage(sid, observation(ex, **GAP))

    assert closed_reason(engine, sid) == "STOP_LIMIT_NOT_FILLED"
    assert venue._position_rows() == []
    assert not [o for o in venue.orders.values() if o["status"] in {"new", "pending_cancel"}]
    events = setup_events(engine, sid)
    after_fill = [e["body"] for e in events if e["event_seq"] > first_fill_seq(engine, sid)]
    assert after_fill and after_fill[-1]["state"] == "CLOSED"
    assert all(b.get("exit_requested") != "ENTRY_EXPIRED" for b in after_fill)
    assert all(e["body"].get("reason") != "ENTRY_EXPIRED" for e in events)
    # Once requested, the exit reason is carried unchanged through the fills to the close.
    requested = [b["exit_requested"] for b in after_fill if b.get("exit_requested")]
    assert requested and set(requested) == {"STOP_LIMIT_NOT_FILLED"}
    assert [b["state"] for b in after_fill if b["state"] != "OPEN"] == ["CLOSED"]
    plans = [(e["body"]["state"], e["body"]["reason"])
             for e in setup_events(engine, sid, "PROTECTION_PLAN")]
    assert ("CANCELING", "STOP_LIMIT_NOT_FILLED") in plans
    assert ("EXIT_REQUIRED", "STOP_LIMIT_NOT_FILLED") in plans

    # Analytics: a protective stop exit with a negative test R, not an untriggered expiry.
    [item] = [i for i in paginated_managed_results(engine.repo)["items"]
              if str(i["setup_id"]) == str(sid)]
    assert item["engineering"] is True
    assert (item["state"]["state"], item["state"]["reason"]) == ("CLOSED", "STOP_LIMIT_NOT_FILLED")
    assert D(item["measurement"]["gross_realized_pnl"]) < 0
    assert D(item["measurement"]["test_r"]) < 0

    # The acceptance manifest (the tool that recorded the real defect) now agrees.
    review = re.sub(r"user=\w+", "user=catalyst_review", engine.repo.database_url)
    with psycopg.connect(review, row_factory=dict_row, options="-c timezone=UTC") as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid)
    sections = manifest["sections"]
    assert sections["exit"]["summary"]["exit_reason"] == "STOP_LIMIT_NOT_FILLED"
    assert sections["state_closed"]["summary"]["final_reason"] == "STOP_LIMIT_NOT_FILLED"
    assert verify_events(engine.repo.export_events())["valid"]


# --- The entry window still governs entries that never filled -----------------------------


def test_watching_setup_past_its_entry_window_still_expires_untriggered(mx):
    engine, venue, _ = mx
    by_tick, by_print = short_window(mx, "BTC/USD"), short_window(mx, "ETH/USD")
    venue.now += 2 * WINDOW
    engine.manage(by_tick, observation(mx))
    assert engine.observe_trigger(by_print, observation(mx)) is None
    for sid, reason in ((by_tick, "ENTRY_DEADLINE"), (by_print, "SETUP_EXPIRED")):
        state = engine._load(sid)[1]
        assert (state["state"], state["reason"]) == ("EXPIRED_UNTRIGGERED", reason)
    assert not venue.orders


@pytest.mark.parametrize(
    "symbol,unknown_submission",
    [("BTC/USD", False), ("BTC/USD", True), ("SPY", False)],
    ids=["crypto-order-submitted", "crypto-entry-pending", "stock-order-submitted"],
)
def test_unfilled_entry_past_its_window_is_cancelled_and_closes_entry_expired(
    mx, symbol, unknown_submission
):
    engine, venue, _ = mx
    sid = short_window(mx, symbol)
    # A submission whose response is lost is recovered by client ID and stays ENTRY_PENDING.
    venue.timeout_next_post = unknown_submission
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "APPROVED"
    expected = "ENTRY_PENDING" if unknown_submission else "ORDER_SUBMITTED"
    assert engine._load(sid)[1]["state"] == expected
    entry = entry_of(venue, decision)
    venue.now += 2 * WINDOW
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["exit_requested"] == "ENTRY_EXPIRED"
    assert entry["status"] == "canceled"
    assert all(leg["status"] == "canceled" for leg in entry["legs"])
    engine.manage(sid, observation(mx))
    assert closed_reason(engine, sid) == "ENTRY_EXPIRED"
    assert len(venue.orders_of("buy")) == 1
    assert not venue.orders_of("sell", "market") and not venue.orders_of("sell", "stop_limit")


def test_entry_filling_while_its_expiry_cancel_is_pending_is_flattened_as_entry_expired(mx):
    engine, venue, _ = mx
    sid = short_window(mx)
    entry = entry_of(venue, engine.observe_trigger(sid, observation(mx)))
    venue.now += 2 * WINDOW
    venue.defer_cancel = True
    engine.manage(sid, observation(mx))
    assert entry["status"] == "pending_cancel"
    assert engine._load(sid)[1]["exit_requested"] == "ENTRY_EXPIRED"
    # The broker fills the entry before the cancel lands: the pending expiry exit flattens it.
    assert engine.ingest(venue.fill(entry["id"], entry["qty"]))
    venue.defer_cancel = False
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "OPEN"
    market_close(engine, venue)
    engine.manage(sid, observation(mx))
    assert closed_reason(engine, sid) == "ENTRY_EXPIRED"


# --- A filled setup closes with the reason that emptied it --------------------------------


def test_target_exit_after_the_entry_window_closes_target_exit(mx):
    engine, venue, _ = mx
    sid = open_past_window(mx)
    [stop] = venue.orders_of("sell", "stop_limit")
    reached = observation(mx, trade_price="111", bid="111", ask="111.01")
    engine.manage(sid, reached)
    assert stop["status"] == "canceled"
    engine.manage(sid, reached)
    market_close(engine, venue, price="111")
    engine.manage(sid, reached)
    assert closed_reason(engine, sid) == "TARGET_EXIT"
    # Strategy reporting (a Jev-reviewed setup, not engineering) counts the target exit.
    [row] = managed_daily_rollups(engine.repo)["items"]
    assert dict(row["terminal_reason_counts"]) == {"TARGET_EXIT": 1}
    assert D(row["gross_pnl_usd"]) > 0


def test_time_exit_after_the_entry_window_closes_time_exit(mx):
    engine, venue, _ = mx
    engine.policy = replace(engine.policy, crypto_max_hold_seconds=300)
    sid = open_past_window(mx)
    venue.now = datetime.fromisoformat(engine._load(sid)[1]["hard_exit_at"]) + WINDOW
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["exit_requested"] == "TIME_EXIT"
    engine.manage(sid, observation(mx))
    market_close(engine, venue)
    engine.manage(sid, observation(mx))
    assert closed_reason(engine, sid) == "TIME_EXIT"


def test_protection_failure_after_the_entry_window_closes_protection_rejected(mx):
    engine, venue, _ = mx
    sid = short_window(mx)
    entry = entry_of(venue, engine.observe_trigger(sid, observation(mx)))
    assert engine.ingest(venue.fill(entry["id"], entry["qty"]))
    venue.now += 2 * WINDOW
    venue.reject_protection = True
    engine.manage(sid, observation(mx))
    state = engine._load(sid)[1]
    assert (state["state"], state["exit_requested"]) == ("OPEN", "PROTECTION_REJECTED")
    engine.manage(sid, observation(mx))
    market_close(engine, venue)
    engine.manage(sid, observation(mx))
    assert closed_reason(engine, sid) == "PROTECTION_REJECTED"


@pytest.mark.parametrize("symbol", ["BTC/USD", "SPY"])
def test_native_broker_exit_after_the_entry_window_closes_as_broker_exit(mx, symbol):
    """The broker's own protective order empties the position (no controller exit request):
    the existing close label is BROKER_EXIT, never an untriggered expiry."""
    engine, venue, _ = mx
    sid = open_past_window(mx, symbol)
    if symbol == "SPY":
        [target] = [o for o in venue.orders_of("sell", "limit") if o["status"] == "new"]
        assert engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
        for leg in venue.orders_of("sell", "stop"):
            leg["status"] = "canceled"  # The broker's OCO cancels the sibling leg.
    else:
        [stop] = venue.orders_of("sell", "stop_limit")
        assert engine.ingest(venue.fill(stop["id"], stop["qty"], price=stop["limit_price"]))
    engine.manage(sid, observation(mx))
    assert closed_reason(engine, sid) == "BROKER_EXIT"
    assert all(e["body"].get("exit_requested") != "ENTRY_EXPIRED"
               for e in setup_events(engine, sid))


# --- A pre-fill revocation is the same kind of rule ---------------------------------------


def test_revocation_after_the_fill_does_not_relabel_the_exit(mx):
    engine, venue, _ = mx
    sid, decision = admit_enter(mx)
    assert engine.ingest(venue.fill(entry_of(venue, decision)["id"], decision["payload"]["qty"]))
    engine.manage(sid, observation(mx))
    engine.revoke(sid, "SOURCE_WITHDRAWN")
    state = engine._load(sid)[1]
    assert state["state"] == "OPEN" and state["revoked"]
    reached = observation(mx, trade_price="111", bid="111", ask="111.01")
    engine.manage(sid, reached)
    engine.manage(sid, reached)
    market_close(engine, venue, price="111")
    engine.manage(sid, reached)
    assert closed_reason(engine, sid) == "TARGET_EXIT"
    state = engine._load(sid)[1]
    assert state["revoked"] and state["revocation_reason"] == "SOURCE_WITHDRAWN"


def test_revoked_unfilled_entry_still_closes_review_revoked(mx):
    engine, venue, _ = mx
    sid, decision = admit_enter(mx)
    entry = entry_of(venue, decision)
    engine.revoke(sid, "SOURCE_WITHDRAWN")
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["exit_requested"] == "REVIEW_REVOKED"
    assert entry["status"] == "canceled"
    engine.manage(sid, observation(mx))
    assert closed_reason(engine, sid) == "REVIEW_REVOKED"
