"""``AI_MODE_SETTING_V1``: whether the managed paper engine uses an AI judge (package
oss-packaging, 2026-10-03; docs/CONFIGURATION.md, "AI mode").

The setting is ``CATALYST_AI_MODE``. Two named versions:

* ``JEV_AI_MODE_V1`` -- the reference deployment (docs/CONTRACT-RESOLUTIONS.md): research
  agents' picks, Jev's top-K selection, Jev's trade maintenance and window reviews. This is
  what an absent setting means, so every existing deployment keeps exactly its behaviour and
  its configuration hash.
* ``NO_AI_MODE_V1`` -- no AI judge anywhere:

  - **Selection is deterministic**: only promoted mechanical strategy plug-ins
    (``STRATEGY_PAPER_PATH_V1``: their own signals, ``STRATEGY_SIGNAL_SELECTION_V1``, no Jev
    receipt) reach admission. The research-report intake is not served (its routes answer
    503 ``MUSE_REPORT_INTAKE_NOT_CONFIGURED``), so no pick can wait on a judge.
  - **Maintenance is code rules only**: every admitted setup takes the ``FIXED_EXIT`` arm
    (the broker-held stop, the plan's target, the trade window, the daily limits and the
    operator's flatten; ``CRYPTO_WINDOW_HOLD_V1`` instead of the window review), and
    ``MANAGED_MANAGEMENT_REVIEWS`` must be ``DISABLED``, so no ``CRYPTO_MAINTENANCE_V5``
    question is ever asked. ``CRYPTO_MAINTENANCE_V4``'s raise guards have nothing to guard:
    no one proposes a raise.
  - **Universe**: with no research report there is no recorded research universe, so the paper
    path's strategies scan every Alpaca USD pair of ``ALPACA_CRYPTO_SECTORS_V1``
    (``strategy_universe``), the history tester's default universe.
  - Protection, reconciliation, the risk gate's exact one-use authorization of every broker
    change and every account-risk limit are unchanged.

A half-configured AI mode is refused at startup with a code (``findings`` below): ``NO_AI_MODE_V1``
with a TypeSafe credential or credential file, management reviews enabled, a Jev selection
rule, K, quality floor or budget, research-agent tokens or a research schedule; and an unknown
value. ``JEV_AI_MODE_V1`` keeps the checks it always had (the Railway profile requires the key,
the budget is required while reviews are enabled).
"""

AI_MODE_ENV = "CATALYST_AI_MODE"
SETTING_VERSION = "AI_MODE_SETTING_V1"
JEV_AI_MODE = "JEV_AI_MODE_V1"
NO_AI_MODE = "NO_AI_MODE_V1"
MODES = (JEV_AI_MODE, NO_AI_MODE)
DEFAULT_MODE = JEV_AI_MODE

UNKNOWN = "AI_MODE_UNKNOWN"
JEV_CREDENTIAL_PRESENT = "NO_AI_MODE_JEV_CREDENTIAL_PRESENT"
REVIEWS_ENABLED = "NO_AI_MODE_MANAGEMENT_REVIEWS_NOT_DISABLED"
JEV_SELECTION_CONFIGURED = "NO_AI_MODE_JEV_SELECTION_CONFIGURED"
JEV_BUDGET_CONFIGURED = "NO_AI_MODE_JEV_BUDGET_CONFIGURED"
RESEARCH_AGENTS_CONFIGURED = "NO_AI_MODE_RESEARCH_AGENTS_CONFIGURED"
RESEARCH_SCHEDULE_CONFIGURED = "NO_AI_MODE_RESEARCH_SCHEDULE_CONFIGURED"

# What turns an AI judge on; NO_AI_MODE_V1 refuses each by name (values are never read out).
JEV_CREDENTIALS = ("TYPESAFE_API_KEY", "TYPESAFE_ENV_FILE")
JEV_SELECTION = ("MANAGED_SELECTION_RULE", "MANAGED_TOPK_SELECTION_JSON",
                 "MANAGED_SELECTION_QUALITY_FLOOR")
JEV_BUDGET = ("JEV_MONTHLY_BUDGET_USD", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD",
              "JEV_BYTES_PER_TOKEN")
