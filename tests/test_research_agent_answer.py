"""research_agent.answers and ``python -m research_agent.run answer``: MUSE_ANSWER_RULES_V2, the
research agent's answers to the app's window reviews and Jev early-exit flags (package
kit-answers, 2026-09-29).

Offline only. The pending items are fixtures in the app's own shapes; the app's routes and
Coinbase's candles route are an ``httpx.MockTransport`` (tests/kit_answers_fixtures.py), and
every answer body is checked with the app's own validator. Nothing here reaches a real
service. The real routes on a disposable database: tests/test_research_agent_answer_app.py.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest

from catalyst_lab import day_review as dr
from research_agent import answers, market, run
from research_agent import token as token_module
from tests.kit_answers_fixtures import (
    AGENT,
    AGENT_ID,
    ANSWER_AT,
    BASE_URL,
    COINBASE_HOST,
    FAKE_TOKEN,
    FLAG_AT,
    FLAG_ID,
    REAL_CLIENT,
    REVIEW_ID,
    FakeApp,
    candles_doc,
    files_under,
    five_minute_rows,
    flag_item,
    review_item,
    token_file,
    uni_rows,
)
from tests.test_research_agent_procedure import PROCEDURE, PROMPTS, commands, section

D = Decimal
FLAG_ROUTE = f"/api/v1/lab/exit-flags/{FLAG_ID}/answer"
REVIEW_ROUTE = f"/api/v1/lab/reviews/{REVIEW_ID}/answer"


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


def item_of(raw):
    return answers.parse_item(raw)


def decided(raw, rows, *, at=ANSWER_AT):
    return answers.decide(item_of(raw), candles_doc(rows, retrieved_at=at))


def stale_quote(raw, *, at=FLAG_AT - timedelta(hours=1)):
    """``raw`` with the app's own quote from ``at`` (an hour before the flag by default): too
    old for MUSE_ANSWER_RULES_V2's fallback, so only a usable bar can decide."""
    trade = raw["trade"] if raw["kind"] == "EXIT_FLAG" else raw["request"]["trade"]
    quote = trade["quote"] if raw["kind"] == "EXIT_FLAG" else trade
    quote["quote_at"] = at.isoformat()
    return raw


def rows_closing(close, *, at=ANSWER_AT, age=None):
    """Candles whose last completed bar closes ``close``: by default the last bar completed at
    ``at``, followed by the one still forming; with ``age``, the bar that ended ``age`` before
    ``at`` (on a 5-minute boundary), and none after it (no trade since)."""
    if age is None:
        end = at.replace(minute=at.minute - at.minute % 5, second=0, microsecond=0)
        return five_minute_rows(["1", "1", close], last_completed_end=end, forming="1")
    end = at - age
    assert end.minute % 5 == 0 and end.second == 0, end
    return five_minute_rows(["1", "1", close], last_completed_end=end)


# --- The rules at their exact boundaries -----------------------------------------------------

@pytest.mark.parametrize("close, decision, reason", [
    ("95", "EXIT", answers.AT_OR_BELOW_HALF_RISK),  # Exactly the half-risk line.
    ("95.00001", "CONTINUE", answers.ABOVE_EXIT_LINE),  # Just above it: the stop decides.
    ("90.00001", "EXIT", answers.AT_OR_BELOW_HALF_RISK),
    ("90", "EXIT", answers.AT_OR_BELOW_STOP),  # At the stop.
    ("89", "EXIT", answers.AT_OR_BELOW_STOP),
])
def test_the_flag_rule_exits_at_or_below_half_the_planned_risk(close, decision, reason):
    found, why, half_risk, line = answers.flag_rule(D("100"), D("90"), D(close))
    assert (found, why) == (decision, reason)
    assert half_risk == line == D("95")  # 100 - 0.5 x (100 - 90).


@pytest.mark.parametrize("close, decision, reason", [
    ("102", "EXIT", answers.AT_OR_BELOW_STOP),  # A stop raised above the entry: its line.
    ("102.0001", "CONTINUE", answers.ABOVE_EXIT_LINE),
    ("101.5", "EXIT", answers.AT_OR_BELOW_STOP),  # Above the half-risk line, below the stop.
])
def test_a_stop_above_the_entry_is_the_flags_exit_line(close, decision, reason):
    found, why, half_risk, line = answers.flag_rule(D("100"), D("102"), D(close))
    assert (found, why, half_risk, line) == (decision, reason, D("101"), D("102"))


