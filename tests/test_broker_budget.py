"""Broker request governor: fake clocks, a request-counting fake paper venue, disposable DB.

No provider or broker network is used. The ten-minute simulation drives the real
ManagedRuntime tick, the real shared snapshot and caches, and the real per-setup broker
view (``ManagedExecution._broker_view``) over in-memory ledger rows.
``test_simulated_setups_read_the_broker_exactly_like_the_real_controller`` runs the real
``ManagedExecution.manage`` on a disposable PostgreSQL ledger to show that the simulated
per-setup read pattern is the real controller's. Fixture evidence only.
"""

import copy
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.broker_budget import (
    ACCOUNT,
    PROTECTIVE,
    RECONCILIATION,
    RESEARCH,
    BrokerBudget,
    BrokerRateLimited,
    BudgetedBroker,
    BudgetPolicy,
    request_priority,
    retry_after_seconds,
)
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_account_safety import ManagedAccountSafety
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_latches import PROTECTION, REST
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.market import NY, Session
from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import ManagedVenue, admit_enter, observation, packet

CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
START = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)  # Monday 12:00 New York.
PAPER = "https://paper-api.alpaca.markets"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)


def noon_after(moment):
    """The next New York noon. Mid-session and later than the database's clock, so the
    review windows checked by SQL against clock_timestamp() stay valid."""
    local = moment.astimezone(NY)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if noon <= local:
        noon += timedelta(days=1)
    return noon.astimezone(UTC)


class CountingVenue(ManagedVenue):
    """ManagedVenue plus a timestamped request log and an optional HTTP 429 storm."""

    def __init__(self, now):
        super().__init__()
        self.now = now
        self.requests = []
        self.rate_limited_until = None
        self.retry_after = "2"

    def handle(self, request):
        self.requests.append((self.now, request.method, request.url.path))
        if (
            self.rate_limited_until is not None
            and self.now < self.rate_limited_until
            and request.url.host == "paper-api.alpaca.markets"
        ):
            return httpx.Response(
                429, headers={"Retry-After": self.retry_after}, json={"message": "fixture limit"}
            )
        return super().handle(request)

    def count(self, method="GET", path=None, since=None):
        return sum(
            m == method and (path is None or p == path) and (since is None or at >= since)
            for at, m, p in self.requests
        )


def peak_per_window(times, seconds=60):
    times, best, first = sorted(times), 0, 0
    for last, at in enumerate(times):
        while (at - times[first]).total_seconds() >= seconds:
            first += 1
        best = max(best, last - first + 1)
    return best


# --- Token bucket, Retry-After and the transport ---------------------------------


def exhaust(budget, priority):
    sent = 0
    while True:
        try:
            budget.bucket.acquire(priority)
        except BrokerRateLimited as refused:
            return sent, refused.code
        sent += 1


def test_each_priority_class_leaves_a_reserve_for_the_classes_above_it():
    clock = Clock(START)
    budget = BrokerBudget(clock=clock)
    assert [exhaust(budget, p) for p in (RESEARCH, RECONCILIATION, ACCOUNT, PROTECTIVE)] == [
        (90, "BROKER_BUDGET_EXHAUSTED"),
        (30, "BROKER_BUDGET_EXHAUSTED"),
        (15, "BROKER_BUDGET_EXHAUSTED"),
        (15, "BROKER_BUDGET_EXHAUSTED"),
    ]
    clock.advance(2)  # 150 per minute refills five tokens.
    assert exhaust(budget, ACCOUNT) == (0, "BROKER_BUDGET_EXHAUSTED")  # Minute ceiling reached.
    assert exhaust(budget, PROTECTIVE)[0] == 5  # Protective reads use broker headroom.
    status = budget.status()
    assert status["reads_sent"]["RESEARCH_AND_LIQUIDITY"] == 90
    assert status["reads_denied"]["ACCOUNT_SNAPSHOT"] == 2
    assert status["requests_last_minute"] == 155


