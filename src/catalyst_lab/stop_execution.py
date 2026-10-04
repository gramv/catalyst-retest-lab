"""``CRYPTO_STOP_EXECUTION_V1``: the app emulates the stop on the reference market and exits with
a collared IOC sell, then market (plan docs/TRADING-QUALITY-PLAN.md section 2 item 6, section 5
move 4, phase D; owner, 2026-10-02: "B, C, D looks good").

**Paper trading only.** Evidence: the native stop-limit filled 3 of 12 triggered stops on the
thin Alpaca paper venue; the market fallback then slipped 0.7-1.9% (2026-10-02); Alpaca paper
fills a marketable limit on a thin coin only when its venue prints through. The plan's research
(NautilusTrader-style emulation): watch the stop in the app against the liquid reference market's
trades and, when it triggers, release a marketable order with a price collar.

Scope: a crypto setup admitted under ``CRYPTO_COINBASE_TRIGGER_V1`` and
``CRYPTO_STOP_BREACH_V3`` (a coin with a Coinbase USD product, an engine with the Coinbase feed)
on an engine where this version is switched on (``execution_setting``; default off). Admission
records the exact policy as ``stop_execution_policy``; the detection version recorded beside it
stays ``CRYPTO_STOP_BREACH_V3`` (the entry's pre-fill stop check, ``entry_working``, still reads
V3). Every other setup is unchanged.

1. **Resting protection unchanged.** The native stop-limit (with ``CRYPTO_STOP_BREACH_V4``'s
   cushion when recorded) stays at Alpaca until the app decides the breach, so a stopped app is
   still protected by the broker.
2. **Breach on the reference market, confirmed.** While Coinbase is healthy for the coin
   (``coinbase_feed``'s rule: acknowledged channels, a heartbeat at most 3 s old, no clock skew,
   no trade-tape gap) the breach is decided on Coinbase prints only (Alpaca's bid and prints
   decide nothing): the *run* is the Coinbase prints at or below the stop traded since the last
   Coinbase print above the stop, no earlier than the pass that first measured this stop
   (``stop_since``) and within ``CONFIRM_WINDOW_SECONDS`` (10 s). The breach is confirmed by
   ``PRINTS``: at least ``CONFIRM_PRINTS`` (2) distinct trades in the run, the latest at most
   ``PRINT_MAX_AGE_SECONDS`` (5 s) old; or ``DWELL``: the run's first print at least
   ``DWELL_SECONDS`` (3 s) old with no print above the stop since (a thin coin that trades once
   and stays down). One print at the stop followed by a print above it is noise, not a breach:
   V3 sold on a single print, and a lone sweep of a thin Coinbase book is exactly the false
   stop-out this version must not repeat; two prints, or three seconds without recovery, cost at
   most about one protection pass more on a real break.
3. **Fail-closed fallback.** While Coinbase is unhealthy (or absent) the breach is V3's fallback
   exactly (V2's Alpaca evidence: a print at or below the stop, or the bid held 15 s), recorded
   ``fallback: true``, and the exit is today's: the native stop-limit works its 5 s
   (``stop_breach.FALLBACK_SECONDS``), then the market sell (``STOP_LIMIT_NOT_FILLED``). No
   collar is priced off a stale reference.
4. **The emulated exit.** A confirmed reference breach records ``stop_breached_at``,
   ``stop_breach_evidence`` and ``exit_requested`` = ``STOP_EMULATED_EXIT`` (final for the
   lifecycle) and appends ``STOP_BREACH_ESTABLISHED``. The protection plan then cancels every
   working entry and the native stop-limit (Alpaca refuses a sell while a buy of the coin works;
   the stop-limit reserves the inventory), each under its own exact one-use authorization, and
   once the broker shows them gone and the inventory available it sends ONE IOC limit sell (the
   *collar*) at the latest retained Coinbase print times (1 - ``COLLAR_FRACTION``) (1.0%),
   rounded down to the grid. The collar is recorded (``stop_execution_collar``,
   ``STOP_EXECUTION_COLLAR``) before it is authorized. If the reference is unhealthy at that
   moment, no collar: the market sell goes at once (``REFERENCE_UNHEALTHY_AT_EXIT``).
5. **Market fallback.** An IOC ends at once at the broker; a collar still working
   ``COLLAR_WAIT_SECONDS`` (3 s) after it was planned is cancelled (``COLLAR_NOT_FILLED``). Once
   no exit works, whatever remains is sold at market (the existing close path: refused-close
   backoff, fresh client order IDs, exact authorization). A collar that was planned but never
   authorized is re-proposed with the same price and client order ID until its wait ends.
6. **Record.** The breach body carries the confirming prints, the last print above the stop,
   Coinbase's quote and health, Alpaca's bid and quote time, and the confirmation latency; the
   close appends ``STOP_EXECUTION_RESULT`` (``execution_quality.exit_summary``): the exit path,
   the average exit price, and slippage against the stop and against the reference print.

Choice of N prints / dwell (justification): the 2026-09-28 false stops were single quote dips
without a trade; under V3 a single Coinbase print at the stop already filters those, but a thin
Coinbase book can print one stray trade below a level and recover in the same second. Requiring
two trades (on liquid coins they arrive within milliseconds of a real break) or three seconds
without a print above the stop (on illiquid coins) keeps the latency of a true break to about
one to three protection passes while removing the single-print whipsaw. These numbers are a
starting point to be measured (``STOP_EXECUTION_RESULT``), not proven.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from catalyst_lab import coinbase_feed, stop_breach
from catalyst_lab.strategies import core

VERSION = "CRYPTO_STOP_EXECUTION_V1"
FIELD = "stop_execution_policy"
COLLAR_FIELD = "stop_execution_collar"
CONFIRM_PRINTS = 2
DWELL_SECONDS = 3
CONFIRM_WINDOW_SECONDS = 10
PRINT_MAX_AGE_SECONDS = 5
COLLAR_FRACTION = Decimal("0.01")
COLLAR_WAIT_SECONDS = 3
TIME_IN_FORCE = "ioc"
MAX_RECORDED_PRINTS = 10

PRINTS, DWELL = "PRINTS", "DWELL"
EXIT_REASON = "STOP_EMULATED_EXIT"
COLLAR_REASON = "STOP_EMULATED_COLLAR"
COLLAR_CANCEL_REASON = "COLLAR_NOT_FILLED"
REFERENCE_UNHEALTHY_AT_EXIT = "REFERENCE_UNHEALTHY_AT_EXIT"
COLLAR_EVENT = "STOP_EXECUTION_COLLAR"
RESULT_EVENT = "STOP_EXECUTION_RESULT"
EXIT_PATH_EMULATED = "COLLAR_IOC_THEN_MARKET"
EXIT_PATH_FALLBACK = "NATIVE_STOP_LIMIT_THEN_MARKET"


@dataclass(frozen=True)
class StopExecutionPolicy:
    """The exact ``CRYPTO_STOP_EXECUTION_V1`` record; a state can never carry altered numbers."""

    policy_id: str
    confirm_prints: int
    dwell_seconds: int
    confirm_window_seconds: int
    print_max_age_seconds: int
    collar_fraction: str
    collar_wait_seconds: int
    time_in_force: str
    detection: str

    def __post_init__(self):
        if asdict(self) != _EXPECTED:
            raise ValueError("EXPLICIT_STOP_EXECUTION_POLICY_REQUIRED")

    def record(self):
        return asdict(self)


_EXPECTED = {
    "policy_id": VERSION, "confirm_prints": CONFIRM_PRINTS, "dwell_seconds": DWELL_SECONDS,
    "confirm_window_seconds": CONFIRM_WINDOW_SECONDS,
    "print_max_age_seconds": PRINT_MAX_AGE_SECONDS, "collar_fraction": str(COLLAR_FRACTION),
    "collar_wait_seconds": COLLAR_WAIT_SECONDS, "time_in_force": TIME_IN_FORCE,
    "detection": "COINBASE_PRINTS_CONFIRMED_ELSE_" + stop_breach.STOP_BREACH_VERSION_V3,
}
CRYPTO_STOP_EXECUTION = StopExecutionPolicy(**_EXPECTED)


def admission_fields(enabled, coinbase_reference):
    """The field admission records: the policy, only on an engine with the version switched on
    and only for a setup of ``CRYPTO_STOP_BREACH_V3`` (the Coinbase reference)."""
    if not enabled or not coinbase_reference:
        return {}
    return {FIELD: CRYPTO_STOP_EXECUTION.record()}


def recorded(state):
    """The policy a setup's state carries, or None. An altered record is refused."""
    value = state.get(FIELD) if isinstance(state, dict) else None
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("EXPLICIT_STOP_EXECUTION_POLICY_REQUIRED")
    try:
        return StopExecutionPolicy(**value)
    except TypeError:
        raise ValueError("EXPLICIT_STOP_EXECUTION_POLICY_REQUIRED") from None


