"""Package plugin-c3: ``JEV_MANAGED_RISK_V5`` (migration 031) = V4 plus a per-strategy open-risk cap
(a mechanical strategy at most 0.5% of equity; research picks unchanged inside the 2% crypto
cluster cap), with SQL/Python parity, V4 unchanged, and migration 031 on a schema-30 ledger.

Fixture evidence only: per-test disposable PostgreSQL databases built from the real migrations,
the fake paper venue. No broker, provider, network or owner-ledger contact.
"""

import hashlib
import json
from decimal import Decimal as D

import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.account_risk import (
    CAPACITY_REASONS,
    MANAGED_RISK_V4_POLICY_ID,
    MANAGED_RISK_V5_POLICY_ID,
    MECHANICAL_SOURCE,
    RESEARCH_SOURCE,
    STRATEGY_RISK_CAP,
    binding_constraint,
    load_policy,
    setup_strategy,
    strategy_cap_failure,
    strategy_open_risk,
    strategy_risk_failure,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.repository import Repository
from tests.maintenance_fixtures import mt as mt  # noqa: F401
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission  # noqa: F401
from tests.test_account_risk_policy import owner
from tests.test_crypto_size_hold import MIGRATIONS
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import observation
from tests.test_strategy_paper import (  # noqa: F401
    SID,
    admit,
    engine_of,
    registry,
    signalled,
)

V4, V5 = MANAGED_RISK_V4_POLICY_ID, MANAGED_RISK_V5_POLICY_ID
EQUITY = D("10000")
MIGRATION_031 = MIGRATIONS / "031_strategy_paper_risk_v5.sql"
MECHANICAL_RECORD = {"selection_policy": "STRATEGY_SIGNAL_SELECTION_V1", "strategy_id": SID}
RESEARCH_RECORD = {"selection_policy": "JEV_TOP_K_SELECTION_V3"}
V5_AUDIT_ROWS = (
    ("ACCOUNT_RISK_POLICIES", {
        "policy_id": V5, "engine": "MANAGED", "risk_pct": D("0.005"),
        "account_cap_pct": D("0.05"),
        "market_caps": {"US_STOCKS": D("0.03"), "CRYPTO": D("0.02"), "FOREX": D(0)},
        "max_per_sector": 2, "max_per_theme": 1, "sector_limited_markets": ["US_STOCKS"],
        "leverage_allowed": True, "intraday_buying_power_multiple": 2,
        "capacity_cooldown_seconds": 60, "fixed_exit_arm_pct": 30,
        "owner_ruling_ref": "TRADING-QUALITY-PLAN 12.3 (V4 numbers kept); plugin-c3"}),
    ("ACCOUNT_RISK_MARKET_TERMS", {
        "policy_id": V5, "market": "CRYPTO", "sizing_method": "EQUITY_SLICE_RISK_CAPPED_V1",
        "notional_pct": D("0.10"), "max_per_theme": 3, "min_stop_fraction": D("0.02"),
        "owner_ruling_ref": "TRADING-QUALITY-PLAN 12.3 (V4 terms kept); plugin-c3"}),
    ("ACCOUNT_RISK_DAILY_LIMITS", {
        "policy_id": V5, "hard_loss_pct": D("0.03"), "hard_action": "CANCEL_AND_FLATTEN",
        "soft_loss_pct": D("0.02"), "soft_action": "NO_NEW_ENTRIES",
        "owner_ruling_ref": "TRADING-QUALITY-PLAN 12.3 (V4 limits kept); plugin-c3"}),
    ("ACCOUNT_RISK_STRATEGY_CAPS", {
        "policy_id": V5, "strategy_source": "MECHANICAL", "cap_pct": D("0.005"),
        "owner_ruling_ref": "TRADING-QUALITY-PLAN 12.3, a mechanical plug-in at most 0.5% open "
                            "risk; plugin-c3"}),
)


def risk_repo(er):  # noqa: F811
    return Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))


# --- The V5 row: V4's numbers plus the strategy cap ---------------------------------------------


