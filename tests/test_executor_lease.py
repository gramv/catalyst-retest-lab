import hashlib

import httpx
import pytest

from catalyst_lab import localdb
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.executor_lease import (
    LEGACY_EXECUTOR_LOCK,
    AccountExecutorLease,
    FencedAuthorizationGate,
)
from catalyst_lab.paper_execution import AuthorizedPaperTransport

IDENTITY = hashlib.sha256(b"fixture-paper-account").hexdigest()


def lease(cluster):
    return AccountExecutorLease(
        RiskRepository(localdb.connection_url(cluster, "catalyst_risk")), lambda: IDENTITY
    )


def test_second_executor_refused_and_new_process_can_take_over(cluster):
    first, second = lease(cluster), lease(cluster)
    first.acquire()
    try:
        with pytest.raises(SubmissionDisabled, match="ACCOUNT_EXECUTOR_ALREADY_RUNNING"):
            second.acquire()
        first.assert_owned()
    finally:
        first.close()
    second.acquire()
    second.assert_owned()
    second.close()
    with pytest.raises(SubmissionDisabled, match="OWNERSHIP_LOST"):
        first.acquire()


def test_legacy_observer_lock_prevents_managed_executor(cluster):
    candidate = lease(cluster)
    with candidate.repo.connect() as legacy:
        legacy.execute("SELECT pg_advisory_lock(%s)", (LEGACY_EXECUTOR_LOCK,))
        with pytest.raises(SubmissionDisabled, match="ALREADY_RUNNING"):
            candidate.acquire()


def test_lost_lock_is_terminal_not_automatically_reacquired(cluster):
    owner = lease(cluster)
    owner.acquire()
    owner.connection.execute("SELECT pg_advisory_unlock_all()")
    with pytest.raises(SubmissionDisabled, match="OWNERSHIP_LOST"):
        owner.assert_owned()
    assert owner.lost and owner.connection is None
    with pytest.raises(SubmissionDisabled, match="OWNERSHIP_LOST"):
        owner.acquire()


def test_final_http_transport_refuses_send_after_ownership_loss(cluster):
    owner = lease(cluster)
    gate = AuthorizationGate(owner.repo)
    claimed, sent = [], []
    gate.claim = lambda request: claimed.append(request)
    transport = AuthorizedPaperTransport(
        FencedAuthorizationGate(gate, owner),
        httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)),
    )
    request = httpx.Request(
        "DELETE", "https://paper-api.alpaca.markets/v2/orders/fixture-order",
        headers={"APCA-API-KEY-ID": "PKEXECUTORFIXTURE"},
    )
    with pytest.raises(SubmissionDisabled, match="OWNERSHIP_REQUIRED"):
        transport.handle_request(request)
    assert not sent and not claimed
    owner.acquire()
    assert transport.handle_request(request).status_code == 200
    assert len(claimed) == len(sent) == 1
    owner.connection.close()
    with pytest.raises(SubmissionDisabled, match="OWNERSHIP_LOST"):
        transport.handle_request(request)
    assert len(sent) == 1


def test_ownership_loss_while_waiting_for_authorization_prevents_http_send(cluster):
    owner = lease(cluster)
    owner.acquire()
    gate = AuthorizationGate(owner.repo)
    sent = []

    def claim(_request):
        owner.connection.close()  # Simulate owner-session loss while risk claim awaited.
        return "already-committed-fixture-claim"

    gate.claim = claim
    transport = AuthorizedPaperTransport(
        FencedAuthorizationGate(gate, owner),
        httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)),
    )
    request = httpx.Request(
        "DELETE", "https://paper-api.alpaca.markets/v2/orders/fixture-order",
        headers={"APCA-API-KEY-ID": "PKEXECUTORFIXTURE"},
    )
    with pytest.raises(SubmissionDisabled, match="OWNERSHIP_LOST"):
        transport.handle_request(request)
    assert not sent
