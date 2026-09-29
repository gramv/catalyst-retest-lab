"""Selection rule B1 (MUSE_JEV_RESEARCH_SELECTION_B1_V1), plan 1.7 lean form.

Fixture and disposable-PostgreSQL evidence only: a scripted mock Jev transport, the mock paper
venue of test_managed_execution; no provider, broker, service or owner-ledger contact.
"""

import asyncio
import base64
import copy
import hashlib
import importlib.util
import itertools
import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab import localdb
from catalyst_lab import managed_runtime as runtime_module
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import INSUFFICIENT, SKEPTIC
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy, ReviewResult
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_runtime import (
    PERMANENT_ADMISSION_REFUSALS,
    ManagedRuntime,
    build_runtime_from_env,
)
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle, selection_disposition
from catalyst_lab.research_ranking import (
    QUALITY,
    QUALITY_CATEGORIES,
    QUALITY_V2,
    QUALITY_V2_POLICY,
    meets_quality_floor,
    quality_category,
)
from catalyst_lab.research_selection_b1 import (
    ACTIVATION_KIND,
    B1_POLICY,
    FLOOR_ENV,
    RULE_ENV,
    SHADOW_KIND,
    V2_POLICY,
    SelectionRule,
    b1_disposition,
    selection_rule_from_env,
    shadow_key,
)
from catalyst_lab.review_storage import ReviewStorage
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_operator_controls import audit_state, populate_schema14, start_cluster_at

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_selection_policy.py"
SPEC = importlib.util.spec_from_file_location("replay_selection_policy_under_test", SCRIPT)
replay_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay_script)
MIGRATIONS = Path(localdb.__file__).with_name("migrations")
B1_MIGRATION = MIGRATIONS / "018_selection_b1.sql"

# --- The rule itself: full truth table against V2 ------------------------------------------

VERDICTS = ("APPROVE", "REJECT", "NEEDS_REVIEW", INSUFFICIENT)
YES_NO = ("YES", "NO", INSUFFICIENT)
PRICED = ("LOW", "MEDIUM", "HIGH", INSUFFICIENT)
PASSING = {
    "news_stale": {"NO"},
    "unsupported_inference": {"NO"},
    "already_priced": {"LOW", "MEDIUM"},
}


def answers_for(verdict, stale, unsupported, priced):
    return replay_script.skeptic_reply(verdict, stale, unsupported, priced)["answers"]


def reviewed(answers, receipts=("receipt-1",)):
    """A ReviewResult with the status JevReviewer.jev_review itself assigns."""
    uncertain = any(
        answer["choice"] in {INSUFFICIENT, "NEEDS_REVIEW"}
        or sum(p == max(answer["probabilities"].values())
               for p in answer["probabilities"].values()) > 1
        for answer in answers.values()
    )
    return ReviewResult(
        "request-1",
        "NEEDS_REVIEW" if uncertain else "RECORDED",
        "UNCERTAIN_JUDGMENT" if uncertain else None,
        receipts,
        answers,
    )


def v2_oracle(verdict, stale, unsupported, priced):
    """The owner's 2026-09-19 conjunction with REJECT precedence, written independently."""
    if verdict == "REJECT":
        return "REJECTED"
    if (verdict, stale, unsupported) == ("APPROVE", "NO", "NO") and priced in {"LOW", "MEDIUM"}:
        return "APPROVED"
    return "NEEDS_REVIEW"


def b1_oracle(stale, unsupported, priced):
    """B1: the three components only; the verdict never enters."""
    if (stale, unsupported) == ("NO", "NO") and priced in {"LOW", "MEDIUM"}:
        return "APPROVED"
    return "NEEDS_REVIEW"


@pytest.mark.parametrize(
    ("verdict", "stale", "unsupported", "priced"),
    list(itertools.product(VERDICTS, YES_NO, YES_NO, PRICED)),
)
def test_full_truth_table_of_b1_against_v2(verdict, stale, unsupported, priced):
    result = reviewed(answers_for(verdict, stale, unsupported, priced))
    v2 = selection_disposition(result)[0]
    assert v2 == v2_oracle(verdict, stale, unsupported, priced)
    outcome = b1_disposition(result)
    assert outcome.disposition == b1_oracle(stale, unsupported, priced)
    assert (outcome.dissent, outcome.dissent_tied) == (verdict, False)  # Recorded, never blocking.
    labels = {"news_stale": stale, "unsupported_inference": unsupported, "already_priced": priced}
    unresolved = [f"{n.upper()}_INSUFFICIENT" for n, v in labels.items() if v == INSUFFICIENT]
    failed = [
        f"{n.upper()}_{v}" for n, v in labels.items() if v != INSUFFICIENT and v not in PASSING[n]
    ]
    if outcome.disposition == "APPROVED":
        assert (outcome.reason, outcome.reasons) == (
            "B1_COMPONENTS_PASSED", ("B1_COMPONENTS_PASSED",))
    else:
        assert outcome.reason == ("UNRESOLVED_EVIDENCE" if unresolved else "COMPONENTS_NOT_PASSED")
        assert list(outcome.reasons) == unresolved + failed
    assert v2 != "APPROVED" or outcome.disposition == "APPROVED"  # Every V2 approval is B1's.


TIES = {
    "verdict": "TIE:APPROVE/REJECT",
    "news_stale": "TIE:NO/YES",
    "unsupported_inference": "TIE:NO/YES",
    "already_priced": "TIE:LOW/HIGH",
}


@pytest.mark.parametrize("question", sorted(TIES))
def test_a_tied_component_is_unresolved_while_a_tied_verdict_is_only_dissent(question):
    labels = {"verdict": "APPROVE", "news_stale": "NO", "unsupported_inference": "NO",
              "already_priced": "LOW", question: TIES[question]}
    result = reviewed(answers_for(labels["verdict"], labels["news_stale"],
                                  labels["unsupported_inference"], labels["already_priced"]))
    assert result.status == "NEEDS_REVIEW"  # The reviewer marks every tie uncertain.
    assert selection_disposition(result) == ("NEEDS_REVIEW", "UNCERTAIN_JUDGMENT")
    outcome = b1_disposition(result)
    if question == "verdict":
        assert (outcome.disposition, outcome.dissent, outcome.dissent_tied) == (
            "APPROVED", "APPROVE", True)
    else:
        assert (outcome.disposition, outcome.reason, outcome.reasons) == (
            "NEEDS_REVIEW", "UNRESOLVED_EVIDENCE", (question.upper() + "_TIED",))


def test_a_tied_rejecting_verdict_keeps_v2_reject_precedence_and_is_b1_dissent():
    result = reviewed(answers_for("TIE:REJECT/APPROVE", "NO", "NO", "MEDIUM"))
    assert selection_disposition(result) == ("REJECTED", "JEV_REJECTED")
    outcome = b1_disposition(result)
    assert (outcome.disposition, outcome.dissent, outcome.dissent_tied) == (
        "APPROVED", "REJECT", True)


