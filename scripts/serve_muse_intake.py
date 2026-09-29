"""Local external-Muse intake; uses an existing worker, starts no account executor."""

import argparse
import asyncio
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from catalyst_lab import localdb
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.position_news import PositionNewsService
from catalyst_lab.research_cycle import CyclePolicy, ResearchIntake


def build_intake(root, worker_port, cycle_policy, report_seconds):
    if type(worker_port) is not int or not 1024 <= worker_port <= 65535:
        raise ValueError("VALID_LOCAL_WORKER_PORT_REQUIRED")
    if type(report_seconds) is not int or not 1 <= report_seconds <= 86400:
        raise ValueError("EXPLICIT_REPORT_DEADLINE_REQUIRED")
    token_path = root / "api-token"
    if token_path.is_symlink() or stat.S_IMODE(token_path.stat().st_mode) != 0o600:
        raise ValueError("PRIVATE_TOKEN_FILE_REQUIRED")
    token = token_path.read_text().strip()
    repo = RiskRepository(localdb.connection_url(root, "catalyst_risk"))
    repo.check_role()

    def now():
        return datetime.now(UTC)

    intake = ResearchIntake(repo, cycle_policy, clock=now)

    def status():
        try:
            response = httpx.get(
                f"http://127.0.0.1:{worker_port}/api/v1/lab/status",
                headers={"Authorization": "Bearer " + token}, timeout=2,
                follow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
            with repo.connect() as conn:
                row = conn.execute("""SELECT recorded_at FROM lab.managed_events
                    WHERE kind='BROKER_RECONCILIATION' AND body->>'clean'='true'
                    ORDER BY event_seq DESC LIMIT 1""").fetchone()
            body["last_reconciliation_at"] = row["recorded_at"] if row else None
            return body
        except Exception:
            return {"worker_state": "UNAVAILABLE", "entry_ready": False,
                    "management_review_enabled": False,
                    "error_code": "WORKER_STATUS_UNAVAILABLE"}

    return create_managed_app(
        intake, intake.store, api_token=token, runtime_status=status,
        report_submit=lambda raw: asyncio.to_thread(
            intake.start_report, raw, max_seconds=report_seconds
        ),
        position_news=PositionNewsService(intake.store, clock=now),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--worker-port", type=int, required=True)
    parser.add_argument("--report-max-seconds", type=int, required=True)
    parser.add_argument("--cycle-policy", required=True,
                        help="Explicit non-secret CyclePolicy JSON")
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535 or args.port == args.worker_port:
            raise ValueError
        app = build_intake(
            args.root.expanduser().resolve(), args.worker_port,
            CyclePolicy(**json.loads(args.cycle_policy)), args.report_max_seconds,
        )
    except Exception:
        raise SystemExit("MUSE_INTAKE_STARTUP_FAILED") from None
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
