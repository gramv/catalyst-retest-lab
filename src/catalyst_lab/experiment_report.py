"""The public live dashboard's data: one JSON-safe document built from the four
``lab.public_dashboard_*`` views of migration 024 (role ``catalyst_public``).

Paper only: nothing here can place, change or cancel an order, and nothing reads a broker or
Jev/TypeSafe credential or calls an exchange. Display definitions (docs/packages/
experiment-page.md):

* **P&L of a closed trade** is net of fees when every fee is known (verified, reconciled, never
  ``LAB_FIXTURE`` evidence); otherwise it is the gross fill P&L, flagged ``fees_pending``.
  Nothing is estimated.
* **P&L of an open trade** is the held quantity times (the freshest price the ledger holds, the
  bid, minus the average entry). No price in the ledger: unknown.
* **R** divides P&L by the planned risk, filled quantity x (max entry - initial stop).
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
  Jev has not ranked yet is shown as awaiting Jev.
* **Jev's scoped logs**: "This cycle" is Jev's decisions since the current cycle's selection
  (its per-pick verdicts, then reviews, level changes, 24-hour reviews, and the trades' entries
  and exits); "Today" is the New York day's (each run's selection as one line). Newest first,
  a trade's repeated reviews folded; at most ``SCOPE_LENGTH`` lines, from at most ``DAY_LIMIT``
  decisions read.
"""

from collections import Counter, defaultdict
from datetime import UTC, datetime, time
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from catalyst_lab.repository import json_safe

DASHBOARD_VERSION = "EXPERIMENT_DASHBOARD_V1"
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
    "BREAKEVEN": "breakeven", "SWING_LOW_15M": "15-minute swing low",
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
    "latest_picks": """SELECT d.* FROM lab.public_dashboard_decisions d
        JOIN (SELECT agent, max(run_no) AS run_no FROM lab.public_dashboard_runs GROUP BY agent)
        l ON l.run_no=d.run_no WHERE d.kind IN ('PICK','SELECTION')
        ORDER BY d.run_no, d.kind, d.jev_rank NULLS LAST, d.symbol""",
}


# Every listed decision of the trades the page shows (open, and the latest closed): their
# events and levels. At most 60 reviews per trade (the view), plus picks, answers and flags.
TRADE_DECISIONS = """SELECT * FROM lab.public_dashboard_decisions
    WHERE trade_no = ANY(%s) AND at IS NOT NULL ORDER BY trade_no, at, kind"""
# The current cycle's picks and Jev's verdicts (its runs may not be each agent's latest).
CYCLE_PICKS = """SELECT * FROM lab.public_dashboard_decisions
    WHERE kind IN ('PICK','SELECTION') AND run_no = ANY(%s)
    ORDER BY run_no, kind, jev_rank NULLS LAST, symbol"""
# Jev's scoped logs: every decision since the earlier of the New York day's start and the
# current cycle's selection, newest first and bounded.
DAY_DECISIONS = f"""SELECT * FROM lab.public_dashboard_decisions
    WHERE at >= %s AND kind NOT IN ('PICK','SELECTION')
    ORDER BY at DESC, trade_no, kind LIMIT {DAY_LIMIT}"""


def shown_trades(rows, limit=CLOSED_LIMIT):
    """The trade numbers the page shows: every open trade and the latest ``limit`` closed ones
    (the order of ``closed_history``)."""
    closed = sorted((r for r in rows if r["closed"]),
                    key=lambda r: (_aware(r["exit_at"]) or EPOCH, r["trade_no"]), reverse=True)
    return [r["trade_no"] for r in rows if not r["closed"]] + [
        r["trade_no"] for r in closed[:limit]]


def cycle_runs(runs):
    """The current cycle: the runs of the newest run slot Jev has ranked (oldest run first)."""
    ranked = [r for r in runs if r["ranked_at"] is not None]
    if not ranked:
        return []
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


def read_snapshot(conn):
    """Every public view, read in the caller's transaction (the service opens it REPEATABLE
    READ, READ ONLY, so all the figures describe one instant)."""
    snapshot = {"status": conn.execute("SELECT * FROM lab.public_dashboard_status").fetchone()}
    for name, sql in SNAPSHOT_QUERIES.items():
        snapshot[name] = conn.execute(sql).fetchall()
    numbers = shown_trades(snapshot["trades"])
    snapshot["trade_decisions"] = (
        conn.execute(TRADE_DECISIONS, (numbers,)).fetchall() if numbers else [])
    cycle = cycle_runs(snapshot["runs"])
    snapshot["cycle_picks"] = (conn.execute(
        CYCLE_PICKS, ([r["run_no"] for r in cycle],)).fetchall() if cycle else [])
    snapshot["day_decisions"] = conn.execute(
        DAY_DECISIONS, (scope_start(snapshot["status"], cycle),)).fetchall()
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
    if moves_stop and basis == "BREAKEVEN":
        text = f"Raised stop to breakeven ({stop_text})"
    elif moves_stop:
        text = f"Raised stop to {stop_text}"
    elif action == "RAISE_TARGET":
        text = f"Raised target to {target_text}"
    else:
        return "Changed the levels"
    if action == "RAISE_STOP_AND_TARGET":
        text += f" and target to {target_text}"
    if basis in BASES and basis != "BREAKEVEN":
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


