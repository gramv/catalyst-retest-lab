"""End-to-end engineering proofs. Both external providers are fake; DB is real and disposable."""

import asyncio
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.api import create_app
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.config import Settings
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.jev_paper import COHORT, POLICY, JevPaperAdmission, revoke_pending_reviews
from catalyst_lab.jev_review import JevReviewer
from catalyst_lab.jev_store import JevStore
from catalyst_lab.market import NY, Observation, Session
from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
from catalyst_lab.reconciliation import Reconciler
from catalyst_lab.repository import json_safe
from catalyst_lab.research_reports import ResearchReports, review_report
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs
from catalyst_lab.review_runtime import ClockSample, Gate1Runtime
from catalyst_lab.risk import RiskEngine, RiskPolicy
from catalyst_lab.risk_runtime import RiskRuntime
from catalyst_lab.us_admission import USAdmissionEvidence, admission_profile
from catalyst_lab.watcher import Watcher
from tests.conftest import TOKEN
from tests.fake_paper_broker import FakePaperBroker
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_research_reports import reply, report_packet


@pytest.fixture
def setup(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    fake = FakePaperBroker()
    fake.now = datetime.now(UTC)
    day = fake.now.astimezone(NY).date()
    days = sorted(
        day - timedelta(days=i) for i in range(1, 40) if (day - timedelta(days=i)).weekday() < 5
    )[-20:]
    sessions = [
        Session.from_calendar({"date": d.isoformat(), "open": "00:00", "close": "23:59"})
        for d in [*days, day]
    ]

    def wire(request):
        if request.url.path == "/v2/calendar":
            return httpx.Response(
                200,
                json=[
                    {"date": s.session_date.isoformat(), "open": "00:00", "close": "23:59"}
                    for s in sessions
                    if request.url.params["start"]
                    <= s.session_date.isoformat()
                    <= request.url.params["end"]
                ],
            )
        if request.url.path == "/v2/stocks/bars":
            return httpx.Response(
                200,
                json={
                    "bars": {
                        request.url.params["symbols"]: [
                            {"t": s.opens.isoformat(), "v": 300000, "vw": 100}
                            for s in sessions[:-1]
                        ]
                    },
                    "next_page_token": None,
                },
            )
        return fake.handle(request)

    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        AuthorizationGate(risk, clock=lambda: fake.now),
        transport=httpx.MockTransport(wire),
    )
    engine = RiskEngine(risk, client, RiskPolicy(), ready=lambda: True, clock=lambda: fake.now)
    recon = Reconciler(
        risk, client, clock=lambda: fake.now, baseline_recorder=engine.capture_reconciled_baseline
    )
    assert recon.run_once()["clean"]
    provider = USAdmissionEvidence(
        er,
        client,
        admission_profile("US_PAPER_ADMISSION_TEST_V1"),
        ready=lambda: True,
        feed_healthy=lambda: True,
        clock=lambda: fake.now,
    )
    gate = Gate1Inputs(copy.deepcopy(APPROVED_GATE1))
    store = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    rt = Gate1Runtime(
        store,
        gate,
        credential_slot="fixture-" + uuid4().hex,
        worker_id=uuid4(),
        clock_health=lambda: ClockSample(0, 0, datetime.now(UTC)),
    )
    rt.heartbeat("RUNNING")
    reports = ResearchReports(
        er.database_url.replace("user=catalyst_app", "user=catalyst_review"), gate
    )
    reviewer = JevReviewer(
        store,
        rt.policy,
        runtime=rt,
        key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply())),
    )
    bridge = JevPaperAdmission(risk, provider, policy_id=POLICY, clock=lambda: fake.now)
    result = {
        "fake": fake,
        "engine": engine,
        "runtime": RiskRuntime(engine),
        "recon": recon,
        "provider": provider,
        "rt": rt,
        "reports": reports,
        "reviewer": reviewer,
        "bridge": bridge,
        "session": sessions[-1],
        "risk": risk,
    }
    yield result
    client.close()


