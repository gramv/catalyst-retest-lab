"""The 24-hour review and early exits of maintained crypto trades (plan phase 6, day-review).

Plan ``docs/CRYPTO-AGENT-LOOP.md`` 4.6.3 and 4.6.4 with 4.6.5-4.6.8, owner-approved 2026-09-26.
Scope: open trades whose state records ``CRYPTO_24H_REVIEW_V1``, ``CRYPTO_24H_REVIEW_V2`` or
``CRYPTO_WINDOW_REVIEW_V1`` (``crypto_holding``; V2 from package answer-rules, the window version
from package review-window): report-V3 crypto setups admitted in the maintained ``JEV_MANAGED``
arm. The control arm (``CRYPTO_24H_HOLD_V1``, ``CRYPTO_WINDOW_HOLD_V1``) and every older setup
are never touched here. Rules and versions are in ``day_review``; what Jev reads and answers in
``day_review_dossier``. Every review version runs the same way on its setup's own recorded
numbers (the window version: T every window instead of every 24 hours); each Jev request
carries its setup's own review record as the context's ``policy`` and its answer is read by
that version's rule (``day_review.review_reader``: V1's reader or ``DAY_REVIEW_ANSWER_RULE_V2``).

Two parts share the append-only ledger (``lab.managed_events``, every event keyed and carrying
the trade's ``setup_id``), so a restart resumes from it and nothing is decided twice:

* ``DayReviews`` runs in the runtime's research loop about once a second (its own periodic
  task) over the open trades under the version. It asks the agent (``DAY_REVIEW_REQUESTED`` at
  T - 30 min), asks Jev at T and in the final round, opens the one discussion round, applies
  every timeout and records exactly one ``DAY_REVIEW_DECISION`` per review. For early exits it
  asks the other side at once, asks Jev about an agent's flag, and resolves every flag
  (``EXIT_FLAG_RESOLVED``) with one ``EARLY_EXIT_DECISION``. It never touches the broker: an
  exit is ``exit_requested`` (``DAY_REVIEW_EXIT`` or ``EARLY_EXIT_AGREED``) and a raised stop is
  ``stop_replace``; the protection loop then cancels, replaces or sells, each under its own
  exact one-use five-second authorization, and one exit order per revision never double-sells.
* ``TradeReviewService`` serves the agent routes: its pending requests, its answers and its own
  exit flags. An agent authenticates with its own token and sees and answers only requests
  addressed to it; answers are recorded for ``DayReviews`` to act on.

Hard exits always win (plan 4.6.5): a trade exiting (stop fallback, target, the daily halt,
operator flatten, a raised stop crossed) or closed discards its pending review
(``DISCARDED``) and ends its pending flags (``LIFECYCLE_ENDED``).
"""

import asyncio
import re
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import UUID, uuid4

from catalyst_lab import crypto_holding, exit_flags, trade_plan
from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import day_review as dr
from catalyst_lab.day_review_dossier import ANSWERED as AGENT_ANSWERED
from catalyst_lab.day_review_dossier import (
    EARLY_EXIT_QUESTIONS,
    NO_AGENT,
    NO_ANSWER,
    compile_day_review_state,
    compile_early_exit_state,
    meanings,
    review_questions_for,
)
from catalyst_lab.jev_contract import JEV_MODEL, digest, encoded, strict_json, validated_answers
from catalyst_lab.jev_review import ReviewResult
from catalyst_lab.maintenance_dossier import brief
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable
from catalyst_lab.managed_review import MANAGED_COHORT, ManagedContext
from catalyst_lab.position_monitor import MANAGEMENT_REVIEWS_ENABLED, MANAGEMENT_REVIEWS_SETTINGS
from catalyst_lab.repository import json_safe
from catalyst_lab.system_check import LivePriceReader, LivePriceUnavailable
from catalyst_lab.trade_maintenance import TradeMaintenance, v4_guards

PURPOSE = "ENGINEERING_TEST"  # The position reviews' record purpose (PositionMonitor's).
_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
POLL_HINT_SECONDS = 60
RECOVERY_MARGIN_SECONDS = 5
HARD_EXIT_MARGIN_SECONDS = 60  # A continue is never applied within a minute of the fail-safe.
DECISION_REST_READS_PER_PASS = 10  # A decision's own REST quote reads per pass (every trade's).
_OPEN_REVIEW_SETUPS = """SELECT s.setup_id,s.symbol,s.market,s.strategy_version,s.record_json,
s.cycle_id,s.revision,t.body AS state FROM lab.managed_states t JOIN lab.managed_setups s
USING(setup_id) WHERE t.body->>'state'='OPEN'
AND t.body->'holding_policy'->>'policy_id'=ANY(%s) ORDER BY s.event_seq"""
_UNFINISHED = """SELECT e.setup_id,e.kind,e.body,t.body AS state FROM lab.managed_events e
JOIN lab.managed_states t ON t.setup_id=e.setup_id
WHERE e.kind IN (%(requested)s,%(raised)s)
AND t.body->'holding_policy'->>'policy_id'=ANY(%(policies)s)
AND (%(ids)s::uuid[] IS NULL OR e.setup_id=ANY(%(ids)s::uuid[]))
AND (t.body->>'state'<>'OPEN' OR t.body->>'lifecycle_id'<>e.body->>'lifecycle_id')
AND NOT EXISTS(SELECT 1 FROM lab.managed_events d WHERE d.setup_id=e.setup_id
  AND ((e.kind=%(requested)s AND d.kind=%(decision)s AND d.body->>'review_id'=e.body->>'review_id')
    OR (e.kind=%(raised)s AND d.kind=%(resolved)s AND d.body->>'flag_id'=e.body->>'flag_id')))
ORDER BY e.event_seq"""


class ReviewNotFound(LookupError):
    """No open review or flag with this ID (HTTP 404)."""


class ReviewAccessDenied(PermissionError):
    """The caller is not the agent the request is addressed to (HTTP 403)."""


class ReviewConflict(ValueError):
    """The request exists but cannot take this answer now (HTTP 409); the code says why."""


