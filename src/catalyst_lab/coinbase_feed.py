"""Coinbase Exchange's public market data: the reference market of ``CRYPTO_COINBASE_TRIGGER_V1``
and ``CRYPTO_STOP_BREACH_V3`` (owner approval 2026-09-29 of the operating session's plan: "use
Coinbase's public market prices as the reference for crypto entry triggers and stop breaches,
while orders stay on Alpaca paper").

The feed reads and never trades. It connects to the fixed public endpoint ``ENDPOINT`` with no
key and no credential, sends only ``subscribe`` and ``unsubscribe`` messages for the public
``heartbeat``, ``ticker`` and ``matches`` channels (docs: ``DOCS``), and places, authenticates or
amends nothing. Every order stays on Alpaca paper under its exact one-use risk authorization.

Products. An Alpaca pair ``X/USD`` maps to Coinbase's ``X-USD`` when that product is in
``COINBASE_USD_PRODUCTS``: the 33 Alpaca USD pairs of 2026-09-26 (``ALPACA_CRYPTO_SECTORS_V1``),
each checked against Coinbase's public product list on 2026-09-29 (every one listed ``online``,
trading enabled). Any other symbol has no reference product and keeps the Alpaca versions.

Per product the feed keeps, each with its Coinbase time and its receipt (read) time:
- the latest print and the prints received in the last ``PRINT_RETENTION_SECONDS`` (at most
  ``MAX_RETAINED_PRINTS``) from the ``matches`` channel (``last_match`` at subscription, then
  every ``match``), so a print between two protection passes is not lost to a later one;
- the best bid and ask from the ``ticker`` channel. Coinbase sends a ticker only with a trade,
  so a quote is known current as of the trade it reports (its own ``time``); the ticker sent at
  subscription can be minutes old. The trigger version therefore bounds both times;
- the last heartbeat (Coinbase sends one per product every second, with its ``last_trade_id``).

Health, per product, at an instant (``health``): the product is healthy when the connection is
open, this session's ``subscriptions`` acknowledgment lists it on all three channels, a
heartbeat for it was received at most ``HEARTBEAT_MAX_AGE_SECONDS`` ago, that heartbeat's
Coinbase time is within ``CLOCK_TOLERANCE_SECONDS`` of its receipt (a lagging feed or a skewed
clock is unhealthy), and no trade-tape gap was seen in the last ``TAPE_GAP_HOLD_SECONDS``.
Coinbase documents that the ``matches`` channel can drop messages and that the heartbeat's
``last_trade_id`` detects it: a heartbeat whose ``last_trade_id`` is ahead of the matches
received, a jump in match trade ids, or an unreadable match is a tape gap. Otherwise unhealthy,
with a code (``UNHEALTHY_CODES``).

The session (``CoinbaseFeed.session``) subscribes to what ``wanted()`` returns, follows its
changes, and ends on a malformed frame, an unexpected provider error, an unacknowledged
subscription, or silence: no message at all for ``SILENCE_LIMIT_SECONDS`` while a product is
acknowledged (heartbeats come every second, so the connection is open but delivering nothing).
The managed runtime reconnects with the Alpaca streams' bounded backoff
(``managed_runtime.reconnect_wait``). A product Coinbase refuses (``Failed to subscribe``: not a
valid product, delisted) is unhealthy for that session while the others stay subscribed.
"""

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from websockets.sync.client import reconnect

from catalyst_lab.runtime import WIRE_LOG

PROVIDER = "COINBASE_EXCHANGE"
ENDPOINT = "wss://ws-feed.exchange.coinbase.com"
CHANNELS = ("heartbeat", "ticker", "matches")
DOCS = (
    "https://docs.cdp.coinbase.com/exchange/websocket-feed/overview",
    "https://docs.cdp.coinbase.com/exchange/websocket-feed/channels",
)

