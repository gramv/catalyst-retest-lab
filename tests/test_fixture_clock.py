"""Wall-clock fixture sessions validate at every New York instant, including midnight."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from catalyst_lab.domain import Candidate
from catalyst_lab.market import NY
from catalyst_lab.validation import validate
from tests.clock import (
    EDGE,
    FLATTEN,
    GUARD,
    fixture_now,
    fixture_session,
    ny_midnight,
    session_bounds,
)

# A normal New York date, then the 23-hour and 25-hour daylight-saving dates.
DAYS = [("2026-09-18", 96), ("2026-03-08", 92), ("2026-11-01", 100)]
NEAR_MIDNIGHT = [
    time(0, 0),
    time(0, 1),
    time(0, 9, 59),
    time(0, 10),
    time(23, 49, 59),
    time(23, 50),
    time(23, 54),
    time(23, 58),
    time(23, 59, 59, 999999),
]


def instants(day):
    """Every quarter hour of one New York date plus the instants nearest its midnights."""
    start, end = ny_midnight(day), ny_midnight(day + timedelta(days=1))
    quarter = timedelta(minutes=15)
    grid = [start + i * quarter for i in range((end - start) // quarter)]
    return grid, [datetime.combine(day, t, NY).astimezone(UTC) for t in NEAR_MIDNIGHT]


def session_evidence(evidence, now, opens, closes):
    day = now.astimezone(NY).date()
    return replace(
        evidence,
        observed_at=now,
        quote_timestamp=now,
        session_date=day,
        reconciled_session=day,
        official_open=opens,
        official_close=closes,
    )


@pytest.mark.parametrize("hours", [1, 2])
@pytest.mark.parametrize("day,quarters", DAYS)
def test_fixture_session_validates_at_every_new_york_instant(
    raw, evidence, policy, day, quarters, hours
):
    local = date.fromisoformat(day)
    grid, near = instants(local)
    assert len(grid) == quarters
    start, end = ny_midnight(local), ny_midnight(local + timedelta(days=1))
    span = timedelta(hours=hours)
    candidate = Candidate.model_validate(raw)
    refused, unclipped = [], set()
    for instant in grid + near:
        now, opens, closes = fixture_session(hours, now=instant)
        result = validate(candidate, session_evidence(evidence, now, opens, closes), policy, now)
        if not result.passed:
            refused.append((instant.astimezone(NY).isoformat(), result.failed_rule))
        # Control: the replaced unclipped now ± hours session at the same instant.
        old = session_evidence(evidence, instant, instant - span, instant + span)
        old_result = validate(candidate, old, policy, instant)
        if not old_result.passed:
            assert old_result.failed_rule == "MARKET_SESSION_CLOSED"
            unclipped.add(instant)
    assert refused == []
    # The sweep crosses the whole nightly window where the old fixtures were refused.
    assert unclipped == {i for i in grid + near if i - start < span or end - i <= span}


@pytest.mark.parametrize("hours", [1, 2])
@pytest.mark.parametrize("day", [day for day, _ in DAYS])
def test_fixture_clock_invariants(day, hours):
    grid, near = instants(date.fromisoformat(day))
    span = timedelta(hours=hours)
    for instant in grid + near:
        now = fixture_now(instant)
        own = instant.astimezone(NY).date()
        clear = ny_midnight(own) + GUARD <= instant < ny_midnight(own + timedelta(days=1)) - GUARD
        # The clock only moves forward, and only beside midnight, by at most 2 * GUARD.
        assert (now == instant) is clear
        assert instant <= now <= instant + 2 * GUARD
        assert fixture_now(now) == now
        today = now.astimezone(NY).date()
        start, end = ny_midnight(today), ny_midnight(today + timedelta(days=1))
        assert start + GUARD <= now < end - GUARD
        opens, closes = session_bounds(now, hours)
        assert opens.astimezone(NY).date() == today == closes.astimezone(NY).date()
        assert start + EDGE <= opens < now < closes - FLATTEN
        assert closes <= end - EDGE
        assert now - span <= opens and closes <= now + span


@pytest.mark.parametrize("at", [time(0, 0, 30), time(23, 55), time(23, 58)])
def test_session_bounds_refuse_instants_beside_midnight(at):
    with pytest.raises(ValueError, match="too close to New York midnight"):
        session_bounds(datetime.combine(date(2026, 9, 18), at, NY), 2)


def test_default_fixture_clock_is_real_time_or_slightly_later():
    before = datetime.now(UTC)
    now = fixture_now()
    assert before <= now <= datetime.now(UTC) + 2 * GUARD
