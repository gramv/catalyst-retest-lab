"""``SYSTEM_CHECK_V1``: the system's own check of a selected report-V3 pick at admission.

Plan ``docs/CRYPTO-AGENT-LOOP.md`` sections 4.3 and 4.4 (owner-approved 2026-09-26) and the
owner decisions relayed for package system-check (plan phase 3a, 2026-09-27):

* The check runs after Jev's selection, independently of the agent and of Jev, on every
  ``AGENT_RESEARCH_REPORT_V3`` selection (a packet whose ``report_schema_version`` says so),
  after every existing admission check and the broker price grid and before the setup row
  that spends the symbol slot. V1, V2, B1, B2 and operator engineering packets never reach it.
* Checks, evaluated in this order; the first failure names a permanent refusal:
  ``STOP_DISTANCE_BELOW_MINIMUM`` ((max entry - stop) / max entry below 2%; levels only, so
  no live price is read for it), then, on the live price, ``PRICE_MISMATCH`` (live mid more
  than 5% from the agent's current price), ``STOP_ALREADY_HIT`` (live bid at or below the
  stop, or the last trade at or below it) and ``BREAKOUT_NOT_ENABLED`` (the entry type is
  BREAKOUT). Every check that could be evaluated is recorded with its result.
* Entry type from the live mid: PULLBACK when the entry trigger is below the mid by more than
  0.2% of the mid, BREAKOUT when above it by more than 0.2%, IMMEDIATE otherwise (within
  plus or minus 0.2%, both bounds included). Breakouts are tracked, not traded, until a
  backtest earns them: the current trigger (a print at or below the entry) would buy one at
  once.
* Live price: bid, ask and last from the runtime's stream observation when its quote is at
  most 5 seconds old by the quote's own timestamp; otherwise one REST latest-quote read
  through the runtime's market source, fresh by its read time (plan 4.4: a quote that has not
  changed is still current), its own timestamp recorded. The last trade is the stream's, when
  there is one. Nothing usable is ``LIVE_PRICE_UNAVAILABLE``, a transient refusal the next
  tick retries.

Decisions compare exact Decimal products under an 80-digit context (never a rounded ratio);
the recorded fractions are rounded to 12 decimal places for reading only.

``RESEARCH_RUN_SUPERSESSION_V1`` (plan 4.4, "a setup watches its trigger until the next run's
shortlist goes live"): once a V3 selection of a newer ``run_slot`` is published, every older
V3 setup still WATCHING is revoked and every older, unexpired, unadmitted V3 selection is
declined, both with ``SUPERSEDED_BY_NEW_RESEARCH``. Positions, working entries and V2 cycles
are never touched.

``RESEARCH_RUN_SUPERSESSION_V2`` (package research-loop-app, docs/RESEARCH-LOOP-V2.md 3.2)
applies instead while the configured schedule is ``RESEARCH_SCHEDULE_V2``: a V3 pick of run
``r`` is superseded only by a published V3 selection of a later run that is a full run (any
symbol: the daily run replaces the day's set) or is for the same symbol (any run: an adjusted
pick replaces that coin's setup). Run kinds come from the configured schedule. Under V1 the V1
rule applies unchanged. The SQL helpers below read the ledger; they never write.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from catalyst_lab import strategies
from catalyst_lab.broker_budget import RESEARCH, request_priority
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from catalyst_lab.research_schedule import FULL_RUN, SCHEDULE_VERSION_V2
from catalyst_lab.strategies import core

D = Decimal

SYSTEM_CHECK_VERSION = "SYSTEM_CHECK_V1"
SUPERSESSION_VERSION = "RESEARCH_RUN_SUPERSESSION_V1"
SUPERSESSION_VERSION_V2 = "RESEARCH_RUN_SUPERSESSION_V2"

# Owner thresholds (named constants, Decimal only).
PRICE_MISMATCH_FRACTION = D("0.05")
ENTRY_TYPE_BAND_FRACTION = D("0.002")
MINIMUM_CRYPTO_STOP_FRACTION = D("0.02")
# A stream quote this old or younger is live; a REST read is live this long after it was read.
LIVE_PRICE_MAX_AGE_SECONDS = 5
# Admission never waits on more than this many REST quote reads in one protection tick.
REST_QUOTE_READS_PER_TICK = 1
RATIO_QUANTUM = D("1e-12")  # Recorded fractions only; decisions compare exact products.
_PRECISION = 80

PULLBACK, IMMEDIATE, BREAKOUT = core.PULLBACK, core.IMMEDIATE, core.BREAKOUT
# PULLBACK_V1's entry types (STRATEGY_REGISTRY_V1): breakouts after a backtest earns them. The
# check reads the packet's strategy (``strategies.traded_entry_types``); every pick without a
# ``strategy_id`` is PULLBACK_V1, so this set is the one applied today.
TRADED_ENTRY_TYPES = strategies.PULLBACK_V1.entry_types

LIVE_PRICE_UNAVAILABLE = "LIVE_PRICE_UNAVAILABLE"  # Transient: the next tick retries.
STOP_DISTANCE_BELOW_MINIMUM = "STOP_DISTANCE_BELOW_MINIMUM"
PRICE_MISMATCH = "PRICE_MISMATCH"
STOP_ALREADY_HIT = "STOP_ALREADY_HIT"
BREAKOUT_NOT_ENABLED = "BREAKOUT_NOT_ENABLED"
SUPERSEDED_BY_NEW_RESEARCH = "SUPERSEDED_BY_NEW_RESEARCH"
# AGENT_RESEARCH_WITHDRAWAL_V1 (research_withdrawal.py): the proposing agent withdrew the pick.
WITHDRAWN_BY_RESEARCH = "WITHDRAWN_BY_RESEARCH"
# A selected pick refused with one of these is to be replaced by Jev's next-ranked pick (a
# later package); SUPERSEDED_BY_NEW_RESEARCH is permanent too, but its run is over, and a
# withdrawn pick is replaced by the agent's own newer research, never by the ranking.
# CRYPTO_TRADE_PLAN_V1 (trade_plan.py): a pick whose planned levels cannot stand is refused
# like a system-check failure (recorded once, final for the receipt, replaced under top-K).
TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY = "TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY"
TRADE_PLAN_STOP_NOT_POSITIVE = "TRADE_PLAN_STOP_NOT_POSITIVE"
SYSTEM_CHECK_REFUSALS = frozenset(
    {STOP_DISTANCE_BELOW_MINIMUM, PRICE_MISMATCH, STOP_ALREADY_HIT, BREAKOUT_NOT_ENABLED,
     TRADE_PLAN_TARGET_NOT_ABOVE_MAX_ENTRY, TRADE_PLAN_STOP_NOT_POSITIVE}
)
PERMANENT_REFUSALS = SYSTEM_CHECK_REFUSALS | {SUPERSEDED_BY_NEW_RESEARCH, WITHDRAWN_BY_RESEARCH}
# Declines that never get a replacement decision (TOPK_REPLACEMENT_V1 or _V2).
NOT_REPLACED = frozenset({SUPERSEDED_BY_NEW_RESEARCH, WITHDRAWN_BY_RESEARCH})

# (recorded check name, refusal code), in evaluation order.
CHECKS = (
    ("stop_distance", STOP_DISTANCE_BELOW_MINIMUM),
    ("price_match", PRICE_MISMATCH),
    ("stop_not_hit", STOP_ALREADY_HIT),
    ("entry_type_traded", BREAKOUT_NOT_ENABLED),
)
PASS, FAIL, NOT_EVALUATED = "PASS", "FAIL", "NOT_EVALUATED"
PASSED, REFUSED, RETRY = "PASSED", "REFUSED", "RETRY"

# The final refusal of one selection receipt (like CRYPTO_ADMISSION_REFUSED for the grid).
REFUSED_EVENT = "SYSTEM_CHECK_REFUSED"
REFUSED_KEY = "system-check-refused:"
# A selection's one RESEARCH_ADMISSION_DECLINED is keyed by this plus its selection event_seq
# (the runtime's declines, and AGENT_RESEARCH_WITHDRAWAL_V1's).
ADMISSION_DECLINED_KEY = "research:admission-declined:"

STREAM_SOURCE = "ALPACA_STREAM"
REST_SOURCE = "ALPACA_REST_LATEST_QUOTE"
_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


class AdmissionRefused(ValueError):
    """An admission refusal code carrying evidence for the refusal and decline bodies.

    ``str(exc)`` is the code alone, so ``failure_code`` and every caller that compares
    messages see exactly the code. ``details`` holds top-level body fields (codes, prices,
    times; never free text).
    """

    def __init__(self, code, details=None):
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


class LivePriceUnavailable(Exception):
    """No live bid and ask for this attempt; ``attempts`` says what was tried."""

    def __init__(self, attempts):
        super().__init__(LIVE_PRICE_UNAVAILABLE)
        self.attempts = list(attempts)


def is_v3_packet(packet):
    return isinstance(packet, dict) and packet.get("report_schema_version") == REPORT_SCHEMA_V3


def v3_facts(packet):
    """The agent's current price and the run slot of a V3 packet, validated.

    A V3 packet without them is not an app-reviewed V3 packet: APP_REVIEWED_PACKET_REQUIRED,
    the same permanent code ``ManagedExecution.admit`` uses for missing packet fields.
    """
    try:
        state = packet["state"]
        agent = D(str(state["agent_current_price"]))
        slot = datetime.fromisoformat(packet["run_slot"])
        if (
            packet["market"] != "CRYPTO"
            or not agent.is_finite()
            or agent <= 0
            or slot.tzinfo is None
            or not isinstance(state["agent_price_at"], str)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("APP_REVIEWED_PACKET_REQUIRED") from None
    return agent, slot


def run_slot_of(record):
    return datetime.fromisoformat(record["run_slot"])


def _code(exc):
    text = str(exc)
    return text if len(text) <= 80 and _CODE.fullmatch(text) else type(exc).__name__


def _ratio(numerator, denominator):
    with localcontext() as context:
        context.prec = _PRECISION
        return (numerator / denominator).quantize(RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def _valid_quote(bid, ask):
    return all(isinstance(v, D) and v.is_finite() for v in (bid, ask)) and 0 < bid <= ask


@dataclass(frozen=True)
class LiveQuote:
    """The live price one check used: the quote, where and when it was read, the last trade."""

    symbol: str
    bid: D
    ask: D
    quote_at: datetime
    source: str
    read_at: datetime
    last: D | None = None
    last_at: datetime | None = None
    last_trade_id: str | None = None

    @property
    def mid(self):
        with localcontext() as context:
            context.prec = _PRECISION
            return (self.bid + self.ask) / 2

    def evidence(self):
        return {
            "quote_source": self.source,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "mid": str(self.mid),
            "quote_at": self.quote_at.isoformat(),
            "read_at": self.read_at.isoformat(),
            "quote_age_seconds": str(D(str((self.read_at - self.quote_at).total_seconds()))),
            "last": str(self.last) if self.last is not None else None,
            "last_at": self.last_at.isoformat() if self.last_at is not None else None,
            "last_trade_id": self.last_trade_id,
            "last_source": STREAM_SOURCE if self.last is not None else None,
        }


def entry_type(entry, mid):
    """PULLBACK, IMMEDIATE or BREAKOUT for an entry trigger against the live mid."""
    with localcontext() as context:
        context.prec = _PRECISION
        offset, band = entry - mid, ENTRY_TYPE_BAND_FRACTION * mid
        if offset < -band:
            return PULLBACK
        if offset > band:
            return BREAKOUT
        return IMMEDIATE


def stop_distance_ok(max_entry, stop):
    """(max entry - stop) / max entry is at least MINIMUM_CRYPTO_STOP_FRACTION."""
    with localcontext() as context:
        context.prec = _PRECISION
        return max_entry - stop >= MINIMUM_CRYPTO_STOP_FRACTION * max_entry


def price_matches(mid, agent_price):
    """|mid - agent price| / agent price is at most PRICE_MISMATCH_FRACTION."""
    with localcontext() as context:
        context.prec = _PRECISION
        return abs(mid - agent_price) <= PRICE_MISMATCH_FRACTION * agent_price


def evaluate(packet, quote, *, now, attempts=None):
    """The ``SYSTEM_CHECK_V1`` evidence for ``packet``, with ``result`` and ``code``.

    ``quote`` None evaluates the levels only: the stop-distance refusal, else
    LIVE_PRICE_UNAVAILABLE (``result: RETRY``) with ``attempts``. Admission calls it first
    without a quote, so a pick the levels already refuse never costs a live-price read.
    """
    agent, _ = v3_facts(packet)
    levels = packet["levels"]
    entry, max_entry, stop = (
        D(str(levels[k])) for k in ("entry_trigger", "max_entry_price", "stop")
    )
    results = dict.fromkeys((name for name, _ in CHECKS), NOT_EVALUATED)
    results["stop_distance"] = PASS if stop_distance_ok(max_entry, stop) else FAIL
    evidence = {
        "version": SYSTEM_CHECK_VERSION,
        "checked_at": now.isoformat(),
        "levels": {k: str(levels[k]) for k in (
            "entry_trigger", "max_entry_price", "stop", "target")},
        "agent_current_price": str(packet["state"]["agent_current_price"]),
        "agent_price_at": packet["state"]["agent_price_at"],
        "stop_distance_fraction": str(_ratio(max_entry - stop, max_entry)),
        "minimum_stop_fraction": str(MINIMUM_CRYPTO_STOP_FRACTION),
        "price_mismatch_fraction": str(PRICE_MISMATCH_FRACTION),
        "entry_type_band_fraction": str(ENTRY_TYPE_BAND_FRACTION),
        "live": None,
        "price_deviation_fraction": None,
        "entry_offset_fraction": None,
        "entry_type": None,
    }
    if quote is not None and results["stop_distance"] == PASS:
        mid = quote.mid
        kind = entry_type(entry, mid)
        results["price_match"] = PASS if price_matches(mid, agent) else FAIL
        results["stop_not_hit"] = FAIL if quote.bid <= stop or (
            quote.last is not None and quote.last <= stop
        ) else PASS
        traded = strategies.traded_entry_types(packet)
        results["entry_type_traded"] = PASS if kind in traded else FAIL
        evidence.update(
            live=quote.evidence(),
            price_deviation_fraction=str(_ratio(abs(mid - agent), agent)),
            entry_offset_fraction=str(_ratio(entry - mid, mid)),
            entry_type=kind,
        )
    code = next((code for name, code in CHECKS if results[name] == FAIL), None)
    if code is not None:
        result = REFUSED
    elif quote is None:
        result, code = RETRY, LIVE_PRICE_UNAVAILABLE
        evidence["live_price_attempts"] = list(attempts or [])
    else:
        result = PASSED
    return {**evidence, "checks": results, "result": result, "code": code}


def _parsed_stream_quote(observation):
    """``((bid, ask, quote_at), None)`` for a well-formed stream quote, else ``(None, code)``."""
    if not observation or not all(observation.get(k) for k in ("bid", "ask", "quote_at")):
        return None, "STREAM_QUOTE_MISSING"
    try:
        bid, ask = D(str(observation["bid"])), D(str(observation["ask"]))
        quote_at = datetime.fromisoformat(observation["quote_at"])
        if quote_at.tzinfo is None or not _valid_quote(bid, ask):
            raise ValueError
    except (TypeError, ValueError, ArithmeticError):
        return None, "STREAM_QUOTE_INVALID"
    return (bid, ask, quote_at), None


def _stream_quote(observation, now, max_age):
    """``((bid, ask, quote_at), None)`` for a live stream quote, else ``(None, code)``."""
    quote, code = _parsed_stream_quote(observation)
    if quote is None:
        return None, code
    if not 0 <= (now - quote[2]).total_seconds() <= max_age:
        return None, "STREAM_QUOTE_STALE"
    return quote, None


def _received_stream_quote(observation, received_at, now, max_age):
    """Like ``_stream_quote``, but live by the runtime's receipt time (CRYPTO_ALPACA_TRIGGER_V1):
    a quote received at most ``max_age`` seconds ago is live whatever its own timestamp, which
    can never be later than its receipt."""
    quote, code = _parsed_stream_quote(observation)
    if quote is None:
        return None, code
    if not isinstance(received_at, datetime) or received_at.tzinfo is None:
        return None, "STREAM_QUOTE_MISSING"
    if quote[2] > received_at:
        return None, "STREAM_QUOTE_INVALID"
    if not 0 <= (now - received_at).total_seconds() <= max_age:
        return None, "STREAM_QUOTE_STALE"
    return quote, None


def _stream_last(observation):
    """``(price, at, trade_id)`` of the stream's last trade, or three Nones."""
    try:
        price = D(str(observation["trade_price"]))
        at = datetime.fromisoformat(observation["trade_at"])
        if not price.is_finite() or price <= 0 or at.tzinfo is None:
            raise ValueError
        trade_id = observation.get("trade_id")
        return price, at, str(trade_id) if trade_id is not None else None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None, None, None


