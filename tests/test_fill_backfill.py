"""Fill backfill after a trade-updates gap (plan 4.4): fake venues and disposable DBs only.

Alpaca FILL activities carry no execution id, so fills are deduplicated on (broker order
id, cumulative filled quantity): an outage fill is recorded once from REST, and the late
stream copy of the same execution is recognised instead of exceeding the order quantity.
V1 runs the backfill before ``BrokerMonitor`` marks the stream connected; the managed
runtime runs it before the reconciliation of each new trade-updates connection. No
provider or broker network is used and no order is sent. Fixture evidence only.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import PAPER_ENDPOINT, AlpacaCredentials, AlpacaPaperClient
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.broker_budget import BrokerBudget
from catalyst_lab.broker_ledger import BrokerLedger, rest_fill_id
from catalyst_lab.broker_runtime import BrokerMonitor
from catalyst_lab.execution import ExecutionService, system_event
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_latches import PROTECTION, REST
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.market import MarketDataError
from tests.clock import fixture_now
from tests.fake_paper_broker import FakePaperBroker
from tests.test_broker_budget import CountingVenue, noon_after
from tests.test_execution import BrokerSocket
from tests.test_execution import confirmed as confirmed
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import admit_enter, observation

CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
AUTHORIZED = {"stream": "authorization", "data": {"status": "authorized"}}
LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}


# --- The paginated FILL activity reader ------------------------------------------------


def test_fill_activity_reader_pages_oldest_first_and_refuses_an_incomplete_read():
    rows = [{"id": f"{n:05d}::fixture", "activity_type": "FILL"} for n in range(250)]
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        start = 0
        if "page_token" in params:
            start = [r["id"] for r in rows].index(params["page_token"]) + 1
        return httpx.Response(200, json=rows[start : start + int(params["page_size"])])

    client = AlpacaPaperClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    after = datetime(2026, 9, 24, 13, 55, 0, 250000, tzinfo=UTC)
    try:
        assert client.fill_activities_since(after) == rows
        assert [p.get("page_token") for p in seen] == [None, "00099::fixture", "00199::fixture"]
        assert all(
            p["activity_types"] == "FILL" and p["direction"] == "asc"
            and p["page_size"] == "100" and p["after"] == "2026-09-24T13:55:00.250000Z"
            for p in seen
        )
        with pytest.raises(MarketDataError, match="AWARE_ACTIVITY_WINDOW_REQUIRED"):
            client.fill_activities_since(after.replace(tzinfo=None))
    finally:
        client.close()

    def repeating(request):
        return httpx.Response(200, json=rows[:100])  # The page token never advances.

    looping = AlpacaPaperClient(CREDENTIALS, transport=httpx.MockTransport(repeating))
    try:
        with pytest.raises(MarketDataError, match="INCOMPLETE_FILL_ACTIVITIES"):
            looping.fill_activities_since(after)
    finally:
        looping.close()
    limited = AlpacaPaperClient(
        CREDENTIALS, transport=httpx.MockTransport(lambda r: httpx.Response(429, json={}))
    )
    try:
        with pytest.raises(MarketDataError, match="ALPACA_HTTP_429"):
            limited.fill_activities_since(after)
    finally:
        limited.close()


# --- V1: BrokerMonitor backfills before the stream counts as connected -------------------


def v1_entry(er, candidate_id, raw, fake, qty):
    """A registered V1 bracket at the fake venue: the ``submitted`` fixture, sized ``qty``."""
    from catalyst_lab.risk import RiskEngine, RiskPolicy

    client = AlpacaPaperClient(CREDENTIALS, transport=httpx.MockTransport(fake.handle))
    intent = ExecutionService(er, client).prepare_entry(candidate_id, qty)
    risk_repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    engine = RiskEngine(risk_repo, client, RiskPolicy(5, "BROKER_PREVIOUS_CLOSE"))
    engine.classify(raw["ticker"], raw["ticker"], raw["ticker"], "SIMULATED_HISTORY")
    budget = D("1.15") * qty  # qty × (max entry − stop), exactly 1% of the fixture equity.
    with risk_repo.connect() as conn:
        er.transition(
            conn, candidate_id, "TRIGGER_CONFIRMED", "RISK_CHECK", {"reason": "SIMULATED_HISTORY"}
        )
        decision = engine._decision(
            conn, candidate_id=candidate_id, action="ENTRY", session_date=fake.now.date(),
            equity=budget * 100, reason="SIMULATED_HISTORY", approved=True,
            payload=intent["payload_json"], method="POST", path="/v2/orders", qty=qty,
            budget=budget, planned=budget,
        )
        event = system_event(er, conn, "SIMULATED_RESERVATION", {}, candidate_id)
        conn.execute(
            "INSERT INTO lab.risk_reservations VALUES(%s,%s,%s,%s,%s,100.15,%s,%s,%s)",
            (candidate_id, decision["risk_decision_id"], budget, budget, qty, raw["ticker"],
             raw["ticker"], event["seq"]),
        )
        er.transition(
            conn, candidate_id, "RISK_CHECK", "ORDER_SUBMITTED",
            {"reason": "SIMULATED_HISTORY",
             "risk_decision_id": str(decision["risk_decision_id"])},
        )
    response = fake.handle(
        httpx.Request("POST", PAPER_ENDPOINT + "/v2/orders", json=intent["payload_json"])
    )
    order = response.json()
    BrokerLedger(er).register_submitted(candidate_id, intent, order)
    return client, order


@pytest.fixture
def v1(er, confirmed, raw):
    fake = FakePaperBroker()
    fake.now = fixture_now()  # After the link's database registration time.
    client, order = v1_entry(er, confirmed, raw, fake, 10)
    monitor = BrokerMonitor(er, client, clock=lambda: fake.now + timedelta(seconds=30))
    yield SimpleNamespace(fake=fake, client=client, order=order, monitor=monitor, cid=confirmed)
    client.close()


def reconnect(monitor, *frames):
    """One trade-updates session: authorization, listen, then ``frames``, then shutdown."""
    socket = BrokerSocket([AUTHORIZED, LISTENING, *({"stream": "trade_updates", "data": f}
                                                     for f in frames)])
    socket.on_empty = monitor.stop_event.set
    monitor.connector = lambda *_, **__: socket
    monitor.stop_event.clear()
    monitor._stream_session()
    return socket


def v1_rows(er, sql, *args):
    with er.connect() as conn:
        return conn.execute(sql, args).fetchall()


def v1_state(er, cid):
    return er.get_candidate(cid)["state"]


def halts(er):
    return [row["reason"] for row in v1_rows(er, "SELECT reason FROM lab.execution_halts")]


def reconnect_events(er):
    return [
        row["payload_json"]
        for row in v1_rows(
            er, "SELECT payload_json FROM lab.system_events WHERE event_type='WEBSOCKET_RECONNECT'"
            " ORDER BY created_at"
        )
    ]


def test_v1_outage_fill_is_recorded_once_and_its_late_stream_copy_is_not_counted(er, v1):
    fake, entry = v1.fake, v1.order["id"]
    fake.execute(entry, 10)  # The whole entry fills while the stream is down.
    late = fake.events.pop()  # Its stream message arrives only after the reconnect.
    reconnect(v1.monitor, late)
    fills = v1_rows(er, "SELECT fill_id,qty,price,side,role FROM lab.fills")
    assert [(f["fill_id"], f["qty"], f["role"]) for f in fills] == [
        (rest_fill_id(entry, 10), D(10), "ENTRY")
    ]
    assert v1_state(er, v1.cid) == "OPEN" and halts(er) == []
    kinds = v1_rows(er, "SELECT broker_event_type FROM lab.broker_events ORDER BY event_seq")
    assert [k["broker_event_type"] for k in kinds][:1] == ["fill"]  # The REST activity.
    assert "fill" in [k["broker_event_type"] for k in kinds][1:]  # The late stream copy.
    first = reconnect_events(er)[-1]
    assert first["rest_backfill"]["fills_recorded"] == 1 and v1.monitor.connected
    # Every REST item is a BROKER_REST_BACKFILL event carrying the raw object as evidence.
    backfill = v1_rows(
        er, "SELECT payload_json FROM lab.system_events WHERE event_type='BROKER_REST_BACKFILL'"
    )
    assert backfill[0]["payload_json"]["source"] == "ALPACA_REST_FILL_ACTIVITY"
    assert backfill[0]["payload_json"]["message"]["cum_qty"] == "10"
    # The V1 trade projection counts the REST fill exactly once.
    projected = v1_rows(er, "SELECT bought FROM lab.trade_projection_source")
    assert [row["bought"] for row in projected] == [D(10)]
    # Another reconnect finds nothing new: the activity and its order state are known.
    reconnect(v1.monitor)
    assert len(v1_rows(er, "SELECT 1 FROM lab.fills")) == 1
    again = reconnect_events(er)[-1]["rest_backfill"]
    assert again["fills_recorded"] == again["orders_recorded"] == 0
    assert again["fills_known"] == 1 and halts(er) == []


def test_v1_partial_fill_spanning_the_outage(er, v1):
    fake, entry, ledger = v1.fake, v1.order["id"], v1.monitor.ledger
    fake.execute(entry, 4)
    assert ledger.consume(fake.events.pop(), v1.monitor.now()) == "RECORDED"
    assert v1_state(er, v1.cid) == "PARTIALLY_FILLED"
    fake.now += timedelta(seconds=40)
    fake.execute(entry, 3)  # During the outage.
    lost = fake.events.pop()
    fake.now += timedelta(seconds=40)
    reconnect(v1.monitor, lost)  # The lost message is replayed late: no double count.
    fake.now += timedelta(seconds=5)
    fake.execute(entry, 3)  # After the reconnect the stream delivers the rest as usual.
    assert ledger.consume(fake.events.pop(), v1.monitor.now()) == "RECORDED"
    fills = v1_rows(er, "SELECT fill_id,qty FROM lab.fills ORDER BY timestamp")
    assert [f["qty"] for f in fills] == [D(4), D(3), D(3)]
    assert fills[1]["fill_id"] == rest_fill_id(entry, 7)
    assert v1_state(er, v1.cid) == "OPEN" and halts(er) == []
    status = v1_rows(
        er, "SELECT status,filled_qty FROM lab.broker_order_states WHERE role='ENTRY'"
    )[0]
    assert (status["status"], status["filled_qty"]) == ("filled", D(10))


def test_v1_cancel_during_the_outage_is_recorded_from_the_order_state(er, v1):
    fake, entry = v1.fake, v1.order["id"]
    fake.now += timedelta(seconds=20)
    fake.handle(httpx.Request("DELETE", PAPER_ENDPOINT + "/v2/orders/" + entry))
    fake.events.clear()  # Every cancel message is lost with the stream.
    reconnect(v1.monitor)
    assert v1_state(er, v1.cid) == "CANCELED" and halts(er) == []
    states = {
        row["role"]: row["status"]
        for row in v1_rows(er, "SELECT role,status FROM lab.broker_order_states")
    }
    assert states == {"ENTRY": "canceled", "STOP": "canceled", "TARGET": "canceled"}
    recorded = reconnect_events(er)[-1]["rest_backfill"]
    assert recorded["orders_recorded"] == 3 and recorded["fills_recorded"] == 0


def test_v1_backfill_pages_through_more_than_one_hundred_activities(er, confirmed, raw):
    fake = FakePaperBroker()
    fake.now = fixture_now()
    client, order = v1_entry(er, confirmed, raw, fake, 150)
    monitor = BrokerMonitor(er, client, clock=lambda: fake.now + timedelta(seconds=30))
    try:
        for _ in range(150):  # 150 one-share executions during the outage.
            fake.execute(order["id"], 1)
        fake.events.clear()
        reconnect(monitor)
        pages = [read.get("page_token") for read in fake.activity_reads]
        assert len(pages) == 2 and pages[0] is None and pages[1]
        fills = v1_rows(er, "SELECT sum(qty) AS n, count(*) AS c FROM lab.fills")[0]
        assert (fills["n"], fills["c"]) == (D(150), 150)
        assert v1_state(er, confirmed) == "OPEN" and halts(er) == []
        reconnect(monitor)  # Nothing is recorded twice.
        assert v1_rows(er, "SELECT count(*) AS c FROM lab.fills")[0]["c"] == 150
    finally:
        client.close()


def test_v1_rate_limited_backfill_keeps_the_stream_unready_without_a_halt(er, v1):
    fake, monitor = v1.fake, v1.monitor
    fake.execute(v1.order["id"], 10)
    fake.events.clear()
    fake.activity_status = 429
    with pytest.raises(MarketDataError, match="REST_DEGRADED"):
        reconnect(monitor)
    assert not monitor.connected and not monitor.ready()
    assert halts(er) == [] and v1_rows(er, "SELECT 1 FROM lab.fills") == []
    assert reconnect_events(er) == []  # Never marked connected.
    fake.activity_status = 500
    with pytest.raises(MarketDataError, match="FILL_BACKFILL_UNAVAILABLE"):
        reconnect(monitor)
    fake.activity_status = None
    reconnect(monitor)
    assert monitor.connected and halts(er) == []
    assert len(v1_rows(er, "SELECT 1 FROM lab.fills")) == 1


def test_v1_unknown_order_activity_is_quarantined_not_adopted(er, v1):
    fake = v1.fake
    fake.execute(v1.order["id"], 10)
    fake.events.clear()
    stranger = {**fake.activities[-1], "id": "fixture-unknown", "order_id": "unknown-order-1"}
    fake.activities.append(stranger)
    reconnect(v1.monitor)
    assert halts(er) == ["UNEXPLAINED_BROKER_ORDER"]
    assert len(v1_rows(er, "SELECT 1 FROM lab.fills")) == 1  # Only the owned execution.
    reconnect(v1.monitor)
    assert halts(er) == ["UNEXPLAINED_BROKER_ORDER"]  # Quarantined once.


def test_v1_stream_duplicate_with_conflicting_quantity_halts(er, v1):
    fake, entry, ledger = v1.fake, v1.order["id"], v1.monitor.ledger
    fake.execute(entry, 10)
    late = fake.events.pop()
    reconnect(v1.monitor)
    forged = json.loads(json.dumps(late))
    forged["execution_id"] = "fixture-other-execution"
    forged["price"] = "100.2"  # Same order and cumulative quantity, different price.
    assert ledger.consume(forged, v1.monitor.now()) == "QUARANTINED"
    assert halts(er) == ["CONFLICTING_FILL_EVIDENCE"]
    assert len(v1_rows(er, "SELECT 1 FROM lab.fills")) == 1


# --- Managed: the backfill runs before a new trade-updates connection reconciles --------


class BackfillVenue(CountingVenue):
    """The managed fake venue plus FILL account activities with Alpaca's pagination."""

    def __init__(self, now):
        super().__init__(now)
        self.activities = []
        self.activity_reads = []

    def fill(self, broker_id, qty, *, price="100", fee_qty="0"):
        update = super().fill(broker_id, qty, price=price, fee_qty=fee_qty)
        order = self.orders[broker_id]
        self.activities.append({
            "activity_type": "FILL", "id": f"{len(self.activities):020d}-{uuid4()}",
            "order_id": broker_id, "symbol": order["symbol"].replace("/", ""),
            "side": order["side"], "type": update["event"], "qty": str(qty),
            "price": price, "cum_qty": order["filled_qty"],
            "leaves_qty": str(D(order["qty"]) - D(order["filled_qty"])),
            "order_status": order["status"], "transaction_time": self.now.isoformat(),
        })
        return update

    def handle(self, request):
        params = dict(request.url.params)
        if request.url.path != "/v2/account/activities" or params.get("activity_types") != "FILL":
            return super().handle(request)
        self.requests.append((self.now, request.method, request.url.path))
        self.activity_reads.append(params)
        if self.rate_limited_until is not None and self.now < self.rate_limited_until:
            return httpx.Response(429, headers={"Retry-After": self.retry_after}, json={})
        after = datetime.fromisoformat(params["after"].replace("Z", "+00:00"))
        rows = [a for a in self.activities
                if datetime.fromisoformat(a["transaction_time"]) > after]
        if params.get("page_token"):
            rows = rows[[a["id"] for a in rows].index(params["page_token"]) + 1:]
        return httpx.Response(200, json=rows[: int(params["page_size"])])


