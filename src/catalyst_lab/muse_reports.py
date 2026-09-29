"""External research report contract. No market discovery, broker or model calls.

Intake validates the report envelope as a whole and every item on its own, so one
malformed item is recorded as rejected without discarding its siblings. An invalid
envelope or credential-like content anywhere still refuses the whole report before
anything is stored. Error details carry field paths and codes, never submitted values.

``AGENT_RESEARCH_REPORT_V2`` bodies carry a required ``agent`` block naming the proposing
agent; unversioned legacy bodies may not carry one and stay LEGACY_UNATTRIBUTED. The
block is envelope metadata: it never reaches an item's review dossier.
"""

import re
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from catalyst_lab.agent_identity import (
    AGENT_ID_PATTERN,
    AGENT_VERSION_PATTERN,
    GUIDELINES_VERSION_PATTERN,
    REPORT_SCHEMA_V2,
    SHA256_PATTERN,
)
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.repository import json_safe
from catalyst_lab.research_evidence import (
    MAX_ERROR_DETAILS,
    field_path,
    rationale_reference_errors,
    screen_sensitive,
    sensitive_paths,
)
from catalyst_lab.research_reports import ResearchItem
from catalyst_lab.review_storage import SourceExcerpt
from catalyst_lab.technical_evidence import TechnicalEvidence

RATIONALE_VERSION = "AGENT_SELECTION_RATIONALE_V1"
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,79}")
_SIGNAL = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")

ClaimId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
SourceRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
BarRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")]
RiskText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ClaimSupport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ids: list[SourceRef] = Field(default_factory=list, max_length=8)
    bar_ids: list[BarRef] = Field(default_factory=list, max_length=8)


class RationaleClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim_id: ClaimId
    kind: Literal["CATALYST", "NOVELTY", "ECONOMIC_LINK", "TECHNICAL", "RISK"]
    text: str = Field(min_length=1, max_length=300)
    supported_by: ClaimSupport


class AgentConfidence(BaseModel):
    """Recorded for calibration analytics only; never a review input or code threshold."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    level: Literal["LOW", "MEDIUM", "HIGH"]
    basis: str = Field(min_length=1, max_length=200)


class SelectionRationale(BaseModel):
    """AGENT_SELECTION_RATIONALE_V1: why this pick, why now, why these levels, why over peers.

    Each claim must cite sources or bars retained in the same item (checked at intake).
    The reviewer treats the claims as unverified and checks them against the citations.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claims: list[RationaleClaim] = Field(min_length=1, max_length=8)
    why_now: str = Field(min_length=1, max_length=500)
    why_these_levels: str = Field(min_length=1, max_length=500)
    why_over_peers: str = Field(min_length=1, max_length=500)
    what_would_change_my_mind: str = Field(min_length=1, max_length=300)
    known_risks: list[RiskText] = Field(max_length=5)
    agent_confidence: AgentConfidence

    @model_validator(mode="after")
    def distinct_claims(self):
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ValueError("DUPLICATE_CLAIM_ID")
        return self


class MuseContender(ResearchItem):
    market: Literal["US_STOCKS", "CRYPTO", "INDIA"]
    technical_analysis: str = Field(min_length=1, max_length=2000)
    technical_evidence: TechnicalEvidence | None = None
    selection_rationale: SelectionRationale | None = None

    @model_validator(mode="after")
    def bounded_sources(self):
        """Replaces ResearchItem's check: same source rules and privacy patterns, no byte cap.

        What reaches the reviewer is bounded by the review dossier budget
        (research_dossier); retained observations beyond it are stored, not sent.
        """
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("DUPLICATE_SOURCE_ID")
        if sum(len(source.excerpt) for source in self.sources) > 8000:
            raise ValueError("EXCERPT_BUDGET_EXCEEDED")
        screen_sensitive(encoded(self.model_dump(mode="json")))
        return self


class AgentIdentity(BaseModel):
    """The proposing agent of an AGENT_RESEARCH_REPORT_V2 body (plan 1.3).

    ``agent_id`` must equal the agent of the submitting credential (checked by the HTTP
    layer); version and guideline fields are declared by the agent and recorded as is.
    """

    model_config = ConfigDict(extra="forbid")
    agent_id: Annotated[str, StringConstraints(pattern=AGENT_ID_PATTERN)]
    agent_version: Annotated[str, StringConstraints(pattern=AGENT_VERSION_PATTERN)]
    guidelines_version: Annotated[str, StringConstraints(pattern=GUIDELINES_VERSION_PATTERN)]
    guidelines_sha256: Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
    run_id: UUID


