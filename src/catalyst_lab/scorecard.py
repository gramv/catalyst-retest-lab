"""``DAILY_SCORECARD_V1``: one immutable record of a New York day, its last 7 and its last 30
days (package learning-app, 2026-09-28; plan ``docs/LEARNING-LOOP-PLAN.md`` section 4).

**A measurement definition, never a trading rule. Paper trading only.** Read-only apart from one
``DAILY_SCORECARD`` event per day (``lab.managed_events``, no setup, key
``daily-scorecard:<day>``). Nothing here places an order, changes a rule or reaches Jev.

Windows: ``1d``, ``7d`` and ``30d`` New York days ending with the scored day. A pick belongs to a
window by its report's ``run_slot`` (the research run that sent it), a trade by its
``closed_at``, a Jev call by its receipt's ``started_at``. Every rate and mean has its count
beside it, and a rate or mean with no input is null, never zero. Operator ``ENGINEERING_TEST``
setups are counted apart (``engineering_excluded``) and enter no figure; a trade whose fee
evidence carries a ``LAB_FIXTURE`` source never counts as verified R.

Each window has ``overall`` and ``agents`` (``{agent_id: LINES}``; unattributed legacy records
under ``LEGACY_UNATTRIBUTED``). ``LINES`` (the research context's lessons show an agent its
own): ``funnel`` (report-V3 picks sent to closed, with a reason for every drop: intake codes,
veto reasons, not-ranked codes, skip reasons, system-check decline codes, expiry, setup
invalidation and risk reasons), ``results`` (closed trades, wins and losses by gross P&L, R after
verified fees = the official R), ``selection`` (Jev's selected, passed, vetoed and not-ranked
picks on their ``PICK_SHADOW_OUTCOME_V1`` shadow net R), research craft (``fill_rate_by_distance``,
``results_by_timeframe``, ``results_by_rule``, ``results_by_kind``, ``results_by_sector``,
``excerpt_drop_rate``, ``stale_news_vetoes``, ``dossier_size_rejections``) and ``causes`` (the
cause of each notable trade's latest post-mortem). ``overall`` adds ``maintenance`` (every
recorded replay of the window's trades by decision type, actual official R minus the
counterfactual's net R), ``arms`` (Jev-managed against the control arm), ``costs`` (the Jev
meter's estimate and verified trading fees) and, in the ``1d`` window, the per-trade list.

Package learning-measure (2026-10-02; owner: "yes, pull those two fixes forward"), additive:
net R is reported only over fee-verified trades, always beside the count of closed trades
(``results``: ``r_net_count`` of ``trades_closed``, ``fees_verified_share``, the reasons of the
unverified ones, and the gross R of every closed trade for comparison); each per-trade line has
its ``r_net`` (null until its fees are verified), ``gross_r``, ``fees_status``, its
``MARKET_REGIME_V1`` tags and its recorded versions; ``overall`` adds ``dimensions`` (results
by regime and by recorded version, ``result_dimensions``) and, in the ``1d`` window,
``late_fee_settlements``: trades of the 7 days before whose own day's scorecard was recorded
before their fees posted (Alpaca posts crypto fees in a daily batch after this job runs) and
whose net R is known now.

Package jev-b3 (2026-10-03), additive: the ``1d`` ``overall`` adds ``jev``
(``JEV_CALIBRATION_V1``, ``jev_calibration.day_counts``): the day's Jev probabilities by source
(top-K pick reviews, answered maintenance reviews), how many have their outcome recorded and
how many are pending (most of a day's are, since an outcome needs 24 hours), and the records to
date.

Package strategy-c1 (2026-10-03, STRATEGY_REGISTRY_V1), additive: ``overall`` adds
``strategies`` (``strategy_shadow.strategy_section``): the window's closed paper trades by their
stamped strategy (``live_paper``) and the mechanical strategies' simulated outcomes by signal time
(``shadow``), each labelled, with every strategy's stage on the promotion ladder; and
``dimensions`` adds ``by_strategy``.
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab.agent_identity import agent_fields
from catalyst_lab.execution_quality import trade_execution
from catalyst_lab.jev_budget import CONFIG_EVENT, KINDS, SpendConfig, kind_of
from catalyst_lab.jev_calibration import day_counts
from catalyst_lab.learning_intake import POST_MORTEM_EVENT, day_bounds
from catalyst_lab.learning_replays import (
    DAY_REPLAY_EVENT,
    DAY_REPLAY_METHOD,
    DAY_REPLAY_WINDOW_METHOD,
    counterfactual_r,
    decision_type,
    recorded_replays,
    scale_of,
)
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market_reality import sector_map, sector_of
from catalyst_lab.market_regime import REGIME_VERSION, recorded_regimes, recorded_trade_regimes
from catalyst_lab.pick_outcomes import SHADOW_EVENT_KIND, cycle_picks, rank_bucket
from catalyst_lab.repository import json_safe
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from catalyst_lab.result_dimensions import (
    CELL_MINIMUM,
    a_versions,
    dimensions,
    recorded_versions,
    regime_line,
    unverified_reason,
)
from catalyst_lab.stats_exclusion import excluded_setups_of
from catalyst_lab.strategies import DEFAULT_STRATEGY_ID, strategy_id_of
from catalyst_lab.strategy_shadow import strategy_section
from catalyst_lab.unchanged_plan import UNCHANGED_PLAN_METHOD, UNCHANGED_PLAN_WINDOW_METHOD

D = Decimal
SCORECARD_VERSION = "DAILY_SCORECARD_V1"
SCORECARD_EVENT = "DAILY_SCORECARD"
TIMEZONE = "America/New_York"
WINDOWS = (("1d", 1), ("7d", 7), ("30d", 30))
LEGACY_KEY = "LEGACY_UNATTRIBUTED"
# --- The version's definitions (DAILY_SCORECARD_V1) --------------------------------------------
NOTABLE_WIN_R = D("1.5")  # A win above 1.5R is notable (plan 5b).
DISTANCE_EDGES = ((D(1), "0-1%"), (D(2), "1-2%"), (D(3), "2-3%"))
DISTANCE_ORDER = ("0-1%", "1-2%", "2-3%", "3%+", "UNKNOWN")
TIMEFRAMES = {3600: "1h", 7200: "2h", 14400: "4h", 21600: "6h", 86400: "1d"}
TIMEFRAME_ORDER = ("1h", "2h", "4h", "6h", "1d", "OTHER", "NONE")
RULE_ORDER = ("A", "B", "UNDECLARED")
KIND_ORDER = ("CHART", "NEWS", "BOTH")
_RULE = re.compile(r"(?<![A-Za-z0-9])rule-([AB])(?![A-Za-z0-9])")
STALE_NEWS_VETO = "NEWS_STALE_YES"
SOURCE_REJECTION = "INVALID_SOURCE_EVIDENCE"
DOSSIER_REJECTION = "DOSSIER_OVER_BUDGET"
EXIT_KINDS = {
    "BROKER_EXIT": "STOP", "STOP_LIMIT_NOT_FILLED": "STOP", "STOP_CROSSED_DURING_REPLACE": "STOP",
    # CRYPTO_STOP_EXECUTION_V1 (package exec-d): the app's own exit of a reference-confirmed stop.
    "STOP_EMULATED_EXIT": "STOP",
    "TARGET_EXIT": "TARGET", "EARLY_EXIT_AGREED": "EARLY_EXIT",
    "DAY_REVIEW_EXIT": "TWENTY_FOUR_HOUR_EXIT", "DAY_REVIEW_DEADLINE_EXIT": "TWENTY_FOUR_HOUR_EXIT",
    "HOLD_24H_EXIT": "TWENTY_FOUR_HOUR_EXIT", "TIME_EXIT": "TWENTY_FOUR_HOUR_EXIT",
}
NOTABLE_EXITS = {"STOP": "STOP", "EARLY_EXIT": "EARLY_EXIT",
                 "TWENTY_FOUR_HOUR_EXIT": "TWENTY_FOUR_HOUR_EXIT"}
SELECTION_GROUPS = (("selected", ("TOP_K", "REPLACEMENT")), ("passed", ("NOT_SELECTED",)),
                    ("vetoed", ("VETOED",)), ("not_ranked", ("NOT_RANKED",)))
DECISION_TYPES = ("STOP_RAISE", "TARGET_RAISE", "STOP_AND_TARGET_RAISE", "EARLY_EXIT",
                  "DAY_REVIEW_CONTINUE", "DAY_REVIEW_EXIT")
TERMINAL_UNFILLED = {"EXPIRED_UNTRIGGERED", "INVALIDATED", "RISK_REJECTED", "REJECTED"}
LATE_SETTLEMENT_DAYS = 7  # Earlier scored days whose unverified trades are looked up again.
FOUR = D("0.0001")
LIMITATIONS = (
    "Shadow outcomes are 1-minute-bar approximations with an assumed taker fee; only picks "
    "whose window plus the 24-hour hold has passed have one, so recent days show pending picks.",
    "Replays compare a trade's actual official R (verified fees) with a counterfactual priced "
    "with the assumed taker fee; the fee bases differ.",
    "Jev costs are the spend meter's estimate from request bytes, not an invoice; Railway "
    "usage is not in the ledger.",
    "Excerpts an agent's own kit dropped before sending are not in the ledger; "
    "excerpt_drop_rate counts intake refusals for source evidence only.",
)


class ScorecardUnavailable(RuntimeError):
    """The day cannot be scored now (nothing stored); ``str()`` is a code."""


def _q(value, step=FOUR):
    if value is None:
        return None
    rounded = value.quantize(step, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def rate(numerator, denominator):
    return _q(D(numerator) / D(denominator)) if denominator else None


def mean(values):
    values = list(values)
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return _q(sum(values, D(0)) / len(values))


def _aware(value):
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


def _number(value):
    try:
        number = D(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


# --- Classification of one pick and one trade (pure) --------------------------------------------


def distance_bucket(agent_price, entry_trigger):
    price, entry = _number(agent_price), _number(entry_trigger)
    if price is None or entry is None or price <= 0:
        return "UNKNOWN"
    distance = abs(price - entry) / price * 100
    for edge, label in DISTANCE_EDGES:
        if distance < edge:
            return label
    return "3%+"


def timeframe_bucket(seconds):
    if seconds in (None, ""):
        return "NONE"
    try:
        return TIMEFRAMES.get(int(seconds), "OTHER")
    except (TypeError, ValueError):
        return "OTHER"


def rule_bucket(*texts):
    """The kit's level rule when exactly one of ``rule-A`` / ``rule-B`` is written."""
    found = {match.group(1) for text in texts if isinstance(text, str)
             for match in _RULE.finditer(text)}
    return found.pop() if len(found) == 1 else "UNDECLARED"


