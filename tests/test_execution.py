"""Synthetic broker history in private disposable databases. No external order requests."""

import copy
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

from catalyst_lab import localdb
from catalyst_lab.alpaca import (
    PAPER_ENDPOINT,
    AlpacaCredentials,
    AlpacaPaperClient,
    FixedStreamConnection,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.broker_ledger import BrokerLedger
from catalyst_lab.broker_runtime import BrokerMonitor
from catalyst_lab.domain import Candidate
from catalyst_lab.execution import (
    ExecutionService,
    Phase3Gate,
    SubmissionDisabled,
    TimeExits,
    bracket,
)
from catalyst_lab.market import MarketDataError, Session
from catalyst_lab.reconciliation import Reconciler
from catalyst_lab.repository import Repository
from catalyst_lab.watcher import Watcher
from tests.conftest import NOW


@pytest.fixture(scope="module")
def pristine_cluster():
    with tempfile.TemporaryDirectory(prefix="catalyst-execution-", dir="/tmp") as directory:
        root = Path(directory)
        localdb.start(root)
        try:
            yield root
        finally:
            localdb.stop(root)


@pytest.fixture
def er(pristine_cluster):
    name = "test_" + uuid4().hex
    admin = localdb.connection_url(pristine_cluster, "lab_owner").replace(
        "dbname=catalyst_lab", "dbname=postgres"
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE catalyst_lab").format(sql.Identifier(name))
        )
    repo = Repository(
        localdb.connection_url(pristine_cluster).replace("dbname=catalyst_lab", "dbname=" + name)
    )
    try:
        yield repo
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture
def paper():
    requests = []

    def transport(request):
        requests.append(request)
        raise AssertionError("No network request is allowed in mutation tests")

    client = AlpacaPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "test-secret"),
        transport=httpx.MockTransport(transport),
    )
    client.requests = requests
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def confirmed(er, raw, evidence, policy):
    record = er.submit(raw, NOW, lambda c, now: evidence, policy)
    cid = record["candidate_id"]
    with er.connect() as conn:
        er.transition(conn, cid, "VALIDATED", "WATCHING", {"reason": "LAB_FIXTURE"})
        er.transition(conn, cid, "WATCHING", "TRIGGER_CONFIRMED", {"reason": "LAB_FIXTURE"})
    return cid


@pytest.fixture
def submitted(er, confirmed, paper, raw):
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.execution import system_event
    from catalyst_lab.risk import RiskEngine, RiskPolicy

    service = ExecutionService(er, paper)
    intent = service.prepare_entry(confirmed, 10)
    risk_repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    engine = RiskEngine(risk_repo, paper, RiskPolicy(5, "BROKER_PREVIOUS_CLOSE"))
    engine.classify(raw["ticker"], raw["ticker"], raw["ticker"], "SIMULATED_HISTORY")
    with risk_repo.connect() as conn:
        er.transition(
            conn, confirmed, "TRIGGER_CONFIRMED", "RISK_CHECK", {"reason": "SIMULATED_HISTORY"}
        )
        decision = engine._decision(
            conn,
            candidate_id=confirmed,
            action="ENTRY",
            session_date=NOW.date(),
            equity=D("1150"),
            reason="SIMULATED_HISTORY",
            approved=True,
            payload=intent["payload_json"],
            method="POST",
            path="/v2/orders",
            qty=10,
            budget=D("11.5"),
            planned=D("11.5"),
        )
        event = system_event(er, conn, "SIMULATED_RESERVATION", {}, confirmed)
        conn.execute(
            "INSERT INTO lab.risk_reservations VALUES(%s,%s,11.5,11.5,10,100.15,%s,%s,%s)",
            (confirmed, decision["risk_decision_id"], raw["ticker"], raw["ticker"], event["seq"]),
        )
        er.transition(
            conn,
            confirmed,
            "RISK_CHECK",
            "ORDER_SUBMITTED",
            {"reason": "SIMULATED_HISTORY", "risk_decision_id": str(decision["risk_decision_id"])},
        )
    base = {
        "symbol": raw["ticker"],
        "qty": "10",
        "filled_qty": "0",
        "asset_class": "us_equity",
        "time_in_force": "day",
        "updated_at": NOW.isoformat(),
    }
    order = {
        **base,
        "id": str(uuid4()),
        "client_order_id": intent["payload_json"]["client_order_id"],
        "side": "buy",
        "type": "limit",
        "limit_price": "100.15",
        "stop_price": None,
        "status": "new",
        "order_class": "bracket",
        "extended_hours": False,
    }
    order["legs"] = [
        {
            **base,
            "id": str(uuid4()),
            "side": "sell",
            "type": "stop",
            "limit_price": None,
            "stop_price": "99",
            "status": "held",
        },
        {
            **base,
            "id": str(uuid4()),
            "side": "sell",
            "type": "limit",
            "limit_price": raw["target"],
            "stop_price": None,
            "status": "held",
        },
    ]
    ledger = BrokerLedger(er)
    oid = ledger.register_submitted(confirmed, intent, order)

    def event(
        kind="fill",
        *,
        role="ENTRY",
        qty="10",
        price="100.10",
        cumulative=None,
        seconds=1,
        status=None,
        execution_id=None,
        total_qty=None,
        reason=None,
    ):
        source = order if role == "ENTRY" else order["legs"][0 if role == "STOP" else 1]
        observed = copy.deepcopy(source)
        observed.update(
            filled_qty=cumulative if cumulative is not None else qty,
            status=status or {"fill": "filled", "partial_fill": "partially_filled"}.get(kind, kind),
            updated_at=(NOW + timedelta(seconds=seconds)).isoformat(),
        )
        if total_qty:
            observed["qty"] = total_qty
        data = {"event": kind, "order": observed, "timestamp": observed["updated_at"]}
        if kind in {"fill", "partial_fill"}:
            data.update(execution_id=execution_id or str(uuid4()), qty=qty, price=price)
        if reason:
            data["reason"] = reason
        return data

    return {
        "cid": confirmed,
        "oid": oid,
        "order": order,
        "ledger": ledger,
        "event": event,
        "service": service,
    }


