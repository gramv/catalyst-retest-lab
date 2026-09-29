"""Per-cause, per-setup runtime latches: fake runtime collaborators and disposable DB only.

Every rule is exercised through ManagedRuntime itself. The fixture execution records
events under the same idempotency semantics as ManagedStore.event; the operator-clear and
durable-halt tests run against a disposable PostgreSQL ledger.
"""

import json
import sys
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from catalyst_lab import cloud_runtime, managed_ops
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.broker_budget import BrokerBudget, BrokerRateLimited
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.managed_latches import (
    ACCOUNT_SAFETY,
    AUDIT,
    PERSISTENT,
    PROTECTION,
    REST,
    LatchBook,
    LatchPolicy,
    TickRecord,
    classify_failure,
)
from catalyst_lab.managed_ops import clear_protection_latch, config_template
from catalyst_lab.managed_runtime import (
    ManagedRuntime,
    engineering_runtime_policy,
    failure_code,
)
from catalyst_lab.market import MarketDataError
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import packet
from tests.test_managed_runtime import Source, runtime
from tests.test_managed_runtime import ready as make_ready
from tests.test_position_monitor import opened
from tests.test_setup_scan import NOW


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)


class Faults:
    """Make the fixture execution's manage raise ``error`` while it is set."""

    def __init__(self, run):
        self.error = None
        original = run.execution.manage

        def manage(setup_id, observation):
            if self.error is not None:
                raise self.error
            return original(setup_id, observation)

        run.execution.manage = manage


def events(run, kind):
    return [body for event_kind, body in run.execution.events if event_kind == kind]


def ticks(run, count, clock=None):
    for _ in range(count):
        run.execution_once()
        if clock is not None:
            clock.advance()


def unavailable(*_, **__):
    raise psycopg.OperationalError("database unavailable")


def test_transient_protection_failure_needs_k_clean_ticks_and_a_later_reconciliation():
    run = runtime()
    make_ready(run)
    faults = Faults(run)
    assert run.ready()
    faults.error = psycopg.OperationalError("server closed the connection unexpectedly")
    ticks(run, 3)
    assert run.error == PROTECTION and not run.ready()
    assert run.reconciled_at is None and run.execution.reconciled_at is None
    latched = events(run, "RUNTIME_LATCH_SET")
    assert latched == [{
        "runtime_id": run.runtime_id, "cause": PROTECTION, "scope": "setup-1", "episode": 1,
        "category": "TRANSIENT", "code": "OperationalError",
        "exception_class": "OperationalError", "phase": "MANAGE",
        "first_failure_at": NOW.isoformat(),
        "clears_by": "K_CLEAN_TICKS_AND_CLEAN_RECONCILIATION",
        "clear_after_clean_ticks": 10, "persistent_failure_seconds": 30,
    }]  # One event for three failing ticks; free text never reaches the ledger.
    assert f"runtime-latch:{run.runtime_id}:{PROTECTION}:setup-1:1:SET:TRANSIENT" in (
        run.execution.keyed
    )
    faults.error = None
    ticks(run, 12)
    assert run.error == PROTECTION  # Clean ticks alone never replace a reconciliation.
    assert run.reconcile_once()
    assert run.error is None and run.ready()
    assert events(run, "RUNTIME_LATCH_CLEARED") == [{
        "runtime_id": run.runtime_id, "cause": PROTECTION, "scope": "setup-1", "episode": 1,
        "category": "TRANSIENT", "cleared_by": "K_CLEAN_TICKS_AND_CLEAN_RECONCILIATION",
    }]


def test_clean_ticks_count_only_after_the_last_failure():
    run = runtime()
    make_ready(run)
    faults = Faults(run)
    faults.error = MarketDataError("ALPACA_HTTP_503")
    run.execution_once()
    faults.error = None
    assert run.reconcile_once()
    ticks(run, 5)
    faults.error = MarketDataError("ALPACA_HTTP_503")  # A relapse restarts the count.
    run.execution_once()
    faults.error = None
    assert run.reconcile_once()
    ticks(run, 9)
    assert run.error == PROTECTION
    run.execution_once()
    assert run.error is None
    assert len(events(run, "RUNTIME_LATCH_SET")) == 1  # Still one episode.


