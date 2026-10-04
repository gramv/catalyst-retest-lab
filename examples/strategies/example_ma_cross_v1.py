"""EXAMPLE_MA_CROSS_V1: an example strategy plug-in for STRATEGY_SDK_V1 (package plugin-c3).

PAPER TRADING ONLY. An illustration of the plug-in interface, not a trading recommendation:
nothing here has passed the history test. It runs in history tests and in shadow; it can reach the
paper account only through the owner's promotion and the runtime's MANAGED_STRATEGIES_JSON.

The rule, on completed 1-hour bars: a signal at the end of bar B when the mean close of the
FAST bars ending with B rises above the mean close of the SLOW bars ending with B, having been at
or below it on the bar before (a fresh cross). Entry: marketable at the signal; exits:
CRYPTO_TRADE_PLAN_V1 (stop 2% below the fill widened to two hourly ranges, 1.5R target, 24
hours) through the SDK's shared simulation.

    catalyst-lab history-test --plugins-dir examples/strategies --strategy EXAMPLE_MA_CROSS_V1
    CATALYST_STRATEGY_PLUGINS_DIR=examples/strategies   # the nightly shadow picks it up
"""

from decimal import Decimal, localcontext

from catalyst_lab.strategies import sdk

FAST, SLOW = 12, 48
HISTORY_VARIANTS = (
    {"variant_id": "FAST_12_SLOW_48", "fast": 12, "slow": 48},
    {"variant_id": "FAST_6_SLOW_24", "fast": 6, "slow": 24},
)
DEFAULT_VARIANT_ID = "FAST_12_SLOW_48"  # The registered rule (the history tester reads it).


def _mean_close(bars):
    with localcontext() as context:
        context.prec = 40
        return sum((b.close for b in bars), Decimal(0)) / len(bars)


def signals(bars, context):
    """Fresh crosses of one coin in ``(since, until]``; pure (no I/O, no clock)."""
    parameters = context.get("parameters") or {}
    fast, slow = int(parameters.get("fast", FAST)), int(parameters.get("slow", SLOW))
    proposals = []
    for index, bar, _at in sdk.signal_bars(bars, context):
        if index < slow:  # The slow mean of this bar and of the bar before.
            continue
        now_fast = _mean_close(bars[index - fast + 1:index + 1])
        now_slow = _mean_close(bars[index - slow + 1:index + 1])
        before_fast = _mean_close(bars[index - fast:index])
        before_slow = _mean_close(bars[index - slow:index])
        if now_fast > now_slow and before_fast <= before_slow:
            proposals.append(sdk.marketable_proposal(
                STRATEGY.strategy_id, context["symbol"], bars, bar,
                facts={"fast_mean": str(now_fast), "slow_mean": str(now_slow),
                       "fast": fast, "slow": slow},
                parameters=context.get("parameters")))
    return proposals


STRATEGY = sdk.mechanical_strategy(
    name="EXAMPLE_MA_CROSS", version=1,
    description="Example: the 12-hour mean close crosses above the 48-hour mean; marketable "
                "entry at the signal; CRYPTO_TRADE_PLAN_V1's stop, 1.5R target and 24 hours.",
    signals=signals,
    trigger_rule="MARKETABLE_AT_SIGNAL (EXAMPLE_MA_CROSS_V1 fresh cross on a completed 1-hour bar)",
    parameters={"fast_hours": FAST, "slow_hours": SLOW, "timeframe": sdk.TIMEFRAME},
    history_variants=HISTORY_VARIANTS,
    entry_types=frozenset({sdk.BREAKOUT, sdk.IMMEDIATE}),
    history_hours=SLOW + 2,
)
