"""Recorded maintenance replays for the scorecard and the weekly review (package learning-app).

Plan ``docs/LEARNING-LOOP-PLAN.md`` sections 4.4 and 7 ("the jobs run the shadow outcome job, the
unchanged-plan replay, the scorecard ..."). Two kinds of immutable ``lab.managed_events`` rows,
no setup (the setup is in the body), each appended once, by the nightly job, only when its
counterfactual is complete; the trade's actual R is never stored with it (it is read live from
``managed_measurement`` whenever a reader needs it, so fee evidence that arrives later still
counts):

* ``UNCHANGED_PLAN_REPLAY`` (method ``UNCHANGED_PLAN_REPLAY_V1``, unchanged): what each applied
  stop or target raise and each agreed early exit would have left the trade with on the levels
  in force before it (``unchanged_plan.unchanged_plan_comparisons``), keyed by the change's own
  event.
* ``DAY_REVIEW_DECISION_REPLAY`` (``DAY_REVIEW_DECISION_REPLAY_V1``, new): each 24-hour
  continue-or-exit decision against the decision not taken, on Alpaca's public 1-minute bars.
  A CONTINUE is compared with exiting at the decision (the open of the first bar at or after it,
  within 15 minutes); an EXIT with continuing for 24 hours more on the levels in force at the
  decision (the unchanged-plan walk: first touch of the stop or the target, a bar reaching both
  resolved as the stop, else the first open at or after the 24 hours). R is priced like official
  R: the move from the admitted max entry over (max entry - admitted initial stop), less the
  assumed Alpaca taker fee on both legs (``pick_outcomes.FEE_ASSUMPTION``).
* ``DAY_REVIEW_DECISION_REPLAY_V2`` (package review-window, 2026-09-28): V1 for a decision of a
  setup that recorded a window (``CRYPTO_WINDOW_REVIEW_V1``), whose continue lasts one window,
  not 24 hours: an EXIT is compared with continuing for the setup's recorded window
  (``CONTINUE_WINDOW_ON_LEVELS_IN_FORCE``; the record adds ``window_seconds``). Everything else
  is V1's, and every decision of a 24-hour setup keeps V1 exactly.

Read-only apart from those appends: no order, no live quote, nothing sent to Jev.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from decimal import InvalidOperation

from catalyst_lab import crypto_holding
from catalyst_lab import day_review as dr
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.pick_outcomes import (
    BAR_FETCH_BUFFER,
    DATA_INCOMPLETE,
    FEE_ASSUMPTION_VERSION,
    ShadowDataError,
    parse_bars,
    r_values,
    walk_to_exit,
)
from catalyst_lab.repository import json_safe
from catalyst_lab.trade_review import decision_records
from catalyst_lab.unchanged_plan import traded_scale, unchanged_plan_comparisons

REPLAY_EVENT = "UNCHANGED_PLAN_REPLAY"
DAY_REPLAY_EVENT = "DAY_REVIEW_DECISION_REPLAY"
DAY_REPLAY_METHOD = "DAY_REVIEW_DECISION_REPLAY_V1"
DAY_REPLAY_WINDOW_METHOD = "DAY_REVIEW_DECISION_REPLAY_V2"  # Package review-window.
EXIT_AT_DECISION = "EXIT_AT_DECISION"
CONTINUE_24H = "CONTINUE_24H_ON_LEVELS_IN_FORCE"
CONTINUE_WINDOW = "CONTINUE_WINDOW_ON_LEVELS_IN_FORCE"
CONTINUE_HORIZON = timedelta(hours=24)
EXIT_BAR_WITHIN = timedelta(minutes=15)
LOOKBACK = timedelta(days=40)  # Older changes are recorded already or never will be.
RECORDED_BY = "LEARNING_JOBS_V1"


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


class CachedBars:
    """One public-bar reader shared by every replay of a run: a window already fetched for a
    symbol is sliced, never fetched again (each setup's changes share one hold window)."""

    def __init__(self, reader):
        self.reader = reader
        self._ranges = {}

    def minute_bars(self, symbol, start, end):
        for (low, high), rows in self._ranges.get(symbol, ()):
            if low <= start and end <= high:
                return [row for row in rows if start <= _aware(str(row["t"]).replace(
                    "Z", "+00:00")) < end]
        rows = self.reader.minute_bars(symbol, start, end)
        self._ranges.setdefault(symbol, []).append(((start, end), rows))
        return rows


# --- DAY_REVIEW_DECISION_REPLAY_V1 (pure) --------------------------------------------------------


def continue_horizon(window_seconds=None):
    """How long an EXIT's alternative continues: 24 hours (V1), or the setup's recorded window
    (``DAY_REVIEW_DECISION_REPLAY_V2``)."""
    return CONTINUE_HORIZON if window_seconds is None else timedelta(seconds=window_seconds)


def day_decision_replay(decision, bars, *, entry_price, initial_stop, window_seconds=None):
    """The decision not taken, for one ``trade_review.decision_records`` record of a
    continue-or-exit review (``outcome`` CONTINUE or EXIT). ``bars``: ascending
    ``pick_outcomes.Bar`` from the decision on. ``window_seconds``: the setup's recorded window
    (``DAY_REVIEW_DECISION_REPLAY_V2``), None for a 24-hour setup (V1 exactly). Returns the
    record body (``data_complete`` false: nothing to record yet)."""
    at = _aware(decision["at"])
    after = [bar for bar in bars if bar.start >= at]
    old_stop, old_target = D(str(decision["old_stop"])), D(str(decision["old_target"]))
    ambiguous, examined = False, 0
    if decision["outcome"] == dr.CONTINUE:
        alternative = EXIT_AT_DECISION
        first = after[0] if after and after[0].start < at + EXIT_BAR_WITHIN else None
        reason = "EXIT_AT_DECISION" if first else DATA_INCOMPLETE
        price, exit_at = (first.open, first.start) if first else (None, None)
        examined = 1 if first else 0
    else:
        alternative = CONTINUE_24H if window_seconds is None else CONTINUE_WINDOW
        walk = walk_to_exit(old_stop, old_target, after,
                            hold_deadline=at + continue_horizon(window_seconds))
        reason, price, exit_at = walk.reason, walk.price, walk.at
        ambiguous, examined = walk.ambiguous, walk.bars_examined
    complete = reason != DATA_INCOMPLETE
    gross = net = None
    if complete:
        gross, net = r_values(entry_price, initial_stop, price)
    return json_safe({
        "method": DAY_REPLAY_METHOD if window_seconds is None else DAY_REPLAY_WINDOW_METHOD,
        "decision": decision["outcome"],
        "alternative": alternative, "at": at, "old_stop": old_stop, "old_target": old_target,
        "new_stop": decision.get("new_stop"), "new_target": decision.get("new_target"),
        "exit_reason": reason, "exit_price": price, "exit_at": exit_at,
        "same_bar_ambiguous": ambiguous, "bars_examined": examined,
        "entry_price": entry_price, "initial_stop": initial_stop,
        "alternative_gross_r": gross, "alternative_net_r": net, "data_complete": complete,
        "fee_assumption": FEE_ASSUMPTION_VERSION,
        **({} if window_seconds is None else {"window_seconds": window_seconds}),
    })


def day_replay_ready_at(decision, window_seconds=None):
    at = _aware(decision["at"])
    horizon = (EXIT_BAR_WITHIN if decision["outcome"] == dr.CONTINUE
               else continue_horizon(window_seconds))
    return at + horizon + BAR_FETCH_BUFFER


# --- The nightly step ----------------------------------------------------------------------------


def replay_candidates(repository, *, since):
    """Non-engineering setups with a replayable change or a review decision since ``since``;
    ``state`` is the setup's latest state (its recorded holding policy and window)."""
    with repository.connect() as conn:
        return conn.execute(
            """SELECT s.setup_id, s.symbol, s.record_json, t.body->>'arm' AS arm,
            t.body AS state
            FROM lab.managed_setups s LEFT JOIN lab.managed_states t USING(setup_id)
            WHERE s.setup_id IN (SELECT DISTINCT e.setup_id FROM lab.managed_events e
              WHERE e.setup_id IS NOT NULL AND e.recorded_at >= %s AND (
                e.kind='MANAGEMENT_PLAN_AUTHORIZED'
                OR (e.kind='MAINTENANCE_DECISION' AND e.body->>'outcome'='APPLIED')
                OR (e.kind='EXIT_FLAG_RESOLVED'
                    AND e.body->>'outcome' IN ('EXIT_AGREED','NO_ANSWER_EXIT'))
                OR (e.kind=%s AND e.body->>'outcome' IN (%s, %s))))
            ORDER BY s.event_seq""",
            (since, dr.DECISION, dr.CONTINUE, dr.EXIT),
        ).fetchall()


def _recorded(conn, key):
    return conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                        (key,)).fetchone() is not None


