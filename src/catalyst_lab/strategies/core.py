"""The decision-core seed (``STRATEGY_REGISTRY_V1``, package strategy-c1, 2026-10-03; plan
``docs/TRADING-QUALITY-PLAN.md`` section 5, move 2, and section 2 item 8 "backtest = shadow =
live").

Pure functions without I/O, shared by the live engine and every simulation, so a rule is written
once. This module imports nothing from the application: the live modules and the simulations
import it, never the other way round.

What is shared today:

* **The pullback touch** (``PULLBACK_V1``'s trigger rule): an entry is touched when a price is at
  or below the entry trigger, and the stop is reached when a price is at or below the stop
  (``touches_entry``, ``reaches_stop``). The live Alpaca trigger (``crypto_trigger.evaluate``)
  and, since package plugin-c3, the Coinbase reference trigger (``coinbase_trigger``) apply them
  to prints and quotes; the pick shadow simulation (``pick_outcomes.simulate_pick``) to a
  1-minute bar's low, stop first (``pullback_bar_event``).
* **The open position's stop and target touches** (package plugin-c3): ``reaches_stop`` decides
  every live stop-breach mark (``stop_breach``'s print and bid evidence, ``stop_execution``'s
  Coinbase print run, the engine's V1 bid mark, the partial-entry stop) and ``reaches_target``
  the target touch of the protection planner (``crypto_execution.plan_crypto_recovery``). Same
  comparisons as before, written once.
* **The exit walk** (``walk_to_exit``, moved here unchanged from ``pick_outcomes``): the first
  stop or target hit on bars, else the open of the first bar at or after the hold deadline.
* **R** (``r_values``, moved unchanged): P&L per unit over (entry - stop), and net of an assumed
  fee on both legs.
* **The trade-plan math** (``CRYPTO_TRADE_PLAN_V1``): the hourly-range floor of the stop
  (``range_floor``: entry trigger - 2 x hourly range) and the target cap (``target_cap``: entry
  trigger + 1.5 x (entry trigger - stop)). ``trade_plan.plan`` (live admission) calls both; the
  mechanical strategies' simulations call ``plan_levels``, which applies the same rule.
* **The breakout signal** (``BREAKOUT_7D_VOL2X_V1``, ``breakout_signals`` with
  ``BREAKOUT_V1_RULE``; the history tester's declared variants are other ``BreakoutRule``
  values through the same functions) and the marketable entry simulation
  (``simulate_marketable``).
* **The slippage model** (``HALF_SPREAD_PLUS_VOLATILITY_V1``, package strategy-c2):
  ``slippage_estimate`` and ``cost_r``, used by the strategy shadow and the history tester.

Decisions compare exact Decimals under an 80-digit context, as the live modules do.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext

D = Decimal
PRECISION = 80

# Entry types (``SYSTEM_CHECK_V1``'s classification of an entry trigger against the live mid).
PULLBACK, IMMEDIATE, BREAKOUT = "PULLBACK", "IMMEDIATE", "BREAKOUT"
ENTRY_TYPES = frozenset({PULLBACK, IMMEDIATE, BREAKOUT})

# Exit-walk outcomes (``PICK_SHADOW_OUTCOME_V1``'s names, unchanged).
STOP = "STOP"
TARGET = "TARGET"
HOLD_EXIT = "HOLD_24H_EXIT"
DATA_INCOMPLETE = "DATA_INCOMPLETE"

# CRYPTO_TRADE_PLAN_V1's numbers (``trade_plan`` pins the same values in its policy record).
STOP_RANGE_MULTIPLE = D(2)
TARGET_R_MULTIPLE = D("1.5")
MIN_STOP_FRACTION = D("0.02")
HOURLY_RANGE_HOURS = 24  # crypto_maintenance.HOURLY_RANGE_BARS
HOURLY_RANGE_MIN_BARS = 20  # crypto_maintenance.HOURLY_RANGE_MIN_BARS

HOUR = timedelta(hours=1)


class ShadowDataError(ValueError):
    """Malformed bar evidence; the caller keeps the outcome unknown (fail-closed)."""


# --- The pullback touch (PULLBACK_V1) -----------------------------------------------------------


def touches_entry(price, entry_trigger):
    """A price at or below the entry trigger touches a pullback entry."""
    return price <= entry_trigger


def reaches_stop(price, stop):
    """A price at or below the stop reaches it."""
    return price <= stop


def reaches_target(price, target):
    """A price at or above the target reaches it (a long position's take-profit)."""
    return price >= target


STOP_FIRST, TRIGGER = "STOP_FIRST", "TRIGGER"


def pullback_bar_event(bar, entry_trigger, stop):
    """``STOP_FIRST`` when the bar's low reaches the stop (checked first: the stop invalidates
    before a confirmation, as the live trigger does), ``TRIGGER`` when the low touches the
    entry, else None."""
    if reaches_stop(bar.low, stop):
        return STOP_FIRST
    if touches_entry(bar.low, entry_trigger):
        return TRIGGER
    return None


# --- The exit walk and R ------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitWalk:
    """The result of walking bars forward from a known price to its first stop/target/hold exit.

    ``reason`` is one of ``STOP``, ``TARGET``, ``HOLD_24H_EXIT`` or ``DATA_INCOMPLETE`` (bars ran
    out before the hold deadline with neither level hit -- the outcome is not yet knowable from
    what was fetched). ``price``/``at`` are ``None`` only for ``DATA_INCOMPLETE``.
    """

    reason: str
    price: D | None
    at: object
    ambiguous: bool
    bars_examined: int
    last_bar_at: object


def walk_to_exit(stop, target, bars, *, hold_deadline):
    """First stop/target hit in ``bars`` (ascending, at or after the fill), else a 24-hour exit.

    Shared by the shadow pick simulation, the unchanged-plan replay (``unchanged_plan.py``) and
    the strategy shadow simulation: each asks "starting now, with these levels, on these bars,
    what happens?" A bar reaching both levels is the stop (conservative) and flagged.
    """
    examined, last_at = 0, None
    for bar in bars:
        if bar.start >= hold_deadline:
            break
        examined += 1
        last_at = bar.start
        hit_stop, hit_target = bar.low <= stop, bar.high >= target
        if hit_stop and hit_target:
            return ExitWalk(STOP, stop, bar.start, True, examined, last_at)
        if hit_stop:
            return ExitWalk(STOP, stop, bar.start, False, examined, last_at)
        if hit_target:
            return ExitWalk(TARGET, target, bar.start, False, examined, last_at)
    at_or_after_deadline = [b for b in bars if b.start >= hold_deadline]
    if at_or_after_deadline:
        exit_bar = at_or_after_deadline[0]
        return ExitWalk(HOLD_EXIT, exit_bar.open, hold_deadline, False, examined, last_at)
    return ExitWalk(DATA_INCOMPLETE, None, None, False, examined, last_at)


def r_values(entry_price, stop, exit_price, *, fee_rate):
    """Gross and (assumed-fee) net R, priced at ``entry_price`` -- the official-R convention
    (P&L per unit / (entry - stop)), so no trade-size assumption is needed."""
    risk = entry_price - stop
    if risk <= 0:
        raise ShadowDataError("NONPOSITIVE_RISK_DENOMINATOR")
    gross_r = (exit_price - entry_price) / risk
    fee_r = fee_rate * (entry_price + exit_price) / risk
    return gross_r, gross_r - fee_r


# --- CRYPTO_TRADE_PLAN_V1's math ----------------------------------------------------------------


def range_floor(entry_trigger, hourly_range, multiple=STOP_RANGE_MULTIPLE):
    """The stop's hourly-range floor: entry trigger - ``multiple`` x hourly range (exact)."""
    with localcontext() as context:
        context.prec = PRECISION
        return entry_trigger - multiple * hourly_range


def target_cap(entry_trigger, stop, multiple=TARGET_R_MULTIPLE):
    """The target cap: entry trigger + ``multiple`` x (entry trigger - stop) (exact)."""
    with localcontext() as context:
        context.prec = PRECISION
        return entry_trigger + multiple * (entry_trigger - stop)


def mean_range(bars):
    """The mean high minus low of ``bars`` (exact, 80 digits); None without bars."""
    if not bars:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        return sum((b.high - b.low for b in bars), D(0)) / len(bars)


def hourly_range_at(hour_bars, at):
    """``(range, bars used)``: the mean range of the 1-hour bars that ended in the 24 hours
    before ``at`` (``crypto_maintenance.hourly_range``'s basis), None with fewer than 20."""
    used = [b for b in hour_bars if at - HOURLY_RANGE_HOURS * HOUR <= b.start
            and b.start + HOUR <= at]
    if len(used) < HOURLY_RANGE_MIN_BARS:
        return None, len(used)
    return mean_range(used), len(used)


def plan_levels(entry_trigger, research_stop, research_target, hourly_range):
    """CRYPTO_TRADE_PLAN_V1's stop and target, off the price grid (simulations).

    The stop is the lower of the proposed stop and the hourly-range floor; the target is the
    lower of the proposed target (None: no target of its own) and the 1.5R cap from the entry
    trigger. Returns a dict of exact Decimals with each level's basis, or raises
    ``ValueError`` (``TRADE_PLAN_STOP_NOT_POSITIVE`` or ``STOP_DISTANCE_BELOW_MINIMUM``)."""
    floor = range_floor(entry_trigger, hourly_range)
    stop = min(research_stop, floor)
    if stop <= 0:
        raise ValueError("TRADE_PLAN_STOP_NOT_POSITIVE")
    with localcontext() as context:
        context.prec = PRECISION
        if entry_trigger - stop < MIN_STOP_FRACTION * entry_trigger:
            raise ValueError("STOP_DISTANCE_BELOW_MINIMUM")
    cap = target_cap(entry_trigger, stop)
    target = cap if research_target is None else min(research_target, cap)
    return {"stop": stop, "stop_basis": "RESEARCH_STOP" if research_stop <= floor
            else "HOURLY_RANGE_FLOOR", "range_floor": floor, "target_cap": cap,
            "target": target, "target_basis": "PLAN_CAP" if target == cap else "RESEARCH_TARGET"}


# --- BREAKOUT_7D_VOL2X_V1's signal --------------------------------------------------------------

BREAKOUT_PRIOR_HOURS = 168  # The prior 7 days of the high and of the volume baseline.
BREAKOUT_DAY_HOURS = 24  # The 24-hour volume ending with the signal bar.
BREAKOUT_MIN_PRIOR_BARS = 120
BREAKOUT_MIN_DAY_BARS = 20
BREAKOUT_VOLUME_MULTIPLE = D(2)
BREAKOUT_FRESH_HOURS = 24  # No signal while the coin had one in the 23 hours before.
# Bars needed before the first evaluated bar: the freshness hours, the day and the baseline.
BREAKOUT_HISTORY_HOURS = BREAKOUT_FRESH_HOURS + BREAKOUT_DAY_HOURS + BREAKOUT_PRIOR_HOURS


@dataclass(frozen=True)
class BreakoutRule:
    """The breakout rule's numbers. ``BREAKOUT_7D_VOL2X_V1`` is ``BREAKOUT_V1_RULE``; the history
    tester (``HISTORY_TEST_V1``) evaluates declared variants of it through the same functions.

    ``min_prior_bars`` is the bar minimum of each ``prior_hours`` window (the high and the volume
    baseline); ``for_variant`` scales V1's 120 of 168 to other lookbacks (rounded up)."""

    prior_hours: int = BREAKOUT_PRIOR_HOURS
    volume_multiple: D = BREAKOUT_VOLUME_MULTIPLE
    min_prior_bars: int = BREAKOUT_MIN_PRIOR_BARS
    day_hours: int = BREAKOUT_DAY_HOURS
    min_day_bars: int = BREAKOUT_MIN_DAY_BARS
    fresh_hours: int = BREAKOUT_FRESH_HOURS

    def __post_init__(self):
        if (type(self.prior_hours) is not int or self.prior_hours < self.day_hours
                or not isinstance(self.volume_multiple, D) or self.volume_multiple <= 0
                or not 1 <= self.min_prior_bars <= self.prior_hours
                or not 1 <= self.min_day_bars <= self.day_hours or self.fresh_hours < 1):
            raise ValueError("BREAKOUT_RULE_INVALID")

    @classmethod
    def for_variant(cls, prior_days, volume_multiple):
        hours = int(prior_days) * 24
        minimum = -(-hours * BREAKOUT_MIN_PRIOR_BARS // BREAKOUT_PRIOR_HOURS)
        return cls(prior_hours=hours, volume_multiple=D(str(volume_multiple)),
                   min_prior_bars=minimum)

    @property
    def history_hours(self):
        """Bars needed before the first evaluated bar (freshness, day and baseline)."""
        return self.fresh_hours + self.day_hours + self.prior_hours

    def record(self):
        return {"prior_hours": self.prior_hours, "volume_multiple": str(self.volume_multiple),
                "min_prior_bars": self.min_prior_bars, "day_hours": self.day_hours,
                "min_day_bars": self.min_day_bars, "fresh_hours": self.fresh_hours}


BREAKOUT_V1_RULE = BreakoutRule()


class _Series:
    """Ascending hourly bars indexed by start, with prefix sums of volume."""

    def __init__(self, bars):
        self.bars = list(bars)
        self.starts = [b.start for b in self.bars]
        self.volume = [D(0)]
        for bar in self.bars:
            self.volume.append(self.volume[-1] + bar.volume)

    def span(self, start, end):
        """Index range of the bars starting in ``[start, end)``."""
        return bisect_left(self.starts, start), bisect_left(self.starts, end)


def breakout_facts(series, index, rule=BREAKOUT_V1_RULE):
    """The rule's facts for the bar at ``index`` (its close known at the bar's end)."""
    bar = series.bars[index]
    end = bar.start + HOUR
    p0, p1 = series.span(bar.start - rule.prior_hours * HOUR, bar.start)
    d0, d1 = series.span(end - rule.day_hours * HOUR, end)
    b0, b1 = series.span(end - (rule.day_hours + rule.prior_hours) * HOUR,
                         end - rule.day_hours * HOUR)
    facts = {"signal_bar_start": bar.start.isoformat(), "signal_at": end.isoformat(),
             "close": str(bar.close), "prior_bars": p1 - p0, "day_bars": d1 - d0,
             "baseline_bars": b1 - b0}
    baseline = series.volume[b1] - series.volume[b0]
    if (p1 - p0 < rule.min_prior_bars or d1 - d0 < rule.min_day_bars
            or b1 - b0 < rule.min_prior_bars or baseline <= 0):
        return {**facts, "status": "INSUFFICIENT_BARS", "signal": False}
    high = max(b.high for b in series.bars[p0:p1])
    day_volume = series.volume[d1] - series.volume[d0]
    with localcontext() as context:
        context.prec = PRECISION
        # The day's volume at least ``volume_multiple`` x the mean day volume of the prior
        # window: an exact product (day x prior hours >= multiple x baseline x day hours; for
        # V1, day x 7 >= 2 x baseline), so exactly the multiple passes.
        volume_ok = (day_volume * rule.prior_hours
                     >= rule.volume_multiple * baseline * rule.day_hours)
        ratio = (day_volume * rule.prior_hours / (baseline * rule.day_hours)).quantize(
            D("0.0001"))
    return {**facts, "status": "COMPUTED", "high_7d": str(high), "volume_24h": str(day_volume),
            "baseline_volume_7d": str(baseline), "volume_ratio": str(ratio),
            "close_above_high": bar.close > high, "volume_ok": volume_ok,
            "signal": bar.close > high and volume_ok}


def breakout_signals(bars, *, since, until, rule=BREAKOUT_V1_RULE):
    """Fresh breakout signals of one coin whose signal time (the bar's end) is in
    ``(since, until]``: the rule holds on the bar and held on none of the bars that started
    in the ``fresh_hours - 1`` hours before it. Stateless: a backfill and a nightly run find
    the same signals given the same bars. Each item is ``breakout_facts`` of its bar
    (``BREAKOUT_7D_VOL2X_V1`` with the default ``rule``)."""
    series = _Series(bars)
    computed = {}

    def holds(i):
        if i not in computed:
            computed[i] = breakout_facts(series, i, rule)
        return computed[i]["signal"]

    found = []
    for i, bar in enumerate(series.bars):
        end = bar.start + HOUR
        if not since < end <= until or not holds(i):
            continue
        f0, _ = series.span(bar.start - (rule.fresh_hours - 1) * HOUR, bar.start)
        if any(holds(j) for j in range(f0, i)):
            continue
        found.append(computed[i])
    return found


# --- The slippage model (HALF_SPREAD_PLUS_VOLATILITY_V1) -----------------------------------------

SLIPPAGE_MODEL = "HALF_SPREAD_PLUS_VOLATILITY_V1"
SLIPPAGE_HOURS = 24  # The hourly bars that ended in the 24 hours before the decision.
SLIPPAGE_MIN_BARS = 20
HALF_SPREAD_FLOOR = D("0.0001")  # 1 basis point: never assume a free crossing.
HALF_SPREAD_CAP = D("0.005")  # 50 basis points: the estimator is noisy on hourly bars.
VOLATILITY_COEFFICIENT = D("0.5")
SLIPPAGE_QUANTUM = D("0.0000000001")
SLIPPAGE_DESCRIPTION = (
    "Per leg, as a fraction of price: half the Abdi-Ranaldo (2017) close-high-low spread "
    "estimate on the 24 hourly bars before the decision (clamped to 1-50 basis points), plus "
    "0.5 x a one-minute volatility proxy (the mean hourly range / price / sqrt(60)). Charged on "
    "both legs like the fee: slippage R = per-leg fraction x (entry + exit) / (entry - stop). "
    "An assumption, not calibrated against fills; stop gaps beyond it are not modelled."
)


def _ln(value):
    with localcontext() as context:
        context.prec = 40
        return value.ln()


def spread_proxy(bars):
    """The Abdi-Ranaldo close-high-low estimate of the relative spread on ascending ``bars``:
    ``sqrt(max(0, mean(4 (c_t - eta_t)(c_t - eta_t+1))))`` with log prices and ``eta`` the
    bar's log mid-range. None with fewer than two bars."""
    if len(bars) < 2:
        return None
    with localcontext() as context:
        context.prec = 40
        eta = [(_ln(b.high) + _ln(b.low)) / 2 for b in bars]
        close = [_ln(b.close) for b in bars]
        terms = [4 * (close[t] - eta[t]) * (close[t] - eta[t + 1]) for t in range(len(bars) - 1)]
        mean = sum(terms, D(0)) / len(terms)
        return max(mean, D(0)).sqrt()


def slippage_estimate(hour_bars, at, reference_price):
    """``HALF_SPREAD_PLUS_VOLATILITY_V1``'s per-leg slippage fraction for a marketable order at
    ``at`` (pure; the hourly bars that ended in the 24 hours before ``at``). None with fewer
    than 20 bars or no positive reference price."""
    used = [b for b in hour_bars if at - SLIPPAGE_HOURS * HOUR <= b.start
            and b.start + HOUR <= at]
    if len(used) < SLIPPAGE_MIN_BARS or reference_price is None or reference_price <= 0:
        return None
    with localcontext() as context:
        context.prec = 40
        spread = spread_proxy(used)
        half = min(max(spread / 2, HALF_SPREAD_FLOOR), HALF_SPREAD_CAP)
        range_fraction = mean_range(used) / reference_price
        volatility = VOLATILITY_COEFFICIENT * range_fraction / D(60).sqrt()
        per_leg = (half + volatility).quantize(SLIPPAGE_QUANTUM)
    return {"model": SLIPPAGE_MODEL, "bars": len(used),
            "spread_proxy": str(spread.quantize(SLIPPAGE_QUANTUM)),
            "half_spread": str(half.quantize(SLIPPAGE_QUANTUM)),
            "volatility_term": str(volatility.quantize(SLIPPAGE_QUANTUM)),
            "per_leg": str(per_leg)}


def cost_r(entry_price, stop, exit_price, rate):
    """A both-legs cost charged at ``rate`` of each leg's price, in R (the fee's form)."""
    risk = entry_price - stop
    if risk <= 0:
        raise ShadowDataError("NONPOSITIVE_RISK_DENOMINATOR")
    with localcontext() as context:
        context.prec = PRECISION
        return rate * (entry_price + exit_price) / risk


# --- A marketable entry at a signal, then the plan's exits --------------------------------------

ENTRY_FILL_WITHIN = timedelta(minutes=15)
NOT_FILLED = "NOT_FILLED_NO_PRINT"
PLAN_REFUSED = "PLAN_REFUSED"


def simulate_marketable(signal_at, minute_bars, *, hourly_range, stop_fraction,
                        hold, fee_rate, slippage_fraction=None):
    """A marketable entry at ``signal_at`` and CRYPTO_TRADE_PLAN_V1's exits, on 1-minute bars.

    Fill: the open of the first bar starting at or after ``signal_at`` and within
    ``ENTRY_FILL_WITHIN`` (else ``NOT_FILLED_NO_PRINT``). Plan: the proposed stop is
    ``stop_fraction`` below the fill (the plan's 2% minimum), widened to the hourly-range
    floor; the target is the 1.5R cap; ``hold`` after the fill bar's start the position exits
    at the open of the first bar at or after it (``walk_to_exit``). R at the fill price.

    With ``slippage_fraction`` (``slippage_estimate``'s ``per_leg``), the result also carries
    ``slippage_r`` and ``net_r_after_slippage`` (net R minus the slippage on both legs); the
    gross and fee-net R are unchanged.
    """
    fill_bar = next((b for b in minute_bars if b.start >= signal_at), None)
    if fill_bar is None or fill_bar.start >= signal_at + ENTRY_FILL_WITHIN:
        return {"outcome": NOT_FILLED, "fill_price": None}
    entry = fill_bar.open
    with localcontext() as context:
        context.prec = PRECISION
        proposed = entry - stop_fraction * entry
    try:
        plan = plan_levels(entry, proposed, None, hourly_range)
    except ValueError as exc:
        return {"outcome": PLAN_REFUSED, "code": str(exc), "fill_price": entry,
                "fill_at": fill_bar.start}
    deadline = fill_bar.start + hold
    after = [b for b in minute_bars if b.start >= fill_bar.start]
    walk = walk_to_exit(plan["stop"], plan["target"], after, hold_deadline=deadline)
    result = {"outcome": walk.reason, "fill_price": entry, "fill_at": fill_bar.start,
              "plan": plan, "hold_deadline": deadline, "exit_price": walk.price,
              "exit_at": walk.at, "same_bar_ambiguous": walk.ambiguous,
              "bars_examined": walk.bars_examined, "last_bar_at": walk.last_bar_at,
              "gross_r": None, "net_r": None}
    if walk.reason != DATA_INCOMPLETE:
        result["gross_r"], result["net_r"] = r_values(entry, plan["stop"], walk.price,
                                                      fee_rate=fee_rate)
        if slippage_fraction is not None:
            slip = cost_r(entry, plan["stop"], walk.price, slippage_fraction)
            result["slippage_fraction"] = slippage_fraction
            result["slippage_r"] = slip
            result["net_r_after_slippage"] = result["net_r"] - slip
    return result


__all__ = [
    "BREAKOUT", "BREAKOUT_HISTORY_HOURS", "BREAKOUT_V1_RULE", "BreakoutRule", "SLIPPAGE_MODEL",
    "cost_r", "slippage_estimate", "spread_proxy", "DATA_INCOMPLETE",
    "ENTRY_FILL_WITHIN", "ENTRY_TYPES",
    "ExitWalk", "HOLD_EXIT", "IMMEDIATE", "NOT_FILLED", "PLAN_REFUSED", "PULLBACK", "STOP",
    "STOP_FIRST", "ShadowDataError", "TARGET", "TRIGGER", "breakout_facts", "breakout_signals",
    "hourly_range_at", "mean_range", "plan_levels", "pullback_bar_event", "r_values",
    "range_floor", "reaches_stop", "reaches_target", "simulate_marketable", "target_cap",
    "touches_entry", "walk_to_exit",
]
