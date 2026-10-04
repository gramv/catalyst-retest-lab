"""Coinbase as the reference market (owner approval 2026-09-29): the public feed
(``coinbase_feed``), ``CRYPTO_COINBASE_TRIGGER_V1`` and ``CRYPTO_STOP_BREACH_V3``.

Setups are admitted at entry trigger 100, max entry 100.10, stop 95 and target 111 (the report-V3
fixture pick); the native stop-limit rests at stop 95, limit 94.99.

Fixture evidence only: the real ``CoinbaseFeed`` fed Coinbase-shaped messages (the shapes of a
short read-only probe of the public feed on 2026-09-29) at the test clock, per-test disposable
PostgreSQL databases, the fake paper venue, a mock Jev transport and Alpaca market data behind an
httpx MockTransport. No network, broker or owner-ledger contact.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import coinbase_feed as cf
from catalyst_lab import coinbase_trigger as cb
from catalyst_lab import crypto_trigger as ct
from catalyst_lab import stop_breach
from catalyst_lab import system_check as sc
from catalyst_lab.alpaca import FixedStreamConnection
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_app import public_status
from catalyst_lab.managed_classification import ALPACA_CRYPTO_SECTOR_OF
from catalyst_lab.managed_runtime import ManagedRuntime
from catalyst_lab.managed_service import STATE_FIELDS, STATUS_FIELDS
from tests.test_crypto_trigger import entry_decision, rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation, packet
from tests.test_system_check import market as market  # noqa: F401
from tests.test_system_check import publish_v3, stream, system_runtime, two_slots, v3_pick

T0 = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
ADMITTED = T0 - timedelta(minutes=5)
LEVELS = {"entry_trigger": D("100"), "max_entry_price": D("100.10"), "stop": D("95")}


def stamp(at):
    """A Coinbase time: UTC with microseconds and ``Z``."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Coinbase:
    """The real ``CoinbaseFeed`` fed Coinbase-shaped messages at the test clock (no network).

    Trade ids start at 1000 per product and heartbeats report the last one sent, so the tape is
    complete unless a test says otherwise."""

    def __init__(self, clock):
        self.clock, self.feed = clock, cf.CoinbaseFeed(clock=clock)
        self.products, self.trade_ids, self.sequence = (), {}, 20943163680

    def _next(self):
        self.sequence += 1
        return self.sequence

    def send(self, message, *, received_at=None):
        self.feed.handle(message, received_at=received_at or self.clock())

    def connect(self, *products):
        self.products = products
        self.feed.begin()
        self.feed.request(products)
        self.send({"type": "subscriptions", "channels": [
            {"name": name, "product_ids": list(products), "account_ids": None}
            for name in ("ticker", "matches", "heartbeat")]})
        self.beat()
        return self

    def beat(self, *products, at=None, received_at=None, last_trade_id=None):
        """One heartbeat for each product (every connected one by default)."""
        for product in products or self.products:
            self.send({"type": "heartbeat", "product_id": product, "sequence": self._next(),
                       "last_trade_id": last_trade_id if last_trade_id is not None
                       else self.trade_ids.setdefault(product, 1000),
                       "time": stamp(at or self.clock())}, received_at=received_at)

    def trade(self, product, price, *, at=None, received_at=None, trade_id=None):
        trade_id = trade_id if trade_id is not None else self.trade_ids.get(product, 1000) + 1
        self.trade_ids[product] = max(trade_id, self.trade_ids.get(product, 0))
        self.send({"type": "match", "trade_id": trade_id, "maker_order_id": "fixture-maker",
                   "taker_order_id": "fixture-taker", "side": "sell", "size": "0.01",
                   "price": str(price), "product_id": product, "sequence": self._next(),
                   "time": stamp(at or self.clock())}, received_at=received_at)
        return str(trade_id)

    def quote(self, product, bid, ask, *, at=None, received_at=None):
        self.send({"type": "ticker", "sequence": self._next(), "product_id": product,
                   "price": str(bid), "open_24h": "1", "volume_24h": "1", "low_24h": "1",
                   "high_24h": "1", "volume_30d": "1", "best_bid": str(bid),
                   "best_bid_size": "1", "best_ask": str(ask), "best_ask_size": "1",
                   "side": "sell", "time": stamp(at or self.clock()),
                   "trade_id": self.trade_ids.get(product, 1000), "last_size": "1"},
                  received_at=received_at)

    def disconnect(self):
        self.feed.end()


@pytest.fixture
def coinbase(mx):
    """The engine's Coinbase reference feed (as ``build_runtime_from_env`` wires it)."""
    engine, venue, _ = mx
    feed = Coinbase(lambda: venue.now)
    engine.reference_feed = feed.feed
    return feed


# --- The feed (no database) -------------------------------------------------------------------

# A short read-only probe of the public feed on 2026-09-29 (BTC-USD; receipt time, message). The
# heartbeat's last_trade_id is the last match kept in this excerpt.
RECORDED = [
    ("2026-09-29T13:45:58.249531+00:00", {"type": "subscriptions", "channels": [
        {"name": "ticker", "product_ids": ["BTC-USD"], "account_ids": None},
        {"name": "matches", "product_ids": ["BTC-USD"], "account_ids": None},
        {"name": "heartbeat", "product_ids": ["BTC-USD"], "account_ids": None}]}),
    ("2026-09-29T13:45:58.271634+00:00", {
        "type": "ticker", "sequence": 136967652357, "product_id": "BTC-USD", "price": "84140.83",
        "open_24h": "83629.9", "volume_24h": "5667.41926850", "low_24h": "82510.37",
        "high_24h": "84557.01", "volume_30d": "180889.67159800", "best_bid": "84138.41",
        "best_bid_size": "0.02880000", "best_ask": "84146.91", "best_ask_size": "0.00949200",
        "side": "buy", "time": "2026-09-29T13:45:58.116787Z", "trade_id": 1099995623,
        "last_size": "0.00004104"}),
    ("2026-09-29T13:45:58.271734+00:00", {
        "type": "last_match", "trade_id": 1099995623,
        "maker_order_id": "7b0e4aeb-72c1-4d36-adce-e6221ffecc05",
        "taker_order_id": "92906bb7-eb38-40c1-9c54-76bc1dd19ce4", "side": "sell",
        "size": "0.00004104", "price": "84140.83", "product_id": "BTC-USD",
        "sequence": 136967652357, "time": "2026-09-29T13:45:58.116787Z"}),
    ("2026-09-29T13:45:58.484908+00:00", {
        "type": "ticker", "sequence": 136967653295, "product_id": "BTC-USD", "price": "84149.24",
        "open_24h": "83629.9", "volume_24h": "5667.41926871", "low_24h": "82510.37",
        "high_24h": "84557.01", "volume_30d": "180889.67159821", "best_bid": "84149.24",
        "best_bid_size": "0.00594082", "best_ask": "84149.25", "best_ask_size": "0.10011574",
        "side": "sell", "time": "2026-09-29T13:45:58.467935Z", "trade_id": 1099995624,
        "last_size": "0.00000021"}),
    ("2026-09-29T13:45:58.485022+00:00", {
        "type": "match", "trade_id": 1099995624,
        "maker_order_id": "886cfa0b-c186-48d7-872c-21e24e710159",
        "taker_order_id": "398a1ee7-48a9-407d-abe5-98fe31687336", "side": "buy",
        "size": "0.00000021", "price": "84149.24", "product_id": "BTC-USD",
        "sequence": 136967653295, "time": "2026-09-29T13:45:58.467935Z"}),
    ("2026-09-29T13:45:58.562698+00:00", {
        "type": "match", "trade_id": 1099995625,
        "maker_order_id": "886cfa0b-c186-48d7-872c-21e24e710159",
        "taker_order_id": "5fc23b3c-d076-46b1-8514-15b7e60996f4", "side": "buy",
        "size": "0.00000004", "price": "84149.24", "product_id": "BTC-USD",
        "sequence": 136967653370, "time": "2026-09-29T13:45:58.545416Z"}),
    ("2026-09-29T13:45:59.027031+00:00", {
        "type": "heartbeat", "last_trade_id": 1099995625, "product_id": "BTC-USD",
        "sequence": 136967653778, "time": "2026-09-29T13:45:59.000000Z"}),
]


def at(text):
    return datetime.fromisoformat(text)


def recorded_feed(messages=RECORDED):
    feed = cf.CoinbaseFeed(clock=lambda: at(messages[0][0]))
    feed.begin()
    feed.request({"BTC-USD"})
    for received, message in messages:
        feed.handle(message, received_at=at(received))
    return feed


def test_every_alpaca_usd_pair_maps_to_its_coinbase_product():
    assert {cf.product_id(s) for s in ALPACA_CRYPTO_SECTOR_OF} == cf.COINBASE_USD_PRODUCTS
    assert len(cf.COINBASE_USD_PRODUCTS) == 33 and cf.PRODUCTS_VERIFIED_ON == "2026-09-29"
    assert (cf.product_id("BTC/USD"), cf.product_id("RENDER/USD")) == ("BTC-USD", "RENDER-USD")
    for other in ("AAA/USD", "MATIC/USD", "BTC/EUR", "BTC-USD", "btc/usd", "", None, 7):
        assert cf.product_id(other) is None, other


