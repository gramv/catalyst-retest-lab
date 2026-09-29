"""Offline, read-only replay of selection rules V2, B1 and B2 over retained Jev receipts.

For each SKEPTIC receipt chain (one per research item revision) it reports the V2
disposition, the B1 disposition (``MUSE_JEV_RESEARCH_SELECTION_B1_V1``), the B2 disposition
(``MUSE_JEV_RESEARCH_SELECTION_B2_V1``), the verdict recorded as dissent, the item's QUALITY_V2
category when one exists, and totals: how many items each rule would select, and which, with
and without each QUALITY floor. A rule is replayed only over receipts of its own question set:
V2 and B1 over ``SKEPTIC_QUESTIONS_V1``, B2 over ``SKEPTIC_QUESTIONS_V2``; the other rules
report ``NOT_APPLICABLE`` / ``REPLAY_NOT_APPLICABLE_QUESTION_SET`` instead of an invented
disposition (V1 has no ``factual_claims_supported`` and V2 no ``unsupported_inference``).
SKEPTIC requests that name no research item (engineering probes and proofs) are counted and
skipped. It prints codes, identifiers and counts only, never evidence text; it makes no
provider or broker call and writes nothing to any ledger.

Sources (exactly one):

``--database-url URL``
    A ledger at schema 18 or later (the views of migration 018; from schema 20 they also
    carry SKEPTIC_QUESTIONS_V2 requests), read through the ``lab.selection_replay_*`` views
    as ``catalyst_review`` in a read-only session. Every other role is refused before a row
    is read (``catalyst_app``, ``catalyst_risk`` and ``lab_owner`` can write).
``--export PATH``
    A JSON export (format below); no database connection at all.
``--dump-fixture PATH``
    Builds a disposable LAB_FIXTURE ledger under /tmp (mock Jev transport, no provider,
    broker or owner ledger) with a V2, a B1 and a B2 cycle, writes its export to PATH through
    the read-only role, prints its replay and removes the ledger. This demonstrates the
    export format.
``--print-export-sql``
    Prints the single read-only query that produces an export from a ledger's base tables,
    for archived ledgers that predate the views. The owner runs it (for example with
    ``psql -AtX``) inside a read-only transaction; this script never connects as lab_owner.

Export format ``CATALYST_SELECTION_REPLAY_EXPORT_V1`` (one JSON object):

    {"format": "CATALYST_SELECTION_REPLAY_EXPORT_V1",
     "jev_requests": [{"request_id", "evidence_identity", "stage", "question_set_version",
                       "template_hash"}],
     "jev_receipts": [{"receipt_id", "request_id", "attempt", "outcome", "http_status",
                       "error_code", "actual_model", "response_hash",
                       "response_bytes_base64"}],
     "ai_decisions": [{"decision_id", "receipt_id", "question", "answer_json"}]}

Rows are limited to the question sets SKEPTIC_QUESTIONS_V1 and V2 and
MUSE_JEV_COMPARATIVE_QUALITY_V1 and V2 (a ledger before schema 20 exposes no V2 SKEPTIC rows
through the views; it has none); ``request_json`` (the reviewed evidence) is never exported.
The replay checks each receipt's response hash and its projected judgments; it does not
verify the database audit chain, and it does not apply the per-cycle selection limit, expiry
or supersession, app eligibility, reward/risk at max entry, B2's rationale requirement or the
risk limits (listed under ``not_applied``).
"""

import argparse
import asyncio
import base64
import hashlib
import json
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from catalyst_lab.jev_contract import SKEPTIC, SKEPTIC_V2, strict_json, validated_answers
from catalyst_lab.jev_review import ReviewResult
from catalyst_lab.research_cycle import _uncertain_answers, selection_disposition
from catalyst_lab.research_ranking import (
    QUALITY_CATEGORIES,
    QUALITY_POLICY,
    QUALITY_V2,
    QUALITY_V2_POLICY,
    meets_quality_floor,
    quality_category,
)
from catalyst_lab.research_selection_b1 import B1_POLICY, B2_POLICY, V2_POLICY, b1_disposition
from catalyst_lab.research_selection_b2 import b2_disposition

