"""Bounded public Coinbase observations for isolated research shadow cases only.

Ticker batches matches. It is not a guaranteed complete trade tape or a rebuilt
order book. No discovery, case creation, credentials, reconnect or order transport
is provided here. Raw probes never create a ShadowStore or a synthetic case.
"""

import base64
import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

from websockets.sync.client import connect

from catalyst_lab.jev_shadow import ShadowObservation, prequalify

VENUE = "COINBASE_EXCHANGE"
ENDPOINT = "wss://ws-feed.exchange.coinbase.com"
DOCS = (
    "https://docs.cdp.coinbase.com/exchange/websocket-feed/channels",
    "https://docs.cdp.coinbase.com/exchange/websocket-feed/overview",
)
MAX_MESSAGES = 50_000
MAX_BYTES = 64 * 1024 * 1024
LIMITATIONS = (
    "Public ticker batches cascading matches; no uninterrupted complete trade-tape proof.",
    "Ticker sequence numbers may skip non-ticker events; sequence jumps flag coverage uncertainty.",
    "Trade-ID jumps mean batched or missing matches and latch the product unhealthy.",
    "Top-of-book sizes are observations, not fills, queue position or guaranteed available volume.",
    "Ticker timestamp is reused for its bundled quote; no independent quote timestamp is supplied.",
    "No reconnect or historical backfill is used to claim continuous observations.",
    "Research-only matched shadow outcomes are not actual fills or portfolio performance.",
)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) \
        if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("AWARE_FEED_TIMESTAMP_REQUIRED")
    return result.astimezone(UTC)


def products_list(products):
    products = tuple(products)
    if (not 1 <= len(products) <= 10 or len(set(products)) != len(products)
            or any(not isinstance(p, str) or not re.fullmatch(r"[A-Z0-9]{2,15}-USD", p)
                   for p in products)):
        raise ValueError("ONE_TO_TEN_EXPLICIT_UNIQUE_USD_PRODUCTS_REQUIRED")
    return products


def bounded_seconds(value):
    if type(value) is not int or not 1 <= value <= 300:
        raise ValueError("COLLECTION_SECONDS_MUST_BE_1_TO_300")
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal(value, *, zero=False):
    if not isinstance(value, str):
        raise ValueError("FEED_DECIMAL_STRING_REQUIRED")
    value = D(value)
    if not value.is_finite() or value < 0 or (not zero and value == 0):
        raise ValueError("INVALID_FEED_NUMBER")
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_FEED_SEQUENCE")
    return value


@dataclass(frozen=True)
class ParsedTicker:
    product_id: str
    sequence: int
    trade_id: int
    at: datetime
    received_at: datetime
    price: D
    bid: D
    ask: D
    bid_size: D | None
    ask_size: D | None
    feed_healthy: bool

    def observation(self, case_id):
        return ShadowObservation(
            observation_id=f"coinbase:{self.product_id}:{self.sequence}:{self.trade_id}",
            case_id=case_id, symbol=self.product_id.replace("-", "/"), venue=VENUE,
            kind="tick", at=self.at, received_at=self.received_at, trade_price=self.price,
            bid=self.bid, ask=self.ask, quote_at=self.at, bid_size=self.bid_size,
            ask_size=self.ask_size, feed_healthy=self.feed_healthy,
        )