def test_trailing_minute_ceiling_holds_even_while_tokens_refill():
    clock = Clock(START)
    budget = BrokerBudget(clock=clock)
    for _ in range(50):
        for _ in range(3):
            budget.bucket.acquire(PROTECTIVE)
        clock.advance(1)
    status = budget.status()
    assert status["requests_last_minute"] == 150 and status["tokens"] >= 120
    with pytest.raises(BrokerRateLimited, match="BROKER_BUDGET_EXHAUSTED"):
        budget.bucket.acquire(ACCOUNT)
    budget.bucket.acquire(PROTECTIVE)
    clock.advance(20)  # The first twenty seconds of reads leave the trailing minute.
    budget.bucket.acquire(ACCOUNT)


def test_retry_after_pauses_every_read_and_empties_the_bucket():
    clock = Clock(START)
    budget = BrokerBudget(clock=clock)
    budget.observe(httpx.Response(429, headers={"Retry-After": "3"}))
    for priority in (PROTECTIVE, ACCOUNT, RECONCILIATION, RESEARCH):
        with pytest.raises(BrokerRateLimited) as refused:
            budget.bucket.acquire(priority)
        assert refused.value.code == "BROKER_RETRY_AFTER"
    assert budget.cooldown_remaining() == 3
    clock.advance(3)
    budget.bucket.acquire(PROTECTIVE)  # 7.5 tokens refilled while waiting.
    with pytest.raises(BrokerRateLimited, match="BROKER_BUDGET_EXHAUSTED"):
        budget.bucket.acquire(RESEARCH)
    assert budget.status()["rate_limited_responses"] == 1


def test_retry_after_forms_are_bounded():
    now = START
    assert retry_after_seconds(httpx.Headers({"Retry-After": "4.5"}), now) == 4.5
    date = (now + timedelta(seconds=9)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert retry_after_seconds(httpx.Headers({"Retry-After": date}), now) == 9
    reset = str(int(now.timestamp()) + 7)
    assert retry_after_seconds(httpx.Headers({"X-RateLimit-Reset": reset}), now) == 7
    assert retry_after_seconds(httpx.Headers({"Retry-After": "soon"}), now) is None
    clock = Clock(now)
    for headers, expected in (({}, 5), ({"Retry-After": "86400"}, 60), ({"Retry-After": "-3"}, 0)):
        budget = BrokerBudget(clock=clock)
        budget.observe(httpx.Response(429, headers=headers))
        assert budget.cooldown_remaining() == expected


def test_mutations_are_never_refused_and_invalidate_the_shared_snapshot():
    clock = Clock(START)
    budget = BrokerBudget(clock=clock)
    delivered = []

    def broker(request):
        delivered.append((request.method, request.url.host))
        return httpx.Response(204 if request.method == "DELETE" else 200, json=[])

    transport = budget.transport(httpx.MockTransport(broker))
    exhaust(budget, PROTECTIVE)
    budget.observe(httpx.Response(429, headers={"Retry-After": "30"}))
    with pytest.raises(BrokerRateLimited):
        transport.handle_request(httpx.Request("GET", PAPER + "/v2/positions"))
    before = budget.status()
    response = transport.handle_request(httpx.Request("DELETE", PAPER + "/v2/orders/abc-1"))
    assert response.status_code == 204
    # Market data is another provider limit; it is neither budgeted nor paused.
    transport.handle_request(httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/bars"))
    assert delivered == [
        ("DELETE", "paper-api.alpaca.markets"),
        ("GET", "data.alpaca.markets"),
    ]
    after = budget.status()
    assert after["mutations_counted"] == before["mutations_counted"] + 1
    assert after["invalidations"] == before["invalidations"] + 1
    assert after["last_invalidation"] == "BROKER_MUTATION"


def test_budgeted_broker_is_a_read_side_proxy_only():
    budget = BrokerBudget(clock=Clock(START))
    broker = ManagedPaperBroker(
        CREDENTIALS, ReadOnlyFixtureGate(), transport=budget.transport(httpx.MockTransport(
            lambda request: httpx.Response(200, json=[])
        ))
    )
    try:
        proxy = budget.wrap(broker)
        assert proxy.mutate == broker.mutate and proxy.credentials is broker.credentials
        assert proxy.wrapped is broker and proxy.governor is budget
        with pytest.raises(ValueError):
            budget.wrap(proxy)
        with pytest.raises(ValueError):
            BudgetedBroker(broker, object())
    finally:
        broker.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"requests_per_minute": 201},
        {"burst_capacity": 151},
        {"protective_requests_per_minute": 140},
        {"protective_requests_per_minute": 201},
        {"snapshot_seconds": 0},
        {"activity_cache_seconds": 61},
        {"reserve_fractions": (0.1, 0.1, 0.2, 0.4)},
        {"reserve_fractions": (0.0, 0.3, 0.2, 0.4)},
        {"requests_per_minute": True},
    ],
)
def test_budget_policy_rejects_unsafe_settings(changes):
    with pytest.raises(ValueError, match="EXPLICIT_BROKER_BUDGET_POLICY_REQUIRED"):
        BudgetPolicy(**changes)


