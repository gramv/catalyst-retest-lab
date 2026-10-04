import asyncio
import json
import threading
from dataclasses import fields, replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab import crypto_maintenance
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_app import public_status
from catalyst_lab.managed_runtime import (
    CRYPTO_STREAM_SYMBOL_CAPACITY,
    ManagedRuntime,
    RuntimePolicy,
    build_runtime_from_env,
    engineering_runtime_policy,
)
from catalyst_lab.managed_service import STATUS_FIELDS
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from tests.test_complete_managed_cycle import external_muse_item
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_research_cycle import response
from tests.test_setup_scan import NOW


class Execution:
    def __init__(self):
        self.events, self.managed, self.triggered, self.admitted, self.updates = [], [], [], [], []
        self.reconciled_at = None
        self.clean = True
        self.setups = [
            {
                "setup_id": "setup-1",
                "symbol": "BTC/USD",
                "market": "CRYPTO",
                "state": {"state": "WATCHING"},
                "record_json": {"levels": {"stop": "95"}},
            }
        ]
        self.store = SimpleNamespace(active=lambda: self.setups)
        self.reconciliations = 0
        self.keyed = {}

    def _event(self, kind, body, setup_id=None, key=None):
        # Mirrors ManagedStore.event: a repeated key is a no-op unless its content differs.
        if key is not None:
            if key in self.keyed:
                if self.keyed[key] != (kind, body):
                    raise ValueError("IDEMPOTENCY_CONTENT_MISMATCH")
                return
            self.keyed[key] = (kind, body)
        self.events.append((kind, body))

    def reconcile(self):
        self.reconciliations += 1
        self.reconciled_at = NOW if self.clean else None
        return {"clean": self.clean}

    def admit(self, packet):
        self.admitted.append(packet)

    def manage(self, setup_id, observation):
        self.managed.append((setup_id, observation))

    def observe_trigger(self, setup_id, observation):
        if float(observation["trade_price"]) <= 95:
            self.revoke(setup_id, "STOP_TRADED_BEFORE_TRIGGER")
            return None
        self.triggered.append((setup_id, observation))
        return {"already_dispatched": True}

    def revoke(self, setup_id, reason):
        self.events.append(("REVOKE", {"setup_id": setup_id, "reason": reason}))
        for setup in self.setups:
            if setup["setup_id"] == setup_id:
                setup["state"]["state"] = "INVALIDATED"

    def ingest(self, event):
        self.updates.append(event)


class Source:
    def __init__(self):
        self.policy = SimpleNamespace(stock_feed="iex")
        self.data = {
            ("CRYPTO", "BTC/USD"): {
                "trade_price": "100",
                "trade_at": NOW.isoformat(),
                "quote_at": NOW.isoformat(),
                "bid": "99.99",
                "ask": "100.01",
                "feed_healthy": True,
            }
        }

    def current_observations(self, universe):
        assert universe.crypto_symbols == ("BTC/USD",)
        return self.data, ()


class Research:
    def __init__(self):
        self.ticks, self.published = [], []

    async def tick(self, cycle_id):
        self.ticks.append(cycle_id)

    def approved_packets(self, cycle_id):
        self.published.append(cycle_id)


def runtime(**overrides):
    result = ManagedRuntime(
        Execution(),
        Research(),
        Source(),
        AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret"),
        engineering_runtime_policy(),
        clock=lambda: NOW,
        reviewer_heartbeat=lambda: True,
        **overrides,
    )
    result._cycle_ids = lambda: ["durable-cycle"]
    result._selected_packets = lambda: []
    result.queued = []

    def append(setup, observation):
        if len(result.queued) >= result.policy.market_queue_capacity:
            raise ValueError("MARKET_QUEUE_OVERLOAD")
        result.queued.append(
            {
                "event_seq": len(result.queued) + 1,
                "setup_id": setup["setup_id"],
                "body": observation,
            }
        )

    result._append_trade = append
    result._pending_trades = lambda: list(result.queued)
    result._consume_trade = lambda row, reason: result.queued.remove(row)
    return result


def market_data(run, *, price="100", trade_id=1):
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"BTC/USD"}
    run.market_message(
        "CRYPTO", {"T": "q", "S": "BTC/USD", "bp": "99.99", "ap": "100.01", "t": NOW.isoformat()}
    )
    run.market_message(
        "CRYPTO", {"T": "t", "S": "BTC/USD", "p": price, "t": NOW.isoformat(), "i": trade_id}
    )


def ready(run):
    run.connected = True
    run.research_healthy = True
    run.reconcile_once()
    market_data(run)


def test_startup_stream_and_reconciliation_are_all_required_for_entries():
    run = runtime()
    market_data(run)
    run.execution_once()
    assert run.execution.managed and not run.execution.triggered
    assert not run.ready()
    run.reconcile_once()
    assert not run.ready()
    run.connected = True
    assert not run.ready()  # Runtime heartbeat is also required.
    run.heartbeat_once()
    assert run.ready()
    run.execution_once()
    assert len(run.execution.triggered) == 1


def test_status_exposes_real_reconciliation_time_and_clears_it_on_gap():
    run = runtime()
    assert run.status()["reconciled_at"] is None
    run.reconcile_once()
    assert run.status()["reconciled_at"] == NOW.isoformat()
    run.market_gap("CRYPTO", "TEST_GAP")
    assert run.status()["reconciled_at"] is None
    assert not hasattr(run.execution, "dispatch")  # Runtime must not dispatch twice.


def test_stream_loss_or_unclean_reconciliation_does_not_stop_protection():
    run = runtime()
    ready(run)
    run.connected = False
    run.execution_once()
    assert run.execution.managed and not run.execution.triggered
    run.connected = True
    run.execution.clean = False
    assert run.reconcile_once() is False
    run.execution_once()
    assert len(run.execution.managed) == 2
    assert not run.execution.triggered


def test_market_gap_removes_quotes_revokes_watching_and_preserves_protection():
    run = runtime()
    ready(run)
    assert run.observations and run.queued
    run.market_gap("CRYPTO", "FIXTURE_CONNECTION_LOST")
    assert run.observations == {} and not run.ready()
    run.execution_once()
    assert not run.execution.triggered
    assert run.execution.managed[-1][1] is None
    assert run.execution.setups[0]["state"]["state"] == "INVALIDATED"
    assert not run.queued


def test_selected_packets_admitted_only_when_ready_and_india_never_executed():
    run = runtime()
    run._selected_packets = lambda: [
        {"market": "CRYPTO", "symbol": "BTC/USD", "selection_event_seq": 1},
        {"market": "INDIA", "symbol": "RELIANCE", "selection_event_seq": 2},
    ]
    run.execution_once()
    assert not run.execution.admitted
    ready(run)
    run.execution_once()
    assert run.execution.admitted == [
        {"market": "CRYPTO", "symbol": "BTC/USD", "selection_event_seq": 1}
    ]