@pytest.fixture
def mb(er):
    """A governed managed controller and runtime over the backfill venue."""
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = BackfillVenue(noon_after(datetime.now(UTC)))
    budget = BrokerBudget(clock=lambda: venue.now)
    broker = ManagedPaperBroker(
        CREDENTIALS, ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=budget.transport(httpx.MockTransport(venue.handle)),
    )
    engine = ManagedExecution(
        risk, budget.wrap(broker), policy=engineering_execution_policy(),
        clock=lambda: venue.now, review_store=reviews,
    )
    assert engine.reconcile()["clean"]
    run = ManagedRuntime(
        engine, SimpleNamespace(), SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        CREDENTIALS, engineering_runtime_policy(), clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True,
    )
    run._selected_packets = lambda: []
    yield SimpleNamespace(engine=engine, venue=venue, run=run, mx=(engine, venue, reviews))
    broker.close()


class Socket:
    def __init__(self, frames, run):
        self.frames, self.run, self.sent = list(frames), run, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, timeout):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        self.run.stop_event.set()
        raise TimeoutError


def session(run, *updates):
    """One managed trade-updates session, ended as the stream loop would end it."""
    socket = Socket([AUTHORIZED, LISTENING, *({"stream": "trade_updates", "data": u}
                                              for u in updates)], run)
    run.connector = lambda *_, **__: socket
    run.stop_event.clear()
    run.stream_session()
    with run.lock:  # _stream_loop's ``finally`` after the socket closes.
        run.connected, run.socket, run.reconciled_at = False, None, None
        run.execution.reconciled_at = None
    return socket