@pytest.mark.parametrize("close, decision, reason", [
    ("8.604", "CONTINUE", answers.AT_OR_ABOVE_ENTRY),  # Exactly the entry: working.
    ("8.6040001", "CONTINUE", answers.AT_OR_ABOVE_ENTRY),
    ("8.6039999", "EXIT", answers.BELOW_ENTRY),  # Just below: a time stop.
])
def test_the_review_rule_continues_at_or_above_the_entry(close, decision, reason):
    assert answers.review_rule(D("8.604"), D(close)) == (decision, reason)


def test_tonights_uni_flag_is_an_exit():
    """Jev flagged UNI/USD at 02:28:58 UTC (entry 8.604, stop 8.4427). At 02:29:30 the last
    completed bar is 02:20-02:25, closed 8.5061: at or below 8.604 - 0.5 x 0.1613 = 8.52335."""
    decision = decided(flag_item(), uni_rows())
    assert (decision.decision, decision.reason) == ("EXIT", answers.AT_OR_BELOW_HALF_RISK)
    assert decision.bar.started_at == datetime(2026, 9, 29, 2, 20, tzinfo=UTC)
    assert decision.bar.close == D("8.5061")  # Not the 02:25 bar, still forming (8.47).
    assert decision.numbers["half_risk"] == "8.52335"
    assert D("8.604") - D("0.5") * (D("8.604") - D("8.4427")) == D("8.52335")
    body = answers.answer_body(item_of(flag_item()), decision)
    assert body["decision"] == "EXIT"
    assert body["what_changed"] == (
        "The last completed 5-minute bar (2026-09-29 02:20-02:25 UTC) closed at 8.5061, at or "
        "below 8.52335, halfway between the entry 8.604 and the stop 8.4427: the trade has "
        "given back half of its planned risk.")
    assert body["next_24h"] == ("The setup has failed by this rule: exit now near 8.5061 "
                                "rather than wait for the stop at 8.4427.")
    assert body["proves_wrong"] == "A 5-minute close back above 8.52335."


def test_the_texts_are_built_from_the_numbers_read_for_every_decision():
    flag_continue = decided(flag_item(), rows_closing("8.55"))
    body = answers.answer_body(item_of(flag_item()), flag_continue)
    assert (body["decision"], body["next_24h"], body["proves_wrong"]) == (
        "CONTINUE", "The stop 8.4427 decides: the trade keeps its stop and its target 8.9.",
        "A 5-minute close at or below 8.52335.")
    assert "closed at 8.55, above 8.52335, halfway between the entry 8.604" in body[
        "what_changed"]
    at_stop = answers.answer_body(item_of(flag_item()),
                                  decided(flag_item(), rows_closing("8.44")))
    assert at_stop["what_changed"].endswith("at or below the stop 8.4427 (entry 8.604).")
    assert at_stop["proves_wrong"] == "A 5-minute close back above 8.52335."
    working = answers.answer_body(item_of(review_item()),
                                  decided(review_item(), rows_closing("8.61")))
    assert working == {
        "schema_version": "AGENT_REVIEW_ANSWER_V1",
        "answer_id": answers.answer_id_for("DAY_REVIEW", REVIEW_ID, "FIRST"),
        "decision": "CONTINUE",
        "what_changed": "The last completed 5-minute bar (2026-09-29 02:20-02:25 UTC) closed "
                        "at 8.61, at or above the entry 8.604: the trade is working.",
        "next_24h": "It continues for another 4-hour window toward the target 8.9; the stop "
                    "8.4427 decides if the price falls.",
        "proves_wrong": "A 5-minute close below the entry 8.604.",
    }
    stopped = answers.answer_body(item_of(review_item(window=None)),
                                  decided(review_item(window=None), rows_closing("8.6")))
    assert stopped["decision"] == "EXIT"
    assert stopped["what_changed"].endswith("below the entry 8.604: the trade did not work "
                                            "within its window.")
    assert stopped["next_24h"] == ("A time stop: exit now rather than hold the trade for "
                                   "another 24 hours.")  # A 24-hour version's review.
    assert stopped["proves_wrong"] == "A 5-minute close back at or above the entry 8.604."


