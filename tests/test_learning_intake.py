"""MARKET_OUTLOOK_V1 and POST_MORTEM_V1 intake (package learning-app).

Fixture evidence only: per-test disposable PostgreSQL databases, a fixture universe and schedule,
the fixture paper venue for closed trades. No broker, provider, network or owner-ledger contact.
"""

import copy
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab import (
    daily_brief,
    learning_replays,
    market_reality,
    scorecard,
    trade_paths,
    weekly_review,
)
from catalyst_lab.learning_intake import (
    LEARNING_EVENT_KINDS,
    OUTLOOK_EVENT,
    POST_MORTEM_EVENT,
    LearningIntake,
    LearningPolicy,
    LearningRecordRejected,
    grading_day,
    identity_paths,
)
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.research_report_v3 import ResearchCapabilityUnavailable
from tests.learning_fixtures import (
    NOW,
    bearer,
    client_for,
    coin,
    intake_for,
    learning,  # noqa: F401 -- fixture
    mover,
    mover_item,
    outlook,
    post_mortem,
    record_reality,
    skipped,
    source,
    trade_item,
)
from tests.test_agent_identity import attributed, fixture_agent
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_analytics import close
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation
from tests.test_managed_service import FixtureCycle
from tests.test_research_report_v3 import AGENTS, LEGACY, OPERATOR, STATUS

OUTLOOKS = "/api/v1/lab/market-outlooks"
POST_MORTEMS = "/api/v1/lab/post-mortems"


def events(store, kind):
    with store.repo.connect() as conn:
        return conn.execute(
            "SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq", (kind,),
        ).fetchall()


# --- MARKET_OUTLOOK_V1 ---------------------------------------------------------------------------