def managed(engine, sql, *args):
    with engine.repo.connect() as conn:
        return conn.execute(sql, args).fetchall()


def kinds(engine, kind, setup_id=None):
    return [
        row["body"] for row in managed(
            engine, "SELECT body FROM lab.managed_events WHERE kind=%s"
            " AND (%s::uuid IS NULL OR setup_id=%s) ORDER BY event_seq",
            kind, setup_id, setup_id,
        )
    ]


def managed_fills(engine, sid):
    return managed(
        engine, "SELECT fill_id,qty,price,source FROM lab.managed_fills WHERE setup_id=%s"
        " ORDER BY filled_at,event_seq", sid,
    )


def execution_halts(engine):
    return [row["reason"] for row in managed(engine, "SELECT reason FROM lab.execution_halts")]


def test_managed_outage_fill_is_backfilled_once_and_positions_reconcile(mb):
    engine, venue, run = mb.engine, mb.venue, mb.run
    sid, _ = admit_enter(mb.mx, "BTC/USD")
    entry = venue.orders_of("buy")[0]
    venue.now += timedelta(seconds=20)
    update = venue.fill(entry["id"], entry["qty"])  # Filled while the stream is down.
    venue.now += timedelta(seconds=20)
    session(run, update)  # The lost message arrives late, after the backfill.
    fills = managed_fills(engine, sid)
    assert [(f["fill_id"], f["qty"], f["source"]) for f in fills] == [
        (rest_fill_id(entry["id"], entry["qty"]), D(entry["qty"]), "ALPACA_PAPER_REST_BACKFILL")
    ]
    evidence = kinds(engine, "BROKER_REST_BACKFILL", sid)
    fill_evidence = [b for b in evidence if b["source"] == "ALPACA_REST_FILL_ACTIVITY"]
    assert len(fill_evidence) == 1 and fill_evidence[0]["activity"]["order_id"] == entry["id"]
    positions = kinds(engine, "BROKER_POSITION", sid)
    assert positions[0]["source"] == "ALPACA_REST_POSITIONS"
    assert D(positions[0]["qty"]) == D(entry["qty"]) == D(positions[-1]["qty"])
    assert len(kinds(engine, "BROKER_EVENT", sid)) == 1  # The late copy is still evidence.
    completed = kinds(engine, "BROKER_REST_BACKFILL_COMPLETED")
    assert completed[0]["fills_recorded"] == 1 and completed[0]["positions_recorded"] == 1
    assert engine.reconcile()["clean"] and execution_halts(engine) == []
    assert run.latches.public()["active"] == []
    session(run)  # Another reconnect: nothing new is recorded.
    assert len(managed_fills(engine, sid)) == 1
    assert len(kinds(engine, "BROKER_REST_BACKFILL_COMPLETED")) == 1
    assert engine.reconcile()["clean"]


