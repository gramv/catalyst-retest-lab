"""A realistic FIXTURE ledger for the public page's screenshots (package public-page-v3).

``build_realistic(ledger, document, market, now)`` rebuilds, in a disposable database only, the
shape of a public page document (``/api/public/experiment`` JSON, read-only, saved to a file
beforehand): the same coins, entry and exit times and prices, stops, targets, quantities and exit
reasons, written through tests/experiment_fixtures.py's engine-shaped writers (every row is
LAB_FIXTURE data; fees are the fixture's 0.25%, so P&L differs slightly from the source). The day
starts chain from the cumulative P&L; today's equity snapshots are the day start plus the open
positions' value at Alpaca's public 5-minute closes (keyless) when ``market`` is given, else a
flat line. The page shows the FIXTURE DATA banner. Never run against an owner ledger.
"""

import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalyst_lab.experiment_report import EXIT_REASONS  # noqa: E402
from catalyst_lab.stats_exclusion import record_exclusion  # noqa: E402
from tests.experiment_fixtures import Pick  # noqa: E402

NY = ZoneInfo("America/New_York")
REASON_CODES = {words: code for code, words in EXIT_REASONS.items()}
TAGS = {"Uptrend, high volatility, sell-off": "UP/HIGH/BROAD/SELLOFF",
        "Uptrend, high volatility": "UP/HIGH/BROAD/NO_SELLOFF",
        "Uptrend, normal volatility": "UP/NORMAL/BROAD/NO_SELLOFF",
        "Downtrend, high volatility, sell-off": "DOWN/HIGH/NARROW/SELLOFF"}


