"""``STRATEGY_PLUGIN_LOADER_V1``: drop-in strategy plug-ins (package plugin-c3, 2026-10-03; plan
docs/TRADING-QUALITY-PLAN.md section 12 item 3, "a drop-in plug-ins folder/package so a strategy
needs no core edits"; guide docs/STRATEGY-PLUGINS.md).

Besides the built-ins registered in ``strategies/__init__.py``, a strategy can come from:

* **a folder** (``CATALYST_STRATEGY_PLUGINS_DIR``, or ``folder=`` / ``--plugins-dir``): every
  ``*.py`` file not starting with ``_``, imported in name order under the module name
  ``catalyst_strategy_plugins.<stem>``;
* **an installed package** declaring the entry-point group ``catalyst_lab.strategies``
  (``[project.entry-points."catalyst_lab.strategies"]``), each entry loading a ``Strategy``, a
  module, or a callable returning one or several.

A module offers its strategies as ``STRATEGY`` (one record) or ``STRATEGIES`` (a sequence). Every
record is validated before it joins the registry (``validate``): it must be a ``Strategy``
(which checks its own id, version, entry types, plan rules, stage and sources), must not clash
with a built-in's id or name, must not declare ``LIVE_PAPER`` (a plug-in reaches the paper account
only through an owner's ``STRATEGY_PROMOTION`` record, never by its own declaration), and its
``signals``/``simulate`` must take the interface's arguments. A refused plug-in raises
``PluginError`` with a code; the loading summary (``LoadResult``) lists what joined and what was
refused. Nothing here reads a ledger, a broker or a clock; importing a plug-in runs its code, so
only folders and packages the owner installed are ever read.
"""

import importlib.util
import inspect
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import ModuleType

from catalyst_lab.strategies.base import LIVE_PAPER, Strategy

LOADER_VERSION = "STRATEGY_PLUGIN_LOADER_V1"
ENTRY_POINT_GROUP = "catalyst_lab.strategies"
PLUGINS_DIR_ENV = "CATALYST_STRATEGY_PLUGINS_DIR"
MODULE_PREFIX = "catalyst_strategy_plugins"


class PluginError(ValueError):
    """A refused plug-in: ``code`` (``PLUGIN_*``) and the plug-in's origin."""

    def __init__(self, code, origin=None):
        super().__init__(code)
        self.code, self.origin = code, origin


@dataclass
class LoadResult:
    loaded: list = field(default_factory=list)  # [(strategy_id, origin)]
    errors: list = field(default_factory=list)  # [(origin, code)]

    def record(self):
        return {"version": LOADER_VERSION,
                "loaded": [{"strategy_id": s, "origin": o} for s, o in self.loaded],
                "errors": [{"origin": o, "code": c} for o, c in self.errors]}


