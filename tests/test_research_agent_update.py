"""research_agent.update and ``python -m research_agent.run update``: the update run of
RESEARCH_SCHEDULE_V2 (docs/RESEARCH-LOOP-V2.md 3.5, package research-loop-kit).

Offline only. The research context is a fixture in the app's RESEARCH_CONTEXT_V3 shape;
Coinbase is ``market.fetch_coinbase`` monkeypatched; the app's report and withdrawal routes
are an ``httpx.MockTransport``. Nothing here reaches a real service.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from catalyst_lab.research_report_v3 import UniverseSnapshot, check_pick, parse_report_v3
from research_agent import build as build_module
from research_agent import context as context_module
from research_agent import levels, run, update
from research_agent import market as market_module
from research_agent import submit as submit_module
from research_agent import token as token_module
from tests.research_loop_kit_fixtures import (
    AGENT,
    FAKE_TOKEN,
    SETUP,
    UPDATE_AT,
    UPDATE_VERSION,
    V1,
    V2,
    context_v3,
    fake_fetch,
    market_doc,
    open_trade,
    rally_rows,
    setup_rows,
    two_structure_rows,
    watching_row,
)

D = Decimal
PROFILE = levels.INTRADAY_V2
REAL_CLIENT = httpx.Client  # Before any test replaces it with a MockTransport client.


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


def levels_doc(ctx, raw_market, profile=PROFILE):
    """levels.json as 'levels' writes it: the profile's first setup for every coin."""
    found = {}
    for symbol in context_module.coin_symbols(ctx):
        coin = symbol.split("/")[0]
        inputs = update.coin_inputs(ctx, raw_market, symbol, profile)
        if isinstance(inputs, str):
            found[coin] = {"profile": profile.name, "setup": None,
                           "tried": ["no Coinbase market data or no live quote for this coin"]}
            continue
        series, mid, increment = inputs
        setup, tried = levels.find_setup(series, mid=mid, increment=increment, profile=profile)
        found[coin] = {"profile": profile.name,
                       "setup": levels.setup_to_json(setup) if setup else None, "tried": tried}
    return found


def review(ctx, rows_by_coin, *, now=UPDATE_AT, excluded=None):
    raw_market = market_doc(rows_by_coin, excluded=excluded)
    return update.review(ctx, raw_market, levels_doc(ctx, raw_market), profile=PROFILE, now=now)


def decision(result, symbol):
    [found] = [item for item in result.decisions if item.symbol == symbol]
    return found


def with_levels(**changes):
    return {**SETUP, **changes}


def decimals(values):
    return {key: D(value) for key, value in values.items()}


# --- Refusals: only a V2 schedule's update slots ------------------------------------------

def test_an_update_answers_the_next_update_slot_within_the_grace():
    for now, slot in ((UPDATE_AT, "2026-09-29T10:00:00-04:00"),  # 10:05: the 10:00 run.
                      (datetime(2026, 9, 29, 13, 7, tzinfo=UTC),  # 09:07: the 10:00 run.
                       "2026-09-29T10:00:00-04:00"),
                      (datetime(2026, 9, 29, 3, 7, tzinfo=UTC),  # 23:07: tomorrow's 00:00.
                       "2026-09-29T00:00:00-04:00")):
        found, limit = update.answered_slot(context_v3([], now=now)["schedule"], now)
        assert found == slot
        # Valid until the next daily run plus the grace (RESEARCH_SCHEDULE_V2).
        assert limit.endswith("09:00:00-04:00")


@pytest.mark.parametrize("now", [datetime(2026, 9, 29, 11, 30, tzinfo=UTC),  # 07:30: 08:00.
                                 datetime(2026, 9, 29, 12, 45, tzinfo=UTC)])  # 08:45: 08:00.
def test_an_update_refuses_the_daily_full_runs_slot(now):
    with pytest.raises(update.UpdateRefused, match="UPDATE_SLOT_IS_FULL_RUN"):
        update.answered_slot(context_v3([], now=now)["schedule"], now)


def test_an_update_refuses_a_v1_schedule_and_a_missing_one():
    with pytest.raises(update.UpdateRefused,
                       match="UPDATE_NEEDS_SCHEDULE_V2.*RESEARCH_SCHEDULE_V1"):
        update.answered_slot(context_v3([], schedule=V1)["schedule"], UPDATE_AT)
    with pytest.raises(update.UpdateRefused, match="UPDATE_NEEDS_SCHEDULE_V2"):
        update.answered_slot(None, UPDATE_AT)
    incomplete = {"version": "RESEARCH_SCHEDULE_V2", "current_run_slot": "x"}
    with pytest.raises(update.UpdateRefused, match="UPDATE_SCHEDULE_INCOMPLETE"):
        update.answered_slot(incomplete, UPDATE_AT)


def test_the_cli_refuses_before_fetching_any_market_data(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_fetch({}, calls=calls))
    for ctx, code in ((context_v3(["SOL/USD"], schedule=V1), "UPDATE_NEEDS_SCHEDULE_V2"),
                      (context_v3(["SOL/USD"], now=datetime(2026, 9, 29, 11, 30, tzinfo=UTC)),
                       "UPDATE_SLOT_IS_FULL_RUN")):
        run_dir = tmp_path / code
        run_dir.mkdir()
        (run_dir / "context.json").write_text(json.dumps(ctx))
        now = ctx["as_of"]
        assert run.main(["update", "--run-dir", str(run_dir), "--profile", "intraday",
                         *AGENT, "--now", now]) == 2
        assert code in capsys.readouterr().err
        assert not calls and not (run_dir / "market.json").exists()
        assert not (run_dir / update.NOTES_FILE).exists()
        assert not (run_dir / update.MARKER_FILE).exists()  # Refused: not an update folder.


def test_the_cli_never_touches_another_runs_folder(tmp_path, monkeypatch, capsys):
    """The daily run's folder (runs/<date>) holds its own context, market, levels and report:
    an update pointed at it by mistake refuses before reading or writing anything."""
    run_dir = tmp_path / "runs" / "2026-09-29"
    run_dir.mkdir(parents=True)
    daily = {"context.json": '{"daily": 1}', "market.json": "{}", "report.json": '{"r": 1}',
             "build-notes.json": "{}"}
    for name, text in daily.items():
        (run_dir / name).write_text(text)
    fetched = []
    monkeypatch.setattr(context_module, "fetch_context",
                        lambda *a, **k: fetched.append(1) or scenario_context())
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_fetch({}, calls=fetched))
    assert run.main(["update", "--run-dir", str(run_dir), "--profile", "intraday",
                     "--base-url", "https://app.example", "--token-file",
                     str(_token(tmp_path)), *AGENT, "--now", UPDATE_AT.isoformat()]) == 2
    assert "UPDATE_RUN_DIR_IN_USE" in capsys.readouterr().err and fetched == []
    assert {path.name: path.read_text() for path in run_dir.iterdir()} == daily


