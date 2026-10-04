"""Package crypto-size-hold (plan phase 4b): JEV_MANAGED_RISK_V3 equity-slice sizing and
limits (migration 022), CRYPTO_24H_HOLD_V1, the ALPACA_CRYPTO_SECTORS_V1 classification and the
top-K decline of a coin that already has an open trade.

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue, a
scripted mock Jev transport and fixture universes. LAB_FIXTURE policy rows are inserted as the
database owner, the way an owner step would. No broker, provider, network or owner-ledger
contact; every broker mutation still passes its exact one-use five-second authorization.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.account_risk import (
    FROZEN_V1_POLICY_ID,
    LEGACY_MANAGED_POLICY_ID,
    MANAGED_RISK_V2_POLICY_ID,
    MANAGED_RISK_V3_POLICY_ID,
    MarketTerms,
    account_risk_failure,
    load_policy,
    slice_size,
    slice_sizing_failure,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.crypto_holding import (
    CRYPTO_24H_HOLD,
    HOLD_EXIT_REASON,
    CryptoHoldPolicy,
    admission_policy,
    recorded_hold_policy,
    time_exit_reason,
)
from catalyst_lab.managed_execution import ManagedExecution
from catalyst_lab.market import NY
from catalyst_lab.repository import Repository
from catalyst_lab.risk import RiskEngine, RiskPolicy
from tests.test_account_risk_policy import Ledger, add_policy, owner
from tests.test_capacity_policy import events, selected
from tests.test_cross_engine_attempts import legacy_candidate
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_system_check import market as market

EQUITY = D("10000")
MIGRATIONS = Path(localdb.__file__).with_name("migrations")
V3 = MANAGED_RISK_V3_POLICY_ID
# Levels on Alpaca's 0.01 grid; entry = max entry, so a print at the entry with the ask at the
# max entry and a one-basis-point spread triggers under every crypto trigger version.
TWO = {"entry_trigger": "100", "max_entry_price": "100", "stop": "98", "target": "106"}
SIX = {"entry_trigger": "100", "max_entry_price": "100", "stop": "94", "target": "114"}
SHORT = {"entry_trigger": "100", "max_entry_price": "100", "stop": "98.5", "target": "104"}


@pytest.fixture
def v3(mx):
    """The managed engine admitting under JEV_MANAGED_RISK_V3 (the deploy example's default)."""
    engine, venue, reviews = mx
    managed = ManagedExecution(
        engine.repo, engine.broker, policy=engine.policy, clock=engine.now,
        review_store=reviews, risk_policy_id=V3,
    )
    assert managed.reconcile()["clean"]
    return managed, venue, reviews


def touch(mx, levels):
    """A fresh print at the entry trigger with the ask at the max entry, one basis point wide."""
    ask = D(levels["max_entry_price"])
    return observation(mx, trade_price=levels["entry_trigger"], bid=str(ask - D("0.01")),
                       ask=str(ask))


def reservation(engine, setup_id):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT * FROM lab.managed_active_reservations WHERE setup_id=%s", (setup_id,)
        ).fetchone()


def enter(mx, symbol, levels, classification=("CRYPTO", None)):
    """Admit a receipt-bound selection of ``symbol`` and fire its trigger once."""
    engine = mx[0]
    sector, theme = classification
    sid = engine.admit(selected(mx, symbol, levels=levels,
                                classification=(sector, theme or "T_" + symbol)))
    return sid, engine.observe_trigger(sid, touch(mx, levels))


# --- The V3 policy row, its crypto terms and their guard --------------------------------------


def test_v3_row_and_crypto_terms_carry_the_owner_numbers_and_v1_v2_are_untouched(er):
    with er.connect() as conn:
        v3_policy = load_policy(conn, V3, engine="MANAGED")
        v2 = load_policy(conn, MANAGED_RISK_V2_POLICY_ID, engine="MANAGED")
        v1 = load_policy(conn, FROZEN_V1_POLICY_ID, engine="FROZEN_V1")
        legacy = load_policy(conn, LEGACY_MANAGED_POLICY_ID, engine="MANAGED")
    # Per trade 0.5% planned risk, 5% open planned risk; crypto 5%, US 3% (V2's), forex 0.
    assert (v3_policy.risk_pct, v3_policy.account_cap_pct) == (D("0.005"), D("0.05"))
    assert v3_policy.market_caps == {"US_STOCKS": D("0.03"), "CRYPTO": D("0.05"), "FOREX": D(0)}
    # V2's US numbers, cooldown and arm are kept.
    assert (v3_policy.max_per_sector, v3_policy.max_per_theme) == (2, 1)
    assert v3_policy.sector_limited_markets == ("US_STOCKS",)
    assert v3_policy.leverage_allowed and v3_policy.intraday_buying_power_multiple == 2
    assert v3_policy.capacity_cooldown_seconds == 60 and v3_policy.fixed_exit_arm_pct == 30
    assert v3_policy.owner_ruling_ref == (
        "CRYPTO-AGENT-LOOP 2026-09-26 D2; crypto-size-hold 2026-09-27")
    [terms] = v3_policy.market_terms.values()
    assert terms == MarketTerms(
        V3, "CRYPTO", "EQUITY_SLICE_RISK_CAPPED_V1", D("0.10"), 3, D("0.02"),
        "CRYPTO-AGENT-LOOP 2026-09-26 D2 and 4.4; crypto-size-hold 2026-09-27")
    assert v3_policy.terms("US_STOCKS") is None and v3_policy.terms("CRYPTO") is terms
    assert (v3_policy.theme_limit("CRYPTO"), v3_policy.theme_limit("US_STOCKS")) == (3, 1)
    assert v3_policy.evidence()["market_terms"]["CRYPTO"]["notional_pct"] == D("0.10")
    # Every earlier row keeps its numbers, its evidence shape and no terms.
    for policy in (v1, v2, legacy):
        assert policy.market_terms == {} and "market_terms" not in policy.evidence()
    assert (v2.risk_pct, v2.account_cap_pct, v2.market_caps["CRYPTO"]) == (
        D("0.005"), D("0.05"), D("0.02"))
    with owner(er).connect() as conn:
        audited = conn.execute(
            """SELECT count(*) FILTER (WHERE lab.research_audit_matches('ACCOUNT_RISK_POLICIES',
            to_jsonb(p),p.event_seq)) AS policies,(SELECT count(*) FILTER (WHERE
            lab.research_audit_matches('ACCOUNT_RISK_MARKET_TERMS',to_jsonb(t),t.event_seq))
            FROM lab.account_risk_market_terms t) AS terms FROM lab.account_risk_policies p"""
        ).fetchone()
    # Migration 026 adds JEV_MANAGED_RISK_V4 and its terms (tests/test_risk_v4.py).
    assert (audited["policies"], audited["terms"]) == (6, 3)  # V4 (026) and V5 (031).
    assert verify_events(er.export_events())["valid"]


