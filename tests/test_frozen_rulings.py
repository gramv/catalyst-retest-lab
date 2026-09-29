from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import psycopg
import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.engineering import EngineeringAcceptance
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.market import MarketDataError, Observation
from catalyst_lab.reconciliation import Reconciler
from catalyst_lab.risk import RiskPolicy
from catalyst_lab.risk_runtime import RiskRuntime
from catalyst_lab.watcher import Watcher
from tests.conftest import NOW
from tests.test_risk import candidate_factory as candidate_factory
from tests.test_risk import er as er
from tests.test_risk import execution_setup as execution_setup
from tests.test_risk import pristine_cluster as pristine_cluster
from tests.test_risk import risk_setup as risk_setup


def test_policy_is_frozen_and_decision_expiry_is_exactly_five_seconds(
    risk_setup, candidate_factory
):
    assert RiskPolicy().authorization_ttl_seconds == 5
    for ttl in [1, 10, None, True]:
        with pytest.raises(ValueError, match="5 seconds"):
            RiskPolicy(ttl)
    with pytest.raises(ValueError, match="previous-close"):
        RiskPolicy(5, "SAVED_ONLY")
    decision = risk_setup[2].authorize_entry(candidate_factory())
    assert decision["expires_at"] - decision["decided_at"] == timedelta(seconds=5)


def test_quote_must_still_be_fresh_when_decision_is_consumed(risk_setup, candidate_factory):
    decision = risk_setup[2].authorize_entry(candidate_factory())
    request = httpx.Request(
        "POST",
        PAPER_ENDPOINT + "/v2/orders",
        json=decision["payload_json"],
        extensions={"risk_decision_id": str(decision["risk_decision_id"])},
    )
    with pytest.raises(SubmissionDisabled, match="FRESH_MARKET_SNAPSHOT_REQUIRED"):
        AuthorizationGate(risk_setup[0], clock=lambda: NOW + timedelta(seconds=6)).claim(request)


def test_reconciliation_captures_previous_close_once_across_restarts_and_session_rollover(
    risk_setup,
):
    repo, broker, engine = risk_setup
    with repo.connect() as conn:
        original = conn.execute("SELECT * FROM lab.risk_sessions").fetchone()
        reconciliation = conn.execute(
            "SELECT * FROM lab.reconciliation_runs WHERE event_seq=%s",
            (original["reconciliation_seq"],),
        ).fetchone()
    assert original["day_start_equity"] == D("10000") and reconciliation["startup"]
    assert original["source"] == "ALPACA_LAST_EQUITY"
    account = broker.account
    broker.account = lambda: account() | {"last_equity": "12500", "equity": "9500"}
    restarted = Reconciler(
        repo, broker, clock=lambda: broker.at, baseline_recorder=engine.capture_reconciled_baseline
    )
    assert restarted.run_once()["clean"]
    with repo.connect() as conn:
        assert conn.execute("SELECT * FROM lab.risk_sessions").fetchone() == original
    broker.at += timedelta(days=3)
    assert restarted.run_once()["clean"]
    with repo.connect() as conn:
        rows = conn.execute("SELECT * FROM lab.risk_sessions ORDER BY session_date").fetchall()
    assert [r["day_start_equity"] for r in rows] == [D("10000"), D("12500")]
    assert rows[1]["reconciliation_seq"] != rows[0]["reconciliation_seq"]


@pytest.fixture
def engineering_body(raw, execution_setup):
    fake, engine, _, _ = execution_setup
    engine.classify(raw["ticker"], "TECH", "DEVICES", "LAB_FIXTURE_SERVER_MAPPING")
    return raw | {
        "signal_id": "TEST-" + uuid4().hex,
        "catalyst": "ENGINEERING_TEST",
        "stop": "90",
        "target": "121",
        "thesis": "Engineering plumbing test, not strategy research",
        "disproof": "A plumbing or audit requirement fails",
    }


def enter_engineering(er, execution_setup, engineering_body):
    fake, engine, _, _ = execution_setup
    runtime = RiskRuntime(engine)
    row = runtime.engineering.enroll(engineering_body)
    assert row["state"] == "VALIDATED" and not fake.root_ids
    watcher = Watcher(er, feed="iex", reconciliation_gate=engine.ready)
    sessions = {fake.now.date(): engine.session()}
    ticker = row["ticker"]
    watcher.tick(sessions, fake.now, {ticker})
    watcher.tick(sessions, fake.now, {ticker}, engine.client.quotes([ticker])[0])
    trade = Observation.from_wire(
        {
            "T": "t",
            "S": ticker,
            "t": fake.now.isoformat(),
            "p": "100",
            "s": "10",
            "i": "engineering-printed-touch",
        },
        "iex",
    )
    watcher.tick(sessions, fake.now, {ticker}, trade)
    assert er.get_candidate(row["candidate_id"])["state"] == "TRIGGER_CONFIRMED"
    runtime.tick()
    assert len(fake.root_ids) == 1 and runtime.last_error is None
    return row, runtime