def active(state):
    return recorded(state) is not None


def emulated(state):
    """Whether the setup's established breach is this version's own (reference-confirmed), so the
    app exits it rather than waiting on the native stop-limit."""
    evidence = state.get("stop_breach_evidence") if isinstance(state, dict) else None
    return (isinstance(evidence, dict) and evidence.get("version") == VERSION
            and evidence.get("fallback") is False)


def _seconds(later, earlier):
    return Decimal(str((later - earlier).total_seconds()))


def reference_confirmation(view, marks, stop, now):
    """The ``PRINTS`` or ``DWELL`` evidence on ``view``'s retained prints, or None."""
    try:
        since = datetime.fromisoformat(marks["stop_since"])
    except (KeyError, TypeError, ValueError):
        return None
    prints = [p for p in view.prints if since <= p.traded_at <= now]
    above = [p for p in prints if p.price > stop]
    last_above = max(above, key=lambda p: p.traded_at) if above else None
    start = now - timedelta(seconds=CONFIRM_WINDOW_SECONDS)
    run, seen = [], set()
    for item in sorted(prints, key=lambda p: (p.traded_at, p.trade_id)):
        if (core.reaches_stop(item.price, stop) and item.traded_at >= start
                and item.trade_id not in seen
                and (last_above is None or item.traded_at > last_above.traded_at)):
            seen.add(item.trade_id)
            run.append(item)
    if not run:
        return None
    first, latest = run[0], run[-1]
    if len(run) >= CONFIRM_PRINTS and _seconds(now, latest.traded_at) <= PRINT_MAX_AGE_SECONDS:
        confirmation = PRINTS
    elif _seconds(now, first.traded_at) >= DWELL_SECONDS:
        confirmation = DWELL
    else:
        return None
    return {
        "breach_evidence": "COINBASE_" + confirmation,
        "confirmation": confirmation,
        "fallback": False,
        "evidence_at": latest.traded_at.isoformat(),
        "evidence_price": str(min(p.price for p in run)),
        "print_count": len(run),
        "prints": [p.record(now) for p in run[-MAX_RECORDED_PRINTS:]],
        "first_print_at": first.traded_at.isoformat(),
        "confirm_latency_seconds": _seconds(now, first.traded_at),
        "last_above": last_above.record(now) if last_above is not None else None,
        "exit_path": EXIT_PATH_EMULATED,
    }


