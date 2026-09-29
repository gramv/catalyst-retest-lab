"""Disposable-PostgreSQL managed lifecycle with fake Jev and fake paper HTTP only."""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import psycopg
import pytest

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import SubmissionDisabled, system_event
from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.market import NY
from tests.clock import fixture_now
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_research_reports import reply

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


class ManagedVenue:
    """Broker fixture accepts paper HTTP only; fills require explicit test calls."""

    def __init__(self):
        # Owns the clock for both engines; starts clear of New York midnight so
        # date-bound sessions and calendar flatten times hold at any wall-clock time.
        self.now = fixture_now()
        self.equity = "10000"
        self.cash = "10000"
        self.non_marginable_buying_power = "10000"
        # Margin evidence (plan 2.3); ``buying_power`` None follows cash.
        self.account_id = "fixture-paper-account"
        self.multiplier = "1"
        self.buying_power = None
        self.crypto_status = "ACTIVE"
        self.orders = {}
        self.inventory = {}
        self.entry_prices = {}
        self.calls = []
        self.timeout_next_post = False
        self.reject_protection = False
        self.reject_market_sells = 0  # Refuse this many market-sell closes with a 422.
        self.defer_cancel = False
        self.asset_class_override = None
        # Per-symbol asset metadata overrides, e.g. a coin's quantity increment (package
        # crypto-size-hold); empty keeps every symbol on the defaults below.
        self.asset_overrides = {}
        # CFEE/FEE account activities (package fees-net-r); served whole, unpaginated —
        # fee-import tests inject far fewer rows than one page.
        self.fee_activities = []

    def _position_rows(self):
        result = []
        for symbol, qty in self.inventory.items():
            if qty == 0:
                continue
            reserved = sum(
                (
                    D(o["qty"]) - D(o["filled_qty"])
                    for o in self.orders.values()
                    if o["symbol"] == symbol and o["side"] == "sell" and o["status"] not in TERMINAL
                ),
                D(0),
            )
            # Brackets share the same inventory reservation.
            if "/" not in symbol:
                reserved = min(reserved, qty)
            result.append(
                {
                    "symbol": symbol,
                    "qty": str(qty),
                    "qty_available": str(max(D(0), qty - reserved)),
                    "avg_entry_price": self.entry_prices.get(symbol, "100"),
                    "unrealized_pl": "0",
                    "asset_class": "crypto" if "/" in symbol else "us_equity",
                }
            )
        return result

    def handle(self, request):
        assert request.url.host in {"paper-api.alpaca.markets", "data.alpaca.markets"}
        self.calls.append((request.method, request.url.path, request.content))
        path = request.url.path
        if request.url.host == "data.alpaca.markets":
            assert request.method == "GET"
            symbol = request.url.params["symbols"]
            if path == "/v2/stocks/quotes/latest":
                return httpx.Response(
                    200,
                    json={
                        "quotes": {
                            symbol: {
                                "t": self.now.isoformat(),
                                "bp": 99.99,
                                "ap": 100.01,
                            }
                        }
                    },
                )
            if path == "/v2/stocks/bars":
                start = datetime.fromisoformat(request.url.params["start"]).astimezone(NY).date()
                end = datetime.fromisoformat(request.url.params["end"]).astimezone(NY).date()
                days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
                rows = [
                    {
                        "t": datetime.combine(day, datetime.min.time(), NY).isoformat(),
                        "v": 500000,
                        "vw": 100,
                    }
                    for day in days
                    if day.weekday() < 5
                ]
                return httpx.Response(200, json={"bars": {symbol: rows}, "next_page_token": None})
            pytest.fail("Unexpected fixture market-data GET")
        if request.method == "GET":
            if path == "/v2/account":
                data = {
                    "id": self.account_id,
                    "equity": self.equity,
                    "last_equity": "10000",
                    "cash": self.cash,
                    "multiplier": self.multiplier,
                    "buying_power": self.buying_power or self.cash,
                    "regt_buying_power": self.buying_power or self.cash,
                    "non_marginable_buying_power": self.non_marginable_buying_power,
                    "initial_margin": "0",
                    "maintenance_margin": "0",
                    "shorting_enabled": False,
                    "crypto_status": self.crypto_status,
                    "status": "ACTIVE",
                    "currency": "USD",
                    "trading_blocked": False,
                    "account_blocked": False,
                    "trade_suspended_by_user": False,
                }
            elif path == "/v2/positions":
                data = self._position_rows()
            elif path == "/v2/orders":
                data = [o for o in self.orders.values() if o["status"] not in TERMINAL]
            elif path == "/v2/account/activities":
                data = (
                    self.fee_activities
                    if request.url.params.get("activity_types") == "CFEE,FEE"
                    else []
                )
            elif path == "/v2/calendar":
                start = datetime.fromisoformat(request.url.params["start"]).date()
                end = datetime.fromisoformat(request.url.params["end"]).date()
                days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
                # Explicit fake venue session lets lifecycle tests run on any wall-clock day.
                data = [
                    {"date": day.isoformat(), "open": "00:00", "close": "23:59"}
                    for day in days
                    if day.weekday() < 5 or day == self.now.astimezone(NY).date()
                ]
            elif path.startswith("/v2/assets/"):
                name = path.removeprefix("/v2/assets/")
                data = {
                    "id": str(uuid4()),
                    "symbol": name,
                    "class": self.asset_class_override
                    or ("crypto" if "/" in name else "us_equity"),
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                    "min_order_size": "0.0001",
                    "min_trade_increment": "0.0001",
                    "price_increment": "0.01",
                    **self.asset_overrides.get(name, {}),
                }
            elif path == "/v2/orders:by_client_order_id":
                data = next(
                    (
                        o
                        for o in self.orders.values()
                        if o["client_order_id"] == request.url.params["client_order_id"]
                    ),
                    None,
                )
            elif path.startswith("/v2/orders/"):
                data = self.orders.get(path.rsplit("/", 1)[1])
            else:
                pytest.fail("Unexpected fixture GET")
            return httpx.Response(200 if data is not None else 404, json=data)
        if request.method == "DELETE":
            order = self.orders[path.rsplit("/", 1)[1]]
            order["status"] = "pending_cancel" if self.defer_cancel else "canceled"
            return httpx.Response(204)
        payload = json.loads(request.content)
        if request.method == "PATCH":
            order = self.orders[path.rsplit("/", 1)[1]]
            replacement = {
                **copy.deepcopy(order),
                **payload,
                "id": str(uuid4()),
                "client_order_id": uuid4().hex,
                "status": "new",
                "replaces": order["id"],
            }
            order["status"] = "replaced"
            order["replaced_by"] = replacement["id"]
            self.orders[replacement["id"]] = replacement
            return httpx.Response(200, json=replacement)
        if self.reject_protection and payload["type"] == "stop_limit":
            return httpx.Response(422, json={"message": "fixture protection rejection"})
        if self.reject_market_sells and (payload["type"], payload["side"]) == ("market", "sell"):
            self.reject_market_sells -= 1
            return httpx.Response(422, json={"message": "fixture close rejection"})
        existing = next(
            (o for o in self.orders.values() if o["client_order_id"] == payload["client_order_id"]),
            None,
        )
        if existing:
            return httpx.Response(422, json={"message": "duplicate client ID"})
        order = {
            **payload,
            "id": str(uuid4()),
            "status": "new",
            "filled_qty": "0",
            "updated_at": self.now.isoformat(),
            "legs": [],
            "asset_class": "crypto" if "/" in payload["symbol"] else "us_equity",
        }
        self.orders[order["id"]] = order
        if payload.get("order_class") == "bracket":
            for kind, field, price in (
                ("stop", "stop_price", payload["stop_loss"]["stop_price"]),
                ("limit", "limit_price", payload["take_profit"]["limit_price"]),
            ):
                child = {
                    "id": str(uuid4()),
                    "client_order_id": uuid4().hex,
                    "symbol": payload["symbol"],
                    "qty": payload["qty"],
                    "filled_qty": "0",
                    "side": "sell",
                    "type": kind,
                    "status": "held",
                    "asset_class": "us_equity",
                    "time_in_force": "day",
                    field: price,
                    "updated_at": self.now.isoformat(),
                }
                order["legs"].append(child)
                self.orders[child["id"]] = child
        if self.timeout_next_post:
            self.timeout_next_post = False
            raise httpx.ReadTimeout("Fixture timeout after accept", request=request)
        return httpx.Response(201, json=order)

    def fill(self, broker_id, qty, *, price="100", fee_qty="0"):
        order = self.orders[broker_id]
        qty = D(qty)
        order["filled_qty"] = str(D(order["filled_qty"]) + qty)
        order["status"] = (
            "filled" if D(order["filled_qty"]) == D(order["qty"]) else "partially_filled"
        )
        order["updated_at"] = self.now.isoformat()
        signed_qty = qty - D(fee_qty) if order["side"] == "buy" else -qty
        if order["side"] == "buy":
            previous = self.inventory.get(order["symbol"], D(0))
            old_average = D(self.entry_prices.get(order["symbol"], price))
            self.entry_prices[order["symbol"]] = str(
                (previous * old_average + qty * D(price)) / (previous + qty)
            )
        self.inventory[order["symbol"]] = self.inventory.get(order["symbol"], D(0)) + signed_qty
        if order["status"] == "filled":
            # As Alpaca staff describe it: the take-profit leg becomes ``new`` and the
            # stop-loss leg stays ``held``. A partial fill leaves both legs ``held``.
            for child in order.get("legs", []):
                if child["type"] == "limit":
                    child["status"] = "new"
        return {
            "event": "fill" if order["status"] == "filled" else "partial_fill",
            "execution_id": str(uuid4()),
            "order": copy.deepcopy(order),
            "qty": str(qty),
            "price": price,
            "position_qty": str(self.inventory[order["symbol"]]),
            "timestamp": self.now.isoformat(),
        }

    def orders_of(self, side=None, kind=None):
        return [
            o
            for o in self.orders.values()
            if (side is None or o["side"] == side) and (kind is None or o["type"] == kind)
        ]


