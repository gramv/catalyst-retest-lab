"""ALPACA_FEE_MATCH_V4: interchangeable ambiguous fee rows bind in time order, never a guess
(package learning-measure, 2026-10-02).

Fixture evidence only: the shape of the live SHIB exit of 2026-10-01 (22 sells of one order in
132 ms, 18 left without fee evidence because their USD fees of $0.01 and $0.02 each had several
consistent sells), synthetic ids and amounts. Disposable PostgreSQL behind a fake activities
endpoint; no network, broker, Jev or owner ledger.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from catalyst_lab.managed_analytics import (
    FEE_MATCH_V2,
    FEE_MATCH_V4,
    _time_order_matching,
    assign_fee_activities_v2,
    assign_fee_activities_v4,
    fee_correction_id,
    import_alpaca_fee_activities,
    normalize_fee_activity,
)
from catalyst_lab.managed_measurement import managed_measurement
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import packet
from tests.test_managed_fees import counts, fee_evidence, insert_fill

T0 = datetime(2026, 10, 1, 14, 5, 13, tzinfo=UTC)
PRICE = D("0.00000569")
# Notional ~ $4.0, $5.0 and $2.0: fees at 0.15%/0.25% rounded up to the cent.
QTY_A, QTY_B, QTY_C = D("700000"), D("880000"), D("351000")  # {0.01,0.01}, {0.01,0.02}, {0.01}


def usd_row(n, amount, at=T0 + timedelta(minutes=10)):
    return {"id": f"20261001000000000::usd-{n:02d}", "activity_type": "CFEE",
            "status": "executed", "currency": "USD", "net_amount": "-" + amount,
            "created_at": at.isoformat(), "date": at.date().isoformat()}


def coin_row(n, qty, price, at=T0 + timedelta(minutes=10), symbol="SHIBUSD"):
    return {"id": f"20261001000000000::coin-{n:02d}", "activity_type": "CFEE",
            "status": "executed", "currency": "USD", "net_amount": "0", "symbol": symbol,
            "qty": "-" + qty, "price": price, "created_at": at.isoformat(),
            "date": at.date().isoformat()}


def fill(n, qty, *, side="sell", setup="setup-shib", price=PRICE, ms=0, symbol="SHIB/USD"):
    return {"fill_id": f"fill-{n:02d}", "setup_id": setup, "broker_order_id": "exit",
            "side": side, "qty": qty, "price": price,
            "filled_at": T0 + timedelta(milliseconds=ms), "fee_usd": None,
            "source": "ALPACA_PAPER", "event_seq": n, "symbol": symbol, "market": "CRYPTO",
            "cost_correction_ids": []}


def run(fills, rows):
    """V2 first, then V4 on what V2 left ambiguous (the import's own order)."""
    activities = [normalize_fee_activity(r) for r in rows]
    recorded, bound, unmatched, ambiguous = assign_fee_activities_v2(fills, activities)
    used = {f["fill_id"] for _, f in bound}
    bound_v4, left = assign_fee_activities_v4(fills, ambiguous, taken=used)
    return bound, bound_v4, left


def names(pairs):
    return {a["activity_id"].split("::")[1]: f["fill_id"] for a, f in pairs}


def shib_group():
    """One $0.01 sell and two sells that could each be charged $0.01 or $0.02."""
    return [fill(1, QTY_A, ms=0), fill(2, QTY_B, ms=40), fill(3, QTY_B + 1000, ms=90)]


def test_v4_binds_a_closed_group_of_one_trades_sells_in_time_order():
    rows = [usd_row(1, "0.01"), usd_row(2, "0.02"), usd_row(3, "0.02")]
    bound, bound_v4, left = run(shib_group(), rows)
    assert bound == [] and left == []  # V2 binds nothing: every row has two candidates.
    assert names(bound_v4) == {"usd-01": "fill-01", "usd-02": "fill-02", "usd-03": "fill-03"}
    assert {a["match_policy"] for a, _ in bound_v4} == {FEE_MATCH_V4}
    assert {a["row_policy"] for a, _ in bound_v4} == {FEE_MATCH_V2}
    assert {a["group_size"] for a, _ in bound_v4} == {3}
    total = sum((a["cash_fee_usd"] for a, _ in bound_v4), D(0))
    assert total == D("0.05")  # The trade's fee is the same whichever sell takes which row.


def test_v4_uses_a_complete_assignment_when_plain_time_order_is_inconsistent():
    # The earliest row is a $0.02 one, and the earliest sell cannot be charged $0.02.
    rows = [usd_row(1, "0.02", at=T0 + timedelta(minutes=9)), usd_row(2, "0.01"),
            usd_row(3, "0.02")]
    _, bound_v4, left = run(shib_group(), rows)
    assert left == []
    assert names(bound_v4) == {"usd-01": "fill-03", "usd-02": "fill-01", "usd-03": "fill-02"}


def test_v4_never_binds_when_the_counts_differ():
    """A fourth consistent sell whose own row has not been read keeps the group open."""
    fills = [*shib_group(), fill(4, QTY_A, ms=120)]
    rows = [usd_row(1, "0.01"), usd_row(2, "0.02"), usd_row(3, "0.02")]
    bound, bound_v4, left = run(fills, rows)
    assert bound == bound_v4 == [] and len(left) == 3


def test_v4_never_binds_different_amounts_across_two_trades():
    """$0.01 and $0.02 with one sell in each of two setups, both consistent with both: the
    trades' fees would differ by the choice, so nothing binds."""
    fills = [fill(1, QTY_B, setup="setup-a"), fill(2, QTY_B, setup="setup-b", ms=50)]
    rows = [usd_row(1, "0.01"), usd_row(2, "0.02")]
    bound, bound_v4, left = run(fills, rows)
    assert bound == bound_v4 == [] and len(left) == 2


def test_v4_binds_equal_amounts_across_trades_and_sides_of_the_rows():
    """Two $0.01 rows, two sells of two setups each consistent with both: every assignment
    gives each sell $0.01, so they bind in time order."""
    fills = [fill(1, QTY_A, setup="setup-a", symbol="DOGE/USD", price=D("0.2")),
             fill(2, QTY_A, setup="setup-b", ms=50)]
    fills[0]["qty"] = D("20")  # $4 notional: {0.01, 0.01}
    rows = [usd_row(1, "0.01"), usd_row(2, "0.01", at=T0 + timedelta(minutes=11))]
    _, bound_v4, left = run(fills, rows)
    assert left == [] and names(bound_v4) == {"usd-01": "fill-01", "usd-02": "fill-02"}


def test_v4_never_binds_a_group_of_twin_rows_with_one_fill():
    fills = [fill(1, QTY_C)]
    rows = [usd_row(1, "0.01"), usd_row(2, "0.01")]
    bound, bound_v4, left = run(fills, rows)
    assert bound == bound_v4 == [] and len(left) == 2


def test_v4_needs_a_complete_assignment():
    rows = [normalize_fee_activity(usd_row(n, "0.01")) for n in (1, 2, 3)]
    f1, f2, f3 = fill(1, QTY_A), fill(2, QTY_A, ms=1), fill(3, QTY_A, ms=2)
    # Rows 1 and 2 can only take fill 1; three rows, three fills, no complete assignment.
    options = {rows[0]["activity_id"]: [f1], rows[1]["activity_id"]: [f1],
               rows[2]["activity_id"]: [f1, f2, f3]}
    assert _time_order_matching(rows, [f1, f2, f3], options) is None
    options[rows[1]["activity_id"]] = [f1, f2]
    pairs = _time_order_matching(rows, [f1, f2, f3], options)
    assert [(a["activity_id"][-6:], f["fill_id"]) for a, f in pairs] == [
        ("usd-01", "fill-01"), ("usd-02", "fill-02"), ("usd-03", "fill-03")]


def test_v4_unpriced_coin_rows_bind_only_when_the_value_cannot_depend_on_the_choice():
    """V3 rows (price "0") are valued at their fill's price: one buy order at two prices
    with different coin amounts would change the trade's fee in USD, so it stays ambiguous;
    at one price it binds."""
    buy_a = fill(1, D("1000000"), side="buy", price=D("0.00000569"))
    buy_b = fill(2, D("1000000"), side="buy", price=D("0.00000570"), ms=30)
    # 1,000,000 at 0.15% = 1500, at 0.25% = 2500: each row fits both buys.
    rows = [coin_row(1, "1500", "0"), coin_row(2, "2500", "0")]
    bound, bound_v4, left = run([buy_a, buy_b], rows)
    assert bound == bound_v4 == [] and len(left) == 2
    buy_b["price"] = buy_a["price"]
    bound, bound_v4, left = run([buy_a, buy_b], rows)
    assert left == [] and names(bound_v4) == {"coin-01": "fill-01", "coin-02": "fill-02"}
    # Priced rows (V2) carry their own value: different prices do not matter.
    buy_b["price"] = D("0.00000570")
    priced = [coin_row(1, "1500", "0.00000569"), coin_row(2, "2500", "0.00000570")]
    bound, bound_v4, left = run([buy_a, buy_b], priced)
    assert left == [] and len(bound_v4) == 2


def test_v4_leaves_v2_bindings_and_fills_with_evidence_alone():
    """A fill V2 bound in this run, or one that already carries fee evidence, is never a V4
    candidate; the remaining group then has fewer fills than rows and stays ambiguous."""
    fills = [fill(1, QTY_A), fill(2, QTY_A, ms=10)]
    fills[1]["cost_correction_ids"] = ["already"]
    rows = [usd_row(1, "0.01"), usd_row(2, "0.01")]
    bound, bound_v4, left = run(fills, rows)
    assert bound == bound_v4 == [] and len(left) == 2


def test_import_counts_an_open_group_as_ambiguous_and_writes_nothing():
    fills = [*shib_group(), fill(4, QTY_A, ms=95)]
    rows = [usd_row(1, "0.01"), usd_row(2, "0.02"), usd_row(3, "0.02")]
    result = import_alpaca_fee_activities(object(), fills, rows, recorded_at=T0)
    assert (result["ambiguous"], result["matched"]) == (3, 0)  # No write is attempted.


def test_v4_binds_the_shib_shape_through_the_engine_and_replays_idempotently(mx):  # noqa: F811
    """One buy of 0.1 (its coin fee 0.00025 at 0.25%, bound by V2) and three sells of one order
    within 90 ms totalling 0.09975: $4.00 and $0.975 can only be charged $0.01, $5.00 $0.01 or
    $0.02. The $0.02 row binds by V2; the two $0.01 rows, each consistent with two sells of the
    same trade, bind by V4 in time order. Net and official R become known."""
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "BTC/USD"))
    t0 = venue.now
    insert_fill(engine, sid, fill_id="b-1", broker_order_id="entry", side="buy",
                qty=D("0.1"), price=D("100"), filled_at=t0)
    sells = (("s-1", D("0.04")), ("s-2", D("0.05")), ("s-3", D("0.00975")))
    t1 = t0 + timedelta(hours=1)
    for n, (fill_id, qty) in enumerate(sells):
        insert_fill(engine, sid, fill_id=fill_id, broker_order_id="exit", side="sell",
                    qty=qty, price=D("100"), filled_at=t1 + timedelta(milliseconds=45 * n))
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "CLOSED", exit_reason="STOP_LIMIT_NOT_FILLED")
    posted = t1 + timedelta(minutes=10)
    venue.fee_activities = [
        coin_row(1, "0.00025", "100", at=posted, symbol="BTCUSD"),
        usd_row(1, "0.01", at=posted), usd_row(2, "0.02", at=posted),
        usd_row(3, "0.01", at=posted),
    ]
    venue.now = posted + timedelta(minutes=1)
    before = managed_measurement(engine.repo, sid)
    assert before["official_r"] is None and not before["fees_verified"]
    summary = engine.fee_backfill()
    assert counts(summary) == {"activities_read": 4, "matched": 4, "already_recorded": 0,
                               "unmatched": 0, "invalid": 0, "conflicting": 0,
                               "ambiguous": 0}
    evidence = {b["fill_id"]: b for b in fee_evidence(engine)}
    assert evidence["b-1"]["broker_source_reference"].endswith(";" + FEE_MATCH_V2)
    assert evidence["s-2"]["broker_source_reference"].endswith(";" + FEE_MATCH_V2)
    for fill_id, row in (("s-1", "usd-01"), ("s-3", "usd-03")):
        body = evidence[fill_id]
        assert body["broker_source_reference"] == (
            f"alpaca-activity:20261001000000000::{row};{FEE_MATCH_V4};row:{FEE_MATCH_V2}"
            ";interchangeable-group:2")
        assert body["correction_id"] == fee_correction_id(f"20261001000000000::{row}", fill_id)
        assert D(body["fee_usd"]) == D("0.01")
    after = managed_measurement(engine.repo, sid)
    assert after["fees_verified"] and D(after["verified_cash_fee_usd"]) == D("0.04")
    assert after["official_r"] is not None
    again = engine.fee_backfill(lookback=timedelta(days=1))
    assert (again["already_recorded"], again["matched"], again["conflicting"]) == (4, 0, 0)
    assert len(fee_evidence(engine)) == 4
