"""``CRYPTO_TRADE_PLAN_V1``: the app shapes a report-V3 crypto pick's stop, target and window at
admission (owner approval 2026-10-02 of docs/TRADING-QUALITY-PLAN.md A3, package trade-plan).

Evidence (the MAE/MFE study of 2026-10-03, 40 closed paper trades and 22 untraded trigger
touches): original stops sat a median 1.75 hourly ranges below entry; entries that later reached
+1R first dipped up to 1.8-2.0 hourly ranges (90th percentile); targets (median 3.2R) were hit
by 6% within 24 hours. A stop no tighter than 2 hourly ranges with the same dollar risk, a
1.5R target and a 24-hour window was the best simulated rule (+0.47R a trade on the 27 complete
trades, day-bootstrap 90% CI +0.23..+0.65; positive in every leave-one-day-out cut).

The research agent's report contract does not change: the app derives the plan from the pick's
levels and the coin's own recent range.

* **Scope.** A crypto setup admitted from an ``AGENT_RESEARCH_REPORT_V3`` packet in both arms,
  on an engine whose ledger provides ``lab.managed_planned_stop(uuid)`` (the reservation guard
  must size planned risk on the plan's stop; see ``ManagedExecution`` and the package record).
  Every other setup, and every setup admitted before it, keeps its recorded levels.
* **Hourly range (HR).** The mean high minus low of the coin's completed 1-hour bars that ended
  in the 24 hours before admission, at least 20 of them (``crypto_maintenance.hourly_range``).
  Fewer, or a failed read, is ``HOURLY_RANGE_UNAVAILABLE``: a transient refusal that the next
  protection tick retries (as ``LIVE_PRICE_UNAVAILABLE``); a pick is never admitted on the
  research stop instead.
* **Stop.** The lower of the research stop and the entry trigger minus 2 x HR, rounded down to
  the coin's price grid (a lower stop is wider; never rounded up). The research stop already
  passed ``SYSTEM_CHECK_V1``'s 2% minimum; the plan's stop is checked again against it
  (``STOP_DISTANCE_BELOW_MINIMUM``, (max entry - stop) / max entry below 2%), so the rule
  holds for the stop that is actually used. A stop at or below zero is refused
  (``TRADE_PLAN_STOP_NOT_POSITIVE``).
* **Sizing.** Unchanged rule, on the plan's stop: the per-trade risk cap (0.5% of equity under
  ``JEV_MANAGED_RISK_V3``) divided by max entry minus the plan's stop, so a wider stop buys a
  smaller quantity for the same dollars at risk; the reservation's planned risk is quantity x
  (max entry - plan stop) and official R uses the same stop.
* **Target.** The lower of the research target and the entry trigger plus 1.5 x (entry trigger
  - plan stop), rounded down to the grid. A target at or below the max entry is refused
  (``TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY``). The cap (1.5R from the entry trigger, rounded
  down) is recorded as ``target_cap``; ``CRYPTO_MAINTENANCE_V4`` never raises a target above
  it.
* **Window.** 24 hours, pinned by the version: admission records ``CRYPTO_WINDOW_REVIEW_V1``
  (maintained arm) or ``CRYPTO_WINDOW_HOLD_V1`` (control arm) with a 1,440-minute window,
  whatever ``MANAGED_CRYPTO_WINDOW_JSON`` says; the setting still governs setups outside this
  version.
* **Record.** The WATCHING state's ``stop`` and ``target`` are the plan's (protection, the
  stop-breach rules, maintenance and the reviews read them there); ``trade_plan`` records the
  policy, the research levels, the hourly range and its bars, and each derived level with its
  basis. The admitted packet (``record_json``) keeps the research levels the reviews bound, and
  the trigger, its invalidation and the admission reward-to-risk rule read them as before.

Pure: decisions on exact Decimals under an 80-digit context; ledger and broker access live in
``ManagedExecution``; the runtime's bounded bar reader is ``HourlyRangeReader``.
"""

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal, localcontext

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import system_check
from catalyst_lab.broker_budget import RESEARCH, request_priority
from catalyst_lab.crypto_holding import WINDOW_REVIEW_POLICY_ID, CryptoWindowSetting
from catalyst_lab.strategies import core