def consume(fixture, event):
    return fixture["ledger"].consume(event, NOW + timedelta(hours=1))


def state(er, fixture):
    return er.get_candidate(fixture["cid"])["state"]


def counts(er):
    with er.connect() as conn:
        return {
            name: conn.execute(
                sql.SQL("SELECT count(*) AS n FROM lab.{}").format(sql.Identifier(name))
            ).fetchone()["n"]
            for name in ["orders", "fills", "broker_events", "execution_halts"]
        }


@pytest.mark.parametrize("qty", [0, -1, 1.2, 1.0, True, "10", D("10")])
def test_whole_share_inputs_are_strict(raw, qty):
    with pytest.raises(ValueError):
        bracket(Candidate.model_validate(raw), qty, uuid4())


def test_bracket_uses_max_not_trigger_and_never_sizes(raw):
    result = bracket(Candidate.model_validate(raw), 7, uuid4())
    assert result["qty"] == "7" and result["limit_price"] == "100.15"
    assert result["stop_loss"] == {"stop_price": "99.00"}
    assert result["take_profit"] == {"limit_price": "102.45"}
    assert result["side"] == "buy" and result["type"] == "limit"
    assert result["time_in_force"] == "day" and result["order_class"] == "bracket"
    assert result["extended_hours"] is False


def test_invalid_price_precision_is_not_silently_rounded(raw):
    candidate = Candidate.model_validate(raw | {"max_entry_price": "100.151"})
    with pytest.raises(ValueError, match="precision"):
        bracket(candidate, 1, uuid4())


@pytest.mark.parametrize(
    "key", ["AKFIXTURE000000000001", "UNKNOWN0000000000001", "pkfixture0000000001", ""]
)
def test_nonpaper_key_identifiers_are_rejected_before_transport(key):
    with pytest.raises(ValueError, match="paper key"):
        AlpacaCredentials(key, "test-secret")


