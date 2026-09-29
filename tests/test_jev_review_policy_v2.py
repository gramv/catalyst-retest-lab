"""JEV_LIVE_REVIEW_POLICY_V2 (migration 025, owner ruling 2026-09-28) end to end.

V2 is V1 with an 8-second question and batch (per-attempt) budget inside the unchanged 10-second
overall deadline. On the suite's disposable /tmp cluster (every migration, 025 included) and on
a disposable schema-24 ledger, with mock provider transports only: its own runtime scope and
stored policy, 8-second permits, an attempt that takes 5 s answered under V2 and timed out under
V1, the worker's database waits, the research intake and worker under V2, and V1 unchanged.
No provider, broker, Railway or owner ledger.
"""

import asyncio
import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab import ledger_ops, localdb
from catalyst_lab.jev_store import JevStore
from catalyst_lab.repository import Repository
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, APPROVED_GATE1_V2, Gate1Inputs
from catalyst_lab.review_worker import WorkerJevStore
from tests.test_localdb_guard import audit_head, disposable_root, migrations_through
from tests.test_research_reports import reply, report_packet
from tests.test_review_worker import (
    args,
    isolate_prior_pending_fixtures,  # noqa: F401  (autouse: closes earlier pending items)
    reviewer,
    runtime,
    worker,
)
from tests.test_selection_topk_v2 import function_source

MIGRATIONS = Path(localdb.__file__).with_name("migrations")
MIGRATION = MIGRATIONS / "025_jev_review_policy_v2.sql"
V1_WORKER_MIGRATION = MIGRATIONS / "011_review_worker.sql"
V1_INTAKE_MIGRATION = MIGRATIONS / "010_research_reports.sql"
INTAKE_HEADER = "CREATE FUNCTION lab.store_research_report(body jsonb, gate1 jsonb)"


@pytest.fixture
def v1():
    return Gate1Inputs(copy.deepcopy(APPROVED_GATE1))


@pytest.fixture
def v2():
    return Gate1Inputs(copy.deepcopy(APPROVED_GATE1_V2))


@pytest.fixture
def store(cluster):
    return JevStore(localdb.connection_url(cluster, "catalyst_jev"))


def scope_id(slot, version=None):
    suffix = "" if version is None else ":" + version
    return hashlib.sha256(f"TYPESAFE:jev-1.13.0:{slot}{suffix}".encode()).hexdigest()


def canonical(policy):
    return json.dumps(policy, sort_keys=True, separators=(",", ":"))


def permit_seconds(cluster, request_id):
    """Each attempt's permit lifetime: its expiry minus the moment the permit was recorded."""
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        return [row[0] for row in conn.execute(
            """SELECT extract(epoch FROM p.expires_at - e.created_at)::float
            FROM lab.review_attempt_permits p JOIN lab.trade_events e ON e.seq = p.event_seq
            WHERE p.request_id = %s ORDER BY p.attempt""", (request_id,))]


def receipts(store, result):
    with store.connect() as conn:
        return [(r["attempt"], r["outcome"], r["latency_ms"]) for r in conn.execute(
            """SELECT attempt, outcome, latency_ms FROM lab.jev_receipts
            WHERE receipt_id = ANY(%s::uuid[]) ORDER BY attempt""",
            (list(result.receipt_ids),)).fetchall()]


# --- The runtime scope ------------------------------------------------------------------------


def test_v2_registers_its_own_scope_with_its_own_stored_policy(store, v1, v2):
    slot = "policy-v2-" + uuid4().hex
    first, second = runtime(store, v1, slot=slot), runtime(store, v2, slot=slot)
    assert first.scope_id == scope_id(slot)  # V1: migration 011's id, unchanged.
    assert second.scope_id == scope_id(slot, "JEV_LIVE_REVIEW_POLICY_V2")
    assert runtime(store, v2, slot=slot).scope_id == second.scope_id  # Idempotent.
    with store.connect() as conn:
        rows = {r["scope_id"]: r for r in conn.execute(
            "SELECT * FROM lab.review_runtime_scopes WHERE credential_slot=%s", (slot,))}
    assert set(rows) == {first.scope_id, second.scope_id}
    assert rows[first.scope_id]["policy_json"] == APPROVED_GATE1
    assert rows[second.scope_id]["policy_json"] == APPROVED_GATE1_V2
    assert second.policy.version == "JEV_LIVE_REVIEW_POLICY_V2"
    assert second.policy.deadline_seconds == first.policy.deadline_seconds == 10
    # Two scopes, two breakers: V2 starts closed whatever V1's breaker holds.
    assert second.state()["state"] == "CLOSED" and second.state()["epoch"] == 0


