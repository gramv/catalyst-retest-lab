"""Durable Muse -> decision Jev -> evidence follow-up research orchestration.

The execution controller consumes a verified selection, then independently checks
the trigger and risk. This module has no broker client or order/admission method.
"""

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from catalyst_lab import research_selection_b2 as b2_rule
from catalyst_lab import research_selection_topk as topk_rule
from catalyst_lab.agent_identity import agent_cycle_id
from catalyst_lab.jev_contract import (
    INSUFFICIENT,
    JEV_MODEL,
    SKEPTIC,
    SKEPTIC_V2,
    digest,
    encoded,
    strict_json,
    validated_answers,
)
from catalyst_lab.jev_review import ReviewResult, _privacy_check
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.muse_reports import ResearchReportRejected, parse_report
from catalyst_lab.repository import json_safe
from catalyst_lab.research_dossier import (
    DOSSIER_VERSION,
    RATIONALE_BUDGET_BYTES,
    STATE_BUDGET_BYTES,
    DossierRejected,
    compile_review_dossier,
    encoded_bytes,
    review_rationale,
    stored_rationale,
)
from catalyst_lab.research_dossier_v3 import DOSSIER_VERSION_V3, compile_pick_dossier
from catalyst_lab.research_evidence import (
    canonical_sources,
    canonical_technical_facts,
    rationale_reference_errors,
)
from catalyst_lab.research_ranking import (
    QUALITY,
    QUALITY_POLICY,
    QUALITY_V2,
    QUALITY_V2_POLICY,
    QUALITY_V3,
    QUALITY_V3_POLICY,
    meets_quality_floor,
    quality_category,
    quality_score,
)
from catalyst_lab.research_report_v3 import (
    MARKET as V3_MARKET,
)
from catalyst_lab.research_report_v3 import (
    REPORT_SCHEMA_V3,
    REVIEW_VALIDITY,
    TARGET_PICKS,
    ResearchCapabilityUnavailable,
    check_pick,
    is_v3,
    parse_report_v3,
)
from catalyst_lab.research_selection_b1 import (
    ACTIVATION_KIND,
    B1_POLICY,
    B2_POLICY,
    SHADOW_KIND,
    SelectionRule,
    activation_body,
    b1_disposition,
    shadow_body,
    shadow_key,
    started_rule,
)
from catalyst_lab.setup_scan import _evidence_hash
from catalyst_lab.system_check import SUPERSEDED_BY_NEW_RESEARCH, newest_v3_run_slot
from catalyst_lab.technical_evidence import TechnicalEvidence

SELECTION_POLICY = "MUSE_JEV_RESEARCH_SELECTION_V2"
LEGACY_REPORT_ORIGIN = "EXTERNAL_MUSE"  # Unversioned reports: unattributed, never inferred.
AGENT_REPORT_ORIGIN = "EXTERNAL_RESEARCH_AGENT"  # AGENT_RESEARCH_REPORT_V2 with an agent block.


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("AWARE_DEADLINE_REQUIRED")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CyclePolicy:
    selection_limit: int
    review_deadline_seconds: float
    claim_lease_seconds: float
    max_packet_age_seconds: float
    max_inflight: int
    review_validity_seconds: float = 60
    require_technical_evidence: bool = False

    def __post_init__(self):
        if (
            type(self.selection_limit) is not int
            or not 1 <= self.selection_limit <= 10
            or type(self.max_inflight) is not int
            or not 1 <= self.max_inflight <= 30
            or type(self.require_technical_evidence) is not bool
            or any(
                isinstance(v, bool) or not math.isfinite(v) or v <= 0
                for v in (
                    self.review_deadline_seconds,
                    self.claim_lease_seconds,
                    self.max_packet_age_seconds,
                    self.review_validity_seconds,
                )
            )
            or self.claim_lease_seconds <= self.review_deadline_seconds
        ):
            raise ValueError("EXPLICIT_CYCLE_POLICY_REQUIRED")


@dataclass(frozen=True)
class ResearchThesis:
    thesis: str
    disproof: str
    economic_relationship: str

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in asdict(self).values()
        ):
            raise ValueError("SHORT_MARKET_THESIS_REQUIRED")
        _privacy_check(encoded(asdict(self)))


@dataclass(frozen=True)
class TopKReview:
    """A top-K review read back from its receipts: the result as the reviewer would have
    returned it, the answers parsed with exact decimals and the final receipt's audit seq."""

    result: ReviewResult
    decimal_answers: dict | None
    receipt_seq: int | None


def _answer_bearing_result(result):
    return result.status == "RECORDED" or (
        result.status == "NEEDS_REVIEW" and result.reason == "UNCERTAIN_JUDGMENT"
    )


def _uncertain_answers(answers):
    return any(
        answer["choice"] in {INSUFFICIENT, "NEEDS_REVIEW"}
        or sum(
            probability == max(answer["probabilities"].values())
            for probability in answer["probabilities"].values()
        )
        != 1
        for answer in answers.values()
    )


def selection_disposition(result):
    """The owner's existing conjunction, without a confidence or quota override."""
    if not _answer_bearing_result(result) or not result.receipt_ids:
        return "NEEDS_REVIEW", result.reason or "MISSING_VALID_REVIEW"
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
        return "NEEDS_REVIEW", "INVALID_REVIEW"
    choices = {name: answer["choice"] for name, answer in answers.items()}
    if choices["verdict"] == "REJECT":
        return "REJECTED", "JEV_REJECTED"
    if result.status != "RECORDED":
        return "NEEDS_REVIEW", result.reason or "UNRESOLVED_EVIDENCE"
    if _uncertain_answers(answers):
        return "NEEDS_REVIEW", "UNRESOLVED_EVIDENCE"
    if choices == {
        "verdict": "APPROVE",
        "news_stale": "NO",
        "unsupported_inference": "NO",
        "already_priced": "LOW",
    } or choices == {
        "verdict": "APPROVE",
        "news_stale": "NO",
        "unsupported_inference": "NO",
        "already_priced": "MEDIUM",
    }:
        return "APPROVED", "RESEARCH_POLICY_PASSED"
    return "NEEDS_REVIEW", "CONTRADICTORY_OR_UNSUPPORTED_ANSWERS"


def evidence_tasks(answers):
    """Tasks for Muse, not open-ended model commands and never an instruction to buy."""
    choices = {k: v.get("choice") for k, v in answers.items()}
    tasks = []
    if choices.get("news_stale") != "NO" or choices.get("already_priced") not in {"LOW", "MEDIUM"}:
        tasks.append(
            {
                "task": "FETCH_PRIOR_DISCLOSURES",
                "required_fields": [
                    "original_source_excerpt",
                    "prior_disclosure_excerpt",
                    "source_ids",
                    "content_hashes",
                ],
            }
        )
    if choices.get("unsupported_inference") != "NO":
        tasks.append(
            {
                "task": "VERIFY_ECONOMIC_RELATIONSHIP",
                "required_fields": [
                    "economic_relationship",
                    "direct_source_support",
                    "adverse_qualifications",
                ],
            }
        )
    if not tasks:
        tasks.append(
            {
                "task": "RESOLVE_RESEARCH_OBJECTION",
                "required_fields": [
                    "new_original_source_excerpt",
                    "contradictory_evidence",
                    "declared_missing_information",
                ],
            }
        )
    return tasks


