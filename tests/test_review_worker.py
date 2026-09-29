import asyncio
import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import JEV_MODEL, SKEPTIC
from catalyst_lab.jev_review import JevReviewer
from catalyst_lab.jev_store import JevStore
from catalyst_lab.repository import json_safe
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs, ReviewSettings
from catalyst_lab.review_runtime import (
    HEALTH_QUESTIONS,
    ClockSample,
    Gate1Runtime,
    database_clock_sample,
)
from catalyst_lab.review_service import create_review_app
from catalyst_lab.review_worker import ReviewWorker, WorkerJevStore, WorkerSettings
from tests.test_jev_review import FIXTURE_KEY
from tests.test_research_reports import reply, report_packet, trading_counts
from tests.test_review_storage import READ, WRITE


@pytest.fixture(autouse=True)
def isolate_prior_pending_fixtures(cluster):
    # Shared disposable cluster: close leftovers from earlier failure-case fixtures with
    # new annotated rows. Never erase ledger history or touch a runtime database.
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        conn.execute("""INSERT INTO lab.research_outcomes
          (item_id,policy_id,disposition,reason,answers_json)
          SELECT i.item_id,'JEV_SKEPTIC_RESEARCH_TEST_V1','NEEDS_REVIEW','PRIOR_FIXTURE_ENDED','{}'
          FROM lab.research_report_items i WHERE NOT EXISTS
           (SELECT 1 FROM lab.research_outcomes o WHERE o.item_id=i.item_id)""")


@pytest.fixture
def gate():
    return Gate1Inputs(copy.deepcopy(APPROVED_GATE1))


@pytest.fixture
def store(cluster):
    return JevStore(localdb.connection_url(cluster, "catalyst_jev"))


@pytest.fixture
def reports(cluster, gate):
    return ResearchReports(localdb.connection_url(cluster, "catalyst_review"), gate)


def fresh_clock():
    return ClockSample(0, 0, datetime.now(UTC))


def test_worker_database_waits_are_bounded_by_explicit_policy(cluster, gate):
    bounded = WorkerJevStore(localdb.connection_url(cluster, "catalyst_jev"), gate)
    with bounded.connect() as conn:
        assert conn.execute("SHOW timezone").fetchone()["TimeZone"] == "UTC"
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "3s"
        assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "3s"
        assert (
            conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[
                "idle_in_transaction_session_timeout"
            ]
            == "10s"
        )
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(4)")


def test_private_review_screen_and_market_index(cluster, reports, gate):
    settings = ReviewSettings(
        reports.database_url, WRITE, READ, "local", gate, "MUSE_JEV_ACTIVE_V1"
    )
    data = report_packet("INDIA", 1)
    reports.submit(data)
    with TestClient(create_review_app(settings, credential_provider=lambda: FIXTURE_KEY)) as api:
        shell = api.get("/")
        assert shell.status_code == 200
        assert data["report_id"] not in shell.text and READ not in shell.text
        assert "no-store" == shell.headers["cache-control"]
        assert "frame-ancestors 'none'" in shell.headers["content-security-policy"]
        assert api.get("/review-assets/review.js").status_code == 200
        assert api.get("/review-assets/index.html").status_code == 404
        assert api.get("/api/v1/research-reports?market=INDIA").status_code == 401
        assert (
            api.get(
                "/api/v1/research-reports?market=INDIA",
                headers={"Authorization": "Bearer " + WRITE},
            ).status_code
            == 401
        )
        result = api.get(
            "/api/v1/research-reports?market=INDIA", headers={"Authorization": "Bearer " + READ}
        )
        assert result.status_code == 200 and result.json()["authorizes_entry"] is False
        assert any(r["report_id"] == data["report_id"] for r in result.json()["reports"])
        assert all(r["market"] == "INDIA" for r in result.json()["reports"])
        assert (
            api.get(
                "/api/v1/research-reports?market=INVALID",
                headers={"Authorization": "Bearer " + READ},
            ).status_code
            == 422
        )


def runtime(store, gate, *, slot=None, health=fresh_clock):
    rt = Gate1Runtime(
        store,
        gate,
        credential_slot=slot or "test-" + uuid4().hex,
        worker_id=uuid4(),
        clock_health=health,
    )
    rt.heartbeat("RUNNING")
    return rt


def reviewer(rt, handler, *, jitter=lambda: 0):
    return JevReviewer(
        rt.store,
        rt.policy,
        runtime=rt,
        key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(handler),
        jitter=jitter,
    )


