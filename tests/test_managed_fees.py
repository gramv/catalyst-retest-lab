"""Alpaca fee reads, matching and net/official R (package fees-net-r, plan phase 0).

Fixture evidence only: the "reproduces the first trade" tests build the shape of the
2026-09-25 first real managed trade (artifacts/first-managed-trade-2026-09-25/README.md,
evidence.json) — same setup levels, same broker order ids, the same four fill
quantities and prices where the artifact records them exactly (both exit fills; the
entry fills' individual split was never published, only their sum) — behind a fake
Alpaca activities endpoint. No network, no real broker or TypeSafe/Jev call, no owner
ledger; disposable PostgreSQL only.

ALPACA_FEE_MATCH_V2 (2026-09-29) is tested on the live paper account's real CFEE rows
and the ledger's real fills (tests/fixtures/alpaca_cfee_2026_09_29.json, ids synthetic),
still behind the fake activities endpoint.
"""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials, AlpacaPaperClient
from catalyst_lab.managed_analytics import (
    FEE_MATCH_V1,
    FEE_MATCH_V2,
    FEE_MATCH_V3,
    assign_fee_activities_v2,
    fee_correction_id,
    import_alpaca_fee_activities,
    import_fill_cost_correction,
    managed_result_aggregates,
    match_fee_activity,
    normalize_fee_activity,
)
from catalyst_lab.managed_execution import FEE_BACKFILL_COUNTS, FEE_BACKFILL_EVENT
from catalyst_lab.managed_measurement import OFFICIAL_R_METHOD, managed_measurement
from catalyst_lab.market import MarketDataError, timestamp
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
                      "unmatched": 1, "invalid": 1, "conflicting": 0, "ambiguous": 0}


# --- ALPACA_FEE_MATCH_V2: the live paper account's real CFEE rows (2026-09-29) -----------
#
# The 31 CFEE activities the live paper account returned on 2026-09-29 (ids synthetic, in
# the live format) and the ledger's 31 managed fills (fill ids synthetic; the order group is
# the first 8 characters of the real order id). No row carries an order id or a transaction
# time, so every one was ``invalid`` under V1 and no fee was ever recorded.

