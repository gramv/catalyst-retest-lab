"""Migration 015 operator controls on disposable PostgreSQL with fake brokers only.

Every halt, pause, release and reconciliation below is a labelled local fixture. None of it is
broker or deployment evidence, and no test touches an owner ledger.
"""

import json
import tempfile
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.broker_ledger import TERMINAL
from catalyst_lab.config import PAPER_ENDPOINT, SCHEMA_VERSION
from catalyst_lab.engineering import EngineeringAcceptance
from catalyst_lab.execution import SubmissionDisabled, halt, system_event
from catalyst_lab.managed_ops import main as managed_ops
from catalyst_lab.managed_ops import operator_command, operator_repository
from catalyst_lab.market import MarketDataError
from catalyst_lab.reconciliation import Reconciler
from catalyst_lab.repository import Repository
from tests.conftest import NOW
from tests.test_execution import SnapshotClient
from tests.test_execution import confirmed as confirmed
from tests.test_execution import er as er
from tests.test_execution import paper as paper
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_execution import submitted as submitted
from tests.test_frozen_rulings import engineering_body as engineering_body
from tests.test_managed_execution import admit_enter, observation, packet
from tests.test_managed_execution import mx as mx
from tests.test_managed_operator_halts import restart
from tests.test_position_monitor import opened
from tests.test_risk import candidate_factory as candidate_factory
from tests.test_risk import execution_setup as execution_setup
from tests.test_risk import risk_setup as risk_setup

REASON = "Operator verified broker and ledger agree (fixture)"
NEW_TABLES = ("execution_halt_releases", "execution_halt_residual_acceptances",
              "operator_flatten_requests")


def url(repo, role):
    return repo.database_url.replace("user=catalyst_app", "user=" + role)


def as_role(repo, role):
    return Repository(url(repo, role))


def operator(repo, statement, params=()):
    with as_role(repo, "catalyst_operator").connect() as conn:
        return conn.execute(statement, params).fetchone()


def release(repo, halt_id, reason=REASON, correction=None):
    return operator(
        repo, "SELECT lab.operator_release_halt(%s,%s,%s) AS r", (halt_id, reason, correction)
    )["r"]


def pause(repo, role="catalyst_operator", reason="Fixture entry pause"):
    with as_role(repo, role).connect() as conn:
        return conn.execute("SELECT lab.operator_pause(%s) AS id", (reason,)).fetchone()["id"]


def resume(repo, reason="Fixture resume after operator review"):
    return operator(repo, "SELECT lab.operator_resume(%s) AS ids", (reason,))["ids"]


def refused(code):
    return pytest.raises(psycopg.errors.RaiseException, match=code)


def record_halt(repo, reason, payload=None):
    """The real execution.halt() writer: RISK_HALT event plus positional INSERT (view)."""
    with repo.connect() as conn:
        halt(repo, conn, reason, payload or {"fixture": True})
        return conn.execute(
            "SELECT max(event_seq) AS id FROM lab.execution_halts WHERE reason=%s", (reason,)
        ).fetchone()["id"]


def active(repo):
    with repo.connect() as conn:
        return [
            (r["event_seq"], r["reason"])
            for r in conn.execute("SELECT * FROM lab.execution_halts ORDER BY event_seq")
        ]


def mirrored_reconciliation(repo):
    """Real V1 Reconciler against a fixture broker that agrees with the local ledger."""
    with repo.connect() as conn:
        positions = [
            {"symbol": r["ticker"], "qty": str(r["qty"])}
            for r in conn.execute(
                "SELECT ticker,sum(qty) AS qty FROM lab.strategy_positions GROUP BY ticker"
            ).fetchall()
        ]
        orders = [
            {
                "id": r["broker_order_id"], "symbol": r["symbol"], "side": r["side"],
                "qty": str(r["qty"]), "filled_qty": str(r["filled_qty"]),
                "status": r["status"], "type": r["order_type"],
                "limit_price": None if r["limit_price"] is None else str(r["limit_price"]),
                "stop_price": None if r["stop_price"] is None else str(r["stop_price"]),
            }
            for r in conn.execute("SELECT * FROM lab.broker_order_states").fetchall()
            if r["status"] not in TERMINAL
        ]
    return Reconciler(repo, SnapshotClient(positions, orders), clock=lambda: NOW).run_once()


def managed_style_clean_reconciliation(repo):
    """Fixture copy of the two rows ManagedExecution.reconcile() writes when it is clean."""
    with repo.connect() as conn:
        event = system_event(
            repo, conn, "MANAGED_STARTUP_RECONCILIATION",
            {"clean": True, "baseline_source": "LAB_FIXTURE"},
        )
        conn.execute(
            """INSERT INTO lab.reconciliation_runs(event_seq,process_run_id,session_date,
            started_at,completed_at,startup,clean,discrepancies,broker_snapshot)
            VALUES(%s,%s,%s,%s,%s,true,true,'[]','{}')""",
            (event["seq"], uuid4(), NOW.date(), NOW, NOW),
        )
        return event["seq"]


