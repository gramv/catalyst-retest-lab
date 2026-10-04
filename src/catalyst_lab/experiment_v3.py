"""EXPERIMENT_DASHBOARD_V3's data (package public-page-v3, 2026-10-03; the owner: "live page
doesn't show charts and current amount … professional designer and domain-knowledge work").

Pure functions over the public views (migrations 024, 028 and 029) and the V2 trades already
built by ``experiment_report``; plus ``enrich``, which adds what only public market data can
give (open positions' mark prices, the BTC buy-and-hold benchmark) through a keyless, cached
``public_market.PublicMarketData``, and ``trade_chart`` for a trade's own page. Every figure is
real; an unknown is null and the page omits it.

Display definitions:

* **Account equity** (the hero): the newer of the latest ``ACCOUNT_EQUITY_SNAPSHOT_V1`` (at most
  five minutes apart while the trader runs) and the last positive equity a risk decision
  recorded. **Today** is that equity minus the day's start (RISK_SESSION_BASELINE_V2's basis,
  else the lab.risk_sessions row); **since start** is it minus the first recorded day start.
* **Equity curve** (``EQUITY_HISTORY_WALK_V1``, fix of 2026-10-03): before the first snapshot,
  a walk from one known-good anchor (the first RISK_SESSION_BASELINE_V2 basis, else the first
  snapshot) over each closed trade's P&L (net when fee-verified, else gross) at its exit,
  backwards and forwards (a step line, marked ``RECONSTRUCTED``); lab.risk_sessions rows never
  set a value; from the first snapshot on, the snapshots. Thinned for the page: every
  point of the last 36 hours, one per 30 minutes to 8 days, one per 2 hours before.
* **Drawdown**: equity below its running peak, in % of the peak; the maximum with its $.
* **BTC buy-and-hold**: the curve's first equity "invested" in BTC at the first point's hourly
  close, valued at each later hourly close (the same $ axis: one axis, indexed to one base).
* **Daily P&L**: each New York day's closed trades (by exit): net after verified fees (gross
  where fees are pending, counted), gross, fees, trades and wins. Days without a closed trade
  between the first and the last are listed with no trades.
* **Performance** (since start, earlier rules, current rules): closed trades with a known R,
  leaving out STATS_EXCLUSION_V1 trades (owner ruling 2026-10-03). Win rate, expectancy (mean
  R), average win and loss (R), profit factor (gross wins over gross losses, $), fees. Under
  ``MIN_SAMPLE`` (30) trades a row's ratios are null (counts only).
* **R histogram**: the same trades' R in 0.25R bins from −2R to +3R (beyond: the end bins).
* **Open position**: stop (current) to target, entry and the mark: the public latest quote's
  bid when it is newer than the ledger's price (cached ≤15 s), else the ledger's; R uses the
  traded risk per unit (max entry − the traded initial stop), as the trade's R does.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MIN_SAMPLE = 30
BIN_WIDTH = D("0.25")
BIN_LOW, BIN_HIGH = D("-2"), D("3")
STALE_EQUITY_SECONDS = 600
BENCHMARK_SYMBOL = "BTC/USD"
BENCHMARK_LABEL = "BTC buy-and-hold"
FULL_WINDOW = timedelta(hours=36)
MID_WINDOW = timedelta(days=8)
MID_STEP, OLD_STEP = 1800, 7200
AFTER_HOURS = (1, 4, 24)
PCT = D("0.01")
FOUR = D("0.0001")

# Thinned equity snapshots (see the display definitions).
EQUITY_SNAPSHOTS = """SELECT DISTINCT ON (bucket) at, equity, cash, long_market_value,
    unrealized_pl FROM (SELECT *, CASE
     WHEN at >= clock_timestamp() - interval '36 hours' THEN extract(epoch FROM at)::bigint
     WHEN at >= clock_timestamp() - interval '8 days'
      THEN floor(extract(epoch FROM at) / 1800)::bigint * 1800
     ELSE floor(extract(epoch FROM at) / 7200)::bigint * 7200 END AS bucket
    FROM lab.public_page_equity) e ORDER BY bucket, at DESC"""
DAY_STARTS = "SELECT * FROM lab.public_page_day_starts ORDER BY day"
ACCOUNT_MARKS = "SELECT * FROM lab.public_page_account_marks ORDER BY at"
EXCLUSIONS = "SELECT * FROM lab.public_page_stats_exclusions ORDER BY trade_no"


def read_v3(conn):
    """The V3 views, in the caller's (read-only, repeatable-read) transaction."""
    snapshots = conn.execute(EQUITY_SNAPSHOTS).fetchall()
    return {
        "equity_snapshots": sorted(snapshots, key=lambda r: r["at"]),
        "day_starts": conn.execute(DAY_STARTS).fetchall(),
        "account_marks": conn.execute(ACCOUNT_MARKS).fetchall(),
        "exclusions": conn.execute(EXCLUSIONS).fetchall(),
    }


def _d(value):
    return None if value is None else D(str(value))


def _aware(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC)


def _ny_day(value):
    return _aware(value).astimezone(NY).date()


