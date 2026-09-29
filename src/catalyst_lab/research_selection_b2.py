"""Selection rule B2 (``MUSE_JEV_RESEARCH_SELECTION_B2_V1``): pure, no side effects.

A named version under the owner's 2026-09-24 ruling that rules change only as versions
(docs/CONTRACT-RESOLUTIONS.md), in B1's shape: the SKEPTIC components decide and the overall
``verdict`` is recorded as ``dissent`` and never blocks or rejects. B2 reviews with
``SKEPTIC_QUESTIONS_V2``: V1's ``news_stale`` and ``already_priced`` plus
``mechanism_contradicted``, ``inference_labelled`` and ``factual_claims_supported``; V1's
``unsupported_inference``, which answered YES for every compliant catalyst thesis, is not
asked (artifacts/jev-rule-experiment-2026-09-25, artifacts/agent-session-2026-09-24).

Every hard veto of B1 stays: invalid, tied or insufficient component answers and receipt or
binding failures (here and in admission SQL, migration 020); expiry and supersession
(research_cycle); app eligibility, reward/risk below 2 at max entry and the risk limits
(admission and entry); and the owner's QUALITY floor, a category check exactly as B1's. B2
also needs the agent's selection rationale, whose claims are one of its components: an item
without one is NEEDS_REVIEW / RATIONALE_REQUIRED and is never sent to the provider. B1's
shadow disposition is not computable from V2 answers, so a B2 decision records none. No
database, provider or broker access; no trading permission.
"""

from dataclasses import dataclass

from pydantic import ValidationError

from catalyst_lab.jev_contract import (
    INSUFFICIENT,
    JEV_MODEL,
    SKEPTIC_V2,
    digest,
    encoded,
    validated_answers,
)
from catalyst_lab.muse_reports import SelectionRationale
from catalyst_lab.research_evidence import screen_sensitive
from catalyst_lab.research_ranking import QUALITY_CATEGORIES, QUALITY_V2_POLICY
from catalyst_lab.research_selection_b1 import (
    B1_POLICY,
    B2_POLICY,
    RULE_ENV,
    SelectionRule,
    _answer_bearing,
    _tied,
)

QUESTION_SET = SKEPTIC_V2
RATIONALE_REQUIRED = "RATIONALE_REQUIRED"
# Recorded in every B2 decision body instead of a RESEARCH_SHADOW_DISPOSITION event.
SHADOW_NOT_APPLICABLE = "NOT_APPLICABLE_QUESTION_SET"
PASSING = {
    "news_stale": frozenset({"NO"}),
    "already_priced": frozenset({"LOW", "MEDIUM"}),
    "mechanism_contradicted": frozenset({"NO"}),
    "inference_labelled": frozenset({"YES"}),
    "factual_claims_supported": frozenset({"SUPPORTED"}),
}
COMPONENTS = tuple(PASSING)

# Evidence-task requirements. The two V1 requirements are reused verbatim for the two V1
# questions B2 keeps; every new one carries instructions, never a trading instruction.
FETCH_PRIOR_DISCLOSURES = {
    "task": "FETCH_PRIOR_DISCLOSURES",
    "required_fields": [
        "original_source_excerpt",
        "prior_disclosure_excerpt",
        "source_ids",
        "content_hashes",
    ],
}
RESOLVE_RESEARCH_OBJECTION = {
    "task": "RESOLVE_RESEARCH_OBJECTION",
    "required_fields": [
        "new_original_source_excerpt",
        "contradictory_evidence",
        "declared_missing_information",
    ],
}
RESOLVE_CONTRADICTION = {
    "task": "RESOLVE_CONTRADICTION",
    "required_fields": ["sources", "economic_relationship"],
    "instructions": "A supplied excerpt, or a qualification inside one, contradicts the stated "
    "mechanism or a fact it relies on. Supply an original source that resolves the "
    "contradiction, or restate economic_relationship so that every excerpt is consistent "
    "with it.",
}
LABEL_INFERENCE = {
    "task": "LABEL_INFERENCE",
    "required_fields": ["thesis", "economic_relationship", "selection_rationale"],
    "instructions": "Present every step of thesis and economic_relationship that goes beyond "
    "the excerpts as an inference or assumption, resting on facts the cited excerpts state. "
    "A revision is material only with new source content or changed rationale claims or "
    "citations.",
}
RECITE_FACTUAL_CLAIMS = {
    "task": "RECITE_FACTUAL_CLAIMS",
    "required_fields": ["selection_rationale"],
    "instructions": "Every number and fact in a claim must be stated by its cited excerpt or "
    "bar; cite additional bars or sources, or trim the claim.",
}
SUPPLY_SELECTION_RATIONALE = {
    "task": "SUPPLY_SELECTION_RATIONALE",
    "required_fields": ["selection_rationale"],
    "instructions": "Rule B2 reviews the proposer's claims: submit a revision with an "
    "AGENT_SELECTION_RATIONALE_V1 block whose claims cite this revision's sources or the "
    "report's bars.",
}
_COMPONENT_TASKS = {
    "news_stale": FETCH_PRIOR_DISCLOSURES,
    "already_priced": FETCH_PRIOR_DISCLOSURES,
    "mechanism_contradicted": RESOLVE_CONTRADICTION,
    "inference_labelled": LABEL_INFERENCE,
    "factual_claims_supported": RECITE_FACTUAL_CLAIMS,
}