@pytest.mark.parametrize("change", [
    {"batch_timeout_seconds": 8},                 # V1's label with V2's budget
    {"question_timeout_seconds": 8},
    {"version": "JEV_LIVE_REVIEW_POLICY_V2"},     # V2's label with V1's budgets
    {"version": "JEV_LIVE_REVIEW_POLICY_V2", "batch_timeout_seconds": 8},
    {"version": "JEV_LIVE_REVIEW_POLICY_V2", "question_timeout_seconds": 8,
     "batch_timeout_seconds": 8, "overall_review_deadline_seconds": 20},
])
def test_the_database_registers_exactly_v1_or_v2_and_nothing_between(store, change):
    policy = {**APPROVED_GATE1, **change}
    with store.connect() as conn:
        with pytest.raises(psycopg.errors.RaiseException,
                           match="EXPLICIT_APPROVED_WORKER_POLICY_REQUIRED"):
            conn.execute("SELECT lab.register_review_scope(%s,%s)",
                         ("policy-v2-" + uuid4().hex, Jsonb(policy)))
    for slot in ("Upper-case", "has:colon", "-leading", None):
        with store.connect() as conn:
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute("SELECT lab.register_review_scope(%s,%s)",
                             (slot, Jsonb(APPROVED_GATE1_V2)))


# --- Each attempt's budget --------------------------------------------------------------------


def test_a_v2_permit_expires_eight_seconds_after_it_is_issued_and_v1_three(cluster, store, v1,
                                                                            v2):
    for gate, seconds in ((v2, 8), (v1, 3)):
        rt, values = runtime(store, gate), args()
        result = asyncio.run(reviewer(rt, lambda r: httpx.Response(200, json=reply()))
                             .jev_review(**values))
        assert result.status == "RECORDED"
        (lifetime,) = permit_seconds(cluster, values["request_id"])
        assert seconds - 0.5 <= lifetime <= seconds, (gate.values["version"], lifetime)


def test_an_attempt_of_five_seconds_answers_under_v2_and_times_out_under_v1(cluster, store, v1,
                                                                            v2):
    """The live stalls of 2026-09-28: under V1 the 3 s attempt budget cuts a 5 s answer off and
    the transport retry (once per review) asks again; under V2 the same answer arrives within
    the attempt's 8 s, inside the unchanged 10 s deadline."""
    def slow_first(calls):
        async def provider(request):
            calls.append(request.content)
            if len(calls) == 1:
                await asyncio.sleep(5)
            return httpx.Response(200, json=reply())
        return provider

    calls = []
    values = args()
    result = asyncio.run(reviewer(runtime(store, v2), slow_first(calls)).jev_review(**values))
    assert (result.status, len(calls)) == ("RECORDED", 1)
    [(attempt, outcome, latency)] = receipts(store, result)
    assert (attempt, outcome) == (1, "VALID") and 4900 <= latency < 6500
    assert permit_seconds(cluster, values["request_id"])[0] > 7.5

    calls = []
    values = args()
    result = asyncio.run(reviewer(runtime(store, v1), slow_first(calls)).jev_review(**values))
    assert (result.status, len(calls)) == ("RECORDED", 2) and calls[0] == calls[1]
    first, second = receipts(store, result)
    assert first[:2] == (1, "TRANSPORT_FAILURE") and 2800 <= first[2] < 3500
    assert second[:2] == (2, "VALID")


def test_the_workers_database_waits_follow_v2s_budget(cluster, v2):
    bounded = WorkerJevStore(localdb.connection_url(cluster, "catalyst_jev"), v2)
    assert bounded.connection_seconds == 8
    with bounded.connect() as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "8s"
        assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "8s"
        assert conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[
            "idle_in_transaction_session_timeout"] == "10s"
        conn.execute("SELECT pg_sleep(3.5)")  # Past V1's 3 s bound, inside V2's 8 s.


