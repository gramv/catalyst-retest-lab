"""ManagedRuntime <-> Jev circuit-breaker recovery and health visibility.

Package jev-breaker (plan phase 0, docs/CRYPTO-AGENT-LOOP.md section 7 / "Fixes from the
2026-09-26 review: Jev breaker recovery"). Fixture evidence only: a disposable per-test
PostgreSQL database (the ``er`` fixture) and a scripted mock Jev transport. No provider,
broker, network or owner ledger is touched -- ``no_external_test_connections`` in
tests/conftest.py enforces that for the whole session.
"""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.jev_review import JevReviewer
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_ops import status_alarms
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.repository import json_safe
from catalyst_lab.review_config import APPROVED_GATE1_V2, Gate1Inputs
from catalyst_lab.review_runtime import ClockSample, Gate1Runtime
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_runtime import Execution, Source
from tests.test_research_reports import reply
from tests.test_review_worker import args, health_reply
from tests.test_review_worker import gate as gate

# The three status_alarms() bounds; the exact values do not matter for these tests, which
# only assert on JEV_BREAKER_OPEN, not on the staleness alarms a raw ManagedRuntime.status()
# also trips because it uses different key names than the mapped HTTP status.
WATCHDOG_POLICY = {
    "tick_max_age_seconds": 60,
    "reconciliation_max_age_seconds": 60,
    "research_max_age_seconds": 60,
}


def fresh_clock():
    return ClockSample(0, 0, datetime.now(UTC))


def rt(er, gate, *, slot="jev-breaker-test"):
    """A Gate1Runtime against the per-test ``er`` database (not the shared session
    cluster), so exact-count assertions on lab.jev_requests/jev_receipts are safe."""
    store = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    runtime = Gate1Runtime(
        store, gate, credential_slot=slot, worker_id=uuid4(), clock_health=fresh_clock
    )
    runtime.heartbeat("RUNNING")
    return runtime


def scripted(runtime, handler):
    return JevReviewer(
        runtime.store, runtime.policy, runtime=runtime, key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(handler),
    )


def bump_clock(er, runtime, **fields):
    """Virtual time in a disposable database only: an append-only fixture row, exactly
    like tests/test_review_worker.py's append_clock_fixture, against the per-test ``er``
    database rather than the shared session cluster."""
    state = runtime.state() | fields
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=lab_owner")) as conn:
        conn.execute(
            "INSERT INTO lab.review_breaker_events(scope_id,state_json,reason) "
            "VALUES(%s,%s,'VIRTUAL_TIME_FIXTURE')",
            (runtime.scope_id, Jsonb(json_safe(state))),
        )


class ProbeResearch:
    """The only surface ManagedRuntime.probe_once touches on ``research``: its reviewer,
    the same public attribute ResearchCycle and PositionMonitor already expose."""

    def __init__(self, reviewer):
        self.reviewer = reviewer


def managed(runtime, reviewer):
    run = ManagedRuntime(
        Execution(), ProbeResearch(reviewer), Source(),
        AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret"),
        engineering_runtime_policy(),
        clock=lambda: datetime.now(UTC),
        reviewer_heartbeat=lambda: True,
        gate1=runtime,
    )
    run.research_healthy = True
    return run


def trip(er, gate):
    """A fresh breaker, tripped OPEN by three 529s, with its cooldown already elapsed."""
    runtime = rt(er, gate)
    result = asyncio.run(scripted(runtime, lambda r: httpx.Response(529)).jev_review(**args()))
    assert result.status == "NEEDS_REVIEW" and runtime.state()["state"] == "OPEN"
    bump_clock(er, runtime, blocked_until=datetime.now(UTC) - timedelta(seconds=1))
    return runtime


def test_probe_once_does_nothing_while_the_breaker_is_closed(er, gate):
    runtime = rt(er, gate)
    calls = []

    def never(request):
        calls.append(request)
        pytest.fail("A CLOSED breaker must never be probed")

    run = managed(runtime, scripted(runtime, never))
    asyncio.run(run.probe_once())
    assert calls == [] and runtime.state()["state"] == "CLOSED"


def test_probe_once_skips_while_research_is_unhealthy(er, gate):
    """Gated on research_healthy exactly like ReviewWorker.tick gates its own probe on its
    heartbeat/clock-health result -- even though the breaker is OPEN and due."""
    runtime = trip(er, gate)
    calls = []
    reviewer = scripted(runtime, lambda r: calls.append(1) or httpx.Response(
        200, json=health_reply()
    ))
    run = managed(runtime, reviewer)
    run.research_healthy = False
    asyncio.run(run.probe_once())
    assert calls == [] and runtime.state()["state"] == "OPEN"


def test_probe_once_without_gate1_is_a_safe_noop(er, gate):
    """Most engineering fixtures build a ManagedRuntime with no gate1 at all; probing must
    stay a harmless no-op rather than raising, so existing callers are unaffected."""
    runtime = trip(er, gate)
    calls = []
    reviewer = scripted(runtime, lambda r: calls.append(1) or httpx.Response(
        200, json=health_reply()
    ))
    run = managed(runtime, reviewer)
    run.gate1 = None
    asyncio.run(run.probe_once())
    assert calls == [] and runtime.state()["state"] == "OPEN"