# --- The review: kept, adjusted, withdrawn ---------------------------------------------------

def test_a_setup_found_again_unchanged_is_kept_and_nothing_is_sent_for_it():
    ctx = context_v3(["SOL/USD"], watching=[watching_row("SOL/USD", SETUP)])
    result = review(ctx, {"SOL": setup_rows()})
    found = decision(result, "SOL/USD")
    assert found.action == update.KEPT and found.checked
    assert found.reason.startswith("Found again on 1h/20 rule A")
    assert result.new_coins == {}  # A WATCHING coin is never a new pick.


def test_a_setup_no_longer_found_is_withdrawn_with_the_first_failing_check():
    ctx = context_v3(["AVAX/USD"], watching=[watching_row("AVAX/USD", SETUP)])
    found = decision(review(ctx, {"AVAX": rally_rows()}), "AVAX/USD")
    assert found.action == update.WITHDRAWN
    assert found.reason == ("No INTRADAY_V2 setup found now; first failing check: 1h/20 rule A: "
                            "the window high is its most recent bar (still extending, no held "
                            "resistance to target)")
    assert 1 <= len(found.reason) <= 300


def test_a_changed_plan_is_an_adjusted_pick_with_the_profiles_setup():
    ctx = context_v3(["ETH/USD"], watching=[watching_row("ETH/USD", with_levels(
        entry_trigger="98.00", max_entry_price="98.15"))])
    found = decision(review(ctx, {"ETH": setup_rows()}), "ETH/USD")
    assert found.action == update.ADJUSTED
    assert (found.setup.entry, found.setup.stop, found.setup.target) == (
        D("99.60"), D("89.64"), D("130"))
    assert "Not found again; the INTRADAY_V2 setup now (1h/20 rule A) moves entry 1.63%" in \
        found.reason


@pytest.mark.parametrize("changes, action", [
    ({"entry_trigger": "99.36"}, update.KEPT),  # 0.24% from 99.60: the same entry.
    ({"entry_trigger": "99.35"}, update.ADJUSTED),  # 0.25%: moved.
    ({"stop": "90.05"}, update.KEPT),  # 0.46% from 89.64.
    ({"stop": "90.10"}, update.ADJUSTED),  # 0.51%.
    ({"target": "130.65"}, update.KEPT),  # 0.50% down to 130, under the threshold.
    ({"target": "130.66"}, update.ADJUSTED),  # 0.51%.
])
def test_the_thresholds_entry_quarter_percent_stop_and_target_half_percent(changes, action):
    ctx = context_v3(["SOL/USD"], watching=[watching_row("SOL/USD", with_levels(**changes))])
    assert decision(review(ctx, {"SOL": setup_rows()}), "SOL/USD").action == action


def test_the_thresholds_are_relative_to_the_watching_level_at_their_exact_bounds():
    old = {"entry_trigger": D("100"), "max_entry_price": D("100.15"), "stop": D("100"),
           "target": D("100")}

    def setup(entry="100", stop="100", target="100"):
        return SimpleNamespace(entry=D(entry), stop=D(stop), target=D(target))

    assert update.material(old, setup()) == {}
    assert set(update.material(old, setup(entry="100.25"))) == {"entry_trigger"}
    assert update.material(old, setup(entry="100.24")) == {}
    assert set(update.material(old, setup(entry="99.75"))) == {"entry_trigger"}
    assert set(update.material(old, setup(stop="100.5"))) == {"stop"}
    assert update.material(old, setup(stop="100.49")) == {}
    assert set(update.material(old, setup(target="99.5"))) == {"target"}
    assert update.material(old, setup(target="99.51")) == {}


def test_a_setup_still_shown_by_other_bars_is_kept_not_churned():
    """The daily run's 4-hour setup, reviewed under INTRADAY_V2: the profile's first setup
    is a different 1-hour one, but the 4-hour bars still show the WATCHING plan, so it stays
    (found again), rather than being replaced by whatever the search happens to try first."""
    rows = two_structure_rows()
    raw_market = market_doc({"SOL": rows})
    ctx = context_v3(["SOL/USD"])
    series, mid, increment = update.coin_inputs(ctx, raw_market, "SOL/USD", PROFILE)
    first, _tried = levels.find_setup(series, mid=mid, increment=increment, profile=PROFILE)
    daily, _tried = levels.find_setup(series, mid=mid, increment=increment, profile=PROFILE,
                                      timeframes=("4h",))
    assert (first.timeframe, first.entry, first.stop, first.target) == (
        "1h", D("99.60"), D("96.61"), D("107"))
    assert (daily.timeframe, daily.entry, daily.stop, daily.target) == (
        "4h", D("97.00"), D("89.64"), D("130"))
    old = {"entry_trigger": "97.00", "max_entry_price": "97.15", "stop": "89.64",
           "target": "130"}
    ctx = context_v3(["SOL/USD"], watching=[watching_row("SOL/USD", old)])
    found = decision(update.review(ctx, raw_market, levels_doc(ctx, raw_market),
                                   profile=PROFILE, now=UPDATE_AT), "SOL/USD")
    assert found.action == update.KEPT
    assert found.reason.startswith("Found again on 4h/20 rule A: entry 0.00%, stop 0.00%, "
                                   "target 0.00%")


@pytest.mark.parametrize("bid, ask, words", [
    ("99.95", "100.05", "0.10% above the entry 99.9"),  # Inside the 0.3% band minimum.
    ("99.80", "99.84", "at or below the entry 99.9"),  # Through it, still WATCHING.
])
def test_price_at_the_entry_leaves_the_setup_to_the_apps_trigger(bid, ask, words):
    watching = [watching_row("SOL/USD", with_levels(entry_trigger="99.90",
                                                    max_entry_price="100.05"))]
    ctx = context_v3(["SOL/USD"], watching=watching, quotes={"SOL/USD": (bid, ask)})
    found = decision(review(ctx, {"SOL": rally_rows()}), "SOL/USD")  # No setup found now.
    assert found.action == update.KEPT and words in found.reason
    assert found.reason.endswith("left to the app's trigger.")