def test_v5_is_v4_plus_one_mechanical_strategy_cap_and_v4_is_untouched(er):  # noqa: F811
    with er.connect() as conn:
        v4, v5 = load_policy(conn, V4, engine="MANAGED"), load_policy(conn, V5, engine="MANAGED")
    for name in ("risk_pct", "account_cap_pct", "market_caps", "max_per_sector",
                 "max_per_theme", "sector_limited_markets", "leverage_allowed",
                 "intraday_buying_power_multiple", "capacity_cooldown_seconds",
                 "fixed_exit_arm_pct"):
        assert getattr(v5, name) == getattr(v4, name), name
    t4, t5 = v4.terms("CRYPTO"), v5.terms("CRYPTO")
    assert (t4.sizing_method, t4.notional_pct, t4.max_per_theme, t4.min_stop_fraction) == (
        t5.sizing_method, t5.notional_pct, t5.max_per_theme, t5.min_stop_fraction)
    assert (v5.daily_hard_loss_pct(), v5.daily_soft_loss_pct()) == (
        v4.daily_hard_loss_pct(), v4.daily_soft_loss_pct())
    assert v5.strategy_caps == {"MECHANICAL": D("0.005")} and v4.strategy_caps == {}
    assert v5.strategy_cap(MECHANICAL_SOURCE) == D("0.005")
    assert v5.strategy_cap(RESEARCH_SOURCE) is None
    assert "strategy_caps" not in v4.evidence()  # V4's decision evidence is as before.
    assert v5.evidence()["strategy_caps"] == {"MECHANICAL": D("0.005")}
    with owner(er).connect() as conn:
        audited = conn.execute(
            """SELECT count(*) FILTER (WHERE lab.research_audit_matches(
            'ACCOUNT_RISK_STRATEGY_CAPS',to_jsonb(c),c.event_seq)) AS ok,count(*) AS n
            FROM lab.account_risk_strategy_caps c""").fetchone()
    assert audited["n"] == audited["ok"] == 1
    assert STRATEGY_RISK_CAP in CAPACITY_REASONS
    assert binding_constraint(STRATEGY_RISK_CAP) == "STRATEGY_RISK_CAP"
    assert verify_events(er.export_events())["valid"]


def test_strategy_caps_are_written_only_with_a_new_managed_policy_and_never_changed(er):  # noqa: F811
    caps_sql = """INSERT INTO lab.account_risk_strategy_caps(policy_id,strategy_source,cap_pct,
        owner_ruling_ref) VALUES(%s,%s,0.004,'LAB_FIXTURE caps')"""
    for policy_id, source in ((V4, "MECHANICAL"), (V5, "RESEARCH_REPORT")):
        with owner(er).connect() as conn, pytest.raises(
                psycopg.errors.RaiseException, match="STRATEGY_CAPS_REQUIRE_NEW_MANAGED_POLICY"):
            conn.execute(caps_sql, (policy_id, source))
    row = """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
        market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
        intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
        owner_ruling_ref) VALUES(%s,'MANAGED',0.005,0.05,'{"CRYPTO":0.02}',2,1,'{US_STOCKS}',
        false,1,60,30,'LAB_FIXTURE policy')"""
    for bad in ("0", "0.2"):  # Out of range.
        with owner(er).connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(row, ("LAB_FIXTURE_BAD_CAP",))
            conn.execute(caps_sql.replace("0.004", bad), ("LAB_FIXTURE_BAD_CAP", "MECHANICAL"))
    with owner(er).connect() as conn:
        conn.execute(row, ("LAB_FIXTURE_CAPPED",))
        conn.execute(caps_sql, ("LAB_FIXTURE_CAPPED", "MECHANICAL"))
    for statement in ("UPDATE lab.account_risk_strategy_caps SET cap_pct=0.01",
                      "DELETE FROM lab.account_risk_strategy_caps"):
        with owner(er).connect() as conn, pytest.raises(psycopg.errors.RaiseException):
            conn.execute(statement)
    for repo in (er, risk_repo(er)):  # No application role writes a cap.
        with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(caps_sql, ("LAB_FIXTURE_CAPPED", "RESEARCH_REPORT"))
    assert verify_events(er.export_events())["valid"]


# --- SQL/Python parity ------------------------------------------------------------------------


