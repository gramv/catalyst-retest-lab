"""Selection rule JEV_TOP_K_SELECTION_V3 (package jev-b2, migration 030): the stage-1 checks
(PICK_CHECK_QUESTIONS_V1_C<n>), the one comparative review (COMPARATIVE_PICK_QUESTIONS_V1_N<m>,
COMPARATIVE_FACTS_V1), the selection rule (T = 0.60, REJECT at 0.40, zero allowed), admission
SQL and the replacement flow.

Fixture and disposable-PostgreSQL evidence only: a scripted mock Jev transport (no TypeSafe
call), canned public bars, the fixture universe of test_research_report_v3 and the paper venue
of test_managed_execution. No provider, broker, network or owner-ledger contact.
"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from catalyst_lab import localdb
from catalyst_lab import research_selection_topk as topk
from catalyst_lab import research_selection_topk_v3 as v3
from catalyst_lab import selection_facts_v3 as facts
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import INSUFFICIENT, JEV_MODEL, encoded, strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy, ReviewResult
from catalyst_lab.managed_runtime import (
    PERMANENT_ADMISSION_REFUSALS,
    TOPK_PERMANENT_ADMISSION_REFUSALS,
    decline_selection,
)
from catalyst_lab.repository import Repository
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_selection_b1 import FLOOR_ENV, RULE_ENV
from catalyst_lab.system_check import LiveQuote
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_research_report_v3 import cycle_of, pick, rationale, report_v3, v3_intake
from tests.test_selection_b1 import as_role, classify, no_sleep, replay_script, review_failure
from tests.test_selection_topk import DROP, write_selected

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = Path(localdb.__file__).with_name("migrations")
V2_MIGRATION = MIGRATIONS / "023_selection_topk_v2.sql"
V3_MIGRATION = MIGRATIONS / "030_selection_topk_v3.sql"
V3 = topk.TOPK_POLICY_V3
WINDOW = CyclePolicy(10, 10, 15, 60, 30, review_validity_seconds=600)
# Pinned independently of the code (research_selection_topk_v3.TEMPLATE_HASHES, migration 030).
HASHES = {
    "PICK_CHECK_QUESTIONS_V1_C0":
        "4f719a1868fe0d6e0326b3fb3ca8e10afb790e9cf80f464cbd071bce34dfd05f",
    "PICK_CHECK_QUESTIONS_V1_C1":
        "954a2957250862870cf176a5ef315496205d2a11ec93cce0cd9857f3c9cd67aa",
    "PICK_CHECK_QUESTIONS_V1_C2":
        "13a5e6da923fcc10796d656decbdb09f948810eff8b9f020d5f0b97368280707",
    "PICK_CHECK_QUESTIONS_V1_C3":
        "8bc4e46a6c6d229fd8ae1b753948851c003488aba7fc88e9bb9218e95775f579",
    "PICK_CHECK_QUESTIONS_V1_C4":
        "ae1cd48d70b0fa5eb2495b9a4b794d29bb16648a68b43838651de95a89ce7f1e",
    "PICK_CHECK_QUESTIONS_V1_C5":
        "dae84adcbf06f7cabacac4cb2367fba732b8867b9b1d26768344d052642571c7",
    "PICK_CHECK_QUESTIONS_V1_C6":
        "88c3187e549a4fd3a62126ef107af906d93ca60889996c45d26b5f0770a6773e",
    "COMPARATIVE_PICK_QUESTIONS_V1_N1":
        "9072234e67f9668f4e5525754397862936602e03e89167258a7cf5dc62d63881",
    "COMPARATIVE_PICK_QUESTIONS_V1_N2":
        "1379d90bbe3feeafb729699478666a2d9db50cdabfef08033dd7e328debf8361",
    "COMPARATIVE_PICK_QUESTIONS_V1_N3":
        "8f3385f107244c1de66b04c5d0800cbeb24fd735b6460b26567190262037dce3",
    "COMPARATIVE_PICK_QUESTIONS_V1_N4":
        "0d3221956e70efdb6ca66ae175c7b2ac45fe18eb5c5760b793591f7a921848e5",
    "COMPARATIVE_PICK_QUESTIONS_V1_N5":
        "1e15bb295582ccb22c82e6434546cb1e664fc575dfb4e53e12bba0cb5c913c1a",
    "COMPARATIVE_PICK_QUESTIONS_V1_N6":
        "2ead06ce49b7fc1c2ec49d57a971d3177bf088e6cc1d9e9e9c418bf9ac501582",
    "COMPARATIVE_PICK_QUESTIONS_V1_N7":
        "d8aa1de2e6d16f4720f9eb67d58462537249a2005cc0099e60cfff70a8b134f3",
    "COMPARATIVE_PICK_QUESTIONS_V1_N8":
        "e39cbcdbf30ddc8ade87f5c0b1935398dc489b1f523dc1f66fb77fa018a9072d",
    "COMPARATIVE_PICK_QUESTIONS_V1_N9":
        "33c2e1bf4ab5cd8a87a9839cd8ce44b33e785fb6fe9a2f562d445df3f6668793",
    "COMPARATIVE_PICK_QUESTIONS_V1_N10":
        "a1b070533ed3c56c686f36917c3f40b3eec195c9d767dd4c2581b43375c0d927",
}


def apply_migrations_after_027(root):
    """Apply, as the owner, every real migration after 027 (the public page's 028 and 029, then
    030) to a disposable cluster at schema 27 and assert they appended no audit event (none adds a
    row); then 031 (package plugin-c3), which appends exactly its four audited rows
    (tests/test_risk_v5.py). Returns the audit head."""
    import psycopg

    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 27
        for version, path in sorted(localdb.migration_files().items()):
            if 27 < version <= 30:
                conn.execute(path.read_text())
    with psycopg.connect(owner_url) as conn:
        after = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 30
    assert after == head
    from tests.test_risk_v5 import apply_migration_031

    return apply_migration_031(root)


def apply_migration_030(root):
    """Apply 030 as the owner to a disposable cluster at schema 29 and assert it appended no
    audit event (functions only); returns the audit head."""
    import psycopg

    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 29
        conn.execute(V3_MIGRATION.read_text())
    with psycopg.connect(owner_url) as conn:
        after = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 30
    assert after == head
    return after


# --- The question sets -----------------------------------------------------------------------


def test_every_template_is_pinned_in_code_and_in_migration_030():
    assert v3.TEMPLATE_HASHES == HASHES
    sql = V3_MIGRATION.read_text()
    for version, value in HASHES.items():
        # Each hash appears in the CASE that pins it and in the activation's question sets.
        assert sql.count(value) == 2, version
    for n, qs in v3.CHECK_SETS.items():
        assert set(qs.questions) == {"concrete_coin_specific_catalyst", "thesis_contradicted",
                                     *(f"claim_{i}" for i in range(1, n + 1))}
        assert qs.stage == "SKEPTIC" and all(q["type"] == "noul"
                                              for q in qs.questions.values())
    for m, qs in v3.COMPARE_SETS.items():
        assert qs.stage == "TRIAGE" and len(qs.questions) == m + 2
        assert {q["type"] for name, q in qs.questions.items()
                if name.startswith("candidate_")} == {"noul"}


def test_stage_one_asks_one_narrow_noul_per_check_with_no_arithmetic_or_verdict():
    questions = v3.checks_set(3).questions
    assert set(questions) == {"thesis_contradicted", "concrete_coin_specific_catalyst",
                              "claim_1", "claim_2", "claim_3"}
    for name, question in questions.items():
        assert set(question["criteria"]) == {"true", "false"}, name
        text = question["instructions"]
        # Geometry, prices, reward-to-risk and the holistic verdict moved to code or out.
        for word in ("reward-to-risk", "stated_reward_risk", "max_entry_price", "verdict"):
            assert word not in text, (name, word)
    for index in range(3):
        assert f"`rationale.claims[{index}]`" in questions[f"claim_{index + 1}"]["instructions"]
    with pytest.raises(ValueError, match="CLAIM_COUNT_INVALID"):
        v3.checks_set(7)
    with pytest.raises(ValueError, match="CANDIDATE_COUNT_INVALID"):
        v3.comparison_set(11)


def test_the_two_best_choices_list_the_same_options_in_reversed_order():
    questions = v3.comparison_set(3).questions

    def listed(name):
        criteria = questions[name]["criteria"]
        # The order the provider reads: encoded() sorts the keys (A01 < A02 < ... < Insufficient).
        order = list(strict_json(encoded(criteria)))
        assert order[-1] == INSUFFICIENT
        return [criteria[key] for key in order[:-1]]

    forward, backward = listed(v3.FORWARD), listed(v3.REVERSED)
    assert forward == [*(f"`candidates[{i}]` is the best setup to open today." for i in range(3)),
                       "None of the candidates is worth opening today."]
    assert backward == list(reversed(forward))
    assert questions[v3.FORWARD]["instructions"] == questions[v3.REVERSED]["instructions"]
    candidate = questions["candidate_2"]["instructions"]
    assert "`candidates[1]`" in candidate and "`market`" in candidate


# --- Stage 1: the vetoes ---------------------------------------------------------------------


def checks_state(kinds):
    return {"kind": "BOTH", "rationale": {"claims": [
        {"claim_id": f"C{i}", "kind": kind, "text": "x", "supported_by": {}}
        for i, kind in enumerate(kinds)]}}


def checks_answers(state, *, thesis=0.05, catalyst=0.9, claims=None):
    n = v3.claim_count(state)
    claims = list(claims or [0.95] * n)
    answers = {"thesis_contradicted": {"type": "noul", "noul": thesis},
               "concrete_coin_specific_catalyst": {"type": "noul", "noul": catalyst}}
    for index in range(n):
        answers[f"claim_{index + 1}"] = {"type": "noul", "noul": claims[index]}
    return answers


def checks_of(state, **answers):
    result = ReviewResult("request-1", "RECORDED", None, ("receipt-1",),
                          checks_answers(state, **answers))
    decimals = json.loads(json.dumps(result.answers), parse_float=D)
    return v3.assess_checks(result, state, decimals)


@pytest.mark.parametrize(("thesis", "status", "codes"), [
    (0.70, "VETOED", ("THESIS_CONTRADICTED",)),
    (0.69, "RANKABLE", ("THESIS_CONTRADICTED_UNSURE",)),
    (0.31, "RANKABLE", ("THESIS_CONTRADICTED_UNSURE",)),
    (0.30, "RANKABLE", ()),
    (1.0, "VETOED", ("THESIS_CONTRADICTED",)),
])
def test_thesis_contradicted_vetoes_at_seventy_percent(thesis, status, codes):
    outcome = checks_of(checks_state(["CATALYST"]), thesis=thesis)
    assert outcome.status == status
    assert outcome.veto_reasons + tuple(c for c in outcome.uncertain
                                        if c.startswith("THESIS")) == codes


@pytest.mark.parametrize(("kinds", "claims", "status", "factual", "unsupported"), [
    # Half or more of the asked factual claims at or below 0.30 vetoes.
    (["CATALYST", "TECHNICAL"], [0.30, 0.95], "VETOED", 2, 1),
    (["CATALYST", "TECHNICAL"], [0.31, 0.95], "RANKABLE", 2, 0),
    (["CATALYST", "TECHNICAL", "RISK"], [0.1, 0.9, 0.9], "RANKABLE", 3, 1),
    (["CATALYST", "TECHNICAL", "RISK"], [0.1, 0.2, 0.9], "VETOED", 3, 2),
    # ECONOMIC_LINK claims are asked and recorded but never counted.
    (["ECONOMIC_LINK", "CATALYST"], [0.0, 0.9], "RANKABLE", 1, 0),
    (["ECONOMIC_LINK", "ECONOMIC_LINK"], [0.0, 0.0], "RANKABLE", 0, 0),
    # Only the first six claims are asked; a seventh is never counted.
    (["CATALYST"] * 6 + ["TECHNICAL"], [0.9, 0.9, 0.9, 0.1, 0.1, 0.1], "VETOED", 6, 3),
    (["ECONOMIC_LINK"] + ["CATALYST"] * 5 + ["TECHNICAL"], [0.0, 0.9, 0.9, 0.1, 0.1, 0.9],
     "RANKABLE", 5, 2),
])
def test_unsupported_claims_are_counted_by_code(kinds, claims, status, factual, unsupported):
    state = checks_state(kinds)
    outcome = checks_of(state, claims=claims)
    assert v3.pick_checks_set(state).version == f"PICK_CHECK_QUESTIONS_V1_C{min(len(kinds), 6)}"
    assert (outcome.status, outcome.factual_claims, outcome.unsupported_claims) == (
        status, factual, unsupported)
    if status == "VETOED":
        assert outcome.veto_reasons == ("CLAIMS_UNSUPPORTED",)


def test_a_missing_catalyst_and_unsure_claims_are_recorded_never_vetoing():
    outcome = checks_of(checks_state(["CATALYST", "TECHNICAL"]), catalyst=0.3,
                        claims=[0.5, 0.95])
    assert outcome.status == "RANKABLE" and outcome.reason == "TOPK_CHECKS_UNCERTAIN"
    assert outcome.uncertain == ("NO_COIN_SPECIFIC_CATALYST", "CLAIM_1_UNSURE")
    assert outcome.probabilities["concrete_coin_specific_catalyst"] == "0.3"


@pytest.mark.parametrize(("result", "code"), [
    (ReviewResult("r", "NEEDS_REVIEW", "HTTP_500", ("x",), {}), "HTTP_500"),
    (ReviewResult("r", "RECORDED", None, (), {}), "MISSING_VALID_REVIEW"),
    (ReviewResult("r", "RECORDED", None, ("x",), {"thesis_contradicted": {}}), "INVALID_REVIEW"),
])
def test_a_failed_or_invalid_stage_one_review_is_not_ranked_with_its_code(result, code):
    outcome = v3.assess_checks(result, checks_state(["CATALYST"]))
    assert (outcome.status, outcome.reason) == ("NOT_RANKED", code)


# --- Stage 2 and the selection rule ----------------------------------------------------------


def test_the_shuffle_is_a_deterministic_function_of_the_cycle_id():
    items = [{"item_key": f"CRYPTO:C{i:02}/USD"} for i in range(10)]
    cycle_id = "8a3d2c70-3d0e-4d0a-9b0c-1f2e3d4c5b6a"
    once = v3.shuffled(cycle_id, items)
    assert v3.shuffled(cycle_id, list(reversed(items))) == once  # Input order never matters.
    assert once != items  # This cycle's order is not the agent's.
    other = v3.shuffled("2b1f0e9d-8c7b-4a6f-9e5d-4c3b2a1f0e9d", items)
    assert other != once
    import hashlib
    assert [i["item_key"] for i in once] == sorted(
        (i["item_key"] for i in items),
        key=lambda k: hashlib.sha256(f"{cycle_id}:{k}".encode()).hexdigest())


def comparison_answers(probabilities, forward="A01", backward="A01", m=None):
    m = m or len(probabilities)
    answers = {f"candidate_{i + 1}": {"type": "noul", "noul": p}
               for i, p in enumerate(probabilities)}
    for name, label in ((v3.FORWARD, forward), (v3.REVERSED, backward)):
        options = list(v3.COMPARE_SETS[m].questions[name]["criteria"])
        dist = {key: 0.0 for key in options}
        dist[label] = 0.9
        dist[next(k for k in options if k != label)] = 0.1
        answers[name] = {"type": "choice", "choice": label, "confidence": 0.9,
                         "probabilities": dist}
    return answers


@pytest.mark.parametrize(("forward", "backward", "agreement", "chosen"), [
    ("A02", "A03", "AGREE", (1, 1)),  # Candidate 1 is A02 forward and A03 reversed (m = 3).
    ("A01", "A04", "AGREE", (0, 0)),
    ("A04", "A01", "AGREE", (None, None)),  # Both NONE.
    ("A01", "A01", "DISAGREE_UNCERTAIN", (0, None)),  # Same position, different candidates.
    ("A02", "A02", "DISAGREE_UNCERTAIN", (1, 2)),
])
def test_reversed_choices_map_back_to_candidates_and_disagreement_is_uncertain(
        forward, backward, agreement, chosen):
    result = ReviewResult("r", "RECORDED", None, ("x",),
                          comparison_answers([0.7, 0.5, 0.2], forward, backward))
    outcome = v3.assess_comparison(result, 3)
    assert outcome.status == "ANSWERED" and outcome.agreement == agreement
    assert (outcome.forward, outcome.reversed) == chosen
    assert outcome.probabilities == (D("0.7"), D("0.5"), D("0.2"))
    body = outcome.body_fields()
    assert body["best_choice"]["agreement"] == agreement
    assert body["choice_probabilities"]["forward"][chosen[0] if chosen[0] is not None else 0]


def assessed(key, p, *, agent, status="RANKABLE"):
    checks = v3.ChecksOutcome(status, "TOPK_CHECKS_PASSED" if status == "RANKABLE" else "X",
                              ("THESIS_CONTRADICTED",) if status == "VETOED" else (), (),
                              "PICK_CHECK_QUESTIONS_V1_C1", {})
    return v3.Assessed(item_key=key, revision=1, symbol=key, kind="BOTH", agent_rank=agent,
                       checks=checks, receipt_id="r", position=0 if p is not None else None,
                       probability=None if p is None else D(p),
                       comparison_receipt_id="c" if p is not None else None)


def test_the_selection_rule_threshold_reject_band_and_ordering():
    entries = v3.rank_entries([
        assessed("a", "0.60", agent=1), assessed("b", "0.59", agent=2),
        assessed("c", "0.41", agent=3), assessed("d", "0.40", agent=4),
        assessed("e", "0.95", agent=5), assessed("f", "0.95", agent=6),
        assessed("g", None, agent=7), assessed("h", None, agent=8, status="VETOED"),
    ])
    view = [(e["item_key"], e["status"], e["rank"], e["veto_reasons"], e["reason"])
            for e in entries]
    assert view == [
        ("e", "RANKED", 1, [], None), ("f", "RANKED", 2, [], None),  # Tie: the agent's order.
        ("a", "RANKED", 3, [], None),  # Exactly T.
        ("d", "VETOED", None, ["COMPARATIVE_REJECT"], None),  # Exactly 0.40 is a REJECT.
        ("h", "VETOED", None, ["THESIS_CONTRADICTED"], None),
        ("b", "NOT_RANKED", None, [], "BELOW_SELECTION_THRESHOLD"),
        ("c", "NOT_RANKED", None, [], "BELOW_SELECTION_THRESHOLD"),
        ("g", "NOT_RANKED", None, [], "COMPARISON_REVIEW_MISSING"),
    ]


def test_zero_ranked_is_a_valid_ranking():
    entries = v3.rank_entries([assessed("a", "0.5", agent=1), assessed("b", "0.1", agent=2)])
    body = v3.ranking_body(cycle_id="c", run_slot=None, k=5, entries=entries, complete=True,
                           comparison=None)
    assert body["counts"] == {"RANKED": 0, "VETOED": 1, "NOT_RANKED": 1}
    assert (body["select_at"], body["reject_at"]) == ("0.60", "0.40")


# --- Configuration ---------------------------------------------------------------------------


def test_the_owner_switch_reads_v3_with_k_and_refuses_a_floor():
    rule = topk.selection_rule_from_env({RULE_ENV: V3, topk.TOPK_ENV: '{"k": 7}'})
    assert (rule.policy, rule.k, rule.topk) == (V3, 7, True)
    with pytest.raises(ValueError, match="SELECTION_QUALITY_FLOOR_NOT_APPLICABLE"):
        topk.selection_rule_from_env({RULE_ENV: V3, FLOOR_ENV: "WEAK"})


def test_activation_and_started_blocks_round_trip_and_tampering_is_refused():
    rule = topk.TopKRule(V3, 6)
    activation = topk.activation_body(rule, runtime_id="r")
    assert activation["question_sets"] == v3.question_set_versions()
    assert {k: activation[k] for k in v3.thresholds()} == v3.thresholds()
    assert "quality_policy" not in activation
    started = topk.started_rule(rule, {"event_id": uuid4(), "event_seq": 9})
    assert topk.stored_rule(started) == rule
    for change in ({"select_at": "0.50"}, {"reject_at": "0.30"}, {"max_candidates": 12},
                   {"question_sets": {}}, {"shuffle": "AGENT_ORDER"}):
        with pytest.raises(ValueError, match="SELECTION_RULE_UNAVAILABLE"):
            topk.stored_rule({**started, **change})
    # V1 and V2 blocks are unchanged by V3's existence.
    v2 = topk.started_rule(topk.TopKRule(topk.TOPK_POLICY_V2, 5),
                           {"event_id": uuid4(), "event_seq": 1})
    assert set(v2) == {"selection_policy", "k", "question_sets", "quality_policy",
                       "veto_min_probability", "activation_event_id", "activation_event_seq"}


# --- Code facts (COMPARATIVE_FACTS_V1) -------------------------------------------------------

NOW = datetime(2026, 10, 3, 14, 30, tzinfo=UTC)


def hourly(now, *, start=D(100), trend=D(0), volume=D(10), recent_multiple=D(1), days=31,
           range_pct=D("0.01")):
    """Completed 1-hour bars ending at the hour before ``now``: closes move linearly by
    ``trend`` (a fraction) over the window; the last 24 hours trade ``recent_multiple`` times
    the volume."""
    end = now.replace(minute=0, second=0, microsecond=0)
    hours = days * 24
    bars = []
    for index in range(hours):
        at = end - timedelta(hours=hours - index)
        close = start * (1 + trend * index / (hours - 1))
        spread = close * range_pct / 2
        recent = at >= end - timedelta(hours=24)
        bars.append({"t": at.isoformat(), "o": str(close), "h": str(close + spread),
                     "l": str(close - spread), "c": str(close),
                     "v": str(volume * (recent_multiple if recent else 1))})
    return bars


def parsed(rows):
    from catalyst_lab.pick_outcomes import parse_bars
    return parse_bars(rows)


@pytest.mark.parametrize(("trend", "label"), [
    (D("-0.2"), "STRONG_DOWN"), (D("-0.05"), "DOWN"), (D("0.01"), "FLAT"),
    (D("0.05"), "UP"), (D("0.3"), "STRONG_UP")])
def test_trend_buckets(trend, label):
    assert facts.trend_bucket(parsed(hourly(NOW, trend=trend)), NOW)[0].startswith(label)


@pytest.mark.parametrize(("multiple", "label"), [
    (D("0.4"), "LOW"), (D("0.8"), "BELOW_AVERAGE"), (D("1.5"), "ABOVE_AVERAGE"),
    (D("3"), "HIGH"), (D("5"), "VERY_HIGH")])
def test_volume_buckets(multiple, label):
    assert facts.volume_bucket(parsed(hourly(NOW, recent_multiple=multiple)), NOW)[0].startswith(
        label)


def test_too_few_bars_are_unknown_never_guessed():
    short = parsed(hourly(NOW, days=10))
    assert facts.trend_bucket(short, NOW) == ("UNKNOWN", None)
    assert facts.volume_bucket(short, NOW) == ("UNKNOWN", None)
    assert facts.hourly_range(parsed(hourly(NOW, days=31))[:-10], NOW) is None


def test_stop_width_and_fees_follow_the_trade_plan_rule():
    levels = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
    # HR = 1: the plan stop is min(95, 100 - 2) = 95, exactly 5 ranges below the trigger.
    assert facts.stop_bucket(levels, D(1))[0].startswith("3_TO_5")
    assert facts.stop_bucket(levels, D("0.5"))[0].startswith("OVER_5")
    assert facts.stop_bucket(levels, D(3))[0].startswith("AT_PLAN_MINIMUM")  # 100 - 6 = 94.
    assert facts.stop_bucket(levels, D("2"))[0].startswith("2_TO_3")  # 5 / 2 = 2.5.
    label, fees = facts.fees_bucket(levels, D(1))
    assert label == "UNDER_0.10R"  # 2 x 0.0025 x 100.10 / 5.10 = 0.0981.
    assert fees.quantize(D("0.000001")) == D("0.098137")
    assert facts.fees_bucket(levels, None)[0].endswith("(on the research stop; hourly range "
                                                       "unknown)")
    assert facts.stop_bucket(levels, None) == ("UNKNOWN", None)


def test_results_words():
    assert facts.results_words([]).startswith("NONE")
    words = facts.results_words([(NOW, D("-1")), (NOW, D("0.5")), (NOW, None)])
    assert words == ("3 closed in 30 days, 1 of 2 with known R above 0R; the last ended at or "
                     "below 0R")


# --- A V3 cycle on the fixture venue, through admission SQL ----------------------------------


def seven_claims():
    """Seven claims: an inference first, then three catalyst and three technical claims (only
    the first six are asked; five of them are factual)."""
    block = rationale()
    claims = [{"claim_id": "E0", "kind": "ECONOMIC_LINK",
               "text": "A listing adds spot demand.",
               "supported_by": {"source_ids": ["src-1"], "bar_ids": []}}]
    for i in range(3):
        claims.append({"claim_id": f"K{i}", "kind": "CATALYST",
                       "text": f"The exchange listing starts today ({i}).",
                       "supported_by": {"source_ids": ["src-1"], "bar_ids": []}})
    for i in range(3):
        claims.append({"claim_id": f"T{i}", "kind": "TECHNICAL",
                       "text": f"The last completed hourly bar closed at 100.40 ({i}).",
                       "supported_by": {"source_ids": [], "bar_ids": ["b-19"]}})
    return {**block, "claims": claims}


class FakeBars:
    """Canned public 1-hour bars per symbol (``trend`` from the script); records each read."""

    def __init__(self, script, clock, fail=()):
        self.script, self.clock, self.fail, self.reads = script, clock, set(fail), []

    def bars(self, symbol, start, end, timeframe):
        self.reads.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("fixture bar failure")
        spec = self.script.get(symbol, {})
        rows = hourly(self.clock(), trend=D(str(spec.get("trend", "0.05"))),
                      recent_multiple=D(str(spec.get("volume", "1.5"))))
        return [r for r in rows if start <= datetime.fromisoformat(r["t"]) < end]


def noul(p):
    return {"type": "noul", "noul": p}


def choice_of(question, label, p=0.8):
    options = list(question["criteria"])
    dist = {key: 0.0 for key in options}
    dist[label] = p
    dist[next(k for k in options if k != label)] = round(1 - p, 2)
    return {"type": "choice", "choice": label, "confidence": p, "probabilities": dist}


def v3_provider(script, calls, served=None):
    """Mock TypeSafe transport. Stage 1 keyed by the state's symbol: ``thesis``, ``catalyst``,
    ``claims`` (default 0.05, 0.9, 0.95 each) and ``plan`` (HTTP statuses first). The
    comparison: each candidate's ``p`` (default 0.8); ``__comparison__`` may set ``plan``,
    ``best`` and ``best_reversed`` (symbols or NONE; default the highest p)."""
    served = {} if served is None else served

    def provider(request):
        body = strict_json(request.content)
        calls.append(body)
        questions, state = body["questions"], body["state"]
        if "candidate_1" in questions:
            spec, key = script.get("__comparison__", {}), "__comparison__"
        else:
            spec, key = script.get(state["symbol"], {}), state["symbol"]
        attempt = served.get(key, 0)
        served[key] = attempt + 1
        plan = spec.get("plan", ())
        if attempt < len(plan):
            return httpx.Response(plan[attempt], json={"error": "fixture"})
        if "candidate_1" in questions:
            symbols = [c["symbol"] for c in state["candidates"]]
            ps = [script.get(s, {}).get("p", 0.8) for s in symbols]
            answers = {f"candidate_{i + 1}": noul(p) for i, p in enumerate(ps)}
            top = symbols[ps.index(max(ps))]
            m = len(symbols)
            for name, wanted, order in ((v3.FORWARD, spec.get("best", top), v3.forward_options(m)),
                                        (v3.REVERSED, spec.get("best_reversed", top),
                                         v3.reversed_options(m))):
                index = None if wanted == "NONE" else symbols.index(wanted)
                answers[name] = choice_of(questions[name], v3.option_key(order.index(index) + 1))
        else:
            n = sum(1 for name in questions if name.startswith("claim_"))
            claims = list(spec.get("claims", [0.95] * n))
            answers = {"thesis_contradicted": noul(spec.get("thesis", 0.05)),
                       "concrete_coin_specific_catalyst": noul(spec.get("catalyst", 0.9)),
                       **{f"claim_{i + 1}": noul(claims[i]) for i in range(n)}}
        return httpx.Response(200, json={"model": JEV_MODEL, "answers": answers,
                                         "usage": {"input_tokens": 10, "output_tokens": 5}})

    return provider


def v3_cycle(mx, script, *, k=5, facts_reader=True, fail_bars=(), clock=None):
    engine, venue, receipts = mx
    calls = []
    clock = clock or (lambda: venue.now)
    reviewer = JevReviewer(
        receipts,
        ReliabilityPolicy("SELECTION_TOPK_V3_FIXTURE", 10, 2, 0.01, 1000, 30),
        transport=httpx.MockTransport(v3_provider(script, calls)),
        key_provider=lambda: replay_script.FIXTURE_KEY,
        clock=clock,
        sleep=no_sleep,
    )
    bars = FakeBars(script, clock, fail_bars)
    reader = facts.ComparisonFacts(engine.repo, bars) if facts_reader else None
    cycle = ResearchCycle(engine.repo, reviewer, WINDOW, clock=clock,
                          selection=topk.TopKRule(V3, k), comparison_facts=reader)
    cycle.record_selection_rule(runtime_id=str(uuid4()))
    return cycle, calls, bars


def submit3(cycle, script, now):
    picks = [pick(i, symbol, kind=spec.get("kind", "BOTH"), now=now,
                  **({"selection_rationale": spec["rationale"]} if "rationale" in spec else {}))
             for i, (symbol, spec) in enumerate(
                 (s, v) for s, v in script.items() if not s.startswith("__"))]
    raw = report_v3(picks, now=now)
    result = cycle.start_report(raw, max_seconds=86400,
                                v3=v3_intake(universe={p["symbol"] for p in picks}, now=now))
    assert result["rejected_count"] == 0, result
    return cycle_of(raw)


def run3(cycle, cycle_id):
    asyncio.run(cycle.tick(cycle_id))
    return cycle.approved_packets(cycle_id)


def events_of(engine, kind, cycle_id):
    with engine.repo.connect() as conn:
        return conn.execute("""SELECT * FROM lab.managed_events WHERE kind=%s
            AND body->>'cycle_id'=%s ORDER BY event_seq""", (kind, cycle_id)).fetchall()


def ranking3(engine, cycle_id):
    [row] = [r for r in events_of(engine, "RESEARCH_RANKING", cycle_id)
             if r["idempotency_key"] == f"research:ranking:{cycle_id}"]
    return row


def sym(entry):
    return entry["item_key"].split(":", 1)[1]


E2E = {
    "P1/USD": {"kind": "BOTH", "p": 0.9},
    "P2/USD": {"kind": "NEWS", "p": 0.85},
    "P3/USD": {"kind": "CHART", "p": 0.6},  # Exactly T: ranked, sixth (not published at K=5).
    "P4/USD": {"kind": "BOTH", "p": 0.59},  # Below T: NOT_RANKED.
    "P5/USD": {"kind": "BOTH", "p": 0.4},  # A REJECT.
    "P6/USD": {"kind": "BOTH", "p": 0.75, "trend": "-0.2"},
    "VT/USD": {"kind": "BOTH", "thesis": 0.7},  # Stage-1 veto: thesis contradicted.
    "VC/USD": {"kind": "BOTH", "claims": [0.3, 0.9]},  # Stage-1 veto: half the claims.
    "UT/USD": {"kind": "BOTH", "thesis": 0.69, "catalyst": 0.2, "p": 0.7},  # Unsure, survives.
    "M7/USD": {"kind": "BOTH", "rationale": seven_claims(),
               "claims": [0.0, 0.9, 0.9, 0.1, 0.1, 0.9], "p": 0.65},  # 2 of 5 unsupported.
    "F1/USD": {"kind": "NEWS", "plan": (500, 500)},  # Its stage-1 review failed.
}
E2E_RANKED = [("P1/USD", "0.9"), ("P2/USD", "0.85"), ("P6/USD", "0.75"), ("UT/USD", "0.7"),
              ("M7/USD", "0.65"), ("P3/USD", "0.6")]
E2E_VETOED = {"P5/USD": ["COMPARATIVE_REJECT"], "VT/USD": ["THESIS_CONTRADICTED"],
              "VC/USD": ["CLAIMS_UNSUPPORTED"]}
E2E_NOT_RANKED = {"P4/USD": "BELOW_SELECTION_THRESHOLD", "F1/USD": "HTTP_500"}


@pytest.fixture
def e2e3(mx):
    engine, venue, _ = mx
    cycle, calls, bars = v3_cycle(mx, E2E)
    cycle_id = submit3(cycle, E2E, venue.now)
    chosen = run3(cycle, cycle_id)
    return SimpleNamespace(engine=engine, venue=venue, cycle=cycle, cycle_id=cycle_id,
                           chosen=chosen, calls=calls, bars=bars)


def test_a_v3_cycle_checks_each_pick_compares_survivors_once_and_selects_up_to_k(e2e3):
    engine, cycle_id = e2e3.engine, e2e3.cycle_id
    checks = [c for c in e2e3.calls if "thesis_contradicted" in c["questions"]]
    comparisons = [c for c in e2e3.calls if "candidate_1" in c["questions"]]
    # One stage-1 request per pick (F1's HTTP 500 is not retried) and one comparison.
    assert len(checks) == len(E2E) and len(comparisons) == 1
    for call in checks:
        n = min(len(call["state"]["rationale"]["claims"]), 6)
        assert call["questions"] == v3.CHECK_SETS[n].questions
        # Stage 1 reads the pick's own dossier, nothing else (no verdict, no quality score).
        assert "candidates" not in call["state"]
    m7 = next(c for c in checks if c["state"]["symbol"] == "M7/USD")
    assert len(m7["state"]["rationale"]["claims"]) == 7 and "claim_6" in m7["questions"]
    [comparison] = comparisons
    survivors = {"P1/USD", "P2/USD", "P3/USD", "P4/USD", "P5/USD", "P6/USD", "UT/USD", "M7/USD"}
    symbols = [c["symbol"] for c in comparison["state"]["candidates"]]
    assert set(symbols) == survivors
    assert comparison["questions"] == v3.COMPARE_SETS[8].questions
    assert set(comparison["state"]) == {"market", "candidates"}
    assert len(encoded(comparison["state"])) <= 8 * 800 + 400
    ranking = ranking3(engine, cycle_id)["body"]
    assert (ranking["policy"], ranking["k"], ranking["counts"]) == (
        V3, 5, {"RANKED": 6, "VETOED": 3, "NOT_RANKED": 2})
    assert [(sym(e), e["probability"]) for e in ranking["entries"]
            if e["status"] == "RANKED"] == E2E_RANKED
    assert [e["rank"] for e in ranking["entries"] if e["status"] == "RANKED"] == list(range(1, 7))
    assert {sym(e): e["veto_reasons"] for e in ranking["entries"]
            if e["status"] == "VETOED"} == E2E_VETOED
    assert {sym(e): e["reason"] for e in ranking["entries"]
            if e["status"] == "NOT_RANKED"} == E2E_NOT_RANKED
    ut = next(e for e in ranking["entries"] if sym(e) == "UT/USD")
    assert ut["uncertain"] == ["THESIS_CONTRADICTED_UNSURE", "NO_COIN_SPECIFIC_CATALYST"]
    m7e = next(e for e in ranking["entries"] if sym(e) == "M7/USD")
    assert m7e["uncertain"] == ["CLAIM_4_UNSUPPORTED", "CLAIM_5_UNSUPPORTED"]
    # The top K are published in rank order; P3 (rank 6) waits for a replacement.
    assert [p["symbol"] for p in e2e3.chosen] == [s for s, _ in E2E_RANKED[:5]]
    assert not events_of(engine, "RESEARCH_SELECTION_NONE", cycle_id)
    # The holistic verdict is not asked and there is no QUALITY review.
    assert not events_of(engine, "RESEARCH_QUALITY", cycle_id)
    assert all("verdict" not in c["questions"] for c in e2e3.calls)


def test_the_comparison_order_is_the_recorded_shuffle_and_never_reasked(e2e3):
    engine, cycle_id = e2e3.engine, e2e3.cycle_id
    [state] = events_of(engine, "RESEARCH_COMPARISON_STATE", cycle_id)
    body = state["body"]
    order = [item["item_key"] for item in body["order"]]
    assert order == [i["item_key"] for i in v3.shuffled(cycle_id, [
        {"item_key": "CRYPTO:" + s} for s in ("P1/USD", "P2/USD", "P3/USD", "P4/USD",
                                              "P5/USD", "P6/USD", "UT/USD", "M7/USD")])]
    assert order != ["CRYPTO:" + s for s in ("P1/USD", "P2/USD", "P3/USD", "P4/USD",
                                             "P5/USD", "P6/USD", "UT/USD", "M7/USD")]
    assert [c["symbol"] for c in body["state"]["candidates"]] == [k.split(":", 1)[1]
                                                                  for k in order]
    assert (body["shuffle"], body["seed"]) == (v3.SHUFFLE_METHOD, cycle_id)
    [result] = events_of(engine, "RESEARCH_COMPARISON", cycle_id)
    assert result["body"]["best_choice"] == {"forward": order.index("CRYPTO:P1/USD"),
                                             "reversed": order.index("CRYPTO:P1/USD"),
                                             "agreement": "AGREE"}
    calls = len(e2e3.calls)
    for _ in range(2):
        run3(e2e3.cycle, cycle_id)
    assert len(e2e3.calls) == calls
    assert len(events_of(engine, "RESEARCH_COMPARISON_STATE", cycle_id)) == 1


def test_the_comparison_state_carries_code_buckets_and_the_market(e2e3):
    [state] = events_of(e2e3.engine, "RESEARCH_COMPARISON_STATE", e2e3.cycle_id)
    body = state["body"]
    candidates = {c["symbol"]: c for c in body["state"]["candidates"]}
    p6, ut, p2 = candidates["P6/USD"], candidates["UT/USD"], candidates["P2/USD"]
    assert set(p6) == {"symbol", "setup_type", "catalyst", "checks", "trend_20d",
                       "volume_vs_30d", "stop_width", "fees_in_r", "recent_results"}
    assert p6["trend_20d"].startswith("STRONG_DOWN") and p2["trend_20d"].startswith("UP")
    assert p2["volume_vs_30d"].startswith("ABOVE_AVERAGE")
    assert p2["setup_type"].startswith("news catalyst; RETEST_ENTRY")
    assert p2["catalyst"] == "The exchange listing starts today."
    assert ut["checks"] == {"coin_specific_catalyst": "NO",
                            "claims": "2 of 2 factual claims supported"}
    assert p2["recent_results"].startswith("NONE")
    assert set(body["state"]["market"]) == {"btc_regime_yesterday", "btc_last_4h",
                                            "entry_pacing"}
    assert body["state"]["market"]["btc_regime_yesterday"] == "UNKNOWN"  # None recorded.
    assert body["state"]["market"]["entry_pacing"] == "UNKNOWN"  # Fixture: no pacing.
    assert body["evidence"]["candidates"]["CRYPTO:P2/USD"]["bars"] > 700
    # Every candidate stays within the planned ~800 bytes.
    assert max(len(encoded(c)) for c in candidates.values()) <= 800


def test_v3_packets_cross_admission_sql_and_the_top_pick_trades(e2e3, mx):
    engine, venue = e2e3.engine, e2e3.venue
    for packet in e2e3.chosen:
        assert packet["selection_policy"] == V3 and packet["k"] == 5
        assert review_failure(engine, packet) is None, packet["symbol"]

    def live_quote(symbol):
        return LiveQuote(symbol, D("100.49"), D("100.51"), venue.now, "ALPACA_STREAM",
                         venue.now)

    setups = []
    for packet in e2e3.chosen:
        classify(engine, packet["symbol"])
        setups.append(engine.admit(packet, live_quote=live_quote))
    assert len(set(setups)) == 5
    top = e2e3.chosen[0]
    trigger = D(top["levels"]["entry_trigger"])
    risk = engine.observe_trigger(setups[0], observation(
        mx, trade_price=str(trigger), bid=str(trigger - D(".01")),
        ask=str(trigger + D(".01"))))
    assert risk["outcome"] == "APPROVED"
    with engine.repo.connect() as conn:
        record = conn.execute("SELECT record_json FROM lab.managed_setups WHERE setup_id=%s",
                              (setups[0],)).fetchone()["record_json"]
    assert (record["selection_policy"], record["rank"], record["probability"]) == (V3, 1, "0.9")
    assert verify_events(engine.repo.export_events())["valid"]


def as_published3(run_state, symbol, **changes):
    """What publish_ranked would build for ``symbol``'s entry, forced RANKED (and altered)."""
    cycle, cycle_id, engine = run_state.cycle, run_state.cycle_id, run_state.engine
    ranking = ranking3(engine, cycle_id)
    entry = next(e for e in ranking["body"]["entries"] if sym(e) == symbol)
    comparison = ranking["body"]["comparison"]
    entry = {**entry, "status": "RANKED", "rank": entry["rank"] or 1,
             "comparison_receipt_id": entry["comparison_receipt_id"] or comparison["receipt_id"],
             "comparison_position": entry["comparison_position"]
             if entry["comparison_position"] is not None else 0,
             "probability": entry["probability"] or "0.9"}
    item_key = "CRYPTO:" + symbol
    packet = next(r["body"] for r in events_of(engine, "RESEARCH_PACKET", cycle_id)
                  if r["body"]["item_key"] == item_key)
    decision = next(r["body"] for r in events_of(engine, "RESEARCH_DECISION", cycle_id)
                    if r["body"]["item_key"] == item_key)
    result = ReviewResult(decision["request_id"], "RECORDED", None,
                          tuple(decision["receipt_ids"]) or ("missing",), {})
    body = {**cycle._selected_body(packet, result),
            **v3.selected_fields(entry, ranking, k=5, agent_rank=packet["rank"],
                                 comparison=comparison, replacement_for=None)}
    for name, value in changes.items():
        if value is DROP:
            body.pop(name, None)
        else:
            body[name] = value
    return write_selected(engine, body)