def _aware(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return value


def _text(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _num(value):
    return cm.number(value)


def _uuids(values):
    return [UUID(str(value)) for value in values]


def error_code(exc):
    """The exception's own UPPER_SNAKE code, else its class name; free text is never kept."""
    text = str(exc)
    return text if len(text) <= 80 and _CODE.fullmatch(text) else type(exc).__name__


# --- The ledger view of a trade's reviews and flags ----------------------------------------------


class ReviewView:
    """One 24-hour review's events, oldest first."""

    def __init__(self, review_id):
        self.review_id = review_id
        self.request = self.discussion = self.decision = None
        self.answers, self.requests, self.results = {}, [], []

    def add(self, row):
        kind, body = row["kind"], row["body"]
        if kind == dr.REQUESTED:
            self.request = row
        elif kind == dr.AGENT_ANSWER:
            self.answers.setdefault(body["round"], row)
        elif kind == dr.JEV_REQUEST:
            self.requests.append(row)
        elif kind == dr.JEV_RESULT:
            self.results.append(row)
        elif kind == dr.DISCUSSION_OPENED:
            self.discussion = row
        elif kind == dr.DECISION:
            self.decision = row

    def result(self, rnd):
        """Jev's latest answer (usable or not) of a round, or None."""
        found = [r for r in self.results if r["body"]["round"] == rnd
                 and r["body"]["status"] in {dr.ANSWERED, dr.UNUSABLE}]
        return found[-1] if found else None

    def last_failure(self, rnd):
        found = [r for r in self.results if r["body"]["round"] == rnd
                 and r["body"]["status"] == dr.FAILED]
        return found[-1] if found else None

    def pending_request(self, rnd):
        done = {r["body"].get("request_id") for r in self.results}
        found = [r for r in self.requests if r["body"]["round"] == rnd
                 and r["body"]["request_id"] not in done]
        return found[-1] if found else None

    def attempts(self, rnd):
        return sum(1 for r in self.requests if r["body"]["round"] == rnd) + sum(
            1 for r in self.results if r["body"]["round"] == rnd and not r["body"].get(
                "request_id"))

    def request_for(self, request_id):
        return next((r for r in self.requests if r["body"]["request_id"] == request_id), None)


class FlagView(ReviewView):
    """One early-exit flag's events: the flag, the ask, the answers and the resolution."""

    def __init__(self, flag_id):
        super().__init__(flag_id)
        self.flag_id = flag_id
        self.raised = self.asked = self.agent_answer = self.resolution = None

    def add(self, row):
        kind = row["kind"]
        if kind == dr.FLAG_RAISED:
            self.raised = row
        elif kind == dr.FLAG_ASKED:
            self.asked = row
        elif kind == dr.FLAG_AGENT_ANSWER:
            self.agent_answer = row
        elif kind == dr.FLAG_JEV_REQUEST:
            self.requests.append(row)
        elif kind == dr.FLAG_JEV_RESULT:
            self.results.append(row)
        elif kind == dr.FLAG_RESOLVED:
            self.resolution = row
        elif kind == dr.EARLY_EXIT_DECISION:
            self.decision = row

    @property
    def side(self):
        return self.raised["body"]["side"]


def load_views(conn, setup_ids):
    """``(reviews, flags)`` of the given setups: ``{review_id: ReviewView}``, ``{flag_id:
    FlagView}``. One indexed read (setup and kind)."""
    reviews, flags = {}, {}
    if not setup_ids:
        return reviews, flags
    rows = conn.execute(
        """SELECT event_seq,setup_id,kind,body,recorded_at FROM lab.managed_events
        WHERE setup_id=ANY(%s) AND kind=ANY(%s) ORDER BY event_seq""",
        (_uuids(setup_ids), list(dr.REVIEW_KINDS + dr.FLAG_KINDS)),
    ).fetchall()
    for row in rows:
        body = row["body"]
        if row["kind"] in dr.REVIEW_KINDS:
            reviews.setdefault(body["review_id"], ReviewView(body["review_id"])).add(row)
        else:
            flags.setdefault(body["flag_id"], FlagView(body["flag_id"])).add(row)
    return reviews, flags


def open_review_setups(conn):
    """Open trades under a review version (V1, V2 from package answer-rules, or the window
    version from package review-window)."""
    return conn.execute(_OPEN_REVIEW_SETUPS, (list(crypto_holding.REVIEW_POLICY_IDS),)).fetchall()


def review_policy(state):
    """The setup's recorded review version (``CRYPTO_24H_REVIEW_V1``, ``_V2`` or
    ``CRYPTO_WINDOW_REVIEW_V1``): the numbers that time its reviews."""
    hold = crypto_holding.recorded_hold_policy(state)
    if not isinstance(hold, crypto_holding.CryptoReviewPolicy):
        raise ValueError("CRYPTO_REVIEW_POLICY_REQUIRED")
    return hold


def review_policy_id(state):
    """The recorded review ``policy_id`` as written (for notices; never validated)."""
    recorded = (state or {}).get("holding_policy")
    return recorded.get("policy_id") if isinstance(recorded, dict) else None


def current_review_id(setup):
    state = setup["state"]
    return dr.review_id_for(setup["setup_id"], state["lifecycle_id"],
                            crypto_holding.continuations(state) + 1)


def addressee_for(conn, setup, agents):
    """``(agent_id, basis)`` who answers for a trade (plan 4.6.7): the proposing agent while its
    credential is configured, else the configured research agent whose report arrived last,
    else ``(None, NO_ACTIVE_AGENT)`` (Jev then decides alone). ``agents`` None: the configured
    set is unknown and the proposing agent answers."""
    proposing = ((setup["record_json"] or {}).get("agent") or {}).get("agent_id")
    if agents is None:
        return (proposing, dr.PROPOSING_AGENT) if proposing else (None, dr.NO_ACTIVE_AGENT)
    if proposing in agents:
        return proposing, dr.PROPOSING_AGENT
    row = conn.execute(
        """SELECT body->'agent'->>'agent_id' AS agent_id FROM lab.managed_events
        WHERE kind='RESEARCH_STARTED' AND body->'agent'->>'agent_id'=ANY(%s)
        ORDER BY event_seq DESC LIMIT 1""", (sorted(agents),),
    ).fetchone()
    if row is not None:
        return row["agent_id"], dr.ACTIVE_RESEARCH_AGENT
    return None, dr.NO_ACTIVE_AGENT


def selection_judgment(store, receipt_id):
    """The receipt-verified answers of one selection review, or None (maintenance's rule)."""
    from catalyst_lab.jev_contract import QuestionSet

    if not receipt_id:
        return None
    if not store.verify(receipt_id)["valid"]:
        raise ValueError("SELECTION_RECEIPT_INTEGRITY_FAILED")
    with store.connect() as conn:
        row = conn.execute(
            """SELECT r.response_bytes,r.outcome,q.question_set_version,q.stage,q.request_json
            FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
            WHERE r.receipt_id=%s""", (receipt_id,),
        ).fetchone()
    if not row or row["outcome"] != "VALID":
        return None
    template = QuestionSet(row["question_set_version"], row["stage"],
                           encoded(strict_json(row["request_json"])["questions"]))
    return {"receipt_id": str(receipt_id), "question_set_version": row["question_set_version"],
            "answers": validated_answers(bytes(row["response_bytes"]), template)}


def verify_receipts(store, context, result, questions, *, expected_request_id):
    """Exact context, question set, request and answers, as maintenance's V4 verification."""
    try:
        if result.request_id != expected_request_id or any(
            not store.verify(receipt)["valid"] for receipt in result.receipt_ids
        ):
            raise ValueError("RECEIPT_INTEGRITY_FAILED")
        with store.connect() as conn:
            for receipt_id in result.receipt_ids:
                row = conn.execute(
                    """SELECT r.request_id,r.outcome,r.response_bytes,q.evidence_identity,
                    q.request_json,q.deadline FROM lab.jev_receipts r
                    JOIN lab.jev_requests q USING(request_id) WHERE r.receipt_id=%s""",
                    (receipt_id,),
                ).fetchone()
                if (
                    row is None or str(row["request_id"]) != expected_request_id
                    or any(row["evidence_identity"].get(k) != v
                           for k, v in context.identity.items())
                    or row["deadline"] != context.expires_at
                    or strict_json(row["request_json"]) != {
                        "model": JEV_MODEL, "state": context.state,
                        "questions": questions.questions,
                    }
                ):
                    raise ValueError("RECEIPT_BINDING_MISMATCH")
                if receipt_id == result.receipt_ids[-1] and result.answers:
                    if row["outcome"] != "VALID" or validated_answers(
                        bytes(row["response_bytes"]), questions
                    ) != result.answers:
                        raise ValueError("RECEIPT_ANSWER_MISMATCH")
    except Exception:
        return ReviewResult(expected_request_id, "NEEDS_REVIEW", "RECEIPT_INTEGRITY_FAILED",
                            tuple(result.receipt_ids), {})
    return result


def recover_receipt(store, request_id, questions):
    """The recorded answer of a request asked before a restart (never a second vote)."""
    with store.connect() as conn:
        receipt = conn.execute(
            """SELECT * FROM lab.jev_receipts WHERE request_id=%s
            ORDER BY attempt DESC LIMIT 1""", (request_id,),
        ).fetchone()
    if not receipt:
        return None
    store.verify(receipt["receipt_id"])
    answers = (validated_answers(bytes(receipt["response_bytes"]), questions)
               if receipt["outcome"] == "VALID" else {})
    return ReviewResult(str(request_id), "RECORDED" if answers else "NEEDS_REVIEW",
                        None if answers else receipt["error_code"],
                        (str(receipt["receipt_id"]),), answers)


# --- The orchestrator ----------------------------------------------------------------------------


class DayReviews:
    """The continue-or-exit reviews of ``CRYPTO_24H_REVIEW_V1``/``_V2`` (every 24 hours) and
    ``CRYPTO_WINDOW_REVIEW_V1`` (every window), and ``EARLY_EXIT_AGREEMENT_V1``'s early exits.

    ``agents``: the agent IDs whose credentials are configured (the app sets it at start; None
    when unknown, then the proposing agent is always asked). ``management_reviews`` is the
    staging switch: DISABLED asks Jev nothing, so a review exits at T and an agent's flag ends
    with no answer (the trade stays).
    """

    def __init__(self, execution, reviewer, *, bars, clock, management_reviews, agents=None,
                 live_prices=None):
        if management_reviews not in MANAGEMENT_REVIEWS_SETTINGS:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_REQUIRED")
        execution.repo.require_same_database(reviewer.store)
        self.execution, self.reviewer, self.bars, self.now = execution, reviewer, bars, clock
        self.management_reviews = management_reviews
        self.agents = frozenset(agents) if agents is not None else None
        self.live_prices = live_prices or LivePriceReader(bars, clock=clock)
        # A decision or an early-exit resolution reads its quote through its own reader (as in
        # trade_maintenance, 2026-09-28): a slow answer never finds the pass's one REST read
        # spent on its own request. Freshness rules are the reader's, unchanged.
        self.decision_prices = LivePriceReader(self.live_prices.source, clock=clock,
                                               rest_reads_per_tick=DECISION_REST_READS_PER_PASS)
        # The review's numbers come from each setup's own recorded version (``review_policy``):
        # V2 has V1's exactly, and CRYPTO_WINDOW_REVIEW_V1 differs only in its interval (the
        # window the setup recorded at admission). The version also names its records and reads
        # its answers.
        self.flag_policy = dr.EARLY_EXIT_AGREEMENT
        self._bar_cache = {}
        self._inflight = set()
        self._seen = {}  # setup -> [low, high] bids observed while a Jev answer is awaited
        self._noted = set()
        self._watched = set()
        self._swept = False
        self.last_pass_at = None

    @property
    def store(self):
        return self.execution.store

    @property
    def reviews_enabled(self):
        return self.management_reviews == MANAGEMENT_REVIEWS_ENABLED

    # --- the pass ------------------------------------------------------------------------

    async def run_pass(self, setups, observe):
        """One pass over the open trades under the version (``setups``: active rows)."""
        now = self.now()
        self.last_pass_at = now
        self.live_prices.new_tick()
        self.decision_prices.new_tick()
        reviewed = [s for s in setups if (s.get("state") or {}).get("state") == "OPEN"
                    and crypto_holding.review_active(s.get("state"))]
        self._sweep({s["setup_id"] for s in reviewed})
        jobs = []
        for setup in reviewed:
            row = observe(setup)
            self._observe(setup, row)
            try:
                jobs += [(setup, job) for job in await self._advance(setup, row, now)]
            except Exception as exc:  # One trade's fault never stops another's review.
                self._unavailable(setup, "DAY_REVIEW_PASS_FAILED", type(exc).__name__)
        results = await asyncio.gather(*(job() for _, job in jobs), return_exceptions=True)
        for (setup, _), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception):
                self._unavailable(setup, "DAY_REVIEW_JEV_FAILED", type(result).__name__)
            try:  # Decide at once on the answer just recorded (no new Jev call here).
                current = self._reload(setup)
                if current is not None:
                    await self._advance(current, observe(current), self.now(), schedule=False)
            except Exception as exc:
                self._unavailable(setup, "DAY_REVIEW_PASS_FAILED", type(exc).__name__)
        return len(jobs)

    def _reload(self, setup):
        with self.store.repo.connect() as conn:
            state = self.store.state(conn, setup["setup_id"])
        if state.get("state") != "OPEN" or not crypto_holding.review_active(state):
            return None
        return {**setup, "state": state}

    def _observe(self, setup, row):
        seen = self._seen.get(str(setup["setup_id"]))
        if seen is not None and row and row.get("bid"):
            bid = _num(row["bid"])
            seen[0] = bid if seen[0] is None else min(seen[0], bid)
            seen[1] = bid if seen[1] is None else max(seen[1], bid)

    async def _advance(self, setup, row, now, schedule=True):
        with self.store.repo.connect() as conn:
            reviews, flags = load_views(conn, [setup["setup_id"]])
        jobs = await self._review(setup, row, now, reviews, schedule)
        jobs += await self._flags(setup, row, now, flags, schedule)
        return jobs

    def _unavailable(self, setup, reason, code):
        lifecycle = (setup.get("state") or {}).get("lifecycle_id")
        key = f"day-review-unavailable:{setup['setup_id']}:{lifecycle}:{reason}:{code}"
        if key in self._noted:
            return
        with self.store.transaction() as conn:
            if not conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                                (key,)).fetchone():
                self.store.event(conn, "DAY_REVIEW_UNAVAILABLE", {
                    "reason": reason, "code": code, "lifecycle_id": lifecycle,
                    "policy_id": review_policy_id(setup.get("state")),
                }, setup_id=setup["setup_id"], key=key)
        self._noted.add(key)

    # --- quotes, bars and the trade now ----------------------------------------------------

    def _quote(self, symbol, row, reader=None):
        received = (row or {}).get("quote_received_at")
        received = _aware(received) if isinstance(received, str) else received
        try:
            return (reader or self.live_prices).read(symbol, row, by_read_time=True,
                                                     received_at=received)
        except LivePriceUnavailable:
            return None

    async def _bars(self, symbol, now):
        sets = {}
        for name, sym, timeframe, count in (
            ("bars_15m", symbol, "15Min", cm.BARS_15M), ("bars_1h", symbol, "1Hour", cm.BARS_1H),
            ("btc_15m", cm.BENCHMARK_SYMBOL, "15Min", cm.BARS_15M),
            ("btc_1h", cm.BENCHMARK_SYMBOL, "1Hour", cm.BARS_1H),
        ):
            boundary = cm.floor_time(now, cm.TIMEFRAMES[timeframe])
            cached = self._bar_cache.get((sym, timeframe))
            if cached and cached[0] == boundary:
                sets[name] = cached[1]
                continue
            bars, issues = await asyncio.to_thread(
                self.bars.timeframe_bars, "CRYPTO", sym, timeframe=timeframe, count=count)
            if issues and name.startswith("bars"):
                return None
            if not issues:
                self._bar_cache[(sym, timeframe)] = (boundary, tuple(bars))
            sets[name] = tuple(bars) if not issues else ()
        return sets

    def _bid_extremes(self, setup_id, lifecycle, since, current):
        """The lowest and highest bids since ``since``: the protection loop's per-second
        position samples, what this process observed meanwhile and the current bid."""
        with self.store.repo.connect() as conn:
            row = conn.execute(
                """SELECT min((body->>'bid')::numeric) AS low,max((body->>'bid')::numeric) AS high
                FROM lab.managed_events WHERE setup_id=%s AND kind='POSITION_MARKET_SNAPSHOT'
                AND body->>'lifecycle_id'=%s AND (body->>'received_at')::timestamptz>%s""",
                (setup_id, lifecycle, since),
            ).fetchone()
        values = [current] + [v for v in (row["low"], row["high"]) if v is not None]
        values += [v for v in self._seen.get(str(setup_id), [None, None]) if v is not None]
        return min(values), max(values)

    async def _gathered(self, setup, row, now, *, options, selection=True):
        """``_inputs`` whose failure is a code (``(None, code)``), never an exception: a Jev
        attempt that cannot be built is recorded as failed and retried after a minute."""
        try:
            return await self._inputs(setup, row, now, options=options, selection=selection)
        except Exception as exc:
            return None, error_code(exc)

    async def _inputs(self, setup, row, now, *, options, selection=True):
        """Everything a context needs: ``(inputs, None)``, or ``(None, code)``."""
        sid, state = setup["setup_id"], setup["state"]
        lifecycle = state["lifecycle_id"]
        quote = self._quote(setup["symbol"], row)
        if quote is None:
            return None, "CURRENT_QUOTE_UNAVAILABLE"
        bars = await self._bars(setup["symbol"], now)
        if bars is None:
            return None, "COMPLETED_BARS_UNAVAILABLE"
        increment = _num(await asyncio.to_thread(
            lambda: self.execution.broker.asset(setup["symbol"])["price_increment"]))
        packet = setup["record_json"]
        # CRYPTO_TRADE_PLAN_V1: R and the initial levels are the plan's (trade_plan.py).
        levels = trade_plan.initial_levels(
            {k: _num(v) for k, v in packet["levels"].items()}, state)
        risk = cm.r_per_coin(levels)
        with self.store.repo.connect() as conn:
            entry = TradeMaintenance._average_entry(conn, sid, state)
            extremes = conn.execute(
                """SELECT max((body->>'bid')::numeric) AS high,min((body->>'bid')::numeric) AS low
                FROM lab.managed_events WHERE setup_id=%s AND kind='POSITION_MARKET_SNAPSHOT'
                AND body->>'lifecycle_id'=%s""", (sid, lifecycle),
            ).fetchone()
            milestone = conn.execute(
                """SELECT max((body->>'level')::int) AS n FROM lab.managed_events
                WHERE setup_id=%s AND kind=%s AND body->>'lifecycle_id'=%s
                AND body->>'trigger'=%s""", (sid, cm.TRIGGER_EVENT, lifecycle, cm.R_MILESTONE),
            ).fetchone()["n"] or 0
            changes = TradeMaintenance._changes(conn, sid, lifecycle)
            news = TradeMaintenance._news_since_entry(conn, sid, lifecycle)
            history = TradeMaintenance.review_history(conn, sid, lifecycle)
        stop, target = _num(state["stop"]), _num(state["target"])
        best = max(v for v in (extremes["high"], quote.bid,
                               entry + risk * milestone if milestone else None) if v is not None)
        worst = min(v for v in (extremes["low"], quote.bid) if v is not None)
        # CRYPTO_MAINTENANCE_V4: the stop-raise guards (None under V1-V3: options unchanged).
        guards = cm.raise_guards(
            cm.recorded_policy(state), hourly_range=cm.hourly_range(bars["bars_1h"], now)[0],
            last_raise_at=cm.last_stop_raise(state), target_cap=trade_plan.target_cap(state))
        computed = None
        if options:
            computed = {
                "stop": [o.record() for o in cm.stop_options(
                    bars_15m=bars["bars_15m"], bars_1h=bars["bars_1h"], now=now, bid=quote.bid,
                    entry=entry, current_stop=stop, best_bid=best, risk=risk,
                    increment=increment, guards=guards)],
                "target": [o.record() for o in cm.target_options(
                    bars_15m=bars["bars_15m"], bars_1h=bars["bars_1h"], now=now, bid=quote.bid,
                    current_target=target, increment=increment,
                    target_cap=guards.target_cap if guards is not None else None)],
            }
        return {
            "raise_guards": {**guards.record(), "best_bid": best} if guards is not None else None,
            "quote": quote, "bars": bars, "increment": increment, "entry": entry, "risk": risk,
            "stop": stop, "target": target, "options": computed, "changes": changes,
            "news": news, "history": history, "pick": packet.get("state") or packet,
            "selection": {
                "pick_review": selection_judgment(self.reviewer.store, packet.get("receipt_id")),
                "quality": selection_judgment(self.reviewer.store,
                                              packet.get("quality_receipt_id")),
            } if selection else {},
            "trade": {
                "entry": entry, "qty": _num(state.get("qty") or "0"),
                "initial_stop": levels["stop"], "initial_target": levels["target"],
                "max_entry": levels["max_entry_price"], "risk": risk, "stop": stop,
                "target": target, "bid": quote.bid, "ask": quote.ask, "quote_at": quote.read_at,
                "opened_at": state["opened_at"], "review_at": state.get("day_review_at"),
                "best_bid": best, "worst_bid": worst, "milestone": milestone,
            },
        }, None

    # --- the continue-or-exit review (every 24 hours, or every window) -----------------------

    async def _review(self, setup, row, now, reviews, schedule):
        state = setup["state"]
        if not state.get("day_review_at"):
            return []  # Not yet recorded as open.
        hold = review_policy(state)
        review_at = _aware(state["day_review_at"])
        number = crypto_holding.continuations(state) + 1
        review_id = dr.review_id_for(setup["setup_id"], state["lifecycle_id"], number)
        view = reviews.get(review_id) or ReviewView(review_id)
        if view.decision is not None:
            return []
        if state.get("exit_requested"):
            if view.request is not None:  # A hard exit discards the pending review.
                self._decide(setup, row, now, view, dr.DISCARDED,
                             dr.EXIT_IN_PROGRESS_DURING_REVIEW)
            return []
        if now < review_at - timedelta(seconds=hold.request_lead_seconds):
            return []
        if view.request is None:
            await self._request(setup, row, now, review_id, number, review_at)
            return []
        if now < review_at:
            return []  # The agent answers until T.
        if not self.reviews_enabled:
            self._decide(setup, row, now, view, dr.EXIT, dr.JEV_REVIEWS_DISABLED)
            return []
        agent = view.answers.get(dr.FIRST)
        first = view.result(dr.FIRST)
        if first is None:
            return await self._jev_due(setup, row, now, view, dr.FIRST,
                                       review_at + timedelta(seconds=hold.jev_answer_seconds),
                                       schedule)
        outcome = dr.first_round(
            dr.answer_from_record(first["body"]["answer"]),
            agent["body"]["answer"]["decision"] if agent else None,
            addressee=view.request["body"]["addressee"]["agent_id"])
        if outcome is not None:
            self._decide(setup, row, now, view, *outcome, jev=first)
            return []
        if view.discussion is None:
            self._open_discussion(setup, view, first, agent, now)
            return []
        reply = view.answers.get(dr.DISCUSSION)
        if reply is None:
            if now >= _aware(view.discussion["body"]["reply_due_at"]):
                self._decide(setup, row, now, view, dr.EXIT, dr.DISCUSSION_NO_AGENT_REPLY,
                             jev=first)
            return []
        final = view.result(dr.FINAL)
        if final is None:
            window = _aware(reply["body"]["received_at"]) + timedelta(
                seconds=hold.jev_answer_seconds)
            return await self._jev_due(setup, row, now, view, dr.FINAL, window, schedule)
        outcome = dr.final_round(dr.answer_from_record(final["body"]["answer"]),
                                 reply["body"]["answer"]["decision"])
        self._decide(setup, row, now, view, *outcome, jev=final)
        return []

    async def _request(self, setup, row, now, review_id, number, review_at):
        """``DAY_REVIEW_REQUESTED``: what the agent answers from (plan 4.6.4 step 1)."""
        sid, state = setup["setup_id"], setup["state"]
        lifecycle = state["lifecycle_id"]
        with self.store.repo.connect() as conn:
            addressee, basis = addressee_for(conn, setup, self.agents)
            entry = TradeMaintenance._average_entry(conn, sid, state)
            changes = TradeMaintenance._changes(conn, sid, lifecycle)
            news = TradeMaintenance._news_since_entry(conn, sid, lifecycle)
        quote = self._quote(setup["symbol"], row)
        options = None
        if quote is not None:  # Informational for the agent; Jev's are computed again at T.
            inputs, _ = await self._gathered(setup, row, now, options=True, selection=False)
            options = inputs["options"] if inputs else None
        packet = setup["record_json"]
        pick = packet.get("state") or packet
        # CRYPTO_TRADE_PLAN_V1: the plan's levels (the research levels stay in original_pick).
        levels = trade_plan.initial_levels(packet["levels"], state)
        risk = cm.r_per_coin({k: _num(v) for k, v in levels.items()})
        qty = _num(state.get("qty") or "0")
        trade = {
            "entry": entry, "quantity": qty, "stop": state["stop"], "target": state["target"],
            "levels": levels, "risk_per_coin": risk, "opened_at": state["opened_at"],
            "hours_in_trade": brief(D((now - _aware(state["opened_at"])).total_seconds())
                                    / D(3600), 2),
            "bid": quote.bid if quote else None, "ask": quote.ask if quote else None,
            "quote_at": quote.read_at if quote else None,
            "unrealized_pnl_usd": (quote.bid - entry) * qty if quote else None,
            "pnl_r": brief(cm.in_r(quote.bid - entry, risk), 2) if quote else None,
        }
        hold = review_policy(state)
        body = {
            "policy_id": hold.policy_id, "review_id": review_id,
            # CRYPTO_WINDOW_REVIEW_V1 only: the setup's window, the time until the next review
            # after a continue (the 24-hour versions' requests are unchanged).
            **crypto_holding.window_fields(hold),
            "setup_id": str(sid), "symbol": setup["symbol"], "lifecycle_id": lifecycle,
            "signal_id": packet.get("signal_id"), "review_number": number,
            "continuations": number - 1, "review_at": review_at.isoformat(),
            "requested_at": now.isoformat(), "agent_answer_due_at": review_at.isoformat(),
            "addressee": {"agent_id": addressee, "basis": basis},
            "answer_schema": crypto_holding.AGENT_REVIEW_ANSWER_VERSION,
            "answer_route": f"/api/v1/lab/reviews/{review_id}/answer",
            "trade": trade, "level_changes": changes, "news_since_entry": news,
            "options": options,
            "original_pick": {key: pick.get(key) for key in (
                "kind", "agent_current_price", "agent_price_at", "levels", "stated_reward_risk",
                "thesis", "why_now", "why_these_levels", "risks", "disproof")},
        }
        with self.store.transaction() as conn:
            current = self.store.state(conn, sid)
            if (current.get("state") != "OPEN" or current.get("lifecycle_id") != lifecycle
                    or current.get("exit_requested")
                    or crypto_holding.continuations(current) != number - 1):
                return
            if not conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                                ("day-review-requested:" + review_id,)).fetchone():
                self.store.event(conn, dr.REQUESTED, body, setup_id=sid,
                                 key="day-review-requested:" + review_id)

    def _open_discussion(self, setup, view, first, agent, now):
        """The one discussion round: the agent sees Jev's reasons and answers within 15 min."""
        due = now + timedelta(seconds=review_policy(setup["state"]).agent_reply_seconds)
        body = {
            "review_id": view.review_id, "setup_id": str(setup["setup_id"]),
            "lifecycle_id": view.request["body"]["lifecycle_id"], "opened_at": now.isoformat(),
            "reply_due_at": due.isoformat(),
            "agent_first_decision": agent["body"]["answer"]["decision"],
            "jev_first": {k: first["body"].get(k) for k in (
                "request_id", "receipt_ids", "answer", "meanings", "chosen", "answered_at")},
        }
        key = "day-review-discussion:" + view.review_id
        with self.store.transaction() as conn:
            if not conn.execute(
                """SELECT 1 FROM lab.managed_events WHERE idempotency_key IN (%s,%s)""",
                (key, "day-review-decision:" + view.review_id),
            ).fetchone():
                self.store.event(conn, dr.DISCUSSION_OPENED, body, setup_id=setup["setup_id"],
                                 key=key)

    async def _jev_due(self, setup, row, now, view, rnd, window_end, schedule):
        """Jev's answer for a round: in flight, recovered, timed out, retried or asked."""
        key = ("REVIEW", view.review_id, rnd)
        if key in self._inflight:
            return []
        pending = view.pending_request(rnd)
        if pending is not None:
            self._recover(setup, "REVIEW", view, pending, now)
            return []
        if now >= window_end:  # Jev unable to answer within 30 minutes: exit.
            self._decide(setup, row, now, view, dr.EXIT, dr.JEV_UNAVAILABLE,
                         jev=view.last_failure(rnd))
            return []
        failure = view.last_failure(rnd)
        if failure is not None and now < _aware(failure["body"]["recorded_at"]) + timedelta(
                seconds=review_policy(setup["state"]).jev_retry_seconds):
            return []
        if not schedule:
            return []
        prepared = await self._prepare_review(setup, row, now, view, rnd, window_end)
        if prepared is None:
            return []
        return [self._job(key, prepared)]

    def _job(self, key, prepared):
        self._inflight.add(key)
        self._seen[str(prepared["setup_id"])] = [None, None]

        async def job():
            try:
                await self._ask(prepared)
            finally:
                self._inflight.discard(key)

        return job

    async def _prepare_review(self, setup, row, now, view, rnd, window_end):
        """Build and record one Jev request of a review round; None when it cannot be built
        (a FAILED result is then recorded and retried after a minute inside the window)."""
        inputs, code = await self._gathered(setup, row, now, options=True)
        if inputs is None:
            self._failed("REVIEW", setup, view, rnd, code, now)
            return None
        request = view.request["body"]
        first_answer = view.answers.get(dr.FIRST)
        status = AGENT_ANSWERED if first_answer else (
            NO_ANSWER if request["addressee"]["agent_id"] else NO_AGENT)
        discussion = None
        if rnd == dr.FINAL:
            jev_first = view.result(dr.FIRST)["body"]
            discussion = {
                "your_first_answer": {**jev_first["answer"]["summary"],
                                      "meanings": jev_first.get("meanings")},
                "agent_reply": view.answers[dr.DISCUSSION]["body"]["answer"],
            }
            status = AGENT_ANSWERED
        review = {
            "round": rnd, "review_number": request["review_number"],
            "continuations": request["continuations"],
            "hours_in_trade": int((now - _aware(setup["state"]["opened_at"])).total_seconds()
                                  // 3600),
            "agent_answer_status": status,
        }
        trigger = {"reasons": ["DAY_REVIEW"], "review_number": request["review_number"],
                   "round": rnd}
        hold = review_policy(setup["state"])  # V1's context, or V2's with the review history.
        v2 = hold.context_version == crypto_holding.DAY_REVIEW_CONTEXT_V2_VERSION
        try:
            compiled = compile_day_review_state(
                review=review, agent_answer=first_answer["body"]["answer"] if first_answer
                else None, discussion=discussion, now=now, symbol=setup["symbol"],
                pick=inputs["pick"], selection=inputs["selection"], trade=inputs["trade"],
                changes=inputs["changes"], news=inputs["news"], options=inputs["options"],
                trigger=trigger, context_version=hold.context_version,
                history=inputs["history"] if v2 else None, **inputs["bars"])
        except ContextBudgetUnsatisfiable as exc:
            self._failed("REVIEW", setup, view, rnd, exc.code, now)
            return None
        return self._record_request("REVIEW", setup, view, rnd, now, window_end, compiled,
                                    inputs, extra={"agent_answer_seqs": sorted(
                                        r["event_seq"] for r in view.answers.values())})

    def _record_request(self, subject, setup, view, rnd, now, window_end, compiled, inputs,
                        extra):
        sid, state = setup["setup_id"], setup["state"]
        # A review request carries the setup's own review version (V1 or V2), whose rule reads
        # the answer; a flag request carries EARLY_EXIT_AGREEMENT_V1 (the same under both).
        hold = review_policy(state)
        policy = hold if subject == "REVIEW" else self.flag_policy
        expires = min(now + timedelta(seconds=policy.jev_call_deadline_seconds), window_end)
        request_id = str(uuid4())
        identity = {
            "candidate_id": str(sid), "position_id": str(sid),
            "lifecycle_id": state["lifecycle_id"], "subject": subject,
            "subject_id": view.review_id, "round": rnd, "attempt": view.attempts(rnd) + 1,
            "holding_policy_id": hold.policy_id, "cohort": MANAGED_COHORT,
            "strategy_version": setup["strategy_version"],
        }
        context_json = encoded(json_safe({
            "context_version": compiled.state["context_version"], "identity": identity,
            "state": compiled.state, "options": inputs["options"] or {"stop": [], "target": []},
            "basis": {"stop": inputs["stop"], "target": inputs["target"],
                      "entry": inputs["entry"], "risk_per_coin": inputs["risk"],
                      "increment": inputs["increment"], "bid": inputs["quote"].bid,
                      "quote": inputs["quote"].evidence(),
                      # CRYPTO_MAINTENANCE_V4 only (earlier versions' contexts are unchanged).
                      **({"raise_guards": inputs["raise_guards"]}
                         if inputs.get("raise_guards") is not None else {})},
            "policy": policy.record(), "expires_at": expires.isoformat(),
            "manifest": compiled.manifest,
        }))
        context = ManagedContext(context_json, digest(context_json))
        questions = (review_questions_for(context) if subject == "REVIEW"
                     else EARLY_EXIT_QUESTIONS)
        kind = dr.JEV_REQUEST if subject == "REVIEW" else dr.FLAG_JEV_REQUEST
        id_key = "review_id" if subject == "REVIEW" else "flag_id"
        with self.store.transaction() as conn:
            fresh, flags = load_views(conn, [sid])
            current = (fresh if subject == "REVIEW" else flags).get(view.review_id)
            if current is None or current.decision is not None or (
                    subject == "FLAG" and current.resolution is not None) or (
                    current.pending_request(rnd) is not None):
                return None
            if subject == "REVIEW" and sorted(
                    r["event_seq"] for r in current.answers.values()) != extra[
                    "agent_answer_seqs"]:
                return None  # An answer arrived meanwhile: rebuild on the next pass.
            self.store.event(conn, kind, {
                id_key: view.review_id, "setup_id": str(sid),
                "lifecycle_id": state["lifecycle_id"], "round": rnd,
                "attempt": identity["attempt"], "request_id": request_id,
                "context_hash": context.context_hash, "context": context.data,
                "asked_at": now.isoformat(), "expires_at": expires.isoformat(), **extra,
            }, setup_id=sid, key=f"{kind.lower()}:{request_id}")
        return {"subject": subject, "setup_id": sid, "lifecycle_id": state["lifecycle_id"],
                "view_id": view.review_id, "round": rnd, "request_id": request_id,
                "context": context, "questions": questions}

    async def _ask(self, prepared):
        context = prepared["context"]
        result = await self.reviewer.jev_review(
            request_id=prepared["request_id"], identity=context.identity, state=context.state,
            question_set=prepared["questions"], expires_at=context.expires_at, purpose=PURPOSE,
        )
        result = verify_receipts(self.reviewer.store, context, result, prepared["questions"],
                                 expected_request_id=prepared["request_id"])
        self._record_result(prepared, result, self.now())

    def _answered_at(self, result):
        if not result.receipt_ids:
            return None
        with self.reviewer.store.connect() as conn:
            row = conn.execute("SELECT completed_at FROM lab.jev_receipts WHERE receipt_id=%s",
                               (result.receipt_ids[-1],)).fetchone()
        return row["completed_at"] if row else None

    def _record_result(self, prepared, result, now):
        """``*_JEV_RESULT``: ANSWERED, UNUSABLE (answered, but code cannot use it) or FAILED."""
        subject, context = prepared["subject"], prepared["context"]
        answer, chosen = None, None
        if result.answers:
            if subject == "REVIEW":
                reader = dr.review_reader(context.data["policy"])
                answer = reader(result.answers, context.data["options"],
                                agent_asked="agent_case" in prepared["questions"].questions)
                chosen = {kind: next((o for o in context.data["options"][kind]
                                      if o["option_id"] == dr.option_to_raise(answer, kind)),
                                     None) for kind in ("stop", "target")}
            else:
                answer = dr.read_flag_answer(result.answers)
            status, code = (dr.ANSWERED, None) if answer.usable else (dr.UNUSABLE, answer.code)
        else:
            status, code = dr.FAILED, result.reason or "REVIEW_UNAVAILABLE"
        kind = dr.JEV_RESULT if subject == "REVIEW" else dr.FLAG_JEV_RESULT
        body = {
            ("review_id" if subject == "REVIEW" else "flag_id"): prepared["view_id"],
            "setup_id": str(prepared["setup_id"]), "lifecycle_id": prepared["lifecycle_id"],
            "round": prepared["round"], "request_id": prepared["request_id"],
            "status": status, "code": code, "review_status": result.status,
            "receipt_ids": list(result.receipt_ids),
            "answered_at": _text(self._answered_at(result)), "recorded_at": now.isoformat(),
            "answer": answer.record() if answer else None,
            "meanings": meanings(prepared["questions"], result.answers) if result.answers
            else None,
            "chosen": chosen,
        }
        with self.store.transaction() as conn:
            key = f"{kind.lower()}:{prepared['request_id']}"
            if not conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                                (key,)).fetchone():
                self.store.event(conn, kind, body, setup_id=prepared["setup_id"], key=key)

    def _failed(self, subject, setup, view, rnd, code, now):
        """A Jev attempt that could not be sent (inputs unavailable, budget, interrupted)."""
        kind = dr.JEV_RESULT if subject == "REVIEW" else dr.FLAG_JEV_RESULT
        key = f"{kind.lower()}:{view.review_id}:{rnd}:failed:{now.isoformat()}"
        with self.store.transaction() as conn:
            if not conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                                (key,)).fetchone():
                self.store.event(conn, kind, {
                    ("review_id" if subject == "REVIEW" else "flag_id"): view.review_id,
                    "setup_id": str(setup["setup_id"]),
                    "lifecycle_id": setup["state"]["lifecycle_id"], "round": rnd,
                    "request_id": None, "status": dr.FAILED, "code": code,
                    "receipt_ids": [], "recorded_at": now.isoformat(), "answer": None,
                    "meanings": None, "chosen": None,
                }, setup_id=setup["setup_id"], key=key)

    def _recover(self, setup, subject, view, pending, now):
        """A request recorded before a restart: its recorded answer, never a second vote; once
        its deadline has passed without one, a failed attempt (retried inside the window)."""
        body = pending["body"]
        context = ManagedContext(encoded(body["context"]), body["context_hash"])
        questions = (review_questions_for(context) if subject == "REVIEW"
                     else EARLY_EXIT_QUESTIONS)
        result = recover_receipt(self.reviewer.store, body["request_id"], questions)
        prepared = {"subject": subject, "setup_id": setup["setup_id"],
                    "lifecycle_id": body["lifecycle_id"], "view_id": view.review_id,
                    "round": body["round"], "request_id": body["request_id"],
                    "context": context, "questions": questions}
        if result is None:
            if now >= _aware(body["expires_at"]) + timedelta(seconds=RECOVERY_MARGIN_SECONDS):
                self._record_result(prepared, ReviewResult(
                    body["request_id"], "NEEDS_REVIEW", "REQUEST_INTERRUPTED", (), {}), now)
            return
        result = verify_receipts(self.reviewer.store, context, result, questions,
                                 expected_request_id=body["request_id"])
        self._record_result(prepared, result, now)

    # --- the decision ----------------------------------------------------------------------

    def _decide(self, setup, row, now, view, outcome, code, jev=None):
        """Exactly one ``DAY_REVIEW_DECISION`` per review: CONTINUE (a new plan until the next
        review, one interval of the setup's version on: 24 hours, or its window), EXIT (sell at
        market) or DISCARDED (the trade closed or began exiting meanwhile)."""
        sid = setup["setup_id"]
        request = view.request["body"]
        quote = (self._quote(setup["symbol"], row, self.decision_prices)
                 if outcome != dr.DISCARDED else None)
        key = "day-review-decision:" + view.review_id
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                            (key,)).fetchone():
                return
            if code == dr.DISCUSSION_NO_AGENT_REPLY and conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                (f"day-review-answer:{view.review_id}:{dr.DISCUSSION}",),
            ).fetchone():
                return  # The reply arrived at the deadline: the next pass asks Jev.
            state = self.store.state(conn, sid)
            before = {"stop": state.get("stop"), "target": state.get("target")}
            number = request["review_number"]
            body = {
                "policy_id": request["policy_id"], "review_id": view.review_id,
                "setup_id": str(sid), "lifecycle_id": request["lifecycle_id"],
                "review_number": number, "review_at": request["review_at"],
                "requested_at": request["requested_at"], "decided_at": now.isoformat(),
                "quote": quote.evidence() if quote else None, "levels_before": before,
                "continuations_before": number - 1,
                "agent": {"addressee": request["addressee"], "answers": {
                    rnd: {"event_seq": r["event_seq"], "agent_id": r["body"]["agent_id"],
                          "answer_id": r["body"]["answer"]["answer_id"],
                          "decision": r["body"]["answer"]["decision"],
                          "received_at": r["body"]["received_at"]}
                    for rnd, r in sorted(view.answers.items())}},
                "jev": {"decisive_request_id": jev["body"].get("request_id") if jev else None,
                        "results": [{k: r["body"].get(k) for k in (
                            "round", "request_id", "status", "code", "receipt_ids",
                            "answered_at", "answer", "chosen")} for r in view.results]},
            }
            if (state.get("state") != "OPEN"
                    or state.get("lifecycle_id") != request["lifecycle_id"]):
                outcome, code = dr.DISCARDED, dr.POSITION_CLOSED_DURING_REVIEW
            elif state.get("exit_requested"):
                outcome, code = dr.DISCARDED, dr.EXIT_IN_PROGRESS_DURING_REVIEW
            elif crypto_holding.continuations(state) != number - 1:
                return  # Already moved on (a decision this view has not seen).
            if outcome == dr.CONTINUE and now >= _aware(state["hard_exit_at"]) - timedelta(
                    seconds=HARD_EXIT_MARGIN_SECONDS):
                outcome, code = dr.EXIT, dr.REVIEW_WINDOW_EXCEEDED
            measure = {"quote": body["quote"], "continuation_decision": outcome,
                       "continuations_before": number - 1}
            if outcome == dr.DISCARDED:
                body.update(outcome=outcome, code=code, level_change="NONE",
                            levels_after=before, measurement=None)
            elif outcome == dr.EXIT:
                hold = review_policy(state)
                self.store.transition(conn, sid, state["state"],
                                      exit_requested=hold.exit_reason)
                body.update(outcome=outcome, code=code, level_change="NONE",
                            levels_after=before, exit_requested=hold.exit_reason,
                            measurement=dr.measurement(
                                change_kind=dr.CONTINUE_EXIT_DECISION, at=now.isoformat(),
                                levels_before=before, levels_after=before, exited=True,
                                continuations_after=number - 1, **measure))
            else:
                hold = review_policy(state)  # The setup's own interval: 24 hours or its window.
                change, fields, level = self._level_change(sid, state, jev, quote, now, view)
                next_at = _aware(state["day_review_at"]) + timedelta(
                    seconds=hold.review_interval_seconds)
                fields.update(continuations=number, day_review_at=next_at.isoformat(),
                              hard_exit_at=(next_at + timedelta(
                                  seconds=hold.review_window_seconds
                                  + hold.deadline_grace_seconds)).isoformat())
                after = self.store.transition(conn, sid, state["state"], **fields)
                levels_after = {"stop": after["stop"], "target": after["target"]}
                body.update(outcome=outcome, code=code, level_change=level[0],
                            level_change_code=level[1], stop=change["stop"],
                            target=change["target"], levels_after=levels_after,
                            stop_replace_path=cm.PATCH_REPLACE if "stop_replace" in fields
                            else None, next_review_at=next_at.isoformat(),
                            hard_exit_at=fields["hard_exit_at"], continuations_after=number,
                            measurement=dr.measurement(
                                change_kind=dr.CONTINUE_EXIT_DECISION, at=now.isoformat(),
                                levels_before=before, levels_after=levels_after,
                                exited=False, continuations_after=number, **measure))
            self.store.event(conn, dr.DECISION, body, setup_id=sid, key=key)

    def _level_change(self, setup_id, state, jev, quote, now, view):
        """The continue's stop and target: Jev's chosen options through phase 5's checks
        (``cm.check_change``). A refused change keeps the levels; the continue stands."""
        chosen = (jev["body"].get("chosen") if jev else None) or {"stop": None, "target": None}
        request = view.request_for(jev["body"]["request_id"]) if jev else None
        basis = request["body"]["context"]["basis"] if request else None
        old_stop, old_target = _num(state["stop"]), _num(state["target"])
        new_stop = _num(chosen["stop"]["price"]) if chosen.get("stop") else None
        new_target = _num(chosen["target"]["price"]) if chosen.get("target") else None
        change = {
            kind: {"old": str(old), "new": str(new) if new is not None else None,
                   "option_id": chosen[kind]["option_id"] if chosen.get(kind) else None,
                   "bases": chosen[kind]["bases"] if chosen.get(kind) else None}
            for kind, old, new in (("stop", old_stop, new_stop),
                                   ("target", old_target, new_target))
        }
        if new_stop is None and new_target is None:
            return change, {}, ("NONE", None)
        if quote is None:
            code = "CURRENT_QUOTE_UNAVAILABLE"
        elif state.get("stop_replace"):
            code = "STOP_REPLACE_IN_PROGRESS"
        elif state.get("account_risk"):
            code = "ACCOUNT_RISK_UNEVALUABLE"
        elif (old_stop, old_target) != (_num(basis["stop"]), _num(basis["target"])):
            code = "LEVELS_CHANGED_SINCE_REVIEW"
        else:
            low, high = self._bid_extremes(setup_id, state["lifecycle_id"],
                                           _aware(request["body"]["asked_at"]), quote.bid)
            answered = jev["body"].get("answered_at")
            guards, best_bid = v4_guards(basis, state, high)
            code = cm.check_change(
                old_stop=old_stop, new_stop=new_stop, old_target=old_target,
                new_target=new_target, bid=quote.bid, min_bid=low, max_bid=high,
                answered_at=_aware(answered) if answered else now, now=now,
                increment=_num(basis["increment"]), guards=guards, best_bid=best_bid,
                entry=_num(basis["entry"]), risk=_num(basis["risk_per_coin"]))
        if code is not None:
            return change, {}, ("REFUSED", code)
        fields = {}
        if new_target is not None:
            fields["target"] = str(new_target)
        if new_stop is not None:
            fields["stop"] = str(new_stop)
            fields["stop_replace"] = {"change_id": view.review_id, "path": cm.PATCH_REPLACE,
                                      "from_stop": str(old_stop), "to_stop": str(new_stop),
                                      "applied_at": now.isoformat()}
            if "raise_guards" in basis:  # CRYPTO_MAINTENANCE_V4: the spacing starts now.
                fields[cm.STOP_RAISED_AT] = now.isoformat()
        return change, fields, ("APPLIED", None)

    # --- early exits (plan 4.6.3) ----------------------------------------------------------

    async def _flags(self, setup, row, now, flags, schedule):
        state = setup["state"]
        pending = [f for f in flags.values() if f.raised is not None and f.resolution is None
                   and f.raised["body"]["lifecycle_id"] == state["lifecycle_id"]]
        if not pending:
            return []
        if state.get("exit_requested"):  # A hard exit ends every pending flag.
            self._resolve(setup, row, now, pending, dr.LIFECYCLE_ENDED,
                          dr.EXIT_IN_PROGRESS_DURING_REVIEW)
            return []
        jev_side = [f for f in pending if f.side == "JEV"]
        agent_side = [f for f in pending if f.side == "AGENT"]
        if jev_side and agent_side:  # Each side has flagged: both say exit.
            self._resolve(setup, row, now, pending, dr.EXIT_AGREED, dr.BOTH_SIDES_FLAGGED)
            return []
        jobs = []
        for flag in jev_side:
            self._jev_flag(setup, row, now, flag)
        for flag in agent_side:
            jobs += await self._agent_flag(setup, row, now, flag, schedule)
        return jobs

    def _jev_flag(self, setup, row, now, flag):
        """Jev flagged the trade (maintenance): the agent is asked at once and has 15 min.

        ``CRYPTO_MAINTENANCE_V5`` (package jev-b1): a flag whose reasons say ``if_unanswered``
        EXIT (a confirmed invalidation) resolves ``NO_ANSWER_EXIT`` (the market sell) when no
        agent answers by its deadline, or at once when no agent can be asked; every other flag
        keeps the trade when unanswered (``NO_ANSWER_IN_TIME``), as before."""
        due = _aware(flag.raised["body"]["answer_due_at"])
        unanswered = (dr.NO_ANSWER_EXIT if (flag.raised["body"].get("reasons") or {}).get(
            "if_unanswered") == dr.EXIT_IF_UNANSWERED else dr.NO_ANSWER_IN_TIME)
        if flag.asked is None:
            with self.store.repo.connect() as conn:
                addressee, basis = addressee_for(conn, setup, self.agents)
            key = "exit-flag-asked:" + flag.flag_id
            with self.store.transaction() as conn:
                if not conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                                    (key,)).fetchone():
                    self.store.event(conn, dr.FLAG_ASKED, {
                        "flag_id": flag.flag_id, "setup_id": str(setup["setup_id"]),
                        "lifecycle_id": flag.raised["body"]["lifecycle_id"],
                        "ask_side": "AGENT", "addressee": {"agent_id": addressee,
                                                           "basis": basis},
                        "asked_at": now.isoformat(), "answer_due_at": due.isoformat(),
                        "answer_schema": crypto_holding.AGENT_REVIEW_ANSWER_VERSION,
                        "answer_route": f"/api/v1/lab/exit-flags/{flag.flag_id}/answer",
                        "policy_id": self.flag_policy.policy_id,
                        **({"if_unanswered": dr.EXIT_IF_UNANSWERED}
                           if unanswered == dr.NO_ANSWER_EXIT else {}),
                    }, setup_id=setup["setup_id"], key=key)
            if addressee is None:
                self._resolve(setup, row, now, [flag], unanswered, dr.NO_ACTIVE_AGENT)
            return
        if flag.agent_answer is not None:
            decision = flag.agent_answer["body"]["answer"]["decision"]
            self._resolve(setup, row, now, [flag], dr.flag_resolution(decision == dr.EXIT),
                          None, agent=flag.agent_answer)
        elif now >= due:
            self._resolve(setup, row, now, [flag], unanswered, None)

    async def _agent_flag(self, setup, row, now, flag, schedule):
        """The agent flagged the trade: Jev is asked at once and has the flag's 15 minutes."""
        due = _aware(flag.raised["body"]["answer_due_at"])
        if not self.reviews_enabled:
            self._resolve(setup, row, now, [flag], dr.NO_ANSWER_IN_TIME, dr.JEV_REVIEWS_DISABLED)
            return []
        result = flag.result(dr.FIRST)
        if result is not None:
            answer = dr.answer_from_record(result["body"]["answer"])
            if answer.usable:
                self._resolve(setup, row, now, [flag],
                              dr.flag_resolution(answer.decision == dr.EXIT), None, jev=result)
            else:
                self._resolve(setup, row, now, [flag], dr.EXIT_NOT_AGREED,
                              dr.JEV_ANSWER_UNUSABLE, jev=result)
            return []
        key = ("FLAG", flag.flag_id, dr.FIRST)
        if key in self._inflight:
            return []
        pending = flag.pending_request(dr.FIRST)
        if pending is not None:
            self._recover(setup, "FLAG", flag, pending, now)
            return []
        if now >= due:
            self._resolve(setup, row, now, [flag], dr.NO_ANSWER_IN_TIME, dr.JEV_UNAVAILABLE,
                          jev=flag.last_failure(dr.FIRST))
            return []
        failure = flag.last_failure(dr.FIRST)
        if failure is not None and now < _aware(failure["body"]["recorded_at"]) + timedelta(
                seconds=self.flag_policy.jev_retry_seconds):
            return []
        if not schedule:
            return []
        inputs, code = await self._gathered(setup, row, now, options=False)
        if inputs is None:
            self._failed("FLAG", setup, flag, dr.FIRST, code, now)
            return []
        raised = flag.raised["body"]
        agent_flag = {"decision": dr.EXIT, **{k: raised["reasons"].get(k)
                                            for k in dr.TEXT_LIMITS},
                      "suggested_stop": None, "suggested_target": None,
                      "sources": (raised.get("evidence") or {}).get("sources") or []}
        review = {"round": dr.FIRST, "flagged_minutes_ago": int(
            (now - _aware(raised["raised_at"])).total_seconds() // 60),
            "hours_in_trade": int((now - _aware(setup["state"]["opened_at"])).total_seconds()
                                  // 3600),
            "continuations": crypto_holding.continuations(setup["state"])}
        try:
            compiled = compile_early_exit_state(
                review=review, agent_flag=agent_flag, now=now, symbol=setup["symbol"],
                pick=inputs["pick"], selection=inputs["selection"], trade=inputs["trade"],
                changes=inputs["changes"], news=inputs["news"],
                trigger={"reasons": ["AGENT_EXIT_FLAG"]}, **inputs["bars"])
        except ContextBudgetUnsatisfiable as exc:
            self._failed("FLAG", setup, flag, dr.FIRST, exc.code, now)
            return []
        prepared = self._record_request("FLAG", setup, flag, dr.FIRST, now, due, compiled,
                                        inputs, extra={})
        return [self._job(key, prepared)] if prepared is not None else []

    def _resolve(self, setup, row, now, flags, outcome, code, *, agent=None, jev=None):
        """``EXIT_FLAG_RESOLVED`` for each flag (``EXIT_AGREED`` requests the market sell) and one
        ``EARLY_EXIT_DECISION`` with both sides' answers and the measurement hook."""
        sid = setup["setup_id"]
        quote = (self._quote(setup["symbol"], row, self.decision_prices)
                 if row is not None else None)
        first = flags[0]
        key = "early-exit-decision:" + first.flag_id
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                            (key,)).fetchone():
                return
            if outcome in (dr.NO_ANSWER_IN_TIME, dr.NO_ANSWER_EXIT) and agent is None \
                    and conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                ("exit-flag-answer:" + first.flag_id,),
            ).fetchone():
                return  # The agent answered at the deadline: the next pass resolves by it.
            state = self.store.state(conn, sid)
            before = {"stop": state.get("stop"), "target": state.get("target")}
            agent_view = ({"event_seq": agent["event_seq"], "agent_id": agent["body"]["agent_id"],
                           "answer_id": agent["body"]["answer"]["answer_id"],
                           "decision": agent["body"]["answer"]["decision"],
                           "received_at": agent["body"]["received_at"]} if agent else None)
            jev_view = ({k: jev["body"].get(k) for k in (
                "request_id", "status", "code", "receipt_ids", "answered_at", "answer")}
                if jev else None)
            resolved = []
            for flag in flags:
                if conn.execute(
                    "SELECT 1 FROM lab.managed_events WHERE kind=%s AND body->>'flag_id'=%s",
                    (dr.FLAG_RESOLVED, flag.flag_id),
                ).fetchone():
                    continue
                other = "AGENT" if flag.side == "JEV" else "JEV"
                exit_flags.resolve_exit_flag(
                    self.store, conn, flag_id=flag.flag_id, outcome=outcome,
                    answered_by={"side": other, "code": code,
                                 **({"agent": agent_view} if other == "AGENT" else
                                    {"jev": {k: (jev_view or {}).get(k) for k in (
                                        "request_id", "receipt_ids")}})},
                    answer={"agent": agent_view, "jev": jev_view, "policy_id":
                            self.flag_policy.policy_id},
                    resolved_at=now,
                )
                resolved.append(flag.flag_id)
            after = self.store.state(conn, sid)
            exited = (outcome in exit_flags.EXIT_OUTCOMES and not state.get("exit_requested")
                      and after.get("exit_requested") == self.flag_policy.exit_reason)
            quote_record = quote.evidence() if quote else None
            self.store.event(conn, dr.EARLY_EXIT_DECISION, {
                "policy_id": self.flag_policy.policy_id, "flag_id": first.flag_id,
                "flag_ids": [f.flag_id for f in flags], "resolved": resolved,
                "sides": sorted({f.side for f in flags}), "setup_id": str(sid),
                "lifecycle_id": first.raised["body"]["lifecycle_id"],
                "raised_at": first.raised["body"]["raised_at"], "outcome": outcome,
                "code": code, "decided_at": now.isoformat(), "quote": quote_record,
                "levels": before, "agent": agent_view, "jev": jev_view,
                "exit_requested": self.flag_policy.exit_reason if exited else None,
                "measurement": None if outcome == dr.LIFECYCLE_ENDED else dr.measurement(
                    change_kind=dr.EARLY_EXIT, at=now.isoformat(), levels_before=before,
                    levels_after=before, exited=exited, quote=quote_record,
                    early_exit_outcome=outcome),
            }, setup_id=sid, key=key)

    # --- closed trades ---------------------------------------------------------------------

    def _sweep(self, open_ids):
        """Reviews and flags of trades that closed (or began a new lifecycle) are discarded
        (``POSITION_CLOSED_DURING_REVIEW``) and ended (``LIFECYCLE_ENDED``): all of them once
        after start, then those of trades that left the open set since the last pass."""
        gone = self._watched - set(open_ids)
        self._watched = set(open_ids)
        if self._swept and not gone:
            return
        ids = None if not self._swept else _uuids(gone)
        with self.store.repo.connect() as conn:
            rows = conn.execute(_UNFINISHED, {
                "requested": dr.REQUESTED, "raised": dr.FLAG_RAISED, "decision": dr.DECISION,
                "resolved": dr.FLAG_RESOLVED,
                "policies": list(crypto_holding.REVIEW_POLICY_IDS), "ids": ids,
            }).fetchall()
        self._swept = True
        now = self.now()
        for row in rows:
            setup = {"setup_id": row["setup_id"], "symbol": None, "state": row["state"]}
            if row["kind"] == dr.REQUESTED:
                view = ReviewView(row["body"]["review_id"])
                view.request = {"body": row["body"]}
                self._decide(setup, None, now, view, dr.DISCARDED,
                             dr.POSITION_CLOSED_DURING_REVIEW)
            else:
                flag = FlagView(row["body"]["flag_id"])
                flag.raised = {"body": row["body"]}
                self._resolve(setup, None, now, [flag], dr.LIFECYCLE_ENDED,
                              dr.POSITION_CLOSED_DURING_REVIEW)

    # --- status (alerts) -------------------------------------------------------------------

    def status(self, active):
        """Open trades under the version, reviews in progress and their phase, reviews whose
        latest Jev attempt failed, and pending flags by side, for the runtime status."""
        reviewed = [s for s in active if (s.get("state") or {}).get("state") == "OPEN"
                    and crypto_holding.review_active(s.get("state"))]
        now = self.now()
        with self.store.repo.connect() as conn:
            reviews, flags = load_views(conn, [s["setup_id"] for s in reviewed])
        progress, failing = [], []
        for setup in reviewed:
            state = setup["state"]
            if not state.get("day_review_at"):
                continue
            view = reviews.get(current_review_id(setup))
            if view is None or view.request is None or view.decision is not None:
                continue
            phase = review_phase(view, now)
            progress.append({"setup_id": str(setup["setup_id"]), "symbol": setup["symbol"],
                             "review_id": view.review_id,
                             "review_number": view.request["body"]["review_number"],
                             "review_at": view.request["body"]["review_at"], "phase": phase})
            latest = view.results[-1] if view.results else None
            if latest is not None and latest["body"]["status"] == dr.FAILED:
                failing.append({"setup_id": str(setup["setup_id"]), "symbol": setup["symbol"],
                                "code": latest["body"]["code"],
                                "recorded_at": latest["body"]["recorded_at"]})
        ids = {str(s["setup_id"]) for s in reviewed}
        pending = [f for f in flags.values() if f.raised is not None and f.resolution is None
                   and f.raised["body"]["setup_id"] in ids]
        by_policy = {}
        for setup in reviewed:
            key = review_policy_id(setup.get("state"))
            by_policy[key] = by_policy.get(key, 0) + 1
        window = getattr(self.execution, "crypto_window", None)
        return {
            # The version admission records now (CRYPTO_WINDOW_REVIEW_V1 and its window while
            # MANAGED_CRYPTO_WINDOW_JSON is set); open trades keep the one they recorded.
            "policy_id": crypto_holding.admission_policy(
                True, crypto_holding.JEV_MANAGED_ARM, window).policy_id,
            "window_minutes": window.window_minutes if window is not None else None,
            "open_trades_by_policy": dict(sorted(by_policy.items())),
            "early_exit_policy_id": self.flag_policy.policy_id,
            "management_reviews": self.management_reviews,
            "open_trades": len(reviewed),
            "reviews_in_progress": progress,
            "failing_reviews": failing,
            "pending_exit_flags": {side: sum(f.side == side for f in pending)
                                   for side in ("JEV", "AGENT")},
            "last_pass_at": self.last_pass_at.isoformat() if self.last_pass_at else None,
        }


