from datetime import timedelta
from decimal import Decimal as D

import psycopg
import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.market import Observation, Session
from catalyst_lab.trigger import TriggerPolicy
from catalyst_lab.watcher import Watcher
from tests.conftest import NOW


@pytest.fixture
def watching(repo, raw, evidence, policy):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    watcher = Watcher(
        repo, feed="IEX_FIXTURE", provider="LAB_FIXTURE", reconciliation_gate=lambda: True
    )
    session = Session(NOW.date(), evidence.official_open, evidence.official_close)
    sessions = {NOW.date(): session}
    watcher.tick(sessions, NOW, {raw["ticker"]})
    return watcher, record["candidate_id"], sessions


def quote(raw, seconds=0, bid="99.99", ask="100.01"):
    return Observation(
        "quote",
        raw["ticker"],
        (NOW + timedelta(seconds=seconds)).isoformat(),
        "IEX_FIXTURE",
        bid=D(bid),
        ask=D(ask),
        data_provider="LAB_FIXTURE",
    )


def trade(raw, seconds=1, price="100", trade_id="1"):
    return Observation(
        "trade",
        raw["ticker"],
        (NOW + timedelta(seconds=seconds)).isoformat(),
        "IEX_FIXTURE",
        price=D(price),
        trade_id=trade_id,
        data_provider="LAB_FIXTURE",
    )


def test_watching_and_trigger_are_audited_without_orders(repo, raw, watching):
    watcher, cid, sessions = watching
    assert repo.get_candidate(cid)["state"] == "WATCHING"
    watcher.tick(sessions, NOW, {raw["ticker"]}, quote(raw))
    watcher.tick(sessions, NOW + timedelta(seconds=1), {raw["ticker"]}, trade(raw))
    assert repo.get_candidate(cid)["state"] == "TRIGGER_CONFIRMED"
    with repo.connect() as conn:
        snapshots = conn.execute(
            "SELECT * FROM lab.market_snapshots WHERE candidate_id=%s", (cid,)
        ).fetchall()
        assert len(snapshots) == 2
        assert {s["data_provider"] for s in snapshots} == {"LAB_FIXTURE"}
        assert {s["data_feed"] for s in snapshots} == {"IEX_FIXTURE"}
        assert all(s["event_id"] and s["provider_timestamp"] for s in snapshots)
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.orders WHERE candidate_id=%s", (cid,)
            ).fetchone()["n"]
            == 0
        )
    states = [
        e["payload_json"]["to_state"]
        for e in repo.candidate_events(cid)
        if e["event_type"] == "STATE_TRANSITION"
    ]
    assert states == ["RECEIVED", "VALIDATING", "VALIDATED", "WATCHING", "TRIGGER_CONFIRMED"]
    assert verify_events(repo.export_events())["event_count"] > 0


def test_duplicate_prints_do_not_duplicate_observations(repo, raw, watching):
    watcher, cid, sessions = watching
    obs = trade(raw, price="100.5")
    for _ in range(2):
        watcher.tick(sessions, NOW + timedelta(seconds=1), {raw["ticker"]}, obs)
    with repo.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.market_snapshots WHERE candidate_id=%s", (cid,)
            ).fetchone()["n"]
            == 1
        )


def test_restart_keeps_touch_and_applies_short_gap(repo, raw, watching):
    watcher, cid, sessions = watching
    watcher.tick(sessions, NOW, {raw["ticker"]}, quote(raw, bid="99", ask="101"))
    watcher.tick(sessions, NOW + timedelta(seconds=1), {raw["ticker"]}, trade(raw))
    restarted = Watcher(
        repo, feed="IEX_FIXTURE", provider="LAB_FIXTURE", reconciliation_gate=lambda: True
    )
    restarted.tick({}, NOW + timedelta(seconds=2), set(), recovering=True)
    assert repo.get_candidate(cid)["state"] == "WATCHING"
    restarted.tick({}, NOW + timedelta(seconds=3), {raw["ticker"]}, quote(raw, seconds=3))
    assert repo.get_candidate(cid)["state"] == "TRIGGER_CONFIRMED"


