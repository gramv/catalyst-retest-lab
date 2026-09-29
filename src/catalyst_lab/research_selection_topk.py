"""Selection rule ``JEV_TOP_K_SELECTION_V1`` (plan phase 2): pure, no side effects.

A named version under the owner's 2026-09-24 ruling that rules change only as versions
(docs/CONTRACT-RESOLUTIONS.md), for ``AGENT_RESEARCH_REPORT_V3`` reports only. Owner decisions
of 2026-09-26 (docs/CRYPTO-AGENT-LOOP.md 4.2): Jev reads every pick exactly as the agent sent
it (never the agent's name or confidence), vetoes a pick only for a definite "wrong" answer,
lets uncertain answers only lower its score, ranks every pick it did not veto, and the system
keeps its best K (5-10, default 10) every run; the independent system check comes after this
selection and replaces failed picks from the same ranking.

Each pick gets one review with its kind's question set (``NEWS_PICK_QUESTIONS_V1``,
``CHART_PICK_QUESTIONS_V1`` or ``BOTH_PICK_QUESTIONS_V1``, jev_contract) and one
``MUSE_JEV_COMPARATIVE_QUALITY_V3`` review (research_ranking, a 0-100 score and a category).

* **Veto**: a component's veto label is its unique most probable answer (``VETO``).
* **Uncertain**: Insufficient evidence, a tie, ``already_priced`` HIGH,
  ``factual_claims_supported`` PARTIALLY_SUPPORTED and any other non-passing label that is not
  a veto. ``adjusted_score = max(0, quality_score - 10 x uncertain_count)`` (an implementation
  choice the owner can change as a new version).
* **Dissent**: the ``verdict`` is recorded with its tie flag and never blocks.
* A failed, invalid, missing or unbound review is NOT_RANKED with its own code (never a veto).
* **Ranking**: adjusted score descending, then quality category (STRONG > ADEQUATE > WEAK >
  Insufficient), then the earlier final review receipt, then the agent's own item order.

Admission SQL (migration 021, ``lab.managed_review_failure_topk``) re-derives the veto, the
uncertain codes, both scores and the ranking binding from the stored receipts and events. No
database, provider or broker access here; no trading permission.

**``JEV_TOP_K_SELECTION_V2``** (named version, 2026-09-27; owner "ok do it" after the live check
in artifacts/news-stale-check-2026-09-27). Everything is V1's except two things:

* NEWS and BOTH picks are reviewed with ``NEWS_PICK_QUESTIONS_V2`` / ``BOTH_PICK_QUESTIONS_V2``,
  whose ``news_stale`` asks whether the catalyst was first made public more than 48 hours before
  the pick's ``agent_price_at`` (V1's asks about "supplied prior disclosures", which a V3 pick
  never has). CHART picks keep ``CHART_PICK_QUESTIONS_V1``.
* A veto label vetoes only when it is the unique most probable answer AND its probability is at
  least ``VETO_MIN_PROBABILITY`` (0.70) in the stored response bytes: the plan's "vetoed only for
  a definite wrong answer" (docs/CRYPTO-AGENT-LOOP.md 4.2). A unique most probable veto label
  below 0.70 is uncertain ``<Q>_<LABEL>_UNSURE`` and costs 10 points like any other uncertain
  component. Every veto in the first real runs (16 of 16) was between 0.51 and 0.70.

V1 keeps its definition, question sets, SQL branch and every stored event byte for byte; a cycle
keeps the rule its RESEARCH_STARTED records. Admission SQL for V2: migration 023
(``lab.managed_review_failure_topk_v2``).
"""

from dataclasses import dataclass
from decimal import Decimal