def test_research_outputs_are_durable_publication_not_direct_broker_actions():
    run = runtime()
    run.research_healthy = True
    asyncio.run(run.research_once())
    assert run.research.ticks == ["durable-cycle"]
    assert run.research.published == ["durable-cycle"]
    assert not run.execution.admitted and not run.execution.triggered


def test_slow_jev_does_not_block_protection_tick():
    run = runtime()
    ready(run)
    entered, release = threading.Event(), threading.Event()

    async def blocked(cycle_id):
        entered.set()
        await asyncio.to_thread(release.wait, 2)

    run.research.tick = blocked
    thread = threading.Thread(target=lambda: asyncio.run(run.research_once()))
    thread.start()
    try:
        assert entered.wait(1)
        run.execution_once()
        assert len(run.execution.managed) == 1
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_missing_monitor_callback_is_explicit_in_health():
    run = runtime()
    assert not run.status()["position_jev_configured"]
    assert run.status()["operation_claim"] == "SUPERVISED_PAPER_TEST"


def test_monitor_receives_open_position_context_and_fresh_supplier():
    seen = []

    async def monitor(setup, observation, fresh):
        seen.append((setup, observation, fresh()))

    run = runtime(monitor_tick=monitor)
    ready(run)
    run.execution.setups[0]["state"]["state"] = "OPEN"
    asyncio.run(run.research_once())
    assert len(seen) == 1
    assert seen[0][1] == seen[0][2]
    assert run.status()["position_jev_configured"]


def test_heartbeat_failure_blocks_new_entries_keeps_protection():
    run = runtime()
    ready(run)
    run.reviewer_heartbeat = lambda: False
    run.heartbeat_once()
    run.execution_once()
    assert run.execution.managed
    assert not run.execution.triggered
    assert not run.ready()


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


def test_primary_stream_auth_subscription_early_fill_and_reconcile():
    run = runtime()
    socket = Socket(
        [
            {"stream": "authorization", "data": {"status": "authorized"}},
            {"stream": "trade_updates", "data": {"event": "partial_fill"}},
            {"stream": "listening", "data": {"streams": ["trade_updates"]}},
            {"stream": "trade_updates", "data": {"event": "fill"}},
        ],
        run,
    )

    def connect(endpoint, **kwargs):
        assert "paper-api" in endpoint
        assert kwargs["proxy"] is None
        return socket

    run.connector = connect
    run.stream_session()
    assert socket.sent[0]["action"] == "auth"
    assert socket.sent[1]["data"]["streams"] == ["trade_updates"]
    assert run.execution.updates == [{"event": "partial_fill"}, {"event": "fill"}]
    assert run.execution.reconciliations == 1
    assert "test-only-secret" not in repr(run.execution.events)


def test_stream_not_ready_without_subscription_ack():
    run = runtime()
    socket = Socket(
        [
            {"stream": "authorization", "data": {"status": "authorized"}},
            {"stream": "listening", "data": {"streams": []}},
        ],
        run,
    )
    run.connector = lambda *_, **__: socket
    with pytest.raises(ValueError, match="TRADE_STREAM_SUBSCRIPTION_FAILED"):
        run.stream_session()
    assert not run.connected and not run.ready()


def test_explicit_env_required_before_credentials_or_database(monkeypatch):
    monkeypatch.delenv("MANAGED_ENVIRONMENT", raising=False)
    with pytest.raises(ValueError, match="REQUIRED_MANAGED_CONFIGURATION"):
        build_runtime_from_env()


def test_runtime_policy_has_no_silent_constructor_defaults():
    from dataclasses import MISSING

    assert all(f.default is MISSING and f.default_factory is MISSING for f in fields(RuntimePolicy))
    with pytest.raises(TypeError):
        RuntimePolicy()
    with pytest.raises(ValueError, match="EXPLICIT_MANAGED_RUNTIME"):
        replace(engineering_runtime_policy(), execution_tick_seconds=10)


def configure_env(monkeypatch):
    from dataclasses import asdict

    from catalyst_lab.managed_runtime import engineering_monitor_policy
    from catalyst_lab.review_config import APPROVED_GATE1

    values = {
        "MANAGED_ENVIRONMENT": "local_test",
        "MANAGED_RUNTIME_POLICY_JSON": json.dumps(asdict(engineering_runtime_policy())),
        "MANAGED_SOURCE_POLICY_JSON": json.dumps(
            {
                "stock_feed": "iex",
                "timeout_seconds": 3,
                "batch_size": 50,
                "page_size": 10000,
                "max_pages": 10,
                "lookback_padding_bars": 5,
            }
        ),
        "MANAGED_CYCLE_POLICY_JSON": json.dumps(
            {
                "selection_limit": 10,
                "review_deadline_seconds": 10,
                "claim_lease_seconds": 15,
                "max_packet_age_seconds": 60,
                "max_inflight": 5,
            }
        ),
        "MANAGED_POSITION_POLICY_JSON": json.dumps(asdict(engineering_monitor_policy())),
        "MANAGED_POSITION_REVIEW_SECONDS": "10",
        "JEV_REVIEW_POLICY_JSON": json.dumps(APPROVED_GATE1),
        "JEV_WORKER_DATABASE_URL": "fixture-jev-db",
        "JEV_CREDENTIAL_SLOT": "fixture",
        "JEV_WORKER_MAX_INFLIGHT": "5",
        "JEV_WORKER_POLL_SECONDS": "1",
        "MANAGED_DATABASE_URL": "fixture-risk-db",
        "MANAGED_RISK_POLICY_ID": "JEV_MANAGED_RISK_V2",
        "MANAGED_MANAGEMENT_REVIEWS": "ENABLED",
        # Required with reviews ENABLED from package jev-budget (the deploy example's budget).
        "JEV_MONTHLY_BUDGET_USD": "50",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_factory_fails_on_missing_broker_credentials_without_fallback(monkeypatch):
    configure_env(monkeypatch)
    for key in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="credentials are required"):
        build_runtime_from_env()


