"""The nightly ``jobs`` cron service, its configuration and the owner's learning commands
(package learning-app).

Fixture evidence only: per-test disposable PostgreSQL databases, canned bars and injected
release checks. No Railway, broker, provider, network or owner-ledger contact.
"""

import io
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest

from catalyst_lab import cloud_config, cloud_entry, cloud_runtime
from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.learning_jobs import STEPS, previous_days, run_jobs
from catalyst_lab.managed_ops import readonly_audit_repository
from catalyst_lab.market_reality import recorded_reality
from catalyst_lab.scorecard import recorded_scorecard
from catalyst_lab.weekly_review import last_completed_week_end, recorded_review
from tests.learning_fixtures import (
    NOW,
    bearer,
    learning,  # noqa: F401 -- fixture
    minute_rows,
    outlook,
)
from tests.learning_fixtures import FakeBars as Bars
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_research_report_v3 import AGENTS

RISK_DSN = ("postgresql://catalyst_risk:fixture-risk-password-0123456789abcdef@"
            "postgres.railway.internal:5432/catalyst_lab?sslmode=require")
RELEASE = {"release_commit": "fixture-commit", "source_sha256": "0" * 64}


def jobs_env(**changes):
    env = {"CATALYST_ENVIRONMENT": "railway", "MANAGED_DATABASE_URL": RISK_DSN,
           "RAILWAY_ENVIRONMENT_ID": "fixture-environment"}
    env.update(changes)
    return {k: v for k, v in env.items() if v is not None}


# --- The container entry and the configuration --------------------------------------------------


def test_the_entry_runs_jobs_without_a_volume(monkeypatch):
    assert cloud_entry.command(["jobs"]) == (
        "jobs", [sys.executable, "-m", "catalyst_lab.cloud_runtime", "jobs"])
    with pytest.raises(SystemExit):
        cloud_entry.command(["jobs", "--day", "2026-09-27"])
    assert "jobs" not in cloud_entry.STATEFUL
    calls = []
    monkeypatch.setattr(cloud_entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cloud_entry, "prepare_state_directory",
                        lambda *a: calls.append("prepare"))
    monkeypatch.setattr(cloud_entry, "drop_privileges", lambda: calls.append("drop"))
    monkeypatch.setattr(cloud_entry.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_dir="/nonexistent"))
    cloud_entry.main(["jobs"], execve=lambda *a: calls.append("exec"), environ={"HOME": "/r"})
    assert calls == ["drop", "exec"]  # No state directory: the ledger is its only state.


def test_the_jobs_configuration_holds_only_the_ledger_connection(tmp_path):
    places = {"cwd": tmp_path, "release_root": tmp_path}
    config = cloud_config.jobs_config(jobs_env(), **places)
    assert config.on_railway and config.database_url == RISK_DSN
    assert "fixture-risk-password" not in repr(config)
    assert cloud_config.summary(config) == {"on_railway": True, "component": "jobs"}
    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY",
                 "MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN", "MANAGED_AGENT_TOKENS_JSON",
                 "JEV_WORKER_DATABASE_URL", "RISK_DATABASE_PASSWORD", "JEV_DATABASE_PASSWORD",
                 "AUDIT_DATABASE_URL"):
        with pytest.raises(cloud_config.CloudConfigError) as refused:
            cloud_config.jobs_config(jobs_env(**{name: "x" * 40}), **places)
        assert (refused.value.code, refused.value.names) == (
            "JOBS_MUST_NOT_HOLD_TRADING_SECRETS", (name,))
    cases = {
        "CLOUD_UNKNOWN_ENGINE_VARIABLE": jobs_env(MANAGED_SELECTION_RULE="X"),
        "RAILWAY_MODE_REQUIRED": jobs_env(CATALYST_ENVIRONMENT="local"),
        "CLOUD_SECRET_MISSING": jobs_env(MANAGED_DATABASE_URL=None),
        "CLOUD_DATABASE_URL_INVALID": jobs_env(
            MANAGED_DATABASE_URL=RISK_DSN.replace("catalyst_risk", "catalyst_app")),
        "CLOUD_DATABASE_SSL_REQUIRED": jobs_env(
            MANAGED_DATABASE_URL=RISK_DSN.replace("?sslmode=require", "")),
        "TYPESAFE_ENV_FILE_FORBIDDEN": jobs_env(TYPESAFE_ENV_FILE="/x"),
    }
    for code, environ in cases.items():
        with pytest.raises(cloud_config.CloudConfigError) as refused:
            cloud_config.jobs_config(environ, **places)
        assert refused.value.code == code
    (tmp_path / ".env").write_text("")
    with pytest.raises(cloud_config.CloudConfigError, match="DOTENV_PRESENT"):
        cloud_config.jobs_config(jobs_env(), **places)


