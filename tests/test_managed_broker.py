"""Paper HTTP boundary fixtures; no provider credentials or external network."""

import copy
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.managed_broker import ManagedPaperBroker, normalize_trade_update
from catalyst_lab.market import MarketDataError
from catalyst_lab.paper_execution import BrokerMutationRejected, BrokerMutationUnknown


class FixtureGate(AuthorizationGate):
    """Checks gateway use of the gate. PostgreSQL authorization has separate tests."""

    def __init__(self):
        self.allowed = {}
        self.claimed = []

    def grant(self, method, path, payload):
        decision = str(uuid4())
        self.allowed[decision] = (method, path, payload)
        return decision

    def claim(self, request):
        decision = request.extensions.get("risk_decision_id")
        exact = (request.method, request.url.path, json.loads(request.content or "{}"))
        if decision not in self.allowed or self.allowed[decision] != exact:
            raise SubmissionDisabled("EXACT_RISK_DECISION_REQUIRED")
        del self.allowed[decision]
        self.claimed.append(decision)
        return decision


def gateway(handler):
    gate = FixtureGate()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PK" + "A" * 20, "fixture-secret-not-a-real-key"),
        gate,
        transport=httpx.MockTransport(handler),
    )
    return broker, gate


def test_missing_risk_decision_sends_nothing():
    calls = []
    broker, _ = gateway(lambda request: calls.append(request))
    with pytest.raises(SubmissionDisabled, match="RISK_DECISION_REQUIRED"):
        broker.mutate("POST", "/v2/orders", {}, None)
    assert calls == []


def test_nonfinite_request_does_not_reach_transport():
    broker, _ = gateway(lambda request: pytest.fail("No request expected"))
    with pytest.raises(SubmissionDisabled, match="INVALID_BROKER_PAYLOAD"):
        broker.mutate("POST", "/v2/orders", {"qty": float("nan")}, str(uuid4()))


def test_exact_request_gate_is_used_once_before_post():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"id": "paper-order", "status": "new"})

    broker, gate = gateway(handler)
    body = {"symbol": "BTC/USD", "qty": "0.01", "side": "buy", "type": "limit"}
    grant = gate.grant("POST", "/v2/orders", body)
    assert broker.mutate("POST", "/v2/orders", body, grant)["id"] == "paper-order"
    with pytest.raises(SubmissionDisabled):
        broker.mutate("POST", "/v2/orders", body, grant)
    assert len(calls) == 1 and gate.claimed == [grant]
    assert str(calls[0].url) == PAPER_ENDPOINT + "/v2/orders"


def test_changed_payload_never_reaches_broker():
    calls = []
    broker, gate = gateway(lambda request: calls.append(request))
    grant = gate.grant("POST", "/v2/orders", {"qty": "1"})
    with pytest.raises(SubmissionDisabled):
        broker.mutate("POST", "/v2/orders", {"qty": "2"}, grant)
    assert not calls


def test_patch_only_prices_and_acknowledgement_retained_without_assuming_completion():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "replacement", "status": "pending_replace"})

    broker, gate = gateway(handler)
    payload = {"stop_price": "105.00"}
    decision = gate.grant("PATCH", "/v2/orders/stop-leg", payload)
    result = broker.mutate("PATCH", "/v2/orders/stop-leg", payload, decision)
    assert result["status"] == "pending_replace"
    assert json.loads(calls[0].content) == payload
    with pytest.raises(SubmissionDisabled, match="PRICE_AMENDMENT_ONLY"):
        broker.mutate("PATCH", "/v2/orders/stop-leg", {"qty": "100"}, decision)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0"])
def test_bad_amendment_price_rejected(value):
    broker, _ = gateway(lambda request: pytest.fail("No request expected"))
    with pytest.raises(SubmissionDisabled, match="INVALID_AMENDMENT_PRICE"):
        broker.mutate("PATCH", "/v2/orders/stop-leg", {"stop_price": value}, str(uuid4()))


def test_delete_acknowledgement_requires_gate_and_has_no_json_body():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    broker, gate = gateway(handler)
    decision = gate.grant("DELETE", "/v2/orders/order-one", {})
    assert broker.mutate("DELETE", "/v2/orders/order-one", {}, decision) == {}
    assert calls[0].content == b""


def test_timeout_is_unknown_not_automatically_retried_or_secret_logged():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive upstream detail", request=request)

    broker, gate = gateway(handler)
    decision = gate.grant("POST", "/v2/orders", {})
    with pytest.raises(BrokerMutationUnknown, match="BROKER_RESPONSE_UNKNOWN") as exc:
        broker.mutate("POST", "/v2/orders", {}, decision)
    assert "sensitive" not in str(exc.value) and len(calls) == 1


@pytest.mark.parametrize(
    "status,exception",
    [
        (422, BrokerMutationRejected),
        (500, BrokerMutationUnknown),
        (302, BrokerMutationUnknown),
    ],
)
def test_provider_errors_sanitized_and_redirects_not_followed(status, exception):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private detail", headers={"Location": "https://example.org"}
        )

    broker, gate = gateway(handler)
    decision = gate.grant("POST", "/v2/orders", {})
    with pytest.raises(exception) as exc:
        broker.mutate("POST", "/v2/orders", {}, decision)
    assert "private" not in str(exc.value) and len(calls) == 1