def args():
    return dict(
        request_id=uuid4(),
        identity={"diagnostic_id": str(uuid4())},
        state={"source": "Unrelated engineering health fixture", "sample": uuid4().hex},
        question_set=SKEPTIC,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
        purpose="ENGINEERING_TEST",
    )


def health_reply():
    return {
        "model": JEV_MODEL,
        "answers": {
            "diagnostic": {
                "type": "choice",
                "choice": "YES",
                "confidence": 0.9,
                "probabilities": {
                    k: float(k == "YES")
                    for k in HEALTH_QUESTIONS.questions["diagnostic"]["criteria"]
                },
            }
        },
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def append_clock_fixture(cluster, rt, **fields):
    # Virtual time state only in disposable DB: new audited rows, never UPDATE/DELETE.
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        state = rt.state() | fields
        conn.execute(
            "INSERT INTO lab.review_breaker_events(scope_id,state_json,reason) "
            "VALUES(%s,%s,'VIRTUAL_TIME_FIXTURE')",
            (rt.scope_id, Jsonb(json_safe(state))),
        )


def worker(cluster, gate, handler, *, slot=None, health=fresh_clock, max_inflight=4):
    return ReviewWorker(
        WorkerSettings(
            localdb.connection_url(cluster, "catalyst_jev"),
            gate,
            slot or "test-" + uuid4().hex,
            max_inflight,
            0.01,
        ),
        key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(handler),
        clock_health=health,
    )


@pytest.mark.parametrize("status", [429, 529])
def test_three_failures_trip_shared_durable_breaker(store, gate, status):
    rt, calls = runtime(store, gate), []

    def provider(req):
        calls.append(req.content)
        return httpx.Response(status, json={"error": "fixture overload"})

    result = asyncio.run(reviewer(rt, provider).jev_review(**args()))
    assert len(calls) == 3 and len(set(calls)) == 1
    assert len(result.receipt_ids) == 3 and result.status == "NEEDS_REVIEW"
    state = rt.state()
    assert state["state"] == "OPEN" and state["failures"] == 3
    assert (
        28
        <= (datetime.fromisoformat(state["blocked_until"]) - datetime.now(UTC)).total_seconds()
        <= 30
    )
    # Reopen the same durable scope using its non-secret configured slot.
    with store.connect() as conn:
        slot = conn.execute(
            "SELECT credential_slot FROM lab.review_runtime_scopes WHERE scope_id=%s",
            (rt.scope_id,),
        ).fetchone()["credential_slot"]
    restarted = runtime(store, gate, slot=slot)
    blocked = asyncio.run(reviewer(restarted, provider).jev_review(**args()))
    assert len(calls) == 3 and blocked.reason == "CIRCUIT_OPEN"


@pytest.mark.parametrize("status", [401, 422, 500, 302])
def test_only_429_and_529_retry(store, gate, status):
    rt, calls = runtime(store, gate), []
    result = asyncio.run(
        reviewer(rt, lambda req: calls.append(1) or httpx.Response(status)).jev_review(**args())
    )
    assert len(calls) == 1 and result.status == "NEEDS_REVIEW"
    assert rt.state()["failures"] == 1


def test_an_invalid_distribution_is_retried_once_under_the_gate1_runtime(store, gate):
    from tests.test_jev_review import invalid_distribution, response_for

    rt, calls = runtime(store, gate), []
    replies = [httpx.Response(200, json=invalid_distribution()),
               httpx.Response(200, json=response_for())]

    def provider(req):
        calls.append(req.content)
        return replies[len(calls) - 1]

    result = asyncio.run(reviewer(rt, provider).jev_review(**args()))
    # The second attempt holds its own durable permit and receipt; the breaker only counts.
    assert (result.status, len(result.receipt_ids), len(calls)) == ("RECORDED", 2, 2)
    assert calls[0] == calls[1] and rt.state()["state"] == "CLOSED"
    again = []

    def invalid(req):
        again.append(req.content)
        return httpx.Response(200, json=invalid_distribution())

    failed = asyncio.run(reviewer(runtime(store, gate), invalid).jev_review(**args()))
    assert (failed.status, failed.reason, len(failed.receipt_ids), len(again)) == (
        "NEEDS_REVIEW", "INVALID_PROVIDER_RESPONSE", 2, 2)  # One further attempt, not three.


@pytest.mark.parametrize(
    "attempt,jitter,expected", [(1, 0, 0.125), (1, 1, 0.25), (2, 0, 0.25), (2, 1, 0.5), (8, 1, 2)]
)
def test_exact_equal_jitter_and_cap(store, gate, attempt, jitter, expected):
    rt = runtime(store, gate)
    assert rt.retry_delay(attempt, jitter, None, datetime.now(UTC), 10) == (expected, None)


def test_retry_after_minimum_abandons_instead_of_overrunning(store, gate):
    rt, calls = runtime(store, gate), []
    result = asyncio.run(
        reviewer(
            rt, lambda req: calls.append(1) or httpx.Response(529, headers={"Retry-After": "3"})
        ).jev_review(**args())
    )
    assert len(calls) == 1 and result.reason == "RETRY_AFTER_EXCEEDS_BUDGET"
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT reason FROM lab.jev_control_events WHERE request_id=%s",
                (result.request_id,),
            ).fetchone()["reason"]
            == "RETRY_AFTER_EXCEEDS_BUDGET"
        )
    assert rt.retry_delay(1, 0, "2", datetime.now(UTC), 10)[0] == 2
    assert rt.retry_delay(1, 0, "nonsense", datetime.now(UTC), 10) == (0.125, "INVALID_RETRY_AFTER")


