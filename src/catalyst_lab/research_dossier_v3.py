"""``REVIEW_DOSSIER_V3``: the selection-Jev input for one ``AGENT_RESEARCH_REPORT_V3`` pick.

Owner decision 2026-09-26: Jev reads each pick exactly as the agent sent it — the kind, every
price (the agent's current price and its time, entry, max entry, stop, target, the stated
reward-to-risk), the reasoning, the selection rationale, the sources and the bars — and never
the agent's name, its confidence or the signal ID. The dossier is stored as the packet
``state`` and sent unchanged, so admission SQL binds the reviewed input to the packet exactly
as for ``REVIEW_DOSSIER_V1``.

Layout (every text untruncated, every submitted bar included):

* ``market`` (CRYPTO), ``symbol``, ``kind``, ``agent_current_price``, ``agent_price_at``,
  ``levels``, ``stated_reward_risk`` and ``valid_until`` exactly as sent (``valid_until`` is the
  pick's own, else the report's).
* The reasoning at the top level: ``thesis``, ``why_now``, ``why_these_levels`` and ``risks``
  under their own names, and ``invalidation`` as ``disproof``. The configured question sets and
  the admission and position code already read ``thesis`` and ``disproof``; the manifest's
  ``field_map`` records the mapping.
* ``sources`` (0-8, canonical with ``content_hash``), ``technical_context`` (origin, pending
  independent checks and, when sent, ``observed_facts`` with every bar and code-computed
  descriptive metrics) and ``rationale`` (``UNVERIFIED_PROPOSER_CLAIMS``, no confidence).

Refusals are per pick: ``INVALID_SOURCE_EVIDENCE`` (a source retrieved after intake),
``FUTURE_TECHNICAL_EVIDENCE`` (bars retrieved after intake), ``AGENT_IDENTITY_IN_PICK`` (the
agent ID as a whole word, any case, in agent-written reviewed text; source excerpts and URLs
are third-party text and are not screened for it) and ``DOSSIER_OVER_BUDGET`` (11,000 bytes,
rationale 3,000; never truncated). Credential-like content still refuses the whole report.
"""

import re
from datetime import timedelta
from decimal import Decimal

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.muse_reports import RATIONALE_VERSION
from catalyst_lab.repository import json_safe
from catalyst_lab.research_dossier import (
    JEV_STATE_CAP_BYTES,
    RATIONALE_BUDGET_BYTES,
    REVIEW_ORIGIN,
    STATE_BUDGET_BYTES,
    Dossier,
    DossierRejected,
    encoded_bytes,
    review_rationale,
)
from catalyst_lab.research_evidence import canonical_source
from catalyst_lab.research_report_v3 import (
    AGENT_IDENTITY_IN_PICK,
    DOSSIER_OVER_BUDGET,
    FUTURE_TECHNICAL_EVIDENCE,
    INVALID_SOURCE_EVIDENCE,
    MARKET,
)

DOSSIER_VERSION_V3 = "REVIEW_DOSSIER_V3"
BAR_SELECTION = "ALL_SUBMITTED_BARS"
# Pick reasoning field -> reviewed state field.
FIELD_MAP = {
    "reasoning.thesis": "thesis",
    "reasoning.why_now": "why_now",
    "reasoning.why_these_levels": "why_these_levels",
    "reasoning.risks": "risks",
    "reasoning.invalidation": "disproof",
}
# Top-level pick fields the identity screen skips: the two never reach the reviewer, and the
# symbol is the coin chosen from the universe, not agent prose.
UNSCREENED = ("signal_id", "agent_confidence", "symbol")
# Values fixed by the schema at any depth (wire version, pick and claim kind, OHLC field).
LITERALS = frozenset({"schema_version", "kind", "field"})
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,79}")
D = Decimal


def _section(value):
    text = encoded(value)
    return {"bytes": len(text.encode()), "sha256": digest(text)}


def _code(exc, default):
    return str(exc) if _CODE.fullmatch(str(exc)) else default


