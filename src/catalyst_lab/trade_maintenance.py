"""Trade maintenance: the monitoring Jev maintains open crypto trades (``CRYPTO_MAINTENANCE_V1``).

Plan ``docs/CRYPTO-AGENT-LOOP.md`` 4.6.2, 4.6.3 (the Jev side), 4.6.5 and 4.6.7; the rules and
numbers are in ``crypto_maintenance``, what Jev reads in ``maintenance_dossier``. The runtime's
research loop calls ``TradeMaintenance.run_pass`` about once a second with the open trades whose
state records the version; protection, stops, targets and every exit keep running in the
execution loop whatever happens here (a slow or failing Jev never blocks them).

Each pass: a Bitcoin shock is recorded once (``MAINTENANCE_BTC_SHOCK``); for each trade the
triggers the quote shows are recorded once (``MAINTENANCE_TRIGGER``: the first +nR milestones,
the bid entering 0.5% of the target or of the stop, each once per level) and the review is due
at a completed 15-minute bar, an unserved trigger, agent news or a shock, never inside a minute
of the last request unless near the target. Due trades are prepared nearest to their stop first
(``POSITION_REVIEW_REQUEST`` with the V4 context), then reviewed concurrently. Each answer ends
in exactly one ``MAINTENANCE_DECISION`` (the measurement record: outcome, old and new levels,
decision time, bid/ask/last at the decision, receipts) and the done-marker the recovery reads
(``MANAGED_JEV_JUDGMENT``, or ``POSITION_REVIEW_OBSOLETE`` for a discarded review).

* APPLIED: the checks passed; a raised target takes effect at once (the app watches it), a
  raised stop is recorded as ``stop_replace`` and the protection loop replaces the resting
  stop-limit (PATCH replace, else cancel-then-place) under its own one-use authorization.
* REFUSED: a check failed, or the answer was uncertain or inconsistent; the trade keeps its
  levels. HELD: Jev chose HOLD (under ``CRYPTO_MAINTENANCE_V2`` also a raise with no usable
  option answer, code ``NO_USABLE_OPTION``). FLAGGED: Jev flagged the trade for an early-exit
  review (``exit_flags``); the trade keeps its levels. FAILED: Jev was unavailable (breaker,
  provider, deadline); the trade keeps its levels and the status reports it. DISCARDED: the stop
  was crossed or the trade closed or began exiting while Jev answered.

Each setup keeps the maintenance version it recorded at admission (``CRYPTO_MAINTENANCE_V1`` or
``_V2``, package answer-rules): its requests carry that version's record as the context's
``policy``, and everything version-specific follows it: the cadence (V1 a review at every
completed 15-minute bar, V2 at every completed 1-minute bar, ``BAR_1M``), the context and
questions (V4, or V5 with the last 60 1-minute bars and the trade's last 5 maintenance reviews)
and the answer rule (``maintenance_dossier.answer_reader``: V1's consistency rules or
``MAINTENANCE_ANSWER_RULE_V2``). Under V2 at most one review is in flight per trade: a minute
that completes while the trade's previous review is still running is skipped and recorded
(``MAINTENANCE_REVIEW_SKIPPED``, code ``REVIEW_SKIPPED_IN_FLIGHT``), never reviewed late.

A pass runs its Jev calls concurrently, at most ``max_inflight`` at a time (the runtime passes
the research cycle's own in-flight limit, ``CyclePolicy.max_inflight``): due trades take their
turn nearest to their stop first, and each request (with its 10-second deadline) is recorded
only when its turn comes, so a trade that waits is never failed by the wait.

``CRYPTO_MAINTENANCE_V3`` (package jev-budget) keeps V2's reviews and sets the routine cadence
from the monthly Jev budget: each pass takes one decision of the spend guard
(``jev_budget.SpendGuard``, ``JEV_SPEND_GUARD_V1``) and every V3 trade is reviewed at every
completed 1-minute (``NORMAL``, ``BAR_1M``), 5-minute (``THROTTLED``, ``BAR_5M``) or 15-minute
(``TIGHT``, ``BAR_15M``) bar, plus V2's events. ``EXHAUSTED``, or a guard that cannot decide,
sends nothing for V3 trades (one ``POSITION_REVIEW_SKIPPED`` per lifecycle and episode; triggers
are still recorded and are served when reviews resume). Each V3 request records the guard's
facts (version, tier, its event, the cadence and the review's ``routine_weight``, the per-minute
reviews it stands for) in its trigger and its Jev identity, where the meter reads the weight.
V1 and V2 trades are never governed.

``MANAGED_MANAGEMENT_REVIEWS=DISABLED`` sends nothing (one POSITION_REVIEW_SKIPPED per trade
lifecycle); protection and the partial-entry rule run unchanged in the execution loop.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import NAMESPACE_URL, uuid4, uuid5

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import exit_flags, jev_budget
from catalyst_lab.jev_contract import JEV_MODEL, digest, encoded, strict_json, validated_answers
from catalyst_lab.jev_review import ReviewResult
from catalyst_lab.maintenance_dossier import (
    STATE_BYTE_BUDGET,
    MaintenanceAnswerV2,
    answer_reader,
    compile_state_for,
    option_to_raise,
    questions_for,
)
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable
from catalyst_lab.managed_review import EXIT_POLICY, MANAGED_COHORT, ManagedContext
from catalyst_lab.position_monitor import MANAGEMENT_REVIEWS_ENABLED, MANAGEMENT_REVIEWS_SETTINGS
from catalyst_lab.repository import json_safe
from catalyst_lab.system_check import LivePriceReader, LivePriceUnavailable

DECISION_EVENT = "MAINTENANCE_DECISION"
REQUEST_EVENT = "POSITION_REVIEW_REQUEST"
JUDGMENT_EVENT = "MANAGED_JEV_JUDGMENT"
OBSOLETE_EVENT = "POSITION_REVIEW_OBSOLETE"
SKIPPED_EVENT = "POSITION_REVIEW_SKIPPED"
UNAVAILABLE_EVENT = "POSITION_REVIEW_UNAVAILABLE"
# CRYPTO_MAINTENANCE_V3: why a guarded trade was not reviewed (POSITION_REVIEW_SKIPPED reasons).
BUDGET_EXHAUSTED = "JEV_BUDGET_EXHAUSTED"
BUDGET_UNAVAILABLE = "JEV_BUDGET_UNAVAILABLE"
APPLIED, REFUSED, HELD, FLAGGED, FAILED, DISCARDED = (
    "APPLIED", "REFUSED", "HELD", "FLAGGED", "FAILED", "DISCARDED")
PURPOSE = "ENGINEERING_TEST"  # The position reviews' record purpose (PositionMonitor's).
DEFAULT_MAX_INFLIGHT = 10  # Without a configured limit: ten trades' calls in one wave.


def _aware(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return value


def _num(value):
    return cm.number(value)


def _policy_id(state):
    """The setup's recorded maintenance version as written (V1 or V2), for event bodies."""
    return cm.recorded_policy_id(state)


def verify_maintenance_receipts(store, context, result, *, expected_request_id):
    """V4's receipt verification: exact context, question set, request and answers."""
    try:
        if result.request_id != expected_request_id or any(
            not store.verify(receipt)["valid"] for receipt in result.receipt_ids
        ):
            raise ValueError("RECEIPT_INTEGRITY_FAILED")
        questions = questions_for(context)
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


