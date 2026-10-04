"""Package public-page-v3: ACCOUNT_EQUITY_SNAPSHOT_V1, STATS_EXCLUSION_V1, migration 029 and
EXPERIMENT_DASHBOARD_V3 (the account, the equity curve with the BTC benchmark, the drawdown,
daily P&L, the R distribution, performance, open positions and a trade's price chart).

Disposable PostgreSQL only (a private cluster for this module; catalyst_public gets LOGIN here as
the cloud provisioner grants it in production). Public market data comes from a fake in memory or
an ``httpx.MockTransport``: no test reaches a network, a broker or Jev.
"""

import io
import json
import re
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from catalyst_lab import cloud_runtime, experiment_v3, localdb
from catalyst_lab.equity_snapshot import (
    SNAPSHOT_EVENT,
    EquitySnapshotRecorder,
    bucket_start,
    snapshot_body,
)
from catalyst_lab.experiment_html import render_page
from catalyst_lab.experiment_page import create_experiment_app
from catalyst_lab.experiment_report import build_dashboard, read_snapshot
from catalyst_lab.public_market import PublicMarketData, PublicMarketError, parse_bar
from catalyst_lab.repository import Repository
from catalyst_lab.stats_exclusion import (
    EXCLUSION_EVENT,
    StatsExclusionRefused,
    excluded_setups_of,
    record_exclusion,
)
from tests.experiment_fixtures import (
    ExperimentLedger,
    build_demo_v3,
    enable_public_login,
    public_url,
    role_url,
)
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401

NY = ZoneInfo("America/New_York")
MIGRATION_029 = Path(localdb.__file__).with_name("migrations") / "029_public_page_v3.sql"
NEW_VIEWS = ("public_page_equity", "public_page_day_starts", "public_page_account_marks",
             "public_page_stats_exclusions")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
T0 = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def public_login(pristine_cluster):  # noqa: F811
    enable_public_login(pristine_cluster)


@pytest.fixture
def demo(er):  # noqa: F811
    ledger = ExperimentLedger(er.database_url)
    now = datetime.now(UTC)
    runs, trades, extra, info = build_demo_v3(ledger, now)
    return ledger, now, info


def public(er):  # noqa: F811
    return psycopg.connect(public_url(er.database_url), row_factory=dict_row,
                           options="-c timezone=UTC")


def document_of(er, **kwargs):  # noqa: F811
    with public(er) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = read_snapshot(conn)
    return snapshot, json.loads(json.dumps(build_dashboard(snapshot, **kwargs), default=str))