from catalyst_lab.jev_contract import (
    BOTH_PICK_QUESTIONS,
    BOTH_PICK_QUESTIONS_V2,
    CHART_PICK_QUESTIONS,
    INSUFFICIENT,
    JEV_MODEL,
    NEWS_PICK_QUESTIONS,
    NEWS_PICK_QUESTIONS_V2,
    encoded,
    strict_json,
    validated_answers,
)
from catalyst_lab.research_ranking import (
    QUALITY_CATEGORIES,
    QUALITY_V3,
    QUALITY_V3_POLICY,
    QUALITY_V3_QUANTUM,
    quality_category,
    quality_score_v3,
)
from catalyst_lab.research_selection_b1 import (
    FLOOR_ENV,
    RULE_ENV,
    _answer_bearing,
    _tied,
)
from catalyst_lab.research_selection_b1 import (
    selection_rule_from_env as selection_rule_before_topk,
)

TOPK_POLICY = "JEV_TOP_K_SELECTION_V1"
TOPK_POLICY_V2 = "JEV_TOP_K_SELECTION_V2"
TOPK_POLICIES = frozenset({TOPK_POLICY, TOPK_POLICY_V2})
# V2 only: a veto label vetoes at this probability or above; below it is uncertain _UNSURE.
VETO_MIN_PROBABILITY = Decimal("0.70")
UNSURE_SUFFIX = "_UNSURE"
TOPK_ENV = "MANAGED_TOPK_SELECTION_JSON"
DEFAULT_K = 10
MIN_K, MAX_K = 5, 10
UNCERTAIN_PENALTY = Decimal(10)
RANKING_KIND = "RESEARCH_RANKING"
SKIPPED_KIND = "RESEARCH_SELECTION_SKIPPED"
# Ranking entry statuses, in the order entries are listed after the ranked ones.
RANKED, VETOED, NOT_RANKED = "RANKED", "VETOED", "NOT_RANKED"
STATUSES = (RANKED, VETOED, NOT_RANKED)
# RESEARCH_DECISION dispositions of a top-K review (the ranking decides RANKED).
RANKABLE = "RANKABLE"
# RESEARCH_QUALITY statuses of a QUALITY_V3 review.
SCORED, NOT_SCORED = "SCORED", "NOT_SCORED"
# Codes.
REPORT_V3_REQUIRED = "REPORT_V3_REQUIRED"
EVIDENCE_REVISION_NOT_APPLICABLE = "EVIDENCE_REVISION_NOT_APPLICABLE"
REVIEW_DEADLINE_PASSED = "REVIEW_DEADLINE_PASSED"
DUPLICATE_SYMBOL_IN_RUN = "DUPLICATE_SYMBOL_IN_RUN"
TOPK_INVALID = "SELECTION_TOPK_INVALID"
FLOOR_NOT_APPLICABLE = "SELECTION_QUALITY_FLOOR_NOT_APPLICABLE"
PASSED, UNCERTAIN, VETO_WON = (
    "TOPK_COMPONENTS_PASSED", "TOPK_COMPONENTS_UNCERTAIN", "TOPK_VETOED"
)
# Admission SQL refusal codes added by migration 021 (permanent: the stored selection can
# never overcome them). Every other code of the branch is an existing one.
SQL_REFUSALS = ("TOPK_VETOED", "TOPK_SCORE_MISMATCH", "TOPK_RANKING_BINDING_FAILURE")
# Replacement rule TOPK_REPLACEMENT_V1 (package replacement, plan phase 3b, 2026-09-27): a
# top-K pick declined at admission is replaced by the cycle's next-ranked pick that
# ``ResearchCycle.publish_ranked`` accepts; one RESEARCH_REPLACEMENT records each decision.
REPLACEMENT_RULE = "TOPK_REPLACEMENT_V1"
REPLACEMENT_KIND = "RESEARCH_REPLACEMENT"
REPLACEMENT_PUBLISHED, REPLACEMENT_EXHAUSTED = "PUBLISHED", "EXHAUSTED"
RANKING_EXHAUSTED = "TOPK_RANKING_EXHAUSTED"
# publish_ranked's refusals of one entry: the walk records the code and moves to the next
# entry. Every other refusal is about the cycle, not the entry, and stops the decision.
PASSED_OVER_REFUSALS = frozenset({"TOPK_ENTRY_SKIPPED", DUPLICATE_SYMBOL_IN_RUN, "REVIEW_EXPIRED",
                                  "TOPK_RANKING_BINDING_FAILURE"})

