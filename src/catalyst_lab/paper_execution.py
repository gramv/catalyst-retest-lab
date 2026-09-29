"""Fixed paper broker mutations, each guarded at the final HTTP transport boundary."""

import json
import re
from contextlib import nullcontext

import httpx

from catalyst_lab.alpaca import STREAM_ENDPOINTS, AlpacaPaperClient, ReadOnlyPaperTransport
from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.market import MarketDataError

# Alpaca's forbidden code covers insufficient buying power and insufficient quantity alike;
# the message tells them apart.
ALPACA_FORBIDDEN_CODE = 40310000
EVIDENCE_MESSAGE_LIMIT = 200
_REDACT = (
    re.compile(r"PK[A-Z0-9]{8,}"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)(?:secret|password|passwd|token|api[_-]?key|key[_-]?id)\S*\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z0-9+/_=-]{32,}"),
)


class BrokerMutationUnknown(Exception):
    pass


class BrokerMutationRejected(Exception):
    """The broker definitively refused the request; ``str(exc)`` is the classified reason.

    ``evidence`` holds only the HTTP status and the sanitized, truncated broker ``code`` and
    ``message``; request headers and credentials are never part of it.
    """

    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence or {})

    def __str__(self):
        return self.reason


def _broker_code(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    return None


def _broker_message(value):
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in " ".join(value.split()) if ch.isprintable())
    for pattern in _REDACT:
        text = pattern.sub("[REDACTED]", text)
    return text[:EVIDENCE_MESSAGE_LIMIT]


def classify_rejection(http_status, code, message):
    text = (message or "").lower()
    if code == ALPACA_FORBIDDEN_CODE and "buying power" in text:
        return "BROKER_MARGIN_REJECTED"
    if "qty available" in text:
        return "BROKER_QTY_UNAVAILABLE"
    if http_status == 401:
        return "BROKER_AUTH_REJECTED"
    if http_status == 422:
        return "BROKER_INVALID_REQUEST"
    return "BROKER_REJECTED"


def rejected_mutation(response):
    """A classified BrokerMutationRejected from a definite 4xx broker refusal."""
    try:
        body = json.loads(response.content[:8192])
    except (ValueError, UnicodeError):
        body = None
    body = body if isinstance(body, dict) else {}
    code, message = _broker_code(body.get("code")), _broker_message(body.get("message"))
    evidence = {"http_status": response.status_code, "code": code, "message": message}
    return BrokerMutationRejected(
        classify_rejection(response.status_code, code, message), evidence
    )


class AuthorizedPaperTransport(httpx.BaseTransport):
    def __init__(self, gate: AuthorizationGate, delegate=None):
        if not isinstance(gate, AuthorizationGate):
            raise ValueError("A database-backed authorization gate is required")
        self.gate = gate
        self.delegate = delegate or httpx.HTTPTransport(retries=0)
        self.read_only = ReadOnlyPaperTransport(self.delegate)

    def handle_request(self, request):
        if request.method == "GET":
            return self.read_only.handle_request(request)
        u = request.url
        valid_route = (request.method == "POST" and u.path == "/v2/orders") or (
            request.method in {"DELETE", "PATCH"}
            and re.fullmatch(r"/v2/orders/[A-Za-z0-9-]{1,128}", u.path)
        )
        if (
            u.scheme != "https"
            or u.host != "paper-api.alpaca.markets"
            or u.port not in {None, 443}
            or u.userinfo
            or u.query
            or not valid_route
        ):
            raise SubmissionDisabled("BROKER_MUTATION_NOT_ALLOWED")
        if not re.fullmatch(r"PK[A-Z0-9]{8,62}", request.headers.get("APCA-API-KEY-ID", "")):
            raise SubmissionDisabled("PAPER_CREDENTIAL_REQUIRED")
        guard = getattr(self.gate, "dispatch_guard", nullcontext)
        with guard():
            self.gate.claim(request)
            return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


class RiskAuthorizedPaperClient(AlpacaPaperClient):
    """No global enable flag: every call must carry a fresh, matching, unused decision ID."""

    def __init__(self, credentials, gate, feed="iex", *, transport=None):
        if feed not in STREAM_ENDPOINTS:
            raise ValueError("Explicit feed required")
        self.credentials, self.feed = credentials, feed
        self._client = httpx.Client(
            headers={
                "APCA-API-KEY-ID": credentials.key_id,
                "APCA-API-SECRET-KEY": credentials.secret,
            },
            follow_redirects=False,
            trust_env=False,
            timeout=8,
            transport=AuthorizedPaperTransport(gate, transport),
        )

    def _mutate(self, method, path, payload, decision_id):
        if decision_id is None:
            raise SubmissionDisabled("RISK_DECISION_REQUIRED")
        try:
            response = self._client.request(
                method,
                PAPER_ENDPOINT + path,
                json=payload if method in {"POST", "PATCH"} else None,
                extensions={"risk_decision_id": str(decision_id)},
            )
        except httpx.HTTPError:
            raise BrokerMutationUnknown("BROKER_RESPONSE_UNKNOWN") from None
        if response.status_code in {200, 201, 204}:
            if response.status_code == 204:
                return {}
            try:
                result = response.json()
                if not isinstance(result, dict) or not result.get("id"):
                    raise ValueError
                return result
            except ValueError:
                raise BrokerMutationUnknown("BROKER_RESPONSE_INVALID") from None
        if response.status_code not in {400, 401, 403, 404, 422}:
            raise BrokerMutationUnknown(f"BROKER_HTTP_{response.status_code}")
        raise rejected_mutation(response)

    def submit_bracket(self, payload, *, risk_decision_id=None):
        return self._mutate("POST", "/v2/orders", payload, risk_decision_id)

    def flatten_position(self, payload, *, risk_decision_id=None):
        return self._mutate("POST", "/v2/orders", payload, risk_decision_id)

    def cancel_order(self, broker_order_id, *, risk_decision_id=None):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", broker_order_id):
            raise MarketDataError("INVALID_BROKER_ORDER_ID")
        return self._mutate("DELETE", "/v2/orders/" + broker_order_id, {}, risk_decision_id)
