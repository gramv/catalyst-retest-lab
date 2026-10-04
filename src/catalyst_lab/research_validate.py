"""``RESEARCH_REPORT_VALIDATE_V1``: a dry run of report-V3 intake (package agent-api).

Owner direction of 2026-09-29 evening: the AI loop is the main experiment, and a research agent
that does not run the kit (``research_agent/``) calls the HTTP API directly. Such an agent had
no way to learn, before sending, whether intake would accept a pick, and the level and grid
rules refused a pick only after Jev had selected it, which wasted a selection slot and Jev
spend. ``POST /api/v1/lab/research-reports/validate`` answers both, and writes nothing:

* **The verdict** is report-V3 intake's own (``ResearchIntake.validate_report``: the same
  parsing, clock and byte-budget checks through the same methods, with no event, cycle,
  packet, idempotency record or Jev call). A refusal is exactly intake's refusal (the HTTP
  layer answers it as the report route does). Otherwise the 200 body carries intake's
  ``item_results`` byte for byte, with its counts and the cycle ID the report would get.
* **Admission warnings**, per pick: the admission checks that would refuse the pick after
  Jev's selection and can be evaluated now, against the app's current state and a fresh
  price, in admission's order (the first listed is the code admission would name):

  ============== =============================== ===========================================
  check          admission code                  rule
  ============== =============================== ===========================================
  LEVELS         INVALID_OR_EXPIRED_SETUP        0 < stop < entry_trigger <= max_entry_price
                                                 < target, and reward-to-risk at max entry
                                                 (target - max) / (max - stop) of at least 2
  ACTIVE_SYMBOL  ACTIVE_SYMBOL_ALREADY_MANAGED   another setup of the coin is active (any
                                                 agent's), unless it is a report-V3 setup
                                                 still WATCHING that this pick's run
                                                 supersedes: the runtime retires that one
                                                 before it admits the pick
  PRICE_GRID     CRYPTO_LEVEL_OFF_PRICE_GRID     a level off the coin's price increment
  STOP_DISTANCE  STOP_DISTANCE_BELOW_MINIMUM     SYSTEM_CHECK_V1 (``system_check.evaluate``,
  PRICE_MATCH    PRICE_MISMATCH                  called exactly as admission calls it, on a
  STOP_NOT_HIT   STOP_ALREADY_HIT                live quote)
  ENTRY_TYPE     BREAKOUT_NOT_ENABLED
  ============== =============================== ===========================================

  The price increment and the live quote and last trade come from the research context's own
  caches (the Alpaca asset list, at most an hour old; latest quotes and trades, under 5
  seconds). A check that cannot be evaluated is listed under ``not_evaluated`` with its
  reason, never passed. Warnings are information: intake, selection and admission behave
  exactly as before.

The route is rate limited per credential: one validation at a time, and at most
``VALIDATIONS_PER_MINUTE`` in any rolling minute (429 with ``Retry-After``). Nothing here
authorizes, sizes or places a trade.
"""

import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab.crypto_execution import CryptoExecutionError, off_grid_levels
from catalyst_lab.managed_store import TERMINAL
from catalyst_lab.research_context import QUOTE_SOURCE
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3, ResearchCapabilityUnavailable
from catalyst_lab.system_check import (
    FAIL,
    SUPERSESSION_VERSION_V2,
    LiveQuote,
    evaluate,
    superseding_run_slot,
    supersession_version,
)

D = Decimal
VALIDATE_VERSION = "RESEARCH_REPORT_VALIDATE_V1"
VALIDATED = "RESEARCH_REPORT_VALIDATED"
NOT_CONFIGURED = "RESEARCH_REPORT_VALIDATE_NOT_CONFIGURED"
RATE_LIMITED = "RESEARCH_REPORT_VALIDATE_RATE_LIMITED"
VALIDATIONS_PER_MINUTE = 12
RATE_WINDOW_SECONDS = 60
MINIMUM_REWARD_RISK = D(2)
RATIO_PLACES = D("0.0001")

