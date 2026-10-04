"""``JEV_CALIBRATION_V1``: each probability Jev stated, next to what happened (package jev-b3,
plan ``docs/TRADING-QUALITY-PLAN.md`` sections 4 and 10, B3: "each probability next to its
outcome. Weekly reliability table, Brier score, and ranking lift against mechanical
baselines").

**Record-only. Paper trading only.** Nothing here places an order, changes a rule, reaches Jev
or is read by any trading decision. The nightly job appends one immutable
``JEV_CALIBRATION_RECORD`` (``lab.managed_events``, no setup; the setup is in the body) per
reviewed pick and per answered maintenance review, once that probability's outcome window is
complete; the weekly review reads them (``calibration_section``) and the scorecard counts them
(``day_counts``). No migration.

**Sources** (non-engineering only; the last 40 days, as the other nightly steps):

* ``SELECTION`` -- every intake-accepted pick of a top-K cycle (``JEV_TOP_K_SELECTION_V1``/
  ``V2``): ranked, vetoed, not ranked, selected or not. The forecasts are read from the pick's
  ``RESEARCH_DECISION`` and ``RESEARCH_QUALITY`` answers (the reviews' own probabilities). Each
  Choice answer becomes ``p`` = the summed probability of its favourable labels (the top-K
  rule's passing labels; ``APPROVE`` for ``verdict``; ``STRONG`` for ``quality_category``).
  Outcome: the pick's ``PICK_SHADOW_OUTCOME_V1`` (same fill rules and assumed fees for every
  pick); the record waits until that shadow outcome exists. A traded pick adds its official R
  as known at record time (``traded``).
* ``MAINTENANCE`` -- every ``MAINTENANCE_DECISION`` that carries an answer:
  ``CRYPTO_MAINTENANCE_V5`` records ``invalidation_met`` and ``news_contradicts`` (each Noul's
  ``p``); V1-V4 record ``action:FLAG_EARLY_EXIT`` (the action Choice's probability of
  ``FLAG_EARLY_EXIT`` from the final receipt's stored judgment, else the decision's own summary
  when ``FLAG_EARLY_EXIT`` was its choice), tagged by the decision's version. Outcome, from the
  review's decision time on the public 1-minute bars the replays read: the trade's R path over
  4 and 24 hours (max, min and last close, R = (price - entry) / risk per coin as maintenance
  records them), whether the plan's initial stop was reached before the plan's initial target
  within 24 hours, and, for a confirmed invalidation (or a V1-V4 flag), the R of exiting at the
  decision (the first bar's open within 15 minutes) minus the R of holding to the plan's levels
  for 24 hours (gross, no fee: both sides pay the same entry fee).

**Events each probability predicts** (``EVENTS``):

* ``SHADOW_NET_R_24H_POSITIVE`` (selection): the pick's shadow net R is above zero. Picks the
  shadow never filled have no R and are excluded from this event (counted as untriggered).
* ``PLAN_STOP_BEFORE_PLUS_1R_24H`` (maintenance): within 24 hours after the review a bar's low
  reached the plan's initial stop before any bar's high reached entry + 1R (a bar reaching both
  counts as the stop, as every replay resolves it).

**Mechanical baseline** ``BREAKOUT_7D_VOLUME_V1`` (recorded with each selection record; the
signal study of 2026-09-29, approximated on Alpaca's public 1-hour bars before the pick): with
``end`` the pick's time floored to the hour, the last 24 completed hours close above the
highest high of the 168 hours before them, on a 24-hour volume at least 2x the mean 24-hour
volume of those 7 days. At least 20 and 120 bars are required, else ``INSUFFICIENT_BARS``.
Ranking: flagged first, then the volume ratio, then the agent's order.

The weekly section, its reliability bins, Brier scores, ranking lift and threshold notes are
described at ``calibration_section``.
"""

from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from catalyst_lab import trade_plan
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.pick_outcomes import (
    BAR_FETCH_BUFFER,
    DATA_INCOMPLETE,
    SHADOW_METHOD_VERSION,
    STOP,
    ShadowDataError,
    cycle_picks,
    existing_shadow_outcomes,
    parse_bars,
    rank_bucket,
    report_v3_cycle_started,
    walk_to_exit,
)
from catalyst_lab.repository import json_safe
from catalyst_lab.research_ranking import QUALITY_V3_POLICY
from catalyst_lab.research_selection_topk import (
    PASSING,
    RANKED,
    RANKING_KIND,
    TOPK_POLICIES,
    TOPK_POLICY_V3,
    ranking_key_for,
)

D = Decimal
CALIBRATION_VERSION = "JEV_CALIBRATION_V1"
RECORD_EVENT = "JEV_CALIBRATION_RECORD"
SELECTION, MAINTENANCE = "SELECTION", "MAINTENANCE"
SELECTION_EVENT = "SHADOW_NET_R_24H_POSITIVE"
STOP_EVENT = "PLAN_STOP_BEFORE_PLUS_1R_24H"
EVENTS = {
    SELECTION_EVENT: "The pick's PICK_SHADOW_OUTCOME_V1 net R (simulated fill at max entry, "
                     "24-hour hold, assumed taker fee on both legs) is above 0; picks the "
                     "shadow never filled are excluded.",
    STOP_EVENT: "Within 24 hours after the review, a 1-minute bar's low reached the plan's "
                "initial stop before any bar's high reached entry + 1R (a bar reaching both "
                "counts as the stop).",
}
SELECTION_BASIS = "PICK_SHADOW_OUTCOME_V1_NET_R"
PATH_BASIS = "PUBLIC_1MIN_BARS_FROM_DECISION_V1"
MECHANICAL = "BREAKOUT_7D_VOLUME_V1"
LOOKBACK = timedelta(days=40)
HORIZONS = (("4h", timedelta(hours=4)), ("24h", timedelta(hours=24)))
OUTCOME_HORIZON = timedelta(hours=24)
EXIT_BAR_WITHIN = timedelta(minutes=15)
CYCLE_PAGE = 200
FOUR = D("0.0001")
FLAG_ACTION = "FLAG_EARLY_EXIT"
V5_POLICY = "CRYPTO_MAINTENANCE_V5"
V5_QUESTIONS = ("invalidation_met", "news_contradicts")
FAVOURABLE = {**{name: frozenset(labels) for name, labels in PASSING.items()},
              "verdict": frozenset({"APPROVE"}), "quality_category": frozenset({"STRONG"})}