def selected(setup, *, market="US_STOCKS", response=None):
    packet = report_packet(market)
    # Fake item is intentionally a plausible symbol; it never goes to an external broker.
    assert setup["reports"].submit(packet)["accepted"]
    if response:
        setup["reviewer"]._transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json=response)
        )
    result = asyncio.run(
        review_report(setup["reports"], setup["reviewer"], packet["report_id"], 1, max_inflight=1)
    )
    item = result["items"][0]
    setup["engine"].classify(item["symbol"], "FIXTURE_SECTOR", "FIXTURE_THEME", "LAB_FIXTURE")
    return packet, item


def arm_and_trigger(setup, item):
    enrolled = setup["bridge"].admit(item["item_id"])
    assert enrolled["outcome"] == "VALIDATED", enrolled
    cid = enrolled["candidate_id"]
    watcher = Watcher(setup["risk"], reconciliation_gate=lambda: True)
    session, now = setup["session"], setup["fake"].now
    watcher.tick({session.session_date: session}, now, {item["symbol"]})
    assert setup["risk"].get_candidate(cid)["state"] == "WATCHING"
    assert not setup["fake"].root_ids
    watcher.tick(
        {session.session_date: session},
        now,
        {item["symbol"]},
        Observation(
            "quote",
            item["symbol"],
            now.isoformat(),
            "iex",
            bid=D("99.99"),
            ask=D("100.01"),
        ),
    )
    watcher.tick(
        {session.session_date: session},
        now,
        {item["symbol"]},
        Observation(
            "trade",
            item["symbol"],
            now.isoformat(),
            "iex",
            price=D("100"),
            volume=D(1),
            trade_id="fixture-touch",
        ),
    )
    assert setup["risk"].get_candidate(cid)["state"] == "TRIGGER_CONFIRMED"
    return cid


def test_review_to_trigger_risk_fill_and_calendar_flatten(er, setup):
    packet, item = selected(setup)
    cid = arm_and_trigger(setup, item)
    fake, runtime = setup["fake"], setup["runtime"]
    decision = runtime.dispatcher.enter(cid)
    assert decision["decision"] == "APPROVED"
    assert decision["computed_qty"] == 86
    assert len(fake.root_ids) == 1
    entry = fake.orders[fake.root_ids[0]]
    assert entry["limit_price"] == "100.15" and entry["time_in_force"] == "day"
    fake.fill_entry(entry["id"])
    fake.drain(runtime.dispatcher)
    assert er.get_candidate(cid)["state"] in {"FILLED", "OPEN"}
    setup["rt"].heartbeat("STOPPED")
    revoke_pending_reviews(runtime)  # AI loss after fill cannot request an early model exit.
    assert fake.positions
    fake.now = setup["session"].flatten_time
    runtime.safety.calendar_exits(setup["session"])
    fake.drain(runtime.dispatcher)
    assert er.get_candidate(cid)["state"] == "CLOSED"
    assert not fake.positions
    assert setup["recon"].run_once()["clean"]
    readback = setup["reports"].report(packet["report_id"])["items"][0]["paper_execution"]
    assert readback["state"] == "CLOSED" and readback["cohort"] == COHORT
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.active_reservations").fetchone()
        assert not conn.execute("SELECT 1 FROM lab.strategy_trades").fetchone()
        fills = conn.execute("SELECT qty,price,side,record_purpose FROM lab.fills").fetchall()
    events = er.export_events()
    checkpoint = verify_events(events)
    assert checkpoint["valid"]
    proof = json_safe(
        {
            "scope": "DISPOSABLE_DB_FAKE_JEV_FAKE_PAPER",
            "cohort": COHORT,
            "candidate_id": cid,
            "fills": fills,
            "checkpoint": checkpoint,
            "broker_positions": fake.positions,
            "local_reservations": 0,
            "real_broker_orders": 0,
            "events": events,
        }
    )
    if os.environ.get("CATALYST_TEST_PROOF_PATH"):
        Path(os.environ["CATALYST_TEST_PROOF_PATH"]).write_text(json.dumps(proof, indent=2) + "\n")