@pytest.mark.parametrize("raw, close", [
    (flag_item(), "8.5061"), (flag_item(), "8.55"), (flag_item(), "8.44"),
    (review_item(), "8.61"), (review_item(), "8.6"),
    (review_item(rnd="DISCUSSION"), "8.61"),
])
def test_every_body_passes_the_apps_own_answer_validation(raw, close):
    item = item_of(raw)
    body = answers.answer_body(item, decided(raw, rows_closing(close)))
    assert set(body) == {"schema_version", "answer_id", "decision", "what_changed", "next_24h",
                         "proves_wrong"}  # No suggestion, source or confidence, ever.
    for allowed in (False, True):  # A flag's answer and a review's.
        canonical = dr.validate_review_answer(body, agent_id=AGENT_ID, now=ANSWER_AT,
                                              suggestions_allowed=allowed)
        assert canonical == {**body, "suggested_stop": None, "suggested_target": None,
                             "sources": []}
    assert dr.identity_leaks({**body, "sources": []}, AGENT_ID) == []
    assert "UNI" not in json.dumps(body) and "muse" not in json.dumps(body).lower()
    assert len(json.dumps(body).encode()) < 12_000
    assert answers.body_problems(body, agent_id=AGENT_ID, now=ANSWER_AT) == []


def test_the_apps_validator_refusals_reach_the_kit_before_any_send():
    body = answers.answer_body(item_of(flag_item()), decided(flag_item(), uni_rows()))
    # An agent ID that is a word of the texts: the app would refuse AGENT_IDENTITY_IN_ANSWER.
    assert answers.body_problems(body, agent_id="stop", now=ANSWER_AT) == [
        "answer: AGENT_IDENTITY_IN_ANSWER (the app's own answer validator)"]
    assert "SUGGESTED_LEVELS_NOT_ALLOWED" in answers.body_problems(
        {**body, "suggested_stop": "8.5"}, agent_id=AGENT_ID, now=ANSWER_AT)[-1]


def test_answer_ids_are_deterministic_per_flag_and_per_review_round():
    flag = item_of(flag_item())
    assert flag.answer_id == str(uuid5(NAMESPACE_URL, f"agent-review-answer:exit-flag:{FLAG_ID}"))
    assert item_of(flag_item()).answer_id == flag.answer_id
    first, reply = item_of(review_item()), item_of(review_item(rnd="DISCUSSION"))
    assert first.answer_id != reply.answer_id  # The app replays an ID across both rounds.
    assert first.key == f"review-{REVIEW_ID}-first" and reply.key.endswith("-discussion")
    assert UUID(first.answer_id).version == 5


# --- No usable data --------------------------------------------------------------------------

@pytest.mark.parametrize("age, usable", [
    (timedelta(minutes=15), True),  # Closed exactly 15 minutes before the read: usable.
    (timedelta(minutes=15, seconds=1), False),
    (timedelta(minutes=20), False),
])
def test_the_last_completed_bar_must_have_closed_within_15_minutes(age, usable):
    end = datetime(2026, 9, 29, 2, 15, tzinfo=UTC)
    rows = five_minute_rows(["8.7"], last_completed_end=end)
    decision = answers.decide(item_of(stale_quote(flag_item())),
                              candles_doc(rows, retrieved_at=end + age))
    if usable:
        assert decision.decision == "CONTINUE"
    else:
        assert (decision.decision, decision.reason) == (None, answers.NO_RECENT_BAR)
        assert "more than 15 minutes before" in decision.detail


def test_no_completed_bar_and_unreadable_candles_are_no_answer():
    only_forming = five_minute_rows([], last_completed_end=datetime(2026, 9, 29, 2, 25,
                                                                    tzinfo=UTC), forming="8.5")
    assert decided(stale_quote(flag_item()), only_forming).reason == answers.NO_RECENT_BAR
    assert decided(stale_quote(flag_item()), []).reason == answers.NO_RECENT_BAR
    broken = decided(stale_quote(flag_item()), [[1, "x"]])
    assert (broken.decision, broken.reason) == (None, answers.CANDLES_UNAVAILABLE)


