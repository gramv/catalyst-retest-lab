"""Fixture venue, mock Jev and agent client for the day-review tests (package day-review).

Disposable per-test PostgreSQL databases, the fake paper venue and scripted bars of
tests/maintenance_fixtures.py, a mock TypeSafe transport that answers the 24-hour review
(JEV_DAY_REVIEW_QUESTIONS_V1), early-exit (JEV_EARLY_EXIT_QUESTIONS_V1) and maintenance (V4)
question sets, and the real agent routes over an in-process TestClient. No broker, provider,
network or owner-ledger contact.
"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab.account_risk import JEV_MANAGED_ARM
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, digest
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.system_check import LivePriceReader
from catalyst_lab.trade_maintenance import TradeMaintenance
from catalyst_lab.trade_review import DayReviews, TradeReviewService
from tests.maintenance_fixtures import (
    DEFAULT,
    Bars,
    Prices,
    choice_answer,
    entry_order,
    fill,
    fresh_bar,
    quote,
    shaped_answer,
    spend_guard,
    trigger,
)
from tests.test_jev_review import FIXTURE_KEY

LEGACY = "fixture-day-review-legacy-token-" + "l" * 16
STATUS = "fixture-day-review-status-token-" + "s" * 16
OPERATOR = "fixture-day-review-operator-token-" + "o" * 16
AGENTS = {"claude": "fixture-day-review-claude-token-" + "c" * 16,
          "instinct": "fixture-day-review-instinct-token-" + "i" * 16}


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def managed_arm(monkeypatch):
    """Every admission lands in the maintained arm (the arm is otherwise random)."""
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda *_: JEV_MANAGED_ARM)


# --- Admission, entry and the clock --------------------------------------------------------------


def publish(mt, symbols, *, agent_id="claude", levels=DEFAULT):
    from tests.test_system_check import publish_v3, two_slots, v3_pick

    venue = mt[1]
    slot, _ = two_slots(venue.now)
    return publish_v3(mt, [v3_pick(i, symbol, venue.now, levels=levels)
                           for i, symbol in enumerate(symbols)], run_slot=slot,
                      agent_id=agent_id)


def open_trade(mt, symbol="SOL/USD", *, agent_id="claude", bid="100.20"):
    """A report-V3 pick admitted, triggered, filled at the max entry and protected."""
    from tests.test_system_check import at_price

    engine, venue, _ = mt
    fresh_bar(mt)
    selected = publish(mt, [symbol], agent_id=agent_id)
    sid = engine.admit(selected[symbol], live_quote=at_price("100.49", "100.51", at=venue.now))
    trigger(mt, sid)
    order = entry_order(mt, symbol)
    fill(mt, order, order["qty"])
    engine.manage(sid, quote(mt, bid))
    engine.manage(sid, quote(mt, bid))
    assert engine._load(sid)[1]["state"] == "OPEN"
    return sid


def state(mt, sid):
    return mt[0]._load(sid)[1]


def move_to(mt, at):
    """Move the venue clock (it only moves forward) and reconcile, as a new day needs."""
    engine, venue, _ = mt
    assert at >= venue.now
    venue.now = at
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]


def review_at(mt, sid):
    from datetime import datetime

    return datetime.fromisoformat(state(mt, sid)["day_review_at"])


def stop_orders(mt, symbol, *, live=True):
    return [o for o in mt[1].orders.values() if o["symbol"] == symbol and o["type"] == "stop_limit"
            and (not live or o["status"] in {"new", "accepted", "partially_filled"})]


def market_sells(mt, symbol):
    return [o for o in mt[1].orders.values() if o["symbol"] == symbol and o["side"] == "sell"
            and o["type"] == "market"]


def events(engine, kind, setup_id=None):
    with engine.repo.connect() as conn:
        if setup_id is None:
            return [r["body"] for r in conn.execute(
                "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                (kind,)).fetchall()]
        return [r["body"] for r in conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind=%s AND setup_id=%s
            ORDER BY event_seq""", (kind, setup_id)).fetchall()]


def decisions(engine, setup_id, action=None):
    with engine.repo.connect() as conn:
        rows = conn.execute("""SELECT d.*,EXISTS(SELECT 1 FROM lab.managed_claims c
            WHERE c.decision_id=d.decision_id) AS claimed FROM lab.managed_risk_decisions d
            WHERE d.setup_id=%s ORDER BY d.event_seq""", (setup_id,)).fetchall()
    return [r for r in rows if action is None or r["action"] == action]


