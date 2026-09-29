"""research_agent.lessons and the ``lessons`` / ``build --lessons`` commands: the morning's
lessons read plainly, and ordering hints that never narrow coverage (plan 5, guidelines V6).

Offline only: fixture lessons in the shape of the contract's RESEARCH_LESSONS_V1.
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from research_agent import lessons, levels, run
from research_agent.market import Bar
from tests.learning_kit_fixtures import NOW, context_v2, lessons_fixture, pending_mover

D = Decimal


def bucket(**changes):
    return {"picks": 10, "admitted": 4, "filled": 2, "fill_rate": "0.5", "shadow_recorded": 8,
            "shadow_triggered": 6, "shadow_trigger_rate": "0.75", "shadow_r_count": 6,
            "mean_shadow_r_net": "0.40", "shadow_hit_rate": "0.5", **changes}


def lines(**changes):
    return {
        "start": "2026-09-22", "end": "2026-09-29",
        "funnel": {"picks_sent": 120, "accepted": 118, "vetoed": 30, "selected": 60,
                   "admitted": 40, "triggered": 12, "filled": 11, "closed": 9},
        "results": {"trades_closed": 9, "wins": 4, "losses": 5, "mean_r_net": "-0.12",
                    "r_net_count": 9},
        "fill_rate_by_distance": {
            "0-1%": bucket(shadow_recorded=8, shadow_trigger_rate="0.75"),
            "1-2%": bucket(shadow_recorded=6, shadow_trigger_rate="0.5"),
            "3%+": bucket(shadow_recorded=9, shadow_trigger_rate="0")},
        "results_by_timeframe": {"4h": bucket(shadow_r_count=6, shadow_hit_rate="0.67"),
                                 "1d": bucket(shadow_r_count=5, shadow_hit_rate="0")},
        "results_by_rule": {"A": bucket(shadow_r_count=6, shadow_hit_rate="0.5"),
                            "B": bucket(shadow_r_count=6, shadow_hit_rate="0.4")},
        "results_by_kind": {"CHART": bucket(shadow_r_count=3)},
        "excerpt_drop_rate": {"picks_sent": 120, "dropped": 2, "rate": "0.02"},
        "stale_news_vetoes": {"news_picks_ranked": 10, "vetoed": 3, "rate": "0.3"},
        "dossier_size_rejections": {"picks_sent": 120, "rejected": 0, "rate": "0"},
        "causes": {"notable_trades": 5, "with_post_mortem": 4,
                   "by_cause": {"MARKET_WIDE": 3, "COIN_NEWS": 1},
                   "knowable_before_move": {"true": 1, "false": 3, "null": 0}},
        **changes,
    }


def full_lessons(**windows):
    found = lessons_fixture(movers=[pending_mover()],
                            windows={"1d": None, "7d": lines(), "30d": lines(), **windows})
    found["outlook"] = {
        "status": "GRADED", "day": "2026-09-28", "outlook_id": "o1",
        "received_at": "2026-09-27T12:10:00+00:00", "window_start": "2026-09-27T12:10:00+00:00",
        "window_end": "2026-09-28T12:10:00+00:00", "compared": 30, "hits": 12,
        "hit_rate": "0.4", "skipped": 3,
        "calibration": [{"bucket": "0.6-0.8", "count": 10, "hits": 5, "hit_rate": "0.5",
                         "mean_confidence": "0.68"}],
        "misses": [{"symbol": "ETH/USD", "return_pct": "8.2", "move_start_at": "x",
                    "outlook_direction": "FLAT", "outlook_confidence": "0.5"}],
        "false_alarms": [],
        "by_window": {"7d": {"outlooks_graded": 5, "compared": 150, "hits": 60, "hit_rate": "0.4",
                             "misses": 4, "false_alarms": 2, "calibration": []}},
    }
    return found


# --- Buckets ------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry, expected", [
    ("99.01", "0-1%"), ("99", "1-2%"), ("98.01", "1-2%"), ("98", "2-3%"), ("97", "3%+"),
    ("100.5", "0-1%"),  # The distance is absolute, as the scorecard measures it.
])
def test_distance_buckets_match_the_scorecards(entry, expected):
    assert lessons.distance_bucket(D(100), D(entry)) == expected


# --- Hints ----------------------------------------------------------------------------------------

def test_hints_come_only_from_enough_evidence_with_a_real_spread():
    hints = {hint["dimension"]: hint for hint in lessons.derive_hints(full_lessons())}
    assert set(hints) == {"distance_bucket", "timeframe"}  # Rule: spread 0.1; kind: 3 picks.
    distance = hints["distance_bucket"]
    assert distance["prefer"] == ["0-1%", "1-2%"] and distance["window"] == "7d"
    assert distance["text"].startswith(
        "entries 0-1% from price reached the entry 6 of 8 times, entries 3%+ from price 0 of 9")
    assert hints["timeframe"]["prefer"] == ["4h"]
    assert "4-hour setups hit 4 of 6 times, daily setups 0 of 5" in hints["timeframe"]["text"]


def test_the_30_day_lines_answer_only_when_the_7_day_lines_lack_evidence():
    thin = lines(results_by_timeframe={"4h": bucket(shadow_r_count=2)})
    hints = {hint["dimension"]: hint for hint in lessons.derive_hints(full_lessons(**{"7d": thin}))}
    assert hints["timeframe"]["window"] == "30d"
    flat = lines(fill_rate_by_distance={"0-1%": bucket(shadow_trigger_rate="0.5"),
                                        "3%+": bucket(shadow_trigger_rate="0.45")})
    hints = {hint["dimension"] for hint in lessons.derive_hints(full_lessons(**{"7d": flat}))}
    assert "distance_bucket" not in hints  # Enough evidence, no difference: no hint.


def test_a_rule_lesson_never_writes_a_rule_tag_the_scorecard_would_read():
    rules = lines(results_by_rule={"A": bucket(shadow_r_count=6, shadow_hit_rate="0.67"),
                                   "B": bucket(shadow_r_count=6, shadow_hit_rate="0.17")})
    hint = lessons.derive_hint("rule", {"7d": rules})
    assert hint["prefer"] == ["A"] and "rule-" not in hint["text"].lower()
    assert "the stop under the whole window's low" in hint["text"]
    assert len(hint["text"]) <= lessons.MAX_HINT_TEXT
    lessons.load_emphasis({"schema": lessons.EMPHASIS_SCHEMA, "hints": [hint]})


def test_no_lessons_means_no_hints():
    assert lessons.derive_hints(None) == []
    doc = lessons.emphasis_doc(None, context_as_of=NOW.isoformat(), now=NOW)
    assert doc["hints"] == [] and "never drops a coin" in doc["use"]


@pytest.mark.parametrize("hint, code", [
    ({"dimension": "distance_bucket", "prefer": ["0-1%"], "text": "t", "exclude": ["3%+"]},
     "may only hold"),
    ({"dimension": "sector", "prefer": ["DEFI"], "text": "t"}, "dimension unknown"),
    ({"dimension": "timeframe", "prefer": ["3h"], "text": "t"}, "distinct values"),
    ({"dimension": "timeframe", "prefer": [], "text": "t"}, "distinct values"),
    ({"dimension": "rule", "prefer": ["A"], "text": "rule-A setups hit more"}, "level-rule tag"),
    ({"dimension": "kind", "prefer": ["CHART"], "text": ""}, "1-200 characters"),
])
def test_an_emphasis_file_can_only_reorder(hint, code):
    with pytest.raises(lessons.LessonsError, match=code):
        lessons.load_emphasis({"schema": lessons.EMPHASIS_SCHEMA, "hints": [hint]})


def test_a_dimension_may_appear_once_and_the_schema_must_match():
    hint = {"dimension": "kind", "prefer": ["CHART"], "text": "chart-only picks first"}
    with pytest.raises(lessons.LessonsError, match="repeated"):
        lessons.load_emphasis({"schema": lessons.EMPHASIS_SCHEMA, "hints": [hint, hint]})
    with pytest.raises(lessons.LessonsError, match="EMPHASIS_UNKNOWN"):
        lessons.load_emphasis({"schema": "SOMETHING_ELSE", "hints": []})


def test_rank_key_orders_preferred_values_first_in_hint_order():
    hints = ({"dimension": "distance_bucket", "prefer": ["0-1%", "1-2%"]},
             {"dimension": "timeframe", "prefer": ["4h"]})
    near_4h = {"distance_bucket": "0-1%", "timeframe": "4h", "rule": "A", "kind": "CHART"}
    near_1d = {**near_4h, "timeframe": "1d"}
    far_4h = {**near_4h, "distance_bucket": "3%+"}
    assert sorted([far_4h, near_1d, near_4h], key=lambda a: lessons.rank_key(a, hints)) == [
        near_4h, near_1d, far_4h]
    assert lessons.lesson_sentence([]) is None


# --- The plain summary ---------------------------------------------------------------------------

def test_the_summary_reads_every_part_of_the_lessons():
    found = full_lessons()
    text = "\n".join(lessons.summary_lines(found, lessons.derive_hints(found)))
    for expected in ("price reached the entry, by distance: 0-1% 0.75 over 8",
                     "shadow hit rate by timeframe: 4h 0.67 over 6",
                     "stale-news vetoes: 3 of 10",
                     "causes {'MARKET_WIDE': 3, 'COIN_NEWS': 1}",
                     "Outlook graded on 2026-09-28", "12 direction hits of 30",
                     "misses: ETH/USD 8.2% (said FLAT)",
                     "Post-mortems owed: 0 trades, 1 movers", "ETH/USD 2026-09-28: 8.20% (a miss)",
                     "Emphasis for today"):
        assert expected in text, expected
    assert "No lessons in this context" in lessons.summary_lines(None, [])[0]


# --- The commands --------------------------------------------------------------------------------

def test_cli_lessons_writes_emphasis_json(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "context.json").write_text(json.dumps(context_v2(["SOL/USD"],
                                                                lessons=full_lessons())))
    assert run.main(["--run-dir", str(run_dir), "lessons", "--now", NOW.isoformat()]) == 0
    assert "Emphasis for today" in capsys.readouterr().out
    doc = json.loads((run_dir / "emphasis.json").read_text())
    assert [hint["dimension"] for hint in doc["hints"]] == ["distance_bucket", "timeframe"]
    assert lessons.load_emphasis(doc)  # What 'lessons' writes, 'build' accepts.


def _setup_json(retrieved_at, entry_low):
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = entry_low, entry_low + 0.5
    highs[10] = 130
    bars = [Bar(started_at=retrieved_at - timedelta(hours=4 * (20 - i)), open=D(str(lo)),
                high=D(str(hi)), low=D(str(lo)), close=D(str(lo)), volume=D(10))
            for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))]
    setup, tried = levels.find_setup({"4h": bars}, mid=D(100), increment=D("0.01"),
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is not None, tried
    return levels.setup_to_json(setup)


def test_cli_build_with_lessons_reorders_and_records_the_hints(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    retrieved_at = NOW - timedelta(minutes=5)
    (run_dir / "context.json").write_text(json.dumps(context_v2(["FAR/USD", "NEAR/USD"])))
    (run_dir / "market.json").write_text(json.dumps(
        {"retrieved_at": retrieved_at.isoformat(), "coinbase": {}, "excluded": {}}))
    (run_dir / "levels.json").write_text(json.dumps({
        "FAR": {"setup": _setup_json(retrieved_at, 95), "tried": []},
        "NEAR": {"setup": _setup_json(retrieved_at, 99.2), "tried": []}}))
    emphasis = {"schema": lessons.EMPHASIS_SCHEMA, "hints": [
        {"dimension": "distance_bucket", "prefer": ["0-1%"],
         "text": "entries 0-1% from price reached the entry 6 of 8 times: rank them first"}]}
    (run_dir / "emphasis.json").write_text(json.dumps(emphasis))
    base = ["--run-dir", str(run_dir), "build", "--agent-id", "claude", "--agent-version",
            "kit-test-1", "--now", NOW.isoformat()]
    assert run.main([*base, "--lessons", str(run_dir / "emphasis.json")]) == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["NEAR/USD", "FAR/USD"]
    notes = json.loads((run_dir / "build-notes.json").read_text())
    assert notes["lessons"]["order_before"] == ["FAR/USD", "NEAR/USD"]
    assert notes["lessons"]["promoted"] == {"NEAR/USD": ["distance_bucket"]}
    run_id = json.loads((run_dir / "run.json").read_text())["run_id"]
    assert report["agent"]["run_id"] == run_id  # The run folder's research-run ID.

    (run_dir / "emphasis.json").write_text(json.dumps({
        **emphasis, "hints": [{**emphasis["hints"][0], "exclude": ["3%+"]}]}))
    assert run.main([*base, "--lessons", str(run_dir / "emphasis.json")]) == 2
    assert "EMPHASIS_INVALID" in capsys.readouterr().err


UNAVAILABLE = {"lessons_version": "RESEARCH_LESSONS_V1", "agent_id": "claude",
               "as_of": NOW.isoformat(), "available": False, "code": "LESSONS_UNAVAILABLE"}


def test_unavailable_lessons_say_so_and_give_no_emphasis(tmp_path, capsys):
    """lessons.available false (package learning-app, 65b5e17): 'lessons' says so with the
    code and writes an emphasis.json without hints, which 'build --lessons' accepts."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "context.json").write_text(json.dumps(context_v2(["SOL/USD"],
                                                                lessons=UNAVAILABLE)))
    assert run.main(["--run-dir", str(run_dir), "lessons", "--now", NOW.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "Lessons unavailable this run (LESSONS_UNAVAILABLE)" in out
    doc = json.loads((run_dir / "emphasis.json").read_text())
    assert doc["hints"] == [] and doc["unavailable"] == "LESSONS_UNAVAILABLE"
    assert lessons.load_emphasis(doc) == ()


def test_build_with_an_empty_emphasis_builds_exactly_as_without_it(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    retrieved_at = NOW - timedelta(minutes=5)
    (run_dir / "context.json").write_text(json.dumps(context_v2(["FAR/USD", "NEAR/USD"])))
    (run_dir / "market.json").write_text(json.dumps(
        {"retrieved_at": retrieved_at.isoformat(), "coinbase": {}, "excluded": {}}))
    (run_dir / "levels.json").write_text(json.dumps({
        "FAR": {"setup": _setup_json(retrieved_at, 95), "tried": []},
        "NEAR": {"setup": _setup_json(retrieved_at, 99.2), "tried": []}}))
    empty = lessons.emphasis_doc(None, context_as_of=None, now=NOW,
                                 unavailable="LESSONS_UNAVAILABLE")
    (run_dir / "emphasis.json").write_text(json.dumps(empty))
    base = ["--run-dir", str(run_dir), "build", "--agent-id", "claude", "--agent-version",
            "kit-test-1", "--now", NOW.isoformat()]

    def picks():
        report = json.loads((run_dir / "report.json").read_text())
        return [(p["symbol"], p["selection_rationale"]["why_over_peers"]) for p in report["picks"]]

    assert run.main(base) == 0
    without = picks()
    assert run.main([*base, "--lessons", str(run_dir / "emphasis.json")]) == 0
    assert picks() == without
    assert json.loads((run_dir / "build-notes.json").read_text())["lessons"] is None