def test_a_coin_without_market_data_is_kept_unchecked_never_withdrawn():
    ctx = context_v3(["SOL/USD"], watching=[watching_row("SOL/USD", SETUP)])
    found = decision(review(ctx, {}, excluded={"SOL": "INCOMPLETE_COINBASE_MARKET_DATA"}),
                     "SOL/USD")
    assert found.action == update.KEPT and not found.checked
    assert found.reason == ("Not re-checked: INCOMPLETE_COINBASE_MARKET_DATA; the setup "
                            "stays.")


def test_a_coin_that_left_the_tradable_universe_is_withdrawn():
    ctx = context_v3(["SOL/USD"], watching=[watching_row("XRP/USD", SETUP)])
    found = decision(review(ctx, {"SOL": setup_rows()}), "XRP/USD")
    assert found.action == update.WITHDRAWN
    assert found.reason == "Not in the research context's tradable universe now."


def test_an_expired_or_unreadable_setup_is_left_to_the_app():
    expired = watching_row("SOL/USD", SETUP, expires_at="2026-09-29T09:00:00-04:00")
    unreadable = watching_row("ETH/USD", {"entry_trigger": "x", "max_entry_price": "1",
                                         "stop": "1", "target": "2"})
    ctx = context_v3(["SOL/USD", "ETH/USD"], watching=[expired, unreadable])
    result = review(ctx, {"SOL": rally_rows(), "ETH": rally_rows()})
    assert decision(result, "SOL/USD").reason.startswith("Expired at 2026-09-29T09:00:00-04:00")
    assert decision(result, "ETH/USD").reason == "Not re-checked: its levels cannot be read."
    assert {item.action for item in result.decisions} == {update.KEPT}


def test_new_coins_exclude_watching_and_open_trade_coins():
    ctx = context_v3(["SOL/USD", "GRT/USD", "DOGE/USD", "LINK/USD"],
                     watching=[watching_row("SOL/USD", SETUP)],
                     open_trades=[open_trade("GRT/USD")])
    result = review(ctx, {"SOL": setup_rows(), "GRT": setup_rows(), "DOGE": setup_rows(),
                          "LINK": rally_rows()})
    assert set(result.new_coins) == {"DOGE"}
    assert set(result.skipped_coins) == {"LINK"}
    assert result.open_symbols == ("GRT/USD",)
    assert [item.symbol for item in result.decisions] == ["SOL/USD"]


def test_watching_rows_come_from_watching_setups_and_the_open_trades_fallback():
    row = watching_row("SOL/USD", SETUP)
    rows, source, notes = update.watching_rows(context_v3(["SOL/USD"], watching=[row]))
    assert (rows, source, notes) == ([row], "watching_setups", [])
    legacy = context_v3(["SOL/USD"], version="RESEARCH_CONTEXT_V2")
    rows, source, notes = update.watching_rows(legacy)
    assert rows == [] and source is None
    assert notes == ["The research context has no watching_setups (RESEARCH_CONTEXT_V3 lists "
                     "the agent's own WATCHING setups there): none were reviewed from it."]
    fallback = {**open_trade("ETH/USD", state="WATCHING")}
    legacy["open_trades"] = [fallback, open_trade("GRT/USD")]
    rows, source, notes = update.watching_rows(legacy)
    assert rows == [fallback] and source == "open_trades"
    assert update.open_trade_symbols(legacy) == {"GRT/USD"}


# --- AGENT_RESEARCH_WITHDRAWAL_V1 ---------------------------------------------------------------

def withdrawn(*symbols):
    return [update.Decision(symbol, update.WITHDRAWN, f"No setup for {symbol} now.", ("s",))
            for symbol in symbols]


def test_the_withdrawal_body_is_exactly_the_apps_contract():
    body = update.withdrawal_body(withdrawn("XRP/USD", "AVAX/USD"), agent_id="muse",
                                  agent_version=UPDATE_VERSION)
    assert set(body) == {"schema_version", "withdrawal_id", "agent", "items"}
    assert body["schema_version"] == "AGENT_RESEARCH_WITHDRAWAL_V1"
    assert UUID(body["withdrawal_id"]).version == 4
    assert body["agent"] == {"agent_id": "muse", "agent_version": UPDATE_VERSION}
    assert body["items"] == [{"symbol": "XRP/USD", "reason": "No setup for XRP/USD now."},
                             {"symbol": "AVAX/USD", "reason": "No setup for AVAX/USD now."}]
    assert update.withdrawal_problems(body) == []


def test_the_withdrawal_check_refuses_whatever_the_app_would():
    good = update.withdrawal_body(withdrawn("XRP/USD"), agent_id="muse",
                                  agent_version=UPDATE_VERSION)

    def problems(**changes):
        return update.withdrawal_problems({**good, **changes})

    assert problems(extra=1)
    assert problems(schema_version="AGENT_RESEARCH_WITHDRAWAL_V2")
    assert problems(withdrawal_id="not-a-uuid")
    assert problems(withdrawal_id="c0a80121-7ac0-11ef-8000-000000000000")  # A UUID1.
    assert problems(withdrawal_id=good["withdrawal_id"].upper())  # Not the canonical form.
    assert problems(agent={**good["agent"], "run_id": str(uuid4())})  # Not the report's block.
    assert problems(agent={"agent_id": "Muse", "agent_version": UPDATE_VERSION})
    assert problems(items=[])
    assert problems(items=[{"symbol": f"C{index}/USD", "reason": "x"} for index in range(31)])
    assert problems(items=[{"symbol": "XRP/USD", "reason": "x"}] * 2)  # Not unique.
    assert problems(items=[{"symbol": "XRP-USD", "reason": "x"}])
    assert problems(items=[{"symbol": "XRP/EUR", "reason": "x"}])
    assert problems(items=[{"symbol": "XRP/USD", "reason": ""}])
    assert problems(items=[{"symbol": "XRP/USD", "reason": "x" * 301}])
    assert problems(items=[{"symbol": "XRP/USD", "reason": "x", "note": "y"}])
    assert problems(items=[{"symbol": "XRP/USD", "reason": "write to someone@example.com"}]) == [
        "items[0].reason: SENSITIVE_EVIDENCE_REJECTED"]
    assert not problems(items=[{"symbol": f"C{index}/USD", "reason": "x"} for index in range(30)])


# --- The update command, end to end -----------------------------------------------------------

