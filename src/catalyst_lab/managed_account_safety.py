"""Single-owner account safety joining the managed and frozen paper ledgers.

The runtime calls tick even when there are no managed setups. Legacy mutations
reuse RiskSafety/RiskDispatcher and a separately database-gated paper client;
managed positions retain their market-specific protective controller. There is
no entry API and no new authorization bypass in this coordinator.

An operator flatten (``managed_ops operator flatten-all``, a durable request from migration
015) runs through the daily halt's own sequence: an ``OPERATOR_FLATTEN`` execution halt blocks
entries, WATCHING setups are invalidated, every working managed setup gets an exit request that
its protective controller carries out (cancels first, then the close, each with its own exact
one-use five-second authorization) and legacy exposure gets durable exit requests. Every tick
then compares both ledgers and the broker. Once nothing is left, each pending request gets its
one COMPLETED completion (migration 019). A refused or unknown mutation, or exposure the app
cannot act on, is recorded as PARTIAL history, a failed pass as FAILED; the request stays pending
and the next tick carries on. Failures still reach the runtime's per-cause latches, which clear
themselves, and nothing here sends an order itself.
"""

import re
from uuid import uuid4

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.managed_execution import error_code, normalized_symbol, num
from catalyst_lab.managed_store import TERMINAL
from catalyst_lab.market import NY
from catalyst_lab.repository import json_safe
from catalyst_lab.risk import RiskEngine, RiskPolicy, signed
from catalyst_lab.risk_dispatch import RiskDispatcher
from catalyst_lab.risk_safety import RiskSafety

OPERATOR_FLATTEN = "OPERATOR_FLATTEN"
FLATTEN_EXECUTED = "OPERATOR_FLATTEN_EXECUTED"
WORKING_STATES = frozenset({"ENTRY_PENDING", "ORDER_SUBMITTED", "OPEN"})
# Exits still in flight: later ticks clear these by themselves, so no history row is written.
IN_PROGRESS_CODES = frozenset(
    {"MANAGED_EXPOSURE_OPEN", "LEGACY_EXPOSURE_OPEN", "AUTHORIZATION_UNRESOLVED"}
)
# A refused or unknown cancel or close is recorded as PARTIAL at once. Any other residual code
# (halted protection, exposure no ledger owns) is recorded as PARTIAL once nothing is in flight
# any more, because only the operator or the broker can clear it.
FAILURE_CODES = frozenset({"BROKER_REFUSED", "BROKER_RESPONSE_UNKNOWN"})
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def residual_code(exc):
    """The failure's own UPPER_SNAKE code for a FAILED completion, never free text."""
    code = error_code(exc)
    return code if _CODE.fullmatch(code) else "FLATTEN_PASS_FAILED"


