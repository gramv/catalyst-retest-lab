"""``CRYPTO_MAINTENANCE_V1`` and ``CRYPTO_PARTIAL_ENTRY_V1``: Jev maintains open crypto trades.

Owner-approved plan ``docs/CRYPTO-AGENT-LOOP.md`` section 4.6 (2026-09-26; plan phase 5, package
maintenance, 2026-09-27). Named versions under the owner's 2026-09-24 ruling; every earlier
version (``JEV_MANAGED_EXITS_V1``, ``JEV_MANAGED_POSITION_CONTEXT_V3``/``QUESTIONS_V3``,
``JEV_MONITOR_SCHEDULING_ENGINEERING_V1``, the crypto protection plan) keeps its definition.

Scope. A crypto setup admitted from an ``AGENT_RESEARCH_REPORT_V3`` packet (the scope of
``SYSTEM_CHECK_V1`` and ``CRYPTO_ALPACA_TRIGGER_V1``). Admission records
``partial_entry_policy`` in its WATCHING state in both arms (plan 4.6.1 is how every trade
opens, so the control arm enters exactly as the maintained arm does) and
``maintenance_policy`` only when its randomized arm is ``JEV_MANAGED``; everything afterwards
keys off those state fields only (``active``/``partial_entry_active``). The ``FIXED_EXIT``
control arm (plan 4.6.6) is never maintained. V1, V2, B1, B2, operator engineering setups and
every crypto setup admitted before this version record neither and keep today's behaviour.

This module is pure: the rules, their numbers and their decisions, with exact Decimal
arithmetic. ``trade_maintenance`` (reviews, ledger), ``maintenance_dossier`` (what Jev reads)
and ``managed_execution`` (protection at the broker) apply them.

* Opening (4.6.1, ``CRYPTO_PARTIAL_ENTRY_V1``): a partial fill is protected at once for the
  filled quantity; the rest of the entry order keeps working for at most
  ``PARTIAL_ENTRY_MAX_SECONDS`` after the first fill, and is cancelled earlier once a fresh
  quote leaves the entry zone (ask above the max entry, or bid at or below the stop).
* Cadence (4.6.2): a review at every completed 15-minute bar, and at once on the first
  +1R, +2R, ... milestone, on price within 0.5% of the target or of the stop, on agent news
  about the coin, and on a 3% Bitcoin move within 15 minutes (every maintained trade, nearest
  to its stop first). Never more than once a minute per trade, except near the target.
* Options (code computes, Jev only chooses): stops = breakeven (once price has been at least
  1R above entry) and recent 15-minute and 1-hour swing lows above the current stop and at
  least 1% below the price; targets = 1-hour and 4-hour swing highs, the 24-hour high and the
  7-day high above the current target; at most five each, on the coin's price increment.
* Checks before applying (code): a new stop above the old stop and at least 0.5% below the
  current bid; a new target above the current price and the old target; the answer less than
  60 seconds old; the new level not crossed since the review was requested.

``CRYPTO_MAINTENANCE_V2`` (package answer-rules, 2026-09-27; owner direction the same day: Jev
monitors a day trade every minute, with the trade's context) is V1 with three changes, every
other number, option, check and the flag unchanged:

* Cadence: a review at every completed 1-minute bar (``review_bar_seconds`` 60, reason
  ``BAR_1M``) plus V1's events, with V1's once-a-minute floor per trade (near the target
  exempt). At most one review is in flight per trade: a minute that completes while the
  trade's previous review is still running is skipped and recorded
  (``MAINTENANCE_REVIEW_SKIPPED``, ``REVIEW_SKIPPED_IN_FLIGHT``), never reviewed late.
* Context ``JEV_MANAGED_POSITION_CONTEXT_V5`` and questions ``JEV_MANAGED_POSITION_QUESTIONS_V5``
  (``maintenance_dossier``): V4 plus the last 60 completed 1-minute bars and the trade's last 5
  maintenance reviews.
* Answer rule ``MAINTENANCE_ANSWER_RULE_V2`` (``maintenance_dossier.read_answer_v2``):
  ``action`` decides; an option answer is used only when the action raises that level and it
  is the unique most probable offered option; nothing is a contradiction.

Admission records V2 in the maintained arm from that package on (``ADMITTED_MAINTENANCE``); a
setup that recorded V1 keeps V1's cadence, context, questions and reader
(``recorded_policy`` dispatches on the recorded ``policy_id``).

``CRYPTO_MAINTENANCE_V3`` (package jev-budget, 2026-09-28; owner: a monthly Jev budget, with
Jev reviewing every open trade every minute while the budget allows it) is V2 with one change:
the routine cadence follows the owner's monthly Jev budget (``JEV_SPEND_GUARD_V1``,
``jev_budget``). The guard's tier at each maintenance pass sets it: ``NORMAL`` a review at every
completed 1-minute bar (V2's cadence, ``BAR_1M``), ``THROTTLED`` at every completed 5-minute
bar (``BAR_5M``), ``TIGHT`` at every completed 15-minute bar (``BAR_15M``); ``EXHAUSTED`` (or a
guard that cannot decide) no maintenance review at all, while protection, the stop and the
target run unchanged. Event-triggered reviews run in every tier below ``EXHAUSTED``. Context,
questions, answer rule, options, checks, the minute floor and the in-flight skip are V2's.
Admission records V3 in the maintained arm from that package on; setups that recorded V1 or V2
keep their recorded cadence whatever the budget.
"""

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from catalyst_lab import crypto_trigger
from catalyst_lab.account_risk import JEV_MANAGED_ARM