def exit_kind(reason):
    return EXIT_KINDS.get(reason, "OTHER")


def notable_reasons(kind, r_value):
    reasons = [NOTABLE_EXITS[kind]] if kind in NOTABLE_EXITS else []
    if r_value is not None and r_value > NOTABLE_WIN_R:
        reasons.append("WIN_OVER_1_5R")
    return reasons


# --- Ledger reads -------------------------------------------------------------------------------


@dataclass
class Pick:
    record: object  # pick_outcomes.PickRecord
    agent: str
    distance: str
    timeframe: str
    rule: str
    sector: str
    shadow: dict | None
    setup: dict | None
    traded_shadow: dict | None = None  # TRADED_LEVELS_V1: the plan's levels' shadow outcome.


@dataclass
class Cycle:
    cycle_id: str
    agent: str
    run_slot: datetime
    item_results: list
    picks: list


@dataclass
class Trade:
    setup_id: str
    symbol: str
    agent: str
    arm: str | None
    kind: str | None
    opened_at: datetime | None
    closed_at: datetime
    exit_reason: str | None
    measurement: dict
    fixture_tainted: bool
    versions: dict | None = None
    regime: dict | None = None
    strategy_id: str = DEFAULT_STRATEGY_ID
    execution: dict | None = None  # EXECUTION_QUALITY_V1 (package exec-d).
    r_basis: dict | None = None  # Official R's scale (TRADED_LEVELS_V1, package learning-loop2).

    @property
    def r_net(self):
        """R after verified fees (official R), never from fixture-sourced fee evidence."""
        value = self.measurement.get("official_r")
        return None if self.fixture_tainted or value is None else D(str(value))

    @property
    def gross_pnl(self):
        return _number(self.measurement.get("gross_realized_pnl"))

    @property
    def gross_r(self):
        """Gross P&L over the official R's denominator (planned filled risk), fees aside."""
        gross, risk = self.gross_pnl, _number(self.measurement.get("planned_filled_risk"))
        return gross / risk if gross is not None and risk else None

    @property
    def fees_status(self):
        return unverified_reason(self.measurement, self.fixture_tainted) or "VERIFIED"

    def item(self):
        """This trade as ``result_dimensions`` reads it."""
        return {"r_net": self.r_net, "win": None if self.gross_pnl is None
                else self.gross_pnl > 0, "regime": self.regime, "versions": self.versions or {},
                "strategy_id": self.strategy_id, "execution": self.execution}

    @property
    def notability_r(self):
        """Official R, else gross P&L over the same denominator (fees pending)."""
        return self.r_net if self.r_net is not None else self.gross_r

    @property
    def exit_kind(self):
        return exit_kind(self.exit_reason)

    @property
    def hold_hours(self):
        """First entry fill to the close, in hours (exact)."""
        if self.opened_at is None:
            return None
        return D(str((self.closed_at - _aware(self.opened_at)).total_seconds())) / 3600