# --- The run: each step independent, then exit 0, or 1 when a step did not end OK ----------------


class Step:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def run_cron(monkeypatch, capsys, *, store_factory=lambda url: object(), steps=None,
             **kwargs):
    reader = Step()
    if steps is not None:
        monkeypatch.setattr("catalyst_lab.learning_jobs.run_jobs", steps)
    code = cloud_runtime.jobs(jobs_env(), verify_release=lambda: RELEASE,
                              store_factory=store_factory, reader_factory=lambda: reader,
                              **kwargs)
    return code, capsys.readouterr().out, reader


def steps_ending(monkeypatch, *, shadow=None, replay=None):
    """Every step ends OK unless given: ``shadow`` / ``replay`` replace those two steps."""
    from catalyst_lab import learning_jobs

    monkeypatch.setattr(learning_jobs, "shadow_step",
                        shadow or (lambda *_: (None, {"recorded": 1})))
    monkeypatch.setattr(learning_jobs, "replay_step",
                        replay or (lambda *_: (None, {"recorded": 2})))
    monkeypatch.setattr(learning_jobs, "daily_step",
                        lambda record, unavailable: lambda *_: (None, {"2026-09-27": "OK"}))
    monkeypatch.setattr(learning_jobs, "weekly_step", lambda *_: (None, {}))
    monkeypatch.setattr(learning_jobs, "regime_step", lambda *_: (None, {"days": {}}))
    monkeypatch.setattr(learning_jobs, "calibration_step", lambda *_: (None, {"recorded": 0}))
    # Package learning-loop2's steps.
    monkeypatch.setattr(learning_jobs, "trade_paths_step", lambda *_: (None, {"path_recorded": 0}))
    monkeypatch.setattr(learning_jobs, "missed_tradeable_step", lambda *_: (None, {}))


def test_a_failing_step_is_logged_the_others_still_run_and_the_run_exits_one(monkeypatch,
                                                                             capsys):
    """Package ops-alarms (2026-09-29): a step that did not end OK exits 1, so Railway shows a
    failed run (restart policy NEVER: nothing repeats). Until then the run exited 0 whatever
    the steps logged. Every line is still printed."""
    steps_ending(monkeypatch)
    code, out, reader = run_cron(monkeypatch, capsys)
    assert code == 0 and reader.closed and "JOBS DONE failed=0" in out

    def shadow(*_):
        raise RuntimeError("PUBLIC_BAR_CONNECTION_ERROR")

    steps_ending(monkeypatch, shadow=shadow)
    code, out, reader = run_cron(monkeypatch, capsys)
    assert code == cloud_runtime.EXIT_JOBS_FAILED == 1 and reader.closed
    assert "JOBS STARTING release_commit=fixture-commit" in out
    assert "JOBS STEP code=PUBLIC_BAR_CONNECTION_ERROR result=FAILED step=shadow_outcomes" in out
    assert "JOBS STEP recorded=2 result=OK step=maintenance_replays" in out
    assert out.count("JOBS STEP ") == len(STEPS)
    assert "JOBS DONE failed=1" in out
    assert "fixture-risk-password" not in out
    # A step that returns its own failure code fails the run the same way.
    steps_ending(monkeypatch, replay=lambda *_: ("REPLAY_BARS_UNAVAILABLE", {"failed": 1}))
    code, out, _ = run_cron(monkeypatch, capsys)
    assert code == 1 and "code=REPLAY_BARS_UNAVAILABLE failed=1 result=FAILED" in out


