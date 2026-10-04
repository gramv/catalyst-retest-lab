"""``catalyst-lab new-strategy NAME``: a working plug-in skeleton (package plugin-c3).

The file it writes imports only ``catalyst_lab.strategies.sdk`` (``STRATEGY_SDK_V1``), declares
one ``SHADOW`` mechanical strategy ``NAME_V1`` and passes the loader's validation as written. Its
placeholder rule (an hourly close above the prior 24 hours' high) is there to be replaced; the
guide is docs/STRATEGY-PLUGINS.md ("Write your first strategy").
"""

import re
from pathlib import Path

NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")

TEMPLATE = '''"""{strategy_id}: {description}

A strategy plug-in written against STRATEGY_SDK_V1 (catalyst_lab.strategies.sdk). PAPER TRADING
ONLY: it runs in history tests and in shadow; it can reach the paper account only through the
owner's promotion and the runtime's MANAGED_STRATEGIES_JSON. Replace the placeholder rule below,
then:

    catalyst-lab history-test --plugins-dir {folder} --strategy {strategy_id} --offline
    (shadow: set CATALYST_STRATEGY_PLUGINS_DIR={folder} for the nightly jobs)
"""

from catalyst_lab.strategies import sdk

LOOKBACK_HOURS = 24  # The placeholder rule: a close above the prior 24 hours' high.


def signals(bars, context):
    """Fresh signals of one coin on ascending completed 1-hour ``bars`` whose signal time (the
    bar's end) is in ``(context["since"], context["until"]]``. Pure: no I/O, no clock."""
    proposals = []
    for index, bar, _at in sdk.signal_bars(bars, context):
        prior = bars[max(0, index - LOOKBACK_HOURS):index]
        if len(prior) < LOOKBACK_HOURS:
            continue
        high = max(b.high for b in prior)
        if bar.close > high:
            proposals.append(sdk.marketable_proposal(
                STRATEGY.strategy_id, context["symbol"], bars, bar,
                facts={{"close": str(bar.close), "prior_high": str(high)}}))
    return proposals


STRATEGY = sdk.mechanical_strategy(
    name="{name}", version=1,
    description="{description}",
    signals=signals,
    trigger_rule="MARKETABLE_AT_SIGNAL ({strategy_id} signal on a completed 1-hour bar)",
    parameters={{"lookback_hours": LOOKBACK_HOURS}},
    entry_types=frozenset({{sdk.BREAKOUT, sdk.IMMEDIATE}}),
)
'''


def render(name, folder="PLUGINS_DIR"):
    """The plug-in file's text for ``NAME`` (upper case, ``A-Z0-9_``)."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or re.search(r"_V[0-9]+$", name):
        raise ValueError("STRATEGY_NAME_INVALID")
    return TEMPLATE.format(
        name=name, strategy_id=f"{name}_V1", folder=folder,
        description="Placeholder: an hourly close above the prior 24 hours' high; marketable "
                    "entry; CRYPTO_TRADE_PLAN_V1's exits. Replace with your rule.")


def write(name, directory):
    """Write ``<directory>/<name>_v1.py``; refuses an existing file or a built-in's name."""
    from catalyst_lab.strategies import BUILTINS

    if name in {s.name for s in BUILTINS.values()}:
        raise ValueError("STRATEGY_NAME_IS_BUILTIN")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name.lower()}_v1.py"
    text = render(name, str(directory))
    with open(path, "x") as handle:  # Never overwrites an existing plug-in.
        handle.write(text)
    return path


def main(argv=None):
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(
        prog="catalyst-lab new-strategy",
        description="Write a STRATEGY_SDK_V1 plug-in skeleton NAME_V1 (shadow stage, paper only).")
    parser.add_argument("name", help="the strategy's name, upper case (e.g. MY_BREAKOUT)")
    parser.add_argument("--dir", type=Path,
                        default=Path(os.environ.get("CATALYST_STRATEGY_PLUGINS_DIR")
                                     or "strategy_plugins"),
                        help="the plug-ins folder (default: $CATALYST_STRATEGY_PLUGINS_DIR, "
                             "else ./strategy_plugins)")
    args = parser.parse_args(argv)
    try:
        path = write(args.name, args.dir)
    except FileExistsError:
        raise SystemExit("new-strategy refused: STRATEGY_FILE_EXISTS") from None
    except ValueError as exc:
        raise SystemExit(f"new-strategy refused: {exc}") from None
    print(json.dumps({"written": str(path), "strategy_id": f"{args.name}_V1",
                      "stage": "SHADOW", "sdk": "STRATEGY_SDK_V1",
                      "next": [f"catalyst-lab history-test --plugins-dir {args.dir} "
                               f"--strategy {args.name}_V1",
                               f"CATALYST_STRATEGY_PLUGINS_DIR={args.dir} (shadow)"]},
                     sort_keys=True))
    return path


__all__ = ["NAME", "main", "render", "write"]
