"""Durable dispatch and reconciliation after an uncertain broker response."""

import threading

from psycopg.types.json import Jsonb

from catalyst_lab.broker_ledger import TERMINAL, BrokerLedger, normalize_order
from catalyst_lab.execution import SubmissionDisabled, system_event
from catalyst_lab.paper_execution import BrokerMutationRejected, BrokerMutationUnknown
from catalyst_lab.repository import json_safe


class RiskDispatcher:
    def __init__(self, engine):
        self.engine = engine
        self.repo, self.client = engine.repo, engine.client
        self.lock = threading.RLock()
        self.ledger = BrokerLedger(
            self.repo, pending_authorization=self._pending, after_projection=self._after_projection
        )
        self.safety = None

    def _pending(self, conn, body):
        if not isinstance(body, dict) or not isinstance(body.get("order"), dict):
            return False
        order = body["order"]
        return bool(
            conn.execute(
                """SELECT 1 FROM lab.risk_decisions d
            JOIN lab.authorization_claims c USING(risk_decision_id)
            LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
            WHERE d.decision='APPROVED' AND d.action IN ('ENTRY','FLATTEN')
             AND (r.outcome IS NULL OR r.outcome IN ('UNKNOWN','NOT_FOUND'))
             AND (d.payload_json->>'client_order_id'=%s OR
                  (d.action='ENTRY' AND d.payload_json->>'symbol'=%s
                   AND %s='sell' AND %s IN ('stop','limit'))) LIMIT 1""",
                (
                    order.get("client_order_id"),
                    order.get("symbol"),
                    order.get("side"),
                    order.get("type"),
                ),
            ).fetchone()
        )

    def _after_projection(self, conn, link):
        current = conn.execute(
            "SELECT state FROM lab.candidate_states WHERE candidate_id=%s", (link["candidate_id"],)
        ).fetchone()["state"]
        if current in {"CLOSED", "CANCELED", "BROKER_REJECTED", "RISK_REJECTED", "INVALIDATED"}:
            self.engine.release(link["candidate_id"], current, connection=conn)

    def result(self, decision, outcome, receipt=None):
        receipt = receipt or {}
        with self.repo.connect() as conn:
            event = system_event(
                self.repo,
                conn,
                "BROKER_MUTATION_RESULT",
                {
                    "risk_decision_id": str(decision["risk_decision_id"]),
                    "outcome": outcome,
                    "broker_response": receipt,
                },
                decision["candidate_id"],
            )
            conn.execute(
                "INSERT INTO lab.authorization_results VALUES(%s,%s,%s,%s,%s)",
                (
                    event["seq"],
                    decision["risk_decision_id"],
                    outcome,
                    receipt.get("id"),
                    Jsonb(json_safe(receipt)),
                ),
            )

    def _saved(self, decision_id):
        with self.repo.connect() as conn:
            return conn.execute(
                """SELECT d.*,d.expires_at>clock_timestamp() AS fresh,
                c.risk_decision_id IS NOT NULL AS claimed,r.outcome
                FROM lab.risk_decisions d
                LEFT JOIN lab.authorization_claims c USING(risk_decision_id)
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE d.risk_decision_id=%s""",
                (decision_id,),
            ).fetchone()

    def enter(self, candidate_id):
        decision = self.engine.authorize_entry(candidate_id)
        if decision["decision"] == "APPROVED":
            self.dispatch(decision["risk_decision_id"])
        elif decision["reason"] == "DAILY_RISK_HALT" and self.safety:
            self.safety.daily_halt()
        return decision

    def dispatch(self, decision_id):
        with self.lock:
            decision = self._saved(decision_id)
            if not decision or decision["decision"] != "APPROVED":
                raise SubmissionDisabled("RISK_DECISION_REQUIRED")
            if decision["claimed"]:
                return self.recover(decision_id)
            if not decision["fresh"]:
                self.result(decision, "EXPIRED")
                self._terminal_unsent(decision, "AUTHORIZATION_EXPIRED")
                return None
            try:
                if decision["action"] == "ENTRY":
                    receipt = self.client.submit_bracket(
                        decision["payload_json"], risk_decision_id=decision_id
                    )
                elif decision["action"] == "CANCEL":
                    receipt = self.client.cancel_order(
                        decision["path"].rsplit("/", 1)[1], risk_decision_id=decision_id
                    )
                else:
                    receipt = self.client.flatten_position(
                        decision["payload_json"], risk_decision_id=decision_id
                    )
            except SubmissionDisabled:
                # A gate refusal sent nothing; a concurrent claim must instead be recovered.
                if self._saved(decision_id)["claimed"]:
                    return self.recover(decision_id)
                self.result(decision, "EXPIRED", {"reason": "AUTHORIZATION_REFUSED"})
                self._terminal_unsent(decision, "AUTHORIZATION_REFUSED")
                return None
            except BrokerMutationRejected as exc:
                # str(exc) is the classified reason (BROKER_MARGIN_REJECTED, ...); the evidence
                # is the sanitized broker code and message, never request credentials.
                self.result(decision, "REJECTED", {"reason": str(exc), "evidence": exc.evidence})
                self._terminal_unsent(decision, str(exc), rejected=True)
                return None
            except Exception as exc:
                # The request may have reached the broker. Retain risk; never issue another POST.
                reason = (
                    str(exc)
                    if isinstance(exc, BrokerMutationUnknown)
                    else "BROKER_RESPONSE_UNKNOWN"
                )
                self.result(decision, "UNKNOWN", {"reason": reason})
                return self.recover(decision_id)
            if decision["action"] != "CANCEL":
                self._associate(decision, receipt)
            self.result(decision, "ACCEPTED", receipt)
            self.replay_deferred()
            if self.safety and decision["action"] == "ENTRY":
                self.safety.check_protection(decision["candidate_id"])
            return receipt

    def _terminal_unsent(self, decision, reason, rejected=False):
        if decision["action"] != "ENTRY":
            return
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            current = conn.execute(
                "SELECT state FROM lab.candidate_states WHERE candidate_id=%s",
                (decision["candidate_id"],),
            ).fetchone()["state"]
            if current == "ORDER_SUBMITTED":
                self.repo.transition(
                    conn,
                    decision["candidate_id"],
                    current,
                    "BROKER_REJECTED" if rejected else "CANCELED",
                    {"reason": reason},
                )
                self.engine.release(decision["candidate_id"], reason, connection=conn)

    def recover(self, decision_id):
        with self.lock:
            decision = self._saved(decision_id)
            if not decision or not decision["claimed"]:
                return None
            if decision["action"] == "CANCEL":
                order = self.client.order(decision["path"].rsplit("/", 1)[1])
                if order and order["status"] in TERMINAL:
                    self.result(decision, "RECOVERED", order)
                    return order
                return None
            try:
                order = self.client.order_by_client_id(decision["payload_json"]["client_order_id"])
            except Exception:
                return None  # No retry on lookup failure, even after a restart.
            if order is None:
                if decision["outcome"] != "NOT_FOUND":
                    # One 404 does not prove an in-flight submit failed. Keep the reservation.
                    self.result(
                        decision, "NOT_FOUND", {"reason": "LOOKUP_INCONCLUSIVE_NO_RESUBMISSION"}
                    )
                return None
            full = self.client.order(order["id"]) or order
            self._associate(decision, full)
            if decision["outcome"] not in {"ACCEPTED", "RECOVERED"}:
                self.result(decision, "RECOVERED", full)
            self.replay_deferred()
            return full

    def _associate(self, decision, receipt):
        if decision["action"] == "ENTRY":
            with self.repo.connect() as conn:
                intent = conn.execute(
                    "SELECT * FROM lab.order_intents WHERE candidate_id=%s AND kind='ENTRY'",
                    (decision["candidate_id"],),
                ).fetchone()
            self.ledger.register_submitted(
                decision["candidate_id"], intent, receipt, allow_incomplete=True
            )
            self.ledger.discover_protective_legs(decision["candidate_id"], receipt)
        elif decision["action"] == "FLATTEN" and decision["candidate_id"]:
            item = normalize_order(receipt)
            payload = decision["payload_json"]
            if (
                item["symbol"] != payload["symbol"]
                or item["side"] != payload["side"]
                or item["qty"] != int(payload["qty"])
                or item["type"] != "market"
                or receipt.get("client_order_id") != payload["client_order_id"]
            ):
                raise ValueError("Exit receipt does not match risk authorization")
            with self.repo.connect() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(719172026)")
                if conn.execute(
                    "SELECT 1 FROM lab.order_links WHERE broker_order_id=%s", (item["id"],)
                ).fetchone():
                    return
                entry = conn.execute(
                    "SELECT order_id FROM lab.orders WHERE candidate_id=%s",
                    (decision["candidate_id"],),
                ).fetchone()
                if not entry:
                    raise ValueError("No strategy entry ledger for exit")
                event = system_event(
                    self.repo,
                    conn,
                    "RISK_EXIT_REGISTERED",
                    {
                        "risk_decision_id": str(decision["risk_decision_id"]),
                        "broker_order": receipt,
                    },
                    decision["candidate_id"],
                )
                role = "TIME_EXIT" if decision["reason"] == "TIME_EXIT" else "EMERGENCY_EXIT"
                conn.execute(
                    """INSERT INTO lab.order_links
                    VALUES(%s,%s,%s,%s,%s,%s,'market',NULL,NULL,%s,%s)""",
                    (
                        item["id"],
                        entry["order_id"],
                        role,
                        item["symbol"],
                        item["side"],
                        int(item["qty"]),
                        item["status"],
                        event["seq"],
                    ),
                )

    def ingest(self, data, now):
        with self.lock:
            body = data if isinstance(data, dict) else {}
            order = body.get("order") if isinstance(body.get("order"), dict) else {}
            with self.repo.connect() as conn:
                known = conn.execute(
                    "SELECT 1 FROM lab.order_links WHERE broker_order_id=%s", (order.get("id"),)
                ).fetchone()
                pending_receipt = not known and self._pending(conn, data)
                decisions = (
                    conn.execute(
                        """SELECT d.risk_decision_id FROM lab.risk_decisions d
                    JOIN lab.authorization_claims c USING(risk_decision_id)
                    WHERE d.decision='APPROVED' AND d.action IN ('ENTRY','FLATTEN')
                     AND (d.payload_json->>'client_order_id'=%s OR d.candidate_id IN
                         (SELECT candidate_id FROM lab.active_reservations))""",
                        (order.get("client_order_id"),),
                    ).fetchall()
                    if not known and order
                    else []
                )
            # Persist an early execution before network recovery; broker lookup can time out.
            result = self.ledger.consume(data, now) if pending_receipt else None
            for row in decisions:
                self.recover(row["risk_decision_id"])
            if result is None:
                result = self.ledger.consume(data, now)
            if (
                self.safety
                and isinstance(data, dict)
                and data.get("event") in {"fill", "partial_fill", "rejected", "canceled", "expired"}
            ):
                with self.repo.connect() as conn:
                    link = conn.execute(
                        """SELECT o.candidate_id FROM lab.order_links l
                        JOIN lab.orders o USING(order_id) WHERE l.broker_order_id=%s""",
                        (order.get("id"),),
                    ).fetchone()
                if link:
                    self.safety.check_protection(link["candidate_id"])
            return result

    def replay_deferred(self):
        with self.repo.connect() as conn:
            pending = conn.execute("""SELECT b.event_seq,b.payload_json FROM lab.broker_events b
                JOIN lab.order_links l ON l.broker_order_id=b.broker_order_id
                WHERE NOT EXISTS(SELECT 1 FROM lab.order_updates u WHERE u.event_seq=b.event_seq)
                AND NOT (b.payload_json ? 'local_replay_of')
                AND EXISTS(SELECT 1 FROM lab.system_events s
                    WHERE s.event_type='BROKER_UPDATE_AWAITING_RECEIPT'
                      AND (s.payload_json->>'broker_event_seq')::bigint=b.event_seq)
                AND NOT EXISTS(SELECT 1 FROM lab.broker_events replay
                    WHERE replay.payload_json->>'local_replay_of'=b.event_seq::text)
                ORDER BY b.event_seq""").fetchall()
        for row in pending:
            self.ledger.consume(
                row["payload_json"] | {"local_replay_of": str(row["event_seq"])}, self.engine.now()
            )

    def recover_outstanding(self):
        with self.repo.connect() as conn:
            pending = conn.execute("""SELECT d.risk_decision_id FROM lab.risk_decisions d
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE d.decision='APPROVED'
                AND (r.outcome IS NULL OR r.outcome IN ('UNKNOWN','NOT_FOUND'))
                ORDER BY d.decided_at""").fetchall()
        for row in pending:
            self.dispatch(row["risk_decision_id"])
