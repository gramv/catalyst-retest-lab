"""Measurement/disclosure tests with disposable PostgreSQL and synthetic broker events."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from catalyst_lab.analytics import Analytics, measured_trade, summarize
from catalyst_lab.audit import verify_events
from catalyst_lab.dashboard import create_dashboard_app, csv_cell
from catalyst_lab.market import Observation, Session
from catalyst_lab.measurement import Measurements, MeasurementWorker
from catalyst_lab.repository import Repository
from tests.conftest import NOW
from tests.test_execution import confirmed as confirmed
from tests.test_execution import er as er
from tests.test_execution import paper as paper
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_execution import submitted as submitted


def observation(raw, kind, seconds, **values):
    return Observation.from_wire(
        {
            "T": kind,
            "S": raw["ticker"],
            "t": (NOW + timedelta(seconds=seconds)).isoformat(),
            **values,
        },
        "iex",
    )


def consume(submitted, **values):
    event = submitted["event"](**values)
    submitted["ledger"].consume(event, NOW + timedelta(seconds=values.get("seconds", 1) + 1))
    return event


@pytest.fixture
def measured(er, submitted, raw):
    collector = Measurements(er)
    collector.observe(observation(raw, "q", 0, bp="99.99", ap="100.01"), NOW)
    collector.observe(observation(raw, "t", 0, p="10", s=1, i="before-entry"), NOW)
    first = consume(submitted, kind="partial_fill", qty="4", price="100", seconds=1)
    submitted["ledger"].consume(first, NOW + timedelta(seconds=2))  # duplicate broker event
    consume(submitted, qty="6", price="100.10", cumulative="10", seconds=2)
    for seconds, price in [(5, "99.8"), (40, "101.5")]:
        obs = observation(raw, "t", seconds, p=price, s=10, i=str(seconds))
        assert collector.observe(obs, NOW + timedelta(seconds=seconds)) == 1
        assert collector.observe(obs, NOW + timedelta(seconds=seconds)) == 0
    # Exclude the entry minute: its high may predate the position.
    collector.observe(
        observation(raw, "b", 0, c="100", h="999", l="1", v=100), NOW + timedelta(seconds=61)
    )
    collector.observe(
        observation(raw, "b", 60, c="101", h="102", l="99.5", v=100), NOW + timedelta(seconds=121)
    )
    # Later corrected bar must supersede this bar without deleting its audit record.
    collector.observe(
        observation(raw, "u", 60, c="101", h="101.8", l="99.6", v=110), NOW + timedelta(seconds=122)
    )
    consume(submitted, role="TARGET", price="102", seconds=180)
    collector.observe(
        observation(raw, "t", 190, p="900", s=1, i="after-exit"), NOW + timedelta(seconds=190)
    )
    collector.refresh()
    with er.connect() as conn:
        row = conn.execute(
            "SELECT * FROM lab.trades WHERE candidate_id=%s", (submitted["cid"],)
        ).fetchone()
    return collector, row


def test_projection_replays_partial_fills_and_in_position_snapshots(er, measured):
    collector, row = measured
    assert row["status"] == "CLOSED" and row["entry_qty"] == row["exit_qty"] == 10
    assert row["entry_price"] == D("100.06") and row["broker_paper_pnl"] == D("19.4")
    assert row["initial_risk_dollars"] == D("10.6")
    assert row["planned_filled_risk"] == D("11.5")
    assert row["mfe"] == D("1.74") and row["mae"] == D("-.46")
    assert row["snapshot_count"] == 3
    assert {f["data_feed"] for f in row["feeds_json"]} == {"iex"}
    metrics = measured_trade(row, "ACTUAL_FILLED")
    assert metrics["mfe_r"] == D("17.4") / D("10.6")
    assert metrics["mae_r"] == D("-4.6") / D("10.6")
    assert row["adjusted_r"] is None and row["conservative_adjusted_pnl"] is None
    before = er.export_events()
    collector.refresh()
    assert er.export_events() == before  # idempotent with no new measurement evidence
    assert verify_events(before)["valid"]


def test_missing_snapshots_are_unavailable_not_zero(er, submitted):
    consume(submitted, price="100", seconds=1)
    consume(submitted, role="STOP", price="99", seconds=2)
    Measurements(er).refresh()
    with er.connect() as conn:
        row = conn.execute("SELECT * FROM lab.trades").fetchone()
    assert row["broker_paper_pnl"] == -10
    assert row["mfe"] is None and row["mae"] is None and row["snapshot_count"] == 0
    assert row["coverage_json"]["status"] == "NO_OBSERVATIONS"
    assert measured_trade(row, "ACTUAL_FILLED")["r_multiple"] == -1


def test_busted_broker_fill_withholds_affected_statistics(er, submitted, measured):
    collector, row = measured
    consume(submitted, kind="trade_bust", role="TARGET", seconds=200, status="filled")
    collector.refresh()
    stats = Analytics(er, r_method="ACTUAL_FILLED").stats()
    assert stats["review_required_trades"] == 1
    assert stats["gross_pnl"] is None and stats["win_rate"] is None
    assert stats["curve"] == [] and stats["all_time_gross_pnl"] is None
    assert Analytics(er).trades()[0]["pnl_quality"] == "BROKER_CORRECTION_REVIEW_REQUIRED"
    collector.record_sessions([completed_session()])
    collector.rollup(NOW.date())
    with er.connect() as conn:
        metrics = conn.execute("SELECT metrics_json FROM lab.daily_stats").fetchone()[
            "metrics_json"
        ]
    assert metrics["review_required_trades"] == 1 and metrics["gross_pnl"] is None
    assert verify_events(er.export_events())["valid"]


def test_reporting_role_cannot_mutate_or_refresh(er, measured):
    reporting = Repository(er.database_url.replace("user=catalyst_app", "user=catalyst_reporting"))
    for query in [
        "SELECT * FROM lab.fills",
        "SELECT * FROM lab.trade_events",
        "SELECT lab.refresh_measurements()",
        "DELETE FROM lab.trades",
    ]:
        with reporting.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(query)
    for table in ["exchange_sessions", "measurement_runs"]:
        for command in [
            "UPDATE lab." + table + " SET event_seq=event_seq",
            "DELETE FROM lab." + table,
            "TRUNCATE lab." + table,
        ]:
            with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(command)


def test_observation_cannot_claim_a_different_price_than_its_audited_source(er, measured):
    with (
        er.connect() as conn,
        pytest.raises(psycopg.errors.RaiseException, match="immutable source"),
    ):
        conn.execute("""INSERT INTO lab.market_snapshots(candidate_id,market_data_timestamp,
          price,bid,ask,spread_bps,volume,data_provider,data_feed,observation_resolution,event_id,
          observation_key,observation_type,provider_timestamp,high,low)
          SELECT candidate_id,market_data_timestamp,price+100,bid,ask,spread_bps,volume,
          data_provider,data_feed,observation_resolution,event_id,observation_key,observation_type,
          provider_timestamp,high,low FROM lab.market_snapshots LIMIT 1""")


def completed_session(day=None):
    # DB-clock boundary tests, explicitly synthetic calendar values, never production observations.
    end = datetime.now(UTC) - timedelta(minutes=1)
    return Session(day or NOW.date(), end - timedelta(hours=6), end)


def test_public_calendar_gate_applies_to_log_detail_csv_and_trades(er, measured):
    collector, row = measured
    url = er.database_url.replace("user=catalyst_app", "user=catalyst_reporting")
    with TestClient(create_dashboard_app(url, r_method="ACTUAL_FILLED")) as public:
        assert public.get("/api/public/candidates").json()["items"] == []
        assert public.get("/api/public/trades").json()["items"] == []
        assert public.get("/api/public/candidates/" + str(row["candidate_id"])).status_code == 404
        assert row["ticker"] not in public.get("/api/public/export.csv").text
        stats = public.get("/api/public/stats").json()
        assert stats["closed_trades"] == 1 and row["ticker"] not in str(stats)
        end = datetime.now(UTC) + timedelta(minutes=5)
        collector.record_sessions([Session(NOW.date(), end - timedelta(hours=3), end)])
        assert public.get("/api/public/candidates").json()["items"] == []
        collector.record_sessions([completed_session()])
        assert public.get("/api/public/candidates").json()["items"][0]["ticker"] == row["ticker"]
        detail = public.get("/api/public/candidates/" + str(row["candidate_id"])).json()
        assert D(detail["trade"]["broker_paper_pnl"]) == D("19.4")
        assert row["ticker"] in public.get("/api/public/export.csv").text
        assert public.post("/api/v1/candidates", json={}).status_code == 404
        assert public.get("/api/v1/risk").status_code == 404
        assert "frame-ancestors 'none'" in public.get("/").headers["content-security-policy"]


def test_rollup_is_after_close_only_and_idempotent(er, measured):
    collector, row = measured
    with pytest.raises(psycopg.errors.RaiseException, match="completed session"):
        collector.rollup(NOW.date())
    collector.record_sessions([completed_session()])
    worker = MeasurementWorker(collector)
    worker.tick()
    worker.tick()
    with er.connect() as conn:
        rows = conn.execute("SELECT * FROM lab.daily_stats").fetchall()
    assert len(rows) == 1 and rows[0]["metrics_json"]["closed_trades"] == 1
    assert D(str(rows[0]["metrics_json"]["gross_pnl"])) == D("19.4")
    assert verify_events(er.export_events())["valid"]


def stat_trade(pnl, at, *, r=None):
    return {
        "trade_id": uuid4(),
        "status": "CLOSED",
        "closed_at": at,
        "broker_paper_pnl": D(pnl),
        "r_actual_filled": D(r) if r is not None else None,
        "snapshot_count": 0,
    }


def test_statistics_are_closed_trade_weighted_and_drawdown_never_resets():
    rows = [
        stat_trade("20", NOW, r="2"),
        stat_trade("-10", NOW + timedelta(days=1), r="-1"),
        stat_trade("-15", NOW + timedelta(days=2), r="-1.5"),
        stat_trade("5", NOW + timedelta(days=3), r=".5"),
    ]
    stats = summarize(rows, D("1000"), "ACTUAL_FILLED")
    assert stats["gross_pnl"] == 0 and stats["win_rate"] == D(".5")
    assert stats["profit_factor"] == 1 and stats["max_drawdown_dollars"] == 25
    assert stats["worst_losing_streak"] == 2 and stats["avg_r"] == 0
    assert stats["curve"][-1]["equity"] == 1000
    assert summarize([], method="ACTUAL_FILLED")["win_rate"] is None
    assert summarize([rows[0]], method="ACTUAL_FILLED")["profit_factor"] is None
    assert summarize(rows, method=None)["avg_r"] is None


def test_window_changes_stats_not_equity_history(er, measured):
    stats = Analytics(er, clock=lambda: NOW + timedelta(days=40), r_method="ACTUAL_FILLED").stats(
        days=30
    )
    assert stats["closed_trades"] == 0 and len(stats["curve"]) == 1
    assert D(stats["all_time_gross_pnl"]) == D("19.4")


@pytest.mark.parametrize("text", ['=HYPERLINK("bad")', "+CMD", "-CMD", "@SUM(A1)", " \t=1"])
def test_csv_cannot_become_a_spreadsheet_formula(text):
    assert csv_cell(text).startswith("'")


def test_manual_research_is_separate_delayed_and_corrected_append_only(er, measured):
    from pydantic import ValidationError

    from catalyst_lab.research import import_research, read_research

    day = datetime.now(UTC) - timedelta(days=2)
    raw = {
        "external_id": "research-1",
        "market": "INDIA",
        "strategy_version": "MUSE_RESEARCH_V1",
        "ticker": "RELIANCE",
        "currency": "INR",
        "direction": "LONG",
        "opened_at": day.isoformat(),
        "closed_at": (day + timedelta(hours=1)).isoformat(),
        "qty": "10",
        "entry_price": "100",
        "exit_price": "110",
        "stop": "90",
        "catalyst": "EARNINGS",
        "thesis": "Fixture research",
        "disproof": "Fixture stop",
        "source_reference": "LAB_FIXTURE_MANUAL_REPORT",
    }
    before = Analytics(er, r_method="ACTUAL_FILLED").stats()
    event = import_research(er, raw)
    assert read_research(er, "INDIA")["items"][0]["reported_price_pnl"] == "100"
    after = Analytics(er, r_method="ACTUAL_FILLED").stats()
    assert before["closed_trades"] == after["closed_trades"] == 1
    assert before["gross_pnl"] == after["gross_pnl"]
    with pytest.raises(ValueError, match="correction_of"):
        import_research(er, raw | {"exit_price": "120"})
    import_research(er, raw | {"exit_price": "120"}, correction_of=event["event_id"])
    assert read_research(er, "INDIA")["items"][0]["reported_price_pnl"] == "200"
    today = datetime.now(UTC) - timedelta(seconds=10)
    import_research(
        er,
        raw
        | {"external_id": "today", "opened_at": today.isoformat(), "closed_at": today.isoformat()},
    )
    assert read_research(er, "INDIA")["total"] == 1
    with pytest.raises(ValidationError):
        import_research(er, raw | {"market": "US"})
    with pytest.raises(ValidationError):
        import_research(er, raw | {"net_r": 999})
    assert verify_events(er.export_events())["valid"]
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.research_results").fetchone()["n"] == 3
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM lab.research_results")


def test_recording_stays_subscribed_after_trigger_and_ignores_future_data(er, submitted, raw):
    from catalyst_lab.watcher import Watcher

    collector = Measurements(er)
    assert not Watcher(er).active()
    assert {str(r["candidate_id"]) for r in collector.active(NOW)} == {submitted["cid"]}
    assert collector.observe(observation(raw, "t", 10, p="100", s=1, i="future"), NOW) == 0