# Coinbase USD products of the 33 Alpaca USD pairs (ALPACA_CRYPTO_SECTORS_V1), each listed
# ``online`` with trading enabled in Coinbase's public product list on this date.
PRODUCTS_VERIFIED_ON = "2026-09-29"
COINBASE_USD_PRODUCTS = frozenset({
    "AAVE-USD", "ADA-USD", "ARB-USD", "AVAX-USD", "BAT-USD", "BCH-USD", "BONK-USD", "BTC-USD",
    "CRV-USD", "DOGE-USD", "DOT-USD", "ETH-USD", "FIL-USD", "GRT-USD", "HYPE-USD", "LDO-USD",
    "LINK-USD", "LTC-USD", "ONDO-USD", "PAXG-USD", "PEPE-USD", "POL-USD", "RENDER-USD",
    "SHIB-USD", "SKY-USD", "SOL-USD", "SUSHI-USD", "TRUMP-USD", "UNI-USD", "WIF-USD", "XRP-USD",
    "XTZ-USD", "YFI-USD",
})

# The health rule's limits (seconds). Coinbase sends one heartbeat per product every second.
HEARTBEAT_MAX_AGE_SECONDS = 3
CLOCK_TOLERANCE_SECONDS = 3
TAPE_GAP_HOLD_SECONDS = 10
# Prints are kept this long after their receipt, at most this many per product (oldest first).
PRINT_RETENTION_SECONDS = 30
MAX_RETAINED_PRINTS = 2000
# Frames buffered by the connection (a burst on many products must not stall the reader).
MAX_QUEUE = 1024
# A session with an acknowledged product that receives nothing this long ends and reconnects.
SILENCE_LIMIT_SECONDS = 10

# Health codes.
FEED_NOT_CONFIGURED = "FEED_NOT_CONFIGURED"
NOT_A_COINBASE_PRODUCT = "NOT_A_COINBASE_PRODUCT"
DISCONNECTED = "DISCONNECTED"
NOT_REQUESTED = "NOT_REQUESTED"
SUBSCRIPTION_REFUSED = "SUBSCRIPTION_REFUSED"
SUBSCRIPTION_UNACKNOWLEDGED = "SUBSCRIPTION_UNACKNOWLEDGED"
NO_HEARTBEAT = "NO_HEARTBEAT"
HEARTBEAT_STALE = "HEARTBEAT_STALE"
CLOCK_SKEW = "CLOCK_SKEW"
TRADE_TAPE_GAP = "TRADE_TAPE_GAP"
READ_FAILED = "REFERENCE_READ_FAILED"
UNHEALTHY_CODES = frozenset({
    FEED_NOT_CONFIGURED, NOT_A_COINBASE_PRODUCT, DISCONNECTED, NOT_REQUESTED,
    SUBSCRIPTION_REFUSED, SUBSCRIPTION_UNACKNOWLEDGED, NO_HEARTBEAT, HEARTBEAT_STALE, CLOCK_SKEW,
    TRADE_TAPE_GAP, READ_FAILED,
})

# How a session ended without an error.
STOPPED, NOTHING_WANTED = "STOPPED", "NOTHING_WANTED"

_SYMBOL = re.compile(r"([A-Z0-9]{1,16})/USD")
_PRODUCT = re.compile(r"[A-Z0-9]{1,16}-USD")
# The session clock (a module name, so a test can stand in for it).
_monotonic = time.monotonic


def product_id(symbol):
    """Coinbase's product for an Alpaca pair ``X/USD`` (``X-USD``), or None without one."""
    match = _SYMBOL.fullmatch(symbol) if isinstance(symbol, str) else None
    if match is None:
        return None
    product = match.group(1) + "-USD"
    return product if product in COINBASE_USD_PRODUCTS else None


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("REFERENCE_TIME_INVALID")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("REFERENCE_TIME_INVALID")
    return result


def _price(value):
    if not isinstance(value, str):
        raise ValueError("REFERENCE_PRICE_INVALID")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("REFERENCE_PRICE_INVALID") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("REFERENCE_PRICE_INVALID")
    return result


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("REFERENCE_COUNT_INVALID")
    return value


