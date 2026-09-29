"""research_agent.submit: POST against a mock intake. Offline only: httpx.MockTransport
stands in for the app; no real network access anywhere in this file.
"""

import httpx
import pytest

from research_agent import submit

REPORT = {"schema_version": "AGENT_RESEARCH_REPORT_V3", "report_id": "r1", "picks": []}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_submit_sends_the_bearer_token_and_the_report_body():
    seen = {}

    def handle(request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(202, json={"status": "MUSE_REPORT_RECORDED", "cycle_id": "c1"})

    result = submit.submit_report("https://app.example", "secret-token-xyz", REPORT,
                                  client=_client(handle))
    assert result.status_code == 202 and result.accepted
    assert result.body == {"status": "MUSE_REPORT_RECORDED", "cycle_id": "c1"}
    assert seen["auth"] == "Bearer secret-token-xyz"
    assert seen["path"] == submit.SUBMIT_ROUTE
    assert b"secret-token-xyz" not in seen["body"]  # The token is a header, never the body.


def test_submit_reports_a_422_rejection_without_raising():
    def handle(request):
        return httpx.Response(422, json={"detail": "INVALID_MUSE_REPORT", "errors": []})

    result = submit.submit_report("https://app.example", "t", REPORT, client=_client(handle))
    assert result.status_code == 422 and not result.accepted
    assert result.body["detail"] == "INVALID_MUSE_REPORT"


def test_submit_reports_a_403_agent_mismatch():
    def handle(request):
        return httpx.Response(403, json={"detail": "AGENT_IDENTITY_MISMATCH"})

    result = submit.submit_report("https://app.example", "wrong-agent-token", REPORT,
                                  client=_client(handle))
    assert result.status_code == 403 and not result.accepted


def test_submit_handles_a_non_json_response_body():
    def handle(request):
        return httpx.Response(500, text="internal error")

    result = submit.submit_report("https://app.example", "t", REPORT, client=_client(handle))
    assert result.status_code == 500 and result.body is None and result.raw_text == "internal error"


def test_submit_raises_submit_error_on_connection_failure():
    def handle(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(submit.SubmitError, match="CONNECTION_ERROR"):
        submit.submit_report("https://app.example", "t", REPORT, client=_client(handle))


def test_submit_result_accepted_property_is_exactly_202():
    assert submit.SubmitResult(202, {}, "").accepted is True
    assert submit.SubmitResult(200, {}, "").accepted is False
    assert submit.SubmitResult(201, {}, "").accepted is False


@pytest.mark.parametrize("send, route", [
    (submit.submit_outlook, "/api/v1/lab/market-outlooks"),
    (submit.submit_post_mortem, "/api/v1/lab/post-mortems"),
])
def test_the_learning_routes_post_with_the_token_in_the_header_only(send, route):
    seen = {}

    def handle(request):
        seen["auth"], seen["path"] = request.headers.get("authorization"), request.url.path
        seen["body"] = request.content
        return httpx.Response(202, json={"status": "RECORDED", "idempotent_replay": False})

    result = send("https://app.example", "secret-token-xyz", {"schema_version": "X"},
                  client=_client(handle))
    assert result.accepted and seen["path"] == route
    assert seen["auth"] == "Bearer secret-token-xyz" and b"secret-token-xyz" not in seen["body"]


def test_the_learning_routes_refuse_an_unsafe_base_url_before_any_request():
    sent = []
    client = _client(lambda request: sent.append(request) or httpx.Response(202, json={}))
    for send in (submit.submit_outlook, submit.submit_post_mortem):
        with pytest.raises(submit.SubmitError, match="BASE_URL_REFUSED"):
            send("http://trader.up.railway.app", "t", {}, client=client)
    assert sent == []