D = Decimal

MAINTENANCE_VERSION = "CRYPTO_MAINTENANCE_V1"
PARTIAL_ENTRY_VERSION = "CRYPTO_PARTIAL_ENTRY_V1"
CONTEXT_VERSION = "JEV_MANAGED_POSITION_CONTEXT_V4"
QUESTION_VERSION = "JEV_MANAGED_POSITION_QUESTIONS_V4"
EXIT_FLAG_VERSION = "EARLY_EXIT_FLAG_V1"
# CRYPTO_MAINTENANCE_V2 (package answer-rules): the minute cadence, context and questions V5 and
# MAINTENANCE_ANSWER_RULE_V2.
MAINTENANCE_V2_VERSION = "CRYPTO_MAINTENANCE_V2"
# CRYPTO_MAINTENANCE_V3 (package jev-budget): V2 with the routine cadence set by the monthly Jev
# budget's tier (JEV_SPEND_GUARD_V1): every 1, 5 or 15 completed minutes, or none when exhausted.
MAINTENANCE_V3_VERSION = "CRYPTO_MAINTENANCE_V3"
SPEND_GUARD_VERSION = "JEV_SPEND_GUARD_V1"
V3_THROTTLED_REVIEW_BAR_SECONDS = 300  # THROTTLED: a review at every completed 5-minute bar.
V3_TIGHT_REVIEW_BAR_SECONDS = 900  # TIGHT: a review at every completed 15-minute bar.
MAINTENANCE_ANSWER_RULE_V2 = "MAINTENANCE_ANSWER_RULE_V2"
CONTEXT_V5_VERSION = "JEV_MANAGED_POSITION_CONTEXT_V5"
QUESTION_V5_VERSION = "JEV_MANAGED_POSITION_QUESTIONS_V5"
CONTEXT_VERSIONS = (CONTEXT_VERSION, CONTEXT_V5_VERSION)
V2_REVIEW_BAR_SECONDS = 60  # A review at every completed 1-minute bar.
BARS_1M = 60  # The last hour of 1-minute bars (context V5).
REVIEW_HISTORY = 5  # The trade's last 5 maintenance reviews (context V5).

# --- The version's numbers (recorded in each maintained setup's state at admission) ----------
REVIEW_BAR_SECONDS = 900  # A review at every completed 15-minute bar.
NEAR_TARGET_FRACTION = D("0.005")  # Bid within 0.5% of the target.
NEAR_STOP_FRACTION = D("0.005")  # Bid within 0.5% of the stop.
MIN_REVIEW_INTERVAL_SECONDS = 60  # At most one review a minute per trade, except near target.
ANSWER_MAX_AGE_SECONDS = 60  # An answer is applied only while less than 60 s old.
REVIEW_DEADLINE_SECONDS = 10  # The approved position-review deadline (PositionMonitor's).
STOP_BID_MARGIN = D("0.005")  # A new stop at least 0.5% below the current bid.
SWING_LOW_PRICE_MARGIN = D("0.01")  # A swing-low option at least 1% below the price.
MAX_STOP_OPTIONS = 5
MAX_TARGET_OPTIONS = 5
SWING_SPAN = 2  # A swing low/high is lower/higher than the two bars on either side.
BENCHMARK_SYMBOL = "BTC/USD"
BENCHMARK_SHOCK_FRACTION = D("0.03")  # Bitcoin moving 3% ...
BENCHMARK_WINDOW_SECONDS = 900  # ... within 15 minutes.
PARTIAL_ENTRY_MAX_SECONDS = 600  # The rest of a partly filled entry works 10 minutes at most.
BARS_15M = 96  # 24 hours of 15-minute bars.
BARS_1H = 168  # 7 days of 1-hour bars.
SWING_LOW_15M_SECONDS = 86400  # Stop options: 15-minute swing lows of the last 24 hours ...
SWING_LOW_1H_SECONDS = 259200  # ... and 1-hour swing lows of the last 72 hours.
SWING_HIGH_SECONDS = 604800  # Target options: 1-hour and 4-hour swing highs of 7 days.
HIGH_24H_SECONDS = 86400
HIGH_7D_SECONDS = 604800
TIMEFRAMES = {"15Min": 900, "1Hour": 3600, "1Min": 60}  # 1Min: CRYPTO_MAINTENANCE_V2 only.
FOUR_HOURS = 14400

