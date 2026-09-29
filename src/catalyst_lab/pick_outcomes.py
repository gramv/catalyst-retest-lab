"""``PICK_SHADOW_OUTCOME_V1``: what every report-V3 pick would have done, on Alpaca's own
public crypto minute bars, fetched after the fact.

Plan ``docs/CRYPTO-AGENT-LOOP.md`` section 4.8 (plan phase 7, package results): "Every pick of
every run is tracked on live Alpaca prices -- selected, not selected, rejected by the check --
so Jev's picks can be compared with the ones it passed on." This module is pure simulation and
read-only ledger reconstruction; it places no order and holds no broker credential. Building
the shadow record (``run_shadow_outcome_job`` in this module, driven by
``scripts/run_pick_shadow_outcomes.py``) is a deterministic offline job: it fetches Alpaca's
public ``v1beta3`` crypto bars (no key needed -- see ``public_crypto_bars.py``) once a pick's
relevant window has fully elapsed, and appends one immutable ``PICK_SHADOW_OUTCOME`` event per
pick. It never reads a live quote and never subscribes to a stream.

**Method, plainly (a 1-minute-bar approximation, not tick or quote data):**

* A pick's window is ``[report generated_at, its own packet expiry)`` -- the same window for
  every pick of a run, selected or not, so the comparison is fair. This is *not* the real
  system's actual WATCHING window for an admitted setup (which starts later, once Jev and the
  system check finish); the shadow model asks "what if this pick had been live from the moment
  it was proposed," deliberately applying one rule to every pick alike.
* **Touch** = a bar's low at or below a level (never the bar's open/close, and never an
  intra-bar path). The entry trigger is checked before the stop, so a bar that reaches down to
  the stop is read as "stop before (or without) a valid trigger" even though, mechanically, a
  low that reaches the stop also passes through the (higher) entry trigger -- this mirrors the
  running system's own "stop invalidation takes priority before confirmation."
* Once triggered, the assumed fill price is the **max entry price** (a limit order there is
  never filled worse than that -- the same assumption the running system's entry order makes).
* After the fill: the first bar whose low is at or below the stop or whose high is at or above
  the target decides the exit. A bar that reaches **both** in the same minute is an unavoidable
  ambiguity from OHLC data alone; it is resolved conservatively as the **stop**, and counted
  (``same_bar_ambiguous``) rather than silently averaged away.
* Neither hit by 24 hours after the fill (``CRYPTO_24H_HOLD_V1``'s own hold horizon, since
  report V3 is crypto-only): the shadow position exits at that mark, priced at the first bar's
  open at or after the deadline.
* Bars run out before the pick's window, or before 24 hours past a fill, close: the outcome is
  ``DATA_INCOMPLETE`` -- fail-closed. No exit is invented from a gap in Alpaca's own history.
* **R** is computed at the max entry price, exactly as ``official_r`` (owner ruling R5): R = P&L
  per unit / (max entry - stop), so it needs no assumed trade size. ``net_r`` additionally
  assumes Alpaca's crypto tier-1 **taker** fee (0.25%) on both legs (``FEE_ASSUMPTION_V1``) --
  stated explicitly because it is an assumption, not read from any account; a pick that actually
  traded reports its own verified ``managed_measurement`` figures alongside the shadow ones,
  and those, not this assumption, are the real economics of that trade.

Only intake-*accepted* picks (those with a recorded ``RESEARCH_PACKET``, i.e. valid schema and
levels) are simulated; a pick rejected at intake never had usable levels to simulate.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from decimal import InvalidOperation

from catalyst_lab.agent_identity import agent_fields
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_store import COHORT
from catalyst_lab.repository import json_safe
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from catalyst_lab.research_selection_topk import (
    NOT_RANKED,
    RANKED,
    RANKING_KIND,
    REPLACEMENT_KIND,
    SKIPPED_KIND,
    VETOED,
    ranking_key_for,
)

SHADOW_METHOD_VERSION = "PICK_SHADOW_OUTCOME_V1"
SHADOW_EVENT_KIND = "PICK_SHADOW_OUTCOME"
METHOD_LABEL = (
    "1-MINUTE-BAR APPROXIMATION: touch is a bar's low at or below a level, never an intra-bar "
    "path; a same-bar stop-and-target ambiguity is resolved conservatively as the stop. This is "
    "not a fill, a quote, or a tick-level reconstruction."
)
HOLD_HORIZON = timedelta(hours=24)  # CRYPTO_24H_HOLD_V1's own horizon; report V3 is crypto only.
# walk_to_exit's HOLD_24H_EXIT price is the open of the first bar AT OR AFTER hold_deadline; a
# bar-reader fetch window is exclusive of its own ``end``, so every caller that fetches exactly
# up to a hold deadline must ask a little past it, or that boundary bar can never be seen and
# the walk is always DATA_INCOMPLETE right at the deadline. A few minutes covers any ordinary
# gap in a live feed without materially widening what "at the deadline" means.
BAR_FETCH_BUFFER = timedelta(minutes=5)

# Fee assumption (stated plainly; no better evidence than Alpaca's published tier-1 schedule is
# available to an offline job that never opens the owner's account). A pick that actually traded
# reports its own verified fee evidence (managed_measurement) alongside this assumption.
TAKER_FEE_TIER1 = D("0.0025")
FEE_ASSUMPTION_VERSION = "ALPACA_CRYPTO_TIER1_TAKER_BOTH_LEGS_V1"
FEE_ASSUMPTION = {
    "version": FEE_ASSUMPTION_VERSION,
    "entry_fee_rate": str(TAKER_FEE_TIER1),
    "exit_fee_rate": str(TAKER_FEE_TIER1),
    "basis": "Alpaca crypto tier-1 taker fee (0.25%), assumed on both the entry and the exit "
             "leg; not read from any account. A pick that actually traded carries its own "
             "verified fee evidence separately (managed_measurement.verified_cash_fee_usd).",
}

# Outcome classification (top-level; every shadow record has exactly one).
NEVER_TRIGGERED_STOP_FIRST = "NEVER_TRIGGERED_STOP_FIRST"
NEVER_TRIGGERED_EXPIRED = "NEVER_TRIGGERED_VALIDITY_EXPIRED"
STOP = "STOP"
TARGET = "TARGET"
HOLD_EXIT = "HOLD_24H_EXIT"
DATA_INCOMPLETE = "DATA_INCOMPLETE"
NEVER_TRIGGERED = frozenset({NEVER_TRIGGERED_STOP_FIRST, NEVER_TRIGGERED_EXPIRED})
TRADED_OUTCOMES = frozenset({STOP, TARGET, HOLD_EXIT})

D0 = D(0)


class ShadowDataError(ValueError):
    """Malformed bar evidence; the caller keeps the pick's outcome unknown (fail-closed)."""