def test_without_a_recent_bar_the_apps_own_bid_decides():
    """MUSE_ANSWER_RULES_V2: no completed 5-minute bar in 15 minutes (a thin coin at night;
    YFI's first window review on 2026-09-29 had none from 04:45), so the app's own bid in the
    item decides, by the same rules, when it is at most 15 minutes old."""
    flag = decided(flag_item(), [])  # Bid 8.467 at the flag: at or below the line 8.52335.
    assert (flag.decision, flag.reason) == ("EXIT", answers.AT_OR_BELOW_HALF_RISK)
    assert flag.bar is None and flag.numbers["source"] == answers.APP_BID
    words = answers.texts(item_of(flag_item()), flag)["what_changed"]
    assert words.startswith("The app's bid at 02:28:58 UTC was 8.467")
    review = decided(review_item(), [])  # Bid 8.51 at 02:15, 14.5 minutes old: below 8.604.
    assert (review.decision, review.reason) == ("EXIT", answers.BELOW_ENTRY)
    working = review_item()
    working["request"]["trade"]["bid"] = "8.61"
    assert decided(working, []).decision == "CONTINUE"
    body = answers.answer_body(item_of(working), decided(working, []))
    assert answers.body_problems(body, agent_id="muse", now=ANSWER_AT) == []
    # A usable bar still decides first: the 02:20-02:25 close 8.5061, not the bid.
    assert decided(flag_item(), uni_rows()).numbers["source"] == answers.COINBASE_CLOSE


def test_an_app_bid_older_than_15_minutes_is_never_used():
    old = decided(review_item(), [], at=ANSWER_AT + timedelta(minutes=1))  # 15.5 min old.
    assert (old.decision, old.reason) == (None, answers.NO_RECENT_BAR)
    assert "the app's own bid in the item is missing or more than 15 minutes old" in old.detail


@pytest.mark.parametrize("raw, missing", [
    (flag_item(levels={"entry": None}), ["entry"]),
    (flag_item(levels={"stop": "0"}), ["stop"]),
    (flag_item(trade=None), ["entry", "stop"]),
    (review_item(levels={"entry": "not a price"}), ["entry"]),
])
def test_a_missing_level_is_no_usable_data(raw, missing):
    assert answers.missing_levels(item_of(raw)) == missing
    assert answers.missing_levels(item_of(review_item(levels={"stop": None}))) == []


@pytest.mark.parametrize("changes, code", [
    ({"kind": "SOMETHING_NEW"}, "ITEM_KIND_UNKNOWN"),
    ({"flag_id": "not-a-uuid"}, "ITEM_ID_INVALID"),
    ({"answer_route": "/api/v1/lab/research-reports"}, "ITEM_ROUTE_UNEXPECTED"),
    ({"answer_schema": "AGENT_REVIEW_ANSWER_V2"}, "ITEM_ANSWER_SCHEMA_UNEXPECTED"),
])
def test_an_item_the_policy_cannot_read_is_never_answered(changes, code):
    with pytest.raises(answers.ItemUnreadable, match=code):
        answers.parse_item(flag_item(**changes))
    with pytest.raises(answers.ItemUnreadable, match="ITEM_ROUND_UNKNOWN"):
        answers.parse_item(review_item(rnd="FINAL"))


# --- Coinbase's 5-minute candles (market.fetch_candles) --------------------------------------

def test_fetch_candles_reads_one_hour_of_five_minute_candles_and_records_the_read_instant():
    seen = []

    def handle(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=uni_rows())

    client = REAL_CLIENT(transport=httpx.MockTransport(handle))
    doc = answers.read_candles("UNI", now=ANSWER_AT,
                               fetch=lambda coin, **kw: market.fetch_candles(
                                   coin, client=client, sleep=0, **kw))
    [(path, params)] = seen
    assert path == "/products/UNI-USD/candles"
    assert params == {"granularity": "300", "start": "2026-09-29T01:29:30+00:00",
                      "end": "2026-09-29T02:29:30+00:00"}
    assert doc["retrieved_at"] == ANSWER_AT.isoformat() and doc["candles"] == uni_rows()
    bars = market.completed_bars(doc["candles"], 300, retrieved_at=ANSWER_AT)
    assert bars[-1].started_at == datetime(2026, 9, 29, 2, 20, tzinfo=UTC)  # Oldest first.
    assert len(bars) == 11 and bars[0].started_at == datetime(2026, 9, 29, 1, 30, tzinfo=UTC)


def test_fetch_candles_refuses_a_missing_market_and_a_bad_granularity():
    client = REAL_CLIENT(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, json={"message": "NotFound"})))
    with pytest.raises(market.MarketDataError, match="NO_COINBASE_USD_CANDLES"):
        market.fetch_candles("NOPE", seconds=300, start=ANSWER_AT - timedelta(hours=1),
                             end=ANSWER_AT, client=client, sleep=0)
    with pytest.raises(market.MarketDataError, match="UNKNOWN_GRANULARITY"):
        market.fetch_candles("UNI", seconds=120, start=ANSWER_AT - timedelta(hours=1),
                             end=ANSWER_AT, client=client, sleep=0)