QUESTION_SETS = {"NEWS": NEWS_PICK_QUESTIONS, "CHART": CHART_PICK_QUESTIONS,
                 "BOTH": BOTH_PICK_QUESTIONS}
QUESTION_SETS_V2 = {"NEWS": NEWS_PICK_QUESTIONS_V2, "CHART": CHART_PICK_QUESTIONS,
                    "BOTH": BOTH_PICK_QUESTIONS_V2}
POLICY_QUESTION_SETS = {TOPK_POLICY: QUESTION_SETS, TOPK_POLICY_V2: QUESTION_SETS_V2}
# The probability a veto label needs (None: the unique most probable answer is enough).
VETO_THRESHOLDS = {TOPK_POLICY: None, TOPK_POLICY_V2: VETO_MIN_PROBABILITY}
# Pinned here, in migrations 021 and 023 and in tests/test_selection_topk*.py.
TEMPLATE_HASHES = {
    "NEWS_PICK_QUESTIONS_V1": "cabd489b5021c7dc1f201db918ad2e772558821a201e52570cb8f0094b105a0e",
    "CHART_PICK_QUESTIONS_V1": "5082d2dd329badd9e27d1f487aa01027ac32bedebc5b95a77e518de7a41652bc",
    "BOTH_PICK_QUESTIONS_V1": "7bb2871752c4eb6cd4fe20c160687af6821e65daeeea5928b277fd869865e4b9",
    "NEWS_PICK_QUESTIONS_V2": "5fe5d168d6289e67fc24062fbb389b65c7e76875116d2518935fe60cebb61f84",
    "BOTH_PICK_QUESTIONS_V2": "f010bd3fcbca186ed713fc82de64802961e7b6c0d0eb9cc082c69a3b9b7a254e",
    QUALITY_V3_POLICY: "a9675c4ef501db83ed19e4fbfe07d22e55ee8daa60d45ef58fb6a372ab322f78",
}
# The components of each kind, in the order their codes are listed (and SQL loops).
COMPONENTS = {
    "NEWS": ("news_stale", "already_priced", "mechanism_contradicted",
             "factual_claims_supported", "prices_consistent"),
    "CHART": ("levels_supported_by_bars", "setup_already_broken", "factual_claims_supported",
              "prices_consistent"),
    "BOTH": ("news_stale", "already_priced", "mechanism_contradicted",
             "levels_supported_by_bars", "setup_already_broken", "factual_claims_supported",
             "prices_consistent"),
}
PASSING = {
    "news_stale": frozenset({"NO"}),
    "already_priced": frozenset({"LOW", "MEDIUM"}),
    "mechanism_contradicted": frozenset({"NO"}),
    "factual_claims_supported": frozenset({"SUPPORTED"}),
    "prices_consistent": frozenset({"YES"}),
    "levels_supported_by_bars": frozenset({"YES"}),
    "setup_already_broken": frozenset({"NO"}),
}
# A definite "wrong" answer. already_priced has none: HIGH only lowers the score.
VETO = {
    "news_stale": "YES",
    "mechanism_contradicted": "YES",
    "factual_claims_supported": "UNSUPPORTED",
    "prices_consistent": "NO",
    "levels_supported_by_bars": "NO",
    "setup_already_broken": "YES",
}
# Ranking tie-break 2: STRONG > ADEQUATE > WEAK > Insufficient (no category).
CATEGORY_ORDER = {None: 0, **{category: index + 1
                              for index, category in enumerate(QUALITY_CATEGORIES)}}


def is_topk_policy(policy):
    """True for either top-K version's policy name."""
    return policy in TOPK_POLICIES


def _policy_sets(policy):
    if policy not in POLICY_QUESTION_SETS:
        raise ValueError("UNKNOWN_SELECTION_RULE")
    return POLICY_QUESTION_SETS[policy]