def sell_to_close(mt, sid, symbol, bid):
    """The protection loop turns an exit request into a cancelled stop-limit and one market
    sell (each under its own claimed authorization); the sell fills and the trade closes."""
    engine, venue, _ = mt
    for _ in range(3):
        engine.manage(sid, quote(mt, bid))
    [close] = market_sells(mt, symbol)
    engine.ingest(venue.fill(close["id"], close["qty"], price=bid))
    engine.manage(sid, quote(mt, bid))
    return close


# --- The mock Jev --------------------------------------------------------------------------------


def question_kind(questions):
    if "exit_now" in questions:
        return "EARLY_EXIT"
    if "decision" in questions:
        return "DAY_REVIEW"
    if "action" in questions:
        return "MAINTENANCE"
    return "UNKNOWN"


class ReviewJev:
    """Scripted TypeSafe answers by question set.

    ``review`` answers the 24-hour review's first round, ``final`` its final round (default:
    ``review``), ``flag`` an agent's early-exit flag and ``maintenance`` the V4 maintenance
    review. Labels ``first``/``last`` pick the first or last code option. ``exact`` maps a
    kind (``DAY_REVIEW``, ``DAY_REVIEW_FINAL``, ``EARLY_EXIT``, ``MAINTENANCE``) to questions
    answered with exact probabilities (``shaped_answer``: ties, Insufficient evidence, copies
    of real answers; package answer-rules). ``status`` (an int, or a dict by kind) other than
    200 answers with that HTTP status; ``hook`` runs while "answering"."""

    def __init__(self):
        self.review = {"trade_reason": "INTACT", "agent_case": "HOLDS", "decision": "CONTINUE",
                       "stop_option": "KEEP", "target_option": "KEEP"}
        self.final = None
        self.flag = {"trade_reason": "WEAKENED", "agent_case": "HOLDS", "exit_now": "EXIT"}
        self.maintenance = {"trade_reason": "INTACT", "action": "HOLD", "stop_option": "KEEP",
                            "target_option": "KEEP"}
        self.status = 200
        self.hook = None
        self.calls = []
        self.exact = {}

    def answer(self, **labels):
        self.review = {**self.review, **labels}

    def __call__(self, request):
        body = json.loads(request.content)
        questions = body["questions"]
        kind = question_kind(questions)
        self.calls.append((kind, body))
        if self.hook is not None:
            self.hook(kind, body)
        status = self.status.get(kind, 200) if isinstance(self.status, dict) else self.status
        if status != 200:
            return httpx.Response(status, json={"error": "fixture unavailable"})
        exact = self.exact.get(kind, {})
        if kind == "DAY_REVIEW":
            final = body["state"]["review"]["round"] == "FINAL"
            script = (self.final or self.review) if final else self.review
            if final:
                exact = self.exact.get("DAY_REVIEW_FINAL", exact)
        else:
            script = {"EARLY_EXIT": self.flag, "MAINTENANCE": self.maintenance}[kind]
        answers = {}
        for name, question in questions.items():
            if name in exact:
                answers[name] = shaped_answer(question, exact[name])
                continue
            label = script[name]
            codes = [k for k in question["criteria"] if k not in {"KEEP", INSUFFICIENT}]
            if label in {"first", "last"}:
                label = codes[0] if label == "first" else codes[-1]
            answers[name] = choice_answer(question, label)
        return httpx.Response(200, json={"model": JEV_MODEL, "answers": answers,
                                         "usage": {"input_tokens": 900, "output_tokens": 40}})

    def of(self, kind):
        return [body for k, body in self.calls if k == kind]


def reviewer_for(mt, jev, *, failure_threshold=1000):
    engine, venue, store = mt
    return JevReviewer(
        store, ReliabilityPolicy("DAY_REVIEW_FIXTURE", 10, 1, 0.25, failure_threshold, 30),
        transport=httpx.MockTransport(jev), key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now,
    )