def test_request_priority_is_scoped_and_validated():
    from catalyst_lab.broker_budget import current_priority

    assert current_priority() == RESEARCH
    with request_priority(PROTECTIVE):
        assert current_priority() == PROTECTIVE
        with request_priority(ACCOUNT):
            assert current_priority() == ACCOUNT
        assert current_priority() == PROTECTIVE
    assert current_priority() == RESEARCH
    with pytest.raises(ValueError):
        with request_priority(7):
            pass


# --- Shared snapshot and read caches ----------------------------------------------


class Reader:
    """Direct broker reads with call counts for snapshot and cache unit tests."""

    def __init__(self):
        self.calls = Counter()
        self.during_positions = None

    def open_orders(self):
        self.calls["open_orders"] += 1
        return [{"id": "parent", "client_order_id": "cl-parent", "status": "new", "legs": [
            {"id": "leg-old", "client_order_id": "cl-leg", "status": "canceled"}
        ]}]

    def positions(self):
        self.calls["positions"] += 1
        if self.during_positions is not None:
            self.during_positions()
        return [{"symbol": "BTCUSD", "qty": "1"}]

    def account(self):
        self.calls["account"] += 1
        return {"equity": "10000", "status": "ACTIVE"}

    def capital_activities(self, session_date):
        self.calls["capital_activities"] += 1
        return [{"activity_type": "CSD", "net_amount": "0", "date": session_date.isoformat()}]

    def order(self, order_id):
        self.calls["order"] += 1
        status = "filled" if order_id.startswith("done") else "new"
        return {"id": order_id, "client_order_id": "cl-" + order_id, "status": status}

    def order_by_client_id(self, client_order_id):
        self.calls["order_by_client_id"] += 1
        return None

    def calendar(self, start, end):
        self.calls["calendar"] += 1
        return [Session(start, START, START + timedelta(hours=7))]

    def asset(self, symbol):
        self.calls["asset"] += 1
        return {"symbol": symbol, "price_increment": "0.01"}


def test_snapshot_is_shared_within_the_interval_and_refreshed_early_on_invalidation():
    clock, reader = Clock(START), Reader()
    budget = BrokerBudget(clock=clock)
    first = budget.snapshot(reader)
    for _ in range(4):
        clock.advance(1)
        assert budget.snapshot(reader) is first
    assert reader.calls == {"open_orders": 1, "positions": 1, "account": 1,
                            "capital_activities": 1}
    clock.advance(1)
    second = budget.snapshot(reader)
    assert second is not first and reader.calls["positions"] == 2
    assert reader.calls["capital_activities"] == 1  # Cached for 60 seconds.
    budget.invalidate("TRADE_UPDATE_FILL")
    assert budget.snapshot(reader) is not second and reader.calls["positions"] == 3
    budget.snapshot(reader, max_age=0)  # The entry-time fresh read.
    assert reader.calls["positions"] == 4
    # A fill that lands during the reads makes the snapshot re-read once.
    reader.during_positions = lambda: (budget.invalidate("TRADE_UPDATE_FILL"),
                                       setattr(reader, "during_positions", None))
    budget.invalidate("TRADE_UPDATE_FILL")
    budget.snapshot(reader)
    assert reader.calls["positions"] == 6
    assert budget.status()["snapshot_invalidated"] is False


def test_snapshot_rows_are_copies_and_index_nested_legs():
    clock, reader = Clock(START), Reader()
    budget = BrokerBudget(clock=clock)
    snapshot = budget.snapshot(reader)
    snapshot.position_rows()[0]["qty"] = "999"
    snapshot.account["equity"] = "0"
    assert snapshot.position_rows()[0]["qty"] == "1" and snapshot.account["equity"] == "10000"
    assert snapshot.order("leg-old")["status"] == "canceled"
    assert snapshot.order_by_client_id("cl-parent")["id"] == "parent"
    # The canceled nested leg is terminal: later reads by ID never reach the broker.
    assert budget.order(reader, "leg-old")["status"] == "canceled"
    assert reader.calls["order"] == 0