# Trigger kinds and the review reasons they give.
BAR_15M = "BAR_15M"
BAR_1M = "BAR_1M"  # CRYPTO_MAINTENANCE_V2: a completed 1-minute bar.
BAR_5M = "BAR_5M"  # CRYPTO_MAINTENANCE_V3 while throttled: a completed 5-minute bar.
BAR_REASONS = {900: BAR_15M, 300: BAR_5M, 60: BAR_1M}
# CRYPTO_MAINTENANCE_V2: a minute that completed while the trade's review was in flight.
MINUTE_SKIPPED_EVENT = "MAINTENANCE_REVIEW_SKIPPED"
REVIEW_SKIPPED_IN_FLIGHT = "REVIEW_SKIPPED_IN_FLIGHT"
R_MILESTONE = "R_MILESTONE"
NEAR_TARGET = "NEAR_TARGET"
NEAR_STOP = "NEAR_STOP"
NEWS = "AGENT_NEWS"
BTC_SHOCK = "BTC_SHOCK"
TRIGGER_EVENT = "MAINTENANCE_TRIGGER"
SHOCK_EVENT = "MAINTENANCE_BTC_SHOCK"

# Jev's answers (JEV_MANAGED_POSITION_QUESTIONS_V4) and the stop-replace paths.
HOLD, RAISE_STOP, RAISE_TARGET, RAISE_BOTH, FLAG = (
    "HOLD", "RAISE_STOP", "RAISE_TARGET", "RAISE_STOP_AND_TARGET", "FLAG_EARLY_EXIT")
ACTIONS = (HOLD, RAISE_STOP, RAISE_TARGET, RAISE_BOTH, FLAG)
KEEP = "KEEP"
PATCH_REPLACE, CANCEL_THEN_PLACE = "PATCH_REPLACE", "CANCEL_THEN_PLACE"
# The app's own stop while a raised stop is not yet at the broker: a fresh bid at or below it
# sells at market at once (plan 4.6.2, "Applying at Alpaca").
STOP_CROSSED_DURING_REPLACE = "STOP_CROSSED_DURING_REPLACE"

# Why the rest of a partly filled entry is cancelled (CRYPTO_PARTIAL_ENTRY_V1).
PARTIAL_ENTRY_TIMEOUT = "PARTIAL_ENTRY_TIMEOUT"
PARTIAL_ENTRY_ABOVE_MAX_ENTRY = "PARTIAL_ENTRY_ABOVE_MAX_ENTRY"
PARTIAL_ENTRY_AT_OR_BELOW_STOP = "PARTIAL_ENTRY_AT_OR_BELOW_STOP"
PARTIAL_ENTRY_FIRST_FILL_UNKNOWN = "PARTIAL_ENTRY_FIRST_FILL_UNKNOWN"
PARTIAL_ENTRY_PROTECTION_REFUSED = "PARTIAL_ENTRY_PROTECTION_REFUSED"

# Refusal codes of the checks before applying (the trade keeps its levels).
STOP_NOT_ABOVE_CURRENT = "STOP_NOT_ABOVE_CURRENT"
STOP_TOO_CLOSE_TO_BID = "STOP_TOO_CLOSE_TO_BID"
TARGET_NOT_ABOVE_PRICE = "TARGET_NOT_ABOVE_PRICE"
TARGET_NOT_ABOVE_CURRENT = "TARGET_NOT_ABOVE_CURRENT"
ANSWER_TOO_OLD = "ANSWER_TOO_OLD"
STOP_LEVEL_CROSSED = "STOP_LEVEL_CROSSED"
TARGET_LEVEL_CROSSED = "TARGET_LEVEL_CROSSED"
OFF_PRICE_INCREMENT = "OFF_PRICE_INCREMENT"
_PRECISION = 80


@dataclass(frozen=True)
class MaintenancePolicy:
    """The exact ``CRYPTO_MAINTENANCE_V1`` numbers; a state can never carry altered ones."""

    policy_id: str
    context_version: str
    question_version: str
    review_bar_seconds: int
    near_target_fraction: str
    near_stop_fraction: str
    min_review_interval_seconds: int
    answer_max_age_seconds: int
    review_deadline_seconds: int
    stop_bid_margin: str
    swing_low_price_margin: str
    max_stop_options: int
    max_target_options: int
    swing_span: int
    benchmark_symbol: str
    benchmark_shock_fraction: str
    benchmark_window_seconds: int
    exit_flag_version: str

    def __post_init__(self):
        if asdict(self) != _MAINTENANCE_VALUES:
            raise ValueError("EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED")

    def record(self):
        return asdict(self)


