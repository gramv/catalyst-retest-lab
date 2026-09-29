"""The status reports the last completed clean reconciliation (package learning-app).

A watchdog read that landed inside a reconciliation pass (about 0.3 s every 30 s) saw
``last_reconciliation_at: null`` and raised a false ``RECONCILIATION_STALE``. The status now
reports when the last pass completed clean; ``ready()`` is exactly as fail-closed as before.
Fixture runtime only (no broker, database or network).
"""

from datetime import timedelta

from catalyst_lab.managed_app import public_status
from catalyst_lab.managed_ops import status_alarms
from tests.test_managed_runtime import runtime
from tests.test_setup_scan import NOW

POLICY = {"tick_max_age_seconds": 15, "reconciliation_max_age_seconds": 90,
          "research_max_age_seconds": 180}


def test_a_status_read_inside_a_pass_shows_the_last_clean_reconciliation():
    run = runtime()
    assert run.status()["last_clean_reconciliation_at"] is None
    assert public_status(run.status())["last_reconciliation_at"] is None
    assert run.reconcile_once()
    assert run.status()["last_clean_reconciliation_at"] == NOW.isoformat()
    seen = {}
    original = run.execution.reconcile

    def reconcile_and_look():
        # The watchdog's read, taken while this pass is running.
        seen["status"], seen["ready"] = run.status(), run.ready()
        return original()

    run.execution.reconcile = reconcile_and_look
    assert run.reconcile_once()
    inside = seen["status"]
    assert inside["reconciled_at"] is None and seen["ready"] is False  # Still fail-closed.
    assert inside["last_clean_reconciliation_at"] == NOW.isoformat()
    public = public_status(inside)
    assert public["last_reconciliation_at"] == NOW.isoformat()
    alarms = status_alarms(public, NOW + timedelta(seconds=1), POLICY)
    assert "RECONCILIATION_STALE" not in alarms


def test_an_unclean_or_interrupted_reconciliation_still_goes_stale():
    run = runtime()
    assert run.reconcile_once()
    run.execution.clean = False
    assert run.reconcile_once() is False
    status = run.status()
    assert status["reconciled_at"] is None and not run.ready()  # Entries stay refused.
    public = public_status(status)
    assert public["last_reconciliation_at"] == NOW.isoformat()  # The last clean one.
    assert "RECONCILIATION_STALE" not in status_alarms(public, NOW + timedelta(seconds=90),
                                                       POLICY)
    assert "RECONCILIATION_STALE" in status_alarms(public, NOW + timedelta(seconds=91), POLICY)
    run.market_gap("CRYPTO", "TEST_GAP")
    assert run.status()["reconciled_at"] is None and not run.ready()
    # The gap never invents a clean reconciliation; the heartbeat ignores the timestamp.
    assert run.status()["last_clean_reconciliation_at"] == NOW.isoformat()
    assert "last_clean_reconciliation_at" in type(run)._HEARTBEAT_VOLATILE
