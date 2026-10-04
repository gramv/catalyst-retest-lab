"""``LEARNING_LOOP_WEEKLY_V1``: the weekly speed of the learning loop -- decide what to propose
(package learning-loop2, 2026-10-03; owner direction of 2026-10-03, two speeds: the daily brief
observes and explains, the weekly review decides and **may propose** rule changes).

**Proposals only, never applied.** An additive section ``learning`` of ``WEEKLY_REVIEW_V1``.
Every item reads the evidence recorded before the week's end; trades of ``STATS_EXCLUSION_V1``
days are left out of every trade figure here (their count is shown), while market and mover
figures are unaffected. Nothing here changes a rule, a threshold, a Jev question, sizing or an
order: a ``PROPOSED_NOT_APPLIED`` item names its evidence and says what it would need -- a
history test, a named version recorded in ``docs/CONTRACT-RESOLUTIONS.md`` and the owner's yes.

Sections:

* ``patterns``: recurring observations counted over the evidence to date; a pattern is
  ``REACHED`` at ``PATTERN_MINIMUM`` (10) occurrences (the research checklist's bar), else
  ``BELOW_MINIMUM``. The catalog is fixed: after-exit recoveries of stop-outs, mover causes and
  knowable-before-move movers (post-mortems), sector clusters (daily briefs), mechanical signals
  on movers and missed-tradeable movers (``MISSED_TRADEABLE_V1``), and regime day tags.
* ``strategy_fit_by_regime``: per ``strategy_id`` and ``MARKET_REGIME_V1`` dimension (the day
  tag and its four parts): live paper trades (net R over fee-verified trades,
  ``result_dimensions.cell``) and shadow outcomes (the mechanical strategies' simulations, by
  the regime of the signal's day), each cell ``NOT_ENOUGH_DATA`` below ``CELL_MINIMUM`` (30).
* ``shadow_vs_live``: per strategy, both figures side by side, labelled.
* ``jev_calibration_summary``: the Brier skill and status of each Jev question and the
  ranking lift (``JEV_CALIBRATION_V1``), compact.
* ``missed_tradeable_by_regime``: the ``MISSED_TRADEABLE_V1`` totals per day tag and part.
* ``management_split`` (``MANAGEMENT_CHANGE_CONTEXT_V1``) and ``after_exit``
  (``AFTER_EXIT_PATH_V1``).
* ``proposals``: the fixed proposal rules below; each item ``PROPOSED_NOT_APPLIED``.

Proposal rules (fixed before the data; each needs a ``MEASURED`` cell):

1. ``STRATEGY_REGIME_GATE``: a live strategy's regime-part cell with mean net R below zero
   while the strategy's other values of that part are at or above zero.
2. ``SHADOW_PROMOTION_REVIEW``: a shadow strategy with at least 30 simulated trades and mean
   net R after slippage (else after fees) above zero.
3. ``MANAGEMENT_BUCKET``: a management-split cell with mean R difference below zero (the
   decisions in that bucket lost value against keeping the levels).
4. ``STOP_RULE_REVIEW``: stop-outs' after-exit cell measured and at least half of them reached
   1R above the exit within 24 hours.
5. ``MISSED_TRADEABLE_REVIEW``: at least 10 missed-tradeable movers and the simulated entries'
   mean net R above zero.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from catalyst_lab import strategies
from catalyst_lab.daily_brief import BRIEF_EVENT, MISSED_EVENT
from catalyst_lab.learning_intake import POST_MORTEM_EVENT
from catalyst_lab.market import NY
from catalyst_lab.market_regime import recorded_regimes
from catalyst_lab.result_dimensions import CELL_MINIMUM, DAY_PARTS, cell, regime_value
from catalyst_lab.strategies import core
from catalyst_lab.strategy_shadow import OUTCOME_EVENT
from catalyst_lab.trade_paths import after_exit_summary, management_split, recorded_paths

D = Decimal
LEARNING_VERSION = "LEARNING_LOOP_WEEKLY_V1"
SPEED = "WEEKLY_DECIDE_PROPOSE_ONLY"
PATTERN_MINIMUM = 10
MISSED_MINIMUM = 10
REGIME_DIMENSIONS = ("day_tag", *DAY_PARTS)
TRADED = frozenset({core.STOP, core.TARGET, core.HOLD_EXIT})
PROPOSAL_STATUS = "PROPOSED_NOT_APPLIED"
REQUIRES = ("HISTORY_TEST", "NAMED_VERSION_IN_CONTRACT_RESOLUTIONS", "OWNER_APPROVAL")
FOUR = D("0.0001")


def _q(value):
    if value is None:
        return None
    rounded = value.quantize(FOUR, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _mean(values):
    values = list(values)
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return _q(sum(values, D(0)) / len(values))


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


def _events(conn, kind):
    return [row["body"] for row in conn.execute(
        "SELECT body FROM lab.managed_events WHERE kind=%s AND setup_id IS NULL "
        "ORDER BY event_seq", (kind,)).fetchall()]


# --- Shadow cells by regime ---------------------------------------------------------------------


def shadow_cell(results, minimum=CELL_MINIMUM):
    traded = [r for r in results if r.get("outcome") in TRADED and r.get("net_r") is not None]
    nets = [D(str(r["net_r"])) for r in traded]
    gross = [D(str(r["gross_r"])) for r in traded if r.get("gross_r") is not None]
    slipped = [D(str(r["net_r_after_slippage"])) for r in traded
               if r.get("net_r_after_slippage") is not None]
    wins = sum(1 for v in gross if v > 0)
    return {"label": "SHADOW", "outcomes": len(results), "trades": len(traded),
            "win_rate": _q(D(wins) / len(gross)) if gross else None,
            "mean_net_r": _mean(nets), "mean_net_r_after_slippage": _mean(slipped),
            "slippage_trades": len(slipped),
            "status": "MEASURED" if len(traded) >= minimum else "NOT_ENOUGH_DATA"}


def shadow_items(outcomes, tags, until):
    """``[(strategy_id, regime-like dict, result)]`` of shadow outcomes signalled before
    ``until``, the regime being the recorded day tag of the signal's New York day."""
    items = []
    for body in outcomes:
        at = _aware(body["signal_at"])
        if at >= until:
            continue
        tag = tags.get(at.astimezone(NY).date().isoformat())
        items.append((body["strategy_id"], {"day_tag": tag} if tag else None,
                      body.get("result") or {}))
    return items