def assert_audited(repo):
    """Every operator row equals its hash-chained audit event and the chain verifies."""
    with as_role(repo, "lab_owner").connect() as conn:
        for table in NEW_TABLES:
            row = conn.execute(
                sql.SQL(
                    "SELECT count(*) AS n, count(*) FILTER (WHERE lab.research_audit_matches("
                    "%s,to_jsonb(t),t.event_seq)) AS ok FROM lab.{} t"
                ).format(sql.Identifier(table)),
                (table.upper(),),
            ).fetchone()
            assert row["n"] == row["ok"], table
    assert verify_events(repo.export_events())["valid"]


def test_positional_insert_uses_view_and_status_readers_honour_release(er):
    halt_id = record_halt(er, "TEST_FIXTURE_HALT", {"fixture": True})
    with er.connect() as conn:
        record = conn.execute(
            "SELECT * FROM lab.execution_halt_records WHERE event_seq=%s", (halt_id,)
        ).fetchone()
        event = conn.execute(
            "SELECT payload_json FROM lab.trade_events WHERE seq=%s", (halt_id,)
        ).fetchone()["payload_json"]
    assert record["reason"] == "TEST_FIXTURE_HALT" and record["payload_json"] == {"fixture": True}
    assert event["kind"] == "RISK_HALT" and event["reason"] == "TEST_FIXTURE_HALT"
    reconciler = Reconciler(er, SnapshotClient(), clock=lambda: NOW)
    assert reconciler.run_once()["clean"] and not reconciler.ready()  # reconciliation.py
    status = er.execution_status()  # repository.py
    assert status["halted"] and status["halt_reasons"] == ["TEST_FIXTURE_HALT"]
    result = release(er, halt_id)
    assert result["released"] and result["halt_reason"] == "TEST_FIXTURE_HALT"
    assert result["reconciliation_seq"] > halt_id and result["correction_seq"] is None
    assert reconciler.ready()
    assert er.execution_status()["halted"] is False and active(er) == []
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.execution_halt_records").fetchone()[
            "n"
        ] == 1
        released = conn.execute("SELECT * FROM lab.execution_halt_releases").fetchone()
    assert released["operator_role"] == "catalyst_operator"
    assert released["release_kind"] == "OPERATOR_RELEASE" and released["reason"] == REASON
    with refused("HALT_ALREADY_RELEASED"):
        release(er, halt_id)
    assert_audited(er)


def test_release_requires_clean_reconciliation_recorded_after_the_halt(er):
    client = SnapshotClient([{"symbol": "AAPL", "qty": "5"}])
    reconciler = Reconciler(er, client, clock=lambda: NOW)
    assert not reconciler.run_once()["clean"]  # Latches after recording the dirty run.
    [(first, reason)] = active(er)
    assert reason == "BROKER_RECONCILIATION_MISMATCH"
    with refused("CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED"):
        release(er, first)
    assert not reconciler.run_once()["clean"]
    with refused("CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED"):
        release(er, first)
    for short in ("", "   ", "too short"):
        with refused("OPERATOR_REASON_(REQUIRED|TOO_SHORT)"):
            release(er, first, reason=short)
    client.position_rows = []
    assert reconciler.run_once()["clean"] and not reconciler.ready()
    for halt_id, _ in active(er):
        assert release(er, halt_id)["released"]
    assert reconciler.ready()
    # A later dirty reconciliation supersedes an earlier clean one.
    third = record_halt(er, "TEST_FIXTURE_HALT")
    assert reconciler.run_once()["clean"]
    client.position_rows = [{"symbol": "AAPL", "qty": "5"}]
    assert not reconciler.run_once()["clean"]
    with refused("CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED"):
        release(er, third)
    assert_audited(er)


def test_reconciliation_in_the_halting_transaction_cannot_release_it(er):
    with er.connect() as conn:  # Same order as ManagedExecution.reconcile(): latch, then record.
        halt(er, conn, "TEST_FIXTURE_HALT", {"fixture": True})
        event = system_event(er, conn, "MANAGED_STARTUP_RECONCILIATION", {"clean": True})
        conn.execute(
            """INSERT INTO lab.reconciliation_runs(event_seq,process_run_id,session_date,
            started_at,completed_at,startup,clean,discrepancies,broker_snapshot)
            VALUES(%s,%s,%s,%s,%s,true,true,'[]','{}')""",
            (event["seq"], uuid4(), NOW.date(), NOW, NOW),
        )
    [(halt_id, _)] = active(er)
    assert event["seq"] > halt_id
    with refused("CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED"):
        release(er, halt_id)
    later = managed_style_clean_reconciliation(er)
    assert release(er, halt_id)["reconciliation_seq"] == later


