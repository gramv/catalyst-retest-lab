"""Race/expiry regressions: disposable DB, fake Jev and fake paper broker only."""

import asyncio
import copy
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.market import NY, Session
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation, packet
from tests.test_managed_execution import mx as mx
from tests.test_position_monitor import bars, monitor, opened


def event_bodies(engine, kind, sid):
    with engine.repo.connect() as conn:
        return [
            row["body"]
            for row in conn.execute(
                "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind=%s "
                "ORDER BY event_seq",
                (sid, kind),
            ).fetchall()
        ]


def accept_amendment(mx, sid, action="TIGHTEN_AND_EXTEND"):
    watcher, _ = monitor(mx, action)
    observed = observation(mx, bid="108", ask="108.01")
    result = asyncio.run(
        watcher.review(
            sid,
            observed,
            bars(mx),
            fresh_observation=lambda: observed,
        )
    )
    assert result.status == "RECORDED"
    assert mx[0]._load(sid)[1]["amendment_expires_at"]
    return observed


def supersede_research(engine, sid):
    setup, _ = engine._load(sid)
    revised = copy.deepcopy(setup["record_json"])
    revised["revision"] += 1
    revised["evidence_hash"] = "a" * 64
    with engine.store.transaction() as conn:
        engine.store.event(conn, "RESEARCH_PACKET", revised)


@pytest.mark.parametrize("symbol", ["SPY", "BTC/USD"])
def test_accepted_but_unstarted_management_expires_without_changing_protection(mx, symbol):
    engine, venue, _ = mx
    sid = opened(mx, symbol)
    accept_amendment(mx, sid)
    venue.now += timedelta(seconds=11)
    engine.manage(sid, observation(mx, bid="108", ask="108.01"))
    state = engine._load(sid)[1]
    assert D(state["stop"]) == D(95) and D(state["target"]) == D(111)
    assert state["amendment_expires_at"] is None
    assert event_bodies(engine, "MANAGEMENT_EXPIRED", sid)[-1]["reason"] == "REVIEW_EXPIRED"
    assert not event_bodies(engine, "MANAGEMENT_STARTED", sid)
    assert not any(method == "PATCH" for method, _, _ in venue.calls)
    stops = venue.orders_of("sell", "stop" if symbol == "SPY" else "stop_limit")
    assert len(stops) == 1 and D(stops[0]["stop_price"]) == D(95)
    assert verify_events(engine.repo.export_events())["valid"]


@pytest.mark.parametrize("symbol", ["SPY", "BTC/USD"])
def test_a_print_with_no_quote_defers_an_accepted_amendment(mx, symbol):
    """After a market gap the runtime's observation can hold a print and no quote. An accepted
    amendment then waits, as for a stale quote, with the earlier levels in force."""
    engine, venue, _ = mx
    sid = opened(mx, symbol)
    accept_amendment(mx, sid)
    now = venue.now.isoformat()
    engine.manage(sid, {"trade_price": "108", "trade_at": now, "trade_id": "7",
                        "feed_healthy": True, "data_provider": "ALPACA",
                        "data_feed": "CRYPTO_US", "retrieved_at": now})
    assert not event_bodies(engine, "MANAGEMENT_STARTED", sid)
    assert not event_bodies(engine, "MANAGEMENT_EXPIRED", sid)
    assert engine._load(sid)[1]["amendment_expires_at"]
    assert not any(method == "PATCH" for method, _, _ in venue.calls)
    engine.manage(sid, observation(mx, bid="108", ask="108.01"))
    [started] = event_bodies(engine, "MANAGEMENT_STARTED", sid)
    assert started["reason"] == "FRESH_STATE_REVALIDATED"


def test_fresh_premarket_print_cannot_trigger_after_regular_session_opens(mx, monkeypatch):
    engine, venue, _ = mx
    p = packet(mx, "SPY")
    sid = engine.admit(p)
    venue.now += timedelta(seconds=1)
    session = Session(venue.now.astimezone(NY).date(), venue.now, venue.now + timedelta(hours=1))
    monkeypatch.setattr(engine.broker, "calendar", lambda *_: [session])
    result = engine.observe_trigger(
        sid,
        observation(
            mx,
            trade_at=(venue.now - timedelta(seconds=1)).isoformat(),
        ),
    )
    assert result is None
    assert engine._load(sid)[1]["state"] == "WATCHING"
    assert not venue.orders


def test_durable_market_queue_preserves_stop_touch_and_has_immutable_consumption(mx):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx))
    run = ManagedRuntime(
        engine,
        SimpleNamespace(),
        SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        engine.broker.credentials,
        engineering_runtime_policy(),
        clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True,
    )
    run._selected_packets = lambda: []
    run.connected = run.research_healthy = True
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"BTC/USD"}
    run.reconcile_once()
    quote = {"T": "q", "S": "BTC/USD", "bp": "99.99", "ap": "100.01", "t": venue.now.isoformat()}
    run.market_message("CRYPTO", quote)
    for trade_id, price in ((1, "94"), (2, "100")):
        run.market_message(
            "CRYPTO",
            {"T": "t", "S": "BTC/USD", "p": price, "i": trade_id, "t": venue.now.isoformat()},
        )
    assert len(run._pending_trades()) == 2
    run.execution_once()
    assert engine._load(sid)[1]["state"] == "INVALIDATED"
    assert engine._load(sid)[1]["reason"] == "STOP_TRADED_BEFORE_TRIGGER"
    assert not venue.orders and run._pending_trades() == []
    assert len(event_bodies(engine, "MARKET_PRINT", sid)) == 2
    assert len(event_bodies(engine, "MARKET_PRINT_CONSUMED", sid)) == 2
    assert verify_events(engine.repo.export_events())["valid"]


