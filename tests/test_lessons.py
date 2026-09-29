"""RESEARCH_LESSONS_V1, the research context V2's ``lessons`` (package learning-app).

Fixture evidence only: the fixture paper venue, fixture market-data feeds and per-test disposable
PostgreSQL databases. No broker, provider, network or owner-ledger contact.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.lessons import LESSONS_VERSION, lessons_for, outlook_section, was_miss
from catalyst_lab.market import NY
from catalyst_lab.scorecard import record_scorecard
from tests.learning_fixtures import agent_block, close_attributed, mover, record_reality
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_research_context import context_lab as context_lab  # noqa: F401
from tests.test_scorecard import scored_day


def grade(agent_id, day, received, *, hits, compared, misses=(), false_alarms=()):
    return {
        "agent_id": agent_id, "agent_version": "1.0", "outlook_id": str(uuid4()),
        "outlook_event_seq": 1, "received_at": received.isoformat(),
        "window_start": received.isoformat(),
        "window_end": (received + timedelta(hours=24)).isoformat(), "grading_day": day,
        "compared": compared, "hits": hits,
        "hit_rate": f"{hits / compared:.4f}" if compared else None, "skipped": 1,
        "unmeasured": 0,
        "calibration": [
            {"bucket": "0.6-0.8", "count": compared, "hits": hits, "hit_rate": None,
             "mean_confidence": "0.7000", "confidence_sum": str(0.7 * compared)},
        ],
        "misses": [{"symbol": s, "return_pct": "6.0000", "move_start_at": received.isoformat(),
                    "outlook_direction": "FLAT", "outlook_confidence": "0.5"} for s in misses],
        "false_alarms": [{"symbol": s, "return_pct": "0.2000",
                          "move_start_at": received.isoformat(), "outlook_direction": "UP",
                          "outlook_confidence": "0.8"} for s in false_alarms],
        "market_calls": {}, "coins": [],
    }


def test_an_agent_reads_only_its_own_lessons(mx):  # noqa: F811
    store, sid, day = scored_day(mx)
    engine, venue, _ = mx
    unnoted, _ = close_attributed(mx, "SOL/USD")  # Closed, no post-mortem yet.
    day_start, day_end = day_bounds(day)
    now = day_end + timedelta(hours=2)
    assert record_scorecard(store, day, now=now)[0] == "RECORDED"
    previous = (day - timedelta(days=1)).isoformat()
    received = day_start - timedelta(hours=16)
    record_reality(store, previous, [], grades=[
        grade("claude", previous, received - timedelta(days=1), hits=1, compared=4)],
        outlook_agents=["claude"], universe={"count": 33}, measured_count=33,
        mover_share="0.0000", factors={})
    start = day_start + timedelta(hours=9)
    movers = [mover("DOGE/USD", "8.0000", start, top_up=True),
              mover("BTC/USD", "2.0000", start, top_up=True),
              mover("ETH/USD", "-6.5000", start, top_down=True)]
    movers[0]["missed_by"] = ["claude"]  # claude called DOGE FLAT; ETH was called right.
    record_reality(store, day.isoformat(), movers, grades=[
        grade("claude", day.isoformat(), received, hits=3, compared=4, misses=["DOGE/USD"],
              false_alarms=["XRP/USD"]),
        grade("instinct", day.isoformat(), received, hits=0, compared=4,
              misses=["SECRET/USD"])],
        outlook_agents=["claude", "instinct"], universe={"count": 33}, measured_count=33,
        mover_share="0.0909", factors={"btc_return_pct": "2.0000", "eth_return_pct": "-6.5000",
                                       "sectors": [], "total_volume_usd": "1.00",
                                       "total_volume_vs_7d_avg": None, "btc": {}})
    with store.transaction() as conn:
        store.event(conn, "POST_MORTEM", {
            "schema_version": "POST_MORTEM_V1", "note_id": str(uuid4()),
            "agent": agent_block("claude"),
            "items": [{"subject_key": f"MOVER:{day.isoformat()}:ETH/USD", "cause": "NO_NEWS",
                       "knowable_before_move": None}]})
    lessons = lessons_for(store.repo, "claude", now=now)
    assert lessons["lessons_version"] == LESSONS_VERSION and lessons["agent_id"] == "claude"
    assert lessons["scorecard_day"] == day.isoformat()
    one = lessons["windows"]["1d"]
    assert one["results"]["trades_closed"] == 2 and one["funnel"]["cycles"] == 1
    assert set(one) >= {"funnel", "results", "selection", "fill_rate_by_distance",
                        "results_by_timeframe", "results_by_rule", "results_by_kind",
                        "results_by_sector", "excerpt_drop_rate", "stale_news_vetoes",
                        "dossier_size_rejections", "causes"}
    assert not {"maintenance", "arms", "costs", "trades"} & set(one)
    outlook = lessons["outlook"]
    assert outlook["status"] == "GRADED" and outlook["day"] == day.isoformat()
    assert (outlook["compared"], outlook["hits"], outlook["hit_rate"]) == (4, 3, "0.7500")
    assert outlook["window_start"] == received.isoformat()
    assert [m["symbol"] for m in outlook["misses"]] == ["DOGE/USD"]
    assert [f["symbol"] for f in outlook["false_alarms"]] == ["XRP/USD"]
    assert "confidence_sum" not in outlook["calibration"][0]
    assert [g["grading_day"] for g in outlook["graded_outlooks"]] == [day.isoformat(), previous]
    seven = outlook["by_window"]["7d"]
    assert (seven["outlooks_graded"], seven["compared"], seven["hits"], seven["misses"]) == (
        2, 8, 4, 1)
    assert seven["hit_rate"] == "0.5000"
    assert seven["calibration"][3] == {"bucket": "0.6-0.8", "count": 8, "hits": 4,
                                       "hit_rate": "0.5000", "mean_confidence": "0.7000"}
    assert [d["day"] for d in lessons["recent_days"]] == [day.isoformat(), previous]
    assert lessons["recent_days"][0]["factors"]["btc_return_pct"] == "2.0000"
    assert "btc" not in lessons["recent_days"][0]["factors"]
    pending = lessons["pending_post_mortems"]
    assert [(m["symbol"], m["was_miss"]) for m in pending["movers"]] == [
        ("DOGE/USD", True), ("BTC/USD", False)]  # ETH noted; BTC under 5%, never a miss.
    [trade] = pending["trades"]  # The noted BTC trade is not pending.
    assert trade["setup_id"] == str(unnoted) and trade["symbol"] == "SOL/USD"
    assert trade["notable_reasons"] == ["WIN_OVER_1_5R"] and trade["r_net"]
    assert trade["entry_at"] and trade["exit_at"] and trade["exit_reason"] == "TARGET_EXIT"
    text = str(lessons)
    assert "instinct" not in text and "SECRET/USD" not in text  # Never another agent's.
    other = lessons_for(store.repo, "instinct", now=now)
    assert other["windows"] == {"1d": None, "7d": None, "30d": None}
    assert other["pending_post_mortems"]["trades"] == []
    assert [m["was_miss"] for m in other["pending_post_mortems"]["movers"]] == [
        False, False, False]  # instinct's outlook covered none of them as a miss.
    assert other["outlook"]["misses"][0]["symbol"] == "SECRET/USD"
    stranger = lessons_for(store.repo, "grogbot", now=now)
    assert stranger["outlook"]["status"] == "NO_GRADED_OUTLOOK_YET"
    # With no outlook at all, a big mover counts as missed ("no such outlook exists").
    assert [m["was_miss"] for m in stranger["pending_post_mortems"]["movers"]] == [
        True, False, True]
    assert venue.now.astimezone(NY).date() == day


def test_an_unreadable_record_never_blocks_the_context(mx, monkeypatch):  # noqa: F811
    import catalyst_lab.lessons as lessons

    engine, venue, _ = mx
    record_reality(engine.store, "2026-09-27", [{"symbol": "X/USD"}])  # No return, no start.
    monkeypatch.setattr(lessons, "pending_trades", lambda *a: [])
    served = lessons.safe_lessons(engine.repo, "claude", now=venue.now)
    assert served == {"lessons_version": LESSONS_VERSION, "agent_id": "claude",
                      "as_of": venue.now.isoformat(), "available": False,
                      "code": "LESSONS_UNAVAILABLE"}


def test_was_miss_and_an_empty_record():
    big = {"big_move": True, "missed_by": ["claude"]}
    assert was_miss(big, "claude", ["claude"]) and not was_miss(big, "muse", ["claude", "muse"])
    assert was_miss(big, "grogbot", ["claude"]) and not was_miss({"big_move": False}, "x", [])
    assert outlook_section([])["status"] == "NO_GRADED_OUTLOOK_YET"


def test_the_status_credential_reads_no_lessons(context_lab):  # noqa: F811
    principal = SimpleNamespace(role="status", agent_id=None, legacy=False)
    body = context_lab.service.context(principal)
    assert body["context_version"] == "RESEARCH_CONTEXT_V2" and body["lessons"] is None
    agent = context_lab.service.context(SimpleNamespace(role="muse", agent_id="claude",
                                                        legacy=False))
    assert agent["lessons"]["outlook"]["status"] == "NO_GRADED_OUTLOOK_YET"
    assert agent["lessons"]["pending_post_mortems"] == {"trades": [], "movers": []}
