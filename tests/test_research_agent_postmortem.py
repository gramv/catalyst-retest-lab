"""research_agent.postmortem and the post-mortem commands: the evening worksheet, the
knowable-before-move helper and POST_MORTEM_V1 (package learning-kit; contract of package
learning-app).

Offline only: fixture lessons and candles, news pages served by a monkeypatched
``sources.fetch_page``, and a monkeypatched POST. No real network access in this file.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from research_agent import market as market_module
from research_agent import postmortem, run, sources, technicals
from research_agent import submit as submit_module
from research_agent import token as token_module
from tests.learning_kit_fixtures import (
    AGENT,
    DAY,
    DAY_START,
    FAKE_TOKEN,
    NOW,
    context_v2,
    hour_rows,
    lessons_fixture,
    news_page,
    pending_mover,
    pending_trade,
)

URL = "https://news.example/eth-upgrade"
EXCERPT = "The network upgrade was scheduled for activation this week."
BEFORE = "2026-09-27T09:00:00Z"  # Before ETH's move_start_at (2026-09-28T13:00Z).
AFTER = "2026-09-28T14:30:00Z"


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


def closes_for(symbol, start, hours):
    """Flat at 100, then ETH steps up at 13:00 UTC on DAY and SOL falls from 14:00."""
    closes = []
    for index in range(hours):
        moment = start + timedelta(hours=index)
        price = 100
        if symbol == "ETH/USD" and moment >= DAY_START + timedelta(hours=9):
            price = 108
        if symbol == "SOL/USD" and moment >= DAY_START + timedelta(hours=10):
            price = 96
        closes.append(price)
    return closes


def bars_for(symbol, start, end):
    hours = int((end - start) / timedelta(hours=1))
    rows = hour_rows(start, closes_for(symbol, start, hours))
    return technicals.complete_hourly_bars(rows, retrieved_at=end), None, end.isoformat()


def lessons(**changes):
    return lessons_fixture(trades=[pending_trade()],
                           movers=[pending_mover("BTC/USD", was_miss=False, return_pct="-3.1"),
                                   pending_mover("ETH/USD")], **changes)


def worksheet(previous=None, **changes):
    found = lessons(**changes)
    return postmortem.build_worksheet(ctx=context_v2(["BTC/USD", "ETH/USD", "SOL/USD"],
                                                     lessons=found),
                                      lessons=found, day=DAY, bars_for=bars_for,
                                      previous=previous, now=NOW)


def decide(row, **answers):
    row.update({"cause": "COIN_NEWS", "knowable_before_move": True,
                "summary": "The network upgrade drew buyers.",
                "sources": [{"url": URL, "excerpt": EXCERPT, "published_at": BEFORE}],
                "pre_move_technicals": "A tight range under the 7-day high, then a breakout.",
                "factors": [{"kind": "EVENT", "key": "NETWORK_UPGRADE",
                             "description": "Scheduled network upgrades"}], **answers})
    return row


def decide_all(sheet):
    for row in sheet["subjects"]:
        if row["symbol"] == "ETH/USD":
            decide(row)
        else:
            decide(row, cause="NO_NEWS", knowable_before_move=None, sources=[],
                   summary="The setup failed with the market.", factors=[])
    return sheet


# --- The day must be recorded before anything is built ------------------------------------

def test_the_worksheet_waits_for_the_apps_record_of_the_day():
    with pytest.raises(postmortem.PostmortemError, match="LESSONS_MISSING"):
        postmortem.require_reality(None, DAY)
    with pytest.raises(postmortem.PostmortemError,
                       match="REALITY_DAY_NOT_RECORDED.*latest recorded: 2026-09-27.*02:00"):
        postmortem.require_reality(lessons(recorded=(DAY - timedelta(days=1),)), DAY)
    with pytest.raises(postmortem.PostmortemError, match="LESSONS_OUTLOOK_UNKNOWN"):
        postmortem.require_reality(lessons(outlook_status="NO_REALITY_YET"), DAY)
    assert postmortem.require_reality(lessons(outlook_status="NO_GRADED_OUTLOOK_YET"), DAY) == []
    older = postmortem.require_reality(lessons(outlook_day=DAY - timedelta(days=2)), DAY)
    assert older and "No outlook was graded on 2026-09-28" in older[0]


# --- The worksheet ----------------------------------------------------------------------------

def test_the_subjects_are_exactly_the_owed_post_mortems_misses_first():
    sheet = worksheet()
    assert [row["subject_id"] for row in sheet["subjects"]] == [
        "TRADE:0b5d9d44-1f55-4a5e-9f8a-2f3c7a1e9b10",
        "MOVER:2026-09-28:ETH/USD", "MOVER:2026-09-28:BTC/USD"]
    trade, eth, _btc = sheet["subjects"]
    assert trade["subject"] == {"kind": "TRADE", "setup_id": pending_trade()["setup_id"]}
    assert eth["subject"] == {"kind": "MOVER", "symbol": "ETH/USD", "day": "2026-09-28"}
    assert (eth["cause"], eth["knowable_before_move"]) == (None, postmortem.UNDECIDED)
    assert eth["was_miss"] is True and trade["notable_reasons"] == ["STOP"]


def test_each_subject_has_its_price_window_reference_bar_and_pre_move_facts():
    trade, eth, _btc = worksheet()["subjects"]
    facts = eth["facts"]
    assert facts["reference_at"] == "2026-09-28T13:00:00+00:00"
    assert facts["reference_basis"] == "MOVE_START_AT"
    assert facts["reference_bar"]["bar_id"] == "ETH-1h-20260928T1300Z"
    assert facts["window"]["bars_used"] == 24
    assert facts["kit_measure"]["return_pct"] == "8.00"
    assert facts["pre_move_technicals_computed"]["as_of"] == "2026-09-28T13:00:00+00:00"
    draft = facts["pre_move_technicals_draft"]
    assert draft.startswith("Before 2026-09-28 13:00 UTC") and len(draft) <= 300
    trade_facts = trade["facts"]
    assert (trade_facts["reference_at"], trade_facts["reference_basis"]) == (
        "2026-09-28T10:17:00+00:00", "FIRST_ENTRY_FILL")
    assert (trade_facts["window"]["start"], trade_facts["window"]["end"]) == (
        "2026-09-28T10:00:00+00:00", "2026-09-28T16:00:00+00:00")
    assert trade_facts["kit_measure"]["return_pct"] == "-4.00"
    assert trade_facts["kit_measure"]["half_move_bar"]["bar_id"] == "SOL-1h-20260928T1400Z"


def test_a_coin_without_market_data_still_gets_its_subject():
    found = lessons()
    sheet = postmortem.build_worksheet(
        ctx=context_v2(["ETH/USD"]), lessons=found, day=DAY, now=NOW,
        bars_for=lambda symbol, start, end: (None, "NO_COINBASE_USD_CANDLES", None))
    assert len(sheet["subjects"]) == 3
    assert sheet["subjects"][1]["facts"]["market_data"] == {
        "unavailable": "NO_COINBASE_USD_CANDLES"}


def test_rebuilding_keeps_the_sessions_answers():
    first = worksheet()
    decide(first["subjects"][1])
    again = worksheet(previous=first)
    assert again["subjects"][1]["cause"] == "COIN_NEWS"
    assert again["subjects"][1]["sources"][0]["url"] == URL
    assert again["subjects"][0]["cause"] is None


# --- The knowable-before-move helper ------------------------------------------------------------

@pytest.mark.parametrize("published, suggestion", [
    ([BEFORE], True), (["2026-09-28T13:00:00Z"], True),  # At the reference time counts.
    ([AFTER], False), ([None], None), ([AFTER, None], None), ([], None),
])
def test_the_helper_suggests_true_only_with_a_source_published_by_the_reference(published,
                                                                                 suggestion):
    row = worksheet()["subjects"][1]
    row["sources"] = [{"url": URL, "excerpt": EXCERPT, "published_at": value}
                      for value in published]
    helper = postmortem.knowable_helper(row)
    assert helper["suggestion"] is suggestion
    assert helper["reference_at"] == "2026-09-28T13:00:00+00:00"


def test_the_helper_never_supports_true_without_a_reference_time():
    row = worksheet()["subjects"][1]
    row["facts"]["reference_at"] = None
    row["sources"] = [{"url": URL, "excerpt": EXCERPT, "published_at": BEFORE}]
    helper = postmortem.knowable_helper(row)
    assert helper["suggestion"] is None and "never supported" in helper["why"]


def test_the_helper_reads_a_decided_mover_in_plan_5c_terms():
    row = decide(worksheet()["subjects"][1])
    assert postmortem.knowable_helper(row)["label"] == "KNOWABLE_AND_MISSED"
    row.update(knowable_before_move=False)
    assert postmortem.knowable_helper(row)["label"] == "KNOWABLE_NOT_ACTIONABLE"
    row.update(cause="SURPRISE")
    assert postmortem.knowable_helper(row)["label"] == "SURPRISE"


# --- Checks -------------------------------------------------------------------------------------

def test_a_decided_subject_passes():
    assert postmortem.check_subject(decide(worksheet()["subjects"][1]), agent_id="claude") == []


@pytest.mark.parametrize("answers, code", [
    ({"cause": "HACK"}, "cause"),
    ({"knowable_before_move": "maybe"}, "decide true, false or null"),
    ({"summary": " "}, "summary: 1-400"),
    ({"pre_move_technicals": "x" * 301}, "pre_move_technicals: 1-300"),
    ({"summary": "Claude missed it."}, "AGENT_IDENTITY_IN_POST_MORTEM"),
    ({"sources": []}, "POST_MORTEM_SOURCES_REQUIRED"),
    ({"sources": [{"url": URL, "excerpt": EXCERPT, "published_at": AFTER}]},
     "KNOWABLE_BEFORE_MOVE_UNSUPPORTED"),
    ({"sources": [{"url": URL, "excerpt": EXCERPT, "published_at": "yesterday"}]},
     "RFC3339"),
    ({"sources": [{"url": URL, "excerpt": EXCERPT, "retrieved_at": BEFORE}]}, "only"),
    ({"factors": [{"kind": "TECHNICAL", "key": "VOLUME_SPIKE", "description": "x"}]},
     "CHECKLIST_FACTOR_INVALID"),
])
def test_what_the_app_or_the_kit_would_refuse(answers, code):
    problems = postmortem.check_subject(decide(worksheet()["subjects"][1], **answers),
                                        agent_id="claude")
    assert any(code in problem for problem in problems), problems


# --- The notes ----------------------------------------------------------------------------------

AGENT_BLOCK = {"agent_id": "claude", "agent_version": "kit-test-1",
               "guidelines_version": "MUSE_RESEARCH_GUIDELINES_V6", "guidelines_sha256": "0" * 64,
               "run_id": "7f1c2d3e-4b5a-4c6d-8e7f-a1b2c3d4e5f6"}


def verified_for(rows):
    return {path: {"source_id": f"s{n}", **source, "retrieved_at": NOW.isoformat()}
            for n, (path, source) in enumerate(
                (pair for row in rows for pair in postmortem.citations(row)), 1)}


def test_items_carry_exactly_the_contracts_fields_and_notes_hold_30():
    sheet = decide_all(worksheet())
    rows = sheet["subjects"]
    items = [postmortem.item(row, verified_for(rows)) for row in rows]
    assert set(items[1]) == {"subject", "cause", "knowable_before_move", "summary", "sources",
                             "pre_move_technicals"}
    assert items[1]["sources"][0]["source_id"] == "s1"
    note = postmortem.note(items, agent=AGENT_BLOCK, generated_at=NOW)
    assert set(note) == {"schema_version", "note_id", "generated_at", "agent", "items"}
    assert [len(batch) for batch in postmortem.batches(list(range(31)))] == [30, 1]


def test_the_notes_pass_the_apps_own_models_once_merged():
    intake = pytest.importorskip("catalyst_lab.learning_intake")
    rows = decide_all(worksheet())["subjects"]
    note = postmortem.note([postmortem.item(row, verified_for(rows)) for row in rows],
                           agent=AGENT_BLOCK, generated_at=NOW)
    intake.PostMortemEnvelope.model_validate(note)
    for item in note["items"]:
        intake.PostMortemItem.model_validate(item)
    assert postmortem.CAUSES == intake.CAUSES
    assert (postmortem.MAX_ITEMS, postmortem.MAX_BODY_BYTES) == (
        intake.MAX_POST_MORTEM_ITEMS, intake.POST_MORTEM_BODY_LIMIT)


# --- The commands --------------------------------------------------------------------------------

def _evening(tmp_path, found):
    run_dir = tmp_path / "evening"
    run_dir.mkdir()
    (run_dir / "context.json").write_text(json.dumps(
        context_v2(["BTC/USD", "ETH/USD", "SOL/USD"], lessons=found)))
    return run_dir


def _fetch_span(coins, *, start, end, now, **_kw):
    coin = coins[0]
    hours = int((end - start) / timedelta(hours=1))
    return {"retrieved_at": now.isoformat(), "excluded": {},
            "coinbase": {coin: {"candles_1h": hour_rows(start, closes_for(f"{coin}/USD", start,
                                                                          hours))}}}


def _token(tmp_path):
    path = tmp_path / "token"
    path.write_text(FAKE_TOKEN)
    path.chmod(0o600)
    return str(path)


def test_cli_postmortem_stops_until_the_day_is_recorded(tmp_path, monkeypatch, capsys):
    run_dir = _evening(tmp_path, lessons(recorded=(DAY - timedelta(days=1),)))
    monkeypatch.setattr(market_module, "fetch_hourly_span",
                        lambda *a, **k: pytest.fail("must not fetch"))
    code = run.main(["--run-dir", str(run_dir), "postmortem", "--day", "2026-09-28",
                     "--now", NOW.isoformat()])
    assert code == 2 and "REALITY_DAY_NOT_RECORDED" in capsys.readouterr().err
    assert not (run_dir / "postmortem.json").exists()


def test_cli_postmortem_check_and_submit(tmp_path, monkeypatch, capsys):
    run_dir = _evening(tmp_path, lessons())
    monkeypatch.setattr(market_module, "fetch_hourly_span", _fetch_span)
    base = ["--run-dir", str(run_dir)]
    # The default day is yesterday in New York: NOW is the morning after DAY.
    assert run.main([*base, "postmortem", "--now", NOW.isoformat()]) == 0
    assert "1 trades and 2 movers (1 misses) owed; 3 still to decide" in capsys.readouterr().out

    submit_args = [*base, "postmortem-submit", *AGENT, "--base-url", "https://app.example",
                   "--token-file", _token(tmp_path)]
    assert run.main(submit_args) == 2  # Nothing is sent while a subject is undecided.
    assert "3 subjects still to decide" in capsys.readouterr().err

    sheet = decide_all(json.loads((run_dir / "postmortem.json").read_text()))
    (run_dir / "postmortem.json").write_text(json.dumps(sheet))
    pages = {URL: news_page(EXCERPT, published=BEFORE)}
    monkeypatch.setattr(sources, "fetch_page", lambda url, **_kw: pages[url])
    assert run.main([*base, "postmortem-check", "--agent-id", "claude"]) == 0
    assert "ETH/USD: helper suggests knowable_before_move true" in capsys.readouterr().out
    helper = json.loads((run_dir / "postmortem.json").read_text())["subjects"][1]
    assert helper["knowable_helper"]["suggestion"] is True

    posted = []

    def fake_submit(base_url, tok, body, **_kw):
        posted.append((tok, body))
        results = [{"index": i, "status": "ACCEPTED", "subject_key": None}
                   for i in range(len(body["items"]))]
        results[-1] = {**results[-1], "status": "REJECTED", "code": "NOT_A_RECORDED_MOVER"}
        return submit_module.SubmitResult(202, {"status": "POST_MORTEM_RECORDED",
                                                "item_results": results}, "")

    monkeypatch.setattr(submit_module, "submit_post_mortem", fake_submit)
    assert run.main(submit_args) == 1  # One item refused.
    assert "NOT_A_RECORDED_MOVER" in capsys.readouterr().err
    token_seen, body = posted[0]
    assert token_seen == FAKE_TOKEN and len(body["items"]) == 3
    sent_at = datetime.fromisoformat(body["generated_at"])
    assert abs((datetime.now(UTC) - sent_at).total_seconds()) < 60  # Stamped at send time.
    assert body["items"][1]["sources"][0]["published_at"] == BEFORE
    assert body["items"][1]["sources"][0]["retrieved_at"]

    # A second send resends only the item that was not accepted.
    assert run.main(submit_args) == 1
    assert "2 subjects already accepted" in capsys.readouterr().out
    assert [item["subject"]["symbol"] for item in posted[1][1]["items"]] == ["BTC/USD"]
    history = json.loads((run_dir / "postmortem-submit.json").read_text())
    assert len(history["attempts"]) == 2
    for path in run_dir.iterdir():
        assert FAKE_TOKEN not in path.read_text()


def test_a_malformed_source_is_reported_not_a_crash():
    row = decide(worksheet()["subjects"][1], sources=["https://news.example/a"])
    assert postmortem.knowable_helper(row)["sources"][0]["position"] == "NO_PUBLISH_TIME"
    problems = postmortem.check_subject(row, agent_id="claude")
    assert any("{url, excerpt, published_at} only" in problem for problem in problems)


UNAVAILABLE = {"lessons_version": "RESEARCH_LESSONS_V1", "agent_id": "claude",
               "as_of": NOW.isoformat(), "available": False, "code": "LESSONS_UNAVAILABLE"}


def test_lessons_the_app_could_not_serve_mean_no_subjects_this_run(tmp_path, monkeypatch, capsys):
    """lessons.available false (package learning-app, 65b5e17): no pending subjects this
    run, a worksheet with none, and nothing to send; the owed ones stay pending."""
    run_dir = _evening(tmp_path, UNAVAILABLE)
    monkeypatch.setattr(market_module, "fetch_hourly_span",
                        lambda *a, **k: pytest.fail("must not fetch"))
    monkeypatch.setattr(submit_module, "submit_post_mortem",
                        lambda *a, **k: pytest.fail("must not post"))
    base = ["--run-dir", str(run_dir)]
    assert run.main([*base, "postmortem", "--now", NOW.isoformat()]) == 0
    assert "lessons unavailable this run (LESSONS_UNAVAILABLE)" in capsys.readouterr().out
    sheet = json.loads((run_dir / "postmortem.json").read_text())
    assert sheet["subjects"] == [] and "LESSONS_UNAVAILABLE" in sheet["reality"]["notes"][0]
    assert run.main([*base, "postmortem-submit", *AGENT, "--base-url", "https://app.example",
                     "--token-file", _token(tmp_path)]) == 0
    assert "nothing left to send" in capsys.readouterr().out


def test_unavailable_lessons_never_overwrite_a_worksheet_with_answers(tmp_path, capsys):
    run_dir = _evening(tmp_path, UNAVAILABLE)
    answered = decide_all(worksheet())
    (run_dir / "postmortem.json").write_text(json.dumps(answered))
    assert run.main(["--run-dir", str(run_dir), "postmortem", "--now", NOW.isoformat()]) == 0
    assert "kept" in capsys.readouterr().out
    assert json.loads((run_dir / "postmortem.json").read_text()) == answered


# --- Review fixes (2026-09-28) --------------------------------------------------------------------

def test_a_trade_with_no_recorded_exit_keeps_its_subject_and_blocks_nothing():
    """A CLOSED setup flattened outside the engine comes with exit_at and exit_price null.
    It stays a subject (its window ends when the lessons were read, no kit measure, a note);
    it neither refuses the worksheet nor crashes on a Decimal."""
    found = lessons_fixture(trades=[pending_trade(exit_at=None, exit_price=None)],
                            movers=[pending_mover("ETH/USD")])
    sheet = postmortem.build_worksheet(ctx=context_v2(["ETH/USD", "SOL/USD"], lessons=found),
                                       lessons=found, day=DAY, bars_for=bars_for, now=NOW)
    trade, mover = sheet["subjects"]
    facts = trade["facts"]
    assert facts["kit_measure"] is None and facts["window"]["end"] == "2026-09-29T13:00:00+00:00"
    assert any("no exit fill" in note for note in facts["notes"])
    assert facts["reference_at"] == "2026-09-28T10:17:00+00:00"  # The entry fill still counts.
    assert mover["facts"]["kit_measure"]["return_pct"] == "8.00"  # The other subject is whole.


def test_malformed_pending_fields_are_notes_never_a_whole_worksheet_refusal():
    found = lessons_fixture(
        trades=[pending_trade(entry_at=None, entry_price="n/a"),
                pending_trade("9f00c6d2-0a4e-4bd2-8a55-4d6f0b9d1a11", entry_price=None)],
        movers=[{**pending_mover("ETH/USD"), "day": "not-a-day", "move_start_at": None}])
    sheet = postmortem.build_worksheet(ctx=context_v2(["ETH/USD"], lessons=found),
                                       lessons=found, day=DAY, bars_for=bars_for, now=NOW)
    by_id = {row["subject_id"]: row for row in sheet["subjects"]}
    assert len(by_id) == 3
    no_entry = by_id["TRADE:0b5d9d44-1f55-4a5e-9f8a-2f3c7a1e9b10"]["facts"]
    assert no_entry["window"] is None and no_entry["reference_at"] is None
    assert by_id["TRADE:9f00c6d2-0a4e-4bd2-8a55-4d6f0b9d1a11"]["facts"]["kit_measure"] is None
    assert by_id["MOVER:not-a-day:ETH/USD"]["facts"]["window"] is None


def test_a_trade_with_no_notable_reason_is_optional_and_never_blocks_the_send(tmp_path,
                                                                             monkeypatch,
                                                                             capsys):
    optional_trade = pending_trade("5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f70812", notable_reasons=[])
    run_dir = _evening(tmp_path, lessons_fixture(trades=[optional_trade],
                                                 movers=[pending_mover("ETH/USD")]))
    monkeypatch.setattr(market_module, "fetch_hourly_span", _fetch_span)
    base = ["--run-dir", str(run_dir)]
    assert run.main([*base, "postmortem", "--now", NOW.isoformat()]) == 0
    sheet = json.loads((run_dir / "postmortem.json").read_text())
    assert [row["required"] for row in sheet["subjects"]] == [True, False]  # Mover, then trade.
    assert postmortem.undecided(sheet) == ["MOVER:2026-09-28:ETH/USD"]
    decide(sheet["subjects"][0])
    (run_dir / "postmortem.json").write_text(json.dumps(sheet))
    pages = {URL: news_page(EXCERPT, published=BEFORE)}
    monkeypatch.setattr(sources, "fetch_page", lambda url, **_kw: pages[url])
    posted = []

    def accept(base_url, tok, body, **_kw):
        posted.append(body)
        return submit_module.SubmitResult(202, {"item_results": [
            {"index": 0, "status": "ACCEPTED", "subject_key": "MOVER:2026-09-28:ETH/USD"}]}, "")

    monkeypatch.setattr(submit_module, "submit_post_mortem", accept)
    assert run.main([*base, "postmortem-submit", *AGENT, "--base-url", "https://app.example",
                     "--token-file", _token(tmp_path)]) == 0
    assert "1 optional trades (no notable reason) not decided" in capsys.readouterr().out
    assert len(posted[0]["items"]) == 1
    record = json.loads((run_dir / "postmortem-submit.json").read_text())
    item = record["attempts"][0]["notes"][0]["items"][0]
    assert record["schema"] == "RESEARCH_AGENT_POSTMORTEM_SUBMISSIONS_V1"
    assert (item["status"], item["knowable_before_move"]) == ("ACCEPTED", True)
    assert item["factors"][0]["key"] == "NETWORK_UPGRADE"  # Kept for the checklist.


@pytest.mark.parametrize("value", [1, 0, "true"])
def test_knowable_before_move_is_only_true_false_or_null(value):
    problems = postmortem.check_subject(decide(worksheet()["subjects"][1],
                                               knowable_before_move=value), agent_id="claude")
    assert any("decide true, false or null" in problem for problem in problems)