@pytest.mark.parametrize(
    "error",
    [ValueError("STOP_WIDENING_REFUSED"), ValueError("CONFLICTING_BROKER_EXECUTION"),
     KeyError("qty"), SubmissionDisabled("BROKER_MUTATION_NOT_ALLOWED")],
)
def test_invariant_failures_never_clear_automatically(error):
    run = runtime()
    make_ready(run)
    faults = Faults(run)
    faults.error = error
    run.execution_once()
    faults.error = None
    ticks(run, 25)
    assert run.reconcile_once()
    ticks(run, 5)
    assert run.error == PROTECTION and not run.ready()
    [latched] = events(run, "RUNTIME_LATCH_SET")
    assert latched["category"] == "INVARIANT" and latched["code"] == failure_code(error)
    assert latched["clears_by"] == "OPERATOR_CLEAR_PROTECTION_LATCH"
    assert not events(run, "RUNTIME_LATCH_CLEARED")


def test_transient_latch_escalates_to_invariant_within_one_episode():
    run = runtime()
    make_ready(run)
    faults = Faults(run)
    faults.error = MarketDataError("ALPACA_HTTP_502")
    run.execution_once()
    faults.error = ValueError("TARGET_REDUCTION_REFUSED")
    run.execution_once()
    faults.error = None
    ticks(run, 12)
    assert run.reconcile_once()
    assert run.error == PROTECTION
    assert [(e["episode"], e["category"], e["code"]) for e in events(run, "RUNTIME_LATCH_SET")] == [
        (1, "TRANSIENT", "ALPACA_HTTP_502"), (1, "INVARIANT", "TARGET_REDUCTION_REFUSED")
    ]


@pytest.mark.parametrize(
    "error",
    [MarketDataError("ALPACA_HTTP_429"), MarketDataError("ALPACA_CONNECTION_ERROR"),
     BrokerRateLimited("BROKER_BUDGET_EXHAUSTED"), httpx.ReadTimeout("slow")],
)
def test_rate_limits_and_timeouts_block_entries_but_never_latch_protection(error):
    run = runtime()
    make_ready(run)
    faults = Faults(run)
    reconciled = run.reconciled_at
    faults.error = error
    ticks(run, 5)
    assert run.error == REST and not run.ready()
    assert not run.latches.has(PROTECTION)
    assert run.reconciled_at == reconciled  # A rate limit says nothing about broker state.
    faults.error = None
    ticks(run, 9)
    assert run.error == REST
    run.execution_once()
    assert run.error is None and run.ready()
    assert [e["cause"] for e in events(run, "RUNTIME_LATCH_SET")] == [REST]
    assert [e["cleared_by"] for e in events(run, "RUNTIME_LATCH_CLEARED")] == [
        "K_CLEAN_TICKS_AFTER_RETRY_AFTER"
    ]


def test_rest_degraded_waits_for_retry_after_to_elapse():
    clock = Clock()
    budget = BrokerBudget(clock=clock)
    run = runtime(broker_budget=budget)
    run.now = clock
    make_ready(run)
    faults = Faults(run)
    budget.observe(httpx.Response(429, headers={"Retry-After": "20"}))
    faults.error = MarketDataError("ALPACA_HTTP_429")
    run.execution_once()
    faults.error = None
    ticks(run, 12)
    assert run.error == REST  # Clean ticks, but the broker asked for 20 seconds.
    clock.advance(20)
    run.execution_once()
    assert run.error is None