@pytest.fixture
def mx(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = ManagedVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(
        risk,
        broker,
        policy=engineering_execution_policy(),
        clock=lambda: venue.now,
        review_store=reviews,
    )
    assert engine.reconcile()["clean"]
    yield engine, venue, reviews
    broker.close()


def packet(mx, symbol="BTC/USD", *, verdict="APPROVE", sector=None, expires_at=None, levels=None):
    engine, venue, reviews = mx
    now = venue.now
    raw = {
        "cycle_id": str(uuid4()),
        "item_key": ("CRYPTO:" if "/" in symbol else "US:") + symbol,
        "revision": 1,
        "symbol": symbol,
        "market": "CRYPTO" if "/" in symbol else "US_STOCKS",
        "levels": levels or {
            "entry_trigger": "100",
            "max_entry_price": "100.10",
            "stop": "95",
            "target": "111",
        },
        "thesis": "Synthetic new product with supported demand; engineering fixture only.",
        "disproof": "Synthetic product withdrawal.",
        "sources": [
            {
                "source_id": "fixture-release",
                "url": "https://example.org/fixture",
                "excerpt": "Synthetic verified release for test only.",
                "content_hash": digest("Synthetic verified release for test only."),
                "retrieved_at": now.isoformat(),
            }
        ],
        "expires_at": (expires_at or now + timedelta(minutes=20)).isoformat(),
        "review_valid_until": (now + timedelta(seconds=60)).isoformat(),
    }
    review_state = {
        key: raw[key] for key in ("market", "symbol", "levels", "thesis", "disproof", "sources")
    }
    raw["state"] = copy.deepcopy(review_state)
    raw["evidence_hash"] = digest(encoded(review_state))
    with engine.store.transaction() as conn:
        engine.store.event(conn, "RESEARCH_PACKET", copy.deepcopy(raw))
    reviewer = JevReviewer(
        reviews,
        ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=reply(verdict=verdict))
        ),
        key_provider=lambda: FIXTURE_KEY,
    )
    result = asyncio.run(
        reviewer.jev_review(
            request_id=uuid4(),
            identity={
                "cycle_id": raw["cycle_id"],
                "candidate_revision": 1,
                "research_item_key": raw["item_key"],
                "evidence_hash": raw["evidence_hash"],
            },
            state=review_state,
            question_set=SKEPTIC,
            expires_at=now + timedelta(seconds=10),
            purpose="ENGINEERING_TEST",
        )
    )
    assert result.status == "RECORDED"
    raw["receipt_id"] = result.receipt_ids[0]
    with engine.store.transaction() as conn:
        event = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": copy.deepcopy(raw)})
        classification = system_event(
            engine.repo,
            conn,
            "CLASSIFICATION_IMPORTED",
            {"ticker": symbol, "source": "LAB_FIXTURE"},
        )
        conn.execute(
            "INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
            (
                classification["seq"],
                symbol,
                sector or symbol,
                sector or symbol,
                "LAB_FIXTURE",
            ),
        )
    raw["selection_event_seq"] = event["event_seq"]
    return raw


