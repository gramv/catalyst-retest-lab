"""Reproducible FIXTURE proof; every provider is mocked, never owner broker credentials.

Optional local read-only dashboard serves the completed fixture ledger. This script
imports test helpers deliberately and cannot be mistaken for the real worker launcher.
"""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from catalyst_lab import localdb
from catalyst_lab.acceptance_evidence import (
    canonical_json,
    collect_acceptance_evidence,
    rows_sha256,
)
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from tests.test_complete_managed_cycle import run_complete_cycle
from tests.test_managed_execution import mx
from tests.test_position_monitor import monitor

FIXTURE_TOKEN = "fixture-proof-local-only-not-a-provider-secret"


def _write_evidence(root, events, lifecycles, destination):
    """Plan 0.10 rehearsal: run the read-only acceptance-evidence collector, over a genuine
    catalyst_review connection, against every closed lifecycle this fixture proof produced (one
    US_STOCKS, one CRYPTO by default) — never the catalyst_risk connection the proof itself used.
    """
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    url = localdb.connection_url(root, "catalyst_review")
    manifests = {}
    with psycopg.connect(url, row_factory=dict_row) as conn:
        for lifecycle in lifecycles:
            setup_id = str(lifecycle["setup_id"])
            manifest = collect_acceptance_evidence(
                conn, setup_id=setup_id, audit_export_rows=events
            )
            setup_dir = destination / setup_id
            setup_dir.mkdir(mode=0o700)
            os.chmod(setup_dir, 0o700)
            body = canonical_json(manifest).encode()
            for name, content in (
                ("evidence.json", body),
                ("evidence.sha256", (rows_sha256(manifest) + "\n").encode()),
            ):
                path = setup_dir / name
                with open(path, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
                    f.write(content)
            manifests[setup_id] = {
                "market": manifest["market"],
                "all_checks_passed": all(c["passed"] for c in manifest["checks"]),
                "unknowns": sorted(manifest["unknowns"]),
            }
    return manifests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve-port", type=int)
    parser.add_argument(
        "--evidence",
        type=Path,
        help="also write plan 0.10 acceptance-evidence manifests here, one per closed lifecycle",
    )
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if root.exists() or not root.name.startswith("managed-proof-"):
        raise SystemExit("A new isolated managed-proof-* directory is required")
    args.output.mkdir(parents=True, exist_ok=True)
    localdb.start(root)
    generator = mx.__wrapped__(Repository(localdb.connection_url(root)))
    try:
        components = next(generator)
        proof = run_complete_cycle(components)
        engine, venue, reviews = components
        events = engine.repo.export_events()
        proof.update(
            {
                "proof_mode": "FIXTURE_PROVIDERS_REAL_POSTGRES",
                "real_alpaca_orders": 0,
                "real_jev_calls": 0,
                "created_at": datetime.now(UTC).isoformat(),
                "audit": verify_events(events),
            }
        )
        for name, body in (("proof.json", proof), ("events.json", events)):
            destination = args.output / name
            destination.write_text(json.dumps(json_safe(body), indent=2) + "\n")
            os.chmod(destination, 0o600)
        print(json.dumps(json_safe(proof), indent=2))
        if args.evidence:
            manifests = _write_evidence(root, events, proof["lifecycles"], args.evidence)
            print(json.dumps({"acceptance_evidence": manifests}, indent=2))
        if args.serve_port:
            import uvicorn

            reviewer = monitor(components)[0].reviewer
            cycle = ResearchCycle(
                engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 5), clock=lambda: venue.now
            )
            app = create_managed_app(
                cycle,
                engine.store,
                api_token=FIXTURE_TOKEN,
                runtime_status=lambda: {
                    "worker_state": "STOPPED_AFTER_FIXTURE_PROOF",
                    "mode": "FIXTURE_PROVIDERS_REAL_POSTGRES",
                    "management_review_enabled": False,
                    "trade_stream_connected": False,
                },
            )
            uvicorn.run(app, host="127.0.0.1", port=args.serve_port, log_level="warning")
    finally:
        generator.close()
        localdb.stop(root)


if __name__ == "__main__":
    main()