def test_audit_outage_clears_after_an_audit_write_and_a_clean_reconciliation():
    run = runtime()
    make_ready(run)
    original = run.execution._event
    run.execution._event = unavailable
    run.heartbeat_once()
    assert run.error == AUDIT and not run.ready() and run.reconciled_at is None
    ticks(run, 3)
    assert run.reconcile_once() and run.error == AUDIT  # No audit write has succeeded yet.
    run.execution._event = original
    run.execution_once()  # The deferred latch event is written: the audit works again.
    assert [e["cause"] for e in events(run, "RUNTIME_LATCH_SET")] == [AUDIT]
    assert run.error == AUDIT  # The reconciliation above preceded the recovery.
    assert run.reconcile_once()
    assert run.error is None and run.ready()
    assert [e["cleared_by"] for e in events(run, "RUNTIME_LATCH_CLEARED")] == [
        "AUDIT_WRITE_AND_CLEAN_RECONCILIATION"
    ]


def test_keyed_notices_share_the_single_audit_latch():
    run = runtime()
    make_ready(run)
    run.execution._event = unavailable
    assert run._audit_once("RESEARCH_CYCLE_FAULT", {"cycle_id": "c", "code": "X_Y"},
                           "research:c:fault:X_Y") is False
    assert run.error == AUDIT and run.latches.has(AUDIT) and not run.ready()


def attach_halt_ledger(run):
    halts = []
    run.execution.store.transaction = lambda: nullcontext("fixture-connection")
    run.execution._latch_execution_halt = lambda conn, reason, details: halts.append(
        (reason, details)
    )
    return halts


def test_open_position_protection_failing_over_thirty_seconds_halts_durably():
    clock = Clock()
    run = runtime()
    run.now = clock
    make_ready(run)
    halts = attach_halt_ledger(run)
    run.execution.setups[0]["state"] = {"state": "OPEN", "qty": "0.5"}
    faults = Faults(run)
    faults.error = MarketDataError("ALPACA_HTTP_503")
    ticks(run, 31, clock)  # Failing from t=0 to t=30: not yet longer than 30 seconds.
    assert not halts and not run.status()["protection_critical"]
    run.execution_once()  # t=31.
    assert halts == [(PERSISTENT, {
        "scope": "setup-1", "runtime_id": run.runtime_id, "episode": 1,
        "first_failure_at": NOW.isoformat(), "code": "ALPACA_HTTP_503", "setup_id": "setup-1",
    })]
    status = run.status()
    assert status["protection_critical"] and status["error"] == PERSISTENT
    assert [e["cause"] for e in events(run, "RUNTIME_LATCH_SET")] == [PROTECTION, PERSISTENT]
    faults.error = None
    ticks(run, 12, clock)
    assert run.reconcile_once()
    assert not run.latches.has(PROTECTION)  # The transient latch followed its own rule.
    assert run.error == PERSISTENT and not run.ready()  # The durable failure did not.
    assert len(halts) == 1


def test_intermittent_or_positionless_failures_do_not_halt():
    clock = Clock()
    run = runtime()
    run.now = clock
    make_ready(run)
    halts = attach_halt_ledger(run)
    faults = Faults(run)
    run.execution.setups[0]["state"] = {"state": "OPEN", "qty": "1"}
    for second in range(60):  # Protection keeps succeeding every other second.
        faults.error = MarketDataError("ALPACA_HTTP_503") if second % 2 == 0 else None
        run.execution_once()
        clock.advance()
    run.execution.setups[0]["state"] = {"state": "WATCHING", "qty": "0"}
    faults.error = MarketDataError("ALPACA_HTTP_503")
    ticks(run, 60, clock)
    assert not halts and not run.latches.has(PERSISTENT)
    assert run.error == PROTECTION


def test_entry_order_working_counts_as_exposure_for_the_persistent_rule():
    clock = Clock()
    run = runtime()
    run.now = clock
    make_ready(run)
    halts = attach_halt_ledger(run)
    run.execution.setups[0]["state"] = {"state": "ORDER_SUBMITTED", "qty": "0"}
    faults = Faults(run)
    faults.error = ValueError("OWNED_PROTECTION_REQUIRED")
    ticks(run, 32, clock)
    assert [reason for reason, _ in halts] == [PERSISTENT]