def kit(mt, *, jev=None, reviews="ENABLED", agents=frozenset({"claude", "instinct", "muse"})):
    """The day-review component, the agent routes (over the real app) and a maintenance
    component sharing one mock Jev, bars and stream prices."""
    engine, venue, _ = mt
    jev = jev or ReviewJev()
    bars = Bars(venue)
    reviewer = reviewer_for(mt, jev)
    day = DayReviews(engine, reviewer, bars=bars, clock=lambda: venue.now,
                     management_reviews=reviews, agents=agents,
                     live_prices=LivePriceReader(bars, clock=lambda: venue.now))
    # CRYPTO_MAINTENANCE_V3 (package jev-budget): the deploy example's budget, never reached by a
    # test ledger, so maintained trades keep the per-minute cadence.
    maintenance = TradeMaintenance(engine, reviewer, bars=bars, clock=lambda: venue.now,
                                   management_reviews=reviews,
                                   live_prices=LivePriceReader(bars, clock=lambda: venue.now),
                                   spend_guard=spend_guard((engine, venue, reviewer.store)))
    service = TradeReviewService(engine.store, clock=lambda: venue.now, agents=agents)
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    app = create_managed_app(cycle, engine.store, api_token=LEGACY, runtime_status=lambda: {},
                             status_token=STATUS, operator_token=OPERATOR, agent_tokens=AGENTS,
                             trade_reviews=service)
    return SimpleNamespace(day=day, jev=jev, bars=bars, prices=Prices(mt), reviewer=reviewer,
                           maintenance=maintenance, service=service, client=TestClient(app))


def run(mt, k, *, bid=None, symbol="SOL/USD"):
    """One day-review pass at the venue's time, with a fresh stream quote at ``bid``."""
    if bid is not None:
        k.prices.set(symbol, bid)
    return asyncio.run(k.day.run_pass(mt[0].store.active(), k.prices))


def maintain(mt, k, *, bid=None, symbol="SOL/USD"):
    if bid is not None:
        k.prices.set(symbol, bid)
    return asyncio.run(k.maintenance.run_pass(mt[0].store.active(), k.prices))


# --- The agent client ----------------------------------------------------------------------------


def answer_body(decision="CONTINUE", **changes):
    body = {
        "schema_version": "AGENT_REVIEW_ANSWER_V1", "answer_id": str(uuid4()),
        "decision": decision,
        "what_changed": "Synthetic fixture: volume stayed above the 7-day average and the "
                        "hourly structure kept higher lows since entry.",
        "next_24h": "Synthetic fixture: a retest of the 24-hour high is expected while the "
                    "hourly higher lows hold.",
        "proves_wrong": "Synthetic fixture: an hourly close below the entry price.",
    }
    body.update(changes)
    return body


def flag_body(lifecycle_id, **changes):
    body = {
        "schema_version": "AGENT_EXIT_FLAG_V1", "flag_ref": str(uuid4()),
        "lifecycle_id": lifecycle_id,
        "what_changed": "Synthetic fixture: the exchange paused withdrawals of the coin an "
                        "hour ago.",
        "next_24h": "Synthetic fixture: selling pressure is expected while withdrawals stay "
                    "paused.",
        "proves_wrong": "Synthetic fixture: withdrawals resume within the hour.",
    }
    body.update(changes)
    return body


def source(mt, excerpt, *, stance="ADVERSE"):
    now = mt[1].now.isoformat()
    return {"source_id": "fixture-" + digest(excerpt)[:8], "url": "https://example.org/news",
            "excerpt": excerpt, "published_at": now, "retrieved_at": now,
            "content_hash": digest(excerpt), "primary_source": True, "asset_relevant": True,
            "novelty": "NEW_FACT", "stance": stance}


def pending(k, token=AGENTS["claude"]):
    reply = k.client.get("/api/v1/lab/reviews", headers=bearer(token))
    assert reply.status_code == 200, reply.text
    return reply.json()["items"]


def answer(k, review_id, body, token=AGENTS["claude"]):
    return k.client.post(f"/api/v1/lab/reviews/{review_id}/answer", json=body,
                         headers=bearer(token))


def post_news(mt, sid, excerpt, *, stance="ADVERSE"):
    from catalyst_lab.position_news import PositionNewsService

    engine, venue, _ = mt
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    context = service.context(sid)
    return service.submit(sid, {
        "news_id": str(uuid4()), "lifecycle_id": context["lifecycle_id"],
        "expected_news_revision": context["news_revision"],
        "sources": [source(mt, excerpt, stance=stance)],
    })


def at_request(mt, sid, seconds=1):
    """T minus 30 minutes, plus ``seconds``."""
    move_to(mt, review_at(mt, sid) - timedelta(minutes=30) + timedelta(seconds=seconds))


def at_review(mt, sid, seconds=1):
    move_to(mt, review_at(mt, sid) + timedelta(seconds=seconds))


def bid_near(risk_r):
    """A bid ``risk_r`` R above the fixture entry (100.10, R 5.10), on the 0.01 grid."""
    return str((D("100.10") + D("5.10") * D(str(risk_r))).quantize(D("0.01")))


__all__ = ["cm"]