def test_restart_with_gap_invalidates_even_without_calendar_network(repo, raw, watching):
    watcher, cid, sessions = watching
    watcher.tick(sessions, NOW, {raw["ticker"]}, quote(raw))
    restarted = Watcher(
        repo, feed="IEX_FIXTURE", provider="LAB_FIXTURE", reconciliation_gate=lambda: True
    )
    restarted.tick({}, NOW + timedelta(seconds=10), set(), recovering=True)
    result = repo.get_candidate(cid)
    assert result["state"] == "INVALIDATED" and result["state_reason"] == "DATA_FEED_FAILURE"


def test_expiry_uses_frozen_calendar_after_restart(repo, raw, watching):
    watcher, cid, sessions = watching
    watcher.tick({}, NOW.replace(hour=16), set(), recovering=True)
    result = repo.get_candidate(cid)
    assert result["state"] == "EXPIRED_UNTRIGGERED" and result["status"] == "expired"


def test_feed_mismatch_cannot_trigger(repo, raw, watching):
    watcher, cid, sessions = watching
    with pytest.raises(ValueError, match="provenance"):
        watcher.tick(
            sessions,
            NOW,
            {raw["ticker"]},
            Observation("trade", raw["ticker"], NOW.isoformat(), "sip", price=D("100")),
        )
    assert repo.get_candidate(cid)["state"] == "WATCHING"


def test_observation_and_state_roll_back_atomically(repo, raw, watching, monkeypatch):
    watcher, cid, sessions = watching
    watcher.tick(sessions, NOW, {raw["ticker"]}, quote(raw))
    before = len(repo.candidate_events(cid))
    original = repo.transition

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected process failure before commit")

    monkeypatch.setattr(repo, "transition", broken)
    with pytest.raises(RuntimeError):
        watcher.tick(sessions, NOW + timedelta(seconds=1), {raw["ticker"]}, trade(raw))
    assert repo.get_candidate(cid)["state"] == "WATCHING"
    assert len(repo.candidate_events(cid)) == before
    with repo.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.market_snapshots WHERE candidate_id=%s", (cid,)
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("table", ["watch_contexts", "market_snapshots"])
def test_new_observer_records_are_database_immutable(repo, table):
    with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"DELETE FROM lab.{table}")


def test_policy_is_frozen_per_watch(repo, raw, evidence, policy):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    cid = record["candidate_id"]
    sessions = {NOW.date(): Session(NOW.date(), evidence.official_open, evidence.official_close)}
    watcher = Watcher(
        repo,
        TriggerPolicy(D("5")),
        feed="IEX_FIXTURE",
        provider="LAB_FIXTURE",
        reconciliation_gate=lambda: True,
    )
    watcher.tick(sessions, NOW, {raw["ticker"]}, quote(raw, bid="99.96", ask="100.04"))
    watcher.tick(sessions, NOW + timedelta(seconds=1), {raw["ticker"]}, trade(raw))
    looser = Watcher(
        repo,
        TriggerPolicy(D("10")),
        feed="IEX_FIXTURE",
        provider="LAB_FIXTURE",
        reconciliation_gate=lambda: True,
    )
    looser.tick(
        sessions,
        NOW + timedelta(seconds=2),
        {raw["ticker"]},
        quote(raw, seconds=2, bid="99.96", ask="100.04"),
    )
    assert repo.get_candidate(cid)["state"] == "WATCHING"


def test_unwatched_candidate_expires_from_validated_calendar_without_network(
    repo, raw, evidence, policy
):
    record = repo.submit(raw, NOW, lambda c, now: evidence, policy)
    watcher = Watcher(
        repo, feed="IEX_FIXTURE", provider="LAB_FIXTURE", reconciliation_gate=lambda: True
    )
    watcher.tick({}, evidence.official_close, set())
    assert repo.get_candidate(record["candidate_id"])["state"] == "EXPIRED_UNTRIGGERED"
    states = [
        event["payload_json"]["to_state"]
        for event in repo.candidate_events(record["candidate_id"])
        if event["event_type"] == "STATE_TRANSITION"
    ]
    assert "WATCHING" not in states
