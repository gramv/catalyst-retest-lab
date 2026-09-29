"""Independent observations and measurement: private DB, no external requests."""

from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.managed_measurement import managed_measurement, record_position_snapshot
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation
from tests.test_managed_execution import mx as mx
from tests.test_position_monitor import opened


def sample(mx, sid, **changes):
    engine, venue, _ = mx
    setup, state = engine._load(sid)
    return record_position_snapshot(
        engine.store,
        setup,
        state,
        venue._position_rows()[0],
        observation(mx, **changes),
        received_at=venue.now,
    )


def test_manage_records_independently_first_valid_sample_per_second_and_immutable(mx):
    engine, venue, _ = mx
    sid = opened(mx, "SPY")
    assert managed_measurement(engine.repo, sid, as_of=venue.now)["sample_count"] == 1
    engine.manage(sid, observation(mx, trade_price="101", bid="100.99", ask="101.01"))
    assert managed_measurement(engine.repo, sid, as_of=venue.now)["sample_count"] == 1
    venue.now += timedelta(seconds=1)
    engine.manage(sid, observation(mx, trade_price="102", bid="101.99", ask="102.01"))
    result = managed_measurement(engine.repo, sid, as_of=venue.now)
    assert result["sample_count"] == 2
    # manage() samples under the change-only version since plan 4.7.
    assert result["sampling_method"] == "FIRST_VALID_OBSERVATION_PER_RECEIVED_SECOND_ON_CHANGE_V2"
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.jev_receipts").fetchone()["n"] == 1
        row = conn.execute(
            "SELECT * FROM lab.managed_events WHERE kind='POSITION_MARKET_SNAPSHOT' "
            "ORDER BY event_seq DESC LIMIT 1"
        ).fetchone()
        assert row["body"]["price"] == "102" and D(row["body"]["spread_bps"]) > 0
    with pytest.raises(psycopg.Error), engine.repo.connect() as conn:
        conn.execute(
            "UPDATE lab.managed_events SET body='{}' WHERE event_id=%s", (row["event_id"],)
        )
    assert verify_events(engine.repo.export_events())["valid"]


@pytest.mark.parametrize(
    "change",
    [
        {"quote_at": "2000-01-01T00:00:00+00:00"},
        {"trade_at": "2000-01-01T00:00:00+00:00"},
        {"feed_healthy": False},
        {"bid": "102", "ask": "101"},
        {"trade_price": "NaN"},
        {"data_feed": ""},
        {"trade_at": "not-a-date"},
        {"bid": "0"},
    ],
)
def test_invalid_observation_never_measures_or_blocks_protection(mx, change):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    venue.now += timedelta(seconds=1)
    assert not sample(mx, sid, **change)
    assert managed_measurement(engine.repo, sid, as_of=venue.now)["sample_count"] == 1
    assert venue.orders_of("sell", "stop_limit")[0]["status"] == "new"


def test_pre_entry_and_post_exit_prints_do_not_measure_and_missing_is_null(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    assert not sample(mx, sid, trade_at=(venue.now - timedelta(seconds=1)).isoformat())
    empty = managed_measurement(engine.repo, sid, as_of=venue.now)
    assert empty["sample_count"] == 0
    assert empty["observed_mfe_r"] is empty["observed_mae_r"] is None
    assert empty["max_observation_gap_seconds"] is None
    assert empty["gross_realized_pnl"] is empty["test_r"] is None
    target = venue.orders_of("sell", "limit")[0]
    engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
    venue.orders_of("sell", "stop")[0]["status"] = "canceled"
    engine.manage(sid, observation(mx, trade_price="111"))
    setup, state = engine._load(sid)
    assert state["state"] == "CLOSED"
    assert not record_position_snapshot(
        engine.store, setup, state, {"qty": "1"}, observation(mx), received_at=venue.now
    )
    # Even appended out-of-lifecycle fixture data cannot contaminate the read.
    with engine.store.transaction() as conn:
        for delta in (-1, 1):
            engine.store.event(
                conn,
                "POSITION_MARKET_SNAPSHOT",
                {
                    "lifecycle_id": state["lifecycle_id"],
                    "market_data_timestamp": (venue.now + timedelta(seconds=delta)).isoformat(),
                    "price": "10000",
                    "position_qty": "100",
                    "data_provider": "LAB_FIXTURE",
                    "data_feed": "FIXTURE",
                },
                setup_id=sid,
            )
    assert managed_measurement(engine.repo, sid)["sample_count"] == 0


def test_gross_fill_measurement_original_risk_and_observed_gaps_provenance(mx):
    engine, venue, _ = mx
    sid = opened(mx, "SPY")
    qty = D(venue.orders_of("buy")[0]["qty"])
    venue.now += timedelta(seconds=2)
    assert sample(mx, sid, trade_price="102", bid="101.99", ask="102.01", data_feed="IEX")
    venue.now += timedelta(seconds=8)
    assert sample(mx, sid, trade_price="98", bid="97.99", ask="98.01", data_feed="IEX")
    target = venue.orders_of("sell", "limit")[0]
    engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
    venue.orders_of("sell", "stop")[0]["status"] = "canceled"
    engine.manage(sid, observation(mx))
    result = managed_measurement(engine.repo, sid, as_of=venue.now)
    assert D(result["entry_fill_average"]) == 100
    assert D(result["exit_fill_average"]) == 111
    assert D(result["gross_realized_pnl"]) == 11 * qty
    assert D(result["initial_planned_risk"]) == D("5.10") * qty
    assert D(result["test_r"]) == 11 * qty / (D("5.10") * qty)
    assert D(result["observed_mfe_pnl"]) == 2 * qty
    assert D(result["observed_mae_pnl"]) == -2 * qty
    assert result["max_observation_gap_seconds"] == 8
    assert {"data_provider": "LAB_FIXTURE", "data_feed": "IEX"} in result["provenance"]
    assert any("not a consolidated" in text for text in result["limitations"])
    assert result["net_pnl"] is None and result["fees_verified"] is False


def test_crypto_base_asset_fee_does_not_invent_net_or_remaining_position(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"], fee_qty=".01"))
    engine.manage(sid, observation(mx))
    reached = observation(mx, trade_price="111", bid="111", ask="111.01")
    engine.manage(sid, reached)
    engine.manage(sid, reached)
    close = venue.orders_of("sell", "market")[0]
    engine.ingest(venue.fill(close["id"], close["qty"], price="111"))
    engine.manage(sid, reached)
    result = managed_measurement(engine.repo, sid, as_of=venue.now)
    assert venue._position_rows() == [] and engine._load(sid)[1]["state"] == "CLOSED"
    assert D(result["bought_qty"]) - D(result["sold_qty"]) == D(".01")
    assert result["gross_realized_pnl"] is not None and result["test_r"] is not None
    assert result["net_pnl"] is None and result["fees_verified"] is False
    empty = managed_measurement(engine.repo, uuid4())
    assert empty["sample_count"] == 0 and empty["gross_realized_pnl"] is None
