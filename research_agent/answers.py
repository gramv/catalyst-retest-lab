"""``MUSE_ANSWER_RULES_V1``: the research agent's answers to the app's window reviews and Jev's
early-exit flags (package kit-answers, 2026-09-29).

**Paper trading only.** A kit-side agent policy, not an app rule: the app's rules, its
authorization gate and every broker action stay the app's (``docs/MUSE-CONNECTION.md`` 4f). An
answer states the agent's side of a decision the app takes with Jev. It carries no size, order,
level or suggestion.

Why (owner, 2026-09-29, after a review found that the kit never answered the app's reviews or
exit flags: "you take the right decision. And fix all these issues."): unanswered, every window
review (``CRYPTO_WINDOW_REVIEW_V1``) is Jev's alone, and a Jev early-exit flag
(``EARLY_EXIT_FLAG_V1``) can only time out, because an early exit needs both sides. On
2026-09-29 at 02:28:58 UTC Jev flagged UNI/USD ``BROKEN`` at bid 8.467 (entry 8.604, stop
8.4427). Nobody answered, and the stop-limit fallback closed it at 8.4403 three minutes later.

The pending items are ``GET /api/v1/lab/reviews`` (the same items as the research context's
``pending_reviews``). Each is decided from Coinbase's public 5-minute candles (``market``), with
the trade's levels as the item states them:

* **A Jev early-exit flag** (``EXIT_FLAG``). EXIT when the last completed 5-minute bar closed at
  or below the stop, or at or below the half-risk line, ``entry - 0.5 x (entry - stop)``: the
  trade has given back half of its planned risk. The entry is the item's ``trade.entry`` (the
  average entry) and the stop ``trade.levels.stop`` (the stop in force when Jev flagged).
  Otherwise CONTINUE: the stop decides. UNI on 2026-09-29: the 02:20-02:25 bar closed 8.5061,
  at or below 8.604 - 0.5 x 0.1613 = 8.52335, so EXIT.
* **A continue-or-exit review** (``DAY_REVIEW``: the window review of
  ``CRYPTO_WINDOW_REVIEW_V1``, or the review of a 24-hour version; either round). CONTINUE when
  the last completed 5-minute close is at or above the trade's entry (``request.trade.entry``):
  the trade is working. Otherwise EXIT, a time stop: the trade did not work within its window.
  A discussion round's reply is decided the same way, on the bar completed by then.
* **No usable data.** No completed 5-minute bar that closed within the last 15 minutes
  (Coinbase has no candle for five minutes without a trade), the candles unavailable, or a
  level missing: no answer. The reason is recorded and the app's fallback applies (a review:
  Jev decides alone; a flag: it times out and the trade stays). The next run tries again while
  the item is still pending.

The texts are factual and built only from the numbers read. ``what_changed`` gives the bar's
time and close against the line (the half-risk line, the stop or the entry). ``next_24h`` gives
what the rule expects (the stop decides; the trade continues toward its target; a time stop).
``proves_wrong`` names the line a 5-minute close back above would invalidate (after an EXIT),
or a close at or below it (after a CONTINUE). No confidence, no sources, no suggested levels,
no symbol and nothing that names the agent; the app's own validator
(``catalyst_lab.day_review.validate_review_answer``) checks every body before it is sent.

What the answer does together with Jev's (the app's rules, ``trade_review`` and
``day_review``):

* **A flag.** EXIT agrees and the trade sells at market (``EXIT_AGREED``,
  ``EARLY_EXIT_AGREED``). CONTINUE keeps it with its stop and target (``EXIT_NOT_AGREED``). So
  under this policy a Jev flag ends the trade exactly when the last 5-minute close is at or
  below the half-risk line (or the stop); otherwise the stop decides.
* **A review** (answer rule ``DAY_REVIEW_ANSWER_RULE_V2``). Agreement decides. A disagreement
  opens one discussion round, whose reply is decided again on the latest bar; after it,
  anything but agreement exits. An unusable Jev answer exits whatever the agent said. So a trade
  continues past its window only when its last 5-minute close is at or above the entry and Jev
  also says continue. A time stop (EXIT) ends it unless, by the discussion reply, the close is
  back at or above the entry and Jev's final answer is continue.

Idempotent and cheap. ``answer_id`` is a UUID5 of the flag ID, or of the review ID and its
round (a review is answered once per round, and the app replays an ``answer_id`` across both
rounds). An item's first decided body is written to its record before it is sent, and it is the
only body ever sent for it: a resend after no answer sends the same body, which the app replays.
A 404 or 409 (answered, resolved, window closed) is final, never an error. A run with nothing
pending is one GET. The run folder keeps one record per item (``items/``) and one line per run
(``polls/<UTC day>.jsonl``); the token is never written to either.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from catalyst_lab.crypto_holding import AGENT_REVIEW_ANSWER_VERSION
from catalyst_lab.day_review import TEXT_LIMITS, validate_review_answer
from research_agent import market
from research_agent.build import plain
from research_agent.submit import BaseUrlRefused, SubmitError, answer_route, checked_base_url

RULES_VERSION = "MUSE_ANSWER_RULES_V2"
REVIEWS_ROUTE = "/api/v1/lab/reviews"
DAY_REVIEW, EXIT_FLAG = "DAY_REVIEW", "EXIT_FLAG"
KINDS = (DAY_REVIEW, EXIT_FLAG)
FIRST, DISCUSSION = "FIRST", "DISCUSSION"
EXIT, CONTINUE = "EXIT", "CONTINUE"
BAR_SECONDS = market.FIVE_MINUTE_SECONDS
MAX_BAR_AGE = timedelta(minutes=15)  # The last completed bar closed at most this long ago.
CANDLE_SPAN = timedelta(hours=1)  # The candles read for a coin: the last hour (12 bars).
HALF = Decimal("0.5")
BODY_KEYS = frozenset({"schema_version", "answer_id", "decision", *TEXT_LIMITS})

# Why an item got its decision.
AT_OR_BELOW_STOP = "CLOSE_AT_OR_BELOW_STOP"
AT_OR_BELOW_HALF_RISK = "CLOSE_AT_OR_BELOW_HALF_RISK"
ABOVE_EXIT_LINE = "CLOSE_ABOVE_EXIT_LINE"
AT_OR_ABOVE_ENTRY = "CLOSE_AT_OR_ABOVE_ENTRY"
BELOW_ENTRY = "CLOSE_BELOW_ENTRY_TIME_STOP"
# Why an item got no answer (no usable data).
NO_RECENT_BAR = "NO_COMPLETED_5M_BAR_IN_15_MINUTES"
# MUSE_ANSWER_RULES_V2 (2026-09-29): with no completed Coinbase 5-minute bar in the last 15
# minutes (a thin coin at night trades minutes apart, and Coinbase leaves out empty intervals),
# the price is the app's own bid stated in the item, when it is at most as old as a usable bar.
COINBASE_CLOSE, APP_BID = "COINBASE_5M_CLOSE", "APP_BID"
CANDLES_UNAVAILABLE = "COINBASE_CANDLES_UNAVAILABLE"
LEVEL_MISSING = "LEVEL_MISSING"

# The state of one item's record (items/<key>.json).
NO_DATA = "NO_DATA"  # No usable data at the last reading; nothing sent. Read again next run.
DRY_RUN = "DRY_RUN"  # Decided with --dry-run and written, not sent. A real run decides again.
REFUSED_LOCALLY = "REFUSED_LOCALLY"  # The app's own validator refused the body; never sent.
SENDING = "SENDING"  # The body is fixed and its POST under way (left so if the process died).
UNKNOWN = "UNKNOWN"  # No answer (a transport error, a 5xx): the same body goes again next run.
RETRY = "RETRY"  # The app stored nothing (401, 503, not yet asked): the same body again.
RECORDED = "RECORDED"  # 200: the app recorded the answer, or replayed it.
CLOSED = "CLOSED"  # 404 or 409: the item takes no answer any more. Final, not an error.
REFUSED = "REFUSED"  # Another 4xx: the app refused this body and stored nothing. Never resent.
FINAL_STATES = frozenset({RECORDED, CLOSED, REFUSED})
RESEND_STATES = frozenset({SENDING, UNKNOWN, RETRY})
FAILED_STATES = frozenset({UNKNOWN, RETRY, REFUSED, REFUSED_LOCALLY})
# The item cannot be read, or its record cannot: left alone this run (exit code 1).
UNREADABLE, RECORD_UNREADABLE = "UNREADABLE", "RECORD_UNREADABLE"

ITEMS_DIR, POLLS_DIR, LOCK_FILE = "items", "polls", "answer.lock"
RECORD_SCHEMA = "RESEARCH_AGENT_ANSWER_RECORD_V1"
POLL_SCHEMA = "RESEARCH_AGENT_ANSWER_POLL_V1"
_CODE = re.compile(r"[A-Z_]{1,80}")  # The app's refusal codes (``managed_service``).


class AnswerError(Exception):
    """The pending items could not be read, so nothing is answered this run. The message is a
    code, never a token or a response body."""


class ItemUnreadable(ValueError):
    """A pending item this policy cannot read (its kind, ID, round or route); not answered."""


class RecordUnreadable(ValueError):
    """An item's record exists but cannot be read. The item is left alone: a body may already
    have been sent under its ``answer_id``, so it is never decided again."""


# --- GET /api/v1/lab/reviews ------------------------------------------------------------------

def fetch_pending(base_url, token, *, client=None, timeout=30.0):
    """The agent's pending items (``GET /api/v1/lab/reviews``, the agent's own token): ``{as_of,
    poll_hint_seconds, request_lead_seconds, items, trade_authorized}``. ``client`` is an
    injectable ``httpx.Client``, as in ``context.fetch_context``. Raises ``AnswerError`` for
    anything but a 200 with an ``items`` list."""
    try:
        base_url = checked_base_url(base_url)
    except BaseUrlRefused as exc:
        raise AnswerError(str(exc)) from None
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = client.get(base_url + REVIEWS_ROUTE,
                                  headers={"Authorization": "Bearer " + token,
                                           "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AnswerError(f"REVIEWS_CONNECTION_ERROR: {type(exc).__name__}") from None
    finally:
        if owns_client:
            client.close()
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200:
        detail = body.get("detail") if isinstance(body, dict) else None
        code = detail if isinstance(detail, str) and _CODE.fullmatch(detail) else ""
        raise AnswerError(f"REVIEWS_HTTP_{response.status_code}" + (f": {code}" if code else ""))
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise AnswerError("REVIEWS_UNEXPECTED_SHAPE")
    return body


# --- The items ------------------------------------------------------------------------------

def answer_id_for(kind, item_id, rnd=None):
    """The deterministic ``answer_id``: a UUID5 of the flag ID, or of the review ID and its round.
    A rerun sends the same ID with the same body, which the app replays. The round is part of a
    review's: the app replays an ``answer_id`` across both rounds, so a discussion reply under
    the first answer's ID would never be recorded as a reply."""
    if kind == EXIT_FLAG:
        seed = f"agent-review-answer:exit-flag:{item_id}"
    else:
        seed = f"agent-review-answer:review:{item_id}:{rnd}"
    return str(uuid5(NAMESPACE_URL, seed))