EXPORT_FORMAT = "CATALYST_SELECTION_REPLAY_EXPORT_V1"
REPORT_FORMAT = "SELECTION_POLICY_REPLAY_V1"
QUESTION_SETS = (SKEPTIC.version, SKEPTIC_V2.version, QUALITY_POLICY, QUALITY_V2_POLICY)
SKEPTIC_SETS = {SKEPTIC.version: SKEPTIC, SKEPTIC_V2.version: SKEPTIC_V2}
NOT_APPLICABLE = "REPLAY_NOT_APPLICABLE_QUESTION_SET"
READ_ONLY_ROLES = frozenset({"catalyst_review"})
WRITE_CAPABLE_ROLES = frozenset({"catalyst_app", "catalyst_risk", "lab_owner"})
VIEWS = {
    "requests": "lab.selection_replay_requests",
    "receipts": "lab.selection_replay_receipts",
    "decisions": "lab.selection_replay_decisions",
}
BASE_TABLES = {
    "requests": "lab.jev_requests",
    "receipts": "lab.jev_receipts",
    "decisions": "lab.ai_decisions",
}
NOT_APPLIED = [
    "SELECTION_LIMIT",
    "EXPIRY_AND_SUPERSESSION",
    "APP_ELIGIBILITY",
    "REWARD_RISK_AT_MAX_ENTRY",
    "RISK_LIMITS",
    "AUDIT_CHAIN_VERIFICATION",
    "B2_RATIONALE_REQUIREMENT",
]


class ReplayRefused(Exception):
    """A refused source or malformed export; the message is always a code."""


def export_sql(relations):
    """One read-only statement; the same filter applies to the views and the base tables."""
    versions = ",".join(f"'{version}'" for version in QUESTION_SETS)
    selected = (
        f"SELECT request_id FROM {relations['requests']} "
        f"WHERE question_set_version IN ({versions})"
    )
    return f"""SELECT json_build_object(
 'format','{EXPORT_FORMAT}',
 'jev_requests',(SELECT coalesce(json_agg(json_build_object(
   'request_id',q.request_id,'evidence_identity',q.evidence_identity,'stage',q.stage,
   'question_set_version',q.question_set_version,'template_hash',q.template_hash)
   ORDER BY q.request_id),'[]'::json)
  FROM {relations['requests']} q WHERE q.question_set_version IN ({versions})),
 'jev_receipts',(SELECT coalesce(json_agg(json_build_object(
   'receipt_id',r.receipt_id,'request_id',r.request_id,'attempt',r.attempt,
   'outcome',r.outcome,'http_status',r.http_status,'error_code',r.error_code,
   'actual_model',r.actual_model,'response_hash',r.response_hash,
   'response_bytes_base64',translate(encode(r.response_bytes,'base64'),E'\\n',''))
   ORDER BY r.request_id,r.attempt),'[]'::json)
  FROM {relations['receipts']} r WHERE r.request_id IN ({selected})),
 'ai_decisions',(SELECT coalesce(json_agg(json_build_object(
   'decision_id',d.decision_id,'receipt_id',d.receipt_id,'question',d.question,
   'answer_json',d.answer_json) ORDER BY d.receipt_id,d.question),'[]'::json)
  FROM {relations['decisions']} d WHERE d.receipt_id IN (
   SELECT receipt_id FROM {relations['receipts']} WHERE request_id IN ({selected})))
) AS export"""


def read_export(database_url):
    """Read an export as catalyst_review in a read-only session; other roles are refused."""
    with psycopg.connect(
        database_url,
        options="-c default_transaction_read_only=on -c timezone=UTC",
        row_factory=dict_row,
    ) as conn:
        role = conn.execute(
            """SELECT current_user AS role,
            rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls AS privileged
            FROM pg_roles WHERE rolname=current_user"""
        ).fetchone()
        if role["role"] in WRITE_CAPABLE_ROLES:
            raise ReplayRefused("REPLAY_WRITE_CAPABLE_ROLE_REFUSED")
        if role["role"] not in READ_ONLY_ROLES or role["privileged"]:
            raise ReplayRefused("REPLAY_READ_ONLY_ROLE_REQUIRED")
        if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
            raise ReplayRefused("REPLAY_READ_ONLY_SESSION_REQUIRED")
        return conn.execute(export_sql(VIEWS)).fetchone()["export"]


