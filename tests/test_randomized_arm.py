"""Randomized FIXED_EXIT control arm inside the managed engine (plan 2.2). Fixtures only."""

import asyncio
import hashlib
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

import catalyst_lab.managed_execution as execution_module
from catalyst_lab.account_risk import (
    ARM_METHOD,
    FIXED_EXIT_ARM,
    JEV_MANAGED_ARM,
    MANAGED_RISK_V2_POLICY_ID,
    arm_bucket,
    assign_arm,
)
from catalyst_lab.managed_service import create_managed_app
from tests.test_capacity_policy import v2 as v2
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation, packet
from tests.test_managed_execution import mx as mx
from tests.test_managed_service import TOKEN, FixtureCycle, auth
from tests.test_position_monitor import bars, monitor


def test_assignment_is_a_deterministic_hash_of_the_setup_id():
    setup_id = UUID("6f2b8e1c-4a7d-4c3e-9b1a-2d5e8f0c7a91")
    expected = int(hashlib.sha256(str(setup_id).encode()).hexdigest(), 16) % 100
    assert arm_bucket(setup_id) == arm_bucket(str(setup_id)) == expected
    assert assign_arm(setup_id, expected + 1) == FIXED_EXIT_ARM
    assert assign_arm(setup_id, expected) == JEV_MANAGED_ARM
    assert assign_arm(setup_id, 0) == JEV_MANAGED_ARM
    assert assign_arm(setup_id, 100) == FIXED_EXIT_ARM
    for invalid in (-1, 101, 30.0, True, None):
        with pytest.raises(ValueError, match="INVALID_FIXED_EXIT_SHARE"):
            assign_arm(setup_id, invalid)


def test_about_thirty_percent_over_a_large_sample():
    sample = [uuid5(NAMESPACE_URL, f"catalyst-arm-sample-{i}") for i in range(20000)]
    share = sum(assign_arm(s, 30) == FIXED_EXIT_ARM for s in sample) / len(sample)
    assert 0.29 <= share <= 0.31  # Binomial sd is about 0.0032.
    buckets = [arm_bucket(s) for s in sample]
    assert min(buckets) == 0 and max(buckets) == 99


def forced_setup_id(monkeypatch, arm, share=30):
    """Make admit() draw a setup id that the hash places in ``arm``; later draws are real."""
    chosen = next(u for u in iter(uuid4, None) if assign_arm(u, share) == arm)
    draws = iter([chosen])
    monkeypatch.setattr(execution_module, "uuid4", lambda: next(draws, None) or uuid4())
    return chosen


def opened(mx, symbol):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, symbol)
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == symbol)
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    return sid


def states(engine, sid):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind='STATE' "
            "ORDER BY event_seq", (sid,),
        ).fetchall()
    return [row["body"] for row in rows]


@pytest.mark.parametrize("arm", [FIXED_EXIT_ARM, JEV_MANAGED_ARM])
def test_arm_is_written_at_admission_and_never_changes(v2, monkeypatch, arm):
    engine, venue, _ = v2
    chosen = forced_setup_id(monkeypatch, arm)
    sid = opened(v2, "BTC/USD")
    assert sid == chosen
    history = states(engine, sid)
    assert history[0]["state"] == "WATCHING"  # Recorded in the admitting transaction.
    assert {s["arm"] for s in history} == {arm}
    assert {s["risk_policy_id"] for s in history} == {MANAGED_RISK_V2_POLICY_ID}
    assert history[0]["arm_method"] == ARM_METHOD and history[0]["fixed_exit_arm_pct"] == 30
    assert history[-1]["state"] == "OPEN"


def test_fixed_exit_is_never_reviewed_while_mechanical_protection_runs(v2, monkeypatch):
    engine, venue, _ = v2
    fixed = forced_setup_id(monkeypatch, FIXED_EXIT_ARM)
    sid = opened(v2, "BTC/USD")
    assert sid == fixed and venue.orders_of("sell", "stop_limit")  # Protection placed.
    watcher, calls = monitor(v2)
    observed = observation(v2, bid="108", ask="108.01")
    for _ in range(2):
        result = asyncio.run(watcher.review(sid, observed, bars(v2),
                                            fresh_observation=lambda: observed))
        assert result is None
    assert calls == []  # No management Jev request was ever sent.
    with engine.repo.connect() as conn:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM lab.managed_events WHERE setup_id=%s ORDER BY event_seq", (sid,)
        ).fetchall()]
    assert "POSITION_REVIEW_REQUEST" not in kinds and "MANAGED_JEV_JUDGMENT" not in kinds
    assert kinds.count("POSITION_REVIEW_SKIPPED") == 1
    # The original levels still act mechanically: the unchanged target exits the position.
    state = engine._load(sid)[1]
    assert (state["stop"], state["target"]) == ("95", "111")
    assert datetime.fromisoformat(state["hard_exit_at"]) > venue.now
    for _ in range(3):
        engine.manage(sid, observation(v2, bid="111", ask="111.01"))
    assert engine._load(sid)[1]["exit_requested"] == "TARGET_EXIT"
    assert venue.orders_of("sell", "market")


def test_jev_managed_arm_is_still_reviewed(v2, monkeypatch):
    engine, venue, _ = v2
    forced_setup_id(monkeypatch, JEV_MANAGED_ARM)
    sid = opened(v2, "BTC/USD")
    watcher, calls = monitor(v2)
    observed = observation(v2, bid="108", ask="108.01")
    result = asyncio.run(watcher.review(sid, observed, bars(v2),
                                        fresh_observation=lambda: observed))
    assert result is not None and result.status == "RECORDED" and len(calls) == 1


def test_legacy_policy_assigns_every_setup_to_the_managed_arm(mx):
    engine, _, _ = mx
    for symbol in ("BTC/USD", "ETH/USD", "SOL/USD", "SPY", "QQQ"):
        sid = engine.admit(packet(mx, symbol))
        state = engine._load(sid)[1]
        assert state["arm"] == JEV_MANAGED_ARM and state["fixed_exit_arm_pct"] == 0


def test_read_routes_expose_the_arm_and_policy(v2, monkeypatch):
    engine, _, _ = v2
    forced_setup_id(monkeypatch, FIXED_EXIT_ARM)
    sid, _ = admit_enter(v2, "BTC/USD")
    client = TestClient(create_managed_app(FixtureCycle(engine.repo, engine.store), engine.store,
                                           api_token=TOKEN, runtime_status=lambda: {}))
    [item] = client.get("/api/v1/lab/setups", headers=auth()).json()["items"]
    assert item["setup_id"] == str(sid)
    assert item["state"]["arm"] == FIXED_EXIT_ARM
    assert item["state"]["risk_policy_id"] == MANAGED_RISK_V2_POLICY_ID