# --- The research intake and the worker -------------------------------------------------------


def test_the_research_intake_and_the_worker_run_end_to_end_under_v2(cluster, v2):
    reports = ResearchReports(localdb.connection_url(cluster, "catalyst_review"), v2)
    packet = report_packet(count=2)
    assert reports.submit(packet)["accepted"] is True
    calls = []
    w = worker(cluster, v2, lambda r: calls.append(1) or httpx.Response(200, json=reply()))
    assert w.runtime.scope_id == scope_id(w.settings.credential_slot,
                                          "JEV_LIVE_REVIEW_POLICY_V2")
    asyncio.run(w.tick())
    assert reports.report(packet["report_id"])["counts"] == {"SELECTED": 2} and len(calls) == 2
    item_ids = [str(item["item_id"]) for item in reports.report(packet["report_id"])["items"]]
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        requests = conn.execute(
            """SELECT q.evidence_identity->>'reliability_policy_version',
                q.evidence_identity->>'runtime_scope', p.scope_id,
                extract(epoch FROM p.expires_at - e.created_at)::float
            FROM lab.jev_requests q JOIN lab.review_attempt_permits p USING(request_id)
            JOIN lab.trade_events e ON e.seq = p.event_seq
            WHERE q.evidence_identity->>'research_item_id' = ANY(%s)""", (item_ids,)).fetchall()
    assert len(requests) == 2
    for version, scope, permit_scope, lifetime in requests:
        assert version == "JEV_LIVE_REVIEW_POLICY_V2"
        assert scope == permit_scope == w.runtime.scope_id
        assert 7.5 <= lifetime <= 8  # The request's 10 s deadline is later than the budget.


@pytest.mark.parametrize("change", [
    {"overall_review_deadline_seconds": 20},
    {"evidence_max_age_seconds": 120},
    {"context_max_age_seconds": 30},
    {"version": "JEV_LIVE_REVIEW_POLICY_V3"},
])
def test_the_intake_takes_v2_only_with_v1s_report_timing(cluster, v2, change):
    """V2 is accepted with V1's deadline and ages exactly. The intake's SQL, called directly as
    its service role (the Python policy check would refuse these first), refuses any other
    timing under V2's label, as it does under V1's."""
    reports = ResearchReports(localdb.connection_url(cluster, "catalyst_review"), v2)
    result = reports.submit(report_packet())
    assert result["accepted"] is True and result["authorizes_entry"] is False
    with psycopg.connect(localdb.connection_url(cluster, "catalyst_review")) as conn:
        with pytest.raises(psycopg.errors.RaiseException,
                           match="EXPLICIT_APPROVED_REPORT_TIMING_REQUIRED"):
            conn.execute("SELECT lab.store_research_report(%s,%s)",
                         (Jsonb(report_packet()), Jsonb({**APPROVED_GATE1_V2, **change})))


# --- Migration 025 itself ---------------------------------------------------------------------


