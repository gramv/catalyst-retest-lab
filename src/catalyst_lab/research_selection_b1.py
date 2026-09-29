"""Selection rule B1 (``MUSE_JEV_RESEARCH_SELECTION_B1_V1``): pure, no side effects.

A named version under the owner's 2026-09-24 ruling that rules change only as versions
(docs/CONTRACT-RESOLUTIONS.md). The three SKEPTIC component answers decide; the overall
``verdict`` is recorded as ``dissent`` and cannot block, because questions in one TypeSafe
request cannot see one another's answers (pinned skill). Every hard veto stays: invalid,
tied or insufficient component answers and receipt or binding failures (here and in
admission SQL); expiry and supersession (research_cycle); app eligibility, reward/risk
below 2 at max entry and the risk limits (admission and entry); and, when B1 is active, the
owner's QUALITY floor, a category check (research_ranking). ``MUSE_JEV_RESEARCH_SELECTION_V2``
stays the default and unchanged. B1 is always computed in shadow; only the owner's
configuration activates it. No database, provider or broker access; no trading permission.
"""

from dataclasses import dataclass

from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, SKEPTIC, encoded, validated_answers
from catalyst_lab.research_ranking import QUALITY_CATEGORIES, QUALITY_V2_POLICY

V2_POLICY = "MUSE_JEV_RESEARCH_SELECTION_V2"
B1_POLICY = "MUSE_JEV_RESEARCH_SELECTION_B1_V1"
# Selection rule B2 (research_selection_b2) is configured through the same switch; its rule,
# question set and activation live in that module. Named here only so SelectionRule, which
# every runtime reads, can accept it without importing the rule itself.
B2_POLICY = "MUSE_JEV_RESEARCH_SELECTION_B2_V1"
SELECTION_RULES = (V2_POLICY, B1_POLICY, B2_POLICY)
FLOOR_POLICIES = (B1_POLICY, B2_POLICY)  # Rules that require the owner's QUALITY floor.
RULE_ENV = "MANAGED_SELECTION_RULE"
FLOOR_ENV = "MANAGED_SELECTION_QUALITY_FLOOR"
ACTIVATION_KIND = "RESEARCH_SELECTION_RULE_ACTIVATED"
SHADOW_KIND = "RESEARCH_SHADOW_DISPOSITION"
# The passing component answers are exactly V2's; the verdict is not a component.
PASSING = {
    "news_stale": frozenset({"NO"}),
    "unsupported_inference": frozenset({"NO"}),
    "already_priced": frozenset({"LOW", "MEDIUM"}),
}
COMPONENTS = tuple(PASSING)


@dataclass(frozen=True)
class SelectionRule:
    """The configured rule: V2 (default, no floor), or B1 or B2 with the owner's floor category."""

    policy: str = V2_POLICY
    quality_floor: str | None = None

    def __post_init__(self):
        if self.policy in FLOOR_POLICIES:
            if self.quality_floor not in QUALITY_CATEGORIES:
                raise ValueError("SELECTION_QUALITY_FLOOR_REQUIRED")
        elif self.policy != V2_POLICY:
            raise ValueError("UNKNOWN_SELECTION_RULE")
        elif self.quality_floor is not None:
            # The code predates B2 and is kept: a floor needs a floor rule (B1 or B2).
            raise ValueError("SELECTION_QUALITY_FLOOR_REQUIRES_B1")

    @property
    def b1(self):
        return self.policy == B1_POLICY

    @property
    def b2(self):
        return self.policy == B2_POLICY

    @property
    def floored(self):
        """B1 and B2: every approval needs a QUALITY_V2 category at or above the floor."""
        return self.policy in FLOOR_POLICIES


def selection_rule_from_env(environ):
    """Exact values only: ``MANAGED_SELECTION_RULE`` absent or empty means V2.

    B1 and B2 require ``MANAGED_SELECTION_QUALITY_FLOOR`` = WEAK, ADEQUATE or STRONG; V2
    refuses a floor, so a staged floor cannot be mistaken for an active one.
    """
    return SelectionRule(environ.get(RULE_ENV) or V2_POLICY, environ.get(FLOOR_ENV) or None)