def test_factory_wires_actual_second_jev_role_with_separate_market_source(monkeypatch):
    import catalyst_lab.authorization as auth
    import catalyst_lab.managed_broker as broker_module
    import catalyst_lab.managed_execution as execution_module
    import catalyst_lab.managed_store as store_module
    import catalyst_lab.research_cycle as cycles
    import catalyst_lab.review_worker as workers
    import catalyst_lab.scan_sources as sources
    from catalyst_lab.managed_runtime import PositionMonitorLoop
    from catalyst_lab.position_monitor import PositionMonitor

    configure_env(monkeypatch)
    monkeypatch.setattr(
        AlpacaCredentials,
        "from_env",
        lambda: AlpacaCredentials("PKFACTORYFIXTURE", "fixture-secret-only"),
    )
    from contextlib import nullcontext

    import catalyst_lab.risk as risk_module
    from catalyst_lab.account_risk import AccountRiskPolicy

    fake_repo = SimpleNamespace(check_role=lambda: None, require_same_database=lambda _: None,
                                connect=lambda: nullcontext(None))

    def risk_repository(_, **options):
        fake_repo.options = options
        return fake_repo

    monkeypatch.setattr(auth, "RiskRepository", risk_repository)
    # The legacy safety engine checks its archived policy row at construction (migration 016).
    frozen = AccountRiskPolicy("CATALYST_RETEST_V1", "FROZEN_V1", D("0.01"), D("0.02"),
                               {"US_STOCKS": D("0.02")}, 1, 1, ("US_STOCKS",), False, D(1),
                               None, 0, "LAB_FIXTURE")
    monkeypatch.setattr(risk_module, "load_policy", lambda *_, **__: frozen)
    fake_review = SimpleNamespace(
        store=object(), reviewer=SimpleNamespace(store=object()), heartbeat=lambda: True,
        runtime=SimpleNamespace(),
    )
    monkeypatch.setattr(workers, "ReviewWorker", lambda _: fake_review)
    monkeypatch.setattr(store_module, "ManagedAuthorizationGate",
                        lambda repo: auth.AuthorizationGate(repo))
    fake_broker = object()
    monkeypatch.setattr(broker_module, "ManagedPaperBroker", lambda *_, **__: fake_broker)
    execution = Execution()
    execution.repo = fake_repo
    execution.now = lambda: NOW
    monkeypatch.setattr(execution_module, "ManagedExecution", lambda *_, **__: execution)
    monkeypatch.setattr(cycles, "ResearchCycle", lambda *_, **__: Research())
    built_sources = []

    def new_source(*args):
        source = Source()
        built_sources.append(source)
        return source

    monkeypatch.setattr(sources, "AlpacaMarketSource", new_source)
    run = build_runtime_from_env()
    assert isinstance(run.position_monitor, PositionMonitor)
    assert isinstance(run.monitor_tick, PositionMonitorLoop)
    assert run.status()["position_jev_configured"]
    assert run.gate1 is fake_review.runtime  # package jev-breaker: wired from worker.runtime.
    assert len(built_sources) == 2
    assert run.source is not run.monitor_tick.source
    assert fake_repo.options == {"reuse_connections": True}  # Package pass-speed.


def test_monitor_loop_passes_actual_completed_bars_and_refreshed_quote():
    from catalyst_lab.managed_runtime import PositionMonitorLoop, engineering_monitor_policy
    from catalyst_lab.setup_scan import engineering_scan_policy

    execution = Execution()
    # Real controller event accepts optional setup ID.
    execution._event = lambda *args: execution.events.append(args)
    received = []

    async def review(setup_id, observation, bars, *, fresh_observation, structural_bars):
        assert structural_bars == bars
        received.append((setup_id, observation, bars, fresh_observation()))

    monitor = SimpleNamespace(
        execution=execution, policy=engineering_monitor_policy(), review=review
    )
    source = SimpleNamespace(completed_bars=lambda *args: ((), ()))
    loop = PositionMonitorLoop(monitor, source, engineering_scan_policy(), clock=lambda: NOW)
    fresh = {"bid": "101", "ask": "101.01"}
    asyncio.run(loop(execution.setups[0], {"bid": "old"}, lambda: fresh))
    assert received == [("setup-1", fresh, (), fresh)]


def test_market_stream_auth_exact_subscription_and_all_prints_are_queued():
    run = runtime()
    socket = Socket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
            [
                {"T": "q", "S": "BTC/USD", "bp": 99.99, "ap": 100.01, "t": NOW.isoformat()},
                {"T": "t", "S": "BTC/USD", "p": 94, "i": 1, "t": NOW.isoformat()},
                {"T": "t", "S": "BTC/USD", "p": 100, "i": 2, "t": NOW.isoformat()},
            ],
        ],
        run,
    )

    def connect(endpoint, **kwargs):
        assert endpoint == "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
        assert kwargs["proxy"] is None
        return socket

    run.connector = connect
    run.market_stream_session("CRYPTO")
    assert socket.sent[0]["action"] == "auth"
    assert socket.sent[1] == {"action": "subscribe", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}
    assert [row["body"]["trade_price"] for row in run.queued] == ["94", "100"]
    assert run.observations[("CRYPTO", "BTC/USD")]["trade_price"] == "100"
    run.connected = run.research_healthy = True
    run.execution_once()
    assert run.execution.setups[0]["state"]["state"] == "INVALIDATED"
    assert not run.execution.triggered  # Later recovery tick cannot erase the preceding stop print.
    assert "test-only-secret" not in repr(run.execution.events)


def test_market_subscription_mismatch_never_permits_symbol():
    run = runtime()
    socket = Socket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": []}],
        ],
        run,
    )
    run.connector = lambda *_, **__: socket
    with pytest.raises(ValueError, match="MARKET_STREAM_SUBSCRIPTION_MISMATCH"):
        run.market_stream_session("CRYPTO")
    assert not run._market_ready("CRYPTO", "BTC/USD")


def test_market_queue_overflow_disconnects_and_blocks_entries():
    run = runtime()
    run.policy = replace(run.policy, market_queue_capacity=1)
    run.connector = lambda *_, **__: Socket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
            [
                {"T": "q", "S": "BTC/USD", "bp": "99.99", "ap": "100.01", "t": NOW.isoformat()},
                {"T": "t", "S": "BTC/USD", "p": "100", "i": 1, "t": NOW.isoformat()},
                {"T": "t", "S": "BTC/USD", "p": "100", "i": 2, "t": NOW.isoformat()},
            ],
        ],
        run,
    )
    original_gap = run.market_gap

    def stop_on_gap(market, reason, **details):
        original_gap(market, reason, **details)
        run.stop_event.set()

    run.market_gap = stop_on_gap
    run._market_stream_loop("CRYPTO")
    assert not run.market_connected["CRYPTO"] and "CRYPTO" in run.market_gaps
    assert market_gaps(run) == [{"market": "CRYPTO", "reason": "MARKET_QUEUE_OVERLOAD",
                                 "code": "MARKET_QUEUE_OVERLOAD"}]
    run.execution_once()
    assert run.execution.setups[0]["state"]["state"] == "INVALIDATED"
    assert not run.execution.triggered


def market_gaps(run):
    return [{k: v for k, v in body.items() if k != "runtime_id"}
            for kind, body in run.execution.events if kind == "RUNTIME_MARKET_GAP"]


class TextSocket(Socket):
    """``Socket`` whose text frames arrive as they are, so a frame can be malformed."""

    def recv(self, timeout):
        if self.frames and isinstance(self.frames[0], str):
            return self.frames.pop(0)
        return super().recv(timeout)


