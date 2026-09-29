"""Pure 1-hour-bar measurements for the daily market review (plan section 5c).

A window's return and largest excursions, its volume against the seven days before it,
the bar where its move started, and a coin's technical state at an instant: the facts
``movers``, ``outlook`` and ``postmortem`` put in front of the research session, so it
never has to eyeball them.

No I/O and no clock reads, like ``levels``: every function takes completed bars and
instants as data. A bar counts only when it had ended by the time it was retrieved
(``complete_hourly_bars`` reuses ``market.aggregate``'s cut), as everywhere in the kit.
Decimal arithmetic throughout; percentages are rounded only when written out.

The technical-state tags are research-side descriptions with fixed thresholds, not trading
rules: nothing here admits, sizes, stops or targets anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import ROUND_HALF_UP, Decimal

from research_agent.market import aggregate, bar_to_json

HOUR = timedelta(hours=1)
LOOKBACK = timedelta(days=7)
RECENT = timedelta(hours=24)
HUNDRED = Decimal(100)
CENT = Decimal("0.01")

# Fixed thresholds for the state tags (research emphasis only).
VOLUME_SPIKE_RATIO = Decimal(2)
NEAR_EXTREME_PCT = Decimal(2)
TREND_PCT = Decimal(10)
TIGHT_RANGE_PCT = Decimal(3)
TAG_MEANINGS = {
    "VOLUME_SPIKE": "volume of the last 24 hours at least 2x the 7-day hourly average",
    "NEAR_7D_HIGH": "last close within 2% below the 7-day high",
    "NEAR_7D_LOW": "last close within 2% above the 7-day low",
    "TREND_UP_7D": "up at least 10% over 7 days",
    "TREND_DOWN_7D": "down at least 10% over 7 days",
    "TIGHT_RANGE_24H": "the last 24 hours' high-low range at most 3% of the last close",
}
TAGS = tuple(TAG_MEANINGS)

MOVE_START_RULE = (
    "MARKET_REALITY_V1's rule, here on the kit's 1-hour bars: for a rise, the bar of the "
    "lowest low at or before the first bar of the window's high; for a fall, the bar of the "
    "highest high at or before the first bar of the window's low; the latest such bar on a "
    "tie. None when the window did not move. The app's own move_start_at (1-minute bars) is "
    "the reference the post-mortem uses; this one is for reading next to it."
)
HALF_MOVE_RULE = (
    "The first completed 1-hour bar whose close reached at least half of the window's move "
    "(from the reference price, the window's first open or a trade's entry, to its final "
    "price, the window's last close or a trade's exit), in the move's direction. When no "
    "close reached it (a trade that exited inside a bar), the first bar whose high (a rise) "
    "or low (a fall) did, with basis EXTREME. None when the move is zero."
)


def plain(value):
    """A Decimal as a fixed-point string ("1E-9" reads as "0.000000001")."""
    value = Decimal(value)
    return (format(value.normalize(), "f") if value != value.to_integral()
            else format(value.quantize(Decimal(1)), "f"))


def pct_text(value):
    """A percentage (or ratio) rounded to 2 places for the written file, or ``None``."""
    return None if value is None else str(value.quantize(CENT, ROUND_HALF_UP))


def change_pct(new, old):
    """``(new / old - 1) * 100``, or ``None`` when ``old`` is zero or missing."""
    if old is None or new is None or old == 0:
        return None
    return (new / old - 1) * HUNDRED


def complete_hourly_bars(candles_1h, *, retrieved_at):
    """Coinbase 1-hour candle rows as ``Bar``s, oldest first: one per hour (a row returned
    twice by overlapping requests counts once) and only those that had ended at or before
    ``retrieved_at``."""
    unique = {}
    for row in candles_1h:
        unique[int(row[0])] = row
    return aggregate(list(unique.values()), 3600, retrieved_at=retrieved_at)


def between(bars, start, end):
    """The bars that started at or after ``start`` and ended at or before ``end``."""
    return [bar for bar in bars if bar.started_at >= start and bar.started_at + HOUR <= end]


def bar_json(bar, coin):
    """One bar for a written file, with the kit's usual bar ID (``SOL-1h-20260928T1400Z``)."""
    return {"bar_id": bar.bar_id(coin, "1h"), **bar_to_json(bar)}


# --- A window's move ------------------------------------------------------------------------

@dataclass(frozen=True)
class MoveBar:
    index: int  # into the window's bars
    bar: object  # market.Bar
    basis: str  # the rule's basis: TROUGH_BEFORE_HIGH, PEAK_BEFORE_LOW, CLOSE or EXTREME


def find_move_start(bars, *, rising):
    """Where a window's move started (``MOVE_START_RULE``), or ``None`` without bars."""
    if not bars:
        return None
    if rising:
        peak = max(bar.high for bar in bars)
        end = next(i for i, bar in enumerate(bars) if bar.high == peak)
        trough = min(bar.low for bar in bars[:end + 1])
        index = max(i for i in range(end + 1) if bars[i].low == trough)
        return MoveBar(index, bars[index], "TROUGH_BEFORE_HIGH")
    bottom = min(bar.low for bar in bars)
    end = next(i for i, bar in enumerate(bars) if bar.low == bottom)
    top = max(bar.high for bar in bars[:end + 1])
    index = max(i for i in range(end + 1) if bars[i].high == top)
    return MoveBar(index, bars[index], "PEAK_BEFORE_LOW")