def test_the_feed_reads_the_public_messages_as_recorded():
    feed = recorded_feed()
    now = at("2026-09-29T13:45:59.5+00:00")
    view = feed.view("BTC-USD", now)
    assert (view.healthy, view.code, view.connected, view.acknowledged) == (True, None, True, True)
    # The quote is the latest ticker's, with its own time (the trade it reports) and receipt.
    assert (view.bid, view.ask) == (D("84149.24"), D("84149.25"))
    assert view.quote_at == at("2026-09-29T13:45:58.467935+00:00")
    assert view.quote_received_at == at("2026-09-29T13:45:58.484908+00:00")
    # Prints: last_match at subscription, then every match, each with both times.
    assert [(p.trade_id, p.price) for p in view.prints] == [
        ("1099995623", D("84140.83")), ("1099995624", D("84149.24")),
        ("1099995625", D("84149.24"))]
    assert view.last_print.trade_id == "1099995625"
    assert view.last_print.received_at == at("2026-09-29T13:45:58.562698+00:00")
    assert view.heartbeat_at == at("2026-09-29T13:45:59+00:00")
    low = view.lowest_print(traded_from=at("2026-09-29T13:45:58+00:00"), traded_to=now)
    assert low.price == D("84140.83")
    assert view.latest_print_at_or_below(
        D("84149.24"), traded_from=at("2026-09-29T13:45:58+00:00"), traded_to=now
    ).trade_id == "1099995625"
    assert view.health() == {
        "provider": "COINBASE_EXCHANGE", "product_id": "BTC-USD", "healthy": True, "code": None,
        "as_of": now.isoformat(), "heartbeat_at": "2026-09-29T13:45:59+00:00",
        "heartbeat_received_at": "2026-09-29T13:45:59.027031+00:00"}
    status = feed.status(now)
    assert [status[k] for k in ("requested", "acknowledged", "healthy", "unhealthy")] == [
        ["BTC-USD"], ["BTC-USD"], ["BTC-USD"], {}]
    assert status["channels"] == ["heartbeat", "ticker", "matches"]


def test_health_needs_all_three_channels_acknowledged_and_a_current_heartbeat():
    now = T0
    feed = cf.CoinbaseFeed(clock=lambda: now)
    assert feed.health("BTC-USD", now) == (False, "DISCONNECTED")
    feed.begin()
    feed.request({"BTC-USD", "ETH-USD"})
    assert feed.health("BTC-USD", now) == (False, "SUBSCRIPTION_UNACKNOWLEDGED")
    assert feed.health("SOL-USD", now) == (False, "NOT_REQUESTED")
    assert feed.health(None, now) == (False, "NOT_A_COINBASE_PRODUCT")
    feed.handle({"type": "subscriptions", "channels": [  # ETH lacks the matches channel.
        {"name": "ticker", "product_ids": ["BTC-USD", "ETH-USD"]},
        {"name": "matches", "product_ids": ["BTC-USD"]},
        {"name": "heartbeat", "product_ids": ["BTC-USD", "ETH-USD"]}]}, received_at=now)
    assert feed.health("ETH-USD", now) == (False, "SUBSCRIPTION_UNACKNOWLEDGED")
    assert feed.health("BTC-USD", now) == (False, "NO_HEARTBEAT")
    feed.handle({"type": "heartbeat", "product_id": "BTC-USD", "sequence": 1,
                 "last_trade_id": 5, "time": stamp(now)}, received_at=now)
    assert feed.health("BTC-USD", now) == (True, None)
    assert feed.health("BTC-USD", now + timedelta(seconds=3)) == (True, None)
    assert feed.health("BTC-USD", now + timedelta(seconds=3, microseconds=1)) == (
        False, "HEARTBEAT_STALE")
    # A heartbeat received after the reader's own clock read is fresh, not stale.
    assert feed.health("BTC-USD", now - timedelta(seconds=1)) == (True, None)
    # An unreadable heartbeat is no heartbeat.
    feed.handle({"type": "heartbeat", "product_id": "BTC-USD", "sequence": 2,
                 "last_trade_id": "5", "time": stamp(now)}, received_at=now + timedelta(seconds=2))
    assert feed.health("BTC-USD", now + timedelta(seconds=4)) == (False, "HEARTBEAT_STALE")
    feed.end()
    assert feed.health("BTC-USD", now) == (False, "DISCONNECTED")
    assert feed.view("BTC-USD", now).code == "DISCONNECTED"


@pytest.mark.parametrize(("offset", "healthy"), [(-3, True), (-3.001, False), (3, True),
                                                 (3.001, False)])
def test_a_lagging_feed_or_a_skewed_clock_is_unhealthy(offset, healthy):
    """The heartbeat's Coinbase time must be within 3 s of its receipt, either way."""
    feed = cf.CoinbaseFeed(clock=lambda: T0)
    feed.begin()
    feed.request({"BTC-USD"})
    feed.handle({"type": "subscriptions", "channels": [
        {"name": n, "product_ids": ["BTC-USD"]} for n in cf.CHANNELS]}, received_at=T0)
    feed.handle({"type": "heartbeat", "product_id": "BTC-USD", "sequence": 1, "last_trade_id": 5,
                 "time": stamp(T0 + timedelta(seconds=offset))}, received_at=T0)
    assert feed.health("BTC-USD", T0) == ((True, None) if healthy else (False, "CLOCK_SKEW"))


def test_a_trade_tape_gap_holds_the_product_unhealthy_for_ten_seconds():
    clock = [T0]
    coinbase = Coinbase(lambda: clock[0]).connect("BTC-USD")
    feed = coinbase.feed
    coinbase.trade("BTC-USD", "100")  # 1001: continuous with the heartbeat's 1000.
    assert feed.health("BTC-USD", T0) == (True, None)
    # A heartbeat ahead of the matches received: a match the channel dropped.
    coinbase.beat(last_trade_id=1003)
    assert feed.health("BTC-USD", T0) == (False, "TRADE_TAPE_GAP")
    clock[0] = T0 + timedelta(seconds=9.9)
    coinbase.beat(last_trade_id=1003)  # Consistent from here on: no new gap.
    assert feed.health("BTC-USD", clock[0]) == (False, "TRADE_TAPE_GAP")
    clock[0] = T0 + timedelta(seconds=10)
    coinbase.beat(last_trade_id=1003)
    assert feed.health("BTC-USD", clock[0]) == (True, None)
    # A jump in match trade ids, and an unreadable match, are gaps too.
    coinbase.trade("BTC-USD", "100", trade_id=1006)
    assert feed.health("BTC-USD", clock[0]) == (False, "TRADE_TAPE_GAP")
    clock[0] += timedelta(seconds=10)
    coinbase.beat()
    assert feed.health("BTC-USD", clock[0]) == (True, None)
    coinbase.send({"type": "match", "product_id": "BTC-USD", "trade_id": 1007,
                   "price": "not-a-price", "time": stamp(clock[0])})
    assert feed.health("BTC-USD", clock[0]) == (False, "TRADE_TAPE_GAP")
    assert feed.status(clock[0])["tape_gaps"] == 3
    # The retained prints keep only readable trades, each once.
    assert [p.trade_id for p in feed.view("BTC-USD", clock[0]).prints] == ["1001", "1006"]


def test_a_refused_product_is_unhealthy_while_the_others_stay_subscribed():
    """Coinbase's answer to a delisted or unknown product (probe of 2026-09-29): one error per
    channel, and the others subscribed. Any other provider error ends the session."""
    feed = cf.CoinbaseFeed(clock=lambda: T0)
    feed.begin()
    feed.request({"BAT-USD", "YFI-USD"})
    for _ in range(3):
        feed.handle({"type": "error", "message": "Failed to subscribe",
                     "reason": "YFI-USD is delisted"}, received_at=T0)
    feed.handle({"type": "subscriptions", "channels": [
        {"name": n, "product_ids": ["BAT-USD"], "account_ids": None}
        for n in ("ticker", "matches", "heartbeat")]}, received_at=T0)
    feed.handle({"type": "heartbeat", "product_id": "BAT-USD", "sequence": 3,
                 "last_trade_id": 9, "time": stamp(T0)}, received_at=T0)
    assert feed.settled()
    assert feed.health("BAT-USD", T0) == (True, None)
    assert feed.health("YFI-USD", T0) == (False, "SUBSCRIPTION_REFUSED")
    assert feed.status(T0)["refused"] == {"YFI-USD": "PRODUCT_DELISTED"}
    with pytest.raises(ValueError, match="^REFERENCE_FEED_PROVIDER_ERROR$"):
        feed.handle({"type": "error", "message": "Failed to subscribe",
                     "reason": "SOL-USD is not a valid product"}, received_at=T0)  # Not asked.
    with pytest.raises(ValueError, match="^REFERENCE_FEED_PROVIDER_ERROR$"):
        feed.handle({"type": "error", "message": "rate limited"}, received_at=T0)
    with pytest.raises(ValueError, match="^REFERENCE_SUBSCRIPTION_MISMATCH$"):
        feed.handle({"type": "subscriptions", "channels": [
            {"name": "ticker", "product_ids": ["BAT-USD", "ETH-USD"]}]}, received_at=T0)
    with pytest.raises(ValueError, match="^REFERENCE_SUBSCRIPTION_INVALID$"):
        feed.handle({"type": "subscriptions", "channels": "all"}, received_at=T0)


