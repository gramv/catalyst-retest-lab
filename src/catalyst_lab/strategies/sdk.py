"""``STRATEGY_SDK_V1``: the stable surface a strategy plug-in imports (package plugin-c3,
2026-10-03; guide docs/STRATEGY-PLUGINS.md, "Write your first strategy").

**PAPER TRADING ONLY.** A plug-in written against this module is tested on history
(``catalyst-lab history-test``) and in shadow (the nightly ``STRATEGY_SHADOW_V1``); it reaches the
paper account only through the owner's promotion (``managed_ops promote-strategy``) and the
runtime's ``MANAGED_STRATEGIES_JSON``, and then only through the shared admission, system check,
trade plan, pacing and the exact one-use risk authorization of every order. Nothing here places
an order, reads a ledger, a broker or a clock.

What is stable under ``STRATEGY_SDK_V1`` (a later change is ``STRATEGY_SDK_V2``, never an edit):

* ``Strategy`` and the constants it takes (stages ``HISTORY_TEST``/``SHADOW``, source
  ``MECHANICAL``, entry types ``PULLBACK``/``IMMEDIATE``/``BREAKOUT``, plan rules
  ``CRYPTO_TRADE_PLAN_V1``), and ``mechanical_strategy`` which fills in the defaults;
* the shape of what ``signals(bars, context)`` returns: ``marketable_proposal`` builds the one
  proposal shape the paper path supports (``MARKETABLE_AT_SIGNAL``) with the hourly range its
  plan uses and the slippage estimate at the signal;
* ``simulate_marketable_proposal(proposal, minute_bars, *, fee_rate)``: the shared marketable fill
  and ``CRYPTO_TRADE_PLAN_V1`` exits, the function the breakout uses;
* ``Bar`` (what ``bars`` and ``minute_bars`` hold: ``start``, ``open``, ``high``, ``low``,
  ``close``, ``volume``; ascending, completed, UTC) and the decision-core helpers re-exported
  below (``hourly_range_at``, ``mean_range``, ``slippage_estimate``, ``plan_levels``,
  ``walk_to_exit``, ``r_values``, ``touches_entry``, ``reaches_stop``, ``reaches_target``).

A plug-in module offers its record as ``STRATEGY`` (or several as ``STRATEGIES``), and may name
its registered history variant ``DEFAULT_VARIANT_ID``. ``catalyst-lab new-strategy NAME`` writes a
working skeleton.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from catalyst_lab.strategies import core
from catalyst_lab.strategies.base import (
    HISTORY_TEST,
    LIVE_PAPER,
    MECHANICAL,
    SHADOW,
    Strategy,
)
from catalyst_lab.strategies.core import (
    BREAKOUT,
    DATA_INCOMPLETE,
    ENTRY_FILL_WITHIN,
    HOLD_EXIT,
    HOUR,
    IMMEDIATE,
    MIN_STOP_FRACTION,
    NOT_FILLED,
    PULLBACK,
    STOP,
    TARGET,
    ShadowDataError,
    hourly_range_at,
    mean_range,
    plan_levels,
    r_values,
    reaches_stop,
    reaches_target,
    slippage_estimate,
    touches_entry,
    walk_to_exit,
)

SDK_VERSION = "STRATEGY_SDK_V1"
MARKETABLE_AT_SIGNAL = "MARKETABLE_AT_SIGNAL"
CRYPTO_TRADE_PLAN_V1 = "CRYPTO_TRADE_PLAN_V1"
HOLD = timedelta(hours=24)
TIMEFRAME = "1Hour"


@dataclass(frozen=True)
class Bar:
    """One completed bar (the shape the shadow, the history tester and the paper source pass)."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        if self.start.tzinfo is None:
            raise ShadowDataError("AWARE_BAR_TIMESTAMP_REQUIRED")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ShadowDataError("INCONSISTENT_BAR")


def signal_bars(bars, context):
    """``(index, bar, signal_at)`` of each completed hourly bar whose end (the signal time) is
    in ``(context["since"], context["until"]]`` -- the window ``signals`` must answer for."""
    since, until = context["since"], context["until"]
    for index, bar in enumerate(bars):
        at = bar.start + HOUR
        if since < at <= until:
            yield index, bar, at