def test_forbidden_live_endpoint_is_rejected(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", PAPER_ENDPOINT.replace("paper-", ""))
    with pytest.raises(ValueError, match="fixed Alpaca Paper"):
        AlpacaCredentials.from_env()
    with pytest.raises(ValueError):
        FixedStreamConnection(
            "wss://" + PAPER_ENDPOINT.split("://")[1].replace("paper-", "") + "/stream"
        )


def test_submission_impossible_even_if_flag_environment_or_instance_is_forged(paper, monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper.__dict__["trading_enabled"] = True
    assert paper.trading_enabled is False and Phase3Gate().trading_enabled is False
    with pytest.raises(AttributeError):
        paper.trading_enabled = True
    for method, argument in [
        (paper.submit_bracket, {}),
        (paper.cancel_order, "fixture"),
        (paper.flatten_position, {}),
    ]:
        with pytest.raises(SubmissionDisabled, match="PHASE_4"):
            method(argument)
    assert paper.requests == []


def test_service_gate_cannot_be_bypassed_by_an_allowing_client(er, confirmed):
    class AllowingClient:
        def submit_bracket(self, payload):
            raise AssertionError("The gate must stop before this call")

    service = ExecutionService(er, AllowingClient())
    intent = service.prepare_entry(confirmed, 10)
    assert service.attempt(intent) is False
    assert counts(er)["orders"] == 0
    with er.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.system_events WHERE event_type='SUBMISSION_DENIED'"
            ).fetchone()["n"]
            == 1
        )


def test_intent_idempotence_and_validation_separation(er, confirmed, paper, raw, evidence, policy):
    service = ExecutionService(er, paper)
    first = service.prepare_entry(confirmed, 10)
    assert service.prepare_entry(confirmed, 10)["intent_id"] == first["intent_id"]
    with pytest.raises(ValueError, match="immutable"):
        service.prepare_entry(confirmed, 11)
    with pytest.raises(ValueError):
        service.prepare_entry(uuid4(), 1)
    assert er.get_candidate(confirmed)["state"] == "TRIGGER_CONFIRMED"
    assert counts(er)["orders"] == 0


def test_partial_full_and_target_exit_advance_from_individual_fills(er, submitted):
    assert (
        consume(submitted, submitted["event"]("partial_fill", qty="4", cumulative="4"))
        == "RECORDED"
    )
    assert state(er, submitted) == "PARTIALLY_FILLED"
    assert consume(submitted, submitted["event"](qty="6", cumulative="10", seconds=2)) == "RECORDED"
    assert state(er, submitted) == "OPEN"
    assert (
        consume(submitted, submitted["event"](role="TARGET", price="102", seconds=3)) == "RECORDED"
    )
    assert state(er, submitted) == "CLOSED"
    assert counts(er)["fills"] == 3
    assert counts(er)["execution_halts"] == 0
    states = [
        r["payload_json"]["to_state"]
        for r in er.candidate_events(submitted["cid"])
        if r["event_type"] == "STATE_TRANSITION"
    ]
    assert states[-5:] == ["PARTIALLY_FILLED", "FILLED", "OPEN", "TARGET_EXIT", "CLOSED"]
    assert verify_events(er.export_events())["valid"]


def test_direct_full_fill_does_not_invent_a_partial(er, submitted):
    consume(submitted, submitted["event"]())
    states = [
        r["payload_json"]["to_state"]
        for r in er.candidate_events(submitted["cid"])
        if r["event_type"] == "STATE_TRANSITION"
    ]
    assert states[-2:] == ["FILLED", "OPEN"] and "PARTIALLY_FILLED" not in states


def test_partial_target_and_reduced_stop_preserve_actual_exposure(er, submitted):
    consume(submitted, submitted["event"]())
    consume(
        submitted,
        submitted["event"](
            "partial_fill", role="TARGET", qty="4", cumulative="4", price="102", seconds=2
        ),
    )
    assert state(er, submitted) == "OPEN"
    consume(
        submitted, submitted["event"](role="STOP", qty="6", total_qty="6", price="98.98", seconds=3)
    )
    assert state(er, submitted) == "CLOSED"
    assert counts(er)["fills"] == 3 and counts(er)["execution_halts"] == 0


@pytest.mark.parametrize(
    ("kind", "result"),
    [("canceled", "CANCELED"), ("rejected", "BROKER_REJECTED"), ("expired", "CANCELED")],
)
def test_terminal_entry_reasons(er, submitted, kind, result):
    consume(submitted, submitted["event"](kind, qty="0", reason="fixture broker reason"))
    record = er.get_candidate(submitted["cid"])
    assert record["state"] == result and record["state_reason"] == "fixture broker reason"


def test_canceled_partial_entry_does_not_hide_position(er, submitted):
    consume(submitted, submitted["event"]("partial_fill", qty="4", cumulative="4"))
    consume(submitted, submitted["event"]("canceled", qty="4", seconds=2))
    assert state(er, submitted) == "OPEN"
    with er.connect() as conn:
        assert conn.execute("SELECT qty FROM lab.strategy_positions").fetchone()["qty"] == 4
    assert counts(er)["execution_halts"] > 0


