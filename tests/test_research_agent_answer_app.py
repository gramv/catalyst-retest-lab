"""The kit's ``answer`` command against the app's real routes (package kit-answers,
MUSE_ANSWER_RULES_V1, 2026-09-29).

The kit reads its pending items from the real ``GET /api/v1/lab/reviews`` and posts its
answers to the real answer routes, in process, on the day-review fixtures
(tests/day_review_fixtures.py): disposable per-test PostgreSQL databases, the fake paper venue,
scripted bars and a mock Jev. Coinbase's candles are fixture rows on ``httpx.MockTransport``.
What the app then decides with Jev is read back from the ledger:

* a Jev flag the kit answers EXIT sells at market (``EARLY_EXIT_AGREED``); one it answers
  CONTINUE keeps the trade (``EXIT_NOT_AGREED``), the half-risk line deciding at its exact
  value;
* a window review (``CRYPTO_WINDOW_REVIEW_V1``) answered CONTINUE, then, after Jev disagrees,
  its discussion reply answered EXIT on the next bar: two answers, each under its own round's
  ``answer_id``, and the trade exits by agreement.

Fixture evidence only: no broker, provider, network or owner-ledger contact.
"""

from datetime import datetime, timedelta

import httpx
import pytest

from catalyst_lab import crypto_holding as ch
from catalyst_lab import day_review as dr
from research_agent import answers, market, run
from tests.day_review_fixtures import (
    AGENTS,
    at_request,
    at_review,
    events,
    kit,
    maintain,
    managed_arm,
    open_trade,
    pending,
    sell_to_close,
    state,
)
from tests.day_review_fixtures import run as review_pass
from tests.kit_answers_fixtures import (
    COINBASE_HOST,
    REAL_CLIENT,
    _NoSleep,
    files_under,
    five_minute_rows,
    token_file,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

pytestmark = pytest.mark.usefixtures("managed_arm")
_ = managed_arm
KIT_AGENT = ["--agent-id", "claude", "--agent-version", "kit-answers-e2e-v1"]
# The fixture trade: filled at 100.10 with its stop at 95, so the half-risk line is 97.55.
HALF_RISK = "97.55"


@pytest.fixture
def window(mt):
    """The deploy example's window setting (240 minutes) on the fixture engine, as
    ``build_runtime_from_env`` sets it from ``MANAGED_CRYPTO_WINDOW_JSON``."""
    mt[0].crypto_window = ch.parse_window_setting(
        '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}')
    return mt[0].crypto_window


def closing_at(now, close):
    """Coinbase rows whose last completed 5-minute bar, the one ending at or before ``now``,
    closes ``close``; the bar after it is still forming."""
    end = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    return five_minute_rows(["101", "100.5", close], last_completed_end=end, forming="99")


def kit_answer(tmp_path, monkeypatch, k, now, rows_by_coin, *extra):
    """``python -m research_agent.run answer`` with the proposing agent's own token. The app's
    routes are answered by the real app in process; Coinbase's by the fixture rows."""
    seen = []

    def handle(request):
        seen.append((request.method, request.url.host, request.url.path))
        if request.url.host == COINBASE_HOST:
            coin = request.url.path.split("/")[2].split("-")[0]
            return httpx.Response(200, json=rows_by_coin[coin])
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() in {"authorization", "content-type", "accept"}}
        reply = k.client.request(request.method, request.url.path, content=request.content,
                                 headers=headers)
        return httpx.Response(reply.status_code, content=reply.content,
                              headers={"content-type": reply.headers.get("content-type",
                                                                         "application/json")})

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "Client",
                      lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handle)))
        patch.setattr(market, "time", _NoSleep)
        code = run.main(["answer", "--base-url", "https://app.example", "--token-file",
                         str(token_file(tmp_path, AGENTS["claude"])), *KIT_AGENT,
                         "--run-dir", str(tmp_path / "answers"), "--now", now.isoformat(),
                         *extra])
    return code, seen


def jev_flags(mt, k, sid):
    """A maintenance review in which Jev flags the trade (FLAG_EARLY_EXIT, BROKEN), then the
    day-review pass that asks the proposing agent (tests/test_early_exit.py's)."""
    k.jev.maintenance = {"trade_reason": "BROKEN", "action": "FLAG_EARLY_EXIT",
                         "stop_option": "KEEP", "target_option": "KEEP"}
    mt[1].now += timedelta(seconds=1)
    maintain(mt, k, bid="106")
    [flag] = events(mt[0], dr.FLAG_RAISED, sid)
    review_pass(mt, k, bid="106")
    return flag


def assert_no_token(tmp_path):
    for path in files_under(tmp_path / "answers"):
        assert AGENTS["claude"] not in path.read_text(), path


