"""research_agent.outlook and the outlook commands: the morning worksheet, its checks, and
MARKET_OUTLOOK_V1 (package learning-kit; contract of package learning-app).

Offline only: fixture contexts and candles, news pages served by a monkeypatched
``sources.fetch_page``, and a monkeypatched POST. No real network access in this file.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from research_agent import context as context_module
from research_agent import levels, outlook, records, run, sources
from research_agent import submit as submit_module
from research_agent import token as token_module
from research_agent.market import Bar
from tests.learning_kit_fixtures import (
    AGENT,
    FAKE_TOKEN,
    NOW,
    SCHEDULE,
    context_v2,
    hour_rows,
    market_json,
    news_page,
)

D = Decimal
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
RETRIEVED_AT = NOW - timedelta(minutes=10)
URL = "https://news.example/listing"
PUBLISHED = "2026-09-20T09:00:00Z"  # Before the real clock the source checks read.
EXCERPT = "The exchange listed SOL for spot trading today."


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


def sol_setup():
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = 98.5, 99
    highs[10] = 130
    bars = [Bar(started_at=RETRIEVED_AT - timedelta(hours=4 * (20 - i)), open=D(str(lo)),
                high=D(str(hi)), low=D(str(lo)), close=D(str(lo)), volume=D(10))
            for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))]
    setup, tried = levels.find_setup({"4h": bars}, mid=D(100), increment=D("0.01"),
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is not None, tried
    return levels.setup_to_json(setup)


def inputs():
    start = RETRIEVED_AT.replace(minute=0) - timedelta(hours=200)
    flat = hour_rows(start, [100] * 200)
    spike = hour_rows(start, [100] * 200, volumes=[10] * 176 + [60] * 24)
    ctx = context_v2(SYMBOLS)
    market = market_json({"BTC": flat, "ETH": flat, "SOL": spike}, retrieved_at=RETRIEVED_AT)
    levels_doc = {"SOL": {"setup": sol_setup(), "tried": []},
                  "BTC": {"setup": None, "tried": ["4h/20 rule A: the window high is its most "
                                                   "recent bar"]}}
    news = {"coins": {"SOL": {"catalysts": [{"claim": "SOL listed.", "kind": "CATALYST",
                                             "source": {"url": URL, "excerpt": EXCERPT,
                                                        "published_at": PUBLISHED}}]}}}
    exported = {"use": "Order and emphasize checks only.", "items": [
        {"id": "TECHNICAL:VOLUME_SPIKE", "kind": "TECHNICAL", "key": "VOLUME_SPIKE",
         "description": "Coins with a volume spike", "value": "2.40"},
        {"id": "EVENT:TOKEN_UNLOCK", "kind": "EVENT", "key": "TOKEN_UNLOCK",
         "description": "Token unlocks", "value": "0.60"}]}
    return ctx, market, levels_doc, news, exported


def worksheet(previous=None):
    ctx, market, levels_doc, news, exported = inputs()
    return outlook.build_worksheet(ctx=ctx, market_data=market, levels_doc=levels_doc,
                                   news_doc=news, checklist_doc=exported, previous=previous,
                                   now=NOW)


def fill(sheet):
    """A complete set of answers: SOL up with a cited NEWS reason, BTC flat, ETH skipped."""
    answers = {
        "SOL/USD": {"direction": "UP", "confidence": "0.65", "expected_move_pct": "4.5",
                    "reasons": [{"kind": "NEWS", "text": "A new spot listing.",
                                 "source": {"url": URL, "excerpt": EXCERPT,
                                            "published_at": PUBLISHED}},
                                {"kind": "TECHNICAL", "text": "Volume at 2x its average."}]},
        "BTC/USD": {"direction": "FLAT", "confidence": 0.55, "reasons": []},
        "ETH/USD": {"direction": "SKIPPED", "skip_reason": "No read on it this morning."},
    }
    for entry in sheet["coins"]:
        entry.update(answers[entry["symbol"]])
    sheet["market"].update({
        "summary": "Bitcoin steady; the stock futures are flat before the open.",
        "btc": {"direction": "FLAT", "confidence": "0.6"},
        "eth": {"direction": "UP", "confidence": "0.55"},
        "factors": [{"name": "Nasdaq futures", "note": "Flat before the open."}],
        "events": [{"at": None, "what": "A central bank speech this afternoon."}],
    })
    return sheet


# --- The worksheet ---------------------------------------------------------------------------

def test_the_worksheet_has_one_entry_per_universe_coin_with_the_kits_facts():
    sheet = worksheet()
    assert sheet["schema"] == outlook.WORKSHEET_SCHEMA and sheet["contract"] == "MARKET_OUTLOOK_V1"
    assert [entry["symbol"] for entry in sheet["coins"]] == SYMBOLS
    sol = next(entry for entry in sheet["coins"] if entry["symbol"] == "SOL/USD")
    assert (sol["direction"], sol["confidence"], sol["reasons"]) == (None, None, [])
    facts = sol["facts"]
    assert facts["mid"] == "100" and facts["setup"]["entry"] == "98.5"
    assert facts["setup"]["distance_to_entry_pct"] == "1.50"
    assert facts["setup"]["distance_bucket"] == "1-2%"
    assert "VOLUME_SPIKE" in facts["technical"]["tags"]
    assert facts["news"][0]["url"] == URL
    assert facts["checklist_matches"] == [{"id": "TECHNICAL:VOLUME_SPIKE",
                                           "description": "Coins with a volume spike",
                                           "value": "2.40"}]
    btc = next(entry for entry in sheet["coins"] if entry["symbol"] == "BTC/USD")
    assert btc["facts"]["setup"] is None and "most recent bar" in btc["facts"]["setup_note"]
    assert sheet["checklist"]["check_for_every_coin"][0]["id"] == "EVENT:TOKEN_UNLOCK"
    assert sheet["market"]["facts"]["btc"]["tags"]


def test_rebuilding_keeps_every_answer_and_follows_the_universe():
    first = fill(worksheet())
    ctx, market, levels_doc, news, exported = inputs()
    ctx = context_v2(["BTC/USD", "SOL/USD", "XRP/USD"])  # ETH left, XRP arrived.
    again = outlook.build_worksheet(ctx=ctx, market_data=market, levels_doc=levels_doc,
                                    news_doc=news, checklist_doc=exported, previous=first,
                                    now=NOW)
    by_symbol = {entry["symbol"]: entry for entry in again["coins"]}
    assert by_symbol["SOL/USD"]["confidence"] == "0.65"
    assert by_symbol["XRP/USD"]["direction"] is None
    assert again["dropped_since_last_build"] == ["ETH/USD"]
    assert again["outlook_id"] == first["outlook_id"]
    assert again["market"]["summary"] == first["market"]["summary"]


# --- Checks -------------------------------------------------------------------------------------

def test_a_filled_worksheet_passes():
    assert outlook.check_worksheet(fill(worksheet()), universe=SYMBOLS, agent_id="claude") == (
        [], [])


def test_an_unfilled_coin_refuses_the_whole_outlook():
    sheet = fill(worksheet())
    sheet["coins"][0]["direction"] = None
    problems, _ = outlook.check_worksheet(sheet, universe=SYMBOLS, agent_id="claude")
    assert any("UNFILLED (1): BTC/USD" in problem for problem in problems)


@pytest.mark.parametrize("change, code", [
    ({"direction": "SKIPPED", "skip_reason": None, "confidence": None, "reasons": [],
      "expected_move_pct": None}, "SKIP_REASON_REQUIRED"),
    ({"direction": "SKIPPED", "skip_reason": "x", "confidence": "0.5", "reasons": [],
      "expected_move_pct": None}, "SKIPPED_COIN_FIELDS_NOT_ALLOWED"),
    ({"confidence": "1.5"}, "CONFIDENCE_REQUIRED"),
    ({"confidence": "0.12345678901"}, "CONFIDENCE_REQUIRED"),  # 11 places.
    ({"expected_move_pct": "4.12345"}, "expected_move_pct"),
    ({"skip_reason": "not skipped"}, "SKIP_REASON_NOT_ALLOWED"),
    ({"reasons": [{"kind": "NEWS", "text": "Uncited news."}]}, "SOURCE_REQUIRED"),
    ({"reasons": [{"kind": "EVENT", "text": "An unlock on Oct. 3."}]}, "SOURCE_REQUIRED"),
    ({"reasons": [{"kind": "GOSSIP", "text": "x"}]}, "one of"),
    ({"reasons": [{"kind": "TECHNICAL", "text": "x" * 201}]}, "1-200 characters"),
    ({"reasons": [{"kind": "TECHNICAL", "text": "Claude likes it."}]}, "AGENT_IDENTITY"),
    ({"reasons": [{"kind": "NEWS", "text": "x", "source": {"url": URL, "excerpt": "e",
                                                           "retrieved_at": "now"}}]},
     "SOURCE_INVALID"),
])
def test_coin_answers_the_app_or_the_kit_would_refuse(change, code):
    sheet = fill(worksheet())
    sheet["coins"][2].update(change)  # SOL/USD
    problems, _ = outlook.check_worksheet(sheet, universe=SYMBOLS, agent_id="claude")
    assert any(code in problem for problem in problems), problems


def test_market_answers_are_checked():
    sheet = fill(worksheet())
    sheet["market"].update({"summary": "", "btc": {"direction": "SKIPPED", "confidence": "0.5"},
                            "factors": [{"name": "n" * 81, "note": "x"}] * 13})
    problems, _ = outlook.check_worksheet(sheet, universe=SYMBOLS, agent_id="claude")
    joined = " ".join(problems)
    assert "market.summary" in joined and "market.btc.direction" in joined
    assert "at most 12 factors" in joined


def test_the_universe_the_outlook_is_sent_for_decides_coverage():
    sheet = fill(worksheet())
    problems, notes = outlook.check_worksheet(sheet, universe=["BTC/USD", "SOL/USD", "XRP/USD"],
                                              agent_id="claude")
    assert any("OUTLOOK_UNIVERSE_CHANGED: no entry for XRP/USD" in p for p in problems)
    assert notes == ["left the tradable universe, not sent: ETH/USD"]
    too_many = [f"C{i:03}/USD" for i in range(outlook.MAX_COINS + 1)]
    problems, _ = outlook.check_worksheet(sheet, universe=too_many, agent_id="claude")
    assert any("OUTLOOK_TOO_MANY_COINS" in problem for problem in problems)


# --- The payload ---------------------------------------------------------------------------------

AGENT_BLOCK = {"agent_id": "claude", "agent_version": "kit-test-1",
               "guidelines_version": "MUSE_RESEARCH_GUIDELINES_V6", "guidelines_sha256": "0" * 64,
               "run_id": "7f1c2d3e-4b5a-4c6d-8e7f-a1b2c3d4e5f6"}


def payload(sheet, *, universe=SYMBOLS):
    cited = outlook.citations(sheet, universe)
    verified = {path: {"source_id": f"s{n}", **source, "retrieved_at": NOW.isoformat()}
                for n, (path, source) in enumerate(cited, 1)}
    return outlook.assemble(sheet, universe=universe, verified=verified, agent=AGENT_BLOCK,
                            run_slot="2026-09-29T08:00:00-04:00", generated_at=NOW)


def test_the_payload_is_the_contracts_shape_and_nothing_else():
    body = payload(fill(worksheet()))
    assert set(body) == {"schema_version", "outlook_id", "generated_at", "run_slot",
                         "horizon_hours", "agent", "market", "coins"}
    assert body["horizon_hours"] == 24 and body["schema_version"] == "MARKET_OUTLOOK_V1"
    by_symbol = {coin["symbol"]: coin for coin in body["coins"]}
    assert by_symbol["ETH/USD"] == {"symbol": "ETH/USD", "direction": "SKIPPED",
                                    "skip_reason": "No read on it this morning."}
    assert by_symbol["BTC/USD"] == {"symbol": "BTC/USD", "direction": "FLAT",
                                    "confidence": "0.55", "reasons": []}
    sol = by_symbol["SOL/USD"]
    assert (sol["confidence"], sol["expected_move_pct"]) == ("0.65", "4.5")
    assert sol["reasons"][0]["source"]["source_id"] == "s1"
    assert "source" not in sol["reasons"][1]
    assert body["market"]["events"] == [{"at": None,
                                         "what": "A central bank speech this afternoon."}]
    assert not any("facts" in coin for coin in body["coins"])  # The kit's facts stay home.


def test_the_payload_passes_the_apps_own_model_once_merged():
    intake = pytest.importorskip("catalyst_lab.learning_intake")
    intake.MarketOutlook.model_validate(payload(fill(worksheet())))


def test_the_pinned_limits_match_the_apps_once_merged():
    intake = pytest.importorskip("catalyst_lab.learning_intake")
    assert outlook.MAX_COINS == intake.MAX_OUTLOOK_COINS
    assert outlook.MAX_BODY_BYTES == intake.OUTLOOK_BODY_LIMIT
    assert outlook.REASON_KINDS == intake.REASON_KINDS
    assert outlook.DIRECTIONS == intake.DIRECTIONS


# --- The commands --------------------------------------------------------------------------------

def _run_folder(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx, market, levels_doc, news, exported = inputs()
    for name, doc in (("context.json", ctx), ("market.json", market), ("levels.json", levels_doc),
                      ("news.json", news), ("checklist-export.json", exported)):
        (run_dir / name).write_text(json.dumps(doc))
    return run_dir


def _serve(monkeypatch, pages):
    def fetch_page(url, **_kw):
        if url not in pages:
            raise sources.SourceError("SOURCE_HTTP_404")
        return pages[url]
    monkeypatch.setattr(sources, "fetch_page", fetch_page)


def _token(tmp_path):
    path = tmp_path / "token"
    path.write_text(FAKE_TOKEN)
    path.chmod(0o600)
    return str(path)


def test_cli_outlook_then_check_then_submit(tmp_path, monkeypatch, capsys):
    run_dir = _run_folder(tmp_path)
    assert run.main(["--run-dir", str(run_dir), "outlook", "--now", NOW.isoformat()]) == 0
    sheet = fill(json.loads((run_dir / "outlook.json").read_text()))
    (run_dir / "outlook.json").write_text(json.dumps(sheet))
    _serve(monkeypatch, {URL: news_page(EXCERPT, published=PUBLISHED)})

    assert run.main(["--run-dir", str(run_dir), "outlook-check", *AGENT]) == 0
    checked = json.loads((run_dir / "outlook-check.json").read_text())
    assert checked["problems"] == [] and checked["payload"]["coins"]

    fresh = context_v2(SYMBOLS)
    monkeypatch.setattr(context_module, "fetch_context", lambda base_url, tok, **kw: fresh)
    sent = {}

    def fake_submit(base_url, tok, body, **_kw):
        sent.update(token=tok, raw=body, body=json.loads(body))
        return submit_module.SubmitResult(202, {
            "status": "MARKET_OUTLOOK_RECORDED", "outlook_id": sent["body"]["outlook_id"],
            "coin_count": 3, "skipped_count": 1, "window_start": "a", "window_end": "b",
            "grading_day": "2026-09-30", "idempotent_replay": False}, "")

    monkeypatch.setattr(submit_module, "submit_outlook", fake_submit)
    code = run.main(["--run-dir", str(run_dir), "outlook-submit", *AGENT, "--base-url",
                     "https://app.example", "--token-file", _token(tmp_path)])
    assert code == 0
    assert "MARKET_OUTLOOK_RECORDED" in capsys.readouterr().out
    assert sent["token"] == FAKE_TOKEN
    body = sent["body"]
    assert (run_dir / "outlook-sent.json").read_bytes() == sent["raw"]  # Exactly what was sent.
    assert json.loads((run_dir / "outlook-submit.json").read_text())["state"] == "RECORDED"
    assert body["run_slot"] == fresh["schedule"]["current_run_slot"]
    assert body["agent"]["guidelines_sha256"] == fresh["report_format"]["guidelines_sha256"]
    source = body["coins"][2]["reasons"][0]["source"]
    assert source["published_at"] == PUBLISHED and source["retrieved_at"]
    for path in run_dir.iterdir():
        assert FAKE_TOKEN not in path.read_text()


def test_cli_outlook_submit_refuses_before_posting(tmp_path, monkeypatch, capsys):
    run_dir = _run_folder(tmp_path)
    assert run.main(["--run-dir", str(run_dir), "outlook", "--now", NOW.isoformat()]) == 0
    sheet = fill(json.loads((run_dir / "outlook.json").read_text()))
    (run_dir / "outlook.json").write_text(json.dumps(sheet))
    monkeypatch.setattr(submit_module, "submit_outlook",
                        lambda *a, **k: pytest.fail("must not post"))
    base = ["--run-dir", str(run_dir), "outlook-submit", *AGENT, "--base-url",
            "https://app.example", "--token-file", _token(tmp_path)]

    # A coin joined the universe since the worksheet was built. The context read now is
    # saved, so 'outlook' adds the coin (keeping every answer) for the session to fill.
    monkeypatch.setattr(context_module, "fetch_context",
                        lambda *a, **k: context_v2([*SYMBOLS, "XRP/USD"]))
    assert run.main(base) == 2
    assert "OUTLOOK_UNIVERSE_CHANGED: no entry for XRP/USD" in capsys.readouterr().err
    assert run.main(["--run-dir", str(run_dir), "outlook", "--now", NOW.isoformat()]) == 0
    rebuilt = {entry["symbol"]: entry for entry in
               json.loads((run_dir / "outlook.json").read_text())["coins"]}
    assert rebuilt["XRP/USD"]["direction"] is None and rebuilt["SOL/USD"]["direction"] == "UP"

    # A published_at the page's own metadata does not give.
    monkeypatch.setattr(context_module, "fetch_context", lambda *a, **k: context_v2(SYMBOLS))
    _serve(monkeypatch, {URL: news_page(EXCERPT, published="2026-09-20T07:00:00Z")})
    assert run.main(base) == 2
    assert "PUBLISHED_AT_NOT_FROM_PAGE_METADATA" in capsys.readouterr().err
    assert not (run_dir / "outlook-sent.json").exists()


def _filled_folder(tmp_path, monkeypatch):
    run_dir = _run_folder(tmp_path)
    assert run.main(["--run-dir", str(run_dir), "outlook", "--now", NOW.isoformat()]) == 0
    sheet = fill(json.loads((run_dir / "outlook.json").read_text()))
    (run_dir / "outlook.json").write_text(json.dumps(sheet))
    _serve(monkeypatch, {URL: news_page(EXCERPT, published=PUBLISHED)})
    monkeypatch.setattr(context_module, "fetch_context", lambda *a, **k: context_v2(SYMBOLS))
    base = ["--run-dir", str(run_dir), "outlook-submit", *AGENT, "--base-url",
            "https://app.example", "--token-file", _token(tmp_path)]
    return run_dir, sheet, base


def _answers(monkeypatch, *answers):
    """Each POST gets the next answer: a SubmitResult, or an exception to raise."""
    posted, queue = [], list(answers)

    def post(base_url, tok, body, **_kw):
        posted.append(body)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(submit_module, "submit_outlook", post)
    return posted


def _state(run_dir):
    return json.loads((run_dir / "outlook-submit.json").read_text())["state"]


RECEIPT = submit_module.SubmitResult(202, {"status": "MARKET_OUTLOOK_RECORDED",
                                           "idempotent_replay": True}, "")


def test_a_send_with_no_answer_is_resent_byte_for_byte_and_recorded_once(tmp_path, monkeypatch,
                                                                         capsys):
    run_dir, _sheet, base = _filled_folder(tmp_path, monkeypatch)
    posted = _answers(monkeypatch, submit_module.SubmitError("CONNECTION_ERROR: ReadTimeout"),
                      RECEIPT)
    assert run.main(base) == 1  # The reply was lost: the outcome is unknown.
    assert _state(run_dir) == "UNKNOWN"  # Written for a transport error too.
    saved = (run_dir / "outlook-sent.json").read_bytes()
    assert posted[0] == saved

    # The resend is the same bytes: nothing is rebuilt, fetched or re-verified.
    monkeypatch.setattr(context_module, "fetch_context",
                        lambda *a, **k: pytest.fail("must not rebuild"))
    monkeypatch.setattr(sources, "fetch_page", lambda *a, **k: pytest.fail("must not refetch"))
    assert run.main(base) == 0
    assert posted[1] == saved and "a replay" in capsys.readouterr().out
    assert _state(run_dir) == "RECORDED"

    # Recorded: never sent again by accident.
    assert run.main(base) == 0
    assert len(posted) == 2 and "already recorded" in capsys.readouterr().out


def test_a_resend_found_stale_proves_the_first_never_landed_and_rebuilds_same_id(tmp_path,
                                                                                monkeypatch):
    run_dir, sheet, base = _filled_folder(tmp_path, monkeypatch)
    stale = submit_module.SubmitResult(422, {"detail": "MARKET_OUTLOOK_STALE_OR_FUTURE"}, "")
    posted = _answers(monkeypatch, submit_module.SubmitError("CONNECTION_ERROR: ConnectError"),
                      stale, RECEIPT)
    assert run.main(base) == 1
    assert run.main(base) == 0
    first, resent, rebuilt = (json.loads(body) for body in posted)
    assert first == resent  # The resend was the saved body.
    assert rebuilt["outlook_id"] == first["outlook_id"] == sheet["outlook_id"]
    assert rebuilt["generated_at"] > first["generated_at"]  # Stamped again for the new send.
    assert _state(run_dir) == "RECORDED"


def test_an_idempotency_refusal_means_already_recorded_and_new_id_is_deliberate(tmp_path,
                                                                               monkeypatch,
                                                                               capsys):
    run_dir, sheet, base = _filled_folder(tmp_path, monkeypatch)
    taken = submit_module.SubmitResult(422, {"detail": "OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH"},
                                       "")
    posted = _answers(monkeypatch, taken, RECEIPT)
    assert run.main(base) == 1
    err = capsys.readouterr().err
    assert "already recorded" in err and "--new-id" not in err  # No advice to send twice.
    assert _state(run_dir) == "RECORDED"
    assert run.main(base) == 0 and len(posted) == 1  # Nothing sent again.
    assert run.main([*base, "--new-id"]) == 0
    assert "a second outlook" in capsys.readouterr().err
    assert json.loads(posted[1])["outlook_id"] != sheet["outlook_id"]


def test_a_refused_outlook_is_fixed_and_sent_again_under_the_same_id(tmp_path, monkeypatch):
    run_dir, sheet, base = _filled_folder(tmp_path, monkeypatch)
    refused = submit_module.SubmitResult(422, {"detail": "INVALID_MARKET_OUTLOOK"}, "")
    posted = _answers(monkeypatch, refused, RECEIPT)
    assert run.main(base) == 1 and _state(run_dir) == "REFUSED"
    assert run.main(base) == 0 and _state(run_dir) == "RECORDED"
    assert json.loads(posted[1])["outlook_id"] == sheet["outlook_id"]


def test_a_coin_row_that_is_not_an_object_is_a_problem_not_a_crash():
    sheet = fill(worksheet())
    sheet["coins"][1] = "ETH/USD UP"
    problems, _ = outlook.check_worksheet(sheet, universe=SYMBOLS, agent_id="claude")
    assert problems == ["coins[1]: must be an object"]


def test_an_outlook_sent_at_0715_answers_that_mornings_run():
    """The same rule as reports (build.run_slot_for): from 07:00, inside the 60-minute grace,
    the outlook declares today's 08:00 run, not yesterday's."""
    ctx = context_v2(SYMBOLS)
    ctx["schedule"] = {**SCHEDULE.as_dict(), **ctx["schedule"]}
    at = datetime(2026, 9, 29, 11, 15, tzinfo=UTC)  # 07:15 in New York.
    assert records.run_slot(ctx, at) == "2026-09-29T08:00:00-04:00"
    assert records.run_slot(ctx, at - timedelta(minutes=30)) == "2026-09-28T08:00:00-04:00"
