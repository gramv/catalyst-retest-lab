"""Durable Gate 1 controls for the app-owned review process; no trading capability."""

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.jev_contract import QuestionSet, choice, encoded
from catalyst_lab.jev_review import ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.market import NY
from catalyst_lab.review_config import Gate1Inputs

HEALTH_STATE = {"diagnostic": "SYNTHETIC_HEALTH_CHECK", "statement": "The sample is a diagnostic."}
HEALTH_QUESTIONS = QuestionSet(
    "PROVIDER_HEALTH_V1",
    "ROUTING",
    encoded(
        {
            "diagnostic": choice(
                "Does the supplied statement describe a diagnostic?",
                {"YES": "It describes a diagnostic.", "NO": "It describes something else."},
            )
        }
    ),
)


@dataclass(frozen=True)
class ClockSample:
    offset_ms: float
    uncertainty_ms: float
    observed_at: datetime


def database_clock_sample(store, *, wall=lambda: datetime.now(UTC), monotonic=time.monotonic):
    """Conservative app/DB clock agreement measurement, not a claim of external NTP proof."""
    begin, mono = wall(), monotonic()
    with store.connect() as conn:
        stamp = conn.execute("SELECT clock_timestamp() AS stamp").fetchone()["stamp"]
    elapsed, end = monotonic() - mono, wall()
    uncertainty = elapsed * 500
    jump = abs((end - begin).total_seconds() - elapsed) * 1000
    midpoint = begin + timedelta(seconds=elapsed / 2)
    return ClockSample((stamp - midpoint).total_seconds() * 1000, uncertainty + jump, end)


class Gate1Runtime:
    def __init__(
        self, store: JevStore, gate1: Gate1Inputs, *, credential_slot, worker_id, clock_health
    ):
        if not isinstance(gate1, Gate1Inputs) or not callable(clock_health):
            raise ValueError("EXPLICIT_GATE1_AND_CLOCK_PROVIDER_REQUIRED")
        if not isinstance(credential_slot, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,47}", credential_slot
        ):
            raise ValueError("NONSECRET_CREDENTIAL_SLOT_REQUIRED")
        store.check_role()
        self.store, self.inputs = store, Gate1Inputs(gate1.values).values
        self.worker_id, self.clock_health = worker_id, clock_health
        with store.connect() as conn:
            self.scope_id = conn.execute(
                "SELECT lab.register_review_scope(%s,%s) AS scope",
                (credential_slot, Jsonb(self.inputs)),
            ).fetchone()["scope"]
        self.policy = ReliabilityPolicy(
            self.inputs["version"],
            self.inputs["overall_review_deadline_seconds"],
            self.inputs["max_attempts"],
            self.inputs["backoff_base_ms"] / 1000,
            self.inputs["breaker_failure_threshold"],
            self.inputs["breaker_cooldown_seconds"],
        )

    def healthy_clock(self, now):
        try:
            sample = self.clock_health()
            return (
                isinstance(sample, ClockSample)
                and all(math.isfinite(v) for v in (sample.offset_ms, sample.uncertainty_ms))
                and sample.uncertainty_ms >= 0
                and abs(sample.offset_ms) + sample.uncertainty_ms
                <= self.inputs["max_clock_offset_ms"]
                and -self.inputs["max_clock_offset_ms"] / 1000
                <= (now - sample.observed_at).total_seconds()
                <= self.inputs["heartbeat_period_seconds"]
            )
        except Exception:
            return False

    def permit(self, request_id, attempt, *, probe, now):
        if not self.healthy_clock(now):
            return {"allowed": False, "reason": "CLOCK_UNHEALTHY"}
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT lab.review_attempt_permit(%s,%s,%s,%s) AS value",
                (self.scope_id, request_id, attempt, probe),
            ).fetchone()["value"]

    def identity(self):
        return {"runtime_scope": self.scope_id, "review_worker_id": str(self.worker_id)}

    def heartbeat(self, status):
        with self.store.connect() as conn:
            conn.execute(
                "SELECT lab.review_heartbeat(%s,%s,%s)", (self.worker_id, self.scope_id, status)
            )

    def state(self):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT lab.review_breaker_state(%s) AS value", (self.scope_id,)
            ).fetchone()["value"]

    def probe_due(self, now):
        state = self.state()
        if state["state"] == "CLOSED":
            return False
        for field in ("blocked_until", "next_probe_at"):
            if state[field] and now < datetime.fromisoformat(state[field]):
                return False
        return not state["probe_id"] or now >= datetime.fromisoformat(state["probe_until"])

    def calls_today(self, now):
        """Jev call volume since local NY midnight: attempts (``jev_requests`` started,
        one per ``jev_review`` call including health probes) and receipts (``jev_receipts``
        recorded, which can exceed attempts because of retries). Visibility only -- the
        owner set no daily cap (docs/CONTRACT-RESOLUTIONS.md, 2026-09-26). Counts every
        scope and credential slot in this database; only one engine is ever live against
        it, so this is the whole picture in practice.
        """
        midnight = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT (SELECT count(*) FROM lab.jev_requests WHERE created_at>=%(since)s) "
                "AS attempts, (SELECT count(*) FROM lab.jev_receipts WHERE "
                "completed_at>=%(since)s) AS receipts",
                {"since": midnight},
            ).fetchone()
        return {"attempts": row["attempts"], "receipts": row["receipts"]}

    def retry_delay(self, attempt, jitter, retry_after, now, remaining):
        """Return (seconds, reason); None forbids retry. Caller persists the reason."""
        values = self.inputs
        bounded = (
            min(
                values["backoff_cap_ms"],
                values["backoff_base_ms"] * values["backoff_multiplier"] ** (attempt - 1),
            )
            / 1000
        )
        if not math.isfinite(jitter) or not 0 <= jitter <= 1:
            raise ValueError("INVALID_JITTER_SOURCE")
        delay = bounded * (
            values["jitter_lower_fraction"] + (1 - values["jitter_lower_fraction"]) * jitter
        )
        reason = None
        if retry_after is not None:
            try:
                if re.fullmatch(r"[0-9]+", retry_after):
                    minimum = float(retry_after)
                else:
                    parsed = parsedate_to_datetime(retry_after)
                    if parsed.tzinfo is None:
                        raise ValueError
                    minimum = max(0.0, (parsed - now).total_seconds())
                if not math.isfinite(minimum):
                    raise ValueError
                if minimum > values["backoff_cap_ms"] / 1000 or minimum >= remaining:
                    return None, "RETRY_AFTER_EXCEEDS_BUDGET"
                delay = max(delay, minimum)
            except (TypeError, ValueError, OverflowError):
                reason = "INVALID_RETRY_AFTER"
        if delay >= remaining:
            return None, "REVIEW_DEADLINE_EXCEEDED"
        return delay, reason

    async def probe(self, reviewer, now):
        return await reviewer.jev_review(
            request_id=uuid4(),
            identity={"health_probe": True, "diagnostic_id": str(uuid4())},
            state=HEALTH_STATE,
            question_set=HEALTH_QUESTIONS,
            expires_at=now + timedelta(seconds=self.inputs["overall_review_deadline_seconds"]),
            purpose="ENGINEERING_TEST",
        )
