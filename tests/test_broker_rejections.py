"""Classified broker refusals (plan 2.3) for both transports. Mock HTTP and disposable DBs."""

import json

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.paper_execution import (
    BrokerMutationRejected,
    RiskAuthorizedPaperClient,
    classify_rejection,
)
from catalyst_lab.risk import RiskEngine, RiskPolicy
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_broker import FixtureGate, gateway
from tests.test_managed_execution import ManagedVenue, observation, packet
from tests.test_risk import candidate_factory as candidate_factory
from tests.test_risk import risk_setup as risk_setup

MARGIN = {"code": 40310000, "message": "insufficient buying power"}
QTY = {"code": 40310000, "message": "insufficient qty available for order (requested: 3, "
       "available: 1)"}
CASES = [
    (403, MARGIN, "BROKER_MARGIN_REJECTED"),
    (403, QTY, "BROKER_QTY_UNAVAILABLE"),
    (422, {"code": 40010001, "message": "insufficient qty available"}, "BROKER_QTY_UNAVAILABLE"),
    (401, {"code": 40110000, "message": "request is not authorized"}, "BROKER_AUTH_REJECTED"),
    (422, {"code": 42210000, "message": "invalid limit_price"}, "BROKER_INVALID_REQUEST"),
    (403, {"code": 40310000, "message": "trading is suspended"}, "BROKER_REJECTED"),
    (400, {"message": "insufficient buying power"}, "BROKER_REJECTED"),  # No forbidden code.
    (404, None, "BROKER_REJECTED"),
]


def respond(status, body):
    if body is None:
        return httpx.Response(status, text="<html>private upstream detail</html>")
    return httpx.Response(status, json=body)


@pytest.mark.parametrize("status,body,reason", CASES)
def test_managed_transport_classifies_the_sanitized_broker_body(status, body, reason):
    broker, gate = gateway(lambda request: respond(status, body))
    decision = gate.grant("POST", "/v2/orders", {})
    with pytest.raises(BrokerMutationRejected) as caught:
        broker.mutate("POST", "/v2/orders", {}, decision)
    error = caught.value
    assert str(error) == error.reason == reason
    assert error.evidence["http_status"] == status
    assert set(error.evidence) == {"http_status", "code", "message"}
    if body is None:
        assert error.evidence["code"] is None and error.evidence["message"] is None
    else:
        assert error.evidence["code"] == body.get("code")
        assert error.evidence["message"] == body["message"]


@pytest.mark.parametrize("status,body,reason", CASES)
def test_frozen_transport_classifies_identically(status, body, reason):
    gate = FixtureGate()
    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PK" + "B" * 20, "fixture-secret-not-a-real-key"), gate,
        transport=httpx.MockTransport(lambda request: respond(status, body)),
    )
    try:
        grant = gate.grant("POST", "/v2/orders", {"symbol": "SPY"})
        with pytest.raises(BrokerMutationRejected) as caught:
            client.submit_bracket({"symbol": "SPY"}, risk_decision_id=grant)
        assert str(caught.value) == reason
    finally:
        client.close()


def test_evidence_is_truncated_and_never_carries_credentials():
    message = ("insufficient buying power; APCA-API-KEY-ID: PKABCDEFGHIJKLMNOPQRST "
               "secret=abcdefghijklmnopqrstuvwxyz0123456789 Bearer tok.en.value\n" + "x" * 500)
    broker, gate = gateway(lambda request: httpx.Response(
        403, json={"code": 40310000, "message": message}))
    decision = gate.grant("POST", "/v2/orders", {})
    with pytest.raises(BrokerMutationRejected) as caught:
        broker.mutate("POST", "/v2/orders", {}, decision)
    evidence = caught.value.evidence
    assert str(caught.value) == "BROKER_MARGIN_REJECTED"
    assert len(evidence["message"]) <= 200 and "\n" not in evidence["message"]
    for secret in ("PKABCDEFGHIJKLMNOPQRST", "abcdefghijklmnopqrstuvwxyz0123456789",
                   "tok.en.value"):
        assert secret not in json.dumps(evidence)
    assert "[REDACTED]" in evidence["message"]


def test_classification_order_is_fixed():
    assert classify_rejection(403, 40310000, "Insufficient Buying Power") == \
        "BROKER_MARGIN_REJECTED"
    assert classify_rejection(401, 40310000, "insufficient buying power") == \
        "BROKER_MARGIN_REJECTED"  # The margin code and message win over the status.
    assert classify_rejection(401, None, None) == "BROKER_AUTH_REJECTED"
    assert classify_rejection(422, None, "") == "BROKER_INVALID_REQUEST"


