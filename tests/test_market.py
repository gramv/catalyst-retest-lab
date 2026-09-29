import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials, AlpacaReadOnly, FixedStreamConnection
from catalyst_lab.market import MarketDataError, Observation, Session
from catalyst_lab.runtime import MarketRuntime
from tests.conftest import NOW


def test_precision_calendar_and_provenance():
    obs = Observation.from_wire(
        {
            "T": "t",
            "S": "AAPL",
            "t": "2026-09-18T14:00:00.123456789Z",
            "p": "100.01",
            "s": 2,
            "i": 42,
        },
        "iex",
    )
    assert obs.ns % 1_000_000_000 == 123456789
    assert obs.price == D("100.01") and obs.data_feed == "iex"
    assert Observation.from_json(obs.to_json()) == obs
    session = Session.from_calendar({"date": "2026-11-27", "open": "09:30", "close": "13:00"})
    assert session.flatten_time.hour == 12 and session.flatten_time.minute == 55
    assert not session.contains(session.closes)


@pytest.mark.parametrize(
    "field,value",
    [("p", "NaN"), ("p", "Infinity"), ("p", -1), ("t", "2026-09-18T10:00:00"), ("S", "../orders")],
)
def test_malformed_market_data_is_not_strategy_evidence(field, value):
    row = {"T": "t", "S": "AAPL", "t": NOW.isoformat(), "p": 100, "s": 1, "i": 1}
    with pytest.raises(MarketDataError):
        Observation.from_wire(row | {field: value}, "iex")