_MAINTENANCE_VALUES = {
    "policy_id": MAINTENANCE_VERSION,
    "context_version": CONTEXT_VERSION,
    "question_version": QUESTION_VERSION,
    "review_bar_seconds": REVIEW_BAR_SECONDS,
    "near_target_fraction": str(NEAR_TARGET_FRACTION),
    "near_stop_fraction": str(NEAR_STOP_FRACTION),
    "min_review_interval_seconds": MIN_REVIEW_INTERVAL_SECONDS,
    "answer_max_age_seconds": ANSWER_MAX_AGE_SECONDS,
    "review_deadline_seconds": REVIEW_DEADLINE_SECONDS,
    "stop_bid_margin": str(STOP_BID_MARGIN),
    "swing_low_price_margin": str(SWING_LOW_PRICE_MARGIN),
    "max_stop_options": MAX_STOP_OPTIONS,
    "max_target_options": MAX_TARGET_OPTIONS,
    "swing_span": SWING_SPAN,
    "benchmark_symbol": BENCHMARK_SYMBOL,
    "benchmark_shock_fraction": str(BENCHMARK_SHOCK_FRACTION),
    "benchmark_window_seconds": BENCHMARK_WINDOW_SECONDS,
    "exit_flag_version": EXIT_FLAG_VERSION,
}
CRYPTO_MAINTENANCE = MaintenancePolicy(**_MAINTENANCE_VALUES)


@dataclass(frozen=True)
class MaintenancePolicyV2(MaintenancePolicy):
    """The exact ``CRYPTO_MAINTENANCE_V2`` record: V1's numbers except ``review_bar_seconds``
    60, context and questions V5, its own ``policy_id`` and ``MAINTENANCE_ANSWER_RULE_V2``."""

    answer_rule: str

    def __post_init__(self):
        if asdict(self) != _MAINTENANCE_V2_VALUES:
            raise ValueError("EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED")


_MAINTENANCE_V2_VALUES = {**_MAINTENANCE_VALUES, "policy_id": MAINTENANCE_V2_VERSION,
                          "context_version": CONTEXT_V5_VERSION,
                          "question_version": QUESTION_V5_VERSION,
                          "review_bar_seconds": V2_REVIEW_BAR_SECONDS,
                          "answer_rule": MAINTENANCE_ANSWER_RULE_V2}
CRYPTO_MAINTENANCE_V2 = MaintenancePolicyV2(**_MAINTENANCE_V2_VALUES)


@dataclass(frozen=True)
class MaintenancePolicyV3(MaintenancePolicyV2):
    """The exact ``CRYPTO_MAINTENANCE_V3`` record: V2's (``review_bar_seconds`` 60 is the
    ``NORMAL`` tier's cadence) with its own ``policy_id``, the spend guard version whose tier
    sets the routine cadence, and the throttled and tight cadences."""

    spend_guard: str
    throttled_review_bar_seconds: int
    tight_review_bar_seconds: int

    def __post_init__(self):
        if asdict(self) != _MAINTENANCE_V3_VALUES:
            raise ValueError("EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED")

    def review_bar_seconds_for(self, tier):
        """The routine cadence in the guard's ``tier``; None (no maintenance review at all)
        for ``EXHAUSTED``, a guard that could not decide, or anything else."""
        return {"NORMAL": self.review_bar_seconds,
                "THROTTLED": self.throttled_review_bar_seconds,
                "TIGHT": self.tight_review_bar_seconds}.get(tier)


_MAINTENANCE_V3_VALUES = {**_MAINTENANCE_V2_VALUES, "policy_id": MAINTENANCE_V3_VERSION,
                          "spend_guard": SPEND_GUARD_VERSION,
                          "throttled_review_bar_seconds": V3_THROTTLED_REVIEW_BAR_SECONDS,
                          "tight_review_bar_seconds": V3_TIGHT_REVIEW_BAR_SECONDS}
CRYPTO_MAINTENANCE_V3 = MaintenancePolicyV3(**_MAINTENANCE_V3_VALUES)
# The version admission records in the maintained arm (package jev-budget: V3; package
# answer-rules recorded V2). Setups keep the version they recorded; V1's and V2's records,
# readers, cadences and stored events are unchanged.
ADMITTED_MAINTENANCE = CRYPTO_MAINTENANCE_V3
MAINTENANCE_VERSIONS = (MAINTENANCE_VERSION, MAINTENANCE_V2_VERSION, MAINTENANCE_V3_VERSION)


@dataclass(frozen=True)
class PartialEntryPolicy:
    policy_id: str
    max_remainder_seconds: int

    def __post_init__(self):
        if (self.policy_id != PARTIAL_ENTRY_VERSION
                or type(self.max_remainder_seconds) is not int
                or self.max_remainder_seconds != PARTIAL_ENTRY_MAX_SECONDS):
            raise ValueError("EXPLICIT_CRYPTO_PARTIAL_ENTRY_POLICY_REQUIRED")

    def record(self):
        return asdict(self)


CRYPTO_PARTIAL_ENTRY = PartialEntryPolicy(PARTIAL_ENTRY_VERSION, PARTIAL_ENTRY_MAX_SECONDS)


def applies(packet, arm):
    """Both versions apply to a report-V3 crypto setup in the maintained (JEV_MANAGED) arm."""
    return crypto_trigger.applies(packet) and arm == JEV_MANAGED_ARM


