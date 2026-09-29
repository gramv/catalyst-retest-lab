"""Immutable review evidence and write-only inert intents. No execution imports."""

import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.repository import Repository, json_safe


class SourceExcerpt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    url: str = Field(max_length=1000)
    excerpt: str = Field(min_length=1, max_length=1200)
    retrieved_at: AwareDatetime
    published_at: AwareDatetime | None = None

    @field_validator("excerpt")
    @classmethod
    def exact_excerpt(cls, value):
        if not value.strip():
            raise ValueError("SOURCE_EXCERPT_REQUIRED")
        return value

    @field_validator("url")
    @classmethod
    def source_url(cls, value):
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError("PUBLIC_HTTPS_SOURCE_REQUIRED")
        return value

    @field_validator("retrieved_at", "published_at", mode="before")
    @classmethod
    def typed_timestamp(cls, value):
        if value is None:
            return value
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError("RFC3339_TIMESTAMP_REQUIRED")
        return value

    @model_validator(mode="after")
    def chronology(self):
        if self.published_at and self.published_at > self.retrieved_at:
            raise ValueError("INVALID_SOURCE_CHRONOLOGY")
        return self


class EvidenceSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    revision: int = Field(ge=1, strict=True)
    sources: list[SourceExcerpt] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def source_bounds(self):
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("DUPLICATE_SOURCE_ID")
        if sum(len(s.excerpt) for s in self.sources) > 8000:
            raise ValueError("EXCERPT_BUDGET_EXCEEDED")
        _privacy_check(encoded(self.model_dump(mode="json")))
        return self


class StoredIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: UUID
    evidence_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ai_decision_id: UUID
    strategy_version: Literal["CATALYST_RETEST_V1"]


class ReviewStorage(Repository):
    def check_role(self):
        with self.connect() as conn:
            row = conn.execute("""SELECT current_user AS role,
              rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls AS privileged,
              has_schema_privilege(current_user,'lab','CREATE') AS ddl
              FROM pg_roles WHERE rolname=current_user""").fetchone()
            if row["role"] != "catalyst_review" or row["privileged"] or row["ddl"]:
                raise RuntimeError("RESTRICTED_REVIEW_STORAGE_ROLE_REQUIRED")
            forbidden = conn.execute("""SELECT c.relname FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='lab'
              AND c.relkind IN ('r','v','m') AND
              (has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE')
               OR (c.relname='entry_intents' AND has_table_privilege(current_user,c.oid,'SELECT')))
              LIMIT 1""").fetchone()
            if forbidden:
                raise RuntimeError("REVIEW_STORAGE_ROLE_TOO_BROAD")
            # Check migration presence without granting schema/table-owner capability.
            conn.execute("SELECT 1 FROM lab.evidence_bundles LIMIT 0")
            conn.execute("SELECT 1 FROM lab.review_observations LIMIT 0")

    def store_evidence(self, raw, policy):
        submission = EvidenceSubmission.model_validate(raw)
        sources = []
        for source in submission.sources:
            item = source.model_dump(mode="json")
            item["retrieved_at"] = source.retrieved_at.astimezone(UTC).isoformat()
            item["published_at"] = (
                source.published_at.astimezone(UTC).isoformat() if source.published_at else None
            )
            item["excerpt_hash"] = digest(source.excerpt)
            sources.append(item)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM lab.store_review_evidence(%s,%s,%s,%s,%s,%s)",
                (
                    submission.candidate_id,
                    submission.revision,
                    Jsonb(sources),
                    policy.research_policy_id,
                    policy.gate1.values["evidence_max_age_seconds"],
                    policy.gate1.values["context_max_age_seconds"],
                ),
            ).fetchone()
            # Server-sourced thesis/context must pass privacy checks before transaction commits.
            record = conn.execute(
                "SELECT record_json FROM lab.evidence_bundles WHERE bundle_hash=%s",
                (row["bundle_hash"],),
            ).fetchone()
            _privacy_check(encoded(record["record_json"]))
        return json_safe({**row, "status": "STORED_EVIDENCE_ONLY", "authorizes_entry": False})

    def evidence(self, bundle_hash):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM lab.evidence_bundles WHERE bundle_hash=%s", (bundle_hash,)
            ).fetchone()
        return json_safe(row) if row else None

    def store_intent(self, raw):
        intent = StoredIntent.model_validate(raw)
        with self.connect() as conn:
            conn.execute(
                "SELECT lab.store_entry_intent(%s,%s,%s,%s)",
                (
                    intent.intent_id,
                    intent.evidence_bundle_hash,
                    intent.ai_decision_id,
                    intent.strategy_version,
                ),
            )
        return {
            "intent_id": str(intent.intent_id),
            "status": "STORED_ONLY_NOT_AUTHORIZED",
            "authorizes_entry": False,
        }

    def observations(self, *, cohort, after_seq, limit):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM lab.review_observations
              WHERE cohort=%s AND event_seq>%s ORDER BY event_seq LIMIT %s""",
                (cohort, after_seq, limit),
            ).fetchall()
        return json_safe(
            {
                "cohort": cohort,
                "strategy_version": "CATALYST_RETEST_V1",
                "strategy_results": False,
                "items": rows,
                "next_after_seq": rows[-1]["event_seq"] if rows else after_seq,
            }
        )


def bound_review_args(store, bundle_hash, *, request_id, question_set, now=None):
    """Trusted adapter input loader; no model invocation, scheduling or entry consumer."""
    now = now or datetime.now(UTC)
    with store.connect() as conn:
        row = conn.execute(
            """SELECT b.*,c.deadline FROM lab.evidence_bundles b
          JOIN lab.review_contexts c USING(context_hash) WHERE b.bundle_hash=%s""",
            (bundle_hash,),
        ).fetchone()
    if not row:
        raise ValueError("EVIDENCE_BUNDLE_MISSING")
    if now >= row["deadline"]:
        raise ValueError("EVIDENCE_BUNDLE_EXPIRED")
    body = row["record_json"]
    state = {
        **body["market_state"],
        "sources": [
            {"source_id": source["source_id"], "excerpt": source["excerpt"]}
            for source in body["sources"]
        ],
    }
    _privacy_check(encoded(state))
    return {
        "request_id": request_id,
        "identity": {
            "evidence_bundle_hash": bundle_hash,
            "context_hash": row["context_hash"],
            "candidate_id": str(row["candidate_id"]),
            "evidence_revision": row["revision"],
            "strategy_version": row["strategy_version"],
            "cohort": row["cohort"],
            "research_policy_id": body["context"]["research_policy_id"],
        },
        "state": state,
        "question_set": question_set,
        "expires_at": row["deadline"],
        "purpose": row["record_purpose"],
    }