def test_completed_bars_counts_a_repeated_row_once_and_drops_the_one_still_forming():
    rows = uni_rows()
    bars = market.completed_bars(rows + rows[:3], 300, retrieved_at=ANSWER_AT)
    assert len(bars) == 11
    assert market.completed_bars(rows, 300, retrieved_at=datetime(
        2026, 9, 29, 2, 30, tzinfo=UTC))[-1].close == D("8.47")  # Complete at 02:30 exactly.


# --- The command -----------------------------------------------------------------------------

def answer(tmp_path, *extra, now=ANSWER_AT, run_dir=None):
    return run.main(["answer", "--base-url", BASE_URL, "--token-file",
                     str(token_file(tmp_path)), *AGENT,
                     "--run-dir", str(run_dir or tmp_path / "answers"),
                     *(["--now", now.isoformat()] if now else []), *extra])


def record_of(tmp_path, key):
    return json.loads((tmp_path / "answers" / "items" / f"{key}.json").read_text())


def polls(tmp_path):
    [path] = (tmp_path / "answers" / "polls").iterdir()
    assert path.name == "2026-09-29.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def assert_no_token_anywhere(tmp_path, captured):
    for path in files_under(tmp_path / "answers"):
        assert FAKE_TOKEN not in path.read_text(), path
    assert FAKE_TOKEN not in captured.out and FAKE_TOKEN not in captured.err


def test_nothing_pending_is_one_get(tmp_path, monkeypatch, capsys):
    app = FakeApp().install(monkeypatch)
    assert answer(tmp_path) == 0
    assert [(r[0], r[2]) for r in app.requests] == [("GET", "/api/v1/lab/reviews")]
    assert app.requests[0][3] == "Bearer " + FAKE_TOKEN  # The token goes in the header only.
    [line] = polls(tmp_path)
    assert (line["pending"], line["items"], line["rules"], line["error"]) == (
        0, [], "MUSE_ANSWER_RULES_V2", None)
    assert not (tmp_path / "answers" / "items").exists()
    captured = capsys.readouterr()
    assert "nothing pending" in captured.out
    assert_no_token_anywhere(tmp_path, captured)


def test_tonights_uni_flag_is_answered_exit_and_recorded(tmp_path, monkeypatch, capsys):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    assert answer(tmp_path) == 0
    [get, candles, post] = app.requests
    assert (get[0], get[2]) == ("GET", "/api/v1/lab/reviews")
    assert (candles[1], candles[2], candles[5]["granularity"]) == (
        COINBASE_HOST, "/products/UNI-USD/candles", "300")
    assert candles[3] is None  # Coinbase never sees the token.
    assert (post[0], post[2], post[3]) == ("POST", FLAG_ROUTE, "Bearer " + FAKE_TOKEN)
    body = post[4]
    assert body["decision"] == "EXIT" and body["answer_id"] == item_of(flag_item()).answer_id
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert (record["state"], record["body"], record["rules"]) == (
        "RECORDED", body, "MUSE_ANSWER_RULES_V2")
    assert record["item"] == flag_item()  # The item as read.
    [evaluation] = record["evaluations"]
    assert evaluation["market"]["candles"] == uni_rows()  # The bars read, as returned.
    assert evaluation["bar"]["started_at"] == "2026-09-29T02:20:00+00:00"
    assert evaluation["bar"]["close"] == "8.5061"
    assert (record["decision"]["decision"], record["decision"]["reason"]) == (
        "EXIT", "CLOSE_AT_OR_BELOW_HALF_RISK")
    assert record["decision"]["detail"] == (
        "5-minute close 8.5061 at or below the half-risk line 8.52335 (entry 8.604, stop "
        "8.4427)")
    [attempt] = record["attempts"]
    assert (attempt["status_code"], attempt["body"]["status"], attempt["state"]) == (
        200, "EXIT_FLAG_ANSWER_RECORDED", "RECORDED")
    [line] = polls(tmp_path)
    assert line["items"][0]["action"] == "ANSWERED" and line["items"][0]["state"] == "RECORDED"
    captured = capsys.readouterr()
    assert "EXIT (CLOSE_AT_OR_BELOW_HALF_RISK" in captured.out and "HTTP 200" in captured.out
    assert_no_token_anywhere(tmp_path, captured)
    # The next run: the answered flag is no longer listed, so one GET and nothing else.
    app.requests.clear()
    assert answer(tmp_path) == 0
    assert [(r[0], r[2]) for r in app.requests] == [("GET", "/api/v1/lab/reviews")]


