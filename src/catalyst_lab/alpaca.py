import os
import re
from dataclasses import dataclass, field
from datetime import date

import httpx
from websockets.sync.client import reconnect

from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.market import MarketDataError, Observation, Session, symbol

TRADE_STREAM_ENDPOINT = "wss://paper-api.alpaca.markets/stream"
CRYPTO_STREAM_ENDPOINT = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"

DATA_ENDPOINT = "https://data.alpaca.markets"
STREAM_ENDPOINTS = {
    "iex": "wss://stream.data.alpaca.markets/v2/iex",
    "sip": "wss://stream.data.alpaca.markets/v2/sip",
}
# Account evidence both engines read. The margin fields feed the pre-trade buying-power check
# (risk_math.buying_power_check); the account identifier is never part of this snapshot.
ACCOUNT_FIELDS = (
    "status",
    "currency",
    "equity",
    "last_equity",
    "cash",
    "trading_blocked",
    "account_blocked",
    "trade_suspended_by_user",
    "multiplier",
    "buying_power",
    "regt_buying_power",
    "non_marginable_buying_power",
    "initial_margin",
    "maintenance_margin",
    "shorting_enabled",
    "crypto_status",
)


class ReadOnlyPaperTransport(httpx.BaseTransport):
    """The HTTP boundary itself refuses mutations and destinations outside the allowlist."""

    def __init__(self, delegate=None):
        self.delegate = delegate or httpx.HTTPTransport()

    def handle_request(self, request):
        from catalyst_lab.execution import SubmissionDisabled

        if request.method != "GET":
            raise SubmissionDisabled("PHASE_4_RISK_GATE_REQUIRED")
        allowed = {
            "paper-api.alpaca.markets": {
                "/v2/account",
                "/v2/assets",
                "/v2/account/activities",
                "/v2/clock",
                "/v2/calendar",
                "/v2/positions",
                "/v2/orders",
                "/v2/orders:by_client_order_id",
            },
            "data.alpaca.markets": {"/v2/stocks/quotes/latest", "/v2/stocks/bars"},
        }
        url = request.url
        if (
            url.scheme != "https"
            or url.port not in {None, 443}
            or url.userinfo
            or (
                url.path not in allowed.get(url.host, set())
                and not (
                    url.host == "paper-api.alpaca.markets"
                    and (
                        re.fullmatch(r"/v2/orders/[a-zA-Z0-9-]{1,128}", url.path)
                        or re.fullmatch(r"/v2/assets/[A-Z0-9.-]{1,16}(?:/[A-Z0-9]{1,8})?", url.path)
                    )
                )
            )
        ):
            raise MarketDataError("ENDPOINT_NOT_ALLOWED")
        if not re.fullmatch(r"PK[A-Z0-9]{8,62}", request.headers.get("APCA-API-KEY-ID", "")):
            raise MarketDataError("PAPER_CREDENTIAL_REQUIRED")
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


class FixedStreamConnection(reconnect):
    """Disable the library's default redirects before any authentication is sent."""

    def __init__(self, uri, **kwargs):
        if uri not in {*STREAM_ENDPOINTS.values(), TRADE_STREAM_ENDPOINT, CRYPTO_STREAM_ENDPOINT}:
            raise ValueError("Only fixed Alpaca market streams are permitted")
        super().__init__(uri, **kwargs)

    def process_redirect(self, exc):
        return exc


@dataclass(frozen=True)
class AlpacaCredentials:
    key_id: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self):
        # Prefix is a conservative local allowlist, not proof of account environment.
        # Successful authentication at the fixed paper host supplies that proof.
        if not re.fullmatch(r"PK[A-Z0-9]{8,62}", self.key_id or ""):
            raise ValueError("Only recognized paper key identifiers are permitted")
        if not self.secret or any(c.isspace() for c in self.secret):
            raise ValueError("A non-empty Alpaca secret is required")

    @classmethod
    def from_env(cls):
        for variable in ("APCA_API_BASE_URL", "ALPACA_BASE_URL"):
            if os.environ.get(variable, PAPER_ENDPOINT) != PAPER_ENDPOINT:
                raise ValueError("Only the fixed Alpaca Paper endpoint is permitted")
        if os.environ.get("APCA_DATA_BASE_URL", DATA_ENDPOINT) != DATA_ENDPOINT:
            raise ValueError("Only the fixed Alpaca data endpoint is permitted")
        key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
        if not key or not secret:
            raise ValueError("Alpaca Paper credentials are required in environment variables")
        return cls(key, secret)