def strategy_fit(trades, shadow, minimum=CELL_MINIMUM):
    """Per strategy and regime dimension: live paper cells and shadow cells."""
    ids = sorted({t.get("strategy_id") or strategies.DEFAULT_STRATEGY_ID for t in trades}
                 | {sid for sid, _, _ in shadow})
    result = {}
    for sid in ids:
        live = [t for t in trades if (t.get("strategy_id")
                                      or strategies.DEFAULT_STRATEGY_ID) == sid]
        sim = [(regime, r) for s, regime, r in shadow if s == sid]
        entry = {"live_paper": {}, "shadow": {}}
        for dimension in REGIME_DIMENSIONS:
            groups = {}
            for trade in live:
                groups.setdefault(regime_value(trade.get("regime"), dimension), []).append(trade)
            entry["live_paper"][dimension] = {k: cell(v, minimum)
                                              for k, v in sorted(groups.items())}
            shadow_groups = {}
            for regime, outcome in sim:
                shadow_groups.setdefault(regime_value(regime, dimension), []).append(outcome)
            entry["shadow"][dimension] = {k: shadow_cell(v, minimum)
                                          for k, v in sorted(shadow_groups.items())}
        entry["live_paper_all"] = cell(live, minimum)
        entry["shadow_all"] = shadow_cell([r for _, r in sim], minimum)
        result[sid] = entry
    return result


def shadow_vs_live(fit):
    return {sid: {"live_paper": entry["live_paper_all"], "shadow": entry["shadow_all"],
                  "labels": {"live_paper": "closed paper trades, net R after verified fees",
                             "shadow": "simulated on 1-minute bars with an assumed fee; no "
                                       "order"}}
            for sid, entry in fit.items()}


