"""``CRYPTO_ENTRY_PACING_V1``: new crypto entries are paced and kept out of a falling market and
of scheduled US macro releases (docs/TRADING-QUALITY-PLAN.md phase A, item A2; owner approval
2026-10-02, "A1-A5: yes"; docs/packages/risk-pacing.md).

Why: on 2026-10-02 the account took 10 entries in 10 minutes into a -3.5%/h altcoin drop, and
the daily halt then flattened them all at the low.

The version is recorded at admission (``entry_pacing_version``) on every crypto setup admitted by
an engine configured with ``EntryPacing`` (the managed runtime's factory configures it); setups
admitted before it, US setups and engines without it keep today's behaviour exactly. For a setup
of the version, a trigger that reaches ``authorize_entry`` waits, before any broker read and again
under the shared risk lock, while any rule below holds:

- ``ENTRY_RATE_LIMIT``: at least ``MAX_ENTRIES`` approved crypto ENTRY decisions in the rolling
  ``RATE_WINDOW`` (30 minutes) across the account (every crypto setup's decisions count).
- ``MARKET_DROP``: the median 1-hour return of the streamed crypto coins is at most
  ``DROP_THRESHOLD`` (-2%). ``MarketBreadthWindow`` keeps the stream's prices.
- ``MARKET_BREADTH_UNAVAILABLE``: fewer than ``MIN_COINS`` coins have a current price (at most
  ``LATEST_MAX_AGE`` old) and a price from 60 to 70 minutes ago. Fail-safe: the gate cannot tell
  that the market is not falling, so it waits.

Seeding (``refresh``): the managed runtime's execution tick, at most once per
``REFRESH_INTERVAL`` (a start or gap backlog continues tick by tick, ``MAX_READS_PER_TICK`` reads
each), reads completed one-minute bars (the read-only market source's
``window_bars``, the GET route the gap check uses) for each coin the window cannot yet measure:
the streamed coins and, while fewer than ``MIN_COINS`` are streamed, the fixed reference basket
``BASKET``. Each bar's close is a price at its end. So the median is available right after a
start or a stream gap, and the basket keeps it measurable on a quiet stream. A failed read seeds
nothing for that coin (the gate keeps waiting); the stream reader itself only does in-memory work.
- ``MACRO_EVENT_WINDOW``: from 15 minutes before to 45 minutes after a scheduled US CPI or FOMC
  release in the versioned calendar file ``macro_calendar_v1.json``.
- ``MACRO_CALENDAR_EXPIRED``: the New York date is past the file's coverage (fail-safe; the file
  must be extended, see its ``note``), or the engine has no pacing configured at all.

Each block records one ``CRYPTO_ENTRY_PACING_WAIT`` per setup and UTC minute; it is never a
refusal: no decision, reservation or order is written, the setup keeps WATCHING and enters on a
later trigger once every rule clears, within its own validity window. Exits, protection, the
daily halt and admission are unchanged.
"""

import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "CRYPTO_ENTRY_PACING_V1"
STATE_FIELD = "entry_pacing_version"
WAIT_EVENT = "CRYPTO_ENTRY_PACING_WAIT"
WAIT_KEY = "crypto-entry-pacing-wait:"  # + setup and UTC minute: at most one per minute.
CONTEXT_FIELD = "entry_pacing"  # The approved decision's engine-clock time (the rate's clock).

RATE_LIMIT = "ENTRY_RATE_LIMIT"
MARKET_DROP = "MARKET_DROP"
BREADTH_UNAVAILABLE = "MARKET_BREADTH_UNAVAILABLE"
MACRO_WINDOW = "MACRO_EVENT_WINDOW"
CALENDAR_EXPIRED = "MACRO_CALENDAR_EXPIRED"
REASONS = (RATE_LIMIT, MARKET_DROP, BREADTH_UNAVAILABLE, MACRO_WINDOW, CALENDAR_EXPIRED)