def marketable_proposal(strategy_id, symbol, bars, signal_bar, *, facts=None, parameters=None):
    """The ``MARKETABLE_AT_SIGNAL`` proposal of a signal on ``signal_bar`` (a completed 1-hour
    bar of ``bars``): buy at the next price after the bar closes; ``CRYPTO_TRADE_PLAN_V1``'s stop
    (2% below the fill, widened to 2 hourly ranges), 1.5R target and 24-hour window."""
    at = signal_bar.start + HOUR
    hourly_range, used = hourly_range_at(bars, at)
    proposal = {
        "strategy_id": strategy_id, "symbol": symbol, "signal_at": at.isoformat(),
        "entry_type": BREAKOUT, "entry": MARKETABLE_AT_SIGNAL,
        "reference_price": str(signal_bar.close),
        "hourly_range": str(hourly_range) if hourly_range is not None else None,
        "hourly_range_bars": used, "facts": dict(facts or {}),
        "slippage": slippage_estimate(bars, at, signal_bar.close),
    }
    if parameters is not None:
        proposal["parameters"] = dict(parameters)
    return proposal


def simulate_marketable_proposal(proposal, minute_bars, *, fee_rate, hold=HOLD):
    """A ``marketable_proposal``'s outcome on 1-minute bars (``core.simulate_marketable``):
    the fill at the first minute bar's open within 15 minutes of the signal, the plan's stop and
    target, a 24-hour exit, R net of ``fee_rate`` on both legs (and of the slippage estimate)."""
    if proposal.get("hourly_range") is None:
        return {"outcome": "HOURLY_RANGE_UNAVAILABLE", "fill_price": None}
    slippage = proposal.get("slippage")
    return core.simulate_marketable(
        datetime.fromisoformat(proposal["signal_at"]), minute_bars,
        hourly_range=Decimal(proposal["hourly_range"]), stop_fraction=MIN_STOP_FRACTION,
        hold=hold, fee_rate=fee_rate,
        slippage_fraction=Decimal(slippage["per_leg"]) if slippage else None)


def mechanical_strategy(*, name, version, description, signals, trigger_rule,
                        simulate=simulate_marketable_proposal, parameters=None,
                        history_variants=(), stage=SHADOW, entry_types=frozenset({BREAKOUT}),
                        history_hours=core.BREAKOUT_HISTORY_HOURS, jev_questions=()):
    """A mechanical ``Strategy`` record (source ``MECHANICAL``, plan ``CRYPTO_TRADE_PLAN_V1``,
    id ``NAME_V<version>``). ``stage`` is ``HISTORY_TEST`` or ``SHADOW``: paper is the owner's
    promotion record, never a declaration."""
    return Strategy(
        strategy_id=f"{name}_V{version}", name=name, version=version, description=description,
        entry_types=frozenset(entry_types), trigger_rule=trigger_rule,
        plan_rules=CRYPTO_TRADE_PLAN_V1, jev_questions=tuple(jev_questions),
        sources=frozenset({MECHANICAL}), stage=stage, signals=signals, simulate=simulate,
        parameters=dict(parameters or {}), history_variants=tuple(history_variants),
        history_hours=history_hours,
    )


__all__ = [
    "BREAKOUT", "Bar", "CRYPTO_TRADE_PLAN_V1", "DATA_INCOMPLETE", "ENTRY_FILL_WITHIN",
    "HISTORY_TEST", "HOLD", "HOLD_EXIT", "HOUR", "IMMEDIATE", "LIVE_PAPER", "MARKETABLE_AT_SIGNAL",
    "MECHANICAL", "MIN_STOP_FRACTION", "NOT_FILLED", "PULLBACK", "SDK_VERSION", "SHADOW", "STOP",
    "ShadowDataError", "Strategy", "TARGET", "TIMEFRAME", "hourly_range_at",
    "marketable_proposal", "mean_range", "mechanical_strategy", "plan_levels", "r_values",
    "reaches_stop", "reaches_target", "signal_bars", "simulate_marketable_proposal",
    "slippage_estimate", "touches_entry", "walk_to_exit",
]