@dataclass(frozen=True)
class B1Outcome:
    disposition: str  # APPROVED or NEEDS_REVIEW; supersession and expiry override it later.
    reason: str
    reasons: tuple[str, ...]
    dissent: str | None  # The verdict's chosen label: recorded, never blocking.
    dissent_tied: bool | None


def _tied(answer):
    top = max(answer["probabilities"].values())
    return sum(value == top for value in answer["probabilities"].values()) != 1


def _answer_bearing(result):
    # The same predicate as research_cycle._answer_bearing_result, which V2 keeps using.
    return result.status == "RECORDED" or (
        result.status == "NEEDS_REVIEW" and result.reason == "UNCERTAIN_JUDGMENT"
    )


def b1_disposition(result):
    """B1 on one verified SKEPTIC ReviewResult; REJECTED is never produced by the verdict.

    A reviewer status of NEEDS_REVIEW caused only by the verdict (NEEDS_REVIEW, Insufficient
    evidence or a tie) is still answer-bearing, so the components decide. A component that is
    Insufficient evidence or tied is unresolved; a definite failing label is not passed. Both
    are NEEDS_REVIEW, so the existing evidence-task loop can supply new evidence.
    """
    if not _answer_bearing(result) or not result.receipt_ids:
        code = result.reason or "MISSING_VALID_REVIEW"
        return B1Outcome("NEEDS_REVIEW", code, (code,), None, None)
    try:
        answers = validated_answers(
            encoded(
                {
                    "model": JEV_MODEL,
                    "answers": result.answers,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            ),
            SKEPTIC,
        )
    except (ValueError, TypeError):
        return B1Outcome("NEEDS_REVIEW", "INVALID_REVIEW", ("INVALID_REVIEW",), None, None)
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
        return B1Outcome("NEEDS_REVIEW", "UNRESOLVED_EVIDENCE", reasons, dissent, dissent_tied)
    if failed:
        return B1Outcome(
            "NEEDS_REVIEW", "COMPONENTS_NOT_PASSED", tuple(failed), dissent, dissent_tied
        )
    return B1Outcome(
        "APPROVED", "B1_COMPONENTS_PASSED", ("B1_COMPONENTS_PASSED",), dissent, dissent_tied
    )


def activation_body(rule, *, runtime_id):
    """The audited startup record that lets admission SQL accept this rule's packets."""
    if not rule.b1:
        raise ValueError("B1_ACTIVATION_ONLY")
    return {
        "selection_policy": rule.policy,
        "quality_floor": rule.quality_floor,
        "quality_policy": QUALITY_V2_POLICY,
        "quality_categories": list(QUALITY_CATEGORIES),
        "runtime_id": str(runtime_id),
        "source": "OWNER_CONFIGURATION_" + RULE_ENV,
    }


def started_rule(rule, activation):
    """RESEARCH_STARTED's ``selection_rule`` block: the cycle's rule for its whole life."""
    return {
        "selection_policy": rule.policy,
        "quality_floor": rule.quality_floor,
        "quality_policy": QUALITY_V2_POLICY,
        "activation_event_id": str(activation["event_id"]),
        "activation_event_seq": activation["event_seq"],
    }


def shadow_key(receipt_id):
    """One shadow disposition per final SKEPTIC receipt; earlier attempts are coalesced."""
    return f"research:shadow:{B1_POLICY}:{receipt_id}"


def shadow_body(
    *, packet, request_id, receipt_ids, active_policy, disposition, reasons, outcome
):
    """B1's disposition for one decision, never read by publication or admission.

    Keyed by ``research_cycle_id`` rather than ``cycle_id``, so it is outside every cycle's
    working set and the agent-facing cycle feed; the global event feed still carries it.
    """
    return {
        "research_cycle_id": str(packet["cycle_id"]),
        "item_key": packet["item_key"],
        "revision": packet["revision"],
        "evidence_hash": packet["evidence_hash"],
        "policy": B1_POLICY,
        "active_policy": active_policy,
        "disposition": disposition,
        "reasons": list(reasons),
        "dissent": outcome.dissent,
        "dissent_tied": outcome.dissent_tied,
        "request_id": request_id,
        "receipt_id": receipt_ids[-1],
        "receipt_ids": list(receipt_ids),
        "admissible": False,
    }