def test_parallel_admission_is_one_candidate(er, setup):
    _, item = selected(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: setup["bridge"].admit(item["item_id"]), range(2)))
    assert results[0]["candidate_id"] == results[1]["candidate_id"]
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.candidates").fetchone()["n"] == 1


@pytest.mark.parametrize("market", ["CRYPTO", "INDIA"])
def test_other_markets_cannot_enter_us_bridge(er, setup, market):
    _, item = selected(setup, market=market)
    result = setup["bridge"].admit(item["item_id"])
    assert result["reason"] == "US_ENGINEERING_REVIEW_REQUIRED"
    assert not result["candidate_id"]


def test_worker_loss_before_risk_is_rejection(setup):
    _, item = selected(setup)
    cid = arm_and_trigger(setup, item)
    setup["rt"].heartbeat("STOPPED")
    decision = setup["runtime"].dispatcher.enter(cid)
    assert decision["reason"] == "WORKER_HEARTBEAT_DOWN"
    assert not setup["fake"].root_ids


def test_loss_between_decision_and_dispatch_sends_nothing(setup):
    _, item = selected(setup)
    cid = arm_and_trigger(setup, item)
    decision = setup["engine"].authorize_entry(cid)
    setup["rt"].heartbeat("STOPPED")
    setup["runtime"].dispatcher.dispatch(decision["risk_decision_id"])
    assert not setup["fake"].root_ids
    assert setup["risk"].get_candidate(cid)["state"] == "CANCELED"


def test_new_material_revision_invalidates_old_grant(setup):
    packet, item = selected(setup)
    cid = arm_and_trigger(setup, item)
    packet["revision"] = 2
    packet["submission_id"] = str(uuid4())
    packet["items"][0]["thesis"] = "Material source was withdrawn."
    assert setup["reports"].submit(packet)["accepted"]
    assert setup["engine"].authorize_entry(cid)["reason"] == "REVIEW_SUPERSEDED"


@pytest.mark.parametrize(
    "response", [reply(verdict="REJECT"), reply(priced="Insufficient evidence")]
)
def test_rejection_or_uncertainty_creates_no_candidate(setup, response):
    _, item = selected(setup, response=response)
    assert setup["bridge"].admit(item["item_id"])["reason"] == "JEV_SELECTION_REQUIRED"
    assert not setup["fake"].root_ids


def test_durable_operator_halt_prevents_selection_handoff(er, setup):
    _, item = selected(setup)
    with psycopg.connect(
        er.database_url.replace("user=catalyst_app", "user=catalyst_review_operator")
    ) as conn:
        conn.execute("SELECT lab.review_operator_halt(true)")
    assert setup["bridge"].admit(item["item_id"])["reason"] == "REVIEW_OPERATOR_HALTED"


def test_database_claim_guard_cannot_be_bypassed_by_skipping_python_gate(setup):
    _, item = selected(setup)
    cid = arm_and_trigger(setup, item)
    decision = setup["engine"].authorize_entry(cid)
    setup["rt"].heartbeat("STOPPED")
    with setup["risk"].connect() as conn:
        event = setup["risk"].append_event(conn, "SYSTEM_EVENT", {"kind": "LAB_FIXTURE_CLAIM"})
        with pytest.raises(psycopg.errors.RaiseException, match="JEV_ENTRY_REFUSED"):
            conn.execute(
                "INSERT INTO lab.authorization_claims(risk_decision_id,event_seq) VALUES(%s,%s)",
                (decision["risk_decision_id"], event["seq"]),
            )