def test_duplicates_and_conflicting_execution_ids(er, submitted):
    event = submitted["event"]()
    assert consume(submitted, event) == "RECORDED"
    assert consume(submitted, event) == "DUPLICATE"
    revised = copy.deepcopy(event)
    revised["price"] = "99.50"
    assert consume(submitted, revised) == "QUARANTINED"
    assert counts(er)["fills"] == 1 and counts(er)["broker_events"] == 2


def test_out_of_order_fills_do_not_regress_state_or_double_count(er, submitted):
    consume(submitted, submitted["event"](qty="6", cumulative="10", seconds=2))
    consume(submitted, submitted["event"]("partial_fill", qty="4", cumulative="4", seconds=1))
    assert state(er, submitted) == "OPEN"
    with er.connect() as conn:
        row = conn.execute("SELECT * FROM lab.broker_order_states WHERE role='ENTRY'").fetchone()
        assert row["status"] == "filled" and row["filled_qty"] == 10


def test_unknown_order_is_retained_but_not_adopted(er):
    ledger = BrokerLedger(er)
    assert ledger.consume({"event": "fill", "order": {"id": "unexplained"}}, NOW) == "QUARANTINED"
    assert counts(er) == {"orders": 0, "fills": 0, "broker_events": 1, "execution_halts": 1}


def test_projection_failure_keeps_raw_message_and_rolls_back_fill(er, submitted, monkeypatch):
    def broken(*args):
        raise RuntimeError("private exception detail")

    monkeypatch.setattr(submitted["ledger"], "_advance", broken)
    assert consume(submitted, submitted["event"]()) == "QUARANTINED"
    assert counts(er)["fills"] == 0 and counts(er)["broker_events"] == 1
    assert "private exception detail" not in json.dumps(er.export_events())


def test_unexpected_fractional_fill_is_recorded_honestly_and_halts(er, submitted):
    assert (
        consume(submitted, submitted["event"]("partial_fill", qty=".5", cumulative=".5"))
        == "RECORDED"
    )
    with er.connect() as conn:
        assert conn.execute("SELECT qty FROM lab.fills").fetchone()["qty"] == D(".5")
    assert counts(er)["execution_halts"] == 1


class SnapshotClient:
    def __init__(self, positions=(), orders=()):
        self.position_rows, self.order_rows = list(positions), list(orders)
        self.calls = []

    def account(self):
        self.calls.append("GET account")
        return {"status": "ACTIVE"}

    def positions(self):
        self.calls.append("GET positions")
        return copy.deepcopy(self.position_rows)

    def open_orders(self):
        self.calls.append("GET orders")
        return copy.deepcopy(self.order_rows)


def test_startup_restart_and_freshness_gates(er):
    client = SnapshotClient()
    reconciler = Reconciler(er, client, clock=lambda: NOW)
    assert not reconciler.ready()
    assert reconciler.run_once()["clean"] and reconciler.ready()
    assert not reconciler.ready(NOW + timedelta(seconds=61))
    restarted = Reconciler(er, client, clock=lambda: NOW)
    assert not restarted.ready()
    assert restarted.run_once()["clean"] and restarted.ready()
    with er.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.reconciliation_runs WHERE startup"
            ).fetchone()["n"]
            == 2
        )


def test_position_mismatch_is_logged_without_repair_and_halt_survives_restart(er):
    client = SnapshotClient([{"symbol": "AAPL", "qty": "5"}])
    reconciler = Reconciler(er, client, clock=lambda: NOW)
    result = reconciler.run_once()
    assert result["discrepancies"][0]["code"] == "POSITION_QTY_MISMATCH"
    assert not reconciler.ready() and counts(er)["orders"] == counts(er)["fills"] == 0
    client.position_rows = []
    restarted = Reconciler(er, client, clock=lambda: NOW)
    assert restarted.run_once()["clean"] and not restarted.ready()
    with er.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.system_events WHERE event_type='BROKER_MISMATCH'"
            ).fetchone()["n"]
            == 1
        )


