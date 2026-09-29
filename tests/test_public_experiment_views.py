"""Migration 024: the catalyst_public role and the four sanitized dashboard views.

Disposable PostgreSQL only (a private cluster for this module; catalyst_public gets LOGIN here the
way the cloud provisioner grants it in production). Fixture ledgers come from
tests/experiment_fixtures.py; the parity tests read the same rows through the private Python
readers (managed_measurement, pick_outcomes.cycle_picks) as catalyst_app and through the public
views as catalyst_public.
"""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from catalyst_lab import ledger_ops, localdb
from catalyst_lab.execution import halt
from catalyst_lab.experiment_report import read_snapshot
from catalyst_lab.managed_analytics import managed_result_aggregates
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.pick_outcomes import cycle_picks
from tests.experiment_fixtures import (
    FIXTURE_ACCOUNT_MARKER,
    FIXTURE_AGENT_ID,
    LONG_THESIS,
    ExperimentLedger,
    Pick,
    enable_public_login,
    public_url,
    role_url,
)
from tests.maintenance_fixtures import mt as mt  # noqa: F401
from tests.maintenance_fixtures import open_trade, quote
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_localdb_guard import audit_head, disposable_root, migrations_through

PUBLIC_VIEWS = {"public_dashboard_status", "public_dashboard_trades", "public_dashboard_runs",
                "public_dashboard_decisions"}
PUBLIC_FUNCTIONS = {"lab.public_decimal(jsonb)", "lab.public_timestamp(jsonb)",
                    "lab.public_line(jsonb)"}
MIGRATION = Path(localdb.__file__).with_name("migrations") / "024_public_experiment.sql"
SLOT = datetime(2026, 9, 20, 12, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def public_login(pristine_cluster):  # noqa: F811
    enable_public_login(pristine_cluster)


@pytest.fixture
def ledger(er):  # noqa: F811
    return ExperimentLedger(er.database_url)


def public(er):  # noqa: F811
    return psycopg.connect(public_url(er.database_url), row_factory=dict_row,
                           options="-c timezone=UTC")


def owner(er):  # noqa: F811
    return psycopg.connect(role_url(er.database_url, "lab_owner"), row_factory=dict_row)


def rows(er, view, order="1", where="true"):  # noqa: F811
    with public(er) as conn:
        return conn.execute(f"SELECT * FROM lab.{view} WHERE {where} ORDER BY {order}").fetchall()


def everything_public(er):  # noqa: F811
    """Every row of every public view, as one JSON text."""
    with public(er) as conn:
        return json.dumps({v: conn.execute(f"SELECT * FROM lab.{v}").fetchall()
                           for v in sorted(PUBLIC_VIEWS)}, default=str)


# --- The role ----------------------------------------------------------------------------------


def test_catalyst_public_reads_exactly_the_public_views_and_nothing_else(er):  # noqa: F811
    with owner(er) as conn:
        relations = conn.execute("""SELECT c.oid, c.relname::text AS name, c.relkind::text AS kind,
            has_table_privilege('catalyst_public', c.oid, 'SELECT') AS can_select,
            has_table_privilege('catalyst_public', c.oid,
             'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS can_write
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog','information_schema')
              AND n.nspname !~ '^pg_toast' AND c.relkind IN ('r','v','m','p','f','S')""").fetchall()
        functions = {r["sig"] for r in conn.execute("""SELECT p.oid::regprocedure::text AS sig
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='lab'
            AND has_function_privilege('catalyst_public', p.oid, 'EXECUTE')""").fetchall()}
        role = conn.execute("""SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication,
            rolbypassrls, rolconnlimit,
            has_schema_privilege('catalyst_public','lab','USAGE') AS lab_usage,
            has_schema_privilege('catalyst_public','lab','CREATE') AS lab_create,
            has_schema_privilege('catalyst_public','public','CREATE') AS public_create,
            (SELECT count(*) FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid
             WHERE m.member=r.oid) AS memberships,
            (SELECT count(*) FROM pg_class c WHERE c.relowner=r.oid) +
            (SELECT count(*) FROM pg_proc p WHERE p.proowner=r.oid) +
            (SELECT count(*) FROM pg_namespace s WHERE s.nspowner=r.oid) AS owned
            FROM pg_roles r WHERE r.rolname='catalyst_public'""").fetchone()
    readable = {r["name"] for r in relations if r["can_select"]}
    assert readable == PUBLIC_VIEWS
    assert all(r["kind"] == "v" for r in relations if r["can_select"])
    assert not [r["name"] for r in relations if r["can_write"]]
    assert functions == PUBLIC_FUNCTIONS
    assert role == {"rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
                    "rolreplication": False, "rolbypassrls": False, "rolconnlimit": 8,
                    "lab_usage": True, "lab_create": False, "public_create": False,
                    "memberships": 0, "owned": 0}


@pytest.mark.parametrize("relation", [
    "lab.managed_events", "lab.managed_setups", "lab.managed_fills", "lab.managed_risk_decisions",
    "lab.trade_events", "lab.schema_migrations", "lab.account_risk_policies",
    "lab.ledger_account_binding", "lab.jev_receipts", "lab.jev_requests", "lab.managed_states",
    "lab.execution_halts", "lab.experiment_trade_facts", "lab.experiment_fill_costs",
    "lab.experiment_pick_facts", "lab.experiment_scope_setups", "lab.experiment_run_facts",
    "lab.public_trades", "lab.strategy_trades",
])
def test_catalyst_public_is_refused_every_other_relation(er, relation):  # noqa: F811
    with public(er) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"SELECT 1 FROM {relation} LIMIT 1")


