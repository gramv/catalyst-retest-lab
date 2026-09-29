from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from catalyst_lab.crypto_liquidity import CryptoLiquidityPolicy, CryptoLiquidityReader
from tests.test_managed_execution import er as er
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_managed_execution import pristine_cluster as pristine_cluster
from tests.test_research_cycle import NOW, fixture_asset


def policy():
    return CryptoLiquidityPolicy("CRYPTO_LIQUIDITY_PAPER_V1", 60, D("1000000"),
                                 D(".01"), D("50"), D(".25"))


def reader(*, change=None):
    bars = [replace(b, provider="ALPACA", feed="CRYPTO_US") for b in fixture_asset().bars]
    if change:
        bars = change(bars)
    return CryptoLiquidityReader(SimpleNamespace(completed_bars=lambda *args: (bars, ())),
                                 policy(), clock=lambda: NOW)


def test_independent_venue_volume_and_costs_are_retained_as_assumptions():
    result = reader()("BTC/USD", {"max_entry_price": "106", "stop": "100"})
    assert result["passed"] and len(result["observations"]) == 60
    assert D(result["maximum_entry_notional"]) == D(result["observed_dollar_volume"]) / 100
    assert "ASSUMPTION" in result["cost_scope"]
    assert result["policy"]["policy_id"] == "CRYPTO_LIQUIDITY_PAPER_V1"


@pytest.mark.parametrize("change", [
    lambda rows: rows[:-1],
    lambda rows: [replace(b, volume=D(0)) for b in rows],
    lambda rows: [replace(b, provider="MUSE") for b in rows],
    lambda rows: [replace(b, start_at=b.start_at-timedelta(minutes=3),
                         end_at=b.end_at-timedelta(minutes=3)) for b in rows],
])
def test_unknown_low_stale_or_nonvenue_liquidity_cannot_pass(change):
    assert not reader(change=change)("BTC/USD", {"max_entry_price": "106", "stop": "100"})["passed"]


def test_estimated_cost_does_not_disappear_from_narrow_risk_setup():
    result = reader()("BTC/USD", {"max_entry_price": "106", "stop": "105.9"})
    assert result["reason"] == "CRYPTO_ASSUMED_COST_EXCEEDS_POLICY"


def test_liquidity_limit_rejects_entry_without_changing_risk_size(mx):
    engine, venue, _ = mx
    engine.crypto_liquidity_reader = lambda *args: {
        "passed": True, "observed_at": venue.now.isoformat(), "maximum_entry_notional": "1",
    }
    sid = engine.admit(packet(mx))
    engine.observe_trigger(sid, observation(mx))
    assert engine._load(sid)[1]["state"] == "RISK_REJECTED"
    assert engine._load(sid)[1]["reason"] == "CRYPTO_VENUE_PARTICIPATION_LIMIT"
    assert not venue.orders
