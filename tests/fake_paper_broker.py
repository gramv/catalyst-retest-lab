"""Deterministic simulated paper venue; used only by disposable database tests."""

import copy
import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx

from tests.conftest import NOW


class FakePaperBroker:
    def __init__(self):
        self.now = NOW
        self.equity = D("10000")
        self.last_equity = self.equity
        self.cash = D("10000")
        self.positions = {}
        self.orders = {}
        self.root_ids = []
        self.events = []
        self.calls = []
        self.timeout_after_accept = False
        self.timeout_without_accept = False
        self.fill_on_cancel = False
        self.event_number = 0
        self.calendar_close = "16:00"
        # FILL account activities (no execution id), served with Alpaca's pagination.
        self.activities = []
        self.activity_status = None  # e.g. 429: every FILL activity read is refused.
        self.activity_reads = []

    def _event(self, kind, order, qty="0", price="100.1"):
        self.event_number += 1
        stamp = (
            self.now - timedelta(seconds=1) + timedelta(microseconds=self.event_number)
        ).isoformat()
        order["updated_at"] = stamp
        self.events.append(
            {
                "event": kind,
                "execution_id": str(uuid4()),
                "timestamp": stamp,
                "qty": str(qty),
                "price": str(price),
                "order": copy.deepcopy(order),
            }
        )
        if kind in {"fill", "partial_fill"}:
            self.activities.append(
                {
                    "activity_type": "FILL",
                    "id": f"{self.event_number:020d}-{uuid4()}",
                    "order_id": order["id"],
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "type": kind,
                    "qty": str(qty),
                    "price": str(price),
                    "cum_qty": order["filled_qty"],
                    "leaves_qty": str(D(order["qty"]) - D(order["filled_qty"])),
                    "order_status": order["status"],
                    "transaction_time": stamp,
                }
            )

    def execute(self, order_id, qty, price="100.1"):
        """One execution of ``qty`` more shares; cumulative quantity and position follow."""
        order = self.orders[order_id]
        qty = D(str(qty))
        filled = D(order["filled_qty"]) + qty
        assert filled <= D(order["qty"])
        order["filled_qty"] = str(filled)
        full = filled == D(order["qty"])
        order["status"] = "filled" if full else "partially_filled"
        signed = qty if order["side"] == "buy" else -qty
        position = self.positions.setdefault(
            order["symbol"],
            {"symbol": order["symbol"], "qty": "0", "unrealized_pl": "0",
             "asset_class": "us_equity"},
        )
        position["qty"] = str(D(position["qty"]) + signed)
        if D(position["qty"]) == 0:
            del self.positions[order["symbol"]]
        for leg in order.get("legs") or []:
            leg["status"] = "new" if full and leg["type"] == "limit" else "held"
        self._event("fill" if full else "partial_fill", order, qty, price)

    def _fill_activities(self, params):
        self.activity_reads.append(dict(params))
        if self.activity_status is not None:
            return httpx.Response(self.activity_status, json={"message": "fixture refusal"})
        def moment(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        after = moment(params["after"]) if params.get("after") else None
        rows = sorted(
            (a for a in self.activities
             if after is None or moment(a["transaction_time"]) > after),
            key=lambda a: (moment(a["transaction_time"]), a["id"]),
        )
        if params.get("page_token"):
            ids = [a["id"] for a in rows]
            rows = rows[ids.index(params["page_token"]) + 1:]
        return httpx.Response(200, json=rows[: int(params.get("page_size", 100))])

    def fill_entry(self, root_id, *, qty=None, stop_rejected=False):
        root = self.orders[root_id]
        qty = D(str(qty or root["qty"]))
        root["filled_qty"] = str(qty)
        full = qty == D(root["qty"])
        root["status"] = "filled" if full else "partially_filled"
        self.positions[root["symbol"]] = {
            "symbol": root["symbol"],
            "qty": str(qty),
            "unrealized_pl": "0",
            "asset_class": "us_equity",
        }
        self.cash -= qty * D("100.1")
        # Alpaca staff describe a full fill as take-profit ``new`` with the stop-loss leg
        # still ``held``; a partial fill leaves both legs ``held``.
        for leg in root["legs"]:
            leg["status"] = "new" if full and leg["type"] == "limit" else "held"
        stop = next((leg for leg in root["legs"] if leg["type"] == "stop"), None)
        if stop_rejected:
            stop["status"] = "rejected"
        self._event("fill" if full else "partial_fill", root, qty)
        if stop_rejected:
            self._event("rejected", stop)

    def handle(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path, request.extensions.get("risk_decision_id")))
        if method == "GET":
            if path.startswith("/v2/assets/"):
                return httpx.Response(
                    200,
                    json={
                        "symbol": path.rsplit("/", 1)[-1],
                        "class": "us_equity",
                        "status": "active",
                        "tradable": True,
                    },
                )
            if path == "/v2/account":
                return httpx.Response(
                    200,
                    json={
                        "status": "ACTIVE",
                        "currency": "USD",
                        "equity": str(self.equity),
                        "last_equity": str(self.last_equity),
                        "cash": str(self.cash),
                        "multiplier": "1",
                        "buying_power": str(self.cash),
                        "regt_buying_power": str(self.cash),
                        "non_marginable_buying_power": str(self.cash),
                        "initial_margin": "0",
                        "maintenance_margin": "0",
                        "shorting_enabled": False,
                        "crypto_status": "ACTIVE",
                        "trading_blocked": False,
                        "account_blocked": False,
                        "trade_suspended_by_user": False,
                    },
                )
            if path == "/v2/account/activities":
                if request.url.params.get("activity_types") == "FILL":
                    return self._fill_activities(dict(request.url.params))
                return httpx.Response(200, json=[])
            if path == "/v2/calendar":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "date": request.url.params["start"],
                            "open": "09:30",
                            "close": self.calendar_close,
                        }
                    ],
                )
            if path == "/v2/stocks/quotes/latest":
                return httpx.Response(
                    200,
                    json={
                        "quotes": {
                            s: {"t": self.now.isoformat(), "bp": 99.99, "ap": 100.01}
                            for s in request.url.params["symbols"].split(",")
                        }
                    },
                )
            if path == "/v2/positions":
                return httpx.Response(200, json=list(self.positions.values()))
            if path == "/v2/orders":
                return httpx.Response(
                    200,
                    json=[
                        copy.deepcopy(o)
                        for o in self.orders.values()
                        if o["status"] not in {"filled", "canceled", "expired", "rejected"}
                    ],
                )
            if path == "/v2/orders:by_client_order_id":
                found = next(
                    (
                        o
                        for o in self.orders.values()
                        if o["client_order_id"] == request.url.params["client_order_id"]
                    ),
                    None,
                )
            else:
                found = self.orders.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=copy.deepcopy(found)) if found else httpx.Response(404)
        if method == "DELETE":
            oid = path.rsplit("/", 1)[-1]
            order = self.orders.get(oid)
            if order is None:
                return httpx.Response(404)
            root = next(
                (
                    self.orders[i]
                    for i in self.root_ids
                    if i == oid or any(leg["id"] == oid for leg in self.orders[i]["legs"])
                ),
                None,
            )
            if root and self.fill_on_cancel and root["status"] == "new":
                self.fill_on_cancel = False
                self.fill_entry(root["id"])
            group = [root, *root["legs"]] if root else [order]
            for item in group:
                if item["status"] not in {"filled", "rejected", "canceled"}:
                    item["status"] = "canceled"
                    self._event("canceled", item)
            return httpx.Response(204)
        assert method == "POST" and path == "/v2/orders"
        body = json.loads(request.content)
        existing = next(
            (o for o in self.orders.values() if o["client_order_id"] == body["client_order_id"]),
            None,
        )
        if existing:
            return httpx.Response(422, json={"message": "duplicate client id"})
        if self.timeout_without_accept:
            raise httpx.ReadTimeout("simulated", request=request)
        oid = str(uuid4())
        order = body | {
            "id": oid,
            "status": "new",
            "filled_qty": "0",
            "asset_class": "us_equity",
            "updated_at": self.now.isoformat(),
        }
        self.orders[oid] = order
        if body.get("order_class") == "bracket":
            order["stop_price"] = None
            order["legs"] = []
            self.root_ids.append(oid)
            for kind, prices in [
                ("stop", {"stop_price": body["stop_loss"]["stop_price"], "limit_price": None}),
                ("limit", {"limit_price": body["take_profit"]["limit_price"], "stop_price": None}),
            ]:
                leg = {
                    "id": str(uuid4()),
                    "symbol": body["symbol"],
                    "qty": body["qty"],
                    "filled_qty": "0",
                    "client_order_id": "leg-" + uuid4().hex,
                    "side": "sell",
                    "type": kind,
                    "status": "held",
                    "time_in_force": "day",
                    "asset_class": "us_equity",
                    "updated_at": self.now.isoformat(),
                    **prices,
                }
                self.orders[leg["id"]] = leg
                order["legs"].append(leg)
        else:
            assert body["type"] == "market"
            position = self.positions[body["symbol"]]
            qty = D(body["qty"])
            assert qty <= abs(D(position["qty"]))
            remaining = (
                D(position["qty"]) - qty if body["side"] == "sell" else D(position["qty"]) + qty
            )
            if not remaining:
                del self.positions[body["symbol"]]
            else:
                position["qty"] = str(remaining)
            order["status"] = "filled"
            order["filled_qty"] = body["qty"]
            self._event("fill", order, body["qty"])
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise httpx.ReadTimeout("simulated timeout after acceptance", request=request)
        return httpx.Response(200, json=copy.deepcopy(order))

    def drain(self, dispatcher):
        while self.events:
            dispatcher.ingest(self.events.pop(0), self.now)