# --- Jev, missed tradeable, patterns ------------------------------------------------------------


def jev_summary(section):
    if not section:
        return None
    lift = section.get("ranking_lift") or {}
    return {"calibration_version": section.get("calibration_version"),
            "records": section.get("records"), "pending": section.get("pending"),
            "questions": [{k: q.get(k) for k in ("source", "question", "version", "status",
                                                 "with_outcome", "minimum", "brier",
                                                 "base_rate_brier", "brier_skill")}
                          for q in section.get("questions") or []],
            "ranking_lift": {k: lift.get(k) for k in ("status", "cycles", "lift_vs_agent",
                                                      "lift_vs_random", "minimum")}}


def missed_by_regime(records, minimum=MISSED_MINIMUM):
    def line(members):
        totals = [m["totals"] for m in members]
        entries = sum(t["simulated_entries"] for t in totals)
        sums = [D(str(t["sum_net_r"])) for t in totals if t.get("sum_net_r") is not None]
        total = sum(sums, D(0))
        missed = sum(t["missed_tradeable"] for t in totals)
        return {"days": len(members), "movers": sum(t["movers"] for t in totals),
                "with_signal": sum(t["with_signal"] for t in totals),
                "simulated_entries": entries,
                "positive_entries": sum(t["positive_entries"] for t in totals),
                "mean_net_r": _q(total / entries) if entries else None,
                "missed_tradeable": missed,
                "status": "MEASURED" if missed >= minimum else "NOT_ENOUGH_DATA"}
    result = {"all": line(records), "by_day_tag": {}, "by_part": {}}
    groups = {}
    for record in records:
        groups.setdefault(record.get("day_tag") or "UNKNOWN", []).append(record)
    result["by_day_tag"] = {k: line(v) for k, v in sorted(groups.items())}
    for part in DAY_PARTS:
        parts = {}
        for record in records:
            value = (record.get("regime_parts") or {}).get(part) or "UNKNOWN"
            parts.setdefault(value, []).append(record)
        result["by_part"][part] = {k: line(v) for k, v in sorted(parts.items())}
    return result


def _pattern(name, occurrences, base, kind):
    return {"pattern": name, "occurrences": occurrences, "base": base,
            "share": _q(D(occurrences) / base) if base else None, "evidence": kind,
            "status": "REACHED" if occurrences >= PATTERN_MINIMUM else "BELOW_MINIMUM"}


def patterns(*, paths, notes, briefs, missed, regimes, excluded):
    found = []
    stops = [p for p in paths if p.get("exit_kind") == "STOP" and p["setup_id"] not in excluded
             and (p["horizons"].get("+24h") or {}).get("status") == "MEASURED"]
    recovered = sum(1 for p in stops if D(str(p["horizons"]["+24h"]["high_r"])) >= 1)
    found.append(_pattern("AFTER_EXIT_RECOVERY_STOP_1R_24H", recovered, len(stops),
                          "AFTER_EXIT_PATH_V1"))
    movers = [n for n in notes if n["subject_key"].startswith("MOVER:")]
    for cause in sorted({n.get("cause") for n in movers if n.get("cause")}):
        found.append(_pattern(f"MOVER_CAUSE:{cause}",
                              sum(1 for n in movers if n.get("cause") == cause), len(movers),
                              "POST_MORTEM_V1"))
    found.append(_pattern("MOVER_KNOWABLE_BEFORE_MOVE",
                          sum(1 for n in movers if n.get("knowable_before_move") is True),
                          len(movers), "POST_MORTEM_V1"))
    clusters = {}
    for brief in briefs:
        for cluster in brief["movers"]["sector_clusters"]:
            key = f"SECTOR_CLUSTER:{cluster['sector']}:{cluster['direction']}"
            clusters[key] = clusters.get(key, 0) + 1
    for key, count in sorted(clusters.items()):
        found.append(_pattern(key, count, len(briefs), "DAILY_BRIEF_V1"))
    lines = [line for record in missed for line in record["movers"]]
    for sid in sorted({s for record in missed for s in record.get("strategies") or []}):
        with_signal = [line for line in lines if (line.get("strategies") or {}).get(sid)]
        found.append(_pattern(f"MECHANICAL_SIGNAL_ON_MOVER:{sid}", len(with_signal),
                              len(lines), "MISSED_TRADEABLE_V1"))
    found.append(_pattern("MISSED_TRADEABLE_MOVER",
                          sum(1 for line in lines if line.get("missed_tradeable")), len(lines),
                          "MISSED_TRADEABLE_V1"))
    tags = {}
    for body in regimes:
        tags[body["tag"]] = tags.get(body["tag"], 0) + 1
    for tag, count in sorted(tags.items()):
        found.append(_pattern(f"REGIME_DAY:{tag}", count, len(regimes), "MARKET_REGIME_V1"))
    return {"minimum": PATTERN_MINIMUM, "reached": [p for p in found
                                                    if p["status"] == "REACHED"],
            "below_minimum": [p for p in found if p["status"] != "REACHED"]}