def test_refusals_ledger_outages_and_the_time_limits(monkeypatch, capsys):
    code = cloud_runtime.jobs(jobs_env(APCA_API_KEY_ID="PK" + "X" * 20),
                              verify_release=lambda: RELEASE)
    assert code == 2
    assert "JOBS REFUSED code=JOBS_MUST_NOT_HOLD_TRADING_SECRETS APCA_API_KEY_ID" in \
        capsys.readouterr().out

    def down(url):
        raise ValueError("DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT")

    code, out, _ = run_cron(monkeypatch, capsys, store_factory=down)
    assert code == 1  # An unreachable ledger fails every step (exit 0 until 2026-09-29).
    for name in STEPS:
        assert (f"code=DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT result=FAILED step={name}"
                in out)
    assert f"JOBS DONE failed={len(STEPS)}" in out
    # A step not started by the soft limit is skipped, which fails the run too.
    ticks = iter([0.0, *[10_000.0] * (len(STEPS) - 1)])
    results = run_jobs(object(), Step(), now=NOW, deadline=5.0, monotonic=lambda: next(ticks))
    assert [r.result for r in results] == ["FAILED"] + ["SKIPPED"] * (len(STEPS) - 1)
    assert results[1].code == "JOB_TIME_LIMIT"
    steps_ending(monkeypatch)
    clock = iter([0.0, 0.0, *[cloud_runtime.JOBS_SOFT_SECONDS] * (len(STEPS) + 1)])
    code, out, _ = run_cron(monkeypatch, capsys, monotonic=lambda: next(clock))
    assert code == 1 and "result=OK step=shadow_outcomes" in out
    skipped = len(STEPS) - 1
    assert out.count("code=JOB_TIME_LIMIT result=SKIPPED") == skipped
    assert f"JOBS DONE failed={skipped}" in out
    exited = threading.Event()

    def slow(*_args, **_kwargs):
        exited.wait(2)
        return []

    statuses = []

    def hard_exit(status):
        statuses.append(status)
        exited.set()

    code, out, _ = run_cron(monkeypatch, capsys, steps=slow, hard_seconds=0.05,
                            hard_exit=hard_exit)
    assert exited.is_set() and "JOBS TIME_LIMIT_EXIT" in out
    assert statuses == [1]  # The hard stop exits 1 (0 until 2026-09-29).


def test_the_hard_stop_keeps_the_lines_of_the_steps_that_finished(monkeypatch, capsys):
    """Each step's line is printed as the step ends: the hard stop (``os._exit``, which prints
    nothing more) keeps the lines of the steps that finished before it. Until 2026-09-29 the
    lines were printed after the last step, so a run the hard stop ended printed none."""
    timers, statuses = [], []

    class Timer:  # threading.Timer, fired by the test while the second step runs.
        daemon = False

        def __init__(self, seconds, function):
            self.function = function
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    def hanging(*_):
        timers[0].function()  # The 30-minute stop, while the replays run.
        return None, {"recorded": 0}

    monkeypatch.setattr(cloud_runtime.threading, "Timer", Timer)
    steps_ending(monkeypatch, replay=hanging)
    _, out, _ = run_cron(monkeypatch, capsys, hard_exit=statuses.append)
    assert statuses == [1]
    assert (out.index("JOBS STEP recorded=1 result=OK step=shadow_outcomes")
            < out.index("JOBS TIME_LIMIT_EXIT") < out.index("step=maintenance_replays"))


def test_previous_days_catch_up_two_missed_nights():
    now = datetime(2026, 9, 28, 5, 30, tzinfo=UTC)  # 01:30 on the 28th in New York.
    assert previous_days(now) == [date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)]