def _identity(setup):
    agent = (setup["record_json"] or {}).get("agent")
    return {"setup_id": str(setup["setup_id"]), "symbol": setup["symbol"], "arm": setup["arm"],
            "agent_id": agent.get("agent_id") if isinstance(agent, dict) else None,
            "recorded_by": RECORDED_BY}


def record_replays(store, reader, *, now):
    """Append every complete replay not yet recorded; counts by outcome. A setup whose bars
    cannot be read, or whose recorded data cannot be replayed (``invalid``, also counted in
    ``failed``), is left for the next run; the others proceed."""
    now = _aware(now)
    bars = CachedBars(reader)
    summary = {"setups": 0, "recorded": 0, "already_recorded": 0, "pending": 0, "failed": 0,
               "invalid": 0}
    for setup in replay_candidates(store.repo, since=now - LOOKBACK):
        if is_engineering(setup["record_json"]):
            continue
        summary["setups"] += 1
        identity = _identity(setup)
        try:
            outcomes = unchanged_plan_comparisons(store.repo, bars, setup["setup_id"], now=now)
            levels = setup["record_json"]["levels"]
            # TRADED_LEVELS_V1 (package learning-loop2): official R's scale, the plan's stop.
            entry, initial_stop = traded_scale(levels, setup.get("state"))
            basis = traded_basis(levels, setup.get("state"))
            # CRYPTO_WINDOW_REVIEW_V1 setups: V2, their continue lasts their recorded window.
            window = crypto_holding.recorded_window_seconds(setup.get("state"))
            decisions = [d for d in decision_records(store.repo, setup["setup_id"])
                         if d.get("change_kind") == dr.CONTINUE_EXIT_DECISION
                         and d.get("outcome") in (dr.CONTINUE, dr.EXIT)]
            day = []
            for decision in decisions:
                if now < day_replay_ready_at(decision, window):
                    summary["pending"] += 1
                    continue
                at = _aware(decision["at"])
                end = min(day_replay_ready_at(decision, window), now)
                rows = bars.minute_bars(setup["symbol"], at, end)
                day.append((decision, day_decision_replay(
                    decision, parse_bars(rows), entry_price=entry, initial_stop=initial_stop,
                    window_seconds=window)))
        except (ShadowDataError, KeyError, TypeError, ValueError, InvalidOperation):
            summary["failed"] += 1
            summary["invalid"] += 1  # Its ledger data, not the bars: counted apart.
            continue
        except Exception:  # noqa: BLE001 -- a bar transport failure: this setup only, retried.
            summary["failed"] += 1
            continue
        with store.transaction() as conn:
            for outcome in outcomes:
                if not outcome.data_complete:
                    summary["pending"] += 1
                    continue
                key = f"unchanged-plan-replay:{outcome.source_event_seq}"
                if _recorded(conn, key):
                    summary["already_recorded"] += 1
                    continue
                record = outcome.to_dict()
                for derived in ("actual_r", "r_difference"):  # Read live, never frozen here.
                    record.pop(derived, None)
                store.event(conn, REPLAY_EVENT, json_safe({
                    **identity, **record, "fee_assumption": FEE_ASSUMPTION_VERSION,
                    **basis}), key=key)
                summary["recorded"] += 1
            for decision, record in day:
                if not record["data_complete"]:
                    summary["pending"] += 1
                    continue
                key = f"day-review-decision-replay:{decision['source_event_seq']}"
                if _recorded(conn, key):
                    summary["already_recorded"] += 1
                    continue
                store.event(conn, DAY_REPLAY_EVENT, json_safe({
                    **identity, **record, "source_event_seq": decision["source_event_seq"],
                    "review_number": decision.get("review_number"), **basis}), key=key)
                summary["recorded"] += 1
    return summary


