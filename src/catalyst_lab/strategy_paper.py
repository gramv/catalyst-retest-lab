"""``STRATEGY_PAPER_PATH_V1``: a promoted mechanical strategy plug-in on the paper account (package
plugin-c3, 2026-10-03; plan docs/TRADING-QUALITY-PLAN.md section 5 move 5 and section 12 item 3;
guide docs/STRATEGY-PLUGINS.md; package record docs/packages/plugin-c3.md).

**PAPER TRADING ONLY. Default off.** Nothing here can send an order: a strategy's signal becomes a
selected packet, and the packet then goes through the SAME path as a research pick -- admission
(``ManagedExecution.admit``: the ledger checks, the broker price grid, ``SYSTEM_CHECK_V1``,
``CRYPTO_TRADE_PLAN_V1``), the recorded trigger version, ``CRYPTO_ENTRY_PACING_V1``, the soft
and hard daily limits, the account-risk policy (``JEV_MANAGED_RISK_V5``'s per-strategy cap) and
the exact one-use five-second risk authorization of every broker change. No order code is new.

Two gates, both required, both the owner's:

1. **Promotion** (``promote``): a durable ``STRATEGY_PROMOTION`` event (``STRATEGY_PROMOTION_V1``)
   naming the strategy id (its version is in the id), the SHA-256 of the history-test report that
   justified it, the ladder check (``history_test.promotion_check``) on that report and the
   strategy's shadow scorecard cell read from the ledger, and the owner's ruling reference. The
   operator command refuses unless the check says the history and shadow rungs are met
   (``ELIGIBLE_FOR_OWNER_PAPER_REVIEW``), except with an explicit owner override reason, which is
   recorded in the event. ``demote`` appends a ``STRATEGY_DEMOTION``; from then on the admission SQL
   refuses the strategy's packets (``STRATEGY_DEMOTED``) and revokes its setups still waiting for
   an entry; open positions keep their protection and exits.
2. **Configuration** (``MANAGED_STRATEGIES_JSON``, a JSON list of strategy ids; absent or empty
   is the default): the runtime runs the signal source only for listed, promoted strategies, and
   admission refuses a strategy packet whose strategy is not listed (``STRATEGY_NOT_CONFIGURED``).

The signal source (``StrategySignalSource``), in the runtime, once per completed hour (90 seconds
after the hour, when the public bar has settled): for each configured, promoted, mechanical
strategy and each coin of the latest recorded research universe, it reads the coin's completed
1-hour bars from Alpaca's public endpoint (the strategy shadow's source, so shadow and paper see
the same bars), runs the strategy's own ``signals`` for the hour that just closed, and for each
fresh ``MARKETABLE_AT_SIGNAL`` proposal still inside its 15-minute entry window appends one
``STRATEGY_SIGNAL`` event and one ``RESEARCH_SELECTED`` packet (selection policy
``STRATEGY_SIGNAL_SELECTION_V1``, no Jev receipt). Its levels, on the coin's broker price grid:

* entry trigger = max entry = the signal close x (1 + 0.5%), rounded up: the marketable entry is
  a touch of a level just above the price (the existing trigger confirms at once on a fresh quote
  at or below it), and the order is a limit at that collar, never a market order;
* stop 2% below the max entry, rounded down (the system check's minimum; ``CRYPTO_TRADE_PLAN_V1``
  then widens it to two hourly ranges when wider) and a research target at the admission rule's
  reward/risk 2 at the max entry (the plan caps it at 1.5R from the entry trigger);
* the packet expires 15 minutes after the signal (the shadow's fill window), and the plan's
  24-hour window governs the trade.

Jev has no part in a mechanical strategy's selection; an open trade in the maintained arm keeps
``CRYPTO_MAINTENANCE``'s reviews exactly as a research trade does. A packet carries the report-V3
packet shape (``report_schema_version``, ``run_slot`` = the signal time, the signal close as the
"current price") so that every crypto version admitted for report-V3 picks applies identically;
the research run's supersession and duplicate rules never read a strategy selection.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from catalyst_lab import strategies
from catalyst_lab.account_risk import MECHANICAL_SOURCE
from catalyst_lab.repository import json_safe
from catalyst_lab.strategies import core

D = Decimal
PATH_VERSION = strategies.PAPER_PATH_VERSION
SELECTION_POLICY = strategies.SIGNAL_SELECTION_POLICY
SIGNAL_EVENT = "STRATEGY_SIGNAL"
PROMOTION_EVENT = "STRATEGY_PROMOTION"
DEMOTION_EVENT = "STRATEGY_DEMOTION"
PROMOTION_VERSION = "STRATEGY_PROMOTION_V1"
DEMOTION_VERSION = "STRATEGY_DEMOTION_V1"
STRATEGIES_ENV = "MANAGED_STRATEGIES_JSON"
RUNG_OWNER = "ELIGIBLE_FOR_OWNER_PAPER_REVIEW"
MARKETABLE_AT_SIGNAL = "MARKETABLE_AT_SIGNAL"
RESEARCH_ORIGIN = "STRATEGY_SIGNAL"
PRODUCT_VERSION = "CRYPTO_STRUCTURAL_RETEST_TEST_V1"  # The crypto setups' product version.
REPORT_SCHEMA_V3 = "AGENT_RESEARCH_REPORT_V3"
ENTRY_WINDOW = core.ENTRY_FILL_WITHIN  # 15 minutes: the shadow's fill window.
ENTRY_COLLAR = D("0.005")  # The marketable limit: 0.5% above the signal close.
STOP_FRACTION = core.MIN_STOP_FRACTION  # 2% below the max entry (SYSTEM_CHECK_V1's minimum).
REWARD_RISK_AT_MAX_ENTRY = D(2)  # Admission's rule; the plan caps the target at 1.5R.
SETTLE_SECONDS = 90  # After the hour, before its public 1-hour bar is read.
BAR_SOURCE = "ALPACA_PUBLIC_V1BETA3_CRYPTO_US_BARS"
OPERATORS = frozenset({"LOCAL_OWNER_CLI", "RAILWAY_OPS_SHELL"})
_ID = re.compile(r"^[A-Z][A-Z0-9_]*_V[1-9][0-9]*$")
_PRECISION = 80

# Admission refusals of a strategy packet that never clear (the runtime declines the selection).
STRATEGY_NOT_CONFIGURED = "STRATEGY_NOT_CONFIGURED"
PERMANENT_REFUSALS = frozenset({
    STRATEGY_NOT_CONFIGURED, strategies.STRATEGY_NOT_PAPER_ELIGIBLE,
    strategies.STRATEGY_NOT_REGISTERED, "STRATEGY_NOT_PROMOTED", "STRATEGY_DEMOTED",
    "STRATEGY_SIGNAL_BINDING_FAILURE", "SELECTION_INTEGRITY_FAILURE", "REVIEW_EXPIRED",
})


class PromotionRefused(ValueError):
    """A refused promotion or demotion: ``code`` and the evidence that decided it."""

    def __init__(self, code, evidence=None):
        super().__init__(code)
        self.code, self.evidence = code, dict(evidence or {})


# --- Configuration ------------------------------------------------------------------------------


def configured_strategies(environ):
    """``MANAGED_STRATEGIES_JSON``: a JSON list of distinct strategy ids (``NAME_V<n>``). Absent
    or blank is ``()`` (default off); anything else malformed raises
    ``MANAGED_STRATEGIES_JSON_INVALID``. Registration is checked where the ids are used."""
    raw = (environ.get(STRATEGIES_ENV) or "").strip()
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("MANAGED_STRATEGIES_JSON_INVALID") from None
    if (not isinstance(value, list) or len(value) > 20
            or any(not isinstance(v, str) or not _ID.fullmatch(v) for v in value)
            or len(set(value)) != len(value)):
        raise ValueError("MANAGED_STRATEGIES_JSON_INVALID")
    return tuple(value)


# --- Promotion and demotion (owner commands) ----------------------------------------------------


def promotion_rows(conn, strategy_id):
    return conn.execute(
        """SELECT event_seq, kind, body FROM lab.managed_events WHERE setup_id IS NULL
        AND kind IN (%s,%s) AND body->>'strategy_id'=%s ORDER BY event_seq""",
        (PROMOTION_EVENT, DEMOTION_EVENT, strategy_id),
    ).fetchall()


def active_promotion(conn, strategy_id):
    """The strategy's latest ``STRATEGY_PROMOTION`` row (``event_seq``, ``body``) with no
    ``STRATEGY_DEMOTION`` after it, else None."""
    current = None
    for row in promotion_rows(conn, strategy_id):
        current = row if row["kind"] == PROMOTION_EVENT else None
    return current


def _text(value, low, high, code):
    text = value.strip() if isinstance(value, str) else ""
    if not low <= len(text) <= high or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise PromotionRefused(code)
    from catalyst_lab.audit import credential_findings

    if credential_findings(text):  # The ledger is append-only: a secret could never leave it.
        raise PromotionRefused(code)
    return text


def history_entry(path, strategy_id):
    """``(sha256 of the report file, the report, the strategy's entry)`` from a history-test
    ``results.json`` (``HISTORY_TEST_V1``): a path, or its bytes."""
    import hashlib

    try:
        raw = path if isinstance(path, bytes) else Path(path).read_bytes()
        report = json.loads(raw)
    except (OSError, ValueError):
        raise PromotionRefused("PROMOTION_HISTORY_REPORT_UNREADABLE") from None
    entry = (report.get("strategies") or {}).get(strategy_id) if isinstance(report, dict) else None
    if not isinstance(entry, dict):
        raise PromotionRefused("PROMOTION_HISTORY_ENTRY_MISSING")
    # Package oss-packaging: a run on generated sample bars is never evidence, not even with
    # the owner's override.
    from catalyst_lab.history_bars import SOURCE_LABELS, SYNTHETIC

    if (report.get("inputs") or {}).get("source") == SOURCE_LABELS[SYNTHETIC]:
        raise PromotionRefused("PROMOTION_HISTORY_REPORT_SYNTHETIC")
    return hashlib.sha256(raw).hexdigest(), report, entry


def history_summary(report, entry):
    """The history report's figures the promotion records (what justified it)."""
    inputs = report.get("inputs") or {}
    wf = (entry.get("walk_forward") or {}).get("out_of_sample") or {}
    return {
        "run_id": report.get("run_id"), "generated_at": report.get("generated_at"),
        "start": inputs.get("start"), "end": inputs.get("end"), "source": inputs.get("source"),
        "fee_model": inputs.get("fee_model"), "universe_size": len(inputs.get("universe") or ()),
        "walk_forward_oos": {k: wf.get(k) for k in ("trades", "mean_net_r", "ci90_mean_net_r")},
        "deflated_sharpe": ((entry.get("deflated_sharpe") or {}).get("walk_forward_oos") or {}
                            ).get("dsr"),
        "pbo": (entry.get("pbo") or {}).get("pbo"),
    }


def promote(store, strategy_id, *, history_report, owner_ruling_ref, now,
            owner_override_reason=None, operator="LOCAL_OWNER_CLI", shadow_cell=None):
    """Append the strategy's ``STRATEGY_PROMOTION`` (``STRATEGY_PROMOTION_V1``) and return a
    summary. Refuses (``PromotionRefused``): an unregistered or non-mechanical strategy, an
    unreadable report or one without the strategy, a strategy already promoted, and -- unless
    ``owner_override_reason`` is given (recorded) -- a ladder check below
    ``ELIGIBLE_FOR_OWNER_PAPER_REVIEW``. ``shadow_cell`` defaults to the ledger's own shadow
    scorecard cell of the strategy (``strategy_shadow.shadow_cells``)."""
    from catalyst_lab.history_test import promotion_check
    from catalyst_lab.strategy_shadow import shadow_cells

    if operator not in OPERATORS:
        raise PromotionRefused("OPERATOR_ORIGIN_INVALID")
    strategies.ensure_plugins_loaded()
    try:
        strategy = strategies.get(strategy_id)
    except ValueError:
        raise PromotionRefused(strategies.STRATEGY_NOT_REGISTERED) from None
    if not strategies.paper_eligible(strategy):
        raise PromotionRefused(strategies.STRATEGY_NOT_PAPER_ELIGIBLE)
    ruling = _text(owner_ruling_ref, 3, 300, "OWNER_RULING_REF_REQUIRED")
    override = (None if owner_override_reason is None
                else _text(owner_override_reason, 10, 500, "OWNER_OVERRIDE_REASON_INVALID"))
    sha, report, entry = history_entry(history_report, strategy_id)
    now = now.astimezone(UTC)
    if shadow_cell is None:
        shadow_cell = shadow_cells(store.repo, end=now + timedelta(seconds=1)).get(strategy_id)
    shadow = json_safe(dict(shadow_cell or {"label": "SHADOW", "trades": 0, "signals": 0,
                                            "status": "NO_SHADOW_OUTCOMES"}))
    check = json_safe(promotion_check(entry, shadow_cell))
    if check["rung"] != RUNG_OWNER and override is None:
        raise PromotionRefused("PROMOTION_LADDER_NOT_MET", {"promotion_check": check})
    body = json_safe({
        "version": PROMOTION_VERSION, "path_version": PATH_VERSION,
        "strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
        "strategy_source": MECHANICAL_SOURCE, "strategy_record": strategy.record(),
        "history_report_sha256": sha, "history_report": history_summary(report, entry),
        "shadow_summary": shadow, "promotion_check": check, "rung": check["rung"],
        "owner_ruling_ref": ruling, "owner_override_reason": override,
        "operator": operator, "recorded_at": now,
        "note": "Paper only. Admission, the trade plan, pacing, the daily limits, the account "
                "risk policy and the one-use risk authorization of every order still apply.",
    })
    with store.transaction() as conn:
        if active_promotion(conn, strategy_id) is not None:
            raise PromotionRefused("STRATEGY_ALREADY_PROMOTED")
        prior = len(promotion_rows(conn, strategy_id))
        row = store.event(conn, PROMOTION_EVENT, body,
                          key=f"strategy-promotion:{strategy_id}:{prior}:{sha}")
    return {"action": "promote-strategy", "mode": "PAPER_ONLY", "event_seq": row["event_seq"],
            "strategy_id": strategy_id, "rung": check["rung"],
            "override": override is not None, "history_report_sha256": sha}


def demote(store, strategy_id, *, reason, owner_ruling_ref, now, operator="LOCAL_OWNER_CLI"):
    """Append a ``STRATEGY_DEMOTION`` of the strategy's active promotion; refuses
    ``STRATEGY_NOT_PROMOTED`` when there is none."""
    if operator not in OPERATORS:
        raise PromotionRefused("OPERATOR_ORIGIN_INVALID")
    text = _text(reason, 10, 500, "DEMOTION_REASON_REQUIRED")
    ruling = _text(owner_ruling_ref, 3, 300, "OWNER_RULING_REF_REQUIRED")
    with store.transaction() as conn:
        active = active_promotion(conn, strategy_id)
        if active is None:
            raise PromotionRefused("STRATEGY_NOT_PROMOTED")
        body = json_safe({"version": DEMOTION_VERSION, "strategy_id": strategy_id,
                          "promotion_event_seq": active["event_seq"], "reason": text,
                          "owner_ruling_ref": ruling, "operator": operator,
                          "recorded_at": now.astimezone(UTC)})
        row = store.event(conn, DEMOTION_EVENT, body,
                          key=f"strategy-demotion:{strategy_id}:{active['event_seq']}")
    return {"action": "demote-strategy", "mode": "PAPER_ONLY", "event_seq": row["event_seq"],
            "strategy_id": strategy_id, "promotion_event_seq": active["event_seq"]}


# --- From a signal to a selected packet ---------------------------------------------------------


def paper_levels(reference_price, increment):
    """The packet's levels on the coin's grid (see the module text): ``entry_trigger`` =
    ``max_entry_price`` = the reference x 1.005 rounded up, the stop 2% below it rounded down, the
    target at reward/risk 2 at the max entry rounded up. Raises ``PAPER_LEVELS_INVALID``."""
    from catalyst_lab.crypto_maintenance import ceil_grid, floor_grid

    reference, increment = D(str(reference_price)), D(str(increment))
    if not reference.is_finite() or not increment.is_finite() or reference <= 0 or increment <= 0:
        raise ValueError("PAPER_LEVELS_INVALID")
    with localcontext() as context:
        context.prec = _PRECISION
        entry = ceil_grid(reference * (1 + ENTRY_COLLAR), increment)
        stop = floor_grid(entry * (1 - STOP_FRACTION), increment)
        target = ceil_grid(entry + REWARD_RISK_AT_MAX_ENTRY * (entry - stop), increment)
    if not 0 < stop < entry < target:
        raise ValueError("PAPER_LEVELS_INVALID")
    return {"entry_trigger": str(entry), "max_entry_price": str(entry), "stop": str(stop),
            "target": str(target)}


def signal_key(strategy_id, symbol, signal_at):
    return f"strategy-signal:{strategy_id}:{symbol}:{signal_at}"


def selection_key(strategy_id, symbol, signal_at):
    return f"strategy-signal:{strategy_id}:{symbol}:{signal_at}:selected"


def build_state(strategy, proposal, levels):
    """The reviewed-state shape of a strategy packet (its thesis is the strategy's rule)."""
    return json_safe({
        "market": "CRYPTO", "symbol": proposal["symbol"],
        "thesis": f"{strategy.strategy_id} (mechanical): {strategy.description}",
        "disproof": "The plan's stop trades (2% below the entry, widened to two hourly ranges by "
                    "CRYPTO_TRADE_PLAN_V1), or the 24-hour window ends.",
        "sources": [],
        "levels": levels,
        "agent_current_price": proposal["reference_price"],
        "agent_price_at": proposal["signal_at"],
        "strategy_signal": {
            "strategy_id": strategy.strategy_id, "trigger_rule": strategy.trigger_rule,
            "signal_at": proposal["signal_at"], "entry": proposal.get("entry"),
            "facts": proposal.get("facts") or {}, "hourly_range": proposal.get("hourly_range"),
            "slippage": proposal.get("slippage"),
        },
    })


def build_packet(strategy, proposal, *, levels, state, evidence_hash, signal_event_seq,
                 promotion_event_seq, now):
    signal_at = proposal["signal_at"]
    symbol = proposal["symbol"]
    expires = datetime.fromisoformat(signal_at) + ENTRY_WINDOW
    return json_safe({
        "cycle_id": str(uuid5(NAMESPACE_URL, f"{PATH_VERSION}:{strategy.strategy_id}:{symbol}:"
                                             f"{signal_at}")),
        "item_key": f"STRATEGY:{strategy.strategy_id}:{symbol}",
        "asset_id": symbol, "signal_id": f"{strategy.strategy_id}:{symbol}:{signal_at}",
        "market": "CRYPTO", "symbol": symbol, "revision": 1, "rank": 1,
        "research_origin": RESEARCH_ORIGIN, "selection_policy": SELECTION_POLICY,
        "execution_scope": "PAPER_ONLY", "levels": levels, "state": state,
        "thesis": state["thesis"], "disproof": state["disproof"], "sources": state["sources"],
        "created_at": now, "received_at": now, "expires_at": expires,
        "evidence_hash": evidence_hash,
        # The report-V3 packet shape: every crypto version of report-V3 picks applies.
        "report_schema_version": REPORT_SCHEMA_V3, "run_slot": signal_at,
        "strategy_id": strategy.strategy_id, "strategy_source": MECHANICAL_SOURCE,
        "strategy_path": PATH_VERSION, "signal_event_seq": signal_event_seq,
        "promotion_event_seq": promotion_event_seq,
        "receipt_id": None, "disposition": "SELECTED", "strategy_version": PRODUCT_VERSION,
        "entry": MARKETABLE_AT_SIGNAL, "entry_collar": str(ENTRY_COLLAR),
    })


def record_signal(store, strategy, proposal, *, promotion, increment, bars_window, now):
    """One fresh proposal's ``STRATEGY_SIGNAL`` and its ``RESEARCH_SELECTED`` packet, in one
    transaction; idempotent by key. Returns the selection row, or None when already recorded."""
    from catalyst_lab.jev_contract import digest, encoded

    symbol, signal_at = proposal["symbol"], proposal["signal_at"]
    levels = paper_levels(proposal["reference_price"], increment)
    state = build_state(strategy, proposal, levels)
    evidence_hash = digest(encoded(state))
    expires = datetime.fromisoformat(signal_at) + ENTRY_WINDOW
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                        (signal_key(strategy.strategy_id, symbol, signal_at),)).fetchone():
            return None
        signal = store.event(conn, SIGNAL_EVENT, json_safe({
            "path_version": PATH_VERSION, "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.version, "strategy_stage": strategy.stage,
            "promotion_event_seq": promotion["event_seq"], "symbol": symbol,
            "signal_at": signal_at, "proposal": proposal, "levels": levels,
            "price_increment": str(increment), "expires_at": expires, "state": state,
            "evidence_hash": evidence_hash, "bars": bars_window,
            "orders": "VIA_ADMISSION_AND_THE_RISK_GATE_ONLY",
        }), key=signal_key(strategy.strategy_id, symbol, signal_at))
        packet = build_packet(strategy, proposal, levels=levels, state=state,
                              evidence_hash=evidence_hash, signal_event_seq=signal["event_seq"],
                              promotion_event_seq=promotion["event_seq"], now=now)
        return store.event(conn, "RESEARCH_SELECTED", {"packet": packet},
                           key=selection_key(strategy.strategy_id, symbol, signal_at))