def load_export(path):
    try:
        export = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ReplayRefused("REPLAY_EXPORT_UNREADABLE") from None
    if not isinstance(export, dict) or export.get("format") != EXPORT_FORMAT:
        raise ReplayRefused("REPLAY_EXPORT_FORMAT_REQUIRED")
    return export


def _identity(request):
    identity = request.get("evidence_identity") or {}
    return {
        # Managed cycles, else the 2026-09-19 research-report flow's report and item IDs.
        "cycle_id": identity.get("cycle_id") or identity.get("report_id"),
        "item_key": identity.get("research_item_key") or identity.get("research_item_id"),
        "revision": identity.get("candidate_revision", identity.get("report_revision")),
        "recorded_policy": identity.get("selection_policy") or identity.get("research_policy_id"),
    }


def _receipt_answers(receipt, decisions, question_set):
    """Validated answers of one VALID receipt, or None when its bytes or projection differ."""
    try:
        raw = base64.b64decode(receipt["response_bytes_base64"] or "", validate=True)
        if hashlib.sha256(raw).hexdigest() != receipt.get("response_hash"):
            return None
        answers = validated_answers(raw, question_set)
        projected = {d["question"]: d["answer_json"] for d in decisions}
        if projected != answers:
            return None
        return answers
    except (ValueError, TypeError, KeyError):
        return None


def _result(request, receipts, decisions_by_receipt, question_set):
    """The runtime's recovery of one request (research_cycle._existing_result), offline."""
    request_id = request["request_id"]
    if not receipts:
        return ReviewResult(request_id, "NEEDS_REVIEW", "INTERRUPTED_REVIEW", (), {})
    ids = tuple(r["receipt_id"] for r in receipts)
    final = receipts[-1]
    if final["outcome"] != "VALID":
        code = final.get("error_code") or "PROVIDER_UNAVAILABLE"
        return ReviewResult(request_id, "NEEDS_REVIEW", code, ids, {})
    answers = _receipt_answers(final, decisions_by_receipt.get(final["receipt_id"], []),
                               question_set)
    if answers is None:
        return ReviewResult(request_id, "NEEDS_REVIEW", "RECEIPT_INTEGRITY_FAILED", ids, {})
    if question_set.stage != "SKEPTIC":
        return ReviewResult(request_id, "RECORDED", None, ids, answers)
    uncertain = _uncertain_answers(answers)
    return ReviewResult(
        request_id,
        "NEEDS_REVIEW" if uncertain else "RECORDED",
        "UNCERTAIN_JUDGMENT" if uncertain else None,
        ids,
        answers,
    )