# The SQL code each entry's status leads to when its packet is forged as RANKED.
PARITY = {**{s: None for s, _ in E2E_RANKED[:5]},
          "P5/USD": "TOPK_BELOW_THRESHOLD", "P4/USD": "TOPK_BELOW_THRESHOLD",
          "VT/USD": "TOPK_VETOED", "VC/USD": "TOPK_VETOED"}


def test_admission_sql_and_python_agree_on_every_pick(e2e3):
    """Python's ranking and migration 030 decide each pick alike from the same stored bytes."""
    ranking = ranking3(e2e3.engine, e2e3.cycle_id)["body"]
    statuses = {sym(e): e["status"] for e in ranking["entries"]}
    for symbol, code in PARITY.items():
        packet = as_published3(e2e3, symbol)
        assert review_failure(e2e3.engine, packet) == code, symbol
        assert (code is None) == (statuses[symbol] == "RANKED"), symbol
    # Rank 6 is RANKED but not published: as itself it binds too (a replacement crosses).
    assert review_failure(e2e3.engine, as_published3(e2e3, "P3/USD")) is None
    # The failed review never binds.
    assert review_failure(e2e3.engine, as_published3(e2e3, "F1/USD")) in (
        PERMANENT_ADMISSION_REFUSALS)


FORGERIES = {
    "probability raised": ("P1/USD", {"probability": "0.95"},
                           "TOPK_COMPARISON_BINDING_FAILURE"),
    "position of another": ("P1/USD", {"comparison_position": "SWAP"},
                            "TOPK_COMPARISON_BINDING_FAILURE"),
    "stage-1 receipt as the comparison": ("P1/USD", {"comparison_receipt_id": "STAGE1"},
                                          "TOPK_COMPARISON_BINDING_FAILURE"),
    "comparison state of another event": ("P1/USD", {"comparison_state_event_seq": 1},
                                          "TOPK_COMPARISON_BINDING_FAILURE"),
    "comparison set claimed for 7": ("P1/USD", {"comparison_question_set_version":
                                                "COMPARATIVE_PICK_QUESTIONS_V1_N7"},
                                     "TOPK_COMPARISON_BINDING_FAILURE"),
    "rank changed": ("P2/USD", {"rank": 1}, "TOPK_RANKING_BINDING_FAILURE"),
    "uncertain dropped": ("UT/USD", {"uncertain": []}, "TOPK_RANKING_BINDING_FAILURE"),
    "threshold claimed lower": ("P1/USD", {"select_at": "0.50"}, "SELECTION_RULE_NOT_ACTIVATED"),
    "k changed": ("P1/USD", {"k": 10}, "SELECTION_RULE_NOT_ACTIVATED"),
    "v2 question set claimed": ("P1/USD", {"question_set_version": "BOTH_PICK_QUESTIONS_V2"},
                                "SELECTION_QUESTION_POLICY_MISMATCH"),
    # V2's branch checks the receipt as a V2 pick review: a V3 checks receipt never binds.
    "claimed as V2": ("P1/USD", {"selection_policy": topk.TOPK_POLICY_V2},
                      "RECEIPT_BINDING_FAILURE"),
    "probability dropped": ("P1/USD", {"probability": DROP}, "TOPK_COMPARISON_BINDING_FAILURE"),
}


