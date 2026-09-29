r"""Standalone shadow CLI; no case discovery, model approvals or order authority.

Keep the SQLite store outside the checkout. Supply real frozen Muse/Jev case JSON;
the CLI never constructs proposed levels or promotes benchmark fixtures into cases.

Examples (replace input paths; capture directories must not already exist):
  ./run python scripts/jev_shadow.py register \
      --store ~/.local/share/catalyst-retest-lab/jev-validation/shadow.sqlite3 \
      --cases /path/to/frozen-cases.json
  ./run python scripts/jev_shadow.py ingest \
      --store ~/.local/share/catalyst-retest-lab/jev-validation/shadow.sqlite3 \
      --observations /path/to/observations.jsonl
  ./run python scripts/jev_shadow.py report \
      --store ~/.local/share/catalyst-retest-lab/jev-validation/shadow.sqlite3
  ./run python scripts/jev_shadow.py collect \
      --store ~/.local/share/catalyst-retest-lab/jev-validation/shadow.sqlite3 \
      --products BTC-USD ETH-USD --seconds 25 --output-dir artifacts/new-feed-capture
  ./run python scripts/jev_shadow.py probe \
      --products BTC-USD ETH-USD --seconds 25 --output-dir artifacts/new-feed-probe

Limits: 1-300 seconds, 1-10 explicit unique USD products, 100 eligible prospective
venue-bound cases per collection, 50,000 messages or 64 MiB per capture. Register
accepts 1-200 frozen cases; ingest accepts 1-50,000 observations. There is one
connection and no reconnect. Coinbase ticker batching leaves complete-tape coverage
unverified. Reports preserve unknown fees, incomplete outcomes and continuity events.
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from catalyst_lab.jev_shadow import (
    ShadowCase,
    ShadowObservation,
    ShadowStore,
    export_metrics,
    prequalify,
)
from catalyst_lab.jev_shadow_feed import (
    bounded_seconds,
    collect_public_feed,
    feed_sessions,
    products_list,
    timestamp,
)


def seconds_argument(value):
    try:
        return bounded_seconds(int(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parser():
    result = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = result.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="Freeze supplied case JSON list; no discovery")
    register.add_argument("--store", required=True, type=Path)
    register.add_argument("--cases", required=True, type=Path)
    ingest = commands.add_parser("ingest", help="Append supplied observation JSONL")
    ingest.add_argument("--store", required=True, type=Path)
    ingest.add_argument("--observations", required=True, type=Path)
    report = commands.add_parser("report", help="Export outcomes, metrics and retained head")
    report.add_argument("--store", required=True, type=Path)
    report.add_argument("--as-of", type=timestamp)
    report.add_argument("--output", type=Path)
    for name in ("collect", "probe"):
        feed = commands.add_parser(name, help="Bounded public feed; probe never creates cases")
        feed.add_argument("--products", nargs="+", required=True)
        feed.add_argument("--seconds", type=seconds_argument, required=True)
        feed.add_argument("--output-dir", required=True, type=Path)
        if name == "collect":
            feed.add_argument("--store", required=True, type=Path)
    return result


def open_store(path, *, create=False):
    if not path.exists() and not create:
        raise ValueError("EXISTING_ISOLATED_SHADOW_STORE_REQUIRED")
    if path.exists():
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"shadow_events"}:
            raise ValueError("NOT_AN_ISOLATED_SHADOW_STORE")
    return ShadowStore(path)


def run(args):
    if args.command == "probe":
        return collect_public_feed(products=products_list(args.products), seconds=args.seconds,
                                   output_dir=args.output_dir)
    with open_store(args.store, create=args.command == "register") as store:
        if args.command == "register":
            if args.cases.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("CASE_FILE_TOO_LARGE")
            bodies = json.loads(args.cases.read_text())
            if not isinstance(bodies, list) or not 1 <= len(bodies) <= 200:
                raise ValueError("ONE_TO_200_CASES_REQUIRED")
            cases = [ShadowCase.from_dict(body) for body in bodies]
            if len({c.case_id for c in cases}) != len(cases):
                raise ValueError("DUPLICATE_CASE_IDS_IN_IMPORT")
            items = [{"case_id": case.case_id, "inserted": store.register(case),
                      "eligible": prequalify(case).eligible,
                      "qualification_reasons": list(prequalify(case).reasons),
                      "case_hash": case.case_hash} for case in cases]
            return {"items": items, "retained_head_hash": store.verify(),
                    "authorizes_orders": False}
        if args.command == "ingest":
            if args.observations.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("OBSERVATION_FILE_TOO_LARGE")
            lines = args.observations.read_text().splitlines()
            if not 1 <= len(lines) <= 50_000:
                raise ValueError("ONE_TO_50000_OBSERVATIONS_REQUIRED")
            observations = [ShadowObservation.from_dict(json.loads(line)) for line in lines]
            inserted = sum(store.append(observation) for observation in observations)
            return {"received": len(observations), "inserted": inserted,
                    "duplicates": len(observations) - inserted,
                    "retained_head_hash": store.verify()}
        if args.command == "collect":
            return collect_public_feed(products=products_list(args.products), seconds=args.seconds,
                                       output_dir=args.output_dir, store=store)
        as_of = args.as_of or datetime.now(UTC)
        results = []
        for case in store.cases():
            if case.decision_at > as_of:
                continue
            if case.cohort == "PROSPECTIVE":
                frozen = store.conn.execute(
                    "SELECT recorded_at FROM shadow_events WHERE event_id=?",
                    ("case:" + case.case_id,),
                ).fetchone()
                if timestamp(frozen["recorded_at"]) > as_of:
                    continue
            results.append(store.evaluate(case.case_id, as_of=as_of))
        # Avoid turning an interrupted or gapped public session into performance proof.
        return {"as_of": as_of.isoformat(), "retained_head_hash": store.verify(),
                "metrics": export_metrics(results), "cases": [r.to_dict() for r in results],
                "public_feed_sessions": feed_sessions(store), "authorizes_orders": False}


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        result = run(arguments)
        text = json.dumps(result, indent=2) + "\n"
        output = getattr(arguments, "output", None)
        if output:
            with output.open("x") as handle:
                handle.write(text)
        print(text, end="")
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": type(error).__name__, "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