def test_three_concurrent_jev_candidates_cannot_reserve_over_two_percent(setup):
    candidates = []
    for index in range(3):
        _, item = selected(setup)
        setup["engine"].classify(item["symbol"], f"SECTOR{index}", f"THEME{index}", "LAB_FIXTURE")
        candidates.append(arm_and_trigger(setup, item))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(setup["engine"].authorize_entry, candidates))
    assert sum(row["decision"] == "APPROVED" for row in results) == 2
    assert {r["reason"] for r in results if r["decision"] == "REJECTED"} == {
        "MAX_OPEN_PLANNED_RISK"
    }
    with setup["risk"].connect() as conn:
        assert (
            conn.execute("SELECT sum(budget) AS total FROM lab.active_reservations").fetchone()[
                "total"
            ]
            == 200
        )


def test_expired_selection_never_admitted(setup):
    packet = report_packet()
    packet["valid_until"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    assert setup["reports"].submit(packet)["accepted"]
    result = asyncio.run(
        review_report(setup["reports"], setup["reviewer"], packet["report_id"], 1, max_inflight=1)
    )
    item = result["items"][0]
    assert item["recorded_disposition"] == "SELECTED"
    time.sleep(1.05)
    assert setup["bridge"].admit(item["item_id"])["reason"] == "REVIEW_EXPIRED"


def test_tampered_receipt_cannot_admit(er, setup):
    _, item = selected(setup)
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=lab_owner")) as owner:
        owner.execute("ALTER TABLE lab.jev_receipts DISABLE TRIGGER immutable_rows")
        owner.execute(
            "UPDATE lab.jev_receipts SET latency_ms=latency_ms+1 WHERE receipt_id=%s",
            (item["receipt_id"],),
        )
        owner.execute("ALTER TABLE lab.jev_receipts ENABLE TRIGGER immutable_rows")
    assert setup["bridge"].admit(item["item_id"])["reason"] == "RECEIPT_INTEGRITY_FAILURE"


@pytest.mark.parametrize("table", ["jev_paper_admissions", "jev_paper_revocations"])
def test_new_ledgers_append_only_at_database_layer(setup, table):
    with setup["risk"].connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"DELETE FROM lab.{table}")


def test_cancel_fill_race_keeps_protection_or_flattens(setup):
    _, item = selected(setup)
    cid = arm_and_trigger(setup, item)
    runtime, fake = setup["runtime"], setup["fake"]
    runtime.dispatcher.enter(cid)
    fake.fill_on_cancel = True
    setup["rt"].heartbeat("STOPPED")
    revoke_pending_reviews(runtime)
    fake.drain(runtime.dispatcher)
    runtime.safety.process_exits()
    fake.drain(runtime.dispatcher)
    assert not fake.positions
    # Every cancellation/flatten remains risk-authorized, including after AI loss.
    assert all(cap is not None for method, path, cap in fake.calls if method != "GET")


def test_normal_candidate_without_risk_decision_stays_locked(setup):
    with pytest.raises(SubmissionDisabled):
        setup["engine"].client.submit_bracket({"qty": "1"}, risk_decision_id=uuid4())
    assert not setup["fake"].root_ids


@pytest.mark.parametrize("role", ["catalyst_app", "catalyst_jev", "catalyst_review"])
def test_research_roles_cannot_manufacture_execution_binding(er, role):
    with psycopg.connect(er.database_url.replace("user=catalyst_app", "user=" + role)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO lab.jev_paper_admissions DEFAULT VALUES")


def test_jev_mode_refuses_legacy_unreviewed_candidate_route(er):
    from fastapi.testclient import TestClient

    settings = Settings(
        er.database_url,
        TOKEN,
        read_only_market_data=True,
        risk_database_url=er.database_url.replace("user=catalyst_app", "user=catalyst_risk"),
        us_admission_policy="US_PAPER_ADMISSION_TEST_V1",
        jev_paper_policy=POLICY,
        jev_admission_poll_seconds=D(1),
    )
    app = create_app(
        settings, market_runtime=SimpleNamespace(validation_evidence=lambda c, n: None)
    )
    response = TestClient(app).post(
        "/api/v1/candidates", json={}, headers={"Authorization": "Bearer " + TOKEN}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "REVIEWED_RESEARCH_REPORT_REQUIRED"
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.candidates").fetchone()
