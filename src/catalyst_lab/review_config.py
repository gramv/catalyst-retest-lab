"""Explicit testing policy inputs for the isolated Step 4 storage surface."""

import json
import os
from dataclasses import dataclass, field

from catalyst_lab.jev_secrets import typesafe_key

# Expected approved values, NOT runtime defaults: every field must be supplied.
APPROVED_GATE1 = {
    "version": "JEV_LIVE_REVIEW_POLICY_V1",
    "retry_http_statuses": [429, 529],
    "max_attempts": 3,
    "backoff_base_ms": 250,
    "backoff_multiplier": 2,
    "backoff_cap_ms": 2000,
    "jitter_mode": "EQUAL",
    "jitter_lower_fraction": 0.5,
    "retry_after_policy": "RESPECT_MINIMUM_OR_ABANDON",
    "breaker_scope": "PROVIDER_MODEL_CREDENTIAL_SLOT",
    "breaker_failure_threshold": 3,
    "breaker_failure_classes": [
        "HTTP_ERROR",
        "TRANSPORT_FAILURE",
        "TIMEOUT",
        "INVALID_RESPONSE",
        "MODEL_MISMATCH",
        "CREDENTIAL_ECHO",
    ],
    "breaker_cooldown_seconds": 30,
    "half_open_max_inflight": 1,
    "half_open_max_attempts": 1,
    "breaker_recovery_successes": 2,
    "recovery_probe_spacing_seconds": 1,
    "recovery_probe_policy": "SYNTHETIC_NON_AUTHORIZING",
    "question_timeout_seconds": 3,
    "batch_timeout_seconds": 3,
    "overall_review_deadline_seconds": 10,
    "evidence_max_age_seconds": 60,
    "context_max_age_seconds": 60,
    "expiry_grace_seconds": 0,
    "max_clock_offset_ms": 250,
    "same_candidate_max_active_chains": 1,
    "selection_policy": "EXACT_REVISION_FIRST_VALID_CONFLICT_NEEDS_REVIEW",
    "material_supersession_policy": "MATERIAL_REVISION_ONLY",
    "duplicate_policy": "AUDIT_REJECT_SAME_REQUEST_RETRY",
    "heartbeat_period_seconds": 5,
    "heartbeat_deadline_seconds": 15,
}
# JEV_LIVE_REVIEW_POLICY_V2 (owner ruling 2026-09-28, docs/CONTRACT-RESOLUTIONS.md): V1's exact
# values except the version and an 8-second question and batch (per-attempt) budget, inside
# V1's unchanged 10-second overall review deadline. Live per-minute maintenance reviews stalled
# past V1's 3-second budget in bursts while valid answers took about 0.5 s. Its runtime scope
# needs migration 025; V1 stays approved and unchanged.
APPROVED_GATE1_V2 = {
    **APPROVED_GATE1,
    "version": "JEV_LIVE_REVIEW_POLICY_V2",
    "question_timeout_seconds": 8,
    "batch_timeout_seconds": 8,
}
# Exactly these policies, each matched field for field; nothing in between is approved.
APPROVED_GATE1_POLICIES = (APPROVED_GATE1, APPROVED_GATE1_V2)


class ReviewConfigurationError(ValueError):
    pass


def _canonical(values):
    return json.dumps(values, sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class Gate1Inputs:
    values: dict

    def __post_init__(self):
        # Canonical serialization distinguishes booleans from numeric values, rejects NaN,
        # and detaches mutable caller objects. No default is substituted for missing fields.
        try:
            actual = _canonical(self.values)
            if actual not in {_canonical(policy) for policy in APPROVED_GATE1_POLICIES}:
                raise ValueError
        except (TypeError, ValueError):
            raise ReviewConfigurationError("GATE1_INPUTS_MISSING_OR_UNAPPROVED") from None
        object.__setattr__(self, "values", json.loads(actual))


@dataclass(frozen=True)
class ReviewSettings:
    database_url: str = field(repr=False)
    write_token: str = field(repr=False)
    read_token: str = field(repr=False)
    environment: str
    gate1: Gate1Inputs
    research_policy_id: str

    def __post_init__(self):
        if (
            not self.database_url
            or min(len(self.write_token), len(self.read_token)) < 32
            or self.write_token == self.read_token
        ):
            raise ReviewConfigurationError("REVIEW_DATABASE_AND_TOKEN_REQUIRED")
        if self.environment not in {"local", "railway"}:
            raise ReviewConfigurationError("REVIEW_ENVIRONMENT_REQUIRED")
        if self.research_policy_id != "MUSE_JEV_ACTIVE_V1":
            raise ReviewConfigurationError("RESEARCH_POLICY_ID_REQUIRED")

    @classmethod
    def from_env(cls):
        try:
            values = json.loads(os.environ["JEV_REVIEW_POLICY_JSON"])
            result = cls(
                os.environ["REVIEW_DATABASE_URL"],
                os.environ["REVIEW_WRITE_TOKEN"],
                os.environ["REVIEW_READ_TOKEN"],
                os.environ["CATALYST_ENVIRONMENT"],
                Gate1Inputs(values),
                os.environ["JEV_RESEARCH_POLICY_ID"],
            )
        except (KeyError, json.JSONDecodeError):
            raise ReviewConfigurationError("REQUIRED_REVIEW_CONFIGURATION_MISSING") from None
        if os.environ.get("RAILWAY_ENVIRONMENT_ID") and result.environment != "railway":
            raise ReviewConfigurationError("RAILWAY_ENVIRONMENT_REQUIRED")
        # Separate storage service: never accept a broker/risk capability, even unused.
        if (
            any(
                os.environ.get(name)
                for name in (
                    "RISK_DATABASE_URL",
                    "APCA_API_KEY_ID",
                    "APCA_API_SECRET_KEY",
                )
            )
            or os.environ.get("ALPACA_READ_ONLY_ENABLED", "0") != "0"
        ):
            raise ReviewConfigurationError("BROKER_CONFIGURATION_FORBIDDEN_ON_REVIEW_SERVICE")
        typesafe_key()  # Validate presence only; no provider call, logging or receipt.
        return result