VALID = answers_for("APPROVE", "NO", "NO", "LOW")


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (ReviewResult("r", "NEEDS_REVIEW", "HTTP_529", ("a", "b"), {}), "HTTP_529"),
        (ReviewResult("r", "NEEDS_REVIEW", "RECEIPT_INTEGRITY_FAILED", ("a",), {}),
         "RECEIPT_INTEGRITY_FAILED"),
        (ReviewResult("r", "NEEDS_REVIEW", "INTERRUPTED_REVIEW", (), {}), "INTERRUPTED_REVIEW"),
        (ReviewResult("r", "RECORDED", None, (), VALID), "MISSING_VALID_REVIEW"),
        (ReviewResult("r", "RECORDED", None, ("a",),
                      {k: v for k, v in VALID.items() if k != "verdict"}), "INVALID_REVIEW"),
    ],
)
def test_invalid_missing_or_failed_reviews_select_under_neither_rule(result, code):
    outcome = b1_disposition(result)
    assert (outcome.disposition, outcome.reason, outcome.dissent) == ("NEEDS_REVIEW", code, None)
    assert selection_disposition(result) == ("NEEDS_REVIEW", code)


# --- Configuration and the QUALITY_V2 category ---------------------------------------------


def test_selection_rule_configuration_is_exact_and_b1_requires_the_floor():
    assert selection_rule_from_env({}) == SelectionRule() == SelectionRule(V2_POLICY, None)
    assert selection_rule_from_env({RULE_ENV: "", FLOOR_ENV: ""}) == SelectionRule()
    assert selection_rule_from_env({RULE_ENV: V2_POLICY}).b1 is False
    for floor in ("WEAK", "ADEQUATE", "STRONG"):
        rule = selection_rule_from_env({RULE_ENV: B1_POLICY, FLOOR_ENV: floor})
        assert (rule.policy, rule.quality_floor, rule.b1) == (B1_POLICY, floor, True)
    for floor in (None, "", "adequate", " ADEQUATE", "0.5", "MEDIUM", INSUFFICIENT):
        env = {RULE_ENV: B1_POLICY, **({FLOOR_ENV: floor} if floor is not None else {})}
        with pytest.raises(ValueError, match="^SELECTION_QUALITY_FLOOR_REQUIRED$"):
            selection_rule_from_env(env)
    with pytest.raises(ValueError, match="^SELECTION_QUALITY_FLOOR_REQUIRES_B1$"):
        selection_rule_from_env({FLOOR_ENV: "ADEQUATE"})
    for name in ("muse_jev_research_selection_b1_v1", "MUSE_JEV_RESEARCH_SELECTION_V3", "B1"):
        with pytest.raises(ValueError, match="^UNKNOWN_SELECTION_RULE$"):
            selection_rule_from_env({RULE_ENV: name, FLOOR_ENV: "ADEQUATE"})


def test_quality_floor_is_a_category_check_never_a_confidence_threshold():
    strong = replay_script.quality_v2_reply("STRONG")["answers"]
    assert quality_category(strong) == "STRONG"
    unsure = copy.deepcopy(strong)
    unsure["quality_category"]["confidence"] = 0.01
    assert quality_category(unsure) == "STRONG"
    for label in (INSUFFICIENT, "TIE:STRONG/WEAK"):
        assert quality_category(replay_script.quality_v2_reply(label)["answers"]) is None
    assert quality_category({}) is None
    assert [c for c in QUALITY_CATEGORIES if meets_quality_floor(c, "ADEQUATE")] == [
        "ADEQUATE", "STRONG"]
    assert not meets_quality_floor(None, "WEAK") and not meets_quality_floor("STRONG", None)
    # V1's score questions are kept verbatim; one category Choice with Insufficient evidence.
    assert {k: v for k, v in QUALITY_V2.questions.items() if k != "quality_category"} == (
        QUALITY.questions)
    assert set(QUALITY_V2.questions["quality_category"]["criteria"]) == {
        *QUALITY_CATEGORIES, INSUFFICIENT}
    # Parity of code and the SQL literals of migration 018.
    sql = B1_MIGRATION.read_text()
    assert QUALITY_V2.template_hash in sql and SKEPTIC.template_hash in sql
    assert f"'{B1_POLICY}'" in sql and f"'{QUALITY_V2_POLICY}'" in sql
    assert "ARRAY['WEAK','ADEQUATE','STRONG']" in sql


def test_runtime_factory_refuses_b1_without_its_floor(monkeypatch):
    from tests.test_managed_runtime import configure_env

    configure_env(monkeypatch)
    for key in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(RULE_ENV, B1_POLICY)
    with pytest.raises(ValueError, match="^REQUIRED_MANAGED_CONFIGURATION_MISSING_OR_INVALID$"):
        build_runtime_from_env()
    monkeypatch.setenv(FLOOR_ENV, "STRONG")
    with pytest.raises(ValueError, match="credentials are required"):
        build_runtime_from_env()  # Configuration passed; startup reached the next check.


def factory_selection(monkeypatch, env):
    """build_runtime_from_env with every external dependency replaced, as its own test does."""
    from contextlib import nullcontext

    import catalyst_lab.authorization as auth
    import catalyst_lab.managed_broker as broker_module
    import catalyst_lab.managed_execution as execution_module
    import catalyst_lab.managed_store as store_module
    import catalyst_lab.research_cycle as cycles
    import catalyst_lab.review_worker as workers
    import catalyst_lab.risk as risk_module
    import catalyst_lab.scan_sources as sources
    from catalyst_lab.account_risk import AccountRiskPolicy
    from catalyst_lab.alpaca import AlpacaCredentials
    from tests.test_managed_runtime import NOW, Execution, Research, Source, configure_env

    configure_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(AlpacaCredentials, "from_env",
                        lambda: AlpacaCredentials("PKFACTORYFIXTURE", "fixture-secret-only"))
    fake_repo = SimpleNamespace(check_role=lambda: None, require_same_database=lambda _: None,
                                connect=lambda: nullcontext(None))
    monkeypatch.setattr(auth, "RiskRepository", lambda *_, **__: fake_repo)
    frozen = AccountRiskPolicy("CATALYST_RETEST_V1", "FROZEN_V1", D("0.01"), D("0.02"),
                               {"US_STOCKS": D("0.02")}, 1, 1, ("US_STOCKS",), False, D(1),
                               None, 0, "LAB_FIXTURE")
    monkeypatch.setattr(risk_module, "load_policy", lambda *_, **__: frozen)
    monkeypatch.setattr(workers, "ReviewWorker", lambda _: SimpleNamespace(
        store=object(), reviewer=SimpleNamespace(store=object()), heartbeat=lambda: True,
        runtime=SimpleNamespace()))
    monkeypatch.setattr(store_module, "ManagedAuthorizationGate",
                        lambda repo: auth.AuthorizationGate(repo))
    monkeypatch.setattr(broker_module, "ManagedPaperBroker", lambda *_, **__: object())
    execution = Execution()
    execution.repo, execution.now = fake_repo, lambda: NOW
    monkeypatch.setattr(execution_module, "ManagedExecution", lambda *_, **__: execution)
    captured = {}

    def research_cycle(*args, **kwargs):
        captured.update(kwargs)
        return Research()

    monkeypatch.setattr(cycles, "ResearchCycle", research_cycle)
    monkeypatch.setattr(sources, "AlpacaMarketSource", lambda *args: Source())
    run = build_runtime_from_env()
    return captured["selection"], run.configuration_hash