REAL = json.loads((Path(__file__).parent / "fixtures" / "alpaca_cfee_2026_09_29.json").read_text())
REAL_POSTED_LAST = max(timestamp(raw["created_at"]) for raw in REAL["cfee_activities"])
# The binding worked out by hand from the amounts: activity uuid prefix -> fill id prefix.
# A coin fee is rate x the buy's quantity, rounded up to 1e-9 of the coin; a USD fee is
# rate x the sell's notional, rounded up to the cent. Taker 0.25% unless marked maker 0.15%.
REAL_BINDING = {
    "93a6a1fb": "9fbb8a66",  # UNI buy 112.962074947: 0.282405188 (0.2824051873675 up)
    "f8ff09f3": "b59eccfd",  # LTC buy 12.802763388: 0.032006909 (0.03200690847 up)
    "7b93c848": "a7b09fe3",  # LTC sell, 880.43: $2.21 (2.2011 up)
    "0290e956": "3b65e688",  # UNI sell, 653.69: $1.64
    "5b0db14f": "408741bd",  # UNI sell, 340.17: $0.86
    "fd9e2d85": "c07d2d55",  # GRT buy 8578.621: 21.4465525
    "5d7acde9": "3c1e2a39",  # GRT buy 463.56819833: 0.695352298, maker
    "a25dbeea": "8b055284",  # GRT buy 8495.93: 12.743895, maker
    "99079d48": "c1c4ec17",  # UNI sell 02:31, 969.15: $2.43, posted 20 min later
    "f59a0f24": "bbcde682",  # FIL buy 02:37: 2.434493323, posted 14 min later
    "1ff99823": "34858586",  # GRT sell 00:51, 262.98: $0.66, posted 04:21
    "f1df6cbd": "a2b01404",  # GRT sell 00:51, 271.86: $0.68, posted 04:21
    "c46800de": "4b3b3648",  # UNI buy 01:20: 0.287780913, posted 04:21
    "d0d35215": "e6dbe827",  # YFI buy 0.1338: 0.0003345 (the order's taker part)
    "38cd766e": "cd09efe6",  # YFI buy 0.134438: 0.000201657, maker
    "360ad069": "39a0494f",  # YFI buy 0.1343: 0.00020145, maker
    "feaf5d1e": "4e6042d5",  # YFI buy 0.023337901: 0.000035007, maker
    "465f1d04": "28629a88",  # DOT buy 788.4: 1.971
    "d0af69ec": "ade2eb74",  # DOT buy 73.277076045: 0.109915615, maker
    "b24a6aa5": "2def856b",  # SUSHI buy 1111.636: 2.77909
    "3fbf334e": "ee34bad6",  # SUSHI buy 2221.09: 5.552725
    "e5830bd4": "25cb31f5",  # SUSHI buy 693.900456419: 1.040850685, maker
    "26ae1fa3": "b2958230",  # DOT sell, 909.38: $2.28
    "02364304": "dcfb5e2b",  # DOT sell, 81.59: $0.21
    "4da2c6f3": "d2ca69ad",  # YFI sell, 311.81: $0.78
    "5a95f14b": "7c10f858",  # YFI sell, 619.49: $1.55
    "11d34c9c": "b5cf8a18",  # YFI sell, 57.55: $0.15
    "6f7b9767": "2b295b8f",  # SUSHI sell, 277.99: $0.70
    "e0378141": "a0b11fe7",  # SUSHI sell, 548.77: $1.38
    "e95f59ab": "e672e0f6",  # SUSHI sell, 166.62: $0.42 (also 0.15% of 277.99)
    "2662e990": "24ee36d2",  # FIL sell, 1014.10: $2.54
}
# The eight closed trades: symbol, fill id prefixes, and USD sell fees ($18.49 in all).
REAL_TRADES = (
    ("UNI/USD", ("9fbb8a66", "3b65e688", "408741bd"), D("2.50")),  # 1.64 + 0.86
    ("LTC/USD", ("b59eccfd", "a7b09fe3"), D("2.21")),
    ("GRT/USD", ("c07d2d55", "8b055284", "3c1e2a39", "34858586", "a2b01404"), D("1.34")),
    ("UNI/USD", ("4b3b3648", "c1c4ec17"), D("2.43")),
    ("YFI/USD", ("e6dbe827", "cd09efe6", "39a0494f", "4e6042d5",
                 "d2ca69ad", "7c10f858", "b5cf8a18"), D("2.48")),  # 0.78 + 1.55 + 0.15
    ("DOT/USD", ("28629a88", "ade2eb74", "b2958230", "dcfb5e2b"), D("2.49")),  # 2.28 + 0.21
    ("SUSHI/USD", ("2def856b", "ee34bad6", "25cb31f5",
                   "2b295b8f", "a0b11fe7", "e672e0f6"), D("2.50")),  # 0.70 + 1.38 + 0.42
    ("FIL/USD", ("bbcde682", "24ee36d2"), D("2.54")),
)


def short(activity_id):
    """An activity id's uuid prefix (``YYYYMMDD000000000::<uuid>``)."""
    return activity_id.split("::")[1][:8]


def real_row(prefix):
    return next(raw for raw in REAL["cfee_activities"] if short(raw["id"]) == prefix)


COIN_ROW = real_row("93a6a1fb")  # UNIUSD, "Coin Pair Transaction Fee (Non USD)"
USD_ROW = real_row("7b93c848")  # the LTC sell's $2.21, "Coin Pair Transaction Fee (USD)"


def real_activities(shift=timedelta(0), *, only=None):
    """The real rows moved by ``shift`` (``date`` follows ``created_at``), or a subset."""
    rows = []
    for raw in REAL["cfee_activities"]:
        if only is None or short(raw["id"]) in only:
            at = timestamp(raw["created_at"]) + shift
            rows.append({**raw, "created_at": at.isoformat(), "date": at.date().isoformat()})
    return rows