def test_market_terms_are_written_only_with_their_new_managed_policy_row(er):
    terms_sql = """INSERT INTO lab.account_risk_market_terms(policy_id,market,sizing_method,
        notional_pct,max_per_theme,min_stop_fraction,owner_ruling_ref)
        VALUES(%s,%s,'EQUITY_SLICE_RISK_CAPPED_V1',0.10,3,0.02,'LAB_FIXTURE terms')"""
    # No existing policy (V1, V2, the legacy row, V3 itself) can gain terms afterwards.
    for policy_id in (FROZEN_V1_POLICY_ID, LEGACY_MANAGED_POLICY_ID, MANAGED_RISK_V2_POLICY_ID,
                      V3):
        with owner(er).connect() as conn, pytest.raises(
            psycopg.errors.RaiseException, match="MARKET_TERMS_REQUIRE_NEW_MANAGED_POLICY"
        ):
            conn.execute(terms_sql, (policy_id, "CRYPTO"))
    # A new MANAGED row may carry them, in its own transaction only.
    add_policy(er, "LAB_FIXTURE_LATE_TERMS")
    with owner(er).connect() as conn, pytest.raises(
        psycopg.errors.RaiseException, match="MARKET_TERMS_REQUIRE_NEW_MANAGED_POLICY"
    ):
        conn.execute(terms_sql, ("LAB_FIXTURE_LATE_TERMS", "CRYPTO"))
    with owner(er).connect() as conn:
        conn.execute(
            """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
            market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
            intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
            owner_ruling_ref) VALUES('LAB_FIXTURE_WITH_TERMS','MANAGED',0.005,0.05,
            '{"CRYPTO":0.05}',2,1,'{US_STOCKS}',false,1,60,30,'LAB_FIXTURE policy')""")
        conn.execute(terms_sql, ("LAB_FIXTURE_WITH_TERMS", "CRYPTO"))
    # A frozen-engine row never has terms; only crypto is sliced; rows are immutable.
    with owner(er).connect() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(
            """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
            market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
            intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
            owner_ruling_ref) VALUES('LAB_FIXTURE_FROZEN_COPY','FROZEN_V1',0.01,0.02,
            '{"US_STOCKS":0.02}',1,1,'{US_STOCKS}',false,1,NULL,0,'LAB_FIXTURE')""")
        conn.execute(terms_sql, ("LAB_FIXTURE_FROZEN_COPY", "CRYPTO"))
    with owner(er).connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
            market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
            intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
            owner_ruling_ref) VALUES('LAB_FIXTURE_US_TERMS','MANAGED',0.005,0.05,
            '{"US_STOCKS":0.03}',2,1,'{US_STOCKS}',false,1,60,30,'LAB_FIXTURE policy')""")
        conn.execute(terms_sql, ("LAB_FIXTURE_US_TERMS", "US_STOCKS"))
    for statement in ("UPDATE lab.account_risk_market_terms SET notional_pct=0.2",
                      "DELETE FROM lab.account_risk_market_terms"):
        with owner(er).connect() as conn, pytest.raises(psycopg.errors.RaiseException):
            conn.execute(statement)
    risk = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    for repo in (er, risk):  # No application role writes policy rows or terms.
        with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(terms_sql, ("LAB_FIXTURE_WITH_TERMS", "CRYPTO"))
    with er.connect() as conn:
        assert {r["policy_id"] for r in conn.execute(
            "SELECT policy_id FROM lab.account_risk_market_terms").fetchall()} == {
            V3, "JEV_MANAGED_RISK_V4", "JEV_MANAGED_RISK_V5",
            "LAB_FIXTURE_WITH_TERMS"}  # V4: migration 026; V5: 031.
    assert verify_events(er.export_events())["valid"]


# --- Sizing: the Python mirror and its parity with the SQL check ------------------------------


def terms_of(er):
    with er.connect() as conn:
        policy = load_policy(conn, V3, engine="MANAGED")
    return policy, policy.terms("CRYPTO")


@pytest.mark.parametrize("stop,qty,notional,risk,binding", [
    ("98", "10", "1000", "20", "NOTIONAL"),                 # 2% stop: the $1,000 slice binds.
    ("97.5", "10", "1000", "25", "NOTIONAL"),
    ("95", "10", "1000", "50", "NOTIONAL"),                 # 5%: slice and risk bind together.
    ("94", "8.3333", "833.33", "49.9998", "RISK"),          # 6%: the $50 risk cap binds.
    ("90", "5", "500", "50", "RISK"),
])
def test_slice_size_takes_a_ten_percent_slice_capped_at_half_a_percent_risk(
        er, stop, qty, notional, risk, binding):
    policy, terms = terms_of(er)
    sizing = slice_size(policy, terms, equity=EQUITY, max_entry=D("100"), stop=D(stop),
                        available=D("10000"), increment=D("0.0001"))
    assert (sizing.qty, sizing.notional, sizing.planned_risk, sizing.binding) == (
        D(qty), D(notional), D(risk), binding)
    assert sizing.notional <= D(1000) and sizing.planned_risk <= D(50)
    assert (sizing.notional_cap, sizing.risk_cap) == (D(1000), D(50))


def test_slice_size_cash_limits_rounds_down_and_refuses_a_short_stop(er):
    policy, terms = terms_of(er)

    def size(**overrides):
        values = {"equity": EQUITY, "max_entry": D("100"), "stop": D("98"),
                  "available": D("10000"), "increment": D("0.0001"), **overrides}
        return slice_size(policy, terms, **values)

    cash = size(available=D("512.3456"))  # Cash-limited below the $1,000 slice.
    assert (cash.qty, cash.notional, cash.binding, cash.policy_qty) == (
        D("5.1234"), D("512.34"), "CAPITAL", D("10"))
    empty = size(available=D("-5"))  # Negative cash is none: capital-limited, not zero-size.
    assert (empty.qty, empty.policy_qty, empty.binding) == (D(0), D(10), "CAPITAL")
    whole = size(max_entry=D("300"), stop=D("294"), increment=D("1"))
    assert (whole.qty, whole.notional, whole.planned_risk) == (D(3), D(900), D(18))
    cents = size(stop=D("94"), increment=D("0.01"))
    assert (cents.qty, cents.notional, cents.planned_risk) == (D("8.33"), D("833"), D("49.98"))
    tiny = size(max_entry=D("0.00002345"), stop=D("0.00002298"), increment=D("1"))
    assert tiny.qty == D(42643923) and tiny.notional <= D(1000)  # A meme-coin price, whole units.
    too_big = size(max_entry=D("1500"), stop=D("1470"), increment=D("1"))
    assert (too_big.qty, too_big.policy_qty) == (D(0), D(0))  # One coin exceeds the slice.
    assert size(stop=D("98")).qty == D(10)  # Exactly 2% passes.
    with pytest.raises(ValueError, match="^STOP_DISTANCE_BELOW_MINIMUM$"):
        size(stop=D("98.01"))
    for bad in ({"equity": D(0)}, {"stop": D("100")}, {"increment": D("-1")},
                {"available": D("NaN")}, {"max_entry": 100}):
        with pytest.raises(ValueError, match="^INVALID_SLICE_SIZING_INPUT$"):
            size(**bad)


def test_python_sizing_is_exactly_the_largest_size_the_sql_check_accepts(er):
    """Parity (one SQL source of truth, as migration 016): lab.slice_sizing_failure accepts
    every size slice_size returns and its policy maximum (the size without the cash cap), and
    refuses one increment more than that maximum; a short stop is refused by both."""
    policy, terms = terms_of(er)
    risk = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    cases = checks = 0
    with risk.connect() as conn:
        for equity in (D("10000"), D("10000.37"), D("9876.54321"), D("250")):
            for price in (D("100"), D("64000.5"), D("1.2345"), D("0.00002345")):
                for fraction in (D("0.02"), D("0.0275"), D("0.05"), D("0.06"), D("0.25")):
                    stop = price * (1 - fraction)
                    for increment in (D("0.0001"), D("1"), D("0.00000001")):
                        for cash in (equity, equity / 7):
                            sizing = slice_size(policy, terms, equity=equity, max_entry=price,
                                                stop=stop, available=cash, increment=increment)
                            cases += 1
                            assert sizing.qty <= sizing.policy_qty
                            assert sizing.qty * price <= max(cash, D(0))
                            for accepted in {sizing.qty, sizing.policy_qty} - {D(0)}:
                                assert slice_sizing_failure(conn, V3, "CRYPTO", equity,
                                                            accepted, price, stop) is None
                                checks += 1
                            more = sizing.policy_qty + increment
                            expected = ("SLICE_NOTIONAL_EXCEEDED"
                                        if more * price > terms.notional_pct * equity
                                        else "SLICE_RISK_EXCEEDED")
                            assert slice_sizing_failure(conn, V3, "CRYPTO", equity, more,
                                                        price, stop) == expected
                            checks += 1
        assert slice_sizing_failure(conn, V3, "CRYPTO", EQUITY, D(1), D(100),
                                    D("98.01")) == "STOP_DISTANCE_BELOW_MINIMUM"
        assert slice_sizing_failure(conn, V3, "CRYPTO", EQUITY, D(10), D(100), D(98)) is None
        assert slice_sizing_failure(conn, V3, "US_STOCKS", EQUITY, D(1), D(100),
                                    D(90)) == "SLICE_TERMS_UNKNOWN"
        assert slice_sizing_failure(conn, MANAGED_RISK_V2_POLICY_ID, "CRYPTO", EQUITY, D(1),
                                    D(100), D(90)) == "SLICE_TERMS_UNKNOWN"
        assert slice_sizing_failure(conn, V3, "CRYPTO", EQUITY, D(0), D(100),
                                    D(90)) == "INVALID_SLICE_SIZING_INPUT"
    print(f"\nSLICE SIZING PARITY: {cases} cases, {checks} SQL checks")
    assert cases == 480 and checks > cases


# --- The engine: a V3 crypto entry is sized, checked, reserved and authorized -----------------


def test_a_two_percent_stop_enters_a_one_thousand_dollar_slice_risking_twenty(v3):
    engine, venue, _ = v3
    sid, decision = enter(v3, "TWO/USD", TWO)
    assert decision["outcome"] == "APPROVED" and decision["reason"] == "RISK_APPROVED"
    payload, context = decision["payload"], decision["context"]
    assert (D(payload["qty"]), D(payload["limit_price"]), payload["type"]) == (
        D(10), D(100), "limit")
    assert context["risk_policy_id"] == V3 and D(context["budget"]) == D(20)
    assert context["binding_constraint"] == "NOTIONAL"
    sizing = {k: v for k, v in context["sizing"].items() if k not in {"method", "binding"}}
    assert context["sizing"]["method"] == "EQUITY_SLICE_RISK_CAPPED_V1"
    assert {k: D(v) for k, v in sizing.items()} == {
        "equity": D(10000), "notional_pct": D("0.10"), "risk_pct": D("0.005"),
        "min_stop_fraction": D("0.02"), "notional_cap": D(1000), "risk_cap": D(50),
        "available_cash": D(10000), "increment": D("0.0001"), "slice_qty": D(10),
        "risk_qty": D(25), "cash_qty": D(100), "qty": D(10), "notional": D(1000),
        "planned_risk": D(20), "unfilled_reserved_notional": D(0),
        "min_order_size": D("0.0001")}
    row = reservation(engine, sid)
    assert (row["budget"], row["planned_risk"], row["qty"]) == (D(20), D(20), D(10))
    assert row["qty"] * row["max_entry"] == D(1000)
    # One limit buy at the max entry, sent under its own one-use authorization.
    [order] = venue.orders_of("buy")
    assert (order["symbol"], D(order["qty"]), D(order["limit_price"])) == (
        "TWO/USD", D(10), D(100))
    with engine.repo.connect() as conn:
        claims = conn.execute("SELECT count(*) AS n FROM lab.managed_claims WHERE decision_id=%s",
                              (decision["decision_id"],)).fetchone()
    assert claims["n"] == 1 and engine._load(sid)[1]["state"] == "ORDER_SUBMITTED"
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_six_percent_stop_is_capped_at_fifty_dollars_of_risk(v3):
    engine, _, _ = v3
    sid, decision = enter(v3, "SIX/USD", SIX)
    assert decision["outcome"] == "APPROVED"
    assert D(decision["payload"]["qty"]) == D("8.3333")
    assert decision["context"]["binding_constraint"] == "RISK"
    sizing = decision["context"]["sizing"]
    assert (D(sizing["notional"]), D(sizing["planned_risk"])) == (D("833.33"), D("49.9998"))
    row = reservation(engine, sid)
    assert row["budget"] == row["planned_risk"] == D("49.9998")
    assert row["planned_risk"] <= D(50) and row["qty"] * row["max_entry"] <= D(1000)


def test_crypto_cash_caps_the_slice_and_bought_trades_are_not_counted_twice(v3):
    engine, venue, _ = v3
    # Cash-limited below the $1,000 slice: the size shrinks to the cash (a capital binding).
    venue.cash = venue.non_marginable_buying_power = "512.3456"
    _, small = enter(v3, "CSH0/USD", TWO)
    assert small["outcome"] == "APPROVED" and small["context"]["binding_constraint"] == "CAPITAL"
    assert (D(small["payload"]["qty"]), D(small["context"]["sizing"]["notional"])) == (
        D("5.1234"), D("512.34"))
    # $2,000 of cash: the first $1,000 slice is bought, and the broker's cash falls to $1,000.
    venue.cash = venue.non_marginable_buying_power = "2000"
    _, first = enter(v3, "CSHA/USD", TWO)
    order = next(o for o in venue.orders_of("buy") if o["symbol"] == "CSHA/USD")
    engine.ingest(venue.fill(order["id"], order["qty"], price="100"))
    unfilled_small = D("512.34")  # CSH0's entry is still working: its cash is still promised.
    venue.cash, venue.non_marginable_buying_power = str(D(1000) + unfilled_small), "1000"
    _, second = enter(v3, "CSHB/USD", TWO)
    assert second["outcome"] == "APPROVED" and D(second["payload"]["qty"]) == D(10)
    assert D(second["context"]["sizing"]["unfilled_reserved_notional"]) == unfilled_small
    # The bought trade is not subtracted again (cash already fell); CSHB's working entry is,
    # even before the broker nets it from its buying power.
    sid, third = enter(v3, "CSHC/USD", TWO)
    assert third["reason"] == "INSUFFICIENT_BUYING_POWER" and third["payload"] == {}
    assert D(third["context"]["sizing"]["available_cash"]) == D(0)
    state = engine._load(sid)[1]
    assert state["state"] == "WATCHING" and state["capacity_deferred_reason"] == (
        "INSUFFICIENT_BUYING_POWER")  # A 60-second capacity deferral, not a rejection.
    assert len(events(engine, "RISK_CAPACITY_DEFERRED", sid)) == 1


def test_the_quantity_rounds_down_to_each_coins_increment(v3):
    engine, venue, _ = v3
    venue.asset_overrides = {
        "CENT/USD": {"min_trade_increment": "0.01", "min_order_size": "0.01"},
        "UNIT/USD": {"min_trade_increment": "1", "min_order_size": "1"},
        "BIG/USD": {"min_trade_increment": "1", "min_order_size": "1"},
    }
    _, cent = enter(v3, "CENT/USD", SIX)
    assert cent["payload"]["qty"] == "8.33"
    assert D(cent["context"]["sizing"]["planned_risk"]) == D("49.98")
    unit_levels = {"entry_trigger": "300", "max_entry_price": "300", "stop": "294",
                   "target": "312"}
    _, unit = enter(v3, "UNIT/USD", unit_levels)
    assert unit["payload"]["qty"] == "3" and D(unit["context"]["budget"]) == D(18)
    big_levels = {"entry_trigger": "1500", "max_entry_price": "1500", "stop": "1470",
                  "target": "1560"}
    sid, big = enter(v3, "BIG/USD", big_levels)  # One whole coin is more than the slice.
    assert big["reason"] == "ZERO_SHARE_SIZE" and engine._load(sid)[1]["state"] == "RISK_REJECTED"


def test_a_stop_closer_than_two_percent_is_refused_at_entry_under_v3(v3):
    engine, venue, _ = v3
    sid, decision = enter(v3, "NEAR/USD", SHORT)
    assert decision["reason"] == "STOP_DISTANCE_BELOW_MINIMUM" and decision["payload"] == {}
    assert engine._load(sid)[1]["state"] == "RISK_REJECTED" and not venue.orders_of("buy")


# --- Limits: 5% of equity open, three per sector, BTC, ETH and SOL together -------------------


def test_open_crypto_planned_risk_is_capped_at_five_percent_of_equity(v3):
    engine, _, _ = v3
    outcomes = [enter(v3, f"CAP{i}/USD", SIX)[1]["reason"] for i in range(11)]
    # Ten 6%-stop trades at $49.9998 each hold $499.998; an eleventh would pass $500.
    assert outcomes == ["RISK_APPROVED"] * 10 + ["MAX_OPEN_PLANNED_RISK"]
    with engine.repo.connect() as conn:
        total = conn.execute("""SELECT sum(budget) AS budget,sum(planned_risk) AS planned
            FROM lab.account_risk_reservations""").fetchone()
        # SQL/Python parity: the engine recorded exactly the database function's answer.
        assert account_risk_failure(conn, V3, "CRYPTO", "CRYPTO", "T_CAP10/USD", EQUITY,
                                    D("49.9998")) == "MAX_OPEN_PLANNED_RISK"
    assert total["budget"] == total["planned"] == D("499.998")


def test_ten_two_percent_trades_fit_in_the_ten_thousand_dollar_account_at_once(v3):
    engine, _, _ = v3
    outcomes = [enter(v3, f"FIT{i}/USD", TWO)[1] for i in range(11)]
    assert [d["reason"] for d in outcomes] == ["RISK_APPROVED"] * 10 + [
        "INSUFFICIENT_BUYING_POWER"]
    assert [D(d["payload"]["qty"]) for d in outcomes[:10]] == [D(10)] * 10  # $1,000 each.
    with engine.repo.connect() as conn:
        total = conn.execute("""SELECT sum(planned_risk) AS risk,sum(qty*max_entry) AS notional
            FROM lab.account_risk_reservations""").fetchone()
    assert (total["risk"], total["notional"]) == (D(200), D(10000))


def cancel_entry(mx, setup_id, symbol):
    """The broker cancels an unfilled crypto entry; the next protection pass closes it."""
    engine, venue, _ = mx
    for order in venue.orders_of("buy"):
        if order["symbol"] == symbol:
            order["status"] = "canceled"
    engine.manage(setup_id, observation(mx))
    assert engine._load(setup_id)[1]["state"] == "CLOSED"


def test_three_open_trades_per_sector_btc_eth_and_sol_together(v3):
    from catalyst_lab.managed_classification import (
        SECTOR_CLASSIFICATION_POLICY,
        initialize_managed_classifications,
    )

    engine, venue, _ = v3
    coins = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "SHIB/USD", "PEPE/USD", "BONK/USD",
             "NEWCOIN/USD"]
    initialize_managed_classifications(engine.repo, engine.broker,
                                       policy_id=SECTOR_CLASSIFICATION_POLICY, us_mapping=(),
                                       crypto_symbols=coins)
    setups, reasons = {}, {}
    for coin in coins:
        setups[coin] = engine.admit(selected(v3, coin, levels=TWO))  # The imported sector.
        reasons[coin] = engine.observe_trigger(setups[coin], touch(v3, TWO))["reason"]
    assert reasons == {**{c: "RISK_APPROVED" for c in coins}, "BONK/USD": "CORRELATION_LIMIT"}
    with engine.repo.connect() as conn:
        themes = {r["theme"]: r["n"] for r in conn.execute(
            "SELECT theme,count(*) AS n FROM lab.account_risk_reservations GROUP BY theme"
        ).fetchall()}
    assert themes == {"LARGE_CAP_L1": 3, "MEME": 3, "CRYPTO_OTHER": 1}
    state = engine._load(setups["BONK/USD"])[1]
    assert state["state"] == "WATCHING" and state["capacity_deferred_reason"] == (
        "CORRELATION_LIMIT")
    # A meme trade closes: after the 60-second cooldown the fourth meme coin may enter.
    cancel_entry(v3, setups["DOGE/USD"], "DOGE/USD")
    venue.now += timedelta(seconds=61)
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(setups["BONK/USD"], touch(v3, TWO))["reason"] == (
        "RISK_APPROVED")


def test_a_v2_reservation_keeps_its_one_per_theme_rule_against_v3_entries(v3):
    engine, venue, reviews = v3
    v2 = ManagedExecution(engine.repo, engine.broker, policy=engine.policy, clock=engine.now,
                          review_store=reviews, risk_policy_id=MANAGED_RISK_V2_POLICY_ID)
    v2.reconciled_at = engine.reconciled_at
    _, first = enter((v2, venue, reviews), "OLDM/USD", TWO, ("CRYPTO", "MEME"))
    assert first["outcome"] == "APPROVED" and first["context"]["risk_policy_id"] == (
        MANAGED_RISK_V2_POLICY_ID)
    assert "sizing" not in first["context"] and D(first["context"]["budget"]) == D(50)
    # The stricter of the two rules holds while the V2 reservation is open.
    _, blocked = enter(v3, "NEWM/USD", TWO, ("CRYPTO", "MEME"))
    assert blocked["reason"] == "CORRELATION_LIMIT"
    _, other = enter(v3, "OTHR/USD", TWO, ("CRYPTO", "LARGE_CAP_L1"))
    assert other["outcome"] == "APPROVED"


# --- The reservation guard: one SQL source of truth ------------------------------------------


def function_body(path, header):
    text = path.read_text()
    start = text.index("AS $$", text.index(header)) + len("AS $$")
    return text[start:text.index("$$;", start)]


def test_the_reservation_guard_is_migration_016_byte_for_byte_behind_one_slice_branch(er):
    header = "CREATE OR REPLACE FUNCTION lab.guard_managed_reservation()"
    old = function_body(MIGRATIONS / "016_account_risk_policy.sql", header)
    new = function_body(MIGRATIONS / "022_crypto_size_hold.sql", header)
    start = new.index(" IF EXISTS(SELECT 1 FROM lab.account_risk_market_terms t")
    end = new.index(" IF s.market='US_STOCKS' AND p.leverage_allowed")
    assert new[:start] + new[end:] == old
    with owner(er).connect() as conn:
        live = conn.execute(
            "SELECT prosrc FROM pg_proc WHERE oid='lab.guard_managed_reservation'::regproc"
        ).fetchone()["prosrc"]
        unchanged = {r["proname"]: r["prosrc"] for r in conn.execute(
            """SELECT proname,prosrc FROM pg_proc WHERE proname IN ('enforce_risk_reservation',
            'guard_legacy_shared_budget') AND pronamespace='lab'::regnamespace""").fetchall()}
    # Migration 027 (package trade-plan) reads the stop through lab.managed_planned_stop; for a
    # setup without a trade plan that is this packet stop (tests/test_trade_plan_migration.py).
    assert live == new.replace("(s.record_json->'levels'->>'stop')::numeric",
                               "lab.managed_planned_stop(s.setup_id)")
    for name in unchanged:  # V1's two triggers are migration 016's, untouched by 022.
        assert unchanged[name] == function_body(
            MIGRATIONS / "016_account_risk_policy.sql",
            f"CREATE OR REPLACE FUNCTION lab.{name}()")


def test_the_guard_refuses_a_slice_reservation_off_the_v3_rule_and_v2_keeps_its_own(v3):
    engine, _, _ = v3
    sids = {name: engine.admit(selected(v3, f"{name}/USD", levels=levels,
                                        classification=("CRYPTO", "T_" + name)))
            for name, levels in (("SIXG", SIX), ("TWOG", TWO), ("SHRT", SHORT))}

    def attempt(name, qty, *, budget=None, planned=None, policy=V3):
        setup_id = sids[name]
        with engine.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            setup = engine.store.setup(conn, setup_id)
            m = D(setup["record_json"]["levels"]["max_entry_price"])
            s = D(setup["record_json"]["levels"]["stop"])
            planned = qty * (m - s) if planned is None else planned
            budget = planned if budget is None else budget
            decision = engine._decision(
                conn, setup, engine.store.state(conn, setup_id), "ENTRY",
                {"symbol": setup["symbol"], "qty": str(qty), "client_order_id": uuid4().hex},
                {"risk_policy_id": policy, "venue": "ALPACA_PAPER"}, equity=EQUITY,
            )
            try:
                conn.execute(
                    """INSERT INTO lab.managed_reservations(setup_id,decision_id,budget,
                    planned_risk,qty,max_entry,sector,theme) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (setup_id, decision["decision_id"], budget, planned, qty, m, "CRYPTO",
                     "T_" + name))
                outcome = None
            except psycopg.errors.RaiseException as exc:
                outcome = exc.diag.message_primary
            conn.rollback()
        return outcome

    invalid = "INVALID_MANAGED_RISK_RESERVATION"
    assert attempt("SIXG", D("8.3333")) is None  # The engine's own size.
    assert attempt("TWOG", D(10)) is None
    assert attempt("SIXG", D("8.3333"), budget=D(50)) == invalid  # V3 holds actual risk.
    assert attempt("SIXG", D("8.3334")) == invalid  # $50.0004 of risk.
    assert attempt("TWOG", D("10.0001")) == invalid  # $1,000.01 of notional.
    assert attempt("SHRT", D(1)) == invalid  # A 1.5% stop.
    assert attempt("SIXG", D("8.3333"), planned=D("49.9997")) == invalid
    # JEV_MANAGED_RISK_V2 keeps its fixed budget on the same setup.
    assert attempt("SIXG", D(8), budget=D(50), policy=MANAGED_RISK_V2_POLICY_ID) is None
    assert attempt("SIXG", D(8), policy=MANAGED_RISK_V2_POLICY_ID) == invalid  # Budget 48.
    assert attempt("SHRT", D(1), budget=D(50), policy=MANAGED_RISK_V2_POLICY_ID) is None


