"""Wall-clock fixture sessions that never straddle a New York calendar date.

``validation.validate`` requires the official open and close on the same New York date
as ``now`` and ``now`` before the five-minute flatten time, so a ``now ± hours`` session
is refused for part of every night. Pinning fixtures to a fixed past date does not work:
database-side ``clock_timestamp()`` checks (review deadlines, risk-decision expiry)
compare against real time. Fixtures that own their clock therefore step forward, never
back, past a nearby New York midnight, and clip their session inside that date. Reads
that default to real time (e.g. ``managed_measurement`` ``as_of``) must be given the
fixture clock instead.
"""

from datetime import UTC, datetime, time, timedelta

from catalyst_lab.market import NY

EDGE = timedelta(minutes=1)  # Session bounds stay this far inside the New York date.
FLATTEN = timedelta(minutes=5)  # validation.py: no new entries at or after close - 5 min.
GUARD = timedelta(minutes=10)  # Fixture instants keep this distance from NY midnight.


def ny_midnight(day):
    """Start of a New York calendar date as a UTC instant."""
    return datetime.combine(day, time.min, NY).astimezone(UTC)


def fixture_now(now=None):
    """Real time (or ``now``), moved forward past a New York midnight closer than GUARD.

    Moving forward keeps every database-side ``clock_timestamp()`` deadline derived from
    the fixture clock in the future; the shift is at most ``2 * GUARD``.
    """
    now = (datetime.now(UTC) if now is None else now).astimezone(UTC)
    day = now.astimezone(NY).date()
    start, end = ny_midnight(day), ny_midnight(day + timedelta(days=1))
    if now < start + GUARD:
        return start + GUARD
    if now >= end - GUARD:
        return end + GUARD
    return now


def session_bounds(now, hours):
    """Official open/close for ``now ± hours``, clipped inside ``now``'s New York date.

    Both bounds share ``now``'s New York date and ``open < now < close - FLATTEN``.
    """
    now = now.astimezone(UTC)
    day = now.astimezone(NY).date()
    span = timedelta(hours=hours)
    opens = max(now - span, ny_midnight(day) + EDGE)
    closes = min(now + span, ny_midnight(day + timedelta(days=1)) - EDGE)
    if not opens < now < closes - FLATTEN:
        raise ValueError(f"{now.isoformat()} is too close to New York midnight; use fixture_now")
    return opens, closes


def fixture_session(hours, now=None):
    """``(now, open, close)`` for a fixture that owns its clock."""
    now = fixture_now(now)
    return (now, *session_bounds(now, hours))
