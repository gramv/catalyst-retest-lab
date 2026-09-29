"""Research run schedule ``RESEARCH_SCHEDULE_V1`` and ``_V2`` (``MANAGED_RESEARCH_SCHEDULE_JSON``).

Owner decision 2026-09-26: research agents report once a day at 08:00 America/New_York;
a second run (for example 20:00) can be switched on. A run's picks may trigger until the
next run's shortlist goes live, so an ``AGENT_RESEARCH_REPORT_V3`` answering the run at
``run_slot`` is valid at most until the next scheduled run after it plus ``grace_minutes``.

Exact JSON: ``{"timezone": "America/New_York", "runs": ["08:00"], "grace_minutes": 60}``.
``timezone`` is an IANA zone; ``runs`` are 1-24 wall-clock ``HH:MM`` times in strictly
ascending order; ``grace_minutes`` (optional, default 60) is an integer from 0 to 720 and
shorter than the smallest gap between consecutive runs, so a report can never stay valid
past the run after next. Anything else refuses startup. A run time that does not exist on a
daylight-saving change day is skipped that day; an ambiguous one uses its first occurrence.

``RESEARCH_SCHEDULE_V2`` (owner direction 2026-09-28 evening, docs/RESEARCH-LOOP-V2.md) is the
same JSON plus ``"daily": "HH:MM"``, one of ``runs``: that run is the full daily run and every
other run is an update run (``run_kind``: ``FULL`` or ``UPDATE``; under V1 every run is
``FULL``). A report answering any run is then valid at most until the next full run after it
plus ``grace_minutes`` (the day's picks live until the next daily run), instead of V1's next
run. Every other rule is V1's; a schedule without ``daily`` is V1, unchanged.

Pure: no clock, database or network access.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from catalyst_lab.jev_contract import strict_json

SCHEDULE_VERSION = "RESEARCH_SCHEDULE_V1"
SCHEDULE_VERSION_V2 = "RESEARCH_SCHEDULE_V2"
FULL_RUN, UPDATE_RUN = "FULL", "UPDATE"
SCHEDULE_ENV = "MANAGED_RESEARCH_SCHEDULE_JSON"
DEFAULT_GRACE_MINUTES = 60
MAX_GRACE_MINUTES = 720
MAX_RUNS = 24
INVALID = "RESEARCH_SCHEDULE_INVALID"
_RUN = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,40}(?:/[A-Za-z0-9_+-]{1,40}){0,2}")
_SEARCH_DAYS = 3  # Wide enough for a run skipped on a daylight-saving change day.


def _minutes(run):
    hours, minutes = _RUN.fullmatch(run).groups()
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class ResearchSchedule:
    timezone: str
    runs: tuple[str, ...]
    grace_minutes: int = DEFAULT_GRACE_MINUTES
    daily: str | None = None  # RESEARCH_SCHEDULE_V2: the full daily run, one of ``runs``.

    def __post_init__(self):
        runs = self.runs
        if isinstance(runs, list):  # JSON arrays arrive as lists; store an immutable tuple.
            runs = tuple(runs)
            object.__setattr__(self, "runs", runs)
        try:
            if not isinstance(self.timezone, str) or not _ZONE.fullmatch(self.timezone):
                raise ValueError
            ZoneInfo(self.timezone)
            if (
                not isinstance(runs, tuple)
                or not 1 <= len(runs) <= MAX_RUNS
                or any(not isinstance(run, str) or not _RUN.fullmatch(run) for run in runs)
            ):
                raise ValueError
            minutes = [_minutes(run) for run in runs]
            if minutes != sorted(set(minutes)):
                raise ValueError  # Strictly ascending: sorted and unique.
            gaps = [b - a for a, b in zip(minutes, minutes[1:], strict=False)]
            gaps.append(minutes[0] + 24 * 60 - minutes[-1])
            if (
                type(self.grace_minutes) is not int
                or not 0 <= self.grace_minutes <= MAX_GRACE_MINUTES
                or self.grace_minutes >= min(gaps)
            ):
                raise ValueError
            if self.daily is not None and (
                not isinstance(self.daily, str) or self.daily not in runs
            ):
                raise ValueError
        except (ValueError, TypeError, ZoneInfoNotFoundError, OSError):
            raise ValueError(INVALID) from None

    @classmethod
    def from_json(cls, text):
        """Strict JSON object with exactly the documented keys; anything else is invalid."""
        try:
            value = strict_json(text)
            if not isinstance(value, dict) or not {"timezone", "runs"} <= set(value):
                raise ValueError
            if not isinstance(value["runs"], list):
                raise ValueError
            if "daily" in value and not isinstance(value["daily"], str):
                raise ValueError  # V2 names its daily run; an explicit null is not V1.
            return cls(**value)
        except (ValueError, TypeError):
            raise ValueError(INVALID) from None

    @classmethod
    def from_env(cls, environ):
        """None when the setting is absent (report V3 then stays unavailable)."""
        text = environ.get(SCHEDULE_ENV)
        return None if text is None else cls.from_json(text)

    @property
    def zone(self):
        return ZoneInfo(self.timezone)

    @property
    def grace(self):
        return timedelta(minutes=self.grace_minutes)

    @property
    def version(self):
        return SCHEDULE_VERSION if self.daily is None else SCHEDULE_VERSION_V2

    def as_dict(self):
        value = {"version": self.version, "timezone": self.timezone,
                 "runs": list(self.runs), "grace_minutes": self.grace_minutes}
        if self.daily is not None:
            value["daily"] = self.daily
        return value

    def run_kind(self, run_slot):
        """``FULL`` for the daily run (every run under V1), ``UPDATE`` for the others."""
        if self.daily is None:
            return FULL_RUN
        wall = _aware(run_slot).astimezone(self.zone).strftime("%H:%M")
        return FULL_RUN if wall == self.daily else UPDATE_RUN

    def _slots(self, start, end):
        """Every scheduled run instant (UTC) in [start, end], ascending."""
        zone = self.zone
        day = start.astimezone(zone).date() - timedelta(days=1)
        last = end.astimezone(zone).date() + timedelta(days=1)
        found = []
        while day <= last:
            for run in self.runs:
                wall = datetime.combine(day, time(*divmod(_minutes(run), 60)))
                local = wall.replace(tzinfo=zone)  # fold=0: the first of an ambiguous pair
                instant = local.astimezone(UTC)
                if instant.astimezone(zone).replace(tzinfo=None) != wall:
                    continue  # This wall time does not exist today (daylight-saving gap).
                if start <= instant <= end:
                    found.append(instant)
            day += timedelta(days=1)
        return sorted(found)

    def is_slot(self, instant):
        instant = _aware(instant)
        return instant in self._slots(instant, instant)

    def next_after(self, instant):
        """The first scheduled run strictly after ``instant``."""
        instant = _aware(instant)
        return next(s for s in self._slots(instant, instant + timedelta(days=_SEARCH_DAYS))
                    if s > instant)

    def latest_at_or_before(self, instant):
        instant = _aware(instant)
        return self._slots(instant - timedelta(days=_SEARCH_DAYS), instant)[-1]

    def next_runs(self, instant, count=None):
        """The next ``count`` runs after ``instant`` (default: one per configured run time)."""
        count = len(self.runs) if count is None else count
        result, cursor = [], _aware(instant)
        for _ in range(count):
            cursor = self.next_after(cursor)
            result.append(cursor)
        return result

    def next_full_after(self, instant):
        """The first full run strictly after ``instant`` (under V1, the next run)."""
        instant = _aware(instant)
        return next(s for s in self._slots(instant, instant + timedelta(days=_SEARCH_DAYS))
                    if s > instant and self.run_kind(s) == FULL_RUN)

    def validity_limit(self, run_slot):
        """The latest ``valid_until`` for a report answering ``run_slot``: the next run plus the
        grace under V1, the next full (daily) run plus the grace under V2."""
        return self.next_full_after(run_slot) + self.grace

    def local(self, instant):
        """``instant`` in the schedule's zone, for display."""
        return _aware(instant).astimezone(self.zone)


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return value.astimezone(UTC)
