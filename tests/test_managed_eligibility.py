from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.managed_classification import (
    CLASSIFICATION_POLICY,
    initialize_managed_classifications,
)
from catalyst_lab.managed_eligibility import validate_managed_eligibility
from catalyst_lab.market import NY, Observation, Session
from catalyst_lab.us_admission import admission_profile
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=NY)
PROFILE = admission_profile("US_PAPER_ADMISSION_TEST_V1")


class EvidenceBroker:
    feed = "iex"

    def __init__(self):
        days = []
        cursor = NOW.date() - timedelta(days=1)
        while len(days) < 20:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        self.sessions = [
            Session(
                day,
                datetime.combine(day, datetime.min.time(), NY).replace(hour=9, minute=30),
                datetime.combine(day, datetime.min.time(), NY).replace(hour=16),
            )
            for day in sorted(days + [NOW.date()])
        ]
        self.bars = [
            {
                "t": datetime.combine(day, datetime.min.time(), NY).isoformat(),
                "v": 200000,
                "vw": 100,
            }
            for day in sorted(days)
        ]
        self.quote = Observation(
            "quote", "TEST", NOW.isoformat(), "iex", bid=D("100"), ask=D("100.01")
        )
        self.asset_update = {}
        self.calls = []

    def asset(self, symbol):
        self.calls.append("asset")
        return {
            "symbol": symbol,
            "class": "crypto" if "/" in symbol else "us_equity",
            "status": "active",
            "tradable": True,
            "fractionable": True,
            "price_increment": "0.01",
            "min_trade_increment": "0.001",
            "min_order_size": "0.001",
            **self.asset_update,
        }

    def calendar(self, start, end):
        self.calls.append("calendar")
        return self.sessions

    def daily_bars(self, symbol, start, end):
        self.calls.append("daily_bars")
        assert start.date() == self.sessions[0].session_date
        assert end.astimezone(NY).date() < NOW.date()
        return self.bars

    def quotes(self, symbols):
        self.calls.append("quotes")
        assert symbols == ["TEST"]
        return [self.quote]


def packet(market="US_STOCKS"):
    return {
        "market": market,
        "symbol": "TEST" if market == "US_STOCKS" else "BTC/USD",
        "levels": {"max_entry_price": "100.15"},
    }


def test_twenty_complete_exchange_days_not_intraday_proxy_and_get_only():
    broker = EvidenceBroker()
    result = validate_managed_eligibility(broker, packet(), NOW, PROFILE)
    assert result["passed"] and result["reason"] is None
    evidence = result["evidence"]
    assert evidence["average_daily_dollar_volume"] == "20000000"
    assert evidence["completed_sessions"] == 20
    assert evidence["feed_coverage"] == "IEX_ONLY_NOT_CONSOLIDATED"
    assert evidence["liquidity_method"] == "MEAN_RAW_DAILY_VOLUME_TIMES_PROVIDER_VWAP"
    assert len(evidence["daily_bars"]) == 20
    assert broker.calls == ["asset", "calendar", "daily_bars", "quotes"]


def test_low_daily_volume_rejected_even_if_scan_said_intraday_liquid():
    broker = EvidenceBroker()
    broker.bars[0]["v"] -= 1
    candidate = {**packet(), "scan_intraday_dollar_volume": "999999999999"}
    result = validate_managed_eligibility(broker, candidate, NOW, PROFILE)
    assert result["reason"] == "MIN_DOLLAR_VOLUME" and not result["passed"]


@pytest.mark.parametrize("defect", ["missing", "duplicate", "current", "non_session"])
def test_missing_duplicate_partial_or_nonexchange_daily_bar_fails_closed(defect):
    broker = EvidenceBroker()
    if defect == "missing":
        broker.bars.pop()
    elif defect == "duplicate":
        broker.bars[-1] = dict(broker.bars[0])
    elif defect == "current":
        broker.bars[-1]["t"] = NOW.isoformat()
    else:
        broker.bars[-1]["t"] = "2026-09-13T00:00:00-04:00"
    assert validate_managed_eligibility(broker, packet(), NOW, PROFILE)["reason"] == (
        "LIQUIDITY_EVIDENCE_INCOMPLETE"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("class", "crypto"),
        ("symbol", "WRONG"),
        ("status", "inactive"),
        ("tradable", False),
    ],
)
def test_wrong_or_untradable_broker_asset_never_gets_eligibility(field, value):
    broker = EvidenceBroker()
    broker.asset_update = {field: value}
    assert validate_managed_eligibility(broker, packet(), NOW, PROFILE)["reason"] == (
        "ASSET_NOT_TRADABLE"
    )
    assert broker.calls == ["asset"]


@pytest.mark.parametrize("seconds,expected", [(5, None), (6, "STALE_QUOTE"), (-1, "STALE_QUOTE")])
def test_quote_freshness_exact_five_second_boundary(seconds, expected):
    broker = EvidenceBroker()
    broker.quote = replace(
        broker.quote, provider_timestamp=(NOW - timedelta(seconds=seconds)).isoformat()
    )
    assert validate_managed_eligibility(broker, packet(), NOW, PROFILE)["reason"] == expected


@pytest.mark.parametrize(
    "bid,ask,reason",
    [
        ("99", "100", "MAX_SPREAD"),
        ("100.17", "100.18", "PRICE_BEYOND_MAX_ENTRY"),
        ("100.02", "100.01", "INVALID_QUOTE"),
    ],
)
def test_quote_spread_and_max_entry_revalidated_with_current_broker_quote(bid, ask, reason):
    broker = EvidenceBroker()
    broker.quote = replace(broker.quote, bid=D(bid), ask=D(ask))
    assert validate_managed_eligibility(broker, packet(), NOW, PROFILE)["reason"] == reason


