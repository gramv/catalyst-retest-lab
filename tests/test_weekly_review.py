"""WEEKLY_REVIEW_V1: pre-registered tests, minimum samples, seeded bootstrap, verdicts.

Fixture evidence only: synthetic shadow outcomes and replays, the fixture paper venue and
per-test disposable PostgreSQL databases.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_replays import REPLAY_EVENT
from catalyst_lab.weekly_review import (
    MINIMUMS,
    SplitMix64,
    bootstrap,
    compute_review,
    last_completed_week_end,
    next_version,
    record_review,
    seed_for,
    verdict,
    week_of,
)
from tests.learning_fixtures import close_attributed
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401


def test_splitmix64_matches_the_reference_sequence():
    generator = SplitMix64(0)
    assert [generator.next() for _ in range(3)] == [
        0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F]
    assert all(0 <= SplitMix64(7).below(3) < 3 for _ in range(10))


def test_the_bootstrap_is_seeded_and_deterministic():
    a = [D(i % 7) - D(3) for i in range(50)]
    b = [D(i % 5) - D(3) for i in range(50)]
    seed = seed_for("2026-10-04", "SELECTION_VALUE")
    first = bootstrap([a, b], seed=seed)
    assert first == bootstrap([a, b], seed=seed)
    assert first != bootstrap([a, b], seed=seed + 1)
    lower, upper = first
    effect = sum(a) / 50 - sum(b) / 50
    assert lower < effect < upper
    one = bootstrap([[D(1), D(2), D(3)]], seed=1)
    assert D(1) <= one[0] <= one[1] <= D(3)


def test_verdicts_follow_the_minimums_and_the_interval():
    seed = seed_for("2026-10-04", "X")
    few = [D("0.5")] * 10
    effect, interval, evidence, outcome, short = verdict([("a", few)], [30], seed=seed)
    assert (outcome, interval, evidence, short) == ("NOT_ENOUGH_DATA", None, None, ["a:10<30"])
    assert effect == D("0.5")
    good = [D("0.5") + D(i % 3) / 10 for i in range(30)]
    assert verdict([("a", good)], [30], seed=seed)[2:4] == ("ADDS_VALUE", "KEEP")
    bad = [-v for v in good]
    effect, interval, evidence, outcome, _ = verdict([("a", bad)], [30], seed=seed)
    assert (evidence, outcome) == ("LOSES_VALUE", "PROPOSE") and D(interval["upper"]) < 0
    mixed = [D(i % 5) - D(2) for i in range(30)]
    assert verdict([("a", mixed)], [30], seed=seed)[2:4] == ("INCONCLUSIVE", "KEEP")
    # A comparison group needs at least two values even where its minimum is lower.
    assert verdict([("passed", [D(1)]), ("vetoed", good)], [0, 15], seed=seed)[3] == \
        "NOT_ENOUGH_DATA"


def test_the_plan_minimums_are_pre_registered():
    assert MINIMUMS == {"SELECTION_VALUE": 40, "MANAGEMENT_VALUE": 30, "LEVEL_MOVES": 30,
                        "DAY_REVIEW_DECISIONS": 20, "VETOES": 15}


def test_versions_and_weeks():
    assert next_version("CRYPTO_MAINTENANCE_V3", "X") == "CRYPTO_MAINTENANCE_V4"
    assert next_version("JEV_TOP_K_SELECTION_V12", "X") == "JEV_TOP_K_SELECTION_V13"
    assert next_version(None, "FALLBACK") == "FALLBACK"
    assert week_of(date(2026, 10, 1)) == (date(2026, 9, 28), date(2026, 10, 4))
    monday_night = datetime(2026, 10, 5, 5, 30, tzinfo=UTC)  # 01:30 Monday in New York.
    assert last_completed_week_end(monday_night) == date(2026, 10, 4)
    sunday = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)  # 16:00 Sunday: the week is not over.
    assert last_completed_week_end(sunday) == date(2026, 9, 27)


def shadow_pick(store, *, group, net_r, generated_at, policy="JEV_TOP_K_SELECTION_V2"):
    status, selected = {"selected": ("RANKED", True), "passed": ("RANKED", False),
                        "vetoed": ("VETOED", False)}[group]
    cycle_id, item_key = str(uuid4()), "CRYPTO:" + uuid4().hex[:6].upper() + "/USD"
    body = {"pick": {"cycle_id": cycle_id, "item_key": item_key, "revision": 1,
                     "ranking_status": status, "selected": selected, "replacement_for": None,
                     "generated_at": generated_at.isoformat(), "engineering": False,
                     "selection_policy": policy},
            "outcome": {"outcome": "TARGET", "triggered": True, "data_complete": True,
                        "net_r": str(net_r)}}
    with store.transaction() as conn:
        store.event(conn, "PICK_SHADOW_OUTCOME", body,
                    key=f"pick-shadow-outcome:{cycle_id}:{item_key}:1")


def test_the_review_records_verdicts_and_drafts_proposals(mx):  # noqa: F811
    engine, venue, _ = mx
    store = engine.store
    sid, _ = close_attributed(mx, "BTC/USD")
    week_end = last_completed_week_end(venue.now + timedelta(days=7))
    _, until = day_bounds(week_end)
    before = venue.now - timedelta(hours=1)
    for i in range(40):
        shadow_pick(store, group="selected", net_r=D("0.5") + D(i % 4) / 10, generated_at=before)
        shadow_pick(store, group="passed", net_r=D("-0.3") + D(i % 3) / 10, generated_at=before)
    for i in range(15):
        shadow_pick(store, group="vetoed", net_r=D("1.0") + D(i % 2) / 10, generated_at=before)
    shadow_pick(store, group="vetoed", net_r="9", generated_at=until + timedelta(hours=1))
    with store.transaction() as conn:
        for i in range(30):  # Every stop raise left the trade far below its unchanged plan.
            store.event(conn, REPLAY_EVENT, {
                "method": "UNCHANGED_PLAN_REPLAY_V1", "setup_id": str(sid),
                "change_kind": "STOP_RAISE", "unchanged_net_r": str(D("9") + D(i) / 100),
                "data_complete": True, "source_event_seq": 10_000 + i})
    with pytest.raises(ValueError, match="WEEK_END_MUST_BE_SUNDAY"):
        compute_review(store.repo, week_end - timedelta(days=1), now=until)
    with pytest.raises(ValueError, match="WEEK_NOT_OVER"):
        compute_review(store.repo, week_end, now=until - timedelta(seconds=1))
    now = until + timedelta(hours=1, minutes=30)
    status, seq = record_review(store, week_end, now=now)
    assert status == "RECORDED"
    assert record_review(store, week_end, now=now) == ("ALREADY_RECORDED", seq)
    with store.repo.connect() as conn:
        row = conn.execute("SELECT * FROM lab.managed_events WHERE event_seq=%s",
                           (seq,)).fetchone()
    assert row["kind"] == "WEEKLY_REVIEW" and row["setup_id"] is None
    assert row["idempotency_key"] == f"weekly-review:{week_end.isoformat()}"
    body = row["body"]
    assert body["review_version"] == "WEEKLY_REVIEW_V1"
    assert body["week_start"] == (week_end - timedelta(days=6)).isoformat()
    tests = {t["test_id"]: t for t in body["tests"]}
    assert set(tests) == {"SELECTION_VALUE", "MANAGEMENT_VALUE", "LEVEL_MOVES_STOP_RAISE",
                          "LEVEL_MOVES_TARGET_RAISE", "LEVEL_MOVES_STOP_AND_TARGET_RAISE",
                          "DAY_REVIEW_DECISIONS", "VETOES"}
    selection = tests["SELECTION_VALUE"]
    assert selection["samples"] == {"selected": 40, "passed": 40}
    assert (selection["evidence"], selection["verdict"]) == ("ADDS_VALUE", "KEEP")
    assert D(selection["interval_90"]["lower"]) > 0 and selection["proposal"] is None
    vetoes = tests["VETOES"]
    assert vetoes["samples"] == {"passed": 40, "vetoed": 15}  # The late pick is not evidence.
    assert (vetoes["evidence"], vetoes["verdict"]) == ("LOSES_VALUE", "PROPOSE")
    assert vetoes["proposal"]["version"] == "JEV_TOP_K_SELECTION_V3"
    assert "owner approval required" in vetoes["proposal"]["text"]
    assert "JEV_TOP_K_SELECTION_V2" in vetoes["proposal"]["text"]
    raises = tests["LEVEL_MOVES_STOP_RAISE"]
    assert raises["samples"] == {"STOP_RAISE": 30} and raises["verdict"] == "PROPOSE"
    assert raises["proposal"]["text"].startswith("### `")
    management = tests["MANAGEMENT_VALUE"]
    assert management["verdict"] == "NOT_ENOUGH_DATA" and management["interval_90"] is None
    assert tests["LEVEL_MOVES_TARGET_RAISE"]["shortfall"] == ["TARGET_RAISE:0<30"]
    assert tests["DAY_REVIEW_DECISIONS"]["by_decision"]["CONTINUE"]["count"] == 0
    assert body["method"]["resamples"] == 2000 and body["method"]["percentile_ranks"] == [
        100, 1900]
    # Deterministic: the same evidence gives the same intervals on demand.
    again = compute_review(store.repo, week_end, now=now)
    assert [t["interval_90"] for t in again["tests"]] == [t["interval_90"] for t in body["tests"]]