# --- Pure bar simulation ------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: D
    high: D
    low: D
    close: D
    volume: D

    def __post_init__(self):
        if self.start.tzinfo is None:
            raise ShadowDataError("AWARE_BAR_TIMESTAMP_REQUIRED")
        values = (self.open, self.high, self.low, self.close)
        if any(not isinstance(v, D) or not v.is_finite() or v <= 0 for v in values):
            raise ShadowDataError("INVALID_BAR_PRICE")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ShadowDataError("INVALID_BAR_RANGE")
        if not isinstance(self.volume, D) or not self.volume.is_finite() or self.volume < 0:
            raise ShadowDataError("INVALID_BAR_VOLUME")


def parse_bars(rows):
    """Ascending, strictly-increasing-start ``Bar`` tuples from raw Alpaca bar rows.

    Raises ``ShadowDataError`` on the first malformed row: a bad bar is never silently
    dropped from a walk that decides a stop or a target (fail-closed).
    """
    bars = []
    for row in rows:
        try:
            start = row["t"] if isinstance(row["t"], datetime) else datetime.fromisoformat(
                str(row["t"]).replace("Z", "+00:00")
            )
            if start.tzinfo is None:
                raise ShadowDataError("AWARE_BAR_TIMESTAMP_REQUIRED")
            bar = Bar(
                start=start.astimezone(UTC),
                open=D(str(row["o"])), high=D(str(row["h"])),
                low=D(str(row["l"])), close=D(str(row["c"])),
                volume=D(str(row.get("v", "0"))),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ShadowDataError("INVALID_BAR_ROW") from exc
        bars.append(bar)
    bars.sort(key=lambda b: b.start)
    for earlier, later in zip(bars, bars[1:], strict=False):
        if later.start <= earlier.start:
            raise ShadowDataError("DUPLICATE_OR_UNORDERED_BAR")
    return bars


@dataclass(frozen=True)
class ExitWalk:
    """The result of walking bars forward from a known price to its first stop/target/hold exit.

    ``reason`` is one of ``STOP``, ``TARGET``, ``HOLD_24H_EXIT`` or ``DATA_INCOMPLETE`` (bars ran
    out before the hold deadline with neither level hit -- the outcome is not yet knowable from
    what was fetched). ``price``/``at`` are ``None`` only for ``DATA_INCOMPLETE``.
    """

    reason: str
    price: D | None
    at: datetime | None
    ambiguous: bool
    bars_examined: int
    last_bar_at: datetime | None


def walk_to_exit(stop, target, bars, *, hold_deadline):
    """First stop/target hit in ``bars`` (ascending, at or after the fill), else a 24-hour exit.

    Shared by the shadow pick simulation and the unchanged-plan replay (``unchanged_plan.py``):
    both are "starting now, with these levels, on these bars, what happens?"
    """
    examined, last_at = 0, None
    for bar in bars:
        if bar.start >= hold_deadline:
            break
        examined += 1
        last_at = bar.start
        hit_stop, hit_target = bar.low <= stop, bar.high >= target
        if hit_stop and hit_target:
            return ExitWalk(STOP, stop, bar.start, True, examined, last_at)
        if hit_stop:
            return ExitWalk(STOP, stop, bar.start, False, examined, last_at)
        if hit_target:
            return ExitWalk(TARGET, target, bar.start, False, examined, last_at)
    at_or_after_deadline = [b for b in bars if b.start >= hold_deadline]
    if at_or_after_deadline:
        exit_bar = at_or_after_deadline[0]
        return ExitWalk(HOLD_EXIT, exit_bar.open, hold_deadline, False, examined, last_at)
    return ExitWalk(DATA_INCOMPLETE, None, None, False, examined, last_at)


def r_values(entry_price, stop, exit_price, *, fee_rate=TAKER_FEE_TIER1):
    """Gross and (assumed-fee) net R, priced at ``entry_price`` -- the official-R convention
    (P&L per unit / (entry - stop)), so no trade-size assumption is needed."""
    risk = entry_price - stop
    if risk <= 0:
        raise ShadowDataError("NONPOSITIVE_RISK_DENOMINATOR")
    gross_r = (exit_price - entry_price) / risk
    fee_r = fee_rate * (entry_price + exit_price) / risk
    return gross_r, gross_r - fee_r


# --- One pick's full simulation -----------------------------------------------------------------


@dataclass(frozen=True)
class PickSimulation:
    method: str
    fee_assumption: dict
    outcome: str
    triggered: bool
    trigger_at: datetime | None
    fill_price: D | None
    exit_reason: str | None
    exit_price: D | None
    exit_at: datetime | None
    same_bar_ambiguous: bool
    gross_r: D | None
    net_r: D | None
    data_complete: bool
    bars_examined: int
    window_start: datetime
    window_end: datetime
    hold_deadline: D | None
    first_bar_at: datetime | None
    last_bar_at: datetime | None
    limitations: tuple

    def to_dict(self):
        return json_safe({
            "method": self.method, "method_label": METHOD_LABEL,
            "fee_assumption": self.fee_assumption, "outcome": self.outcome,
            "triggered": self.triggered, "trigger_at": self.trigger_at,
            "fill_price": self.fill_price, "exit_reason": self.exit_reason,
            "exit_price": self.exit_price, "exit_at": self.exit_at,
            "same_bar_ambiguous": self.same_bar_ambiguous,
            "gross_r": self.gross_r, "net_r": self.net_r, "data_complete": self.data_complete,
            "bars_examined": self.bars_examined, "window_start": self.window_start,
            "window_end": self.window_end, "hold_deadline": self.hold_deadline,
            "first_bar_at": self.first_bar_at, "last_bar_at": self.last_bar_at,
            "limitations": list(self.limitations),
        })


def simulate_pick(levels, bars, *, window_start, window_end, hold=HOLD_HORIZON,
                  fee_rate=TAKER_FEE_TIER1):
    """The shadow outcome of one pick's levels over ``bars``; pure, no ledger or network access.

    ``levels`` needs ``entry_trigger``, ``max_entry_price``, ``stop`` and ``target`` (numbers or
    numeric strings). ``bars`` need not be pre-filtered to the window; only ascending order and
    validity (``parse_bars``) are assumed.
    """
    entry_trigger, max_entry, stop, target = (
        D(str(levels[k])) for k in ("entry_trigger", "max_entry_price", "stop", "target")
    )
    if not stop < entry_trigger <= max_entry < target:
        raise ShadowDataError("INVALID_PICK_LEVELS")
    limitations = [METHOD_LABEL]
    considered = [b for b in bars if window_start <= b.start < window_end]
    trigger_bar, first_at = None, considered[0].start if considered else None
    outcome = NEVER_TRIGGERED_EXPIRED
    for bar in considered:
        if bar.low <= stop:
            outcome = NEVER_TRIGGERED_STOP_FIRST
            break
        if bar.low <= entry_trigger:
            trigger_bar = bar
            break
    if trigger_bar is None:
        return PickSimulation(
            method=SHADOW_METHOD_VERSION, fee_assumption=FEE_ASSUMPTION, outcome=outcome,
            triggered=False, trigger_at=None, fill_price=None, exit_reason=None, exit_price=None,
            exit_at=None, same_bar_ambiguous=False, gross_r=None, net_r=None,
            data_complete=True, bars_examined=len(considered), window_start=window_start,
            window_end=window_end, hold_deadline=None, first_bar_at=first_at,
            last_bar_at=considered[-1].start if considered else None,
            limitations=tuple(limitations),
        )
    hold_deadline = trigger_bar.start + hold
    after = [b for b in bars if b.start >= trigger_bar.start]
    walk = walk_to_exit(stop, target, after, hold_deadline=hold_deadline)
    if walk.ambiguous:
        limitations.append(
            "A bar reached both the stop and the target in the same minute; resolved as the "
            "stop (conservative), and counted."
        )
    if walk.reason == DATA_INCOMPLETE:
        limitations.append(
            "Fetched bars end before the 24-hour hold deadline; the outcome is not yet known."
        )
        return PickSimulation(
            method=SHADOW_METHOD_VERSION, fee_assumption=FEE_ASSUMPTION, outcome=DATA_INCOMPLETE,
            triggered=True, trigger_at=trigger_bar.start, fill_price=max_entry, exit_reason=None,
            exit_price=None, exit_at=None, same_bar_ambiguous=False, gross_r=None, net_r=None,
            data_complete=False, bars_examined=len(considered) + walk.bars_examined,
            window_start=window_start, window_end=window_end, hold_deadline=hold_deadline,
            first_bar_at=first_at, last_bar_at=walk.last_bar_at,
            limitations=tuple(limitations),
        )
    gross_r, net_r = r_values(max_entry, stop, walk.price, fee_rate=fee_rate)
    return PickSimulation(
        method=SHADOW_METHOD_VERSION, fee_assumption=FEE_ASSUMPTION, outcome=walk.reason,
        triggered=True, trigger_at=trigger_bar.start, fill_price=max_entry,
        exit_reason=walk.reason, exit_price=walk.price, exit_at=walk.at,
        same_bar_ambiguous=walk.ambiguous, gross_r=gross_r, net_r=net_r, data_complete=True,
        bars_examined=len(considered) + walk.bars_examined, window_start=window_start,
        window_end=window_end, hold_deadline=hold_deadline, first_bar_at=first_at,
        last_bar_at=walk.last_bar_at, limitations=tuple(limitations),
    )


# --- Reconstructing every report-V3 pick from the ledger (read-only) ----------------------------


@dataclass(frozen=True)
class PickRecord:
    """Everything the shadow job and the results views need about one tracked pick."""

    cycle_id: str
    run_slot: str | None
    item_key: str
    revision: int
    signal_id: str | None
    symbol: str
    kind: str | None
    levels: dict
    agent_current_price: str | None
    agent_price_at: str | None
    generated_at: datetime
    window_end: datetime
    agent_id: str | None
    agent_version: str | None
    attribution: str
    selection_policy: str | None
    question_set_version: str | None
    selected: bool
    selection_status: str | None
    decline_code: str | None
    replacement_for: str | None
    jev_rank: int | None
    agent_rank: int | None
    ranking_status: str | None
    ranking_reasons: list
    review_disposition: str | None
    review_reason: str | None
    skip_reason: str | None
    setup_id: str | None
    arm: str | None
    engineering: bool

    def to_dict(self):
        from dataclasses import asdict

        return json_safe({**asdict(self), "rank_bucket": rank_bucket(self)})


def _selection_status(selection, setup, decline, replacement, now):
    if selection is None:
        return None
    if setup is not None:
        return "ADMITTED"
    if decline is not None:
        if decline.get("reason") == "SUPERSEDED_BY_NEW_RESEARCH":
            return "SUPERSEDED"
        if replacement is not None and replacement.get("outcome") == "PUBLISHED":
            return "REPLACED_BY"
        return "DECLINED"
    deadline = selection.get("review_valid_until") or selection.get("expires_at")
    try:
        if deadline is not None and now >= datetime.fromisoformat(deadline):
            return "EXPIRED"
    except (TypeError, ValueError):
        pass
    return "SELECTED"


def _ranking_reasons(entry):
    if entry is None:
        return []
    if entry.get("status") == VETOED:
        return list(entry.get("veto_reasons") or [])
    if entry.get("status") == NOT_RANKED:
        return [entry.get("reason")]
    return []


def report_v3_cycle_started(repository, *, after_event_seq=0, limit=50):
    """Ascending page of report-V3 ``RESEARCH_STARTED`` events (every selection rule)."""
    with repository.connect() as conn:
        return conn.execute(
            """SELECT event_seq, body, recorded_at FROM lab.managed_events
            WHERE kind='RESEARCH_STARTED' AND body->>'report_schema_version'=%s
            AND event_seq>%s ORDER BY event_seq LIMIT %s""",
            (REPORT_SCHEMA_V3, after_event_seq, limit),
        ).fetchall()


def cycle_picks(repository, cycle_id, *, now=None):
    """Every intake-accepted report-V3 pick of one cycle, latest revision, as ``PickRecord``.

    Read-only: scans that cycle's ``RESEARCH_*`` events (as ``research_context.py`` does for
    the caller's latest cycle) plus a lookup of any admitted setup by ``(cycle_id, item_key)``.
    Unlike ``research_context.ResearchContextService._last_run`` this is not scoped to one
    caller's agent and works for any (including historical) cycle -- what the measurement job
    needs, not what one agent is shown.
    """
    now = now or datetime.now(UTC)
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT event_seq, kind, body, idempotency_key FROM lab.managed_events
            WHERE body->>'cycle_id'=%s AND kind LIKE 'RESEARCH_%%' ORDER BY event_seq""",
            (str(cycle_id),),
        ).fetchall()
        setups = conn.execute(
            """SELECT s.record_json->>'item_key' AS item_key, s.setup_id, s.record_json,
            t.body AS state FROM lab.managed_setups s LEFT JOIN lab.managed_states t
            USING(setup_id) WHERE s.cycle_id=%s""",
            (cycle_id,),
        ).fetchall()
    packets, decisions, selected = {}, {}, {}
    declines, replacements, skipped, ranking = {}, {}, {}, None
    for row in rows:
        body, kind = row["body"], row["kind"]
        if kind == "RESEARCH_PACKET" and body.get("report_schema_version") == REPORT_SCHEMA_V3:
            packets[body["item_key"]] = body
        elif kind == "RESEARCH_DECISION":
            decisions[(body["item_key"], body["revision"])] = body
        elif kind == "RESEARCH_SELECTED":
            selected[body["packet"]["item_key"]] = (row["event_seq"], body["packet"])
        elif kind == "RESEARCH_ADMISSION_DECLINED":
            declines[body.get("selection_event_seq")] = body
        elif kind == REPLACEMENT_KIND:
            replacements[body.get("declined_item_key")] = body
        elif kind == SKIPPED_KIND:
            skipped[body.get("item_key")] = body
        elif kind == RANKING_KIND and row["idempotency_key"] == ranking_key_for(cycle_id):
            ranking = body
    entries = {e["item_key"]: e for e in (ranking or {}).get("entries") or []}
    by_item_key = {row["item_key"]: row for row in setups if row["item_key"]}
    records = []
    for item_key, packet in packets.items():
        revision = packet["revision"]
        decision = decisions.get((item_key, revision))
        seq, selection = selected.get(item_key, (None, None))
        decline = declines.get(seq) if selection is not None else None
        replacement = replacements.get(item_key) if decline is not None else None
        entry = entries.get(item_key)
        setup = by_item_key.get(item_key)
        state = setup["state"] if setup else None
        try:
            generated_at = datetime.fromisoformat(packet["created_at"])
        except (KeyError, TypeError, ValueError):
            continue  # No recorded proposal time: fail closed, never guess one.
        try:
            window_end = datetime.fromisoformat(packet["expires_at"])
        except (KeyError, TypeError, ValueError):
            continue
        agent = agent_fields(packet.get("agent") or (selection or {}).get("agent"))
        records.append(PickRecord(
            cycle_id=str(cycle_id), run_slot=packet.get("run_slot"), item_key=item_key,
            revision=revision, signal_id=packet.get("signal_id"), symbol=packet["symbol"],
            kind=(packet.get("state") or {}).get("kind"), levels=packet["levels"],
            agent_current_price=(packet.get("state") or {}).get("agent_current_price"),
            agent_price_at=(packet.get("state") or {}).get("agent_price_at"),
            generated_at=generated_at.astimezone(UTC), window_end=window_end.astimezone(UTC),
            agent_id=agent["agent_id"], agent_version=agent["agent_version"],
            attribution=agent["attribution"],
            selection_policy=packet.get("selection_policy"),
            question_set_version=(selection or {}).get("question_set_version"),
            selected=selection is not None,
            selection_status=_selection_status(selection, setup, decline, replacement, now),
            decline_code=decline.get("reason") if decline else None,
            replacement_for=(selection or {}).get("replacement_for"),
            jev_rank=entry.get("rank") if entry else None,
            agent_rank=(entry.get("agent_rank") if entry else packet.get("rank")),
            ranking_status=entry.get("status") if entry else None,
            ranking_reasons=_ranking_reasons(entry),
            review_disposition=decision["disposition"] if decision else None,
            review_reason=decision.get("reason") if decision else None,
            skip_reason=(skipped.get(item_key) or {}).get("reason"),
            setup_id=str(setup["setup_id"]) if setup else None,
            arm=(state or {}).get("arm") if setup else None,
            engineering=is_engineering(setup["record_json"]) if setup else False,
        ))
    records.sort(key=lambda r: (r.agent_rank if r.agent_rank is not None else 10**9, r.item_key))
    return records


# --- Recording and reading shadow outcomes (append-only ``PICK_SHADOW_OUTCOME`` events) ---------


def shadow_outcome_key(cycle_id, item_key, revision):
    return f"pick-shadow-outcome:{cycle_id}:{item_key}:{revision}"


def ready_at(pick):
    """When this pick's outcome becomes fully knowable: its window plus the 24-hour hold, plus
    ``BAR_FETCH_BUFFER`` so the bar exactly at that hold deadline has had time to close and
    become fetchable (see ``BAR_FETCH_BUFFER``'s own docstring)."""
    return pick.window_end + HOLD_HORIZON + BAR_FETCH_BUFFER


def existing_shadow_outcomes(repository, cycle_id):
    """``{item_key: event}`` of already-recorded shadow outcomes for one cycle."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT body, event_seq, setup_id FROM lab.managed_events
            WHERE kind=%s AND body->'pick'->>'cycle_id'=%s ORDER BY event_seq""",
            (SHADOW_EVENT_KIND, str(cycle_id)),
        ).fetchall()
    return {row["body"]["pick"]["item_key"]: row for row in rows}


def record_shadow_outcome(store, conn, pick, simulation):
    """Append one immutable ``PICK_SHADOW_OUTCOME`` event; idempotent per (cycle, item, revision).

    ``setup_id`` is set on the event row (not only in its body) so an admitted, later-traded
    pick's shadow record also surfaces on that setup's own ``/positions/{id}/timeline``.
    """
    pick_body = {
        "cycle_id": pick.cycle_id, "run_slot": pick.run_slot, "item_key": pick.item_key,
        "revision": pick.revision, "signal_id": pick.signal_id, "symbol": pick.symbol,
        "kind": pick.kind, "levels": json_safe(pick.levels),
        "agent_current_price": pick.agent_current_price, "agent_price_at": pick.agent_price_at,
        "generated_at": pick.generated_at, "window_end": pick.window_end,
        "attribution": pick.attribution, "agent_id": pick.agent_id,
        "agent_version": pick.agent_version, "selection_policy": pick.selection_policy,
        "question_set_version": pick.question_set_version, "selected": pick.selected,
        "selection_status": pick.selection_status, "decline_code": pick.decline_code,
        "replacement_for": pick.replacement_for, "jev_rank": pick.jev_rank,
        "agent_rank": pick.agent_rank, "ranking_status": pick.ranking_status,
        "ranking_reasons": pick.ranking_reasons, "review_disposition": pick.review_disposition,
        "review_reason": pick.review_reason, "skip_reason": pick.skip_reason,
        "setup_id": pick.setup_id, "arm": pick.arm, "engineering": pick.engineering,
    }
    body = {"pick": json_safe(pick_body), "outcome": simulation.to_dict(),
            "computed_at": datetime.now(UTC)}
    return store.event(
        conn, SHADOW_EVENT_KIND, body,
        setup_id=pick.setup_id, key=shadow_outcome_key(pick.cycle_id, pick.item_key, pick.revision),
    )


@dataclass
class JobSummary:
    cycles_scanned: int = 0
    picks_considered: int = 0
    already_recorded: int = 0
    not_yet_ready: int = 0
    recorded: int = 0
    bar_fetch_failed: int = 0
    invalid_levels: int = 0
    recorded_item_keys: list = field(default_factory=list)

    def to_dict(self):
        return {**vars(self), "recorded_item_keys": list(self.recorded_item_keys)}


def run_shadow_outcome_job(store, bar_reader, *, now=None, cycle_id=None, since=None, until=None,
                           after_cycle_event_seq=0, limit=200, force=False):
    """Compute and append shadow outcomes for report-V3 picks whose window has fully elapsed.

    ``cycle_id`` restricts to one cycle (a single run); ``since``/``until`` (aware datetimes)
    restrict by the cycle's ``recorded_at``. Skips a pick already recorded (unless ``force``)
    and a pick not yet ``ready_at`` (its window plus the 24-hour hold has not yet passed) --
    fail-closed: nothing is guessed ahead of the data that would decide it. A bar fetch or
    parse failure for one pick is counted and skipped; every other pick still proceeds.
    """
    now = now or datetime.now(UTC)
    summary = JobSummary()
    repository = store.repo
    for started in report_v3_cycle_started(
        repository, after_event_seq=after_cycle_event_seq, limit=limit
    ):
        body, recorded_at = started["body"], started["recorded_at"]
        this_cycle = body["cycle_id"]
        if cycle_id is not None and str(this_cycle) != str(cycle_id):
            continue
        if since is not None and recorded_at < since:
            continue
        if until is not None and recorded_at > until:
            continue
        summary.cycles_scanned += 1
        existing = {} if force else existing_shadow_outcomes(repository, this_cycle)
        for pick in cycle_picks(repository, this_cycle, now=now):
            summary.picks_considered += 1
            if pick.item_key in existing:
                summary.already_recorded += 1
                continue
            if now < ready_at(pick):
                summary.not_yet_ready += 1
                continue
            try:
                bars = parse_bars(bar_reader.minute_bars(
                    pick.symbol, pick.generated_at, ready_at(pick)
                ))
            except ShadowDataError:
                summary.bar_fetch_failed += 1
                continue
            except Exception:  # noqa: BLE001 -- a transport failure is this pick's only, retried.
                summary.bar_fetch_failed += 1
                continue
            try:
                simulation = simulate_pick(
                    pick.levels, bars, window_start=pick.generated_at, window_end=pick.window_end,
                )
            except ShadowDataError:
                summary.invalid_levels += 1
                continue
            with store.transaction() as conn:
                record_shadow_outcome(store, conn, pick, simulation)
            summary.recorded += 1
            summary.recorded_item_keys.append(pick.item_key)
    return summary


# --- Listing and aggregating tracked picks (read-only) -------------------------------------------


def _real_measurement(repository, setup_id):
    if setup_id is None:
        return None
    from catalyst_lab.managed_measurement import managed_measurement

    return managed_measurement(repository, setup_id)


def pick_outcome_page(repository, *, after_event_seq=0, limit=100):
    """Every recorded shadow outcome, ascending; the real measurement joined in when traded."""
    if type(after_event_seq) is not int or after_event_seq < 0 or (
        type(limit) is not int or not 1 <= limit <= 500
    ):
        raise ValueError("INVALID_PICK_OUTCOME_CURSOR")
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT event_seq, body, setup_id, recorded_at FROM lab.managed_events
            WHERE kind=%s AND event_seq>%s ORDER BY event_seq LIMIT %s""",
            (SHADOW_EVENT_KIND, after_event_seq, limit),
        ).fetchall()
    items = []
    for row in rows:
        pick = row["body"]["pick"]
        items.append({
            "event_seq": row["event_seq"], "recorded_at": row["recorded_at"],
            "pick": pick, "shadow": row["body"]["outcome"],
            "real": _real_measurement(repository, row["setup_id"]) if row["setup_id"] else None,
        })
    return json_safe({
        "method": SHADOW_METHOD_VERSION, "items": items,
        "next_cursor": rows[-1]["event_seq"] if rows else after_event_seq,
    })


PICK_AGGREGATE_VERSION = "PICK_SHADOW_OUTCOME_AGGREGATES_V1"
# Grouping dimensions (plan 4.8): by agent, Jev's rank/replacement/not-selected status, pick
# kind, managed-vs-control arm and selected-vs-not; ``continuation_decision`` is a named hook
# for the later 24-hour-review package (always null today -- there is nothing to group yet).
PICK_AGGREGATE_DIMENSIONS = (
    "agent_id", "rank_bucket", "pick_kind", "arm", "selected", "continuation_decision",
)
TOP_K, REPLACEMENT, NOT_SELECTED, NO_RANKING_RULE = (
    "TOP_K", "REPLACEMENT", "NOT_SELECTED", "NO_RANKING_RULE",
)


def rank_bucket(pick):
    """A display bucket for ``pick``'s ranking/publication fate (plan 4.8's own wording)."""
    status = pick.get("ranking_status") if isinstance(pick, dict) else pick.ranking_status
    selected = pick.get("selected") if isinstance(pick, dict) else pick.selected
    replacement_for = (
        pick.get("replacement_for") if isinstance(pick, dict) else pick.replacement_for
    )
    if status is None:
        return NO_RANKING_RULE
    if status == VETOED:
        return VETOED
    if status == NOT_RANKED:
        return NOT_RANKED
    if status == RANKED:
        if not selected:
            return NOT_SELECTED
        return REPLACEMENT if replacement_for else TOP_K
    return status


def pick_outcome_aggregates(repository, *, group_by=PICK_AGGREGATE_DIMENSIONS):
    """Counts (never a bare mean) of shadow and real outcomes, grouped as asked.

    Every group reports ``count`` and a breakdown of the shadow ``outcome`` classification
    (``outcome_counts``), plus, from only the picks with a *definitive* shadow R (triggered and
    ``data_complete``), ``shadow_win_rate``/``mean_shadow_gross_r``/``mean_shadow_net_r`` -- and,
    from only the subset that actually traded and closed with verified fees,
    ``real_win_rate``/``mean_real_official_r``/``real_count`` (``managed_measurement``, never
    this module's own fee assumption). A statistic with no known input is ``null``.
    """
    if not group_by or not set(group_by) <= set(PICK_AGGREGATE_DIMENSIONS):
        raise ValueError("INVALID_PICK_AGGREGATE_DIMENSIONS")
    with repository.connect() as conn:
        rows = conn.execute(
            "SELECT body, setup_id FROM lab.managed_events WHERE kind=%s", (SHADOW_EVENT_KIND,),
        ).fetchall()
    groups = {}
    for row in rows:
        pick, outcome = row["body"]["pick"], row["body"]["outcome"]
        if pick.get("engineering"):
            continue
        dims = {
            "agent_id": pick.get("agent_id"), "rank_bucket": rank_bucket(pick),
            "pick_kind": pick.get("kind"), "arm": pick.get("arm"),
            "selected": pick.get("selected"),
            "continuation_decision": None,  # Hook: the 24-hour-review package fills this in.
        }
        key = tuple(dims[k] for k in group_by)
        group = groups.setdefault(key, {
            "dims": {k: dims[k] for k in group_by}, "count": 0, "outcome_counts": {},
            "shadow_r_count": 0, "shadow_gross_sum": D0, "shadow_net_sum": D0,
            "shadow_win_count": 0, "real_count": 0, "real_official_sum": D0,
            "real_win_count": 0,
        })
        group["count"] += 1
        group["outcome_counts"][outcome["outcome"]] = (
            group["outcome_counts"].get(outcome["outcome"], 0) + 1
        )
        if outcome.get("data_complete") and outcome.get("gross_r") is not None:
            group["shadow_r_count"] += 1
            group["shadow_gross_sum"] += D(str(outcome["gross_r"]))
            group["shadow_net_sum"] += D(str(outcome["net_r"]))
            group["shadow_win_count"] += D(str(outcome["gross_r"])) > 0
        if row["setup_id"] is not None:
            measurement = _real_measurement(repository, row["setup_id"])
            if measurement and measurement.get("official_r") is not None:
                group["real_count"] += 1
                group["real_official_sum"] += D(str(measurement["official_r"]))
                group["real_win_count"] += D(str(measurement["gross_realized_pnl"] or 0)) > 0
    items = []
    for group in groups.values():
        items.append({
            **group["dims"], "count": group["count"], "outcome_counts": group["outcome_counts"],
            "shadow_r_count": group["shadow_r_count"],
            "shadow_win_rate": (group["shadow_win_count"] / group["shadow_r_count"])
            if group["shadow_r_count"] else None,
            "mean_shadow_gross_r": (group["shadow_gross_sum"] / group["shadow_r_count"])
            if group["shadow_r_count"] else None,
            "mean_shadow_net_r": (group["shadow_net_sum"] / group["shadow_r_count"])
            if group["shadow_r_count"] else None,
            "real_count": group["real_count"],
            "real_win_rate": (group["real_win_count"] / group["real_count"])
            if group["real_count"] else None,
            "mean_real_official_r": (group["real_official_sum"] / group["real_count"])
            if group["real_count"] else None,
        })
    items.sort(key=lambda item: [str(item[k]) for k in group_by])
    return json_safe({
        "aggregate_version": PICK_AGGREGATE_VERSION, "cohort": COHORT, "group_by": list(group_by),
        "items": items,
        "continuation_decision_scope": "PENDING_24H_REVIEW_PACKAGE_NO_DATA_YET",
    })


__all__ = [
    "BAR_FETCH_BUFFER", "Bar", "ExitWalk", "FEE_ASSUMPTION", "FEE_ASSUMPTION_VERSION",
    "HOLD_HORIZON", "METHOD_LABEL", "PICK_AGGREGATE_DIMENSIONS", "PickRecord", "PickSimulation",
    "SHADOW_EVENT_KIND", "SHADOW_METHOD_VERSION", "ShadowDataError", "TAKER_FEE_TIER1",
    "cycle_picks", "existing_shadow_outcomes", "parse_bars", "pick_outcome_aggregates",
    "pick_outcome_page", "r_values", "rank_bucket", "ready_at", "record_shadow_outcome",
    "report_v3_cycle_started", "run_shadow_outcome_job", "shadow_outcome_key", "simulate_pick",
    "walk_to_exit",
]