class FakeMarket:
    """Public market data in memory: hourly or minute bars on a gentle slope, and quotes."""

    def __init__(self, *, fail=False, quote=None):
        self.fail, self.quote, self.calls = fail, quote, []

    def bars(self, symbol, start, end, timeframe):
        self.calls.append(("bars", symbol, timeframe))
        if self.fail:
            raise PublicMarketError("PUBLIC_MARKET_CONNECTION_ERROR")
        step = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600}[timeframe]
        t = datetime.fromtimestamp(int(start.timestamp()) // step * step, UTC)
        out, i = [], 0
        while t < end:
            c = D("100") + D(i) / 10
            out.append({"t": t, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": D(10 + i % 7)})
            t += timedelta(seconds=step)
            i += 1
        return out

    def latest_quotes(self, symbols):
        self.calls.append(("quotes", tuple(symbols)))
        if self.fail:
            raise PublicMarketError("PUBLIC_MARKET_HTTP_503")
        at = datetime.now(UTC)
        return {s: {"bid": self.quote or D("1"), "ask": None, "at": at} for s in symbols}


# --- Migration 029 ----------------------------------------------------------------------------


def apply_migration_029(root):
    """Apply 029 as the owner to a disposable cluster at schema 28 and assert it appended no
    audit event (views and grants only); returns the audit head."""
    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 28
        conn.execute(MIGRATION_029.read_text())
    with psycopg.connect(owner_url) as conn:
        after = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 29
    assert after == head
    return after


def test_029_is_views_and_grants_only():
    text = MIGRATION_029.read_text()
    code = re.sub(r"--[^\n]*", "", text)
    for forbidden in ("CREATE OR REPLACE", "DROP ", "ALTER ", "UPDATE ", "DELETE ", "TRUNCATE",
                      "CREATE ROLE", "SECURITY DEFINER", "CREATE FUNCTION", "CREATE TABLE",
                      "INSERT INTO lab.managed", "GRANT INSERT", "GRANT UPDATE"):
        assert forbidden not in code, forbidden
    assert code.count("CREATE VIEW lab.") == 4
    assert text.rstrip().endswith("INSERT INTO lab.schema_migrations(version) VALUES(29);")
    assert "catalyst_public" in code and "TO catalyst_public" in code


def test_029_applies_to_a_schema_28_ledger_without_an_audit_event_or_a_changed_view():
    from catalyst_lab.config import SCHEMA_VERSION
    from tests.test_operator_controls import start_cluster_at

    assert SCHEMA_VERSION == 31  # 030 (JEV_TOP_K_SELECTION_V3) and 031 (plugin-c3) follow.
    with tempfile.TemporaryDirectory(prefix="catalyst-029-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 28)
            owner_url = localdb.connection_url(root, "lab_owner")
            definitions = """SELECT c.relname::text, pg_get_viewdef(c.oid) FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='lab' AND c.relkind='v' ORDER BY 1"""
            with psycopg.connect(owner_url) as conn:
                before = dict(conn.execute(definitions).fetchall())
                count = conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0]
            apply_migration_029(root)
            with psycopg.connect(owner_url) as conn:
                after = dict(conn.execute(definitions).fetchall())
                assert conn.execute("SELECT count(*) FROM lab.trade_events").fetchone()[0] == count
                grants = {r[0] for r in conn.execute(
                    """SELECT table_name FROM information_schema.role_table_grants
                    WHERE grantee='catalyst_public' AND privilege_type='SELECT'""").fetchall()}
                others = {r[0] for r in conn.execute(
                    """SELECT table_name FROM information_schema.role_table_grants
                    WHERE grantee='catalyst_public' AND privilege_type<>'SELECT'""").fetchall()}
            assert {k: after[k] for k in before} == before  # 024's and 028's views unchanged.
            assert set(after) - set(before) == set(NEW_VIEWS)
            assert set(NEW_VIEWS) <= grants and not others
            # This release's schema is 30: 030 (JEV_TOP_K_SELECTION_V3, functions only) follows.
            from tests.test_selection_topk_v3 import apply_migration_030

            apply_migration_030(root)
            from tests.test_risk_v5 import apply_migration_031

            apply_migration_031(root)  # 031 (package plugin-c3) changes no view either.
            with psycopg.connect(owner_url) as conn:
                assert dict(conn.execute(definitions).fetchall()) == after
            Repository(role_url(localdb.connection_url(root), "catalyst_app")).check_role()
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def test_the_public_role_reads_the_new_views_and_writes_nothing(er, demo):  # noqa: F811
    ledger, now, info = demo
    with public(er) as conn:
        rows = {v: conn.execute(f"SELECT * FROM lab.{v}").fetchall() for v in NEW_VIEWS}
        for statement in ("INSERT INTO lab.managed_events(event_id,idempotency_key,kind,body) "
                          "VALUES(gen_random_uuid(),'x','STATS_EXCLUSION','{}')",
                          "SELECT * FROM lab.managed_events LIMIT 1"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
    assert len(rows["public_page_equity"]) > 300  # Every 5 minutes for about 30 hours.
    assert {r["basis"] for r in rows["public_page_day_starts"]} == {
        "ALPACA_LAST_EQUITY", "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2"}
    assert [r["kind"] for r in rows["public_page_account_marks"]] == ["SOFT_LIMIT"]
    excluded = rows["public_page_stats_exclusions"]
    assert excluded and {r["day"] for r in excluded} == {info["excluded_day"]}
    assert {r["reason"] for r in excluded} == {"UNMONITORED_OPERATION_FIXTURE"}
    text = json.dumps(rows, default=str)
    assert not UUID.search(text)  # No setup, account or order identifier in any new view.


# --- ACCOUNT_EQUITY_SNAPSHOT_V1 ---------------------------------------------------------------


def test_the_snapshot_body_and_bucket():
    at = datetime(2026, 10, 3, 12, 7, 31, tzinfo=UTC)
    assert bucket_start(at) == datetime(2026, 10, 3, 12, 5, tzinfo=UTC)
    body = snapshot_body({"equity": "9601.37", "cash": "8000", "long_market_value": "1601.37",
                          "last_equity": "9590.78"},
                         [{"unrealized_pl": "10.5"}, {"unrealized_pl": "-2.25"}], at)
    assert (body["equity"], body["unrealized_pl"], body["positions"]) == (
        D("9601.37"), D("8.25"), 2)
    assert body["version"] == "ACCOUNT_EQUITY_SNAPSHOT_V1" and body["observed_at"] == \
        at.isoformat()
    assert snapshot_body({"equity": "0"}, [], at) is None
    assert snapshot_body({"equity": "abc"}, [], at) is None
    partial = snapshot_body({"equity": "1"}, [{"unrealized_pl": None}], at)
    assert partial["unrealized_pl"] is None  # Unknown, never summed as zero.


def test_the_trader_records_one_snapshot_per_five_minutes_from_the_tick_it_already_makes(mx):  # noqa: F811
    from catalyst_lab.managed_account_safety import ManagedAccountSafety

    engine, venue, _ = mx
    safety = ManagedAccountSafety(engine, None)

    def snapshots():
        with engine.repo.connect() as conn:
            return conn.execute("SELECT body FROM lab.managed_events WHERE kind=%s "
                                "ORDER BY event_seq", (SNAPSHOT_EVENT,)).fetchall()

    def account_reads():
        return sum(1 for method, path, _ in venue.calls if (method, path) == ("GET",
                                                                             "/v2/account"))

    venue.now = venue.now.replace(minute=1, second=0)
    reads = account_reads()
    safety.tick()
    per_tick = account_reads() - reads
    venue.equity = "10012.50"
    safety.tick()
    venue.now += timedelta(minutes=2)
    safety.tick()
    assert [r["body"]["equity"] for r in snapshots()] == ["10000"]  # One per bucket.
    venue.now += timedelta(minutes=3)  # The next five-minute bucket.
    safety.tick()
    assert [r["body"]["equity"] for r in snapshots()] == ["10000", "10012.50"]
    # No extra broker read: every tick read the account exactly as often as the first did.
    assert account_reads() - reads == 4 * per_tick
    # A failure while recording never reaches the account tick.
    venue.now += timedelta(minutes=5)
    safety.equity_snapshots = type("Broken", (), {"record": staticmethod(
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))})()
    assert safety.tick() is None
    assert len(snapshots()) == 2


def test_the_recorder_skips_a_settled_bucket_without_a_query():
    class Conn:
        def __init__(self):
            self.queries = 0

        def execute(self, *args):
            self.queries += 1
            return type("R", (), {"fetchone": staticmethod(lambda: None)})()

    class Store:
        events = []

        def event(self, conn, kind, body, key=None):
            self.events.append((kind, key))

    recorder, conn, store = EquitySnapshotRecorder(), Conn(), Store()
    assert recorder.record(conn, store, {"equity": "1"}, [], T0) == "RECORDED"
    assert recorder.record(conn, store, {"equity": "2"}, [], T0 + timedelta(minutes=4)) == \
        "ALREADY_RECORDED"
    assert conn.queries == 1 and len(store.events) == 1
    assert recorder.record(conn, store, {"equity": "0"}, [], T0 + timedelta(minutes=5)) == \
        "NO_EQUITY"


# --- STATS_EXCLUSION_V1 ---------------------------------------------------------------------


def history(er):  # noqa: F811
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"),
                         row_factory=dict_row) as conn:
        return {
            "fills": conn.execute("SELECT * FROM lab.managed_fills ORDER BY fill_id").fetchall(),
            "states": conn.execute("SELECT * FROM lab.managed_states ORDER BY setup_id")
            .fetchall(),
            "events": conn.execute("SELECT event_seq, kind, body FROM lab.managed_events "
                                   "WHERE kind<>%s ORDER BY event_seq", (EXCLUSION_EVENT,))
            .fetchall(),
        }


def test_an_exclusion_is_one_idempotent_record_and_changes_no_history(er, demo):  # noqa: F811
    ledger, now, info = demo
    day = info["excluded_day"]
    before = history(er)
    again = record_exclusion(ledger.store, day, "UNMONITORED_OPERATION_FIXTURE", now=now)
    assert again["status"] == "ALREADY_RECORDED"
    assert again["event_seq"] == info["exclusion"]["event_seq"]
    assert history(er) == before  # No fill, state or event changed or added.
    with ledger.store.repo.connect() as conn:
        rows = conn.execute("SELECT body FROM lab.managed_events WHERE kind=%s",
                            (EXCLUSION_EVENT,)).fetchall()
    assert len(rows) == 1
    body = rows[0]["body"]
    assert (body["version"], body["day"], body["reason"], body["effect"]) == (
        "STATS_EXCLUSION_V1", day.isoformat(), "UNMONITORED_OPERATION_FIXTURE",
        "PERFORMANCE_STATISTICS_ONLY")
    # The scope: every trade entered or closed that New York day, by setup id.
    with ledger.store.repo.connect() as conn:
        from catalyst_lab.stats_exclusion import resolve_scope

        scope = resolve_scope(conn, day)
    assert sorted(body["setup_ids"]) == sorted(s for s, _ in scope)
    assert set(excluded_setups_of(ledger.store.repo)) == set(body["setup_ids"])


def test_an_exclusion_is_refused_for_an_unknown_day_an_open_trade_or_a_day_not_over(er, demo):  # noqa: F811
    ledger, now, info = demo
    today = now.astimezone(NY).date()
    for day, code in ((today - timedelta(days=40), "STATS_EXCLUSION_DAY_UNKNOWN"),
                      (today - timedelta(days=1), "STATS_EXCLUSION_DAY_HAS_OPEN_TRADES"),
                      (today, "STATS_EXCLUSION_DAY_NOT_OVER")):
        with pytest.raises(StatsExclusionRefused, match=code):
            record_exclusion(ledger.store, day, "UNMONITORED_OPERATION_FIXTURE", now=now)
    for reason in ("lower case", "", "X" * 80):
        with pytest.raises(StatsExclusionRefused, match="STATS_EXCLUSION_REASON_INVALID"):
            record_exclusion(ledger.store, info["excluded_day"], reason, now=now)


def test_the_cloud_command_appends_through_the_traders_role(er, demo):  # noqa: F811
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.cloud_config import DATABASE_NAME

    ledger, now, info = demo
    environ = {"MANAGED_DATABASE_URL": f"host=/tmp/x user=catalyst_risk password=p "
                                       f"dbname={DATABASE_NAME}"}
    repo = RiskRepository(role_url(er.database_url, "catalyst_risk"))
    out = io.StringIO()
    assert cloud_runtime.exclude_from_stats(info["excluded_day"], "UNMONITORED_OPERATION_FIXTURE",
                                            environ, out=out, repository=repo, now=now) == 0
    printed = json.loads(out.getvalue())
    assert (printed["status"], printed["mode"]) == ("ALREADY_RECORDED", "PAPER_ONLY")
    out = io.StringIO()
    assert cloud_runtime.exclude_from_stats(date(2020, 1, 1), "UNMONITORED_OPERATION_FIXTURE",
                                            environ, out=out, repository=repo, now=now) != 0
    assert out.getvalue().strip() == "CLOUD_STATS_EXCLUSION_REFUSED: STATS_EXCLUSION_DAY_UNKNOWN"
    out = io.StringIO()
    assert cloud_runtime.exclude_from_stats(info["excluded_day"], "x", {}, out=out) != 0
    assert "MANAGED_DATABASE_URL" in out.getvalue() and "password" not in out.getvalue()


def test_the_scorecard_and_the_weekly_review_show_both_views(er, demo):  # noqa: F811
    from catalyst_lab.scorecard import compute_scorecard
    from catalyst_lab.weekly_review import exclusion_views

    ledger, now, info = demo
    repo = Repository(er.database_url)
    body = compute_scorecard(repo, info["excluded_day"], now=now)
    day = body["windows"]["1d"]["overall"]
    excluded = day["stats_exclusions"]
    assert excluded["excluded_trades"] >= 1 and excluded["days"] == [
        info["excluded_day"].isoformat()]
    assert day["results"]["trades_closed"] == (
        day["results_excluding_exclusions"]["trades_closed"] + excluded["excluded_trades"])
    trades = {"a": {"r_net": D("1"), "win": True, "closed_at": T0},
              "b": {"r_net": D("-2"), "win": False, "closed_at": T0},
              "c": {"r_net": None, "win": False, "closed_at": T0 - timedelta(days=9)}}
    views = exclusion_views(trades, {"b": {"day": "2026-10-02", "reason": "X"}},
                            T0 - timedelta(days=1))
    assert views["all_before_week_end"]["all_trades"]["closed"] == 3
    assert views["all_before_week_end"]["excluding_exclusions"] == {
        "closed": 2, "wins": 1, "win_rate": D("0.5000"), "r_net_count": 1,
        "mean_r_net": D("1.0000")}
    assert views["week"]["excluded_trades"] == 1 and views["days"] == ["2026-10-02"]


# --- The document -------------------------------------------------------------------------------


def trade_numbers(value, out=None):
    """Every ``trade_no`` anywhere in a document (lists and dicts, recursively)."""
    out = set() if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "trade_no" and item is not None:
                out.add(item)
            elif key in ("trade_nos",):
                out.update(item or [])
            else:
                trade_numbers(item, out)
    elif isinstance(value, list):
        for item in value:
            trade_numbers(item, out)
    return out


def test_the_document_has_the_account_curve_daily_performance_and_positions(er, demo):  # noqa: F811
    ledger, now, info = demo
    snapshot, doc = document_of(er, fixture_data=True)
    assert doc["dashboard_version"] == "EXPERIMENT_DASHBOARD_V3"
    a = doc["account"]
    # The hero is the real broker equity; Today is real.
    assert a["equity_basis"] in {"SNAPSHOT", "RISK_DECISION"} and D(a["equity_usd"]) > 0
    assert D(a["today_usd"]) == D(a["equity_usd"]) - D(a["day_start_usd"])
    assert doc["limits"]["measure"] == "ACCOUNT_EQUITY"
    assert doc["limits"]["account_equity_usd"] == a["equity_usd"]
    assert doc["limits"]["pnl_usd"] == a["today_usd"]
    # PAGE_EXCLUSION_V2: "since start" is the included trades' trading P&L.
    closed, live = doc["past"]["closed_trades"], doc["live_trades"]
    realized = sum(D(t["pnl_usd"]) for t in closed if t["pnl_usd"] is not None)
    opened = sum(D(t["pnl_usd"]) for t in live if t["pnl_usd"] is not None)
    assert D(a["since_start_usd"]) == D(a["trading_pnl_usd"]) == realized + opened
    assert a["trading_pnl_label"] == "Trading P&L excl. " + info["excluded_day"].strftime(
        "%b %-d")
    e = doc["equity"]
    assert e["mode"] == "TRADING_PNL" and D(e["capital_usd"]) > 0
    sources = [p["basis"] for p in e["points"]]
    assert sources[0] == "REALIZED" and D(e["points"][0]["pnl_usd"]) == 0
    assert sources[-1] == "NOW" and D(e["points"][-1]["pnl_usd"]) == realized + opened
    assert "SNAPSHOT" in sources
    assert e["max_drawdown"]["pct"] == a["max_drawdown_pct"]
    assert D(e["max_drawdown"]["pct"]) <= 0
    assert [m["kind"] for m in e["marks"]] == ["SOFT_LIMIT"]  # Not on the excluded day.
    excluded_day = info["excluded_day"].isoformat()
    assert doc["exclusions"]["trades"] == info["exclusion"]["trades"] > 0
    assert doc["exclusions"]["days"] == [{"day": excluded_day,
                                          "trades": info["exclusion"]["trades"]}]
    assert doc["exclusions"]["text"].startswith("Excludes ")
    assert excluded_day not in {d["day"] for d in doc["daily"]}
    assert excluded_day not in {d["day"] for d in doc["past"]["days"]}
    shown = [D(t["pnl_usd"]) for t in closed if t["pnl_usd"] is not None]
    # Each figure is rounded to cents on its own: the sums agree within a cent per trade.
    assert abs(sum(D(d["net_usd"]) for d in doc["daily"]) - sum(shown)) <= D("0.01") * len(
        shown)
    assert D(doc["past"]["days"][-1]["cumulative_pnl_usd"]) == D(doc["overall"]["pnl_usd"])
    assert D(a["fees_paid_usd"]) == sum(D(t["fees_usd"]) for t in closed if t["fees_usd"])
    since = doc["performance"]["rows"][0]
    assert since["trades"] == len([t for t in closed if t["pnl_r"] is not None])
    for t in live:
        p = t["position"]
        assert p and D(p["stop"]) < D(p["target"]) and p["mark_basis"] == "LEDGER"
    assert any(t["jev_check"] and "Invalidation met?" in t["jev_check"]["text"]
               for t in live)


def test_excluded_trades_vanish_from_every_public_list_and_page(er, demo):  # noqa: F811
    ledger, now, info = demo
    with public(er) as conn:
        excluded = {r["trade_no"] for r in conn.execute(
            "SELECT trade_no FROM lab.public_page_stats_exclusions").fetchall()}
    assert excluded
    app = create_experiment_app(public_url(er.database_url), environ={}, market=FakeMarket())
    with TestClient(app) as c:
        doc = c.get("/api/public/experiment").json()
        assert not trade_numbers(doc) & excluded
        for section in ("feed", "past", "live_trades", "agents", "performance", "results",
                        "daily", "equity"):
            part = c.get(f"/api/public/experiment/{section}").json()
            assert not trade_numbers(part) & excluded, section
        for number in excluded:
            assert c.get(f"/trade/{number}").status_code == 404
            assert c.get(f"/api/public/experiment/trades/{number}/chart").status_code == 404
        kept = doc["past"]["closed_trades"][0]["trade_no"]
        assert c.get(f"/trade/{kept}").status_code == 200
        assert c.get(f"/trade/{doc['live_trades'][0]['trade_no']}").status_code == 200


def test_every_reader_with_and_without_the_exclusion(er, demo):  # noqa: F811
    ledger, now, info = demo
    with public(er) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = read_snapshot(conn)
    with_ = json.loads(json.dumps(build_dashboard(snapshot), default=str))
    without = json.loads(json.dumps(build_dashboard({**snapshot, "exclusions": []}),
                                    default=str))
    day = info["excluded_day"].isoformat()
    kept = {t["trade_no"] for t in with_["past"]["closed_trades"]}
    gone = [t for t in without["past"]["closed_trades"] if t["trade_no"] not in kept]
    assert without["exclusions"]["trades"] == 0 and without["exclusions"]["text"] is None
    # The scope: every trade entered or closed on the day (one closed the next day).
    n = with_["exclusions"]["trades"]
    assert len(gone) == n == info["exclusion"]["trades"] > 0
    assert with_["overall"]["closed"] == without["overall"]["closed"] - n
    gone_pnl = sum(D(t["pnl_usd"]) for t in gone)
    cents = D("0.01") * n  # Each trade's P&L is rounded to cents on its own.
    assert abs(D(with_["overall"]["pnl_usd"]) - (D(without["overall"]["pnl_usd"]) - gone_pnl)
               ) <= cents
    assert abs(D(with_["account"]["since_start_usd"]) - (
        D(without["account"]["since_start_usd"]) - gone_pnl)) <= cents
    gone_fees = sum(D(t["fees_usd"]) for t in gone if t["fees_usd"])
    assert abs(D(with_["account"]["fees_paid_usd"]) - (
        D(without["account"]["fees_paid_usd"]) - gone_fees)) <= cents
    assert len(with_["feed"]) <= len(without["feed"])
    assert not any(line["at"] and datetime.fromisoformat(line["at"]).astimezone(NY).date()
                   .isoformat() == day and line["kind"] in ("RUN", "SELECTION")
                   for line in with_["feed"])
    assert day in {d["day"] for d in without["past"]["days"]}
    assert day not in {d["day"] for d in with_["past"]["days"]}
    assert with_["performance"]["rows"][0]["trades"] == without["performance"]["rows"][0][
        "trades"] - n
    assert with_["results"]["since_start"]["trades"] == without["results"]["since_start"][
        "trades"] - n
    # The real account figures do not change.
    for key in ("equity_usd", "today_usd", "day_start_usd", "open_risk_pct"):
        assert with_["account"][key] == without["account"][key], key
    assert with_["limits"] == without["limits"] and with_["live_trades"] == without[
        "live_trades"]
    # The capital (the walk's start, over every trade) is the same in both.
    assert with_["equity"]["capital_usd"] == without["equity"]["capital_usd"]


def test_the_trading_pnl_curve_and_its_drawdown():
    day = date(2026, 9, 28)
    rows = [{"day": day, "day_start_equity": D("10000"), "observed_at": _ny(day, 0, 4),
             "basis": "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2", "session_row_equity": None}]
    trades = [{"trade_no": i + 1, "entry_at": _ny(day, 9 + i), "exit_at": _ny(day, 10 + i),
               "pnl_usd": D(v)} for i, v in enumerate(("50", "-80", "20"))]
    excluded = [{"trade_no": 9, "entry_at": _ny(day + timedelta(days=1), 9),
                 "exit_at": _ny(day + timedelta(days=1), 10), "pnl_usd": D("-300")}]
    snapshot = {"day_starts": rows, "equity_snapshots": [], "account_marks": []}
    live = [{"pnl_usd": D("5")}]
    curve = experiment_v3.pnl_section(snapshot, trades, trades + excluded, live,
                                      {(day + timedelta(days=1)).isoformat()}, None,
                                      _ny(day + timedelta(days=2), 9))
    assert curve["capital_usd"] == D("10000")
    assert [p["pnl_usd"] for p in curve["points"]] == [D("0"), D("50"), D("-30"), D("-10"),
                                                       D("-5")]
    assert curve["trading_pnl_usd"] == D("-5") and curve["trading_pnl_pct"] == D("-0.05")
    worst = curve["max_drawdown"]
    assert worst["drawdown_usd"] == D("-80.00")
    assert worst["pct"] == (D("-80") / D("10050") * 100).quantize(D("0.01"))
    b = experiment_v3.benchmark([{"at": curve["points"][0]["at"]}],
                                [{"t": _ny(day, 0), "c": D("100")},
                                 {"t": _ny(day, 1), "c": D("101")}], curve["capital_usd"])
    assert [p["pnl_usd"] for p in b["points"]] == [D("0.00"), D("100.00")]


def _ny(day, hour=0, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).astimezone(UTC)


def test_the_history_is_a_walk_over_closed_trades_from_one_anchor():
    """EQUITY_HISTORY_WALK_V1: every step is a real trade; day starts lie on the walk."""
    d1, d2 = date(2026, 9, 28), date(2026, 9, 29)
    rows = [{"day": d1, "day_start_equity": D("10000"), "observed_at": _ny(d1, 0, 4),
             "basis": "ALPACA_LAST_EQUITY", "session_row_equity": D("10000")},
            {"day": d2, "day_start_equity": D("9970"), "observed_at": _ny(d2, 0, 4),
             "basis": "ALPACA_LAST_EQUITY", "session_row_equity": D("9970")},
            {"day": date(2026, 9, 30), "day_start_equity": D("9993.5"),
             "observed_at": _ny(date(2026, 9, 30), 0, 5),
             "basis": "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2", "session_row_equity": None}]
    closed = [{"trade_no": 1, "entry_at": _ny(d1, 9), "exit_at": _ny(d1, 11), "pnl_usd": D("-12")},
              {"trade_no": 2, "entry_at": _ny(d2, 9), "exit_at": _ny(d2, 10), "pnl_usd": D("5.5")},
              {"trade_no": 3, "entry_at": _ny(d2, 9), "exit_at": _ny(d2, 12), "pnl_usd": None}]
    points = experiment_v3.equity_points(rows, closed, [], (_ny(date(2026, 9, 30), 9),
                                                            D("9997")))
    assert [(p["equity"], p.get("kind")) for p in points] == [
        (D("10000"), "START"), (D("9988"), "TRADE_EXIT"), (D("9988"), "DAY_START"),
        (D("9993.5"), "TRADE_EXIT"), (D("9993.5"), "DAY_START"), (D("9997"), None)]
    assert points[4]["at"] == _ny(date(2026, 9, 30), 0, 5)  # The anchor itself.
    # The 9-29 row (9,970) disagrees with the walk (9,988): it sets nothing, unconfirmed.
    assert points[2]["confirmed"] is False
    snaps = [{"at": _ny(d2, 10, 30), "equity": D("9994")}]
    cut = experiment_v3.equity_points(rows[:2], closed, snaps)  # Anchor: the first snapshot.
    assert [(p["equity"], p["source"]) for p in cut] == [
        (D("9994") + D("12") - D("5.5"), "RECONSTRUCTED"), (D("9988.5"), "RECONSTRUCTED"),
        (D("9988.5"), "RECONSTRUCTED"), (D("9994.0"), "RECONSTRUCTED"),
        (D("9994"), "SNAPSHOT")]
    assert experiment_v3.equity_points(rows[:2], closed, []) == []  # No anchor: no history.


def test_lagging_session_rows_make_no_phantom_dip():
    """The live shape of 2026-10-03: Alpaca's last_equity rows lag a day (each day's row is the
    day before's start), and only 10-03 has a RISK_SESSION_BASELINE_V2 basis. The old curve
    dipped to the lagging rows; the walk never leaves the trades' path."""
    days = [date(2026, 9, 28) + timedelta(days=i) for i in range(5)]
    daily = [D("-28.10"), D("7.83"), D("5.35"), D("-33.56"), D("-286.52")]
    anchor = D("9590.78")  # The 10-03 V2 basis: every trade closed before it.
    start = anchor - sum(daily)
    true_starts, equity = [], start
    for pnl in daily:
        true_starts.append(equity)
        equity += pnl
    rows = [{"day": d, "day_start_equity": true_starts[max(0, i - 1)] - D("150") * (i % 2),
             "observed_at": _ny(d, 0, 4), "basis": "ALPACA_LAST_EQUITY",
             "session_row_equity": true_starts[max(0, i - 1)] - D("150") * (i % 2)}
            for i, d in enumerate(days)]
    rows.append({"day": date(2026, 10, 3), "day_start_equity": anchor,
                 "observed_at": _ny(date(2026, 10, 3), 0, 5),
                 "basis": "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2", "session_row_equity": D("9877.30")})
    closed, n = [], 0
    for d, pnl in zip(days, daily, strict=True):
        for k, part in enumerate((pnl / 2, pnl - pnl / 2)):  # Two trades a day.
            n += 1
            closed.append({"trade_no": n, "entry_at": _ny(d, 9 + k), "exit_at": _ny(d, 12 + k),
                           "pnl_usd": part})
    snaps = [{"at": _ny(date(2026, 10, 3), 13), "equity": D("9601.37")}]
    points = experiment_v3.equity_points(rows, closed, snaps)
    walk = [p for p in points if p["source"] == "RECONSTRUCTED"]
    allowed = {start}
    running = start
    for t in closed:
        running += t["pnl_usd"]
        allowed.add(running)
    assert {p["equity"] for p in walk} <= allowed  # Every value is on the trade walk.
    for p in walk:  # Each day start equals the walk at that midnight.
        if p.get("kind") == "DAY_START":
            index = [d for d in days + [date(2026, 10, 3)]].index(_ny_day_of(p["at"]))
            expected = true_starts[index] if index < len(true_starts) else anchor
            assert p["equity"] == expected
    _, worst = experiment_v3.drawdowns(points)
    peak = max(allowed)
    trough = min(p["equity"] for p in points)
    assert trough == anchor  # No phantom dip below the real low.
    assert worst["pct"] == ((anchor - peak) / peak * 100).quantize(D("0.01"))
    assert worst["drawdown_usd"] == (anchor - peak).quantize(D("0.01"))


def _ny_day_of(at):
    return at.astimezone(NY).date()


def test_drawdown_math():
    pts = [{"at": T0 + timedelta(hours=i), "equity": D(v), "source": "SNAPSHOT"}
           for i, v in enumerate(("100", "110", "99", "104", "121", "115.95"))]
    series, worst = experiment_v3.drawdowns(pts)
    assert [s["pct"] for s in series] == [D("0.00"), D("0.00"), D("-10.00"), D("-5.45"),
                                          D("0.00"), D("-4.17")]
    assert worst["pct"] == D("-10.00") and worst["drawdown_usd"] == D("-11.00")
    assert (worst["peak_at"], worst["trough_at"]) == (pts[1]["at"], pts[2]["at"])
    assert experiment_v3.drawdowns([]) == ([], None)


def trades_with(rs, usd=None):
    usd = usd or [D(r) * 10 for r in rs]
    return [{"trade_no": i + 1, "pnl_r": D(r), "pnl_usd": D(u), "fees_usd": D("1"),
             "exit_at": T0} for i, (r, u) in enumerate(zip(rs, usd, strict=True))]


def test_expectancy_profit_factor_and_the_30_trade_minimum():
    rs = ["2", "-1", "-1", "0.5", "-0.5"] * 6  # 30 trades.
    row = experiment_v3.stats_row(trades_with(rs), "since_start", "Since start")
    assert row["enough"] and row["trades"] == 30 and (row["wins"], row["losses"]) == (12, 18)
    assert row["win_rate"] == D("0.4000") and row["expectancy_r"] == D("0")
    assert row["avg_win_r"] == D("1.25") and row["avg_loss_r"] == D("-2.5") / 3
    assert row["profit_factor"] == D("1.00") and row["fees_usd"] == D("30")
    small = experiment_v3.stats_row(trades_with(rs[:29]), "since_start", "Since start")
    assert not small["enough"] and small["win_rate"] is None and small["profit_factor"] is None
    assert small["win_rate_all"] is not None and small["trades"] == 29  # Counts stay.


def test_r_histogram_bins():
    h = experiment_v3.r_histogram(trades_with(["-2", "-1.99", "-3.5", "0", "-0.01", "2.99", "3",
                                               "7"]))
    counts = {str(b["lo"]): b["count"] for b in h["bins"] if b["count"]}
    assert len(h["bins"]) == 20 and h["bins"][0]["open_low"] and h["bins"][-1]["open_high"]
    assert counts == {"-2.00": 3, "0.00": 1, "-0.25": 1, "2.75": 3}
    assert (h["below"], h["above"], h["trades"]) == (1, 2, 8)
    assert h["expectancy_r"] == sum(D(r) for r in ("-2", "-1.99", "-3.5", "0", "-0.01", "2.99",
                                                    "3", "7")) / 8


def test_daily_columns_with_fees_and_pending_fees():
    closed = [
        {"trade_no": 1, "exit_at": datetime(2026, 9, 30, 15, tzinfo=UTC), "pnl_usd": D("10"),
         "gross_pnl_usd": D("12"), "fees_usd": D("2"), "fees_pending": False},
        {"trade_no": 2, "exit_at": datetime(2026, 9, 30, 20, tzinfo=UTC), "pnl_usd": D("-5"),
         "gross_pnl_usd": D("-5"), "fees_usd": None, "fees_pending": True},
        {"trade_no": 3, "exit_at": datetime(2026, 10, 2, 15, tzinfo=UTC), "pnl_usd": D("-1"),
         "gross_pnl_usd": D("0"), "fees_usd": D("1"), "fees_pending": False},
    ]
    days = experiment_v3.daily_section(closed, [{"day": "2026-09-30", "short": "Uptrend"}],
                                       {"2026-10-02"}, date(2026, 10, 3))
    # PAGE_EXCLUSION_V2: the excluded day is not listed at all.
    assert [d["day"] for d in days] == ["2026-09-30", "2026-10-01", "2026-10-03"]
    first = days[0]
    assert (first["net_usd"], first["gross_usd"], first["fees_usd"], first["trades"],
            first["won"], first["fees_pending"], first["market"]) == (
        D("5"), D("7"), D("2"), 2, 1, 1, "Uptrend")
    assert days[1]["trades"] == 0 and not any(d["excluded"] for d in days)


def test_the_position_bar_and_its_r_distances():
    t = {"stop": "95", "target": "111", "entry": "100", "limit": "100.10",
         "planned_stop": "95", "price": "103.06", "price_at": "2026-10-03T12:00:00+00:00"}
    p = experiment_v3.position(t)
    assert p["entry_at_fraction"] == D("0.3125") and p["mark_fraction"] == D("0.5038")
    assert p["risk_per_unit"] == D("5.10")
    assert (p["r_to_stop"], p["r_to_target"]) == (D("-1.58"), D("1.56"))
    assert experiment_v3.position({**t, "target": "90"}) is None


def test_after_the_sale_from_bars():
    exit_at = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    trade = {"exit_at": exit_at.isoformat(), "exit": "100", "entry": "102", "limit": "102",
             "planned_stop": "98", "pnl_r": "-0.5"}
    bars = [{"t": exit_at + timedelta(minutes=5 * i), "c": D("100") + D(i) / 10}
            for i in range(12 * 5)]  # Five hours of 5-minute bars.
    out = experiment_v3.after_exit(trade, bars, exit_at + timedelta(hours=5))
    one, four, day = out["points"]
    assert (one["price"], one["change_pct"], one["r"]) == ("101.1", "1.10", "0.28")
    assert (four["price"], four["change_pct"]) == ("104.7", "4.70")
    assert day == {"label": "+24 h", "hours": 24, "pending": True,
                   "at": (exit_at + timedelta(hours=24)).isoformat()}
    assert experiment_v3.after_exit(trade, [], exit_at + timedelta(hours=5)) is None


def test_the_benchmark_is_the_starting_equity_in_btc():
    points = [{"at": T0.isoformat(), "equity_usd": "10000"}]
    bars = [{"t": T0 - timedelta(minutes=30), "c": D("60000")},
            {"t": T0 + timedelta(minutes=30), "c": D("61200")}]
    b = experiment_v3.benchmark(points, bars)
    assert b["base_price"] == D("60000") and b["label"] == "BTC buy-and-hold"
    assert [p["value_usd"] for p in b["points"]] == [D("10000.00"), D("10200.00")]
    assert experiment_v3.benchmark(points, [{"t": T0 + timedelta(hours=5),
                                             "c": D("1")}]) is None  # Bars start too late.


def test_halt_and_soft_limit_marks():
    rows = [{"kind": "DAILY_HALT", "day": date(2026, 10, 2), "at": T0, "pnl": D("-290")},
            {"kind": "SOFT_LIMIT", "day": date(2026, 10, 2), "at": T0 - timedelta(hours=1),
             "pnl": D("-199")}, {"kind": "SOFT_LIMIT", "day": None, "at": None, "pnl": None}]
    marks = experiment_v3.account_marks(rows)
    assert [(m["kind"], m["pnl_usd"]) for m in marks] == [("DAILY_HALT", D("-290")),
                                                         ("SOFT_LIMIT", D("-199"))]
    assert marks[0]["text"].startswith("Daily loss limit")


def test_the_exclusion_line():
    rows = [{"trade_no": 3, "day": date(2026, 10, 2), "reason": "UNMONITORED_OPERATION_2026-10-02"}]
    block = experiment_v3.exclusion_block(rows, [{"trade_no": 3}, {"trade_no": 4}])
    assert block["text"] == "Excludes Oct 2 (unmonitored day)" and block["trades"] == 1
    assert experiment_v3.exclusion_block([], [])["text"] is None


# --- Public market data: server side, keyless, cached, degrading gracefully -------------------


def test_marks_and_the_benchmark_come_from_public_data_and_degrade_to_nothing(er, demo):  # noqa: F811
    ledger, now, info = demo
    _, doc = document_of(er)
    market = FakeMarket(quote=D("999"))
    enriched = experiment_v3.enrich(json.loads(json.dumps(doc)), market, now)
    assert enriched["market_data"] == {"source": "ALPACA_PUBLIC_KEYLESS", "quotes": "OK",
                                       "benchmark": "OK"}
    assert all(t["price"] == "999" and t["price_live"] and t["position"]["mark_basis"] ==
               "PUBLIC_QUOTE" for t in enriched["live_trades"])
    assert D(enriched["account"]["open_pnl_usd"]) == sum(D(t["pnl_usd"])
                                                        for t in enriched["live_trades"])
    assert enriched["equity"]["benchmark"]["points"]
    failed = experiment_v3.enrich(json.loads(json.dumps(doc)), FakeMarket(fail=True), now)
    assert failed["market_data"]["quotes"] == failed["market_data"]["benchmark"] == "UNAVAILABLE"
    assert failed["equity"]["benchmark"] is None
    assert failed["live_trades"] == doc["live_trades"]  # Nothing estimated, nothing changed.
    assert experiment_v3.enrich(doc, None) is doc


def test_the_chart_route_serves_candles_after_the_sale_or_an_unavailable_status(er, demo):  # noqa: F811
    ledger, now, info = demo
    for market, status in ((FakeMarket(), "OK"), (FakeMarket(fail=True), "UNAVAILABLE"),
                           (None, "UNAVAILABLE")):
        app = create_experiment_app(public_url(er.database_url), environ={}, market=market)
        with TestClient(app) as c:
            doc = c.get("/api/public/experiment").json()
            closed = doc["past"]["closed_trades"][-1]
            body = c.get(f"/api/public/experiment/trades/{closed['trade_no']}/chart").json()
            assert body["status"] == status, market
            if status == "OK":
                assert body["candles"] and body["after"] and body["after_exit"]["points"]
                assert {"t", "o", "h", "l", "c", "v"} == set(body["candles"][0])
            else:
                assert body["candles"] == [] and body["after_exit"] is None
            assert c.get("/api/public/experiment/trades/9999/chart").status_code == 404
            page = c.get(f"/trade/{closed['trade_no']}")
            assert page.status_code == 200
    assert {"account", "equity", "daily", "performance"} <= set(doc)


def test_the_keyless_reader_allows_two_routes_and_caches():
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.headers)))
        if request.url.path.endswith("/latest/quotes"):
            return httpx.Response(200, json={"quotes": {"PEPE/USD": {
                "bp": 0.0000042344, "ap": 0.0000042401, "t": "2026-10-03T12:00:00Z"}}})
        return httpx.Response(200, json={"bars": {"BTC/USD": [
            {"t": "2026-10-03T11:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3},
            {"t": "bad"}]}, "next_page_token": None})

    clock = [0.0]
    market = PublicMarketData(transport=httpx.MockTransport(handler), clock=lambda: clock[0],
                              now=lambda: datetime(2026, 10, 3, 12, tzinfo=UTC))
    quotes = market.latest_quotes(["PEPE/USD", "nope"])
    assert quotes["PEPE/USD"]["bid"] == D("0.0000042344")
    market.latest_quotes(["PEPE/USD"])
    assert len(seen) == 1  # Cached for 15 s.
    clock[0] = 16
    market.latest_quotes(["PEPE/USD"])
    assert len(seen) == 2
    bars = market.bars("BTC/USD", datetime(2026, 10, 3, 10, 30, tzinfo=UTC),
                       datetime(2026, 10, 3, 11, 40, tzinfo=UTC), "1Hour")
    assert [b["c"] for b in bars] == [D("1.5")]  # The malformed row is dropped.
    for _, headers in seen:
        assert not {"authorization", "apca-api-key-id", "apca-api-secret-key"} & set(headers)
    from catalyst_lab.public_market import _KeylessTransport

    transport = _KeylessTransport(httpx.MockTransport(handler))
    for request in (httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/bars"),
                    httpx.Request("POST", "https://data.alpaca.markets/v1beta3/crypto/us/bars"),
                    httpx.Request("GET", "https://paper-api.alpaca.markets/v2/account")):
        with pytest.raises(PublicMarketError, match="ROUTE_NOT_ALLOWED"):
            transport.handle_request(request)
    with pytest.raises(PublicMarketError, match="KEYLESS"):
        transport.handle_request(httpx.Request(
            "GET", "https://data.alpaca.markets/v1beta3/crypto/us/bars",
            headers={"APCA-API-KEY-ID": "x"}))


def test_a_failed_read_is_cached_briefly_and_never_raises_its_body():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="secret upstream body")

    market = PublicMarketData(transport=httpx.MockTransport(handler), clock=lambda: 0.0)
    for _ in range(3):
        with pytest.raises(PublicMarketError) as error:
            market.latest_quotes(["BTC/USD"])
        assert str(error.value) == "PUBLIC_MARKET_HTTP_503"
    assert len(calls) == 1
    assert parse_bar({"t": "2026-10-03T11:00:00Z", "o": 0, "h": 1, "l": 1, "c": 1}) is None


