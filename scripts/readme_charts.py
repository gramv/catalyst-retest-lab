#!/usr/bin/env python3
"""Draw the README's research charts (docs/images/research-*.svg).

Every number comes from a study's own output files; nothing is typed in by hand.
The inputs live outside the repository, so each is a command-line argument:

  python scripts/readme_charts.py \
      --history-dir <history test run dir: results.json, cost-floor-maker-no-slippage/> \
      --mae-dir     <trade-management study dir: results.md> \
      --b2-dir      <AI-judge replay dir: rows_v31.json> \
      --out docs/images

Needs matplotlib, plus fontTools and brotli to use the bundled IBM Plex Sans
(src/catalyst_lab/static/fonts); without them it falls back to a system sans.
The charts are research only: simulated or replayed outcomes, not a forecast.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

# Colour tokens (validated set; text never wears a data colour).
STRATEGY = "#1D4ED8"
BENCH = "#C2410C"
GAIN = "#0B7A47"
LOSS = "#B42318"
SEQ = ("#86B6EF", "#2A78D6", "#184F95")  # sequential blue, light -> dark
MUTED_MARK = "#A3A3A0"
TEXT = "#0B0B0B"
TEXT_2 = "#52514E"
TEXT_3 = "#8A8984"
GRID = "#E7E6E2"
AXIS = "#C9C8C2"
SURFACE = "#FFFFFF"

REPO = Path(__file__).resolve().parent.parent


def setup_fonts(fonts_dir: Path) -> str:
    """Register IBM Plex Sans from the bundled woff2 files when possible."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return "sans-serif"
    tmp = Path(tempfile.mkdtemp(prefix="plex-"))
    found = False
    for woff in sorted(fonts_dir.glob("IBMPlexSans-*.woff2")):
        try:
            font = TTFont(str(woff))
            font.flavor = None
            ttf = tmp / (woff.stem + ".ttf")
            font.save(str(ttf))
            font_manager.fontManager.addfont(str(ttf))
            found = True
        except Exception:  # brotli missing or a bad file: fall back
            continue
    return "IBM Plex Sans" if found else "sans-serif"


def style(family: str) -> None:
    plt.rcParams.update({
        "font.family": [family, "Helvetica Neue", "Arial", "sans-serif"],
        "font.size": 10.5,
        "text.color": TEXT,
        "axes.edgecolor": AXIS,
        "axes.labelcolor": TEXT_2,
        "axes.linewidth": 0.8,
        "xtick.color": TEXT_2,
        "ytick.color": TEXT_2,
        "xtick.labelsize": 9.5,
        "ytick.labelsize": 9.5,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "axes.grid": False,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "svg.fonttype": "path",
        "svg.hashsalt": "catalyst-readme",
        "legend.frameon": False,
        "legend.fontsize": 9.5,
    })


def frame(ax, *, grid_axis: str = "y") -> None:
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)


def titles(fig, title: str, caption: str) -> None:
    fig.text(0.02, 0.965, title, fontsize=13, fontweight="semibold", color=TEXT,
             va="top", ha="left")
    fig.text(0.02, 0.03, caption, fontsize=9, color=TEXT_3, va="bottom", ha="left")


def signed(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}".replace("-", "−")


def save(fig, out: Path, name: str) -> Path:
    path = out / name
    fig.savefig(path, format="svg", metadata={"Date": None, "Creator": None})
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- history test

VARIANT_ORDER = [
    "LOOKBACK_5D_VOL_1.5X", "LOOKBACK_5D_VOL_2X", "LOOKBACK_5D_VOL_3X",
    "LOOKBACK_7D_VOL_1.5X", "LOOKBACK_7D_VOL_2X", "LOOKBACK_7D_VOL_3X",
    "LOOKBACK_10D_VOL_1.5X", "LOOKBACK_10D_VOL_2X", "LOOKBACK_10D_VOL_3X",
]


def variant_label(vid: str) -> str:
    m = re.match(r"LOOKBACK_(\d+)D_VOL_([\d.]+)X", vid)
    return f"{m.group(1)}-day high, volume {m.group(2)}×" if m else vid


def load_history(history_dir: Path):
    main = json.loads((history_dir / "results.json").read_text())
    floor = json.loads(
        (history_dir / "cost-floor-maker-no-slippage" / "results.json").read_text())
    sid = "BREAKOUT_7D_VOL2X_V1"
    return main, main["strategies"][sid], floor["strategies"][sid]