def test_runtime_factory_wires_the_configured_rule_into_research_and_its_hash(monkeypatch):
    with monkeypatch.context() as patch:
        default, default_hash = factory_selection(patch, {})
    with monkeypatch.context() as patch:
        b1, b1_hash = factory_selection(patch, {RULE_ENV: B1_POLICY, FLOOR_ENV: "ADEQUATE"})
    assert default == SelectionRule() and b1 == SelectionRule(B1_POLICY, "ADEQUATE")
    assert default_hash != b1_hash


def test_runtime_start_records_the_activation_and_a_failed_write_never_stops_it(monkeypatch):
    from tests.test_managed_runtime import runtime as stub_runtime

    started = []

    class NoThread:
        def __init__(self, target, args, daemon):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(runtime_module.threading, "Thread", NoThread)

    def prepared(record):
        run = stub_runtime()
        run.research.record_selection_rule = record
        run.reconcile_once = lambda: None
        run._invalidate_market_gaps = lambda tick=None: None
        return run

    recorded = []
    run = prepared(lambda *, runtime_id: recorded.append(runtime_id))
    run.start()
    # Eight worker threads since fees-net-r added the fee-import loop.
    assert recorded == [run.runtime_id] and len(started) == 8
    assert run.error is None

    def unavailable(*, runtime_id):
        raise RuntimeError("database unavailable")

    failing = prepared(unavailable)
    failing.start()  # Protection threads still start; B1 simply stays unactivated.
    assert len(started) == 16 and failing.error is not None


# --- Database fixtures ---------------------------------------------------------------------


async def no_sleep(_):
    return None


def b1_rule(floor="ADEQUATE"):
    return SelectionRule(B1_POLICY, floor)


def make_cycle(mx, rule, script, quality=None, *, activate=True):
    engine, venue, receipts = mx
    calls = []
    reviewer = JevReviewer(
        receipts,
        ReliabilityPolicy("SELECTION_B1_FIXTURE", 10, 2, 0.01, 1000, 30),
        transport=httpx.MockTransport(
            replay_script.scripted_provider(script, quality or {}, calls)
        ),
        key_provider=lambda: replay_script.FIXTURE_KEY,
        clock=lambda: venue.now,
        sleep=no_sleep,
    )
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now, selection=rule)
    if activate:
        cycle.record_selection_rule(runtime_id=str(uuid4()))
    return cycle, calls


def submit(cycle, symbols, now):
    report_id = str(uuid4())
    cycle.start_report(
        {
            "report_id": report_id,
            "generated_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=5)).isoformat(),
            "items": [replay_script.fixture_item(symbol, now) for symbol in symbols],
        },
        max_seconds=300,
    )
    return report_id


def review(cycle, cycle_id):
    asyncio.run(cycle.tick(cycle_id))
    asyncio.run(cycle.tick(cycle_id))  # QUALITY judgments follow the decisions.
    return cycle.approved_packets(cycle_id)


def events(engine, kind, **body):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq", (kind,)
        ).fetchall()
    return [r for r in rows if all(r["body"].get(k) == v for k, v in body.items())]


def item_rows(cycle, cycle_id, kind, symbol):
    return [
        e["body"] for e in cycle.outputs(cycle_id, limit=1000)
        if e["kind"] == kind and e["body"].get("item_key", "").endswith(":" + symbol)
    ]


def review_failure(engine, packet):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT lab.managed_review_failure(%s) AS reason", (Jsonb(json_safe(packet)),)
        ).fetchone()["reason"]


def owner_url(engine):
    return engine.repo.database_url.replace("user=catalyst_risk", "user=lab_owner")


def as_role(repo, role):
    return repo.database_url.replace("user=catalyst_app", "user=" + role)


def classify(engine, symbol):
    with engine.store.transaction() as conn:
        event = system_event(engine.repo, conn, "CLASSIFICATION_IMPORTED",
                             {"ticker": symbol, "source": "SELECTION_B1_FIXTURE"})
        conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                      (event["seq"], symbol, symbol, symbol, "SELECTION_B1_FIXTURE"))


DROP = object()


def forge(engine, cycle, cycle_id, symbol, *, quality_from=None, write=True, **changes):
    """A selection body built from the item's own receipts, written directly by the risk role
    as a code defect could; admission SQL must judge it, not the code that built it."""
    packet = item_rows(cycle, cycle_id, "RESEARCH_PACKET", symbol)[-1]
    decision = item_rows(cycle, cycle_id, "RESEARCH_DECISION", symbol)[-1]
    result = cycle._existing_result(packet, decision["request_id"])
    quality = item_rows(cycle, cycle_id, "RESEARCH_QUALITY", quality_from or symbol)
    q = quality[-1] if quality else {}
    body = {
        **cycle._selected_body(packet, result),
        "dissent": b1_disposition(result).dissent,
        "quality_required": True,
        "quality_policy": QUALITY_V2_POLICY,
        "quality_receipt_id": (q.get("receipt_ids") or [None])[-1],
        "quality_score": q.get("score"),
        "quality_category": q.get("category"),
        "quality_floor": "ADEQUATE",
        "quality_rank": 1,
        "quality_candidate_count": 1,
        "selection_limit": 10,
    }
    for key, value in changes.items():
        if value is DROP:
            body.pop(key)
        else:
            body[key] = value
    body = json_safe(body)
    if not write:
        return body
    with engine.store.transaction() as conn:
        row = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": body})
    return {**body, "selection_event_seq": row["event_seq"]}


def tamper(engine, receipt_id):
    """Privileged byte change in the disposable fixture DB only; still valid JSON."""
    with psycopg.connect(owner_url(engine)) as owner:
        owner.execute("ALTER TABLE lab.jev_receipts DISABLE TRIGGER immutable_rows")
        owner.execute(
            "UPDATE lab.jev_receipts SET response_bytes=response_bytes||convert_to(' ','UTF8')"
            " WHERE receipt_id=%s",
            (receipt_id,),
        )
        owner.execute("ALTER TABLE lab.jev_receipts ENABLE TRIGGER immutable_rows")


