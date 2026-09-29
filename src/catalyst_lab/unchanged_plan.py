"""``UNCHANGED_PLAN_REPLAY_V1``: what a trade's *original* stop/target would have done.

Plan ``docs/CRYPTO-AGENT-LOOP.md`` section 4.6.8 (plan phase 7, package results): "For every
stop or target change, every early exit and every continue-or-exit decision, the system also
computes what the unchanged plan would have done on the same prices, so each kind of decision
is measured in R." This is the "managed Jev's maintenance" half of the comparison; section
4.6.6 (the 30% control arm) is the other half, already covered by
``managed_analytics.managed_result_aggregates``'s ``arm`` dimension.

**What "the unchanged plan" means here.** For a detected change, "original" is the setup's
*admitted* stop and target (``lab.managed_setups.record_json.levels`` -- the same immutable
reference ``managed_measurement.official_r`` already uses, so the two R values share a
denominator and are directly comparable). The replay asks: "if none of the maintenance changes
observed up to and including this one had ever been applied -- the position still carried its
original admitted levels at this moment -- what happens from here?" It shares its bar-walk
(stop/target/24-hour-hold, same-bar ambiguity resolved as the stop) with
``pick_outcomes.walk_to_exit``, so a stop-raise and an early-exit are measured exactly the same
way a shadow pick's own exit is.

**Wired to two maintenance mechanisms, unioned per setup (``setup_level_changes``).** A given
setup uses exactly one of these in practice (which one follows from its admission: report-V3
crypto in the ``JEV_MANAGED`` arm uses the second; every other setup that ever gets an
amendment uses the first), but nothing here assumes that -- both are always read and merged in
time order, so this stays correct regardless of which mechanism governs a setup, now or later.

1. **`JEV_MANAGED_EXITS_V1`** (``ManagedExecution.accept_management`` in
   ``managed_execution.py``, pre-dates package maintenance): one ``MANAGEMENT_PLAN_AUTHORIZED``
   event (the new ``stop``/``target``) immediately followed by a ``STATE`` transition carrying
   them. ``stop_target_changes`` reads that pair (the new levels from the event, the old ones
   from the ``STATE`` row just before it) for every setup; a stop or target that only ever moves
   up (never down; the running code already refuses a widening amendment) is a "raise".
2. **`CRYPTO_MAINTENANCE_V1`** (package maintenance, ``trade_maintenance.py``): one
   ``MAINTENANCE_DECISION`` per review, ``outcome: "APPLIED"`` only when a level actually
   changed (``REFUSED``/``HELD``/``FLAGGED``/``FAILED``/``DISCARDED`` "change nothing and need
   no replay" -- the coordinator's own words). ``maintenance_level_changes`` reads the old and
   new levels straight from the decision's own ``levels_before``/``levels_after`` (never the
   setup's current state, which could have moved again since), labelled by its own recorded
   ``action`` (``RAISE_STOP``, ``RAISE_TARGET`` or ``RAISE_STOP_AND_TARGET``).

**Early exit, wired to `EARLY_EXIT_FLAG_V1`** (``exit_flags.py``, package maintenance): an
agreed exit is one ``EXIT_FLAG_RESOLVED`` with ``outcome: "EXIT_AGREED"``.
``maintenance_exit_changes`` reads it together with its own ``EXIT_FLAG_RAISED`` (joined by
``flag_id``): the "original" levels are the flag's own recorded ``evidence.levels`` (the levels
in force at the moment the flag was raised, never re-derived from the setup's state at read
time), ``new_stop``/``new_target`` stay ``None`` and ``actually_exited=True`` -- an exit changes
*when* the trade ends, not the levels the counterfactual should keep riding, so
``replay_unchanged_plan`` needs no change at all to accept it. A raised-but-not-yet-agreed flag,
or one resolved ``EXIT_NOT_AGREED``/``NO_ANSWER_IN_TIME``/``LIFECYCLE_ENDED``, changed nothing
and is not a change to replay.

**Still a hook, not built:** the 24-hour continue-or-exit review (``docs/CRYPTO-AGENT-LOOP.md``
4.6.4, plan phase 6) does not exist in the ledger yet. Once it does, a continue decision that
also raises a level is another stop/target-raise-shaped ``LevelChange``; an exit decided there
is another exit-shaped one (``CONTINUE_EXIT_DECISION``, already named here) -- neither needs a
change to ``replay_unchanged_plan`` or the aggregate/listing functions below, only one more
small reader function alongside ``maintenance_level_changes``/``maintenance_exit_changes``.

**Performance, stated plainly.** Nothing here is persisted: every replay is computed live, on
request, by fetching each change's own (short, bounded) bar window from ``bar_reader``. This
matches ``pick_outcomes``' own read path for a traded pick's real measurement (never frozen into
an event, always read fresh) and keeps this package's footprint small, but it does mean the
listing and aggregate routes below cost one bar fetch per replayed change on every call --
acceptable at today's low review volume for a private operator dashboard, not a design that
scales to a heavily-trafficked or high-change-volume deployment without adding caching.

**`UNCHANGED_PLAN_REPLAY_V2`** (package review-window, 2026-09-28): V1 for a setup that recorded
a window at admission (``CRYPTO_WINDOW_REVIEW_V1`` / ``CRYPTO_WINDOW_HOLD_V1``): the unchanged
plan's own hold ends at the first fill plus that window instead of 24 hours. Everything else is
V1's, and every other setup keeps V1 exactly.

Nothing here places an order, reads a live quote or holds a broker credential.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D
from decimal import InvalidOperation

from catalyst_lab.crypto_holding import recorded_window_seconds
from catalyst_lab.pick_outcomes import (
    BAR_FETCH_BUFFER,
    DATA_INCOMPLETE,
    HOLD_HORIZON,
    TAKER_FEE_TIER1,
    ShadowDataError,
    parse_bars,
    r_values,
    walk_to_exit,
)
from catalyst_lab.repository import json_safe

UNCHANGED_PLAN_METHOD = "UNCHANGED_PLAN_REPLAY_V1"
UNCHANGED_PLAN_WINDOW_METHOD = "UNCHANGED_PLAN_REPLAY_V2"  # Package review-window.
MAINTENANCE_SOURCE = "JEV_MANAGED_EXITS_V1"

STOP_RAISE = "STOP_RAISE"
TARGET_RAISE = "TARGET_RAISE"
STOP_AND_TARGET_RAISE = "STOP_AND_TARGET_RAISE"
# Hook only: no code writes these yet (see the module docstring). Named here so the
# maintenance package's own writer and this module's reader agree on the label from day one.
EARLY_EXIT = "EARLY_EXIT"
CONTINUE_EXIT_DECISION = "CONTINUE_EXIT_DECISION"


@dataclass(frozen=True)
class LevelChange:
    """One point where a setup's plan diverged from what it would otherwise have kept doing."""

    at: datetime
    change_kind: str
    old_stop: D
    old_target: D
    new_stop: D | None = None
    new_target: D | None = None
    actually_exited: bool = False
    source_event_seq: int | None = None
    source_kind: str | None = None

    def __post_init__(self):
        if self.at.tzinfo is None:
            raise ShadowDataError("AWARE_CHANGE_TIMESTAMP_REQUIRED")
        if not 0 < self.old_stop < self.old_target:
            raise ShadowDataError("INVALID_ORIGINAL_LEVELS")