def recorded_replays(repository, setup_ids):
    """Every recorded replay of these setups, oldest first: ``(kind, body)`` rows."""
    if not setup_ids:
        return []
    with repository.connect() as conn:
        return conn.execute(
            """SELECT kind, body, event_seq FROM lab.managed_events
            WHERE kind IN (%s, %s) AND setup_id IS NULL AND body->>'setup_id' = ANY(%s)
            ORDER BY event_seq""",
            (REPLAY_EVENT, DAY_REPLAY_EVENT, [str(s) for s in setup_ids]),
        ).fetchall()


def decision_type(kind, body):
    """The scorecard's decision type of a recorded replay."""
    if kind == DAY_REPLAY_EVENT:
        return "DAY_REVIEW_" + body["decision"]
    return body["change_kind"]


def traded_basis(levels, state):
    """The replay record's R scale (TRADED_LEVELS_V1, package learning-loop2): ``r_basis``
    and the research levels kept beside the traded ones."""
    from catalyst_lab import trade_plan

    return {"r_basis": "TRADE_PLAN_STOP" if trade_plan.active(state or {})
            else "ADMITTED_PACKET_STOP",
            "research_levels": {k: str(levels[k]) for k in ("max_entry_price", "stop", "target")
                                if k in levels}}


def counterfactual_r(kind, body, basis=None):
    """A recorded replay's counterfactual net R. ``basis`` (``{"entry", "stop"}``: the trade's
    official-R scale, ``scale_of``) rebases a replay recorded before TRADED_LEVELS_V1 (no
    ``r_basis``) whose ``initial_stop`` is not that scale's stop -- a CRYPTO_TRADE_PLAN_V1
    setup's replay priced on the research stop -- from its own recorded exit price, with the
    same assumed fee; the record itself is never changed."""
    value = body.get("alternative_net_r" if kind == DAY_REPLAY_EVENT else "unchanged_net_r")
    if value is None:
        return None
    if basis and body.get("r_basis") is None and body.get("exit_price") is not None:
        try:
            recorded_stop = D(str(body["initial_stop"]))
            if recorded_stop != basis["stop"]:
                _gross, net = r_values(basis["entry"], basis["stop"], D(str(body["exit_price"])))
                return net
        except (KeyError, TypeError, ValueError, InvalidOperation, ShadowDataError):
            return D(str(value))
    return D(str(value))


def scale_of(record_json, state):
    """``{"entry", "stop"}``: a trade's official-R scale (for ``counterfactual_r``), or None
    when its levels cannot be read."""
    try:
        entry, stop = traded_scale((record_json or {})["levels"], state)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    return {"entry": entry, "stop": stop}


__all__ = [
    "CONTINUE_24H", "CONTINUE_WINDOW", "CachedBars", "DAY_REPLAY_EVENT", "DAY_REPLAY_METHOD",
    "DAY_REPLAY_WINDOW_METHOD", "EXIT_AT_DECISION", "REPLAY_EVENT", "counterfactual_r",
    "day_decision_replay", "decision_type", "record_replays", "recorded_replays",
    "replay_candidates", "scale_of", "traded_basis",
]