def test_catalyst_public_cannot_write_or_call_private_functions(er):  # noqa: F811
    # 42501 insufficient_privilege; a write into a view is refused as 55000 (not updatable)
    # before the privilege check is even reached. Either way nothing is written.
    refused = {"42501", "55000"}
    with public(er) as conn:
        for statement in (
            "INSERT INTO lab.public_dashboard_trades(trade_no) VALUES (1)",
            "DELETE FROM lab.public_dashboard_runs",
            "UPDATE lab.public_dashboard_decisions SET symbol='X'",
            "SELECT lab.account_risk_failure('JEV_MANAGED_RISK_V3','ALPACA_PAPER','CRYPTO',"
            "'CRYPTO','MEME',10000,10)",
            "SELECT lab.operator_pause('fixture reason that is long enough')",
            "CREATE TABLE lab.fixture_probe(id int)",
            "CREATE TABLE public.fixture_probe(id int)",
        ):
            with pytest.raises(psycopg.Error) as error:
                conn.execute(statement)
            assert error.value.sqlstate in refused, statement
            conn.rollback()


def test_the_migration_creates_a_nologin_role_and_is_ddl_only():
    text = "\n".join(line.split("--")[0] for line in MIGRATION.read_text().splitlines())
    assert "CREATE ROLE catalyst_public NOLOGIN" in text
    assert "PASSWORD" not in text.upper()
    assert "catalyst_public" in ledger_ops.expected_roles(24)
    assert "catalyst_public" not in ledger_ops.expected_roles(23)
    statements = [s.strip().upper() for s in text.split(";")]
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE"))]
    assert writes == ["INSERT INTO LAB.SCHEMA_MIGRATIONS(VERSION) VALUES(24)"]
    assert "SECURITY DEFINER" not in text.upper()


def test_migration_024_applied_to_a_populated_ledger_appends_nothing():
    from catalyst_lab.repository import Repository

    with disposable_root("ledger") as root, tempfile.TemporaryDirectory() as partial, \
            pytest.MonkeyPatch.context() as patch:
        patch.setattr(localdb, "MIGRATIONS", migrations_through(Path(partial) / "m", 23))
        localdb.start(root)
        with Repository(localdb.connection_url(root)).connect() as conn:
            for index in range(3):
                Repository.append_event(conn, "SYSTEM_EVENT", {"kind": "LAB_FIXTURE",
                                                               "index": index})
        before = audit_head(root)
        owner_url = localdb.connection_url(root, "lab_owner")
        with psycopg.connect(owner_url) as conn:
            tables = conn.execute(ledger_ops.TABLES).fetchall()
            counts = {t: conn.execute(f'SELECT count(*) FROM "{t[0]}"."{t[1]}"').fetchone()[0]
                      for t in tables}
            conn.execute(MIGRATION.read_text())
        with psycopg.connect(owner_url) as conn:
            after_counts = {t: conn.execute(f'SELECT count(*) FROM "{t[0]}"."{t[1]}"'
                                            ).fetchone()[0] for t in tables}
            version = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
        assert audit_head(root) == before
        assert version == 24
        changed = {t: (counts[t], after_counts[t]) for t in tables if counts[t] != after_counts[t]}
        assert changed == {("lab", "schema_migrations"): (counts[("lab", "schema_migrations")],
                                                         counts[("lab", "schema_migrations")] + 1)}


# --- Scope: engineering, non-V3 and fixture evidence -----------------------------------------