def test_frozen_dispatch_records_the_classified_reason_and_evidence(er, risk_setup,
                                                                   candidate_factory):
    from catalyst_lab.risk_dispatch import RiskDispatcher
    from tests.fake_paper_broker import FakePaperBroker

    repo, _, _ = risk_setup
    fake = FakePaperBroker()

    def handle(request):
        if request.method == "POST":
            fake.calls.append(("POST", request.url.path, "refused"))
            return httpx.Response(403, json=MARGIN)
        return fake.handle(request)

    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        AuthorizationGate(repo, clock=lambda: fake.now), transport=httpx.MockTransport(handle),
    )
    try:
        engine = RiskEngine(repo, client, RiskPolicy(), ready=lambda: True,
                            clock=lambda: fake.now)
        cid = candidate_factory()
        decision = RiskDispatcher(engine).enter(cid)
        assert decision["decision"] == "APPROVED"
        candidate = er.get_candidate(cid)
        assert candidate["state"] == "BROKER_REJECTED"
        with er.connect() as conn:
            result = conn.execute(
                "SELECT * FROM lab.current_authorization_results WHERE risk_decision_id=%s",
                (decision["risk_decision_id"],),
            ).fetchone()
            reserved = conn.execute("SELECT 1 FROM lab.active_reservations").fetchone()
        assert result["outcome"] == "REJECTED"
        assert result["payload_json"]["reason"] == "BROKER_MARGIN_REJECTED"
        assert result["payload_json"]["evidence"] == {
            "http_status": 403, "code": 40310000, "message": "insufficient buying power"}
        assert reserved is None  # Nothing was sent, so nothing stays reserved.
    finally:
        client.close()


class RefusingVenue(ManagedVenue):
    """ManagedVenue that answers new buy orders with a configured broker refusal."""

    refusal = None  # (HTTP status, JSON body)

    def handle(self, request):
        if request.method == "POST" and self.refusal is not None:
            if json.loads(request.content).get("side") == "buy":
                self.calls.append((request.method, request.url.path, request.content))
                status, body = self.refusal
                return httpx.Response(status, json=body)
        return super().handle(request)


@pytest.fixture
def refusing(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = RefusingVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(risk, broker, policy=engineering_execution_policy(),
                              clock=lambda: venue.now, review_store=reviews)
    assert engine.reconcile()["clean"]
    yield engine, venue, reviews
    broker.close()


def managed_events(engine, kind):
    with engine.repo.connect() as conn:
        return [r["body"] for r in conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq", (kind,)
        ).fetchall()]


def test_managed_margin_refusal_forces_reconciliation_only(refusing):
    engine, venue, _ = refusing
    venue.refusal = (403, MARGIN)
    sid = engine.admit(packet(refusing, "BTC/USD"))
    decision = engine.observe_trigger(sid, observation(refusing))
    assert decision["outcome"] == "APPROVED"  # Our risk check passed; the broker refused.
    [rejected] = managed_events(engine, "BROKER_REJECTED")
    assert rejected["reason"] == "BROKER_MARGIN_REJECTED"
    assert managed_events(engine, "MARGIN_REJECTION_RECONCILE_REQUIRED") == [
        {"decision_id": str(decision["decision_id"]), "reason": "BROKER_MARGIN_REJECTED"}
    ]
    assert engine.reconciled_at is None  # Entries wait for the next clean reconciliation.
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.execution_halts").fetchone()  # No halt.
        assert not conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()
    assert engine.reconcile()["clean"] and engine.reconciled_at is not None


@pytest.mark.parametrize("refusal,reason", [((422, {"message": "invalid qty"}),
                                              "BROKER_INVALID_REQUEST"),
                                             ((403, QTY), "BROKER_QTY_UNAVAILABLE")])
def test_other_entry_refusals_do_not_force_reconciliation(refusing, refusal, reason):
    engine, venue, _ = refusing
    venue.refusal = refusal
    sid = engine.admit(packet(refusing, "BTC/USD"))
    engine.observe_trigger(sid, observation(refusing))
    assert [e["reason"] for e in managed_events(engine, "BROKER_REJECTED")] == [reason]
    assert not managed_events(engine, "MARGIN_REJECTION_RECONCILE_REQUIRED")
    assert engine.reconciled_at is not None


def test_protect_refusal_keeps_its_exit_reason(refusing):
    engine, venue, _ = refusing
    sid = engine.admit(packet(refusing, "BTC/USD"))
    engine.observe_trigger(sid, observation(refusing))
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    venue.reject_protection = True  # The venue answers the stop-limit with a 422.
    engine.manage(sid, observation(refusing))
    assert [e["reason"] for e in managed_events(engine, "BROKER_REJECTED")] == [
        "BROKER_INVALID_REQUEST"]
    assert engine._load(sid)[1]["exit_requested"] == "PROTECTION_REJECTED"
