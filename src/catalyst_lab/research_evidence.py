"""Canonical external research evidence shared by intake and follow-up paths."""

import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.review_storage import SourceExcerpt

MAX_ERROR_DETAILS = 20
_FIELD_NAME = re.compile(r"[a-z_][a-z0-9_]{0,63}")


def field_path(prefix, parts=()):
    """Render a field location without echoing input: keys that are not schema-like
    field names (for example a submitted dictionary key) are replaced by ``*``."""
    path = prefix
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            path += f"[{part}]"
        else:
            name = part if isinstance(part, str) and _FIELD_NAME.fullmatch(part) else "*"
            path += ("." if path else "") + name
    return path


def screen_sensitive(text):
    """The credential/PII patterns of ``jev_review._privacy_check`` without its byte cap.

    ``_privacy_check`` tests every pattern before it measures length, so a length-only
    failure proves the patterns passed. Review states keep the full check; the dossier
    budget, not this screen, bounds what the reviewer receives.
    """
    try:
        _privacy_check(text)
    except ValueError as exc:
        if str(exc) != "EVIDENCE_TOO_LONG":
            raise


def sensitive_paths(value, prefix=""):
    """Paths of keys or strings matching a sensitive pattern; never the matched values."""
    try:
        screen_sensitive(encoded(value))
        return []
    except ValueError:
        pass
    found = []

    def flagged(leaf):
        try:
            screen_sensitive(encoded(leaf))
            return False
        except ValueError:
            return True

    def walk(node, path):
        if len(found) >= MAX_ERROR_DETAILS:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = field_path(path, (key,))
                if flagged({key: None}):
                    found.append(child_path)
                else:
                    walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
        elif isinstance(node, str) and flagged(node):
            found.append(path)

    walk(value, prefix)
    return found or [prefix]


def rationale_reference_errors(rationale, *, source_ids, bar_ids, prefix):
    """Every claim must cite at least one source or bar retained in the same item."""
    known = {"source_ids": set(source_ids), "bar_ids": set(bar_ids)}
    errors = []
    for index, claim in enumerate(rationale["claims"]):
        base = f"{prefix}.claims[{index}].supported_by"
        support = claim["supported_by"]
        if not support["source_ids"] and not support["bar_ids"]:
            errors.append({"path": base, "code": "CLAIM_SUPPORT_REQUIRED"})
        for kind in ("source_ids", "bar_ids"):
            seen = set()
            for position, reference in enumerate(support[kind]):
                if reference not in known[kind]:
                    code = "RATIONALE_REFERENCE_UNKNOWN"
                elif reference in seen:
                    code = "DUPLICATE_RATIONALE_REFERENCE"
                else:
                    code = None
                if code:
                    errors.append({"path": f"{base}.{kind}[{position}]", "code": code})
                seen.add(reference)
    return errors[:MAX_ERROR_DETAILS]


def canonical_source(value, *, now: datetime):
    raw = (
        asdict(value)
        if is_dataclass(value)
        else (value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value))
    )
    allowed = set(SourceExcerpt.model_fields) | {
        "content_hash",
        "primary_source",
        "asset_relevant",
        "novelty",
        "stance",
    }
    if set(raw) - allowed:
        raise ValueError("SOURCE_FIELDS_NOT_ALLOWED")
    for key in ("retrieved_at", "published_at"):
        if isinstance(raw.get(key), datetime):
            raw[key] = raw[key].isoformat()
    source = SourceExcerpt.model_validate({k: raw.get(k) for k in SourceExcerpt.model_fields})
    computed_hash = digest(source.excerpt)
    if raw.get("content_hash") not in {None, computed_hash}:
        raise ValueError("SOURCE_CONTENT_HASH_MISMATCH")
    retrieved = source.retrieved_at.astimezone(UTC)
    published = source.published_at.astimezone(UTC) if source.published_at else None
    if retrieved > now.astimezone(UTC) or (published is not None and published > retrieved):
        raise ValueError("INVALID_SOURCE_EVIDENCE")
    result = source.model_dump(mode="json")
    result["retrieved_at"] = retrieved.isoformat()
    result["published_at"] = published.isoformat() if published else None
    result["content_hash"] = computed_hash
    # Rich legacy annotations remain evidence metadata, never authority.
    for key in ("primary_source", "asset_relevant", "novelty", "stance"):
        if key in raw:
            result[key] = raw[key]
    if any(
        key in raw and type(raw[key]) is not bool for key in ("primary_source", "asset_relevant")
    ):
        raise ValueError("INVALID_SOURCE_ANNOTATION")
    if "novelty" in raw and raw["novelty"] not in {"NEW_FACT", "PREVIOUSLY_KNOWN", "UNVERIFIED"}:
        raise ValueError("INVALID_SOURCE_ANNOTATION")
    if "stance" in raw and raw["stance"] not in {"SUPPORTS", "ADVERSE", "NEUTRAL", "WITHDRAWN"}:
        raise ValueError("INVALID_SOURCE_ANNOTATION")
    _privacy_check(encoded(result))
    return result


def canonical_sources(values, *, now: datetime):
    rows = [canonical_source(value, now=now) for value in values]
    if not 1 <= len(rows) <= 8 or len({row["source_id"] for row in rows}) != len(rows):
        raise ValueError("INVALID_SOURCE_EVIDENCE")
    return rows


def canonical_technical_facts(value, *, now=None, allowed_source_ids=None):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {
        "observed_at",
        "timeframe",
        "summary",
        "facts",
        "source_ids",
    }:
        raise ValueError("INVALID_TECHNICAL_FACTS")
    observed = value.get("observed_at")
    if not isinstance(observed, str):
        raise ValueError("INVALID_TECHNICAL_FACTS")
    stamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("INVALID_TECHNICAL_FACTS")
    if now is not None and stamp.astimezone(UTC) > now.astimezone(UTC):
        raise ValueError("INVALID_TECHNICAL_FACTS")
    if not isinstance(value.get("timeframe"), str) or not value["timeframe"]:
        raise ValueError("INVALID_TECHNICAL_FACTS")
    if not isinstance(value.get("summary"), str) or not 1 <= len(value["summary"]) <= 2000:
        raise ValueError("INVALID_TECHNICAL_FACTS")
    facts = value.get("facts")
    if (
        not isinstance(facts, list)
        or not 1 <= len(facts) <= 32
        or any(not isinstance(fact, str) or not 1 <= len(fact) <= 300 for fact in facts)
    ):
        raise ValueError("INVALID_TECHNICAL_FACTS")
    source_ids = value.get("source_ids")
    if not isinstance(source_ids, list) or any(not isinstance(x, str) for x in source_ids):
        raise ValueError("INVALID_TECHNICAL_FACTS")
    if allowed_source_ids is not None and not set(source_ids).issubset(set(allowed_source_ids)):
        raise ValueError("INVALID_TECHNICAL_FACTS")
    result = {**value, "observed_at": stamp.astimezone(UTC).isoformat()}
    _privacy_check(encoded(result))
    return result