RESEARCH_AGENTS = "MANAGED_AGENT_TOKENS_JSON"
RESEARCH_SCHEDULE = "MANAGED_RESEARCH_SCHEDULE_JSON"
# Settings a NO_AI_MODE_V1 runtime still needs to compose (the Jev worker's database role and
# review policy stay configured, with nothing to review) and the ones it never reads.
NO_AI_NOT_REQUIRED = frozenset({*JEV_CREDENTIALS, *JEV_SELECTION, *JEV_BUDGET, RESEARCH_AGENTS,
                                RESEARCH_SCHEDULE})


class AiModeError(ValueError):
    """A refusal code plus the variable names involved (names only, never a value)."""

    def __init__(self, code, names=()):
        super().__init__(code)
        self.code = code
        self.names = tuple(sorted(names))


def mode_from_env(environ):
    """The configured mode: absent or empty means ``JEV_AI_MODE_V1`` (the reference
    deployment); anything else must be an exact version name (``AI_MODE_UNKNOWN``)."""
    value = environ.get(AI_MODE_ENV) or DEFAULT_MODE
    if value not in MODES:
        raise AiModeError(UNKNOWN, [AI_MODE_ENV])
    return value


def _present(environ, name):
    value = environ.get(name)
    return value is not None and value != ""


def _empty_agents(raw):
    from catalyst_lab.jev_contract import strict_json

    try:
        value = strict_json(raw)
    except (TypeError, ValueError):
        return False
    return value in ({}, [], None)


def findings(environ):
    """``[(code, names)]`` of a half-configured AI mode; empty when coherent."""
    try:
        mode = mode_from_env(environ)
    except AiModeError as exc:
        return [(exc.code, exc.names)]
    if mode == JEV_AI_MODE:
        return []
    found = []
    credentials = [n for n in JEV_CREDENTIALS if n in environ]
    if credentials:
        found.append((JEV_CREDENTIAL_PRESENT, credentials))
    if environ.get("MANAGED_MANAGEMENT_REVIEWS") != "DISABLED":
        found.append((REVIEWS_ENABLED, ["MANAGED_MANAGEMENT_REVIEWS"]))
    selection = [n for n in JEV_SELECTION if _present(environ, n)]
    if selection:
        found.append((JEV_SELECTION_CONFIGURED, selection))
    budget = [n for n in JEV_BUDGET if _present(environ, n)]
    if budget:
        found.append((JEV_BUDGET_CONFIGURED, budget))
    if _present(environ, RESEARCH_AGENTS) and not _empty_agents(environ[RESEARCH_AGENTS]):
        found.append((RESEARCH_AGENTS_CONFIGURED, [RESEARCH_AGENTS]))
    if _present(environ, RESEARCH_SCHEDULE):
        found.append((RESEARCH_SCHEDULE_CONFIGURED, [RESEARCH_SCHEDULE]))
    return found


def require(environ):
    """The mode, or ``AiModeError`` with the first finding's code and every finding's names."""
    found = findings(environ)
    if found:
        names = sorted({n for _, ns in found for n in ns})
        raise AiModeError(found[0][0], names)
    return mode_from_env(environ)


def strategy_universe(conn=None, now=None):
    """``NO_AI_MODE_V1``'s coins for ``STRATEGY_PAPER_PATH_V1``: every Alpaca USD pair of
    ``ALPACA_CRYPTO_SECTORS_V1`` (the history tester's default universe). The reference mode
    reads the latest recorded research universe instead; with no research reports there is
    none."""
    from catalyst_lab.managed_classification import ALPACA_CRYPTO_SECTOR_OF

    return sorted(ALPACA_CRYPTO_SECTOR_OF)


def record(mode):
    """The evidence a runtime records for its mode (the configuration hash's ``ai_mode``)."""
    return {"setting_version": SETTING_VERSION, "mode": mode,
            "selection": ("STRATEGY_SIGNAL_SELECTION_V1 only (promoted mechanical plug-ins)"
                          if mode == NO_AI_MODE else "research reports + Jev selection rule"),
            "maintenance": ("FIXED_EXIT arm only; no management reviews"
                            if mode == NO_AI_MODE else "randomized arms; reviews per setting"),
            "strategy_universe": ("ALPACA_CRYPTO_SECTORS_V1 pairs" if mode == NO_AI_MODE
                                  else "latest recorded research universe")}


__all__ = ["AI_MODE_ENV", "AiModeError", "DEFAULT_MODE", "JEV_AI_MODE", "MODES", "NO_AI_MODE",
           "NO_AI_NOT_REQUIRED", "SETTING_VERSION", "findings", "mode_from_env", "record",
           "require", "strategy_universe"]