class TradeMaintenance:
    """The monitoring Jev for trades admitted under ``CRYPTO_MAINTENANCE_V1``, ``_V2`` or
    ``_V3``; ``spend_guard`` (``jev_budget.SpendGuard``) sets V3's routine cadence."""

    def __init__(self, execution, reviewer, *, bars, clock, management_reviews,
                 live_prices=None, max_inflight=DEFAULT_MAX_INFLIGHT, spend_guard=None):
        if management_reviews not in MANAGEMENT_REVIEWS_SETTINGS:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_REQUIRED")
        if type(max_inflight) is not int or not 1 <= max_inflight <= 50:
            raise ValueError("MAINTENANCE_MAX_INFLIGHT_INVALID")
        execution.repo.require_same_database(reviewer.store)
        self.max_inflight = max_inflight
        self.execution, self.reviewer, self.bars, self.now = execution, reviewer, bars, clock
        self.management_reviews = management_reviews
        # Its own bounded reader (one REST quote read per pass, one per symbol per 5 s), so
        # review quotes never spend the protection tick's budget.
        self.live_prices = live_prices or LivePriceReader(bars, clock=clock)
        self._bar_cache = {}
        self._inflight = {}
        self._noted = set()
        # CRYPTO_MAINTENANCE_V3 (package jev-budget): the monthly Jev budget's guard and this
        # pass's decision (one per pass, shared by every V3 trade).
        self.spend_guard = spend_guard
        self.guard_decision = None
        self.last_pass_at = None
        self.observe = None  # The runtime's stream-row reader, set by each pass.

    @property
    def store(self):
        return self.execution.store

    @property
    def reviews_enabled(self):
        return self.management_reviews == MANAGEMENT_REVIEWS_ENABLED

    # --- ledger helpers ------------------------------------------------------------------

    def _event(self, conn, kind, body, setup_id=None, key=None):
        return self.store.event(conn, kind, body, setup_id=setup_id, key=key)

    def _note_once(self, kind, body, key, setup_id=None):
        """A keyed notice, written once (per key) and remembered by this process."""
        if key in self._noted:
            return
        with self.store.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone():
                self._event(conn, kind, body, setup_id, key)
        self._noted.add(key)

    def _withheld(self, setup):
        if self.reviews_enabled:
            return False
        lifecycle = setup["state"].get("lifecycle_id")
        reason = "MANAGEMENT_REVIEWS_DISABLED"
        self._note_once(SKIPPED_EVENT, {"reason": reason, "lifecycle_id": lifecycle},
                        f"position-review-skipped:{setup['setup_id']}:{lifecycle}:{reason}",
                        setup["setup_id"])
        return True

    @staticmethod
    def _pending_request(conn, setup_id, lifecycle):
        return conn.execute(
            """SELECT e.event_seq,e.body FROM lab.managed_events e WHERE e.setup_id=%s
            AND e.kind='POSITION_REVIEW_REQUEST'
            AND e.body->'context'->>'context_version'=ANY(%s)
            AND e.body->'context'->'identity'->>'lifecycle_id'=%s
            AND NOT EXISTS(SELECT 1 FROM lab.managed_events d WHERE d.setup_id=e.setup_id
              AND d.kind IN ('MANAGED_JEV_JUDGMENT','POSITION_REVIEW_OBSOLETE')
              AND d.body->>'request_id'=e.body->>'request_id')
            ORDER BY e.event_seq DESC LIMIT 1""",
            (setup_id, list(cm.CONTEXT_VERSIONS), lifecycle),
        ).fetchone()

    @staticmethod
    def _last_request(conn, setup_id, lifecycle):
        return conn.execute(
            """SELECT event_seq,body FROM lab.managed_events WHERE setup_id=%s
            AND kind='POSITION_REVIEW_REQUEST' AND body->'context'->>'context_version'=ANY(%s)
            AND body->'context'->'identity'->>'lifecycle_id'=%s
            ORDER BY event_seq DESC LIMIT 1""",
            (setup_id, list(cm.CONTEXT_VERSIONS), lifecycle),
        ).fetchone()

    @staticmethod
    def _entry_complete(conn, setup_id, lifecycle):
        return bool(conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
            (f"maintenance-entry-completed:{setup_id}:{lifecycle}",),
        ).fetchone())

    @staticmethod
    def _average_entry(conn, setup_id, state):
        row = conn.execute(
            """SELECT sum(qty) AS qty,sum(qty*price) AS cost FROM lab.managed_fills
            WHERE setup_id=%s AND side='buy'""", (setup_id,),
        ).fetchone()
        if row and row["qty"]:
            return row["cost"] / row["qty"]
        return _num(state["fill_price"])

    @staticmethod
    def _news_revision(conn, setup, lifecycle):
        from catalyst_lab.position_news import current_position_evidence

        return current_position_evidence(conn, setup, lifecycle)[0]

    @staticmethod
    def _triggers(conn, setup_id, lifecycle, after_seq):
        return conn.execute(
            """SELECT event_seq,body FROM lab.managed_events WHERE setup_id=%s
            AND kind=%s AND body->>'lifecycle_id'=%s AND event_seq>%s ORDER BY event_seq""",
            (setup_id, cm.TRIGGER_EVENT, lifecycle, after_seq),
        ).fetchall()

    @staticmethod
    def _shocks(conn, after_seq, since):
        return conn.execute(
            """SELECT event_seq,body FROM lab.managed_events WHERE setup_id IS NULL
            AND kind=%s AND event_seq>%s AND (body->>'price_at')::timestamptz>=%s
            ORDER BY event_seq""",
            (cm.SHOCK_EVENT, after_seq, since),
        ).fetchall()

    @staticmethod
    def _decided_at(conn, setup_id, request_id):
        """When a request's one MAINTENANCE_DECISION was recorded, or None (still in flight)."""
        row = conn.execute(
            "SELECT body->>'decided_at' AS at FROM lab.managed_events WHERE idempotency_key=%s",
            ("maintenance-decision:" + str(request_id),),
        ).fetchone()
        return _aware(row["at"]) if row and row["at"] else None

    @staticmethod
    def _last_skipped_bar(conn, setup_id, lifecycle):
        """CRYPTO_MAINTENANCE_V2: the latest minute skipped while a review was in flight."""
        row = conn.execute(
            """SELECT body->>'bar_end' AS bar_end FROM lab.managed_events WHERE setup_id=%s
            AND kind=%s AND body->>'lifecycle_id'=%s ORDER BY event_seq DESC LIMIT 1""",
            (setup_id, cm.MINUTE_SKIPPED_EVENT, lifecycle),
        ).fetchone()
        return _aware(row["bar_end"]) if row else None

    @staticmethod
    def review_history(conn, setup_id, lifecycle, limit=cm.REVIEW_HISTORY):
        """The lifecycle's last ``limit`` maintenance reviews, oldest first, as
        ``maintenance_dossier.review_history_row`` reads them (context V5 and
        ``JEV_DAY_REVIEW_CONTEXT_V2``): each MAINTENANCE_DECISION with its request's offered
        option prices."""
        rows = conn.execute(
            """SELECT d.body AS decision,r.body->'context'->'options' AS options
            FROM lab.managed_events d LEFT JOIN lab.managed_events r
              ON r.setup_id=d.setup_id AND r.kind='POSITION_REVIEW_REQUEST'
              AND r.body->>'request_id'=d.body->>'request_id'
            WHERE d.setup_id=%s AND d.kind=%s AND d.body->>'lifecycle_id'=%s
            ORDER BY d.event_seq DESC LIMIT %s""",
            (setup_id, DECISION_EVENT, lifecycle, limit),
        ).fetchall()
        history = []
        for row in reversed(rows):
            body, options = row["decision"], row["options"] or {}
            history.append({
                "requested_at": body.get("requested_at"),
                "trigger_reasons": body.get("trigger_reasons"),
                "answers": body.get("answers"), "outcome": body.get("outcome"),
                "code": body.get("code"),
                "option_prices": {kind: {o["option_id"]: o["price"]
                                         for o in options.get(kind) or ()}
                                  for kind in ("stop", "target")},
            })
        return history

    def _bid_extremes(self, setup_id, lifecycle, since, current):
        """The lowest and highest bids after ``since`` (the request): the per-second position
        samples the protection loop records, this process's own observations while Jev
        answered, and the current bid."""
        with self.store.repo.connect() as conn:
            row = conn.execute(
                """SELECT min((body->>'bid')::numeric) AS low,max((body->>'bid')::numeric) AS high
                FROM lab.managed_events WHERE setup_id=%s AND kind='POSITION_MARKET_SNAPSHOT'
                AND body->>'lifecycle_id'=%s AND (body->>'received_at')::timestamptz>%s""",
                (setup_id, lifecycle, since),
            ).fetchone()
        values = [current]
        for value in (row["low"], row["high"]):
            if value is not None:
                values.append(value)
        seen = self._inflight.get(str(setup_id), {})
        values.extend(v for v in (seen.get("low"), seen.get("high")) if v is not None)
        return min(values), max(values)

    # --- quotes and bars -----------------------------------------------------------------

    def _quote(self, symbol, row):
        """A quote fresh by read time: the stream's if received within 5 s, else one bounded
        REST read; None when neither is available."""
        received = (row or {}).get("quote_received_at")
        received = _aware(received) if isinstance(received, str) else received
        try:
            return self.live_prices.read(symbol, row, by_read_time=True, received_at=received)
        except LivePriceUnavailable:
            return None

    async def _timeframe(self, symbol, timeframe, count):
        """Completed bars of one timeframe, fetched once per bar boundary and symbol."""
        seconds = cm.TIMEFRAMES[timeframe]
        boundary = cm.floor_time(self.now(), seconds)
        cached = self._bar_cache.get((symbol, timeframe))
        if cached and cached[0] == boundary:
            return cached[1], ()
        bars, issues = await asyncio.to_thread(
            self.bars.timeframe_bars, "CRYPTO", symbol, timeframe=timeframe, count=count
        )
        if issues:
            return (), tuple(issues)
        self._bar_cache[(symbol, timeframe)] = (boundary, tuple(bars))
        return tuple(bars), ()

    async def _bar_sets(self, symbol, *, minute=False):
        """The coin's and Bitcoin's 15-minute and 1-hour bars and, for context V5
        (``minute``), the coin's last 60 completed 1-minute bars (GET-only, once per completed
        minute and symbol). A missing set of the coin's bars stops the review."""
        sets, issues = {}, []
        wanted = [
            ("bars_15m", symbol, "15Min", cm.BARS_15M), ("bars_1h", symbol, "1Hour", cm.BARS_1H),
            ("btc_15m", cm.BENCHMARK_SYMBOL, "15Min", cm.BARS_15M),
            ("btc_1h", cm.BENCHMARK_SYMBOL, "1Hour", cm.BARS_1H),
        ]
        if minute:
            wanted.append(("bars_1m", symbol, "1Min", cm.BARS_1M))
        for name, sym, timeframe, count in wanted:
            bars, found = await self._timeframe(sym, timeframe, count)
            if found and name.startswith("bars"):
                issues.extend(found)
            sets[name] = bars
        return sets, issues

    # --- the pass ------------------------------------------------------------------------

    async def run_pass(self, setups, observe, *, benchmark=None, benchmark_ready=False):
        """One maintenance pass over the open maintained trades (``setups``: active rows)."""
        now = self.now()
        self.last_pass_at, self.observe = now, observe
        self.live_prices.new_tick()
        setups = [s for s in setups if (s.get("state") or {}).get("state") == "OPEN"
                  and cm.active(s.get("state"))]
        if not self.reviews_enabled:
            for setup in setups:
                self._withheld(setup)  # Nothing is sent to Jev and no bar is fetched.
            return []
        # CRYPTO_MAINTENANCE_V3: one decision of the spend guard sets this pass's routine
        # cadence for every guarded trade (V1 and V2 trades never ask it). Off the event loop:
        # its first read loads the month's receipts.
        self.guard_decision = await asyncio.to_thread(self._guard_decision, now) if any(
            cm.guarded(cm.recorded_policy(s["state"])) for s in setups) else None
        if benchmark is not None and benchmark_ready and setups:
            shock = benchmark.check(now)
            if shock is not None:
                # One market fact for every maintained trade: V1's shock rule, which V2 keeps.
                with self.store.transaction() as conn:
                    self._event(conn, cm.SHOCK_EVENT,
                                {**shock, "policy_id": cm.MAINTENANCE_VERSION},
                                key=f"maintenance-btc-shock:{shock['price_at']}")
        due = []
        for setup in setups:
            try:
                item = await self._due(setup, observe(setup), now)
            except Exception as exc:  # One trade's fault never stops another's review.
                self._unavailable(setup, "MAINTENANCE_PASS_FAILED", type(exc).__name__)
                continue
            if item is not None:
                due.append(item)
        # Nearest to its stop first (plan 4.6.7); a trade without a stream quote goes last.
        order = cm.nearest_to_stop_first([(i, item["bid"], item["stop"])
                                          for i, item in enumerate(due)
                                          if item["bid"] is not None])
        order += [i for i, item in enumerate(due) if item["bid"] is None]
        # At most ``max_inflight`` Jev calls at once. A trade waits for a free slot in that
        # order, and its request (with its deadline) is built and recorded only then.
        slots = asyncio.Semaphore(self.max_inflight)
        started = []

        async def review(request):
            try:
                return await self._review(request)
            finally:
                self._inflight.pop(str(request["setup_id"]), None)
                slots.release()

        for rank, index in enumerate(order, start=1):
            await slots.acquire()
            try:
                request = await self._request(due[index], rank)
            except Exception as exc:
                slots.release()
                self._unavailable(due[index]["setup"], "MAINTENANCE_REQUEST_FAILED",
                                  type(exc).__name__)
                continue
            if request is None:
                slots.release()
                continue
            started.append((request, asyncio.ensure_future(review(request))))
        results = await asyncio.gather(*(task for _, task in started), return_exceptions=True)
        for (request, _), result in zip(started, results, strict=True):
            if isinstance(result, Exception):
                self._unavailable(request["setup"], "MAINTENANCE_REVIEW_FAILED",
                                  type(result).__name__)
        return [request["request_id"] for request, _ in started]

    def _guard_decision(self, now):
        """The spend guard's decision now; without a guard, or when it fails, UNAVAILABLE (V3
        trades then wait: fail closed on spend, never on protection)."""
        if self.spend_guard is None:
            return jev_budget.unavailable(now, "JEV_SPEND_GUARD_NOT_CONFIGURED")
        try:
            return self.spend_guard.evaluate(now)
        except Exception as exc:
            return jev_budget.unavailable(now, type(exc).__name__)

    def _withheld_by_budget(self, setup, guard):
        """CRYPTO_MAINTENANCE_V3: no review while the budget is exhausted or the guard cannot
        decide. One POSITION_REVIEW_SKIPPED per lifecycle and episode (the tier's event, or the
        guard's failure code); the trade keeps its stop and target and protection runs."""
        lifecycle = setup["state"].get("lifecycle_id")
        reason = BUDGET_EXHAUSTED if guard.tier == jev_budget.EXHAUSTED else BUDGET_UNAVAILABLE
        episode = guard.tier_event_seq if guard.available else guard.code
        self._note_once(SKIPPED_EVENT, {
            "reason": reason, "lifecycle_id": lifecycle, "policy_id": _policy_id(setup["state"]),
            "spend_guard": guard.facts(), "code": guard.code,
        }, f"position-review-skipped:{setup['setup_id']}:{lifecycle}:{reason}:{episode}",
            setup["setup_id"])

    @staticmethod
    def _guard_facts(guard, bar_seconds, reasons):
        """What a V3 request records about the guard; ``routine_weight`` is how many per-minute
        reviews it stands for (the cadence in minutes for a routine review, else 1)."""
        routine = cm.BAR_REASONS[bar_seconds] in reasons
        return {**guard.facts(), "review_bar_seconds": bar_seconds,
                "routine_weight": bar_seconds // 60 if routine else 1}

    def _unavailable(self, setup, reason, code):
        lifecycle = (setup.get("state") or {}).get("lifecycle_id")
        self._note_once(UNAVAILABLE_EVENT, {
            "reason": reason, "code": code, "lifecycle_id": lifecycle,
            "policy_id": _policy_id(setup.get("state")),
        }, f"maintenance-unavailable:{setup['setup_id']}:{lifecycle}:{reason}:{code}",
            setup["setup_id"])

    async def _due(self, setup, row, now):
        """Record the triggers the quote shows; the due item, or None."""
        sid, state = setup["setup_id"], setup["state"]
        lifecycle = state.get("lifecycle_id")
        inflight = self._inflight.get(str(sid))
        bid = _num(row["bid"]) if row and row.get("bid") else None
        if inflight is not None:
            if bid is not None:  # Price seen while Jev answers (the crossing check).
                inflight["low"] = bid if inflight.get("low") is None else min(inflight["low"], bid)
                inflight["high"] = bid if inflight.get("high") is None else max(
                    inflight["high"], bid)
            return None
        with self.store.repo.connect() as conn:
            pending = self._pending_request(conn, sid, lifecycle)
        if pending is not None:
            await self._recover(setup, pending["body"])
            return None
        if state.get("exit_requested") or state.get("stop_replace"):
            return None
        levels = {k: _num(v) for k, v in setup["record_json"]["levels"].items()}
        risk = cm.r_per_coin(levels)
        stop, target = _num(state["stop"]), _num(state["target"])
        opened_at = _aware(state["opened_at"])
        # V1 (15-minute bars), V2 (1-minute bars) or V3 (the spend guard's tier: 1, 5 or 15
        # minutes, or no review at all while the budget is exhausted).
        policy = cm.recorded_policy(state)
        bar_seconds, guard = policy.review_bar_seconds, None
        if cm.guarded(policy):
            guard = self.guard_decision or jev_budget.unavailable(
                now, "JEV_SPEND_GUARD_NOT_EVALUATED")
            bar_seconds = policy.review_bar_seconds_for(guard.tier)
        with self.store.repo.connect() as conn:  # Reads only: no ledger lock each pass.
            if not self._entry_complete(conn, sid, lifecycle):
                return None  # Reviews start once the entry is filled or its rest cancelled.
            entry = self._average_entry(conn, sid, state)
            last = self._last_request(conn, sid, lifecycle)
            recorded = {(r["body"]["trigger"], r["body"]["level"]) for r in self._triggers(
                conn, sid, lifecycle, 0)}
        served = (last["body"].get("trigger") or {}).get("served", {}) if last else {}
        served_bar_end = _aware(served["bar_end"]) if served.get("bar_end") else None
        if isinstance(policy, cm.MaintenancePolicyV2) and bar_seconds is not None:
            served_bar_end = self._skip_in_flight(setup, policy, last, served_bar_end,
                                                  opened_at, now, bar_seconds)
        # Triggers need the stream's current quote (a thin coin's unchanged quote is not
        # re-sent, so its age is recorded, not bounded); bars, news and shocks do not.
        found = []
        if bid is not None:
            quote = {"bid": row.get("bid"), "ask": row.get("ask"),
                     "quote_at": row.get("quote_at"),
                     "quote_received_at": _text(row.get("quote_received_at"))}
            reached = cm.milestone(bid, entry, risk)
            found += [(cm.R_MILESTONE, str(n)) for n in range(1, reached + 1)]
            if cm.near_target(bid, target):
                found.append((cm.NEAR_TARGET, str(target)))
            if cm.near_stop(bid, stop):
                found.append((cm.NEAR_STOP, str(stop)))
        new = [(kind, level) for kind, level in found if (kind, level) not in recorded]
        if new:
            with self.store.transaction() as conn:
                for kind, level in new:
                    self._event(conn, cm.TRIGGER_EVENT, {
                        "policy_id": _policy_id(state), "lifecycle_id": lifecycle,
                        "trigger": kind, "level": level, "entry": entry, "risk_per_coin": risk,
                        "stop": stop, "target": target, "quote": quote, "at": now,
                    }, sid, f"maintenance-trigger:{sid}:{lifecycle}:{kind}:{level}")
        if bar_seconds is None:  # V3: the budget allows no review now (triggers wait).
            self._withheld_by_budget(setup, guard)
            return None
        with self.store.repo.connect() as conn:
            unserved = [(r["body"]["trigger"], r["body"]["level"], r["event_seq"])
                        for r in self._triggers(conn, sid, lifecycle,
                                                served.get("trigger_seq") or 0)]
            news_revision = self._news_revision(conn, setup, lifecycle)
            shocks = self._shocks(conn, served.get("shock_seq") or 0, opened_at)
        reasons, exempt = cm.due_reasons(
            now=now, opened_at=opened_at, served_bar_end=served_bar_end,
            unserved_triggers=[(k, level) for k, level, _ in unserved],
            news_revision=news_revision,
            served_news_revision=served.get("news_revision", setup["revision"]),
            shocks=[r["event_seq"] for r in shocks],
            last_requested_at=_aware(last["body"]["requested_at"]) if last else None,
            bar_seconds=bar_seconds,
        )
        if not reasons:
            return None
        return {
            "setup": setup, "setup_id": sid, "lifecycle_id": lifecycle, "row": row, "bid": bid,
            "stop": stop, "target": target, "entry": entry, "risk": risk, "levels": levels,
            "reasons": reasons, "near_target_exempt": exempt,
            "triggers": [{"trigger": k, "level": level, "event_seq": seq}
                         for k, level, seq in unserved],
            "shocks": [r["event_seq"] for r in shocks], "news_revision": news_revision,
            "spend_guard": self._guard_facts(guard, bar_seconds, reasons)
            if guard is not None else None,
            "served": {
                "bar_end": cm.floor_time(now, bar_seconds).isoformat(),
                "trigger_seq": max([seq for *_, seq in unserved],
                                   default=served.get("trigger_seq") or 0),
                "news_revision": news_revision,
                "shock_seq": max([r["event_seq"] for r in shocks],
                                 default=served.get("shock_seq") or 0),
            },
        }

    def _skip_in_flight(self, setup, policy, last, served_bar_end, opened_at, now, bar_seconds):
        """``CRYPTO_MAINTENANCE_V2`` (and V3): the routine bars already served. A bar that
        completed while the trade's previous review was in flight (requested before the bar's
        end, decided at or after it) is recorded once (``MAINTENANCE_REVIEW_SKIPPED``,
        ``REVIEW_SKIPPED_IN_FLIGHT``) and counts as served: it is never reviewed late.
        ``bar_seconds`` is the cadence (V2: 60; V3: its tier's)."""
        sid, lifecycle = setup["setup_id"], setup["state"].get("lifecycle_id")
        boundary = cm.floor_time(now, bar_seconds)
        if last is None or boundary <= opened_at or (
                served_bar_end is not None and boundary <= served_bar_end):
            return served_bar_end  # Most passes: this minute is already served.
        with self.store.repo.connect() as conn:
            skipped = self._last_skipped_bar(conn, sid, lifecycle)
            if skipped is not None and (served_bar_end is None or skipped > served_bar_end):
                served_bar_end = skipped
            if served_bar_end is not None and boundary <= served_bar_end:
                return served_bar_end
            request_id = last["body"]["request_id"]
            requested_at = _aware(last["body"]["requested_at"])
            decided_at = self._decided_at(conn, sid, request_id)
        if not cm.in_flight_at(boundary, requested_at=requested_at, decided_at=decided_at):
            return served_bar_end
        self._note_once(cm.MINUTE_SKIPPED_EVENT, {
            "policy_id": policy.policy_id, "lifecycle_id": lifecycle,
            "code": cm.REVIEW_SKIPPED_IN_FLIGHT, "bar_end": boundary.isoformat(),
            "in_flight_request_id": request_id, "in_flight_requested_at": _text(requested_at),
            "in_flight_decided_at": _text(decided_at), "recorded_at": now.isoformat(),
        }, f"maintenance-review-skipped:{sid}:{lifecycle}:{boundary.isoformat()}", sid)
        return boundary

    # --- the request ---------------------------------------------------------------------

    def _selection_judgment(self, receipt_id):
        """The receipt-verified answers of one selection review, or None."""
        from catalyst_lab.jev_contract import QuestionSet

        if not receipt_id:
            return None
        if not self.reviewer.store.verify(receipt_id)["valid"]:
            raise ValueError("SELECTION_RECEIPT_INTEGRITY_FAILED")
        with self.reviewer.store.connect() as conn:
            row = conn.execute(
                """SELECT r.response_bytes,r.outcome,q.question_set_version,q.stage,q.request_json
                FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
                WHERE r.receipt_id=%s""", (receipt_id,),
            ).fetchone()
        if not row or row["outcome"] != "VALID":
            return None
        template = QuestionSet(row["question_set_version"], row["stage"],
                               encoded(strict_json(row["request_json"])["questions"]))
        return {"receipt_id": str(receipt_id),
                "question_set_version": row["question_set_version"],
                "answers": validated_answers(bytes(row["response_bytes"]), template)}

    @staticmethod
    def _changes(conn, setup_id, lifecycle):
        """Every applied stop and target change of this lifecycle and why, oldest first: the
        maintenance decisions applied and, under CRYPTO_24H_REVIEW_V1 (package day-review), the
        levels a 24-hour review's continue raised (reason ``DAY_REVIEW``)."""
        rows = conn.execute(
            """SELECT kind,body FROM lab.managed_events WHERE setup_id=%s
            AND kind IN (%s,'DAY_REVIEW_DECISION') AND body->>'lifecycle_id'=%s
            AND ((kind=%s AND body->>'outcome'=%s)
                 OR (kind='DAY_REVIEW_DECISION' AND body->>'level_change'='APPLIED'))
            ORDER BY event_seq""",
            (setup_id, DECISION_EVENT, lifecycle, DECISION_EVENT, APPLIED),
        ).fetchall()
        changes = []
        for row in rows:
            body = row["body"]
            if row["kind"] != DECISION_EVENT:
                body = {**body, "trigger_reasons": ["DAY_REVIEW"]}
            for kind in ("stop", "target"):
                change = body.get(kind)
                if change and change.get("new") is not None:
                    changes.append({
                        "at": body["decided_at"], "kind": kind.upper(), "old": change["old"],
                        "new": change["new"], "option": change.get("option_id"),
                        "bases": change.get("bases"), "reasons": body.get("trigger_reasons"),
                    })
        return changes

    @staticmethod
    def _news_since_entry(conn, setup_id, lifecycle):
        rows = conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind='POSITION_NEWS' AND setup_id=%s
            AND body->>'lifecycle_id'=%s ORDER BY event_seq""", (setup_id, lifecycle),
        ).fetchall()
        items = []
        for row in rows:
            for source in row["body"].get("sources") or ():
                items.append({**source, "received_at": row["body"].get("received_at")})
        return items

    def _snapshot(self, item):
        """The broker view a review needs; None (and nothing sent) otherwise."""
        sid = item["setup_id"]
        setup, state, orders, roles, position, uncertain = self.execution._broker_view(
            sid, read_only=True)
        if (uncertain or not position or state.get("state") != "OPEN"
                or state.get("lifecycle_id") != item["lifecycle_id"]
                or state.get("exit_requested") or state.get("stop_replace")):
            return None
        qty = _num(position["qty"])
        live = {"new", "accepted", "partially_filled", "pending_new", "held"}
        if qty <= 0 or any(roles[o["id"]] == "ENTRY" and o["status"] in live for o in orders):
            return None
        protection = [o for o in orders if roles[o["id"]] == "PROTECT" and o["status"] in live]
        covered = sum((_num(o["qty"]) - _num(o.get("filled_qty") or "0") for o in protection),
                      D(0))
        if not protection or covered != qty:
            return None
        return setup, state, qty, protection

    async def _request(self, item, rank):
        """Build and record one review request (the recorded version's context: V4, or V5 with
        the 1-minute bars and the review history); None when it cannot be built."""
        sid, lifecycle = item["setup_id"], item["lifecycle_id"]
        snapshot = await asyncio.to_thread(self._snapshot, item)
        if snapshot is None:
            return None
        setup, state, qty, protection = snapshot
        # The setup's own version (V1 or V2, recorded at admission): its record is the
        # context's policy, so the answer is read by that version's rule even after a restart.
        policy = cm.recorded_policy(state)
        v5 = policy.context_version == cm.CONTEXT_V5_VERSION
        if (_num(state["stop"]), _num(state["target"])) != (item["stop"], item["target"]):
            return None
        quote = self._quote(setup["symbol"], item["row"])
        if quote is None:
            self._unavailable(setup, "CURRENT_QUOTE_UNAVAILABLE", "LIVE_PRICE_UNAVAILABLE")
            return None
        if quote.bid <= item["stop"]:
            return None  # The stop is firing: protection owns the trade now.
        bars, issues = await self._bar_sets(setup["symbol"], minute=v5)
        if issues:
            self._unavailable(setup, "COMPLETED_BARS_UNAVAILABLE",
                              sorted({i.code for i in issues})[0])
            return None
        minute_bars = bars.pop("bars_1m", ())
        now = self.now()
        increment = _num(await asyncio.to_thread(
            lambda: self.execution.broker.asset(setup["symbol"])["price_increment"]))
        packet = setup["record_json"]
        pick = packet.get("state") or packet
        with self.store.repo.connect() as conn:
            best = conn.execute(
                """SELECT max((body->>'bid')::numeric) AS high,min((body->>'bid')::numeric) AS low
                FROM lab.managed_events WHERE setup_id=%s AND kind='POSITION_MARKET_SNAPSHOT'
                AND body->>'lifecycle_id'=%s""", (sid, lifecycle),
            ).fetchone()
            milestone = conn.execute(
                """SELECT max((body->>'level')::int) AS n FROM lab.managed_events
                WHERE setup_id=%s AND kind=%s AND body->>'lifecycle_id'=%s
                AND body->>'trigger'=%s""", (sid, cm.TRIGGER_EVENT, lifecycle, cm.R_MILESTONE),
            ).fetchone()["n"] or 0
            changes = self._changes(conn, sid, lifecycle)
            news = self._news_since_entry(conn, sid, lifecycle)
            history = self.review_history(conn, sid, lifecycle) if v5 else ()
        entry, risk = item["entry"], item["risk"]
        best_bid = max(v for v in (best["high"], quote.bid,
                                   entry + risk * milestone if milestone else None)
                       if v is not None)
        worst_bid = min(v for v in (best["low"], quote.bid) if v is not None)
        options = {
            "stop": [o.record() for o in cm.stop_options(
                bars_15m=bars["bars_15m"], bars_1h=bars["bars_1h"], now=now, bid=quote.bid,
                entry=entry, current_stop=item["stop"], best_bid=best_bid, risk=risk,
                increment=increment)],
            "target": [o.record() for o in cm.target_options(
                bars_15m=bars["bars_15m"], bars_1h=bars["bars_1h"], now=now, bid=quote.bid,
                current_target=item["target"], increment=increment)],
        }
        selection = {
            "pick_review": self._selection_judgment(packet.get("receipt_id")),
            "quality": self._selection_judgment(packet.get("quality_receipt_id")),
        }
        trigger = {"reasons": item["reasons"], "priority_rank": rank}
        trade = {
            "entry": entry, "qty": qty, "initial_stop": item["levels"]["stop"],
            "initial_target": item["levels"]["target"],
            "max_entry": item["levels"]["max_entry_price"], "risk": risk,
            "stop": item["stop"], "target": item["target"], "bid": quote.bid, "ask": quote.ask,
            "quote_at": quote.read_at, "opened_at": state["opened_at"],
            # CRYPTO_24H_REVIEW_V1 records T (package day-review); the hold's T is its exit.
            "review_at": state.get("day_review_at") or state.get("hard_exit_at"),
            "best_bid": best_bid,
            "worst_bid": worst_bid, "milestone": milestone,
        }
        request_id = str(uuid4())
        try:
            compiled = compile_state_for(
                policy, bars_1m=minute_bars, history=history,
                budget=STATE_BYTE_BUDGET, now=now, symbol=setup["symbol"], pick=pick,
                selection=selection, trade=trade, changes=changes, news=news, options=options,
                trigger=trigger, **bars,
            )
        except ContextBudgetUnsatisfiable as exc:
            self._note_once(SKIPPED_EVENT, {
                "reason": exc.code, "lifecycle_id": lifecycle,
                "state_byte_budget": exc.budget, "smallest_state_bytes": exc.state_bytes,
                "manifest": exc.manifest, "policy_id": _policy_id(state),
            }, f"position-review-skipped:{sid}:{lifecycle}:{exc.code}", sid)
            return None
        expires = min(now + timedelta(seconds=cm.REVIEW_DEADLINE_SECONDS),
                      _aware(state["hard_exit_at"]))
        if expires <= now:
            return None  # The 24-hour exit is due: nothing is left to maintain.
        identity = {
            "candidate_id": str(sid), "position_id": str(sid), "lifecycle_id": lifecycle,
            "context_revision": state["revision"], "news_revision": item["news_revision"],
            "exit_policy": EXIT_POLICY, "cohort": MANAGED_COHORT,
            "strategy_version": setup["strategy_version"],
            "maintenance_policy_id": policy.policy_id,
        }
        if item.get("spend_guard") is not None:  # V3: the meter reads routine_weight here.
            identity["spend_guard"] = item["spend_guard"]
        context_json = encoded(json_safe({
            "context_version": policy.context_version, "identity": identity,
            "state": compiled.state, "options": options,
            "basis": {"stop": item["stop"], "target": item["target"], "entry": entry,
                      "risk_per_coin": risk, "increment": increment, "qty": qty,
                      "quote": quote.evidence(), "bid": quote.bid},
            "policy": policy.record(), "expires_at": expires.isoformat(),
            "manifest": compiled.manifest,
            "retained": {"protection_orders": [o["id"] for o in protection],
                         "trigger_events": [t["event_seq"] for t in item["triggers"]],
                         "shock_events": item["shocks"]},
        }))
        context = ManagedContext(context_json, digest(context_json))
        served_key = digest(encoded(json_safe(item["served"])))
        with self.store.transaction() as conn:
            current = self.store.state(conn, sid)
            if (current.get("revision") != state["revision"]
                    or self._pending_request(conn, sid, lifecycle) is not None):
                return None
            row = self._event(conn, REQUEST_EVENT, {
                "request_id": request_id, "context_hash": context.context_hash,
                "context": context.data, "expires_at": context.expires_at, "requested_at": now,
                "trigger": {"policy_id": policy.policy_id, "reasons": item["reasons"],
                            "near_target_exempt": item["near_target_exempt"],
                            "triggers": item["triggers"], "shock_events": item["shocks"],
                            "priority_rank": rank, "served": item["served"],
                            **({"spend_guard": item["spend_guard"]}
                               if item.get("spend_guard") is not None else {})},
            }, sid, f"maintenance-review:{sid}:{lifecycle}:{served_key}")
            if row["body"]["request_id"] != request_id:
                return None  # These facts were already served by an earlier request.
        self._inflight[str(sid)] = {"request_id": request_id, "low": quote.bid,
                                    "high": quote.bid}
        return {"setup": setup, "setup_id": sid, "lifecycle_id": lifecycle,
                "request_id": request_id, "context": context, "requested_at": now,
                "reasons": item["reasons"]}

    # --- the review and the decision -----------------------------------------------------

    async def _review(self, request):
        context = request["context"]
        result = await self.reviewer.jev_review(
            request_id=request["request_id"], identity=context.identity, state=context.state,
            question_set=questions_for(context), expires_at=context.expires_at,
            purpose=PURPOSE,
        )
        result = verify_maintenance_receipts(self.reviewer.store, context, result,
                                             expected_request_id=request["request_id"])
        return self.decide(request["setup_id"], request["request_id"], context, result,
                           request["requested_at"], request["reasons"])

    def _answered_at(self, result):
        if not result.receipt_ids:
            return None
        with self.reviewer.store.connect() as conn:
            row = conn.execute("SELECT completed_at FROM lab.jev_receipts WHERE receipt_id=%s",
                               (result.receipt_ids[-1],)).fetchone()
        return row["completed_at"] if row else None

    def decide(self, setup_id, request_id, context, result, requested_at, reasons):
        """Exactly one outcome for one answered (or failed) review; returns the decision body.

        The answer is read by the rule of the version the request's context records
        (``answer_reader``): V1's consistency rules, or ``MAINTENANCE_ANSWER_RULE_V2`` whose
        decision body also records ``answer_rule`` and ``option_use``. ``review_status`` stays
        the transport's whole-answer flag (any uncertain answer), which V2 does not act on."""
        now = self.now()
        basis = context.data["basis"]
        options = context.data["options"]
        policy = cm.policy_from_record(context.data["policy"])
        lifecycle = context.identity["lifecycle_id"]
        answered_at = self._answered_at(result)
        setup, state = self.execution._load(setup_id)
        row = self._current_row(setup)
        quote = self._quote(setup["symbol"], row)
        body = {
            "policy_id": policy.policy_id, "lifecycle_id": lifecycle,
            "request_id": request_id, "context_hash": context.context_hash,
            "receipt_ids": list(result.receipt_ids), "requested_at": _text(requested_at),
            "answered_at": _text(answered_at), "decided_at": now.isoformat(),
            "trigger_reasons": reasons, "review_status": result.status,
            "quote": quote.evidence() if quote else None,
            "levels_before": {"stop": state.get("stop"), "target": state.get("target")},
            "entry": basis["entry"], "risk_per_coin": basis["risk_per_coin"],
            "qty": basis["qty"],
        }
        if result.status != "RECORDED" and result.reason != "UNCERTAIN_JUDGMENT":
            return self._record(setup_id, request_id, {
                **body, "outcome": FAILED, "code": result.reason or "REVIEW_UNAVAILABLE",
                "action": None, "answers": None})
        reader = answer_reader(context.data["policy"])
        answer = reader(result.answers, options) if result.answers else None
        if answer is not None:
            body.update(action=answer.action, trade_reason=answer.trade_reason,
                        answers=answer.summary)
            if isinstance(answer, MaintenanceAnswerV2):  # V1's bodies keep their fields.
                body.update(answer_rule=answer.answer_rule, option_use=answer.option_use)
        discard = self._discard_reason(state, lifecycle, basis, quote, requested_at, now,
                                       setup_id)
        if discard is not None:
            return self._record(setup_id, request_id, {**body, "outcome": DISCARDED,
                                                       "code": discard}, obsolete=discard)
        if answer is None or answer.code is not None:
            code = answer.code if answer is not None else result.reason or "UNCERTAIN_JUDGMENT"
            return self._record(setup_id, request_id, {**body, "outcome": REFUSED, "code": code})
        if answer.action == cm.HOLD:
            return self._record(setup_id, request_id, {**body, "outcome": HELD, "code": None})
        if answer.flagged:
            return self._flag(setup_id, request_id, context, result, body, quote, now)
        if not answer.changes:  # V2 only: a raise with no usable option answer.
            return self._record(setup_id, request_id, {**body, "outcome": HELD,
                                                       "code": answer.held_code})
        return self._apply(setup_id, request_id, context, answer, body, quote, answered_at, now,
                           requested_at)

    def _current_row(self, setup):
        observe = getattr(self, "observe", None)
        return observe(setup) if callable(observe) else None

    def _discard_reason(self, state, lifecycle, basis, quote, requested_at, now, setup_id):
        """The stop fired, or the trade closed or began exiting, while Jev answered."""
        if state.get("state") != "OPEN" or state.get("lifecycle_id") != lifecycle:
            return "POSITION_CLOSED_DURING_REVIEW"
        if state.get("exit_requested"):
            return "EXIT_IN_PROGRESS_DURING_REVIEW"
        stop = _num(basis["stop"])
        current = quote.bid if quote is not None else _num(basis["bid"])
        low, _ = self._bid_extremes(setup_id, lifecycle, _aware(requested_at), current)
        if low <= stop:
            return "STOP_CROSSED_DURING_REVIEW"
        return None

    def _record(self, setup_id, request_id, body, *, obsolete=None):
        with self.store.transaction() as conn:
            existing = conn.execute("SELECT body FROM lab.managed_events WHERE idempotency_key=%s",
                                    ("maintenance-decision:" + request_id,)).fetchone()
            if existing:
                return existing["body"]
            self._write_decision(conn, setup_id, request_id, body, obsolete=obsolete)
        return json_safe(body)

    def _write_decision(self, conn, setup_id, request_id, body, *, obsolete=None):
        self._event(conn, DECISION_EVENT, body, setup_id, "maintenance-decision:" + request_id)
        if obsolete:
            self._event(conn, OBSOLETE_EVENT, {
                "request_id": request_id, "receipt_ids": body["receipt_ids"], "reason": obsolete,
            }, setup_id, "position-obsolete:" + request_id)
        else:
            self._event(conn, JUDGMENT_EVENT, {
                "context_hash": body["context_hash"], "action": body.get("action"),
                "reason": body.get("code"), "receipt_ids": body["receipt_ids"],
                "request_id": request_id, "outcome": body["outcome"],
                "policy_id": body["policy_id"],
            }, setup_id, "maintenance-judgment:" + request_id)

    def _flag(self, setup_id, request_id, context, result, body, quote, now):
        """Jev's early-exit flag (plan 4.6.3): recorded and alerted; levels unchanged."""
        with self.store.transaction() as conn:
            existing = conn.execute("SELECT body FROM lab.managed_events WHERE idempotency_key=%s",
                                    ("maintenance-decision:" + request_id,)).fetchone()
            if existing:
                return existing["body"]
            flag, created = exit_flags.raise_exit_flag(
                self.store, conn, setup_id=setup_id, lifecycle_id=body["lifecycle_id"],
                side="JEV",
                raised_by={"request_id": request_id, "receipt_ids": list(result.receipt_ids),
                           "context_hash": context.context_hash},
                reasons={"trade_reason": body.get("trade_reason"),
                         "action": body.get("action"), "answers": body.get("answers"),
                         "trigger_reasons": body["trigger_reasons"]},
                evidence={"levels": body["levels_before"], "quote": body["quote"],
                          "entry": body["entry"], "risk_per_coin": body["risk_per_coin"]},
                raised_at=now, reference=request_id,
            )
            full = {**body, "outcome": FLAGGED, "code": None,
                    "flag_id": flag["body"]["flag_id"], "flag_created": created}
            self._write_decision(conn, setup_id, request_id, full)
        return json_safe(full)

    def _apply(self, setup_id, request_id, context, answer, body, quote, answered_at, now,
               requested_at):
        """The checks before applying (code) and the change, in one ledger transaction."""
        basis, options = context.data["basis"], context.data["options"]
        chosen = {kind: next((o for o in options[kind]
                              if o["option_id"] == option_to_raise(answer, kind)), None)
                  for kind in ("stop", "target")}
        old_stop, old_target = _num(basis["stop"]), _num(basis["target"])
        new_stop = _num(chosen["stop"]["price"]) if chosen["stop"] else None
        new_target = _num(chosen["target"]["price"]) if chosen["target"] else None
        change = {
            kind: {"old": str(old), "new": str(new) if new is not None else None,
                   "option_id": chosen[kind]["option_id"] if chosen[kind] else None,
                   "bases": chosen[kind]["bases"] if chosen[kind] else None}
            for kind, old, new in (("stop", old_stop, new_stop),
                                   ("target", old_target, new_target))
        }
        with self.store.transaction() as conn:
            existing = conn.execute("SELECT body FROM lab.managed_events WHERE idempotency_key=%s",
                                    ("maintenance-decision:" + request_id,)).fetchone()
            if existing:
                return existing["body"]
            state = self.store.state(conn, setup_id)
            code = None
            if quote is None:
                code = "CURRENT_QUOTE_UNAVAILABLE"
            elif (state.get("state") != "OPEN" or state.get("lifecycle_id") != body["lifecycle_id"]
                  or state.get("exit_requested")):
                code = "POSITION_NOT_OPEN"
            elif state.get("stop_replace"):
                code = "STOP_REPLACE_IN_PROGRESS"
            elif state.get("account_risk"):
                code = "ACCOUNT_RISK_UNEVALUABLE"
            elif (_num(state["stop"]), _num(state["target"])) != (old_stop, old_target):
                code = "LEVELS_CHANGED_SINCE_REVIEW"
            else:
                low, high = self._bid_extremes(setup_id, body["lifecycle_id"],
                                               _aware(requested_at), quote.bid)
                code = cm.check_change(
                    old_stop=old_stop, new_stop=new_stop, old_target=old_target,
                    new_target=new_target, bid=quote.bid, min_bid=low, max_bid=high,
                    answered_at=answered_at or now, now=now,
                    increment=_num(basis["increment"]),
                )
            full = {**body, **change, "outcome": REFUSED if code else APPLIED, "code": code}
            if code is None:
                changes = {}
                if new_target is not None:
                    changes["target"] = str(new_target)
                if new_stop is not None:
                    changes["stop"] = str(new_stop)
                    changes["stop_replace"] = {
                        "change_id": request_id, "path": cm.PATCH_REPLACE,
                        "from_stop": str(old_stop), "to_stop": str(new_stop),
                        "applied_at": now.isoformat(),
                    }
                    full["stop_replace_path"] = cm.PATCH_REPLACE
                after = self.store.transition(conn, setup_id, state["state"], **changes)
                full["levels_after"] = {"stop": after["stop"], "target": after["target"]}
            self._write_decision(conn, setup_id, request_id, full)
        return json_safe(full)

    # --- recovery ------------------------------------------------------------------------

    async def _recover(self, setup, pending):
        """A request recorded before a crash: its recorded answer is used, never a second
        vote; without one it is closed once its deadline has passed."""
        request_id = pending["request_id"]
        context = ManagedContext(encoded(pending["context"]), pending["context_hash"])
        result = self.recover_receipt(request_id, questions_for(context))
        if result is None:
            if self.now() >= context.expires_at:
                self._record(setup["setup_id"], request_id, {
                    "policy_id": cm.policy_from_record(context.data["policy"]).policy_id,
                    "lifecycle_id": context.identity["lifecycle_id"], "request_id": request_id,
                    "context_hash": context.context_hash, "receipt_ids": [],
                    "requested_at": pending["requested_at"], "decided_at": self.now().isoformat(),
                    "outcome": DISCARDED, "code": "REVIEW_EXPIRED_DURING_RECOVERY",
                    "trigger_reasons": (pending.get("trigger") or {}).get("reasons"),
                }, obsolete="REVIEW_EXPIRED_DURING_RECOVERY")
            return None
        result = verify_maintenance_receipts(self.reviewer.store, context, result,
                                             expected_request_id=request_id)
        return self.decide(setup["setup_id"], request_id, context, result,
                           _aware(pending["requested_at"]),
                           (pending.get("trigger") or {}).get("reasons"))

    def recover_receipt(self, request_id, questions):
        with self.reviewer.store.connect() as conn:
            receipt = conn.execute(
                """SELECT * FROM lab.jev_receipts WHERE request_id=%s
                ORDER BY attempt DESC LIMIT 1""", (request_id,),
            ).fetchone()
        if not receipt:
            return None
        self.reviewer.store.verify(receipt["receipt_id"])
        answers = (validated_answers(bytes(receipt["response_bytes"]), questions)
                   if receipt["outcome"] == "VALID" else {})
        uncertain = answers and any(
            a.get("type") == "choice" and (a.get("choice") == "Insufficient evidence" or sum(
                p == max(a["probabilities"].values()) for p in a["probabilities"].values()) > 1)
            for a in answers.values())
        return ReviewResult(str(request_id), "RECORDED" if answers and not uncertain
                            else "NEEDS_REVIEW",
                            "UNCERTAIN_JUDGMENT" if uncertain else receipt["error_code"],
                            (str(receipt["receipt_id"]),), answers)

    # --- status (alerts) -----------------------------------------------------------------

    def status(self, active):
        """Open maintained trades, pending exit flags and reviews that are failing, for the
        runtime status and the watchdog's alarms."""
        maintained = [s for s in active if cm.active(s.get("state"))
                      and (s.get("state") or {}).get("state") == "OPEN"]
        with self.store.repo.connect() as conn:
            flags = exit_flags.pending_exit_flags(conn)
            failing = []
            for setup in maintained:
                row = conn.execute(
                    """SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind=%s
                    AND body->>'lifecycle_id'=%s ORDER BY event_seq DESC LIMIT 1""",
                    (setup["setup_id"], DECISION_EVENT, setup["state"].get("lifecycle_id")),
                ).fetchone()
                if row and row["body"].get("outcome") == FAILED:
                    failing.append({"setup_id": str(setup["setup_id"]), "symbol": setup["symbol"],
                                    "code": row["body"].get("code"),
                                    "decided_at": row["body"].get("decided_at")})
        by_policy = {}
        for setup in maintained:
            key = _policy_id(setup.get("state"))
            by_policy[key] = by_policy.get(key, 0) + 1
        return {
            # The version admission records now; open trades keep the one they recorded.
            "policy_id": cm.ADMITTED_MAINTENANCE.policy_id,
            "open_trades_by_policy": dict(sorted(by_policy.items())),
            "management_reviews": self.management_reviews,
            "open_trades": len(maintained),
            "pending_exit_flags": len(flags),
            "oldest_exit_flag_raised_at": flags[0]["body"]["raised_at"] if flags else None,
            "exit_flags": [{"flag_id": f["body"]["flag_id"], "setup_id": str(f["setup_id"]),
                            "side": f["body"]["side"], "raised_at": f["body"]["raised_at"]}
                           for f in flags],
            "failing_reviews": failing,
            "last_pass_at": self.last_pass_at.isoformat() if self.last_pass_at else None,
        }


def _text(value):
    return value.isoformat() if isinstance(value, datetime) else value


def maintenance_uuid(*parts):
    return str(uuid5(NAMESPACE_URL, ":".join(str(p) for p in parts)))