D = Decimal

TRADE_PLAN_VERSION = "CRYPTO_TRADE_PLAN_V1"
STOP_RANGE_MULTIPLE = D(2)
TARGET_R_MULTIPLE = D("1.5")
MIN_STOP_FRACTION = D("0.02")  # system_check.MINIMUM_CRYPTO_STOP_FRACTION, on the plan's stop.
WINDOW_MINUTES = 1440
ENTRY_BASIS = "ENTRY_TRIGGER"
ROUNDING = "STOP_AND_TARGET_FLOOR_TO_PRICE_INCREMENT"
FIELD = "trade_plan"
# The ledger function the reservation guard must read the planned stop through (the migration
# that adds it is the activation step; package record docs/packages/trade-plan.md).
PLANNED_STOP_FUNCTION = "lab.managed_planned_stop(uuid)"

HOURLY_RANGE_UNAVAILABLE = "HOURLY_RANGE_UNAVAILABLE"  # Transient: the next tick retries.
STOP_DISTANCE_BELOW_MINIMUM = system_check.STOP_DISTANCE_BELOW_MINIMUM
TARGET_NOT_ABOVE_MAX_ENTRY = system_check.TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY
STOP_NOT_POSITIVE = system_check.TRADE_PLAN_STOP_NOT_POSITIVE
# Permanent refusals of a pick by this version (final for the receipt, like the system check's).
PLAN_REFUSALS = frozenset({TARGET_NOT_ABOVE_MAX_ENTRY, STOP_NOT_POSITIVE})
RESEARCH_STOP, RANGE_FLOOR = "RESEARCH_STOP", "HOURLY_RANGE_FLOOR"
RESEARCH_TARGET, PLAN_CAP = "RESEARCH_TARGET", "PLAN_CAP"
# How long the runtime's reader waits after a failed bar read of a coin before trying again.
RETRY_SECONDS = 60
_PRECISION = 80
_QUANTUM = D("1e-12")  # Recorded fractions only.


@dataclass(frozen=True)
class TradePlanPolicy:
    """The exact ``CRYPTO_TRADE_PLAN_V1`` record; a state can never carry altered numbers."""

    policy_id: str
    hourly_range_basis: str
    hourly_range_bars: int
    hourly_range_min_bars: int
    stop_range_multiple: str
    target_r_multiple: str
    entry_basis: str
    min_stop_fraction: str
    window_minutes: int
    rounding: str

    def __post_init__(self):
        if asdict(self) != _VALUES:
            raise ValueError("EXPLICIT_TRADE_PLAN_POLICY_REQUIRED")

    def record(self):
        return asdict(self)

    def window_setting(self):
        """The window this version pins (24 hours), recorded per setup like the setting's."""
        return CryptoWindowSetting(WINDOW_REVIEW_POLICY_ID, self.window_minutes)


_VALUES = {
    "policy_id": TRADE_PLAN_VERSION,
    "hourly_range_basis": cm.HOURLY_RANGE_BASIS,
    "hourly_range_bars": cm.HOURLY_RANGE_BARS,
    "hourly_range_min_bars": cm.HOURLY_RANGE_MIN_BARS,
    "stop_range_multiple": str(STOP_RANGE_MULTIPLE),
    "target_r_multiple": str(TARGET_R_MULTIPLE),
    "entry_basis": ENTRY_BASIS,
    "min_stop_fraction": str(MIN_STOP_FRACTION),
    "window_minutes": WINDOW_MINUTES,
    "rounding": ROUNDING,
}
CRYPTO_TRADE_PLAN = TradePlanPolicy(**_VALUES)
# The plan an engine following its ledger (``trade_plan_enabled=None``) admits under once the
# ledger is at schema 27 (migration 027, ``lab.managed_planned_stop``). None admits every setup
# on its research levels, as before this version (the earlier-version tests pin it).
ADMITTED = CRYPTO_TRADE_PLAN


