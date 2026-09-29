import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import digest
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.position_news import PositionNewsService
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_position_monitor import bars, monitor, opened

TOKEN = "fixture-position-news-token-only-for-disposable-tests"


def packet(service, sid, now, *, text="Issuer discloses material adverse product evidence."):
    context = service.context(sid)
    return {
        "news_id": str(uuid4()),
        "lifecycle_id": context["lifecycle_id"],
        "expected_news_revision": context["news_revision"],
        "sources": [
            {
                "source_id": "issuer-new",
                "url": "https://issuer.example/new-disclosure",
                "excerpt": text,
                "published_at": now.isoformat(),
                "retrieved_at": now.isoformat(),
                "content_hash": digest(text),
                "primary_source": True,
                "asset_relevant": True,
                "novelty": "NEW_FACT",
                "stance": "ADVERSE",
            }
        ],
    }


def client_for(engine, service):
    app = create_managed_app(
        SimpleNamespace(repo=engine.repo),
        engine.store,
        api_token=TOKEN,
        runtime_status=lambda: {},
        position_news=service,
    )
    return TestClient(app)


def auth():
    return {"Authorization": "Bearer " + TOKEN}


def test_post_entry_news_after_research_expiry_keeps_original_thesis_and_exit(mx):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    original_setup, original_state = engine._load(sid)
    venue.now = original_setup["expires_at"] + timedelta(seconds=1)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    raw = packet(service, sid, venue.now)
    client = client_for(engine, service)
    path = f"/api/v1/lab/positions/{sid}/news"
    assert client.get(path).status_code == 401
    result = client.post(path, headers=auth(), json=raw)
    assert result.status_code == 200
    assert result.json()["position_modified"] is False
    setup, state = engine._load(sid)
    assert setup == original_setup and state == original_state
    context = client.get(path, headers=auth()).json()
    assert context["sources"][0]["content_hash"] == raw["sources"][0]["content_hash"]
    assert context["original_thesis"] == original_setup["record_json"]["thesis"]
    assert context["hard_exit_at"] == original_state["hard_exit_at"]
    watcher, _ = monitor(mx)
    revision, sources = watcher.current_evidence(setup)
    assert revision == result.json()["news_revision"] and sources[0] == raw["sources"][0]
    assert verify_events(engine.repo.export_events())["valid"]


def test_exact_retry_idempotent_and_material_revision_required(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    raw = packet(service, sid, venue.now)
    first = service.submit(sid, raw)
    replay = service.submit(sid, raw)
    assert replay["idempotent_replay"] and replay["news_revision"] == first["news_revision"]
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONTENT_MISMATCH"):
        service.submit(sid, {**raw, "expected_news_revision": first["news_revision"]})
    venue.now += timedelta(seconds=1)
    with pytest.raises(ValueError, match="MATERIAL_POSITION_NEWS_REQUIRED"):
        service.submit(sid, packet(service, sid, venue.now))
    with engine.repo.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) n FROM lab.managed_events WHERE kind='POSITION_NEWS'"
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("case", ["wrong_lifecycle", "closed", "deadline", "stale_revision"])
def test_late_or_mismatched_position_news_rejected(mx, case):
    engine, venue, _ = mx
    sid = opened(mx)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    raw = packet(service, sid, venue.now)
    if case == "wrong_lifecycle":
        raw["lifecycle_id"] = str(uuid4())
    elif case == "closed":
        with engine.store.transaction() as conn:
            engine.store.transition(conn, sid, "CLOSED", qty="0")
    elif case == "deadline":
        _, state = engine._load(sid)
        venue.now = datetime.fromisoformat(state["hard_exit_at"])
    else:
        raw["expected_news_revision"] += 1
    with pytest.raises(ValueError, match="POSITION_|STALE_POSITION"):
        service.submit(sid, raw)
    with engine.repo.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE kind='POSITION_NEWS'"
        ).fetchone()


@pytest.mark.parametrize(
    "case", ["quantity", "expires_at", "thesis", "bad_hash", "credential", "naive_time"]
)
def test_route_rejects_overrides_and_invalid_private_evidence(mx, case):
    engine, venue, _ = mx
    sid = opened(mx)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    raw = packet(service, sid, venue.now)
    if case in {"quantity", "expires_at", "thesis"}:
        raw[case] = "override"
    elif case == "bad_hash":
        raw["sources"][0]["content_hash"] = "wrong"
    elif case == "credential":
        text = "Authorization: Bearer fixture-secret-must-not-be-persisted"
        raw["sources"][0].update(excerpt=text, content_hash=digest(text))
    else:
        raw["sources"][0]["retrieved_at"] = "2026-09-19T12:00:00"
    response = client_for(engine, service).post(
        f"/api/v1/lab/positions/{sid}/news",
        headers=auth(),
        json=raw,
    )
    assert response.status_code == 422
    assert "fixture-secret" not in response.text
    with engine.repo.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE kind='POSITION_NEWS'"
        ).fetchone()


def test_news_arriving_during_jev_review_invalidates_old_response(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    raw = packet(service, sid, venue.now)
    watcher, calls = monitor(mx, hook=lambda: service.submit(sid, raw))
    observed = observation(mx, bid="108", ask="108.01")
    result = asyncio.run(
        watcher.review(sid, observed, bars(mx), fresh_observation=lambda: observed)
    )
    assert result.receipt_ids and len(calls) == 1
    engine.manage(sid, observed)
    assert not any(method == "PATCH" for method, _, _ in venue.calls)


def test_later_research_source_changes_advance_position_evidence_revision(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    service = PositionNewsService(engine.store, clock=lambda: venue.now)
    first = service.submit(sid, packet(service, sid, venue.now))
    setup, _ = engine._load(sid)
    source = packet(service, sid, venue.now, text="New separate research disclosure.")["sources"]
    with engine.store.transaction() as conn:
        event = engine.store.event(
            conn,
            "RESEARCH_PACKET",
            {
                "cycle_id": str(setup["cycle_id"]),
                "item_key": setup["record_json"]["item_key"],
                "revision": setup["revision"] + 1,
                "state": {"sources": source},
            },
        )
    watcher, _ = monitor(mx)
    revision, sources = watcher.current_evidence(setup)
    assert revision == event["event_seq"] > first["news_revision"]
    assert source[0] in sources
