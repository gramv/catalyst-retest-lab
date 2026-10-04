"""EXAMPLE_RSI_REVERSION_V1: a teaching example for STRATEGY_SDK_V1 (package oss-packaging).

PAPER TRADING ONLY. A worked example of the plug-in interface, not a recommendation: it has not
passed a history test on real bars. Buying weakness is a classic idea that often loses after
fees in crypto; the point here is to show how to compute an indicator inside `signals` with
Decimal arithmetic and to declare variants honestly.

The rule, on completed 1-hour bars of one coin:

    RSI(PERIOD) is Wilder's relative strength index of the closes. A signal at the end of bar B
    when RSI crosses back above OVERSOLD (at or below it on the bar before, above it on B) while
    the mean close of the TREND bars ending with B is higher than the same mean 24 bars earlier
    (buy a dip while the slow average still rises).

Entry: MARKETABLE_AT_SIGNAL. Exits: CRYPTO_TRADE_PLAN_V1 through the SDK's shared simulation.

    catalyst-lab history-test --source synthetic --plugins-dir examples/strategies \\
        --strategy EXAMPLE_RSI_REVERSION_V1 --start 2026-05-01 --end 2026-09-01 \\
        --is-days 30 --oos-days 15
"""

from decimal import Decimal, localcontext

from catalyst_lab.strategies import sdk

PERIOD, OVERSOLD, TREND = 14, Decimal(30), 72
SLOPE_BARS = 24  # The trend is "up" when its mean is higher than 24 bars earlier.
HISTORY_VARIANTS = (
    {"variant_id": "RSI14_30_TREND72", "period": 14, "oversold": "30", "trend": 72},
    {"variant_id": "RSI14_25_TREND72", "period": 14, "oversold": "25", "trend": 72},
)
DEFAULT_VARIANT_ID = "RSI14_30_TREND72"


def rsi_series(closes, period):
    """Wilder's RSI for every close from index ``period`` on (None before), in Decimal.

    RSI = 100 - 100 / (1 + average gain / average loss), the averages smoothed as
    avg = (avg * (period - 1) + this change) / period after a simple first average."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    with localcontext() as context:
        context.prec = 40
        gains = losses = Decimal(0)
        for i in range(1, period + 1):
            change = closes[i] - closes[i - 1]
            gains += max(change, Decimal(0))
            losses += max(-change, Decimal(0))
        avg_gain, avg_loss = gains / period, losses / period
        for i in range(period, len(closes)):
            if i > period:
                change = closes[i] - closes[i - 1]
                avg_gain = (avg_gain * (period - 1) + max(change, Decimal(0))) / period
                avg_loss = (avg_loss * (period - 1) + max(-change, Decimal(0))) / period
            out[i] = (Decimal(100) if avg_loss == 0
                      else Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss))
    return out


def signals(bars, context):
    """RSI crosses back above the oversold line in an uptrend; pure (no I/O, no clock)."""
    parameters = context.get("parameters") or {}
    period = int(parameters.get("period", PERIOD))
    oversold = Decimal(str(parameters.get("oversold", OVERSOLD)))
    trend = int(parameters.get("trend", TREND))
    closes = [b.close for b in bars]
    rsi = rsi_series(closes, period)
    proposals = []
    for index, bar, _at in sdk.signal_bars(bars, context):
        if index < max(trend + SLOPE_BARS, period + 1) or rsi[index - 1] is None:
            continue
        with localcontext() as ctx:
            ctx.prec = 40
            trend_mean = sum(closes[index - trend + 1:index + 1], Decimal(0)) / trend
            earlier = index - SLOPE_BARS
            earlier_mean = sum(closes[earlier - trend + 1:earlier + 1], Decimal(0)) / trend
        crossed = rsi[index - 1] <= oversold < rsi[index]
        if crossed and trend_mean > earlier_mean:
            proposals.append(sdk.marketable_proposal(
                STRATEGY.strategy_id, context["symbol"], bars, bar,
                facts={"rsi": str(rsi[index].quantize(Decimal("0.01"))),
                       "rsi_before": str(rsi[index - 1].quantize(Decimal("0.01"))),
                       "trend_mean": str(trend_mean), "trend_mean_24h_ago": str(earlier_mean),
                       "period": period,
                       "oversold": str(oversold), "trend_hours": trend},
                parameters=context.get("parameters")))
    return proposals


STRATEGY = sdk.mechanical_strategy(
    name="EXAMPLE_RSI_REVERSION", version=1,
    description="Teaching example: RSI(14) crosses back above 30 while the 72-hour mean close "
                "rises; marketable entry; CRYPTO_TRADE_PLAN_V1's stop, 1.5R target, 24 hours.",
    signals=signals,
    trigger_rule="MARKETABLE_AT_SIGNAL (EXAMPLE_RSI_REVERSION_V1 oversold cross in an uptrend on "
                 "a completed 1-hour bar)",
    parameters={"rsi_period": PERIOD, "oversold": str(OVERSOLD), "trend_hours": TREND,
                "timeframe": sdk.TIMEFRAME},
    history_variants=HISTORY_VARIANTS,
    # The price is moving up off a dip when the signal arrives: a marketable entry either way.
    entry_types=frozenset({sdk.BREAKOUT, sdk.IMMEDIATE}),
    history_hours=TREND + SLOPE_BARS + PERIOD + 2,
)
