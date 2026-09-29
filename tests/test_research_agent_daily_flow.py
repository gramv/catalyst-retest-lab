"""The daily procedure end to end through the CLI, in the order DAILY_PROCEDURE.md gives:
yesterday's evening (context, movers, postmortem, check, submit, checklist update), then
today's morning (context, lessons, checklist export, market, levels, outlook, check,
outlook-submit, build --lessons, validate, submit).

Offline only: every network boundary (the app's routes, Coinbase, news pages) is
monkeypatched; the checklist lives in a temporary owner-only folder.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from catalyst_lab.research_schedule import ResearchSchedule
from research_agent import context as context_module
from research_agent import market as market_module
from research_agent import run, sources
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
)

URL = "https://news.example/sol-upgrade"
EXCERPT = "The upgrade goes live on mainnet this week."
PUBLISHED = "2026-09-27T09:00:00Z"
MARKET_AT = NOW - timedelta(minutes=10)


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


def setup_rows():
    """80 hourly candles ending at MARKET_AT: the kit's rule-A scenario on 4-hour bars."""
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = 99.2, 99.7
    highs[10] = 130
    first = MARKET_AT.replace(minute=0) - timedelta(hours=80)
    first -= timedelta(hours=first.hour % 4)
    rows = []
    for bucket in range(20):
        for hour in range(4):
            t = int((first + timedelta(hours=bucket * 4 + hour)).timestamp())
            rows.append([t, str(lows[bucket]), str(highs[bucket]), str(lows[bucket]),
                         str(lows[bucket]), "1"])
    return rows


def fake_market(coins, **_kw):
    return {"retrieved_at": MARKET_AT.isoformat(), "excluded": {},
            "coinbase": {coin: {"quote_increment": "0.01", "candles_1h": setup_rows(),
                                "candles_1d": [], "ticker": {"bid": "100.09", "ask": "100.11",
                                                             "time": MARKET_AT.isoformat()}}
                         for coin in coins}}


def fake_span(coins, *, start, end, now, **_kw):
    hours = int((end - start) / timedelta(hours=1))
    closes = [100 if start + timedelta(hours=i) < DAY_START + timedelta(hours=9) else 109
              for i in range(hours)]
    return {"retrieved_at": now.isoformat(), "excluded": {},
            "coinbase": {coin: {"candles_1h": hour_rows(start, closes)} for coin in coins}}