# --- Exhaustive parity: V1 and V2 answer as under migration 016; V3 as its oracle -------------

S_NEW, T_NEW = "SECTOR_NEW", "THEME_NEW"


def recreate_016_account_risk_failure(er):
    """Migration 016's lab.account_risk_failure, byte for byte, beside the live one."""
    text = (MIGRATIONS / "016_account_risk_policy.sql").read_text()
    start = text.index("CREATE FUNCTION lab.account_risk_failure(")
    source = text[start:text.index("END $$;", start) + len("END $$;")]
    with owner(er).connect() as conn:
        conn.execute(source.replace("lab.account_risk_failure(", "lab.account_risk_failure_016(",
                                    1))
        conn.execute("""GRANT EXECUTE ON FUNCTION lab.account_risk_failure_016(text,text,text,
            text,text,numeric,numeric) TO catalyst_risk""")


def oracle(policy, market, existing, budget):
    """Independent restatement: stricter-of correlation with per-market theme limits, then caps
    summing every open reservation's budget (a V3 crypto reservation's is its planned risk)."""
    same_sector = [e for e in existing if e["sector"] == S_NEW]
    same_theme = [e for e in existing if e["theme"] == T_NEW]
    sector_limits = [e["policy"].max_per_sector for e in same_sector
                     if e["market"] in e["policy"].sector_limited_markets]
    if market in policy.sector_limited_markets:
        sector_limits.append(policy.max_per_sector)
    theme_limits = [e["policy"].theme_limit(e["market"]) for e in same_theme]
    theme_limits.append(policy.theme_limit(market))
    if (sector_limits and len(same_sector) >= min(sector_limits)) or len(same_theme) >= min(
        theme_limits
    ):
        return "CORRELATION_LIMIT"
    if sum(e["budget"] for e in existing) + budget > policy.account_cap_pct * EQUITY:
        return "MAX_OPEN_PLANNED_RISK"
    market_total = sum(e["budget"] for e in existing if e["market"] == market)
    if market_total + budget > policy.market_cap(market) * EQUITY:
        return "MARKET_RISK_CAP"
    return None