def replay(export, *, source, floors=QUALITY_CATEGORIES):
    """Pure replay of one export; identical input gives identical output."""
    requests = export.get("jev_requests")
    receipts = export.get("jev_receipts")
    decisions = export.get("ai_decisions")
    if not all(isinstance(rows, list) for rows in (requests, receipts, decisions)):
        raise ReplayRefused("REPLAY_EXPORT_FORMAT_REQUIRED")
    by_request, by_receipt = {}, {}
    for receipt in receipts:
        by_request.setdefault(receipt["request_id"], []).append(receipt)
    for rows in by_request.values():
        rows.sort(key=lambda r: r["attempt"])
    for decision in decisions:
        by_receipt.setdefault(decision["receipt_id"], []).append(decision)
    categories = {}
    for request in requests:
        if (
            request.get("question_set_version") != QUALITY_V2_POLICY
            or request.get("template_hash") != QUALITY_V2.template_hash
        ):
            continue
        identity = request.get("evidence_identity") or {}
        result = _result(request, by_request.get(request["request_id"], []), by_receipt,
                         QUALITY_V2)
        key = (identity.get("cycle_id"), identity.get("research_item_key"),
               identity.get("candidate_revision"), identity.get("evidence_hash"))
        category = quality_category(result.answers) if result.status == "RECORDED" else None
        if category is not None:
            categories[key] = (category, "CATEGORIZED")
        elif result.status == "RECORDED":  # Insufficient evidence or tied: meets no floor.
            categories[key] = (None, "CATEGORY_UNRESOLVED")
        else:
            categories[key] = (None, result.reason)
    items, skipped = [], 0
    not_applicable = {"disposition": "NOT_APPLICABLE", "reason": NOT_APPLICABLE}
    for request in requests:
        question_set = SKEPTIC_SETS.get(request.get("question_set_version"))
        if (
            question_set is None
            or request.get("stage") != "SKEPTIC"
            or request.get("template_hash") != question_set.template_hash
        ):
            continue
        if not _identity(request)["item_key"]:
            skipped += 1  # Engineering probes and proofs review no research item.
            continue
        chain = by_request.get(request["request_id"], [])
        result = _result(request, chain, by_receipt, question_set)
        if question_set is SKEPTIC:
            # V2 and B1 read V1's answers; B2's components are not in a V1 receipt.
            v2_disposition, v2_reason = selection_disposition(result)
            b1 = b1_disposition(result)
            rules = {
                "v2": {"disposition": v2_disposition, "reason": v2_reason},
                "b1": {"disposition": b1.disposition, "reason": b1.reason,
                       "reasons": list(b1.reasons)},
                "b2": {**not_applicable, "reasons": [NOT_APPLICABLE]},
            }
            dissent = b1
        else:
            # B2 reads V2's answers; V1's unsupported_inference is not in a V2 receipt.
            b2 = b2_disposition(result)
            rules = {
                "v2": dict(not_applicable),
                "b1": {**not_applicable, "reasons": [NOT_APPLICABLE]},
                "b2": {"disposition": b2.disposition, "reason": b2.reason,
                       "reasons": list(b2.reasons)},
            }
            dissent = b2
        label = _identity(request)
        identity = request.get("evidence_identity") or {}
        category, quality_status = categories.get(
            (identity.get("cycle_id"), identity.get("research_item_key"),
             identity.get("candidate_revision"), identity.get("evidence_hash")),
            (None, "QUALITY_V2_UNAVAILABLE"),
        )
        items.append(
            {
                **label,
                "request_id": request["request_id"],
                "question_set_version": question_set.version,
                "receipt_id": chain[-1]["receipt_id"] if chain else None,
                "receipt_count": len(chain),
                **rules,
                "dissent": {"verdict": dissent.dissent, "tied": dissent.dissent_tied},
                "quality": {"status": quality_status, "category": category},
            }
        )
    items.sort(key=lambda i: (str(i["cycle_id"]), str(i["item_key"]), str(i["revision"])))

    def name(item):
        return f"{item['cycle_id']}/{item['item_key']}@{item['revision']}"

    def selection(chosen):
        return {"count": len(chosen), "items": [name(i) for i in chosen]}

    def counts(rule):
        return dict(sorted(Counter(i[rule]["disposition"] for i in items).items()))

    def with_floor(approved):
        return {
            floor: selection(
                [i for i in approved if meets_quality_floor(i["quality"]["category"], floor)]
            )
            for floor in floors
        }

    v2 = [i for i in items if i["v2"]["disposition"] == "APPROVED"]
    b1 = [i for i in items if i["b1"]["disposition"] == "APPROVED"]
    b2 = [i for i in items if i["b2"]["disposition"] == "APPROVED"]
    return {
        "replay": REPORT_FORMAT,
        "source": source,
        "policies": {"v2": V2_POLICY, "b1": B1_POLICY, "b2": B2_POLICY,
                     "quality": QUALITY_V2_POLICY},
        "question_sets": {"v2": SKEPTIC.version, "b1": SKEPTIC.version,
                          "b2": SKEPTIC_V2.version},
        "not_applied": NOT_APPLIED,
        "items": items,
        "totals": {
            "items": len(items),
            "skipped_requests_without_research_item": skipped,
            "v2": counts("v2"),
            "b1": counts("b1"),
            "b2": counts("b2"),
            "dissent": dict(
                sorted(
                    Counter(i["dissent"]["verdict"] or "NO_VALID_ANSWERS" for i in items).items()
                )
            ),
            "would_select": {
                "v2": selection(v2),
                "b1_without_floor": selection(b1),
                "b1_with_floor": with_floor(b1),
                "b1_only": selection([i for i in b1 if i not in v2]),
                "b2_without_floor": selection(b2),
                "b2_with_floor": with_floor(b2),
            },
            # Only active B1 cycles request QUALITY_V2, so shadow-only items have no category.
            "b1_approved_without_category": sum(
                i["quality"]["status"] != "CATEGORIZED" for i in b1
            ),
            "b2_approved_without_category": sum(
                i["quality"]["status"] != "CATEGORIZED" for i in b2
            ),
        },
    }