def test_the_whole_day_runs_as_the_procedure_says(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    token_path = tmp_path / "token"
    token_path.write_text(FAKE_TOKEN)
    token_path.chmod(0o600)
    state = ["--state-dir", str(tmp_path / "muse-state")]
    app = ["--base-url", "https://app.example", "--token-file", str(token_path)]
    evening, morning = f"runs/{DAY.isoformat()}/evening", "runs/2026-09-29"
    found = lessons_fixture(movers=[pending_mover("SOL/USD", move_start_at="2026-09-28T13:00:00Z")])
    contexts = [context_v2(["SOL/USD", "ETH/USD"], lessons=found)] * 3
    monkeypatch.setattr(context_module, "fetch_context", lambda *a, **k: contexts.pop(0))
    monkeypatch.setattr(market_module, "fetch_hourly_span", fake_span)
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_market)
    monkeypatch.setattr(sources, "fetch_page",
                        lambda url, **_kw: news_page(EXCERPT, published=PUBLISHED))
    posted = []

    def accept(kind):
        def post(base_url, tok, body, **_kw):
            body = json.loads(body) if isinstance(body, bytes) else body  # The outlook: bytes.
            posted.append((kind, tok, body))
            items = [{"index": i, "status": "ACCEPTED"} for i in range(len(body.get("items", [])))]
            return submit_module.SubmitResult(202, {"status": "RECORDED", "item_results": items,
                                                    "outlook_id": body.get("outlook_id")}, "")
        return post

    for name in ("submit_report", "submit_outlook", "submit_post_mortem"):
        monkeypatch.setattr(submit_module, name, accept(name))

    def cli(*argv):
        assert run.main(list(argv)) == 0, capsys.readouterr()

    # --- Evening, for yesterday (the 07:15 combined order) ------------------------------------
    cli("context", *app, "--run-dir", evening)
    cli("movers", "--day", DAY.isoformat(), "--now", NOW.isoformat())  # Default: the evening dir.
    cli("postmortem", "--now", NOW.isoformat())  # Default day: yesterday in New York.
    sheet = json.loads((tmp_path / evening / "postmortem.json").read_text())
    subject = sheet["subjects"][0]
    subject.update(cause="COIN_NEWS", knowable_before_move=True,
                   summary="The mainnet upgrade was public the day before.",
                   sources=[{"url": URL, "excerpt": EXCERPT, "published_at": PUBLISHED}],
                   pre_move_technicals=subject["facts"]["pre_move_technicals_draft"],
                   factors=[{"kind": "EVENT", "key": "NETWORK_UPGRADE",
                             "description": "Scheduled network upgrades"}])
    (tmp_path / evening / "postmortem.json").write_text(json.dumps(sheet))
    cli("postmortem-check", "--agent-id", "claude", "--now", NOW.isoformat())
    cli("postmortem-submit", *AGENT, *app, "--now", NOW.isoformat())
    cli("checklist", "update", *state, "--from", f"{evening}/movers.json", "--from",
        f"{evening}/postmortem-submit.json", "--lessons", f"{evening}/context.json")

    # --- Morning, for today ---------------------------------------------------------------------
    cli("context", *app, "--run-dir", morning)
    cli("lessons", "--run-dir", morning, "--now", NOW.isoformat())
    cli("checklist", "export", *state, "--run-dir", morning, "--now", NOW.isoformat())
    cli("market", "--run-dir", morning)
    cli("levels", "--run-dir", morning)
    cli("outlook", "--run-dir", morning, "--now", NOW.isoformat())
    outlook_path = tmp_path / morning / "outlook.json"
    worksheet = json.loads(outlook_path.read_text())
    for entry in worksheet["coins"]:
        entry.update(direction="UP", confidence="0.6", reasons=[
            {"kind": "EVENT", "text": "A network upgrade this week.",
             "source": {"url": URL, "excerpt": EXCERPT, "published_at": PUBLISHED}}])
    worksheet["market"].update(summary="Quiet futures; Bitcoin steady.",
                               btc={"direction": "FLAT", "confidence": "0.5"},
                               eth={"direction": "FLAT", "confidence": "0.5"})
    outlook_path.write_text(json.dumps(worksheet))
    cli("outlook-check", "--run-dir", morning, *AGENT)
    cli("outlook-submit", "--run-dir", morning, *AGENT, *app)
    cli("build", "--run-dir", morning, *AGENT, "--lessons", f"{morning}/emphasis.json",
        "--now", NOW.isoformat())
    cli("validate", "--run-dir", morning, "--now", NOW.isoformat())
    cli("submit", "--run-dir", morning, *app)

    kinds = [kind for kind, _tok, _body in posted]
    assert kinds == ["submit_post_mortem", "submit_outlook", "submit_report"]
    assert all(tok == FAKE_TOKEN for _kind, tok, _body in posted)
    note, outlook_body, report = (body for _kind, _tok, body in posted)
    assert note["items"][0]["subject"] == {"kind": "MOVER", "symbol": "SOL/USD",
                                           "day": DAY.isoformat()}
    assert [coin["symbol"] for coin in outlook_body["coins"]] == ["SOL/USD", "ETH/USD"]
    assert outlook_body["agent"]["run_id"] == report["agent"]["run_id"]  # One research run.
    assert {pick["symbol"] for pick in report["picks"]} == {"SOL/USD", "ETH/USD"}
    exported = json.loads((tmp_path / morning / "checklist-export.json").read_text())
    assert exported["items"] == []  # One day of evidence activates nothing.
    for folder in (tmp_path / evening, tmp_path / morning):
        for path in folder.iterdir():
            assert FAKE_TOKEN not in path.read_text(), path


# --- The intraday run (DAILY_PROCEDURE.md, "Intraday run (every 2 hours)") ------------------

TWO_HOURLY = ResearchSchedule("America/New_York",
                              tuple(f"{hour:02d}:00" for hour in range(0, 24, 2)), 60)
SLOT_AT = datetime(2026, 9, 29, 14, 5, tzinfo=UTC)  # 10:05 New York: the 10:00 run.
INTRADAY_AGENT = ["--agent-id", "muse", "--agent-version", "claude-as-muse-intraday-v1-09.28"]


def intraday_rows(end):
    """Hourly candles up to ``end`` (on the hour) whose last 20 complete hours hold a 1-hour
    setup: entry 99.50, 0.5% below the fixture mid (100.00), under a window high of 130.
    The hour still forming at ``end`` is included, as Coinbase returns it."""
    lows, highs = [100] * 60, [101] * 60
    lows[45], highs[45] = 90, 90.5
    highs[50] = 130
    lows[55], highs[55] = 99.5, 100
    first = end - timedelta(hours=60)
    rows = [[int((first + timedelta(hours=hour)).timestamp()), str(lows[hour]),
             str(highs[hour]), str(lows[hour]), str(lows[hour]), "1"] for hour in range(60)]
    rows.append([int(end.timestamp()), "80", "150", "100", "100", "1"])  # Still forming.
    return rows


