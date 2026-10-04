"""Package plugin-c3: drop-in strategy plug-ins (``STRATEGY_PLUGIN_LOADER_V1``), the stable SDK
(``STRATEGY_SDK_V1``), the ``new-strategy`` template and the example plug-in through the history
tester and the shadow.

Fixture evidence only: temporary folders, canned public bars, a temporary bar cache, per-test
disposable PostgreSQL databases (the shadow test) and no network. Nothing here places an order.
"""

import json
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import httpx
import pytest

from catalyst_lab import cli, strategies
from catalyst_lab import strategy_shadow as ss
from catalyst_lab.strategies import core, loader, sdk, template
from catalyst_lab.strategies.base import LIVE_PAPER, SHADOW
from tests.learning_fixtures import learning  # noqa: F401 -- fixture
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_history_test import write_fixture_cache
from tests.test_strategy_plugins import (
    test_every_registered_strategy_conforms_to_the_interface as conforms,
)
from tests.test_strategy_shadow import NOW, events, reader, universe

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "strategies"
EXAMPLE_ID = "EXAMPLE_MA_CROSS_V1"


@pytest.fixture
def registry():
    """The registry and the once-per-process load, restored after the test."""
    saved, loaded = dict(strategies.REGISTRY), strategies._PLUGINS["loaded"]
    yield strategies.REGISTRY
    strategies.REGISTRY.clear()
    strategies.REGISTRY.update(saved)
    strategies._PLUGINS["loaded"] = loaded


def plugin(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)
    return folder / name


GOOD = '''
from catalyst_lab.strategies import sdk

def signals(bars, context):
    return []

STRATEGY = sdk.mechanical_strategy(name="{name}", version=1, description="fixture plug-in",
                                   signals=signals, trigger_rule="MARKETABLE_AT_SIGNAL (fixture)")
'''


# --- Loading and validation ---------------------------------------------------------------------


def test_a_folder_plugin_joins_the_registry_and_conforms(tmp_path, registry):
    folder = tmp_path / "plugins"
    plugin(folder, "fixture_one_v1.py", GOOD.format(name="FIXTURE_ONE"))
    plugin(folder, "_private.py", "raise RuntimeError('never imported')")
    plugin(folder, "notes.txt", "not a plug-in")
    result = strategies.load_plugins(folder=folder, entry_points=False)
    assert result.loaded == [("FIXTURE_ONE_V1", str(folder / "fixture_one_v1.py"))]
    assert result.errors == []
    added = strategies.get("FIXTURE_ONE_V1")
    assert added.stage == SHADOW and added in strategies.mechanical(SHADOW)
    assert sys.modules["catalyst_strategy_plugins.fixture_one_v1"].STRATEGY is added
    assert added.record()["strategy_id"] == "FIXTURE_ONE_V1" and added.mechanical
    # Loading again changes nothing (the same record is already registered).
    assert strategies.load_plugins(folder=folder, entry_points=False).loaded == []
    assert result.record()["version"] == "STRATEGY_PLUGIN_LOADER_V1"


@pytest.mark.parametrize("text,code", [
    (GOOD.format(name="PULLBACK"), "PLUGIN_CLASHES_WITH_BUILTIN"),
    (GOOD.format(name="BREAKOUT_7D_VOL2X"), "PLUGIN_CLASHES_WITH_BUILTIN"),
    ("from catalyst_lab.strategies import sdk\nSTRATEGY = 3\n", "PLUGIN_NOT_A_STRATEGY"),
    ("X = 1\n", "PLUGIN_MODULE_DECLARES_NO_STRATEGY"),
    ("STRATEGIES = []\n", "PLUGIN_MODULE_DECLARES_NO_STRATEGY"),
    ("import nonexistent_module_for_fixture\n", "PLUGIN_IMPORT_FAILED"),
    (GOOD.format(name="FIXTURE_BAD").replace("def signals(bars, context)", "def signals(bars)"),
     "PLUGIN_SIGNALS_SIGNATURE"),
    (GOOD.format(name="FIXTURE_BAD").replace(
        "trigger_rule=", "simulate=lambda proposal, bars: None, trigger_rule="),
     "PLUGIN_SIMULATE_SIGNATURE"),
    (GOOD.format(name="FIXTURE_BAD").replace("version=1,", "version=1, stage=sdk.LIVE_PAPER,"),
     "PLUGIN_IMPORT_FAILED"),  # A LIVE_PAPER record without a live trigger fails to build.
])
def test_a_refused_plugin_is_reported_and_never_registered(tmp_path, registry, text, code):
    folder = tmp_path / "plugins"
    plugin(folder, "fixture_bad_v1.py", text)
    before = dict(registry)
    result = strategies.load_plugins(folder=folder, entry_points=False)
    assert result.loaded == [] and [c for _, c in result.errors] == [code]
    assert dict(registry) == before
    with pytest.raises(loader.PluginError) as refused:
        strategies.load_plugins(folder=folder, entry_points=False, strict=True)
    assert refused.value.code == code