def _agent_key(agent):
    agent_id = agent_fields(agent)["agent_id"]
    return agent_id or LEGACY_KEY


def load_cycles(repository, start, end, *, now, classified):
    """Report-V3 cycles whose ``run_slot`` falls in ``[start, end)``, each pick with its fate.

    A report is received at most the schedule's grace (and the report age) before the run slot
    it answers, so only starts recorded from two days before ``start`` can answer a slot in the
    window: older report bodies are never loaded."""
    with repository.connect() as conn:
        started = conn.execute(
            """SELECT body->>'cycle_id' AS cycle_id, body->'agent' AS agent,
            body->>'run_slot' AS run_slot, body->'item_results' AS item_results
            FROM lab.managed_events WHERE kind='RESEARCH_STARTED' AND recorded_at >= %s
            AND body->>'report_schema_version'=%s AND body ? 'run_slot'
            ORDER BY event_seq""",
            (start - timedelta(days=2), REPORT_SCHEMA_V3),
        ).fetchall()
    chosen = []
    for row in started:
        try:
            slot = _aware(row["run_slot"])
        except (TypeError, ValueError):
            continue
        if start <= slot < end:
            chosen.append((row, slot))
    cycles = []
    for row, slot in chosen:
        cycle_id = row["cycle_id"]
        records = cycle_picks(repository, cycle_id, now=now)
        with repository.connect() as conn:
            packets = {p["item_key"]: p for p in conn.execute(
                """SELECT body->>'item_key' AS item_key, (body->>'revision')::int AS revision,
                body->'state'->'technical_context'->'observed_facts'->'observations'
                  ->>'timeframe_seconds' AS timeframe_seconds,
                body->'selection_rationale'->>'why_over_peers' AS why_over_peers,
                body->'selection_rationale'->'agent_confidence'->>'basis' AS basis
                FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
                AND body->>'cycle_id'=%s ORDER BY event_seq""", (cycle_id,),
            ).fetchall()}
            shadow_rows = conn.execute(
                """SELECT body FROM lab.managed_events WHERE kind=%s
                AND body->'pick'->>'cycle_id'=%s ORDER BY event_seq""",
                (SHADOW_EVENT_KIND, cycle_id)).fetchall()
            shadows = {(r["body"]["pick"]["item_key"], r["body"]["pick"]["revision"]):
                       r["body"]["outcome"] for r in shadow_rows}
            traded = {(r["body"]["pick"]["item_key"], r["body"]["pick"]["revision"]):
                      (r["body"].get("traded_plan") or {}).get("outcome") for r in shadow_rows}
            setup_ids = [r.setup_id for r in records if r.setup_id]
            setups = {str(s["setup_id"]): s for s in conn.execute(
                """SELECT s.setup_id, s.record_json, t.body AS state,
                EXISTS(SELECT 1 FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
                       AND f.side='buy') AS filled,
                EXISTS(SELECT 1 FROM lab.managed_events e WHERE e.setup_id=s.setup_id
                       AND e.kind='TRIGGER_CONFIRMED') AS triggered
                FROM lab.managed_setups s LEFT JOIN lab.managed_states t USING(setup_id)
                WHERE s.setup_id::text = ANY(%s)""", (setup_ids,),
            ).fetchall()} if setup_ids else {}
        agent = _agent_key(row["agent"])
        picks = []
        for record in records:
            packet = packets.get(record.item_key) or {}
            setup = setups.get(record.setup_id) if record.setup_id else None
            if setup is not None and is_engineering(setup["record_json"]):
                setup = None
            picks.append(Pick(
                record=record, agent=agent,
                distance=distance_bucket(record.agent_current_price,
                                         (record.levels or {}).get("entry_trigger")),
                timeframe=timeframe_bucket(packet.get("timeframe_seconds")),
                rule=rule_bucket(packet.get("why_over_peers"), packet.get("basis")),
                sector=sector_of(record.symbol, classified),
                shadow=shadows.get((record.item_key, record.revision)),
                setup=setup,
                traded_shadow=traded.get((record.item_key, record.revision)),
            ))
        cycles.append(Cycle(cycle_id=cycle_id, agent=agent, run_slot=slot,
                            item_results=list(row["item_results"] or []), picks=picks))
    return cycles


