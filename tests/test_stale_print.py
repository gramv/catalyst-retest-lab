"""``STALE_PRINT_ABOVE_TRIGGER_V1`` (owner approval 2026-09-28): a late print above the entry
trigger is consumed and the setup keeps watching; everything else keeps today's revocation.

The fixture venue's report-V3 setups are admitted at entry trigger 100, max entry 100.10 and
stop 95 (``tests.test_crypto_trigger.v3_setups``).
"""

from datetime import timedelta

import pytest

from catalyst_lab import stale_print
from tests.test_crypto_trigger import rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import packet
from tests.test_system_check import feed as feed  # noqa: F401
from tests.test_system_check import market as market  # noqa: F401
from tests.test_system_check import system_runtime


def late_print(run, venue, symbol, price, *, seconds=6, trade_id=1):
    """A trade print that reaches the runtime ``seconds`` after it happened, half a minute after
    the setup's admission (a print before admission is consumed for that reason instead)."""
    venue.now += timedelta(seconds=30)
    assert run.reconcile_once()
    run.market_message("CRYPTO", {"T": "t", "S": symbol, "p": price, "i": trade_id,
                                  "t": (venue.now - timedelta(seconds=seconds)).isoformat()})


def consumed(engine, sid):
    return [r["body"]["reason"] for r in rows(engine, "MARKET_PRINT_CONSUMED", sid)]


def test_admission_records_the_version_for_report_v3_crypto_setups_only(mx):
    engine, _, _ = mx
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    v2 = engine.admit(packet(mx, "BTC/USD"))
    assert state(engine, v3)["stale_print_version"] == stale_print.STALE_PRINT_VERSION
    assert "stale_print_version" not in state(engine, v2)


def test_a_late_print_above_the_trigger_is_consumed_and_the_setup_keeps_watching(mx, market):
    """The first live day's case: XRP's print 2.5% above its trigger, six seconds late."""
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    run = system_runtime(mx, market, ["AAA/USD"])
    late_print(run, venue, "AAA/USD", "102.5")
    run.execution_once()
    now = state(engine, sid)
    assert now["state"] == "WATCHING" and not now.get("revoked")
    assert consumed(engine, sid) == [stale_print.CONSUMED_REASON]
    assert rows(engine, "REVOKE", sid) == []


@pytest.mark.parametrize("price", ["100", "99.5", "94"])  # At, below the trigger, below the stop.
def test_a_late_print_at_or_below_the_trigger_still_revokes(mx, market, price):
    engine, venue, _ = mx
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    run = system_runtime(mx, market, ["AAA/USD"])
    late_print(run, venue, "AAA/USD", price)
    run.execution_once()
    now = state(engine, sid)
    assert (now["state"], now["revocation_reason"]) == ("INVALIDATED", "DATA_FEED_FAILURE")
    assert consumed(engine, sid) == ["PRINT_PROCESSING_DEADLINE"]


def test_setups_admitted_before_the_version_keep_todays_revocation(mx, market, monkeypatch):
    engine, venue, _ = mx
    monkeypatch.setattr(stale_print, "admission_fields", lambda packet: {})
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    assert "stale_print_version" not in state(engine, sid)
    run = system_runtime(mx, market, ["AAA/USD"])
    late_print(run, venue, "AAA/USD", "102.5")
    run.execution_once()
    assert state(engine, sid)["revocation_reason"] == "DATA_FEED_FAILURE"
    assert consumed(engine, sid) == ["PRINT_PROCESSING_DEADLINE"]


def test_harmless_only_for_this_version_and_a_price_strictly_above_the_trigger():
    active = {"stale_print_version": stale_print.STALE_PRINT_VERSION}
    levels = {"entry_trigger": "100"}
    assert stale_print.harmless(active, levels, {"trade_price": "100.01"})
    assert not stale_print.harmless(active, levels, {"trade_price": "100"})
    assert not stale_print.harmless({}, levels, {"trade_price": "150"})
    assert not stale_print.harmless({"stale_print_version": "OTHER"}, levels,
                                    {"trade_price": "150"})
    # Anything unclear keeps today's handling.
    assert not stale_print.harmless(active, {}, {"trade_price": "150"})
    assert not stale_print.harmless(active, levels, {"trade_price": "not-a-number"})
    assert not stale_print.harmless(active, levels, {})