MAX_ENTRIES = 2
RATE_WINDOW = timedelta(minutes=30)
DROP_THRESHOLD = Decimal("-0.02")
LOOKBACK = timedelta(minutes=60)
LOOKBACK_TOLERANCE = timedelta(minutes=10)  # The hour-ago price may be up to 70 minutes old.
LATEST_MAX_AGE = timedelta(minutes=5)
MIN_COINS = 3
RETENTION = LOOKBACK + LOOKBACK_TOLERANCE + timedelta(minutes=5)
MACRO_BEFORE = timedelta(minutes=15)
MACRO_AFTER = timedelta(minutes=45)
CALENDAR_FILE = Path(__file__).with_name("macro_calendar_v1.json")
CALENDAR_VERSION = "US_MACRO_CALENDAR_V1"
MACRO_KINDS = ("CPI", "FOMC")
# The seed: once a minute at most; a coin is read when it lacks an hour-ago price or its latest
# price is older than REFRESH_AGE; the basket of the most liquid Alpaca USD pairs.
REFRESH_INTERVAL = timedelta(seconds=60)
REFRESH_AGE = timedelta(minutes=2)
SEED_SPAN = LOOKBACK + LOOKBACK_TOLERANCE  # The first read of a coin: its last 70 minutes.
BASKET = ("BTC/USD", "ETH/USD", "SOL/USD")
# Bar reads per execution tick, so a seed never holds a protection pass for long; a backlog (a
# start or a gap with many coins) continues on the next tick.
MAX_READS_PER_TICK = 3
TRIGGER_FIELDS = ("trade_price", "trade_at", "trade_id", "bid", "ask", "quote_at",
                  "quote_read_at")
_PRECISION = 40


def admission_fields(packet, pacing):
    """The state field admission records: crypto setups of an engine with pacing only."""
    if pacing is None or packet.get("market") != "CRYPTO":
        return {}
    return {STATE_FIELD: VERSION}


def active(state):
    return isinstance(state, dict) and state.get(STATE_FIELD) == VERSION


def decision_fields(state, now):
    """The approved-or-not decision context's pacing record (the setups of the version only):
    the engine-clock time the rate window counts."""
    if not active(state):
        return {}
    return {CONTEXT_FIELD: {"version": VERSION, "decided_at": now.isoformat()}}


def recent_entries(conn, now):
    """Approved crypto ENTRY decisions in the rolling window ending at ``now``. A decision of the
    version is timed by its recorded engine-clock time, any other by its database time."""
    return conn.execute(
        """SELECT count(*) AS n FROM lab.managed_risk_decisions d
        JOIN lab.managed_setups s USING(setup_id)
        WHERE d.action='ENTRY' AND d.outcome='APPROVED' AND s.market='CRYPTO'
        AND coalesce((d.context->'entry_pacing'->>'decided_at')::timestamptz,d.created_at)>%s
        AND coalesce((d.context->'entry_pacing'->>'decided_at')::timestamptz,d.created_at)<=%s""",
        (now - RATE_WINDOW, now),
    ).fetchone()["n"]


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = _PRECISION
        return (ordered[middle - 1] + ordered[middle]) / 2