def load_trades(repository, start, end):
    """Managed trades (entered) closed in ``[start, end)``, and the close times of operator
    ``ENGINEERING_TEST`` trades there (counted apart, never measured into a figure)."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT s.setup_id, s.symbol, s.record_json, t.body AS state,
            (SELECT min(f.filled_at) FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
             AND f.side='buy') AS opened_at
            FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
            WHERE s.cohort=%s AND t.body->>'state'='CLOSED'
            AND EXISTS(SELECT 1 FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
                       AND f.side='buy')
            ORDER BY s.event_seq""",
            (COHORT,),
        ).fetchall()
    trades, engineering = [], []
    for row in rows:
        state = row["state"] or {}
        try:
            closed_at = _aware(state.get("closed_at"))
        except (TypeError, ValueError):
            continue
        if closed_at is None or not start <= closed_at < end:
            continue
        if is_engineering(row["record_json"]):
            engineering.append(closed_at)
            continue
        measurement = managed_measurement(repository, row["setup_id"])
        with repository.connect() as conn:
            execution = trade_execution(
                conn, row["setup_id"], state,
                planned_filled_risk=measurement.get("planned_filled_risk"))
        tainted = any(item.get("source") == "LAB_FIXTURE"
                      for item in measurement.get("cost_evidence") or [])
        record = row["record_json"] or {}
        trades.append(Trade(
            setup_id=str(row["setup_id"]), symbol=row["symbol"],
            agent=_agent_key(record.get("agent")), arm=state.get("arm"),
            kind=(record.get("state") or {}).get("kind"),
            opened_at=row["opened_at"], closed_at=closed_at,
            exit_reason=state.get("reason") or state.get("exit_requested"),
            measurement=measurement, fixture_tainted=tainted,
            versions=recorded_versions(record, state),
            strategy_id=strategy_id_of(state, record),
            execution=json_safe(execution),
            r_basis=scale_of(record, state),
        ))
    regimes = recorded_trade_regimes(repository, [t.setup_id for t in trades])
    for trade in trades:
        trade.regime = regimes.get(trade.setup_id)
    return trades, engineering


def latest_post_mortems(repository):
    """``{subject_key: item}``: the latest accepted post-mortem item of every subject."""
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT body->'items' AS items, body->>'note_id' AS note_id,
            body->'agent'->>'agent_id' AS agent_id FROM lab.managed_events
            WHERE kind=%s AND setup_id IS NULL ORDER BY event_seq""",
            (POST_MORTEM_EVENT,),
        ).fetchall()
    latest = {}
    for row in rows:
        for item in row["items"] or []:
            latest[item["subject_key"]] = {**item, "note_id": row["note_id"],
                                           "agent_id": row["agent_id"]}
    return latest


def spend_estimate(repository):
    """The trader's latest recorded Jev price estimate (``JEV_SPEND_GUARD_CONFIGURED``), else
    the meter's defaults; ``(SpendConfig, source)``."""
    with repository.connect() as conn:
        row = conn.execute(
            """SELECT body->'configuration'->'estimate' AS estimate FROM lab.managed_events
            WHERE kind=%s AND setup_id IS NULL ORDER BY event_seq DESC LIMIT 1""",
            (CONFIG_EVENT,),
        ).fetchone()
    try:
        estimate = row["estimate"]
        return SpendConfig(D(estimate["monthly_budget_usd"]),
                           D(estimate["price_per_million_input_tokens_usd"]),
                           D(estimate["bytes_per_token"])), "JEV_SPEND_GUARD_CONFIGURED"
    except (TypeError, KeyError, ValueError, InvalidOperation):
        return SpendConfig(D("1")), "JEV_SPEND_METER_V1_DEFAULTS"


def jev_calls(repository, start, end):
    """Provider calls (``JEV_SPEND_METER_V1``'s definition) started in ``[start, end)``: calls
    and request bytes by question-set version."""
    with repository.connect() as conn:
        return conn.execute(
            """SELECT q.question_set_version, count(*) AS calls,
            coalesce(sum(octet_length(q.request_json)), 0) AS request_bytes
            FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
            WHERE r.started_at >= %s AND r.started_at < %s
            AND (r.http_status IS NOT NULL OR r.outcome='TRANSPORT_FAILURE')
            GROUP BY q.question_set_version""",
            (start, end),
        ).fetchall()


# --- The sections (pure over loaded rows) --------------------------------------------------------