@pytest.mark.parametrize("case", sorted(FORGERIES))
def test_admission_sql_refuses_a_forged_v3_packet(e2e3, case):
    symbol, changes, code = FORGERIES[case]
    ranking = ranking3(e2e3.engine, e2e3.cycle_id)["body"]
    if changes.get("comparison_position") == "SWAP":
        mine = next(e for e in ranking["entries"] if sym(e) == symbol)
        changes = {"comparison_position": (mine["comparison_position"] + 1) % 8}
    if changes.get("comparison_receipt_id") == "STAGE1":
        mine = next(e for e in ranking["entries"] if sym(e) == symbol)
        changes = {"comparison_receipt_id": mine["receipt_id"]}
    packet = as_published3(e2e3, symbol, **changes)
    assert review_failure(e2e3.engine, packet) == code
    assert code in PERMANENT_ADMISSION_REFUSALS
    with pytest.raises(ValueError, match=f"^{code}$"):
        e2e3.engine.admit(packet)


def test_every_refusal_of_the_v3_branch_is_classified_permanent():
    sql = V3_MIGRATION.read_text()
    codes = set(re.findall(r"RETURN '([A-Z_]+)'", sql))
    assert codes >= set(v3.SQL_REFUSALS)
    assert codes <= PERMANENT_ADMISSION_REFUSALS
    assert codes <= TOPK_PERMANENT_ADMISSION_REFUSALS


