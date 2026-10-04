"""The public live dashboard's data: one JSON-safe document built from the four
``lab.public_dashboard_*`` views of migration 024 and the four ``lab.public_page_*`` views of
migration 028 (role ``catalyst_public``). EXPERIMENT_DASHBOARD_V2 (package public-page) adds the
status line, today's P&L against the daily limits, open risk against the cap, the market's
regime, results by market and by rules, and each trade's traded levels, plan and entry waits
(their definitions are with the page V2 functions below).

Paper only: nothing here can place, change or cancel an order, and nothing reads a broker or
Jev/TypeSafe credential or calls an exchange. Display definitions (docs/packages/
experiment-page.md):

* **P&L of a closed trade** is net of fees when every fee is known (verified, reconciled, never
  ``LAB_FIXTURE`` evidence); otherwise it is the gross fill P&L, flagged ``fees_pending``.
  Nothing is estimated.
* **P&L of an open trade** is the held quantity times (the freshest price the ledger holds, the
  bid, minus the average entry). No price in the ledger: unknown.
* **R** divides P&L by the planned risk, filled quantity x (max entry - initial stop). V2: the
  initial stop is the stop actually traded (CRYPTO_TRADE_PLAN_V1's plan stop when the trade
  recorded one, as lab.managed_planned_stop plans the reservation; else the research stop).
* **Size**: an open trade's quantity is the quantity it holds, a closed trade's the quantity it
  bought; its value at entry is that quantity x the average entry, and an open trade's value
  now is its quantity x the freshest price the ledger holds (none without a price).
* **A win** is a closed trade whose P&L is above zero. **Today** is the New York day.
* **Status**: Stopped without a runtime heartbeat in the last 180 s; else Halted with an
  unreleased execution halt or today's daily loss halt; else Running.
* **Paper account**: the equity the latest risk decision recorded. Protective decisions (stop
  changes, cancels, exits) record 0, so zero or missing equity is unknown.
* **Repeated reviews**: a trade's consecutive reviews with the same unchanged outcome (held,
  failed, refused, discarded) are one line with their count.
* **A trade's events** (``events``, oldest first): the agent's pick and Jev's selection, the
  buy (quantity, average price, limit = the admitted max entry), the stop and target set at
  entry, Jev's reviews (repeats folded; a level change as old -> new with its basis and
  confidence), 24-hour reviews, the agents' answers and exit flags, the exit with its reason,
  and the fees and P&L. The view keeps a trade's latest 60 reviews: with 60, earlier reviews
  are not listed (a ``GAP`` line) and the oldest listed streak may continue earlier ("+").
* **A trade's levels** (``levels``): the stop and target from the entry, a step at each
  recorded level (a review's levels after, and an open trade's last change). ``known`` is false
  on a step whose span may hide an unlisted change (reviews cut at 60). Levels only rise.
* **Failed reviews today**: reviews Jev did not answer (outcome FAILED, an error or a timeout)
  on the New York day; "+" when a trade's 60 listed reviews all fall today.
* **The current cycle** is the newest run slot Jev has ranked (a newer run's published selection
  supersedes an older run's picks, RESEARCH_RUN_SUPERSESSION_V1). It is valid until the
  schedule's limit for its slot (the next run plus the grace; the research kit sets reports to
  that limit). A pick's status: in trade or closed (with the trade number), no entry yet
  (selected, before the limit), expired (selected, after it) or not selected. A newer run that
  Jev has not ranked yet is shown as awaiting Jev. Under RESEARCH_SCHEDULE_V2 (a daily full run
  plus update runs, RESEARCH_RUN_SUPERSESSION_V2) the current cycle is the newest ranked full
  run and every ranked update run after it, valid until the next full run plus the grace; a
  selected pick without a trade whose coin a later run of the cycle selected again is
  replaced. The schedule in words then names the daily run and the updates.
* **Jev's scoped logs**: "This cycle" is Jev's decisions since the current cycle's selection
  (its per-pick verdicts, then reviews, level changes, 24-hour reviews, and the trades' entries
  and exits); "Today" is the New York day's (each run's selection as one line). Newest first,
  a trade's repeated reviews folded; at most ``SCOPE_LENGTH`` lines, from at most ``DAY_LIMIT``
  decisions read.
"""

from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from catalyst_lab import experiment_v3 as v3
from catalyst_lab.repository import json_safe

DASHBOARD_VERSION = "EXPERIMENT_DASHBOARD_V3"
DEFAULT_TITLE = "AI crypto trading — live"
FIXTURE_BANNER = "FIXTURE DATA — not real results"
ACCOUNT_LABEL = "Paper account"
RUNNING_HEARTBEAT_SECONDS = 180
NY = ZoneInfo("America/New_York")
MANAGED, CONTROL = "JEV_MANAGED", "FIXED_EXIT"
ARM_TAGS = {MANAGED: "Jev-managed", CONTROL: "Fixed exit"}
FEED_LENGTH = 20
JEV_LOG_LENGTH = 12  # Jev's latest decisions (the JSON's jev.decisions).
SCOPE_LENGTH = 150  # Lines in each of Jev's scoped logs (this cycle, today).
CLOSED_LIMIT = 50  # Closed trades listed, latest exit first.
RECENT_LIMIT = 300  # Decisions read per build (the view keeps 60 maintenance rows per trade).
DAY_LIMIT = 2000  # Decisions read for the scoped logs, since the earlier of the two starts.
PER_TRADE_LIMIT = 60
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
UNNAMED_AGENT = "unnamed agent"
DEFAULT_SCHEDULE = {"timezone": "America/New_York", "runs": ["08:00"]}  # Owner, 2026-09-26.
USD_PLACES = D("0.01")
R_PLACES = D("0.001")

EXIT_REASONS = {
    "TARGET_EXIT": "Target reached",
    "BROKER_EXIT": "Stop hit",
    "STOP_LIMIT_NOT_FILLED": "Stop hit, sold at market",
    "STOP_EMULATED_EXIT": "Stop hit on Coinbase, sold",
    "STOP_CROSSED_DURING_REPLACE": "Raised stop hit",
    # Window-neutral (package review-window): the 24-hour and the window versions share these
    # exit codes, and the public views carry no trade's window.
    "HOLD_24H_EXIT": "Hold time reached",
    "DAY_REVIEW_EXIT": "Review: exit",
    "DAY_REVIEW_DEADLINE_EXIT": "Review: no decision in time",
    "EARLY_EXIT_AGREED": "Early exit",
    "DAILY_RISK_HALT": "Daily loss halt",
    "OPERATOR_FLATTEN": "Closed by the operator",
    "PROTECTION_REJECTED": "Stop order refused, closed",
    "TIME_EXIT": "Time limit",
}
BASES = {
    "BREAKEVEN": "breakeven", "BREAKEVEN_AFTER_FEES": "breakeven after fees",
    "SWING_LOW_15M": "15-minute swing low",
    "SWING_LOW_1H": "1-hour swing low", "HIGH_24H": "24-hour high", "HIGH_7D": "7-day high",
    "SWING_HIGH_1H": "1-hour swing high", "SWING_HIGH_4H": "4-hour swing high",
}
MAINTENANCE_WORDS = {
    "HELD": "Held", "FLAGGED": "Flagged for an early exit",
    "REFUSED": "Change refused by the safety checks", "FAILED": "Review failed",
    "DISCARDED": "Review discarded",
}
# Review outcomes that leave a trade unchanged (Jev reviews every minute): a trade's consecutive
# repeats of one are folded into one line with their count.
REPEATING = ("HELD", "FAILED", "REFUSED", "DISCARDED")
DAY_REVIEW_WORDS = {  # Window-neutral: a review comes every 24 hours or every window.
    "CONTINUE": "Continued", "EXIT": "Exit at the review",
    "DISCARDED": "Review discarded",
}
DAY_REVIEW_CODES = {
    "AGREED": "the agent agreed", "AGENT_SILENT_JEV_ALONE": "no agent answer; Jev decided alone",
}
SNAPSHOT_QUERIES = {
    "trades": "SELECT * FROM lab.public_dashboard_trades ORDER BY trade_no",
    "runs": "SELECT * FROM lab.public_dashboard_runs ORDER BY run_no",
    # Picks and selections are summarized per run (from the runs view), not listed one by one.
    "recent": f"""SELECT * FROM lab.public_dashboard_decisions
        WHERE at IS NOT NULL AND kind NOT IN ('PICK','SELECTION')
        ORDER BY at DESC, trade_no, kind LIMIT {RECENT_LIMIT}""",
}


# Every listed decision of the trades the page shows (open, and the latest closed): their
# events and levels. At most 60 reviews per trade (the view), plus picks, answers and flags.
TRADE_DECISIONS = """SELECT * FROM lab.public_dashboard_decisions
    WHERE trade_no = ANY(%s) AND at IS NOT NULL ORDER BY trade_no, at, kind"""
# The picks and Jev's verdicts of the given runs: each agent's latest run (``latest_runs``, read
# from the runs already fetched) and the current cycle's runs (they may not be each agent's
# latest). Not a join with the runs view: that plan took 5.8 s on the live ledger (2026-09-29,
# 12 runs) against 0.15 s for these run numbers, and the page stopped answering.
CYCLE_PICKS = """SELECT * FROM lab.public_dashboard_decisions
    WHERE kind IN ('PICK','SELECTION') AND run_no = ANY(%s)
    ORDER BY run_no, kind, jev_rank NULLS LAST, symbol"""
# Jev's scoped logs: every decision since the earlier of the New York day's start and the
# current cycle's selection, newest first and bounded.
DAY_DECISIONS = f"""SELECT * FROM lab.public_dashboard_decisions
    WHERE at >= %s AND kind NOT IN ('PICK','SELECTION')
    ORDER BY at DESC, trade_no, kind LIMIT {DAY_LIMIT}"""


PAGE_STATUS = "SELECT * FROM lab.public_page_status"
PAGE_TRADES = "SELECT * FROM lab.public_page_trades ORDER BY trade_no"
PAGE_WAITS = """SELECT * FROM lab.public_page_trade_waits WHERE trade_no = ANY(%s)
    ORDER BY trade_no, first_at, wait_kind, reason"""
PAGE_REGIMES = "SELECT * FROM lab.public_page_regimes ORDER BY day"
PAGE_REVIEWS = """SELECT * FROM lab.public_page_reviews WHERE trade_no = ANY(%s)
    ORDER BY trade_no, at"""


def shown_trades(rows, limit=CLOSED_LIMIT):
    """The trade numbers the page shows: every open trade and the latest ``limit`` closed ones
    (the order of ``closed_history``)."""
    closed = sorted((r for r in rows if r["closed"]),
                    key=lambda r: (_aware(r["exit_at"]) or EPOCH, r["trade_no"]), reverse=True)
    return [r["trade_no"] for r in rows if not r["closed"]] + [
        r["trade_no"] for r in closed[:limit]]


def latest_runs(runs):
    """Each agent's newest run number, ascending."""
    newest = {}
    for r in runs:
        newest[r["agent"]] = max(newest.get(r["agent"], r["run_no"]), r["run_no"])
    return sorted(newest.values())


def cycle_runs(runs, schedule=None):
    """The current cycle: the runs of the newest run slot Jev has ranked (oldest run first).

    Under RESEARCH_SCHEDULE_V2 (``schedule`` names its daily run): the newest ranked full run
    and every ranked run after it, by slot, since their picks all stay valid until the next
    full run; the newest slot's runs, as under V1, while no full run has been ranked."""
    ranked = [r for r in runs if r["ranked_at"] is not None]
    if not ranked:
        return []
    plan = _v2_plan(schedule)
    if plan is not None:
        full = [_aware(r["run_at"]) for r in ranked
                if plan.run_kind(_aware(r["run_at"])) == FULL_RUN_KIND]
        if full:
            start = max(full)
            return sorted((r for r in ranked if _aware(r["run_at"]) >= start),
                          key=lambda r: (_aware(r["run_at"]), r["run_no"]))
    slot = max(_aware(r["run_at"]) for r in ranked)
    return sorted((r for r in ranked if _aware(r["run_at"]) == slot), key=lambda r: r["run_no"])


