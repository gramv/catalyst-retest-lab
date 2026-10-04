"""Package risk-pacing, A1: ``JEV_MANAGED_RISK_V4`` (migration 026). V3's numbers with a 2% crypto
open-risk cap (all coins one cluster), and daily loss limits in the policy: hard 3% (cancel and
flatten, unchanged), soft 2% (no new entries, open positions keep their protection).

Fixture evidence only: per-test disposable PostgreSQL databases and the fake paper venue. No
broker, provider, network or owner-ledger contact.
"""

import hashlib
import json
from datetime import UTC, timedelta
from decimal import Decimal as D

import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.account_risk import (
    LEGACY_DAILY_HARD_LOSS_PCT,
    MANAGED_RISK_V2_POLICY_ID,
    MANAGED_RISK_V3_POLICY_ID,
    MANAGED_RISK_V4_POLICY_ID,
    SOFT_LIMIT_EVENT,
    SOFT_LIMIT_WAIT_EVENT,
    DailyLimits,
    account_risk_failure,
    load_policy,
    slice_size,
    slice_sizing_failure,
    soft_limit_reached,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_execution import ManagedExecution
from catalyst_lab.market import NY
from catalyst_lab.repository import Repository
from tests.clock import ny_midnight
from tests.test_account_risk_policy import owner
from tests.test_crypto_size_hold import MIGRATIONS, SIX, enter, reservation
from tests.test_crypto_trigger import rows, state
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation, packet

V3, V4 = MANAGED_RISK_V3_POLICY_ID, MANAGED_RISK_V4_POLICY_ID
EQUITY = D("10000")
RULING = "TRADING-QUALITY-PLAN A1, owner approval 2026-10-02; risk-pacing"

# to_jsonb of the three rows 026 inserts, as their audit events record them (event_seq removed).
V4_POLICY_AUDIT_ROW = {
    "policy_id": V4, "engine": "MANAGED", "risk_pct": D("0.005"), "account_cap_pct": D("0.05"),
    "market_caps": {"US_STOCKS": D("0.03"), "CRYPTO": D("0.02"), "FOREX": D(0)},
    "max_per_sector": 2, "max_per_theme": 1, "sector_limited_markets": ["US_STOCKS"],
    "leverage_allowed": True, "intraday_buying_power_multiple": 2,
    "capacity_cooldown_seconds": 60, "fixed_exit_arm_pct": 30, "owner_ruling_ref": RULING,
}
V4_TERMS_AUDIT_ROW = {
    "policy_id": V4, "market": "CRYPTO", "sizing_method": "EQUITY_SLICE_RISK_CAPPED_V1",
    "notional_pct": D("0.10"), "max_per_theme": 3, "min_stop_fraction": D("0.02"),
    "owner_ruling_ref": "TRADING-QUALITY-PLAN A1 (V3 terms kept), owner approval 2026-10-02; "
                        "risk-pacing",
}
V4_LIMITS_AUDIT_ROW = {
    "policy_id": V4, "hard_loss_pct": D("0.03"), "hard_action": "CANCEL_AND_FLATTEN",
    "soft_loss_pct": D("0.02"), "soft_action": "NO_NEW_ENTRIES", "owner_ruling_ref": RULING,
}
V4_AUDIT_EVENTS = (("ACCOUNT_RISK_POLICIES", V4_POLICY_AUDIT_ROW),
                   ("ACCOUNT_RISK_MARKET_TERMS", V4_TERMS_AUDIT_ROW),
                   ("ACCOUNT_RISK_DAILY_LIMITS", V4_LIMITS_AUDIT_ROW))
MIGRATION_026 = MIGRATIONS / "026_risk_v4.sql"


def engine_under(mx, policy_id):
    engine, venue, reviews = mx
    managed = ManagedExecution(engine.repo, engine.broker, policy=engine.policy,
                               clock=engine.now, review_store=reviews, risk_policy_id=policy_id)
    assert managed.reconcile()["clean"]
    return managed, venue, reviews


@pytest.fixture
def v4(mx):
    """The managed engine admitting under JEV_MANAGED_RISK_V4 (the deploy example's value)."""
    return engine_under(mx, V4)


@pytest.fixture
def v3(mx):
    return engine_under(mx, V3)


def decisions(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT action,outcome,reason FROM lab.managed_risk_decisions WHERE setup_id=%s
            ORDER BY created_at""", (sid,)).fetchall()


def buys(venue, symbol):
    return [o for o in venue.orders_of("buy") if o["symbol"] == symbol]


# --- The V4 row, its terms and daily limits ---------------------------------------------------


def test_v4_row_terms_and_limits_carry_the_owner_numbers_and_v3_is_untouched(er):
    with er.connect() as conn:
        v4 = load_policy(conn, V4, engine="MANAGED")
        v3 = load_policy(conn, V3, engine="MANAGED")
        v2 = load_policy(conn, MANAGED_RISK_V2_POLICY_ID, engine="MANAGED")
    # V3's numbers except the crypto cap: 2% of equity.
    for field in ("risk_pct", "account_cap_pct", "max_per_sector", "max_per_theme",
                  "sector_limited_markets", "leverage_allowed",
                  "intraday_buying_power_multiple", "capacity_cooldown_seconds",
                  "fixed_exit_arm_pct"):
        assert getattr(v4, field) == getattr(v3, field), field
    assert v4.market_caps == {"US_STOCKS": D("0.03"), "CRYPTO": D("0.02"), "FOREX": D(0)}
    assert v3.market_caps["CRYPTO"] == D("0.05")
    t3, t4 = v3.terms("CRYPTO"), v4.terms("CRYPTO")
    assert (t4.sizing_method, t4.notional_pct, t4.max_per_theme, t4.min_stop_fraction) == (
        t3.sizing_method, t3.notional_pct, t3.max_per_theme, t3.min_stop_fraction)
    assert v4.daily_limits == DailyLimits(V4, D("0.03"), "CANCEL_AND_FLATTEN", D("0.02"),
                                          "NO_NEW_ENTRIES", RULING)
    assert (v4.daily_hard_loss_pct(), v4.daily_soft_loss_pct()) == (D("0.03"), D("0.02"))
    # Every earlier policy: no daily-limits row, the legacy 3% halt, no soft limit, and its
    # evidence exactly as before.
    for policy in (v3, v2):
        assert policy.daily_limits is None and "daily_limits" not in policy.evidence()
        assert policy.daily_hard_loss_pct() == LEGACY_DAILY_HARD_LOSS_PCT == D("0.03")
        assert policy.daily_soft_loss_pct() is None
    assert v4.evidence()["daily_limits"]["soft_loss_pct"] == D("0.02")
    with owner(er).connect() as conn:
        audited = conn.execute(
            """SELECT count(*) FILTER (WHERE lab.research_audit_matches(
            'ACCOUNT_RISK_DAILY_LIMITS',to_jsonb(d),d.event_seq)) AS ok,count(*) AS n
            FROM lab.account_risk_daily_limits d""").fetchone()
    assert audited["n"] == audited["ok"] == 2  # V4's and V5's (migration 031).
    assert verify_events(er.export_events())["valid"]


def test_soft_limit_reached_is_inclusive_and_false_without_a_soft_limit(er):
    with er.connect() as conn:
        v4, v3 = load_policy(conn, V4), load_policy(conn, V3)
    assert soft_limit_reached(v4, D("-200"), EQUITY)
    assert soft_limit_reached(v4, D("-250"), EQUITY)
    assert not soft_limit_reached(v4, D("-199.99"), EQUITY)
    assert not soft_limit_reached(v3, D("-299.99"), EQUITY)


def test_daily_limits_are_written_only_with_their_new_managed_policy_row(er):
    limits_sql = """INSERT INTO lab.account_risk_daily_limits(policy_id,hard_loss_pct,
        hard_action,soft_loss_pct,soft_action,owner_ruling_ref)
        VALUES(%s,0.03,'CANCEL_AND_FLATTEN',%s,%s,'LAB_FIXTURE limits')"""
    for policy_id in (V3, V4, MANAGED_RISK_V2_POLICY_ID):
        with owner(er).connect() as conn, pytest.raises(
            psycopg.errors.RaiseException, match="DAILY_LIMITS_REQUIRE_NEW_MANAGED_POLICY"
        ):
            conn.execute(limits_sql, (policy_id, D("0.02"), "NO_NEW_ENTRIES"))
    row = """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
        market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
        intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
        owner_ruling_ref) VALUES(%s,'MANAGED',0.005,0.05,'{"CRYPTO":0.02}',2,1,'{US_STOCKS}',
        false,1,60,30,'LAB_FIXTURE policy')"""
    # A soft limit at or above the hard one, or without its action, is refused.
    for soft, action in ((D("0.03"), "NO_NEW_ENTRIES"), (D("0.02"), None),
                         (None, "NO_NEW_ENTRIES"), (D("0.02"), "FLATTEN")):
        with owner(er).connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(row, ("LAB_FIXTURE_BAD_LIMITS",))
            conn.execute(limits_sql, ("LAB_FIXTURE_BAD_LIMITS", soft, action))
    with owner(er).connect() as conn:  # A new MANAGED row may carry them, hard limit only.
        conn.execute(row, ("LAB_FIXTURE_HARD_ONLY",))
        conn.execute(limits_sql, ("LAB_FIXTURE_HARD_ONLY", None, None))
    with er.connect() as conn:
        hard_only = load_policy(conn, "LAB_FIXTURE_HARD_ONLY")
    assert hard_only.daily_soft_loss_pct() is None and hard_only.daily_hard_loss_pct() == D(".03")
    for statement in ("UPDATE lab.account_risk_daily_limits SET soft_loss_pct=0.01",
                      "DELETE FROM lab.account_risk_daily_limits"):
        with owner(er).connect() as conn, pytest.raises(psycopg.errors.RaiseException):
            conn.execute(statement)
    risk = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    for repo in (er, risk):  # No application role writes daily limits.
        with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(limits_sql, ("LAB_FIXTURE_HARD_ONLY", None, None))
    assert verify_events(er.export_events())["valid"]


# --- Sizing parity: V4 sizes exactly as V3, and the SQL check agrees --------------------------


def test_v4_slice_sizing_equals_v3_and_matches_the_sql_check(er):
    with er.connect() as conn:
        v3, v4 = load_policy(conn, V3), load_policy(conn, V4)
    risk = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    cases = 0
    with risk.connect() as conn:
        for equity in (D("10000"), D("9876.54321"), D("250")):
            for price in (D("100"), D("64000.5"), D("0.00002345")):
                for fraction in (D("0.02"), D("0.05"), D("0.25")):
                    stop = price * (1 - fraction)
                    for increment in (D("0.0001"), D("1")):
                        args = {"equity": equity, "max_entry": price, "stop": stop,
                                "available": equity, "increment": increment}
                        four = slice_size(v4, v4.terms("CRYPTO"), **args)
                        three = slice_size(v3, v3.terms("CRYPTO"), **args)
                        assert (four.qty, four.planned_risk, four.binding) == (
                            three.qty, three.planned_risk, three.binding)
                        if four.qty:
                            assert slice_sizing_failure(conn, V4, "CRYPTO", equity, four.qty,
                                                        price, stop) is None
                        more = four.policy_qty + increment
                        assert slice_sizing_failure(conn, V4, "CRYPTO", equity, more, price,
                                                    stop) == slice_sizing_failure(
                            conn, V3, "CRYPTO", equity, more, price, stop) is not None
                        cases += 1
        assert slice_sizing_failure(conn, V4, "CRYPTO", EQUITY, D(1), D(100),
                                    D("98.01")) == "STOP_DISTANCE_BELOW_MINIMUM"
    assert cases == 54


# --- The 2% crypto cap: every coin one cluster ------------------------------------------------


def oracle(existing_crypto, budget, cap):
    """Independent restatement of the CRYPTO cap: every open crypto reservation counts."""
    return "MARKET_RISK_CAP" if sum(existing_crypto, D(0)) + budget > cap * EQUITY else None


def test_open_crypto_risk_is_capped_at_two_percent_across_every_sector_and_theme(v4):
    engine, _, _ = v4
    # Four 6%-stop trades at $49.9998 each, each in its own sector and theme, hold $199.9992;
    # a fifth would pass $200, whatever its sector: one cluster.
    outcomes = [enter(v4, f"CL{i}/USD", SIX, classification=(f"SECTOR_{i}", f"T_{i}"))[1]
                for i in range(5)]
    assert [d["reason"] for d in outcomes] == ["RISK_APPROVED"] * 4 + ["MARKET_RISK_CAP"]
    assert outcomes[4]["context"]["binding_constraint"] == "MARKET_RISK_CAP"
    with engine.repo.connect() as conn:
        held = [r["budget"] for r in conn.execute(
            "SELECT budget FROM lab.account_risk_reservations WHERE market='CRYPTO'").fetchall()]
        assert sum(held) == D("199.9992")
        for budget in (D("0.0008"), D("0.0009"), D("49.9998")):
            # SQL/Python parity on the engine's own question: the one SQL check answers as the
            # independent oracle, for an entry in a brand-new sector and theme.
            assert account_risk_failure(conn, V4, "CRYPTO", "SECTOR_NEW", "T_NEW", EQUITY,
                                        budget) == oracle(held, budget, D("0.02"))
        # The same open book under V3: its 5% cap still admits it.
        assert account_risk_failure(conn, V3, "CRYPTO", "SECTOR_NEW", "T_NEW", EQUITY,
                                    D("49.9998")) is None


def test_a_v3_setup_keeps_its_five_percent_cap_beside_v4_reservations(v4, mx):
    engine, _, _ = v4
    for i in range(4):  # $199.9992 of V4 crypto risk.
        assert enter(v4, f"FOUR{i}/USD", SIX)[1]["reason"] == "RISK_APPROVED"
    v3 = engine_under(mx, V3)
    sid, decision = enter(v3, "THREE/USD", SIX)
    assert decision["reason"] == "RISK_APPROVED"  # V3 semantics: under its own 5% cap.
    assert state(v3[0], sid)["risk_policy_id"] == V3
    assert reservation(v3[0], sid)["budget"] == D("49.9998")


# --- Daily limits: the hard halt from the row, the soft no-new-entries limit ------------------


def halt_row(engine):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT * FROM lab.daily_risk_halts").fetchone()


def test_the_v4_hard_limit_halts_at_three_percent_from_the_policy_row(v4):
    from tests.test_position_monitor import opened

    engine, venue, _ = v4
    held = opened(v4, "BTC/USD")
    venue.equity = "9701"
    engine.manage(held, observation(v4))
    assert halt_row(engine) is None and not state(engine, held).get("exit_requested")
    venue.equity = "9700"  # Exactly 3% below the day's starting equity.
    engine.manage(held, observation(v4))
    row = halt_row(engine)
    assert row["threshold"] == D("-300") and state(engine, held)["exit_requested"] == (
        "DAILY_RISK_HALT")


def test_a_v3_engine_keeps_the_literal_three_percent_halt(v3):
    from tests.test_position_monitor import opened

    engine, venue, _ = v3
    held = opened(v3, "BTC/USD")
    venue.equity = "9800"  # The V4 soft limit means nothing to a V3 engine.
    engine.manage(held, observation(v3))
    assert halt_row(engine) is None and rows(engine, SOFT_LIMIT_EVENT) == []
    venue.equity = "9700"
    engine.manage(held, observation(v3))
    assert halt_row(engine)["threshold"] == D("-300")


def test_the_soft_limit_holds_new_entries_and_open_positions_keep_their_protection(v4):
    from tests.test_position_monitor import opened

    engine, venue, _ = v4
    held = opened(v4, "BTC/USD")
    [stop] = venue.orders_of("sell", "stop_limit")
    watching = engine.admit(packet(v4, "ETH/USD"))
    venue.equity = "9800"  # 2% below the day's starting equity: realized plus unrealized.
    engine.manage(held, observation(v4))  # The protection pass latches the soft limit.
    [latch] = rows(engine, SOFT_LIMIT_EVENT)
    day = venue.now.astimezone(NY).date()
    assert latch["setup_id"] is None
    assert latch["idempotency_key"] == f"daily-soft-loss-limit:{V4}:{day.isoformat()}"
    assert {k: latch["body"][k] for k in ("reason", "action", "risk_policy_id", "protection")} == {
        "reason": "DAILY_SOFT_LOSS_LIMIT", "action": "NO_NEW_ENTRIES", "risk_policy_id": V4,
        "protection": "UNCHANGED"}
    assert D(latch["body"]["threshold"]) == D("-200")
    assert halt_row(engine) is None
    assert stop["status"] == "new" and not state(engine, held).get("exit_requested")
    calls = len(venue.calls)
    assert engine.observe_trigger(watching, observation(v4)) is None
    assert engine.observe_trigger(watching, observation(v4)) is None  # The same minute.
    assert len(venue.calls) == calls  # Latched: no broker read, let alone an order.
    assert state(engine, watching)["state"] == "WATCHING" and decisions(engine, watching) == []
    [wait] = rows(engine, SOFT_LIMIT_WAIT_EVENT, watching)
    minute = venue.now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    assert wait["idempotency_key"] == f"daily-soft-loss-entry-wait:{watching}:{minute}"
    assert (wait["body"]["reason"], wait["body"]["version"]) == ("DAILY_SOFT_LOSS_LIMIT", V4)
    # A recovery the same day does not lift it: the latch holds for the New York day.
    venue.equity = "10100"
    venue.now += timedelta(minutes=1)
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(watching, observation(v4)) is None
    assert len(rows(engine, SOFT_LIMIT_WAIT_EVENT, watching)) == 2
    engine.manage(held, observation(v4))
    assert stop["status"] == "new" and not buys(venue, "ETH/USD")
    assert len(rows(engine, SOFT_LIMIT_EVENT)) == 1
    # The hard limit still cancels and flattens.
    venue.equity = "9700"
    engine.manage(held, observation(v4))
    assert state(engine, held)["exit_requested"] == "DAILY_RISK_HALT"
    # And past the hard limit a trigger is the terminal DAILY_RISK_HALT, not a soft-limit wait.
    venue.now += timedelta(minutes=1)
    decision = engine.observe_trigger(watching, observation(v4))
    assert (decision["outcome"], decision["reason"]) == ("REJECTED", "DAILY_RISK_HALT")
    assert state(engine, watching)["state"] == "RISK_REJECTED"
    assert len(rows(engine, SOFT_LIMIT_WAIT_EVENT, watching)) == 2
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_trigger_reaching_the_soft_limit_latches_it_and_waits(v4):
    engine, venue, _ = v4
    sid = engine.admit(packet(v4, "ETH/USD"))
    venue.equity = "9799.99"
    assert engine.observe_trigger(sid, observation(v4)) is None  # Read under the lock.
    assert len(rows(engine, SOFT_LIMIT_EVENT)) == 1
    assert len(rows(engine, SOFT_LIMIT_WAIT_EVENT, sid)) == 1
    assert state(engine, sid)["state"] == "WATCHING" and decisions(engine, sid) == []


def test_just_above_the_soft_limit_a_v4_entry_is_approved(v4):
    engine, venue, _ = v4
    sid = engine.admit(packet(v4, "ETH/USD"))
    venue.equity = "9800.01"
    assert engine.observe_trigger(sid, observation(v4))["outcome"] == "APPROVED"
    assert rows(engine, SOFT_LIMIT_EVENT) == []


def test_a_v3_setup_enters_under_its_own_rules_while_v4_setups_wait(v4, mx):
    engine, venue, _ = v4
    v3 = engine_under(mx, V3)
    old = v3[0].admit(packet(mx, "OLD/USD"))
    new = engine.admit(packet(v4, "NEW/USD"))
    venue.equity = "9800"
    assert engine.observe_trigger(new, observation(v4)) is None
    decision = engine.observe_trigger(old, observation(v4))  # Any engine evaluates it as V3.
    assert decision["outcome"] == "APPROVED" and decision["context"]["risk_policy_id"] == V3
    assert len(buys(venue, "OLD/USD")) == 1 and not buys(venue, "NEW/USD")


def test_the_latch_is_per_new_york_day(v4):
    engine, venue, _ = v4
    # The clock starts 30 minutes before the next New York midnight (forward only, so every
    # database-side deadline stays in the future), so the day change below is one hour away
    # whatever the wall clock reads: from just after midnight the jump to 00:30 the next day
    # was almost 24 hours and outlived the setup's 3-hour expiry (jev-b3, 2026-10-03).
    venue.now = ny_midnight(venue.now.astimezone(NY).date() + timedelta(days=1)) - timedelta(
        minutes=30)
    assert engine.reconcile()["clean"]
    sid = engine.admit(selected_setup(v4, "DAY/USD"))
    venue.equity = "9800"
    assert engine.observe_trigger(sid, observation(v4)) is None
    # The next New York day: its reconciliation records a new day-start equity, and the
    # previous day's latch no longer applies.
    day = venue.now.astimezone(NY).date()
    venue.now += timedelta(hours=1)
    assert venue.now.astimezone(NY).date() == day + timedelta(days=1)
    venue.equity = "9800"
    assert engine.reconcile()["clean"]
    with engine.repo.connect() as conn:
        base = conn.execute("SELECT day_start_equity FROM lab.risk_sessions "
                            "WHERE session_date=%s", (day + timedelta(days=1),)).fetchone()
    assert base is not None
    venue.equity = str(base["day_start_equity"])
    assert engine.observe_trigger(sid, observation(v4))["outcome"] == "APPROVED"


def selected_setup(mx, symbol):
    from tests.test_capacity_policy import selected

    return selected(mx, symbol, levels={"entry_trigger": "100", "max_entry_price": "100.10",
                                        "stop": "95", "target": "111"},
                    classification=("CRYPTO", "T_" + symbol))


# --- Migration 026 on a disposable cluster at schema 25 ---------------------------------------


def assert_migration_026_appended(owner_url, head_before):
    """Exactly migration 026's three audit events follow ``head_before``, hash-chained."""
    with psycopg.connect(owner_url) as conn:
        start = conn.execute("SELECT coalesce((SELECT seq FROM lab.trade_events "
                             "WHERE event_hash=%s),0)", (head_before,)).fetchone()[0]
        found = conn.execute(
            """SELECT event_type,previous_hash,event_hash,event_body,payload_json::text
            FROM lab.trade_events WHERE seq>%s ORDER BY seq""", (start,)).fetchall()
    assert len(found) == len(V4_AUDIT_EVENTS)
    previous = head_before
    for row, (kind, audited) in zip(found, V4_AUDIT_EVENTS, strict=True):
        event_type, previous_hash, event_hash, body, payload = row
        assert event_type == "SYSTEM_EVENT"
        assert json.loads(payload, parse_float=D, parse_int=D) == {"kind": kind, "row": audited}
        assert previous_hash == previous
        assert event_hash == hashlib.sha256((previous_hash + body).encode()).hexdigest()
        previous = event_hash
    return previous


def apply_migration_026(root):
    """Apply 026 as the owner to a disposable cluster at schema 25 and assert exactly its
    three audit events; returns the new audit head."""
    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 25
        conn.execute(MIGRATION_026.read_text())
    return assert_migration_026_appended(owner_url, head)


def test_migration_026_applies_to_a_schema_25_ledger_with_exactly_its_three_rows():
    """The owner's step 25 -> 26 on a disposable cluster: three audited inserts, every earlier
    policy row byte for byte, the application roles accept the migrated ledger."""
    import tempfile
    from pathlib import Path

    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.config import SCHEMA_VERSION
    from tests.test_operator_controls import start_cluster_at

    assert SCHEMA_VERSION == 31
    with tempfile.TemporaryDirectory(prefix="catalyst-026-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 25)
            owner_url = localdb.connection_url(root, "lab_owner")
            with psycopg.connect(owner_url) as conn:
                before = conn.execute("""SELECT policy_id,to_jsonb(p)::text AS row
                    FROM lab.account_risk_policies p ORDER BY policy_id""").fetchall()
            apply_migration_026(root)
            with psycopg.connect(owner_url) as conn:
                after = conn.execute("""SELECT policy_id,to_jsonb(p)::text AS row
                    FROM lab.account_risk_policies p WHERE policy_id<>%s ORDER BY policy_id""",
                                     (V4,)).fetchall()
                version = conn.execute("SELECT max(version) FROM lab.schema_migrations"
                                       ).fetchone()[0]
            assert after == before and version == 26
            # This release's schema is 31: 027 (the planned-stop guard), 028 and 029 (public page
            # views) and 030 (JEV_TOP_K_SELECTION_V3) add no row; 031 (plugin-c3) its four.
            from tests.test_selection_topk_v3 import apply_migrations_after_027
            from tests.test_trade_plan_migration import apply_migration_027

            apply_migration_027(root)
            apply_migrations_after_027(root)
            Repository(localdb.connection_url(root)).check_role()
            risk = RiskRepository(localdb.connection_url(root, "catalyst_risk"))
            risk.check_role()
            with risk.connect() as conn:
                assert load_policy(conn, V4, engine="MANAGED").daily_soft_loss_pct() == D("0.02")
            assert verify_events(Repository(owner_url).export_events())["valid"]
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
