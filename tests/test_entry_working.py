"""``CRYPTO_ENTRY_WORKING_LIMIT_V1`` (six-day operation, 2026-09-29): an unfilled crypto entry is
cancelled once it has filled nothing for 300 s after its acknowledgement, or once the setup's stop
evidence trades before a fill; the setup then ends with the reason.

The live case: ONDO/USD's limit buy at the max entry sat unfilled for over an hour with Alpaca's
ask below the limit and no ONDO print on Alpaca. The fixture venue, like that paper engine, fills
only when a test says so.

Setups are admitted at entry trigger 100, max entry 100.10, stop 95 and target 111 unless a test
says otherwise. Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper
venue, a mock Jev transport, and the real ``CoinbaseFeed`` fed Coinbase-shaped messages. No
network, broker or owner-ledger contact.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab import entry_working
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.crypto_execution import (
    CryptoAsset,
    CryptoOrder,
    CryptoProtectionPolicy,
    CryptoSnapshot,
    plan_crypto_recovery,
)
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_service import STATE_FIELDS
from catalyst_lab.managed_store import ManagedAuthorizationGate
from tests.maintenance_fixtures import pre_trade_plan_admission as pre_trade_plan_admission
from tests.test_coinbase_reference import Coinbase, reference_observation
from tests.test_crypto_trigger import rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import ManagedVenue, observation, packet

# Every setup here is admitted as before package trade-plan (CRYPTO_MAINTENANCE_V3 or earlier,
# the one-tick stop-limit); the new versions are tests/test_trade_plan_*.py.
pytestmark = pytest.mark.usefixtures("pre_trade_plan_admission")

WINDOW = timedelta(seconds=300)
# ONDO/USD on 2026-09-29: the trigger print and ask, the limit at M, the ask later below it.
ONDO = {"entry_trigger": "0.5155", "max_entry_price": "0.5163734", "stop": "0.5",
        "target": "0.55"}
ONDO_TRIGGER = {"trade_price": "0.5154", "bid": "0.5150", "ask": "0.5154"}


class EntryVenue(ManagedVenue):
    """The fake paper venue, plus a fill that races a cancel: ``fill_before_cancel`` fills that
    quantity of the order as the next DELETE reaches it. Alpaca refuses to cancel an order that
    has filled (422); the rest of a partly filled order is cancelled."""

    def __init__(self):
        super().__init__()
        self.fill_before_cancel = None
        self.race_fills = []

    def handle(self, request):
        if request.method == "DELETE" and self.fill_before_cancel is not None:
            order = self.orders[request.url.path.rsplit("/", 1)[1]]
            qty, self.fill_before_cancel = self.fill_before_cancel, None
            self.race_fills.append(self.fill(order["id"], qty, price=order["limit_price"]))
            if order["status"] == "filled":
                self.calls.append((request.method, request.url.path, request.content))
                return httpx.Response(422, json={"code": 42210000,
                                                 "message": "order is not cancelable"})
        return super().handle(request)


@pytest.fixture
def ex(er):  # noqa: F811
    """The managed engine on ``EntryVenue`` (``tests.test_managed_execution.mx`` otherwise)."""
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = EntryVenue()
    broker = ManagedPaperBroker(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret"),
        ManagedAuthorizationGate(risk, clock=lambda: venue.now),
        transport=httpx.MockTransport(venue.handle),
    )
    engine = ManagedExecution(risk, broker, policy=engineering_execution_policy(),
                              clock=lambda: venue.now, review_store=reviews)
    assert engine.reconcile()["clean"]
    yield engine, venue, reviews
    broker.close()


@pytest.fixture
def cb(ex):
    """The engine's Coinbase reference feed (as ``build_runtime_from_env`` wires it)."""
    engine, venue, _ = ex
    feed = Coinbase(lambda: venue.now)
    engine.reference_feed = feed.feed
    return feed


def entered(ex, symbol="BTC/USD", **packet_options):
    """An older-style crypto setup (today's trigger, ``CRYPTO_STOP_BREACH_V2``) whose limit buy at
    the max entry works unfilled; returns the setup, its entry order and the acknowledgement."""
    engine, venue, _ = ex
    sid = engine.admit(packet(ex, symbol, **packet_options))
    decision = engine.observe_trigger(sid, observation(ex))
    assert decision["outcome"] == "APPROVED"
    [entry] = [o for o in venue.orders_of("buy") if o["symbol"] == symbol]
    return sid, entry, venue.now