@pytest.mark.parametrize("close, decision, outcome", [
    (HALF_RISK, "EXIT", "EXIT_AGREED"),  # At the half-risk line exactly: exit.
    ("97.56", "CONTINUE", "EXIT_NOT_AGREED"),  # A cent above it: the stop decides.
])
@pytest.mark.usefixtures("pre_jev_b1_admission")  # V4's maintenance (before jev-b1).
def test_the_kit_answers_a_real_jev_flag_and_the_app_decides_by_it(
        mt, tmp_path, monkeypatch, close, decision, outcome):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    flag = jev_flags(mt, k, sid)
    [item] = pending(k)
    assert (item["kind"], item["flag_id"]) == ("EXIT_FLAG", flag["flag_id"])
    code, seen = kit_answer(tmp_path, monkeypatch, k, venue.now, {"SOL": closing_at(venue.now,
                                                                                    close)})
    assert code == 0
    assert [(method, host) for method, host, _path in seen] == [
        ("GET", "app.example"), ("GET", COINBASE_HOST), ("POST", "app.example")]
    [recorded] = events(engine, dr.FLAG_AGENT_ANSWER, sid)
    record = answers.load_record(tmp_path / "answers" / "items" / f"flag-{flag['flag_id']}.json")
    assert record["state"] == "RECORDED" and record["body"]["decision"] == decision
    assert recorded["agent_id"] == "claude"
    assert recorded["answer"] == {**record["body"], "suggested_stop": None,
                                  "suggested_target": None, "sources": []}
    assert recorded["answer"]["answer_id"] == answers.answer_id_for("EXIT_FLAG",
                                                                    flag["flag_id"])
    assert pending(k) == []
    review_pass(mt, k, bid="97.6")  # The app resolves the flag by both sides' answers.
    [resolved] = events(engine, dr.EARLY_EXIT_DECISION, sid)
    assert (resolved["outcome"], resolved["agent"]["decision"]) == (outcome, decision)
    if decision == "EXIT":
        assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"
        sell_to_close(mt, sid, "SOL/USD", "97.6")
        assert (state(mt, sid)["state"], state(mt, sid)["reason"]) == (
            "CLOSED", "EARLY_EXIT_AGREED")
    else:
        assert not state(mt, sid).get("exit_requested")
        assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    # A rerun finds nothing pending: one GET.
    code, seen = kit_answer(tmp_path, monkeypatch, k, venue.now, {})
    assert code == 0 and seen == [("GET", "app.example", "/api/v1/lab/reviews")]
    assert_no_token(tmp_path)


def test_the_kit_answers_both_rounds_of_a_real_window_review(mt, window, tmp_path,
                                                             monkeypatch):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["holding_policy"]["policy_id"] == "CRYPTO_WINDOW_REVIEW_V1"
    k = kit(mt)
    k.jev.answer(decision="EXIT", trade_reason="WEAKENED", agent_case="DOES_NOT_HOLD")
    at_request(mt, sid)
    review_pass(mt, k, bid="100.2")
    [item] = pending(k)
    assert (item["kind"], item["round"]) == ("DAY_REVIEW", "FIRST")
    entry = item["request"]["trade"]["entry"]
    # The first answer: the last 5-minute close is the entry exactly, so the trade is working.
    code, _seen = kit_answer(tmp_path, monkeypatch, k, venue.now,
                             {"SOL": closing_at(venue.now, entry)})
    assert code == 0
    [first] = events(engine, dr.AGENT_ANSWER, sid)
    assert (first["round"], first["answer"]["decision"]) == ("FIRST", "CONTINUE")
    assert first["answer"]["answer_id"] == answers.answer_id_for("DAY_REVIEW", item["review_id"],
                                                                 "FIRST")
    assert "another 4-hour window" in first["answer"]["next_24h"]
    # At T Jev says exit: they disagree, and the discussion item comes back.
    at_review(mt, sid)
    review_pass(mt, k, bid="100.2")
    [opened] = events(engine, dr.DISCUSSION_OPENED, sid)
    [reply_item] = pending(k)
    assert reply_item["round"] == "DISCUSSION"
    # The reply is decided on the bar completed by then: below the entry, a time stop.
    assert datetime.fromisoformat(opened["opened_at"]) == venue.now
    code, _seen = kit_answer(tmp_path, monkeypatch, k, venue.now,
                             {"SOL": closing_at(venue.now, "100.09")})
    assert code == 0
    first_again, reply = events(engine, dr.AGENT_ANSWER, sid)
    assert first_again == first  # The first answer is never sent again.
    assert (reply["round"], reply["answer"]["decision"]) == ("DISCUSSION", "EXIT")
    assert reply["answer"]["answer_id"] == answers.answer_id_for(
        "DAY_REVIEW", item["review_id"], "DISCUSSION") != first["answer"]["answer_id"]
    review_pass(mt, k, bid="100.2")  # Jev's final answer: still exit. Agreement decides.
    [decision] = events(engine, dr.DECISION, sid)
    assert (decision["outcome"], decision["code"]) == ("EXIT", "AGREED_AFTER_DISCUSSION")
    assert [decision["agent"]["answers"][rnd]["decision"] for rnd in ("FIRST", "DISCUSSION")] == [
        "CONTINUE", "EXIT"]
    assert state(mt, sid)["exit_requested"] == "DAY_REVIEW_EXIT"
    items = sorted(path.name for path in (tmp_path / "answers" / "items").iterdir())
    assert items == [f"review-{item['review_id']}-discussion.json",
                     f"review-{item['review_id']}-first.json"]
    assert_no_token(tmp_path)