def test_release_waits_for_in_flight_frozen_engine_authorization(
    er, execution_setup, candidate_factory, monkeypatch
):
    fake, engine, dispatcher, safety = execution_setup
    fake.timeout_after_accept = True
    monkeypatch.setattr(engine.client, "order_by_client_id", lambda client_id: None)
    decision = dispatcher.enter(candidate_factory())  # UNKNOWN, then an inconclusive lookup.
    halt_id = record_halt(er, "TEST_FIXTURE_HALT")
    managed_style_clean_reconciliation(er)
    with refused("AUTHORIZATION_CLAIMS_UNRESOLVED"):
        release(er, halt_id)
    monkeypatch.undo()
    dispatcher.recover_outstanding()
    with er.connect() as conn:
        assert conn.execute(
            "SELECT outcome FROM lab.current_authorization_results WHERE risk_decision_id=%s",
            (decision["risk_decision_id"],),
        ).fetchone()["outcome"] == "RECOVERED"
    assert release(er, halt_id)["released"]


def test_release_waits_for_unresolved_managed_claim(er, mx, monkeypatch):
    engine, venue, _ = mx
    venue.timeout_next_post = True
    monkeypatch.setattr(engine.broker, "order_by_client_id", lambda client_id: None)
    _, decision = admit_enter(mx)
    halt_id = record_halt(engine.repo, "TEST_FIXTURE_HALT")
    assert restart(engine).reconcile()["clean"]
    with refused("AUTHORIZATION_CLAIMS_UNRESOLVED"):
        release(er, halt_id)
    monkeypatch.undo()
    assert engine.recover(decision)  # The fixture broker still holds the accepted order.
    assert release(er, halt_id)["released"]