def test_retained_prints_are_bounded_by_age_and_count(monkeypatch):
    clock = [T0]
    coinbase = Coinbase(lambda: clock[0]).connect("BTC-USD")
    for second in range(40):
        clock[0] = T0 + timedelta(seconds=second)
        coinbase.trade("BTC-USD", str(100 - second))
    view = coinbase.feed.view("BTC-USD", clock[0])
    # Received in the last 30 s only: seconds 9 to 39.
    assert [p.price for p in view.prints] == [D(100 - s) for s in range(9, 40)]
    # A repeated trade (last_match after a reconnect) is kept once.
    coinbase.feed.begin()
    coinbase.feed.request({"BTC-USD"})
    coinbase.send({"type": "last_match", "trade_id": 1040, "price": "61", "product_id": "BTC-USD",
                   "time": stamp(clock[0]), "sequence": 1, "side": "buy", "size": "1",
                   "maker_order_id": "m", "taker_order_id": "t"})
    assert len(coinbase.feed.view("BTC-USD", clock[0]).prints) == 31
    monkeypatch.setattr(cf, "MAX_RETAINED_PRINTS", 3)
    coinbase.trade("BTC-USD", "60")
    assert [p.price for p in coinbase.feed.view("BTC-USD", clock[0]).prints] == [
        D("62"), D("61"), D("60")]


class Socket:
    """A fake connection: frames in order (a callable frame runs instead of being sent), then
    the stop event."""

    def __init__(self, frames, stop):
        self.frames, self.stop, self.sent, self.closed = list(frames), stop, [], False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, timeout):
        while self.frames and callable(self.frames[0]):
            self.frames.pop(0)()
        if self.frames:
            frame = self.frames.pop(0)
            return frame if isinstance(frame, str) else json.dumps(frame)
        self.stop.set()
        raise TimeoutError

    def close(self):
        self.closed = True


class Stop:
    def __init__(self):
        self.done = False

    def is_set(self):
        return self.done

    def set(self):
        self.done = True

    def wait(self, _seconds):
        return self.done


def acknowledgment(*products):
    return {"type": "subscriptions", "channels": [
        {"name": n, "product_ids": list(products), "account_ids": None} for n in cf.CHANNELS]}


def test_the_session_sends_only_subscribe_and_unsubscribe_and_follows_the_wanted_products():
    stop, wanted, opened, settled = Stop(), [{"BTC-USD", "ETH-USD"}], [], []
    socket = Socket([
        acknowledgment("BTC-USD", "ETH-USD"),
        {"type": "heartbeat", "product_id": "BTC-USD", "sequence": 1, "last_trade_id": 9,
         "time": stamp(T0)},
        lambda: wanted.append({"BTC-USD"}),  # ETH's setup closed.
        acknowledgment("BTC-USD"),
        {"type": "heartbeat", "product_id": "ETH-USD", "sequence": 2, "last_trade_id": 3,
         "time": stamp(T0)},  # A late message for an unsubscribed product: ignored.
    ], stop)

    def connect(endpoint, **options):
        opened.append((endpoint, options))
        return socket

    feed = cf.CoinbaseFeed(clock=lambda: T0, connector=connect)
    ended = feed.session(wanted=lambda: wanted[-1], stop_event=stop, open_timeout=8,
                         read_timeout=1, on_acknowledged=lambda p, r: settled.append((p, r)))
    assert ended == cf.STOPPED
    [(endpoint, options)] = opened
    assert endpoint == "wss://ws-feed.exchange.coinbase.com" and options["proxy"] is None
    assert socket.sent == [
        {"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"],
         "channels": ["heartbeat", "ticker", "matches"]},
        {"type": "unsubscribe", "product_ids": ["ETH-USD"],
         "channels": ["heartbeat", "ticker", "matches"]},
    ]
    assert settled == [(["BTC-USD", "ETH-USD"], {}), (["BTC-USD"], {})]
    status = feed.status(T0)
    assert status["connected"] is False and status["sessions"] == 1
    assert status["disconnected_at"] == T0.isoformat()
    assert feed.health("BTC-USD", T0) == (False, "DISCONNECTED")
    # Nothing wanted: the session ends at once, before any subscription.
    quiet = Socket([], Stop())
    assert cf.CoinbaseFeed(clock=lambda: T0, connector=lambda *_, **__: quiet).session(
        wanted=frozenset, stop_event=Stop(), open_timeout=8, read_timeout=1) == cf.NOTHING_WANTED
    assert quiet.sent == []


def test_an_unacknowledged_subscription_or_a_malformed_frame_ends_the_session(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cf, "_monotonic", lambda: clock[0])

    def later():
        clock[0] += 9

    stop = Stop()  # Nine seconds pass with only a message the rules do not read.
    feed = cf.CoinbaseFeed(clock=lambda: T0, connector=lambda *_, **__: Socket(
        [later, {"type": "status", "products": []}], stop))
    with pytest.raises(ValueError, match="^REFERENCE_SUBSCRIPTION_TIMEOUT$"):
        feed.session(wanted=lambda: {"BTC-USD"}, stop_event=stop, open_timeout=8,
                     read_timeout=1)
    for frame in ("[{not json", "[]", '{"no": "type"}'):
        broken = cf.CoinbaseFeed(clock=lambda: T0, connector=lambda *_, f=frame, **__: Socket(
            [acknowledgment("BTC-USD"), f], Stop()))
        with pytest.raises(ValueError, match="^REFERENCE_FRAME_INVALID$"):
            broken.session(wanted=lambda: {"BTC-USD"}, stop_event=Stop(), open_timeout=8,
                           read_timeout=1)
        assert broken.health("BTC-USD", T0) == (False, "DISCONNECTED")


def test_a_silent_connection_ends_the_session_so_the_runtime_reconnects(monkeypatch):
    """Heartbeats come every second per product: ten seconds of nothing at all, with a product
    acknowledged, is a connection open but delivering nothing."""
    clock = [0.0]
    monkeypatch.setattr(cf, "_monotonic", lambda: clock[0])

    class Quiet(Socket):
        def recv(self, timeout):
            if self.frames:
                return super().recv(timeout)
            clock[0] += 4
            raise TimeoutError

    stop = Stop()
    feed = cf.CoinbaseFeed(clock=lambda: T0, connector=lambda *_, **__: Quiet(
        [acknowledgment("BTC-USD"), {"type": "heartbeat", "product_id": "BTC-USD",
                                     "sequence": 1, "last_trade_id": 9, "time": stamp(T0)}],
        stop))
    with pytest.raises(ValueError, match="^REFERENCE_FEED_SILENT$"):
        feed.session(wanted=lambda: {"BTC-USD"}, stop_event=stop, open_timeout=8,
                     read_timeout=1)
    assert clock[0] == 12 and cf.SILENCE_LIMIT_SECONDS == 10
    # A session whose only product Coinbase refused expects nothing: it stays open.
    clock[0], waits = 0.0, []

    class Refusing(Socket):
        def recv(self, timeout):
            if self.frames:
                return super().recv(timeout)
            clock[0] += 4
            waits.append(clock[0])
            if len(waits) == 5:
                self.stop.set()
            raise TimeoutError

    stop = Stop()
    refused = cf.CoinbaseFeed(clock=lambda: T0, connector=lambda *_, **__: Refusing(
        [{"type": "error", "message": "Failed to subscribe", "reason": "YFI-USD is delisted"},
         {"type": "subscriptions", "channels": []}], stop))
    assert refused.session(wanted=lambda: {"YFI-USD"}, stop_event=stop, open_timeout=8,
                           read_timeout=1) == cf.STOPPED
    assert len(waits) == 5


def test_only_the_fixed_public_endpoint_is_permitted():
    with pytest.raises(ValueError, match="fixed Coinbase public market-data feed"):
        cf.CoinbaseStreamConnection("wss://ws-direct.exchange.coinbase.com")
    with pytest.raises(ValueError, match="fixed Alpaca market streams"):
        FixedStreamConnection(cf.ENDPOINT)
    assert cf.ENDPOINT == "wss://ws-feed.exchange.coinbase.com"


class _Waits:
    """``stop_event`` for a reconnect loop: records each wait and stops after ``limit``."""

    def __init__(self, limit):
        self.limit, self.seen = limit, []

    def is_set(self):
        return len(self.seen) >= self.limit

    def wait(self, seconds):
        self.seen.append(seconds)
        return self.is_set()