def test_account_evidence_filtered_and_crypto_asset_path_encoded():
    seen = []

    def handler(request):
        seen.append(request.url)
        if request.url.path == "/v2/account":
            return httpx.Response(
                200,
                json={
                    "id": "private-account",
                    "account_number": "private-number",
                    "equity": "10000",
                    "non_marginable_buying_power": "9990",
                    "status": "ACTIVE",
                    "currency": "USD",
                },
            )
        return httpx.Response(200, json={"id": "asset-one", "symbol": "BTC/USD", "class": "crypto"})

    broker, _ = gateway(handler)
    data = broker.account()
    assert data["non_marginable_buying_power"] == "9990"
    assert "account_number" not in data and "id" not in data
    assert broker.asset("BTC/USD")["symbol"] == "BTC/USD"
    assert seen[-1].raw_path == b"/v2/assets/BTC%2FUSD"


def test_stock_asset_invalid_paths_and_read_404():
    broker, _ = gateway(lambda request: httpx.Response(404))
    assert broker.asset("SPY") is None
    assert broker.order_by_client_id("stable-001") is None
    for bad in ("../account", "BTC/USD?foo=bar", "https://example.org", "BTC/EUR"):
        with pytest.raises(MarketDataError):
            broker.asset(bad)


def test_calendar_and_capital_activity_interface():
    def handler(request):
        if request.url.path == "/v2/calendar":
            return httpx.Response(
                200, json=[{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": "cash-event",
                    "account_id": "private",
                    "activity_type": "CSD",
                    "net_amount": "500",
                    "description": "private transfer reference",
                }
            ],
        )

    broker, _ = gateway(handler)
    day = date(2026, 11, 27)
    assert broker.calendar(day, day)[0].flatten_time.hour == 12
    activity = broker.capital_activities(day)[0]
    assert activity == {"id": "cash-event", "activity_type": "CSD", "net_amount": "500"}


def fill_event(asset="crypto", **changes):
    data = {
        "event": "partial_fill",
        "execution_id": "execution-one",
        "timestamp": "2026-09-19T20:01:00.123456789Z",
        "qty": "0.02",
        "price": "101.11",
        "position_qty": "0.0599",
        "order": {
            "id": "order-one",
            "client_order_id": "client-one",
            "asset_class": asset,
            "symbol": "BTCUSD",
            "qty": "0.1",
            "filled_qty": "0.06",
            "filled_avg_price": "100.99",
            "side": "buy",
            "status": "partially_filled",
            "updated_at": "2026-09-19T20:01:00.123456900Z",
        },
    }
    data.update(changes)
    return {"stream": "trade_updates", "data": data}


def test_fill_uses_event_increment_price_and_execution_identity_not_order_average():
    raw = fill_event()
    update = normalize_trade_update(raw)
    assert update.fill_qty == Decimal("0.02")
    assert update.fill_price == Decimal("101.11")
    assert update.cumulative_fill_qty == Decimal("0.06")
    assert update.position_qty == Decimal("0.0599")
    assert update.symbol == "BTC/USD" and update.execution_id == "execution-one"
    assert update.occurred_at_ns % 1_000_000_000 == 123456789
    assert update.payload == raw and update.payload_hash == normalize_trade_update(raw).payload_hash


@pytest.mark.parametrize("field", ["execution_id", "qty", "price", "timestamp", "position_qty"])
def test_missing_fill_evidence_is_not_reconstructed(field):
    raw = fill_event()
    del raw["data"][field]
    with pytest.raises(MarketDataError, match="INVALID_MANAGED"):
        normalize_trade_update(raw)


def test_identity_redaction_preserves_other_broker_payload_and_does_not_mutate_input():
    raw = fill_event()
    raw["data"]["account_id"] = "private-owner"
    raw["data"]["order"]["account_number"] = "private-owner-number"
    raw["data"]["order"]["secret"] = "fixture-sensitive-value"
    before = copy.deepcopy(raw)
    update = normalize_trade_update(raw)
    assert raw == before
    assert update.redacted_paths == (
        "data.order.account_number",
        "data.order.secret",
        "data.account_id",
    )
    assert "private-owner" not in json.dumps(update.payload)
    assert "fixture-sensitive-value" not in json.dumps(update.payload)


def test_nonfill_deterministic_local_id_and_no_invented_fill():
    raw = fill_event(event="canceled")
    for key in ("execution_id", "qty", "price", "position_qty"):
        del raw["data"][key]
    result = normalize_trade_update(raw)
    assert result.event_id.startswith("sha256:")
    assert result.fill_qty is None and result.fill_price is None


def test_stock_fill_whole_shares_and_negative_position_exposed_for_halt():
    raw = fill_event(asset="us_equity", qty="2", position_qty="-1")
    raw["data"]["order"].update(symbol="SPY", qty="5", filled_qty="2")
    result = normalize_trade_update(raw)
    assert result.symbol == "SPY" and result.position_qty == Decimal(-1)
    raw["data"]["qty"] = "0.5"
    with pytest.raises(MarketDataError):
        normalize_trade_update(raw)


def test_malformed_or_authentication_frame_never_becomes_trade_evidence():
    for raw in (None, [], {"stream": "authorization", "data": {}}, fill_event(qty="NaN")):
        with pytest.raises(MarketDataError):
            normalize_trade_update(raw)