class PolicyLedger(Ledger):
    """016's savepointed ledger, reserving managed setups under any policy row's own rule."""

    def reserve(self, setup_id, policy, sector, theme):
        setup = self.managed.store.setup(self.conn, setup_id)
        state = self.managed.store.state(self.conn, setup_id)
        m = D(setup["record_json"]["levels"]["max_entry_price"])
        s = D(setup["record_json"]["levels"]["stop"])
        terms = policy.terms(setup["market"])
        if terms is not None:
            qty = slice_size(policy, terms, equity=EQUITY, max_entry=m, stop=s,
                             available=EQUITY, increment=D("0.0001")).qty
            budget = qty * (m - s)
        else:
            budget = EQUITY * policy.risk_pct
            qty = (budget / (m - s)).to_integral_value(rounding="ROUND_DOWN")
        decision = self.managed._decision(
            self.conn, setup, state, "ENTRY",
            {"symbol": setup["symbol"], "qty": str(qty), "client_order_id": uuid4().hex},
            {"risk_policy_id": policy.policy_id, "venue": "ALPACA_PAPER"}, equity=EQUITY,
        )
        self.conn.execute(
            """INSERT INTO lab.managed_reservations(setup_id,decision_id,budget,planned_risk,
            qty,max_entry,sector,theme) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (setup_id, decision["decision_id"], budget, qty * (m - s), qty, m, sector, theme),
        )


def test_exhaustive_parity_v1_v2_answer_as_016_and_v3_as_its_oracle(mx, er, raw, evidence,
                                                                   policy):
    engine, venue, _ = mx
    recreate_016_account_risk_failure(er)
    # A LAB_FIXTURE entry rule with small market caps, so the caps' sums, which now hold V3's
    # planned-risk budgets, are reached within three open reservations.
    add_policy(er, "LAB_FIXTURE_SMALL_CAPS", sector=2, theme=2, risk="0.005", cap="0.05",
               caps={"US_STOCKS": "0.006", "CRYPTO": "0.006"}, limited=("US_STOCKS",))
    with er.connect() as conn:
        rows = {p: load_policy(conn, p) for p in (FROZEN_V1_POLICY_ID, LEGACY_MANAGED_POLICY_ID,
                                                  MANAGED_RISK_V2_POLICY_ID, V3,
                                                  "LAB_FIXTURE_SMALL_CAPS")}
    v1, legacy, v2, v3_policy, small_caps = rows.values()
    pools = {
        "V1": [legacy_candidate(mx, er, raw, evidence, policy, symbol=f"PQV{i}")[0]
               for i in range(4)],
        "US_STOCKS": [engine.admit(packet(mx, f"PQU{i}")) for i in range(4)],
        "CRYPTO": [engine.admit(packet(mx, f"PQC{i}/USD")) for i in range(4)],
    }
    legacy_engine = RiskEngine(engine.repo, None, RiskPolicy(), ready=lambda: True,
                               clock=lambda: venue.now)
    # The slice size of the fixture levels (M 100.10, S 95): 9.8039 x 5.10 = $49.99989.
    v3_crypto_budget = slice_size(v3_policy, v3_policy.terms("CRYPTO"), equity=EQUITY,
                                  max_entry=D("100.10"), stop=D("95"), available=EQUITY,
                                  increment=D("0.0001")).planned_risk
    assert v3_crypto_budget == D("49.99989")
    kinds = [("V1", v1, "US_STOCKS"), ("V2", v2, "US_STOCKS"), ("V2", v2, "CRYPTO"),
             ("V3", v3_policy, "US_STOCKS"), ("V3", v3_policy, "CRYPTO")]
    options = [(kind, overlap) for kind in kinds for overlap in ("NONE", "SECTOR", "THEME",
                                                                 "BOTH")]
    entries = [("V1", v1, "US_STOCKS"), ("LEGACY", legacy, "CRYPTO"),
               ("V2", v2, "US_STOCKS"), ("V2", v2, "CRYPTO"),
               ("V3", v3_policy, "US_STOCKS"), ("V3", v3_policy, "CRYPTO"),
               ("FIXTURE", small_caps, "CRYPTO")]
    results = {"checks": 0, "same_as_016": 0, "v3_differs": 0, "reachable": 0, "outcomes": {}}

    def budget_of(rule, market):
        if rule.terms(market) is not None:
            return v3_crypto_budget
        return EQUITY * rule.risk_pct

    with engine.store.transaction() as conn:
        ledger = PolicyLedger(conn, engine, legacy_engine, pools, er)
        new_v1, new_us, new_crypto = pools["V1"][3], pools["US_STOCKS"][3], pools["CRYPTO"][3]
        for ticker in ("PQV3", "PQU3", "PQC3/USD"):
            ledger.classify(ticker, S_NEW, T_NEW)

        def check(existing):
            for label, rule, market in entries:
                budget = budget_of(rule, market)
                expected = oracle(rule, market, existing, budget)
                args = (rule.policy_id, "ALPACA_PAPER", market, S_NEW, T_NEW, EQUITY, budget)
                python = account_risk_failure(conn, rule.policy_id, market, S_NEW, T_NEW,
                                              EQUITY, budget)
                before = conn.execute(
                    "SELECT lab.account_risk_failure_016(%s,%s,%s,%s,%s,%s,%s) AS r", args
                ).fetchone()["r"]
                if label == "V1":
                    trigger = ledger.attempt(lambda: ledger.reserve_v1(new_v1, S_NEW, T_NEW))
                else:
                    setup = new_us if market == "US_STOCKS" else new_crypto
                    trigger = ledger.attempt(
                        lambda s=setup, r=rule: ledger.reserve(s, r, S_NEW, T_NEW))
                assert python == expected == trigger, (label, market, existing)
                if label in {"V1", "LEGACY", "V2"}:  # Exactly as under migration 016.
                    assert python == before, (label, market, existing)
                    results["same_as_016"] += 1
                elif label == "V3" and python != before:
                    results["v3_differs"] += 1
                results["checks"] += 1
                results["outcomes"][expected] = results["outcomes"].get(expected, 0) + 1

        def explore(start, existing, used):
            check(existing)
            if len(existing) == 3:
                return
            for index in range(start, len(options)):
                (kind, rule, market), overlap = options[index]
                pool = "V1" if kind == "V1" else market
                item = pools[pool][used[pool]]
                ticker = (f"PQV{used[pool]}" if pool == "V1" else
                          f"PQU{used[pool]}" if pool == "US_STOCKS" else
                          f"PQC{used[pool]}/USD")
                sector = S_NEW if overlap in {"SECTOR", "BOTH"} else "SECTOR_" + ticker
                theme = T_NEW if overlap in {"THEME", "BOTH"} else "THEME_" + ticker
                depth = f"node{len(existing)}"
                conn.execute(f"SAVEPOINT {depth}")
                ledger.classify(ticker, sector, theme)

                def write(kind=kind, item=item, rule=rule, sector=sector, theme=theme):
                    if kind == "V1":
                        ledger.reserve_v1(item, sector, theme)
                    else:
                        ledger.reserve(item, rule, sector, theme)

                if ledger.attempt(write) is None:  # Reachable through the real triggers.
                    write()
                    results["reachable"] += 1
                    budget = D("100") if kind == "V1" else budget_of(rule, market)
                    explore(index, existing + [{"policy": rule, "market": market,
                                                "sector": sector, "theme": theme,
                                                "budget": budget}],
                            {**used, pool: used[pool] + 1})
                conn.execute(f"ROLLBACK TO SAVEPOINT {depth}")

        explore(0, [], {"V1": 0, "US_STOCKS": 0, "CRYPTO": 0})
        conn.rollback()
    print(f"\nV3 ACCOUNT RISK PARITY: {results}")
    assert results["checks"] >= 2000 and results["v3_differs"] > 0
    assert set(results["outcomes"]) == {
        None, "CORRELATION_LIMIT", "MAX_OPEN_PLANNED_RISK", "MARKET_RISK_CAP"}
    with engine.repo.connect() as conn:  # Nothing from the exploration was committed.
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()
    assert verify_events(engine.repo.export_events())["valid"]


# --- CRYPTO_24H_HOLD_V1: 24 hours from the first fill, no midnight close ----------------------

DEFAULT = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}


def day_policy():
    """The deploy example's NY-day policy, configured on the engine for non-V3 setups."""
    from catalyst_lab.crypto_execution import CryptoDayPolicy

    return CryptoDayPolicy("CRYPTO_NY_DAY_PAPER_V1", 10, 5, 86400)


def ny(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), NY).astimezone(UTC)


