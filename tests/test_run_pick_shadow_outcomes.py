"""The shadow-outcome CLI's own delegation: a real (disposable) risk-role DB connection,
never a real network call (nothing in the fresh ledger needs one)."""

from datetime import UTC, datetime

from scripts.run_pick_shadow_outcomes import _aware, run
from tests.test_execution import er as er  # noqa: F401 -- ``mx`` needs this fixture in scope
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx  # noqa: F401


def test_run_reads_only_the_database_url_and_returns_a_job_summary(mx):  # noqa: F811
    engine, _, _ = mx
    config = {"environment": {"MANAGED_DATABASE_URL": engine.repo.database_url}}
    summary = run(config, now=datetime(2026, 1, 2, tzinfo=UTC))
    assert summary["cycles_scanned"] == 0  # A fresh ledger: nothing to compute, no network hit.
    assert summary["recorded"] == 0
    assert set(summary) >= {"picks_considered", "already_recorded", "not_yet_ready",
                            "bar_fetch_failed", "invalid_levels", "recorded_item_keys"}


def test_aware_accepts_none_and_offset_timestamps_only():
    assert _aware(None) is None
    assert _aware("2026-01-01T00:00:00+00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    try:
        _aware("2026-01-01T00:00:00")
    except SystemExit as exc:
        assert "AWARE_TIMESTAMP_REQUIRED" in str(exc)
    else:
        raise AssertionError("naive timestamp should have been refused")