def question_set(kind, policy=TOPK_POLICY):
    """The pick kind's question set under ``policy``; an unknown kind has none."""
    sets = _policy_sets(policy)
    if kind not in sets:
        raise ValueError("PICK_KIND_UNKNOWN")
    return sets[kind]


def question_set_versions(policy=TOPK_POLICY):
    return {kind: {"version": qs.version, "template_hash": TEMPLATE_HASHES[qs.version]}
            for kind, qs in _policy_sets(policy).items()}


# --- Configuration --------------------------------------------------------------------------


@dataclass(frozen=True)
class TopKRule:
    """The configured top-K rule: K picks per report (5-10, default 10), no quality floor."""

    policy: str = TOPK_POLICY
    k: int = DEFAULT_K

    def __post_init__(self):
        if self.policy not in TOPK_POLICIES:
            raise ValueError("UNKNOWN_SELECTION_RULE")
        if type(self.k) is not int or not MIN_K <= self.k <= MAX_K:
            raise ValueError(TOPK_INVALID)

    @property
    def veto_min_probability(self):
        """V2's 0.70; None under V1 (the unique most probable veto label is enough)."""
        return VETO_THRESHOLDS[self.policy]

    # The attributes research_cycle reads from a SelectionRule: top-K has no floor.
    quality_floor = None
    b1 = b2 = floored = False
    topk = True


def is_topk(rule):
    return isinstance(rule, TopKRule)


def k_from_json(text):
    """``MANAGED_TOPK_SELECTION_JSON``: exactly ``{"k": N}`` with an integer 5-10.

    ``None`` (absent) gives None; any other value, an empty string included, must parse.
    """
    if text is None:
        return None
    try:
        value = strict_json(text)
        if not isinstance(value, dict) or set(value) != {"k"}:
            raise ValueError
        k = value["k"]
        if type(k) is not int or not MIN_K <= k <= MAX_K:
            raise ValueError
        return k
    except (ValueError, TypeError):
        raise ValueError(TOPK_INVALID) from None


def selection_rule_from_env(environ):
    """``MANAGED_SELECTION_RULE`` = ``JEV_TOP_K_SELECTION_V1`` or ``JEV_TOP_K_SELECTION_V2``
    gives that top-K rule with K from ``MANAGED_TOPK_SELECTION_JSON`` (default 10) and refuses a
    quality floor; every other value is read by the V2/B1/B2 function, unchanged. K is
    validated whenever it is present and applies only to top-K, so the deploy example can carry
    it while the rule is the owner's.
    """
    k = k_from_json(environ.get(TOPK_ENV))
    policy = environ.get(RULE_ENV)
    if policy in TOPK_POLICIES:
        if environ.get(FLOOR_ENV):
            raise ValueError(FLOOR_NOT_APPLICABLE)
        return TopKRule(policy, DEFAULT_K if k is None else k)
    return selection_rule_before_topk(environ)


def _threshold_fields(policy):
    """V2 records its veto threshold; V1's bodies keep exactly their original keys."""
    threshold = VETO_THRESHOLDS[policy]
    return {} if threshold is None else {"veto_min_probability": str(threshold)}


def activation_body(rule, *, runtime_id):
    """The audited startup record admission SQL requires before a top-K cycle."""
    if not is_topk(rule):
        raise ValueError("TOPK_ACTIVATION_ONLY")
    return {
        "selection_policy": rule.policy,
        "k": rule.k,
        "question_sets": question_set_versions(rule.policy),
        "quality_policy": QUALITY_V3_POLICY,
        "quality_template_hash": TEMPLATE_HASHES[QUALITY_V3_POLICY],
        "quality_categories": list(QUALITY_CATEGORIES),
        "uncertain_penalty": str(UNCERTAIN_PENALTY),
        **_threshold_fields(rule.policy),
        "runtime_id": str(runtime_id),
        "source": "OWNER_CONFIGURATION_" + RULE_ENV,
    }