def test_adapter_is_get_only_and_filters_account_identifiers():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host in {"paper-api.alpaca.markets", "data.alpaca.markets"}
        assert request.headers["APCA-API-KEY-ID"] == "PKFIXTURE000000000001"
        if request.url.path == "/v2/account":
            return httpx.Response(
                200, json={"id": "private", "status": "ACTIVE", "equity": "10000"}
            )
        if request.url.path == "/v2/calendar":
            return httpx.Response(
                200, json=[{"date": NOW.date().isoformat(), "open": "09:30", "close": "16:00"}]
            )
        if request.url.path.endswith("/latest"):
            assert request.url.params["feed"] == "iex"
            return httpx.Response(
                200, json={"quotes": {"AAPL": {"bp": 100, "ap": 100.01, "t": NOW.isoformat()}}}
            )
        return httpx.Response(200, json=[])

    adapter = AlpacaReadOnly(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert "id" not in adapter.account()
        assert adapter.calendar(NOW.date(), NOW.date())[0].closes.hour == 16
        assert adapter.quotes(["AAPL"])[0].data_feed == "iex"
        assert adapter.positions() == adapter.open_orders() == []
        with pytest.raises(MarketDataError, match="ENDPOINT_NOT_ALLOWED"):
            adapter._get("https://unexpected.example")
        assert len(requests) == 5
    finally:
        adapter.close()


def test_redirects_do_not_forward_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://unexpected.example"})

    adapter = AlpacaReadOnly(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MarketDataError, match="ALPACA_HTTP_302"):
            adapter.account()
        assert len(calls) == 1
    finally:
        adapter.close()
    connection = FixedStreamConnection("wss://stream.data.alpaca.markets/v2/iex")
    error = RuntimeError("fixture redirect")
    assert connection.process_redirect(error) is error
    with pytest.raises(ValueError):
        FixedStreamConnection("wss://unexpected.example")


@pytest.mark.parametrize("variable", ["APCA_API_BASE_URL", "APCA_DATA_BASE_URL"])
def test_environment_cannot_redirect_credentials(monkeypatch, variable):
    monkeypatch.setenv(variable, "https://unexpected.example")
    with pytest.raises(ValueError):
        AlpacaCredentials.from_env()


def test_secrets_are_redacted_and_provider_errors_sanitized():
    credentials = AlpacaCredentials("PKPRIVATE000000000001", "private-secret")
    assert "private" not in repr(credentials)

    def broken(request):
        raise httpx.ConnectError("private-secret")

    adapter = AlpacaReadOnly(credentials, transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(MarketDataError) as error:
            adapter.account()
        assert str(error.value) == "ALPACA_CONNECTION_ERROR"
    finally:
        adapter.close()


class FakeWatcher:
    def __init__(self):
        self.events, self.ticks = [], []

    def active(self):
        return [{"ticker": "AAPL", "session_date": NOW.date()}]

    def system_event(self, kind, payload):
        self.events.append((kind, payload))

    def tick(self, sessions, now, healthy, obs=None, **kwargs):
        self.ticks.append((set(healthy), obs))


class FakeSocket:
    def __init__(self, frames):
        self.frames = frames
        self.sent = []
        self.on_empty = lambda: None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self, timeout):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        self.on_empty()
        raise TimeoutError


def test_stream_requires_auth_and_complete_subscription_before_watch():
    socket = FakeSocket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [
                {"T": "subscription", "quotes": ["AAPL"], "trades": [], "bars": []},
                {"T": "t", "S": "AAPL", "t": NOW.isoformat(), "p": 100, "s": 1, "i": 1},
            ],
            [{"T": "subscription", "quotes": ["AAPL"], "trades": ["AAPL"], "bars": ["AAPL"]}],
            [
                {"T": "q", "S": "AAPL", "t": NOW.isoformat(), "bp": 99.99, "ap": 100.01},
                {"T": "t", "S": "AAPL", "t": NOW.isoformat(), "p": 100, "s": 1, "i": 2},
            ],
        ]
    )

    def connector(url, **kwargs):
        assert url == "wss://stream.data.alpaca.markets/v2/iex"
        assert kwargs["proxy"] is None
        assert not kwargs["logger"].isEnabledFor(10)
        return socket

    watcher = FakeWatcher()
    adapter = AlpacaReadOnly(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"))
    runtime = MarketRuntime(adapter, watcher, connector=connector, clock=lambda: NOW)
    socket.on_empty = runtime.stop_event.set
    try:
        runtime._stream_session()
        assert socket.sent[0]["action"] == "auth"
        assert socket.sent[1] == {
            "action": "subscribe",
            "trades": ["AAPL"],
            "quotes": ["AAPL"],
            "bars": ["AAPL"],
        }
        observations = [obs for _, obs in watcher.ticks if obs]
        assert [obs.kind for obs in observations] == ["quote", "trade"]
        assert observations[-1].trade_id == "2"
        assert runtime.status()["feed_coverage"] == "IEX_ONLY"
        assert runtime.status()["fresh_quote_count"] == 1
        assert "fixture-secret" not in json.dumps(runtime.status(private=True))
    finally:
        adapter.close()


def test_stream_provider_error_text_is_not_exposed():
    socket = FakeSocket([[{"T": "error", "code": 409, "msg": "sensitive-provider-text"}]])
    with pytest.raises(MarketDataError) as error:
        MarketRuntime._messages(socket, 1)
    assert str(error.value) == "ALPACA_STREAM_409"


def test_connection_does_not_claim_quote_freshness():
    adapter = AlpacaReadOnly(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"))
    runtime = MarketRuntime(adapter, FakeWatcher(), clock=lambda: NOW)
    try:
        runtime.authenticated = runtime.connected = True
        runtime.rest_at = NOW
        runtime.quotes["AAPL"] = Observation(
            "quote",
            "AAPL",
            (NOW - timedelta(seconds=6)).isoformat(),
            "iex",
            bid=D("100"),
            ask=D("100.01"),
        )
        status = runtime.status()
        assert status["broker_connected"] and status["stream_authenticated"]
        assert status["fresh_quote_count"] == 0 and not status["execution_enabled"]
    finally:
        adapter.close()


def test_worker_lease_prevents_duplicate_observers(repo):
    from catalyst_lab.watcher import Watcher

    class QuietRuntime(MarketRuntime):
        def _rest_loop(self):
            self.stop_event.wait()

        def _stream_loop(self):
            self.stop_event.wait()

    first_adapter = AlpacaReadOnly(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"))
    second_adapter = AlpacaReadOnly(AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"))
    first = QuietRuntime(first_adapter, Watcher(repo), clock=lambda: NOW)
    second = QuietRuntime(second_adapter, Watcher(repo), clock=lambda: NOW)
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.start()
    finally:
        first.stop()
    second.start()
    second.stop()
    assert not any(t.is_alive() for t in first.threads + second.threads)
