"""Opt-in local engineering bridge. Jev selects; normal validation and risk decide."""

import threading
from datetime import datetime, timedelta

from catalyst_lab.config import STRATEGY_VERSION
from catalyst_lab.domain import Candidate
from catalyst_lab.execution import system_event
from catalyst_lab.market import NY

POLICY = "JEV_US_SELECTED_FIXED_TEST_V1"
COHORT = "JEV_US_SELECTED_FIXED_ENGINEERING"


class JevPaperAdmission:
    def __init__(self, repository, provider, *, policy_id, clock):
        if policy_id != POLICY:
            raise ValueError("Explicit US Jev engineering policy required")
        self.repo, self.provider, self.now = repository, provider, clock

    def admit(self, item_id):
        with self.repo.connect() as conn:
            packet = conn.execute("SELECT lab.jev_us_item(%s) AS p", (item_id,)).fetchone()["p"]
            existing = conn.execute(
                "SELECT * FROM lab.jev_paper_admissions WHERE item_id=%s", (item_id,)
            ).fetchone()
        if existing:
            return existing
        if not packet:
            raise ValueError("Research item missing")
        raw = packet["research"]
        now = self.now()
        body = {
            "strategy_version": STRATEGY_VERSION,
            "signal_id": "TEST-JEV-" + str(item_id),
            "ticker": raw["symbol"],
            "market": "US",
            **raw["levels"],
            "catalyst": raw["catalyst"],
            "thesis": raw["thesis"],
            "disproof": raw["disproof"],
            "expires_at": packet["expires_at"],
        }
        # Structural checks and every provider read happen outside the ledger lock.
        failure = packet["failure"]
        prepared = None
        if not failure:
            try:
                Candidate.model_validate(body)
            except ValueError:
                failure = "INVALID_US_CANDIDATE"
            else:
                prepared = self.repo._prefetch_evidence(body, now, self.provider, None)
                if prepared[0] is not None:
                    body["expires_at"] = min(
                        datetime.fromisoformat(packet["expires_at"]),
                        prepared[0].official_close - timedelta(minutes=5),
                    ).isoformat()
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            existing = conn.execute(
                "SELECT * FROM lab.jev_paper_admissions WHERE item_id=%s", (item_id,)
            ).fetchone()
            if existing:
                return existing
            failure = (
                failure
                or conn.execute(
                    "SELECT lab.jev_us_review_failure(%s) AS why", (item_id,)
                ).fetchone()["why"]
            )
            candidate = None
            if not failure:
                candidate = self.repo.submit(
                    body,
                    now,
                    self.provider,
                    self.provider.profile.validation_policy,
                    submission_context={
                        "strategy_version": STRATEGY_VERSION,
                        "date": now.astimezone(NY).date().isoformat(),
                        "origin": POLICY,
                        "research_item_id": str(item_id),
                        "cohort": COHORT,
                    },
                    connection=conn,
                    _evidence_result=prepared,
                    decision_clock=self.now,
                )
                failure = candidate["failed_rule"] if candidate["state"] == "REJECTED" else None
            return conn.execute(
                """INSERT INTO lab.jev_paper_admissions(item_id,candidate_id,receipt_id,
                policy_id,cohort,outcome,reason,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *""",
                (
                    item_id,
                    candidate["candidate_id"] if candidate else None,
                    packet["receipt_id"],
                    POLICY,
                    COHORT,
                    "REJECTED" if failure else "VALIDATED",
                    failure or "AWAIT_CODED_TRIGGER_AND_RISK",
                    datetime.fromisoformat(body["expires_at"]),
                ),
            ).fetchone()


class JevAdmissionWorker:
    """Admission I/O has its own thread; never runs in the protection heartbeat."""

    def __init__(self, admission, *, poll_seconds):
        if not 0.1 <= poll_seconds <= 5:
            raise ValueError("Explicit admission poll must be 0.1–5 seconds")
        self.admission, self.poll_seconds = admission, poll_seconds
        self.stop_event, self.thread, self.error = threading.Event(), None, None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=30)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                with self.admission.repo.connect() as conn:
                    ids = conn.execute("SELECT lab.jev_us_pending() AS item_id").fetchall()
                for row in ids:
                    if self.stop_event.is_set():
                        break
                    self.admission.admit(row["item_id"])
                self.error = None
            except Exception:
                first_failure = self.error is None
                self.error = "ADMISSION_WORKER_FAILED"
                if first_failure:
                    try:
                        with self.admission.repo.connect() as conn:
                            system_event(
                                self.admission.repo,
                                conn,
                                "JEV_ADMISSION_FAILURE",
                                {"reason": self.error, "cohort": COHORT},
                            )
                    except Exception:
                        pass  # Health stays failed when PostgreSQL cannot record the outage.
            self.stop_event.wait(self.poll_seconds)


def revoke_pending_reviews(runtime):
    """Review loss cancels unfilled entry only. Filled inventory keeps mechanical exits."""
    repo = runtime.engine.repo
    with repo.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        rows = conn.execute("""SELECT a.candidate_id,s.state,o.alpaca_order_id,
            lab.jev_us_entry_failure(a.candidate_id) AS reason
            FROM lab.jev_paper_admissions a JOIN lab.candidate_states s USING(candidate_id)
            LEFT JOIN lab.orders o USING(candidate_id)
            WHERE a.outcome='VALIDATED' AND s.state IN
              ('VALIDATED','WATCHING','TRIGGER_CONFIRMED','ORDER_SUBMITTED','PARTIALLY_FILLED')""").fetchall()
        for row in rows:
            if not row["reason"]:
                continue
            conn.execute(
                """INSERT INTO lab.jev_paper_revocations(candidate_id,reason)
                SELECT %s,%s WHERE NOT EXISTS
                 (SELECT 1 FROM lab.jev_paper_revocations WHERE candidate_id=%s)""",
                (row["candidate_id"], row["reason"], row["candidate_id"]),
            )
            if row["state"] == "WATCHING":
                repo.transition(
                    conn, row["candidate_id"], "WATCHING", "INVALIDATED", {"reason": row["reason"]}
                )
    for row in rows:
        if not row["reason"] or not row["alpaca_order_id"]:
            continue
        order = runtime.engine.client.order(row["alpaca_order_id"])
        if order and order.get("side") == "buy":
            from catalyst_lab.market import decimal

            if order.get("status") not in {"filled", "canceled", "expired", "rejected"} and decimal(
                order["filled_qty"], positive=False
            ) < decimal(order["qty"]):
                runtime.safety._cancel(order, "JEV_PENDING_REVIEW_REVOKED")
            # A fill may have won the cancel race; normal partial-fill protection remains active.
            runtime.safety.check_protection(row["candidate_id"])
