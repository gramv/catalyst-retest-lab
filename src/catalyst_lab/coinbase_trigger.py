"""``CRYPTO_COINBASE_TRIGGER_V1``: the crypto entry trigger read on Coinbase's public market, the
order still a limit on Alpaca paper.

Authority: owner approval 2026-09-29 of the operating session's plan ("use Coinbase's public
market prices as the reference for crypto entry triggers and stop breaches, while orders stay on
Alpaca paper"). Alpaca's crypto venue is thin (on 2026-09-28 BTC traded $126k there against
$557M on Coinbase, and most pairs print in only a few percent of minutes); the research kit sets
entries and stops from Coinbase candles, so the trigger reads the same market. A named version
under the owner's 2026-09-24 ruling; ``CRYPTO_ALPACA_TRIGGER_V1`` (``crypto_trigger.py``) keeps
its definition and every setup that recorded it keeps it.

Scope. A crypto setup admitted from a report-V3 packet (``CRYPTO_ALPACA_TRIGGER_V1``'s scope)
whose coin has a Coinbase USD product (``coinbase_feed.product_id``), on an engine that runs the
Coinbase reference feed (the managed runtime always does). Admission records ``trigger_version``
and ``reference_product`` (the Coinbase product) in the WATCHING state; everything here keys off
that state field. A report-V3 coin without a Coinbase product keeps ``CRYPTO_ALPACA_TRIGGER_V1``.

T = entry trigger, M = max entry, S = stop. Coinbase is the reference market (``coinbase_feed``);
Alpaca is where the order executes. Evaluated in this order:

1. Invalidation before the trigger, from Coinbase only (an Alpaca print or bid alone never
   invalidates): a Coinbase print at or below S traded at or after admission, among the prints
   the feed retains (the last ``coinbase_feed.PRINT_RETENTION_SECONDS``):
   ``STOP_TRADED_BEFORE_TRIGGER``; a fresh Coinbase bid at or below S:
   ``STOP_QUOTED_BEFORE_TRIGGER``. Recorded Coinbase evidence invalidates whatever the feed's
   health.
2. A Coinbase feed unhealthy for the coin (``coinbase_feed`` health rule): no touch can confirm;
   the setup waits (``COINBASE_FEED_UNHEALTHY``, recorded).
3. Touch: a Coinbase print at or below T, traded at or after admission and at most
   ``REFERENCE_PRINT_MAX_AGE_SECONDS`` old (``PRINT``), else a fresh Coinbase ask at or below T
   (``QUOTE``). Neither: no touch, nothing recorded.
4. Confirmation on Alpaca, as ``CRYPTO_ALPACA_TRIGGER_V1`` confirms: a healthy Alpaca feed (else
   ``DATA_FEED_FAILURE`` invalidates, as there); a fresh Alpaca quote (by read time, 5 s; else
   the setup waits, ``FRESH_QUOTE_UNAVAILABLE``); the Alpaca spread at most 1% (else waits,
   ``SPREAD_ABOVE_MAXIMUM``); the Alpaca ask at or below M (else waits,
   ``ALPACA_ASK_ABOVE_MAX_ENTRY``: under this version an Alpaca ask above M at a Coinbase touch
   is Alpaca's book not following yet, never an invalidation, since Alpaca's book alone ends no
   setup; the next touch is evaluated afresh). Then the entry is authorized as today, a limit
   buy at M.

Freshness (``REFERENCE_QUOTE_MAX_AGE_SECONDS``): a Coinbase quote is fresh while its receipt is
at most 5 s old (its read time, as ``CRYPTO_ALPACA_TRIGGER_V1``) and so is its own time. Coinbase
sends the top of book only with a trade (the ``ticker`` channel), so its own time is when it was
last known current; the ticker sent at subscription can be minutes old and is not fresh. An
Alpaca quote is fresh by its read time exactly as in ``CRYPTO_ALPACA_TRIGGER_V1``. A print's
age is by its trade time (0 to 5 s), as there.

``evaluate`` is pure; decisions compare exact Decimals. Waits, invalidations, the lock's
re-check and the entry decision's quote times work as in ``CRYPTO_ALPACA_TRIGGER_V1``: the
decision's ``quote_at`` is the Alpaca quote's read time, which the authorization gate bounds at
five seconds.
"""