def admission_fields(packet, arm):
    """The state fields admission records for a report-V3 crypto setup: the partial-entry rule
    in both arms (plan 4.6.1 is how every trade opens, so the control arm enters exactly as
    the maintained arm does), maintenance (``ADMITTED_MAINTENANCE``: V2 from package
    answer-rules, V3 from package jev-budget) in the maintained arm only (plan 4.6.6). Every
    other setup records nothing."""
    if not crypto_trigger.applies(packet):
        return {}
    fields = {"partial_entry_policy": CRYPTO_PARTIAL_ENTRY.record()}
    if applies(packet, arm):
        fields = {"maintenance_policy": ADMITTED_MAINTENANCE.record(), **fields}
    return fields


def policy_from_record(record):
    """``CRYPTO_MAINTENANCE_V1``, ``_V2`` or ``_V3`` from its exact record (a state's
    ``maintenance_policy`` or a review context's ``policy``); anything else is refused."""
    if not isinstance(record, dict):
        raise ValueError("EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED")
    if record.get("policy_id") == MAINTENANCE_V3_VERSION:
        try:
            return MaintenancePolicyV3(**record)
        except TypeError:  # A field missing or extra: never a V3 record.
            raise ValueError("EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED") from None
    if record.get("policy_id") == MAINTENANCE_V2_VERSION:
        return MaintenancePolicyV2(**record)
    return MaintenancePolicy(**record)


def guarded(policy):
    """Whether a recorded maintenance policy's routine cadence follows the spend guard (V3)."""
    return isinstance(policy, MaintenancePolicyV3)


def recorded_policy(state):
    """The maintenance policy recorded at admission (V1, V2 or V3), or None."""
    recorded = (state or {}).get("maintenance_policy") if isinstance(state, dict) else None
    return policy_from_record(recorded) if recorded else None


def active(state):
    """Whether a setup's state was admitted under ``CRYPTO_MAINTENANCE_V1``, ``_V2`` or
    ``_V3``."""
    return recorded_policy(state) is not None


def recorded_policy_id(state):
    """The recorded maintenance ``policy_id`` as written (for notices; never validated), or
    None."""
    recorded = (state or {}).get("maintenance_policy") if isinstance(state, dict) else None
    return recorded.get("policy_id") if isinstance(recorded, dict) else None


def partial_entry_active(state):
    recorded = (state or {}).get("partial_entry_policy") if isinstance(state, dict) else None
    return recorded is not None and PartialEntryPolicy(**recorded) == CRYPTO_PARTIAL_ENTRY


def partial_entry_cancel_reason(*, opened_at, now, bid, ask, max_entry, stop):
    """Why the rest of a partly filled entry must be cancelled now, or None to keep it.

    ``opened_at`` is the first fill; ``bid``/``ask`` a fresh quote, or None (then only the
    ten-minute bound applies). Leaving the entry zone: the ask above the max entry, or the bid
    at or below the stop.
    """
    if opened_at is None:
        return PARTIAL_ENTRY_FIRST_FILL_UNKNOWN
    if (now - opened_at).total_seconds() >= PARTIAL_ENTRY_MAX_SECONDS:
        return PARTIAL_ENTRY_TIMEOUT
    if ask is not None and ask > max_entry:
        return PARTIAL_ENTRY_ABOVE_MAX_ENTRY
    if bid is not None and bid <= stop:
        return PARTIAL_ENTRY_AT_OR_BELOW_STOP
    return None


# --- Exact arithmetic helpers --------------------------------------------------------------------


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("NONFINITE_MARKET_NUMBER")
    return result


