"""Shared fixtures for the research kit's answers to the app's reviews and exit flags (package
kit-answers, ``MUSE_ANSWER_RULES_V1``, ``research_agent/answers.py``).

Offline only: pending items in the exact shapes ``GET /api/v1/lab/reviews`` serves
(``trade_review.TradeReviewService._setup_items``), Coinbase 5-minute candle rows, and a fake app
on ``httpx.MockTransport`` that records every request and treats a repeated ``answer_id`` as the
app does. Tonight's UNI numbers are the task's: entry 8.604, stop 8.4427, Jev's flag at 02:28:58
UTC, and the 02:20-02:25 bar closing 8.5061. Every other price and ID here is a fixture value.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import httpx

from catalyst_lab import day_review as dr
from research_agent import market

FAKE_TOKEN = "fixture-kit-answers-token-never-written"  # noqa: S105 - fixture
AGENT_ID, AGENT_VERSION = "muse", "claude-as-muse-answers-v1-09.29"
AGENT = ["--agent-id", AGENT_ID, "--agent-version", AGENT_VERSION]
BASE_URL = "https://app.example"
REAL_CLIENT = httpx.Client  # Before any test replaces it with a MockTransport client.
COINBASE_HOST = "api.exchange.coinbase.com"

FLAG_AT = datetime(2026, 9, 29, 2, 28, 58, tzinfo=UTC)  # Jev flagged UNI/USD BROKEN.
ANSWER_AT = datetime(2026, 9, 29, 2, 29, 30, tzinfo=UTC)  # A poll 32 seconds later.
UNI = {"entry": "8.604", "stop": "8.4427", "target": "8.9"}  # The target is a fixture value.
FLAG_ID = str(uuid5(NAMESPACE_URL, "fixture-kit-answers:flag:UNI"))
REVIEW_ID = str(uuid5(NAMESPACE_URL, "fixture-kit-answers:review:UNI"))
SETUP_ID = str(uuid5(NAMESPACE_URL, "fixture-kit-answers:setup:UNI"))
LIFECYCLE_ID = str(uuid5(NAMESPACE_URL, "fixture-kit-answers:lifecycle:UNI"))
REVIEW_AT = datetime(2026, 9, 29, 2, 45, tzinfo=UTC)  # T of a fixture window review.
WINDOW_SECONDS = 14400  # The deploy example's window: 240 minutes.


def flag_item(flag_id=FLAG_ID, *, symbol="UNI/USD", levels=None, raised_at=FLAG_AT, **changes):
    """One ``EXIT_FLAG`` item exactly as the app lists it: ``trade`` is the flag's evidence
    (``trade_maintenance._flag``: the levels in force, the quote, the entry and R)."""
    levels = {**UNI, **(levels or {})}
    trade = {"levels": {"stop": levels["stop"], "target": levels["target"]},
             "quote": {"quote_source": "ALPACA_STREAM", "bid": "8.467", "ask": "8.4712",
                       "mid": "8.4691", "quote_at": raised_at.isoformat(),
                       "read_at": raised_at.isoformat(), "quote_age_seconds": "0.4",
                       "last": None, "last_at": None, "last_trade_id": None,
                       "last_source": None},
             "entry": levels["entry"], "risk_per_coin": "0.1613"}
    item = {"kind": "EXIT_FLAG", "flag_id": flag_id, "setup_id": SETUP_ID, "symbol": symbol,
            "lifecycle_id": LIFECYCLE_ID, "raised_by": "JEV", "raised_at": raised_at.isoformat(),
            "answer_due_at": (raised_at + timedelta(minutes=15)).isoformat(),
            "answer_route": f"/api/v1/lab/exit-flags/{flag_id}/answer",
            "answer_schema": "AGENT_REVIEW_ANSWER_V1",
            "jev_reasons": {"trade_reason": "BROKEN", "answers": {
                "trade_reason": {"choice": "BROKEN", "top_p": "0.81"},
                "action": {"choice": "FLAG_EARLY_EXIT", "top_p": "0.77"}},
                "trigger_reasons": ["BAR_1M"]},
            "trade": trade}
    item.update(changes)
    return item


def review_item(review_id=REVIEW_ID, *, rnd="FIRST", symbol="UNI/USD", levels=None,
                review_at=REVIEW_AT, window=WINDOW_SECONDS, **changes):
    """One ``DAY_REVIEW`` item exactly as the app lists it (``request`` is the recorded request
    without its addressee; ``holding_window_seconds`` only under ``CRYPTO_WINDOW_REVIEW_V1``)."""
    levels = {**UNI, **(levels or {})}
    route = f"/api/v1/lab/reviews/{review_id}/answer"
    request = {"policy_id": "CRYPTO_WINDOW_REVIEW_V1" if window else "CRYPTO_24H_REVIEW_V2",
               "review_id": review_id, **({"holding_window_seconds": window} if window else {}),
               "setup_id": SETUP_ID, "symbol": symbol, "lifecycle_id": LIFECYCLE_ID,
               "signal_id": "RA260928-UNI", "review_number": 1, "continuations": 0,
               "review_at": review_at.isoformat(),
               "requested_at": (review_at - timedelta(minutes=30)).isoformat(),
               "agent_answer_due_at": review_at.isoformat(),
               "answer_schema": "AGENT_REVIEW_ANSWER_V1", "answer_route": route,
               "trade": {"entry": levels["entry"], "quantity": "120", "stop": levels["stop"],
                         "target": levels["target"],
                         "levels": {"entry_trigger": "8.6", "max_entry_price": "8.61",
                                    "stop": "8.4427", "target": "8.9"},
                         "risk_per_coin": "0.1613", "opened_at": "2026-09-28T22:45:00+00:00",
                         "hours_in_trade": "3.5", "bid": "8.51", "ask": "8.52",
                         "quote_at": (review_at - timedelta(minutes=30)).isoformat(),
                         "unrealized_pnl_usd": "-11.28", "pnl_r": "-0.58"},
               "level_changes": [], "news_since_entry": [], "options": None,
               "original_pick": {"kind": "CHART", "thesis": "Fixture thesis."}}
    item = {"kind": "DAY_REVIEW", "review_id": review_id, "setup_id": SETUP_ID,
            "symbol": symbol, "lifecycle_id": LIFECYCLE_ID, "round": rnd,
            "answer_due_at": review_at.isoformat(), "review_at": review_at.isoformat(),
            "review_number": 1, "answer_route": route,
            "answer_schema": "AGENT_REVIEW_ANSWER_V1", "request": request}
    item.update(changes)
    return item


def five_minute_rows(closes, *, last_completed_end, forming=None):
    """Coinbase 5-minute rows ``[time, low, high, open, close, volume]``, newest first as
    Coinbase returns them: one completed bar per close, the last ending at
    ``last_completed_end``, then (``forming``) the bar still forming after it."""
    first = last_completed_end - timedelta(minutes=5 * len(closes))
    rows = [[int((first + timedelta(minutes=5 * index)).timestamp()), close, close, close,
             close, "10"] for index, close in enumerate(closes)]
    if forming is not None:
        rows.append([int(last_completed_end.timestamp()), forming, forming, forming, forming,
                     "3"])
    return list(reversed(rows))


def uni_rows():
    """The hour to 02:29:30: the 02:20-02:25 bar closes 8.5061 (the task's number); the bar
    starting 02:25 is still forming at 02:29:30 and is never used."""
    closes = ["8.61", "8.6", "8.59", "8.58", "8.57", "8.56", "8.55", "8.54", "8.53", "8.52",
              "8.5061"]
    return five_minute_rows(closes, last_completed_end=datetime(2026, 9, 29, 2, 25, tzinfo=UTC),
                            forming="8.47")


def candles_doc(rows, *, retrieved_at, coin="UNI"):
    """``market.fetch_candles``'s result for ``rows``."""
    return {"retrieved_at": retrieved_at.isoformat(), "product": f"{coin}-USD",
            "granularity": market.FIVE_MINUTE_SECONDS,
            "start": (retrieved_at - timedelta(hours=1)).isoformat(),
            "end": retrieved_at.isoformat(), "candles": rows}


class FakeApp:
    """The app's pending items and answer routes, and Coinbase's candles route, on one
    ``httpx.MockTransport``. Every request is recorded (method, host, path, Authorization,
    JSON body). An answer is checked with the app's own validator; the same ``answer_id`` with
    the same body is 200 ``idempotent_replay``, with another body 409
    ``IDEMPOTENCY_CONTENT_MISMATCH``, and any other second answer 409 ``*_ALREADY_ANSWERED``, as
    ``trade_review.TradeReviewService`` does. An answered item leaves the list."""

    def __init__(self, items=(), *, rows=None, now=ANSWER_AT):
        self.items = list(items)
        self.rows = dict(rows or {})
        self.now = now
        self.requests = []
        self.answers = {}  # answer route -> the body recorded
        self.reply = None  # (status, body) answering every POST instead, when set
        self.fail_before = False  # the POST never reaches the app
        self.lose_reply = False  # the app records the answer, then the reply is lost

    def install(self, monkeypatch):
        monkeypatch.setattr(httpx, "Client",
                            lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(self.handle)))
        monkeypatch.setattr(market, "time", _NoSleep)
        return self

    def of(self, method=None, host=None):
        return [r for r in self.requests if (method is None or r[0] == method)
                and (host is None or r[1] == host)]

    def handle(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.host, request.url.path,
                              request.headers.get("authorization"), body,
                              dict(request.url.params)))
        if request.url.host == COINBASE_HOST:
            coin = request.url.path.split("/")[2].split("-")[0]
            if coin not in self.rows:
                return httpx.Response(404, json={"message": "NotFound"})
            return httpx.Response(200, json=self.rows[coin])
        if request.method == "GET" and request.url.path == "/api/v1/lab/reviews":
            return httpx.Response(200, json={
                "as_of": self.now.isoformat(), "poll_hint_seconds": 60,
                "request_lead_seconds": 1800, "items": self.items, "trade_authorized": False})
        if request.method != "POST":
            return httpx.Response(404, json={"detail": "Not Found"})
        if self.fail_before:
            raise httpx.ConnectError("fixture: the app is unreachable")
        if self.reply is not None:
            return httpx.Response(self.reply[0], json=self.reply[1])
        return self._answer(request.url.path, body)

    def _answer(self, path, body):
        item = next((i for i in self.items if i["answer_route"] == path), None)
        flag = path.startswith("/api/v1/lab/exit-flags/")
        recorded = self.answers.get(path)
        if recorded is not None:
            if recorded["answer_id"] == body.get("answer_id"):
                if recorded != body:
                    return httpx.Response(409, json={"detail": "IDEMPOTENCY_CONTENT_MISMATCH"})
                return httpx.Response(200, json=self._receipt(path, body, replay=True))
            return httpx.Response(409, json={"detail": "EXIT_FLAG_ALREADY_ANSWERED" if flag
                                             else "REVIEW_ALREADY_ANSWERED"})
        if item is None:
            return httpx.Response(404, json={"detail": "EXIT_FLAG_NOT_FOUND" if flag
                                             else "REVIEW_NOT_FOUND"})
        try:
            dr.validate_review_answer(body, agent_id=AGENT_ID, now=self.now,
                                      suggestions_allowed=not flag)
        except ValueError as exc:
            return httpx.Response(422, json={"detail": str(exc)})
        self.answers[path] = body
        self.items.remove(item)
        if self.lose_reply:
            raise httpx.ReadTimeout("fixture: the reply was lost")
        return httpx.Response(200, json=self._receipt(path, body, replay=False))

    def _receipt(self, path, body, *, replay):
        item_id = path.split("/")[-2]
        flag = path.startswith("/api/v1/lab/exit-flags/")
        return {"status": "EXIT_FLAG_ANSWER_RECORDED" if flag else "REVIEW_ANSWER_RECORDED",
                **({"flag_id": item_id} if flag else {"review_id": item_id, "round": "FIRST"}),
                "decision": body["decision"], "received_at": self.now.isoformat(),
                "idempotent_replay": replay, "trade_authorized": False,
                "position_modified": False}


class _NoSleep:
    """``market``'s pacing sleep, skipped in tests."""

    @staticmethod
    def sleep(_seconds):
        return None


def token_file(tmp_path, value=FAKE_TOKEN):
    path = tmp_path / "agent-token"
    path.write_text(value)
    path.chmod(0o600)
    return path


def files_under(folder):
    return [path for path in folder.rglob("*") if path.is_file()]