@dataclass(frozen=True)
class Item:
    """One pending item as this policy reads it; the levels as the item states them."""

    kind: str
    item_id: str
    round: str | None  # FIRST or DISCUSSION for a review; None for a flag.
    symbol: str | None
    setup_id: str | None
    answer_due_at: str | None
    entry: object
    stop: object
    target: object
    window_seconds: int | None  # The review's window (CRYPTO_WINDOW_REVIEW_V1), else None.
    raw: dict

    @property
    def key(self):
        if self.kind == EXIT_FLAG:
            return f"flag-{self.item_id}"
        return f"review-{self.item_id}-{self.round.lower()}"

    @property
    def answer_id(self):
        return answer_id_for(self.kind, self.item_id, self.round)

    @property
    def coin(self):
        """The Coinbase base currency (``UNI`` for ``UNI/USD``), or None for another quote."""
        base, _, quote = (self.symbol or "").partition("/")
        return base if base.isalnum() and quote == "USD" else None

    def label(self):
        what = (f"flag {self.item_id}" if self.kind == EXIT_FLAG
                else f"review {self.item_id}, {self.round}")
        return f"{self.kind} {self.symbol or '?'} ({what})"


def _object(value):
    return value if isinstance(value, dict) else {}


def parse_item(raw):
    """An ``Item`` from one of ``fetch_pending``'s items, or ``ItemUnreadable``. The item's own
    ``answer_route`` must be exactly the one its kind and ID give, and its schema
    ``AGENT_REVIEW_ANSWER_V1``: an answer never goes anywhere else."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        raise ItemUnreadable("ITEM_KIND_UNKNOWN")
    kind = raw["kind"]
    id_field = "flag_id" if kind == EXIT_FLAG else "review_id"
    try:
        item_id = str(UUID(str(raw.get(id_field))))
    except ValueError:
        raise ItemUnreadable(f"ITEM_ID_INVALID: {id_field}") from None
    rnd = None
    if kind == DAY_REVIEW:
        rnd = raw.get("round")
        if rnd not in (FIRST, DISCUSSION):
            raise ItemUnreadable("ITEM_ROUND_UNKNOWN")
    if raw.get("answer_route") != answer_route(kind, item_id):
        raise ItemUnreadable("ITEM_ROUTE_UNEXPECTED")
    if raw.get("answer_schema") != AGENT_REVIEW_ANSWER_VERSION:
        raise ItemUnreadable("ITEM_ANSWER_SCHEMA_UNEXPECTED")
    if kind == EXIT_FLAG:
        trade = _object(raw.get("trade"))  # The flag's evidence: levels, quote, entry, R.
        levels = _object(trade.get("levels"))
        entry, stop, target, window = trade.get("entry"), levels.get("stop"), levels.get(
            "target"), None
    else:
        request = _object(raw.get("request"))
        trade = _object(request.get("trade"))
        entry, stop, target = trade.get("entry"), trade.get("stop"), trade.get("target")
        window = request.get("holding_window_seconds")
        window = window if type(window) is int and window > 0 else None
    symbol = raw.get("symbol")
    return Item(kind=kind, item_id=item_id, round=rnd,
                symbol=symbol if isinstance(symbol, str) else None,
                setup_id=raw.get("setup_id") if isinstance(raw.get("setup_id"), str) else None,
                answer_due_at=raw.get("answer_due_at") if isinstance(
                    raw.get("answer_due_at"), str) else None,
                entry=entry, stop=stop, target=target, window_seconds=window, raw=raw)


def price(value):
    """A level as a positive, finite ``Decimal``, or None (missing or unreadable)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def missing_levels(item):
    """The levels the item's rule needs and the item does not state usably: the entry (both
    rules) and the stop (a flag's)."""
    needed = ("entry", "stop") if item.kind == EXIT_FLAG else ("entry",)
    return [name for name in needed if price(getattr(item, name)) is None]