def test_validation_refuses_live_paper_live_triggers_and_duplicates(tmp_path, registry):
    from dataclasses import replace

    base = sdk.mechanical_strategy(name="FIXTURE_V", version=1, description="d",
                                   signals=lambda bars, context: [], trigger_rule="r")
    with pytest.raises(loader.PluginError, match="PLUGIN_MAY_NOT_DECLARE_LIVE_PAPER"):
        loader.validate(replace(base, stage=LIVE_PAPER, live_trigger=lambda s: None),
                        builtins=strategies.BUILTINS)
    with pytest.raises(loader.PluginError, match="PLUGIN_MAY_NOT_DECLARE_LIVE_TRIGGER"):
        loader.validate(replace(base, live_trigger=lambda s: None), builtins=strategies.BUILTINS)
    folder = tmp_path / "plugins"
    plugin(folder, "a_v1.py", GOOD.format(name="FIXTURE_DUP"))
    plugin(folder, "b_v1.py", GOOD.format(name="FIXTURE_DUP"))
    result = strategies.load_plugins(folder=folder, entry_points=False)
    assert [s for s, _ in result.loaded] == ["FIXTURE_DUP_V1"]
    assert [c for _, c in result.errors] == ["PLUGIN_DUPLICATE_STRATEGY_ID"]
    missing = strategies.load_plugins(folder=tmp_path / "nowhere", entry_points=False)
    assert [c for _, c in missing.errors] == ["PLUGIN_FOLDER_MISSING"]


def test_entry_points_load_records_modules_and_factories(registry, monkeypatch):
    record = sdk.mechanical_strategy(name="FIXTURE_EP", version=2, description="d",
                                     signals=lambda bars, context: [], trigger_rule="r")

    class EntryPoint:
        def __init__(self, name, value, target):
            self.name, self.value, self.target = name, value, target

        def load(self):
            if isinstance(self.target, Exception):
                raise self.target
            return self.target

    found = [EntryPoint("b", "pkg:factory", lambda: [record]),
             EntryPoint("a", "pkg:broken", ImportError("fixture"))]
    monkeypatch.setattr(metadata, "entry_points",
                        lambda group: found if group == "catalyst_lab.strategies" else [])
    result = strategies.load_plugins()
    assert result.loaded == [("FIXTURE_EP_V2", "entry-point:b=pkg:factory")]
    assert result.errors == [("entry-point:a=pkg:broken", "PLUGIN_IMPORT_FAILED")]
    assert strategies.get("FIXTURE_EP_V2") is record


def test_ensure_plugins_loaded_reads_the_configured_folder_once(tmp_path, registry):
    folder = tmp_path / "plugins"
    plugin(folder, "fixture_env_v1.py", GOOD.format(name="FIXTURE_ENV"))
    strategies._PLUGINS["loaded"] = None
    first = strategies.ensure_plugins_loaded({loader.PLUGINS_DIR_ENV: str(folder)})
    assert [s for s, _ in first.loaded] == ["FIXTURE_ENV_V1"]
    assert strategies.ensure_plugins_loaded({}) is first  # Once per process.


# --- The SDK and the template -------------------------------------------------------------------


def test_the_sdk_surface_is_stable():
    assert sdk.SDK_VERSION == "STRATEGY_SDK_V1"
    for name in sdk.__all__:
        assert hasattr(sdk, name), name
    assert sdk.hourly_range_at is core.hourly_range_at and sdk.reaches_stop is core.reaches_stop
    record = sdk.mechanical_strategy(name="FIXTURE_SDK", version=3, description="d",
                                     signals=lambda bars, context: [], trigger_rule="r")
    assert (record.strategy_id, record.plan_rules, sorted(record.sources), record.stage) == (
        "FIXTURE_SDK_V3", "CRYPTO_TRADE_PLAN_V1", ["MECHANICAL"], SHADOW)
    assert record.simulate is sdk.simulate_marketable_proposal and record.mechanical
    assert strategies.paper_eligible(record)