def test_a_rerun_resends_the_identical_body_and_the_app_replays_it(tmp_path, monkeypatch,
                                                                   capsys):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    app.lose_reply = True  # The app records the answer; its reply never arrives.
    assert answer(tmp_path) == 1
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert record["state"] == "UNKNOWN" and "REVIEW_ANSWER_SUBMIT_CONNECTION_ERROR" in (
        record["attempts"][0]["error"])
    sent = record["body"]
    assert "the next run resends the same body" in capsys.readouterr().err
    # Two minutes later the market has recovered: deciding again would say CONTINUE. The kit
    # never decides again: it resends the recorded body, which the app replays.
    app.lose_reply = False
    app.items = [flag_item()]  # Say the list still shows it (a replica, a race).
    app.rows["UNI"] = rows_closing("8.7", at=ANSWER_AT + timedelta(minutes=2))
    app.requests.clear()
    assert answer(tmp_path, now=ANSWER_AT + timedelta(minutes=2)) == 0
    [get, post] = app.requests  # No candles read: the body was fixed before the first send.
    assert (get[0], post[0], post[4]) == ("GET", "POST", sent)
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert record["state"] == "RECORDED" and record["body"] == sent
    assert [a["kind"] for a in record["attempts"]] == ["SEND", "RESEND"]
    assert record["attempts"][1]["body"]["idempotent_replay"] is True
    assert "a replay: already recorded" in capsys.readouterr().out
    assert len(record["evaluations"]) == 1


def test_a_process_that_died_before_its_post_resends_the_fixed_body(tmp_path, monkeypatch):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    app.fail_before = True
    assert answer(tmp_path) == 1
    fixed = record_of(tmp_path, f"flag-{FLAG_ID}")
    path = tmp_path / "answers" / "items" / f"flag-{FLAG_ID}.json"
    path.write_text(json.dumps({**fixed, "state": "SENDING", "attempts": []}))  # Died mid-POST.
    app.fail_before = False
    app.requests.clear()
    assert answer(tmp_path) == 0
    assert [r[0] for r in app.requests] == ["GET", "POST"]
    assert app.requests[1][4] == fixed["body"] and app.answers[FLAG_ROUTE] == fixed["body"]


@pytest.mark.parametrize("status, detail", [
    (409, "EXIT_FLAG_ALREADY_ANSWERED"), (409, "EXIT_FLAG_ANSWER_WINDOW_CLOSED"),
    (409, "EXIT_FLAG_RESOLVED"), (409, "IDEMPOTENCY_CONTENT_MISMATCH"),
    (404, "EXIT_FLAG_NOT_FOUND"),
])
def test_a_409_or_404_is_final_not_an_error(tmp_path, monkeypatch, capsys, status, detail):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    app.reply = (status, {"detail": detail})
    assert answer(tmp_path) == 0
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert record["state"] == "CLOSED" and record["attempts"][0]["detail"] == detail
    assert f"HTTP {status} {detail}" in capsys.readouterr().out
    app.requests.clear()  # Still listed (say): never sent again, and still not an error.
    assert answer(tmp_path) == 0
    assert [r[0] for r in app.requests] == ["GET"]
    assert "already CLOSED; nothing sent" in capsys.readouterr().out


@pytest.mark.parametrize("status, detail, state, resent", [
    (503, "LAB_DATABASE_UNAVAILABLE", "RETRY", True),  # Nothing stored: sent again.
    (401, "AUTHENTICATION_REQUIRED", "RETRY", True),
    (502, None, "UNKNOWN", True),  # Unknown whether it was recorded: the same body again.
    (409, "EXIT_FLAG_NOT_YET_ASKED", "RETRY", True),
    (422, "ANSWER_TEXT_INVALID", "REFUSED", False),  # Refused: never resent.
    (403, "AGENT_IDENTITY_MISMATCH", "REFUSED", False),
])
def test_other_answers_are_resent_unchanged_or_refused_for_good(tmp_path, monkeypatch, status,
                                                                detail, state, resent):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    app.reply = (status, {"detail": detail} if detail else {})
    assert answer(tmp_path) == 1
    first = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert first["state"] == state
    app.reply = None
    app.requests.clear()
    assert answer(tmp_path) == (0 if resent else 1)
    posts = app.of("POST")
    assert len(posts) == (1 if resent else 0)
    if resent:
        assert posts[0][4] == first["body"]
        assert record_of(tmp_path, f"flag-{FLAG_ID}")["state"] == "RECORDED"


