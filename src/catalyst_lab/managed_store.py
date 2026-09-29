"""Durable engineering lifecycle; every write is append-only and hash-audited."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.authorization import AuthorizationGate
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.repository import json_safe

POLICY = "MUSE_JEV_MANAGED_TEST_V1"
COHORT = "JEV_MANAGED_PAPER_V1"
TERMINAL = {"CLOSED", "INVALIDATED", "EXPIRED_UNTRIGGERED", "RISK_REJECTED", "REJECTED"}


class ManagedStore:
    def __init__(self, repository):
        repository.check_role()
        self.repo = repository

    @contextmanager
    def transaction(self):
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            yield conn

    def event(self, conn, kind, body, *, setup_id=None, key=None):
        key = key or str(uuid4())
        existing = conn.execute(
            "SELECT * FROM lab.managed_events WHERE idempotency_key=%s", (key,)
        ).fetchone()
        if existing:
            if existing["kind"] != kind or existing["body"] != json_safe(body):
                raise ValueError("IDEMPOTENCY_CONTENT_MISMATCH")
            return existing
        return conn.execute(
            """INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,body)
            VALUES(%s,%s,%s,%s,%s) RETURNING *""",
            (uuid4(), setup_id, key, kind, Jsonb(json_safe(body))),
        ).fetchone()

    def setup(self, conn, setup_id):
        row = conn.execute(
            "SELECT * FROM lab.managed_setups WHERE setup_id=%s", (setup_id,)
        ).fetchone()
        if not row:
            raise ValueError("SETUP_MISSING")
        return row

    def state(self, conn, setup_id):
        row = conn.execute(
            "SELECT * FROM lab.managed_states WHERE setup_id=%s", (setup_id,)
        ).fetchone()
        return {**row["body"], "event_seq": row["event_seq"]} if row else {}

    def transition(self, conn, setup_id, state, **changes):
        before = self.state(conn, setup_id)
        body = {k: v for k, v in before.items() if k != "event_seq"}
        if before.get("state") in TERMINAL and state != before["state"]:
            raise ValueError("CLOSED_LIFECYCLE_CANNOT_REOPEN")
        body.update(changes)
        body["state"] = state
        body["revision"] = before.get("revision", 0) + 1
        self.event(conn, "STATE", body, setup_id=setup_id)
        return body

    def active(self):
        with self.repo.connect() as conn:
            rows = conn.execute("""SELECT s.*,t.body AS state FROM lab.managed_setups s
                JOIN lab.managed_states t USING(setup_id) ORDER BY s.event_seq""").fetchall()
        return [r for r in rows if r["state"]["state"] not in TERMINAL]

    def outputs(self, after=0, limit=100, *, exclude_kinds=()):
        """Events after ``after``, ascending. ``exclude_kinds`` are left out by the query itself,
        so a page (and its cursor) covers the next visible events however many are hidden."""
        excluded = sorted(exclude_kinds)
        with self.repo.connect() as conn:
            if not excluded:
                return conn.execute(
                    """SELECT event_seq,setup_id,kind,body,recorded_at
                    FROM lab.managed_events WHERE event_seq>%s ORDER BY event_seq LIMIT %s""",
                    (after, min(1000, max(1, limit))),
                ).fetchall()
            return conn.execute(
                """SELECT event_seq,setup_id,kind,body,recorded_at
                FROM lab.managed_events WHERE event_seq>%s AND kind <> ALL(%s)
                ORDER BY event_seq LIMIT %s""",
                (after, excluded, min(1000, max(1, limit))),
            ).fetchall()


class ManagedAuthorizationGate(AuthorizationGate):
    """Exact five-second one-use decisions. The frozen gate is unchanged for old rows."""

    def claim(self, request):
        raw = request.extensions.get("risk_decision_id")
        from uuid import UUID

        try:
            decision_id = UUID(str(raw))
            payload = strict_json(request.content) if request.content else {}
        except (ValueError, TypeError, UnicodeError):
            raise SubmissionDisabled("RISK_DECISION_REQUIRED") from None
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            d = conn.execute(
                """SELECT *,expires_at>clock_timestamp() AS fresh
                FROM lab.managed_risk_decisions WHERE decision_id=%s""",
                (decision_id,),
            ).fetchone()
            if not d:
                raise SubmissionDisabled("VALID_UNUSED_RISK_DECISION_REQUIRED")
            if (
                d["outcome"] != "APPROVED"
                or not d["fresh"]
                or d["method"] != request.method
                or d["path"] != request.url.path
                or d["payload"] != payload
                or request.url.query
                or conn.execute(
                    "SELECT 1 FROM lab.managed_claims WHERE decision_id=%s", (decision_id,)
                ).fetchone()
            ):
                raise SubmissionDisabled("VALID_UNUSED_RISK_DECISION_REQUIRED")
            setup = conn.execute(
                "SELECT * FROM lab.managed_setups WHERE setup_id=%s", (d["setup_id"],)
            ).fetchone()
            current = conn.execute(
                "SELECT body FROM lab.managed_states WHERE setup_id=%s", (d["setup_id"],)
            ).fetchone()["body"]
            if current["revision"] != d["context"]["state_revision"]:
                raise SubmissionDisabled("STALE_POSITION_REVISION")
            if d["action"] == "ENTRY":
                why = conn.execute(
                    "SELECT lab.managed_review_failure(%s) AS reason",
                    (Jsonb(setup["record_json"]),),
                ).fetchone()["reason"]
                if why:
                    raise SubmissionDisabled(why)
                now = self.now()
                context = d["context"]
                quote_at = datetime.fromisoformat(context["quote_at"])
                if (
                    current["state"] != "ENTRY_PENDING"
                    or setup["expires_at"] <= now
                    or not 0 <= (now - quote_at).total_seconds() <= 5
                    or not conn.execute(
                        "SELECT 1 FROM lab.managed_active_reservations WHERE decision_id=%s",
                        (decision_id,),
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s",
                        (context["session_date"],),
                    ).fetchone()
                    or conn.execute("SELECT 1 FROM lab.execution_halts LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM lab.pending_risk_exits LIMIT 1").fetchone()
                    or current.get("revoked")
                ):
                    raise SubmissionDisabled("ENTRY_AUTHORIZATION_REVOKED")
                liquidity = context.get("crypto_liquidity")
                if liquidity is not None and (
                    liquidity.get("passed") is not True
                    or not 0 <= (now - datetime.fromisoformat(
                        liquidity["observed_at"]
                    )).total_seconds() <= 5
                    or Decimal(payload["qty"]) * Decimal(payload["limit_price"])
                    > Decimal(liquidity["maximum_entry_notional"])
                ):
                    raise SubmissionDisabled("CRYPTO_LIQUIDITY_INVALID_AT_DISPATCH")
                if setup["market"] == "US_STOCKS" and not (
                    datetime.fromisoformat(context["session_open"])
                    <= now
                    < datetime.fromisoformat(context["entry_deadline"])
                ):
                    raise SubmissionDisabled("ENTRY_WINDOW_CLOSED")
                if setup["market"] == "CRYPTO" and context.get("entry_deadline") and (
                    now >= datetime.fromisoformat(context["entry_deadline"])
                ):
                    raise SubmissionDisabled("ENTRY_WINDOW_CLOSED")
                # Material evidence revocation is durable and may arrive after risk reservation.
                if conn.execute(
                    """SELECT 1 FROM lab.managed_events WHERE setup_id=%s
                    AND kind='REVOKE'""",
                    (d["setup_id"],),
                ).fetchone():
                    raise SubmissionDisabled("MATERIAL_EVIDENCE_REVOKED")
            conn.execute("INSERT INTO lab.managed_claims(decision_id) VALUES(%s)", (decision_id,))
        return decision_id


def body_json(value):
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"))


def utcnow():
    return datetime.now(UTC)