def funnel(cycles):
    counts = Counter()
    reasons = {key: Counter() for key in ("rejection_codes", "veto_reasons", "not_ranked_reasons",
                                           "skip_reasons", "decline_codes",
                                           "invalidation_reasons")}
    counts["cycles"] = len(cycles)
    for cycle in cycles:
        counts["picks_sent"] += len(cycle.item_results)
        for result in cycle.item_results:
            if result.get("status") != "ACCEPTED":
                counts["rejected_at_intake"] += 1
                reasons["rejection_codes"][result.get("code") or "UNKNOWN"] += 1
        for pick in cycle.picks:
            record = pick.record
            counts["accepted"] += 1
            status = record.ranking_status
            if status == "RANKED":
                counts["ranked"] += 1
                if not record.selected:
                    counts["not_selected"] += 1
            elif status == "VETOED":
                counts["vetoed"] += 1
                reasons["veto_reasons"].update(record.ranking_reasons or ["UNKNOWN"])
            elif status == "NOT_RANKED":
                counts["not_ranked"] += 1
                reasons["not_ranked_reasons"].update(
                    [r for r in record.ranking_reasons if r] or ["UNKNOWN"])
            if record.selected:
                counts["selected"] += 1
            if record.skip_reason:
                counts["skipped"] += 1
                reasons["skip_reasons"][record.skip_reason] += 1
            selection = record.selection_status
            if selection in {"DECLINED", "REPLACED_BY", "SUPERSEDED"}:
                counts["declined"] += 1
                reasons["decline_codes"][record.decline_code or "UNKNOWN"] += 1
            elif selection == "EXPIRED":
                counts["expired_before_admission"] += 1
            elif selection == "SELECTED":
                counts["awaiting_admission"] += 1
            setup = pick.setup
            if setup is None:
                continue
            counts["admitted"] += 1
            state = setup["state"] or {}
            current = state.get("state")
            counts["triggered"] += bool(setup["triggered"])
            counts["filled"] += bool(setup["filled"])
            if current == "CLOSED":
                counts["closed"] += 1
            elif setup["filled"]:
                counts["open"] += 1
            elif current == "EXPIRED_UNTRIGGERED":
                counts["expired_untriggered"] += 1
            elif current == "INVALIDATED":
                counts["invalidated"] += 1
                reasons["invalidation_reasons"][state.get("reason")
                                                or state.get("revocation_reason")
                                                or "UNKNOWN"] += 1
            elif current in {"RISK_REJECTED", "REJECTED"}:
                counts["risk_rejected"] += 1
            else:
                counts["watching"] += 1
    fields = ("cycles", "picks_sent", "accepted", "rejected_at_intake", "ranked", "vetoed",
              "not_ranked", "selected", "not_selected", "skipped", "declined",
              "expired_before_admission", "awaiting_admission", "admitted",
              "expired_untriggered", "invalidated", "risk_rejected", "watching", "triggered",
              "filled", "closed", "open")
    result = {name: counts[name] for name in fields}
    result.update({name: dict(sorted(counter.items())) for name, counter in reasons.items()})
    return result


def results(trades):
    wins = sum(1 for t in trades if t.gross_pnl is not None and t.gross_pnl > 0)
    losses = sum(1 for t in trades if t.gross_pnl is not None and t.gross_pnl <= 0)
    r_values = [t.r_net for t in trades if t.r_net is not None]
    gross = [t.gross_r for t in trades if t.gross_r is not None]
    holds = [t.hold_hours for t in trades if t.hold_hours is not None]
    return {
        "trades_closed": len(trades), "wins": wins, "losses": losses,
        "win_rate": rate(wins, wins + losses), "r_net_count": len(r_values),
        "mean_r_net": mean(r_values), "sum_r_net": _q(sum(r_values, D(0))) if r_values else None,
        # Net R covers the fee-verified trades only; these say how many of the closed that is.
        "r_net_scope": "FEE_VERIFIED_TRADES_ONLY",
        "fees_verified_share": rate(len(r_values), len(trades)),
        "gross_r_count": len(gross), "mean_gross_r": mean(gross),
        "fees_unverified": sum(1 for t in trades if t.r_net is None),
        "fees_unverified_by_reason": dict(sorted(Counter(
            t.fees_status for t in trades if t.r_net is None).items())),
        "exit_reasons": dict(sorted(Counter(t.exit_reason or "UNKNOWN" for t in trades).items())),
        "mean_hold_hours": mean(holds),
    }


def shadow_net_r(pick, outcome=None):
    outcome = pick.shadow if outcome is None else outcome
    if not outcome or not outcome.get("data_complete") or outcome.get("net_r") is None:
        return None
    return D(str(outcome["net_r"]))


def traded_net_r(pick):
    """The shadow net R on the admitted setup's traded levels (TRADED_LEVELS_V1), or None."""
    return shadow_net_r(pick, pick.traded_shadow) if pick.traded_shadow else None


def selection(picks):
    groups, means = {}, {}
    for name, buckets in SELECTION_GROUPS:
        members = [p for p in picks if rank_bucket(p.record) in buckets]
        recorded = [p for p in members if p.shadow is not None]
        values = [v for v in (shadow_net_r(p) for p in members) if v is not None]
        traded = [v for v in (traded_net_r(p) for p in members) if v is not None]
        groups[name] = {
            "picks": len(members), "shadow_recorded": len(recorded),
            "pending": len(members) - len(recorded),
            "triggered": sum(1 for p in recorded if p.shadow.get("triggered")),
            "shadow_r_count": len(values), "mean_shadow_r_net": mean(values),
            "shadow_hit_rate": rate(sum(1 for v in values if v > 0), len(values)),
            # TRADED_LEVELS_V1 (package learning-loop2): admitted picks on the plan's levels,
            # beside the research levels above (which every pick, selected or not, shares).
            "traded_plan_r_count": len(traded), "mean_traded_plan_shadow_r_net": mean(traded),
        }
        means[name] = mean(values)
    groups["selected_minus_passed_mean_shadow_r_net"] = (
        _q(means["selected"] - means["passed"])
        if means["selected"] is not None and means["passed"] is not None else None)
    return groups


