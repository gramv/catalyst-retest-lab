"""Package baseline: ``RISK_SESSION_BASELINE_V2`` (owner approval 2026-10-03, "yes, go with option
1"). Under ``JEV_MANAGED_RISK_V4`` the New York day starts from the account equity at the first
clean reconciliation at or after New York midnight, not Alpaca's ``last_equity`` (US stock
calendar, not rolled on weekends). A session recorded on the old basis is corrected once, by
appended events; a soft latch recorded on it gets one decision; a hard halt is never lifted.

Fixture evidence only: per-test disposable PostgreSQL databases and the fake paper venue. No
broker, provider, network or owner-ledger contact. The live numbers of 2026-10-03 are used.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import risk_baseline
from catalyst_lab.account_risk import (
    MANAGED_RISK_V3_POLICY_ID,
    MANAGED_RISK_V4_POLICY_ID,
    SOFT_LIMIT_EVENT,
    SOFT_LIMIT_WAIT_EVENT,
    soft_limit_key,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market import NY
from tests.test_crypto_trigger import rows, state
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation, packet
from tests.test_risk_v4 import engine_under, halt_row

V3, V4 = MANAGED_RISK_V3_POLICY_ID, MANAGED_RISK_V4_POLICY_ID
STALE_LAST_EQUITY, EQUITY = "9877.3", "9590.78"  # Live, 2026-10-03 00:00:09 ET.


def next_midnight(venue, seconds=9):
    """The venue clock moved to ``seconds`` after the next New York midnight."""
    day = venue.now.astimezone(NY).date() + timedelta(days=1)
    venue.now = (datetime.combine(day, time(0), tzinfo=NY)
                 + timedelta(seconds=seconds)).astimezone(UTC)
    return day


def session_row(engine, day):
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT * FROM lab.risk_sessions WHERE session_date=%s", (day,)).fetchone()


def on(engine, kind, day):
    return [r for r in rows(engine, kind) if r["body"].get("session_date") == day.isoformat()]


def kinds(engine, *names):
    return {name: rows(engine, name) for name in names}


def old_release_latch(engine, day, last_equity, equity):
    """The DAILY_SOFT_LOSS_LIMIT the 535719a release recorded on the ALPACA_LAST_EQUITY row."""
    base, total = D(last_equity), D(equity) - D(last_equity)
    with engine.store.transaction() as conn:
        return engine.store.event(conn, SOFT_LIMIT_EVENT, {
            "reason": "DAILY_SOFT_LOSS_LIMIT", "action": "NO_NEW_ENTRIES",
            "risk_policy_id": V4, "session_date": day, "total_pnl": total,
            "day_start_equity": base, "soft_loss_pct": D("0.02"),
            "threshold": -D("0.02") * base, "protection": "UNCHANGED", "cohort": COHORT,
        }, key=soft_limit_key(V4, day))


def live_saturday(mx):
    """2026-10-03 as it happened: the old release's session row (``last_equity`` 9877.3, not
    rolled) and its soft latch at −286.52, then the new release's V4 engine starts."""
    _, venue, _ = mx
    day = next_midnight(venue)
    venue.last_equity, venue.equity = STALE_LAST_EQUITY, EQUITY
    old = engine_under(mx, V3)[0]  # Writes the day's row exactly as the old release did.
    assert session_row(old, day)["source"] == "ALPACA_LAST_EQUITY"
    latch = old_release_latch(old, day, STALE_LAST_EQUITY, EQUITY)
    assert D(latch["body"]["total_pnl"]) == D("-286.52")
    assert D(latch["body"]["threshold"]) == D("-197.546")
    venue.now += timedelta(minutes=30)  # The deploy.
    return day, latch, engine_under(mx, V4)


# --- The V2 rule -------------------------------------------------------------------------------


def test_midnight_baseline_is_account_equity_not_stale_last_equity(mx):
    engine, venue, _ = engine_under(mx, V4)  # Running across midnight.
    day = next_midnight(venue)
    venue.last_equity, venue.equity = STALE_LAST_EQUITY, EQUITY
    assert engine.reconcile()["clean"]
    [base] = on(engine, risk_baseline.BASELINE_EVENT, day)
    body = base["body"]
    assert base["idempotency_key"] == f"risk-session-baseline-v2:{day.isoformat()}"
    assert (body["rule"], body["source"], D(body["day_start_equity"])) == (
        "RISK_SESSION_BASELINE_V2", "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2", D(EQUITY))
    assert body["started_mid_session"] is False and body["seconds_after_ny_midnight"] == 9
    assert body["alpaca_last_equity"] == STALE_LAST_EQUITY and body["risk_policy_id"] == V4
    # The row keeps recording the last_equity observation, untouched (migration 006's rule).
    row = session_row(engine, day)
    assert (row["day_start_equity"], row["source"]) == (D(STALE_LAST_EQUITY), "ALPACA_LAST_EQUITY")
    assert body["risk_session_row"]["event_seq"] == row["event_seq"]
    assert on(engine, risk_baseline.CORRECTED_EVENT, day) == []
    # No trade, no loss: no latch, and an entry is approved.
    sid = engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    assert rows(engine, SOFT_LIMIT_EVENT) == [] and halt_row(engine) is None


