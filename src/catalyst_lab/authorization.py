"""One-use, exact-request risk capabilities backed by committed PostgreSQL evidence."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from catalyst_lab.config import SCHEMA_VERSION
from catalyst_lab.execution import SubmissionDisabled, system_event
from catalyst_lab.market import timestamp
from catalyst_lab.repository import Repository


class RiskRepository(Repository):
    def require_same_database(self, application_repository):
        with self.connect() as risk, application_repository.connect() as application:
            fields = ("host", "port", "dbname")
            if any(getattr(risk.info, k) != getattr(application.info, k) for k in fields):
                raise RuntimeError("Application and risk roles must use the same database")

    def check_role(self):
        with self.connect() as conn:
            row = conn.execute("""SELECT current_user AS role, rolsuper, rolcreaterole,
                rolcreatedb, has_table_privilege(current_user,'lab.trade_events','UPDATE') AS u,
                has_table_privilege(current_user,'lab.trade_events','DELETE') AS d,
                has_table_privilege(current_user,'lab.trade_events','TRUNCATE') AS t,
                has_schema_privilege(current_user,'lab','CREATE') AS c
                FROM pg_roles WHERE rolname=current_user""").fetchone()
            if row["role"] != "catalyst_risk" or any(v for k, v in row.items() if k != "role"):
                raise RuntimeError("Risk engine requires the restricted catalyst_risk role")
            if (
                conn.execute("SELECT max(version) AS v FROM lab.schema_migrations").fetchone()["v"]
                != SCHEMA_VERSION
            ):
                raise RuntimeError("Risk engine schema is unavailable")


class AuthorizationGate:
    """The transport invokes this immediately before sending any mutating HTTP request.

    A UUID is only a lookup key, never authorization by itself. The committed row binds the exact
    method, path and JSON; the one-use claim commits before network I/O, surviving a process kill.
    """

    def __init__(self, repository: RiskRepository, *, clock=None):
        repository.check_role()
        self.repo = repository
        self.now = clock or (lambda: datetime.now(UTC))

    def claim(self, request):
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Ambiguous request body")
                result[key] = value
            return result

        def finite(value):
            raise ValueError("Non-finite request body")

        try:
            decision_id = UUID(str(request.extensions.get("risk_decision_id")))
            payload = (
                json.loads(request.content, object_pairs_hook=unique_object, parse_constant=finite)
                if request.content
                else {}
            )
        except (ValueError, TypeError, UnicodeError):
            raise SubmissionDisabled("RISK_DECISION_REQUIRED") from None
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            decision = conn.execute(
                """SELECT *,expires_at>clock_timestamp() AS fresh
                FROM lab.risk_decisions WHERE risk_decision_id=%s""",
                (decision_id,),
            ).fetchone()
            if (
                not decision
                or decision["decision"] != "APPROVED"
                or not decision["fresh"]
                or request.method != decision["http_method"]
                or request.url.path != decision["path"]
                or payload != decision["payload_json"]
                or request.url.query
                or conn.execute(
                    "SELECT 1 FROM lab.authorization_claims WHERE risk_decision_id=%s",
                    (decision_id,),
                ).fetchone()
            ):
                raise SubmissionDisabled("VALID_UNUSED_RISK_DECISION_REQUIRED")
            if request.method == "POST":
                context = decision["context_json"]
                try:
                    opens = datetime.fromisoformat(context["session_opens"])
                    closes = datetime.fromisoformat(context["session_closes"])
                    if not opens <= self.now() < closes:
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise SubmissionDisabled("REGULAR_SESSION_REQUIRED") from None
            if decision["action"] == "ENTRY":
                jev_failure = conn.execute(
                    "SELECT lab.jev_us_entry_failure(%s) AS reason", (decision["candidate_id"],)
                ).fetchone()["reason"]
                if jev_failure:
                    raise SubmissionDisabled(jev_failure)
                try:
                    deadline = min(
                        timestamp(decision["context_json"].get("entry_deadline")),
                        timestamp(decision["context_json"].get("session_closes"))
                        - timedelta(minutes=5),
                    )
                    if self.now() >= deadline:
                        raise ValueError
                except Exception:
                    raise SubmissionDisabled("ENTRY_WINDOW_CLOSED") from None
                try:
                    quote_at = timestamp(decision["context_json"].get("market_quote_timestamp"))
                    if not 0 <= (self.now() - quote_at).total_seconds() <= 5:
                        raise ValueError
                except Exception:
                    raise SubmissionDisabled("FRESH_MARKET_SNAPSHOT_REQUIRED") from None
                reserved = conn.execute(
                    "SELECT 1 FROM lab.active_reservations WHERE risk_decision_id=%s",
                    (decision_id,),
                ).fetchone()
                halted = conn.execute(
                    "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s",
                    (decision["session_date"],),
                ).fetchone()
                blocked = conn.execute("SELECT 1 FROM lab.execution_halts LIMIT 1").fetchone()
                exiting = conn.execute("SELECT 1 FROM lab.pending_risk_exits LIMIT 1").fetchone()
                if not reserved or halted or blocked or exiting:
                    raise SubmissionDisabled("ENTRY_AUTHORIZATION_REVOKED")
            event = system_event(
                self.repo,
                conn,
                "BROKER_DISPATCH_CLAIM",
                {
                    "risk_decision_id": str(decision_id),
                    "action": decision["action"],
                    "method": request.method,
                    "path": request.url.path,
                },
                decision["candidate_id"],
            )
            conn.execute(
                "INSERT INTO lab.authorization_claims(risk_decision_id,event_seq) VALUES(%s,%s)",
                (decision_id, event["seq"]),
            )
        return decision_id
