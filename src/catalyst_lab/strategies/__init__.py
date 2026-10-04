"""``STRATEGY_REGISTRY_V1``: the strategy plug-ins (package strategy-c1, 2026-10-03; owner:
"where I want this project to excel is plugins -- you should be able to plug in a strategy and
test it on live"; plan docs/TRADING-QUALITY-PLAN.md section 5; guide docs/STRATEGY-PLUGINS.md).

The registry is the one list of strategies. Every proposal and every admitted setup names one:

* a report-V3 pick may carry ``strategy_id`` (validated against this registry at intake; a pick
  without one is ``DEFAULT_STRATEGY_ID``, today's pullback strategy, so every existing report
  and agent is unchanged);
* admission stamps ``strategy_id`` and ``strategy_version`` in the setup's WATCHING state
  (report-V3 crypto setups, the setups the strategy layer covers); a setup without them, every
  setup admitted before this version, reads as ``DEFAULT_STRATEGY_ID``;
* the engine's trigger dispatch (``managed_execution.crypto_trigger_rules``) and the system
  check's traded entry types go through the setup's strategy.

Mechanical strategies run in shadow (``strategy_shadow``), never on the account, until a named
promotion under the owner's yes.

Package plugin-c3 (2026-10-03):

* **Drop-in plug-ins** (``STRATEGY_PLUGIN_LOADER_V1``, ``strategies/loader.py``): ``load_plugins``
  adds strategies from a folder (``CATALYST_STRATEGY_PLUGINS_DIR``) and from installed entry
  points (group ``catalyst_lab.strategies``) to this registry, validated, never replacing a
  built-in; ``ensure_plugins_loaded`` does it once per process (the runtime, the nightly shadow
  and the history tester call it). The stable author surface is ``strategies.sdk``.
* **The paper path** (``STRATEGY_PAPER_PATH_V1``, ``strategy_paper.py``): a promoted mechanical
  strategy's signal becomes a ``STRATEGY_SIGNAL_SELECTION_V1`` packet. Its setup stamps
  ``strategy_path`` with the promotion and the signal; ``live_trigger`` then dispatches it to the
  existing trigger modules (the marketable entry is a touch of a collar above the signal price),
  and ``admission_fields`` admits it although the strategy's declared stage is not
  ``LIVE_PAPER``: the owner's promotion record, checked by the admission SQL, is its stage.
"""

from catalyst_lab.strategies.base import (
    LIVE_PAPER,
    MECHANICAL,
    REGISTRY_VERSION,
    RESEARCH_REPORT,
    SHADOW,
    Strategy,
)
from catalyst_lab.strategies.breakout_7d_vol2x_v1 import BREAKOUT_7D_VOL2X_V1
from catalyst_lab.strategies.pullback_v1 import PULLBACK_V1

DEFAULT_STRATEGY_ID = PULLBACK_V1.strategy_id
STRATEGY_NOT_REGISTERED = "STRATEGY_NOT_REGISTERED"
STRATEGY_NOT_OPEN_TO_REPORTS = "STRATEGY_NOT_OPEN_TO_REPORTS"
STRATEGY_NOT_LIVE = "STRATEGY_NOT_LIVE"


def _registry(*strategies):
    found = {}
    for strategy in strategies:
        if not isinstance(strategy, Strategy):
            raise TypeError("STRATEGY_RECORD_REQUIRED")
        if strategy.strategy_id in found:
            raise ValueError("DUPLICATE_STRATEGY_ID")
        found[strategy.strategy_id] = strategy
    return found


# Registration is this list: a plug-in module builds its record, and one line here adds it.
REGISTRY = _registry(PULLBACK_V1, BREAKOUT_7D_VOL2X_V1)
# The built-ins, fixed at import: a drop-in plug-in may never take one of their ids or names.
BUILTINS = dict(REGISTRY)

