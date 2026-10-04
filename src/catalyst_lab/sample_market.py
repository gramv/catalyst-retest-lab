"""``SYNTHETIC_SAMPLE_BARS_V1``: a generated sample market for tutorials, tests and the local
simulated venue (package oss-packaging, 2026-10-03).

**Not market data.** Every bar here is generated from a fixed seed, so a newcomer can run the
history tester, the shadow and the local stack with no network and no account, and a test can
pin exact numbers. Results on these bars say nothing about any real market: the history tester
labels a synthetic run ``SYNTHETIC`` and its promotion check never passes, and
``strategy_paper.promote`` refuses a synthetic history report outright.

The model (deterministic, Decimal arithmetic, the same on every platform):

* Coins and their start price, hourly volatility, tick and base volume are ``COINS``; hour 0 is
  ``EPOCH`` (2025-01-01 00:00 UTC). Nothing exists before ``EPOCH``, and nothing is returned
  that has not completed by the reader's ``now``.
* Each hour's return is a regime drift (one of five levels from -0.02% to +0.02% an hour,
  redrawn every 240 hours), plus half the coin's variance, plus the coin's volatility times an
  Irwin-Hall draw (three uniforms; mean 0, variance 1), all from a ``random.Random``
  seeded with ``"<seed>|<symbol>|<hour>"`` (string seeds are hashed with SHA-512, so the draws
  are portable).
* Inside an hour the 60 one-minute closes follow a pinned random walk (a Brownian-bridge shape)
  from the previous hour's close to this hour's close; each minute gets small wicks. The hourly
  bar is the aggregate of its minutes, so the two timeframes always agree.
* Volume: the coin's base volume times a draw in [0.5, 1.5), times 4 on about 3% of hours.

The reader has the keyless public reader's interface (``bars(symbol, start, end, timeframe)``
and ``minute_bars(symbol, start, end)``; rows in Alpaca's ``t``/``o``/``h``/``l``/``c``/``v``
shape), so the history tester (``--source synthetic``), the strategy shadow and the local
simulated venue read it exactly as they read the real one.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext

D = Decimal
VERSION = "SYNTHETIC_SAMPLE_BARS_V1"
SOURCE_LABEL = "SYNTHETIC_SAMPLE_BARS_V1"
LABEL = ("SYNTHETIC: generated sample bars (SYNTHETIC_SAMPLE_BARS_V1) for tutorials and tests; "
         "not market data, never evidence for a strategy.")
EPOCH = datetime(2025, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
SEED = "catalyst-sample-market-v1"
REGIME_HOURS = 240
DRIFTS = (D("-0.0002"), D("-0.0001"), D("0"), D("0.0001"), D("0.0002"))
TIMEFRAMES = frozenset({"1Min", "1Hour"})
# symbol: (start price, hourly volatility, tick, base hourly volume)
COINS = {
    "BTC/USD": (D("60000"), D("0.006"), D("0.01"), D("40")),
    "ETH/USD": (D("2500"), D("0.008"), D("0.01"), D("400")),
    "SOL/USD": (D("150"), D("0.011"), D("0.001"), D("5000")),
    "DOGE/USD": (D("0.2"), D("0.013"), D("0.000001"), D("4000000")),
}
SYMBOLS = tuple(sorted(COINS))


class SampleMarketError(ValueError):
    """A code: an unknown coin, timeframe or window."""


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SampleMarketError("AWARE_DATETIME_REQUIRED")
    return value.astimezone(UTC)


def _uniform(rng):
    return D(rng.getrandbits(32)) / D(4294967296)


def _normalish(rng):
    """Irwin-Hall with three uniforms, centred and scaled to variance 1: (sum - 1.5) * 2."""
    return (_uniform(rng) + _uniform(rng) + _uniform(rng) - D("1.5")) * 2


def _rng(*parts):
    return random.Random("|".join(str(p) for p in parts))


class SyntheticBarReader:
    """Deterministic sample bars with the public reader's interface (see the module)."""

    source_label = SOURCE_LABEL

    def __init__(self, *, seed=SEED, now=None):
        self.seed = seed
        self._now = now
        self._closes = {}  # symbol -> [close of hour 0, close of hour 1, ...]
        self._hour_bars = {}  # (symbol, hour) -> the hourly row (small; minutes are not kept)

    def now(self):
        return _aware(self._now() if callable(self._now) else
                      (self._now or datetime.now(UTC)))

    def close(self):
        """Nothing to release; present so callers can treat every reader alike."""

    # --- the path ---------------------------------------------------------------------------

    def _coin(self, symbol):
        if symbol not in COINS:
            raise SampleMarketError("SAMPLE_SYMBOL_UNKNOWN")
        return COINS[symbol]

    def _quantize(self, symbol, value, rounding=ROUND_HALF_EVEN):
        tick = self._coin(symbol)[2]
        return max(tick, value.quantize(tick, rounding=rounding))

    def hour_close(self, symbol, hour):
        """The (tick-rounded) close of hour ``hour`` (0 = ``EPOCH``); hour -1 is the start
        price."""
        start, vol, _, _ = self._coin(symbol)
        if hour < 0:
            return self._quantize(symbol, start)
        closes = self._closes.setdefault(symbol, [])
        with localcontext() as context:
            context.prec = 28
            price = closes[-1] if closes else start
            while len(closes) <= hour:
                n = len(closes)
                regime = _rng(self.seed, "regime", symbol, n // REGIME_HOURS)
                drift = DRIFTS[regime.randrange(len(DRIFTS))]
                # The vol^2/2 term keeps the median path from sinking (a multiplicative walk).
                step = drift + vol * vol / 2 + vol * _normalish(_rng(self.seed, symbol, n))
                step = max(D("-0.2"), min(D("0.2"), step))
                price = price * (1 + step)
                closes.append(price)
        return self._quantize(symbol, closes[hour])

    def hour_minutes(self, symbol, hour):
        """The 60 one-minute rows of hour ``hour`` (aggregated, they are its hourly bar)."""
        _, vol, tick, base_volume = self._coin(symbol)
        opening, closing = self.hour_close(symbol, hour - 1), self.hour_close(symbol, hour)
        rng = _rng(self.seed, "minutes", symbol, hour)
        with localcontext() as context:
            context.prec = 28
            walk, total = [D(0)], D(0)
            scale = vol * opening / D(8)  # Minute noise: about the hour's move over sqrt(60).
            for _ in range(60):
                total += _normalish(rng) * scale
                walk.append(total)
            path = [opening + (closing - opening) * D(k) / 60 + walk[k] - walk[60] * D(k) / 60
                    for k in range(61)]
            volume_rng = _rng(self.seed, "volume", symbol, hour)
            volume = base_volume * (D("0.5") + _uniform(volume_rng))
            if volume_rng.random() < 0.03:
                volume *= 4
            per_minute = (volume / 60).quantize(D("0.000001"), rounding=ROUND_HALF_EVEN)
            rows, at = [], EPOCH + hour * HOUR
            for k in range(60):
                o = opening if k == 0 else self._quantize(symbol, path[k])
                c = closing if k == 59 else self._quantize(symbol, path[k + 1])
                wick = abs(_normalish(rng)) * scale / 4
                high = self._quantize(symbol, max(o, c) + wick, ROUND_CEILING)
                low = min(min(o, c), self._quantize(symbol, min(o, c) - wick, ROUND_FLOOR))
                low = max(low, tick)
                rows.append({"t": (at + k * MINUTE).isoformat().replace("+00:00", "Z"),
                             "o": str(o), "h": str(max(high, o, c)), "l": str(low),
                             "c": str(c), "v": str(per_minute)})
        return rows

    def hour_bar(self, symbol, hour):
        key = (symbol, hour)
        if key not in self._hour_bars:
            self._hour_bars[key] = self._aggregate(symbol, hour)
        return dict(self._hour_bars[key])

    def _aggregate(self, symbol, hour):
        minutes = self.hour_minutes(symbol, hour)
        return {"t": (EPOCH + hour * HOUR).isoformat().replace("+00:00", "Z"),
                "o": minutes[0]["o"], "h": str(max(D(r["h"]) for r in minutes)),
                "l": str(min(D(r["l"]) for r in minutes)), "c": minutes[-1]["c"],
                "v": str(sum((D(r["v"]) for r in minutes), D(0)))}

    # --- the reader interface ---------------------------------------------------------------

    def bars(self, symbol, start, end, timeframe):
        """Rows of completed bars starting in ``[start, end)``, ascending."""
        if timeframe not in TIMEFRAMES:
            raise SampleMarketError("INVALID_BAR_TIMEFRAME")
        self._coin(symbol)
        start, end = _aware(start), _aware(end)
        if not start < end:
            raise SampleMarketError("INVALID_BAR_WINDOW")
        now = self.now()
        step = HOUR if timeframe == "1Hour" else MINUTE
        first = max(start, EPOCH)
        last = min(end, now)
        rows = []
        hour = max(0, int((first - EPOCH) // HOUR))
        while EPOCH + hour * HOUR < last:
            hour_start = EPOCH + hour * HOUR
            if timeframe == "1Hour":
                if hour_start >= first and hour_start + step <= now and hour_start < end:
                    rows.append(self.hour_bar(symbol, hour))
            else:
                for row in self.hour_minutes(symbol, hour):
                    at = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                    if first <= at < end and at + step <= now:
                        rows.append(row)
            hour += 1
        return rows

    def minute_bars(self, symbol, start, end):
        return self.bars(symbol, start, end, "1Min")


__all__ = ["COINS", "EPOCH", "LABEL", "SEED", "SOURCE_LABEL", "SYMBOLS", "SampleMarketError",
           "SyntheticBarReader", "VERSION"]