def test_matching_nested_bracket_and_all_order_field_mismatches(er, submitted):
    client = SnapshotClient(orders=[submitted["order"]])
    reconciler = Reconciler(er, client, clock=lambda: NOW)
    assert reconciler.run_once()["clean"]
    client.order_rows[0]["limit_price"] = "100.14"
    client.order_rows[0]["qty"] = "11"
    result = reconciler.run_once()
    fields = {d.get("field") for d in result["discrepancies"]}
    assert {"limit_price", "qty"} <= fields
    assert counts(er)["fills"] == 0


def test_missing_local_orders_are_each_logged(er, submitted):
    result = Reconciler(er, SnapshotClient(), clock=lambda: NOW).run_once()
    assert len(result["discrepancies"]) == 3
    assert {d["code"] for d in result["discrepancies"]} == {"LOCAL_ORDER_MISSING_AT_BROKER"}


@pytest.mark.parametrize("interval", [29, 61])
def test_reconcile_interval_cannot_escape_required_bounds(er, interval):
    with pytest.raises(ValueError):
        Reconciler(er, SnapshotClient(), interval_seconds=interval)


def test_watching_requires_actual_clean_startup(er, raw, evidence, policy):
    cid = er.submit(raw, NOW, lambda c, now: evidence, policy)["candidate_id"]
    reconciler = Reconciler(er, SnapshotClient(), clock=lambda: NOW)
    watcher = Watcher(er, reconciliation_gate=reconciler.ready)
    sessions = {NOW.date(): Session(NOW.date(), evidence.official_open, evidence.official_close)}
    watcher.tick(sessions, NOW, {raw["ticker"]})
    assert er.get_candidate(cid)["state"] == "VALIDATED"
    reconciler.run_once()
    watcher.tick(sessions, NOW, {raw["ticker"]})
    assert er.get_candidate(cid)["state"] == "WATCHING"


@pytest.mark.parametrize(
    ("close_hour", "flatten_hour", "flatten_minute"), [(16, 15, 55), (13, 12, 55)]
)
def test_time_exit_fires_once_at_calendar_deadline_and_cannot_submit(
    er, submitted, paper, close_hour, flatten_hour, flatten_minute
):
    consume(submitted, submitted["event"]())
    session = Session.from_calendar(
        {"date": NOW.date().isoformat(), "open": "09:30", "close": f"{close_hour:02d}:00"}
    )
    assert (
        session.flatten_time.hour == flatten_hour and session.flatten_time.minute == flatten_minute
    )
    timer = TimeExits(submitted["service"])
    assert timer.tick(session, session.flatten_time - timedelta(microseconds=1)) == []
    intents = timer.tick(session, session.flatten_time)
    assert len(intents) == 1 and intents[0]["payload_json"]["type"] == "market"
    assert intents[0]["payload_json"]["qty"] == "10"
    assert timer.tick(session, session.flatten_time + timedelta(seconds=1)) == []
    assert state(er, submitted) == "OPEN" and paper.requests == []
    with er.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.system_events WHERE event_type='TIME_EXIT_DUE'"
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.system_events WHERE event_type='SUBMISSION_DENIED'"
            ).fetchone()["n"]
            == 1
        )


def test_calendar_absence_and_wrong_day_do_not_invent_exit_times(er, submitted):
    consume(submitted, submitted["event"]())
    timer = TimeExits(submitted["service"])
    assert timer.tick(None, NOW.replace(hour=16)) == []
    session = Session.from_calendar(
        {"date": NOW.date().isoformat(), "open": "09:30", "close": "16:00"}
    )
    assert timer.tick(session, NOW + timedelta(days=1)) == []


def test_known_time_exit_fill_closes_with_time_reason(er, submitted, paper):
    consume(submitted, submitted["event"]())
    session = Session.from_calendar(
        {"date": NOW.date().isoformat(), "open": "09:30", "close": "16:00"}
    )
    intent = TimeExits(submitted["service"]).tick(session, session.flatten_time)[0]
    receipt = {
        **intent["payload_json"],
        "id": str(uuid4()),
        "status": "new",
        "filled_qty": "0",
    }
    submitted["ledger"].register_time_exit(submitted["cid"], intent, receipt)
    event = {
        "event": "fill",
        "execution_id": str(uuid4()),
        "qty": "10",
        "price": "101.00",
        "timestamp": session.flatten_time.isoformat(),
        "order": {**receipt, "status": "filled", "filled_qty": "10"},
    }
    assert submitted["ledger"].consume(event, session.closes) == "RECORDED"
    result = er.get_candidate(submitted["cid"])
    assert result["state"] == "CLOSED" and result["status"] == "closed_time"
    assert result["state_reason"] == "TIME_EXIT" and D(result["exit_price"]) == D("101")
    assert paper.requests == []