def test_no_usable_data_is_no_answer_and_the_next_run_reads_again(tmp_path, monkeypatch,
                                                                  capsys):
    stale = rows_closing("8.5", age=timedelta(minutes=19, seconds=30))  # Closed 02:10.
    app = FakeApp([stale_quote(flag_item())], rows={"UNI": stale}).install(monkeypatch)
    assert answer(tmp_path) == 0
    assert app.of("POST") == []
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert (record["state"], record["body"]) == ("NO_DATA", None)
    [evaluation] = record["evaluations"]
    assert (evaluation["decision"], evaluation["reason"]) == (
        None, "NO_COMPLETED_5M_BAR_IN_15_MINUTES")
    assert evaluation["market"]["candles"] == stale
    out = capsys.readouterr().out
    assert "no answer: NO_COMPLETED_5M_BAR_IN_15_MINUTES" in out and "fallback applies" in out
    # Two minutes later a fresh bar is there: read again, decided and sent.
    app.rows["UNI"] = uni_rows()
    assert answer(tmp_path) == 0
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert record["state"] == "RECORDED" and len(record["evaluations"]) == 2
    assert app.of("POST")[0][4]["decision"] == "EXIT"


def test_no_coinbase_market_and_a_missing_level_are_no_answer(tmp_path, monkeypatch):
    app = FakeApp([flag_item(symbol="ZZZ/USD"), flag_item(
        str(uuid5(NAMESPACE_URL, "fixture-kit-answers:flag:no-entry")),
        levels={"entry": None})], rows={}).install(monkeypatch)
    assert answer(tmp_path) == 0
    assert app.of("POST") == []
    assert len(app.of("GET", COINBASE_HOST)) == 1  # The missing level needs no candles.
    reasons = sorted(json.loads(path.read_text())["evaluations"][0]["reason"]
                     for path in (tmp_path / "answers" / "items").iterdir())
    assert reasons == ["COINBASE_CANDLES_UNAVAILABLE", "LEVEL_MISSING"]


def test_dry_run_writes_the_answer_without_sending(tmp_path, monkeypatch, capsys):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    assert answer(tmp_path, "--dry-run") == 0
    assert app.of("POST") == [] and len(app.of("GET")) == 2  # The items and the candles.
    record = record_of(tmp_path, f"flag-{FLAG_ID}")
    assert record["state"] == "DRY_RUN" and record["body"]["decision"] == "EXIT"
    assert record["attempts"] == []
    assert "dry run: written, not sent" in capsys.readouterr().out
    assert polls(tmp_path)[0]["dry_run"] is True
    # A real run decides again (nothing was sent under the ID) and sends.
    assert answer(tmp_path) == 0
    assert app.of("POST")[0][4] == record_of(tmp_path, f"flag-{FLAG_ID}")["body"]


def test_a_window_review_and_its_discussion_reply_are_answered_on_their_own_routes(
        tmp_path, monkeypatch):
    app = FakeApp([review_item()], rows={"UNI": rows_closing("8.61")}).install(monkeypatch)
    assert answer(tmp_path) == 0
    [post] = app.of("POST")
    assert post[2] == REVIEW_ROUTE and post[4]["decision"] == "CONTINUE"
    first = record_of(tmp_path, f"review-{REVIEW_ID}-first")
    assert first["state"] == "RECORDED" and first["round"] == "FIRST"
    # Jev disagreed: the discussion item comes back; the reply is decided on the latest bar.
    app.items = [review_item(rnd="DISCUSSION", your_first_answer=post[4],
                             jev_first_answer={"answers": {"decision": {"choice": "EXIT"}}})]
    app.answers.clear()
    later = ANSWER_AT + timedelta(minutes=5)
    app.rows["UNI"] = rows_closing("8.59", at=later)
    assert answer(tmp_path, now=later) == 0
    reply = app.of("POST")[-1][4]
    assert reply["decision"] == "EXIT" and reply["answer_id"] != post[4]["answer_id"]
    assert record_of(tmp_path, f"review-{REVIEW_ID}-discussion")["state"] == "RECORDED"
    assert record_of(tmp_path, f"review-{REVIEW_ID}-first")["body"] == post[4]  # Unchanged.


