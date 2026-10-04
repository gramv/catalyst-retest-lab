"""``STATS_EXCLUSION_V1``: an audited, append-only record that leaves a New York day's trades
out of the performance statistics (package public-page-v3; owner ruling 2026-10-03, "Exclude
from stats (Recommended)": 2026-10-02's 13 trades were an unmonitored day — another AI ran the
account with no day watch — and are an outlier).

**A measurement definition, never a trading rule, and never a rewrite.** One
``STATS_EXCLUSION`` managed event (no setup) per day, key ``stats-exclusion-v1:<day>``; no trade,
fill, fee or event is changed or removed. The scope is resolved once, when the record is
appended: every non-engineering managed trade (a setup with a buy fill) whose first buy or
whose close falls on the New York ``day``, by setup id. A day with no such trade is refused
(``STATS_EXCLUSION_DAY_UNKNOWN``), a day not yet over is refused
(``STATS_EXCLUSION_DAY_NOT_OVER``) and a day with a trade still open is refused
(``STATS_EXCLUSION_DAY_HAS_OPEN_TRADES``). A repeated command returns the first record.

Readers: the public page's performance statistics (win rate, expectancy, average win and
loss, profit factor, the R histogram, the rules and market comparisons) leave the excluded
trades out; the account's balance, equity curve, drawdown, daily P&L and fees stay real and
include them. The scorecard and the weekly review show both views with the excluded counts.
"""

import re
from datetime import UTC, datetime

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_store import COHORT

EXCLUSION_EVENT = "STATS_EXCLUSION"
EXCLUSION_VERSION = "STATS_EXCLUSION_V1"
REASON = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")
RULING = re.compile(r"^[A-Za-z0-9 .:,;'\"()_/-]{3,200}$")
DEFAULT_RULING = "owner 2026-10-03"


class StatsExclusionRefused(ValueError):
    """A refusal code (``STATS_EXCLUSION_*``)."""


def exclusion_key(day):
    return f"stats-exclusion-v1:{day.isoformat()}"


def resolve_scope(conn, day):
    """``[(setup_id, state_name)]`` of the day's non-engineering managed trades: a buy fill,
    and the first buy or the close on New York ``day``."""
    start, end = day_bounds(day)
    rows = conn.execute(
        """SELECT s.setup_id, s.record_json, t.body->>'state' AS state,
        t.body->>'closed_at' AS closed_at,
        (SELECT min(f.filled_at) FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
         AND f.side='buy') AS entry_at
        FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
        WHERE s.cohort=%s AND EXISTS(SELECT 1 FROM lab.managed_fills f
         WHERE f.setup_id=s.setup_id AND f.side='buy') ORDER BY s.event_seq""", (COHORT,),
    ).fetchall()
    scope = []
    for row in rows:
        if is_engineering(row["record_json"]):
            continue
        closed = row["closed_at"]
        closed_at = datetime.fromisoformat(closed).astimezone(UTC) if closed else None
        entry_at = row["entry_at"].astimezone(UTC) if row["entry_at"] else None
        if any(at is not None and start <= at < end for at in (entry_at, closed_at)):
            scope.append((str(row["setup_id"]), row["state"]))
    return scope


def record_exclusion(store, day, reason, *, now, ruling=DEFAULT_RULING):
    """Append the day's ``STATS_EXCLUSION_V1`` record once; ``{"status", "event_seq", ...}``.
    ``status`` is ``RECORDED`` or ``ALREADY_RECORDED`` (the first record, unchanged)."""
    if not isinstance(reason, str) or not REASON.fullmatch(reason):
        raise StatsExclusionRefused("STATS_EXCLUSION_REASON_INVALID")
    if not isinstance(ruling, str) or not RULING.fullmatch(ruling):
        raise StatsExclusionRefused("STATS_EXCLUSION_RULING_INVALID")
    _, end = day_bounds(day)
    if now.astimezone(UTC) < end:
        raise StatsExclusionRefused("STATS_EXCLUSION_DAY_NOT_OVER")
    with store.transaction() as conn:
        existing = conn.execute(
            "SELECT event_seq, body FROM lab.managed_events WHERE idempotency_key=%s",
            (exclusion_key(day),)).fetchone()
        if existing is not None:
            return {"status": "ALREADY_RECORDED", "event_seq": existing["event_seq"],
                    "day": day.isoformat(), "trades": len(existing["body"]["setup_ids"])}
        scope = resolve_scope(conn, day)
        if not scope:
            raise StatsExclusionRefused("STATS_EXCLUSION_DAY_UNKNOWN")
        if any(state != "CLOSED" for _, state in scope):
            raise StatsExclusionRefused("STATS_EXCLUSION_DAY_HAS_OPEN_TRADES")
        body = {
            "version": EXCLUSION_VERSION, "scope": "NY_DAY_TRADES_ENTERED_OR_CLOSED",
            "day": day.isoformat(), "timezone": "America/New_York",
            "setup_ids": [setup_id for setup_id, _ in scope], "reason": reason,
            "ruling": ruling, "recorded_at": now.astimezone(UTC).isoformat(),
            "effect": "PERFORMANCE_STATISTICS_ONLY", "history": "UNCHANGED",
        }
        row = store.event(conn, EXCLUSION_EVENT, body, key=exclusion_key(day))
    return {"status": "RECORDED", "event_seq": row["event_seq"], "day": day.isoformat(),
            "trades": len(scope)}


def excluded_setups(conn):
    """``{setup_id: {"day", "reason", "event_seq"}}`` over every recorded exclusion."""
    rows = conn.execute(
        """SELECT event_seq, body FROM lab.managed_events WHERE kind=%s AND setup_id IS NULL
        AND body->>'version'=%s ORDER BY event_seq""", (EXCLUSION_EVENT, EXCLUSION_VERSION),
    ).fetchall()
    out = {}
    for row in rows:
        body = row["body"]
        for setup_id in body.get("setup_ids") or []:
            out.setdefault(str(setup_id), {"day": body.get("day"), "reason": body.get("reason"),
                                            "event_seq": row["event_seq"]})
    return out


def excluded_setups_of(repository):
    with repository.connect() as conn:
        return excluded_setups(conn)


__all__ = ["DEFAULT_RULING", "EXCLUSION_EVENT", "EXCLUSION_VERSION", "StatsExclusionRefused",
           "excluded_setups", "excluded_setups_of", "exclusion_key", "record_exclusion",
           "resolve_scope"]
