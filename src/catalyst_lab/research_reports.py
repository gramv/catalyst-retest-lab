"""Research reports and supervised Jev review. No broker or risk imports."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.review_config import Gate1Inputs
from catalyst_lab.review_storage import SourceExcerpt

SELECTION_POLICY = "JEV_SKEPTIC_RESEARCH_TEST_V1"


class ResearchLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_trigger: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    max_entry_price: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    stop: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    target: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12)


class ResearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    signal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9][A-Z0-9./_-]*$")
    direction: Literal["LONG"]
    catalyst: str = Field(min_length=1, max_length=64)
    thesis: str = Field(min_length=1, max_length=1000)
    disproof: str = Field(min_length=1, max_length=1000)
    economic_relationship: str = Field(min_length=1, max_length=1000)
    levels: ResearchLevels
    sources: list[SourceExcerpt] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded_sources(self):
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("DUPLICATE_SOURCE_ID")
        if sum(len(s.excerpt) for s in self.sources) > 8000:
            raise ValueError("EXCERPT_BUDGET_EXCEEDED")
        _privacy_check(encoded(self.model_dump(mode="json")))
        return self


class ResearchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: UUID
    report_id: UUID
    report_key: str = Field(pattern=r"^TEST-[A-Za-z0-9_.:-]{1,120}$")
    revision: int = Field(ge=1, strict=True)
    market: Literal["US_STOCKS", "CRYPTO", "INDIA"]
    timeframe: str = Field(pattern=r"^[A-Z0-9_]{1,32}$")
    generated_at: AwareDatetime
    valid_until: AwareDatetime
    items: list[ResearchItem] = Field(min_length=1, max_length=50)

    @field_validator("generated_at", "valid_until", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        return SourceExcerpt.typed_timestamp(value)

    @model_validator(mode="after")
    def bounded_report(self):
        if self.generated_at >= self.valid_until:
            raise ValueError("INVALID_REPORT_EXPIRY")
        if len({i.signal_id for i in self.items}) != len(self.items):
            raise ValueError("DUPLICATE_SIGNAL_IN_REPORT")
        if len({i.symbol for i in self.items}) != len(self.items):
            raise ValueError("DUPLICATE_SYMBOL_IN_REPORT")
        return self


class ResearchReports(Repository):
    def __init__(self, database_url, gate1: Gate1Inputs):
        super().__init__(database_url)
        self.gate1 = gate1

    def submit(self, raw):
        report = ResearchReport.model_validate(raw)
        data = report.model_dump(mode="json")
        data["generated_at"] = report.generated_at.astimezone(UTC).isoformat()
        data["valid_until"] = report.valid_until.astimezone(UTC).isoformat()
        for item, model in zip(data["items"], report.items, strict=True):
            # Stable ordering and decimal spelling prevent a reordered source list or
            # numerically identical level from becoming a fresh independent vote.
            item["levels"] = {k: format(v.normalize(), "f") for k, v in vars(model.levels).items()}
            item["sources"].sort(key=lambda s: s["source_id"])
            for source in item["sources"]:
                for name in ("published_at", "retrieved_at"):
                    if source[name] is not None:
                        source[name] = (
                            datetime.fromisoformat(source[name]).astimezone(UTC).isoformat()
                        )
                source["excerpt_hash"] = digest(source["excerpt"])
            _privacy_check(encoded(item))
        with self.connect() as conn:
            result = conn.execute(
                "SELECT lab.store_research_report(%s,%s) AS result",
                (Jsonb(data), Jsonb(self.gate1.values)),
            ).fetchone()["result"]
        return result

    def report(self, report_id, revision=None):
        with self.connect() as conn:
            report = conn.execute(
                """SELECT * FROM lab.research_reports WHERE report_id=%s
                AND (%s::integer IS NULL OR revision=%s) ORDER BY revision DESC LIMIT 1""",
                (report_id, revision, revision),
            ).fetchone()
            if report is None:
                return None
            rows = conn.execute(
                """SELECT * FROM lab.research_report_results
                WHERE report_id=%s AND revision=%s ORDER BY item_index""",
                (report_id, report["revision"]),
            ).fetchall()
            execution = {
                str(row["item_id"]): row
                for row in conn.execute(
                    "SELECT p.* FROM lab.jev_paper_status p "
                    "JOIN lab.research_report_items i USING(item_id) "
                    "WHERE i.report_id=%s AND i.revision=%s",
                    (report_id, report["revision"]),
                ).fetchall()
            }
            for row in rows:
                row["paper_execution"] = execution.get(str(row["item_id"]))
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return json_safe(
            {
                "report": report,
                "items": rows,
                "counts": counts,
                "authorizes_entry": False,
                "execution_enabled": False,
                "selection_policy": SELECTION_POLICY,
            }
        )

    def index(self, market, limit):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT report_id,report_key,revision,market,timeframe,created_at AS received_at,
                  cohort,record_purpose FROM (
                    SELECT DISTINCT ON(report_id) * FROM lab.research_reports
                    WHERE market=%s ORDER BY report_id,revision DESC
                  ) latest ORDER BY event_seq DESC LIMIT %s""",
                (market, limit),
            ).fetchall()
        return json_safe({"reports": rows, "authorizes_entry": False})

    def outputs(self, after_seq, limit):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lab.research_output_events WHERE event_seq>%s "
                "ORDER BY event_seq LIMIT %s",
                (after_seq, limit),
            ).fetchall()
        return json_safe(
            {
                "events": rows,
                "next_cursor": rows[-1]["event_seq"] if rows else after_seq,
                "authorizes_entry": False,
            }
        )

    def workers(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lab.review_worker_status ORDER BY worker_id"
            ).fetchall()
        return json_safe({"workers": rows, "execution_enabled": False})