def test_the_soft_and_hard_limits_measure_from_the_v2_baseline(mx):
    engine, venue, _ = engine_under(mx, V4)
    next_midnight(venue)
    venue.last_equity, venue.equity = STALE_LAST_EQUITY, "10000"
    assert engine.reconcile()["clean"]
    sid = engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    venue.equity = "9800.01"  # Just above −2% of 10000 (the stale 9877.3 would not matter).
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    other = engine.admit(packet((engine, venue, mx[2]), "SOL/USD"))
    venue.equity = "9800"
    venue.now += timedelta(minutes=1)
    assert engine.observe_trigger(other, observation(mx)) is None
    [latch] = rows(engine, SOFT_LIMIT_EVENT)
    assert (D(latch["body"]["day_start_equity"]), D(latch["body"]["threshold"])) == (
        D("10000"), D("-200"))
    with engine.store.transaction() as conn:  # −3% of the V2 basis, not of 9877.3.
        assert engine._account_halt(conn, {"equity": "9700.01"}, [], D(0)) is None
        assert engine._account_halt(conn, {"equity": "9700"}, [], D(0)) == "DAILY_RISK_HALT"
    assert halt_row(engine)["threshold"] == D("-300")


def test_a_mid_session_start_baselines_at_its_first_clean_reconciliation(mx):
    _, venue, _ = mx
    day = next_midnight(venue, seconds=6 * 3600)
    venue.last_equity, venue.equity = STALE_LAST_EQUITY, "9950"
    engine = engine_under(mx, V4)[0]  # A fresh process; no row for the day yet.
    [base] = on(engine, risk_baseline.BASELINE_EVENT, day)
    assert D(base["body"]["day_start_equity"]) == D("9950")
    assert base["body"]["started_mid_session"] is True
    assert base["body"]["seconds_after_ny_midnight"] == 6 * 3600
    assert on(engine, risk_baseline.CORRECTED_EVENT, day) == []


def test_open_positions_carry_their_unrealized_pnl_into_the_baseline(mx):
    from tests.test_position_monitor import opened

    v4 = engine_under(mx, V4)
    engine, venue, _ = v4
    opened(v4, "BTC/USD")
    day = next_midnight(venue)
    assert engine.reconcile()["clean"]
    [base] = on(engine, risk_baseline.BASELINE_EVENT, day)
    assert [p["symbol"] for p in base["body"]["open_positions"]] == ["BTC/USD"]
    assert "open_positions_unrealized_pl" in base["body"]


def test_the_baseline_is_recorded_once_per_day_and_restarts_keep_it(mx):
    engine, venue, _ = engine_under(mx, V4)
    day = next_midnight(venue)
    venue.equity = EQUITY
    assert engine.reconcile()["clean"]
    venue.equity = "9000"
    venue.now += timedelta(hours=2)
    assert engine.reconcile()["clean"]
    restarted = engine_under(mx, V4)[0]
    assert restarted.reconcile()["clean"]
    [base] = on(engine, risk_baseline.BASELINE_EVENT, day)
    assert D(base["body"]["day_start_equity"]) == D(EQUITY)


# --- Earlier policies and frozen V1 -------------------------------------------------------------


def test_a_v3_engine_keeps_alpaca_last_equity(mx):
    engine, venue, _ = engine_under(mx, V3)
    day = next_midnight(venue)
    venue.last_equity, venue.equity = STALE_LAST_EQUITY, EQUITY
    assert engine.reconcile()["clean"]
    assert on(engine, risk_baseline.BASELINE_EVENT, day) == []
    with engine.repo.connect() as conn:
        base = risk_baseline.session_baseline(conn, engine.risk_policy, day)
    assert base == {"day_start_equity": D(STALE_LAST_EQUITY), "source": "ALPACA_LAST_EQUITY",
                    "baseline_event_seq": None}
    # V3's literal 3% halt still measures from the row: −286.52 is above −296.319.
    # (9581.98 − 9877.3 = −295.32 is still above it; the halt below is at −296.32.)
    with engine.store.transaction() as conn:
        assert engine._account_halt(conn, {"equity": EQUITY}, [], D(0)) is None
        assert engine._account_halt(conn, {"equity": "9581.98"}, [], D(0)) is None
        assert engine._account_halt(conn, {"equity": "9580.98"}, [], D(0)) == "DAILY_RISK_HALT"