def test_snapshot_refreshes_when_the_new_york_date_changes():
    midnight = datetime(2026, 9, 22, 4, 0, tzinfo=UTC)  # 00:00 New York.
    clock, reader = Clock(midnight - timedelta(seconds=2)), Reader()
    budget = BrokerBudget(clock=clock)
    budget.snapshot(reader)
    clock.advance(3)
    budget.snapshot(reader)
    assert reader.calls["positions"] == 2 and reader.calls["capital_activities"] == 2


def test_terminal_orders_activities_assets_and_calendar_are_cached():
    clock, reader = Clock(START), Reader()
    budget = BrokerBudget(clock=clock)
    for _ in range(3):
        assert budget.order(reader, "done-1")["status"] == "filled"
    assert budget.order_by_client_id(reader, "cl-done-1")["id"] == "done-1"
    for _ in range(2):
        budget.order(reader, "open-1")
    assert reader.calls["order"] == 3 and reader.calls["order_by_client_id"] == 0
    day = START.astimezone(NY).date()
    budget.capital_activities(reader, day)
    clock.advance(59)
    budget.capital_activities(reader, day)
    assert reader.calls["capital_activities"] == 1
    clock.advance(1)
    budget.capital_activities(reader, day)
    assert reader.calls["capital_activities"] == 2
    budget.asset(reader, "BTC/USD")
    budget.asset(reader, "BTC/USD")
    clock.advance(60)
    budget.asset(reader, "BTC/USD")
    assert reader.calls["asset"] == 2
    today = clock().astimezone(NY).date()
    for _ in range(5):
        budget.calendar(reader, today, today)
    budget.calendar(reader, today - timedelta(days=20), today)  # Eligibility's range.
    assert reader.calls["calendar"] == 2
    clock.advance(24 * 3600)
    tomorrow = clock().astimezone(NY).date()
    budget.calendar(reader, tomorrow, tomorrow)
    budget.calendar(reader, tomorrow, tomorrow)
    assert reader.calls["calendar"] == 3


# --- Ten simulated minutes: real runtime, real broker view, in-memory ledger rows --


class ReadOnlyFixtureGate(AuthorizationGate):
    """The simulation never mutates: any claim is a test failure, never a broker call."""

    def __init__(self):
        pass

    def claim(self, request):
        raise SubmissionDisabled("SIMULATION_IS_READ_ONLY")


class LedgerRows:
    """Answers exactly the four durable-row queries of ManagedExecution._broker_view."""

    def __init__(self, execution):
        self.execution = execution

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        if "FROM lab.managed_risk_decisions" in sql:
            rows = self.execution.rows[params[0]]["decisions"]
        elif "kind='BROKER_ACK'" in sql:
            rows = [{"body": body} for body in self.execution.rows[params[0]]["acks"]]
        elif "kind='BROKER_REJECTED'" in sql or "FROM lab.managed_claims" in sql:
            rows = []
        else:
            raise AssertionError("UNEXPECTED_LEDGER_QUERY")
        return SimpleNamespace(
            fetchall=lambda: list(rows), fetchone=lambda: rows[0] if rows else None
        )