from catalyst_lab import coinbase_feed, crypto_trigger
from catalyst_lab.crypto_trigger import (
    CONFIRM,
    DATA_FEED_FAILURE,
    FRESH_QUOTE_UNAVAILABLE,
    INVALIDATE,
    MAX_SPREAD_BPS,
    NO_TOUCH,
    PRINT,
    QUOTE,
    QUOTE_MAX_AGE_SECONDS,
    SPREAD_ABOVE_MAXIMUM,
    STOP_QUOTED_BEFORE_TRIGGER,
    STOP_TRADED_BEFORE_TRIGGER,
    TOUCH_NOT_CURRENT,
    WAIT,
    Verdict,
    _instant,
    _number,
    _quote,
    _seconds,
    _text,
    read_basis,
    spread_bps,
    spread_within_cap,
)
from catalyst_lab.strategies import core

COINBASE_TRIGGER_VERSION = "CRYPTO_COINBASE_TRIGGER_V1"
REFERENCE_PRINT_MAX_AGE_SECONDS = 5
REFERENCE_QUOTE_MAX_AGE_SECONDS = 5

COINBASE_FEED_UNHEALTHY = "COINBASE_FEED_UNHEALTHY"
ALPACA_ASK_ABOVE_MAX_ENTRY = "ALPACA_ASK_ABOVE_MAX_ENTRY"
REFERENCE_MISSING = "REFERENCE_MISSING"
# No trigger and no decision: the setup keeps WATCHING and the next touch is evaluated afresh.
WAIT_REASONS = frozenset({COINBASE_FEED_UNHEALTHY, FRESH_QUOTE_UNAVAILABLE, SPREAD_ABOVE_MAXIMUM,
                          ALPACA_ASK_ABOVE_MAX_ENTRY, TOUCH_NOT_CURRENT})
# ``reference_verdict`` only: Coinbase touched; Alpaca's confirmation decides.
TOUCH = "TOUCH"
# An Alpaca print is neither a touch nor an invalidation under this version: the runtime
# consumes a queued one with this reason, unevaluated.
ALPACA_PRINT_CONSUMED_REASON = "ALPACA_PRINT_NOT_TRIGGER_EVIDENCE"


def applies(packet):
    """A report-V3 crypto packet whose coin has a Coinbase USD product."""
    return crypto_trigger.applies(packet) and coinbase_feed.product_id(
        packet.get("symbol")) is not None