def next_ny(now, hour, minute=0):
    """The first New York wall-clock time at or after ``now``; the fixture clock only moves
    forward, so every database-side deadline derived from it stays in the future."""
    day = now.astimezone(NY).date()
    at = ny(day, hour, minute)
    return at if at >= now else ny(day + timedelta(days=1), hour, minute)


def admit_v3(mx, symbol, levels=TWO):
    """A report-V3 pick through intake, the mock Jev and publication, admitted on a live
    pullback quote (SYSTEM_CHECK_V1 passes: mid 100.50 is the agent's price)."""
    from tests.test_system_check import at_price, publish_v3, two_slots, v3_pick

    engine, venue, _ = mx
    slot, _ = two_slots(venue.now)
    selected_v3 = publish_v3(mx, [v3_pick(0, symbol, venue.now, levels=levels)], run_slot=slot)
    return engine.admit(selected_v3[symbol], live_quote=at_price("100.49", "100.51",
                                                                 at=venue.now))


def fill_entry(mx, setup_id, symbol, levels):
    engine, venue, _ = mx
    decision = engine.observe_trigger(setup_id, touch(mx, levels))
    assert decision["outcome"] == "APPROVED", decision["reason"]
    order = next(o for o in venue.orders_of("buy") if o["symbol"] == symbol)
    engine.ingest(venue.fill(order["id"], order["qty"], price=levels["max_entry_price"]))
    return order


def exit_orders(venue, symbol):
    return [o for o in venue.orders_of("sell", "market") if o["symbol"] == symbol]