def cancel_decisions(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT d.*,EXISTS(SELECT 1 FROM lab.managed_claims c
            WHERE c.decision_id=d.decision_id) AS claimed FROM lab.managed_risk_decisions d
            WHERE d.setup_id=%s AND d.action='CANCEL' ORDER BY d.event_seq""", (sid,)).fetchall()


def reservation(engine, sid):
    """``(active, release reason)`` of the setup's risk reservation."""
    with engine.repo.connect() as conn:
        active = conn.execute("SELECT 1 FROM lab.managed_active_reservations WHERE setup_id=%s",
                              (sid,)).fetchone()
        released = conn.execute("SELECT reason FROM lab.managed_releases WHERE setup_id=%s",
                                (sid,)).fetchone()
    return bool(active), released["reason"] if released else None


def deletes(venue, order):
    return [c for c in venue.calls if c[:2] == ("DELETE", "/v2/orders/" + order["id"])]


def stop_limits(venue, symbol):
    return [o for o in venue.orders_of("sell", "stop_limit")
            if o["symbol"] == symbol and o["status"] in {"new", "accepted"}]


# --- The planner (no database) ------------------------------------------------------------------

NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
ASSET = CryptoAsset("BTC/USD", D("0.0001"), D("0.0001"), D("0.01"))


def planned(*orders, qty="0", **options):
    snapshot = CryptoSnapshot("7", NOW, D(qty), D(qty), orders, True, (), (), D("100"), NOW, None)
    return plan_crypto_recovery(
        ASSET, snapshot, CryptoProtectionPolicy(D(5), D(5), D(5)), now=NOW, operation_key="k",
        stop=D("95"), stop_limit=D("94.99"), target=D("111"), exit_requested=False,
        exit_deadline=NOW + timedelta(hours=20), **options)


def test_the_planner_cancels_an_ended_unfilled_entry_and_protects_any_inventory():
    entry = CryptoOrder("entry-1", "e1", "ENTRY", D("10"), D("0"), "new", True, None,
                        D("100.10"))
    assert (planned(entry).state, planned(entry).reason) == ("ENTRY_WORKING", "AWAIT_FIRST_FILL")
    ended = planned(entry, unfilled_entry_cancel="ENTRY_NOT_FILLED")
    assert (ended.state, ended.reason) == ("CANCELING", "ENTRY_NOT_FILLED")
    [cancel] = ended.proposals
    assert (cancel.action, cancel.method, cancel.path, cancel.payload, cancel.reason) == (
        "CRYPTO_CANCEL", "DELETE", "/v2/orders/entry-1", {}, "ENTRY_NOT_FILLED")
    pending = CryptoOrder("entry-1", "e1", "ENTRY", D("10"), D("0"), "pending_cancel", True, None,
                          D("100.10"))
    waiting = planned(pending, unfilled_entry_cancel="STOP_CROSSED_BEFORE_FILL")
    assert (waiting.state, waiting.reason, waiting.proposals) == (
        "CANCELING", "STOP_CROSSED_BEFORE_FILL", ())
    # A fill that raced the cancel: the inventory is planned exactly as without the version.
    raced = CryptoOrder("entry-1", "e1", "ENTRY", D("10"), D("10"), "filled", True, None,
                        D("100.10"))
    with_version = planned(raced, qty="10", unfilled_entry_cancel="ENTRY_NOT_FILLED")
    assert with_version == planned(raced, qty="10")
    assert (with_version.state, [p.action for p in with_version.proposals]) == (
        "PROTECTION_REQUIRED", ["CRYPTO_PROTECT"])


# --- Admission --------------------------------------------------------------------------------

def test_admission_records_the_version_on_crypto_setups_only(ex):
    engine, _, _ = ex
    crypto, stock = engine.admit(packet(ex, "BTC/USD")), engine.admit(packet(ex, "SPY"))
    [v3] = v3_setups(ex, ["AAA/USD"]).values()
    for sid in (crypto, v3):
        assert state(engine, sid)["entry_working_version"] == "CRYPTO_ENTRY_WORKING_LIMIT_V1"
        assert "entry_acknowledged_at" not in state(engine, sid)
    assert "entry_working_version" not in state(engine, stock)
    assert {"entry_working_version", "entry_acknowledged_at", "entry_stop_marks",
            "entry_working_cancel"} <= STATE_FIELDS
    assert entry_working.ENTRY_FILL_WINDOW_SECONDS == WINDOW.total_seconds()


# --- The fill window ---------------------------------------------------------------------------

def test_the_ondo_entry_is_cancelled_when_its_fill_window_ends_and_the_setup_closes(ex):
    """ONDO/USD, 2026-09-29: the limit buy at M works while Alpaca's ask sits below it and nothing
    prints. It is cancelled 300 s after its acknowledgement, the setup closes ENTRY_NOT_FILLED and
    its risk reservation is released."""
    engine, venue, _ = ex
    venue.asset_overrides["ONDO/USD"] = {"price_increment": "0.0000001"}
    sid = engine.admit(packet(ex, "ONDO/USD", levels=ONDO,
                              expires_at=venue.now + timedelta(hours=23)))
    triggered = venue.now
    assert engine.observe_trigger(sid, observation(ex, **ONDO_TRIGGER))["outcome"] == "APPROVED"
    [entry] = venue.orders_of("buy")
    assert (entry["type"], entry["limit_price"], entry["time_in_force"]) == (
        "limit", "0.5163734", "gtc")
    quiet = {"trade_price": "0.5154", "trade_at": triggered.isoformat(),  # No print since.
             "bid": "0.5122", "ask": "0.5126"}
    for seconds in (1, 60, 120, 240, 299):
        venue.now = triggered + timedelta(seconds=seconds)
        plan = engine.manage(sid, observation(ex, **quiet))
        assert (plan.state, plan.reason) == ("ENTRY_WORKING", "AWAIT_FIRST_FILL")
    assert entry["status"] == "new" and reservation(engine, sid) == (True, None)
    venue.now = triggered + WINDOW
    plan = engine.manage(sid, observation(ex, **quiet))
    assert (plan.state, plan.reason) == ("CANCELING", "ENTRY_NOT_FILLED")
    assert entry["status"] == "canceled"
    [cancel] = cancel_decisions(engine, sid)
    assert (cancel["outcome"], cancel["method"], cancel["path"], cancel["reason"]) == (
        "APPROVED", "DELETE", "/v2/orders/" + entry["id"], "ENTRY_NOT_FILLED")
    assert cancel["claimed"] and len(deletes(venue, entry)) == 1
    current = state(engine, sid)
    assert current["state"] == "ORDER_SUBMITTED" and not current.get("exit_requested")
    closed = engine.manage(sid, observation(ex, **quiet))
    assert (closed["state"], closed["reason"]) == ("CLOSED", "ENTRY_NOT_FILLED")
    assert reservation(engine, sid) == (False, "BROKER_AND_ORDERS_FLAT")
    assert not venue.orders_of("sell") and venue._position_rows() == []
    # The record: the acknowledgement, the order's age and the decision.
    assert current["entry_acknowledged_at"] == triggered.isoformat()
    assert current["entry_working_cancel"] == {
        "reason": "ENTRY_NOT_FILLED", "decided_at": (triggered + WINDOW).isoformat()}
    [event] = rows(engine, "ENTRY_WORKING_CANCEL", sid)
    assert event["idempotency_key"] == f"entry-working-cancel:{sid}:{current['lifecycle_id']}"
    assert event["body"] == {
        "version": "CRYPTO_ENTRY_WORKING_LIMIT_V1",
        "reason": "ENTRY_NOT_FILLED",
        "lifecycle_id": current["lifecycle_id"],
        "entry_order_ids": [entry["id"]],
        "acknowledged_at": triggered.isoformat(),
        "fill_window_seconds": 300,
        "window_ends_at": (triggered + WINDOW).isoformat(),
        "order_age_seconds": "300.0",
        "filled_qty": "0",
        "remaining_qty": entry["qty"],
        "stop": "0.5",
        "stop_evidence": None,
        "decided_at": (triggered + WINDOW).isoformat(),
    }
    assert verify_events(engine.repo.export_events())["valid"]


def test_an_entry_located_after_a_lost_response_starts_its_window_when_located(ex):
    """The POST's response is lost; reconciliation locates the order and acknowledges it then.
    The window survives a restart: a new engine on the same ledger cancels at 300 s."""
    engine, venue, reviews = ex
    venue.timeout_next_post = True
    sid = engine.admit(packet(ex, "ETH/USD"))
    engine.observe_trigger(sid, observation(ex))
    located = venue.now
    [entry] = venue.orders_of("buy")
    current = state(engine, sid)
    assert current["state"] == "ENTRY_PENDING"  # Acknowledged by the lookup, not the POST.
    assert current["entry_acknowledged_at"] == located.isoformat()
    restarted = ManagedExecution(engine.repo, engine.broker,
                                 policy=engineering_execution_policy(),
                                 clock=lambda: venue.now, review_store=reviews)
    assert restarted.reconcile()["clean"]
    venue.now = located + WINDOW - timedelta(seconds=1)
    assert restarted.manage(sid, observation(ex)).state == "ENTRY_WORKING"
    venue.now = located + WINDOW
    assert restarted.manage(sid, observation(ex)).reason == "ENTRY_NOT_FILLED"
    assert entry["status"] == "canceled"
    closed = restarted.manage(sid, observation(ex))
    assert (closed["state"], closed["reason"]) == ("CLOSED", "ENTRY_NOT_FILLED")
    assert sum(method == "POST" for method, _, _ in venue.calls) == 1


# --- The stop crossed before the fill ---------------------------------------------------------

def test_an_alpaca_print_at_the_stop_cancels_a_v2_setups_unfilled_entry(ex):
    engine, venue, _ = ex
    sid, entry, acknowledged = entered(ex)
    assert state(engine, sid)["stop_breach_version"] == "CRYPTO_STOP_BREACH_V2"
    # A print at the stop from before the acknowledgement, and a stale one, are no evidence.
    venue.now = acknowledged + timedelta(seconds=2)
    early = (acknowledged - timedelta(seconds=1)).isoformat()
    assert engine.manage(sid, observation(ex, trade_price="95", trade_at=early)).state == (
        "ENTRY_WORKING")
    venue.now = acknowledged + timedelta(seconds=20)
    stale = (venue.now - timedelta(seconds=6)).isoformat()
    assert engine.manage(sid, observation(ex, trade_price="94.50", trade_at=stale)).state == (
        "ENTRY_WORKING")
    traded = venue.now - timedelta(seconds=2)  # Delivered two seconds after the trade.
    plan = engine.manage(sid, observation(ex, trade_price="95", trade_at=traded.isoformat(),
                                          trade_id="t-95", bid="95.40", ask="95.60"))
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_CROSSED_BEFORE_FILL")
    assert entry["status"] == "canceled"
    [cancel] = cancel_decisions(engine, sid)
    assert cancel["reason"] == "STOP_CROSSED_BEFORE_FILL" and cancel["claimed"]
    [event] = rows(engine, "ENTRY_WORKING_CANCEL", sid)
    assert event["body"]["order_age_seconds"] == "20.0"
    assert event["body"]["stop_evidence"] == {
        "stop_rule": "CRYPTO_STOP_BREACH_V2",
        "stop": "95",
        "stop_since": acknowledged.isoformat(),
        "breach_evidence": "TRADE_PRINT",
        "evidence_at": traded.isoformat(),
        "evidence_price": "95",
        "trade_id": "t-95",
        "print_age_seconds": "2.0",
    }
    closed = engine.manage(sid, observation(ex))
    assert (closed["state"], closed["reason"]) == ("CLOSED", "STOP_CROSSED_BEFORE_FILL")
    assert reservation(engine, sid) == (False, "BROKER_AND_ORDERS_FLAT")


def test_alpacas_bid_held_at_the_stop_for_15_s_cancels_and_a_touch_that_recovers_does_not(ex):
    engine, venue, _ = ex
    sid, entry, acknowledged = entered(ex)

    def tick(seconds, bid):
        venue.now = acknowledged + timedelta(seconds=seconds)
        return engine.manage(sid, observation(ex, bid=bid, ask=str(D(bid) + D("0.10"))))

    assert tick(1, "94.90").state == "ENTRY_WORKING"
    assert state(engine, sid)["entry_stop_marks"]["bid_since"] == (
        acknowledged + timedelta(seconds=1)).isoformat()
    assert tick(2, "99.99").state == "ENTRY_WORKING"  # Recovered: the mark is cleared.
    assert state(engine, sid)["entry_stop_marks"]["bid_since"] is None
    for second in range(5, 20):
        assert tick(second, "94.90").state == "ENTRY_WORKING", second
    plan = tick(20, "94.85")  # Held 15 s.
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_CROSSED_BEFORE_FILL")
    evidence = rows(engine, "ENTRY_WORKING_CANCEL", sid)[0]["body"]["stop_evidence"]
    assert {k: evidence[k] for k in ("stop_rule", "breach_evidence", "held_since",
                                     "held_seconds", "first_bid", "evidence_price")} == {
        "stop_rule": "CRYPTO_STOP_BREACH_V2", "breach_evidence": "BID_HELD",
        "held_since": (acknowledged + timedelta(seconds=5)).isoformat(),
        "held_seconds": "15.0", "first_bid": "94.90", "evidence_price": "94.85"}
    assert entry["status"] == "canceled"
    # The marks are the entry's own; the position's stop_breach_* fields were never written.
    assert all(key not in state(engine, sid)
               for key in ("stop_breach_marks", "stop_breached_at", "stop_breach_evidence"))


def coinbase_entered(ex, cb, symbol="BTC/USD"):
    """A report-V3 pick of a Coinbase coin (CRYPTO_COINBASE_TRIGGER_V1, CRYPTO_STOP_BREACH_V3)
    entered on a Coinbase touch; its limit buy works unfilled."""
    engine, venue, _ = ex
    product = symbol.replace("/", "-")
    cb.connect(product)
    [sid] = v3_setups(ex, [symbol]).values()
    cb.trade(product, "99.98")
    decision = engine.observe_trigger(
        sid, reference_observation(ex, cb, sid, quote=("99.99", "100.01")))
    assert decision["outcome"] == "APPROVED"
    [entry] = [o for o in venue.orders_of("buy") if o["symbol"] == symbol]
    assert state(engine, sid)["stop_breach_version"] == "CRYPTO_STOP_BREACH_V3"
    return sid, entry, venue.now


def test_a_coinbase_print_at_the_stop_cancels_a_coinbase_setups_entry_and_alpaca_alone_does_not(
        ex, cb):
    engine, venue, _ = ex
    sid, entry, acknowledged = coinbase_entered(ex, cb)
    # Alpaca's book dips to the stop and prints there for 20 s while Coinbase trades above it.
    for _ in range(20):
        venue.now += timedelta(seconds=1)
        cb.beat()
        cb.trade("BTC-USD", "99.40")
        plan = engine.manage(sid, observation(ex, trade_price="94.90", bid="94.80", ask="95.10"))
        assert plan.state == "ENTRY_WORKING"
    assert entry["status"] == "new" and not rows(engine, "ENTRY_WORKING_CANCEL", sid)
    venue.now += timedelta(seconds=1)
    cb.beat()
    traded = venue.now - timedelta(seconds=1)
    trade_id = cb.trade("BTC-USD", "95", at=traded)
    plan = engine.manage(sid, observation(ex, trade_price="99.30", bid="99.20", ask="99.25"))
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_CROSSED_BEFORE_FILL")
    assert entry["status"] == "canceled"
    [event] = rows(engine, "ENTRY_WORKING_CANCEL", sid)
    evidence = event["body"]["stop_evidence"]
    assert {k: evidence[k] for k in ("stop_rule", "breach_evidence", "fallback", "evidence_at",
                                     "evidence_price", "trade_id", "stop_since")} == {
        "stop_rule": "CRYPTO_STOP_BREACH_V3", "breach_evidence": "COINBASE_PRINT",
        "fallback": False, "evidence_at": traded.isoformat(), "evidence_price": "95",
        "trade_id": trade_id, "stop_since": acknowledged.isoformat()}
    assert (evidence["reference"]["product_id"], evidence["reference"]["healthy"]) == (
        "BTC-USD", True)
    assert event["body"]["order_age_seconds"] == "21.0"
    closed = engine.manage(sid, observation(ex))
    assert (closed["state"], closed["reason"]) == ("CLOSED", "STOP_CROSSED_BEFORE_FILL")
    assert reservation(engine, sid) == (False, "BROKER_AND_ORDERS_FLAT")


def test_an_unhealthy_coinbase_feed_falls_back_to_alpacas_print(ex, cb):
    engine, venue, _ = ex
    sid, entry, _ = coinbase_entered(ex, cb)
    venue.now += timedelta(seconds=4)  # No heartbeat for 4 s: HEARTBEAT_STALE.
    plan = engine.manage(sid, observation(ex, trade_price="94.95", bid="95.10", ask="95.20"))
    assert (plan.state, plan.reason) == ("CANCELING", "STOP_CROSSED_BEFORE_FILL")
    evidence = rows(engine, "ENTRY_WORKING_CANCEL", sid)[0]["body"]["stop_evidence"]
    assert (evidence["stop_rule"], evidence["breach_evidence"], evidence["fallback"],
            evidence["evidence_price"]) == ("CRYPTO_STOP_BREACH_V3", "TRADE_PRINT", True, "94.95")
    assert (evidence["reference"]["healthy"], evidence["reference"]["code"]) == (
        False, "HEARTBEAT_STALE")
    assert entry["status"] == "canceled"


# --- Partial fills -----------------------------------------------------------------------------

def test_a_partial_fill_is_protected_and_the_rest_is_cancelled_at_the_window(ex):
    """CRYPTO_PARTIAL_ENTRY_V1 keeps the rest of a partly filled entry working; this version ends
    it with the fill window. The filled part stays protected by its native stop-limit."""
    engine, venue, _ = ex
    [sid] = v3_setups(ex, ["SOL/USD"]).values()
    assert state(engine, sid)["partial_entry_policy"]["policy_id"] == "CRYPTO_PARTIAL_ENTRY_V1"
    decision = engine.observe_trigger(sid, observation(ex, trade_price="100", bid="100.09",
                                                       ask="100.10"))
    assert decision["outcome"] == "APPROVED"
    acknowledged = venue.now
    [entry] = venue.orders_of("buy")
    venue.now = acknowledged + timedelta(seconds=60)
    assert engine.ingest(venue.fill(entry["id"], "5", price="100.10"))
    engine.manage(sid, observation(ex))
    [stop] = stop_limits(venue, "SOL/USD")
    assert (D(stop["qty"]), stop["stop_price"]) == (5, "95")  # Protected at once.
    assert entry["status"] == "partially_filled"
    [retained] = rows(engine, "PARTIAL_ENTRY_RETAINED", sid)
    assert retained["body"]["deadline"] == (venue.now + timedelta(minutes=10)).isoformat()
    assert retained["body"]["fill_window_ends_at"] == (acknowledged + WINDOW).isoformat()
    venue.now = acknowledged + WINDOW - timedelta(seconds=1)
    engine.manage(sid, observation(ex))
    assert entry["status"] == "partially_filled" and not cancel_decisions(engine, sid)
    venue.now = acknowledged + WINDOW
    engine.manage(sid, observation(ex))
    assert entry["status"] == "canceled"
    [cancel] = cancel_decisions(engine, sid)
    assert (cancel["reason"], cancel["claimed"], cancel["path"]) == (
        "ENTRY_REMAINDER_NOT_FILLED", True, "/v2/orders/" + entry["id"])
    [remainder] = rows(engine, "PARTIAL_ENTRY_REMAINDER_CANCEL", sid)
    assert remainder["body"]["reason"] == "ENTRY_REMAINDER_NOT_FILLED"
    [event] = rows(engine, "ENTRY_WORKING_CANCEL", sid)
    body = event["body"]
    assert (body["reason"], body["order_age_seconds"], D(body["filled_qty"]),
            D(body["remaining_qty"])) == (
        "ENTRY_REMAINDER_NOT_FILLED", "300.0", D(5), D(entry["qty"]) - 5)
    current = state(engine, sid)
    assert (current["state"], current["partial_entry_cancel"], current.get("exit_requested")) == (
        "OPEN", "ENTRY_REMAINDER_NOT_FILLED", None)
    plan = engine.manage(sid, observation(ex))
    assert (plan.state, plan.reason) == ("PROTECTED", "NATIVE_STOP_LIMIT_PRESENT")
    assert stop_limits(venue, "SOL/USD") == [stop] and not venue.orders_of("sell", "market")


def test_a_partial_fill_seen_with_a_print_and_no_quote_keeps_working(ex):
    """After a market gap the runtime's observation can carry a print and no quote (the coin's
    first message was a trade). The partly filled entry is then judged as without a quote: the
    filled part is protected and the rest keeps working inside its window."""
    engine, venue, _ = ex
    [sid] = v3_setups(ex, ["SOL/USD"]).values()
    decision = engine.observe_trigger(sid, observation(ex, trade_price="100", bid="100.09",
                                                       ask="100.10"))
    assert decision["outcome"] == "APPROVED"
    [entry] = venue.orders_of("buy")
    venue.now += timedelta(seconds=60)
    assert engine.ingest(venue.fill(entry["id"], "5", price="100.10"))
    now = venue.now.isoformat()
    print_only = {"trade_price": "100", "trade_at": now, "trade_id": "7", "feed_healthy": True,
                  "data_provider": "ALPACA", "data_feed": "CRYPTO_US", "retrieved_at": now}
    plan = engine.manage(sid, print_only)
    [stop] = stop_limits(venue, "SOL/USD")
    assert (D(stop["qty"]), stop["stop_price"]) == (5, "95")
    assert entry["status"] == "partially_filled" and not cancel_decisions(engine, sid)
    [retained] = rows(engine, "PARTIAL_ENTRY_RETAINED", sid)
    assert (retained["body"]["bid"], retained["body"]["ask"], retained["body"]["quote_at"]) == (
        None, None, None)
    plan = engine.manage(sid, print_only)
    assert (plan.state, plan.reason) == ("PROTECTED", "NATIVE_STOP_LIMIT_PRESENT")
    assert state(engine, sid)["state"] == "OPEN"


def test_without_the_partial_entry_rule_the_rest_is_cancelled_at_once_as_before(ex):
    engine, venue, _ = ex
    sid, entry, _ = entered(ex)
    assert "partial_entry_policy" not in state(engine, sid)
    assert engine.ingest(venue.fill(entry["id"], "0.5"))
    engine.manage(sid, observation(ex))
    assert entry["status"] == "canceled"
    [cancel] = cancel_decisions(engine, sid)
    assert cancel["reason"] == "CANCEL_REMAINING_ENTRY"
    [stop] = stop_limits(venue, "BTC/USD")
    assert D(stop["qty"]) == D("0.5") and not rows(engine, "ENTRY_WORKING_CANCEL", sid)


# --- A fill that races the cancel --------------------------------------------------------------

def test_a_whole_fill_that_races_the_window_cancel_is_kept_and_protected(ex):
    engine, venue, _ = ex
    sid, entry, acknowledged = entered(ex)
    venue.now = acknowledged + WINDOW
    venue.fill_before_cancel = entry["qty"]  # The venue fills as the DELETE arrives.
    plan = engine.manage(sid, observation(ex))
    assert (plan.state, plan.reason) == ("CANCELING", "ENTRY_NOT_FILLED")
    [cancel] = cancel_decisions(engine, sid)
    assert cancel["claimed"] and entry["status"] == "filled"
    [refused] = rows(engine, "BROKER_REJECTED", sid)
    assert refused["body"]["decision_id"] == str(cancel["decision_id"])
    assert engine.ingest(venue.race_fills[0])
    venue.now += timedelta(seconds=1)
    plan = engine.manage(sid, observation(ex))
    assert plan.state == "PROTECTION_REQUIRED"
    [stop] = stop_limits(venue, "BTC/USD")
    assert (D(stop["qty"]), stop["stop_price"], stop["limit_price"]) == (
        D(entry["qty"]), "95", "94.99")
    current = state(engine, sid)
    assert current["state"] == "OPEN" and current.get("exit_requested") is None
    assert current["entry_working_cancel"]["reason"] == "ENTRY_NOT_FILLED"  # The record stands.
    assert not venue.orders_of("sell", "market")
    plan = engine.manage(sid, observation(ex))
    assert (plan.state, plan.reason) == ("PROTECTED", "NATIVE_STOP_LIMIT_PRESENT")
    assert reservation(engine, sid) == (True, None)  # A trade: its reservation stays.


def test_a_partial_fill_that_races_the_stop_cancel_is_protected_and_the_rest_cancelled(ex):
    engine, venue, _ = ex
    sid, entry, acknowledged = entered(ex)
    venue.now = acknowledged + timedelta(seconds=3)
    venue.fill_before_cancel = "0.4"
    plan = engine.manage(sid, observation(ex, trade_price="94.90",
                                          trade_at=(venue.now - timedelta(seconds=1)).isoformat()))
    assert plan.reason == "STOP_CROSSED_BEFORE_FILL"
    assert entry["status"] == "canceled" and entry["filled_qty"] == "0.4"
    assert engine.ingest(venue.race_fills[0])
    venue.now += timedelta(seconds=1)
    plan = engine.manage(sid, observation(ex, trade_price="95.20", bid="95.10", ask="95.30"))
    assert plan.state == "PROTECTION_REQUIRED"
    [stop] = stop_limits(venue, "BTC/USD")
    assert D(stop["qty"]) == D("0.4") and not venue.orders_of("sell", "market")
    assert state(engine, sid)["state"] == "OPEN"


def test_a_partial_fill_while_the_cancel_is_pending_is_protected_under_the_partial_entry_rule(ex):
    engine, venue, _ = ex
    [sid] = v3_setups(ex, ["SOL/USD"]).values()
    engine.observe_trigger(sid, observation(ex, trade_price="100", bid="100.09", ask="100.10"))
    acknowledged = venue.now
    [entry] = venue.orders_of("buy")
    venue.defer_cancel = True
    venue.now = acknowledged + WINDOW
    assert engine.manage(sid, observation(ex)).reason == "ENTRY_NOT_FILLED"
    assert entry["status"] == "pending_cancel"
    update = venue.fill(entry["id"], "5", price="100.10")  # Fills before the cancel completes.
    entry["status"] = "pending_cancel"
    assert engine.ingest(update)
    venue.now += timedelta(seconds=1)
    plan = engine.manage(sid, observation(ex))
    # The stop-limit waits while the rest's cancel is in flight: Alpaca refuses a sell while a buy
    # of the coin works (the 2026-09-30 sequencing fix). The app watches the stop meanwhile.
    assert (plan.state, plan.reason, plan.proposals) == (
        "CANCELING", "CANCEL_ENTRY_BEFORE_PROTECT", ())
    assert not stop_limits(venue, "SOL/USD")
    [remainder] = rows(engine, "PARTIAL_ENTRY_REMAINDER_CANCEL", sid)
    assert remainder["body"]["reason"] == "ENTRY_NOT_FILLED"  # The decision's reason.
    assert len(rows(engine, "ENTRY_WORKING_CANCEL", sid)) == 1
    entry["status"] = "canceled"
    plan = engine.manage(sid, observation(ex))
    assert plan.state == "PROTECTION_REQUIRED"
    [stop] = stop_limits(venue, "SOL/USD")
    assert D(stop["qty"]) == 5  # Protected once the rest's cancel completed.
    plan = engine.manage(sid, observation(ex))
    assert (plan.state, plan.reason) == ("PROTECTED", "NATIVE_STOP_LIMIT_PRESENT")
    assert not venue.orders_of("sell", "market") and state(engine, sid)["state"] == "OPEN"
    assert state(engine, sid).get("exit_requested") is None


# --- Authorization -------------------------------------------------------------------------------

def test_every_cancel_needs_its_own_authorization_and_a_refused_one_sends_nothing(ex, monkeypatch):
    engine, venue, _ = ex
    sid, entry, acknowledged = entered(ex)
    claim = ManagedAuthorizationGate.claim
    refusals = []

    def refuse_first_delete(gate, request):
        if request.method == "DELETE" and not refusals:
            refusals.append(request.url.path)
            raise SubmissionDisabled("VALID_UNUSED_RISK_DECISION_REQUIRED")
        return claim(gate, request)

    monkeypatch.setattr(ManagedAuthorizationGate, "claim", refuse_first_delete)
    venue.now = acknowledged + WINDOW
    assert engine.manage(sid, observation(ex)).reason == "ENTRY_NOT_FILLED"
    assert refusals == ["/v2/orders/" + entry["id"]]
    assert entry["status"] == "new" and not deletes(venue, entry)  # Nothing reached the venue.
    [refused] = cancel_decisions(engine, sid)
    assert refused["outcome"] == "APPROVED" and not refused["claimed"]
    [not_claimed] = rows(engine, "AUTHORIZATION_NOT_CLAIMED", sid)
    assert not_claimed["body"]["decision_id"] == str(refused["decision_id"])
    assert state(engine, sid)["state"] == "ORDER_SUBMITTED"
    venue.now += timedelta(seconds=1)
    assert engine.manage(sid, observation(ex)).reason == "ENTRY_NOT_FILLED"
    first, second = cancel_decisions(engine, sid)
    assert first["decision_id"] == refused["decision_id"] and second["claimed"]
    assert entry["status"] == "canceled" and len(deletes(venue, entry)) == 1
    closed = engine.manage(sid, observation(ex))
    assert (closed["state"], closed["reason"]) == ("CLOSED", "ENTRY_NOT_FILLED")
    # Every DELETE the venue saw carried a claimed, exact, one-use decision.
    with engine.repo.connect() as conn:
        claimed = conn.execute(
            """SELECT count(*) AS n FROM lab.managed_risk_decisions d JOIN lab.managed_claims c
            USING(decision_id) WHERE d.method='DELETE'""").fetchone()["n"]
    assert claimed == sum(call[0] == "DELETE" for call in venue.calls) == 1


# --- Setups admitted before the version ----------------------------------------------------------

def test_setups_admitted_before_the_version_keep_their_entry_working_as_today(ex, monkeypatch):
    engine, venue, _ = ex
    monkeypatch.setattr(entry_working, "admission_fields", lambda packet: {})
    sid, entry, acknowledged = entered(ex)
    current = state(engine, sid)
    assert current["state"] == "ORDER_SUBMITTED"
    assert not {"entry_working_version", "entry_acknowledged_at"} & set(current)
    # Past the window, a print at the stop and a bid held under it: the entry keeps working.
    for seconds in (300, 600):
        venue.now = acknowledged + timedelta(seconds=seconds)
        assert engine.manage(sid, observation(ex)).state == "ENTRY_WORKING"
    for second in range(20):
        venue.now = acknowledged + timedelta(seconds=601 + second)
        plan = engine.manage(sid, observation(ex, trade_price="94", bid="93.90", ask="94.10"))
        assert (plan.state, plan.reason) == ("ENTRY_WORKING", "AWAIT_FIRST_FILL")
    assert entry["status"] == "new" and not deletes(venue, entry)
    assert not rows(engine, "ENTRY_WORKING_CANCEL", sid)
    # Until the entry window closes, exactly as before.
    venue.now = acknowledged + timedelta(minutes=20)
    plan = engine.manage(sid, observation(ex))
    assert (plan.state, plan.reason) == ("CANCELING", "FLAT_WITH_WORKING_ORDERS")
    closed = engine.manage(sid, observation(ex))
    assert (closed["state"], closed["reason"]) == ("CLOSED", "ENTRY_EXPIRED")