def observed_facts(evidence, *, now):
    """Every submitted bar, as sent, with V1's descriptive metrics and no level rule."""
    if evidence.retrieved_at > now:
        raise ValueError(FUTURE_TECHNICAL_EVIDENCE)
    data = evidence.model_dump(mode="json")
    bars, quote = evidence.bars, evidence.quote
    closes = [bar.close for bar in bars]
    prior_volume = sum((bar.volume for bar in bars[-20:-1]), D(0)) / 19
    frame = timedelta(seconds=evidence.timeframe_seconds)
    return json_safe({
        "schema_version": evidence.schema_version,
        "origin": "EXTERNAL_RESEARCH_OBSERVATIONS",
        "execution_authority": False,
        "bar_selection": BAR_SELECTION,
        "observations": data,
        "observations_hash": digest(encoded(data)),
        "metrics": {
            "last_completed_close": closes[-1],
            "sma_5": sum(closes[-5:]) / 5,
            "sma_20": sum(closes[-20:]) / 20,
            "last_volume_vs_prior_19_mean": bars[-1].volume / prior_volume
            if prior_volume else None,
            "observed_dollar_volume_20": sum(
                (bar.close * bar.volume for bar in bars[-20:]), D(0)
            ),
            "spread_bps": (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000
            if quote else None,
            "last_bar_age_seconds": D(str((now - bars[-1].started_at - frame).total_seconds())),
            "quote_age_seconds": D(str((now - quote.observed_at).total_seconds()))
            if quote else None,
            "gap_count": sum(
                b.started_at - a.started_at != frame
                for a, b in zip(bars, bars[1:], strict=False)
            ),
        },
        "limitations": [
            "Submitted research observations are not independently authenticated broker data.",
            "Dollar volume is close times base-unit volume; gaps remain explicit.",
            "Fresh broker quotes, liquidity checks and the independent system check after "
            "selection remain mandatory.",
        ],
    })


def identity_paths(data, agent_id, prefix):
    """Paths of agent-written reviewed strings that name the agent (whole word, any case).

    Unreviewed fields (signal ID, both confidences), the symbol and schema literals (wire
    versions, kinds, OHLC field names) are skipped, and so are source excerpts and URLs: they
    are verbatim third-party text and pages, which the agent does not write. Letters, digits
    and the underscore continue a word, so ``claudes`` or ``muse_x`` is not the agent.
    """
    word = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(agent_id) + r"(?![A-Za-z0-9_])", re.IGNORECASE
    )
    found = []

    def walk(node, path, skip):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in skip or key in LITERALS:
                    continue
                inner = {"excerpt", "url"} if key == "sources" else (
                    {"agent_confidence"} if key == "selection_rationale" else set()
                )
                walk(child, f"{path}.{key}", inner)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]", skip)
        elif isinstance(node, str) and word.search(node):
            found.append(path)

    walk(data, prefix, set(UNSCREENED))
    return found