def _pct(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator * 100).quantize(PCT)


def _mean(values):
    return sum(values, D(0)) / len(values) if values else None


# --- Exclusions ------------------------------------------------------------------------------


def exclusion_block(rows, closed):
    """``{trade_nos, days: [{day, reason, trades}], trades, text}``; the text is the one factual
    line the page shows under the statistics (``Excludes Oct 2 (unmonitored day)``)."""
    numbers = {r["trade_no"] for r in rows}
    days = defaultdict(lambda: {"trades": 0, "reason": None})
    for r in rows:
        key = r["day"].isoformat() if hasattr(r["day"], "isoformat") else str(r["day"])
        days[key]["trades"] += 1
        days[key]["reason"] = r["reason"]
    listed = [{"day": day, "reason": v["reason"], "trades": v["trades"]}
              for day, v in sorted(days.items())]
    text = None
    if listed:
        names = [datetime.fromisoformat(d["day"]).strftime("%b %-d") for d in listed]
        why = {d["reason"] or "" for d in listed}
        words = " (unmonitored day)" if all(w.startswith("UNMONITORED_OPERATION") for w in why) \
            and len(listed) == 1 else (" (unmonitored days)" if all(
                w.startswith("UNMONITORED_OPERATION") for w in why) else "")
        text = "Excludes " + ", ".join(names) + words
    return {"trade_nos": sorted(numbers), "days": listed,
            "trades": sum(1 for t in closed if t["trade_no"] in numbers), "text": text}


PAGE_EXCLUSION_VERSION = "PAGE_EXCLUSION_V2"
PAGE_EXCLUSION_MODE = "EXCLUDE_EVERYWHERE"


def exclusion_summary(rows):
    """The public ``exclusions`` summary (PAGE_EXCLUSION_V2): counts and days only, never a
    trade number; ``text`` is the one factual line the page shows."""
    block = exclusion_block(rows, [{"trade_no": r["trade_no"]} for r in rows])
    return {"version": PAGE_EXCLUSION_VERSION, "mode": PAGE_EXCLUSION_MODE,
            "rule": "STATS_EXCLUSION_V1", "trades": block["trades"],
            "days": [{"day": d["day"], "trades": d["trades"]} for d in block["days"]],
            "text": block["text"]}


# --- Equity ------------------------------------------------------------------------------------