def real_fill_rows():
    """The real fills as ``fee_match_fills`` reads them, before any fee evidence."""
    return [
        {"fill_id": f["fill_id"], "setup_id": f"setup-{f['symbol']}",
         "broker_order_id": f["broker_order_id_group"], "side": f["side"], "qty": D(f["qty"]),
         "price": D(f["price"]), "filled_at": timestamp(f["filled_at"]), "fee_usd": None,
         "source": "ALPACA_PAPER", "event_seq": n, "symbol": f["symbol"], "market": "CRYPTO",
         "cost_correction_ids": []}
        for n, f in enumerate(REAL["ledger_fills"], start=1)
    ]


def real_fill(prefix):
    return next(f for f in real_fill_rows() if f["fill_id"].startswith(prefix))


def assign_real(fills, only=None):
    activities = [normalize_fee_activity(raw) for raw in real_activities(only=only)]
    return assign_fee_activities_v2(fills, activities)


def pairs(bindings):
    return {short(activity["activity_id"]): fill["fill_id"][:8] for activity, fill in bindings}


def real_shift(now):
    """Whole days that move the sample to just before ``now``: every lag, and so every
    binding, is unchanged, and the database's real-time checks still hold."""
    return timedelta(days=(now - timedelta(minutes=1) - REAL_POSTED_LAST).days)


def admit_real_trade(mx, symbol, prefixes, shift):
    """A CLOSED crypto setup carrying these real fills, moved by ``shift``."""
    engine, _venue, _ = mx
    sid = engine.admit(packet(mx, symbol))
    for fill in REAL["ledger_fills"]:
        if fill["fill_id"][:8] in prefixes:
            insert_fill(engine, sid, fill_id=fill["fill_id"],
                        broker_order_id=fill["broker_order_id_group"], side=fill["side"],
                        qty=D(fill["qty"]), price=D(fill["price"]),
                        filled_at=timestamp(fill["filled_at"]) + shift)
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "CLOSED", exit_reason="STOP_LIMIT_NOT_FILLED")
    return sid


def fee_evidence(engine):
    """Every FILL_COST_CORRECTION body, oldest first."""
    with engine.repo.connect() as conn:
        return [row["body"] for row in conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind='FILL_COST_CORRECTION' "
            "ORDER BY event_seq"
        ).fetchall()]


def counts(summary):
    return {name: summary[name] for name in FEE_BACKFILL_COUNTS}


def test_v2_reads_both_real_cfee_shapes_without_order_id_or_transaction_time():
    rows = REAL["cfee_activities"]
    assert not any("order_id" in raw or "transaction_time" in raw for raw in rows)
    coin = normalize_fee_activity(COIN_ROW)
    assert (coin["match_policy"], coin["fee_kind"], coin["symbol"]) == (
        FEE_MATCH_V2, "COIN", "UNIUSD"
    )
    assert coin["at"] == datetime(2026, 9, 28, 14, 29, 34, 569345, tzinfo=UTC)
    assert coin["cash_fee_usd"] == 0
    assert coin["base_asset_fee_qty"] == D("0.282405188")
    assert coin["base_asset_fee_usd"] == D("0.282405188") * D("8.78")
    usd = normalize_fee_activity(USD_ROW)
    assert (usd["fee_kind"], usd["symbol"], usd["cash_fee_usd"]) == ("USD", None, D("2.21"))
    assert usd["base_asset_fee_qty"] == usd["base_asset_fee_usd"] == 0
    assert Counter(normalize_fee_activity(raw)["fee_kind"] for raw in rows) == {
        "COIN": 16, "USD": 15,
    }
    # A row with an order id is still read by the fees-net-r rule.
    v1 = normalize_fee_activity({"id": "a1", "activity_type": "CFEE", "order_id": "o1",
                                 "transaction_time": "2026-09-25T09:59:01Z",
                                 "net_amount": "-1"})
    assert (v1["match_policy"], v1["order_id"]) == (FEE_MATCH_V1, "o1")