def ny_midnight(day):
    """The start of the New York day ``day``, in UTC."""
    return datetime.combine(day, time(0), tzinfo=NY).astimezone(UTC)


def scope_start(status, cycle):
    """The earliest decision Jev's scoped logs need: the New York day's start, or the current
    cycle's selection when that came before it."""
    start = ny_midnight(status["ny_today"])
    selected = [_aware(r["ranked_at"]) for r in cycle]
    return min([start, *selected])


def read_snapshot(conn, schedule=None):
    """Every public view, read in the caller's transaction (the service opens it REPEATABLE
    READ, READ ONLY, so all the figures describe one instant). ``schedule`` decides the current
    cycle's runs under RESEARCH_SCHEDULE_V2 (``cycle_runs``)."""
    snapshot = {"status": conn.execute("SELECT * FROM lab.public_dashboard_status").fetchone()}
    for name, sql in SNAPSHOT_QUERIES.items():
        snapshot[name] = conn.execute(sql).fetchall()
    latest = latest_runs(snapshot["runs"])
    snapshot["latest_picks"] = (
        conn.execute(CYCLE_PICKS, (latest,)).fetchall() if latest else [])
    numbers = shown_trades(snapshot["trades"])
    snapshot["trade_decisions"] = (
        conn.execute(TRADE_DECISIONS, (numbers,)).fetchall() if numbers else [])
    cycle = cycle_runs(snapshot["runs"], schedule)
    snapshot["cycle_picks"] = (conn.execute(
        CYCLE_PICKS, ([r["run_no"] for r in cycle],)).fetchall() if cycle else [])
    snapshot["day_decisions"] = conn.execute(
        DAY_DECISIONS, (scope_start(snapshot["status"], cycle),)).fetchall()
    # Page V2 (migration 028): the account's state, the traded levels and versions, the entry
    # waits of the shown trades and the days' market regimes.
    snapshot["page_status"] = conn.execute(PAGE_STATUS).fetchone()
    snapshot["page_trades"] = conn.execute(PAGE_TRADES).fetchall()
    snapshot["page_waits"] = (
        conn.execute(PAGE_WAITS, (numbers,)).fetchall() if numbers else [])
    snapshot["page_reviews"] = (
        conn.execute(PAGE_REVIEWS, (numbers,)).fetchall() if numbers else [])
    snapshot["regimes"] = conn.execute(PAGE_REGIMES).fetchall()
    # Page V3 (migration 029): equity snapshots, day starts, halts and soft limits, and the
    # STATS_EXCLUSION_V1 records.
    from catalyst_lab.experiment_v3 import read_v3

    snapshot.update(read_v3(conn))
    return snapshot


def _dec(value):
    return None if value is None else D(str(value))


def _plain(value):
    """A price as a Decimal without trailing zeros or an exponent (150.1860000000 -> 150.186)."""
    return None if value is None else D(f"{D(str(value)).normalize():f}")


def _int(value):
    return None if value is None else int(value)


def _ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _q(value, places):
    return None if value is None else value.quantize(places)


def _aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return value.astimezone(UTC)


def _ny_day(value):
    return _aware(value).astimezone(NY).date() if value is not None else None