@dataclass(frozen=True)
class _RestRead:
    attempted_at: datetime
    quote: tuple | None
    code: str | None


class LivePriceReader:
    """Bid, ask and last for the V3 system check: the stream first, else one REST read.

    The runtime owns one reader and calls ``new_tick`` once per protection tick. The
    account request budget governs only the trading host, so this market-data read is
    declared in the research class and bounded here: at most ``REST_QUOTE_READS_PER_TICK``
    REST reads per protection tick and at most one per symbol per
    ``LIVE_PRICE_MAX_AGE_SECONDS`` (a success is reused for that long, a failure waits that
    long; the stream is still consulted on every tick).

    ``CRYPTO_ALPACA_TRIGGER_V1`` (package crypto-trigger) reads through the same reader with
    ``by_read_time``: the stream quote is live when the runtime received it at most
    ``LIVE_PRICE_MAX_AGE_SECONDS`` ago, whatever its own timestamp, and the quote's ``read_at``
    is that receipt time. Trigger and admission reads share both limits.
    """

    def __init__(self, source, *, clock, max_age_seconds=LIVE_PRICE_MAX_AGE_SECONDS,
                 rest_reads_per_tick=REST_QUOTE_READS_PER_TICK):
        self.source, self.clock = source, clock
        self.max_age_seconds = max_age_seconds
        self.rest_reads_per_tick = rest_reads_per_tick
        self._reads_left = rest_reads_per_tick
        self._rest = {}
        self._attempted = {}  # symbol -> the last REST attempt, kept past the reuse window.
        self.rest_reads = 0

    def rest_attempted_at(self, symbol):
        """When the last REST read of ``symbol`` completed (or failed), or None."""
        return self._attempted.get(symbol)

    def new_tick(self):
        self._reads_left = self.rest_reads_per_tick
        now = self.clock()
        self._rest = {s: r for s, r in self._rest.items() if self._recent(r, now)}

    def spend_read(self):
        """Take one of this tick's REST reads for another market-data GET through the same
        source (``CRYPTO_GAP_RESUME_V1``'s bar check, package gap-resume), so the runtime never
        makes more than ``rest_reads_per_tick`` market-data REST reads in one protection tick.
        False when none is left."""
        if self._reads_left <= 0:
            return False
        self._reads_left -= 1
        return True

    def _recent(self, read, now):
        return 0 <= (now - read.attempted_at).total_seconds() < self.max_age_seconds

    def read(self, symbol, observation, *, by_read_time=False, received_at=None):
        """``observation`` is a copy of the runtime's stream row for ``symbol`` (or None).

        ``by_read_time`` (the crypto trigger version): ``received_at`` is when the runtime
        received the row's quote on the stream; the stream quote is live when that was at most
        ``max_age_seconds`` ago and its ``read_at`` is that receipt time. Otherwise the stream
        quote is live by its own timestamp (SYSTEM_CHECK_V1) and read now.
        """
        now = self.clock()
        last = _stream_last(observation or {})
        if by_read_time:
            quote, code = _received_stream_quote(observation, received_at, now,
                                                 self.max_age_seconds)
            if quote is not None:
                return LiveQuote(symbol, *quote, STREAM_SOURCE, received_at, *last)
        else:
            quote, code = _stream_quote(observation, now, self.max_age_seconds)
            if quote is not None:
                return LiveQuote(symbol, *quote, STREAM_SOURCE, now, *last)
        attempts = [{
            "source": STREAM_SOURCE, "code": code,
            "quote_at": (observation or {}).get("quote_at"),
        }]
        if by_read_time:
            attempts[0]["received_at"] = (
                received_at.isoformat() if isinstance(received_at, datetime) else None)
        read = self._rest.get(symbol)
        if read is None or not self._recent(read, now):
            if self._reads_left <= 0:
                attempts.append({"source": REST_SOURCE, "code": "REST_READ_LIMIT_THIS_TICK"})
                raise LivePriceUnavailable(attempts)
            self._reads_left -= 1
            read = self._rest[symbol] = self._rest_read(symbol)
        elif read.quote is None:
            attempts.append({"source": REST_SOURCE, "code": "REST_RETRY_WAIT",
                             "last_code": read.code,
                             "attempted_at": read.attempted_at.isoformat()})
            raise LivePriceUnavailable(attempts)
        if read.quote is None:
            attempts.append({"source": REST_SOURCE, "code": read.code,
                             "attempted_at": read.attempted_at.isoformat()})
            raise LivePriceUnavailable(attempts)
        return LiveQuote(symbol, *read.quote, REST_SOURCE, read.attempted_at, *last)

    def _rest_read(self, symbol):
        """One latest-quote request for ``symbol`` through the market source; never raises."""
        self.rest_reads += 1
        quote, code = None, None
        try:
            with request_priority(RESEARCH):
                quotes, issues = self.source._latest("CRYPTO", (symbol,), "quotes")
            read_at = self.clock()
            snapshot = quotes.get(symbol)
            if snapshot is None:
                code = next((i.code for i in issues if i.code), "REST_QUOTE_MISSING")
            elif not _valid_quote(snapshot.bid, snapshot.ask) or snapshot.timestamp.tzinfo is None:
                code = "REST_QUOTE_INVALID"
            elif snapshot.timestamp > read_at:
                code = "REST_QUOTE_IN_FUTURE"
            else:
                quote = (snapshot.bid, snapshot.ask, snapshot.timestamp)
        except Exception as exc:
            read_at, code = self.clock(), _code(exc)
        self._attempted[symbol] = read_at
        return _RestRead(read_at, quote, code)