# STRATEGY_PAPER_PATH_V1 (package plugin-c3): the selection policy of a promoted strategy's
# signal packet and the state field its setup records.
PAPER_PATH_VERSION = "STRATEGY_PAPER_PATH_V1"
SIGNAL_SELECTION_POLICY = "STRATEGY_SIGNAL_SELECTION_V1"
STRATEGY_NOT_PAPER_ELIGIBLE = "STRATEGY_NOT_PAPER_ELIGIBLE"


def get(strategy_id):
    """The registered strategy; ``STRATEGY_NOT_REGISTERED`` (ValueError) otherwise."""
    strategy = REGISTRY.get(strategy_id) if isinstance(strategy_id, str) else None
    if strategy is None:
        raise ValueError(STRATEGY_NOT_REGISTERED)
    return strategy


def report_strategy(strategy_id):
    """Intake's check of a pick's declared ``strategy_id``: registered, live and fed by
    research reports. Returns the id; raises ValueError with the code."""
    strategy = get(strategy_id)
    if not strategy.accepts_reports():
        raise ValueError(STRATEGY_NOT_OPEN_TO_REPORTS)
    return strategy_id


def strategy_id_of(*records):
    """The ``strategy_id`` the first record that names one carries (a setup's state, then its
    admitted packet), else ``DEFAULT_STRATEGY_ID``."""
    for record in records:
        if isinstance(record, dict) and record.get("strategy_id"):
            return record["strategy_id"]
    return DEFAULT_STRATEGY_ID


def for_packet(packet):
    """The strategy of a proposal packet (its ``strategy_id``, else the default)."""
    return get(strategy_id_of(packet))


def for_state(state):
    """The strategy of a setup's state (its stamped ``strategy_id``, else the default)."""
    return get(strategy_id_of(state))


def applies(packet):
    """The setups the strategy layer stamps: crypto setups admitted from report-V3 packets
    (the scope of every crypto version since 2026-09-26)."""
    return (isinstance(packet, dict) and packet.get("market") == "CRYPTO"
            and packet.get("report_schema_version") == "AGENT_RESEARCH_REPORT_V3")


def is_signal_packet(packet):
    """A promoted strategy's signal packet (``STRATEGY_PAPER_PATH_V1``)."""
    return isinstance(packet, dict) and packet.get("selection_policy") == SIGNAL_SELECTION_POLICY


def on_paper_path(state):
    """A setup admitted from a promoted strategy's signal (its state records the path)."""
    return isinstance(state, dict) and state.get("strategy_path") == PAPER_PATH_VERSION


def paper_eligible(strategy):
    """Whether the paper path may carry ``strategy``: a mechanical strategy (its own signals and
    simulation) that is not fed by research reports alone."""
    return strategy.mechanical and strategy.simulate is not None


def admission_fields(packet):
    """The strategy fields a WATCHING state records at admission: ``strategy_id`` and
    ``strategy_version`` for a report-V3 crypto setup; nothing for every other setup. A strategy
    that is not live on paper is refused (``STRATEGY_NOT_LIVE``): fail-closed, never admitted.

    A promoted strategy's signal packet (``STRATEGY_PAPER_PATH_V1``) also records the path, its
    promotion and its signal; its strategy must be a registered mechanical one
    (``STRATEGY_NOT_PAPER_ELIGIBLE`` otherwise). Its promotion is the admission SQL's check."""
    if not applies(packet):
        return {}
    strategy = for_packet(packet)
    if is_signal_packet(packet):
        if not paper_eligible(strategy):
            raise ValueError(STRATEGY_NOT_PAPER_ELIGIBLE)
        return {**strategy.admission_fields(), "strategy_path": PAPER_PATH_VERSION,
                "promotion_event_seq": packet["promotion_event_seq"],
                "signal_event_seq": packet["signal_event_seq"]}
    if not strategy.live:
        raise ValueError(STRATEGY_NOT_LIVE)
    return strategy.admission_fields()