def bucket_line(picks):
    admitted = [p for p in picks if p.setup is not None]
    filled = [p for p in admitted if p.setup["filled"]]
    recorded = [p for p in picks if p.shadow is not None]
    triggered = [p for p in recorded if p.shadow.get("triggered")]
    values = [v for v in (shadow_net_r(p) for p in picks) if v is not None]
    total = sum(values, D(0))
    # Package learning-loop2 (L2): net R per resolved pick, a pick the shadow never filled
    # counting 0 R -- what sending a pick of this bucket earned -- for the lessons' ranking.
    resolved = [p for p in recorded if p.shadow.get("data_complete")]
    return {
        "picks": len(picks), "admitted": len(admitted), "filled": len(filled),
        "fill_rate": rate(len(filled), len(admitted)), "shadow_recorded": len(recorded),
        "shadow_triggered": len(triggered),
        "shadow_trigger_rate": rate(len(triggered), len(recorded)),
        "shadow_r_count": len(values), "mean_shadow_r_net": mean(values),
        "shadow_hit_rate": rate(sum(1 for v in values if v > 0), len(values)),
        "sum_shadow_r_net": _q(total) if values else None,
        "shadow_resolved": len(resolved),
        "shadow_r_net_per_resolved_pick": _q(total / len(resolved)) if resolved else None,
    }


def buckets(picks, key, order=None):
    grouped = {}
    for pick in picks:
        grouped.setdefault(key(pick) or "UNKNOWN", []).append(pick)
    names = [n for n in order if n in grouped] if order else sorted(grouped)
    names += sorted(n for n in grouped if order and n not in order)
    return {name: bucket_line(grouped[name]) for name in names}


def craft(cycles):
    picks = [p for c in cycles for p in c.picks]
    sent = sum(len(c.item_results) for c in cycles)
    rejected = Counter(r.get("code") for c in cycles for r in c.item_results
                       if r.get("status") != "ACCEPTED")
    news = [p for p in picks if p.record.kind in {"NEWS", "BOTH"}
            and p.record.ranking_status is not None]
    stale = [p for p in news if STALE_NEWS_VETO in (p.record.ranking_reasons or [])]
    return {
        "fill_rate_by_distance": buckets(picks, lambda p: p.distance, DISTANCE_ORDER),
        "results_by_timeframe": buckets(picks, lambda p: p.timeframe, TIMEFRAME_ORDER),
        "results_by_rule": buckets(picks, lambda p: p.rule, RULE_ORDER),
        "results_by_kind": buckets(picks, lambda p: p.record.kind, KIND_ORDER),
        "results_by_sector": buckets(picks, lambda p: p.sector),
        "excerpt_drop_rate": {"picks_sent": sent, "dropped": rejected[SOURCE_REJECTION],
                              "rate": rate(rejected[SOURCE_REJECTION], sent)},
        "stale_news_vetoes": {"news_picks_ranked": len(news), "vetoed": len(stale),
                              "rate": rate(len(stale), len(news))},
        "dossier_size_rejections": {"picks_sent": sent, "rejected": rejected[DOSSIER_REJECTION],
                                    "rate": rate(rejected[DOSSIER_REJECTION], sent)},
    }


def trade_line(trade, post_mortems, *, with_arm):
    r_value = trade.notability_r
    note = post_mortems.get(f"TRADE:{trade.setup_id}")
    line = {
        "setup_id": trade.setup_id, "symbol": trade.symbol, "agent_id": trade.agent,
        "pick_kind": trade.kind, "opened_at": trade.opened_at, "closed_at": trade.closed_at,
        "hold_hours": _q(trade.hold_hours),
        "exit_reason": trade.exit_reason, "exit_kind": trade.exit_kind,
        "gross_pnl_usd": trade.measurement.get("gross_realized_pnl"),
        "net_pnl_usd": trade.measurement.get("net_pnl"), "r_net": _q(trade.r_net),
        "gross_r": _q(trade.gross_r), "fees_status": trade.fees_status,
        "fees_verified": bool(trade.measurement.get("fees_verified"))
        and not trade.fixture_tainted,
        "regime": regime_line(trade.regime), "a_versions": a_versions(trade.versions),
        # EXECUTION_QUALITY_V1 (package exec-d): fee rate, maker/taker, exit path, stop slippage.
        "execution": trade.execution,
        "win": trade.gross_pnl > 0 if trade.gross_pnl is not None else None,
        "best_r": trade.measurement.get("observed_mfe_r"),
        "worst_r": trade.measurement.get("observed_mae_r"),
        "notable_reasons": notable_reasons(trade.exit_kind, r_value),
        "post_mortem": None if note is None else {
            "cause": note.get("cause"), "knowable_before_move": note.get("knowable_before_move"),
            "note_id": note.get("note_id")},
    }
    if with_arm:
        line["arm"] = trade.arm
    return line


def causes(trades, post_mortems, *, items):
    notable = [trade_line(t, post_mortems, with_arm=False) for t in trades]
    notable = [line for line in notable if line["notable_reasons"]]
    noted = [line for line in notable if line["post_mortem"]]
    result = {
        "notable_trades": len(notable), "with_post_mortem": len(noted),
        "by_cause": dict(sorted(Counter(line["post_mortem"]["cause"] for line in noted).items())),
        "knowable_before_move": {
            label: sum(1 for line in noted
                       if line["post_mortem"]["knowable_before_move"] is value)
            for label, value in (("true", True), ("false", False), ("null", None))},
    }
    if items:
        result["items"] = [{k: line[k] for k in ("setup_id", "symbol", "notable_reasons",
                                                  "post_mortem")} for line in notable]
    return result