def test_engineering_and_non_v3_setups_never_reach_a_public_view(er, ledger):  # noqa: F811
    run = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True)])
    sid, _, _ = ledger.trade(run.picks["SOL/USD"], arm="JEV_MANAGED",
                             entry_at=SLOT + timedelta(hours=1), exit_at=SLOT + timedelta(hours=5),
                             exit_price="104", reason="TARGET_EXIT")
    ledger.engineering_trade("DOGE/USD", at=datetime.now(UTC) - timedelta(minutes=30))
    # A managed setup from a V2 report (no report_schema_version): outside the experiment.
    v2 = Pick("ETH/USD")
    record = {"cycle_id": str(uuid4()), "item_key": "CRYPTO:ETH/USD", "revision": 1,
              "symbol": "ETH/USD", "market": "CRYPTO", "levels": v2.levels,
              "expires_at": (SLOT + timedelta(days=1)).isoformat(), "evidence_hash": "5" * 64}
    old = ledger.setup(v2, arm="FIXED_EXIT", admitted_at=SLOT, record=record)
    ledger.open(old, qty=D("2"), price=D("100.10"), at=SLOT + timedelta(minutes=5),
                arm="FIXED_EXIT")
    ledger.maintenance_hold(old, at=SLOT + timedelta(minutes=6), levels=(D("95"), D("111")),
                            bid="100.2")
    ledger.close(old, qty=D("2"), price=D("99"), at=SLOT + timedelta(hours=2),
                 reason="BROKER_EXIT")

    trades = rows(er, "public_dashboard_trades")
    assert [(r["trade_no"], r["symbol"], r["closed"]) for r in trades] == [(1, "SOL/USD", True)]
    [run_row] = rows(er, "public_dashboard_runs")
    assert (run_row["run_no"], run_row["picks"], run_row["selected"], run_row["traded"]) == (
        1, 1, 1, 1)
    symbols = {r["symbol"] for r in rows(er, "public_dashboard_decisions")}
    assert symbols == {"SOL/USD"}
    # The private analytics see both excluded trades, so the fixture really built them.
    private = managed_result_aggregates(ledger.app, group_by=("market",))
    assert private["engineering_count"] == 1
    assert sum(item["count"] for item in private["items"]) == 2  # SOL/USD and the V2 setup
    assert str(sid)  # the experiment trade is a real setup row


def test_lab_fixture_fee_evidence_is_flagged_on_the_trade(er, ledger):  # noqa: F811
    run = ledger.run(SLOT, [Pick("ADA/USD", jev_rank=1, selected=True)])
    ledger.trade(run.picks["ADA/USD"], arm="FIXED_EXIT", entry_at=SLOT + timedelta(hours=1),
                 exit_at=SLOT + timedelta(hours=3), exit_price="106", reason="TARGET_EXIT",
                 fees="LAB_FIXTURE")
    [row] = rows(er, "public_dashboard_trades")
    assert row["fixture_fee_evidence"] is True
    assert row["fees_known"] is True and row["net_pnl_usd"] is not None


# --- Parity with the private readers ---------------------------------------------------------