# --- Shadow mode: always on, never admitted --------------------------------------------------

SHADOW_SCRIPT = {
    "SHA": ("APPROVE", "NO", "NO", "LOW", ()),  # Both rules approve.
    "SHB": ("REJECT", "NO", "NO", "MEDIUM", (529,)),  # V2 rejects, B1 approves; two receipts.
    "SHC": ("APPROVE", "NO", "NO", "LOW", (500,)),  # Provider failure.
    "SHD": ("REJECT", "YES", "NO", "HIGH", ()),  # Both decline.
}


def test_shadow_disposition_once_per_receipt_chain_and_outside_the_cycle(mx):
    engine, venue, _ = mx
    cycle, _ = make_cycle(mx, SelectionRule(), SHADOW_SCRIPT)
    cycle_id = submit(cycle, SHADOW_SCRIPT, venue.now)
    assert [p["symbol"] for p in review(cycle, cycle_id)] == ["SHA"]
    shadows = events(engine, SHADOW_KIND, research_cycle_id=cycle_id)
    by_symbol = {row["body"]["item_key"].split(":")[-1]: row for row in shadows}
    assert len(shadows) == 4 and sorted(by_symbol) == sorted(SHADOW_SCRIPT)
    decisions = {s: item_rows(cycle, cycle_id, "RESEARCH_DECISION", s)[-1] for s in SHADOW_SCRIPT}
    expected = {
        "SHA": ("APPROVED", ["B1_COMPONENTS_PASSED"], "APPROVE", 1, "APPROVED"),
        "SHB": ("APPROVED", ["B1_COMPONENTS_PASSED"], "REJECT", 2, "REJECTED"),
        "SHC": ("NEEDS_REVIEW", ["HTTP_500"], None, 1, "NEEDS_REVIEW"),
        "SHD": ("NEEDS_REVIEW", ["NEWS_STALE_YES", "ALREADY_PRICED_HIGH"], "REJECT", 1,
                "REJECTED"),
    }
    for symbol, (disposition, reasons, dissent, receipts, v2) in expected.items():
        row, decision = by_symbol[symbol], decisions[symbol]
        body = row["body"]
        assert (body["disposition"], body["reasons"], body["dissent"]) == (
            disposition, reasons, dissent)
        # Coalesced: one event covers the whole receipt chain, keyed by its final receipt.
        assert body["receipt_ids"] == decision["receipt_ids"] and len(body["receipt_ids"]) == (
            receipts)
        assert body["receipt_id"] == body["receipt_ids"][-1]
        assert row["idempotency_key"] == shadow_key(body["receipt_id"])
        assert (body["policy"], body["active_policy"], body["admissible"]) == (
            B1_POLICY, V2_POLICY, False)
        assert body["evidence_hash"] == decision["evidence_hash"] and "cycle_id" not in body
        assert decision["disposition"] == v2 and "dissent" not in decision  # V2 unchanged.
    assert SHADOW_KIND not in {e["kind"] for e in cycle.outputs(cycle_id, limit=1000)}
    # Idempotent per receipt: a re-run review, more ticks and publication add nothing.
    packet = item_rows(cycle, cycle_id, "RESEARCH_PACKET", "SHB")[-1]
    claim = item_rows(cycle, cycle_id, "RESEARCH_CLAIM", "SHB")[-1]
    again = json_safe(asyncio.run(cycle._review(packet, claim)))
    assert {**again, "cycle_id": cycle_id} == decisions["SHB"]
    asyncio.run(cycle.tick(cycle_id))
    cycle.approved_packets(cycle_id)
    assert [r["event_seq"] for r in events(engine, SHADOW_KIND, research_cycle_id=cycle_id)] == [
        r["event_seq"] for r in shadows]
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_shadow_b1_approval_can_never_be_selected_or_admitted(mx):
    engine, venue, _ = mx
    script = {"SHX": ("REJECT", "NO", "NO", "LOW", ())}
    cycle, _ = make_cycle(mx, SelectionRule(), script)
    cycle_id = submit(cycle, script, venue.now)
    assert review(cycle, cycle_id) == []
    [shadow] = events(engine, SHADOW_KIND, research_cycle_id=cycle_id)
    assert shadow["body"]["disposition"] == "APPROVED"
    # Publication and the runtime's admission queue read RESEARCH_SELECTED only.
    assert not events(engine, "RESEARCH_SELECTED", cycle_id=cycle_id)
    assert ManagedRuntime._selected_packets(SimpleNamespace(execution=engine)) == []
    classify(engine, "SHX")
    body = forge(engine, cycle, cycle_id, "SHX", write=False)
    with pytest.raises(ValueError, match="^DURABLE_SELECTION_BINDING_REQUIRED$"):
        engine.admit({**body, "selection_event_seq": shadow["event_seq"]})
    # An owner activation recorded after this V2 cycle started does not make it a B1 cycle.
    make_cycle(mx, b1_rule(), script)
    assert events(engine, ACTIVATION_KIND)
    forged = forge(engine, cycle, cycle_id, "SHX", selection_policy=B1_POLICY)
    assert review_failure(engine, forged) == "SELECTION_RULE_NOT_ACTIVATED"
    with pytest.raises(ValueError, match="^SELECTION_RULE_NOT_ACTIVATED$"):
        engine.admit(forged)
    claimed_v2 = forge(engine, cycle, cycle_id, "SHX")  # The packet's own policy: V2.
    assert claimed_v2["selection_policy"] == V2_POLICY
    assert review_failure(engine, claimed_v2) == "JEV_SELECTION_REQUIRED"
    with pytest.raises(ValueError, match="^JEV_SELECTION_REQUIRED$"):
        engine.admit(claimed_v2)
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.managed_setups").fetchone()


# --- V2 unaffected --------------------------------------------------------------------------


def test_v2_cycles_bodies_sql_and_privileges_are_unchanged(mx):
    engine, venue, _ = mx
    script = {"VTA": ("APPROVE", "NO", "NO", "LOW", ()),
              "VTB": ("APPROVE", "YES", "NO", "LOW", ())}
    cycle, calls = make_cycle(mx, SelectionRule(), script)
    assert not events(engine, ACTIVATION_KIND)  # V2, the default, records no activation.
    cycle_id = submit(cycle, script, venue.now)
    [chosen] = review(cycle, cycle_id)
    started = next(e["body"] for e in cycle.outputs(cycle_id) if e["kind"] == "RESEARCH_STARTED")
    assert started["selection_policy"] == V2_POLICY and "selection_rule" not in started
    v2_keys = {"cycle_id", "item_key", "revision", "request_id", "receipt_ids", "evidence_hash",
               "answers", "disposition", "reason", "evidence_tasks"}
    for symbol in script:
        decision = item_rows(cycle, cycle_id, "RESEARCH_DECISION", symbol)[-1]
        extra = {"evidence_task_id"} if decision["disposition"] == "NEEDS_REVIEW" else set()
        assert set(decision) == v2_keys | extra
    assert chosen["selection_policy"] == V2_POLICY and chosen["quality_required"] is False
    assert not {"dissent", "quality_floor", "quality_category"} & set(chosen)
    assert not [c for c in calls if set(c["questions"]) == set(QUALITY_V2.questions)]
    assert review_failure(engine, chosen) is None
    with Repository(owner_url(engine)).connect() as owner:  # UTC session, as the app's.
        assert owner.execute("SELECT lab.managed_review_failure_before_b1(%s) AS r",
                             (Jsonb(json_safe(chosen)),)).fetchone()["r"] is None
    for name in ("managed_review_failure_before_b1", "managed_review_failure_v13"):
        with engine.repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT lab.{name}(%s)", (Jsonb({}),))