# --- Proposals (never applied) ------------------------------------------------------------------


def _proposal(proposal_id, rule, text, evidence):
    return {"proposal_id": proposal_id, "rule": rule, "status": PROPOSAL_STATUS,
            "evidence": evidence, "requires": list(REQUIRES),
            "text": text + " Not applied: a rule change needs a history test, a named version "
                    "in docs/CONTRACT-RESOLUTIONS.md and the owner's yes."}


def proposals(fit, management, after, missed):
    items = []
    for sid, entry in sorted(fit.items()):
        try:
            live = strategies.get(sid).live
        except ValueError:
            live = False
        if not live:
            continue
        for part in DAY_PARTS:
            cells = entry["live_paper"].get(part) or {}
            measured = {k: c for k, c in cells.items() if c["status"] == "MEASURED"
                        and c["mean_r_net"] is not None}
            for value, c in measured.items():
                others = [o for k, o in measured.items() if k != value]
                if D(str(c["mean_r_net"])) < 0 and others and all(
                        D(str(o["mean_r_net"])) >= 0 for o in others):
                    items.append(_proposal(
                        f"STRATEGY_REGIME_GATE:{sid}:{part}:{value}", "STRATEGY_REGIME_GATE",
                        f"{sid} loses on {part} {value} (mean net R {c['mean_r_net']} over "
                        f"{c['r_net_count']} trades) and not on its other {part} values: "
                        f"consider a named version of {sid} that does not trade then.",
                        {"strategy_id": sid, "dimension": part, "value": value, "cell": c}))
    for sid, entry in sorted(fit.items()):
        try:
            stage = strategies.get(sid).stage
        except ValueError:
            continue
        shadow = entry["shadow_all"]
        mean = shadow["mean_net_r_after_slippage"] if shadow["slippage_trades"] >= \
            CELL_MINIMUM else shadow["mean_net_r"]
        if stage == strategies.SHADOW and shadow["status"] == "MEASURED" and mean is not None \
                and D(str(mean)) > 0:
            items.append(_proposal(
                f"SHADOW_PROMOTION_REVIEW:{sid}", "SHADOW_PROMOTION_REVIEW",
                f"{sid} has {shadow['trades']} simulated trades with mean net R {mean} "
                "in shadow: consider a promotion review (paper path, per-strategy risk cap).",
                {"strategy_id": sid, "shadow": shadow}))
    for split in ("by_stop_distance", "by_time_in_trade", "by_day_tag"):
        for decision, cells in sorted((management.get(split) or {}).items()):
            for value, c in cells.items():
                if c["status"] == "MEASURED" and c["mean_r_difference"] is not None \
                        and D(str(c["mean_r_difference"])) < 0:
                    items.append(_proposal(
                        f"MANAGEMENT_BUCKET:{decision}:{split}:{value}", "MANAGEMENT_BUCKET",
                        f"{decision} decisions at {split.removeprefix('by_')} {value} lost "
                        f"{c['mean_r_difference']} R on average over {c['changes']} changes "
                        "against keeping the levels: consider a maintenance version that does "
                        "not make them there.",
                        {"decision": decision, "split": split, "value": value, "cell": c}))
    stops = (after.get("by_exit_kind") or {}).get("STOP")
    if stops and stops["status"] == "MEASURED" and stops["high_at_least_1r_share"] is not None \
            and D(str(stops["high_at_least_1r_share"])) >= D("0.5"):
        items.append(_proposal(
            "STOP_RULE_REVIEW", "STOP_RULE_REVIEW",
            f"{stops['high_at_least_1r']} of {stops['measured_24h']} stop-outs reached 1R above "
            "the exit within 24 hours: consider a named stop-rule version (wider stop or "
            "confirmation) tested on the recorded trades.", {"cell": stops}))
    overall = missed.get("all") or {}
    if overall.get("missed_tradeable", 0) >= MISSED_MINIMUM and overall.get("mean_net_r") \
            is not None and D(str(overall["mean_net_r"])) > 0:
        items.append(_proposal(
            "MISSED_TRADEABLE_REVIEW", "MISSED_TRADEABLE_REVIEW",
            f"{overall['missed_tradeable']} movers had a profitable mechanical entry we did not "
            f"take (mean net R {overall['mean_net_r']}): consider whether that strategy should "
            "climb the ladder.", {"totals": overall}))
    return items