def chart_variants(history_dir: Path, out: Path) -> Path:
    main, strat, floor = load_history(history_dir)
    variants = strat["variants"]
    order = [v for v in VARIANT_ORDER if v in variants][::-1]
    fig, ax = plt.subplots(figsize=(8.4, 5.0))
    fig.subplots_adjust(left=0.27, right=0.96, top=0.80, bottom=0.17)
    frame(ax, grid_axis="x")
    ax.spines["bottom"].set_visible(False)
    for i, vid in enumerate(order):
        v = variants[vid]
        lo, hi = v["ci90_mean_net_r"]
        mean = float(v["mean_net_r"])
        ax.plot([lo, hi], [i, i], color=STRATEGY, linewidth=2, solid_capstyle="round",
                zorder=2)
        ax.scatter([mean], [i], s=64, color=STRATEGY, edgecolor=SURFACE, linewidth=2,
                   zorder=3)
        fmean = float(floor["variants"][vid]["mean_net_r"])
        ax.scatter([fmean], [i], s=56, facecolor=SURFACE, edgecolor=TEXT_2, linewidth=1.4,
                   zorder=3)
        if v.get("registered_rule"):
            ax.text(lo - 0.012, i, f"{signed(mean)}R", va="center", ha="right", fontsize=9,
                    color=TEXT)
    ax.axvline(0, color=TEXT_2, linewidth=1)
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels([variant_label(v) + (" (registered)" if variants[v].get("registered_rule")
                                            else "") for v in order])
    for tick, vid in zip(ax.get_yticklabels(), order, strict=True):
        if variants[vid].get("registered_rule"):
            tick.set_fontweight("semibold")
            tick.set_color(TEXT)
    ax.set_xlim(-0.6, 0.2)
    ax.xaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda x, _: "0" if x == 0 else signed(x, 1) + "R"))
    ax.scatter([], [], s=64, color=STRATEGY, label="Taker fee + slippage: mean, 90% interval")
    ax.scatter([], [], s=56, facecolor=SURFACE, edgecolor=TEXT_2, linewidth=1.4,
               label="Cost floor (maker fee, no slippage): mean")
    ax.legend(loc="lower left", bbox_to_anchor=(-0.36, 1.0), ncol=2, handletextpad=0.3,
              columnspacing=1.4)
    start, end = main["inputs"]["start"][:10], main["inputs"]["end"][:10]
    coins = len(main["inputs"]["universe"])
    titles(fig, "Net R per trade, breakout rule and its 8 declared variants",
           f"History test, {coins} coins, hourly bars, {start} to {end}; "
           f"n = {min(variants[v]['trades'] for v in order)}–"
           f"{max(variants[v]['trades'] for v in order)} trades per variant. "
           "Simulated, not fills.")
    return save(fig, out, "research-breakout-variants.svg")


def chart_waterfall(history_dir: Path, out: Path) -> Path:
    main, strat, _ = load_history(history_dir)
    v = strat["variants"][strat["registered_variant"]]
    gross = float(v["mean_gross_r"])
    fee = float(v["mean_fee_r"])
    slip = float(v["mean_slippage_r"])
    net = float(v["mean_net_r"])
    steps = [("Gross move", 0.0, gross), ("Fees\n0.25% per leg", gross, gross - fee),
             ("Slippage\nestimate", gross - fee, gross - fee - slip), ("Net", 0.0, net)]
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    fig.subplots_adjust(left=0.11, right=0.97, top=0.84, bottom=0.2)
    frame(ax)
    width = 0.42
    for i, (label, a, b) in enumerate(steps):
        lo, hi = min(a, b), max(a, b)
        colour = GAIN if b > a else LOSS
        ax.add_patch(FancyBboxPatch((i - width / 2, lo), width, hi - lo,
                                    boxstyle="square,pad=0", facecolor=colour,
                                    edgecolor="none"))
        delta = b - a
        y = hi + 0.012 if delta > 0 else lo - 0.012
        ax.text(i, y, signed(delta, 3) + "R", ha="center",
                va="bottom" if delta > 0 else "top", fontsize=10, color=TEXT,
                fontweight="semibold" if label == "Net" else "normal")
        if i < len(steps) - 1:
            nxt = steps[i + 1][1] if label != "Slippage\nestimate" else b
            ax.plot([i + width / 2, i + 1 - width / 2], [b, nxt], color=AXIS, linewidth=1)
    ax.axhline(0, color=TEXT_2, linewidth=1)
    ax.set_xticks(range(len(steps)))
    ax.set_xticklabels([s[0] for s in steps])
    ax.set_xlim(-0.6, len(steps) - 0.4)
    ax.set_ylim(-0.32, 0.14)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda x, _: "0" if abs(x) < 1e-9 else signed(x, 1) + "R"))
    ax.spines["bottom"].set_visible(False)
    start, end = main["inputs"]["start"][:10], main["inputs"]["end"][:10]
    titles(fig, "Where the registered breakout's edge goes (mean R per trade)",
           f"BREAKOUT_7D_VOL2X_V1, n = {v['trades']} simulated trades, {start} to {end}. "
           "Fee and slippage charged on both legs.")
    return save(fig, out, "research-breakout-costs.svg")