def past_days(closed):
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
            "cumulative_pnl_usd": cumulative})
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
    if kind == "MAINTENANCE":
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


def _fold(events, window_start=None):
    """A trade's consecutive reviews with one unchanged outcome become one line. With its
    reviews cut at 60 (``window_start``: the oldest listed one), the streak that starts there
    may continue earlier ("+")."""
    out = []
    for event in events:
        last = out[-1] if out else None
        if (event["kind"] == "REVIEW" and event["outcome"] in REPEATING and last is not None
                and last["kind"] == "REVIEW" and last["outcome"] == event["outcome"]):
            last["repeats"] += 1
            last["at"], last["confidence"] = event["at"], event["confidence"]
            continue
        out.append(event)
    for event in out:
        if event["kind"] != "REVIEW" or event["repeats"] < 2:
            continue
        event["repeats_more"] = (window_start is not None and event["outcome"] in REPEATING
                                 and _aware(event["since"]) == _aware(window_start))
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


def trade_story(row, trade, decisions):
    """A trade's ``events`` (oldest first) and ``levels`` (display definitions)."""
    reviews = [d for d in decisions if d["kind"] == "MAINTENANCE"]
    cut = len(reviews) >= PER_TRADE_LIMIT
    window_start = reviews[0]["at"] if reviews else None
    steps, entry_known = level_steps(row, decisions, cut, window_start)
    events = [_decision_event(d, None) for d in decisions if d["kind"] in {"PICK", "SELECTION"}]
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
            "text": decision_text(row), "note": note, "confidence": row["confidence"]}


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
        if repeating and current is not None and current["outcome"] == item["outcome"]:
            current["repeats"] += 1
            current["since"] = item["at"]
            continue
        line = None
        if len(out) < length:  # A full list skips older lines but still grows open streaks.
            line = {**item, "repeats": 1, "since": item["at"]}
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
        if item["repeats"] > 1:
            item["text"] = repeated_text(item)
    return out


# --- Agents ------------------------------------------------------------------------------------


def _plan(schedule):
    from catalyst_lab.research_schedule import ResearchSchedule

    return ResearchSchedule(**schedule) if isinstance(schedule, dict) else schedule


def next_run(schedule, now):
    """The next scheduled research run (``RESEARCH_SCHEDULE_V1``), or None when unreadable."""
    try:
        return _plan(schedule).next_after(now)
    except Exception:  # noqa: BLE001 -- an unreadable schedule shows no next run.
        return None


def schedule_summary(schedule, now):
    """``Every 2 hours``, ``Daily at 08:00`` or ``3 runs a day``, with the run times, their time
    zone and the next run; None when the schedule is unreadable."""
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
    return {"text": text, "timezone": plan.timezone, "times": list(plan.runs),
            "next_run_at": next_run(plan, now)}


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
    slot = _aware(cycle[0]["run_at"]) if cycle else None
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
               "EXPIRED": "Expired", "NOT_SELECTED": "Not selected", "NO_VERDICT": "No verdict"}


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
    listed = []
    for p in picks:
        verdict = verdicts.get((p["run_no"], p["symbol"])) or {}
        trade, closed = trades.get(p["trade_no"], (None, False))
        code, words = pick_status(verdict.get("outcome"), trade, closed, valid_until, as_of)
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


def build_dashboard(snapshot, *, title=None, fixture_data=False, schedule=None):
    """The whole dashboard as a JSON-safe dict (Decimals become strings)."""
    status = snapshot["status"]
    as_of = _aware(status["as_of"])
    rows, runs, recent = snapshot["trades"], snapshot["runs"], snapshot["recent"]
    window_full = len(recent) >= RECENT_LIMIT
    # Each shown trade's decisions (read_snapshot), else the latest ones read for the feed.
    stories = by_trade(snapshot.get("trade_decisions", recent))
    closed, live, pairs = [], [], []
    for row in rows:
        trade = closed_trade(row) if row["closed"] else open_trade(row, as_of)
        trade.update(trade_story(row, trade, stories.get(row["trade_no"], [])))
        (closed if row["closed"] else live).append(trade)
        pairs.append((row, trade))
    by_number = {row["trade_no"]: (trade, row["closed"]) for row, trade in pairs}
    # The current cycle's picks and Jev's scoped logs (read_snapshot; else what was read).
    cycle = cycle_runs(runs)
    cycle_rows = snapshot.get("cycle_picks", snapshot["latest_picks"])
    day_rows = snapshot.get("day_decisions", recent)
    schedule = schedule or DEFAULT_SCHEDULE
    today = status["ny_today"]
    closed_today = [t for t in closed if _ny_day(t["exit_at"]) == today]
    opened_today = [r for r in rows if _ny_day(r["entry_at"]) == today]
    runs_today = [r for r in runs if _ny_day(r["run_at"]) == today]
    open_pnl = [t["pnl_usd"] for t in live if t["pnl_usd"] is not None]
    open_cost = [t["entry_value_usd"] for t in live if t["entry_value_usd"] is not None]
    equity, equity_at = account_equity(status)
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
        "feed": feed(recent, runs, window_full=window_full),
        "past": {"days": past_days(closed), "closed_trades": closed_history(closed)},
    }
    return json_safe(_rounded(document))


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
    "validity_limit",
]
