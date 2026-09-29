"""Operator/scheduled CLI: compute and append ``PICK_SHADOW_OUTCOME_V1`` records.

Package results (plan phase 7, ``docs/CRYPTO-AGENT-LOOP.md`` 4.8). Deterministic and fully
offline: reads only Alpaca's *public* crypto minute bars (no API key --
``catalyst_lab.public_crypto_bars``) and needs no broker credential of its own. It places no
order, sends no risk authorization and never touches ``APCA_API_KEY_ID``/``APCA_API_SECRET_KEY``
even though it reads the same ``--config`` file every other operator script here uses (only its
``MANAGED_DATABASE_URL`` is read); the running app never has to be up for this to work, and
this can run as the owner's own daily scheduled job right after the research run's picks have
had a chance to fully play out (each pick's own window plus the 24-hour hold).

    python -m scripts.run_pick_shadow_outcomes --config PATH [--cycle-id UUID]
        [--since ISO8601] [--until ISO8601] [--limit N] [--force]

Every pick already recorded is skipped (idempotent); a pick whose window has not yet fully
elapsed is skipped and counted, never guessed at early. Re-running this exact command later is
always safe and picks up wherever it left off.
"""

import argparse
import json
from datetime import UTC, datetime

from catalyst_lab.authorization import RiskRepository
from catalyst_lab.managed_ops import load_private_config
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.pick_outcomes import run_shadow_outcome_job
from catalyst_lab.public_crypto_bars import PublicCryptoBarReader


def run(config, *, cycle_id=None, since=None, until=None, limit=200, force=False, now=None):
    store = ManagedStore(RiskRepository(config["environment"]["MANAGED_DATABASE_URL"]))
    bar_reader = PublicCryptoBarReader()
    try:
        summary = run_shadow_outcome_job(
            store, bar_reader, now=now or datetime.now(UTC), cycle_id=cycle_id, since=since,
            until=until, after_cycle_event_seq=0, limit=limit, force=force,
        )
    finally:
        bar_reader.close()
    return summary.to_dict()


def _aware(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise SystemExit("AWARE_TIMESTAMP_REQUIRED: --since/--until need a UTC offset")
    return parsed.astimezone(UTC)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True,
        help="The owner's existing private v2 configuration; only MANAGED_DATABASE_URL is read",
    )
    parser.add_argument("--cycle-id", default=None, help="Restrict to one research cycle (run)")
    parser.add_argument(
        "--since", default=None,
        help="Only cycles recorded at/after this aware ISO-8601 timestamp",
    )
    parser.add_argument(
        "--until", default=None,
        help="Only cycles recorded at/before this aware ISO-8601 timestamp",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Report-V3 cycles scanned in this invocation (page through a date range with "
             "--since/--until across repeated runs if there are more)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Recompute even a pick that already has a recorded shadow outcome",
    )
    args = parser.parse_args()
    if args.limit < 1:
        raise SystemExit("POSITIVE_LIMIT_REQUIRED")
    try:
        config = load_private_config(args.config)
        summary = run(
            config, cycle_id=args.cycle_id, since=_aware(args.since), until=_aware(args.until),
            limit=args.limit, force=args.force,
        )
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(
            "PICK_SHADOW_OUTCOME_JOB_INCOMPLETE_RERUN_IS_IDEMPOTENT"
        ) from None
    print(json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