def maintenance(trades, replays, *, items):
    by_trade = {t.setup_id: t for t in trades}
    groups = {name: [] for name in DECISION_TYPES}
    methods = {name: set() for name in DECISION_TYPES}
    rows = []
    for replay in replays:
        body = replay["body"]
        trade = by_trade.get(body.get("setup_id"))
        if trade is None:
            continue
        kind = decision_type(replay["kind"], body)
        actual = trade.r_net
        counterfactual = counterfactual_r(replay["kind"], body, getattr(trade, "r_basis", None))
        difference = actual - counterfactual if actual is not None and counterfactual is not None \
            else None
        groups.setdefault(kind, []).append(difference)
        methods.setdefault(kind, set()).add(body.get("method"))
        rows.append({"setup_id": trade.setup_id, "symbol": trade.symbol, "decision_type": kind,
                     "at": body.get("change_at") or body.get("at"), "actual_r_net": _q(actual),
                     "counterfactual_r_net": _q(counterfactual), "r_difference": _q(difference),
                     "method": body.get("method")})
    result = {}
    for name, differences in groups.items():
        known = [d for d in differences if d is not None]
        # The replay method(s) behind the group: V1's, as before, unless a window setup's
        # replays (package review-window: *_V2) are in it too.
        default = DAY_REPLAY_METHOD if name.startswith("DAY_REVIEW_") else UNCHANGED_PLAN_METHOD
        recorded = sorted(m for m in methods.get(name, ()) if isinstance(m, str))
        result[name] = {
            "method": "+".join(recorded) if recorded else default,
            "changes": len(differences), "r_difference_count": len(known),
            "mean_r_difference": mean(known),
            "helped": sum(1 for d in known if d > 0), "hurt": sum(1 for d in known if d < 0),
        }
    if items:
        result["items"] = rows
    return result


def arms(trades):
    result = {}
    for name in sorted({t.arm or "UNASSIGNED" for t in trades}):
        members = [t for t in trades if (t.arm or "UNASSIGNED") == name]
        values = [t.r_net for t in members if t.r_net is not None]
        wins = sum(1 for t in members if t.gross_pnl is not None and t.gross_pnl > 0)
        known = sum(1 for t in members if t.gross_pnl is not None)
        result[name] = {"closed": len(members), "wins": wins, "win_rate": rate(wins, known),
                        "r_net_count": len(values), "mean_r_net": mean(values),
                        "sum_r_net": _q(sum(values, D(0))) if values else None}
    managed = result.get("JEV_MANAGED", {}).get("mean_r_net")
    control = result.get("FIXED_EXIT", {}).get("mean_r_net")
    result["managed_minus_control_mean_r_net"] = (
        _q(managed - control) if managed is not None and control is not None else None)
    return result


def costs(trades, calls, config, source):
    by_kind, total_calls, total_bytes = {}, 0, 0
    for row in calls:
        kind = kind_of(row["question_set_version"])
        calls_, bytes_ = by_kind.get(kind, (0, 0))
        by_kind[kind] = (calls_ + row["calls"], bytes_ + int(row["request_bytes"]))
        total_calls += row["calls"]
        total_bytes += int(row["request_bytes"])
    verified = [t for t in trades if t.r_net is not None
                and t.measurement.get("verified_cash_fee_usd") is not None]
    return {
        "jev": {
            "calls": total_calls, "request_bytes": total_bytes,
            "estimated_usd": _q(config.cost(total_bytes)),
            "by_kind": {kind: {"calls": by_kind[kind][0],
                               "estimated_usd": _q(config.cost(by_kind[kind][1]))}
                        for kind in KINDS if kind in by_kind},
            "estimate": config.record(), "estimate_source": source,
            "meter": "JEV_SPEND_METER_V1",
        },
        "fees": {"closed_trades": len(trades), "verified_trades": len(verified),
                 "verified_cash_fee_usd": _q(sum((D(str(t.measurement["verified_cash_fee_usd"]))
                                                  for t in verified), D(0)))
                 if verified else None,
                 "unverified_trades": len(trades) - len(verified)},
        "railway": None,
    }


def late_fee_settlements(repository, day, trades):
    """Trades of the ``LATE_SETTLEMENT_DAYS`` days before ``day`` that their own day's recorded
    scorecard listed without verified fees and whose net R is known now."""
    pending = {}
    for back in range(1, LATE_SETTLEMENT_DAYS + 1):
        earlier = day - timedelta(days=back)
        row = recorded_scorecard(repository, earlier)
        lines_ = (((row or {}).get("body") or {}).get("windows", {}).get("1d", {})
                  .get("overall", {}).get("trades") or [])
        for line in lines_:
            if not line.get("fees_verified"):
                pending[line["setup_id"]] = earlier.isoformat()
    settled = [t for t in trades if t.setup_id in pending and t.r_net is not None]
    return {
        "count": len(settled), "mean_r_net": mean([t.r_net for t in settled]),
        "items": [{"setup_id": t.setup_id, "symbol": t.symbol, "closed_at": t.closed_at,
                   "scored_day": pending[t.setup_id], "r_net": _q(t.r_net),
                   "gross_r": _q(t.gross_r)} for t in settled],
        "still_unverified": sorted(sid for sid in pending
                                   if sid not in {t.setup_id for t in settled}),
    }


def exclusion_section(trades, excluded):
    """The window's trades left out by ``STATS_EXCLUSION_V1`` records, with their days."""
    hit = [excluded[t.setup_id] for t in trades if t.setup_id in excluded]
    return {"version": "STATS_EXCLUSION_V1", "excluded_trades": len(hit),
            "included_trades": len(trades) - len(hit),
            "days": sorted({h["day"] for h in hit}),
            "reasons": sorted({h["reason"] for h in hit})}


def lines(cycles, trades, post_mortems, *, start, end, items):
    return {
        "start": start, "end": end, "funnel": funnel(cycles), "results": results(trades),
        "selection": selection([p for c in cycles for p in c.picks]), **craft(cycles),
        "causes": causes(trades, post_mortems, items=items),
    }


# --- The record ---------------------------------------------------------------------------------


def scorecard_key(day):
    return f"daily-scorecard:{day.isoformat()}"