def test_the_runtime_reconnects_the_feed_with_the_bounded_backoff(monkeypatch):
    """The Alpaca streams' pattern (``reconnect_wait``): drops in a row double the wait, a drop
    after a session that stayed up waits the first wait again; each drop records its gap."""
    import catalyst_lab.managed_runtime as runtime_module
    from tests.test_managed_runtime import runtime

    run = runtime()
    first, cap = run.policy.reconnect_seconds, run.policy.max_reconnect_seconds
    clock, durations = [0.0], iter([0.1, 0.1, 0.1, cap + 1, 0.1])
    monkeypatch.setattr(runtime_module, "_monotonic", lambda: clock[0])

    class Dropping:
        def session(self, **_options):
            clock[0] += next(durations)
            raise ConnectionError("REFERENCE_DROPPED")

    run.execution.reference_feed = Dropping()
    run._reference_products = lambda: frozenset({"BTC-USD"})
    run.stop_event = _Waits(5)
    run._reference_stream_loop()
    assert run.stop_event.seen == [first, min(2 * first, cap), min(4 * first, cap), first,
                                   min(2 * first, cap)]
    gaps = [body for kind, body in run.execution.events if kind == "RUNTIME_REFERENCE_GAP"]
    assert len(gaps) == 5 and {(g["reason"], g["code"]) for g in gaps} == {
        ("REFERENCE_FEED_DISCONNECTED", "REFERENCE_DROPPED")}


# --- CRYPTO_COINBASE_TRIGGER_V1: the rule (pure) ---------------------------------------------

def printed(price, traded=T0, received=None, trade_id="7"):
    return {"price": price, "at": traded.isoformat(),
            "received_at": (received or traded).isoformat(), "trade_id": trade_id}


def reference(*, healthy=True, code=None, low=None, touch=None, bid=None, ask=None,
              quote_at=T0, received=T0):
    return {"provider": "COINBASE_EXCHANGE", "product_id": "BTC-USD", "healthy": healthy,
            "code": code, "as_of": T0.isoformat(), "heartbeat_at": T0.isoformat(),
            "heartbeat_received_at": T0.isoformat(), "bid": bid, "ask": ask,
            "quote_at": quote_at.isoformat() if bid else None,
            "quote_received_at": received.isoformat() if bid else None,
            "low_print": low, "touch_print": touch, "last_print": None,
            "print_retention_seconds": 30}


def alpaca(bid, ask, *, read_at=T0, healthy=True):
    """Alpaca's confirming quote as the runtime hands it over (read ``read_at``)."""
    return {"feed_healthy": healthy, "data_provider": "ALPACA", "data_feed": "CRYPTO_US",
            "bid": bid, "ask": ask, "quote_at": read_at.isoformat(),
            "quote_read_at": read_at.isoformat(), "quote_source": sc.STREAM_SOURCE}


def verdict(ref, quote=None, now=T0):
    return cb.evaluate(LEVELS, {**(quote or {"feed_healthy": True}), "reference": ref},
                       now=now, admitted_at=ADMITTED)


def outcome(ref, quote=None, now=T0):
    result = verdict(ref, quote, now)
    return result.outcome, result.reason, result.touch


def test_a_coinbase_print_at_or_below_the_trigger_touches_and_alpaca_confirms():
    touched = reference(touch=printed("100"), low=printed("99.50"))
    result = verdict(touched, alpaca("99.99", "100.01"))
    assert (result.outcome, result.reason, result.touch) == ("CONFIRM", None, "PRINT")
    evidence = result.evidence
    assert evidence["version"] == "CRYPTO_COINBASE_TRIGGER_V1" and evidence["touch"] == "PRINT"
    assert evidence["reference"]["touch_print"] == {**printed("100"), "age_seconds": "0.0"}
    assert (evidence["reference"]["product_id"], evidence["reference"]["healthy"]) == (
        "BTC-USD", True)
    # Alpaca's confirming quote, recorded as CRYPTO_ALPACA_TRIGGER_V1 records it.
    assert (evidence["bid"], evidence["ask"], evidence["spread_bps"]) == (
        "99.99", "100.01", "2.0000")
    assert (evidence["quote_source"], evidence["quote_fresh"]) == ("ALPACA_STREAM", True)
    # A Coinbase print above the trigger is no touch; nor is Alpaca's own touch.
    assert outcome(reference(touch=None, low=printed("100.01")), alpaca("99.97", "99.99")) == (
        "NO_TOUCH", None, None)


def test_a_fresh_coinbase_ask_touches_and_the_subscription_snapshot_is_not_fresh():
    assert outcome(reference(bid="99.95", ask="100"), alpaca("99.99", "100.01")) == (
        "CONFIRM", None, "QUOTE")
    assert outcome(reference(bid="99.95", ask="100.000001"),
                   alpaca("99.99", "100.01"))[0] == "NO_TOUCH"
    # Fresh: received at most 5 s ago and reporting a trade at most 5 s old.
    old = T0 - timedelta(minutes=2)
    snapshot = reference(bid="99.95", ask="99.99", quote_at=old, received=T0)
    assert outcome(snapshot, alpaca("99.99", "100.01")) == ("NO_TOUCH", None, None)
    assert verdict(snapshot).evidence["reference"]["quote_code"] == "QUOTE_NOT_FRESH"
    edge = T0 - timedelta(seconds=5)
    assert outcome(reference(bid="99.95", ask="99.99", quote_at=edge, received=edge),
                   alpaca("99.99", "100.01"))[0] == "CONFIRM"
    late = T0 - timedelta(seconds=5, microseconds=1)
    assert outcome(reference(bid="99.95", ask="99.99", quote_at=edge, received=late),
                   alpaca("99.99", "100.01"))[0] == "NO_TOUCH"


def test_confirmation_stays_on_alpaca_and_its_book_alone_ends_no_setup():
    touched = reference(touch=printed("99.90"))
    assert outcome(touched) == ("WAIT", "FRESH_QUOTE_UNAVAILABLE", "PRINT")
    stale = alpaca("99.99", "100.01", read_at=T0 - timedelta(seconds=5, microseconds=1))
    assert outcome(touched, stale) == ("WAIT", "FRESH_QUOTE_UNAVAILABLE", "PRINT")
    # Alpaca's spread: exactly 1% passes, 1.01% waits.
    assert outcome(touched, alpaca("99.0025", "99.9975"))[0] == "CONFIRM"
    assert outcome(touched, alpaca("98.977626", "99.982374")) == (
        "WAIT", "SPREAD_ABOVE_MAXIMUM", "PRINT")
    # Alpaca's ask above M waits for its book (CRYPTO_ALPACA_TRIGGER_V1 would invalidate).
    assert outcome(touched, alpaca("100.09", "100.11")) == (
        "WAIT", "ALPACA_ASK_ABOVE_MAX_ENTRY", "PRINT")
    assert outcome(touched, alpaca("100.09", "100.10"))[0] == "CONFIRM"
    assert outcome(touched, alpaca("99.99", "100.01", healthy=False)) == (
        "INVALIDATE", "DATA_FEED_FAILURE", None)
    assert cb.WAIT_REASONS == {"COINBASE_FEED_UNHEALTHY", "FRESH_QUOTE_UNAVAILABLE",
                               "SPREAD_ABOVE_MAXIMUM", "ALPACA_ASK_ABOVE_MAX_ENTRY",
                               "TOUCH_NOT_CURRENT"}


def test_only_coinbase_invalidates_before_the_trigger():
    # A Coinbase print at or below the stop since admission, whatever its age.
    assert outcome(reference(low=printed("95", T0 - timedelta(seconds=20)))) == (
        "INVALIDATE", "STOP_TRADED_BEFORE_TRIGGER", "PRINT")
    assert outcome(reference(low=printed("95.01"))) == ("NO_TOUCH", None, None)
    before = printed("90", ADMITTED - timedelta(seconds=1))  # Before admission: not evaluated.
    assert outcome(reference(low=before)) == ("NO_TOUCH", None, None)
    # A fresh Coinbase bid at or below the stop.
    assert outcome(reference(bid="95", ask="95.02")) == (
        "INVALIDATE", "STOP_QUOTED_BEFORE_TRIGGER", "QUOTE")
    # Alpaca's bid at the stop, with no Coinbase trade or bid there: nothing (V1 invalidates).
    dip = alpaca("94.90", "99.60")
    assert outcome(reference(bid="100.38", ask="100.41", low=printed("100.40")), dip) == (
        "NO_TOUCH", None, None)
    assert ct.evaluate(LEVELS, dip, now=T0, admitted_at=ADMITTED).reason == (
        "STOP_QUOTED_BEFORE_TRIGGER")
    # With Coinbase touching the trigger, the same Alpaca book waits on its spread.
    assert outcome(reference(bid="99.38", ask="99.41", low=printed("99.40")), dip) == (
        "WAIT", "SPREAD_ABOVE_MAXIMUM", "QUOTE")
    # Recorded Coinbase evidence invalidates even while the feed is unhealthy now.
    assert outcome(reference(healthy=False, code="HEARTBEAT_STALE", low=printed("94"))) == (
        "INVALIDATE", "STOP_TRADED_BEFORE_TRIGGER", "PRINT")
    with pytest.raises(ValueError, match="INVALID_MARKET_EVIDENCE"):
        verdict(reference(low={"price": "x", "at": T0.isoformat()}))