@pytest.mark.parametrize("frame,code", [
    ("[{not json", "JSONDecodeError"),
    ({"T": "t", "S": "BTC/USD", "p": "100", "i": 1}, "INVALID_MARKET_STREAM_FRAME"),
    ([{"T": "t", "S": "BTC/USD", "p": "100", "i": 1}], "KeyError"),
    ([{"T": "q", "S": "BTC/USD", "bp": "bid", "ap": "100.01", "t": NOW.isoformat()}],
     "InvalidOperation"),
    ([{"T": "q", "S": "BTC/USD", "bp": "99.99", "ap": "100.01",
       "t": (NOW + timedelta(minutes=1)).isoformat()}], "INVALID_MARKET_TIMESTAMP"),
])
def test_a_malformed_market_message_ends_the_session_and_its_gap_records_the_cause(frame, code):
    """Fail-closed as before: one malformed message ends the stream session, the market gap
    blocks entries and the WATCHING setup is invalidated. From 2026-09-29 the gap event also
    carries the sanitized cause (``failure_code``: the exception's own code, else its class
    name, never message text); until then it recorded only MARKET_STREAM_DISCONNECTED_OR_GAP."""
    run = runtime()
    run.connector = lambda *_, **__: TextSocket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
            frame,
            [{"T": "t", "S": "BTC/USD", "p": "90", "i": 2, "t": NOW.isoformat()}],
        ],
        run,
    )
    original_gap = run.market_gap

    def stop_on_gap(market, reason, **details):
        original_gap(market, reason, **details)
        run.stop_event.set()

    run.market_gap = stop_on_gap
    run._market_stream_loop("CRYPTO")
    assert market_gaps(run) == [{"market": "CRYPTO",
                                 "reason": "MARKET_STREAM_DISCONNECTED_OR_GAP", "code": code}]
    assert not run.market_connected["CRYPTO"] and "CRYPTO" in run.market_gaps
    assert not run.queued  # The session ended at the malformed message: no later print.
    run.connected = run.research_healthy = True
    run.execution_once()
    assert run.execution.setups[0]["state"]["state"] == "INVALIDATED"
    assert not run.execution.triggered and "test-only-secret" not in repr(run.execution.events)


@pytest.mark.parametrize("error,code", [
    ({"T": "error", "code": 405, "msg": "provider-text-not-kept"}, "ALPACA_STREAM_405"),
    ({"T": "error", "code": 406, "msg": "provider-text-not-kept"}, "ALPACA_STREAM_406"),
    ({"T": "error", "msg": "provider-text-not-kept"}, "ALPACA_STREAM_ERROR"),
    ({"T": "error", "code": "405", "msg": "provider-text-not-kept"}, "ALPACA_STREAM_ERROR"),
])
def test_a_provider_error_records_the_providers_numeric_code_and_never_its_text(error, code):
    """2026-09-29: the crypto stream failed every 30 s with MARKET_STREAM_PROVIDER_ERROR after
    new picks widened its subscription, and the gap recorded nothing that could tell a symbol
    limit from a connection limit. The session still ends fail-closed with the same reason; the
    gap's code is now the provider's number, and the provider's message text is never kept."""
    run = runtime()
    run.connector = lambda *_, **__: Socket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
            [error],
        ],
        run,
    )
    original_gap = run.market_gap

    def stop_on_gap(market, reason, **details):
        original_gap(market, reason, **details)
        run.stop_event.set()

    run.market_gap = stop_on_gap
    run._market_stream_loop("CRYPTO")
    assert market_gaps(run) == [{"market": "CRYPTO", "reason": "MARKET_STREAM_PROVIDER_ERROR",
                                 "code": code}]
    assert "provider-text-not-kept" not in repr(run.execution.events)
    assert not run.market_connected["CRYPTO"] and "CRYPTO" in run.market_gaps


def test_a_session_that_ends_without_an_error_records_no_cause():
    run = runtime()
    run.connector = lambda *_, **__: Socket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
        ],
        run,
    )
    run._market_stream_loop("CRYPTO")  # The socket stops the runtime once its frames run out.
    assert market_gaps(run) == [{"market": "CRYPTO",
                                 "reason": "MARKET_STREAM_DISCONNECTED_OR_GAP"}]


# CRYPTO_STREAM_CAPACITY_V1 (2026-09-29): Alpaca serves 15 coins (30 trade and quote channels) on
# one crypto stream connection. New picks widened the subscription from 13 to 19 coins, the
# provider refused it (405) and every refusal ended the session for all 13 coins.
COINS = tuple(f"C{n:02d}/USD" for n in range(1, 16))
CAPACITY_NOTICE = "CRYPTO_STREAM_CAPACITY_WAIT"


def crypto_setup(symbol, *, maintained=False):
    """An OPEN crypto setup of the fake execution; ``maintained`` records the admitted
    maintenance policy, so the crypto stream also wants Bitcoin."""
    state = {"state": "OPEN"}
    if maintained:
        state["maintenance_policy"] = crypto_maintenance.ADMITTED_MAINTENANCE.record()
    return {"setup_id": f"setup-{symbol}", "symbol": symbol, "market": "CRYPTO", "state": state,
            "record_json": {"levels": {"stop": "95"}}}


def offered(symbol, seq, market="CRYPTO"):
    return {"market": market, "symbol": symbol, "selection_event_seq": seq}


def full_stream(run):
    """14 active crypto setups, the first maintained: with Bitcoin, 15 coins."""
    run.execution.setups = [crypto_setup(c, maintained=c == COINS[0]) for c in COINS[:14]]


def test_a_full_crypto_stream_holds_back_every_offered_pick():
    """With 14 active setups and Bitcoin wanted the plan is exactly those 15 coins: every offered
    pick is held back, in selection order and once per coin. The caller's read is used."""
    run = runtime()
    full_stream(run)
    packets = [offered("NEW/USD", 41), offered("OTHER/USD", 42), offered("NEW/USD", 43)]
    run._selected_packets = lambda: packets
    wanted, held_back = run._stream_plan("CRYPTO")
    assert wanted == {*COINS[:14], "BTC/USD"} and len(wanted) == CRYPTO_STREAM_SYMBOL_CAPACITY
    assert held_back == ["NEW/USD", "OTHER/USD"]
    assert run._desired_symbols("CRYPTO") == wanted
    run._selected_packets = lambda: pytest.fail("the plan reads the ledger again")
    assert run._stream_plan("CRYPTO", packets[1:2]) == (wanted, ["OTHER/USD"])


def test_without_bitcoin_one_slot_is_kept_for_it():
    """While no maintained setup is active the plan stops at 14 coins. Admitting a maintained
    pick adds Bitcoin into the kept slot without displacing a coin; a Bitcoin pick takes it."""
    run = runtime()
    run.execution.setups = [crypto_setup(c) for c in COINS[:10]]
    picks = [offered(f"P{n}/USD", 40 + n) for n in range(1, 7)]
    run._selected_packets = lambda: picks
    wanted, held_back = run._stream_plan("CRYPTO")
    assert wanted == {*COINS[:10], "P1/USD", "P2/USD", "P3/USD", "P4/USD"}
    assert held_back == ["P5/USD", "P6/USD"]
    run.execution.setups.append(crypto_setup("P2/USD", maintained=True))  # Admitted.
    run._selected_packets = lambda: [p for p in picks if p["symbol"] != "P2/USD"]
    assert run._stream_plan("CRYPTO") == (wanted | {"BTC/USD"}, ["P5/USD", "P6/USD"])
    run.execution.setups = [crypto_setup(c) for c in COINS[:10]]
    run._selected_packets = lambda: [*picks, offered("BTC/USD", 47)]
    assert run._stream_plan("CRYPTO") == (wanted | {"BTC/USD"}, ["P5/USD", "P6/USD"])


