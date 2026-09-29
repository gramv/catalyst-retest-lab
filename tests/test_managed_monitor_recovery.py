"""Process death between persisted model response and management consumption."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_position_monitor import bars, monitor, opened


@pytest.mark.parametrize("expired", [False, True])
def test_restart_consumes_original_receipt_or_records_expiry_without_new_vote(
    mx, monkeypatch, expired
):
    engine, venue, _ = mx
    sid = opened(mx)
    original_stop = Decimal(engine._load(sid)[1]["stop"])
    watcher, calls = monitor(mx)
    quote = observation(mx, bid="108", ask="108.01")
    original = engine.accept_management

    def crash(*args):
        raise RuntimeError("FIXTURE_PROCESS_DIED_AFTER_RECEIPT")

    monkeypatch.setattr(engine, "accept_management", crash)
    with pytest.raises(RuntimeError, match="FIXTURE_PROCESS_DIED"):
        asyncio.run(watcher.review(sid, quote, bars(mx), fresh_observation=lambda: quote))
    assert len(calls) == 1
    monkeypatch.setattr(engine, "accept_management", original)
    if expired:
        venue.now += timedelta(seconds=11)
    restarted, new_calls = monitor(mx)
    restarted.recover_pending(
        sid, fresh_observation=lambda: observation(mx, bid="108", ask="108.01")
    )
    assert new_calls == []
    state = engine._load(sid)[1]
    assert Decimal(state["stop"]) == (original_stop if expired else Decimal("102"))
    with engine.repo.connect() as conn:
        kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM lab.managed_events WHERE setup_id=%s", (sid,)
            ).fetchall()
        }
        receipts = conn.execute("SELECT count(*) AS n FROM lab.jev_receipts").fetchone()["n"]
    assert ("POSITION_REVIEW_OBSOLETE" if expired else "POSITION_REVIEW_RECOVERED") in kinds
    restarted.recover_pending(sid, fresh_observation=lambda: quote)
    assert new_calls == []
    with engine.repo.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.jev_receipts").fetchone()["n"] == receipts
        )


@pytest.mark.parametrize("expired", [False, True])
def test_v3_production_window_restart_consumes_the_stored_receipt(mx, monkeypatch, expired):
    """A crash after a production-sized V3 review: recovery rebuilds nothing, asks nothing."""
    from catalyst_lab.managed_review import CONTEXT_VERSION, managed_questions
    from catalyst_lab.managed_runtime import engineering_monitor_policy
    from tests import managed_dossier_fixtures as fx
    from tests.test_position_monitor import context_from_ledger

    engine, venue, _ = mx
    sid = opened(mx)
    original_stop = Decimal(engine._load(sid)[1]["stop"])
    policy = engineering_monitor_policy()
    watcher, calls = monitor(mx, policy=policy)
    quote = observation(mx, bid="108", ask="108.01")
    window = fx.monitor_bars(venue.now)
    original = engine.accept_management

    def crash(*args):
        raise RuntimeError("FIXTURE_PROCESS_DIED_AFTER_RECEIPT")

    monkeypatch.setattr(engine, "accept_management", crash)
    with pytest.raises(RuntimeError, match="FIXTURE_PROCESS_DIED"):
        asyncio.run(watcher.review(
            sid, quote, window, fresh_observation=lambda: quote, structural_bars=window,
        ))
    monkeypatch.setattr(engine, "accept_management", original)
    stored = context_from_ledger(engine, sid)
    assert stored.data["context_version"] == CONTEXT_VERSION
    assert stored.data["manifest"]["state_bytes"] <= policy.state_byte_budget
    # The stored context reproduces the exact questions that were sent.
    assert managed_questions(stored).questions == calls[0]["questions"]
    chosen = Decimal(stored.state["eligible_options"]["stop"][0]["price"])
    if expired:
        venue.now += timedelta(seconds=11)
    restarted, new_calls = monitor(mx, policy=policy)
    restarted.recover_pending(sid, fresh_observation=lambda: observation(mx, bid="108",
                                                                         ask="108.01"))
    assert new_calls == [] and len(calls) == 1
    assert Decimal(engine._load(sid)[1]["stop"]) == (original_stop if expired else chosen)
    with engine.repo.connect() as conn:
        kinds = {r["kind"] for r in conn.execute(
            "SELECT kind FROM lab.managed_events WHERE setup_id=%s", (sid,)
        ).fetchall()}
    assert ("POSITION_REVIEW_OBSOLETE" if expired else "POSITION_REVIEW_RECOVERED") in kinds