def test_probe_once_recovers_an_open_breaker_and_selection_reviews_resume(er, gate):
    runtime = trip(er, gate)
    calls = []

    def healthy(request):
        calls.append(1)
        return httpx.Response(200, json=health_reply())

    run = managed(runtime, scripted(runtime, healthy))

    # First probe: one successful synthetic health check moves OPEN -> HALF_OPEN.
    asyncio.run(run.probe_once())
    assert len(calls) == 1
    assert runtime.state()["state"] == "HALF_OPEN" and runtime.state()["successes"] == 1
    status_while_open = run.status()
    assert status_while_open["jev_breaker"]["state"] == "HALF_OPEN"
    assert "JEV_BREAKER_OPEN" in status_alarms(status_while_open, datetime.now(UTC),
                                                WATCHDOG_POLICY)

    # Immediately again: the required probe spacing has not elapsed, so this is a no-op.
    asyncio.run(run.probe_once())
    assert len(calls) == 1 and runtime.state()["successes"] == 1

    # Elapse the spacing (virtual time only) and probe again: breaker_recovery_successes
    # (2) is now met and the breaker closes.
    bump_clock(er, runtime, next_probe_at=datetime.now(UTC) - timedelta(seconds=1))
    asyncio.run(run.probe_once())
    assert len(calls) == 2 and runtime.state()["state"] == "CLOSED"
    status_after_recovery = run.status()
    assert status_after_recovery["jev_breaker"]["state"] == "CLOSED"
    assert "JEV_BREAKER_OPEN" not in status_alarms(status_after_recovery, datetime.now(UTC),
                                                    WATCHDOG_POLICY)

    # Selection reviews resume: an ordinary (non-probe) review completes normally again,
    # rather than being refused CIRCUIT_OPEN as every attempt was while OPEN.
    resumed = asyncio.run(
        scripted(runtime, lambda r: httpx.Response(200, json=reply())).jev_review(**args())
    )
    assert resumed.status == "RECORDED"


def test_status_reports_the_breaker_state_and_todays_call_counts(er, gate):
    runtime = rt(er, gate)
    reviewer = scripted(runtime, lambda r: httpx.Response(200, json=reply()))
    for _ in range(2):
        assert asyncio.run(reviewer.jev_review(**args())).status == "RECORDED"
    run = managed(runtime, reviewer)
    status = run.status()
    # The scope's live review policy is named beside its breaker (migration 025 adds V2).
    assert status["jev_breaker"] == {"state": "CLOSED", "epoch": 0, "blocked_until": None,
                                     "policy_version": "JEV_LIVE_REVIEW_POLICY_V1"}
    assert status["jev_calls_today"] == {"attempts": 2, "receipts": 2}
    # No DB round trip is skipped silently: the two fields come straight from gate1.
    assert status["jev_calls_today"] == runtime.calls_today(run.now())


def test_status_names_v2_beside_its_own_breaker_after_the_switch(er, gate):
    """JEV_LIVE_REVIEW_POLICY_V2 (migration 025) on the same credential slot: the owner's
    status names it, and its own scope's breaker starts closed whatever V1's holds."""
    v1 = trip(er, gate)
    v2 = rt(er, Gate1Inputs(copy.deepcopy(APPROVED_GATE1_V2)))
    assert v2.scope_id != v1.scope_id
    reviewer = scripted(v2, lambda r: httpx.Response(200, json=reply()))
    assert asyncio.run(reviewer.jev_review(**args())).status == "RECORDED"
    assert managed(v2, reviewer).status()["jev_breaker"] == {
        "state": "CLOSED", "epoch": 0, "blocked_until": None,
        "policy_version": "JEV_LIVE_REVIEW_POLICY_V2"}
    old = managed(v1, scripted(v1, lambda r: httpx.Response(529))).status()["jev_breaker"]
    assert (old["state"], old["policy_version"]) == ("OPEN", "JEV_LIVE_REVIEW_POLICY_V1")


def test_status_degrades_the_two_jev_fields_alone_when_gate1_is_unavailable(er, gate):
    """A broken secondary read must not take down the whole status payload (the exact
    failure mode this package exists to prevent for breaker visibility itself). Raising
    directly from a patched connect(), rather than pointing at a real unreachable host,
    keeps this deterministic and fast, and avoids the no_external_test_connections guard."""
    runtime = rt(er, gate)
    run = managed(runtime, scripted(runtime, lambda r: httpx.Response(200, json=reply())))

    def broken():
        raise psycopg.OperationalError("simulated fixture failure, no real connection attempted")

    runtime.store.connect = broken
    status = run.status()
    assert status["jev_breaker"] is None and status["jev_calls_today"] is None
    assert status["runtime_id"] == run.runtime_id  # The rest of the payload still comes back.


def test_calls_today_counts_attempts_and_receipts_since_ny_midnight(er, gate):
    from tests.test_jev_review import invalid_distribution, response_for

    runtime = rt(er, gate)
    replies = [httpx.Response(200, json=invalid_distribution()),
               httpx.Response(200, json=response_for())]
    calls = []

    def provider(request):
        calls.append(request)
        return replies[len(calls) - 1]

    result = asyncio.run(scripted(runtime, provider).jev_review(**args()))
    # One retried request: one attempt (one jev_requests row) but two receipts.
    assert result.status == "RECORDED" and len(calls) == 2

    now = datetime.now(UTC)
    assert runtime.calls_today(now) == {"attempts": 1, "receipts": 2}
    # Comfortably past local NY midnight of the next day under any DST shift: the rows
    # made "now" no longer count.
    assert runtime.calls_today(now + timedelta(hours=24)) == {"attempts": 0, "receipts": 0}