def reference_record(reference):
    if not isinstance(reference, coinbase_feed.ReferenceView):
        return {"provider": coinbase_feed.PROVIDER, "product_id": None, "healthy": False,
                "code": coinbase_feed.FEED_NOT_CONFIGURED}
    record = reference.health()
    record.update(
        bid=str(reference.bid) if reference.bid is not None else None,
        ask=str(reference.ask) if reference.ask is not None else None,
        quote_at=reference.quote_at.isoformat() if reference.quote_at else None,
        last_print=reference.last_print.record(reference.as_of)
        if reference.last_print is not None else None,
    )
    return record


def evaluate(state, *, observation, bid, reference, now):
    """One protection pass over an open position of this version: ``(changes, breach)`` as
    ``stop_breach.evaluate_v3``. A confirmed reference breach also requests the exit
    (``EXIT_REASON``) unless another exit is already requested."""
    stop = Decimal(str(state["stop"]))
    marks = state.get("stop_breach_marks")
    changes = {}
    if not isinstance(marks, dict) or marks.get("stop") is None or (
            Decimal(str(marks["stop"])) != stop):
        marks = {"stop": state["stop"], "stop_since": now.isoformat(),
                 "bid_since": None, "bid": None, "bid_quote_at": None}
        changes["stop_breach_marks"] = marks
        if state.get("stop_breached_at") is not None or state.get("stop_breach_evidence"):
            changes.update(stop_breached_at=None, stop_breach_evidence=None)
    elif state.get("stop_breached_at"):
        return {}, None  # Established for this stop: final.
    if isinstance(reference, coinbase_feed.ReferenceView) and reference.healthy is True:
        if marks.get("bid_since") is not None:
            marks = {**marks, "bid_since": None, "bid": None, "bid_quote_at": None}
            changes["stop_breach_marks"] = marks
        evidence = reference_confirmation(reference, marks, stop, now)
    else:
        fallback_changes, fallback = stop_breach.evaluate(
            {**state, "stop_breach_marks": marks, "stop_breached_at": None,
             "stop_breach_evidence": None},
            observation=observation, bid=bid, now=now)
        if "stop_breach_marks" in fallback_changes:
            marks = fallback_changes["stop_breach_marks"]
            changes["stop_breach_marks"] = marks
        evidence = None if fallback is None else {
            **{k: v for k, v in fallback.items() if k not in stop_breach._V2_FRAME},
            "fallback": True, "exit_path": EXIT_PATH_FALLBACK,
            "fallback_seconds": stop_breach.FALLBACK_SECONDS,
            "fallback_at": (now + timedelta(seconds=stop_breach.FALLBACK_SECONDS)).isoformat(),
        }
    if evidence is None:
        return changes, None
    breach = {
        "version": VERSION,
        "lifecycle_id": state.get("lifecycle_id"),
        "stop": state["stop"],
        "stop_since": marks["stop_since"],
        **evidence,
        "reference": reference_record(reference),
        "alpaca": {"bid": str(bid) if bid is not None else None,
                   "quote_at": (observation or {}).get("quote_at"),
                   "trade_price": (observation or {}).get("trade_price"),
                   "trade_at": (observation or {}).get("trade_at")},
        "established_at": now.isoformat(),
    }
    changes.update(stop_breached_at=now.isoformat(), stop_breach_evidence=breach)
    if not evidence["fallback"] and not state.get("exit_requested"):
        changes["exit_requested"] = EXIT_REASON
    return changes, breach


