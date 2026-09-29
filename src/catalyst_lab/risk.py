"""Atomic risk authorizations, reservations and session risk state."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.account_risk import (
    FROZEN_V1_POLICY_ID,
    VENUE,
    account_risk_failure,
    binding_constraint,
    load_policy,
)
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.config import AUTHORIZATION_TTL_SECONDS, BASELINE_SOURCE, STRATEGY_VERSION
from catalyst_lab.domain import Candidate
from catalyst_lab.execution import ExecutionService, bracket, system_event
from catalyst_lab.market import NY, MarketDataError, decimal
from catalyst_lab.repository import json_safe
from catalyst_lab.risk_math import (
    DAILY_HALT_PCT,
    MAX_OPEN_RISK_PCT,
    RISK_PCT,
    buying_power_check,
    size_entry,
)


@dataclass(frozen=True)
class RiskPolicy:
    authorization_ttl_seconds: int = AUTHORIZATION_TTL_SECONDS
    baseline_source: str = BASELINE_SOURCE
    max_per_sector: int = 1
    max_per_theme: int = 1
    max_spread_bps: Decimal = Decimal("10")

    def __post_init__(self):
        if type(self.authorization_ttl_seconds) is not int or self.authorization_ttl_seconds != 5:
            raise ValueError("CATALYST_RETEST_V1 authorization TTL is frozen at 5 seconds")
        if self.baseline_source != BASELINE_SOURCE:
            raise ValueError("CATALYST_RETEST_V1 requires broker previous-close equity")
        if any(type(n) is not int or n < 1 for n in (self.max_per_sector, self.max_per_theme)):
            raise ValueError("Sector/theme limits must be positive integers")
        if not self.max_spread_bps.is_finite() or not 0 < self.max_spread_bps <= 10:
            raise ValueError("CATALYST_RETEST_V1 spread must be positive and at most 10 bps")


def signed(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError
        return number
    except Exception:
        raise MarketDataError("INVALID_RISK_NUMBER") from None


def import_classifications(repository, rows):
    """Operator-owned mapping import; deliberately absent from the Muse HTTP API."""
    from catalyst_lab.market import symbol

    if not isinstance(rows, list) or not rows:
        raise ValueError("Provide a nonempty classification list")
    checked = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"ticker", "sector", "theme", "source"}:
            raise ValueError("Each mapping requires ticker, sector, theme and source only")
        ticker = symbol(row["ticker"])
        if any(
            not isinstance(row[k], str) or not row[k].strip() for k in ("sector", "theme", "source")
        ):
            raise ValueError("Classification identifiers and provenance must be nonempty")
        checked.append({**row, "ticker": ticker})
    repository.check_role()
    with repository.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        for row in checked:
            event = system_event(repository, conn, "CLASSIFICATION_IMPORTED", row)
            conn.execute(
                "INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                (event["seq"], row["ticker"], row["sector"], row["theme"], row["source"]),
            )
    return len(checked)


class RiskEngine:
    def __init__(
        self, repository: RiskRepository, client, policy: RiskPolicy, *, ready=None, clock=None
    ):
        repository.check_role()
        self.repo, self.client, self.policy = repository, client, policy
        self.ready = ready or (lambda: False)
        self.now = clock or (lambda: datetime.now(UTC))
        # Startup refuses a configured correlation limit (RISK_MAX_PER_SECTOR/THEME) that
        # differs from the archived V1 row: the database checks use only the row.
        with repository.connect() as conn:
            self.account_policy = load_policy(conn, FROZEN_V1_POLICY_ID, engine="FROZEN_V1")
        row = self.account_policy
        if (
            (row.max_per_sector, row.max_per_theme)
            != (policy.max_per_sector, policy.max_per_theme)
            or row.risk_pct != RISK_PCT
            or row.account_cap_pct != MAX_OPEN_RISK_PCT
        ):
            raise ValueError("V1_RISK_POLICY_MISMATCH")

    def classify(self, ticker, sector, theme, source):
        return import_classifications(
            self.repo, [{"ticker": ticker, "sector": sector, "theme": theme, "source": source}]
        )

    def session(self):
        day = self.now().astimezone(NY).date()
        sessions = self.client.calendar(day, day)
        return next((s for s in sessions if s.session_date == day), None)

    def _baseline(self, conn, session, account):
        existing = conn.execute(
            "SELECT * FROM lab.risk_sessions WHERE session_date=%s", (session.session_date,)
        ).fetchone()
        if existing:
            return existing["day_start_equity"]
        raise MarketDataError("DAY_START_EQUITY_REQUIRED")

    def capture_reconciled_baseline(self, conn, session, account, reconciliation_seq):
        """Called only by startup/session reconciliation, in its transaction."""
        existing = conn.execute(
            "SELECT * FROM lab.risk_sessions WHERE session_date=%s", (session.session_date,)
        ).fetchone()
        if existing:
            return existing["day_start_equity"]
        value = decimal(account.get("last_equity"))
        event = system_event(
            self.repo,
            conn,
            "DAY_START_EQUITY",
            {
                "session_date": session.session_date,
                "equity": value,
                "source": "ALPACA_LAST_EQUITY",
                "reconciliation_seq": reconciliation_seq,
            },
        )
        conn.execute(
            "INSERT INTO lab.risk_sessions VALUES(%s,%s,%s,%s,%s,%s)",
            (
                session.session_date,
                value,
                "ALPACA_LAST_EQUITY",
                self.now(),
                event["seq"],
                reconciliation_seq,
            ),
        )
        return value

    def _daily_risk(self, conn, session, account, positions):
        baseline = self._baseline(conn, session, account)
        # Account-wide risk P&L includes fills missed while this process was offline.
        # Remove external cash flows; no performance/R/MFE/MAE analytics are produced here.
        unrealized = sum((signed(p["unrealized_pl"]) for p in positions), Decimal(0))
        total = signed(account["equity"]) - baseline - signed(account["external_cash_flow"])
        realized = total - unrealized
        return baseline, realized, unrealized

    def account_snapshot(self, session):
        activities = self.client.capital_activities(session.session_date)
        if any(a.get("activity_type") not in {"CSD", "CSW", "ACATC"} for a in activities):
            raise MarketDataError("CAPITAL_ACTIVITY_RECONCILIATION_REQUIRED")
        cash_flow = sum((signed(a["net_amount"]) for a in activities), Decimal(0))
        account = self.client.account()
        account["external_cash_flow"] = str(cash_flow)
        account["capital_activity_count"] = len(activities)
        return account

    def _latch_daily(self, conn, session, baseline, realized, unrealized):
        already = conn.execute(
            "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s", (session.session_date,)
        ).fetchone()
        if realized + unrealized <= -DAILY_HALT_PCT * baseline and not already:
            event = system_event(
                self.repo,
                conn,
                "DAILY_RISK_HALT",
                {
                    "session_date": session.session_date,
                    "day_start_equity": baseline,
                    "realized_pnl": realized,
                    "unrealized_pnl": unrealized,
                    "threshold": -DAILY_HALT_PCT * baseline,
                    "action": "CANCEL_AND_FLATTEN",
                },
            )
            conn.execute(
                "INSERT INTO lab.daily_risk_halts VALUES(%s,%s,%s,%s,%s)",
                (
                    session.session_date,
                    realized,
                    unrealized,
                    -DAILY_HALT_PCT * baseline,
                    event["seq"],
                ),
            )
            return True
        return bool(already)

    def observe_daily_risk(self):
        session = self.session()
        if not session:
            return False
        positions = self.client.positions()
        account = self.account_snapshot(session)
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            baseline, realized, unrealized = self._daily_risk(conn, session, account, positions)
            return self._latch_daily(conn, session, baseline, realized, unrealized)

    def _decision(
        self,
        conn,
        *,
        candidate_id,
        action,
        session_date,
        equity,
        reason,
        approved,
        payload=None,
        method="NONE",
        path="",
        qty=0,
        budget=Decimal(0),
        planned=Decimal(0),
        before=Decimal(0),
        after=Decimal(0),
        context=None,
    ):
        decision_id = uuid4()
        event = system_event(
            self.repo,
            conn,
            "RISK_DECISION",
            {
                "risk_decision_id": str(decision_id),
                "action": action,
                "approved": approved,
                "reason": reason,
                "equity": equity,
                "budget": budget,
                "planned_risk": planned,
                "computed_qty": qty,
                "request": payload or {},
                "context": context or {},
            },
            candidate_id,
        )
        return conn.execute(
            """INSERT INTO lab.risk_decisions(risk_decision_id,candidate_id,
            strategy_version,equity,risk_pct,risk_dollars,planned_risk,theme_exposure_before,
            theme_exposure_after,computed_qty,decision,reason,event_id,action,session_date,
            expires_at,http_method,path,payload_json,context_json)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            clock_timestamp()+(%s * interval '1 second'),%s,%s,%s,%s) RETURNING *""",
            (
                decision_id,
                candidate_id,
                STRATEGY_VERSION,
                equity,
                RISK_PCT if action == "ENTRY" else 0,
                budget,
                planned,
                before,
                after,
                qty,
                "APPROVED" if approved else "REJECTED",
                reason,
                event["event_id"],
                action,
                session_date,
                self.policy.authorization_ttl_seconds,
                method,
                path,
                Jsonb(payload or {}),
                Jsonb(json_safe(context or {})),
            ),
        ).fetchone()

    def authorize_entry(self, candidate_id):
        # Fetch current broker evidence before the short atomic reservation transaction.
        record = self.repo.get_candidate(candidate_id)
        if not record or record["state"] != "TRIGGER_CONFIRMED":
            raise ValueError("A confirmed candidate is required")
        candidate = Candidate.model_validate(record["payload_json"])
        session = self.session()
        failure = None
        account = {}
        positions = []
        quote = None
        if session is None or not session.contains(self.now()):
            failure = "MARKET_SESSION_CLOSED"
        elif self.now() >= session.flatten_time:
            failure = "ENTRY_WINDOW_CLOSED"
        elif candidate.target - candidate.max_entry_price < 2 * (
            candidate.max_entry_price - candidate.stop
        ):
            failure = "MIN_REWARD_RISK"
        elif not self.ready():
            failure = "STARTUP_RECONCILIATION_REQUIRED"
        else:
            try:
                quotes = self.client.quotes([candidate.ticker])
                quote = next((q for q in quotes if q.ticker == candidate.ticker), None)
                positions = self.client.positions()
                account = self.account_snapshot(session)  # Account is the last network read.
            except Exception:
                failure = "BROKER_RISK_EVIDENCE_UNAVAILABLE"
        try:
            equity = decimal(account.get("equity")) if account else Decimal(0)
        except MarketDataError:
            equity, failure = Decimal(0), "INVALID_ACCOUNT_EVIDENCE"
        observed = self.now()
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            current = self.repo.get_candidate(candidate_id, connection=conn)
            if current["state"] != "TRIGGER_CONFIRMED":
                raise ValueError("Candidate already attempted risk check")
            self.repo.transition(conn, candidate_id, "TRIGGER_CONFIRMED", "RISK_CHECK")
            now = self.now()
            day = now.astimezone(NY).date()
            managed_attempt = conn.execute(
                """SELECT 1 FROM lab.managed_setups s
                JOIN lab.managed_states t USING(setup_id)
                WHERE s.market='US_STOCKS' AND upper(s.symbol)=upper(%s)
                AND ((t.body->>'admitted_at')::timestamptz
                     AT TIME ZONE 'America/New_York')::date=%s LIMIT 1""",
                (candidate.ticker, day),
            ).fetchone()
            if managed_attempt:
                failure = failure or "TICKER_ALREADY_ATTEMPTED"
            existing = conn.execute("SELECT * FROM lab.account_risk_reservations").fetchall()
            combined = sum((r["budget"] for r in existing), Decimal(0))
            classification = conn.execute(
                "SELECT * FROM lab.current_classifications WHERE ticker=%s", (candidate.ticker,)
            ).fetchone()
            sized = None
            jev_failure = conn.execute(
                "SELECT lab.jev_us_entry_failure(%s) AS reason", (candidate_id,)
            ).fetchone()["reason"]
            failure = failure or jev_failure
            if not failure:
                try:
                    if not session.contains(now) or now >= datetime.fromisoformat(
                        current["expires_at"]
                    ):
                        failure = "CANDIDATE_EXPIRED"
                    elif now >= session.flatten_time:
                        failure = "ENTRY_WINDOW_CLOSED"
                    elif (now - observed).total_seconds() > self.policy.authorization_ttl_seconds:
                        failure = "STALE_ACCOUNT_EVIDENCE"
                    elif (
                        account.get("status") != "ACTIVE"
                        or account.get("currency") != "USD"
                        or any(
                            account.get(k) is not False
                            for k in (
                                "trading_blocked",
                                "account_blocked",
                                "trade_suspended_by_user",
                            )
                        )
                    ):
                        failure = "BROKER_ACCOUNT_BLOCKED"
                    elif quote is None or not 0 <= (now - quote.at).total_seconds() <= 5:
                        failure = "STALE_QUOTE"
                    elif quote.spread_bps is None:
                        failure = "INVALID_QUOTE"
                    elif quote.spread_bps > self.policy.max_spread_bps:
                        failure = "MAX_SPREAD"
                    elif quote.ask > candidate.max_entry_price:
                        failure = "PRICE_BEYOND_MAX_ENTRY"
                    elif conn.execute("SELECT 1 FROM lab.execution_halts LIMIT 1").fetchone():
                        failure = "RISK_HALT"
                    elif conn.execute("SELECT 1 FROM lab.pending_risk_exits LIMIT 1").fetchone():
                        failure = "RISK_EXIT_PENDING"
                    elif (
                        current["record_purpose"] == "ENGINEERING_TEST"
                        and not conn.execute(
                            "SELECT 1 FROM lab.engineering_acceptance_runs WHERE candidate_id=%s",
                            (candidate_id,),
                        ).fetchone()
                        and not conn.execute(
                            "SELECT 1 FROM lab.jev_paper_admissions "
                            "WHERE candidate_id=%s AND outcome='VALIDATED'",
                            (candidate_id,),
                        ).fetchone()
                    ):
                        failure = "ENGINEERING_TEST_NOT_ENROLLED"
                    else:
                        baseline, realized, unrealized = self._daily_risk(
                            conn, session, account, positions
                        )
                        if self._latch_daily(conn, session, baseline, realized, unrealized):
                            failure = "DAILY_RISK_HALT"
                except MarketDataError as exc:
                    failure = str(exc)
            budget = RISK_PCT * equity if equity > 0 else Decimal(0)
            account_failure = buying_power = binding = None
            if not failure:
                # The one SQL account-risk check (lab.account_risk_failure) under this
                # engine's archived policy row; the reservation triggers ask the same question
                # in this transaction, so no approved decision can be refused by them.
                account_failure = account_risk_failure(
                    conn,
                    FROZEN_V1_POLICY_ID,
                    "US_STOCKS",
                    classification["sector"] if classification else None,
                    classification["theme"] if classification else None,
                    equity,
                    budget,
                )
                failure, binding = account_failure, binding_constraint(account_failure)
            if not failure:
                # Broker cash already reflects fills; reserve only unfilled entry notional.
                pending = conn.execute("""SELECT coalesce(sum(r.max_entry*greatest(0,r.qty-
                    coalesce(f.bought,0))),0) AS n FROM lab.active_reservations r
                    LEFT JOIN lab.orders o USING(candidate_id)
                    LEFT JOIN LATERAL(SELECT sum(qty) AS bought FROM lab.fills
                        WHERE order_id=o.order_id AND side='buy') f ON true
                    LEFT JOIN lab.broker_order_states b
                      ON b.order_id=o.order_id AND b.role='ENTRY'
                    WHERE b.status IS NULL OR b.status NOT IN
                      ('filled','canceled','expired','rejected')""").fetchone()["n"]
                managed_pending = conn.execute("""SELECT coalesce(sum(qty*max_entry),0) AS n
                    FROM lab.managed_active_reservations""").fetchone()["n"]
                available = max(Decimal(0), signed(account["cash"]) - pending - managed_pending)
                sized = size_entry(
                    equity, candidate.max_entry_price, candidate.stop, available=available
                )
                risk_qty = int(
                    (budget / (candidate.max_entry_price - candidate.stop)).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                binding = "RISK" if sized.qty == risk_qty else "CASH"
                if sized.qty < 1:
                    failure = "ZERO_SHARE_SIZE"
                else:
                    # After frozen sizing: reject, never resize, against the broker's own figure.
                    buying_power = buying_power_check(
                        account, market="US_STOCKS", notional=sized.notional, equity=equity
                    )
                    if buying_power.failure:
                        failure, binding = buying_power.failure, "BUYING_POWER"
            payload = bracket(candidate, sized.qty, candidate_id) if not failure else {}
            theme_before = sum(
                (
                    r["budget"]
                    for r in existing
                    if classification and r["theme"] == classification["theme"]
                ),
                Decimal(0),
            )
            decision = self._decision(
                conn,
                candidate_id=candidate_id,
                action="ENTRY",
                session_date=day,
                equity=equity,
                reason=failure or "RISK_APPROVED",
                approved=not failure,
                payload=payload,
                method="NONE" if failure else "POST",
                path="/v2/orders" if not failure else "",
                qty=sized.qty if sized else 0,
                budget=budget,
                planned=sized.planned_risk if sized else 0,
                before=theme_before,
                after=theme_before + (budget if not failure else 0),
                context={
                    "combined_risk_before": combined,
                    "combined_risk_after": combined + (budget if not failure else 0),
                    "account_observed_at": observed,
                    "market_quote_timestamp": quote.at if quote else None,
                    "account": account,
                    "classification": classification,
                    "policy": self.policy.__dict__,
                    "risk_policy_id": FROZEN_V1_POLICY_ID,
                    "venue": VENUE,
                    "account_risk_policy": self.account_policy.evidence(),
                    "account_risk_failure": account_failure,
                    "binding_constraint": binding,
                    "buying_power": buying_power.evidence if buying_power else None,
                    "session_opens": session.opens if session else None,
                    "session_closes": session.closes if session else None,
                    "entry_deadline": min(
                        session.flatten_time, datetime.fromisoformat(current["expires_at"])
                    )
                    if session
                    else None,
                },
            )
            if failure:
                self.repo.transition(
                    conn, candidate_id, "RISK_CHECK", "RISK_REJECTED", {"reason": failure}
                )
                return decision
            event = system_event(
                self.repo,
                conn,
                "RISK_RESERVED",
                {
                    "risk_decision_id": str(decision["risk_decision_id"]),
                    "budget": budget,
                    "qty": sized.qty,
                    "planned_risk": sized.planned_risk,
                },
                candidate_id,
            )
            conn.execute(
                "INSERT INTO lab.risk_reservations VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    candidate_id,
                    decision["risk_decision_id"],
                    budget,
                    sized.planned_risk,
                    sized.qty,
                    candidate.max_entry_price,
                    classification["sector"],
                    classification["theme"],
                    event["seq"],
                ),
            )
            ExecutionService(self.repo, self.client)._intent(conn, candidate_id, "ENTRY", payload)
            self.repo.transition(
                conn,
                candidate_id,
                "RISK_CHECK",
                "ORDER_SUBMITTED",
                {
                    "reason": "RISK_AUTHORIZED_DISPATCH_PENDING",
                    "risk_decision_id": str(decision["risk_decision_id"]),
                },
            )
            return decision

    def release(self, candidate_id, reason, *, connection=None):
        from contextlib import nullcontext

        with nullcontext(connection) if connection else self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            if not conn.execute(
                "SELECT 1 FROM lab.active_reservations WHERE candidate_id=%s", (candidate_id,)
            ).fetchone():
                return
            if conn.execute(
                "SELECT 1 FROM lab.strategy_positions WHERE candidate_id=%s", (candidate_id,)
            ).fetchone():
                return
            if conn.execute(
                """SELECT 1 FROM lab.broker_order_states b
                JOIN lab.orders o USING(order_id) WHERE o.candidate_id=%s AND b.role='ENTRY'
                AND b.status NOT IN ('filled','canceled','expired','rejected')""",
                (candidate_id,),
            ).fetchone():
                return
            event = system_event(
                self.repo, conn, "RISK_RESERVATION_RELEASED", {"reason": reason}, candidate_id
            )
            conn.execute(
                "INSERT INTO lab.reservation_releases VALUES(%s,%s,%s)",
                (candidate_id, reason, event["seq"]),
            )
