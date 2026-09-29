"""Phase 3 intent construction. Every broker mutation is structurally disabled."""

from decimal import Decimal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.config import STRATEGY_VERSION
from catalyst_lab.domain import Candidate
from catalyst_lab.market import NY
from catalyst_lab.repository import json_safe


class SubmissionDisabled(RuntimeError):
    pass


def whole_shares(qty):
    if type(qty) is not int or qty <= 0:
        raise ValueError("An explicit positive integer share count is required")
    return qty


def broker_price(price):
    tick = Decimal(".01") if price >= 1 else Decimal(".0001")
    if price != price.quantize(tick):
        raise ValueError("Price exceeds broker precision; frozen levels must not be rounded")
    return str(price)


def bracket(candidate: Candidate, qty: int, candidate_id):
    whole_shares(qty)
    if not candidate.stop < candidate.entry_trigger <= candidate.max_entry_price < candidate.target:
        raise ValueError("Invalid long bracket levels")
    return {
        "symbol": candidate.ticker,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": broker_price(candidate.max_entry_price),
        "time_in_force": "day",
        "order_class": "bracket",
        "extended_hours": False,
        "stop_loss": {"stop_price": broker_price(candidate.stop)},
        "take_profit": {"limit_price": broker_price(candidate.target)},
        "client_order_id": "crt1-" + UUID(str(candidate_id)).hex,
    }


class Phase3Gate:
    @property
    def trading_enabled(self):
        return False

    def authorize(self):
        raise SubmissionDisabled("PHASE_4_RISK_GATE_REQUIRED")


def system_event(repo, conn, kind, payload, candidate_id=None):
    event = repo.append_event(conn, "SYSTEM_EVENT", {"kind": kind, **payload}, candidate_id)
    conn.execute(
        "INSERT INTO lab.system_events(event_id,event_type,payload_json) VALUES (%s,%s,%s)",
        (event["event_id"], kind, Jsonb(json_safe(payload))),
    )
    return event


def halt(repo, conn, reason, payload, candidate_id=None):
    event = system_event(repo, conn, "RISK_HALT", {"reason": reason, **payload}, candidate_id)
    conn.execute(
        "INSERT INTO lab.execution_halts VALUES (%s,%s,%s,%s)",
        (event["seq"], reason, candidate_id, Jsonb(json_safe(payload))),
    )


class ExecutionService:
    def __init__(self, repo, client):
        self.repo, self.client = repo, client

    def prepare_entry(self, candidate_id, qty):
        whole_shares(qty)
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            candidate = self.repo.get_candidate(candidate_id, connection=conn)
            if not candidate or candidate["state"] != "TRIGGER_CONFIRMED":
                raise ValueError("An actual confirmed trigger is required before order intent")
            model = Candidate.model_validate(
                {"strategy_version": STRATEGY_VERSION, **candidate["payload_json"]}
            )
            payload = bracket(model, qty, candidate_id)
            return self._intent(conn, candidate_id, "ENTRY", payload)

    def _intent(self, conn, candidate_id, kind, payload, scheduled_for=None):
        existing = conn.execute(
            "SELECT * FROM lab.order_intents WHERE candidate_id=%s AND kind=%s",
            (candidate_id, kind),
        ).fetchone()
        if existing:
            if existing["payload_json"] != payload:
                raise ValueError("Order intent is immutable")
            return existing
        intent_id = uuid4()
        event = system_event(
            self.repo,
            conn,
            "ORDER_INTENT",
            {
                "intent_id": str(intent_id),
                "intent_kind": kind,
                "order": payload,
                "scheduled_for": scheduled_for,
                "trading_enabled": False,
            },
            candidate_id,
        )
        return conn.execute(
            """INSERT INTO lab.order_intents
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                intent_id,
                candidate_id,
                STRATEGY_VERSION,
                kind,
                Jsonb(payload),
                scheduled_for,
                event["seq"],
            ),
        ).fetchone()

    def attempt(self, intent):
        # The client method itself always raises. No injected boolean/callback can authorize it.
        try:
            Phase3Gate().authorize()
            if intent["kind"] == "ENTRY":
                self.client.submit_bracket(intent["payload_json"])
            else:
                self.client.flatten_position(intent["payload_json"])
        except SubmissionDisabled:
            with self.repo.connect() as conn:
                system_event(
                    self.repo,
                    conn,
                    "SUBMISSION_DENIED",
                    {
                        "intent_id": str(intent["intent_id"]),
                        "reason": "PHASE_4_RISK_GATE_REQUIRED",
                        "trading_enabled": False,
                    },
                    intent["candidate_id"],
                )
            return False
        raise RuntimeError("Phase 3 broker mutation invariant violated")


class TimeExits:
    def __init__(self, service):
        self.service = service

    def tick(self, session, now):
        if (
            session is None
            or now.astimezone(NY).date() != session.session_date
            or now < session.flatten_time
        ):
            return []
        intents = []
        repo = self.service.repo
        with repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            rows = conn.execute("""SELECT p.* FROM lab.strategy_positions p
                WHERE p.qty > 0 AND NOT EXISTS(SELECT 1 FROM lab.order_intents i
                    WHERE i.candidate_id=p.candidate_id AND i.kind='TIME_EXIT')""").fetchall()
            for row in rows:
                qty = int(row["qty"])
                if Decimal(qty) != row["qty"]:
                    halt(repo, conn, "FRACTIONAL_STRATEGY_POSITION", {}, row["candidate_id"])
                    continue
                payload = {
                    "symbol": row["ticker"],
                    "qty": str(whole_shares(qty)),
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": "crt1-exit-" + row["candidate_id"].hex,
                }
                intent = self.service._intent(
                    conn, row["candidate_id"], "TIME_EXIT", payload, session.flatten_time
                )
                system_event(
                    repo,
                    conn,
                    "TIME_EXIT_DUE",
                    {
                        "intent_id": str(intent["intent_id"]),
                        "scheduled_for": session.flatten_time,
                        "observed_at": now,
                        "official_close": session.closes,
                        "trading_enabled": False,
                    },
                    row["candidate_id"],
                )
                intents.append(intent)
        for intent in intents:
            self.service.attempt(intent)
        return intents
