"""``PULLBACK_V1``: today's strategy, as a plug-in (``STRATEGY_REGISTRY_V1``).

Research agents propose report-V3 crypto picks (entry trigger, max entry, stop, target, thesis);
Jev's top-K selection and ``SYSTEM_CHECK_V1`` admit them; ``CRYPTO_TRADE_PLAN_V1`` shapes the
stop and target; the setup waits for a pullback (or immediate) touch of its entry trigger under
the trigger version it recorded at admission (``CRYPTO_COINBASE_TRIGGER_V1`` or
``CRYPTO_ALPACA_TRIGGER_V1``; earlier setups today's trigger). This record changes none of
that: it names the existing pieces. Breakout entries stay refused (``BREAKOUT_NOT_ENABLED``).
"""

from catalyst_lab.strategies import core
from catalyst_lab.strategies.base import LIVE_PAPER, RESEARCH_REPORT, Strategy


def live_trigger(state):
    """The crypto trigger version a setup recorded, as its module (``coinbase_trigger``,
    ``crypto_trigger``), or None for today's trigger -- ``managed_execution``'s dispatch,
    unchanged."""
    from catalyst_lab import coinbase_trigger, crypto_trigger

    if coinbase_trigger.active(state):
        return coinbase_trigger
    if crypto_trigger.active(state):
        return crypto_trigger
    return None


PULLBACK_V1 = Strategy(
    strategy_id="PULLBACK_V1", name="PULLBACK", version=1,
    description="Research agents' report-V3 crypto picks: buy a pullback (or immediate) touch "
                "of the pick's entry trigger, with CRYPTO_TRADE_PLAN_V1's stop, 1.5R target "
                "and 24-hour window.",
    entry_types=frozenset({core.PULLBACK, core.IMMEDIATE}),
    trigger_rule="PRICE_AT_OR_BELOW_ENTRY_TRIGGER (CRYPTO_COINBASE_TRIGGER_V1 or "
                 "CRYPTO_ALPACA_TRIGGER_V1, as recorded at admission)",
    plan_rules="CRYPTO_TRADE_PLAN_V1",
    jev_questions=("JEV_TOP_K_SELECTION (the engine's configured version)",
                   "CRYPTO_MAINTENANCE_V5"),
    sources=frozenset({RESEARCH_REPORT}),
    stage=LIVE_PAPER,
    live_trigger=live_trigger,
)

__all__ = ["PULLBACK_V1", "live_trigger"]
