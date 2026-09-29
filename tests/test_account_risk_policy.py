"""One SQL source of truth for account risk (plan 2.1). Disposable PostgreSQL, fake broker only.

LAB_FIXTURE policy rows are inserted as the database owner, the way an owner step would; the
seeded rows come from migration 016 itself.
"""

import json
import tempfile
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab.account_risk import (
    FROZEN_V1_POLICY_ID,
    LEGACY_MANAGED_POLICY_ID,
    MANAGED_RISK_V2_POLICY_ID,
    MANAGED_RISK_V3_POLICY_ID,
    account_risk_failure,
    load_policy,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.domain import Candidate
from catalyst_lab.execution import bracket, system_event
from catalyst_lab.market import NY
from catalyst_lab.repository import Repository
from catalyst_lab.risk import RiskEngine, RiskPolicy
from tests.test_cross_engine_attempts import legacy_candidate
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet

EQUITY = D("10000")
S_NEW, T_NEW = "SECTOR_NEW", "THEME_NEW"


def owner(er):
    return Repository(er.database_url.replace("user=catalyst_app", "user=lab_owner"))


def add_policy(er, policy_id, *, sector=1, theme=1, risk="0.01", cap="0.03",
               caps=None, limited=("US_STOCKS", "CRYPTO"), cooldown=None, arm=0,
               multiple="1"):
    """An owner step inserting a LAB_FIXTURE policy row into the disposable ledger."""
    caps = caps if caps is not None else {"US_STOCKS": "0.025", "CRYPTO": "0.03"}
    with owner(er).connect() as conn:
        conn.execute(
            """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
            market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
            intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
            owner_ruling_ref) VALUES(%s,'MANAGED',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            'LAB_FIXTURE disposable policy')""",
            (policy_id, D(risk), D(cap), Jsonb({k: json.loads(v) for k, v in caps.items()}),
             sector, theme, list(limited), D(multiple) > 1, D(multiple), cooldown, arm),
        )
    return policy_id


def test_seeded_policy_rows_carry_today_and_the_approved_v2_numbers(er):
    with er.connect() as conn:
        v1 = load_policy(conn, FROZEN_V1_POLICY_ID, engine="FROZEN_V1")
        legacy = load_policy(conn, LEGACY_MANAGED_POLICY_ID, engine="MANAGED")
        v2 = load_policy(conn, MANAGED_RISK_V2_POLICY_ID, engine="MANAGED")
        count = conn.execute("SELECT count(*) AS n FROM lab.account_risk_policies").fetchone()
    # Migration 022 adds JEV_MANAGED_RISK_V3 (tests/test_crypto_size_hold.py); the three seeded
    # here carry no market terms, so every earlier rule keeps its fixed budget.
    assert count["n"] == 4
    assert v1.market_terms == legacy.market_terms == v2.market_terms == {}
    assert (v1.risk_pct, v1.account_cap_pct, v1.max_per_sector, v1.max_per_theme) == (
        D("0.01"), D("0.02"), 1, 1)
    assert v1.market_caps == {"US_STOCKS": D("0.02")} and not v1.leverage_allowed
    assert v1.intraday_buying_power_multiple == 1 and v1.capacity_cooldown_seconds is None
    assert (legacy.risk_pct, legacy.account_cap_pct, legacy.max_per_sector,
            legacy.max_per_theme) == (D("0.01"), D("0.02"), 1, 1)
    assert legacy.sector_limited_markets == ("US_STOCKS", "CRYPTO")
    assert legacy.fixed_exit_arm_pct == 0 and legacy.capacity_cooldown_seconds is None
    # JEV_MANAGED_RISK_V2 (CONTRACT-RESOLUTIONS.md, 2026-09-24).
    assert (v2.risk_pct, v2.account_cap_pct) == (D("0.005"), D("0.05"))
    assert v2.market_caps == {"US_STOCKS": D("0.03"), "CRYPTO": D("0.02"), "FOREX": D(0)}
    assert (v2.max_per_sector, v2.max_per_theme) == (2, 1)
    assert v2.sector_limited_markets == ("US_STOCKS",)
    assert v2.leverage_allowed and v2.intraday_buying_power_multiple == 2
    assert v2.capacity_cooldown_seconds == 60 and v2.fixed_exit_arm_pct == 30
    assert v2.owner_ruling_ref == "CONTRACT-RESOLUTIONS 2026-09-24"
    with pytest.raises(ValueError, match="RISK_POLICY_ENGINE_MISMATCH"), er.connect() as conn:
        load_policy(conn, FROZEN_V1_POLICY_ID, engine="MANAGED")
    with pytest.raises(ValueError, match="RISK_POLICY_UNKNOWN"), er.connect() as conn:
        load_policy(conn, "NO_SUCH_POLICY")


def test_deploy_example_names_the_seeded_v3_managed_row(er):
    """Parity between the launch example and the database rows (no numbers live in JSON).
    The example names JEV_MANAGED_RISK_V3 since migration 022 (package crypto-size-hold)."""
    example = json.loads((Path(__file__).resolve().parents[1] / "deploy" /
                          "private-paper.example.json").read_text())

    def find(node):
        if isinstance(node, dict):
            if "MANAGED_RISK_POLICY_ID" in node:
                return node["MANAGED_RISK_POLICY_ID"]
            return next((v for v in map(find, node.values()) if v is not None), None)
        return None

    policy_id = find(example)
    assert policy_id == MANAGED_RISK_V3_POLICY_ID
    with er.connect() as conn:
        assert load_policy(conn, policy_id, engine="MANAGED").policy_id == policy_id


def test_policy_rows_are_owner_only_immutable_audited_and_v1_is_frozen(er):
    risk = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    for repo in (er, risk):
        with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO lab.account_risk_policies(policy_id) VALUES('X_Y_Z')")
    with owner(er).connect() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE lab.account_risk_policies SET risk_pct=0.02")
    with owner(er).connect() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM lab.account_risk_policies")
    frozen = """INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,
        market_caps,max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
        intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,
        owner_ruling_ref) VALUES('LAB_FIXTURE_V1_COPY','FROZEN_V1',%s,0.02,'{"US_STOCKS":0.02}',
        %s,1,'{US_STOCKS}',false,1,NULL,0,'LAB_FIXTURE')"""
    for risk_pct, sector in ((D("0.02"), 1), (D("0.01"), 2)):
        with owner(er).connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(frozen, (risk_pct, sector))
    for caps in ({"US_STOCKS": "0.04"}, {"OPTIONS": "0.01"}, {}):
        with pytest.raises(psycopg.errors.CheckViolation):
            add_policy(er, "LAB_FIXTURE_BAD_CAPS", caps=caps)
    with pytest.raises(psycopg.errors.CheckViolation):  # Leverage flag must match the multiple.
        with owner(er).connect() as conn:
            conn.execute(frozen.replace("FROZEN_V1", "MANAGED").replace("false,1", "true,1"),
                         (D("0.01"), 1))
    with owner(er).connect() as conn:
        rows = conn.execute(
            """SELECT count(*) AS n, count(*) FILTER (WHERE lab.research_audit_matches(
            'ACCOUNT_RISK_POLICIES',to_jsonb(p),p.event_seq)) AS ok
            FROM lab.account_risk_policies p"""
        ).fetchone()
    assert rows["n"] == rows["ok"] == 4  # The three 016 seeds and 022's JEV_MANAGED_RISK_V3.
    assert verify_events(er.export_events())["valid"]


def test_v1_reservation_trigger_keeps_the_frozen_equality_checks_verbatim(er):
    """005's frozen sizing/request equality block (lines 109-135) is reproduced byte for byte;
    only its correlation/cap clause now asks lab.account_risk_failure."""
    source = (Path(localdb.__file__).with_name("migrations") / "005_risk_engine.sql").read_text()
    frozen = "\n".join(source.splitlines()[108:135])
    assert frozen.startswith(" PERFORM pg_advisory_xact_lock(719172026);")
    assert frozen.endswith("THEN RAISE EXCEPTION 'risk reservation does not match frozen sizing "
                           "and request'; END IF;")
    stamp = "\n".join(
        (Path(localdb.__file__).with_name("migrations") / "006_frozen_risk_rulings.sql")
        .read_text().splitlines()[83:89]
    )
    with owner(er).connect() as conn:
        enforce = conn.execute(
            "SELECT prosrc FROM pg_proc WHERE oid='lab.enforce_risk_reservation'::regproc"
        ).fetchone()["prosrc"]
        stamped = conn.execute(
            "SELECT prosrc FROM pg_proc WHERE oid='lab.stamp_risk_decision'::regproc"
        ).fetchone()["prosrc"]
    assert frozen in enforce and "lab.account_risk_failure('CATALYST_RETEST_V1'" in enforce
    assert stamp in stamped


# --- Exhaustive parity: Python check == trigger outcome == independent oracle ------------


@dataclass(frozen=True)
class Rule:
    policy_id: str
    sector: int
    theme: int
    cap: D
    market_caps: dict
    limited: tuple


V1_RULE = Rule(FROZEN_V1_POLICY_ID, 1, 1, D("0.02"), {"US_STOCKS": D("0.02")}, ("US_STOCKS",))


def fixture_rule(sector, theme):
    return Rule(f"LAB_FIXTURE_S{sector}_T{theme}", sector, theme, D("0.03"),
                {"US_STOCKS": D("0.025"), "CRYPTO": D("0.03")}, ("US_STOCKS", "CRYPTO"))


def oracle(rule, market, existing, budget):
    """Independent restatement of the approved rule: stricter-of correlation, then caps."""
    same_sector = [e for e in existing if e["sector"] == S_NEW]
    same_theme = [e for e in existing if e["theme"] == T_NEW]
    sector_limits = [e["rule"].sector for e in same_sector if e["market"] in e["rule"].limited]
    if market in rule.limited:
        sector_limits.append(rule.sector)
    theme_limits = [e["rule"].theme for e in same_theme] + [rule.theme]
    if (sector_limits and len(same_sector) >= min(sector_limits)) or len(same_theme) >= min(
        theme_limits
    ):
        return "CORRELATION_LIMIT"
    if sum(e["budget"] for e in existing) + budget > rule.cap * EQUITY:
        return "MAX_OPEN_PLANNED_RISK"
    market_total = sum(e["budget"] for e in existing if e["market"] == market)
    if market_total + budget > rule.market_caps.get(market, D(0)) * EQUITY:
        return "MARKET_RISK_CAP"
    return None


class Ledger:
    """Reservations written through the real triggers inside one savepointed transaction."""

    def __init__(self, conn, managed, legacy, pools, er):
        self.conn, self.managed, self.legacy, self.pools, self.er = conn, managed, legacy, pools, er

    def classify(self, ticker, sector, theme):
        event = system_event(self.managed.repo, self.conn, "CLASSIFICATION_IMPORTED",
                             {"ticker": ticker, "source": "LAB_FIXTURE"})
        self.conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                          (event["seq"], ticker, sector, theme, "LAB_FIXTURE"))

    def reserve_v1(self, cid, sector, theme):
        candidate = Candidate.model_validate(self.er.get_candidate(cid)["payload_json"])
        budget = D("0.01") * EQUITY
        distance = candidate.max_entry_price - candidate.stop
        qty = int(budget / distance)
        decision = self.legacy._decision(
            self.conn, candidate_id=cid, action="ENTRY",
            session_date=self.legacy.now().astimezone(NY).date(), equity=EQUITY,
            reason="RISK_APPROVED", approved=True, payload=bracket(candidate, qty, cid),
            method="POST", path="/v2/orders", qty=qty, budget=budget,
            planned=qty * distance, context={"policy": {"max_per_sector": 1,
                                                        "max_per_theme": 1}},
        )
        event = system_event(self.managed.repo, self.conn, "RISK_RESERVED",
                             {"risk_decision_id": str(decision["risk_decision_id"])}, cid)
        self.conn.execute(
            "INSERT INTO lab.risk_reservations VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (cid, decision["risk_decision_id"], budget, qty * distance, qty,
             candidate.max_entry_price, sector, theme, event["seq"]),
        )

    def reserve_managed(self, setup_id, policy_id, sector, theme):
        setup = self.managed.store.setup(self.conn, setup_id)
        state = self.managed.store.state(self.conn, setup_id)
        m = D(setup["record_json"]["levels"]["max_entry_price"])
        s = D(setup["record_json"]["levels"]["stop"])
        budget = D("0.01") * EQUITY
        qty = int(budget / (m - s))
        decision = self.managed._decision(
            self.conn, setup, state, "ENTRY",
            {"symbol": setup["symbol"], "qty": str(qty), "client_order_id": uuid4().hex},
            {"risk_policy_id": policy_id, "venue": "ALPACA_PAPER"}, equity=EQUITY,
        )
        self.conn.execute(
            """INSERT INTO lab.managed_reservations(setup_id,decision_id,budget,planned_risk,
            qty,max_entry,sector,theme) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (setup_id, decision["decision_id"], budget, qty * (m - s), qty, m, sector, theme),
        )

    def attempt(self, write):
        """The trigger outcome of one reservation insert; always rolled back."""
        self.conn.execute("SAVEPOINT attempt")
        try:
            write()
            outcome = None
        except psycopg.errors.RaiseException as exc:
            outcome = exc.diag.message_primary
        self.conn.execute("ROLLBACK TO SAVEPOINT attempt")
        return outcome


def test_exhaustive_account_risk_parity_across_engines_overlaps_and_limits(mx, er, raw,
                                                                          evidence, policy):
    engine, venue, _ = mx
    rules = {(s, t): fixture_rule(s, t) for s in (1, 2) for t in (1, 2)}
    for rule in rules.values():
        add_policy(er, rule.policy_id, sector=rule.sector, theme=rule.theme)
    pools = {
        "V1": [legacy_candidate(mx, er, raw, evidence, policy, symbol=f"PARV{i}")[0]
               for i in range(4)],
        "US_STOCKS": [engine.admit(packet(mx, f"PARU{i}")) for i in range(4)],
        "CRYPTO": [engine.admit(packet(mx, f"PARC{i}/USD")) for i in range(4)],
    }
    legacy = RiskEngine(engine.repo, None, RiskPolicy(), ready=lambda: True,
                        clock=lambda: venue.now)
    # Existing reservations: frozen V1, or managed under the strictest and loosest fixtures.
    kinds = [("V1", V1_RULE, "US_STOCKS"), ("M11", rules[1, 1], "US_STOCKS"),
             ("M22", rules[2, 2], "US_STOCKS"), ("M22", rules[2, 2], "CRYPTO")]
    options = [(kind, overlap) for kind in kinds
               for overlap in ("NONE", "SECTOR", "THEME", "BOTH")]
    entries = [("V1", V1_RULE, "US_STOCKS")] + [
        ("MANAGED", rule, "US_STOCKS") for rule in rules.values()
    ] + [("MANAGED", rules[2, 2], "CRYPTO")]
    results = {"checks": 0, "reachable": 0, "unreachable": 0, "outcomes": {}}
    with engine.store.transaction() as conn:
        ledger = Ledger(conn, engine, legacy, pools, er)
        new_v1, new_us, new_crypto = pools["V1"][3], pools["US_STOCKS"][3], pools["CRYPTO"][3]
        for ticker in (f"PARV{3}", "PARU3", "PARC3/USD"):
            ledger.classify(ticker, S_NEW, T_NEW)

        def check(existing):
            for label, rule, market in entries:
                budget = D("0.01") * EQUITY
                expected = oracle(rule, market, existing, budget)
                python = account_risk_failure(conn, rule.policy_id, market, S_NEW, T_NEW,
                                              EQUITY, budget)
                if label == "V1":
                    trigger = ledger.attempt(lambda: ledger.reserve_v1(new_v1, S_NEW, T_NEW))
                else:
                    setup = new_us if market == "US_STOCKS" else new_crypto
                    trigger = ledger.attempt(
                        lambda s=setup, r=rule: ledger.reserve_managed(s, r.policy_id,
                                                                       S_NEW, T_NEW))
                assert python == expected == trigger, (label, rule, market, existing)
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
                ticker = f"PARV{used[pool]}" if pool == "V1" else (
                    f"PARU{used[pool]}" if pool == "US_STOCKS" else f"PARC{used[pool]}/USD")
                sector = S_NEW if overlap in {"SECTOR", "BOTH"} else "SECTOR_" + ticker
                theme = T_NEW if overlap in {"THEME", "BOTH"} else "THEME_" + ticker
                depth = f"node{len(existing)}"
                conn.execute(f"SAVEPOINT {depth}")
                ledger.classify(ticker, sector, theme)

                def write(kind=kind, item=item, rule=rule, sector=sector, theme=theme):
                    if kind == "V1":
                        ledger.reserve_v1(item, sector, theme)
                    else:
                        ledger.reserve_managed(item, rule.policy_id, sector, theme)

                outcome = ledger.attempt(write)
                if outcome is None:  # Reachable through the real triggers: explore below it.
                    write()
                    results["reachable"] += 1
                    explore(index, existing + [{"rule": rule, "market": market, "sector": sector,
                                                "theme": theme, "budget": D("0.01") * EQUITY}],
                            {**used, pool: used[pool] + 1})
                else:
                    results["unreachable"] += 1
                conn.execute(f"ROLLBACK TO SAVEPOINT {depth}")

        explore(0, [], {"V1": 0, "US_STOCKS": 0, "CRYPTO": 0})
        conn.rollback()
    print(f"\nACCOUNT RISK PARITY: {results}")
    assert results["checks"] >= 1000
    assert set(results["outcomes"]) == {
        None, "CORRELATION_LIMIT", "MAX_OPEN_PLANNED_RISK", "MARKET_RISK_CAP"
    }
    with engine.repo.connect() as conn:  # Nothing from the exploration was committed.
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()
    assert verify_events(engine.repo.export_events())["valid"]


# --- Engine-level regressions ------------------------------------------------------------


def test_sector_two_regression_managed_reservation_makes_v1_reject_not_raise(
    mx, er, raw, evidence, policy
):
    """Before 016 a V1 limit of 2 approved an entry the trigger then refused, leaving the
    candidate stuck in TRIGGER_CONFIRMED. Now startup refuses the limit, and V1 asks the same
    SQL function: a managed reservation in the sector is a recorded CORRELATION_LIMIT."""
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "SAMESECT", sector="SHARED_SECTOR"))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    cid, legacy = legacy_candidate(mx, er, raw, evidence, policy, symbol="OTHERTKR")
    legacy.classify("OTHERTKR", "SHARED_SECTOR", "DIFFERENT_THEME", "LAB_FIXTURE")
    with pytest.raises(ValueError, match="V1_RISK_POLICY_MISMATCH"):
        RiskEngine(engine.repo, legacy.client, RiskPolicy(5, "BROKER_PREVIOUS_CLOSE", 2, 1),
                   ready=lambda: True, clock=lambda: venue.now)
    decision = legacy.authorize_entry(cid)  # No exception escapes.
    assert decision["decision"] == "REJECTED" and decision["reason"] == "CORRELATION_LIMIT"
    assert decision["context_json"]["binding_constraint"] == "CORRELATION"
    assert er.get_candidate(cid)["state"] == "RISK_REJECTED"
    with er.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM lab.account_risk_reservations"
        ).fetchone()["n"] == 1


def test_stricter_of_keeps_v1_one_per_sector_while_v1_occupies_it(mx, er, raw, evidence,
                                                                  policy):
    engine, venue, _ = mx
    looser = add_policy(er, "LAB_FIXTURE_TWO_PER_SECTOR", sector=2, theme=2)
    cid, legacy = legacy_candidate(mx, er, raw, evidence, policy, symbol="V1OWNS")
    legacy.classify("V1OWNS", "OCCUPIED", "V1_THEME", "LAB_FIXTURE")
    assert legacy.authorize_entry(cid)["decision"] == "APPROVED"
    managed = type(engine)(engine.repo, engine.broker, policy=engine.policy, clock=engine.now,
                           review_store=engine.review_store, risk_policy_id=looser)
    managed.reconciled_at = engine.reconciled_at
    sid = managed.admit(packet(mx, "MGDTWO"))
    legacy.classify("MGDTWO", "OCCUPIED", "MANAGED_THEME", "LAB_FIXTURE")
    decision = managed.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "REJECTED" and decision["reason"] == "CORRELATION_LIMIT"
    assert decision["context"]["risk_policy_id"] == looser
    with er.connect() as conn:
        view = conn.execute(
            "SELECT policy_id,market,venue,source FROM lab.account_risk_reservations"
        ).fetchall()
    assert [dict(r) for r in view] == [{"policy_id": "CATALYST_RETEST_V1", "market": "US_STOCKS",
                                        "venue": "ALPACA_PAPER", "source": "FROZEN"}]


def test_legacy_managed_reservation_maps_to_the_seeded_policy_in_the_view(mx):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "ETH/USD"))
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["context"]["risk_policy_id"] == LEGACY_MANAGED_POLICY_ID
    with engine.repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.account_risk_reservations").fetchone()
    assert (row["policy_id"], row["market"], row["venue"], row["source"]) == (
        LEGACY_MANAGED_POLICY_ID, "CRYPTO", "ALPACA_PAPER", "MANAGED")
    assert row["budget"] == D(100)


def test_unknown_policy_input_fails_closed(er):
    risk = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    with risk.connect() as conn:
        assert account_risk_failure(conn, "NO_SUCH_POLICY", "US_STOCKS", "A", "B", EQUITY,
                                    D(100)) == "RISK_POLICY_UNKNOWN"
        assert account_risk_failure(conn, FROZEN_V1_POLICY_ID, "US_STOCKS", "A", "B", EQUITY,
                                    D(100), venue="OANDA_PRACTICE") == \
            "INVALID_ACCOUNT_RISK_INPUT"
        assert account_risk_failure(conn, FROZEN_V1_POLICY_ID, "OPTIONS", "A", "B", EQUITY,
                                    D(100)) == "INVALID_ACCOUNT_RISK_INPUT"
        assert account_risk_failure(conn, FROZEN_V1_POLICY_ID, "US_STOCKS", None, "B", EQUITY,
                                    D(100)) == "CORRELATION_UNKNOWN"
        assert account_risk_failure(conn, FROZEN_V1_POLICY_ID, "US_STOCKS", "A", "B", EQUITY,
                                    D(0)) == "INVALID_ACCOUNT_RISK_INPUT"
        assert account_risk_failure(conn, LEGACY_MANAGED_POLICY_ID, "FOREX", "A", "B", EQUITY,
                                    D(100)) == "MARKET_RISK_CAP"
        assert account_risk_failure(conn, MANAGED_RISK_V2_POLICY_ID, "FOREX", "A", "B", EQUITY,
                                    D(50)) == "MARKET_RISK_CAP"
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT lab.account_risk_failure('CATALYST_RETEST_V1','ALPACA_PAPER',"
                     "'US_STOCKS','A','B',10000,100)")


# --- Migration: a populated schema-15 ledger migrated to 016 -----------------------------


def test_populated_schema15_ledger_migrates_with_audit_intact():
    from tests.test_operator_controls import audit_state, populate_schema14, start_cluster_at

    with tempfile.TemporaryDirectory(prefix="catalyst-016-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 15)
            populate_schema14(root)
            v1_seq, managed_setup = populate_reservations(root)
            before, audited, halts, version = audit_state(root)
            assert version == 15 and before["valid"]
            assert all(n == ok for n, ok in audited.values())
            migration = Path(localdb.__file__).with_name("migrations") / \
                "016_account_risk_policy.sql"
            with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
                conn.execute(migration.read_text())
            after, audited_after, halts_after, version_after = audit_state(root)
            assert version_after == 16 and after["valid"] and halts_after == halts
            # Every historical audited row still verifies; only the three seeds were appended.
            assert {t: v for t, v in audited_after.items() if t in audited} == audited
            assert audited_after["account_risk_policies"] == (3, 3)
            assert audited_after["ledger_account_binding"] == (0, 0)
            assert after["event_count"] == before["event_count"] + 3
            owner_repo = Repository(localdb.connection_url(root, "lab_owner"))
            with owner_repo.connect() as conn:
                rows = conn.execute("""SELECT reference_id,source,market,venue,policy_id
                    FROM lab.account_risk_reservations ORDER BY source""").fetchall()
            assert [(r["source"], r["market"], r["venue"], r["policy_id"]) for r in rows] == [
                ("FROZEN", "US_STOCKS", "ALPACA_PAPER", "CATALYST_RETEST_V1"),
                ("MANAGED", "US_STOCKS", "ALPACA_PAPER", "MUSE_JEV_MANAGED_TEST_V1"),
            ]
            assert {str(r["reference_id"]) for r in rows} >= {str(managed_setup)}
            assert v1_seq is not None
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def populate_reservations(root):
    """A frozen and a legacy managed reservation written through the schema-15 triggers."""
    from tests.conftest import NOW

    risk = Repository(localdb.connection_url(root, "catalyst_risk"))
    app = Repository(localdb.connection_url(root))
    cid = uuid4()
    levels = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
              "target": "111"}
    body = {"strategy_version": "CATALYST_RETEST_V1", "signal_id": "fixture-016-" + cid.hex[:8],
            "market": "US", "ticker": "MIGV", "catalyst": "EARNINGS",
            "thesis": "Fixture only", "disproof": "Loss of level", **levels}
    with app.connect() as conn:
        conn.execute(
            """INSERT INTO lab.candidates(candidate_id,payload_json,strategy_version,signal_id,
            ticker,session_date) VALUES(%s,%s,'CATALYST_RETEST_V1',%s,'MIGV',%s)""",
            (cid, Jsonb(body), body["signal_id"], NOW.date()),
        )
    with risk.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        for ticker in ("MIGV", "MIGM"):
            event = system_event(risk, conn, "CLASSIFICATION_IMPORTED", {"ticker": ticker})
            conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                         (event["seq"], ticker, "S_" + ticker, "T_" + ticker, "LAB_FIXTURE"))
        candidate = Candidate.model_validate(body)
        qty = 19
        decision_event = system_event(risk, conn, "RISK_DECISION", {"fixture": True}, cid)
        decision = conn.execute(
            """INSERT INTO lab.risk_decisions(candidate_id,strategy_version,equity,risk_pct,
            risk_dollars,planned_risk,theme_exposure_before,theme_exposure_after,computed_qty,
            decision,reason,event_id,action,session_date,expires_at,http_method,path,
            payload_json,context_json) VALUES(%s,'CATALYST_RETEST_V1',10000,0.01,100,%s,0,100,
            %s,'APPROVED','RISK_APPROVED',%s,'ENTRY',%s,clock_timestamp(),'POST','/v2/orders',
            %s,'{}') RETURNING *""",
            (cid, qty * D("5.10"), qty, decision_event["event_id"], NOW.date(),
             Jsonb(bracket(candidate, qty, cid))),
        ).fetchone()
        reserved = system_event(risk, conn, "RISK_RESERVED", {"fixture": True}, cid)
        conn.execute(
            "INSERT INTO lab.risk_reservations VALUES(%s,%s,100,%s,%s,100.10,%s,%s,%s)",
            (cid, decision["risk_decision_id"], qty * D("5.10"), qty, "S_MIGV", "T_MIGV",
             reserved["seq"]),
        )
        receipt = conn.execute("SELECT receipt_id FROM lab.jev_receipts LIMIT 1").fetchone()
        setup = uuid4()
        conn.execute(
            """INSERT INTO lab.managed_setups(setup_id,cycle_id,revision,symbol,market,
            strategy_version,policy_id,cohort,receipt_id,evidence_hash,expires_at,record_json)
            VALUES(%s,%s,1,'MIGM','US_STOCKS','CATALYST_RETEST_V1','MUSE_JEV_MANAGED_TEST_V1',
            'JEV_MANAGED_PAPER_V1',%s,'fixture',clock_timestamp()+interval '1 hour',%s)""",
            (setup, uuid4(), receipt["receipt_id"], Jsonb({"levels": levels})),
        )
        managed = conn.execute(
            """INSERT INTO lab.managed_risk_decisions(decision_id,setup_id,action,outcome,reason,
            method,path,payload,context,equity,expires_at) VALUES(%s,%s,'ENTRY','APPROVED',
            'RISK_APPROVED','POST','/v2/orders',%s,'{"state_revision":1}',10000,
            clock_timestamp()+interval '5 seconds') RETURNING *""",
            (uuid4(), setup, Jsonb({"qty": "19", "client_order_id": uuid4().hex})),
        ).fetchone()
        conn.execute(
            """INSERT INTO lab.managed_reservations(setup_id,decision_id,budget,planned_risk,qty,
            max_entry,sector,theme) VALUES(%s,%s,100,%s,19,100.10,'S_MIGM','T_MIGM')""",
            (setup, managed["decision_id"], 19 * D("5.10")),
        )
    return decision["risk_decision_id"], setup