class _LegacySafety(RiskSafety):
    """Process only legacy-owned requests; never take over a managed symbol."""

    EXIT_REASONS = RiskSafety.EXIT_REASONS | {OPERATOR_FLATTEN}

    def __init__(self, dispatcher, coordinator):
        super().__init__(dispatcher)
        self.coordinator = coordinator

    @staticmethod
    def flatten_orders(rows):
        # Frozen order normalization deliberately rejects fractional crypto.
        # Those orders belong to the managed controller on this shared account.
        stocks = [row for row in rows if row.get("asset_class") != "crypto"
                  and "/" not in row.get("symbol", "")]
        return RiskSafety.flatten_orders(stocks)

    def daily_halt(self):
        for row in self.coordinator.legacy_exposure():
            self.request_exit(row["ticker"], "DAILY_RISK_HALT", row["candidate_id"])
        self.process_exits()

    def operator_flatten(self):
        """The daily halt's legacy sequence under the operator's own exit reason."""
        for row in self.coordinator.legacy_exposure():
            self.request_exit(row["ticker"], OPERATOR_FLATTEN, row["candidate_id"])
        self.process_exits()

    def process_exits(self):
        with self.dispatcher.lock:
            with self.repo.connect() as conn:
                requests = conn.execute(
                    "SELECT * FROM lab.pending_risk_exits WHERE candidate_id IS NOT NULL "
                    "ORDER BY event_seq"
                ).fetchall()
            for request in requests:
                if self.coordinator.legacy_exit_owned(request):
                    self._process_exit(request)

    def _cancel(self, order, reason, exit_request=None):
        """A late parent fill can reactivate a child after an acknowledged cancel.

        Recover the old request first. A fresh active broker state after a known
        acknowledgement permits another DELETE of that same order ID, with a new
        exact authorization. Pending/uncertain cancellation is never a flat proof.
        """
        if exit_request is None:
            return super()._cancel(order, reason, exit_request)
        path = "/v2/orders/" + order["id"]
        with self.repo.connect() as conn:
            previous = conn.execute(
                """SELECT d.risk_decision_id,r.outcome FROM lab.risk_decisions d
                JOIN lab.authorization_claims c USING(risk_decision_id)
                JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE d.action='CANCEL' AND d.path=%s AND d.decision='APPROVED'
                ORDER BY d.decided_at DESC LIMIT 1""", (path,),
            ).fetchone()
        if not previous or previous["outcome"] not in {"ACCEPTED", "RECOVERED"}:
            return super()._cancel(order, reason, exit_request)
        self.dispatcher.recover(previous["risk_decision_id"])
        fresh = self.client.order(order["id"])
        if not fresh or fresh.get("status") not in {
            "new", "accepted", "partially_filled", "held", "accepted_for_bidding"
        }:
            return
        account = self.client.account()
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            decision = self.engine._decision(
                conn, candidate_id=exit_request["candidate_id"], action="CANCEL",
                session_date=self.engine.now().astimezone(NY).date(),
                equity=signed(account["equity"]), reason=reason, approved=True,
                method="DELETE", path=path,
                context={"broker_order": fresh,
                         "exit_request_id": str(exit_request["exit_request_id"]),
                         "recovery_of": str(previous["risk_decision_id"])},
            )
        self.dispatcher.dispatch(decision["risk_decision_id"])


