"""Cost evidence and complete-history views against disposable PostgreSQL only."""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from catalyst_lab.audit import verify_events
from catalyst_lab.managed_analytics import (
    import_fill_cost_correction,
    managed_daily_rollups,
    paginated_managed_results,
)
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.managed_service import create_managed_app
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation, packet
from tests.test_managed_execution import mx as mx


def close(mx, symbol="SPY", *, base_fee="0"):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, symbol)
    entry = venue.orders_of("buy")[-1]
    engine.ingest(venue.fill(entry["id"], entry["qty"], fee_qty=base_fee))
    engine.manage(sid, observation(mx))
    reached = observation(mx, trade_price="111", bid="111", ask="111.01")
    if "/" in symbol:
        engine.manage(sid, reached)
        engine.manage(sid, reached)
        target = venue.orders_of("sell", "market")[-1]
    else:
        target = venue.orders_of("sell", "limit")[-1]
        venue.orders_of("sell", "stop")[-1]["status"] = "canceled"
    engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
    engine.manage(sid, reached)
    assert engine._load(sid)[1]["state"] == "CLOSED"
    return sid


def fills(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT * FROM lab.managed_fills WHERE setup_id=%s ORDER BY event_seq", (sid,),
        ).fetchall()


def correction(fill, now, **changes):
    return {
        "correction_id": str(uuid4()), "setup_id": str(fill["setup_id"]),
        "fill_id": fill["fill_id"], "fill_event_seq": fill["event_seq"], "fee_usd": "1",
        "source": "LAB_FIXTURE", "broker_source_reference": "fixture-statement-row-1",
        "observed_at": now.isoformat(), **changes,
    }


def test_append_only_cost_import_exact_replay_and_explicit_latest_correction(mx):
    engine, venue, _ = mx
    sid = close(mx)
    original = fills(engine, sid)
    first = correction(original[0], venue.now)
    ack = import_fill_cost_correction(engine.store, first, recorded_at=venue.now)
    retry = import_fill_cost_correction(
        engine.store, first, recorded_at=venue.now + timedelta(seconds=1)
    )
    assert retry["idempotent_replay"] and retry["event_seq"] == ack["event_seq"]
    assert managed_measurement(engine.repo, sid)["net_pnl"] is None
    with pytest.raises(ValueError, match="IDEMPOTENCY_MISMATCH"):
        import_fill_cost_correction(engine.store, {**first, "fee_usd": "2"}, recorded_at=venue.now)
    with pytest.raises(ValueError, match="LATEST_COST_CORRECTION_BINDING"):
        import_fill_cost_correction(
            engine.store, correction(original[0], venue.now, fee_usd="2"), recorded_at=venue.now
        )
    updated = correction(original[0], venue.now, fee_usd="2",
                         supersedes_correction_id=first["correction_id"])
    import_fill_cost_correction(engine.store, updated, recorded_at=venue.now)
    import_fill_cost_correction(
        engine.store, correction(original[1], venue.now, fee_usd=".50"), recorded_at=venue.now
    )
    measured = managed_measurement(engine.repo, sid)
    assert measured["fees_verified"]
    assert D(measured["verified_cash_fee_usd"]) == D("2.50")
    assert D(measured["net_pnl"]) == D(measured["gross_realized_pnl"]) - D("2.50")
    assert updated["correction_id"] in measured["cost_correction_ids"]
    assert first["correction_id"] not in measured["cost_correction_ids"]
    assert fills(engine, sid) == original
    with pytest.raises(psycopg.Error), engine.repo.connect() as conn:
        conn.execute("UPDATE lab.managed_events SET body='{}' WHERE event_seq=%s",
                     (ack["event_seq"],))
    assert verify_events(engine.repo.export_events())["valid"]


def test_base_asset_fee_reconciles_inventory_without_double_subtracting_economic_fee(mx):
    engine, venue, _ = mx
    sid = close(mx, "BTC/USD", base_fee=".01")
    rows = fills(engine, sid)
    before = managed_measurement(engine.repo, sid)
    assert before["net_pnl"] is None
    import_fill_cost_correction(engine.store, correction(
        rows[0], venue.now, fee_usd="1.25", base_asset_fee_qty=".01", base_asset_fee_usd="1.00"
    ), recorded_at=venue.now)
    import_fill_cost_correction(engine.store, correction(
        rows[1], venue.now, fee_usd=".50"
    ), recorded_at=venue.now)
    measured = managed_measurement(engine.repo, sid)
    assert measured["inventory_reconciled_with_costs"] and measured["fees_verified"]
    assert D(measured["verified_total_economic_fee_usd"]) == D("1.75")
    assert D(measured["verified_cash_fee_usd"]) == D(".75")
    assert D(measured["verified_base_asset_fee_qty"]) == D(".01")
    assert D(measured["net_pnl"]) == D(measured["gross_realized_pnl"]) - D(".75")
    assert fills(engine, sid) == rows
    assert engine._load(sid)[1]["qty"] == "0"