def test_an_offered_pick_for_an_active_coin_takes_no_second_slot():
    run = runtime()
    run.execution.setups = [crypto_setup(c) for c in COINS[:12]]
    run._selected_packets = lambda: [
        offered(COINS[3], 41), offered("NEW/USD", 42), offered(COINS[7], 43),
        offered("OTHER/USD", 44), offered("LAST/USD", 45),
    ]
    wanted, held_back = run._stream_plan("CRYPTO")
    assert wanted == {*COINS[:12], "NEW/USD", "OTHER/USD"}  # 14, one slot kept for Bitcoin.
    assert held_back == ["LAST/USD"]


def test_a_held_back_pick_waits_with_one_notice_and_is_admitted_once_a_slot_frees_up():
    """Admission skips a held-back pick, even with its coin acknowledged, and records one
    CRYPTO_STREAM_CAPACITY_WAIT per selection (one ledger write however long it waits). Once a
    trade closes, the next tick admits it as usual."""
    run = runtime()
    full_stream(run)
    ready(run)
    packet = offered("NEW/USD", 41)
    run._selected_packets = lambda: [packet]
    run.market_subscriptions["CRYPTO"] = {*COINS[:14], "BTC/USD", "NEW/USD"}
    writes, write = [], run.execution._event

    def counted(kind, body, setup_id=None, key=None):
        writes.append(kind)
        return write(kind, body, setup_id=setup_id, key=key)

    run.execution._event = counted
    for _ in range(3):
        run.execution_once()
    notice = {"version": "CRYPTO_STREAM_CAPACITY_V1", "symbol": "NEW/USD", "capacity": 15,
              "selection_event_seq": 41}
    assert run.execution.admitted == [] and run.ready()
    assert [body for kind, body in run.execution.events if kind == CAPACITY_NOTICE] == [notice]
    assert run.execution.keyed["stream-capacity:41"] == (CAPACITY_NOTICE, notice)
    assert writes.count(CAPACITY_NOTICE) == 1
    assert run.status()["crypto_stream"]["held_back"] == ["NEW/USD"]
    run.execution.setups = [s for s in run.execution.setups if s["symbol"] != COINS[5]]  # Closed.
    run.execution_once()
    assert run.execution.admitted == [packet] and writes.count(CAPACITY_NOTICE) == 1
    assert run.status()["crypto_stream"]["held_back"] == [] and run.error is None


def test_the_us_stream_plan_and_admission_are_unchanged():
    """Stocks keep every active setup's and offered pick's symbol, however many, and no stock
    pick waits, while the crypto stream is full."""
    run = runtime()
    full_stream(run)
    tickers = [f"TK{n:02d}" for n in range(1, 21)]
    run.execution.setups += [
        {"setup_id": f"setup-{t}", "symbol": t, "market": "US_STOCKS",
         "state": {"state": "OPEN"}, "record_json": {"levels": {"stop": "95"}}}
        for t in tickers[:10]
    ]
    packets = [offered(t, 40 + n, market="US_STOCKS") for n, t in enumerate(tickers[8:])]
    run._selected_packets = lambda: packets
    assert run._stream_plan("US") == (set(tickers), [])
    assert run._desired_symbols("US") == set(tickers)
    ready(run)
    run.market_connected["US"], run.market_subscriptions["US"] = True, set(tickers)
    run.execution_once()
    assert run.execution.admitted == packets
    assert not any(kind == CAPACITY_NOTICE for kind, _ in run.execution.events)


class CappedStream(Socket):
    """Alpaca's crypto stream and its limit, 15 coins (30 trade and quote channels): a subscribe
    beyond it is answered with error 405 and changes nothing; every other request is
    acknowledged with the whole subscription. Each idle read runs the next of ``steps`` (the
    plan changes); once they are done the session ends."""

    LIMIT = 15

    def __init__(self, run, steps):
        super().__init__([[{"T": "success", "msg": "connected"}]], run)
        self.steps, self.coins, self.largest = list(steps), set(), 0

    def send(self, message):
        super().send(message)
        request = self.sent[-1]
        if request["action"] == "auth":
            self.frames.append([{"T": "success", "msg": "authenticated"}])
            return
        coins = set(request["trades"])
        after = self.coins | coins if request["action"] == "subscribe" else self.coins - coins
        if len(after) > self.LIMIT:
            self.frames.append([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])
            return
        self.coins, self.largest = after, max(self.largest, len(after))
        self.frames.append([{"T": "subscription", "trades": sorted(after),
                             "quotes": sorted(after)}])

    def recv(self, timeout):
        if not self.frames and self.steps:
            self.steps.pop(0)()
            raise TimeoutError
        return super().recv(timeout)


def test_the_market_stream_buffers_1024_frames():
    """2026-09-30: with 64 frames a reader that fell behind stopped reading the socket, the
    keepalive's pong went unread and the crypto stream closed every 45-100 s; it now buffers
    as many frames as the Coinbase feed."""
    from catalyst_lab.managed_runtime import MARKET_STREAM_MAX_QUEUE
    run = runtime()
    seen = {}

    def connect(endpoint, **kwargs):
        seen.update(kwargs)
        return Socket([], run)

    run.connector = connect
    with pytest.raises(TimeoutError):
        run.market_stream_session("CRYPTO")  # The fake socket has nothing to say.
    assert seen["max_queue"] == MARKET_STREAM_MAX_QUEUE == 1024
    assert seen["ping_interval"] == 20 and seen["ping_timeout"] == 20


class _Close:
    def __init__(self, code):
        self.code = code


class _Closed(Exception):
    """websockets' ConnectionClosed shape: the received and sent close frames."""

    def __init__(self, rcvd, sent):
        super().__init__("sent 1011 (internal error) keepalive ping timeout")
        self.rcvd, self.sent = rcvd, sent


@pytest.mark.parametrize("rcvd,sent,code", [
    (None, _Close(1011), "WS_CLOSED_NONE_1011"),
    (_Close(1008), _Close(1008), "WS_CLOSED_1008_1008"),
    (_Close(1006), None, "WS_CLOSED_1006_NONE"),
])
def test_a_closed_market_stream_records_its_close_codes(rcvd, sent, code):
    """A closed connection's gap names who closed it and why (the close frames' codes), never
    the exception's text; it recorded only ConnectionClosedError before."""
    run = runtime()

    class Closing(Socket):
        def recv(self, timeout):
            if not self.frames:
                raise _Closed(rcvd, sent)
            return super().recv(timeout)

    run.connector = lambda *_, **__: Closing(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
        ],
        run,
    )
    original_gap = run.market_gap

    def stop_on_gap(market, reason, **details):
        original_gap(market, reason, **details)
        run.stop_event.set()

    run.market_gap = stop_on_gap
    run._market_stream_loop("CRYPTO")
    assert market_gaps(run) == [{"market": "CRYPTO",
                                 "reason": "MARKET_STREAM_DISCONNECTED_OR_GAP", "code": code}]
    assert "keepalive" not in repr(run.execution.events)


