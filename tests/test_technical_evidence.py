from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.research_reports import ResearchLevels
from catalyst_lab.technical_evidence import TechnicalEvidence

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def evidence():
    return {
        "schema_version": "MUSE_OBSERVED_TECHNICALS_V1", "provider": "LAB_FIXTURE",
        "venue": "FIXTURE", "feed": "FIXTURE_MINUTES",
        "source_url": "https://market.example/bars", "retrieved_at": NOW.isoformat(),
        "timeframe_seconds": 60,
        "bars": [{"bar_id": f"bar-{i}", "started_at": (NOW-timedelta(minutes=20-i)).isoformat(),
                  "open": "100", "high": "112", "low": "95", "close": "101",
                  "volume": "1000"} for i in range(20)],
        "quote": {"observed_at": NOW.isoformat(), "bid": "100", "ask": "100.01"},
        "level_references": {
            "entry_trigger": {"bar_id": "bar-19", "field": "open", "rationale": "Retest"},
            "stop": {"bar_id": "bar-18", "field": "low", "rationale": "Invalidation"},
            "target": {"bar_id": "bar-1", "field": "high", "rationale": "Observed resistance"},
        },
    }


def levels(**changes):
    return ResearchLevels(**dict(entry_trigger="100", max_entry_price="100.1", stop="95",
                                 target="112") | changes)


def test_metrics_reproduce_from_retained_observations_and_preserve_origin():
    data = TechnicalEvidence.model_validate(evidence()).computed(now=NOW, levels=levels())
    assert D(data["metrics"]["sma_5"]) == D(data["metrics"]["sma_20"]) == 101
    assert D(data["metrics"]["observed_dollar_volume_20"]) == 2020000
    assert D(data["metrics"]["last_volume_vs_prior_19_mean"]) == 1
    assert D(data["metrics"]["last_bar_age_seconds"]) == 0
    assert data["metrics"]["gap_count"] == 0
    assert len(data["observations_hash"]) == 64 and not data["execution_authority"]
    restored = TechnicalEvidence.model_validate(data["observations"])
    assert restored.computed(now=NOW, levels=levels()) == data


def test_unobserved_level_cannot_claim_provenance():
    with pytest.raises(ValueError, match="LEVEL_OBSERVATION_MISMATCH"):
        TechnicalEvidence.model_validate(evidence()).computed(now=NOW, levels=levels(target="113"))


@pytest.mark.parametrize("change", ["incomplete", "duplicate", "unordered", "crossed", "ohlc"])
def test_invalid_observations_are_rejected(change):
    data = evidence()
    if change == "incomplete":
        data["bars"][-1]["started_at"] = NOW.isoformat()
    if change == "duplicate":
        data["bars"][-1]["bar_id"] = "bar-0"
    if change == "unordered":
        data["bars"].reverse()
    if change == "crossed":
        data["quote"]["bid"] = "101"
    if change == "ohlc":
        data["bars"][0]["high"] = "99"
    with pytest.raises(ValueError):
        TechnicalEvidence.model_validate(data)


def test_zero_volume_and_absent_quote_remain_unknown():
    data = evidence()
    data["quote"] = None
    for bar in data["bars"]:
        bar["volume"] = "0"
    metrics = TechnicalEvidence.model_validate(data).computed(now=NOW, levels=levels())["metrics"]
    assert metrics["last_volume_vs_prior_19_mean"] is None and metrics["spread_bps"] is None