@pytest.mark.parametrize(("row", "change"), [
    (COIN_ROW, {"qty": "0.282405188"}),  # a positive coin quantity is not the verified shape
    (COIN_ROW, {"qty": "0"}),
    (COIN_ROW, {"qty": -0.282405188}),  # a float is never exact
    (COIN_ROW, {"price": None}),
    (COIN_ROW, {"price": "-8.78"}),  # a zero price is V3 below; a negative one is nothing
    (COIN_ROW, {"price": "0", "net_amount": "-0.10"}),  # no price and a cash debit as well
    (COIN_ROW, {"symbol": None}),
    (COIN_ROW, {"net_amount": "-2.48"}),  # a coin fee with a cash debit as well
    (USD_ROW, {"net_amount": "0"}),
    (USD_ROW, {"net_amount": "2.21"}),
    (USD_ROW, {"net_amount": None}),
    (USD_ROW, {"qty": "-1"}),  # a quantity without symbol and price
    (USD_ROW, {"status": "canceled"}),
    (USD_ROW, {"currency": "EUR"}),
    (USD_ROW, {"activity_type": "FEE"}),  # V2 reads crypto CFEE rows only
    (USD_ROW, {"created_at": None}),
    (USD_ROW, {"created_at": "not-a-date"}),
    (USD_ROW, {"created_at": "2026-09-28T15:16:58"}),  # no zone
    (USD_ROW, {"id": ""}),
])
def test_v2_refuses_every_other_cfee_shape(row, change):
    with pytest.raises(ValueError, match="INVALID_FEE_ACTIVITY"):
        normalize_fee_activity({**row, **change})


# --- ALPACA_FEE_MATCH_V3: a coin fee posted without a price (2026-10-02) -----------------
#
# From 2026-09-30 the live paper account posted every PEPE, SHIB and BONK buy fee with
# ``price`` "0" (a price with more decimals than the field carries): 42 rows in the 10-02 read,
# all ``invalid`` under V2, so those trades' fees stayed pending for days. The row below is
# the PEPE one of 2026-09-30 19:15 UTC as Alpaca returned it (id synthetic).

PEPE_ROW = {
    "id": "20260930000000000::6f0c5e2a-7d1b-4c3e-9a8f-2b4d6e8f0a1c", "activity_type": "CFEE",
    "date": "2026-09-30", "created_at": "2026-09-30T19:15:30.31651Z", "net_amount": "0",
    "description": "Coin Pair Transaction Fee (Non USD)", "symbol": "PEPEUSD",
    "cusip": "PEPE12345", "qty": "-87139.031206335", "price": "0", "status": "executed",
    "currency": "USD",
}
PEPE_BUY_QTY, PEPE_BUY_PRICE = D("34855612.482534"), D("0.00000438")  # 0.25% of it is the fee


def test_v3_reads_a_coin_fee_posted_without_a_price():
    activity = normalize_fee_activity(PEPE_ROW)
    assert (activity["match_policy"], activity["fee_kind"], activity["symbol"]) == (
        FEE_MATCH_V3, "COIN", "PEPEUSD"
    )
    assert activity["at"] == datetime(2026, 9, 30, 19, 15, 30, 316510, tzinfo=UTC)
    assert activity["cash_fee_usd"] == 0
    assert activity["base_asset_fee_qty"] == D("87139.031206335")
    assert activity["base_asset_fee_usd"] is None  # valued at the fill it binds to
    for change in ({"price": "-0.00000438"}, {"price": "0", "net_amount": "-0.10"},
                   {"price": "0", "qty": "87139.031206335"}, {"price": "0", "symbol": None}):
        with pytest.raises(ValueError, match="INVALID_FEE_ACTIVITY"):
            normalize_fee_activity({**PEPE_ROW, **change})


def test_v3_row_without_its_buy_is_unmatched_not_invalid():
    result = import_alpaca_fee_activities(
        object(), real_fill_rows(), [PEPE_ROW], recorded_at=datetime.now(UTC)
    )
    assert (result["invalid"], result["unmatched"], result["matched"]) == (0, 1, 0)