# --------------------------------------------------------------------------- MAE / MFE study

def chart_stop_width(mae_dir: Path, out: Path) -> Path:
    text = (mae_dir / "results.md").read_text()
    section = text.split("## Q2a.")[1].split("## Q2b.")[0]
    series = []
    for line in section.splitlines():
        m = re.match(r"- (real\+shadow|real): (\d+) of (\d+) reached \+1R", line.strip())
        if m:
            series.append({"name": m.group(1), "winners": int(m.group(2)),
                           "n": int(m.group(3)), "pts": []})
        for mult, pct in re.findall(r"stop at ([\d.]+)x hourly range keeps (\d+)% alive", line):
            series[-1]["pts"].append((float(mult), int(pct)))
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    fig.subplots_adjust(left=0.1, right=0.96, top=0.8, bottom=0.2)
    frame(ax)
    colours = {"real+shadow": STRATEGY, "real": BENCH}
    names = {"real+shadow": "Paper trades + untaken setups",
             "real": "Paper trades only"}
    series.sort(key=lambda s: s["name"] != "real+shadow")
    for s in series:
        xs = [p[0] for p in s["pts"]]
        ys = [p[1] for p in s["pts"]]
        c = colours[s["name"]]
        ax.plot(xs, ys, color=c, linewidth=2, solid_capstyle="round",
                label=f"{names[s['name']]} ({s['winners']} winners)")
        ax.scatter(xs, ys, s=40, color=c, edgecolor=SURFACE, linewidth=1.5, zorder=3)
        at2 = dict(s["pts"]).get(2.0)
        if at2 is not None:
            above = s["name"] == "real+shadow"
            ax.text(1.94 if above else 2.06, at2 + (0.8 if above else -0.8), f"{at2}%",
                    ha="right" if above else "left", va="bottom" if above else "top",
                    fontsize=9, color=TEXT)
    ref = dict(series[0]["pts"])
    if 2.0 in ref:
        ax.axvline(2.0, color=AXIS, linewidth=1)
        ax.text(2.03, 63, "live rule:\nstop ≥ 2× hourly range", fontsize=9,
                color=TEXT_2, va="bottom")
    ax.set_xticks([p[0] for p in series[0]["pts"]])
    ax.set_xticklabels([f"{p[0]:g}×" for p in series[0]["pts"]])
    ax.set_xlabel("Stop distance in average hourly ranges")
    ax.set_ylim(60, 102)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2)
    titles(fig, "Share of eventual +1R winners a stop would have kept alive",
           f"Entries that reached +1R within 24 h, stop ignored: {series[1]['winners']} of "
           f"{series[1]['n']} paper trades; {series[0]['winners']} of {series[0]['n']} with "
           "untaken setups. Sep 28–Oct 2 2026.")
    return save(fig, out, "research-stop-width.svg")


def chart_reach(mae_dir: Path, out: Path) -> Path:
    text = (mae_dir / "results.md").read_text()
    section = text.split("## Q2b.")[1].split("## Q3.")[0]
    rows = []
    header = None
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0] == "population":
            header = cells
            continue
        if cells[0] == "real+shadow" and cells[2] == "orig stop":
            rows.append(cells)
    levels = [h for h in header if h.startswith("+")]
    idx = [header.index(h) for h in levels]
    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    fig.subplots_adjust(left=0.09, right=0.97, top=0.8, bottom=0.2)
    frame(ax)
    width = 0.24
    for k, row in enumerate(rows):
        vals = [int(row[i].rstrip("%")) for i in idx]
        xs = [j + (k - 1) * (width + 0.02) for j in range(len(levels))]
        ax.bar(xs, vals, width=width, color=SEQ[k], edgecolor=SURFACE, linewidth=0,
               label=f"within {row[1]} (n = {row[3]})")
        if levels.index("+1R") is not None:
            j = levels.index("+1R")
            ax.text(xs[j], vals[j] + 1.5, f"{vals[j]}%", ha="center", va="bottom",
                    fontsize=9, color=TEXT)
    ax.set_xticks(range(len(levels)))
    ax.set_xticklabels([lv + " reached" for lv in levels])
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3)
    titles(fig, "How far entries ran in their favour, by time since entry",
           "Paper trades + untaken setups, original stop in place, 1-minute bars; "
           "n shrinks with the window. Sep 28–Oct 2 2026.")
    return save(fig, out, "research-reach.svg")


