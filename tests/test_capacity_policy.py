"""JEV_MANAGED_RISK_V2 capacity (plan 2.2/2.3): caps, buckets, buying power, deferral.

Disposable PostgreSQL, fake Jev and the fake paper venue only (LAB_FIXTURE evidence).
"""

import asyncio
import copy
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.account_risk import (
    LEGACY_MANAGED_POLICY_ID,
    MANAGED_RISK_V2_POLICY_ID,
    UNLISTED_CRYPTO_THEME,
    account_risk_failure,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_classification import (
    BUCKET_CLASSIFICATION_POLICY,
    CLASSIFICATION_POLICY,
    initialize_managed_classifications,
)
from catalyst_lab.managed_execution import ManagedExecution
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_research_reports import reply

WIDE = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
TIGHT = {"entry_trigger": "100.05", "max_entry_price": "100.10", "stop": "100.00",
         "target": "100.40"}


def classify(engine, symbol, sector, theme):
    with engine.store.transaction() as conn:
        event = system_event(engine.repo, conn, "CLASSIFICATION_IMPORTED",
                             {"ticker": symbol, "source": "LAB_FIXTURE"})
        conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                     (event["seq"], symbol, sector, theme, "LAB_FIXTURE"))


def selected(mx, symbol, *, levels=WIDE, classification=None):
    """A recorded, receipt-bound Jev selection (as tests.test_managed_execution.packet), with
    explicit levels and an optional server classification (None leaves it untouched)."""
    engine, venue, reviews = mx
    now = venue.now
    raw = {
        "cycle_id": str(uuid4()),
        "item_key": ("CRYPTO:" if "/" in symbol else "US:") + symbol,
        "revision": 1,
        "symbol": symbol,
        "market": "CRYPTO" if "/" in symbol else "US_STOCKS",
        "levels": dict(levels),
        "thesis": "Synthetic new product with supported demand; engineering fixture only.",
        "disproof": "Synthetic product withdrawal.",
        "sources": [{
            "source_id": "fixture-release",
            "url": "https://example.org/fixture",
            "excerpt": "Synthetic verified release for test only.",
            "content_hash": digest("Synthetic verified release for test only."),
            "retrieved_at": now.isoformat(),
        }],
        "expires_at": (now + timedelta(hours=3)).isoformat(),
        "review_valid_until": (now + timedelta(seconds=60)).isoformat(),
    }
    state = {k: raw[k] for k in ("market", "symbol", "levels", "thesis", "disproof", "sources")}
    raw["state"] = copy.deepcopy(state)
    raw["evidence_hash"] = digest(encoded(state))
    with engine.store.transaction() as conn:
        engine.store.event(conn, "RESEARCH_PACKET", copy.deepcopy(raw))
    reviewer = JevReviewer(
        reviews,
        ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply())),
        key_provider=lambda: FIXTURE_KEY,
    )
    result = asyncio.run(reviewer.jev_review(
        request_id=uuid4(),
        identity={"cycle_id": raw["cycle_id"], "candidate_revision": 1,
                  "research_item_key": raw["item_key"], "evidence_hash": raw["evidence_hash"]},
        state=state, question_set=SKEPTIC, expires_at=now + timedelta(seconds=10),
        purpose="ENGINEERING_TEST",
    ))
    raw["receipt_id"] = result.receipt_ids[0]
    with engine.store.transaction() as conn:
        event = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": copy.deepcopy(raw)})
    raw["selection_event_seq"] = event["event_seq"]
    if classification is not None:
        classify(engine, symbol, *classification)
    return raw


def quote(mx, *, levels=WIDE):
    trigger = D(levels["entry_trigger"])
    return observation(mx, trade_price=str(trigger - D("0.01")),
                       bid=str(trigger - D("0.02")), ask=str(trigger))


@pytest.fixture
def v2(mx):
    engine, venue, reviews = mx
    managed = ManagedExecution(
        engine.repo, engine.broker, policy=engine.policy, clock=engine.now,
        review_store=reviews, risk_policy_id=MANAGED_RISK_V2_POLICY_ID,
    )
    assert managed.reconcile()["clean"]
    return managed, venue, reviews