def test_025_keeps_v1_byte_for_byte_and_adds_exactly_v2(cluster):
    text = MIGRATION.read_text()
    literals = re.findall(r"\$(policy|policy_v2)\$(.*?)\$\1\$", text)
    assert [tag for tag, _ in literals] == ["policy", "policy_v2"]
    v1_011 = re.search(r"\$policy\$(.*?)\$policy\$", V1_WORKER_MIGRATION.read_text()).group(1)
    assert literals[0][1] == v1_011 == canonical(APPROVED_GATE1)
    assert literals[1][1] == canonical(APPROVED_GATE1_V2)
    assert "public.digest('TYPESAFE:jev-1.13.0:'||slot,'sha256')" in text  # 011's V1 id.
    body = "\n".join(line.split("--")[0] for line in text.splitlines())
    statements = [s.strip().upper() for s in body.split(";")]
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE"))]
    assert writes == ["INSERT INTO LAB.SCHEMA_MIGRATIONS(VERSION) VALUES(25)"]
    assert "CREATE ROLE" not in body.upper() and "PASSWORD" not in body.upper()
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        stored = conn.execute("""SELECT proname::text, prosrc, prosecdef, proconfig,
            pg_get_userbyid(proowner)::text AS owner,
            has_function_privilege('catalyst_review', oid, 'EXECUTE') AS review,
            has_function_privilege('catalyst_jev', oid, 'EXECUTE') AS jev,
            has_function_privilege('catalyst_app', oid, 'EXECUTE') AS app
            FROM pg_proc WHERE pronamespace='lab'::regnamespace AND proname IN
            ('store_research_report','store_research_report_v1','register_review_scope')
            """).fetchall()
    rows = {row[0]: row for row in stored}
    # Migration 010's intake is stored unchanged under its new name, reachable only through
    # the entry point; every function stays lab_owner's, SECURITY DEFINER with a fixed path.
    assert rows["store_research_report_v1"][1] == function_source(V1_INTAKE_MIGRATION,
                                                                   INTAKE_HEADER)
    assert rows["store_research_report"][1] == function_source(MIGRATION, INTAKE_HEADER)
    for name, row in rows.items():
        assert row[2] is True and row[3] == ["search_path=pg_catalog, lab"], name
        assert row[4] == "lab_owner" and row[7] is False, name
    assert (rows["store_research_report"][5], rows["store_research_report_v1"][5]) == (
        True, False)
    assert rows["register_review_scope"][6] is True and rows["register_review_scope"][5] is False
    assert not rows["store_research_report"][6] and not rows["store_research_report_v1"][6]


def test_migration_025_on_a_populated_schema24_ledger_changes_only_its_functions():
    """Applied to a schema-24 ledger holding a V1 scope and audit history: the audit head, every
    table's rows and the V1 scope stay exactly as they were; only schema_migrations gains 25."""
    with disposable_root("ledger") as root, tempfile.TemporaryDirectory() as partial, \
            pytest.MonkeyPatch.context() as patch:
        patch.setattr(localdb, "MIGRATIONS", migrations_through(Path(partial) / "m", 24))
        localdb.start(root)
        with Repository(localdb.connection_url(root)).connect() as conn:
            for index in range(3):
                Repository.append_event(conn, "SYSTEM_EVENT", {"kind": "LAB_FIXTURE",
                                                               "index": index})
        slot = "policy-v2-" + uuid4().hex
        jev_url = localdb.connection_url(root, "catalyst_jev")
        with psycopg.connect(jev_url) as conn:
            v1_scope = conn.execute("SELECT lab.register_review_scope(%s,%s)",
                                    (slot, Jsonb(APPROVED_GATE1))).fetchone()[0]
            with pytest.raises(psycopg.errors.RaiseException):  # Schema 24 knows no V2.
                conn.execute("SELECT lab.register_review_scope(%s,%s)",
                             (slot, Jsonb(APPROVED_GATE1_V2)))
        before = audit_head(root)
        owner_url = localdb.connection_url(root, "lab_owner")
        with psycopg.connect(owner_url) as conn:
            tables = conn.execute(ledger_ops.TABLES).fetchall()

            def counts():
                return {t: conn.execute(f'SELECT count(*) FROM "{t[0]}"."{t[1]}"').fetchone()[0]
                        for t in tables}

            scopes = conn.execute("SELECT to_jsonb(s) FROM lab.review_runtime_scopes s"
                                  ).fetchall()
            counted = counts()
            conn.execute(MIGRATION.read_text())
            conn.commit()
            changed = {t: n for t, n in counts().items() if counted[t] != n}
            assert conn.execute("SELECT to_jsonb(s) FROM lab.review_runtime_scopes s"
                                ).fetchall() == scopes
            version = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()
        assert audit_head(root) == before and version == (25,)
        assert changed == {("lab", "schema_migrations"):
                           counted[("lab", "schema_migrations")] + 1}
        with psycopg.connect(jev_url) as conn:  # V1 resolves to its old scope; V2 now exists.
            assert conn.execute("SELECT lab.register_review_scope(%s,%s)",
                                (slot, Jsonb(APPROVED_GATE1))).fetchone()[0] == v1_scope
            assert conn.execute("SELECT lab.register_review_scope(%s,%s)",
                                (slot, Jsonb(APPROVED_GATE1_V2))).fetchone()[0] == scope_id(
                slot, "JEV_LIVE_REVIEW_POLICY_V2")