@dataclass(frozen=True)
class UnchangedPlanOutcome:
    method: str
    change_kind: str
    change_at: datetime
    source_event_seq: int | None
    original_stop: D
    original_target: D
    exit_reason: str
    exit_price: D | None
    exit_at: datetime | None
    same_bar_ambiguous: bool
    data_complete: bool
    unchanged_gross_r: D | None
    unchanged_net_r: D | None
    actual_r: D | None
    r_difference: D | None
    bars_examined: int
    limitations: tuple

    def to_dict(self):
        return json_safe({
            "method": self.method, "change_kind": self.change_kind, "change_at": self.change_at,
            "source_event_seq": self.source_event_seq, "original_stop": self.original_stop,
            "original_target": self.original_target, "exit_reason": self.exit_reason,
            "exit_price": self.exit_price, "exit_at": self.exit_at,
            "same_bar_ambiguous": self.same_bar_ambiguous, "data_complete": self.data_complete,
            "unchanged_gross_r": self.unchanged_gross_r, "unchanged_net_r": self.unchanged_net_r,
            "actual_r": self.actual_r, "r_difference": self.r_difference,
            "bars_examined": self.bars_examined, "limitations": list(self.limitations),
        })


def replay_unchanged_plan(entry_price, change, bars_from_change, *, hold_deadline,
                          actual_r=None, fee_rate=TAKER_FEE_TIER1, method=UNCHANGED_PLAN_METHOD):
    """What would have happened from ``change.at`` had ``change`` never been applied.

    Keeps ``change.old_stop``/``change.old_target`` (never moves, never exits early) and walks
    ``bars_from_change`` (ascending, at or after ``change.at``) to their own stop/target/24-hour
    exit -- the same rule ``pick_outcomes.simulate_pick`` uses after a trigger. ``actual_r``
    (typically the trade's real ``official_r``, verified fees and all) is compared against the
    counterfactual's own *assumed*-fee net R; that fee-basis mismatch is unavoidable for a trade
    that never happened and is named in ``limitations``, not hidden.

    ``entry_price``, in the same denominator convention as ``official_r``, should be the
    setup's *admitted* max entry price, not its actual average fill. ``method``
    ``UNCHANGED_PLAN_REPLAY_V2``: ``hold_deadline`` is the first fill plus the setup's window.
    """
    walk = walk_to_exit(change.old_stop, change.old_target, bars_from_change,
                        hold_deadline=hold_deadline)
    limitations = []
    if walk.ambiguous:
        limitations.append(
            "A bar reached both the original stop and the original target in the same minute; "
            "resolved as the stop (conservative), and counted."
        )
    if walk.reason == DATA_INCOMPLETE:
        gross = net = None
        limitations.append(
            "Fetched bars end before the unchanged plan's own 24-hour hold deadline; its "
            "outcome is not yet known." if method == UNCHANGED_PLAN_METHOD else
            "Fetched bars end before the unchanged plan's own hold deadline (the setup's "
            "window); its outcome is not yet known."
        )
    else:
        gross, net = r_values(entry_price, change.old_stop, walk.price, fee_rate=fee_rate)
        limitations.append(
            "unchanged_net_r assumes Alpaca's tier-1 taker fee on both legs (see "
            "pick_outcomes.FEE_ASSUMPTION); actual_r, when supplied, is the trade's own "
            "verified fee evidence. The two R values are not on an identical fee basis."
        )
    difference = None if actual_r is None or net is None else actual_r - net
    return UnchangedPlanOutcome(
        method=method, change_kind=change.change_kind, change_at=change.at,
        source_event_seq=change.source_event_seq, original_stop=change.old_stop,
        original_target=change.old_target, exit_reason=walk.reason, exit_price=walk.price,
        exit_at=walk.at, same_bar_ambiguous=walk.ambiguous,
        data_complete=walk.reason != DATA_INCOMPLETE, unchanged_gross_r=gross,
        unchanged_net_r=net, actual_r=actual_r, r_difference=difference,
        bars_examined=walk.bars_examined, limitations=tuple(limitations),
    )