def test_managed_partial_fill_spanning_the_outage(mb):
    engine, venue, run = mb.engine, mb.venue, mb.run
    sid, _ = admit_enter(mb.mx, "BTC/USD")
    entry = venue.orders_of("buy")[0]
    qty = D(entry["qty"])
    first, second = qty * 4 / 10, qty * 3 / 10
    engine.ingest(venue.fill(entry["id"], str(first)))  # Before the outage.
    venue.now += timedelta(seconds=20)
    lost = venue.fill(entry["id"], str(second))  # During it.
    venue.now += timedelta(seconds=20)
    session(run, lost)
    venue.now += timedelta(seconds=5)
    engine.ingest(venue.fill(entry["id"], str(qty - first - second)))  # After it.
    fills = managed_fills(engine, sid)
    assert [f["qty"] for f in fills] == [first, second, qty - first - second]
    assert fills[1]["fill_id"] == rest_fill_id(entry["id"], first + second)
    assert sum(f["qty"] for f in fills) == qty
    assert engine.reconcile()["clean"] and execution_halts(engine) == []


def test_managed_cancel_during_the_outage_is_recorded_as_rest_evidence(mb):
    engine, venue, run = mb.engine, mb.venue, mb.run
    sid, _ = admit_enter(mb.mx, "ETH/USD")
    entry = venue.orders_of("buy")[0]
    venue.now += timedelta(seconds=20)
    entry["status"] = "canceled"  # The broker cancels while the stream is down.
    session(run)
    states = [b["order"] for b in kinds(engine, "BROKER_REST_BACKFILL", sid)]
    assert [(s["id"], s["status"]) for s in states] == [(entry["id"], "canceled")]
    assert managed_fills(engine, sid) == [] and execution_halts(engine) == []
    session(run)  # The same state is not recorded twice.
    assert len(kinds(engine, "BROKER_REST_BACKFILL", sid)) == 1


