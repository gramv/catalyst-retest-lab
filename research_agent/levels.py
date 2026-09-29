"""The level rules the real Jev accepted on 2026-09-27 (research3/levels2.py, evidence in
``artifacts/real-jev-topk-2026-09-27/README.md`` and
``.claude/.../memory/catalyst-research-agent-method.md``), tightened where that evidence
showed a gap. Pure: no I/O, no clock reads — every function takes bars and a mid price as
data (``market.py`` is the only module that touches a clock or a network).

Rules, in the order tried (the profile's timeframes; rule A before rule B; windows 20, 24,
30). A research profile names the timeframes, their order and the entry band; everything
else below is shared by every profile (its 2% stop-distance and 2R minimums are the app's
own rules):

* ``DAILY_V1`` (the default, the daily 08:00 run): timeframes 4h, 6h, 1d, 2h, 1h; entry
  band 0.6%-6% below the mid.
* ``INTRADAY_V2`` (the 2-hourly runs): timeframes 1h, 2h, 4h, 6h, 1d, shortest first; entry
  band 0.3%-6% below the mid. Its search holds all of ``DAILY_V1``'s (the same timeframes,
  a wider band), so on the same market data it finds a setup for every coin ``DAILY_V1``
  does, and for a coin with a qualifying 1-hour setup it keeps that one.
* ``INTRADAY_V1`` (the first intraday profile, kept): timeframes 1h, 2h, 4h, shortest
  first; entry band 0.3%-3% below the mid. On 2026-09-28's 33 coins it found 2 setups
  where ``DAILY_V1`` found 12: 1-4 hour structure within 3% of price rarely allows the
  2% minimum stop.

* **Entry** ``T``: a swing low — the low of a bar with two bars lower-or-equal on each
  side — that has *held*: no later cited bar's low is below it. ``T`` must sit inside the
  profile's entry band below the current mid (``(mid - T) / mid``).
* **Max entry** ``M``: ``T`` x 1.0015, rounded UP to the coin's price increment, so a
  limit buy at ``M`` is never worse than 0.15% above the entry.
* **Stop** ``S``, rule A: 0.4% under the lowest low of *every* cited bar in the window.
  Rule B (tried only when rule A finds nothing): 0.4% under a held lower pivot below the
  entry — bars *before* that pivot formed may have traded lower; the structure "starts"
  at the pivot, and ``build`` states that when it writes the pick's reasoning.
* **Stop distance**: ``(M - S) / M`` must be at least 2%.
* **Target** ``P``: the window's own highest high, provided the most recent bar did not
  make (or tie) it — see ``_window_target`` for why, and at least 2R away from ``M``
  (``(P - M) / (M - S) >= 2``).

A coin with no qualifying window on any tried rule/timeframe/window combination has no
setup; ``find_setup`` returns ``None`` and every combination's reason, for ``build`` to
turn into the report's ``skipped`` entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from research_agent.market import (
    DAILY_LOOKBACK_DAYS,
    HOURLY_LOOKBACK_HOURS,
    bar_from_json,
    bar_to_json,
)

RULES = ("A", "B")
# DAILY_V1's timeframe order and entry band (the default profile, below).
TIMEFRAME_ORDER = ("4h", "6h", "1d", "2h", "1h")
WINDOWS = (20, 24, 30)
ENTRY_BAND_MIN = Decimal("0.006")
ENTRY_BAND_MAX = Decimal("0.06")
# Shared by every profile: the app's own rules (stop >= 2% under the max entry, >= 2R).
MAX_ENTRY_FACTOR = Decimal("1.0015")
STOP_BUFFER_FACTOR = Decimal("0.996")
MIN_STOP_DISTANCE = Decimal("0.02")
MIN_REWARD_RISK = Decimal("2")


class LevelsError(Exception):
    """A sanitized, code-like failure (bad increment, empty series, ...)."""


def _percent_text(fraction):
    """``Decimal("0.006")`` -> ``"0.6"``, ``Decimal("0.06")`` -> ``"6"``."""
    value = (fraction * 100).normalize()
    return format(value, "f")


@dataclass(frozen=True)
class ResearchProfile:
    """A named level-search profile: which timeframes are tried, in which order, and how
    far below the mid an entry may sit. ``hourly_lookback_hours`` and
    ``daily_lookback_days`` are what ``market`` fetches for it (0 days: no daily candles).
    The stop, stop-distance and reward:risk rules are not profile settings: every profile
    shares them."""

    name: str
    timeframes: tuple
    windows: tuple
    entry_band_min: Decimal
    entry_band_max: Decimal
    hourly_lookback_hours: int
    daily_lookback_days: int

    @property
    def band_text(self):
        return f"{_percent_text(self.entry_band_min)}-{_percent_text(self.entry_band_max)}%"

    @property
    def timeframes_text(self):
        return "/".join(self.timeframes)

    @property
    def windows_text(self):
        return f"{min(self.windows)}-{max(self.windows)}"

    def fetch_covers(self, other):
        """Whether market data fetched for this profile holds everything ``other`` needs."""
        return (self.hourly_lookback_hours >= other.hourly_lookback_hours
                and self.daily_lookback_days >= other.daily_lookback_days)


DAILY_V1 = ResearchProfile(
    name="DAILY_V1", timeframes=TIMEFRAME_ORDER, windows=WINDOWS,
    entry_band_min=ENTRY_BAND_MIN, entry_band_max=ENTRY_BAND_MAX,
    hourly_lookback_hours=HOURLY_LOOKBACK_HOURS, daily_lookback_days=DAILY_LOOKBACK_DAYS,
)
# 30 four-hour bars span 120 hours, plus up to 4 more for the bucket still forming; a week of
# 1-hour candles leaves room for the hours Coinbase omits on a thin coin (a bucket missing an
# hour is dropped). No 6h or daily bars, so no daily candles.
INTRADAY_V1 = ResearchProfile(
    name="INTRADAY_V1", timeframes=("1h", "2h", "4h"), windows=WINDOWS,
    entry_band_min=Decimal("0.003"), entry_band_max=Decimal("0.03"),
    hourly_lookback_hours=168, daily_lookback_days=0,
)
# DAILY_V1's timeframes shortest first and its band widened to 0.3%, on DAILY_V1's own market
# data (the same fetch, so a coin the daily profile finds is never short of bars here).
INTRADAY_V2 = ResearchProfile(
    name="INTRADAY_V2", timeframes=("1h", "2h", "4h", "6h", "1d"), windows=WINDOWS,
    entry_band_min=Decimal("0.003"), entry_band_max=Decimal("0.06"),
    hourly_lookback_hours=HOURLY_LOOKBACK_HOURS, daily_lookback_days=DAILY_LOOKBACK_DAYS,
)
DEFAULT_PROFILE = DAILY_V1
# The CLI's --profile choices: "intraday" is the current intraday profile.
PROFILES = {"daily": DAILY_V1, "intraday": INTRADAY_V2, "intraday-v1": INTRADAY_V1}
PROFILES_BY_NAME = {profile.name: profile for profile in PROFILES.values()}


@dataclass(frozen=True)
class LevelSetup:
    """A qualifying setup: everything ``build`` needs to cite and phrase it."""

    rule: str  # "A" or "B"
    timeframe: str
    window: int
    bars: tuple  # the cited window, oldest first, length == window
    entry_index: int  # index into ``bars`` of the entry/T bar
    stop_index: int  # index into ``bars`` the stop is 0.4% under
    target_index: int  # index into ``bars`` of the target bar
    entry: Decimal
    max_entry: Decimal
    stop: Decimal
    target: Decimal
    reward_risk: Decimal


def round_price(value, increment, mode=ROUND_DOWN):
    """``value`` rounded to a whole multiple of ``increment`` (Decimal arithmetic only)."""
    inc = Decimal(str(increment))
    if inc <= 0:
        raise LevelsError("NON_POSITIVE_PRICE_INCREMENT")
    return (Decimal(str(value)) / inc).to_integral_value(mode) * inc


def _held_pivots(lows):
    """Local-minimum bars (2 either side, ties allowed) no later bar's low has broken.

    This is both the entry rule ("no later cited bar has traded below it") and rule B's
    stop-pivot rule ("a held lower pivot"); both search the same held-pivot set.
    """
    pivots = [i for i in range(2, len(lows) - 2)
              if all(lows[i] <= lows[j] for j in range(i - 2, i + 3))]
    return [i for i in pivots if all(lows[j] >= lows[i] for j in range(i + 1, len(lows)))]


def _window_target(highs):
    """The window's own highest high, unless the most recent bar made (or tied) it.

    Real-Jev evidence (2026-09-27 run 2, ``artifacts/real-jev-topk-2026-09-27/README.md``):
    AVAX, ARB and BAT were vetoed ``LEVELS_SUPPORTED_BY_BARS_NO`` for targeting the latest
    bar's high ("just above the current price, so it is not real resistance"); BTC and
    PEPE for "a higher high in the same window" than their chosen target. Both come from
    one gap in the original script (research3/levels2.py): it picked the *nearest*
    qualifying pivot high, not the window's actual peak, so a higher high could sit
    elsewhere in the same cited bars. This version always targets the window's true
    maximum — nothing cited is higher than it, by construction — and refuses when that
    maximum is the bar that just printed: a level price has not yet pulled back from is
    the current thrust, not proven resistance. A coin whose latest bar is making a fresh
    window high has no valid target here; that is a breakout setup, which stays disabled
    until a backtest enables it (``docs/CRYPTO-AGENT-LOOP.md`` 4.4).
    """
    peak = max(highs)
    if highs[-1] >= peak:
        return None
    return highs.index(peak), peak


def _entry_band(lows, mid, profile):
    """Held pivot-low indices whose low sits inside ``profile``'s entry band below ``mid``
    (0.6%-6% for ``DAILY_V1``, 0.3%-3% for ``INTRADAY_V1``)."""
    return [i for i in _held_pivots(lows)
            if profile.entry_band_min <= (mid - lows[i]) / mid <= profile.entry_band_max]


def _rule_a(bars, lows, timeframe, mid, increment, target_index, target, profile):
    window_low = min(lows)
    stop_index = lows.index(window_low)
    stop = round_price(window_low * STOP_BUFFER_FACTOR, increment, ROUND_DOWN)
    best = None
    for i in _entry_band(lows, mid, profile):
        entry = round_price(lows[i], increment, ROUND_DOWN)
        max_entry = round_price(entry * MAX_ENTRY_FACTOR, increment, ROUND_UP)
        if max_entry <= stop or (max_entry - stop) / max_entry < MIN_STOP_DISTANCE:
            continue
        reward_risk = (target - max_entry) / (max_entry - stop)
        if reward_risk < MIN_REWARD_RISK:
            continue
        if best is None or (entry, reward_risk) > (best[0], best[1]):
            best = (entry, reward_risk, i, max_entry)
    if best is None:
        return None
    entry, reward_risk, entry_index, max_entry = best
    return LevelSetup(rule="A", timeframe=timeframe, window=len(bars), bars=tuple(bars),
                      entry_index=entry_index, stop_index=stop_index,
                      target_index=target_index, entry=entry, max_entry=max_entry,
                      stop=stop, target=target, reward_risk=reward_risk)


def _rule_b(bars, lows, timeframe, mid, increment, target_index, target, profile):
    held = _held_pivots(lows)
    best = None
    for i in _entry_band(lows, mid, profile):
        entry = round_price(lows[i], increment, ROUND_DOWN)
        max_entry = round_price(entry * MAX_ENTRY_FACTOR, increment, ROUND_UP)
        lower_pivots = sorted((j for j in held if j < i and lows[j] < lows[i]),
                              key=lambda j: -lows[j])  # nearest-below first
        for j in lower_pivots:
            stop = round_price(lows[j] * STOP_BUFFER_FACTOR, increment, ROUND_DOWN)
            if max_entry <= stop or (max_entry - stop) / max_entry < MIN_STOP_DISTANCE:
                continue  # too close; a farther (lower) pivot gives more distance
            reward_risk = (target - max_entry) / (max_entry - stop)
            if reward_risk < MIN_REWARD_RISK:
                break  # a farther pivot only makes this harder; stop trying
            if best is None or (entry, reward_risk) > (best[0], best[1]):
                best = (entry, reward_risk, i, j, max_entry, stop)
            break
    if best is None:
        return None
    entry, reward_risk, entry_index, stop_index, max_entry, stop = best
    return LevelSetup(rule="B", timeframe=timeframe, window=len(bars), bars=tuple(bars),
                      entry_index=entry_index, stop_index=stop_index,
                      target_index=target_index, entry=entry, max_entry=max_entry,
                      stop=stop, target=target, reward_risk=reward_risk)


def find_setup(series_by_timeframe, *, mid, increment, rules=RULES, timeframes=None,
               windows=None, profile=DEFAULT_PROFILE):
    """The first qualifying setup, trying rule A across every timeframe/window, then
    rule B; ``(None, tried)`` with every combination's reason when none qualifies.

    ``series_by_timeframe`` is ``{timeframe: [Bar, ...]}`` (``market.all_series``'
    shape), oldest bar first, completed bars only. ``mid`` and ``increment`` are the
    coin's current mid price and Alpaca price increment. ``profile`` sets the entry band
    and, unless ``timeframes``/``windows`` narrow them, the timeframes (in order) and
    windows tried; the default is ``DAILY_V1``.
    """
    mid = Decimal(str(mid))
    timeframes = profile.timeframes if timeframes is None else timeframes
    windows = profile.windows if windows is None else windows
    tried = []
    for rule in rules:
        for timeframe in timeframes:
            available = series_by_timeframe.get(timeframe) or []
            for n in windows:
                if len(available) < n:
                    tried.append(f"{timeframe}/{n}: only {len(available)} completed bars")
                    continue
                bars = available[-n:]
                lows = [bar.low for bar in bars]
                highs = [bar.high for bar in bars]
                target = _window_target(highs)
                if target is None:
                    tried.append(
                        f"{timeframe}/{n} rule {rule}: the window high is its most "
                        "recent bar (still extending, no held resistance to target)"
                    )
                    continue
                target_index, target_price = target
                setup_fn = _rule_a if rule == "A" else _rule_b
                setup = setup_fn(bars, lows, timeframe, mid, increment, target_index,
                                 target_price, profile)
                if setup is not None:
                    return setup, tried
                tried.append(
                    f"{timeframe}/{n} rule {rule}: no held higher low {profile.band_text} "
                    "below price with a stop >=2% away and a 2R cited target"
                )
    return None, tried


def setup_to_json(setup):
    """A ``LevelSetup`` as plain JSON, its bars included, for one run's ``levels.json``."""
    return {
        "rule": setup.rule, "timeframe": setup.timeframe, "window": setup.window,
        "bars": [bar_to_json(bar) for bar in setup.bars],
        "entry_index": setup.entry_index, "stop_index": setup.stop_index,
        "target_index": setup.target_index, "entry": str(setup.entry),
        "max_entry": str(setup.max_entry), "stop": str(setup.stop), "target": str(setup.target),
        "reward_risk": str(setup.reward_risk),
    }


def setup_from_json(data):
    return LevelSetup(
        rule=data["rule"], timeframe=data["timeframe"], window=data["window"],
        bars=tuple(bar_from_json(bar) for bar in data["bars"]),
        entry_index=data["entry_index"], stop_index=data["stop_index"],
        target_index=data["target_index"], entry=Decimal(data["entry"]),
        max_entry=Decimal(data["max_entry"]), stop=Decimal(data["stop"]),
        target=Decimal(data["target"]), reward_risk=Decimal(data["reward_risk"]),
    )


def range_position(bars, mid):
    """``mid``'s position in ``bars``' high-low range, as a percent (0 = the low, 100 =
    the high). For the report's skip-reason narrative, not a level rule."""
    lows, highs = [bar.low for bar in bars], [bar.high for bar in bars]
    low, high = min(lows), max(highs)
    if high <= low:
        return Decimal(50)
    return (Decimal(str(mid)) - low) / (high - low) * 100


def change_over(bars, back, mid):
    """Percent change of ``mid`` versus the close ``back`` completed bars ago, or
    ``None`` when the window is too short."""
    if back <= 0 or back > len(bars):
        return None
    reference = bars[-back].close
    if reference == 0:
        return None
    return (Decimal(str(mid)) / reference - 1) * 100