def test_a_busy_market_stream_reads_its_plan_once_a_poll_interval_not_every_frame():
    """2026-09-30: the session read its plan (two ledger reads) after every frame, which capped
    how fast it read; around US market events the provider cut the lagging connection every
    2-5 minutes. 200 back-to-back quote frames now cost one plan read (the poll interval is
    1 s), and an idle read still re-reads it at once."""
    run = runtime()
    calls = []

    def plan(market):
        calls.append(market)
        return {"BTC/USD"}

    run._desired_symbols = plan
    quotes = [[{"T": "q", "S": "BTC/USD", "bp": "99.99", "ap": "100.01", "t": NOW.isoformat()}]
              for _ in range(200)]
    socket = Socket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
            *quotes,
        ],
        run,
    )
    run.connector = lambda *_, **__: socket
    run.market_stream_session("CRYPTO")  # Ends when the frames run out (an idle read).
    assert run.policy.market_poll_seconds == 1
    assert len(calls) <= 2  # The first read, and at most one more if a second passed.
    assert run.market_subscriptions["CRYPTO"] == {"BTC/USD"}


class LaggingStream(Socket):
    """A stream whose acknowledgments arrive late: each request's answer (the whole
    subscription after it) is held until the session reads with no plan change left to run.
    ``steps`` change the plan, one per idle read, as a research update does within seconds."""

    def __init__(self, run, steps):
        super().__init__([[{"T": "success", "msg": "connected"}]], run)
        self.steps, self.coins, self.answers = list(steps), set(), []

    def send(self, message):
        super().send(message)
        request = self.sent[-1]
        if request["action"] == "auth":
            self.frames.append([{"T": "success", "msg": "authenticated"}])
            return
        coins = set(request["trades"])
        self.coins = self.coins | coins if request["action"] == "subscribe" else self.coins - coins
        self.answers.append([{"T": "subscription", "trades": sorted(self.coins),
                              "quotes": sorted(self.coins)}])

    def recv(self, timeout):
        if not self.frames and self.steps:
            self.steps.pop(0)()
            raise TimeoutError
        if not self.frames and self.answers:
            self.frames.append(self.answers.pop(0))
        return super().recv(timeout)


def test_a_plan_change_waits_for_the_previous_subscription_to_be_acknowledged():
    """2026-09-30 01:11 UTC: a research update retired, re-offered and admitted coins within
    seconds; a second change was sent before the first was acknowledged, the first's late
    answer listed a coin no longer requested, and MARKET_STREAM_SUBSCRIPTION_MISMATCH ended
    the session twice. Now the second change waits for the first acknowledgment, the session
    stays up, and both subscriptions are recorded in order (fails on the old code)."""
    run = runtime()
    plan = [{"AAA/USD", "BBB/USD", "NEW/USD"}]
    run._desired_symbols = lambda market: set(plan[0])

    def update_swaps_a_coin():
        plan[0] = {"AAA/USD", "BBB/USD", "NEXT/USD"}

    socket = LaggingStream(run, [update_swaps_a_coin])
    run.connector = lambda *_, **__: socket
    run.market_stream_session("CRYPTO")  # A mismatch would end it with an exception.
    first, second = ["AAA/USD", "BBB/USD", "NEW/USD"], ["AAA/USD", "BBB/USD", "NEXT/USD"]
    assert socket.sent[1:] == [
        {"action": "subscribe", "trades": first, "quotes": first},
        {"action": "unsubscribe", "trades": ["NEW/USD"], "quotes": ["NEW/USD"]},
        {"action": "subscribe", "trades": ["NEXT/USD"], "quotes": ["NEXT/USD"]},
    ]
    assert run.market_subscriptions["CRYPTO"] == set(second)
    assert [body["symbols"] for kind, body in run.execution.events
            if kind == "RUNTIME_MARKET_CONNECTED"] == [first, second]


def test_the_crypto_stream_session_never_asks_for_more_than_15_coins():
    """Against a stream that refuses a 16th coin as Alpaca does, the first subscribe asks for
    the plan's 15 coins; when a trade closes, its coin is unsubscribed before the held-back pick
    is subscribed, so no request exceeds 15 and the session stays up. Before this version the
    first request asked for 17 coins, and a swap subscribed first."""
    run = runtime()
    run.execution.setups = [crypto_setup(c, maintained=c == COINS[0]) for c in COINS[:13]]
    picks = [offered("NEW/USD", 41), offered("NEXT/USD", 42), offered("LAST/USD", 43)]
    run._selected_packets = lambda: list(picks)

    def trade_closes():
        run.execution.setups = [s for s in run.execution.setups if s["symbol"] != COINS[1]]

    def pick_admitted():
        run.execution.setups.append(crypto_setup("NEW/USD", maintained=True))
        picks.pop(0)

    socket = CappedStream(run, [trade_closes, pick_admitted])
    run.connector = lambda *_, **__: socket
    run.market_stream_session("CRYPTO")  # A provider error would end it with an exception.
    first = {*COINS[:13], "BTC/USD", "NEW/USD"}
    second = first - {COINS[1]} | {"NEXT/USD"}
    assert socket.sent[1:] == [
        {"action": "subscribe", "trades": sorted(first), "quotes": sorted(first)},
        {"action": "unsubscribe", "trades": [COINS[1]], "quotes": [COINS[1]]},
        {"action": "subscribe", "trades": ["NEXT/USD"], "quotes": ["NEXT/USD"]},
    ]
    assert socket.largest == 15 and run.market_subscriptions["CRYPTO"] == second
    assert [body["symbols"] for kind, body in run.execution.events
            if kind == "RUNTIME_MARKET_CONNECTED"] == [sorted(first), sorted(second)]
    assert run._stream_plan("CRYPTO") == (second, ["LAST/USD"])


def test_the_status_reports_the_crypto_stream_plan():
    run = runtime()
    full_stream(run)
    run._selected_packets = lambda: [offered("NEW/USD", 41), offered("OTHER/USD", 42)]
    status = run.status()
    section = {"version": "CRYPTO_STREAM_CAPACITY_V1", "capacity": 15, "wanted": 15,
               "held_back": ["NEW/USD", "OTHER/USD"]}
    assert status["crypto_stream"] == section
    assert public_status(status)["crypto_stream"] == section and "crypto_stream" in STATUS_FIELDS
    freed = {**status, "crypto_stream": {**section, "held_back": ["OTHER/USD"]}}
    assert ManagedRuntime._heartbeat_signature(freed) != ManagedRuntime._heartbeat_signature(
        status)  # A changed plan is a change for the heartbeat record.

    def unreadable():
        raise RuntimeError("ledger unavailable")

    run._selected_packets = unreadable
    assert run.status()["crypto_stream"] == {"version": "CRYPTO_STREAM_CAPACITY_V1",
                                             "capacity": 15, "available": False}


class _Waits:
    """``stop_event`` for a reconnect loop: records each wait and stops after ``limit``."""

    def __init__(self, limit):
        self.limit, self.seen = limit, []

    def is_set(self):
        return len(self.seen) >= self.limit

    def wait(self, seconds):
        self.seen.append(seconds)
        return self.is_set()


