"""Hash-chained audit export verification and credential-shape detection.

``verify_events`` checks a contiguous segment of ``lab.trade_events`` rows. A full export
starts at the genesis (sequence 1 after the zero hash). An incremental export starts
after an event that an earlier checkpoint retained: its first row must follow
``start_seq`` and chain to ``start_hash``, so a missing segment or a changed earlier event
is detected, and an independently retained head still detects tail truncation.

``CREDENTIAL_PATTERNS`` is the one list of credential shapes shared by the repository
hygiene test and the audit checkpoint writer, which refuses to write a matching file.
"""

import hashlib
import json
import re
from collections.abc import Iterable

ZERO_HASH = "0" * 64
_HASH = re.compile(r"[0-9a-f]{64}")

# Tuned to skip fixtures (PKFIXTURE…, test-only tokens) and snake_case identifiers.
CREDENTIAL_PATTERNS = {
    # Alpaca key IDs are "PK" + 18 upper-case alphanumerics; fixtures use PKFIXTURE… / PKTEST….
    "alpaca_key_id": re.compile(r"(?<![A-Z0-9])PK(?!FIXTURE|TEST)[A-Z0-9]{18}(?![A-Z0-9])"),
    "alpaca_secret_assignment": re.compile(
        r"APCA_API_SECRET_KEY[ \t]*[=:][ \t]*['\"]?[A-Za-z0-9/+]{30,}"
    ),
    "typesafe_assignment": re.compile(r"TYPESAFE_API_KEY[ \t]*[=:][ \t]*['\"]?[A-Za-z0-9_-]{16,}"),
    # Provider-style keys: prefix then a long purely alphanumeric tail (no snake_case words).
    "provider_key": re.compile(
        r"(?<![A-Za-z0-9_-])(?:sk|ts|apikey)[-_][A-Za-z0-9]{32,}(?![A-Za-z0-9_-])"
    ),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{32,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
# A bearer-shaped match containing one of these words is a documented placeholder.
FIXTURE_MARKERS = ("fixture", "test-only", "replace", "required", "example", "placeholder")


def credential_matches(text: str):
    """Yield ``(pattern name, match)`` for every credential-shaped string in ``text``."""
    for name, pattern in CREDENTIAL_PATTERNS.items():
        for match in pattern.finditer(text):
            lowered = match.group(0).lower()
            if name == "bearer_token" and any(m in lowered for m in FIXTURE_MARKERS):
                continue
            yield name, match


def credential_findings(text: str) -> list[str]:
    """Sorted names of the credential patterns found in ``text``; values are never kept."""
    return sorted({name for name, _ in credential_matches(text)})


def verify_events(
    events: Iterable[dict],
    expected_head: str | None = None,
    *,
    start_seq: int = 0,
    start_hash: str | None = None,
) -> dict:
    """Verify a full or incremental export; an independently retained head detects tail
    truncation.

    ``start_seq``/``start_hash`` name the last event before the segment (sequence 0 and
    the zero hash for a full export): the first row must be ``start_seq + 1`` and chain to
    ``start_hash``. An empty segment is valid and its head is ``start_hash``.
    """
    if isinstance(start_seq, bool) or not isinstance(start_seq, int) or start_seq < 0:
        raise ValueError("Invalid export start sequence")
    if start_hash is None:
        if start_seq:
            raise ValueError("An incremental export needs the previous head hash")
        start_hash = ZERO_HASH
    if (
        not isinstance(start_hash, str)
        or not _HASH.fullmatch(start_hash)
        or (start_seq == 0) != (start_hash == ZERO_HASH)
    ):
        raise ValueError("Invalid export start hash")
    previous = start_hash
    count = 0
    for row in events:
        body = row["event_body"]
        envelope = json.loads(body)
        count += 1
        seq = start_seq + count
        if envelope["seq"] != seq or row["previous_hash"] != previous:
            raise ValueError(f"Broken event ordering at {seq}")
        for key in (
            "seq",
            "event_id",
            "candidate_id",
            "trade_id",
            "strategy_version",
            "event_type",
            "payload_json",
            "correction_of",
            "created_at",
        ):
            if row[key] != envelope[key]:
                raise ValueError(f"Envelope mismatch at {seq}: {key}")
        digest = hashlib.sha256((previous + body).encode()).hexdigest()
        if digest != row["event_hash"]:
            raise ValueError(f"Broken event hash at {seq}")
        previous = digest
    if expected_head is not None and expected_head != previous:
        raise ValueError("Export does not match the independently retained head")
    return {
        "valid": True,
        "event_count": count,
        "head_hash": previous,
        "start_seq": start_seq,
        "last_seq": start_seq + count,
    }
