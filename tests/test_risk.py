"""Risk authorizations use real PostgreSQL and fake paper evidence/HTTP only."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import psycopg
import pytest

from catalyst_lab.alpaca import PAPER_ENDPOINT, AlpacaCredentials
from catalyst_lab.authorization import AuthorizationGate, RiskRepository
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.market import Observation, Session
from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
from catalyst_lab.risk import RiskEngine, RiskPolicy
from catalyst_lab.risk_math import size_entry
from tests.conftest import NOW
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster


class EvidenceClient:
    def __init__(self):
        self.calls = []
        self.equity = D("10000")
        self.cash = D("10000")
        self.buying_power = None  # None follows cash (multiplier 1).
        self.multiplier = "1"
        self.positions_list = []
        self.at = NOW

    def account(self):
        self.calls.append("GET account")
        power = str(self.cash if self.buying_power is None else self.buying_power)
        return {
            "status": "ACTIVE",
            "currency": "USD",
            "equity": str(self.equity),
            "last_equity": "10000",
            "cash": str(self.cash),
            "multiplier": self.multiplier,
            "buying_power": power,
            "regt_buying_power": power,
            "non_marginable_buying_power": str(self.cash),
            "initial_margin": "0",
            "maintenance_margin": "0",
            "shorting_enabled": False,
            "crypto_status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        }

    def capital_activities(self, session_date):
        return []

    def positions(self):
        return copy.deepcopy(self.positions_list)

    def open_orders(self):
        return []

    def calendar(self, start, end):
        return [
            Session.from_calendar({"date": start.isoformat(), "open": "09:30", "close": "16:00"})
        ]

    def quotes(self, tickers):
        return [
            Observation.from_wire(
                {"T": "q", "S": s, "t": self.at.isoformat(), "bp": 99.99, "ap": 100.01}, "iex"
            )
            for s in tickers
        ]


@pytest.fixture
def risk_setup(er):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    client = EvidenceClient()
    engine = RiskEngine(
        repo,
        client,
        RiskPolicy(5, "BROKER_PREVIOUS_CLOSE"),
        ready=lambda: True,
        clock=lambda: client.at,
    )
    from catalyst_lab.reconciliation import Reconciler

    assert Reconciler(
        repo, client, clock=lambda: client.at, baseline_recorder=engine.capture_reconciled_baseline
    ).run_once()["clean"]
    client.calls.clear()
    return repo, client, engine


@pytest.fixture
def candidate_factory(er, raw, evidence, policy, risk_setup):
    from dataclasses import replace

    def make(*, sector=None, theme=None, at=NOW, **changes):
        suffix = uuid4().hex[:9].upper()
        body = (
            raw
            | {
                "ticker": "T" + suffix,
                "signal_id": "fixture-" + suffix,
                "stop": "90",
                "target": "121",
            }
            | changes
        )
        delta = at - NOW
        ev = replace(
            evidence,
            ticker=body["ticker"],
            observed_at=at,
            session_date=at.date(),
            reconciled_session=at.date(),
            official_open=evidence.official_open + delta,
            official_close=evidence.official_close + delta,
            quote_timestamp=at - timedelta(seconds=1),
        )
        row = er.submit(body, at, lambda c, n: ev, policy)
        assert row["state"] == "VALIDATED", row
        cid = row["candidate_id"]
        with er.connect() as conn:
            er.transition(conn, cid, "VALIDATED", "WATCHING", {"reason": "LAB_FIXTURE"})
            er.transition(conn, cid, "WATCHING", "TRIGGER_CONFIRMED", {"reason": "LAB_FIXTURE"})
        risk_setup[2].classify(body["ticker"], sector or suffix, theme or suffix, "LAB_FIXTURE")
        return cid

    return make


@pytest.mark.parametrize(
    "equity,m,s,qty",
    [
        ("10000", "100", "90", 10),
        ("10000", "100.15", "99", 86),
        ("50", "100", "99", 0),
        ("10000", "20000", "100", 0),
        ("10000", "100", "99.99", 100),
    ],
)
def test_frozen_sizing_and_no_leverage(equity, m, s, qty):
    result = size_entry(D(equity), D(m), D(s))
    assert result.qty == qty and result.notional <= D(equity)
    assert result.planned_risk <= D(equity) * D(".01")


def test_risk_uses_broker_equity_at_check_and_persists_atomic_reservation(
    er, risk_setup, candidate_factory
):
    repo, broker, engine = risk_setup
    cid = candidate_factory()
    broker.equity = D("20000")
    broker.cash = D("20000")
    decision = engine.authorize_entry(cid)
    assert decision["decision"] == "APPROVED"
    assert decision["computed_qty"] == 19 and decision["equity"] == D("20000")
    assert broker.calls == ["GET account"]
    with er.connect() as conn:
        assert conn.execute("SELECT budget FROM lab.active_reservations").fetchone()["budget"] == D(
            "200"
        )
    assert er.get_candidate(cid)["state"] == "ORDER_SUBMITTED"


def test_concurrent_risk_checks_cannot_spend_over_two_percent(er, risk_setup, candidate_factory):
    repo, broker, engine = risk_setup
    candidates = [candidate_factory() for _ in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        decisions = list(pool.map(engine.authorize_entry, candidates))
    assert sum(d["decision"] == "APPROVED" for d in decisions) == 2
    assert {d["reason"] for d in decisions if d["decision"] == "REJECTED"} == {
        "MAX_OPEN_PLANNED_RISK"
    }
    with er.connect() as conn:
        assert conn.execute("SELECT sum(budget) AS total FROM lab.active_reservations").fetchone()[
            "total"
        ] == D("200")


def test_sector_theme_and_working_orders_share_budget(risk_setup, candidate_factory):
    engine = risk_setup[2]
    a, b, c = [candidate_factory(sector=s) for s in ("TECH", "TECH", "HEALTH")]
    assert engine.authorize_entry(a)["decision"] == "APPROVED"
    assert engine.authorize_entry(b)["reason"] == "CORRELATION_LIMIT"
    rejected = risk_setup[0].get_candidate(b)
    assert rejected["status"] == "rejected" and rejected["rejection_reason"] == "CORRELATION_LIMIT"
    assert engine.authorize_entry(c)["decision"] == "APPROVED"


@pytest.mark.parametrize("limits", [(2, 1), (1, 2), (2, 2)])
def test_configured_correlation_must_match_the_v1_policy_row(risk_setup, limits):
    """Migration 016: the database row is the one source; a differing RISK_MAX_PER_SECTOR/THEME
    fails V1 startup instead of approving what the reservation trigger would then refuse."""
    repo, broker, _ = risk_setup
    with pytest.raises(ValueError, match="V1_RISK_POLICY_MISMATCH"):
        RiskEngine(
            repo,
            broker,
            RiskPolicy(5, "BROKER_PREVIOUS_CLOSE", *limits),
            ready=lambda: True,
            clock=lambda: NOW,
        )


def test_spy_golden_case_sizes_thirteen_shares_under_the_v1_policy(er, risk_setup, raw, policy):
    """The Sep 18 SPY levels (target raised to pass the 2R-at-M admission rule): risk alone
    allows 40 shares, the frozen cash cap binds at floor(10,000 / 761.50) = 13."""
    repo, broker, engine = risk_setup
    cid = spy_candidate(er, engine, raw, policy)
    broker.buying_power = D("20000")  # A 2x margin account never lets V1 size beyond cash.
    decision = engine.authorize_entry(cid)
    assert decision["decision"] == "APPROVED" and decision["computed_qty"] == 13
    assert decision["planned_risk"] == D("32.50") and decision["risk_dollars"] == D("100")
    context = decision["context_json"]
    assert context["risk_policy_id"] == "CATALYST_RETEST_V1" and context["venue"] == "ALPACA_PAPER"
    assert context["binding_constraint"] == "CASH"
    assert context["buying_power"]["allowed"] == "10000"  # min(buying_power, 1 x equity)
    assert context["account"]["multiplier"] == "1"


def test_spy_buying_power_shortfall_rejects_and_reserves_nothing(er, risk_setup, raw, policy):
    repo, broker, engine = risk_setup
    cid = spy_candidate(er, engine, raw, policy)
    broker.buying_power = D("9899.49")  # One cent short of 13 x 761.50: reject, never resize.
    decision = engine.authorize_entry(cid)
    assert decision["decision"] == "REJECTED"
    assert decision["reason"] == "INSUFFICIENT_BUYING_POWER"
    assert decision["context_json"]["binding_constraint"] == "BUYING_POWER"
    assert er.get_candidate(cid)["state"] == "RISK_REJECTED"
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"multiplier": "3"}, "BUYING_POWER_EVIDENCE_UNAVAILABLE"),
        ({"multiplier": None}, "BUYING_POWER_EVIDENCE_UNAVAILABLE"),
        ({"buying_power": None}, "BUYING_POWER_EVIDENCE_UNAVAILABLE"),
        ({"buying_power": "NaN"}, "BUYING_POWER_EVIDENCE_UNAVAILABLE"),
    ],
)
def test_missing_margin_evidence_fails_closed(er, risk_setup, raw, policy, change, reason):
    repo, broker, engine = risk_setup
    cid = spy_candidate(er, engine, raw, policy)
    account = broker.account
    broker.account = lambda: account() | change
    decision = engine.authorize_entry(cid)
    assert decision["decision"] == "REJECTED" and decision["reason"] == reason
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()


def spy_candidate(er, engine, raw, policy):
    from dataclasses import replace

    body = raw | {
        "ticker": "SPY",
        "signal_id": "fixture-spy-golden-" + uuid4().hex[:8],
        "entry_trigger": "761.00",
        "max_entry_price": "761.50",
        "stop": "759.00",
        "target": "766.50",
    }
    evidence = golden_evidence(body)
    row = er.submit(body, NOW, lambda c, n: replace(evidence, ticker="SPY"), policy)
    assert row["state"] == "VALIDATED", row
    cid = row["candidate_id"]
    with er.connect() as conn:
        er.transition(conn, cid, "VALIDATED", "WATCHING", {"reason": "LAB_FIXTURE"})
        er.transition(conn, cid, "WATCHING", "TRIGGER_CONFIRMED", {"reason": "LAB_FIXTURE"})
    engine.classify("SPY", "BROAD_MARKET", "US_LARGE_CAP_INDEX", "LAB_FIXTURE")
    return cid


def golden_evidence(body):
    from catalyst_lab.domain import Evidence

    return Evidence(
        source="LAB_FIXTURE",
        observed_at=NOW,
        session_date=NOW.date(),
        official_open=NOW.replace(hour=9, minute=30),
        official_close=NOW.replace(hour=16),
        calendar_provider="LAB_FIXTURE",
        ticker=body["ticker"],
        asset_class="us_equity",
        tradable=True,
        data_provider="LAB_FIXTURE",
        data_feed="IEX_FIXTURE",
        quote_timestamp=NOW - timedelta(seconds=1),
        bid=D("760.99"),
        ask=D("761.01"),
        average_daily_dollar_volume=D("100000000"),
        feed_healthy=True,
        reconciled_session=NOW.date(),
        unexplained_positions=False,
        sector="BROAD_MARKET",
        theme="US_LARGE_CAP_INDEX",
        open_sectors=frozenset(),
        open_themes=frozenset(),
        equity=D("10000"),
        start_of_day_equity=D("10000"),
        realized_pnl_today=D("0"),
        open_unrealized_pnl=D("0"),
        open_planned_risk=D("0"),
        daily_halted=False,
    )


def test_app_role_cannot_create_risk_authorization_or_submit_state(er, candidate_factory):
    cid = candidate_factory()
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("INSERT INTO lab.risk_decisions(risk_decision_id) VALUES(%s)", (uuid4(),))
    with er.connect() as conn, pytest.raises(psycopg.errors.RaiseException, match="atomic risk"):
        er.transition(conn, cid, "TRIGGER_CONFIRMED", "RISK_CHECK")
        er.transition(conn, cid, "RISK_CHECK", "ORDER_SUBMITTED")


def test_no_decision_blocks_actual_submission_transport(risk_setup):
    calls = []
    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        AuthorizationGate(risk_setup[0], clock=lambda: NOW),
        transport=httpx.MockTransport(lambda r: calls.append(r)),
    )
    try:
        with pytest.raises(SubmissionDisabled):
            client.submit_bracket({})
        with pytest.raises(SubmissionDisabled):
            client.submit_bracket({}, risk_decision_id=uuid4())
        with pytest.raises(SubmissionDisabled):
            client._client.post(PAPER_ENDPOINT + "/v2/orders", json={})
        assert not calls
    finally:
        client.close()


def test_capability_binds_payload_and_is_one_use(risk_setup, candidate_factory):
    decision = risk_setup[2].authorize_entry(candidate_factory())
    calls = []

    def transport(r):
        calls.append(r)
        return httpx.Response(200, json={"id": "fixture-broker-order"})

    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        AuthorizationGate(risk_setup[0], clock=lambda: NOW),
        transport=httpx.MockTransport(transport),
    )
    try:
        with pytest.raises(SubmissionDisabled):
            client.submit_bracket(
                decision["payload_json"] | {"qty": "999"},
                risk_decision_id=decision["risk_decision_id"],
            )
        client.submit_bracket(
            decision["payload_json"], risk_decision_id=decision["risk_decision_id"]
        )
        with pytest.raises(SubmissionDisabled):
            client.submit_bracket(
                decision["payload_json"], risk_decision_id=decision["risk_decision_id"]
            )
        assert len(calls) == 1
    finally:
        client.close()


@pytest.fixture
def execution_setup(risk_setup):
    from catalyst_lab.risk_dispatch import RiskDispatcher
    from catalyst_lab.risk_safety import RiskSafety
    from tests.fake_paper_broker import FakePaperBroker

    repo, _, _ = risk_setup
    fake = FakePaperBroker()
    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        AuthorizationGate(repo, clock=lambda: fake.now),
        transport=httpx.MockTransport(fake.handle),
    )
    engine = RiskEngine(
        repo,
        client,
        RiskPolicy(5, "BROKER_PREVIOUS_CLOSE"),
        ready=lambda: True,
        clock=lambda: fake.now,
    )
    dispatcher = RiskDispatcher(engine)
    safety = RiskSafety(dispatcher)
    try:
        yield fake, engine, dispatcher, safety
    finally:
        client.close()


def test_timeout_reconciles_existing_order_without_another_post(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    cid = candidate_factory()
    fake.timeout_after_accept = True
    decision = dispatcher.enter(cid)
    dispatcher.recover_outstanding()
    dispatcher.dispatch(decision["risk_decision_id"])
    assert len(fake.root_ids) == 1
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1
    assert any(c[1] == "/v2/orders:by_client_order_id" for c in fake.calls)
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.orders").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT outcome FROM lab.current_authorization_results").fetchone()[
                "outcome"
            ]
            == "RECOVERED"
        )


def test_timeout_not_found_keeps_reservation_and_never_blindly_retries(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    fake.timeout_without_accept = True
    decision = dispatcher.enter(candidate_factory())
    dispatcher.recover_outstanding()
    dispatcher.dispatch(decision["risk_decision_id"])
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1
    with er.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.active_reservations").fetchone()["n"] == 1
        )
    assert fake.root_ids == []


def test_stop_rejection_flattens_automatically_with_authorized_actions(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    fake.fill_entry(fake.root_ids[0], stop_rejected=True)
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {}
    assert all(c[2] for c in fake.calls if c[0] in {"POST", "DELETE"})
    assert er.get_candidate(cid)["state"] == "CLOSED"
    with er.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.active_reservations").fetchone()["n"] == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM lab.risk_decisions WHERE action='FLATTEN'"
            ).fetchone()["n"]
            == 1
        )


def test_daily_halt_cancels_flattens_blocks_and_resumes_next_clean_session(
    er, execution_setup, candidate_factory
):

    fake, engine, dispatcher, safety = execution_setup
    first, working, blocked = [candidate_factory() for _ in range(3)]
    dispatcher.enter(first)
    dispatcher.enter(working)
    fake.fill_entry(fake.root_ids[0])
    fake.drain(dispatcher)
    ticker = er.get_candidate(first)["ticker"]
    fake.positions[ticker]["unrealized_pl"] = "-100"
    fake.equity = D("9700")
    assert engine.observe_daily_risk()
    with er.connect() as conn:
        halt = conn.execute("SELECT * FROM lab.daily_risk_halts").fetchone()
        assert halt["realized_pnl"] == D("-200") and halt["unrealized_pnl"] == D("-100")
    safety.daily_halt()
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {}
    assert all(o["status"] in {"filled", "canceled", "rejected"} for o in fake.orders.values())
    with er.connect() as conn:
        assert not conn.execute("SELECT reason,payload_json FROM lab.execution_halts").fetchall()
    assert dispatcher.enter(blocked)["reason"] == "DAILY_RISK_HALT"
    restarted = RiskEngine(
        engine.repo, engine.client, engine.policy, ready=lambda: True, clock=lambda: fake.now
    )
    assert restarted.observe_daily_risk()
    fake.now += timedelta(days=3)
    fake.last_equity = fake.equity
    from catalyst_lab.market import MarketDataError

    with pytest.raises(MarketDataError, match="DAY_START_EQUITY_REQUIRED"):
        restarted.observe_daily_risk()
    with er.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.active_reservations").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM lab.execution_halts").fetchone()["n"] == 0
    from catalyst_lab.reconciliation import Reconciler
    from catalyst_lab.risk_dispatch import RiskDispatcher

    reconciler = Reconciler(
        engine.repo,
        engine.client,
        clock=lambda: fake.now,
        baseline_recorder=restarted.capture_reconciled_baseline,
    )
    assert reconciler.run_once()["clean"]
    assert not restarted.observe_daily_risk()
    restarted.ready = reconciler.ready
    next_candidate = candidate_factory(at=fake.now)
    assert RiskDispatcher(restarted).enter(next_candidate)["decision"] == "APPROVED"


def test_cancel_fill_race_uses_fresh_broker_position_for_flatten(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    fake.fill_on_cancel = True
    safety.request_exit(er.get_candidate(cid)["ticker"], "TIME_EXIT", cid)
    safety.process_exits()
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {}
    assert len([o for o in fake.orders.values() if o["type"] == "market"]) == 1
    assert er.get_candidate(cid)["state"] == "CLOSED"


def test_transaction_failure_rolls_back_decision_reservation_and_state(
    er, risk_setup, candidate_factory, monkeypatch
):
    engine = risk_setup[2]
    cid = candidate_factory()
    original = engine._decision
    before = er.export_events()

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash before reservation commit")

    monkeypatch.setattr(engine, "_decision", crash)
    with pytest.raises(RuntimeError):
        engine.authorize_entry(cid)
    assert er.get_candidate(cid)["state"] == "TRIGGER_CONFIRMED"
    assert er.export_events() == before
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.risk_decisions").fetchone()["n"] == 0


def test_expired_decision_cannot_reach_broker(risk_setup, candidate_factory):
    repo, broker, _ = risk_setup
    engine = RiskEngine(repo, broker, RiskPolicy(), ready=lambda: True, clock=lambda: NOW)
    decision = engine.authorize_entry(candidate_factory())
    with repo.connect() as conn:
        conn.execute("SELECT pg_sleep(5.05)")
    calls = []
    client = RiskAuthorizedPaperClient(
        AlpacaCredentials("PKFIXTURE000000000001", "fixture-secret"),
        AuthorizationGate(repo, clock=lambda: NOW),
        transport=httpx.MockTransport(lambda r: calls.append(r)),
    )
    try:
        with pytest.raises(SubmissionDisabled):
            client.submit_bracket(
                decision["payload_json"], risk_decision_id=decision["risk_decision_id"]
            )
        assert calls == []
    finally:
        client.close()


def test_release_cannot_remove_a_working_order_reservation(er, execution_setup, candidate_factory):
    fake, engine, dispatcher, _ = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    engine.release(cid, "UNTRUSTED_CANCEL_WITHOUT_BROKER_CONFIRMATION")
    with er.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.active_reservations").fetchone()["n"] == 1
        )


def test_partial_fill_with_held_protection_is_closed_automatically(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    fake.fill_entry(fake.root_ids[0], qty=3)
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {} and er.get_candidate(cid)["state"] == "CLOSED"
    closes = [o for o in fake.orders.values() if o["type"] == "market"]
    assert len(closes) == 1 and closes[0]["qty"] == "3"


def test_close_timeout_recovery_never_sends_a_second_market_order(
    er, execution_setup, candidate_factory
):
    fake, engine, dispatcher, safety = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    fake.fill_entry(fake.root_ids[0])
    fake.drain(dispatcher)
    # Trigger timeout after the next POST, not the protective-order DELETE requests.
    fake.timeout_after_accept = True
    safety.request_exit(er.get_candidate(cid)["ticker"], "TIME_EXIT", cid)
    safety.process_exits()
    fake.drain(dispatcher)
    safety.process_exits()
    assert fake.positions == {}
    assert len([o for o in fake.orders.values() if o["type"] == "market"]) == 1


def test_cash_deposit_does_not_hide_trading_loss(risk_setup):
    repo, broker, engine = risk_setup
    broker.equity = D("10700")
    broker.capital_activities = lambda day: [{"activity_type": "CSD", "net_amount": "1000"}]
    assert engine.observe_daily_risk()
    with repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.daily_risk_halts").fetchone()
    assert row["realized_pnl"] + row["unrealized_pnl"] == D("-300")


def test_missing_day_baseline_fails_closed(risk_setup, candidate_factory):
    repo, broker, _ = risk_setup
    engine = RiskEngine(repo, broker, RiskPolicy(), ready=lambda: True, clock=lambda: broker.at)
    broker.at = NOW + timedelta(days=3)
    decision = engine.authorize_entry(candidate_factory(at=broker.at))
    assert decision["reason"] == "DAY_START_EQUITY_REQUIRED"


def test_risk_runtime_picks_confirmed_candidate_and_stays_gated(execution_setup, candidate_factory):
    from catalyst_lab.risk_runtime import RiskRuntime

    fake, engine, _, _ = execution_setup
    candidate_factory()
    runtime = RiskRuntime(engine)
    runtime.tick()
    assert len(fake.root_ids) == 1
    assert runtime.status()["submission_mode"] == "RISK_DECISION_REQUIRED"


@pytest.mark.parametrize(
    "table",
    [
        "risk_classifications",
        "risk_sessions",
        "risk_reservations",
        "reservation_releases",
        "daily_risk_halts",
        "authorization_claims",
        "authorization_results",
        "risk_exit_requests",
        "risk_exit_completions",
        "risk_decisions",
        "engineering_acceptance_runs",
        "engineering_acceptance_results",
    ],
)
def test_risk_ledger_is_append_only_for_both_roles(er, risk_setup, table):
    from psycopg import sql

    for repo in [er, risk_setup[0]]:
        for operation in ["DELETE FROM", "TRUNCATE"]:
            with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL(operation + " lab.{}").format(sql.Identifier(table)))
        with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                sql.SQL("UPDATE lab.{} SET {}={}").format(
                    sql.Identifier(table),
                    sql.Identifier(
                        "risk_decision_id" if table == "risk_decisions" else "event_seq"
                    ),
                    sql.Identifier(
                        "risk_decision_id" if table == "risk_decisions" else "event_seq"
                    ),
                )
            )
    with er.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(sql.SQL("INSERT INTO lab.{} DEFAULT VALUES").format(sql.Identifier(table)))


def test_operator_classification_import_is_atomic_and_not_available_to_app(er, risk_setup):
    from catalyst_lab.risk import import_classifications

    row = {"ticker": "AAPL", "sector": "Technology", "theme": "Devices", "source": "TEST_OPERATOR"}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        import_classifications(er, [row])
    with pytest.raises(ValueError):
        import_classifications(risk_setup[0], [row, row | {"theme": ""}])
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.current_classifications").fetchone()
    assert import_classifications(risk_setup[0], [row]) == 1
    assert import_classifications(risk_setup[0], [row | {"theme": "Hardware"}]) == 1
    with er.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.risk_classifications").fetchone()["n"] == 2
        )
        assert (
            conn.execute("SELECT theme FROM lab.current_classifications").fetchone()["theme"]
            == "Hardware"
        )


def test_app_and_risk_databases_cannot_be_mixed(er, risk_setup):
    from catalyst_lab.repository import Repository

    risk_setup[0].require_same_database(er)
    different = Repository(
        er.database_url.replace(
            next(p for p in er.database_url.split() if p.startswith("dbname=")), "dbname=postgres"
        )
    )
    with pytest.raises(RuntimeError, match="same database"):
        risk_setup[0].require_same_database(different)


def test_early_fill_is_committed_before_network_recovery(
    er, execution_setup, candidate_factory, monkeypatch
):
    fake, engine, dispatcher, _ = execution_setup
    decision = engine.authorize_entry(candidate_factory())
    engine.client.submit_bracket(
        decision["payload_json"], risk_decision_id=decision["risk_decision_id"]
    )
    fake.fill_entry(fake.root_ids[0])
    original = engine.client.order_by_client_id

    def lookup(client_id):
        with er.connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM lab.broker_events").fetchone()["n"] == 1
        return original(client_id)

    monkeypatch.setattr(engine.client, "order_by_client_id", lookup)
    fake.drain(dispatcher)
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.fills").fetchone()["n"] == 1
        assert not conn.execute("SELECT 1 FROM lab.execution_halts").fetchone()


def test_malformed_stream_update_is_still_persisted_before_halt(er, execution_setup):
    fake, _, dispatcher, _ = execution_setup
    assert dispatcher.ingest({"event": "fill", "order": "invalid"}, fake.now) == "QUARANTINED"
    assert not fake.calls
    with er.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.broker_events").fetchone()["n"] == 1
        assert conn.execute("SELECT 1 FROM lab.execution_halts").fetchone()


def test_request_with_duplicate_json_keys_cannot_claim_decision(risk_setup, candidate_factory):
    decision = risk_setup[2].authorize_entry(candidate_factory())
    payload = decision["payload_json"]
    ambiguous = '{"qty":"999",' + json.dumps(payload)[1:]
    request = httpx.Request(
        "POST",
        PAPER_ENDPOINT + "/v2/orders",
        content=ambiguous,
        extensions={"risk_decision_id": str(decision["risk_decision_id"])},
    )
    with pytest.raises(SubmissionDisabled):
        AuthorizationGate(risk_setup[0], clock=lambda: NOW).claim(request)
    with risk_setup[0].connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.authorization_claims").fetchone()


def test_fresh_decision_cannot_dispatch_outside_calendar_session(risk_setup, candidate_factory):
    decision = risk_setup[2].authorize_entry(candidate_factory())
    request = httpx.Request(
        "POST",
        PAPER_ENDPOINT + "/v2/orders",
        json=decision["payload_json"],
        extensions={"risk_decision_id": str(decision["risk_decision_id"])},
    )
    with pytest.raises(SubmissionDisabled, match="REGULAR_SESSION_REQUIRED"):
        AuthorizationGate(risk_setup[0], clock=lambda: NOW.replace(hour=16)).claim(request)


def test_restart_recovers_claim_before_any_resubmission(er, execution_setup, candidate_factory):
    from catalyst_lab.risk_dispatch import RiskDispatcher

    fake, engine, _, _ = execution_setup
    decision = engine.authorize_entry(candidate_factory())
    # Simulate process death after the HTTP transport accepted, before saving the receipt.
    engine.client.submit_bracket(
        decision["payload_json"], risk_decision_id=decision["risk_decision_id"]
    )
    RiskDispatcher(engine).recover_outstanding()
    assert len(fake.root_ids) == 1 and len([c for c in fake.calls if c[0] == "POST"]) == 1
    with er.connect() as conn:
        assert (
            conn.execute("SELECT outcome FROM lab.current_authorization_results").fetchone()[
                "outcome"
            ]
            == "RECOVERED"
        )


def test_reconciliation_records_pending_receipt_without_permanent_halt(
    er, execution_setup, candidate_factory
):
    from catalyst_lab.reconciliation import Reconciler

    fake, engine, dispatcher, _ = execution_setup
    decision = engine.authorize_entry(candidate_factory())
    engine.client.submit_bracket(
        decision["payload_json"], risk_decision_id=decision["risk_decision_id"]
    )
    reconciler = Reconciler(er, engine.client, clock=lambda: fake.now)
    result = reconciler.run_once()
    assert not result["clean"] and not reconciler.ready()
    assert "BROKER_AUTHORIZATION_IN_FLIGHT" in {d["code"] for d in result["discrepancies"]}
    with er.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.execution_halts").fetchone()
        assert conn.execute(
            "SELECT 1 FROM lab.system_events WHERE event_type='BROKER_MISMATCH'"
        ).fetchone()
    dispatcher.recover_outstanding()
    assert reconciler.run_once()["clean"]


def test_missing_stop_is_automatically_flattened(er, execution_setup, candidate_factory):
    fake, engine, dispatcher, safety = execution_setup
    cid = candidate_factory()
    dispatcher.enter(cid)
    root = fake.orders[fake.root_ids[0]]
    missing = root["legs"].pop(0)
    fake.orders.pop(missing["id"])
    fake.fill_entry(root["id"])
    fake.drain(dispatcher)
    safety.process_exits()
    assert not fake.positions and er.get_candidate(cid)["state"] == "CLOSED"


@pytest.mark.parametrize("close", ["16:00", "13:00"])
def test_authorized_flatten_uses_official_calendar_close(
    er, execution_setup, candidate_factory, close
):
    fake, engine, dispatcher, safety = execution_setup
    fake.calendar_close = close
    cid = candidate_factory()
    dispatcher.enter(cid)
    fake.fill_entry(fake.root_ids[0])
    fake.drain(dispatcher)
    session = engine.session()
    fake.now = session.flatten_time - timedelta(seconds=1)
    safety.calendar_exits(session)
    assert fake.positions
    fake.now = session.flatten_time
    safety.calendar_exits(session)
    fake.drain(dispatcher)
    safety.process_exits()
    assert not fake.positions and er.get_candidate(cid)["status"] == "closed_time"