class _DeclaredIdentity(BaseModel):
    schema_version: Literal["AGENT_RESEARCH_REPORT_V2"] | None = None
    agent: AgentIdentity | None = Field(default=None, validate_default=True)

    @field_validator("agent")
    @classmethod
    def agent_matches_schema(cls, value, info):
        if "schema_version" not in info.data:
            return value  # The schema version itself is already reported as invalid.
        if info.data["schema_version"] == REPORT_SCHEMA_V2 and value is None:
            raise ValueError("AGENT_IDENTITY_REQUIRED")
        if info.data["schema_version"] is None and value is not None:
            raise ValueError("AGENT_IDENTITY_REQUIRES_V2")
        return value


class ReportIdentity(_DeclaredIdentity):
    """Only the identity fields of a report, for the credential check before intake."""

    model_config = ConfigDict(extra="ignore")


class ReportEnvelope(_DeclaredIdentity):
    """Report-level fields; each item is validated separately by ``parse_report``."""

    model_config = ConfigDict(extra="forbid")
    report_id: UUID
    generated_at: AwareDatetime
    valid_until: AwareDatetime
    items: list[Any] = Field(min_length=1, max_length=30)

    @field_validator("generated_at", "valid_until", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return SourceExcerpt.typed_timestamp(value)

    @model_validator(mode="after")
    def bounded_report(self):
        if self.generated_at >= self.valid_until:
            raise ValueError("INVALID_REPORT_EXPIRY")
        return self


class ResearchReportRejected(ValueError):
    """Whole-report refusal with nothing stored; ``str()`` is the code, never input text."""

    def __init__(self, code, *, errors=(), item_results=()):
        super().__init__(code)
        self.code = code
        self.errors = tuple(errors)[:MAX_ERROR_DETAILS]
        self.item_results = tuple(item_results)

    def detail(self):
        body = {"detail": self.code}
        if self.errors:
            body["errors"] = list(self.errors)
        if self.item_results:
            body["item_results"] = list(self.item_results)
        return body


@dataclass(frozen=True)
class ItemIntake:
    """One submitted item after schema and reference checks (no clock or ledger reads)."""

    index: int
    signal_id: str | None
    item_sha256: str
    contender: MuseContender | None
    canonical: dict
    code: str | None = None
    errors: tuple = ()

    @property
    def path(self):
        return f"items[{self.index}]"

    def rejected(self, code, errors):
        return replace(self, code=code, errors=tuple(errors)[:MAX_ERROR_DETAILS])


@dataclass(frozen=True)
class ReportIntake:
    report_id: str
    schema_version: str | None
    generated_at: Any
    valid_until: Any
    items: tuple
    canonical: dict
    report_hash: str
    agent: dict | None = None  # Canonical agent block of a V2 body; None for legacy.


def validation_errors(exc, prefix=""):
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        inner = (error.get("ctx") or {}).get("error")
        kind = str(error.get("type", "")).upper()
        if isinstance(inner, ValueError) and _CODE.fullmatch(str(inner)):
            code = str(inner)  # This module's validators raise constant codes only.
        else:
            code = kind if _CODE.fullmatch(kind) else "INVALID_VALUE"
        details.append({"path": field_path(prefix, error.get("loc", ())), "code": code})
        if len(details) == MAX_ERROR_DETAILS:
            break
    return details


def _parse_item(index, value, schema_version):
    prefix = f"items[{index}]"
    raw_signal = value.get("signal_id") if isinstance(value, dict) else None
    signal = raw_signal.strip() if isinstance(raw_signal, str) else None
    signal = signal if signal and _SIGNAL.fullmatch(signal) else None
    item_sha256 = digest(encoded(json_safe(value)))
    try:
        contender = MuseContender.model_validate(value)
    except ValidationError as exc:
        errors = validation_errors(exc, prefix)
        if any(e["code"] == "SENSITIVE_EVIDENCE_REJECTED" for e in errors):
            raise ResearchReportRejected("SENSITIVE_EVIDENCE_REJECTED", errors=errors) from None
        # Only a hash of content that failed validation is retained.
        return ItemIntake(
            index, signal, item_sha256, None, {"invalid_item_sha256": item_sha256}
        ).rejected("INVALID_RESEARCH_ITEM", errors)
    canonical = contender.model_dump(mode="json")
    # Absent optional extensions keep the pre-extension canonical form, so replays of
    # reports accepted before these fields existed still hash identically.
    for key in ("technical_evidence", "selection_rationale"):
        if canonical.get(key) is None:
            canonical.pop(key, None)
    item = ItemIntake(index, contender.signal_id, item_sha256, contender, canonical)
    rationale = canonical.get("selection_rationale")
    if rationale is None:
        if schema_version == REPORT_SCHEMA_V2:
            return item.rejected(
                "SELECTION_RATIONALE_REQUIRED",
                [{"path": prefix + ".selection_rationale", "code": "SELECTION_RATIONALE_REQUIRED"}],
            )
        return item  # Legacy body: stored with selection_rationale null, never synthesized.
    technical = contender.technical_evidence
    errors = rationale_reference_errors(
        rationale,
        source_ids=[source.source_id for source in contender.sources],
        bar_ids=[bar.bar_id for bar in technical.bars] if technical else [],
        prefix=prefix + ".selection_rationale",
    )
    return item.rejected("SELECTION_RATIONALE_INVALID", errors) if errors else item


def _duplicates(items, key, code, path):
    seen, errors = set(), []
    for item in items:
        value = key(item)
        if value is None:
            continue
        if value in seen:
            errors.append({"path": item.path + path, "code": code})
        seen.add(value)
    if errors:
        raise ResearchReportRejected(code, errors=errors)


def _market_symbol(item, raw):
    if item.contender is not None:
        return item.contender.market, item.contender.symbol
    if not isinstance(raw, dict):
        return None
    market, symbol = raw.get("market"), raw.get("symbol")
    if isinstance(market, str) and isinstance(symbol, str):
        return market, symbol.strip()
    return None


def parse_report(raw):
    """Validate a submitted report without clock or ledger access.

    Raises ResearchReportRejected for problems that refuse the whole report; returns
    per-item outcomes otherwise. The report hash depends on submitted content only.
    """
    if not isinstance(raw, dict):
        raise ResearchReportRejected("INVALID_MUSE_REPORT")
    try:
        plain = json_safe(raw)
        encoded(plain)  # Strict JSON only (no NaN or infinity) before any screening.
    except (TypeError, ValueError):
        raise ResearchReportRejected("INVALID_MUSE_REPORT") from None
    flagged = sensitive_paths(plain)
    if flagged:
        raise ResearchReportRejected(
            "SENSITIVE_EVIDENCE_REJECTED",
            errors=[{"path": path, "code": "SENSITIVE_EVIDENCE_REJECTED"} for path in flagged],
        )
    try:
        envelope = ReportEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise ResearchReportRejected("INVALID_MUSE_REPORT", errors=validation_errors(exc)) from None
    items = tuple(
        _parse_item(index, value, envelope.schema_version)
        for index, value in enumerate(envelope.items)
    )
    _duplicates(items, lambda item: item.signal_id, "DUPLICATE_SIGNAL_IN_REPORT", ".signal_id")
    pairs = [_market_symbol(item, value) for item, value in zip(items, envelope.items, strict=True)]
    _duplicates(items, lambda item: pairs[item.index], "DUPLICATE_SYMBOL_IN_REPORT", ".symbol")
    canonical = envelope.model_dump(mode="json", exclude={"items"})
    # Legacy bodies keep their pre-V2 canonical form, so recorded report hashes still replay.
    for key in ("schema_version", "agent"):
        if canonical[key] is None:
            canonical.pop(key)
    canonical["items"] = [item.canonical for item in items]
    return ReportIntake(
        report_id=str(envelope.report_id),
        schema_version=envelope.schema_version,
        generated_at=envelope.generated_at,
        valid_until=envelope.valid_until,
        items=items,
        canonical=canonical,
        report_hash=digest(encoded(canonical)),
        agent=canonical.get("agent"),
    )


def declared_agent(raw):
    """The validated agent block of a report, or None for an unversioned legacy body.

    Checks only ``schema_version`` and ``agent`` (no clock, ledger or item work), so the
    HTTP layer can compare the declared agent with the authenticated credential before
    intake; ``parse_report`` validates the same fields again with the whole report.
    """
    if not isinstance(raw, dict):
        raise ResearchReportRejected("INVALID_MUSE_REPORT")
    try:
        identity = ReportIdentity.model_validate(raw)
    except ValidationError as exc:
        raise ResearchReportRejected("INVALID_MUSE_REPORT", errors=validation_errors(exc)) from None
    return None if identity.agent is None else identity.agent.model_dump(mode="json")