class MarketBreadthWindow:
    """The streamed crypto coins' prices of the last ``RETENTION``, one per coin and UTC minute
    (its last quote mid or trade print). Thread-safe: the market stream writes, the entry gate
    reads. A gap does not empty it: every price kept is a true price at its time, and the
    freshness rules decide what may be used."""

    def __init__(self):
        self._prices = {}  # symbol -> deque([minute, price, at])
        self._lock = threading.Lock()

    def observe(self, symbol, price, at):
        if not isinstance(price, Decimal):
            price = Decimal(str(price))
        if not price.is_finite() or price <= 0 or at.tzinfo is None:
            return
        at = at.astimezone(UTC)
        minute = at.replace(second=0, microsecond=0)
        with self._lock:
            rows = self._prices.setdefault(symbol, deque())
            if rows and rows[-1][0] == minute:
                if at >= rows[-1][2]:
                    rows[-1][1], rows[-1][2] = price, at
            elif not rows or minute > rows[-1][0]:
                rows.append([minute, price, at])
            while rows and rows[0][2] < at - RETENTION:
                rows.popleft()

    def seed(self, symbol, points):
        """Merge ``(price, at)`` points (completed bars' closes at their ends) with the coin's
        stream prices: per minute the latest price wins, whatever its source."""
        rows = {}
        with self._lock:
            for minute, price, at in self._prices.get(symbol, ()):
                rows[minute] = [minute, price, at]
            for price, at in points:
                price = price if isinstance(price, Decimal) else Decimal(str(price))
                if not price.is_finite() or price <= 0 or at.tzinfo is None:
                    continue
                at = at.astimezone(UTC)
                minute = at.replace(second=0, microsecond=0)
                if minute not in rows or at >= rows[minute][2]:
                    rows[minute] = [minute, price, at]
            if not rows:
                return
            ordered = deque(rows[m] for m in sorted(rows))
            while ordered and ordered[0][2] < ordered[-1][2] - RETENTION:
                ordered.popleft()
            self._prices[symbol] = ordered

    def needs(self, symbol, now):
        """``(needed, start)``: whether the seed should read the coin, from when: all of
        ``SEED_SPAN`` without an hour-ago price, else from its latest price."""
        with self._lock:
            rows = [r for r in self._prices.get(symbol, ()) if r[2] <= now]
        cutoff = now - LOOKBACK
        has_reference = any(cutoff - LOOKBACK_TOLERANCE <= r[2] <= cutoff for r in rows)
        if not has_reference:
            return True, now - SEED_SPAN
        if now - rows[-1][2] > REFRESH_AGE:
            return True, rows[-1][2]
        return False, None

    def returns(self, now):
        """``{symbol: (return, reference_at, latest_at)}`` for each coin with a current price and
        a price from ``LOOKBACK`` (up to ``LOOKBACK + LOOKBACK_TOLERANCE``) before ``now``."""
        result = {}
        with self._lock:
            for symbol, rows in self._prices.items():
                usable = [r for r in rows if r[2] <= now]
                if not usable:
                    continue
                latest = usable[-1]
                if now - latest[2] > LATEST_MAX_AGE:
                    continue
                cutoff = now - LOOKBACK
                reference = next((r for r in reversed(usable) if r[2] <= cutoff), None)
                if reference is None or reference[2] < cutoff - LOOKBACK_TOLERANCE:
                    continue
                with localcontext() as context:
                    context.prec = _PRECISION
                    move = latest[1] / reference[1] - 1
                result[symbol] = (move, reference[2], latest[2])
        return result

    def breadth(self, now):
        """``(median or None, evidence)``: None when fewer than ``MIN_COINS`` coins qualify."""
        moves = self.returns(now)
        value = median([m for m, _, _ in moves.values()]) if len(moves) >= MIN_COINS else None
        return value, {
            "median_1h_return": None if value is None else str(value),
            "coins": len(moves),
            "min_coins": MIN_COINS,
            "returns": {s: str(m) for s, (m, _, _) in sorted(moves.items())},
            "threshold": str(DROP_THRESHOLD),
        }


@dataclass(frozen=True)
class MacroCalendar:
    """The versioned US release calendar: each event an instant (New York time in the file)."""

    version: str
    covers_through: date
    events: tuple  # ((kind, instant, label), ...) sorted by instant.

    @classmethod
    def load(cls, path=CALENDAR_FILE):
        raw = json.loads(Path(path).read_text())
        if raw.get("version") != CALENDAR_VERSION or raw.get("timezone") != "America/New_York":
            raise ValueError("MACRO_CALENDAR_INVALID")
        zone = ZoneInfo(raw["timezone"])
        coverage = raw["coverage"]
        if set(coverage) != set(MACRO_KINDS):
            raise ValueError("MACRO_CALENDAR_INVALID")
        events = []
        for item in raw["events"]:
            if item["kind"] not in MACRO_KINDS:
                raise ValueError("MACRO_CALENDAR_INVALID")
            day = date.fromisoformat(item["date"])
            if not date.fromisoformat(coverage[item["kind"]]["from"]) <= day <= \
                    date.fromisoformat(coverage[item["kind"]]["through"]):
                raise ValueError("MACRO_CALENDAR_INVALID")
            instant = datetime.combine(day, time.fromisoformat(item["time"]), zone)
            events.append((item["kind"], instant.astimezone(UTC), item.get("label", "")))
        through = min(date.fromisoformat(c["through"]) for c in coverage.values())
        return cls(raw["version"], through, tuple(sorted(events, key=lambda e: e[1])))

    def window(self, now):
        """The release whose window (15 minutes before to 45 after) contains ``now``, or None."""
        for kind, instant, label in self.events:
            if instant - MACRO_BEFORE <= now < instant + MACRO_AFTER:
                return {"kind": kind, "release_at": instant.isoformat(), "label": label,
                        "window_from": (instant - MACRO_BEFORE).isoformat(),
                        "window_until": (instant + MACRO_AFTER).isoformat()}
        return None

    def expired(self, now):
        return now.astimezone(ZoneInfo("America/New_York")).date() > self.covers_through