class CoinbaseTickerFeed:
    """One connection's conservative parser. Faults never heal during that session."""

    def __init__(self, products, *, started_at):
        self.products = products_list(products)
        self.started_at = timestamp(started_at)
        self.acknowledged = set()
        self.last_heartbeat = {}
        self.previous = {}
        self.seen = {}
        self.faults = {p: set() for p in self.products}
        self.flags = Counter()
        self.tickers = Counter()
        self.heartbeats = Counter()

    def flag(self, code, product=None, *, fatal=True):
        self.flags[code] += 1
        if fatal:
            for item in ([product] if product in self.faults else self.products):
                self.faults[item].add(code)

    def check_timeouts(self, now):
        now = timestamp(now)
        for product in self.products:
            last = self.last_heartbeat.get(product, self.started_at)
            if (now - last).total_seconds() > 5:
                code = "HEARTBEAT_GAP" if product in self.last_heartbeat else "NO_HEARTBEAT"
                if code not in self.faults[product]:
                    self.flag(code, product)
        if (now - self.started_at).total_seconds() > 5:
            for product in set(self.products) - self.acknowledged:
                if "SUBSCRIPTION_NOT_ACKNOWLEDGED" not in self.faults[product]:
                    self.flag("SUBSCRIPTION_NOT_ACKNOWLEDGED", product)

    def consume(self, message, *, received_at):
        now = timestamp(received_at)
        self.check_timeouts(now)
        if not isinstance(message, dict):
            self.flag("INVALID_MESSAGE")
            return None
        kind = message.get("type")
        if not isinstance(kind, str):
            self.flag("INVALID_MESSAGE_TYPE")
            return None
        if kind == "subscriptions":
            channels = message.get("channels", [])
            if not isinstance(channels, list) or any(
                not isinstance(c, dict) or not isinstance(c.get("product_ids"), list)
                for c in channels
            ):
                self.flag("INVALID_SUBSCRIPTION_ACK")
                return None
            for product in self.products:
                if all(any(c.get("name") == name and product in c.get("product_ids", [])
                           for c in channels) for name in ("ticker", "heartbeat")):
                    self.acknowledged.add(product)
            return None
        if kind == "error":
            self.flag("PROVIDER_ERROR")
            return None
        if kind not in {"heartbeat", "ticker"}:
            self.flag("UNHANDLED_MESSAGE_TYPE", fatal=False)
            return None
        product = message.get("product_id")
        if product not in self.products:
            self.flag("UNREQUESTED_PRODUCT", fatal=False)
            return None
        try:
            at = timestamp(message["time"])
            if not 0 <= (now - at).total_seconds() <= 5:
                self.flag("FUTURE_OR_STALE_FEED_TIMESTAMP", product)
                return None
            if kind == "heartbeat":
                _integer(message["sequence"])
                _integer(message["last_trade_id"])
                previous = self.last_heartbeat.get(product)
                if previous is not None and now < previous:
                    self.flag("HEARTBEAT_CLOCK_REGRESSION", product)
                    return None
                self.last_heartbeat[product] = now
                self.heartbeats[product] += 1
                return None
            sequence, trade_id = _integer(message["sequence"]), _integer(message["trade_id"])
            key, fingerprint = (product, sequence, trade_id), digest(encoded(message))
            if key in self.seen:
                same = self.seen[key] == fingerprint
                self.flag("DUPLICATE_TICK" if same else "DUPLICATE_CONFLICT", product,
                          fatal=not same)
                return None
            self.seen[key] = fingerprint
            price = _decimal(message["price"])
            bid, ask = _decimal(message["best_bid"]), _decimal(message["best_ask"])
            if ask < bid:
                raise ValueError("CROSSED_QUOTE")
            bid_size = (_decimal(message["best_bid_size"], zero=True)
                        if "best_bid_size" in message else None)
            ask_size = (_decimal(message["best_ask_size"], zero=True)
                        if "best_ask_size" in message else None)
            if bid_size is None or ask_size is None:
                self.flag("MISSING_DISPLAYED_SIZE", product)
            old = self.previous.get(product)
            if old and (sequence <= old.sequence or trade_id <= old.trade_id or at < old.at):
                self.flag("OUT_OF_ORDER_TICK", product)
                return None
            if old and trade_id > old.trade_id + 1:
                self.flag("BATCHED_OR_MISSING_MATCHES", product)
            if old and sequence > old.sequence + 1:
                self.flag("SEQUENCE_COVERAGE_UNPROVEN", product, fatal=False)
            if product not in self.acknowledged:
                self.flag("TICK_BEFORE_SUBSCRIPTION_ACK", product)
            tick = ParsedTicker(product, sequence, trade_id, at, now, price, bid, ask,
                                bid_size, ask_size, not self.faults[product])
            self.previous[product] = tick
            self.tickers[product] += 1
            return tick
        except (KeyError, ValueError, TypeError, ArithmeticError):
            self.flag("MALFORMED_FEED_MESSAGE", product)
            return None

    def summary(self):
        return {
            "subscription_acknowledged": sorted(self.acknowledged),
            "ticker_counts": dict(self.tickers), "heartbeat_counts": dict(self.heartbeats),
            "flags": dict(self.flags), "latched_faults": {k: sorted(v)
                                                          for k, v in self.faults.items()},
            "complete_trade_tape_verified": False,
            "observed_continuity_fault": any(self.faults.values()),
        }


