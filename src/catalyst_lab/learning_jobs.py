"""The nightly learning jobs (``LEARNING_JOBS_V1``, package learning-app, 2026-09-28; plan
``docs/LEARNING-LOOP-PLAN.md`` section 7, owner decision of 2026-09-28: a Railway cron service
``jobs``, not inside the trader).

``cloud_runtime jobs`` runs these steps once, in order, each independent (a failing step is
logged with its code and the next still runs), then exits: 0 when every step ended OK, else 1
(package ops-alarms, 2026-09-29; it exited 0 whatever the steps logged before):

1. ``shadow_outcomes``: the existing ``PICK_SHADOW_OUTCOME_V1`` job over the report-V3 cycles of
   the last 40 days (every pick whose window plus the 24-hour hold has passed), then
   ``STRATEGY_SHADOW_V1`` (package strategy-c1, 2026-10-03; ``strategy_shadow``): the mechanical
   strategies' signals of the last 40 days and their simulated outcomes, no orders.
2. ``maintenance_replays``: the recorded ``UNCHANGED_PLAN_REPLAY_V1`` and
   ``DAY_REVIEW_DECISION_REPLAY_V1`` counterfactuals now complete (``learning_replays``).
3. ``jev_calibration``: ``JEV_CALIBRATION_V1`` (package jev-b3, 2026-10-03): each Jev
   probability of the last 40 days (top-K pick reviews, answered maintenance reviews) whose
   outcome window is complete, joined to its outcome once (``jev_calibration``; record-only).
4. ``market_regime``: ``MARKET_REGIME_V1`` (package learning-measure, 2026-10-02) of every
   New York day of the last 40 not recorded yet, and the at-entry tag of every trade entered in
   them (``market_regime.record_regimes``; its own catch-up and the backfill of older days).
5. ``market_reality``: ``MARKET_REALITY_V1`` of the previous New York day (with the day's
   recorded regime).
6. ``trade_paths`` (package learning-loop2, 2026-10-03): ``AFTER_EXIT_PATH_V1`` of every
   closed trade whose 24 hours after the exit have passed, and ``MANAGEMENT_CHANGE_CONTEXT_V1``
   of every recorded replay (``trade_paths``; 40-day lookback, record-only).
7. ``missed_tradeable`` (package learning-loop2): ``MISSED_TRADEABLE_V1`` of each day whose
   movers' simulated holds have all passed (normally the day before yesterday), up to four
   older unrecorded days of the last 14 per run (``daily_brief.missed_step``).
8. ``scorecard``: ``DAILY_SCORECARD_V1`` of the previous New York day.
9. ``daily_brief`` (package learning-loop2): ``DAILY_BRIEF_V1`` of the previous New York day --
   the daily speed: observe and explain, research attention only (``daily_brief``).
10. ``weekly_review``: ``WEEKLY_REVIEW_V1`` of the latest Monday-to-Sunday week once it has ended
   (the Monday run records it); since package learning-loop2 it carries the weekly speed's
   ``learning`` section (PROPOSE-only, never applied).

Every record is keyed by its day or week, so a rerun records nothing twice. A missed night is
caught up: steps 5 and 6 also take the two days before the previous one when those are not
recorded yet (steps 5, 8 and 9), step 7 catches up older days within its lookback, and step
10 records a finished week on the first run after it. Nothing here places
an order, holds a broker or Jev credential, or sends anything to Jev; the bars are Alpaca's
public, keyless crypto bars.
"""

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from catalyst_lab.daily_brief import NOT_APPLICABLE as BRIEF_NOT_APPLICABLE
from catalyst_lab.daily_brief import BriefUnavailable, missed_step, record_brief
from catalyst_lab.jev_calibration import record_calibration
from catalyst_lab.learning_replays import record_replays
from catalyst_lab.market import NY
from catalyst_lab.market_reality import RealityUnavailable, record_day
from catalyst_lab.market_regime import record_regimes
from catalyst_lab.pick_outcomes import report_v3_cycle_started, run_shadow_outcome_job
from catalyst_lab.scorecard import ScorecardUnavailable, record_scorecard
from catalyst_lab.strategy_shadow import run_strategy_shadow
from catalyst_lab.trade_paths import record_trade_paths
from catalyst_lab.weekly_review import last_completed_week_end, record_review

