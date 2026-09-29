"""Ticker-attempt ownership across engines, isolated DB and synthetic providers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.market import NY, Session
from catalyst_lab.risk import RiskEngine, RiskPolicy
from tests.clock import session_bounds
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_risk import EvidenceClient


def legacy_candidate(mx, er, raw, evidence, policy, symbol="OWNED"):
    engine, venue, _ = mx
    now = venue.now  # ManagedVenue starts on fixture_now(), clear of New York midnight.
    day = now.astimezone(NY).date()
    opens, closes = session_bounds(now, hours=2)
    body = raw | {
        "ticker": symbol,
        "signal_id": "fixture-" + uuid4().hex,
        "entry_trigger": "100",
        "max_entry_price": "100.10",
        "stop": "95",
        "target": "111",
    }
    ev = replace(
        evidence,
        ticker=symbol,
        observed_at=now,
        session_date=day,
        reconciled_session=day,
        official_open=opens,
        official_close=closes,
        quote_timestamp=now,
    )
    row = er.submit(body, now, lambda c, n: ev, policy)
    assert row["state"] == "VALIDATED", row
    cid = row["candidate_id"]
    with er.connect() as conn:
        er.transition(conn, cid, "VALIDATED", "WATCHING", {"reason": "LAB_FIXTURE"})
        er.transition(conn, cid, "WATCHING", "TRIGGER_CONFIRMED", {"reason": "LAB_FIXTURE"})
    client = EvidenceClient()
    client.at = now
    client.calendar = lambda start, end: [Session(day, opens, closes)]
    legacy = RiskEngine(
        engine.repo,
        client,
        RiskPolicy(),
        ready=lambda: True,
        clock=lambda: venue.now,
    )
    legacy.classify(symbol, symbol, symbol, "LAB_FIXTURE")
    return cid, legacy


@pytest.mark.parametrize("terminal", [None, "INVALIDATED", "EXPIRED_UNTRIGGERED", "CLOSED"])
def test_us_new_receipt_cannot_reset_daily_attempt_even_after_terminal_state(mx, terminal):
    engine, venue, _ = mx
    first = packet(mx, "OWNED")
    sid = engine.admit(first)
    assert engine.admit(first) == sid  # Idempotent replay is not another attempt.
    if terminal == "CLOSED":
        assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
        entry = venue.orders_of("buy")[0]
        engine.ingest(venue.fill(entry["id"], entry["qty"]))
        engine.manage(sid, observation(mx))
        target = venue.orders_of("sell", "limit")[0]
        engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
        venue.orders_of("sell", "stop")[0]["status"] = "canceled"
        engine.manage(sid, observation(mx))
        assert engine._load(sid)[1]["state"] == "CLOSED"
    elif terminal:
        with engine.store.transaction() as conn:
            engine.store.transition(conn, sid, terminal, reason="ENGINEERING_FIXTURE")
    retry = packet(mx, "OWNED")
    assert retry["cycle_id"] != first["cycle_id"] and retry["receipt_id"] != first["receipt_id"]
    with pytest.raises(ValueError, match="^TICKER_ALREADY_ATTEMPTED$"):
        engine.admit(retry)
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_setups").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "symbol,reason",
    [("OWNED", "TICKER_ALREADY_ATTEMPTED"), ("BTC/USD", "ACTIVE_SYMBOL_ALREADY_MANAGED")],
)
def test_competing_receipts_have_one_atomic_symbol_owner(mx, symbol, reason):
    engine, _, _ = mx
    packets = [packet(mx, symbol) for _ in range(4)]
    barrier = Barrier(len(packets))

    def attempt(p):
        barrier.wait()
        try:
            return "ADMITTED", engine.admit(p)
        except ValueError as exc:
            return "REJECTED", str(exc)

    with ThreadPoolExecutor(max_workers=len(packets)) as pool:
        results = list(pool.map(attempt, packets))
    assert sum(r[0] == "ADMITTED" for r in results) == 1
    assert [r[1] for r in results if r[0] == "REJECTED"] == [reason] * 3
    assert verify_events(engine.repo.export_events())["valid"]


def test_crypto_may_research_new_attempt_only_after_old_lifecycle_is_terminal(mx):
    engine, _, _ = mx
    sid = engine.admit(packet(mx))
    engine.revoke(sid, "FIXTURE_RESEARCH_WITHDRAWAL")
    second = engine.admit(packet(mx))
    assert second != sid
    assert engine._load(second)[1]["state"] == "WATCHING"


def test_us_new_session_allows_new_attempt_without_rewriting_previous_claim(mx):
    engine, venue, _ = mx
    first_day = venue.now.astimezone(NY).date()
    sid = engine.admit(packet(mx, "OWNED"))
    engine.revoke(sid, "FIXTURE_EXPIRED")
    venue.now += timedelta(days=1)
    second = engine.admit(packet(mx, "OWNED"))
    assert second != sid and venue.now.astimezone(NY).date() != first_day
    assert engine._load(sid)[1]["state"] == "INVALIDATED"


def test_legacy_daily_claim_prevents_managed_admission(mx, er, raw, evidence, policy):
    cid, _ = legacy_candidate(mx, er, raw, evidence, policy)
    with pytest.raises(ValueError, match="^TICKER_ALREADY_ATTEMPTED$"):
        mx[0].admit(packet(mx, "OWNED"))
    assert er.get_candidate(cid)["state"] == "TRIGGER_CONFIRMED"
    assert mx[1].orders == {}


@pytest.mark.parametrize("managed_closed", [False, True])
def test_managed_daily_owner_prevents_legacy_risk_entry(
    mx, er, raw, evidence, policy, managed_closed
):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "OWNED"))
    if managed_closed:
        engine.revoke(sid, "FIXTURE_RESEARCH_WITHDRAWAL")
    cid, legacy = legacy_candidate(mx, er, raw, evidence, policy)
    result = legacy.authorize_entry(cid)
    assert result["decision"] == "REJECTED" and result["reason"] == "TICKER_ALREADY_ATTEMPTED"
    assert er.get_candidate(cid)["state"] == "RISK_REJECTED"
    assert venue.orders == {}
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()
    assert verify_events(engine.repo.export_events())["valid"]


def test_cross_engine_competing_entry_and_managed_admission_respect_legacy_claim(
    mx, er, raw, evidence, policy
):
    cid, legacy = legacy_candidate(mx, er, raw, evidence, policy)
    reviewed = packet(mx, "OWNED")
    barrier = Barrier(2)

    def legacy_attempt():
        barrier.wait()
        return legacy.authorize_entry(cid)

    def managed_attempt():
        barrier.wait()
        with pytest.raises(ValueError, match="^TICKER_ALREADY_ATTEMPTED$"):
            mx[0].admit(reviewed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        frozen = pool.submit(legacy_attempt)
        managed = pool.submit(managed_attempt)
        result = frozen.result()
        managed.result()
    assert result["decision"] == "APPROVED"
    with mx[0].repo.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.account_risk_reservations").fetchone()["n"]
            == 1
        )
        assert not conn.execute("SELECT 1 FROM lab.managed_setups").fetchone()