class ResearchIntake:
    """Append and read external research; has no provider or execution capability."""

    def __init__(self, repository, policy, *, clock, selection=None):
        self.store = ManagedStore(repository)
        self.repo, self.policy, self.clock = repository, policy, clock
        # The configured rule applies only to cycles this intake starts. A stored cycle is
        # always evaluated under the rule its RESEARCH_STARTED records (_cycle_rule).
        self.selection = SelectionRule() if selection is None else selection
        if not isinstance(self.selection, SelectionRule | topk_rule.TopKRule):
            raise ValueError("EXPLICIT_SELECTION_RULE_REQUIRED")
        self.selection_activation = None

    def record_selection_rule(self, *, runtime_id):
        """Append the audited B1, B2 or top-K activation at startup; V2 records nothing.

        New B1, B2 and top-K cycles reference this event from RESEARCH_STARTED; admission SQL
        refuses such a packet whose cycle does not reference an earlier, intact activation of
        its own rule.
        """
        if topk_rule.is_topk(self.selection):
            body = topk_rule.activation_body(self.selection, runtime_id=runtime_id)
        elif not self.selection.floored:
            return None
        else:
            build = activation_body if self.selection.b1 else b2_rule.activation_body
            body = build(self.selection, runtime_id=runtime_id)
        with self.store.transaction() as conn:
            row = self.store.event(
                conn,
                ACTIVATION_KIND,
                body,
                key=f"research:selection-rule:{runtime_id}",
            )
        self.selection_activation = row
        return row

    def _started_rule_fields(self, *, v3=False):
        """RESEARCH_STARTED fields for a new cycle; a V2 body is exactly as before.

        Top-K selects among AGENT_RESEARCH_REPORT_V3 picks only: under it a V2, legacy or
        scanner intake is refused (REPORT_V3_REQUIRED) before anything is stored.
        """
        if topk_rule.is_topk(self.selection):
            if not v3:
                # A whole-report refusal: the HTTP layer answers 422 with this code.
                raise ResearchReportRejected(topk_rule.REPORT_V3_REQUIRED)
            if self.selection_activation is None:
                raise ValueError("SELECTION_RULE_ACTIVATION_REQUIRED")
            return {
                "selection_policy": self.selection.policy,
                "selection_rule": topk_rule.started_rule(
                    self.selection, self.selection_activation
                ),
            }
        if not self.selection.floored:
            return {"selection_policy": SELECTION_POLICY}
        if self.selection_activation is None:
            raise ValueError("SELECTION_RULE_ACTIVATION_REQUIRED")
        if self.selection.b2:
            return {
                "selection_policy": B2_POLICY,
                "selection_rule": b2_rule.started_rule(self.selection, self.selection_activation),
            }
        return {
            "selection_policy": B1_POLICY,
            "selection_rule": started_rule(self.selection, self.selection_activation),
        }

    @staticmethod
    def _cycle_rule(started):
        """The rule a cycle started under; it never changes during the cycle's life."""
        if started.get("selection_policy") in topk_rule.TOPK_POLICIES:
            return topk_rule.stored_rule(started.get("selection_rule"))
        if started.get("selection_policy") == B2_POLICY:
            return b2_rule.stored_rule(started.get("selection_rule"))
        if started.get("selection_policy") != B1_POLICY:
            return SelectionRule()  # V2 and every earlier cycle keep the V2 path.
        stored = started.get("selection_rule")
        try:
            if (
                not isinstance(stored, dict)
                or stored.get("selection_policy") != B1_POLICY
                or stored.get("quality_policy") != QUALITY_V2_POLICY
            ):
                raise ValueError
            return SelectionRule(B1_POLICY, stored.get("quality_floor"))
        except ValueError:
            raise ValueError("SELECTION_RULE_UNAVAILABLE") from None

    def _rows(self, conn, cycle_id):
        return conn.execute(
            """SELECT * FROM lab.managed_events WHERE body->>'cycle_id'=%s
            AND kind LIKE 'RESEARCH_%%' ORDER BY event_seq""",
            (str(cycle_id),),
        ).fetchall()

    def _event(self, conn, cycle_id, kind, body, key):
        return self.store.event(
            conn, "RESEARCH_" + kind, {"cycle_id": str(cycle_id), **body}, key=key
        )

    def _cycle(self, rows):
        started = next((r["body"] for r in rows if r["kind"] == "RESEARCH_STARTED"), None)
        if started is None:
            raise ValueError("RESEARCH_CYCLE_MISSING")
        if not isinstance(started.get("policy"), dict):
            raise ValueError("RESEARCH_POLICY_UNAVAILABLE")
        stored_policy = {**started["policy"]}
        stored_policy.setdefault("review_validity_seconds", 60)
        stored_policy.setdefault("require_technical_evidence", False)
        if stored_policy != asdict(self.policy):
            raise ValueError("RESEARCH_POLICY_CHANGED")
        return started

    def _latest(self, rows):
        packets = {}
        for row in rows:
            if row["kind"] == "RESEARCH_PACKET":
                packets[row["body"]["item_key"]] = row["body"]
        return packets

    def _sources(self, sources, now):
        rows = json_safe(canonical_sources(sources, now=now))
        _privacy_check(encoded(rows))
        return rows

    def _packet(self, cycle_id, item_key, decision, asset, thesis, expires_at, now):
        if _evidence_hash(asset) != decision.evidence_hash:
            raise ValueError("SCANNER_ASSET_BINDING_MISMATCH")
        levels = {
            k: str(getattr(decision.geometry, k))
            for k in ("entry_trigger", "max_entry_price", "stop", "target")
        }
        state = {
            "market": decision.market,
            "symbol": decision.symbol,
            **asdict(thesis),
            "levels": levels,
            "sources": self._sources(asset.news, now),
            "technical_context": {
                "scan_policy": decision.policy_id,
                "observed_level_provenance": {
                    k: getattr(decision.geometry, k)
                    for k in ("trigger_bar_id", "stop_bar_id", "target_bar_id")
                },
                "code_computed_metrics": json_safe(decision.metrics),
                "all_technical_eligibility_checks_passed": True,
            },
        }
        _privacy_check(encoded(state))
        packet = {
            "cycle_id": str(cycle_id),
            "item_key": item_key,
            "asset_id": decision.asset_id,
            "market": decision.market,
            "symbol": decision.symbol,
            "revision": 1,
            "rank": decision.rank,
            "scan_evidence_hash": decision.evidence_hash,
            "selection_policy": self.selection.policy,
            "execution_scope": decision.execution_scope,
            "levels": levels,
            "state": state,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        packet["evidence_hash"] = digest(encoded(state))
        packet["source_content_hash"] = self._source_content_hash(state["sources"])
        return packet

    @staticmethod
    def _source_content_hash(sources):
        # Retrieval times, labels, question phrasing and IDs alone are not new evidence.
        return digest(encoded(sorted((s["url"], s["content_hash"]) for s in sources)))

    def start_report(self, raw, *, max_seconds, v3=None):
        """Receive an agent's ordered shortlist without scanning or asserting eligibility.

        Report levels and technical analysis are research proposals, not trusted
        market observations. Existing admission/trigger/risk gates validate them.
        Each item is judged on its own: a failing item is recorded as
        RESEARCH_ITEM_REJECTED_AT_INTAKE and its siblings proceed. The whole report is
        refused with nothing stored for an invalid envelope, credential-like content,
        staleness or expiry, or when no item is acceptable. Each accepted item's review
        state is its REVIEW_DOSSIER_V1, whose manifest is appended as RESEARCH_DOSSIER.

        A V2 body's agent block is recorded in RESEARCH_STARTED and every RESEARCH_PACKET
        body (outside ``state``) and its cycle ID is derived from agent and report ID, so
        two agents cannot collide on a report ID. Legacy bodies keep ``cycle_id =
        report_id`` and record ``agent: null`` (LEGACY_UNATTRIBUTED). The HTTP layer has
        already bound the agent ID to the submitting credential.

        An ``AGENT_RESEARCH_REPORT_V3`` body goes to ``_start_report_v3``, which needs ``v3``
        (the schedule and the tradable universe); V2 and legacy bodies never read ``v3``.
        """
        if is_v3(raw):
            return self._start_report_v3(raw, max_seconds=max_seconds, v3=v3)
        intake = parse_report(raw)
        if type(max_seconds) is not int or not 1 <= max_seconds <= 86400:
            raise ValueError("EXPLICIT_REPORT_DEADLINE_REQUIRED")
        now, generated = _utc(self.clock()), _utc(intake.generated_at)
        agent = intake.agent
        if agent is None:
            cycle_id, origin = intake.report_id, LEGACY_REPORT_ORIGIN
        else:
            cycle_id = agent_cycle_id(agent["agent_id"], intake.report_id)
            origin = AGENT_REPORT_ORIGIN
        with self.store.transaction() as conn:
            rows = self._rows(conn, cycle_id)
            prior = next((r["body"] for r in rows if r["kind"] == "RESEARCH_STARTED"), None)
            if prior:
                if prior.get("report_hash") != intake.report_hash:
                    raise ValueError("REPORT_IDEMPOTENCY_CONTENT_MISMATCH")
                return self._report_response(cycle_id, prior, replay=True)
            rule_fields = self._started_rule_fields()  # B1 without activation stores nothing.
            if not 0 <= (now - generated).total_seconds() <= self.policy.max_packet_age_seconds:
                raise ValueError("RESEARCH_REPORT_STALE_OR_FUTURE")
            expires = min(_utc(intake.valid_until), generated + timedelta(seconds=max_seconds))
            if expires <= now:
                raise ValueError("RESEARCH_REPORT_EXPIRED")
            outcomes, results = [], []
            for item in intake.items:
                dossier = packet = None
                if (
                    item.code is None
                    and item.contender.market == "CRYPTO"
                    and self._off_recorded_crypto_grid(conn, item.contender)
                ):
                    code = "CRYPTO_LEVEL_OFF_PRICE_GRID"
                    item = item.rejected(code, [{"path": item.path + ".levels", "code": code}])
                if item.code is None:
                    try:
                        dossier = compile_review_dossier(
                            item,
                            now=now,
                            require_technical_evidence=self.policy.require_technical_evidence,
                        )
                    except DossierRejected as exc:
                        item = item.rejected(exc.code, exc.errors)
                    except ValueError as exc:
                        if str(exc) != "SENSITIVE_EVIDENCE_REJECTED":
                            raise
                        raise ResearchReportRejected(
                            "SENSITIVE_EVIDENCE_REJECTED",
                            errors=[{"path": item.path, "code": "SENSITIVE_EVIDENCE_REJECTED"}],
                        ) from None
                if item.code is not None:
                    outcomes.append((item, None, None))
                    results.append(
                        {
                            "index": item.index,
                            "signal_id": item.signal_id,
                            "status": "REJECTED",
                            "code": item.code,
                            "errors": list(item.errors),
                        }
                    )
                    continue
                contender, data = item.contender, item.canonical
                packet = {
                    "cycle_id": cycle_id,
                    "item_key": contender.market + ":" + contender.symbol,
                    "asset_id": contender.symbol,
                    "signal_id": contender.signal_id,
                    "market": dossier.state["market"],
                    "symbol": contender.symbol,
                    "revision": 1,
                    "rank": item.index + 1,
                    "research_origin": origin,
                    # Attribution only: kept outside ``state``, so never reviewed.
                    "agent": agent,
                    "selection_policy": rule_fields["selection_policy"],
                    "execution_scope": "RESEARCH_ONLY"
                    if contender.market == "INDIA"
                    else "PAPER_ONLY",
                    "levels": data["levels"],
                    "state": dossier.state,
                    "created_at": generated.isoformat(),
                    "received_at": now.isoformat(),
                    "expires_at": expires.isoformat(),
                    "evidence_hash": dossier.evidence_hash,
                    "source_content_hash": self._source_content_hash(dossier.state["sources"]),
                    # Full block incl. agent_confidence for analytics; the reviewed state
                    # carries it without the confidence. Legacy items store null.
                    "selection_rationale": stored_rationale(data.get("selection_rationale")),
                }
                outcomes.append((item, dossier, packet))
                results.append(
                    {
                        "index": item.index,
                        "signal_id": item.signal_id,
                        "status": "ACCEPTED",
                        "item_key": packet["item_key"],
                        "revision": 1,
                        "evidence_hash": packet["evidence_hash"],
                        "dossier_bytes": dossier.manifest["state_bytes"],
                    }
                )
            accepted = [packet for _, _, packet in outcomes if packet is not None]
            if not accepted:
                code = results[0]["code"]
                raise ResearchReportRejected(
                    "INVALID_MUSE_REPORT" if code == "INVALID_RESEARCH_ITEM" else code,
                    item_results=results,
                )
            body = {
                "expires_at": expires.isoformat(),
                "policy": asdict(self.policy),
                **rule_fields,
                "contender_count": len(accepted),
                "submitted_count": len(results),
                "rejected_count": len(results) - len(accepted),
                "target_minimum_met": len(accepted) >= 20,
                "purpose": "ENGINEERING_TEST",
                "research_origin": origin,
                "agent": agent,
                "report_schema_version": intake.schema_version,
                "dossier_version": DOSSIER_VERSION,
                "report_hash": intake.report_hash,
                "report": intake.canonical,
                "item_results": results,
            }
            self._event(conn, cycle_id, "STARTED", body, "research:" + cycle_id + ":start")
            for item, dossier, packet in outcomes:
                if packet is None:
                    self._event(
                        conn,
                        cycle_id,
                        "ITEM_REJECTED_AT_INTAKE",
                        {
                            "index": item.index,
                            "signal_id": item.signal_id,
                            "code": item.code,
                            "errors": list(item.errors),
                            "item_sha256": item.item_sha256,
                        },
                        f"research:{cycle_id}:item:{item.index}:rejected",
                    )
                    continue
                key = packet["item_key"]
                self._event(conn, cycle_id, "PACKET", packet, f"research:{cycle_id}:{key}:1:packet")
                self._event(
                    conn,
                    cycle_id,
                    "DOSSIER",
                    {
                        "item_key": key,
                        "revision": 1,
                        "index": item.index,
                        "signal_id": item.signal_id,
                        "evidence_hash": packet["evidence_hash"],
                        "report_hash": intake.report_hash,
                        "manifest": dossier.manifest,
                    },
                    f"research:{cycle_id}:{key}:1:dossier",
                )
        return self._report_response(cycle_id, body, replay=False)

    def _start_report_v3(self, raw, *, max_seconds, v3):
        """Receive an ``AGENT_RESEARCH_REPORT_V3`` (research_report_v3): format checks only.

        The envelope is checked as a whole (identity, times, the scheduled run it answers and
        its validity); each pick on its own, in order: schema, positive prices, symbol in the
        current tradable universe, sources or bars its kind needs, citations, validity and
        price time, then its REVIEW_DOSSIER_V3 (sources, bars, no agent identity, budget). A
        refused pick is recorded as RESEARCH_ITEM_REJECTED_AT_INTAKE and its siblings proceed.
        There is no geometry, reward-to-risk, stop-distance, price-grid or live-price check:
        the independent system check comes after Jev's selection.

        Every accepted pick becomes a packet that expires at its own ``valid_until`` (else the
        report's), bounded by ``max_seconds`` after generation, and its review stays valid
        until that expiry (``review_validity: PACKET_EXPIRY``). The cycle runs under the
        configured selection rule, exactly as a V2 cycle. The universe is read (cached, at most
        one broker read an hour) before the ledger transaction; an exact retry of a recorded
        report returns the stored response without reading it.
        """
        if v3 is None:
            raise ResearchCapabilityUnavailable("RESEARCH_V3_INTAKE_NOT_CONFIGURED")
        intake = parse_report_v3(raw, schedule=v3.schedule)
        if type(max_seconds) is not int or not 1 <= max_seconds <= 86400:
            raise ValueError("EXPLICIT_REPORT_DEADLINE_REQUIRED")
        agent = intake.agent
        cycle_id = agent_cycle_id(agent["agent_id"], intake.report_id)
        with self.repo.connect() as conn:
            prior = self._started(self._rows(conn, cycle_id))
        if prior:
            return self._replay(cycle_id, prior, intake)
        universe = v3.snapshot()
        now, generated = _utc(self.clock()), _utc(intake.generated_at)
        with self.store.transaction() as conn:
            prior = self._started(self._rows(conn, cycle_id))
            if prior:
                return self._replay(cycle_id, prior, intake)
            # B1, B2 and top-K without their activation store nothing.
            rule_fields = self._started_rule_fields(v3=True)
            if not 0 <= (now - generated).total_seconds() <= self.policy.max_packet_age_seconds:
                raise ValueError("RESEARCH_REPORT_STALE_OR_FUTURE")
            expires = min(_utc(intake.valid_until), generated + timedelta(seconds=max_seconds))
            if expires <= now:
                raise ValueError("RESEARCH_REPORT_EXPIRED")
            outcomes, results = [], []
            for item in intake.picks:
                item, pick_expiry = check_pick(
                    item, intake=intake, universe=universe, expires_at=expires, now=now
                )
                dossier = None
                if item.code is None:
                    try:
                        dossier = compile_pick_dossier(
                            item, now=now, agent_id=agent["agent_id"],
                            valid_until=item.canonical["valid_until"]
                            or intake.canonical["valid_until"],
                        )
                    except DossierRejected as exc:
                        item = item.rejected(exc.code, exc.errors)
                    except ValueError as exc:
                        if str(exc) != "SENSITIVE_EVIDENCE_REJECTED":
                            raise
                        raise ResearchReportRejected(
                            "SENSITIVE_EVIDENCE_REJECTED",
                            errors=[{"path": item.path, "code": "SENSITIVE_EVIDENCE_REJECTED"}],
                        ) from None
                if item.code is not None:
                    outcomes.append((item, None, None))
                    results.append({
                        "index": item.index, "signal_id": item.signal_id, "status": "REJECTED",
                        "code": item.code, "errors": list(item.errors),
                    })
                    continue
                pick, data = item.pick, item.canonical
                packet = {
                    "cycle_id": cycle_id,
                    "item_key": V3_MARKET + ":" + pick.symbol,
                    "asset_id": pick.symbol,
                    "signal_id": pick.signal_id,
                    "market": V3_MARKET,
                    "symbol": pick.symbol,
                    "revision": 1,
                    "rank": item.index + 1,
                    "research_origin": AGENT_REPORT_ORIGIN,
                    # Attribution only: kept outside ``state``, so never reviewed.
                    "agent": agent,
                    "selection_policy": rule_fields["selection_policy"],
                    "execution_scope": "PAPER_ONLY",
                    "levels": data["levels"],
                    "state": dossier.state,
                    "created_at": generated.isoformat(),
                    "received_at": now.isoformat(),
                    "expires_at": _utc(pick_expiry).isoformat(),
                    "evidence_hash": dossier.evidence_hash,
                    "source_content_hash": self._source_content_hash(dossier.state["sources"]),
                    # Full block incl. its agent_confidence for analytics; the reviewed state
                    # carries it without the confidence.
                    "selection_rationale": stored_rationale(data["selection_rationale"]),
                    # Report V3, outside the reviewed state: the review lasts until expiry.
                    "report_schema_version": REPORT_SCHEMA_V3,
                    "dossier_version": DOSSIER_VERSION_V3,
                    "review_validity": REVIEW_VALIDITY,
                    "run_slot": intake.canonical["run_slot"],
                    "agent_confidence": data["agent_confidence"],  # Analytics only.
                }
                outcomes.append((item, dossier, packet))
                results.append({
                    "index": item.index, "signal_id": item.signal_id, "status": "ACCEPTED",
                    "item_key": packet["item_key"], "revision": 1,
                    "evidence_hash": packet["evidence_hash"],
                    "dossier_bytes": dossier.manifest["state_bytes"],
                    "expires_at": packet["expires_at"],
                })
            accepted = [packet for _, _, packet in outcomes if packet is not None]
            if not accepted:
                code = results[0]["code"]
                raise ResearchReportRejected(
                    "INVALID_MUSE_REPORT" if code == "INVALID_RESEARCH_ITEM" else code,
                    item_results=results,
                )
            body = {
                "expires_at": expires.isoformat(),
                "policy": asdict(self.policy),
                **rule_fields,
                "contender_count": len(accepted),
                "submitted_count": len(results),
                "rejected_count": len(results) - len(accepted),
                "target_minimum_met": len(accepted) >= TARGET_PICKS,
                "purpose": "ENGINEERING_TEST",
                "research_origin": AGENT_REPORT_ORIGIN,
                "agent": agent,
                "report_schema_version": REPORT_SCHEMA_V3,
                "dossier_version": DOSSIER_VERSION_V3,
                "review_validity": REVIEW_VALIDITY,
                "report_hash": intake.report_hash,
                "report": intake.canonical,
                "item_results": results,
                "run_slot": intake.canonical["run_slot"],
                "context_as_of": intake.canonical["context_as_of"],
                "skipped_count": len(intake.skipped),
                "research_schedule": intake.schedule,
                # The tradable universe the picks were checked against.
                "universe": {
                    "source": universe.source,
                    "fetched_at": _utc(universe.fetched_at).isoformat(),
                    "symbols": sorted(universe.symbols),
                },
            }
            self._event(conn, cycle_id, "STARTED", body, "research:" + cycle_id + ":start")
            for item, dossier, packet in outcomes:
                if packet is None:
                    self._event(
                        conn, cycle_id, "ITEM_REJECTED_AT_INTAKE",
                        {
                            "index": item.index, "signal_id": item.signal_id, "code": item.code,
                            "errors": list(item.errors), "item_sha256": item.item_sha256,
                        },
                        f"research:{cycle_id}:item:{item.index}:rejected",
                    )
                    continue
                key = packet["item_key"]
                self._event(conn, cycle_id, "PACKET", packet, f"research:{cycle_id}:{key}:1:packet")
                self._event(
                    conn, cycle_id, "DOSSIER",
                    {
                        "item_key": key, "revision": 1, "index": item.index,
                        "signal_id": item.signal_id, "evidence_hash": packet["evidence_hash"],
                        "report_hash": intake.report_hash, "manifest": dossier.manifest,
                    },
                    f"research:{cycle_id}:{key}:1:dossier",
                )
        return self._report_response(cycle_id, body, replay=False)

    @staticmethod
    def _started(rows):
        return next((r["body"] for r in rows if r["kind"] == "RESEARCH_STARTED"), None)

    def _replay(self, cycle_id, prior, intake):
        """An exact retry returns the stored response; changed content is refused."""
        if prior.get("report_hash") != intake.report_hash:
            raise ValueError("REPORT_IDEMPOTENCY_CONTENT_MISMATCH")
        return self._report_response(cycle_id, prior, replay=True)

    @staticmethod
    def _off_recorded_crypto_grid(conn, item):
        """Early refusal against broker price metadata that admission already recorded.

        Only a record from the last day counts. Without one the item is accepted and
        execution admission performs the live broker check.
        """
        row = conn.execute(
            """SELECT body->>'price_increment' AS increment FROM lab.managed_events
            WHERE setup_id IS NULL AND kind='CRYPTO_ASSET_METADATA'
              AND upper(replace(body->>'symbol','/',''))=upper(replace(%s,'/',''))
              AND recorded_at>clock_timestamp()-interval '1 day'
            ORDER BY event_seq DESC LIMIT 1""",
            (item.symbol,),
        ).fetchone()
        try:
            increment = Decimal(row["increment"]) if row else None
            if increment is None or not increment.is_finite() or increment <= 0:
                return False
            levels = item.levels
            return any(
                getattr(levels, name) % increment
                for name in ("entry_trigger", "max_entry_price", "stop", "target")
            )
        except (ArithmeticError, TypeError, ValueError):
            return False

    @staticmethod
    def _report_response(cycle_id, body, *, replay):
        agent = body.get("agent")  # Absent in reports recorded before plan 1.3: legacy.
        response = {
            "status": "MUSE_REPORT_RECORDED",
            "cycle_id": cycle_id,
            "contender_count": body["contender_count"],
            "expires_at": body["expires_at"],
            "idempotent_replay": replay,
            "research_origin": body.get("research_origin", LEGACY_REPORT_ORIGIN),
            "agent_id": agent["agent_id"] if agent else None,
            "agent_version": agent["agent_version"] if agent else None,
            "polling_url": f"/api/v1/lab/cycles/{cycle_id}/outputs",
            "trade_authorized": False,
        }
        if "item_results" in body:  # Reports recorded before per-item intake have none.
            response["submitted_count"] = body["submitted_count"]
            response["rejected_count"] = body["rejected_count"]
            response["item_results"] = body["item_results"]
        if body.get("report_schema_version") == REPORT_SCHEMA_V3:  # V2 responses unchanged.
            response["report_schema_version"] = REPORT_SCHEMA_V3
            response["run_slot"] = body["run_slot"]
            response["skipped_count"] = body["skipped_count"]
            response["review_validity"] = body["review_validity"]
        return response

    def start(self, cycle_id, batch, assets, theses, *, expires_at):
        """Legacy offline scanner fixture importer; never wired to the application API."""
        cycle_id, now, expires_at = str(UUID(str(cycle_id))), _utc(self.clock()), _utc(expires_at)
        if not now < expires_at or not 0 <= (now - _utc(batch.generated_at)).total_seconds() <= (
            self.policy.max_packet_age_seconds
        ):
            raise ValueError("RESEARCH_CYCLE_EXPIRED")
        if len(batch.contenders) > 30 or any(
            d.disposition != "CONTENDER" or d.geometry is None or d.rank is None
            for d in batch.contenders
        ):
            raise ValueError("SCANNER_CONTENDERS_REQUIRED")
        mapping = {(a.market, a.asset_id): a for a in assets}
        rule_fields = self._started_rule_fields()
        packets = []
        for decision in batch.contenders:
            key = decision.market + ":" + decision.asset_id
            asset = mapping.get((decision.market, decision.asset_id))
            if asset is None or key not in theses:
                raise ValueError("CONTENDER_EVIDENCE_AND_THESIS_REQUIRED")
            packets.append(
                self._packet(
                    cycle_id,
                    key,
                    decision,
                    asset,
                    theses[key],
                    expires_at,
                    now,
                )
            )
        with self.store.transaction() as conn:
            self._event(
                conn,
                cycle_id,
                "STARTED",
                {
                    "expires_at": expires_at.isoformat(),
                    "policy": asdict(self.policy),
                    **rule_fields,
                    "scan_policy": batch.policy_id,
                    "contender_count": len(packets),
                    "target_minimum_met": batch.target_minimum_met,
                    "scan": batch.to_dict(),
                    "purpose": "ENGINEERING_TEST",
                },
                "research:" + cycle_id + ":start",
            )
            for packet in packets:
                self._event(
                    conn,
                    cycle_id,
                    "PACKET",
                    packet,
                    f"research:{cycle_id}:{packet['item_key']}:1:packet",
                )
        return cycle_id

    def submit_evidence(
        self,
        cycle_id,
        item_key,
        *,
        revision,
        sources,
        thesis,
        task_id=None,
        technical_facts=None,
        selection_rationale=None,
    ):
        """Append an evidence revision; ``selection_rationale`` is accepted for B2 items only.

        Every policy requires a material revision. For V2 and B1 that is new source content,
        exactly as before. A B2 item's revision is material with new source content or with
        rationale claims and citations that no earlier revision of the item had, so a
        RECITE_FACTUAL_CLAIMS task can be answered by re-citing or trimming claims alone.
        """
        now = _utc(self.clock())
        with self.store.transaction() as conn:
            rows = self._rows(conn, cycle_id)
            cycle = self._cycle(rows)
            old = self._latest(rows).get(item_key)
            if old is not None and old.get("selection_policy") in topk_rule.TOPK_POLICIES:
                # Top-K reviews each pick once and ranks the run: no evidence-task loop.
                raise ValueError(topk_rule.EVIDENCE_REVISION_NOT_APPLICABLE)
            b2_item = old is not None and old.get("selection_policy") == B2_POLICY
            if selection_rationale is not None and old is not None and not b2_item:
                # V2 and B1 keep their revision contract: sources, thesis and facts only.
                raise ValueError("RATIONALE_REVISION_NOT_APPLICABLE")
            if old is not None and task_id is not None and revision == old["revision"]:
                resolved = next(
                    (
                        r["body"]
                        for r in rows
                        if r["kind"] == "RESEARCH_EVIDENCE_RESOLVED"
                        and r["body"].get("task_id") == str(task_id)
                        and r["body"].get("resolved_by_revision") == revision
                    ),
                    None,
                )
                if resolved is not None:
                    retry_sources = self._sources(sources, now)
                    retry_facts = canonical_technical_facts(
                        technical_facts,
                        now=now,
                        allowed_source_ids=[source["source_id"] for source in retry_sources],
                    )
                    if b2_item:
                        # The B2 revision is idempotent: re-applied to the revision it
                        # created, the same content gives the same state and rationale.
                        retry_state, retry_block = self._b2_revision(
                            rows, old, retry_sources, thesis, retry_facts, selection_rationale
                        )
                        if retry_state != old["state"] or (
                            selection_rationale is not None
                            and retry_block != old.get("selection_rationale")
                        ):
                            raise ValueError("EVIDENCE_TASK_IDEMPOTENCY_CONTENT_MISMATCH")
                        return old
                    retry_state = {**old["state"], **asdict(thesis), "sources": retry_sources}
                    if retry_facts is not None:
                        retry_state["technical_context"] = {
                            **retry_state.get("technical_context", {}),
                            "muse_verified_facts": retry_facts,
                        }
                    if retry_state != old["state"]:
                        raise ValueError("EVIDENCE_TASK_IDEMPOTENCY_CONTENT_MISMATCH")
                    return old
            if old is None or revision != old["revision"] + 1:
                raise ValueError("CURRENT_EVIDENCE_REVISION_REQUIRED")
            if now >= datetime.fromisoformat(cycle["expires_at"]):
                raise ValueError("ORIGINAL_CYCLE_DEADLINE_PASSED")
            source_rows = self._sources(sources, now)
            task = None
            if task_id is not None:
                task = next(
                    (
                        r["body"]
                        for r in rows
                        if r["kind"] == "RESEARCH_EVIDENCE_TASK"
                        and r["body"].get("task_id") == str(task_id)
                    ),
                    None,
                )
                if (
                    task is None
                    or task["item_key"] != item_key
                    or task["revision"] != old["revision"]
                    or now >= datetime.fromisoformat(task["expires_at"])
                    or any(
                        r["kind"] == "RESEARCH_EVIDENCE_RESOLVED"
                        and r["body"].get("task_id") == str(task_id)
                        for r in rows
                    )
                ):
                    raise ValueError("CURRENT_EVIDENCE_TASK_REQUIRED")
            source_hash = self._source_content_hash(source_rows)
            if b2_item:
                facts = canonical_technical_facts(
                    technical_facts,
                    now=now,
                    allowed_source_ids=[source["source_id"] for source in source_rows],
                )
                state, block = self._b2_revision(
                    rows, old, source_rows, thesis, facts, selection_rationale
                )
                earlier = [
                    r["body"] for r in rows
                    if r["kind"] == "RESEARCH_PACKET" and r["body"]["item_key"] == item_key
                ]
                claims = b2_rule.claims_fingerprint(state.get("rationale"))
                if any(p["source_content_hash"] == source_hash for p in earlier) and any(
                    b2_rule.claims_fingerprint(p["state"].get("rationale")) == claims
                    for p in earlier
                ):
                    # Neither new source content nor new rationale claims or citations.
                    raise ValueError("MATERIAL_NEW_EVIDENCE_REQUIRED")
            else:
                if any(
                    r["kind"] == "RESEARCH_PACKET"
                    and r["body"]["item_key"] == item_key
                    and r["body"]["source_content_hash"] == source_hash
                    for r in rows
                ):
                    raise ValueError("MATERIAL_NEW_SOURCE_EVIDENCE_REQUIRED")
                facts = canonical_technical_facts(
                    technical_facts,
                    now=now,
                    allowed_source_ids=[source["source_id"] for source in source_rows],
                )
                state = self._revised_state(old, source_rows, thesis, facts)
            _privacy_check(encoded(state))
            packet = {
                **old,
                "state": state,
                "revision": revision,
                "created_at": now.isoformat(),
                "evidence_hash": digest(encoded(state)),
                "source_content_hash": source_hash,
            }
            if b2_item:
                packet["selection_rationale"] = block
            self._event(
                conn,
                cycle_id,
                "SUPERSEDED",
                {
                    "item_key": item_key,
                    "revision": old["revision"],
                    "new_revision": revision,
                    "reason": "MATERIAL_NEW_EVIDENCE",
                    "previous_evidence_hash": old["evidence_hash"],
                },
                f"research:{cycle_id}:{item_key}:{revision}:superseded",
            )
            self._event(
                conn,
                cycle_id,
                "PACKET",
                packet,
                f"research:{cycle_id}:{item_key}:{revision}:packet",
            )
            if task is not None:
                self._event(
                    conn,
                    cycle_id,
                    "EVIDENCE_RESOLVED",
                    {
                        "task_id": str(task_id),
                        "item_key": item_key,
                        "revision": task["revision"],
                        "resolved_by_revision": revision,
                    },
                    f"research:task:{task_id}:resolved",
                )
        return packet

    @staticmethod
    def _revised_state(old, source_rows, thesis, facts):
        """``old``'s state with a revision's sources, thesis and follow-up facts applied."""
        state = {**old["state"], **asdict(thesis), "sources": source_rows}
        if facts is not None:
            technical_context = {**state.get("technical_context", {})}
            if "observed_facts" in technical_context:
                technical_context.pop("observed_facts")
                technical_context["observed_facts_status"] = "SUPERSEDED_BY_FOLLOWUP"
            state["technical_context"] = {
                **technical_context,
                "muse_verified_facts": facts,
            }
        elif "observed_facts" in state.get("technical_context", {}):
            state["technical_context"] = {
                **state["technical_context"],
                "observed_facts_status": "OBSERVATIONS_NOT_REFRESHED",
            }
        return state

    def _b2_revision(self, rows, old, source_rows, thesis, facts, rationale):
        """A B2 revision's state and stored rationale; the same inputs give the same result.

        Sources, thesis and follow-up facts apply exactly as for every policy. A supplied
        rationale replaces the reviewed one (the stored block keeps its analytics-only
        confidence); the dossier then carries the bars the levels and the new claims cite,
        taken from the report's own technical evidence. The effective rationale, new or
        carried, may cite only this revision's sources and the bars the state shows, and the
        state keeps the dossier budgets, since B2 sends every approval to QUALITY_V2 too.
        """
        state = self._revised_state(old, source_rows, thesis, facts)
        block = old.get("selection_rationale")
        if rationale is not None:
            canonical = b2_rule.canonical_rationale(rationale)
            block = stored_rationale(canonical)
            state["rationale"] = review_rationale(canonical)
            observed = state.get("technical_context", {}).get("observed_facts")
            if observed is not None:
                state["technical_context"] = {
                    **state["technical_context"],
                    "observed_facts": self._cited_observations(rows, old, observed, canonical),
                }
        reviewed = state.get("rationale")
        if reviewed is not None:
            observed = state.get("technical_context", {}).get("observed_facts") or {}
            errors = rationale_reference_errors(
                reviewed,
                source_ids=[source["source_id"] for source in source_rows],
                bar_ids=[
                    bar["bar_id"] for bar in (observed.get("observations") or {}).get("bars", ())
                ],
                prefix="selection_rationale",
            )
            if errors:
                raise ValueError(errors[0]["code"])
            if encoded_bytes(reviewed) > RATIONALE_BUDGET_BYTES:
                raise ValueError("DOSSIER_OVER_BUDGET")
        if encoded_bytes(state) > STATE_BUDGET_BYTES:
            raise ValueError("DOSSIER_OVER_BUDGET")
        return state, block

    @staticmethod
    def _cited_observations(rows, old, observed, rationale):
        """The observed facts with exactly the bars the levels and ``rationale`` cite.

        Bars come from the item's technical evidence in the stored report
        (``RESEARCH_STARTED.report``), re-validated, so a newly cited bar reaches the reviewer
        byte for byte as intake would have sent it. Metrics, hashes and every other field
        are unchanged; without stored evidence only the bars already shown can be cited.
        """
        started = next((r["body"] for r in rows if r["kind"] == "RESEARCH_STARTED"), {})
        item = next(
            (
                entry
                for entry in (started.get("report") or {}).get("items") or ()
                if isinstance(entry, dict)
                and old.get("signal_id") is not None
                and entry.get("signal_id") == old.get("signal_id")
                and entry.get("symbol") == old.get("symbol")
            ),
            None,
        )
        if item is None or item.get("technical_evidence") is None:
            return observed
        evidence = TechnicalEvidence.model_validate(item["technical_evidence"])
        bars = json_safe(evidence.model_dump(mode="json"))["bars"]
        cited = {ref.bar_id for ref in evidence.level_references.values()} | {
            reference
            for claim in rationale["claims"]
            for reference in claim["supported_by"]["bar_ids"]
        }
        return {
            **observed,
            "observations": {
                **observed["observations"],
                "bars": [bar for bar in bars if bar["bar_id"] in cited],
            },
        }

    def claim_evidence_tasks(self, cycle_id, claimant, *, lease_seconds=30, limit=10):
        now = _utc(self.clock())
        if not isinstance(claimant, str) or not claimant or not 1 <= lease_seconds <= 300:
            raise ValueError("VALID_EVIDENCE_TASK_CLAIM_REQUIRED")
        claimed = []
        with self.store.transaction() as conn:
            rows = self._rows(conn, cycle_id)
            self._cycle(rows)
            tasks = [r["body"] for r in rows if r["kind"] == "RESEARCH_EVIDENCE_TASK"]
            for task in tasks:
                if len(claimed) >= min(30, max(1, limit)):
                    break
                if now >= datetime.fromisoformat(task["expires_at"]):
                    continue
                if any(
                    r["kind"] == "RESEARCH_EVIDENCE_RESOLVED"
                    and r["body"].get("task_id") == task["task_id"]
                    for r in rows
                ):
                    continue
                leases = [
                    r["body"]
                    for r in rows
                    if r["kind"] == "RESEARCH_EVIDENCE_CLAIM"
                    and r["body"].get("task_id") == task["task_id"]
                ]
                if leases and datetime.fromisoformat(leases[-1]["lease_until"]) > now:
                    continue
                body = {
                    "task_id": task["task_id"],
                    "claimant": claimant,
                    "lease_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
                }
                self._event(
                    conn, cycle_id, "EVIDENCE_CLAIM", body, "research:task-claim:" + str(uuid4())
                )
                claimed.append(task)
        return claimed

    def outputs(self, cycle_id, *, after=0, limit=100):
        with self.repo.connect() as conn:
            return conn.execute(
                """SELECT event_seq,kind,body,recorded_at FROM lab.managed_events
                WHERE body->>'cycle_id'=%s AND kind LIKE 'RESEARCH_%%' AND event_seq>%s
                ORDER BY event_seq LIMIT %s""",
                (str(cycle_id), after, min(1000, max(1, limit))),
            ).fetchall()


class ResearchCycle(ResearchIntake):
    """The independently running app worker evaluates the durable research queue."""

    def __init__(self, repository, reviewer, policy, *, clock, selection=None):
        super().__init__(repository, policy, clock=clock, selection=selection)
        self.reviewer = reviewer
        repository.require_same_database(reviewer.store)
        if reviewer.policy.deadline_seconds > policy.review_deadline_seconds:
            raise ValueError("REVIEWER_DEADLINE_EXCEEDS_CYCLE_POLICY")

    def _identity(self, packet):
        """Receipt binding metadata; jev_review stores it but never sends it to the provider.

        A V2 packet adds its agent ID and version, so every receipt is attributable while
        the reviewed state stays blind to the proposer. Legacy packets keep the exact
        identity they were reviewed under.
        """
        identity = {
            "cycle_id": packet["cycle_id"],
            "research_item_key": packet["item_key"],
            "candidate_revision": packet["revision"],
            "evidence_hash": packet["evidence_hash"],
            # Only a B1, B2 or top-K cycle's packets name their rule; every other packet keeps
            # V2's.
            "selection_policy": packet["selection_policy"]
            if packet.get("selection_policy") in {B1_POLICY, B2_POLICY} | topk_rule.TOPK_POLICIES
            else SELECTION_POLICY,
        }
        agent = packet.get("agent")
        if agent:
            identity["agent_id"] = agent["agent_id"]
            identity["agent_version"] = agent["agent_version"]
        return identity

    @staticmethod
    def _skeptic_set(packet):
        """B2 packets are reviewed with SKEPTIC_QUESTIONS_V2, top-K packets with their pick
        kind's question set; every other packet keeps V1."""
        policy = packet.get("selection_policy")
        if policy in topk_rule.TOPK_POLICIES:
            return topk_rule.question_set((packet.get("state") or {}).get("kind"), policy)
        return SKEPTIC_V2 if packet.get("selection_policy") == B2_POLICY else SKEPTIC

    def _claim(self, cycle_id, *, eligible=None):
        """Lease the next undecided packets; ``eligible`` (top-K) skips packets it refuses."""
        now = _utc(self.clock())
        claims = []
        with self.store.transaction() as conn:
            rows = self._rows(conn, cycle_id)
            self._cycle(rows)
            packets = self._latest(rows)
            for packet in sorted(packets.values(), key=lambda p: p["rank"]):
                if eligible is not None and not eligible(packet):
                    continue
                key, revision = packet["item_key"], packet["revision"]
                related = [
                    r
                    for r in rows
                    if r["body"].get("item_key") == key and r["body"].get("revision") == revision
                ]
                if any(r["kind"] == "RESEARCH_DECISION" for r in related):
                    continue
                past_claims = [r["body"] for r in related if r["kind"] == "RESEARCH_CLAIM"]
                if past_claims and datetime.fromisoformat(past_claims[-1]["lease_until"]) > now:
                    continue
                request_id = str(uuid5(UUID(str(cycle_id)), f"{key}:{revision}"))
                deadline = min(
                    self._review_deadline(packet),
                    now + timedelta(seconds=self.policy.review_deadline_seconds),
                )
                claim = {
                    "item_key": key,
                    "revision": revision,
                    "request_id": request_id,
                    "deadline": deadline.isoformat(),
                    "lease_until": (
                        now + timedelta(seconds=self.policy.claim_lease_seconds)
                    ).isoformat(),
                }
                self._event(conn, cycle_id, "CLAIM", claim, "research:claim:" + str(uuid4()))
                claims.append((packet, claim))
                if len(claims) == self.policy.max_inflight:
                    break
        return claims

    def _existing_result(self, packet, request_id):
        """Recover retained receipts; an interrupted request is never a fresh vote."""
        with self.reviewer.store.connect() as conn:
            request = conn.execute(
                "SELECT * FROM lab.jev_requests WHERE request_id=%s", (request_id,)
            ).fetchone()
            if request is None:
                return None
            receipts = conn.execute(
                "SELECT * FROM lab.jev_receipts WHERE request_id=%s ORDER BY attempt,event_seq",
                (request_id,),
            ).fetchall()
        if not receipts:
            return ReviewResult(request_id, "NEEDS_REVIEW", "INTERRUPTED_REVIEW", (), {})
        receipt = receipts[-1]
        ids = tuple(str(r["receipt_id"]) for r in receipts)
        if receipt["outcome"] != "VALID":
            return ReviewResult(
                request_id, "NEEDS_REVIEW", receipt["error_code"] or "PROVIDER_UNAVAILABLE", ids, {}
            )
        try:
            self.reviewer.store.project_receipt(receipt["receipt_id"])
            answers = validated_answers(
                bytes(receipt["response_bytes"]), self._skeptic_set(packet)
            )
            uncertain = _uncertain_answers(answers)
            return ReviewResult(
                request_id,
                "NEEDS_REVIEW" if uncertain else "RECORDED",
                "UNCERTAIN_JUDGMENT" if uncertain else None,
                ids,
                answers,
            )
        except Exception:
            return ReviewResult(request_id, "NEEDS_REVIEW", "RECEIPT_INTEGRITY_FAILED", ids, {})

    def _verify_result(self, packet, request_id, result):
        question_set = self._skeptic_set(packet)
        try:
            if result.request_id != request_id:
                raise ValueError
            for receipt_id in result.receipt_ids:
                if not self.reviewer.store.verify(receipt_id)["valid"]:
                    raise ValueError
                with self.reviewer.store.connect() as conn:
                    row = conn.execute(
                        """SELECT q.*,r.response_bytes,r.outcome FROM lab.jev_requests q
                        JOIN lab.jev_receipts r USING(request_id) WHERE r.receipt_id=%s""",
                        (receipt_id,),
                    ).fetchone()
                if (
                    row is None
                    or str(row["request_id"]) != request_id
                    or any(
                        row["evidence_identity"].get(k) != v
                        for k, v in self._identity(packet).items()
                    )
                    or strict_json(row["request_json"])
                    != {
                        "model": JEV_MODEL,
                        "state": packet["state"],
                        "questions": question_set.questions,
                    }
                    or row["deadline"] > datetime.fromisoformat(packet["expires_at"])
                ):
                    raise ValueError
                if receipt_id == result.receipt_ids[-1] and _answer_bearing_result(result):
                    if (
                        row["outcome"] != "VALID"
                        or validated_answers(bytes(row["response_bytes"]), question_set)
                        != result.answers
                    ):
                        raise ValueError
            return result
        except Exception:
            return ReviewResult(
                request_id, "NEEDS_REVIEW", "RECEIPT_INTEGRITY_FAILED", result.receipt_ids, {}
            )

    @staticmethod
    def _evidence_task_id(packet):
        return str(
            uuid5(
                UUID(str(packet["cycle_id"])),
                f"evidence:{packet['item_key']}:{packet['revision']}",
            )
        )

    async def _review_b2(self, packet, claim):
        """B2: SKEPTIC_QUESTIONS_V2, components decide, the verdict is dissent.

        An item without a rationale is NEEDS_REVIEW / RATIONALE_REQUIRED with no provider
        call. B1's shadow is not computable from V2 answers, so no RESEARCH_SHADOW_DISPOSITION
        is written and the decision says so. Supersession, expiry and the evidence-task loop
        are exactly V2's and B1's.
        """
        request_id = claim["request_id"]
        if b2_rule.has_rationale(packet):
            result = self._existing_result(packet, request_id)
            if result is None:
                result = await self.reviewer.jev_review(
                    request_id=request_id,
                    identity=self._identity(packet),
                    state=packet["state"],
                    question_set=SKEPTIC_V2,
                    expires_at=datetime.fromisoformat(claim["deadline"]),
                    purpose="ENGINEERING_TEST",
                )
            result = self._verify_result(packet, request_id, result)
            outcome = b2_rule.b2_disposition(result)
        else:
            result = ReviewResult(request_id, "NEEDS_REVIEW", b2_rule.RATIONALE_REQUIRED, (), {})
            outcome = b2_rule.rationale_required()
        disposition, reason, reasons = outcome.disposition, outcome.reason, list(outcome.reasons)
        now = _utc(self.clock())
        with self.store.transaction() as conn:
            latest = self._latest(self._rows(conn, packet["cycle_id"]))[packet["item_key"]]
            if latest["revision"] != packet["revision"]:
                disposition, reason = "SUPERSEDED", "MATERIAL_NEW_EVIDENCE"
                reasons = [reason]
            elif now >= self._review_deadline(packet):
                disposition, reason = "EXPIRED", "ORIGINAL_EVIDENCE_DEADLINE_PASSED"
                reasons = [reason]
            body = {
                "item_key": packet["item_key"],
                "revision": packet["revision"],
                "request_id": request_id,
                "receipt_ids": list(result.receipt_ids),
                "evidence_hash": packet["evidence_hash"],
                "answers": result.answers,
                "disposition": disposition,
                "reason": reason,
                "evidence_tasks": b2_rule.evidence_tasks(reasons)
                if disposition == "NEEDS_REVIEW"
                else [],
                **b2_rule.decision_fields(outcome, reasons),
            }
            if disposition == "NEEDS_REVIEW":
                body["evidence_task_id"] = self._evidence_task_id(packet)
            self._event(
                conn,
                packet["cycle_id"],
                "DECISION",
                body,
                f"research:{packet['cycle_id']}:{packet['item_key']}:{packet['revision']}:decision",
            )
            if disposition == "NEEDS_REVIEW":
                task_id = body["evidence_task_id"]
                task = {
                    "task_id": task_id,
                    "item_key": packet["item_key"],
                    "revision": packet["revision"],
                    "request_id": request_id,
                    "created_at": now.isoformat(),
                    "expires_at": packet["expires_at"],
                    "requirements": body["evidence_tasks"],
                }
                self._event(
                    conn, packet["cycle_id"], "EVIDENCE_TASK", task, f"research:task:{task_id}"
                )
        return body

    async def _review(self, packet, claim):
        if packet.get("selection_policy") == B2_POLICY:
            return await self._review_b2(packet, claim)
        request_id = claim["request_id"]
        result = self._existing_result(packet, request_id)
        if result is None:
            result = await self.reviewer.jev_review(
                request_id=request_id,
                identity=self._identity(packet),
                state=packet["state"],
                question_set=SKEPTIC,
                expires_at=datetime.fromisoformat(claim["deadline"]),
                purpose="ENGINEERING_TEST",
            )
        result = self._verify_result(packet, request_id, result)
        b1 = b1_disposition(result)  # Always computed: the shadow, or the rule itself.
        b1_cycle = packet.get("selection_policy") == B1_POLICY
        if b1_cycle:
            disposition, reason = b1.disposition, b1.reason
        else:
            disposition, reason = selection_disposition(result)
        shadow, shadow_reasons = b1.disposition, list(b1.reasons)
        now = _utc(self.clock())
        with self.store.transaction() as conn:
            latest = self._latest(self._rows(conn, packet["cycle_id"]))[packet["item_key"]]
            if latest["revision"] != packet["revision"]:
                disposition, reason = "SUPERSEDED", "MATERIAL_NEW_EVIDENCE"
                shadow, shadow_reasons = disposition, [reason]
            elif now >= self._review_deadline(packet):
                disposition, reason = "EXPIRED", "ORIGINAL_EVIDENCE_DEADLINE_PASSED"
                shadow, shadow_reasons = disposition, [reason]
            body = {
                "item_key": packet["item_key"],
                "revision": packet["revision"],
                "request_id": request_id,
                "receipt_ids": list(result.receipt_ids),
                "evidence_hash": packet["evidence_hash"],
                "answers": result.answers,
                "disposition": disposition,
                "reason": reason,
                "evidence_tasks": evidence_tasks(result.answers)
                if disposition == "NEEDS_REVIEW"
                else [],
            }
            if b1_cycle:  # A V2 decision body is exactly as before.
                body["selection_policy"] = B1_POLICY
                body["dissent"] = b1.dissent
                body["dissent_tied"] = b1.dissent_tied
                body["reasons"] = shadow_reasons
            if disposition == "NEEDS_REVIEW":
                body["evidence_task_id"] = str(
                    uuid5(
                        UUID(str(packet["cycle_id"])),
                        f"evidence:{packet['item_key']}:{packet['revision']}",
                    )
                )
            self._event(
                conn,
                packet["cycle_id"],
                "DECISION",
                body,
                f"research:{packet['cycle_id']}:{packet['item_key']}:{packet['revision']}:decision",
            )
            if result.receipt_ids:
                # Shadow B1 for every recorded SKEPTIC receipt chain, in the decision's own
                # transaction; nothing in publication or admission reads this kind.
                self.store.event(
                    conn,
                    SHADOW_KIND,
                    shadow_body(
                        packet=packet,
                        request_id=request_id,
                        receipt_ids=result.receipt_ids,
                        active_policy=B1_POLICY if b1_cycle else SELECTION_POLICY,
                        disposition=shadow,
                        reasons=shadow_reasons,
                        outcome=b1,
                    ),
                    key=shadow_key(result.receipt_ids[-1]),
                )
            if disposition == "NEEDS_REVIEW":
                task_id = body["evidence_task_id"]
                task = {
                    "task_id": task_id,
                    "item_key": packet["item_key"],
                    "revision": packet["revision"],
                    "request_id": request_id,
                    "created_at": now.isoformat(),
                    "expires_at": packet["expires_at"],
                    "requirements": body["evidence_tasks"],
                }
                self._event(
                    conn, packet["cycle_id"], "EVIDENCE_TASK", task, f"research:task:{task_id}"
                )
        return body

    async def tick(self, cycle_id):
        if self._topk_cycle(str(cycle_id)):
            return await self._topk_tick(str(cycle_id))
        claims = self._claim(str(cycle_id))
        results = await asyncio.gather(*(self._review(packet, claim) for packet, claim in claims))
        await self._quality_tick(str(cycle_id))
        return results

    @staticmethod
    def _quality_set(packet):
        """B1 and B2 packets need QUALITY_V2's category, top-K packets QUALITY_V3's score;
        every other packet keeps V1."""
        if packet.get("selection_policy") in topk_rule.TOPK_POLICIES:
            return QUALITY_V3, QUALITY_V3_POLICY
        if packet.get("selection_policy") in (B1_POLICY, B2_POLICY):
            return QUALITY_V2, QUALITY_V2_POLICY
        return QUALITY, QUALITY_POLICY

    def _quality_identity(self, packet):
        return {**self._identity(packet), "quality_policy": self._quality_set(packet)[1]}

    def _review_deadline(self, packet):
        """V2 and earlier: the packet expiry or ``review_validity_seconds`` after receipt,
        whichever is first. Report V3 (``review_validity: PACKET_EXPIRY``): the packet expiry,
        so a pick stays reviewable and selectable until the agent's own ``valid_until``."""
        if packet.get("review_validity") == REVIEW_VALIDITY:
            return datetime.fromisoformat(packet["expires_at"])
        return min(
            datetime.fromisoformat(packet["expires_at"]),
            datetime.fromisoformat(packet.get("received_at", packet["created_at"]))
            + timedelta(seconds=self.policy.review_validity_seconds),
        )

    @staticmethod
    def _decisions(rows, packet):
        return [
            r["body"]
            for r in rows
            if r["kind"] == "RESEARCH_DECISION"
            and r["body"].get("item_key") == packet["item_key"]
            and r["body"].get("revision") == packet["revision"]
        ]

    @staticmethod
    def _selections(rows):
        """Published selections in event order; each one spends a cycle slot for good."""
        return [r for r in rows if r["kind"] == "RESEARCH_SELECTED"]

    def _unpublished_approvals(self, rows, now):
        """Rank-ordered latest-revision approvals, inside their window, never published."""
        published = {
            (r["body"]["packet"]["item_key"], r["body"]["packet"]["revision"])
            for r in self._selections(rows)
        }
        approvals = []
        for packet in sorted(self._latest(rows).values(), key=lambda p: p["rank"]):
            decisions = self._decisions(rows, packet)
            if (
                (packet["item_key"], packet["revision"]) not in published
                and now < self._review_deadline(packet)
                and decisions
                and decisions[-1]["disposition"] == "APPROVED"
            ):
                approvals.append((packet, decisions[-1]))
        return approvals

    async def _quality_tick(self, cycle_id):
        with self.repo.connect() as conn:
            rows = self._rows(conn, cycle_id)
        started = next((r["body"] for r in rows if r["kind"] == "RESEARCH_STARTED"), {})
        floored = self._cycle_rule(started).floored
        candidates = self._unpublished_approvals(rows, _utc(self.clock()))
        remaining = self.policy.selection_limit - len(self._selections(rows))
        if floored:
            # B1 and B2: every approval needs its QUALITY_V2 category for the owner's floor.
            if remaining <= 0 or not candidates:
                return
        # V2: a comparative cutoff is needed only when unpublished qualified supply exceeds
        # the slots this cycle has left. Published selections are never re-ranked.
        elif remaining <= 0 or len(candidates) <= remaining:
            return
        work = [
            packet
            for packet, _ in candidates
            if not any(
                r["kind"] == "RESEARCH_QUALITY"
                and r["body"].get("item_key") == packet["item_key"]
                and r["body"].get("revision") == packet["revision"]
                for r in rows
            )
        ]

        async def judge(packet):
            question_set, quality_policy = self._quality_set(packet)
            request_id = str(
                uuid5(
                    UUID(str(cycle_id)),
                    f"quality:{packet['item_key']}:{packet['revision']}:{quality_policy}",
                )
            )
            result = self._existing_quality(packet, request_id)
            if result is None:
                result = await self.reviewer.jev_review(
                    request_id=request_id,
                    identity=self._quality_identity(packet),
                    state={"candidate": packet["state"], "muse_rank": packet["rank"]},
                    question_set=question_set,
                    expires_at=datetime.fromisoformat(packet["expires_at"]),
                    purpose="ENGINEERING_TEST",
                )
            # QUALITY_V2's category Choice can be Insufficient evidence or tied: the judgment
            # is still recorded, its category is None and it meets no floor.
            unresolved_category = (
                quality_policy == QUALITY_V2_POLICY
                and result.status == "NEEDS_REVIEW"
                and result.reason == "UNCERTAIN_JUDGMENT"
            )
            try:
                if (result.status != "RECORDED" and not unresolved_category) or (
                    not result.receipt_ids
                ):
                    raise ValueError
                answers = validated_answers(
                    encoded(
                        {
                            "model": JEV_MODEL,
                            "answers": result.answers,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        }
                    ),
                    question_set,
                )
                score = quality_score(answers)
                status = "RANKED"
            except Exception:
                answers, score, status = {}, None, "NEEDS_REVIEW"
            body = {
                "item_key": packet["item_key"],
                "revision": packet["revision"],
                "request_id": request_id,
                "receipt_ids": list(result.receipt_ids),
                "quality_policy": quality_policy,
                "status": status,
                "score": score,
                "answers": answers,
            }
            if quality_policy == QUALITY_V2_POLICY:
                # None when insufficient or tied: such an item meets no floor.
                body["category"] = quality_category(answers)
            with self.store.transaction() as conn:
                latest = self._latest(self._rows(conn, cycle_id))[packet["item_key"]]
                if latest["revision"] != packet["revision"]:
                    body["status"] = "SUPERSEDED"
                self._event(
                    conn,
                    cycle_id,
                    "QUALITY",
                    body,
                    f"research:{cycle_id}:{packet['item_key']}:{packet['revision']}:quality",
                )

        await asyncio.gather(*(judge(packet) for packet in work))

    def _existing_quality(self, packet, request_id):
        with self.reviewer.store.connect() as conn:
            request = conn.execute(
                "SELECT * FROM lab.jev_requests WHERE request_id=%s", (request_id,)
            ).fetchone()
            if request is None:
                return None
            receipts = conn.execute(
                "SELECT * FROM lab.jev_receipts WHERE request_id=%s ORDER BY attempt,event_seq",
                (request_id,),
            ).fetchall()
        if not receipts:
            return ReviewResult(request_id, "NEEDS_REVIEW", "INTERRUPTED_REVIEW", (), {})
        receipt = receipts[-1]
        ids = tuple(str(r["receipt_id"]) for r in receipts)
        question_set = self._quality_set(packet)[0]
        try:
            expected_state = {"candidate": packet["state"], "muse_rank": packet["rank"]}
            if (
                receipt["outcome"] != "VALID"
                or any(
                    request["evidence_identity"].get(k) != v
                    for k, v in self._quality_identity(packet).items()
                )
                or strict_json(request["request_json"])
                != {
                    "model": JEV_MODEL,
                    "state": expected_state,
                    "questions": question_set.questions,
                }
            ):
                raise ValueError
            self.reviewer.store.project_receipt(receipt["receipt_id"])
            return ReviewResult(
                request_id,
                "RECORDED",
                None,
                ids,
                validated_answers(bytes(receipt["response_bytes"]), question_set),
            )
        except Exception:
            return ReviewResult(request_id, "NEEDS_REVIEW", "QUALITY_RECEIPT_INVALID", ids, {})

    def _selected_body(self, packet, result):
        """The published packet body shared by V2 and B1 before their policy fields.

        Every V2 and legacy state carries all four research fields; a REVIEW_DOSSIER_V3 state
        has no ``economic_relationship``, and a field the state lacks is never invented here
        (admission SQL compares thesis, disproof and sources with the stored state).
        """
        return {
            **packet,
            **{
                k: packet["state"][k]
                for k in ("thesis", "disproof", "economic_relationship", "sources")
                if k in packet["state"]
            },
            "receipt_id": result.receipt_ids[-1],
            "receipt_ids": list(result.receipt_ids),
            "disposition": "SELECTED",
            "strategy_version": "CATALYST_RETEST_V1"
            if packet["market"] == "US"
            else "CRYPTO_STRUCTURAL_RETEST_TEST_V1"
            if packet["market"] == "CRYPTO"
            else "INDIA_RESEARCH_ONLY",
            "market": "US_STOCKS" if packet["market"] == "US" else packet["market"],
            "review_valid_until": self._review_deadline(packet).isoformat(),
        }

    def _new_selections(self, rows, now, remaining):
        """Unpublished approvals for the ``remaining`` slots; published items are fixed."""
        # Publish only after every contender has a recorded outcome; completion order
        # must not determine which approvals fill the slots that are left.
        if any(not self._decisions(rows, p) for p in self._latest(rows).values()):
            return []
        started = next((r["body"] for r in rows if r["kind"] == "RESEARCH_STARTED"), {})
        rule = self._cycle_rule(started)
        if rule.floored:
            return self._new_floor_selections(rows, now, remaining, rule)
        approved = []
        for packet, decision in self._unpublished_approvals(rows, now):
            result = self._existing_result(packet, decision["request_id"])
            if result is None:
                continue
            result = self._verify_result(packet, decision["request_id"], result)
            if selection_disposition(result)[0] != "APPROVED":
                continue
            approved.append(self._selected_body(packet, result))
        if len(approved) <= remaining:
            # Supply within capacity needs no comparative cutoff; admission SQL requires
            # the explicit boolean for every MUSE_JEV_RESEARCH_SELECTION_V2 packet.
            for packet in approved:
                packet["quality_required"] = False
                packet["selection_limit"] = self.policy.selection_limit
            return approved
        quality = {
            (r["body"]["item_key"], r["body"]["revision"]): r["body"]
            for r in rows
            if r["kind"] == "RESEARCH_QUALITY"
        }
        if any(
            quality.get((p["item_key"], p["revision"]), {}).get("status") != "RANKED"
            for p in approved
        ):
            return []
        approved.sort(
            key=lambda p: (
                -quality[(p["item_key"], p["revision"])]["score"],
                p["item_key"],
                p["rank"],
            )
        )
        for index, packet in enumerate(approved, 1):
            q = quality[(packet["item_key"], packet["revision"])]
            recovered = self._existing_quality(packet, q["request_id"])
            if (
                recovered is None
                or recovered.status != "RECORDED"
                or list(recovered.receipt_ids) != q["receipt_ids"]
                or quality_score(recovered.answers) != q["score"]
                or not self.reviewer.store.verify(q["receipt_ids"][-1])["valid"]
            ):
                return []
            packet["quality_rank"] = index
            packet["quality_score"] = q["score"]
            packet["quality_policy"] = QUALITY_POLICY
            packet["quality_receipt_id"] = q["receipt_ids"][-1]
            packet["quality_required"] = True
            packet["quality_candidate_count"] = len(approved)
            packet["selection_limit"] = self.policy.selection_limit
        return approved[:remaining]

    def _new_floor_selections(self, rows, now, remaining, rule):
        """B1 and B2: components decide, the verdict is dissent, the owner's floor filters.

        Every approval needs its QUALITY_V2 judgment before any slot is filled, so
        completion order cannot decide which items take the remaining slots. An item whose
        judgment failed or whose category is insufficient, tied or below the floor is not
        selected; a receipt integrity failure holds publication, as in V2. A B2 packet also
        names its question set, which admission SQL binds to the receipt.
        """
        if rule.b2:
            disposition, extra = b2_rule.b2_disposition, {
                "question_set_version": SKEPTIC_V2.version
            }
        else:
            disposition, extra = b1_disposition, {}
        approved = []
        for packet, decision in self._unpublished_approvals(rows, now):
            if packet.get("selection_policy") != rule.policy:
                continue
            result = self._existing_result(packet, decision["request_id"])
            if result is None:
                continue
            result = self._verify_result(packet, decision["request_id"], result)
            outcome = disposition(result)
            if outcome.disposition == "APPROVED":
                approved.append((packet, result, outcome))
        quality = {
            (r["body"]["item_key"], r["body"]["revision"]): r["body"]
            for r in rows
            if r["kind"] == "RESEARCH_QUALITY"
        }
        if any((p["item_key"], p["revision"]) not in quality for p, _, _ in approved):
            return []
        eligible = []
        for packet, result, outcome in approved:
            q = quality[(packet["item_key"], packet["revision"])]
            if q.get("status") != "RANKED" or q.get("quality_policy") != QUALITY_V2_POLICY:
                continue
            recovered = self._existing_quality(packet, q["request_id"])
            if (
                recovered is None
                or recovered.status != "RECORDED"
                or list(recovered.receipt_ids) != q["receipt_ids"]
                or quality_score(recovered.answers) != q["score"]
                or quality_category(recovered.answers) != q.get("category")
                or not self.reviewer.store.verify(q["receipt_ids"][-1])["valid"]
            ):
                return []
            if not meets_quality_floor(q["category"], rule.quality_floor):
                continue
            eligible.append(
                {
                    **self._selected_body(packet, result),
                    "dissent": outcome.dissent,
                    **extra,
                    "quality_required": True,
                    "quality_policy": QUALITY_V2_POLICY,
                    "quality_receipt_id": q["receipt_ids"][-1],
                    "quality_score": q["score"],
                    "quality_category": q["category"],
                    "quality_floor": rule.quality_floor,
                    "selection_limit": self.policy.selection_limit,
                }
            )
        eligible.sort(key=lambda p: (-p["quality_score"], p["item_key"], p["rank"]))
        for index, packet in enumerate(eligible, 1):
            packet["quality_rank"] = index
            packet["quality_candidate_count"] = len(eligible)
        return eligible[:remaining]

    @staticmethod
    def _live(packet, latest, now):
        current = latest.get(packet.get("item_key"))
        valid_until = packet.get("review_valid_until")
        return (
            current is not None
            and current["revision"] == packet.get("revision")
            and valid_until is not None
            and now < datetime.fromisoformat(valid_until)
        )

    def approved_packets(self, cycle_id):
        """Publish add-only and return the cycle's live selections in publication order.

        A cycle records at most ``selection_limit`` RESEARCH_SELECTED events. A published
        packet is never re-emitted, re-ranked or rewritten: its stored body stays bound to
        its idempotency key and to admission SQL. Only unpublished approvals compete for
        the slots that remain, and they are ranked only when they exceed those slots.
        """
        now = _utc(self.clock())
        with self.repo.connect() as conn:
            rows = self._rows(conn, cycle_id)
        started = self._cycle(rows)
        if started.get("selection_policy") in topk_rule.TOPK_POLICIES:
            return self._topk_approved(str(cycle_id), rows, started, now)
        published = [r["event_seq"] for r in self._selections(rows)]
        remaining = self.policy.selection_limit - len(published)
        chosen = self._new_selections(rows, now, remaining) if remaining > 0 else []
        if chosen:
            with self.store.transaction() as conn:
                rows = self._rows(conn, cycle_id)
                current = self._latest(rows)
                # Add only against the exact published set the remaining slots came from.
                if [r["event_seq"] for r in self._selections(rows)] == published:
                    for packet in chosen:
                        if current[packet["item_key"]]["revision"] != packet["revision"]:
                            continue
                        rows.append(
                            self._event(
                                conn,
                                cycle_id,
                                "SELECTED",
                                {"packet": packet},
                                f"research:{cycle_id}:{packet['item_key']}:"
                                f"{packet['revision']}:selected",
                            )
                        )
        latest = self._latest(rows)
        return [
            {**r["body"]["packet"], "selection_event_seq": r["event_seq"]}
            for r in self._selections(rows)
            if self._live(r["body"]["packet"], latest, now)
        ]

    # --- Selection rule JEV_TOP_K_SELECTION_V1 (research_selection_topk) --------------------

    def _topk_cycle(self, cycle_id):
        """True for a cycle whose RESEARCH_STARTED names the top-K rule."""
        with self.repo.connect() as conn:
            row = conn.execute(
                """SELECT body->>'selection_policy' AS policy FROM lab.managed_events
                WHERE kind='RESEARCH_STARTED' AND body->>'cycle_id'=%s
                ORDER BY event_seq LIMIT 1""",
                (str(cycle_id),),
            ).fetchone()
        return row is not None and row["policy"] in topk_rule.TOPK_POLICIES

    def _topk_deadline(self, packet):
        """The last moment a pick can be reviewed for its cycle's ranking: its review deadline
        (the packet expiry), bounded by the cycle policy's ``review_validity_seconds`` after
        the report's receipt, so one unreviewable pick cannot hold the whole run."""
        received = datetime.fromisoformat(packet.get("received_at", packet["created_at"]))
        return min(
            self._review_deadline(packet),
            received + timedelta(seconds=self.policy.review_validity_seconds),
        )

    @staticmethod
    def _topk_ranking_row(rows):
        """The cycle's one ranking: the RESEARCH_RANKING under ``research:ranking:<cycle_id>``.
        Any other event of that kind (a defect or a forgery) is never read as the ranking."""
        return next(
            (
                r for r in rows
                if r["kind"] == topk_rule.RANKING_KIND
                and r["idempotency_key"] == topk_rule.ranking_key_for(r["body"].get("cycle_id"))
            ),
            None,
        )

    @staticmethod
    def _topk_basis(rows):
        """The ledger rows a ranking is computed from; it is written only against these."""
        return tuple(
            r["event_seq"]
            for r in rows
            if r["kind"] in ("RESEARCH_PACKET", "RESEARCH_DECISION", "RESEARCH_QUALITY")
        )

    async def _topk_tick(self, cycle_id):
        """Each pick's kind review and QUALITY_V3 review, concurrently, until the ranking.

        Each pick is reviewed once (no evidence task); nothing is reviewed after the cycle's
        ranking is recorded or past a pick's ranking deadline. Returns the kind decisions.
        """
        now = _utc(self.clock())
        with self.repo.connect() as conn:
            rows = self._rows(conn, cycle_id)
        self._cycle(rows)
        if self._topk_ranking_row(rows) is not None:
            return []

        def reviewable(packet):
            return now < self._topk_deadline(packet)

        claims = self._claim(cycle_id, eligible=reviewable)
        scored = {
            (r["body"]["item_key"], r["body"]["revision"])
            for r in rows
            if r["kind"] == "RESEARCH_QUALITY"
        }
        work = [
            packet
            for packet in sorted(self._latest(rows).values(), key=lambda p: p["rank"])
            if (packet["item_key"], packet["revision"]) not in scored and reviewable(packet)
        ][: self.policy.max_inflight]
        results = await asyncio.gather(
            *(self._review_topk(packet, claim) for packet, claim in claims),
            *(self._quality_topk(packet) for packet in work),
        )
        return list(results[: len(claims)])

    def _topk_verified(self, packet, request_id, questions, identity, *, fresh=None):
        """A top-K review read back from its retained request and receipts only.

        None when no request was recorded and ``fresh`` is None (never reviewed). The
        reviewer's own refusal before any request row (a spent call cap) keeps its code; a
        request without receipts is INTERRUPTED_REVIEW; a failed final attempt keeps its
        provider code; a request that does not bind this packet's state, identity and
        question set, or any receipt failing audit verification, is RECEIPT_INTEGRITY_FAILED.
        Judgments are re-projected idempotently, as ``_existing_result`` does. A database
        error propagates (unlike ``_existing_result``), so an outage is retried on a later
        tick and never recorded as a review outcome: top-K has no evidence loop to recover.
        """
        store = self.reviewer.store
        with store.connect() as conn:
            request = conn.execute(
                "SELECT * FROM lab.jev_requests WHERE request_id=%s", (request_id,)
            ).fetchone()
            receipts = conn.execute(
                "SELECT * FROM lab.jev_receipts WHERE request_id=%s ORDER BY attempt,event_seq",
                (request_id,),
            ).fetchall() if request else []
        if request is None:
            if fresh is None:
                return None
            code = fresh.reason or "MISSING_VALID_REVIEW"
            return TopKReview(ReviewResult(request_id, "NEEDS_REVIEW", code, (), {}), None, None)
        ids = tuple(str(r["receipt_id"]) for r in receipts)
        if not receipts:
            return TopKReview(
                ReviewResult(request_id, "NEEDS_REVIEW", "INTERRUPTED_REVIEW", (), {}),
                None, None,
            )
        final = receipts[-1]
        try:
            if (
                any(request["evidence_identity"].get(k) != v for k, v in identity.items())
                or request["question_set_version"] != questions.version
                or request["template_hash"] != questions.template_hash
                or strict_json(request["request_json"])
                != {"model": JEV_MODEL, "state": packet["state"],
                    "questions": questions.questions}
                or request["deadline"] > datetime.fromisoformat(packet["expires_at"])
            ):
                raise ValueError("REVIEW_BINDING_MISMATCH")
            for receipt in receipts:
                store.verify(receipt["receipt_id"], require_decisions=False)
            if final["outcome"] != "VALID":
                code = final["error_code"] or "PROVIDER_UNAVAILABLE"
                return TopKReview(
                    ReviewResult(request_id, "NEEDS_REVIEW", code, ids, {}), None, None
                )
            store.project_receipt(final["receipt_id"])  # Idempotent: restores judgments.
            store.verify(final["receipt_id"])
            raw = bytes(final["response_bytes"])
            answers = validated_answers(raw, questions)
            decimals = json.loads(raw, parse_float=Decimal)["answers"]
        except (ValueError, TypeError, KeyError, UnicodeError):
            return TopKReview(
                ReviewResult(request_id, "NEEDS_REVIEW", "RECEIPT_INTEGRITY_FAILED", ids, {}),
                None, None,
            )
        # The reviewer's own uncertainty test (jev_review): a Choice answered Insufficient
        # evidence or NEEDS_REVIEW, or tied at the top.
        uncertain = any(
            answer.get("type") == "choice"
            and (
                answer["choice"] in {INSUFFICIENT, "NEEDS_REVIEW"}
                or sum(
                    p == max(answer["probabilities"].values())
                    for p in answer["probabilities"].values()
                ) != 1
            )
            for answer in answers.values()
        )
        result = ReviewResult(
            request_id,
            "NEEDS_REVIEW" if uncertain else "RECORDED",
            "UNCERTAIN_JUDGMENT" if uncertain else None,
            ids,
            answers,
        )
        return TopKReview(result, decimals, final["event_seq"])

    async def _review_topk(self, packet, claim):
        """One pick's review with its kind's question set. The decision records the veto
        codes, the uncertain components and the verdict as dissent; there is no evidence
        task, and a failed or unbound review is NOT_RANKED with its own code."""
        request_id = claim["request_id"]
        kind = packet["state"].get("kind")
        policy = packet["selection_policy"]
        questions = topk_rule.question_set(kind, policy)
        identity = self._identity(packet)
        verified = self._topk_verified(packet, request_id, questions, identity)
        if verified is None:
            fresh = await self.reviewer.jev_review(
                request_id=request_id,
                identity=identity,
                state=packet["state"],
                question_set=questions,
                expires_at=datetime.fromisoformat(claim["deadline"]),
                purpose="ENGINEERING_TEST",
            )
            verified = self._topk_verified(packet, request_id, questions, identity, fresh=fresh)
        result = verified.result
        outcome = topk_rule.assess_review(kind, result, policy, verified.decimal_answers)
        body = {
            "item_key": packet["item_key"],
            "revision": packet["revision"],
            "request_id": request_id,
            "receipt_ids": list(result.receipt_ids),
            "evidence_hash": packet["evidence_hash"],
            "answers": result.answers,
            "disposition": outcome.status,
            "reason": outcome.reason,
            **topk_rule.decision_fields(outcome, kind, policy),
        }
        if _utc(self.clock()) >= self._review_deadline(packet):
            body["disposition"] = "EXPIRED"
            body["reason"] = "ORIGINAL_EVIDENCE_DEADLINE_PASSED"
            body["reasons"] = [body["reason"]]
        with self.store.transaction() as conn:
            self._event(
                conn,
                packet["cycle_id"],
                "DECISION",
                body,
                f"research:{packet['cycle_id']}:{packet['item_key']}:{packet['revision']}:decision",
            )
        return body

    def _topk_quality_request(self, packet):
        return str(
            uuid5(
                UUID(str(packet["cycle_id"])),
                f"quality:{packet['item_key']}:{packet['revision']}:{QUALITY_V3_POLICY}",
            )
        )

    async def _quality_topk(self, packet):
        """One pick's MUSE_JEV_COMPARATIVE_QUALITY_V3 review of the same reviewed state: its
        0-100 score and category (or NOT_SCORED with its code)."""
        cycle_id = packet["cycle_id"]
        request_id = self._topk_quality_request(packet)
        identity = self._quality_identity(packet)
        verified = self._topk_verified(packet, request_id, QUALITY_V3, identity)
        if verified is None:
            deadline = min(
                self._review_deadline(packet),
                _utc(self.clock()) + timedelta(seconds=self.policy.review_deadline_seconds),
            )
            fresh = await self.reviewer.jev_review(
                request_id=request_id,
                identity=identity,
                state=packet["state"],
                question_set=QUALITY_V3,
                expires_at=deadline,
                purpose="ENGINEERING_TEST",
            )
            verified = self._topk_verified(packet, request_id, QUALITY_V3, identity, fresh=fresh)
        outcome = topk_rule.assess_quality(verified.result, verified.decimal_answers)
        body = {
            "item_key": packet["item_key"],
            "revision": packet["revision"],
            "request_id": request_id,
            "receipt_ids": list(verified.result.receipt_ids),
            "quality_policy": QUALITY_V3_POLICY,
            "status": outcome.status,
            "reason": outcome.reason,
            "score": topk_rule.score_text(outcome.score),
            "category": outcome.category,
            "answers": verified.result.answers,
        }
        with self.store.transaction() as conn:
            self._event(
                conn,
                cycle_id,
                "QUALITY",
                body,
                f"research:{cycle_id}:{packet['item_key']}:{packet['revision']}:quality",
            )
        return body

    def _topk_assess(self, packet, decision, quality):
        """One pick's ranking inputs, re-derived from its retained receipts: a recorded
        decision or score is never trusted on its own."""
        kind = packet["state"].get("kind")
        policy = packet["selection_policy"]
        receipt_id = receipt_seq = quality_receipt_id = None
        if decision is None:
            review = topk_rule.not_ranked(topk_rule.REVIEW_DEADLINE_PASSED, kind, policy)
        elif decision["disposition"] == "EXPIRED":
            review = topk_rule.not_ranked(decision["reason"], kind, policy)
        else:
            verified = self._topk_verified(
                packet, decision["request_id"], topk_rule.question_set(kind, policy),
                self._identity(packet),
            )
            if verified is None:
                review = topk_rule.not_ranked("MISSING_VALID_REVIEW", kind, policy)
            else:
                review = topk_rule.assess_review(
                    kind, verified.result, policy, verified.decimal_answers
                )
                receipt_id = (verified.result.receipt_ids or (None,))[-1]
                receipt_seq = verified.receipt_seq
        if quality is None:
            scored = topk_rule.QualityOutcome(
                topk_rule.NOT_SCORED, topk_rule.REVIEW_DEADLINE_PASSED, None, None
            )
        else:
            verified = self._topk_verified(
                packet, quality["request_id"], QUALITY_V3, self._quality_identity(packet)
            )
            if verified is None:
                scored = topk_rule.QualityOutcome(
                    topk_rule.NOT_SCORED, "MISSING_VALID_REVIEW", None, None
                )
            else:
                scored = topk_rule.assess_quality(verified.result, verified.decimal_answers)
                quality_receipt_id = (verified.result.receipt_ids or (None,))[-1]
                if scored.status == topk_rule.SCORED and (
                    quality.get("status") != topk_rule.SCORED
                    or quality.get("score") != topk_rule.score_text(scored.score)
                    or quality.get("category") != scored.category
                    or quality.get("receipt_ids") != list(verified.result.receipt_ids)
                ):
                    # Admission SQL binds the recorded score to the receipt: they must agree.
                    scored = topk_rule.QualityOutcome(
                        topk_rule.NOT_SCORED, "QUALITY_RECORD_MISMATCH", None, None
                    )
        return topk_rule.Assessed(
            item_key=packet["item_key"],
            revision=packet["revision"],
            symbol=packet["symbol"],
            kind=kind,
            agent_rank=packet["rank"],
            review=review,
            quality=scored,
            receipt_id=receipt_id,
            receipt_seq=receipt_seq,
            quality_receipt_id=quality_receipt_id,
        )

    def _topk_ranking(self, cycle_id, rows, started, rule, now):
        """The cycle's RESEARCH_RANKING body and its basis, or None while a pick is still
        reviewable: every pick needs both reviews, or its ranking deadline must have passed
        (it is then NOT_RANKED ``REVIEW_DEADLINE_PASSED``)."""
        packets = sorted(self._latest(rows).values(), key=lambda p: p["rank"])
        decisions, quality = {}, {}
        for row in rows:
            key = (row["body"].get("item_key"), row["body"].get("revision"))
            if row["kind"] == "RESEARCH_DECISION":
                decisions[key] = row["body"]
            elif row["kind"] == "RESEARCH_QUALITY":
                quality[key] = row["body"]
        pending = [
            packet for packet in packets
            if (packet["item_key"], packet["revision"]) not in decisions
            or (packet["item_key"], packet["revision"]) not in quality
        ]
        if any(now < self._topk_deadline(packet) for packet in pending):
            return None
        assessed = [
            self._topk_assess(
                packet,
                decisions.get((packet["item_key"], packet["revision"])),
                quality.get((packet["item_key"], packet["revision"])),
            )
            for packet in packets
        ]
        body = topk_rule.ranking_body(
            cycle_id=cycle_id,
            run_slot=started.get("run_slot"),
            k=rule.k,
            entries=topk_rule.rank_entries(assessed),
            complete=not pending,
            policy=rule.policy,
        )
        return body, self._topk_basis(rows)

    def _topk_approved(self, cycle_id, rows, started, now):
        """Rank once (one RESEARCH_RANKING per cycle), publish ranks 1..K, and return the
        cycle's live selections in publication order."""
        rule = self._cycle_rule(started)
        if self._topk_ranking_row(rows) is None:
            computed = self._topk_ranking(cycle_id, rows, started, rule, now)
            if computed is None:
                return []
            body, basis = computed
            with self.store.transaction() as conn:
                current = self._rows(conn, cycle_id)
                # Write only against the exact rows the ranking was computed from.
                if self._topk_ranking_row(current) is None and self._topk_basis(current) == basis:
                    self.store.event(
                        conn, topk_rule.RANKING_KIND, body,
                        key=topk_rule.ranking_key_for(cycle_id),
                    )
        with self.store.transaction() as conn:
            rows = self._rows(conn, cycle_id)
            if self._topk_ranking_row(rows) is not None:
                self._topk_publish_top(conn, cycle_id, rows, started, now)
                rows = self._rows(conn, cycle_id)
        latest = self._latest(rows)
        return [
            {**r["body"]["packet"], "selection_event_seq": r["event_seq"]}
            for r in self._selections(rows)
            if self._live(r["body"]["packet"], latest, now)
        ]

    def _topk_publish_top(self, conn, cycle_id, rows, started, now):
        """Publish ranks 1..K in rank order, once. A pick whose symbol another cycle of the
        same ``run_slot`` already selected is skipped (DUPLICATE_SYMBOL_IN_RUN), as is a pick
        whose review expired (REVIEW_EXPIRED), each recorded as RESEARCH_SELECTION_SKIPPED;
        the next-ranked pick takes its place, so up to K picks of the run stay live. A
        published or skipped pick keeps that outcome; later publications (replacements after
        the system check) go through ``publish_ranked``."""
        ranking = self._topk_ranking_row(rows)
        k = ranking["body"]["k"]
        published = {r["body"]["packet"]["item_key"] for r in self._selections(rows)}
        skipped = {r["body"]["item_key"] for r in rows if r["kind"] == topk_rule.SKIPPED_KIND}
        latest = self._latest(rows)
        slots = 0
        for entry in ranking["body"]["entries"]:
            if slots >= k:
                break
            if entry["status"] != topk_rule.RANKED or entry["item_key"] in skipped:
                continue
            if entry["item_key"] in published:
                slots += 1
                continue
            packet = latest[entry["item_key"]]
            reason, other = None, None
            if now >= self._review_deadline(packet):
                reason = "REVIEW_EXPIRED"
            else:
                other = self._topk_selected_in_run(conn, cycle_id, packet)
                if other is not None:
                    reason = topk_rule.DUPLICATE_SYMBOL_IN_RUN
            if reason is None:
                self.publish_ranked(conn, cycle_id, entry["item_key"])
                slots += 1
                continue
            self._event(
                conn,
                cycle_id,
                "SELECTION_SKIPPED",
                {
                    "item_key": entry["item_key"],
                    "revision": entry["revision"],
                    "symbol": packet["symbol"],
                    "rank": entry["rank"],
                    "reason": reason,
                    "run_slot": packet.get("run_slot"),
                    "selected_in_cycle_id": other["cycle_id"] if other else None,
                    "selected_event_seq": other["event_seq"] if other else None,
                    "ranking_event_seq": ranking["event_seq"],
                },
                f"research:{cycle_id}:{entry['item_key']}:{entry['revision']}:selection-skipped",
            )

    @staticmethod
    def _topk_selected_in_run(conn, cycle_id, packet):
        """The first selection of the same symbol in another cycle answering the same run."""
        run_slot = packet.get("run_slot")
        if not run_slot:
            return None
        slot = datetime.fromisoformat(run_slot)
        rows = conn.execute(
            """SELECT event_seq,body->'packet'->>'cycle_id' AS cycle_id,
            body->'packet'->>'run_slot' AS run_slot FROM lab.managed_events
            WHERE kind='RESEARCH_SELECTED' AND body->'packet'->>'symbol'=%s
            AND body->'packet'->>'cycle_id'<>%s ORDER BY event_seq""",
            (packet["symbol"], str(cycle_id)),
        ).fetchall()
        for row in rows:
            try:
                if row["run_slot"] and datetime.fromisoformat(row["run_slot"]) == slot:
                    return row
            except ValueError:
                continue
        return None

    def publish_ranked(self, conn, cycle_id, item_key, *, replacement_for=None):
        """Publish one RANKED entry of the cycle's RESEARCH_RANKING as RESEARCH_SELECTED.

        The public step for ranks 1..K here and for the later replacement of a pick that
        fails the system check. It runs in the caller's ledger transaction ``conn`` and reads
        only the ledger: the entry's receipts were re-verified when the ranking was written,
        and admission SQL verifies the published packet against them again. Idempotent: an
        item already published returns its RESEARCH_SELECTED row unchanged, whatever
        ``replacement_for`` says now. ``replacement_for`` is the item_key of another entry of
        the same ranking that this pick replaces (recorded in the packet). The run's duplicate
        rule holds for every publication: an entry recorded as skipped (TOPK_ENTRY_SKIPPED) or
        whose symbol another cycle of the same run already selected (DUPLICATE_SYMBOL_IN_RUN)
        is refused, and the caller moves on to the next entry. Other refusals:
        TOPK_CYCLE_REQUIRED, TOPK_RANKING_REQUIRED, TOPK_ENTRY_NOT_RANKED,
        TOPK_REPLACEMENT_UNKNOWN, REVIEW_EXPIRED and TOPK_RANKING_BINDING_FAILURE.
        """
        cycle_id = str(cycle_id)
        rows = self._rows(conn, cycle_id)
        started = self._cycle(rows)
        if started.get("selection_policy") not in topk_rule.TOPK_POLICIES:
            raise ValueError("TOPK_CYCLE_REQUIRED")
        ranking = self._topk_ranking_row(rows)
        if ranking is None:
            raise ValueError("TOPK_RANKING_REQUIRED")
        entries = {entry["item_key"]: entry for entry in ranking["body"]["entries"]}
        entry = entries.get(item_key)
        if entry is None or entry["status"] != topk_rule.RANKED:
            raise ValueError("TOPK_ENTRY_NOT_RANKED")
        if replacement_for is not None and (
            replacement_for == item_key or replacement_for not in entries
        ):
            raise ValueError("TOPK_REPLACEMENT_UNKNOWN")
        packet = self._latest(rows)[item_key]
        key = f"research:{cycle_id}:{item_key}:{packet['revision']}:selected"
        existing = next((r for r in self._selections(rows) if r["idempotency_key"] == key), None)
        if existing is not None:
            return existing
        if packet["revision"] != entry["revision"]:
            raise ValueError("TOPK_RANKING_BINDING_FAILURE")
        if _utc(self.clock()) >= self._review_deadline(packet):
            raise ValueError("REVIEW_EXPIRED")
        if any(
            r["kind"] == topk_rule.SKIPPED_KIND and r["body"].get("item_key") == item_key
            for r in rows
        ):
            raise ValueError("TOPK_ENTRY_SKIPPED")
        if self._topk_selected_in_run(conn, cycle_id, packet) is not None:
            raise ValueError(topk_rule.DUPLICATE_SYMBOL_IN_RUN)
        decision = self._decisions(rows, packet)
        quality = [
            r["body"] for r in rows
            if r["kind"] == "RESEARCH_QUALITY"
            and r["body"].get("item_key") == item_key
            and r["body"].get("revision") == packet["revision"]
        ]
        if (
            not decision
            or not quality
            or decision[-1]["receipt_ids"][-1:] != [entry["receipt_id"]]
            or quality[-1]["receipt_ids"][-1:] != [entry["quality_receipt_id"]]
        ):
            raise ValueError("TOPK_RANKING_BINDING_FAILURE")
        result = ReviewResult(
            decision[-1]["request_id"], "RECORDED", None, tuple(decision[-1]["receipt_ids"]), {}
        )
        body = {
            **self._selected_body(packet, result),
            **topk_rule.selected_fields(
                entry,
                ranking,
                k=ranking["body"]["k"],
                agent_rank=packet["rank"],
                quality_receipt_ids=quality[-1]["receipt_ids"],
                replacement_for=replacement_for,
                policy=started["selection_policy"],
            ),
        }
        return self._event(conn, cycle_id, "SELECTED", {"packet": body}, key)

    def replace_declined(self, conn, decline):
        """``TOPK_REPLACEMENT_V1``: the one replacement decision for a top-K pick declined at
        admission (plan 4.3, package replacement). Returns its RESEARCH_REPLACEMENT row, or None
        when no decision applies.

        ``decline`` is the selection's RESEARCH_ADMISSION_DECLINED row; the caller runs this in
        that decline's own ledger transaction ``conn``, under the shared lock every publication
        takes, so two ticks can never publish two replacements. Only the recorded ledger rows
        decide:

        * no decision for a cycle that is not top-K, for SUPERSEDED_BY_NEW_RESEARCH (the run is
          over), once the cycle or the declined pick has expired, or once a newer ``run_slot``
          than the pick's has been published;
        * otherwise the walk goes down the cycle's RESEARCH_RANKING to the next RANKED entry not
          yet selected (declined picks included) or skipped and publishes it through
          ``publish_ranked`` with ``replacement_for`` = the declined item. An entry it refuses
          (PASSED_OVER_REFUSALS: skipped, a duplicate symbol in the run, an expired review, a
          broken binding) is recorded in ``passed_over`` and the walk moves on. It stops at the
          first publication (PUBLISHED) or at the end of the ranking (EXHAUSTED,
          TOPK_RANKING_EXHAUSTED).

        One decision per declined pick, keyed by its cycle and item: an existing one is returned
        unchanged, so repeated ticks and restarts never publish another. Each decline removes
        one live pick and adds at most one, so a cycle never has more than K live picks. Any
        other refusal (a cycle-level code such as RESEARCH_POLICY_CHANGED) raises, and the
        caller's transaction then commits neither the decline nor anything of this decision.
        """
        declined_body = decline["body"]
        reason = declined_body.get("reason")
        cycle_id, item_key = declined_body.get("cycle_id"), declined_body.get("item_key")
        if not cycle_id or not item_key or reason == SUPERSEDED_BY_NEW_RESEARCH:
            return None
        cycle_id = str(cycle_id)
        rows = self._rows(conn, cycle_id)
        started = self._started(rows)
        if started is None or started.get("selection_policy") not in topk_rule.TOPK_POLICIES:
            return None
        selection = next(
            (
                r for r in self._selections(rows)
                if r["event_seq"] == declined_body.get("selection_event_seq")
                and r["body"]["packet"].get("item_key") == item_key
            ),
            None,
        )
        if selection is None:
            return None  # Not a selection of this cycle: nothing of the cycle was declined.
        declined = selection["body"]["packet"]
        key = topk_rule.replacement_key_for(cycle_id, item_key, declined["revision"])
        existing = next((r for r in rows if r["idempotency_key"] == key), None)
        if existing is not None:
            return existing
        self._cycle(rows)  # The cycle's stored policy (RESEARCH_POLICY_CHANGED raises).
        now = _utc(self.clock())
        if (
            now >= datetime.fromisoformat(started["expires_at"])
            or now >= self._review_deadline(declined)
        ):
            return None  # The cycle's picks have expired.
        slot, newest = started.get("run_slot"), newest_v3_run_slot(conn)
        if newest is not None and (slot is None or newest > datetime.fromisoformat(slot)):
            return None  # A newer run has been published: this run is over.
        ranking = self._topk_ranking_row(rows)
        if ranking is None:
            raise ValueError("TOPK_RANKING_REQUIRED")
        taken = {r["body"]["packet"]["item_key"] for r in self._selections(rows)} | {
            r["body"].get("item_key") for r in rows if r["kind"] == topk_rule.SKIPPED_KIND
        }
        passed_over, chosen, published = [], None, None
        for entry in ranking["body"]["entries"]:
            if entry["status"] != topk_rule.RANKED or entry["item_key"] in taken:
                continue
            try:
                published = self.publish_ranked(
                    conn, cycle_id, entry["item_key"], replacement_for=item_key
                )
            except ValueError as exc:
                if str(exc) not in topk_rule.PASSED_OVER_REFUSALS:
                    raise
                passed_over.append({"item_key": entry["item_key"], "rank": entry["rank"],
                                    "symbol": entry["symbol"], "code": str(exc)})
                continue
            chosen = entry
            break
        body = {
            "replacement_rule": topk_rule.REPLACEMENT_RULE,
            "declined_item_key": item_key,
            "declined_revision": declined["revision"],
            "declined_symbol": declined["symbol"],
            "declined_rank": declined.get("rank"),
            "declined_code": reason,
            "declined_selection_event_seq": selection["event_seq"],
            "decline_event_seq": decline["event_seq"],
            "outcome": topk_rule.REPLACEMENT_PUBLISHED if chosen
            else topk_rule.REPLACEMENT_EXHAUSTED,
            "code": None if chosen else topk_rule.RANKING_EXHAUSTED,
            "replacement_item_key": chosen["item_key"] if chosen else None,
            "replacement_revision": chosen["revision"] if chosen else None,
            "replacement_symbol": chosen["symbol"] if chosen else None,
            "replacement_rank": chosen["rank"] if chosen else None,
            "replacement_selection_event_seq": published["event_seq"] if chosen else None,
            "passed_over": passed_over,
            "ranking_event_seq": ranking["event_seq"],
            "k": ranking["body"]["k"],
            "run_slot": slot,
        }
        return self._event(conn, cycle_id, "REPLACEMENT", body, key)
