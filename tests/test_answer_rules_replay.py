"""Round 5 of the first real-Jev loop (2026-09-27, jev-1.13.0) replayed through V1 and V2.

Evidence, not fixtures: ``artifacts/real-jev-loop-2026-09-27/events.json.gz`` is the session
harness export of that run (SESSION_SIMULATION venue and bars, real TypeSafe answers). Its
SHA-256 is the manifest's ``events.json`` and its hash chain verifies to the recorded head.
Each Jev answer below is the recorded response bytes, validated against the question set
rebuilt from the recorded request context (and equal to the recorded request's questions), as
recovery reads a receipt. V1's readers reproduce every recorded outcome; V2's readers read the
same bytes as package answer-rules intends: the six maintenance refusals are held, and the WIF
24-hour review continues with its stop raised to S1 (breakeven) and its target kept.
"""

import gzip
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import day_review as dr
from catalyst_lab import day_review_dossier as drd
from catalyst_lab import maintenance_dossier as md
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import digest, encoded, validated_answers
from catalyst_lab.managed_review import ManagedContext

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "real-jev-loop-2026-09-27"
# The run evidence (artifacts/) stays in the private checkout; a public one skips the replay.
pytestmark = pytest.mark.skipif(not ARTIFACTS.exists(), reason="artifacts/ is not in this checkout")
WIF, CRV, DOT, LINK, RENDER, LTC = (
    "22434b45-2d6c-4d53-a6ba-be9824669ae1", "1712860b-97f7-4c5a-8917-ef8856ba1583",
    "cd6624ce-9b93-4296-847d-144609d065d8", "f3faafad-77a1-434d-a9cd-2e6147c01571",
    "4c0e80e7-6a58-49c9-97b3-c7205ffdcb8e", "5689b453-88b4-4fd8-b7f4-0e846ac4ed5a")


class RoundFive:
    """The export's rows by table, and the recorded Jev answer of a request."""

    def __init__(self, rows):
        self.requests = {r["request_id"]: r for r in rows["JEV_REQUESTS"]}
        self.receipts = {}
        for receipt in rows["JEV_RECEIPTS"]:
            known = self.receipts.get(receipt["request_id"])
            if known is None or receipt["attempt"] > known["attempt"]:
                self.receipts[receipt["request_id"]] = receipt
        self.events = rows["MANAGED_EVENTS"]

    def of(self, kind, setup_id=None):
        return [e for e in self.events if e["kind"] == kind
                and (setup_id is None or e["setup_id"] == setup_id)]

    def one(self, kind, setup_id):
        [event] = self.of(kind, setup_id)
        return event["body"]

    def context(self, body):
        context = ManagedContext(encoded(body["context"]), body["context_hash"])
        assert digest(encoded(body["context"])) == body["context_hash"]  # Stored exactly.
        return context

    def answers(self, request_id, questions):
        request = self.requests[request_id]
        assert json.loads(request["request_json"])["questions"] == questions.questions
        assert request["question_set_version"] == questions.version
        receipt = self.receipts[request_id]
        assert receipt["outcome"] == "VALID" and receipt["actual_model"] == "jev-1.13.0"
        raw = bytes.fromhex(receipt["response_bytes"].removeprefix("\\x"))
        return validated_answers(raw, questions)


@pytest.fixture(scope="module")
def run():
    raw = gzip.open(ARTIFACTS / "events.json.gz").read()
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == manifest["files"]["events.json"]["sha256"]
    export = json.loads(raw)
    assert export["format"] == "AGENT_RESEARCH_SESSION_EVENTS_V1"
    assert verify_events(export["events"], export["verification"]["head_hash"])["valid"]
    rows = defaultdict(list)
    for event in export["events"]:
        payload = json.loads(event["event_body"])["payload_json"]
        if "row" in payload:
            rows[payload["kind"]].append(payload["row"])
    assert manifest["jev_calls"]["used"] == len(rows["JEV_RECEIPTS"]) == 31
    return RoundFive(rows)


