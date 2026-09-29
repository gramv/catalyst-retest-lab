"""Audit volume (plan 4.7): coalesced writers against the per-event writers they replace.

* A recorded synthetic tape of quotes and trades (tests/fixtures/managed_print_tape.json)
  replayed through the coalesced and the per-event print writers, each on its own
  disposable ledger, yields identical trigger and invalidation decisions.
* Change-only position sampling keeps observed MFE/MAE identical to per-second sampling.
* The runtime heartbeat is written on change or once a minute.
* An events-per-minute budget for 10 WATCHING + 10 OPEN setups over 10 simulated minutes.

Fake venues and disposable PostgreSQL only; no provider or broker network. Fixture
evidence only.
"""

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

from catalyst_lab import localdb
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_measurement import (
    CHANGE_SAMPLING,
    SAMPLING,
    managed_measurement,
    record_position_snapshot,
)
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.repository import Repository
from tests.test_broker_budget import noon_after
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import ManagedVenue, admit_enter, observation, packet
from tests.test_managed_execution import mx as mx
from tests.test_managed_runtime import runtime
from tests.test_setup_scan import NOW

CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
TAPE = json.loads((Path(__file__).parent / "fixtures" / "managed_print_tape.json").read_text())


# --- Disposable ledgers and the managed controller ----------------------------------------