def test_v3_binds_like_v2_and_values_the_fee_at_its_fills_price(mx):
    """The PEPE buy's fee binds to the PEPE buy whose quantity at 0.25% rounds up to the
    posted coins, and is recorded at that fill's price: 87,139.03 PEPE x $0.00000438."""
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "PEPE/USD"))
    t0 = venue.now
    insert_fill(engine, sid, fill_id="pepe-buy", broker_order_id="pepe-entry", side="buy",
                qty=PEPE_BUY_QTY, price=PEPE_BUY_PRICE, filled_at=t0)
    insert_fill(engine, sid, fill_id="pepe-buy-other", broker_order_id="pepe-entry",
                side="buy", qty=PEPE_BUY_QTY * 2, price=PEPE_BUY_PRICE,
                filled_at=t0 + timedelta(seconds=1))  # the same coin, a different quantity
    posted = t0 + timedelta(seconds=3)
    venue.fee_activities = [
        {**PEPE_ROW, "created_at": posted.isoformat(), "date": posted.date().isoformat()},
    ]
    venue.now = posted + timedelta(minutes=1)
    summary = engine.fee_backfill()
    assert counts(summary) == {"activities_read": 1, "matched": 1, "already_recorded": 0,
                               "unmatched": 0, "invalid": 0, "conflicting": 0,
                               "ambiguous": 0}
    [body] = fee_evidence(engine)
    assert body["fill_id"] == "pepe-buy"
    prefix = "alpaca-activity:" + PEPE_ROW["id"] + ";" + FEE_MATCH_V3 + ";valued-at-fill-price:"
    assert body["broker_source_reference"].startswith(prefix)
    assert D(body["broker_source_reference"].removeprefix(prefix)) == PEPE_BUY_PRICE
    assert D(body["base_asset_fee_qty"]) == D("87139.031206335")
    assert D(body["base_asset_fee_usd"]) == D(body["fee_usd"]) == (
        D("87139.031206335") * PEPE_BUY_PRICE
    )
    # A second run replays its own binding and records nothing new.
    again = engine.fee_backfill(lookback=timedelta(days=1))
    assert (again["already_recorded"], again["matched"], again["invalid"]) == (1, 0, 0)


def test_v2_binds_yfi_partial_fills_charged_at_different_rates():
    """One YFI order filled in four parts: Alpaca charged one part 0.25% and three 0.15%,
    all posted together at 04:21:52, 2 h 51 min later. Each fee binds to its own part."""
    recorded, bound, unmatched, ambiguous = assign_real(
        real_fill_rows(), only={"38cd766e", "360ad069", "feaf5d1e", "d0d35215"}
    )
    assert pairs(bound) == {
        "d0d35215": "e6dbe827",  # 0.0003345 = 0.25% x 0.1338
        "360ad069": "39a0494f",  # 0.00020145 = 0.15% x 0.1343
        "38cd766e": "cd09efe6",  # 0.000201657 = 0.15% x 0.134438
        "feaf5d1e": "4e6042d5",  # 0.000035007 = 0.15% x 0.023337901, rounded up
    }
    assert recorded == unmatched == ambiguous == []


def test_v2_binds_the_0421_batch_posted_hours_after_its_fills():
    """At 04:21-04:22 UTC Alpaca posted the fees of the buys made 01:20-02:09 and of the
    GRT sells made at 00:51. Each of the 12 binds to its own fill."""
    batch = {short(raw["id"]) for raw in REAL["cfee_activities"]
             if raw["created_at"].startswith("2026-09-29T04:2")}
    assert len(batch) == 12
    recorded, bound, unmatched, ambiguous = assign_real(real_fill_rows(), only=batch)
    assert pairs(bound) == {key: REAL_BINDING[key] for key in batch}
    assert recorded == unmatched == ambiguous == []
    lags = [activity["at"] - fill["filled_at"] for activity, fill in bound]
    assert min(lags) > timedelta(hours=2, minutes=13)
    assert max(lags) > timedelta(hours=3, minutes=29)


