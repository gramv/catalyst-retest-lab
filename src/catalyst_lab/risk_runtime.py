"""Bounded risk heartbeat attached to the single broker-monitor owner."""

import time

from catalyst_lab.engineering import EngineeringAcceptance
from catalyst_lab.execution import system_event
from catalyst_lab.market import NY, MarketDataError
from catalyst_lab.risk_dispatch import RiskDispatcher
from catalyst_lab.risk_safety import RiskSafety


class RiskRuntime:
    def __init__(self, engine):
        self.engine = engine
        self.dispatcher = RiskDispatcher(engine)
        self.safety = RiskSafety(self.dispatcher)
        self.engineering = EngineeringAcceptance(engine)
        self.last_check = 0.0
        self.last_error = None

    def status(self):
        with self.engine.repo.connect() as conn:
            rows = conn.execute(
                """SELECT r.*,c.record_purpose FROM lab.active_reservations r
                   JOIN lab.candidates c USING(candidate_id) ORDER BY r.event_seq"""
            ).fetchall()
            halted = conn.execute(
                "SELECT * FROM lab.daily_risk_halts ORDER BY session_date DESC LIMIT 1"
            ).fetchone()
            baseline = conn.execute(
                "SELECT * FROM lab.risk_sessions WHERE session_date=%s",
                (self.engine.now().astimezone(NY).date(),),
            ).fetchone()
            engineering = conn.execute("""
                SELECT r.candidate_id,c.ticker,r.deadline,s.state,x.outcome
                FROM lab.engineering_acceptance_runs r JOIN lab.candidates c USING(candidate_id)
                JOIN lab.candidate_states s USING(candidate_id)
                LEFT JOIN lab.engineering_acceptance_results x USING(candidate_id)
                ORDER BY r.event_seq DESC""").fetchall()
        from catalyst_lab.repository import json_safe

        return json_safe(
            {
                "configured": True,
                "submission_mode": "RISK_DECISION_REQUIRED",
                "reservations": rows,
                "last_daily_halt": halted,
                "error": self.last_error,
                "authorization_ttl_seconds": self.engine.policy.authorization_ttl_seconds,
                "session_baseline": baseline,
                "engineering_acceptance": engineering,
            }
        )

    def consume(self, data, now):
        with self.dispatcher.lock:
            result = self.dispatcher.ingest(data, now)
            self.engineering.tick(self.safety)
            return result

    def tick(self):
        try:
            with self.dispatcher.lock:
                self.dispatcher.recover_outstanding()
                self.safety.process_exits()
                self.engineering.tick(self.safety)
                session = self.engine.session()
                if session and session.contains(self.engine.now()):
                    self.safety.calendar_exits(session)
                    if time.monotonic() - self.last_check >= 5:
                        with self.engine.repo.connect() as conn:
                            positions = conn.execute(
                                "SELECT candidate_id FROM lab.active_reservations"
                            ).fetchall()
                        for row in positions:
                            self.safety.check_protection(row["candidate_id"])
                        halted = self.engine.observe_daily_risk()
                        self.last_check = time.monotonic()
                        if halted:
                            self.safety.daily_halt()
                    if self.engine.ready():
                        with self.engine.repo.connect() as conn:
                            candidates = conn.execute(
                                """SELECT candidate_id FROM lab.candidate_states
                                WHERE state='TRIGGER_CONFIRMED'"""
                            ).fetchall()
                        for row in candidates:
                            self.dispatcher.enter(row["candidate_id"])
                    self.safety.calendar_exits(session)
                # AI-specific cancellation work follows mechanical protection/time exits.
                # Every entry independently checks the binding before it can submit.
                from catalyst_lab.jev_paper import revoke_pending_reviews

                revoke_pending_reviews(self)
            self.last_error = None
        except Exception as exc:
            code = str(exc) if isinstance(exc, MarketDataError) else "RISK_ENGINE_FAILURE"
            if code != self.last_error:
                with self.engine.repo.connect() as conn:
                    system_event(self.engine.repo, conn, "RISK_ENGINE_FAILURE", {"reason": code})
            self.last_error = code
