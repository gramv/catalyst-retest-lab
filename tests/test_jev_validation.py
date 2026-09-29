from copy import deepcopy

import pytest

from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, digest, encoded, validated_answers
from catalyst_lab.jev_validation import (
    DIMENSIONS,
    compose_diagnostic,
    diagnostic_questions,
    reconcile_references,
    score_results,
    validate_split,
    verify_blind_binding,
)


def packet():
    return {"sources": [{"source_id": "s1", "excerpt": "Observed evidence."}],
            "technical": {"evidence_id": "t1"}}


def answers(state, overrides=None):
    template = diagnostic_questions(state)
    result = {}
    for key, q in template.questions.items():
        selected = "t1" if key.endswith("_evidence") else "SUPPORTED"
        selected = (overrides or {}).get(key, selected)
        result[key] = {"type": "choice", "choice": selected, "confidence": 1,
                       "probabilities": {k: int(k == selected) for k in q["criteria"]}}
    return validated_answers(encoded({"model": JEV_MODEL, "answers": result,
                                      "usage": {"input_tokens": 1, "output_tokens": 1}}),
                             template)


def test_diagnostic_is_reason_bearing_and_never_authorizes():
    state = packet()
    result = compose_diagnostic(state, answers(state), receipt_valid=True)
    assert result["disposition"] == "SUPPORTED"
    assert result["execution_authority"] is False
    rejection = compose_diagnostic(
        state, answers(state, {"economic_support": "CONTRADICTED"}), receipt_valid=True,
    )
    assert rejection["disposition"] == "CONTRADICTED"
    assert rejection["reasons"] == [{"code": "ECONOMIC_SUPPORT_CONTRADICTED",
                                     "evidence_id": "t1"}]


@pytest.mark.parametrize("overrides", [
    {"technical_coherence": INSUFFICIENT},
    {"technical_coherence_evidence": INSUFFICIENT},
])
def test_uncertain_or_uncited_support_cannot_pass(overrides):
    result = compose_diagnostic(packet(), answers(packet(), overrides), receipt_valid=True)
    assert result["disposition"] == "NEEDS_EVIDENCE"


def test_invalid_receipt_stays_service_failure_not_correct_rejection():
    result = compose_diagnostic(packet(), {}, receipt_valid=False)
    assert result["disposition"] == "PROVIDER_FAILURE"
    assert result["execution_authority"] is False


def test_ties_and_unknown_references_do_not_pass():
    state = packet()
    result = answers(state)
    result["novelty"]["probabilities"] = {
        "SUPPORTED": .5, "CONTRADICTED": .5, INSUFFICIENT: 0,
    }
    assert compose_diagnostic(state, result, receipt_valid=True)["disposition"] == "NEEDS_EVIDENCE"
    result = answers(state)
    result["novelty_evidence"]["choice"] = "not-in-packet"
    with pytest.raises(ValueError, match="UNKNOWN_EVIDENCE"):
        compose_diagnostic(state, result, receipt_valid=True)


def cases():
    return [{"case_id": "a", "family_id": "one", "split": "holdout", "market": "CRYPTO",
             "state": packet(), "reference": dict.fromkeys(DIMENSIONS, "SUPPORTED")}]


def test_disagreement_remains_unadjudicated_and_split_leakage_fails():
    rows = cases()
    second = {"a": {"labels": dict.fromkeys(DIMENSIONS, INSUFFICIENT)}}
    reference = reconcile_references(rows, second, reviewed_states={'a': rows[0]['state']})[0]
    assert reference["agreement"] is False and reference["reference"] is None
    assert reference["human_validated"] is False
    with pytest.raises(ValueError, match="COMPLETE_BLIND"):
        reconcile_references(rows, {}, reviewed_states={'a': rows[0]['state']})
    copied = deepcopy(rows[0])
    copied.update(case_id="b", split="development")
    with pytest.raises(ValueError, match="FAMILY_LEAKAGE"):
        validate_split(rows + [copied])


def test_missing_and_invalid_results_cannot_inflate_semantic_success():
    rows = cases()
    refs = reconcile_references(rows, {"a": {"labels": rows[0]["reference"]}},
                                reviewed_states={"a": rows[0]["state"]})
    results = [{"case_id": "a", "arm": "diagnostic", "provider_valid": False,
                "predicted_group": "PROVIDER_FAILURE"}]
    metrics = score_results(refs, results)["groups"]["diagnostic/holdout/ALL"]
    assert metrics["provider_invalid"] == 1
    assert metrics["semantic_accuracy"] is None
    assert metrics["class_recalls_including_failures"]["SUPPORTED"]["correct"] == 0
    with pytest.raises(ValueError, match="DUPLICATE_VOTE"):
        score_results(refs, results + results)


