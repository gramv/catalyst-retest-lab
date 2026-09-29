"""Dataset integrity checks, not checks of model judgment or market performance."""

import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

import pytest

from catalyst_lab.jev_benchmark_cases import (
    CONTRADICTED,
    DIMENSIONS,
    INSUFFICIENT,
    LABELS,
    PROVENANCE,
    SUPPORTED,
    get_cases,
)
from catalyst_lab.jev_contract import digest, encoded


def _overall(case):
    choices = set(case["reference"].values())
    if CONTRADICTED in choices:
        return CONTRADICTED
    return SUPPORTED if choices == {SUPPORTED} else INSUFFICIENT


def test_balanced_frozen_counts_and_exact_envelope():
    cases = get_cases()
    assert len(cases) == len({case["case_id"] for case in cases}) == 60
    assert len({digest(encoded(case["state"])) for case in cases}) == 60
    for case in cases:
        assert set(case) == {
            "case_id", "family_id", "split", "market", "provenance", "state", "reference"
        }
        assert case["provenance"] == PROVENANCE == "SYNTHETIC_ENGINEERING"
        assert set(case["reference"]) == set(DIMENSIONS)
        assert set(case["reference"].values()) <= set(LABELS)
        assert case["state"]["market"] == case["market"]
        assert case["state"]["strategy_mode"] == "FRESH_CATALYST"
    assert Counter(_overall(case) for case in cases) == dict.fromkeys(LABELS, 20)
    assert Counter((case["market"], case["split"]) for case in cases) == {
        (market, split): 15
        for market in ("US", "CRYPTO") for split in ("development", "holdout")
    }
    for market in ("US", "CRYPTO"):
        for split in ("development", "holdout"):
            assert Counter(
                _overall(case) for case in cases
                if case["market"] == market and case["split"] == split
            ) == dict.fromkeys(LABELS, 5)


def test_complete_counterfactual_families_cannot_cross_split():
    families = defaultdict(list)
    sources_by_split = defaultdict(set)
    instruments_by_split = defaultdict(set)
    for case in get_cases():
        families[case["family_id"]].append(case)
        sources_by_split[case["split"]].update(
            source["source_id"] for source in case["state"]["sources"]
        )
        instruments_by_split[case["split"]].add(case["state"]["instrument"]["symbol"])
    assert len(families) == 20
    assert not sources_by_split["development"] & sources_by_split["holdout"]
    assert not instruments_by_split["development"] & instruments_by_split["holdout"]
    for family in families.values():
        assert len(family) == 3
        assert len({case["split"] for case in family}) == 1
        assert len({case["market"] for case in family}) == 1
        assert {_overall(case) for case in family} == set(LABELS)
        varied_dimensions = [
            dimension for dimension in DIMENSIONS
            if len({case["reference"][dimension] for case in family}) > 1
        ]
        assert len(varied_dimensions) == 1
        baseline = next(case["state"] for case in family if _overall(case) == SUPPORTED)
        for case in family:
            state = case["state"]
            assert state["claims"] == baseline["claims"]
            assert state["thesis"] == baseline["thesis"]
            assert state["disproof"] == baseline["disproof"]
            assert state["technical_narrative"] == baseline["technical_narrative"]
            changed_sources = sum(
                left != right for left, right in zip(state["sources"], baseline["sources"],
                                                    strict=True)
            )
            changed_technical = state["technical"] != baseline["technical"]
            assert changed_sources + changed_technical == int(_overall(case) != SUPPORTED)


@pytest.mark.parametrize("case", get_cases(), ids=lambda case: case["case_id"])
def test_packet_has_exact_evidence_ids_hashes_and_asof_records(case):
    state = case["state"]
    as_of = datetime.fromisoformat(state["as_of"])
    assert as_of.tzinfo is not None
    evidence_ids = [source["source_id"] for source in state["sources"]]
    evidence_ids.append(state["technical"]["evidence_id"])
    assert len(evidence_ids) == len(set(evidence_ids)) == 4
    for source in state["sources"]:
        assert datetime.fromisoformat(source["available_at"]) <= as_of
        if source["capture_status"] == "CAPTURED":
            assert datetime.fromisoformat(source["published_at"]) <= as_of
            assert source["publication_precision"] == "SECOND"
            assert source["excerpt_sha256"] == sha256(source["excerpt"].encode()).hexdigest()
        else:
            assert source["capture_status"] == "MISSING_FROM_PACKET"
            assert source["publication_precision"] == "UNKNOWN"
            assert source["excerpt"] is source["published_at"] is source["excerpt_sha256"] is None
    technical = state["technical"]
    assert datetime.fromisoformat(technical["observed_at"]) <= as_of
    assert technical["context_sha256"] == digest(encoded(technical["context"]))
    context = technical["context"]
    for bar in context["completed_bars"] or []:
        assert datetime.fromisoformat(bar["end"]) < as_of
        assert Decimal(bar["low"]) <= min(Decimal(bar["open"]), Decimal(bar["close"]))
        assert Decimal(bar["high"]) >= max(Decimal(bar["open"]), Decimal(bar["close"]))
    if context["overhead_level_observed_at"] is not None:
        assert datetime.fromisoformat(context["overhead_level_observed_at"]) < as_of


def test_numeric_facts_are_precomputed_from_declared_levels():
    for case in get_cases():
        technical = case["state"]["technical"]
        levels, facts = technical["levels"], technical["deterministic_facts"]
        maximum, stop, target = (
            Decimal(levels[key]) for key in ("maximum_entry", "stop", "target")
        )
        assert Decimal(facts["risk_per_unit_at_maximum_entry"]) == maximum - stop
        assert Decimal(facts["reward_per_unit_at_maximum_entry"]) == target - maximum
        calculated_rr = (target - maximum) / (maximum - stop)
        assert Decimal(facts["reward_risk_at_maximum_entry"]) == calculated_rr
        assert Decimal(facts["quote_age_seconds"]) <= Decimal(facts["maximum_quote_age_seconds"])
        assert Decimal(facts["spread_bps"]) <= Decimal(facts["maximum_spread_bps"])
        assert Decimal(facts["recent_dollar_volume"]) >= Decimal(facts["minimum_dollar_volume"])


def test_blind_state_has_no_author_labels_split_metadata_or_outcomes():
    forbidden_keys = {
        "reference", "expected", "expected_answer", "expected_label", "author_label",
        "provenance", "split", "case_id", "family_id", "future_outcomes", "shouldapprove",
        "ground_truth", "profit", "realized_return",
    }

    def inspect(value):
        if isinstance(value, dict):
            assert not set(value) & forbidden_keys
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    for case in get_cases():
        state = case["state"]
        inspect(state)
        rendered = json.dumps(state)
        assert all(label not in rendered for label in LABELS)
        assert "SYNTHETIC" not in rendered
        assert len(encoded(state).encode()) < 12000


def test_calls_are_independent_and_deterministic():
    original = get_cases()
    expected = encoded(original)
    original[0]["state"]["sources"][0]["excerpt"] = "caller mutation"
    original[0]["reference"]["novelty"] = "caller mutation"
    original[0]["state"]["technical"]["context"]["completed_bars"] = []
    assert encoded(get_cases()) == expected


def test_frozen_dataset_digest():
    # Updating this value requires a new benchmark version and a fresh blind review.
    assert digest(encoded(get_cases())) == (
        "bcbb6adae7d841f2a30e082962dde2c01b03af971b58870711d941697b21d409"
    )