def test_the_calibration_record_carries_every_v3_probability(e2e3):
    """JEV_CALIBRATION_V1 (record-only) joins V3's stage-1 Nouls (favourable answer: no
    contradiction, a coin-specific catalyst, each claim supported), the comparative Noul and
    both best-choice probabilities to the pick's outcome."""
    from catalyst_lab import jev_calibration as cal
    from catalyst_lab.pick_outcomes import cycle_picks

    repo = e2e3.engine.repo
    ranking, decisions, _ = cal._cycle_reviews(repo, e2e3.cycle_id)
    picks = {p.symbol: p for p in cycle_picks(repo, e2e3.cycle_id, now=e2e3.venue.now)}
    entries = {e["item_key"]: e for e in ranking["entries"]}
    shadow = {"event_seq": 1, "body": {"outcome": {
        "outcome": "TARGET", "data_complete": True, "triggered": True, "net_r": "0.5",
        "gross_r": "0.6"}}}
    ut = picks["UT/USD"]
    body = cal.selection_record(
        ut, entries[ut.item_key], decisions[(ut.item_key, ut.revision)], None, shadow,
        policy=V3, k=5, cycle_size=len(picks), mechanical=None, traded=None)
    forecasts = {f["question"]: f for f in body["forecasts"]}
    assert forecasts["thesis_contradicted"]["p"] == "0.31"  # 1 - 0.69: no contradiction.
    assert forecasts["thesis_contradicted"]["favourable"] == "NO"
    assert forecasts["concrete_coin_specific_catalyst"]["p"] == "0.2"
    assert forecasts["claim_1"]["p"] == forecasts["claim_2"]["p"] == "0.95"
    assert forecasts["comparative:worth_opening"]["p"] == "0.7"
    assert {"comparative:best_forward", "comparative:best_reversed"} <= set(forecasts)
    assert {f["event"] for f in body["forecasts"]} == {cal.SELECTION_EVENT}
    assert (body["ranking"]["probability"], body["outcome"]["event"]) == ("0.7", True)
    assert body["ranking"]["comparison_position"] == entries[ut.item_key]["comparison_position"]
    # A stage-1 veto has its Nouls and no comparative forecast.
    vt = picks["VT/USD"]
    vetoed = cal.selection_record(
        vt, entries[vt.item_key], decisions[(vt.item_key, vt.revision)], None, shadow,
        policy=V3, k=5, cycle_size=len(picks), mechanical=None, traded=None)
    assert {f["question"] for f in vetoed["forecasts"]} == {
        "thesis_contradicted", "concrete_coin_specific_catalyst", "claim_1", "claim_2"}