def test_the_selection_ranked_nine_and_vetoed_sol_as_news_already_known(run):
    [ranking] = [e["body"] for e in run.of("RESEARCH_RANKING")]
    assert ranking["counts"] == {"RANKED": 9, "VETOED": 1, "NOT_RANKED": 0}
    [vetoed] = [e for e in ranking["entries"] if e["status"] == "VETOED"]
    assert (vetoed["symbol"], vetoed["veto_reasons"]) == ("SOL/USD", ["NEWS_STALE_YES"])


def test_the_six_maintenance_refusals_are_v1s_rule_and_v2_holds_every_one(run):
    requests = [e["body"] for e in run.of("POSITION_REVIEW_REQUEST")]
    assert {e["setup_id"] for e in run.of("POSITION_REVIEW_REQUEST")} == {
        WIF, CRV, DOT, LINK, RENDER, LTC}
    for body in requests:
        context = run.context(body)
        # Recorded under CRYPTO_MAINTENANCE_V1 (context and questions V4): V1's reader forever.
        assert context.data["policy"] == cm.CRYPTO_MAINTENANCE.record()
        assert context.data["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V4"
        assert md.answer_reader(context.data["policy"]) is md.read_answer
        questions = md.questions_for(context)
        assert questions.version == "JEV_MANAGED_POSITION_QUESTIONS_V4"
        answers = run.answers(body["request_id"], questions)
        options = context.data["options"]
        [recorded] = [e["body"] for e in run.of("MAINTENANCE_DECISION")
                      if e["body"]["request_id"] == body["request_id"]]
        v1 = md.read_answer(answers, options)
        assert (recorded["outcome"], recorded["code"]) == (
            "REFUSED", "CONTRADICTORY_MANAGEMENT_ANSWERS") == ("REFUSED", v1.code)
        assert recorded["answers"] == v1.summary and recorded["action"] == v1.action == "HOLD"
        # Every refusal is HOLD with stop_option S1: the stop answer is explicitly conditional
        # ("to use if the stop is raised"), and V1 demands both options KEEP with HOLD.
        assert (v1.stop_option, v1.target_option, v1.trade_reason) == ("S1", "KEEP", "INTACT")
        top = answers["action"]["probabilities"]
        assert top["HOLD"] == max(top.values()) and 0.38 <= top["HOLD"] <= 0.49
        v2 = md.read_answer_v2(answers, options)
        assert v2.code is None and v2.action == "HOLD"  # Held: nothing changes.
        assert not v2.changes and not v2.flagged and v2.held_code is None
        assert v2.option_use["stop"] == {"choice": "S1", "raise_to": None, "code": None,
                                         "reason": "NOT_RAISED_BY_ACTION"}
        assert v2.summary == v1.summary


def review_parts(run, setup_id):
    request = run.one("DAY_REVIEW_JEV_REQUEST", setup_id)
    context = run.context(request)
    assert context.data["policy"]["policy_id"] == "CRYPTO_24H_REVIEW_V1"
    assert dr.review_reader(context.data["policy"]) is dr.read_review_answer
    questions = drd.review_questions_for(context)
    assert questions.version == "JEV_DAY_REVIEW_QUESTIONS_V1"
    answers = run.answers(request["request_id"], questions)
    agent_asked = "agent_case" in questions.questions
    return request, context, answers, agent_asked


def test_wif_v1_exits_on_the_tied_target_and_v2_continues_with_the_stop_raised_to_s1(run):
    request, context, answers, agent_asked = review_parts(run, WIF)
    options = context.data["options"]
    target = answers["target_option"]["probabilities"]
    assert target["KEEP"] == target["T1"] == 0.44 == max(target.values())  # The tie.
    assert answers["decision"]["choice"] == "CONTINUE"
    assert answers["decision"]["probabilities"]["CONTINUE"] == 0.6
    result = run.one("DAY_REVIEW_JEV_RESULT", WIF)
    decision = run.one("DAY_REVIEW_DECISION", WIF)
    agent = decision["agent"]["answers"]["FIRST"]["decision"]
    assert agent == "CONTINUE"
    # V1, as recorded: the tie makes the whole answer uncertain, so the trade was sold.
    v1 = dr.read_review_answer(answers, options, agent_asked=agent_asked)
    assert v1.record() == result["answer"] and v1.code == "UNCERTAIN_JUDGMENT"
    assert (result["status"], result["code"]) == ("UNUSABLE", "UNCERTAIN_JUDGMENT")
    assert dr.first_round(v1, agent, addressee="fable") == (dr.EXIT, dr.JEV_ANSWER_UNUSABLE) \
        == (decision["outcome"], decision["code"])
    # V2: the decision is clear and agreed; the tie only keeps the target.
    v2 = dr.read_review_answer_v2(answers, options, agent_asked=agent_asked)
    assert v2.usable and v2.decision == "CONTINUE"
    assert (v2.raise_stop_to, v2.raise_target_to) == ("S1", None)
    assert v2.option_use["target"] == {"choice": "KEEP", "raise_to": None,
                                       "code": "TARGET_OPTION_NOT_USABLE", "reason": "TIED"}
    assert dr.first_round(v2, agent, addressee="fable") == (dr.CONTINUE, dr.AGREED)
    [s1] = [o for o in options["stop"] if o["option_id"] == dr.option_to_raise(v2, "stop")]
    assert s1["bases"] == ["BREAKEVEN"] and D(s1["price"]) == D("0.23424")  # The entry.
    # Phase 5's checks at the recorded quote pass: the stop would rise to S1, the target
    # stay 0.25679.
    basis, bid = context.data["basis"], D(decision["quote"]["bid"])
    decided = datetime.fromisoformat(decision["decided_at"])
    assert cm.check_change(
        old_stop=D(basis["stop"]), new_stop=D(s1["price"]), old_target=D(basis["target"]),
        new_target=None, bid=bid, min_bid=bid, max_bid=bid,
        answered_at=datetime.fromisoformat(result["answered_at"]), now=decided,
        increment=D(basis["increment"])) is None
    assert (basis["stop"], basis["target"]) == ("0.22692", "0.25679")


@pytest.mark.parametrize("setup_id,code", [
    (CRV, dr.AGREED),  # The agent said exit and Jev EXIT 0.97.
    (DOT, dr.AGENT_SILENT_JEV_ALONE),  # The answer window had closed: Jev alone, EXIT 0.58.
    (LINK, dr.AGENT_SILENT_JEV_ALONE),
])
def test_the_other_three_reviews_exit_under_both_rules(run, setup_id, code):
    _, context, answers, agent_asked = review_parts(run, setup_id)
    decision = run.one("DAY_REVIEW_DECISION", setup_id)
    agent = (decision["agent"]["answers"].get("FIRST") or {}).get("decision")
    for reader in (dr.read_review_answer, dr.read_review_answer_v2):
        answer = reader(answers, context.data["options"], agent_asked=agent_asked)
        assert answer.usable and answer.decision == dr.EXIT
        assert dr.first_round(answer, agent, addressee="fable") == (dr.EXIT, code) == (
            decision["outcome"], decision["code"])


def test_the_render_exit_flag_reads_the_same_under_both_versions(run):
    request = run.one("EXIT_FLAG_JEV_REQUEST", RENDER)
    context = run.context(request)
    answers = run.answers(request["request_id"], drd.EARLY_EXIT_QUESTIONS)
    answer = dr.read_flag_answer(answers)
    probabilities = answers["exit_now"]["probabilities"]
    assert answer.usable and answer.decision == dr.STAY
    assert (probabilities["STAY"], probabilities["EXIT"]) == (0.51, 0.44)
    assert dr.flag_resolution(answer.decision == dr.EXIT) == dr.EXIT_NOT_AGREED == run.one(
        "EARLY_EXIT_DECISION", RENDER)["outcome"]
    assert context.data["policy"]["policy_id"] == "EARLY_EXIT_AGREEMENT_V1"