# --- Disposable fixture ledger (--dump-fixture and tests) -----------------------------------

FIXTURE_KEY = "fixture-selection-replay-no-provider-access"
FIXTURE_CYCLE_POLICY = {
    "selection_limit": 10,
    "review_deadline_seconds": 10,
    "claim_lease_seconds": 15,
    "max_packet_age_seconds": 60,
    "max_inflight": 30,
}
# symbol -> (verdict, news_stale, unsupported_inference, already_priced, provider plan).
# "TIE:<a>/<b>" ties two labels; a plan lists HTTP statuses before the answering 200.
V2_CYCLE = {
    "FXA": ("APPROVE", "NO", "NO", "LOW", ()),
    "FXB": ("REJECT", "NO", "NO", "MEDIUM", ()),
    "FXC": ("NEEDS_REVIEW", "NO", "NO", "LOW", ()),
    "FXD": ("APPROVE", "YES", "NO", "LOW", ()),
    "FXE": ("REJECT", "NO", "NO", "HIGH", ()),
    "FXF": ("APPROVE", "NO", "Insufficient evidence", "LOW", ()),
    "FXG": ("APPROVE", "NO", "NO", "TIE:LOW/HIGH", ()),
    "FXH": ("REJECT", "NO", "NO", "LOW", (529,)),
    "FXI": ("APPROVE", "NO", "NO", "LOW", (500,)),
}
B1_CYCLE = {
    "FYA": ("REJECT", "NO", "NO", "LOW", ()),
    "FYB": ("APPROVE", "NO", "NO", "MEDIUM", ()),
    "FYC": ("APPROVE", "NO", "NO", "LOW", ()),
    "FYD": ("NEEDS_REVIEW", "NO", "NO", "LOW", ()),
    "FYE": ("APPROVE", "YES", "NO", "LOW", ()),
}
B1_QUALITY = {"FYA": "STRONG", "FYB": "ADEQUATE", "FYC": "WEAK",
              "FYD": "Insufficient evidence"}
FIXTURE_FLOOR = "ADEQUATE"
# The B2 cycle of --dump-fixture: symbol -> SKEPTIC_QUESTIONS_V2 labels in SKEPTIC_V2_ORDER
# (news_stale, already_priced, mechanism_contradicted, inference_labelled,
# factual_claims_supported, verdict) and a provider plan.
B2_CYCLE = {
    "FZA": ("NO", "LOW", "NO", "YES", "SUPPORTED", "REJECT", ()),
    "FZB": ("NO", "MEDIUM", "NO", "YES", "SUPPORTED", "NEEDS_REVIEW", ()),
    "FZC": ("NO", "LOW", "NO", "YES", "PARTIALLY_SUPPORTED", "APPROVE", ()),
    "FZD": ("NO", "LOW", "YES", "YES", "SUPPORTED", "APPROVE", ()),
    "FZE": ("NO", "LOW", "NO", "YES", "SUPPORTED", "APPROVE", ("INVALID_SUM",)),
}
B2_QUALITY = {"FZA": "STRONG", "FZB": "WEAK", "FZE": "ADEQUATE"}