def previous_review_function():
    """The body of the last migration before B1's that defines lab.managed_review_failure."""
    headers = ("CREATE FUNCTION lab.managed_review_failure(packet jsonb)",
               "CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb)")
    for path in sorted(MIGRATIONS.glob("*.sql"), reverse=True):
        text = path.read_text()
        if path.name >= B1_MIGRATION.name or not any(h in text for h in headers):
            continue
        start = max(text.find(h) for h in headers)
        body = text.index("AS $$", start) + len("AS $$")
        return path.name, text[body:text.index("$$;", body)]
    raise AssertionError("NO_EARLIER_REVIEW_FUNCTION")


def test_every_non_b1_packet_keeps_the_previous_admission_function_byte_for_byte(er):
    name, body = previous_review_function()
    v2 = (MIGRATIONS / "014_managed_completion.sql").read_text()
    v2 = v2.split("CREATE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text")[1]
    v2 = v2.split("AS $$", 1)[1].split("$$;", 1)[0]
    with psycopg.connect(as_role(er, "lab_owner")) as conn:
        stored = conn.execute("""SELECT prosrc FROM pg_proc
            WHERE oid='lab.managed_review_failure_before_b1(jsonb)'::regprocedure""").fetchone()[0]
        current = conn.execute("""SELECT prosrc FROM pg_proc
            WHERE oid='lab.managed_review_failure(jsonb)'::regprocedure""").fetchone()[0]
    assert stored == body  # Renamed and delegated to, never edited.
    # V2's migration-014 statements are all still there (as the whole body when 014 is the
    # previous definition), and B1's branch lives only in the new function.
    assert all(line in stored for line in v2.splitlines() if line.strip())
    assert B1_POLICY not in stored and "managed_review_failure_before_b1(packet)" in current
    if name == "014_managed_completion.sql":
        assert stored == v2  # V2's function exactly as migration 014 wrote it.


# --- Active B1: components, dissent, the floor and the SQL branch -----------------------------

B1_SCRIPT = {
    "OKA": ("REJECT", "NO", "NO", "LOW", ()),  # Selected: the verdict is dissent only.
    "OKB": ("NEEDS_REVIEW", "NO", "NO", "MEDIUM", ()),  # Selected at the floor itself.
    "WKC": ("APPROVE", "NO", "NO", "MEDIUM", ()),  # WEAK: below the ADEQUATE floor.
    "INQ": ("APPROVE", "NO", "NO", "LOW", ()),  # QUALITY answers Insufficient evidence.
    "STD": ("APPROVE", "YES", "NO", "LOW", ()),  # A failing component.
    "TIE": ("APPROVE", "NO", "NO", "TIE:LOW/HIGH", ()),  # A tied component.
}
B1_QUALITY = {"OKA": "STRONG", "OKB": "ADEQUATE", "WKC": "WEAK", "INQ": INSUFFICIENT}


@pytest.fixture
def b1_selection(mx):
    engine, venue, _ = mx
    cycle, calls = make_cycle(mx, b1_rule(), B1_SCRIPT, B1_QUALITY)
    cycle_id = submit(cycle, B1_SCRIPT, venue.now)
    return engine, venue, cycle, cycle_id, review(cycle, cycle_id), calls


def test_b1_selects_on_components_with_dissent_and_the_owner_floor(b1_selection):
    engine, _, cycle, cycle_id, chosen, calls = b1_selection
    assert [p["symbol"] for p in chosen] == ["OKA", "OKB"]
    decisions = {s: item_rows(cycle, cycle_id, "RESEARCH_DECISION", s)[-1] for s in B1_SCRIPT}
    assert {s: d["disposition"] for s, d in decisions.items()} == {
        "OKA": "APPROVED", "OKB": "APPROVED", "WKC": "APPROVED", "INQ": "APPROVED",
        "STD": "NEEDS_REVIEW", "TIE": "NEEDS_REVIEW"}
    assert (decisions["OKA"]["selection_policy"], decisions["OKA"]["dissent"]) == (
        B1_POLICY, "REJECT")
    assert decisions["STD"]["reasons"] == ["NEWS_STALE_YES"] and decisions["STD"]["evidence_tasks"]
    assert decisions["TIE"]["reasons"] == ["ALREADY_PRICED_TIED"]
    # Every B1 approval, not only an over-capacity cutoff, gets one QUALITY_V2 judgment.
    judged = [c["state"]["candidate"]["symbol"] for c in calls
              if set(c["questions"]) == set(QUALITY_V2.questions)]
    assert sorted(judged) == ["INQ", "OKA", "OKB", "WKC"]
    quality = {s: item_rows(cycle, cycle_id, "RESEARCH_QUALITY", s)[-1]["category"]
               for s in ("OKA", "OKB", "WKC", "INQ")}
    assert quality == {"OKA": "STRONG", "OKB": "ADEQUATE", "WKC": "WEAK", "INQ": None}
    for packet, category, dissent in zip(chosen, ("STRONG", "ADEQUATE"),
                                         ("REJECT", "NEEDS_REVIEW"), strict=True):
        assert (packet["selection_policy"], packet["quality_policy"]) == (
            B1_POLICY, QUALITY_V2_POLICY)
        assert (packet["quality_category"], packet["quality_floor"], packet["dissent"]) == (
            category, "ADEQUATE", dissent)
        assert packet["quality_required"] is True and packet["quality_candidate_count"] == 2
        assert review_failure(engine, packet) is None
    assert [p["quality_rank"] for p in chosen] == [1, 2]
    shadows = events(engine, SHADOW_KIND, research_cycle_id=cycle_id)
    assert len(shadows) == 6 and {r["body"]["active_policy"] for r in shadows} == {B1_POLICY}


