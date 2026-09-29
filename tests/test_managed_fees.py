"""Alpaca fee reads, matching and net/official R (package fees-net-r, plan phase 0).

Fixture evidence only: the "reproduces the first trade" tests build the shape of the
2026-09-25 first real managed trade (artifacts/first-managed-trade-2026-09-25/README.md,
evidence.json) — same setup levels, same broker order ids, the same four fill
quantities and prices where the artifact records them exactly (both exit fills; the
entry fills' individual split was never published, only their sum) — behind a fake
Alpaca activities endpoint. No network, no real broker or TypeSafe/Jev call, no owner
ledger; disposable PostgreSQL only.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials, AlpacaPaperClient
from catalyst_lab.managed_analytics import (
    import_alpaca_fee_activities,
    import_fill_cost_correction,
    managed_result_aggregates,
    match_fee_activity,
    normalize_fee_activity,
)
from catalyst_lab.managed_execution import FEE_BACKFILL_EVENT
from catalyst_lab.managed_measurement import OFFICIAL_R_METHOD, managed_measurement
from catalyst_lab.market import MarketDataError
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_analytics import close, correction
from tests.test_managed_analytics import fills as setup_fills
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import packet

CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")

# --- Real-trade fixture numbers (artifacts/first-managed-trade-2026-09-25) --------------
M, S, TARGET = "84630", "83680", "86530"
BUY_FILLS = (  # qty split is this fixture's own choice; only the total (0.052630789) and
    ("real-buy-1", D("0.026315390"), D("84595.25")),  # both prices are from the artifact.
    ("real-buy-2", D("0.026315399"), D("84605.52")),
)
SELL_FILLS = (  # both exit fills exactly as recorded (evidence.json "exit"/"fills").
    ("real-sell-1", D("0.00011951"), D("83675.06")),
    ("real-sell-2", D("0.052379701"), D("83616.292")),
)
BUY_FEE_QTY = (D("0.000065789"), D("0.000065789"))  # sums to bought-sold exactly (0.000131578)
SELL_FEE_CASH = (D("0.03"), D("10.95"))  # 0.25% taker of proceeds, rounded to cents


def insert_fill(engine, sid, *, fill_id, broker_order_id, side, qty, price, filled_at):
    """A managed fill exactly as production's own trade-updates/REST paths record one,
    but injected directly: this package tests fee evidence, not the fill pipeline
    (already covered by test_managed_execution.py/test_fill_backfill.py)."""
    with engine.store.transaction() as conn:
        engine.store.event(
            conn, "FIXTURE_FEES_NET_R_FILL_EVIDENCE",
            {"fill_id": fill_id, "broker_order_id": broker_order_id, "side": side},
            setup_id=sid, key="fixture-fill-evidence:" + fill_id,
        )
        conn.execute(
            """INSERT INTO lab.managed_fills(fill_id,setup_id,broker_order_id,
            side,qty,price,filled_at,fee_usd,source)
            VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,'ALPACA_PAPER')""",
            (fill_id, sid, broker_order_id, side, str(qty), str(price), filled_at),
        )


def build_real_trade(mx):
    """A CLOSED crypto setup carrying the real trade's levels and its four real fills,
    plus a fake Alpaca activities endpoint carrying the corresponding fee evidence."""
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "BTC/USD", levels={
        "entry_trigger": "84590", "max_entry_price": M, "stop": S, "target": TARGET,
    }))
    t0 = venue.now
    for (fill_id, qty, price), offset in zip(BUY_FILLS, (0, 1), strict=True):
        insert_fill(engine, sid, fill_id=fill_id, broker_order_id="entry-order", side="buy",
                    qty=qty, price=price, filled_at=t0 + timedelta(seconds=offset))
    t1 = t0 + timedelta(hours=4)
    for (fill_id, qty, price), offset in zip(SELL_FILLS, (0, 1), strict=True):
        insert_fill(engine, sid, fill_id=fill_id, broker_order_id="exit-order", side="sell",
                    qty=qty, price=price, filled_at=t1 + timedelta(seconds=offset))
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "CLOSED", exit_reason="STOP_LIMIT_NOT_FILLED")
    venue.fee_activities = [
        {"id": "fee-buy-1", "activity_type": "CFEE", "order_id": "entry-order",
         "transaction_time": t0.isoformat(), "qty": str(BUY_FEE_QTY[0]),
         "price": str(BUY_FILLS[0][2])},
        {"id": "fee-buy-2", "activity_type": "CFEE", "order_id": "entry-order",
         "transaction_time": (t0 + timedelta(seconds=1)).isoformat(),
         "qty": str(BUY_FEE_QTY[1]), "price": str(BUY_FILLS[1][2])},
        {"id": "fee-sell-1", "activity_type": "FEE", "order_id": "exit-order",
         "transaction_time": t1.isoformat(), "net_amount": "-" + str(SELL_FEE_CASH[0])},
        {"id": "fee-sell-2", "activity_type": "FEE", "order_id": "exit-order",
         "transaction_time": (t1 + timedelta(seconds=1)).isoformat(),
         "net_amount": "-" + str(SELL_FEE_CASH[1])},
        # An activity of an order this setup never placed: proves ``unmatched`` counting
        # without being confused with a fill whose own fee simply never posted.
        {"id": "fee-unrelated", "activity_type": "FEE", "order_id": "some-other-order",
         "transaction_time": t1.isoformat(), "net_amount": "-1"},
    ]
    venue.now = t1 + timedelta(minutes=5)
    return sid


# --- Pure parsing and matching -----------------------------------------------------------


def test_normalize_fee_activity_reads_cash_in_kind_and_confirmed_zero():
    at = "2026-09-25T09:59:01.517075Z"
    cash = normalize_fee_activity({
        "id": "a1", "activity_type": "FEE", "order_id": "o1", "transaction_time": at,
        "net_amount": "-10.95",
    })
    assert cash["cash_fee_usd"] == D("10.95")
    assert cash["base_asset_fee_qty"] == cash["base_asset_fee_usd"] == 0
    in_kind = normalize_fee_activity({
        "id": "a2", "activity_type": "CFEE", "order_id": "o1", "transaction_time": at,
        "qty": "0.0001", "price": "80000",
    })
    assert in_kind["cash_fee_usd"] == 0
    assert in_kind["base_asset_fee_qty"] == D("0.0001")
    assert in_kind["base_asset_fee_usd"] == D("0.0001") * D("80000")
    zero = normalize_fee_activity(
        {"id": "a3", "activity_type": "FEE", "order_id": "o1", "transaction_time": at}
    )
    assert zero["cash_fee_usd"] == 0 and zero["base_asset_fee_qty"] == 0


@pytest.mark.parametrize("change", [
    {"activity_type": "FILL"}, {"activity_type": "CSD"}, {"order_id": None}, {"order_id": ""},
    {"id": ""}, {"id": None}, {"qty": "-1"}, {"qty": "NaN"}, {"net_amount": "NaN"},
    {"transaction_time": "not-a-date"}, {"transaction_time": None},
    {"qty": "0.0001", "price": "0"}, {"qty": "0.0001", "price": "not-a-number"},
])
def test_normalize_fee_activity_refuses_malformed_rows(change):
    base = {"id": "a1", "activity_type": "FEE", "order_id": "o1",
            "transaction_time": "2026-09-25T09:59:01Z", "net_amount": "-1"}
    with pytest.raises(ValueError, match="INVALID_FEE_ACTIVITY"):
        normalize_fee_activity({**base, **change})


def test_match_fee_activity_by_order_and_closest_time_within_tolerance():
    fills = [
        {"fill_id": "f1", "broker_order_id": "o1", "filled_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"fill_id": "f2", "broker_order_id": "o1",
         "filled_at": datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)},
        {"fill_id": "f3", "broker_order_id": "o2", "filled_at": datetime(2026, 1, 1, tzinfo=UTC)},
    ]
    near_f2 = {"order_id": "o1", "at": datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)}
    assert match_fee_activity(fills, near_f2)["fill_id"] == "f2"
    exactly_f1 = {"order_id": "o1", "at": datetime(2026, 1, 1, tzinfo=UTC)}
    assert match_fee_activity(fills, exactly_f1)["fill_id"] == "f1"
    too_far = {"order_id": "o1", "at": datetime(2026, 1, 1, 0, 1, tzinfo=UTC)}
    assert match_fee_activity(fills, too_far) is None
    unknown_order = {"order_id": "o9", "at": datetime(2026, 1, 1, tzinfo=UTC)}
    assert match_fee_activity(fills, unknown_order) is None
    assert match_fee_activity([], exactly_f1) is None


# --- The paginated CFEE/FEE activity reader (mirrors test_fill_backfill.py) --------------


def test_fee_activity_reader_pages_oldest_first_and_refuses_incomplete_or_stale():
    rows = [{"id": f"{n:05d}::fixture", "activity_type": "CFEE"} for n in range(150)]
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        start = 0
        if "page_token" in params:
            start = [r["id"] for r in rows].index(params["page_token"]) + 1
        return httpx.Response(200, json=rows[start:start + int(params["page_size"])])

    client = AlpacaPaperClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    after = datetime(2026, 9, 25, 9, 0, 0, tzinfo=UTC)
    try:
        assert client.fee_activities_since(after) == rows
        assert [p.get("page_token") for p in seen] == [None, "00099::fixture"]
        assert all(
            p["activity_types"] == "CFEE,FEE" and p["direction"] == "asc"
            and p["after"] == "2026-09-25T09:00:00Z"
            for p in seen
        )
        with pytest.raises(MarketDataError, match="AWARE_ACTIVITY_WINDOW_REQUIRED"):
            client.fee_activities_since(after.replace(tzinfo=None))
    finally:
        client.close()

    def repeating(request):
        return httpx.Response(200, json=rows[:100])  # The page token never advances.

    looping = AlpacaPaperClient(CREDENTIALS, transport=httpx.MockTransport(repeating))
    try:
        with pytest.raises(MarketDataError, match="INCOMPLETE_FEE_ACTIVITIES"):
            looping.fee_activities_since(after)
    finally:
        looping.close()
    limited = AlpacaPaperClient(
        CREDENTIALS, transport=httpx.MockTransport(lambda r: httpx.Response(429, json={}))
    )
    try:
        with pytest.raises(MarketDataError, match="ALPACA_HTTP_429"):
            limited.fee_activities_since(after)
    finally:
        limited.close()


# --- Reproducing the first real managed trade (fixture evidence only) -------------------


def test_alpaca_fee_activities_reproduce_first_managed_trade_net_and_official_r(mx):
    engine, _venue, _ = mx
    sid = build_real_trade(mx)
    summary = engine.fee_backfill()
    assert summary["activities_read"] == 5
    assert summary["matched"] == 4
    assert summary["unmatched"] == 1  # the unrelated order's activity
    assert summary["already_recorded"] == summary["invalid"] == summary["conflicting"] == 0

    measurement = managed_measurement(engine.repo, sid)
    assert measurement["fees_verified"] and measurement["inventory_reconciled_with_costs"]
    assert measurement["official_r_method"] == OFFICIAL_R_METHOD
    # No reservation exists for this directly-injected fixture (admission only, no
    # authorize_entry/risk reservation): the reservation-denominated fields stay null.
    assert measurement["test_r"] is None and measurement["net_r"] is None
    assert D(measurement["planned_filled_risk"]) == D("0.052630789") * (D(M) - D(S))

    net = D(measurement["net_pnl"])
    official_r = D(measurement["official_r"])
    gross_r = D(measurement["gross_realized_pnl"]) / D(measurement["planned_filled_risk"])
    # Exact given this fixture's own literals (the real entry fills' individual split
    # was never published, only their sum, so this is not bit-exact against the artifact).
    assert abs(net - D("-73.77")) < D("0.05")
    assert abs(official_r - D("-1.4754")) < D("0.001")
    # The owner's reference figures for the real trade (net ~-$74.03, official R ~-1.48,
    # gross R ~-1.26): approximate, since the split above is this fixture's own choice.
    assert abs(net - D("-74.03")) < D("0.5")
    assert abs(official_r - D("-1.48")) < D("0.02")
    assert abs(gross_r - D("-1.26")) < D("0.02")


def test_alpaca_fee_import_is_idempotent_on_replay(mx):
    engine, _venue, _ = mx
    sid = build_real_trade(mx)
    first = engine.fee_backfill()
    assert first["matched"] == 4 and first["already_recorded"] == 0
    before = managed_measurement(engine.repo, sid)
    assert before["net_pnl"] is not None

    second = engine.fee_backfill()
    assert second["matched"] == 0 and second["already_recorded"] == 4
    after = managed_measurement(engine.repo, sid)
    assert after["net_pnl"] == before["net_pnl"]
    assert after["cost_correction_ids"] == before["cost_correction_ids"]
    assert len(after["cost_correction_ids"]) == 4


def test_fee_import_records_a_run_only_on_changed_counts_or_after_the_refresh(mx):
    """2026-09-28: every 30 s run was recorded, 20% of the ledger's managed events. A run
    whose counts match the last recorded run's is recorded only once the refresh interval
    has passed, which keeps the window's anchor moving."""
    engine, _venue, _ = mx
    build_real_trade(mx)

    def recorded():
        with engine.repo.connect() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM lab.managed_events WHERE kind=%s",
                (FEE_BACKFILL_EVENT,),
            ).fetchone()["n"]

    assert engine.fee_backfill()["matched"] == 4 and recorded() == 1  # the first run
    assert engine.fee_backfill()["already_recorded"] == 4 and recorded() == 2  # changed
    for _ in range(3):
        assert engine.fee_backfill()["already_recorded"] == 4
    assert recorded() == 2  # the same counts, inside the refresh interval
    engine.fee_backfill(refresh=timedelta(0))
    assert recorded() == 3  # the same counts, the refresh interval passed