class HourlyRangeUnavailable(Exception):
    """No hourly range for this attempt; ``evidence`` says what was read."""

    def __init__(self, code, evidence=None):
        super().__init__(HOURLY_RANGE_UNAVAILABLE)
        self.code = code
        self.evidence = dict(evidence or {})


class PlanRefused(ValueError):
    """A permanent refusal of the pick by this version; ``str(exc)`` is the code."""

    def __init__(self, code, evidence):
        super().__init__(code)
        self.code = code
        self.evidence = dict(evidence)


def applies(packet):
    """The version's scope: a report-V3 crypto packet (the engine also needs the ledger
    function; ``ManagedExecution`` decides that)."""
    return (isinstance(packet, dict) and packet.get("market") == "CRYPTO"
            and packet.get("report_schema_version") == "AGENT_RESEARCH_REPORT_V3")


def _ratio(numerator, denominator):
    with localcontext() as context:
        context.prec = _PRECISION
        return (numerator / denominator).quantize(_QUANTUM)


def plan(levels, *, hourly_range, range_evidence, increment, policy=CRYPTO_TRADE_PLAN):
    """The ``trade_plan`` record for research ``levels`` (entry trigger, max entry, stop,
    target), the coin's hourly range and price ``increment``; raises ``PlanRefused``.

    ``stop`` and ``target`` are the plan's levels, on the grid; ``research_levels`` keeps the
    pick's own, for analytics."""
    if policy != CRYPTO_TRADE_PLAN:
        raise ValueError("EXPLICIT_TRADE_PLAN_POLICY_REQUIRED")
    t, m, s, p = (cm.number(levels[k])
                  for k in ("entry_trigger", "max_entry_price", "stop", "target"))
    increment = cm.number(increment)
    hr = cm.number(hourly_range)
    if increment <= 0 or hr < 0 or not 0 < s < t <= m < p:
        raise ValueError("INVALID_TRADE_PLAN_INPUT")
    # The decision core's plan math (strategies.core, shared with the strategy simulations).
    floor = core.range_floor(t, hr, STOP_RANGE_MULTIPLE)
    stop_basis = RESEARCH_STOP if s <= floor else RANGE_FLOOR
    raw_stop = min(s, floor)
    evidence = {
        "policy": policy.record(),
        "research_levels": {k: str(levels[k]) for k in (
            "entry_trigger", "max_entry_price", "stop", "target")},
        "hourly_range": str(hr),
        "hourly_range_fraction": str(_ratio(hr, t)),
        "hourly_range_evidence": dict(range_evidence or {}),
        "increment": str(increment),
        "range_floor": str(floor),
        "stop_basis": stop_basis,
    }
    if raw_stop <= 0:
        raise PlanRefused(STOP_NOT_POSITIVE, evidence)
    stop = cm.floor_grid(raw_stop, increment)
    if stop <= 0:
        raise PlanRefused(STOP_NOT_POSITIVE, evidence)
    with localcontext() as context:
        context.prec = _PRECISION
        evidence.update(stop=str(stop), stop_distance_fraction=str(_ratio(m - stop, m)))
        if m - stop < MIN_STOP_FRACTION * m:
            raise PlanRefused(STOP_DISTANCE_BELOW_MINIMUM, evidence)
        cap = core.target_cap(t, stop, TARGET_R_MULTIPLE)
        target_basis = RESEARCH_TARGET if p <= cap else PLAN_CAP
        target = cm.floor_grid(min(p, cap), increment)
        cap_on_grid = cm.floor_grid(cap, increment)
        evidence.update(
            target_cap=str(cap_on_grid), target_basis=target_basis, target=str(target),
            risk_per_coin=str(m - stop), entry_risk=str(t - stop),
            target_r_from_entry=str(_ratio(target - t, t - stop)),
            reward_risk_at_max_entry=str(_ratio(target - m, m - stop)),
        )
        if target <= m:
            raise PlanRefused(TARGET_NOT_ABOVE_MAX_ENTRY, evidence)
    return evidence