def test_all_reject_classifier_cannot_claim_good_balanced_accuracy():
    refs = []
    results = []
    for i, label in enumerate(("SUPPORTED", "CONTRADICTED", INSUFFICIENT)):
        refs.append({"case_id": str(i), "split": "holdout", "market": "CRYPTO",
                     "agreement": True, "reference_group": label})
        results.append({"case_id": str(i), "arm": "always-reject", "provider_valid": True,
                        "predicted_group": "CONTRADICTED"})
    metrics = score_results(refs, results)["groups"]["always-reject/holdout/ALL"]
    assert metrics["semantic_accuracy"] == pytest.approx(1/3)
    assert metrics["class_recalls_including_failures"]["SUPPORTED"]["correct"] == 0


def test_missing_case_remains_in_denominator_and_component_scores_include_citations():
    rows = cases()
    other = deepcopy(rows[0])
    other["case_id"] = "b"
    rows.append(other)
    refs = reconcile_references(rows, {r["case_id"]: {
        "labels": r["reference"], "evidence_ids": dict.fromkeys(DIMENSIONS, ["t1"]),
    } for r in rows}, reviewed_states={r["case_id"]: r["state"] for r in rows})
    decision = compose_diagnostic(packet(), answers(packet()), receipt_valid=True)
    results = [{"case_id": "a", "arm": "diagnostic", "provider_valid": True,
                "predicted_group": "SUPPORTED", "decision": decision}]
    metrics = score_results(refs, results)["groups"]["diagnostic/holdout/ALL"]
    assert metrics["missing_results"] == 1
    assert metrics["end_to_end_denominator"] == 2
    assert metrics["semantic_correct"] == 1
    for value in metrics["components"].values():
        assert value["end_to_end_denominator"] == 2
        assert value["raw_correct"] == value["gated_correct"] == 1
        assert value["citation_in_blind_reviewer_set"] == 1


def test_absent_planned_arm_is_counted_without_any_provider_results():
    rows = cases()
    refs = reconcile_references(rows, {"a": {"labels": rows[0]["reference"]}},
                                reviewed_states={"a": rows[0]["state"]})
    metrics = score_results(refs, [], planned_arms=["diagnostic"])["groups"]
    group = metrics["diagnostic/holdout/ALL"]
    assert group["missing_results"] == group["end_to_end_denominator"] == 1
    assert group["semantic_correct"] == 0
    with pytest.raises(ValueError, match="PLANNED_ARMS"):
        score_results(refs, [])


def test_gating_does_not_turn_wrong_raw_labels_into_correct_semantic_judgments():
    rows = cases()
    rows[0]["reference"] = dict.fromkeys(DIMENSIONS, INSUFFICIENT)
    refs = reconcile_references(rows, {"a": {"labels": rows[0]["reference"]}},
                                reviewed_states={"a": rows[0]["state"]})
    decision = compose_diagnostic(packet(), answers(packet(), {
        d + "_evidence": INSUFFICIENT for d in DIMENSIONS
    }), receipt_valid=True)
    result = {"case_id": "a", "arm": "diagnostic", "provider_valid": True,
              "predicted_group": INSUFFICIENT, "decision": decision}
    group = score_results(refs, [result])["groups"]["diagnostic/holdout/ALL"]
    for component in group["components"].values():
        assert component["gated_accuracy"] == 1
        assert component["raw_accuracy"] == 0


def test_changed_packet_cannot_inherit_frozen_independent_labels():
    rows = cases()
    labels = {"a": {"labels": rows[0]["reference"]}}
    blind = encoded([{"case_id": "a", "state": rows[0]["state"]}]).encode()
    raw_labels = encoded(labels).encode()
    provenance = {"blind_packet_sha256": digest(blind),
                  "independent_labels_sha256": digest(raw_labels)}
    assert verify_blind_binding(rows, blind, raw_labels, provenance)[0] == labels
    with pytest.raises(ValueError, match="FILE_HASH_MISMATCH"):
        verify_blind_binding(rows, blind + b" ", raw_labels, provenance)
    rows[0]["state"]["sources"][0]["excerpt"] = "Revised after independent review."
    with pytest.raises(ValueError, match="STATE_MISMATCH"):
        verify_blind_binding(rows, blind, raw_labels, provenance)