def test_managed_backfill_pages_through_more_than_one_hundred_activities(mb):
    engine, venue, run = mb.engine, mb.venue, mb.run
    sid, _ = admit_enter(mb.mx, "BTC/USD")
    entry = venue.orders_of("buy")[0]
    qty = D(entry["qty"])
    step = qty / 200
    venue.now += timedelta(seconds=20)
    for _ in range(119):
        venue.fill(entry["id"], str(step))
    venue.fill(entry["id"], str(qty - 119 * step))
    venue.now += timedelta(seconds=20)
    session(run)
    assert len(venue.activity_reads) == 2  # 100 + 20 rows.
    fills = managed_fills(engine, sid)
    assert len(fills) == 120 and sum(f["qty"] for f in fills) == qty
    assert engine.reconcile()["clean"] and execution_halts(engine) == []
    assert D(kinds(engine, "BROKER_POSITION", sid)[-1]["qty"]) == qty


def test_managed_backfill_records_a_closed_setups_missed_exit_and_needs_no_active_setup(mb):
    engine, venue = mb.engine, mb.venue
    sid, _ = admit_enter(mb.mx, "BTC/USD")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mb.mx))  # Protection placed.
    stop = venue.orders_of("sell", "stop_limit")[0]
    venue.now += timedelta(seconds=20)
    venue.fill(stop["id"], stop["qty"], price="95")  # The stop fills while the stream is down.
    engine.manage(sid, observation(mb.mx, bid="95", ask="95.01"))  # Broker truth: flat.
    assert engine._load(sid)[1]["state"] == "CLOSED" and engine.store.active() == []
    venue.now += timedelta(seconds=20)
    summary = engine.rest_backfill()
    assert summary["fills_recorded"] == 1 and summary["orders_read"] == 0
    fills = managed_fills(engine, sid)
    assert [f["qty"] for f in fills] == [D(entry["qty"]), D(stop["qty"])]
    assert fills[-1]["fill_id"] == rest_fill_id(stop["id"], stop["qty"])
    assert engine.rest_backfill()["fills_recorded"] == 0  # Once.
    assert engine.reconcile()["clean"] and execution_halts(engine) == []


