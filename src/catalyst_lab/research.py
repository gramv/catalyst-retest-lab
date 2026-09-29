"""Operator-only manual research ingestion; never part of the broker-verified trade population."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from catalyst_lab.repository import json_safe


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    external_id: str = Field(min_length=1, max_length=128)
    market: Literal["INDIA", "CRYPTO"]
    strategy_version: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=30)
    currency: str = Field(pattern=r"^[A-Z]{3,8}$")
    direction: Literal["LONG", "SHORT"]
    opened_at: AwareDatetime
    closed_at: AwareDatetime
    qty: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=10)
    entry_price: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=10)
    exit_price: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=10)
    stop: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=10)
    catalyst: str = Field(min_length=1, max_length=80)
    thesis: str = Field(min_length=1, max_length=1000)
    disproof: str = Field(min_length=1, max_length=1000)
    source_reference: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def sane(self):
        if self.closed_at < self.opened_at or self.closed_at > datetime.now(UTC):
            raise ValueError("A completed historical research observation is required")
        if (self.entry_price - self.stop) * (1 if self.direction == "LONG" else -1) <= 0:
            raise ValueError("Stop must define positive risk in the reported direction")
        return self


def import_research(repo, raw, *, correction_of=None):
    record = ResearchResult.model_validate(raw)
    body = record.model_dump(mode="json")
    payload = {
        "kind": "RESEARCH_RESULT",
        "market": record.market,
        "execution_source": "MUSE_MANUAL",
        "record": body,
        "verified": False,
    }
    with repo.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        prior = conn.execute(
            """SELECT r.*,e.event_id FROM lab.current_research_results r
            JOIN lab.trade_events e ON e.seq=r.event_seq WHERE market=%s AND external_id=%s""",
            (record.market, record.external_id),
        ).fetchone()
        if prior and (correction_of is None or UUID(str(correction_of)) != prior["event_id"]):
            raise ValueError(
                "An existing research result requires its latest event ID as correction_of"
            )
        if correction_of and not prior:
            raise ValueError("Cannot correct an unknown research record")
        event = conn.execute(
            """INSERT INTO lab.trade_events(strategy_version,event_type,payload_json,
            correction_of) VALUES(%s,%s,%s,%s) RETURNING seq,event_id,event_hash""",
            (
                record.strategy_version,
                "CORRECTION" if prior else "SYSTEM_EVENT",
                Jsonb(payload),
                prior["event_id"] if prior else None,
            ),
        ).fetchone()
        conn.execute(
            """INSERT INTO lab.research_results(event_seq,external_id,market,strategy_version,
            payload_json) VALUES(%s,%s,%s,%s,%s)""",
            (event["seq"], record.external_id, record.market, record.strategy_version, Jsonb(body)),
        )
    return json_safe(event)


def read_research(repo, market, *, limit=100, offset=0):
    with repo.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM lab.public_research_results WHERE market=%s
            ORDER BY received_at DESC,event_seq DESC LIMIT %s OFFSET %s""",
            (market, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT count(*) AS n FROM lab.public_research_results WHERE market=%s", (market,)
        ).fetchone()["n"]
    items = []
    for row in rows:
        body = row["payload_json"]
        sign = 1 if body["direction"] == "LONG" else -1
        pnl = (
            sign
            * Decimal(body["qty"])
            * (Decimal(body["exit_price"]) - Decimal(body["entry_price"]))
        )
        items.append(
            {
                **body,
                "received_at": row["received_at"],
                "event_seq": row["event_seq"],
                "reported_price_pnl": pnl,
                "execution_source": "MUSE_MANUAL",
                "verified": False,
            }
        )
    return json_safe(
        {
            "market": market,
            "execution_source": "MUSE_MANUAL",
            "verification": "MANUAL_OR_SIMULATED",
            "included_in_headline": False,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "publication_policy": "NEXT_NEW_YORK_DAY",
            "status": "AVAILABLE" if items else "NO_PUBLISHED_RESEARCH_RESULTS",
        }
    )