def test_an_agent_records_its_outlook_once_as_an_immutable_event(learning):  # noqa: F811
    raw = outlook()
    reply = learning.client.post(OUTLOOKS, json=raw, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202, reply.text
    receipt = reply.json()
    assert receipt["status"] == "MARKET_OUTLOOK_RECORDED"
    assert receipt["outlook_id"] == raw["outlook_id"] and receipt["agent_id"] == "claude"
    assert (receipt["coin_count"], receipt["skipped_count"], receipt["universe_count"]) == (4, 1, 4)
    assert receipt["received_at"] == receipt["window_start"] == NOW.isoformat()
    assert receipt["window_end"] == (NOW + timedelta(hours=24)).isoformat()
    assert receipt["grading_day"] == "2026-09-27"  # 08:30 ET + 24 h ends on the 27th.
    assert receipt["idempotent_replay"] is False and receipt["trade_authorized"] is False
    [row] = events(learning.store, "MARKET_OUTLOOK")
    assert row["setup_id"] is None and row["event_seq"] == receipt["event_seq"]
    assert row["idempotency_key"] == f"market-outlook:claude:{raw['outlook_id']}"
    body = row["body"]
    assert body["universe"]["symbols"] == ["BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD"]
    factor = body["market"]["factors"][0]["source"]
    assert factor["content_hash"] and factor["retrieved_at"].endswith("+00:00")
    assert [c["direction"] for c in body["coins"]] == ["UP", "FLAT", "DOWN", "SKIPPED"]
    # An identical retry is answered from the ledger, even after the freshness window.
    learning.now[0] = NOW + timedelta(minutes=10)
    again = learning.client.post(OUTLOOKS, json=raw, headers=bearer(AGENTS["claude"]))
    assert again.status_code == 202
    assert again.json() == {**receipt, "idempotent_replay": True}
    changed = copy.deepcopy(raw)
    changed["coins"][0]["confidence"] = "0.9"
    refused = learning.client.post(OUTLOOKS, json=changed, headers=bearer(AGENTS["claude"]))
    assert (refused.status_code, refused.json()) == (
        422, {"detail": "OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH"})
    assert len(events(learning.store, "MARKET_OUTLOOK")) == 1


def test_only_the_agents_own_credential_may_post_its_outlook(learning):  # noqa: F811
    raw = outlook()
    for token, code in ((AGENTS["instinct"], 403), (LEGACY, 403)):
        reply = learning.client.post(OUTLOOKS, json=raw, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (code, {"detail": "AGENT_IDENTITY_MISMATCH"})
    for token in (STATUS, OPERATOR):
        reply = learning.client.post(OUTLOOKS, json=raw, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (403, {"detail": "TOKEN_ROLE_NOT_PERMITTED"})
    assert learning.client.post(OUTLOOKS, json=raw).status_code == 401
    # The legacy credential acts for agent muse.
    muse = outlook(agent_id="muse")
    assert learning.client.post(OUTLOOKS, json=muse, headers=bearer(LEGACY)).status_code == 202
    missing = outlook()
    del missing["agent"]
    reply = learning.client.post(OUTLOOKS, json=missing, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 422
    assert reply.json() == {"detail": "INVALID_MARKET_OUTLOOK",
                            "errors": [{"path": "agent", "code": "AGENT_IDENTITY_REQUIRED"}]}
    assert len(events(learning.store, "MARKET_OUTLOOK")) == 1


def test_results_routes_stay_closed_to_agent_tokens(learning):  # noqa: F811
    for route in ("results", "results/picks", "results/aggregates", "analytics/daily"):
        reply = learning.client.get("/api/v1/lab/" + route, headers=bearer(AGENTS["claude"]))
        assert (reply.status_code, reply.json()) == (403, {"detail": "TOKEN_ROLE_NOT_PERMITTED"})


def refusal(learning, raw, token="claude"):  # noqa: F811
    reply = learning.client.post(OUTLOOKS, json=raw, headers=bearer(AGENTS[token]))
    assert reply.status_code == 422, reply.text
    return reply.json()


@pytest.mark.parametrize(("mutate", "path", "code"), [
    (lambda raw: raw["coins"].__setitem__(3, {"symbol": "DOGE/USD", "direction": "SKIPPED"}),
     "coins[3]", "SKIP_REASON_REQUIRED"),
    (lambda raw: raw["coins"][3].update(confidence="0.5"), "coins[3]",
     "SKIPPED_COIN_FIELDS_NOT_ALLOWED"),
    (lambda raw: raw["coins"][0].update(confidence=None), "coins[0]", "CONFIDENCE_REQUIRED"),
    (lambda raw: raw["coins"][0].update(skip_reason="no"), "coins[0]", "SKIP_REASON_NOT_ALLOWED"),
    (lambda raw: raw["coins"][0].update(confidence="1.2"), "coins[0].confidence",
     "LESS_THAN_EQUAL"),
    (lambda raw: raw.update(horizon_hours=12), "horizon_hours", "LITERAL_ERROR"),
    (lambda raw: raw["coins"][0]["reasons"][0].update(kind="RUMOUR"),
     "coins[0].reasons[0].kind", "LITERAL_ERROR"),
    (lambda raw: raw["market"]["btc"].update(direction="SKIPPED"), "market.btc.direction",
     "LITERAL_ERROR"),
    (lambda raw: raw["coins"].append(coin("BTC/USD")), "coins[4].symbol",
     "DUPLICATE_COIN_IN_OUTLOOK"),
    (lambda raw: raw.update(run_slot="2026-09-26T08:05:00-04:00"), "run_slot",
     "RUN_SLOT_NOT_SCHEDULED"),
    (lambda raw: raw["market"].update(factors=[
        {"name": "x", "note": "y", "source": None}] * 13), "market.factors", "TOO_LONG"),
])
def test_format_refusals_name_the_field_and_store_nothing(learning, mutate, path, code):  # noqa: F811
    raw = outlook()
    mutate(raw)
    body = refusal(learning, raw)
    assert body["detail"] == "INVALID_MARKET_OUTLOOK"
    assert {"path": path, "code": code} in body["errors"], body
    assert events(learning.store, "MARKET_OUTLOOK") == []


def test_the_run_slot_may_not_be_answered_early(learning):  # noqa: F811
    early = NOW - timedelta(hours=2)  # 06:30 ET, 90 minutes before the 08:00 run (grace 60).
    learning.now[0] = early
    raw = outlook(now=early, run_slot="2026-09-26T08:00:00-04:00")
    body = refusal(learning, raw)
    assert {"path": "run_slot", "code": "RUN_SLOT_IN_FUTURE"} in body["errors"]


def test_the_universe_decides_coverage_and_membership(learning):  # noqa: F811
    raw = outlook(coins=[coin("BTC/USD"), coin("ETH/USD"), coin("SOL/USD")])
    body = refusal(learning, raw)
    assert body == {"detail": "INVALID_MARKET_OUTLOOK",
                    "errors": [{"path": "coins", "code": "OUTLOOK_COINS_INCOMPLETE"}]}
    raw = outlook()
    raw["coins"].append(coin("PEPE/USD"))
    body = refusal(learning, raw)
    assert body["errors"] == [{"path": "coins[4].symbol", "code": "COIN_NOT_IN_UNIVERSE"}]
    assert events(learning.store, "MARKET_OUTLOOK") == []


def test_freshness_sources_identity_and_sensitive_content(learning):  # noqa: F811
    stale = outlook(generated_at=(NOW - timedelta(seconds=61)).isoformat())
    assert refusal(learning, stale) == {"detail": "MARKET_OUTLOOK_STALE_OR_FUTURE"}
    future = outlook(generated_at=(NOW + timedelta(seconds=1)).isoformat())
    assert refusal(learning, future) == {"detail": "MARKET_OUTLOOK_STALE_OR_FUTURE"}
    raw = outlook()
    raw["market"]["factors"][0]["source"]["retrieved_at"] = (NOW + timedelta(minutes=1)).isoformat()
    assert refusal(learning, raw)["errors"] == [
        {"path": "market.factors[0].source", "code": "INVALID_SOURCE_EVIDENCE"}]
    raw = outlook()
    raw["market"]["summary"] = "Claude expects a quiet day."
    raw["coins"][3]["skip_reason"] = "CLAUDE had no bars."
    body = refusal(learning, raw)
    assert body["detail"] == "AGENT_IDENTITY_IN_OUTLOOK"
    assert {e["path"] for e in body["errors"]} == {"market.summary", "coins[3].skip_reason"}
    # Excerpts and URLs are third-party text: an agent's name there is not a leak.
    raw = outlook()
    raw["market"]["factors"][0]["source"]["excerpt"] = "Claude, the model, was mentioned."
    assert learning.client.post(OUTLOOKS, json=raw,
                                headers=bearer(AGENTS["claude"])).status_code == 202
    raw = outlook()
    raw["market"]["summary"] = "Write to desk@example.com for the data."
    body = refusal(learning, raw)
    assert body["detail"] == "SENSITIVE_EVIDENCE_REJECTED"
    assert body["errors"] == [{"path": "market.summary", "code": "SENSITIVE_EVIDENCE_REJECTED"}]
    big = learning.client.post(OUTLOOKS, content=b"{" + b" " * 2097152 + b"}",
                               headers=bearer(AGENTS["claude"]))
    assert (big.status_code, big.json()) == (413, {"detail": "MARKET_OUTLOOK_TOO_LARGE"})
    bad = learning.client.post(OUTLOOKS, content=b'{"a": 1, "a": 2}',
                               headers=bearer(AGENTS["claude"]))
    assert (bad.status_code, bad.json()) == (422, {"detail": "INVALID_MARKET_OUTLOOK"})
    assert len(events(learning.store, "MARKET_OUTLOOK")) == 1


def test_missing_capabilities_answer_unavailable(learning):  # noqa: F811
    store = learning.store
    no_schedule = intake_for(store, learning.now, schedule=None)
    with pytest.raises(ResearchCapabilityUnavailable, match="RESEARCH_SCHEDULE_NOT_CONFIGURED"):
        no_schedule.submit_outlook(outlook())

    def unavailable():
        raise ResearchCapabilityUnavailable("RESEARCH_UNIVERSE_UNAVAILABLE")

    blind = LearningIntake(store, clock=lambda: NOW,
                           policy=LearningPolicy(max_age_seconds=60, schedule=learning.intake
                                                 .policy.schedule), universe=unavailable)
    reply = client_for(store, blind).post(OUTLOOKS, json=outlook(),
                                          headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (503, {"detail": "RESEARCH_UNIVERSE_UNAVAILABLE"})
    reply = client_for(store, None).post(OUTLOOKS, json=outlook(),
                                         headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (503, {"detail": "LEARNING_INTAKE_NOT_CONFIGURED"})
    with pytest.raises(ValueError, match="LEARNING_INTAKE_POLICY_INVALID"):
        LearningIntake(store, clock=lambda: NOW, policy=LearningPolicy(max_age_seconds=0))
    assert events(store, "MARKET_OUTLOOK") == []


@pytest.mark.parametrize(("received", "day"), [
    (datetime(2026, 9, 26, 12, 30, tzinfo=UTC), date(2026, 9, 27)),
    (datetime(2026, 9, 26, 4, 0, tzinfo=UTC), date(2026, 9, 26)),  # 00:00 ET: ends at midnight.
    (datetime(2026, 9, 26, 3, 59, tzinfo=UTC), date(2026, 9, 26)),  # 23:59 ET on the 25th.
    (datetime(2026, 11, 1, 12, 0, tzinfo=UTC), date(2026, 11, 2)),  # Across the DST change.
])
def test_the_grading_day_is_the_first_day_end_at_or_after_the_window(received, day):
    assert grading_day(received + timedelta(hours=24)) == day


def test_the_outlook_cap_never_sits_below_the_universe_bound():
    """Every universe coin needs an entry, so the cap must cover the largest universe the
    context can serve: the context has no bound of its own, and a report names at most its
    picks plus its skipped coins. A lower cap would refuse every outlook of a larger universe."""
    from catalyst_lab import learning_intake, research_context
    from catalyst_lab.research_report_v3 import MAX_PICKS, MAX_SKIPPED

    universe_bound = getattr(research_context, "MAX_UNIVERSE_COINS", MAX_PICKS + MAX_SKIPPED)
    assert universe_bound >= MAX_PICKS + MAX_SKIPPED
    assert learning_intake.MAX_OUTLOOK_COINS >= universe_bound
    assert learning_intake.MarketOutlook.model_fields["coins"].metadata[0].max_length == \
        learning_intake.MAX_OUTLOOK_COINS


def test_a_universe_above_one_hundred_coins_is_covered(learning):  # noqa: F811
    universe = tuple(f"C{i:03}/USD" for i in range(150))
    now = [NOW]
    intake = intake_for(learning.store, now, universe=universe)
    raw = outlook(coins=[coin(symbol, "FLAT", "0.5", reasons=[]) for symbol in universe])
    receipt = intake.submit_outlook(raw)
    assert receipt["coin_count"] == 150 and receipt["universe_count"] == 150


def test_identity_paths_read_only_agent_prose():
    data = {"agent": {"agent_id": "claude"}, "summary": "claude", "symbol": "CLAUDE/USD",
            "coins": [{"skip_reason": "Muse-like, not claudes"}],
            "sources": [{"source_id": "claude-1", "excerpt": "claude", "url": "https://claude"}]}
    assert identity_paths(data, "claude") == ["summary", "sources[0].source_id"]


# --- POST_MORTEM_V1 -----------------------------------------------------------------------------


def test_mover_post_mortems_record_accepted_items_and_refuse_the_rest(learning):  # noqa: F811
    day = "2026-09-25"
    start = NOW - timedelta(days=1, hours=2)
    record_reality(learning.store, day, [mover("SOL/USD", "7.2", start, top_up=True),
                                         mover("DOGE/USD", "-2.1", start, top_down=True)])
    note = post_mortem([
        mover_item("SOL/USD", day),
        mover_item("ETH/USD", day),  # Not a mover of that day.
        mover_item("SOL/USD", "2026-09-20"),  # No reality recorded that day.
        mover_item("DOGE/USD", day, cause="MARKET_WIDE", sources=[]),
        mover_item("DOGE/USD", day, knowable=True, sources=[
            source("late", published_at=start + timedelta(minutes=1))]),
        mover_item("SOL/USD", day, cause="NO_NEWS", knowable=None, sources=[]),  # Duplicate.
        mover_item("DOGE/USD", day, summary="As claude noted, flows drove it."),
        {"subject": {"kind": "MOVER", "symbol": "SOL/USD", "day": "25-09-2026"},
         "cause": "SURPRISE", "knowable_before_move": None, "summary": "x",
         "pre_move_technicals": "y"},
    ])
    reply = learning.client.post(POST_MORTEMS, json=note, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202, reply.text
    receipt = reply.json()
    assert (receipt["accepted_count"], receipt["rejected_count"]) == (1, 7)
    codes = [(r["status"], r.get("code")) for r in receipt["item_results"]]
    assert codes == [
        ("ACCEPTED", None), ("REJECTED", "NOT_A_RECORDED_MOVER"),
        ("REJECTED", "REALITY_DAY_NOT_RECORDED"), ("REJECTED", "POST_MORTEM_SOURCES_REQUIRED"),
        ("REJECTED", "KNOWABLE_BEFORE_MOVE_UNSUPPORTED"),
        ("REJECTED", "DUPLICATE_SUBJECT_IN_NOTE"), ("REJECTED", "AGENT_IDENTITY_IN_POST_MORTEM"),
        ("REJECTED", "INVALID_POST_MORTEM_ITEM"),
    ]
    assert receipt["item_results"][0]["subject_key"] == "MOVER:2026-09-25:SOL/USD"
    assert receipt["item_results"][7]["subject_key"] is None
    # The union's tag is not a schema-like key, so the path masks it as ``*``.
    assert {"path": "items[7].subject.*.day", "code": "DAY_FORMAT_REQUIRED"} in \
        receipt["item_results"][7]["errors"]
    [row] = events(learning.store, "POST_MORTEM")
    assert row["setup_id"] is None
    assert row["idempotency_key"] == f"post-mortem:claude:{note['note_id']}"
    [item] = row["body"]["items"]
    assert item["subject_key"] == "MOVER:2026-09-25:SOL/USD" and item["cause"] == "COIN_NEWS"
    assert item["reference_at"] == start.isoformat()
    assert item["sources"][0]["content_hash"]
    again = learning.client.post(POST_MORTEMS, json=note, headers=bearer(AGENTS["claude"]))
    assert again.json() == {**receipt, "idempotent_replay": True}
    changed = copy.deepcopy(note)
    changed["items"][0]["summary"] = "Different."
    reply = learning.client.post(POST_MORTEMS, json=changed, headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (
        422, {"detail": "POST_MORTEM_IDEMPOTENCY_CONTENT_MISMATCH"})


def test_a_note_with_no_acceptable_item_is_refused_whole(learning):  # noqa: F811
    note = post_mortem([mover_item("SOL/USD", "2026-09-20")])
    reply = learning.client.post(POST_MORTEMS, json=note, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 422
    body = reply.json()
    assert body["detail"] == "REALITY_DAY_NOT_RECORDED"
    assert body["item_results"][0]["status"] == "REJECTED"
    stale = post_mortem([mover_item("SOL/USD", "2026-09-20")],
                        generated_at=(NOW - timedelta(minutes=2)).isoformat())
    reply = learning.client.post(POST_MORTEMS, json=stale, headers=bearer(AGENTS["claude"]))
    assert reply.json() == {"detail": "POST_MORTEM_STALE_OR_FUTURE"}
    empty = post_mortem([])
    reply = learning.client.post(POST_MORTEMS, json=empty, headers=bearer(AGENTS["claude"]))
    assert reply.json()["detail"] == "INVALID_POST_MORTEM"
    other = post_mortem([mover_item("SOL/USD", "2026-09-20")], agent_id="instinct")
    reply = learning.client.post(POST_MORTEMS, json=other, headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    assert events(learning.store, "POST_MORTEM") == []


def test_trade_post_mortems_only_for_the_agents_own_closed_trades(mx):  # noqa: F811
    engine, venue, _ = mx
    now = [venue.now]
    intake = intake_for(engine.store, now)
    web = client_for(engine.store, intake)
    agent = fixture_agent("claude", "1.0")
    own = engine.admit(attributed(mx, "BTC/USD", agent))
    assert engine.observe_trigger(own, observation(mx))["outcome"] == "APPROVED"
    entry = venue.orders_of("buy")[-1]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(own, observation(mx))  # Open, not closed yet.
    legacy_closed = close(mx, "ETH/USD")  # An unattributed legacy trade, closed.
    now[0] = venue.now

    def note(*items, agent_id="claude"):
        return post_mortem(list(items), now=now[0], agent_id=agent_id)

    reply = web.post(POST_MORTEMS, json=note(trade_item(own), trade_item(legacy_closed),
                                             trade_item(uuid4())),
                     headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 422
    assert [r["code"] for r in reply.json()["item_results"]] == [
        "TRADE_NOT_CLOSED", "TRADE_OF_ANOTHER_AGENT", "TRADE_NOT_FOUND"]
    # The legacy credential owns unattributed legacy trades.
    published = now[0] - timedelta(days=1)
    item = trade_item(legacy_closed, cause="COIN_NEWS", knowable=True,
                      sources=[source("n1", published_at=published, retrieved_at=now[0])])
    reply = web.post(POST_MORTEMS, json=note(item, agent_id="muse"), headers=bearer(LEGACY))
    assert reply.status_code == 202, reply.text
    [row] = events(engine.store, "POST_MORTEM")
    with engine.repo.connect() as conn:
        first_fill = conn.execute("""SELECT min(filled_at) AS at FROM lab.managed_fills
            WHERE setup_id=%s AND side='buy'""", (legacy_closed,)).fetchone()["at"]
    assert row["body"]["items"][0]["reference_at"] == first_fill.astimezone(UTC).isoformat()
    # knowable_before_move needs a source published at or before the entry.
    now[0] = venue.now + timedelta(minutes=5)
    late = trade_item(legacy_closed, cause="COIN_NEWS", knowable=True,
                      sources=[source("n2", published_at=first_fill + timedelta(seconds=1),
                                      retrieved_at=now[0])])
    reply = web.post(POST_MORTEMS, json=note(late, agent_id="muse"), headers=bearer(LEGACY))
    assert reply.json()["item_results"][0]["code"] == "KNOWABLE_BEFORE_MOVE_UNSUPPORTED"


def test_an_unconfigured_intake_answers_503(learning):  # noqa: F811
    reply = client_for(learning.store, None).post(
        POST_MORTEMS, json=post_mortem([mover_item("SOL/USD", "2026-09-25")]),
        headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (503, {"detail": "LEARNING_INTAKE_NOT_CONFIGURED"})


def test_intake_refuses_non_object_bodies():
    intake = LearningIntake(None, clock=lambda: NOW,
                            policy=LearningPolicy(max_age_seconds=60, schedule=None))
    for method in (intake.parse_outlook, intake.parse_post_mortem):
        with pytest.raises(LearningRecordRejected):
            method([])
    assert skipped("X/USD")["direction"] == "SKIPPED"


def test_the_event_feed_never_carries_learning_records_to_a_research_agent(learning):  # noqa: F811
    # Owner decision of 2026-09-28: an agent sees only its own sanitized lessons.
    assert LEARNING_EVENT_KINDS == {
        OUTLOOK_EVENT, POST_MORTEM_EVENT, market_reality.REALITY_EVENT,
        scorecard.SCORECARD_EVENT, weekly_review.REVIEW_EVENT, learning_replays.REPLAY_EVENT,
        learning_replays.DAY_REPLAY_EVENT, daily_brief.BRIEF_EVENT, daily_brief.MISSED_EVENT,
        trade_paths.PATH_EVENT, trade_paths.CONTEXT_EVENT}
    store = learning.store
    with store.transaction() as conn:
        first = store.event(conn, "FIXTURE_FEED_EVENT", {"n": 1})
        for kind in sorted(LEARNING_EVENT_KINDS):
            store.event(conn, kind, {"fixture": kind})
        last = store.event(conn, "FIXTURE_FEED_EVENT", {"n": 2})

    def page(token, after=0, limit=1000, client=learning.client):
        reply = client.get(f"/api/v1/lab/outputs?after={after}&limit={limit}",
                           headers=bearer(token))
        assert reply.status_code == 200, reply.text
        return reply.json()

    everything = page(STATUS)["items"]
    assert {item["kind"] for item in everything} >= LEARNING_EVENT_KINDS
    visible = [i["event_seq"] for i in everything if i["kind"] not in LEARNING_EVENT_KINDS]
    assert first["event_seq"] in visible and last["event_seq"] in visible
    for token in (AGENTS["claude"], LEGACY):
        assert [item["event_seq"] for item in page(token)["items"]] == visible
        # The query skips the hidden run: the next page holds the next visible event.
        after_first = page(token, after=first["event_seq"], limit=1)
        assert [i["event_seq"] for i in after_first["items"]] == [last["event_seq"]]
        assert after_first["next_cursor"] == last["event_seq"]
        assert page(token, after=last["event_seq"]) == {"items": [],
                                                        "next_cursor": last["event_seq"]}
    # The single legacy token without role tokens keeps its full access.
    alone = TestClient(create_managed_app(FixtureCycle(store.repo, store), store,
                                          api_token=LEGACY, runtime_status=lambda: {}))
    assert page(LEGACY, client=alone)["items"] == everything