def decision_records(repository, setup_id):
    """Every continue-or-exit decision and early-exit decision of a trade, oldest first, as the
    results package's ``LevelChange`` fields (plan 4.6.8): ``at``, ``change_kind``
    (``CONTINUE_EXIT_DECISION`` or ``EARLY_EXIT``), old and new stop and target,
    ``actually_exited``, the quote at the decision, the outcome and the source event. Discarded
    reviews and ended flags decided nothing and are left out. Read-only."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT event_seq,kind,body FROM lab.managed_events WHERE setup_id=%s
            AND kind IN (%s,%s) ORDER BY event_seq""",
            (UUID(str(setup_id)), dr.DECISION, dr.EARLY_EXIT_DECISION),
        ).fetchall()
    records = []
    for row in rows:
        body = row["body"]
        if not body.get("measurement"):
            continue
        records.append({**body["measurement"], "outcome": body["outcome"],
                        "code": body.get("code"), "source_event_seq": row["event_seq"],
                        "source_kind": row["kind"], "review_number": body.get("review_number"),
                        "flag_id": body.get("flag_id"), "agent": body.get("agent"),
                        "jev": body.get("jev")})
    return records


def review_phase(view, now):
    """Where a review stands: AWAITING_AGENT (before T), AWAITING_JEV, DISCUSSION (the agent's
    reply), AWAITING_JEV_FINAL or DECIDING."""
    if now < _aware(view.request["body"]["review_at"]):
        return "AWAITING_AGENT"
    if view.discussion is None:
        return "AWAITING_JEV" if view.result(dr.FIRST) is None else "DECIDING"
    if dr.DISCUSSION not in view.answers:
        return "DISCUSSION"
    return "AWAITING_JEV_FINAL" if view.result(dr.FINAL) is None else "DECIDING"


