"""The teaching plug-ins in ``examples/strategies`` and the generated sample market
(``SYNTHETIC_SAMPLE_BARS_V1``), package oss-packaging.

Fixture evidence only: generated bars, temporary folders, a disposable PostgreSQL for the shadow
and no network. The examples are teaching examples, not profitable strategies; a run on
synthetic bars is never evidence, and these tests check that the tools say so.
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from catalyst_lab import cli, strategies
from catalyst_lab import strategy_shadow as ss
from catalyst_lab.history_bars import BarCache
from catalyst_lab.pick_outcomes import parse_bars
from catalyst_lab.sample_market import EPOCH, SYMBOLS, SampleMarketError, SyntheticBarReader
from catalyst_lab.strategies import sdk
from catalyst_lab.strategy_paper import PromotionRefused, history_entry
from tests.learning_fixtures import learning  # noqa: F401 -- fixture
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_strategy_plugins import (
    test_every_registered_strategy_conforms_to_the_interface as conforms,
)
from tests.test_strategy_shadow import events

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "strategies"
EXAMPLE_IDS = ("EXAMPLE_DONCHIAN_BREAKOUT_V1", "EXAMPLE_MA_CROSS_V1", "EXAMPLE_RSI_REVERSION_V1")
NOW = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)


@pytest.fixture
def registry():
    saved, loaded = dict(strategies.REGISTRY), strategies._PLUGINS["loaded"]
    yield strategies.REGISTRY
    strategies.REGISTRY.clear()
    strategies.REGISTRY.update(saved)
    strategies._PLUGINS["loaded"] = loaded


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("NETWORK_USED")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


# --- The sample market ---------------------------------------------------------------------------


def test_sample_bars_are_deterministic_consistent_and_never_from_the_future():
    first = SyntheticBarReader(now=NOW)
    second = SyntheticBarReader(now=NOW)
    start = datetime(2026, 8, 30, tzinfo=UTC)
    hours = first.bars("ETH/USD", start, NOW + timedelta(days=5), "1Hour")
    assert hours == second.bars("ETH/USD", start, NOW + timedelta(days=5), "1Hour")
    parsed = parse_bars(hours)  # Valid bars (low <= open, close <= high), ascending.
    assert parsed[-1].start + timedelta(hours=1) <= NOW  # Completed bars only.
    assert len(parsed) == 60  # 2.5 days to 12:30 -> 60 completed hours.
    # Each hourly bar is the aggregate of its 60 minutes.
    bar = parsed[10]
    minutes = parse_bars(first.minute_bars("ETH/USD", bar.start, bar.start + timedelta(hours=1)))
    assert len(minutes) == 60
    assert (minutes[0].open, minutes[-1].close) == (bar.open, bar.close)
    assert max(m.high for m in minutes) == bar.high and min(m.low for m in minutes) == bar.low
    assert sum((m.volume for m in minutes), D(0)) == bar.volume
    # Consecutive hours join: an hour opens at the previous close.
    assert all(a.close == b.open for a, b in zip(parsed, parsed[1:], strict=False))
    # Pinned values: a change to the generator is a new version, never an edit.
    assert first.hour_close("BTC/USD", 0) == SyntheticBarReader().hour_close("BTC/USD", 0)
    assert str(first.hour_close("BTC/USD", -1)) == "60000.00"
    assert first.bars("BTC/USD", EPOCH - timedelta(days=2), EPOCH, "1Hour") == []


def test_the_sample_market_refuses_what_it_does_not_have():
    reader = SyntheticBarReader(now=NOW)
    with pytest.raises(SampleMarketError, match="SAMPLE_SYMBOL_UNKNOWN"):
        reader.bars("LINK/USD", NOW - timedelta(days=1), NOW, "1Hour")
    with pytest.raises(SampleMarketError, match="INVALID_BAR_TIMEFRAME"):
        reader.bars("BTC/USD", NOW - timedelta(days=1), NOW, "1Day")
    with pytest.raises(SampleMarketError, match="INVALID_BAR_WINDOW"):
        reader.bars("BTC/USD", NOW, NOW, "1Hour")
    assert set(SYMBOLS) == {"BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"}


def test_the_bar_cache_reads_synthetic_bars_offline_and_caches_nothing(tmp_path, no_network):
    cache = BarCache("synthetic", cache_dir=tmp_path, offline=True, now=NOW)
    bars = cache.hourly("SOL/USD", NOW - timedelta(days=3), NOW - timedelta(days=1))
    assert len(bars) == 48 and not any(tmp_path.rglob("*.json"))


# --- The examples --------------------------------------------------------------------------------


def test_every_example_loads_strictly_and_conforms(registry):
    result = strategies.load_plugins(folder=EXAMPLES, entry_points=False, strict=True)
    assert sorted(s for s, _ in result.loaded) == list(EXAMPLE_IDS)
    for sid in EXAMPLE_IDS:
        strategy = strategies.get(sid)
        if sid != "EXAMPLE_RSI_REVERSION_V1":  # The shared check feeds a breakout series.
            conforms(strategy)
        assert strategy.record()["strategy_id"] == sid
        assert strategy.plan_rules == "CRYPTO_TRADE_PLAN_V1"
        assert strategy.stage == sdk.SHADOW and strategy.mechanical
        assert len(strategy.history_variants) >= 2  # Declared before any run.
        module = sys.modules["catalyst_strategy_plugins." + Path(
            result.loaded[[s for s, _ in result.loaded].index(sid)][1]).stem]
        assert module.DEFAULT_VARIANT_ID in {v["variant_id"] for v in strategy.history_variants}
        text = Path(module.__file__).read_text()
        assert "PAPER TRADING ONLY" in text and "not" in text.lower()


def test_signals_are_pure_and_only_inside_the_window(registry):
    strategies.load_plugins(folder=EXAMPLES, entry_points=False, strict=True)
    reader = SyntheticBarReader(now=NOW)
    bars = parse_bars(reader.bars("SOL/USD", NOW - timedelta(days=30), NOW, "1Hour"))
    since, until = NOW - timedelta(days=20), NOW.replace(minute=0)
    context = {"symbol": "SOL/USD", "since": since, "until": until}
    for sid in EXAMPLE_IDS:
        strategy = strategies.get(sid)
        first = strategy.signals(bars, dict(context))
        assert first == strategy.signals(bars, dict(context))  # Pure.
        for proposal in first:
            at = datetime.fromisoformat(proposal["signal_at"])
            assert since < at <= until
            assert proposal["entry"] == sdk.MARKETABLE_AT_SIGNAL
            assert proposal["strategy_id"] == sid and proposal["facts"]


def test_rsi_matches_a_hand_computed_value():
    sys.path.insert(0, str(EXAMPLES))
    try:
        import example_rsi_reversion_v1 as rsi
    finally:
        sys.path.remove(str(EXAMPLES))
    closes = [D(x) for x in ("10", "11", "10", "12", "11", "13")]
    series = rsi.rsi_series(closes, 2)
    # Gains/losses: +1 -1 +2 -1 +2. First average (2): gain 0.5, loss 0.5 -> RSI 50.
    assert series[:2] == [None, None] and series[2] == D(50)
    # Next: gain (0.5*1+2)/2 = 1.25, loss 0.25 -> 100 - 100/6.
    assert series[3].quantize(D("0.0001")) == (D(100) - D(100) / 6).quantize(D("0.0001"))
    assert rsi.rsi_series([D(1)] * 3, 5) == [None] * 3


def run_history(tmp_path, monkeypatch, strategy_ids, *extra):
    out = tmp_path / "out"
    argv = ["catalyst-lab", "history-test", "--source", "synthetic", "--plugins-dir",
            str(EXAMPLES), "--start", "2026-06-01", "--end", "2026-07-15", "--is-days", "20",
            "--oos-days", "10", "--resamples", "100", "--out", str(out), "--quiet",
            *[a for sid in strategy_ids for a in ("--strategy", sid)], *extra]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    return out, json.loads((out / "results.json").read_text())


def test_the_examples_run_through_the_history_tester_on_synthetic_bars(tmp_path, monkeypatch,
                                                                         registry, no_network):
    out, results = run_history(tmp_path, monkeypatch, EXAMPLE_IDS)
    assert results["inputs"]["source"] == "SYNTHETIC_SAMPLE_BARS_V1"
    assert results["inputs"]["universe"] == list(SYMBOLS)
    assert results["label"].startswith("SYNTHETIC SAMPLE BARS")
    assert set(results["strategies"]) == set(EXAMPLE_IDS)
    for sid in EXAMPLE_IDS:
        entry = results["strategies"][sid]
        assert entry["promotion"]["rung"] == "NONE" and entry["promotion"]["synthetic"]
        assert entry["promotion"]["reason"] == "SYNTHETIC_DATA_IS_NOT_EVIDENCE"
        assert entry["simulation_errors"] == {}
    assert sum(v["signals"] for v in results["strategies"]["EXAMPLE_MA_CROSS_V1"][
        "variants"].values()) > 0
    # Synthetic trials go to their own ledger, never the one the deflated Sharpe counts.
    assert (tmp_path / "TRIALS-synthetic.jsonl").exists()
    assert not (tmp_path / "TRIALS.jsonl").exists()
    assert "SYNTHETIC SAMPLE BARS" in (out / "REPORT.md").read_text()
    # And the paper promotion refuses the report outright.
    with pytest.raises(PromotionRefused, match="PROMOTION_HISTORY_REPORT_SYNTHETIC"):
        history_entry(out / "results.json", "EXAMPLE_MA_CROSS_V1")


def test_the_history_test_folders_follow_the_environment(tmp_path, monkeypatch, registry,
                                                         no_network):
    monkeypatch.setenv("CATALYST_HISTORY_TEST_OUT", str(tmp_path / "reports"))
    monkeypatch.setenv("CATALYST_HISTORY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(sys, "argv", [
        "catalyst-lab", "history-test", "--source", "synthetic", "--plugins-dir", str(EXAMPLES),
        "--strategy", "EXAMPLE_MA_CROSS_V1", "--universe", "BTC/USD,ETH/USD",
        "--start", "2026-06-20", "--end", "2026-07-05", "--is-days", "5", "--oos-days", "5",
        "--resamples", "50", "--quiet"])
    cli.main()
    reports = list((tmp_path / "reports").glob("*/results.json"))
    assert len(reports) == 1
    assert (tmp_path / "reports" / "TRIALS-synthetic.jsonl").exists()


def test_the_examples_run_in_shadow_on_synthetic_bars(learning, registry):  # noqa: F811
    from catalyst_lab.sample_market import SOURCE_LABEL

    strategies.load_plugins(folder=EXAMPLES, entry_points=False, strict=True)
    store = learning.store
    code, details = ss.run_strategy_shadow(store, SyntheticBarReader(now=NOW), now=NOW,
                                           symbols=["BTC/USD", "SOL/USD"],
                                           bar_source=SOURCE_LABEL)
    assert code is None and details["coins"] == 2 and "universe" not in details
    signals = events(store, ss.SIGNAL_EVENT)
    by_strategy = {s["strategy_id"] for s in signals}
    assert {"EXAMPLE_MA_CROSS_V1", "EXAMPLE_DONCHIAN_BREAKOUT_V1"} <= by_strategy
    assert {s["bars"]["source"] for s in signals} == {SOURCE_LABEL}
    assert all(s["orders"] == "NONE_SHADOW_ONLY" for s in signals)
    outcomes = events(store, ss.OUTCOME_EVENT)
    assert outcomes and all(o["orders"] == "NONE_SHADOW_ONLY" for o in outcomes)
    cells = ss.shadow_cells(store.repo, end=NOW)
    assert cells["EXAMPLE_MA_CROSS_V1"]["label"] == "SHADOW"


def test_the_nightly_default_still_reads_the_recorded_universe(learning, registry):  # noqa: F811
    # No research universe recorded and no symbols given: nothing is scanned (the jobs' rule).
    code, details = ss.run_strategy_shadow(learning.store, SyntheticBarReader(now=NOW), now=NOW)
    assert code is None and details["coins"] == 0
    assert details["universe"] == "STRATEGY_SHADOW_UNIVERSE_UNAVAILABLE"


def test_several_plugin_folders_load_from_the_setting(tmp_path, registry):
    import os

    from catalyst_lab.strategies import loader

    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "mine_v1.py").write_text(
        "from catalyst_lab.strategies import sdk\n"
        "STRATEGY = sdk.mechanical_strategy(name='MINE_FIXTURE', version=1, description='d',\n"
        "    signals=lambda bars, context: [], trigger_rule='r')\n")
    setting = {loader.PLUGINS_DIR_ENV: f"{EXAMPLES}{os.pathsep}{mine}"}
    assert loader.configured_folders(setting) == [EXAMPLES, mine]
    assert loader.configured_folder(setting) == EXAMPLES
    assert loader.configured_folders({}) == [] and loader.configured_folder({}) is None
    strategies._PLUGINS["loaded"] = None
    result = strategies.ensure_plugins_loaded(setting)
    assert {s for s, _ in result.loaded} == {*EXAMPLE_IDS, "MINE_FIXTURE_V1"}
