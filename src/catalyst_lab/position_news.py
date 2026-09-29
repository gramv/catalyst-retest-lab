"""Immutable post-entry source updates, independent of the original research expiry.

This service records evidence only. It never changes a position, expiry, thesis,
quantity or broker request. The position reviewer remains a bounded judgment caller.
"""

import re
from datetime import UTC, datetime
from uuid import UUID

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.repository import json_safe
from catalyst_lab.research_dossier_v3 import identity_paths
from catalyst_lab.research_evidence import canonical_sources
from catalyst_lab.review_storage import SourceExcerpt


def _date(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        raise ValueError("SOURCE_UTC_TIMESTAMP_REQUIRED") from None


def _sources(raw, now):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("SOURCE_COUNT_INVALID")
    fields = set(SourceExcerpt.model_fields) | {
        "content_hash", "primary_source", "asset_relevant", "novelty", "stance"
    }
    for row in raw:
        if not isinstance(row, dict) or set(row) - fields:
            raise ValueError("POSITION_NEWS_FIELDS_INVALID")
        if (
            any(type(row[k]) is not bool for k in ("primary_source", "asset_relevant") if k in row)
            or ("novelty" in row and row["novelty"] not in
                {"NEW_FACT", "PREVIOUSLY_KNOWN", "UNVERIFIED"})
            or ("stance" in row and row["stance"] not in
                {"SUPPORTS", "ADVERSE", "NEUTRAL", "WITHDRAWN"})
        ):
            raise ValueError("INVALID_SOURCE_EVIDENCE")
    try:
        raw = canonical_sources(raw, now=now)
    except (ValueError, TypeError, KeyError):
        raise ValueError("INVALID_SOURCE_EVIDENCE") from None
    serialized = encoded(raw)
    _privacy_check(serialized)
    if re.search(r"(?i)\b(?:authorization\s*:|bearer\s+|APCA_API_|TYPESAFE_API_KEY)", serialized):
        raise ValueError("SENSITIVE_EVIDENCE_REJECTED")
    return json_safe(raw)


def current_position_evidence(conn, setup, lifecycle_id):
    """Newest evidence first; the immutable original thesis stays in the setup.

    Without post-entry news the legacy research revision is retained. Afterwards,
    the newest relevant audit sequence is the monotonic evidence revision, so a
    later research packet also invalidates an in-flight position review.
    """
    packet = setup["record_json"]
    research = conn.execute(
        """SELECT event_seq,body FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
        AND body->>'cycle_id'=%s AND body->>'item_key'=%s ORDER BY event_seq DESC LIMIT 1""",
        (str(setup["cycle_id"]), packet.get("item_key")),
    ).fetchone()
    if research and research["body"]["revision"] > setup["revision"]:
        revision = research["body"]["revision"]
        sources = research["body"]["state"]["sources"]
    else:
        revision, sources = setup["revision"], packet["sources"]
    news = conn.execute(
        """SELECT event_seq,body FROM lab.managed_events WHERE kind='POSITION_NEWS'
        AND setup_id=%s AND body->>'lifecycle_id'=%s ORDER BY event_seq DESC""",
        (setup["setup_id"], str(lifecycle_id)),
    ).fetchall()
    if not news:
        return revision, sources
    revision = max(news[0]["event_seq"], research["event_seq"] if research else 0)
    # Bound the model context; every retained source remains in the append-only log.
    groups = [(event["event_seq"], event["body"]["sources"]) for event in news]
    groups.append((research["event_seq"] if research else 0, sources))
    merged, seen = [], set()
    for source in [s for _, group in sorted(groups, key=lambda row: -row[0]) for s in group]:
        if source["content_hash"] not in seen:
            merged.append(source)
            seen.add(source["content_hash"])
        if len(merged) == 8:
            break
    return revision, merged


AGENT_IDENTITY_IN_NEWS = "AGENT_IDENTITY_IN_NEWS"


class PositionNewsService:
    def __init__(self, store, *, clock):
        self.store, self.now = store, clock

    def context(self, setup_id):
        with self.store.repo.connect() as conn:
            setup = self.store.setup(conn, setup_id)
            state = self.store.state(conn, setup_id)
            revision, sources = current_position_evidence(conn, setup, state.get("lifecycle_id"))
        return json_safe(
            {
                "setup_id": str(setup_id),
                "symbol": setup["symbol"],
                "state": state["state"],
                "lifecycle_id": state.get("lifecycle_id"),
                "news_revision": revision,
                "hard_exit_at": state.get("hard_exit_at"),
                "sources": sources,
                "original_thesis": setup["record_json"]["thesis"],
                "cohort": setup["cohort"],
            }
        )

    def submit(self, setup_id, raw, *, agent_id=None):
        """Append one post-entry news update. ``agent_id`` (the posting research agent) must not
        appear, as a whole word in any case, in an agent-written source field (the source ID and
        times; excerpts and URLs are third-party text): position sources reach Jev's maintenance
        and 24-hour-review contexts, and Jev never sees the agent (AGENTS.md). Refused
        ``AGENT_IDENTITY_IN_NEWS`` before anything is stored (the report-V3 rule
        ``AGENT_IDENTITY_IN_PICK`` applied to position news, 2026-09-27)."""
        if not isinstance(raw, dict) or set(raw) != {
            "news_id",
            "lifecycle_id",
            "expected_news_revision",
            "sources",
        }:
            raise ValueError("POSITION_NEWS_FIELDS_INVALID")
        try:
            news_id, lifecycle_id = str(UUID(raw["news_id"])), str(UUID(raw["lifecycle_id"]))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("POSITION_NEWS_IDENTITY_INVALID") from None
        if type(raw["expected_news_revision"]) is not int or raw["expected_news_revision"] < 1:
            raise ValueError("POSITION_NEWS_REVISION_REQUIRED")
        now = self.now()
        sources = _sources(raw["sources"], now)
        if agent_id and identity_paths({"sources": sources}, agent_id, "news"):
            raise ValueError(AGENT_IDENTITY_IN_NEWS)
        request_hash = digest(encoded({"setup_id": str(setup_id), **raw}))
        key = "position-news:" + news_id
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone()
            if existing:
                if existing["body"].get("request_hash") != request_hash:
                    raise ValueError("IDEMPOTENCY_CONTENT_MISMATCH")
                return self._ack(existing, replay=True)
            setup = self.store.setup(conn, setup_id)
            state = self.store.state(conn, setup_id)
            if state.get("state") != "OPEN" or state.get("lifecycle_id") != lifecycle_id:
                raise ValueError("POSITION_LIFECYCLE_NOT_OPEN")
            if now >= _date(state.get("hard_exit_at")):
                raise ValueError("POSITION_HARD_DEADLINE_REACHED")
            revision, _ = current_position_evidence(conn, setup, lifecycle_id)
            if raw["expected_news_revision"] != revision:
                raise ValueError("STALE_POSITION_NEWS_REVISION")
            history = conn.execute(
                """SELECT body FROM lab.managed_events WHERE
                (kind='POSITION_NEWS' AND setup_id=%s AND body->>'lifecycle_id'=%s)
                OR (kind='RESEARCH_PACKET' AND body->>'cycle_id'=%s AND body->>'item_key'=%s)""",
                (
                    setup_id,
                    lifecycle_id,
                    str(setup["cycle_id"]),
                    setup["record_json"].get("item_key"),
                ),
            ).fetchall()
            old_hashes = {s["content_hash"] for s in setup["record_json"]["sources"]}
            for event in history:
                row = event["body"]
                old_hashes.update(
                    s["content_hash"]
                    for s in row.get("sources", row.get("state", {}).get("sources", []))
                )
            if not any(s["content_hash"] not in old_hashes for s in sources):
                raise ValueError("MATERIAL_POSITION_NEWS_REQUIRED")
            body = {
                "news_id": news_id,
                "setup_id": str(setup_id),
                "lifecycle_id": lifecycle_id,
                "previous_news_revision": revision,
                "sources": sources,
                "received_at": now,
                "hard_exit_at": state["hard_exit_at"],
                "request_hash": request_hash,
            }
            body["evidence_hash"] = digest(encoded(json_safe(body)))
            event = self.store.event(conn, "POSITION_NEWS", body, setup_id=setup_id, key=key)
        return self._ack(event, replay=False)

    @staticmethod
    def _ack(event, *, replay):
        return {
            "status": "POSITION_NEWS_RECORDED",
            "news_revision": event["event_seq"],
            "evidence_hash": event["body"]["evidence_hash"],
            "idempotent_replay": replay,
            "trade_authorized": False,
            "position_modified": False,
        }