def test_genuine_b1_selection_is_admitted_and_its_entry_authorized(b1_selection, mx):
    engine, venue, _, _, chosen, _ = b1_selection
    packet = chosen[0]
    classify(engine, packet["symbol"])
    sid = engine.admit(packet)
    trigger = D(packet["levels"]["entry_trigger"])
    risk = engine.observe_trigger(sid, observation(
        mx, trade_price=str(trigger), bid=str(trigger - D(".01")), ask=str(trigger + D(".01"))))
    assert risk["outcome"] == "APPROVED"  # Dispatch re-ran lab.managed_review_failure.
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == packet["symbol"])
    assert D(entry["limit_price"]) == D(packet["levels"]["max_entry_price"])
    with engine.repo.connect() as conn:
        record = conn.execute("SELECT record_json FROM lab.managed_setups WHERE setup_id=%s",
                              (sid,)).fetchone()["record_json"]
    assert (record["selection_policy"], record["dissent"], record["quality_category"]) == (
        B1_POLICY, "REJECT", "STRONG")
    assert verify_events(engine.repo.export_events())["valid"]


FORGERIES = {
    "failing component": ("STD", "OKA", {}, "B1_COMPONENTS_REQUIRED"),
    "tied component": ("TIE", "OKA", {}, "B1_COMPONENTS_REQUIRED"),
    "quality not required": ("OKA", None, {"quality_required": False},
                             "QUALITY_RECEIPT_REQUIRED"),
    "quality receipt missing": ("OKA", None, {"quality_receipt_id": DROP},
                                "QUALITY_RECEIPT_REQUIRED"),
    "V1 quality claimed": ("OKA", None, {"quality_policy": "MUSE_JEV_COMPARATIVE_QUALITY_V1"},
                           "QUALITY_RECEIPT_REQUIRED"),
    "weak quality": ("WKC", None, {}, "QUALITY_FLOOR_NOT_MET"),
    "insufficient quality": ("INQ", None, {}, "QUALITY_FLOOR_NOT_MET"),
    "category inflated": ("WKC", None, {"quality_category": "STRONG"},
                          "QUALITY_RECEIPT_BINDING_FAILURE"),
    "another item's quality": ("OKA", "OKB", {}, "QUALITY_RECEIPT_BINDING_FAILURE"),
    "score changed": ("OKA", None, {"quality_score": 5.0}, "QUALITY_SCORE_MISMATCH"),
    "floor lowered": ("WKC", None, {"quality_floor": "WEAK"}, "SELECTION_RULE_NOT_ACTIVATED"),
    "dissent rewritten": ("OKA", None, {"dissent": "APPROVE"}, "RECEIPT_BINDING_FAILURE"),
    "claimed as V2": ("OKA", None, {"selection_policy": V2_POLICY}, "JEV_SELECTION_REQUIRED"),
}


@pytest.mark.parametrize("case", sorted(FORGERIES))
def test_admission_sql_refuses_a_forged_b1_packet(b1_selection, case):
    engine, _, cycle, cycle_id, _, _ = b1_selection
    symbol, quality_from, changes, code = FORGERIES[case]
    forged = forge(engine, cycle, cycle_id, symbol, quality_from=quality_from, **changes)
    assert review_failure(engine, forged) == code
    assert code in PERMANENT_ADMISSION_REFUSALS  # Declined once, never retried each tick.
    with pytest.raises(ValueError, match=f"^{code}$"):
        engine.admit(forged)


def test_tampered_receipt_bytes_refuse_the_b1_packet(b1_selection):
    engine, _, _, _, chosen, _ = b1_selection
    selected, at_floor = chosen
    tamper(engine, selected["receipt_id"])
    assert review_failure(engine, selected) == "RECEIPT_BINDING_FAILURE"
    with pytest.raises(ValueError):
        engine.admit(selected)  # The receipt's own audit verification fails first.
    tamper(engine, at_floor["quality_receipt_id"])
    assert review_failure(engine, at_floor) == "QUALITY_RECEIPT_BINDING_FAILURE"
    with pytest.raises(ValueError, match="^QUALITY_RECEIPT_BINDING_FAILURE$"):
        engine.admit(at_floor)


# --- Activation and the life of a cycle ------------------------------------------------------


def test_b1_intake_needs_the_recorded_activation_and_stores_nothing_without_it(mx):
    engine, venue, _ = mx
    script = {"ACT": ("REJECT", "NO", "NO", "LOW", ())}
    cycle, _ = make_cycle(mx, b1_rule(), script, {"ACT": "STRONG"}, activate=False)
    with pytest.raises(ValueError, match="^SELECTION_RULE_ACTIVATION_REQUIRED$"):
        submit(cycle, script, venue.now)
    assert not events(engine, "RESEARCH_STARTED") and not events(engine, "RESEARCH_PACKET")
    activation = cycle.record_selection_rule(runtime_id="runtime-a")
    assert activation["kind"] == ACTIVATION_KIND and activation["body"] == {
        "selection_policy": B1_POLICY, "quality_floor": "ADEQUATE",
        "quality_policy": QUALITY_V2_POLICY, "quality_categories": ["WEAK", "ADEQUATE", "STRONG"],
        "runtime_id": "runtime-a", "source": "OWNER_CONFIGURATION_MANAGED_SELECTION_RULE"}
    again = cycle.record_selection_rule(runtime_id="runtime-a")  # Once per runtime.
    assert again["event_seq"] == activation["event_seq"]
    cycle_id = submit(cycle, script, venue.now)
    started = next(e["body"] for e in cycle.outputs(cycle_id) if e["kind"] == "RESEARCH_STARTED")
    assert started["selection_policy"] == B1_POLICY and started["selection_rule"] == {
        "selection_policy": B1_POLICY, "quality_floor": "ADEQUATE",
        "quality_policy": QUALITY_V2_POLICY,
        "activation_event_id": str(activation["event_id"]),
        "activation_event_seq": activation["event_seq"]}
    [selected] = review(cycle, cycle_id)
    assert selected["dissent"] == "REJECT" and review_failure(engine, selected) is None


def test_a_cycle_keeps_the_rule_it_started_with(mx):
    engine, venue, _ = mx
    script = {"KPA": ("REJECT", "NO", "NO", "LOW", ()), "KPB": ("APPROVE", "NO", "NO", "LOW", ())}
    quality = {"KPA": "STRONG", "KPB": "STRONG"}
    v2_worker, v2_calls = make_cycle(mx, SelectionRule(), script, quality)
    v2_cycle = submit(v2_worker, script, venue.now)
    # The owner activates B1 (floor WEAK) after the V2 cycle started; a B1 worker ticks it.
    b1_worker, b1_calls = make_cycle(mx, b1_rule("WEAK"), script, quality)
    chosen = review(b1_worker, v2_cycle)
    assert [(p["symbol"], p["selection_policy"], p["quality_required"]) for p in chosen] == [
        ("KPB", V2_POLICY, False)]
    kpa = item_rows(b1_worker, v2_cycle, "RESEARCH_DECISION", "KPA")[-1]
    assert kpa["disposition"] == "REJECTED" and "dissent" not in kpa
    assert not [c for c in b1_calls if set(c["questions"]) == set(QUALITY_V2.questions)]
    # A B1 cycle ticked by a worker configured for V2 stays B1, with its own floor.
    b1_cycle = submit(b1_worker, script, venue.now)
    chosen = review(v2_worker, b1_cycle)
    assert [(p["symbol"], p["selection_policy"], p["quality_floor"]) for p in chosen] == [
        ("KPA", B1_POLICY, "WEAK"), ("KPB", B1_POLICY, "WEAK")]
    assert [review_failure(engine, p) for p in chosen] == [None, None]
    judged = [c for c in v2_calls if set(c["questions"]) == set(QUALITY_V2.questions)]
    assert len(judged) == 2