def test_an_unhealthy_coinbase_feed_waits_and_confirms_no_touch():
    sick = reference(healthy=False, code="HEARTBEAT_STALE", touch=printed("99.90"))
    assert outcome(sick, alpaca("99.97", "99.99")) == ("WAIT", "COINBASE_FEED_UNHEALTHY", None)
    missing = verdict(None, alpaca("99.97", "99.99"))  # No Coinbase part at all.
    assert (missing.outcome, missing.reason) == ("WAIT", "COINBASE_FEED_UNHEALTHY")
    assert missing.evidence["reference"]["code"] == "REFERENCE_MISSING"


def test_prints_before_admission_or_older_than_five_seconds_do_not_touch():
    quote = alpaca("99.99", "100.01")
    assert outcome(reference(touch=printed("100", T0 - timedelta(seconds=5))), quote)[0] == (
        "CONFIRM")
    assert outcome(reference(touch=printed("100", T0 - timedelta(seconds=5, microseconds=1))),
                   quote)[0] == "NO_TOUCH"
    assert outcome(reference(touch=printed("100", ADMITTED - timedelta(seconds=1))),
                   quote)[0] == "NO_TOUCH"
    assert outcome(reference(touch=printed("100", T0 + timedelta(seconds=1))), quote)[0] == (
        "NO_TOUCH")  # Stamped after now (a skewed clock): the next pass sees it.


# --- The controller (disposable PostgreSQL, fake paper venue) ---------------------------------

def reference_observation(mx, coinbase, sid, *, quote=None):
    """The observation the runtime builds: the coin's Coinbase record now and, when given,
    Alpaca's ``(bid, ask)`` received on the stream now."""
    engine, venue, _ = mx
    setup, current = engine._load(sid)
    levels = {k: D(v) for k, v in setup["record_json"]["levels"].items()}
    view = coinbase.feed.view(current["reference_product"], venue.now)
    record = cb.reference_record(view, levels, now=venue.now,
                                 admitted_at=datetime.fromisoformat(current["admitted_at"]))
    live = None
    if quote is not None:
        live = sc.LiveQuote(setup["symbol"], D(quote[0]), D(quote[1]), venue.now,
                            sc.STREAM_SOURCE, venue.now)
    return cb.observation(reference=record, quote=live)


def test_admission_records_the_coinbase_versions_only_for_a_coin_with_a_product(mx, coinbase):
    engine, _, _ = mx
    setups = v3_setups(mx, ["BTC/USD", "AAA/USD"])
    btc, aaa = state(engine, setups["BTC/USD"]), state(engine, setups["AAA/USD"])
    assert (btc["trigger_version"], btc["stop_breach_version"], btc["reference_product"]) == (
        "CRYPTO_COINBASE_TRIGGER_V1", "CRYPTO_STOP_BREACH_V3", "BTC-USD")
    # A coin without a Coinbase USD product keeps the Alpaca versions.
    assert (aaa["trigger_version"], aaa["stop_breach_version"]) == (
        "CRYPTO_ALPACA_TRIGGER_V1", "CRYPTO_STOP_BREACH_V2")
    assert "reference_product" not in aaa
    for key in ("gap_resume_version", "stale_print_version", "pause_wait_version", "entry_type"):
        assert btc[key] == aaa[key], key  # Every other version is recorded as before.
    # An older-style research packet keeps today's trigger and V2, product or not.
    older = state(engine, engine.admit(packet(mx, "ETH/USD")))
    assert "trigger_version" not in older and older["stop_breach_version"] == (
        "CRYPTO_STOP_BREACH_V2")
    # An engine without the Coinbase feed admits every pick exactly as before.
    engine.reference_feed = None
    [sol] = v3_setups(mx, ["SOL/USD"]).values()
    assert (state(engine, sol)["trigger_version"], state(engine, sol)["stop_breach_version"]) == (
        "CRYPTO_ALPACA_TRIGGER_V1", "CRYPTO_STOP_BREACH_V2")
    assert {"reference_product", "trigger_version", "stop_breach_version"} <= STATE_FIELDS


def test_a_coinbase_touch_enters_on_alpaca_with_its_evidence(mx, coinbase):
    engine, venue, _ = mx
    coinbase.connect("BTC-USD")
    [sid] = v3_setups(mx, ["BTC/USD"]).values()
    venue.now += timedelta(seconds=10)
    coinbase.beat()
    trade_id = coinbase.trade("BTC-USD", "99.98")  # Coinbase trades below the entry trigger.
    decision = engine.observe_trigger(
        sid, reference_observation(mx, coinbase, sid, quote=("100.00", "100.05")))
    assert decision["outcome"] == "APPROVED"
    [entry] = venue.orders_of("buy")
    assert (entry["symbol"], entry["type"], D(entry["limit_price"])) == (
        "BTC/USD", "limit", D("100.10"))  # Still a limit at max entry, on Alpaca.
    [confirmed] = rows(engine, "TRIGGER_CONFIRMED", sid)
    body = confirmed["body"]
    assert body["trigger_version"] == "CRYPTO_COINBASE_TRIGGER_V1"
    assert "trade_price" not in body  # No Alpaca print was part of the touch.
    evidence = body["crypto_trigger"]
    assert (evidence["version"], evidence["touch"]) == ("CRYPTO_COINBASE_TRIGGER_V1", "PRINT")
    touch = evidence["reference"]["touch_print"]
    assert (touch["price"], touch["trade_id"], touch["at"]) == (
        "99.98", trade_id, venue.now.isoformat())
    assert (evidence["reference"]["product_id"], evidence["reference"]["healthy"]) == (
        "BTC-USD", True)
    assert (evidence["bid"], evidence["ask"], evidence["quote_read_at"]) == (
        "100.00", "100.05", venue.now.isoformat())
    [row], [claimed] = entry_decision(engine, sid)
    context = row["context"]
    assert claimed and context["quote_at"] == venue.now.isoformat()  # Alpaca's read time.
    assert (context["trigger_version"], context["reference_product"]) == (
        "CRYPTO_COINBASE_TRIGGER_V1", "BTC-USD")
    assert verify_events(engine.repo.export_events())["valid"]


