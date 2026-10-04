"""``MARKET_OUTLOOK_V1`` and ``POST_MORTEM_V1``: a research agent's learning records (package
learning-app, 2026-09-28; plan ``docs/LEARNING-LOOP-PLAN.md`` sections 5b and 5c).

**Paper trading only.** Both records are research-side: nothing here places or changes an order,
changes a trading rule, or reaches Jev (Jev's dossiers and questions are unchanged). Each accepted
body is one immutable ``lab.managed_events`` row with no setup (``setup_id`` null), appended
through the audited append; there is no migration.

* ``MARKET_OUTLOOK_V1`` (``POST /api/v1/lab/market-outlooks``): the morning test of plan 5c. The
  agent's expected direction and confidence for every coin of the tradable universe (or SKIPPED
  with a reason), its Bitcoin and Ether calls and a short market view with cited factors and
  events. The nightly ``MARKET_REALITY_V1`` grades it on its forward window ``[received_at,
  received_at + 24 h)`` once that window has fully elapsed (``market_reality``).
* ``POST_MORTEM_V1`` (``POST /api/v1/lab/post-mortems``): a batch of up to 30 cited causes, each
  about one of the agent's own closed trades or one mover of a recorded reality day, with whether
  the cause was public before the move started (plan 5b and 5c).

Checks follow report V3's: strict schema, the report source rules, the credential/e-mail/0x
screen (whole body refused), the agent-identity screen (the agent ID in agent-written text), the
report freshness rule for ``generated_at`` and, for outlooks, the scheduled ``run_slot`` and the
current tradable universe. An identical retry under the same ID returns the stored receipt without
re-reading the clock, the universe or the ledger rows it checked; a different body under the same
ID is refused. Errors name codes and field paths only, never submitted values.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from catalyst_lab.agent_identity import agent_fields
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.market import NY
from catalyst_lab.muse_reports import AgentIdentity, ResearchReportRejected, validation_errors
from catalyst_lab.repository import json_safe
from catalyst_lab.research_evidence import MAX_ERROR_DETAILS, canonical_source, sensitive_paths
from catalyst_lab.research_report_v3 import (
    MAX_PICKS,
    MAX_SKIPPED,
    SYMBOL_PATTERN,
    ResearchCapabilityUnavailable,
)
from catalyst_lab.review_storage import SourceExcerpt

OUTLOOK_SCHEMA = "MARKET_OUTLOOK_V1"
POST_MORTEM_SCHEMA = "POST_MORTEM_V1"
OUTLOOK_EVENT = "MARKET_OUTLOOK"
POST_MORTEM_EVENT = "POST_MORTEM"
REALITY_EVENT = "MARKET_REALITY"  # market_reality.REALITY_EVENT (kept here: no import cycle).
# Every learning record kind. The event feed ``GET /api/v1/lab/outputs`` leaves them out for
# research-agent credentials (owner decision of 2026-09-28: an agent sees only its own sanitized
# lessons, never another agent's outlooks, notes, grades or lines, the arms or the costs);
# tests/test_learning_intake.py pins each name to its module's constant.
LEARNING_EVENT_KINDS = frozenset({
    OUTLOOK_EVENT, POST_MORTEM_EVENT, REALITY_EVENT, "DAILY_SCORECARD", "WEEKLY_REVIEW",
    "UNCHANGED_PLAN_REPLAY", "DAY_REVIEW_DECISION_REPLAY",
    # Package learning-loop2 (2026-10-03): the daily brief (it carries the account's picks and
    # trades; agents read its sanitized view in their lessons), the missed-tradeable records and
    # the per-trade path and decision-context records.
    "DAILY_BRIEF", "MISSED_TRADEABLE", "AFTER_EXIT_PATH", "MANAGEMENT_CHANGE_CONTEXT",
})
HORIZON_HOURS = 24
# An outlook must list every coin of the universe, so its cap may never sit below the largest
# universe the research context can serve. The context has no bound of its own; the most coins
# one report can name (30 picks plus 200 skipped) is the bound the research run already works
# to, so the outlook takes exactly that (tests/test_learning_intake.py pins it).
MAX_OUTLOOK_COINS = MAX_PICKS + MAX_SKIPPED
MAX_POST_MORTEM_ITEMS = 30
OUTLOOK_BODY_LIMIT = 2097152  # 230 coins with four cited reasons each fit.
POST_MORTEM_BODY_LIMIT = 524288
DIRECTIONS = ("UP", "DOWN", "FLAT")
COIN_DIRECTIONS = (*DIRECTIONS, "SKIPPED")
REASON_KINDS = ("NEWS", "EVENT", "TECHNICAL", "FUNDAMENTAL", "MARKET")
CAUSES = ("COIN_NEWS", "MARKET_WIDE", "NO_NEWS", "SURPRISE")
SOURCED_CAUSES = frozenset({"COIN_NEWS", "MARKET_WIDE"})
# Agent-written text the identity screen reads (report V3's rule); excerpts and URLs are
# third-party text, and symbols, enumerations and times are not prose.
IDENTITY_SCREENED = frozenset({
    "summary", "name", "note", "what", "skip_reason", "text", "source_id", "pre_move_technicals",
})
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

Confidence = Annotated[
    Decimal, Field(ge=0, le=1, allow_inf_nan=False, max_digits=12, decimal_places=10)
]
MovePct = Annotated[
    Decimal, Field(ge=0, le=100, allow_inf_nan=False, max_digits=8, decimal_places=4)
]


class LearningRecordRejected(ResearchReportRejected):
    """A refused learning record, nothing stored; answered 422 like a refused report."""


def _typed(value):
    return SourceExcerpt.typed_timestamp(value)


# --- MARKET_OUTLOOK_V1 -----------------------------------------------------------------------


class MarketCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["UP", "DOWN", "FLAT"]
    confidence: Confidence


class Factor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=300)
    source: SourceExcerpt | None = None


class MarketEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    at: AwareDatetime | None
    what: str = Field(min_length=1, max_length=300)
    source: SourceExcerpt | None = None

    @field_validator("at", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return _typed(value)


class MarketView(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=600)
    btc: MarketCall
    eth: MarketCall
    factors: list[Factor] = Field(max_length=12)
    events: list[MarketEvent] = Field(max_length=20)


class CoinReason(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["NEWS", "EVENT", "TECHNICAL", "FUNDAMENTAL", "MARKET"]
    text: str = Field(min_length=1, max_length=200)
    source: SourceExcerpt | None = None


class CoinOutlook(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)
    direction: Literal["UP", "DOWN", "FLAT", "SKIPPED"]
    confidence: Confidence | None = None
    skip_reason: str | None = Field(default=None, min_length=1, max_length=200)
    expected_move_pct: MovePct | None = None
    reasons: list[CoinReason] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def skipped_or_called(self):
        if self.direction == "SKIPPED":
            if self.skip_reason is None:
                raise ValueError("SKIP_REASON_REQUIRED")
            if (self.confidence is not None or self.expected_move_pct is not None
                    or self.reasons):
                raise ValueError("SKIPPED_COIN_FIELDS_NOT_ALLOWED")
        else:
            if self.confidence is None:
                raise ValueError("CONFIDENCE_REQUIRED")
            if self.skip_reason is not None:
                raise ValueError("SKIP_REASON_NOT_ALLOWED")
        return self


class MarketOutlook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["MARKET_OUTLOOK_V1"]
    outlook_id: UUID
    generated_at: AwareDatetime
    run_slot: AwareDatetime
    horizon_hours: Literal[24]
    agent: AgentIdentity
    market: MarketView
    coins: list[CoinOutlook] = Field(max_length=MAX_OUTLOOK_COINS)

    @field_validator("generated_at", "run_slot", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return _typed(value)


# --- POST_MORTEM_V1 -------------------------------------------------------------------------


class TradeSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["TRADE"]
    setup_id: UUID


class MoverSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["MOVER"]
    symbol: str = Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)
    day: str

    @field_validator("day")
    @classmethod
    def new_york_date(cls, value):
        if not isinstance(value, str) or not _DAY.fullmatch(value):
            raise ValueError("DAY_FORMAT_REQUIRED")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("DAY_FORMAT_REQUIRED") from None
        return value


class PostMortemItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: Annotated[TradeSubject | MoverSubject, Field(discriminator="kind")]
    cause: Literal["COIN_NEWS", "MARKET_WIDE", "NO_NEWS", "SURPRISE"]
    knowable_before_move: StrictBool | None
    summary: str = Field(min_length=1, max_length=400)
    sources: list[SourceExcerpt] = Field(default_factory=list, max_length=4)
    pre_move_technicals: str = Field(min_length=1, max_length=300)


class PostMortemEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["POST_MORTEM_V1"]
    note_id: UUID
    generated_at: AwareDatetime
    agent: AgentIdentity
    items: list[Any] = Field(min_length=1, max_length=MAX_POST_MORTEM_ITEMS)

    @field_validator("generated_at", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return _typed(value)


# --- Shared helpers -------------------------------------------------------------------------


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return value.astimezone(UTC)


def identity_paths(data, agent_id, prefix=""):
    """Paths of agent-written strings naming the agent (whole word, any case): report V3's rule
    over the learning records' prose fields (``IDENTITY_SCREENED``); the ``agent`` block itself,
    excerpts and URLs are never read."""
    if not agent_id:
        return []
    word = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(agent_id) + r"(?![A-Za-z0-9_])",
                      re.IGNORECASE)
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "agent":
                    continue
                child_path = f"{path}.{key}" if path else key
                if key in IDENTITY_SCREENED and isinstance(child, str):
                    if word.search(child):
                        found.append(child_path)
                else:
                    walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(data, prefix)
    return found[:MAX_ERROR_DETAILS]


def _screen_sensitive(raw):
    try:
        plain = json_safe(raw)
        encoded(plain)  # Strict JSON only before any screening.
    except (TypeError, ValueError):
        return None
    flagged = sensitive_paths(plain)
    if flagged:
        raise LearningRecordRejected(
            "SENSITIVE_EVIDENCE_REJECTED",
            errors=[{"path": path, "code": "SENSITIVE_EVIDENCE_REJECTED"} for path in flagged],
        )
    return plain


def _canonical_sources(data, now):
    """Every ``source`` / ``sources`` object in ``data`` in its stored form (``content_hash``,
    UTC times); ``INVALID_SOURCE_EVIDENCE`` with the paths of any retrieved after ``now``."""
    errors = []

    def one(value, path):
        try:
            return canonical_source(value, now=now)
        except ValueError as exc:
            if str(exc) == "SENSITIVE_EVIDENCE_REJECTED":
                raise LearningRecordRejected("SENSITIVE_EVIDENCE_REJECTED", errors=[
                    {"path": path, "code": "SENSITIVE_EVIDENCE_REJECTED"}]) from None
            errors.append({"path": path, "code": "INVALID_SOURCE_EVIDENCE"})
            return None

    def walk(node, path):
        if isinstance(node, dict):
            result = {}
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else key
                if key == "source" and isinstance(child, dict):
                    result[key] = one(child, child_path)
                elif key == "sources" and isinstance(child, list):
                    result[key] = [one(s, f"{child_path}[{i}]") for i, s in enumerate(child)]
                else:
                    result[key] = walk(child, child_path)
            return result
        if isinstance(node, list):
            return [walk(child, f"{path}[{i}]") for i, child in enumerate(node)]
        return node

    canonical = walk(data, "")
    return canonical, errors[:MAX_ERROR_DETAILS]


def _fresh(now, generated_at, max_age_seconds, code):
    if not 0 <= (now - _utc(generated_at)).total_seconds() <= max_age_seconds:
        raise LearningRecordRejected(code)


def day_bounds(day):
    """``[start, end)`` of a New York calendar day, in UTC (23 or 25 hours on a DST change)."""
    start = datetime(day.year, day.month, day.day, tzinfo=NY)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=NY)
    return start.astimezone(UTC), end.astimezone(UTC)


def grading_day(window_end):
    """The New York day whose ``MARKET_REALITY_V1`` grades an outlook: the day whose end is the
    first day end at or after ``window_end`` (the first nightly run after the window)."""
    local = _utc(window_end).astimezone(NY)
    day = local.date()
    start, _ = day_bounds(day)
    # A window ending exactly at a New York midnight has fully elapsed at the previous day's end.
    return day - timedelta(days=1) if _utc(window_end) == start else day


def _agent_key(agent):
    return agent["agent_id"]


# --- The service ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningPolicy:
    """What intake needs from the application: the report freshness bound and the schedule."""

    max_age_seconds: float
    schedule: Any = None


class LearningIntake:
    """Records outlooks and post-mortems for the research-agent routes.

    ``store`` is the managed store (``catalyst_risk``: it appends ``lab.managed_events``);
    ``universe`` a zero-argument callable returning the report intake's
    ``research_report_v3.UniverseSnapshot`` (the research context's cached, budgeted read) or
    raising ``ResearchCapabilityUnavailable``; ``policy`` the report freshness bound and schedule.
    """

    def __init__(self, store, *, clock, policy, universe=None):
        if not isinstance(policy, LearningPolicy) or isinstance(policy.max_age_seconds, bool) \
                or not isinstance(policy.max_age_seconds, int | float) \
                or not 0 < policy.max_age_seconds <= 86400:
            raise ValueError("LEARNING_INTAKE_POLICY_INVALID")
        self.store, self.clock, self.policy, self.universe = store, clock, policy, universe

    # --- MARKET_OUTLOOK_V1 -------------------------------------------------------------------

    def parse_outlook(self, raw):
        """Schema, identity and schedule checks without clock, ledger or broker access."""
        if not isinstance(raw, dict):
            raise LearningRecordRejected("INVALID_MARKET_OUTLOOK")
        plain = _screen_sensitive(raw)
        if plain is None:
            raise LearningRecordRejected("INVALID_MARKET_OUTLOOK")
        try:
            outlook = MarketOutlook.model_validate(raw)
        except ValidationError as exc:
            raise LearningRecordRejected("INVALID_MARKET_OUTLOOK",
                                         errors=validation_errors(exc)) from None
        schedule = self.policy.schedule
        if schedule is None:
            raise ResearchCapabilityUnavailable("RESEARCH_SCHEDULE_NOT_CONFIGURED")
        errors = []
        if not schedule.is_slot(outlook.run_slot):
            errors.append({"path": "run_slot", "code": "RUN_SLOT_NOT_SCHEDULED"})
        elif outlook.generated_at < outlook.run_slot - schedule.grace:
            errors.append({"path": "run_slot", "code": "RUN_SLOT_IN_FUTURE"})
        seen = set()
        for index, coin in enumerate(outlook.coins):
            if coin.symbol in seen:
                errors.append({"path": f"coins[{index}].symbol",
                               "code": "DUPLICATE_COIN_IN_OUTLOOK"})
            seen.add(coin.symbol)
        if errors:
            raise LearningRecordRejected("INVALID_MARKET_OUTLOOK", errors=errors)
        canonical = outlook.model_dump(mode="json")
        leaks = identity_paths(canonical, outlook.agent.agent_id)
        if leaks:
            raise LearningRecordRejected("AGENT_IDENTITY_IN_OUTLOOK", errors=[
                {"path": path, "code": "AGENT_IDENTITY_IN_OUTLOOK"} for path in leaks])
        return outlook, canonical

    @staticmethod
    def outlook_key(agent_id, outlook_id):
        return f"market-outlook:{agent_id}:{outlook_id}"

    @staticmethod
    def _outlook_receipt(row, *, replay):
        body = row["body"]
        return json_safe({
            "status": "MARKET_OUTLOOK_RECORDED", "schema_version": OUTLOOK_SCHEMA,
            "outlook_id": body["outlook_id"], **{k: v for k, v in agent_fields(
                body["agent"]).items() if k in {"agent_id", "agent_version"}},
            "run_slot": body["run_slot"], "generated_at": body["generated_at"],
            "received_at": body["received_at"], "window_start": body["window_start"],
            "window_end": body["window_end"], "grading_day": body["grading_day"],
            "coin_count": body["coin_count"], "skipped_count": body["skipped_count"],
            "universe_count": len(body["universe"]["symbols"]), "event_seq": row["event_seq"],
            "idempotent_replay": replay, "trade_authorized": False,
        })

    def submit_outlook(self, raw):
        """Validate and record one outlook; returns the receipt (see the module docstring)."""
        outlook, canonical = self.parse_outlook(raw)
        outlook_hash = digest(encoded(canonical))
        key = self.outlook_key(outlook.agent.agent_id, outlook.outlook_id)
        with self.store.repo.connect() as conn:
            prior = conn.execute("SELECT * FROM lab.managed_events WHERE idempotency_key=%s",
                                 (key,)).fetchone()
        if prior is not None:
            return self._outlook_replay(prior, outlook_hash)
        now = _utc(self.clock())
        _fresh(now, outlook.generated_at, self.policy.max_age_seconds,
               "MARKET_OUTLOOK_STALE_OR_FUTURE")
        if self.universe is None:
            raise ResearchCapabilityUnavailable("RESEARCH_UNIVERSE_UNAVAILABLE")
        snapshot = self.universe()
        symbols = getattr(snapshot, "symbols", None)
        if not isinstance(symbols, frozenset) or not symbols:
            raise ResearchCapabilityUnavailable("RESEARCH_UNIVERSE_UNAVAILABLE")
        errors = [{"path": f"coins[{index}].symbol", "code": "COIN_NOT_IN_UNIVERSE"}
                  for index, coin in enumerate(outlook.coins) if coin.symbol not in symbols]
        if not errors and symbols - {coin.symbol for coin in outlook.coins}:
            errors.append({"path": "coins", "code": "OUTLOOK_COINS_INCOMPLETE"})
        stored, source_errors = _canonical_sources(canonical, now)
        errors += source_errors
        if errors:
            raise LearningRecordRejected("INVALID_MARKET_OUTLOOK",
                                         errors=errors[:MAX_ERROR_DETAILS])
        window_end = now + timedelta(hours=HORIZON_HOURS)
        body = {
            **stored,
            "generated_at": _utc(outlook.generated_at).isoformat(),
            "run_slot": _utc(outlook.run_slot).isoformat(),
            "received_at": now.isoformat(),
            "window_start": now.isoformat(),
            "window_end": window_end.isoformat(),
            "grading_day": grading_day(window_end).isoformat(),
            "coin_count": len(outlook.coins),
            "skipped_count": sum(coin.direction == "SKIPPED" for coin in outlook.coins),
            "outlook_hash": outlook_hash,
            "universe": {
                "source": getattr(snapshot, "source", None),
                "fetched_at": _utc(snapshot.fetched_at).isoformat(),
                "symbols": sorted(symbols),
            },
            "trade_authorized": False,
        }
        with self.store.transaction() as conn:
            prior = conn.execute("SELECT * FROM lab.managed_events WHERE idempotency_key=%s",
                                 (key,)).fetchone()
            if prior is not None:
                return self._outlook_replay(prior, outlook_hash)
            row = self.store.event(conn, OUTLOOK_EVENT, json_safe(body), key=key)
        return self._outlook_receipt(row, replay=False)

    def _outlook_replay(self, prior, outlook_hash):
        if prior["kind"] != OUTLOOK_EVENT or prior["body"].get("outlook_hash") != outlook_hash:
            raise LearningRecordRejected("OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH")
        return self._outlook_receipt(prior, replay=True)

    # --- POST_MORTEM_V1 ----------------------------------------------------------------------

    @staticmethod
    def post_mortem_key(agent_id, note_id):
        return f"post-mortem:{agent_id}:{note_id}"

    def parse_post_mortem(self, raw):
        """The envelope (whole-note refusals) and each item's schema result."""
        if not isinstance(raw, dict):
            raise LearningRecordRejected("INVALID_POST_MORTEM")
        if _screen_sensitive(raw) is None:
            raise LearningRecordRejected("INVALID_POST_MORTEM")
        try:
            envelope = PostMortemEnvelope.model_validate(raw)
        except ValidationError as exc:
            raise LearningRecordRejected("INVALID_POST_MORTEM",
                                         errors=validation_errors(exc)) from None
        items = []
        for index, value in enumerate(envelope.items):
            try:
                item = PostMortemItem.model_validate(value)
                items.append((index, item, item.model_dump(mode="json"), None, ()))
            except ValidationError as exc:
                items.append((index, None, None, "INVALID_POST_MORTEM_ITEM",
                              tuple(validation_errors(exc, f"items[{index}]"))))
        canonical = envelope.model_dump(mode="json", exclude={"items"})
        canonical["items"] = [data if data is not None else {"invalid_item_sha256": digest(
            encoded(json_safe(envelope.items[index])))} for index, _, data, _, _ in items]
        return envelope, items, canonical

    @staticmethod
    def subject_key(subject):
        if subject.kind == "TRADE":
            return f"TRADE:{subject.setup_id}"
        return f"MOVER:{subject.day}:{subject.symbol}"

    def submit_post_mortem(self, raw, *, principal):
        envelope, items, canonical = self.parse_post_mortem(raw)
        note_hash = digest(encoded(canonical))
        agent_id = envelope.agent.agent_id
        key = self.post_mortem_key(agent_id, envelope.note_id)
        with self.store.repo.connect() as conn:
            prior = conn.execute("SELECT * FROM lab.managed_events WHERE idempotency_key=%s",
                                 (key,)).fetchone()
        if prior is not None:
            return self._post_mortem_replay(prior, note_hash)
        now = _utc(self.clock())
        _fresh(now, envelope.generated_at, self.policy.max_age_seconds,
               "POST_MORTEM_STALE_OR_FUTURE")
        with self.store.transaction() as conn:
            prior = conn.execute("SELECT * FROM lab.managed_events WHERE idempotency_key=%s",
                                 (key,)).fetchone()
            if prior is not None:
                return self._post_mortem_replay(prior, note_hash)
            results, accepted = self._check_items(conn, items, agent_id, principal, now)
            if not accepted:
                first = results[0]
                raise LearningRecordRejected(first["code"], item_results=results)
            body = {
                "schema_version": POST_MORTEM_SCHEMA, "note_id": str(envelope.note_id),
                "agent": canonical["agent"],
                "generated_at": _utc(envelope.generated_at).isoformat(),
                "received_at": now.isoformat(), "items": accepted, "item_results": results,
                "accepted_count": len(accepted), "rejected_count": len(results) - len(accepted),
                "note_hash": note_hash, "trade_authorized": False,
            }
            row = self.store.event(conn, POST_MORTEM_EVENT, json_safe(body), key=key)
        return self._post_mortem_receipt(row, replay=False)

    def _post_mortem_replay(self, prior, note_hash):
        if prior["kind"] != POST_MORTEM_EVENT or prior["body"].get("note_hash") != note_hash:
            raise LearningRecordRejected("POST_MORTEM_IDEMPOTENCY_CONTENT_MISMATCH")
        return self._post_mortem_receipt(prior, replay=True)

    @staticmethod
    def _post_mortem_receipt(row, *, replay):
        body = row["body"]
        return json_safe({
            "status": "POST_MORTEM_RECORDED", "schema_version": POST_MORTEM_SCHEMA,
            "note_id": body["note_id"], "agent_id": body["agent"]["agent_id"],
            "received_at": body["received_at"], "accepted_count": body["accepted_count"],
            "rejected_count": body["rejected_count"], "item_results": body["item_results"],
            "event_seq": row["event_seq"], "idempotent_replay": replay,
            "trade_authorized": False,
        })

    def _check_items(self, conn, items, agent_id, principal, now):
        """Each item in order: the first failing check names it (see the module docstring)."""
        results, accepted, seen = [], [], set()
        realities = {}
        for index, item, data, code, errors in items:
            subject_key = self.subject_key(item.subject) if item is not None else None
            reference = None
            if code is None and subject_key in seen:
                code, errors = "DUPLICATE_SUBJECT_IN_NOTE", (
                    {"path": f"items[{index}].subject", "code": "DUPLICATE_SUBJECT_IN_NOTE"},)
            if code is None:
                code, reference = self._subject(conn, item.subject, principal, realities)
                if code is not None:
                    errors = ({"path": f"items[{index}].subject", "code": code},)
            if code is None and item.cause in SOURCED_CAUSES and not item.sources:
                code = "POST_MORTEM_SOURCES_REQUIRED"
                errors = ({"path": f"items[{index}].sources", "code": code},)
            stored = None
            if code is None:
                stored, source_errors = _canonical_sources(data, now)
                if source_errors:
                    code = "INVALID_SOURCE_EVIDENCE"
                    errors = tuple({"path": f"items[{index}].{e['path']}", "code": e["code"]}
                                   for e in source_errors)
            if code is None:
                leaks = identity_paths(stored, agent_id, f"items[{index}]")
                if leaks:
                    code = "AGENT_IDENTITY_IN_POST_MORTEM"
                    errors = tuple({"path": path, "code": code} for path in leaks)
            if code is None and item.knowable_before_move is True and not _published_before(
                    stored["sources"], reference):
                code = "KNOWABLE_BEFORE_MOVE_UNSUPPORTED"
                errors = ({"path": f"items[{index}].knowable_before_move", "code": code},)
            if code is not None:
                results.append({"index": index, "status": "REJECTED",
                                "subject_key": subject_key, "code": code,
                                "errors": list(errors)[:MAX_ERROR_DETAILS]})
                continue
            seen.add(subject_key)
            accepted.append({**stored, "index": index, "subject_key": subject_key,
                             "reference_at": reference.isoformat() if reference else None})
            results.append({"index": index, "status": "ACCEPTED", "subject_key": subject_key})
        return results, accepted

    def _subject(self, conn, subject, principal, realities):
        """``(code, reference_at)``: a refusal code, or None and the item's reference time."""
        if subject.kind == "TRADE":
            row = conn.execute(
                """SELECT s.record_json->'agent' AS agent, t.body->>'state' AS state,
                (SELECT min(f.filled_at) FROM lab.managed_fills f
                 WHERE f.setup_id=s.setup_id AND f.side='buy') AS entered_at
                FROM lab.managed_setups s LEFT JOIN lab.managed_states t USING(setup_id)
                WHERE s.setup_id=%s""", (subject.setup_id,),
            ).fetchone()
            if row is None:
                return "TRADE_NOT_FOUND", None
            agent = row["agent"] if isinstance(row["agent"], dict) else None
            owner = agent.get("agent_id") if agent else None
            if not principal.acts_for(owner):
                return "TRADE_OF_ANOTHER_AGENT", None
            if row["entered_at"] is None or row["state"] != "CLOSED":
                return "TRADE_NOT_CLOSED", None
            return None, row["entered_at"]
        if subject.day not in realities:
            found = conn.execute(
                """SELECT body->'movers' AS movers FROM lab.managed_events
                WHERE kind=%s AND idempotency_key=%s""",
                (REALITY_EVENT, f"market-reality:{subject.day}"),
            ).fetchone()
            realities[subject.day] = found["movers"] if found else None
        movers = realities[subject.day]
        if movers is None:
            return "REALITY_DAY_NOT_RECORDED", None
        mover = next((m for m in movers if m.get("symbol") == subject.symbol), None)
        if mover is None:
            return "NOT_A_RECORDED_MOVER", None
        start = mover.get("move_start_at")
        return None, datetime.fromisoformat(start) if start else None


class _DeclaredAgent(BaseModel):
    """Only a learning body's agent block, for the credential check before intake."""

    model_config = ConfigDict(extra="ignore")
    agent: AgentIdentity | None = Field(default=None, validate_default=True)

    @field_validator("agent")
    @classmethod
    def agent_required(cls, value):
        if value is None:
            raise ValueError("AGENT_IDENTITY_REQUIRED")
        return value


def declared_agent(raw, code):
    """The validated agent block of an outlook or post-mortem body (``code`` names a refusal):
    the HTTP layer compares it with the credential before intake, as for reports."""
    try:
        return _DeclaredAgent.model_validate(raw).agent.model_dump(mode="json")
    except ValidationError as exc:
        raise LearningRecordRejected(code, errors=validation_errors(exc)) from None


def _published_before(sources, reference):
    """At least one cited source was published at or before ``reference`` (the page's own
    publish time; an unknown time never counts)."""
    if reference is None:
        return False
    for source in sources:
        published = source.get("published_at")
        if published and datetime.fromisoformat(published) <= reference:
            return True
    return False


__all__ = [
    "CAUSES", "COIN_DIRECTIONS", "DIRECTIONS", "HORIZON_HOURS", "LEARNING_EVENT_KINDS",
    "LearningIntake", "LearningPolicy", "LearningRecordRejected", "MarketOutlook",
    "OUTLOOK_BODY_LIMIT",
    "OUTLOOK_EVENT", "OUTLOOK_SCHEMA", "POST_MORTEM_BODY_LIMIT", "POST_MORTEM_EVENT",
    "POST_MORTEM_SCHEMA", "PostMortemItem", "REASON_KINDS", "day_bounds", "grading_day",
    "identity_paths",
]