def test_only_v4_uses_the_v2_baseline():
    class Policy:
        def __init__(self, policy_id):
            self.policy_id = policy_id

    assert risk_baseline.uses_baseline_v2(Policy(V4))
    for other in (V3, "JEV_MANAGED_RISK_V2", "MUSE_JEV_MANAGED_TEST_V1", "CATALYST_RETEST_V1"):
        assert not risk_baseline.uses_baseline_v2(Policy(other))


def test_frozen_v1_and_us_paths_do_not_import_the_v2_baseline():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "catalyst_lab"
    for name in ("risk.py", "risk_math.py", "validation.py", "us_admission.py",
                 "risk_runtime.py"):
        text = (root / name).read_text()
        assert "risk_baseline" not in text and "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2" not in text
    assert '"ALPACA_LAST_EQUITY"' in (root / "risk.py").read_text()


# --- The 2026-10-03 correction ------------------------------------------------------------------


def test_the_live_saturday_is_corrected_once_and_trading_resumes(mx):
    day, latch, (engine, venue, _) = live_saturday(mx)
    [base] = on(engine, risk_baseline.BASELINE_EVENT, day)
    assert D(base["body"]["day_start_equity"]) == D(EQUITY)
    assert base["body"]["started_mid_session"] is True
    [corrected] = on(engine, risk_baseline.CORRECTED_EVENT, day)
    body = corrected["body"]
    assert corrected["idempotency_key"] == f"risk-session-baseline-corrected:{day.isoformat()}"
    assert body["reason"] == "ALPACA_LAST_EQUITY_NOT_ROLLED"
    assert body["owner_approval_ref"] == risk_baseline.OWNER_APPROVAL_REF
    assert (D(body["old_basis"]["day_start_equity"]), body["old_basis"]["source"]) == (
        D(STALE_LAST_EQUITY), "ALPACA_LAST_EQUITY")
    assert (D(body["new_basis"]["day_start_equity"]), body["new_basis"]["source"]) == (
        D(EQUITY), "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2")
    assert body["new_basis"]["baseline_event_seq"] == base["event_seq"]
    assert (body["soft_latch"], body["soft_latch_event_seq"], body["hard_halt"]) == (
        "DECIDED_AT_NEXT_ACCOUNT_CHECK", latch["event_seq"], "NONE")
    # The old row and latch keep their bytes.
    row = session_row(engine, day)
    assert (row["day_start_equity"], row["source"]) == (D(STALE_LAST_EQUITY), "ALPACA_LAST_EQUITY")
    [still] = rows(engine, SOFT_LIMIT_EVENT)
    assert still["body"] == latch["body"] and still["event_seq"] == latch["event_seq"]
    # The next account check withdraws the latch once; the entry is approved.
    sid = engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    [withdrawn] = rows(engine, risk_baseline.WITHDRAWN_EVENT)
    wbody = withdrawn["body"]
    assert withdrawn["idempotency_key"] == f"daily-soft-loss-limit-correction:{latch['event_seq']}"
    assert wbody["latch_event_seq"] == latch["event_seq"]
    assert wbody["correction_event_seq"] == corrected["event_seq"]
    assert (D(wbody["total_pnl"]), D(wbody["threshold"])) == (D(0), D("-191.8156"))
    assert rows(engine, risk_baseline.KEPT_EVENT) == []
    assert rows(engine, SOFT_LIMIT_WAIT_EVENT, sid) == []
    assert verify_events(engine.repo.export_events())["valid"]


def test_the_correction_is_idempotent_across_restarts(mx):
    day, latch, (engine, venue, _) = live_saturday(mx)
    sid = engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    before = kinds(engine, risk_baseline.BASELINE_EVENT, risk_baseline.CORRECTED_EVENT,
                   risk_baseline.WITHDRAWN_EVENT, risk_baseline.KEPT_EVENT, SOFT_LIMIT_EVENT)
    venue.equity = "9600"
    venue.now += timedelta(minutes=5)
    for symbol in ("SOL/USD", "ADA/USD"):
        restarted = engine_under(mx, V4)[0]
        assert restarted.reconcile()["clean"]
        other = restarted.admit(packet((restarted, venue, mx[2]), symbol))
        assert restarted.observe_trigger(other, observation(mx))["outcome"] == "APPROVED"
        venue.now += timedelta(minutes=1)
    after = kinds(engine, risk_baseline.BASELINE_EVENT, risk_baseline.CORRECTED_EVENT,
                  risk_baseline.WITHDRAWN_EVENT, risk_baseline.KEPT_EVENT, SOFT_LIMIT_EVENT)
    assert {k: [r["event_seq"] for r in v] for k, v in after.items()} == {
        k: [r["event_seq"] for r in v] for k, v in before.items()}


