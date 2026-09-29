"""REVIEW_DOSSIER_V1: the deterministic, size-checked selection-Jev input for one item.

The dossier state is stored as the research packet ``state`` and sent to selection Jev
unchanged: admission SQL (migrations 013/014) binds the receipt's request state to the
stored packet state, so the reviewed input and the stored packet are one object.

It carries every text field and source excerpt untruncated, the code-computed technical
metrics, only the bars that the levels or the selection rationale cite, and the rationale
without the agent's own confidence. Nothing is truncated or dropped to fit: an item over
budget is rejected at intake with DOSSIER_OVER_BUDGET. The manifest records what was
included and what was omitted, with hashes, so the reviewed input can be audited against
the stored report.

Budget: jev_review refuses any encoded review state over JEV_STATE_CAP_BYTES. The largest
wrapper a review request puts around a dossier inside that state is QUALITY's
``{"candidate": ..., "muse_rank": 30}`` (29 bytes; SKEPTIC adds none; see
``wrapper_overhead_bytes``). STATE_BUDGET_BYTES stays well below cap minus wrapper so a
later wrapper (for example a second-stage review that also carries first-stage answers)
still fits. The rationale has its own ceiling inside the state budget.
"""

import re
from dataclasses import dataclass

from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.muse_reports import RATIONALE_VERSION
from catalyst_lab.repository import json_safe
from catalyst_lab.research_evidence import MAX_ERROR_DETAILS, canonical_sources
from catalyst_lab.research_ranking import QUALITY_POLICY

DOSSIER_VERSION = "REVIEW_DOSSIER_V1"
JEV_STATE_CAP_BYTES = 12_000  # jev_review._privacy_check limit on the encoded state
MAX_REPORT_RANK = 30  # Report items are ranked 1-30; QUALITY embeds the rank.
STATE_BUDGET_BYTES = 11_000
RATIONALE_BUDGET_BYTES = 3_000
RATIONALE_STATUS = "UNVERIFIED_PROPOSER_CLAIMS"
# The same label for every proposing agent and for legacy reports, so the reviewer cannot
# tell who proposed (plan 1.3). States recorded before 2026-09-24 keep the former constant
# EXTERNAL_MUSE_RESEARCH: stored packets are never recompiled, and a report's replay hash
# covers only the submitted report, never this state.
REVIEW_ORIGIN = "EXTERNAL_RESEARCH_AGENT"
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,79}")


def encoded_bytes(value):
    """Bytes exactly as sent: ``encoded`` escapes non-ASCII text (6 or 12 bytes a char)."""
    return len(encoded(value).encode())


def review_wrappers(state):
    """Every request state that embeds a dossier today, keyed by question-set version.

    Mirrors research_cycle: SKEPTIC sends the packet state itself and the comparative
    QUALITY review sends ``{"candidate": state, "muse_rank": rank}``.
    """
    return {
        SKEPTIC.version: state,
        QUALITY_POLICY: {"candidate": state, "muse_rank": MAX_REPORT_RANK},
    }


def wrapper_overhead_bytes():
    """The largest number of bytes a review request adds around a dossier state."""
    return max(
        encoded_bytes(wrapped) - encoded_bytes({}) for wrapped in review_wrappers({}).values()
    )


class DossierRejected(ValueError):
    """An item that cannot become a review dossier; its siblings are unaffected."""

    def __init__(self, code, errors):
        super().__init__(code)
        self.code = code
        self.errors = tuple(errors)[:MAX_ERROR_DETAILS]


@dataclass(frozen=True)
class Dossier:
    state: dict
    manifest: dict

    @property
    def evidence_hash(self):
        return digest(encoded(self.state))


def stored_rationale(block):
    """The full block kept in the packet body for analytics, confidence included."""
    return None if block is None else {"schema_version": RATIONALE_VERSION, **block}


def review_rationale(block):
    """The rationale as the reviewer receives it: claims to verify, never the confidence."""
    if block is None:
        return None
    return {
        "status": RATIONALE_STATUS,
        **{k: v for k, v in block.items() if k not in {"agent_confidence", "schema_version"}},
    }


def _section(value):
    text = encoded(value)
    return {"bytes": len(text.encode()), "sha256": digest(text)}


def _code(exc, default):
    return str(exc) if _CODE.fullmatch(str(exc)) else default