# --- Offline replay ------------------------------------------------------------------------


def strip_source(report):
    return {k: v for k, v in report.items() if k != "source"}


def one_receipt_export(identity, raw):
    """A hand-written CATALYST_SELECTION_REPLAY_EXPORT_V1 with one valid SKEPTIC receipt."""
    answers = json.loads(raw)["answers"]
    return {
        "format": replay_script.EXPORT_FORMAT,
        "jev_requests": [{"request_id": "request-1", "evidence_identity": identity,
                          "stage": "SKEPTIC", "question_set_version": SKEPTIC.version,
                          "template_hash": SKEPTIC.template_hash}],
        "jev_receipts": [{"receipt_id": "receipt-1", "request_id": "request-1", "attempt": 1,
                          "outcome": "VALID", "http_status": 200, "error_code": None,
                          "actual_model": "jev-1.13.0",
                          "response_hash": hashlib.sha256(raw).hexdigest(),
                          "response_bytes_base64": base64.b64encode(raw).decode()}],
        "ai_decisions": [{"decision_id": f"decision-{q}", "receipt_id": "receipt-1",
                          "question": q, "answer_json": a} for q, a in answers.items()],
    }


def test_replay_checks_each_receipt_and_skips_requests_without_a_research_item():
    raw = json.dumps(replay_script.skeptic_reply("REJECT", "NO", "NO", "LOW")).encode()
    probe = replay_script.replay(one_receipt_export({"proof_id": "p"}, raw), source="EXPORT")
    totals = probe["totals"]
    assert (totals["items"], totals["skipped_requests_without_research_item"]) == (0, 1)
    item = {"cycle_id": "c", "research_item_key": "US_STOCKS:X", "candidate_revision": 1,
            "evidence_hash": "e", "selection_policy": V2_POLICY}
    export = one_receipt_export(item, raw)
    [replayed] = replay_script.replay(export, source="EXPORT")["items"]
    assert (replayed["v2"]["disposition"], replayed["b1"]["disposition"]) == (
        "REJECTED", "APPROVED")
    assert (replayed["item_key"], replayed["recorded_policy"]) == ("US_STOCKS:X", V2_POLICY)
    tampered = copy.deepcopy(export)
    tampered["jev_receipts"][0]["response_bytes_base64"] = base64.b64encode(raw + b" ").decode()
    projection = copy.deepcopy(export)
    projection["ai_decisions"][0]["answer_json"] = {"type": "choice"}
    for broken in (tampered, projection):
        [replayed] = replay_script.replay(broken, source="EXPORT")["items"]
        assert replayed["v2"]["reason"] == replayed["b1"]["reason"] == "RECEIPT_INTEGRITY_FAILED"
    with pytest.raises(replay_script.ReplayRefused, match="^REPLAY_EXPORT_FORMAT_REQUIRED$"):
        replay_script.replay({"format": replay_script.EXPORT_FORMAT}, source="EXPORT")


def test_replay_of_a_fixture_ledger_and_of_its_export_agree_with_the_ledger(er, tmp_path,
                                                                           capsys):
    now = datetime.now(UTC)
    v2_cycle, b1_cycle = replay_script.run_fixture_cycles(
        as_role(er, "catalyst_risk"), as_role(er, "catalyst_jev"), now=now)
    review_url = as_role(er, "catalyst_review")
    export = replay_script.read_export(review_url)
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export))
    from_db = replay_script.replay(replay_script.read_export(review_url), source="DATABASE")
    from_file = replay_script.replay(replay_script.load_export(path), source="EXPORT")
    assert strip_source(from_db) == strip_source(from_file)
    assert replay_script.main(["--export", str(path)]) == 0
    cli_file = json.loads(capsys.readouterr().out)
    assert replay_script.main(["--database-url", review_url]) == 0
    cli_db = json.loads(capsys.readouterr().out)
    assert strip_source(cli_file) == strip_source(cli_db) == strip_source(from_db)
    # The offline replay reproduces what the runtime recorded, decision by decision.
    items = {i["item_key"].split(":")[-1]: i for i in from_db["items"]}
    with Repository(as_role(er, "catalyst_risk")).connect() as conn:
        rows = conn.execute("""SELECT kind,body FROM lab.managed_events
            WHERE kind IN ('RESEARCH_DECISION','RESEARCH_SHADOW_DISPOSITION')""").fetchall()
    recorded = {(r["kind"], r["body"]["item_key"].split(":")[-1]): r["body"] for r in rows}
    for symbol in replay_script.V2_CYCLE:
        assert items[symbol]["v2"]["disposition"] == recorded[
            ("RESEARCH_DECISION", symbol)]["disposition"]
    for symbol in replay_script.B1_CYCLE:
        assert items[symbol]["b1"]["disposition"] == recorded[
            ("RESEARCH_DECISION", symbol)]["disposition"]
    for symbol, item in items.items():
        assert item["b1"]["disposition"] == recorded[
            ("RESEARCH_SHADOW_DISPOSITION", symbol)]["disposition"]
    totals = from_db["totals"]
    assert (totals["items"], totals["v2"], totals["b1"]) == (
        14, {"APPROVED": 3, "NEEDS_REVIEW": 7, "REJECTED": 4},
        {"APPROVED": 8, "NEEDS_REVIEW": 6})
    selected = totals["would_select"]
    counts = {name: selected[name]["count"] for name in ("v2", "b1_without_floor", "b1_only")}
    assert counts == {"v2": 3, "b1_without_floor": 8, "b1_only": 5}
    assert {floor: v["count"] for floor, v in selected["b1_with_floor"].items()} == {
        "WEAK": 3, "ADEQUATE": 2, "STRONG": 1}
    assert totals["b1_approved_without_category"] == 5
    assert items["FXH"]["receipt_count"] == 2 and items["FXB"]["dissent"]["verdict"] == "REJECT"
    # What active B1 published is exactly the replay's selection at the cycle's floor.
    with Repository(as_role(er, "catalyst_risk")).connect() as conn:
        published = conn.execute("""SELECT body->'packet'->>'symbol' AS s FROM lab.managed_events
            WHERE kind='RESEARCH_SELECTED' AND body->'packet'->>'cycle_id'=%s ORDER BY 1""",
                                 (b1_cycle,)).fetchall()
    assert [f"{b1_cycle}/US_STOCKS:{r['s']}@1" for r in published] == (
        selected["b1_with_floor"]["ADEQUATE"]["items"])
    text = json.dumps(from_db)
    for fragment in ("Fixture issuer", "issuer.example", "new product", "Issuer withdrawal"):
        assert fragment not in text  # Codes, identifiers and counts only.


