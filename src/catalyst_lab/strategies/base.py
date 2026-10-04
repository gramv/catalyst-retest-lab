"""The strategy plug-in interface (``STRATEGY_REGISTRY_V1``; see ``docs/STRATEGY-PLUGINS.md``).

A plug-in is one module that builds one ``Strategy`` record. The record declares:

* ``strategy_id`` (``NAME_V<n>``: ``name`` and ``version`` joined) -- a named version under the
  owner's 2026-09-24 ruling: a changed rule is a new id, never an edit of a registered one;
* ``entry_types`` it trades (``core.PULLBACK``, ``core.IMMEDIATE``, ``core.BREAKOUT``);
* ``trigger_rule``: the named rule that decides its entry, and for a strategy that trades live,
  ``live_trigger(state)``, the trigger module the engine dispatches to;
* ``plan_rules``: the trade-plan version its stop, target and window follow
  (``CRYPTO_TRADE_PLAN_V1`` for both registered strategies);
* ``jev_questions``: the Jev question sets it uses (none for a mechanical strategy in shadow);
* ``sources``: where its proposals come from (``RESEARCH_REPORT``: research agents' report-V3
  picks; ``MECHANICAL``: its own ``signals``);
* ``stage`` on the promotion ladder (``HISTORY_TEST`` -> ``SHADOW`` -> ``LIVE_PAPER``); only a
  ``LIVE_PAPER`` strategy is ever admitted to the paper account, and only from the sources it
  declares;
* for a mechanical strategy, ``signals(bars, context) -> [proposal]`` (pure) and
  ``simulate(proposal, minute_bars) -> outcome`` (pure, on the shared core);
* ``history_variants``: the parameter variants the history tester (``HISTORY_TEST_V1``) tries,
  declared up front (each a dict with a ``variant_id``; ``signals`` reads one from
  ``context["parameters"]``). Every one is counted in the trials ledger.
* ``history_hours`` (package plugin-c3): the completed 1-hour bars ``signals`` needs before the
  first bar it evaluates (the shadow and the paper signal source fetch that many); the default
  is the breakout's 216.

The record checks itself on construction; the registry (``catalyst_lab.strategies``) refuses
duplicates. Nothing here reads a ledger, a broker or a clock.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from catalyst_lab.strategies import core

REGISTRY_VERSION = "STRATEGY_REGISTRY_V1"
HISTORY_TEST, SHADOW, LIVE_PAPER = "HISTORY_TEST", "SHADOW", "LIVE_PAPER"
STAGES = (HISTORY_TEST, SHADOW, LIVE_PAPER)
RESEARCH_REPORT, MECHANICAL = "RESEARCH_REPORT", "MECHANICAL"
SOURCES = frozenset({RESEARCH_REPORT, MECHANICAL})
# The trade-plan versions a strategy may name (the plan math lives in ``core``/``trade_plan``).
PLAN_RULES = frozenset({"CRYPTO_TRADE_PLAN_V1"})
_ID = re.compile(r"^([A-Z][A-Z0-9_]*)_V([1-9][0-9]*)$")
MAX_HISTORY_HOURS = 24 * 120  # The history tester's padding (``HISTORY_PAD``).


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    name: str
    version: int
    description: str
    entry_types: frozenset
    trigger_rule: str
    plan_rules: str
    jev_questions: tuple
    sources: frozenset
    stage: str
    markets: frozenset = frozenset({"CRYPTO"})
    live_trigger: Callable | None = None
    signals: Callable | None = None
    simulate: Callable | None = None
    parameters: dict = field(default_factory=dict)
    history_variants: tuple = ()
    history_hours: int = core.BREAKOUT_HISTORY_HOURS

    def __post_init__(self):
        match = _ID.fullmatch(self.strategy_id or "")
        if not match or match.group(1) != self.name or int(match.group(2)) != self.version:
            raise ValueError("STRATEGY_ID_MUST_BE_NAME_V_VERSION")
        if not self.entry_types or not set(self.entry_types) <= core.ENTRY_TYPES:
            raise ValueError("STRATEGY_ENTRY_TYPES_INVALID")
        if self.plan_rules not in PLAN_RULES:
            raise ValueError("STRATEGY_PLAN_RULES_UNKNOWN")
        if self.stage not in STAGES:
            raise ValueError("STRATEGY_STAGE_INVALID")
        if not self.sources or not set(self.sources) <= SOURCES:
            raise ValueError("STRATEGY_SOURCES_INVALID")
        if not self.trigger_rule or not self.description:
            raise ValueError("STRATEGY_DECLARATION_INCOMPLETE")
        if MECHANICAL in self.sources and (self.signals is None or self.simulate is None):
            raise ValueError("MECHANICAL_STRATEGY_NEEDS_SIGNALS_AND_SIMULATE")
        if self.stage == LIVE_PAPER and self.live_trigger is None:
            raise ValueError("LIVE_STRATEGY_NEEDS_LIVE_TRIGGER")
        ids = [v.get("variant_id") if isinstance(v, dict) else None
               for v in self.history_variants]
        if (not isinstance(self.history_variants, tuple)
                or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids)
                or (ids and MECHANICAL not in self.sources)):
            raise ValueError("STRATEGY_HISTORY_VARIANTS_INVALID")
        if type(self.history_hours) is not int or not 1 <= self.history_hours <= MAX_HISTORY_HOURS:
            raise ValueError("STRATEGY_HISTORY_HOURS_INVALID")

    @property
    def live(self):
        return self.stage == LIVE_PAPER

    def accepts_reports(self):
        """Whether research agents may propose for it: a live strategy fed by reports."""
        return self.live and RESEARCH_REPORT in self.sources

    @property
    def mechanical(self):
        """A strategy with its own signals (the paper path's only kind)."""
        return MECHANICAL in self.sources and self.signals is not None

    def admission_fields(self):
        """The fields a setup's WATCHING state records at admission."""
        return {"strategy_id": self.strategy_id, "strategy_version": self.version}

    def record(self):
        """The declaration as data (for documents, the weekly review and tests)."""
        return {"strategy_id": self.strategy_id, "name": self.name, "version": self.version,
                "description": self.description, "entry_types": sorted(self.entry_types),
                "trigger_rule": self.trigger_rule, "plan_rules": self.plan_rules,
                "jev_questions": list(self.jev_questions), "sources": sorted(self.sources),
                "stage": self.stage, "markets": sorted(self.markets),
                "mechanical": self.signals is not None, "parameters": dict(self.parameters),
                "history_variants": [dict(v) for v in self.history_variants]}


__all__ = ["HISTORY_TEST", "LIVE_PAPER", "MAX_HISTORY_HOURS", "MECHANICAL", "PLAN_RULES",
           "REGISTRY_VERSION", "RESEARCH_REPORT", "SHADOW", "SOURCES", "STAGES", "Strategy"]