def _text(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _seconds(later, earlier):
    return str(Decimal(str((later - earlier).total_seconds())))


@dataclass(frozen=True)
class ReferencePrint:
    """One Coinbase trade: price, trade time (Coinbase's), receipt time and trade id."""

    price: Decimal
    traded_at: datetime
    received_at: datetime
    trade_id: str

    def record(self, now=None):
        value = {"price": str(self.price), "at": self.traded_at.isoformat(),
                 "received_at": self.received_at.isoformat(), "trade_id": self.trade_id}
        if now is not None:
            value["age_seconds"] = _seconds(now, self.traded_at)
        return value


@dataclass(frozen=True)
class ReferenceView:
    """One product's reference market as of ``as_of``: its health and the data behind it.

    ``prints`` are the retained prints in receipt order: those received in the last
    ``PRINT_RETENTION_SECONDS``, at most ``MAX_RETAINED_PRINTS`` of them.
    """

    product_id: str | None
    as_of: datetime
    healthy: bool
    code: str | None
    connected: bool = False
    acknowledged: bool = False
    heartbeat_at: datetime | None = None
    heartbeat_received_at: datetime | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    quote_at: datetime | None = None
    quote_received_at: datetime | None = None
    last_print: ReferencePrint | None = None
    prints: tuple = ()

    @classmethod
    def unavailable(cls, product, now, code):
        return cls(product, now, False, code)

    def lowest_print(self, *, traded_from, traded_to):
        """The lowest-priced retained print traded in ``[traded_from, traded_to]`` (the latest
        of equal prices), or None."""
        found = None
        for item in self.prints:
            if traded_from <= item.traded_at <= traded_to and (
                    found is None or item.price <= found.price):
                found = item
        return found

    def latest_print_at_or_below(self, price, *, traded_from, traded_to):
        """The most recently traded retained print at or below ``price`` traded in
        ``[traded_from, traded_to]``, or None."""
        found = None
        for item in self.prints:
            if (item.price <= price and traded_from <= item.traded_at <= traded_to
                    and (found is None or item.traded_at >= found.traded_at)):
                found = item
        return found

    def health(self):
        """The health facts a record carries (codes and times only)."""
        return {
            "provider": PROVIDER,
            "product_id": self.product_id,
            "healthy": self.healthy,
            "code": self.code,
            "as_of": self.as_of.isoformat(),
            "heartbeat_at": _text(self.heartbeat_at),
            "heartbeat_received_at": _text(self.heartbeat_received_at),
        }


def read_view(feed, product, now):
    """``feed.view(product, now)``, or an unavailable (unhealthy) view without a feed or when the
    read fails: never an exception, so a reader's protection pass always runs."""
    if feed is None:
        return ReferenceView.unavailable(product, now, FEED_NOT_CONFIGURED)
    try:
        return feed.view(product, now)
    except Exception:
        return ReferenceView.unavailable(product, now, READ_FAILED)


class _Product:
    """One product's state; mutated only under the feed's lock."""

    def __init__(self):
        self.heartbeat_at = self.heartbeat_received_at = None
        self.trade_id = None  # The highest trade id this session has seen (matches, heartbeats).
        self.tape_gap_at = None
        self.bid = self.ask = self.quote_at = self.quote_received_at = None
        self.quote_sequence = None
        self.last_print = None
        self.prints = deque()
        self.print_ids = set()

    def new_session(self):
        self.heartbeat_at = self.heartbeat_received_at = None
        self.trade_id = None
        self.tape_gap_at = None
        self.quote_sequence = None  # Sequence numbers restart their comparison per session.

    def prune(self, now):
        horizon = now - timedelta(seconds=PRINT_RETENTION_SECONDS)
        while self.prints and (self.prints[0].received_at < horizon
                               or len(self.prints) > MAX_RETAINED_PRINTS):
            self.print_ids.discard(self.prints.popleft().trade_id)


class CoinbaseStreamConnection(reconnect):
    """The one public Coinbase endpoint, with the library's redirects disabled."""

    def __init__(self, uri, **kwargs):
        if uri != ENDPOINT:
            raise ValueError("Only the fixed Coinbase public market-data feed is permitted")
        super().__init__(uri, **kwargs)

    def process_redirect(self, exc):
        return exc


def _request(kind, products):
    """The only messages the feed ever sends."""
    return json.dumps({"type": kind, "product_ids": sorted(products), "channels": list(CHANNELS)})


def _frame(raw):
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("REFERENCE_FRAME_INVALID") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ValueError("REFERENCE_FRAME_INVALID")
    return message


def _refusal_code(reason):
    if reason.endswith(" is not a valid product"):
        return "NOT_A_VALID_PRODUCT"
    if reason.endswith(" is delisted"):
        return "PRODUCT_DELISTED"
    return "SUBSCRIBE_REFUSED"


class CoinbaseFeed:
    """The public reference feed's state and its connection sessions (thread-safe)."""

    def __init__(self, *, clock, connector=CoinbaseStreamConnection):
        self.clock, self.connector = clock, connector
        self._lock = threading.Lock()
        self._socket = None
        self._connected = False
        self._sessions = 0
        self._connected_at = self._disconnected_at = None
        self._last_message_at = None
        self._requested = frozenset()
        self._acknowledged = frozenset()
        self._refused = {}
        self._products = {}
        self._tape_gaps = 0

    # --- Messages -------------------------------------------------------------------------

    def begin(self, socket=None):
        """A new connection: acknowledgments, heartbeats and tape baselines start afresh; the
        retained prints and quotes stay (their own times bound their use)."""
        with self._lock:
            self._socket = socket
            self._connected = True
            self._sessions += 1
            self._connected_at = self.clock()
            self._requested, self._acknowledged, self._refused = frozenset(), frozenset(), {}
            for product in self._products.values():
                product.new_session()

    def end(self):
        with self._lock:
            self._socket = None
            if self._connected:
                self._disconnected_at = self.clock()
            self._connected = False
            self._acknowledged = frozenset()

    def close(self):
        """Close the open connection (runtime shutdown); its session then ends."""
        with self._lock:
            socket = self._socket
        if socket is not None:
            socket.close()

    def request(self, products):
        """The products this session asked for; state of a product no longer asked for goes."""
        products = frozenset(products)
        with self._lock:
            self._requested = products
            self._refused = {p: c for p, c in self._refused.items() if p in products}
            for gone in set(self._products) - products:
                del self._products[gone]
            for product in products:
                self._products.setdefault(product, _Product())

    def settled(self):
        """Every requested product is acknowledged on all channels, or refused by Coinbase."""
        with self._lock:
            return bool(self._requested) and self._requested <= (
                self._acknowledged | set(self._refused))

    def handle(self, message, *, received_at):
        """Apply one decoded message received at ``received_at``. Raises (ending the session)
        on a malformed acknowledgment, an unexpected provider error or an unrequested product
        in an acknowledgment; an unusable product message only affects its product."""
        kind = message.get("type")
        with self._lock:
            self._last_message_at = received_at
            if kind == "subscriptions":
                self._subscriptions(message)
            elif kind == "error":
                self._error(message)
            elif kind in {"heartbeat", "ticker", "match", "last_match"}:
                product = self._products.get(message.get("product_id"))
                if product is None or message.get("product_id") not in self._requested:
                    return  # Not asked for in this session (a late message after unsubscribe).
                if kind == "heartbeat":
                    self._heartbeat(product, message, received_at)
                elif kind == "ticker":
                    self._ticker(product, message, received_at)
                else:
                    self._match(product, message, received_at)
            # Any other type (status, auction and the like) carries nothing the rules read.

    def _subscriptions(self, message):
        channels = message.get("channels")
        if not isinstance(channels, list):
            raise ValueError("REFERENCE_SUBSCRIPTION_INVALID")
        listed = {}
        for channel in channels:
            if (not isinstance(channel, dict) or not isinstance(channel.get("name"), str)
                    or not isinstance(channel.get("product_ids"), list)
                    or any(not isinstance(p, str) for p in channel["product_ids"])):
                raise ValueError("REFERENCE_SUBSCRIPTION_INVALID")
            listed[channel["name"]] = set(channel["product_ids"])
        everything = set().union(*listed.values()) if listed else set()
        if not everything <= self._requested | self._acknowledged:
            raise ValueError("REFERENCE_SUBSCRIPTION_MISMATCH")
        self._acknowledged = frozenset(
            set.intersection(*(listed.get(name, set()) for name in CHANNELS)))

    def _error(self, message):
        reason = message.get("reason")
        if message.get("message") == "Failed to subscribe" and isinstance(reason, str):
            product = reason.split(" ", 1)[0]
            if product in self._requested:
                self._refused[product] = _refusal_code(reason)
                return
        raise ValueError("REFERENCE_FEED_PROVIDER_ERROR")

    def _gap(self, product, received_at):
        product.tape_gap_at = received_at
        self._tape_gaps += 1

    def _heartbeat(self, product, message, received_at):
        try:
            at = _instant(message["time"])
            last_trade_id = _count(message["last_trade_id"])
        except (KeyError, ValueError):
            return  # Unreadable: not a heartbeat; the product goes stale without one.
        product.heartbeat_at, product.heartbeat_received_at = at, received_at
        if product.trade_id is not None and last_trade_id > product.trade_id:
            self._gap(product, received_at)  # A trade the matches channel did not deliver.
        product.trade_id = last_trade_id if product.trade_id is None else max(
            product.trade_id, last_trade_id)

    def _ticker(self, product, message, received_at):
        try:
            bid, ask = _price(message["best_bid"]), _price(message["best_ask"])
            at = _instant(message["time"])
            sequence = _count(message["sequence"])
        except (KeyError, ValueError):
            return  # An unusable quote: the previous one ages out by its own times.
        if bid > ask or (product.quote_sequence is not None and sequence <= product.quote_sequence):
            return  # Crossed, or older than the quote already held.
        product.bid, product.ask, product.quote_at = bid, ask, at
        product.quote_received_at, product.quote_sequence = received_at, sequence

    def _match(self, product, message, received_at):
        try:
            trade_id = _count(message["trade_id"])
            price = _price(message["price"])
            at = _instant(message["time"])
        except (KeyError, ValueError):
            self._gap(product, received_at)  # A print that cannot be read is a print missed.
            return
        if (message["type"] == "match" and product.trade_id is not None
                and trade_id > product.trade_id + 1):
            self._gap(product, received_at)
        product.trade_id = trade_id if product.trade_id is None else max(product.trade_id,
                                                                          trade_id)
        item = ReferencePrint(price, at, received_at, str(trade_id))
        if item.trade_id not in product.print_ids:
            product.prints.append(item)
            product.print_ids.add(item.trade_id)
        if product.last_print is None or trade_id > int(product.last_print.trade_id):
            product.last_print = item
        product.prune(received_at)

    # --- Reads ----------------------------------------------------------------------------

    def _health(self, product_name, product, now):
        if product_name is None:
            return False, NOT_A_COINBASE_PRODUCT
        if not self._connected:
            return False, DISCONNECTED
        if product_name in self._refused:
            return False, SUBSCRIPTION_REFUSED
        if product_name not in self._requested or product is None:
            return False, NOT_REQUESTED
        if product_name not in self._acknowledged:
            return False, SUBSCRIPTION_UNACKNOWLEDGED
        if product.heartbeat_received_at is None:
            return False, NO_HEARTBEAT
        # A heartbeat received after ``now`` (another thread's clock read) is fresh.
        if (now - product.heartbeat_received_at).total_seconds() > HEARTBEAT_MAX_AGE_SECONDS:
            return False, HEARTBEAT_STALE
        if abs((product.heartbeat_received_at - product.heartbeat_at).total_seconds()) > (
                CLOCK_TOLERANCE_SECONDS):
            return False, CLOCK_SKEW
        if product.tape_gap_at is not None and (
                (now - product.tape_gap_at).total_seconds() < TAPE_GAP_HOLD_SECONDS):
            return False, TRADE_TAPE_GAP
        return True, None

    def health(self, product_name, now):
        """``(healthy, code)`` of one product at ``now``."""
        with self._lock:
            return self._health(product_name, self._products.get(product_name), now)

    def view(self, product_name, now):
        """One product's ``ReferenceView`` at ``now`` (a copy; the feed keeps running)."""
        with self._lock:
            product = self._products.get(product_name)
            healthy, code = self._health(product_name, product, now)
            if product is None:
                return ReferenceView(product_name, now, healthy, code, connected=self._connected)
            product.prune(now)
            return ReferenceView(
                product_name, now, healthy, code,
                connected=self._connected,
                acknowledged=product_name in self._acknowledged,
                heartbeat_at=product.heartbeat_at,
                heartbeat_received_at=product.heartbeat_received_at,
                bid=product.bid, ask=product.ask, quote_at=product.quote_at,
                quote_received_at=product.quote_received_at,
                last_print=product.last_print,
                prints=tuple(product.prints),
            )

    def status(self, now):
        """The feed as the runtime status reports it (codes, product ids and ages only)."""
        with self._lock:
            unhealthy, heartbeat_ages = {}, []
            for name in sorted(self._requested):
                product = self._products.get(name)
                healthy, code = self._health(name, product, now)
                if not healthy:
                    unhealthy[name] = code
                if product is not None and product.heartbeat_received_at is not None:
                    heartbeat_ages.append((now - product.heartbeat_received_at).total_seconds())
            return {
                "provider": PROVIDER,
                "endpoint": ENDPOINT,
                "channels": list(CHANNELS),
                "connected": self._connected,
                "sessions": self._sessions,
                "connected_at": _text(self._connected_at) if self._connected else None,
                "disconnected_at": _text(self._disconnected_at) if not self._connected else None,
                "requested": sorted(self._requested),
                "acknowledged": sorted(self._acknowledged),
                "refused": dict(sorted(self._refused.items())),
                "healthy": sorted(set(self._requested) - set(unhealthy)),
                "unhealthy": unhealthy,
                "tape_gaps": self._tape_gaps,
                "last_message_age_seconds": _seconds(now, self._last_message_at)
                if self._last_message_at is not None else None,
                "oldest_heartbeat_age_seconds": str(Decimal(str(max(heartbeat_ages))))
                if heartbeat_ages else None,
                "heartbeat_max_age_seconds": HEARTBEAT_MAX_AGE_SECONDS,
                "clock_tolerance_seconds": CLOCK_TOLERANCE_SECONDS,
                "tape_gap_hold_seconds": TAPE_GAP_HOLD_SECONDS,
                "as_of": now.isoformat(),
            }

    # --- One connection -------------------------------------------------------------------

    def session(self, *, wanted, stop_event, open_timeout, read_timeout, on_acknowledged=None):
        """One public connection: subscribe to ``wanted()`` at once (Coinbase closes a connection
        that has not subscribed within 5 s), follow its changes, apply every message.

        Returns ``STOPPED`` (the stop event) or ``NOTHING_WANTED``; raises when the frame, the
        acknowledgment or the provider fails, when the requested products are neither
        acknowledged nor refused within ``open_timeout`` seconds, or when nothing at all arrives
        for ``SILENCE_LIMIT_SECONDS`` while a product is acknowledged (``REFERENCE_FEED_SILENT``).
        ``on_acknowledged(products, refused)`` is called each time the requested products settle.
        """
        with self.connector(
            ENDPOINT,
            proxy=None,
            open_timeout=open_timeout,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
            max_queue=MAX_QUEUE,
            logger=WIRE_LOG,
        ) as socket:
            self.begin(socket)
            try:
                return self._read(socket, wanted, stop_event, open_timeout, read_timeout,
                                  on_acknowledged)
            finally:
                self.end()

    def _silent(self, heard):
        """Nothing arrived for ``SILENCE_LIMIT_SECONDS`` although a product is acknowledged."""
        with self._lock:
            expecting = bool(self._acknowledged & self._requested)
        return expecting and _monotonic() - heard > SILENCE_LIMIT_SECONDS

    def _read(self, socket, wanted, stop_event, open_timeout, read_timeout, on_acknowledged):
        requested, deadline, heard = frozenset(), None, _monotonic()
        while not stop_event.is_set():
            desired = frozenset(wanted())
            if not desired:
                return NOTHING_WANTED
            if desired != requested:
                adding, removing = desired - requested, requested - desired
                self.request(desired)
                if adding:
                    socket.send(_request("subscribe", adding))
                if removing:
                    socket.send(_request("unsubscribe", removing))
                requested, deadline = desired, _monotonic() + open_timeout
            if deadline is not None and self.settled():
                deadline = None
                if on_acknowledged is not None:
                    with self._lock:
                        acknowledged, refused = sorted(self._acknowledged), dict(self._refused)
                    on_acknowledged(acknowledged, refused)
            if deadline is not None and _monotonic() >= deadline:
                raise ValueError("REFERENCE_SUBSCRIPTION_TIMEOUT")
            if self._silent(heard):
                raise ValueError("REFERENCE_FEED_SILENT")
            try:
                raw = socket.recv(timeout=read_timeout)
            except TimeoutError:
                continue
            heard = _monotonic()
            self.handle(_frame(raw), received_at=self.clock())
        return STOPPED
