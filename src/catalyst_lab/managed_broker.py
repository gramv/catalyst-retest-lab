"""Concrete paper gateway for the managed controller, with no credential discovery.

Every mutation crosses AuthorizedPaperTransport and its database-backed gate. Read
methods expose broker evidence only; no configuration can choose another destination.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

import httpx

from catalyst_lab.alpaca import ACCOUNT_FIELDS
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.market import MarketDataError, decimal, timestamp, timestamp_ns
from catalyst_lab.paper_execution import (
    BrokerMutationUnknown,
    RiskAuthorizedPaperClient,
    rejected_mutation,
)

SENSITIVE_KEYS = frozenset(
    {
        "account_id",
        "account_number",
        "account_uuid",
        "account_no",
        "account",
        "api_key",
        "api_secret",
        "key_id",
        "secret",
        "secret_key",
        "access_token",
        "refresh_token",
        "authorization",
        "apca_api_key_id",
        "apca_api_secret_key",
        "email",
        "name",
        "first_name",
        "last_name",
        "address",
        "phone_number",
    }
)


def sanitize_broker_payload(raw):
    """Keep broker data intact except explicit secret/owner identity fields.

    Never call a sanitized copy an unmodified receipt. The returned redaction paths
    document the transformation; request credentials are never part of this payload.
    """
    removed = []

    def walk(value, path):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError
                next_path = f"{path}.{key}" if path else key
                if key.lower().replace("-", "_") in SENSITIVE_KEYS:
                    removed.append(next_path)
                else:
                    result[key] = walk(item, next_path)
            return result
        if isinstance(value, list):
            return [walk(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, (str, bool, int, float)) or value is None:
            return value
        raise ValueError

    try:
        result = walk(raw, "")
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError):
        raise MarketDataError("INVALID_BROKER_JSON") from None
    return result, tuple(removed)


class ManagedPaperBroker(RiskAuthorizedPaperClient):
    """Credentials and gate are injected by the executable runtime, never read here."""

    def account_identity(self):
        """An opaque lock key; never persist or expose the broker account identifier."""
        raw = self._get("account")
        identity = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(identity, str) or not identity.strip():
            raise MarketDataError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
        return hashlib.sha256(("ALPACA_PAPER:" + identity).encode()).hexdigest()

    def account(self):
        raw = self._get("account")
        if not isinstance(raw, dict):
            raise MarketDataError("INVALID_ACCOUNT_RESPONSE")
        # Never the account identifier: margin fields feed the pre-trade buying-power check.
        return {
            key: raw.get(key)
            for key in (*ACCOUNT_FIELDS, "long_market_value", "short_market_value")
        }

    def positions(self):
        return sanitize_broker_payload(super().positions())[0]

    def open_orders(self):
        return sanitize_broker_payload(super().open_orders())[0]

    def order(self, broker_order_id):
        return sanitize_broker_payload(super().order(broker_order_id))[0]

    def order_by_client_id(self, client_order_id):
        return sanitize_broker_payload(super().order_by_client_id(client_order_id))[0]

    def asset(self, symbol):
        if not isinstance(symbol, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9.-]{0,15}(?:/USD)?", symbol
        ):
            raise MarketDataError("INVALID_SYMBOL")
        raw = self._order_get("/v2/assets/" + quote(symbol, safe=""), {})
        return sanitize_broker_payload(raw)[0]

    def capital_activities(self, session_date):
        fields = {
            "id",
            "activity_type",
            "date",
            "net_amount",
            "qty",
            "symbol",
            "price",
            "status",
            "transaction_time",
            "order_id",
            "side",
        }
        rows = super().capital_activities(session_date)
        if any(not isinstance(row, dict) for row in rows):
            raise MarketDataError("INVALID_CAPITAL_ACTIVITIES")
        return [{key: value for key, value in row.items() if key in fields} for row in rows]

    def mutate(self, method, path, payload, decision_id):
        """No retries. Unknown responses must be reconciled by the durable controller."""
        if decision_id is None:
            raise SubmissionDisabled("RISK_DECISION_REQUIRED")
        route = re.fullmatch(r"/v2/orders/[A-Za-z0-9-]{1,128}", path or "")
        if not isinstance(payload, dict) or not (
            (method == "POST" and path == "/v2/orders") or (method in {"DELETE", "PATCH"} and route)
        ):
            raise SubmissionDisabled("BROKER_MUTATION_NOT_ALLOWED")
        if method == "DELETE" and payload:
            raise SubmissionDisabled("CANCEL_PAYLOAD_NOT_ALLOWED")
        try:
            json.dumps(payload, allow_nan=False)
        except (ValueError, TypeError):
            raise SubmissionDisabled("INVALID_BROKER_PAYLOAD") from None
        if method == "PATCH":
            if not payload or set(payload) - {"limit_price", "stop_price"}:
                raise SubmissionDisabled("PRICE_AMENDMENT_ONLY")
            try:
                for value in payload.values():
                    decimal(value)
            except MarketDataError:
                raise SubmissionDisabled("INVALID_AMENDMENT_PRICE") from None
        try:
            response = self._client.request(
                method,
                PAPER_ENDPOINT + path,
                json=payload if method != "DELETE" else None,
                extensions={"risk_decision_id": str(decision_id)},
            )
        except httpx.HTTPError:
            raise BrokerMutationUnknown("BROKER_RESPONSE_UNKNOWN") from None
        if response.status_code in {200, 201, 204}:
            if response.status_code == 204:
                if method != "DELETE":
                    raise BrokerMutationUnknown("BROKER_RECEIPT_MISSING")
                return {}
            try:
                raw = response.json()
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise ValueError
                if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", raw["id"]):
                    raise ValueError
                result, _ = sanitize_broker_payload(raw)
                return result
            except (ValueError, MarketDataError):
                raise BrokerMutationUnknown("BROKER_RESPONSE_INVALID") from None
        if response.status_code not in {400, 401, 403, 404, 422}:
            raise BrokerMutationUnknown(f"BROKER_HTTP_{response.status_code}")
        raise rejected_mutation(response)


@dataclass(frozen=True)
class ManagedTradeUpdate:
    event_id: str
    execution_id: str | None
    event_type: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    asset_class: str
    side: str
    status: str
    order_qty: Decimal
    cumulative_fill_qty: Decimal
    fill_qty: Decimal | None
    fill_price: Decimal | None
    position_qty: Decimal | None
    occurred_at: datetime
    occurred_at_ns: int
    payload: dict
    payload_hash: str
    redacted_paths: tuple[str, ...]


def normalize_trade_update(raw):
    """Normalize authenticated single-asset stock/crypto stream evidence only.

    Fill qty/price are the event's incremental execution, never order averages or
    cumulative quantity. A fill without an execution ID is quarantined by rejection.
    Non-fill streams need not supply an event ID; a canonical payload hash supplies
    deterministic local deduplication while retaining that distinction explicitly.
    """
    payload, redactions = sanitize_broker_payload(raw)
    try:
        if not isinstance(payload, dict):
            raise ValueError
        if "stream" in payload:
            if payload["stream"] != "trade_updates":
                raise ValueError
            body = payload["data"]
        else:
            body = payload
        order = body["order"]
        kind = body["event"]
        allowed = {
            "new",
            "fill",
            "partial_fill",
            "canceled",
            "expired",
            "done_for_day",
            "replaced",
            "accepted",
            "rejected",
            "pending_new",
            "stopped",
            "pending_cancel",
            "pending_replace",
            "calculated",
            "suspended",
            "order_replace_rejected",
            "order_cancel_rejected",
        }
        if kind not in allowed or not isinstance(order, dict):
            raise ValueError
        broker_id, client_id = order["id"], order["client_order_id"]
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", broker_id) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", client_id
        ):
            raise ValueError
        asset_class, name = order["asset_class"], order["symbol"]
        if asset_class == "crypto":
            if "/" not in name and name.endswith("USD"):
                name = name[:-3] + "/USD"
            if not re.fullmatch(r"[A-Z0-9]{1,16}/USD", name):
                raise ValueError
        elif asset_class == "us_equity":
            if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", name):
                raise ValueError
        else:
            raise ValueError
        if order["side"] not in {"buy", "sell"} or not isinstance(order["status"], str):
            raise ValueError
        qty, cumulative = decimal(order["qty"]), decimal(order["filled_qty"], positive=False)
        if cumulative > qty:
            raise ValueError
        if asset_class == "us_equity" and (qty != int(qty) or cumulative != int(cumulative)):
            raise ValueError
        fill_qty = fill_price = position_qty = execution_id = None
        if kind in {"fill", "partial_fill"}:
            execution_id = body["execution_id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
                raise ValueError
            fill_qty, fill_price = decimal(body["qty"]), decimal(body["price"])
            if fill_qty > cumulative or (asset_class == "us_equity" and fill_qty != int(fill_qty)):
                raise ValueError
            position_qty = Decimal(str(body["position_qty"]))
            if not position_qty.is_finite():
                raise ValueError
            stamp = body["timestamp"]
        else:
            stamp = body.get("timestamp") or order["updated_at"]
        at, at_ns = timestamp(stamp), timestamp_ns(stamp)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        event_id = body.get("event_id") or execution_id or "sha256:" + digest
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", event_id):
            raise ValueError
        return ManagedTradeUpdate(
            event_id,
            execution_id,
            kind,
            broker_id,
            client_id,
            name,
            asset_class,
            order["side"],
            order["status"],
            qty,
            cumulative,
            fill_qty,
            fill_price,
            position_qty,
            at,
            at_ns,
            payload,
            digest,
            redactions,
        )
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, MarketDataError):
        raise MarketDataError("INVALID_MANAGED_TRADE_UPDATE") from None
