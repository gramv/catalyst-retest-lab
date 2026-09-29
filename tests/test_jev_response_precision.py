"""JEV_RESPONSE_PRECISION_V1: two-decimal rounding in real jev-1.13.0 bodies is valid."""

import copy
import json

import pytest

from catalyst_lab.jev_contract import (
    CHART_PICK_QUESTIONS,
    PRECISION_VERSION,
    SKEPTIC,
    distribution_sum_tolerance,
    encoded,
    score_tolerance,
    validated_answers,
)
from catalyst_lab.research_ranking import QUALITY_V3, quality_score_v3
from tests.test_jev_review import response_for

# A real MUSE_JEV_COMPARATIVE_QUALITY_V3 body from the 2026-09-27 run, byte for byte as received.
# disproof_quality prints score 1.96 while its printed distribution weighs 1.97: rejected as
# INVALID_SCORE_VALUE before this version, like 21 of the other 24 quality bodies that day.
REAL_QUALITY_V3_BODY = (
    b'{"model":"jev-1.13.0","answers":{"disproof_quality":{"type":"score","score":1.96,'
    b'"confidence":0.94,"legend":{"0":"Vague or not falsifiable.","1":"Partly concrete.",'
    b'"2":"Concrete, bounded, and falsifiable."},"probabilities":{"0":0.0,"1":0.03,"2":0.97}},'
    b'"evidence_support":{"type":"score","score":1.34,"confidence":0.39,"legend":{"0":"Indirect, '
    b'weak, or materially incomplete support.","1":"Mixed support with meaningful limitations.",'
    b'"2":"Direct, specific support for each stated reason."},"probabilities":{"0":0.03,"1":0.6,'
    b'"2":0.37}},"level_rationale":{"type":"score","score":1.94,"confidence":0.91,"legend":{"0":'
    b'"The levels are unexplained or arbitrary.","1":"The levels are explained but only partly '
    b'tied to the evidence.","2":"The levels are tied to identifiable structure in the evidence."},'
    b'"probabilities":{"0":0.0,"1":0.06,"2":0.94}},"quality_category":{"type":"choice","choice":'
    b'"STRONG","confidence":0.49,"probabilities":{"Insufficient evidence":0.0,"ADEQUATE":0.36,'
    b'"WEAK":0.02,"STRONG":0.62}},"timing_specificity":{"type":"score","score":1.32,"confidence":'
    b'0.5,"legend":{"0":"Vague or generic timing.","1":"Specific timing that is only partly '
    b'supported.","2":"Specific timing directly supported by the excerpts or bars."},'
    b'"probabilities":{"0":0.01,"1":0.66,"2":0.33}}},"usage":{"input_tokens":6647,'
    b'"output_tokens":122}}'
)


def test_version_and_tolerances_are_half_a_hundredth_per_printed_number():
    assert PRECISION_VERSION == "JEV_RESPONSE_PRECISION_V1"
    assert distribution_sum_tolerance(4) == pytest.approx(0.02)
    assert distribution_sum_tolerance(3) == pytest.approx(0.015)
    # Levels 0, 1, 2: the score's own rounding plus 1 x and 2 x a probability's rounding.
    assert score_tolerance({"0": 0, "1": 0, "2": 0}) == pytest.approx(0.02)
    assert score_tolerance({"0": 0, "1": 0}) == pytest.approx(0.01)


def test_the_real_quality_body_is_valid_and_kept_exactly_as_received():
    answers = validated_answers(REAL_QUALITY_V3_BODY, QUALITY_V3)
    assert answers == json.loads(REAL_QUALITY_V3_BODY)["answers"]  # Nothing renormalised.
    assert answers["disproof_quality"]["score"] == 1.96
    # The ranking score reads the printed distribution, never the provider's score field.
    assert str(quality_score_v3(answers)) == "82.1250"


@pytest.mark.parametrize("delta, valid", [(0.01, True), (0.02, True), (0.03, False)])
def test_a_score_beyond_rounding_is_still_invalid(delta, valid):
    body = json.loads(REAL_QUALITY_V3_BODY)
    answer = body["answers"]["evidence_support"]
    answer["score"] = round(0.6 + 2 * 0.37 + delta, 2)
    raw = encoded(body).encode()
    if valid:
        assert validated_answers(raw, QUALITY_V3)["evidence_support"]["score"] == answer["score"]
    else:
        with pytest.raises(ValueError, match="^INVALID_SCORE_VALUE$"):
            validated_answers(raw, QUALITY_V3)


@pytest.mark.parametrize("template", [SKEPTIC, CHART_PICK_QUESTIONS])
@pytest.mark.parametrize("excess, valid", [(0.01, True), (-0.01, True), (0.05, False),
                                           (-0.05, False)])
def test_a_distribution_off_by_rounding_is_valid(template, excess, valid):
    body = response_for(template, verdict="REJECT")
    probabilities = body["answers"]["verdict"]["probabilities"]
    runner_up = next(k for k, v in probabilities.items() if v == 0)
    if excess > 0:
        probabilities[runner_up] = excess  # 1.01: seen 5 times in real bodies, 2026-09-25/27.
    else:
        probabilities["REJECT"] = round(1 + excess, 2)  # 0.99.
    raw = encoded(body).encode()
    if valid:
        assert validated_answers(raw, template)["verdict"] == body["answers"]["verdict"]
    else:
        with pytest.raises(ValueError, match="^INVALID_DISTRIBUTION_SUM$"):
            validated_answers(raw, template)


def test_other_checks_are_unchanged():
    body = json.loads(REAL_QUALITY_V3_BODY)
    wrong_choice = copy.deepcopy(body)
    wrong_choice["answers"]["quality_category"]["choice"] = "ADEQUATE"
    with pytest.raises(ValueError, match="^CHOICE_NOT_MAXIMUM$"):
        validated_answers(encoded(wrong_choice).encode(), QUALITY_V3)
    negative = copy.deepcopy(body)
    negative["answers"]["timing_specificity"]["probabilities"]["0"] = -0.01
    with pytest.raises(ValueError, match="^INVALID_PROBABILITY$"):
        validated_answers(encoded(negative).encode(), QUALITY_V3)