@pytest.mark.parametrize("loop", ["trade", "market"])
def test_the_reconnect_wait_starts_again_after_a_session_that_stayed_up(monkeypatch, loop):
    """Drops in a row double the wait; a drop after a session that stayed up at least
    max_reconnect_seconds waits the first reconnect_seconds again. Until 2026-09-29 the wait
    never reset, so a long-running process waited the 30-second cap after every drop."""
    import catalyst_lab.managed_runtime as runtime_module

    run = runtime()
    first, cap = run.policy.reconnect_seconds, run.policy.max_reconnect_seconds
    clock = [0.0]
    durations = iter([0.1, 0.1, 0.1, cap + 1, 0.1])
    monkeypatch.setattr(runtime_module, "_monotonic", lambda: clock[0])

    def dropped(*_args):
        clock[0] += next(durations)
        raise ConnectionError("STREAM_DROPPED")

    run.stop_event = _Waits(5)
    if loop == "trade":
        run.stream_session = dropped
        run._stream_loop()
    else:
        run._desired_symbols = lambda market: {"BTC/USD"}
        run.market_stream_session = dropped
        run._market_stream_loop("CRYPTO")
    assert run.stop_event.seen == [first, min(2 * first, cap), min(4 * first, cap), first,
                                   min(2 * first, cap)]
    assert runtime_module.reconnect_wait(cap, cap, run.policy) == first
    assert runtime_module.reconnect_wait(cap, cap - 1, run.policy) == cap


def test_old_queued_print_is_invalidated_instead_of_replayed_into_order():
    from datetime import timedelta

    run = runtime()
    ready(run)
    run.now = lambda: NOW + timedelta(seconds=6)
    run.execution_once()
    assert not run.execution.triggered
    assert run.execution.setups[0]["state"]["state"] == "INVALIDATED"
    assert not run.queued


def test_out_of_order_print_is_explicit_gap_not_silently_dropped():
    from datetime import timedelta

    run = runtime()
    ready(run)
    with pytest.raises(ValueError, match="OUT_OF_ORDER_MARKET_TRADE"):
        run.market_message(
            "CRYPTO",
            {
                "T": "t",
                "S": "BTC/USD",
                "p": "94",
                "i": 3,
                "t": (NOW - timedelta(seconds=1)).isoformat(),
            },
        )


def test_position_jev_runs_while_research_selection_awaits_provider():
    async def scenario():
        release, monitor_seen = asyncio.Event(), asyncio.Event()

        async def review_position(*_):
            monitor_seen.set()

        run = runtime(monitor_tick=review_position)
        ready(run)
        run.execution.setups[0]["state"]["state"] = "OPEN"

        async def delayed_research(_):
            await release.wait()

        run.research.tick = delayed_research
        task = asyncio.create_task(run.research_once())
        await asyncio.wait_for(monitor_seen.wait(), timeout=1)
        assert not task.done()
        release.set()
        await task

    asyncio.run(scenario())


def test_account_safety_failure_blocks_entries_but_keeps_managed_protection():
    def fail():
        raise ValueError("unavailable")

    safety = SimpleNamespace(tick=fail)
    run = runtime(account_safety=safety)
    ready(run)
    run.execution_once()
    assert not run.ready() and not run.execution.triggered
    assert run.execution.managed
    assert not run.status()["account_safety_healthy"]
    safety.tick = lambda: None
    run.execution_once()
    assert not run.ready()  # A clean tick cannot replace reconciliation.
    run.reconcile_once()
    assert run.ready()


def test_faulting_research_cycle_is_recorded_once_and_later_cycles_continue():
    run = runtime()
    run._cycle_ids = lambda: ["policy-cycle", "opaque-cycle", "publish-cycle", "healthy-cycle"]
    tick, publish = run.research.tick, run.research.approved_packets

    async def faulting_tick(cycle_id):
        if cycle_id == "policy-cycle":
            raise ValueError("RESEARCH_POLICY_CHANGED")
        if cycle_id == "opaque-cycle":
            raise RuntimeError("provider detail that must never be persisted")
        await tick(cycle_id)

    def faulting_publish(cycle_id):
        if cycle_id == "publish-cycle":
            raise ValueError("IDEMPOTENCY_CONTENT_MISMATCH")
        publish(cycle_id)

    run.research.tick, run.research.approved_packets = faulting_tick, faulting_publish
    run.research_healthy = True
    for _ in range(3):
        asyncio.run(run.research_once())
    assert run.research.published == ["healthy-cycle"] * 3
    assert run.research.ticks == ["publish-cycle", "healthy-cycle"] * 3
    faults = [body for kind, body in run.execution.events if kind == "RESEARCH_CYCLE_FAULT"]
    assert faults == [
        {"cycle_id": "policy-cycle", "code": "RESEARCH_POLICY_CHANGED"},
        {"cycle_id": "opaque-cycle", "code": "RuntimeError"},
        {"cycle_id": "publish-cycle", "code": "IDEMPOTENCY_CONTENT_MISMATCH"},
    ]
    assert "research:policy-cycle:fault:RESEARCH_POLICY_CHANGED" in run.execution.keyed
    assert "must never be persisted" not in repr(run.execution.events)
    assert run.error is None and run.last_research_tick is not None


def test_admission_refusal_audit_is_keyed_by_runtime_selection_and_reason():
    run = runtime()
    ready(run)
    packet = {"market": "CRYPTO", "symbol": "BTC/USD", "selection_event_seq": 7}
    run._selected_packets = lambda: [packet]
    reasons = iter(["ACCOUNT_EXIT_PENDING"] * 3 + ["DAILY_RISK_HALT"])

    def refuse(_):
        raise ValueError(next(reasons))

    run.execution.admit = refuse
    for _ in range(4):
        run.execution_once()
    refusals = [body for kind, body in run.execution.events if kind == "RUNTIME_ADMISSION_REFUSED"]
    assert refusals == [
        {"runtime_id": run.runtime_id, "selection_event_seq": 7, "reason": reason}
        for reason in ("ACCOUNT_EXIT_PENDING", "DAILY_RISK_HALT")
    ]
    assert not any(kind == "RESEARCH_ADMISSION_DECLINED" for kind, _ in run.execution.events)
    assert run.error is None


