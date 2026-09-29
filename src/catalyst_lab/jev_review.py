"""The only TypeSafe evaluation transport. Reviews never authorize broker mutations."""

import asyncio
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from catalyst_lab.jev_contract import (
    INSUFFICIENT,
    JEV_ENDPOINT,
    JEV_MODEL,
    digest,
    encoded,
    strict_json,
    validated_answers,
)
from catalyst_lab.jev_secrets import CredentialUnavailable, typesafe_key
from catalyst_lab.jev_store import JudgmentProjectionFailed


@dataclass(frozen=True)
class ReliabilityPolicy:
    version: str
    deadline_seconds: float
    max_attempts: int
    backoff_seconds: float
    failure_threshold: int
    cooldown_seconds: float

    def __post_init__(self):
        if (
            not self.version
            or any(
                not math.isfinite(v) or v <= 0
                for v in (self.deadline_seconds, self.backoff_seconds, self.cooldown_seconds)
            )
            or any(type(v) is not int or v < 1 for v in (self.max_attempts, self.failure_threshold))
        ):
            raise ValueError("EXPLICIT_RELIABILITY_POLICY_REQUIRED")


@dataclass(frozen=True)
class ReviewResult:
    request_id: str
    status: str
    reason: str | None
    receipt_ids: tuple[str, ...]
    answers: dict


def _privacy_check(state_json):
    # Defense in depth. Bundle admission (next gate) must enforce a market-only field allowlist.
    forbidden = (
        r"(?i)\b(?:apikey_|ts_|sk-|pk_)[a-z0-9_\-]{12,}",
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r'(?i)"(?:api_key|secret|password|account_number|account_id|wallet_address|email|owner_name)"\s*:',
        r"\b0x[0-9a-fA-F]{40}\b",
    )
    if any(re.search(pattern, state_json) for pattern in forbidden):
        raise ValueError("SENSITIVE_EVIDENCE_REJECTED")
    if len(state_json.encode()) > 12000:
        raise ValueError("EVIDENCE_TOO_LONG")


