from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from catalyst_lab.domain import Candidate, Decision, Evidence, Policy

NEW_YORK = ZoneInfo("America/New_York")
RISK_PCT = Decimal("0.01")
MAX_OPEN_RISK_PCT = Decimal("0.02")
DAILY_HALT_PCT = Decimal("0.03")


def reject(rule: str, reason: str) -> Decision:
    return Decision(False, rule, reason)


def validate(
    candidate: Candidate, evidence: Evidence | None, policy: Policy | None, now: datetime
) -> Decision:
    c, e = candidate, evidence
    if not c.stop < c.entry_trigger < c.target:
        return reject("INVALID_LEVELS", "Require stop < entry_trigger < target")
    if not c.entry_trigger <= c.max_entry_price < c.target:
        return reject("INVALID_MAX_ENTRY", "Require trigger <= max_entry_price < target")
    if c.target - c.max_entry_price < 2 * (c.max_entry_price - c.stop):
        return reject("MIN_REWARD_RISK", "Reward-to-risk at maximum entry must be at least 2")
    if e is None:
        return reject("DATA_FEED_FAILURE", "Market and broker evidence is unavailable")
    if policy is None:
        return reject("POLICY_NOT_CONFIGURED", "Operational validation thresholds are required")
    # Bad provider input must fail closed, including NaN and naive timestamps.
    times = [e.observed_at, e.official_open, e.official_close, e.quote_timestamp]
    numbers = [
        e.bid,
        e.ask,
        e.average_daily_dollar_volume,
        e.equity,
        e.start_of_day_equity,
        e.realized_pnl_today,
        e.open_unrealized_pnl,
        e.open_planned_risk,
    ]
    if any(t.tzinfo is None for t in times) or any(not n.is_finite() for n in numbers):
        return reject("DATA_FEED_FAILURE", "Invalid provider evidence")
    if (
        not e.feed_healthy
        or not e.data_provider
        or not e.data_feed
        or not 0 <= (now - e.observed_at).total_seconds() <= policy.evidence_max_age_seconds
    ):
        return reject("DATA_FEED_FAILURE", "Feed or account evidence is unhealthy or stale")
    if e.ticker != c.ticker or e.asset_class != "us_equity" or not e.tradable:
        return reject("ASSET_NOT_TRADABLE", "Require a tradable US equity matching the candidate")
    session = now.astimezone(NEW_YORK).date()
    if (
        e.session_date != session
        or not e.calendar_provider
        or e.official_open >= e.official_close
        or e.official_open.astimezone(NEW_YORK).date() != session
        or e.official_close.astimezone(NEW_YORK).date() != session
        or now >= e.official_close
    ):
        return reject("MARKET_SESSION_CLOSED", "No current exchange-calendar session remains")
    expiration = c.expires_at or e.official_close
    if not now < expiration <= e.official_close:
        return reject(
            "INVALID_EXPIRATION", "Expiry must be future and no later than official close"
        )
    if now >= e.official_close - timedelta(minutes=5):
        return reject("ENTRY_WINDOW_CLOSED", "No new entries at or after calendar flatten time")
    # Premarket receipt is permitted; order-session gating belongs to execution.
    age = (now - e.quote_timestamp).total_seconds()
    if not 0 <= age <= policy.quote_max_age_seconds:
        return reject("STALE_QUOTE", "Quote is stale or future-dated")
    if e.bid <= 0 or e.ask < e.bid:
        return reject("INVALID_QUOTE", "Require positive, uncrossed bid and ask")
    spread = (e.ask - e.bid) / ((e.ask + e.bid) / 2) * 10000
    if spread > policy.max_spread_bps:
        return reject("MAX_SPREAD", "Quoted spread exceeds the configured maximum")
    if e.average_daily_dollar_volume < policy.min_average_daily_dollar_volume:
        return reject("MIN_DOLLAR_VOLUME", "Average daily dollar volume is below minimum")
    if e.reconciled_session != session or e.unexplained_positions:
        return reject(
            "STARTUP_RECONCILIATION_REQUIRED", "Broker state is not explained this session"
        )
    if e.equity <= 0 or e.start_of_day_equity <= 0 or e.open_planned_risk < 0:
        return reject("INVALID_ACCOUNT_EVIDENCE", "Equity and risk evidence is invalid")
    if (
        e.daily_halted
        or e.realized_pnl_today + e.open_unrealized_pnl <= -DAILY_HALT_PCT * e.start_of_day_equity
    ):
        return reject("DAILY_RISK_HALT", "Realized plus unrealized daily loss reached the halt")
    if not e.sector or not e.theme:
        return reject(
            "CORRELATION_UNKNOWN", "Server-side sector and theme classification is required"
        )
    if e.sector in e.open_sectors or e.theme in e.open_themes:
        return reject("CORRELATION_LIMIT", "Sector or theme already has open exposure")
    distance = c.max_entry_price - c.stop
    qty = min(
        int((RISK_PCT * e.equity / distance).to_integral_value(rounding=ROUND_DOWN)),
        int((e.equity / c.max_entry_price).to_integral_value(rounding=ROUND_DOWN)),
    )
    if qty < 1:
        return reject("ZERO_SHARE_SIZE", "Risk budget cannot fund one whole share")
    planned = qty * distance
    if e.open_planned_risk + RISK_PCT * e.equity > MAX_OPEN_RISK_PCT * e.equity:
        return reject("MAX_OPEN_PLANNED_RISK", "Combined planned risk exceeds 2% of equity")
    return Decision(
        True, None, "Eligible for watching; no execution is authorized", expiration, qty, planned
    )


class EvidenceUnavailable(Exception):
    def __init__(self, rule, reason):
        self.rule, self.reason = rule, reason
