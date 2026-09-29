"""The nightly learning jobs (``LEARNING_JOBS_V1``, package learning-app, 2026-09-28; plan
``docs/LEARNING-LOOP-PLAN.md`` section 7, owner decision of 2026-09-28: a Railway cron service
``jobs``, not inside the trader).

``cloud_runtime jobs`` runs these steps once, in order, each independent (a failing step is
logged with its code and the next still runs), then exits 0:

1. ``shadow_outcomes``: the existing ``PICK_SHADOW_OUTCOME_V1`` job over the report-V3 cycles of
   the last 40 days (every pick whose window plus the 24-hour hold has passed).
2. ``maintenance_replays``: the recorded ``UNCHANGED_PLAN_REPLAY_V1`` and
   ``DAY_REVIEW_DECISION_REPLAY_V1`` counterfactuals now complete (``learning_replays``).
3. ``market_reality``: ``MARKET_REALITY_V1`` of the previous New York day.
4. ``scorecard``: ``DAILY_SCORECARD_V1`` of the previous New York day.
5. ``weekly_review``: ``WEEKLY_REVIEW_V1`` of the latest Monday-to-Sunday week once it has ended
   (the Monday run records it).

Every record is keyed by its day or week, so a rerun records nothing twice. A missed night is
caught up: steps 3 and 4 also take the two days before the previous one when those are not
recorded yet, and step 5 records a finished week on the first run after it. Nothing here places
an order, holds a broker or Jev credential, or sends anything to Jev; the bars are Alpaca's
public, keyless crypto bars.
"""

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from catalyst_lab.learning_replays import record_replays
from catalyst_lab.market import NY
from catalyst_lab.market_reality import RealityUnavailable, record_day
from catalyst_lab.pick_outcomes import report_v3_cycle_started, run_shadow_outcome_job
from catalyst_lab.scorecard import ScorecardUnavailable, record_scorecard
from catalyst_lab.weekly_review import last_completed_week_end, record_review

JOBS_VERSION = "LEARNING_JOBS_V1"
STEPS = ("shadow_outcomes", "maintenance_replays", "market_reality", "scorecard",
         "weekly_review")
CATCH_UP_DAYS = 3  # The previous day and the two before it.
SHADOW_LOOKBACK = timedelta(days=40)
SHADOW_PAGE = 200
_CODE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@dataclass
class StepResult:
    name: str
    result: str  # OK, FAILED or SKIPPED
    code: str | None = None
    details: dict = field(default_factory=dict)


def failure_code(exc):
    """A code from an exception, never its text: ``str(exc)`` when it is a code."""
    text = str(exc)
    if 3 <= len(text) <= 64 and set(text) <= _CODE_CHARS and text[0].isalpha():
        return text
    return type(exc).__name__.upper()


def previous_days(now):
    """The previous New York day and the ``CATCH_UP_DAYS - 1`` before it, oldest first."""
    yesterday = now.astimezone(NY).date() - timedelta(days=1)
    return [yesterday - timedelta(days=n) for n in range(CATCH_UP_DAYS - 1, -1, -1)]


def shadow_step(store, reader, now):
    totals, cursor = Counter(), 0
    while True:
        page = report_v3_cycle_started(store.repo, after_event_seq=cursor, limit=SHADOW_PAGE)
        if not page:
            break
        summary = run_shadow_outcome_job(store, reader, now=now, after_cycle_event_seq=cursor,
                                         limit=SHADOW_PAGE, since=now - SHADOW_LOOKBACK)
        totals.update({k: v for k, v in summary.to_dict().items() if isinstance(v, int)})
        cursor = page[-1]["event_seq"]
        if len(page) < SHADOW_PAGE:
            break
    return None, {k: totals[k] for k in ("recorded", "not_yet_ready", "bar_fetch_failed",
                                         "invalid_levels", "already_recorded")}


def replay_step(store, reader, now):
    summary = record_replays(store, reader, now=now)
    return ("REPLAY_BARS_UNAVAILABLE" if summary["failed"] else None), summary


def daily_step(record, unavailable):
    def step(store, reader, now):
        details, code = {}, None
        for day in previous_days(now):
            try:
                status, _ = record(store, reader, day, now=now)
            except unavailable as exc:
                status = failure_code(exc)
                code = code or status
            details[day.isoformat()] = status
        return code, details
    return step


def weekly_step(store, reader, now):
    week_end = last_completed_week_end(now)
    status, _ = record_review(store, week_end, now=now)
    return None, {week_end.isoformat(): status}


def run_jobs(store, reader, *, now, deadline=None, monotonic=time.monotonic):
    """Every step once, in order, each independent; a step not started by ``deadline`` (a
    ``monotonic`` instant) is SKIPPED ``JOB_TIME_LIMIT``. Returns the ``StepResult``s."""
    now = now.astimezone(UTC)
    steps = {
        "shadow_outcomes": shadow_step,
        "maintenance_replays": replay_step,
        "market_reality": daily_step(record_day, RealityUnavailable),
        "scorecard": daily_step(lambda s, _r, day, now: record_scorecard(s, day, now=now),
                                ScorecardUnavailable),
        "weekly_review": weekly_step,
    }
    results = []
    for name in STEPS:
        if deadline is not None and monotonic() >= deadline:
            results.append(StepResult(name, "SKIPPED", "JOB_TIME_LIMIT"))
            continue
        try:
            code, details = steps[name](store, reader, now)
        except Exception as exc:  # noqa: BLE001 -- each step is independent by design.
            results.append(StepResult(name, "FAILED", failure_code(exc)))
            continue
        results.append(StepResult(name, "FAILED" if code else "OK", code, details))
    return results


__all__ = ["CATCH_UP_DAYS", "JOBS_VERSION", "STEPS", "StepResult", "failure_code",
           "previous_days", "run_jobs"]