def test_new_strategy_writes_a_skeleton_that_loads_and_conforms(tmp_path, registry, capsys,
                                                                monkeypatch):
    folder = tmp_path / "mine"
    monkeypatch.setattr(sys, "argv", ["catalyst-lab", "new-strategy", "MY_FIRST",
                                      "--dir", str(folder)])
    cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["strategy_id"] == "MY_FIRST_V1" and printed["stage"] == "SHADOW"
    assert Path(printed["written"]) == folder / "my_first_v1.py"
    result = strategies.load_plugins(folder=folder, entry_points=False, strict=True)
    assert [s for s, _ in result.loaded] == ["MY_FIRST_V1"]
    skeleton = strategies.get("MY_FIRST_V1")
    conforms(skeleton)
    with pytest.raises(SystemExit, match="STRATEGY_FILE_EXISTS"):
        cli.main()  # Never overwrites.
    for name, code in (("PULLBACK", "STRATEGY_NAME_IS_BUILTIN"), ("lower", "STRATEGY_NAME_INVALID"),
                       ("ENDS_V2", "STRATEGY_NAME_INVALID")):
        monkeypatch.setattr(sys, "argv", ["catalyst-lab", "new-strategy", name,
                                          "--dir", str(folder)])
        with pytest.raises(SystemExit, match=code):
            cli.main()
    assert template.render("ANOTHER").count("ANOTHER_V1") >= 2


def test_the_template_rule_signals_on_a_new_high(tmp_path, registry):
    from datetime import timedelta
    from decimal import Decimal as D

    folder = tmp_path / "mine"
    template.write("NEW_HIGH", folder)
    strategies.load_plugins(folder=folder, entry_points=False, strict=True)
    strategy = strategies.get("NEW_HIGH_V1")
    start = datetime(2026, 6, 1, tzinfo=UTC)
    bars = [sdk.Bar(start + timedelta(hours=n), D(100), D(100), D(100), D(100), D(10))
            for n in range(30)]
    bars.append(sdk.Bar(start + timedelta(hours=30), D(100), D(102), D(100), D(101), D(10)))
    proposals = strategy.signals(bars, {"symbol": "AAA/USD", "since": start,
                                        "until": start + timedelta(hours=31)})
    assert [p["signal_at"] for p in proposals] == [(start + timedelta(hours=31)).isoformat()]
    assert proposals[0]["entry"] == "MARKETABLE_AT_SIGNAL"
    assert proposals[0]["reference_price"] == "101"


# --- The example plug-in end to end: history test and shadow ------------------------------------


def test_the_example_plugin_runs_through_the_history_tester(tmp_path, monkeypatch, registry):
    def no_network(*args, **kwargs):
        raise AssertionError("NETWORK_USED")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    start, end = datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 11, tzinfo=UTC)
    write_fixture_cache(tmp_path / "cache", start, end)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "catalyst-lab", "history-test", "--offline", "--cache-dir", str(tmp_path / "cache"),
        "--plugins-dir", str(EXAMPLES), "--strategy", EXAMPLE_ID,
        "--universe", "AAA/USD,BBB/USD,CCC/USD,BTC/USD", "--start", "2026-06-01",
        "--end", "2026-07-11", "--is-days", "10", "--oos-days", "5", "--resamples", "100",
        "--out", str(out), "--quiet"])
    cli.main()
    results = json.loads((out / "results.json").read_text())
    example = results["strategies"][EXAMPLE_ID]
    assert set(example["variants"]) == {"FAST_12_SLOW_48", "FAST_6_SLOW_24"}
    registered = example["variants"]["FAST_12_SLOW_48"]
    assert registered["registered_rule"] and registered["signals"] >= 1
    assert sum(registered["simulated_outcomes"].values()) == registered["signals"]
    assert results["trials"]["recorded_this_run"] == 2
    assert (out / "REPORT.md").read_text().startswith("# History test")


def test_the_example_plugin_runs_in_shadow_beside_the_breakout(learning, registry):  # noqa: F811
    strategies.load_plugins(folder=EXAMPLES, entry_points=False, strict=True)
    store = learning.store
    universe(store, ["AAA/USD", "BBB/USD", "QQQ/USD"])
    code, details = ss.run_strategy_shadow(store, reader(), now=NOW)
    assert code is None
    signals = [s for s in events(store, ss.SIGNAL_EVENT) if s["strategy_id"] == EXAMPLE_ID]
    assert signals and all(s["proposal"]["entry"] == "MARKETABLE_AT_SIGNAL" for s in signals)
    assert {s["symbol"] for s in signals} <= {"AAA/USD", "BBB/USD"}
    outcomes = [o for o in events(store, ss.OUTCOME_EVENT) if o["strategy_id"] == EXAMPLE_ID]
    assert outcomes and all(o["orders"] == "NONE_SHADOW_ONLY" for o in outcomes)
    cells = ss.shadow_cells(store.repo, end=NOW)
    assert cells[EXAMPLE_ID]["label"] == "SHADOW"
    assert "BREAKOUT_7D_VOL2X_V1" in cells  # The built-in is still measured beside it.