def _t(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def _levels(t):
    entry = D(t["entry"])
    stop = D(t.get("research_stop") or t["planned_stop"])
    target = D(t.get("research_target") or t["planned_target"])
    if stop >= entry:
        stop = entry * D("0.97")
    if target <= entry:
        target = entry * D("1.05")
    return {"entry_trigger": str(entry), "max_entry_price": str(entry), "stop": str(stop),
            "target": str(target)}


def _runs(ledger, trades):
    """One research run per New York entry day (split when a coin repeats), slot an hour
    before its first entry; every traded pick selected."""
    groups = defaultdict(list)
    for t in trades:
        groups[_t(t["entry_at"]).astimezone(NY).date()].append(t)
    picks = {}
    for _day, items in sorted(groups.items()):
        batches = []
        for t in sorted(items, key=lambda t: t["entry_at"]):
            batch = next((b for b in batches if t["symbol"] not in {x["symbol"] for x in b}),
                         None)
            if batch is None:
                batch = []
                batches.append(batch)
            batch.append(t)
        for batch in batches:
            slot = min(_t(t["entry_at"]) for t in batch) - timedelta(hours=1)
            run_picks = [Pick(t["symbol"], kind=("CHART", "NEWS", "BOTH")[i % 3],
                              levels=_levels(t), agent_price=t["entry"], jev_rank=i + 1,
                              selected=True) for i, t in enumerate(batch)]
            run = ledger.run(slot, run_picks)
            for t in batch:
                picks[t["trade_no"]] = run.picks[t["symbol"]]
    return picks


def build_realistic(ledger, document, market=None, now=None):
    now = now or datetime.now(UTC)
    closed = sorted(document["past"]["closed_trades"], key=lambda t: t["trade_no"])
    live = sorted(document["live_trades"], key=lambda t: t["trade_no"])
    picks = _runs(ledger, closed + live)
    days = document["past"]["days"]
    for d in days:
        if d.get("market") in TAGS:
            day = datetime.fromisoformat(d["day"]).date()
            worst = datetime(day.year, day.month, day.day, 14, tzinfo=NY).astimezone(UTC)
            tag = TAGS[d["market"]]
            ledger.market_regime(d["day"], tag, worst_hour_start=worst,
                                 worst_pct="-3.4" if tag.endswith("/SELLOFF") else "-0.9",
                                 at=worst + timedelta(hours=11))
    for t in closed:
        pick = picks[t["trade_no"]]
        qty = D(t["qty"])
        arm = "JEV_MANAGED" if t["tag"] == "Jev-managed" else "FIXED_EXIT"
        ledger.trade(pick, arm=arm, entry_at=_t(t["entry_at"]), exit_at=_t(t["exit_at"]),
                     exit_price=D(t["exit"]), reason=REASON_CODES.get(t["exit_reason"],
                                                                      "BROKER_EXIT"),
                     qty=qty)
    sids = {}
    for t in live:
        pick = picks[t["trade_no"]]
        qty = D(t["qty"])
        entry_at = _t(t["entry_at"])
        sid = ledger.trade(pick, arm="JEV_MANAGED", entry_at=entry_at, qty=qty,
                           rules="PHASE_A")[0]
        sids[t["trade_no"]] = (sid, t)
        levels = (D(t["planned_stop"]), D(t["planned_target"]))
        bid = D(t["price"])
        for minutes, p in ((14, "0.06"), (13, "0.09"), (12, "0.05"), (11, "0.07")):
            ledger.maintenance_v5(sid, at=now - timedelta(minutes=minutes), levels=levels,
                                  bid=bid, invalidation=(p, "NO", "RESET", 0),
                                  news=("0.10", "NO", "RESET", 0))
        ledger.position_sample(sid, at=_t(t["price_at"]), bid=bid, qty=qty)
    # Day starts: chained from the first, so each day's start is the previous one plus its
    # closed trades' P&L; today's is the V2 basis the document shows (corrected once).
    limits = document["limits"]
    today = now.astimezone(NY).date()
    total = sum(D(d["pnl_usd"]) for d in days)
    start = D(limits["day_start_equity_usd"]) - total
    running = start
    first = datetime.fromisoformat(days[0]["day"]).date()
    by_day = {datetime.fromisoformat(d["day"]).date(): D(d["pnl_usd"]) for d in days}
    day = first
    while day < today:
        midnight = datetime(day.year, day.month, day.day, tzinfo=NY).astimezone(UTC)
        ledger.day_start(day, running.quantize(D("0.01")), at=midnight + timedelta(minutes=4))
        running += by_day.get(day, D(0))
        day += timedelta(days=1)
    midnight = datetime(today.year, today.month, today.day, tzinfo=NY).astimezone(UTC)
    base = D(limits["day_start_equity_usd"])
    ledger.day_start(today, D(limits.get("baseline_corrected_from_usd") or base),
                     at=midnight + timedelta(minutes=3))
    ledger.baseline_v2(today, base, at=midnight + timedelta(minutes=5),
                       row_equity=D(limits.get("baseline_corrected_from_usd") or base))
    # Oct 2's soft limit (−2%) before the halt, as a marker on the curve.
    for d in days:
        if D(d["pnl_usd"]) < -D("0.02") * start:
            day = datetime.fromisoformat(d["day"]).date()
            at = datetime(day.year, day.month, day.day, 14, 40, tzinfo=NY).astimezone(UTC)
            ledger.soft_limit(d["day"], at=at, total=-D("0.02") * start, day_start=start)
    # Today's snapshots: the day start plus the open positions at public 5-minute closes.
    closes = {}
    if market is not None:
        for _, t in sids.values():
            try:
                bars = market.bars(t["symbol"], midnight, now, "5Min")
            except Exception:  # noqa: BLE001 -- a fixture without bars draws a flat line.
                bars = []
            closes[t["symbol"]] = [(b["t"] + timedelta(minutes=5), b["c"]) for b in bars]
    at = midnight + timedelta(minutes=10)
    while at <= now - timedelta(minutes=1):
        equity, unrealized, value = base, D(0), D(0)
        for _, t in sids.values():
            if _t(t["entry_at"]) > at:
                continue
            series = [c for when, c in closes.get(t["symbol"], []) if when <= at]
            mark = series[-1] if series else D(t["entry"])
            qty = D(t["qty"])
            unrealized += qty * (mark - D(t["entry"]))
            value += qty * mark
        equity += unrealized
        ledger.equity_snapshot(at, equity.quantize(D("0.01")), cash=(equity - value).quantize(
            D("0.01")), long_market_value=value.quantize(D("0.01")),
            unrealized=unrealized.quantize(D("0.01")), positions=1)
        at += timedelta(minutes=5)
    # STATS_EXCLUSION_V1 for the outlier day (the owner's 2026-10-03 ruling, in this fixture).
    worst_day = min(days, key=lambda d: D(d["pnl_usd"]))
    record_exclusion(ledger.store, datetime.fromisoformat(worst_day["day"]).date(),
                     "UNMONITORED_OPERATION_" + worst_day["day"], now=now,
                     ruling="owner 2026-10-03 (fixture)")
    ledger.heartbeat()
    return {"excluded_day": worst_day["day"], "open": [t["trade_no"] for t in live]}