def test_a_latch_still_breached_on_the_new_basis_is_kept(mx):
    day, latch, (engine, venue, _) = live_saturday(mx)
    venue.equity = "9398.96"  # 2.0% below the new basis before the first account check.
    sid = engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    assert engine.observe_trigger(sid, observation(mx)) is None
    [kept] = rows(engine, risk_baseline.KEPT_EVENT)
    assert kept["body"]["latch_event_seq"] == latch["event_seq"]
    assert rows(engine, risk_baseline.WITHDRAWN_EVENT) == []
    assert len(rows(engine, SOFT_LIMIT_EVENT)) == 1  # The original latch holds.
    # A same-day recovery does not lift it: the decision is made once.
    venue.equity = EQUITY
    venue.now += timedelta(minutes=1)
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert len(rows(engine, risk_baseline.KEPT_EVENT)) == 1
    assert state(engine, sid)["state"] == "WATCHING"


def test_after_a_withdrawal_a_real_loss_latches_again(mx):
    day, latch, (engine, venue, _) = live_saturday(mx)
    sid = engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    [withdrawn] = rows(engine, risk_baseline.WITHDRAWN_EVENT)
    other = engine.admit(packet((engine, venue, mx[2]), "SOL/USD"))
    venue.equity = str(D(EQUITY) - D("191.8156"))
    venue.now += timedelta(minutes=1)
    assert engine.observe_trigger(other, observation(mx)) is None
    first, second = rows(engine, SOFT_LIMIT_EVENT)
    assert first["event_seq"] == latch["event_seq"]
    assert second["idempotency_key"] == (
        f"{soft_limit_key(V4, day)}:after-withdrawal:{withdrawn['event_seq']}")
    assert D(second["body"]["day_start_equity"]) == D(EQUITY)
    assert len(rows(engine, SOFT_LIMIT_WAIT_EVENT, other)) == 1


def test_a_hard_halt_is_never_lifted_by_the_correction(mx):
    _, venue, _ = mx
    day = next_midnight(venue)
    venue.last_equity, venue.equity = STALE_LAST_EQUITY, "9500"
    old = engine_under(mx, V3)[0]
    with old.store.transaction() as conn:  # −377.3 ≤ −296.319: the old basis's hard halt.
        assert old._account_halt(conn, {"equity": "9500"}, [], D(0)) == "DAILY_RISK_HALT"
    halted = halt_row(old)
    latch = old_release_latch(old, day, STALE_LAST_EQUITY, "9500")
    venue.equity = EQUITY
    venue.now += timedelta(minutes=30)
    engine = engine_under(mx, V4)[0]
    [corrected] = on(engine, risk_baseline.CORRECTED_EVENT, day)
    assert corrected["body"]["hard_halt"] == "UNCHANGED"
    assert corrected["body"]["soft_latch_event_seq"] == latch["event_seq"]
    assert halt_row(engine) == halted
    # Flat against the new basis, the day stays halted: no withdrawal, no new admission.
    with engine.store.transaction() as conn:
        assert engine._account_halt(conn, {"equity": EQUITY}, [], D(0)) == "DAILY_RISK_HALT"
    with pytest.raises(ValueError, match="DAILY_RISK_HALT"):
        engine.admit(packet((engine, venue, mx[2]), "ETH/USD"))
    assert rows(engine, risk_baseline.WITHDRAWN_EVENT) == []
    assert rows(engine, risk_baseline.KEPT_EVENT) == []
    assert halt_row(engine) == halted


def test_v4_without_a_v2_baseline_fails_closed(mx):
    _, venue, _ = mx
    day = next_midnight(venue)
    old = engine_under(mx, V3)[0]
    assert session_row(old, day) is not None
    from catalyst_lab.managed_execution import ManagedExecution

    engine = ManagedExecution(old.repo, old.broker, policy=old.policy, clock=old.now,
                              review_store=old.review_store, risk_policy_id=V4)
    # Not yet reconciled by the V4 engine: the old row is not its basis.
    with engine.store.transaction() as conn:
        assert engine._account_halt(conn, {"equity": EQUITY}, [], D(0)) == (
            "STARTUP_RECONCILIATION_REQUIRED")


@pytest.mark.parametrize("equity", ["0", "-1"])
def test_a_non_positive_equity_records_no_baseline(mx, equity):
    engine, venue, _ = engine_under(mx, V4)
    day = next_midnight(venue)
    venue.equity = equity
    with pytest.raises(ValueError, match="ACCOUNT_EQUITY_REQUIRED"):
        engine.reconcile()
    assert on(engine, risk_baseline.BASELINE_EVENT, day) == []
    assert session_row(engine, day) is None  # The whole reconciliation rolled back.
