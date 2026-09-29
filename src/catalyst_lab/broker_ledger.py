"""Broker evidence ledger. Unknown orders are quarantined, never adopted by polling.

After a trade-updates gap, ``record_rest_backfill`` records what the stream missed from
REST reads (FILL account activities and the current state of each non-terminal linked
order) as ``BROKER_REST_BACKFILL`` events. Fills are deduplicated on (broker order id,
cumulative filled quantity), because FILL activities carry no execution id: a late stream
delivery of a backfilled fill is recognised instead of exceeding the order quantity.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.config import STRATEGY_VERSION
from catalyst_lab.execution import halt, system_event
from catalyst_lab.market import MarketDataError, decimal, symbol, timestamp, timestamp_ns
from catalyst_lab.repository import json_safe

TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})

# Bracket-leg protection, rule A (plan packages 2.5 and 3.1). One pure classifier serves
# the frozen V1 safety check and the managed stock controller; it reads no clock and does
# no I/O. Alpaca staff describe a fully filled bracket as take-profit ``new`` with the
# stop-loss leg still ``held``; a partial fill keeps both legs ``held``.
PROTECTED = "PROTECTED"
TRANSITIONING = "PROTECTION_TRANSITIONING"
UNPROTECTED = "UNPROTECTED"
ACTIVE_LEG_STATUSES = frozenset({"new", "accepted", "partially_filled", "accepted_for_bidding"})
STOP_LEG_STATUSES = ACTIVE_LEG_STATUSES | {"held"}
# Owned legs in these states are between two protective states (our own replacement, a
# leg not yet released after the parent fill). They protect nothing on their own, so they
# count only for a bounded grace; ``accepted`` is already active and never needs it.
TRANSITIONING_LEG_STATUSES = frozenset({"pending_new", "pending_replace", "accepted", "held"})
TRANSITION_GRACE_SECONDS = 5  # The exact risk-decision TTL.
ENTRY_NOT_FILLED = "ENTRY_NOT_FULLY_FILLED"
LEG_MISMATCH = "BRACKET_LEG_MISMATCH"


@dataclass(frozen=True)
class BracketProtection:
    """``status`` is PROTECTED, PROTECTION_TRANSITIONING or UNPROTECTED.

    ``stop`` and ``target`` are the covering legs (raw orders) when a role is covered or
    transitioning; the managed controller amends only the legs of a PROTECTED bracket.
    """

    status: str
    reason: str
    stop: dict | None = None
    target: dict | None = None


def _leg_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def classify_bracket(parent, legs, *, position_qty, stop_prices, target_prices, known_ids=None):
    """Classify one long bracket against broker truth; never raises for broker content.

    Rule A: protected iff the parent is ``filled``, the take-profit leg is active
    (``new|accepted|partially_filled|accepted_for_bidding``) and the stop leg is active or
    ``held``, each a sell whose remaining quantity covers the position at a price in the
    authorized set (``stop_prices``/``target_prices``). Both legs ``held`` is not
    protected, and a partially filled parent is never protected.

    With a filled parent, a role whose only live leg is an owned leg (``known_ids``, all
    legs when ``None``) in a transitioning state makes the bracket PROTECTION_TRANSITIONING.
    The caller bounds that with ``within_transition_grace`` and then treats it as
    UNPROTECTED. Any live leg with the wrong side, too little quantity or an unauthorized
    price makes the bracket UNPROTECTED at once: our orders changed outside our authority.

    ``parent`` is the entry order (``None`` when unavailable); ``legs`` yields
    ``(role, order)`` with role ``STOP`` or ``TARGET``; ``position_qty`` must be positive.
    """
    qty = _leg_number(position_qty)
    if qty is None or qty <= 0:
        raise ValueError("POSITIVE_POSITION_REQUIRED")
    if not isinstance(parent, dict) or parent.get("status") != "filled":
        return BracketProtection(UNPROTECTED, ENTRY_NOT_FILLED)
    stops = {p for p in (_leg_number(v) for v in stop_prices) if p is not None}
    targets = {p for p in (_leg_number(v) for v in target_prices) if p is not None}
    covering = {"STOP": None, "TARGET": None}
    waiting = {"STOP": None, "TARGET": None}
    for role, order in legs:
        if role not in covering or not isinstance(order, dict):
            continue
        status = order.get("status")
        protective = STOP_LEG_STATUSES if role == "STOP" else ACTIVE_LEG_STATUSES
        known = known_ids is None or order.get("id") in known_ids
        if status in protective:
            slot = covering
        elif status in TRANSITIONING_LEG_STATUSES and known:
            slot = waiting
        else:
            continue  # Terminal, cancel-pending or unrecognized legs protect nothing.
        leg_qty, filled = _leg_number(order.get("qty")), _leg_number(order.get("filled_qty", 0))
        price = _leg_number(order.get("stop_price" if role == "STOP" else "limit_price"))
        if (
            order.get("side") != "sell"
            or leg_qty is None
            or filled is None
            or leg_qty - filled < qty
            or price not in (stops if role == "STOP" else targets)
        ):
            return BracketProtection(UNPROTECTED, LEG_MISMATCH)
        if slot[role] is None:
            slot[role] = order
    if covering["STOP"] is not None and covering["TARGET"] is not None:
        return BracketProtection(PROTECTED, "RULE_A_BRACKET", covering["STOP"], covering["TARGET"])
    missing = [role for role in ("STOP", "TARGET") if covering[role] is None]
    if all(waiting[role] is not None for role in missing):
        role = missing[0]
        return BracketProtection(
            TRANSITIONING,
            f"{role}_LEG_{str(waiting[role].get('status')).upper()}",
            covering["STOP"] or waiting["STOP"],
            covering["TARGET"] or waiting["TARGET"],
        )
    return BracketProtection(UNPROTECTED, "_AND_".join(missing) + "_LEG_MISSING")


def within_transition_grace(since, now, seconds=TRANSITION_GRACE_SECONDS):
    """True while a transition first seen at ``since`` is younger than the grace."""
    if since is None or now is None:
        return False
    elapsed = (now - since).total_seconds()
    return 0 <= elapsed < seconds


def normalize_order(raw):
    if not isinstance(raw, dict):
        raise MarketDataError("INVALID_BROKER_ORDER")
    try:
        oid = raw["id"]
        if not isinstance(oid, str) or not 1 <= len(oid) <= 128:
            raise ValueError
        qty = decimal(raw["qty"])
        filled = decimal(raw.get("filled_qty", "0"), positive=False)
        if qty != int(qty) or filled > qty:
            raise ValueError
        if raw.get("asset_class", "us_equity") != "us_equity":
            raise ValueError
        side, status, kind = raw["side"], raw["status"], raw.get("type", raw.get("order_type"))
        if side not in {"buy", "sell"} or not isinstance(status, str) or not kind:
            raise ValueError
        return {
            "id": oid,
            "symbol": symbol(raw["symbol"]),
            "side": side,
            "qty": qty,
            "filled_qty": filled,
            "status": status,
            "type": kind,
            "limit_price": decimal(raw["limit_price"]) if raw.get("limit_price") else None,
            "stop_price": decimal(raw["stop_price"]) if raw.get("stop_price") else None,
        }
    except (KeyError, TypeError, ValueError):
        raise MarketDataError("INVALID_BROKER_ORDER") from None


# REST fill backfill after a trade-updates gap (plan 4.4). Changes no trading rule.
BACKFILL_LOOKBACK = timedelta(minutes=5)  # The window starts this far before the last message.
REST_BACKFILL_EVENT = "BROKER_REST_BACKFILL"
REST_FILL_SOURCE = "ALPACA_REST_FILL_ACTIVITY"
REST_ORDER_SOURCE = "ALPACA_REST_ORDER"
REST_ORDER_EVENT = "rest_order_snapshot"  # broker_events type of a REST order state.
REST_TERMINAL_EVENTS = frozenset({"canceled", "expired", "rejected", "replaced"})
_BROKER_ID = re.compile(r"[A-Za-z0-9-]{1,128}")


def quantity_key(value):
    """Canonical text of a quantity: ``10`` and ``10.00`` share one key, ``0.9999`` stays."""
    return format(Decimal(str(value)).normalize(), "f")


def rest_fill_id(broker_order_id, cumulative):
    """The fill id of a REST-recorded execution: one per broker order and cumulative qty."""
    return f"rest-fill:{broker_order_id}:{quantity_key(cumulative)}"


def message_digest(raw):
    return hashlib.sha256(
        json.dumps(json_safe(raw), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_fill_activity(raw):
    """Validate one Alpaca FILL account activity (no execution id; cumulative qty known)."""
    try:
        if not isinstance(raw, dict) or raw.get("activity_type") != "FILL":
            raise ValueError
        order_id, activity_id = raw["order_id"], raw["id"]
        if not isinstance(order_id, str) or not _BROKER_ID.fullmatch(order_id):
            raise ValueError
        if not isinstance(activity_id, str) or not 1 <= len(activity_id) <= 160:
            raise ValueError
        qty, price, cumulative = (decimal(raw[k]) for k in ("qty", "price", "cum_qty"))
        leaves = raw.get("leaves_qty")
        leaves = decimal(leaves, positive=False) if leaves is not None else None
        kind, side, name = raw.get("type"), raw["side"], raw["symbol"]
        status = raw.get("order_status")
        if (
            kind not in {"fill", "partial_fill"}
            or side not in {"buy", "sell"}
            or not isinstance(name, str)
            or not name
            or qty > cumulative
            or (status is not None and not isinstance(status, str))
        ):
            raise ValueError
        return {
            "activity_id": activity_id,
            "order_id": order_id,
            "symbol": name,
            "side": side,
            "type": kind,
            "qty": qty,
            "price": price,
            "cumulative": cumulative,
            "leaves": leaves,
            "order_status": status or ("filled" if kind == "fill" else "partially_filled"),
            "at": timestamp(raw["transaction_time"]),
            "at_ns": timestamp_ns(raw["transaction_time"]),
        }
    except (KeyError, TypeError, ValueError, AttributeError, MarketDataError):
        raise MarketDataError("INVALID_FILL_ACTIVITY") from None


class BrokerLedger:
    def __init__(self, repo, *, pending_authorization=None, after_projection=None):
        self.repo = repo
        self.pending_authorization = pending_authorization
        self.after_projection = after_projection

    def register_submitted(self, candidate_id, intent, broker_order, *, allow_incomplete=False):
        """Record a known submission receipt only; never called by Phase 3 runtime/reconciler.

        The candidate must already have reached ORDER_SUBMITTED through a future risk-approved
        submission. Tests seed that historical state explicitly; this method cannot send orders.
        """
        root = normalize_order(broker_order)
        with self.repo.connect() as conn:
            existing = conn.execute(
                "SELECT order_id,intent_id FROM lab.orders WHERE alpaca_order_id=%s", (root["id"],)
            ).fetchone()
            if existing:
                if existing["intent_id"] != intent["intent_id"]:
                    raise ValueError("Broker order already belongs to a different intent")
                return existing["order_id"]
        payload = intent["payload_json"]
        if intent["kind"] != "ENTRY" or root["id"] is None:
            raise ValueError("A known entry intent is required")
        if (
            broker_order.get("order_class") != "bracket"
            or broker_order.get("time_in_force") != "day"
            or broker_order.get("extended_hours", False)
        ):
            raise ValueError("Require a regular-session DAY bracket receipt")
        if (
            root["symbol"] != payload["symbol"]
            or root["qty"] != Decimal(payload["qty"])
            or root["side"] != "buy"
            or root["type"] != "limit"
            or root["limit_price"] != Decimal(payload["limit_price"])
            or broker_order.get("client_order_id") != payload["client_order_id"]
        ):
            raise ValueError("Broker receipt differs from the immutable entry intent")
        legs = broker_order.get("legs") or []
        if len(legs) != 2 and not allow_incomplete:
            raise ValueError("A bracket receipt must identify both protective legs")
        normalized = []
        for leg in legs:
            item = normalize_order(leg)
            role = (
                "STOP" if item["type"] == "stop" else "TARGET" if item["type"] == "limit" else None
            )
            expected = (
                payload["stop_loss"]["stop_price"]
                if role == "STOP"
                else payload["take_profit"]["limit_price"]
            )
            price = item["stop_price"] if role == "STOP" else item["limit_price"]
            if (
                role is None
                or item["side"] != "sell"
                or item["symbol"] != root["symbol"]
                or item["qty"] != root["qty"]
                or price != Decimal(expected)
            ):
                raise ValueError("Protective leg does not match the bracket intent")
            normalized.append((role, item))
        if {role for role, _ in normalized} != {"STOP", "TARGET"} and not allow_incomplete:
            raise ValueError("Both stop and target legs are required")
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            state = conn.execute(
                "SELECT state FROM lab.candidate_states WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
            saved = conn.execute(
                "SELECT * FROM lab.order_intents WHERE intent_id=%s", (intent["intent_id"],)
            ).fetchone()
            if not state or state["state"] != "ORDER_SUBMITTED" or saved != intent:
                raise ValueError("No known submitted candidate/intent to associate")
            if str(intent["candidate_id"]) != str(candidate_id):
                raise ValueError("Candidate does not own the intent")
            event = system_event(
                self.repo,
                conn,
                "BROKER_ORDER_REGISTERED",
                {"intent_id": str(intent["intent_id"]), "broker_order": broker_order},
                candidate_id,
            )
            order_id = uuid4()
            conn.execute(
                """INSERT INTO lab.orders(order_id,candidate_id,strategy_version,
                execution_source,client_order_id,alpaca_order_id,bracket_legs,status,event_id,
                intent_id,ticker,qty) VALUES (%s,%s,%s,'ALPACA_PAPER',%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    order_id,
                    candidate_id,
                    STRATEGY_VERSION,
                    payload["client_order_id"],
                    root["id"],
                    Jsonb(legs),
                    root["status"],
                    event["event_id"],
                    intent["intent_id"],
                    root["symbol"],
                    int(root["qty"]),
                ),
            )
            for role, item in [("ENTRY", root), *normalized]:
                conn.execute(
                    """INSERT INTO lab.order_links VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        item["id"],
                        order_id,
                        role,
                        item["symbol"],
                        item["side"],
                        int(item["qty"]),
                        item["type"],
                        item["limit_price"],
                        item["stop_price"],
                        item["status"],
                        event["seq"],
                    ),
                )
            return order_id

    def register_time_exit(self, candidate_id, intent, broker_order):
        """Associate a known exit receipt with its entry ledger; never submits an order."""
        item = normalize_order(broker_order)
        payload = intent["payload_json"]
        if (
            intent["kind"] != "TIME_EXIT"
            or str(intent["candidate_id"]) != str(candidate_id)
            or item["symbol"] != payload["symbol"]
            or item["qty"] != Decimal(payload["qty"])
            or item["side"] != "sell"
            or item["type"] != "market"
            or item["limit_price"] is not None
            or item["stop_price"] is not None
            or broker_order.get("client_order_id") != payload["client_order_id"]
            or broker_order.get("time_in_force") != "day"
            or broker_order.get("order_class", "simple") not in {"", "simple"}
            or broker_order.get("extended_hours", False)
        ):
            raise ValueError("Exit receipt differs from the immutable market exit intent")
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            saved = conn.execute(
                "SELECT * FROM lab.order_intents WHERE intent_id=%s", (intent["intent_id"],)
            ).fetchone()
            position = conn.execute(
                "SELECT * FROM lab.strategy_positions WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
            entry = conn.execute(
                "SELECT order_id FROM lab.orders WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
            if saved != intent or not entry or not position or position["qty"] != item["qty"]:
                raise ValueError(
                    "Exit receipt has no matching intent and current strategy position"
                )
            event = system_event(
                self.repo,
                conn,
                "BROKER_TIME_EXIT_REGISTERED",
                {"intent_id": str(intent["intent_id"]), "broker_order": broker_order},
                candidate_id,
            )
            conn.execute(
                """INSERT INTO lab.order_links VALUES
                (%s,%s,'TIME_EXIT',%s,'sell',%s,'market',NULL,NULL,%s,%s)""",
                (
                    item["id"],
                    entry["order_id"],
                    item["symbol"],
                    int(item["qty"]),
                    item["status"],
                    event["seq"],
                ),
            )
            return item["id"]

    def discover_protective_legs(self, candidate_id, broker_order):
        """Append verified child identities that weren't present in the initial receipt."""
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            entry = conn.execute(
                """SELECT o.*,i.payload_json FROM lab.orders o
                JOIN lab.order_intents i USING(intent_id) WHERE o.candidate_id=%s
                AND o.alpaca_order_id=%s""",
                (candidate_id, broker_order["id"]),
            ).fetchone()
            if not entry:
                raise ValueError("Unknown entry receipt")
            payload = entry["payload_json"]
            for raw in broker_order.get("legs") or []:
                item = normalize_order(raw)
                role = (
                    "STOP"
                    if item["type"] == "stop"
                    else "TARGET"
                    if item["type"] == "limit"
                    else None
                )
                expected = Decimal(
                    payload["stop_loss"]["stop_price"]
                    if role == "STOP"
                    else payload["take_profit"]["limit_price"]
                )
                price = item["stop_price"] if role == "STOP" else item["limit_price"]
                if (
                    not role
                    or price != expected
                    or item["symbol"] != entry["ticker"]
                    or item["side"] != "sell"
                    or item["qty"] > entry["qty"]
                ):
                    raise MarketDataError("BROKER_PROTECTIVE_LEG_CHANGED")
                if conn.execute(
                    "SELECT 1 FROM lab.order_links WHERE broker_order_id=%s", (item["id"],)
                ).fetchone():
                    continue
                event = system_event(
                    self.repo, conn, "BROKER_LEG_DISCOVERED", {"order": raw}, candidate_id
                )
                conn.execute(
                    "INSERT INTO lab.order_links VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        item["id"],
                        entry["order_id"],
                        role,
                        item["symbol"],
                        item["side"],
                        int(item["qty"]),
                        item["type"],
                        item["limit_price"],
                        item["stop_price"],
                        item["status"],
                        event["seq"],
                    ),
                )

    def consume(self, data, now):
        # Raw authenticated broker messages are committed even when malformed or unexplained.
        raw = json_safe(data)
        message_hash = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        body = raw if isinstance(raw, dict) else {}
        order = body.get("order") if isinstance(body.get("order"), dict) else {}
        broker_id = order.get("id") if isinstance(order.get("id"), str) else None
        event_type = body.get("event") if isinstance(body.get("event"), str) else None
        try:
            observed_stamp = timestamp(body.get("timestamp") or order.get("updated_at"))
        except MarketDataError:
            observed_stamp = None
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            if conn.execute(
                "SELECT 1 FROM lab.broker_events WHERE message_hash=%s", (message_hash,)
            ).fetchone():
                return "DUPLICATE"
            envelope = system_event(self.repo, conn, "BROKER_TRADE_UPDATE", {"message": raw})
            conn.execute(
                "INSERT INTO lab.broker_events VALUES (%s,%s,%s,%s,%s,%s)",
                (envelope["seq"], message_hash, broker_id, event_type, observed_stamp, Jsonb(raw)),
            )
            link = conn.execute(
                """SELECT l.*,o.candidate_id,o.qty AS entry_qty
                FROM lab.order_links l JOIN lab.orders o USING(order_id)
                WHERE l.broker_order_id=%s""",
                (broker_id,),
            ).fetchone()
            if not link:
                if self.pending_authorization and self.pending_authorization(conn, body):
                    system_event(
                        self.repo,
                        conn,
                        "BROKER_UPDATE_AWAITING_RECEIPT",
                        {"broker_event_seq": envelope["seq"], "broker_order_id": broker_id},
                    )
                    return "DEFERRED"
                halt(self.repo, conn, "UNEXPLAINED_BROKER_ORDER", {"broker_order_id": broker_id})
                return "QUARANTINED"
            candidate_id = link["candidate_id"]
            try:
                with conn.transaction():
                    normalized = normalize_order(order)
                    at = timestamp(body.get("timestamp") or order.get("updated_at"))
                    if at > now:
                        raise MarketDataError("FUTURE_BROKER_EVENT")
                    if (
                        normalized["symbol"] != link["symbol"]
                        or normalized["side"] != link["side"]
                        or (
                            normalized["qty"] != link["qty"]
                            if link["role"] == "ENTRY"
                            else normalized["qty"] > link["qty"]
                        )
                        or normalized["type"] != link["order_type"]
                        or normalized["limit_price"] != link["limit_price"]
                        or normalized["stop_price"] != link["stop_price"]
                    ):
                        raise MarketDataError("BROKER_ORDER_CHANGED")
                    if event_type in {"fill", "partial_fill"}:
                        self._fill(conn, envelope, body, normalized, link, at)
                    reason = body.get("reason") or body.get("message")
                    if reason is not None and not isinstance(reason, str):
                        reason = "BROKER_EVENT_REASON_INVALID"
                    conn.execute(
                        "INSERT INTO lab.order_updates VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            envelope["seq"],
                            broker_id,
                            normalized["status"],
                            normalized["filled_qty"],
                            at,
                            reason,
                            int(normalized["qty"]),
                            timestamp_ns(body.get("timestamp") or order.get("updated_at")),
                        ),
                    )
                    self._advance(conn, link, event_type, reason, at)
                    if self.after_projection:
                        self.after_projection(conn, link)
                    if event_type in {"trade_bust", "trade_correct", "replaced"}:
                        halt(
                            self.repo,
                            conn,
                            "BROKER_EVENT_REQUIRES_MANUAL_REVIEW",
                            {"broker_order_id": broker_id, "event": event_type},
                            candidate_id,
                        )
            except Exception as exc:
                code = str(exc) if isinstance(exc, MarketDataError) else "INVALID_BROKER_EVENT"
                halt(self.repo, conn, code, {"broker_order_id": broker_id}, candidate_id)
                return "QUARANTINED"
            return "RECORDED"

    def _fill(self, conn, envelope, body, normalized, link, at):
        fill_id = body.get("execution_id")
        if not isinstance(fill_id, str) or not 1 <= len(fill_id) <= 128:
            raise MarketDataError("MISSING_EXECUTION_ID")
        qty, price = decimal(body["qty"]), decimal(body["price"])
        if qty != int(qty):
            halt(
                self.repo,
                conn,
                "UNEXPECTED_FRACTIONAL_BROKER_FILL",
                {"broker_order_id": normalized["id"], "qty": qty},
                link["candidate_id"],
            )
        existing = conn.execute("SELECT * FROM lab.fills WHERE fill_id=%s", (fill_id,)).fetchone()
        if existing:
            if (
                existing["broker_order_id"] != normalized["id"]
                or existing["qty"] != qty
                or existing["price"] != price
                or existing["timestamp"] != at
            ):
                raise MarketDataError("CONFLICTING_EXECUTION_ID")
            return
        # A REST backfill (or an earlier delivery) may already hold this execution under its
        # cumulative quantity; recording it again would exceed the order quantity.
        covered = self._fill_at_cumulative(conn, normalized["id"], normalized["filled_qty"])
        if covered is not None:
            if covered["qty"] != qty or covered["price"] != price:
                raise MarketDataError("CONFLICTING_FILL_EVIDENCE")
            return
        prior = conn.execute(
            "SELECT coalesce(sum(qty),0) AS n FROM lab.fills WHERE broker_order_id=%s",
            (normalized["id"],),
        ).fetchone()["n"]
        if prior + qty > min(link["qty"], normalized["qty"]):
            raise MarketDataError("FILL_EXCEEDS_ORDER_QTY")
        conn.execute(
            """INSERT INTO lab.fills(fill_id,order_id,strategy_version,execution_source,
            qty,price,timestamp,event_id,broker_order_id,side,role)
            VALUES (%s,%s,%s,'ALPACA_PAPER',%s,%s,%s,%s,%s,%s,%s)""",
            (
                fill_id,
                link["order_id"],
                STRATEGY_VERSION,
                qty,
                price,
                at,
                envelope["event_id"],
                normalized["id"],
                link["side"],
                link["role"],
            ),
        )
        if prior + qty < normalized["filled_qty"]:
            halt(
                self.repo,
                conn,
                "FILL_HISTORY_GAP",
                {"broker_order_id": normalized["id"]},
                link["candidate_id"],
            )

    @staticmethod
    def _fill_at_cumulative(conn, broker_order_id, cumulative):
        """The recorded fill that brought ``broker_order_id`` to ``cumulative``, if any.

        Every fill row shares its envelope's event with an ``order_updates`` row carrying
        the order's cumulative filled quantity, for stream and REST evidence alike.
        """
        return conn.execute(
            """SELECT f.* FROM lab.fills f
            JOIN lab.trade_events e ON e.event_id=f.event_id
            JOIN lab.order_updates u ON u.event_seq=e.seq
            WHERE f.broker_order_id=%s AND u.filled_qty=%s LIMIT 1""",
            (broker_order_id, cumulative),
        ).fetchone()

    # --- REST backfill after a trade-updates gap -----------------------------

    def backfill_window(self, lookback=BACKFILL_LOOKBACK):
        """``(after, non-terminal linked broker order ids)``, or ``(None, [])``.

        ``after`` is the latest recorded broker timestamp less ``lookback`` (with no broker
        message yet, the earliest registration of a non-terminal linked order). Without a
        non-terminal linked order the stream cannot have missed an owned fill or change.
        """
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT s.broker_order_id,e.created_at FROM lab.broker_order_states s
                JOIN lab.trade_events e ON e.seq=s.event_seq
                WHERE NOT (s.status = ANY(%s)) ORDER BY s.event_seq""",
                (sorted(TERMINAL),),
            ).fetchall()
            last = conn.execute(
                "SELECT max(broker_timestamp) AS at FROM lab.broker_events"
            ).fetchone()["at"]
        if not rows:
            return None, []
        anchor = last if last is not None else min(row["created_at"] for row in rows)
        return anchor - lookback, [row["broker_order_id"] for row in rows]

    def record_rest_backfill(self, activities, orders, now, *, after):
        """Record what the stream missed, from REST reads, exactly once.

        ``orders`` are the current broker states of non-terminal linked orders, read
        before ``activities`` (the FILL activities since ``after``), so every fill an order
        state reflects is also among the activities. Fills are applied first, oldest
        first, then order states newer than the ledger's. A fill already recorded at the
        same (broker order id, cumulative quantity), whatever delivered it, and an order
        state the ledger already holds are skipped. An activity of an unknown order is
        quarantined exactly like an unexplained stream message, never adopted.

        Returns counts and the candidates whose ledger changed; raises only for REST
        content that is not a valid activity or order (the caller stays unready).
        """
        items = sorted(
            ((raw, normalize_fill_activity(raw)) for raw in activities),
            key=lambda pair: (pair[1]["at_ns"], pair[1]["cumulative"]),
        )
        counts = {"fills_recorded": 0, "fills_known": 0, "orders_recorded": 0,
                  "orders_known": 0, "quarantined": 0, "deferred": 0}
        touched = set()
        for raw, item in items:
            outcome, candidate_id = self._rest_fill(raw, item, now, after)
            counts[outcome] += 1
            if candidate_id is not None:
                touched.add(candidate_id)
        for raw in orders:
            outcome, candidate_id = self._rest_order(raw, now, after)
            counts[outcome] += 1
            if candidate_id is not None:
                touched.add(candidate_id)
        return {**counts, "candidates": sorted(touched, key=str)}

    def _rest_envelope(self, conn, raw, broker_order_id, event_type, at, source, after):
        """Commit the raw REST object as broker evidence, like a stream message."""
        message = json_safe(raw)
        envelope = system_event(
            self.repo,
            conn,
            REST_BACKFILL_EVENT,
            {"message": message, "source": source, "window_after": after},
        )
        conn.execute(
            "INSERT INTO lab.broker_events VALUES (%s,%s,%s,%s,%s,%s)",
            (envelope["seq"], message_digest(message), broker_order_id, event_type, at,
             Jsonb(message)),
        )
        return envelope

    @staticmethod
    def _link(conn, broker_order_id):
        return conn.execute(
            """SELECT l.*,o.candidate_id,o.qty AS entry_qty
            FROM lab.order_links l JOIN lab.orders o USING(order_id)
            WHERE l.broker_order_id=%s""",
            (broker_order_id,),
        ).fetchone()

    @staticmethod
    def _authorization_pending(conn, ticker):
        """An approved, claimed and unresolved V1 order for ``ticker`` may explain it."""
        return bool(conn.execute(
            """SELECT 1 FROM lab.risk_decisions d
            JOIN lab.authorization_claims c USING(risk_decision_id)
            LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
            WHERE d.decision='APPROVED' AND d.action IN ('ENTRY','FLATTEN')
             AND (r.outcome IS NULL OR r.outcome IN ('UNKNOWN','NOT_FOUND'))
             AND d.payload_json->>'symbol'=%s LIMIT 1""",
            (ticker,),
        ).fetchone())

    def _rest_fill(self, raw, item, now, after):
        order_id = item["order_id"]
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            if conn.execute(
                "SELECT 1 FROM lab.broker_events WHERE message_hash=%s", (message_digest(raw),)
            ).fetchone():
                return "fills_known", None  # This exact REST object is already recorded.
            link = self._link(conn, order_id)
            if not link:
                if conn.execute(
                    "SELECT 1 FROM lab.broker_events WHERE broker_order_id=%s LIMIT 1",
                    (order_id,),
                ).fetchone():
                    return "fills_known", None  # The stream already quarantined or deferred it.
                if self.pending_authorization and self._authorization_pending(
                    conn, item["symbol"]
                ):
                    return "deferred", None  # Receipt recovery links it; reconciliation checks.
                self._rest_envelope(conn, raw, order_id, item["type"], item["at"],
                                    REST_FILL_SOURCE, after)
                halt(self.repo, conn, "UNEXPLAINED_BROKER_ORDER",
                     {"broker_order_id": order_id, "source": REST_FILL_SOURCE})
                return "quarantined", None
            candidate_id = link["candidate_id"]
            covered = self._fill_at_cumulative(conn, order_id, item["cumulative"])
            if covered is not None and (
                covered["qty"] == item["qty"] and covered["price"] == item["price"]
            ):
                return "fills_known", None
            envelope = self._rest_envelope(conn, raw, order_id, item["type"], item["at"],
                                           REST_FILL_SOURCE, after)
            try:
                with conn.transaction():
                    if covered is not None:
                        raise MarketDataError("CONFLICTING_FILL_EVIDENCE")
                    order_qty = (
                        item["cumulative"] + item["leaves"]
                        if item["leaves"] is not None else link["qty"]
                    )
                    if item["at"] > now:
                        raise MarketDataError("FUTURE_BROKER_EVENT")
                    if (
                        symbol(item["symbol"]) != link["symbol"]
                        or item["side"] != link["side"]
                        or (order_qty != link["qty"] if link["role"] == "ENTRY"
                            else order_qty > link["qty"])
                    ):
                        raise MarketDataError("BROKER_ORDER_CHANGED")
                    qty = item["qty"]
                    if qty != int(qty):
                        halt(self.repo, conn, "UNEXPECTED_FRACTIONAL_BROKER_FILL",
                             {"broker_order_id": order_id, "qty": qty}, candidate_id)
                    prior = conn.execute(
                        "SELECT coalesce(sum(qty),0) AS n FROM lab.fills WHERE broker_order_id=%s",
                        (order_id,),
                    ).fetchone()["n"]
                    if prior + qty > min(link["qty"], order_qty):
                        raise MarketDataError("FILL_EXCEEDS_ORDER_QTY")
                    conn.execute(
                        """INSERT INTO lab.fills(fill_id,order_id,strategy_version,
                        execution_source,qty,price,timestamp,event_id,broker_order_id,side,role)
                        VALUES (%s,%s,%s,'ALPACA_PAPER',%s,%s,%s,%s,%s,%s,%s)""",
                        (rest_fill_id(order_id, item["cumulative"]), link["order_id"],
                         STRATEGY_VERSION, qty, item["price"], item["at"],
                         envelope["event_id"], order_id, link["side"], link["role"]),
                    )
                    conn.execute(
                        "INSERT INTO lab.order_updates VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (envelope["seq"], order_id, item["order_status"], item["cumulative"],
                         item["at"], None, int(order_qty), item["at_ns"]),
                    )
                    if prior + qty < item["cumulative"]:
                        halt(self.repo, conn, "FILL_HISTORY_GAP",
                             {"broker_order_id": order_id, "source": REST_FILL_SOURCE},
                             candidate_id)
                    self._advance(conn, link, item["type"], None, item["at"])
                    if self.after_projection:
                        self.after_projection(conn, link)
            except Exception as exc:
                code = str(exc) if isinstance(exc, MarketDataError) else "INVALID_BROKER_EVENT"
                halt(self.repo, conn, code,
                     {"broker_order_id": order_id, "source": REST_FILL_SOURCE}, candidate_id)
                return "quarantined", candidate_id
            return "fills_recorded", candidate_id

    def _rest_order(self, raw, now, after):
        normalized = normalize_order(raw)  # Invalid REST content raises: the caller retries.
        stamp = raw.get("updated_at")
        at, at_ns = timestamp(stamp), timestamp_ns(stamp)
        order_id = normalized["id"]
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            link = self._link(conn, order_id)
            if not link:
                return "orders_known", None  # Only linked orders are read; nothing to adopt.
            local = conn.execute(
                """SELECT s.status,s.filled_qty,
                (SELECT max(timestamp_ns) FROM lab.order_updates WHERE broker_order_id=%s) AS ns
                FROM lab.broker_order_states s WHERE s.broker_order_id=%s""",
                (order_id, order_id),
            ).fetchone()
            newer = local["ns"] is None or at_ns > local["ns"]
            if not newer or (
                local["status"] == normalized["status"]
                and local["filled_qty"] == normalized["filled_qty"]
            ):
                return "orders_known", None
            if conn.execute(
                "SELECT 1 FROM lab.broker_events WHERE message_hash=%s", (message_digest(raw),)
            ).fetchone():
                return "orders_known", None
            candidate_id = link["candidate_id"]
            envelope = self._rest_envelope(conn, raw, order_id, REST_ORDER_EVENT, at,
                                           REST_ORDER_SOURCE, after)
            status = normalized["status"]
            try:
                with conn.transaction():
                    if at > now:
                        raise MarketDataError("FUTURE_BROKER_EVENT")
                    if (
                        normalized["symbol"] != link["symbol"]
                        or normalized["side"] != link["side"]
                        or (normalized["qty"] != link["qty"] if link["role"] == "ENTRY"
                            else normalized["qty"] > link["qty"])
                        or normalized["type"] != link["order_type"]
                        or normalized["limit_price"] != link["limit_price"]
                        or normalized["stop_price"] != link["stop_price"]
                    ):
                        raise MarketDataError("BROKER_ORDER_CHANGED")
                    if normalized["filled_qty"] > local["filled_qty"]:
                        # The activities read after this state did not supply every fill.
                        halt(self.repo, conn, "FILL_HISTORY_GAP",
                             {"broker_order_id": order_id, "source": REST_ORDER_SOURCE},
                             candidate_id)
                    conn.execute(
                        "INSERT INTO lab.order_updates VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (envelope["seq"], order_id, status, normalized["filled_qty"], at, None,
                         int(normalized["qty"]), at_ns),
                    )
                    event_type = status if status in REST_TERMINAL_EVENTS else REST_ORDER_EVENT
                    self._advance(conn, link, event_type, None, at)
                    if self.after_projection:
                        self.after_projection(conn, link)
                    if status == "replaced":
                        halt(self.repo, conn, "BROKER_EVENT_REQUIRES_MANUAL_REVIEW",
                             {"broker_order_id": order_id, "event": status,
                              "source": REST_ORDER_SOURCE}, candidate_id)
            except Exception as exc:
                code = str(exc) if isinstance(exc, MarketDataError) else "INVALID_BROKER_EVENT"
                halt(self.repo, conn, code,
                     {"broker_order_id": order_id, "source": REST_ORDER_SOURCE}, candidate_id)
                return "quarantined", candidate_id
            return "orders_recorded", candidate_id

    def _advance(self, conn, link, event_type, reason, at):
        candidate_id = link["candidate_id"]
        current = conn.execute(
            "SELECT state FROM lab.candidate_states WHERE candidate_id=%s", (candidate_id,)
        ).fetchone()["state"]
        totals = conn.execute(
            """SELECT coalesce(sum(qty) FILTER(WHERE side='buy'),0) AS bought,
            coalesce(sum(qty) FILTER(WHERE side='sell'),0) AS sold
            FROM lab.fills WHERE order_id=%s""",
            (link["order_id"],),
        ).fetchone()
        bought, sold = totals["bought"], totals["sold"]
        reported = conn.execute(
            """SELECT coalesce(max(u.filled_qty),0) AS n
            FROM lab.order_updates u JOIN lab.order_links l USING(broker_order_id)
            WHERE l.order_id=%s AND l.role='ENTRY'""",
            (link["order_id"],),
        ).fetchone()["n"]
        entry = conn.execute(
            "SELECT status FROM lab.broker_order_states WHERE order_id=%s AND role='ENTRY'",
            (link["order_id"],),
        ).fetchone()["status"]
        controlled_exit = bool(
            self.after_projection
            and conn.execute(
                "SELECT 1 FROM lab.pending_risk_exits WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
        )

        def move(target, why):
            nonlocal current
            self.repo.transition(
                conn,
                candidate_id,
                current,
                target,
                {
                    "reason": why,
                    "broker_timestamp": at.isoformat(),
                    "broker_order_id": link["broker_order_id"],
                },
            )
            current = target

        if sold > bought:
            halt(
                self.repo,
                conn,
                "NEGATIVE_STRATEGY_POSITION",
                {"bought": bought, "sold": sold},
                candidate_id,
            )
            return
        if bought > 0 and current in {"ORDER_SUBMITTED", "CANCELED", "BROKER_REJECTED"}:
            if current in {"CANCELED", "BROKER_REJECTED"}:
                halt(self.repo, conn, "LATE_FILL_AFTER_TERMINAL_ENTRY", {}, candidate_id)
            move("FILLED" if bought == link["entry_qty"] else "PARTIALLY_FILLED", "BROKER_FILL")
        if bought == link["entry_qty"] and current == "PARTIALLY_FILLED":
            move("FILLED", "BROKER_FILL")
        if current == "FILLED":
            move("OPEN", "ENTRY_FILLED")
        if bought > 0 and bought < link["entry_qty"] and entry in TERMINAL:
            if current == "PARTIALLY_FILLED":
                move("OPEN", "PARTIAL_ENTRY_REMAINDER_TERMINATED")
                if not controlled_exit:
                    halt(self.repo, conn, "PARTIAL_ENTRY_PROTECTION_UNVERIFIED", {}, candidate_id)
        if current == "CLOSED" and bought > sold:
            move("OPEN", "LATE_BROKER_FILL_REOPENED")
            halt(self.repo, conn, "LATE_FILL_REOPENED_POSITION", {}, candidate_id)
        if (
            bought == 0
            and reported == 0
            and current == "ORDER_SUBMITTED"
            and entry in {"canceled", "expired", "rejected"}
        ):
            move("BROKER_REJECTED" if entry == "rejected" else "CANCELED", reason or entry.upper())
        if bought > 0 and sold == bought and entry in TERMINAL and current == "OPEN":
            exit_fill = conn.execute(
                """SELECT role FROM lab.fills WHERE order_id=%s AND side='sell'
                ORDER BY timestamp DESC,fill_id DESC LIMIT 1""",
                (link["order_id"],),
            ).fetchone()
            exit_reason = {
                "STOP": "STOP_EXIT",
                "TARGET": "TARGET_EXIT",
                "TIME_EXIT": "TIME_EXIT",
                "EMERGENCY_EXIT": "EMERGENCY_EXIT",
            }[exit_fill["role"]]
            move(exit_reason, "BROKER_EXIT_FILL")
            move("CLOSED", exit_reason)
        if (
            event_type in {"rejected", "canceled", "expired"}
            and link["role"] != "ENTRY"
            and bought > sold
            and not (
                self.after_projection
                and conn.execute(
                    "SELECT 1 FROM lab.pending_risk_exits WHERE candidate_id=%s", (candidate_id,)
                ).fetchone()
            )
        ):
            halt(
                self.repo,
                conn,
                "PROTECTIVE_EXIT_TERMINATED",
                {"reason": reason or event_type},
                candidate_id,
            )