def test_the_steps_record_each_day_once_against_the_ledger(learning):  # noqa: F811
    reply = learning.client.post("/api/v1/lab/market-outlooks", json=outlook(),
                                 headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202
    now = datetime(2026, 9, 28, 5, 30, tzinfo=UTC)
    minutes, hours = {}, {}
    for symbol in ("BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD"):
        rows = []
        for day in previous_days(now):
            start, _ = day_bounds(day)
            rows += minute_rows(start, [100, 101])
        minutes[symbol] = minute_rows(NOW, [100, 100]) + rows
    reader = Bars(minutes=minutes, hours=hours)
    results = {r.name: r for r in run_jobs(learning.store, reader, now=now)}
    assert results["shadow_outcomes"].result == "OK"
    assert results["maintenance_replays"].result == "OK"
    # The 25th ended before any universe was recorded (the outlook came on the 26th): it can
    # never be measured, so it is listed but does not fail the step (or the run's exit code).
    assert (results["market_reality"].result, results["market_reality"].code) == ("OK", None)
    assert results["market_reality"].details == {
        "2026-09-25": "REALITY_UNIVERSE_UNAVAILABLE", "2026-09-26": "RECORDED",
        "2026-09-27": "RECORDED"}
    assert results["scorecard"].result == "OK"
    assert set(results["scorecard"].details.values()) == {"RECORDED"}
    week_end = last_completed_week_end(now)
    assert results["weekly_review"].details == {week_end.isoformat(): "RECORDED"}
    again = {r.name: r for r in run_jobs(learning.store, reader, now=now)}
    assert again["scorecard"].details["2026-09-27"] == "ALREADY_RECORDED"
    assert again["market_reality"].details["2026-09-27"] == "ALREADY_RECORDED"
    assert again["weekly_review"].details == {week_end.isoformat(): "ALREADY_RECORDED"}
    assert recorded_reality(learning.store.repo, date(2026, 9, 27))["body"]["grades"]
    # MARKET_REGIME_V1 (package learning-measure) runs first: the 26th and 27th (the 25th
    # ended before any universe) are recorded once, and each reality day carries its regime.
    assert results["market_regime"].result == "OK"
    assert results["market_regime"].details["days"] == {"2026-09-26": "RECORDED",
                                                         "2026-09-27": "RECORDED"}
    assert again["market_regime"].details["days"] == {}
    regime = recorded_reality(learning.store.repo, date(2026, 9, 27))["body"]["regime"]
    assert regime["status"] == "RECORDED" and regime["regime_version"] == "MARKET_REGIME_V1"
    assert regime["tag"] == "UNKNOWN/UNKNOWN/UNKNOWN/UNKNOWN"  # No hour bars in this fixture.
    # Package learning-loop2: the daily brief of each recorded reality day, once; the 25th
    # (no universe) is not applicable; the missed-tradeable record waits for its holds (the
    # 26th is ready at 05:30 UTC on the 28th); the paths step has nothing closed to measure.
    assert STEPS.index("daily_brief") == STEPS.index("scorecard") + 1
    assert STEPS.index("trade_paths") > STEPS.index("market_reality")
    assert (results["daily_brief"].result, results["daily_brief"].code) == ("OK", None)
    assert results["daily_brief"].details == {
        "2026-09-25": "BRIEF_UNIVERSE_UNAVAILABLE", "2026-09-26": "RECORDED",
        "2026-09-27": "RECORDED"}
    assert again["daily_brief"].details["2026-09-27"] == "ALREADY_RECORDED"
    assert results["missed_tradeable"].result == "OK"
    assert results["missed_tradeable"].details["2026-09-26"] == "RECORDED"
    assert "2026-09-27" not in results["missed_tradeable"].details  # Holds not over yet.
    assert results["trade_paths"].result == "OK"
    assert results["trade_paths"].details["path_recorded"] == 0
    assert "learning" in recorded_review(learning.store.repo, week_end)["body"]


# --- The owner's commands (ops shell, catalyst_app, read-only) ----------------------------------


def owner_repo(er):  # noqa: F811
    return readonly_audit_repository(er.database_url)  # catalyst_app, as AUDIT_DATABASE_URL.


def test_the_owner_role_reads_but_cannot_write(er, learning):  # noqa: F811
    repo = owner_repo(er)
    with repo.connect() as conn:
        assert conn.execute("SELECT current_user AS u").fetchone()["u"] == "catalyst_app"
        with pytest.raises(psycopg.Error):
            conn.execute("""INSERT INTO lab.managed_events(event_id,idempotency_key,kind,body)
                VALUES(gen_random_uuid(),'x','DAILY_SCORECARD','{}')""")


def test_scorecard_market_and_weekly_commands_print_recorded_or_preview(er, learning):  # noqa: F811
    reply = learning.client.post("/api/v1/lab/market-outlooks", json=outlook(),
                                 headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202
    repo = owner_repo(er)
    now = datetime(2026, 9, 28, 5, 30, tzinfo=UTC)
    day = date(2026, 9, 27)
    out = io.StringIO()
    assert cloud_runtime.scorecard(day, out=out, repository=repo, now=now) == 0
    text = out.getvalue()
    assert text.startswith("Scorecard 2026-09-27 (DAILY_SCORECARD_V1, preview, not recorded")
    assert '"recorded": false' in text and "Costs (day): Jev" in text
    minutes = {s: minute_rows(day_bounds(day)[0], [100, 106 if s == "SOL/USD" else 100])
               for s in ("BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD")}
    out = io.StringIO()
    assert cloud_runtime.market_review(day, out=out, repository=repo, now=now,
                                       reader_factory=lambda: Bars(minutes=minutes)) == 0
    text = out.getvalue()
    assert "Market 2026-09-27 (MARKET_REALITY_V1, preview, not recorded)" in text
    assert "SOL/USD 6.0000%" in text
    run_jobs(learning.store, Bars(minutes=minutes), now=now)  # Records the day and the week.
    out = io.StringIO()
    assert cloud_runtime.scorecard(day, out=out, repository=repo, now=now) == 0
    assert "recorded, computed" in out.getvalue() and '"recorded": true' in out.getvalue()
    assert recorded_scorecard(repo, day) is not None
    out = io.StringIO()
    assert cloud_runtime.scorecard(day, out=out, repository=repo, now=now, full=True) == 0
    assert '"limitations"' in out.getvalue()
    out = io.StringIO()
    assert cloud_runtime.market_review(day, out=out, repository=repo, now=now) == 0
    assert "(MARKET_REALITY_V1, recorded)" in out.getvalue()
    assert "Outlook of claude" in out.getvalue()
    out = io.StringIO()
    assert cloud_runtime.weekly_review(out=out, repository=repo, now=now) == 0
    assert "WEEKLY_REVIEW_V1, recorded" in out.getvalue()
    assert "SELECTION_VALUE: NOT_ENOUGH_DATA" in out.getvalue()
    assert recorded_review(repo) is not None
    out = io.StringIO()
    assert cloud_runtime.weekly_review(out=out, repository=repo, now=now, preview=True) == 0
    assert "preview, not recorded" in out.getvalue()
    out = io.StringIO()
    assert cloud_runtime.scorecard(date(2026, 9, 29), out=out, repository=repo, now=now) == 1
    assert out.getvalue() == "CLOUD_LEARNING_UNAVAILABLE: SCORECARD_DAY_NOT_OVER\n"


def test_the_daily_brief_command_prints_the_recorded_brief_or_a_preview(er, learning):  # noqa: F811
    reply = learning.client.post("/api/v1/lab/market-outlooks", json=outlook(),
                                 headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 202
    repo = owner_repo(er)
    now = datetime(2026, 9, 28, 5, 30, tzinfo=UTC)
    day = date(2026, 9, 27)
    minutes = {s: minute_rows(day_bounds(day)[0], [100, 106 if s == "SOL/USD" else 100])
               for s in ("BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD")}
    out = io.StringIO()
    assert cloud_runtime.daily_brief(day, out=out, repository=repo, now=now,
                                     reader_factory=lambda: Bars(minutes=minutes)) == 1
    assert out.getvalue() == "CLOUD_LEARNING_UNAVAILABLE: BRIEF_REALITY_NOT_RECORDED\n"
    run_jobs(learning.store, Bars(minutes=minutes), now=now)
    out = io.StringIO()
    assert cloud_runtime.daily_brief(day, out=out, repository=repo, now=now) == 0
    text = out.getvalue()
    assert text.startswith("(recorded)\nDaily brief 2026-09-27 (DAILY_BRIEF_V1")
    assert "Mover SOL/USD 6.0000%" in text and '"research_focus"' in text
    with pytest.raises(SystemExit):
        cloud_runtime.main(["daily-brief", "--week-end", "2026-09-27"])


def test_the_commands_refuse_without_the_ops_connection():
    out = io.StringIO()
    assert cloud_runtime.scorecard(environ={}, out=out) == 2
    assert out.getvalue() == "CLOUD_LEARNING_REFUSED: CLOUD_SECRET_MISSING AUDIT_DATABASE_URL\n"
    with pytest.raises(SystemExit):
        cloud_runtime.main(["status", "--day", "2026-09-27"])
    with pytest.raises(SystemExit):
        cloud_runtime.main(["scorecard", "--now"])
    with pytest.raises(SystemExit):
        cloud_runtime.main(["market-review", "--full"])


def test_time_is_monotonic_enough():
    assert time.monotonic() > 0 and timedelta(minutes=20).total_seconds() == \
        cloud_runtime.JOBS_SOFT_SECONDS