RULES = [
    ("actual (what happened)", "Actual management"),
    ("a. plan, 4 h", "Plan unchanged, 4 h"),
    ("a. plan, 8 h", "Plan unchanged, 8 h"),
    ("a. plan, 24 h", "Plan unchanged, 24 h"),
    ("b. stop max(orig,2.0xHR), tgt 1.5R, 24 h", "Stop ≥ 2× range, 1.5R target, 24 h"),
    ("c. orig stop; half @1R, BE+fees, runner trail 2xHR, 24 h",
     "Half off at 1R, trail rest, 24 h"),
]


def chart_rules(mae_dir: Path, out: Path) -> Path:
    text = (mae_dir / "results.md").read_text()
    section = text.split("#### Real trades, 24 h-complete")[1].split("####")[0]
    table = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 8 and cells[0] not in ("rule",) and not cells[0].startswith("---"):
            table[cells[0]] = cells
    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    fig.subplots_adjust(left=0.33, right=0.95, top=0.84, bottom=0.17)
    frame(ax, grid_axis="x")
    ax.spines["bottom"].set_visible(False)
    labels = []
    n = None
    for i, (key, label) in enumerate(reversed(RULES)):
        cells = table[key]
        n = cells[1]
        mean = float(cells[4])
        lo, hi = (float(x) for x in re.findall(r"[+-]?\d+\.\d+", cells[7])[:2])
        c = GAIN if mean > 0 else LOSS
        ax.plot([lo, hi], [i, i], color=c, linewidth=2, solid_capstyle="round", zorder=2)
        ax.scatter([mean], [i], s=64, color=c, edgecolor=SURFACE, linewidth=2, zorder=3)
        ax.text(max(hi, 0) + 0.03, i, signed(mean) + "R", va="center", fontsize=9.5,
                color=TEXT)
        labels.append(label)
    ax.axvline(0, color=TEXT_2, linewidth=1)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.set_xlim(-0.35, 0.85)
    ax.xaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda x, _: "0" if abs(x) < 1e-9 else signed(x, 1) + "R"))
    titles(fig, "Average net R on the same entries under different exit rules",
           f"n = {n} paper trades with a full 24 h of prices, Sep 28–Oct 1 2026; "
           "90% interval from a 4-day bootstrap (rough). Hindsight simulation.")
    return save(fig, out, "research-exit-rules.svg")


# --------------------------------------------------------------------------- AI-judge replay

def chart_judge(b2_dir: Path, out: Path) -> Path:
    rows = json.loads((b2_dir / "rows_v31.json").read_text())
    runs = len({r["cyc"] for r in rows})
    bars = [("Picks sent by the research agent", len(rows), MUTED_MARK),
            ("Published, rule V2 (top-K)", sum(bool(r["v2_selected"]) for r in rows), STRATEGY),
            ("Published, rule V3 (judge threshold)", sum(bool(r["v3_published"]) for r in rows),
             STRATEGY),
            ("Published, rule V3.1 (coin record needs 5+ trades)",
             sum(bool(r["v31_published"]) for r in rows), STRATEGY)]
    fig, ax = plt.subplots(figsize=(7.6, 3.6))
    fig.subplots_adjust(left=0.42, right=0.95, top=0.82, bottom=0.2)
    frame(ax, grid_axis="x")
    ax.spines["bottom"].set_visible(False)
    ys = list(range(len(bars)))[::-1]
    for y, (_label, value, colour) in zip(ys, bars, strict=True):
        ax.barh(y, value, height=0.5, color=colour)
        ax.text(value + 0.8, y, str(value), va="center", fontsize=10, color=TEXT)
    ax.set_yticks(ys)
    ax.set_yticklabels([b[0] for b in bars])
    ax.set_xlim(0, 75)
    titles(fig, "Same picks, three selection rules",
           f"Offline replay with the real judge model: {len(rows)} picks over {runs} research "
           "runs, Oct 2–3 2026. No orders.")
    return save(fig, out, "research-judge-selection.svg")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history-dir", type=Path, required=True)
    ap.add_argument("--mae-dir", type=Path, required=True)
    ap.add_argument("--b2-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "images")
    ap.add_argument("--fonts-dir", type=Path,
                    default=REPO / "src" / "catalyst_lab" / "static" / "fonts")
    args = ap.parse_args()
    style(setup_fonts(args.fonts_dir))
    args.out.mkdir(parents=True, exist_ok=True)
    made = [
        chart_variants(args.history_dir, args.out),
        chart_waterfall(args.history_dir, args.out),
        chart_stop_width(args.mae_dir, args.out),
        chart_reach(args.mae_dir, args.out),
        chart_rules(args.mae_dir, args.out),
        chart_judge(args.b2_dir, args.out),
    ]
    for path in made:
        print(f"wrote {path.name} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