def _token(tmp_path):
    path = tmp_path / "token"
    path.write_text(FAKE_TOKEN)
    path.chmod(0o600)
    return path


def _update(tmp_path, monkeypatch, ctx, rows_by_coin, *extra, name="update-1000"):
    run_dir = tmp_path / name
    token_path = _token(tmp_path)
    monkeypatch.setattr(context_module, "fetch_context", lambda base_url, tok, **kw: ctx)
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_fetch(rows_by_coin))
    code = run.main(["update", "--run-dir", str(run_dir), "--profile", "intraday",
                     "--base-url", "https://app.example", "--token-file", str(token_path),
                     *AGENT, "--now", UPDATE_AT.isoformat(), *extra])
    return code, run_dir, token_path


SCENARIO = {
    "coins": ["SOL/USD", "ETH/USD", "AVAX/USD", "DOGE/USD", "LINK/USD", "GRT/USD"],
    "watching": [watching_row("SOL/USD", SETUP),  # Unchanged: kept.
                 watching_row("ETH/USD", with_levels(entry_trigger="98.00")),  # Adjusted.
                 watching_row("AVAX/USD", SETUP)],  # No setup now: withdrawn.
    "open_trades": [open_trade("GRT/USD")],  # Never reviewed, never picked again.
    "rows": {"SOL": setup_rows(), "ETH": setup_rows(), "AVAX": rally_rows(),
             "DOGE": setup_rows(), "LINK": rally_rows(), "GRT": setup_rows()},
}


def scenario_context():
    return context_v3(SCENARIO["coins"], watching=SCENARIO["watching"],
                      open_trades=SCENARIO["open_trades"])


def test_update_writes_the_withdrawal_the_report_and_the_notes(tmp_path, monkeypatch, capsys):
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"])
    out = capsys.readouterr().out
    assert code == 0, out
    withdrawal = json.loads((run_dir / "withdrawal.json").read_text())
    assert [item["symbol"] for item in withdrawal["items"]] == ["AVAX/USD"]
    assert update.withdrawal_problems(withdrawal) == []
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]  # Adjusted 1st.
    assert report["run_slot"] == "2026-09-29T10:00:00-04:00"
    # Valid until the next daily run plus the grace (V2), under 23 h 50 min from now.
    assert report["valid_until"] == "2026-09-30T09:00:00-04:00"
    assert decimals(report["picks"][0]["levels"]) == decimals(SETUP)
    assert {row["symbol"] for row in report["skipped"]} == {"LINK/USD"}
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["schema"] == "RESEARCH_AGENT_UPDATE_NOTES_V1"
    assert (notes["run_slot"], notes["run_kind"], notes["profile"]) == (
        "2026-09-29T10:00:00-04:00", "UPDATE", "INTRADAY_V2")
    assert [row["symbol"] for row in notes["kept"]] == ["SOL/USD"]
    assert [row["symbol"] for row in notes["withdrawn"]] == ["AVAX/USD"]
    [adjusted] = notes["adjusted"]
    assert adjusted["symbol"] == "ETH/USD" and adjusted["in_report"] is True
    assert adjusted["left_out_reason"] is None and adjusted["new_setup"] == "1h/20 rule A"
    assert notes["withdrawn"][0]["in_withdrawal"] is True
    assert D(adjusted["old_levels"]["entry_trigger"]) == D("98")
    assert decimals(adjusted["new_levels"]) == decimals(SETUP)
    assert adjusted["moved_pct"] == {"entry_trigger": "1.63", "stop": "0.00", "target": "0.00"}
    assert [row["symbol"] for row in notes["new"]] == ["DOGE/USD"]
    assert notes["open_trades"] == ["GRT/USD"] and notes["watching_source"] == "watching_setups"
    assert notes["files"] == {"withdrawal": "withdrawal.json", "report": "report.json"}
    assert notes["nothing_to_send"] is False
    validated = json.loads((run_dir / "validate.json").read_text())
    assert validated["accepted"] == ["ETH/USD", "DOGE/USD"] and validated["rejected"] == []
    assert validated["withdrawal"] == {"items": 1, "problems": []}
    assert "1 kept, 1 adjusted, 1 withdrawn, 1 new picks" in out
    for path in run_dir.iterdir():
        assert FAKE_TOKEN not in path.read_text(), path


def test_every_update_pick_passes_the_apps_own_intake_checks(tmp_path, monkeypatch):
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"])
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    intake = parse_report_v3(report, schedule=V2)  # The envelope: slot, validity, agent.
    universe = UniverseSnapshot(frozenset(SCENARIO["coins"]), UPDATE_AT, "TEST_UNIVERSE")
    for item in intake.picks:
        checked, _expiry = check_pick(item, intake=intake, universe=universe,
                                      expires_at=intake.valid_until, now=UPDATE_AT)
        assert checked.code is None, (checked.code, checked.errors)


class _Clock(datetime):
    """``datetime`` whose ``now`` is the test's clock, for the kit modules that import it."""

    current = UPDATE_AT

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


def test_a_live_update_stamps_its_report_after_the_market_read(tmp_path, monkeypatch, capsys):
    """The timer runs without --now: the update starts, then reads the context and the
    market. The report is stamped when it is built, as 'build' stamps it. Stamped with the
    start, the fresh bars would read as retrieved after the report and the app's own models
    would refuse every pick FUTURE_TECHNICAL_EVIDENCE (the live dry run of 2026-09-28)."""
    monkeypatch.setattr(run, "datetime", _Clock)
    monkeypatch.setattr(build_module, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "current", UPDATE_AT)
    read = fake_fetch(SCENARIO["rows"], retrieved_at=UPDATE_AT + timedelta(seconds=20))

    def fetch_after_the_start(coins, **kwargs):
        doc = read(coins, **kwargs)
        monkeypatch.setattr(_Clock, "current", UPDATE_AT + timedelta(seconds=40))
        return doc

    monkeypatch.setattr(context_module, "fetch_context",
                        lambda base_url, tok, **kw: scenario_context())
    monkeypatch.setattr(market_module, "fetch_coinbase", fetch_after_the_start)
    run_dir = tmp_path / "update-1000"
    code = run.main(["update", "--run-dir", str(run_dir), "--profile", "intraday",
                     "--base-url", "https://app.example", "--token-file",
                     str(_token(tmp_path)), *AGENT])
    out = capsys.readouterr()
    assert code == 0, out
    assert "FUTURE_TECHNICAL_EVIDENCE" not in out.out
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]
    assert report["generated_at"] == (UPDATE_AT + timedelta(seconds=40)).isoformat()
    assert report["run_slot"] == "2026-09-29T10:00:00-04:00"
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["generated_at"] == UPDATE_AT.isoformat()  # The review's own instant.
    assert notes["left_out"] == []