def eligible_cases(store, products):
    products = products_list(products)
    selected, excluded = [], []
    for case in store.cases():
        reasons = list(prequalify(case).reasons)
        if case.cohort != "PROSPECTIVE":
            reasons.append("LIVE_COLLECTION_REQUIRES_PROSPECTIVE_CASE")
        if case.venue != VENUE:
            reasons.append("COLLECTOR_VENUE_MISMATCH")
        if case.symbol.replace("/", "-") not in products:
            reasons.append("PRODUCT_NOT_REQUESTED")
        if reasons:
            excluded.append({"case_id": case.case_id, "reasons": reasons})
        else:
            selected.append(case)
    if len(selected) > 100:
        raise ValueError("MAXIMUM_100_COLLECTED_CASES")
    return selected, excluded


def _session_event(store, session_id, kind, body):
    # Use the same isolated append-only log and hash chain, without changing its
    # case or observation schemas. Public reports surface these continuity events.
    store._write(f"feed:{session_id}:{kind}", "FEED_SESSION", "", body, lambda: None)


def feed_sessions(store):
    events = [json.loads(row["payload"]) for row in store.conn.execute(
        "SELECT payload FROM shadow_events WHERE kind='FEED_SESSION' ORDER BY seq")]
    started = {e["session_id"] for e in events if e["phase"] == "STARTED"}
    finished = {e["session_id"] for e in events if e["phase"] == "FINISHED"}
    return {"events": events, "interrupted_session_ids": sorted(started - finished),
            "complete_trade_tape_verified": False}