def floor_grid(price, increment):
    """The highest multiple of ``increment`` at or below ``price`` (a stop never rounds up)."""
    with localcontext() as context:
        context.prec = _PRECISION
        grid = (price / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
        return grid.quantize(increment)  # Written with the increment's places (101.40).


def ceil_grid(price, increment):
    """The lowest multiple of ``increment`` at or above ``price``."""
    with localcontext() as context:
        context.prec = _PRECISION
        grid = (price / increment).to_integral_value(rounding=ROUND_CEILING) * increment
        return grid.quantize(increment)


def on_grid(price, increment):
    with localcontext() as context:
        context.prec = _PRECISION
        return price % increment == 0


def floor_time(at, seconds):
    """The UTC boundary of ``seconds`` at or before ``at`` (bars are UTC-aligned)."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((at.astimezone(UTC) - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - elapsed % seconds)


def r_per_coin(levels):
    """The initial risk per coin, max entry minus the initial stop (plan 4.6.1 and 4.8)."""
    m, s = number(levels["max_entry_price"]), number(levels["stop"])
    if not m > s > 0:
        raise ValueError("INVALID_INITIAL_RISK")
    return m - s


def milestone(bid, entry, risk):
    """The highest whole R above entry the bid has reached (0 at or below +1R)."""
    if risk <= 0 or bid <= entry:
        return 0
    with localcontext() as context:
        context.prec = _PRECISION
        return int(((bid - entry) / risk).to_integral_value(rounding=ROUND_FLOOR))


def near_target(bid, target):
    """The bid within 0.5% of the target: bid >= target x (1 - 0.005)."""
    with localcontext() as context:
        context.prec = _PRECISION
        return bid >= target * (1 - NEAR_TARGET_FRACTION)


def near_stop(bid, stop):
    """The bid within 0.5% of the stop (above it): bid <= stop x (1 + 0.005)."""
    with localcontext() as context:
        context.prec = _PRECISION
        return stop < bid <= stop * (1 + NEAR_STOP_FRACTION)


def stop_distance(bid, stop):
    """How far the bid is above the stop, as a fraction of the bid (for nearest-first order)."""
    with localcontext() as context:
        context.prec = _PRECISION
        return (bid - stop) / bid


def in_r(value, risk):
    """``value`` in R, exact to 80 digits (callers round for reading only)."""
    with localcontext() as context:
        context.prec = _PRECISION
        return value / risk


def fraction(value, base):
    with localcontext() as context:
        context.prec = _PRECISION
        return value / base


# --- Bars: swing points, 4-hour aggregation, highs -----------------------------------------------


def completed(bars, now):
    """Completed bars only (end at or before now), oldest first, one per start."""
    seen, result = set(), []
    for bar in sorted(bars, key=lambda b: (b.start_at, b.source_id)):
        if bar.end_at <= now and bar.start_at not in seen:
            seen.add(bar.start_at)
            result.append(bar)
    return result


def within(bars, now, seconds):
    """Bars that ended within the last ``seconds`` before ``now``."""
    start = now - timedelta(seconds=seconds)
    return [bar for bar in bars if start < bar.end_at <= now]


def swing_lows(bars, span=SWING_SPAN):
    """Bars whose low is strictly below the lows of the ``span`` bars on either side."""
    result = []
    for index in range(span, len(bars) - span):
        low = bars[index].low
        neighbours = bars[index - span:index] + bars[index + 1:index + 1 + span]
        if all(low < other.low for other in neighbours):
            result.append(bars[index])
    return result


def swing_highs(bars, span=SWING_SPAN):
    """Bars whose high is strictly above the highs of the ``span`` bars on either side."""
    result = []
    for index in range(span, len(bars) - span):
        high = bars[index].high
        neighbours = bars[index - span:index] + bars[index + 1:index + 1 + span]
        if all(high > other.high for other in neighbours):
            result.append(bars[index])
    return result


def aggregate(bars, seconds, *, now):
    """UTC-aligned bars of ``seconds`` built from shorter completed ``bars``; an unfinished
    bucket is dropped. A bucket's open is its first bar's, its close its last bar's."""
    from catalyst_lab.setup_scan import CompletedBar

    buckets = {}
    for bar in bars:
        buckets.setdefault(floor_time(bar.start_at, seconds), []).append(bar)
    result = []
    for start in sorted(buckets):
        end = start + timedelta(seconds=seconds)
        if end > now:
            continue
        group = sorted(buckets[start], key=lambda b: b.start_at)
        result.append(CompletedBar(
            start, end, group[0].open, max(b.high for b in group), min(b.low for b in group),
            group[-1].close, sum((b.volume for b in group), D(0)), group[0].provider,
            group[0].feed, f"AGGREGATED_{seconds}S:{group[0].source_id}", True,
        ))
    return result


def highest(bars):
    """The bar with the highest high (the latest on a tie), or None."""
    best = None
    for bar in bars:
        if best is None or bar.high >= best.high:
            best = bar
    return best


# --- Options (code computes; Jev only chooses) ---------------------------------------------------


@dataclass(frozen=True)
class LevelOption:
    option_id: str
    kind: str  # "stop" or "target"
    price: D
    bases: tuple[str, ...]
    bar_end: datetime | None
    source_ids: tuple[str, ...] = field(default=())

    def record(self):
        return {"option_id": self.option_id, "kind": self.kind, "price": str(self.price),
                "bases": list(self.bases),
                "bar_end": self.bar_end.isoformat() if self.bar_end else None,
                "source_ids": list(self.source_ids)}


def _merge(candidates, price, basis, bar):
    bases, bar_end, sources = candidates.get(price, ((), None, ()))
    if basis not in bases:
        bases = (*bases, basis)
    end = bar.end_at if bar is not None else None
    if end is not None and (bar_end is None or end > bar_end):
        bar_end = end
    if bar is not None and bar.source_id not in sources:
        sources = (*sources, bar.source_id)
    candidates[price] = (bases, bar_end, sources)


def stop_options(*, bars_15m, bars_1h, now, bid, entry, current_stop, best_bid, risk, increment):
    """At most five stop options, highest first (``S1``...).

    Breakeven (the average entry rounded up to the increment) once the best bid since entry has
    been at least 1R above entry, above the current stop and at least 0.5% below the bid (the
    apply rule); the 15-minute swing lows of the last 24 hours and the 1-hour swing lows of the
    last 72 hours, rounded down to the increment, above the current stop and at least 1% below
    the bid. Breakeven always keeps its place; the remaining places go to the highest swing lows.
    """
    candidates = {}
    breakeven = None
    if risk > 0 and best_bid is not None and best_bid >= entry + risk:
        price = ceil_grid(entry, increment)
        with localcontext() as context:
            context.prec = _PRECISION
            if current_stop < price <= bid * (1 - STOP_BID_MARGIN):
                breakeven = price
                _merge(candidates, price, "BREAKEVEN", None)
    for bars, seconds, basis in (
        (bars_15m, SWING_LOW_15M_SECONDS, "SWING_LOW_15M"),
        (bars_1h, SWING_LOW_1H_SECONDS, "SWING_LOW_1H"),
    ):
        for bar in swing_lows(within(completed(bars, now), now, seconds)):
            price = floor_grid(bar.low, increment)
            with localcontext() as context:
                context.prec = _PRECISION
                if current_stop < price <= bid * (1 - SWING_LOW_PRICE_MARGIN):
                    _merge(candidates, price, basis, bar)
    chosen = [breakeven] if breakeven is not None else []
    for price in sorted(candidates, reverse=True):
        if len(chosen) >= MAX_STOP_OPTIONS:
            break
        if price not in chosen:
            chosen.append(price)
    return [
        LevelOption(f"S{i}", "stop", price, *candidates[price])
        for i, price in enumerate(sorted(chosen, reverse=True), start=1)
    ]


def target_options(*, bars_15m, bars_1h, now, bid, current_target, increment):
    """At most five target options, nearest first (``T1``...).

    The 1-hour and 4-hour swing highs of the last 7 days (4-hour bars aggregated from the
    1-hour bars), the 24-hour high and the 7-day high, each rounded up to the increment and
    above the current target and the bid. The 24-hour and 7-day highs always keep their
    places; the remaining places go to the swing highs nearest above the current target.
    """
    bars_15m, bars_1h = completed(bars_15m, now), completed(bars_1h, now)
    candidates, fixed = {}, []

    def eligible(price):
        return price > current_target and price > bid

    for label, source, seconds in (
        ("HIGH_24H", bars_15m + bars_1h, HIGH_24H_SECONDS),
        ("HIGH_7D", bars_15m + bars_1h, HIGH_7D_SECONDS),
    ):
        bar = highest(within(source, now, seconds))
        if bar is not None:
            price = ceil_grid(bar.high, increment)
            if eligible(price):
                _merge(candidates, price, label, bar)
                if price not in fixed:
                    fixed.append(price)
    week = within(bars_1h, now, SWING_HIGH_SECONDS)
    for bars, basis in ((week, "SWING_HIGH_1H"),
                        (aggregate(week, FOUR_HOURS, now=now), "SWING_HIGH_4H")):
        for bar in swing_highs(bars):
            price = ceil_grid(bar.high, increment)
            if eligible(price):
                _merge(candidates, price, basis, bar)
    chosen = list(fixed)
    for price in sorted(candidates):
        if len(chosen) >= MAX_TARGET_OPTIONS:
            break
        if price not in chosen:
            chosen.append(price)
    return [
        LevelOption(f"T{i}", "target", price, *candidates[price])
        for i, price in enumerate(sorted(chosen), start=1)
    ]


# --- Checks before applying (code) ---------------------------------------------------------------


def check_change(*, old_stop, new_stop, old_target, new_target, bid, min_bid, max_bid,
                 answered_at, now, increment):
    """The first refusal code of one proposed change, or None when it may be applied.

    ``min_bid``/``max_bid`` are the lowest and highest bids seen since the review was requested
    (the current bid included); ``new_stop``/``new_target`` are None when unchanged.
    """
    age = (now - answered_at).total_seconds()
    if not 0 <= age < ANSWER_MAX_AGE_SECONDS:
        return ANSWER_TOO_OLD
    with localcontext() as context:
        context.prec = _PRECISION
        for price in (new_stop, new_target):
            if price is not None and not on_grid(price, increment):
                return OFF_PRICE_INCREMENT
        if new_stop is not None:
            if new_stop <= old_stop:
                return STOP_NOT_ABOVE_CURRENT
            if min_bid <= new_stop:
                return STOP_LEVEL_CROSSED
            if new_stop > bid * (1 - STOP_BID_MARGIN):
                return STOP_TOO_CLOSE_TO_BID
        if new_target is not None:
            if new_target <= bid:
                return TARGET_NOT_ABOVE_PRICE
            if new_target <= old_target:
                return TARGET_NOT_ABOVE_CURRENT
            if max_bid >= new_target:
                return TARGET_LEVEL_CROSSED
    return None


# --- The review schedule -------------------------------------------------------------------------


def due_reasons(*, now, opened_at, served_bar_end, unserved_triggers, news_revision,
                served_news_revision, shocks, last_requested_at,
                bar_seconds=REVIEW_BAR_SECONDS):
    """``(reasons, exempt)``: why a review is due now, and whether it may run inside the minute.

    ``unserved_triggers`` are ``(kind, detail)`` pairs recorded since the last review;
    ``shocks`` are the Bitcoin shocks since the last review that happened while the trade was
    open. A review is due for any reason; it waits while the last request is less than a minute
    old unless a near-target trigger is among its reasons. ``bar_seconds`` is the recorded
    version's cadence: 900 (V1, reason ``BAR_15M``) or 60 (V2, reason ``BAR_1M``); under V3 the
    spend guard's tier's (60, 300 with reason ``BAR_5M``, or 900).
    """
    reasons = []
    boundary = floor_time(now, bar_seconds)
    if boundary > opened_at and (served_bar_end is None or boundary > served_bar_end):
        reasons.append(BAR_REASONS[bar_seconds])
    for kind, _ in unserved_triggers:
        if kind not in reasons:
            reasons.append(kind)
    if news_revision is not None and served_news_revision is not None \
            and news_revision > served_news_revision:
        reasons.append(NEWS)
    if shocks:
        reasons.append(BTC_SHOCK)
    exempt = any(kind == NEAR_TARGET for kind, _ in unserved_triggers)
    if not reasons:
        return [], exempt
    if last_requested_at is not None and not exempt and (
        (now - last_requested_at).total_seconds() < MIN_REVIEW_INTERVAL_SECONDS
    ):
        return [], exempt
    return reasons, exempt


def in_flight_at(boundary, *, requested_at, decided_at):
    """``CRYPTO_MAINTENANCE_V2``: whether a trade's previous review (requested at
    ``requested_at``, decided at ``decided_at``, None while undecided) was still in flight when
    the bar ending at ``boundary`` completed; that minute is then skipped, never reviewed late."""
    if requested_at is None or requested_at >= boundary:
        return False
    return decided_at is None or decided_at >= boundary


def nearest_to_stop_first(items):
    """``items`` of ``(key, bid, stop)``; the keys ordered nearest to the stop first."""
    return [key for key, bid, stop in sorted(
        items, key=lambda item: (stop_distance(item[1], item[2]), str(item[0])))]


# --- Bitcoin benchmark window --------------------------------------------------------------------


class BenchmarkWindow:
    """Bitcoin prices of the last 15 minutes, from the runtime's authenticated stream.

    Each second keeps its low, high and last price (quote mids and trade prints). ``check``
    finds a move of at least 3% between the latest price and any price of the window: at least
    3% above its lowest (UP) or 3% below its highest (DOWN). After a shock the window restarts
    at the shock, so one move is one shock; a market gap or restart empties it. Thread-safe:
    the market stream writes, the maintenance pass reads.
    """

    def __init__(self, *, symbol=BENCHMARK_SYMBOL, window_seconds=BENCHMARK_WINDOW_SECONDS,
                 threshold=BENCHMARK_SHOCK_FRACTION):
        self.symbol, self.window_seconds, self.threshold = symbol, window_seconds, threshold
        self._seconds = deque()  # [second, low, high, last, last_at]
        self._since = None
        self._lock = threading.Lock()

    def observe(self, price, at):
        price = number(price)
        if price <= 0 or at.tzinfo is None:
            return
        second = at.astimezone(UTC).replace(microsecond=0)
        with self._lock:
            if self._since is not None and at <= self._since:
                return
            if self._seconds and self._seconds[-1][0] == second:
                row = self._seconds[-1]
                row[1], row[2] = min(row[1], price), max(row[2], price)
                if at >= row[4]:
                    row[3], row[4] = price, at
            elif not self._seconds or second > self._seconds[-1][0]:
                self._seconds.append([second, price, price, price, at])

    def reset(self, at=None):
        with self._lock:
            self._seconds.clear()
            self._since = at

    def samples(self):
        with self._lock:
            return len(self._seconds)

    def check(self, now):
        """The shock of the latest price against the window, or None; restarts after one."""
        with self._lock:
            start = now - timedelta(seconds=self.window_seconds)
            while self._seconds and self._seconds[0][0] < start.replace(microsecond=0):
                self._seconds.popleft()
            if not self._seconds:
                return None
            latest = self._seconds[-1]
            price, at = latest[3], latest[4]
            low = min(self._seconds, key=lambda row: (row[1], row[0]))
            high = max(self._seconds, key=lambda row: (row[2], row[0]))
            with localcontext() as context:
                context.prec = _PRECISION
                if price >= low[1] * (1 + self.threshold) and low[0] <= latest[0]:
                    direction, reference = "UP", low
                    move = (price - low[1]) / low[1]
                elif price <= high[2] * (1 - self.threshold) and high[0] <= latest[0]:
                    direction, reference = "DOWN", high
                    move = (high[2] - price) / high[2]
                else:
                    return None
            self._seconds.clear()
            self._since = at
            return {
                "symbol": self.symbol, "direction": direction,
                "reference_price": str(reference[1] if direction == "UP" else reference[2]),
                "reference_second": reference[0].isoformat(),
                "price": str(price), "price_at": at.isoformat(), "move_fraction": str(move),
                "threshold": str(self.threshold), "window_seconds": self.window_seconds,
                "detected_at": now.isoformat(),
            }