# --- MUSE_ANSWER_RULES_V1 ---------------------------------------------------------------------

def flag_rule(entry, stop, close):
    """``(decision, reason, half_risk_line, exit_line)`` for a Jev flag. EXIT when ``close`` is
    at or below the stop or the half-risk line (``entry - 0.5 x (entry - stop)``), else
    CONTINUE. The exit line is the higher of the two (the stop only once it is above the entry,
    when the half-risk line is below it)."""
    half_risk = entry - HALF * (entry - stop)
    line = max(half_risk, stop)
    if close <= stop:
        return EXIT, AT_OR_BELOW_STOP, half_risk, line
    if close <= half_risk:
        return EXIT, AT_OR_BELOW_HALF_RISK, half_risk, line
    return CONTINUE, ABOVE_EXIT_LINE, half_risk, line


def review_rule(entry, close):
    """``(decision, reason)`` for a continue-or-exit review: CONTINUE at or above the entry,
    else EXIT (a time stop)."""
    if close >= entry:
        return CONTINUE, AT_OR_ABOVE_ENTRY
    return EXIT, BELOW_ENTRY


def bar_end(bar):
    return bar.started_at + timedelta(seconds=BAR_SECONDS)


def usable_bar(rows, *, retrieved_at):
    """``(bar, None)``: the last completed 5-minute bar of ``rows`` (as fetched at
    ``retrieved_at``) when it closed at most 15 minutes before then; else ``(None, why)``."""
    bars = market.completed_bars(rows, BAR_SECONDS, retrieved_at=retrieved_at)
    if not bars:
        return None, "no completed 5-minute bar in the candles read"
    bar = bars[-1]
    if retrieved_at - bar_end(bar) > MAX_BAR_AGE:
        return None, (f"the last completed 5-minute bar closed at "
                      f"{bar_end(bar):%Y-%m-%d %H:%M} UTC, more than 15 minutes before the "
                      f"candles were read ({retrieved_at.astimezone(UTC):%H:%M:%S} UTC)")
    return bar, None


