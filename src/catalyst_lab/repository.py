import json
import re
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from catalyst_lab.config import SCHEMA_VERSION, STRATEGY_VERSION
from catalyst_lab.domain import Candidate, Evidence, Policy
from catalyst_lab.validation import NEW_YORK, EvidenceUnavailable, reject, validate


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Unsupported JSON type {type(value).__name__}")


def json_safe(value):
    return json.loads(json.dumps(value, default=json_value))


class ReusableConnection(psycopg.Connection):
    """A connection that a reusing :class:`Repository` hands out.

    ``with`` ends it exactly as psycopg does (commit, or roll back on an exception) and then
    returns it to its repository instead of closing it. A connection used without ``with``
    (the executor lease holds one for its session locks) is never returned: it stays the
    caller's until the caller closes it.
    """

    _home = None

    def __exit__(self, exc_type, exc_val, exc_tb):
        home = self._home
        if home is None:
            return super().__exit__(exc_type, exc_val, exc_tb)
        if self.closed:
            return
        if exc_type:
            try:
                self.rollback()
            except Exception:
                self.close()  # Never reused; the original exception still propagates.
                return
        else:
            try:
                self.commit()
            except BaseException:
                self.close()
                raise
        home._release(self)


class Repository:
    # Connection reuse (package pass-speed, 2026-09-28). On Railway a new TLS connection took
    # about 22 ms and a query round trip 1.7 ms, and the protection pass opened 32 connections
    # with five watched setups, so most of its 1.7 s went to connecting. A reusing repository
    # keeps up to REUSE_IDLE_MAX idle connections and hands out the most recently used one.
    # Nothing a caller sees changes: every ``with`` still commits or rolls back, the code's
    # session settings are all transaction-scoped (SET LOCAL, SET TRANSACTION), and a
    # connection that was closed, broken, switched to autocommit, given another row factory or
    # left inside a transaction is closed instead of reused. One idle longer than
    # REUSE_PING_AFTER_SECONDS is checked first, so a dead one is replaced, never handed out.
    REUSE_IDLE_MAX = 8
    REUSE_PING_AFTER_SECONDS = 5
    REUSE_IDLE_SECONDS = 60
    REUSE_LIFETIME_SECONDS = 1800

    def __init__(self, database_url: str, *, reuse_connections: bool = False):
        self.database_url = database_url
        self._idle = [] if reuse_connections else None
        self._idle_lock = threading.Lock()

    def connect(self):
        if self._idle is None:
            return psycopg.connect(
                self.database_url, row_factory=dict_row, options="-c timezone=UTC",
                connect_timeout=5,
            )
        return self._checkout()

    def _checkout(self):
        while True:
            now = time.monotonic()
            with self._idle_lock:
                keep, expired = [], []
                for conn in self._idle:
                    stale = (now - conn._released > self.REUSE_IDLE_SECONDS
                             or now - conn._opened > self.REUSE_LIFETIME_SECONDS)
                    (expired if stale else keep).append(conn)
                self._idle = keep
                conn = self._idle.pop() if self._idle else None
            for old in expired:
                old.close()
            if conn is None:
                break
            if now - conn._released <= self.REUSE_PING_AFTER_SECONDS or self._alive(conn):
                return conn
            conn.close()
        conn = ReusableConnection.connect(
            self.database_url, row_factory=dict_row, options="-c timezone=UTC",
            connect_timeout=5,
        )
        # Fresh connections never prepared statements; reused ones do not either.
        conn.prepare_threshold = None
        conn._home, conn._opened = self, time.monotonic()
        return conn

    @staticmethod
    def _alive(conn):
        try:
            conn.autocommit = True
            conn.execute("SELECT 1")
            conn.autocommit = False
            return True
        except Exception:
            return False

    def _release(self, conn):
        reusable = (
            not conn.closed and not conn.broken and not conn.autocommit
            and conn.row_factory is dict_row
            and conn.info.transaction_status == TransactionStatus.IDLE
        )
        if reusable:
            conn._released = time.monotonic()
            with self._idle_lock:
                if len(self._idle) < self.REUSE_IDLE_MAX:
                    self._idle.append(conn)
                    return
        conn.close()

    def close_idle(self):
        """Close every idle connection (a reusing repository only; otherwise nothing)."""
        if self._idle is None:
            return
        with self._idle_lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    def check_role(self):
        with self.connect() as conn:
            row = conn.execute("""
                SELECT current_user AS role, rolsuper, rolcreaterole, rolcreatedb,
                    has_table_privilege(current_user, 'lab.trade_events', 'UPDATE') AS can_update,
                    has_table_privilege(current_user, 'lab.trade_events', 'DELETE') AS can_delete,
                    has_table_privilege(current_user, 'lab.trade_events', 'TRUNCATE')
                        AS can_truncate,
                    has_schema_privilege(current_user, 'lab', 'CREATE') AS can_create
                FROM pg_roles WHERE rolname = current_user
            """).fetchone()
            if row["role"] != "catalyst_app" or any(v for k, v in row.items() if k != "role"):
                raise RuntimeError("Service requires the restricted catalyst_app database role")
            if (
                conn.execute("SELECT max(version) AS v FROM lab.schema_migrations").fetchone()["v"]
                != SCHEMA_VERSION
            ):
                raise RuntimeError("Unsupported schema migration version")

    @staticmethod
    def append_event(conn, event_type, payload, candidate_id=None, correction_of=None):
        return conn.execute(
            """
            INSERT INTO lab.trade_events(candidate_id, strategy_version, event_type,
                                         payload_json, correction_of)
            VALUES (%s, %s, %s, %s, %s) RETURNING event_id, seq, event_hash
        """,
            (candidate_id, STRATEGY_VERSION, event_type, Jsonb(json_safe(payload)), correction_of),
        ).fetchone()

    def transition(self, conn, candidate_id, before, after, details=None):
        self.append_event(
            conn,
            "STATE_TRANSITION",
            {**(details or {}), "from_state": before, "to_state": after},
            candidate_id,
        )

    @staticmethod
    def _prefetch_evidence(raw, now, provider, context):
        model_body = (
            {"strategy_version": context["strategy_version"], **raw}
            if context and isinstance(raw, dict)
            else raw
        )
        try:
            candidate = Candidate.model_validate(model_body)
        except ValidationError:
            return None, None
        try:
            return provider(candidate, now), None
        except EvidenceUnavailable as exc:
            return None, exc
        except Exception:
            # Providers can contain credentials in exceptions; retain only a fixed failure rule.
            return None, None

    def submit_batch(self, batch, now, provider, policy, *, decision_clock=None):
        batch_id = str(uuid4())
        context = {
            "batch_id": batch_id,
            "date": batch.date.isoformat(),
            "strategy_version": batch.strategy_version,
        }
        # No network call may hold the account/audit transaction lock. All records still
        # commit atomically; freshness is checked again after acquiring that lock.
        prepared = [
            self._prefetch_evidence(raw, now, provider, context) for raw in batch.candidates
        ]
        with self.connect() as conn:
            items = [
                self.submit(
                    raw,
                    now,
                    provider,
                    policy,
                    submission_context=context | {"index": index},
                    connection=conn,
                    _evidence_result=prepared[index],
                    decision_clock=decision_clock,
                )
                for index, raw in enumerate(batch.candidates)
            ]
        return {**context, "items": items}

    def submit(
        self,
        raw,
        now: datetime,
        provider,
        policy: Policy | None,
        submission_context=None,
        connection=None,
        *,
        _evidence_result=None,
        decision_clock=None,
    ):
        candidate_id = uuid4()
        body = raw if isinstance(raw, dict) else {}
        context = submission_context or {}
        model_body = (
            {"strategy_version": context["strategy_version"], **raw}
            if context and isinstance(raw, dict)
            else raw
        )
        ticker = body.get("ticker")
        ticker = ticker.strip().upper() if isinstance(ticker, str) else None
        if ticker and not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", ticker):
            ticker = None
        signal = body.get("signal_id")
        signal = signal.strip() if isinstance(signal, str) else None
        if signal and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", signal):
            signal = None
        version = context.get("strategy_version", body.get("strategy_version"))
        version = version if isinstance(version, str) else None
        session = now.astimezone(NEW_YORK).date()
        prepared = (
            _evidence_result
            if _evidence_result is not None
            else self._prefetch_evidence(raw, now, provider, context)
        )
        with nullcontext(connection) if connection else self.connect() as conn:
            # One transaction retains raw input, claims, all decisions and all audit events.
            # Serialize before claims to prevent concurrent duplicate acceptance and lock inversion.
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            evaluated_at = decision_clock() if decision_clock else now
            conn.execute(
                """
                INSERT INTO lab.candidates(candidate_id, payload_json, strategy_version,
                                           ticker, signal_id, session_date, submission_context)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    candidate_id,
                    Jsonb(raw),
                    STRATEGY_VERSION,
                    ticker,
                    signal,
                    session,
                    Jsonb(context),
                ),
            )
            self.append_event(
                conn,
                "CANDIDATE_RECEIVED",
                {"candidate": raw, "submission_context": context},
                candidate_id,
            )
            self.transition(conn, candidate_id, None, "RECEIVED")
            self.transition(conn, candidate_id, "RECEIVED", "VALIDATING")
            duplicate_signal = False
            duplicate_ticker = False
            if signal:
                duplicate_signal = (
                    conn.execute(
                        """
                    INSERT INTO lab.signal_claims VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """,
                        (signal, candidate_id),
                    ).rowcount
                    == 0
                )
            if ticker:
                duplicate_ticker = (
                    conn.execute(
                        """
                    INSERT INTO lab.ticker_day_claims VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                """,
                        (ticker, session, candidate_id),
                    ).rowcount
                    == 0
                )
            evidence: Evidence | None = None
            if duplicate_signal:
                decision = reject("DUPLICATE_SIGNAL_ID", "Signal ID was already attempted")
            elif duplicate_ticker:
                decision = reject(
                    "TICKER_ALREADY_ATTEMPTED", "Only one ticker attempt per session day"
                )
            elif context and context["date"] != session.isoformat():
                decision = reject(
                    "CANDIDATE_DATE_MISMATCH",
                    "Batch date must match the current New York session day",
                )
            elif context and body.get("strategy_version", version) != version:
                decision = reject(
                    "STRATEGY_VERSION_MISMATCH",
                    "Candidate cannot override the envelope strategy version",
                )
            else:
                try:
                    candidate = Candidate.model_validate(model_body)
                except ValidationError as exc:
                    fields = sorted({".".join(map(str, e["loc"])) or "body" for e in exc.errors()})
                    decision = reject(
                        "INVALID_SCHEMA", "Invalid candidate fields: " + ", ".join(fields)
                    )
                else:
                    evidence, unavailable = prepared
                    decision = validate(candidate, evidence, policy, evaluated_at)
                    if evaluated_at.astimezone(NEW_YORK).date() != session:
                        decision = reject(
                            "MARKET_SESSION_CLOSED", "Session changed during admission"
                        )
                    if unavailable and decision.failed_rule == "DATA_FEED_FAILURE":
                        decision = reject(unavailable.rule, unavailable.reason)
            evidence_json = json_safe(asdict(evidence)) if evidence else None
            policy_json = json_safe(asdict(policy)) if policy else None
            decision_payload = {
                **json_safe(asdict(decision)),
                "evidence": evidence_json,
                "policy": policy_json,
                "evaluated_at": evaluated_at.isoformat(),
                "risk_stage": "PRELIMINARY_VALIDATION_ONLY",
            }
            event = self.append_event(conn, "VALIDATION_DECISION", decision_payload, candidate_id)
            conn.execute(
                """
                INSERT INTO lab.validation_decisions(candidate_id, event_id, passed,
                    failed_rule, reason, expires_at, evidence_json, policy_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    candidate_id,
                    event["event_id"],
                    decision.passed,
                    decision.failed_rule,
                    decision.reason,
                    decision.expires_at,
                    Jsonb(evidence_json),
                    Jsonb(policy_json),
                ),
            )
            state = "VALIDATED" if decision.passed else "REJECTED"
            self.transition(conn, candidate_id, "VALIDATING", state)
            if decision.failed_rule in {
                "DATA_FEED_FAILURE",
                "DAILY_RISK_HALT",
                "STARTUP_RECONCILIATION_REQUIRED",
            }:
                system = self.append_event(
                    conn,
                    "SYSTEM_EVENT",
                    {"kind": decision.failed_rule, "phase": "LAB_CORE", "execution_enabled": False},
                    candidate_id,
                )
                conn.execute(
                    """
                    INSERT INTO lab.system_events(event_id, event_type, payload_json)
                    VALUES (%s, %s, %s)
                """,
                    (
                        system["event_id"],
                        decision.failed_rule,
                        Jsonb({"candidate_id": str(candidate_id)}),
                    ),
                )
        return self.get_candidate(candidate_id, connection=connection)

    @staticmethod
    def status_record(row):
        from catalyst_lab.analytics import measured_trade

        item = json_safe(row)
        metrics = measured_trade(row)
        state = item["state"]
        status = {
            "REJECTED": "rejected",
            "RISK_REJECTED": "rejected",
            "BROKER_REJECTED": "rejected",
            "EXPIRED_UNTRIGGERED": "expired",
            "PARTIALLY_FILLED": "open",
            "FILLED": "open",
            "OPEN": "open",
        }.get(state, "not_entered")
        if state == "CLOSED":
            status = {
                "TARGET_EXIT": "closed_target",
                "STOP_EXIT": "closed_stop",
                "TIME_EXIT": "closed_time",
            }.get(item.get("state_reason"), "closed_other")
        qty = row.get("entry_qty")
        return {
            **item,
            "date": item["submission_context"].get("date", item["session_date"]),
            "status": status,
            "strategy_eligible": item["record_purpose"] == "STRATEGY",
            "rejection_reason": item["failed_rule"]
            or (
                item.get("state_reason") if state in {"BROKER_REJECTED", "RISK_REJECTED"} else None
            ),
            "state_reason": item.get("state_reason"),
            "fill_price": item.get("average_entry"),
            "size_shares": (int(qty) if qty == int(qty) else str(qty)) if qty else None,
            "mfe_r": str(metrics["mfe_r"]) if metrics["mfe_r"] is not None else None,
            "mae_r": str(metrics["mae_r"]) if metrics["mae_r"] is not None else None,
            "gross_r": str(metrics["r_multiple"]) if metrics["r_multiple"] is not None else None,
            "r_method": metrics["r_method"],
            "exit_price": item.get("average_exit"),
            "net_r": None,
            "notes": item.get("state_reason") or item["reason"],
        }

    def get_candidate(self, candidate_id, connection=None):
        with nullcontext(connection) if connection else self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, s.state, d.passed, d.failed_rule, d.reason, d.expires_at,
                    d.evidence_json->>'source' AS evidence_source,
                    e.payload_json->>'reason' AS state_reason,
                    f.entry_qty, f.average_entry, f.average_exit,
                    m.r_actual_filled,m.r_planned_filled,m.r_authorized_order,
                    m.initial_risk_dollars,m.planned_filled_risk,m.authorized_order_risk,
                    m.mfe,m.mae,m.broker_paper_pnl,m.coverage_json,m.feeds_json
                FROM lab.candidates c JOIN lab.candidate_states s USING(candidate_id)
                JOIN lab.trade_events e ON e.seq = s.last_event_seq
                JOIN lab.validation_decisions d ON d.candidate_id = c.candidate_id
                LEFT JOIN lab.trades m ON m.candidate_id=c.candidate_id
                LEFT JOIN LATERAL (
                    SELECT sum(f.qty) FILTER(WHERE f.side='buy') AS entry_qty,
                      sum(f.qty*f.price) FILTER(WHERE f.side='buy') /
                        nullif(sum(f.qty) FILTER(WHERE f.side='buy'),0) AS average_entry,
                      sum(f.qty*f.price) FILTER(WHERE f.side='sell') /
                        nullif(sum(f.qty) FILTER(WHERE f.side='sell'),0) AS average_exit
                    FROM lab.fills f JOIN lab.orders o USING(order_id)
                    WHERE o.candidate_id=c.candidate_id
                ) f ON true
                WHERE c.candidate_id = %s
            """,
                (candidate_id,),
            ).fetchone()
            return self.status_record(row) if row else None

    def list_candidates(self, limit=50, offset=0, session_date=None):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, s.state, d.passed, d.failed_rule, d.reason, d.expires_at,
                    d.evidence_json->>'source' AS evidence_source,
                    e.payload_json->>'reason' AS state_reason,
                    f.entry_qty, f.average_entry, f.average_exit,
                    m.r_actual_filled,m.r_planned_filled,m.r_authorized_order,
                    m.initial_risk_dollars,m.planned_filled_risk,m.authorized_order_risk,
                    m.mfe,m.mae,m.broker_paper_pnl,m.coverage_json,m.feeds_json
                FROM lab.candidates c JOIN lab.candidate_states s USING(candidate_id)
                JOIN lab.trade_events e ON e.seq = s.last_event_seq
                JOIN lab.validation_decisions d ON d.candidate_id = c.candidate_id
                LEFT JOIN lab.trades m ON m.candidate_id=c.candidate_id
                LEFT JOIN LATERAL (
                    SELECT sum(f.qty) FILTER(WHERE f.side='buy') AS entry_qty,
                      sum(f.qty*f.price) FILTER(WHERE f.side='buy') /
                        nullif(sum(f.qty) FILTER(WHERE f.side='buy'),0) AS average_entry,
                      sum(f.qty*f.price) FILTER(WHERE f.side='sell') /
                        nullif(sum(f.qty) FILTER(WHERE f.side='sell'),0) AS average_exit
                    FROM lab.fills f JOIN lab.orders o USING(order_id)
                    WHERE o.candidate_id=c.candidate_id
                ) f ON true
                WHERE (%s::text IS NULL OR
                    coalesce(c.submission_context->>'date', c.session_date::text) = %s)
                ORDER BY c.received_at DESC, c.candidate_id LIMIT %s OFFSET %s
            """,
                (session_date, session_date, limit, offset),
            ).fetchall()
            return [self.status_record(row) for row in rows]

    def candidate_events(self, candidate_id):
        with self.connect() as conn:
            return json_safe(
                conn.execute(
                    """
                SELECT seq, event_id, event_type, payload_json, created_at,
                    previous_hash, event_hash FROM lab.trade_events
                WHERE candidate_id = %s ORDER BY seq
            """,
                    (candidate_id,),
                ).fetchall()
            )

    def export_events(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM lab.trade_events ORDER BY seq").fetchall()
        result = []
        for row in rows:
            item = json_safe(row)
            item["created_at"] = row["created_at"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result.append(item)
        return result

    def analytics(self):
        with self.connect() as conn:
            counts = conn.execute("""
                SELECT s.state, count(*) AS count FROM lab.candidate_states s
                JOIN lab.strategy_candidates c USING(candidate_id) GROUP BY s.state
            """).fetchall()
            trades = conn.execute(
                """
                SELECT count(*) AS count FROM lab.strategy_trades
                WHERE market = 'US' AND execution_source = 'ALPACA_PAPER'
                  AND strategy_version = %s
            """,
                (STRATEGY_VERSION,),
            ).fetchone()["count"]
        return {
            "label": "PAPER TRADING — SIMULATED. Not real money.",
            "strategy_version": STRATEGY_VERSION,
            "market": "US",
            "execution_source": "ALPACA_PAPER",
            "record_purpose": "STRATEGY",
            "engineering_tests_excluded": True,
            "trade_count": trades,
            "candidate_counts": {r["state"]: r["count"] for r in counts},
            "metrics_status": "AVAILABLE_AT_STATS_ENDPOINT",
        }

    def execution_status(self):
        with self.connect() as conn:
            positions = conn.execute("""SELECT p.*,c.record_purpose FROM lab.strategy_positions p
                JOIN lab.candidates c USING(candidate_id)""").fetchall()
            orders = conn.execute("""SELECT b.*,o.record_purpose FROM lab.broker_order_states b
                JOIN lab.orders o USING(order_id)""").fetchall()
            last = conn.execute(
                "SELECT * FROM lab.reconciliation_runs ORDER BY event_seq DESC LIMIT 1"
            ).fetchone()
            reasons = conn.execute("SELECT DISTINCT reason FROM lab.execution_halts").fetchall()
        return json_safe(
            {
                "trading_enabled": False,
                "positions": positions,
                "orders": orders,
                "last_reconciliation": last,
                "halted": bool(reasons),
                "halt_reasons": sorted(r["reason"] for r in reasons),
            }
        )