def recorded(state):
    """The setup's ``trade_plan`` record when it was admitted under this exact version, else
    None (every other setup). An altered policy record is refused."""
    record = state.get(FIELD) if isinstance(state, dict) else None
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("policy") != CRYPTO_TRADE_PLAN.record():
        raise ValueError("EXPLICIT_TRADE_PLAN_POLICY_REQUIRED")
    return record


def active(state):
    return recorded(state) is not None


def initial_levels(levels, state):
    """``levels`` (a setup's research levels, as Decimals or as text) with the plan's stop and
    target in place for a setup of this version; the levels themselves for every other setup.
    The admission-time levels sizing, official R and maintenance read."""
    record = recorded(state)
    if record is None:
        return levels
    convert = (lambda v: D(str(v))) if isinstance(levels.get("stop"), D) else str
    return {**levels, "stop": convert(record["stop"]), "target": convert(record["target"])}


def target_cap(state):
    """The plan's target cap (Decimal) for a setup of this version, else None."""
    record = recorded(state)
    return D(record["target_cap"]) if record is not None else None


class HourlyRangeReader:
    """The runtime's bounded read of a coin's hourly range for admission (GET-only, research
    request class).

    A success is reused until the next hourly bar completes; a failure waits ``RETRY_SECONDS``
    before the coin is read again; each read spends one of the protection tick's market-data
    REST reads (``spend``, the system check's ``LivePriceReader.spend_read``), so admission
    never makes more market-data reads in one tick than before. Raises
    ``HourlyRangeUnavailable`` (code, evidence); never returns a partial range.
    """

    def __init__(self, source, *, clock, spend=None, retry_seconds=RETRY_SECONDS):
        self.source, self.clock, self.spend = source, clock, spend
        self.retry_seconds = retry_seconds
        self._ok = {}
        self._failed = {}
        self.reads = 0

    def __call__(self, symbol):
        now = self.clock()
        boundary = cm.floor_time(now, 3600)
        cached = self._ok.get(symbol)
        if cached is not None and cached[0] == boundary:
            return cached[1], cached[2]
        failed = self._failed.get(symbol)
        if failed is not None and now - failed[0] < timedelta(seconds=self.retry_seconds):
            raise HourlyRangeUnavailable("HOURLY_RANGE_RETRY_WAIT", {
                "last_code": failed[1], "attempted_at": failed[0].isoformat()})
        if self.spend is not None and not self.spend():
            raise HourlyRangeUnavailable("REST_READ_LIMIT_THIS_TICK")
        self.reads += 1
        try:
            with request_priority(RESEARCH):
                bars, issues = self.source.timeframe_bars(
                    "CRYPTO", symbol, timeframe="1Hour", count=cm.HOURLY_RANGE_BARS + 6)
        except Exception as exc:  # A transport fault is a missing range, never a pass.
            code = type(exc).__name__
            self._failed[symbol] = (now, code)
            raise HourlyRangeUnavailable(code) from None
        if issues:
            code = next((getattr(i, "code", None) for i in issues if getattr(i, "code", None)),
                        "HOURLY_BARS_INCOMPLETE")
            self._failed[symbol] = (now, code)
            raise HourlyRangeUnavailable(code)
        value, evidence = cm.hourly_range(bars, now)
        if value is None:
            self._failed[symbol] = (now, "HOURLY_BARS_TOO_FEW")
            raise HourlyRangeUnavailable("HOURLY_BARS_TOO_FEW", evidence)
        self._failed.pop(symbol, None)
        self._ok[symbol] = (boundary, value, evidence)
        return value, evidence