@dataclass(frozen=True)
class Decision:
    """An item's decision (``decision`` None: no usable data, no answer), why, the bar used and
    the numbers its texts are built from (strings, as sent)."""

    decision: str | None
    reason: str
    detail: str
    bar: market.Bar | None = None
    numbers: dict | None = None


def no_answer(reason, detail):
    return Decision(None, reason, detail)


def _span(bar):
    return f"{bar.started_at.astimezone(UTC):%Y-%m-%d %H:%M}-{bar_end(bar):%H:%M} UTC"


def _period(window_seconds):
    if window_seconds is None:
        return "another 24 hours"
    if window_seconds % 3600 == 0:
        return f"another {window_seconds // 3600}-hour window"
    return f"another {window_seconds // 60}-minute window"


def app_quote(item, *, now):
    """``(bid, quote_at)``: the app's own bid as the item states it (a review's
    ``request.trade``, a flag's evidence ``trade.quote``) when it is at most ``MAX_BAR_AGE``
    old at ``now`` (MUSE_ANSWER_RULES_V2), else None."""
    if item.kind == EXIT_FLAG:
        quote = _object(_object(item.raw.get("trade")).get("quote"))
    else:
        quote = _object(_object(item.raw.get("request")).get("trade"))
    bid = price(quote.get("bid"))
    try:
        at = datetime.fromisoformat(str(quote.get("quote_at")))
    except ValueError:
        return None
    if bid is None or at.tzinfo is None or not timedelta(0) <= now - at <= MAX_BAR_AGE:
        return None
    return bid, at


