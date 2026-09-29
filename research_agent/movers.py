"""What moved: the evening review of the whole universe over one New York day (plan 5c).

``movers`` measures every coin the day's research context listed, not only the picks:
from Coinbase's public 1-hour candles, each coin's return over the window (its first
open to its last close), its largest move up and down from that open, its volume per
hour against the seven days before the window, and the bar where its move started
(``technicals.MOVE_START_RULE``, the app's ``MARKET_REALITY_V1`` rule on 1-hour bars, and
``HALF_MOVE_RULE``, the first bar past half of the move). The movers are the 5 largest
rises, the 5 largest falls, and every coin whose return is at least 5% either way.

This is the kit's own view, for reading. The app's nightly ``MARKET_REALITY_V1`` (1-minute
bars) is the record: its movers are the post-mortem subjects and its ``move_start_at`` the
time a post-mortem's knowability is judged against. The research checklist scores its
TECHNICAL patterns on this file's start-of-day state tags, with the app's movers as hits.

The window is a New York calendar day (``day_window``; 23 or 25 hours on a daylight-
saving change) or the last whole hours before retrieval (``hours_window``). A bar counts
only if it had ended when it was retrieved, and a day that has not ended yet is refused
rather than measured in part. Pure: the network call is ``market.fetch_hourly_span``.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from research_agent import technicals
from research_agent.technicals import (
    HOUR,
    between,
    move_bar_json,
    pct_text,
    plain,
    technical_state,
    volume_ratio,
    window_move,
)

MOVERS_SCHEMA = "RESEARCH_AGENT_MOVERS_V1"
RUN_TIMEZONE = "America/New_York"
TOP_N = 5
BIG_MOVE_PCT = Decimal(5)
BASELINE = timedelta(days=7)
TOP_UP, TOP_DOWN, BIG_MOVE = "TOP_UP", "TOP_DOWN", "RETURN_AT_LEAST_5_PCT"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{COIN}-USD/candles"


class MoversError(Exception):
    """A refused window (a day not over yet, a bad hour count); the message is a code."""


def day_window(day, *, timezone=RUN_TIMEZONE):
    """``[start, end)`` in UTC of one calendar day in ``timezone``."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    return start, end


def hours_window(retrieved_at, hours):
    """The last ``hours`` whole hours that had ended by ``retrieved_at``."""
    if not isinstance(hours, int) or hours < 1:
        raise MoversError("HOURS_MUST_BE_A_POSITIVE_WHOLE_NUMBER")
    end = retrieved_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=hours), end


def check_day_over(end, retrieved_at, day):
    if end > retrieved_at:
        raise MoversError(
            f"DAY_NOT_OVER: the New York day {day.isoformat()} ends at {end.isoformat()}; "
            "run after it ends, or measure the last hours with --hours"
        )


def _coin_record(coin, symbol, rows, *, start, end, retrieved_at):
    """One coin's measurements, or ``(None, reason)`` without a completed bar in the window."""
    bars = technicals.complete_hourly_bars(rows, retrieved_at=retrieved_at)
    window = between(bars, start, end)
    move = window_move(window)
    if move is None:
        return None, "NO_COMPLETED_BARS_IN_WINDOW"
    hours = int((end - start) / HOUR)
    baseline = between(bars, start - BASELINE, start)
    ratio = volume_ratio(window, hours, baseline, int(BASELINE / HOUR))
    record = {
        "symbol": symbol,
        "bars_expected": hours, "bars_used": len(window),
        "open": plain(move.open), "close": plain(move.close),
        "high": plain(move.high), "low": plain(move.low),
        "return_pct": pct_text(move.return_pct),
        "up_excursion_pct": pct_text(move.up_excursion_pct),
        "down_excursion_pct": pct_text(move.down_excursion_pct),
        "volume": plain(move.volume),
        "baseline_bars": len(baseline),
        "volume_ratio_7d": pct_text(ratio),
        "move_start": move_bar_json(move.move_start, coin, window_start=start,
                                    reference=move.open),
        "half_move_bar": move_bar_json(move.half_move, coin, window_start=start,
                                       reference=move.open),
        # The state at the window's start, from the 7 days before it: what the morning
        # could have seen. The research checklist scores its TECHNICAL patterns on this.
        "pre_window": technical_state(bars, as_of=start),
    }
    return (record, move, bars), None