@pytest.mark.parametrize(
    ("role", "code"),
    [
        ("catalyst_app", "REPLAY_WRITE_CAPABLE_ROLE_REFUSED"),
        ("catalyst_risk", "REPLAY_WRITE_CAPABLE_ROLE_REFUSED"),
        ("lab_owner", "REPLAY_WRITE_CAPABLE_ROLE_REFUSED"),
        ("catalyst_jev", "REPLAY_READ_ONLY_ROLE_REQUIRED"),
        ("catalyst_reporting", "REPLAY_READ_ONLY_ROLE_REQUIRED"),
        ("catalyst_operator", "REPLAY_READ_ONLY_ROLE_REQUIRED"),
    ],
)
def test_replay_refuses_every_role_but_the_read_only_review_role(er, capsys, role, code):
    with pytest.raises(replay_script.ReplayRefused, match=f"^{code}$"):
        replay_script.read_export(as_role(er, role))
    assert replay_script.main(["--database-url", as_role(er, role)]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": code}


def test_review_role_reads_only_the_evidence_free_replay_views(er):
    review_url = as_role(er, "catalyst_review")
    ReviewStorage(review_url).check_role()  # Still no write privilege and no entry intents.
    views = ("selection_replay_requests", "selection_replay_receipts",
             "selection_replay_decisions")
    with psycopg.connect(review_url) as conn:
        def allowed(relation, privilege):
            return conn.execute("SELECT has_table_privilege(current_user,%s,%s)",
                                ("lab." + relation, privilege)).fetchone()[0]

        assert not any(allowed(t, "SELECT") for t in ("jev_requests", "jev_receipts",
                                                       "ai_decisions"))
        assert all(allowed(v, "SELECT") and not allowed(v, "INSERT") for v in views)
        columns = {r[0] for r in conn.execute("""SELECT column_name FROM
            information_schema.columns WHERE table_schema='lab'
            AND table_name LIKE 'selection_replay_%%'""").fetchall()}
    assert "request_json" not in columns and "response_bytes" in columns
    with psycopg.connect(as_role(er, "catalyst_reporting")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM lab.selection_replay_requests")


def test_owner_export_query_on_base_tables_equals_the_view_export(er, capsys):
    replay_script.run_fixture_cycles(as_role(er, "catalyst_risk"), as_role(er, "catalyst_jev"),
                                     now=datetime.now(UTC))
    assert replay_script.main(["--print-export-sql"]) == 0
    query = capsys.readouterr().out
    assert "lab.jev_requests" in query and "request_json" not in query
    with psycopg.connect(as_role(er, "lab_owner"),
                         options="-c default_transaction_read_only=on") as owner:
        base = owner.execute(query).fetchone()[0]
    assert base == replay_script.read_export(as_role(er, "catalyst_review"))
    assert len(base["jev_requests"]) == 18 and base["format"] == replay_script.EXPORT_FORMAT


def test_dump_fixture_writes_a_private_export_that_replays(tmp_path, capsys):
    path = tmp_path / "fixture-export.json"
    assert replay_script.main(["--dump-fixture", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert path.stat().st_mode & 0o777 == 0o600
    export = replay_script.load_export(path)
    assert replay_script.replay(export, source="LAB_FIXTURE_EXPORT") == report
    selected = report["totals"]["would_select"]
    assert (selected["b1_without_floor"]["count"], selected["b1_with_floor"]["ADEQUATE"]["count"],
            selected["v2"]["count"]) == (8, 2, 3)


# --- Migration: a populated schema-17 ledger migrated forward --------------------------------


def test_populated_schema17_ledger_migrates_ddl_only_and_v2_selections_still_admit(
    monkeypatch,
):
    with tempfile.TemporaryDirectory(prefix="catalyst-018-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 17)
            populate_schema14(root)
            risk_url = localdb.connection_url(root, "catalyst_risk")
            now = datetime.now(UTC)
            script = {"MGA": ("APPROVE", "NO", "NO", "LOW", ()),
                      "MGB": ("REJECT", "NO", "NO", "LOW", ())}
            with monkeypatch.context() as patch:
                # The V2 writers are unchanged; only the role check's pin is relaxed to write
                # this pre-migration fixture.
                patch.setattr("catalyst_lab.authorization.SCHEMA_VERSION", 17)
                reviewer = JevReviewer(
                    JevStore(localdb.connection_url(root, "catalyst_jev")),
                    ReliabilityPolicy("SELECTION_B1_MIGRATION_FIXTURE", 10, 1, 0.01, 1000, 30),
                    transport=httpx.MockTransport(
                        replay_script.scripted_provider(script, {}, [])),
                    key_provider=lambda: replay_script.FIXTURE_KEY,
                    clock=lambda: now,
                )
                cycle = ResearchCycle(RiskRepository(risk_url), reviewer,
                                      CyclePolicy(10, 10, 15, 60, 30), clock=lambda: now)
                cycle_id = submit(cycle, script, now)
                selected = review(cycle, cycle_id)
            assert [p["symbol"] for p in selected] == ["MGA"]

            def failures():
                with Repository(risk_url).connect() as conn:
                    return [conn.execute("SELECT lab.managed_review_failure(%s) AS r",
                                         (Jsonb(json_safe(p)),)).fetchone()["r"]
                            for p in selected]

            assert failures() == [None]  # Migration 017's function, before 018.
            proof, audited, halts, version = audit_state(root)
            assert version == 17 and proof["valid"] and all(n == ok for n, ok in audited.values())
            with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
                conn.execute(B1_MIGRATION.read_text())
                # The current schema (019, operator flatten, DDL-only) is applied too.
                conn.execute((MIGRATIONS / "019_operator_flatten.sql").read_text())
            after, audited_after, halts_after, version_after = audit_state(root)
            # DDL only: the same event count and head; every historical row still verifies.
            assert version_after == 19 and after == proof
            assert {t: v for t, v in audited_after.items() if t in audited} == audited
            assert audited_after["operator_flatten_completions"] == (0, 0)
            assert halts_after == halts
            assert failures() == [None]  # The V2 selection is still admissible after 018.
            review_url = localdb.connection_url(root, "catalyst_review")
            ReviewStorage(review_url).check_role()
            report = replay_script.replay(replay_script.read_export(review_url),
                                          source="DATABASE")
            assert report["totals"]["would_select"]["b1_only"]["count"] == 1  # MGB.
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