def decide(item, candles):
    """The decision for ``item`` from its coin's candles (``market.fetch_candles``'s result),
    or, with no usable bar, from the app's own bid in the item (MUSE_ANSWER_RULES_V2). The item
    must state its levels (``missing_levels`` empty)."""
    retrieved_at = datetime.fromisoformat(candles["retrieved_at"])
    try:
        bar, why = usable_bar(candles["candles"], retrieved_at=retrieved_at)
    except market.MarketDataError as exc:
        bar, why = None, str(exc)
        if app_quote(item, now=retrieved_at) is None:
            return no_answer(CANDLES_UNAVAILABLE, why)
    entry, stop, target = price(item.entry), price(item.stop), price(item.target)
    numbers = {"entry": plain(entry), "stop": plain(stop) if stop is not None else None,
               "target": plain(target) if target is not None else None}
    if bar is not None:
        value, word = bar.close, "5-minute close"
        numbers.update(source=COINBASE_CLOSE, bar=_span(bar), close=plain(bar.close),
                       said=f"The last completed 5-minute bar ({_span(bar)}) closed at "
                            f"{plain(bar.close)}")
    else:
        quote = app_quote(item, now=retrieved_at)
        if quote is None:
            return no_answer(NO_RECENT_BAR, f"{why}; and the app's own bid in the item is "
                                            "missing or more than 15 minutes old")
        value, at = quote
        word = "the app's bid"
        numbers.update(source=APP_BID, bar=None, close=plain(value),
                       said=f"The app's bid at {at.astimezone(UTC):%H:%M:%S} UTC was "
                            f"{plain(value)} (no completed 5-minute bar in the last 15 "
                            "minutes)")
        bar = None
    if item.kind == EXIT_FLAG:
        decision, reason, half_risk, line = flag_rule(entry, stop, value)
        numbers.update(half_risk=plain(half_risk), line=plain(line))
        levels = f"entry {numbers['entry']}, stop {numbers['stop']}"
        if reason == AT_OR_BELOW_STOP:
            detail = f"{word} {numbers['close']} at or below the stop ({levels})"
        elif reason == AT_OR_BELOW_HALF_RISK:
            detail = (f"{word} {numbers['close']} at or below the half-risk line "
                      f"{numbers['half_risk']} ({levels})")
        else:
            detail = (f"{word} {numbers['close']} above the exit line {numbers['line']} "
                      f"({levels}, half-risk line {numbers['half_risk']})")
    else:
        decision, reason = review_rule(entry, value)
        numbers["period"] = _period(item.window_seconds)
        relation = "at or above" if decision == CONTINUE else "below"
        detail = f"{word} {numbers['close']} {relation} the entry {numbers['entry']}"
    return Decision(decision, reason, detail, bar, numbers)


def texts(item, decision):
    """``what_changed``, ``next_24h`` and ``proves_wrong``: facts from the numbers read."""
    n = decision.numbers
    bar = n["said"]
    if item.kind == EXIT_FLAG:
        halfway = (f"{n['half_risk']}, halfway between the entry {n['entry']} and the stop "
                   f"{n['stop']}")
        if decision.reason == AT_OR_BELOW_STOP:
            return {
                "what_changed": f"{bar}, at or below the stop {n['stop']} (entry {n['entry']}).",
                "next_24h": "The trade is at its stop "
                            + ("on a 5-minute close" if n["source"] == COINBASE_CLOSE
                               else "by the app's own bid")
                            + ": exit now rather than wait for the stop order.",
                "proves_wrong": f"A 5-minute close back above {n['line']}.",
            }
        if decision.reason == AT_OR_BELOW_HALF_RISK:
            return {
                "what_changed": f"{bar}, at or below {halfway}: the trade has given back half "
                                "of its planned risk.",
                "next_24h": f"The setup has failed by this rule: exit now near {n['close']} "
                            f"rather than wait for the stop at {n['stop']}.",
                "proves_wrong": f"A 5-minute close back above {n['half_risk']}.",
            }
        above = (f"above {halfway}" if n["line"] == n["half_risk"]
                 else f"above the stop {n['stop']} (entry {n['entry']})")
        keeps = f"its stop and its target {n['target']}" if n["target"] else "its stop"
        return {
            "what_changed": f"{bar}, {above}.",
            "next_24h": f"The stop {n['stop']} decides: the trade keeps {keeps}.",
            "proves_wrong": f"A 5-minute close at or below {n['line']}.",
        }
    if decision.decision == CONTINUE:
        plan = f"It continues for {n['period']}"
        if n["target"]:
            plan += f" toward the target {n['target']}"
        if n["stop"]:
            plan += f"; the stop {n['stop']} decides if the price falls"
        return {
            "what_changed": f"{bar}, at or above the entry {n['entry']}: the trade is working.",
            "next_24h": plan + ".",
            "proves_wrong": f"A 5-minute close below the entry {n['entry']}.",
        }
    return {
        "what_changed": f"{bar}, below the entry {n['entry']}: the trade did not work within "
                        "its window.",
        "next_24h": f"A time stop: exit now rather than hold the trade for {n['period']}.",
        "proves_wrong": f"A 5-minute close back at or above the entry {n['entry']}.",
    }