def test_setup_strategy_matches_the_sql_reading(er):  # noqa: F811
    records = (MECHANICAL_RECORD, RESEARCH_RECORD, {}, {"strategy_id": ""},
               {"strategy_id": "PULLBACK_V1", "selection_policy": "JEV_TOP_K_SELECTION_V2"},
               {"selection_policy": "MANAGED_ENGINEERING_ENROLLMENT_V1"})
    with risk_repo(er).connect() as conn:
        for record in records:
            sql = conn.execute("""SELECT lab.managed_setup_strategy(%s::jsonb) AS s,
                lab.managed_setup_strategy_source(%s::jsonb) AS src""",
                               (json.dumps(record), json.dumps(record))).fetchone()
            assert (sql["s"], sql["src"]) == setup_strategy(record), record


def test_the_strategy_cap_sql_and_python_agree_on_an_empty_book(er):  # noqa: F811
    with er.connect() as conn:
        v4, v5 = load_policy(conn, V4), load_policy(conn, V5)
    cases = 0
    with risk_repo(er).connect() as conn:
        for equity in (D("10000"), D("9876.54321"), D("250")):
            cap = D("0.005") * equity
            for budget in (cap - D("0.0001"), cap, cap + D("0.0001"), cap * 2, D("0.01")):
                for policy, record in ((v5, MECHANICAL_RECORD), (v5, RESEARCH_RECORD),
                                       (v4, MECHANICAL_RECORD)):
                    _, source = setup_strategy(record)
                    held = strategy_open_risk(conn, record)
                    assert held == 0
                    assert strategy_risk_failure(conn, policy.policy_id, record, equity,
                                                 budget) == strategy_cap_failure(
                        policy, source, held, budget, equity), (policy.policy_id, budget)
                    cases += 1
        # Exactly the cap passes; above it is the capacity code; V4 and research never cap.
        assert strategy_risk_failure(conn, V5, MECHANICAL_RECORD, EQUITY, D("50")) is None
        assert strategy_risk_failure(conn, V5, MECHANICAL_RECORD, EQUITY,
                                     D("50.0000001")) == STRATEGY_RISK_CAP
        assert strategy_risk_failure(conn, V4, MECHANICAL_RECORD, EQUITY, D("5000")) is None
        assert strategy_risk_failure(conn, V5, RESEARCH_RECORD, EQUITY, D("5000")) is None
        assert strategy_risk_failure(conn, "NOPE_V1", MECHANICAL_RECORD, EQUITY,
                                     D("1")) == "RISK_POLICY_UNKNOWN"
        assert strategy_risk_failure(conn, V5, MECHANICAL_RECORD, D(0),
                                     D("1")) == "INVALID_ACCOUNT_RISK_INPUT"
    assert cases == 45