def observation(mx, **overrides):
    now = mx[1].now.isoformat()
    return {
        "trade_price": "100",
        "bid": "99.99",
        "ask": "100.01",
        "quote_at": now,
        "trade_at": now,
        "feed_healthy": True,
        "data_provider": "LAB_FIXTURE",
        "data_feed": "FIXTURE",
        **overrides,
    }


def admit_enter(mx, symbol="BTC/USD"):
    sid = mx[0].admit(packet(mx, symbol))
    decision = mx[0].observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "APPROVED"
    return sid, decision


def test_verified_selection_watch_then_risk_authorized_crypto_entry(mx):
    engine, venue, _ = mx
    p = packet(mx)
    sid = engine.admit(p)
    assert not venue.orders
    assert engine.observe_trigger(sid, observation(mx, trade_price="101")) is None
    assert not venue.orders
    decision = engine.observe_trigger(sid, observation(mx))
    entry = venue.orders_of("buy")[0]
    assert decision["outcome"] == "APPROVED"
    assert entry["type"] == "limit" and entry["time_in_force"] == "gtc"
    assert entry["limit_price"] == "100.10"
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_claims").fetchone()["n"] == 1
        reservation = conn.execute("SELECT * FROM lab.managed_active_reservations").fetchone()
    assert reservation["budget"] == D(100) and reservation["planned_risk"] <= D(100)
    assert verify_events(engine.repo.export_events())["valid"]