@pytest.mark.parametrize(
    "table",
    [
        "order_intents",
        "orders",
        "fills",
        "broker_events",
        "order_links",
        "order_updates",
        "reconciliation_runs",
        # Migration 015: halt rows live here; lab.execution_halts is now the unreleased view.
        "execution_halt_records",
    ],
)
def test_all_execution_relations_reject_application_mutation(er, table):
    column = "event_id" if table in {"orders", "fills"} else "event_seq"
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            sql.SQL("UPDATE lab.{} SET {}={}").format(
                sql.Identifier(table), sql.Identifier(column), sql.Identifier(column)
            )
        )
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(sql.SQL("DELETE FROM lab.{}").format(sql.Identifier(table)))
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(sql.SQL("TRUNCATE lab.{} CASCADE").format(sql.Identifier(table)))


def test_http_transport_itself_blocks_mutations_and_wrong_host(paper):
    for method in ["POST", "PUT", "PATCH", "DELETE"]:
        with pytest.raises(SubmissionDisabled):
            paper._client.request(method, PAPER_ENDPOINT + "/v2/orders", json={"qty": 1})
    with pytest.raises(MarketDataError, match="ENDPOINT_NOT_ALLOWED"):
        paper._client.get(PAPER_ENDPOINT.replace("paper-", "") + "/v2/account")
    paper._client.headers["APCA-API-KEY-ID"] = "AKFIXTURE000000000001"
    with pytest.raises(MarketDataError, match="PAPER_CREDENTIAL_REQUIRED"):
        paper.account()
    assert paper.requests == []


def test_late_fill_after_cancellation_restores_exposure_with_halt(er, submitted):
    consume(submitted, submitted["event"]("canceled", qty="0"))
    assert state(er, submitted) == "CANCELED"
    consume(submitted, submitted["event"](seconds=2))
    assert state(er, submitted) == "OPEN" and counts(er)["execution_halts"] > 0


def test_fill_readback_uses_execution_prices_not_broker_average(er, submitted):
    first = submitted["event"]("partial_fill", qty="4", cumulative="4", price="100.01")
    first["order"]["filled_avg_price"] = "777.00"
    consume(submitted, first)
    consume(submitted, submitted["event"](qty="6", cumulative="10", price="100.11", seconds=2))
    record = er.get_candidate(submitted["cid"])
    assert record["status"] == "open" and record["size_shares"] == 10
    assert D(record["fill_price"]) == D("100.07")
    assert record["net_r"] is None and record["mfe_r"] is None


def test_reconciliation_detects_stream_race_without_latching_false_mismatch(er):
    class RacingClient(SnapshotClient):
        def positions(self):
            with er.connect() as conn:
                event = er.append_event(conn, "SYSTEM_EVENT", {"kind": "TEST_BROKER_RACE"})
                conn.execute(
                    "INSERT INTO lab.broker_events VALUES (%s,%s,NULL,NULL,NULL,'{}')",
                    (event["seq"], uuid4().hex),
                )
            return []

    reconciler = Reconciler(er, RacingClient(), clock=lambda: NOW)
    result = reconciler.run_once()
    assert result["discrepancies"][0]["code"] == "BROKER_UPDATE_DURING_SNAPSHOT"
    assert not reconciler.ready() and counts(er)["execution_halts"] == 0


def test_failed_snapshot_does_not_open_gate_or_hide_failure(er):
    class BrokenClient(SnapshotClient):
        def positions(self):
            raise RuntimeError("private-header-secret")

    reconciler = Reconciler(er, BrokenClient(), clock=lambda: NOW)
    assert not reconciler.run_once()["clean"] and not reconciler.ready()
    assert "private-header-secret" not in json.dumps(er.export_events())


@pytest.mark.parametrize(
    "table",
    [
        "order_intents",
        "broker_events",
        "order_links",
        "order_updates",
        "reconciliation_runs",
        "execution_halt_records",  # TRUNCATE of the view is refused as "not a table".
        "fills",
        "orders",
    ],
)
def test_database_triggers_reject_owner_truncate(er, table):
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=lab_owner")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(sql.SQL("TRUNCATE lab.{} CASCADE").format(sql.Identifier(table)))