def thin(points, now):
    """Every point of the last 36 hours, the last per 30 minutes to 8 days, per 2 hours
    before (the newest point of each bucket); reconstructed points are all kept."""
    kept, buckets = [], {}
    for p in points:
        at = _aware(p["at"])
        age = now - at
        if p["source"] == "RECONSTRUCTED" or age <= FULL_WINDOW:
            kept.append(p)
            continue
        step = MID_STEP if age <= MID_WINDOW else OLD_STEP
        buckets[(step, int(at.timestamp()) // step)] = p
    kept += buckets.values()
    return sorted(kept, key=lambda p: _aware(p["at"]))


V2_BASIS = "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2"
AGREE_USD = D("1")  # A lab.risk_sessions row confirms a day start only within $1 of the walk.


def _ny_midnights(t0, t1):
    """Every New York midnight in ``(t0, t1)``, in UTC."""
    out, day = [], _aware(t0).astimezone(NY).date() + timedelta(days=1)
    while True:
        at = datetime(day.year, day.month, day.day, tzinfo=NY).astimezone(UTC)
        if at >= t1:
            return out
        out.append(at)
        day += timedelta(days=1)


def equity_points(day_starts, closed, snapshots, latest=None):
    """The curve (display definitions, ``EQUITY_HISTORY_WALK_V1``).

    Before the first snapshot the curve is a walk over realized P&L from ONE known-good anchor:
    the first RISK_SESSION_BASELINE_V2 basis before the first snapshot, else the first snapshot.
    Each closed trade's P&L (net after verified fees; gross while fees are pending) steps the
    equity at its exit, backwards before the anchor and forwards after it, so every step is a
    real trade; each New York midnight in between is a point on the walk. ``lab.risk_sessions``
    rows (Alpaca's ``last_equity``, which lags a day: 2026-10-03's phantom dip) never set a value;
    a row agreeing with the walk within $1 is marked ``confirmed``. Without an anchor the walk
    is not drawn (no known-good equity). From the first snapshot on: the snapshots; ``latest``
    ``(at, equity)`` (a risk decision's account read) is appended when newer than every point."""
    first_snapshot = _aware(snapshots[0]["at"]) if snapshots else None
    rows = [r for r in day_starts if r.get("day_start_equity") is not None
            and r.get("observed_at") is not None]
    anchors = sorted(((_aware(r["observed_at"]), _d(r["day_start_equity"])) for r in rows
                      if r.get("basis") == V2_BASIS
                      and (first_snapshot is None or _aware(r["observed_at"]) < first_snapshot)),
                     key=lambda a: a[0])
    if anchors:
        anchor_at, anchor_equity, anchor_kind = anchors[0][0], anchors[0][1], "DAY_START"
    elif snapshots:
        anchor_at, anchor_equity, anchor_kind = first_snapshot, _d(snapshots[0]["equity"]), None
    else:
        anchor_at = anchor_equity = anchor_kind = None
    exits = sorted((t for t in closed if t.get("exit_at") and t.get("pnl_usd") is not None),
                   key=lambda t: (_aware(t["exit_at"]), t["trade_no"]))
    points = []
    if anchor_at is not None:
        before = [t for t in exits if _aware(t["exit_at"]) <= anchor_at]
        # The walk's start: the first recorded day start or first entry, whichever is earlier.
        times = [_aware(r["observed_at"]) for r in rows] + [
            _aware(t["entry_at"]) for t in closed if t.get("entry_at")]
        start_at = min([t for t in times if t <= anchor_at], default=anchor_at)
        equity = anchor_equity - sum((_d(t["pnl_usd"]) for t in before), D(0))
        end = first_snapshot if first_snapshot is not None else None
        walk = [t for t in exits if _aware(t["exit_at"]) >= start_at
                and (end is None or _aware(t["exit_at"]) < end)]
        stops = sorted(
            [(start_at, "START", None)]
            + [(_aware(t["exit_at"]), "TRADE_EXIT", t) for t in walk]
            + [(m, "DAY_START", None) for m in _ny_midnights(start_at, end or anchor_at)
               if not (anchor_kind and _ny_day(m) == _ny_day(anchor_at))]
            + ([(anchor_at, anchor_kind, None)] if anchor_kind and anchor_at != start_at else []),
            key=lambda x: (x[0], 0 if x[1] == "TRADE_EXIT" else 1))
        sessions = {r["day"]: _d(r["session_row_equity"]) for r in rows
                    if r.get("session_row_equity") is not None and r.get("day") is not None}
        for at, kind, t in stops:
            if kind == "TRADE_EXIT":
                equity += _d(t["pnl_usd"])
                points.append({"at": at, "equity": equity, "source": "RECONSTRUCTED",
                               "kind": kind, "trade_no": t["trade_no"]})
                continue
            point = {"at": at, "equity": equity, "source": "RECONSTRUCTED", "kind": kind}
            row = sessions.get(_ny_day(at))
            if kind == "DAY_START" and row is not None:
                point["confirmed"] = abs(row - equity) <= AGREE_USD
            points.append(point)
    for s in snapshots:
        points.append({"at": _aware(s["at"]), "equity": _d(s["equity"]), "source": "SNAPSHOT"})
    if latest is not None and latest[1] is not None and latest[1] > 0:
        at = _aware(latest[0])
        if not points or at > points[-1]["at"]:
            points.append({"at": at, "equity": _d(latest[1]), "source": "ACCOUNT_READ"})
    return points


def drawdowns(points):
    """``(series, maximum)``: each point's drawdown from the running peak (a fraction ≤ 0) and
    the deepest one with its $ and the peak and trough times (None without points)."""
    series, peak, peak_at, worst = [], None, None, None
    for p in points:
        if peak is None or p["equity"] > peak:
            peak, peak_at = p["equity"], p["at"]
        fraction = (p["equity"] - peak) / peak if peak > 0 else D(0)
        series.append({"at": p["at"], "pct": (fraction * 100).quantize(PCT),
                       "usd": p["equity"] - peak, "source": p.get("source")})
        if worst is None or fraction < worst["fraction"]:
            worst = {"fraction": fraction, "usd": p["equity"] - peak, "peak_at": peak_at,
                     "trough_at": p["at"], "peak_usd": peak}
    if worst is None:
        return series, None
    return series, {"pct": (worst["fraction"] * 100).quantize(PCT),
                    "drawdown_usd": worst["usd"].quantize(PCT), "peak_at": worst["peak_at"],
                    "trough_at": worst["trough_at"], "peak_usd": worst["peak_usd"]}


MARK_WORDS = {"DAILY_HALT": "Daily loss limit: open trades sold, no entries until 00:00 ET",
              "SOFT_LIMIT": "Soft loss limit: no new entries until 00:00 ET"}


def account_marks(rows):
    return [{"kind": r["kind"], "at": r["at"], "day": r["day"], "pnl_usd": _d(r["pnl"]),
             "text": MARK_WORDS.get(r["kind"], r["kind"])} for r in rows if r["at"] is not None]


def equity_section(snapshot, closed, page_status, now):
    day_starts = snapshot.get("day_starts", [])
    snaps = snapshot.get("equity_snapshots", [])
    latest = None
    page = page_status or {}
    if page.get("equity_usd") is not None and page.get("equity_at") is not None:
        latest = (page["equity_at"], _d(page["equity_usd"]))
    points = thin(equity_points(day_starts, closed, snaps, latest), now)
    series, worst = drawdowns(points)
    start = next((p for p in points if p["source"] != "ACCOUNT_READ"), None) or (
        points[0] if points else None)
    days = sorted({_ny_day(p["at"]).isoformat() for p in points})
    return {
        "points": [{"at": p["at"], "equity_usd": p["equity"], "basis": p["source"],
                    **({"trade_no": p["trade_no"]} if "trade_no" in p else {})} for p in points],
        "first_snapshot_at": snaps[0]["at"] if snaps else None,
        "start_equity_usd": start["equity"] if start else None,
        "start_at": start["at"] if start else None,
        "drawdown": [{"at": d["at"], "pct": d["pct"], "basis": d["source"]} for d in series],
        "max_drawdown": worst, "marks": account_marks(snapshot.get("account_marks", [])),
        "days": days, "benchmark": None,
    }


def pnl_section(snapshot, included, all_closed, live, excluded_days, page_status, now):
    """PAGE_EXCLUSION_V2's chart: trading P&L, cumulative, without the excluded trades.

    ``capital``: the account's starting equity (the first point of EQUITY_HISTORY_WALK_V1,
    over every closed trade, else the first snapshot). Before the first snapshot the curve is
    the included trades' realized P&L stepping at each exit (from 0 at the walk's start); from
    it on, each snapshot (outside an excluded day) is the realized P&L to that time plus the
    snapshot's unrealized P&L; the last point (``NOW``) is the realized total plus the open
    trades' P&L — the figure the page calls trading P&L. Drawdown: below the running peak, in
    % of the capital plus that peak."""
    day_starts = snapshot.get("day_starts", [])
    snaps = [s for s in snapshot.get("equity_snapshots", [])
             if _ny_day(s["at"]).isoformat() not in excluded_days]
    all_snaps = snapshot.get("equity_snapshots", [])
    walk = equity_points(day_starts, all_closed, all_snaps)
    capital = walk[0]["equity"] if walk else (_d(all_snaps[0]["equity"]) if all_snaps else None)
    starts = [walk[0]["at"]] if walk else []
    starts += [_aware(t["entry_at"]) for t in included if t.get("entry_at")]
    starts += [_aware(s["at"]) for s in snaps]
    start_at = min(starts) if starts else None
    exits = sorted((t for t in included if t.get("exit_at") and t.get("pnl_usd") is not None),
                   key=lambda t: (_aware(t["exit_at"]), t["trade_no"]))
    first_snapshot = _aware(snaps[0]["at"]) if snaps else None

    def realized_at(at):
        return sum((_d(t["pnl_usd"]) for t in exits if _aware(t["exit_at"]) <= at), D(0))

    points = []
    if start_at is not None:
        points.append({"at": start_at, "pnl": D(0), "source": "REALIZED"})
    running = D(0)
    for t in exits:
        at = _aware(t["exit_at"])
        running += _d(t["pnl_usd"])
        if first_snapshot is None or at < first_snapshot:
            points.append({"at": at, "pnl": running, "source": "REALIZED",
                           "trade_no": t["trade_no"]})
    for s in snaps:
        unrealized = _d(s.get("unrealized_pl"))
        if unrealized is None or (start_at is not None and _aware(s["at"]) < start_at):
            continue
        points.append({"at": _aware(s["at"]), "pnl": realized_at(_aware(s["at"])) + unrealized,
                       "source": "SNAPSHOT"})
    priced = [_d(t["pnl_usd"]) for t in live if t.get("pnl_usd") is not None]
    open_pnl = sum(priced, D(0))
    realized = sum((_d(t["pnl_usd"]) for t in exits), D(0))
    total = realized + open_pnl
    points.sort(key=lambda p: p["at"])
    if points:
        at = max(_aware(now), points[-1]["at"])
        points.append({"at": at, "pnl": total, "source": "NOW"})
    base = capital if capital is not None else D(0)
    series, worst = drawdowns([{"at": p["at"], "equity": base + p["pnl"],
                                "source": p["source"]} for p in points])
    if capital is None:
        worst = None if worst is None else {**worst, "pct": None}
        series = [{**d, "pct": None} for d in series]
    return {
        "mode": "TRADING_PNL", "capital_usd": capital, "start_at": start_at,
        "realized_usd": realized, "open_usd": open_pnl, "trading_pnl_usd": total,
        "trading_pnl_pct": _pct(total, capital) if capital else None,
        "points": [{"at": p["at"], "pnl_usd": p["pnl"], "basis": p["source"],
                    **({"trade_no": p["trade_no"]} if "trade_no" in p else {})}
                   for p in points],
        "first_snapshot_at": snaps[0]["at"] if snaps else None,
        "drawdown": [{"at": d["at"], "pct": d["pct"], "usd": d["usd"], "basis": d["source"]}
                     for d in series],
        "max_drawdown": worst,
        "marks": [m for m in account_marks(snapshot.get("account_marks", []))
                  if (m["day"].isoformat() if hasattr(m["day"], "isoformat") else str(m["day"]))
                  not in excluded_days],
        "benchmark": None,
    }


# --- The hero --------------------------------------------------------------------------------


def latest_equity(snapshot, page_status):
    """``(equity, at, source)``: the newer of the latest snapshot and the last positive risk
    decision's equity; ``(None, None, None)`` without either."""
    options = []
    snaps = snapshot.get("equity_snapshots", [])
    if snaps:
        options.append((_aware(snaps[-1]["at"]), _d(snaps[-1]["equity"]), "SNAPSHOT"))
    page = page_status or {}
    if page.get("equity_usd") is not None and page.get("equity_at") is not None \
            and _d(page["equity_usd"]) > 0:
        options.append((_aware(page["equity_at"]), _d(page["equity_usd"]), "RISK_DECISION"))
    if not options:
        return None, None, None
    at, equity, source = max(options, key=lambda o: o[0])
    return equity, at, source


def account_section(snapshot, page_status, live, closed, closed_today, open_risk_block,
                    equity, performance_block, as_of):
    value, at, source = latest_equity(snapshot, page_status)
    page = page_status or {}
    day_start = _d(page.get("day_start_equity"))
    start = _d(equity["start_equity_usd"])
    priced = [t for t in live if t.get("pnl_usd") is not None]
    open_pnl = sum((_d(t["pnl_usd"]) for t in priced), D(0)) if priced else None
    rs = [_d(t["pnl_r"]) for t in priced if t.get("pnl_r") is not None]
    fees = [_d(t["fees_usd"]) for t in closed if t.get("fees_usd") is not None]
    realized = [_d(t["pnl_usd"]) for t in closed_today if t.get("pnl_usd") is not None]
    since = performance_block["rows"][0]
    age = int((_aware(as_of) - at).total_seconds()) if at is not None else None
    worst = equity.get("max_drawdown") or {}
    return {
        "equity_usd": value, "equity_at": at, "equity_basis": source,
        "equity_age_seconds": age,
        "equity_stale": age is not None and age > STALE_EQUITY_SECONDS,
        "day_start_usd": day_start,
        "today_usd": value - day_start if value is not None and day_start else None,
        "today_pct": _pct(value - day_start, day_start) if value is not None and day_start
        else None,
        "start_equity_usd": start, "start_at": equity.get("start_at"),
        "since_start_usd": value - start if value is not None and start else None,
        "since_start_pct": _pct(value - start, start) if value is not None and start else None,
        "open_pnl_usd": open_pnl, "open_pnl_r": sum(rs, D(0)) if rs else None,
        "open_unpriced": len(live) - len(priced), "open_trades": len(live),
        "realized_today_usd": sum(realized, D(0)),
        "open_risk_pct": open_risk_block.get("risk_pct"),
        "open_risk_cap_pct": open_risk_block.get("cap_pct"),
        "open_risk_fraction": open_risk_block.get("fraction"),
        "open_risk_usd": open_risk_block.get("risk_usd"),
        "fees_paid_usd": sum(fees, D(0)),
        "fees_pending_trades": sum(1 for t in closed if t.get("fees_pending")),
        "max_drawdown_pct": worst.get("pct"), "max_drawdown_usd": worst.get("drawdown_usd"),
        "win_rate": since["win_rate_all"], "expectancy_r": since["expectancy_all_r"],
        "stats_trades": since["trades"], "stats_enough": since["enough"],
    }


# --- Daily P&L, performance and the R histogram ---------------------------------------------


def daily_section(closed, regimes, excluded_days, today):
    markets = {r["day"]: r.get("short") for r in regimes}
    by_day = defaultdict(list)
    for t in closed:
        if t.get("exit_at") is not None:
            by_day[_ny_day(t["exit_at"])].append(t)
    if not by_day:
        return []
    first, last = min(by_day), max(max(by_day), today) if today else max(by_day)
    out, day = [], first
    while day <= last:
        if day.isoformat() in excluded_days:  # PAGE_EXCLUSION_V2: the day is not listed.
            day += timedelta(days=1)
            continue
        trades = by_day.get(day, [])
        known = [t for t in trades if t.get("pnl_usd") is not None]
        fees = [_d(t["fees_usd"]) for t in trades if t.get("fees_usd") is not None]
        gross = [_d(t["gross_pnl_usd"]) for t in trades if t.get("gross_pnl_usd") is not None]
        out.append({
            "day": day.isoformat(), "net_usd": sum((_d(t["pnl_usd"]) for t in known), D(0)),
            "gross_usd": sum(gross, D(0)) if gross else None,
            "fees_usd": sum(fees, D(0)) if fees else None, "trades": len(trades),
            "won": sum(1 for t in known if _d(t["pnl_usd"]) > 0),
            "fees_pending": sum(1 for t in trades if t.get("fees_pending")),
            "market": markets.get(day.isoformat()),
            "excluded": day.isoformat() in excluded_days,
        })
        day += timedelta(days=1)
    return out


def stats_row(trades, key, label, *, since=None, fees_all=None, drawdown=None):
    """One performance row over closed trades with a known R (display definitions)."""
    rated = [t for t in trades if t.get("pnl_r") is not None]
    rs = [_d(t["pnl_r"]) for t in rated]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    won_usd = sum((_d(t["pnl_usd"]) for t in rated if _d(t["pnl_r"]) > 0), D(0))
    lost_usd = -sum((_d(t["pnl_usd"]) for t in rated if _d(t["pnl_r"]) <= 0), D(0))
    enough = len(rs) >= MIN_SAMPLE
    win_rate = (D(len(wins)) / len(rs)).quantize(FOUR) if rs else None
    expectancy = _mean(rs)
    fees = [_d(t["fees_usd"]) for t in trades if t.get("fees_usd") is not None]
    return {
        "row": key, "label": label, "since": since, "trades": len(rs), "wins": len(wins),
        "losses": len(losses), "enough": enough,
        "win_rate": win_rate if enough else None,
        "expectancy_r": expectancy if enough else None,
        "avg_win_r": _mean(wins) if enough else None,
        "avg_loss_r": _mean(losses) if enough else None,
        "profit_factor": (won_usd / lost_usd).quantize(D("0.01"))
        if enough and lost_usd > 0 else None,
        "fees_usd": sum(fees, D(0)) if fees_all is None else fees_all,
        "pnl_usd": sum((_d(t["pnl_usd"]) for t in rated), D(0)),
        "max_drawdown_pct": drawdown,
        # The hero shows these at any count, with the count beside them.
        "win_rate_all": win_rate, "expectancy_all_r": expectancy,
    }


def r_histogram(trades):
    """0.25R bins from −2R to +3R; a value outside lands in the end bin (``open_low`` /
    ``open_high``). ``expectancy_r`` is the mean R of the same trades."""
    rs = [_d(t["pnl_r"]) for t in trades if t.get("pnl_r") is not None]
    count = int((BIN_HIGH - BIN_LOW) / BIN_WIDTH)
    bins = [{"lo": BIN_LOW + i * BIN_WIDTH, "hi": BIN_LOW + (i + 1) * BIN_WIDTH, "count": 0,
             "open_low": i == 0, "open_high": i == count - 1, "trade_nos": []}
            for i in range(count)]
    for t, r in zip([t for t in trades if t.get("pnl_r") is not None], rs, strict=True):
        index = int(((r - BIN_LOW) / BIN_WIDTH).to_integral_value(rounding="ROUND_FLOOR"))
        index = min(max(index, 0), count - 1)
        bins[index]["count"] += 1
        bins[index]["trade_nos"].append(t["trade_no"])
    return {"bin_width": BIN_WIDTH, "low": BIN_LOW, "high": BIN_HIGH, "bins": bins,
            "trades": len(rs), "expectancy_r": _mean(rs),
            "below": sum(1 for r in rs if r < BIN_LOW),
            "above": sum(1 for r in rs if r >= BIN_HIGH)}


def performance_section(closed, excluded_nos, keys, current_rules, rules_since, exclusions,
                        drawdown_pct, selloff=None):
    """Since start, earlier and current rules, and (``selloff``: ``{day: True/False}`` of the
    recorded MARKET_REGIME_V1 days) sell-off days and other days, by the exit day."""
    included = [t for t in closed if t["trade_no"] not in excluded_nos]
    selloff = selloff or {}

    def market_of(t):
        return selloff.get(_ny_day(t["exit_at"]).isoformat()) if t.get("exit_at") else None

    known = any(current_rules)
    current = [t for t in included if known and keys.get(t["trade_no"]) == current_rules]
    earlier = [t for t in included if not (known and keys.get(t["trade_no"]) == current_rules)]
    first_exit = min((t["exit_at"] for t in included if t.get("exit_at")), default=None,
                     key=_aware)
    return {
        "minimum_sample": MIN_SAMPLE,
        "rows": [
            stats_row(included, "since_start", "Since start", since=first_exit,
                      drawdown=drawdown_pct),
            stats_row(earlier, "earlier", "Earlier rules"),
            stats_row(current, "current", "Current rules", since=rules_since if known else None),
            stats_row([t for t in included if market_of(t) is True], "selloff",
                      "Sell-off days"),
            stats_row([t for t in included if market_of(t) is False], "other", "Other days"),
        ],
        "untagged": sum(1 for t in included if market_of(t) is None),
        "r_histogram": r_histogram(included), "exclusions": exclusions,
    }


# --- Open positions --------------------------------------------------------------------------


def position(t, *, mark=None, mark_at=None, source="LEDGER"):
    """The position bar's numbers for an open trade (display definitions); None without a
    stop, a target and an entry."""
    stop, target = _d(t.get("stop")), _d(t.get("target"))
    entry, limit = _d(t.get("entry")), _d(t.get("limit"))
    initial = _d(t.get("planned_stop"))
    if mark is None:
        mark, mark_at = _d(t.get("price")), t.get("price_at")
    if stop is None or target is None or entry is None or target <= stop:
        return None
    risk_unit = (limit or entry) - initial if initial is not None else None
    if risk_unit is not None and risk_unit <= 0:
        risk_unit = None

    def frac(x):
        return None if x is None else ((x - stop) / (target - stop)).quantize(FOUR)

    return {
        "stop": stop, "target": target, "entry": entry, "mark": mark, "mark_at": mark_at,
        "mark_basis": source if mark is not None else None,
        "entry_at_fraction": frac(entry), "mark_fraction": frac(mark),
        "r_to_stop": ((stop - mark) / risk_unit).quantize(D("0.01"))
        if mark is not None and risk_unit else None,
        "r_to_target": ((target - mark) / risk_unit).quantize(D("0.01"))
        if mark is not None and risk_unit else None,
        "risk_per_unit": risk_unit,
    }


def jev_check(decisions):
    """The newest maintenance review in words (V5 yes/no answers when recorded)."""
    from catalyst_lab.experiment_report import jev_action, v5_words

    reviews = [d for d in decisions if d["kind"] == "MAINTENANCE"]
    if not reviews:
        return None
    d = max(reviews, key=lambda r: _aware(r["at"]))
    if d.get("v5"):
        text = v5_words(d["v5"], d["outcome"])
    else:
        text = jev_action(d["kind"], d["outcome"], d["action"], d["stop"], d["target"],
                          d["basis"])
    return {"text": text, "at": d["at"], "outcome": d["outcome"]}


# --- Public market data (server side, keyless, cached) ---------------------------------------


def _price_from_string(value):
    return None if value in (None, "") else D(str(value))


def apply_quotes(document, quotes):
    """Open positions marked at the public latest quote's bid when it is newer than the
    ledger's price; their P&L and R at that mark; the hero's open P&L again. ``document`` is
    the JSON-safe document (strings); changed in place."""
    changed = False
    for t in document.get("live_trades", []):
        quote = quotes.get(t.get("symbol"))
        if not quote:
            continue
        ledger_at = _aware(t["price_at"]) if t.get("price_at") else None
        if ledger_at is not None and quote["at"] <= ledger_at:
            continue
        bid = quote["bid"]
        entry, qty = _price_from_string(t.get("entry")), _price_from_string(t.get("qty"))
        pos = position(t, mark=bid, mark_at=quote["at"].isoformat(), source="PUBLIC_QUOTE")
        t["position"] = _json(pos)
        if entry is not None and qty is not None:
            pnl = qty * (bid - entry)
            t["pnl_usd"] = str(pnl.quantize(D("0.01")))
            unit = (pos or {}).get("risk_per_unit")
            t["pnl_r"] = str(((bid - entry) / unit).quantize(D("0.001"))) if unit else None
        t["price"], t["price_at"], t["price_live"] = str(bid), quote["at"].isoformat(), True
        changed = True
    if changed:
        account = document.get("account") or {}
        live = document.get("live_trades", [])
        priced = [t for t in live if t.get("pnl_usd") is not None]
        if priced:
            account["open_pnl_usd"] = str(sum((D(t["pnl_usd"]) for t in priced), D(0)))
            rs = [D(t["pnl_r"]) for t in priced if t.get("pnl_r") is not None]
            account["open_pnl_r"] = str(sum(rs, D(0)).quantize(D("0.001"))) if rs else None
            curve = document.get("equity") or {}
            if curve.get("mode") == "TRADING_PNL" and curve.get("realized_usd") is not None:
                total = D(curve["realized_usd"]) + D(account["open_pnl_usd"])
                curve["open_usd"] = account["open_pnl_usd"]
                curve["trading_pnl_usd"] = account["trading_pnl_usd"] = str(total)
                if curve.get("points") and curve["points"][-1].get("basis") == "NOW":
                    curve["points"][-1]["pnl_usd"] = str(total)
                capital = curve.get("capital_usd")
                if capital:
                    pct = str((total / D(capital) * 100).quantize(PCT))
                    curve["trading_pnl_pct"] = account["trading_pnl_pct"] = pct
    return document


def benchmark(points, bars, capital=None):
    """``{"symbol", "label", "base_usd", "base_at", "points"}``: the curve's first equity
    (``capital``, for a P&L curve) invested at the BTC close of the first bar at or after the
    first point, valued at each later hourly close (``pnl_usd``: that value minus the base);
    None when the bars do not reach back to the first point."""
    if not points or not bars:
        return None
    base_at = _aware(points[0]["at"])
    base_usd = _d(capital) if capital is not None else _d(points[0].get("equity_usd"))
    first = next((b for b in bars if b["t"] + timedelta(hours=1) > base_at), None)
    if first is None or base_usd is None or first["t"] - base_at > timedelta(hours=2):
        return None
    units = base_usd / first["c"]
    series = [{"at": max(b["t"] + timedelta(hours=1), base_at),
               "value_usd": (units * b["c"]).quantize(D("0.01")),
               "pnl_usd": (units * b["c"] - base_usd).quantize(D("0.01")), "btc": b["c"]}
              for b in bars if b["t"] >= first["t"]]
    return {"symbol": BENCHMARK_SYMBOL, "label": BENCHMARK_LABEL, "base_usd": base_usd,
            "base_at": base_at, "base_price": first["c"], "points": series}


def enrich(document, market, now=None):
    """Add the public market data to a built document (in place): open positions' marks and
    the BTC benchmark. Any failure omits that element; nothing is estimated."""
    from catalyst_lab.public_market import PublicMarketError

    if market is None:
        return document
    now = now or datetime.now(UTC)
    status = {"quotes": "UNAVAILABLE", "benchmark": "UNAVAILABLE"}
    symbols = [t["symbol"] for t in document.get("live_trades", []) if t.get("symbol")]
    if symbols:
        try:
            apply_quotes(document, market.latest_quotes(symbols))
            status["quotes"] = "OK"
        except (PublicMarketError, ValueError):
            pass
    else:
        status["quotes"] = "NONE_OPEN"
    equity = document.get("equity") or {}
    points = equity.get("points") or []
    if points:
        try:
            start = _aware(points[0]["at"]) - timedelta(hours=1)
            bars = market.bars(BENCHMARK_SYMBOL, start, now, "1Hour")
            equity["benchmark"] = _json(benchmark(points, bars, equity.get("capital_usd")))
            status["benchmark"] = "OK" if equity["benchmark"] else "NO_BARS"
        except (PublicMarketError, ValueError):
            pass
    document["market_data"] = {"source": "ALPACA_PUBLIC_KEYLESS", **status}
    return document


def _json(value):
    from catalyst_lab.repository import json_safe

    return None if value is None else json_safe(value)


def _bar_json(b):
    return {"t": b["t"].isoformat(), "o": str(b["o"]), "h": str(b["h"]), "l": str(b["l"]),
            "c": str(b["c"]), "v": str(b["v"])}


def after_exit(trade, bars, now):
    """+1 h / +4 h / +24 h after the sale: the close of the last bar that started before each
    time, its change from the exit price and that change in R (the trade's risk per unit). A
    time not reached yet is ``pending``; one the bars do not cover is omitted."""
    exit_at, exit_price = _aware(trade.get("exit_at")), _d(trade.get("exit"))
    if exit_at is None or exit_price is None or not bars:
        return None
    initial = _d(trade.get("planned_stop"))
    limit = _d(trade.get("limit")) or _d(trade.get("entry"))
    risk = limit - initial if initial is not None and limit is not None and limit > initial \
        else None
    points = []
    for hours in AFTER_HOURS:
        at = exit_at + timedelta(hours=hours)
        label = f"+{hours} h"
        if at > now:
            points.append({"label": label, "hours": hours, "at": at.isoformat(),
                           "pending": True})
            continue
        before = [b for b in bars if exit_at <= b["t"] < at]
        if not before or at - before[-1]["t"] > timedelta(minutes=30):
            continue
        price = before[-1]["c"]
        points.append({
            "label": label, "hours": hours, "at": at.isoformat(), "pending": False,
            "price": str(price), "change_pct": str(_pct(price - exit_price, exit_price)),
            "r": str(((price - exit_price) / risk).quantize(D("0.01"))) if risk else None,
        })
    return {"exit_price": str(exit_price), "points": points} if points else None


def trade_chart(trade, closed, market, now=None):
    """A trade's chart data: candles (1-minute for a trade shorter than 2 hours, else
    5-minute) from 30 minutes (or a fifth of the holding time) before the entry to the exit
    (an open trade: now); after a closed trade's exit, 24 hours of 5-minute closes; and the
    after-the-sale numbers. Bars that cannot be read leave ``candles`` empty with a reason."""
    from catalyst_lab.public_market import PublicMarketError

    now = now or datetime.now(UTC)
    entry_at = _aware(trade.get("entry_at"))
    if entry_at is None:
        return {"candles": [], "after": [], "after_exit": None, "status": "NO_ENTRY"}
    end = _aware(trade.get("exit_at")) if closed else now
    held = end - entry_at
    pad = max(timedelta(minutes=30), held / 5)
    timeframe = "1Min" if held < timedelta(hours=2) else "5Min"
    if held > timedelta(days=4):
        timeframe = "15Min"
    out = {"timeframe": timeframe, "candles": [], "after": [], "after_exit": None,
           "status": "OK", "window": {"start": (entry_at - pad).isoformat(),
                                      "end": end.isoformat()}}
    try:
        candles = market.bars(trade["symbol"], entry_at - pad, min(end + timedelta(minutes=5),
                                                                    now), timeframe)
        out["candles"] = [_bar_json(b) for b in candles]
        if closed:
            later = market.bars(trade["symbol"], end, min(end + timedelta(hours=24, minutes=5),
                                                          now), "5Min") \
                if now > end + timedelta(minutes=5) else []
            out["after"] = [{"t": b["t"].isoformat(), "c": str(b["c"])} for b in later]
            out["after_exit"] = after_exit(trade, later, now)
    except (PublicMarketError, ValueError):
        out["status"] = "UNAVAILABLE"
    return out


__all__ = ["PAGE_EXCLUSION_VERSION", "exclusion_summary", "pnl_section",
           "BENCHMARK_LABEL", "MIN_SAMPLE", "account_section", "after_exit", "apply_quotes",
           "benchmark", "daily_section", "drawdowns", "enrich", "equity_points",
           "equity_section", "exclusion_block", "jev_check", "latest_equity",
           "performance_section", "position", "r_histogram", "read_v3", "stats_row",
           "thin", "trade_chart"]