def test_pause_blocks_frozen_entries_not_exits_and_resume_reopens(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    held = candidate_factory()
    dispatcher.enter(held)
    fake.fill_entry(fake.root_ids[0])
    fake.drain(dispatcher)
    ticker = er.get_candidate(held)["ticker"]
    assert er.get_candidate(held)["state"] == "OPEN"
    paused = pause(er, role="catalyst_risk")  # Risk-reducing, so the risk role may pause.
    assert active(er) == [(paused, "OPERATOR_PAUSE")]
    blocked = engine.authorize_entry(candidate_factory())  # risk.py
    assert blocked["decision"] == "REJECTED" and blocked["reason"] == "RISK_HALT"
    safety.process_exits()  # A pause alone never requests or sends an exit.
    fake.drain(dispatcher)
    assert ticker in fake.positions and er.get_candidate(held)["state"] == "OPEN"
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.risk_exit_requests").fetchone()
    safety.request_exit(ticker, "TIME_EXIT", held)
    safety.process_exits()
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {} and er.get_candidate(held)["state"] == "CLOSED"
    assert resume(er) == [paused]
    approved = engine.authorize_entry(candidate_factory())
    assert approved["decision"] == "APPROVED"
    request = httpx.Request(
        "POST",
        PAPER_ENDPOINT + "/v2/orders",
        json=approved["payload_json"],
        extensions={"risk_decision_id": str(approved["risk_decision_id"])},
    )
    gate = AuthorizationGate(engine.repo, clock=lambda: fake.now)
    pause(er)
    with pytest.raises(SubmissionDisabled, match="ENTRY_AUTHORIZATION_REVOKED"):
        gate.claim(request)  # authorization.py
    resume(er)
    assert gate.claim(request) == approved["risk_decision_id"]
    with refused("NO_ACTIVE_OPERATOR_PAUSE"):
        resume(er)
    assert_audited(er)


def test_engineering_enrollment_honours_pause_and_resume(er, execution_setup, engineering_body):
    engine = execution_setup[1]
    pause(er)
    with pytest.raises(MarketDataError, match="RISK_HALT"):
        EngineeringAcceptance(engine).enroll(engineering_body)  # engineering.py
    resume(er)
    assert EngineeringAcceptance(engine).enroll(engineering_body)["state"] == "VALIDATED"


def test_pause_blocks_managed_entries_but_protection_and_exits_continue(er, mx, monkeypatch):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    stop = venue.orders_of("sell", "stop_limit")[0]
    watching = engine.admit(packet(mx, "ETH/USD"))
    pause(er)
    with pytest.raises(ValueError, match="RISK_HALT"):
        engine.admit(packet(mx, "SOL/USD"))  # managed_execution admit
    # managed authorize_entry: OPERATOR_PAUSE_ENTRY_WAIT_V1 records a wait, no decision.
    assert engine.observe_trigger(watching, observation(mx)) is None
    assert engine._load(watching)[1]["state"] == "WATCHING"
    assert not [o for o in venue.orders_of("buy") if o["symbol"] == "ETH/USD"]
    plan = engine.manage(sid, observation(mx))  # A pause alone never exits.
    assert plan.state == "PROTECTED" and stop["status"] == "new"
    assert not venue.orders_of("sell", "market")
    assert engine._load(sid)[1].get("exit_requested") is None
    gap = observation(mx, trade_price="90", bid="90", ask="90.01")  # A print through the stop.
    engine.manage(sid, gap)
    venue.now += timedelta(seconds=5)
    engine.manage(sid, observation(mx, bid="90", ask="90.01"))
    assert stop["status"] == "canceled"  # Authorized CANCEL claim while paused.
    engine.manage(sid, observation(mx, bid="90", ask="90.01"))
    closing = venue.orders_of("sell", "market")[0]  # Authorized EXIT claim while paused.
    engine.ingest(venue.fill(closing["id"], closing["qty"], price="90"))
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "CLOSED"
    resume(er)
    captured = []
    monkeypatch.setattr(engine, "dispatch", captured.append)
    fresh = engine.admit(packet(mx, "SOL/USD"))
    decision = engine.observe_trigger(fresh, observation(mx))
    assert decision["outcome"] == "APPROVED" and captured == [decision]
    pause(er)
    with pytest.raises(SubmissionDisabled, match="ENTRY_AUTHORIZATION_REVOKED"):
        engine.broker.mutate(  # managed_store gate
            decision["method"], decision["path"], decision["payload"], decision["decision_id"]
        )
    resume(er)
    accepted = engine.broker.mutate(
        decision["method"], decision["path"], decision["payload"], decision["decision_id"]
    )
    assert accepted["id"] in venue.orders
    assert_audited(er)


def test_released_managed_latch_can_latch_again_on_recurrence(er, mx):
    engine, venue, _ = mx
    venue.inventory["UNKNOWN"] = D(2)
    assert not engine.reconcile()["clean"]
    venue.inventory.clear()
    fresh = restart(engine)
    assert fresh.reconcile()["clean"]
    [(halt_id, reason)] = active(er)
    assert reason == "MANAGED_UNEXPLAINED_BROKER_POSITION"
    with pytest.raises(ValueError, match="RISK_HALT"):
        fresh.admit(packet(mx, "BTC/USD"))
    release(er, halt_id)
    assert fresh.admit(packet(mx, "BTC/USD"))
    venue.inventory["UNKNOWN"] = D(2)
    assert not fresh.reconcile()["clean"]  # managed_execution latch dedup
    with er.connect() as conn:
        rows = conn.execute(
            """SELECT x.halt_id IS NOT NULL AS released FROM lab.execution_halt_records r
            LEFT JOIN lab.execution_halt_releases x ON x.halt_id=r.event_seq
            WHERE r.reason='MANAGED_UNEXPLAINED_BROKER_POSITION' ORDER BY r.event_seq"""
        ).fetchall()
    assert [r["released"] for r in rows] == [True, False]


def test_broker_correction_halt_requires_a_bound_later_correction_event(
    er, submitted, capsys, monkeypatch
):
    def consume(**values):
        submitted["ledger"].consume(submitted["event"](**values), NOW + timedelta(hours=1))

    consume(price="100", seconds=1)
    consume(role="TARGET", price="102", seconds=2)
    consume(kind="trade_bust", role="TARGET", status="filled", seconds=3)
    target = submitted["order"]["legs"][1]["id"]
    [(halt_id, reason)] = active(er)
    assert reason == "BROKER_EVENT_REQUIRES_MANUAL_REVIEW"
    assert mirrored_reconciliation(er)["clean"]
    with refused("CORRECTION_EVENT_REQUIRED"):
        release(er, halt_id)
    with er.connect() as conn:
        entry_event, bust_event = (
            conn.execute(
                """SELECT e.event_id FROM lab.broker_events b JOIN lab.trade_events e
                ON e.seq=b.event_seq WHERE b.broker_order_id=%s AND b.broker_event_type=%s""",
                (order_id, kind),
            ).fetchone()["event_id"]
            for order_id, kind in ((submitted["order"]["id"], "fill"), (target, "trade_bust"))
        )
        body = {"kind": "BROKER_CORRECTION_REVIEWED", "record": "LAB_FIXTURE"}
        unrelated = er.append_event(conn, "CORRECTION", body, submitted["cid"], entry_event)
        bound = er.append_event(conn, "CORRECTION", body, submitted["cid"], bust_event)
    with refused("CORRECTION_EVENT_REQUIRED"):
        release(er, halt_id, correction=unrelated["seq"])
    other = record_halt(er, "TEST_FIXTURE_HALT")
    managed_style_clean_reconciliation(er)
    with refused("CORRECTION_EVENT_NOT_APPLICABLE"):
        release(er, other, correction=bound["seq"])
    monkeypatch.setenv("OPERATOR_DATABASE_URL", url(er, "catalyst_operator"))
    result = managed_ops(["operator", "release-halt", str(halt_id), "--reason", REASON,
                          "--correction-seq", str(bound["seq"])])
    assert json.loads(capsys.readouterr().out) == result
    assert result["correction_seq"] == bound["seq"] and result["released"]
    assert [reason for _, reason in active(er)] == ["TEST_FIXTURE_HALT"]
    assert_audited(er)


def test_crypto_dust_halt_requires_explicit_residual_acceptance(er, mx, capsys, monkeypatch):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    stop = venue.orders_of("sell", "stop_limit")[0]
    dust = D("0.00001")
    engine.ingest(venue.fill(stop["id"], str(D(stop["qty"]) - dust), price="95"))
    stop["status"] = "canceled"  # Broker-side remainder cancellation (fixture).
    plan = engine.manage(sid, observation(mx))
    assert plan.state == "HALTED" and plan.reason == "UNPROTECTED_RESIDUAL_BELOW_BROKER_MINIMUM"
    operator_url = url(er, "catalyst_operator")
    listing = operator_command(operator_repository(operator_url), "list-halts")
    [dust_halt] = listing["halts"]
    assert dust_halt["reason"] == "MANAGED_CRYPTO_UNPROTECTED_RESIDUAL_BELOW_BROKER_MINIMUM"
    assert dust_halt["release_requirement"] == "RESIDUAL_ACCEPTANCE"
    assert dust_halt["release_blockers"] == [
        "CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED", "RESIDUAL_ACCEPTANCE_REQUIRED"
    ]
    halt_id = dust_halt["halt_id"]
    monkeypatch.setenv("OPERATOR_DATABASE_URL", operator_url)
    command = ["operator", "release-halt", str(halt_id), "--reason", REASON, "--accept-residual"]
    with pytest.raises(SystemExit) as failure:  # The acceptance rolls back with the release.
        managed_ops(command)
    assert str(failure.value) == (
        "OPERATOR_COMMAND_REFUSED: CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED"
    )
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.execution_halt_residual_acceptances").fetchone()
    assert restart(engine).reconcile()["clean"]
    with refused("RESIDUAL_ACCEPTANCE_REQUIRED"):
        release(er, halt_id)
    other = record_halt(er, "TEST_FIXTURE_HALT")
    with refused("RESIDUAL_ACCEPTANCE_NOT_APPLICABLE"):
        operator(er, "SELECT lab.operator_accept_residual(%s,%s)", (other, REASON))
    result = managed_ops(command)
    assert result["released"] and result["halt_reason"] == dust_halt["reason"]
    with er.connect() as conn:
        accepted = conn.execute(
            "SELECT * FROM lab.execution_halt_residual_acceptances"
        ).fetchall()
    assert [(a["halt_id"], a["decision"]) for a in accepted] == [(halt_id, "RESIDUAL_ACCEPTED")]
    with refused("HALT_ALREADY_RELEASED"):
        operator(er, "SELECT lab.operator_accept_residual(%s,%s)", (halt_id, REASON))
    capsys.readouterr()
    assert_audited(er)


def test_pause_cannot_be_released_as_a_halt_and_resume_touches_only_pauses(er):
    paused = pause(er)
    halt_id = record_halt(er, "TEST_FIXTURE_HALT")
    managed_style_clean_reconciliation(er)
    with refused("OPERATOR_PAUSE_REQUIRES_RESUME"):
        release(er, paused)
    with refused("OPERATOR_REASON_TOO_SHORT"):
        resume(er, reason="resume")
    assert resume(er) == [paused]
    assert active(er) == [(halt_id, "TEST_FIXTURE_HALT")]
    with refused("OPERATOR_REASON_REQUIRED"):
        pause(er, reason="   ")


def test_only_the_operator_can_resume_release_accept_or_request_flatten(er):
    halt_id = record_halt(er, "TEST_FIXTURE_HALT")
    managed_style_clean_reconciliation(er)
    statements = (
        ("SELECT lab.operator_resume(%s)", (REASON,)),
        ("SELECT lab.operator_release_halt(%s,%s)", (halt_id, REASON)),
        ("SELECT lab.operator_accept_residual(%s,%s)", (halt_id, REASON)),
        ("SELECT lab.operator_request_flatten_all(%s)", (REASON,)),
        ("SELECT * FROM lab.operator_list_halts()", ()),
        (
            """INSERT INTO lab.execution_halt_releases(halt_id,halt_reason,release_kind,reason,
            operator_role,reconciliation_seq) VALUES(%s,'TEST_FIXTURE_HALT','OPERATOR_RELEASE',
            %s,current_user,1)""",
            (halt_id, REASON),
        ),
    )
    roles = ("catalyst_app", "catalyst_risk", "catalyst_review", "catalyst_jev",
             "catalyst_review_operator", "catalyst_reporting")
    for role in roles:
        for statement, params in statements:
            with as_role(er, role).connect() as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement, params)
        if role != "catalyst_risk":
            with as_role(er, role).connect() as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("SELECT lab.operator_pause(%s)", (REASON,))
    with as_role(er, "catalyst_operator").connect() as conn:
        granted = conn.execute("""SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='lab'
            AND has_table_privilege(current_user,c.oid,
              'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')""").fetchall()
        assert granted == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM lab.execution_halts")
    operator_repository(url(er, "catalyst_operator")).check_role()
    for role in ("catalyst_app", "catalyst_risk", "catalyst_review_operator"):
        with pytest.raises(RuntimeError, match="OPERATOR_ROLE_REQUIRED"):
            operator_repository(url(er, role)).check_role()
    assert active(er) == [(halt_id, "TEST_FIXTURE_HALT")]