def test_the_pick_cap_puts_adjusted_picks_first(tmp_path, monkeypatch):
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         {**SCENARIO["rows"], "LINK": setup_rows()},
                                         "--max-picks", "2")
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["left_out"] == [{"symbol": "LINK/USD",
                                  "reason": "Report already at its pick limit."}]
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], "--max-picks", "1", name="cap-1")
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert [pick["symbol"] for pick in json.loads((run_dir / "report.json").read_text())[
        "picks"]] == ["ETH/USD"]
    assert notes["new"] == [] and notes["left_out"][0]["symbol"] == "DOGE/USD"


def test_the_evidence_options_leave_out_new_coins_only(tmp_path, monkeypatch):
    """--exclude and --max-entry-distance-pct (owner direction 2026-10-02) leave a new coin
    out of the report with its reason in the notes; the adjusted pick (ETH) is never
    filtered. Nonsense values are refused by the parser before anything is fetched."""
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], "--exclude", "doge, link")
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD"]
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["new"] == []
    assert notes["left_out"] == [{"symbol": "DOGE/USD",
                                  "reason": build_module.EXCLUDED_SKIP_REASON}]
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], "--max-entry-distance-pct", "0.1",
                                         name="far")
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD"]  # Adjusted: kept.
    notes = json.loads((run_dir / "update-notes.json").read_text())
    [left_out] = notes["left_out"]
    assert left_out["symbol"] == "DOGE/USD"
    assert left_out["reason"].startswith("Entry ") and "0.10%" in left_out["reason"]
    with pytest.raises(SystemExit):
        _update(tmp_path, monkeypatch, scenario_context(), SCENARIO["rows"],
                "--max-entry-distance-pct", "abc", name="bad")


def test_max_new_picks_caps_the_new_coins_after_the_adjusted_ones(tmp_path, monkeypatch):
    """--max-new-picks 0 keeps the adjusted pick (ETH) and leaves the new coin (DOGE) out at
    the limit; 1 lets DOGE in; a negative value is refused."""
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], "--max-new-picks", "0")
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD"]
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["left_out"] == [{"symbol": "DOGE/USD",
                                  "reason": "Report already at its pick limit."}]
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], "--max-new-picks", "1", name="one")
    assert code == 0
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]
    with pytest.raises(SystemExit):
        _update(tmp_path, monkeypatch, scenario_context(), SCENARIO["rows"],
                "--max-new-picks", "-1", name="neg")


def test_an_update_with_nothing_to_change_writes_the_notes_and_sends_nothing(
        tmp_path, monkeypatch, capsys):
    ctx = context_v3(["SOL/USD", "LINK/USD"], watching=[watching_row("SOL/USD", SETUP)])
    code, run_dir, token_path = _update(tmp_path, monkeypatch, ctx,
                                        {"SOL": setup_rows(), "LINK": rally_rows()})
    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to change: no withdrawal and no picks, so nothing to send" in out
    assert not (run_dir / "withdrawal.json").exists() and not (run_dir / "report.json").exists()
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["nothing_to_send"] is True and [row["symbol"] for row in notes["kept"]] == [
        "SOL/USD"]
    sent = []
    monkeypatch.setattr(submit_module, "post_json", lambda *a, **k: sent.append(a))
    assert run.main(["submit", "--run-dir", str(run_dir), "--base-url", "https://app.example",
                     "--token-file", str(token_path)]) == 0
    assert "nothing to send" in capsys.readouterr().out and sent == []
    assert run.main(["validate", "--run-dir", str(run_dir)]) == 0
    assert json.loads((run_dir / "validate.json").read_text()) == {
        "accepted": [], "rejected": [], "dossier_bytes": {}}


def test_an_update_clears_the_files_of_an_earlier_update_in_its_folder(tmp_path, monkeypatch):
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"])
    assert code == 0 and (run_dir / "withdrawal.json").exists()
    ctx = context_v3(["SOL/USD"], watching=[watching_row("SOL/USD", SETUP)])
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, ctx, {"SOL": setup_rows()})
    assert code == 0
    assert not (run_dir / "withdrawal.json").exists() and not (run_dir / "report.json").exists()
    assert not (run_dir / "validate.json").exists()


def test_a_context_without_watching_setups_reviews_none_and_says_so(tmp_path, monkeypatch):
    ctx = context_v3(["SOL/USD", "DOGE/USD"], version="RESEARCH_CONTEXT_V2")
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, ctx,
                                         {"SOL": setup_rows(), "DOGE": setup_rows()})
    assert code == 0
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["watching_source"] is None and notes["reviewed"] == 0
    assert "no watching_setups" in notes["notes"][0]
    assert [row["symbol"] for row in notes["new"]] == ["DOGE/USD", "SOL/USD"]


# --- submit: the withdrawal first, then the report --------------------------------------------

def recording_app(monkeypatch, *, withdrawal_status=200, withdrawal_body=None):
    """The app's two routes on an httpx.MockTransport; every request is recorded."""
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers.get("authorization"), body))
        if request.url.path == submit_module.WITHDRAWAL_ROUTE:
            answer = withdrawal_body or {
                "status": "RESEARCH_WITHDRAWAL_RECORDED",
                "schema_version": "AGENT_RESEARCH_WITHDRAWAL_V1",
                "withdrawal_id": body["withdrawal_id"], "agent_id": "muse",
                "agent_version": UPDATE_VERSION,
                "results": [{"symbol": item["symbol"], "result": "WITHDRAWN",
                             "setup_ids": ["s1"], "selections_declined": 0}
                            for item in body["items"]],
                "idempotent_replay": False, "trade_authorized": False}
            return httpx.Response(withdrawal_status, json=answer)
        return httpx.Response(202, json={"status": "MUSE_REPORT_RECORDED"})

    monkeypatch.setattr(submit_module.httpx, "Client",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handle)))
    return seen


def _submit(run_dir, token_path):
    return run.main(["submit", "--run-dir", str(run_dir), "--base-url", "https://app.example",
                     "--token-file", str(token_path)])