def find_half_move(bars, *, reference, final):
    """The first bar past half of a window's move (``HALF_MOVE_RULE``), or ``None``."""
    move = final - reference
    if move == 0 or not bars:
        return None
    half = reference + move / 2
    for index, bar in enumerate(bars):
        if (bar.close >= half) if move > 0 else (bar.close <= half):
            return MoveBar(index, bar, "CLOSE")
    for index, bar in enumerate(bars):
        if (bar.high >= half) if move > 0 else (bar.low <= half):
            return MoveBar(index, bar, "EXTREME")
    return None


@dataclass(frozen=True)
class WindowMove:
    bars: tuple
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    return_pct: Decimal
    up_excursion_pct: Decimal  # (highest high / open - 1) * 100
    down_excursion_pct: Decimal  # (lowest low / open - 1) * 100
    volume: Decimal
    move_start: MoveBar | None  # MOVE_START_RULE
    half_move: MoveBar | None  # HALF_MOVE_RULE


def window_move(bars):
    """A window's move from its first open to its last close, or ``None`` without bars."""
    if not bars:
        return None
    open_, close = bars[0].open, bars[-1].close
    if open_ <= 0:
        return None
    high, low = max(bar.high for bar in bars), min(bar.low for bar in bars)
    return WindowMove(
        bars=tuple(bars), open=open_, close=close, high=high, low=low,
        return_pct=change_pct(close, open_), up_excursion_pct=change_pct(high, open_),
        down_excursion_pct=change_pct(low, open_),
        volume=sum((bar.volume for bar in bars), Decimal(0)),
        move_start=(find_move_start(bars, rising=close > open_) if close != open_ else None),
        half_move=find_half_move(bars, reference=open_, final=close),
    )


def volume_ratio(window_bars, window_hours, baseline_bars, baseline_hours):
    """The window's volume per hour over the baseline's volume per hour, by nominal hours
    (an hour with no candle traded nothing), or ``None`` when the baseline traded nothing."""
    baseline = sum((bar.volume for bar in baseline_bars), Decimal(0))
    if baseline <= 0 or window_hours <= 0 or baseline_hours <= 0:
        return None
    window = sum((bar.volume for bar in window_bars), Decimal(0))
    return (window / Decimal(window_hours)) / (baseline / Decimal(baseline_hours))


def move_bar_json(move_bar, coin, *, window_start, reference):
    """A ``MoveBar`` for a written file: the bar, and the move from ``reference`` to its
    close."""
    if move_bar is None:
        return None
    bar = move_bar.bar
    return {
        "bar_id": bar.bar_id(coin, "1h"),
        "started_at": bar.started_at.isoformat(),
        "ended_at": (bar.started_at + HOUR).isoformat(),
        "close": plain(bar.close),
        "cumulative_return_pct": pct_text(change_pct(bar.close, reference)),
        "hours_into_window": int((bar.started_at - window_start) / HOUR),
        "basis": move_bar.basis,
    }


def floor_hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def bar_at(bars, moment):
    """The 1-hour bar that contains ``moment``, or ``None``."""
    start = floor_hour(moment.astimezone(UTC))
    return next((bar for bar in bars if bar.started_at == start), None)


# --- A coin's technical state at an instant --------------------------------------------------

def technical_state(bars, *, as_of):
    """Facts and tags from the completed bars that ended by ``as_of``: the 7 days and the
    24 hours before it. Every number is computed from the bars, never estimated; a fact
    that cannot be computed is ``None`` and its tag is simply not set."""
    week = between(bars, as_of - LOOKBACK, as_of)
    day = between(bars, as_of - RECENT, as_of)
    state = {"as_of": as_of.isoformat(), "bars_7d": len(week), "bars_24h": len(day)}
    if not week or not day:
        return {**state, "tags": [], "unavailable": "NO_COMPLETED_BARS_BEFORE_THIS_INSTANT"}
    close = week[-1].close
    high_7d, low_7d = max(bar.high for bar in week), min(bar.low for bar in week)
    high_24h, low_24h = max(bar.high for bar in day), min(bar.low for bar in day)
    from_high = change_pct(close, high_7d)
    from_low = change_pct(close, low_7d)
    return_24h = change_pct(close, day[0].open)
    return_7d = change_pct(close, week[0].open)
    range_24h = (high_24h - low_24h) / close * HUNDRED if close > 0 else None
    ratio = volume_ratio(day, int(RECENT / HOUR), week, int(LOOKBACK / HOUR))
    tags = []
    if ratio is not None and ratio >= VOLUME_SPIKE_RATIO:
        tags.append("VOLUME_SPIKE")
    if from_high is not None and from_high >= -NEAR_EXTREME_PCT:
        tags.append("NEAR_7D_HIGH")
    if from_low is not None and from_low <= NEAR_EXTREME_PCT:
        tags.append("NEAR_7D_LOW")
    if return_7d is not None and return_7d >= TREND_PCT:
        tags.append("TREND_UP_7D")
    if return_7d is not None and return_7d <= -TREND_PCT:
        tags.append("TREND_DOWN_7D")
    if range_24h is not None and range_24h <= TIGHT_RANGE_PCT:
        tags.append("TIGHT_RANGE_24H")
    return {
        **state,
        "close": plain(close),
        "high_7d": plain(high_7d), "low_7d": plain(low_7d),
        "distance_from_7d_high_pct": pct_text(from_high),
        "distance_from_7d_low_pct": pct_text(from_low),
        "return_24h_pct": pct_text(return_24h),
        "return_7d_pct": pct_text(return_7d),
        "range_24h_pct": pct_text(range_24h),
        "volume_ratio_24h_vs_7d": pct_text(ratio),
        "tags": tags,
    }