def _accepts(function, positional, keywords=()):
    """Whether ``function`` can be called with ``positional`` positional arguments and the
    ``keywords`` keyword arguments (the interface's calls)."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    try:
        signature.bind(*range(positional), **dict.fromkeys(keywords))
    except TypeError:
        return False
    return True


def validate(strategy, *, builtins, origin=None):
    """Refuse a plug-in record that is not a clean, separate, non-live strategy; returns it."""
    if not isinstance(strategy, Strategy):
        raise PluginError("PLUGIN_NOT_A_STRATEGY", origin)
    builtin_names = {s.name for s in builtins.values()}
    if strategy.strategy_id in builtins or strategy.name in builtin_names:
        raise PluginError("PLUGIN_CLASHES_WITH_BUILTIN", origin)
    if strategy.stage == LIVE_PAPER:
        raise PluginError("PLUGIN_MAY_NOT_DECLARE_LIVE_PAPER", origin)
    if type(strategy.version) is not int or strategy.version < 1:
        raise PluginError("PLUGIN_VERSION_INVALID", origin)
    if not strategy.trigger_rule or not strategy.plan_rules:
        raise PluginError("PLUGIN_RULES_UNDECLARED", origin)
    if strategy.signals is not None and not _accepts(strategy.signals, 2):
        raise PluginError("PLUGIN_SIGNALS_SIGNATURE", origin)
    if strategy.simulate is not None and not _accepts(strategy.simulate, 2, ("fee_rate",)):
        raise PluginError("PLUGIN_SIMULATE_SIGNATURE", origin)
    if strategy.live_trigger is not None:
        raise PluginError("PLUGIN_MAY_NOT_DECLARE_LIVE_TRIGGER", origin)
    return strategy


def strategies_of(obj, origin=None):
    """The ``Strategy`` records a module, record, callable or sequence offers."""
    if isinstance(obj, Strategy):
        return [obj]
    if isinstance(obj, ModuleType):
        if hasattr(obj, "STRATEGIES"):
            return strategies_of(list(obj.STRATEGIES), origin)
        if hasattr(obj, "STRATEGY"):
            return strategies_of(obj.STRATEGY, origin)
        raise PluginError("PLUGIN_MODULE_DECLARES_NO_STRATEGY", origin)
    if isinstance(obj, (list, tuple)):
        found = []
        for item in obj:
            if not isinstance(item, Strategy):
                raise PluginError("PLUGIN_NOT_A_STRATEGY", origin)
            found.append(item)
        if not found:
            raise PluginError("PLUGIN_MODULE_DECLARES_NO_STRATEGY", origin)
        return found
    if callable(obj):
        return strategies_of(obj(), origin)
    raise PluginError("PLUGIN_NOT_A_STRATEGY", origin)


def import_file(path):
    """Import one plug-in file under ``catalyst_strategy_plugins.<stem>`` (kept in
    ``sys.modules`` so the history tester can find the module of its ``signals``)."""
    path = Path(path)
    name = f"{MODULE_PREFIX}.{path.stem}"
    if name in sys.modules and getattr(sys.modules[name], "__file__", None) == str(path):
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError("PLUGIN_FILE_UNREADABLE", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except PluginError:
        sys.modules.pop(name, None)
        raise
    except Exception:  # noqa: BLE001 -- any import failure refuses the plug-in by code.
        sys.modules.pop(name, None)
        raise PluginError("PLUGIN_IMPORT_FAILED", str(path)) from None
    return module


def folder_candidates(folder):
    """``(origin, loader)`` for every plug-in file of ``folder``, in name order."""
    folder = Path(folder)
    if not folder.is_dir():
        raise PluginError("PLUGIN_FOLDER_MISSING", str(folder))
    return [(str(path), lambda path=path: import_file(path))
            for path in sorted(folder.glob("*.py")) if not path.name.startswith("_")]


def entry_point_candidates(group=ENTRY_POINT_GROUP):
    """``(origin, loader)`` for every installed entry point of the group, in name order."""
    found = metadata.entry_points(group=group)
    return [(f"entry-point:{ep.name}={ep.value}", ep.load)
            for ep in sorted(found, key=lambda ep: ep.name)]


def load_into(registry, candidates: Iterable, *, builtins, strict=False):
    """Validate and add every candidate's strategies to ``registry``; returns ``LoadResult``.

    A plug-in that is refused (or whose id another plug-in already took) is recorded in
    ``errors`` and skipped; with ``strict`` the first refusal raises instead."""
    result = LoadResult()
    for origin, load in candidates:
        try:
            try:
                obj = load()
            except PluginError:
                raise
            except Exception:  # noqa: BLE001 -- an entry point that fails to import.
                raise PluginError("PLUGIN_IMPORT_FAILED", origin) from None
            found = [validate(s, builtins=builtins, origin=origin)
                     for s in strategies_of(obj, origin)]
            for strategy in found:
                existing = registry.get(strategy.strategy_id)
                if existing is not None and existing is not strategy:
                    raise PluginError("PLUGIN_DUPLICATE_STRATEGY_ID", origin)
            for strategy in found:
                if registry.get(strategy.strategy_id) is not strategy:
                    registry[strategy.strategy_id] = strategy
                    result.loaded.append((strategy.strategy_id, origin))
        except PluginError as exc:
            if strict:
                raise
            result.errors.append((origin, exc.code))
    return result


def configured_folder(environ=None):
    """The first configured folder (see ``configured_folders``), or None."""
    folders = configured_folders(environ)
    return folders[0] if folders else None


def configured_folders(environ=None):
    """``CATALYST_STRATEGY_PLUGINS_DIR``: one folder, or several separated by ``os.pathsep``
    (package oss-packaging: the local stack loads the examples and the user's own folder)."""
    value = (os.environ if environ is None else environ).get(PLUGINS_DIR_ENV, "").strip()
    return [Path(part.strip()) for part in value.split(os.pathsep) if part.strip()]


__all__ = ["ENTRY_POINT_GROUP", "LOADER_VERSION", "LoadResult", "MODULE_PREFIX",
           "PLUGINS_DIR_ENV", "PluginError", "configured_folder", "configured_folders",
           "entry_point_candidates",
           "folder_candidates", "import_file", "load_into", "strategies_of", "validate"]