def _fee_scenarios(ledger):
    run = ledger.run(SLOT, [Pick(s, jev_rank=i, selected=True) for i, s in enumerate(
        ("BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "XRP/USD", "LINK/USD"), 1)])
    p = run.picks
    t0 = SLOT + timedelta(hours=1)
    verified = ledger.trade(p["BTC/USD"], arm="JEV_MANAGED", entry_at=t0,
                            exit_at=t0 + timedelta(hours=3), exit_price="106.4",
                            reason="TARGET_EXIT")[0]
    pending = ledger.trade(p["ETH/USD"], arm="FIXED_EXIT", entry_at=t0,
                           exit_at=t0 + timedelta(hours=2), exit_price="96",
                           reason="BROKER_EXIT", fees=None)[0]
    fixture = ledger.trade(p["SOL/USD"], arm="JEV_MANAGED", entry_at=t0,
                           exit_at=t0 + timedelta(hours=4), exit_price="101",
                           reason="HOLD_24H_EXIT", fees="LAB_FIXTURE")[0]
    # A broken correction chain: the second correction does not supersede the first.
    broken, buy, _ = ledger.trade(p["LTC/USD"], arm="FIXED_EXIT", entry_at=t0,
                                  exit_at=t0 + timedelta(hours=1), exit_price="97",
                                  reason="BROKER_EXIT")
    with ledger.app.connect() as conn:
        first = conn.execute("""SELECT body FROM lab.managed_events WHERE kind=
            'FILL_COST_CORRECTION' AND body->>'fill_id'=%s""", (buy["fill_id"],)).fetchone()
    body = {**first["body"], "correction_id": str(uuid4()),
            "supersedes_correction_id": str(uuid4())}
    with ledger.store.transaction() as conn:
        ledger.store.event(conn, "FILL_COST_CORRECTION", body, setup_id=broken,
                           key="fill-cost-correction:" + body["correction_id"])
    # Native fee on the fills themselves, no correction.
    native = ledger.setup(p["XRP/USD"], arm="JEV_MANAGED", admitted_at=t0)
    ledger.fill(native, side="buy", qty=D("10"), price=D("100.10"), at=t0 + timedelta(minutes=1),
                fee_usd="2.50")
    ledger.transition(native, "OPEN", qty="10")
    ledger.close(native, qty=D("10"), price="103", at=t0 + timedelta(hours=2),
                 reason="TARGET_EXIT", fee_usd="2.57")
    # Coin fee recorded but the sold quantity does not reconcile with it.
    unreconciled = ledger.trade(p["LINK/USD"], arm="FIXED_EXIT", entry_at=t0, qty=D("10"),
                                fees="ALPACA_PAPER_ACTIVITY")[0]
    ledger.close(unreconciled, qty=D("10"), price="102", at=t0 + timedelta(hours=2),
                 reason="TARGET_EXIT", fee_usd="2.55")
    return {"verified": verified, "pending": pending, "fixture": fixture, "broken": broken,
            "native": native, "unreconciled": unreconciled}


def test_closed_trade_figures_equal_managed_measurement(er, ledger):  # noqa: F811
    setups = _fee_scenarios(ledger)
    with public(er) as conn:
        snapshot = read_snapshot(conn)
    by_symbol = {r["symbol"]: r for r in snapshot["trades"] if r["closed"]}
    symbols = {"verified": "BTC/USD", "pending": "ETH/USD", "fixture": "SOL/USD",
               "broken": "LTC/USD", "native": "XRP/USD", "unreconciled": "LINK/USD"}
    for name, sid in setups.items():
        view = by_symbol[symbols[name]]
        private = managed_measurement(ledger.app, str(sid))

        def same(public_value, private_value):
            if private_value is None:
                return public_value is None
            return public_value is not None and D(str(public_value)) == D(str(private_value))

        assert same(view["gross_pnl_usd"], private["gross_realized_pnl"]), name
        assert same(view["net_pnl_usd"], private["net_pnl"]), name
        assert same(view["planned_risk_usd"], private["planned_filled_risk"]), name
        assert same(view["fees_usd"], private["verified_total_economic_fee_usd"]), name
        assert view["inventory_reconciled"] is private["inventory_reconciled_with_costs"], name
        official = (D(view["net_pnl_usd"]) / D(view["planned_risk_usd"])
                    if view["net_pnl_usd"] is not None else None)
        assert same(official, private["official_r"]), name
        fixture = any(e["source"] == "LAB_FIXTURE" for e in private["cost_evidence"])
        assert view["fixture_fee_evidence"] is fixture, name
    assert by_symbol["BTC/USD"]["net_pnl_usd"] is not None
    assert by_symbol["ETH/USD"]["fees_known"] is False
    assert by_symbol["LTC/USD"]["fees_known"] is False  # the broken chain keeps it unknown
    assert by_symbol["XRP/USD"]["fees_usd"] == D("5.07")
    assert by_symbol["LINK/USD"]["inventory_reconciled"] is False


def test_runs_and_pick_decisions_follow_the_ranking(er, ledger):  # noqa: F811
    picks = [
        Pick("BTC/USD", jev_rank=1, selected=True),
        Pick("ETH/USD", jev_rank=2, selected=True, declined="PRICE_MISMATCH"),
        Pick("SOL/USD", jev_rank=3, selected=True, replacement_for="CRYPTO:ETH/USD"),
        Pick("DOGE/USD", jev_rank=11),
        Pick("PEPE/USD", kind="NEWS", status="VETOED",
             levels={"entry_trigger": "0.0000098", "max_entry_price": "0.0000099",
                     "stop": "0.0000090", "target": "0.0000120"}),
        Pick("LTC/USD", kind="BOTH", status="NOT_RANKED"),
    ]
    run = ledger.run(SLOT, picks)
    ledger.trade(run.picks["BTC/USD"], arm="JEV_MANAGED", entry_at=SLOT + timedelta(hours=1))
    private = {p.symbol: p for p in cycle_picks(ledger.app, run.cycle_id,
                                                now=SLOT + timedelta(days=1))}
    decisions = rows(er, "public_dashboard_decisions", "kind, symbol",
                     "kind IN ('PICK','SELECTION')")
    proposed = {r["symbol"]: r for r in decisions if r["kind"] == "PICK"}
    verdicts = {r["symbol"]: r for r in decisions if r["kind"] == "SELECTION"}
    assert set(proposed) == set(verdicts) == set(private)
    expected = {"SELECTED": "BTC/USD ETH/USD SOL/USD", "PASSED": "DOGE/USD",
                "VETOED": "PEPE/USD", "NOT_RANKED": "LTC/USD"}
    for outcome, symbols in expected.items():
        for symbol in symbols.split():
            assert verdicts[symbol]["outcome"] == outcome, symbol
    for symbol, pick in private.items():
        assert verdicts[symbol]["jev_rank"] == pick.jev_rank, symbol
        assert (verdicts[symbol]["outcome"] == "SELECTED") is pick.selected, symbol
        assert proposed[symbol]["action"] == pick.kind, symbol
        assert proposed[symbol]["agent"] == FIXTURE_AGENT_ID
        assert proposed[symbol]["at"] == SLOT + timedelta(minutes=10)  # when it was received
        assert verdicts[symbol]["at"] == SLOT + timedelta(minutes=25)  # when Jev ranked
        assert verdicts[symbol]["actor"] == "JEV" and proposed[symbol]["actor"] == "RESEARCH_AGENT"
    assert proposed["PEPE/USD"]["entry"] == D("0.0000098")
    assert (proposed["PEPE/USD"]["stop"], proposed["PEPE/USD"]["target"]) == (
        D("0.0000090"), D("0.0000120"))
    assert proposed["BTC/USD"]["trade_no"] == 1 and verdicts["BTC/USD"]["trade_no"] == 1
    assert verdicts["PEPE/USD"]["note"] == "PRICES_CONSISTENT_NO"
    [run_row] = rows(er, "public_dashboard_runs")
    assert (run_row["run_no"], run_row["agent"], run_row["run_at"], run_row["submitted_at"],
            run_row["ranked_at"]) == (1, FIXTURE_AGENT_ID, SLOT, SLOT + timedelta(minutes=10),
                                      SLOT + timedelta(minutes=25))
    assert (run_row["picks"], run_row["ranked"], run_row["vetoed"], run_row["not_ranked"],
            run_row["selected"], run_row["traded"]) == (6, 4, 1, 1, 3, 1)
    assert run_row["selected_symbols"] == "BTC/USD,ETH/USD,SOL/USD"  # Jev's order


def test_agent_names_pass_only_in_their_identifier_shape(er, ledger):  # noqa: F811
    ledger.run(SLOT, [Pick("BTC/USD", jev_rank=1, selected=True)], agent="claude")
    ledger.run(SLOT + timedelta(days=1), [Pick("ETH/USD", jev_rank=1, selected=True)],
               agent="<script>alert(1)</script>")
    runs = rows(er, "public_dashboard_runs", "run_no")
    assert [r["agent"] for r in runs] == ["claude", None]
    agents = {r["symbol"]: r["agent"] for r in rows(er, "public_dashboard_decisions", "at",
                                                    "kind='PICK'")}
    assert agents == {"BTC/USD": "claude", "ETH/USD": None}
    assert "script" not in everything_public(er)


def test_thesis_shows_as_one_short_line_and_sources_never_appear(er, ledger):  # noqa: F811
    run = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True, thesis=LONG_THESIS),
                            Pick("ADA/USD", jev_rank=2, selected=True, thesis="  Tight\trange;"
                                 "\n\n breakout   retest.  ")])
    sid = ledger.trade(run.picks["SOL/USD"], arm="JEV_MANAGED",
                       entry_at=SLOT + timedelta(hours=1))[0]
    ledger.agent_flag(sid, at=SLOT + timedelta(hours=3), agent=FIXTURE_AGENT_ID,
                      why="Listing news\nreversed; the catalyst is gone.",
                      levels=(D("95"), D("111")))
    notes = {r["symbol"]: r["note"] for r in rows(er, "public_dashboard_decisions", "symbol",
                                                  "kind='PICK'")}
    assert notes["ADA/USD"] == "Tight range; breakout retest."
    long_line = notes["SOL/USD"]
    assert len(long_line) == 160 and long_line.endswith("...") and "\n" not in long_line
    assert long_line.startswith("Range reclaim after a two-week base. Volume expanded")
    [flag] = rows(er, "public_dashboard_decisions", "at", "kind='EXIT_FLAG'")
    assert (flag["actor"], flag["agent"], flag["outcome"], flag["note"]) == (
        "RESEARCH_AGENT", FIXTURE_AGENT_ID, "EXIT", "Listing news reversed; the catalyst is gone.")
    text = everything_public(er)
    for private_text in ("excerpt", "Fixture source excerpt", "fixture-source", "fixture-news",
                         "report text withheld", "request_hash", "flag_ref", "evidence_hash"):
        assert private_text not in text, private_text


# --- Live trades: price and Jev's actions ----------------------------------------------------


def test_live_price_is_the_freshest_price_the_ledger_holds(er, ledger):  # noqa: F811
    run = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True),
                            Pick("ETH/USD", jev_rank=2, selected=True)])
    t0 = SLOT + timedelta(hours=1)
    sid = ledger.trade(run.picks["SOL/USD"], arm="JEV_MANAGED", entry_at=t0)[0]
    ledger.trade(run.picks["ETH/USD"], arm="FIXED_EXIT", entry_at=t0,
                 exit_at=t0 + timedelta(hours=2), exit_price="104", reason="TARGET_EXIT")
    live = {r["symbol"]: r for r in rows(er, "public_dashboard_trades")}
    assert live["SOL/USD"]["price"] is None and live["SOL/USD"]["price_at"] is None  # unknown
    ledger.position_sample(sid, at=t0 + timedelta(minutes=5), bid="101.5", qty="9.975")
    ledger.maintenance_hold(sid, at=t0 + timedelta(minutes=6), levels=(D("95"), D("111")),
                            bid="101.7")
    [row] = rows(er, "public_dashboard_trades", where="symbol='SOL/USD'")
    assert (row["price"], row["price_at"]) == (D("101.7"), t0 + timedelta(minutes=6))
    ledger.position_sample(sid, at=t0 + timedelta(minutes=7), bid="101.2", qty="9.975")
    [row] = rows(er, "public_dashboard_trades", where="symbol='SOL/USD'")
    assert (row["price"], row["price_at"]) == (D("101.2"), t0 + timedelta(minutes=7))
    assert row["open_qty"] == D("10") and row["closed"] is False
    [closed] = rows(er, "public_dashboard_trades", where="symbol='ETH/USD'")
    assert closed["price"] is None and closed["closed"] is True  # closed: no live price


