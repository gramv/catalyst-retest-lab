"""The supervised session tool runs CRYPTO_MAINTENANCE_V1 reviews (package maintenance).

Fixture evidence only: the scripted mock Jev transport and the SESSION_SIMULATION paper venue,
in-process on disposable template-clone databases. ``--jev typesafe`` uses the same path with
the real model and is run only by the coordinator or the owner.
"""

import json
from datetime import UTC, datetime

import pytest

from catalyst_lab.account_risk import JEV_MANAGED_ARM
from catalyst_lab.audit import verify_events
from tests.test_agent_research_session import make_session as make_session
from tests.test_agent_research_session import session_script, submit
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster


def report(now):
    from tests.test_research_report_v3 import pick, report_v3

    return report_v3([pick(0, "SOL/USD", kind="BOTH", now=now),
                      pick(1, "XRP/USD", kind="NEWS", now=now)],
                     now=now, agent_id=session_script.AGENT_ID, skipped=[])


def lifecycles(session):
    session.tick()
    executed = session.execute(simulate_prints=True)
    return executed, {life["symbol"]: life for life in executed["lifecycles"]}


def test_a_top_k_session_maintains_its_trades_with_fixture_answers(make_session, monkeypatch,
                                                                    tmp_path):
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda setup_id, pct: JEV_MANAGED_ARM)
    fixture = session_script.FixtureScript({"SOL/USD": {"maintenance": "RAISE_STOP"},
                                            "XRP/USD": {"maintenance": "HOLD"}})
    session = make_session(selection_rule="TOPK", topk_k=5, fixture=fixture)
    # The version admission records (package answer-rules: V2; package jev-budget: V3, whose
    # session guard runs with the deploy example's budget).
    configuration = session._configuration()
    assert configuration["maintenance_policy"]["policy_id"] == "CRYPTO_MAINTENANCE_V3"
    assert configuration["jev_spend_guard"]["estimate"]["monthly_budget_usd"] == "50"
    submit(session, report(datetime.now(UTC)))
    executed, lives = lifecycles(session)
    sol = lives["SOL/USD"]
    review = sol["maintenance_review"]
    assert "management_review" not in sol  # Today's monitor never sees a maintained trade.
    assert (review["outcome"], review["action"], review["jev_calls"]) == (
        "APPLIED", "RAISE_STOP", 1), json.dumps(review, default=str)
    assert "R_MILESTONE" in review["trigger_reasons"]  # Price was moved to +1R.
    assert review["options"]["stop"][0] == ("S1", "100.00000000", ["BREAKEVEN"])
    assert [option[2] for option in review["options"]["target"]][-1][0] == "HIGH_7D"
    assert review["stop"]["new"] == "100.00000000" and review["state_bytes"] <= 11_000
    assert review["stop_replaced"] == {"path": "PATCH_REPLACE", "to_stop": "100.00000000"}
    assert review["levels_after"]["stop"] == "100.00000000"
    assert sol["protection_after_review"]["protected"]
    assert sol["exit"]["reason"] == "TARGET_EXIT" and sol["final_state"] == "CLOSED"
    assert sol["residual"]["zero"] and sol["reconciliation_after_exit"]["clean"]
    xrp = lives["XRP/USD"]["maintenance_review"]
    assert (xrp["outcome"], xrp["action"]) == ("HELD", "HOLD")
    assert xrp["levels_after"] == {"stop": "95", "target": "111"}
    assert sorted(c for c in fixture.calls if c.startswith("MAINTENANCE")) == [
        "MAINTENANCE:SOL/USD", "MAINTENANCE:XRP/USD"]
    assert not any(c.startswith("MANAGEMENT:") for c in fixture.calls)
    text = session_script.render_execute(json.loads(json.dumps(executed, default=str)))
    assert "maintenance     APPLIED RAISE_STOP" in text and "replaced by PATCH_REPLACE" in text
    manifest = session.export(tmp_path / "export")
    events = json.loads((tmp_path / "export" / "events.json").read_text())
    assert manifest["audit"]["valid"] and verify_events(events["events"])["valid"]
    with session.repo.connect() as conn:
        recorded = conn.execute("""SELECT body->>'outcome' AS outcome FROM lab.managed_events
            WHERE kind='MAINTENANCE_DECISION' ORDER BY event_seq""").fetchall()
    assert sorted(r["outcome"] for r in recorded) == ["APPLIED", "HELD"]


def test_a_disabled_session_sends_nothing_to_the_maintenance_jev(make_session, monkeypatch):
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda setup_id, pct: JEV_MANAGED_ARM)
    fixture = session_script.FixtureScript({"*": {"maintenance": "RAISE_TARGET"}})
    session = make_session(selection_rule="TOPK", topk_k=5, fixture=fixture,
                           management_reviews="DISABLED")
    submit(session, report(datetime.now(UTC)))
    _, lives = lifecycles(session)
    for life in lives.values():
        review = life["maintenance_review"]
        assert (review["outcome"], review["reason"], review["jev_calls"]) == (
            "SKIPPED", "MANAGEMENT_REVIEWS_DISABLED", 0)
        assert life["exit"]["reason"] == "TARGET_EXIT" and life["final_state"] == "CLOSED"
    assert not any(c.startswith("MAINTENANCE") for c in fixture.calls)


def test_the_fixture_script_answers_only_the_maintenance_actions():
    with pytest.raises(session_script.SessionRefused, match="FIXTURE_SCRIPT_INVALID"):
        session_script.FixtureScript({"SOL/USD": {"maintenance": "TIGHTEN_STOP"}})
    assert session_script.FixtureScript({"SOL/USD": {"maintenance": "FLAG_EARLY_EXIT"}}).spec(
        "SOL/USD")["maintenance"] == "FLAG_EARLY_EXIT"