def test_waits_and_invalidations_record_the_coinbase_version(mx, coinbase):
    engine, venue, _ = mx
    products = ("ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "LTC-USD")
    coinbase.connect(*products)
    setups = v3_setups(mx, [p.replace("-", "/") for p in products])
    eth, sol, xrp, doge, ltc = (setups[p.replace("-", "/")] for p in products)
    venue.now += timedelta(seconds=2)
    coinbase.beat(*(p for p in products if p != "XRP-USD"))
    coinbase.trade("ETH-USD", "95")  # At the stop.
    coinbase.quote("SOL-USD", "94.99", "95.01")  # A fresh bid below the stop.
    coinbase.trade("DOGE-USD", "99.90")  # A touch; Alpaca's ask stays above M below.
    coinbase.trade("LTC-USD", "100.40")
    coinbase.quote("LTC-USD", "100.38", "100.41")  # Coinbase above the entry trigger.
    for sid in (eth, sol):
        assert engine.observe_trigger(sid, reference_observation(mx, coinbase, sid)) is None
    for sid, reason in ((eth, "STOP_TRADED_BEFORE_TRIGGER"), (sol, "STOP_QUOTED_BEFORE_TRIGGER")):
        current = state(engine, sid)
        assert (current["state"], current["reason"]) == ("INVALIDATED", reason)
        assert current["crypto_trigger"]["version"] == "CRYPTO_COINBASE_TRIGGER_V1"
    assert state(engine, eth)["crypto_trigger"]["reference"]["low_print"]["price"] == "95"
    assert state(engine, sol)["crypto_trigger"]["reference"]["bid"] == "94.99"
    # XRP's feed is 5 s without a heartbeat: the touch cannot confirm; the wait is recorded.
    venue.now += timedelta(seconds=3)
    coinbase.beat(*(p for p in products if p != "XRP-USD"))
    for _ in range(2):  # Once per minute.
        assert engine.observe_trigger(
            xrp, reference_observation(mx, coinbase, xrp, quote=("99.97", "99.99"))) is None
    [wait] = rows(engine, "CRYPTO_TRIGGER_WAIT", xrp)
    assert (wait["body"]["reason"], wait["body"]["trigger_version"]) == (
        "COINBASE_FEED_UNHEALTHY", "CRYPTO_COINBASE_TRIGGER_V1")
    assert wait["body"]["crypto_trigger"]["reference"]["code"] == "HEARTBEAT_STALE"
    # DOGE: Coinbase touched, Alpaca's ask is above M: the setup waits, then enters.
    coinbase.trade("DOGE-USD", "99.95")
    assert engine.observe_trigger(
        doge, reference_observation(mx, coinbase, doge, quote=("100.10", "100.12"))) is None
    [wait] = rows(engine, "CRYPTO_TRIGGER_WAIT", doge)
    assert (wait["body"]["reason"], wait["body"]["touch"]) == (
        "ALPACA_ASK_ABOVE_MAX_ENTRY", "PRINT")
    assert state(engine, doge)["state"] == "WATCHING"
    assert engine.observe_trigger(
        doge, reference_observation(mx, coinbase, doge, quote=("100.05", "100.08"))
    )["outcome"] == "APPROVED"
    # LTC: Alpaca's bid at the stop while Coinbase trades above the trigger: nothing happens.
    before = rows(engine, "STATE", ltc)
    dip = {**reference_observation(mx, coinbase, ltc), "bid": "94.90", "ask": "99.60",
           "quote_at": venue.now.isoformat(), "quote_read_at": venue.now.isoformat(),
           "quote_source": sc.STREAM_SOURCE}
    assert engine.observe_trigger(ltc, dip) is None
    assert state(engine, ltc)["state"] == "WATCHING" and rows(engine, "STATE", ltc) == before
    assert rows(engine, "CRYPTO_TRIGGER_WAIT", ltc) == []


def test_setups_admitted_before_keep_v1_and_v2_exactly(mx, coinbase):
    """A setup without the Coinbase versions ignores Coinbase: CRYPTO_ALPACA_TRIGGER_V1's fresh
    Alpaca bid at the stop invalidates, and CRYPTO_STOP_BREACH_V2's held bid sells, while
    Coinbase is healthy and trades above the stop."""
    engine, venue, _ = mx
    coinbase.connect("ETH-USD", "BTC-USD")
    engine.reference_feed = None  # Admitted before the release.
    [eth] = v3_setups(mx, ["ETH/USD"]).values()
    engine.reference_feed = coinbase.feed
    assert state(engine, eth)["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    coinbase.trade("ETH-USD", "99.40")
    assert engine.observe_trigger(eth, observation(mx, trade_price="99.40", bid="95",
                                                   ask="99.60")) is None
    assert state(engine, eth)["reason"] == "STOP_QUOTED_BEFORE_TRIGGER"
    engine.reference_feed = None
    from tests.test_stop_breach import opened

    sid, stop = opened(mx, "BTC/USD")  # An older-style pick: CRYPTO_STOP_BREACH_V2.
    engine.reference_feed = coinbase.feed
    assert state(engine, sid)["stop_breach_version"] == "CRYPTO_STOP_BREACH_V2"
    for seconds in [1] * 21 + [0]:  # 15 s held, the fallback's 5 s, then the sell.
        venue.now += timedelta(seconds=seconds)
        coinbase.beat()
        coinbase.trade("BTC-USD", "99.40")
        engine.manage(sid, observation(mx, trade_price="99.40", bid="94.90", ask="99.60"))
    assert state(engine, sid)["stop_breach_evidence"]["breach_evidence"] == "BID_HELD"
    assert stop["status"] == "canceled" and len(venue.orders_of("sell", "market")) == 1


# --- CRYPTO_STOP_BREACH_V3 --------------------------------------------------------------------

def opened_v3(mx, coinbase, symbol="BTC/USD"):
    """A report-V3 pick of a Coinbase coin under the Coinbase versions, entered on a Coinbase
    touch, filled and protected by its native stop-limit (stop 95, limit 94.99)."""
    engine, venue, _ = mx
    product = cf.product_id(symbol)
    coinbase.connect(product)
    [sid] = v3_setups(mx, [symbol]).values()
    coinbase.trade(product, "99.98")
    decision = engine.observe_trigger(
        sid, reference_observation(mx, coinbase, sid, quote=("99.99", "100.01")))
    assert decision["outcome"] == "APPROVED"
    [entry] = [o for o in venue.orders_of("buy") if o["symbol"] == symbol]
    assert engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    [stop] = [o for o in venue.orders_of("sell", "stop_limit") if o["symbol"] == symbol]
    assert state(engine, sid)["stop_breach_version"] == "CRYPTO_STOP_BREACH_V3"
    return sid, stop


def tick(mx, coinbase, sid, seconds=0, *, beat=True, **alpaca_quote):
    """One protection pass ``seconds`` later on a fresh Alpaca observation; Coinbase's
    heartbeat arrives first unless ``beat`` is False."""
    engine, venue, _ = mx
    venue.now += timedelta(seconds=seconds)
    if beat:
        coinbase.beat()
    return engine.manage(sid, observation(mx, **alpaca_quote))


def market_sells(venue):
    return venue.orders_of("sell", "market")


def test_an_alpaca_bid_dip_without_a_coinbase_trade_never_breaches(mx, coinbase):
    """The false stops of 2026-09-28 (UNI, LTC, GRT): Alpaca's bid sat at the stop with no trade
    at or below it on Alpaca or Coinbase. Under CRYPTO_STOP_BREACH_V2 a bid held 15 s sells at
    market 5 s later; under V3, while Coinbase is healthy, only a Coinbase print at or below the
    stop is a breach."""
    engine, venue, _ = mx
    sid, stop = opened_v3(mx, coinbase)
    for second in range(30):
        venue.now += timedelta(seconds=1)
        coinbase.beat()
        coinbase.trade("BTC-USD", "99.40")  # Coinbase keeps trading well above the stop.
        plan = engine.manage(sid, observation(
            mx, trade_price="99.40", bid="95" if second % 2 else "94.90", ask="99.60"))
    current = state(engine, sid)
    assert plan.state == "PROTECTED" and stop["status"] == "new" and not market_sells(venue)
    assert current.get("stop_breached_at") is None and current.get("exit_requested") is None
    assert current["stop_breach_marks"]["bid_since"] is None  # No held-bid mark at all.
    assert not rows(engine, stop_breach.ESTABLISHED_EVENT, sid)


def test_an_alpaca_print_at_the_stop_is_no_breach_while_coinbase_is_healthy(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened_v3(mx, coinbase)
    for _ in range(8):
        venue.now += timedelta(seconds=1)
        coinbase.beat()
        coinbase.trade("BTC-USD", "95.40")
        plan = engine.manage(sid, observation(mx, trade_price="94.90", bid="94.80",
                                              ask="95.60"))
    assert plan.state == "PROTECTED" and stop["status"] == "new" and not market_sells(venue)
    assert state(engine, sid).get("stop_breached_at") is None


def test_a_coinbase_print_at_the_stop_establishes_the_breach_and_sells_after_5_s(mx, coinbase):
    engine, venue, _ = mx
    sid, stop = opened_v3(mx, coinbase)
    venue.now += timedelta(seconds=5)
    coinbase.beat()
    traded = venue.now - timedelta(seconds=2)  # Delivered two seconds after the trade.
    trade_id = coinbase.trade("BTC-USD", "95", at=traded)
    engine.manage(sid, observation(mx, trade_price="95.30", bid="95.20", ask="95.25"))
    established = venue.now
    current = state(engine, sid)
    assert current["stop_breached_at"] == established.isoformat()
    evidence = current["stop_breach_evidence"]
    assert evidence == {
        "version": "CRYPTO_STOP_BREACH_V3",
        "lifecycle_id": current["lifecycle_id"],
        "stop": "95",
        "stop_since": current["stop_breach_marks"]["stop_since"],
        "breach_evidence": "COINBASE_PRINT",
        "fallback": False,
        "evidence_at": traded.isoformat(),
        "evidence_price": "95",
        "trade_id": trade_id,
        "received_at": established.isoformat(),
        "print_age_seconds": "2.0",
        "reference": {"provider": "COINBASE_EXCHANGE", "product_id": "BTC-USD", "healthy": True,
                      "code": None, "as_of": established.isoformat(),
                      "heartbeat_at": established.isoformat(),
                      "heartbeat_received_at": established.isoformat()},
        "established_at": established.isoformat(),
        "fallback_seconds": 5,
        "fallback_at": (established + timedelta(seconds=5)).isoformat(),
    }
    [event] = rows(engine, stop_breach.ESTABLISHED_EVENT, sid)
    assert event["body"] == evidence
    assert event["idempotency_key"] == (
        f"stop-breach-established:{sid}:{current['lifecycle_id']}:{evidence['stop_since']}")
    plan = tick(mx, coinbase, sid, 4, bid="95.20", ask="95.25")
    assert plan.state == "PROTECTED" and stop["status"] == "new"
    plan = tick(mx, coinbase, sid, 1, bid="95.20", ask="95.25")  # 5 s: still open.
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_LIMIT_NOT_FILLED")
    assert stop["status"] == "canceled"
    plan = tick(mx, coinbase, sid)
    assert (plan.state, plan.reason) == ("EXIT_REQUIRED", "STOP_LIMIT_NOT_FILLED")
    [close] = market_sells(venue)
    assert engine.ingest(venue.fill(close["id"], close["qty"], price="95.10"))
    tick(mx, coinbase, sid)
    closed = state(engine, sid)
    assert (closed["state"], closed["reason"]) == ("CLOSED", "STOP_LIMIT_NOT_FILLED")
    assert verify_events(engine.repo.export_events())["valid"]


def test_an_unhealthy_coinbase_feed_falls_back_to_v2_and_back(mx, coinbase):
    """Protection never stops for a feed outage: V2's Alpaca evidence applies while Coinbase is
    unhealthy (here a silent feed), and the held-bid mark belongs to that fallback only."""
    engine, venue, _ = mx
    sid, stop = opened_v3(mx, coinbase)
    silent = venue.now + timedelta(seconds=4)
    tick(mx, coinbase, sid, 4, beat=False, bid="94.90", ask="95.00")  # 4 s without a heartbeat.
    assert state(engine, sid)["stop_breach_marks"]["bid_since"] == silent.isoformat()
    tick(mx, coinbase, sid, 5, bid="94.90", ask="95.00")  # Coinbase is back: the mark is gone.
    marks = state(engine, sid)["stop_breach_marks"]
    assert (marks["bid_since"], marks["bid"], marks["bid_quote_at"]) == (None, None, None)
    tick(mx, coinbase, sid, 4, beat=False, bid="94.90", ask="95.00")  # Silent again.
    since = venue.now
    for _ in range(14):
        tick(mx, coinbase, sid, 1, beat=False, bid="94.90", ask="95.00")
    assert state(engine, sid).get("stop_breached_at") is None  # 14 s of fallback.
    tick(mx, coinbase, sid, 1, beat=False, bid="94.85", ask="94.95")  # 15 s.
    evidence = state(engine, sid)["stop_breach_evidence"]
    assert {k: evidence[k] for k in ("version", "breach_evidence", "fallback", "held_since",
                                     "held_seconds", "first_bid", "evidence_price")} == {
        "version": "CRYPTO_STOP_BREACH_V3", "breach_evidence": "BID_HELD", "fallback": True,
        "held_since": since.isoformat(), "held_seconds": "15.0", "first_bid": "94.90",
        "evidence_price": "94.85"}
    assert (evidence["reference"]["healthy"], evidence["reference"]["code"]) == (
        False, "HEARTBEAT_STALE")
    assert tick(mx, coinbase, sid, 5, beat=False, bid="94.85", ask="94.95").reason == (
        "STOP_LIMIT_NOT_FILLED")
    assert stop["status"] == "canceled"


def test_a_disconnected_feed_falls_back_to_an_alpaca_print_at_the_stop(mx, coinbase):
    engine, venue, _ = mx
    sid, _ = opened_v3(mx, coinbase)
    coinbase.disconnect()
    tick(mx, coinbase, sid, 1, beat=False, trade_price="94.95", bid="95.10", ask="95.20")
    evidence = state(engine, sid)["stop_breach_evidence"]
    assert (evidence["breach_evidence"], evidence["fallback"], evidence["evidence_price"]) == (
        "TRADE_PRINT", True, "94.95")
    assert (evidence["reference"]["healthy"], evidence["reference"]["code"]) == (
        False, "DISCONNECTED")
    # Without any feed at all the fallback applies the same way (fail-safe).
    engine.reference_feed = None
    assert stop_breach.evaluate_v3(
        {**state(engine, sid), "stop_breached_at": None, "stop_breach_evidence": None},
        observation=observation(mx, trade_price="94.90"), bid=D("95.1"),
        reference=None, now=venue.now)[1]["reference"]["code"] == "FEED_NOT_CONFIGURED"


def test_a_stop_change_discards_the_v3_marks(mx, coinbase):
    engine, venue, _ = mx
    sid, _ = opened_v3(mx, coinbase)
    venue.now += timedelta(seconds=1)
    coinbase.beat()
    coinbase.trade("BTC-USD", "95")
    engine.manage(sid, observation(mx, bid="95.10", ask="95.15"))
    assert state(engine, sid)["stop_breach_evidence"]["stop"] == "95"
    # Two seconds later the price is back at 98 and the stop is raised to 96.
    venue.now += timedelta(seconds=2)
    with engine.store.transaction() as conn:
        engine.store.transition(conn, sid, "OPEN", stop="96")
    tick(mx, coinbase, sid, 0, bid="98", ask="98.05")
    raised = state(engine, sid)
    assert raised["stop_breached_at"] is None and raised["stop_breach_evidence"] is None
    measured = venue.now
    assert raised["stop_breach_marks"] == {"stop": "96", "stop_since": measured.isoformat(),
                                           "bid_since": None, "bid": None, "bid_quote_at": None}
    for _ in range(6):  # Past five seconds after the old stop's breach: nothing sells.
        coinbase.trade("BTC-USD", "98")
        tick(mx, coinbase, sid, 1, bid="98", ask="98.05")
    assert state(engine, sid).get("exit_requested") is None and not market_sells(venue)
    # A Coinbase print at the new stop traded before it was first measured does not count.
    coinbase.trade("BTC-USD", "95.90", at=measured - timedelta(milliseconds=100))
    tick(mx, coinbase, sid, 0, bid="98", ask="98.05")
    assert state(engine, sid).get("stop_breached_at") is None
    coinbase.trade("BTC-USD", "96")
    tick(mx, coinbase, sid, 0, bid="97", ask="97.05")
    events = [e["body"] for e in rows(engine, stop_breach.ESTABLISHED_EVENT, sid)]
    assert [(e["stop"], e["breach_evidence"]) for e in events] == [
        ("95", "COINBASE_PRINT"), ("96", "COINBASE_PRINT")]


@pytest.mark.parametrize(("change", "counts"), [
    ({}, True),                                     # At the stop, 1 s old.
    ({"price": "95.01"}, False),                    # Above it.
    ({"age": 5}, True),
    ({"age": 5.001}, False),
    ({"before_stop": True}, False),                 # Traded before the stop's first measure.
])
def test_what_a_coinbase_print_proves(change, counts):
    now = T0
    change = dict(change)
    traded = now - timedelta(seconds=change.pop("age", 1))
    since = now - timedelta(seconds=10)
    if change.pop("before_stop", False):
        since = traded + timedelta(milliseconds=1)
    marks = {"stop": "95", "stop_since": since.isoformat(), "bid_since": None, "bid": None,
             "bid_quote_at": None}
    view = cf.ReferenceView("BTC-USD", now, True, None, connected=True, acknowledged=True,
                            heartbeat_at=now, heartbeat_received_at=now, prints=(
                                cf.ReferencePrint(D(change.get("price", "95")), traded, now, "7"),))
    state_ = {"stop_breach_version": "CRYPTO_STOP_BREACH_V3", "stop": "95", "lifecycle_id": "L",
              "state": "OPEN", "stop_breach_marks": marks}
    changes, breach = stop_breach.evaluate_v3(
        state_, observation={"feed_healthy": True}, bid=D("94"), reference=view, now=now)
    assert (breach is not None) == counts  # Alpaca's bid at 94 never counts while healthy.
    if counts:
        assert (breach["breach_evidence"], breach["fallback"]) == ("COINBASE_PRINT", False)
    else:
        assert changes == {}


# --- The runtime ----------------------------------------------------------------------------

def test_the_runtime_admits_a_coinbase_pick_only_once_its_feed_is_healthy(mx, market, coinbase):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    publish_v3(mx, [v3_pick(0, "BTC/USD", venue.now)], run_slot=old)
    run = system_runtime(mx, market, ["BTC/USD"])
    assert run._reference_products() == {"BTC-USD"}  # The offered pick's product.
    stream(run, "BTC/USD", at=venue.now)
    run.execution_once()
    assert engine.store.active() == []  # Coinbase not yet subscribed: the pick waits.
    assert rows(engine, "RUNTIME_ADMISSION_REFUSED") == []  # Neither refused nor declined.
    coinbase.connect("BTC-USD")
    run.execution_once()
    [setup] = engine.store.active()
    assert (setup["state"]["trigger_version"], setup["state"]["reference_product"]) == (
        "CRYPTO_COINBASE_TRIGGER_V1", "BTC-USD")
    assert run.error is None


def test_the_runtime_enters_on_a_coinbase_print_confirmed_by_the_alpaca_stream(
    mx, market, coinbase,
):
    engine, venue, _ = mx
    coinbase.connect("BTC-USD")
    [sid] = v3_setups(mx, ["BTC/USD"]).values()
    run = system_runtime(mx, market, ["BTC/USD"])
    start = venue.now
    # Alpaca's ask touches the trigger (a touch under CRYPTO_ALPACA_TRIGGER_V1); Coinbase trades
    # and quotes above it.
    stream(run, "BTC/USD", "99.97", "99.99", at=start)
    coinbase.trade("BTC-USD", "100.20")
    coinbase.quote("BTC-USD", "100.18", "100.21")
    run.execution_once()
    assert entry_decision(engine, sid)[0] == [] and market.data.requests == []
    venue.now = start + timedelta(seconds=1)
    coinbase.beat()
    coinbase.trade("BTC-USD", "99.98")  # Coinbase touches.
    stream(run, "BTC/USD", "99.97", "99.99", at=venue.now)
    run.execution_once()
    [decision], [claimed] = entry_decision(engine, sid)
    assert decision["outcome"] == "APPROVED" and claimed
    evidence = rows(engine, "TRIGGER_CONFIRMED", sid)[0]["body"]["crypto_trigger"]
    assert (evidence["touch"], evidence["reference"]["touch_print"]["price"]) == ("PRINT", "99.98")
    assert (evidence["quote_source"], evidence["quote_read_basis"]) == (
        "ALPACA_STREAM", "STREAM_RECEIPT")
    assert market.data.requests == [] and run.error is None


def test_the_runtime_records_an_unhealthy_feed_wait_once_a_minute(mx, market, coinbase):
    engine, venue, _ = mx
    coinbase.connect("BTC-USD")
    [sid] = v3_setups(mx, ["BTC/USD"]).values()
    run = system_runtime(mx, market, ["BTC/USD"])
    coinbase.disconnect()
    for _ in range(3):
        venue.now += timedelta(seconds=1)
        run.execution_once()
    [wait] = rows(engine, "CRYPTO_TRIGGER_WAIT", sid)
    assert (wait["body"]["reason"], wait["body"]["crypto_trigger"]["reference"]["code"]) == (
        "COINBASE_FEED_UNHEALTHY", "DISCONNECTED")
    assert state(engine, sid)["state"] == "WATCHING" and market.data.requests == []


def test_alpaca_prints_are_consumed_unevaluated_for_the_coinbase_version(mx, market, coinbase):
    engine, venue, _ = mx
    coinbase.connect("BTC-USD")
    setups = v3_setups(mx, ["BTC/USD", "AAA/USD"])
    btc, aaa = setups["BTC/USD"], setups["AAA/USD"]
    run = system_runtime(mx, market, ["BTC/USD", "AAA/USD"])
    venue.now += timedelta(seconds=10)
    coinbase.beat()
    late = (venue.now - timedelta(seconds=6)).isoformat()  # At or below T, six seconds late.
    run.market_message("CRYPTO", {"T": "t", "S": "BTC/USD", "p": "99", "i": 1, "t": late})
    for trade_id, symbol in enumerate(("BTC/USD", "AAA/USD"), start=2):  # Below the stop.
        run.market_message("CRYPTO", {"T": "t", "S": symbol, "p": "94", "i": trade_id,
                                      "t": venue.now.isoformat()})
    run.execution_once()
    assert run._pending_trades() == []
    assert state(engine, btc)["state"] == "WATCHING" and not state(engine, btc).get("revoked")
    assert [r["body"]["reason"] for r in rows(engine, "MARKET_PRINT_CONSUMED", btc)] == [
        "ALPACA_PRINT_NOT_TRIGGER_EVIDENCE"] * 2
    # CRYPTO_ALPACA_TRIGGER_V1, unchanged: Alpaca's print at the stop invalidates.
    assert state(engine, aaa)["reason"] == "STOP_TRADED_BEFORE_TRIGGER"


def test_the_runtime_protection_pass_reads_coinbase_for_v3(mx, market, coinbase):
    engine, venue, _ = mx
    sid, stop = opened_v3(mx, coinbase)
    run = system_runtime(mx, market, ["BTC/USD"])
    venue.now += timedelta(seconds=1)
    coinbase.beat()
    stream(run, "BTC/USD", "94.80", "95.20", at=venue.now)  # Alpaca's bid below the stop.
    run.execution_once()
    assert state(engine, sid).get("stop_breached_at") is None
    coinbase.trade("BTC-USD", "94.98")
    run.execution_once()
    evidence = state(engine, sid)["stop_breach_evidence"]
    assert (evidence["breach_evidence"], evidence["evidence_price"]) == ("COINBASE_PRINT", "94.98")
    venue.now += timedelta(seconds=5)
    coinbase.beat()
    stream(run, "BTC/USD", "94.80", "95.20", at=venue.now)
    run.execution_once()
    assert state(engine, sid)["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    assert stop["status"] == "canceled" and run.error is None


def test_the_status_reports_the_reference_feed(mx, market, coinbase):
    engine, venue, _ = mx
    run = system_runtime(mx, market, [])
    coinbase.connect("BTC-USD", "ETH-USD")
    run._reference_wanted = (0.0, frozenset({"BTC-USD", "ETH-USD"}))  # As the feed loop reads.
    venue.now += timedelta(seconds=4)
    coinbase.beat("BTC-USD")
    status = run.status()
    feed = status["reference_feed"]
    assert (feed["connected"], feed["requested"], feed["healthy"], feed["unhealthy"]) == (
        True, ["BTC-USD", "ETH-USD"], ["BTC-USD"], {"ETH-USD": "HEARTBEAT_STALE"})
    assert (feed["available"], feed["wanted"], feed["unhealthy_since"]) == (
        True, ["BTC-USD", "ETH-USD"], venue.now.isoformat())
    assert (feed["heartbeat_max_age_seconds"], feed["clock_tolerance_seconds"],
            feed["tape_gap_hold_seconds"]) == (3, 3, 10)
    assert public_status(status)["reference_feed"] == feed and "reference_feed" in STATUS_FIELDS
    # Ages move every call; only health is a change for the heartbeat record.
    later = {**status, "reference_feed": {**feed, "as_of": "x", "last_message_age_seconds": "9"}}
    assert ManagedRuntime._heartbeat_signature(later) == ManagedRuntime._heartbeat_signature(
        status)
    sick = {**status, "reference_feed": {**feed, "healthy": [], "unhealthy": {
        "BTC-USD": "DISCONNECTED", "ETH-USD": "DISCONNECTED"}}}
    assert ManagedRuntime._heartbeat_signature(sick) != ManagedRuntime._heartbeat_signature(
        status)
    engine.reference_feed = None
    assert run.status()["reference_feed"] is None


def test_the_launch_gives_the_engine_the_coinbase_feed(monkeypatch):
    from tests.test_managed_engineering import factory

    captured = {}
    factory(monkeypatch, "DISABLED", captured=captured)
    feed = captured["reference_feed"]
    assert isinstance(feed, cf.CoinbaseFeed)
    assert feed.connector is cf.CoinbaseStreamConnection  # The one public endpoint.


def test_start_runs_the_feed_loop_and_stop_closes_its_connection(monkeypatch):
    import catalyst_lab.managed_runtime as runtime_module
    from tests.test_managed_runtime import runtime

    launched = []

    class Thread:
        def __init__(self, target, args, daemon):
            launched.append(getattr(target, "__name__", None))

        def start(self):
            pass

        def is_alive(self):
            return False

        def join(self, timeout):
            pass

    monkeypatch.setattr(runtime_module.threading, "Thread", Thread)
    run = runtime()
    closed = []
    run.execution.reference_feed = type("Feed", (), {
        "session": lambda self, **_: cf.STOPPED, "close": lambda self: closed.append(True),
        "status": lambda self, now: {"connected": False}})()
    run.start()
    assert "_reference_stream_loop" in launched and run.expected_workers == len(launched) == 9
    run.stop()
    assert closed == [True]
    # Without the feed the runtime starts exactly the workers it started before.
    launched.clear()
    other = runtime()
    other.start()
    assert "_reference_stream_loop" not in launched and other.expected_workers == 8


def test_the_watchdog_raises_reference_feed_unhealthy_after_five_minutes(mx, market, coinbase):
    from catalyst_lab.managed_ops import reference_feed_alarms

    engine, venue, _ = mx
    run = system_runtime(mx, market, [])
    run._reference_wanted = (0.0, frozenset({"BTC-USD"}))  # A pick needs BTC; no connection.
    first = run.status()["reference_feed"]
    assert (first["connected"], first["unhealthy_since"]) == (False, venue.now.isoformat())
    assert reference_feed_alarms(first, venue.now + timedelta(seconds=300)) == []
    later = venue.now + timedelta(seconds=301)
    assert reference_feed_alarms(first, later) == ["REFERENCE_FEED_UNHEALTHY"]
    venue.now += timedelta(seconds=10)
    assert run.status()["reference_feed"]["unhealthy_since"] == first["unhealthy_since"]
    coinbase.connect("BTC-USD")  # Healthy again: the time clears.
    assert run.status()["reference_feed"]["unhealthy_since"] is None
    assert reference_feed_alarms(None, later) == []  # No feed, or an older app.
    for broken in ({"available": False}, "x", {"available": True, "unhealthy_since": "x"},
                   {"available": True,
                    "unhealthy_since": (later + timedelta(seconds=1)).isoformat()}):
        assert reference_feed_alarms(broken, later) in (
            ["REFERENCE_FEED_STATUS_UNAVAILABLE"], ["REFERENCE_FEED_UNHEALTHY"])
    assert reference_feed_alarms({"available": True, "unhealthy_since": None}, later) == []