def test_partial_fill_protection_target_cancel_residual_exit_and_chain(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    update = venue.fill(entry["id"], "1", fee_qty="0.0001")
    assert engine.ingest(update)
    assert engine.ingest(update)  # Duplicate stream delivery is idempotent.
    engine.manage(sid, observation(mx))
    stop = venue.orders_of("sell", "stop_limit")[0]
    assert stop["qty"] == "0.9999"
    assert entry["status"] == "canceled"
    venue.defer_cancel = True
    engine.manage(sid, observation(mx, bid="111", ask="111.01"))
    assert stop["status"] == "pending_cancel"
    assert not venue.orders_of("sell", "market")
    # Stop gets a late partial fill before its cancellation finishes.
    engine.ingest(venue.fill(stop["id"], ".4", price="95"))
    stop["status"] = "canceled"
    venue.defer_cancel = False
    engine.manage(sid, observation(mx, bid="110", ask="110.01"))
    closing = venue.orders_of("sell", "market")[0]
    assert D(closing["qty"]) == D(".5999")
    engine.ingest(venue.fill(closing["id"], closing["qty"], price="110"))
    engine.manage(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "CLOSED"
    assert venue._position_rows() == []
    with engine.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_fills").fetchone()["n"] == 3
        assert not conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()
    assert verify_events(engine.repo.export_events())["valid"]


def test_submit_timeout_reconciles_exactly_one_order(mx):
    engine, venue, _ = mx
    venue.timeout_next_post = True
    sid, decision = admit_enter(mx)
    assert len(venue.orders_of("buy")) == 1
    engine.recover(decision)
    engine.manage(sid, observation(mx))
    assert len(venue.orders_of("buy")) == 1
    assert sum(method == "POST" for method, _, _ in venue.calls) == 1


@pytest.mark.parametrize(
    "symbols", [("BTC/USD", "ETH/USD", "SOL/USD"), ("SPY", "ETH/USD", "SOL/USD")]
)
def test_parallel_risk_checks_share_two_percent_budget(mx, symbols):
    engine, venue, _ = mx
    ids = [engine.admit(packet(mx, symbol)) for symbol in symbols]
    with ThreadPoolExecutor(max_workers=3) as pool:
        decisions = list(pool.map(lambda sid: engine.observe_trigger(sid, observation(mx)), ids))
    assert sum(d["outcome"] == "APPROVED" for d in decisions) == 2
    assert len(venue.orders_of("buy")) == 2
    with engine.repo.connect() as conn:
        budget = conn.execute(
            "SELECT sum(budget) AS n FROM lab.account_risk_reservations"
        ).fetchone()["n"]
    assert budget == D(200)


def test_same_sector_second_candidate_rejected(mx):
    engine, _, _ = mx
    one = engine.admit(packet(mx, "BTC/USD", sector="SHARED"))
    two = engine.admit(packet(mx, "ETH/USD", sector="SHARED"))
    assert engine.observe_trigger(one, observation(mx))["outcome"] == "APPROVED"
    decision = engine.observe_trigger(two, observation(mx))
    assert decision["outcome"] == "REJECTED" and decision["reason"] == "CORRELATION_LIMIT"


def test_real_minus_three_percent_halt_flattens_and_is_durable(mx):
    engine, venue, reviews = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], "1"))
    engine.manage(sid, observation(mx))
    venue.equity = "9700"
    engine.manage(sid, observation(mx))
    engine.manage(sid, observation(mx))
    assert venue.orders_of("sell", "market")
    restarted = ManagedExecution(
        engine.repo,
        engine.broker,
        policy=engineering_execution_policy(),
        clock=lambda: venue.now,
        review_store=reviews,
    )
    restarted.reconcile()
    with pytest.raises(ValueError, match="DAILY_RISK_HALT"):
        restarted.admit(packet(mx, "ETH/USD"))