# --- Zero, failures and the replacement flow -------------------------------------------------


def test_a_cycle_may_select_none_and_records_it(mx):
    engine, venue, _ = mx
    script = {"Z1/USD": {"p": 0.55}, "Z2/USD": {"p": 0.2}, "Z3/USD": {"thesis": 0.9}}
    cycle, calls, _ = v3_cycle(mx, script)
    cycle_id = submit3(cycle, script, venue.now)
    assert run3(cycle, cycle_id) == []
    ranking = ranking3(engine, cycle_id)
    assert ranking["body"]["counts"] == {"RANKED": 0, "VETOED": 2, "NOT_RANKED": 1}
    [none] = events_of(engine, "RESEARCH_SELECTION_NONE", cycle_id)
    assert none["body"]["ranking_event_seq"] == ranking["event_seq"]
    assert (none["body"]["selection_policy"], none["body"]["comparison_status"]) == (
        V3, "ANSWERED")
    assert not events_of(engine, "RESEARCH_SELECTED", cycle_id)
    run3(cycle, cycle_id)
    assert len(events_of(engine, "RESEARCH_SELECTION_NONE", cycle_id)) == 1


def test_no_survivor_means_no_comparison_call(mx):
    engine, venue, _ = mx
    script = {"Z1/USD": {"thesis": 0.95}, "Z2/USD": {"claims": [0.0, 0.0]}}
    cycle, calls, bars = v3_cycle(mx, script)
    cycle_id = submit3(cycle, script, venue.now)
    assert run3(cycle, cycle_id) == []
    assert not any("candidate_1" in c["questions"] for c in calls) and not bars.reads
    assert ranking3(engine, cycle_id)["body"]["comparison"] is None
    [none] = events_of(engine, "RESEARCH_SELECTION_NONE", cycle_id)
    assert none["body"]["comparison_status"] is None


