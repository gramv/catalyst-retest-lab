"""CRYPTO_MAINTENANCE_V5 on the fixture venue (package jev-b1): the 15-minute cadence with the
V6 questions, confirm mode, streaks across real reviews (retries, same-bar re-asks, uncertain,
failures), the two flag paths and their unanswered defaults through the early-exit agreement
(and the risk gate's one-use authorizations for the sell), the immediate triggers (news, the
hour, a 3% Bitcoin move), the spend guard, a V4 trade beside a V5 one, the window review's
history, and one end-to-end managed runtime session with a fake Jev.

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars, a
mock Jev transport and the real agent routes in process. No broker, provider, network or
owner-ledger contact; no real TypeSafe call.
"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import day_review as dr
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import JEV_MODEL, encoded
from catalyst_lab.managed_dossier import encoded_bytes
from catalyst_lab.unchanged_plan import maintenance_exit_changes
from tests.day_review_fixtures import (
    AGENTS,
    ReviewJev,
    answer_body,
    at_request,
    at_review,
    bearer,
    decisions,
    events,
    kit,
    managed_arm,
    move_to,
    open_trade,
    pending,
    post_news,
    run,
    sell_to_close,
    state,
)
from tests.maintenance_fixtures import maintainer
from tests.maintenance_fixtures import mt as mt
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

pytestmark = pytest.mark.usefixtures("managed_arm")
_ = managed_arm
DECISION = "MAINTENANCE_DECISION"


class V6Jev(ReviewJev):
    """``ReviewJev`` plus the V6 maintenance questions: each answer is a Noul with the
    probability in ``p`` (or the next entry of ``queue``); ``status["V6"]`` other than 200
    answers with that HTTP status."""

    def __init__(self):
        super().__init__()
        self.p = {"invalidation_met": 0.05, "news_contradicts": 0.05}
        self.queue = []

    def __call__(self, request):
        body = json.loads(request.content)
        if "invalidation_met" not in body["questions"]:
            return super().__call__(request)
        self.calls.append(("V6", body))
        status = self.status.get("V6", 200) if isinstance(self.status, dict) else self.status
        if status != 200:
            return httpx.Response(status, json={"error": "fixture unavailable"})
        script = self.queue.pop(0) if self.queue else self.p
        answers = {name: {"type": "noul", "noul": script[name]} for name in body["questions"]}
        return httpx.Response(200, json={"model": JEV_MODEL, "answers": answers,
                                         "usage": {"input_tokens": 900, "output_tokens": 4}})


def v5_kit(mt, **options):
    return kit(mt, jev=V6Jev(), **options)


def maintain(mt, k, *, bid="101", benchmark=None, symbol="SOL/USD"):
    k.prices.set(symbol, bid)
    return asyncio.run(k.maintenance.run_pass(mt[0].store.active(), k.prices,
                                              benchmark=benchmark,
                                              benchmark_ready=benchmark is not None))


def to_bar(mt, seconds, plus=1):
    """Just past the next completed bar of ``seconds``."""
    now = mt[1].now
    move_to(mt, cm.floor_time(now, seconds) + timedelta(seconds=seconds + plus))


def later(mt, seconds):
    move_to(mt, mt[1].now + timedelta(seconds=seconds))


def v6_calls(k):
    return k.jev.of("V6")


def last_decision(engine, sid):
    return events(engine, DECISION, sid)[-1]


def effect(body, name="invalidation_met"):
    return (body["verdicts"][name]["effect"], body["verdicts"][name]["streak"])


def confirm_invalidation(mt, k, sid):
    """Three counted yeses: the 15-minute bar, then the next two completed 5-minute bars."""
    k.jev.p = {"invalidation_met": 0.9, "news_contradicts": 0.05}
    to_bar(mt, 900)
    maintain(mt, k)
    assert effect(last_decision(mt[0], sid)) == ("COUNTED", 1)
    later(mt, 61)
    maintain(mt, k)  # Inside the same 5-minute bar: not due.
    assert len(events(mt[0], DECISION, sid)) == 1
    for expected in (("COUNTED", 2), ("CONFIRMED", 0)):
        to_bar(mt, 300)
        maintain(mt, k)
        assert "CONFIRM_5M" in last_decision(mt[0], sid)["trigger_reasons"]
        assert effect(last_decision(mt[0], sid)) == expected
    body = last_decision(mt[0], sid)
    assert effect(body) == ("CONFIRMED", 0) and body["outcome"] == "FLAGGED"
    [flag] = events(mt[0], dr.FLAG_RAISED, sid)
    return body, flag


# --- Cadence and what Jev reads ---------------------------------------------------------------


def test_a_v5_trade_is_reviewed_at_completed_15_minute_bars_with_two_narrow_questions(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    assert state(mt, sid)["maintenance_policy"] == cm.CRYPTO_MAINTENANCE_V5.record()
    k = v5_kit(mt)
    maintain(mt, k)
    later(mt, 61)
    maintain(mt, k, bid="105.30")  # +1R: the milestone is recorded but is not a V5 reason.
    assert [t["trigger"] for t in events(engine, cm.TRIGGER_EVENT, sid)] == ["R_MILESTONE"]
    assert v6_calls(k) == [] and events(engine, DECISION, sid) == []
    to_bar(mt, 900)
    maintain(mt, k)
    [sent] = v6_calls(k)
    assert list(sent["questions"]) == ["invalidation_met"]  # No news: one question.
    assert sent["questions"]["invalidation_met"]["type"] == "noul"
    state_sent = sent["state"]
    assert state_sent["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V6"
    assert set(state_sent) == {"context_version", "symbol", "as_of_bar_end", "pick", "observed",
                               "news_new"}
    assert state_sent["pick"]["disproof"] == "INVALIDATION-00: an hourly close below 95."
    assert "last completed 1-hour close below 95: no" in state_sent["observed"][
        "disproof_checks"]
    assert encoded_bytes(state_sent) <= 3072
    assert "claude" not in json.dumps(sent).lower()  # Jev never sees the agent.
    [request] = events(engine, "POSITION_REVIEW_REQUEST", sid)
    assert "BAR_15M" in request["trigger"]["reasons"]
    assert request["trigger"]["questions"] == ["invalidation_met"]
    assert request["context"]["identity"]["spend_guard"]["routine_weight"] == 1
    assert request["context"]["identity"]["maintenance_policy_id"] == "CRYPTO_MAINTENANCE_V5"
    body = last_decision(engine, sid)
    assert (body["outcome"], body["action"], body["code"], body["policy_id"],
            body["answer_rule"]) == ("HELD", "HOLD", None, "CRYPTO_MAINTENANCE_V5",
                                     "MAINTENANCE_ANSWER_RULE_V3")
    # The calibration record: probability, verdict, context hash, state hash and the bar.
    assert body["answers"] == {"invalidation_met": {"p": "0.05", "verdict": "NO"}}
    assert effect(body) == ("RESET", 0)
    assert body["context_hash"] == request["context_hash"]
    assert body["state_hash"] == request["context"]["basis"]["state_hash"]
    assert body["asked_bar_end"] == state_sent["as_of_bar_end"] == cm.floor_time(
        venue.now, 300).isoformat()
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    # No per-minute and no 5-minute review without a running streak.
    later(mt, 61)
    maintain(mt, k)
    to_bar(mt, 300)
    maintain(mt, k)
    assert len(v6_calls(k)) == 1
    with engine.repo.connect() as conn:
        [jev] = conn.execute("""SELECT question_set_version FROM lab.jev_requests
            WHERE question_set_version LIKE 'JEV_MANAGED_POSITION_QUESTIONS_%'""").fetchall()
    assert jev["question_set_version"] == "JEV_MANAGED_POSITION_QUESTIONS_V6"


def test_a_completed_hour_is_a_reason_and_a_restart_resumes_the_cadence(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    to_bar(mt, 3600)
    maintain(mt, k)
    reasons = last_decision(engine, sid)["trigger_reasons"]
    assert reasons == ["BAR_15M", "BAR_1H"]
    fresh = v5_kit(mt)  # A new process reads the served bars from the ledger.
    later(mt, 61)
    maintain(mt, fresh)
    assert v6_calls(fresh) == []


# --- Streaks across reviews -------------------------------------------------------------------


def test_three_yeses_confirm_an_invalidation_and_an_unanswered_flag_exits_at_its_deadline(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    body, flag = confirm_invalidation(mt, k, sid)
    assert body["action"] == "FLAG_EARLY_EXIT"
    assert [f["question"] for f in body["flags"]] == ["invalidation_met"]
    assert flag["side"] == "JEV" and flag["reasons"]["question"] == "invalidation_met"
    assert flag["reasons"]["if_unanswered"] == "EXIT"
    assert flag["evidence"]["observed"]["disproof_checks"]
    assert (state(mt, sid)["stop"], state(mt, sid)["target"]) == ("95", "111")
    assert not state(mt, sid).get("exit_requested")  # A flag never changes the trade.
    run(mt, k, bid="101")  # The agent is asked at once.
    [asked] = events(engine, dr.FLAG_ASKED, sid)
    assert asked["if_unanswered"] == "EXIT"
    [item] = [i for i in pending(k) if i["kind"] == "EXIT_FLAG"]
    assert item["jev_reasons"]["question"] == "invalidation_met"
    assert item["jev_reasons"]["if_unanswered"] == "EXIT"
    move_to(mt, venue.now + timedelta(minutes=14))
    run(mt, k, bid="101")
    assert not state(mt, sid).get("exit_requested")  # Not yet due.
    move_to(mt, venue.now + timedelta(minutes=1, seconds=1))
    run(mt, k, bid="101")
    [resolved] = events(engine, dr.FLAG_RESOLVED, sid)
    assert (resolved["outcome"], resolved["exit_requested"]) == (
        "NO_ANSWER_EXIT", "EARLY_EXIT_AGREED")
    [decision] = events(engine, dr.EARLY_EXIT_DECISION, sid)
    assert decision["outcome"] == "NO_ANSWER_EXIT"
    assert decision["exit_requested"] == "EARLY_EXIT_AGREED"
    assert decision["measurement"]["actually_exited"] is True
    assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"
    sell_to_close(mt, sid, "SOL/USD", "101")
    assert state(mt, sid)["state"] == "CLOSED"
    # Every broker change went through its own one-use authorization.
    exits = [d for d in decisions(engine, sid) if d["action"] in ("CANCEL", "EXIT")]
    assert exits and all(d["outcome"] == "APPROVED" and d["claimed"] for d in exits)
    [change] = maintenance_exit_changes(engine.repo, sid)  # The replay sees the exit.
    assert change.actually_exited and change.old_stop == D("95")


def test_the_agent_can_keep_a_confirmed_invalidation_and_without_an_agent_it_exits_at_once(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    _, flag = confirm_invalidation(mt, k, sid)
    run(mt, k, bid="101")
    reply = k.client.post(f"/api/v1/lab/exit-flags/{flag['flag_id']}/answer",
                          json=answer_body("CONTINUE"), headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 200, reply.text
    run(mt, k, bid="101")
    [resolved] = events(engine, dr.FLAG_RESOLVED, sid)
    assert resolved["outcome"] == "EXIT_NOT_AGREED" and not state(mt, sid).get(
        "exit_requested")


def test_with_no_agent_to_ask_a_confirmed_invalidation_exits_at_once(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt, agents=frozenset())
    confirm_invalidation(mt, k, sid)
    run(mt, k, bid="101")
    [resolved] = events(engine, dr.FLAG_RESOLVED, sid)
    assert (resolved["outcome"], resolved["answered_by"]["code"]) == (
        "NO_ANSWER_EXIT", "NO_ACTIVE_AGENT")
    assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"


def test_a_same_bar_re_ask_never_counts_and_a_no_resets(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    k.jev.p = {"invalidation_met": 0.9, "news_contradicts": 0.05}
    to_bar(mt, 900)
    maintain(mt, k)
    assert effect(last_decision(engine, sid)) == ("COUNTED", 1)
    later(mt, 61)  # Same 5-minute bar: news makes a review due at once.
    post_news(mt, sid, "Synthetic fixture: the coin's exchange listed a new pair.",
              stance="SUPPORTS")
    maintain(mt, k)
    body = last_decision(engine, sid)
    assert body["trigger_reasons"] == ["AGENT_NEWS"]
    assert body["questions"] == ["invalidation_met", "news_contradicts"]
    assert effect(body) == ("NOT_COUNTED_SAME_BAR", 1)
    k.jev.p = {"invalidation_met": 0.1, "news_contradicts": 0.05}
    to_bar(mt, 300)
    maintain(mt, k)  # Confirm mode: the NO resets.
    body = last_decision(engine, sid)
    assert effect(body) == ("RESET", 0) and body["action"] == "HOLD"
    assert body["questions"] == ["invalidation_met"]  # The news was answered NO: settled.
    to_bar(mt, 300)
    maintain(mt, k)  # No running streak: no confirm review.
    assert len(events(engine, DECISION, sid)) == 3


def test_uncertain_and_failed_reviews_neither_add_nor_reset(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    k.jev.queue = [{"invalidation_met": 0.9}, {"invalidation_met": 0.55},
                   {"invalidation_met": 0.9}, {"invalidation_met": 0.85}]
    to_bar(mt, 900)
    first = cm.floor_time(venue.now, 900)
    if first.minute == 45:  # Keep the walk inside one hour (no BAR_1H reason to reason about).
        to_bar(mt, 900)
    maintain(mt, k)
    assert effect(last_decision(engine, sid)) == ("COUNTED", 1)
    k.jev.status = {"V6": 503}
    to_bar(mt, 300)
    maintain(mt, k)
    failed = last_decision(engine, sid)
    assert (failed["outcome"], failed.get("verdicts")) == ("FAILED", None)
    k.jev.status = 200
    to_bar(mt, 300)
    maintain(mt, k)  # The second confirm bar: uncertain.
    body = last_decision(engine, sid)
    assert effect(body) == ("NEUTRAL", 1) and body["action"] == "CONFIRMING"
    to_bar(mt, 300)  # The next 15-minute bar (confirm mode is over): a yes counts.
    maintain(mt, k)
    body = last_decision(engine, sid)
    assert "BAR_15M" in body["trigger_reasons"] and effect(body) == ("COUNTED", 2)
    to_bar(mt, 300)
    maintain(mt, k)
    assert effect(last_decision(engine, sid)) == ("CONFIRMED", 0)


# --- News ---------------------------------------------------------------------------------------


def test_contradicting_news_confirmed_flags_to_the_agent_and_unanswered_keeps_the_trade(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    to_bar(mt, 900)
    maintain(mt, k)  # A routine review records the news revision served.
    later(mt, 61)
    post_news(mt, sid, "Synthetic fixture: the exchange delisted the coin's main pair.")
    k.jev.p = {"invalidation_met": 0.05, "news_contradicts": 0.92}
    maintain(mt, k)
    body = last_decision(engine, sid)
    assert body["trigger_reasons"] == ["AGENT_NEWS"]
    [news_id] = body["news_shown"]
    assert effect(body, "news_contradicts") == ("COUNTED", 1)
    sent = v6_calls(k)[-1]
    [item] = sent["state"]["news_new"]
    assert item["id"] == news_id and item["stance"] == "ADVERSE"
    for _ in range(2):  # The unsettled item is asked again on the confirm bars.
        to_bar(mt, 300)
        maintain(mt, k)
        assert last_decision(engine, sid)["news_shown"] == [news_id]
    body = last_decision(engine, sid)
    assert effect(body, "news_contradicts") == ("CONFIRMED", 0)
    assert body["outcome"] == "FLAGGED"
    [flag] = events(engine, dr.FLAG_RAISED, sid)
    assert (flag["reasons"]["question"], flag["reasons"]["if_unanswered"]) == (
        "news_contradicts", "KEEP")
    assert flag["evidence"]["news_ids"] == [news_id]
    run(mt, k, bid="101")
    move_to(mt, venue.now + timedelta(minutes=15, seconds=1))
    run(mt, k, bid="101")
    [resolved] = events(engine, dr.FLAG_RESOLVED, sid)
    assert resolved["outcome"] == "NO_ANSWER_IN_TIME"
    assert state(mt, sid)["state"] == "OPEN" and not state(mt, sid).get("exit_requested")
    # The item is settled by the confirmation: the next review asks only the invalidation.
    to_bar(mt, 900)
    maintain(mt, k)
    assert list(v6_calls(k)[-1]["questions"]) == ["invalidation_met"]
    assert v6_calls(k)[-1]["state"]["news_new"] == []


def test_news_answered_no_is_settled_and_not_asked_again(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    to_bar(mt, 900)
    maintain(mt, k)
    later(mt, 61)
    post_news(mt, sid, "Synthetic fixture: a market maker joined the coin's order book.",
              stance="SUPPORTS")
    maintain(mt, k)
    assert last_decision(engine, sid)["answers"]["news_contradicts"]["verdict"] == "NO"
    to_bar(mt, 900)
    maintain(mt, k)
    assert list(v6_calls(k)[-1]["questions"]) == ["invalidation_met"]


# --- Bitcoin, the budget, older versions --------------------------------------------------------


def test_a_3_percent_bitcoin_move_since_the_last_review_reviews_at_once(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    window = cm.BenchmarkWindow()
    to_bar(mt, 900)
    window.observe(D("61000"), venue.now)
    maintain(mt, k, benchmark=window)
    [request] = events(engine, "POSITION_REVIEW_REQUEST", sid)
    assert request["trigger"]["served"]["btc_reference"]["price"] == "61000"
    later(mt, 90)
    window.observe(D("62800"), venue.now)  # +2.95%: not yet.
    maintain(mt, k, benchmark=window)
    assert len(v6_calls(k)) == 1
    later(mt, 30)
    window.observe(D("62830"), venue.now)  # +3.0%.
    maintain(mt, k, benchmark=window)
    assert last_decision(engine, sid)["trigger_reasons"] == ["BTC_MOVE"]


def test_the_spend_guard_still_withholds_v5_reviews(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit_ = maintainer(mt, V6Jev(), guard=None)  # No guard: it cannot decide.
    to_bar(mt, 900)
    kit_.prices.set("SOL/USD", "101")
    asyncio.run(kit_.maintenance.run_pass(engine.store.active(), kit_.prices))
    assert kit_.jev.calls == []
    [skipped] = events(engine, "POSITION_REVIEW_SKIPPED", sid)
    assert skipped["reason"] == "JEV_BUDGET_UNAVAILABLE"
    assert skipped["policy_id"] == "CRYPTO_MAINTENANCE_V5"


def test_a_v4_trade_keeps_its_minute_cadence_and_action_questions_beside_a_v5_trade(
        mt, monkeypatch):
    engine, venue, _ = mt
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V4)
    older = open_trade(mt, "ETH/USD")
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V5)
    newer = open_trade(mt, "SOL/USD")
    assert state(mt, older)["maintenance_policy"]["policy_id"] == "CRYPTO_MAINTENANCE_V4"
    k = v5_kit(mt)
    k.prices.set("ETH/USD", "101")
    to_bar(mt, 900)
    maintain(mt, k)
    assert len(k.jev.of("MAINTENANCE")) == 1 and len(v6_calls(k)) == 1
    [v4_sent] = k.jev.of("MAINTENANCE")
    assert set(v4_sent["questions"]) == {"trade_reason", "action", "stop_option",
                                         "target_option"}
    assert v4_sent["state"]["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V5"
    later(mt, 61)
    maintain(mt, k)
    assert len(k.jev.of("MAINTENANCE")) == 2 and len(v6_calls(k)) == 1  # V4: every minute.
    assert last_decision(engine, older)["policy_id"] == "CRYPTO_MAINTENANCE_V4"
    assert last_decision(engine, newer)["policy_id"] == "CRYPTO_MAINTENANCE_V5"


# --- The window review ------------------------------------------------------------------------


def test_the_window_review_runs_on_a_v5_trade_and_its_history_shows_the_v5_verdicts(mt):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    to_bar(mt, 900)
    maintain(mt, k)
    at_request(mt, sid)
    run(mt, k, bid="101")
    at_review(mt, sid)
    run(mt, k, bid="101")
    [sent] = k.jev.of("DAY_REVIEW")
    [row] = sent["state"]["review_history"]
    assert row["invalidation_met"] == {"p": "0.05", "verdict": "NO"}
    assert row["action"] is None  # V5 has no action question.
    [decision] = events(engine, dr.DECISION, sid)
    assert decision["outcome"] and decision["review_id"]


# --- One managed runtime session ----------------------------------------------------------------


def test_a_managed_runtime_session_reviews_confirms_flags_and_exits_a_v5_trade(mt):
    from tests.test_maintenance_runtime import runtime

    engine, venue, _ = mt
    sid = open_trade(mt)
    k = v5_kit(mt)
    k.jev.p = {"invalidation_met": 0.95, "news_contradicts": 0.05}
    rt = runtime(mt, maintenance=k.maintenance, day_reviews=k.day)
    rt.connected = rt.research_healthy = True
    assert rt.reconcile_once()
    rt.market_connected["CRYPTO"] = True
    rt.market_subscriptions["CRYPTO"] = {"SOL/USD", "BTC/USD"}
    serial = iter(range(100, 10_000))

    def tick(bid="101"):
        at = venue.now.isoformat()
        rt.market_message("CRYPTO", {"T": "q", "S": "SOL/USD", "bp": bid,
                                     "ap": str(D(bid) + D("0.03")), "t": at})
        rt.market_message("CRYPTO", {"T": "t", "S": "SOL/USD", "p": bid, "t": at,
                                     "i": next(serial)})
        rt.market_message("CRYPTO", {"T": "q", "S": "BTC/USD", "bp": "61000", "ap": "61010",
                                     "t": at})

    for seconds in (900, 300, 300):
        to_bar(mt, seconds)
        tick()
        asyncio.run(rt._position_pass())
    assert [effect(b)[0] for b in events(engine, DECISION, sid)] == [
        "COUNTED", "COUNTED", "CONFIRMED"]
    request = events(engine, "POSITION_REVIEW_REQUEST", sid)[0]
    assert request["trigger"]["served"]["btc_reference"]["price"] == "61005"
    asyncio.run(rt._day_review_pass())  # The agent is asked; it never answers.
    move_to(mt, venue.now + timedelta(minutes=15, seconds=1))
    tick()
    asyncio.run(rt._day_review_pass())
    assert state(mt, sid)["exit_requested"] == "EARLY_EXIT_AGREED"
    for _ in range(3):
        tick()
        rt.execution_once()
    [sell] = [o for o in venue.orders.values() if o["side"] == "sell" and o["type"] == "market"]
    engine.ingest(venue.fill(sell["id"], sell["qty"], price="101"))
    tick()
    rt.execution_once()
    assert state(mt, sid)["state"] == "CLOSED" and rt.error is None
    assert verify_events(engine.repo.export_events())["valid"]
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]
    # Cost: every review's request is small (state <= 3,072 bytes; the whole request well
    # under 5 KB), and there were three in the session.
    sizes = [len(encoded({"model": JEV_MODEL, "state": b["state"],
                          "questions": b["questions"]})) for b in v6_calls(k)]
    assert len(sizes) == 3 and max(sizes) < 5000