@dataclass
class StrategySignalSource:
    """The runtime's hourly signal pass for configured, promoted strategies (see the module).

    ``reader`` has ``bars(symbol, start, end, timeframe)`` (``PublicCryptoBarReader``);
    ``increment(symbol)`` the broker's price increment or None; ``universe(conn, now)`` the
    symbols (default: the latest recorded research universe)."""

    store: object
    reader: object
    strategy_ids: tuple
    clock: object
    increment: object
    universe: object = None
    settle_seconds: int = SETTLE_SECONDS
    done: set = field(default_factory=set)
    last: dict = field(default_factory=dict)

    def symbols(self, conn, now):
        if self.universe is not None:
            return sorted(set(self.universe(conn, now)))
        from catalyst_lab.market_reality import recorded_universe

        recorded = recorded_universe(conn, now)
        return sorted(set(((recorded["universe"] or {}).get("symbols") or [])
                          if recorded else []))

    def hour(self, now):
        """The end of the last completed hour whose bar has settled."""
        settled = now - timedelta(seconds=self.settle_seconds)
        return settled.replace(minute=0, second=0, microsecond=0)

    def run_once(self):
        """One pass; returns its summary (also kept as ``last``). Each hour is scanned once."""
        from catalyst_lab.pick_outcomes import parse_bars

        now = self.clock().astimezone(UTC)
        until = self.hour(now)
        summary = Counter()
        if until in self.done:
            return dict(self.last)
        strategies.ensure_plugins_loaded()
        with self.store.repo.connect() as conn:
            symbols = self.symbols(conn, now)
            active = {}
            for strategy_id in self.strategy_ids:
                try:
                    strategy = strategies.get(strategy_id)
                except ValueError:
                    summary["strategies_unregistered"] += 1
                    continue
                if not strategies.paper_eligible(strategy):
                    summary["strategies_not_eligible"] += 1
                    continue
                promotion = active_promotion(conn, strategy_id)
                if promotion is None:
                    summary["strategies_not_promoted"] += 1
                    continue
                active[strategy_id] = (strategy, promotion)
        summary["coins"] = len(symbols)
        summary["strategies_active"] = len(active)
        since = until - core.HOUR
        failed = False
        for strategy, promotion in active.values():
            fetch_start = since - strategy.history_hours * core.HOUR
            for symbol in symbols:
                try:
                    bars = parse_bars(self.reader.bars(symbol, fetch_start, until, "1Hour"))
                except Exception:  # noqa: BLE001 -- one coin's read; the hour is retried.
                    summary["bar_fail"] += 1
                    failed = True
                    continue
                for proposal in strategy.signals(bars, {"symbol": symbol, "since": since,
                                                        "until": until}):
                    summary["signals"] += 1
                    self._propose(strategy, promotion, symbol, proposal, now, fetch_start,
                                  until, summary)
        if not failed:
            self.done.add(until)
        self.last = {"version": PATH_VERSION, "hour": until.isoformat(),
                     "checked_at": now.isoformat(), **dict(sorted(summary.items()))}
        return dict(self.last)

    def _propose(self, strategy, promotion, symbol, proposal, now, fetch_start, until, summary):
        if proposal.get("entry") != MARKETABLE_AT_SIGNAL or proposal.get("symbol") != symbol:
            summary["unsupported_entry"] += 1
            return
        signal_at = datetime.fromisoformat(proposal["signal_at"])
        if now >= signal_at + ENTRY_WINDOW:
            summary["stale"] += 1  # A marketable entry has no later entry.
            return
        try:
            increment = self.increment(symbol)
        except Exception:  # noqa: BLE001 -- the broker read; never a guessed grid.
            increment = None
        if increment is None:
            summary["increment_unavailable"] += 1
            return
        try:
            row = record_signal(self.store, strategy, proposal, promotion=promotion,
                                increment=increment, now=now,
                                bars_window={"source": BAR_SOURCE, "timeframe": "1Hour",
                                             "fetch_start": fetch_start.isoformat(),
                                             "fetch_end": until.isoformat()})
        except ValueError as exc:
            summary["refused:" + str(exc)[:60]] += 1
            return
        summary["selected" if row is not None else "already_recorded"] += 1


__all__ = [
    "DEMOTION_EVENT", "ENTRY_COLLAR", "ENTRY_WINDOW", "PATH_VERSION", "PERMANENT_REFUSALS",
    "PROMOTION_EVENT", "PROMOTION_VERSION", "PromotionRefused", "SELECTION_POLICY",
    "SIGNAL_EVENT", "STRATEGIES_ENV", "STRATEGY_NOT_CONFIGURED", "StrategySignalSource",
    "active_promotion", "build_packet", "build_state", "configured_strategies", "demote",
    "history_entry", "paper_levels", "promote", "record_signal",
]