# (check, the admission code that refuses a pick failing it), in admission's order.
CHECKS = (
    ("LEVELS", "INVALID_OR_EXPIRED_SETUP"),
    ("ACTIVE_SYMBOL", "ACTIVE_SYMBOL_ALREADY_MANAGED"),
    ("PRICE_GRID", "CRYPTO_LEVEL_OFF_PRICE_GRID"),
    ("STOP_DISTANCE", "STOP_DISTANCE_BELOW_MINIMUM"),
    ("PRICE_MATCH", "PRICE_MISMATCH"),
    ("STOP_NOT_HIT", "STOP_ALREADY_HIT"),
    ("ENTRY_TYPE", "BREAKOUT_NOT_ENABLED"),
)
CODES = dict(CHECKS)
# SYSTEM_CHECK_V1's recorded check names -> this route's.
SYSTEM_CHECKS = {"stop_distance": "STOP_DISTANCE", "price_match": "PRICE_MATCH",
                 "stop_not_hit": "STOP_NOT_HIT", "entry_type_traded": "ENTRY_TYPE"}
LIVE_CHECKS = ("PRICE_MATCH", "STOP_NOT_HIT", "ENTRY_TYPE")
# Reasons a check was not evaluated.
PICK_SCHEMA_INVALID = "PICK_SCHEMA_INVALID"
SYMBOL_NOT_IN_UNIVERSE = "SYMBOL_NOT_IN_UNIVERSE"
PRICE_INCREMENT_UNAVAILABLE = "PRICE_INCREMENT_UNAVAILABLE"
LIVE_PRICE_UNAVAILABLE = "LIVE_PRICE_UNAVAILABLE"
STOP_DISTANCE_FIRST = "STOP_DISTANCE_FIRST"  # SYSTEM_CHECK_V1 reads no price for a short stop.

ACTIVE_CRYPTO_SETUPS = """SELECT s.symbol,t.body->>'state' AS state,
    s.record_json->>'report_schema_version' AS report_schema_version,
    s.record_json->>'run_slot' AS run_slot
    FROM lab.managed_setups s LEFT JOIN lab.managed_states t USING(setup_id)
    WHERE s.market='CRYPTO' AND upper(replace(s.symbol,'/',''))=ANY(%s)
      AND coalesce(t.body->>'state','')<>ALL(%s)"""


def normalized(symbol):
    """Admission's symbol match (``ACTIVE_SYMBOL_ALREADY_MANAGED``): no slash, upper case."""
    return symbol.replace("/", "").upper()


class ValidateRateLimited(Exception):
    """Refused before any work: ``retry_after`` whole seconds until a validation may run."""

    def __init__(self, retry_after):
        super().__init__(RATE_LIMITED)
        self.retry_after = retry_after


class ValidateLimiter:
    """Per credential: one validation at a time, at most ``per_minute`` in a rolling minute.

    Held in memory by the one app process; a refused call is not counted.
    """

    def __init__(self, per_minute=VALIDATIONS_PER_MINUTE, *, clock=time.monotonic):
        if type(per_minute) is not int or per_minute < 1 or not callable(clock):
            raise ValueError("VALIDATE_RATE_LIMIT_INVALID")
        self.per_minute, self.clock = per_minute, clock
        self._lock = threading.Lock()
        self._recent = {}
        self._busy = set()

    @contextmanager
    def hold(self, key):
        now = self.clock()
        with self._lock:
            if key in self._busy:
                raise ValidateRateLimited(1)
            recent = self._recent.setdefault(key, deque())
            while recent and now - recent[0] >= RATE_WINDOW_SECONDS:
                recent.popleft()
            if len(recent) >= self.per_minute:
                wait = RATE_WINDOW_SECONDS - (now - recent[0])
                raise ValidateRateLimited(max(1, math.ceil(wait)))
            recent.append(now)
            self._busy.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(key)


def principal_key(principal):
    return (principal.role, principal.agent_id, bool(principal.legacy))


# --- The fresh price ----------------------------------------------------------------------------

def _usable(value):
    return isinstance(value, D) and value.is_finite() and value > 0


def live_quote(symbol, market):
    """The coin's quote and last trade from the research context's market read as a
    ``LiveQuote`` (read time = that read's), or None when there is no usable quote: admission's
    REST rules (0 < bid <= ask, an aware quote time not after the read)."""
    if market is None:
        return None
    quote, trade = market["quotes"].get(symbol), market["trades"].get(symbol)
    read_at = market["fetched_at"]
    if (quote is None or not _usable(quote.bid) or not _usable(quote.ask)
            or quote.bid > quote.ask or not isinstance(quote.timestamp, datetime)
            or quote.timestamp.tzinfo is None or quote.timestamp > read_at):
        return None
    last = last_at = trade_id = None
    if (trade is not None and _usable(trade.price) and isinstance(trade.timestamp, datetime)
            and trade.timestamp.tzinfo is not None):
        last, last_at, trade_id = trade.price, trade.timestamp, str(trade.trade_id)
    return LiveQuote(symbol, quote.bid, quote.ask, quote.timestamp, QUOTE_SOURCE, read_at,
                     last, last_at, trade_id)


