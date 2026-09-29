"""The results dashboard's new routes and page: auth, shape, and no leaked secrets."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.pick_outcomes import (
    PickRecord,
    parse_bars,
    record_shadow_outcome,
    simulate_pick,
)
from catalyst_lab.research_selection_topk import RANKED
from tests.test_managed_service import TOKEN, FixtureCycle, auth, surface  # noqa: F401

GEN = datetime(2026, 1, 1, tzinfo=UTC)
EXPIRES = GEN + timedelta(hours=1)


def a_recorded_pick(store, *, symbol="BTC/USD", engineering=False, selected=True,
                    agent_id="claude"):
    # ``surface`` shares its (session-scoped) cluster with every other test that requests it,
    # so a caller that must isolate its own rows from another test's should pass a fresh,
    # unique ``symbol`` and/or ``agent_id`` (e.g. ``uuid4().hex``) rather than assume an empty
    # ledger.
    levels = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
    pick = PickRecord(
        cycle_id=str(uuid4()), run_slot=GEN.isoformat(),
        item_key="CRYPTO:" + symbol + ":" + uuid4().hex[:8], revision=1,
        signal_id="sig-" + symbol, symbol=symbol, kind="CHART", levels=levels,
        agent_current_price="100", agent_price_at=GEN.isoformat(), generated_at=GEN,
        window_end=EXPIRES, agent_id=agent_id, agent_version="v1",
        attribution="AGENT_ATTRIBUTED", selection_policy="JEV_TOP_K_SELECTION_V1",
        question_set_version="CHART_PICK_QUESTIONS_V1", selected=selected,
        selection_status="SELECTED" if selected else None, decline_code=None,
        replacement_for=None, jev_rank=1 if selected else None, agent_rank=1,
        ranking_status=RANKED, ranking_reasons=[], review_disposition=None, review_reason=None,
        skip_reason=None, setup_id=None, arm="JEV_MANAGED", engineering=engineering,
    )
    never_touch = [{"t": (GEN + timedelta(minutes=m)).isoformat(), "o": "500", "h": "501",
                    "l": "499", "c": "500", "v": "1"} for m in range(0, 60, 10)]
    simulation = simulate_pick(pick.levels, parse_bars(never_touch),
                               window_start=GEN, window_end=EXPIRES)
    with store.transaction() as conn:
        event = record_shadow_outcome(store, conn, pick, simulation)
    return pick, event


@pytest.mark.parametrize("path", [
    "results/picks", "results/picks/aggregates", f"cycles/{uuid4()}/picks",
])
def test_new_routes_require_bearer_auth_and_never_leak_the_token(surface, path):  # noqa: F811
    client, _, _ = surface
    assert client.get("/api/v1/lab/" + path).status_code == 401
    assert client.get("/api/v1/lab/" + path,
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    reply = client.get("/api/v1/lab/" + path, headers=auth())
    assert reply.status_code == 200
    assert TOKEN not in reply.text


def test_cycle_picks_route_is_empty_for_an_unknown_cycle(surface):  # noqa: F811
    client, _, _ = surface
    reply = client.get(f"/api/v1/lab/cycles/{uuid4()}/picks", headers=auth())
    assert reply.status_code == 200
    assert reply.json()["items"] == []


def test_pick_outcomes_route_lists_a_recorded_shadow_outcome(surface):  # noqa: F811
    client, _, store = surface
    symbol = "BTC" + uuid4().hex[:6].upper() + "/USD"  # Unique: the cluster is session-shared.
    pick, event = a_recorded_pick(store, symbol=symbol)
    reply = client.get("/api/v1/lab/results/picks?limit=500", headers=auth())
    assert reply.status_code == 200
    body = reply.json()
    matches = [item for item in body["items"] if item["pick"]["symbol"] == symbol]
    assert len(matches) == 1
    item = matches[0]
    assert item["shadow"]["outcome"] == "NEVER_TRIGGERED_VALIDITY_EXPIRED"
    assert item["real"] is None  # Never traded.
    assert item["event_seq"] == event["event_seq"]


def test_pick_outcome_aggregates_route_respects_group_by_and_excludes_engineering(
    surface,  # noqa: F811
):
    client, _, store = surface
    agent_id = "test-agent-" + uuid4().hex[:8]  # Unique: the cluster is session-shared.
    a_recorded_pick(store, symbol="BTC/USD", agent_id=agent_id)
    a_recorded_pick(store, symbol="ETH/USD", agent_id=agent_id, engineering=True)
    reply = client.get(
        "/api/v1/lab/results/picks/aggregates?group_by=agent_id,pick_kind", headers=auth()
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["group_by"] == ["agent_id", "pick_kind"]
    matches = [item for item in body["items"] if item["agent_id"] == agent_id]
    assert len(matches) == 1  # The engineering-only pick never enters a group of its own.
    assert matches[0]["pick_kind"] == "CHART"
    assert matches[0]["count"] == 1


def test_pick_outcome_aggregates_route_rejects_an_unknown_dimension(surface):  # noqa: F811
    client, _, _ = surface
    reply = client.get(
        "/api/v1/lab/results/picks/aggregates?group_by=not_a_real_dimension", headers=auth()
    )
    assert reply.status_code == 422


def test_results_page_is_a_static_shell_without_secrets(surface):  # noqa: F811
    client, _, _ = surface
    page = client.get("/results")
    assert page.status_code == 200
    assert "PAPER TRADING — SIMULATED. Not real money." in page.text
    assert TOKEN not in page.text
    assert "1-minute-bar" in page.text
    assert 'href="/"' in page.text  # Links back to the main dashboard.
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    script = client.get("/results.js").text
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script
    assert "results/picks/aggregates" in script
    assert "/picks'" in script  # cycles/<id>/picks: today's rank/status/outcome


def test_results_route_is_never_reachable_by_a_research_agent_token(surface):  # noqa: F811
    _, _, store = surface
    cycle = FixtureCycle(store.repo, store)
    agent_token = "fixture-private-agent-claude-token-never-used-outside-tests"
    app = create_managed_app(
        cycle, store, api_token=TOKEN, runtime_status=lambda: {},
        status_token="fixture-private-status-token-never-used-outside-tests",
        operator_token="fixture-private-operator-token-never-used-outside-tests",
        agent_tokens={"claude": agent_token},
    )
    client = TestClient(app)
    agent_auth = {"Authorization": "Bearer " + agent_token}
    assert client.get("/api/v1/lab/results/picks", headers=agent_auth).status_code == 403