@pytest.mark.parametrize(
    "sample",
    [
        None,
        ClockSample(251, 0, datetime.now(UTC)),
        ClockSample(float("nan"), 0, datetime.now(UTC)),
        ClockSample(0, 0, datetime.now(UTC) - timedelta(hours=1)),
    ],
)
def test_unknown_bad_or_stale_clock_never_calls_provider(store, gate, sample):
    rt = runtime(store, gate, health=lambda: sample)
    result = asyncio.run(
        reviewer(rt, lambda req: pytest.fail("Clock must gate all requests")).jev_review(**args())
    )
    assert result.reason == "CLOCK_UNHEALTHY" and rt.state()["failures"] == 0


def test_real_app_db_clock_measurement(store, gate):
    sample = database_clock_sample(store)
    assert isinstance(sample, ClockSample)
    rt = runtime(store, gate, health=lambda: database_clock_sample(store))
    assert rt.healthy_clock(datetime.now(UTC))


def test_single_probe_then_two_valid_recoveries(store, gate, cluster):
    rt = runtime(store, gate)
    asyncio.run(reviewer(rt, lambda r: httpx.Response(529)).jev_review(**args()))
    append_clock_fixture(cluster, rt, blocked_until=datetime.now(UTC) - timedelta(seconds=1))
    calls = []

    async def healthy(req):
        calls.append(1)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=health_reply())

    r = reviewer(rt, healthy)

    async def simultaneous():
        return await asyncio.gather(rt.probe(r, datetime.now(UTC)), rt.probe(r, datetime.now(UTC)))

    outcomes = asyncio.run(simultaneous())
    assert len(calls) == 1
    assert sum(o.reason == "CIRCUIT_OPEN" for o in outcomes) == 1
    assert rt.state()["state"] == "HALF_OPEN" and rt.state()["successes"] == 1
    asyncio.run(rt.probe(r, datetime.now(UTC)))
    assert len(calls) == 1  # required spacing, no hidden busy probing
    append_clock_fixture(cluster, rt, next_probe_at=datetime.now(UTC) - timedelta(seconds=1))
    asyncio.run(rt.probe(r, datetime.now(UTC)))
    assert len(calls) == 2 and rt.state()["state"] == "CLOSED"


def test_failed_probe_reopens_and_unknown_probe_lease_expires(store, gate, cluster):
    rt = runtime(store, gate)
    asyncio.run(reviewer(rt, lambda r: httpx.Response(529)).jev_review(**args()))
    append_clock_fixture(cluster, rt, blocked_until=datetime.now(UTC) - timedelta(seconds=1))
    result = asyncio.run(rt.probe(reviewer(rt, lambda r: httpx.Response(529)), datetime.now(UTC)))
    assert len(result.receipt_ids) == 1 and rt.state()["state"] == "OPEN"
    append_clock_fixture(
        cluster,
        rt,
        state="HALF_OPEN",
        probe_id=str(uuid4()),
        probe_until=datetime.now(UTC) - timedelta(seconds=1),
        blocked_until=datetime.now(UTC) - timedelta(seconds=30),
    )
    result = asyncio.run(
        rt.probe(
            reviewer(rt, lambda r: pytest.fail("Expired lease must re-open cooldown")),
            datetime.now(UTC),
        )
    )
    assert result.reason == "CIRCUIT_OPEN" and rt.state()["state"] == "OPEN"