def test_jev_last_action_confidence_and_last_level_change(er, ledger):  # noqa: F811
    run = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True),
                            Pick("UNI/USD", jev_rank=2, selected=True)])
    t0 = SLOT + timedelta(hours=1)
    sid = ledger.trade(run.picks["SOL/USD"], arm="JEV_MANAGED", entry_at=t0)[0]
    fixed = ledger.trade(run.picks["UNI/USD"], arm="FIXED_EXIT", entry_at=t0)[0]
    ledger.maintenance_raise(sid, at=t0 + timedelta(minutes=30), before=(D("95"), D("111")),
                             after=(D("100.1"), D("111")), bid="103", basis="BREAKEVEN",
                             top_p="0.81")
    [row] = rows(er, "public_dashboard_trades", where="symbol='SOL/USD'")
    assert (row["jev_last_kind"], row["jev_last_outcome"], row["jev_last_action"]) == (
        "MAINTENANCE_DECISION", "APPLIED", "RAISE_STOP")
    assert (row["jev_last_stop"], row["jev_last_basis"], row["jev_last_confidence"]) == (
        D("100.1"), "BREAKEVEN", D("0.81"))
    assert row["jev_change_at"] == row["jev_last_at"] == t0 + timedelta(minutes=30)
    assert row["current_stop"] == D("100.1")
    for minutes in (31, 32):
        ledger.maintenance_hold(sid, at=t0 + timedelta(minutes=minutes),
                                levels=(D("100.1"), D("111")), bid="103", top_p="0.7")
    [row] = rows(er, "public_dashboard_trades", where="symbol='SOL/USD'")
    assert (row["jev_last_outcome"], row["jev_last_at"], row["jev_last_confidence"]) == (
        "HELD", t0 + timedelta(minutes=32), D("0.7"))
    assert (row["jev_change_action"], row["jev_change_at"], row["jev_change_stop"],
            row["jev_change_basis"]) == ("RAISE_STOP", t0 + timedelta(minutes=30), D("100.1"),
                                         "BREAKEVEN")
    ledger.agent_review(sid, at=t0 + timedelta(hours=24), agent=FIXTURE_AGENT_ID,
                        decision="CONTINUE", why="Still trending.", jev_decision="CONTINUE",
                        top_p="0.66")
    [row] = rows(er, "public_dashboard_trades", where="symbol='SOL/USD'")
    assert (row["jev_last_kind"], row["jev_last_outcome"], row["jev_last_confidence"]) == (
        "DAY_REVIEW_DECISION", "CONTINUE", D("0.66"))
    [control] = rows(er, "public_dashboard_trades", where="symbol='UNI/USD'")
    assert control["arm"] == "FIXED_EXIT" and control["jev_last_kind"] is None
    assert control["jev_change_at"] is None and str(fixed)


