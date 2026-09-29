"""Operator-only Alpaca fee reconciliation (package fees-net-r); no trading action.

Reads CFEE/FEE account activities from Alpaca Paper (GET only, through the existing
read-only transport) since the last completed run, matches each activity to a
recorded managed fill by broker order id and time, and appends idempotent cost
evidence through the existing FILL_COST_CORRECTION event path (source
ALPACA_PAPER_ACTIVITY). This is the on-demand CLI counterpart of the app's own
periodic ``ManagedExecution.fee_backfill`` (wired into the runtime's reconciliation
cadence). It never places, amends or cancels an order, and never migrates, seeds or
inserts fixtures into a database.

    python -m scripts.import_alpaca_fees --config PATH [--lookback-hours 24]
"""

import argparse
import json
from datetime import UTC, datetime, timedelta

from catalyst_lab.alpaca import AlpacaCredentials, AlpacaReadOnly
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.managed_analytics import import_alpaca_fee_activities
from catalyst_lab.managed_ops import load_private_config
from catalyst_lab.managed_store import ManagedStore

FEE_BACKFILL_EVENT = "ALPACA_FEE_BACKFILL_COMPLETED"
DEFAULT_LOOKBACK_HOURS = 24


def run(config, *, lookback_hours=DEFAULT_LOOKBACK_HOURS):
    env = config["environment"]
    store = ManagedStore(RiskRepository(env["MANAGED_DATABASE_URL"]))
    lookback = timedelta(hours=lookback_hours)
    with store.repo.connect() as conn:
        anchor = conn.execute(
            "SELECT max(recorded_at) AS at FROM lab.managed_events WHERE kind=%s",
            (FEE_BACKFILL_EVENT,),
        ).fetchone()["at"]
        if anchor is None:
            anchor = conn.execute(
                "SELECT min(filled_at) AS at FROM lab.managed_fills"
            ).fetchone()["at"]
    after = anchor - lookback if anchor is not None else None
    if after is None:
        return {"status": "NO_FILLS_RECORDED_YET"}
    broker = AlpacaReadOnly(AlpacaCredentials(env["APCA_API_KEY_ID"], env["APCA_API_SECRET_KEY"]))
    try:
        activities = broker.fee_activities_since(after)
    finally:
        broker.close()
    with store.repo.connect() as conn:
        fills = conn.execute(
            "SELECT * FROM lab.managed_fills WHERE filled_at>=%s", (after,)
        ).fetchall()
    summary = import_alpaca_fee_activities(store, fills, activities, recorded_at=datetime.now(UTC))
    summary["window_after"] = after.isoformat()
    with store.transaction() as conn:
        store.event(conn, FEE_BACKFILL_EVENT, summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS,
        help="how far back to widen the read window past the last completed run",
    )
    args = parser.parse_args()
    try:
        config = load_private_config(args.config)
        summary = run(config, lookback_hours=args.lookback_hours)
    except Exception:
        raise SystemExit("ALPACA_FEE_IMPORT_INCOMPLETE_EXACT_IDS_CAN_BE_RETRIED") from None
    print(json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
