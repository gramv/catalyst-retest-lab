"""Operator-only append-only cost reconciliation; no broker calls or trading actions."""

import argparse
from datetime import UTC, datetime

from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.managed_analytics import import_fill_cost_correction
from catalyst_lab.managed_ops import load_private_config, private_bytes
from catalyst_lab.managed_store import ManagedStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--costs", required=True, help="Owner-only JSON list of verified fill costs")
    args = parser.parse_args()
    try:
        config = load_private_config(args.config)
        rows = strict_json(private_bytes(args.costs))
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError
        store = ManagedStore(RiskRepository(config["environment"]["MANAGED_DATABASE_URL"]))
        for row in rows:
            import_fill_cost_correction(store, row, recorded_at=datetime.now(UTC))
    except Exception:
        raise SystemExit("COST_IMPORT_INCOMPLETE_EXACT_IDS_CAN_BE_RETRIED") from None
    print("COST_IMPORT_COMPLETE")


if __name__ == "__main__":
    main()