def test_v2_usd_fees_name_no_coin_and_bind_sells_of_different_coins():
    """The nine USD fees posted 05:32-06:37 UTC carry no symbol. Each binds to its own sell
    among the DOT, YFI, SUSHI and FIL sells, whatever order the rows and fills come in."""
    late = {short(raw["id"]) for raw in REAL["cfee_activities"]
            if "symbol" not in raw and raw["created_at"] >= "2026-09-29T05"}
    assert len(late) == 9
    expected = {key: REAL_BINDING[key] for key in late}
    fills = real_fill_rows()
    activities = [normalize_fee_activity(raw) for raw in real_activities(only=late)]
    for rows, order in ((fills, activities), (fills[::-1], activities[::-1])):
        recorded, bound, unmatched, ambiguous = assign_fee_activities_v2(rows, order)
        assert pairs(bound) == expected
        assert recorded == unmatched == ambiguous == []
    bound_coins = {f["symbol"] for f in fills if f["fill_id"][:8] in expected.values()}
    assert bound_coins == {"DOT/USD", "YFI/USD", "SUSHI/USD", "FIL/USD"}


def test_v2_never_guesses_between_two_sells_with_the_same_cent_fee():
    """$0.42 is 0.25% of the SUSHI sell of 166.62 and 0.15% of the SUSHI sell of 277.99,
    both rounded up to the cent. With both sells still without fee evidence the fee is
    ambiguous and nothing is written; once the $0.70 fee takes 277.99, it binds."""
    fills = real_fill_rows()
    recorded, bound, unmatched, ambiguous = assign_real(fills, only={"e95f59ab"})
    assert recorded == bound == unmatched == [] and len(ambiguous) == 1
    result = import_alpaca_fee_activities(  # No write is attempted: no store is needed.
        object(), fills, real_activities(only={"e95f59ab"}), recorded_at=datetime.now(UTC)
    )
    assert result == {"activities_read": 1, "matched": 0, "already_recorded": 0,
                      "unmatched": 0, "invalid": 0, "conflicting": 0, "ambiguous": 1}
    recorded, bound, unmatched, ambiguous = assign_real(fills, only={"e95f59ab", "6f7b9767"})
    assert pairs(bound) == {"6f7b9767": "2b295b8f", "e95f59ab": "e672e0f6"}
    # Two rows whose only candidate is the same sell: neither is guessed.
    twin = {**USD_ROW, "id": "20260928000000000::" + str(uuid4())}
    recorded, bound, unmatched, ambiguous = assign_fee_activities_v2(
        fills, [normalize_fee_activity(USD_ROW), normalize_fee_activity(twin)]
    )
    assert recorded == bound == unmatched == [] and len(ambiguous) == 2


def test_v2_binds_a_fill_at_most_24_hours_before_the_posting_and_5_seconds_after():
    sell = real_fill("a7b09fe3")  # the LTC sell of USD_ROW's $2.21

    def outcome(posted_at):
        activity = normalize_fee_activity({**USD_ROW, "created_at": posted_at.isoformat()})
        recorded, bound, unmatched, ambiguous = assign_fee_activities_v2([sell], [activity])
        return len(bound), len(unmatched)

    at = sell["filled_at"]
    assert outcome(at + timedelta(hours=24)) == (1, 0)
    assert outcome(at + timedelta(hours=24, microseconds=1)) == (0, 1)
    assert outcome(at - timedelta(seconds=5)) == (1, 0)  # posting clock 5 s behind
    assert outcome(at - timedelta(seconds=5, microseconds=1)) == (0, 1)
    late = {**USD_ROW, "created_at": (at + timedelta(hours=25)).isoformat()}
    result = import_alpaca_fee_activities(object(), [sell], [late], recorded_at=at)
    assert (result["unmatched"], result["matched"]) == (1, 0)


def test_v2_skips_fills_with_fee_evidence_and_replays_its_own_binding():
    sell = real_fill("a7b09fe3")
    activity = normalize_fee_activity(USD_ROW)
    own = {**sell, "cost_correction_ids": [fee_correction_id(activity["activity_id"],
                                                             sell["fill_id"])]}
    assert assign_fee_activities_v2([own], [activity]) == ([(activity, own)], [], [], [])
    for evidenced in (
        {**sell, "cost_correction_ids": [str(uuid4())]},  # another correction
        {**sell, "fee_usd": D("2.21")},  # a fee on the fill event itself
        {k: v for k, v in sell.items() if k != "cost_correction_ids"},  # evidence unknown
    ):
        assert assign_fee_activities_v2([evidenced], [activity]) == ([], [], [activity], [])
    taken = assign_fee_activities_v2([sell], [activity], taken={sell["fill_id"]})
    assert taken == ([], [], [activity], [])  # bound by V1 in the same run