def test_missing_fee_activity_keeps_that_fee_unknown_and_net_null(mx):
    engine, venue, _ = mx
    sid = build_real_trade(mx)
    # The smaller exit fill's fee never posted at Alpaca yet (e.g. still in flight).
    venue.fee_activities = [a for a in venue.fee_activities if a["id"] != "fee-sell-1"]
    summary = engine.fee_backfill()
    assert summary["matched"] == 3
    assert summary["unmatched"] == 1  # still the unrelated order's activity, not this gap
    measurement = managed_measurement(engine.repo, sid)
    assert measurement["net_pnl"] is None and not measurement["fees_verified"]
    assert measurement["gross_realized_pnl"] is not None  # gross never needs fee evidence
    unresolved = [c for c in measurement["cost_evidence"] if c["fill_id"] == "real-sell-1"]
    assert unresolved == [{"fill_id": "real-sell-1", "source": "UNKNOWN",
                           "correction_id": None, "invalid_correction": False}]


def test_stock_fill_with_confirmed_zero_and_nonzero_fee_activity_type(mx):
    """A US_STOCKS setup: the ``FEE`` activity type (not just crypto's ``CFEE``), one
    fill with a confirmed-zero fee (a stock buy, which Alpaca never charges) and one
    with a small nonzero regulatory-style fee (a stock sell)."""
    engine, venue, _ = mx
    sid = close(mx, "SPY")
    entry, exit_ = setup_fills(engine, sid)
    venue.fee_activities = [
        {"id": "spy-fee-buy", "activity_type": "FEE", "order_id": entry["broker_order_id"],
         "transaction_time": entry["filled_at"].isoformat(), "net_amount": "0"},
        {"id": "spy-fee-sell", "activity_type": "FEE", "order_id": exit_["broker_order_id"],
         "transaction_time": exit_["filled_at"].isoformat(), "net_amount": "-0.02"},
    ]
    summary = engine.fee_backfill()
    assert summary["matched"] == 2 and summary["unmatched"] == 0

    measurement = managed_measurement(engine.repo, sid)
    assert measurement["fees_verified"]
    assert D(measurement["verified_cash_fee_usd"]) == D("0.02")
    assert D(measurement["net_pnl"]) == D(measurement["gross_realized_pnl"]) - D("0.02")
    # A stock entry always fully fills (whole shares, no in-kind fee): the reservation's
    # authorized quantity and the actually filled quantity are the same denominator.
    assert D(measurement["official_r"]) == D(measurement["net_r"])
    assert D(measurement["official_r"]) == D(measurement["net_pnl"]) / D(
        measurement["planned_filled_risk"]
    )