def test_submit_sends_the_withdrawal_before_the_report(tmp_path, monkeypatch, capsys):
    code, run_dir, token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                        SCENARIO["rows"])
    assert code == 0
    seen = recording_app(monkeypatch)
    assert _submit(run_dir, token_path) == 0
    assert [path for path, _auth, _body in seen] == [submit_module.WITHDRAWAL_ROUTE,
                                                     submit_module.SUBMIT_ROUTE]
    assert all(auth == "Bearer " + FAKE_TOKEN for _path, auth, _body in seen)
    assert seen[0][2] == json.loads((run_dir / "withdrawal.json").read_text())  # As written.
    assert seen[1][2]["picks"][0]["symbol"] == "ETH/USD"
    recorded = json.loads((run_dir / "withdrawal-submit.json").read_text())
    assert recorded["status_code"] == 200
    assert recorded["body"]["results"][0] == {"symbol": "AVAX/USD", "result": "WITHDRAWN",
                                              "setup_ids": ["s1"], "selections_declined": 0}
    assert json.loads((run_dir / "submit.json").read_text())["status_code"] == 202
    assert "AVAX/USD WITHDRAWN" in capsys.readouterr().out
    for path in run_dir.iterdir():
        assert FAKE_TOKEN not in path.read_text(), path

    # A second submit does not send the recorded withdrawal again; the report goes again
    # (the same report_id: the app replays or refuses it).
    seen.clear()
    assert _submit(run_dir, token_path) == 0
    assert [path for path, _auth, _body in seen] == [submit_module.SUBMIT_ROUTE]


@pytest.mark.parametrize("status, answer", [
    (409, {"detail": "WITHDRAWAL_ID_CONFLICT"}),
    (422, {"detail": "INVALID_RESEARCH_WITHDRAWAL",
           "errors": [{"path": "items[0].symbol", "code": "STRING_PATTERN_MISMATCH"}]}),
    (403, {"detail": "AGENT_IDENTITY_MISMATCH"}),
    (503, {"detail": "RESEARCH_WITHDRAWAL_NOT_CONFIGURED"}),
    (202, {"status": "SOMETHING_ELSE"}),  # Only 200 is success.
])
def test_any_withdrawal_answer_but_200_stops_before_the_report(tmp_path, monkeypatch, capsys,
                                                               status, answer):
    code, run_dir, token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                        SCENARIO["rows"])
    assert code == 0
    capsys.readouterr()
    seen = recording_app(monkeypatch, withdrawal_status=status, withdrawal_body=answer)
    assert _submit(run_dir, token_path) == 1
    assert [path for path, _auth, _body in seen] == [submit_module.WITHDRAWAL_ROUTE]
    err = capsys.readouterr().err
    assert f"HTTP {status}" in err and "the report was not sent" in err
    if answer.get("detail"):
        assert answer["detail"] in err
    assert not (run_dir / "submit.json").exists()
    assert json.loads((run_dir / "withdrawal-submit.json").read_text())["status_code"] == status


def test_a_withdrawal_without_an_answer_stops_and_is_resent_first(tmp_path, monkeypatch, capsys):
    code, run_dir, token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                        SCENARIO["rows"])
    assert code == 0

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(submit_module.httpx, "Client",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(refuse)))
    assert _submit(run_dir, token_path) == 1
    assert "RESEARCH_WITHDRAWAL_SUBMIT_CONNECTION_ERROR" in capsys.readouterr().err
    assert not (run_dir / "submit.json").exists()
    seen = recording_app(monkeypatch)
    assert _submit(run_dir, token_path) == 0
    assert [path for path, _auth, _body in seen] == [submit_module.WITHDRAWAL_ROUTE,
                                                     submit_module.SUBMIT_ROUTE]


def test_a_withdrawal_only_update_sends_just_the_withdrawal(tmp_path, monkeypatch):
    ctx = context_v3(["AVAX/USD"], watching=[watching_row("AVAX/USD", SETUP)])
    code, run_dir, token_path = _update(tmp_path, monkeypatch, ctx, {"AVAX": rally_rows()})
    assert code == 0 and not (run_dir / "report.json").exists()
    assert json.loads((run_dir / "validate.json").read_text())["withdrawal"] == {
        "items": 1, "problems": []}
    seen = recording_app(monkeypatch)
    assert _submit(run_dir, token_path) == 0
    assert [path for path, _auth, _body in seen] == [submit_module.WITHDRAWAL_ROUTE]


def test_submit_refuses_a_hand_broken_withdrawal_and_sends_nothing(tmp_path, monkeypatch,
                                                                   capsys):
    code, run_dir, token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                        SCENARIO["rows"])
    assert code == 0
    body = json.loads((run_dir / "withdrawal.json").read_text())
    body["agent"]["run_id"] = str(uuid4())
    (run_dir / "withdrawal.json").write_text(json.dumps(body))
    assert run.main(["validate", "--run-dir", str(run_dir), "--now",
                     UPDATE_AT.isoformat()]) == 1
    seen = recording_app(monkeypatch)
    assert _submit(run_dir, token_path) == 2
    assert seen == [] and "agent: must hold exactly agent_id and agent_version" in \
        capsys.readouterr().err