def test_whole_tick_failure_with_known_exposure_also_halts():
    clock = Clock()
    run = runtime()
    run.now = clock
    make_ready(run)
    halts = attach_halt_ledger(run)
    run.execution.setups[0]["state"] = {"state": "OPEN", "qty": "1"}
    run.execution_once()  # Exposure is known from the last successful read.
    active = run.execution.store.active
    run.execution.store.active = unavailable
    ticks(run, 33, clock)
    assert halts and halts[0][1]["scope"] == "RUNTIME"
    run.execution.store.active = active
    assert run.latches.has(PROTECTION, "RUNTIME")


def test_trigger_fault_is_latched_without_skipping_protection():
    run = runtime()
    make_ready(run)

    def broken():
        raise ValueError("PRINT_QUEUE_UNREADABLE")

    run._pending_trades = broken
    run.execution_once()
    assert run.execution.managed  # Every active setup was still managed.
    assert run.latches.has(PROTECTION, "TRIGGERS")
    assert run.status()["last_protection_tick"] is not None


def test_account_safety_failure_is_one_latch_episode_with_its_existing_recovery():
    def fail():
        raise ValueError("BROKER_ACCOUNT_BLOCKED")

    safety = SimpleNamespace(tick=fail)
    run = runtime(account_safety=safety)
    make_ready(run)
    ticks(run, 5)
    assert not run.status()["account_safety_healthy"] and not run.ready()
    assert [e["cause"] for e in events(run, "RUNTIME_LATCH_SET")] == [ACCOUNT_SAFETY]
    assert run.error is None  # Reported by account_safety_healthy, as before.
    safety.tick = lambda: None
    run.execution_once()
    assert [e["cleared_by"] for e in events(run, "RUNTIME_LATCH_CLEARED")] == [
        "CLEAN_ACCOUNT_SAFETY_TICK"
    ]
    assert not run.ready()  # The failure also required a new reconciliation.
    assert run.reconcile_once() and run.ready()


def test_status_exposes_latches_and_the_broker_budget():
    budget = BrokerBudget(clock=lambda: NOW)
    run = runtime(broker_budget=budget, latch_policy=LatchPolicy(3, 30))
    status = run.status()
    assert status["error"] is None and not status["protection_critical"]
    assert status["latches"] == {"active": [], "protection_clean_ticks": 0,
                                 "rest_clean_ticks": 0, "clear_after_clean_ticks": 3,
                                 "persistent_failure_seconds": 30}
    assert status["broker_budget"]["requests_per_minute"] == 150
    assert status["broker_budget"]["snapshot_seconds"] == 5
    make_ready(run)
    faults = Faults(run)
    faults.error = MarketDataError("ALPACA_HTTP_500")
    run.execution_once()
    [latch] = run.status()["latches"]["active"]
    assert latch == {
        "cause": PROTECTION, "scope": "setup-1", "episode": 1, "category": "TRANSIENT",
        "code": "ALPACA_HTTP_500", "exception_class": "MarketDataError", "phase": "MANAGE",
        "first_failure_at": NOW.isoformat(), "failures": 1,
        "clears_by": "K_CLEAN_TICKS_AND_CLEAN_RECONCILIATION", "state": "LATCHED",
        "recorded": True, "durable_halt_pending": False,
    }
    json.dumps(run.status())  # The heartbeat event stores the status as JSON.


def test_latch_policy_and_classification():
    for k, seconds in ((2, 30), (11, 30), (10, 4), (True, 30)):
        with pytest.raises(ValueError, match="EXPLICIT_LATCH_POLICY_REQUIRED"):
            LatchPolicy(k, seconds)
    assert classify_failure(SubmissionDisabled("STALE_POSITION_REVISION")) == "TRANSIENT"
    assert classify_failure(SubmissionDisabled("EXECUTOR_OWNERSHIP_LOST")) == "INVARIANT"
    assert classify_failure(MarketDataError("ALPACA_HTTP_401")) == "INVARIANT"
    assert classify_failure(RuntimeError("anything else")) == "INVARIANT"
    book = LatchBook(LatchPolicy(), "runtime")
    book.record(REST, "RUNTIME", "REST", "ALPACA_HTTP_429", "MarketDataError", "MANAGE", NOW)
    book.finish_tick(TickRecord(rest_failed=True), NOW, set())
    assert book.blocking() and book.error() == REST


