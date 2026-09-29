"""Read-only local view of a real managed ledger; starts no trading worker."""

import argparse
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi.responses import JSONResponse

from catalyst_lab import localdb
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.public_crypto_bars import PublicCryptoBarReader
from catalyst_lab.repository import Repository


class ReadOnlyRepository(Repository):
    def connect(self):
        conn = super().connect()
        conn.read_only = True
        return conn

    def require_same_database(self, other):
        if other is not self:
            raise RuntimeError("Dashboard read models must share one repository")


def build_dashboard(root, worker_port):
    token_path = root / "api-token"
    if token_path.is_symlink() or stat.S_IMODE(token_path.stat().st_mode) != 0o600:
        raise ValueError("PRIVATE_TOKEN_FILE_REQUIRED")
    token = token_path.read_text().strip()
    repo = ReadOnlyRepository(localdb.connection_url(root))
    store = ManagedStore(repo)

    def outputs(cycle_id, *, after, limit):
        with repo.connect() as conn:
            return conn.execute(
                """SELECT event_seq,setup_id,kind,body,recorded_at FROM lab.managed_events
                WHERE body->>'cycle_id'=%s AND event_seq>%s ORDER BY event_seq LIMIT %s""",
                (cycle_id, after, limit),
            ).fetchall()

    def runtime_status():
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

    # Keyless, GET-only public crypto bars (public_crypto_bars.py); never the owner's broker
    # credential, and read-only regardless (this dashboard's own middleware refuses non-GET).
    app = create_managed_app(
        SimpleNamespace(repo=repo, outputs=outputs), store,
        api_token=token, runtime_status=runtime_status, bar_reader=PublicCryptoBarReader(),
    )

    @app.middleware("http")
    async def reads_only(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "READ_ONLY_DASHBOARD"}, status_code=405)
        return await call_next(request)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--worker-port", type=int, required=True)
    args = parser.parse_args()
    if any(not 1024 <= p <= 65535 for p in (args.port, args.worker_port)):
        raise SystemExit("VALID_LOCAL_PORTS_REQUIRED")
    uvicorn.run(build_dashboard(args.root.expanduser().resolve(), args.worker_port),
                host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