def test_engineering_round_trip_is_audited_and_excluded_from_strategy_reporting(
    er, execution_setup, engineering_body, raw, evidence, policy
):
    fake, engine, _, _ = execution_setup
    row, runtime = enter_engineering(er, execution_setup, engineering_body)
    cid = row["candidate_id"]
    assert row["record_purpose"] == "ENGINEERING_TEST"
    with er.connect() as conn:
        assert (
            conn.execute("SELECT sum(budget) AS risk FROM lab.active_reservations").fetchone()[
                "risk"
            ]
            == 100
        )
        assert (
            conn.execute("SELECT record_purpose FROM lab.orders").fetchone()["record_purpose"]
            == "ENGINEERING_TEST"
        )
        assert not conn.execute("SELECT 1 FROM lab.strategy_orders").fetchone()
    fake.fill_entry(fake.root_ids[0])
    while fake.events:
        runtime.consume(fake.events.pop(0), fake.now)
    runtime.tick()
    assert not fake.positions and er.get_candidate(cid)["state"] == "CLOSED"
    assert len([o for o in fake.orders.values() if o["type"] == "market"]) == 1
    with er.connect() as conn:
        assert (
            conn.execute("SELECT outcome FROM lab.engineering_acceptance_results").fetchone()[
                "outcome"
            ]
            == "PASSED"
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.fills WHERE record_purpose='ENGINEERING_TEST'"
            ).fetchone()["n"]
            == 2
        )
        assert not conn.execute("SELECT 1 FROM lab.strategy_fills").fetchone()
        assert not conn.execute("SELECT 1 FROM lab.active_reservations").fetchone()
    audit = er.export_events()
    assert verify_events(audit)["valid"]
    related = [r for r in audit if r["candidate_id"] == cid]
    assert related and all(
        r["payload_json"]["record_purpose"] == "ENGINEERING_TEST" for r in related
    )
    assert all(not r["payload_json"]["strategy_eligible"] for r in related)
    assert er.analytics()["candidate_counts"] == {} and er.analytics()["trade_count"] == 0
    # Phase 5 projects the actual fixture broker events; nobody hand-writes trade outcomes.
    from catalyst_lab.measurement import Measurements

    Measurements(er).refresh()
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=lab_owner")) as conn:
        assert (
            conn.execute("SELECT record_purpose FROM lab.trades").fetchone()[0]
            == "ENGINEERING_TEST"
        )
        conn.execute("SET ROLE catalyst_reporting")
        for relation in [
            "strategy_candidates",
            "strategy_orders",
            "strategy_fills",
            "strategy_trades",
        ]:
            from psycopg import sql

            assert (
                conn.execute(
                    sql.SQL("SELECT count(*) FROM lab.{}").format(sql.Identifier(relation))
                ).fetchone()[0]
                == 0
            )
    assert er.analytics()["trade_count"] == 0
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=lab_owner")) as conn:
        conn.execute("SET ROLE catalyst_reporting")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM lab.candidates")
    # Test attempts do not consume the strategy's per-ticker/day attempt.
    strategy = er.submit(raw, NOW, lambda c, n: evidence, policy)
    assert strategy["state"] == "VALIDATED" and strategy["record_purpose"] == "STRATEGY"
    assert er.analytics()["candidate_counts"] == {"VALIDATED": 1}
    with pytest.raises(MarketDataError, match="ALREADY_SUBMITTED"):
        runtime.engineering.enroll(engineering_body | {"signal_id": "TEST-second"})


def test_test_prefix_cannot_be_promoted_or_submitted_without_enrollment(
    risk_setup, candidate_factory
):
    repo, _, engine = risk_setup
    cid = candidate_factory(signal_id="TEST-unenrolled")
    assert repo.get_candidate(cid)["record_purpose"] == "ENGINEERING_TEST"
    assert engine.authorize_entry(cid)["reason"] == "ENGINEERING_TEST_NOT_ENROLLED"
    with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE lab.candidates SET signal_id='strategy' WHERE candidate_id=%s", (cid,))


def test_engineering_requires_regular_session_and_emits_no_orders(
    execution_setup, engineering_body
):
    fake, engine, _, _ = execution_setup
    fake.now = NOW.replace(hour=18)
    with pytest.raises(MarketDataError, match="REGULAR_SESSION_REQUIRED"):
        EngineeringAcceptance(engine).enroll(engineering_body)
    assert not fake.root_ids and not any(c[0] != "GET" for c in fake.calls)


def test_rejected_malformed_test_signal_stays_out_of_strategy_counts(er, raw, evidence, policy):
    record = er.submit(raw | {"signal_id": "TEST-" + "x" * 130}, NOW, lambda c, n: evidence, policy)
    assert record["state"] == "REJECTED" and record["record_purpose"] == "ENGINEERING_TEST"
    assert not record["strategy_eligible"] and er.analytics()["candidate_counts"] == {}


def test_provenance_cannot_be_overridden_by_an_insert(er, raw):
    from psycopg.types.json import Jsonb

    with er.connect() as conn, pytest.raises(psycopg.errors.GeneratedAlways):
        conn.execute(
            """INSERT INTO lab.candidates(candidate_id,payload_json,signal_id,
            session_date,record_purpose) VALUES(%s,%s,%s,%s,'STRATEGY')""",
            (uuid4(), Jsonb(raw), "TEST-forged-purpose", NOW.date()),
        )


def test_unfilled_engineering_order_expires_and_is_canceled(execution_setup, engineering_body, er):
    fake, engine, _, _ = execution_setup
    row, runtime = enter_engineering(er, execution_setup, engineering_body)
    fake.now += timedelta(seconds=61)
    runtime.tick()
    while fake.events:
        runtime.consume(fake.events.pop(0), fake.now)
    runtime.tick()
    assert er.get_candidate(row["candidate_id"])["state"] == "CANCELED"
    assert not fake.positions and len(fake.root_ids) == 1
    with er.connect() as conn:
        assert (
            conn.execute("SELECT outcome FROM lab.engineering_acceptance_results").fetchone()[
                "outcome"
            ]
            == "NOT_ENTERED"
        )