# --- Disposable PostgreSQL ---------------------------------------------------------


def db_runtime(mx):
    engine, venue, _ = mx
    run = ManagedRuntime(
        engine, SimpleNamespace(), Source(),
        AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret"),
        engineering_runtime_policy(), clock=lambda: venue.now, reviewer_heartbeat=lambda: True,
    )
    run._selected_packets = lambda: []
    run.connected = run.research_healthy = True
    run.market_connected["CRYPTO"] = True
    run.market_subscriptions["CRYPTO"] = {"BTC/USD"}
    assert run.reconcile_once()
    return run


def ledger_events(engine, kind):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT event_seq,idempotency_key,setup_id,body FROM lab.managed_events "
            "WHERE kind=%s ORDER BY event_seq", (kind,),
        ).fetchall()


def private_config(tmp_path, database_url):
    config = config_template(tmp_path)
    config["environment"]["MANAGED_DATABASE_URL"] = database_url
    path = tmp_path / "private.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path, config


def test_audited_operator_command_releases_only_earlier_invariant_latches(
    mx, tmp_path, monkeypatch, capsys
):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx))
    run = db_runtime(mx)
    manage = engine.manage

    def conflicting(*_):
        raise ValueError("CONFLICTING_BROKER_EXECUTION")

    monkeypatch.setattr(engine, "manage", conflicting)
    run.execution_once()
    monkeypatch.setattr(engine, "manage", manage)
    for _ in range(12):
        run.execution_once()
    assert run.reconcile_once()
    assert run.error == PROTECTION and not run.ready()
    [latched] = ledger_events(engine, "RUNTIME_LATCH_SET")
    assert latched["idempotency_key"] == (
        f"runtime-latch:{run.runtime_id}:{PROTECTION}:{sid}:1:SET:INVARIANT"
    )
    assert latched["setup_id"] == sid and latched["body"]["code"] == "CONFLICTING_BROKER_EXECUTION"
    with pytest.raises(ValueError, match="OPERATOR_REASON_REQUIRED"):
        clear_protection_latch({}, "too short")
    path, config = private_config(tmp_path, engine.repo.database_url)
    reason = "Owner verified the broker fills; the conflict was a duplicated stream delivery."
    monkeypatch.setattr(sys, "argv", ["managed_ops", "clear-protection-latch",
                                      "--config", str(path), "--reason", reason])
    managed_ops.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["kind"] == "OPERATOR_PROTECTION_LATCH_CLEARED"
    assert printed["event_seq"] > latched["event_seq"]
    # A retried command against an unchanged ledger is the same audited event.
    repeat = clear_protection_latch(config, reason, repository=RiskRepository(
        engine.repo.database_url
    ))
    assert repeat["event_seq"] == printed["event_seq"]
    run.execution_once()
    assert run.error is None and run.ready()
    [cleared] = ledger_events(engine, "RUNTIME_LATCH_CLEARED")
    assert cleared["body"]["cleared_by"] == "OPERATOR_CLEAR_PROTECTION_LATCH"
    assert cleared["body"]["operator_event_seq"] == printed["event_seq"]
    assert cleared["idempotency_key"].endswith(f"{PROTECTION}:{sid}:1:CLEARED")
    # An invariant latch recorded after the operator's event is not released by it.
    monkeypatch.setattr(engine, "manage", conflicting)
    run.execution_once()
    monkeypatch.setattr(engine, "manage", manage)
    for _ in range(12):
        run.execution_once()
    assert run.error == PROTECTION
    assert len(ledger_events(engine, "RUNTIME_LATCH_SET")) == 2
    assert verify_events(engine.repo.export_events())["valid"]