def test_a_failed_comparison_ranks_nothing_and_missing_facts_are_unknown(mx):
    engine, venue, _ = mx
    script = {"A1/USD": {}, "A2/USD": {}, "__comparison__": {"plan": (500, 500)}}
    cycle, calls, _ = v3_cycle(mx, script, fail_bars={"A1/USD"})
    cycle_id = submit3(cycle, script, venue.now)
    assert run3(cycle, cycle_id) == []
    entries = ranking3(engine, cycle_id)["body"]["entries"]
    assert {e["reason"] for e in entries} == {"COMPARISON_HTTP_500"}
    [state] = events_of(engine, "RESEARCH_COMPARISON_STATE", cycle_id)
    a1 = next(c for c in state["body"]["state"]["candidates"] if c["symbol"] == "A1/USD")
    assert (a1["trend_20d"], a1["volume_vs_30d"]) == ("UNKNOWN", "UNKNOWN")
    assert a1["fees_in_r"].endswith("hourly range unknown)")
    assert state["body"]["evidence"]["candidates"]["CRYPTO:A1/USD"]["bars_error"] == (
        "RuntimeError")


def test_the_comparison_waits_for_a_closed_breaker_and_records_nothing_meanwhile(mx):
    """RESEARCH_REVIEW_BREAKER_GATE_V1 holds stage 2 too: no facts read, no state recorded,
    no ranking; the next tick with a CLOSED breaker compares and ranks."""
    engine, venue, _ = mx
    script = {"A1/USD": {"p": 0.9}, "A2/USD": {"p": 0.7}}
    cycle, calls, bars = v3_cycle(mx, script)
    cycle_id = submit3(cycle, script, venue.now)
    asked = []

    def hold():  # CLOSED for the tick's check and both stage-1 reviews, then OPEN.
        asked.append(1)
        return None if len(asked) <= 1 + len(script) or state["closed"] else {
            "state": "OPEN", "epoch": 1, "blocked_until": None}

    state = {"closed": False}
    cycle._breaker_hold = hold
    assert run3(cycle, cycle_id) == []
    assert not any("candidate_1" in c["questions"] for c in calls) and not bars.reads
    assert not events_of(engine, "RESEARCH_COMPARISON_STATE", cycle_id)
    assert not events_of(engine, "RESEARCH_RANKING", cycle_id)
    state["closed"] = True
    assert [p["symbol"] for p in run3(cycle, cycle_id)] == ["A1/USD", "A2/USD"]
    assert sum("candidate_1" in c["questions"] for c in calls) == 1


