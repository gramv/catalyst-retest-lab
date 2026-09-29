"""``WEEKLY_REVIEW_V1``: the pre-registered tests of plan ``docs/LEARNING-LOOP-PLAN.md`` section 6
(package learning-app, 2026-09-28; owner decisions of 2026-09-28: the minimum samples exactly as
in the plan).

**The owner decides; nothing here changes a rule.** One immutable ``WEEKLY_REVIEW`` event per
Monday-to-Sunday New York week (``lab.managed_events``, no setup, key
``weekly-review:<week_end>``), recorded by the nightly job on the first run after the week ends
(normally Monday's), and computable on demand without recording. A ``PROPOSE`` verdict carries a
generated contract-text draft for a named version; it becomes a rule only through the owner's
approval, tests, a ``CONTRACT-RESOLUTIONS`` entry and a deploy.

Every test uses all the evidence recorded before the week's end (the evidence accumulates; one
week alone is mostly luck) and reads each trade's official R live (verified fees, never a
``LAB_FIXTURE`` source; ``ENGINEERING_TEST`` setups never count):

====================  ==========================================================  =============
Test                  Effect (positive = the current rule adds value)             Minimum
====================  ==========================================================  =============
SELECTION_VALUE       mean shadow net R of selected picks minus passed picks      40 and 40
MANAGEMENT_VALUE      mean official R of JEV_MANAGED minus FIXED_EXIT trades      30 per arm
LEVEL_MOVES_<TYPE>    mean (official R minus UNCHANGED_PLAN_REPLAY_V1 net R) per  30 changes
                      STOP_RAISE, TARGET_RAISE, STOP_AND_TARGET_RAISE
DAY_REVIEW_DECISIONS  mean (official R minus DAY_REVIEW_DECISION_REPLAY_V1 net R) 20 decisions
VETOES                mean shadow net R of passed picks minus vetoed picks        15 vetoed
====================  ==========================================================  =============

"Selected" picks were published (Jev's top K and replacements), "passed" were ranked but not
selected, "vetoed" were vetoed; only shadow outcomes with a complete, triggered net R count.
A trade admitted under the window versions (package review-window) is replayed by
``UNCHANGED_PLAN_REPLAY_V2`` and ``DAY_REVIEW_DECISION_REPLAY_V2`` (its window instead of 24
hours) and counts in the same tests.

**Interval.** A deterministic percentile bootstrap: 2,000 resamples with replacement (two groups
resampled independently; paired differences resampled as one group), drawn by a SplitMix64
generator seeded with the first 8 bytes of SHA-256 of ``WEEKLY_REVIEW_V1|<week_end>|<test>``;
values are exact to 1e-8; the 90% interval is the 100th and the 1,900th of the sorted resample
effects (nearest rank, 5% and 95%).

**Verdict.** ``NOT_ENOUGH_DATA`` below a minimum (or with fewer than two values in a group);
otherwise ``PROPOSE`` when the interval's upper end is below zero (the evidence says the current
rule loses value), else ``KEEP``. ``evidence``: ``ADDS_VALUE`` (lower end above zero),
``INCONCLUSIVE`` or ``LOSES_VALUE``.
"""

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256

from catalyst_lab.crypto_holding import REVIEW_POLICY_IDS
from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_replays import DAY_REPLAY_EVENT, REPLAY_EVENT, counterfactual_r
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market import NY
from catalyst_lab.pick_outcomes import SHADOW_EVENT_KIND, rank_bucket
from catalyst_lab.repository import json_safe
from catalyst_lab.scorecard import recorded_scorecard

D = Decimal
REVIEW_VERSION = "WEEKLY_REVIEW_V1"
REVIEW_EVENT = "WEEKLY_REVIEW"
TIMEZONE = "America/New_York"
RESAMPLES = 2000
LOWER_RANK, UPPER_RANK = 100, 1900  # Nearest rank: ceil(0.05 x 2000), ceil(0.95 x 2000).
SCALE = 10 ** 8
MASK = (1 << 64) - 1
LEVEL_TYPES = ("STOP_RAISE", "TARGET_RAISE", "STOP_AND_TARGET_RAISE")
MINIMUMS = {"SELECTION_VALUE": 40, "MANAGEMENT_VALUE": 30, "LEVEL_MOVES": 30,
            "DAY_REVIEW_DECISIONS": 20, "VETOES": 15}