def started_rule(rule, activation):
    """RESEARCH_STARTED's ``selection_rule`` block: the cycle's rule for its whole life."""
    return {
        "selection_policy": rule.policy,
        "k": rule.k,
        "question_sets": question_set_versions(rule.policy),
        "quality_policy": QUALITY_V3_POLICY,
        **_threshold_fields(rule.policy),
        "activation_event_id": str(activation["event_id"]),
        "activation_event_seq": activation["event_seq"],
    }


def stored_rule(stored):
    """The TopKRule of a stored ``selection_rule`` block, or SELECTION_RULE_UNAVAILABLE."""
    try:
        policy = stored.get("selection_policy") if isinstance(stored, dict) else None
        if (
            policy not in TOPK_POLICIES
            or stored.get("quality_policy") != QUALITY_V3_POLICY
            or stored.get("question_sets") != question_set_versions(policy)
            or stored.get("veto_min_probability")
            != _threshold_fields(policy).get("veto_min_probability")
        ):
            raise ValueError
        return TopKRule(policy, stored.get("k"))
    except ValueError:
        raise ValueError("SELECTION_RULE_UNAVAILABLE") from None


# --- One pick's reviews -----------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewOutcome:
    """A pick's kind review under top-K: RANKABLE, VETOED or NOT_RANKED (with its code)."""

    status: str
    reason: str
    veto_reasons: tuple[str, ...]
    uncertain: tuple[str, ...]
    dissent: str | None
    dissent_tied: bool | None
    question_set_version: str

    @property
    def reasons(self):
        if self.status == NOT_RANKED:
            return (self.reason,)
        return self.veto_reasons + self.uncertain or (PASSED,)


def not_ranked(code, kind, policy=TOPK_POLICY):
    return ReviewOutcome(NOT_RANKED, code, (), (), None, None,
                         question_set(kind, policy).version)


def _veto_probability(name, answer, decimal_answers):
    """The chosen veto label's probability as the stored bytes print it (exact decimal)."""
    choice = answer["choice"]
    if decimal_answers is not None:
        return Decimal(decimal_answers[name]["probabilities"][choice])
    return Decimal(str(answer["probabilities"][choice]))


def assess_review(kind, result, policy=TOPK_POLICY, decimal_answers=None):
    """Top-K on one verified review of the pick kind's question set under ``policy``.

    Per component, in order: Insufficient evidence chosen is ``<Q>_INSUFFICIENT``; a tie for
    the top probability is ``<Q>_TIED``; the veto label as the unique most probable answer is
    a veto ``<Q>_<LABEL>`` (under V2 only when its probability is at least 0.70, else
    uncertain ``<Q>_<LABEL>_UNSURE``); a passing label passes; any other label is uncertain
    ``<Q>_<LABEL>``. The verdict is dissent only. A review without valid answers is
    NOT_RANKED with its own code. ``decimal_answers`` are the receipt's answers parsed with
    exact decimals, so V2's threshold compares the probability admission SQL reads.
    """
    questions = question_set(kind, policy)
    threshold = VETO_THRESHOLDS[policy]
    if not _answer_bearing(result) or not result.receipt_ids:
        return not_ranked(result.reason or "MISSING_VALID_REVIEW", kind, policy)
    try:
        answers = validated_answers(
            encoded({"model": JEV_MODEL, "answers": result.answers,
                     "usage": {"input_tokens": 0, "output_tokens": 0}}),
            questions,
        )
    except (ValueError, TypeError):
        return not_ranked("INVALID_REVIEW", kind, policy)
    verdict = answers["verdict"]
    veto, uncertain = [], []
    for name in COMPONENTS[kind]:
        answer, label = answers[name], name.upper()
        if answer["choice"] == INSUFFICIENT:
            uncertain.append(label + "_INSUFFICIENT")
        elif _tied(answer):
            uncertain.append(label + "_TIED")
        elif answer["choice"] == VETO.get(name):
            try:
                definite = (threshold is None
                            or _veto_probability(name, answer, decimal_answers) >= threshold)
            except (KeyError, TypeError, ArithmeticError):
                return not_ranked("INVALID_REVIEW", kind, policy)
            if definite:
                veto.append(label + "_" + answer["choice"])
            else:
                uncertain.append(label + "_" + answer["choice"] + UNSURE_SUFFIX)
        elif answer["choice"] not in PASSING[name]:
            uncertain.append(label + "_" + answer["choice"])
    if veto:
        status, reason = VETOED, VETO_WON
    else:
        status, reason = RANKABLE, UNCERTAIN if uncertain else PASSED
    return ReviewOutcome(status, reason, tuple(veto), tuple(uncertain), verdict["choice"],
                         _tied(verdict), questions.version)