def test_submit_without_a_withdrawal_sends_the_report_as_before(tmp_path, monkeypatch):
    """A daily or intraday run folder (no withdrawal.json, no update-notes.json): submit is
    unchanged, one POST of report.json."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps({"schema_version": "AGENT_RESEARCH_REPORT_V3",
                                                     "picks": []}))
    seen = recording_app(monkeypatch)
    assert _submit(run_dir, _token(tmp_path)) == 0
    assert [path for path, _auth, _body in seen] == [submit_module.SUBMIT_ROUTE]
    assert not (run_dir / "withdrawal-submit.json").exists()


# --- The daily run's commands accept a RESEARCH_CONTEXT_V3 context ------------------------------

@pytest.mark.parametrize("version", ["RESEARCH_CONTEXT_V1", "RESEARCH_CONTEXT_V2",
                                     "RESEARCH_CONTEXT_V3"])
def test_fetch_context_accepts_v1_v2_and_v3(version):
    body = context_v3(["SOL/USD"], version=version)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert context_module.fetch_context("https://app.example", "t", client=client) == body


def test_fetch_context_still_refuses_an_unknown_version():
    body = context_v3(["SOL/USD"], version="RESEARCH_CONTEXT_V4")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(context_module.ContextError, match="UNEXPECTED_SHAPE"):
        context_module.fetch_context("https://app.example", "t", client=client)


def test_the_daily_runs_commands_accept_a_v3_context(tmp_path, monkeypatch, capsys):
    """context, lessons, market, levels, outlook, outlook-check, build, validate and submit,
    the morning's commands, on a V3 context under a V2 schedule at 08:30 (the daily run)."""
    now = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)
    ctx = context_v3(["SOL/USD"], now=now, watching=[watching_row("SOL/USD", SETUP)])
    token_path = _token(tmp_path)
    run_dir = tmp_path / "runs" / "2026-09-29"
    monkeypatch.setattr(context_module, "fetch_context", lambda *a, **k: ctx)
    rows = setup_rows(end=(now - timedelta(minutes=5)).replace(minute=0))
    monkeypatch.setattr(market_module, "fetch_coinbase",
                        fake_fetch({"SOL": rows}, retrieved_at=now - timedelta(minutes=3)))
    seen = recording_app(monkeypatch)
    app = ["--base-url", "https://app.example", "--token-file", str(token_path)]
    folder = ["--run-dir", str(run_dir)]

    def cli(*argv):
        assert run.main(list(argv)) == 0, capsys.readouterr()

    cli("context", *app, *folder)
    cli("lessons", *folder, "--now", now.isoformat())
    cli("market", "--profile", "intraday", *folder)
    cli("levels", "--profile", "intraday", *folder)
    cli("outlook", *folder, "--now", now.isoformat())
    sheet = json.loads((run_dir / "outlook.json").read_text())
    for entry in sheet["coins"]:
        entry.update(direction="FLAT", confidence="0.5", reasons=[])
    sheet["market"].update(summary="Quiet.", btc={"direction": "FLAT", "confidence": "0.5"},
                           eth={"direction": "FLAT", "confidence": "0.5"})
    (run_dir / "outlook.json").write_text(json.dumps(sheet))
    cli("outlook-check", *folder, *AGENT)
    cli("build", "--profile", "intraday", *folder, *AGENT, "--now", now.isoformat())
    cli("validate", *folder, "--now", now.isoformat())
    cli("submit", *folder, *app)
    [(_path, _auth, report)] = seen
    assert report["run_slot"] == "2026-09-29T08:00:00-04:00"  # The daily run, a FULL run.
    assert [pick["symbol"] for pick in report["picks"]] == ["SOL/USD"]
    assert not (run_dir / "withdrawal.json").exists()


# --- Method v9: the evidence premise of a watched setup -----------------------------------------

def _premise_context(**overrides):
    ctx = scenario_context()
    for row in ctx["universe"]["coins"]:
        if row["symbol"] in overrides:
            row["volume_24h"] = {"base": "1", "usd": overrides[row["symbol"]],
                                 "completed_hour_bars": 24}
    return ctx


def test_premise_review_withdraws_a_kept_or_adjusted_setup_whose_evidence_fails():
    """Method v9 (owner direction 2026-10-02, the two-hourly review of the levels in the
    system): a watched setup the review keeps (SOL) or adjusts (ETH) is re-checked against
    --exclude, --min-alpaca-volume-usd and --trend-floor-pct with the same facts and texts as
    a new pick, and withdrawn with the reason when one fails. Pure, on the review's result."""
    ctx = _premise_context(**{"SOL/USD": "100.00"})
    raw_market = market_doc(SCENARIO["rows"])
    result = update.review(ctx, raw_market, levels_doc(ctx, raw_market), profile=PROFILE,
                           now=UPDATE_AT)
    assert {d.symbol: d.action for d in result.decisions} == {
        "SOL/USD": update.KEPT, "ETH/USD": update.ADJUSTED, "AVAX/USD": update.WITHDRAWN}
    assert update.premise_review(result, ctx, raw_market, None) is result

    excluded = update.premise_review(result, ctx, raw_market,
                                     build_module.EvidenceFilter(exclude=frozenset({"ETH"})))
    by_symbol = {d.symbol: d for d in excluded.decisions}
    assert by_symbol["ETH/USD"].action == update.WITHDRAWN
    assert by_symbol["ETH/USD"].reason == (update.PREMISE_PREFIX
                                           + build_module.EXCLUDED_SKIP_REASON)
    assert by_symbol["ETH/USD"].setup is None and excluded.of(update.ADJUSTED) == []
    assert by_symbol["SOL/USD"].action == update.KEPT
    assert by_symbol["AVAX/USD"] == {d.symbol: d for d in result.decisions}["AVAX/USD"]

    thin = update.premise_review(result, ctx, raw_market, build_module.EvidenceFilter(
        min_alpaca_volume_usd=Decimal("5000")))
    by_symbol = {d.symbol: d for d in thin.decisions}
    assert by_symbol["SOL/USD"].action == update.WITHDRAWN
    assert by_symbol["SOL/USD"].reason.startswith(
        update.PREMISE_PREFIX + "Alpaca 24 h volume $100 is under the evidence minimum $5,000")
    assert by_symbol["ETH/USD"].action == update.ADJUSTED  # $100,000 a day: unchanged.

    # A fill of the coin in the context's window is evidence enough, as for a new pick.
    ctx["recent_outcomes"] = {"window_days": 7, "closed_trades": [
        {"symbol": "SOL/USD", "closed_at": "2026-09-28T10:00:00+00:00"}]}
    filled = update.premise_review(result, ctx, raw_market, build_module.EvidenceFilter(
        min_alpaca_volume_usd=Decimal("5000")))
    assert {d.symbol: d.action for d in filled.decisions}["SOL/USD"] == update.KEPT

    # Without 20 completed daily bars (the fixture market has none) a floor withdraws both.
    floored = update.premise_review(result, ctx, raw_market, build_module.EvidenceFilter(
        trend_floor=Decimal("0")))
    assert [(d.symbol, d.action) for d in floored.decisions] == [
        ("AVAX/USD", update.WITHDRAWN), ("ETH/USD", update.WITHDRAWN),
        ("SOL/USD", update.WITHDRAWN)]
    assert {d.symbol: d.reason for d in floored.decisions}["SOL/USD"] == (
        update.PREMISE_PREFIX + "No 20 completed daily bars to judge the trend against its "
        "20-day average.")