# --- Run supersession ---------------------------------------------------------------------------

def supersession_version(schedule):
    """The run-supersession version the configured schedule selects: V2 while it is
    ``RESEARCH_SCHEDULE_V2``, else V1 (also when no schedule is configured)."""
    if getattr(schedule, "version", None) == SCHEDULE_VERSION_V2:
        return SUPERSESSION_VERSION_V2
    return SUPERSESSION_VERSION


def superseding_run_slot(schedule, run_slot, symbol, newer):
    """``RESEARCH_RUN_SUPERSESSION_V2``: the latest run slot among ``newer`` (``(run_slot,
    symbol)`` of published V3 selections) that supersedes a V3 pick of ``symbol`` answering
    ``run_slot``: a later full run with any symbol, or a later run of either kind with the same
    symbol. None when nothing supersedes it. Pure; run kinds are ``schedule.run_kind``."""
    found = [slot for slot, other in newer
             if slot > run_slot and (other == symbol or schedule.run_kind(slot) == FULL_RUN)]
    return max(found, default=None)


# --- Ledger reads (never writes) ----------------------------------------------------------------

def newest_v3_run_slot(conn):
    """The newest ``run_slot`` of any published V3 selection (whatever became of it), or None.
    A promoted strategy's signal (``STRATEGY_PAPER_PATH_V1``) is not a research run: never read."""
    return conn.execute(
        """SELECT max((body->'packet'->>'run_slot')::timestamptz) AS newest
        FROM lab.managed_events WHERE setup_id IS NULL AND kind='RESEARCH_SELECTED'
        AND body->'packet'->>'report_schema_version'=%s
        AND body->'packet'->>'selection_policy' IS DISTINCT FROM %s""",
        (REPORT_SCHEMA_V3, strategies.SIGNAL_SELECTION_POLICY),
    ).fetchone()["newest"]


def newer_v3_selections(conn, after):
    """``(run_slot, symbol)`` of every published V3 selection (whatever became of it) whose
    ``run_slot`` is later than ``after``: what ``superseding_run_slot`` reads."""
    rows = conn.execute(
        """SELECT DISTINCT (body->'packet'->>'run_slot')::timestamptz AS run_slot,
        body->'packet'->>'symbol' AS symbol
        FROM lab.managed_events WHERE setup_id IS NULL AND kind='RESEARCH_SELECTED'
        AND body->'packet'->>'report_schema_version'=%s
        AND body->'packet'->>'selection_policy' IS DISTINCT FROM %s
        AND (body->'packet'->>'run_slot')::timestamptz>%s""",
        (REPORT_SCHEMA_V3, strategies.SIGNAL_SELECTION_POLICY, after),
    ).fetchall()
    return [(row["run_slot"], row["symbol"]) for row in rows]