@dataclass(frozen=True)
class QualityOutcome:
    """A pick's QUALITY_V3 review: SCORED (a 0-100 score, a category or None) or NOT_SCORED."""

    status: str
    reason: str | None
    score: Decimal | None
    category: str | None


def assess_quality(result, decimal_answers=None):
    """QUALITY_V3 on one verified review. An Insufficient-evidence or tied category is still
    scored (it sorts last among equal scores); a review without valid answers is NOT_SCORED.

    ``decimal_answers`` are the receipt's answers parsed with exact decimals, so the score is
    the one admission SQL computes from the same bytes.
    """
    if not _answer_bearing(result) or not result.receipt_ids:
        return QualityOutcome(NOT_SCORED, result.reason or "MISSING_VALID_REVIEW", None, None)
    try:
        answers = validated_answers(
            encoded({"model": JEV_MODEL, "answers": result.answers,
                     "usage": {"input_tokens": 0, "output_tokens": 0}}),
            QUALITY_V3,
        )
        score = quality_score_v3(decimal_answers if decimal_answers is not None else answers)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return QualityOutcome(NOT_SCORED, "INVALID_REVIEW", None, None)
    return QualityOutcome(SCORED, None, score, quality_category(answers))


def adjusted_score(score, uncertain_count):
    """``max(0, quality_score - 10 x uncertain_count)`` on the four-decimal score."""
    return max(Decimal(0), score - UNCERTAIN_PENALTY * uncertain_count).quantize(
        QUALITY_V3_QUANTUM)


def score_text(value):
    return None if value is None else str(value)


# --- The ranking ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Assessed:
    """Everything the ranking needs about one pick; ``entry`` is what it records."""

    item_key: str
    revision: int
    symbol: str
    kind: str
    agent_rank: int
    review: ReviewOutcome
    quality: QualityOutcome
    receipt_id: str | None
    receipt_seq: int | None  # The final review receipt's audit sequence (tie-break 3).
    quality_receipt_id: str | None

    @property
    def status(self):
        if self.review.status == VETOED:
            return VETOED
        if self.review.status != RANKABLE:
            return NOT_RANKED
        return RANKED if self.quality.status == SCORED else NOT_RANKED

    @property
    def reason(self):
        """A NOT_RANKED entry's own code; a failed QUALITY review's is prefixed QUALITY_."""
        if self.status != NOT_RANKED:
            return None
        if self.review.status == NOT_RANKED:
            return self.review.reason
        code = self.quality.reason or "MISSING_VALID_REVIEW"
        return code if code.startswith("QUALITY_") else "QUALITY_" + code

    @property
    def adjusted(self):
        if self.review.status not in (RANKABLE, VETOED) or self.quality.status != SCORED:
            return None
        return adjusted_score(self.quality.score, len(self.review.uncertain))

    def entry(self, rank):
        return {
            "item_key": self.item_key,
            "revision": self.revision,
            "rank": rank,
            "adjusted_score": score_text(self.adjusted),
            "quality_score": score_text(self.quality.score),
            "quality_category": self.quality.category,
            "status": self.status,
            "veto_reasons": list(self.review.veto_reasons),
            "uncertain": list(self.review.uncertain),
            "dissent": self.review.dissent,
            "receipt_id": self.receipt_id,
            "quality_receipt_id": self.quality_receipt_id,
            # Additional fields: the pick, its question set and a NOT_RANKED entry's code.
            "symbol": self.symbol,
            "kind": self.kind,
            "question_set_version": self.review.question_set_version,
            "dissent_tied": self.review.dissent_tied,
            "agent_rank": self.agent_rank,
            "reason": self.reason,
        }