def test_premise_review_leaves_an_unchecked_setup_and_the_other_rules_alone():
    """A setup the review could not re-check (no market data for the coin) stays KEPT even
    when it fails a rule; the distance and sell-off rules never touch a watched setup."""
    ctx = _premise_context(**{"SOL/USD": "100.00"})
    rows = {coin: rows for coin, rows in SCENARIO["rows"].items() if coin != "SOL"}
    raw_market = market_doc(rows)
    result = update.review(ctx, raw_market, levels_doc(ctx, raw_market), profile=PROFILE,
                           now=UPDATE_AT)
    sol = {d.symbol: d for d in result.decisions}["SOL/USD"]
    assert sol.action == update.KEPT and not sol.checked
    strict = build_module.EvidenceFilter(exclude=frozenset({"SOL"}),
                                         min_alpaca_volume_usd=Decimal("5000"),
                                         max_entry_distance=Decimal("0.001"),
                                         selloff=Decimal("0.0001"))
    after = update.premise_review(result, ctx, raw_market, strict)
    assert {d.symbol: d for d in after.decisions}["SOL/USD"] == sol
    assert {d.symbol: d.action for d in after.decisions}["ETH/USD"] == update.ADJUSTED


def test_the_update_cli_withdraws_a_watched_setup_whose_premise_fails(tmp_path, monkeypatch):
    """Through the CLI (method v9): --exclude sol turns the kept SOL setup into a withdrawal
    item with the premise reason, the notes record it, and the new coin DOGE still goes out;
    without an evidence option nothing changes."""
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], "--exclude", "sol")
    assert code == 0
    withdrawal = json.loads((run_dir / "withdrawal.json").read_text())
    items = {item["symbol"]: item["reason"] for item in withdrawal["items"]}
    assert items["SOL/USD"] == update.PREMISE_PREFIX + build_module.EXCLUDED_SKIP_REASON
    assert "AVAX/USD" in items and len(items) == 2
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert [d["symbol"] for d in notes["kept"]] == []
    assert [d["symbol"] for d in notes["withdrawn"]] == ["AVAX/USD", "SOL/USD"]
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, scenario_context(),
                                         SCENARIO["rows"], name="plain")
    assert code == 0
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert [d["symbol"] for d in notes["kept"]] == ["SOL/USD"]


# --- update --lessons (package learning-loop2, plan L3) ------------------------------------------


def test_update_reads_the_lessons_from_its_own_context(tmp_path, monkeypatch, capsys):
    """``update --lessons``: the hints come from the context the update reads now, are written
    to emphasis.json and recorded in the notes with the brief's research focus; the adjusted
    pick stays first and no coin is dropped."""
    from tests.test_research_agent_lessons import full_lessons

    ctx = scenario_context()
    ctx["lessons"] = {**full_lessons(), "daily_brief": {
        "day": "2026-09-28", "market": {"regime_tag": "UP/HIGH/BROAD/NO_SELLOFF"},
        "movers": [], "sector_clusters": [],
        "research_focus": [{"kind": "SECTOR_ATTENTION", "text": "Watch L1.",
                            "scope": "RESEARCH_ATTENTION_ONLY"}]}}
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, ctx, SCENARIO["rows"],
                                         "--lessons")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "lessons: Emphasis for today" in out and "lessons:   SECTOR_ATTENTION: Watch L1." in out
    emphasis = json.loads((run_dir / "emphasis.json").read_text())
    assert [hint["dimension"] for hint in emphasis["hints"]] == ["distance_bucket", "timeframe"]
    assert emphasis["rules"]["ranking"] == "NET_R_PER_RESOLVED_PICK_V1"
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]
    notes = json.loads((run_dir / "update-notes.json").read_text())
    assert notes["lessons"]["source"] == "context.json"
    assert len(notes["lessons"]["hints"]) == 2
    assert notes["lessons"]["research_focus"] == ["Watch L1."]
    assert "never drops a coin" in notes["lessons"]["use"]
    assert notes["build"]["lessons"]["order_before"][0] == "ETH/USD"
    # Without the option nothing changes and the notes say so.
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, ctx, SCENARIO["rows"],
                                         name="update-1200")
    assert code == 0
    assert json.loads((run_dir / "update-notes.json").read_text())["lessons"] is None
    assert not (run_dir / "emphasis.json").exists()


def test_update_lessons_without_lessons_or_with_a_bad_file(tmp_path, monkeypatch, capsys):
    ctx = scenario_context()  # No lessons in this context: no hints, the same report.
    code, run_dir, _token_path = _update(tmp_path, monkeypatch, ctx, SCENARIO["rows"],
                                         "--lessons")
    assert code == 0
    assert json.loads((run_dir / "emphasis.json").read_text())["hints"] == []
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["symbol"] for pick in report["picks"]] == ["ETH/USD", "DOGE/USD"]
    bad = tmp_path / "bad-emphasis.json"
    bad.write_text(json.dumps({"schema": "RESEARCH_AGENT_EMPHASIS_V1", "hints": [
        {"dimension": "distance_bucket", "prefer": ["0-1%"], "text": "t", "drop": ["3%+"]}]}))
    code, _run_dir, _token_path = _update(tmp_path, monkeypatch, ctx, SCENARIO["rows"],
                                          "--lessons", str(bad), name="update-1400")
    assert code == 2 and "EMPHASIS_INVALID" in capsys.readouterr().err


def test_adjusted_picks_stay_first_under_any_emphasis():
    """build_report with ``first_coins`` (an update's adjusted picks) and a hint that prefers
    the new coin's bucket: the adjusted pick still comes first; the hint orders the rest."""
    from research_agent import lessons as lessons_module
    from tests.learning_kit_fixtures import NOW, context_v2
    from tests.test_research_agent_lessons import _setup_json

    retrieved_at = NOW - timedelta(minutes=5)
    levels_by_coin = {"FAR": (levels.setup_from_json(_setup_json(retrieved_at, 95)), []),
                      "NEAR": (levels.setup_from_json(_setup_json(retrieved_at, 99.2)), [])}
    hints = lessons_module.load_emphasis({"schema": lessons_module.EMPHASIS_SCHEMA, "hints": [
        {"dimension": "distance_bucket", "prefer": ["0-1%"], "text": "near entries first"}]})
    common = {"context": context_v2(["FAR/USD", "NEAR/USD"]),
              "market_data": {"retrieved_at": retrieved_at.isoformat(), "coinbase": {},
                              "excluded": {}},
              "levels_by_coin": levels_by_coin, "agent_id": "claude",
              "agent_version": "kit-test-1", "now": NOW, "emphasis": hints}
    plain = build_module.build_report(**common)
    assert [o.symbol for o in plain.accepted] == ["NEAR/USD", "FAR/USD"]
    first = build_module.build_report(**common, first_coins=frozenset({"FAR"}))
    assert [o.symbol for o in first.accepted] == ["FAR/USD", "NEAR/USD"]
