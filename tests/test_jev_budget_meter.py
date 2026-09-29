"""``JEV_SPEND_METER_V1`` and ``JEV_SPEND_GUARD_V1`` over a disposable ledger (package jev-budget).

Fixture evidence only: per-test PostgreSQL databases, Jev requests and receipts appended through
the restricted ``catalyst_jev`` store exactly as the reviewer appends them (request bytes of a
chosen size, HTTP errors and non-calls), fixed instants. No provider, broker or network.

The meter's configuration here costs exactly one dollar per million request bytes (price 3 USD
per million tokens, 3 bytes per token), with a one-dollar budget, so every boundary is exact:
EXHAUSTED from 980,000 bytes spent, TIGHT from 900,000, throttling above a per-minute projection
of 0.98 and back to NORMAL at or below 0.93.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest

from catalyst_lab import jev_budget as jb
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import JEV_MODEL, QuestionSet, digest
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.review_runtime import HEALTH_QUESTIONS
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

DOLLAR_PER_MEGABYTE = jb.SpendConfig(D("1"), D("3"), D("3"))
# 00:00 on 20 September in New York: exactly 11 days are left in the month.
NOW = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)
MAINTENANCE = "JEV_MANAGED_POSITION_QUESTIONS_V5"
HEAD = '{"model":"' + JEV_MODEL + '","state":{"pad":"'
TAIL = '"},"questions":' + HEALTH_QUESTIONS.questions_json + "}"


@pytest.fixture
def ledger(er):
    risk = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    jev = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
    return SimpleNamespace(store=ManagedStore(risk), jev=jev, repo=risk)


def call(ledger, at, size, *, outcomes=(("HTTP_ERROR", 500),), version=MAINTENANCE,
         weight=None):
    """One Jev request of exactly ``size`` request bytes and one receipt per attempt."""
    request_id = uuid4()
    pad = size - len(HEAD) - len(TAIL)
    assert pad >= 0, size
    request_json = HEAD + "x" * pad + TAIL
    assert len(request_json.encode()) == size
    identity = {"fixture": "JEV_SPEND_METER", "request": str(request_id)}
    if weight is not None:
        identity["spend_guard"] = {"version": jb.VERSION, "routine_weight": weight}
    template = QuestionSet(version, "TRACKING", HEALTH_QUESTIONS.questions_json)
    assert ledger.jev.start(request_id, identity, template, digest(str(request_id)), request_json,
                            at + timedelta(seconds=10), "ENGINEERING_TEST")
    for attempt, (outcome, status) in enumerate(outcomes, start=1):
        ledger.jev.finish(request_id, attempt, outcome, status, None, None, at, at, 0,
                          "FIXTURE_" + outcome, {})
    return request_id


def meter(ledger, **options):
    return jb.SpendMeter(ledger.repo, **options)


def guard(ledger, config=DOLLAR_PER_MEGABYTE, **options):
    return jb.SpendGuard(config, ledger.store, clock=lambda: NOW, refresh_seconds=0, **options)


def events(ledger, kind):
    with ledger.repo.connect() as conn:
        return conn.execute("""SELECT event_seq,body FROM lab.managed_events
            WHERE kind=%s AND setup_id IS NULL ORDER BY event_seq""", (kind,)).fetchall()


def spent(snapshot):
    return snapshot.month_to_date, snapshot.today, snapshot.projection, \
        snapshot.projection_per_minute


# --- The meter -----------------------------------------------------------------------------------


def test_only_provider_calls_are_priced_from_their_exact_request_bytes(ledger):
    at = NOW - timedelta(hours=1)
    call(ledger, at, 10_000)  # One HTTP 500: a call.
    call(ledger, at, 20_000, outcomes=(("HTTP_ERROR", 429), ("HTTP_ERROR", 500)))  # Two calls.
    call(ledger, at, 30_000, outcomes=(("TRANSPORT_FAILURE", None),))  # May have been sent.
    call(ledger, at, 40_000, outcomes=(("EXPIRED", 200),))  # Answered after its deadline.
    call(ledger, at, 50_000, outcomes=(("CIRCUIT_OPEN", None),))  # Never sent.
    call(ledger, at, 60_000, outcomes=(("EXPIRED", None),))  # Out of time before sending.
    call(ledger, at, 70_000, outcomes=(("HTTP_ERROR", 503), ("CIRCUIT_OPEN", None)))
    call(ledger, at, 5_000, version="CHART_PICK_QUESTIONS_V1")
    call(ledger, at, 4_000, version="MUSE_JEV_COMPARATIVE_QUALITY_V3")
    call(ledger, at, 3_000, version="JEV_DAY_REVIEW_QUESTIONS_V2")
    call(ledger, at, 2_000, version="PROVIDER_HEALTH_V1")
    m = meter(ledger)
    m.refresh(NOW)
    snap = m.snapshot(NOW, DOLLAR_PER_MEGABYTE)
    maintenance = 10_000 + 2 * 20_000 + 30_000 + 40_000 + 70_000
    assert snap.month_calls == 6 + 4
    assert snap.month_request_bytes == maintenance + 14_000
    assert snap.month_by_kind == {"MAINTENANCE": (6, maintenance), "SELECTION": (1, 5_000),
                                  "QUALITY": (1, 4_000), "DAY_REVIEW": (1, 3_000),
                                  "HEALTH_PROBE": (1, 2_000)}
    total = D(maintenance + 14_000) / D(1_000_000)
    assert snap.month_to_date == total
    assert snap.today == 0  # 23:00 on the 19th in New York: yesterday there.
    # Everything in the last 24 hours, so 11 days of the same: exact.
    assert snap.projection == snap.projection_per_minute == total + total * 11
    public = snap.public(DOLLAR_PER_MEGABYTE)
    assert public["month_to_date_usd"] == "0.2040" and public["month"] == "2026-09"
    assert public["month_by_kind_usd"]["MAINTENANCE"] == "0.1900"
    assert public["month_estimated_input_tokens"] == str(D(maintenance + 14_000) / 3)


def test_months_are_new_york_calendar_months_and_the_window_spans_them(ledger):
    september_30 = datetime(2026, 10, 1, 3, 30, tzinfo=UTC)  # 23:30 in New York.
    october_1 = datetime(2026, 10, 1, 4, 30, tzinfo=UTC)  # 00:30 in New York.
    call(ledger, datetime(2026, 9, 5, 16, tzinfo=UTC), 100_000)
    call(ledger, september_30, 200_000)
    call(ledger, october_1, 50_000)
    before = datetime(2026, 10, 1, 3, 59, tzinfo=UTC)  # 23:59 on September 30.
    m = meter(ledger)
    m.refresh(before)
    snap = m.snapshot(before, DOLLAR_PER_MEGABYTE)
    assert (snap.month, snap.month_request_bytes) == ("2026-09", 300_000)
    assert snap.today_request_bytes == 200_000  # September 30 in New York.
    # The first minute of October: a new month, the last day's demand carried into it.
    after = datetime(2026, 10, 1, 4, 45, tzinfo=UTC)
    fresh = meter(ledger)
    fresh.refresh(after)
    snap = fresh.snapshot(after, DOLLAR_PER_MEGABYTE)
    assert (snap.month, snap.month_calls, snap.month_request_bytes) == ("2026-10", 1, 50_000)
    assert snap.today_request_bytes == 50_000
    assert snap.window_request_bytes == 250_000
    left = (datetime(2026, 11, 1, 4, tzinfo=UTC) - after).total_seconds()  # EDT all month.
    assert snap.remaining_days == D(int(left)) / D(86400)
    assert snap.projection == D("0.05") + D("0.25") * snap.remaining_days
    # The meter that saw September rolls over by itself.
    m.refresh(after)
    assert spent(m.snapshot(after, DOLLAR_PER_MEGABYTE)) == spent(snap)


def test_reads_are_incremental_and_a_restart_rebuilds_the_same_totals(ledger):
    for day in (1, 2, 3):  # Last month's calls are never read at start (chunks stop early).
        call(ledger, datetime(2026, 8, day, 12, tzinfo=UTC), 1_000)
    for hour in range(6):
        call(ledger, NOW - timedelta(hours=30 - hour), 10_000 + hour)
    first = meter(ledger)
    first.refresh(NOW)
    watermark = first.watermark
    later = NOW + timedelta(minutes=5)
    call(ledger, later, 12_345, weight=5)
    first.refresh(later)
    assert first.watermark > watermark
    rebuilt, chunked = meter(ledger), meter(ledger, chunk=2)
    rebuilt.refresh(later)
    chunked.refresh(later)
    expected = first.snapshot(later, DOLLAR_PER_MEGABYTE)
    assert rebuilt.snapshot(later, DOLLAR_PER_MEGABYTE) == expected
    assert chunked.snapshot(later, DOLLAR_PER_MEGABYTE) == expected
    assert expected.month_calls == 7 and expected.month_request_bytes == sum(
        10_000 + h for h in range(6)) + 12_345
    # The last 24 hours hold only the new call (the others are 25 to 30 hours old).
    assert expected.window_request_bytes == 12_345
    assert expected.window_weighted_request_bytes == 5 * 12_345


def test_routine_weights_make_the_per_minute_projection(ledger):
    call(ledger, NOW - timedelta(hours=2), 40_000, weight=5)  # A 5-minute routine review.
    call(ledger, NOW - timedelta(hours=1), 40_000, weight=15)  # A 15-minute routine review.
    call(ledger, NOW - timedelta(hours=1), 40_000, weight=1)  # An event review.
    call(ledger, NOW - timedelta(hours=1), 40_000, weight="bogus")  # Read as 1.
    call(ledger, NOW - timedelta(hours=1), 40_000, weight=99)  # Out of range: 1.
    m = meter(ledger)
    m.refresh(NOW)
    snap = m.snapshot(NOW, DOLLAR_PER_MEGABYTE)
    assert snap.window_request_bytes == 200_000
    assert snap.window_weighted_request_bytes == 40_000 * (5 + 15 + 1 + 1 + 1)
    assert snap.projection == D("0.2") + D("0.2") * 11
    assert snap.projection_per_minute == D("0.2") + D("0.92") * 11


# --- The guard -----------------------------------------------------------------------------------


def test_each_tier_from_real_spend_and_the_status_shows_it(ledger):
    g = guard(ledger)
    # 20,000 bytes earlier this month and 80,000 in the last 24 hours: P1 = 0.1 + 0.08 x 11.
    call(ledger, datetime(2026, 9, 5, 12, tzinfo=UTC), 20_000)
    call(ledger, NOW - timedelta(hours=3), 80_000)
    decision = g.evaluate(NOW)
    assert decision.snapshot.projection_per_minute == D("0.98")
    assert (decision.tier, decision.review_bar_seconds) == (jb.NORMAL, 60)
    call(ledger, NOW - timedelta(hours=2), 1_000)  # 0.98 + 0.012: throttled.
    decision = g.evaluate(NOW)
    assert (decision.tier, decision.review_bar_seconds) == (jb.THROTTLED, 300)
    assert decision.rule == "PER_MINUTE_PROJECTION_OVER_BUDGET"
    call(ledger, datetime(2026, 9, 6, 12, tzinfo=UTC), 799_000)  # 900,000 spent: TIGHT.
    decision = g.evaluate(NOW)
    assert (decision.snapshot.month_to_date, decision.tier) == (D("0.9"), jb.TIGHT)
    assert decision.review_bar_seconds == 900
    call(ledger, datetime(2026, 9, 7, 12, tzinfo=UTC), 80_000)  # 980,000 spent: EXHAUSTED.
    decision = g.evaluate(NOW)
    assert (decision.tier, decision.review_bar_seconds) == (jb.EXHAUSTED, None)
    status = g.status(NOW)
    assert {k: status[k] for k in ("version", "available", "tier", "routine_review_seconds",
                                   "budget_usd", "month_to_date_usd", "month")} == {
        "version": "JEV_SPEND_GUARD_V1", "available": True, "tier": "EXHAUSTED",
        "routine_review_seconds": None, "budget_usd": "1", "month_to_date_usd": "0.9800",
        "month": "2026-09"}
    assert status["thresholds_usd"] == {"throttle_above_usd": "0.9800",
                                        "normal_at_or_below_usd": "0.9300",
                                        "tight_from_usd": "0.9000",
                                        "exhausted_from_usd": "0.9800"}
    assert status["projection_usd"] == "1.8710" and status["today_usd"] == "0.0000"
    assert status["tier_since"] == NOW.isoformat()
    tiers = [e["body"] for e in events(ledger, jb.TIER_EVENT)]
    assert [(b["tier"], b["previous_tier"]) for b in tiers] == [
        ("NORMAL", None), ("THROTTLED", "NORMAL"), ("TIGHT", "THROTTLED"),
        ("EXHAUSTED", "TIGHT")]
    assert tiers[-1]["month_to_date_usd"] == "0.9800" and tiers[-1]["budget_usd"] == "1"
    assert tiers[-1]["estimate"] == DOLLAR_PER_MEGABYTE.record()
    assert tiers[1]["previous_event_seq"] == events(ledger, jb.TIER_EVENT)[0]["event_seq"]
    assert verify_events(ledger.repo.export_events())["valid"]


def test_a_tier_is_recorded_once_and_the_configuration_on_each_change(ledger):
    g = guard(ledger)
    for _ in range(3):
        assert g.evaluate(NOW).tier == jb.NORMAL
    assert len(events(ledger, jb.TIER_EVENT)) == 1
    [configured] = events(ledger, jb.CONFIG_EVENT)
    assert configured["body"]["configuration"] == {"policy": jb.policy_record(),
                                                   "estimate": DOLLAR_PER_MEGABYTE.record()}
    guard(ledger).evaluate(NOW)  # A restart with the same configuration records nothing.
    assert len(events(ledger, jb.CONFIG_EVENT)) == 1
    recalibrated = jb.SpendConfig(D("1"), D("3"), D("3.5"))
    guard(ledger, recalibrated).evaluate(NOW)
    guard(ledger).evaluate(NOW)  # And back: each change is its own record.
    bodies = [e["body"] for e in events(ledger, jb.CONFIG_EVENT)]
    assert [b["configuration"]["estimate"]["bytes_per_token"] for b in bodies] == [
        "3", "3.5", "3"]
    assert len(events(ledger, jb.TIER_EVENT)) == 1  # The tier never changed.


def test_throttling_holds_until_93_percent_and_a_restart_keeps_it(ledger):
    call(ledger, datetime(2026, 9, 5, 12, tzinfo=UTC), 20_000)
    call(ledger, NOW - timedelta(hours=3), 80_000)
    call(ledger, NOW - timedelta(hours=2), 1_000)  # P1 0.992: throttled.
    assert guard(ledger).evaluate(NOW).tier == jb.THROTTLED
    # Four hours on, those calls are still in the window; P1 has not moved: still throttled.
    later = NOW + timedelta(hours=4)
    restarted = guard(ledger)  # A new process: the tier comes from the ledger.
    decision = restarted.evaluate(later)
    assert D("0.93") < decision.snapshot.projection_per_minute <= D("0.98")
    assert decision.tier == jb.THROTTLED
    # The same spend on a ledger that never throttled would be NORMAL (no hysteresis).
    assert jb.tier_for(month_to_date=decision.snapshot.month_to_date,
                       projection_per_minute=decision.snapshot.projection_per_minute,
                       budget=D("1"), previous_tier=None)[0] == jb.NORMAL
    # A day later the window is empty: P1 is month-to-date alone, back to NORMAL.
    tomorrow = NOW + timedelta(days=1, hours=1)
    decision = guard(ledger).evaluate(tomorrow)
    assert decision.snapshot.projection_per_minute == D("0.101")
    assert decision.tier == jb.NORMAL


def test_an_unreadable_meter_keeps_the_last_decision_briefly_then_fails_closed(ledger):
    class Flaky(jb.SpendMeter):
        broken = False

        def refresh(self, now):
            if self.broken:
                raise RuntimeError("fixture: ledger unreadable")
            super().refresh(now)

    flaky = Flaky(ledger.repo)
    g = guard(ledger, meter=flaky)
    assert g.evaluate(NOW).tier == jb.NORMAL
    flaky.broken = True
    assert g.evaluate(NOW + timedelta(seconds=120)).tier == jb.NORMAL  # The last decision.
    decision = g.evaluate(NOW + timedelta(seconds=121))
    assert (decision.tier, decision.available, decision.review_bar_seconds, decision.code) == (
        jb.UNAVAILABLE, False, None, "RuntimeError")
    status = g.status(NOW + timedelta(seconds=122))
    assert (status["available"], status["tier"], status["code"]) == (
        False, "UNAVAILABLE", "RuntimeError")
    flaky.broken = False
    assert g.evaluate(NOW + timedelta(seconds=180)).tier == jb.NORMAL
    assert len(events(ledger, jb.TIER_EVENT)) == 1  # UNAVAILABLE is never recorded as a tier.


def test_a_guard_reads_nothing_until_asked_and_is_thread_safe(ledger):
    import threading

    g = jb.SpendGuard(DOLLAR_PER_MEGABYTE, SimpleNamespace(), clock=lambda: NOW)
    assert g.evaluate(NOW).tier == jb.UNAVAILABLE  # No ledger: fails closed, never raises.
    call(ledger, NOW - timedelta(hours=1), 10_000)
    shared, seen = guard(ledger), []
    threads = [threading.Thread(target=lambda: seen.append(shared.evaluate(NOW).tier))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == [jb.NORMAL] * 8 and len(events(ledger, jb.TIER_EVENT)) == 1