def test_crossing_original_target_before_amendment_keeps_original_exit(mx):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    accept_amendment(mx, sid)
    engine.manage(sid, observation(mx, bid="111", ask="111.01"))
    assert event_bodies(engine, "MANAGEMENT_EXPIRED", sid)[-1]["reason"] == (
        "PRICE_CHANGED_BEFORE_AMENDMENT"
    )
    assert D(engine._load(sid)[1]["target"]) == D(111)
    engine.manage(sid, observation(mx, bid="111", ask="111.01"))
    assert len(venue.orders_of("sell", "market")) == 1
    assert not event_bodies(engine, "MANAGEMENT_STARTED", sid)


def test_freshly_started_crypto_replacement_completes_after_model_deadline(mx):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    observed = accept_amendment(mx, sid)
    original = venue.orders_of("sell", "stop_limit")[0]
    venue.defer_cancel = True
    # The market prints at 108, as it quotes: under CRYPTO_STOP_BREACH_V2 the fixture's default
    # print (100, below the raised stop of 102) would be a breach.
    engine.manage(sid, {**observed, "trade_price": "108"})
    assert original["status"] == "pending_cancel"
    assert len(event_bodies(engine, "MANAGEMENT_STARTED", sid)) == 1
    venue.now += timedelta(seconds=11)
    original["status"] = "canceled"
    venue.defer_cancel = False
    engine.manage(sid, observation(mx, trade_price="108", bid="108", ask="108.01"))
    replacements = venue.orders_of("sell", "stop_limit")
    assert len(replacements) == 2 and D(replacements[-1]["stop_price"]) == D(102)
    assert replacements[-1]["status"] == "new"
    assert not event_bodies(engine, "MANAGEMENT_EXPIRED", sid)
    assert not venue.orders_of("sell", "market")


def test_material_revision_cancels_working_entry_and_releases_only_when_flat(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    supersede_research(engine, sid)
    venue.defer_cancel = True
    engine.manage(sid, observation(mx))
    assert entry["status"] == "pending_cancel"
    state = engine._load(sid)[1]
    assert state["revoked"] and state["exit_requested"] == "REVIEW_REVOKED"
    with engine.repo.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.managed_active_reservations WHERE setup_id=%s",
                (sid,),
            ).fetchone()["n"]
            == 1
        )
    entry["status"] = "canceled"
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "CLOSED"
    with engine.repo.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.managed_active_reservations WHERE setup_id=%s",
                (sid,),
            ).fetchone()["n"]
            == 0
        )
    assert len(venue.orders_of("buy")) == 1


def test_material_revision_cancel_race_never_sells_more_than_late_fill(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    supersede_research(engine, sid)
    venue.defer_cancel = True
    engine.manage(sid, observation(mx))
    assert entry["status"] == "pending_cancel"
    engine.ingest(venue.fill(entry["id"], "1"))
    entry["status"] = "canceled"
    venue.defer_cancel = False
    engine.manage(sid, observation(mx))
    sells = venue.orders_of("sell")
    assert sells and all(D(order["qty"]) <= D(1) for order in sells)
    assert len(venue.orders_of("buy")) == 1
    assert engine._load(sid)[1]["revoked"]
    assert verify_events(engine.repo.export_events())["valid"]


def test_unknown_child_fill_replayed_after_timeout_parent_ownership_recovery(mx, monkeypatch):
    engine, venue, _ = mx
    lookup = engine.broker.order_by_client_id
    monkeypatch.setattr(engine.broker, "order_by_client_id", lambda client_id: None)
    venue.timeout_next_post = True
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    parent_fill = venue.fill(entry["id"], entry["qty"])
    venue.now += timedelta(seconds=1)
    target = venue.orders_of("sell", "limit")[0]
    child_fill = venue.fill(target["id"], target["qty"], price="111")
    venue.orders_of("sell", "stop")[0]["status"] = "canceled"
    assert engine.ingest(child_fill) is False  # Child arrived before the unknown POST was resolved.
    with engine.repo.connect() as conn:
        pending = conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind='UNMATCHED_BROKER_EVENT'",
        ).fetchone()["body"]
    assert pending["payload"]["execution_id"] == child_fill["execution_id"]
    monkeypatch.setattr(engine.broker, "order_by_client_id", lookup)
    engine._broker_view(sid)  # GET by deterministic client ID, persist ownership, replay child.
    assert engine.ingest(parent_fill)  # Older provider event is delivered after the exit.
    engine._broker_view(sid)
    assert engine.replay_unmatched() == 0
    with engine.repo.connect() as conn:
        fills = conn.execute(
            "SELECT side,qty,price FROM lab.managed_fills WHERE setup_id=%s ORDER BY filled_at",
            (sid,),
        ).fetchall()
    assert [(r["side"], r["price"]) for r in fills] == [("buy", D(100)), ("sell", D(111))]
    assert engine.reconcile()["clean"]  # Inventory uses provider time, not last-arriving event.
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "CLOSED"
    assert len(venue.orders_of("buy")) == 1
    assert not venue.orders_of("sell", "market")
    assert verify_events(engine.repo.export_events())["valid"]
