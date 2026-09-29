"""``AGENT_RESEARCH_REPORT_V3``: a research agent's crypto picks for one scheduled run.

Owner decisions of 2026-09-26: crypto only; picks must be coins Alpaca can trade (the
research-context universe: active, tradable USD pairs without stablecoins); one run a day at
08:00 New York time (``research_schedule``); 20 picks per run (1-30 accepted). Jev reads each
pick exactly as the agent sent it and the independent system check comes after Jev's
selection, so intake validates format only: schema and types, positive prices, symbol in the
current universe, citations that resolve, byte budgets, the existing privacy and credential
screens and no agent identity in the reviewed text. There is no level-geometry, reward-to-
risk, stop-distance, price-grid or live-price check at intake.

Each pick is accepted or refused on its own, with its own code, and its siblings proceed, as
in V2. The envelope (identity, times, run slot, validity) is checked as a whole. This module
is pure: the caller supplies the schedule and, for the per-pick checks, the universe and the
clock. ``AGENT_RESEARCH_REPORT_V2`` and legacy bodies keep their exact contract in
``muse_reports``.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.muse_reports import (
    AgentIdentity,
    ResearchReportRejected,
    SelectionRationale,
    validation_errors,
)
from catalyst_lab.muse_reports import declared_agent as declared_agent_v2
from catalyst_lab.repository import json_safe
from catalyst_lab.research_evidence import (
    MAX_ERROR_DETAILS,
    rationale_reference_errors,
    screen_sensitive,
    sensitive_paths,
)
from catalyst_lab.review_storage import SourceExcerpt
from catalyst_lab.technical_evidence import LevelEvidence, ObservedBar, ObservedQuote

REPORT_SCHEMA_V3 = "AGENT_RESEARCH_REPORT_V3"
PICK_KINDS = ("NEWS", "CHART", "BOTH")
NEWS_KINDS = frozenset({"NEWS", "BOTH"})
CHART_KINDS = frozenset({"CHART", "BOTH"})
TARGET_PICKS = 20
MAX_PICKS = 30
MAX_SKIPPED = 200
MAX_VALIDITY = timedelta(hours=24)
MARKET = "CRYPTO"  # Report V3 is crypto only (owner decision 2026-09-26).
REVIEW_VALIDITY = "PACKET_EXPIRY"  # V3 reviews stay valid until the pick's packet expires.

# Item codes, in the order a pick is checked (the first failing check names the pick).
INVALID_ITEM = "INVALID_RESEARCH_ITEM"
PRICE_NOT_POSITIVE = "PRICE_NOT_POSITIVE"
SYMBOL_NOT_IN_UNIVERSE = "SYMBOL_NOT_IN_UNIVERSE"
NEWS_SOURCES_REQUIRED = "NEWS_SOURCES_REQUIRED"
TECHNICAL_EVIDENCE_REQUIRED = "TECHNICAL_EVIDENCE_REQUIRED"
CITATION_UNRESOLVED = "CITATION_UNRESOLVED"
PICK_VALIDITY_INVALID = "PICK_VALIDITY_INVALID"
AGENT_PRICE_TIME_INVALID = "AGENT_PRICE_TIME_INVALID"
# Raised by research_dossier_v3 while compiling the reviewed state.
INVALID_SOURCE_EVIDENCE = "INVALID_SOURCE_EVIDENCE"
FUTURE_TECHNICAL_EVIDENCE = "FUTURE_TECHNICAL_EVIDENCE"
AGENT_IDENTITY_IN_PICK = "AGENT_IDENTITY_IN_PICK"
DOSSIER_OVER_BUDGET = "DOSSIER_OVER_BUDGET"
PICK_CODES = (
    INVALID_ITEM, PRICE_NOT_POSITIVE, SYMBOL_NOT_IN_UNIVERSE, NEWS_SOURCES_REQUIRED,
    TECHNICAL_EVIDENCE_REQUIRED, CITATION_UNRESOLVED, PICK_VALIDITY_INVALID,
    AGENT_PRICE_TIME_INVALID, INVALID_SOURCE_EVIDENCE, FUTURE_TECHNICAL_EVIDENCE,
    AGENT_IDENTITY_IN_PICK, DOSSIER_OVER_BUDGET,
)
_SIGNAL = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9./_-]{0,31}$"


class ResearchCapabilityUnavailable(Exception):
    """A configured capability report V3 needs is missing or unreadable (HTTP 503).

    Not a ValueError: the service answers 503 with ``code`` and stores nothing, so the agent
    can retry the same report ID once the capability is back.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _positive(value):
    if value <= 0:
        raise ValueError(PRICE_NOT_POSITIVE)
    return value


