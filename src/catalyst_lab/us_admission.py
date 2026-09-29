"""Read-only US admission evidence; no quantities or permissions come from research."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from catalyst_lab.broker_ledger import TERMINAL
from catalyst_lab.domain import Evidence, Policy
from catalyst_lab.market import NY, decimal, timestamp
from catalyst_lab.reconciliation import Reconciler, broker_snapshot, compare
from catalyst_lab.risk import signed
from catalyst_lab.validation import EvidenceUnavailable

US_PAPER_ADMISSION_TEST_V1 = "US_PAPER_ADMISSION_TEST_V1"


@dataclass(frozen=True)
class AdmissionProfile:
    policy_id: str
    completed_sessions: int
    minimum_dollar_volume: Decimal
    allowed_feeds: frozenset[str]

    def __post_init__(self):
        if (
            not self.policy_id
            or not 1 <= self.completed_sessions <= 100
            or not self.minimum_dollar_volume.is_finite()
            or self.minimum_dollar_volume <= 0
            or not self.allowed_feeds
            or not self.allowed_feeds <= {"iex", "sip"}
        ):
            raise ValueError("Invalid explicit US admission profile")

    @property
    def validation_policy(self):
        return Policy(Decimal(5), Decimal(10), self.minimum_dollar_volume, Decimal(5))


def admission_profile(name):
    if name != US_PAPER_ADMISSION_TEST_V1:
        raise ValueError("Unknown US admission policy")
    # Builder-selected testing values. IEX is explicitly permitted, never labeled consolidated.
    return AdmissionProfile(name, 20, Decimal("20000000"), frozenset({"iex", "sip"}))


def unavailable(rule, reason):
    raise EvidenceUnavailable(rule, reason)


class USAdmissionEvidence:
    def __init__(self, repository, client, profile, *, ready, feed_healthy, clock):
        self.repo, self.client, self.profile = repository, client, profile
        self.ready, self.feed_healthy, self.now = ready, feed_healthy, clock

    def _gates(self):
        if not self.ready():
            unavailable("STARTUP_RECONCILIATION_REQUIRED", "Current broker reconciliation required")
        if not self.feed_healthy() or self.client.feed not in self.profile.allowed_feeds:
            unavailable("DATA_FEED_FAILURE", "Current authenticated market feed required")

    def __call__(self, candidate, received_at):
        self._gates()
        day = self.now().astimezone(NY).date()
        with self.repo.connect() as conn:
            classification = conn.execute(
                "SELECT * FROM lab.current_classifications WHERE ticker=%s", (candidate.ticker,)
            ).fetchone()
            baseline = conn.execute(
                "SELECT * FROM lab.risk_sessions WHERE session_date=%s", (day,)
            ).fetchone()
        if not classification:
            unavailable("CORRELATION_UNKNOWN", "Server-owned sector and theme are required")
        if not baseline:
            unavailable("DAY_START_EQUITY_REQUIRED", "Startup must persist previous-close equity")
        start = day - timedelta(days=self.profile.completed_sessions * 3 + 14)
        sessions = self.client.calendar(start, day)
        if len({s.session_date for s in sessions}) != len(sessions):
            unavailable("DATA_FEED_FAILURE", "Duplicate calendar sessions")
        current = next((s for s in sessions if s.session_date == day), None)
        if current is None or self.now() >= current.flatten_time:
            unavailable("ENTRY_WINDOW_CLOSED", "No exchange-calendar entry window remains")
        previous = sorted(
            (s for s in sessions if s.session_date < day and s.closes < self.now()),
            key=lambda s: s.session_date,
        )[-self.profile.completed_sessions :]
        if len(previous) != self.profile.completed_sessions:
            unavailable("LIQUIDITY_EVIDENCE_INCOMPLETE", "Insufficient completed calendar sessions")
        asset = self.client.asset(candidate.ticker)
        if not asset or not (
            asset.get("symbol") == candidate.ticker
            and asset.get("class") == "us_equity"
            and asset.get("status") == "active"
            and asset.get("tradable") is True
        ):
            unavailable("ASSET_NOT_TRADABLE", "Require an active tradable US equity")
        # Daily timestamps represent the provider's New York trading date. No incomplete
        # current bar, forward fill, split adjustment or close-price proxy is permitted.
        first = datetime.combine(previous[0].session_date, datetime.min.time(), NY)
        end = datetime.combine(day, datetime.min.time(), NY).astimezone(UTC) - timedelta(
            microseconds=1
        )
        bars = self.client.daily_bars(candidate.ticker, first, end)
        expected = {s.session_date for s in previous}
        by_day = {}
        for bar in bars:
            bar_day = timestamp(bar["t"]).astimezone(NY).date()
            if bar_day not in expected or bar_day in by_day:
                unavailable("LIQUIDITY_EVIDENCE_INCOMPLETE", "Unexpected or duplicate daily bar")
            volume, vwap = decimal(bar["v"], positive=False), decimal(bar["vw"])
            by_day[bar_day] = {"volume": volume, "vwap": vwap, "dollar_volume": volume * vwap}
        if set(by_day) != expected:
            unavailable("LIQUIDITY_EVIDENCE_INCOMPLETE", "Missing completed daily bars")
        average = sum((b["dollar_volume"] for b in by_day.values()), Decimal(0)) / len(by_day)
        # Capture the earliest account observation; a slow multi-request snapshot expires
        # as a whole. Quote is fetched last. Repository validates again after obtaining its lock.
        observed_at = self.now()
        with self.repo.connect() as conn:
            watermark = Reconciler._watermark(conn)
        activities = self.client.capital_activities(day)
        if any(a.get("activity_type") not in {"CSD", "CSW", "ACATC"} for a in activities):
            unavailable("CAPITAL_ACTIVITY_RECONCILIATION_REQUIRED", "Unsupported capital movement")
        cash_flow = sum((signed(a["net_amount"]) for a in activities), Decimal(0))
        positions = self.client.positions()
        broker_positions, broker_orders = broker_snapshot(positions, self.client.open_orders())
        account = self.client.account()
        if not (
            account.get("status") == "ACTIVE"
            and account.get("currency") == "USD"
            and all(
                account.get(key) is False
                for key in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
            )
        ):
            unavailable("INVALID_ACCOUNT_EVIDENCE", "Require an active unblocked USD paper account")
        equity = decimal(account["equity"])
        unrealized = sum((signed(p["unrealized_pl"]) for p in positions), Decimal(0))
        total = equity - baseline["day_start_equity"] - cash_flow
        quotes = self.client.quotes([candidate.ticker])
        if len(quotes) != 1 or quotes[0].ticker != candidate.ticker:
            unavailable("DATA_FEED_FAILURE", "Exactly one matching current quote is required")
        quote = quotes[0]
        self._gates()
        if self.now().astimezone(NY).date() != day:
            unavailable("MARKET_SESSION_CLOSED", "Session changed while gathering evidence")
        with self.repo.connect() as conn:
            if Reconciler._watermark(conn) != watermark:
                unavailable("STARTUP_RECONCILIATION_REQUIRED", "Broker update during admission")
            local_positions = {
                r["ticker"]: r["qty"]
                for r in conn.execute(
                    "SELECT ticker,sum(qty) AS qty FROM lab.strategy_positions GROUP BY ticker"
                ).fetchall()
            }
            local_orders = {
                r["broker_order_id"]: {
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "qty": r["qty"],
                    "filled_qty": r["filled_qty"],
                    "type": r["order_type"],
                    "limit_price": r["limit_price"],
                    "stop_price": r["stop_price"],
                    "status": r["status"],
                }
                for r in conn.execute("SELECT * FROM lab.broker_order_states").fetchall()
                if r["status"] not in TERMINAL
            }
            if compare(local_positions, local_orders, broker_positions, broker_orders):
                unavailable(
                    "STARTUP_RECONCILIATION_REQUIRED", "Broker exposure differs from ledger"
                )
            reservations = conn.execute("SELECT * FROM lab.active_reservations").fetchall()
            halted = bool(
                conn.execute(
                    "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s", (day,)
                ).fetchone()
            )
        return Evidence(
            source="ALPACA_PAPER",
            observed_at=observed_at,
            session_date=day,
            official_open=current.opens,
            official_close=current.closes,
            calendar_provider="ALPACA",
            ticker=candidate.ticker,
            asset_class="us_equity",
            tradable=True,
            data_provider="ALPACA",
            data_feed=self.client.feed,
            quote_timestamp=quote.at,
            bid=quote.bid,
            ask=quote.ask,
            average_daily_dollar_volume=average,
            feed_healthy=True,
            reconciled_session=day,
            unexplained_positions=False,
            sector=classification["sector"],
            theme=classification["theme"],
            open_sectors=frozenset(r["sector"] for r in reservations),
            open_themes=frozenset(r["theme"] for r in reservations),
            equity=equity,
            start_of_day_equity=baseline["day_start_equity"],
            realized_pnl_today=total - unrealized,
            open_unrealized_pnl=unrealized,
            open_planned_risk=sum((r["budget"] for r in reservations), Decimal(0)),
            daily_halted=halted,
            admission_metadata={
                "policy_id": self.profile.policy_id,
                "feed_coverage": "IEX_ONLY" if self.client.feed == "iex" else "CONSOLIDATED",
                "liquidity_method": "MEAN_RAW_DAILY_VOLUME_TIMES_PROVIDER_VWAP",
                "completed_sessions": len(by_day),
                "daily_bars": [{"session_date": d, **b} for d, b in sorted(by_day.items())],
                "classification_event_seq": classification["event_seq"],
                "baseline_event_seq": baseline["event_seq"],
                "asset_class": "us_equity",
                "asset_status": "active",
            },
        )
