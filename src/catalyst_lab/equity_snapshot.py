"""``ACCOUNT_EQUITY_SNAPSHOT_V1``: the paper account's equity, recorded at most once per five
minutes (package public-page-v3, 2026-10-03; owner: "live page doesn't show charts and current
amount").

**Record-only. Paper only.** Nothing here reads the broker: the account safety tick already
reads the account (the request-governed snapshot it shares with every per-setup view, or one
direct read), and ``record`` appends what that read returned. No rule reads these events; the
public page's equity curve does (migration 029, ``lab.public_page_equity``).

One ``ACCOUNT_EQUITY_SNAPSHOT`` managed event (no setup) per five-minute UTC bucket, key
``account-equity-snapshot-v1:<bucket start>``: the first account read in a bucket is recorded,
later reads in the same bucket are skipped (never compared, never rewritten). Body: equity,
cash, long market value, the positions' unrealized P&L, Alpaca's ``last_equity`` and the time
the account was read. A read without a positive equity records nothing. A failure here never
reaches the account tick: the caller runs it in a savepoint and drops it.
"""

from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import InvalidOperation

SNAPSHOT_EVENT = "ACCOUNT_EQUITY_SNAPSHOT"
SNAPSHOT_VERSION = "ACCOUNT_EQUITY_SNAPSHOT_V1"
INTERVAL_SECONDS = 300


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = D(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def bucket_start(at):
    """The start of ``at``'s five-minute UTC bucket."""
    at = at.astimezone(UTC)
    seconds = int(at.timestamp()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    return datetime.fromtimestamp(seconds, UTC)


def snapshot_key(at):
    return f"account-equity-snapshot-v1:{bucket_start(at).isoformat()}"


def snapshot_body(account, positions, observed_at):
    """The event body, or None when the read has no positive equity."""
    equity = _number((account or {}).get("equity"))
    if equity is None or equity <= 0:
        return None
    unrealized = [_number(p.get("unrealized_pl")) for p in positions or ()]
    known = [u for u in unrealized if u is not None]
    return {
        "version": SNAPSHOT_VERSION, "equity": equity,
        "cash": _number(account.get("cash")),
        "long_market_value": _number(account.get("long_market_value")),
        "unrealized_pl": sum(known, D(0)) if len(known) == len(unrealized) else None,
        "positions": len(unrealized),
        "last_equity": _number(account.get("last_equity")),
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "source": "ACCOUNT_SAFETY_TICK_READ",
    }


class EquitySnapshotRecorder:
    """Appends at most one snapshot per bucket; remembers the last bucket it settled so most
    ticks cost no query at all."""

    def __init__(self):
        self.settled = None  # The bucket key last recorded or found recorded.

    def record(self, conn, store, account, positions, observed_at):
        """``RECORDED``, ``ALREADY_RECORDED`` or ``NO_EQUITY`` (``observed_at``: when the
        account was read)."""
        key = snapshot_key(observed_at)
        if key == self.settled:
            return "ALREADY_RECORDED"
        body = snapshot_body(account, positions, observed_at)
        if body is None:
            return "NO_EQUITY"
        found = conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)).fetchone()
        if found is None:
            store.event(conn, SNAPSHOT_EVENT, body, key=key)
        self.settled = key
        return "ALREADY_RECORDED" if found else "RECORDED"


__all__ = ["INTERVAL_SECONDS", "SNAPSHOT_EVENT", "SNAPSHOT_VERSION", "EquitySnapshotRecorder",
           "bucket_start", "snapshot_body", "snapshot_key"]