def _component_codes():
    """Every objection code b2_disposition can name for a component, mapped to its task."""
    codes = {RATIONALE_REQUIRED: SUPPLY_SELECTION_RATIONALE}
    for name, task in _COMPONENT_TASKS.items():
        labels = [label for label in QUESTION_SET.questions[name]["criteria"]
                  if label != INSUFFICIENT and label not in PASSING[name]]
        for suffix in (*labels, "INSUFFICIENT", "TIED"):
            codes[name.upper() + "_" + suffix] = task
    return codes


OBJECTION_TASKS = _component_codes()


@dataclass(frozen=True)
class B2Outcome:
    disposition: str  # APPROVED or NEEDS_REVIEW; supersession and expiry override it later.
    reason: str
    reasons: tuple[str, ...]
    dissent: str | None  # The verdict's chosen label: recorded, never blocking.
    dissent_tied: bool | None


def has_rationale(packet):
    """B2 reviews the rationale: the reviewed state must carry one with claims."""
    rationale = (packet.get("state") or {}).get("rationale")
    return isinstance(rationale, dict) and bool(rationale.get("claims"))


def rationale_required():
    """A B2 item without a rationale: never sent to the provider, answerable by a revision."""
    return B2Outcome("NEEDS_REVIEW", RATIONALE_REQUIRED, (RATIONALE_REQUIRED,), None, None)