def active(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("trigger_version") == COINBASE_TRIGGER_VERSION


def trigger_fields(packet, *, reference_feed):
    """The trigger fields admission records: this version and the coin's Coinbase product for a
    packet of its scope on an engine with the reference feed (``reference_feed``); else
    ``CRYPTO_ALPACA_TRIGGER_V1`` for a report-V3 crypto packet; else nothing (today's trigger)."""
    if not crypto_trigger.applies(packet):
        return {}
    if reference_feed and applies(packet):
        return {"trigger_version": COINBASE_TRIGGER_VERSION,
                "reference_product": coinbase_feed.product_id(packet["symbol"])}
    return {"trigger_version": crypto_trigger.CRYPTO_TRIGGER_VERSION}


def reference_record(view, levels, *, admitted_at, now):
    """The Coinbase part of an observation (JSON-ready) from the feed's ``ReferenceView`` at
    ``now``: its health, its quote with both times, the lowest print since admission (the stop
    check) and the latest print at or below T since admission (the touch)."""
    low = view.lowest_print(traded_from=admitted_at, traded_to=now)
    touch = view.latest_print_at_or_below(levels["entry_trigger"], traded_from=admitted_at,
                                          traded_to=now)
    return {
        **view.health(),
        "bid": str(view.bid) if view.bid is not None else None,
        "ask": str(view.ask) if view.ask is not None else None,
        "quote_at": _text(view.quote_at),
        "quote_received_at": _text(view.quote_received_at),
        "low_print": low.record() if low is not None else None,
        "touch_print": touch.record() if touch is not None else None,
        "last_print": view.last_print.record() if view.last_print is not None else None,
        "print_retention_seconds": coinbase_feed.PRINT_RETENTION_SECONDS,
    }


def _reference_print(value):
    """``(price, traded_at, received_at, trade_id)`` of a recorded print, or None; malformed
    evidence raises ``INVALID_MARKET_EVIDENCE`` (as a malformed print does in V1)."""
    if value is None:
        return None
    try:
        price, at = _number(value["price"]), _instant(value["at"])
        received = _instant(value["received_at"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("INVALID_MARKET_EVIDENCE") from None
    if price <= 0:
        raise ValueError("INVALID_MARKET_EVIDENCE")
    trade_id = value.get("trade_id")
    return price, at, received, str(trade_id) if trade_id is not None else None


def _reference_quote(reference, now):
    """``(bid, ask, quote_at, received_at, fresh, code)`` of the recorded Coinbase quote."""
    if not all(reference.get(k) for k in ("bid", "ask", "quote_at", "quote_received_at")):
        return None, None, None, None, False, "QUOTE_MISSING"
    try:
        bid, ask = _number(reference["bid"]), _number(reference["ask"])
        quote_at = _instant(reference["quote_at"])
        received = _instant(reference["quote_received_at"])
    except (TypeError, ValueError, ArithmeticError):
        return None, None, None, None, False, "QUOTE_INVALID"
    if not 0 < bid <= ask:
        return bid, ask, quote_at, received, False, "QUOTE_INVALID"
    fresh = (0 <= (now - received).total_seconds() <= REFERENCE_QUOTE_MAX_AGE_SECONDS
             and (now - quote_at).total_seconds() <= REFERENCE_QUOTE_MAX_AGE_SECONDS)
    return bid, ask, quote_at, received, fresh, None if fresh else "QUOTE_NOT_FRESH"


def _print_evidence(printed, now):
    if printed is None:
        return None
    price, at, received, trade_id = printed
    return {"price": str(price), "at": at.isoformat(), "received_at": received.isoformat(),
            "trade_id": trade_id, "age_seconds": _seconds(now, at)}


def reference_verdict(levels, reference, *, now, admitted_at):
    """Steps 1-3 on the Coinbase part alone: ``INVALIDATE``, ``WAIT``
    (``COINBASE_FEED_UNHEALTHY``), ``NO_TOUCH``, or ``TOUCH`` (with ``PRINT`` or ``QUOTE``) for
    Alpaca's confirmation to decide. Its evidence is the ``reference`` object of the record."""
    t, s = levels["entry_trigger"], levels["stop"]
    facts = reference if isinstance(reference, dict) else {}
    healthy = facts.get("healthy") is True
    low = _reference_print(facts.get("low_print"))
    touch_print = _reference_print(facts.get("touch_print"))
    bid, ask, quote_at, received, fresh, code = _reference_quote(facts, now)
    evidence = {
        "provider": facts.get("provider", coinbase_feed.PROVIDER),
        "product_id": facts.get("product_id"),
        "healthy": healthy,
        "code": facts.get("code") if facts else REFERENCE_MISSING,
        "as_of": facts.get("as_of"),
        "heartbeat_received_at": facts.get("heartbeat_received_at"),
        "low_print": _print_evidence(low, now),
        "touch_print": _print_evidence(touch_print, now),
        "bid": str(bid) if bid is not None else None,
        "ask": str(ask) if ask is not None else None,
        "quote_at": _text(quote_at),
        "quote_received_at": _text(received),
        "quote_age_seconds": _seconds(now, quote_at) if quote_at is not None else None,
        "quote_read_age_seconds": _seconds(now, received) if received is not None else None,
        "quote_max_age_seconds": REFERENCE_QUOTE_MAX_AGE_SECONDS,
        "print_max_age_seconds": REFERENCE_PRINT_MAX_AGE_SECONDS,
        "quote_fresh": fresh,
        "quote_code": code,
    }

    def verdict(outcome, reason=None, touch=None):
        return Verdict(outcome, reason, touch, evidence)

    if low is not None and admitted_at <= low[1] <= now and core.reaches_stop(low[0], s):
        return verdict(INVALIDATE, STOP_TRADED_BEFORE_TRIGGER, PRINT)
    if fresh and core.reaches_stop(bid, s):
        return verdict(INVALIDATE, STOP_QUOTED_BEFORE_TRIGGER, QUOTE)
    if not healthy:
        return verdict(WAIT, COINBASE_FEED_UNHEALTHY)
    print_touch = (
        touch_print is not None and core.touches_entry(touch_print[0], t)
        and touch_print[1] >= admitted_at
        and 0 <= (now - touch_print[1]).total_seconds() <= REFERENCE_PRINT_MAX_AGE_SECONDS
    )
    if not print_touch and not (fresh and core.touches_entry(ask, t)):
        return verdict(NO_TOUCH)
    return verdict(TOUCH, None, PRINT if print_touch else QUOTE)


def evaluate(levels, observation, *, now, admitted_at):
    """The version's verdict for one observation of a WATCHING setup at ``now``.

    ``levels`` are Decimals (``entry_trigger``, ``max_entry_price``, ``stop``). The observation
    carries the Coinbase part as ``reference`` (``reference_record``) and Alpaca's confirming
    quote as ``CRYPTO_ALPACA_TRIGGER_V1``'s observation does (``bid``, ``ask``, ``quote_at``,
    ``quote_read_at``, ``quote_source``, the stream's last trade, ``feed_healthy``,
    ``quote_attempts``). A malformed recorded print raises ``INVALID_MARKET_EVIDENCE``; a
    missing Coinbase part is an unhealthy reference.
    """
    m = levels["max_entry_price"]
    first = reference_verdict(levels, observation.get("reference"), now=now,
                              admitted_at=admitted_at)
    bid, ask, quote_at, read_at, code = _quote(observation)
    read_age = (now - read_at).total_seconds() if read_at is not None else None
    fresh = code is None and 0 <= read_age <= QUOTE_MAX_AGE_SECONDS
    if code is None and not fresh:
        code = "QUOTE_NOT_FRESH"
    evidence = {
        "version": COINBASE_TRIGGER_VERSION,
        "evaluated_at": now.isoformat(),
        "touch": None,
        "levels": {k: str(levels[k]) for k in ("entry_trigger", "max_entry_price", "stop")},
        "reference": first.evidence,
        # Alpaca's confirming quote, recorded as CRYPTO_ALPACA_TRIGGER_V1 records it.
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
        "last": observation.get("last_price"),
        "last_at": observation.get("last_at"),
        "last_trade_id": observation.get("last_trade_id"),
    }
    if observation.get("quote_attempts") is not None:
        evidence["quote_attempts"] = observation["quote_attempts"]

    def verdict(outcome, reason=None, touch=None):
        return Verdict(outcome, reason, touch, {**evidence, "touch": touch})

    if first.outcome != TOUCH:
        return verdict(first.outcome, first.reason, first.touch)
    if observation.get("feed_healthy") is not True:
        return verdict(INVALIDATE, DATA_FEED_FAILURE)
    if not fresh:
        return verdict(WAIT, FRESH_QUOTE_UNAVAILABLE, first.touch)
    if not spread_within_cap(bid, ask):
        return verdict(WAIT, SPREAD_ABOVE_MAXIMUM, first.touch)
    if ask > m:
        return verdict(WAIT, ALPACA_ASK_ABOVE_MAX_ENTRY, first.touch)
    return verdict(CONFIRM, None, first.touch)


def observation(*, reference, quote=None, row=None, received_at=None, attempts=None):
    """The observation the runtime hands this version: the Coinbase part (``reference``) and,
    for a touch, Alpaca's freshest quote exactly as ``crypto_trigger.observation`` builds it
    (never an Alpaca print). Only a runtime whose Alpaca stream is ready for the symbol builds
    one, so ``feed_healthy`` (Alpaca's) is true."""
    return {**crypto_trigger.observation(quote=quote, row=row, received_at=received_at,
                                         attempts=attempts),
            "reference": reference}


def decision_quote(state, observation):
    """The entry decision context's quote times, as ``CRYPTO_ALPACA_TRIGGER_V1`` records them:
    ``quote_at``, which the authorization gate requires to be at most five seconds old, is the
    Alpaca quote's read time; the context also names this version and the Coinbase product."""
    _, _, quote_at, read_at, _ = _quote(observation)
    read = _text(read_at) if read_at is not None else observation.get("quote_at")
    reference = observation.get("reference") if isinstance(
        observation.get("reference"), dict) else {}
    return {
        "quote_at": read,
        "quote_exchange_at": _text(quote_at) if quote_at is not None else None,
        "quote_read_at": read,
        "quote_read_basis": read_basis(observation),
        "quote_source": observation.get("quote_source"),
        "trigger_version": COINBASE_TRIGGER_VERSION,
        "reference_product": reference.get("product_id") or state.get("reference_product"),
    }


def body(observation, verdict):
    """The TRIGGER_CONFIRMED body under this version: the observation, its version and the
    evaluation's evidence (``crypto_trigger``)."""
    return {**observation, "trigger_version": COINBASE_TRIGGER_VERSION,
            "crypto_trigger": verdict.evidence if verdict is not None else None}
