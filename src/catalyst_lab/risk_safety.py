"""Durable cancel/flatten workflows for daily halt, calendar exits and failed protection."""

from decimal import Decimal
from uuid import uuid4

from catalyst_lab.broker_ledger import (
    PROTECTED,
    TERMINAL,
    TRANSITIONING,
    classify_bracket,
    normalize_order,
    within_transition_grace,
)
from catalyst_lab.execution import system_event
from catalyst_lab.market import NY, MarketDataError
from catalyst_lab.risk import signed


class RiskSafety:
    # Reasons a durable exit request may carry; the shared-account coordinator adds its own.
    EXIT_REASONS = frozenset(
        {"DAILY_RISK_HALT", "PROTECTION_FAILURE", "TIME_EXIT", "CONTROLLED_ACCEPTANCE"}
    )

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.engine = dispatcher.engine
        self.repo, self.client = self.engine.repo, self.engine.client
        dispatcher.safety = self
        # First time each candidate's bracket was seen transitioning (process-local: a
        # restart grants at most one more grace of TRANSITION_GRACE_SECONDS).
        self._transitioning = {}

    @staticmethod
    def flatten_orders(rows):
        pending = list(rows)
        result = {}
        while pending:
            raw = pending.pop()
            item = normalize_order(raw)
            if item["status"] not in TERMINAL:
                result[item["id"]] = raw
            pending.extend(raw.get("legs") or [])
        return list(result.values())

    def request_exit(self, ticker, reason, candidate_id=None):
        if reason not in self.EXIT_REASONS:
            raise ValueError("Unknown risk exit reason")
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            existing = conn.execute(
                "SELECT * FROM lab.pending_risk_exits WHERE ticker=%s", (ticker,)
            ).fetchone()
            if existing:
                return existing
            request_id = uuid4()
            day = self.engine.now().astimezone(NY).date()
            event = system_event(
                self.repo,
                conn,
                "RISK_EXIT_REQUESTED",
                {"exit_request_id": str(request_id), "ticker": ticker, "reason": reason},
                candidate_id,
            )
            return conn.execute(
                "INSERT INTO lab.risk_exit_requests VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                (request_id, candidate_id, day, ticker, reason, event["seq"]),
            ).fetchone()

    def _candidate_for(self, ticker):
        with self.repo.connect() as conn:
            row = conn.execute(
                """SELECT candidate_id FROM lab.active_reservations r
                JOIN lab.candidates c USING(candidate_id) WHERE c.ticker=%s""",
                (ticker,),
            ).fetchone()
            return row["candidate_id"] if row else None

    def _cancel(self, order, reason, exit_request=None):
        oid = order["id"]
        account = self.client.account()
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            existing = conn.execute(
                """SELECT d.* FROM lab.risk_decisions d
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE d.action='CANCEL' AND d.path=%s AND d.decision='APPROVED'
                 AND (r.outcome IS NULL OR r.outcome NOT IN ('EXPIRED','REJECTED'))
                ORDER BY d.decided_at DESC LIMIT 1""",
                ("/v2/orders/" + oid,),
            ).fetchone()
            decision = existing or self.engine._decision(
                conn,
                candidate_id=exit_request["candidate_id"]
                if exit_request
                else self._candidate_for(order["symbol"]),
                action="CANCEL",
                session_date=self.engine.now().astimezone(NY).date(),
                equity=signed(account["equity"]),
                reason=reason,
                approved=True,
                method="DELETE",
                path="/v2/orders/" + oid,
                context={
                    "broker_order": order,
                    "exit_request_id": str(exit_request["exit_request_id"])
                    if exit_request
                    else None,
                },
            )
        self.dispatcher.dispatch(decision["risk_decision_id"])

    def daily_halt(self):
        day = self.engine.now().astimezone(NY).date()
        with self.repo.connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s", (day,)
            ).fetchone():
                return
        # Requests are durable before protective-leg cancellations can arrive on the stream.
        positions = self.client.positions()
        orders = self.flatten_orders(self.client.open_orders())
        tickers = {p["symbol"] for p in positions} | {o["symbol"] for o in orders}
        for ticker in tickers:
            self.request_exit(ticker, "DAILY_RISK_HALT", self._candidate_for(ticker))
        self.process_exits()

    def check_protection(self, candidate_id):
        with self.repo.connect() as conn:
            order = conn.execute(
                "SELECT * FROM lab.orders WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM lab.pending_risk_exits WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
        if not order:
            return
        if pending:
            self.process_exits()
            return
        receipt = self.client.order(order["alpaca_order_id"])
        with self.repo.connect() as conn:
            intent = conn.execute(
                "SELECT payload_json FROM lab.order_intents WHERE intent_id=%s",
                (order["intent_id"],),
            ).fetchone()["payload_json"]
        positions = {p["symbol"]: p for p in self.client.positions()}
        position = positions.get(order["ticker"])
        if position is None or signed(position["qty"]) == 0:
            self._transitioning.pop(str(candidate_id), None)
            return
        qty = signed(position["qty"])
        legs = []
        for raw in (receipt or {}).get("legs") or []:
            leg = normalize_order(raw)
            role = {"stop": "STOP", "limit": "TARGET"}.get(leg["type"])
            if role:
                legs.append((role, leg))
        # Rule A (shared with the managed controller): a full fill leaves the take-profit
        # active and the stop-loss leg active or held; held legs never protect a partially
        # filled entry, which is flattened rather than waited for.
        classification = "NON_LONG_POSITION"
        if qty > 0:
            result = classify_bracket(
                receipt,
                legs,
                position_qty=qty,
                stop_prices={Decimal(intent["stop_loss"]["stop_price"])},
                target_prices={Decimal(intent["take_profit"]["limit_price"])},
            )
            classification = result.reason
            if result.status == PROTECTED:
                self._transitioning.pop(str(candidate_id), None)
                return
            if result.status == TRANSITIONING:
                now = self.engine.now()
                since = self._transitioning.setdefault(str(candidate_id), now)
                if within_transition_grace(since, now):
                    return  # No flatten inside the grace; the next check decides again.
        self._transitioning.pop(str(candidate_id), None)
        request = self.request_exit(order["ticker"], "PROTECTION_FAILURE", candidate_id)
        with self.repo.connect() as conn:
            system_event(
                self.repo,
                conn,
                "PROTECTIVE_EXIT_FAILURE",
                {
                    "exit_request_id": str(request["exit_request_id"]),
                    "broker_order": receipt,
                    "position": position,
                    "classification": classification,
                },
                candidate_id,
            )
        self.process_exits()

    def calendar_exits(self, session):
        if session is None or self.engine.now().astimezone(NY).date() != session.session_date:
            return
        if self.engine.now() < session.flatten_time:
            return
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT r.candidate_id,c.ticker FROM lab.active_reservations r
                JOIN lab.candidates c USING(candidate_id)"""
            ).fetchall()
        for row in rows:
            self.request_exit(row["ticker"], "TIME_EXIT", row["candidate_id"])
        self.process_exits()

    def process_exits(self):
        # Serialize with dispatch/stream projection; never race two sell intents for one position.
        with self.dispatcher.lock:
            with self.repo.connect() as conn:
                requests = conn.execute(
                    "SELECT * FROM lab.pending_risk_exits ORDER BY event_seq"
                ).fetchall()
            for request in requests:
                self._process_exit(request)

    def _process_exit(self, request):
        ticker = request["ticker"]
        orders = [
            o for o in self.flatten_orders(self.client.open_orders()) if o["symbol"] == ticker
        ]
        # A previously authorized market close is allowed to finish; never cancel and duplicate it.
        with self.repo.connect() as conn:
            exits = conn.execute(
                """SELECT d.*,c.risk_decision_id IS NOT NULL AS claimed,r.outcome
                FROM lab.risk_decisions d
                LEFT JOIN lab.authorization_claims c USING(risk_decision_id)
                LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
                WHERE d.action='FLATTEN' AND d.context_json->>'exit_request_id'=%s
                ORDER BY d.decided_at""",
                (str(request["exit_request_id"]),),
            ).fetchall()
        closing_ids = {d["payload_json"]["client_order_id"] for d in exits}
        for order in orders:
            if order.get("client_order_id") not in closing_ids:
                self._cancel(order, request["reason"], request)
        orders = [
            o for o in self.flatten_orders(self.client.open_orders()) if o["symbol"] == ticker
        ]
        if any(o.get("client_order_id") not in closing_ids for o in orders):
            return
        for previous in exits:
            if previous["outcome"] in {"EXPIRED", "REJECTED"}:
                continue
            broker = (
                self.dispatcher.recover(previous["risk_decision_id"])
                if previous["claimed"]
                else self.dispatcher.dispatch(previous["risk_decision_id"])
            )
            if broker is None or broker.get("status") not in TERMINAL:
                return
        positions = {p["symbol"]: p for p in self.client.positions()}
        position = positions.get(ticker)
        if not position or signed(position["qty"]) == 0:
            # No entry/protective order may remain that could reopen or reverse the position.
            if orders:
                return
            with self.repo.connect() as conn:
                # REST can show flat before the exit fill arrives. Keep the safety workflow
                # active until the ledger also sees flat, so delayed OCO cancellations remain
                # expected and the reservation cannot be released before fill evidence.
                if (
                    request["candidate_id"]
                    and conn.execute(
                        "SELECT 1 FROM lab.strategy_positions WHERE candidate_id=%s",
                        (request["candidate_id"],),
                    ).fetchone()
                ):
                    return
                if conn.execute(
                    "SELECT 1 FROM lab.risk_exit_completions WHERE exit_request_id=%s",
                    (request["exit_request_id"],),
                ).fetchone():
                    return
                event = system_event(
                    self.repo,
                    conn,
                    "RISK_EXIT_CONFIRMED_FLAT",
                    {"exit_request_id": str(request["exit_request_id"]), "ticker": ticker},
                    request["candidate_id"],
                )
                conn.execute(
                    "INSERT INTO lab.risk_exit_completions VALUES(%s,%s)",
                    (request["exit_request_id"], event["seq"]),
                )
                if request["candidate_id"]:
                    self.engine.release(
                        request["candidate_id"], "BROKER_CONFIRMED_FLAT", connection=conn
                    )
            return
        qty = abs(signed(position["qty"]))
        session = self.engine.session()
        if session is None or not session.contains(self.engine.now()):
            return  # Retain the durable exit request; never queue an after-hours market order.
        if qty != int(qty):
            raise MarketDataError("FRACTIONAL_POSITION_REQUIRES_MANUAL_RECONCILIATION")
        account = self.client.account()
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            payload = {
                "symbol": ticker,
                "qty": str(int(qty)),
                "side": "sell" if signed(position["qty"]) > 0 else "buy",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": "crtx-" + request["exit_request_id"].hex + "-" + str(len(exits)),
            }
            decision = self.engine._decision(
                conn,
                candidate_id=request["candidate_id"],
                action="FLATTEN",
                session_date=self.engine.now().astimezone(NY).date(),
                equity=signed(account["equity"]),
                reason=request["reason"],
                approved=True,
                method="POST",
                path="/v2/orders",
                payload=payload,
                qty=int(qty),
                context={
                    "exit_request_id": str(request["exit_request_id"]),
                    "broker_position": position,
                    "open_orders_verified_empty": True,
                    "session_opens": session.opens,
                    "session_closes": session.closes,
                },
            )
        self.dispatcher.dispatch(decision["risk_decision_id"])
