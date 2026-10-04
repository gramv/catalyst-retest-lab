"""``BREAKOUT_7D_VOL2X_V1``: the first mechanical strategy, in shadow (``STRATEGY_REGISTRY_V1``).

Evidence: the 2026-09-29 signal study (Coinbase hourly data; exploratory, found after looking at
the results) found one family positive in both its bear and bull periods at Alpaca fees: a close
above the 7-day high on at least 2x volume, held 24 hours. The plan (docs/TRADING-QUALITY-PLAN.md
section 5, move 5) runs it in shadow beside the pullback strategy, with its own scorecard, before
it can earn paper capital.

The rule, on Alpaca's completed 1-hour bars (``core.breakout_signals``):

* **Signal** at the end of hourly bar B: B's close is above the highest high of the 168 hourly
  bars that started in the 7 days before B (equal is not above), and the volume of the 24 bars
  ending with B is at least 2x the mean 24-hour volume of the 7 days before those 24 hours
  (24-hour volume x 7 >= 2 x the 7-day volume; exactly 2x passes). At least 120 bars in each
  7-day window and 20 in the day are required, else no signal.
* **Fresh only**: no signal while the rule held on a bar that started in the 23 hours before B,
  so a coin that keeps breaking out gives one signal, and runs never depend on where a scan began.
* **Entry**: immediate, marketable at the signal (simulated at the open of the first 1-minute
  bar at or after the signal, within 15 minutes).
* **Plan**: ``CRYPTO_TRADE_PLAN_V1``'s math: the stop is the lower of 2% below the entry (the
  plan's minimum) and the entry minus 2x the hourly range (the 24 hours before the signal); the
  target is 1.5R; the window 24 hours.

Each proposal also carries ``HALF_SPREAD_PLUS_VOLATILITY_V1``'s slippage estimate at the signal
(``core.slippage_estimate``, package strategy-c2); the simulation reports R after it beside the
fee-net R, which is unchanged.

History variants (``HISTORY_TEST_V1``, declared before any run): lookback 5, 7 or 10 days x
volume 1.5, 2 or 3 times; ``LOOKBACK_7D_VOL_2X`` is this strategy's rule. A variant is a trial,
never a registered strategy.

Stage ``SHADOW``: no proposal is ever sent to Jev or admitted to the paper account; research
reports cannot name it. Promotion is a new named stage under the owner's yes.
"""

from datetime import timedelta
from decimal import Decimal

from catalyst_lab.strategies import core
from catalyst_lab.strategies.base import MECHANICAL, SHADOW, Strategy

STRATEGY_ID = "BREAKOUT_7D_VOL2X_V1"
TIMEFRAME = "1Hour"
HOLD = timedelta(hours=24)


def _variant(prior_days, volume_multiple):
    return {"variant_id": f"LOOKBACK_{prior_days}D_VOL_{volume_multiple}X",
            "prior_days": prior_days, "volume_multiple": volume_multiple}


HISTORY_VARIANTS = tuple(_variant(days, multiple) for days in (5, 7, 10)
                         for multiple in ("1.5", "2", "3"))
DEFAULT_VARIANT_ID = "LOOKBACK_7D_VOL_2X"


def rule_for(parameters=None):
    """The breakout rule of a history variant's ``parameters`` (None: ``BREAKOUT_V1_RULE``)."""
    if parameters is None:
        return core.BREAKOUT_V1_RULE
    return core.BreakoutRule.for_variant(parameters["prior_days"], parameters["volume_multiple"])


