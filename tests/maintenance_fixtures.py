"""Fixture venue, bar source and mock Jev for the maintenance tests (package maintenance).

Disposable per-test PostgreSQL databases, the fake paper venue of tests/test_managed_execution
(extended here with a PATCH refusal and a protection refusal while a buy works), a scripted
bar source and a mock TypeSafe transport. No broker, provider, network or owner-ledger contact.
"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_budget import SpendConfig, SpendGuard
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.scan_sources import SourceIssue
from catalyst_lab.setup_scan import CompletedBar
from catalyst_lab.system_check import LivePriceReader
from catalyst_lab.trade_maintenance import TradeMaintenance
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import ManagedVenue, observation

DEFAULT = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
# The deploy example's monthly Jev budget (package jev-budget); a test ledger spends fractions of
# a cent, so the guard stays NORMAL (V3's per-minute cadence) unless a test says otherwise.
FIXTURE_SPEND = SpendConfig(D("50"))
V4_QUESTIONS = {"trade_reason", "action", "stop_option", "target_option"}


class MaintenanceVenue(ManagedVenue):
    """The fake paper venue plus two refusals: a price replace (PATCH) of a crypto order, and a
    sell stop-limit while a buy of the same coin is open, as Alpaca paper's wash-trade guard did
    live (CRV and PEPE, 2026-09-30). A buy whose cancel is pending counts as open here: Alpaca
    keeps the order open until the cancel completes. That refusal is the fixture's assumption,
    not observed live."""

    def __init__(self):
        super().__init__()
        self.reject_patch = False
        self.refuse_stop_while_buy_open = False
        # A replaced order still listed beside its replacement, with this status (UNI, 2026-09-28
        # 15:31 UTC: the app's view still showed it open); None keeps the replace instant.
        self.slow_replace = None

    def handle(self, request):
        if request.method == "PATCH" and self.slow_replace and not self.reject_patch:
            response = super().handle(request)
            if response.status_code == 200:
                self.orders[request.url.path.rsplit("/", 1)[1]]["status"] = self.slow_replace
            return response
        if request.method == "PATCH" and self.reject_patch:
            self.calls.append((request.method, request.url.path, request.content))
            return httpx.Response(422, json={"message": "fixture replace refusal"})
        if request.method == "POST" and self.refuse_stop_while_buy_open:
            payload = json.loads(request.content)
            if payload.get("type") == "stop_limit" and any(
                o["symbol"] == payload["symbol"] and o["side"] == "buy"
                and o["status"] in {"new", "partially_filled", "pending_cancel"}
                for o in self.orders.values()
            ):
                self.calls.append((request.method, request.url.path, request.content))
                return httpx.Response(422, json={"message": "fixture potential wash trade"})
        return super().handle(request)


@pytest.fixture
def v1_admission(monkeypatch):
    """Admission as before package answer-rules: the maintained arm records
    ``CRYPTO_MAINTENANCE_V1`` and ``CRYPTO_24H_REVIEW_V1``, so the setup keeps V1's readers
    (every setup admitted before V2 recorded exactly these)."""
    from catalyst_lab import crypto_holding

    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE)
    monkeypatch.setattr(crypto_holding, "ADMITTED_REVIEW", crypto_holding.CRYPTO_24H_REVIEW)


@pytest.fixture
def v2_admission(monkeypatch):
    """Admission as in package answer-rules: the maintained arm records ``CRYPTO_MAINTENANCE_V2``
    (every setup admitted before package jev-budget recorded exactly this), which keeps V2's
    per-minute cadence whatever the budget."""
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V2)


def spend_guard(mt, config=FIXTURE_SPEND, **options):
    """The real ``JEV_SPEND_GUARD_V1`` over the test ledger, on the venue's clock."""
    engine, venue, _ = mt
    return SpendGuard(config, engine.store, clock=lambda: venue.now, **options)