FOUR = D("0.0001")
LIMITATIONS = (
    "Shadow outcomes are 1-minute-bar approximations with an assumed taker fee.",
    "Replays compare actual official R (verified fees) with counterfactuals priced with the "
    "assumed taker fee.",
    "A PROPOSE draft is a starting point for the owner's decision, not a backtest: a rule "
    "change still needs the owner's approval, tests and a CONTRACT-RESOLUTIONS entry.",
)


def _q(value):
    if value is None:
        return None
    rounded = value.quantize(FOUR, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


# --- The bootstrap (pure, deterministic) ---------------------------------------------------------


class SplitMix64:
    def __init__(self, seed):
        self.state = seed & MASK

    def next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def below(self, n):
        """An unbiased integer in ``[0, n)`` (rejection sampling)."""
        limit = ((1 << 64) // n) * n
        while True:
            value = self.next()
            if value < limit:
                return value % n


def seed_for(week_end, test_id):
    digest = sha256(f"{REVIEW_VERSION}|{week_end}|{test_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _units(values):
    return [int((value * SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)) for value in values]


def _mean_units(total, count):
    return D(total) / D(count) / SCALE


def bootstrap(groups, *, seed, resamples=RESAMPLES):
    """The 90% interval of ``mean(groups[0]) - mean(groups[1])`` (two groups) or of
    ``mean(groups[0])`` (one group), as ``(lower, upper)`` exact Decimals."""
    generator = SplitMix64(seed)
    units = [_units(group) for group in groups]
    effects = []
    for _ in range(resamples):
        means = []
        for values in units:
            total = sum(values[generator.below(len(values))] for _ in range(len(values)))
            means.append(_mean_units(total, len(values)))
        effects.append(means[0] - means[1] if len(means) == 2 else means[0])
    effects.sort()
    return effects[LOWER_RANK - 1], effects[UPPER_RANK - 1]


def mean(values):
    return sum(values, D(0)) / len(values) if values else None


def next_version(name, fallback):
    """``..._V<n+1>`` of a recorded version name (the proposal's name)."""
    match = re.fullmatch(r"(.*_V)(\d+)", name or "")
    return f"{match.group(1)}{int(match.group(2)) + 1}" if match else fallback


# --- Evidence (read-only) -------------------------------------------------------------------------


def shadow_samples(repository, until):
    """Complete, triggered shadow net R of non-engineering picks proposed before ``until``, by
    selection group; and the selection policies recorded (for a proposal's name)."""
    with repository.connect() as conn:
        rows = conn.execute("SELECT body FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (SHADOW_EVENT_KIND,)).fetchall()
    groups = {"selected": [], "passed": [], "vetoed": []}
    policies = Counter()
    for row in rows:
        pick, outcome = row["body"]["pick"], row["body"]["outcome"]
        try:
            if pick.get("engineering") or _aware(pick["generated_at"]) >= until:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if not outcome.get("data_complete") or outcome.get("net_r") is None:
            continue
        bucket = rank_bucket(pick)
        name = ("selected" if bucket in {"TOP_K", "REPLACEMENT"} else "passed"
                if bucket == "NOT_SELECTED" else "vetoed" if bucket == "VETOED" else None)
        if name is None:
            continue
        groups[name].append(D(str(outcome["net_r"])))
        if pick.get("selection_policy"):
            policies[pick["selection_policy"]] += 1
    return groups, policies


def closed_trades(repository, until):
    """``{setup_id: {arm, r_net, maintenance_policy, holding_policy}}`` of non-engineering
    managed trades closed before ``until``; ``r_net`` null without verified, non-fixture fees."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT s.setup_id, s.record_json, t.body AS state FROM lab.managed_setups s
            JOIN lab.managed_states t USING(setup_id) WHERE s.cohort=%s
            AND t.body->>'state'='CLOSED' ORDER BY s.event_seq""", (COHORT,),
        ).fetchall()
    trades = {}
    for row in rows:
        state = row["state"] or {}
        try:
            if is_engineering(row["record_json"]) or _aware(state["closed_at"]) >= until:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        measured = managed_measurement(repository, row["setup_id"])
        tainted = any(item.get("source") == "LAB_FIXTURE"
                      for item in measured.get("cost_evidence") or [])
        official = measured.get("official_r")
        holding = state.get("holding_policy")
        trades[str(row["setup_id"])] = {
            "arm": state.get("arm"),
            "r_net": None if tainted or official is None else D(str(official)),
            "maintenance_policy": state.get("maintenance_policy"),
            # The recorded holding policy's ID (the state keeps the whole record).
            "holding_policy": holding.get("policy_id") if isinstance(holding, dict) else None,
        }
    return trades


def replay_samples(repository, trades):
    """``(level: {type: [difference]}, day: [(decision, difference)])`` for the recorded replays
    of ``trades`` with a known official R."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT kind, body FROM lab.managed_events WHERE kind IN (%s, %s)
            AND setup_id IS NULL ORDER BY event_seq""", (REPLAY_EVENT, DAY_REPLAY_EVENT),
        ).fetchall()
    level, day = {name: [] for name in LEVEL_TYPES}, []
    for row in rows:
        body = row["body"]
        trade = trades.get(body.get("setup_id"))
        counterfactual = counterfactual_r(row["kind"], body)
        if trade is None or trade["r_net"] is None or counterfactual is None:
            continue
        difference = trade["r_net"] - counterfactual
        if row["kind"] == DAY_REPLAY_EVENT:
            day.append((body.get("decision"), difference))
        elif body.get("change_kind") in level:
            level[body["change_kind"]].append(difference)
    return level, day


# --- The tests ---------------------------------------------------------------------------------


def verdict(groups, minimums, *, seed):
    """``(effect, interval, evidence, verdict, shortfall)`` of one test."""
    short = [f"{name}:{len(values)}<{minimum}" for (name, values), minimum
             in zip(groups, minimums, strict=True) if len(values) < max(minimum, 2)]
    values = [v for _, v in groups]
    effect = (mean(values[0]) - mean(values[1]) if len(values) == 2 and values[0] and values[1]
              else mean(values[0]) if len(values) == 1 else None)
    if short:
        return effect, None, None, "NOT_ENOUGH_DATA", short
    lower, upper = bootstrap(values, seed=seed)
    evidence = ("ADDS_VALUE" if lower > 0 else "LOSES_VALUE" if upper < 0 else "INCONCLUSIVE")
    return effect, {"lower": _q(lower), "upper": _q(upper)}, evidence, (
        "PROPOSE" if upper < 0 else "KEEP"), []


def proposal_text(test_id, version, current, week_end, effect, interval, samples, change):
    return (
        f"### `{version}` (draft generated by {REVIEW_VERSION} for the week ending "
        f"{week_end}; owner approval required)\n\n"
        f"Evidence: {test_id}, effect {_q(effect)} R (90% interval {interval['lower']} to "
        f"{interval['upper']}), samples {samples}. The current version, "
        f"`{current or 'UNRECORDED'}`, loses value on this measure.\n\n"
        f"Proposed rule: {change}\n\n"
        f"Scope: setups admitted after the owner approves this version; every earlier trade "
        f"keeps the version it was admitted under, and `{current or 'the current version'}` "
        f"stays archived unchanged. The control arm, the shadow outcomes and every other rule "
        f"are unchanged. Before activation: a replay of the recorded evidence under the new "
        f"rule, fixture tests, a CONTRACT-RESOLUTIONS entry and a deploy.")


def _common(values):
    counter = Counter(v for v in values if v)
    return counter.most_common(1)[0][0] if counter else None


def run_tests(repository, week_end, *, until):
    """Every pre-registered test on the evidence recorded before ``until``."""
    selection, policies = shadow_samples(repository, until)
    trades = closed_trades(repository, until)
    level, day = replay_samples(repository, trades)
    selection_policy = _common(policies.elements())
    maintenance_policy = _common(t["maintenance_policy"] for t in trades.values())
    # The review versions (the 24-hour ones and, package review-window, the window one).
    holding_policy = _common(t["holding_policy"] for t in trades.values()
                             if t["holding_policy"] in REVIEW_POLICY_IDS)
    managed = [t["r_net"] for t in trades.values()
               if t["arm"] == "JEV_MANAGED" and t["r_net"] is not None]
    control = [t["r_net"] for t in trades.values()
               if t["arm"] == "FIXED_EXIT" and t["r_net"] is not None]
    specs = [
        ("SELECTION_VALUE", "Does Jev's selection add value?",
         "Selected vs passed picks, shadow net R",
         [("selected", selection["selected"]), ("passed", selection["passed"])],
         [MINIMUMS["SELECTION_VALUE"]] * 2, selection_policy,
         next_version(selection_policy, "JEV_TOP_K_SELECTION_NEXT"),
         "the top-K choice among picks Jev did not veto follows the agent's own order "
         "(agent_rank) instead of Jev's score; vetoes, K and every other part unchanged."),
        ("MANAGEMENT_VALUE", "Does Jev's management add value?",
         "Jev-managed vs control arm, actual net R",
         [("JEV_MANAGED", managed), ("FIXED_EXIT", control)],
         [MINIMUMS["MANAGEMENT_VALUE"]] * 2, maintenance_policy,
         next_version(maintenance_policy, "CRYPTO_MAINTENANCE_NEXT"),
         "the maintained arm keeps its admitted stop and target (no maintenance level "
         "changes and no Jev exit flags); the continue-or-exit review and the control arm "
         "unchanged."),
    ]
    for kind in LEVEL_TYPES:
        specs.append((
            f"LEVEL_MOVES_{kind}", f"Do Jev's {kind.lower().replace('_', ' ')}s help?",
            "Actual vs unchanged-plan replay, per decision type",
            [(kind, level[kind])], [MINIMUMS["LEVEL_MOVES"]], maintenance_policy,
            next_version(maintenance_policy, "CRYPTO_MAINTENANCE_NEXT"),
            f"maintenance no longer offers {kind} changes (the code keeps those levels); "
            "every other option, check and cadence unchanged."))
    continues = [d for decision, d in day if decision == "CONTINUE"]
    exits = [d for decision, d in day if decision == "EXIT"]
    worse = "CONTINUE" if (mean(continues) or 0) <= (mean(exits) or 0) else "EXIT"
    specs.append((
        "DAY_REVIEW_DECISIONS", "Is the continue-or-exit decision rule working?",
        "Continue vs exit decisions against their replays", [("decisions", [d for _, d in day])],
        [MINIMUMS["DAY_REVIEW_DECISIONS"]], holding_policy,
        next_version(holding_policy, "CRYPTO_24H_REVIEW_NEXT"),
        "every trade exits at its first review (no continuation), as the control arm does."
        if worse == "CONTINUE" else
        "a trade continues at its review unless both the agent and Jev say exit."))
    specs.append((
        "VETOES", "Are vetoes right?", "Vetoed picks' shadow net R vs passed picks'",
        [("passed", selection["passed"]), ("vetoed", selection["vetoed"])],
        [0, MINIMUMS["VETOES"]], selection_policy,
        next_version(selection_policy, "JEV_TOP_K_SELECTION_NEXT"),
        "a definite wrong answer lowers the pick's score like an uncertain one instead of "
        "vetoing it; every question, K and the ranking otherwise unchanged."))
    results = []
    for test_id, question, compares, groups, minimums, current, version, change in specs:
        effect, interval, evidence, outcome, short = verdict(
            groups, minimums, seed=seed_for(week_end, test_id))
        samples = {name: len(values) for name, values in groups}
        entry = {
            "test_id": test_id, "question": question, "compares": compares,
            "minimum": {name: minimum for (name, _), minimum in zip(groups, minimums,
                                                                    strict=True)},
            "samples": samples, "means": {name: _q(mean(values)) for name, values in groups},
            "effect": _q(effect), "interval_90": interval, "evidence": evidence,
            "verdict": outcome, "shortfall": short, "current_version": current, "proposal": None,
        }
        if test_id == "DAY_REVIEW_DECISIONS":
            entry["by_decision"] = {"CONTINUE": {"count": len(continues),
                                                 "mean": _q(mean(continues))},
                                    "EXIT": {"count": len(exits), "mean": _q(mean(exits))}}
        if outcome == "PROPOSE":
            entry["proposal"] = {"version": version, "text": proposal_text(
                test_id, version, current, week_end, effect, interval, samples, change)}
        results.append(entry)
    return results


# --- The record ---------------------------------------------------------------------------------


def week_of(day):
    """``(monday, sunday)`` of the Monday-to-Sunday week containing ``day``."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def last_completed_week_end(now):
    """The Sunday of the latest week that has fully ended by ``now`` (New York)."""
    today = _aware(now).astimezone(NY).date()
    return today - timedelta(days=today.weekday() + 1)


def review_key(week_end):
    return f"weekly-review:{week_end.isoformat()}"


def compute_review(repository, week_end, *, now):
    """The ``WEEKLY_REVIEW_V1`` body of the week ending on Sunday ``week_end`` (not recorded)."""
    if week_end.weekday() != 6:
        raise ValueError("WEEK_END_MUST_BE_SUNDAY")
    _, until = day_bounds(week_end)
    now = _aware(now)
    if now < until:
        raise ValueError("WEEK_NOT_OVER")
    monday, _ = week_of(week_end)
    scorecard = recorded_scorecard(repository, week_end)
    week = None
    if scorecard is not None:
        overall = scorecard["body"]["windows"]["7d"]["overall"]
        week = {key: overall.get(key) for key in ("start", "end", "funnel", "results",
                                                  "selection", "arms", "costs", "causes")}
        maintenance = overall.get("maintenance") or {}
        week["maintenance"] = {k: v for k, v in maintenance.items() if k != "items"}
    return json_safe({
        "review_version": REVIEW_VERSION, "week_start": monday.isoformat(),
        "week_end": week_end.isoformat(), "timezone": TIMEZONE, "evidence_until": until,
        "computed_at": now,
        "method": {"resamples": RESAMPLES, "interval": "90%", "percentile_ranks":
                   [LOWER_RANK, UPPER_RANK], "generator": "SPLITMIX64",
                   "seed": f"SHA256('{REVIEW_VERSION}|<week_end>|<test_id>')[:8]",
                   "value_precision": "1E-8", "minimums": MINIMUMS,
                   "propose_when": "INTERVAL_UPPER_BELOW_ZERO"},
        "tests": run_tests(repository, week_end.isoformat(), until=until),
        "week": week, "limitations": list(LIMITATIONS),
    })


def recorded_review(repository, week_end=None):
    with repository.connect() as conn:
        if week_end is None:
            return conn.execute(
                """SELECT event_seq, body FROM lab.managed_events WHERE kind=%s
                AND setup_id IS NULL ORDER BY body->>'week_end' DESC, event_seq DESC LIMIT 1""",
                (REVIEW_EVENT,),
            ).fetchone()
        return conn.execute(
            "SELECT event_seq, body FROM lab.managed_events WHERE idempotency_key=%s",
            (review_key(week_end),),
        ).fetchone()


def record_review(store, week_end, *, now):
    existing = recorded_review(store.repo, week_end)
    if existing is not None:
        return "ALREADY_RECORDED", existing["event_seq"]
    body = compute_review(store.repo, week_end, now=now)
    with store.transaction() as conn:
        found = conn.execute("SELECT event_seq FROM lab.managed_events WHERE idempotency_key=%s",
                             (review_key(week_end),)).fetchone()
        if found is not None:
            return "ALREADY_RECORDED", found["event_seq"]
        row = store.event(conn, REVIEW_EVENT, body, key=review_key(week_end))
    return "RECORDED", row["event_seq"]


__all__ = [
    "MINIMUMS", "REVIEW_EVENT", "REVIEW_VERSION", "SplitMix64", "bootstrap", "compute_review",
    "last_completed_week_end", "next_version", "record_review", "recorded_review", "seed_for",
    "verdict", "week_of",
]