def decisions(engine, setup_id, action):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT reason,payload FROM lab.managed_risk_decisions WHERE setup_id=%s
            AND action=%s AND outcome='APPROVED' ORDER BY event_seq""", (setup_id, action)
        ).fetchall()


def test_the_hold_policy_is_exact_and_read_back_only_from_the_state():
    assert CRYPTO_24H_HOLD.record() == {"policy_id": "CRYPTO_24H_HOLD_V1",
                                        "max_hold_seconds": 86400,
                                        "exit_reason": "HOLD_24H_EXIT"}
    for bad in (("CRYPTO_24H_HOLD_V2", 86400, HOLD_EXIT_REASON),
                ("CRYPTO_24H_HOLD_V1", 86399, HOLD_EXIT_REASON),
                ("CRYPTO_24H_HOLD_V1", 86400.0, HOLD_EXIT_REASON),
                ("CRYPTO_24H_HOLD_V1", 86400, "TIME_EXIT")):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_HOLD_POLICY_REQUIRED"):
            CryptoHoldPolicy(*bad)
    first = datetime(2026, 11, 1, 3, 30, tzinfo=UTC)  # 23:30 New York, the night DST ends.
    assert CRYPTO_24H_HOLD.exit_at(first) == first + timedelta(hours=24)  # Elapsed, not local.
    with pytest.raises(ValueError, match="AWARE_CRYPTO_FILL_TIMESTAMP_REQUIRED"):
        CRYPTO_24H_HOLD.exit_at(first.replace(tzinfo=None))
    assert recorded_hold_policy({}) is None and time_exit_reason({}) == "TIME_EXIT"
    held = {"holding_policy": CRYPTO_24H_HOLD.record()}
    assert recorded_hold_policy(held) == CRYPTO_24H_HOLD
    assert time_exit_reason(held) == HOLD_EXIT_REASON


def test_a_v3_position_exits_24_hours_after_its_first_fill_across_new_york_midnight(
        v3, monkeypatch):
    mx = v3  # The engine admits under JEV_MANAGED_RISK_V3, as deployed.
    # The control arm keeps CRYPTO_24H_HOLD_V1; the maintained arm's 24-hour review
    # (CRYPTO_24H_REVIEW_V1, package day-review) is tested in tests/test_day_review.py.
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    engine, venue, _ = mx
    engine.crypto_day_policy = day_policy()  # Configured, as in the deploy example.
    venue.now = next_ny(venue.now, 23)  # 23:00 New York.
    assert engine.reconcile()["clean"]  # Entries need a reconciliation within 60 seconds.
    held = admit_v3(mx, "HOLD/USD")
    day = engine.admit(packet(mx, "DAYP/USD"))  # A report-V2 crypto pick: the NY-day policy.
    state = engine._load(held)[1]
    assert state["holding_policy"] == CRYPTO_24H_HOLD.record()
    assert (state["crypto_day_policy"], state["crypto_entry_deadline"],
            state["crypto_flat_deadline"]) == (None, None, None)
    old = engine._load(day)[1]
    assert old["crypto_day_policy"]["policy_id"] == "CRYPTO_NY_DAY_PAPER_V1"
    assert "holding_policy" not in old
    fill_entry(mx, held, "HOLD/USD", TWO)
    fill_entry(mx, day, "DAYP/USD", DEFAULT)
    first_fill = venue.now
    for sid in (held, day):
        engine.manage(sid, observation(mx))  # Opens the position and places its stop-limit.
    state, old = engine._load(held)[1], engine._load(day)[1]
    assert state["state"] == old["state"] == "OPEN"
    assert datetime.fromisoformat(state["hard_exit_at"]) == first_fill + timedelta(hours=24)
    flat = ny(first_fill.astimezone(NY).date(), 23, 55)
    assert datetime.fromisoformat(old["hard_exit_at"]) == flat

    def tick():
        for sid in (held, day):
            engine.manage(sid, observation(mx))

    # 23:56 New York: the NY-day policy flattens the old setup; the V3 position stays open.
    venue.now = flat + timedelta(minutes=1)
    tick()
    tick()
    assert engine._load(day)[1]["exit_requested"] == "TIME_EXIT"
    assert exit_orders(venue, "DAYP/USD") and not exit_orders(venue, "HOLD/USD")
    # After midnight and until one second before the 24 hours: no close of any kind.
    for at in (next_ny(venue.now, 0, 30), first_fill + timedelta(hours=24, seconds=-1)):
        venue.now = at
        engine.manage(held, observation(mx))
        state = engine._load(held)[1]
        assert state["state"] == "OPEN" and not state.get("exit_requested")
        assert not exit_orders(venue, "HOLD/USD")
    [stop] = [o for o in venue.orders_of("sell", "stop_limit") if o["symbol"] == "HOLD/USD"]
    assert stop["status"] == "new"
    # At 24 hours: HOLD_24H_EXIT cancels the stop-limit, then sells the whole position.
    venue.now = first_fill + timedelta(hours=24)
    engine.manage(held, observation(mx))
    assert engine._load(held)[1]["exit_requested"] == HOLD_EXIT_REASON
    assert stop["status"] == "canceled"
    engine.manage(held, observation(mx))
    [close] = exit_orders(venue, "HOLD/USD")
    assert D(close["qty"]) == D(10)
    assert [r["reason"] for r in decisions(engine, held, "CANCEL")] == [HOLD_EXIT_REASON]
    assert [r["reason"] for r in decisions(engine, held, "EXIT")] == [HOLD_EXIT_REASON]
    engine.ingest(venue.fill(close["id"], close["qty"], price="101"))
    engine.manage(held, observation(mx))
    closed = engine._load(held)[1]
    assert (closed["state"], closed["reason"]) == ("CLOSED", HOLD_EXIT_REASON)
    assert reservation(engine, held) is None  # Released at the close.
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_v3_pick_may_enter_after_the_midnight_cutoff_until_its_own_expiry(v3):
    mx = v3  # The engine admits under JEV_MANAGED_RISK_V3, as deployed.
    engine, venue, _ = mx
    engine.crypto_day_policy = day_policy()
    venue.now = next_ny(venue.now, 23, 52)  # Past the NY-day entry cutoff (23:50).
    with pytest.raises(ValueError, match="^CRYPTO_ENTRY_WINDOW_CLOSED$"):
        engine.admit(packet(mx, "LATE/USD"))  # A report-V2 pick keeps the NY-day window.
    late = admit_v3(mx, "LATEV3/USD")
    expiring = admit_v3(mx, "EXPV3/USD")
    venue.now = next_ny(venue.now, 0, 5)  # After midnight, before the picks' expiry.
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(late, touch(mx, TWO))["outcome"] == "APPROVED"
    with engine.repo.connect() as conn:
        expiry = engine.store.setup(conn, expiring)["expires_at"]
    venue.now = expiry  # The setup's expiry, the next run: no more entries.
    assert engine.observe_trigger(expiring, touch(mx, TWO)) is None
    assert engine._load(expiring)[1]["state"] == "EXPIRED_UNTRIGGERED"


def test_the_first_fill_starts_the_24_hour_clock_and_nothing_restarts_it(v3, monkeypatch):
    mx = v3  # The engine admits under JEV_MANAGED_RISK_V3, as deployed.
    # The control arm keeps CRYPTO_24H_HOLD_V1 (package day-review).
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    from tests.test_managed_operator_halts import restart

    engine, venue, _ = mx
    sid = admit_v3(mx, "PART/USD")
    assert engine.observe_trigger(sid, touch(mx, TWO))["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == "PART/USD")
    venue.now += timedelta(seconds=2)
    first_fill = venue.now
    engine.ingest(venue.fill(entry["id"], "4", price="100"))  # A partial fill.
    venue.now += timedelta(minutes=30)
    engine.manage(sid, observation(mx))
    state = engine._load(sid)[1]
    assert datetime.fromisoformat(state["opened_at"]) == first_fill
    assert datetime.fromisoformat(state["hard_exit_at"]) == first_fill + timedelta(hours=24)
    venue.now += timedelta(minutes=30)
    engine.ingest(venue.fill(entry["id"], "6", price="100"))  # A later fill of the rest.
    new = restart(engine)  # No policy supplied: the recorded one controls the lifecycle.
    new.manage(sid, observation(mx))
    assert new._load(sid)[1]["hard_exit_at"] == state["hard_exit_at"]
    assert new._load(sid)[1]["holding_policy"] == CRYPTO_24H_HOLD.record()


# --- ALPACA_CRYPTO_SECTORS_V1 -----------------------------------------------------------------

# Alpaca's USD crypto pairs of 2026-09-26, stablecoins excluded (the owner's list).
ALPACA_USD_PAIRS = (
    "AAVE", "ADA", "ARB", "AVAX", "BAT", "BCH", "BONK", "BTC", "CRV", "DOGE", "DOT", "ETH", "FIL",
    "GRT", "HYPE", "LDO", "LINK", "LTC", "ONDO", "PAXG", "PEPE", "POL", "RENDER", "SHIB", "SKY",
    "SOL", "SUSHI", "TRUMP", "UNI", "WIF", "XRP", "XTZ", "YFI",
)


def current_classifications(engine):
    with engine.repo.connect() as conn:
        return {r["ticker"]: (r["sector"], r["theme"]) for r in conn.execute(
            "SELECT ticker,sector,theme FROM lab.current_classifications").fetchall()}


def test_the_built_in_sectors_cover_every_alpaca_usd_pair_once():
    from catalyst_lab.managed_classification import (
        ALPACA_CRYPTO_SECTOR_OF,
        ALPACA_CRYPTO_SECTORS_V1,
        DEFAULT_CRYPTO_BUCKET,
        crypto_bucket_config,
    )

    assert len(ALPACA_USD_PAIRS) == 33
    assert sorted(ALPACA_CRYPTO_SECTOR_OF) == sorted(f"{c}/USD" for c in ALPACA_USD_PAIRS)
    assert sum(len(s) for s in ALPACA_CRYPTO_SECTORS_V1.values()) == 33  # Each coin once.
    # The list is a valid owner-bucket configuration (names, symbols, no duplicates).
    config = crypto_bucket_config({"buckets": {k: list(v) for k, v in
                                               ALPACA_CRYPTO_SECTORS_V1.items()},
                                   "unlisted": DEFAULT_CRYPTO_BUCKET})
    assert config["buckets"] == dict(ALPACA_CRYPTO_SECTORS_V1)
    assert DEFAULT_CRYPTO_BUCKET not in ALPACA_CRYPTO_SECTORS_V1
    assert ALPACA_CRYPTO_SECTORS_V1["LARGE_CAP_L1"] == ("BTC/USD", "ETH/USD", "SOL/USD")
    assert {k: len(v) for k, v in ALPACA_CRYPTO_SECTORS_V1.items()} == {
        "LARGE_CAP_L1": 3, "SMART_CONTRACT_L1": 4, "PAYMENTS": 3, "MEME": 6, "DEFI": 8,
        "ORACLE_DATA_INFRA": 2, "AI_COMPUTE": 2, "LAYER2_SCALING": 2, "REAL_WORLD_ASSETS": 1,
        "GOLD_BACKED": 1, "WEB3_APPLICATIONS": 1}


def test_the_sector_import_classifies_every_coin_and_an_unlisted_one_as_crypto_other(v3):
    from catalyst_lab.managed_classification import (
        CLASSIFICATION_POLICY,
        SECTOR_CLASSIFICATION_POLICY,
        initialize_managed_classifications,
    )

    engine, venue, _ = v3
    universe = [f"{c}/USD" for c in ALPACA_USD_PAIRS] + ["NEWLIST/USD", "USDC/USD"]
    # The earlier deploy example: every pair in the one shared CRYPTO_SHARED theme.
    initialize_managed_classifications(engine.repo, engine.broker,
                                       policy_id=CLASSIFICATION_POLICY, us_mapping=(),
                                       crypto_symbols=["BTC/USD", "GONE/USD"])
    result = initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=SECTOR_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=universe)
    assert result == {"inserted": 36, "configured": 36, "policy_id": "ALPACA_CRYPTO_SECTORS_V1",
                      "unlisted_rule": "CRYPTO_OTHER"}
    current = current_classifications(engine)
    assert current["BTC/USD"] == current["ETH/USD"] == ("CRYPTO", "LARGE_CAP_L1")
    assert current["PAXG/USD"] == ("CRYPTO", "GOLD_BACKED")
    assert current["DOGE/USD"] == current["TRUMP/USD"] == ("CRYPTO", "MEME")
    # Coins the list does not name, and an earlier row no longer supplied, are CRYPTO_OTHER.
    for coin in ("NEWLIST/USD", "USDC/USD", "GONE/USD"):
        assert current[coin] == ("CRYPTO", "CRYPTO_OTHER")
    with engine.repo.connect() as conn:
        body = conn.execute("""SELECT payload_json AS p FROM lab.system_events
            WHERE event_type='CLASSIFICATION_IMPORTED' AND payload_json->>'ticker'='BTC/USD'
            ORDER BY created_at DESC LIMIT 1""").fetchone()["p"]
    assert (body["classification_policy"], body["unlisted_rule"], body["supersedes"]) == (
        "ALPACA_CRYPTO_SECTORS_V1", "CRYPTO_OTHER", {"sector": "CRYPTO", "theme": "CRYPTO_SHARED"})
    again = initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=SECTOR_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=universe)
    assert again["inserted"] == 0  # Identical imports are idempotent.
    # A pick of a coin not on the list is admitted and sized; it never waits on
    # CORRELATION_UNKNOWN.
    sid = engine.admit(selected(v3, "NEWLIST/USD", levels=TWO))
    decision = engine.observe_trigger(sid, touch(v3, TWO))
    assert decision["outcome"] == "APPROVED"
    assert reservation(engine, sid)["theme"] == "CRYPTO_OTHER"


def test_a_pair_the_list_names_but_alpaca_no_longer_lists_never_fails_the_import(v3):
    from catalyst_lab.managed_classification import (
        SECTOR_CLASSIFICATION_POLICY,
        initialize_managed_classifications,
    )

    engine, venue, _ = v3
    result = initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=SECTOR_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=["BTC/USD"])
    assert result["configured"] == 1  # Only the supplied pair; YFI and the rest are not read.
    assert current_classifications(engine) == {"BTC/USD": ("CRYPTO", "LARGE_CAP_L1")}
    assert [c for c in venue.calls if c[1].startswith("/v2/assets/")] == [
        ("GET", "/v2/assets/BTC/USD", b"")]


def test_the_owners_buckets_override_the_built_in_sectors(v3):
    from catalyst_lab.managed_classification import (
        BUCKET_CLASSIFICATION_POLICY,
        SECTOR_CLASSIFICATION_POLICY,
        initialize_managed_classifications,
    )

    engine, _, _ = v3
    initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=SECTOR_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=["BTC/USD", "ETH/USD", "DOGE/USD", "ZED/USD"])
    buckets = {"buckets": {"MAJORS": ["BTC/USD"], "DOGS": ["DOGE/USD", "ETH/USD"]},
               "unlisted": "REFUSE"}
    result = initialize_managed_classifications(
        engine.repo, engine.broker, policy_id=SECTOR_CLASSIFICATION_POLICY, us_mapping=(),
        crypto_symbols=[], crypto_buckets=buckets)
    # Exactly the V2 bucket import: the owner's buckets, and the refusal marker for the rest.
    assert result["policy_id"] == BUCKET_CLASSIFICATION_POLICY
    assert result["unlisted_rule"] == "REFUSE"
    assert current_classifications(engine) == {
        "BTC/USD": ("CRYPTO", "MAJORS"), "ETH/USD": ("CRYPTO", "DOGS"),
        "DOGE/USD": ("CRYPTO", "DOGS"), "ZED/USD": ("CRYPTO", "CRYPTO_UNLISTED")}
    with pytest.raises(ValueError, match="^CORRELATION_UNKNOWN$"):
        engine.admit(selected(v3, "ZED/USD", levels=TWO))  # The owner chose to refuse it.


def test_launch_settings_accept_optional_owner_buckets_with_the_built_in_sectors():
    from catalyst_lab.managed_app import AppSettings
    from catalyst_lab.managed_classification import (
        BUCKET_CLASSIFICATION_POLICY,
        CLASSIFICATION_POLICY,
        SECTOR_CLASSIFICATION_POLICY,
    )

    token = "t" * 40
    buckets = {"buckets": {"MAJORS": ["BTC/USD"]}, "unlisted": "CRYPTO_OTHER"}
    for policy_id, owner_buckets in ((SECTOR_CLASSIFICATION_POLICY, None),
                                     (SECTOR_CLASSIFICATION_POLICY, buckets),
                                     (BUCKET_CLASSIFICATION_POLICY, buckets),
                                     (CLASSIFICATION_POLICY, None)):
        AppSettings(8799, token, 300, None, policy_id, (), crypto_buckets=owner_buckets)
    for policy_id, owner_buckets in ((BUCKET_CLASSIFICATION_POLICY, None),
                                     (CLASSIFICATION_POLICY, buckets),
                                     ("ALPACA_CRYPTO_SECTORS_V2", None)):
        with pytest.raises(ValueError, match="^REQUIRED_MANAGED_APP_CONFIGURATION_INVALID$"):
            AppSettings(8799, token, 300, None, policy_id, (), crypto_buckets=owner_buckets)


# --- A pick for a coin that already has an open trade (top-K: declined and replaced) ----------


def test_a_topk_pick_for_a_coin_with_an_open_trade_is_declined_and_replaced(mx, market):
    from catalyst_lab.managed_runtime import (
        PERMANENT_ADMISSION_REFUSALS,
        TOPK_PERMANENT_ADMISSION_REFUSALS,
        permanent_refusal,
    )
    from tests.test_replacement import PASSING, TOP, ladder, quote, runtime_for
    from tests.test_replacement import events as ledger_events
    from tests.test_replacement import selected as published

    engine, venue, _ = mx
    code = "ACTIVE_SYMBOL_ALREADY_MANAGED"
    assert code in TOPK_PERMANENT_ADMISSION_REFUSALS and code not in PERMANENT_ADMISSION_REFUSALS
    assert permanent_refusal({"selection_policy": "JEV_TOP_K_SELECTION_V1"}, code)
    assert not permanent_refusal({"selection_policy": "MUSE_JEV_RESEARCH_SELECTION_V2"}, code)
    cycle, cycle_id = ladder(mx)  # Top-K, K = 5: A1..A5 published, A6.. ranked below.
    # A2 already has an open trade: another (report-V2) selection of the coin, bought.
    trade = engine.admit(selected(mx, "A2/USD", levels=DEFAULT))
    fill_entry(mx, trade, "A2/USD", DEFAULT)
    engine.manage(trade, observation(mx))
    assert engine._load(trade)[1]["state"] == "OPEN"
    runtime = runtime_for(mx, market, cycle, TOP + ["A6/USD"])
    quote(runtime, venue, {s: PASSING for s in TOP + ["A6/USD"]})
    runtime.execution_once()
    [decline] = ledger_events(engine, "RESEARCH_ADMISSION_DECLINED")
    [replacement] = ledger_events(engine, "RESEARCH_REPLACEMENT")
    first = published(engine, cycle_id)
    assert decline["body"]["reason"] == code
    assert decline["body"]["selection_event_seq"] == first["A2/USD"]["selection_event_seq"]
    assert {k: replacement["body"][k] for k in (
        "declined_symbol", "declined_code", "outcome", "replacement_symbol",
        "replacement_rank")} == {"declined_symbol": "A2/USD", "declined_code": code,
                                 "outcome": "PUBLISHED", "replacement_symbol": "A6/USD",
                                 "replacement_rank": 6}
    runtime.execution_once()  # The replacement is admitted through the normal path.
    active = {s["symbol"] for s in engine.store.active()}
    assert active == {"A1/USD", "A2/USD", "A3/USD", "A4/USD", "A5/USD", "A6/USD"}
    assert [s for s in engine.store.active() if s["symbol"] == "A2/USD"][0]["setup_id"] == trade
    runtime.execution_once()  # A declined pick is never offered again.
    assert len(ledger_events(engine, "RESEARCH_ADMISSION_DECLINED")) == 1
    assert runtime.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_v2_pick_for_a_coin_with_an_open_trade_still_waits_as_before(mx, market):
    from tests.test_replacement import events as ledger_events
    from tests.test_replacement import runtime_for

    engine, venue, _ = mx
    trade = engine.admit(selected(mx, "BUSY/USD", levels=DEFAULT,
                                  classification=("CRYPTO", "T_BUSY")))
    fill_entry(mx, trade, "BUSY/USD", DEFAULT)
    waiting = selected(mx, "BUSY/USD", levels=DEFAULT)  # A second V2 selection of the coin.
    runtime = runtime_for(mx, market, None, ["BUSY/USD"])
    for _ in range(2):
        runtime.execution_once()
    refused = [e["body"] for e in ledger_events(engine, "RUNTIME_ADMISSION_REFUSED")]
    assert [(b["selection_event_seq"], b["reason"]) for b in refused] == [
        (waiting["selection_event_seq"], "ACTIVE_SYMBOL_ALREADY_MANAGED")]  # Audited once.
    assert ledger_events(engine, "RESEARCH_ADMISSION_DECLINED") == []  # Never declined.
    assert waiting["selection_event_seq"] in {
        p["selection_event_seq"] for p in runtime._selected_packets()}  # Still offered.


# --- End to end: a top-K report-V3 pick under JEV_MANAGED_RISK_V3 ------------------------------


def test_a_topk_v3_pick_is_admitted_on_a_live_quote_sized_reserved_and_authorized(v3, market):
    from tests.test_replacement import PASSING, TOP, ladder, quote, runtime_for

    engine, venue, _ = v3
    cycle, cycle_id = ladder(v3)
    runtime = runtime_for(v3, market, cycle, TOP)
    quote(runtime, venue, {s: PASSING for s in TOP})  # Fresh stream quotes, mid 100.50.
    runtime.execution_once()
    setups = {s["symbol"]: s for s in engine.store.active()}
    assert sorted(setups) == TOP
    first = setups["A1/USD"]
    state = first["state"]
    assert state["risk_policy_id"] == V3 and state["entry_type"] == "PULLBACK"
    assert state["system_check"]["result"] == "PASSED"
    assert state["system_check"]["live"]["quote_source"] == "ALPACA_STREAM"
    # The arm's holding policy: CRYPTO_24H_HOLD_V1 in the control arm, CRYPTO_24H_REVIEW_V1
    # in the maintained arm (package day-review).
    assert state["holding_policy"] == admission_policy(True, state["arm"]).record()
    assert state["crypto_entry_deadline"] is None and state["crypto_flat_deadline"] is None
    assert first["record_json"]["selection_policy"] == "JEV_TOP_K_SELECTION_V1"
    decision = engine.observe_trigger(first["setup_id"], observation(
        v3, trade_price="100", bid="99.99", ask="100.01"))
    assert decision["outcome"] == "APPROVED"
    # Entry 100, max entry 100.10, stop 95: the $50 risk cap binds below the $1,000 slice.
    assert D(decision["payload"]["qty"]) == D("9.8039")
    assert D(decision["payload"]["limit_price"]) == D("100.10")
    assert decision["context"]["binding_constraint"] == "RISK"
    row = reservation(engine, first["setup_id"])
    assert row["budget"] == row["planned_risk"] == D("49.99989")
    assert row["qty"] * row["max_entry"] <= D(1000)
    [order] = [o for o in venue.orders_of("buy") if o["symbol"] == "A1/USD"]
    assert (order["type"], D(order["qty"]), D(order["limit_price"])) == (
        "limit", D("9.8039"), D("100.10"))
    with engine.repo.connect() as conn:
        claim = conn.execute("SELECT 1 FROM lab.managed_claims WHERE decision_id=%s",
                             (decision["decision_id"],)).fetchone()
    assert claim is not None  # The one-use authorization was claimed for this request.
    assert verify_events(engine.repo.export_events())["valid"]


# --- Migration 022's audit append (shared by the migration tests) -----------------------------

# to_jsonb of the two rows 022 inserts, as their audit events record them (event_seq removed).
V3_POLICY_AUDIT_ROW = {
    "policy_id": "JEV_MANAGED_RISK_V3", "engine": "MANAGED", "risk_pct": D("0.005"),
    "account_cap_pct": D("0.05"),
    "market_caps": {"US_STOCKS": D("0.03"), "CRYPTO": D("0.05"), "FOREX": D(0)},
    "max_per_sector": 2, "max_per_theme": 1, "sector_limited_markets": ["US_STOCKS"],
    "leverage_allowed": True, "intraday_buying_power_multiple": 2,
    "capacity_cooldown_seconds": 60, "fixed_exit_arm_pct": 30,
    "owner_ruling_ref": "CRYPTO-AGENT-LOOP 2026-09-26 D2; crypto-size-hold 2026-09-27",
}
V3_TERMS_AUDIT_ROW = {
    "policy_id": "JEV_MANAGED_RISK_V3", "market": "CRYPTO",
    "sizing_method": "EQUITY_SLICE_RISK_CAPPED_V1", "notional_pct": D("0.10"),
    "max_per_theme": 3, "min_stop_fraction": D("0.02"),
    "owner_ruling_ref": "CRYPTO-AGENT-LOOP 2026-09-26 D2 and 4.4; crypto-size-hold 2026-09-27",
}
MIGRATION_022_EVENTS = 2


def assert_migration_022_appended(owner_url, head_before, *, later=()):
    """Exactly migration 022's two audit events follow ``head_before`` (the audit head before
    it ran): the JEV_MANAGED_RISK_V3 row, then its CRYPTO terms, each hash-chained to the one
    before (event_hash = sha256(previous_hash || event_body)), then exactly ``later``'s
    ``(kind, row)`` events of a later migration (026's for a step to the current schema).
    Returns the new head."""
    import hashlib
    import json

    with psycopg.connect(owner_url) as conn:
        start = conn.execute("SELECT coalesce((SELECT seq FROM lab.trade_events "
                             "WHERE event_hash=%s),0)", (head_before,)).fetchone()[0]
        rows = conn.execute(
            """SELECT seq,event_type,strategy_version,candidate_id,previous_hash,event_hash,
            event_body,payload_json::text FROM lab.trade_events WHERE seq>%s ORDER BY seq""",
            (start,),
        ).fetchall()
    assert len(rows) == MIGRATION_022_EVENTS + len(later)
    previous = head_before
    for row, (kind, audited) in zip(rows, (("ACCOUNT_RISK_POLICIES", V3_POLICY_AUDIT_ROW),
                                           ("ACCOUNT_RISK_MARKET_TERMS", V3_TERMS_AUDIT_ROW),
                                           *later),
                                    strict=True):
        seq, event_type, strategy, candidate, previous_hash, event_hash, body, payload = row
        assert (event_type, strategy, candidate) == ("SYSTEM_EVENT", "CATALYST_RETEST_V1", None)
        assert json.loads(payload, parse_float=D, parse_int=D) == {"kind": kind, "row": audited}
        assert previous_hash == previous
        assert event_hash == hashlib.sha256((previous_hash + body).encode()).hexdigest()
        previous = event_hash
    return previous


def apply_migration_022(root):
    """Apply 022 (the current schema) as the owner to a disposable cluster at schema 21 and
    assert exactly its two audit events; returns the new audit head."""
    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 21
        conn.execute((MIGRATIONS / "022_crypto_size_hold.sql").read_text())
    return assert_migration_022_appended(owner_url, head)
