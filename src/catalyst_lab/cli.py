import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.repository import Repository


def local_settings(root: Path):
    token = root / "muse-token"
    if not token.exists():
        with open(token, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            f.write(secrets.token_urlsafe(36))
    os.environ.setdefault("DATABASE_URL", localdb.connection_url(root))
    os.environ.setdefault("MUSE_API_TOKEN", token.read_text().strip())


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "history-test":
        # HISTORY_TEST_V1 (package strategy-c2): offline, keyless public bars, no ledger, no
        # broker; its own options (``catalyst-lab history-test --help``).
        from catalyst_lab.history_test import main as history_main

        history_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "stack":
        # LOCAL_STACK_V1 (package oss-packaging): the local Docker stack's commands (scorecard,
        # promote-strategy, ...); ``catalyst-lab stack --help``.
        from catalyst_lab.local_stack import main as stack_main

        stack_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "new-strategy":
        # STRATEGY_SDK_V1 (package plugin-c3): a plug-in skeleton; writes one file, nothing else.
        from catalyst_lab.strategies.template import main as template_main

        template_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Catalyst Retest Lab — paper laboratory")
    parser.add_argument(
        "command",
        choices=[
            "dev-init",
            "dev-stop",
            "ledger-migrate",
            "ledger-retire",
            "serve",
            "export",
            "verify",
            "risk-import",
            "engineering-acceptance",
            "measure",
            "dashboard",
            "research-import",
            "history-test",
            "new-strategy",
            "stack",
        ],
    )
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    # Development default only; owner ledgers are always named explicitly.
    parser.add_argument("--local-dir", type=Path, default=data_home / "catalyst-retest-lab/dev")
    parser.add_argument("--expect-current", type=int)
    parser.add_argument("--target", type=int)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--expected-head")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--correction-of")
    # ledger-retire only: the owner names the ledger explicitly (no development default).
    parser.add_argument("--ledger-dir", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--app-port", type=int, default=localdb.MAC_APP_PORT,
                        help="ledger-retire: the Mac app's MANAGED_HTTP_PORT (default 8780)")
    args = parser.parse_args()
    if args.command == "ledger-retire":
        if args.ledger_dir is None or args.reason is None:
            parser.error("ledger-retire requires --ledger-dir and --reason (e.g. MOVED_TO_CLOUD)")
        try:
            # Proves the Mac executor is stopped first (package cloud-hardening).
            result = localdb.retire_stopped_ledger(args.ledger_dir.resolve(), args.reason,
                                                   app_port=args.app_port)
        except RuntimeError as exc:
            raise SystemExit(f"ledger-retire refused: {exc}") from None
        print(json.dumps(result, sort_keys=True))
        return
    root = args.local_dir.resolve()
    if args.command == "dev-init":
        localdb.start(root)
        local_settings(root)
        Repository(os.environ["DATABASE_URL"]).check_role()
        print("Private local database ready; restricted application role verified.")
        print(
            f"Token saved at {root / 'muse-token'}; "
            "database initialization does not connect to Alpaca."
        )
    elif args.command == "dev-stop":
        localdb.stop(root)
    elif args.command == "ledger-migrate":
        if None in (args.expect_current, args.target, args.backup_manifest):
            parser.error("ledger-migrate requires --expect-current, --target and --backup-manifest")
        try:
            result = localdb.migrate_ledger(
                root,
                expect_current=args.expect_current,
                target=args.target,
                backup_manifest=args.backup_manifest,
            )
        except RuntimeError as exc:
            raise SystemExit(f"ledger-migrate refused: {exc}") from None
        print(json.dumps(result))
    elif args.command == "research-import":
        from catalyst_lab.research import import_research

        if args.file is None:
            parser.error("--file is required for an operator-owned research record")
        if not os.environ.get("DATABASE_URL"):
            local_settings(root)
        repository = Repository(os.environ["DATABASE_URL"])
        repository.check_role()
        print(
            json.dumps(
                import_research(
                    repository, json.loads(args.file.read_text()), correction_of=args.correction_of
                )
            )
        )
    elif args.command == "dashboard":
        import uvicorn

        from catalyst_lab.dashboard import create_dashboard_app

        reporting_url = os.environ.get("REPORTING_DATABASE_URL") or localdb.connection_url(
            root, "catalyst_reporting"
        )
        uvicorn.run(
            create_dashboard_app(reporting_url), host=args.host, port=args.port, access_log=False
        )
    elif args.command == "measure":
        from catalyst_lab.measurement import Measurements, MeasurementWorker

        if not os.environ.get("DATABASE_URL"):
            local_settings(root)
        repository = Repository(os.environ["DATABASE_URL"])
        repository.check_role()
        MeasurementWorker(Measurements(repository)).tick()
        print(json.dumps({"measurement_refresh": "complete", "broker_requests": 0}))
    elif args.command == "serve":
        import uvicorn

        if not os.environ.get("DATABASE_URL"):
            if not (root / "muse-token").exists():
                parser.error("Run dev-init first or set DATABASE_URL and MUSE_API_TOKEN")
            local_settings(root)
        uvicorn.run(
            "catalyst_lab.api:create_app",
            factory=True,
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
    elif args.command == "export":
        if not os.environ.get("DATABASE_URL"):
            local_settings(root)
        repo = Repository(os.environ["DATABASE_URL"])
        repo.check_role()
        rows = repo.export_events()
        manifest = verify_events(rows)
        path = args.file or Path("exports") / (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        with path.with_suffix(".manifest.json").open("x") as f:
            json.dump(manifest, f, indent=2)
        print(json.dumps({"file": str(path), **manifest}))
    elif args.command == "verify":
        if args.file is None:
            parser.error("--file is required")
        with args.file.open() as f:
            print(json.dumps(verify_events((json.loads(line) for line in f), args.expected_head)))
    elif args.command == "risk-import":
        from catalyst_lab.authorization import RiskRepository
        from catalyst_lab.risk import import_classifications

        if args.file is None:
            parser.error("--file is required for server-owned classifications")
        risk_repo = RiskRepository(
            os.environ.get("RISK_DATABASE_URL") or localdb.connection_url(root, "catalyst_risk")
        )
        count = import_classifications(risk_repo, json.loads(args.file.read_text()))
        print(json.dumps({"classifications_imported": count, "broker_requests": 0}))
    elif args.command == "engineering-acceptance":
        import httpx

        from catalyst_lab.alpaca import AlpacaCredentials, AlpacaPaperClient
        from catalyst_lab.authorization import RiskRepository
        from catalyst_lab.engineering import EngineeringAcceptance
        from catalyst_lab.risk import RiskEngine, RiskPolicy

        if args.file is None:
            parser.error("--file must contain one TEST- engineering candidate")
        risk_repo = RiskRepository(
            os.environ.get("RISK_DATABASE_URL") or localdb.connection_url(root, "catalyst_risk")
        )
        local_settings(root)
        risk_repo.require_same_database(Repository(os.environ["DATABASE_URL"]))

        def ready():
            with httpx.Client(trust_env=False, timeout=5) as observer:
                health = observer.get(f"http://127.0.0.1:{args.port}/health").json()
            market = health.get("market_data", {})
            return (
                health.get("submission_mode") == "RISK_DECISION_REQUIRED"
                and market.get("stream_authenticated")
                and not market.get("error")
                and market.get("broker_monitor", {}).get("watch_permitted")
            )

        client = AlpacaPaperClient(
            AlpacaCredentials.from_env(), os.environ.get("ALPACA_DATA_FEED", "iex")
        )
        try:
            engine = RiskEngine(risk_repo, client, RiskPolicy(), ready=ready)
            result = EngineeringAcceptance(engine).enroll(
                json.loads(args.file.read_text()), timeout_seconds=args.timeout_seconds
            )
            print(
                json.dumps(
                    {
                        "candidate_id": result["candidate_id"],
                        "state": result["state"],
                        "record_purpose": result["record_purpose"],
                        "worker_handles_trigger_and_risk": True,
                    }
                )
            )
        finally:
            client.close()


if __name__ == "__main__":
    main()