# --- Aggregates ---------------------------------------------------------------------------


def test_result_aggregates_exclude_fixture_tainted_net_but_count_and_flag_them(mx):
    engine, venue, _ = mx
    fixture_sid = close(mx, "SPY")
    for fill in setup_fills(engine, fixture_sid):
        import_fill_cost_correction(
            engine.store, correction(fill, venue.now), recorded_at=venue.now
        )

    verified_sid = close(mx, "AAPL")
    verified_fills = setup_fills(engine, verified_sid)
    venue.fee_activities = [
        {"id": f"aapl-fee-{i}", "activity_type": "FEE", "order_id": fill["broker_order_id"],
         "transaction_time": fill["filled_at"].isoformat(), "net_amount": "0"}
        for i, fill in enumerate(verified_fills)
    ]
    assert engine.fee_backfill()["matched"] == len(verified_fills)

    fixture_measurement = managed_measurement(engine.repo, fixture_sid)
    verified_measurement = managed_measurement(engine.repo, verified_sid)
    assert fixture_measurement["net_pnl"] is not None  # verified at the per-setup level
    assert verified_measurement["net_pnl"] is not None

    aggregates = managed_result_aggregates(engine.repo, group_by=("market",))
    assert aggregates["fixture_scope"] == (
        "LAB_FIXTURE_COST_EVIDENCE_EXCLUDED_FROM_VERIFIED_AGGREGATES"
    )
    item = next(i for i in aggregates["items"] if i["market"] == "US_STOCKS")
    assert item["count"] == 2
    assert item["fixture_tainted_count"] == 1
    assert item["win_rate"] is not None  # gross-based: neither setup needs fee evidence
    assert item["mean_gross_r"] is not None
    assert item["mean_net_r"] is not None
    assert item["mean_official_r"] is not None
    assert D(item["mean_net_r"]) == D(verified_measurement["net_r"])
    assert D(item["mean_official_r"]) == D(verified_measurement["official_r"])
    assert D(item["total_fees_usd"]) == D(verified_measurement["verified_cash_fee_usd"])

    with pytest.raises(ValueError, match="INVALID_AGGREGATE_DIMENSIONS"):
        managed_result_aggregates(engine.repo, group_by=())
    with pytest.raises(ValueError, match="INVALID_AGGREGATE_DIMENSIONS"):
        managed_result_aggregates(engine.repo, group_by=("not_a_real_dimension",))


def test_import_alpaca_fee_activities_pure_function_counts_every_outcome():
    """The matcher/importer alone, against plain dicts — no broker, no admitted setup.

    Neither activity below reaches a write (one is invalid, the other unmatched), so a
    placeholder stands in for the store: this proves the counting is right without a
    database.
    """
    fills = [{"fill_id": "f1", "setup_id": "s1", "broker_order_id": "o1", "event_seq": 1,
              "filled_at": datetime(2026, 1, 1, tzinfo=UTC)}]
    activities = [
        {"id": "a1", "activity_type": "NOT_A_FEE"},  # invalid
        {"id": "a2", "activity_type": "FEE", "order_id": "o9",  # unmatched: unknown order
         "transaction_time": "2026-01-01T00:00:00Z", "net_amount": "-1"},
    ]
    result = import_alpaca_fee_activities(
        object(), fills, activities, recorded_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result == {"activities_read": 2, "matched": 0, "already_recorded": 0,
                      "unmatched": 1, "invalid": 1, "conflicting": 0}