@pytest.mark.parametrize(
    "symbol,fee_fraction,beyond,explained",
    [
        ("BTC/USD", "0", "0", True),  # No fee: the quantities agree.
        ("BTC/USD", "0.01", "0", True),  # The in-kind fee at exactly 1% of the bought qty.
        ("BTC/USD", "0.01", "0.0001", False),  # One quantity increment beyond 1%.
        ("BTC/USD", "0", "-0.0001", False),  # More than the fills explain: never a fee.
        ("SPY", "0", "1", False),  # A stock has no in-kind fee: any shortfall.
    ],
)
def test_managed_backfill_explains_a_shortfall_only_as_a_crypto_fee_up_to_one_percent(
    mb, symbol, fee_fraction, beyond, explained
):
    engine, venue = mb.engine, mb.venue
    sid, _ = admit_enter(mb.mx, symbol)
    entry = venue.orders_of("buy")[0]
    bought = D(entry["qty"])
    shortfall = bought * D(fee_fraction) + D(beyond)
    venue.now += timedelta(seconds=20)
    venue.fill(entry["id"], entry["qty"], fee_qty=str(shortfall))  # While the stream is down.
    venue.now += timedelta(seconds=20)
    summary = engine.rest_backfill()
    assert summary["fills_recorded"] == 1
    rest = [p for p in kinds(engine, "BROKER_POSITION", sid)
            if p.get("source") == "ALPACA_REST_POSITIONS"]
    if explained:
        assert summary["positions_recorded"] == 1 and summary["positions_unexplained"] == []
        assert D(rest[-1]["qty"]) == bought - shortfall
        assert D(rest[-1]["fill_explained_qty"]) == bought
    else:
        assert summary["positions_recorded"] == 0 and rest == []
        [item] = summary["positions_unexplained"]
        assert item["setup_id"] == str(sid) and item["symbol"] == symbol
        assert D(item["broker_qty"]) == bought - shortfall
        assert D(item["fill_explained_qty"]) == bought
    assert execution_halts(engine) == []  # Unexplained blocks entries via reconciliation only.