@pytest.mark.trade_plan
@pytest.mark.usefixtures("pre_jev_b1_admission")
def test_the_cap_sums_the_strategys_open_reservations_in_both_checks(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    [packet] = signalled(engine, mt, tmp_path)
    sid = admit(engine, mt, packet)
    decision = engine.observe_trigger(sid, observation(mt, trade_price="100.2", bid="100.19",
                                                       ask="100.21"))
    assert decision["outcome"] == "APPROVED"
    with engine.repo.connect() as conn:
        policy = load_policy(conn, V5)
        held = strategy_open_risk(conn, MECHANICAL_RECORD)
        assert held == D(decision["context"]["sizing"]["planned_risk"]) > 0
        room = D("50") - held
        for budget in (room - D("0.01"), room, room + D("0.000001"), D("25")):
            assert strategy_risk_failure(conn, V5, MECHANICAL_RECORD, EQUITY, budget) == (
                strategy_cap_failure(policy, MECHANICAL_SOURCE, held, budget, EQUITY))
        # Another strategy's book and the research picks hold nothing of this strategy's cap.
        other = {**MECHANICAL_RECORD, "strategy_id": "OTHER_V1"}
        assert strategy_open_risk(conn, other) == 0
        assert strategy_risk_failure(conn, V5, other, EQUITY, D("50")) is None
        assert strategy_open_risk(conn, RESEARCH_RECORD) == 0


@pytest.mark.trade_plan
@pytest.mark.usefixtures("pre_jev_b1_admission")
def test_the_reservation_trigger_enforces_the_cap_without_the_engines_check(
        mt, tmp_path, registry, monkeypatch):  # noqa: F811
    from catalyst_lab import managed_execution

    engine = engine_of(mt)
    packets = signalled(engine, mt, tmp_path, symbols=("SOL/USD", "AVAX/USD"))
    sids = [admit(engine, mt, p) for p in packets]
    touch = observation(mt, trade_price="100.2", bid="100.19", ask="100.21")
    assert engine.observe_trigger(sids[0], touch)["outcome"] == "APPROVED"
    # A defect that skipped the engine's own check: the reservation trigger still refuses.
    monkeypatch.setattr(managed_execution, "strategy_risk_failure", lambda *a, **k: None)
    with pytest.raises(psycopg.errors.RaiseException, match=STRATEGY_RISK_CAP):
        engine.observe_trigger(sids[1], observation(mt, trade_price="100.2", bid="100.19",
                                                    ask="100.21"))
    second = packets[1]["symbol"]
    assert [o for o in mt[1].orders.values() if o["symbol"] == second] == []
    assert engine._load(sids[1])[1]["state"] == "WATCHING"  # Nothing was written for it.


# --- Migration 031 on a disposable cluster at schema 30 ----------------------------------------


def apply_migration_031(root):
    """Apply 031 as the owner to a disposable cluster at schema 30 and assert exactly its four
    audit events, hash-chained; returns the new audit head."""
    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        start = conn.execute("SELECT max(seq) FROM lab.trade_events").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 30
        conn.execute(MIGRATION_031.read_text())
    with psycopg.connect(owner_url) as conn:
        found = conn.execute(
            """SELECT event_type,previous_hash,event_hash,event_body,payload_json::text
            FROM lab.trade_events WHERE seq>%s ORDER BY seq""", (start,)).fetchall()
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 31
    assert len(found) == len(V5_AUDIT_ROWS)
    previous = head
    for row, (kind, audited) in zip(found, V5_AUDIT_ROWS, strict=True):
        event_type, previous_hash, event_hash, body, payload = row
        assert event_type == "SYSTEM_EVENT"
        assert json.loads(payload, parse_float=D, parse_int=D) == {"kind": kind, "row": audited}
        assert previous_hash == previous
        assert event_hash == hashlib.sha256((previous_hash + body).encode()).hexdigest()
        previous = event_hash
    return previous


def test_migration_031_applies_to_a_schema_30_ledger_with_exactly_its_four_rows():
    """The owner's step 30 -> 31 on a disposable cluster: four audited inserts, every earlier
    policy row and setup rule unchanged, the application roles accept the migrated ledger."""
    import tempfile
    from pathlib import Path

    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.config import SCHEMA_VERSION
    from tests.test_operator_controls import start_cluster_at

    assert SCHEMA_VERSION == 31
    with tempfile.TemporaryDirectory(prefix="catalyst-031-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 30)
            owner_url = localdb.connection_url(root, "lab_owner")
            with psycopg.connect(owner_url) as conn:
                before = conn.execute("""SELECT policy_id,to_jsonb(p)::text AS row
                    FROM lab.account_risk_policies p ORDER BY policy_id""").fetchall()
            apply_migration_031(root)
            with psycopg.connect(owner_url) as conn:
                after = conn.execute("""SELECT policy_id,to_jsonb(p)::text AS row
                    FROM lab.account_risk_policies p WHERE policy_id<>%s ORDER BY policy_id""",
                                     (V5,)).fetchall()
                checks = conn.execute("""SELECT conname FROM pg_constraint
                    WHERE conrelid='lab.managed_setups'::regclass AND contype='c'
                    AND conname LIKE 'managed_setups_receipt%%'""").fetchall()
            assert after == before
            assert [c[0] for c in checks] == ["managed_setups_receipt_or_receipt_free_policy"]
            Repository(localdb.connection_url(root)).check_role()
            risk = RiskRepository(localdb.connection_url(root, "catalyst_risk"))
            risk.check_role()
            with risk.connect() as conn:
                assert load_policy(conn, V5, engine="MANAGED").strategy_caps == {
                    "MECHANICAL": D("0.005")}
            assert verify_events(Repository(owner_url).export_events())["valid"]
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