def choice_answer(question, label):
    options = list(question["criteria"])
    if label.startswith("TIE:"):
        first, second = label[4:].split("/")
        probabilities = {k: 0.5 if k in (first, second) else 0.0 for k in options}
        label = first
    else:
        probabilities = {k: float(k == label) for k in options}
    return {"type": "choice", "choice": label, "confidence": 0.8,
            "probabilities": probabilities}


def skeptic_reply(verdict, stale, unsupported, priced):
    from catalyst_lab.jev_contract import JEV_MODEL

    chosen = {"verdict": verdict, "news_stale": stale, "unsupported_inference": unsupported,
              "already_priced": priced}
    return {
        "model": JEV_MODEL,
        "answers": {name: choice_answer(question, chosen[name])
                    for name, question in SKEPTIC.questions.items()},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# SKEPTIC_QUESTIONS_V2 labels in the order agent_research_session's fixture scripts use.
SKEPTIC_V2_ORDER = ("news_stale", "already_priced", "mechanism_contradicted",
                    "inference_labelled", "factual_claims_supported", "verdict")


def skeptic_v2_reply(stale, priced, mechanism, inference, factual, verdict, *, questions=None):
    """A valid SKEPTIC_QUESTIONS_V2 reply (or of ``questions``, a same-shaped template)."""
    from catalyst_lab.jev_contract import JEV_MODEL

    chosen = dict(zip(SKEPTIC_V2_ORDER, (stale, priced, mechanism, inference, factual, verdict),
                      strict=True))
    return {
        "model": JEV_MODEL,
        "answers": {name: choice_answer(question, chosen[name])
                    for name, question in (questions or SKEPTIC_V2.questions).items()},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def quality_v2_reply(category, level=2):
    from catalyst_lab.jev_contract import JEV_MODEL

    answers = {}
    for name, question in QUALITY_V2.questions.items():
        if name == "quality_category":
            answers[name] = choice_answer(question, category)
            continue
        answers[name] = {
            "type": "score",
            "score": level,
            "legend": {str(i): text for i, text in enumerate(question["criteria"])},
            "confidence": 1.0,
            "probabilities": {str(i): float(i == level) for i in range(3)},
        }
    return {"model": JEV_MODEL, "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def fixture_rationale():
    """An AGENT_SELECTION_RATIONALE_V1 block citing the fixture item's one source."""
    return {
        "claims": [
            {"claim_id": "C1", "kind": "CATALYST",
             "text": "Fixture issuer reports a new product available to customers.",
             "supported_by": {"source_ids": ["issuer-source"], "bar_ids": []}},
            {"claim_id": "C2", "kind": "ECONOMIC_LINK",
             "text": "Inference: customers buying the product adds issuer revenue.",
             "supported_by": {"source_ids": ["issuer-source"], "bar_ids": []}},
        ],
        "why_now": "The fixture announcement is one hour old.",
        "why_these_levels": "Fixture levels around the proposed support and target.",
        "why_over_peers": "Fixture ranking only.",
        "what_would_change_my_mind": "A fixture withdrawal of the product.",
        "known_risks": ["Fixture risk."],
        "agent_confidence": {"level": "LOW", "basis": "Fixture only."},
    }


def fixture_item(symbol, now, *, rationale=False):
    item = {
        "signal_id": f"TEST-REPLAY-{symbol}",
        "market": "US_STOCKS",
        "symbol": symbol,
        "direction": "LONG",
        "catalyst": "PRODUCT",
        "thesis": "Fixture new product availability supports a technical retest hypothesis.",
        "disproof": "Issuer withdrawal or loss of structural support invalidates the thesis.",
        "economic_relationship": "Fixture customers buy the issuer product.",
        "technical_analysis": "Fixture research proposes the observed support and target.",
        "levels": {"entry_trigger": "105", "max_entry_price": "105.16", "stop": "103.9",
                   "target": "110"},
        "sources": [{"source_id": "issuer-source", "url": "https://issuer.example/announcement",
                     "excerpt": "Fixture issuer reports a new product available to customers.",
                     "published_at": (now - timedelta(hours=1)).isoformat(),
                     "retrieved_at": now.isoformat()}],
    }
    if rationale:
        item["selection_rationale"] = fixture_rationale()
    return item


def invalid_sum(reply):
    """The reply with a verdict distribution over 1 (INVALID_DISTRIBUTION_SUM), choice kept."""
    broken = json.loads(json.dumps(reply))
    verdict = broken["answers"]["verdict"]
    verdict["probabilities"] = {
        label: value + (0.01 if label != verdict["choice"] and value == 0 else 0)
        for label, value in verdict["probabilities"].items()
    }
    return broken


def scripted_provider(script, quality, calls, v2_script=None):
    """Mock TypeSafe transport keyed by symbol; it never opens a connection.

    ``v2_script`` answers SKEPTIC_QUESTIONS_V2 requests: symbol -> one row, or a list of rows
    for successive reviews (evidence revisions; the last repeats). A row is the six labels in
    SKEPTIC_V2_ORDER with an optional plan: HTTP statuses, or ``"INVALID_SUM"`` for a 200
    whose distribution does not sum to 1, returned before that review's valid reply.
    """
    import httpx

    from catalyst_lab.research_ranking import QUALITY

    served = Counter()
    v2_rows = {}  # symbol -> [review index, attempt within the review]

    def v2_reply(body):
        symbol = body["state"]["symbol"]
        rows = v2_script[symbol]
        rows = [rows] if isinstance(rows[0], str) else list(rows)
        index, attempt = v2_rows.setdefault(symbol, [0, 0])
        row = rows[min(index, len(rows) - 1)]
        labels, plan = row[:6], (row[6] if len(row) > 6 else ())
        reply = skeptic_v2_reply(*labels, questions=body["questions"])
        if attempt < len(plan):
            v2_rows[symbol][1] += 1
            if plan[attempt] == "INVALID_SUM":
                return httpx.Response(200, json=invalid_sum(reply))
            return httpx.Response(plan[attempt], json={"error": "fixture"})
        v2_rows[symbol] = [index + 1, 0]
        return httpx.Response(200, json=reply)

    def provider(request):
        body = strict_json(request.content)
        calls.append(body)
        if set(body["questions"]) == set(QUALITY_V2.questions):
            symbol = body["state"]["candidate"]["symbol"]
            return httpx.Response(200, json=quality_v2_reply(quality[symbol]))
        if set(body["questions"]) == set(QUALITY.questions):
            raise AssertionError("V1 QUALITY is not requested by this fixture")
        if set(body["questions"]) == set(SKEPTIC_V2.questions):
            return v2_reply(body)
        symbol = body["state"]["symbol"]
        verdict, stale, unsupported, priced, plan = script[symbol]
        attempt = served[symbol]
        served[symbol] += 1
        if attempt < len(plan):
            return httpx.Response(plan[attempt], json={"error": "fixture"})
        return httpx.Response(200, json=skeptic_reply(verdict, stale, unsupported, priced))

    return provider


def run_fixture_cycles(risk_url, jev_url, *, now):
    """Two cycles on a disposable ledger: one under V2 (B1 in shadow), one under active B1.

    Returns the cycle IDs. Every provider interaction is the scripted mock above.
    """
    import httpx

    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
    from catalyst_lab.jev_store import JevStore
    from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
    from catalyst_lab.research_selection_b1 import SelectionRule

    async def no_sleep(_):
        return None

    calls = []
    reviewer = JevReviewer(
        JevStore(jev_url),
        ReliabilityPolicy("SELECTION_REPLAY_FIXTURE", 10, 2, 0.01, 1000, 30),
        key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(
            scripted_provider({**V2_CYCLE, **B1_CYCLE}, B1_QUALITY, calls)
        ),
        clock=lambda: now,
        sleep=no_sleep,
    )
    repo = RiskRepository(risk_url)
    policy = CyclePolicy(**FIXTURE_CYCLE_POLICY)
    cycle_ids = []
    for script, rule in (
        (V2_CYCLE, SelectionRule()),
        (B1_CYCLE, SelectionRule(B1_POLICY, FIXTURE_FLOOR)),
    ):
        cycle = ResearchCycle(repo, reviewer, policy, clock=lambda: now, selection=rule)
        cycle.record_selection_rule(runtime_id=str(uuid4()))
        report_id = str(uuid4())
        cycle.start_report(
            {
                "report_id": report_id,
                "generated_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=5)).isoformat(),
                "items": [fixture_item(symbol, now) for symbol in script],
            },
            max_seconds=300,
        )
        asyncio.run(cycle.tick(report_id))
        asyncio.run(cycle.tick(report_id))  # QUALITY_V2 judgments, then publication.
        cycle.approved_packets(report_id)
        cycle_ids.append(report_id)
    return cycle_ids


def run_fixture_b2_cycle(risk_url, jev_url, *, now):
    """One active B2 cycle (floor ADEQUATE) on a disposable ledger; returns its cycle ID.

    Its items carry a selection rationale (B2 reviews the proposer's claims). FZE's first
    attempt is an invalid distribution, retried once by the reviewer.
    """
    import httpx

    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
    from catalyst_lab.jev_store import JevStore
    from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
    from catalyst_lab.research_selection_b1 import SelectionRule

    async def no_sleep(_):
        return None

    reviewer = JevReviewer(
        JevStore(jev_url),
        ReliabilityPolicy("SELECTION_REPLAY_FIXTURE", 10, 2, 0.01, 1000, 30),
        key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(scripted_provider({}, B2_QUALITY, [], B2_CYCLE)),
        clock=lambda: now,
        sleep=no_sleep,
    )
    cycle = ResearchCycle(RiskRepository(risk_url), reviewer,
                          CyclePolicy(**FIXTURE_CYCLE_POLICY), clock=lambda: now,
                          selection=SelectionRule(B2_POLICY, FIXTURE_FLOOR))
    cycle.record_selection_rule(runtime_id=str(uuid4()))
    report_id = str(uuid4())
    cycle.start_report(
        {
            "report_id": report_id,
            "generated_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=5)).isoformat(),
            "items": [fixture_item(symbol, now, rationale=True) for symbol in B2_CYCLE],
        },
        max_seconds=300,
    )
    asyncio.run(cycle.tick(report_id))  # SKEPTIC_QUESTIONS_V2, then QUALITY_V2.
    cycle.approved_packets(report_id)
    return report_id


def dump_fixture(path):
    from catalyst_lab import localdb

    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="catalyst-replay-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            localdb.start(root)  # A fresh disposable cluster, never an owner ledger.
            urls = (localdb.connection_url(root, "catalyst_risk"),
                    localdb.connection_url(root, "catalyst_jev"))
            run_fixture_cycles(*urls, now=now)
            run_fixture_b2_cycle(*urls, now=now)
            export = read_export(localdb.connection_url(root, "catalyst_review"))
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
    target = Path(path)
    target.write_text(json.dumps(export, indent=1, sort_keys=True) + "\n")
    target.chmod(0o600)
    return export


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--database-url")
    sources.add_argument("--export", type=Path)
    sources.add_argument("--dump-fixture", type=Path)
    sources.add_argument("--print-export-sql", action="store_true")
    parser.add_argument("--floor", choices=QUALITY_CATEGORIES)
    args = parser.parse_args(argv)
    if args.print_export_sql:
        print(export_sql(BASE_TABLES))
        return 0
    floors = (args.floor,) if args.floor else QUALITY_CATEGORIES
    try:
        if args.database_url:
            report = replay(read_export(args.database_url), source="DATABASE", floors=floors)
        elif args.export:
            report = replay(load_export(args.export), source="EXPORT", floors=floors)
        else:
            report = replay(dump_fixture(args.dump_fixture), source="LAB_FIXTURE_EXPORT",
                            floors=floors)
    except ReplayRefused as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(json.dumps({"error": "REPLAY_DATABASE_ERROR", "sqlstate": exc.sqlstate}),
              file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