def answer_body(item, decision):
    """The ``AGENT_REVIEW_ANSWER_V1``: exactly ``schema_version``, ``answer_id``, ``decision``
    and the three texts. Never suggested levels, sources or a confidence."""
    return {"schema_version": AGENT_REVIEW_ANSWER_VERSION, "answer_id": item.answer_id,
            "decision": decision.decision, **texts(item, decision)}


def body_problems(body, *, agent_id, now):
    """What the app's own validator refuses in ``body`` (``validate_review_answer``, with
    suggested levels not allowed, as for a flag, since none is ever sent), and any key but the
    six this policy sends."""
    problems = []
    if set(body) != BODY_KEYS:
        problems.append(f"answer: keys must be exactly {sorted(BODY_KEYS)}")
    try:
        validate_review_answer(body, agent_id=agent_id, now=now, suggestions_allowed=False)
    except ValueError as exc:
        problems.append(f"answer: {exc} (the app's own answer validator)")
    return problems


def read_candles(coin, *, now=None, fetch=None):
    """The last hour of Coinbase 5-minute candles for ``coin`` (``market.fetch_candles``);
    ``now`` (``--now``) fixes their ``retrieved_at``."""
    at = now or datetime.now(UTC)
    return (fetch or market.fetch_candles)(coin, seconds=BAR_SECONDS, start=at - CANDLE_SPAN,
                                           end=at, now=now)


# --- The records ------------------------------------------------------------------------------

def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def record_path(run_dir, item):
    return run_dir / ITEMS_DIR / f"{item.key}.json"


def load_record(path):
    """An item's record, or None when there is none yet; ``RecordUnreadable`` otherwise."""
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RecordUnreadable(f"ANSWER_RECORD_UNREADABLE: {path.name}") from None
    if not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA or (
            "state" not in record):
        raise RecordUnreadable(f"ANSWER_RECORD_UNREADABLE: {path.name}")
    return record


def save_record(path, record):
    """Written whole and renamed into place, so a record is never half written: a fixed body is
    on disk before its POST starts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(record, indent=2, default=_json_default) + "\n",
                       encoding="utf-8")
    os.replace(partial, path)


def new_record(item, *, agent_id, agent_version, now):
    return {"schema": RECORD_SCHEMA, "rules": RULES_VERSION, "kind": item.kind,
            "item_id": item.item_id, "round": item.round, "symbol": item.symbol,
            "setup_id": item.setup_id, "answer_due_at": item.answer_due_at,
            "answer_id": item.answer_id, "agent_id": agent_id, "agent_version": agent_version,
            "first_seen_at": now.isoformat(), "item": item.raw, "state": None,
            "evaluations": [], "decision": None, "body": None, "attempts": []}


def bar_json(bar):
    return {**market.bar_to_json(bar), "ended_at": bar_end(bar).isoformat()}


def evaluation_json(now, decision, candles, error, body=None):
    """One reading of an item: the candles read (or why none), the bar used, the decision."""
    market_doc = ({key: candles[key] for key in ("product", "granularity", "retrieved_at",
                                                 "start", "end", "candles")}
                  if candles is not None else {"error": error})
    return {"at": now.isoformat(), "market": market_doc,
            "bar": bar_json(decision.bar) if decision.bar is not None else None,
            "decision": decision.decision, "reason": decision.reason, "detail": decision.detail,
            "numbers": decision.numbers, "body": body}


def send_state(status_code, detail):
    """The record's state after an answer to a POST (MUSE-CONNECTION.md 4f and 7)."""
    if status_code == 200:
        return RECORDED
    if status_code == 409:
        # Answered, resolved or window closed: final. Only "not yet asked" can take it later.
        return RETRY if detail == "EXIT_FLAG_NOT_YET_ASKED" else CLOSED
    if status_code == 404:
        return CLOSED  # The review or flag is gone (its trade closed).
    if status_code in (401, 503):
        return RETRY  # Nothing stored: the same body again.
    if 400 <= status_code < 500:
        return REFUSED
    return UNKNOWN  # A 5xx: whether it was recorded is unknown; the same body goes again.


