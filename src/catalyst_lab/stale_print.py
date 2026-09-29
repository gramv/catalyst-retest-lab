"""``STALE_PRINT_ABOVE_TRIGGER_V1``: a late print above the entry trigger no longer revokes a
crypto setup (owner approval, 2026-09-28: "approve the late-price fix").

The managed runtime writes and evaluates a WATCHING setup's market print on its own when the
print arrives more than ``QUIET_PRINT_MAX_AGE_SECONDS`` after the trade (or without a quote, or
while entries are blocked), and revokes the setup ``DATA_FEED_FAILURE`` when the print is more
than five seconds old by evaluation time (``managed_runtime._process_print``). On Railway,
Alpaca's crypto trade feed delivered some prints two to five seconds after the trade, and the
first live day lost three setups this way to prints far above their entry triggers (DOGE, DOT
and XRP, 3-7.5% above; 2026-09-28).

A print strictly above the entry trigger can neither trigger nor invalidate the setup: the
trigger is a touch at or below the entry trigger, and the stop lies below it. Under this
version such a late print is consumed as harmless (``STALE_PRINT_ABOVE_TRIGGER``) and the setup
keeps watching. Nothing else changes: a late print at or below the entry trigger still revokes
the setup, a print stamped ahead of the runtime's clock still revokes it, and a print within
five seconds is evaluated exactly as before.

Admission records the version on every ``CRYPTO_ALPACA_TRIGGER_V1`` setup (a crypto setup from
a report-V3 packet). Setups admitted before it, and every other setup, keep today's revocation.
"""

from decimal import Decimal

from catalyst_lab import crypto_trigger

STALE_PRINT_VERSION = "STALE_PRINT_ABOVE_TRIGGER_V1"
CONSUMED_REASON = "STALE_PRINT_ABOVE_TRIGGER"


def admission_fields(packet):
    """The state field admission records for a report-V3 crypto setup; nothing otherwise."""
    if not crypto_trigger.applies(packet):
        return {}
    return {"stale_print_version": STALE_PRINT_VERSION}


def active(state):
    """Whether a setup's state was admitted under this version."""
    return isinstance(state, dict) and state.get("stale_print_version") == STALE_PRINT_VERSION


def harmless(state, levels, printed):
    """True only for a setup of this version and a print strictly above its entry trigger.

    Anything unclear (a missing or unparsable price or level) is not harmless, so the print
    keeps today's handling.
    """
    if not active(state):
        return False
    try:
        return Decimal(str(printed["trade_price"])) > Decimal(str(levels["entry_trigger"]))
    except (KeyError, TypeError, ArithmeticError):
        return False