def ranking_key(assessed):
    """Adjusted score descending, category, earlier final receipt, then the agent's order."""
    return (-assessed.adjusted, -CATEGORY_ORDER[assessed.quality.category],
            assessed.receipt_seq, assessed.agent_rank)


def rank_entries(assessed):
    """Every pick's entry: RANKED picks by rank 1..n, then VETOED, then NOT_RANKED, the last
    two in the agent's own order."""
    ranked = sorted((a for a in assessed if a.status == RANKED), key=ranking_key)
    entries = [a.entry(index) for index, a in enumerate(ranked, 1)]
    for status in (VETOED, NOT_RANKED):
        entries += [a.entry(None) for a in sorted(assessed, key=lambda a: a.agent_rank)
                    if a.status == status]
    return entries


def ranking_body(*, cycle_id, run_slot, k, entries, complete, policy=TOPK_POLICY):
    """The cycle's one RESEARCH_RANKING (key ``research:ranking:<cycle_id>``)."""
    return {
        "policy": policy,
        "cycle_id": str(cycle_id),
        "run_slot": run_slot,
        "k": k,
        "entries": entries,
        "quality_policy": QUALITY_V3_POLICY,
        "uncertain_penalty": str(UNCERTAIN_PENALTY),
        # False when the ranking was written at the review deadline with a pick unreviewed.
        "complete": complete,
        "counts": {status: sum(entry["status"] == status for entry in entries)
                   for status in STATUSES},
    }


def ranking_key_for(cycle_id):
    return f"research:ranking:{cycle_id}"


def replacement_key_for(cycle_id, item_key, revision):
    """The one RESEARCH_REPLACEMENT of a declined pick: keyed by its cycle and item."""
    return f"research:{cycle_id}:{item_key}:{revision}:replacement"


def decision_fields(outcome, kind, policy=TOPK_POLICY):
    """Fields a top-K RESEARCH_DECISION adds to the common body; no evidence task exists."""
    return {
        "selection_policy": policy,
        "question_set_version": outcome.question_set_version,
        "pick_kind": kind,
        "dissent": outcome.dissent,
        "dissent_tied": outcome.dissent_tied,
        "reasons": list(outcome.reasons),
        "veto_reasons": list(outcome.veto_reasons),
        "uncertain": list(outcome.uncertain),
        "evidence_tasks": [],
    }


def selected_fields(entry, ranking, *, k, agent_rank, quality_receipt_ids, replacement_for,
                    policy=TOPK_POLICY):
    """The fields a published top-K packet adds to the common selected body.

    ``rank`` is Jev's rank; the pick's own item order stays available as ``agent_rank``.
    """
    return {
        "selection_policy": policy,
        "question_set_version": entry["question_set_version"],
        "rank": entry["rank"],
        "agent_rank": agent_rank,
        "adjusted_score": entry["adjusted_score"],
        "quality_score": entry["quality_score"],
        "quality_category": entry["quality_category"],
        "uncertain": list(entry["uncertain"]),
        "dissent": entry["dissent"],
        "dissent_tied": entry["dissent_tied"],
        "quality_policy": QUALITY_V3_POLICY,
        "quality_receipt_id": entry["quality_receipt_id"],
        "quality_receipt_ids": list(quality_receipt_ids),
        "ranking_event_id": str(ranking["event_id"]),
        "ranking_event_seq": ranking["event_seq"],
        "k": k,
        "replacement_for": replacement_for,
    }