class JevReviewer:
    def __init__(
        self,
        store,
        policy: ReliabilityPolicy,
        *,
        key_provider=typesafe_key,
        transport=None,
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        sleep=asyncio.sleep,
        jitter=random.random,
        runtime=None,
    ):
        store.check_role()
        self.store, self.policy, self._key_provider = store, policy, key_provider
        self._transport, self.clock, self.monotonic = transport, clock, monotonic
        self.sleep, self.jitter = sleep, jitter
        self.runtime = runtime
        if runtime is not None and (
            policy != runtime.policy or store.database_url != runtime.store.database_url
        ):
            raise ValueError("RUNTIME_POLICY_BINDING_MISMATCH")

    async def jev_review(self, *, request_id, identity, state, question_set, expires_at, purpose):
        if expires_at.tzinfo is None:
            raise ValueError("UTC_DEADLINE_REQUIRED")
        start_mono = self.monotonic()
        state_json = encoded(state)
        _privacy_check(state_json)
        # Freeze the caller-owned object before persistence and transmission.
        request_json = (
            '{"model":'
            + encoded(JEV_MODEL)
            + ',"state":'
            + state_json
            + ',"questions":'
            + encoded(question_set.questions)
            + "}"
        )
        key = None
        try:
            key = self._key_provider()
            if not key or not isinstance(key, str) or any(c.isspace() for c in key):
                raise CredentialUnavailable()
        except Exception:
            key = None
        if key and (key in request_json or key in encoded(identity)):
            raise ValueError("SENSITIVE_EVIDENCE_REJECTED")
        # Identity is binding metadata, deliberately excluded from the outbound provider state.
        identity_json = {
            **identity,
            "reliability_policy_version": self.policy.version,
            **(self.runtime.identity() if self.runtime else {}),
        }
        if not self.store.start(
            request_id,
            identity_json,
            question_set,
            digest(state_json),
            request_json,
            expires_at,
            purpose,
        ):
            return ReviewResult(str(request_id), "NEEDS_REVIEW", "DUPLICATE_REVIEW", (), {})
        receipts = []

        def remaining():
            return min(
                (expires_at - self.clock()).total_seconds(),
                self.policy.deadline_seconds - (self.monotonic() - start_mono),
            )

        def save(
            attempt,
            outcome,
            status=None,
            raw=None,
            actual=None,
            started=None,
            latency=0,
            code=None,
            answers=None,
        ):
            now = self.clock()
            try:
                receipt = self.store.finish(
                    request_id,
                    attempt,
                    outcome,
                    status,
                    raw,
                    actual,
                    started or now,
                    now,
                    latency,
                    code,
                    answers or {},
                )
            except JudgmentProjectionFailed as exc:
                receipts.append(exc.receipt_id)
                return ReviewResult(
                    str(request_id),
                    "NEEDS_REVIEW",
                    "JUDGMENT_PROJECTION_FAILED",
                    tuple(receipts),
                    {},
                )
            receipts.append(receipt)
            uncertain = any(
                answer.get("choice") in {INSUFFICIENT, "NEEDS_REVIEW"}
                or (
                    answer.get("type") == "choice"
                    and sum(
                        value == max(answer["probabilities"].values())
                        for value in answer["probabilities"].values()
                    )
                    > 1
                )
                for answer in (answers or {}).values()
            )
            return ReviewResult(
                str(request_id),
                "RECORDED" if outcome == "VALID" and not uncertain else "NEEDS_REVIEW",
                "UNCERTAIN_JUDGMENT" if uncertain else code,
                tuple(receipts),
                answers or {},
            )

        if key is None:
            return save(0, "CREDENTIAL_UNAVAILABLE", code="CREDENTIAL_UNAVAILABLE")
        probe = bool(identity.get("health_probe")) if self.runtime else False
        max_attempts = (
            self.runtime.inputs["half_open_max_attempts"] if probe else self.policy.max_attempts
        )
        # A 200 whose body fails validation (for example INVALID_DISTRIBUTION_SUM: 3 of 24
        # real calls on 2026-09-25) is retried like 429/529, but once per review: one further
        # attempt inside max_attempts and the deadline, with its own receipt. The invalid
        # response itself is retained as received and never normalised into a judgment.
        invalid_retry_left = True
        # A transport failure (the attempt timeout included) is retried the same way, once per
        # review (2026-09-28: 20-38% of the first live maintenance reviews stalled past the
        # 3 s attempt budget while the rest answered in about 0.5 s). Each attempt still needs
        # its own permit, so the breaker and the deadline bound it exactly as before.
        transport_retry_left = True
        async with httpx.AsyncClient(
            transport=self._transport, follow_redirects=False, trust_env=False
        ) as client:
            for attempt in range(1, max_attempts + 1):
                if remaining() <= 0:
                    return save(attempt, "EXPIRED", code="REVIEW_DEADLINE_EXCEEDED")
                attempt_budget = remaining()
                if self.runtime:
                    permit = self.runtime.permit(request_id, attempt, probe=probe, now=self.clock())
                    if not permit["allowed"]:
                        return save(attempt, "CIRCUIT_OPEN", code=permit["reason"])
                    attempt_budget = min(
                        attempt_budget,
                        self.runtime.inputs["batch_timeout_seconds"],
                        (
                            datetime.fromisoformat(permit["expires_at"]) - self.clock()
                        ).total_seconds(),
                    )
                    if attempt_budget <= 0:
                        return save(attempt, "EXPIRED", code="REVIEW_DEADLINE_EXCEEDED")
                elif self.store.circuit_open(
                    self.clock(), self.policy.failure_threshold, self.policy.cooldown_seconds
                ):
                    return save(attempt, "CIRCUIT_OPEN", code="CIRCUIT_OPEN")
                started, call_mono = self.clock(), self.monotonic()
                status, raw, actual, answers, code = None, None, None, {}, None
                retry_after = None
                try:
                    # asyncio bounds total latency; httpx alone only bounds individual IO phases.
                    async with asyncio.timeout(attempt_budget):
                        response = await client.post(
                            JEV_ENDPOINT,
                            content=request_json.encode(),
                            headers={
                                "Authorization": "Bearer " + key,
                                "Content-Type": "application/json",
                            },
                            timeout=attempt_budget,
                        )
                    status, raw = response.status_code, response.content
                    retry_after = response.headers.get("Retry-After")
                    outcome = "HTTP_ERROR"
                    if key.encode() in raw:
                        # Exact receipt goes to no storage if a provider echoes the secret.
                        raw, code, outcome = None, "CREDENTIAL_ECHO_BLOCKED", "INVALID_RESPONSE"
                    elif remaining() <= 0:
                        code, outcome = "REVIEW_DEADLINE_EXCEEDED", "EXPIRED"
                    elif status == 200:
                        try:
                            body = strict_json(raw)
                            if isinstance(body, dict) and isinstance(body.get("model"), str):
                                actual = body["model"]
                            answers = validated_answers(raw, question_set)
                            actual, outcome = JEV_MODEL, "VALID"
                        except (ValueError, TypeError, KeyError, UnicodeError):
                            code, outcome = "INVALID_PROVIDER_RESPONSE", "INVALID_RESPONSE"
                    else:
                        code = f"HTTP_{status}"
                except (httpx.HTTPError, TimeoutError):
                    outcome, code = "TRANSPORT_FAILURE", "PROVIDER_TRANSPORT_FAILURE"
                latency = max(0, (self.monotonic() - call_mono) * 1000)
                result = save(
                    attempt, outcome, status, raw, actual, started, latency, code, answers
                )
                invalid = code == "INVALID_PROVIDER_RESPONSE" and invalid_retry_left
                transport = outcome == "TRANSPORT_FAILURE" and transport_retry_left
                if (
                    outcome == "VALID"
                    or (status not in {429, 529} and not invalid and not transport)
                    or attempt == max_attempts
                ):
                    return result
                if invalid:
                    invalid_retry_left = False
                if transport:
                    transport_retry_left = False
                if self.runtime:
                    delay, retry_code = self.runtime.retry_delay(
                        attempt, self.jitter(), retry_after, self.clock(), remaining()
                    )
                    if retry_code:
                        self.store.control(request_id, retry_code)
                    if delay is None:
                        return ReviewResult(
                            str(request_id), "NEEDS_REVIEW", retry_code, tuple(receipts), {}
                        )
                else:
                    delay = self.policy.backoff_seconds * 2 ** (attempt - 1) * (1 + self.jitter())
                    if delay >= remaining():
                        return save(attempt + 1, "EXPIRED", code="REVIEW_DEADLINE_EXCEEDED")
                await self.sleep(delay)
        raise RuntimeError("UNREACHABLE_REVIEW_STATE")
