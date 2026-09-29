"""Operator-only engineering acceptance; never used to admit a strategy candidate."""

from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.config import STRATEGY_VERSION
from catalyst_lab.domain import Candidate
from catalyst_lab.execution import bracket, system_event
from catalyst_lab.market import MarketDataError
from catalyst_lab.repository import json_safe


class EngineeringAcceptance:
    def __init__(self, engine):
        self.engine, self.repo, self.client = engine, engine.repo, engine.client

    def enroll(self, raw, *, timeout_seconds=60):
        candidate = Candidate.model_validate(raw)
        if not candidate.signal_id.startswith("TEST-") or candidate.catalyst != "ENGINEERING_TEST":
            raise ValueError("A TEST- signal and ENGINEERING_TEST catalyst are required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("Engineering run timeout must be 1–300 seconds")
        # These are engineering run limits, never strategy selection rules or invented evidence.
        bracket(candidate, 1, uuid4())  # Validate broker price precision and frozen long levels.
        if candidate.target - candidate.entry_trigger < 2 * (
            candidate.entry_trigger - candidate.stop
        ):
            raise ValueError("Engineering levels must also meet the frozen candidate 2R check")
        now = self.engine.now()
        session = self.engine.session()
        if session is None or not session.contains(now) or now >= session.flatten_time:
            raise MarketDataError("REGULAR_SESSION_REQUIRED")
        if not self.engine.ready():
            raise MarketDataError("STARTUP_RECONCILIATION_REQUIRED")
        asset = self.client.asset(candidate.ticker)
        if (
            not asset
            or asset.get("symbol") != candidate.ticker
            or asset.get("class") != "us_equity"
            or asset.get("tradable") is not True
            or asset.get("status") != "active"
        ):
            raise MarketDataError("ASSET_NOT_TRADABLE")
        quotes = self.client.quotes([candidate.ticker])
        quote = next((q for q in quotes if q.ticker == candidate.ticker), None)
        if quote is None or not 0 <= (self.engine.now() - quote.at).total_seconds() <= 5:
            raise MarketDataError("STALE_QUOTE")
        if quote.spread_bps is None or quote.spread_bps > self.engine.policy.max_spread_bps:
            raise MarketDataError("MAX_SPREAD")
        if quote.ask > candidate.max_entry_price:
            raise MarketDataError("PRICE_BEYOND_MAX_ENTRY")
        if self.client.positions() or self.client.open_orders():
            raise MarketDataError("ENGINEERING_ACCEPTANCE_REQUIRES_EMPTY_ACCOUNT")
        deadline = min(
            candidate.expires_at or session.closes,
            now + timedelta(seconds=timeout_seconds),
            session.flatten_time,
        )
        if deadline <= self.engine.now():
            raise MarketDataError("CANDIDATE_EXPIRED")
        candidate_id = uuid4()
        body = candidate.model_dump(mode="json") | {"expires_at": deadline.isoformat()}
        context = {
            "strategy_version": STRATEGY_VERSION,
            "date": session.session_date.isoformat(),
            "origin": "LOCAL_ENGINEERING_OPERATOR",
        }
        evidence = {
            "source": "ALPACA_PAPER_ENGINEERING",
            "official_open": session.opens,
            "official_close": session.closes,
            "asset": asset,
            "quote": quote.to_json(),
            "strategy_selection_checks": "NOT_APPLICABLE_ENGINEERING_TEST",
        }
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            if not self.engine.ready() or not session.contains(self.engine.now()):
                raise MarketDataError("STARTUP_RECONCILIATION_REQUIRED")
            self.engine._baseline(conn, session, {})
            if (
                conn.execute("SELECT 1 FROM lab.active_reservations LIMIT 1").fetchone()
                or conn.execute("SELECT 1 FROM lab.pending_risk_exits LIMIT 1").fetchone()
            ):
                raise MarketDataError("ENGINEERING_ACCEPTANCE_REQUIRES_EMPTY_ACCOUNT")
            if (
                conn.execute("SELECT 1 FROM lab.execution_halts LIMIT 1").fetchone()
                or conn.execute(
                    "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s",
                    (session.session_date,),
                ).fetchone()
            ):
                raise MarketDataError("RISK_HALT")
            if not conn.execute(
                "SELECT 1 FROM lab.current_classifications WHERE ticker=%s", (candidate.ticker,)
            ).fetchone():
                raise MarketDataError("CORRELATION_UNKNOWN")
            if conn.execute("""SELECT 1 FROM lab.orders o JOIN lab.candidates c USING(candidate_id)
                WHERE c.record_purpose='ENGINEERING_TEST' LIMIT 1""").fetchone():
                raise MarketDataError("CONTROLLED_ACCEPTANCE_ALREADY_SUBMITTED")
            if conn.execute("""SELECT 1 FROM lab.engineering_acceptance_runs r
                LEFT JOIN lab.engineering_acceptance_results x USING(candidate_id)
                WHERE x.candidate_id IS NULL LIMIT 1""").fetchone():
                raise MarketDataError("ENGINEERING_ACCEPTANCE_ALREADY_PENDING")
            conn.execute(
                """INSERT INTO lab.candidates(candidate_id,payload_json,strategy_version,
                signal_id,ticker,session_date,submission_context) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    candidate_id,
                    Jsonb(body),
                    STRATEGY_VERSION,
                    candidate.signal_id,
                    candidate.ticker,
                    session.session_date,
                    Jsonb(context),
                ),
            )
            conn.execute(
                "INSERT INTO lab.signal_claims VALUES(%s,%s)", (candidate.signal_id, candidate_id)
            )
            conn.execute(
                """INSERT INTO lab.ticker_day_claims(ticker,session_date,candidate_id)
                VALUES(%s,%s,%s)""",
                (candidate.ticker, session.session_date, candidate_id),
            )
            self.repo.append_event(
                conn,
                "CANDIDATE_RECEIVED",
                {"candidate": body, "submission_context": context},
                candidate_id,
            )
            self.repo.transition(conn, candidate_id, None, "RECEIVED")
            self.repo.transition(conn, candidate_id, "RECEIVED", "VALIDATING")
            event = self.repo.append_event(
                conn,
                "VALIDATION_DECISION",
                {
                    "passed": True,
                    "reason": "ENGINEERING_PLUMBING_ADMISSION_ONLY",
                    "evidence": evidence,
                    "policy": asdict(self.engine.policy),
                    "risk_stage": "ENGINEERING_ADMISSION_ONLY",
                },
                candidate_id,
            )
            conn.execute(
                """INSERT INTO lab.validation_decisions(candidate_id,event_id,passed,
                reason,expires_at,evidence_json,policy_json) VALUES(%s,%s,true,%s,%s,%s,%s)""",
                (
                    candidate_id,
                    event["event_id"],
                    "ENGINEERING_PLUMBING_ADMISSION_ONLY",
                    deadline,
                    Jsonb(json_safe(evidence)),
                    Jsonb(json_safe(asdict(self.engine.policy))),
                ),
            )
            event = system_event(
                self.repo,
                conn,
                "ENGINEERING_ACCEPTANCE_ENROLLED",
                {"deadline": deadline, "excluded_from_strategy_results": True},
                candidate_id,
            )
            conn.execute(
                "INSERT INTO lab.engineering_acceptance_runs VALUES(%s,%s,%s)",
                (candidate_id, deadline, event["seq"]),
            )
            self.repo.transition(conn, candidate_id, "VALIDATING", "VALIDATED")
        return self.repo.get_candidate(candidate_id)

    def tick(self, safety):
        """One enrolled run: actual printed trigger, risk gate, fill, then autonomous close."""
        with self.repo.connect() as conn:
            runs = conn.execute("""SELECT r.*,c.ticker,s.state,
                coalesce(p.qty,0) AS position_qty FROM lab.engineering_acceptance_runs r
                JOIN lab.candidates c USING(candidate_id)
                JOIN lab.candidate_states s USING(candidate_id)
                LEFT JOIN lab.strategy_positions p USING(candidate_id)
                LEFT JOIN lab.engineering_acceptance_results x USING(candidate_id)
                WHERE x.candidate_id IS NULL""").fetchall()
        for run in runs:
            if run["position_qty"] or (
                self.engine.now() >= run["deadline"]
                and run["state"] in {"ORDER_SUBMITTED", "PARTIALLY_FILLED", "FILLED", "OPEN"}
            ):
                safety.request_exit(run["ticker"], "CONTROLLED_ACCEPTANCE", run["candidate_id"])
                safety.process_exits()
            self._finish(run["candidate_id"])

    def _finish(self, candidate_id):
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            state = conn.execute(
                "SELECT state FROM lab.candidate_states WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()["state"]
            if state not in {
                "CLOSED",
                "CANCELED",
                "BROKER_REJECTED",
                "RISK_REJECTED",
                "INVALIDATED",
                "EXPIRED_UNTRIGGERED",
            }:
                return
            if (
                conn.execute(
                    "SELECT 1 FROM lab.active_reservations WHERE candidate_id=%s", (candidate_id,)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM lab.pending_risk_exits WHERE candidate_id=%s", (candidate_id,)
                ).fetchone()
            ):
                return
            if conn.execute(
                "SELECT 1 FROM lab.engineering_acceptance_results WHERE candidate_id=%s",
                (candidate_id,),
            ).fetchone():
                return
            closed_by_test = conn.execute(
                """SELECT 1 FROM lab.risk_exit_requests r
                JOIN lab.risk_exit_completions c USING(exit_request_id)
                WHERE r.candidate_id=%s AND r.reason='CONTROLLED_ACCEPTANCE'""",
                (candidate_id,),
            ).fetchone()
            has_exit_fill = conn.execute(
                """SELECT 1 FROM lab.fills f JOIN lab.orders o USING(order_id)
                WHERE o.candidate_id=%s AND f.role='EMERGENCY_EXIT'""",
                (candidate_id,),
            ).fetchone()
            outcome = (
                ("PASSED" if closed_by_test and has_exit_fill else "FAILED_CLOSED")
                if state == "CLOSED"
                else "NOT_ENTERED"
            )
            event = system_event(
                self.repo,
                conn,
                "ENGINEERING_ACCEPTANCE_RESULT",
                {
                    "outcome": outcome,
                    "excluded_from_strategy_results": True,
                    "normal_risk_gate_remains_required": True,
                },
                candidate_id,
            )
            conn.execute(
                "INSERT INTO lab.engineering_acceptance_results VALUES(%s,%s,%s)",
                (candidate_id, outcome, event["seq"]),
            )