def test_an_intraday_run_answers_the_current_two_hour_slot(tmp_path, monkeypatch, capsys):
    """context, market, levels and build under INTRADAY_V2 (at most 10 picks, CHART-only),
    validate and submit, in the run's own folder: the report answers the 10:00 run, holds
    1-hour setups only, and nothing of the daily run (lessons, outlook) is made."""
    monkeypatch.chdir(tmp_path)
    token_path = tmp_path / "token"
    token_path.write_text(FAKE_TOKEN)
    token_path.chmod(0o600)
    app = ["--base-url", "https://app.example", "--token-file", str(token_path)]
    ctx = context_v2(["SOL/USD", "ETH/USD"], now=SLOT_AT)
    slot = TWO_HOURLY.latest_at_or_before(SLOT_AT)
    ctx["schedule"] = {**TWO_HOURLY.as_dict(),
                       "current_run_slot": TWO_HOURLY.local(slot).isoformat(),
                       "current_run_valid_until_limit":
                           TWO_HOURLY.local(TWO_HOURLY.validity_limit(slot)).isoformat(),
                       "next_runs": [TWO_HOURLY.local(run_at).isoformat()
                                     for run_at in TWO_HOURLY.next_runs(SLOT_AT)]}
    monkeypatch.setattr(context_module, "fetch_context", lambda *a, **k: ctx)
    retrieved_at = SLOT_AT.replace(minute=2)
    rows = intraday_rows(SLOT_AT.replace(minute=0))
    asked = []

    daily_rows = [[int((SLOT_AT - timedelta(days=day)).replace(hour=0, minute=0).timestamp()),
                   "95", "105", "100", "100", "1"] for day in range(1, 11)]

    def fake_fetch(coins, **kwargs):
        asked.append(kwargs)
        record = {"quote_increment": "0.01", "candles_1h": rows}
        if kwargs["days"]:  # Like Coinbase's own fetch: daily candles only when asked.
            record["candles_1d"] = daily_rows
        record["ticker"] = {"bid": "99.99", "ask": "100.01", "time": retrieved_at.isoformat()}
        return {"retrieved_at": retrieved_at.isoformat(), "excluded": {},
                "coinbase": {coin: record for coin in coins}}

    monkeypatch.setattr(market_module, "fetch_coinbase", fake_fetch)
    posted = []
    monkeypatch.setattr(submit_module, "submit_report", lambda base_url, tok, body, **_kw: (
        posted.append(body) or submit_module.SubmitResult(202, {"status": "RECORDED"}, "")))

    def cli(*argv):
        assert run.main(list(argv)) == 0, capsys.readouterr()

    folder = "runs/2026-09-29/intraday-1000"
    cli("context", *app, "--run-dir", folder)
    cli("market", "--profile", "intraday", "--run-dir", folder)
    cli("levels", "--profile", "intraday", "--run-dir", folder)
    cli("build", "--profile", "intraday", "--max-picks", "10", "--run-dir", folder,
        *INTRADAY_AGENT, "--now", SLOT_AT.isoformat())
    cli("validate", "--run-dir", folder, "--now", SLOT_AT.isoformat())
    cli("submit", "--run-dir", folder, *app)

    assert [(kw["hours"], kw["days"]) for kw in asked] == [(300, 60)]  # The daily inputs.
    [report] = posted
    assert report["run_slot"] == "2026-09-29T10:00:00-04:00"
    assert report["valid_until"] == "2026-09-29T13:00:00-04:00"  # The next run plus the grace.
    assert report["agent"]["agent_version"] == "claude-as-muse-intraday-v1-09.28"
    assert {pick["symbol"] for pick in report["picks"]} == {"SOL/USD", "ETH/USD"}
    assert {pick["technical_evidence"]["timeframe_seconds"] for pick in report["picks"]} == {3600}
    assert {pick["kind"] for pick in report["picks"]} == {"CHART"}
    for pick in report["picks"]:  # The still-forming hour is never cited.
        last = pick["technical_evidence"]["bars"][-1]["started_at"]
        assert datetime.fromisoformat(last) + timedelta(hours=1) <= retrieved_at
    run_dir = tmp_path / folder
    assert {row["profile"] for row in json.loads((run_dir / "levels.json").read_text()).values()
            } == {"INTRADAY_V2"}
    assert json.loads((run_dir / "build-notes.json").read_text())["profile"] == "INTRADAY_V2"
    for daily_only in ("emphasis.json", "checklist-export.json", "outlook.json", "news.json"):
        assert not (run_dir / daily_only).exists()
    for path in run_dir.iterdir():
        assert FAKE_TOKEN not in path.read_text(), path