class SimulatedExecution(ManagedExecution):
    """The real read side (_broker_view, account_snapshot) over in-memory ledger rows.

    ``manage`` issues the broker reads of the real ``manage`` for each state; its ledger
    writes need PostgreSQL, and the equivalence test below checks this read pattern
    against the real controller on a disposable database.
    """

    def __init__(self, broker, clock):
        self.broker, self.now = broker, clock
        self.policy = engineering_execution_policy()
        self.reconciled_at = None
        self.rows = {}
        self.events = []
        self.repo = SimpleNamespace(connect=lambda: LedgerRows(self))
        self.store = SimpleNamespace(
            active=lambda: [{**row["setup"], "state": row["state"]} for row in self.rows.values()],
            transaction=lambda: nullcontext(None),
            event=lambda *args, **kwargs: None,
        )

    def _load(self, setup_id):
        return self.rows[setup_id]["setup"], self.rows[setup_id]["state"]

    def _event(self, kind, body, setup_id=None, key=None):
        self.events.append((kind, body))

    def replay_unmatched(self):
        return 0

    def reconcile(self):
        # The broker reads of the real reconcile, in its order.
        self.broker.positions()
        self.broker.open_orders()
        self.broker.account()
        self.reconciled_at = self.now()
        return {"clean": True}

    def manage(self, setup_id, observation=None):
        setup, state, *_ = self._broker_view(setup_id)
        now = self.now()
        if state["state"] == "WATCHING":
            if setup["market"] == "US_STOCKS":
                self.broker.calendar(now.astimezone(NY).date(), now.astimezone(NY).date())
            return state
        self.account_snapshot(shared=True)
        if setup["market"] == "CRYPTO":
            self.broker.asset(setup["symbol"])  # Crypto protection's price-grid metadata.
        return state

    def add(self, symbol, market, state, decisions=(), acks=()):
        setup_id = uuid4()
        self.rows[setup_id] = {
            "setup": {"setup_id": setup_id, "symbol": symbol, "market": market,
                      "record_json": {"levels": {"stop": "95"}}},
            "state": {"state": state, "revision": 1, "qty": "0" if state == "WATCHING" else "1"},
            "decisions": list(decisions),
            "acks": list(acks),
        }
        return setup_id


class SimulatedSafety:
    """ManagedAccountSafety.tick's broker read; its halt bookkeeping needs PostgreSQL."""

    def __init__(self, execution):
        self.execution = execution

    def tick(self):
        self.execution.account_snapshot(shared=True)

    def ingest(self, raw):
        return False


def order_row(symbol, side, kind, status, qty, **fields):
    return {
        "id": str(uuid4()), "client_order_id": uuid4().hex, "symbol": symbol, "side": side,
        "type": kind, "status": status, "qty": qty, "filled_qty": qty if status == "filled"
        else "0", "asset_class": "crypto" if "/" in symbol else "us_equity", **fields,
    }


def entered(venue, execution, symbol):
    """A filled entry and its protection, as ledger decision/ack rows plus broker orders."""
    crypto = "/" in symbol
    qty = "1" if crypto else "10"
    rows = []
    if crypto:
        entry = order_row(symbol, "buy", "limit", "filled", qty, legs=[])
        stop = order_row(symbol, "sell", "stop_limit", "new", qty, stop_price="95",
                         limit_price="94.99")
        rows = [("ENTRY", entry), ("PROTECT", stop)]
        venue.orders[stop["id"]] = stop
    else:
        legs = [order_row(symbol, "sell", "stop", "new", qty, stop_price="95"),
                order_row(symbol, "sell", "limit", "new", qty, limit_price="111")]
        entry = order_row(symbol, "buy", "limit", "filled", qty, order_class="bracket",
                          legs=legs)
        rows = [("ENTRY", entry)]
        for leg in legs:
            venue.orders[leg["id"]] = leg
    venue.orders[entry["id"]] = entry
    venue.inventory[symbol] = D(qty)
    decisions, acks = [], []
    for action, order in rows:
        decision = {"decision_id": uuid4(), "action": action,
                    "payload": {"client_order_id": order["client_order_id"]},
                    "expires_at": venue.now}
        decisions.append(decision)
        acks.append({"decision_id": str(decision["decision_id"]), "action": action,
                     "order": copy.deepcopy(order)})
    return execution.add(symbol, "CRYPTO" if crypto else "US_STOCKS", "OPEN", decisions, acks)


WATCHING = ("SPY", "QQQ", "IWM", "DIA", "TLT", "DOGE/USD", "LTC/USD", "BCH/USD", "UNI/USD",
            "AAVE/USD")
OPEN = ("AAPL", "MSFT", "NVDA", "AMZN", "META", "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD",
        "LINK/USD")


