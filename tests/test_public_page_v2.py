"""Migration 028 and EXPERIMENT_DASHBOARD_V2 end to end (package public-page).

Disposable PostgreSQL only (a private cluster for this module; catalyst_public gets LOGIN here as
the cloud provisioner grants it in production). The fixture ledger is tests/experiment_fixtures.py's
demo experiment plus build_demo_v2's phase-A trades, entry waits, day baseline and market
regimes; every row is LAB_FIXTURE data.
"""

import hashlib
import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import catalyst_lab
from catalyst_lab import localdb
from catalyst_lab.experiment_page import create_experiment_app
from catalyst_lab.experiment_report import build_dashboard, read_snapshot
from tests.experiment_fixtures import (
    ExperimentLedger,
    build_demo_v2,
    enable_public_login,
    public_url,
    role_url,
)
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401

NY = ZoneInfo("America/New_York")
MIGRATION_028 = Path(localdb.__file__).with_name("migrations") / "028_public_page_v2.sql"
NEW_VIEWS = ("public_page_trades", "public_page_reviews", "public_page_trade_waits",
             "public_page_status", "public_page_regimes")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@pytest.fixture(scope="module", autouse=True)
def public_login(pristine_cluster):  # noqa: F811
    enable_public_login(pristine_cluster)


@pytest.fixture
def demo(er):  # noqa: F811
    ledger = ExperimentLedger(er.database_url)
    now = datetime.now(UTC)
    runs, trades, extra = build_demo_v2(ledger, now)
    return ledger, now, runs, trades, extra


def public(er):  # noqa: F811
    return psycopg.connect(public_url(er.database_url), row_factory=dict_row,
                           options="-c timezone=UTC")


def snapshot_document(er, **kwargs):  # noqa: F811
    with public(er) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = read_snapshot(conn)
    return snapshot, build_dashboard(snapshot, **kwargs)


# --- The views --------------------------------------------------------------------------------