def report_review_args(store, item_id):
    with store.connect() as conn:
        item = conn.execute(
            "SELECT * FROM lab.research_report_items WHERE item_id=%s", (item_id,)
        ).fetchone()
        if item is None:
            raise ValueError("RESEARCH_ITEM_MISSING")
    context = item["record_json"]["context"]
    return {
        "request_id": uuid4(),
        "identity": {
            "research_item_id": str(item_id),
            "item_hash": item["item_hash"],
            "research_content_hash": item["content_hash"],
            "report_id": str(item["report_id"]),
            "report_revision": item["revision"],
            "market": context["market"],
            "cohort": "JEV_ENGINEERING_TEST",
            "research_policy_id": SELECTION_POLICY,
        },
        "state": item["state_json"],
        "question_set": SKEPTIC,
        "expires_at": item["review_deadline"],
        "purpose": "ENGINEERING_TEST",
    }


def finish_report_item(store, item_id):
    """Disposition is derived by a fixed DB function; callers cannot submit SELECTED."""
    with store.connect() as conn:
        row = conn.execute("SELECT lab.finalize_research_item(%s) AS result", (item_id,)).fetchone()
    return row["result"]


async def review_report(reports, reviewer, report_id, revision, *, max_inflight):
    """Supervised batch runner; durable receipts prevent repeat votes on restart.

    This is not the persistent worker or a trading loop. Explicit concurrency is an
    engineering input; all item deadlines were set at intake, never at dequeue time.
    """
    if type(max_inflight) is not int or not 1 <= max_inflight <= 50:
        raise ValueError("EXPLICIT_BOUNDED_CONCURRENCY_REQUIRED")
    packet = reports.report(report_id, revision)
    if packet is None:
        raise ValueError("RESEARCH_REPORT_MISSING")
    semaphore = asyncio.Semaphore(max_inflight)

    async def evaluate(item):
        async with semaphore:
            if item["recorded_disposition"] is not None:
                return
            if item["status"] in {"EXPIRED", "SUPERSEDED", "NEEDS_REVIEW"}:
                finish_report_item(reviewer.store, item["item_id"])
                return
            args = report_review_args(reviewer.store, item["item_id"])
            with reviewer.store.connect() as conn:
                existing = conn.execute(
                    "SELECT request_id FROM lab.jev_requests "
                    "WHERE evidence_identity->>'research_item_id'=%s",
                    (item["item_id"],),
                ).fetchone()
            if not existing:
                await reviewer.jev_review(**args)
            # Never redo the provider call for a pre-existing request. An uncertain
            # in-flight outcome remains pending until its deadline, then needs review.
            finish_report_item(reviewer.store, item["item_id"])

    await asyncio.gather(*(evaluate(item) for item in packet["items"]))
    return reports.report(report_id, revision)