def test_decisions_carry_answers_flags_and_jev_actions_in_plain_codes(er, ledger):  # noqa: F811
    run = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True)])
    t0 = SLOT + timedelta(hours=1)
    sid = ledger.trade(run.picks["SOL/USD"], arm="JEV_MANAGED", entry_at=t0)[0]
    for minutes in range(1, 66):  # Reviews run every minute; the view keeps the latest 60.
        ledger.maintenance_hold(sid, at=t0 + timedelta(minutes=minutes),
                                levels=(D("95"), D("111")), bid="102")
    ledger.agent_review(sid, at=t0 + timedelta(hours=24), agent=FIXTURE_AGENT_ID,
                        decision="EXIT", why="Momentum faded.", jev_decision="EXIT",
                        top_p="0.83")
    decisions = rows(er, "public_dashboard_decisions", "at",
                     "kind NOT IN ('PICK','SELECTION')")
    holds = [r for r in decisions if r["kind"] == "MAINTENANCE"]
    assert len(holds) == 60
    assert holds[0]["at"] == t0 + timedelta(minutes=6) and holds[-1]["at"] == t0 + timedelta(
        minutes=65)
    assert {(r["actor"], r["outcome"], r["action"], r["confidence"], r["trade_no"]) for r in
            holds} == {("JEV", "HELD", "HOLD", D("0.74"), 1)}
    [answer] = [r for r in decisions if r["kind"] == "REVIEW_ANSWER"]
    assert (answer["actor"], answer["agent"], answer["outcome"], answer["note"], answer["at"]) \
        == ("RESEARCH_AGENT", FIXTURE_AGENT_ID, "EXIT", "Momentum faded.",
            t0 + timedelta(hours=24) - timedelta(minutes=20))
    [review] = [r for r in decisions if r["kind"] == "DAY_REVIEW"]
    assert (review["actor"], review["outcome"], review["confidence"], review["note"]) == (
        "JEV", "EXIT", D("0.83"), "AGREED")