class EntryPacing:
    """The engine's pacing configuration: the market window the runtime feeds and the calendar,
    and the seed's timer and last result (in memory, for the status)."""

    def __init__(self, window=None, calendar=None):
        self.window = window if window is not None else MarketBreadthWindow()
        self.calendar = calendar if calendar is not None else MacroCalendar.load()
        self.next_refresh_at = None  # None: due now (a start or a stream gap).
        self.last_refresh = None

    def refresh_due(self):
        """A start or a stream gap: the next execution tick seeds at once."""
        self.next_refresh_at = None

    def block(self, conn, now):
        """``(reason, detail)`` of the first rule that holds new entries back, or None."""
        return block(self, conn, now)


def block(pacing, conn, now):
    """The gate for a setup of the version; ``pacing`` None (unconfigured) waits, fail-safe."""
    if pacing is None:
        return CALENDAR_EXPIRED, {"configured": False}
    if pacing.calendar.expired(now):
        return CALENDAR_EXPIRED, {"calendar": pacing.calendar.version,
                                  "covers_through": pacing.calendar.covers_through.isoformat()}
    release = pacing.calendar.window(now)
    if release is not None:
        return MACRO_WINDOW, {"calendar": pacing.calendar.version, **release}
    entries = recent_entries(conn, now)
    if entries >= MAX_ENTRIES:
        return RATE_LIMIT, {"entries": entries, "max_entries": MAX_ENTRIES,
                            "window_seconds": int(RATE_WINDOW.total_seconds())}
    value, evidence = pacing.window.breadth(now)
    if value is None:
        return BREADTH_UNAVAILABLE, evidence
    if value <= DROP_THRESHOLD:
        return MARKET_DROP, evidence
    return None


def seed_symbols(streamed):
    """The coins the seed covers: the streamed ones, plus the basket while fewer than
    ``MIN_COINS`` are streamed."""
    symbols = set(streamed)
    if len(symbols) < MIN_COINS:
        symbols |= set(BASKET)
    return sorted(symbols)


def refresh(pacing, read_bars, streamed, now):
    """Seed the window from completed one-minute bars, at most once per ``REFRESH_INTERVAL``.

    ``read_bars(symbol, start, end)`` returns ``(bars, issues)`` (bars with ``end_at`` and
    ``close``; any failure is issues, never an exception). Returns the refresh record, or None
    when not due. A coin whose read fails or returns no bar is left as it was: the gate waits
    for it as before (fail-safe).
    """
    if pacing.next_refresh_at is not None and now < pacing.next_refresh_at:
        return None
    pacing.next_refresh_at = now + REFRESH_INTERVAL
    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    seeded, failed, read, pending = [], {}, 0, []
    for symbol in seed_symbols(streamed):
        needed, start = pacing.window.needs(symbol, now)
        if not needed:
            continue
        start = start.astimezone(UTC).replace(second=0, microsecond=0)
        if start >= end:
            continue
        if read >= MAX_READS_PER_TICK:
            pending.append(symbol)
            continue
        read += 1
        try:
            bars, issues = read_bars(symbol, start, end)
        except Exception as exc:  # A reader must not raise; fail-safe if it does.
            bars, issues = (), (type(exc).__name__.upper(),)
        points = [(bar.close, bar.end_at) for bar in bars
                  if getattr(bar, "completed", True) and bar.end_at <= now]
        if points:
            pacing.window.seed(symbol, points)
            seeded.append(symbol)
        else:
            failed[symbol] = [str(i) for i in issues] or ["NO_BARS"]
    if pending:
        pacing.next_refresh_at = None  # The backlog continues on the next tick.
    pacing.last_refresh = {"at": now.isoformat(), "symbols": seed_symbols(streamed),
                           "reads": read, "seeded": seeded, "failed": failed,
                           "pending": pending}
    return pacing.last_refresh


def wait_key(setup_id, now):
    minute = now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    return f"{WAIT_KEY}{setup_id}:{minute}"


def wait_body(reason, detail, observation, now):
    return {
        "reason": reason,
        "version": VERSION,
        "detail": detail,
        "trigger": {k: observation.get(k) for k in TRIGGER_FIELDS if k in (observation or {})},
        "waited_at": now.isoformat(),
    }