def compute_scorecard(repository, day, *, now):
    """The ``DAILY_SCORECARD_V1`` body of New York ``day`` (not recorded; ``record_scorecard``)."""
    now = _aware(now)
    _, day_end = day_bounds(day)
    if now < day_end:
        raise ScorecardUnavailable("SCORECARD_DAY_NOT_OVER")
    earliest, _ = day_bounds(day - timedelta(days=WINDOWS[-1][1] - 1))
    with repository.connect() as conn:
        classified = sector_map(conn)
        regime = recorded_regimes(conn, [day]).get(day.isoformat())
    day_regime = None if regime is None else {
        k: regime.get(k) for k in ("regime_version", "tag", "computed_at")}
    all_cycles = load_cycles(repository, earliest, day_end, now=now, classified=classified)
    all_trades, engineering_closes = load_trades(repository, earliest, day_end)
    post_mortems = latest_post_mortems(repository)
    replays = recorded_replays(repository, [t.setup_id for t in all_trades])
    config, source = spend_estimate(repository)
    excluded = excluded_setups_of(repository)  # STATS_EXCLUSION_V1 (package public-page-v3).
    windows = {}
    for name, days in WINDOWS:
        start, _ = day_bounds(day - timedelta(days=days - 1))
        cycles = [c for c in all_cycles if start <= c.run_slot < day_end]
        trades = [t for t in all_trades if start <= t.closed_at < day_end]
        engineering = sum(1 for at in engineering_closes if start <= at < day_end)
        items = name == "1d"
        overall = lines(cycles, trades, post_mortems, start=start, end=day_end, items=items)
        overall.update(
            maintenance=maintenance(trades, replays, items=items), arms=arms(trades),
            costs=costs(trades, jev_calls(repository, start, day_end), config, source),
            engineering_excluded=engineering,
            dimensions=dimensions([t.item() for t in trades]),
            # STRATEGY_REGISTRY_V1: paper trades and shadow outcomes per strategy, apart.
            strategies=strategy_section(repository, [t.item() for t in trades], start=start,
                                        end=day_end),
            # STATS_EXCLUSION_V1: ``results`` keeps every trade; this view leaves out the
            # trades of owner-excluded days, with their count.
            stats_exclusions=exclusion_section(trades, excluded),
            results_excluding_exclusions=results(
                [t for t in trades if t.setup_id not in excluded]),
        )
        if items:
            overall["trades"] = [trade_line(t, post_mortems, with_arm=True) for t in trades]
            overall["late_fee_settlements"] = late_fee_settlements(repository, day, all_trades)
            overall["regime"] = day_regime
            overall["jev"] = day_counts(repository, start, day_end)
        agents = {}
        for agent in sorted({c.agent for c in all_cycles} | {t.agent for t in all_trades}):
            agents[agent] = lines([c for c in cycles if c.agent == agent],
                                  [t for t in trades if t.agent == agent], post_mortems,
                                  start=start, end=day_end, items=False)
        windows[name] = {"start": start, "end": day_end, "overall": overall, "agents": agents}
    return json_safe({
        "scorecard_version": SCORECARD_VERSION, "day": day.isoformat(), "timezone": TIMEZONE,
        "computed_at": now, "windows": windows,
        "method": {
            "window_membership": {"picks": "REPORT_RUN_SLOT", "trades": "CLOSED_AT",
                                  "jev_calls": "RECEIPT_STARTED_AT"},
            "r_net": "OFFICIAL_R_VERIFIED_FEES_NO_FIXTURE_SOURCE",
            "wins": "GROSS_PNL_ABOVE_ZERO", "notable_win_r": str(NOTABLE_WIN_R),
            "distance_buckets": list(DISTANCE_ORDER), "timeframes": list(TIMEFRAME_ORDER),
            "rules": list(RULE_ORDER), "shadow_method": "PICK_SHADOW_OUTCOME_V1",
            "replay_methods": [UNCHANGED_PLAN_METHOD, DAY_REPLAY_METHOD,
                               UNCHANGED_PLAN_WINDOW_METHOD, DAY_REPLAY_WINDOW_METHOD],
            "selection_groups": {name: list(b) for name, b in SELECTION_GROUPS},
            "r_net_scope": "FEE_VERIFIED_TRADES_ONLY", "regime_version": REGIME_VERSION,
            "dimension_cell_minimum": CELL_MINIMUM,
        },
        "limitations": list(LIMITATIONS),
    })


def recorded_scorecard(repository, day):
    with repository.connect() as conn:
        return conn.execute(
            "SELECT event_seq, body FROM lab.managed_events WHERE idempotency_key=%s",
            (scorecard_key(day),),
        ).fetchone()


def record_scorecard(store, day, *, now):
    """Compute and append the day's scorecard once; ``(status, event_seq)``."""
    existing = recorded_scorecard(store.repo, day)
    if existing is not None:
        return "ALREADY_RECORDED", existing["event_seq"]
    body = compute_scorecard(store.repo, day, now=now)
    with store.transaction() as conn:
        found = conn.execute("SELECT event_seq FROM lab.managed_events WHERE idempotency_key=%s",
                             (scorecard_key(day),)).fetchone()
        if found is not None:
            return "ALREADY_RECORDED", found["event_seq"]
        row = store.event(conn, SCORECARD_EVENT, body, key=scorecard_key(day))
    return "RECORDED", row["event_seq"]


__all__ = [
    "DAY_REPLAY_EVENT", "LEGACY_KEY", "SCORECARD_EVENT", "SCORECARD_VERSION",
    "ScorecardUnavailable", "compute_scorecard", "distance_bucket", "exit_kind",
    "notable_reasons", "record_scorecard", "recorded_scorecard", "rule_bucket",
    "timeframe_bucket",
]