def test_the_snapshot_reads_each_shown_trades_listed_decisions(er, ledger):  # noqa: F811
    """read_snapshot's trade_decisions (a trade's events on the page): each open and latest
    closed trade's pick, Jev's selection and its listed reviews, oldest first per trade. A
    pick that was not traded has no trade and is not read."""
    run = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True),
                            Pick("ETH/USD", jev_rank=2, selected=True),
                            Pick("BTC/USD", jev_rank=3)])
    t0 = SLOT + timedelta(hours=1)
    sid = ledger.trade(run.picks["SOL/USD"], arm="JEV_MANAGED", entry_at=t0)[0]
    ledger.trade(run.picks["ETH/USD"], arm="FIXED_EXIT", entry_at=t0,
                 exit_at=t0 + timedelta(hours=2), exit_price="104", reason="TARGET_EXIT")
    for minutes in (1, 2):
        ledger.maintenance_hold(sid, at=t0 + timedelta(minutes=minutes),
                                levels=(D("95"), D("111")), bid="101")
    with public(er) as conn:
        snapshot = read_snapshot(conn)
    kinds = {}
    for row in snapshot["trade_decisions"]:
        kinds.setdefault((row["trade_no"], row["symbol"]), []).append(row["kind"])
    assert kinds == {(1, "SOL/USD"): ["PICK", "SELECTION", "MAINTENANCE", "MAINTENANCE"],
                     (2, "ETH/USD"): ["PICK", "SELECTION"]}
    assert [r["at"] for r in snapshot["trade_decisions"] if r["trade_no"] == 1] == [
        SLOT + timedelta(minutes=10), SLOT + timedelta(minutes=25), t0 + timedelta(minutes=1),
        t0 + timedelta(minutes=2)]


def test_the_snapshot_reads_the_current_cycles_picks_and_the_decisions_since_its_start(
        er, ledger):  # noqa: F811
    """read_snapshot's cycle_picks: the newest ranked run's picks and verdicts only (an older
    run's are not read); day_decisions: every decision since the earlier of the New York day's
    start and that run's selection, newest first, never a pick or a selection."""
    older = ledger.run(SLOT, [Pick("SOL/USD", jev_rank=1, selected=True)])
    newer = ledger.run(SLOT + timedelta(days=1), [Pick("ETH/USD", jev_rank=1, selected=True),
                                                  Pick("BTC/USD", status="VETOED")])
    t0 = SLOT + timedelta(hours=1)
    sid = ledger.trade(older.picks["SOL/USD"], arm="JEV_MANAGED", entry_at=t0)[0]
    before, after = t0 + timedelta(minutes=5), SLOT + timedelta(days=1, hours=1)
    for at in (before, after, after + timedelta(minutes=1)):
        ledger.maintenance_hold(sid, at=at, levels=(D("95"), D("111")), bid="101")
    with public(er) as conn:
        snapshot = read_snapshot(conn)
    assert {(r["run_no"], r["kind"], r["symbol"]) for r in snapshot["cycle_picks"]} == {
        (2, "PICK", "ETH/USD"), (2, "PICK", "BTC/USD"), (2, "SELECTION", "ETH/USD"),
        (2, "SELECTION", "BTC/USD")}
    since = newer.run_slot + timedelta(minutes=25)  # run 2's selection, before today
    assert [(r["kind"], r["at"]) for r in snapshot["day_decisions"]] == [
        ("MAINTENANCE", after + timedelta(minutes=1)), ("MAINTENANCE", after)]
    assert all(r["at"] >= since for r in snapshot["day_decisions"])