def collar_reference(reference, breach, now):
    """The reference price the collar is set from: the latest retained Coinbase print while the
    feed is healthy (the confirming run's lowest print when none is retained), else None (the
    exit goes to market: no collar is priced off an unhealthy reference)."""
    if not isinstance(reference, coinbase_feed.ReferenceView) or reference.healthy is not True:
        return None
    if reference.last_print is not None and reference.last_print.traded_at <= now:
        return reference.last_print.price, reference.last_print.record(now)
    if isinstance(breach, dict) and breach.get("evidence_price"):
        return Decimal(str(breach["evidence_price"])), {"basis": "CONFIRMING_RUN_LOW"}
    return None


def collar_key(setup_id, state):
    breach = state.get("stop_breach_evidence") or {}
    return f"{setup_id}:stop-collar:{state.get('lifecycle_id')}:{breach.get('stop_since')}"


def collar_record(*, limit, reference_price, reference_print, client_order_id, qty, bid, now):
    return {
        "version": VERSION, "status": "PLANNED", "limit_price": str(limit),
        "reference_price": str(reference_price), "reference_print": reference_print,
        "collar_fraction": str(COLLAR_FRACTION), "time_in_force": TIME_IN_FORCE,
        "client_order_id": client_order_id, "qty": str(qty),
        "alpaca_bid": str(bid) if bid is not None else None,
        "planned_at": now.isoformat(),
        "wait_ends_at": (now + timedelta(seconds=COLLAR_WAIT_SECONDS)).isoformat(),
    }


def skipped_record(reason, now):
    return {"version": VERSION, "status": "SKIPPED", "reason": reason,
            "planned_at": now.isoformat()}


def collar_waited(collar, now):
    """Whether a planned collar's wait has ended."""
    try:
        return now >= datetime.fromisoformat(collar["wait_ends_at"])
    except (KeyError, TypeError, ValueError):
        return True


__all__ = ["COLLAR_CANCEL_REASON", "COLLAR_EVENT", "COLLAR_FIELD", "COLLAR_FRACTION",
           "COLLAR_REASON", "COLLAR_WAIT_SECONDS", "CONFIRM_PRINTS", "CRYPTO_STOP_EXECUTION",
           "DWELL_SECONDS", "EXIT_REASON", "FIELD", "RESULT_EVENT", "VERSION", "active",
           "admission_fields", "collar_reference", "emulated", "evaluate", "recorded",
           "reference_confirmation"]
