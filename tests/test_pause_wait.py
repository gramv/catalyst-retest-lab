"""``OPERATOR_PAUSE_ENTRY_WAIT_V1`` (owner, 2026-09-29): a trigger during an operator pause
waits, and the setup can enter after ``operator resume``; every other halt, the daily-loss halt
included, stays terminal.

Disposable PostgreSQL, the fake paper venue and the operator role's real ``lab.operator_pause``
and ``lab.operator_resume`` only.
"""

from datetime import UTC, timedelta

import pytest

from catalyst_lab import pause_wait
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_service import STATE_FIELDS
from tests.test_crypto_trigger import TOUCH, quote_only, rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation, packet
from tests.test_operator_controls import active, pause, record_halt, resume
from tests.test_position_monitor import opened
from tests.test_system_check import market as market  # noqa: F401
from tests.test_system_check import stream, system_runtime


def decisions(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT action,outcome,reason FROM lab.managed_risk_decisions WHERE setup_id=%s
            ORDER BY created_at""", (sid,)).fetchall()


def waits(engine, sid):
    return rows(engine, pause_wait.WAIT_EVENT, sid)


def buys(venue, symbol):
    return [o for o in venue.orders_of("buy") if o["symbol"] == symbol]


def test_admission_records_the_version_for_every_setup(mx):
    engine, _, _ = mx
    for sid in (engine.admit(packet(mx, "BTC/USD")), engine.admit(packet(mx, "SPY")),
                *v3_setups(mx, ["AAA/USD"]).values()):
        assert state(engine, sid)["pause_wait_version"] == pause_wait.PAUSE_WAIT_VERSION
    assert "pause_wait_version" in STATE_FIELDS


def test_a_trigger_during_a_pause_waits_and_a_trigger_after_resume_enters(er, mx):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "ETH/USD"))
    paused = pause(er)
    calls = len(venue.calls)
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert engine.observe_trigger(sid, observation(mx)) is None  # The same minute.
    assert len(venue.calls) == calls  # No broker read, let alone an order.
    assert state(engine, sid)["state"] == "WATCHING"
    assert decisions(engine, sid) == [] and not buys(venue, "ETH/USD")
    [wait] = waits(engine, sid)
    seen = observation(mx)
    assert wait["body"] == {
        "reason": "OPERATOR_PAUSE",
        "version": "OPERATOR_PAUSE_ENTRY_WAIT_V1",
        "halt_ids": [paused],
        "trigger": {k: seen[k] for k in ("trade_price", "trade_at", "bid", "ask", "quote_at")},
        "waited_at": venue.now.isoformat(),
    }
    minute = venue.now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    assert wait["idempotency_key"] == f"operator-pause-entry-wait:{sid}:{minute}"
    venue.now += timedelta(minutes=1)
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert len(waits(engine, sid)) == 2  # One record per minute of triggers.
    assert resume(er) == [paused]
    venue.now += timedelta(seconds=1)
    assert engine.reconcile()["clean"]  # As the runtime does every 30 s.
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "APPROVED"
    [entry] = buys(venue, "ETH/USD")
    assert entry["client_order_id"] == decision["payload"]["client_order_id"]
    assert state(engine, sid)["state"] == "ORDER_SUBMITTED"
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_report_v3_quote_touch_during_a_pause_waits_then_enters_after_resume(er, mx):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    pause(er)
    assert engine.observe_trigger(sid, quote_only(mx, *TOUCH)) is None
    assert state(engine, sid)["state"] == "WATCHING" and decisions(engine, sid) == []
    [wait] = waits(engine, sid)
    assert (wait["body"]["trigger"]["bid"], wait["body"]["trigger"]["ask"]) == TOUCH
    assert not rows(engine, "TRIGGER_CONFIRMED", sid)
    resume(er)
    assert engine.observe_trigger(sid, quote_only(mx, *TOUCH))["outcome"] == "APPROVED"
    assert len(buys(venue, "AAA/USD")) == 1


def test_the_runtime_quote_pass_waits_during_a_pause_and_enters_after_resume(er, mx, market):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    run = system_runtime(mx, market, ["AAA/USD"])
    pause(er)
    for _ in range(3):  # One touch a second, as the protection pass evaluates quotes.
        venue.now += timedelta(seconds=1)
        stream(run, "AAA/USD", *TOUCH, at=venue.now)
        run.execution_once()
    assert state(engine, sid)["state"] == "WATCHING" and decisions(engine, sid) == []
    assert len(waits(engine, sid)) == 1 and not buys(venue, "AAA/USD")
    resume(er)
    venue.now += timedelta(seconds=1)
    stream(run, "AAA/USD", *TOUCH, at=venue.now)
    run.execution_once()
    assert len(buys(venue, "AAA/USD")) == 1
    assert run.error is None and not run.latches.blocking()


def test_a_pause_that_begins_during_the_entry_reads_waits(er, mx, monkeypatch):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "ETH/USD"))
    snapshot = engine.account_snapshot

    def paused_meanwhile(**options):
        result = snapshot(**options)
        pause(er)  # After the first check, before the shared lock.
        return result

    monkeypatch.setattr(engine, "account_snapshot", paused_meanwhile)
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert state(engine, sid)["state"] == "WATCHING" and decisions(engine, sid) == []
    assert len(waits(engine, sid)) == 1 and not buys(venue, "ETH/USD")


def test_the_daily_loss_halt_stays_terminal_and_flattens_during_a_pause(er, mx):
    engine, venue, _ = mx
    held = opened(mx, "BTC/USD")
    [stop] = venue.orders_of("sell", "stop_limit")
    watching = engine.admit(packet(mx, "ETH/USD"))
    pause(er)
    venue.equity = "9700"  # 3% below the day's starting equity.
    engine.manage(held, observation(mx))  # The protection pass records the daily-loss halt.
    assert state(engine, held)["exit_requested"] == "DAILY_RISK_HALT"
    decision = engine.observe_trigger(watching, observation(mx))
    assert (decision["outcome"], decision["reason"]) == ("REJECTED", "DAILY_RISK_HALT")
    assert state(engine, watching)["state"] == "RISK_REJECTED"
    assert waits(engine, watching) == []
    # Cancel and flatten, while the pause is still in force.
    engine.manage(held, observation(mx))
    assert stop["status"] == "canceled"
    [close] = venue.orders_of("sell", "market")
    assert engine.ingest(venue.fill(close["id"], close["qty"], price="99.90"))
    engine.manage(held, observation(mx))
    closed = state(engine, held)
    assert (closed["state"], closed["reason"]) == ("CLOSED", "DAILY_RISK_HALT")
    assert [reason for _, reason in active(er)] == ["OPERATOR_PAUSE"]
    resume(er)
    assert engine.observe_trigger(watching, observation(mx)) is None  # Ended for good.


@pytest.mark.parametrize("with_pause", [False, True], ids=["other-halt", "pause-and-other"])
def test_any_other_halt_keeps_the_terminal_risk_halt(er, mx, with_pause):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx, "ETH/USD"))
    if with_pause:
        pause(er)
    record_halt(engine.repo, "TEST_FIXTURE_HALT")
    decision = engine.observe_trigger(sid, observation(mx))
    assert (decision["outcome"], decision["reason"]) == ("REJECTED", "RISK_HALT")
    assert state(engine, sid)["state"] == "RISK_REJECTED" and waits(engine, sid) == []
    assert not buys(venue, "ETH/USD")


def test_setups_admitted_before_the_version_keep_the_terminal_risk_halt(er, mx, monkeypatch):
    engine, venue, _ = mx
    monkeypatch.setattr(pause_wait, "admission_fields", lambda packet: {})
    sid = engine.admit(packet(mx, "ETH/USD"))
    assert "pause_wait_version" not in state(engine, sid)
    pause(er)
    decision = engine.observe_trigger(sid, observation(mx))
    assert (decision["outcome"], decision["reason"]) == ("REJECTED", "RISK_HALT")
    assert state(engine, sid)["state"] == "RISK_REJECTED" and waits(engine, sid) == []
    resume(er)
    assert engine.observe_trigger(sid, observation(mx)) is None  # Lost for good, as before.
    assert not buys(venue, "ETH/USD")
