"""Private PostgreSQL and fake paper reads; never contacts an external broker."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from catalyst_lab.alpaca import AlpacaCredentials, AlpacaPaperClient
from catalyst_lab.config import Settings
from catalyst_lab.market import MarketDataError, Session
from catalyst_lab.us_admission import USAdmissionEvidence, admission_profile
from tests.conftest import NOW, TOKEN
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_risk import risk_setup as risk_setup


@pytest.fixture
def admission(er, risk_setup, raw):
    _, client, engine = risk_setup
    client.feed = "iex"
    engine.classify(raw["ticker"], "FIXTURE_SECTOR", "FIXTURE_THEME", "LAB_FIXTURE")
    previous = [NOW.date() - timedelta(days=i) for i in range(1, 40)]
    previous = sorted(d for d in previous if d.weekday() < 5)[-20:]
    sessions = [
        Session.from_calendar({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
        for d in [*previous, NOW.date()]
    ]
    client.calendar = lambda start, end: sessions
    client.asset = lambda ticker: {
        "symbol": ticker,
        "class": "us_equity",
        "status": "active",
        "tradable": True,
    }
    client.bars = [
        {"t": s.opens.replace(hour=0, minute=0).isoformat(), "v": 300000, "vw": 100}
        for s in sessions[:-1]
    ]
    client.daily_bars = lambda *args: client.bars
    profile = admission_profile("US_PAPER_ADMISSION_TEST_V1")
    provider = USAdmissionEvidence(
        er, client, profile, ready=lambda: True, feed_healthy=lambda: True, clock=lambda: client.at
    )
    return provider, client


def submit(er, raw, admission):
    provider, client = admission
    return er.submit(
        raw, NOW, provider, provider.profile.validation_policy, decision_clock=lambda: client.at
    )


def test_complete_provider_validates_without_order_and_retains_provenance(er, raw, admission):
    result = submit(er, raw, admission)
    assert result["state"] == "VALIDATED"
    with er.connect() as conn:
        evidence = conn.execute("SELECT evidence_json FROM lab.validation_decisions").fetchone()[
            "evidence_json"
        ]
        assert not conn.execute("SELECT 1 FROM lab.orders").fetchone()
        assert not conn.execute("SELECT 1 FROM lab.risk_decisions").fetchone()
    assert evidence["average_daily_dollar_volume"] == "30000000"
    assert evidence["start_of_day_equity"] == "10000"
    assert evidence["admission_metadata"]["completed_sessions"] == 20
    assert evidence["admission_metadata"]["feed_coverage"] == "IEX_ONLY"
    assert "account_number" not in str(evidence)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p, c: c.bars.pop(), "LIQUIDITY_EVIDENCE_INCOMPLETE"),
        (lambda p, c: c.bars.append(c.bars[0]), "LIQUIDITY_EVIDENCE_INCOMPLETE"),
        (lambda p, c: c.bars[0].update(t=NOW.isoformat()), "LIQUIDITY_EVIDENCE_INCOMPLETE"),
        (lambda p, c: c.bars[0].update(v=-1), "DATA_FEED_FAILURE"),
        (lambda p, c: c.bars[0].update(vw="NaN"), "DATA_FEED_FAILURE"),
        (lambda p, c: [b.update(v=1) for b in c.bars], "MIN_DOLLAR_VOLUME"),
        (lambda p, c: setattr(c, "asset", lambda t: None), "ASSET_NOT_TRADABLE"),
        (lambda p, c: setattr(p, "ready", lambda: False), "STARTUP_RECONCILIATION_REQUIRED"),
        (lambda p, c: setattr(p, "feed_healthy", lambda: False), "DATA_FEED_FAILURE"),
        (lambda p, c: setattr(c, "feed", "delayed_sip"), "DATA_FEED_FAILURE"),
        (lambda p, c: setattr(c, "equity", D("9700")), "DAILY_RISK_HALT"),
        (lambda p, c: setattr(c, "quotes", lambda names: []), "DATA_FEED_FAILURE"),
        (
            lambda p, c: setattr(
                c, "positions_list", [{"symbol": "UNKNOWN", "qty": "1", "unrealized_pl": "0"}]
            ),
            "STARTUP_RECONCILIATION_REQUIRED",
        ),
    ],
)
def test_admission_rejects_incomplete_evidence(er, raw, admission, mutation, reason):
    mutation(*admission)
    result = submit(er, raw, admission)
    assert result["state"] == "REJECTED"
    assert result["failed_rule"] == reason


def test_account_snapshot_and_quote_expire_during_io(er, raw, admission):
    _, client = admission
    original = client.account

    def slow():
        client.at += timedelta(seconds=6)
        return original()

    client.account = slow
    assert submit(er, raw, admission)["failed_rule"] == "DATA_FEED_FAILURE"


def test_feed_loss_during_fetch_prevents_validation(er, raw, admission):
    provider, client = admission
    original = client.quotes

    def disconnected(names):
        provider.feed_healthy = lambda: False
        return original(names)

    client.quotes = disconnected
    assert submit(er, raw, admission)["failed_rule"] == "DATA_FEED_FAILURE"


def test_calendar_early_close_prevents_new_admission(er, raw, admission):
    _, client = admission
    sessions = client.calendar(None, None)
    sessions[-1] = replace(sessions[-1], closes=NOW.replace(hour=13, minute=0))
    client.at = NOW.replace(hour=12, minute=55)
    assert submit(er, raw, admission)["failed_rule"] == "ENTRY_WINDOW_CLOSED"


def test_unknown_classification_not_inferred_from_muse(er, raw, admission):
    body = raw | {"ticker": "UNKNOWN"}
    assert submit(er, body, admission)["failed_rule"] == "CORRELATION_UNKNOWN"


def test_missing_persisted_baseline_cannot_use_current_equity(er, raw, admission):
    _, client = admission
    client.at += timedelta(days=1)
    assert submit(er, raw, admission)["failed_rule"] in {
        "DAY_START_EQUITY_REQUIRED",
        "MARKET_SESSION_CLOSED",
    }


def test_provider_does_not_hold_shared_audit_or_risk_lock(er, raw, evidence, policy):
    def provider(candidate, at):
        with er.connect() as other:
            assert other.execute("SELECT pg_try_advisory_xact_lock(719172026) AS ok").fetchone()[
                "ok"
            ]
        return evidence

    assert er.submit(raw, NOW, provider, policy)["state"] == "VALIDATED"


def test_freshness_checked_after_provider_returns(er, raw, evidence, policy):
    result = er.submit(
        raw, NOW, lambda c, n: evidence, policy, decision_clock=lambda: NOW + timedelta(seconds=6)
    )
    assert result["failed_rule"] == "DATA_FEED_FAILURE"


def test_profile_is_explicit_and_requires_monitor_and_baseline():
    with pytest.raises(ValueError):
        admission_profile("unknown")
    with pytest.raises(ValueError):
        Settings("fixture", TOKEN, us_admission_policy="US_PAPER_ADMISSION_TEST_V1")


@pytest.mark.parametrize("repeat", [False, True])
def test_daily_bars_pages_on_fixed_data_host(repeat):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "data.alpaca.markets"
        assert request.url.path == "/v2/stocks/bars"
        assert request.url.params["feed"] == "iex"
        assert request.url.params["adjustment"] == "raw"
        return httpx.Response(
            200,
            json={
                "bars": {"SPY": [{"v": 1}]},
                "next_page_token": "page2" if len(calls) == 1 or repeat else None,
            },
        )

    client = AlpacaPaperClient(
        AlpacaCredentials("PK" + "A" * 20, "fixture-secret"), transport=httpx.MockTransport(handler)
    )
    if repeat:
        with pytest.raises(MarketDataError, match="INCOMPLETE_DAILY_BARS"):
            client.daily_bars("SPY", NOW - timedelta(days=30), NOW)
    else:
        assert len(client.daily_bars("SPY", NOW - timedelta(days=30), NOW)) == 2
    assert calls[1].url.params["page_token"] == "page2"
    client.close()
