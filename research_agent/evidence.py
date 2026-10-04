"""Evidence per coin for the kit's picks (method v8, owner direction 2026-10-02: no new pick
without solid evidence). Pure: every function reads the research context (``context.json``)
and the market document (``market.json``) the run already holds; nothing is fetched.

- ``trend_vs_average``: the live mid against the average of the last 20 completed daily
  closes (a pullback below its 20-day average is a pullback in a downtrend);
- ``two_hour_move`` and ``market_state``: the latest price against the close of the latest
  completed hourly bar that started at least two hours earlier, per coin and as the market's
  median (a market-wide drop fills every pullback entry at once: 10 entries in 10 minutes on
  2026-10-02);
- ``alpaca_volume_usd`` and ``fill_history``: Alpaca's own 24-hour USD volume for the coin and
  the agent's filled trades of it in the context's window (the paper venue fills a marketable
  order only when Alpaca's own venue prints: POL, LDO and WIF never filled).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal

from research_agent import market

TREND_DAYS = 20
TWO_HOURS = timedelta(hours=2)


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def alpaca_volume_usd(context, symbol):
    """The context's Alpaca 24-hour USD volume for ``symbol`` (``universe.coins[].volume_24h.usd``),
    or None when the context does not carry it."""
    universe = context.get("universe") if isinstance(context, dict) else None
    coins = (universe or {}).get("coins") if isinstance(universe, dict) else None
    for row in coins or ():
        if isinstance(row, dict) and row.get("symbol") == symbol:
            volume = row.get("volume_24h")
            return _decimal(volume.get("usd")) if isinstance(volume, dict) else None
    return None


def fill_history(context, symbol):
    """``(fills, last_closed_at, window_days)``: the agent's filled trades of ``symbol`` the
    context shows — its recent outcomes (closed trades with a buy fill, the app's own filter)
    plus its open trades. A coin with none has no fill evidence."""
    outcomes = context.get("recent_outcomes") if isinstance(context, dict) else None
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    closed = [row for row in outcomes.get("closed_trades") or ()
              if isinstance(row, dict) and row.get("symbol") == symbol]
    opened = [row for row in (context.get("open_trades") or ()) if isinstance(context, dict)
              if isinstance(row, dict) and row.get("symbol") == symbol
              and row.get("state") == "OPEN"]
    last = max((str(row.get("closed_at") or "") for row in closed), default="") or None
    window = outcomes.get("window_days")
    return len(closed) + len(opened), last, window if isinstance(window, int) else None


def trend_vs_average(raw_coin, mid, *, retrieved_at, days=TREND_DAYS):
    """``mid`` against the average of the last ``days`` completed daily closes, as a fraction
    (+0.05: 5% above); None without ``days`` completed daily bars or a usable mid."""
    rows = (raw_coin or {}).get("candles_1d")
    mid = _decimal(mid)
    if not rows or mid is None or mid <= 0:
        return None
    try:
        bars = market.daily_bars(rows, retrieved_at=retrieved_at)
    except (market.MarketDataError, ValueError, TypeError, ArithmeticError):
        return None  # A malformed candle row is no evidence.
    closes = [bar.close for bar in bars[-days:]]
    if len(closes) < days:
        return None
    average = sum(closes) / Decimal(days)
    return None if average <= 0 else mid / average - 1


def two_hour_move(raw_coin, *, retrieved_at):
    """The latest price (the Coinbase ticker's, else the last completed hourly close) against
    the close of the latest completed hourly bar that started at least two hours before the
    fetch, as a fraction; None without such bars."""
    rows = (raw_coin or {}).get("candles_1h")
    if not rows:
        return None
    try:
        bars = market.completed_bars(rows, market.TIMEFRAMES["1h"], retrieved_at=retrieved_at)
    except (market.MarketDataError, ValueError, TypeError, ArithmeticError):
        return None  # A malformed candle row is no evidence.
    earlier = [bar for bar in bars if bar.started_at <= retrieved_at - TWO_HOURS]
    if not earlier:
        return None
    reference = earlier[-1].close
    last = _decimal(((raw_coin or {}).get("ticker") or {}).get("price"))
    if last is None or last <= 0:
        last = bars[-1].close
    return None if reference <= 0 else last / reference - 1


def market_state(market_data):
    """``{"median_2h", "btc_2h", "coins"}`` over the market document's coins: the median
    two-hour move of every coin but BTC (as the operator's market check computes it), BTC's
    own, and how many coins had one. Values are fractions or None."""
    try:
        retrieved_at = datetime.fromisoformat(market_data["retrieved_at"])
    except (KeyError, TypeError, ValueError):
        return {"median_2h": None, "btc_2h": None, "coins": 0}
    moves = {}
    for coin, raw_coin in (market_data.get("coinbase") or {}).items():
        move = two_hour_move(raw_coin, retrieved_at=retrieved_at)
        if move is not None:
            moves[coin] = move
    others = [move for coin, move in moves.items() if coin != "BTC"]
    return {"median_2h": statistics.median(others) if others else None,
            "btc_2h": moves.get("BTC"), "coins": len(others)}


def in_selloff(state, limit):
    """Whether the market state crosses the sell-off limit (a positive fraction, e.g. 0.02):
    the median coin or BTC down by at least that much over two hours."""
    median, btc = state.get("median_2h"), state.get("btc_2h")
    return (median is not None and median <= -limit) or (btc is not None and btc <= -limit)


def pct(value, digits=1):
    """``+4.9%``-style text for a fraction, or ``n/a``."""
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def sentence(*, trend, volume, fills, window, state):
    """The pick's evidence sentence from the facts above (always the facts, never a verdict)."""
    parts = [f"{pct(trend)} vs its 20-day average" if trend is not None
             else "no 20-day daily history"]
    if volume is not None:
        parts.append(f"Alpaca 24 h volume ${volume:,.0f}")
    days = f" in the last {window} days" if window else ""
    parts.append(f"filled on Alpaca {fills} time{'s' if fills != 1 else ''}{days}" if fills
                 else f"no fill on Alpaca{days}")
    if state and state.get("median_2h") is not None:
        parts.append(f"the market's median coin {pct(state['median_2h'], 2)} in 2 h")
    return "Evidence: " + "; ".join(parts) + "."