@pytest.mark.parametrize("symbol,base_fee", [("SPY", "0"), ("BTC/USD", ".01")])
def test_results_fee_completeness_uses_audited_corrections_across_views(mx, symbol, base_fee):
    engine, venue, _ = mx
    sid = close(mx, symbol, base_fee=base_fee)
    original = fills(engine, sid)
    token = "fixture-cost-results-token-for-disposable-tests"
    app = create_managed_app(
        SimpleNamespace(repo=engine.repo), engine.store,
        api_token=token, runtime_status=lambda: {},
    )
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        for imported in range(3):
            result = client.get("/api/v1/lab/results").json()["items"][0]
            history = client.get("/api/v1/lab/history/results").json()["items"][0]
            measurement = client.get(f"/api/v1/lab/positions/{sid}/measurement").json()[
                "measurement"
            ]
            daily = client.get("/api/v1/lab/analytics/daily").json()["items"][0]
            complete = imported == 2
            assert result["fees_complete"] is measurement["fees_verified"] is complete
            assert history["measurement"]["fees_verified"] is complete
            assert result["net_pnl_usd"] == history["net_pnl_usd"] == measurement["net_pnl"]
            assert (result["net_pnl_usd"] is not None) is complete
            assert daily["net_known_count"] == int(complete)
            assert daily["net_pnl_usd"] == result["net_pnl_usd"]
            if imported < 2:
                extra = ({"base_asset_fee_qty": base_fee, "base_asset_fee_usd": "1"}
                         if imported == 0 and D(base_fee) else {})
                import_fill_cost_correction(
                    engine.store, correction(original[imported], venue.now, **extra),
                    recorded_at=venue.now,
                )
    assert fills(engine, sid) == original
    assert all(fill["fee_usd"] is None for fill in original)


@pytest.mark.parametrize("change", [
    {"setup_id": str(uuid4())}, {"fill_event_seq": 1}, {"source": "MUSE_ESTIMATE"},
    {"fee_usd": "NaN"}, {"fee_usd": "-1"}, {"fee_usd": 1.5},
    {"base_asset_fee_qty": ".01"},
    {"base_asset_fee_qty": ".01", "base_asset_fee_usd": "2"},
    {"quantity": "100"}, {"observed_at": "2000-01-01T00:00:00+00:00"},
])
def test_bad_binding_unknown_conversion_or_execution_override_cannot_be_imported(mx, change):
    engine, venue, _ = mx
    sid = close(mx)
    raw = correction(fills(engine, sid)[0], venue.now, **change)
    with pytest.raises(ValueError):
        import_fill_cost_correction(engine.store, raw, recorded_at=venue.now)
    assert managed_measurement(engine.repo, sid)["net_pnl"] is None
    with engine.repo.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE kind='FILL_COST_CORRECTION'"
        ).fetchone()


def test_unreconciled_inventory_and_malformed_correction_keep_net_unknown(mx):
    engine, venue, _ = mx
    sid = close(mx, "BTC/USD", base_fee=".01")
    rows = fills(engine, sid)
    for fill in rows:
        import_fill_cost_correction(
            engine.store, correction(fill, venue.now, fee_usd="0"), recorded_at=venue.now
        )
    measured = managed_measurement(engine.repo, sid)
    assert measured["net_pnl"] is None and not measured["inventory_reconciled_with_costs"]
    # Raw event input cannot bypass the projection's binding checks.
    with engine.store.transaction() as conn:
        engine.store.event(conn, "FILL_COST_CORRECTION", {
            "fill_id": rows[0]["fill_id"], "fee_usd": "0", "base_asset_fee_qty": ".01",
        }, setup_id=sid)
    measured = managed_measurement(engine.repo, sid)
    assert measured["net_pnl"] is None
    assert measured["cost_evidence"][0]["invalid_correction"]


def test_results_keyset_pages_and_daily_rollups_keep_unknown_net_visible(mx):
    engine, venue, _ = mx
    ids = [close(mx, symbol) for symbol in ("SPY", "AAPL", "QQQ")]
    for fill in fills(engine, ids[0]):
        import_fill_cost_correction(
            engine.store, correction(fill, venue.now, fee_usd="0"), recorded_at=venue.now
        )
    watched = engine.admit(packet(mx, "DIA"))
    with engine.store.transaction() as conn:
        engine.store.transition(conn, watched, "INVALIDATED", exit_reason="FIXTURE_STOP_TOUCHED")
        engine.store.event(conn, "POSITION_REVIEW_UNAVAILABLE", {"reason": "FIXTURE_MISSING_BARS"},
                           setup_id=ids[0])
    pages, cursor = [], 0
    while True:
        page = paginated_managed_results(engine.repo, after_event_seq=cursor, limit=1)
        assert page["baseline_included"] is False
        assert "NOT_VALIDATED_STRATEGY_PERFORMANCE" in page["result_label"]
        pages.extend(page["items"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert [item["setup_id"] for item in pages] == [str(sid) for sid in ids]
    assert len({item["cursor_event_seq"] for item in pages}) == 3
    rollup = managed_daily_rollups(engine.repo)["items"][0]
    assert rollup["setup_count"] == 4 and rollup["entered_count"] == rollup["closed_count"] == 3
    assert rollup["state_counts"]["INVALIDATED"] == 1
    assert rollup["terminal_reason_counts"]["FIXTURE_STOP_TOUCHED"] == 1
    assert rollup["gross_known_count"] == 3
    assert rollup["net_known_count"] == 1 and rollup["net_unknown_closed_count"] == 2
    assert rollup["net_pnl_usd"] is None
    assert D(rollup["net_known_sum_usd"]) == D(pages[0]["net_pnl_usd"])
    assert rollup["failure_reason_event_counts"]["FIXTURE_MISSING_BARS"] == 1
    assert managed_daily_rollups(engine.repo, end_date="2000-01-01")["items"] == []
    with pytest.raises(ValueError, match="INVALID_ANALYTICS_CURSOR"):
        paginated_managed_results(engine.repo, limit=501)