def test_halt_history_is_append_only_even_for_the_owner(er):
    released = record_halt(er, "TEST_FIXTURE_HALT")
    managed_style_clean_reconciliation(er)
    release(er, released)
    dust = record_halt(er, "MANAGED_CRYPTO_RESIDUAL_BELOW_BROKER_MINIMUM", {"residual_qty": "1E-5"})
    operator(er, "SELECT lab.operator_accept_residual(%s,%s)", (dust, REASON))
    operator(er, "SELECT lab.operator_request_flatten_all(%s)", ("Fixture flatten request",))
    before = er.export_events()
    owner = url(er, "lab_owner")
    for table in ("execution_halt_records", *NEW_TABLES):
        for statement in ("UPDATE lab.{} SET event_seq=event_seq", "DELETE FROM lab.{}",
                          "TRUNCATE lab.{} CASCADE"):
            with psycopg.connect(owner) as conn, refused("append-only"):
                conn.execute(sql.SQL(statement).format(sql.Identifier(table)))
    # The view forwards row changes to the protected table; a view is never truncatable.
    for statement in ("UPDATE lab.execution_halts SET event_seq=event_seq",
                      "DELETE FROM lab.execution_halts"):
        with psycopg.connect(owner) as conn, refused("append-only"):
            conn.execute(statement)
        with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    for database_url in (owner, er.database_url):
        with psycopg.connect(database_url) as conn:
            with pytest.raises(psycopg.errors.WrongObjectType):
                conn.execute("TRUNCATE lab.execution_halts")
    assert er.export_events() == before
    assert active(er) == [(dust, "MANAGED_CRYPTO_RESIDUAL_BELOW_BROKER_MINIMUM")]
    assert_audited(er)