def simulated_account(venue, *, governed=True, watching=WATCHING, opened=OPEN, rows=None):
    budget = BrokerBudget(clock=lambda: venue.now) if governed else None
    transport = httpx.MockTransport(venue.handle)
    raw = ManagedPaperBroker(
        CREDENTIALS, ReadOnlyFixtureGate(),
        transport=budget.transport(transport) if budget else transport,
    )
    execution = SimulatedExecution(budget.wrap(raw) if budget else raw, lambda: venue.now)
    if rows is not None:
        execution.rows = rows
    else:
        for symbol in watching:
            execution.add(symbol, "CRYPTO" if "/" in symbol else "US_STOCKS", "WATCHING")
        for symbol in opened:
            entered(venue, execution, symbol)
    run = ManagedRuntime(
        execution, SimpleNamespace(), SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        CREDENTIALS, engineering_runtime_policy(), clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True, account_safety=SimulatedSafety(execution),
        broker_budget=budget,
    )
    run._selected_packets = lambda: []
    run._pending_trades = lambda: []
    return run, raw, budget


def simulate(run, venue, seconds, *, reconcile_seconds=30):
    for second in range(seconds):
        if second % reconcile_seconds == 0:
            assert run.reconcile_once()
        run.execution_once()
        venue.now += timedelta(seconds=1)


def test_ten_watching_and_ten_open_setups_stay_under_150_requests_per_minute():
    venue = CountingVenue(START)
    run, raw, budget = simulated_account(venue)
    try:
        simulate(run, venue, 600)
        times = [at for at, _, _ in venue.requests]
        per_minute = Counter(int((at - START).total_seconds() // 60) for at in times)
        peak = peak_per_window(times)
        paths = Counter(path for _, _, path in venue.requests)
        print(f"\nGOVERNED 10 WATCHING + 10 OPEN, 600 s: total={len(times)} "
              f"peak_60s={peak} per_minute={[per_minute[m] for m in range(10)]} "
              f"by_path={dict(paths)}")
        assert peak <= 150 and max(per_minute.values()) <= 150
        assert len(times) <= 10 * 60  # Steady state is far below the budget.
        assert paths["/v2/calendar"] == 1  # One trading calendar per New York date.
        status = budget.status()
        assert not any(status["reads_denied"].values()) and status["rate_limited_responses"] == 0
        assert run.error is None and not run.latches.blocking()
        assert run.execution.events == []  # No latch, failure or per-tick event at all.
    finally:
        raw.close()


def test_without_the_governor_the_same_setups_overrun_the_broker_limit():
    venue = CountingVenue(START)
    run, raw, _ = simulated_account(venue, governed=False)
    try:
        simulate(run, venue, 60)
        print(f"\nUNGOVERNED 10 WATCHING + 10 OPEN, 60 s: total={len(venue.requests)}")
        assert len(venue.requests) > 200 * 10  # The rate-limit defect this package removes.
    finally:
        raw.close()


# --- Disposable PostgreSQL: the real controller and runtime -----------------------


@pytest.fixture
def bx(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = CountingVenue(noon_after(datetime.now(UTC)))
    budget = BrokerBudget(clock=lambda: venue.now)
    broker = ManagedPaperBroker(
        CREDENTIALS,
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=budget.transport(httpx.MockTransport(venue.handle)),
    )
    engine = ManagedExecution(
        risk, budget.wrap(broker), policy=engineering_execution_policy(),
        clock=lambda: venue.now, review_store=reviews,
    )
    assert engine.reconcile()["clean"]
    yield engine, venue, reviews
    broker.close()


@pytest.fixture
def governed(bx):
    """The real runtime and account safety, both clients below one account budget."""
    engine, venue, _ = bx
    legacy = RiskAuthorizedPaperClient(
        CREDENTIALS,
        AuthorizationGate(engine.repo, clock=lambda: venue.now),
        transport=engine.broker.governor.transport(httpx.MockTransport(venue.handle)),
    )
    run = ManagedRuntime(
        engine, SimpleNamespace(), SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        CREDENTIALS, engineering_runtime_policy(), clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True, account_safety=ManagedAccountSafety(engine, legacy),
    )
    assert run.broker_budget is engine.broker.governor
    run._selected_packets = lambda: []
    yield run
    legacy.close()


def open_position(bx, symbol):
    engine, venue, _ = bx
    sid, _ = admit_enter(bx, symbol)
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == symbol)
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(bx))
    return sid


def make_ready(run, symbol="BTC/USD"):
    run.connected = run.research_healthy = True
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {symbol}
    assert run.reconcile_once()


def managed_bodies(engine, kind):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            "SELECT idempotency_key,body FROM lab.managed_events WHERE kind=%s "
            "ORDER BY event_seq", (kind,),
        ).fetchall()
    return [(row["idempotency_key"], row["body"]) for row in rows]