@pytest.mark.parametrize(
    "hour,minute,allowed", [(9, 29, False), (9, 30, True), (15, 54, True), (15, 55, False)]
)
def test_regular_session_and_flatten_deadline(hour, minute, allowed):
    broker = EvidenceBroker()
    now = NOW.replace(hour=hour, minute=minute)
    broker.quote = replace(broker.quote, provider_timestamp=now.isoformat())
    result = validate_managed_eligibility(broker, packet(), now, PROFILE)
    assert result["passed"] == allowed
    if not allowed:
        assert result["reason"] == "ENTRY_WINDOW_CLOSED"


def test_early_close_and_calendar_rollover_during_gets():
    broker = EvidenceBroker()
    broker.sessions[-1] = replace(broker.sessions[-1], closes=NOW.replace(hour=13))
    now = NOW.replace(hour=12, minute=55)
    assert (
        validate_managed_eligibility(broker, packet(), now, PROFILE)["reason"]
        == "ENTRY_WINDOW_CLOSED"
    )
    times = iter([NOW, NOW + timedelta(days=1)])
    assert validate_managed_eligibility(broker, packet(), lambda: next(times), PROFILE)[
        "reason"
    ] == ("ENTRY_WINDOW_CLOSED")


def test_current_holiday_missing_calendar_and_insufficient_completed_sessions():
    broker = EvidenceBroker()
    broker.sessions.pop()
    assert (
        validate_managed_eligibility(broker, packet(), NOW, PROFILE)["reason"]
        == "ENTRY_WINDOW_CLOSED"
    )
    broker = EvidenceBroker()
    broker.sessions.pop(0)
    assert validate_managed_eligibility(broker, packet(), NOW, PROFILE)["reason"] == (
        "LIQUIDITY_EVIDENCE_INCOMPLETE"
    )


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({}, None),
        ({"class": "us_equity"}, "ASSET_NOT_TRADABLE"),
        ({"fractionable": False}, "CRYPTO_FRACTIONAL_EXECUTION_UNAVAILABLE"),
        ({"price_increment": "0"}, "ELIGIBILITY_EVIDENCE_UNAVAILABLE"),
        ({"min_order_size": "0.0005"}, "CRYPTO_PRECISION_UNAVAILABLE"),
    ],
)
def test_crypto_uses_its_own_metadata_not_stock_calendar_or_daily_liquidity(updates, reason):
    broker = EvidenceBroker()
    broker.asset_update = updates
    result = validate_managed_eligibility(broker, packet("CRYPTO"), NOW, PROFILE)
    assert result["reason"] == reason and broker.calls == ["asset"]


def test_provider_failure_returns_safe_machine_reason_only():
    broker = EvidenceBroker()

    def unavailable(*args):
        raise RuntimeError("provider response includes fixture-sensitive-payload")

    broker.daily_bars = unavailable
    result = validate_managed_eligibility(broker, packet(), NOW, PROFILE)
    assert result["reason"] == "ELIGIBILITY_EVIDENCE_UNAVAILABLE"
    assert "fixture-sensitive-payload" not in repr(result)


def test_operator_classifications_are_idempotent_and_crypto_shares_one_budget(er):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    broker = EvidenceBroker()
    rows = [{"ticker": "MSFT", "sector": "TECHNOLOGY", "theme": "ENTERPRISE_SOFTWARE"}]
    first = initialize_managed_classifications(
        repo,
        broker,
        policy_id=CLASSIFICATION_POLICY,
        us_mapping=rows,
        crypto_symbols=("BTC/USD", "ETH/USD"),
    )
    assert first["inserted"] == 3
    again = initialize_managed_classifications(
        repo,
        broker,
        policy_id=CLASSIFICATION_POLICY,
        us_mapping=rows,
        crypto_symbols=("BTC/USD", "ETH/USD"),
    )
    assert again["inserted"] == 0
    with repo.connect() as conn:
        crypto = conn.execute(
            "SELECT * FROM lab.current_classifications WHERE ticker LIKE '%%/USD'"
        )
        assert {(r["sector"], r["theme"]) for r in crypto} == {("CRYPTO", "CRYPTO_SHARED")}
    assert verify_events(repo.export_events())["valid"]


@pytest.mark.parametrize(
    "row",
    [
        {"ticker": "TEST", "sector": "TECH", "theme": "SOFTWARE", "risk_pct": 10},
        {"ticker": "TEST", "sector": "TECH"},
    ],
)
def test_operator_mapping_cannot_carry_risk_or_partial_classification(er, row):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    with pytest.raises(ValueError, match="OPERATOR_US_CLASSIFICATION_REQUIRED"):
        initialize_managed_classifications(
            repo,
            EvidenceBroker(),
            policy_id=CLASSIFICATION_POLICY,
            us_mapping=[row],
            crypto_symbols=(),
        )


def test_existing_classification_is_not_silently_replaced(er):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    broker = EvidenceBroker()
    rows = [{"ticker": "NVDA", "sector": "TECHNOLOGY", "theme": "SEMICONDUCTORS"}]
    initialize_managed_classifications(
        repo, broker, policy_id=CLASSIFICATION_POLICY, us_mapping=rows, crypto_symbols=()
    )
    with pytest.raises(ValueError, match="EXISTING_CLASSIFICATION_CONFLICT"):
        initialize_managed_classifications(
            repo,
            broker,
            policy_id=CLASSIFICATION_POLICY,
            us_mapping=[{**rows[0], "theme": "OTHER"}],
            crypto_symbols=(),
        )
