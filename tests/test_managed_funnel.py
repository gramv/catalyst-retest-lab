import asyncio

from fastapi.testclient import TestClient

from catalyst_lab.managed_funnel import execution_quality, research_funnel
from catalyst_lab.managed_service import create_managed_app
from tests.test_managed_analytics import close
from tests.test_managed_execution import er as er
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import pristine_cluster as pristine_cluster
from tests.test_managed_service import TOKEN, FixtureCycle, auth
from tests.test_research_cycle import begin
from tests.test_research_cycle import runtime as runtime


def test_research_conversion_counts_exact_latest_revisions_with_cursor(runtime):
    cycle, _, _, _ = runtime
    with cycle.repo.connect() as conn:
        before = conn.execute(
            "SELECT coalesce(max(event_seq),0) AS seq FROM lab.managed_events"
        ).fetchone()["seq"]
    cid, _, _ = begin(runtime, 2)
    asyncio.run(cycle.tick(cid))
    cycle.approved_packets(cid)
    page = research_funnel(cycle.repo, after=before, limit=1)
    row = page["items"][0]
    assert row["cycle_id"] == cid and row["contender_count"] == row["selected_count"] == 2
    assert row["latest_revision_outcomes"] == {"APPROVED": 2}
    assert row["review_latency"]["receipt_count"] == 2
    assert row["admitted_count"] == row["entered_count"] == 0
    assert research_funnel(cycle.repo, after=page["next_cursor"])["items"] == []


def test_complete_private_history_and_execution_quality(mx):
    engine, _, _ = mx
    sid = close(mx)
    quality = execution_quality(engine.repo, sid)
    assert quality["entry_fill_average"] and quality["slippage_vs_planned_trigger_bps"] is not None
    client = TestClient(create_managed_app(FixtureCycle(engine.repo, engine.store), engine.store,
                                         api_token=TOKEN, runtime_status=lambda: {}))
    for route in ("analytics/research", "analytics/daily", "history/results",
                  f"positions/{sid}/timeline"):
        assert client.get("/api/v1/lab/" + route).status_code == 401
        assert client.get("/api/v1/lab/" + route, headers=auth()).status_code == 200
    first = client.get(f"/api/v1/lab/positions/{sid}/timeline?limit=1", headers=auth()).json()
    path = f"/api/v1/lab/positions/{sid}/timeline?after={first['next_cursor']}&limit=1"
    second = client.get(path, headers=auth()).json()
    assert second["items"][0]["event_seq"] > first["items"][0]["event_seq"]
    results = client.get("/api/v1/lab/results?limit=1", headers=auth()).json()
    assert results["items"][0]["setup_id"] == str(sid)
    assert client.get(f"/api/v1/lab/results?before={results['next_cursor']}",
                      headers=auth()).json()["items"] == []