def test_more_than_ten_survivors_compare_the_agents_first_ten(mx):
    engine, venue, _ = mx
    script = {f"S{i:02}/USD": {"p": 0.9 - i / 100} for i in range(12)}
    cycle, calls, _ = v3_cycle(mx, script, k=10)
    cycle_id = submit3(cycle, script, venue.now)
    chosen = run3(cycle, cycle_id)
    [comparison] = [c for c in calls if "candidate_1" in c["questions"]]
    assert {c["symbol"] for c in comparison["state"]["candidates"]} == {
        f"S{i:02}/USD" for i in range(10)}
    entries = {sym(e): e for e in ranking3(engine, cycle_id)["body"]["entries"]}
    assert {entries[f"S{i:02}/USD"]["reason"] for i in (10, 11)} == {"COMPARISON_CAP_EXCEEDED"}
    assert [p["symbol"] for p in chosen] == [f"S{i:02}/USD" for i in range(10)]


def test_a_declined_v3_pick_is_replaced_by_the_next_ranked_then_exhausted(e2e3):
    engine, cycle, cycle_id = e2e3.engine, e2e3.cycle, e2e3.cycle_id
    selected = {p["symbol"]: p for p in e2e3.chosen}

    def decline(symbol):
        packet = selected[symbol]
        body = {"cycle_id": cycle_id, "item_key": packet["item_key"],
                "revision": packet["revision"], "receipt_id": packet["receipt_id"],
                "selection_event_seq": packet["selection_event_seq"],
                "reason": "PRICE_MISMATCH"}
        key = f"research:admission-declined:{packet['selection_event_seq']}"
        return decline_selection(engine.store, cycle, body, key)

    _, replacement = decline("P2/USD")
    body = replacement["body"]
    assert (body["outcome"], body["replacement_symbol"], body["replacement_rank"]) == (
        "PUBLISHED", "P3/USD", 6)
    published = {p["symbol"]: p for p in cycle.approved_packets(cycle_id)}
    p3 = published["P3/USD"]
    assert (p3["replacement_for"], p3["probability"], p3["selection_policy"]) == (
        "CRYPTO:P2/USD", "0.6", V3)
    assert review_failure(engine, p3) is None  # The replacement crosses admission SQL.
    # The ranking has no further RANKED entry: P4 (below T) and P5 (REJECT) are never used.
    selected["P3/USD"] = p3
    _, exhausted = decline("P3/USD")
    assert (exhausted["body"]["outcome"], exhausted["body"]["code"]) == (
        "EXHAUSTED", "TOPK_RANKING_EXHAUSTED")