# Breakout baseline (BREAKOUT_7D_VOLUME_V1).
HOUR = timedelta(hours=1)
DAY_HOURS, PRIOR_HOURS = 24, 168
MIN_DAY_BARS, MIN_PRIOR_BARS = 20, 120
VOLUME_MULTIPLE = D(2)
# The weekly section.
BINS = ((D("0"), D("0.2")), (D("0.2"), D("0.4")), (D("0.4"), D("0.6")), (D("0.6"), D("0.8")),
        (D("0.8"), D("1")))
MINIMUMS = {SELECTION: 40, MAINTENANCE: 30}  # WEEKLY_REVIEW_V1's SELECTION_VALUE / per-arm 30.
LIFT_MINIMUM = 40  # Jev's top-K picks pooled over complete cycles (SELECTION_VALUE's 40).
YES_THRESHOLDS = (D("0.70"), D("0.80"), D("0.90"))
NO_THRESHOLDS = (D("0.10"), D("0.20"), D("0.30"))
CURRENT_YES, CURRENT_NO = D("0.80"), D("0.20")  # MAINTENANCE_ANSWER_RULE_V3.
K_ALTERNATIVES = (3, 5, 10)
LIMITATIONS = (
    "Shadow outcomes and R paths are 1-minute-bar approximations (Alpaca's public bars) with an "
    "assumed taker fee where fees apply; a traded pick's own official R is recorded beside its "
    "shadow R but the tables use the shadow R so every pick is measured alike.",
    "Maintenance reviews of one trade share its price path: they are not independent samples.",
    "Selection component questions judge evidence, not profit; their reliability against the "
    "profit event says only whether passing answers went with winning picks.",
    "Thresholds are reported, never applied: a change needs the owner's approval, a named "
    "version, tests and a CONTRACT-RESOLUTIONS entry.",
)


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