# --- The section ---------------------------------------------------------------------------------


def learning_section(repository, trades, *, until, excluded, calibration=None):
    """The weekly review's ``learning`` section (see the module docstring). ``trades``: the
    review's ``closed_trades`` (``{setup_id: {...}}``); ``excluded``: the STATS_EXCLUSION_V1
    setup ids."""
    kept = {sid: t for sid, t in trades.items() if sid not in excluded}
    with repository.connect() as conn:
        regimes = [body for day, body in sorted(recorded_regimes(conn).items())
                   if day < until.astimezone(NY).date().isoformat()]
        outcomes = _events(conn, OUTCOME_EVENT)
        briefs = [b for b in _events(conn, BRIEF_EVENT)
                  if b["day"] < until.astimezone(NY).date().isoformat()]
        missed = [b for b in _events(conn, MISSED_EVENT)
                  if b["day"] < until.astimezone(NY).date().isoformat()]
        note_rows = conn.execute(
            """SELECT body->'items' AS items FROM lab.managed_events WHERE kind=%s
            AND setup_id IS NULL AND recorded_at < %s ORDER BY event_seq""",
            (POST_MORTEM_EVENT, until)).fetchall()
    latest = {}
    for row in note_rows:
        for item in row["items"] or []:
            latest[item["subject_key"]] = item
    tags = {body["day"]: body["tag"] for body in regimes}
    shadow = shadow_items(outcomes, tags, until)
    fit = strategy_fit(list(kept.values()), shadow)
    paths = recorded_paths(repository, until=until)
    after = after_exit_summary(paths, frozenset(excluded))
    management = management_split(repository, trades, frozenset(excluded))
    missed_totals = missed_by_regime(missed)
    return {
        "learning_version": LEARNING_VERSION, "speed": SPEED,
        "rule": "The weekly speed may propose rule changes; nothing here is applied. The daily "
                "speed (DAILY_BRIEF_V1) changes research attention only.",
        "stats_exclusions": {"excluded_trades": len(trades) - len(kept),
                             "scope": "TRADE_FIGURES_ONLY; market, movers and shadow "
                                      "figures unaffected"},
        "patterns": patterns(paths=paths, notes=list(latest.values()), briefs=briefs,
                             missed=missed, regimes=regimes, excluded=frozenset(excluded)),
        "strategy_fit_by_regime": {"minimum": CELL_MINIMUM, "dimensions":
                                   list(REGIME_DIMENSIONS), "strategies": fit},
        "shadow_vs_live": shadow_vs_live(fit),
        "jev_calibration_summary": jev_summary(calibration),
        "missed_tradeable_by_regime": missed_totals,
        "management_split": management,
        "after_exit": after,
        "proposals": proposals(fit, management, after, missed_totals),
    }


__all__ = ["LEARNING_VERSION", "PATTERN_MINIMUM", "learning_section", "missed_by_regime",
           "patterns", "proposals", "shadow_cell", "strategy_fit"]