def test_the_railway_trader_shell_command_clears_exactly_like_the_mac_cli(
    mx, monkeypatch, capsys
):
    """package cloud-hardening: ``cloud_runtime clear-protection-latch`` over the trader's own
    MANAGED_DATABASE_URL releases the same operator-only latch, recorded as the trader shell's."""
    engine, venue, _ = mx
    sid = engine.admit(packet(mx))
    run = db_runtime(mx)
    manage = engine.manage

    def conflicting(*_):
        raise ValueError("CONFLICTING_BROKER_EXECUTION")

    monkeypatch.setattr(engine, "manage", conflicting)
    run.execution_once()
    monkeypatch.setattr(engine, "manage", manage)
    for _ in range(12):
        run.execution_once()
    assert run.reconcile_once() and run.error == PROTECTION and not run.ready()
    # The trader's variable (checked as the cloud checks it: the risk login, the ledger
    # database, a password); this test's own ledger database is the per-test copy behind it.
    environ = {"MANAGED_DATABASE_URL": "host=/tmp/trader-socket port=5432 dbname=catalyst_lab "
                                       "user=catalyst_risk password=fixture-unused"}
    ledger = RiskRepository(engine.repo.database_url)
    reason = "Owner verified the broker fills from the Railway trader shell after the conflict."
    assert cloud_runtime.clear_protection_latch("too short", environ, repository=ledger) == 2
    assert capsys.readouterr().out.strip() == (
        "CLOUD_LATCH_CLEAR_REFUSED: OPERATOR_REASON_REQUIRED")
    assert cloud_runtime.clear_protection_latch(reason, environ, repository=ledger) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["kind"] == "OPERATOR_PROTECTION_LATCH_CLEARED"
    assert printed["mode"] == "PAPER_ONLY" and "fixture-unused" not in json.dumps(printed)
    [recorded] = ledger_events(engine, "OPERATOR_PROTECTION_LATCH_CLEARED")
    assert recorded["body"]["operator"] == "RAILWAY_TRADER_SHELL"
    assert recorded["body"]["reason"] == reason
    assert cloud_runtime.clear_protection_latch(reason, environ, repository=ledger) == 0
    assert json.loads(capsys.readouterr().out)["event_seq"] == printed["event_seq"]
    run.execution_once()
    assert run.error is None and run.ready()
    [cleared] = ledger_events(engine, "RUNTIME_LATCH_CLEARED")
    assert cleared["body"]["operator_event_seq"] == printed["event_seq"]
    assert cleared["idempotency_key"].endswith(f"{PROTECTION}:{sid}:1:CLEARED")
    assert verify_events(engine.repo.export_events())["valid"]
    with pytest.raises(ValueError, match="OPERATOR_ORIGIN_INVALID"):
        clear_protection_latch({}, reason, repository=object(), operator="SOMEONE_ELSE")


def test_persistent_failure_writes_a_durable_execution_halt(mx, monkeypatch):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    run = db_runtime(mx)

    def unavailable_broker(*_):
        raise MarketDataError("ALPACA_HTTP_503")

    monkeypatch.setattr(engine, "manage", unavailable_broker)
    for _ in range(32):
        run.execution_once()
        venue.now += timedelta(seconds=1)
    with engine.repo.connect() as conn:
        halts = conn.execute(
            "SELECT reason,payload_json FROM lab.execution_halts WHERE reason=%s", (PERSISTENT,)
        ).fetchall()
    assert len(halts) == 1 and halts[0]["payload_json"]["setup_id"] == str(sid)
    assert run.status()["protection_critical"] and run.error == PERSISTENT
    with pytest.raises(ValueError, match="RISK_HALT"):
        engine.admit(packet(mx, "ETH/USD"))
    persistent = [row for row in ledger_events(engine, "RUNTIME_LATCH_SET")
                  if row["body"]["cause"] == PERSISTENT]
    assert len(persistent) == 1 and persistent[0]["setup_id"] == sid
    assert verify_events(engine.repo.export_events())["valid"]