def test_the_traded_levels_are_the_plans_and_the_research_levels_stay_beside_them(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    with public(er) as conn:
        rows = {r["trade_no"]: r for r in conn.execute(
            "SELECT * FROM lab.public_page_trades").fetchall()}
        old = {r["trade_no"]: r for r in conn.execute(
            "SELECT * FROM lab.public_dashboard_trades").fetchall()}
    assert len(rows) == len(old) == 14
    planned = [r for r in rows.values() if r["trade_plan_policy"]]
    assert len(planned) == 2
    for row in planned:
        item = next(x for x in extra if old[row["trade_no"]]["symbol"] == x["pick"].symbol)
        plan, levels = item["plan"], item["pick"].levels
        assert (row["plan_stop"], row["plan_target"]) == (D(plan["stop"]), D(plan["target"]))
        # 024's view keeps the research levels: the old page's meaning is unchanged.
        assert old[row["trade_no"]]["planned_stop"] == D(levels["stop"])
        assert row["stop_basis"] == "HOURLY_RANGE_FLOOR" and row["target_basis"] == "PLAN_CAP"
        assert row["window_minutes"] == 1440
        assert row["hourly_range_fraction"].quantize(D("0.0001")) == D("0.0200")
        # The reservation's planned risk: qty x (max entry - least(research, plan stop)).
        m = D(levels["max_entry_price"])
        assert row["risk_stop"] == min(D(levels["stop"]), D(plan["stop"])) == D(plan["stop"])
        assert row["risk_usd"] == old[row["trade_no"]]["bought_qty"] * (m - D(plan["stop"]))
        assert (row["risk_policy"], row["entry_pacing_policy"], row["stop_limit_policy"]) == (
            "JEV_MANAGED_RISK_V4", "CRYPTO_ENTRY_PACING_V1", "CRYPTO_STOP_BREACH_V4")
        assert row["regime_day_tag"] == "UP/HIGH/NARROW/NO_SELLOFF"
        assert row["regime_median_coin_1h"] == "DOWN"
    for row in rows.values():
        if not row["trade_plan_policy"]:
            assert row["plan_stop"] is None and row["risk_policy"] == "JEV_MANAGED_RISK_V3"
            assert row["risk_usd"] == old[row["trade_no"]]["planned_risk_usd"]


def test_entry_waits_fold_per_reason_and_only_before_the_buy(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    with public(er) as conn:
        waits = conn.execute("SELECT * FROM lab.public_page_trade_waits ORDER BY reason"
                             ).fetchall()
    assert [(w["wait_kind"], w["reason"], w["waits"]) for w in waits] == [
        ("PACING", "ENTRY_RATE_LIMIT", 4), ("PACING", "MARKET_DROP", 4)]
    drop = waits[1]
    assert drop["worst_median_1h_return"] == D("-0.0234")
    assert drop["last_at"] - drop["first_at"] == timedelta(minutes=3)


def test_status_reads_the_newest_setups_policy_baseline_and_rules(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    with public(er) as conn:
        status = conn.execute("SELECT * FROM lab.public_page_status").fetchone()
        regimes = conn.execute("SELECT * FROM lab.public_page_regimes ORDER BY day").fetchall()
    assert status["risk_policy"] == "JEV_MANAGED_RISK_V4"
    assert (status["risk_pct"], status["crypto_cap_pct"], status["hard_loss_pct"],
            status["soft_loss_pct"]) == (D("0.005"), D("0.02"), D("0.03"), D("0.02"))
    # RISK_SESSION_BASELINE_V2's basis, not the row's stale Alpaca last_equity (package baseline).
    assert (status["day_start_equity"], status["day_start_basis"], status["session_row_equity"]) \
        == (D("9950"), "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2", D("10000"))
    assert (status["baseline_corrected_from"], status["baseline_corrected_to"]) == (
        D("10000"), D("9950"))
    assert status["equity_usd"] == D("10084.27") and status["equity_at"] is not None
    assert status["soft_limit_at"] is None and status["daily_halt_at"] is None
    assert status["execution_halts"] == 0
    assert status["pacing_wait_reason"] == "ENTRY_RATE_LIMIT"
    assert (status["rules_risk_policy"], status["rules_trade_plan_policy"]) == (
        "JEV_MANAGED_RISK_V4", "CRYPTO_TRADE_PLAN_V1")
    assert status["rules_since"] == min(x["entry_at"] for x in extra) - timedelta(minutes=20)
    assert [r["tag"] for r in regimes] == ["UP/HIGH/BROAD/SELLOFF", "UP/NORMAL/BROAD/NO_SELLOFF",
                                           "UP/HIGH/NARROW/NO_SELLOFF"]
    assert regimes[0]["worst_hour_pct"] == D("-3.2")


def test_a_ledger_without_a_policy_or_baseline_reads_nothing_unknown_as_zero(er):  # noqa: F811
    with public(er) as conn:
        status = conn.execute("SELECT * FROM lab.public_page_status").fetchone()
        assert conn.execute("SELECT count(*) AS n FROM lab.public_page_trades").fetchone()[
            "n"] == 0
    assert status["risk_policy"] is None and status["hard_loss_pct"] is None
    assert status["day_start_equity"] is None and status["rules_since"] is None
    assert status["day_start_basis"] is None and status["equity_usd"] is None
    _, document = snapshot_document(er)
    assert document["system"]["state"] == "STOPPED"  # No heartbeat at all.
    assert document["limits"]["hard_at"] is None and document["market"]["day"] is None


def test_the_new_views_expose_no_identifier_or_private_field(er, demo):  # noqa: F811
    with public(er) as conn:
        dump = json.dumps({v: conn.execute(f"SELECT * FROM lab.{v}").fetchall()
                           for v in NEW_VIEWS}, default=str)
        columns = {r["column_name"] for r in conn.execute(
            """SELECT column_name FROM information_schema.columns WHERE table_schema='lab'
            AND table_name = ANY(%s)""", (list(NEW_VIEWS),)).fetchall()}
    assert not UUID.search(dump)
    for word in ("setup_id", "cycle_id", "receipt", "order", "fill_id", "evidence", "account",
                 "hash", "token", "lifecycle", "LAB_FIXTURE", "returns"):
        assert not [c for c in columns if word in c], word
    assert "LAB_FIXTURE" not in dump  # The hourly range evidence stays private.


@pytest.mark.parametrize("relation", ["lab.experiment_setup_versions", "lab.risk_sessions",
                                      "lab.account_risk_policies",
                                      "lab.account_risk_daily_limits", "lab.daily_risk_halts"])
def test_catalyst_public_still_cannot_read_the_tables_behind_the_new_views(er, relation):  # noqa: F811
    with public(er) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"SELECT 1 FROM {relation} LIMIT 1")


def test_catalyst_public_cannot_write_through_the_new_views(er, demo):  # noqa: F811
    with public(er) as conn:
        for view in NEW_VIEWS:
            row = conn.execute(
                """SELECT has_table_privilege(%s, 'SELECT') AS reads, has_table_privilege(%s,
                'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS writes""",
                (f"lab.{view}", f"lab.{view}")).fetchone()
            assert row == {"reads": True, "writes": False}, view
        assert conn.execute("SHOW transaction_read_only").fetchone()[
            "transaction_read_only"] == "off"  # The service adds read-only; the grants hold too.


# --- The document ---------------------------------------------------------------------------


def test_the_document_shows_the_traded_levels_plan_waits_and_rules(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    _, document = snapshot_document(er)
    assert document["dashboard_version"] == "EXPERIMENT_DASHBOARD_V3"
    closed = next(t for t in document["past"]["closed_trades"] if t["plan"])
    assert closed["symbol"] == extra[0]["pick"].symbol
    plan = extra[0]["plan"]
    assert D(closed["planned_stop"]) == D(plan["stop"])
    assert D(closed["planned_target"]) == D(plan["target"])
    assert D(closed["research_stop"]) == D(extra[0]["pick"].levels["stop"])
    assert closed["plan"]["stop_rule"].startswith("2× the hourly range (2.00%)")
    assert closed["plan"]["target_rule"] == "Capped at 1.5R above the entry"
    assert closed["rules"] == "CURRENT" and closed["market_at_entry"]["words"].startswith(
        "Median coin −1.3% the hour before")
    kinds = [e["kind"] for e in closed["events"]]
    assert kinds[:5] == ["PICK", "SELECTION", "PLAN", "WAIT", "BUY"]
    wait = closed["events"][3]
    assert wait["text"] == "Entry waited: market dropping" and wait["waits"] == 4
    assert "Median coin −2.34% over an hour at the worst" in wait["note"]
    # R on the plan's risk: a sale just under the plan stop is one R plus the fees and the
    # 0.2% slip (about 0.16R here); on the research stop it would have read about 1.5R.
    assert D("-1.25") < D(closed["pnl_r"]) < D("-1.05")
    assert closed["after_exit"] is None
    earlier = [t for t in document["past"]["closed_trades"] if not t["plan"]]
    assert {t["rules"] for t in earlier} == {"EARLIER"}
    rules = document["results"]["rules"]
    assert (rules["current"]["trades"], rules["earlier"]["trades"]) == (1, 8)
    assert rules["current"]["versions"]["trade_plan_policy"] == "CRYPTO_TRADE_PLAN_V1"
    assert rules["current"]["avg_net_r"] is None and rules["earlier"]["avg_net_r"] is None


def test_the_status_line_follows_the_ledger(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    _, document = snapshot_document(er)
    assert document["system"]["state"] == "TRADING"
    limits = document["limits"]
    assert limits["day_start_equity_usd"] == "9950.00"
    # The halt engine's figure: account equity minus the V2 basis.
    assert (limits["measure"], limits["pnl_usd"], limits["account_equity_usd"]) == (
        "ACCOUNT_EQUITY", "134.27", "10084.27")
    assert limits["baseline_corrected_from_usd"] == "10000.00"
    sid = extra[1]["setup_id"]
    ledger.pacing_wait(sid, at=datetime.now(UTC) - timedelta(seconds=20))
    _, document = snapshot_document(er)
    assert (document["system"]["state"], document["system"]["reason"]) == ("PAUSED",
                                                                            "MARKET_DROP")
    assert "(now −2.34%)" in document["system"]["text"]
    day = datetime.now(UTC).astimezone(NY).date().isoformat()
    ledger.soft_limit(day, at=datetime.now(UTC), total="-205", day_start="10000")
    _, document = snapshot_document(er)
    assert document["system"]["state"] == "SOFT_LIMIT"
    assert document["system"]["text"].startswith("Daily loss reached the −2.00% soft limit")
    ledger.daily_halt(day, realized="-260.97", unrealized="-48.13", threshold="-300")
    _, document = snapshot_document(er)
    assert document["system"]["state"] == "HALTED"
    assert document["system"]["reason"] == "DAILY_LOSS_LIMIT"
    assert document["limits"]["halt_pnl_usd"] == "-309.10"


def test_the_service_serves_a_trades_own_page_and_404_for_others(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    with TestClient(create_experiment_app(public_url(er.database_url), environ={},
                                          fixture_data=True)) as client:
        document = client.get("/api/public/experiment").json()
        number = document["live_trades"][0]["trade_no"]
        page = client.get(f"/trade/{number}")
        assert page.status_code == 200
        assert f'data-view="trade" data-trade="{number}"' in page.text
        assert "&larr; Live page" in page.text and "FIXTURE DATA" in page.text
        assert client.get("/trade/99999").status_code == 404
        assert client.get("/trade/abc").status_code == 422
        for section in ("system", "limits", "open_risk", "market", "results"):
            body = client.get(f"/api/public/experiment/{section}").json()
            assert body[section] == document[section] or section == "system"


def test_a_withdrawn_soft_limit_is_not_shown_and_a_kept_one_says_so(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    day = datetime.now(UTC).astimezone(NY).date().isoformat()
    at = datetime.now(UTC) - timedelta(minutes=30)
    first = ledger.soft_limit(day, at=at, total="-205", day_start="10000")
    ledger.latch_decision(first, kept=False, at=at + timedelta(minutes=1))
    with public(er) as conn:
        status = conn.execute("SELECT * FROM lab.public_page_status").fetchone()
    assert status["soft_limit_at"] is None and status["soft_limit_withdrawn_latch_at"] == at
    _, document = snapshot_document(er)
    assert (document["system"]["state"], document["system"]["reason"]) == (
        "TRADING", "SOFT_LIMIT_WITHDRAWN")
    assert "was withdrawn after the day's start was corrected" in document["system"]["text"]
    # A later latch measured on the V2 basis is in force again; a kept decision is named.
    suffix = f":after-withdrawal:{first['event_seq']}"
    again = ledger.soft_limit(day, at=at + timedelta(minutes=5), total="-200",
                              day_start="9950", key_suffix=suffix)
    ledger.latch_decision(again, kept=True, at=at + timedelta(minutes=6))
    _, document = snapshot_document(er)
    assert document["system"]["state"] == "SOFT_LIMIT"
    assert document["system"]["text"].endswith("It was kept after the day's start was corrected.")


def test_v5_reviews_read_as_yes_no_answers_with_their_streak(er, demo):  # noqa: F811
    ledger, now, runs, trades, extra = demo
    with public(er) as conn:
        reviews = conn.execute("SELECT * FROM lab.public_page_reviews ORDER BY at").fetchall()
    assert len(reviews) == 7 and reviews[-1]["action"] == "CONFIRMING"
    assert (reviews[-1]["invalidation_p"], reviews[-1]["invalidation_verdict"],
            reviews[-1]["invalidation_effect"], reviews[-1]["invalidation_streak"]) == (
        D("0.86"), "YES", "COUNTED", 1)
    _, document = snapshot_document(er)
    trade = next(t for t in document["live_trades"] if t["plan"])
    texts = [e["text"] for e in trade["events"] if e["kind"] == "REVIEW"]
    assert texts == ["Held: invalidation met? no, 6 reviews in a row (p 0.03–0.12)",
                     "Invalidation met? yes (0.86), 1 of 3 · news contradicts? no (0.11)"]
    lines = [d["text"] for d in document["feed"] if d["trade_no"] == trade["trade_no"]]
    assert lines[:2] == [
        f"{trade['symbol']}: Invalidation met? yes (0.86), 1 of 3 · news contradicts? no (0.11)",
        f"{trade['symbol']}: Held: invalidation met? no, 6 reviews in a row (p 0.03–0.12)"]


# --- Migration 028 --------------------------------------------------------------------------


def test_028_is_views_and_grants_only():
    text = MIGRATION_028.read_text()
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", text).split(";") if s.strip()]
    heads = [" ".join(s.split()[:3]) for s in statements]
    assert heads[:6] == [
        "CREATE VIEW lab.experiment_setup_versions", "CREATE VIEW lab.public_page_trades",
        "CREATE VIEW lab.public_page_reviews", "CREATE VIEW lab.public_page_trade_waits",
        "CREATE VIEW lab.public_page_status", "CREATE VIEW lab.public_page_regimes"]
    assert heads[6:] == ["REVOKE ALL ON", "GRANT SELECT ON",
                         "INSERT INTO lab.schema_migrations(version)"]
    grant = statements[7]
    assert "catalyst_public" in grant and "experiment_setup_versions" not in grant
    for forbidden in ("CREATE OR REPLACE", "DROP ", "ALTER ", "UPDATE ", "DELETE ", "TRUNCATE",
                      "CREATE ROLE", "SECURITY DEFINER", "CREATE FUNCTION", "CREATE TABLE"):
        assert forbidden not in re.sub(r"--[^\n]*", "", text), forbidden
    assert text.rstrip().endswith("INSERT INTO lab.schema_migrations(version) VALUES(28);")


def test_028_applies_to_a_schema_27_ledger_without_an_audit_event_or_a_changed_024_view():
    from catalyst_lab.config import SCHEMA_VERSION
    from tests.test_operator_controls import start_cluster_at

    assert SCHEMA_VERSION == 31  # 029, 030 and 031 (plugin-c3) follow; this proves 028.
    with tempfile.TemporaryDirectory(prefix="catalyst-028-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 27)
            owner_url = localdb.connection_url(root, "lab_owner")
            definitions = """SELECT c.relname::text, pg_get_viewdef(c.oid) FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='lab' AND c.relkind='v' ORDER BY 1"""
            with psycopg.connect(owner_url) as conn:
                head = conn.execute("SELECT event_hash FROM lab.trade_events "
                                    "ORDER BY seq DESC LIMIT 1").fetchone()[0]
                count = conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0]
                before = dict(conn.execute(definitions).fetchall())
                conn.execute(MIGRATION_028.read_text())
            with psycopg.connect(owner_url) as conn:
                assert conn.execute("SELECT max(version) FROM lab.schema_migrations"
                                    ).fetchone()[0] == 28
                assert conn.execute("SELECT event_hash FROM lab.trade_events ORDER BY seq "
                                    "DESC LIMIT 1").fetchone()[0] == head
                assert conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0] == count
                after = dict(conn.execute(definitions).fetchall())
                grants = {r[0] for r in conn.execute(
                    """SELECT table_name FROM information_schema.role_table_grants
                    WHERE grantee='catalyst_public' AND privilege_type='SELECT'""").fetchall()}
            assert {k: after[k] for k in before} == before  # 024's views (and all) unchanged.
            assert set(after) - set(before) == {"experiment_setup_versions", *NEW_VIEWS}
            assert grants == {"public_dashboard_status", "public_dashboard_trades",
                              "public_dashboard_runs", "public_dashboard_decisions", *NEW_VIEWS}
            # This release's schema is 30: 029 (the public page V3 views) and 030
            # (JEV_TOP_K_SELECTION_V3) follow.
            from tests.test_public_page_v3 import apply_migration_029
            from tests.test_selection_topk_v3 import apply_migration_030

            apply_migration_029(root)
            apply_migration_030(root)
            from tests.test_risk_v5 import apply_migration_031

            apply_migration_031(root)  # This release's schema is 31 (package plugin-c3).
            app = role_url(localdb.connection_url(root), "catalyst_app")
            from catalyst_lab.repository import Repository

            Repository(app).check_role()
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def apply_migration_028(root):
    """Apply 028 as the owner to a disposable cluster at schema 27 and assert it appended no
    audit event (views and grants only); returns the audit head."""
    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 27
        conn.execute(MIGRATION_028.read_text())
    with psycopg.connect(owner_url) as conn:
        after = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 28
    assert after == head
    return after


# The 028 file is the one applied to the live ledger on 2026-10-03 05:49 UTC (sha256 prefix).
MIGRATION_028 = Path(catalyst_lab.__file__).with_name("migrations") / "028_public_page_v2.sql"
APPLIED_SHA256_PREFIX = "c65fc28785d01916"


def test_the_shipped_028_is_the_file_applied_to_the_live_ledger():
    digest = hashlib.sha256(MIGRATION_028.read_bytes()).hexdigest()
    assert digest.startswith(APPLIED_SHA256_PREFIX)
    text = MIGRATION_028.read_text()
    assert "INSERT INTO lab.schema_migrations(version) VALUES(28);" in text
    assert text.count("CREATE VIEW lab.") == 6