JOBS_VERSION = "LEARNING_JOBS_V1"
STEPS = ("shadow_outcomes", "maintenance_replays", "jev_calibration", "market_regime",
         "market_reality", "trade_paths", "missed_tradeable", "scorecard", "daily_brief",
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
    details = {k: totals[k] for k in ("recorded", "not_yet_ready", "bar_fetch_failed",
                                      "invalid_levels", "already_recorded")}
    # STRATEGY_SHADOW_V1 (package strategy-c1): the mechanical strategies' shadow signals and
    # outcomes, after the picks' (whose records it never touches). A failure is this step's code;
    # the picks' outcomes above are already recorded.
    try:
        code, strategy = run_strategy_shadow(store, reader, now=now)
    except Exception as exc:  # noqa: BLE001 -- recorded as the step's code, never raised.
        code, strategy = failure_code(exc), {}
    details.update({f"strat_{k}": v for k, v in strategy.items()})
    return code, details


def replay_step(store, reader, now):
    summary = record_replays(store, reader, now=now)
    if summary["invalid"]:  # A setup's recorded data could not be replayed.
        return "REPLAY_DATA_INVALID", summary
    return ("REPLAY_BARS_UNAVAILABLE" if summary["failed"] else None), summary


# A day that ended before the ledger recorded any research universe can never be measured
# (MARKET_REALITY_V1 needs one; later days always have the latest). Its status stays in the
# step's details, but it is not a failure: on the cloud ledger's first nights, the catch-up days
# before its first research run failed the step and the run every night (2026-09-29).
NOT_APPLICABLE = frozenset({"REALITY_UNIVERSE_UNAVAILABLE"}) | BRIEF_NOT_APPLICABLE


def daily_step(record, unavailable):
    def step(store, reader, now):
        details, code = {}, None
        for day in previous_days(now):
            try:
                status, _ = record(store, reader, day, now=now)
            except unavailable as exc:
                status = failure_code(exc)
                if status not in NOT_APPLICABLE:
                    code = code or status
            details[day.isoformat()] = status
        return code, details
    return step


def calibration_step(store, reader, now):
    """JEV_CALIBRATION_V1's records. A pick or setup whose bars could not be read is left for
    the next run and fails the step (as the replays); data that cannot be joined is counted
    (``invalid``) and never blocks the step: the record is measurement only."""
    summary = record_calibration(store, reader, now=now)
    return ("JEV_CALIBRATION_BARS_UNAVAILABLE" if summary["bar_fetch_failed"] else None), summary


def regime_step(store, reader, now):
    """MARKET_REGIME_V1's days and trade tags; a failed bar read records nothing (raised)."""
    return None, record_regimes(store, reader, now=now)


def trade_paths_step(store, reader, now):
    """AFTER_EXIT_PATH_V1 and MANAGEMENT_CHANGE_CONTEXT_V1 (record-only); a failed bar read
    leaves its trade or change for the next run and fails the step."""
    return record_trade_paths(store, reader, now=now)


def missed_tradeable_step(store, reader, now):
    return missed_step(store, reader, now)


def weekly_step(store, reader, now):
    week_end = last_completed_week_end(now)
    status, _ = record_review(store, week_end, now=now)
    return None, {week_end.isoformat(): status}


def run_jobs(store, reader, *, now, deadline=None, monotonic=time.monotonic, on_result=None):
    """Every step once, in order, each independent; a step not started by ``deadline`` (a
    ``monotonic`` instant) is SKIPPED ``JOB_TIME_LIMIT``. ``on_result`` receives each
    ``StepResult`` as its step ends (the cron logs it at once, so a run the hard stop ends
    keeps the lines of the steps it finished). Returns the ``StepResult``s."""
    now = now.astimezone(UTC)
    steps = {
        "shadow_outcomes": shadow_step,
        "maintenance_replays": replay_step,
        "jev_calibration": calibration_step,
        "market_regime": regime_step,
        "market_reality": daily_step(record_day, RealityUnavailable),
        "trade_paths": trade_paths_step,
        "missed_tradeable": missed_tradeable_step,
        "scorecard": daily_step(lambda s, _r, day, now: record_scorecard(s, day, now=now),
                                ScorecardUnavailable),
        "daily_brief": daily_step(record_brief, BriefUnavailable),
        "weekly_review": weekly_step,
    }
    results = []
    for name in STEPS:
        if deadline is not None and monotonic() >= deadline:
            result = StepResult(name, "SKIPPED", "JOB_TIME_LIMIT")
        else:
            try:
                code, details = steps[name](store, reader, now)
            except Exception as exc:  # noqa: BLE001 -- each step is independent by design.
                result = StepResult(name, "FAILED", failure_code(exc))
            else:
                result = StepResult(name, "FAILED" if code else "OK", code, details)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


__all__ = ["CATCH_UP_DAYS", "JOBS_VERSION", "STEPS", "StepResult", "failure_code",
           "previous_days", "run_jobs"]