def _minutes(start, end):
    if start is None or end is None:
        return None
    return int((_aware(end) - _aware(start)).total_seconds() // 60)


def _value(qty, price):
    """Quantity x price in USD, or None when either is unknown."""
    return None if qty is None or price is None else qty * price


def usd_text(value, signed=False):
    """``$1,234.50``; signed: ``+$1.20`` or ``−$0.40``."""
    if value is None:
        return "?"
    value = D(str(value)).quantize(USD_PLACES)
    sign = "−" if value < 0 else "+" if signed and value > 0 else ""
    return f"{sign}${abs(value):,.2f}"


def r_text(value):
    """``+0.92R``, ``−1.00R``."""
    if value is None:
        return "?"
    value = D(str(value)).quantize(D("0.01"))
    return f"{'−' if value < 0 else '+' if value > 0 else ''}{abs(value)}R"


def number_text(value):
    """A price or level for a sentence: plain digits, about six significant figures."""
    if value is None:
        return "?"
    value = D(str(value))
    if value >= 1000:
        return f"{value.quantize(D('0.01')):,}"
    if value >= 1:
        return f"{value.quantize(D('0.0001')).normalize():f}"
    if value == 0:
        return "0"
    return f"{value.quantize(D(1).scaleb(value.normalize().adjusted() - 5)).normalize():f}"


# --- Jev's actions in plain words -----------------------------------------------------------


def level_change(action, stop, target, basis):
    """``Raised stop to breakeven (331.17)``, ``Raised target to 7.61 (24-hour high)``..."""
    stop_text, target_text = number_text(stop), number_text(target)
    moves_stop = action in {"RAISE_STOP", "RAISE_STOP_AND_TARGET"}
    if moves_stop and basis in {"BREAKEVEN", "BREAKEVEN_AFTER_FEES"}:
        text = f"Raised stop to breakeven ({stop_text})"
    elif moves_stop:
        text = f"Raised stop to {stop_text}"
    elif action == "RAISE_TARGET":
        text = f"Raised target to {target_text}"
    else:
        return "Changed the levels"
    if action == "RAISE_STOP_AND_TARGET":
        text += f" and target to {target_text}"
    if basis in BASES and basis not in {"BREAKEVEN", "BREAKEVEN_AFTER_FEES"}:
        text += f" ({BASES[basis]})"
    return text


def jev_action(kind, outcome, action, stop, target, basis):
    """Jev's action in a few words; ``kind`` is a maintenance or continue-or-exit review
    decision."""
    if kind in {"DAY_REVIEW_DECISION", "DAY_REVIEW"}:
        return DAY_REVIEW_WORDS.get(outcome, "Review")
    if outcome == "APPLIED":
        return level_change(action, stop, target, basis)
    return MAINTENANCE_WORDS.get(outcome, "Reviewed")


# --- Trades ------------------------------------------------------------------------------------


def closed_trade(row):
    risk = _dec(row["planned_risk_usd"])
    net, gross = _dec(row["net_pnl_usd"]), _dec(row["gross_pnl_usd"])
    verified = bool(row["fees_known"] and row["inventory_reconciled"]
                    and not row["fixture_fee_evidence"] and net is not None)
    pnl = net if verified else gross
    qty, entry = _dec(row["bought_qty"]), _dec(row["avg_entry_price"])
    return {
        "trade_no": row["trade_no"], "symbol": row["symbol"], "tag": ARM_TAGS.get(row["arm"]),
        "entry_at": row["entry_at"], "exit_at": row["exit_at"],
        "minutes_in_trade": _minutes(row["entry_at"], row["exit_at"]),
        "qty": _plain(qty), "entry_value_usd": _value(qty, entry),
        "entry": _plain(entry), "exit": _plain(row["avg_exit_price"]),
        "limit": _plain(row["max_entry"]),
        "stop": _plain(row["current_stop"]), "target": _plain(row["current_target"]),
        "planned_stop": _plain(row["planned_stop"]),
        "planned_target": _plain(row["planned_target"]),
        "exit_reason": EXIT_REASONS.get(row["exit_reason"], row["exit_reason"]),
        "pnl_usd": pnl, "pnl_r": _ratio(pnl, risk),
        "fees_usd": _dec(row["fees_usd"]) if verified else None, "fees_pending": not verified,
        "gross_pnl_usd": gross,  # EXPERIMENT_DASHBOARD_V3: the daily columns' tooltip.
    }


def open_trade(row, as_of):
    risk = _dec(row["planned_risk_usd"])
    qty = _dec(row["open_qty"])
    if qty is None:
        qty = _dec(row["bought_qty"]) - _dec(row["sold_qty"] or 0)
    price, entry = _dec(row["price"]), _dec(row["avg_entry_price"])
    pnl = qty * (price - entry) if price is not None and entry is not None and qty else None
    last = None
    if row["jev_last_kind"]:
        last = {"text": jev_action(row["jev_last_kind"], row["jev_last_outcome"],
                                   row["jev_last_action"], row["jev_last_stop"],
                                   row["jev_last_target"], row["jev_last_basis"]),
                "outcome": row["jev_last_outcome"], "at": row["jev_last_at"],
                "confidence": row["jev_last_confidence"]}
    change = None
    if row["jev_change_at"] is not None and not (
            row["jev_last_outcome"] == "APPLIED" and row["jev_last_at"] == row["jev_change_at"]):
        change = {"text": level_change(row["jev_change_action"], row["jev_change_stop"],
                                       row["jev_change_target"], row["jev_change_basis"]),
                  "at": row["jev_change_at"]}
    return {
        "trade_no": row["trade_no"], "symbol": row["symbol"], "tag": ARM_TAGS.get(row["arm"]),
        "entry_at": row["entry_at"], "entry": _plain(row["avg_entry_price"]),
        "limit": _plain(row["max_entry"]),
        "qty": _plain(qty), "entry_value_usd": _value(qty, entry),
        "value_usd": _value(qty, price),
        "price": _plain(row["price"]), "price_at": row["price_at"],
        "stop": _plain(row["current_stop"]), "target": _plain(row["current_target"]),
        "planned_stop": _plain(row["planned_stop"]),
        "planned_target": _plain(row["planned_target"]),
        "pnl_usd": pnl, "pnl_r": _ratio(pnl, risk),
        "minutes_in_trade": int((as_of - _aware(row["entry_at"])).total_seconds() // 60)
        if row["entry_at"] else None,
        "closing": bool(row["exit_in_progress"]), "next_review_at": row["next_review_at"],
        "jev_last": last, "jev_last_change": change,
    }


def totals(trades):
    pnl = [t["pnl_usd"] for t in trades if t["pnl_usd"] is not None]
    rs = [t["pnl_r"] for t in trades if t["pnl_r"] is not None]
    wins = sum(1 for p in pnl if p > 0)
    return {
        "closed": len(trades), "wins": wins, "losses": sum(1 for p in pnl if p <= 0),
        "win_rate": _q(D(wins) / len(pnl), D("0.0001")) if pnl else None,
        "pnl_usd": sum(pnl, D(0)), "pnl_r": sum(rs, D(0)),
        "fees_pending": sum(1 for t in trades if t["fees_pending"]),
        "pnl_unknown": sum(1 for t in trades if t["pnl_usd"] is None),
    }


def past_days(closed, regimes=()):
    """Each New York day's closed trades (by exit), with the day's recorded market in words."""
    markets = {r["day"]: regime_words(r["tag"]) for r in regimes}
    by_day = defaultdict(list)
    for trade in closed:
        if trade["exit_at"] is not None:
            by_day[_ny_day(trade["exit_at"])].append(trade)
    days, cumulative = [], D(0)
    for day in sorted(by_day):
        figures = totals(by_day[day])
        cumulative += figures["pnl_usd"]
        days.append({"day": day.isoformat(), **{k: figures[k] for k in (
            "closed", "wins", "losses", "pnl_usd", "pnl_r", "fees_pending")},
            "cumulative_pnl_usd": cumulative,
            "market": (markets.get(day.isoformat()) or {}).get("short")})
    return days


def closed_history(closed, limit=CLOSED_LIMIT):
    """The latest ``limit`` closed trades, latest exit first, each with the total P&L of every
    closed trade up to and including it (in exit order; unknown P&L adds nothing)."""
    ordered = sorted(closed, key=lambda t: (_aware(t["exit_at"]) or EPOCH, t["trade_no"]))
    history, cumulative = [], D(0)
    for trade in ordered:
        if trade["pnl_usd"] is not None:
            cumulative += trade["pnl_usd"]
        history.append({**trade, "cumulative_pnl_usd": cumulative})
    return history[::-1][:limit]


# --- A trade's events and levels -----------------------------------------------------------------

EVENT_ORDER = {"PICK": 0, "SELECTION": 1, "MAINTENANCE": 2, "REVIEW_ANSWER": 3, "DAY_REVIEW": 4,
               "EXIT_FLAG": 5, "FLAG_ANSWER": 6}
PICK_VERDICTS = {"SELECTED": "Selected by Jev", "PASSED": "Passed by Jev",
                 "VETOED": "Vetoed by Jev", "NOT_RANKED": "Not ranked by Jev"}
DAY_REVIEW_EVENTS = {
    "CONTINUE": "24-hour review: continue for another 24 hours",
    "EXIT": "24-hour review: exit", "DISCARDED": "24-hour review discarded",
}


def by_trade(rows):
    """Decision rows grouped by trade, each trade's oldest first."""
    grouped = defaultdict(list)
    for row in rows:
        if row["trade_no"] is not None and row["at"] is not None:
            grouped[row["trade_no"]].append(row)
    for items in grouped.values():
        items.sort(key=lambda r: (_aware(r["at"]), EVENT_ORDER.get(r["kind"], 9)))
    return grouped


def _levels_of(row):
    """A review's recorded levels after it, or None (a hold or a failure records none)."""
    if row["kind"] not in {"MAINTENANCE", "DAY_REVIEW"}:
        return None
    if row["stop"] is None and row["target"] is None:
        return None
    return _plain(row["stop"]), _plain(row["target"])


def level_steps(row, decisions, cut, window_start):
    """The stop and target from the entry, one step at each recorded level, and whether the
    entry levels are known to hold until the first recorded one (display definitions)."""
    if row["entry_at"] is None:
        return [], True
    planned = (_plain(row["planned_stop"]), _plain(row["planned_target"]))
    seen = [(_aware(d["at"]), *_levels_of(d)) for d in decisions if _levels_of(d)]
    change_at = _aware(row.get("jev_change_at"))
    if change_at is not None and not row["closed"] and change_at not in {s[0] for s in seen}:
        seen.append((change_at, _plain(row["jev_change_stop"]),
                     _plain(row["jev_change_target"])))
    seen.sort(key=lambda s: s[0])
    final = (_plain(row["current_stop"]), _plain(row["current_target"]))
    entry_known = True
    if cut and seen:  # Levels only rise: a first recorded level equal to the entry's is known.
        entry_known = (seen[0][1] or planned[0], seen[0][2] or planned[1]) == planned
    elif cut and final != planned and window_start is not None:
        seen, entry_known = [(_aware(window_start), *final)], False
    steps = [{"at": row["entry_at"], "stop": planned[0], "target": planned[1],
              "known": entry_known}]
    for at, stop, target in seen:
        last = steps[-1]
        stop = stop if stop is not None else last["stop"]
        target = target if target is not None else last["target"]
        if (stop, target) == (last["stop"], last["target"]) and last["known"]:
            continue
        steps.append({"at": at, "stop": stop, "target": target, "known": True})
    return steps, entry_known


def level_move(action, before, after, basis):
    """``Raised stop 8.52 → 8.79 (breakeven)``; without the old level ``Raised stop to 8.79``."""
    old_stop, old_target = before or (None, None)
    new_stop, new_target = after
    parts = []
    for name, moved, old, new in (
            ("stop", action in {"RAISE_STOP", "RAISE_STOP_AND_TARGET"}, old_stop, new_stop),
            ("target", action in {"RAISE_TARGET", "RAISE_STOP_AND_TARGET"}, old_target,
             new_target)):
        if not moved:
            continue
        if old is not None and new is not None and old != new:
            parts.append(f"{name} {number_text(old)} → {number_text(new)}")
        else:
            parts.append(f"{name} to {number_text(new)}")
    if not parts:
        return "Changed the levels"
    return "Raised " + " and ".join(parts) + (f" ({BASES[basis]})" if basis in BASES else "")


def repeated_words(outcome, repeats, more):
    """``Held (9 reviews in a row)``, ``Review failed (5+ in a row)``."""
    count = f"{repeats}{'+' if more else ''}"
    unit = " reviews" if outcome == "HELD" else ""
    return f"{MAINTENANCE_WORDS[outcome]} ({count}{unit} in a row)"


def _agent_name(row):
    return row["agent"] or "The research agent"


def _decision_event(d, before):
    """One decision row as a trade event (codes stay private; agents' words stay theirs)."""
    kind, outcome = d["kind"], d["outcome"]
    event = {"at": d["at"], "since": d["at"], "kind": kind, "actor": d["actor"],
             "outcome": outcome, "confidence": d["confidence"], "repeats": 1,
             "repeats_more": False}
    if kind == "MAINTENANCE" and d.get("v5"):
        event["kind"], event["v5"] = "REVIEW", d["v5"]
        event["text"] = v5_words(d["v5"], outcome)
    elif kind == "MAINTENANCE":
        event["kind"] = "REVIEW"
        if outcome == "APPLIED":
            after = (_plain(d["stop"]), _plain(d["target"]))
            event["text"] = level_move(d["action"], before, after, d["basis"])
            event["stop"], event["target"] = after
        else:
            event["text"] = MAINTENANCE_WORDS.get(outcome, "Reviewed")
    elif kind == "DAY_REVIEW":
        event["text"] = DAY_REVIEW_EVENTS.get(outcome, "24-hour review")
        event["note"] = DAY_REVIEW_CODES.get(d["note"])
    elif kind in {"REVIEW_ANSWER", "FLAG_ANSWER"}:
        what = "the 24-hour review" if kind == "REVIEW_ANSWER" else "the exit flag"
        event["text"] = (f"{_agent_name(d)} answered {what}: "
                         f"{(outcome or 'no decision').lower()}")
    elif kind == "EXIT_FLAG":
        event["text"] = f"{_agent_name(d)} flagged an early exit"
    elif kind == "PICK":
        event["text"] = (f"Picked by {_agent_name(d)} (run {d['run_no']}): entry "
                         f"{number_text(d['entry'])}, stop {number_text(d['stop'])}, target "
                         f"{number_text(d['target'])}")
    elif kind == "SELECTION":
        rank = f" (rank {int(d['jev_rank'])})" if d["jev_rank"] is not None else ""
        event["text"] = PICK_VERDICTS.get(outcome, "Reviewed by Jev") + rank
    else:
        event["text"] = "Decision"
    if kind in {"REVIEW_ANSWER", "FLAG_ANSWER", "EXIT_FLAG", "PICK"}:
        event["agent"], event["note"] = d["agent"], d["note"]
    return event


V5_LABELS = (("invalidation", "invalidation met"), ("news", "news contradicts"))
V5_VERDICTS = {"YES": "yes", "NO": "no", "UNCERTAIN": "unsure"}
V5_CONFIRM = 3  # MAINTENANCE_ANSWER_RULE_V3: the third counted yes confirms.


def v5_review(row):
    """A ``public_page_reviews`` row as the decision's ``v5`` record (asked questions only)."""
    out = {"action": row["action"]}
    for key, _ in V5_LABELS:
        if row[f"{key}_verdict"] is not None:
            out[key] = {"p": _dec(row[f"{key}_p"]), "verdict": row[f"{key}_verdict"],
                        "effect": row[f"{key}_effect"],
                        "streak": _int(row[f"{key}_streak"])}
    return out


def with_v5(rows, reviews):
    """Decision rows with CRYPTO_MAINTENANCE_V5's answers attached (``v5``); a review whose
    streak is running reads as ``CONFIRMING`` (never folded with plain holds)."""
    by_key = {(r["trade_no"], _aware(r["at"])): v5_review(r) for r in reviews}
    if not by_key:
        return rows
    out = []
    for row in rows:
        v5 = by_key.get((row["trade_no"], _aware(row["at"]))) if row["kind"] == "MAINTENANCE" \
            else None
        if v5 is None:
            out.append(row)
            continue
        outcome = "CONFIRMING" if v5["action"] == "CONFIRMING" else row["outcome"]
        out.append({**row, "v5": v5, "outcome": outcome})
    return out


def v5_words(v5, outcome=None):
    """``Invalidation met? no (0.08) · news contradicts? no (0.12)``; a counted yes adds its
    streak (``yes (0.86), 2 of 3``); a confirmed question flags an early exit."""
    parts = []
    for key, label in V5_LABELS:
        answer = v5.get(key)
        if not answer:
            continue
        words = f"{label}? {V5_VERDICTS[answer['verdict']]}"
        if answer["p"] is not None:
            words += f" ({answer['p'].quantize(D('0.01'))})"
        if answer["effect"] == "COUNTED" and answer["streak"]:
            words += f", {answer['streak']} of {V5_CONFIRM}"
        elif answer["effect"] == "CONFIRMED":
            words += f", {V5_CONFIRM} of {V5_CONFIRM}"
        parts.append(words)
    text = " · ".join(parts) or "Reviewed"
    text = text[0].upper() + text[1:]
    if outcome == "FLAGGED":
        text += ": flagged for an early exit"
    elif outcome in {"FAILED", "REFUSED", "DISCARDED"}:
        text = MAINTENANCE_WORDS.get(outcome, "Reviewed")
    return text


def _track_v5(target, item):
    """Keep a folded line's range of invalidation probabilities and verdicts."""
    answer = (item.get("v5") or {}).get("invalidation")
    if not answer:
        return
    target.setdefault("v5_ps", []).append(answer["p"])
    target.setdefault("v5_verdicts", set()).add(answer["verdict"])


def v5_repeated(item, count):
    """``Held: invalidation met? no, 18 reviews in a row (p 0.02–0.12)``."""
    ps = [p for p in item.get("v5_ps", []) if p is not None]
    verdicts = item.get("v5_verdicts") or set()
    answer = "no" if verdicts == {"NO"} else "no or unsure"
    span = (f" (p {min(ps).quantize(D('0.01'))}–{max(ps).quantize(D('0.01'))})"
            if ps else "")
    return f"Held: invalidation met? {answer}, {count} reviews in a row{span}"


def _fold(events, window_start=None):
    """A trade's consecutive reviews with one unchanged outcome become one line. With its
    reviews cut at 60 (``window_start``: the oldest listed one), the streak that starts there
    may continue earlier ("+")."""
    out = []
    for event in events:
        last = out[-1] if out else None
        if (event["kind"] == "REVIEW" and event["outcome"] in REPEATING and last is not None
                and last["kind"] == "REVIEW" and last["outcome"] == event["outcome"]
                and bool(last.get("v5")) == bool(event.get("v5"))):
            last["repeats"] += 1
            last["at"], last["confidence"] = event["at"], event["confidence"]
            _track_v5(last, event)
            continue
        _track_v5(event, event)
        out.append(event)
    for event in out:
        ps, verdicts = event.pop("v5_ps", None), event.pop("v5_verdicts", None)
        if event["kind"] != "REVIEW" or event["repeats"] < 2:
            continue
        event["repeats_more"] = (window_start is not None and event["outcome"] in REPEATING
                                 and _aware(event["since"]) == _aware(window_start))
        if event.get("v5") and event["outcome"] == "HELD":
            event["text"] = v5_repeated({"v5_ps": ps, "v5_verdicts": verdicts},
                                        f"{event['repeats']}{'+' if event['repeats_more'] else ''}")
        else:
            event["text"] = repeated_words(event["outcome"], event["repeats"],
                                           event["repeats_more"])
    return out


def _entry_events(row, trade):
    base = (row["symbol"] or "?").split("/")[0]
    bought, entry, limit = _dec(row["bought_qty"]), _plain(row["avg_entry_price"]), trade["limit"]
    stop, target = _plain(row["planned_stop"]), _plain(row["planned_target"])
    return [
        {"at": row["entry_at"], "kind": "BUY", "qty": _plain(bought), "price": entry,
         "limit": limit, "value_usd": _value(bought, _dec(row["avg_entry_price"])),
         "text": f"Bought {_plain(bought)} {base} at {number_text(entry)}"
                 + (f", limit {number_text(limit)}" if limit is not None else "")},
        {"at": row["entry_at"], "kind": "LEVELS_SET", "stop": stop, "target": target,
         "text": f"Stop set at {number_text(stop)}, target {number_text(target)}"
                 + (" (fixed exit)" if row["arm"] == CONTROL else "")},
    ]


def _exit_events(row, trade):
    base = (row["symbol"] or "?").split("/")[0]
    sold, price, reason = _dec(row["sold_qty"]), trade["exit"], trade["exit_reason"] or "Closed"
    pnl, fees, pending = trade["pnl_usd"], trade["fees_usd"], trade["fees_pending"]
    result = "Fees pending" if pending else f"Fees {usd_text(fees)}"
    if pnl is None:
        result += " · P&L unknown"
    else:
        result += f" · P&L {usd_text(pnl, signed=True)}" + (" before fees" if pending else "")
    if trade["pnl_r"] is not None:
        result += f" ({r_text(trade['pnl_r'])})"
    return [
        {"at": row["exit_at"], "kind": "EXIT", "qty": _plain(sold), "price": price,
         "reason": reason,
         "text": f"Sold {_plain(sold)} {base} at {number_text(price)}: {reason}"
                 if price is not None and sold else reason},
        {"at": row["exit_at"], "kind": "RESULT", "pnl_usd": pnl, "pnl_r": trade["pnl_r"],
         "fees_usd": fees, "fees_pending": pending, "text": result},
    ]


def trade_story(row, trade, decisions, *, plan=None, waits=()):
    """A trade's ``events`` (oldest first) and ``levels`` (display definitions). ``plan``
    (CRYPTO_TRADE_PLAN_V1) adds the plan line and ``waits`` the entry waits before the buy."""
    reviews = [d for d in decisions if d["kind"] == "MAINTENANCE"]
    cut = len(reviews) >= PER_TRADE_LIMIT
    window_start = reviews[0]["at"] if reviews else None
    steps, entry_known = level_steps(row, decisions, cut, window_start)
    events = [_decision_event(d, None) for d in decisions if d["kind"] in {"PICK", "SELECTION"}]
    if plan is not None and plan["set_at"] is not None:
        events.append(plan_event(plan))
    events += [wait_event(w) for w in waits if w["first_at"] is not None]
    events.sort(key=lambda e: _aware(e["at"]))
    if row["entry_at"] is not None:
        events += _entry_events(row, trade)
    if cut:
        events.append({"at": None, "kind": "GAP", "text": "Earlier reviews not shown"})
    body = [d for d in decisions if d["kind"] not in {"PICK", "SELECTION"}]
    change_at = _aware(row.get("jev_change_at"))
    if (change_at is not None and not row["closed"] and cut
            and all(_aware(d["at"]) != change_at for d in body)):
        body.append({  # An open trade's last change, older than its listed reviews.
            "at": row["jev_change_at"], "kind": "MAINTENANCE", "actor": "JEV",
            "outcome": "APPLIED", "action": row["jev_change_action"], "confidence": None,
            "stop": row["jev_change_stop"], "target": row["jev_change_target"],
            "basis": row["jev_change_basis"], "note": None, "agent": None})
        body.sort(key=lambda r: (_aware(r["at"]), EVENT_ORDER.get(r["kind"], 9)))
    level = (_plain(row["planned_stop"]), _plain(row["planned_target"]), entry_known)
    story = []
    for d in body:
        story.append(_decision_event(d, level[:2] if level[2] else None))
        after = _levels_of(d)
        if after is not None:
            level = (after[0] or level[0], after[1] or level[1], True)
    events += _fold(story, window_start if cut else None)
    if row["closed"]:
        events += _exit_events(row, trade)
    return {"events": events, "levels": steps}


# --- Decisions -----------------------------------------------------------------------------------


def decision_text(row):
    """One plain line for one decision (the actor is shown beside it); an agent's own words
    stay in ``note``."""
    kind, outcome, symbol = row["kind"], row["outcome"], row["symbol"] or "a coin"
    if kind == "MAINTENANCE" and row.get("v5"):
        return f"{symbol}: " + v5_words(row["v5"], outcome)
    if kind in {"MAINTENANCE", "DAY_REVIEW"}:
        return f"{symbol}: " + jev_action(kind, outcome, row["action"], row["stop"],
                                          row["target"], row["basis"])
    if kind == "REVIEW_ANSWER":
        return f"{symbol}: review answer, {(outcome or 'no decision').lower()}"
    if kind == "FLAG_ANSWER":
        return f"{symbol}: answered Jev's exit flag, {(outcome or 'no decision').lower()}"
    if kind == "EXIT_FLAG":
        return f"{symbol}: flagged for an early exit"
    if kind == "PICK":
        return (f"{symbol}: proposed entry {number_text(row['entry'])}, stop "
                f"{number_text(row['stop'])}, target {number_text(row['target'])}")
    if kind == "SELECTION":
        rank = f" (rank {int(row['jev_rank'])})" if row["jev_rank"] is not None else ""
        return {"SELECTED": f"{symbol}: selected{rank}", "PASSED": f"{symbol}: passed{rank}",
                "VETOED": f"{symbol}: vetoed", "NOT_RANKED": f"{symbol}: not ranked"}.get(
            outcome, f"{symbol}: reviewed")
    return f"{symbol}: decision"


def decision(row):
    note = row["note"]
    if row["kind"] == "DAY_REVIEW":
        note = DAY_REVIEW_CODES.get(note)
    elif row["kind"] in {"MAINTENANCE", "SELECTION"}:
        note = None  # Internal codes stay out of the public lines.
    return {"at": row["at"], "actor": row["actor"], "agent": row["agent"], "kind": row["kind"],
            "outcome": row["outcome"], "symbol": row["symbol"], "trade_no": row["trade_no"],
            "text": decision_text(row), "note": note, "confidence": row["confidence"],
            **({"v5": row["v5"]} if row.get("v5") else {})}


def _coins(symbols, limit=5):
    coins = [s.split("/")[0] for s in (symbols or "").split(",") if s]
    listed = ", ".join(coins[:limit])
    return listed + (f" +{len(coins) - limit} more" if len(coins) > limit else "")


def run_decisions(run, coins=5):
    """Two lines per research run: the agent's picks and Jev's selection (naming at most
    ``coins`` selected coins)."""
    items, picks = [], int(run["picks"] or 0)
    common = {"outcome": None, "symbol": None, "trade_no": None, "confidence": None,
              "run_no": run["run_no"]}
    if run["submitted_at"] is not None:
        items.append({**common, "at": run["submitted_at"], "actor": "RESEARCH_AGENT",
                      "agent": run["agent"], "kind": "RUN",
                      "text": f"Sent {picks} pick{'' if picks == 1 else 's'} (run "
                              f"{run['run_no']})", "note": None})
    if run["ranked_at"] is not None:
        selected, coins = int(run["selected"] or 0), _coins(run["selected_symbols"], coins)
        extra = [f"{int(run[k])} {words}" for k, words in (("vetoed", "vetoed"),
                 ("not_ranked", "not ranked")) if run[k]]
        items.append({**common, "at": run["ranked_at"], "actor": "JEV", "agent": None,
                      "kind": "SELECTION", "outcome": "RANKED",
                      "text": f"Selected {selected} of {picks} (run {run['run_no']})"
                              + (f": {coins}" if coins else ""),
                      "note": " · ".join(extra) or None})
    return items


def repeated_text(item):
    """``UNI/USD: Held (9 reviews in a row)``, ``UNI/USD: Review failed (5+ in a row)``..."""
    return (f"{item['symbol'] or 'a coin'}: "
            + repeated_words(item["outcome"], item["repeats"], item["repeats_more"]))


def feed(rows, runs=(), length=FEED_LENGTH, *, window_full=False, actor=None):
    """Decisions newest first (``actor``: only that actor's). A trade's consecutive reviews with
    the same unchanged outcome (``REPEATING``; Jev reviews every minute) become one line with
    their count, even when other trades' lines interleave; ``+`` marks a count that may continue
    past the rows read (the whole window, or the view's 60 reviews of that trade, is full)."""
    items = [*(decision(r) for r in rows), *(i for run in runs for i in run_decisions(run))]
    return fold_lines([i for i in items if actor is None or i["actor"] == actor], length,
                      window_full=window_full)


def fold_lines(items, length, *, window_full=False):
    """``feed``'s ordering and folding for lines already built (equal times keep their order).
    A streak that starts at its trade's oldest listed review may continue earlier when the
    reviews were cut (the window, or the view's 60 of that trade, is full): "+"."""
    items = sorted(items, key=lambda item: _aware(item["at"]), reverse=True)
    reviews = Counter(i["trade_no"] for i in items if i["kind"] == "MAINTENANCE")
    oldest = {}  # trade -> its oldest listed review (the items run newest first)
    for item in items:
        if item["kind"] == "MAINTENANCE":
            oldest[item["trade_no"]] = _aware(item["at"])
    out, streaks = [], {}  # streaks: trade -> its newest line while that line may still grow.
    for item in items:
        trade = item.get("trade_no")
        repeating = (item["kind"] == "MAINTENANCE" and item["outcome"] in REPEATING
                     and trade is not None)
        current = streaks.get(trade)
        if repeating and current is not None and current["outcome"] == item["outcome"] \
                and bool(current.get("v5")) == bool(item.get("v5")):
            current["repeats"] += 1
            current["since"] = item["at"]
            _track_v5(current, item)
            continue
        line = None
        if len(out) < length:  # A full list skips older lines but still grows open streaks.
            line = {**item, "repeats": 1, "since": item["at"]}
            _track_v5(line, item)
            out.append(line)
        if trade is not None:  # Any other decision on the trade ends its open streak.
            streaks[trade] = line if repeating else None
    for item in out:
        item.setdefault("run_no", None)
        trade = item["trade_no"]
        item["repeats_more"] = bool(
            item["repeats"] > 1 and item["kind"] == "MAINTENANCE"
            and _aware(item["since"]) == oldest.get(trade)
            and (window_full or reviews[trade] >= PER_TRADE_LIMIT))
        ps, verdicts = item.pop("v5_ps", None), item.pop("v5_verdicts", None)
        if item["repeats"] > 1 and item.get("v5") and item["outcome"] == "HELD":
            count = f"{item['repeats']}{'+' if item['repeats_more'] else ''}"
            item["text"] = (f"{item['symbol'] or 'a coin'}: "
                            + v5_repeated({"v5_ps": ps, "v5_verdicts": verdicts}, count))
        elif item["repeats"] > 1:
            item["text"] = repeated_text(item)
    return out


# --- Agents ------------------------------------------------------------------------------------


def _plan(schedule):
    from catalyst_lab.research_schedule import ResearchSchedule

    return ResearchSchedule(**schedule) if isinstance(schedule, dict) else schedule


def _v2_plan(schedule):
    """The schedule when it is RESEARCH_SCHEDULE_V2 (it names its daily full run), else None."""
    try:
        plan = _plan(schedule)
    except Exception:  # noqa: BLE001 -- an unreadable schedule keeps the V1 display.
        return None
    return plan if getattr(plan, "daily", None) is not None else None


def next_run(schedule, now):
    """The next scheduled research run (``RESEARCH_SCHEDULE_V1``), or None when unreadable."""
    try:
        return _plan(schedule).next_after(now)
    except Exception:  # noqa: BLE001 -- an unreadable schedule shows no next run.
        return None


def schedule_summary(schedule, now):
    """``Every 2 hours``, ``Daily at 08:00`` or ``3 runs a day``, with the run times, their time
    zone and the next run; None when the schedule is unreadable. Under RESEARCH_SCHEDULE_V2
    the words name the daily full run and the updates (``Daily at 08:00 + updates every 2
    hours``), with ``daily``, the next run's kind and the next full run."""
    try:
        plan = _plan(schedule)
        minutes = [int(run[:2]) * 60 + int(run[3:]) for run in plan.runs]
    except Exception:  # noqa: BLE001 -- an unreadable schedule shows no schedule.
        return None
    gaps = {b - a for a, b in zip(minutes, minutes[1:], strict=False)}
    gaps.add(minutes[0] + 24 * 60 - minutes[-1])
    gap = min(gaps)
    if len(minutes) == 1:
        text = f"Daily at {plan.runs[0]}"
    elif len(gaps) > 1:
        text = f"{len(minutes)} runs a day"
    elif gap % 60:
        text = f"Every {gap} minutes"
    else:
        text = "Every hour" if gap == 60 else f"Every {gap // 60} hours"
    summary = {"text": text, "timezone": plan.timezone, "times": list(plan.runs),
               "next_run_at": next_run(plan, now)}
    daily = getattr(plan, "daily", None)
    if daily is None:
        return summary
    updates = len(minutes) - 1
    if not updates:
        words = f"Daily at {daily}"
    elif len(gaps) > 1:
        words = f"Daily at {daily} + {updates} update{'s' if updates > 1 else ''} a day"
    else:
        words = f"Daily at {daily} + updates {text[0].lower()}{text[1:]}"
    upcoming = summary["next_run_at"]
    try:
        full = plan.next_full_after(now)
    except Exception:  # noqa: BLE001 -- as ``next_run``: no time, no next full run shown.
        full = None
    return {**summary, "text": words, "daily": daily,
            "next_run_kind": plan.run_kind(upcoming) if upcoming else None,
            "next_full_run_at": full}


def run_summary(run):
    """One research run: its slot, when it was received and ranked, its picks, Jev's selection
    and the trades it led to."""
    return {
        "run_no": run["run_no"], "agent": run["agent"], "run_at": run["run_at"],
        "received_at": run["submitted_at"], "ranked_at": run["ranked_at"],
        "picks": _int(run["picks"]), "selected": _int(run["selected"]),
        "vetoed": _int(run["vetoed"]), "not_ranked": _int(run["not_ranked"]),
        "traded": _int(run["traded"]),
    }


def todays_runs(runs, today):
    """The New York day's research runs (by their slot), the latest slot first."""
    ordered = sorted(runs, key=lambda r: (_aware(r["run_at"]), r["run_no"]), reverse=True)
    return [run_summary(r) for r in ordered if _ny_day(r["run_at"]) == today]


def pending_run(runs, cycle):
    """The newest run Jev has not ranked yet, when it is newer than the current cycle."""
    slot = max(_aware(r["run_at"]) for r in cycle) if cycle else None
    waiting = [r for r in runs if r["ranked_at"] is None
               and (slot is None or _aware(r["run_at"]) > slot)]
    return run_summary(max(waiting, key=lambda r: r["run_no"])) if waiting else None


def validity_limit(schedule, slot):
    """The latest time a run's picks stay valid (the next run plus the grace), or None."""
    try:
        return _plan(schedule).validity_limit(_aware(slot))
    except Exception:  # noqa: BLE001 -- an unreadable schedule shows no limit.
        return None


PICK_STATUS = {"IN_TRADE": "In trade", "CLOSED": "Closed", "NO_ENTRY": "No entry yet",
               "EXPIRED": "Expired", "NOT_SELECTED": "Not selected", "NO_VERDICT": "No verdict",
               # RESEARCH_SCHEDULE_V2 only: a later run of the cycle selected the coin again.
               "REPLACED": "Replaced by a later pick"}
FULL_RUN_KIND = "FULL"  # research_schedule.FULL_RUN, the daily run of RESEARCH_SCHEDULE_V2.


def pick_status(verdict, trade, closed, valid_until, as_of):
    """``(code, words)`` of a current-cycle pick (display definitions)."""
    if trade is not None:
        code = "CLOSED" if closed else "IN_TRADE"
        return code, f"{PICK_STATUS[code]} #{trade['trade_no']}"
    if verdict is None:
        code = "NO_VERDICT"
    elif verdict != "SELECTED":
        code = "NOT_SELECTED"
    elif valid_until is not None and as_of >= _aware(valid_until):
        code = "EXPIRED"
    else:
        code = "NO_ENTRY"
    return code, PICK_STATUS[code]


VERDICT_ORDER = {"SELECTED": 0, "PASSED": 1, "VETOED": 2, "NOT_RANKED": 3}


def _pick_order(verdict, symbol):
    """Jev's order: selected picks by rank, then passed, vetoed and unranked ones."""
    verdict = verdict or {}
    rank = verdict.get("jev_rank")
    return (VERDICT_ORDER.get(verdict.get("outcome"), 4), rank is None,
            rank if rank is not None else 0, symbol or "")


def research_agents(runs, latest_picks, recent, schedule, now, *, window_full=False):
    by_agent = defaultdict(list)
    for run in runs:
        by_agent[run["agent"] or UNNAMED_AGENT].append(run)
    upcoming = next_run(schedule, now)
    cards = []
    for agent, agent_runs in sorted(by_agent.items(), key=lambda kv: -kv[1][-1]["run_no"]):
        last = agent_runs[-1]
        verdicts = {p["symbol"]: p for p in latest_picks
                    if p["kind"] == "SELECTION" and p["run_no"] == last["run_no"]}
        picks = sorted((p for p in latest_picks
                        if p["kind"] == "PICK" and p["run_no"] == last["run_no"]),
                       key=lambda p: _pick_order(verdicts.get(p["symbol"]), p["symbol"]))
        own = [r for r in recent if r["actor"] == "RESEARCH_AGENT"
               and (r["agent"] or UNNAMED_AGENT) == agent]
        cards.append({
            "agent": agent, "runs": len(agent_runs), "last_run_no": last["run_no"],
            "last_run_at": last["run_at"], "received_at": last["submitted_at"],
            "picks": _int(last["picks"]), "selected": _int(last["selected"]),
            "traded": _int(last["traded"]), "next_run_at": upcoming,
            "picks_list": [{
                "symbol": p["symbol"], "kind": p["action"], "entry": _plain(p["entry"]),
                "stop": _plain(p["stop"]), "target": _plain(p["target"]), "why": p["note"],
                "jev": (verdicts.get(p["symbol"]) or {}).get("outcome"),
                "jev_rank": _int((verdicts.get(p["symbol"]) or {}).get("jev_rank")),
                "trade_no": p["trade_no"]} for p in picks],
            "decisions": feed(own, (), 6, window_full=window_full, actor="RESEARCH_AGENT"),
        })
    return cards


def research_cycle(cycle, rows, schedule, as_of, trades):
    """The current cycle (display definitions): its slot, when it was received and ranked, how
    long its picks stay valid, and each pick with Jev's verdict and its status. ``rows`` are
    the cycle's PICK and SELECTION decisions; ``trades`` maps a trade number to (the trade,
    closed). None before Jev's first ranking."""
    if not cycle:
        return None
    numbers = {r["run_no"] for r in cycle}
    slot = cycle[0]["run_at"]
    valid_until = validity_limit(schedule, slot)
    verdicts = {(p["run_no"], p["symbol"]): p for p in rows
                if p["kind"] == "SELECTION" and p["run_no"] in numbers}
    picks = sorted((p for p in rows if p["kind"] == "PICK" and p["run_no"] in numbers),
                   key=lambda p: (_pick_order(verdicts.get((p["run_no"], p["symbol"])),
                                              p["symbol"]), p["run_no"]))
    # RESEARCH_SCHEDULE_V2: the latest slot of the cycle that selected each coin, so an earlier
    # untraded selection of it reads as replaced (RESEARCH_RUN_SUPERSESSION_V2's same-coin rule).
    latest, slots = {}, {r["run_no"]: _aware(r["run_at"]) for r in cycle}
    if _v2_plan(schedule) is not None:
        for (run_no, symbol), verdict in verdicts.items():
            if verdict.get("outcome") == "SELECTED":
                latest[symbol] = max(latest.get(symbol, slots[run_no]), slots[run_no])
    listed = []
    for p in picks:
        verdict = verdicts.get((p["run_no"], p["symbol"])) or {}
        trade, closed = trades.get(p["trade_no"], (None, False))
        code, words = pick_status(verdict.get("outcome"), trade, closed, valid_until, as_of)
        if code in {"NO_ENTRY", "EXPIRED"} and p["symbol"] in latest and (
                latest[p["symbol"]] > slots[p["run_no"]]):
            code, words = "REPLACED", PICK_STATUS["REPLACED"]
        listed.append({
            "symbol": p["symbol"], "agent": p["agent"], "run_no": p["run_no"],
            "kind": p["action"], "entry": _plain(p["entry"]), "stop": _plain(p["stop"]),
            "target": _plain(p["target"]), "jev": verdict.get("outcome"),
            "jev_rank": _int(verdict.get("jev_rank")), "trade_no": p["trade_no"],
            "status": code, "status_text": words,
            "trade_pnl_usd": trade["pnl_usd"] if trade else None,
        })
    total = {k: sum(_int(r[k]) or 0 for r in cycle) for k in ("picks", "selected", "traded")}
    return {
        "runs": sorted(numbers), "run_no": max(numbers),
        "agents": sorted({r["agent"] or UNNAMED_AGENT for r in cycle}), "run_at": slot,
        "received_at": min(_aware(r["submitted_at"]) for r in cycle),
        "ranked_at": min(_aware(r["ranked_at"]) for r in cycle), "valid_until": valid_until,
        "live": valid_until is None or as_of < valid_until, **total, "picks_list": listed,
    }


def trade_lines(pairs, since):
    """The trades' entries and exits at or after ``since``, as log lines (actor TRADE);
    ``pairs`` are (trades view row, the trade as the page shows it)."""
    lines = []
    for row, trade in pairs:
        symbol, number = row["symbol"], row["trade_no"]
        common = {"actor": "TRADE", "agent": None, "outcome": None, "symbol": symbol,
                  "trade_no": number, "run_no": None, "confidence": None, "note": None}
        if row["entry_at"] is not None and _aware(row["entry_at"]) >= since:
            lines.append({**common, "at": row["entry_at"], "kind": "BUY",
                          "text": f"{symbol}: Bought {_plain(row['bought_qty'])} at "
                                  f"{number_text(row['avg_entry_price'])} (#{number})"})
        if row["closed"] and row["exit_at"] is not None and _aware(row["exit_at"]) >= since:
            pnl, pending = trade["pnl_usd"], trade["fees_pending"]
            note = None if pnl is None else (f"P&L {usd_text(pnl, signed=True)}"
                                             + (" before fees" if pending else ""))
            lines.append({**common, "at": row["exit_at"], "kind": "SELL", "note": note,
                          "pnl_usd": pnl,
                          "text": f"{symbol}: Sold at {number_text(row['avg_exit_price'])} · "
                                  f"{trade['exit_reason'] or 'Closed'} (#{number})"})
    return lines


def _scoped(items, window_full):
    """A scope's lines, newest first and folded, and whether older ones were left out."""
    lines = fold_lines(items, SCOPE_LENGTH + 1, window_full=window_full)
    return lines[:SCOPE_LENGTH], len(lines) > SCOPE_LENGTH or window_full


def jev_scopes(status, runs, cycle, cycle_rows, day_rows, pairs, *, window_full=False):
    """Jev's scoped logs (display definitions): ``cycle`` since the current cycle's selection
    (None before the first) and ``today`` since the New York day's start."""
    jev = [r for r in day_rows if r["actor"] == "JEV" and r["at"] is not None]

    def scope(since, lines):
        items = [decision(r) for r in jev if _aware(r["at"]) >= since]
        return _scoped([*items, *lines, *trade_lines(pairs, since)], window_full)

    this_cycle = None
    if cycle:
        since = min(_aware(r["ranked_at"]) for r in cycle)
        verdicts = sorted((p for p in cycle_rows if p["kind"] == "SELECTION"
                           and p["run_no"] in {r["run_no"] for r in cycle}),
                          key=lambda p: (_pick_order(p, p["symbol"]), p["run_no"]))
        summary = [i for r in cycle for i in run_decisions(r, coins=20) if i["actor"] == "JEV"]
        lines, more = scope(since, [*summary, *(decision(p) for p in verdicts)])
        this_cycle = {"run_no": max(r["run_no"] for r in cycle), "since": since,
                      "lines": lines, "more": more}
    start = ny_midnight(status["ny_today"])
    ranked = [r for r in runs if r["ranked_at"] is not None and _aware(r["ranked_at"]) >= start]
    summary = [i for r in ranked for i in run_decisions(r, coins=20) if i["actor"] == "JEV"]
    lines, more = scope(start, summary)
    return this_cycle, {"day": status["ny_today"], "since": start, "lines": lines, "more": more}


def failed_reviews_today(stories, today):
    """(count, may be more): the listed reviews Jev did not answer on the New York day
    ``today`` (``stories``: decisions by trade). A trade whose 60 listed reviews all fall today
    may have more."""
    count, more = 0, False
    for decisions in stories.values():
        reviews = [d for d in decisions if d["kind"] == "MAINTENANCE"]
        count += sum(1 for d in reviews if d["outcome"] == "FAILED" and _ny_day(d["at"]) == today)
        more = more or (len(reviews) >= PER_TRADE_LIMIT and _ny_day(reviews[0]["at"]) == today)
    return count, more


def jev_card(status, runs, recent, *, window_full=False, day_rows=None, day_full=False,
             cycle=(), cycle_rows=(), pairs=()):
    breaker, outcome = status["jev_breaker"], status["jev_last_call_outcome"]
    if breaker in {"OPEN", "HALF_OPEN"}:
        health = "Breaker open"
    elif outcome is not None and outcome != "VALID":
        health = "Unavailable"
    elif breaker == "CLOSED" or outcome == "VALID":
        health = "OK"
    else:
        health = "No calls yet"
    ranked = [r for r in runs if r["ranked_at"] is not None]
    latest = max(ranked, key=lambda r: _aware(r["ranked_at"])) if ranked else None
    selection = None
    if latest is not None:
        selection = {
            "run_no": latest["run_no"], "agent": latest["agent"], "at": latest["ranked_at"],
            "picks": _int(latest["picks"]), "selected": _int(latest["selected"]),
            "passed": max(0, _int(latest["ranked"]) - _int(latest["selected"])),
            "vetoed": _int(latest["vetoed"]), "not_ranked": _int(latest["not_ranked"]),
            "symbols": [s.split("/")[0] for s in (latest["selected_symbols"] or "").split(",")
                        if s],
        }
    day_rows = recent if day_rows is None else day_rows
    failed, failed_more = failed_reviews_today(by_trade(day_rows), status["ny_today"])
    this_cycle, today = jev_scopes(status, runs, cycle, cycle_rows, day_rows, pairs,
                                   window_full=day_full)
    return {
        "health": health, "breaker": breaker, "last_call_at": status["jev_last_call_at"],
        "last_call_ok": outcome == "VALID" if outcome else None,
        "calls_today": _int(status["jev_calls_today"]),
        "failed_today": failed, "failed_today_more": failed_more or day_full,
        "reviews": status["heartbeat_management_reviews"],
        "latest_selection": selection,
        "decisions": feed(recent, runs, JEV_LOG_LENGTH, window_full=window_full, actor="JEV"),
        "cycle": this_cycle, "today": today,
    }


# --- Page V2: traded levels, entry waits, the status line, limits, market and results --------
#
# Display definitions of EXPERIMENT_DASHBOARD_V2 (package public-page; docs/packages/
# public-page.md). Nothing is estimated: a figure the ledger does not hold is omitted (None).

PLAN_VERSION = "CRYPTO_TRADE_PLAN_V1"
RULE_FIELDS = ("risk_policy", "trade_plan_policy", "entry_pacing_policy", "stop_limit_policy")
MIN_SAMPLE = 30  # result_dimensions.CELL_MINIMUM: a net-R average under it shows its count only.
PACING_RECENT_SECONDS = 300  # A pacing wait this recent means new entries are being held back.
LIMIT_SCALE = D(7) / D(6)  # The limits bar spans 7/6 of the hard limit (3% -> 3.5%).
FRACTION = D("0.0001")
PCT_PLACES = D("0.01")
STATES = {"TRADING": "Trading", "PAUSED": "Paused", "SOFT_LIMIT": "Soft limit",
          "HALTED": "Halted", "STOPPED": "Stopped"}
WAIT_WORDS = {
    "MARKET_DROP": "market dropping", "ENTRY_RATE_LIMIT": "entry pace",
    "MACRO_EVENT_WINDOW": "release window", "MARKET_BREADTH_UNAVAILABLE":
    "market breadth unavailable", "MACRO_CALENDAR_EXPIRED": "release calendar expired",
    "DAILY_SOFT_LOSS_LIMIT": "daily soft loss limit",
}
HALT_WORDS = {"OPERATOR_PAUSE": "paused by the operator"}
TREND_WORDS = {"UP": "Bitcoin uptrend", "DOWN": "Bitcoin downtrend", "MIXED": "Bitcoin mixed"}
TREND_SHORT = {"UP": "Uptrend", "DOWN": "Downtrend", "MIXED": "Mixed trend"}
VOLATILITY_WORDS = {"LOW": "low volatility", "NORMAL": "normal volatility",
                    "HIGH": "high volatility"}
BREADTH_WORDS = {"BROAD": "most coins above their 20-day average",
                 "NARROW": "most coins below their 20-day average"}
BUCKET_WORDS = {"DOWN_2+": "down 2% or more", "DOWN": "falling", "FLAT": "flat",
                "UP": "rising", "UP_2+": "up 2% or more"}


def et_time(value):
    """``15:24 ET`` (New York time)."""
    return _aware(value).astimezone(NY).strftime("%H:%M") + " ET"


def _frac(value):
    return None if value is None else value.quantize(FRACTION)


def _pct(fraction):
    """A fraction as a percent figure (0.0213 -> 2.13)."""
    return None if fraction is None else (fraction * 100).quantize(PCT_PLACES)


def next_ny_midnight(as_of):
    day = _aware(as_of).astimezone(NY).date()
    return ny_midnight(day + timedelta(days=1))


def rule_key(page_row):
    """The account-wide rule versions a trade (or the newest setup) recorded."""
    return tuple((page_row or {}).get(f) for f in RULE_FIELDS)


def traded_row(row, page_row):
    """The trades-view row with the levels actually traded: under CRYPTO_TRADE_PLAN_V1 the
    plan's stop and target (and the risk the reservation planned on); every other trade its
    research levels, unchanged. The research levels stay under ``research_*``."""
    out = {**row, "research_stop": row["planned_stop"],
           "research_target": row["planned_target"]}
    if page_row and page_row.get("trade_plan_policy") == PLAN_VERSION and \
            page_row.get("plan_stop") is not None and page_row.get("plan_target") is not None:
        out["planned_stop"], out["planned_target"] = page_row["plan_stop"], page_row[
            "plan_target"]
        if page_row.get("risk_usd") is not None:
            out["planned_risk_usd"] = page_row["risk_usd"]
    return out


def trade_plan(row, page_row):
    """A trade's plan table: research and traded levels with the rule of each (None without
    CRYPTO_TRADE_PLAN_V1)."""
    if not page_row or page_row.get("trade_plan_policy") != PLAN_VERSION or \
            page_row.get("plan_stop") is None:
        return None
    hr = _dec(page_row.get("hourly_range_fraction"))
    multiple = _plain(page_row.get("stop_range_multiple"))
    r_multiple = _plain(page_row.get("target_r_multiple"))
    hr_words = f" ({_pct(hr)}%)" if hr is not None else ""
    stop_rule = (f"{multiple}× the hourly range{hr_words} below the entry"
                 if page_row.get("stop_basis") == "HOURLY_RANGE_FLOOR"
                 else f"Research stop kept: already {multiple}× the hourly range{hr_words} "
                      "or more below the entry")
    target_rule = (f"Capped at {r_multiple}R above the entry"
                   if page_row.get("target_basis") == "PLAN_CAP"
                   else f"Research target kept: under the {r_multiple}R cap")
    window = page_row.get("window_minutes")
    return {
        "version": PLAN_VERSION, "set_at": page_row.get("admitted_at"),
        "research_stop": _plain(row["planned_stop"]),
        "research_target": _plain(row["planned_target"]),
        "stop": _plain(page_row["plan_stop"]), "target": _plain(page_row["plan_target"]),
        "target_cap": _plain(page_row.get("plan_target_cap")),
        "stop_basis": page_row.get("stop_basis"), "target_basis": page_row.get("target_basis"),
        "hourly_range": _plain(page_row.get("hourly_range")),
        "hourly_range_pct": _pct(hr), "stop_rule": stop_rule, "target_rule": target_rule,
        "window_hours": int(window) // 60 if window is not None else None,
        "risk_usd": _dec(page_row.get("risk_usd")),
    }


def plan_event(plan):
    stop = (f"stop {number_text(plan['research_stop'])} → {number_text(plan['stop'])}"
            if plan["stop"] != plan["research_stop"] else
            f"stop {number_text(plan['stop'])} kept")
    target = (f"target {number_text(plan['research_target'])} → {number_text(plan['target'])}"
              if plan["target"] != plan["research_target"] else
              f"target {number_text(plan['target'])} kept")
    window = f", {plan['window_hours']}-hour window" if plan["window_hours"] else ""
    return {"at": plan["set_at"], "kind": "PLAN", "actor": "APP",
            "text": f"Plan set: {stop}, {target}{window}",
            "note": f"{plan['stop_rule']}; {plan['target_rule'][0].lower()}"
                    f"{plan['target_rule'][1:]}"}


def wait_event(w):
    reason = w["reason"]
    words = WAIT_WORDS.get(reason, "entry held back")
    if reason == "MACRO_EVENT_WINDOW" and w.get("release_kind"):
        words = f"{w['release_kind']} release window"
    note = None
    if reason == "MARKET_DROP" and w.get("worst_median_1h_return") is not None:
        move = _pct(_dec(w["worst_median_1h_return"]))
        note = f"Median coin {'−' if move < 0 else '+'}{abs(move)}% over an hour at the worst"
    count = int(w["waits"] or 0)
    checks = f"{count} check{'' if count == 1 else 's'}"
    return {"at": w["first_at"], "since": w["first_at"], "until": w["last_at"], "kind": "WAIT",
            "actor": "PACING" if w["wait_kind"] == "PACING" else "LIMIT",
            "reason": reason, "waits": count,
            "text": f"Entry waited: {words}", "note": f"{checks}" + (f" · {note}" if note else "")}


def regime_words(tag):
    """``{"words", "short", "selloff"}`` of a MARKET_REGIME_V1 tag; unknown parts are left
    out (never guessed). None without a tag."""
    if not tag:
        return None
    trend, volatility, breadth, selloff = (tag.split("/") + [None] * 4)[:4]
    parts = [w for w in (TREND_WORDS.get(trend), VOLATILITY_WORDS.get(volatility),
                         BREADTH_WORDS.get(breadth)) if w]
    short = [w for w in (TREND_SHORT.get(trend), VOLATILITY_WORDS.get(volatility)) if w]
    if selloff == "SELLOFF":
        short.append("sell-off")
    return {"tag": tag, "words": ", ".join(parts) or None, "short": ", ".join(short) or None,
            "selloff": None if selloff in (None, "UNKNOWN") else selloff == "SELLOFF"}


def regime_day(row):
    """One recorded day's regime with its words and worst hour."""
    words = regime_words(row["tag"]) or {}
    worst = _dec(row.get("worst_hour_pct"))
    return {"day": row["day"], **words, "worst_hour_at": row.get("worst_hour_start"),
            "worst_hour_pct": worst.quantize(D("0.1")) if worst is not None else None}


def market_block(regimes, page_status, today):
    """The latest recorded day's market (today's once its night has run), and whether the
    entry pacing's market-drop rule applies to new setups."""
    days = [regime_day(r) for r in regimes if r["tag"]]
    latest = days[-1] if days else None
    rule = bool(page_status and page_status.get("rules_entry_pacing_policy"))
    if latest is None:
        return {"day": None, "pacing_rule": rule}
    return {**latest, "is_today": latest["day"] == (today.isoformat() if today else None),
            "pacing_rule": rule}


def entry_market(page_row):
    """The market at a trade's entry (TRADE_REGIME), in words; None when not recorded."""
    if not page_row or not (page_row.get("regime_median_coin_1h") or
                            page_row.get("regime_day_tag")):
        return None
    pct = _dec(page_row.get("regime_median_coin_1h_pct"))
    bucket = page_row.get("regime_median_coin_1h")
    words = []
    if pct is not None:
        pct = pct.quantize(D("0.1"))
        words.append(f"Median coin {'−' if pct < 0 else '+'}{abs(pct)}% the hour before")
    elif bucket in BUCKET_WORDS:
        words.append(f"Median coin {BUCKET_WORDS[bucket]} the hour before")
    day = regime_words(page_row.get("regime_day_tag"))
    if day and day["short"]:
        words.append(f"day: {day['short'].lower()}")
    return {"median_coin_1h": bucket, "median_coin_1h_pct": pct,
            "btc_1h": page_row.get("regime_btc_1h"), "day_tag": page_row.get("regime_day_tag"),
            "words": " · ".join(words) or None}


def system_line(status, page_status, as_of):
    """The status line: TRADING, PAUSED (market drop, entry pace, a CPI/FOMC window ...),
    SOFT_LIMIT (no new entries today), HALTED (with when entries resume, when known) or STOPPED
    (no runtime heartbeat), in that order of precedence: STOPPED, HALTED, SOFT_LIMIT, PAUSED."""
    page = page_status or {}
    as_of = _aware(as_of)
    midnight = next_ny_midnight(as_of)

    def line(state, text, *, reason=None, since=None, resume_at=None):
        return {"state": state, "label": STATES[state], "reason": reason, "text": text,
                "since": since, "resume_at": resume_at,
                "rules_since": page.get("rules_since"),
                "risk_policy": page.get("risk_policy")}

    pill = status_pill(status)
    beat = status["last_heartbeat_at"]
    if pill == "STOPPED":
        return line("STOPPED", "No heartbeat from the trader since "
                    + (et_time(beat) if beat else "it started") + ".", since=beat)
    if page.get("daily_halt_at") is not None or status["daily_loss_halt_today"]:
        at = page.get("daily_halt_at")
        when = f" at {et_time(at)}" if at else ""
        return line("HALTED", f"Daily loss limit reached{when}. Open trades were sold; new "
                    "entries resume at 00:00 ET.", reason="DAILY_LOSS_LIMIT", since=at,
                    resume_at=midnight)
    if int(status["active_halts"] or 0) > 0:
        reason, at = page.get("execution_halt_reason"), page.get("execution_halt_at")
        when = f" since {et_time(at)}" if at else ""
        if reason == "OPERATOR_PAUSE":
            return line("PAUSED", f"New entries paused by the operator{when}.",
                        reason=reason, since=at)
        return line("HALTED", f"Trading halted{when}; it resumes after a review.",
                    reason=reason, since=at)
    if page.get("soft_limit_at") is not None:
        soft = page.get("soft_loss_pct")
        limit = f"−{_pct(_dec(soft))}% " if soft is not None else ""
        kept = (" It was kept after the day's start was corrected."
                if page.get("soft_limit_kept") else "")
        return line("SOFT_LIMIT", f"Daily loss reached the {limit}soft limit at "
                    f"{et_time(page['soft_limit_at'])}: no new entries today; open trades keep "
                    f"their stops. New entries resume at 00:00 ET.{kept}",
                    reason="DAILY_SOFT_LOSS_LIMIT", since=page["soft_limit_at"],
                    resume_at=midnight)
    wait_at = page.get("pacing_wait_at")
    if wait_at is not None and (as_of - _aware(wait_at)).total_seconds() <= PACING_RECENT_SECONDS:
        reason = page.get("pacing_wait_reason")
        resume = None
        if reason == "MARKET_DROP":
            move = _dec(page.get("pacing_median_1h_return"))
            text = "Market drop: new entries wait while the median coin is down 2% in an hour"
            if move is not None:
                move = _pct(move)
                text += f" (now {'−' if move < 0 else '+'}{abs(move)}%)"
        elif reason == "ENTRY_RATE_LIMIT":
            text = "Entry pace: at most 2 new entries per 30 minutes"
        elif reason == "MACRO_EVENT_WINDOW":
            kind = page.get("pacing_release_kind") or "US data"
            resume = page.get("pacing_release_until")
            text = f"{kind} release window: no new entries" + (
                f" until {et_time(resume)}" if resume else "")
        elif reason == "MARKET_BREADTH_UNAVAILABLE":
            text = "Market breadth unavailable: new entries wait until it can be measured"
        elif reason == "MACRO_CALENDAR_EXPIRED":
            text = "Release calendar expired: new entries wait until it is extended"
        else:
            text = "New entries are waiting"
        return line("PAUSED", text + ".", reason=reason, since=wait_at, resume_at=resume)
    withdrawn = page.get("soft_limit_withdrawn_latch_at")
    if withdrawn is not None:  # DAILY_SOFT_LOSS_LIMIT_WITHDRAWN (RISK_SESSION_BASELINE_V2).
        return line("TRADING", f"New entries allowed. The soft limit of {et_time(withdrawn)} was "
                    "withdrawn after the day's start was corrected.", reason="SOFT_LIMIT_WITHDRAWN",
                    since=page.get("soft_limit_withdrawn_at"))
    return line("TRADING", "New entries allowed.")


def day_limits(page_status, live, closed_today):
    """Today's P&L against the active policy's soft and hard daily limits, on a bar that spans
    7/6 of the hard limit; None without the day's starting equity or a policy.

    With RISK_SESSION_BASELINE_V2's basis (what JEV_MANAGED_RISK_V4's halt measures the day
    from) and an account equity recorded after it, today's P&L is that equity minus the basis
    (``measure`` ``ACCOUNT_EQUITY``, as of ``equity_at``; the halt engine also subtracts the
    day's deposits and withdrawals, which the public views do not hold). Otherwise it is the sum
    of the closed-today and open trades' P&L (``TRADES``)."""
    page = page_status or {}
    base = _dec(page.get("day_start_equity"))
    hard, soft = _dec(page.get("hard_loss_pct")), _dec(page.get("soft_loss_pct"))
    realized = [t["pnl_usd"] for t in closed_today if t["pnl_usd"] is not None]
    unrealized = [t["pnl_usd"] for t in live if t["pnl_usd"] is not None]
    realized_sum, open_sum = sum(realized, D(0)), sum(unrealized, D(0))
    total, measure = realized_sum + open_sum, "TRADES"
    equity, equity_at = _dec(page.get("equity_usd")), page.get("equity_at")
    base_at = page.get("day_start_observed_at")
    if (page.get("day_start_basis") == "ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2" and base is not None
            and equity is not None and equity > 0 and equity_at is not None
            and base_at is not None and _aware(equity_at) >= _aware(base_at)):
        total, measure = equity - base, "ACCOUNT_EQUITY"
    out = {
        "measure": measure, "account_equity_usd": equity if measure == "ACCOUNT_EQUITY" else None,
        "equity_at": equity_at if measure == "ACCOUNT_EQUITY" else None,
        "day_start_basis": page.get("day_start_basis"),
        "baseline_corrected_at": page.get("baseline_corrected_at"),
        "baseline_corrected_from_usd": _dec(page.get("baseline_corrected_from")),
        "policy": page.get("risk_policy"), "day_start_equity_usd": base,
        "day_start_at": page.get("day_start_observed_at"), "pnl_usd": total,
        "realized_usd": realized_sum, "open_usd": open_sum,
        "open_unpriced": sum(1 for t in live if t["pnl_usd"] is None),
        "fees_pending": sum(1 for t in closed_today if t["fees_pending"]),
        "soft_pct": _pct(soft), "hard_pct": _pct(hard),
        "pnl_pct": None, "loss_fraction": None, "soft_at": None, "hard_at": None,
        "soft_limit_usd": None, "hard_limit_usd": None,
        "halt_pnl_usd": _dec(page.get("daily_halt_pnl")), "halt_at": page.get("daily_halt_at"),
        "soft_limit_at": page.get("soft_limit_at"),
    }
    if base is None or base <= 0 or hard is None:
        return out
    scale = hard * LIMIT_SCALE
    loss = max(D(0), -total) / base
    out.update({
        "pnl_pct": _pct(total / base), "loss_fraction": _frac(min(loss / scale, D(1))),
        "hard_at": _frac(hard / scale), "hard_limit_usd": -hard * base,
        "soft_at": _frac(soft / scale) if soft is not None else None,
        "soft_limit_usd": -soft * base if soft is not None else None,
    })
    return out


def open_risk(page_status, live, equity):
    """The open trades' planned risk (the reservation's: quantity x (max entry - the planned
    stop)) against the active policy's crypto cap, as fractions of the day's starting equity
    (else the last recorded equity)."""
    page = page_status or {}
    base = _dec(page.get("day_start_equity")) or equity
    cap = _dec(page.get("crypto_cap_pct"))
    risks = [_dec(t.get("risk_usd")) for t in live]
    known = [r for r in risks if r is not None]
    total = sum(known, D(0))
    out = {"risk_usd": total, "trades": len(live), "unknown": len(risks) - len(known),
           "cap_pct": _pct(cap), "risk_pct": None, "fraction": None,
           "risk_per_trade_pct": _pct(_dec(page.get("risk_pct")))}
    if base is not None and base > 0:
        out["risk_pct"] = _pct(total / base)
        if cap:
            out["fraction"] = _frac(min(total / base / cap, D(1)))
    return out


def result_cell(trades):
    """Closed trades' figures: count, wins, P&L (net where verified, else gross), total R, and
    the net-R average over fee-verified trades only (None under ``MIN_SAMPLE`` of them)."""
    figures = totals(trades)
    verified = [t["pnl_r"] for t in trades if not t["fees_pending"] and t["pnl_r"] is not None]
    days = sorted({_ny_day(t["exit_at"]) for t in trades if t["exit_at"] is not None})
    return {
        "trades": figures["closed"], "wins": figures["wins"], "win_rate": figures["win_rate"],
        "pnl_usd": figures["pnl_usd"], "pnl_r": figures["pnl_r"],
        "fees_pending": figures["fees_pending"], "verified": len(verified),
        "avg_net_r": (sum(verified, D(0)) / len(verified)).quantize(R_PLACES)
        if len(verified) >= MIN_SAMPLE else None,
        "days": len(days), "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
    }


def results(closed, closed_today, regimes, page_status, keys):
    """Since start, today, by the exit day's market (sell-off days and other recorded days),
    and earlier rules against the current ones (the newest setup's account-wide versions)."""
    selloff = {r["day"]: (regime_words(r["tag"]) or {}).get("selloff") for r in regimes}
    by_day = {"selloff": [], "other": [], "untagged": []}
    for t in closed:
        flag = selloff.get(_ny_day(t["exit_at"]).isoformat()) if t["exit_at"] else None
        by_day["selloff" if flag is True else "other" if flag is False else "untagged"].append(t)
    current = rule_key({f: (page_status or {}).get("rules_" + f) for f in RULE_FIELDS})
    known = page_status is not None and any(current)
    now_rules = [t for t in closed if known and keys.get(t["trade_no"]) == current]
    earlier = [t for t in closed if not (known and keys.get(t["trade_no"]) == current)]
    return {
        "minimum_sample": MIN_SAMPLE, "since_start": result_cell(closed),
        "today": result_cell(closed_today), "selloff_days": result_cell(by_day["selloff"]),
        "other_days": result_cell(by_day["other"]), "untagged": len(by_day["untagged"]),
        "rules": {
            "earlier": result_cell(earlier),
            "current": {**result_cell(now_rules),
                        "since": (page_status or {}).get("rules_since") if known else None,
                        "versions": dict(zip(RULE_FIELDS, current, strict=True))
                        if known else None},
        },
    }


# --- Status and the whole document ---------------------------------------------------------


def status_pill(status):
    as_of, beat = _aware(status["as_of"]), _aware(status["last_heartbeat_at"])
    if beat is None or (as_of - beat).total_seconds() > RUNNING_HEARTBEAT_SECONDS:
        return "STOPPED"
    if status["daily_loss_halt_today"] or int(status["active_halts"] or 0) > 0:
        return "HALTED"
    return "RUNNING"


def account_equity(status):
    """The paper account's last recorded equity and its time, or (None, None): protective
    decisions (stop changes, cancels, exits) record 0, which is not the account's equity."""
    equity = _dec(status["equity_usd"])
    if equity is None or equity <= 0:
        return None, None
    return equity, status["equity_at"]


EXCLUDED_LISTS = ("trades", "recent", "trade_decisions", "day_decisions", "page_trades",
                  "page_waits", "page_reviews", "cycle_picks", "latest_picks")


def without_excluded(snapshot):
    """PAGE_EXCLUSION_V2 (owner, 2026-10-03: "Just take it off from list from everywhere, do
    not consider it"): the snapshot without the STATS_EXCLUSION_V1 trades — no row of theirs
    reaches any list, figure or chart. Returns ``(snapshot, excluded_closed_rows,
    exclusion_rows)``; the ledger is untouched."""
    excluded = {r["trade_no"] for r in snapshot.get("exclusions", [])}
    if not excluded:
        return snapshot, [], []
    out = dict(snapshot)
    for name in EXCLUDED_LISTS:
        if name in snapshot:
            out[name] = [r for r in snapshot[name] if r.get("trade_no") not in excluded]
    gone = [r for r in snapshot["trades"] if r["trade_no"] in excluded and r["closed"]]
    pages = {r["trade_no"]: r for r in snapshot.get("page_trades", [])}
    return out, [closed_trade(traded_row(r, pages.get(r["trade_no"]))) for r in gone], \
        snapshot.get("exclusions", [])


def build_dashboard(snapshot, *, title=None, fixture_data=False, schedule=None):
    """The whole dashboard as a JSON-safe dict (Decimals become strings)."""
    snapshot, excluded_closed, exclusion_rows = without_excluded(snapshot)
    status = snapshot["status"]
    as_of = _aware(status["as_of"])
    rows, runs, recent = snapshot["trades"], snapshot["runs"], snapshot["recent"]
    window_full = len(recent) >= RECENT_LIMIT
    # Each shown trade's decisions (read_snapshot), else the latest ones read for the feed.
    reviews_v5 = snapshot.get("page_reviews", [])
    recent = with_v5(recent, reviews_v5)
    stories = by_trade(with_v5(snapshot.get("trade_decisions", snapshot["recent"]),
                               reviews_v5))
    # Page V2 (migration 028): the levels actually traded, versions, waits and regimes.
    page_status = snapshot.get("page_status")
    page_rows = {r["trade_no"]: r for r in snapshot.get("page_trades", [])}
    waits = defaultdict(list)
    for w in snapshot.get("page_waits", []):
        waits[w["trade_no"]].append(w)
    regimes = snapshot.get("regimes", [])
    current_rules = rule_key({f: (page_status or {}).get("rules_" + f) for f in RULE_FIELDS})
    keys = {}
    closed, live, pairs = [], [], []
    for row in rows:
        page_row = page_rows.get(row["trade_no"])
        traded = traded_row(row, page_row)
        trade = closed_trade(traded) if row["closed"] else open_trade(traded, as_of)
        plan = trade_plan(row, page_row)
        decisions = stories.get(row["trade_no"], [])
        trade.update(trade_story(traded, trade, decisions, plan=plan,
                                 waits=waits.get(row["trade_no"], [])))
        keys[row["trade_no"]] = rule_key(page_row) if page_row else None
        trade.update({
            "research_stop": _plain(row["planned_stop"]),
            "research_target": _plain(row["planned_target"]),
            "risk_usd": _dec(traded["planned_risk_usd"]), "plan": plan,
            "versions": {f: page_row.get(f) for f in (*RULE_FIELDS, "maintenance_policy",
                                                      "holding_policy")} if page_row else None,
            "rules": None if not page_row or not any(current_rules) else (
                "CURRENT" if keys[row["trade_no"]] == current_rules else "EARLIER"),
            "market_at_entry": entry_market(page_row),
            "agent": next((d["agent"] for d in decisions if d["kind"] == "PICK"), None),
            # No after-exit price is recorded by any job yet (plan section 11, item 4 waits
            # for the learning loop's after-exit path): the page omits the block.
            "after_exit": None,
        })
        (closed if row["closed"] else live).append(trade)
        pairs.append((row, trade))
    by_number = {row["trade_no"]: (trade, row["closed"]) for row, trade in pairs}
    # The current cycle's picks and Jev's scoped logs (read_snapshot; else what was read).
    schedule = schedule or DEFAULT_SCHEDULE
    cycle = cycle_runs(runs, schedule)
    cycle_rows = snapshot.get("cycle_picks", snapshot["latest_picks"])
    day_rows = with_v5(snapshot.get("day_decisions", recent), reviews_v5)
    today = status["ny_today"]
    closed_today = [t for t in closed if _ny_day(t["exit_at"]) == today]
    opened_today = [r for r in rows if _ny_day(r["entry_at"]) == today]
    runs_today = [r for r in runs if _ny_day(r["run_at"]) == today]
    open_pnl = [t["pnl_usd"] for t in live if t["pnl_usd"] is not None]
    open_cost = [t["entry_value_usd"] for t in live if t["entry_value_usd"] is not None]
    equity, equity_at = account_equity(status)
    # EXPERIMENT_DASHBOARD_V3: positions and the account. PAGE_EXCLUSION_V2: the excluded
    # trades are already gone (without_excluded); only a summary with counts remains.
    summary = v3.exclusion_summary(exclusion_rows)
    exclusions = {"trade_nos": [], "trades": summary["trades"], "days": summary["days"],
                  "text": summary["text"]}
    excluded_nos = set()
    for trade in closed:
        trade["excluded"] = False
    for trade in live:
        trade["position"] = v3.position(trade)
        trade["jev_check"] = v3.jev_check(stories.get(trade["trade_no"], []))
    included = [t for t in closed if t["trade_no"] not in excluded_nos]
    open_risk_block = open_risk(page_status, live, equity)
    newest, newest_at, _ = v3.latest_equity(snapshot, page_status)
    newest_status = page_status
    if page_status is not None and newest is not None:
        newest_status = {**page_status, "equity_usd": newest, "equity_at": newest_at}
    excluded_days = {d["day"] for d in summary["days"]}
    equity_block = v3.pnl_section(snapshot, closed, closed + excluded_closed, live,
                                  excluded_days, page_status, as_of)
    worst = equity_block["max_drawdown"] or {}
    performance = v3.performance_section(
        closed, excluded_nos, keys, current_rules, (page_status or {}).get("rules_since"),
        exclusions, worst.get("pct"),
        {r["day"]: (regime_words(r["tag"]) or {}).get("selloff") for r in regimes})
    days = past_days(closed, regimes)
    for day in days:
        day["excluded"] = day["day"] in excluded_days
    document = {
        "dashboard_version": DASHBOARD_VERSION,
        "title": (title or "").strip() or DEFAULT_TITLE,
        "fixture_data": bool(fixture_data),
        "data_label": FIXTURE_BANNER if fixture_data else None,
        "as_of": as_of,
        "status": {
            "pill": status_pill(status), "last_heartbeat_at": status["last_heartbeat_at"],
            "active_halts": _int(status["active_halts"]),
            "daily_loss_halt_today": status["daily_loss_halt_today"],
            "started_at": status["experiment_started_at"],
        },
        "overall": {
            **totals(closed), "open": len(live),
            "open_pnl_usd": sum(open_pnl, D(0)) if open_pnl else None,
            "open_entry_value_usd": sum(open_cost, D(0)) if open_cost else None,
            "account_label": ACCOUNT_LABEL, "equity_usd": equity, "equity_at": equity_at,
        },
        "today": {
            "day": today.isoformat() if today else None,
            **{k: v for k, v in totals(closed_today).items() if k != "closed"},
            "opened": len(opened_today), "closed": len(closed_today),
            "picks": sum(int(r["picks"] or 0) for r in runs_today),
            "selected": sum(int(r["selected"] or 0) for r in runs_today),
        },
        "live_trades": live,
        "agents": {
            "research": research_agents(runs, snapshot["latest_picks"], recent, schedule,
                                        as_of, window_full=window_full),
            "schedule": schedule_summary(schedule, as_of),
            "cycle": research_cycle(cycle, cycle_rows, schedule, as_of, by_number),
            "pending_run": pending_run(runs, cycle),
            "today_runs": todays_runs(runs, today),
            "latest_run": run_summary(max(runs, key=lambda r: r["run_no"])) if runs else None,
            "runs_total": len(runs),
            "jev": jev_card(status, runs, recent, window_full=window_full, day_rows=day_rows,
                            day_full=len(day_rows) >= DAY_LIMIT, cycle=cycle,
                            cycle_rows=cycle_rows, pairs=pairs),
        },
        # PAGE_EXCLUSION_V2: no line about a research run of an excluded day either.
        "feed": feed(recent, [r for r in runs if _ny_day(r["run_at"]) is None
                              or _ny_day(r["run_at"]).isoformat() not in excluded_days],
                     window_full=window_full),
        "past": {"days": days, "closed_trades": closed_history(closed)},
        "system": system_line(status, page_status, as_of),
        # V3: today's P&L against the limits from the newest account read (a five-minute
        # snapshot when newer than the last risk decision's), so it agrees with the hero.
        "limits": day_limits(newest_status, live, closed_today),
        "open_risk": open_risk_block,
        "market": market_block(regimes, page_status, today),
        # V3: the statistics leave out STATS_EXCLUSION_V1 trades (owner ruling 2026-10-03).
        "results": {**results(included, [t for t in closed_today
                                         if t["trade_no"] not in excluded_nos],
                              regimes, page_status, keys),
                    "excluded": {k: exclusions[k] for k in ("trades", "days", "text")}},
        "account": trading_account(v3.account_section(
            snapshot, page_status, live, closed, closed_today, open_risk_block,
            {**equity_block, "start_equity_usd": equity_block["capital_usd"]}, performance,
            as_of), equity_block, summary),
        "exclusions": summary,
        "equity": equity_block,
        "daily": v3.daily_section(closed, [regime_day(r) for r in regimes if r["tag"]],
                                  excluded_days, today),
        "performance": performance,
    }
    return json_safe(_rounded(document))


def trading_account(account, curve, summary):
    """The hero stays the real broker equity and Today stays real; "since start" becomes the
    trading P&L of the included trades (realized plus open), labelled with the exclusion."""
    account.update({
        "since_start_usd": curve["trading_pnl_usd"], "since_start_pct": curve["trading_pnl_pct"],
        "trading_pnl_usd": curve["trading_pnl_usd"], "trading_pnl_pct": curve["trading_pnl_pct"],
        "trading_pnl_label": "Trading P&L" + (
            " excl. " + summary["text"].removeprefix("Excludes ").split(" (")[0]
            if summary["text"] else ""),
        "start_equity_usd": curve["capital_usd"],
    })
    return account


def _rounded(value):
    """Round every USD figure to cents and every R to 0.001 for display."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, D) and key.endswith("_usd"):
                out[key] = item.quantize(USD_PLACES)
            elif isinstance(item, D) and key.endswith("_r"):
                out[key] = item.quantize(R_PLACES)
            else:
                out[key] = _rounded(item)
        return out
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


__all__ = [
    "ACCOUNT_LABEL", "ARM_TAGS", "DEFAULT_SCHEDULE", "DEFAULT_TITLE", "FIXTURE_BANNER",
    "REPEATING", "RUNNING_HEARTBEAT_SECONDS", "account_equity", "build_dashboard", "by_trade",
    "closed_history", "closed_trade", "cycle_runs", "decision_text", "failed_reviews_today",
    "feed", "fold_lines", "jev_action", "jev_scopes", "level_change", "level_move",
    "level_steps", "next_run", "ny_midnight", "number_text", "open_trade", "past_days",
    "pending_run", "pick_status", "read_snapshot", "repeated_text", "repeated_words",
    "research_cycle", "run_decisions", "run_summary", "schedule_summary", "scope_start",
    "shown_trades", "status_pill", "todays_runs", "totals", "trade_lines", "trade_story",
    "validity_limit", "day_limits", "entry_market", "market_block", "open_risk", "plan_event",
    "regime_words", "result_cell", "results", "system_line", "trade_plan", "traded_row",
    "wait_event", "et_time", "rule_key",
]