class AlpacaReadOnly:
    """GET-only paper/data adapter. No order mutation API or configurable destination."""

    def __init__(self, credentials, feed="iex", *, transport=None):
        if feed not in STREAM_ENDPOINTS:
            raise ValueError("Use an explicit iex or sip feed")
        self.credentials, self.feed = credentials, feed
        self._client = httpx.Client(
            headers={
                "APCA-API-KEY-ID": credentials.key_id,
                "APCA-API-SECRET-KEY": credentials.secret,
            },
            follow_redirects=False,
            trust_env=False,
            timeout=8,
            transport=ReadOnlyPaperTransport(transport),
        )

    def close(self):
        self._client.close()

    def _get(self, route, params=None):
        paths = {
            "account": (PAPER_ENDPOINT, "/v2/account"),
            "clock": (PAPER_ENDPOINT, "/v2/clock"),
            "calendar": (PAPER_ENDPOINT, "/v2/calendar"),
            "positions": (PAPER_ENDPOINT, "/v2/positions"),
            "orders": (PAPER_ENDPOINT, "/v2/orders"),
            "quotes": (DATA_ENDPOINT, "/v2/stocks/quotes/latest"),
            "bars": (DATA_ENDPOINT, "/v2/stocks/bars"),
        }
        if route not in paths:
            raise MarketDataError("ENDPOINT_NOT_ALLOWED")
        base, path = paths[route]
        try:
            response = self._client.get(base + path, params=params)
            if response.status_code != 200:
                raise MarketDataError(f"ALPACA_HTTP_{response.status_code}")
            return response.json()
        except httpx.HTTPError:
            raise MarketDataError("ALPACA_CONNECTION_ERROR") from None
        except ValueError:
            raise MarketDataError("ALPACA_INVALID_JSON") from None

    def account(self):
        data = self._get("account")
        if not isinstance(data, dict):
            raise MarketDataError("INVALID_ACCOUNT_RESPONSE")
        return {k: data.get(k) for k in ACCOUNT_FIELDS}

    def clock(self):
        return self._get("clock")

    def calendar(self, start: date, end: date):
        rows = self._get("calendar", {"start": start.isoformat(), "end": end.isoformat()})
        if not isinstance(rows, list):
            raise MarketDataError("INVALID_CALENDAR")
        return [Session.from_calendar(row) for row in rows]

    def positions(self):
        rows = self._get("positions")
        if not isinstance(rows, list):
            raise MarketDataError("INVALID_POSITIONS_RESPONSE")
        return rows

    def capital_activities(self, session_date):
        """Cash/securities transfers affect equity but must not masquerade as trading P&L."""
        result, token = [], None
        while True:
            params = {
                "date": session_date.isoformat(),
                "direction": "asc",
                "page_size": 100,
                "activity_types": "CSD,CSW,ACATC,JNLC,JNL,JNLS,ACATS,FOPT",
            }
            if token:
                params["page_token"] = token
            try:
                response = self._client.get(
                    PAPER_ENDPOINT + "/v2/account/activities", params=params
                )
                if response.status_code != 200:
                    raise MarketDataError(f"ALPACA_HTTP_{response.status_code}")
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError
            except httpx.HTTPError:
                raise MarketDataError("ALPACA_CONNECTION_ERROR") from None
            except ValueError:
                raise MarketDataError("INVALID_CAPITAL_ACTIVITIES") from None
            result.extend(rows)
            if len(rows) < 100:
                return result
            next_token = rows[-1].get("id")
            if not next_token or next_token == token or len(result) > 10000:
                raise MarketDataError("INCOMPLETE_CAPITAL_ACTIVITIES")
            token = next_token

    def open_orders(self):
        rows = self._get("orders", {"status": "open", "nested": "true", "limit": 500})
        if not isinstance(rows, list) or len(rows) >= 500:
            raise MarketDataError("INCOMPLETE_ORDERS_RESPONSE")
        return rows

    def quotes(self, tickers):
        if not tickers:
            return []
        names = sorted({symbol(ticker) for ticker in tickers})
        data = self._get("quotes", {"symbols": ",".join(names), "feed": self.feed})
        try:
            return [
                Observation.from_wire({**q, "S": name, "T": "q"}, self.feed)
                for name, q in data["quotes"].items()
                if q and name in names
            ]
        except (KeyError, TypeError, AttributeError):
            raise MarketDataError("INVALID_QUOTE_RESPONSE") from None

    def daily_bars(self, ticker, start, end):
        name, result, seen, token = symbol(ticker), [], set(), None
        for _ in range(10):
            params = {
                "symbols": name,
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "feed": self.feed,
                "adjustment": "raw",
                "currency": "USD",
                "sort": "asc",
                "limit": 1000,
            }
            if token:
                params["page_token"] = token
            data = self._get("bars", params)
            try:
                rows = data["bars"].get(name, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError
                if set(data["bars"]) - {name}:
                    raise ValueError
                result.extend(rows)
                token = data.get("next_page_token")
                if token is None:
                    return result
                if not isinstance(token, str) or not token or token in seen:
                    raise ValueError
                seen.add(token)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise MarketDataError("INCOMPLETE_DAILY_BARS") from None
        raise MarketDataError("INCOMPLETE_DAILY_BARS")

    def order_by_client_id(self, client_order_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", client_order_id):
            raise MarketDataError("INVALID_CLIENT_ORDER_ID")
        return self._order_get(
            "/v2/orders:by_client_order_id", {"client_order_id": client_order_id}
        )

    def order(self, broker_order_id):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", broker_order_id):
            raise MarketDataError("INVALID_BROKER_ORDER_ID")
        return self._order_get("/v2/orders/" + broker_order_id, {"nested": "true"})

    def asset(self, ticker):
        return self._order_get("/v2/assets/" + symbol(ticker), {})

    def _order_get(self, path, params):
        try:
            response = self._client.get(PAPER_ENDPOINT + path, params=params)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise MarketDataError(f"ALPACA_HTTP_{response.status_code}")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except httpx.HTTPError:
            raise MarketDataError("ALPACA_CONNECTION_ERROR") from None
        except ValueError:
            raise MarketDataError("ALPACA_INVALID_JSON") from None

    def fill_activities_since(self, after, *, page_size=100, max_rows=10000):
        """Every FILL account activity after ``after`` (an aware time), oldest first.

        Alpaca FILL activities carry the broker order id, the incremental quantity and
        price, the order's cumulative filled quantity and status, and no execution id.
        Pages follow ``capital_activities``: the next ``page_token`` is the last id of a
        full page. A short page ends the read; a repeated token or more than ``max_rows``
        rows is refused rather than returned incomplete.
        """
        from datetime import UTC, datetime

        if not isinstance(after, datetime) or after.tzinfo is None:
            raise MarketDataError("AWARE_ACTIVITY_WINDOW_REQUIRED")
        since = after.astimezone(UTC).isoformat().replace("+00:00", "Z")
        result, token, seen = [], None, set()
        while True:
            params = {
                "activity_types": "FILL",
                "after": since,
                "direction": "asc",
                "page_size": page_size,
            }
            if token:
                params["page_token"] = token
            try:
                response = self._client.get(
                    PAPER_ENDPOINT + "/v2/account/activities", params=params
                )
                if response.status_code != 200:
                    raise MarketDataError(f"ALPACA_HTTP_{response.status_code}")
                rows = response.json()
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    raise ValueError
            except httpx.HTTPError:
                raise MarketDataError("ALPACA_CONNECTION_ERROR") from None
            except ValueError:
                raise MarketDataError("INVALID_FILL_ACTIVITIES") from None
            result.extend(rows)
            if len(rows) < page_size:
                return result
            next_token = rows[-1].get("id")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token in seen
                or len(result) >= max_rows
            ):
                raise MarketDataError("INCOMPLETE_FILL_ACTIVITIES")
            seen.add(next_token)
            token = next_token

    def fee_activities_since(self, after, *, page_size=100, max_rows=10000):
        """Every CFEE/FEE account activity after ``after`` (an aware time), oldest first.

        Package fees-net-r (plan phase 0): Alpaca charges crypto fees on what is
        received (a buy's fee comes out of the coin, a sell's out of the USD proceeds)
        and reports them as ``CFEE``/``FEE`` account activities (docs.alpaca.markets/us/
        docs/crypto-fees). The live paper account's rows, read on 2026-09-29, carry no order
        id and no transaction time; ``managed_analytics.normalize_fee_activity`` documents
        both shapes (ALPACA_FEE_MATCH_V2). ``after`` filters by activity date there, so a
        read returns the whole day of ``after``. Paging mirrors ``fill_activities_since``.
        """
        from datetime import UTC, datetime

        if not isinstance(after, datetime) or after.tzinfo is None:
            raise MarketDataError("AWARE_ACTIVITY_WINDOW_REQUIRED")
        since = after.astimezone(UTC).isoformat().replace("+00:00", "Z")
        result, token, seen = [], None, set()
        while True:
            params = {
                "activity_types": "CFEE,FEE",
                "after": since,
                "direction": "asc",
                "page_size": page_size,
            }
            if token:
                params["page_token"] = token
            try:
                response = self._client.get(
                    PAPER_ENDPOINT + "/v2/account/activities", params=params
                )
                if response.status_code != 200:
                    raise MarketDataError(f"ALPACA_HTTP_{response.status_code}")
                rows = response.json()
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    raise ValueError
            except httpx.HTTPError:
                raise MarketDataError("ALPACA_CONNECTION_ERROR") from None
            except ValueError:
                raise MarketDataError("INVALID_FEE_ACTIVITIES") from None
            result.extend(rows)
            if len(rows) < page_size:
                return result
            next_token = rows[-1].get("id")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token in seen
                or len(result) >= max_rows
            ):
                raise MarketDataError("INCOMPLETE_FEE_ACTIVITIES")
            seen.add(next_token)
            token = next_token


class AlpacaPaperClient(AlpacaReadOnly):
    """Order machinery API; mutations have no transport implementation in Phase 3."""

    @property
    def trading_enabled(self):
        return False

    def submit_bracket(self, payload):
        from catalyst_lab.execution import SubmissionDisabled

        raise SubmissionDisabled("PHASE_4_RISK_GATE_REQUIRED")

    def cancel_order(self, broker_order_id):
        from catalyst_lab.execution import SubmissionDisabled

        raise SubmissionDisabled("PHASE_4_RISK_GATE_REQUIRED")

    def flatten_position(self, payload):
        from catalyst_lab.execution import SubmissionDisabled

        raise SubmissionDisabled("PHASE_4_RISK_GATE_REQUIRED")