# --- One pick's warnings -----------------------------------------------------------------------

def _warning(check, path, **numbers):
    return {"check": check, "code": CODES[check], "path": path, **numbers}


def _reward_risk(target, max_entry, stop):
    with localcontext() as context:
        context.prec = 80
        return ((target - max_entry) / (max_entry - stop)).quantize(
            RATIO_PLACES, rounding=ROUND_HALF_EVEN)


def level_warnings(prefix, levels):
    """Admission's level rule (``ManagedExecution.admit``: INVALID_OR_EXPIRED_SETUP)."""
    t, m, s, p = (levels[key] for key in ("entry_trigger", "max_entry_price", "stop", "target"))
    if not 0 < s < t <= m < p:
        return [_warning("LEVELS", prefix + ".levels",
                         rule="0 < stop < entry_trigger <= max_entry_price < target")]
    if p - m < MINIMUM_REWARD_RISK * (m - s):
        return [_warning("LEVELS", prefix + ".levels",
                         reward_risk_at_max_entry=str(_reward_risk(p, m, s)),
                         minimum_reward_risk=str(MINIMUM_REWARD_RISK))]
    return []


def superseded_by(schedule, setup, pick_symbol, pick_slot):
    """Whether the runtime's supersession pass retires ``setup`` once this pick is selected:
    a report-V3 setup still WATCHING whose run this pick's run supersedes
    (RESEARCH_RUN_SUPERSESSION_V1 or _V2, as the configured schedule selects)."""
    if setup["state"] != "WATCHING" or setup["report_schema_version"] != REPORT_SCHEMA_V3:
        return False
    try:
        slot = datetime.fromisoformat(setup["run_slot"])
        if slot.tzinfo is None:
            return False
    except (TypeError, ValueError):
        return False
    if supersession_version(schedule) == SUPERSESSION_VERSION_V2:
        return superseding_run_slot(schedule, slot, setup["symbol"],
                                    [(pick_slot, pick_symbol)]) is not None
    return slot < pick_slot


def pick_warnings(item, *, run_slot, run_slot_text, coin, quote, active, schedule, now):
    """One entry of ``admission_warnings``: ``index``, ``signal_id``, ``entry_type`` (the
    system check's, when it read a price), ``warnings`` in admission's order and
    ``not_evaluated`` (``check`` and ``reason``)."""
    entry = {"index": item.index, "signal_id": item.signal_id, "entry_type": None,
             "warnings": [], "not_evaluated": []}
    if item.pick is None:
        entry["not_evaluated"].append({"check": "ALL", "reason": PICK_SCHEMA_INVALID})
        return entry
    prefix, data, symbol = item.path, item.canonical, item.pick.symbol
    levels = {key: D(str(value)) for key, value in data["levels"].items()}
    warnings, skipped = entry["warnings"], entry["not_evaluated"]
    warnings += level_warnings(prefix, levels)
    blocking = [setup for setup in active if normalized(setup["symbol"]) == normalized(symbol)
                and not superseded_by(schedule, setup, symbol, run_slot)]
    if blocking:
        warnings.append(_warning("ACTIVE_SYMBOL", prefix + ".symbol",
                                 setup_state=blocking[0]["state"]))
    if coin is None:
        skipped.append({"check": "PRICE_GRID", "reason": SYMBOL_NOT_IN_UNIVERSE})
    else:
        try:
            off = off_grid_levels(coin["price_increment"], data["levels"])
        except (CryptoExecutionError, ArithmeticError, TypeError, ValueError):
            off = None
        if off is None:
            skipped.append({"check": "PRICE_GRID", "reason": PRICE_INCREMENT_UNAVAILABLE})
        elif off:
            warnings.append(_warning("PRICE_GRID", prefix + ".levels",
                                     price_increment=str(coin["price_increment"]),
                                     levels=sorted(off)))
    # SYSTEM_CHECK_V1 exactly as admission evaluates it, on the packet fields it reads.
    packet = {"market": "CRYPTO", "run_slot": run_slot_text, "levels": data["levels"],
              "state": {"agent_current_price": data["agent_current_price"],
                        "agent_price_at": data["agent_price_at"]}}
    try:
        check = evaluate(packet, quote, now=now)
    except (ArithmeticError, InvalidOperation, KeyError, TypeError, ValueError):
        skipped += [{"check": name, "reason": PICK_SCHEMA_INVALID}
                    for name in SYSTEM_CHECKS.values()]
        return entry
    entry["entry_type"] = check["entry_type"]
    failed = [SYSTEM_CHECKS[name] for name, result in check["checks"].items()
              if result == FAIL]
    live = check.get("live") or {}
    numbers = {
        "STOP_DISTANCE": {"path": prefix + ".levels.stop",
                          "stop_distance_fraction": check["stop_distance_fraction"],
                          "minimum_stop_fraction": check["minimum_stop_fraction"]},
        "PRICE_MATCH": {"path": prefix + ".agent_current_price", "live_mid": live.get("mid"),
                        "price_deviation_fraction": check["price_deviation_fraction"],
                        "price_mismatch_fraction": check["price_mismatch_fraction"]},
        "STOP_NOT_HIT": {"path": prefix + ".levels.stop", "live_bid": live.get("bid"),
                         "live_last": live.get("last")},
        "ENTRY_TYPE": {"path": prefix + ".levels.entry_trigger", "live_mid": live.get("mid"),
                       "entry_offset_fraction": check["entry_offset_fraction"],
                       "entry_type_band_fraction": check["entry_type_band_fraction"]},
    }
    for name in failed:
        warnings.append(_warning(name, **numbers[name]))
    if "STOP_DISTANCE" in failed:
        skipped += [{"check": name, "reason": STOP_DISTANCE_FIRST} for name in LIVE_CHECKS]
    elif quote is None:
        reason = SYMBOL_NOT_IN_UNIVERSE if coin is None else LIVE_PRICE_UNAVAILABLE
        skipped += [{"check": name, "reason": reason} for name in LIVE_CHECKS]
    return entry