@pytest.fixture
def mt(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    venue = MaintenanceVenue()
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


# --- Admission, entry and fills ------------------------------------------------------------------

def publish(mt, symbols, *, levels=DEFAULT, agent="100.50"):
    """Report-V3 picks through intake, the mock selection Jev and publication (V2 rule)."""
    from tests.test_system_check import publish_v3, two_slots, v3_pick

    venue = mt[1]
    slot, _ = two_slots(venue.now)
    return publish_v3(mt, [v3_pick(i, symbol, venue.now, agent=agent, levels=levels)
                           for i, symbol in enumerate(symbols)], run_slot=slot)


def admit(mt, symbol="SOL/USD", *, levels=DEFAULT):
    """One maintained setup: a report-V3 crypto pick admitted on a live pullback quote."""
    from tests.test_system_check import at_price

    engine, venue, _ = mt
    selected = publish(mt, [symbol], levels=levels)
    return engine.admit(selected[symbol], live_quote=at_price("100.49", "100.51", at=venue.now))


def admit_many(mt, symbols, *, levels=DEFAULT):
    from tests.test_system_check import at_price

    engine, venue, _ = mt
    selected = publish(mt, symbols, levels=levels)
    return [engine.admit(selected[s], live_quote=at_price("100.49", "100.51", at=venue.now))
            for s in symbols]


def quote(mt, bid, ask=None, **extra):
    bid = D(str(bid))
    ask = D(str(ask)) if ask is not None else bid + D("0.03")
    return observation(mt, trade_price=str(bid), bid=str(bid), ask=str(ask), **extra)


def trigger(mt, sid):
    """A print at the entry trigger with the ask at the max entry: the entry is authorized."""
    engine = mt[0]
    decision = engine.observe_trigger(sid, observation(mt, trade_price="100", bid="100.09",
                                                       ask="100.10"))
    assert decision and decision["outcome"] == "APPROVED", decision
    return decision


def entry_order(mt, symbol):
    return next(o for o in mt[1].orders.values() if o["symbol"] == symbol and o["side"] == "buy")


def fill(mt, order, qty, price="100.10"):
    update = mt[1].fill(order["id"], str(qty), price=price)
    assert mt[0].ingest(update)
    return update


def fresh_bar(mt):
    """Move the venue clock 30 seconds into the next 15-minute bar, so a test's next minutes
    stay inside one bar (the fixture clock otherwise follows real time)."""
    venue = mt[1]
    venue.now = cm.floor_time(venue.now, 900) + timedelta(seconds=930)
    mt[0].reconciled_at = None
    assert mt[0].reconcile()["clean"]


def open_trade(mt, symbol="SOL/USD", *, levels=DEFAULT, bid="100.20"):
    """Admitted, triggered, fully filled at the max entry and protected, 30 s into a bar."""
    engine, venue, _ = mt
    fresh_bar(mt)
    sid = admit(mt, symbol, levels=levels)
    trigger(mt, sid)
    order = entry_order(mt, symbol)
    fill(mt, order, order["qty"])
    engine.manage(sid, quote(mt, bid))
    engine.manage(sid, quote(mt, bid))
    state = engine._load(sid)[1]
    assert state["state"] == "OPEN"
    return sid


def stop_orders(mt, symbol, *, live=True):
    return [o for o in mt[1].orders.values() if o["symbol"] == symbol and o["type"] == "stop_limit"
            and (not live or o["status"] in {"new", "accepted", "partially_filled"})]


def events(engine, kind, setup_id=None):
    with engine.repo.connect() as conn:
        if setup_id is None:
            rows = conn.execute("SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                                (kind,)).fetchall()
        else:
            rows = conn.execute("""SELECT * FROM lab.managed_events WHERE kind=%s AND setup_id=%s
                ORDER BY event_seq""", (kind, setup_id)).fetchall()
    return rows


def bodies(engine, kind, setup_id=None):
    return [r["body"] for r in events(engine, kind, setup_id)]


def decisions(engine, setup_id, action=None):
    with engine.repo.connect() as conn:
        rows = conn.execute("""SELECT d.*,EXISTS(SELECT 1 FROM lab.managed_claims c
            WHERE c.decision_id=d.decision_id) AS claimed FROM lab.managed_risk_decisions d
            WHERE d.setup_id=%s ORDER BY d.event_seq""", (setup_id,)).fetchall()
    return [r for r in rows if action is None or r["action"] == action]


# --- Bars, quotes and the mock Jev ---------------------------------------------------------------

def bars_series(now, count, *, seconds, base, spread=D("0.4"), lows=None, highs=None,
                symbol="SOL/USD"):
    last_end = cm.floor_time(now, seconds)
    result = []
    for i in range(count):
        back = count - 1 - i
        end = last_end - timedelta(seconds=seconds * back)
        low = D(str((lows or {}).get(back, base - spread)))
        high = D(str((highs or {}).get(back, base + spread)))
        high = max(high, low)
        mid = (low + high) / 2
        result.append(CompletedBar(end - timedelta(seconds=seconds), end, mid, high, low, mid,
                                   D("25"), "LAB_FIXTURE", "FIXTURE",
                                   f"LAB_FIXTURE:{symbol}:{seconds}:{end.isoformat()}", True))
    return result


class Bars:
    """Scripted completed bars by symbol and timeframe, built at the venue's time; no REST
    quote (``_latest`` reports it missing), so reviews use the fresh stream quote."""

    def __init__(self, venue):
        self.venue = venue
        self.calls = []
        self.quote_reads = []
        self.fail = False
        self.spec = {
            ("SOL/USD", "15Min"): {"base": D("105.5"), "spread": D("0.2"),
                                   "lows": {3: "103.004", 9: "102.51"}, "highs": {5: "112.345"}},
            ("SOL/USD", "1Hour"): {"base": D("106"), "spread": D("0.9"),
                                   "lows": {6: "101.33"}, "highs": {30: "113.2", 60: "114.3"}},
        }

    def timeframe_bars(self, market, symbol, *, timeframe, count):
        self.calls.append((market, symbol, timeframe, count))
        if self.fail:
            return (), (SourceIssue(market, symbol, "SCAN_HTTP_503", "/v1beta3/crypto/us/bars"),)
        seconds = cm.TIMEFRAMES[timeframe]
        spec = self.spec.get((symbol, timeframe))
        if spec is None:
            base = D("61000") if symbol == "BTC/USD" else D("104")
            spec = {"base": base, "spread": base * D("0.002")}
        return tuple(bars_series(self.venue.now, count, seconds=seconds, symbol=symbol,
                                 **spec)), ()

    def _latest(self, market, symbols, kind):
        self.quote_reads.append((market, tuple(symbols), kind))
        return {}, [SourceIssue(market, s, "REST_QUOTE_MISSING_FIXTURE", "/quotes")
                    for s in symbols]


class Prices:
    """The stream rows a maintenance pass reads: one fresh quote per symbol."""

    def __init__(self, mt):
        self.mt = mt
        self.rows = {}

    def set(self, symbol, bid, ask=None):
        self.rows[symbol] = (D(str(bid)), D(str(ask)) if ask is not None else D(str(bid)) + D(
            "0.03"))

    def __call__(self, setup):
        found = self.rows.get(setup["symbol"])
        if found is None:
            return None
        now = self.mt[1].now
        bid, ask = found
        return {"bid": str(bid), "ask": str(ask), "quote_at": now.isoformat(),
                "quote_received_at": now, "trade_price": str(bid), "trade_at": now.isoformat(),
                "trade_id": "fixture", "feed_healthy": True, "data_provider": "LAB_FIXTURE",
                "data_feed": "FIXTURE"}


def choice_answer(question, chosen, top=0.8):
    options = list(question["criteria"])
    rest = round((1 - top) / (len(options) - 1), 6)
    probabilities = {o: (top if o == chosen else rest) for o in options}
    probabilities[chosen] = round(1 - rest * (len(options) - 1), 6)
    return {"type": "choice", "choice": chosen, "confidence": 0.8,
            "probabilities": probabilities}


def resolve_label(question, label):
    """``first``/``last`` name the first or last code option (not KEEP or Insufficient)."""
    if label in {"first", "last"}:
        codes = [k for k in question["criteria"] if k not in {"KEEP", INSUFFICIENT}]
        return codes[0] if label == "first" else codes[-1]
    return label


def shaped_answer(question, given):
    """A choice answer with exact probabilities for some labels (package answer-rules: faithful
    copies of real answers, ties and Insufficient evidence): ``given`` maps labels (``first``
    and ``last`` allowed) to probabilities, and the other labels share the rest evenly. The
    choice is the most probable given label, the first listed on a tie (as TypeSafe reports
    one of the tied labels)."""
    given = {resolve_label(question, label): p for label, p in given.items()}
    labels = list(question["criteria"])
    assert set(given) <= set(labels), (given, labels)
    others = [label for label in labels if label not in given]
    rest = round((1 - sum(given.values())) / len(others), 6) if others else 0.0
    probabilities = {label: given.get(label, rest) for label in labels}
    top = max(given.values())
    assert all(rest < p for p in given.values() if p == top), "the rest must stay below the top"
    chosen = next(label for label, p in given.items() if p == top)
    return {"type": "choice", "choice": chosen, "confidence": 0.5,
            "probabilities": probabilities}


class Jev:
    """A scripted TypeSafe transport for V4 maintenance questions.

    ``script`` maps the four questions to labels; ``"first"``/``"last"`` pick the first or last
    code option; ``exact`` maps a question to exact probabilities (``shaped_answer``: ties,
    Insufficient evidence, copies of real answers), overriding its label; ``hook`` runs while
    the provider "answers" (to move the market meanwhile); ``status`` other than 200 answers
    with that HTTP status.
    """

    def __init__(self):
        self.script = {"trade_reason": "INTACT", "action": "HOLD", "stop_option": "KEEP",
                       "target_option": "KEEP"}
        self.exact = {}
        self.calls = []
        self.hook = None
        self.status = 200

    def answer(self, action, *, stop="KEEP", target="KEEP", reason="INTACT"):
        self.script = {"trade_reason": reason, "action": action, "stop_option": stop,
                       "target_option": target}

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        if self.hook is not None:
            self.hook(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "fixture unavailable"})
        questions = body["questions"]
        assert set(questions) == V4_QUESTIONS
        answers = {}
        for name, question in questions.items():
            if name in self.exact:
                answers[name] = shaped_answer(question, self.exact[name])
                continue
            label = self.script[name]
            codes = [k for k in question["criteria"] if k not in {"KEEP", INSUFFICIENT}]
            if label in {"first", "last"}:
                label = codes[0] if label == "first" else codes[-1]
            answers[name] = choice_answer(question, label)
        return httpx.Response(200, json={"model": JEV_MODEL, "answers": answers,
                                         "usage": {"input_tokens": 900, "output_tokens": 40}})


def maintainer(mt, jev=None, bars=None, *, reviews="ENABLED", failure_threshold=1000,
               max_inflight=10, guard=True):
    """``guard``: True builds the fixture spend guard (``FIXTURE_SPEND``), None builds none (V3
    trades then wait), or a ``SpendGuard`` to use."""
    engine, venue, store = mt
    jev = jev or Jev()
    bars = bars or Bars(venue)
    reviewer = JevReviewer(
        store, ReliabilityPolicy("MAINTENANCE_FIXTURE", 10, 1, 0.25, failure_threshold, 30),
        transport=httpx.MockTransport(jev), key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now,
    )
    maintenance = TradeMaintenance(
        engine, reviewer, bars=bars, clock=lambda: venue.now, management_reviews=reviews,
        live_prices=LivePriceReader(bars, clock=lambda: venue.now), max_inflight=max_inflight,
        spend_guard=spend_guard(mt) if guard is True else guard,
    )
    return SimpleNamespace(maintenance=maintenance, jev=jev, bars=bars, prices=Prices(mt),
                           reviewer=reviewer)


def run_pass(mt, kit, *, benchmark=None):
    return asyncio.run(kit.maintenance.run_pass(
        mt[0].store.active(), kit.prices, benchmark=benchmark,
        benchmark_ready=benchmark is not None,
    ))


def to_boundary(mt, seconds=1):
    """Move the venue clock just past the next completed 15-minute bar."""
    venue = mt[1]
    venue.now = cm.floor_time(venue.now, 900) + timedelta(seconds=900 + seconds)
    mt[0].reconciled_at = None
    assert mt[0].reconcile()["clean"]


@pytest.fixture
def pre_trade_plan_admission(monkeypatch):
    """Admission as before package trade-plan: the maintained arm records
    ``CRYPTO_MAINTENANCE_V3`` (no stop-raise guards) and a crypto setup records no
    ``stop_limit_policy`` (the one-tick stop-limit), exactly as every setup admitted before
    ``CRYPTO_MAINTENANCE_V4`` and ``CRYPTO_STOP_BREACH_V4``. The tests of those versions' own
    behaviour use it; the new versions' are tests/test_trade_plan_*.py."""
    from catalyst_lab import stop_breach

    # A v1/v2_admission pin stays; V4 (package trade-plan) and V5 (package jev-b1) are later.
    if cm.ADMITTED_MAINTENANCE in (cm.CRYPTO_MAINTENANCE_V4, cm.CRYPTO_MAINTENANCE_V5):
        monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V3)
    monkeypatch.setattr(stop_breach, "ADMITTED_STOP_LIMIT", None)


@pytest.fixture
def pre_jev_b1_admission(monkeypatch):
    """Admission as before package jev-b1: the maintained arm records ``CRYPTO_MAINTENANCE_V4``
    (V3's action questions with the stop-raise guards), exactly as every setup admitted before
    ``CRYPTO_MAINTENANCE_V5``. The tests of V4's own behaviour use it; V5's are
    tests/test_jev_b1_*.py."""
    if cm.ADMITTED_MAINTENANCE is cm.CRYPTO_MAINTENANCE_V5:
        monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V4)