# --- Reading today's maintenance mechanism (read-only) -------------------------------------------


def stop_target_changes(repository, setup_id):
    """Every stop/target raise of one setup, from ``MANAGEMENT_PLAN_AUTHORIZED`` (today's only
    source -- see the module docstring's hook for kinds a later package will add).
    """
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT e.event_seq, e.body, e.recorded_at,
            (SELECT p.body FROM lab.managed_events p WHERE p.setup_id=e.setup_id
             AND p.kind='STATE' AND p.event_seq<e.event_seq
             ORDER BY p.event_seq DESC LIMIT 1) AS previous_state
            FROM lab.managed_events e WHERE e.setup_id=%s AND e.kind='MANAGEMENT_PLAN_AUTHORIZED'
            ORDER BY e.event_seq""",
            (setup_id,),
        ).fetchall()
    changes = []
    for row in rows:
        previous = row["previous_state"] or {}
        try:
            old_stop, old_target = D(str(previous["stop"])), D(str(previous["target"]))
            new_stop, new_target = D(str(row["body"]["stop"])), D(str(row["body"]["target"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue  # No usable prior STATE row: nothing to compare, fail closed (skip it).
        stop_raised, target_raised = new_stop > old_stop, new_target > old_target
        if not stop_raised and not target_raised:
            continue
        kind = (STOP_AND_TARGET_RAISE if stop_raised and target_raised
                else STOP_RAISE if stop_raised else TARGET_RAISE)
        changes.append(LevelChange(
            at=row["recorded_at"], change_kind=kind, old_stop=old_stop, old_target=old_target,
            new_stop=new_stop, new_target=new_target, source_event_seq=row["event_seq"],
            source_kind="MANAGEMENT_PLAN_AUTHORIZED",
        ))
    return changes


def maintenance_level_changes(repository, setup_id):
    """Every applied stop/target raise of one setup, from ``MAINTENANCE_DECISION``
    (``CRYPTO_MAINTENANCE_V1``/``_V2``, packages maintenance and answer-rules) -- old and new
    levels read from the decision's own recorded ``levels_before``/``levels_after``, never the
    setup's current state. Only ``outcome: "APPLIED"`` changed anything; every other outcome is
    skipped. The change kind is the levels the decision actually moved: under V1 exactly the
    ones its action named; under V2 a RAISE_STOP_AND_TARGET whose stop (or target) answer was
    not usable moved only the other level, and is a target (or stop) raise.
    """
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT event_seq, body FROM lab.managed_events
            WHERE setup_id=%s AND kind='MAINTENANCE_DECISION' AND body->>'outcome'='APPLIED'
            ORDER BY event_seq""",
            (setup_id,),
        ).fetchall()
    raises = {"RAISE_STOP", "RAISE_TARGET", "RAISE_STOP_AND_TARGET"}
    changes = []
    for row in rows:
        body = row["body"]
        if body.get("action") not in raises:
            continue  # Defensive: an APPLIED decision's action is always one of the three.
        try:
            old_stop = D(str(body["levels_before"]["stop"]))
            old_target = D(str(body["levels_before"]["target"]))
            new_stop = D(str(body["levels_after"]["stop"]))
            new_target = D(str(body["levels_after"]["target"]))
            at = datetime.fromisoformat(body["decided_at"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue  # Malformed decision body: fail closed, never guess levels.
        stop_raised, target_raised = new_stop != old_stop, new_target != old_target
        if not stop_raised and not target_raised:
            continue  # Defensive: an APPLIED decision always moved a level.
        kind = (STOP_AND_TARGET_RAISE if stop_raised and target_raised
                else STOP_RAISE if stop_raised else TARGET_RAISE)
        changes.append(LevelChange(
            at=at, change_kind=kind, old_stop=old_stop, old_target=old_target,
            new_stop=new_stop, new_target=new_target, source_event_seq=row["event_seq"],
            source_kind="MAINTENANCE_DECISION",
        ))
    return changes


def maintenance_exit_changes(repository, setup_id):
    """Every agreed early exit of one setup (``EXIT_FLAG_RESOLVED`` ``EXIT_AGREED``,
    ``EARLY_EXIT_FLAG_V1``, package maintenance) -- the "original" levels are the flag's own
    recorded ``evidence.levels`` at the moment it was raised, never the setup's state at read
    time. A flag resolved any other way (``EXIT_NOT_AGREED``, ``NO_ANSWER_IN_TIME``,
    ``LIFECYCLE_ENDED``) or still pending changed nothing and is not returned.
    """
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT r.event_seq, r.body AS resolution, f.body AS flag
            FROM lab.managed_events r
            JOIN lab.managed_events f ON f.kind='EXIT_FLAG_RAISED' AND f.setup_id=r.setup_id
              AND f.body->>'flag_id'=r.body->>'flag_id'
            WHERE r.setup_id=%s AND r.kind='EXIT_FLAG_RESOLVED'
              AND r.body->>'outcome'='EXIT_AGREED'
            ORDER BY r.event_seq""",
            (setup_id,),
        ).fetchall()
    changes = []
    for row in rows:
        resolution, flag = row["resolution"], row["flag"]
        try:
            levels = flag["evidence"]["levels"]
            old_stop = D(str(levels["stop"]))
            old_target = D(str(levels["target"]))
            at = datetime.fromisoformat(resolution["resolved_at"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue  # Malformed flag/resolution body: fail closed, never guess levels.
        changes.append(LevelChange(
            at=at, change_kind=EARLY_EXIT, old_stop=old_stop, old_target=old_target,
            new_stop=None, new_target=None, actually_exited=True,
            source_event_seq=row["event_seq"], source_kind="EXIT_FLAG_RESOLVED",
        ))
    return changes


def setup_level_changes(repository, setup_id):
    """Every change this package can currently replay for one setup, in time order: legacy
    ``JEV_MANAGED_EXITS_V1`` amendments, applied ``CRYPTO_MAINTENANCE_V1`` raises and agreed
    early exits. In practice one setup uses at most one of the first two sources (which one
    follows from how it was admitted), but nothing here assumes that.
    """
    changes = (
        stop_target_changes(repository, setup_id)
        + maintenance_level_changes(repository, setup_id)
        + maintenance_exit_changes(repository, setup_id)
    )
    changes.sort(key=lambda change: change.at)
    return changes


def unchanged_plan_comparisons(repository, bar_reader, setup_id, *, now):
    """Every detected change of ``setup_id`` (``setup_level_changes``), replayed against the
    same minute bars.

    Read-only: reads the setup's admitted levels, its first fill (the 24-hour hold's own
    anchor), its recorded window (``UNCHANGED_PLAN_REPLAY_V2``: the hold is the window) and its
    current ``managed_measurement`` for ``actual_r``; fetches bars from ``bar_reader``
    (``public_crypto_bars.PublicCryptoBarReader`` in production, a fixture in tests) only for
    the windows each change actually needs.
    """
    from catalyst_lab.managed_measurement import managed_measurement

    with repository.connect() as conn:
        setup = conn.execute(
            """SELECT s.symbol, s.record_json, t.body AS state FROM lab.managed_setups s
            LEFT JOIN lab.managed_states t USING(setup_id) WHERE s.setup_id=%s""", (setup_id,)
        ).fetchone()
        if not setup:
            raise ValueError("SETUP_NOT_FOUND")
        first_fill = conn.execute(
            """SELECT min(filled_at) AS at FROM lab.managed_fills
            WHERE setup_id=%s AND side='buy'""", (setup_id,),
        ).fetchone()["at"]
    if first_fill is None:
        return []  # Never entered: no maintenance to have changed anything about.
    levels = setup["record_json"]["levels"]
    entry_price = D(str(levels["max_entry_price"]))
    window = recorded_window_seconds(setup["state"])
    method = UNCHANGED_PLAN_METHOD if window is None else UNCHANGED_PLAN_WINDOW_METHOD
    hold_deadline = first_fill + (HOLD_HORIZON if window is None else timedelta(seconds=window))
    measurement = managed_measurement(repository, setup_id)
    actual_r = (
        D(str(measurement["official_r"])) if measurement["official_r"] is not None else None
    )
    results = []
    for change in setup_level_changes(repository, setup_id):
        # The fetch window must reach a little past hold_deadline itself, or the one bar
        # walk_to_exit needs to price a HOLD_24H_EXIT (the first bar AT OR AFTER the deadline)
        # can never be fetched (see pick_outcomes.BAR_FETCH_BUFFER); bounded by ``now`` so a
        # deadline not yet reached in real time correctly yields DATA_INCOMPLETE, not a guess.
        end = min(hold_deadline + BAR_FETCH_BUFFER, now)
        if end <= change.at:
            continue  # Not yet enough elapsed time to fetch anything meaningful.
        try:
            bars = parse_bars(bar_reader.minute_bars(setup["symbol"], change.at, end))
        except ShadowDataError:
            continue
        results.append(replay_unchanged_plan(
            entry_price, change, bars, hold_deadline=hold_deadline, actual_r=actual_r,
            method=method,
        ))
    return results


# --- Listing and aggregating maintenance replays across every setup (read-only) -----------------


def setups_with_level_changes(repository, *, setup_id=None, after_event_seq=0, limit=100):
    """Ascending page of setups with at least one replayable change (any of the three sources),
    one row per setup (``setup_id``, ``symbol``, ``arm``, ``agent_id``, and ``event_seq`` = the
    latest such change's own sequence, the pagination cursor) -- what the maintenance-replay
    listing and aggregate below paginate over, not per change. ``setup_id`` restricts to one
    trade (the "per trade" view); its own cursor still applies.
    """
    if type(after_event_seq) is not int or after_event_seq < 0 or (
        type(limit) is not int or not 1 <= limit <= 500
    ):
        raise ValueError("INVALID_MAINTENANCE_REPLAY_CURSOR")
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT s.setup_id, s.symbol, t.body->>'arm' AS arm,
            s.record_json->'agent'->>'agent_id' AS agent_id, max(e.event_seq) AS event_seq
            FROM lab.managed_events e
            JOIN lab.managed_setups s ON s.setup_id=e.setup_id
            LEFT JOIN lab.managed_states t ON t.setup_id=s.setup_id
            WHERE ((e.kind='MANAGEMENT_PLAN_AUTHORIZED')
               OR (e.kind='MAINTENANCE_DECISION' AND e.body->>'outcome'='APPLIED')
               OR (e.kind='EXIT_FLAG_RESOLVED' AND e.body->>'outcome'='EXIT_AGREED'))
              AND (%(setup)s::uuid IS NULL OR s.setup_id=%(setup)s::uuid)
            GROUP BY s.setup_id, s.symbol, t.body->>'arm', s.record_json->'agent'->>'agent_id'
            HAVING max(e.event_seq) > %(after)s
            ORDER BY max(e.event_seq) LIMIT %(limit)s""",
            {"setup": str(setup_id) if setup_id is not None else None,
             "after": after_event_seq, "limit": limit},
        ).fetchall()
    return rows


def maintenance_replay_rows(repository, bar_reader, *, now, setup_id=None, after_event_seq=0,
                            limit=50):
    """Every change of up to ``limit`` setups with at least one (or of one setup, with
    ``setup_id``), replayed; each row also carries its setup's
    ``setup_id``/``symbol``/``arm``/``agent_id``. ``(rows, next_cursor)``, the cursor over
    setups (see ``setups_with_level_changes``), not over rows.
    """
    setups = setups_with_level_changes(repository, setup_id=setup_id,
                                       after_event_seq=after_event_seq, limit=limit)
    rows = []
    for setup in setups:
        for outcome in unchanged_plan_comparisons(repository, bar_reader, setup["setup_id"],
                                                   now=now):
            rows.append({
                "setup_id": str(setup["setup_id"]), "symbol": setup["symbol"],
                "arm": setup["arm"], "agent_id": setup["agent_id"], "replay": outcome.to_dict(),
            })
    next_cursor = setups[-1]["event_seq"] if setups else after_event_seq
    return rows, next_cursor


def maintenance_replay_page(repository, bar_reader, *, now, setup_id=None, after_event_seq=0,
                            limit=50):
    """Package results, plan 4.6.8 ("Measuring maintenance"): per trade, each change and what
    the unchanged plan would have done (``setup_id`` narrows to one trade). See the module
    docstring's "Performance" note: computed live, one bar fetch per replayed change, every call.
    """
    rows, next_cursor = maintenance_replay_rows(
        repository, bar_reader, now=now, setup_id=setup_id, after_event_seq=after_event_seq,
        limit=limit,
    )
    return json_safe({"method": UNCHANGED_PLAN_METHOD, "items": rows, "next_cursor": next_cursor})


MAINTENANCE_AGGREGATE_VERSION = "MAINTENANCE_REPLAY_AGGREGATES_V1"
MAINTENANCE_AGGREGATE_DIMENSIONS = ("change_kind", "arm", "agent_id")


def maintenance_replay_aggregates(repository, bar_reader, *, now,
                                  group_by=("change_kind", "arm"), limit=500):
    """Totals by change kind and arm (plan 4.6.8), from every change of up to ``limit`` setups
    with at least one. Per group: ``count`` (every replayed change, whatever its data
    completeness); ``r_difference_count``, ``mean_r_difference`` and ``helped_rate`` (the share
    with a positive difference) from only the changes with a known ``r_difference``. A
    statistic with no known input is ``null``, never a default of zero -- counts always shown.
    """
    if not group_by or not set(group_by) <= set(MAINTENANCE_AGGREGATE_DIMENSIONS):
        raise ValueError("INVALID_MAINTENANCE_AGGREGATE_DIMENSIONS")
    rows, _ = maintenance_replay_rows(repository, bar_reader, now=now, limit=limit)
    groups = {}
    for row in rows:
        replay = row["replay"]
        dims = {"change_kind": replay["change_kind"], "arm": row["arm"],
                "agent_id": row["agent_id"]}
        key = tuple(dims[k] for k in group_by)
        group = groups.setdefault(key, {
            "dims": {k: dims[k] for k in group_by}, "count": 0,
            "r_diff_count": 0, "r_diff_sum": D(0), "helped_count": 0,
        })
        group["count"] += 1
        if replay["r_difference"] is not None:
            diff = D(str(replay["r_difference"]))
            group["r_diff_count"] += 1
            group["r_diff_sum"] += diff
            group["helped_count"] += diff > 0
    items = []
    for group in groups.values():
        items.append({
            **group["dims"], "count": group["count"],
            "r_difference_count": group["r_diff_count"],
            "mean_r_difference": (group["r_diff_sum"] / group["r_diff_count"])
            if group["r_diff_count"] else None,
            "helped_rate": (group["helped_count"] / group["r_diff_count"])
            if group["r_diff_count"] else None,
        })
    items.sort(key=lambda item: [str(item[k]) for k in group_by])
    return json_safe({
        "aggregate_version": MAINTENANCE_AGGREGATE_VERSION, "group_by": list(group_by),
        "items": items, "setups_considered": len({row["setup_id"] for row in rows}),
    })


__all__ = [
    "CONTINUE_EXIT_DECISION", "EARLY_EXIT", "LevelChange", "MAINTENANCE_AGGREGATE_DIMENSIONS",
    "MAINTENANCE_AGGREGATE_VERSION", "MAINTENANCE_SOURCE", "STOP_AND_TARGET_RAISE", "STOP_RAISE",
    "TARGET_RAISE", "UNCHANGED_PLAN_METHOD", "UnchangedPlanOutcome", "maintenance_exit_changes",
    "maintenance_level_changes", "maintenance_replay_aggregates", "maintenance_replay_page",
    "maintenance_replay_rows", "replay_unchanged_plan", "setup_level_changes",
    "setups_with_level_changes", "stop_target_changes", "unchanged_plan_comparisons",
]