def test_the_page_shell_has_the_v3_sections():
    html = render_page({"title": "t", "fixture_data": False, "data_label": None})
    order = [html.index(f'id="{key}-body"') for key in (
        "account", "equity", "performance", "open", "tiles", "agents", "closed", "past")]
    assert order == sorted(order)
    script = (Path(localdb.__file__).with_name("static") / "experiment.js").read_text()
    for hook in ("'1D'", "'7D'", "'30D'", "'All'", "'BTC buy-and-hold'", "View as table",
                 "'Drawdown from the peak'", "'R-multiple distribution'", "'Daily P&L'",
                 "Reconstructed from ", "tabindex: 0", "ArrowLeft", "'refreshing'",
                 "SUB = '₀₁₂₃₄₅₆₇₈₉'"):
        assert hook in script, hook
    css = (Path(localdb.__file__).with_name("static") / "experiment.css").read_text()
    for token in ("--series-account: #1D4ED8", "--series-btc: #C2410C", ".kpis",
                  "min-height: 30px", "min-height: 24px", ".refreshing .chart"):
        assert token in css, token


def test_an_unexpected_market_error_never_breaks_the_page(er, demo):  # noqa: F811
    class Broken:
        def bars(self, *args):
            raise RuntimeError("unexpected")

        def latest_quotes(self, symbols):
            raise TypeError("unexpected")

    app = create_experiment_app(public_url(er.database_url), environ={}, market=Broken())
    with TestClient(app) as c:
        doc = c.get("/api/public/experiment").json()
        assert doc["market_data"]["quotes"] == "UNAVAILABLE"
        number = doc["past"]["closed_trades"][0]["trade_no"]
        chart = c.get(f"/api/public/experiment/trades/{number}/chart").json()
        assert chart["status"] == "UNAVAILABLE" and c.get("/").status_code == 200
