from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.domain import Candidate
from catalyst_lab.validation import validate
from tests.conftest import NOW


def test_valid_uses_percentages_whole_shares_and_session_expiry(raw, evidence, policy):
    result = validate(Candidate(**raw), evidence, policy, NOW)
    assert result.passed
    assert result.computed_qty == 43
    assert result.planned_risk == D("49.45")
    assert result.expires_at == evidence.official_close
    bigger = validate(Candidate(**raw), replace(evidence, equity=D("10050")), policy, NOW)
    assert bigger.computed_qty == 87


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"stop": "100"}, "INVALID_LEVELS"),
        ({"target": "101"}, "MIN_REWARD_RISK"),
        ({"max_entry_price": "99.50"}, "INVALID_MAX_ENTRY"),
        ({"max_entry_price": "102.50"}, "INVALID_MAX_ENTRY"),
        ({"expires_at": NOW.isoformat()}, "INVALID_EXPIRATION"),
        ({"expires_at": (NOW + timedelta(days=1)).isoformat()}, "INVALID_EXPIRATION"),
    ],
)
def test_bad_candidates(raw, evidence, policy, changes, rule):
    assert validate(Candidate(**(raw | changes)), evidence, policy, NOW).failed_rule == rule


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"tradable": False}, "ASSET_NOT_TRADABLE"),
        ({"ticker": "OTHER"}, "ASSET_NOT_TRADABLE"),
        ({"asset_class": "crypto"}, "ASSET_NOT_TRADABLE"),
        ({"feed_healthy": False}, "DATA_FEED_FAILURE"),
        ({"observed_at": NOW - timedelta(seconds=10)}, "DATA_FEED_FAILURE"),
        ({"quote_timestamp": NOW - timedelta(seconds=6)}, "STALE_QUOTE"),
        ({"quote_timestamp": NOW + timedelta(seconds=1)}, "STALE_QUOTE"),
        ({"bid": D("NaN")}, "DATA_FEED_FAILURE"),
        ({"bid": D("101")}, "INVALID_QUOTE"),
        ({"bid": D("0")}, "INVALID_QUOTE"),
        ({"ask": D("102")}, "MAX_SPREAD"),
        ({"average_daily_dollar_volume": D("1")}, "MIN_DOLLAR_VOLUME"),
        ({"reconciled_session": None}, "STARTUP_RECONCILIATION_REQUIRED"),
        ({"unexplained_positions": True}, "STARTUP_RECONCILIATION_REQUIRED"),
        ({"equity": D("0")}, "INVALID_ACCOUNT_EVIDENCE"),
        ({"start_of_day_equity": D("-1")}, "INVALID_ACCOUNT_EVIDENCE"),
        ({"daily_halted": True}, "DAILY_RISK_HALT"),
        ({"realized_pnl_today": D("-50"), "open_unrealized_pnl": D("-100")}, "DAILY_RISK_HALT"),
        ({"theme": None}, "CORRELATION_UNKNOWN"),
        ({"open_themes": frozenset({"Semiconductors"})}, "CORRELATION_LIMIT"),
        ({"open_sectors": frozenset({"Technology"})}, "CORRELATION_LIMIT"),
        ({"open_planned_risk": D("50.01")}, "MAX_OPEN_PLANNED_RISK"),
        ({"equity": D("10")}, "ZERO_SHARE_SIZE"),
        ({"session_date": NOW.date() - timedelta(days=1)}, "MARKET_SESSION_CLOSED"),
        ({"official_close": NOW}, "MARKET_SESSION_CLOSED"),
    ],
)
def test_evidence_failures(raw, evidence, policy, changes, rule):
    assert validate(Candidate(**raw), replace(evidence, **changes), policy, NOW).failed_rule == rule


def test_no_evidence_fails_closed(raw, policy):
    assert validate(Candidate(**raw), None, policy, NOW).failed_rule == "DATA_FEED_FAILURE"


def test_no_policy_fails_closed(raw, evidence):
    assert validate(Candidate(**raw), evidence, None, NOW).failed_rule == "POLICY_NOT_CONFIGURED"


def test_premarket_receipt_allowed(raw, evidence, policy):
    premarket = NOW.replace(hour=8)
    e = replace(evidence, quote_timestamp=premarket, observed_at=premarket)
    assert validate(Candidate(**raw), e, policy, premarket).passed


def test_early_close_expiry_comes_from_calendar(raw, evidence, policy):
    e = replace(evidence, official_close=NOW.replace(hour=13))
    assert validate(Candidate(**raw), e, policy, NOW).expires_at.hour == 13


def test_exact_exposure_boundary(raw, evidence, policy):
    assert validate(
        Candidate(**raw), replace(evidence, open_planned_risk=D("50")), policy, NOW
    ).passed


def test_daily_halt_uses_start_equity_not_current(raw, evidence, policy):
    e = replace(evidence, equity=D("10000"), realized_pnl_today=D("-151"))
    assert validate(Candidate(**raw), e, policy, NOW).failed_rule == "DAILY_RISK_HALT"