def durable_rows(engine, setup_ids):
    rows = {}
    with engine.repo.connect() as conn:
        for sid in setup_ids:
            rows[sid] = {
                "setup": dict(engine.store.setup(conn, sid)),
                "state": engine.store.state(conn, sid),
                "decisions": conn.execute(
                    """SELECT * FROM lab.managed_risk_decisions WHERE setup_id=%s
                    AND outcome='APPROVED' AND method='POST' ORDER BY event_seq""", (sid,),
                ).fetchall(),
                "acks": [row["body"] for row in conn.execute(
                    "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind='BROKER_ACK'",
                    (sid,),
                ).fetchall()],
            }
    return rows


def test_simulated_setups_read_the_broker_exactly_like_the_real_controller(bx, governed):
    engine, venue, _ = bx
    setups = [open_position(bx, "SPY"), open_position(bx, "BTC/USD"),
              engine.admit(packet(bx, "QQQ")), engine.admit(packet(bx, "ETH/USD"))]
    venue.now += timedelta(seconds=61)  # Every cached read expires, as after a restart.
    twin = CountingVenue(venue.now)
    twin.orders, twin.inventory = copy.deepcopy(venue.orders), dict(venue.inventory)
    twin.entry_prices = dict(venue.entry_prices)
    simulated, raw, _ = simulated_account(twin, rows=durable_rows(engine, setups))
    try:
        observed = {}
        for name, run, place in (("real", governed, venue), ("simulated", simulated, twin)):
            run.reconcile_once()
            run.execution_once()  # Warm-up: terminal entries, assets and activities cached.
            place.now += timedelta(seconds=1)
            place.requests.clear()
            simulate(run, place, 120)
            observed[name] = Counter((m, p) for _, m, p in place.requests)
        print(f"\nREAL vs SIMULATED reads over 120 s: {dict(observed['real'])}")
        assert observed["real"] == observed["simulated"]
        assert sum(observed["real"].values()) <= 2 * 60
        assert governed.error is None
        assert [state["state"] for state in (engine._load(s)[1] for s in setups)] == [
            "OPEN", "OPEN", "WATCHING", "WATCHING"
        ]
    finally:
        raw.close()


def test_trade_update_fill_refreshes_the_shared_snapshot_early(bx, governed):
    engine, venue, _ = bx
    admit_enter(bx, "BTC/USD")
    run = governed
    run.execution_once()
    reads = venue.count(path="/v2/positions")
    venue.now += timedelta(seconds=1)
    entry = venue.orders_of("buy")[0]
    update = venue.fill(entry["id"], entry["qty"])  # The broker fills; no stream message yet.
    run.execution_once()
    assert venue.count(path="/v2/positions") == reads  # Within the interval: one snapshot.
    assert not venue.orders_of("sell", "stop_limit")
    run.ingest(update)  # The trade-updates stream delivers the fill.
    assert engine.broker.governor.status()["last_invalidation"] == "TRADE_UPDATE_FILL"
    run.execution_once()  # Same second: the refreshed snapshot sees the position.
    assert venue.count(path="/v2/positions") == reads + 1
    assert venue.orders_of("sell", "stop_limit")  # Protection placed without waiting 5 s.
    assert verify_events(engine.repo.export_events())["valid"]


def test_calendar_is_read_once_per_new_york_date(bx, governed):
    engine, venue, _ = bx
    sid = engine.admit(packet(bx, "QQQ", expires_at=venue.now + timedelta(days=2)))
    run = governed
    since = venue.now
    for _ in range(20):
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert venue.count(path="/v2/calendar", since=since) == 1
    venue.now += timedelta(days=1)  # The next New York date.
    for _ in range(20):
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert venue.count(path="/v2/calendar", since=since) == 2
    assert engine._load(sid)[1]["state"] == "WATCHING"


def test_entry_authorization_still_reads_the_account_fresh(bx, governed):
    engine, venue, _ = bx
    sid = engine.admit(packet(bx, "BTC/USD"))
    governed.execution_once()  # The shared snapshot is now current.
    venue.now += timedelta(seconds=4)
    accounts = venue.count(path="/v2/account")
    decision = engine.observe_trigger(sid, observation(bx))
    assert decision["outcome"] == "APPROVED"
    assert venue.count(path="/v2/account") == accounts + 1  # Not the 4-second-old snapshot.