def test_managed_rate_limited_backfill_keeps_watching_unready_without_a_halt(mb):
    engine, venue, run = mb.engine, mb.venue, mb.run
    sid, _ = admit_enter(mb.mx, "BTC/USD")
    entry = venue.orders_of("buy")[0]
    venue.now += timedelta(seconds=20)
    venue.fill(entry["id"], entry["qty"])
    venue.rate_limited_until, venue.retry_after = venue.now + timedelta(seconds=10), "2"
    run.research_healthy = True
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"BTC/USD"}
    socket = Socket([AUTHORIZED, LISTENING], run)
    run.connector = lambda *_, **__: socket
    run.stream_session()
    assert run.connected and run.reconciled_at is None and not run.ready()
    assert run.error == REST and run.latches.has(REST)
    failed = kinds(engine, "RUNTIME_RECONCILIATION_FAILED")
    assert failed[-1]["reason"] == "FILL_BACKFILL_UNAVAILABLE"
    assert failed[-1]["code"] == "ALPACA_HTTP_429"
    assert execution_halts(engine) == [] and managed_fills(engine, sid) == []
    for _ in range(12):  # Protection keeps running through the storm.
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert execution_halts(engine) == [] and not run.latches.has(PROTECTION)
    assert not run.ready()
    # Later reconciliation passes backfill first; the emptied budget refills by class.
    reconciled = None
    for second in range(90):
        run.execution_once()
        venue.now += timedelta(seconds=1)
        if second % 5 == 4 and run.reconcile_once():
            reconciled = second
            break
    assert reconciled is not None and run.reconciled_at is not None
    assert len(managed_fills(engine, sid)) == 1
    for _ in range(15):
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert run.reconcile_once()
    assert run.error is None and run.ready() and execution_halts(engine) == []
    assert len(managed_fills(engine, sid)) == 1