def signals(bars, context):
    """Fresh signals of one coin (``context``: ``symbol``, ``since``, ``until``, optionally a
    history variant's ``parameters``) on ascending completed 1-hour ``bars`` -- proposals as
    plain data, each with the facts it rests on, the hourly range its plan uses and the
    slippage estimate at the signal."""
    parameters = context.get("parameters")
    rule = rule_for(parameters)
    by_start = {b.start.isoformat(): b for b in bars}
    proposals = []
    for facts in core.breakout_signals(bars, since=context["since"], until=context["until"],
                                       rule=rule):
        signal_bar = by_start[facts["signal_bar_start"]]
        at = signal_bar.start + core.HOUR
        hourly_range, used = core.hourly_range_at(bars, at)
        proposal = {
            "strategy_id": STRATEGY_ID, "symbol": context["symbol"],
            "signal_at": at.isoformat(), "entry_type": core.BREAKOUT,
            "entry": "MARKETABLE_AT_SIGNAL", "reference_price": facts["close"],
            "hourly_range": str(hourly_range) if hourly_range is not None else None,
            "hourly_range_bars": used, "facts": facts,
            "slippage": core.slippage_estimate(bars, at, signal_bar.close),
        }
        if parameters is not None:
            proposal["parameters"] = dict(parameters)
        proposals.append(proposal)
    return proposals


def simulate(proposal, minute_bars, *, fee_rate):
    """The proposal's outcome on 1-minute bars (``core.simulate_marketable``)."""
    from datetime import datetime

    if proposal.get("hourly_range") is None:
        return {"outcome": "HOURLY_RANGE_UNAVAILABLE", "fill_price": None}
    slippage = proposal.get("slippage")  # Absent on signals recorded before strategy-c2.
    return core.simulate_marketable(
        datetime.fromisoformat(proposal["signal_at"]), minute_bars,
        hourly_range=Decimal(proposal["hourly_range"]), stop_fraction=core.MIN_STOP_FRACTION,
        hold=HOLD, fee_rate=fee_rate,
        slippage_fraction=Decimal(slippage["per_leg"]) if slippage else None)


BREAKOUT_7D_VOL2X_V1 = Strategy(
    strategy_id=STRATEGY_ID, name="BREAKOUT_7D_VOL2X", version=1,
    description="Mechanical: an hourly close above the prior 7-day high on a 24-hour volume at "
                "least 2x the 7-day mean; marketable entry at the signal; "
                "CRYPTO_TRADE_PLAN_V1's 2x hourly-range stop (2% minimum), 1.5R target, 24 h.",
    entry_types=frozenset({core.BREAKOUT}),
    trigger_rule="MARKETABLE_AT_SIGNAL (BREAKOUT_7D_VOL2X_V1 signal on a completed 1-hour bar)",
    plan_rules="CRYPTO_TRADE_PLAN_V1",
    jev_questions=(),
    sources=frozenset({MECHANICAL}),
    stage=SHADOW,
    signals=signals,
    simulate=simulate,
    parameters={"timeframe": TIMEFRAME, "prior_hours": core.BREAKOUT_PRIOR_HOURS,
                "day_hours": core.BREAKOUT_DAY_HOURS,
                "volume_multiple": str(core.BREAKOUT_VOLUME_MULTIPLE),
                "min_prior_bars": core.BREAKOUT_MIN_PRIOR_BARS,
                "min_day_bars": core.BREAKOUT_MIN_DAY_BARS,
                "fresh_hours": core.BREAKOUT_FRESH_HOURS,
                "entry_fill_within_minutes": int(core.ENTRY_FILL_WITHIN.total_seconds() // 60),
                "stop_range_multiple": str(core.STOP_RANGE_MULTIPLE),
                "min_stop_fraction": str(core.MIN_STOP_FRACTION),
                "target_r_multiple": str(core.TARGET_R_MULTIPLE), "hold_hours": 24,
                "slippage_model": core.SLIPPAGE_MODEL},
    history_variants=HISTORY_VARIANTS,
)

__all__ = ["BREAKOUT_7D_VOL2X_V1", "DEFAULT_VARIANT_ID", "HISTORY_VARIANTS", "HOLD",
           "STRATEGY_ID", "TIMEFRAME", "rule_for", "signals", "simulate"]