# --- Status -------------------------------------------------------------------------------------


def test_status_reports_heartbeat_halts_equity_and_jev_calls(er, ledger):  # noqa: F811
    [empty] = rows(er, "public_dashboard_status")
    assert empty["last_heartbeat_at"] is None and empty["experiment_started_at"] is None
    assert (empty["active_halts"], empty["daily_loss_halt_today"], empty["schema_version"],
            empty["equity_usd"], empty["jev_last_call_at"], empty["jev_calls_today"]) == (
        0, False, 25, None, None, 0)  # The ledger's schema: 025 (Jev review policy V2).
    ledger.heartbeat(management_reviews="DISABLED",
                     jev_breaker={"state": "OPEN", "epoch": 1, "blocked_until": None})
    with ledger.store.transaction() as conn:
        halt(ledger.store.repo, conn, "OPERATOR_PAUSE", {"reason": "LAB_FIXTURE pause"})
    run = ledger.run(SLOT, [Pick("BTC/USD", jev_rank=1, selected=True)])
    sid = ledger.trade(run.picks["BTC/USD"], arm="JEV_MANAGED",
                       entry_at=SLOT + timedelta(hours=1))[0]
    ledger.equity_reading(sid, equity="10084.27")
    [status] = rows(er, "public_dashboard_status")
    assert status["last_heartbeat_at"] is not None
    assert status["heartbeat_management_reviews"] == "DISABLED"
    assert status["jev_breaker"] == "OPEN"
    assert status["active_halts"] == 1
    assert status["experiment_started_at"] == SLOT
    assert status["equity_usd"] == D("10084.27") and status["equity_at"] is not None
    assert status["jev_last_call_outcome"] == "VALID" and status["jev_calls_today"] == 1
    assert status["ny_today"] == status["as_of"].astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")).date()
    assert FIXTURE_ACCOUNT_MARKER not in everything_public(er)


# --- The engine ---------------------------------------------------------------------------------


def test_a_trade_driven_through_the_engine_is_reported_as_the_engine_recorded_it(
        er, mt):  # noqa: F811
    """Real admission (report V3 through intake and the mock Jev), trigger, fill, protection and
    the app-watched target exit: the public row matches the engine's own records."""
    from catalyst_lab.repository import Repository

    engine, venue, _ = mt
    sid = open_trade(mt, "SOL/USD")
    [live] = rows(er, "public_dashboard_trades")
    assert (live["trade_no"], live["symbol"], live["closed"]) == (1, "SOL/USD", False)
    with Repository(er.database_url).connect() as conn:
        samples = conn.execute("""SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind IN ('POSITION_MARKET_SNAPSHOT','MAINTENANCE_DECISION')""",
                               (sid,)).fetchall()
    if not samples:
        assert live["price"] is None  # nothing in the ledger yet: unknown, never fetched
    engine.manage(sid, quote(mt, "111"))
    engine.manage(sid, quote(mt, "111"))
    [close] = [o for o in venue.orders_of("sell", "market") if o["symbol"] == "SOL/USD"]
    assert engine.ingest(venue.fill(close["id"], close["qty"], price="111"))
    engine.manage(sid, quote(mt, "111"))
    state = engine._load(sid)[1]
    assert state["state"] == "CLOSED"
    [row] = rows(er, "public_dashboard_trades")
    measurement = managed_measurement(Repository(er.database_url), str(sid))
    assert (row["trade_no"], row["symbol"], row["arm"]) == (1, "SOL/USD", state["arm"])
    assert row["exit_reason"] == state["reason"] == "TARGET_EXIT"
    assert row["gross_pnl_usd"] == D(measurement["gross_realized_pnl"])
    assert row["fees_known"] is False and measurement["net_pnl"] is None
    assert row["planned_risk_usd"] == D(measurement["planned_filled_risk"])
    assert row["price"] is None
    [run] = rows(er, "public_dashboard_runs")
    assert (run["picks"], run["selected"], run["traded"]) == (1, 1, 1)