def compile_pick_dossier(item, *, now, agent_id, valid_until):
    """Compile one checked V3 pick (``research_report_v3.PickIntake``) into its dossier.

    Deterministic for a given pick, clock and validity. Raises DossierRejected for a per-pick
    failure; a SENSITIVE_EVIDENCE_REJECTED ValueError propagates so intake can refuse the
    whole report.
    """
    pick, data, prefix = item.pick, item.canonical, item.path
    try:
        sources = json_safe([canonical_source(source, now=now) for source in pick.sources])
    except ValueError as exc:
        if str(exc) == "SENSITIVE_EVIDENCE_REJECTED":
            raise
        code = INVALID_SOURCE_EVIDENCE
        raise DossierRejected(code, [
            {"path": prefix + ".sources", "code": _code(exc, code)}
        ]) from None
    technical_context = {
        "origin": REVIEW_ORIGIN,
        "execution_checks": "PENDING_INDEPENDENT_APP_VALIDATION",
    }
    if pick.technical_evidence is not None:
        try:
            technical_context["observed_facts"] = observed_facts(pick.technical_evidence, now=now)
        except ValueError as exc:
            if str(exc) != FUTURE_TECHNICAL_EVIDENCE:
                raise
            raise DossierRejected(FUTURE_TECHNICAL_EVIDENCE, [
                {"path": prefix + ".technical_evidence.retrieved_at",
                 "code": FUTURE_TECHNICAL_EVIDENCE}
            ]) from None
    leaks = identity_paths(data, agent_id, prefix)
    if leaks:
        raise DossierRejected(AGENT_IDENTITY_IN_PICK, [
            {"path": path, "code": AGENT_IDENTITY_IN_PICK} for path in leaks
        ])
    reasoning, block = data["reasoning"], data["selection_rationale"]
    state = json_safe({
        "market": MARKET,
        "symbol": data["symbol"],
        "kind": data["kind"],
        "agent_current_price": data["agent_current_price"],
        "agent_price_at": data["agent_price_at"],
        "levels": data["levels"],
        "stated_reward_risk": data["stated_reward_risk"],
        "valid_until": valid_until,
        **{target: reasoning[source.split(".", 1)[1]] for source, target in FIELD_MAP.items()},
        "sources": sources,
        "technical_context": technical_context,
        "rationale": review_rationale(block),
    })
    size = encoded_bytes(state)
    rationale_size = encoded_bytes(state["rationale"])
    errors = []
    if rationale_size > RATIONALE_BUDGET_BYTES:
        errors.append({
            "path": prefix + ".selection_rationale", "code": DOSSIER_OVER_BUDGET,
            "bytes": rationale_size, "budget_bytes": RATIONALE_BUDGET_BYTES,
        })
    if size > STATE_BUDGET_BYTES:
        errors.append({
            "path": prefix, "code": DOSSIER_OVER_BUDGET,
            "bytes": size, "budget_bytes": STATE_BUDGET_BYTES,
        })
    if errors:
        raise DossierRejected(DOSSIER_OVER_BUDGET, errors)
    _privacy_check(encoded(state))  # Defense in depth; the size check cannot fire here.
    omitted = [
        {"field": "signal_id", "reason": "PACKET_IDENTIFIER_NOT_REVIEW_EVIDENCE",
         **_section(data["signal_id"])},
        {"field": "agent_confidence", "reason": "ANALYTICS_ONLY_NEVER_REVIEW_INPUT",
         **_section(data["agent_confidence"])},
        {"field": "selection_rationale.agent_confidence",
         "reason": "ANALYTICS_ONLY_NEVER_REVIEW_INPUT",
         **_section(block["agent_confidence"])},
    ]
    bars = None
    if pick.technical_evidence is not None:
        submitted = data["technical_evidence"]["bars"]
        bars = {
            "submitted_count": len(submitted),
            "bar_selection": BAR_SELECTION,
            "included_bar_ids": [bar["bar_id"] for bar in submitted],
            "omitted_bar_ids": [],
            "observations_hash": technical_context["observed_facts"]["observations_hash"],
            "included_sha256": digest(encoded(submitted)),
        }
    cited_sources = sorted({ref for claim in block["claims"]
                            for ref in claim["supported_by"]["source_ids"]})
    cited_bars = sorted({ref for claim in block["claims"]
                         for ref in claim["supported_by"]["bar_ids"]})
    manifest = {
        "dossier_version": DOSSIER_VERSION_V3,
        "state_budget_bytes": STATE_BUDGET_BYTES,
        "jev_state_cap_bytes": JEV_STATE_CAP_BYTES,
        "state_bytes": size,
        "state_sha256": digest(encoded(state)),
        "sections": {key: _section(value) for key, value in sorted(state.items())},
        "field_map": dict(FIELD_MAP),
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
        "rationale": {
            "version": RATIONALE_VERSION,
            "bytes": rationale_size,
            "budget_bytes": RATIONALE_BUDGET_BYTES,
            "sha256": digest(encoded(state["rationale"])),
            "claim_count": len(block["claims"]),
            "cited_source_ids": cited_sources,
            "cited_bar_ids": cited_bars,
        },
        "omitted": omitted,
        "truncated": [],
    }
    return Dossier(state, manifest)