def test_stock_entry_uses_whole_day_bracket(mx):
    engine, venue, _ = mx
    sid, d = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    assert D(entry["qty"]) == D(entry["qty"]).to_integral_value()
    assert entry["order_class"] == "bracket" and entry["time_in_force"] == "day"
    assert entry["stop_loss"] == {"stop_price": "95"}
    assert entry["take_profit"] == {"limit_price": "111"}
    with pytest.raises(SubmissionDisabled):
        engine.broker.mutate("POST", "/v2/orders", {**d["payload"], "qty": "1.5"}, d["decision_id"])


def test_price_chasing_stop_touch_and_revocation_never_order(mx):
    engine, venue, _ = mx
    for symbol, overrides, reason in (
        ("BTC/USD", {"ask": "100.11", "bid": "100.10"}, "PRICE_BEYOND_MAX_ENTRY"),
        ("ETH/USD", {"trade_price": "94.99"}, "STOP_TRADED_BEFORE_TRIGGER"),
    ):
        sid = engine.admit(packet(mx, symbol))
        assert engine.observe_trigger(sid, observation(mx, **overrides)) is None
        assert engine._load(sid)[1]["reason"] == reason
    sid = engine.admit(packet(mx, "SOL/USD"))
    engine.revoke(sid, "SOURCE_WITHDRAWN")
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert not venue.orders


def test_unknown_inventory_startup_blocks_new_entries(mx):
    engine, venue, _ = mx
    venue.inventory["UNKNOWN/USD"] = D(1)
    assert engine.reconcile()["clean"] is False
    with pytest.raises(ValueError, match="RISK_HALT"):
        engine.admit(packet(mx))


def test_ledger_tables_are_append_only_and_entry_requires_real_receipt(mx):
    engine, venue, _ = mx
    p = packet(mx)
    p["receipt_id"] = str(uuid4())
    with pytest.raises(ValueError, match="RECEIPT_MISSING"):
        engine.admit(p)
    sid, _ = admit_enter(mx, "ETH/USD")
    with pytest.raises(psycopg.Error), engine.repo.connect() as conn:
        conn.execute("UPDATE lab.managed_setups SET symbol='CHANGED' WHERE setup_id=%s", (sid,))
    assert len(venue.orders_of("buy")) == 1