# --- The service ---------------------------------------------------------------------------------

def active_crypto_setups(repo, symbols):
    """Every crypto setup not in a terminal state whose symbol matches one of ``symbols`` the
    way admission matches (``ACTIVE_SYMBOL_ALREADY_MANAGED``), any agent's. One read, no
    transaction, no lock."""
    if not symbols:
        return []
    with repo.connect() as conn:
        return conn.execute(ACTIVE_CRYPTO_SETUPS, (
            sorted({normalized(symbol) for symbol in symbols}), sorted(TERMINAL),
        )).fetchall()


class ReportValidator:
    """The route's service: intake's verdict (``intake.validate_report``), then the warnings.

    ``intake`` is the app's research cycle, ``context`` its ``ResearchContextService`` (report
    V3's schedule and universe, and the cached prices), ``repo`` the ledger it reads for active
    setups, and ``max_seconds`` intake's ``MANAGED_REPORT_MAX_SECONDS``.
    """

    def __init__(self, intake, context, *, repo, max_seconds):
        self.intake, self.context, self.repo = intake, context, repo
        self.max_seconds = max_seconds

    def validate(self, raw):
        check = self.intake.validate_report(raw, max_seconds=self.max_seconds,
                                            v3=self.context.intake())
        report = check.intake
        try:
            coins, market = self.context.admission_view()
        except ResearchCapabilityUnavailable:
            coins, market = {}, None
        symbols = [item.pick.symbol for item in report.picks if item.pick is not None]
        active = active_crypto_setups(self.repo, symbols)
        warnings = []
        for item in report.picks:
            symbol = item.pick.symbol if item.pick is not None else None
            coin = coins.get(symbol) if symbol is not None else None
            warnings.append(pick_warnings(
                item, run_slot=report.run_slot, run_slot_text=report.canonical["run_slot"],
                coin=coin, quote=live_quote(symbol, market) if coin is not None else None,
                active=active, schedule=self.context.schedule, now=check.checked_at,
            ))
        return validation_body(check, warnings)


def validation_body(check, warnings):
    """The 200 body: intake's counts and ``item_results`` exactly as its 202 receipt carries
    them, the warnings, and that nothing was recorded."""
    receipt = check.receipt
    return {
        "status": VALIDATED,
        "validation_version": VALIDATE_VERSION,
        "checked_at": check.checked_at.isoformat(),
        "cycle_id": receipt["cycle_id"],
        "already_recorded": receipt["idempotent_replay"],
        "expires_at": receipt["expires_at"],
        "submitted_count": receipt["submitted_count"],
        "contender_count": receipt["contender_count"],
        "rejected_count": receipt["rejected_count"],
        "skipped_count": receipt["skipped_count"],
        "item_results": receipt["item_results"],
        "admission_warnings": warnings,
        "recorded": False,
        "trade_authorized": False,
    }