# --- The agent routes --------------------------------------------------------------------------


class TradeReviewService:
    """What a research agent reads and answers (``/api/v1/lab/reviews``, ``/exit-flags``, and
    ``/positions/{id}/exit-flag``). Answers are recorded for ``DayReviews`` to act on; nothing
    here touches a trade or the broker. ``agents`` as for ``DayReviews``."""

    def __init__(self, store, *, clock, agents=None):
        self.store, self.now = store, clock
        self.agents = frozenset(agents) if agents is not None else None

    # --- reading ---------------------------------------------------------------------------

    def pending(self, principal):
        """The caller's pending requests: 24-hour reviews awaiting its first answer (until T)
        or its discussion reply (15 minutes), and Jev exit flags awaiting its answer."""
        now = self.now()
        items = []
        if getattr(principal, "role", None) == "muse":
            with self.store.repo.connect() as conn:
                setups = open_review_setups(conn)
                reviews, flags = load_views(conn, [s["setup_id"] for s in setups])
            for setup in setups:
                items += self._setup_items(principal, setup, reviews, flags, now)
        items.sort(key=lambda item: (item["answer_due_at"], item["setup_id"]))
        return json_safe({"as_of": now, "poll_hint_seconds": POLL_HINT_SECONDS,
                          "request_lead_seconds": crypto_holding.REQUEST_LEAD_SECONDS,
                          "items": items, "trade_authorized": False})

    def _setup_items(self, principal, setup, reviews, flags, now):
        state = setup["state"]
        items = []
        if state.get("exit_requested"):
            return items
        view = reviews.get(current_review_id(setup)) if state.get("day_review_at") else None
        if view is not None and view.request is not None and view.decision is None:
            request = view.request["body"]
            addressee = request["addressee"]["agent_id"]
            if addressee is not None and principal.acts_for(addressee):
                rnd, due = open_round(view, now)
                if rnd is not None and rnd not in view.answers:
                    item = {"kind": "DAY_REVIEW", "review_id": view.review_id,
                            "setup_id": request["setup_id"], "symbol": request["symbol"],
                            "lifecycle_id": request["lifecycle_id"], "round": rnd,
                            "answer_due_at": due.isoformat(),
                            "review_at": request["review_at"],
                            "review_number": request["review_number"],
                            "answer_route": request["answer_route"],
                            "answer_schema": request["answer_schema"],
                            "request": {k: v for k, v in request.items() if k != "addressee"}}
                    if rnd == dr.DISCUSSION:
                        item["your_first_answer"] = view.answers[dr.FIRST]["body"]["answer"]
                        jev = view.discussion["body"]["jev_first"]
                        item["jev_first_answer"] = {
                            "answers": jev["answer"]["summary"], "meanings": jev["meanings"],
                            "chosen": jev["chosen"]}
                    items.append(item)
        for flag in flags.values():
            if (flag.raised is None or flag.side != "JEV" or flag.resolution is not None
                    or flag.asked is None or flag.agent_answer is not None
                    or flag.raised["body"]["lifecycle_id"] != state["lifecycle_id"]):
                continue
            asked = flag.asked["body"]
            addressee = asked["addressee"]["agent_id"]
            if addressee is None or not principal.acts_for(addressee):
                continue
            if now >= _aware(asked["answer_due_at"]):
                continue
            raised = flag.raised["body"]
            items.append({
                "kind": "EXIT_FLAG", "flag_id": flag.flag_id, "setup_id": raised["setup_id"],
                "symbol": setup["symbol"], "lifecycle_id": raised["lifecycle_id"],
                "raised_by": "JEV", "raised_at": raised["raised_at"],
                "answer_due_at": asked["answer_due_at"], "answer_route": asked["answer_route"],
                "answer_schema": asked["answer_schema"],
                "jev_reasons": {**{k: raised["reasons"].get(k) for k in (
                    "trade_reason", "answers", "trigger_reasons")},
                    # CRYPTO_MAINTENANCE_V5: which question confirmed and what no answer does.
                    **{k: raised["reasons"][k] for k in ("question", "if_unanswered")
                       if k in raised["reasons"]}},
                "trade": raised.get("evidence"),
            })
        return items

    # --- answering -------------------------------------------------------------------------

    def _find(self, conn, setup_id=None):
        setups = open_review_setups(conn)
        if setup_id is not None:
            setups = [s for s in setups if str(s["setup_id"]) == str(setup_id)]
        return setups, load_views(conn, [s["setup_id"] for s in setups])

    @staticmethod
    def _replay(rows, answer_id, request_hash):
        for row in rows:
            body = row["body"]
            recorded = body.get("answer") or {}
            if recorded.get("answer_id") == answer_id:
                if body.get("request_hash") != request_hash:
                    raise ReviewConflict("IDEMPOTENCY_CONTENT_MISMATCH")
                return row
        return None

    def answer_review(self, principal, review_id, raw):
        """``AGENT_REVIEW_ANSWER_V1`` for a review round open to the caller now."""
        now = self.now()
        agent_id = principal.agent_id if principal.role == "muse" else None
        answer = dr.validate_review_answer(raw, agent_id=agent_id, now=now,
                                           suggestions_allowed=True)
        request_hash = digest(encoded({"review_id": str(review_id), **raw}))
        with self.store.transaction() as conn:
            setups, (reviews, _) = self._find(conn)
            view = reviews.get(str(review_id))
            if view is None or view.request is None:
                raise ReviewNotFound("REVIEW_NOT_FOUND")
            request = view.request["body"]
            setup = next(s for s in setups if str(s["setup_id"]) == request["setup_id"])
            addressee = request["addressee"]["agent_id"]
            if addressee is None or not principal.acts_for(addressee):
                raise ReviewAccessDenied("AGENT_IDENTITY_MISMATCH")
            replay = self._replay(view.answers.values(), answer["answer_id"], request_hash)
            if replay is not None:
                return self._ack(replay, replay=True)
            if view.decision is not None or setup["state"].get("exit_requested") or (
                    view.review_id != current_review_id(setup)):
                raise ReviewConflict("REVIEW_CLOSED")
            rnd, _ = open_round(view, now)
            if rnd is None:
                raise ReviewConflict("REVIEW_ANSWER_WINDOW_CLOSED")
            if rnd in view.answers:
                raise ReviewConflict("REVIEW_ALREADY_ANSWERED")
            row = self.store.event(conn, dr.AGENT_ANSWER, {
                "review_id": view.review_id, "setup_id": request["setup_id"],
                "lifecycle_id": request["lifecycle_id"], "round": rnd, "agent_id": agent_id,
                "answer": answer, "request_hash": request_hash, "received_at": now.isoformat(),
            }, setup_id=setup["setup_id"], key=f"day-review-answer:{view.review_id}:{rnd}")
        return self._ack(row, replay=False)

    def answer_flag(self, principal, flag_id, raw):
        """``AGENT_REVIEW_ANSWER_V1`` (EXIT or CONTINUE, no suggested levels) for a Jev flag."""
        now = self.now()
        agent_id = principal.agent_id if principal.role == "muse" else None
        answer = dr.validate_review_answer(raw, agent_id=agent_id, now=now,
                                           suggestions_allowed=False)
        request_hash = digest(encoded({"flag_id": str(flag_id), **raw}))
        with self.store.transaction() as conn:
            setups, (_, flags) = self._find(conn)
            flag = flags.get(str(flag_id))
            if flag is None or flag.raised is None or flag.side != "JEV":
                raise ReviewNotFound("EXIT_FLAG_NOT_FOUND")
            if flag.asked is None:
                raise ReviewConflict("EXIT_FLAG_NOT_YET_ASKED")
            addressee = flag.asked["body"]["addressee"]["agent_id"]
            if addressee is None or not principal.acts_for(addressee):
                raise ReviewAccessDenied("AGENT_IDENTITY_MISMATCH")
            if flag.agent_answer is not None:
                replay = self._replay([flag.agent_answer], answer["answer_id"], request_hash)
                if replay is not None:
                    return self._ack(replay, replay=True)
                raise ReviewConflict("EXIT_FLAG_ALREADY_ANSWERED")
            if flag.resolution is not None:
                raise ReviewConflict("EXIT_FLAG_RESOLVED")
            if now >= _aware(flag.asked["body"]["answer_due_at"]):
                raise ReviewConflict("EXIT_FLAG_ANSWER_WINDOW_CLOSED")
            raised = flag.raised["body"]
            row = self.store.event(conn, dr.FLAG_AGENT_ANSWER, {
                "flag_id": flag.flag_id, "setup_id": raised["setup_id"],
                "lifecycle_id": raised["lifecycle_id"], "agent_id": agent_id, "answer": answer,
                "request_hash": request_hash, "received_at": now.isoformat(),
            }, setup_id=UUID(raised["setup_id"]), key="exit-flag-answer:" + flag.flag_id)
        return self._ack(row, replay=False)

    def raise_flag(self, principal, setup_id, raw):
        """``AGENT_EXIT_FLAG_V1``: the agent's early-exit flag (``EXIT_FLAG_RAISED``, side
        AGENT). Jev is asked at once; the trade keeps its stop and target meanwhile."""
        now = self.now()
        agent_id = principal.agent_id if principal.role == "muse" else None
        flag = dr.validate_exit_flag(raw, agent_id=agent_id, now=now)
        request_hash = digest(encoded({"setup_id": str(setup_id), **raw}))
        with self.store.transaction() as conn:
            try:
                setup = self.store.setup(conn, str(setup_id))
            except ValueError:
                raise ReviewNotFound("POSITION_NOT_FOUND") from None
            state = self.store.state(conn, setup["setup_id"])
            setup = {**setup, "state": state}
            if not crypto_holding.review_active(state):
                raise ReviewConflict("EARLY_EXIT_NOT_AVAILABLE")
            responsible, _ = addressee_for(conn, setup, self.agents)
            if responsible is None or not principal.acts_for(responsible):
                raise ReviewAccessDenied("AGENT_IDENTITY_MISMATCH")
            existing = conn.execute(
                """SELECT * FROM lab.managed_events WHERE setup_id=%s AND kind=%s
                AND body->'raised_by'->>'flag_ref'=%s""",
                (setup["setup_id"], dr.FLAG_RAISED, flag["flag_ref"]),
            ).fetchone()
            if existing is not None:
                if existing["body"]["raised_by"].get("request_hash") != request_hash:
                    raise ReviewConflict("IDEMPOTENCY_CONTENT_MISMATCH")
                return self._flag_ack(existing, created=False, replay=True)
            if state.get("state") != "OPEN":
                raise ReviewConflict("POSITION_NOT_OPEN")
            if flag["lifecycle_id"] != state.get("lifecycle_id"):
                raise ReviewConflict("POSITION_LIFECYCLE_MISMATCH")
            if state.get("exit_requested"):
                raise ReviewConflict("EXIT_IN_PROGRESS")
            row, created = exit_flags.raise_exit_flag(
                self.store, conn, setup_id=setup["setup_id"], lifecycle_id=state["lifecycle_id"],
                side="AGENT",
                raised_by={"agent_id": agent_id, "flag_ref": flag["flag_ref"],
                           "request_hash": request_hash, "schema_version": flag[
                               "schema_version"]},
                reasons={k: flag[k] for k in dr.TEXT_LIMITS},
                evidence={"levels": {"stop": state.get("stop"), "target": state.get("target")},
                          "sources": flag["sources"]},
                raised_at=now, reference=flag["flag_ref"],
            )
        return self._flag_ack(row, created=created, replay=False)

    @staticmethod
    def _ack(row, *, replay):
        body = row["body"]
        return json_safe({
            "status": "REVIEW_ANSWER_RECORDED" if "review_id" in body
            else "EXIT_FLAG_ANSWER_RECORDED",
            **({"review_id": body["review_id"], "round": body["round"]} if "review_id" in body
               else {"flag_id": body["flag_id"]}),
            "decision": body["answer"]["decision"], "received_at": body["received_at"],
            "idempotent_replay": replay, "trade_authorized": False, "position_modified": False,
        })

    @staticmethod
    def _flag_ack(row, *, created, replay):
        body = row["body"]
        return json_safe({
            "status": "EXIT_FLAG_RAISED" if created or replay else "EXIT_FLAG_ALREADY_PENDING",
            "flag_id": body["flag_id"], "setup_id": body["setup_id"],
            "raised_at": body["raised_at"], "answer_due_at": body["answer_due_at"],
            "idempotent_replay": replay, "trade_authorized": False, "position_modified": False,
        })


def open_round(view, now):
    """``(round, due)`` open to the agent now: FIRST until T while Jev has not been asked, the
    DISCUSSION reply until its due time; ``(None, None)`` otherwise."""
    request = view.request["body"]
    review_at = _aware(request["review_at"])
    if now < review_at and not any(r["body"]["round"] == dr.FIRST for r in view.requests):
        return dr.FIRST, review_at
    if view.discussion is not None:
        due = _aware(view.discussion["body"]["reply_due_at"])
        if now < due:
            return dr.DISCUSSION, due
    return None, None