def test_view_keeps_the_renamed_table_grants_and_is_auto_updatable(er):
    grants = {}
    with as_role(er, "lab_owner").connect() as conn:
        rows = conn.execute("""SELECT c.relname,r.rolname AS grantee,a.privilege_type AS privilege
            FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
            JOIN pg_roles r ON r.oid=a.grantee
            WHERE c.oid IN ('lab.execution_halts'::regclass,'lab.execution_halt_records'::regclass)
            AND a.grantee<>c.relowner""").fetchall()
        for row in rows:
            grants.setdefault(row["relname"], set()).add((row["grantee"], row["privilege"]))
        view = conn.execute("""SELECT is_insertable_into FROM information_schema.tables
            WHERE table_schema='lab' AND table_name='execution_halts'""").fetchone()
        triggers = {r["tgname"] for r in conn.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid='lab.execution_halt_records'::regclass"
        ).fetchall()}
        version = conn.execute("SELECT max(version) AS v FROM lab.schema_migrations").fetchone()
    expected = {("catalyst_app", "SELECT"), ("catalyst_app", "INSERT")}
    # Migration 019 adds read-only access to the view for the acceptance-evidence tool.
    assert grants == {"execution_halts": expected | {("catalyst_review", "SELECT")},
                      "execution_halt_records": expected}
    assert view["is_insertable_into"] == "YES"
    assert {"immutable_rows", "immutable_truncate"} <= triggers
    assert version["v"] == SCHEMA_VERSION == 31