def traded_entry_types(packet):
    """The entry types the packet's strategy trades (the system check's ``entry_type_traded``)."""
    return for_packet(packet).entry_types


def live_trigger(state):
    """The trigger module the setup's strategy dispatches to (None: today's trigger).

    A setup on the paper path (``STRATEGY_PAPER_PATH_V1``) dispatches to the trigger version it
    recorded at admission, as ``PULLBACK_V1`` does (``CRYPTO_COINBASE_TRIGGER_V1`` or
    ``CRYPTO_ALPACA_TRIGGER_V1``): its marketable entry is a touch of its entry trigger, set a
    collar above the signal price, so no new trigger or order code exists for it."""
    strategy = for_state(state)
    if on_paper_path(state):
        if not paper_eligible(strategy):
            raise ValueError(STRATEGY_NOT_PAPER_ELIGIBLE)
        return PULLBACK_V1.live_trigger(state)
    if not strategy.live:
        raise ValueError(STRATEGY_NOT_LIVE)
    return strategy.live_trigger(state)


def mechanical(stage=SHADOW):
    """The registered mechanical strategies at ``stage``, by id."""
    return [s for _, s in sorted(REGISTRY.items())
            if MECHANICAL in s.sources and s.stage == stage]


def records():
    return [REGISTRY[k].record() for k in sorted(REGISTRY)]


# --- Drop-in plug-ins (STRATEGY_PLUGIN_LOADER_V1, package plugin-c3) ----------------------------

_PLUGINS = {"loaded": None}


def load_plugins(*, folder=None, entry_points=True, strict=False):
    """Add drop-in plug-ins to the registry: every strategy of ``folder`` (a path, a list of
    paths, or None) and, with ``entry_points``, of the installed ``catalyst_lab.strategies``
    entry points. Returns the ``LoadResult``; ``strict`` raises the first refusal
    (``PluginError``)."""
    from catalyst_lab.strategies import loader

    candidates, refused = [], []
    folders = [] if folder is None else list(folder) if isinstance(folder, (list, tuple)) \
        else [folder]
    for one in folders:
        try:
            candidates += loader.folder_candidates(one)
        except loader.PluginError as exc:
            if strict:
                raise
            refused.append((exc.origin, exc.code))
    if entry_points:
        candidates += loader.entry_point_candidates()
    result = loader.load_into(REGISTRY, candidates, builtins=BUILTINS, strict=strict)
    result.errors[:0] = refused
    return result


def ensure_plugins_loaded(environ=None):
    """``load_plugins`` once per process from ``CATALYST_STRATEGY_PLUGINS_DIR`` and the entry
    points (never strict: a refused plug-in is reported, the built-ins keep working)."""
    from catalyst_lab.strategies import loader

    if _PLUGINS["loaded"] is None:
        _PLUGINS["loaded"] = load_plugins(folder=loader.configured_folders(environ))
    return _PLUGINS["loaded"]


__all__ = [
    "BREAKOUT_7D_VOL2X_V1", "BUILTINS", "DEFAULT_STRATEGY_ID", "LIVE_PAPER", "MECHANICAL",
    "PAPER_PATH_VERSION", "PULLBACK_V1", "REGISTRY", "REGISTRY_VERSION", "RESEARCH_REPORT",
    "SHADOW", "SIGNAL_SELECTION_POLICY", "STRATEGY_NOT_LIVE", "STRATEGY_NOT_OPEN_TO_REPORTS",
    "STRATEGY_NOT_PAPER_ELIGIBLE", "STRATEGY_NOT_REGISTERED", "Strategy", "admission_fields",
    "applies", "ensure_plugins_loaded", "for_packet", "for_state", "get", "is_signal_packet",
    "live_trigger", "load_plugins", "mechanical", "on_paper_path", "paper_eligible", "records",
    "report_strategy", "strategy_id_of", "traded_entry_types",
]