def b2_disposition(result):
    """B2 on one verified SKEPTIC_QUESTIONS_V2 ReviewResult; the verdict never rejects.

    A reviewer status of NEEDS_REVIEW caused only by the verdict (NEEDS_REVIEW, Insufficient
    evidence or a tie) is still answer-bearing, so the components decide. A component that is
    Insufficient evidence or tied is unresolved; a definite failing label is not passed. Both
    are NEEDS_REVIEW, so the evidence-task loop can supply new evidence.
    """
    if not _answer_bearing(result) or not result.receipt_ids:
        code = result.reason or "MISSING_VALID_REVIEW"
        return B2Outcome("NEEDS_REVIEW", code, (code,), None, None)
    try:
        answers = validated_answers(
            encoded(
                {
                    "model": JEV_MODEL,
                    "answers": result.answers,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            ),
            QUESTION_SET,
        )
    except (ValueError, TypeError):
        return B2Outcome("NEEDS_REVIEW", "INVALID_REVIEW", ("INVALID_REVIEW",), None, None)
    verdict = answers["verdict"]
    dissent, dissent_tied = verdict["choice"], _tied(verdict)
    unresolved, failed = [], []
    for name in COMPONENTS:
        answer, label = answers[name], name.upper()
        if answer["choice"] == INSUFFICIENT:
            unresolved.append(label + "_INSUFFICIENT")
        elif _tied(answer):
            unresolved.append(label + "_TIED")
        elif answer["choice"] not in PASSING[name]:
            failed.append(label + "_" + answer["choice"])
    if unresolved:
        reasons = tuple(unresolved + failed)
        return B2Outcome("NEEDS_REVIEW", "UNRESOLVED_EVIDENCE", reasons, dissent, dissent_tied)
    if failed:
        return B2Outcome(
            "NEEDS_REVIEW", "COMPONENTS_NOT_PASSED", tuple(failed), dissent, dissent_tied
        )
    return B2Outcome(
        "APPROVED", "B2_COMPONENTS_PASSED", ("B2_COMPONENTS_PASSED",), dissent, dissent_tied
    )


def evidence_tasks(reasons):
    """Requirements for the proposing agent, one per distinct objection, in reason order.

    Component objections map to their own requirement; any other code (a provider failure,
    an invalid or unbound receipt, a spent call cap) asks for the generic resolution.
    """
    tasks = []
    for code in reasons:
        task = OBJECTION_TASKS.get(code, RESOLVE_RESEARCH_OBJECTION)
        if task not in tasks:
            tasks.append(task)
    return [{**task, "required_fields": list(task["required_fields"])} for task in tasks]


def decision_fields(outcome, reasons):
    """Fields a B2 RESEARCH_DECISION adds to the common body; B1's shadow is not applicable."""
    return {
        "selection_policy": B2_POLICY,
        "question_set_version": QUESTION_SET.version,
        "dissent": outcome.dissent,
        "dissent_tied": outcome.dissent_tied,
        "reasons": list(reasons),
        "shadow": SHADOW_NOT_APPLICABLE,
        "shadow_policy": B1_POLICY,
    }


def activation_body(rule, *, runtime_id):
    """The audited startup record that lets admission SQL accept this rule's packets."""
    if not rule.b2:
        raise ValueError("B2_ACTIVATION_ONLY")
    return {
        "selection_policy": rule.policy,
        "quality_floor": rule.quality_floor,
        "quality_policy": QUALITY_V2_POLICY,
        "quality_categories": list(QUALITY_CATEGORIES),
        "question_set_version": QUESTION_SET.version,
        "question_set_template_hash": QUESTION_SET.template_hash,
        "runtime_id": str(runtime_id),
        "source": "OWNER_CONFIGURATION_" + RULE_ENV,
    }


def started_rule(rule, activation):
    """RESEARCH_STARTED's ``selection_rule`` block: the cycle's rule for its whole life."""
    return {
        "selection_policy": rule.policy,
        "quality_floor": rule.quality_floor,
        "quality_policy": QUALITY_V2_POLICY,
        "question_set_version": QUESTION_SET.version,
        "activation_event_id": str(activation["event_id"]),
        "activation_event_seq": activation["event_seq"],
    }


def stored_rule(stored):
    """The SelectionRule of a stored B2 ``selection_rule`` block, or SELECTION_RULE_UNAVAILABLE."""
    try:
        if (
            not isinstance(stored, dict)
            or stored.get("selection_policy") != B2_POLICY
            or stored.get("quality_policy") != QUALITY_V2_POLICY
            or stored.get("question_set_version") != QUESTION_SET.version
        ):
            raise ValueError
        return SelectionRule(B2_POLICY, stored.get("quality_floor"))
    except ValueError:
        raise ValueError("SELECTION_RULE_UNAVAILABLE") from None


def canonical_rationale(raw):
    """A revision's AGENT_SELECTION_RATIONALE_V1 block, validated and canonical as at intake.

    References are checked by the caller against the revised state; the privacy screen
    covers every field, the analytics-only confidence included.
    """
    try:
        block = SelectionRationale.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        raise ValueError("SELECTION_RATIONALE_INVALID") from None
    screen_sensitive(encoded(block))
    return block


def claims_fingerprint(rationale):
    """What makes a rationale revision material: its claims' kinds, texts and citations.

    Claim IDs, claim order, the other rationale fields and the analytics-only confidence are
    not material, as retrieval times and labels are not for sources. ``None`` without claims.
    """
    if not isinstance(rationale, dict) or not rationale.get("claims"):
        return None
    claims = sorted(
        (
            claim["kind"],
            claim["text"],
            sorted((claim.get("supported_by") or {}).get("source_ids") or ()),
            sorted((claim.get("supported_by") or {}).get("bar_ids") or ()),
        )
        for claim in rationale["claims"]
    )
    return digest(encoded(claims))