def test_the_runtime_factory_wires_the_facts_reader_and_v3(monkeypatch):
    from catalyst_lab import managed_runtime
    source = Path(managed_runtime.__file__).read_text()
    assert "comparison_facts=ComparisonFacts(repository, PublicCryptoBarReader()," in source
    assert "pacing=getattr(execution, \"entry_pacing\", None)" in source


def test_the_deploy_example_stays_on_v2_and_preflight_accepts_v3(tmp_path):
    from catalyst_lab.managed_ops import preflight
    from tests.test_research_context import ROOT as REPO
    from tests.test_research_context import private_example

    config = private_example(tmp_path)
    assert config["environment"][RULE_ENV] == topk.TOPK_POLICY_V2  # V3 is a later config step.
    switched = private_example(tmp_path, "v3.json", **{RULE_ENV: V3})
    assert preflight(switched, REPO)["invalid_configuration_names"] == []
    broken = private_example(tmp_path, "floor.json", **{RULE_ENV: V3, FLOOR_ENV: "WEAK"})
    assert preflight(broken, REPO)["invalid_configuration_names"] == [RULE_ENV]


# --- Migration 030 ---------------------------------------------------------------------------


def function_source(path, header):
    text = path.read_text()
    body = text.index("AS $$", text.index(header)) + len("AS $$")
    return text[body:text.index("$$;", body)]


TOPK_V3_DISPATCH = (" -- Top-K V3: its own branch (lab.managed_review_failure_topk_v3, above).\n"
                    " IF packet->>'selection_policy'='JEV_TOP_K_SELECTION_V3' THEN\n"
                    "  RETURN lab.managed_review_failure_topk_v3(packet);\n END IF;\n")


# Migration 031 (package plugin-c3) puts the strategy-signal branch in front of 030's dispatcher.
STRATEGY_DISPATCH = (
    " -- Strategy signals: their own branch (lab.managed_review_failure_strategy, above).\n"
    " IF packet->>'selection_policy'='STRATEGY_SIGNAL_SELECTION_V1' THEN\n"
    "  RETURN lab.managed_review_failure_strategy(packet);\n END IF;\n")


def stored_dispatcher_before_031(stored):
    """The stored dispatcher without 031's strategy branch (030's, byte for byte)."""
    assert stored.count(STRATEGY_DISPATCH) == 1
    return stored.replace(STRATEGY_DISPATCH, "")


def test_migration_030_reuses_021s_bindings_and_puts_v3_in_front_of_023s_dispatcher(er):
    v1 = MIGRATIONS / "021_selection_topk.sql"
    with Repository(as_role(er, "lab_owner")).connect() as conn:
        rows = {r["proname"]: r for r in conn.execute("""SELECT proname,prosrc,prosecdef,
            provolatile,proconfig FROM pg_proc WHERE pronamespace='lab'::regnamespace
            AND (proname LIKE 'managed_review%%' OR proname LIKE 'managed_topk%%')""")}
        grants = {name: conn.execute("""SELECT has_function_privilege('catalyst_risk',%(f)s,
            'EXECUTE') AS risk, has_function_privilege('catalyst_app',%(f)s,'EXECUTE') AS app""",
            {"f": f"lab.{name}(jsonb)"}).fetchone()
            for name in ("managed_review_failure", "managed_review_failure_topk_v3",
                         "managed_review_failure_topk_v3_bindings")}
        version = conn.execute("SELECT max(version) AS v FROM lab.schema_migrations").fetchone()
    assert version["v"] == 31  # 031 (plugin-c3) adds its strategy branch in front.
    bindings = function_source(v1, "CREATE FUNCTION lab.managed_review_failure_topk_bindings(")
    assert rows["managed_review_failure_topk_v3_bindings"]["prosrc"] == bindings.replace(
        "lab.managed_topk_receipt_intact(receipt.receipt_id,latest.body->'state'->>'kind')",
        "lab.managed_topk_v3_checks_receipt_intact(receipt.receipt_id,latest.body->'state')")
    header = "CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb)"
    dispatcher = stored_dispatcher_before_031(rows["managed_review_failure"]["prosrc"])
    assert dispatcher == function_source(V3_MIGRATION, header)
    assert dispatcher.count(TOPK_V3_DISPATCH) == 1
    assert dispatcher.replace(TOPK_V3_DISPATCH, "") == function_source(V2_MIGRATION, header)
    # V1's and V2's branches are stored exactly as 021 and 023 wrote them.
    assert rows["managed_review_failure_topk"]["prosrc"] == function_source(
        v1, "CREATE FUNCTION lab.managed_review_failure_topk(packet jsonb)")
    assert rows["managed_review_failure_topk_v2"]["prosrc"] == function_source(
        V2_MIGRATION, "CREATE FUNCTION lab.managed_review_failure_topk_v2(packet jsonb)")
    for field in ("prosecdef", "provolatile", "proconfig"):
        assert rows["managed_review_failure_topk_v3"][field] == (
            rows["managed_review_failure_topk_v2"][field])
    assert {name for name, grant in grants.items() if grant["risk"]} == {
        "managed_review_failure"}
    assert not any(grant["app"] for grant in grants.values())


def test_migration_030_is_ddl_only_and_self_contained():
    sql = V3_MIGRATION.read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    for statement in ("UPDATE ", "DELETE ", "ALTER TABLE", "CREATE TABLE", "CREATE VIEW"):
        assert statement not in code, statement
    assert code.count("INSERT INTO lab.") == 1
    assert code.rstrip().endswith("INSERT INTO lab.schema_migrations(version) VALUES(30);")
    # Nothing of migrations 028 and 029 (the public page's views) is read or replaced here.
    assert "public_page" not in code and "experiment_" not in code


def test_030_applies_to_a_schema_29_ledger_without_an_audit_event_and_v2_routes_unchanged():
    import tempfile

    import psycopg

    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.config import SCHEMA_VERSION
    from tests.test_operator_controls import start_cluster_at

    assert SCHEMA_VERSION == 31  # 031 (plugin-c3) follows; this proves 030.
    with tempfile.TemporaryDirectory(prefix="catalyst-030-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 29)
            owner = localdb.connection_url(root, "lab_owner")
            with psycopg.connect(owner) as conn:
                head = conn.execute("SELECT event_hash FROM lab.trade_events "
                                    "ORDER BY seq DESC LIMIT 1").fetchone()
                assert conn.execute("SELECT max(version) FROM lab.schema_migrations"
                                    ).fetchone()[0] == 29
                probe = '{"selection_policy":"JEV_TOP_K_SELECTION_V2"}'
                before = conn.execute("SELECT lab.managed_review_failure(%s::jsonb)",
                                      (probe,)).fetchone()[0]
                conn.execute(V3_MIGRATION.read_text())
            with psycopg.connect(owner) as conn:
                assert conn.execute("SELECT event_hash FROM lab.trade_events "
                                    "ORDER BY seq DESC LIMIT 1").fetchone() == head
                assert conn.execute("SELECT max(version) FROM lab.schema_migrations"
                                    ).fetchone()[0] == 30
                assert conn.execute("SELECT lab.managed_review_failure(%s::jsonb)",
                                    (probe,)).fetchone()[0] == before
                assert conn.execute(
                    "SELECT lab.managed_review_failure(%s::jsonb)",
                    ('{"selection_policy":"JEV_TOP_K_SELECTION_V3"}',)).fetchone()[0] is not None
            from tests.test_risk_v5 import apply_migration_031

            apply_migration_031(root)  # This release's schema is 31 (package plugin-c3).
            Repository(localdb.connection_url(root)).check_role()
            RiskRepository(localdb.connection_url(root, "catalyst_risk")).check_role()
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