def events(engine, kind, setup_id=None):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind=%s
            AND (%s::uuid IS NULL OR setup_id=%s) ORDER BY event_seq""",
            (kind, setup_id, setup_id),
        ).fetchall()
    return [row["body"] for row in rows]


def free(engine, venue, setup_id):
    """The broker cancels an unfilled entry; the next protection pass closes and releases."""
    for order in venue.orders.values():
        if order["client_order_id"] == "cl-managed-" + str(setup_id).replace("-", ""):
            for item in (order, *order.get("legs", [])):
                item["status"] = "canceled"
    engine.manage(setup_id, observation((engine, venue, None)))
    assert engine._load(setup_id)[1]["state"] == "CLOSED"


def test_capacity_rejection_is_deferred_once_per_window_and_keeps_the_attempt(v2):
    engine, venue, _ = v2
    first = engine.admit(selected(v2, "CAPA", classification=("TECH", "SAME_THEME")))
    second = engine.admit(selected(v2, "CAPB", classification=("TECH", "SAME_THEME")))
    assert engine.observe_trigger(first, quote(v2))["outcome"] == "APPROVED"
    decision = engine.observe_trigger(second, quote(v2))
    assert decision["outcome"] == "REJECTED" and decision["reason"] == "CORRELATION_LIMIT"
    assert decision["context"]["binding_constraint"] == "CORRELATION"
    state = engine._load(second)[1]
    until = venue.now + timedelta(seconds=60)
    assert state["state"] == "WATCHING" and state["capacity_deferred_reason"] == "CORRELATION_LIMIT"
    assert datetime.fromisoformat(state["capacity_deferred_until"]) == until
    [deferred] = events(engine, "RISK_CAPACITY_DEFERRED", second)
    assert deferred["reason"] == "CORRELATION_LIMIT" and deferred["cooldown_seconds"] == 60
    assert deferred["decision_id"] == str(decision["decision_id"])
    # Inside the cooldown the trigger path skips the setup before any account or asset read.
    venue.now += timedelta(seconds=30)
    reads = [c for c in venue.calls if c[1] in {"/v2/account"} or c[1].startswith("/v2/assets")]
    assert engine.observe_trigger(second, quote(v2)) is None
    assert [c for c in venue.calls
            if c[1] in {"/v2/account"} or c[1].startswith("/v2/assets")] == reads
    assert len(events(engine, "RISK_CAPACITY_DEFERRED", second)) == 1
    # The ticker/day attempt still belongs to the live setup: no second admission today.
    with pytest.raises(ValueError, match="^TICKER_ALREADY_ATTEMPTED$"):
        engine.admit(selected(v2, "CAPB"))
    free(engine, venue, first)
    venue.now = until + timedelta(seconds=1)
    assert engine.reconcile()["clean"]  # Entry evidence needs a reconciliation within 60 s.
    decision = engine.observe_trigger(second, quote(v2))
    assert decision["outcome"] == "APPROVED"
    state = engine._load(second)[1]
    assert state["state"] == "ORDER_SUBMITTED" and state["capacity_deferred_until"] is None
    assert len([o for o in venue.orders_of("buy") if o["symbol"] == "CAPB"]) == 1
    assert verify_events(engine.repo.export_events())["valid"]


def test_legacy_policy_keeps_capacity_rejections_terminal(mx):
    engine, venue, _ = mx
    first = engine.admit(selected(mx, "LEGA", classification=("TECH", "SAME_THEME")))
    second = engine.admit(selected(mx, "LEGB", classification=("TECH", "SAME_THEME")))
    assert engine.observe_trigger(first, quote(mx))["outcome"] == "APPROVED"
    decision = engine.observe_trigger(second, quote(mx))
    assert decision["reason"] == "CORRELATION_LIMIT"
    assert decision["context"]["risk_policy_id"] == LEGACY_MANAGED_POLICY_ID
    assert engine._load(second)[1]["state"] == "RISK_REJECTED"
    assert not events(engine, "RISK_CAPACITY_DEFERRED")


def test_decimal_stacking_market_caps_and_the_five_percent_account_cap(v2):
    """0.5% budgets at equity 10,000.37 stack exactly to the 3% US, 2% crypto and 5% caps."""
    engine, venue, _ = v2
    venue.equity = "10000.37"
    budget = D("10000.37") * D("0.005")
    assert budget == D("50.00185")
    outcomes = []

    def enter(symbol):
        sid = engine.admit(selected(v2, symbol, classification=("S_" + symbol, "T_" + symbol)))
        market = "CRYPTO" if "/" in symbol else "US_STOCKS"
        with engine.repo.connect() as conn:
            expected = account_risk_failure(conn, MANAGED_RISK_V2_POLICY_ID, market,
                                            "S_" + symbol, "T_" + symbol, D("10000.37"), budget)
        decision = engine.observe_trigger(sid, quote(v2))
        # SQL/Python parity: the engine records exactly the database function's answer.
        assert decision["reason"] == (expected or "RISK_APPROVED"), (symbol, decision["reason"])
        outcomes.append(decision["reason"])
        return decision

    for index in range(6):
        enter(f"USS{index}")
    assert enter("USS6")["reason"] == "MARKET_RISK_CAP"  # 7 x 0.5% > 3% for US stocks.
    for index in range(4):
        enter(f"CRY{index}/USD")
    assert enter("CRY4/USD")["reason"] == "MAX_OPEN_PLANNED_RISK"  # Ten already hold 5%.
    assert outcomes.count("RISK_APPROVED") == 10
    with engine.repo.connect() as conn:
        rows = conn.execute(
            """SELECT market,sum(budget) AS total FROM lab.account_risk_reservations
            GROUP BY market ORDER BY market"""
        ).fetchall()
    totals = {row["market"]: row["total"] for row in rows}
    assert totals == {"CRYPTO": D("200.00740"), "US_STOCKS": D("300.01110")}
    assert sum(totals.values()) == D("0.05") * D("10000.37")


def test_day_position_stock_uses_intraday_buying_power_but_is_rejected_not_resized(v2):
    engine, venue, _ = v2
    venue.multiplier, venue.buying_power = "2", "15000"
    sid = engine.admit(selected(v2, "LEV", levels=TIGHT, classification=("S_LEV", "T_LEV")))
    # Risk alone would buy 500 shares ($50 / $0.10); the 2x day-position limit sizes 199
    # ($19,919.90). Broker buying power of $15,000 rejects that size; it never shrinks it.
    decision = engine.observe_trigger(sid, quote(v2, levels=TIGHT))
    assert decision["reason"] == "INSUFFICIENT_BUYING_POWER" and decision["payload"] == {}
    assert decision["context"]["buying_power"]["allowed"] == "15000"
    assert decision["context"]["binding_constraint"] == "BUYING_POWER"
    state = engine._load(sid)[1]
    assert state["state"] == "WATCHING" and not venue.orders_of("buy")
    venue.buying_power = "20000"
    venue.now = datetime.fromisoformat(state["capacity_deferred_until"]) + timedelta(seconds=1)
    assert engine.reconcile()["clean"]
    decision = engine.observe_trigger(sid, quote(v2, levels=TIGHT))
    assert decision["outcome"] == "APPROVED" and decision["payload"]["qty"] == "199"
    context = decision["context"]
    assert context["day_position"] is True and context["buying_power_multiple"] == "2"
    assert context["binding_constraint"] == "CAPITAL"
    assert context["buying_power"]["allowed"] == "20000"
    assert context["account_margin"]["multiplier"] == "2"
    with engine.repo.connect() as conn:
        reservation = conn.execute("SELECT * FROM lab.managed_active_reservations").fetchone()
    assert reservation["qty"] * reservation["max_entry"] == D("19919.90")
    assert reservation["budget"] == D("50.000")
    assert [o["qty"] for o in venue.orders_of("buy")] == ["199"]


def test_legacy_policy_never_uses_margin_for_the_same_stock(mx):
    engine, venue, _ = mx
    venue.multiplier, venue.buying_power = "2", "20000"
    sid = engine.admit(selected(mx, "NOLEV", levels=TIGHT, classification=("S_N", "T_N")))
    decision = engine.observe_trigger(sid, quote(mx, levels=TIGHT))
    assert decision["outcome"] == "APPROVED" and decision["payload"]["qty"] == "99"
    assert decision["context"]["buying_power_multiple"] == "1"


def test_crypto_stays_cash_only_with_the_broker_non_marginable_figure(v2):
    engine, venue, _ = v2
    venue.multiplier, venue.buying_power = "2", "20000"
    venue.non_marginable_buying_power = "500"
    sid = engine.admit(selected(v2, "CASH/USD", levels=TIGHT, classification=("CRYPTO", "L1")))
    decision = engine.observe_trigger(sid, quote(v2, levels=TIGHT))
    assert decision["outcome"] == "APPROVED"
    assert D(decision["payload"]["qty"]) * D(decision["payload"]["limit_price"]) <= D(500)
    context = decision["context"]
    assert context["buying_power_multiple"] == "1" and context["binding_constraint"] == "CAPITAL"
    assert context["buying_power"]["source"] == "non_marginable_buying_power"
    # No cash left for crypto: a capacity deferral, not a terminal zero-size rejection.
    venue.non_marginable_buying_power = "0"
    other = engine.admit(selected(v2, "EMPTY/USD", levels=TIGHT,
                                  classification=("CRYPTO", "L2")))
    decision = engine.observe_trigger(other, quote(v2, levels=TIGHT))
    assert decision["reason"] == "INSUFFICIENT_BUYING_POWER"
    assert engine._load(other)[1]["state"] == "WATCHING"
    # Crypto trading switched off at the broker is terminal.
    venue.non_marginable_buying_power, venue.crypto_status = "10000", "INACTIVE"
    third = engine.admit(selected(v2, "OFF/USD", classification=("CRYPTO", "L3")))
    decision = engine.observe_trigger(third, quote(v2))
    assert decision["reason"] == "CRYPTO_ACCOUNT_NOT_ACTIVE"
    assert engine._load(third)[1]["state"] == "RISK_REJECTED"


def test_owner_buckets_limit_one_crypto_per_bucket_and_refuse_unlisted(v2):
    engine, venue, _ = v2
    # An earlier V1-policy import put every crypto symbol in one shared theme.
    initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=["AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD"],
    )
    buckets = {"buckets": {"L1": ["AAA/USD", "BBB/USD"], "DEFI": ["CCC/USD"]},
               "unlisted": "REFUSE"}
    result = initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=BUCKET_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=[], crypto_buckets=buckets,
    )
    assert result["inserted"] == 4 and result["unlisted_rule"] == "REFUSE"
    with engine.repo.connect() as conn:
        current = {r["ticker"]: (r["sector"], r["theme"]) for r in conn.execute(
            "SELECT ticker,sector,theme FROM lab.current_classifications"
        ).fetchall()}
    assert current == {"AAA/USD": ("CRYPTO", "L1"), "BBB/USD": ("CRYPTO", "L1"),
                       "CCC/USD": ("CRYPTO", "DEFI"),
                       "DDD/USD": ("CRYPTO", UNLISTED_CRYPTO_THEME)}
    again = initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=BUCKET_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=[], crypto_buckets=buckets,
    )
    assert again["inserted"] == 0  # Identical imports are idempotent.
    with pytest.raises(ValueError, match="EXISTING_CLASSIFICATION_CONFLICT"):
        initialize_managed_classifications(
            engine.repo, engine.broker, policy_id=CLASSIFICATION_POLICY, us_mapping=(),
            crypto_symbols=["AAA/USD"],
        )
    for symbol in ("DDD/USD", "EEE/USD"):  # Refused by the marker, or never classified.
        with pytest.raises(ValueError, match="^CORRELATION_UNKNOWN$"):
            engine.admit(selected(v2, symbol))
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.managed_setups").fetchone()
    a, b, c = (engine.admit(selected(v2, s)) for s in ("AAA/USD", "BBB/USD", "CCC/USD"))
    assert engine.observe_trigger(a, quote(v2))["outcome"] == "APPROVED"
    assert engine.observe_trigger(b, quote(v2))["reason"] == "CORRELATION_LIMIT"  # Same bucket.
    assert engine.observe_trigger(c, quote(v2))["outcome"] == "APPROVED"
    # CRYPTO_OTHER is only ever the owner's explicit choice.
    initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=BUCKET_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=["EEE/USD"],
        crypto_buckets={"buckets": buckets["buckets"], "unlisted": "CRYPTO_OTHER"},
    )
    with engine.repo.connect() as conn:
        rows = {r["ticker"]: r["theme"] for r in conn.execute(
            "SELECT ticker,theme FROM lab.current_classifications WHERE ticker=ANY(%s)",
            (["DDD/USD", "EEE/USD"],),
        ).fetchall()}
    assert rows == {"DDD/USD": "CRYPTO_OTHER", "EEE/USD": "CRYPTO_OTHER"}
    with pytest.raises(ValueError, match="CRYPTO_BUCKET_CONFIGURATION_INVALID"):
        initialize_managed_classifications(
            engine.repo, engine.broker, policy_id=BUCKET_CLASSIFICATION_POLICY, us_mapping=(),
            crypto_symbols=[],
            crypto_buckets={"buckets": {"L1": ["AAA/USD"], "L2": ["AAA/USD"]},
                            "unlisted": "REFUSE"},
        )


def test_stock_admission_without_a_classification_is_refused_before_the_attempt(v2):
    engine, _, _ = v2
    with pytest.raises(ValueError, match="^CORRELATION_UNKNOWN$"):
        engine.admit(selected(v2, "NOCLASS"))
    classify(engine, "NOCLASS", "S_X", "T_X")
    sid = engine.admit(selected(v2, "NOCLASS"))  # The refusal spent nothing.
    assert engine._load(sid)[1]["state"] == "WATCHING"
