"""research_agent.run: the CLI, end to end through a temp run-dir.

Network calls (context.fetch_context, market.fetch_coinbase, submit.submit_report) are
monkeypatched at the module level; no real network access anywhere in this file. The
market fixture reproduces the hand-verified rule-A scenario from the other test files,
this time from raw hourly Coinbase candles, so this file also exercises the real
aggregation path (market.fetch_coinbase -> market.all_series -> levels.find_setup).
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_V3_SHA256, MUSE_GUIDELINES_V3_VERSION
from catalyst_lab.research_schedule import ResearchSchedule
from research_agent import build, run
from research_agent import context as context_module
from research_agent import market as market_module
from research_agent import submit as submit_module
from research_agent import token as token_module

NOW = datetime(2026, 9, 27, 12, 30, 0, tzinfo=UTC)
SCHEDULE = ResearchSchedule("America/New_York", ("08:00",), 60)
T0 = int(datetime(2026, 9, 24, 0, 0, tzinfo=UTC).timestamp())
HOUR = 3600
MARKET_RETRIEVED_AT = datetime.fromtimestamp(T0 + 80 * HOUR, UTC)


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    # Never let an ambient developer environment variable leak into these tests.
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


def hourly_rows_for_setup():
    """80 one-hour candles (20 four-hour buckets) reproducing the hand-verified rule-A
    scenario used across the other test files: a deep low (bucket 5), a held entry
    pivot 5% below mid (bucket 15), and a window peak that is not the most recent bar
    (bucket 10)."""
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = 95, 95.5
    highs[10] = 130
    rows = []
    for bucket in range(20):
        for hour in range(4):
            t = T0 + (bucket * 4 + hour) * HOUR
            rows.append([t, str(lows[bucket]), str(highs[bucket]), str(lows[bucket]),
                        str(lows[bucket]), "1"])
    return rows


def fake_market_data(_coins=None, **_ignored):
    return {
        "retrieved_at": MARKET_RETRIEVED_AT.isoformat(),
        "coinbase": {"SOL": {
            "quote_increment": "0.01", "candles_1h": hourly_rows_for_setup(),
            "candles_1d": [[T0 - 86_400, "100", "101", "100", "100", "1"]],
            "ticker": {"bid": "100.09", "ask": "100.11", "time": MARKET_RETRIEVED_AT.isoformat()},
        }},
        "excluded": {},
    }


def fake_context(*_args, now=NOW, **_kwargs):
    slot = SCHEDULE.latest_at_or_before(now)
    quote_at = (now - timedelta(seconds=30)).isoformat()
    return {
        "context_version": "RESEARCH_CONTEXT_V1", "as_of": now.isoformat(),
        "schedule": {"current_run_slot": SCHEDULE.local(slot).isoformat(),
                    "current_run_valid_until_limit":
                        SCHEDULE.local(SCHEDULE.validity_limit(slot)).isoformat()},
        "report_format": {"guidelines_version": MUSE_GUIDELINES_V3_VERSION,
                          "guidelines_sha256": MUSE_GUIDELINES_V3_SHA256},
        "universe": {"count": 1, "coins": [
            {"symbol": "SOL/USD", "bid": "100.09", "ask": "100.11", "quote_at": quote_at,
             "price_increment": "0.01", "min_order_size": "0.01", "quantity_increment": "0.01"},
        ], "excluded": {}, "issues": []},
        "open_trades": [], "pending_reviews": [], "recent_outcomes": {}, "trade_authorized": False,
    }


# --- The deterministic steps, one at a time, through a real temp run-dir ------------------

def test_cli_full_pipeline_context_through_validate(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    token_path = tmp_path / "token.txt"
    token_path.write_text("fixture-cli-token-never-written-anywhere")
    token_path.chmod(0o600)  # The kit reads owner-only files only.

    monkeypatch.setattr(context_module, "fetch_context",
                        lambda base_url, tok, **kw: fake_context())
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_market_data)

    assert run.main(["--run-dir", str(run_dir), "context", "--base-url", "https://app.example",
                     "--token-file", str(token_path)]) == 0
    assert run.main(["--run-dir", str(run_dir), "market"]) == 0
    assert run.main(["--run-dir", str(run_dir), "levels"]) == 0
    assert run.main(["--run-dir", str(run_dir), "build", "--agent-id", "claude",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()]) == 0
    assert run.main(["--run-dir", str(run_dir), "validate", "--now", NOW.isoformat()]) == 0

    report = json.loads((run_dir / "report.json").read_text())
    assert len(report["picks"]) == 1 and report["picks"][0]["symbol"] == "SOL/USD"
    assert report["picks"][0]["kind"] == "CHART"
    validated = json.loads((run_dir / "validate.json").read_text())
    assert validated["rejected"] == [] and validated["accepted"] == ["SOL/USD"]

    for path in run_dir.iterdir():  # The fixture token must never end up in any written file.
        assert "fixture-cli-token" not in path.read_text()


def test_cli_all_runs_the_deterministic_steps_but_never_submits(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    token_path = tmp_path / "token.txt"
    token_path.write_text("fixture-cli-token")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    monkeypatch.setattr(context_module, "fetch_context",
                        lambda base_url, tok, **kw: fake_context())
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_market_data)
    submitted = []
    monkeypatch.setattr(submit_module, "submit_report",
                        lambda *a, **k: submitted.append(1))

    code = run.main(["--run-dir", str(run_dir), "all", "--base-url", "https://app.example",
                     "--token-file", str(token_path), "--agent-id", "claude",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()])
    assert code == 0
    assert not submitted  # 'all' is the deterministic steps only, never submit.
    assert not (run_dir / "submit.json").exists()
    assert (run_dir / "report.json").exists() and (run_dir / "validate.json").exists()


# --- --profile: DAILY_V1 by default, INTRADAY_V1 for the 2-hourly runs ---------------------

def intraday_hourly_rows():
    """80 one-hour candles whose last 20 hold a 1-hour setup: its entry (99.60) 0.5% below
    the fixture mid (100.10), inside INTRADAY_V1's 0.3%-3% band and too close for DAILY_V1's
    0.6% minimum on every timeframe; the window low 90 (hour 65) and high 130 (hour 70)."""
    lows, highs = [100] * 80, [101] * 80
    lows[65], highs[65] = 90, 90.5
    highs[70] = 130
    lows[75], highs[75] = 99.6, 100.1
    return [[T0 + hour * HOUR, str(lows[hour]), str(highs[hour]), str(lows[hour]),
             str(lows[hour]), "1"] for hour in range(80)]


def recording_market(calls, rows):
    """fetch_coinbase's fake: records what it was asked for and, like the real one, leaves
    the daily candles out when asked for none."""
    def fetch(coins, **kwargs):
        calls.append(kwargs)
        record = {"quote_increment": "0.01", "candles_1h": rows}
        if kwargs.get("days", market_module.DAILY_LOOKBACK_DAYS):
            record["candles_1d"] = [[T0 - 86_400, "100", "101", "100", "100", "1"]]
        record["ticker"] = {"bid": "100.09", "ask": "100.11",
                            "time": MARKET_RETRIEVED_AT.isoformat()}
        return {"retrieved_at": MARKET_RETRIEVED_AT.isoformat(), "coinbase": {"SOL": record},
                "excluded": {}}
    return fetch


def _cli_context(run_dir, tmp_path, monkeypatch):
    token_path = tmp_path / "token.txt"
    token_path.write_text("fixture-cli-token")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    monkeypatch.setattr(context_module, "fetch_context",
                        lambda base_url, tok, **kw: fake_context())
    assert run.main(["--run-dir", str(run_dir), "context", "--base-url", "https://app.example",
                     "--token-file", str(token_path)]) == 0
    return token_path


def test_cli_profile_defaults_to_daily_on_market_levels_build_and_all():
    parser = run.build_parser()
    agent = ["--agent-id", "claude", "--agent-version", "test-1.0"]
    for argv in (["market"], ["levels"], ["build", *agent], ["all", *agent]):
        assert parser.parse_args(argv).profile == "daily"
        for key in ("intraday", "intraday-v1"):
            assert parser.parse_args([*argv, "--profile", key]).profile == key
        with pytest.raises(SystemExit):
            parser.parse_args([*argv, "--profile", "hourly"])


def test_cli_intraday_run_uses_intraday_v2_on_the_daily_market_data(tmp_path, monkeypatch):
    """--profile intraday is INTRADAY_V2: the daily profile's fetch (300 hours of 1-hour
    candles, 60 days of daily candles) and the 1-hour setup DAILY_V1 cannot take (0.5%)."""
    run_dir = tmp_path / "intraday-1000"
    calls = []
    _cli_context(run_dir, tmp_path, monkeypatch)
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        recording_market(calls, intraday_hourly_rows()))
    base = ["--run-dir", str(run_dir)]
    assert run.main([*base, "market", "--profile", "intraday"]) == 0
    assert (calls[0]["hours"], calls[0]["days"]) == (300, 60)
    market_json = json.loads((run_dir / "market.json").read_text())
    assert market_json["profile"] == "INTRADAY_V2"
    assert "candles_1d" in market_json["coinbase"]["SOL"]

    assert run.main([*base, "levels", "--profile", "intraday"]) == 0
    row = json.loads((run_dir / "levels.json").read_text())["SOL"]
    assert row["profile"] == "INTRADAY_V2" and row["tried"] == []
    assert (row["setup"]["timeframe"], row["setup"]["window"], row["setup"]["rule"]) == (
        "1h", 20, "A")
    assert Decimal(row["setup"]["entry"]) == Decimal("99.60")  # 0.5% below the 100.10 mid.

    assert run.main([*base, "build", "--profile", "intraday", "--max-picks", "10",
                     "--agent-id", "muse", "--agent-version",
                     "claude-as-muse-intraday-v1-09.28", "--now", NOW.isoformat()]) == 0
    notes = json.loads((run_dir / "build-notes.json").read_text())
    assert notes["profile"] == "INTRADAY_V2" and notes["accepted"] == ["SOL/USD"]
    [pick] = json.loads((run_dir / "report.json").read_text())["picks"]
    assert pick["technical_evidence"]["timeframe_seconds"] == 3600
    assert run.main([*base, "validate", "--now", NOW.isoformat()]) == 0


def test_cli_intraday_v1_run_fetches_finds_and_records_under_intraday_v1(tmp_path, monkeypatch):
    """--profile intraday-v1 keeps INTRADAY_V1 exactly: a week of 1-hour candles, no daily
    call, 1h/2h/4h with entries 0.3%-3% below the mid, recorded as INTRADAY_V1."""
    run_dir = tmp_path / "intraday-1000"
    calls = []
    _cli_context(run_dir, tmp_path, monkeypatch)
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        recording_market(calls, intraday_hourly_rows()))
    base = ["--run-dir", str(run_dir)]
    assert run.main([*base, "market", "--profile", "intraday-v1"]) == 0
    assert (calls[0]["hours"], calls[0]["days"]) == (168, 0)  # A week of 1h; no daily call.
    market_json = json.loads((run_dir / "market.json").read_text())
    assert market_json["profile"] == "INTRADAY_V1"
    assert "candles_1d" not in market_json["coinbase"]["SOL"]

    assert run.main([*base, "levels", "--profile", "intraday-v1"]) == 0
    row = json.loads((run_dir / "levels.json").read_text())["SOL"]
    assert row["profile"] == "INTRADAY_V1" and row["tried"] == []
    assert (row["setup"]["timeframe"], row["setup"]["window"], row["setup"]["rule"]) == (
        "1h", 20, "A")
    assert Decimal(row["setup"]["entry"]) == Decimal("99.60")  # 0.5% below the 100.10 mid.

    assert run.main([*base, "build", "--profile", "intraday-v1", "--max-picks", "10",
                     "--agent-id", "muse", "--agent-version",
                     "claude-as-muse-intraday-v1-09.28", "--now", NOW.isoformat()]) == 0
    notes = json.loads((run_dir / "build-notes.json").read_text())
    assert notes["profile"] == "INTRADAY_V1" and notes["accepted"] == ["SOL/USD"]
    [pick] = json.loads((run_dir / "report.json").read_text())["picks"]
    assert pick["technical_evidence"]["timeframe_seconds"] == 3600
    assert "1-hour" in pick["reasoning"]["thesis"]
    assert run.main([*base, "validate", "--now", NOW.isoformat()]) == 0


def test_cli_the_same_market_under_the_default_profile_finds_no_setup(tmp_path, monkeypatch):
    """DAILY_V1, unchanged: the 0.5% entry is under its 0.6% minimum everywhere, so the coin
    is skipped, and every file records DAILY_V1."""
    run_dir = tmp_path / "run"
    calls = []
    _cli_context(run_dir, tmp_path, monkeypatch)
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        recording_market(calls, intraday_hourly_rows()))
    base = ["--run-dir", str(run_dir)]
    assert run.main([*base, "market"]) == 0
    assert (calls[0]["hours"], calls[0]["days"]) == (300, 60)
    assert json.loads((run_dir / "market.json").read_text())["profile"] == "DAILY_V1"
    assert run.main([*base, "levels"]) == 0
    row = json.loads((run_dir / "levels.json").read_text())["SOL"]
    assert row["profile"] == "DAILY_V1" and row["setup"] is None
    assert any("no held higher low 0.6-6% below price" in reason for reason in row["tried"])
    assert run.main([*base, "build", "--agent-id", "claude", "--agent-version", "test-1.0",
                     "--now", NOW.isoformat()]) == 1  # No picks survived: nothing to send.
    notes = json.loads((run_dir / "build-notes.json").read_text())
    assert notes["profile"] == "DAILY_V1"
    assert "(20-30 bars, 4h/6h/1d/2h/1h)" in notes["skipped"][0]["reason"]


def test_cli_build_refuses_a_profile_other_than_the_one_levels_used(tmp_path, monkeypatch,
                                                                    capsys):
    run_dir = tmp_path / "run"
    _cli_context(run_dir, tmp_path, monkeypatch)
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        recording_market([], intraday_hourly_rows()))
    base = ["--run-dir", str(run_dir)]
    agent = ["--agent-id", "claude", "--agent-version", "test-1.0", "--now", NOW.isoformat()]
    assert run.main([*base, "market", "--profile", "intraday"]) == 0
    assert run.main([*base, "levels", "--profile", "intraday"]) == 0
    assert run.main([*base, "build", *agent]) == 2  # Built as the default, DAILY_V1.
    assert "LEVELS_PROFILE_MISMATCH" in capsys.readouterr().err
    assert not (run_dir / "report.json").exists()
    # A levels.json written before profiles existed holds DAILY_V1's setups.
    (run_dir / "levels.json").write_text(json.dumps({"SOL": {"setup": None, "tried": ["x"]}}))
    assert run.main([*base, "build", "--profile", "intraday", *agent]) == 2
    assert "LEVELS_PROFILE_MISMATCH" in capsys.readouterr().err


def test_cli_levels_refuses_market_data_fetched_for_a_narrower_profile(tmp_path, monkeypatch,
                                                                        capsys):
    run_dir = tmp_path / "run"
    _cli_context(run_dir, tmp_path, monkeypatch)
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        recording_market([], intraday_hourly_rows()))
    base = ["--run-dir", str(run_dir)]
    assert run.main([*base, "market", "--profile", "intraday-v1"]) == 0
    assert run.main([*base, "levels"]) == 2  # DAILY_V1 needs 6h and daily bars, 300 hours.
    err = capsys.readouterr().err
    assert "MARKET_DATA_PROFILE_MISMATCH" in err and "market --profile daily" in err
    assert run.main([*base, "levels", "--profile", "intraday"]) == 2  # So does INTRADAY_V2.
    err = capsys.readouterr().err
    assert "MARKET_DATA_PROFILE_MISMATCH" in err and "market --profile intraday" in err
    assert not (run_dir / "levels.json").exists()
    # The daily fetch holds everything both intraday profiles need.
    assert run.main([*base, "market"]) == 0
    for key in ("intraday", "intraday-v1"):
        assert run.main([*base, "levels", "--profile", key]) == 0
        row = json.loads((run_dir / "levels.json").read_text())["SOL"]
        assert row["setup"]["timeframe"] == "1h"


def test_cli_all_with_the_intraday_profile_never_submits(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    token_path = tmp_path / "token.txt"
    token_path.write_text("fixture-cli-token")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    calls, submitted = [], []
    monkeypatch.setattr(context_module, "fetch_context",
                        lambda base_url, tok, **kw: fake_context())
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        recording_market(calls, intraday_hourly_rows()))
    monkeypatch.setattr(submit_module, "submit_report", lambda *a, **k: submitted.append(1))
    assert run.main(["--run-dir", str(run_dir), "all", "--profile", "intraday", "--max-picks",
                     "10", "--base-url", "https://app.example", "--token-file", str(token_path),
                     "--agent-id", "muse", "--agent-version", "claude-as-muse-intraday-v1-09.28",
                     "--now", NOW.isoformat()]) == 0
    assert (calls[0]["hours"], calls[0]["days"]) == (300, 60) and not submitted
    assert json.loads((run_dir / "levels.json").read_text())["SOL"]["profile"] == "INTRADAY_V2"
    assert json.loads((run_dir / "build-notes.json").read_text())["profile"] == "INTRADAY_V2"
    assert json.loads((run_dir / "validate.json").read_text())["accepted"] == ["SOL/USD"]


# --- submit: the one side-effecting step, kept separate -----------------------------------

def test_cli_submit_posts_the_saved_report_and_never_writes_the_token(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"schema_version": "AGENT_RESEARCH_REPORT_V3", "picks": [],
                    "generated_at": "2026-09-27T12:00:00+00:00"}))  # Built long before.
    token_path = tmp_path / "token.txt"
    token_path.write_text("fixture-cli-token")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    seen = {}

    def fake_submit(base_url, tok, report, **kw):
        seen["base_url"], seen["token"], seen["report"] = base_url, tok, report
        return submit_module.SubmitResult(202, {"status": "MUSE_REPORT_RECORDED"}, "")

    monkeypatch.setattr(submit_module, "submit_report", fake_submit)
    code = run.main(["--run-dir", str(run_dir), "submit", "--base-url", "https://app.example",
                     "--token-file", str(token_path)])
    assert code == 0
    assert seen["token"] == "fixture-cli-token" and seen["base_url"] == "https://app.example"
    # Restamped just before the POST (the intake refuses a report over 60 s old), and saved.
    sent_at = datetime.fromisoformat(seen["report"]["generated_at"])
    assert abs((datetime.now(UTC) - sent_at).total_seconds()) < 60
    assert json.loads((run_dir / "report.json").read_text()) == seen["report"]
    written = (run_dir / "submit.json").read_text()
    assert json.loads(written)["status_code"] == 202
    assert "fixture-cli-token" not in written


def test_cli_submit_exits_nonzero_on_rejection(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps({"picks": []}))
    token_path = tmp_path / "token.txt"
    token_path.write_text("t")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    monkeypatch.setattr(submit_module, "submit_report",
                        lambda *a, **k: submit_module.SubmitResult(422, {"detail": "X"}, ""))
    code = run.main(["--run-dir", str(run_dir), "submit", "--base-url", "https://app.example",
                     "--token-file", str(token_path)])
    assert code == 1


# --- The offline fallback and its explicit refusal at build time --------------------------

def test_cli_context_offline_writes_the_fallback_shape(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        context_module, "offline_universe",
        lambda **kw: {"context_version": context_module.OFFLINE_CONTEXT_VERSION,
                     "as_of": NOW.isoformat(), "universe": {}, "excluded": {}},
    )
    code = run.main(["--run-dir", str(run_dir), "context", "--offline"])
    assert code == 0
    written = json.loads((run_dir / "context.json").read_text())
    assert written["context_version"] == context_module.OFFLINE_CONTEXT_VERSION


# --- build --session: for the harness (scripts/agent_research_session.py), which has no
# research-context route -- and the safety net that keeps its report off a live app -------

def fake_offline_context(*_args, now=None, **_kwargs):
    now = now or NOW  # cmd_context passes now=None explicitly when --now was not given.
    quote_at = (now - timedelta(seconds=30)).isoformat()
    return {"context_version": context_module.OFFLINE_CONTEXT_VERSION, "as_of": now.isoformat(),
            "universe": {"SOL/USD": {"bid": "100.09", "ask": "100.11", "quote_at": quote_at}},
            "excluded": {}}


def _build_session_run_dir(tmp_path, monkeypatch):
    """A run folder taken through context --offline, market and levels, ready for
    'build --session'."""
    run_dir = tmp_path / "run"
    monkeypatch.setattr(context_module, "offline_universe", fake_offline_context)
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_market_data)
    assert run.main(["--run-dir", str(run_dir), "context", "--offline"]) == 0
    assert run.main(["--run-dir", str(run_dir), "market"]) == 0
    assert run.main(["--run-dir", str(run_dir), "levels"]) == 0
    return run_dir


def test_cli_build_session_against_offline_context_omits_scheduling_and_marks_session_only(
    tmp_path, monkeypatch,
):
    run_dir = _build_session_run_dir(tmp_path, monkeypatch)
    code = run.main(["--run-dir", str(run_dir), "build", "--session", "--agent-id", "fable",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()])
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert len(report["picks"]) == 1
    for key in ("run_slot", "context_as_of", "valid_until"):
        assert key not in report, key
    assert (run_dir / build.SESSION_MARKER_FILENAME).exists()


def test_cli_validate_a_session_only_report_uses_the_marker_default(tmp_path, monkeypatch):
    run_dir = _build_session_run_dir(tmp_path, monkeypatch)
    assert run.main(["--run-dir", str(run_dir), "build", "--session", "--agent-id", "fable",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()]) == 0
    code = run.main(["--run-dir", str(run_dir), "validate", "--now", NOW.isoformat()])
    assert code == 0
    validated = json.loads((run_dir / "validate.json").read_text())
    assert validated["rejected"] == [] and validated["accepted"] == ["SOL/USD"]


def test_cli_submit_refuses_a_session_only_report(tmp_path, monkeypatch):
    run_dir = _build_session_run_dir(tmp_path, monkeypatch)
    assert run.main(["--run-dir", str(run_dir), "build", "--session", "--agent-id", "fable",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()]) == 0
    called = []
    monkeypatch.setattr(submit_module, "submit_report", lambda *a, **k: called.append(1))
    token_path = tmp_path / "token.txt"
    token_path.write_text("fixture-cli-token")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    code = run.main(["--run-dir", str(run_dir), "submit", "--base-url", "https://app.example",
                     "--token-file", str(token_path)])
    assert code == 2 and not called  # Refused before any network call was attempted.
    assert not (run_dir / "submit.json").exists()


def test_cli_a_later_plain_build_clears_a_stale_session_marker(tmp_path, monkeypatch):
    """A normal (non-session) rebuild in the same run folder must not leave a stale
    SESSION_ONLY marker that would wrongly block a later legitimate submit."""
    run_dir = tmp_path / "run"
    monkeypatch.setattr(context_module, "offline_universe", fake_offline_context)
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_market_data)
    monkeypatch.setattr(context_module, "fetch_context", lambda base_url, tok, **kw: fake_context())
    assert run.main(["--run-dir", str(run_dir), "context", "--offline"]) == 0
    assert run.main(["--run-dir", str(run_dir), "market"]) == 0
    assert run.main(["--run-dir", str(run_dir), "levels"]) == 0
    assert run.main(["--run-dir", str(run_dir), "build", "--session", "--agent-id", "fable",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()]) == 0
    assert (run_dir / build.SESSION_MARKER_FILENAME).exists()

    token_path = tmp_path / "token.txt"
    token_path.write_text("t")
    token_path.chmod(0o600)  # The kit reads owner-only files only.
    assert run.main(["--run-dir", str(run_dir), "context", "--base-url", "https://app.example",
                     "--token-file", str(token_path)]) == 0
    assert run.main(["--run-dir", str(run_dir), "build", "--agent-id", "claude",
                     "--agent-version", "test-1.0", "--now", NOW.isoformat()]) == 0
    assert not (run_dir / build.SESSION_MARKER_FILENAME).exists()
    assert "run_slot" in json.loads((run_dir / "report.json").read_text())


def test_cli_build_refuses_the_offline_context_with_a_clear_error(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "context.json").write_text(json.dumps({
        "context_version": context_module.OFFLINE_CONTEXT_VERSION, "universe": {}}))
    (run_dir / "market.json").write_text(json.dumps({"retrieved_at": NOW.isoformat(),
                                                      "coinbase": {}}))
    (run_dir / "levels.json").write_text(json.dumps({}))
    code = run.main(["--run-dir", str(run_dir), "build", "--agent-id", "claude",
                     "--agent-version", "v1"])
    assert code == 2
    assert "REAL_RESEARCH_CONTEXT_REQUIRED" in capsys.readouterr().err


# --- Clean, non-tracebacking failures ------------------------------------------------------

def test_cli_context_missing_token_fails_cleanly(tmp_path, capsys):
    run_dir = tmp_path / "run"
    code = run.main(["--run-dir", str(run_dir), "context", "--base-url", "https://app.example"])
    assert code == 2
    assert "RESEARCH_AGENT_TOKEN" in capsys.readouterr().err


def test_cli_context_requires_base_url_without_offline(tmp_path, capsys):
    run_dir = tmp_path / "run"
    code = run.main(["--run-dir", str(run_dir), "context"])
    assert code == 2
    assert "--base-url" in capsys.readouterr().err


def test_cli_market_without_a_context_fails_cleanly(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(SystemExit):
        run.main(["--run-dir", str(run_dir), "market"])


# --- default_run_dir uses the schedule's own timezone --------------------------------------

def test_default_run_dir_uses_new_york_date():
    # 2026-09-27T02:00 UTC is still 2026-09-26 evening in New York (UTC-4 in September).
    early_utc = datetime(2026, 9, 27, 2, 0, tzinfo=UTC)
    assert run.default_run_dir(early_utc).name == "2026-09-26"


def test_run_dir_is_accepted_before_or_after_the_command(tmp_path):
    """DAILY_PROCEDURE.md passes --run-dir after the command; both places work."""
    from research_agent.run import build_parser

    parser = build_parser()
    after = parser.parse_args(["context", "--offline", "--run-dir", str(tmp_path / "a")])
    before = parser.parse_args(["--run-dir", str(tmp_path / "b"), "context", "--offline"])
    neither = parser.parse_args(["context", "--offline"])
    assert (after.run_dir, before.run_dir, neither.run_dir) == (
        str(tmp_path / "a"), str(tmp_path / "b"), None)
