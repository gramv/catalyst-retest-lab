from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from catalyst_lab.domain import Candidate
from catalyst_lab.market import Observation, Session, timestamp


@dataclass(frozen=True)
class TriggerPolicy:
    max_spread_bps: Decimal = Decimal("10")
    feed_failure_tolerance_seconds: Decimal = Decimal("5")
    quote_max_age_seconds: int = field(default=5, init=False)

    def __post_init__(self):
        for value in (self.max_spread_bps, self.feed_failure_tolerance_seconds):
            if not value.is_finite() or value <= 0:
                raise ValueError("Trigger policy values must be positive and finite")
        if self.max_spread_bps > 10:
            raise ValueError("CATALYST_RETEST_V1 spread cannot exceed 10 bps")


def advance(
    candidate: Candidate,
    session: Session,
    context: dict,
    observation: Observation | None,
    now: datetime,
    feed_healthy: bool,
    policy: TriggerPolicy,
) -> tuple[dict, str | None]:
    """Frozen mechanical V1. No broker access or sizing; printed trades alone touch the level."""
    result = dict(context)
    state = result["state"]
    if state not in {"VALIDATED", "WATCHING"}:
        return result, None
    if now >= min(candidate.expires_at or session.closes, session.closes):
        result.update(state="EXPIRED_UNTRIGGERED", checked_at=now.isoformat())
        return result, "CANDIDATE_EXPIRED"
    if now >= session.flatten_time:
        result.update(state="EXPIRED_UNTRIGGERED", checked_at=now.isoformat())
        return result, "ENTRY_WINDOW_CLOSED"
    if state == "VALIDATED":
        if not session.contains(now) or not feed_healthy:
            return result, None
        result.update(
            state="WATCHING",
            started_at=now.isoformat(),
            touched=False,
            quote=None,
            last_trade_ns=None,
            last_quote_ns=None,
            failed_since=None,
        )
    if not session.contains(now):
        return result, None

    result["checked_at"] = now.isoformat()
    # Establish an outage from the old quote before accepting recovery data. Otherwise a
    # stalled worker could erase a long data gap by reading a fresh quote on restart.
    old_quote = Observation.from_json(result["quote"]) if result.get("quote") else None
    if old_quote and now > old_quote.at + timedelta(seconds=policy.quote_max_age_seconds):
        stale_since = old_quote.at + timedelta(seconds=policy.quote_max_age_seconds)
        if not result.get("failed_since") or stale_since < timestamp(result["failed_since"]):
            result["failed_since"] = stale_since.isoformat()
    valid = observation is not None and observation.ticker == candidate.ticker
    if valid:
        # Reject replay, pre-watch, future and delayed prints as unhealthy evidence, not triggers.
        age = (now - observation.at).total_seconds()
        valid = (
            timestamp(result["started_at"]) <= observation.at
            and 0 <= age <= policy.quote_max_age_seconds
        )
    if valid and observation.kind == "trade":
        previous = result.get("last_trade_ns")
        result["last_trade_ns"] = max(previous or 0, observation.ns)
        # A still-current out-of-order print cannot hide a stop breach. The repository
        # deduplicates exact observations; arrival ordering is not an extra strategy rule.
        if observation.price <= candidate.stop:
            result["state"] = "INVALIDATED"
            return result, "STOP_TRADED_BEFORE_TRIGGER"
        if observation.price <= candidate.entry_trigger:
            result.update(touched=True, touch=observation.to_json())
    if valid and observation.kind == "quote":
        previous = result.get("last_quote_ns")
        if previous is None or observation.ns > previous:
            result.update(quote=observation.to_json(), last_quote_ns=observation.ns)

    # Expired outages cannot be erased by a late reconnect / late fresh quote.
    if result.get("failed_since") and (now - timestamp(result["failed_since"])).total_seconds() >= (
        policy.feed_failure_tolerance_seconds
    ):
        result["state"] = "INVALIDATED"
        return result, "DATA_FEED_FAILURE"
    quote = Observation.from_json(result["quote"]) if result.get("quote") else None
    quote_current = (
        quote is not None
        and 0 <= (now - quote.at).total_seconds() <= policy.quote_max_age_seconds
        and quote.spread_bps is not None
    )
    if not feed_healthy or not quote_current:
        result["failed_since"] = result.get("failed_since") or now.isoformat()
        return result, None
    result["failed_since"] = None
    if not result.get("touched") or quote.spread_bps > policy.max_spread_bps:
        return result, None
    if quote.ask > candidate.max_entry_price:
        result["state"] = "INVALIDATED"
        return result, "PRICE_BEYOND_MAX_ENTRY"
    result["state"] = "TRIGGER_CONFIRMED"
    result["entry_intent"] = {
        "type": "limit",
        "side": "buy",
        "limit_price": str(candidate.max_entry_price),
        "stop": str(candidate.stop),
        "target": str(candidate.target),
        "time_in_force": "day",
        "order_class": "bracket",
    }
    return result, "CATALYST_RETEST_V1"