def test_flatten_all_is_a_durable_audited_request_without_broker_action(er, capsys, monkeypatch):
    # The CLI itself only records; the running app's account-safety tick executes the request
    # (migration 019, tests/test_operator_flatten.py).
    monkeypatch.setenv("OPERATOR_DATABASE_URL", url(er, "catalyst_operator"))
    result = managed_ops(["operator", "flatten-all", "--reason", "Owner requested flat account"])
    assert json.loads(capsys.readouterr().out) == result
    assert result["recorded"] and result["broker_action"] == "PENDING_ACCOUNT_SAFETY_TICK"
    with er.connect() as conn:
        row = conn.execute("SELECT * FROM lab.operator_flatten_requests").fetchone()
        exits = conn.execute("SELECT count(*) AS n FROM lab.risk_exit_requests").fetchone()
    assert str(row["request_id"]) == result["flatten_request_id"]
    assert row["event_seq"] == result["request_seq"]
    assert row["scope"] == "ALL_POSITIONS_AND_ORDERS"
    assert row["operator_role"] == "catalyst_operator"
    assert exits["n"] == 0 and active(er) == []
    with as_role(er, "catalyst_risk").connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT lab.operator_request_flatten_all('risk may not request')")
    assert_audited(er)


def test_operator_cli_lists_pauses_resumes_and_never_prints_credentials(
    er, capsys, monkeypatch
):
    monkeypatch.setenv("OPERATOR_DATABASE_URL", url(er, "catalyst_operator"))
    paused = managed_ops(["operator", "pause", "--reason", "CLI fixture pause"])
    assert paused["paused"] and paused["scope"] == "ENTRY_ONLY" and paused["mode"] == "PAPER_ONLY"
    listing = managed_ops(["operator", "list-halts"])
    [row] = listing["halts"]
    assert row["reason"] == "OPERATOR_PAUSE" and row["release_requirement"] == "OPERATOR_RESUME"
    assert row["release_blockers"] == [] and row["payload_json"]["scope"] == "ENTRY_ONLY"
    assert row["payload_json"]["operator_role"] == "catalyst_operator"
    resumed = managed_ops(["operator", "resume", "--reason", "CLI fixture resume reviewed"])
    assert resumed["released_halt_ids"] == [paused["halt_id"]]
    assert managed_ops(["operator", "list-halts"])["halts"] == []
    history = managed_ops(["operator", "list-halts", "--all"])["halts"]
    assert history[0]["released"] and history[0]["release_kind"] == "OPERATOR_RESUME"
    with pytest.raises(SystemExit) as failure:
        managed_ops(["operator", "resume", "--reason", "Nothing is paused right now"])
    assert str(failure.value) == "OPERATOR_COMMAND_REFUSED: NO_ACTIVE_OPERATOR_PAUSE"
    secret = "fixture-password-never-printed"
    wrong = er.database_url + " password=" + secret  # Trust auth: connects as catalyst_app.
    with pytest.raises(SystemExit) as failure:
        managed_ops(["operator", "list-halts", "--database-url", wrong])
    assert str(failure.value) == "OPERATOR_COMMAND_REFUSED: OPERATOR_ROLE_REQUIRED"
    monkeypatch.delenv("OPERATOR_DATABASE_URL")
    with pytest.raises(SystemExit) as failure:
        managed_ops(["operator", "pause", "--reason", "No connection configured"])
    assert str(failure.value) == "OPERATOR_COMMAND_REFUSED: OPERATOR_DATABASE_URL_REQUIRED"
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "host=" not in captured.out + captured.err
    assert_audited(er)


def start_cluster_at(root, version):
    """localdb.start() equivalent that stops at an older schema to prove a later migration."""
    root.chmod(0o700)
    socket = root / "socket"
    socket.mkdir(mode=0o700)
    data = root / "postgres"
    localdb.run_pg("initdb", "-D", data, "-U", "lab_owner", "--auth-local=trust",
                   "--auth-host=reject", "--encoding=UTF8", "--no-locale")
    socket_setting = str(socket.resolve()).replace("'", "''")
    with (data / "postgresql.conf").open("a") as config:
        config.write(f"\nlisten_addresses = ''\nport = 55437\n"
                     f"unix_socket_directories = '{socket_setting}'\n"
                     "unix_socket_permissions = 0700\n")
    localdb.run_pg("pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "start")
    owner = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner.replace("dbname=catalyst_lab", "dbname=postgres"),
                         autocommit=True) as conn:
        conn.execute("CREATE DATABASE catalyst_lab")
    source = Path(localdb.__file__)
    with psycopg.connect(owner) as conn:
        conn.execute(source.with_name("schema.sql").read_text())
        for migration in sorted(source.with_name("migrations").glob("*.sql")):
            if int(migration.name.split("_")[0]) <= version:
                conn.execute(migration.read_text())