def _q(value, step=FOUR):
    if value is None:
        return None
    rounded = D(value).quantize(step, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _prob(value):
    """A stated probability as an exact Decimal in [0, 1] (the JSON number's own text)."""
    if isinstance(value, bool) or value is None:
        raise ValueError("INVALID_PROBABILITY")
    number = D(str(value))
    if not number.is_finite() or not D(0) <= number <= D(1):
        raise ValueError("INVALID_PROBABILITY")
    return number


def selection_key(cycle_id, item_key, revision):
    return f"jev-calibration:selection:{cycle_id}:{item_key}:{revision}"


def maintenance_key(request_id):
    return f"jev-calibration:maintenance:{request_id}"


# --- Forecasts (pure) ---------------------------------------------------------------------------


def choice_forecasts(answers, version):
    """``[{question, version, p, choice, favourable}]`` of every Choice answer with a known
    favourable label set; ``p`` = the summed probability of those labels."""
    forecasts = []
    for name in sorted(answers or {}):
        answer = answers[name]
        labels = FAVOURABLE.get(name)
        if labels is None or not isinstance(answer, dict) or answer.get("type") != "choice":
            continue
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or not probabilities:
            continue
        try:
            p = sum((_prob(probabilities[label]) for label in labels if label in probabilities),
                    D(0))
        except (ValueError, InvalidOperation):
            continue
        forecasts.append({"question": name, "version": version, "p": str(min(p, D(1))),
                          "choice": answer.get("choice"), "favourable": sorted(labels),
                          "event": SELECTION_EVENT})
    return forecasts


# JEV_TOP_K_SELECTION_V3 (package jev-b2): its stage-1 Nouls, whose favourable answer is "no"
# for thesis_contradicted and "yes" for the others, and its comparative Noul per pick.
V3_NEGATIVE_NOULS = frozenset({"thesis_contradicted"})


def noul_forecasts(answers, version):
    """``[{question, version, p, favourable}]`` of every Noul answer: ``p`` = the probability of
    the favourable answer (1 - p for a negative question)."""
    forecasts = []
    for name in sorted(answers or {}):
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            continue
        try:
            p = _prob(answer["noul"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        negative = name in V3_NEGATIVE_NOULS
        forecasts.append({"question": name, "version": version,
                          "p": str(D(1) - p if negative else p),
                          "favourable": "NO" if negative else "YES",
                          "event": SELECTION_EVENT})
    return forecasts


def comparative_forecasts(entry, version):
    """V3's comparative Noul and both best-choice probabilities of a ranking entry."""
    forecasts = []
    entry = entry or {}
    values = [("comparative:worth_opening", entry.get("probability"))]
    for side, value in sorted((entry.get("best_choice") or {}).items()):
        values.append((f"comparative:best_{side}", value))
    for name, value in values:
        if value is None:
            continue
        try:
            p = _prob(value)
        except (TypeError, ValueError, InvalidOperation):
            continue
        forecasts.append({"question": name, "version": version, "p": str(p),
                          "favourable": "YES", "event": SELECTION_EVENT})
    return forecasts


def v5_forecasts(body):
    """``CRYPTO_MAINTENANCE_V5``: each asked Noul's ``p``, verdict and streak effect."""
    verdicts = body.get("verdicts") or {}
    forecasts = []
    for name, answer in sorted((body.get("answers") or {}).items()):
        try:
            p = _prob(answer["p"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        effect = verdicts.get(name) or {}
        forecasts.append({"question": name, "version": body.get("policy_id"), "p": str(p),
                          "verdict": answer.get("verdict"), "effect": effect.get("effect"),
                          "event": STOP_EVENT})
    return forecasts


def action_forecast(body, judgment):
    """V1-V4: the probability of ``FLAG_EARLY_EXIT`` in the action answer, with its source."""
    probabilities = (judgment or {}).get("probabilities") if isinstance(judgment, dict) else None
    try:
        if isinstance(probabilities, dict):
            p, source = _prob(probabilities.get(FLAG_ACTION, 0)), "RECEIPT_JUDGMENT"
        else:
            summary = (body.get("answers") or {}).get("action") or {}
            if summary.get("choice") != FLAG_ACTION or summary.get("top_p") is None:
                return [], "UNAVAILABLE"
            p, source = _prob(summary["top_p"]), "DECISION_SUMMARY_TOP_P"
    except (ValueError, InvalidOperation):
        return [], "UNAVAILABLE"
    return [{"question": "action:" + FLAG_ACTION, "version": body.get("policy_id"),
             "p": str(p), "choice": body.get("action"), "event": STOP_EVENT}], source


# --- The mechanical baseline (pure) -------------------------------------------------------------


def breakout_flag(bars, at):
    """``BREAKOUT_7D_VOLUME_V1`` at ``at`` over 1-hour ``bars`` (see the module)."""
    end = _aware(at).replace(minute=0, second=0, microsecond=0)
    day_start, prior_start = end - DAY_HOURS * HOUR, end - (DAY_HOURS + PRIOR_HOURS) * HOUR
    day = [b for b in bars if day_start <= b.start and b.start + HOUR <= end]
    prior = [b for b in bars if prior_start <= b.start < day_start]
    base = {"method": MECHANICAL, "end": end.isoformat(), "day_bars": len(day),
            "prior_bars": len(prior)}
    prior_volume = sum((b.volume for b in prior), D(0))
    if len(day) < MIN_DAY_BARS or len(prior) < MIN_PRIOR_BARS or prior_volume <= 0:
        return {**base, "status": "INSUFFICIENT_BARS", "flag": None, "volume_ratio": None}
    high = max(b.high for b in prior)
    close = day[-1].close
    ratio = sum((b.volume for b in day), D(0)) / (prior_volume / 7)
    return {**base, "status": "COMPUTED", "flag": close > high and ratio >= VOLUME_MULTIPLE,
            "close": str(close), "high_7d": str(high), "volume_ratio": str(_q(ratio))}


def mechanical_order(picks):
    """Picks ordered by the baseline: flagged, then the volume ratio, then the agent's order."""
    def key(pick):
        mech = pick["mechanical"]
        return (0 if mech.get("flag") else 1, -D(mech.get("volume_ratio") or 0),
                pick.get("agent_rank") if pick.get("agent_rank") is not None else 10 ** 9)
    return sorted(picks, key=key)


# --- The maintenance outcome (pure) -------------------------------------------------------------


class _Bars:
    """One setup's parsed minute bars, sliced by start time without copying the scan."""

    def __init__(self, bars):
        self.bars = bars
        self.starts = [b.start for b in bars]

    def since(self, at):
        return self.bars[bisect_left(self.starts, at):]


def _r(price, entry, risk):
    return (price - entry) / risk


def r_path(after, at, *, entry, risk):
    """``{4h, 24h}`` max, min and last close in R over bars starting in ``[at, at + h)``;
    ``complete`` when a bar at or after ``at + h`` was seen."""
    path = {}
    for name, horizon in HORIZONS:
        end = at + horizon
        inside = []
        complete = False
        for bar in after:
            if bar.start >= end:
                complete = True
                break
            inside.append(bar)
        if not inside:
            path[name] = {"complete": complete, "bars": 0, "max_r": None, "min_r": None,
                          "close_r": None}
            continue
        path[name] = {
            "complete": complete, "bars": len(inside),
            "max_r": _q(_r(max(b.high for b in inside), entry, risk)),
            "min_r": _q(_r(min(b.low for b in inside), entry, risk)),
            "close_r": _q(_r(inside[-1].close, entry, risk)),
        }
    return path


def maintenance_outcome(bars, at, *, entry, risk, plan_stop, plan_target, confirmed):
    """The outcome body of one review at ``at`` (``bars``: ``_Bars``)."""
    after = bars.since(at)
    deadline = at + OUTCOME_HORIZON
    plus_1r = entry + risk
    event_walk = walk_to_exit(plan_stop, plus_1r, after, hold_deadline=deadline)
    plan_walk = walk_to_exit(plan_stop, plan_target, after, hold_deadline=deadline)
    complete = event_walk.reason != DATA_INCOMPLETE
    outcome = {
        "basis": PATH_BASIS, "from": at, "plus_1r": plus_1r, "plan_stop": plan_stop,
        "plan_target": plan_target, "r_path": r_path(after, at, entry=entry, risk=risk),
        "event": None if not complete else event_walk.reason == STOP,
        "event_exit_reason": event_walk.reason, "event_at": event_walk.at,
        "same_bar_ambiguous": event_walk.ambiguous,
        "first_touch_plan": {"reason": plan_walk.reason, "at": plan_walk.at,
                             "same_bar_ambiguous": plan_walk.ambiguous},
        "data_complete": complete and plan_walk.reason != DATA_INCOMPLETE,
    }
    if confirmed is not None:
        first = after[0] if after and after[0].start < at + EXIT_BAR_WITHIN else None
        exit_r = _r(first.open, entry, risk) if first is not None else None
        hold_r = (_r(plan_walk.price, entry, risk) if plan_walk.reason != DATA_INCOMPLETE
                  else None)
        outcome["confirmed"] = {
            **confirmed, "exit_at_decision_r": _q(exit_r), "hold_to_plan_r": _q(hold_r),
            "hold_exit_reason": plan_walk.reason,
            "exit_minus_hold_r": _q(exit_r - hold_r) if None not in (exit_r, hold_r) else None,
        }
    return outcome


# --- Reading the ledger ------------------------------------------------------------------------


def recorded_keys(repository):
    with repository.connect() as conn:
        rows = conn.execute("SELECT idempotency_key FROM lab.managed_events WHERE kind=%s",
                            (RECORD_EVENT,)).fetchall()
    return {row["idempotency_key"] for row in rows}


def _cycle_reviews(repository, cycle_id):
    """``(ranking, decisions, qualities)`` of one cycle: the top-K ranking body (or None) and
    the review events by ``(item_key, revision)`` with their ``recorded_at``."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT kind, body, recorded_at, idempotency_key FROM lab.managed_events
            WHERE body->>'cycle_id'=%s AND kind IN ('RESEARCH_DECISION','RESEARCH_QUALITY',%s)
            ORDER BY event_seq""", (str(cycle_id), RANKING_KIND)).fetchall()
    ranking, decisions, qualities = None, {}, {}
    for row in rows:
        body = row["body"]
        if row["kind"] == RANKING_KIND:
            if row["idempotency_key"] == ranking_key_for(cycle_id):
                ranking = body
            continue
        target = decisions if row["kind"] == "RESEARCH_DECISION" else qualities
        target[(body.get("item_key"), body.get("revision"))] = (body, row["recorded_at"])
    return ranking, decisions, qualities


def _traded(repository, setup_id):
    if setup_id is None:
        return None
    measured = managed_measurement(repository, setup_id)
    tainted = any(item.get("source") == "LAB_FIXTURE"
                  for item in measured.get("cost_evidence") or [])
    official = measured.get("official_r")
    return {"setup_id": setup_id, "official_r": None if tainted else official,
            "fees_status": "FIXTURE_SOURCE" if tainted else (
                "VERIFIED" if official is not None else "NOT_YET_KNOWN"),
            "basis": "OFFICIAL_R_AT_RECORD_TIME"}


def _hour_bars(reader, symbol, at):
    end = _aware(at).replace(minute=0, second=0, microsecond=0)
    start = end - (DAY_HOURS + PRIOR_HOURS) * HOUR
    return parse_bars(reader.bars(symbol, start, end, "1Hour"))


def selection_record(pick, entry, decision, quality, shadow, *, policy, k, cycle_size,
                     mechanical, traded):
    """The ``JEV_CALIBRATION_RECORD`` body of one pick (pure)."""
    decision_body, stated_at = decision if decision else ({}, None)
    quality_body, quality_at = quality if quality else ({}, None)
    version = decision_body.get("question_set_version") or (entry or {}).get(
        "question_set_version")
    forecasts = choice_forecasts(decision_body.get("answers"), version)
    if policy == TOPK_POLICY_V3:
        forecasts += noul_forecasts(decision_body.get("answers"), version)
        forecasts += comparative_forecasts(entry, (entry or {}).get(
            "comparison_question_set_version") or "COMPARATIVE_PICK_QUESTIONS_V1")
    forecasts += choice_forecasts(
        {k_: v for k_, v in (quality_body.get("answers") or {}).items()
         if k_ == "quality_category"}, quality_body.get("quality_policy") or QUALITY_V3_POLICY)
    shadow_outcome = shadow["body"]["outcome"]
    complete = bool(shadow_outcome.get("data_complete"))
    triggered = bool(shadow_outcome.get("triggered"))
    net_r = shadow_outcome.get("net_r")
    event = (None if not complete or not triggered or net_r is None
             else D(str(net_r)) > 0)
    stated = stated_at or quality_at or pick.generated_at
    return json_safe({
        "calibration_version": CALIBRATION_VERSION, "source": SELECTION,
        "source_key": selection_key(pick.cycle_id, pick.item_key, pick.revision),
        "stated_at": stated, "cycle_id": pick.cycle_id, "run_slot": pick.run_slot,
        "item_key": pick.item_key, "revision": pick.revision, "symbol": pick.symbol,
        "pick_kind": pick.kind, "agent_id": pick.agent_id, "generated_at": pick.generated_at,
        "selection_policy": policy, "k": k, "cycle_picks": cycle_size,
        "ranking": {
            "status": pick.ranking_status, "jev_rank": pick.jev_rank,
            "agent_rank": pick.agent_rank, "rank_bucket": rank_bucket(pick),
            "selected": pick.selected, "selection_status": pick.selection_status,
            "veto_reasons": list((entry or {}).get("veto_reasons") or []),
            "uncertain": list((entry or {}).get("uncertain") or []),
            "not_ranked_reason": (entry or {}).get("reason"),
            "adjusted_score": (entry or {}).get("adjusted_score"),
            "quality_score": (entry or {}).get("quality_score"),
            "quality_category": (entry or {}).get("quality_category"),
            "dissent": (entry or {}).get("dissent"),
            # JEV_TOP_K_SELECTION_V3 only: the comparative probability and its place.
            **({"probability": (entry or {}).get("probability"),
                "comparison_position": (entry or {}).get("comparison_position")}
               if policy == TOPK_POLICY_V3 else {}),
        },
        "receipt_ids": {"review": list(decision_body.get("receipt_ids") or []),
                        "quality": list(quality_body.get("receipt_ids") or [])},
        "forecasts": forecasts,
        "outcome": {
            "basis": SELECTION_BASIS, "shadow_method": SHADOW_METHOD_VERSION,
            "shadow_event_seq": shadow["event_seq"], "shadow_outcome": shadow_outcome.get(
                "outcome"), "triggered": triggered, "data_complete": complete,
            "net_r": net_r, "gross_r": shadow_outcome.get("gross_r"), "event": event,
            "traded": traded,
        },
        "mechanical": mechanical,
    })


def record_selection(store, reader, *, now, keys, summary):
    """Every top-K pick of the last 40 days whose shadow outcome exists and is not recorded."""
    repository, cursor = store.repo, 0
    while True:
        page = report_v3_cycle_started(repository, after_event_seq=cursor, limit=CYCLE_PAGE)
        for started in page:
            if started["recorded_at"] < now - LOOKBACK:
                continue
            _record_cycle(store, reader, started["body"]["cycle_id"], now=now, keys=keys,
                          summary=summary)
        if len(page) < CYCLE_PAGE:
            return
        cursor = page[-1]["event_seq"]


def _record_cycle(store, reader, cycle_id, *, now, keys, summary):
    repository = store.repo
    ranking, decisions, qualities = _cycle_reviews(repository, cycle_id)
    if ranking is None or ranking.get("policy") not in TOPK_POLICIES:
        return
    entries = {e.get("item_key"): e for e in ranking.get("entries") or []}
    picks = [p for p in cycle_picks(repository, cycle_id, now=now) if not p.engineering]
    shadows = existing_shadow_outcomes(repository, cycle_id)
    for pick in picks:
        key = selection_key(pick.cycle_id, pick.item_key, pick.revision)
        if key in keys:
            summary["already_recorded"] += 1
            continue
        shadow = shadows.get(pick.item_key)
        if shadow is None:
            summary["selection_pending"] += 1
            continue
        try:
            mechanical = breakout_flag(_hour_bars(reader, pick.symbol, pick.generated_at),
                                       pick.generated_at)
        except ShadowDataError:
            summary["invalid"] += 1
            continue
        except Exception:  # noqa: BLE001 -- a bar transport failure: this pick only, retried.
            summary["bar_fetch_failed"] += 1
            continue
        try:
            body = selection_record(
                pick, entries.get(pick.item_key), decisions.get((pick.item_key, pick.revision)),
                qualities.get((pick.item_key, pick.revision)), shadow,
                policy=ranking.get("policy"), k=ranking.get("k"), cycle_size=len(picks),
                mechanical=mechanical, traded=_traded(repository, pick.setup_id))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            summary["invalid"] += 1
            continue
        with store.transaction() as conn:
            store.event(conn, RECORD_EVENT, body, key=key)
        keys.add(key)
        summary["selection_recorded"] += 1


def maintenance_candidates(repository, *, since):
    """Answered ``MAINTENANCE_DECISION`` rows since ``since``, with their setup, by setup."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT e.event_seq, e.setup_id, e.body, e.recorded_at, s.symbol, s.record_json,
            t.body AS state FROM lab.managed_events e JOIN lab.managed_setups s USING(setup_id)
            LEFT JOIN lab.managed_states t USING(setup_id)
            WHERE e.kind='MAINTENANCE_DECISION' AND e.recorded_at >= %s
            AND jsonb_typeof(e.body->'answers')='object' AND e.body->'answers' <> '{}'::jsonb
            ORDER BY e.setup_id, e.event_seq""", (since,)).fetchall()
    by_setup = defaultdict(list)
    for row in rows:
        if not is_engineering(row["record_json"]):
            by_setup[row["setup_id"]].append(row)
    return by_setup


def _judgments(repository, receipt_ids):
    """``{receipt_id: answer}`` of the stored ``action`` judgment of these receipts."""
    if not receipt_ids:
        return {}
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT receipt_id::text AS receipt_id, answer_json FROM lab.ai_decisions
            WHERE receipt_id = ANY(%s::uuid[]) AND question='action'""",
            (list(receipt_ids),)).fetchall()
    return {row["receipt_id"]: row["answer_json"] for row in rows}


def _decision_time(row):
    body = row["body"]
    return _aware(body.get("decided_at") or row["recorded_at"])


def _confirmation(body, resolutions):
    """The confirmed question of a decision (V5: an invalidation streak's confirmation; V1-V4:
    a raised flag), with its flag and the flag's resolution; else None."""
    if body.get("policy_id") == V5_POLICY:
        effect = ((body.get("verdicts") or {}).get("invalidation_met") or {}).get("effect")
        if effect != "CONFIRMED":
            return None
        flag = next((f for f in body.get("flags") or []
                     if f.get("question") == "invalidation_met"), {})
        question = "invalidation_met"
    elif body.get("outcome") == "FLAGGED":
        flag, question = {"flag_id": body.get("flag_id")}, "action:" + FLAG_ACTION
    else:
        return None
    flag_id = flag.get("flag_id")
    return {"question": question, "flag_id": flag_id,
            "flag_resolution": resolutions.get(flag_id)}


def _resolutions(repository, setup_id):
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind='EXIT_FLAG_RESOLVED'""", (setup_id,)).fetchall()
    return {row["body"].get("flag_id"): row["body"].get("outcome") for row in rows}


def maintenance_record(row, bars, judgment, resolutions):
    """The ``JEV_CALIBRATION_RECORD`` body of one answered review (pure apart from ``bars``)."""
    body, state = row["body"], row["state"] or {}
    at = _decision_time(row)
    levels = trade_plan.initial_levels(
        {k: D(str(v)) for k, v in row["record_json"]["levels"].items()}, state)
    risk = D(str(body["risk_per_coin"])) if body.get("risk_per_coin") is not None else (
        levels["max_entry_price"] - levels["stop"])
    entry = D(str(body["entry"])) if body.get("entry") is not None else levels["max_entry_price"]
    if risk <= 0 or entry <= 0:
        raise ValueError("NONPOSITIVE_RISK")
    if body.get("policy_id") == V5_POLICY:
        forecasts, source = v5_forecasts(body), "DECISION_ANSWERS"
    else:
        forecasts, source = action_forecast(body, judgment)
    confirmed = _confirmation(body, resolutions)
    outcome = maintenance_outcome(bars, at, entry=entry, risk=risk, plan_stop=levels["stop"],
                                  plan_target=levels["target"], confirmed=confirmed)
    for forecast in forecasts:
        forecast["event_value"] = outcome["event"]
    return json_safe({
        "calibration_version": CALIBRATION_VERSION, "source": MAINTENANCE,
        "source_key": maintenance_key(body["request_id"]), "stated_at": at,
        "setup_id": str(row["setup_id"]), "symbol": row["symbol"], "arm": state.get("arm"),
        "lifecycle_id": body.get("lifecycle_id"), "request_id": body["request_id"],
        "decision_event_seq": row["event_seq"], "policy_id": body.get("policy_id"),
        "decision_outcome": body.get("outcome"), "decision_action": body.get("action"),
        "receipt_ids": list(body.get("receipt_ids") or []),
        "asked_bar_end": body.get("asked_bar_end"), "state_hash": body.get("state_hash"),
        "context_hash": body.get("context_hash"), "probability_source": source,
        "levels": {"entry": entry, "risk_per_coin": risk, "plan_stop": levels["stop"],
                   "plan_target": levels["target"]},
        "forecasts": forecasts, "outcome": outcome,
    })


def record_maintenance(store, reader, *, now, keys, summary):
    repository = store.repo
    for setup_id, rows in maintenance_candidates(repository, since=now - LOOKBACK).items():
        todo = []
        for row in rows:
            key = maintenance_key(row["body"].get("request_id"))
            if key in keys:
                summary["already_recorded"] += 1
            elif now < _decision_time(row) + OUTCOME_HORIZON + BAR_FETCH_BUFFER:
                summary["maintenance_pending"] += 1
            else:
                todo.append((key, row))
        if not todo:
            continue
        symbol = rows[0]["symbol"]
        start = min(_decision_time(row) for _, row in todo)
        end = min(max(_decision_time(row) for _, row in todo) + OUTCOME_HORIZON
                  + BAR_FETCH_BUFFER, now)
        try:
            bars = _Bars(parse_bars(reader.minute_bars(symbol, start, end)))
        except ShadowDataError:
            summary["invalid"] += len(todo)
            continue
        except Exception:  # noqa: BLE001 -- a bar transport failure: this setup only, retried.
            summary["bar_fetch_failed"] += len(todo)
            continue
        finals = [r["body"]["receipt_ids"][-1] for _, r in todo
                  if r["body"].get("policy_id") != V5_POLICY and r["body"].get("receipt_ids")]
        judgments = _judgments(repository, finals)
        resolutions = _resolutions(repository, setup_id)
        records = []
        for key, row in todo:
            receipts = row["body"].get("receipt_ids") or []
            try:
                records.append((key, maintenance_record(
                    row, bars, judgments.get(receipts[-1]) if receipts else None,
                    resolutions)))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                summary["invalid"] += 1
        with store.transaction() as conn:
            for key, body in records:
                store.event(conn, RECORD_EVENT, body, key=key)
                keys.add(key)
                summary["maintenance_recorded"] += 1


def record_calibration(store, reader, *, now):
    """The nightly step: append every complete record not yet recorded; counts."""
    now = _aware(now)
    summary = Counter({name: 0 for name in (
        "selection_recorded", "selection_pending", "maintenance_recorded",
        "maintenance_pending", "already_recorded", "bar_fetch_failed", "invalid")})
    keys = recorded_keys(store.repo)
    record_selection(store, reader, now=now, keys=keys, summary=summary)
    record_maintenance(store, reader, now=now, keys=keys, summary=summary)
    return dict(summary)


# --- Reading the records -------------------------------------------------------------------------


def calibration_records(repository, *, until=None, source=None):
    """Recorded bodies stated before ``until`` (all when None), oldest first."""
    with repository.connect() as conn:
        rows = conn.execute(
            "SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
            (RECORD_EVENT,)).fetchall()
    bodies = []
    for row in rows:
        body = row["body"]
        if source is not None and body.get("source") != source:
            continue
        if until is not None and _aware(body["stated_at"]) >= until:
            continue
        bodies.append(body)
    return bodies


def stated_keys(repository, start, end):
    """``{source: set(key)}`` of the probabilities Jev stated in ``[start, end)`` (``start``
    None: from the beginning): top-K reviews of report-V3 picks and answered maintenance
    reviews of non-engineering setups. The record each would carry has the same key."""
    with repository.connect() as conn:
        selection = conn.execute(
            """SELECT body->>'cycle_id' AS cycle_id, body->>'item_key' AS item_key,
            body->>'revision' AS revision FROM lab.managed_events
            WHERE kind='RESEARCH_DECISION' AND body->>'selection_policy' = ANY(%s)
            AND jsonb_typeof(body->'answers')='object' AND body->'answers' <> '{}'::jsonb
            AND (%s::timestamptz IS NULL OR recorded_at >= %s) AND recorded_at < %s""",
            (sorted(TOPK_POLICIES), start, start, end)).fetchall()
        maintenance = conn.execute(
            """SELECT e.body->>'request_id' AS request_id, s.record_json
            FROM lab.managed_events e JOIN lab.managed_setups s USING(setup_id)
            WHERE e.kind='MAINTENANCE_DECISION' AND jsonb_typeof(e.body->'answers')='object'
            AND e.body->'answers' <> '{}'::jsonb
            AND (%s::timestamptz IS NULL OR e.recorded_at >= %s) AND e.recorded_at < %s""",
            (start, start, end)).fetchall()
    return {
        SELECTION: {selection_key(r["cycle_id"], r["item_key"], r["revision"])
                    for r in selection},
        MAINTENANCE: {maintenance_key(r["request_id"]) for r in maintenance
                      if not is_engineering(r["record_json"])},
    }


def day_counts(repository, start, end):
    """The scorecard's ``jev`` line: probabilities stated in ``[start, end)`` by source, how
    many have their outcome recorded and how many are pending, and the records to date."""
    stated = stated_keys(repository, start, end)
    keys = recorded_keys(repository)
    lines = {}
    for source in (SELECTION, MAINTENANCE):
        joined = len(stated[source] & keys)
        lines[source.lower()] = {"stated": len(stated[source]), "outcomes_recorded": joined,
                                 "outcomes_pending": len(stated[source]) - joined}
    return {"calibration_version": CALIBRATION_VERSION, **lines,
            "records_to_date": len(keys)}


# --- The weekly section (pure over records) -----------------------------------------------------


def _bin_index(p):
    for index, (low, high) in enumerate(BINS):
        if low <= p < high or (index == len(BINS) - 1 and p == high):
            return index
    raise ValueError("PROBABILITY_OUT_OF_RANGE")


def reliability(pairs):
    """``pairs`` of ``(p, y)``: the 5-bin table, Brier, base-rate Brier and skill."""
    table = [{"bin": f"{low}-{high}", "count": 0, "mean_p": None, "observed": None}
             for low, high in BINS]
    sums = [[D(0), 0] for _ in BINS]
    for p, y in pairs:
        index = _bin_index(p)
        table[index]["count"] += 1
        sums[index][0] += p
        sums[index][1] += 1 if y else 0
    for row, (p_sum, hits) in zip(table, sums, strict=True):
        if row["count"]:
            row["mean_p"] = _q(p_sum / row["count"])
            row["observed"] = _q(D(hits) / row["count"])
    n = len(pairs)
    if not n:
        return table, None, None, None, None
    base = D(sum(1 for _, y in pairs if y)) / n
    brier = sum(((p - (1 if y else 0)) ** 2 for p, y in pairs), D(0)) / n
    base_brier = sum(((base - (1 if y else 0)) ** 2 for _, y in pairs), D(0)) / n
    skill = None if base_brier == 0 else 1 - brier / base_brier
    return table, base, brier, base_brier, skill


def question_tables(records):
    """One entry per (source, question, version)."""
    groups = defaultdict(lambda: {"stated": 0, "pairs": [], "untriggered": 0,
                                  "data_incomplete": 0})
    for body in records:
        source = body["source"]
        outcome = body.get("outcome") or {}
        for forecast in body.get("forecasts") or []:
            group = groups[(source, forecast["question"], forecast.get("version") or "NONE")]
            group["stated"] += 1
            if source == SELECTION:
                y = outcome.get("event")
                if y is None:
                    if outcome.get("data_complete") and not outcome.get("triggered"):
                        group["untriggered"] += 1
                    else:
                        group["data_incomplete"] += 1
                    continue
            else:
                y = forecast.get("event_value")
                if y is None:
                    group["data_incomplete"] += 1
                    continue
            group["pairs"].append((D(str(forecast["p"])), bool(y)))
    entries = []
    for (source, question, version), group in sorted(groups.items()):
        table, base, brier, base_brier, skill = reliability(group["pairs"])
        minimum = MINIMUMS[source]
        n = len(group["pairs"])
        entries.append({
            "source": source, "question": question, "version": version,
            "event": SELECTION_EVENT if source == SELECTION else STOP_EVENT,
            "stated": group["stated"], "with_outcome": n,
            "untriggered": group["untriggered"], "data_incomplete": group["data_incomplete"],
            "minimum": minimum, "status": "MEASURED" if n >= minimum else "NOT_ENOUGH_DATA",
            "base_rate": _q(base), "brier": _q(brier), "base_rate_brier": _q(base_brier),
            "brier_skill": _q(skill), "reliability": table,
        })
    return entries


def _pick_value(body):
    """Per-pick net R for the lift: the shadow net R, 0 when the shadow never filled (no trade,
    no fee), None when its data is incomplete."""
    outcome = body["outcome"]
    if not outcome.get("data_complete"):
        return None
    if not outcome.get("triggered"):
        return D(0)
    return D(str(outcome["net_r"])) if outcome.get("net_r") is not None else None


def complete_cycles(records):
    """``{cycle_id: [pick]}`` of cycles whose every pick is recorded with a complete value."""
    cycles = defaultdict(list)
    for body in records:
        if body["source"] == SELECTION:
            cycles[body["cycle_id"]].append(body)
    complete = {}
    for cycle_id, bodies in cycles.items():
        size = bodies[0].get("cycle_picks") or 0
        values = [_pick_value(b) for b in bodies]
        if len(bodies) == size and None not in values:
            complete[cycle_id] = [{**b, "value": v} for b, v in zip(bodies, values, strict=True)]
    return complete


def jev_top(picks, k):
    return [p for p in picks if p["ranking"]["status"] == RANKED
            and p["ranking"]["jev_rank"] is not None and p["ranking"]["jev_rank"] <= k]


def agent_top(picks, k):
    ordered = sorted(picks, key=lambda p: (p["ranking"]["agent_rank"] is None,
                                           p["ranking"]["agent_rank"] or 0, p["item_key"]))
    return ordered[:k]


def mechanical_top(picks, k):
    if any((p.get("mechanical") or {}).get("status") != "COMPUTED" for p in picks):
        return None
    flat = [{**p, "agent_rank": p["ranking"]["agent_rank"]} for p in picks]
    return mechanical_order(flat)[:k]


def _set_line(values, triggered):
    return {"picks": len(values), "mean_net_r_per_pick": _q(sum(values, D(0)) / len(values))
            if values else None, "triggered": len(triggered),
            "mean_net_r_triggered": _q(sum(triggered, D(0)) / len(triggered))
            if triggered else None}


def _pool(sets):
    values = [p["value"] for chosen in sets for p in chosen]
    triggered = [p["value"] for chosen in sets for p in chosen if p["outcome"]["triggered"]]
    return values, triggered


def ranking_lift(records, *, k=None):
    """Jev's top K against the agent's own top K, every pick (random K's expected value) and
    the mechanical baseline's top K, on the same complete cycles. ``k`` overrides each
    cycle's recorded K (the threshold notes)."""
    cycles = complete_cycles(records)
    jev, agent, everyone, mechanical, mech_cycles = [], [], [], [], 0
    larger = 0
    for picks in cycles.values():
        size = k or picks[0].get("k") or 0
        jev.append(jev_top(picks, size))
        agent.append(agent_top(picks, size))
        everyone.append(picks)
        larger += len(picks) > size
        top = mechanical_top(picks, size)
        if top is not None:
            mech_cycles += 1
            mechanical.append((jev[-1], top))
    lines = {name: _set_line(*_pool(sets)) for name, sets in (
        ("jev_top_k", jev), ("agent_top_k", agent), ("random_k_expected", everyone))}
    jev_mean = lines["jev_top_k"]["mean_net_r_per_pick"]

    def lift(other):
        return None if jev_mean is None or other is None else _q(jev_mean - other)

    result = {
        "cycles": len(cycles), "cycles_with_more_picks_than_k": larger,
        "k": k or "RECORDED_PER_CYCLE", "value": "SHADOW_NET_R_PER_PICK_UNFILLED_AS_ZERO",
        **lines,
        "lift_vs_agent": lift(lines["agent_top_k"]["mean_net_r_per_pick"]),
        "lift_vs_random": lift(lines["random_k_expected"]["mean_net_r_per_pick"]),
        "minimum": LIFT_MINIMUM,
        "status": "MEASURED" if lines["jev_top_k"]["picks"] >= LIFT_MINIMUM
        else "NOT_ENOUGH_DATA",
    }
    if mech_cycles:
        jev_same = _set_line(*_pool([j for j, _ in mechanical]))
        mech = _set_line(*_pool([m for _, m in mechanical]))
        result["mechanical"] = {
            "method": MECHANICAL, "cycles": mech_cycles, "jev_top_k_same_cycles": jev_same,
            "mechanical_top_k": mech,
            "lift_vs_mechanical": None if None in (jev_same["mean_net_r_per_pick"],
                                                   mech["mean_net_r_per_pick"])
            else _q(D(jev_same["mean_net_r_per_pick"]) - D(mech["mean_net_r_per_pick"])),
        }
    else:
        result["mechanical"] = {
            "method": MECHANICAL, "cycles": 0, "omitted": True,
            "reason": "No complete cycle had the baseline computed for every pick "
                      "(INSUFFICIENT_BARS or no cycle)."}
    return result


def confirmed_exits(records):
    """Confirmed invalidations (V5) and V1-V4 flags: exiting at the decision against holding
    to the plan for 24 hours, by version."""
    groups = defaultdict(list)
    for body in records:
        confirmed = ((body.get("outcome") or {}).get("confirmed")
                     if body["source"] == MAINTENANCE else None)
        if confirmed and confirmed.get("exit_minus_hold_r") is not None:
            groups[(body.get("policy_id") or "NONE", confirmed["question"])].append(
                D(str(confirmed["exit_minus_hold_r"])))
    return [{"version": version, "question": question, "count": len(values),
             "mean_exit_minus_hold_r": _q(sum(values, D(0)) / len(values)),
             "exits_that_saved_r": sum(1 for v in values if v > 0)}
            for (version, question), values in sorted(groups.items())]


def _threshold_rows(pairs):
    rows = []
    for threshold in YES_THRESHOLDS:
        chosen = [y for p, y in pairs if p >= threshold]
        rows.append({"rule": f"YES_AT_P>={threshold}", "answers": len(chosen),
                     "event_rate": _q(D(sum(chosen)) / len(chosen)) if chosen else None,
                     "current": threshold == CURRENT_YES})
    for threshold in NO_THRESHOLDS:
        chosen = [y for p, y in pairs if p <= threshold]
        rows.append({"rule": f"NO_AT_P<={threshold}", "answers": len(chosen),
                     "event_rate": _q(D(sum(chosen)) / len(chosen)) if chosen else None,
                     "current": threshold == CURRENT_NO})
    return rows


def thresholds_suggestion(records, lift):
    """Record-only notes: what MAINTENANCE_ANSWER_RULE_V3's 0.80/0.20 and the recorded K
    produced against alternatives. Never applied."""
    by_question = defaultdict(list)
    for body in records:
        if body["source"] != MAINTENANCE or body.get("policy_id") != V5_POLICY:
            continue
        for forecast in body.get("forecasts") or []:
            if forecast.get("event_value") is not None:
                by_question[forecast["question"]].append(
                    (D(str(forecast["p"])), bool(forecast["event_value"])))
    maintenance, text = [], []
    for question in V5_QUESTIONS:
        pairs = by_question.get(question, [])
        base = _q(D(sum(y for _, y in pairs)) / len(pairs)) if pairs else None
        rows = _threshold_rows(pairs)
        status = ("MEASURED" if len(pairs) >= MINIMUMS[MAINTENANCE] else "NOT_ENOUGH_DATA")
        maintenance.append({"question": question, "version": V5_POLICY, "answers": len(pairs),
                            "base_rate": base, "status": status, "rules": rows})
        parts = ", ".join(f"{r['rule']} {r['answers']} answers (event rate "
                          f"{r['event_rate'] if r['event_rate'] is not None else 'n/a'})"
                          for r in rows)
        text.append(f"{question} ({V5_POLICY}, {len(pairs)} reviews with outcomes, base rate "
                    f"{base if base is not None else 'n/a'}, {status}): {parts}.")
    k_rows = []
    for k in K_ALTERNATIVES:
        line = ranking_lift(records, k=k)
        k_rows.append({"k": k, "jev_top_k": line["jev_top_k"],
                       "lift_vs_random": line["lift_vs_random"], "status": line["status"]})
    text.append("K (recorded per cycle): Jev's top K "
                f"{lift['jev_top_k']['mean_net_r_per_pick']} R per pick over "
                f"{lift['jev_top_k']['picks']} picks; " + "; ".join(
                    f"K={row['k']}: {row['jev_top_k']['mean_net_r_per_pick']} over "
                    f"{row['jev_top_k']['picks']}" for row in k_rows) + ".")
    text.append("Record-only: nothing here changes a threshold or K; a change needs the "
                "owner's approval, a named version, tests and a CONTRACT-RESOLUTIONS entry.")
    return {"current": {"yes": str(CURRENT_YES), "no": str(CURRENT_NO),
                        "answer_rule": "MAINTENANCE_ANSWER_RULE_V3", "k": "RECORDED_PER_CYCLE"},
            "maintenance": maintenance, "k_alternatives": k_rows, "text": text}


def calibration_section(repository, *, until):
    """``WEEKLY_REVIEW_V1``'s ``jev_calibration`` section over every record stated before
    ``until`` (the evidence accumulates, as every other test of the review).

    * ``questions``: per (source, question, version), the event it predicts, counts, a 5-bin
      reliability table (``[0, 0.2)`` ... ``[0.8, 1]``: count, mean stated p, observed
      frequency), Brier = mean (p - y)^2, the base-rate Brier (always forecasting the observed
      frequency) and the skill 1 - Brier / base-rate Brier; ``NOT_ENOUGH_DATA`` below 40
      selection or 30 maintenance outcomes.
    * ``ranking_lift``: on cycles whose every pick is recorded, mean shadow net R per pick (a
      pick the shadow never filled counts 0) of Jev's top K (ranked, rank <= K) against the
      agent's own top K, all picks (random K's expected value) and, where every pick of the
      cycle has it, the mechanical baseline's top K.
    * ``confirmed_exits``, ``thresholds_suggestion`` (record-only) and ``pending``.
    """
    records = calibration_records(repository, until=until)
    tables = question_tables(records)
    lift = ranking_lift(records)
    stated = stated_keys(repository, None, until)
    recorded = {body["source_key"] for body in records}
    return json_safe({
        "calibration_version": CALIBRATION_VERSION, "events": EVENTS,
        "bins": [f"{low}-{high}" for low, high in BINS], "minimums": MINIMUMS,
        "records": {SELECTION.lower(): sum(1 for b in records if b["source"] == SELECTION),
                    MAINTENANCE.lower(): sum(1 for b in records
                                             if b["source"] == MAINTENANCE)},
        "pending": {source.lower(): len(stated[source] - recorded)
                    for source in (SELECTION, MAINTENANCE)},
        "questions": tables, "ranking_lift": lift, "confirmed_exits": confirmed_exits(records),
        "thresholds_suggestion": thresholds_suggestion(records, lift),
        "limitations": list(LIMITATIONS),
    })


__all__ = [
    "BINS", "CALIBRATION_VERSION", "EVENTS", "MECHANICAL", "MINIMUMS", "RECORD_EVENT",
    "action_forecast", "breakout_flag", "calibration_records", "calibration_section",
    "choice_forecasts", "complete_cycles", "day_counts", "maintenance_outcome",
    "maintenance_record", "ranking_lift", "record_calibration", "reliability",
    "selection_record", "stated_keys", "v5_forecasts",
]