def test_v1_rows_keep_order_id_matching_and_their_fills_leave_v2(mx):
    """A row with an order id and a transaction time binds by ALPACA_FEE_MATCH_V1 exactly
    as before; a V2 row whose only consistent fill V1 took stays unmatched."""
    engine, venue, _ = mx
    sid = build_real_trade(mx)  # V1-shaped rows only
    sell_fee = next(a for a in venue.fee_activities if a["id"] == "fee-sell-2")
    posted = timestamp(sell_fee["transaction_time"]) + timedelta(seconds=2)
    venue.fee_activities.append({  # $10.95 = 0.25% x 4379.80, rounded up: real-sell-2's fee
        "id": "20260929000000000::" + str(uuid4()), "activity_type": "CFEE",
        "date": posted.date().isoformat(), "created_at": posted.isoformat(),
        "net_amount": "-10.95", "description": "Coin Pair Transaction Fee (USD)",
        "status": "executed", "currency": "USD",
    })
    summary = engine.fee_backfill()
    assert counts(summary) == {"activities_read": 6, "matched": 4, "already_recorded": 0,
                               "unmatched": 2, "invalid": 0, "conflicting": 0,
                               "ambiguous": 0}
    assert sorted(body["broker_source_reference"] for body in fee_evidence(engine)) == [
        "alpaca-activity:" + name
        for name in ("fee-buy-1", "fee-buy-2", "fee-sell-1", "fee-sell-2")
    ]
    assert managed_measurement(engine.repo, sid)["fees_verified"]


def test_real_sample_binds_every_fee_to_exactly_one_fill_and_nets_the_usd_sell_fees(mx):
    """The full sample: 31 fee rows, 31 fills, 8 closed trades. Every row binds to the fill
    worked out by hand. Each trade's net P&L is its gross less its USD sell fees: a coin
    fee is the coins never sold, already in gross, and never subtracted again."""
    engine, venue, _ = mx
    shift = real_shift(venue.now)
    trades = [(admit_real_trade(mx, symbol, prefixes, shift), prefixes, usd_fees)
              for symbol, prefixes, usd_fees in REAL_TRADES]
    venue.fee_activities = real_activities(shift)
    summary = engine.fee_backfill()
    assert counts(summary) == {"activities_read": 31, "matched": 31, "already_recorded": 0,
                               "unmatched": 0, "invalid": 0, "conflicting": 0,
                               "ambiguous": 0}
    evidence = fee_evidence(engine)
    assert Counter(body["fill_id"] for body in evidence) == Counter(
        f["fill_id"] for f in REAL["ledger_fills"]
    )  # exactly one fee for each fill
    suffix = ";" + FEE_MATCH_V2
    assert all(body["broker_source_reference"].endswith(suffix) for body in evidence)
    assert {
        short(body["broker_source_reference"].removeprefix("alpaca-activity:")
              .removesuffix(suffix)): body["fill_id"][:8]
        for body in evidence
    } == REAL_BINDING
    fills = {f["fill_id"][:8]: f for f in REAL["ledger_fills"]}
    coin_fees = {REAL_BINDING[short(raw["id"])]: raw for raw in REAL["cfee_activities"]
                 if "qty" in raw}
    total = D(0)
    for sid, prefixes, usd_fees in trades:
        own = [fills[prefix] for prefix in prefixes]
        bought = sum(D(f["qty"]) for f in own if f["side"] == "buy")
        sold = sum(D(f["qty"]) for f in own if f["side"] == "sell")
        gross = sum(D(f["qty"]) * D(f["price"]) * (1 if f["side"] == "sell" else -1)
                    for f in own)
        coin = [coin_fees[prefix] for prefix in prefixes if prefix in coin_fees]
        coin_qty = sum(-D(row["qty"]) for row in coin)
        coin_usd = sum(-D(row["qty"]) * D(row["price"]) for row in coin)
        assert coin_qty == bought - sold  # the coin fees are exactly the coins never sold
        measurement = managed_measurement(engine.repo, sid)
        assert measurement["fees_verified"] and measurement["inventory_reconciled_with_costs"]
        assert D(measurement["verified_base_asset_fee_qty"]) == coin_qty
        assert D(measurement["gross_realized_pnl"]) == gross
        assert D(measurement["verified_cash_fee_usd"]) == usd_fees
        assert D(measurement["net_pnl"]) == gross - usd_fees
        assert D(measurement["verified_total_economic_fee_usd"]) == usd_fees + coin_usd
        total += usd_fees
    assert total == D("18.49") == -sum(D(raw["net_amount"]) for raw in REAL["cfee_activities"])


