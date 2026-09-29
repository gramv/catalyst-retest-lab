import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class MarketDataError(Exception):
    """Only sanitized codes cross the provider boundary."""


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value):
        raise MarketDataError("INVALID_SYMBOL")
    return value


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        raise MarketDataError("INVALID_TIMESTAMP") from None


def timestamp_ns(value: str) -> int:
    parsed = timestamp(value)
    match = re.search(r"\.(\d+)(?:Z|[+-]\d\d:\d\d)$", value)
    fraction = (match[1] if match else "").ljust(9, "0")[:9]
    return int(parsed.replace(microsecond=0).timestamp()) * 1_000_000_000 + int(fraction)


def decimal(value, *, positive=True):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or (result <= 0 if positive else result < 0):
            raise ValueError
        return result
    except Exception:
        raise MarketDataError("INVALID_MARKET_NUMBER") from None


@dataclass(frozen=True)
class Session:
    session_date: date
    opens: datetime
    closes: datetime

    @classmethod
    def from_calendar(cls, row):
        try:
            day = date.fromisoformat(row["date"])
            opens = datetime.fromisoformat(f"{day}T{row['open']}").replace(tzinfo=NY)
            closes = datetime.fromisoformat(f"{day}T{row['close']}").replace(tzinfo=NY)
            if opens >= closes:
                raise ValueError
            return cls(day, opens, closes)
        except (KeyError, TypeError, ValueError):
            raise MarketDataError("INVALID_CALENDAR") from None

    @property
    def flatten_time(self):
        return self.closes - timedelta(minutes=5)

    def contains(self, now):
        return self.opens <= now < self.closes


@dataclass(frozen=True)
class Observation:
    kind: str
    ticker: str
    provider_timestamp: str
    data_feed: str
    price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: Decimal | None = None
    trade_id: str | None = None
    data_provider: str = "ALPACA"
    high: Decimal | None = None
    low: Decimal | None = None

    @property
    def at(self):
        return timestamp(self.provider_timestamp)

    @property
    def ns(self):
        return timestamp_ns(self.provider_timestamp)

    @property
    def spread_bps(self):
        if self.bid is None or self.ask is None or self.ask < self.bid:
            return None
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 10000

    def to_json(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, row):
        return cls(
            **{
                k: Decimal(v)
                if k in {"price", "bid", "ask", "volume", "high", "low"} and v is not None
                else v
                for k, v in row.items()
            }
        )

    @property
    def key(self):
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_wire(cls, row, feed):
        try:
            name = symbol(row["S"])
            stamp = row["t"]
            timestamp_ns(stamp)
            kind = row["T"]
            if kind == "q":
                return cls(
                    "quote", name, stamp, feed, bid=decimal(row["bp"]), ask=decimal(row["ap"])
                )
            if kind == "t":
                return cls(
                    "trade",
                    name,
                    stamp,
                    feed,
                    price=decimal(row["p"]),
                    volume=decimal(row["s"], positive=False),
                    trade_id=str(row["i"]),
                )
            if kind in {"b", "u"}:
                return cls(
                    "bar" if kind == "b" else "bar_update",
                    name,
                    stamp,
                    feed,
                    price=decimal(row["c"]),
                    volume=decimal(row["v"], positive=False),
                    high=decimal(row["h"]) if "h" in row else None,
                    low=decimal(row["l"]) if "l" in row else None,
                )
            raise MarketDataError("UNSUPPORTED_MARKET_MESSAGE")
        except (KeyError, TypeError):
            raise MarketDataError("MALFORMED_MARKET_MESSAGE") from None
