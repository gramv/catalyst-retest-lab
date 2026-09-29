import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from scripts.serve_managed_dashboard import ReadOnlyRepository, build_dashboard


def test_real_ledger_dashboard_is_authenticated_and_read_only(cluster):
    token = "fixture-dashboard-access-not-a-provider-credential"
    path = cluster / "api-token"
    path.write_text(token)
    os.chmod(path, 0o600)
    client = TestClient(build_dashboard(cluster, 8799))
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/lab/cycles").status_code == 401
    assert client.get("/api/v1/lab/cycles", headers={
        "Authorization": "Bearer " + token,
    }).status_code == 200
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/api/v1/lab/research-scans").status_code == 405
    repo = ReadOnlyRepository(localdb.connection_url(cluster))
    with repo.connect() as conn:
        setting = conn.execute("SHOW transaction_read_only").fetchone()
        assert setting["transaction_read_only"] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TEMP TABLE prohibited_dashboard_write(n int)")


def test_dashboard_refuses_nonprivate_access_token(cluster):
    path = cluster / "api-token"
    path.write_text("fixture-no-provider-credential")
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="PRIVATE_TOKEN_FILE_REQUIRED"):
        build_dashboard(cluster, 8799)