def test_timeout_bounds_each_attempt_and_is_retried_once(store, gate):
    """Each attempt stops at the 3 s budget; a timeout is retried once per review (2026-09-28),
    never a third time, even with max_attempts 3 and time left in the 10 s deadline."""
    rt, calls = runtime(store, gate), []

    async def slow(req):
        calls.append(1)
        await asyncio.sleep(4)
        return httpx.Response(200, json=reply())

    result = asyncio.run(reviewer(rt, slow).jev_review(**args()))
    assert len(calls) == 2 and result.reason == "PROVIDER_TRANSPORT_FAILURE"
    assert len(result.receipt_ids) == 2
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT attempt, outcome, latency_ms FROM lab.jev_receipts
            WHERE receipt_id = ANY(%s::uuid[]) ORDER BY attempt""", (list(result.receipt_ids),)
        ).fetchall()
    assert [(r["attempt"], r["outcome"]) for r in rows] == [
        (1, "TRANSPORT_FAILURE"), (2, "TRANSPORT_FAILURE")]
    assert all(2800 <= r["latency_ms"] < 3500 for r in rows)


def test_a_timeout_then_an_answer_is_recorded_under_its_own_permits(store, gate):
    """The first live maintenance reviews: a stalled call, then a prompt one."""
    rt, calls = runtime(store, gate), []

    async def stall_once(req):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(4)
        return httpx.Response(200, json=reply())

    values = args()
    result = asyncio.run(reviewer(rt, stall_once).jev_review(**values))
    assert len(calls) == 2 and (result.status, len(result.receipt_ids)) == ("RECORDED", 2)
    with store.connect() as conn:
        permits = conn.execute(
            """SELECT attempt FROM lab.review_attempt_permits WHERE request_id=%s
            ORDER BY attempt""", (values["request_id"],)).fetchall()
    assert [p["attempt"] for p in permits] == [1, 2]


def test_worker_reviews_and_restart_does_not_repeat_votes(cluster, reports, gate, repo):
    packet, calls = report_packet(count=8), []
    reports.submit(packet)
    before = trading_counts(repo)
    w = worker(cluster, gate, lambda r: calls.append(1) or httpx.Response(200, json=reply()))
    asyncio.run(w.tick())
    asyncio.run(w.tick())
    assert reports.report(packet["report_id"])["counts"] == {"SELECTED": 8}
    restarted = worker(
        cluster, gate, lambda r: pytest.fail("Durable outcomes must survive restart")
    )
    asyncio.run(restarted.tick())
    assert len(calls) == 8 and trading_counts(repo) == before
    assert verify_events(repo.export_events())["valid"]


def test_competing_workers_claim_one_effective_job(cluster, reports, gate):
    packet, calls = report_packet(), []
    reports.submit(packet)

    async def provider(req):
        calls.append(1)
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=reply())

    first, second = worker(cluster, gate, provider), worker(cluster, gate, provider)

    async def both():
        await asyncio.gather(first.tick(), second.tick())

    asyncio.run(both())
    assert len(calls) == 1 and reports.report(packet["report_id"])["counts"] == {"SELECTED": 1}


def test_unknown_inflight_restart_expires_without_new_vote(cluster, reports, gate):
    packet = report_packet()
    packet["valid_until"] = (datetime.now(UTC) + timedelta(milliseconds=400)).isoformat()
    reports.submit(packet)

    def crash(req):
        raise RuntimeError("Injected process interruption after request persisted")

    first = worker(cluster, gate, crash)
    with pytest.raises(RuntimeError):
        asyncio.run(first.tick())

    async def recover():
        await asyncio.sleep(0.45)
        await worker(
            cluster, gate, lambda r: pytest.fail("Unknown must never be reevaluated")
        ).tick()

    asyncio.run(recover())
    item = reports.report(packet["report_id"])["items"][0]
    assert item["recorded_disposition"] == "NEEDS_REVIEW"
    assert item["reason"] == "REVIEW_DEADLINE_EXCEEDED"


def test_operator_halt_is_durable_and_unavailable_to_muse(cluster, reports, gate):
    packet = report_packet()
    reports.submit(packet)
    for role in ("catalyst_review", "catalyst_jev", "catalyst_app"):
        with (
            psycopg.connect(localdb.connection_url(cluster, role)) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("SELECT lab.review_operator_halt(false)")
    try:
        with psycopg.connect(localdb.connection_url(cluster, "catalyst_review_operator")) as conn:
            conn.execute("SELECT lab.review_operator_halt(true)")
        w = worker(cluster, gate, lambda r: pytest.fail("Halt blocks provider calls"))
        assert asyncio.run(w.tick()) == []
        assert (
            next(
                row for row in reports.workers()["workers"] if row["worker_id"] == str(w.worker_id)
            )["status"]
            == "HALTED"
        )
    finally:
        with psycopg.connect(localdb.connection_url(cluster, "catalyst_review_operator")) as conn:
            conn.execute("SELECT lab.review_operator_halt(false)")


def test_worker_clock_failure_visible_and_does_not_claim(cluster, reports, gate):
    reports.submit(report_packet())
    w = worker(
        cluster, gate, lambda r: pytest.fail("Clock failure cannot review"), health=lambda: None
    )
    assert asyncio.run(w.tick()) == []
    row = next(r for r in reports.workers()["workers"] if r["worker_id"] == str(w.worker_id))
    assert row["status"] == "CLOCK_UNHEALTHY"


def test_worker_status_lapses_without_heartbeat(cluster, reports, gate):
    w = worker(cluster, gate, lambda r: httpx.Response(200, json=reply()))
    w.heartbeat()
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        conn.execute(
            "INSERT INTO lab.review_worker_events(worker_id,scope_id,status,created_at) "
            "VALUES(%s,%s,'RUNNING',clock_timestamp()-interval '16 seconds')",
            (w.worker_id, w.runtime.scope_id),
        )
    row = next(r for r in reports.workers()["workers"] if r["worker_id"] == str(w.worker_id))
    assert row["status"] == "DOWN"


def test_durable_output_cursor_and_authenticated_health(cluster, reports, gate):
    settings = ReviewSettings(
        reports.database_url, WRITE, READ, "local", gate, "MUSE_JEV_ACTIVE_V1"
    )
    with reports.connect() as conn:
        before = conn.execute(
            "SELECT coalesce(max(event_seq),0) AS seq FROM lab.research_output_events"
        ).fetchone()["seq"]
    packet = report_packet(count=2)
    reports.submit(packet)
    asyncio.run(worker(cluster, gate, lambda r: httpx.Response(200, json=reply())).tick())
    with TestClient(create_review_app(settings, credential_provider=lambda: FIXTURE_KEY)) as client:
        assert client.get("/api/v1/research-output").status_code == 401
        headers = {"Authorization": "Bearer " + READ}
        page1 = client.get(
            f"/api/v1/research-output?after_seq={before}&limit=1", headers=headers
        ).json()
        page2 = client.get(
            f"/api/v1/research-output?after_seq={page1['next_cursor']}", headers=headers
        ).json()
        assert len(page1["events"]) + len(page2["events"]) == 3
        assert page1["events"][0]["event_kind"] == "REPORT_RECEIVED"
        assert len({event["item_id"] for event in page2["events"]}) == 2
        assert not page1["authorizes_entry"]
        assert client.get("/api/v1/review-workers", headers=headers).status_code == 200
        assert client.post("/api/v1/resume", headers=headers).status_code == 404


def test_production_worker_never_accepts_missing_inputs(monkeypatch):
    for key in (
        "JEV_WORKER_DATABASE_URL",
        "JEV_REVIEW_POLICY_JSON",
        "JEV_CREDENTIAL_SLOT",
        "JEV_WORKER_MAX_INFLIGHT",
        "JEV_WORKER_POLL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="REQUIRED_WORKER_CONFIGURATION"):
        WorkerSettings.from_env()


def test_inflight_success_cannot_close_newly_opened_breaker(store, gate):
    rt = runtime(store, gate)

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def provider(request):
            if b"late-success" in request.content:
                started.set()
                await release.wait()
                return httpx.Response(200, json=reply())
            return httpx.Response(401)

        r = reviewer(rt, provider)
        first = args()
        first["state"]["case"] = "late-success"
        task = asyncio.create_task(r.jev_review(**first))
        await started.wait()
        for _ in range(3):
            await r.jev_review(**args())
        assert rt.state()["state"] == "OPEN"
        release.set()
        await task
        assert rt.state()["state"] == "OPEN"

    asyncio.run(exercise())


def test_operator_halt_during_response_prevents_new_selection(cluster, reports, gate):
    packet = report_packet()
    reports.submit(packet)

    def provider(request):
        with psycopg.connect(localdb.connection_url(cluster, "catalyst_review_operator")) as conn:
            conn.execute("SELECT lab.review_operator_halt(true)")
        return httpx.Response(200, json=reply())

    try:
        asyncio.run(worker(cluster, gate, provider).tick())
        item = reports.report(packet["report_id"])["items"][0]
        assert item["status"] == "NEEDS_REVIEW" and item["reason"] == "OPERATOR_HALTED"
        assert item["receipt_id"]
    finally:
        with psycopg.connect(localdb.connection_url(cluster, "catalyst_review_operator")) as conn:
            conn.execute("SELECT lab.review_operator_halt(false)")


def test_material_revision_is_a_durable_notification(cluster, reports, gate):
    packet = report_packet()
    reports.submit(packet)
    asyncio.run(worker(cluster, gate, lambda r: httpx.Response(200, json=reply())).tick())
    with reports.connect() as conn:
        cursor = conn.execute(
            "SELECT max(event_seq) AS seq FROM lab.research_output_events"
        ).fetchone()["seq"]
    packet["revision"] = 2
    packet["submission_id"] = str(uuid4())
    packet["items"][0]["sources"][0]["excerpt"] = "A correction supersedes the original source."
    reports.submit(packet)
    changes = reports.outputs(cursor, 100)
    assert len(changes["events"]) == 1
    assert changes["events"][0]["reason"] == "MATERIAL_REVISION_RECEIVED"
    assert changes["events"][0]["status"] == "REFRESH_REPORT"
    assert reports.report(packet["report_id"], 1)["counts"] == {"SUPERSEDED": 1}


@pytest.mark.parametrize(
    "table",
    [
        "review_runtime_scopes",
        "review_attempt_permits",
        "review_breaker_events",
        "review_worker_events",
        "review_job_events",
        "review_operator_events",
    ],
)
def test_worker_state_is_append_only_and_role_cannot_forge(cluster, store, gate, reports, table):
    packet = report_packet()
    reports.submit(packet)
    asyncio.run(worker(cluster, gate, lambda r: httpx.Response(200, json=reply())).tick())
    with psycopg.connect(localdb.connection_url(cluster, "catalyst_review_operator")) as conn:
        conn.execute("SELECT lab.review_operator_halt(false)")
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(f"DELETE FROM lab.{table}")
    with store.connect() as conn:
        assert not conn.execute(
            "SELECT has_table_privilege(current_user,%s,"
            "'INSERT,UPDATE,DELETE,TRUNCATE') AS permitted",
            ("lab." + table,),
        ).fetchone()["permitted"]


def test_candidate_cannot_pose_as_half_open_probe(store, gate):
    rt = runtime(store, gate)
    request = args()
    request["identity"]["health_probe"] = True
    with pytest.raises(psycopg.errors.RaiseException, match="SYNTHETIC_HEALTH_PROBE_REQUIRED"):
        asyncio.run(
            reviewer(
                rt, lambda r: pytest.fail("Only synthetic health template may probe")
            ).jev_review(**request)
        )


def test_worker_heartbeat_is_required_for_provider_attempt(store, gate, cluster):
    rt = runtime(store, gate)
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        conn.execute(
            """INSERT INTO lab.review_worker_events(worker_id,scope_id,status,created_at)
          VALUES(%s,%s,'RUNNING',clock_timestamp()-interval '16 seconds')""",
            (rt.worker_id, rt.scope_id),
        )
    result = asyncio.run(
        reviewer(rt, lambda r: pytest.fail("Heartbeat down must block call")).jev_review(**args())
    )
    assert result.reason == "WORKER_HEARTBEAT_DOWN"


def test_async_worker_run_stops_cleanly_and_records_health(cluster, gate, reports):
    packet = report_packet()
    reports.submit(packet)
    w = worker(cluster, gate, lambda r: httpx.Response(200, json=reply()))

    async def cycle():
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        for _ in range(100):
            if reports.report(packet["report_id"])["counts"] == {"SELECTED": 1}:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("worker did not process durable queue")
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(cycle())
    row = next(r for r in reports.workers()["workers"] if r["worker_id"] == str(w.worker_id))
    assert row["status"] == "STOPPED"
