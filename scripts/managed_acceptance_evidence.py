"""Read-only acceptance evidence for one managed paper trade (plan 0.10, gate G3).

Collects and hashes the evidence of one managed setup from the append-only ledger, so the first
supervised managed trade is proved from the audit trail instead of hand-collected. Connects to
the database as a read-only role only (``catalyst_review`` or ``catalyst_reporting`` — this tool
refuses ``catalyst_app``, ``catalyst_risk`` and ``lab_owner`` outright) and issues only ``SELECT``
statements; see ``catalyst_lab.acceptance_evidence`` for exactly which tables that role can and
cannot read, and what happens to a section it cannot read (reported unavailable, its checks fail
closed — never silently skipped). This script never contacts a broker or provider and applies no
migration.

Usage:
    python scripts/managed_acceptance_evidence.py --database-url URL \\
        (--setup-id ID | --latest-closed) --output DIR \\
        [--audit-export FILE.jsonl] [--broker-snapshot FILE.json]

``--audit-export FILE`` is an owner-exported ``lab.trade_events`` file from the existing
``catalyst-lab export`` command (run separately, under ``catalyst_app``, by the owner — this
script never runs it and never needs that role); when supplied, it is the chain that is
verified. Without it, the collector verifies ``lab.trade_events`` read directly, which
``catalyst_review`` may read from migration 019; on an older ledger the role cannot, and the
audit section reports the observed event-sequence range only, with the chain-head fields as
explicit unknowns.

``--broker-snapshot FILE`` is an owner-captured, read-only JSON snapshot of broker positions and
open orders, taken with the existing GET-only ``AlpacaReadOnly`` client. This script never
contacts the broker itself. To capture one:

    python - <<'PY'
    import json
    from catalyst_lab.alpaca import AlpacaCredentials, AlpacaReadOnly
    from datetime import UTC, datetime
    client = AlpacaReadOnly(AlpacaCredentials.from_env())
    try:
        snapshot = {
            "captured_at": datetime.now(UTC).isoformat(),
            "positions": client.positions(),
            "open_orders": client.open_orders(),
        }
    finally:
        client.close()
    with open("broker-snapshot.json", "x") as f:
        json.dump(snapshot, f, indent=2, default=str)
    PY

That snapshot holds symbols, quantities and prices only — never a credential or account
identifier (``AlpacaReadOnly`` never reads or logs those) — but write it to a private,
owner-controlled path; this script never prints its contents, only counts.

Exit codes: 0 = collected and every check passed; 2 = collected but at least one check failed
(evidence is still written — a failed or incomplete check is itself the record); 1 = refused to
run or refused to write (wrong role, setup not found, or a credential-shaped string was found in
the assembled evidence, which never happens against real data and would itself be a bug worth
reporting rather than papering over).
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from catalyst_lab.acceptance_evidence import (
    RoleRefused,
    SetupNotFound,
    canonical_json,
    collect_acceptance_evidence,
    rows_sha256,
    scan_for_credential_shapes,
)


def _load_jsonl(path: Path):
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def _private_directory(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"Refusing to reuse an existing output directory: {path}")
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    with open(path, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        f.write(data)


def _print_summary(manifest: dict) -> None:
    print(f"setup_id       {manifest['setup_id']}")
    print(f"market/symbol  {manifest['market']} {manifest['symbol']}")
    print(f"cohort/policy  {manifest['cohort']} {manifest['policy_id']}")
    print("sections:")
    for name, section in sorted(manifest["sections"].items()):
        mark = "present" if section["present"] else "ABSENT"
        reason = "" if section["present"] else f" ({section['summary'].get('reason')})"
        print(f"  {name:<20} {mark}{reason}")
    print("checks:")
    for check in manifest["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['name']}: {check['detail']}")
    if manifest["unknowns"]:
        print("unknowns:")
        for name, reason in sorted(manifest["unknowns"].items()):
            print(f"  {name}: {reason}")
    audit = manifest["audit"]
    chain = audit["chain"]
    print("audit:")
    print(f"  admission_event_seq   {audit['admission_event_seq']}")
    print(f"  evidence_event_seq    {audit['evidence_event_seq_range']}")
    if chain is None:
        print("  chain                 NOT VERIFIED (role cannot read lab.trade_events and no "
              "--audit-export supplied)")
    else:
        print(f"  chain_source          {audit['source']}")
        print(f"  chain_valid           {chain['valid']}")
        print(f"  head_before           {chain['head_before']}")
        print(f"  head_after            {chain['head_after']}")
        if chain["error"]:
            print(f"  chain_error           {chain['error']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True, help="catalyst_review/reporting DSN")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--setup-id", help="lab.managed_setups.setup_id (UUID)")
    target.add_argument("--latest-closed", action="store_true", help="most recently closed setup")
    parser.add_argument("--output", required=True, type=Path, help="new mode-0700 output directory")
    parser.add_argument("--audit-export", type=Path, help="owner-exported lab.trade_events JSONL")
    parser.add_argument(
        "--broker-snapshot", type=Path, help="owner-captured broker positions/orders JSON"
    )
    args = parser.parse_args(argv)

    if args.output.exists():
        raise SystemExit(f"Refusing to reuse an existing output directory: {args.output}")
    audit_export_rows = _load_jsonl(args.audit_export) if args.audit_export else None
    broker_snapshot = _load_json(args.broker_snapshot) if args.broker_snapshot else None

    try:
        with psycopg.connect(
            args.database_url, row_factory=dict_row, options="-c timezone=UTC", connect_timeout=5
        ) as conn:
            manifest = collect_acceptance_evidence(
                conn,
                setup_id=args.setup_id,
                latest_closed=args.latest_closed,
                audit_export_rows=audit_export_rows,
                broker_snapshot=broker_snapshot,
            )
    except (RoleRefused, SetupNotFound) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    findings = scan_for_credential_shapes(manifest)
    if findings:
        print(
            "REFUSED: credential-shaped content found in assembled evidence "
            f"({', '.join(findings)}); nothing was written.",
            file=sys.stderr,
        )
        return 1

    _private_directory(args.output)
    body = canonical_json(manifest).encode()
    _write_private(args.output / "evidence.json", body)
    _write_private(args.output / "evidence.sha256", (rows_sha256(manifest) + "\n").encode())

    _print_summary(manifest)
    return 0 if all(check["passed"] for check in manifest["checks"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