def test_rejected_receipt_cannot_be_promoted_by_selected_label(mx):
    engine, venue, _ = mx
    p = packet(mx, verdict="REJECT")
    with pytest.raises(ValueError):
        engine.admit(p)
    assert not venue.orders


def test_direct_risk_entry_still_requires_fresh_actual_trigger(mx):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx))
    result = engine.authorize_entry(sid, observation(mx, trade_price="102"), None)
    assert result is None or result["outcome"] == "REJECTED"
    assert not venue.orders


def test_crypto_size_respects_nonmarginable_buying_power(mx):
    engine, venue, _ = mx
    venue.non_marginable_buying_power = "25"
    _, decision = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    assert D(entry["qty"]) * D(entry["limit_price"]) <= D(25)
    assert decision["outcome"] == "APPROVED"


def test_rejected_crypto_protection_recovers_by_flattening(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    venue.reject_protection = True
    engine.manage(sid, observation(mx))
    engine.manage(sid, observation(mx))
    assert venue.orders_of("sell", "market")


def test_known_ticker_without_fills_does_not_explain_broker_position(mx):
    engine, venue, _ = mx
    engine.admit(packet(mx))
    venue.inventory["BTC/USD"] = D(1)
    result = engine.reconcile()
    assert result["clean"] is False


def test_native_stock_bracket_children_remain_owned_during_reconciliation(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    assert engine.reconcile()["clean"]


def test_stock_partial_fill_cancel_and_flatten_without_waiting_for_model(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, "SPY")
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], "1"))
    engine.manage(sid, observation(mx))
    assert entry["status"] == "canceled"
    engine.manage(sid, observation(mx))
    close = venue.orders_of("sell", "market")[0]
    assert close["qty"] == "1"


def test_crypto_gap_through_stop_cancels_and_flattens_after_grace(mx):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    stop = venue.orders_of("sell", "stop_limit")[0]
    engine.manage(sid, observation(mx, bid="90", ask="90.01"))
    assert stop["status"] == "new"
    venue.now += timedelta(seconds=3)
    engine.manage(sid, observation(mx, bid="90", ask="90.01"))
    assert stop["status"] == "canceled"
    engine.manage(sid, observation(mx, bid="90", ask="90.01"))
    assert venue.orders_of("sell", "market")


def test_source_revision_supersedes_review_before_trigger(mx):
    engine, venue, _ = mx
    original = packet(mx)
    sid = engine.admit(original)
    with engine.store.transaction() as conn:
        engine.store.event(
            conn,
            "RESEARCH_PACKET",
            {
                **original,
                "revision": 2,
                "evidence_hash": "new-evidence-hash",
            },
        )
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision is None or decision["outcome"] == "REJECTED"
    assert not venue.orders


def test_selected_label_cannot_swap_levels_under_old_receipt(mx):
    engine, _, _ = mx
    altered = packet(mx)
    altered["levels"] = {
        "entry_trigger": "200",
        "max_entry_price": "200.1",
        "stop": "199",
        "target": "203",
    }
    with engine.store.transaction() as conn:
        event = engine.store.event(
            conn,
            "RESEARCH_SELECTED",
            {
                "packet": {
                    key: value for key, value in altered.items() if key != "selection_event_seq"
                }
            },
        )
    altered["selection_event_seq"] = event["event_seq"]
    with pytest.raises(ValueError):
        engine.admit(altered)


def test_unknown_decision_is_refused_without_deadlocking_legacy_fallback(mx, monkeypatch):
    engine, venue, _ = mx
    original = engine.repo.connect

    def bounded_connection():
        conn = original()
        conn.execute("SET statement_timeout = '500ms'")
        return conn

    monkeypatch.setattr(engine.repo, "connect", bounded_connection)
    with pytest.raises(SubmissionDisabled):
        engine.broker.mutate("POST", "/v2/orders", {}, uuid4())
    assert not any(method == "POST" for method, _, _ in venue.calls)
