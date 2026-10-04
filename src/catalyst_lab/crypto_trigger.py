"""``CRYPTO_ALPACA_TRIGGER_V1``: the crypto trigger version for Alpaca's thin crypto venue.

Owner decisions of 2026-09-26 and 2026-09-27 (``docs/CRYPTO-AGENT-LOOP.md`` sections 4.4, 4.5,
5 and 8; package crypto-trigger, plan phase 4a). On 2026-09-26 only 3 of Alpaca's 33 USD pairs
were within 10 bps and 32 within 1%; prints are sparse, and an unchanged quote is not re-sent,
so its own timestamp can be minutes old while it is still the current quote. A named version
under the owner's 2026-09-24 ruling: it applies to crypto setups admitted from report-V3
packets (``applies``; the WATCHING state records ``trigger_version`` at admission). Every other
setup (V1, V2, B1, B2, operator engineering, and every crypto setup admitted before this
version) keeps today's trigger exactly.

1. Touch: a trade print at or below the entry trigger T, or a fresh ask at or below T.
   Pullback and immediate entries only; breakouts are refused at admission (SYSTEM_CHECK_V1).
2. Confirmation: a fresh ask at or below the max entry M; spread (ask - bid) / mid at most
   ``MAX_SPREAD_BPS`` (1%, exactly 1% passes); a healthy feed; the setup inside its window.
   The order stays a limit at M.
3. Freshness by read time: a quote is fresh when it was received on the stream, or read over
   REST, at most ``QUOTE_MAX_AGE_SECONDS`` ago. Its own exchange timestamp is recorded, never
   bounded. An observation without a recorded read time (a direct caller) falls back to its
   exchange timestamp, which can only understate the read time, so the fallback never admits a
   quote the read-time rule would refuse.
4. Invalidation before the trigger: a print at or below the stop
   (``STOP_TRADED_BEFORE_TRIGGER``) or a fresh bid at or below it
   (``STOP_QUOTED_BEFORE_TRIGGER``); a touch whose fresh ask is above M
   (``PRICE_BEYOND_MAX_ENTRY``, after the spread check, as today). A spread above the cap at a
   touch never invalidates: the setup waits for the next touch and the wait is recorded
   (``SPREAD_ABOVE_MAXIMUM``), as is a print touch without a fresh quote
   (``FRESH_QUOTE_UNAVAILABLE``).

``evaluate`` is pure. Decisions compare exact Decimal products under an 80-digit context; the
recorded spread is rounded to 4 decimal places of a basis point for reading only.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from catalyst_lab.strategies import core
from catalyst_lab.system_check import (
    LIVE_PRICE_MAX_AGE_SECONDS,
    REST_SOURCE,
    STREAM_SOURCE,
    is_v3_packet,
)

D = Decimal

CRYPTO_TRIGGER_VERSION = "CRYPTO_ALPACA_TRIGGER_V1"
# The one spread cap of this version: (ask - bid) / mid at most 1% (100 bps); exactly 1% passes.
MAX_SPREAD_BPS = D("100")
# A quote received on the stream or read over REST this recently is fresh (by read time).
QUOTE_MAX_AGE_SECONDS = LIVE_PRICE_MAX_AGE_SECONDS
# A print touches only while it is this recent (today's trigger uses the same five seconds).
PRINT_MAX_AGE_SECONDS = 5
SPREAD_QUANTUM = D("0.0001")  # Recorded basis points only; decisions compare exact products.
_PRECISION = 80

PRINT, QUOTE = "PRINT", "QUOTE"
NO_TOUCH, IGNORED, WAIT, INVALIDATE, CONFIRM = (
    "NO_TOUCH", "IGNORED", "WAIT", "INVALIDATE", "CONFIRM")

STOP_TRADED_BEFORE_TRIGGER = "STOP_TRADED_BEFORE_TRIGGER"
STOP_QUOTED_BEFORE_TRIGGER = "STOP_QUOTED_BEFORE_TRIGGER"
PRICE_BEYOND_MAX_ENTRY = "PRICE_BEYOND_MAX_ENTRY"
DATA_FEED_FAILURE = "DATA_FEED_FAILURE"
SPREAD_ABOVE_MAXIMUM = "SPREAD_ABOVE_MAXIMUM"
FRESH_QUOTE_UNAVAILABLE = "FRESH_QUOTE_UNAVAILABLE"
# Re-checked under the shared lock, a touch seen moments earlier is no longer current (its
# print or quote aged past five seconds while the entry was being sized).
TOUCH_NOT_CURRENT = "TOUCH_NOT_CURRENT"
# No trigger and no decision: the setup keeps WATCHING and the next touch is evaluated afresh.
WAIT_REASONS = frozenset({SPREAD_ABOVE_MAXIMUM, FRESH_QUOTE_UNAVAILABLE, TOUCH_NOT_CURRENT})

WAIT_EVENT = "CRYPTO_TRIGGER_WAIT"
WAIT_KEY = "crypto-trigger-wait:"  # + setup, reason and UTC minute: at most one per minute.

# How ``quote_read_at`` was established: the runtime's stream receipt, its REST read, a read
# time some other caller recorded, or (none recorded) the quote's own timestamp.
STREAM_RECEIPT, REST_READ, RECORDED_READ_TIME, QUOTE_TIMESTAMP = (
    "STREAM_RECEIPT", "REST_READ", "RECORDED_READ_TIME", "QUOTE_TIMESTAMP")


def applies(packet):
    """This version applies to a crypto setup admitted from a report-V3 packet."""
    return is_v3_packet(packet) and packet.get("market") == "CRYPTO"


def active(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("trigger_version") == CRYPTO_TRIGGER_VERSION


@dataclass(frozen=True)
class Verdict:
    """One evaluation: ``outcome``, its ``reason`` (WAIT and INVALIDATE) and ``touch``, the
    kind of market event that decided it: PRINT or QUOTE (the entry touch, or for the stop
    invalidations the print or quote that reached the stop), None otherwise."""

    outcome: str
    reason: str | None
    touch: str | None
    evidence: dict


def _number(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("NONFINITE_MARKET_NUMBER")
    return result


def _instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("INVALID_MARKET_TIMESTAMP")
    return result


def _text(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _seconds(now, then):
    return str(D(str((now - then).total_seconds()))) if then is not None else None


def _print(observation):
    """``(price, at, trade_id)`` of the observation's print, or None; malformed raises."""
    if observation.get("trade_price") is None:
        return None
    try:
        price, at = _number(observation["trade_price"]), _instant(observation["trade_at"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("INVALID_MARKET_EVIDENCE") from None
    if price <= 0:
        raise ValueError("INVALID_MARKET_EVIDENCE")
    trade_id = observation.get("trade_id")
    return price, at, str(trade_id) if trade_id is not None else None


def read_basis(observation):
    """How the observation's read time was established (``QUOTE_TIMESTAMP``: the fallback)."""
    if observation.get("quote_read_at") is None:
        return QUOTE_TIMESTAMP
    return {STREAM_SOURCE: STREAM_RECEIPT, REST_SOURCE: REST_READ}.get(
        observation.get("quote_source"), RECORDED_READ_TIME)


def _quote(observation):
    """``(bid, ask, quote_at, read_at, code)``; ``code`` names why the quote is unusable."""
    if not all(observation.get(k) for k in ("bid", "ask", "quote_at")):
        return None, None, None, None, "QUOTE_MISSING"
    try:
        bid, ask = _number(observation["bid"]), _number(observation["ask"])
        quote_at = _instant(observation["quote_at"])
        read = observation.get("quote_read_at")
        read_at = _instant(read) if read is not None else quote_at
    except (TypeError, ValueError, ArithmeticError):
        return None, None, None, None, "QUOTE_INVALID"
    if not 0 < bid <= ask:
        return bid, ask, quote_at, read_at, "QUOTE_INVALID"
    if quote_at > read_at:
        return bid, ask, quote_at, read_at, "QUOTE_AFTER_READ"
    return bid, ask, quote_at, read_at, None


def spread_within_cap(bid, ask):
    """(ask - bid) / mid at most MAX_SPREAD_BPS basis points, as an exact product."""
    with localcontext() as context:
        context.prec = _PRECISION
        return (ask - bid) * 20000 <= MAX_SPREAD_BPS * (ask + bid)


def spread_bps(bid, ask):
    with localcontext() as context:
        context.prec = _PRECISION
        return ((ask - bid) * 20000 / (ask + bid)).quantize(SPREAD_QUANTUM, ROUND_HALF_EVEN)


def evaluate(levels, observation, *, now, admitted_at):
    """The version's verdict for one observation of a WATCHING setup at ``now``.

    ``levels`` are Decimals (``entry_trigger``, ``max_entry_price``, ``stop``). The
    observation may carry a print (``trade_price``, ``trade_at``, ``trade_id``), a quote
    (``bid``, ``ask``, its exchange time ``quote_at``, its read time ``quote_read_at`` and
    ``quote_source``), the stream's last trade (``last_price``, ``last_at``,
    ``last_trade_id``), ``feed_healthy`` and ``quote_attempts``. A malformed print raises
    ``INVALID_MARKET_EVIDENCE``; a malformed quote is unusable (never fresh).
    """
    t, m, s = levels["entry_trigger"], levels["max_entry_price"], levels["stop"]
    printed = _print(observation)
    bid, ask, quote_at, read_at, code = _quote(observation)
    read_age = (now - read_at).total_seconds() if read_at is not None else None
    fresh = code is None and 0 <= read_age <= QUOTE_MAX_AGE_SECONDS
    if code is None and not fresh:
        code = "QUOTE_NOT_FRESH"
    last = observation.get("last_price")
    evidence = {
        "version": CRYPTO_TRIGGER_VERSION,
        "evaluated_at": now.isoformat(),
        "touch": None,
        "levels": {"entry_trigger": str(t), "max_entry_price": str(m), "stop": str(s)},
        "print": None if printed is None else {
            "price": str(printed[0]), "at": printed[1].isoformat(), "trade_id": printed[2],
            "age_seconds": _seconds(now, printed[1])},
        "bid": str(bid) if bid is not None else None,
        "ask": str(ask) if ask is not None else None,
        "spread_bps": str(spread_bps(bid, ask)) if code in (None, "QUOTE_NOT_FRESH") else None,
        "max_spread_bps": str(MAX_SPREAD_BPS),
        "quote_source": observation.get("quote_source"),
        "quote_at": _text(quote_at),
        "quote_read_at": _text(read_at),
        "quote_read_basis": read_basis(observation),
        "quote_read_age_seconds": _seconds(now, read_at),
        "quote_age_seconds": _seconds(now, quote_at),
        "quote_max_age_seconds": QUOTE_MAX_AGE_SECONDS,
        "quote_fresh": fresh,
        "quote_code": code,
        "last": str(last) if last is not None else (
            str(printed[0]) if printed is not None else None),
        "last_at": observation.get("last_at") if last is not None else (
            printed[1].isoformat() if printed is not None else None),
        "last_trade_id": observation.get("last_trade_id") if last is not None else (
            printed[2] if printed is not None else None),
    }
    if observation.get("quote_attempts") is not None:
        evidence["quote_attempts"] = observation["quote_attempts"]

    def verdict(outcome, reason=None, touch=None):
        return Verdict(outcome, reason, touch, {**evidence, "touch": touch})

    if printed is not None and (printed[1] < admitted_at or printed[1] > now):
        # As today: a print before admission, or stamped after now, is not evaluated at all.
        return verdict(IGNORED, "TRIGGER_BEFORE_ADMISSION" if printed[1] < admitted_at
                       else "STALE_MARKET_OBSERVATION")
    if printed is not None and core.reaches_stop(printed[0], s):
        return verdict(INVALIDATE, STOP_TRADED_BEFORE_TRIGGER, PRINT)
    if observation.get("feed_healthy") is not True:
        return verdict(INVALIDATE, DATA_FEED_FAILURE)
    if fresh and core.reaches_stop(bid, s):
        return verdict(INVALIDATE, STOP_QUOTED_BEFORE_TRIGGER, QUOTE)
    print_touch = (
        printed is not None and core.touches_entry(printed[0], t)
        and 0 <= (now - printed[1]).total_seconds() <= PRINT_MAX_AGE_SECONDS
    )
    quote_touch = fresh and core.touches_entry(ask, t)
    if not print_touch and not quote_touch:
        return verdict(NO_TOUCH)
    touch = PRINT if print_touch else QUOTE
    if not fresh:
        return verdict(WAIT, FRESH_QUOTE_UNAVAILABLE, touch)
    if not spread_within_cap(bid, ask):
        return verdict(WAIT, SPREAD_ABOVE_MAXIMUM, touch)
    if ask > m:
        return verdict(INVALIDATE, PRICE_BEYOND_MAX_ENTRY, touch)
    return verdict(CONFIRM, None, touch)


def decision_quote(state, observation):
    """The entry decision context's quote times.

    Other setups keep today's ``quote_at`` (the observation's own). Under this version
    ``quote_at``, the time the authorization gate requires to be at most five seconds old, is
    the quote's read time, and ``quote_exchange_at`` and ``quote_read_at`` record both times.
    """
    if not active(state):
        return {"quote_at": observation["quote_at"]}
    _, _, quote_at, read_at, _ = _quote(observation)
    read = _text(read_at) if read_at is not None else observation.get("quote_at")
    return {
        "quote_at": read,
        "quote_exchange_at": _text(quote_at) if quote_at is not None else None,
        "quote_read_at": read,
        "quote_read_basis": read_basis(observation),
        "quote_source": observation.get("quote_source"),
        "trigger_version": CRYPTO_TRIGGER_VERSION,
    }


def observation(*, quote=None, row=None, received_at=None, printed=None, attempts=None):
    """The observation the runtime hands this version: the print (if any), the fresh quote
    (a ``system_check.LiveQuote`` read by read time) or else the stream's last quote with its
    receipt time (never fresh by then), the stream's last trade, and the reader's attempts.

    Only a runtime whose market stream is ready for the symbol builds one, so ``feed_healthy``
    is true; the provider and feed are the stream's.
    """
    row = row or {}
    result = {"feed_healthy": True, "data_provider": "ALPACA", "data_feed": "CRYPTO_US"}
    if printed is not None:
        result.update({k: printed[k] for k in ("trade_price", "trade_at", "trade_id")})
    if quote is not None:
        result.update(bid=str(quote.bid), ask=str(quote.ask), quote_at=quote.quote_at.isoformat(),
                      quote_read_at=quote.read_at.isoformat(), quote_source=quote.source)
    elif received_at is not None and all(row.get(k) for k in ("bid", "ask", "quote_at")):
        result.update(bid=row["bid"], ask=row["ask"], quote_at=row["quote_at"],
                      quote_read_at=received_at.isoformat(), quote_source=STREAM_SOURCE)
    if row.get("trade_price") is not None:
        result.update(last_price=row["trade_price"], last_at=row.get("trade_at"),
                      last_trade_id=row.get("trade_id"))
    if attempts is not None:
        result["quote_attempts"] = attempts
    return result


def body(observation, verdict):
    """The TRIGGER_CONFIRMED body under this version: the observation, its version and the
    evaluation's evidence (``crypto_trigger``)."""
    return {**observation, "trigger_version": CRYPTO_TRIGGER_VERSION,
            "crypto_trigger": verdict.evidence if verdict is not None else None}