@pytest.fixture(scope="module")
def schema14_cluster():
    with tempfile.TemporaryDirectory(prefix="catalyst-opctl-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 14)
            yield root
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def populate_schema14(root):
    """Fixture rows written as the real roles, through real writers where they exist."""
    app = Repository(localdb.connection_url(root))
    with app.connect() as conn:
        halt(app, conn, "BROKER_RECONCILIATION_MISMATCH", {"reconciliation_seq": 1})
        event = system_event(app, conn, "BROKER_RECONCILIATION", {"clean": False})
        conn.execute(
            """INSERT INTO lab.reconciliation_runs VALUES(%s,%s,%s,%s,%s,true,false,
            '[{"code":"LAB_FIXTURE"}]','{}')""",
            (event["seq"], uuid4(), NOW.date(), NOW, NOW),
        )
    with Repository(localdb.connection_url(root, "catalyst_review_operator")).connect() as conn:
        conn.execute("SELECT lab.review_operator_halt(true)")
        conn.execute("SELECT lab.review_operator_halt(false)")
    with Repository(localdb.connection_url(root, "catalyst_risk")).connect() as conn:
        conn.execute(
            """INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,body)
            VALUES(%s,NULL,%s,'BROKER_RECONCILIATION',%s)""",
            (uuid4(), "schema14-" + uuid4().hex, Jsonb({"clean": True, "record": "LAB_FIXTURE"})),
        )
    request = uuid4()
    with Repository(localdb.connection_url(root, "catalyst_jev")).connect() as conn:
        conn.execute(
            """INSERT INTO lab.jev_requests(request_id,evidence_identity,stage,
            question_set_version,model,input_hash,template_hash,request_json,deadline,
            record_purpose) VALUES(%s,%s,'TRIAGE','LAB_FIXTURE_V1','jev-1.13.0',%s,%s,%s,
            clock_timestamp()+interval '1 hour','ENGINEERING_TEST')""",
            (request, Jsonb({"record": "LAB_FIXTURE"}), "a" * 64, "b" * 64,
             json.dumps({"model": "jev-1.13.0", "state": {}})),
        )
        conn.execute(
            """INSERT INTO lab.jev_receipts(receipt_id,request_id,attempt,outcome,http_status,
            started_at,completed_at,latency_ms,error_code) VALUES(%s,%s,0,'HTTP_ERROR',500,
            clock_timestamp(),clock_timestamp(),1,'LAB_FIXTURE')""",
            (uuid4(), request),
        )


def audit_state(root):
    owner = Repository(localdb.connection_url(root, "lab_owner"))
    proof = verify_events(owner.export_events())
    with owner.connect() as conn:
        tables = [r["relname"] for r in conn.execute("""SELECT DISTINCT c.relname FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='lab' AND t.tgname='audit_jev' ORDER BY 1""").fetchall()]
        audited = {}
        for table in tables:
            row = conn.execute(
                sql.SQL(
                    "SELECT count(*) AS n, count(*) FILTER (WHERE lab.research_audit_matches("
                    "%s,to_jsonb(t),t.event_seq)) AS ok FROM lab.{} t"
                ).format(sql.Identifier(table)),
                (table.upper(),),
            ).fetchone()
            audited[table] = (row["n"], row["ok"])
        halts = [r["row"] for r in conn.execute(
            "SELECT to_jsonb(h) AS row FROM lab.execution_halts h ORDER BY event_seq"
        ).fetchall()]
        version = conn.execute("SELECT max(version) AS v FROM lab.schema_migrations").fetchone()
    return proof, audited, halts, version["v"]


def test_populated_schema14_ledger_migrates_ddl_only(schema14_cluster):
    root = schema14_cluster
    populate_schema14(root)
    proof, audited, halts, version = audit_state(root)
    assert version == 14 and proof["valid"] and len(halts) == 1
    populated = {t for t, (n, ok) in audited.items() if n}
    assert {"review_operator_events", "managed_events", "jev_requests", "jev_receipts"} <= populated
    assert all(n == ok for n, ok in audited.values())
    with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
        conn.execute(
            (Path(localdb.__file__).with_name("migrations") / "015_operator_controls.sql")
            .read_text()
        )
    after, audited_after, halts_after, version_after = audit_state(root)
    assert version_after == 15 and after == proof  # Same event count and chain head.
    assert halts_after == halts and all(n == ok for n, ok in audited_after.values())
    assert {t: v for t, v in audited_after.items() if t in audited} == audited
    assert {t: v for t, v in audited_after.items() if t not in audited} == {
        table: (0, 0) for table in NEW_TABLES
    }
    app = Repository(localdb.connection_url(root))
    with app.connect() as conn:  # The unchanged positional INSERT now targets the view.
        halt(app, conn, "TEST_FIXTURE_HALT", {"fixture": True})
        records = conn.execute(
            "SELECT count(*) AS n FROM lab.execution_halt_records"
        ).fetchone()["n"]
    assert records == 2
    with Repository(localdb.connection_url(root, "catalyst_operator")).connect() as conn:
        paused = conn.execute("SELECT lab.operator_pause('Migrated ledger pause')").fetchone()
        conn.execute("SELECT lab.operator_resume('Migrated ledger resume reviewed')")
    assert paused and verify_events(app.export_events())["valid"]
    final = audit_state(root)[1]
    assert final["execution_halt_releases"] == (1, 1)