def test_real_sample_reimport_records_nothing_new(mx):
    engine, venue, _ = mx
    shift = real_shift(venue.now)
    for symbol, prefixes, _usd_fees in REAL_TRADES:
        admit_real_trade(mx, symbol, prefixes, shift)
    venue.fee_activities = real_activities(shift)
    assert engine.fee_backfill()["matched"] == 31
    before = fee_evidence(engine)
    # Every fill has evidence now, so the window is anchored to the run just recorded;
    # widened, the next run reads all 31 rows again.
    again = engine.fee_backfill(lookback=timedelta(days=3))
    assert counts(again) == {"activities_read": 31, "matched": 0, "already_recorded": 31,
                             "unmatched": 0, "invalid": 0, "conflicting": 0, "ambiguous": 0}
    plain = engine.fee_backfill()  # the anchored window: the rows dated from its day on
    assert plain["already_recorded"] == plain["activities_read"]
    assert plain["matched"] == plain["unmatched"] == plain["ambiguous"] == 0
    assert fee_evidence(engine) == before


def last_run_recorded_at(engine):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT recorded_at FROM lab.managed_events WHERE kind=%s "
            "ORDER BY event_seq DESC LIMIT 1", (FEE_BACKFILL_EVENT,),
        ).fetchone()["recorded_at"]


def test_fee_backfill_reads_back_to_the_earliest_fill_without_fee_evidence(mx):
    """Production, 2026-09-29: Alpaca filters the read by activity date and the window was
    anchored to the last run, so fees dated a day before it were never read again. The
    window now starts at the earliest fill still without fee evidence, less the lookback."""
    engine, venue, _ = mx
    buy_at = real_fill("b59eccfd")["filled_at"]  # the LTC buy
    shift = venue.now - timedelta(hours=30) - buy_at
    admit_real_trade(mx, "LTC/USD", ("b59eccfd", "a7b09fe3"), shift)
    assert engine.fee_backfill()["activities_read"] == 0  # nothing posted yet; recorded
    venue.fee_activities = real_activities(shift, only={"f8ff09f3", "7b93c848"})
    anchored_day = (last_run_recorded_at(engine) - timedelta(hours=1)).astimezone(UTC).date()
    assert all(row["date"] < anchored_day.isoformat() for row in venue.fee_activities)
    summary = engine.fee_backfill()
    assert summary["window_after"] == buy_at + shift - timedelta(hours=1)
    assert (summary["activities_read"], summary["matched"]) == (2, 2)


def test_fee_backfill_window_reaches_back_at_most_seven_days(mx):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "LTC/USD"))
    now, hour = venue.now, timedelta(hours=1)

    def fill_aged(age):
        insert_fill(engine, sid, fill_id=str(uuid4()), broker_order_id="order-" + str(age),
                    side="buy", qty=D("1"), price=D("68"), filled_at=now - age)

    fill_aged(timedelta(days=10))
    # The first run's anchored window starts at the earliest fill, and it is earlier.
    assert engine.fee_backfill()["window_after"] == now - timedelta(days=10) - hour
    # A fill older than 7 days no longer moves the window.
    assert engine.fee_backfill()["window_after"] == last_run_recorded_at(engine) - hour
    fill_aged(timedelta(days=6))
    assert engine.fee_backfill()["window_after"] == now - timedelta(days=6) - hour
    fill_aged(timedelta(days=7) - timedelta(minutes=30))
    assert engine.fee_backfill()["window_after"] == now - timedelta(days=7)
