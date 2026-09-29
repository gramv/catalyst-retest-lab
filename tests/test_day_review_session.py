"""The supervised session tool runs 24-hour reviews and early exits (package day-review).

``execute --simulate-prints --hold-open`` keeps a maintained report-V3 crypto trade open,
``review`` moves the session's review clock and runs the day reviews, and the agent answers
over its own routes with its token, as a supervised session would with ``reviews``,
``review-answer``, ``flag-answer`` and ``exit-flag``. Fixture evidence only: the scripted mock
Jev transport and the SESSION_SIMULATION paper venue, in-process on disposable template-clone
databases. ``--jev typesafe`` uses the same path with the real model and is run only by the
coordinator or the owner.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab.account_risk import JEV_MANAGED_ARM
from catalyst_lab.audit import verify_events
from tests.test_agent_research_session import make_session as make_session
from tests.test_agent_research_session import session_script, submit
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster


def report(now, symbols=("SOL/USD", "XRP/USD")):
    from tests.test_research_report_v3 import pick, report_v3

    return report_v3([pick(i, symbol, kind="BOTH", now=now) for i, symbol in enumerate(symbols)],
                     now=now, agent_id=session_script.AGENT_ID, skipped=[])


def agent(session):
    client = TestClient(session.app)
    client.headers["Authorization"] = "Bearer " + session.tokens[session_script.AGENT_ID]
    return client


def answer(decision, **changes):
    return {"schema_version": "AGENT_REVIEW_ANSWER_V1", "answer_id": str(uuid4()),
            "decision": decision, "what_changed": "Session fixture: volume held above average.",
            "next_24h": "Session fixture: a retest of the recent high.",
            "proves_wrong": "Session fixture: an hourly close below the entry.", **changes}


def open_session(make_session, monkeypatch, script, symbols=("SOL/USD", "XRP/USD")):
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda setup_id, pct: JEV_MANAGED_ARM)
    session = make_session(selection_rule="TOPK", topk_k=5,
                           fixture=session_script.FixtureScript(script))
    submit(session, report(datetime.now(UTC), symbols))
    session.tick()
    executed = session.execute(simulate_prints=True, hold_open=True)
    lives = {life["symbol"]: life for life in executed["lifecycles"]}
    assert all(life["held_open"] and life["final_state"] == "OPEN" for life in lives.values())
    assert "held open" in session_script.render_execute(json.loads(json.dumps(
        executed, default=str)))
    return session, lives


def by_symbol(review):
    return {trade["symbol"]: trade for trade in review["trades"]}


def test_a_session_runs_the_24_hour_review_on_its_own_clock(make_session, monkeypatch,
                                                              tmp_path):
    session, lives = open_session(make_session, monkeypatch, {
        "SOL/USD": {"day_review": {"decision": "CONTINUE", "stop_option": "first"}},
        "XRP/USD": {"day_review": {"decision": "EXIT", "trade_reason": "WEAKENED"}},
    })
    config = session._configuration()
    assert config["day_review_policy"]["policy_id"] == "CRYPTO_24H_REVIEW_V2"  # answer-rules
    assert config["early_exit_policy"]["policy_id"] == "EARLY_EXIT_AGREEMENT_V1"
    with pytest.raises(session_script.SessionRefused, match="REVIEW_MINUTES_INVALID"):
        session.review(minutes=0)
    requested = session.review(at="request")
    trades = by_symbol(requested)
    assert all(t["requests"][0]["addressee"] == {"agent_id": "fable",
                                                 "basis": "PROPOSING_AGENT"}
               for t in trades.values())
    assert sorted(item["symbol"] for item in requested["pending"]) == ["SOL/USD", "XRP/USD"]
    client = agent(session)
    listed = client.get("/api/v1/lab/reviews").json()["items"]
    assert {item["round"] for item in listed} == {"FIRST"}
    for item in listed:
        decision = "CONTINUE" if item["symbol"] == "SOL/USD" else "EXIT"
        reply = client.post(item["answer_route"], json=answer(decision))
        assert reply.status_code == 200, reply.text
    reviewed = session.review(at="review")
    trades = by_symbol(reviewed)
    sol, xrp = trades["SOL/USD"], trades["XRP/USD"]
    [sol_decision] = sol["decisions"]
    assert (sol_decision["outcome"], sol_decision["code"], sol_decision["level_change"]) == (
        "CONTINUE", "AGREED", "APPLIED")
    assert sol["continuations"] == 1 and sol["acted"]["stop_replaced"]["path"] == (
        "PATCH_REPLACE")
    [xrp_decision] = xrp["decisions"]
    assert (xrp_decision["outcome"], xrp_decision["code"]) == ("EXIT", "AGREED")
    assert xrp["acted"]["exit"]["state"] == "CLOSED"
    assert xrp["acted"]["exit"]["reason"] == "DAY_REVIEW_EXIT"
    assert xrp["acted"]["exit"]["residual"]["zero"]
    # A reconciliation after the review's exit, as ``execute`` records after a target exit.
    assert xrp["acted"]["exit"]["reconciliation_after_exit"]["clean"]
    assert reviewed["jev_calls_used"] == 2
    assert sorted(c for c in session.fixture.calls if c.startswith("DAY_REVIEW")) == [
        "DAY_REVIEW:SOL/USD", "DAY_REVIEW:XRP/USD"]
    text = session_script.render_review(json.loads(json.dumps(reviewed, default=str)))
    assert "CONTINUE AGREED" in text and "EXIT AGREED" in text and "DAY_REVIEW_EXIT" in text
    assert "DAY_REVIEW_EXIT  CLOSED  reconciliation clean" in text
    manifest = session.export(tmp_path / "export")
    assert manifest["audit"]["valid"]
    # Only the check a session can never pass (it has no market stream) fails for the exit.
    assert manifest["acceptance_failed_checks"][str(lives["XRP/USD"]["setup_id"])] == [
        "SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER"]
    events = json.loads((tmp_path / "export" / "events.json").read_text())
    assert verify_events(events["events"])["valid"]


def test_a_session_runs_the_discussion_round_and_an_agent_early_exit(make_session, monkeypatch):
    session, lives = open_session(make_session, monkeypatch, {
        "SOL/USD": {"day_review": {"decision": "EXIT", "trade_reason": "WEAKENED",
                                   "agent_case": "DOES_NOT_HOLD"},
                    "day_review_final": {"decision": "CONTINUE", "trade_reason": "INTACT",
                                         "agent_case": "HOLDS"}},
        "XRP/USD": {"early_exit": {"exit_now": "EXIT"}},
    })
    client = agent(session)
    xrp = lives["XRP/USD"]["setup_id"]
    lifecycle = client.get(f"/api/v1/lab/positions/{xrp}/news").json()["lifecycle_id"]
    flag = client.post(f"/api/v1/lab/positions/{xrp}/exit-flag", json={
        "schema_version": "AGENT_EXIT_FLAG_V1", "flag_ref": str(uuid4()),
        "lifecycle_id": lifecycle,
        "what_changed": "Session fixture: the main exchange paused withdrawals.",
        "next_24h": "Session fixture: selling pressure while they stay paused.",
        "proves_wrong": "Session fixture: withdrawals resume."})
    assert flag.status_code == 200, flag.text
    flagged = by_symbol(session.review())["XRP/USD"]  # Jev is asked at once.
    assert flagged["early_exit_decisions"][0]["outcome"] == "EXIT_AGREED"
    assert flagged["acted"]["exit"]["reason"] == "EARLY_EXIT_AGREED"
    assert flagged["acted"]["exit"]["reconciliation_after_exit"]["clean"]
    session.review(at="request")
    [item] = [i for i in client.get("/api/v1/lab/reviews").json()["items"]
              if i["symbol"] == "SOL/USD"]
    assert client.post(item["answer_route"], json=answer("CONTINUE")).status_code == 200
    first = session.review(at="review")
    trades = by_symbol(first)
    assert list(trades) == ["SOL/USD"]  # XRP/USD has closed.
    assert trades["SOL/USD"]["discussions"] and not trades["SOL/USD"]["decisions"]
    [discussion] = [i for i in client.get("/api/v1/lab/reviews").json()["items"]
                    if i["round"] == "DISCUSSION"]
    assert discussion["jev_first_answer"]["answers"]["decision"]["choice"] == "EXIT"
    reply = client.post(discussion["answer_route"], json=answer(
        "CONTINUE", what_changed="Session fixture: the drop was one venue's outage."))
    assert reply.status_code == 200 and reply.json()["round"] == "DISCUSSION"
    final = by_symbol(session.review(minutes=1))["SOL/USD"]
    [decision] = final["decisions"]
    assert (decision["outcome"], decision["code"]) == ("CONTINUE", "AGREED_AFTER_DISCUSSION")
    assert [r["round"] for r in final["jev_results"]] == ["FIRST", "FINAL"]
    assert sorted(c for c in session.fixture.calls if not c.startswith(
        ("TOPK", "QUALITY", "MAINTENANCE"))) == [
        "DAY_REVIEW:SOL/USD", "DAY_REVIEW:SOL/USD", "EARLY_EXIT:XRP/USD"]


def test_the_fixture_script_answers_only_valid_review_labels():
    script = session_script.FixtureScript
    for broken in ({"SOL/USD": {"day_review": {"decision": "HOLD"}}},
                   {"SOL/USD": {"day_review": {"stop_option": "S9"}}},
                   {"SOL/USD": {"early_exit": {"exit_now": "CONTINUE"}}},
                   {"SOL/USD": {"day_review": {"unknown": "X"}}},
                   {"SOL/USD": {"day_review_final": {"decision": "MAYBE"}}}):
        with pytest.raises(session_script.SessionRefused, match="FIXTURE_SCRIPT_INVALID"):
            script(broken)
    spec = script({"SOL/USD": {"day_review": {"decision": "EXIT"},
                               "day_review_final": {"agent_case": "DOES_NOT_HOLD"}}}).spec(
        "SOL/USD")
    assert spec["day_review"]["decision"] == "EXIT"
    assert spec["day_review_final"] == {**spec["day_review"], "agent_case": "DOES_NOT_HOLD"}
    assert spec["early_exit"] == session_script.EARLY_EXIT_DEFAULT
    kinds = session_script._request_kind
    assert kinds({k: {} for k in session_script.DAY_REVIEW_QUESTIONS}, {"symbol": "S"}) == (
        "DAY_REVIEW", "S")
    assert kinds({k: {} for k in session_script.DAY_REVIEW_QUESTIONS | {"agent_case"}},
                 {"symbol": "S"})[0] == "DAY_REVIEW"
    assert kinds({k: {} for k in session_script.EARLY_EXIT_QUESTIONS}, {"symbol": "S"})[0] == (
        "EARLY_EXIT")


def test_the_cli_parses_the_review_commands():
    parser = session_script.build_parser()
    args = parser.parse_args(["review", "--root", "/tmp/x", "--at", "request", "--bid", "101"])
    assert (args.command, args.at, args.bid, args.minutes) == ("review", "request", "101", None)
    args = parser.parse_args(["execute", "--root", "/tmp/x", "--simulate-prints", "--hold-open"])
    assert args.hold_open and args.simulate_prints
    args = parser.parse_args(["review-answer", "--root", "/tmp/x", "--review-id", "r",
                              "--decision", "EXIT", "--what-changed", "a", "--next-24h", "b",
                              "--proves-wrong", "c", "--suggested-stop", "1"])
    assert (args.review_id, args.decision, args.suggested_stop) == ("r", "EXIT", "1")
    args = parser.parse_args(["exit-flag", "--root", "/tmp/x", "--setup-id", "s",
                              "--what-changed", "a", "--next-24h", "b", "--proves-wrong", "c"])
    assert args.setup_id == "s" and args.sources is None
    with pytest.raises(SystemExit):
        parser.parse_args(["flag-answer", "--root", "/tmp/x", "--flag-id", "f", "--decision",
                           "STAY", "--what-changed", "a", "--next-24h", "b",
                           "--proves-wrong", "c"])