# --- One run ----------------------------------------------------------------------------------

@dataclass
class Outcome:
    """What this run did with one pending item, for the printout and the poll line."""

    raw: dict
    item: Item | None
    action: str  # ANSWERED, RESENT, NO_ANSWER, DRY_RUN, WOULD_RESEND, ALREADY_DONE, ...
    state: str | None
    decision: str | None = None
    reason: str | None = None
    detail: str | None = None
    status_code: int | None = None
    response_detail: str | None = None
    replay: bool | None = None

    def json(self):
        raw = self.raw if isinstance(self.raw, dict) else {}
        return {"kind": self.item.kind if self.item else raw.get("kind"),
                "id": self.item.item_id if self.item else (raw.get("flag_id")
                                                           or raw.get("review_id")),
                "round": self.item.round if self.item else raw.get("round"),
                "symbol": self.item.symbol if self.item else raw.get("symbol"),
                "action": self.action, "state": self.state, "decision": self.decision,
                "reason": self.reason, "status_code": self.status_code,
                "response_detail": self.response_detail, "idempotent_replay": self.replay}

    def line(self):
        label = self.item.label() if self.item else f"item {self.json()['id'] or '?'}"
        if self.action == UNREADABLE:
            return f"answer: {label}: not answered: {self.reason}"
        if self.action == RECORD_UNREADABLE:
            return f"answer: {label}: left alone: {self.reason}"
        if self.action == "NO_ANSWER":
            return (f"answer: {label}: no answer: {self.reason} ({self.detail}); the app's "
                    "fallback applies")
        if self.action == "ALREADY_DONE":
            return f"answer: {label}: already {self.state}; nothing sent"
        if self.action == "WOULD_RESEND":
            return f"answer: {label}: dry run: would resend the recorded {self.decision}"
        decided = f"{self.decision} ({self.reason}: {self.detail})" if self.detail else (
            f"{self.decision}")
        if self.action == "DRY_RUN":
            return f"answer: {label}: {decided}; dry run: written, not sent"
        if self.action == REFUSED_LOCALLY:
            return f"answer: {label}: {decided}; not sent: {self.response_detail}"
        sent = "resent" if self.action == "RESENT" else "sent"
        if self.status_code is None:
            return (f"answer: {label}: {decided}; {sent}, no answer ({self.response_detail}); "
                    "the next run resends the same body")
        return (f"answer: {label}: {decided}; {sent}: HTTP {self.status_code} "
                f"{self.response_detail or ''}"
                + (" (a replay: already recorded)" if self.replay else "")
                + f" -> {self.state}")


def _send(record, path, item, send, now, *, resend):
    """One POST of the record's fixed body; every outcome is written to the record."""
    attempt = {"at": now.isoformat(), "kind": "RESEND" if resend else "SEND",
               "answer_id": record["body"]["answer_id"]}
    outcome = Outcome(item.raw, item, "RESENT" if resend else "ANSWERED", None,
                      decision=record["body"]["decision"],
                      reason=(record.get("decision") or {}).get("reason"),
                      detail=(record.get("decision") or {}).get("detail"))
    try:
        result = send(item.kind, item.item_id, record["body"])
    except SubmitError as exc:
        attempt.update(error=str(exc), state=UNKNOWN)
        outcome.response_detail = str(exc)
    else:
        answer = result.body if isinstance(result.body, dict) else None
        detail = answer.get("detail") if answer else None
        attempt.update(status_code=result.status_code, body=answer,
                       text=None if answer is not None else (result.raw_text or "")[:500],
                       detail=detail, state=send_state(result.status_code, detail))
        outcome.status_code = result.status_code
        outcome.response_detail = detail or (answer or {}).get("status")
        outcome.replay = (answer or {}).get("idempotent_replay")
    record["attempts"].append(attempt)
    record["state"] = outcome.state = attempt["state"]
    save_record(path, record)
    return outcome