def test_every_stored_review_binding_refusal_is_classified_permanent():
    import inspect
    import re
    from pathlib import Path

    import catalyst_lab
    from catalyst_lab.jev_store import JevStore
    from catalyst_lab.managed_runtime import PERMANENT_ADMISSION_REFUSALS, failure_code

    migrations = Path(catalyst_lab.__file__).parent / "migrations"
    sql_reasons = set()
    for name in ("013_managed_paper.sql", "014_managed_completion.sql"):
        sql_reasons |= set(re.findall(r"RETURN '([A-Z_]+)'", (migrations / name).read_text()))
    verify_source = inspect.getsource(JevStore.verify)
    receipt_codes = set(re.findall(r'ValueError\("([A-Z_]+)"\)', verify_source))
    assert len(sql_reasons) == 11 and len(receipt_codes) == 8
    assert sql_reasons | receipt_codes <= PERMANENT_ADMISSION_REFUSALS
    transient = {"RISK_HALT", "DAILY_RISK_HALT", "ACCOUNT_EXIT_PENDING",
                 "CRYPTO_ENTRY_WINDOW_CLOSED", "ACTIVE_SYMBOL_ALREADY_MANAGED"}
    assert not transient & PERMANENT_ADMISSION_REFUSALS
    assert failure_code(ValueError("QUALITY_POLICY_REQUIRED")) == "QUALITY_POLICY_REQUIRED"
    assert failure_code(ValueError("PKABCDEFGHIJKLMNOPQRST")) == "ValueError"
    assert failure_code(KeyError("stop")) == "KeyError"


def reviewed_cycle(mx, index):
    """Real ResearchCycle output for one crypto Muse item approved by the fake Jev."""
    engine, venue, receipts = mx
    reviewer = JevReviewer(
        receipts,
        ReliabilityPolicy("RUNTIME_ADMISSION_FIXTURE", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response())),
        key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now,
    )
    cycle = ResearchCycle(
        engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30), clock=lambda: venue.now
    )
    cycle_id = str(uuid4())
    cycle.start_report(
        {
            "report_id": cycle_id,
            "generated_at": venue.now.isoformat(),
            "valid_until": (venue.now + timedelta(minutes=5)).isoformat(),
            "items": [external_muse_item(index, venue.now)],
        },
        max_seconds=300,
    )
    assert asyncio.run(cycle.tick(cycle_id))[0]["disposition"] == "APPROVED"
    return cycle, cycle_id


def historical_publication(cycle, cycle_id, monkeypatch):
    """Publish exactly what the pre-fix code stored for a cycle at or below its limit."""
    original = cycle._event

    def unflagged(conn, cid, kind, body, key):
        if kind == "SELECTED":
            body = {
                "packet": {
                    k: v
                    for k, v in body["packet"].items()
                    if k not in {"quality_required", "selection_limit"}
                }
            }
        return original(conn, cid, kind, body, key)

    with monkeypatch.context() as patch:
        patch.setattr(cycle, "_event", unflagged)
        return cycle.approved_packets(cycle_id)


def db_runtime(mx, research, symbol):
    engine, venue, _ = mx
    run = ManagedRuntime(
        engine,
        research,
        Source(),
        AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret"),
        engineering_runtime_policy(),
        clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True,
    )
    run.connected = run.research_healthy = True
    assert run.reconcile_once()
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {symbol}
    assert run.ready()
    return run


def counted_admissions(engine, monkeypatch):
    attempts, admit = [], engine.admit

    def counting(packet):
        attempts.append(packet["selection_event_seq"])
        return admit(packet)

    monkeypatch.setattr(engine, "admit", counting)
    return attempts


def bodies(engine, kind):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq", (kind,)
        ).fetchall()
    return [row["body"] for row in rows]


def test_transient_admission_refusal_is_retried_but_recorded_once(mx, monkeypatch):
    engine, venue, _ = mx
    cycle, cycle_id = reviewed_cycle(mx, 1)
    selected = cycle.approved_packets(cycle_id)[0]
    venue.inventory["UNKNOWN/USD"] = D(1)
    assert engine.reconcile()["clean"] is False  # Latches a durable execution halt.
    del venue.inventory["UNKNOWN/USD"]
    run = db_runtime(mx, cycle, selected["symbol"])
    attempts = counted_admissions(engine, monkeypatch)
    for _ in range(3):
        run.execution_once()
    seq = selected["selection_event_seq"]
    assert attempts == [seq] * 3
    assert bodies(engine, "RUNTIME_ADMISSION_REFUSED") == [
        {"runtime_id": run.runtime_id, "selection_event_seq": seq, "reason": "RISK_HALT"}
    ]
    assert bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []
    assert [p["selection_event_seq"] for p in run._selected_packets()] == [seq]
    assert run.error is None


def test_permanent_admission_refusal_is_declined_and_never_retried(mx, monkeypatch):
    engine, _, _ = mx
    cycle, cycle_id = reviewed_cycle(mx, 3)
    historical = historical_publication(cycle, cycle_id, monkeypatch)[0]
    assert "quality_required" not in historical
    run = db_runtime(mx, cycle, historical["symbol"])
    attempts = counted_admissions(engine, monkeypatch)
    for _ in range(3):
        run.execution_once()
    seq = historical["selection_event_seq"]
    assert attempts == [seq]
    assert bodies(engine, "RUNTIME_ADMISSION_REFUSED") == [
        {
            "runtime_id": run.runtime_id,
            "selection_event_seq": seq,
            "reason": "QUALITY_POLICY_REQUIRED",
        }
    ]
    assert bodies(engine, "RESEARCH_ADMISSION_DECLINED") == [
        {
            "cycle_id": cycle_id,
            "item_key": historical["item_key"],
            "revision": 1,
            "receipt_id": historical["receipt_id"],
            "selection_event_seq": seq,
            "reason": "QUALITY_POLICY_REQUIRED",
        }
    ]
    assert run._selected_packets() == []
    assert historical["symbol"] not in run._desired_symbols("CRYPTO")
    # The stored historical packet stays published as-is: never re-emitted or rewritten.
    asyncio.run(run._research_pass())
    assert cycle.approved_packets(cycle_id) == [historical]
    assert [e["kind"] for e in cycle.outputs(cycle_id)].count("RESEARCH_SELECTED") == 1
    assert bodies(engine, "RESEARCH_CYCLE_FAULT") == []
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.managed_setups").fetchone()
    assert run.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def test_v2_packet_without_quality_flag_still_fails_review_sql(mx):
    engine, _, _ = mx
    cycle, cycle_id = reviewed_cycle(mx, 5)
    current = cycle.approved_packets(cycle_id)[0]
    assert current["selection_policy"] == "MUSE_JEV_RESEARCH_SELECTION_V2"
    assert current["quality_required"] is False
    stored = {k: v for k, v in current.items() if k != "selection_event_seq"}
    variants = {}
    with engine.store.transaction() as conn:
        for name, packet in (
            ("absent", {k: v for k, v in stored.items() if k != "quality_required"}),
            ("untyped", {**stored, "quality_required": "false"}),
        ):
            event = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": packet})
            variants[name] = {**packet, "selection_event_seq": event["event_seq"]}

    def failure(packet):
        with engine.repo.connect() as conn:
            return conn.execute(
                "SELECT lab.managed_review_failure(%s) AS reason", (Jsonb(json_safe(packet)),)
            ).fetchone()["reason"]

    assert failure(current) is None
    assert failure(variants["absent"]) == "QUALITY_POLICY_REQUIRED"
    assert failure(variants["untyped"]) == "QUALITY_POLICY_REQUIRED"
    with pytest.raises(ValueError, match="QUALITY_POLICY_REQUIRED"):
        engine.admit(variants["absent"])