def test_broker_nanoseconds_prevent_late_status_regression(er, submitted):
    later = submitted["event"]()
    later["timestamp"] = later["order"]["updated_at"] = "2026-09-18T14:00:00.123456789Z"
    assert consume(submitted, later) == "RECORDED"
    earlier = submitted["event"]("new", qty="0")
    earlier["timestamp"] = earlier["order"]["updated_at"] = "2026-09-18T14:00:00.123456001Z"
    assert consume(submitted, earlier) == "RECORDED"
    with er.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM lab.broker_order_states WHERE role='ENTRY'"
            ).fetchone()["status"]
            == "filled"
        )
    assert state(er, submitted) == "OPEN"


class BrokerSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.on_empty = lambda: None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self, timeout):
        if self.frames:
            return json.dumps(self.frames.pop(0)).encode()  # paper stream supports binary JSON
        self.on_empty()
        raise TimeoutError


def test_trade_updates_protocol_persists_fills_even_before_listen_ack(er, submitted, paper):
    socket = BrokerSocket(
        [
            {"stream": "authorization", "data": {"status": "authorized"}},
            {
                "stream": "trade_updates",
                "data": submitted["event"]("partial_fill", qty="4", cumulative="4"),
            },
            {"stream": "listening", "data": {"streams": ["trade_updates"]}},
            {
                "stream": "trade_updates",
                "data": submitted["event"](qty="6", cumulative="10", seconds=2),
            },
        ]
    )

    def connector(url, **kwargs):
        assert url == "wss://paper-api.alpaca.markets/stream" and kwargs["proxy"] is None
        assert not kwargs["logger"].isEnabledFor(10)
        return socket

    # Before the stream counts as connected the monitor reads REST fills and order states
    # (plan 4.4 backfill); those GETs are the only requests, and none is a mutation.
    orders = {o["id"]: o for o in [submitted["order"], *submitted["order"]["legs"]]}
    reads = []

    def rest(request):
        reads.append((request.method, request.url.path))
        if request.url.path == "/v2/account/activities":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=orders[request.url.path.rsplit("/", 1)[-1]])

    reader = AlpacaPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "test-secret"),
        transport=httpx.MockTransport(rest),
    )
    monitor = BrokerMonitor(
        er, reader, clock=lambda: NOW + timedelta(hours=1), connector=connector
    )
    socket.on_empty = monitor.stop_event.set
    monitor._stream_session()
    reader.close()
    assert socket.sent[0]["action"] == "auth"
    assert socket.sent[1] == {"action": "listen", "data": {"streams": ["trade_updates"]}}
    assert counts(er)["fills"] == 2 and state(er, submitted) == "OPEN"
    assert paper.requests == [] and "test-secret" not in json.dumps(er.export_events())
    assert ("GET", "/v2/account/activities") in reads
    assert {method for method, _ in reads} == {"GET"}


def test_unauthorized_broker_stream_cannot_consume_order_data(er, paper):
    socket = BrokerSocket([{"stream": "authorization", "data": {"status": "unauthorized"}}])
    monitor = BrokerMonitor(er, paper, connector=lambda *a, **k: socket)
    with pytest.raises(MarketDataError, match="AUTH_FAILED"):
        monitor._stream_session()
    assert not monitor.ready() and counts(er)["broker_events"] == 0
    assert len(socket.sent) == 1


def test_monitor_requires_current_reconciliation_and_connected_stream(er):
    monitor = BrokerMonitor(er, SnapshotClient(), clock=lambda: NOW)
    assert not monitor.ready()
    monitor.reconciler.run_once()
    assert not monitor.ready()
    monitor.connected = True
    assert monitor.ready()
    monitor.connected = False
    assert not monitor.ready()


def test_reconciliation_loop_waits_for_configured_cadence(er, monkeypatch):
    monitor = BrokerMonitor(er, SnapshotClient(), clock=lambda: NOW)
    waits = []

    class StopAfterOne:
        def is_set(self):
            return bool(waits)

        def wait(self, seconds):
            waits.append(seconds)

    ticks = iter([100.0, 102.0])
    monkeypatch.setattr("catalyst_lab.broker_runtime.time.monotonic", lambda: next(ticks))
    monitor.stop_event = StopAfterOne()
    monitor._reconciliation_loop()
    assert waits == [43.0]  # 45-second start cadence includes the two-second REST poll.
    assert monitor.reconciler.startup_clean