Price = Annotated[
    Decimal,
    Field(allow_inf_nan=False, max_digits=30, decimal_places=18),
    AfterValidator(_positive),
]
Confidence = Annotated[
    Decimal, Field(ge=0, le=1, allow_inf_nan=False, max_digits=12, decimal_places=10)
]


def _typed(value):
    return SourceExcerpt.typed_timestamp(value)


class PickLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_trigger: Price
    max_entry_price: Price
    stop: Price
    target: Price


class PickReasoning(BaseModel):
    """The agent's reasoning, reviewed verbatim (``invalidation`` reaches Jev as the
    dossier's ``disproof``, the field the configured question sets already name)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    thesis: str = Field(min_length=1, max_length=1000)
    why_now: str = Field(min_length=1, max_length=600)
    why_these_levels: str = Field(min_length=1, max_length=600)
    risks: str = Field(min_length=1, max_length=600)
    invalidation: str = Field(min_length=1, max_length=400)


class PickTechnicalEvidence(BaseModel):
    """``MUSE_OBSERVED_TECHNICALS_V1`` observations; in V3 the bars are evidence for Jev.

    The same wire format as report V2's technical evidence, but ``level_references`` are
    optional and informational: each must name a retained bar, and no rule requires a level
    to equal a bar value.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["MUSE_OBSERVED_TECHNICALS_V1"]
    provider: str = Field(min_length=1, max_length=80)
    venue: str = Field(min_length=1, max_length=80)
    feed: str = Field(min_length=1, max_length=80)
    source_url: str = Field(pattern=r"^https://[^\s]+$", max_length=1000)
    retrieved_at: AwareDatetime
    timeframe_seconds: int = Field(ge=60, le=86400, strict=True)
    bars: list[ObservedBar] = Field(min_length=20, max_length=64)
    quote: ObservedQuote | None = None
    level_references: dict[
        Literal["entry_trigger", "max_entry_price", "stop", "target"], LevelEvidence
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete_observations(self):
        if len({bar.bar_id for bar in self.bars}) != len(self.bars):
            raise ValueError("DUPLICATE_OBSERVATION_ID")
        if any(a.started_at >= b.started_at
               for a, b in zip(self.bars, self.bars[1:], strict=False)):
            raise ValueError("OBSERVATION_ORDER_INVALID")
        if any(bar.started_at + timedelta(seconds=self.timeframe_seconds) > self.retrieved_at
               for bar in self.bars):
            raise ValueError("INCOMPLETE_OBSERVED_BAR")
        if self.quote and self.quote.observed_at > self.retrieved_at:
            raise ValueError("FUTURE_RESEARCH_QUOTE")
        if any(ref.bar_id not in {bar.bar_id for bar in self.bars}
               for ref in self.level_references.values()):
            raise ValueError("LEVEL_OBSERVATION_MISSING")
        return self


class AgentPick(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    signal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")
    symbol: str = Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)
    kind: Literal["NEWS", "CHART", "BOTH"]
    agent_current_price: Price
    agent_price_at: AwareDatetime
    levels: PickLevels
    stated_reward_risk: Price
    valid_until: AwareDatetime | None = None
    reasoning: PickReasoning
    selection_rationale: SelectionRationale
    sources: list[SourceExcerpt] = Field(default_factory=list, max_length=8)
    technical_evidence: PickTechnicalEvidence | None = None
    agent_confidence: Confidence  # Analytics only: never reviewed, never a threshold.

    @field_validator("agent_price_at", "valid_until", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return _typed(value)

    @model_validator(mode="after")
    def bounded_sources(self):
        """V2's source rules (unique IDs, 8,000 excerpt characters) and privacy screen."""
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("DUPLICATE_SOURCE_ID")
        if sum(len(source.excerpt) for source in self.sources) > 8000:
            raise ValueError("EXCERPT_BUDGET_EXCEEDED")
        screen_sensitive(encoded(self.model_dump(mode="json")))
        return self


class SkippedCoin(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)
    reason: str = Field(min_length=1, max_length=200)


class _DeclaredAgentV3(BaseModel):
    schema_version: Literal["AGENT_RESEARCH_REPORT_V3"]
    agent: AgentIdentity | None = Field(default=None, validate_default=True)

    @field_validator("agent")
    @classmethod
    def agent_required(cls, value):
        if value is None:
            raise ValueError("AGENT_IDENTITY_REQUIRED")  # As for V2: a V3 body names its agent.
        return value


class ReportEnvelopeV3(_DeclaredAgentV3):
    model_config = ConfigDict(extra="forbid")
    report_id: UUID
    generated_at: AwareDatetime
    valid_until: AwareDatetime
    run_slot: AwareDatetime
    context_as_of: AwareDatetime
    picks: list[Any] = Field(min_length=1, max_length=MAX_PICKS)
    skipped: list[SkippedCoin] = Field(max_length=MAX_SKIPPED)

    @field_validator("generated_at", "valid_until", "run_slot", "context_as_of", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return _typed(value)


class _IdentityV3(_DeclaredAgentV3):
    """Only the identity fields of a V3 body, for the credential check before intake."""

    model_config = ConfigDict(extra="ignore")


def is_v3(raw):
    return isinstance(raw, dict) and raw.get("schema_version") == REPORT_SCHEMA_V3


def declared_agent(raw):
    """The validated agent block of any report body (V3 here; V2 and legacy unchanged).

    A V3 body always names its agent; the HTTP layer compares it with the credential before
    intake, exactly as for V2.
    """
    if not is_v3(raw):
        return declared_agent_v2(raw)
    try:
        return _IdentityV3.model_validate(raw).agent.model_dump(mode="json")
    except ValidationError as exc:
        raise ResearchReportRejected("INVALID_MUSE_REPORT", errors=validation_errors(exc)) from None


@dataclass(frozen=True)
class PickIntake:
    """One submitted pick after the checks made so far; ``code`` is its first failure."""

    index: int
    signal_id: str | None
    item_sha256: str
    pick: AgentPick | None
    canonical: dict
    code: str | None = None
    errors: tuple = ()

    @property
    def path(self):
        return f"picks[{self.index}]"

    def rejected(self, code, errors):
        return replace(self, code=code, errors=tuple(errors)[:MAX_ERROR_DETAILS])


@dataclass(frozen=True)
class ReportIntakeV3:
    report_id: str
    generated_at: datetime
    valid_until: datetime
    run_slot: datetime
    context_as_of: datetime
    agent: dict
    picks: tuple
    skipped: tuple
    canonical: dict
    report_hash: str
    schedule: dict


@dataclass(frozen=True)
class UniverseSnapshot:
    """The tradable symbols a pick must come from, with where and when they were read."""

    symbols: frozenset
    fetched_at: datetime
    source: str


@dataclass(frozen=True)
class V3Intake:
    """What report-V3 intake needs from the application: the schedule and the universe.

    ``universe`` is called only for a V3 body that passed its envelope checks, so V2 and
    legacy reports never cause a broker read.
    """

    schedule: Any
    universe: Any

    def snapshot(self):
        snapshot = self.universe()
        if not isinstance(snapshot, UniverseSnapshot):
            raise ResearchCapabilityUnavailable("RESEARCH_UNIVERSE_UNAVAILABLE")
        return snapshot


def _parse_pick(index, value):
    prefix = f"picks[{index}]"
    raw_signal = value.get("signal_id") if isinstance(value, dict) else None
    signal = raw_signal.strip() if isinstance(raw_signal, str) else None
    signal = signal if signal and _SIGNAL.fullmatch(signal) else None
    item_sha256 = digest(encoded(json_safe(value)))
    try:
        pick = AgentPick.model_validate(value)
    except ValidationError as exc:
        errors = validation_errors(exc, prefix)
        if any(e["code"] == "SENSITIVE_EVIDENCE_REJECTED" for e in errors):
            raise ResearchReportRejected("SENSITIVE_EVIDENCE_REJECTED", errors=errors) from None
        # A pick whose only schema errors are non-positive prices has that code of its own.
        code = PRICE_NOT_POSITIVE if errors and all(
            e["code"] == PRICE_NOT_POSITIVE for e in errors
        ) else INVALID_ITEM
        # Only a hash of content that failed validation is retained.
        return PickIntake(
            index, signal, item_sha256, None, {"invalid_item_sha256": item_sha256}
        ).rejected(code, errors)
    return PickIntake(index, pick.signal_id, item_sha256, pick, pick.model_dump(mode="json"))


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


def _raw_symbol(item, raw):
    if item.pick is not None:
        return item.pick.symbol
    symbol = raw.get("symbol") if isinstance(raw, dict) else None
    return symbol.strip() if isinstance(symbol, str) else None


def envelope_errors(envelope, schedule):
    """Report-level time and schedule rules; each failure names its field and code."""
    errors = []
    generated, valid_until = envelope.generated_at, envelope.valid_until
    if valid_until <= generated:
        errors.append({"path": "valid_until", "code": "INVALID_REPORT_EXPIRY"})
    elif valid_until - generated > MAX_VALIDITY:
        errors.append({"path": "valid_until", "code": "REPORT_VALIDITY_OVER_24_HOURS"})
    if envelope.context_as_of > generated:
        errors.append({"path": "context_as_of", "code": "CONTEXT_AFTER_REPORT"})
    if not schedule.is_slot(envelope.run_slot):
        errors.append({"path": "run_slot", "code": "RUN_SLOT_NOT_SCHEDULED"})
    else:
        if generated < envelope.run_slot - schedule.grace:
            # A report may be prepared at most the grace before the run it answers.
            errors.append({"path": "run_slot", "code": "RUN_SLOT_IN_FUTURE"})
        if valid_until > schedule.validity_limit(envelope.run_slot):
            errors.append({"path": "valid_until", "code": "REPORT_VALIDITY_AFTER_NEXT_RUN"})
    return errors


def parse_report_v3(raw, *, schedule):
    """Validate a V3 body without clock, ledger or broker access.

    Raises ResearchCapabilityUnavailable without a schedule, and ResearchReportRejected for
    problems that refuse the whole report; otherwise returns every pick with the result of
    its schema check. The report hash depends on submitted content only.
    """
    if schedule is None:
        raise ResearchCapabilityUnavailable("RESEARCH_SCHEDULE_NOT_CONFIGURED")
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
        envelope = ReportEnvelopeV3.model_validate(raw)
    except ValidationError as exc:
        raise ResearchReportRejected("INVALID_MUSE_REPORT", errors=validation_errors(exc)) from None
    errors = envelope_errors(envelope, schedule)
    if errors:
        raise ResearchReportRejected("INVALID_MUSE_REPORT", errors=errors)
    picks = tuple(_parse_pick(index, value) for index, value in enumerate(envelope.picks))
    _duplicates(picks, lambda item: item.signal_id, "DUPLICATE_SIGNAL_IN_REPORT", ".signal_id")
    symbols = [_raw_symbol(item, value) for item, value in zip(picks, envelope.picks,
                                                                 strict=True)]
    _duplicates(picks, lambda item: symbols[item.index], "DUPLICATE_SYMBOL_IN_REPORT",
                ".symbol")
    skipped_errors, seen = [], set()
    for index, coin in enumerate(envelope.skipped):
        if coin.symbol in seen:
            skipped_errors.append({"path": f"skipped[{index}].symbol",
                                   "code": "DUPLICATE_SKIPPED_SYMBOL"})
        elif coin.symbol in symbols:
            skipped_errors.append({"path": f"skipped[{index}].symbol",
                                   "code": "SKIPPED_SYMBOL_ALSO_PICKED"})
        seen.add(coin.symbol)
    if skipped_errors:
        raise ResearchReportRejected("INVALID_MUSE_REPORT", errors=skipped_errors)
    canonical = envelope.model_dump(mode="json", exclude={"picks"})
    canonical["picks"] = [item.canonical for item in picks]
    return ReportIntakeV3(
        report_id=str(envelope.report_id),
        generated_at=envelope.generated_at,
        valid_until=envelope.valid_until,
        run_slot=envelope.run_slot,
        context_as_of=envelope.context_as_of,
        agent=canonical["agent"],
        picks=picks,
        skipped=tuple(coin.model_dump(mode="json") for coin in envelope.skipped),
        canonical=canonical,
        report_hash=digest(encoded(canonical)),
        schedule=schedule.as_dict(),
    )


def check_pick(item, *, intake, universe, expires_at, now):
    """The format checks after the schema, in order; the first failure names the pick.

    ``expires_at`` is the report's effective expiry; the pick's own packet expiry is its
    ``valid_until`` (or the report's) bounded by that. Returns the pick and its packet
    expiry (None when rejected).
    """
    if item.code is not None:
        return item, None
    pick, prefix = item.pick, item.path
    if pick.symbol not in universe.symbols:
        return item.rejected(
            SYMBOL_NOT_IN_UNIVERSE, [{"path": prefix + ".symbol", "code": SYMBOL_NOT_IN_UNIVERSE}]
        ), None
    missing = []
    if pick.kind in NEWS_KINDS and not pick.sources:
        missing.append({"path": prefix + ".sources", "code": NEWS_SOURCES_REQUIRED})
    if pick.kind in CHART_KINDS and pick.technical_evidence is None:
        missing.append({"path": prefix + ".technical_evidence",
                        "code": TECHNICAL_EVIDENCE_REQUIRED})
    if missing:
        return item.rejected(missing[0]["code"], missing), None
    technical = pick.technical_evidence
    errors = rationale_reference_errors(
        item.canonical["selection_rationale"],
        source_ids=[source.source_id for source in pick.sources],
        bar_ids=[bar.bar_id for bar in technical.bars] if technical else [],
        prefix=prefix + ".selection_rationale",
    )
    if errors:
        return item.rejected(CITATION_UNRESOLVED, errors), None
    if pick.valid_until is not None and not (
        intake.generated_at < pick.valid_until <= intake.valid_until
    ):
        code = ("PICK_VALID_UNTIL_AFTER_REPORT" if pick.valid_until > intake.valid_until
                else "PICK_VALID_UNTIL_NOT_AFTER_GENERATED")
        return item.rejected(
            PICK_VALIDITY_INVALID, [{"path": prefix + ".valid_until", "code": code}]
        ), None
    pick_expiry = min(pick.valid_until or intake.valid_until, expires_at)
    if pick_expiry <= now:
        return item.rejected(PICK_VALIDITY_INVALID, [
            {"path": prefix + ".valid_until", "code": "PICK_EXPIRED_AT_INTAKE"}
        ]), None
    if pick.agent_price_at > intake.generated_at:
        return item.rejected(AGENT_PRICE_TIME_INVALID, [
            {"path": prefix + ".agent_price_at", "code": "AGENT_PRICE_AFTER_REPORT"}
        ]), None
    return item, pick_expiry
