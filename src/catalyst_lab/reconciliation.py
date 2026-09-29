"""Compare broker snapshots with event-derived positions/orders; never repair either side."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.broker_ledger import TERMINAL, normalize_order
from catalyst_lab.execution import halt, system_event
from catalyst_lab.market import NY, MarketDataError
from catalyst_lab.repository import json_safe


def broker_snapshot(positions, orders):
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise MarketDataError("INVALID_BROKER_SNAPSHOT")
    result_positions, result_orders = {}, {}
    for position in positions:
        try:
            ticker = position["symbol"]
            qty = Decimal(str(position["qty"]))
            if (
                not isinstance(ticker, str)
                or not ticker
                or not qty.is_finite()
                or ticker in result_positions
            ):
                raise ValueError
            if qty:
                result_positions[ticker] = qty
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise MarketDataError("INVALID_BROKER_POSITION") from None
    pending = list(orders)
    while pending:
        raw = pending.pop()
        order = normalize_order(raw)
        children = raw.get("legs") or []
        if not isinstance(children, list):
            raise MarketDataError("INVALID_BROKER_LEGS")
        pending.extend(children)
        if len(result_orders) + len(pending) > 5000:
            raise MarketDataError("INCOMPLETE_BROKER_SNAPSHOT")
        if order["status"] in TERMINAL:
            continue
        old = result_orders.get(order["id"])
        if old is not None and old != order:
            raise MarketDataError("CONFLICTING_BROKER_ORDER_SNAPSHOT")
        result_orders[order["id"]] = order
    return result_positions, result_orders


def compare(local_positions, local_orders, positions, orders):
    discrepancies = []
    for ticker in sorted(set(local_positions) | set(positions)):
        local_qty, broker_qty = (
            local_positions.get(ticker, Decimal(0)),
            positions.get(ticker, Decimal(0)),
        )
        if local_qty != broker_qty:
            discrepancies.append(
                {
                    "code": "POSITION_QTY_MISMATCH",
                    "ticker": ticker,
                    "local_qty": local_qty,
                    "broker_qty": broker_qty,
                }
            )
    for oid in sorted(set(local_orders) | set(orders)):
        local, broker = local_orders.get(oid), orders.get(oid)
        if local is None or broker is None:
            discrepancies.append(
                {
                    "code": "UNEXPLAINED_BROKER_ORDER"
                    if local is None
                    else "LOCAL_ORDER_MISSING_AT_BROKER",
                    "broker_order_id": oid,
                }
            )
            continue
        for field in (
            "symbol",
            "side",
            "qty",
            "filled_qty",
            "type",
            "limit_price",
            "stop_price",
            "status",
        ):
            if local[field] != broker[field]:
                discrepancies.append(
                    {
                        "code": "ORDER_FIELD_MISMATCH",
                        "broker_order_id": oid,
                        "field": field,
                        "local": local[field],
                        "broker": broker[field],
                    }
                )
    return discrepancies


class Reconciler:
    @staticmethod
    def _watermark(conn):
        # HTTP receipts/claims can race a REST snapshot even before a stream event arrives.
        return conn.execute("""SELECT greatest(
            (SELECT coalesce(max(event_seq),0) FROM lab.broker_events),
            (SELECT coalesce(max(event_seq),0) FROM lab.order_links),
            (SELECT coalesce(max(event_seq),0) FROM lab.authorization_claims),
            (SELECT coalesce(max(event_seq),0) FROM lab.authorization_results)) AS n""").fetchone()[
            "n"
        ]

    def __init__(self, repo, client, *, clock=None, interval_seconds=45, baseline_recorder=None):
        if not 30 <= interval_seconds <= 60:
            raise ValueError("Reconciliation interval must be 30–60 seconds")
        self.repo, self.client = repo, client
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval_seconds = interval_seconds
        self.run_id = uuid4()
        self.startup_clean = False
        self.last_completed = None
        self.last_clean = False
        self.session_date = None
        self.error = "STARTUP_RECONCILIATION_REQUIRED"
        self.baseline_recorder = baseline_recorder

    def ready(self, now=None):
        now = now or self.clock()
        if (
            not self.startup_clean
            or not self.last_clean
            or self.last_completed is None
            or self.session_date != now.astimezone(NY).date()
            or not 0 <= (now - self.last_completed).total_seconds() <= 60
        ):
            return False
        with self.repo.connect() as conn:
            return not conn.execute(
                "SELECT EXISTS(SELECT 1 FROM lab.execution_halts) AS halted"
            ).fetchone()["halted"]

    def status(self):
        return {
            "startup_clean": self.startup_clean,
            "last_snapshot_clean": self.last_clean,
            "completed_at": self.last_completed.isoformat() if self.last_completed else None,
            "interval_seconds": self.interval_seconds,
            "ready": self.ready(),
            "error": self.error,
        }

    def run_once(self):
        started = self.clock()
        startup = not self.startup_clean or self.session_date != started.astimezone(NY).date()
        with self.repo.connect() as conn:
            watermark = self._watermark(conn)
        snapshot, discrepancies, latch_halt = {}, [], False
        session = None
        account = {}
        try:
            if startup and self.baseline_recorder:
                day = started.astimezone(NY).date()
                session = next(
                    (s for s in self.client.calendar(day, day) if s.session_date == day), None
                )
            account = (
                self.client.account()
            )  # Authenticate against the paper host, not the data host.
            if account.get("status") != "ACTIVE":
                raise MarketDataError("PAPER_ACCOUNT_NOT_ACTIVE")
            positions, orders = broker_snapshot(self.client.positions(), self.client.open_orders())
            snapshot = json_safe({"positions": positions, "orders": orders})
        except MarketDataError as exc:
            discrepancies = [{"code": str(exc)}]
        except Exception:
            discrepancies = [{"code": "BROKER_SNAPSHOT_FAILED"}]
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            if not discrepancies:
                after = self._watermark(conn)
                if after != watermark:
                    discrepancies = [{"code": "BROKER_UPDATE_DURING_SNAPSHOT"}]
                else:
                    local_positions = {
                        r["ticker"]: r["qty"]
                        for r in conn.execute("""
                        SELECT ticker,sum(qty) AS qty FROM lab.strategy_positions
                        GROUP BY ticker""").fetchall()
                    }
                    local_orders = {}
                    for row in conn.execute("SELECT * FROM lab.broker_order_states").fetchall():
                        if row["status"] not in TERMINAL:
                            local_orders[row["broker_order_id"]] = {
                                "symbol": row["symbol"],
                                "side": row["side"],
                                "qty": Decimal(row["qty"]),
                                "filled_qty": row["filled_qty"],
                                "type": row["order_type"],
                                "limit_price": row["limit_price"],
                                "stop_price": row["stop_price"],
                                "status": row["status"],
                            }
                    discrepancies = compare(local_positions, local_orders, positions, orders)
                    latch_halt = bool(discrepancies)
                    pending = conn.execute("""SELECT 1 FROM lab.authorization_claims c
                        LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                        WHERE r.outcome IS NULL OR r.outcome IN ('UNKNOWN','NOT_FOUND')
                        LIMIT 1""").fetchone()
                    if pending:
                        # Preserve all differences and close the readiness gate. Recovery must
                        # resolve the claim before a later clean comparison can reopen it.
                        discrepancies.append({"code": "BROKER_AUTHORIZATION_IN_FLIGHT"})
                        latch_halt = False
            completed = self.clock()
            kind = "STARTUP_RECONCILIATION" if startup else "BROKER_RECONCILIATION"
            event = system_event(
                self.repo,
                conn,
                kind,
                {
                    "clean": not discrepancies,
                    "process_run_id": str(self.run_id),
                    "started_at": started,
                    "completed_at": completed,
                    "discrepancies": discrepancies,
                    "snapshot": snapshot,
                },
            )
            conn.execute(
                """INSERT INTO lab.reconciliation_runs VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    event["seq"],
                    self.run_id,
                    started.astimezone(NY).date(),
                    started,
                    completed,
                    startup,
                    not discrepancies,
                    Jsonb(json_safe(discrepancies)),
                    Jsonb(snapshot),
                ),
            )
            if startup and session is not None and account.get("status") == "ACTIVE":
                self.baseline_recorder(conn, session, account, event["seq"])
            for discrepancy in discrepancies:
                system_event(self.repo, conn, "BROKER_MISMATCH", discrepancy)
            if latch_halt:
                halt(
                    self.repo,
                    conn,
                    "BROKER_RECONCILIATION_MISMATCH",
                    {"reconciliation_seq": event["seq"]},
                )
        self.last_completed, self.last_clean = completed, not discrepancies
        self.session_date = started.astimezone(NY).date()
        if self.last_clean:
            self.startup_clean = True
        self.error = discrepancies[0]["code"] if discrepancies else None
        return {
            "clean": self.last_clean,
            "discrepancies": json_safe(discrepancies),
            "event_seq": event["seq"],
        }