def test_rate_limit_storm_yields_rest_degraded_and_leaves_no_sticky_latch(bx, governed):
    engine, venue, _ = bx
    open_position(bx, "BTC/USD")
    run = governed
    make_ready(run)
    run.execution_once()
    assert run.ready()
    venue.now += timedelta(seconds=5)  # The shared snapshot is due for a refresh.
    storm = venue.now
    venue.rate_limited_until, venue.retry_after = storm + timedelta(seconds=12), "2"
    for _ in range(12):
        run.execution_once()
        assert run.error == REST and not run.ready()
        assert not run.latches.has(PROTECTION)
        venue.now += timedelta(seconds=1)
    sent = venue.count(since=storm)
    assert sent <= 7  # Retry-After honoured: about one probe per two seconds, not ~36.
    assert run.reconciled_at is not None  # A rate limit never invalidates reconciliation.
    # The 429 emptied the bucket: protective reads resume first, the account-snapshot
    # class once its reserve refills, and the latch clears K clean ticks after that.
    recovered = None
    for second in range(1, 31):
        run.execution_once()
        venue.now += timedelta(seconds=1)
        if run.error is None:
            recovered = second
            break
    k = run.latch_policy.clear_after_clean_ticks
    assert recovered is not None and k <= recovered <= k + 10
    assert not run.latches.blocking()
    assert run.reconcile_once() and run.ready()  # The reconcile loop's next pass.
    sets = managed_bodies(engine, "RUNTIME_LATCH_SET")
    clears = managed_bodies(engine, "RUNTIME_LATCH_CLEARED")
    assert [body["cause"] for _, body in sets] == [REST]
    assert [body["cause"] for _, body in clears] == [REST]
    assert sets[0][0] == f"runtime-latch:{run.runtime_id}:{REST}:RUNTIME:1:SET:REST"
    assert clears[0][1]["cleared_by"] == "K_CLEAN_TICKS_AFTER_RETRY_AFTER"
    assert verify_events(engine.repo.export_events())["valid"]


@pytest.mark.parametrize("pressure", ["RESEARCH_TRAFFIC", "EXHAUSTED_BUDGET"])
def test_protective_cancel_dispatches_within_five_seconds_under_budget_pressure(
    bx, governed, pressure
):
    engine, venue, _ = bx
    budget = engine.broker.governor
    sid = open_position(bx, "SPY")
    run = governed
    venue.now += timedelta(seconds=61)  # Setup reads leave the trailing minute.
    run.execution_once()
    venue.now += timedelta(seconds=6)  # The shared snapshot has expired.
    if pressure == "RESEARCH_TRAFFIC":
        assert exhaust(budget, RESEARCH)[0] > 0
    else:
        assert exhaust(budget, PROTECTIVE)[0] > 0
    t0 = venue.now
    with engine.store.transaction() as conn:
        current = engine.store.state(conn, sid)
        engine.store.transition(conn, sid, current["state"], hard_exit_at=t0.isoformat())
    for _ in range(6):
        exhaust(budget, RESEARCH)  # Research takes every read its class may take.
        run.execution_once()
        venue.now += timedelta(seconds=1)
    assert budget.status()["reads_denied"]["RESEARCH_AND_LIQUIDITY"] >= 6
    deletes = [at for at, method, _ in venue.requests if method == "DELETE"]
    assert deletes and (min(deletes) - t0).total_seconds() <= 5
    assert len(venue.orders_of("sell", "stop")) == 1
    assert all(o["status"] == "canceled" for o in venue.orders_of("sell") if o["type"] != "market")
    with engine.repo.connect() as conn:
        cancels = conn.execute(
            """SELECT count(*) AS n FROM lab.managed_risk_decisions d
            JOIN lab.managed_claims c USING(decision_id)
            WHERE d.action='CANCEL' AND d.method='DELETE'
            AND d.expires_at<=d.created_at+interval '5 seconds'"""
        ).fetchone()["n"]
    assert cancels == len(deletes)  # Every cancel crossed the exact one-use gate.
    assert verify_events(engine.repo.export_events())["valid"]