@contextmanager
def ledger(cluster):
    """A fresh disposable database cloned from the migrated template (as ``er`` does)."""
    name = "test_" + uuid4().hex
    admin = localdb.connection_url(cluster, "lab_owner").replace(
        "dbname=catalyst_lab", "dbname=postgres"
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE catalyst_lab").format(sql.Identifier(name))
        )
    try:
        yield Repository(
            localdb.connection_url(cluster).replace("dbname=catalyst_lab", "dbname=" + name)
        )
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@contextmanager
def controller(repo, start):
    """The managed controller and runtime, the fake venue's clock at ``start``."""
    risk = RiskRepository(repo.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(repo.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = ManagedVenue()
    venue.now = start
    broker = ManagedPaperBroker(
        CREDENTIALS, ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(
        risk, broker, policy=engineering_execution_policy(), clock=lambda: venue.now,
        review_store=reviews,
    )
    run = ManagedRuntime(
        engine, SimpleNamespace(), SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        CREDENTIALS, engineering_runtime_policy(), clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True,
    )
    run._selected_packets = lambda: []
    try:
        yield SimpleNamespace(engine=engine, venue=venue, run=run, mx=(engine, venue, reviews))
    finally:
        broker.close()


def connect_markets(run, symbols):
    run.connected = run.research_healthy = True
    run.market_connected = {"US": True, "CRYPTO": True}
    run.market_subscriptions = {
        "US": {s for s in symbols if "/" not in s},
        "CRYPTO": {s for s in symbols if "/" in s},
    }
    assert run.reconcile_once() and run.ready()


def wire(row, start):
    """One Alpaca-shaped market message from a tape row."""
    stamp = (start + timedelta(seconds=row["t"])).isoformat().replace("+00:00", "Z")
    if row["T"] == "q":
        return {"T": "q", "S": row["S"], "bp": row["bp"], "ap": row["ap"], "t": stamp}
    return {"T": "t", "S": row["S"], "p": row["p"], "t": stamp, "i": row["i"]}


def events(engine, *kinds):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT kind,setup_id,body FROM lab.managed_events WHERE kind=ANY(%s)"
            " ORDER BY event_seq", (list(kinds),),
        ).fetchall()


# --- Replay: coalesced and per-event print writers decide identically ---------------------


def replay(repo, *, coalesce):
    start = noon_after(datetime.now(UTC))  # Mid-session, later than the database clock.
    with controller(repo, start) as c:
        symbols = sorted({row["S"] for row in TAPE["rows"]})
        setups = {symbol: c.engine.admit(packet(c.mx, symbol)) for symbol in symbols}
        run = c.run
        run.coalesce_quiet_prints = coalesce
        connect_markets(run, symbols)

        def tick(second):
            c.venue.now = start + timedelta(seconds=second)
            if second % 30 == 0:
                assert run.reconcile_once()
            run.execution_once()
            if second % 5 == 0:
                run.heartbeat_once()

        second = 1
        for row in TAPE["rows"]:
            while second <= row["at"]:
                tick(second)
                second += 1
            c.venue.now = start + timedelta(seconds=row["at"])
            run.market_message(row["market"], wire(row, start))
        while second <= 100:
            tick(second)
            second += 1
        run._flush_print_summaries(force=True)
        assert run.error is None
        decisions = {}
        for symbol, setup_id in setups.items():
            with c.engine.repo.connect() as conn:
                states = conn.execute(
                    "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind='STATE'"
                    " ORDER BY event_seq", (setup_id,),
                ).fetchall()
                risk = conn.execute(
                    "SELECT action,outcome,reason FROM lab.managed_risk_decisions"
                    " WHERE setup_id=%s ORDER BY event_seq", (setup_id,),
                ).fetchall()
                revokes = conn.execute(
                    "SELECT body->>'reason' AS reason FROM lab.managed_events"
                    " WHERE setup_id=%s AND kind='REVOKE' ORDER BY event_seq", (setup_id,),
                ).fetchall()
            decisions[symbol] = {
                "states": [
                    tuple(row["body"].get(k) for k in
                          ("state", "reason", "revoked", "revocation_reason"))
                    for row in states
                ],
                "risk": [tuple(row.values()) for row in risk],
                "revokes": [row["reason"] for row in revokes],
            }
        written = Counter(row["kind"] for row in events(
            c.engine, "MARKET_PRINT", "MARKET_PRINT_CONSUMED", "MARKET_PRINT_SUMMARY"
        ))
        summaries = [row["body"] for row in events(c.engine, "MARKET_PRINT_SUMMARY")]
        assert verify_events(c.engine.repo.export_events())["valid"]
        return decisions, written, summaries


def test_recorded_tape_decides_identically_through_coalesced_and_per_event_writers(
    pristine_cluster,
):
    with ledger(pristine_cluster) as coalesced_repo, ledger(pristine_cluster) as event_repo:
        with ThreadPoolExecutor(max_workers=2) as pool:  # Independent ledgers, side by side.
            first = pool.submit(replay, coalesced_repo, coalesce=True)
            second = pool.submit(replay, event_repo, coalesce=False)
            (coalesced, written, summaries), (per_event, baseline, none) = (
                first.result(), second.result()
            )
    print(f"\nTAPE REPLAY coalesced={dict(written)} per-event={dict(baseline)} "
          f"summaries={len(summaries)}")
    assert coalesced == per_event  # Identical trigger and invalidation decisions.
    # The tape's scripted outcomes, so the comparison is not vacuous.
    assert coalesced["BTC/USD"]["risk"] == [("ENTRY", "APPROVED", "RISK_APPROVED")]
    assert ("INVALIDATED", "STOP_TRADED_BEFORE_TRIGGER", None, None) in (
        coalesced["ETH/USD"]["states"]
    )
    assert coalesced["SOL/USD"]["revokes"] == ["DATA_FEED_FAILURE"]  # Stale on arrival.
    assert coalesced["DOGE/USD"]["revokes"] == ["DATA_FEED_FAILURE"]  # No quote yet.
    for symbol in ("AVAX/USD", "SPY"):
        assert [s[0] for s in coalesced[symbol]["states"]] == ["WATCHING"]
    # Per-event prints remain only for decision-relevant ones; every other print is in a
    # summary, and each summary is one per symbol and minute.
    assert none == [] and baseline["MARKET_PRINT_SUMMARY"] == 0
    assert written["MARKET_PRINT"] == written["MARKET_PRINT_CONSUMED"] == 6
    assert written["MARKET_PRINT"] + sum(s["count"] for s in summaries) == (
        baseline["MARKET_PRINT"]
    )
    assert baseline["MARKET_PRINT"] > 20 * written["MARKET_PRINT"]
    keys = Counter((s["market"], s["symbol"], s["minute"]) for s in summaries)
    assert max(keys.values()) == 1 and len(keys) <= 2 * 6
    spy = [s for s in summaries if s["symbol"] == "SPY"]
    assert spy and all(D(s["low"]) > D(TAPE["levels"]["entry_trigger"]) for s in spy)
    assert all(s["reason"] == "ABOVE_ENTRY_TRIGGER_NO_DECISION" for s in summaries)


def test_quiet_classification_is_conservative():
    run = runtime()
    run.connected = run.research_healthy = True
    run.reconcile_once()
    now = NOW
    setup = {
        "setup_id": "setup-1", "symbol": "BTC/USD", "market": "CRYPTO",
        "expires_at": now + timedelta(minutes=5),
        "state": {"state": "WATCHING", "admitted_at": (now - timedelta(minutes=1)).isoformat()},
        "record_json": {"levels": {"entry_trigger": "100", "stop": "95"}},
    }
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"BTC/USD"}
    quiet = {"trade_price": "100.01", "trade_at": now.isoformat(), "bid": "100", "ask": "100.02",
             "quote_at": now.isoformat()}
    assert run._quiet_print(setup, quiet)
    for change in (
        {"trade_price": "100"},  # At the trigger: could trigger.
        {"trade_price": "94"},  # Below the stop: invalidates.
        {"trade_at": (now - timedelta(seconds=3)).isoformat()},  # Could reach the deadline.
        {"bid": None},  # The per-print path revokes a quote-less print.
    ):
        assert not run._quiet_print(setup, {**quiet, **change})
    assert not run._quiet_print({**setup, "expires_at": now}, quiet)
    assert not run._quiet_print(
        {**setup, "state": {**setup["state"], "admitted_at": (now + timedelta(seconds=1))
                            .isoformat()}}, quiet
    )
    run.coalesce_quiet_prints = False
    assert not run._quiet_print(setup, quiet)
    run.coalesce_quiet_prints = True
    run.connected = False  # Not ready: written and evaluated as before.
    assert not run._quiet_print(setup, quiet)


# --- Change-only position sampling keeps MFE/MAE ------------------------------------------


def sampled(mx, sid, price, *, sampling, at=None, **changes):
    engine, venue, _ = mx
    setup, state = engine._load(sid)
    moment = at or venue.now
    fields = {"trade_price": price, "bid": price, "ask": str(D(price) + D("0.01")),
              "trade_at": moment.isoformat(), "quote_at": moment.isoformat(), **changes}
    return record_position_snapshot(
        engine.store, setup, state, {"qty": "2"}, observation(mx, **fields),
        received_at=moment, sampling=sampling,
    )


def test_change_only_sampling_keeps_observed_mfe_and_mae(mx):
    engine, venue, _ = mx
    legacy, change = [], []
    for symbol, target in (("BTC/USD", legacy), ("ETH/USD", change)):
        sid, _ = admit_enter(mx, symbol)
        entry = next(o for o in venue.orders_of("buy") if o["symbol"] == symbol)
        engine.ingest(venue.fill(entry["id"], entry["qty"]))  # No manage(): no other rows.
        target.append(sid)
    empty = managed_measurement(engine.repo, legacy[0], as_of=venue.now)
    assert empty["sample_count"] == 0 and empty["sampling_method"] == SAMPLING
    # Flat runs (one longer than a keyframe), steps, a same-second second observation
    # and invalid observations, identical for both samplers.
    path = [100] * 70 + [101] * 3 + [103, 102, 99, 99, 97] + [98] * 20 + [104] * 5 + [100] * 30
    start = venue.now.replace(microsecond=0) + timedelta(seconds=1)  # Whole received seconds.
    for offset, price in enumerate(path):
        venue.now = start + timedelta(seconds=offset)
        for sampling, sid in ((SAMPLING, legacy[0]), (CHANGE_SAMPLING, change[0])):
            sampled(mx, sid, str(price), sampling=sampling)
            # A later observation in the same second is never the second's sample.
            sampled(mx, sid, str(price + 5), sampling=sampling,
                    at=venue.now + timedelta(milliseconds=500))
            if offset % 17 == 0:  # Stale evidence is never sampled by either.
                sampled(mx, sid, "150", sampling=sampling, quote_at=(
                    venue.now - timedelta(seconds=30)).isoformat())
    old = managed_measurement(engine.repo, legacy[0], as_of=venue.now)
    new = managed_measurement(engine.repo, change[0], as_of=venue.now)
    for key in ("observed_mfe_pnl", "observed_mae_pnl", "observed_mfe_r", "observed_mae_r",
                "first_sample_at", "entry_fill_average"):
        assert old[key] == new[key], key
    assert D(old["observed_mfe_pnl"]) == 2 * 4 and D(old["observed_mae_pnl"]) == 2 * -3
    assert old["sample_count"] == len(path) and new["sample_count"] < len(path) // 4
    # A ledger with only per-second rows keeps its label byte-identical.
    assert old["sampling_method"] == "FIRST_VALID_OBSERVATION_PER_RECEIVED_SECOND"
    assert not any("Change-only" in text for text in old["limitations"])
    assert new["sampling_method"] == CHANGE_SAMPLING
    assert any("Change-only" in text for text in new["limitations"])
    with engine.repo.connect() as conn:
        reasons = Counter(row["reason"] for row in conn.execute(
            "SELECT body->>'sample_reason' AS reason FROM lab.managed_events"
            " WHERE setup_id=%s AND kind='POSITION_MARKET_SNAPSHOT'", (change[0],),
        ).fetchall())
    assert reasons["FIRST_SAMPLE"] == 1 and reasons["KEYFRAME"] >= 1
    assert reasons["PRICE_CHANGED"] >= 8


def test_change_only_sampling_writes_when_quantity_or_entry_fills_change(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SOL/USD")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], str(D(entry["qty"]) / 2)))
    assert sampled(mx, sid, "100", sampling=CHANGE_SAMPLING)
    venue.now += timedelta(seconds=1)
    assert not sampled(mx, sid, "100", sampling=CHANGE_SAMPLING)  # Nothing changed.
    engine.ingest(venue.fill(entry["id"], str(D(entry["qty"]) / 2), price="102"))
    venue.now += timedelta(seconds=1)
    assert sampled(mx, sid, "100", sampling=CHANGE_SAMPLING)  # A new entry fill.
    venue.now += timedelta(seconds=1)
    setup, state = engine._load(sid)
    assert record_position_snapshot(
        engine.store, setup, state, {"qty": "3"}, observation(mx), received_at=venue.now
    )  # The default sampler is change-only; the quantity changed.
    with pytest.raises(ValueError, match="UNKNOWN_SAMPLING_METHOD"):
        sampled(mx, sid, "100", sampling="EVERY_TICK")


# --- Heartbeat on change or once a minute -------------------------------------------------


def test_a_pass_timestamp_is_not_a_heartbeat_change():
    """2026-09-28: the day-review pass stamped ``last_pass_at`` on every heartbeat, so one was
    written every 5 s instead of once a minute (about 17,000 a day)."""
    from catalyst_lab.managed_runtime import ManagedRuntime

    def status(at, **day_reviews):
        return {"worker_state": "RUNNING",
                "day_reviews": {"last_pass_at": at, "open_trades": 1, "failing_reviews": [],
                                **day_reviews},
                "trade_maintenance": {"last_pass_at": at, "open_trades": 1}}

    signature = ManagedRuntime._heartbeat_signature
    first = signature(status("2026-09-28T17:00:00+00:00"))
    assert signature(status("2026-09-28T17:00:05+00:00")) == first
    assert signature(status("2026-09-28T17:00:05+00:00", open_trades=2)) != first
    assert signature(status("2026-09-28T17:00:05+00:00", failing_reviews=["x"])) != first


def test_runtime_heartbeat_is_written_on_change_or_once_a_minute():
    run = runtime()
    clock = SimpleNamespace(now=NOW)
    run.now = lambda: clock.now

    def heartbeats():
        return [body for kind, body in run.execution.events if kind == "RUNTIME_HEARTBEAT"]

    run.heartbeat_once()
    assert len(heartbeats()) == 1
    for _ in range(11):  # 55 seconds at the frozen 5-second period: nothing changed.
        clock.now += timedelta(seconds=5)
        run.last_protection_tick = clock.now.isoformat()  # Timestamps are not a change.
        run.heartbeat_once()
    assert len(heartbeats()) == 1
    clock.now += timedelta(seconds=5)
    run.heartbeat_once()
    assert len(heartbeats()) == 2  # Sixty seconds after the last one.
    run.reviewer_heartbeat = lambda: False
    clock.now += timedelta(seconds=5)
    run.heartbeat_once()
    assert len(heartbeats()) == 3 and heartbeats()[-1]["research_healthy"] is False
    assert run.research_healthy is False  # Health itself is refreshed on every call.
    clock.now += timedelta(seconds=5)
    run.heartbeat_once()
    assert len(heartbeats()) == 3


# --- Events per minute: 10 WATCHING + 10 OPEN setups for 10 simulated minutes -------------

WATCHING = ("SPY", "QQQ", "IWM", "DIA", "TLT", "DOGE/USD", "LTC/USD", "BCH/USD", "UNI/USD",
            "AAVE/USD")
OPEN = ("AAPL", "MSFT", "NVDA", "AMZN", "META", "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD",
        "LINK/USD")
# Measured per simulated minute on this harness (plan 4.7), in hash-chained audit events:
# per-event writers 1,816 (600 prints + 600 consumptions + 600 position samples, 12
# heartbeats, 4 reconciliation events); coalesced writers 196 steady, 186 in the first
# minute (161 change-only samples at this tape's 3-5 s price steps, 10 print summaries,
# 10 decision-relevant prints and their 10 consumptions, 4 reconciliation events, 1
# heartbeat). The budget leaves about 25% headroom above 196.
EVENTS_PER_MINUTE_BUDGET = 250


def open_fixture_setup(c, symbol):
    """An admitted setup moved to OPEN with one fixture entry fill (labelled LAB_FIXTURE)."""
    sid = c.engine.admit(packet(c.mx, symbol))
    with c.engine.store.transaction() as conn:
        state = c.engine.store.state(conn, sid)
        c.engine.store.transition(conn, sid, "OPEN", qty="10", fixture="LAB_FIXTURE_OPEN")
        conn.execute(
            """INSERT INTO lab.managed_fills(fill_id,setup_id,broker_order_id,side,qty,price,
            filled_at,fee_usd,source) VALUES(%s,%s,%s,'buy',10,100,%s,NULL,'LAB_FIXTURE')""",
            ("fixture-" + uuid4().hex, sid, "fixture-order-" + uuid4().hex, c.venue.now),
        )
    assert state["state"] == "WATCHING"
    return sid


def simulate_minutes(repo, *, coalesced, minutes):
    """Real writers only: market messages through ManagedRuntime.market_message and its
    queue consumer, the position sampler for every OPEN setup, the heartbeat every 5 s and
    reconciliation every 30 s. manage() itself adds nothing on unchanged ticks
    (test_managed_failure_isolation: 600 unchanged ticks, one protection plan)."""
    start = noon_after(datetime.now(UTC))
    with controller(repo, start) as c:
        for symbol in WATCHING:
            c.engine.admit(packet(c.mx, symbol))
        opened = [c.engine._load(open_fixture_setup(c, symbol)) for symbol in OPEN]
        run = c.run
        run.coalesce_quiet_prints = coalesced
        if not coalesced:
            run.HEARTBEAT_EVENT_SECONDS = 0  # Every 5-second heartbeat writes, as before.
        sampling = CHANGE_SAMPLING if coalesced else SAMPLING
        connect_markets(run, WATCHING)
        with c.engine.repo.connect() as conn:
            first = conn.execute("SELECT max(seq) AS n FROM lab.trade_events").fetchone()["n"]
        counts, trade_id = [], 0
        for minute in range(minutes):
            with c.engine.repo.connect() as conn:
                before = conn.execute(
                    "SELECT max(seq) AS n FROM lab.trade_events"
                ).fetchone()["n"]
            for second in range(60):
                offset = minute * 60 + second
                c.venue.now = start + timedelta(seconds=offset)
                if offset % 30 == 0:
                    assert run.reconcile_once()
                for index, symbol in enumerate(WATCHING):
                    market = "CRYPTO" if "/" in symbol else "US"
                    stamp = c.venue.now.isoformat().replace("+00:00", "Z")
                    wide = second == index  # Once a minute a trigger-level, wide-spread print.
                    bid, ask = ("99.50", "100.05") if wide else ("101.00", "101.02")
                    run.market_message(market, {"T": "q", "S": symbol, "bp": bid, "ap": ask,
                                                "t": stamp})
                    trade_id += 1
                    price = "100.00" if wide else str(D("101.01") + D(offset % 7) / 100)
                    run.market_message(market, {"T": "t", "S": symbol, "p": price, "t": stamp,
                                                "i": trade_id})
                run._process_queued_prints()
                for index, (setup, state) in enumerate(opened):
                    # A price that moves every few seconds and holds in between.
                    price = D(100) + D((offset // (3 + index % 3)) % 9) / 10
                    record_position_snapshot(
                        c.engine.store, setup, state, {"qty": "10"},
                        observation(c.mx, trade_price=str(price), bid=str(price),
                                    ask=str(price + D("0.01"))),
                        received_at=c.venue.now, sampling=sampling,
                    )
                if offset % 5 == 0:
                    run.heartbeat_once()
            with c.engine.repo.connect() as conn:
                after = conn.execute(
                    "SELECT max(seq) AS n FROM lab.trade_events"
                ).fetchone()["n"]
            counts.append(after - before)
        run._flush_print_summaries(force=True)
        assert run.error is None and run.ready()
        active = {s["state"]["state"] for s in c.engine.store.active()}
        assert active == {"WATCHING", "OPEN"}  # No decision was taken on the budget tape.
        with c.engine.repo.connect() as conn:
            kinds = Counter(row["kind"] for row in conn.execute(
                "SELECT e.kind FROM lab.managed_events e WHERE e.event_seq>%s", (first,)
            ).fetchall())
        assert verify_events(c.engine.repo.export_events())["valid"]
        return counts, kinds


def test_events_per_minute_budget_for_ten_watching_and_ten_open_setups(pristine_cluster):
    with ledger(pristine_cluster) as coalesced_repo, ledger(pristine_cluster) as event_repo:
        started = time.monotonic()
        # Two disposable ledgers, run side by side. The per-event baseline is measured for
        # one minute: its per-minute rate is flat and its queue query grows with each print.
        with ThreadPoolExecutor(max_workers=2) as pool:
            coalesced = pool.submit(simulate_minutes, coalesced_repo, coalesced=True, minutes=10)
            per_event = pool.submit(simulate_minutes, event_repo, coalesced=False, minutes=1)
            (after, after_kinds), (before, before_kinds) = coalesced.result(), per_event.result()
        elapsed = time.monotonic() - started
    print(f"\nEVENTS/MIN 10 WATCHING + 10 OPEN: coalesced={after} (max {max(after)}) "
          f"per-event={before} ({elapsed:.1f}s)")
    print(f"COALESCED KINDS {dict(after_kinds)}")
    print(f"PER-EVENT KINDS {dict(before_kinds)}")
    assert len(after) == 10 and max(after) <= EVENTS_PER_MINUTE_BUDGET
    assert min(before) > 5 * EVENTS_PER_MINUTE_BUDGET  # The volume this package removes.
    assert after_kinds["MARKET_PRINT_SUMMARY"] <= 10 * 11  # One per symbol and minute.
    assert after_kinds["RUNTIME_HEARTBEAT"] <= 11
    assert before_kinds["RUNTIME_HEARTBEAT"] == 12