def answer_pending(pending, *, run_dir, agent_id, agent_version, now, dry_run, fetch, send):
    """Every pending item decided, written and (unless ``dry_run``) sent; returns an ``Outcome``
    per item. ``fetch(coin)`` returns ``market.fetch_candles``'s result (read once per coin per
    run, only for an item that is decided now); ``send(kind, item_id, body)`` returns a
    ``submit.SubmitResult``. An item already recorded, answered or closed is never sent again;
    one sent without an answer is resent with its recorded body, never decided again."""
    outcomes, candles = [], {}
    for raw in pending.get("items") or []:
        try:
            item = parse_item(raw)
        except ItemUnreadable as exc:
            outcomes.append(Outcome(raw, None, UNREADABLE, None, reason=str(exc)))
            continue
        path = record_path(run_dir, item)
        try:
            record = load_record(path)
        except RecordUnreadable as exc:
            outcomes.append(Outcome(raw, item, RECORD_UNREADABLE, None, reason=str(exc)))
            continue
        state = (record or {}).get("state")
        if state in FINAL_STATES:
            outcomes.append(Outcome(raw, item, "ALREADY_DONE", state,
                                    decision=(record.get("body") or {}).get("decision")))
            continue
        if state in RESEND_STATES:
            if dry_run:
                outcomes.append(Outcome(raw, item, "WOULD_RESEND", state,
                                        decision=record["body"]["decision"]))
            else:
                record["last_seen_at"] = now.isoformat()
                outcomes.append(_send(record, path, item, send, now, resend=True))
            continue
        # New, or not yet fixed (NO_DATA, DRY_RUN, REFUSED_LOCALLY): decided now.
        record = record or new_record(item, agent_id=agent_id, agent_version=agent_version,
                                      now=now)
        record["last_seen_at"] = now.isoformat()
        missing = missing_levels(item)
        coin_doc = error = None
        if missing:
            decision = no_answer(LEVEL_MISSING, "the item states no usable "
                                 + " or ".join(missing))
        elif item.coin is None:
            decision = no_answer(CANDLES_UNAVAILABLE, f"no Coinbase USD market for "
                                 f"{item.symbol or 'this item'}")
        else:
            if item.coin not in candles:
                try:
                    candles[item.coin] = (fetch(item.coin), None)
                except market.MarketDataError as exc:
                    candles[item.coin] = (None, str(exc))
            coin_doc, error = candles[item.coin]
            decision = (decide(item, coin_doc) if coin_doc is not None
                        else no_answer(CANDLES_UNAVAILABLE, error))
        if decision.decision is None:
            record["evaluations"].append(evaluation_json(now, decision, coin_doc, error))
            record["state"] = NO_DATA
            save_record(path, record)
            outcomes.append(Outcome(raw, item, "NO_ANSWER", NO_DATA, reason=decision.reason,
                                    detail=decision.detail))
            continue
        body = answer_body(item, decision)
        record["evaluations"].append(evaluation_json(now, decision, coin_doc, error, body))
        record["decision"] = {"decision": decision.decision, "reason": decision.reason,
                              "detail": decision.detail, "decided_at": now.isoformat(),
                              "bar": bar_json(decision.bar) if decision.bar is not None
                              else None, "numbers": decision.numbers}
        record["body"] = body
        problems = body_problems(body, agent_id=agent_id, now=now)
        if problems:
            record.update(state=REFUSED_LOCALLY, problems=problems)
            save_record(path, record)
            outcomes.append(Outcome(raw, item, REFUSED_LOCALLY, REFUSED_LOCALLY,
                                    decision=decision.decision, reason=decision.reason,
                                    detail=decision.detail,
                                    response_detail="; ".join(problems)))
            continue
        if dry_run:
            record["state"] = DRY_RUN
            save_record(path, record)
            outcomes.append(Outcome(raw, item, "DRY_RUN", DRY_RUN, decision=decision.decision,
                                    reason=decision.reason, detail=decision.detail))
            continue
        record["state"] = SENDING  # The body is fixed from here on, sent or not.
        save_record(path, record)
        outcomes.append(_send(record, path, item, send, now, resend=False))
    return outcomes


def exit_code(outcomes):
    """1 when an item was left unanswered for a reason other than no usable data (unreadable,
    refused, or sent without an answer), else 0."""
    return 1 if any(o.action in (UNREADABLE, RECORD_UNREADABLE) or o.state in FAILED_STATES
                    for o in outcomes) else 0


# --- The run log and the lock -----------------------------------------------------------------

def poll_line(*, now, pending, outcomes, dry_run, agent_id, agent_version, error=None):
    """One run's line in ``polls/<UTC day>.jsonl``: the items read and what was done."""
    items = (pending or {}).get("items") or []
    return {"schema": POLL_SCHEMA, "at": now.isoformat(), "rules": RULES_VERSION,
            "agent_id": agent_id, "agent_version": agent_version, "dry_run": dry_run,
            "as_of": (pending or {}).get("as_of"), "pending": len(items),
            "items": [outcome.json() for outcome in outcomes], "error": error}


def append_poll(run_dir, line, now):
    path = run_dir / POLLS_DIR / f"{now.astimezone(UTC).date().isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(line, separators=(",", ":"), default=_json_default) + "\n")
    return path


@contextmanager
def run_lock(run_dir):
    """Yields True while this run holds ``answer.lock`` in the run folder, or False at once when
    another run holds it (that run answers; this one does nothing). Released on exit."""
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(run_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
