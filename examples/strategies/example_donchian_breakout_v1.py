"""EXAMPLE_DONCHIAN_BREAKOUT_V1: a teaching example for STRATEGY_SDK_V1 (package oss-packaging).

PAPER TRADING ONLY. This is a worked example of the plug-in interface, not a strategy anyone
should trade: it has not passed a history test on real bars. (The project's own breakout,
BREAKOUT_7D_VOL2X_V1, failed its history test: -0.23 R per trade after costs.)

The rule, on completed 1-hour bars of one coin:

    signal at the end of bar B when B's close is above the highest high of the CHANNEL bars
    before B, and the bar before B closed at or below that same channel (a fresh breakout).

Entry: MARKETABLE_AT_SIGNAL (buy right after the bar closes). Exits: CRYPTO_TRADE_PLAN_V1 (a stop
2% below the fill widened to two hourly ranges, a 1.5R target, 24 hours), simulated by the SDK's
shared code: the same functions the shadow and the paper path use.

Try it:

    catalyst-lab history-test --source synthetic --plugins-dir examples/strategies \\
        --strategy EXAMPLE_DONCHIAN_BREAKOUT_V1 --start 2026-05-01 --end 2026-09-01 \\
        --is-days 30 --oos-days 15
"""

from catalyst_lab.strategies import sdk

# The registered rule's channel (hours). Every number a rule uses belongs in `parameters` and,
# when you want to compare settings, in HISTORY_VARIANTS -- declared BEFORE the first history
# test. Each variant is a trial that the deflated Sharpe ratio charges you for.
CHANNEL = 48
HISTORY_VARIANTS = (
    {"variant_id": "CHANNEL_48", "channel": 48},
    {"variant_id": "CHANNEL_24", "channel": 24},
    {"variant_id": "CHANNEL_96", "channel": 96},
)
DEFAULT_VARIANT_ID = "CHANNEL_48"  # The registered rule; the history tester reads this name.


def _channel_high(bars, end_index, length):
    """The highest high of the ``length`` bars before ``bars[end_index]`` (not including it)."""
    return max(b.high for b in bars[end_index - length:end_index])


def signals(bars, context):
    """Fresh channel breakouts of one coin whose signal time is in ``(since, until]``.

    Pure: no I/O, no clock, no randomness. ``bars`` are ascending completed 1-hour bars with
    Decimal prices; ``context`` carries ``symbol``, ``since``, ``until`` and, in the history
    tester, ``parameters`` (one of the HISTORY_VARIANTS above)."""
    parameters = context.get("parameters") or {}
    channel = int(parameters.get("channel", CHANNEL))
    proposals = []
    # sdk.signal_bars walks exactly the bars whose end falls in the window you must answer for.
    for index, bar, _signal_at in sdk.signal_bars(bars, context):
        if index < channel + 1:  # Not enough history for this bar's channel and the one before.
            continue
        level = _channel_high(bars, index, channel)
        previous_level = _channel_high(bars, index - 1, channel)
        fresh = bars[index - 1].close <= previous_level
        if bar.close > level and fresh:
            # marketable_proposal builds the one proposal shape the paper path supports.
            # Put the numbers your rule looked at in `facts`: they are recorded with the signal.
            proposals.append(sdk.marketable_proposal(
                STRATEGY.strategy_id, context["symbol"], bars, bar,
                facts={"channel_high": str(level), "close": str(bar.close),
                       "channel_hours": channel},
                parameters=context.get("parameters")))
    return proposals


STRATEGY = sdk.mechanical_strategy(
    name="EXAMPLE_DONCHIAN_BREAKOUT", version=1,
    description="Teaching example: an hourly close above the prior 48-hour high (a fresh "
                "breakout); marketable entry; CRYPTO_TRADE_PLAN_V1's stop, 1.5R target, 24 hours.",
    signals=signals,
    trigger_rule="MARKETABLE_AT_SIGNAL (EXAMPLE_DONCHIAN_BREAKOUT_V1 fresh channel breakout on a "
                 "completed 1-hour bar)",
    parameters={"channel_hours": CHANNEL, "timeframe": sdk.TIMEFRAME},
    history_variants=HISTORY_VARIANTS,
    # A breakout's price may already be at the collar when the signal arrives.
    entry_types=frozenset({sdk.BREAKOUT, sdk.IMMEDIATE}),
    # The longest variant's channel, the bar before it, and the hourly range the plan reads.
    history_hours=96 + 2,
)