def compile_review_dossier(item, *, now, require_technical_evidence=False):
    """Compile one schema-valid report item (``muse_reports.ItemIntake``) into its dossier.

    Deterministic for a given item and clock. Raises DossierRejected for a per-item
    failure; a SENSITIVE_EVIDENCE_REJECTED ValueError propagates so intake can refuse
    the whole report.
    """
    contender, data, prefix = item.contender, item.canonical, item.path
    try:
        sources = json_safe(canonical_sources(contender.sources, now=now))
    except ValueError as exc:
        if str(exc) == "SENSITIVE_EVIDENCE_REJECTED":
            raise
        code = _code(exc, "INVALID_SOURCE_EVIDENCE")
        raise DossierRejected(code, [{"path": prefix + ".sources", "code": code}]) from None
    evidence = contender.technical_evidence
    if require_technical_evidence and evidence is None:
        code = "TECHNICAL_EVIDENCE_REQUIRED"
        raise DossierRejected(code, [{"path": prefix + ".technical_evidence", "code": code}])
    block = data.get("selection_rationale")
    cited_sources = sorted(
        {ref for claim in block["claims"] for ref in claim["supported_by"]["source_ids"]}
    ) if block else []
    cited_bars = sorted(
        {ref for claim in block["claims"] for ref in claim["supported_by"]["bar_ids"]}
    ) if block else []
    technical_context = {
        "origin": REVIEW_ORIGIN,
        "analysis": data["technical_analysis"],
        "execution_checks": "PENDING_INDEPENDENT_APP_VALIDATION",
    }
    if evidence is not None:
        try:
            technical_context["observed_facts"] = evidence.dossier_facts(
                now=now, levels=contender.levels, bar_ids=cited_bars
            )
        except ValueError as exc:
            code = _code(exc, "INVALID_TECHNICAL_EVIDENCE")
            raise DossierRejected(
                code, [{"path": prefix + ".technical_evidence", "code": code}]
            ) from None
    state = json_safe({
        "market": "US" if contender.market == "US_STOCKS" else contender.market,
        "symbol": data["symbol"],
        **{
            key: data[key]
            for key in ("thesis", "disproof", "economic_relationship", "catalyst", "levels")
        },
        "sources": sources,
        "technical_context": technical_context,
        "rationale": review_rationale(block),
    })
    size = encoded_bytes(state)
    rationale_size = encoded_bytes(state["rationale"]) if block else None
    errors = []
    if block and rationale_size > RATIONALE_BUDGET_BYTES:
        errors.append({
            "path": prefix + ".selection_rationale", "code": "DOSSIER_OVER_BUDGET",
            "bytes": rationale_size, "budget_bytes": RATIONALE_BUDGET_BYTES,
        })
    if size > STATE_BUDGET_BYTES:
        errors.append({
            "path": prefix, "code": "DOSSIER_OVER_BUDGET",
            "bytes": size, "budget_bytes": STATE_BUDGET_BYTES,
        })
    if errors:
        raise DossierRejected("DOSSIER_OVER_BUDGET", errors)
    _privacy_check(encoded(state))  # Defense in depth; the size check cannot fire here.

    omitted = [
        {"field": "direction", "reason": "IMPLIED_LONG_ONLY", **_section(data["direction"])},
        {
            "field": "signal_id", "reason": "PACKET_IDENTIFIER_NOT_REVIEW_EVIDENCE",
            **_section(data["signal_id"]),
        },
    ]
    bars = None
    if evidence is not None:
        facts = state["technical_context"]["observed_facts"]
        included = facts["observations"]["bars"]
        included_ids = [bar["bar_id"] for bar in included]
        dropped = [
            bar for bar in data["technical_evidence"]["bars"] if bar["bar_id"] not in included_ids
        ]
        bars = {
            "submitted_count": len(data["technical_evidence"]["bars"]),
            "included_bar_ids": included_ids,
            "omitted_bar_ids": [bar["bar_id"] for bar in dropped],
            "observations_hash": facts["observations_hash"],
            "included_sha256": digest(encoded(included)),
            "omitted_sha256": digest(encoded(dropped)),
        }
        if dropped:
            omitted.append({
                "field": "technical_evidence.bars", "reason": "NOT_CITED_BY_LEVELS_OR_RATIONALE",
                "count": len(dropped), **_section(dropped),
            })
    rationale = None
    if block:
        omitted.append({
            "field": "selection_rationale.agent_confidence",
            "reason": "ANALYTICS_ONLY_NEVER_REVIEW_INPUT",
            **_section(block["agent_confidence"]),
        })
        rationale = {
            "version": RATIONALE_VERSION,
            "bytes": rationale_size,
            "budget_bytes": RATIONALE_BUDGET_BYTES,
            "sha256": digest(encoded(state["rationale"])),
            "claim_count": len(block["claims"]),
            "cited_source_ids": cited_sources,
            "cited_bar_ids": cited_bars,
        }
    manifest = {
        "dossier_version": DOSSIER_VERSION,
        "state_budget_bytes": STATE_BUDGET_BYTES,
        "jev_state_cap_bytes": JEV_STATE_CAP_BYTES,
        "state_bytes": size,
        "state_sha256": digest(encoded(state)),
        "sections": {key: _section(value) for key, value in sorted(state.items())},
        "sources": [
            {
                "source_id": source["source_id"],
                "content_hash": source["content_hash"],
                "excerpt_chars": len(source["excerpt"]),
                **_section(source),
            }
            for source in state["sources"]
        ],
        "bars": bars,
        "rationale": rationale,
        "omitted": omitted,
        "truncated": [],
    }
    return Dossier(state, manifest)