def collect_public_feed(*, products, seconds, output_dir, store=None, connector=None,
                        clock=None, monotonic=None):
    """One bounded unauthenticated connection. A fresh artifact directory is required."""
    products, seconds = products_list(products), bounded_seconds(seconds)
    clock = clock or (lambda: datetime.now(UTC))
    monotonic = monotonic or time.monotonic
    connector = connector or connect
    cases, excluded = eligible_cases(store, products) if store is not None else ([], [])
    if store is not None and not cases:
        raise ValueError("NO_ELIGIBLE_PROSPECTIVE_VENUE_BOUND_CASES")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    session_id, started = str(uuid4()), timestamp(clock())
    parser = CoinbaseTickerFeed(products, started_at=started)
    subscription = {"type": "subscribe", "product_ids": list(products),
                    "channels": ["ticker", "heartbeat"]}
    metadata = {"session_id": session_id, "phase": "STARTED", "started_at": started.isoformat(),
                "venue": VENUE, "endpoint": ENDPOINT, "products": list(products),
                "requested_seconds": seconds, "bound_case_ids": [c.case_id for c in cases],
                "excluded_cases": excluded, "source_documentation": list(DOCS),
                "limitations": list(LIMITATIONS), "mode": "COLLECT" if store else "RAW_PROBE",
                "creates_cases": False, "creates_orders": False}
    (output / "request.json").write_text(json.dumps({**metadata, "subscription": subscription},
                                                  indent=2) + "\n")
    os.chmod(output / "request.json", 0o600)
    if store is not None:
        _session_event(store, session_id, "STARTED", metadata)
        # A new bounded connection cannot silently continue an earlier feed segment.
        for case in cases:
            if store.observations(case.case_id):
                parser.flag("COLLECTION_RESTART_CONTINUITY_UNPROVEN",
                            case.symbol.replace("/", "-"))
    previous_hash, received, byte_count, appended = "0" * 64, 0, 0, 0
    rejected = Counter()
    status = "BOUNDED_WINDOW_COMPLETED"
    start_mono = monotonic()
    deadline = start_mono + seconds
    with (output / "messages.jsonl").open("x") as raw_file:
        os.chmod(output / "messages.jsonl", 0o600)
        try:
            with connector(ENDPOINT, open_timeout=min(seconds, 10), close_timeout=1,
                           proxy=None, max_size=262_144, max_queue=1024) as websocket:
                websocket.send(encoded(subscription))
                while monotonic() < deadline:
                    try:
                        raw = websocket.recv(timeout=min(1, max(.001, deadline - monotonic())))
                    except TimeoutError:
                        parser.check_timeouts(clock())
                        continue
                    now = timestamp(clock())
                    received += 1
                    wire = raw.encode() if isinstance(raw, str) else raw
                    byte_count += len(wire)
                    row = {"index": received, "received_at": now.isoformat(),
                           "wire_text": raw if isinstance(raw, str) else None,
                           "wire_base64": base64.b64encode(wire).decode()
                           if not isinstance(raw, str) else None,
                           "wire_sha256": hashlib.sha256(wire).hexdigest(),
                           "previous_hash": previous_hash}
                    previous_hash = digest(encoded(row))
                    raw_file.write(encoded({**row, "hash": previous_hash}) + "\n")
                    raw_file.flush()
                    if received >= MAX_MESSAGES or byte_count >= MAX_BYTES:
                        parser.flag("CAPTURE_RESOURCE_LIMIT")
                        status = "RESOURCE_LIMIT_STOPPED"
                        break
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError, UnicodeError):
                        parser.flag("INVALID_WIRE_JSON")
                        continue
                    tick = parser.consume(message, received_at=now)
                    if tick is None or store is None:
                        continue
                    for case in cases:
                        if case.symbol != tick.product_id.replace("-", "/"):
                            continue
                        try:
                            appended += store.append(tick.observation(case.case_id))
                        except ValueError as error:
                            rejected[str(error)] += 1
                            parser.flag("SHADOW_OBSERVATION_REFUSED", tick.product_id)
        except KeyboardInterrupt:
            status = "INTERRUPTED"
            parser.flag("COLLECTION_INTERRUPTED")
        except Exception as error:
            # Exception class only: no proxy/environment/request text in artifacts.
            status = "CONNECTION_FAILED_OR_CLOSED"
            parser.flag("CONNECTION_FAILED_OR_CLOSED")
            rejected[type(error).__name__] += 1
        raw_file.flush()
        os.fsync(raw_file.fileno())
    finished = timestamp(clock())
    parser.check_timeouts(finished)
    summary = {
        **metadata, "phase": "FINISHED", "finished_at": finished.isoformat(),
        "elapsed_seconds": round(monotonic() - start_mono, 6), "status": status,
        "raw_message_count": received, "raw_bytes": byte_count, "raw_head_hash": previous_hash,
        "appended_shadow_observations": appended, "rejections": dict(rejected),
        **parser.summary(),
    }
    if store is not None:
        _session_event(store, session_id, "FINISHED", summary)
        summary["shadow_log_head_hash"] = store.verify()
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "README.md").write_text(
        "# Bounded public Coinbase feed evidence\n\n"
        "Exact decoded WebSocket messages and local receipt times are in `messages.jsonl`. "
        "Its retained SHA-256 chain head is in `summary.json`. This is a public-data "
        "connectivity/format probe, not an execution or profitability proof.\n\n"
        + "\n".join(f"- {item}" for item in LIMITATIONS) + "\n\n"
        + "\n".join(f"- Source documentation: {url}" for url in DOCS) + "\n"
    )
    os.chmod(output / "summary.json", 0o600)
    os.chmod(output / "README.md", 0o600)
    return summary