def test_an_unreadable_item_is_never_answered_and_says_so(tmp_path, monkeypatch, capsys):
    app = FakeApp([flag_item(answer_route="/api/v1/lab/research-reports")],
                  rows={"UNI": uni_rows()}).install(monkeypatch)
    assert answer(tmp_path) == 1
    assert app.of("POST") == [] and app.of("GET", COINBASE_HOST) == []
    assert "not answered: ITEM_ROUTE_UNEXPECTED" in capsys.readouterr().err
    assert polls(tmp_path)[0]["items"][0]["action"] == "UNREADABLE"


def test_an_unreadable_record_is_left_alone(tmp_path, monkeypatch, capsys):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    items = tmp_path / "answers" / "items"
    items.mkdir(parents=True)
    (items / f"flag-{FLAG_ID}.json").write_text("{not json")
    assert answer(tmp_path) == 1
    assert app.of("POST") == []
    assert "left alone: ANSWER_RECORD_UNREADABLE" in capsys.readouterr().err


def test_a_run_finding_the_lock_held_does_nothing(tmp_path, monkeypatch, capsys):
    app = FakeApp([flag_item()], rows={"UNI": uni_rows()}).install(monkeypatch)
    with answers.run_lock(tmp_path / "answers") as held:
        assert held
        assert answer(tmp_path) == 0
    assert app.requests == []
    assert "another answer run holds" in capsys.readouterr().out
    assert answer(tmp_path) == 0 and len(app.of("POST")) == 1  # Released afterwards.


def test_a_failed_read_of_the_pending_items_answers_nothing(tmp_path, monkeypatch, capsys):
    def handle(request):
        return httpx.Response(401, json={"detail": "AUTHENTICATION_REQUIRED"})

    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handle)))
    assert answer(tmp_path) == 1
    assert "REVIEWS_HTTP_401: AUTHENTICATION_REQUIRED" in capsys.readouterr().err
    assert polls(tmp_path)[0]["error"] == "REVIEWS_HTTP_401: AUTHENTICATION_REQUIRED"


def test_the_command_refuses_a_missing_run_dir_a_bad_agent_and_an_unsafe_url(
        tmp_path, monkeypatch, capsys):
    app = FakeApp().install(monkeypatch)
    token = str(token_file(tmp_path))
    assert run.main(["answer", "--base-url", BASE_URL, "--token-file", token, *AGENT]) == 2
    assert "answer needs --run-dir" in capsys.readouterr().err
    assert run.main(["answer", "--base-url", BASE_URL, "--token-file", token,
                     "--agent-id", "Muse!", "--agent-version", "v1",
                     "--run-dir", str(tmp_path / "a")]) == 2
    assert "AGENT_ID_INVALID" in capsys.readouterr().err
    assert run.main(["answer", "--base-url", "http://trader.up.railway.app", "--token-file",
                     token, *AGENT, "--run-dir", str(tmp_path / "b")]) == 2
    assert "RESEARCH_AGENT_BASE_URL_REFUSED" in capsys.readouterr().err
    assert app.requests == [] and not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


# --- The procedure and the prompt ------------------------------------------------------------

def test_the_procedure_gives_the_answer_command_as_the_kit_parses_it():
    text = section(PROCEDURE, "## Answering reviews and exit flags")
    parser = run.build_parser()
    parsed = [parser.parse_args(argv) for argv in commands(text)]
    assert {args.command for args in parsed} == {"answer"}
    assert any(args.dry_run for args in parsed) and any(not args.dry_run for args in parsed)
    for args in parsed:
        assert args.run_dir == "runs/answers" and args.base_url == "x"
        assert args.agent_id == "muse" and args.token_file == "x"
    for words in ("MUSE_ANSWER_RULES_V2", "entry - 0.5 x (entry - stop)", "15 minutes",
                  "NO_COMPLETED_5M_BAR_IN_15_MINUTES", "8.5061", "every 2-5 minutes"):
        assert words in text, words
    assert "Answering reviews and exit flags" in PROMPTS
