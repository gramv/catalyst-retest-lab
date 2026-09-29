"""Latest Part A rulings: real gaps only, using isolated PostgreSQL/fake broker evidence."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.domain import Candidate
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.market import Observation, Session
from catalyst_lab.risk import RiskPolicy
from catalyst_lab.trigger import TriggerPolicy, advance
from catalyst_lab.validation import validate
from tests.conftest import NOW
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_risk import candidate_factory as candidate_factory
from tests.test_risk import risk_setup as risk_setup


@pytest.mark.parametrize("target,passed", [("102", False), ("102.449", False), ("102.45", True)])
def test_reward_risk_uses_executable_maximum_entry(raw, evidence, policy, target, passed):
    result = validate(Candidate.model_validate(raw | {"target": target}), evidence, policy, NOW)
    assert result.passed is passed
    if not passed:
        assert result.failed_rule == "MIN_REWARD_RISK"


@pytest.mark.parametrize("close_hour", [13, 16])
def test_flatten_deadline_kills_untriggered_and_new_candidates(raw, evidence, policy, close_hour):
    session = Session(NOW.date(), NOW.replace(hour=9, minute=30), NOW.replace(hour=close_hour))
    at = session.flatten_time
    candidate = Candidate.model_validate(raw)
    quote = Observation(
        "quote",
        raw["ticker"],
        (at - timedelta(seconds=1)).isoformat(),
        "iex",
        bid=D("99.99"),
        ask=D("100.01"),
    )
    context, _ = advance(
        candidate,
        session,
        {"state": "VALIDATED"},
        quote,
        at - timedelta(seconds=1),
        True,
        TriggerPolicy(),
    )
    trade = Observation(
        "trade", raw["ticker"], at.isoformat(), "iex", price=D("100"), trade_id="deadline"
    )
    context, reason = advance(candidate, session, context, trade, at, True, TriggerPolicy())
    assert context["state"] == "EXPIRED_UNTRIGGERED" and reason == "ENTRY_WINDOW_CLOSED"
    ev = replace(evidence, official_close=session.closes, observed_at=at, quote_timestamp=at)
    assert validate(candidate, ev, policy, at).failed_rule == "ENTRY_WINDOW_CLOSED"


@pytest.mark.parametrize("close_hour", [13, 16])
def test_risk_and_transport_recheck_calendar_flatten_deadline(
    risk_setup, candidate_factory, close_hour
):
    repo, client, engine = risk_setup
    session = Session(NOW.date(), NOW.replace(hour=9, minute=30), NOW.replace(hour=close_hour))
    client.calendar = lambda start, end: [session]
    approved_id = candidate_factory()
    denied_id = candidate_factory()
    client.at = session.flatten_time - timedelta(seconds=1)
    decision = engine.authorize_entry(approved_id)
    assert decision["decision"] == "APPROVED"
    client.at = session.flatten_time
    assert engine.authorize_entry(denied_id)["reason"] == "ENTRY_WINDOW_CLOSED"
    request = httpx.Request(
        "POST",
        PAPER_ENDPOINT + "/v2/orders",
        json=decision["payload_json"],
        extensions={"risk_decision_id": str(decision["risk_decision_id"])},
    )
    with pytest.raises(SubmissionDisabled, match="ENTRY_WINDOW_CLOSED"):
        AuthorizationGate(repo, clock=lambda: client.at).claim(request)
    with repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.authorization_claims").fetchone()


def test_candidate_expiry_rechecked_even_with_fresh_risk_decision(risk_setup, candidate_factory):
    repo, client, engine = risk_setup
    cid = candidate_factory(expires_at=(NOW + timedelta(seconds=1)).isoformat())
    decision = engine.authorize_entry(cid)
    assert decision["decision"] == "APPROVED"
    request = httpx.Request(
        "POST",
        PAPER_ENDPOINT + "/v2/orders",
        json=decision["payload_json"],
        extensions={"risk_decision_id": str(decision["risk_decision_id"])},
    )
    with pytest.raises(SubmissionDisabled, match="ENTRY_WINDOW_CLOSED"):
        AuthorizationGate(repo, clock=lambda: NOW + timedelta(seconds=1)).claim(request)


@pytest.mark.parametrize("policy_type", [TriggerPolicy, RiskPolicy])
def test_builder_setting_cannot_relax_ten_basis_point_limit(policy_type):
    with pytest.raises(ValueError, match="10 bps"):
        policy_type(max_spread_bps=D("10.01"))