def select_movers(returns):
    """``{coin: exact return %}`` -> ``{coin: {"reasons", "rank"}}``, largest move first.

    ``reasons`` holds TOP_UP (among the 5 largest rises), TOP_DOWN (among the 5 largest
    falls) and RETURN_AT_LEAST_5_PCT; ``rank`` is the place among the rises or the falls
    (1 = largest), or ``None`` for a 5%+ move outside both top fives. A coin that did not
    move is in neither top five."""
    up = sorted((coin for coin, value in returns.items() if value > 0),
                key=lambda coin: (-returns[coin], coin))[:TOP_N]
    down = sorted((coin for coin, value in returns.items() if value < 0),
                  key=lambda coin: (returns[coin], coin))[:TOP_N]
    chosen = {}
    for rank, coin in enumerate(up, 1):
        chosen[coin] = {"reasons": [TOP_UP], "rank": rank}
    for rank, coin in enumerate(down, 1):
        chosen[coin] = {"reasons": [TOP_DOWN], "rank": rank}
    for coin, value in returns.items():
        if abs(value) >= BIG_MOVE_PCT:
            chosen.setdefault(coin, {"reasons": [], "rank": None})["reasons"].append(BIG_MOVE)
    order = sorted(chosen, key=lambda coin: (-abs(returns[coin]), coin))
    return {coin: chosen[coin] for coin in order}


def build_movers(symbols, fetched, *, start, end, day=None):
    """``movers.json`` for the context's ``symbols`` from ``market.fetch_hourly_span``'s
    result (which must cover the 7 days before ``start`` too)."""
    retrieved_at = datetime.fromisoformat(fetched["retrieved_at"])
    coins, excluded, detail, returns = {}, {}, {}, {}
    for symbol in sorted(symbols):
        coin = symbol.split("/")[0]
        raw = (fetched.get("coinbase") or {}).get(coin)
        if raw is None:
            excluded[coin] = (fetched.get("excluded") or {}).get(coin, "NO_COINBASE_DATA")
            continue
        found, reason = _coin_record(coin, symbol, raw["candles_1h"], start=start, end=end,
                                     retrieved_at=retrieved_at)
        if found is None:
            excluded[coin] = reason
            continue
        record, move, bars = found
        coins[coin] = record
        detail[coin] = (move, bars)
        returns[coin] = move.return_pct

    movers = []
    for coin, chosen in select_movers(returns).items():
        move, bars = detail[coin]
        record = coins[coin]
        movers.append({
            "coin": coin, "symbol": record["symbol"], "reasons": chosen["reasons"],
            "rank": chosen["rank"],
            "return_pct": record["return_pct"],
            "up_excursion_pct": record["up_excursion_pct"],
            "down_excursion_pct": record["down_excursion_pct"],
            "volume_ratio_7d": record["volume_ratio_7d"],
            "move_start": record["move_start"],
            "half_move_bar": record["half_move_bar"],
            # The state just before the move-start bar: what was visible before the move.
            "pre_move": (technical_state(bars, as_of=move.move_start.bar.started_at)
                         if move.move_start else None),
            "bars": [technicals.bar_json(bar, coin) for bar in move.bars],
        })

    hours = int((end - start) / HOUR)
    window = ({"kind": "NEW_YORK_DAY", "day": day.isoformat(), "timezone": RUN_TIMEZONE}
              if day is not None else {"kind": "LAST_HOURS"})
    return {
        "schema": MOVERS_SCHEMA,
        "retrieved_at": retrieved_at.isoformat(),
        "window": {**window, "start": start.isoformat(), "end": end.isoformat(),
                   "hours": hours},
        "baseline": {"start": (start - BASELINE).isoformat(), "end": start.isoformat(),
                     "hours": int(BASELINE / HOUR)},
        "source": {"provider": "COINBASE_EXCHANGE_PUBLIC_API", "feed": "1-hour candles",
                   "url_pattern": COINBASE_CANDLES},
        "rules": {
            "bars": "A 1-hour bar counts only if it had ended at or before retrieved_at.",
            "return_pct": "(last close / first open - 1) * 100 over the window's bars.",
            "excursions": "Highest high and lowest low against the first open, in percent.",
            "volume_ratio_7d": ("The window's volume per hour over the baseline's volume per "
                                "hour; an hour with no candle traded nothing."),
            "move_start": technicals.MOVE_START_RULE,
            "half_move_bar": technicals.HALF_MOVE_RULE,
            "movers": (f"The {TOP_N} largest rises, the {TOP_N} largest falls, and every coin "
                       f"whose return is at least {BIG_MOVE_PCT}% either way."),
            "technical_tags": technicals.TAG_MEANINGS,
        },
        "universe_count": len(symbols),
        "measured_count": len(coins),
        "movers": movers,
        "coins": coins,
        "excluded": excluded,
    }


def summary_line(result):
    window = result["window"]
    label = (f"New York day {window['day']}" if window["kind"] == "NEW_YORK_DAY"
             else f"the last {window['hours']} hours")
    ups = sorted((m for m in result["movers"] if TOP_UP in m["reasons"]),
                 key=lambda m: m["rank"])
    downs = sorted((m for m in result["movers"] if TOP_DOWN in m["reasons"]),
                   key=lambda m: m["rank"])

    def fmt(rows):
        return ", ".join(f"{row['coin']} {row['return_pct']}%" for row in rows) or "none"

    return (f"movers over {label}: {len(result['movers'])} movers among "
            f"{result['measured_count']} measured coins ({len(result['excluded'])} excluded). "
            f"Up: {fmt(ups)}. Down: {fmt(downs)}.")
