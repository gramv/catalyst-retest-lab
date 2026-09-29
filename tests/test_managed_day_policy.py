"""Explicit crypto account-day policy, first-fill clock and fee provenance."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.crypto_execution import CryptoDayPolicy
from catalyst_lab.managed_execution import explicit_fill_fee_usd
from catalyst_lab.market import NY
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation
from tests.test_managed_execution import mx as mx
from tests.test_managed_operator_halts import restart


def policy(max_hold=86400):
    return CryptoDayPolicy("CRYPTO_NY_DAY_PAPER_V1", 10, 5, max_hold)


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_crypto_day_cutoffs_follow_actual_ny_midnight_and_max_elapsed_hold(day, hours):
    first = datetime.fromisoformat(day).replace(tzinfo=NY)
    p = policy()
    cutoff = p.entry_deadline(first)
    assert cutoff.astimezone(NY).strftime("%H:%M") == "23:50"
    assert p.session_flat_at(first).astimezone(NY).strftime("%H:%M") == "23:55"
    assert (p.session_flat_at(first) - first.astimezone(UTC)).total_seconds() == hours * 3600 - 300
    assert p.exit_deadline(first) == min(
        first.astimezone(UTC) + timedelta(hours=24), p.session_flat_at(first)
    )


def test_crypto_new_policy_anchors_first_partial_fill_and_never_restarts_clock(mx):
    engine, venue, _ = mx
    engine.crypto_day_policy = policy(600)
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    venue.now += timedelta(seconds=2)
    first_fill_at = venue.now
    engine.ingest(venue.fill(entry["id"], "1"))
    venue.now += timedelta(seconds=30)
    engine.manage(sid, observation(mx))
    first_state = engine._load(sid)[1]
    assert datetime.fromisoformat(first_state["opened_at"]) == first_fill_at
    expected = min(first_fill_at + timedelta(seconds=600), policy().session_flat_at(first_fill_at))
    assert datetime.fromisoformat(first_state["hard_exit_at"]) == expected
    # Simulate a late partial after the cancel acknowledgment and restart.
    venue.now += timedelta(seconds=30)
    engine.ingest(venue.fill(entry["id"], "1"))
    new = restart(engine)  # No new policy supplied; stored policy controls the open lifecycle.
    new.manage(sid, observation(mx))
    assert new._load(sid)[1]["hard_exit_at"] == first_state["hard_exit_at"]


def test_historical_crypto_record_is_not_upgraded_by_new_runtime_policy(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    engine.crypto_day_policy = policy(1)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    state = engine._load(sid)[1]
    assert state.get("crypto_day_policy") is None
    assert datetime.fromisoformat(state["hard_exit_at"]) == venue.now + timedelta(hours=24)


def test_new_crypto_waiting_entry_expires_at_saved_cutoff_across_restart(mx):
    from tests.test_managed_execution import packet

    engine, venue, _ = mx
    engine.crypto_day_policy = policy()
    expiry = policy().entry_deadline(venue.now) + timedelta(hours=1)
    sid = engine.admit(packet(mx, expires_at=expiry))
    state = engine._load(sid)[1]
    venue.now = datetime.fromisoformat(state["crypto_entry_deadline"])
    new = restart(engine)
    assert new.observe_trigger(sid, observation(mx)) is None
    assert new._load(sid)[1]["state"] == "EXPIRED_UNTRIGGERED"
    assert not venue.orders_of("buy")


def test_late_crypto_fill_cannot_move_exit_into_next_account_day(mx):
    engine, venue, _ = mx
    engine.crypto_day_policy = policy()
    sid, _ = admit_enter(mx)
    original_flat = datetime.fromisoformat(engine._load(sid)[1]["crypto_flat_deadline"])
    entry = venue.orders_of("buy")[0]
    venue.now = original_flat + timedelta(minutes=10)
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    new = restart(engine)
    new.manage(sid, observation(mx))
    assert datetime.fromisoformat(new._load(sid)[1]["hard_exit_at"]) == original_flat
    assert new._load(sid)[1]["exit_requested"]
    assert venue.orders_of("sell", "market")


def test_inventory_without_actual_first_fill_time_gets_no_new_holding_window(mx):
    engine, venue, _ = mx
    engine.crypto_day_policy = policy()
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    venue.fill(entry["id"], entry["qty"])  # Simulated missing stream fill, broker has inventory.
    engine.manage(sid, observation(mx))
    state = engine._load(sid)[1]
    assert state["exit_requested"] == "CRYPTO_FIRST_FILL_TIME_UNAVAILABLE"
    assert datetime.fromisoformat(state["hard_exit_at"]) <= venue.now
    with engine.repo.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM lab.execution_halts WHERE reason='CRYPTO_FIRST_FILL_TIME_UNAVAILABLE'"
        ).fetchone()


@pytest.mark.parametrize("cutoff,flat,hold", [(5, 5, 60), (10, 0, 60), (61, 5, 60),
                                           (10, 5, 0), (10, 5, 86401), (True, 5, 60)])
def test_crypto_session_profile_requires_explicit_sensible_bounds(cutoff, flat, hold):
    with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_DAY_POLICY_REQUIRED"):
        CryptoDayPolicy("CRYPTO_NY_DAY_PAPER_V1", cutoff, flat, hold)


@pytest.mark.parametrize(
    "payload,expected",
    [({}, None), ({"fee": "1"}, None), ({"fee": "1", "fee_currency": "BTC"}, None),
     ({"fee": "0.10", "fee_currency": "USD"}, D("0.10")),
     ({"fee_usd": "0"}, D(0)), ({"fee_usd": "0.11"}, D("0.11")),
     ({"order": {"fee_usd": "1"}}, None), ({"fee_usd": "NaN"}, None),
     ({"fee_usd": True}, None), ({"fee_usd": "-1"}, None),
     ({"fee_usd": "1", "fee": "2", "fee_currency": "USD"}, None)],
)
def test_only_explicit_incremental_usd_fees_are_measured(payload, expected):
    assert explicit_fill_fee_usd(payload) == expected


def test_actual_fill_ingestion_retains_usd_fee_and_missing_is_unknown(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    event = venue.fill(entry["id"], "1")
    event.update(fee="0.20", fee_currency="USD")
    assert engine.ingest(event)
    assert engine.ingest(venue.fill(entry["id"], "1"))
    with engine.repo.connect() as conn:
        rows = conn.execute(
            "SELECT fee_usd FROM lab.managed_fills WHERE setup_id=%s ORDER BY event_seq", (sid,)
        ).fetchall()
    assert [row["fee_usd"] for row in rows] == [D("0.20"), None]