class ManagedAccountSafety:
    def __init__(self, execution, legacy_client):
        self.execution = execution
        self.repo = execution.repo
        self.client = legacy_client
        # The frozen policy fixes authorization TTL to exactly five seconds.
        engine = RiskEngine(self.repo, legacy_client, RiskPolicy(), clock=execution.now)
        self.dispatcher = RiskDispatcher(engine)
        self.safety = _LegacySafety(self.dispatcher, self)
        # Completion rows name the process; ManagedRuntime replaces this with its runtime_id.
        self.runtime_id = str(getattr(execution, "process_run_id", None) or uuid4())
        self._flatten_started = {}
        self._flatten_status = None

    def legacy_exposure(self):
        with self.repo.connect() as conn:
            return conn.execute(
                """SELECT DISTINCT c.candidate_id,c.ticker FROM lab.candidates c
                WHERE EXISTS(SELECT 1 FROM lab.active_reservations r
                             WHERE r.candidate_id=c.candidate_id)
                   OR EXISTS(SELECT 1 FROM lab.strategy_positions p
                             WHERE p.candidate_id=c.candidate_id)
                   OR EXISTS(SELECT 1 FROM lab.pending_risk_exits x
                             WHERE x.candidate_id=c.candidate_id)"""
            ).fetchall()

    def _halt(self, reason, ticker):
        with self.execution.store.transaction() as conn:
            self.execution._latch_execution_halt(conn, reason, {"ticker": ticker})
        self.execution.reconciled_at = None

    def legacy_exit_owned(self, request):
        """Never cancel another controller's order or infer ownership from a symbol."""
        ticker = request["ticker"]
        with self.repo.connect() as conn:
            managed = conn.execute(
                """SELECT s.symbol FROM lab.managed_active_reservations r
                JOIN lab.managed_setups s USING(setup_id)"""
            ).fetchall()
            links = {
                row["broker_order_id"] for row in conn.execute(
                    """SELECT l.broker_order_id FROM lab.order_links l
                    JOIN lab.orders o USING(order_id) WHERE o.candidate_id=%s""",
                    (request["candidate_id"],),
                ).fetchall()
            }
            clients = {
                row["client_id"] for row in conn.execute(
                    "SELECT payload_json->>'client_order_id' AS client_id "
                    "FROM lab.risk_decisions WHERE candidate_id=%s AND decision='APPROVED'",
                    (request["candidate_id"],),
                ).fetchall() if row["client_id"]
            }
            uncertain_entries = conn.execute(
                """SELECT d.risk_decision_id FROM lab.risk_decisions d
                JOIN lab.authorization_claims c USING(risk_decision_id)
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE d.candidate_id=%s AND d.action='ENTRY' AND d.decision='APPROVED'
                AND (r.outcome IS NULL OR r.outcome IN ('UNKNOWN','NOT_FOUND'))""",
                (request["candidate_id"],),
            ).fetchall()
        # A broker position disappearing is not flat proof while an entry POST
        # may still be in flight. Reuse the original ID and retain its reservation.
        for row in uncertain_entries:
            if self.dispatcher.recover(row["risk_decision_id"]) is None:
                return False
        if uncertain_entries:
            # Recovery may have just registered the parent and its protective children.
            with self.repo.connect() as conn:
                links.update(row["broker_order_id"] for row in conn.execute(
                    """SELECT l.broker_order_id FROM lab.order_links l
                    JOIN lab.orders o USING(order_id) WHERE o.candidate_id=%s""",
                    (request["candidate_id"],),
                ).fetchall())
        if any(normalized_symbol(row["symbol"]) == normalized_symbol(ticker) for row in managed):
            self._halt("ACCOUNT_EXIT_OWNERSHIP_CONFLICT", ticker)
            return False
        orders = self.safety.flatten_orders(self.client.open_orders())
        if any(
            order["symbol"] == ticker
            and order["id"] not in links
            and order.get("client_order_id") not in clients
            for order in orders
        ):
            self._halt("ACCOUNT_EXIT_UNEXPLAINED_ORDER", ticker)
            return False
        return True

    def tick(self):
        """Latch account loss independent of setup count and advance durable exits.

        With a request-governed broker this reads the snapshot shared with every
        per-setup broker view in the same interval (default five seconds, refreshed
        early after trade updates and mutations) instead of three GETs per tick.

        While an operator flatten request is pending, its halt and exit requests are written
        first, so the managed controllers act on them in this same runtime tick, and the pass
        is evaluated even when the account step fails. The first failure is re-raised.
        """
        requests = self._pending_flattens()
        failure, reason = None, None
        if requests:
            try:
                self._begin_flatten(requests[0])
            except Exception as exc:
                failure = exc
        try:
            reason = self._account_tick(flatten=bool(requests))
        except Exception as exc:
            failure = failure or exc
        if requests:
            try:
                self._advance_flatten(requests, failure)
            except Exception as exc:
                failure = failure or exc
                self._flatten_failed(requests, failure)  # The first failure is the cause.
        if failure is not None:
            raise failure
        return reason

    def _account_tick(self, *, flatten):
        account, positions, cashflow, _ = self.execution.account_snapshot(shared=True)
        with self.execution.store.transaction() as conn:
            reason = self.execution._account_halt(conn, account, positions, cashflow)
            if reason == "DAILY_RISK_HALT":
                rows = conn.execute(
                    "SELECT setup_id,body FROM lab.managed_states"
                ).fetchall()
                for row in rows:
                    state = row["body"]
                    if state["state"] == "WATCHING":
                        self.execution.store.transition(
                            conn, row["setup_id"], "INVALIDATED", reason="DAILY_RISK_HALT"
                        )
                    elif state["state"] in {"ENTRY_PENDING", "ORDER_SUBMITTED", "OPEN"}:
                        if state.get("exit_requested") != "DAILY_RISK_HALT":
                            self.execution.store.transition(
                                conn, row["setup_id"], state["state"],
                                exit_requested="DAILY_RISK_HALT",
                            )
        if reason == "DAILY_RISK_HALT":
            self.safety.daily_halt()
        elif flatten:
            self.safety.operator_flatten()
        else:
            # Yesterday's incomplete exit remains active after midnight or restart.
            self.safety.process_exits()
        return reason

    # --- Operator flatten (migration 015 requests, 019 completions) --------------------

    def flatten_status(self):
        """The pending-request summary behind the watchdog's FLATTEN_PENDING alarm; None
        until the first tick has read the requests."""
        return self._flatten_status

    def _pending_flattens(self):
        with self.repo.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lab.pending_operator_flatten_requests ORDER BY event_seq"
            ).fetchall()
        now = self.execution.now()
        pending = {row["event_seq"] for row in rows}
        self._flatten_started = {
            seq: at for seq, at in self._flatten_started.items() if seq in pending
        }
        for row in rows:
            self._flatten_started.setdefault(row["event_seq"], now)
        # No per-tick timestamp: the runtime heartbeat records a status only when it changes.
        residual = (self._flatten_status or {}).get("residual", []) if rows else []
        self._flatten_status = {
            "pending_count": len(rows),
            "oldest_pending_request_seq": rows[0]["event_seq"] if rows else None,
            "oldest_pending_requested_at": rows[0]["requested_at"].isoformat() if rows else None,
            "residual": residual,
        }
        return rows

    def _begin_flatten(self, request):
        """What the daily halt does to managed setups, plus a halt that blocks every entry.

        Idempotent: the halt is written once while unreleased, a setup already exiting keeps
        its first exit reason, and nothing changes once every setup is exiting or terminal.
        """
        with self.execution.store.transaction() as conn:
            self.execution._latch_execution_halt(conn, OPERATOR_FLATTEN, {
                "request_seq": request["event_seq"],
                "request_id": str(request["request_id"]),
                "runtime_id": self.runtime_id,
                "scope": request["scope"],
            })
            for row in conn.execute("SELECT setup_id,body FROM lab.managed_states").fetchall():
                state = row["body"]
                if state["state"] == "WATCHING":
                    self.execution.store.transition(
                        conn, row["setup_id"], "INVALIDATED", reason=OPERATOR_FLATTEN
                    )
                elif state["state"] in WORKING_STATES and not state.get("exit_requested"):
                    self.execution.store.transition(
                        conn, row["setup_id"], state["state"], exit_requested=OPERATOR_FLATTEN
                    )

    def _broker_exposure(self, *, fresh):
        """Positions and open orders: the governed shared snapshot (a fresh one when asked),
        else direct reads."""
        snapshot = self.execution._shared_snapshot(max_age=0 if fresh else None)
        if snapshot is not None:
            return snapshot.position_rows(), snapshot.open_order_rows()
        return self.execution.broker.positions(), self.execution.broker.open_orders()

    def _flatten_exposure(self, since_seq, *, fresh=False):
        """Residual codes left after the flatten so far, with compact details."""
        with self.repo.connect() as conn:
            setups = conn.execute(
                """SELECT s.setup_id,s.symbol,s.market,t.body AS state,
                  (SELECT p.body->>'state' FROM lab.managed_events p WHERE p.setup_id=s.setup_id
                   AND p.kind='PROTECTION_PLAN' ORDER BY p.event_seq DESC LIMIT 1) AS plan
                FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
                ORDER BY s.event_seq"""
            ).fetchall()
            # The latest cancel or close of each setup since the request.
            latest = {row["setup_id"]: row for row in conn.execute(
                """SELECT DISTINCT ON(d.setup_id) d.setup_id,d.decision_id,d.action,
                  (d.context->>'state_revision')::bigint AS revision,
                  c.decision_id IS NOT NULL AS claimed,a.event_id IS NOT NULL AS acknowledged,
                  EXISTS(SELECT 1 FROM lab.managed_events x WHERE x.setup_id=d.setup_id
                    AND x.kind='BROKER_REJECTED' AND x.body->>'decision_id'=d.decision_id::text)
                    AS refused,
                  EXISTS(SELECT 1 FROM lab.managed_events u WHERE u.setup_id=d.setup_id
                    AND u.kind IN ('BROKER_UNKNOWN','BROKER_RECONCILE_REQUIRED')
                    AND u.body->>'decision_id'=d.decision_id::text) AS unknown
                FROM lab.managed_risk_decisions d
                LEFT JOIN lab.managed_claims c USING(decision_id)
                LEFT JOIN lab.managed_events a ON a.idempotency_key='ack:'||d.decision_id::text
                WHERE d.outcome='APPROVED' AND d.action IN ('CANCEL','EXIT') AND d.event_seq>%s
                ORDER BY d.setup_id,d.event_seq DESC""",
                (since_seq,),
            ).fetchall()}
            legacy_results = [row["outcome"] for row in conn.execute(
                """SELECT DISTINCT ON(x.exit_request_id) r.outcome FROM lab.pending_risk_exits x
                JOIN lab.risk_decisions d ON d.decision='APPROVED'
                  AND d.context_json->>'exit_request_id'=x.exit_request_id::text
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE x.reason=%s AND x.event_seq>%s
                ORDER BY x.exit_request_id,d.decided_at DESC""",
                (OPERATOR_FLATTEN, since_seq),
            ).fetchall()]
            unresolved = conn.execute(
                "SELECT lab.unresolved_authorization_claims() AS n"
            ).fetchone()["n"]
        legacy = self.legacy_exposure()
        positions, orders = self._broker_exposure(fresh=fresh)
        codes, owned, managed = set(), set(), []
        for row in setups:
            state = row["state"]
            if state.get("state") in TERMINAL:
                continue
            owned.add(normalized_symbol(row["symbol"]))
            codes.add("MANAGED_PROTECTION_HALTED" if row["plan"] == "HALTED"
                      else "MANAGED_EXPOSURE_OPEN")
            decision = latest.get(row["setup_id"])
            if decision is not None and decision["refused"]:
                codes.add("BROKER_REFUSED")
            elif decision is not None and decision["claimed"] and decision["unknown"] and (
                not decision["acknowledged"]
            ):
                codes.add("BROKER_RESPONSE_UNKNOWN")
            managed.append({"setup_id": str(row["setup_id"]), "symbol": row["symbol"],
                            "state": state.get("state"), "protection_plan": row["plan"]})
        for row in legacy:
            owned.add(normalized_symbol(row["ticker"]))
            codes.add("LEGACY_EXPOSURE_OPEN")
        for outcome in legacy_results:
            if outcome == "REJECTED":
                codes.add("BROKER_REFUSED")
            elif outcome in {"UNKNOWN", "NOT_FOUND"}:
                codes.add("BROKER_RESPONSE_UNKNOWN")
        if unresolved:
            codes.add("AUTHORIZATION_UNRESOLVED")
        unowned_positions = sorted({
            p["symbol"] for p in positions
            if num(p["qty"]) != 0 and normalized_symbol(p["symbol"]) not in owned
        })
        unowned_orders = sum(
            normalized_symbol(str(o.get("symbol", ""))) not in owned for o in orders
        )
        if unowned_positions:
            codes.add("UNOWNED_BROKER_POSITION")
        if unowned_orders:
            codes.add("UNOWNED_BROKER_ORDER")
        return {
            "codes": sorted(codes),
            "latest": latest,
            "setups": {row["setup_id"]: row for row in setups},
            "details": {
                "managed": managed,
                "legacy": [{"candidate_id": str(r["candidate_id"]), "ticker": r["ticker"]}
                           for r in legacy],
                "unowned_positions": unowned_positions,
                "unowned_orders": unowned_orders,
                "broker_positions": len(positions),
                "broker_open_orders": len(orders),
                "unresolved_claims": unresolved,
                "broker_read": "FRESH" if fresh else "SHARED_SNAPSHOT",
            },
        }

    def _flatten_counts(self, since_seq):
        """Cancels and closes authorized since the request: claimed, and broker-acknowledged."""
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT d.action,count(c.decision_id) AS attempted,
                  count(a.event_id) FILTER (WHERE c.decision_id IS NOT NULL) AS acknowledged
                FROM lab.managed_risk_decisions d
                LEFT JOIN lab.managed_claims c USING(decision_id)
                LEFT JOIN lab.managed_events a ON a.idempotency_key='ack:'||d.decision_id::text
                WHERE d.outcome='APPROVED' AND d.action IN ('CANCEL','EXIT') AND d.event_seq>%s
                GROUP BY d.action""",
                (since_seq,),
            ).fetchall() + conn.execute(
                """SELECT CASE d.action WHEN 'FLATTEN' THEN 'EXIT' ELSE d.action END AS action,
                  count(c.risk_decision_id) AS attempted,
                  count(c.risk_decision_id) FILTER (WHERE r.outcome IN ('ACCEPTED','RECOVERED'))
                    AS acknowledged
                FROM lab.risk_exit_requests x
                JOIN lab.risk_decisions d ON d.decision='APPROVED'
                  AND d.context_json->>'exit_request_id'=x.exit_request_id::text
                LEFT JOIN lab.authorization_claims c USING(risk_decision_id)
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE x.reason=%s AND x.event_seq>%s AND d.action IN ('CANCEL','FLATTEN')
                GROUP BY 1""",
                (OPERATOR_FLATTEN, since_seq),
            ).fetchall()
        counts = {name: {"attempted": 0, "acknowledged": 0} for name in ("cancels", "closes")}
        for row in rows:
            bucket = counts["cancels" if row["action"] == "CANCEL" else "closes"]
            bucket["attempted"] += row["attempted"]
            bucket["acknowledged"] += row["acknowledged"]
        return counts

    def _advance_flatten(self, requests, failure):
        """Record this pass: COMPLETED once flat (confirmed on a fresh broker read), FAILED
        when the pass failed, PARTIAL for a refused or unknown mutation or when only the
        operator or the broker can clear what is left, and nothing while exits are still in
        flight. Rows are keyed so that a lasting condition is written once per runtime, not
        once per tick. A refused close is retried by its controller, as outside a flatten:
        the refusal records one new state revision and the refused-close backoff
        (``ManagedExecution._exit_refused``), for crypto and stock closes alike."""
        since = requests[0]["event_seq"]
        exposure = self._flatten_exposure(since)
        if not exposure["codes"]:
            exposure = self._flatten_exposure(since, fresh=True)
        codes = exposure["codes"]
        self._flatten_status = {**(self._flatten_status or {}), "residual": codes}
        for request in requests:
            seq = request["event_seq"]
            if not codes:
                self._record_flatten(request, "COMPLETED", [], exposure,
                                     key=f"operator-flatten:{seq}:COMPLETED")
            elif failure is not None:
                code = residual_code(failure)
                self._record_flatten(
                    request, "FAILED", sorted(set(codes) | {code}), exposure,
                    key=f"operator-flatten:{seq}:{self.runtime_id}:FAILED:{code}",
                    failure={"code": code, "exception_class": type(failure).__name__},
                )
            elif set(codes) & FAILURE_CODES or not set(codes) & IN_PROGRESS_CODES:
                self._record_flatten(
                    request, "PARTIAL", codes, exposure,
                    key=f"operator-flatten:{seq}:{self.runtime_id}:PARTIAL:"
                    + digest(encoded(codes))[:16],
                )
        if not codes:  # Every pending request completed in this pass.
            self._flatten_status = {
                "pending_count": 0, "oldest_pending_request_seq": None,
                "oldest_pending_requested_at": None, "residual": [],
            }

    def _flatten_failed(self, requests, exc):
        """Best effort: the pass itself failed, so its evidence is the failure code alone."""
        code = residual_code(exc)
        failure = {"code": code, "exception_class": type(exc).__name__}
        self._flatten_status = {**(self._flatten_status or {}), "residual": [code]}
        for request in requests:
            try:
                self._record_flatten(
                    request, "FAILED", [code], {"details": {"evaluated": False}},
                    key=f"operator-flatten:{request['event_seq']}:{self.runtime_id}:FAILED:{code}",
                    failure=failure,
                )
            except Exception:
                return  # The original failure is re-raised to the runtime's latches.

    def _record_flatten(self, request, outcome, codes, exposure, *, key, failure=None):
        """One OPERATOR_FLATTEN_EXECUTED event and its audited completion row, together."""
        seq = request["event_seq"]
        counts = self._flatten_counts(seq)
        now = self.execution.now()
        started = min(self._flatten_started.get(seq, now), now)
        body = {
            "request_seq": seq,
            "request_id": str(request["request_id"]),
            "runtime_id": self.runtime_id,
            "outcome": outcome,
            "cancels": counts["cancels"],
            "closes": counts["closes"],
            "residual": list(codes),
            "exposure": exposure["details"],
            "started_at": started.isoformat(),
            "finished_at": now.isoformat(),
        }
        if failure is not None:
            body["failure"] = failure
        with self.execution.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone() or conn.execute(
                """SELECT 1 FROM lab.operator_flatten_completions
                WHERE request_seq=%s AND outcome='COMPLETED'""",
                (seq,),
            ).fetchone():
                return False
            self.execution.store.event(conn, FLATTEN_EXECUTED, json_safe(body), key=key)
            conn.execute(
                """INSERT INTO lab.operator_flatten_completions(request_seq,runtime_id,
                started_at,finished_at,cancels_attempted,cancels_acknowledged,closes_attempted,
                closes_acknowledged,residual_codes,outcome)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (seq, self.runtime_id, started, now,
                 counts["cancels"]["attempted"], counts["cancels"]["acknowledged"],
                 counts["closes"]["attempted"], counts["closes"]["acknowledged"],
                 list(codes), outcome),
            )
        return True

    def ingest(self, raw):
        """Route only known legacy order events through its durable fill projector."""
        body = raw.get("data", raw) if isinstance(raw, dict) else {}
        order = body.get("order")
        if not isinstance(order, dict):
            return False
        with self.repo.connect() as conn:
            owned = conn.execute(
                """SELECT 1 FROM lab.order_links WHERE broker_order_id=%s
                UNION ALL SELECT 1 FROM lab.risk_decisions
                WHERE decision='APPROVED' AND payload_json->>'client_order_id'=%s LIMIT 1""",
                (order.get("id"), order.get("client_order_id")),
            ).fetchone()
        if not owned:
            return False
        self.dispatcher.ingest(body, self.execution.now())
        return True

    def status(self):
        with self.repo.connect() as conn:
            pending = conn.execute(
                "SELECT count(*) AS count FROM lab.pending_risk_exits"
            ).fetchone()["count"]
            halted = conn.execute(
                "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s",
                (self.execution.now().astimezone(NY).date(),),
            ).fetchone()
        return {"daily_halt": bool(halted), "pending_account_exits": pending}
