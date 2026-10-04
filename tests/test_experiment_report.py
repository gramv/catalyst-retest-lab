"""EXPERIMENT_DASHBOARD_V1 and V2: the dashboard's arithmetic and wording, without a database."""

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from catalyst_lab.experiment_report import (
    ACCOUNT_LABEL,
    DASHBOARD_VERSION,
    DEFAULT_TITLE,
    FIXTURE_BANNER,
    JEV_LOG_LENGTH,
    PER_TRADE_LIMIT,
    RECENT_LIMIT,
    SCOPE_LENGTH,
    build_dashboard,
    by_trade,
    closed_history,
    closed_trade,
    failed_reviews_today,
    feed,
    jev_action,
    latest_runs,
    level_change,
    level_move,
    next_run,
    number_text,
    ny_midnight,
    open_trade,
    r_text,
    run_decisions,
    schedule_summary,
    scope_start,
    shown_trades,
    status_pill,
    totals,
    usd_text,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 16, tzinfo=UTC)  # 12:00 in New York


def status(**overrides):
    return {"as_of": NOW, "ny_today": date(2026, 10, 1), "experiment_started_at": None,
            "last_heartbeat_at": None, "heartbeat_management_reviews": None, "jev_breaker": None,
            "active_halts": 0, "daily_loss_halt_today": False, "equity_usd": None,
            "equity_at": None, "jev_last_call_at": None, "jev_last_call_outcome": None,
            "jev_calls_today": 0, "schema_version": 24, **overrides}


def trade_row(trade_no, *, closed=True, arm="JEV_MANAGED", symbol="BTC/USD",
              entry_at=NOW - timedelta(hours=5), exit_at=NOW - timedelta(hours=1), gross="11",
              net="10", fees="1", known=True, reconciled=True, fixture=False, risk="20",
              price=None, price_at=None, qty="10", entry="100.10", **jev):
    return {
        "trade_no": trade_no, "run_no": 1, "symbol": symbol, "arm": arm,
        "status": "CLOSED" if closed else "OPEN", "closed": closed, "entry_at": entry_at,
        "exit_at": exit_at if closed else None, "planned_entry": D("100"),
        "max_entry": D("100.10"), "planned_stop": D("98"), "planned_target": D("105"),
        "current_stop": D("98"), "current_target": D("105"), "avg_entry_price": D(entry),
        "avg_exit_price": D("101") if closed else None, "bought_qty": D(qty),
        "sold_qty": D("9.975") if closed else D(0), "open_qty": None if closed else D(qty),
        "exit_reason": "TARGET_EXIT" if closed else None, "exit_in_progress": False,
        "next_review_at": None, "continuations": D(0),
        "gross_pnl_usd": D(gross) if closed and gross is not None else None,
        "fees_known": known, "fees_usd": D(fees) if known else None,
        "inventory_reconciled": reconciled,
        "net_pnl_usd": D(net) if closed and known and reconciled and net is not None else None,
        "planned_risk_usd": D(risk), "fixture_fee_evidence": fixture,
        "price": D(price) if price is not None else None, "price_at": price_at,
        "jev_last_kind": None, "jev_last_outcome": None, "jev_last_action": None,
        "jev_last_at": None, "jev_last_confidence": None, "jev_last_stop": None,
        "jev_last_target": None, "jev_last_basis": None, "jev_change_action": None,
        "jev_change_at": None, "jev_change_stop": None, "jev_change_target": None,
        "jev_change_basis": None, **jev,
    }


def decision_row(kind, at, *, actor="JEV", outcome=None, action=None, symbol="BTC/USD",
                 trade_no=1, confidence=None, stop=None, target=None, basis=None, note=None,
                 agent=None, run_no=None, jev_rank=None, entry=None):
    return {"at": at, "actor": actor, "agent": agent, "kind": kind, "outcome": outcome,
            "action": action, "symbol": symbol, "trade_no": trade_no, "run_no": run_no,
            "jev_rank": jev_rank, "confidence": confidence, "entry": entry, "stop": stop,
            "target": target, "basis": basis, "note": note}


def hold(trade_no, minutes, symbol="BTC/USD"):
    return decision_row("MAINTENANCE", NOW - timedelta(minutes=minutes), outcome="HELD",
                        action="HOLD", trade_no=trade_no, symbol=symbol, confidence=D("0.74"))


def failed(trade_no, minutes, symbol="UNI/USD"):
    return decision_row("MAINTENANCE", NOW - timedelta(minutes=minutes), outcome="FAILED",
                        trade_no=trade_no, symbol=symbol, note="REVIEW_UNAVAILABLE")


def run_row(run_no, *, agent="claude", day=0, picks=20, ranked=16, vetoed=2, not_ranked=2,
            selected=10, symbols="DOT/USD,UNI/USD,AAVE/USD,ARB/USD,RENDER/USD,FIL/USD,BCH/USD,"
            "PEPE/USD,SHIB/USD,GRT/USD", traded=3):
    slot = datetime(2026, 10, 1, 12, tzinfo=UTC) - timedelta(days=day)
    return {"run_no": run_no, "agent": agent, "run_at": slot,
            "submitted_at": slot + timedelta(minutes=10), "ranked_at": slot + timedelta(minutes=25),
            "picks": picks, "ranked": ranked, "vetoed": vetoed, "not_ranked": not_ranked,
            "selected": selected, "selected_symbols": symbols, "traded": traded}


def snapshot(**overrides):
    return {"status": status(), "trades": [], "runs": [], "recent": [], "latest_picks": [],
            **overrides}


# --- Trades -------------------------------------------------------------------------------------


def test_closed_pnl_is_net_when_every_fee_is_verified_else_gross_marked_fees_pending():
    verified = closed_trade(trade_row(1))
    assert (verified["pnl_usd"], verified["pnl_r"], verified["fees_usd"],
            verified["fees_pending"]) == (D("10"), D("0.5"), D("1"), False)
    for pending in (trade_row(2, known=False), trade_row(3, fixture=True),
                    trade_row(4, reconciled=False)):
        result = closed_trade(pending)
        assert (result["pnl_usd"], result["fees_usd"], result["fees_pending"]) == (
            D("11"), None, True), pending["trade_no"]
    unknown = closed_trade(trade_row(5, gross=None, known=False))
    assert unknown["pnl_usd"] is None and unknown["pnl_r"] is None  # never estimated
    assert closed_trade(trade_row(6, exit_reason="BROKER_EXIT"))["exit_reason"] == "Stop hit"


def test_open_pnl_uses_the_price_the_ledger_holds_and_is_unknown_without_one():
    priced = open_trade(trade_row(1, closed=False, price="101.1", price_at=NOW), NOW)
    assert (priced["pnl_usd"], priced["pnl_r"]) == (D("10.00"), D("0.5"))  # 10 x (101.1-100.1)
    assert priced["minutes_in_trade"] == 300 and priced["tag"] == "Jev-managed"
    unpriced = open_trade(trade_row(2, closed=False, arm="FIXED_EXIT"), NOW)
    assert (unpriced["pnl_usd"], unpriced["pnl_r"], unpriced["price"]) == (None, None, None)
    assert unpriced["tag"] == "Fixed exit" and unpriced["jev_last"] is None


def test_every_trade_shows_how_much_was_bought_and_its_dollar_value():
    """An open trade: the quantity it holds, its value at entry and now; a closed trade: the
    quantity it bought and its value at entry (the live UNI trade of 2026-09-28)."""
    live = open_trade(trade_row(1, closed=False, symbol="UNI/USD", qty="112.68",
                                entry="8.78556", price="8.82711", price_at=NOW), NOW)
    assert (live["qty"], live["entry_value_usd"], live["value_usd"], live["pnl_usd"]) == (
        D("112.68"), D("989.9569008"), D("994.6387548"), D("4.6818540"))
    unpriced = open_trade(trade_row(2, closed=False, qty="3.50000000", entry="100"), NOW)
    assert (str(unpriced["qty"]), unpriced["entry_value_usd"], unpriced["value_usd"]) == (
        "3.5", D("350"), None)
    fallback = trade_row(3, closed=False)
    fallback["open_qty"], fallback["sold_qty"] = None, D("4")  # held = bought - sold
    assert open_trade(fallback, NOW)["qty"] == D("6")
    done = closed_trade(trade_row(4))  # bought 10 at 100.10, held from -5 h to -1 h
    assert (done["qty"], done["entry_value_usd"], done["minutes_in_trade"]) == (
        D("10"), D("1001.00"), 240)
    doc = build_dashboard(snapshot(trades=[
        trade_row(1, closed=False, symbol="UNI/USD", qty="112.68", entry="8.78556",
                  price="8.82711", price_at=NOW),
        trade_row(2, closed=False, qty="3.5", entry="100"), trade_row(4)]))
    first = doc["live_trades"][0]
    assert (first["qty"], first["entry_value_usd"], first["value_usd"], first["pnl_usd"]) == (
        "112.68", "989.96", "994.64", "4.68")
    assert doc["overall"]["open_entry_value_usd"] == "1339.96"  # 989.9569008 + 350
    [closed] = doc["past"]["closed_trades"]
    assert (closed["qty"], closed["entry_value_usd"], closed["exit"], closed["exit_reason"]) == (
        "10", "1001.00", "101", "Target reached")
    assert build_dashboard(snapshot())["overall"]["open_entry_value_usd"] is None


@pytest.mark.parametrize("equity, shown", [
    (None, None), (D("0"), None), (D("0E-10"), None), (D("-1"), None),
    (D("10084.2700"), "10084.27"),
])
def test_zero_or_missing_equity_is_unknown(equity, shown):
    """Protective decisions (stop changes, cancels, exits) record equity 0: never "$0.00"."""
    at = NOW - timedelta(minutes=3)
    overall = build_dashboard(snapshot(status=status(equity_usd=equity,
                                                     equity_at=at)))["overall"]
    assert overall["equity_usd"] == shown
    assert overall["equity_at"] == (at.isoformat() if shown else None)
    assert overall["account_label"] == ACCOUNT_LABEL


def test_prices_are_plain_decimals():
    row = open_trade(trade_row(1, closed=False, entry="150.1860000000", price="100.0000"), NOW)
    assert (str(row["entry"]), str(row["price"])) == ("150.186", "100")
    assert number_text(D("63161.1")) == "63,161.10"
    assert number_text(D("0.00000812")) == "0.00000812"
    assert number_text(D("4.050950")) == "4.051"
    assert number_text(None) == "?"


def test_totals_count_wins_and_losses_and_keep_unknowns_out_of_the_sums():
    trades = [closed_trade(trade_row(1, net="12")), closed_trade(trade_row(2, net="-4")),
              closed_trade(trade_row(3, net="0")),
              closed_trade(trade_row(4, gross=None, known=False))]
    figures = totals(trades)
    assert (figures["closed"], figures["wins"], figures["losses"], figures["pnl_usd"],
            figures["pnl_r"], figures["win_rate"], figures["pnl_unknown"],
            figures["fees_pending"]) == (4, 1, 2, D("8"), D("0.4"), D("0.3333"), 1, 1)
    assert totals([])["win_rate"] is None


def test_closed_trades_are_listed_latest_exit_first_with_the_running_total():
    """The running total is the realized P&L after each close, in exit order (the chart)."""
    rows = [trade_row(1, net="5", exit_at=NOW - timedelta(hours=1)),
            trade_row(2, net="-2", exit_at=NOW - timedelta(hours=3)),
            trade_row(3, gross=None, known=False, exit_at=NOW - timedelta(hours=2)),
            trade_row(4, net="7", exit_at=NOW - timedelta(minutes=5))]
    doc = build_dashboard(snapshot(trades=rows))
    listed = doc["past"]["closed_trades"]
    assert [(t["trade_no"], t["pnl_usd"], t["cumulative_pnl_usd"]) for t in listed] == [
        (4, "7.00", "10.00"), (1, "5.00", "3.00"), (3, None, "-2.00"), (2, "-2.00", "-2.00")]
    assert listed[0]["cumulative_pnl_usd"] == doc["overall"]["pnl_usd"]
    assert listed[0]["minutes_in_trade"] == 295  # entered 5 h before NOW, closed 5 min before
    many = [closed_trade(trade_row(n, net="1", exit_at=NOW - timedelta(minutes=100 - n)))
            for n in range(1, 61)]
    latest = closed_history(many)
    assert len(latest) == 50 and latest[0]["trade_no"] == 60 and latest[-1]["trade_no"] == 11
    assert (latest[0]["cumulative_pnl_usd"], latest[-1]["cumulative_pnl_usd"]) == (D(60), D(11))


def test_today_and_past_days_are_new_york_days():
    late = NOW.replace(hour=3)  # 23:00 the evening before in New York
    rows = [trade_row(1, net="5", exit_at=late, entry_at=late - timedelta(hours=2)),
            trade_row(2, net="-2", exit_at=NOW - timedelta(hours=1)),
            trade_row(3, net="7", known=False, exit_at=NOW - timedelta(minutes=5)),
            trade_row(4, closed=False, entry_at=NOW - timedelta(hours=2), price="100.2",
                      price_at=NOW)]
    doc = build_dashboard(snapshot(trades=rows, runs=[run_row(1), run_row(2, day=1)]))
    today = doc["today"]
    assert (today["day"], today["closed"], today["opened"], today["wins"], today["losses"],
            today["pnl_usd"], today["fees_pending"]) == ("2026-10-01", 2, 3, 1, 1, "9.00", 1)
    assert (today["picks"], today["selected"]) == (20, 10)
    assert [(d["day"], d["closed"], d["pnl_usd"], d["cumulative_pnl_usd"])
            for d in doc["past"]["days"]] == [("2026-09-30", 1, "5.00", "5.00"),
                                              ("2026-10-01", 2, "9.00", "14.00")]
    overall = doc["overall"]
    assert (overall["closed"], overall["open"], overall["pnl_usd"], overall["open_pnl_usd"],
            overall["account_label"]) == (3, 1, "14.00", "1.00", ACCOUNT_LABEL)


# --- A trade's events and levels -----------------------------------------------------------------


def test_a_trades_events_tell_its_story_oldest_first():
    """The pick and Jev's selection, the buy with its limit, the levels set at entry, Jev's
    reviews (holds and failures folded, a raise as old -> new with its basis and confidence),
    the agent's answer, the 24-hour review, then the exit in plain words and the fees and P&L."""
    entry_at = NOW - timedelta(hours=3)
    rows = [
        decision_row("PICK", entry_at - timedelta(hours=1), actor="RESEARCH_AGENT",
                     agent="claude", run_no=4, entry=D("100"), stop=D("98"), target=D("105"),
                     note="Range reclaim.", outcome="PROPOSED", action="CHART"),
        decision_row("SELECTION", entry_at - timedelta(minutes=45), outcome="SELECTED",
                     jev_rank=2, run_no=4, note="PRICES_CONSISTENT_NO"),
        hold(1, 170), hold(1, 169), hold(1, 168),
        decision_row("MAINTENANCE", NOW - timedelta(minutes=167), outcome="APPLIED",
                     action="RAISE_STOP", stop=D("100.1"), target=D("105"), basis="BREAKEVEN",
                     confidence=D("0.81")),
        failed(1, 166, "BTC/USD"), failed(1, 165, "BTC/USD"),
        decision_row("REVIEW_ANSWER", NOW - timedelta(minutes=40), actor="RESEARCH_AGENT",
                     agent="claude", outcome="CONTINUE", note="Trend intact."),
        decision_row("DAY_REVIEW", NOW - timedelta(minutes=20), outcome="CONTINUE",
                     note="AGREED", confidence=D("0.72")),
        hold(1, 10),
    ]
    doc = build_dashboard(snapshot(
        trades=[trade_row(1, entry_at=entry_at, exit_at=NOW - timedelta(minutes=5))],
        trade_decisions=rows))
    [trade] = doc["past"]["closed_trades"]
    events = trade["events"]
    assert [(e["kind"], e["text"]) for e in events] == [
        ("PICK", "Picked by claude (run 4): entry 100, stop 98, target 105"),
        ("SELECTION", "Selected by Jev (rank 2)"),
        ("BUY", "Bought 10 BTC at 100.1, limit 100.1"),
        ("LEVELS_SET", "Stop set at 98, target 105"),
        ("REVIEW", "Held (3 reviews in a row)"),
        ("REVIEW", "Raised stop 98 → 100.1 (breakeven)"),
        ("REVIEW", "Review failed (2 in a row)"),
        ("REVIEW_ANSWER", "claude answered the 24-hour review: continue"),
        ("DAY_REVIEW", "24-hour review: continue for another 24 hours"),
        ("REVIEW", "Held"),
        ("EXIT", "Sold 9.975 BTC at 101: Target reached"),
        ("RESULT", "Fees $1.00 · P&L +$10.00 (+0.50R)"),
    ]
    held, raised, failures = events[4:7]
    assert (held["since"], held["at"], held["repeats"], held["confidence"]) == (
        (NOW - timedelta(minutes=170)).isoformat(), (NOW - timedelta(minutes=168)).isoformat(),
        3, "0.74")
    assert (raised["stop"], raised["target"], raised["confidence"]) == ("100.1", "105", "0.81")
    assert (failures["repeats"], failures["confidence"], failures["repeats_more"]) == (
        2, None, False)
    assert (events[0]["note"], events[7]["note"], events[8]["note"]) == (
        "Range reclaim.", "Trend intact.", "the agent agreed")
    assert (events[2]["qty"], events[2]["price"], events[2]["limit"], events[2]["value_usd"]) \
        == ("10", "100.1", "100.1", "1001.00")
    assert (events[-1]["pnl_usd"], events[-1]["pnl_r"], events[-1]["fees_usd"],
            events[-1]["fees_pending"]) == ("10.00", "0.500", "1.00", False)
    text = json.dumps(doc)
    assert "PRICES_CONSISTENT_NO" not in text and "REVIEW_UNAVAILABLE" not in text  # codes
    assert "note" not in events[1] and "note" not in failures
    assert trade["levels"] == [
        {"at": entry_at.isoformat(), "stop": "98", "target": "105", "known": True},
        {"at": (NOW - timedelta(minutes=167)).isoformat(), "stop": "100.1", "target": "105",
         "known": True}]


def test_a_closed_trade_with_fees_pending_says_so_and_an_open_trade_has_no_exit():
    pending = build_dashboard(snapshot(trades=[trade_row(1, known=False)]))
    assert pending["past"]["closed_trades"][0]["events"][-1]["text"] == (
        "Fees pending · P&L +$11.00 before fees (+0.55R)")
    live = build_dashboard(snapshot(trades=[trade_row(2, closed=False, arm="FIXED_EXIT")]))
    [trade] = live["live_trades"]
    assert [(e["kind"], e["text"]) for e in trade["events"]] == [
        ("BUY", "Bought 10 BTC at 100.1, limit 100.1"),
        ("LEVELS_SET", "Stop set at 98, target 105 (fixed exit)")]
    assert trade["levels"] == [{"at": trade["entry_at"], "stop": "98", "target": "105",
                                "known": True}]


def test_a_trade_with_more_than_60_reviews_shows_the_gap_and_what_is_known():
    """The view keeps a trade's latest 60 reviews. An open trade's last level change is older
    than them: it is listed at its time, the entry levels are not known to hold until then
    (known false) and the streak that starts at the oldest listed review may be longer."""
    entry_at, change_at = NOW - timedelta(hours=5), NOW - timedelta(hours=3)
    change = {"jev_change_action": "RAISE_STOP", "jev_change_at": change_at,
              "jev_change_stop": D("100.1"), "jev_change_target": D("105"),
              "jev_change_basis": "BREAKEVEN", "current_stop": D("100.1")}
    rows = [hold(1, m) for m in range(1, PER_TRADE_LIMIT + 1)]
    doc = build_dashboard(snapshot(trades=[trade_row(1, closed=False, entry_at=entry_at,
                                                     price="101", price_at=NOW, **change)],
                                   trade_decisions=rows))
    [trade] = doc["live_trades"]
    assert [(e["kind"], e["text"]) for e in trade["events"]] == [
        ("BUY", "Bought 10 BTC at 100.1, limit 100.1"),
        ("LEVELS_SET", "Stop set at 98, target 105"),
        ("GAP", "Earlier reviews not shown"),
        ("REVIEW", "Raised stop to 100.1 (breakeven)"),
        ("REVIEW", f"Held ({PER_TRADE_LIMIT}+ reviews in a row)")]
    assert trade["events"][2]["at"] is None and trade["events"][3]["at"] == change_at.isoformat()
    assert trade["levels"] == [
        {"at": entry_at.isoformat(), "stop": "98", "target": "105", "known": False},
        {"at": change_at.isoformat(), "stop": "100.1", "target": "105", "known": True}]
    # A closed trade whose listed reviews changed nothing: its final levels hold from the
    # oldest listed review; when they began is not known.
    closed = trade_row(2, entry_at=entry_at, exit_at=NOW, current_stop=D("100.1"))
    rows = [hold(2, m) for m in range(1, PER_TRADE_LIMIT + 1)]
    [done] = build_dashboard(snapshot(trades=[closed], trade_decisions=rows))[
        "past"]["closed_trades"]
    start = (NOW - timedelta(minutes=PER_TRADE_LIMIT)).isoformat()
    assert done["levels"] == [
        {"at": entry_at.isoformat(), "stop": "98", "target": "105", "known": False},
        {"at": start, "stop": "100.1", "target": "105", "known": True}]
    assert [e["kind"] for e in done["events"]] == [
        "BUY", "LEVELS_SET", "GAP", "REVIEW", "EXIT", "RESULT"]
    # 59 reviews: nothing is cut, no gap, no "+".
    rows = [hold(3, m) for m in range(1, PER_TRADE_LIMIT)]
    [whole] = build_dashboard(snapshot(trades=[trade_row(3, closed=False, entry_at=entry_at)],
                                       trade_decisions=rows))["live_trades"]
    assert [e["text"] for e in whole["events"]][2:] == [
        f"Held ({PER_TRADE_LIMIT - 1} reviews in a row)"]


def test_level_changes_read_old_to_new_with_their_basis():
    assert level_move("RAISE_STOP", (D("98"), D("105")), (D("100.1"), D("105")),
                      "BREAKEVEN") == "Raised stop 98 → 100.1 (breakeven)"
    assert level_move("RAISE_TARGET", (D("98"), D("105")), (D("98"), D("107.5")),
                      "HIGH_24H") == "Raised target 105 → 107.5 (24-hour high)"
    assert level_move("RAISE_STOP_AND_TARGET", (D("98"), D("105")), (D("99"), D("106")),
                      "SWING_LOW_15M") == (
        "Raised stop 98 → 99 and target 105 → 106 (15-minute swing low)")
    assert level_move("RAISE_STOP", None, (D("8.79"), None), None) == "Raised stop to 8.79"
    assert level_move("HOLD", None, (None, None), None) == "Changed the levels"


def test_the_shown_trades_are_the_open_ones_and_the_latest_closed():
    rows = [trade_row(n, exit_at=NOW - timedelta(minutes=n)) for n in range(1, 6)]
    rows += [trade_row(9, closed=False)]
    assert shown_trades(rows, limit=3) == [9, 1, 2, 3]


def test_failed_reviews_today_count_new_york_days_and_mark_a_full_window():
    yesterday = NOW - timedelta(days=1)
    rows = [failed(1, 1), failed(1, 2), failed(1, 3), hold(1, 4, "UNI/USD"),
            decision_row("MAINTENANCE", yesterday, outcome="FAILED", trade_no=1)]
    assert failed_reviews_today(by_trade(rows), date(2026, 10, 1)) == (3, False)
    rows += [hold(2, m) for m in range(1, PER_TRADE_LIMIT + 1)]
    assert failed_reviews_today(by_trade(rows), date(2026, 10, 1)) == (3, True)
    jev = build_dashboard(snapshot(day_decisions=rows))["agents"]["jev"]  # the day's reads
    assert (jev["failed_today"], jev["failed_today_more"]) == (3, True)


# --- Research: schedule and runs -----------------------------------------------------------------


def test_the_research_schedule_in_words():
    every_two = {"timezone": "America/New_York", "runs": [f"{h:02d}:00" for h in range(0, 24, 2)]}
    summary = schedule_summary(every_two, NOW)
    assert (summary["text"], len(summary["times"]), summary["timezone"]) == (
        "Every 2 hours", 12, "America/New_York")
    assert summary["next_run_at"] == datetime(2026, 10, 1, 18, tzinfo=UTC)  # 14:00 New York
    assert schedule_summary({"timezone": "America/New_York", "runs": ["08:00"]}, NOW)[
        "text"] == "Daily at 08:00"
    hourly = {"timezone": "UTC", "runs": [f"{h:02d}:00" for h in range(24)], "grace_minutes": 30}
    assert schedule_summary(hourly, NOW)["text"] == "Every hour"
    ninety = {"timezone": "UTC", "grace_minutes": 30,
              "runs": [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 1440, 90)]}
    assert schedule_summary(ninety, NOW)["text"] == "Every 90 minutes"
    assert schedule_summary({"timezone": "UTC", "runs": ["08:00", "14:00", "20:00"]}, NOW)[
        "text"] == "3 runs a day"
    assert schedule_summary({"timezone": "Nowhere/Invalid", "runs": ["08:00"]}, NOW) is None
    doc = build_dashboard(snapshot(), schedule=every_two)
    assert doc["agents"]["schedule"]["text"] == "Every 2 hours"


TWO_HOURS = {"timezone": "America/New_York", "runs": [f"{h:02d}:00" for h in range(0, 24, 2)]}
CYCLE_SLOT = datetime(2026, 10, 1, 14, tzinfo=UTC)  # 10:00 New York; valid until 17:00 UTC


def slot_run(run_no, slot, *, ranked=True, picks=6, ranked_n=5, vetoed=1, not_ranked=0,
             selected=3, symbols="AAA/USD,BBB/USD,CCC/USD", traded=2, agent="claude"):
    return {"run_no": run_no, "agent": agent, "run_at": slot,
            "submitted_at": slot + timedelta(minutes=10),
            "ranked_at": slot + timedelta(minutes=25) if ranked else None, "picks": picks,
            "ranked": ranked_n, "vetoed": vetoed, "not_ranked": not_ranked,
            "selected": selected, "selected_symbols": symbols, "traded": traded}


def cycle_rows(run_no=2, slot=CYCLE_SLOT):
    """Run ``run_no``'s six picks and Jev's verdicts: two traded (#5 open, #6 closed)."""
    received, ranked = slot + timedelta(minutes=10), slot + timedelta(minutes=25)
    rows = []
    for symbol, verdict, rank, trade_no in (
            ("AAA/USD", "SELECTED", 1, 5), ("BBB/USD", "SELECTED", 2, 6),
            ("CCC/USD", "SELECTED", 3, None), ("DDD/USD", "PASSED", 11, None),
            ("EEE/USD", "VETOED", None, None), ("FFF/USD", None, None, None)):
        rows.append(decision_row("PICK", received, actor="RESEARCH_AGENT", agent="claude",
                                 symbol=symbol, trade_no=trade_no, run_no=run_no,
                                 action="CHART", entry=D("100"), stop=D("98"),
                                 target=D("105"), note=f"why {symbol}"))
        if verdict:
            rows.append(decision_row("SELECTION", ranked, symbol=symbol, trade_no=trade_no,
                                     run_no=run_no, outcome=verdict, jev_rank=rank,
                                     note="PRICES_CONSISTENT_NO"))
    return rows


def cycle_snapshot(as_of, **overrides):
    runs = [slot_run(0, CYCLE_SLOT - timedelta(days=1)),
            slot_run(1, CYCLE_SLOT - timedelta(hours=2)), slot_run(2, CYCLE_SLOT),
            slot_run(3, CYCLE_SLOT + timedelta(hours=2), ranked=False)]
    trades = [trade_row(5, closed=False, symbol="AAA/USD", price="101", price_at=as_of,
                        entry_at=CYCLE_SLOT + timedelta(minutes=28)),
              trade_row(6, symbol="BBB/USD", entry_at=NOW - timedelta(hours=5),
                        exit_at=NOW - timedelta(hours=1))]
    return snapshot(status=status(as_of=as_of), runs=runs, trades=trades,
                    cycle_picks=cycle_rows(), **overrides)


def test_the_current_cycle_is_the_newest_ranked_run_with_each_picks_status():
    """Run 3 (12:00 New York) is received but not ranked: the current cycle is run 2 (10:00),
    valid until the next run plus the grace (12:00 + 60 min New York). Its picks in Jev's
    order: traded (open, closed), selected without an entry, passed, vetoed, no verdict."""
    as_of = NOW + timedelta(minutes=15)
    agents = build_dashboard(cycle_snapshot(as_of), schedule=TWO_HOURS)["agents"]
    cycle = agents["cycle"]
    assert (cycle["run_no"], cycle["runs"], cycle["agents"], cycle["run_at"],
            cycle["received_at"], cycle["ranked_at"], cycle["valid_until"], cycle["live"]) == (
        2, [2], ["claude"], "2026-10-01T14:00:00+00:00", "2026-10-01T14:10:00+00:00",
        "2026-10-01T14:25:00+00:00", "2026-10-01T17:00:00+00:00", True)
    assert (cycle["picks"], cycle["selected"], cycle["traded"]) == (6, 3, 2)
    assert [(p["symbol"], p["jev"], p["jev_rank"], p["trade_no"], p["status"],
             p["status_text"]) for p in cycle["picks_list"]] == [
        ("AAA/USD", "SELECTED", 1, 5, "IN_TRADE", "In trade #5"),
        ("BBB/USD", "SELECTED", 2, 6, "CLOSED", "Closed #6"),
        ("CCC/USD", "SELECTED", 3, None, "NO_ENTRY", "No entry yet"),
        ("DDD/USD", "PASSED", 11, None, "NOT_SELECTED", "Not selected"),
        ("EEE/USD", "VETOED", None, None, "NOT_SELECTED", "Not selected"),
        ("FFF/USD", None, None, None, "NO_VERDICT", "No verdict")]
    first = cycle["picks_list"][0]
    assert (first["entry"], first["stop"], first["target"], first["kind"], first["agent"]) == (
        "100", "98", "105", "CHART", "claude")
    # The trade's P&L: unrealized while open (10 x (101 - 100.1)), realized once closed.
    assert [p["trade_pnl_usd"] for p in cycle["picks_list"][:3]] == ["9.00", "10.00", None]
    assert "PRICES_CONSISTENT_NO" not in json.dumps(agents)  # veto codes stay private
    pending = agents["pending_run"]
    assert (pending["run_no"], pending["run_at"], pending["received_at"],
            pending["ranked_at"]) == (3, "2026-10-01T16:00:00+00:00",
                                      "2026-10-01T16:10:00+00:00", None)
    assert agents["latest_run"]["run_no"] == 3 and agents["runs_total"] == 4
    # Today's runs are the New York day's, by slot, newest first (run 0 was yesterday).
    assert [r["run_no"] for r in agents["today_runs"]] == [3, 2, 1]
    assert (agents["today_runs"][1]["picks"], agents["today_runs"][1]["selected"],
            agents["today_runs"][1]["traded"]) == (6, 3, 2)
    # After the limit, a selected pick without an entry has expired.
    late = build_dashboard(cycle_snapshot(NOW + timedelta(hours=1, minutes=1)),
                           schedule=TWO_HOURS)["agents"]["cycle"]
    assert late["live"] is False
    assert [p["status"] for p in late["picks_list"]][:3] == ["IN_TRADE", "CLOSED", "EXPIRED"]


def test_no_cycle_before_jevs_first_ranking_and_none_pending_once_ranked():
    empty = build_dashboard(snapshot())["agents"]
    assert (empty["cycle"], empty["pending_run"], empty["today_runs"], empty["latest_run"],
            empty["jev"]["cycle"]) == (None, None, [], None, None)
    assert empty["jev"]["today"]["lines"] == []
    waiting = build_dashboard(snapshot(runs=[slot_run(1, CYCLE_SLOT, ranked=False)]))["agents"]
    assert waiting["cycle"] is None and waiting["pending_run"]["run_no"] == 1
    ranked = build_dashboard(snapshot(runs=[slot_run(1, CYCLE_SLOT)]))["agents"]
    assert ranked["cycle"]["run_no"] == 1 and ranked["pending_run"] is None
    assert ranked["cycle"]["picks_list"] == []  # no pick rows read


def cycle_day_rows():
    """Jev's decisions of the day (and one the evening before), newest first as read."""
    def review(trade_no, symbol, minutes, outcome="HELD", **fields):
        return decision_row("MAINTENANCE", NOW - timedelta(minutes=minutes), outcome=outcome,
                            trade_no=trade_no, symbol=symbol, confidence=D("0.74"), **fields)

    rows = [review(5, "AAA/USD", 49, "FAILED"), review(5, "AAA/USD", 50, "FAILED"),
            review(5, "AAA/USD", 80, "APPLIED", action="RAISE_STOP", stop=D("100.1"),
                   target=D("105"), basis="BREAKEVEN"),
            *(review(5, "AAA/USD", m) for m in (88, 89, 90)),
            *(review(6, "BBB/USD", m) for m in (178, 179, 180)),
            decision_row("REVIEW_ANSWER", NOW - timedelta(minutes=30), actor="RESEARCH_AGENT",
                         agent="claude", outcome="CONTINUE", trade_no=5, symbol="AAA/USD"),
            decision_row("DAY_REVIEW", NOW - timedelta(hours=13), outcome="CONTINUE",
                         trade_no=6, symbol="BBB/USD", note="AGREED")]
    return sorted(rows, key=lambda r: r["at"], reverse=True)


def test_jevs_log_this_cycle_and_today():
    """This cycle: since run 2's selection (14:25 UTC), its verdicts then the reviews, level
    changes and the trades' entries and exits. Today: since midnight New York, each run's
    selection as one line. Newest first, repeats folded; the agents' answers are not Jev's."""
    as_of = NOW + timedelta(minutes=15)
    jev = build_dashboard(cycle_snapshot(as_of, day_decisions=cycle_day_rows()),
                          schedule=TWO_HOURS)["agents"]["jev"]
    cycle = jev["cycle"]
    assert (cycle["run_no"], cycle["since"], cycle["more"]) == (
        2, "2026-10-01T14:25:00+00:00", False)
    assert [(line["kind"], line["text"]) for line in cycle["lines"]] == [
        ("MAINTENANCE", "AAA/USD: Review failed (2 in a row)"),
        ("SELL", "BBB/USD: Sold at 101 · Target reached (#6)"),
        ("MAINTENANCE", "AAA/USD: Raised stop to breakeven (100.1)"),
        ("MAINTENANCE", "AAA/USD: Held (3 reviews in a row)"),
        ("BUY", "AAA/USD: Bought 10 at 100.1 (#5)"),
        ("SELECTION", "Selected 3 of 6 (run 2): AAA, BBB, CCC"),
        ("SELECTION", "AAA/USD: selected (rank 1)"), ("SELECTION", "BBB/USD: selected (rank 2)"),
        ("SELECTION", "CCC/USD: selected (rank 3)"), ("SELECTION", "DDD/USD: passed (rank 11)"),
        ("SELECTION", "EEE/USD: vetoed")]
    sell = cycle["lines"][1]
    assert (sell["actor"], sell["trade_no"], sell["note"], sell["pnl_usd"]) == (
        "TRADE", 6, "P&L +$10.00", "10.00")
    assert cycle["lines"][0]["since"] == (NOW - timedelta(minutes=50)).isoformat()
    assert all(line["note"] != "PRICES_CONSISTENT_NO" for line in cycle["lines"])
    today = jev["today"]
    assert (today["day"], today["since"], today["more"]) == (
        "2026-10-01", "2026-10-01T04:00:00+00:00", False)
    assert [line["text"] for line in today["lines"]] == [
        "AAA/USD: Review failed (2 in a row)", "BBB/USD: Sold at 101 · Target reached (#6)",
        "AAA/USD: Raised stop to breakeven (100.1)", "AAA/USD: Held (3 reviews in a row)",
        "AAA/USD: Bought 10 at 100.1 (#5)", "Selected 3 of 6 (run 2): AAA, BBB, CCC",
        "BBB/USD: Held (3 reviews in a row)", "Selected 3 of 6 (run 1): AAA, BBB, CCC",
        "BBB/USD: Bought 10 at 100.1 (#6)"]
    assert (jev["failed_today"], jev["failed_today_more"]) == (2, False)


def test_a_new_selection_starts_a_new_cycle():
    as_of = NOW + timedelta(minutes=40)
    snap = cycle_snapshot(as_of, day_decisions=cycle_day_rows())
    snap["runs"][-1]["ranked_at"] = NOW + timedelta(minutes=35)  # run 3 is ranked
    snap["cycle_picks"] = [
        dict(r, run_no=3, trade_no=None,
             at=NOW + timedelta(minutes=35 if r["kind"] == "SELECTION" else 10))
        for r in cycle_rows(run_no=3, slot=NOW)]
    agents = build_dashboard(snap, schedule=TWO_HOURS)["agents"]
    assert agents["cycle"]["run_no"] == 3 and agents["pending_run"] is None
    lines = agents["jev"]["cycle"]["lines"]
    assert [line["text"] for line in lines][:2] == [
        "Selected 3 of 6 (run 3): AAA, BBB, CCC", "AAA/USD: selected (rank 1)"]
    assert all(line["kind"] == "SELECTION" for line in lines)  # nothing else since
    assert agents["jev"]["cycle"]["since"] == (NOW + timedelta(minutes=35)).isoformat()
    assert len(agents["jev"]["today"]["lines"]) == 10  # run 3's selection joins the day


def test_the_scopes_start_at_midnight_new_york_or_the_earlier_selection():
    day = status()["ny_today"]
    assert ny_midnight(day) == datetime(2026, 10, 1, 4, tzinfo=UTC)
    assert scope_start(status(), []) == datetime(2026, 10, 1, 4, tzinfo=UTC)
    late = slot_run(1, datetime(2026, 10, 1, 2, tzinfo=UTC))  # 22:00 New York the day before
    assert scope_start(status(), [late]) == datetime(2026, 10, 1, 2, 25, tzinfo=UTC)
    assert scope_start(status(), [slot_run(2, CYCLE_SLOT)]) == ny_midnight(day)


def test_an_older_buy_does_not_close_a_streak_the_views_60_reviews_cut():
    """A trade bought three hours ago and reviewed every minute: the view lists its latest 60
    reviews, so the hold streak may be longer than counted even though the buy (from the
    trades view) is listed too."""
    entry_at = NOW - timedelta(hours=3)
    rows = [hold(3, m, "ETH/USD") for m in range(1, PER_TRADE_LIMIT + 1)]
    jev = build_dashboard(snapshot(
        trades=[trade_row(3, closed=False, symbol="ETH/USD", entry_at=entry_at)],
        day_decisions=rows))["agents"]["jev"]
    held, bought = jev["today"]["lines"]
    assert held["text"] == f"ETH/USD: Held ({PER_TRADE_LIMIT}+ reviews in a row)"
    assert (bought["kind"], bought["at"]) == ("BUY", entry_at.isoformat())
    whole = build_dashboard(snapshot(
        trades=[trade_row(3, closed=False, symbol="ETH/USD", entry_at=entry_at)],
        day_decisions=rows[:-1]))["agents"]["jev"]  # 59 listed: nothing is cut
    assert whole["today"]["lines"][0]["text"] == (
        f"ETH/USD: Held ({PER_TRADE_LIMIT - 1} reviews in a row)")


def test_scoped_logs_are_bounded_and_say_when_older_lines_are_left_out():
    rows = [decision_row("MAINTENANCE", NOW - timedelta(minutes=m), outcome="APPLIED",
                         action="RAISE_STOP", stop=D(100 + m), trade_no=1)
            for m in range(1, SCOPE_LENGTH + 20)]
    jev = build_dashboard(snapshot(day_decisions=rows))["agents"]["jev"]
    assert len(jev["today"]["lines"]) == SCOPE_LENGTH and jev["today"]["more"] is True
    full = [hold(1, m) for m in range(1, 2001)]  # DAY_LIMIT rows read: older ones may exist
    jev = build_dashboard(snapshot(day_decisions=full))["agents"]["jev"]
    [line] = jev["today"]["lines"]
    assert line["repeats_more"] is True and jev["today"]["more"] is True
    assert jev["failed_today_more"] is True


def test_jevs_decision_log_holds_its_latest_decisions():
    rows = [hold(n, n, symbol=f"C{n}/USD") for n in range(1, 30)]
    jev = build_dashboard(snapshot(recent=rows))["agents"]["jev"]
    assert len(jev["decisions"]) == JEV_LOG_LENGTH == 12


def test_money_and_r_in_sentences():
    assert (usd_text(D("1234.5")), usd_text(D("-0.4")), usd_text(D("1.2"), signed=True),
            usd_text(D("-3.876"), signed=True), usd_text(None)) == (
        "$1,234.50", "−$0.40", "+$1.20", "−$3.88", "?")
    assert (r_text(D("0.915")), r_text(D("-1")), r_text(D("0")), r_text(None)) == (
        "+0.92R", "−1.00R", "0.00R", "?")


# --- Status and Jev -----------------------------------------------------------------------------


@pytest.mark.parametrize("age, halts, daily, expected", [
    (None, 0, False, "STOPPED"), (181, 0, False, "STOPPED"), (30, 1, False, "HALTED"),
    (30, 0, True, "HALTED"), (30, 0, False, "RUNNING"), (400, 1, True, "STOPPED"),
])
def test_status_pill(age, halts, daily, expected):
    beat = None if age is None else NOW - timedelta(seconds=age)
    assert status_pill(status(last_heartbeat_at=beat, active_halts=halts,
                              daily_loss_halt_today=daily)) == expected


@pytest.mark.parametrize("breaker, outcome, health", [
    (None, None, "No calls yet"), ("CLOSED", None, "OK"), (None, "VALID", "OK"),
    ("OPEN", "VALID", "Breaker open"), ("HALF_OPEN", None, "Breaker open"),
    ("CLOSED", "TIMEOUT", "Unavailable"), (None, "INVALID", "Unavailable"),
])
def test_jev_health(breaker, outcome, health):
    doc = build_dashboard(snapshot(status=status(jev_breaker=breaker,
                                                 jev_last_call_outcome=outcome)))
    assert doc["agents"]["jev"]["health"] == health


def test_next_run_follows_the_schedule_and_an_unreadable_one_shows_none():
    assert next_run({"timezone": "America/New_York", "runs": ["08:00"]}, NOW) == datetime(
        2026, 10, 2, 12, tzinfo=UTC)
    assert next_run({"timezone": "America/New_York", "runs": ["08:00", "14:00"]}, NOW) == (
        datetime(2026, 10, 1, 18, tzinfo=UTC))
    assert next_run({"timezone": "Nowhere/Invalid", "runs": ["08:00"]}, NOW) is None
    doc = build_dashboard(snapshot(runs=[run_row(1)]))
    assert doc["agents"]["research"][0]["next_run_at"] == "2026-10-02T12:00:00+00:00"


# --- RESEARCH_SCHEDULE_V2: a daily full run plus update runs (package research-loop-app) ---------

LOOP = {**TWO_HOURS, "daily": "08:00"}
DAILY_SLOT = datetime(2026, 10, 1, 12, tzinfo=UTC)  # 08:00 New York: today's daily run


def test_the_v2_schedule_in_words_names_the_daily_run_and_the_updates():
    assert schedule_summary(LOOP, NOW) == {  # NOW is 12:00 New York.
        "text": "Daily at 08:00 + updates every 2 hours", "timezone": "America/New_York",
        "times": TWO_HOURS["runs"], "next_run_at": datetime(2026, 10, 1, 18, tzinfo=UTC),
        "daily": "08:00", "next_run_kind": "UPDATE",
        "next_full_run_at": datetime(2026, 10, 2, 12, tzinfo=UTC)}
    alone = schedule_summary({"timezone": "America/New_York", "runs": ["08:00"],
                              "daily": "08:00"}, NOW)
    assert (alone["text"], alone["next_run_kind"]) == ("Daily at 08:00", "FULL")
    uneven = {"timezone": "UTC", "runs": ["08:00", "14:00", "20:00"], "daily": "08:00"}
    assert schedule_summary(uneven, NOW)["text"] == "Daily at 08:00 + 2 updates a day"
    # A V1 schedule keeps its words and exactly its keys.
    assert schedule_summary(TWO_HOURS, NOW) == {
        "text": "Every 2 hours", "timezone": "America/New_York", "times": TWO_HOURS["runs"],
        "next_run_at": datetime(2026, 10, 1, 18, tzinfo=UTC)}
    doc = build_dashboard(snapshot(), schedule=LOOP)
    assert doc["agents"]["schedule"]["text"] == "Daily at 08:00 + updates every 2 hours"


def update_rows(run_no=3, slot=CYCLE_SLOT):
    """An update run: CCC/USD selected again (adjusted levels) and a new coin, GGG/USD."""
    received, ranked = slot + timedelta(minutes=10), slot + timedelta(minutes=25)
    rows = []
    for symbol, rank in (("CCC/USD", 1), ("GGG/USD", 2)):
        rows.append(decision_row("PICK", received, actor="RESEARCH_AGENT", agent="claude",
                                 symbol=symbol, trade_no=None, run_no=run_no, action="CHART",
                                 entry=D("99.5"), stop=D("97.5"), target=D("104.5"),
                                 note=f"why {symbol}"))
        rows.append(decision_row("SELECTION", ranked, symbol=symbol, trade_no=None,
                                 run_no=run_no, outcome="SELECTED", jev_rank=rank))
    return rows


def loop_snapshot(as_of):
    runs = [slot_run(0, DAILY_SLOT - timedelta(days=1)),
            slot_run(1, DAILY_SLOT - timedelta(hours=2)),  # 06:00: the previous day's update.
            slot_run(2, DAILY_SLOT), slot_run(3, CYCLE_SLOT),
            slot_run(4, CYCLE_SLOT + timedelta(hours=2), ranked=False)]
    trades = [trade_row(5, closed=False, symbol="AAA/USD", price="101", price_at=as_of,
                        entry_at=DAILY_SLOT + timedelta(minutes=28)),
              trade_row(6, symbol="BBB/USD", entry_at=NOW - timedelta(hours=5),
                        exit_at=NOW - timedelta(hours=1))]
    return snapshot(status=status(as_of=as_of), runs=runs, trades=trades,
                    cycle_picks=cycle_rows(run_no=2, slot=DAILY_SLOT) + update_rows())


def test_under_v2_the_current_cycle_is_the_daily_run_and_its_updates():
    """Today's daily run (run 2) and the 10:00 update (run 3) are one cycle, valid until the
    next daily run plus the grace; the daily run's CCC, selected again by the update, reads
    as replaced; the unranked 12:00 update awaits Jev."""
    agents = build_dashboard(loop_snapshot(NOW + timedelta(minutes=15)), schedule=LOOP)["agents"]
    cycle = agents["cycle"]
    assert (cycle["runs"], cycle["run_no"], cycle["run_at"], cycle["valid_until"],
            cycle["live"]) == ([2, 3], 3, "2026-10-01T12:00:00+00:00",
                               "2026-10-02T13:00:00+00:00", True)
    assert [(p["symbol"], p["run_no"], p["status"], p["status_text"])
            for p in cycle["picks_list"]] == [
        ("AAA/USD", 2, "IN_TRADE", "In trade #5"), ("CCC/USD", 3, "NO_ENTRY", "No entry yet"),
        ("BBB/USD", 2, "CLOSED", "Closed #6"), ("GGG/USD", 3, "NO_ENTRY", "No entry yet"),
        ("CCC/USD", 2, "REPLACED", "Replaced by a later pick"),
        ("DDD/USD", 2, "NOT_SELECTED", "Not selected"),
        ("EEE/USD", 2, "NOT_SELECTED", "Not selected"),
        ("FFF/USD", 2, "NO_VERDICT", "No verdict")]
    assert agents["pending_run"]["run_no"] == 4
    assert agents["jev"]["cycle"]["since"] == "2026-10-01T12:25:00+00:00"  # The daily ranking.
    # Under V1 the same runs make the newest ranked slot the cycle, exactly as before.
    v1 = build_dashboard(loop_snapshot(NOW + timedelta(minutes=15)), schedule=TWO_HOURS)
    assert (v1["agents"]["cycle"]["runs"], v1["agents"]["cycle"]["valid_until"]) == (
        [3], "2026-10-01T17:00:00+00:00")
    assert "REPLACED" not in {p["status"] for p in v1["agents"]["cycle"]["picks_list"]}


# --- Words -------------------------------------------------------------------------------------


def test_jev_actions_in_plain_words():
    assert level_change("RAISE_STOP", D("331.17"), D("352"), "BREAKEVEN") == (
        "Raised stop to breakeven (331.17)")
    assert level_change("RAISE_STOP", D("148.834"), None, "SWING_LOW_15M") == (
        "Raised stop to 148.834 (15-minute swing low)")
    assert level_change("RAISE_TARGET", None, D("7.61"), "HIGH_24H") == (
        "Raised target to 7.61 (24-hour high)")
    assert level_change("RAISE_STOP_AND_TARGET", D("1.5"), D("2"), "UNKNOWN_BASIS") == (
        "Raised stop to 1.5 and target to 2")
    assert jev_action("MAINTENANCE_DECISION", "HELD", "HOLD", None, None, None) == "Held"
    assert jev_action("MAINTENANCE", "FLAGGED", None, None, None, None) == (
        "Flagged for an early exit")
    assert jev_action("MAINTENANCE", "SOMETHING_NEW", None, None, None, None) == "Reviewed"
    # Window-neutral (package review-window): the public views carry no trade's window.
    assert jev_action("DAY_REVIEW_DECISION", "CONTINUE", None, None, None, None) == "Continued"
    assert jev_action("DAY_REVIEW", "EXIT", None, None, None, None) == "Exit at the review"
    assert jev_action("DAY_REVIEW", "DISCARDED", None, None, None, None) == "Review discarded"
    assert jev_action("DAY_REVIEW", "SOMETHING_NEW", None, None, None, None) == "Review"


def test_a_live_trade_shows_the_last_action_and_an_earlier_level_change():
    raise_at, hold_at = NOW - timedelta(minutes=4), NOW - timedelta(minutes=1)
    change = {"jev_change_action": "RAISE_STOP", "jev_change_at": raise_at,
              "jev_change_stop": D("100.1"), "jev_change_target": D("105"),
              "jev_change_basis": "BREAKEVEN"}
    held = open_trade(trade_row(1, closed=False, jev_last_kind="MAINTENANCE_DECISION",
                                jev_last_outcome="HELD", jev_last_action="HOLD",
                                jev_last_at=hold_at, jev_last_confidence=D("0.74"),
                                **change), NOW)
    assert held["jev_last"] == {"text": "Held", "outcome": "HELD", "at": hold_at,
                                "confidence": D("0.74")}
    assert held["jev_last_change"] == {"text": "Raised stop to breakeven (100.1)",
                                       "at": raise_at}
    just_raised = open_trade(trade_row(1, closed=False, jev_last_kind="MAINTENANCE_DECISION",
                                       jev_last_outcome="APPLIED", jev_last_action="RAISE_STOP",
                                       jev_last_at=raise_at, jev_last_stop=D("100.1"),
                                       jev_last_basis="BREAKEVEN", **change), NOW)
    assert just_raised["jev_last"]["text"] == "Raised stop to breakeven (100.1)"
    assert just_raised["jev_last_change"] is None  # not repeated


def test_the_feed_folds_each_trades_consecutive_holds_even_when_trades_interleave():
    rows = [hold(1, 1), hold(2, 1, "ETH/USD"), hold(1, 2), hold(2, 2, "ETH/USD"),
            decision_row("MAINTENANCE", NOW - timedelta(minutes=3), outcome="APPLIED",
                         action="RAISE_STOP", stop=D("100.1"), basis="BREAKEVEN",
                         confidence=D("0.81")),
            hold(2, 3, "ETH/USD"), hold(1, 4), hold(1, 5)]
    items = feed(rows)
    assert [(i["text"], i["repeats"]) for i in items] == [
        ("BTC/USD: Held (2 reviews in a row)", 2), ("ETH/USD: Held (3 reviews in a row)", 3),
        ("BTC/USD: Raised stop to breakeven (100.1)", 1),
        ("BTC/USD: Held (2 reviews in a row)", 2)]
    assert items[0]["at"] == NOW - timedelta(minutes=1)
    assert items[0]["since"] == NOW - timedelta(minutes=2)
    assert not any(i["repeats_more"] for i in items)
    # When the rows read were cut at the limit, an open streak may be longer than counted.
    capped = feed(rows, window_full=True)
    assert [i["repeats_more"] for i in capped] == [False, True, False, True]
    assert capped[1]["text"] == "ETH/USD: Held (3+ reviews in a row)"
    assert len(feed([hold(n, n) for n in range(1, 40)])) == 20


def test_the_feed_folds_each_trades_consecutive_failed_reviews_like_its_holds():
    """The live feed of 2026-09-28: failed reviews of UNI and LTC interleaved minute by minute."""
    rows = [failed(1, 1), failed(2, 1.5, "LTC/USD"), failed(1, 2), failed(2, 2.5, "LTC/USD"),
            failed(1, 3), failed(1, 4), failed(1, 5), hold(1, 6, "UNI/USD"), failed(1, 7),
            failed(1, 8), hold(1, 9, "UNI/USD"), hold(1, 10, "UNI/USD")]
    items = feed(rows)
    assert [(i["text"], i["repeats"]) for i in items] == [
        ("UNI/USD: Review failed (5 in a row)", 5), ("LTC/USD: Review failed (2 in a row)", 2),
        ("UNI/USD: Held", 1), ("UNI/USD: Review failed (2 in a row)", 2),
        ("UNI/USD: Held (2 reviews in a row)", 2)]
    assert (items[0]["at"], items[0]["since"]) == (NOW - timedelta(minutes=1),
                                                    NOW - timedelta(minutes=5))
    assert (items[0]["note"], items[0]["confidence"]) == (None, None)  # codes stay private
    assert [i["repeats_more"] for i in feed(rows, window_full=True)] == [
        False, True, False, False, True]


def test_refused_and_discarded_reviews_fold_but_changes_and_flags_never_do():
    def review(outcome, minutes, action=None):
        return decision_row("MAINTENANCE", NOW - timedelta(minutes=minutes), outcome=outcome,
                            action=action, stop=D("101"), basis="BREAKEVEN")

    rows = [review("REFUSED", 1), review("REFUSED", 2), review("REFUSED", 3),
            review("DISCARDED", 4), review("DISCARDED", 5), review("FLAGGED", 6),
            review("FLAGGED", 7), review("APPLIED", 8, "RAISE_STOP"),
            review("APPLIED", 9, "RAISE_STOP")]
    assert [i["text"] for i in feed(rows)] == [
        "BTC/USD: Change refused by the safety checks (3 in a row)",
        "BTC/USD: Review discarded (2 in a row)",
        "BTC/USD: Flagged for an early exit", "BTC/USD: Flagged for an early exit",
        "BTC/USD: Raised stop to breakeven (101)", "BTC/USD: Raised stop to breakeven (101)"]


def test_a_streak_cut_by_the_views_60_reviews_of_its_trade_may_continue():
    """The view keeps a trade's latest 60 reviews: its oldest open streak gets a "+"."""
    rows = [failed(1, m) for m in range(1, 6)] + [hold(1, m, "UNI/USD")
                                                  for m in range(6, PER_TRADE_LIMIT + 1)]
    assert [i["text"] for i in feed(rows)] == [
        "UNI/USD: Review failed (5 in a row)",
        f"UNI/USD: Held ({PER_TRADE_LIMIT - 5}+ reviews in a row)"]
    assert [i["text"] for i in feed(rows[:-1])] == [
        "UNI/USD: Review failed (5 in a row)",
        f"UNI/USD: Held ({PER_TRADE_LIMIT - 6} reviews in a row)"]


def test_jevs_card_folds_failed_reviews_too():
    rows = [failed(1, m) for m in range(1, 6)] + [hold(1, 6, "UNI/USD")]
    jev = build_dashboard(snapshot(recent=rows))["agents"]["jev"]
    assert [d["text"] for d in jev["decisions"]] == ["UNI/USD: Review failed (5 in a row)",
                                                     "UNI/USD: Held"]


def test_each_research_run_adds_two_lines():
    picks, selection = run_decisions(run_row(4))
    assert (picks["actor"], picks["agent"], picks["text"]) == (
        "RESEARCH_AGENT", "claude", "Sent 20 picks (run 4)")
    assert (selection["actor"], selection["text"], selection["note"]) == (
        "JEV", "Selected 10 of 20 (run 4): DOT, UNI, AAVE, ARB, RENDER +5 more",
        "2 vetoed · 2 not ranked")
    lone = run_decisions(run_row(5, picks=1, ranked=1, vetoed=0, not_ranked=0, selected=1,
                                 symbols="BTC/USD", traded=0))
    assert [i["text"] for i in lone] == ["Sent 1 pick (run 5)", "Selected 1 of 1 (run 5): BTC"]
    assert lone[1]["note"] is None
    items = feed([], [run_row(1, day=1), run_row(2)])
    assert [i["text"][:14] for i in items] == ["Selected 10 of", "Sent 20 picks ",
                                               "Selected 10 of", "Sent 20 picks "]


def test_agent_answers_keep_the_agents_words_and_jev_codes_stay_out():
    rows = [
        decision_row("REVIEW_ANSWER", NOW - timedelta(minutes=30), actor="RESEARCH_AGENT",
                     agent="claude", outcome="CONTINUE", note="Trend intact."),
        decision_row("DAY_REVIEW", NOW - timedelta(minutes=10), outcome="CONTINUE",
                     note="AGREED", confidence=D("0.72")),
        decision_row("MAINTENANCE", NOW - timedelta(minutes=5), outcome="REFUSED",
                     note="STOP_NOT_BELOW_BID"),
        decision_row("EXIT_FLAG", NOW - timedelta(minutes=2), actor="RESEARCH_AGENT",
                     agent="claude", outcome="EXIT", note="Listing news reversed."),
    ]
    items = {i["kind"]: i for i in feed(rows)}
    assert (items["REVIEW_ANSWER"]["text"], items["REVIEW_ANSWER"]["note"]) == (
        "BTC/USD: review answer, continue", "Trend intact.")
    assert (items["DAY_REVIEW"]["text"], items["DAY_REVIEW"]["note"]) == (
        "BTC/USD: Continued", "the agent agreed")
    assert (items["MAINTENANCE"]["text"], items["MAINTENANCE"]["note"]) == (
        "BTC/USD: Change refused by the safety checks", None)
    assert items["EXIT_FLAG"]["text"] == "BTC/USD: flagged for an early exit"


# --- The whole document -------------------------------------------------------------------------


def test_an_empty_ledger():
    doc = build_dashboard(snapshot())
    assert doc["dashboard_version"] == DASHBOARD_VERSION
    assert (doc["title"], doc["fixture_data"], doc["data_label"]) == (DEFAULT_TITLE, False, None)
    assert doc["status"]["pill"] == "STOPPED"
    overall = doc["overall"]
    assert (overall["closed"], overall["open"], overall["pnl_usd"], overall["win_rate"],
            overall["equity_usd"], overall["open_pnl_usd"]) == (0, 0, "0.00", None, None, None)
    assert doc["live_trades"] == [] and doc["feed"] == [] and doc["past"]["days"] == []
    assert doc["agents"]["research"] == []
    jev = doc["agents"]["jev"]
    assert (jev["health"], jev["calls_today"], jev["latest_selection"], jev["decisions"]) == (
        "No calls yet", 0, None, [])


def test_title_override_and_the_fixture_label():
    doc = build_dashboard(snapshot(), title="  My lab  ", fixture_data=True)
    assert (doc["title"], doc["fixture_data"], doc["data_label"]) == (
        "My lab", True, FIXTURE_BANNER)
    assert build_dashboard(snapshot(), title="   ")["title"] == DEFAULT_TITLE


def test_latest_runs_are_each_agents_newest_run():
    runs = [{"run_no": 1, "agent": "muse"}, {"run_no": 2, "agent": "grogbot"},
            {"run_no": 3, "agent": "muse"}, {"run_no": 4, "agent": "muse"}]
    assert latest_runs(runs) == [2, 4]
    assert latest_runs([]) == []


def test_agent_cards_list_the_latest_picks_in_jevs_order_and_only_their_own_answers():
    picks = [decision_row("PICK", NOW, actor="RESEARCH_AGENT", agent="claude", symbol=s,
                          trade_no=None, run_no=2, action="CHART", entry=D("1.10"),
                          stop=D("1"), target=D("1.3"), note=f"why {s}")
             for s in ("AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD")]
    verdicts = [decision_row("SELECTION", NOW, symbol=s, trade_no=None, run_no=2, outcome=o,
                             jev_rank=r) for s, o, r in (("AAA/USD", "VETOED", None),
                                                         ("BBB/USD", "PASSED", 3),
                                                         ("CCC/USD", "SELECTED", 2),
                                                         ("DDD/USD", "SELECTED", 1))]
    recent = [decision_row("REVIEW_ANSWER", NOW - timedelta(hours=1), actor="RESEARCH_AGENT",
                           agent="claude", outcome="EXIT", note="Faded."),
              decision_row("REVIEW_ANSWER", NOW - timedelta(hours=2), actor="RESEARCH_AGENT",
                           agent="muse", outcome="CONTINUE", note="Holding.")]
    doc = build_dashboard(snapshot(runs=[run_row(1, agent="muse", day=1), run_row(2)],
                                   latest_picks=picks + verdicts, recent=recent,
                                   status=status(jev_calls_today=31)))
    cards = {c["agent"]: c for c in doc["agents"]["research"]}
    assert list(cards) == ["claude", "muse"]  # the latest run first
    claude = cards["claude"]
    assert [(p["symbol"], p["jev"], p["jev_rank"]) for p in claude["picks_list"]] == [
        ("DDD/USD", "SELECTED", 1), ("CCC/USD", "SELECTED", 2), ("BBB/USD", "PASSED", 3),
        ("AAA/USD", "VETOED", None)]
    assert claude["picks_list"][0]["why"] == "why DDD/USD"
    assert [d["note"] for d in claude["decisions"]] == ["Faded."]
    assert [d["note"] for d in cards["muse"]["decisions"]] == ["Holding."]
    assert (claude["picks"], claude["selected"], claude["traded"], claude["runs"]) == (
        20, 10, 3, 1)
    jev = doc["agents"]["jev"]
    assert jev["calls_today"] == 31
    assert jev["latest_selection"] == {"run_no": 2, "agent": "claude",
                                       "at": "2026-10-01T12:25:00+00:00", "picks": 20,
                                       "selected": 10, "passed": 6, "vetoed": 2,
                                       "not_ranked": 2,
                                       "symbols": ["DOT", "UNI", "AAVE", "ARB", "RENDER",
                                                   "FIL", "BCH", "PEPE", "SHIB", "GRT"]}
    assert all(d["actor"] == "JEV" for d in jev["decisions"])


def test_the_recent_rows_limit_marks_a_full_window():
    rows = [hold(1, m) for m in range(1, RECENT_LIMIT + 1)]
    doc = build_dashboard(snapshot(recent=rows, trades=[trade_row(1, closed=False)]))
    [line] = doc["feed"]
    assert line["repeats"] == RECENT_LIMIT and line["repeats_more"] is True
    assert line["text"] == f"BTC/USD: Held ({RECENT_LIMIT}+ reviews in a row)"


def test_no_document_field_name_is_a_private_identifier():
    doc = build_dashboard(snapshot(
        trades=[trade_row(1), trade_row(2, closed=False, price="101", price_at=NOW)],
        runs=[run_row(1)], recent=[hold(2, 1)]), fixture_data=True)
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(doc)
    forbidden = re.compile(r"(^|_)(account_id|account_hash|account_number|order|fill_id|"
                           r"setup_id|cycle_id|receipt|request|evidence|token|secret|key|"
                           r"lifecycle|excerpt|source)($|_)")
    assert not sorted(k for k in keys if forbidden.search(k))
    json.dumps(doc)  # JSON-safe as it stands


def test_the_dashboard_rules_are_recorded_in_the_package_doc():
    if not (ROOT / "docs" / "packages").is_dir():
        pytest.skip("history doc not shipped in the public export")
    doc = (ROOT / "docs" / "packages" / "experiment-page.md").read_text()
    # V3's own record (package public-page-v3) names the version; V1's rules stay in the first.
    assert DASHBOARD_VERSION in (ROOT / "docs" / "packages" / "public-page-v3.md").read_text()
    for rule in ("fees pending", "New York", "180", "catalyst_public"):
        assert rule in doc, rule


# --- EXPERIMENT_DASHBOARD_V2 (package public-page) --------------------------------------------

from catalyst_lab.experiment_report import (  # noqa: E402
    day_limits,
    entry_market,
    market_block,
    open_risk,
    regime_words,
    results,
    system_line,
    trade_plan,
    trade_story,
    traded_row,
    wait_event,
)


def page_status(**overrides):
    return {"risk_policy": "JEV_MANAGED_RISK_V4", "risk_pct": D("0.005"),
            "crypto_cap_pct": D("0.02"), "hard_loss_pct": D("0.03"), "soft_loss_pct": D("0.02"),
            "day_start_equity": D("10000"), "day_start_observed_at": NOW - timedelta(hours=11),
            "soft_limit_at": None, "soft_limit_pnl": None, "daily_halt_at": None,
            "daily_halt_pnl": None, "execution_halts": 0, "execution_halt_reason": None,
            "execution_halt_at": None, "pacing_wait_at": None, "pacing_wait_reason": None,
            "pacing_release_kind": None, "pacing_release_until": None,
            "pacing_median_1h_return": None, "pacing_recent_entries": None, "soft_wait_at": None,
            "newest_admitted_at": NOW, "rules_risk_policy": "JEV_MANAGED_RISK_V4",
            "rules_trade_plan_policy": "CRYPTO_TRADE_PLAN_V1",
            "rules_maintenance_policy": "CRYPTO_MAINTENANCE_V4",
            "rules_entry_pacing_policy": "CRYPTO_ENTRY_PACING_V1",
            "rules_stop_limit_policy": "CRYPTO_STOP_BREACH_V4",
            "rules_since": NOW - timedelta(hours=6), **overrides}


def page_trade(trade_no, *, plan=True, **overrides):
    row = {"trade_no": trade_no, "admitted_at": NOW - timedelta(hours=6),
           "risk_policy": "JEV_MANAGED_RISK_V4" if plan else "JEV_MANAGED_RISK_V3",
           "trade_plan_policy": "CRYPTO_TRADE_PLAN_V1" if plan else None,
           "maintenance_policy": "CRYPTO_MAINTENANCE_V4" if plan else "CRYPTO_MAINTENANCE_V3",
           "entry_pacing_policy": "CRYPTO_ENTRY_PACING_V1" if plan else None,
           "stop_limit_policy": "CRYPTO_STOP_BREACH_V4" if plan else None,
           "holding_policy": None, "plan_stop": D("96") if plan else None,
           "plan_target": D("106") if plan else None, "plan_target_cap": D("106"),
           "hourly_range": D("2"), "hourly_range_fraction": D("0.02"), "range_floor": D("96"),
           "stop_basis": "HOURLY_RANGE_FLOOR", "target_basis": "PLAN_CAP",
           "stop_range_multiple": D("2"), "target_r_multiple": D("1.5"),
           "window_minutes": D("1440"), "risk_stop": D("96") if plan else D("98"),
           "risk_usd": D("41") if plan else D("20"), "regime_day_tag": None,
           "regime_prior_day_tag": None, "regime_btc_1h": None, "regime_btc_4h": None,
           "regime_median_coin_1h": None, "regime_median_coin_1h_pct": None}
    return {**row, **overrides}


def heartbeat_status(**overrides):
    return status(**{"last_heartbeat_at": NOW - timedelta(seconds=30), **overrides})


@pytest.mark.parametrize("overrides,page,state,reason", [
    ({}, {}, "TRADING", None),
    ({"last_heartbeat_at": None}, {}, "STOPPED", None),
    ({}, {"pacing_wait_at": NOW - timedelta(seconds=40), "pacing_wait_reason": "MARKET_DROP",
          "pacing_median_1h_return": D("-0.031")}, "PAUSED", "MARKET_DROP"),
    ({}, {"pacing_wait_at": NOW - timedelta(minutes=6), "pacing_wait_reason": "MARKET_DROP"},
     "TRADING", None),  # An older wait no longer holds entries back.
    ({}, {"pacing_wait_at": NOW, "pacing_wait_reason": "ENTRY_RATE_LIMIT"}, "PAUSED",
     "ENTRY_RATE_LIMIT"),
    ({}, {"pacing_wait_at": NOW, "pacing_wait_reason": "MACRO_EVENT_WINDOW",
          "pacing_release_kind": "CPI", "pacing_release_until": NOW + timedelta(minutes=30)},
     "PAUSED", "MACRO_EVENT_WINDOW"),
    ({}, {"soft_limit_at": NOW - timedelta(hours=1), "pacing_wait_at": NOW,
          "pacing_wait_reason": "MARKET_DROP"}, "SOFT_LIMIT", "DAILY_SOFT_LOSS_LIMIT"),
    ({"daily_loss_halt_today": True}, {"daily_halt_at": NOW - timedelta(hours=2),
                                       "soft_limit_at": NOW - timedelta(hours=3)},
     "HALTED", "DAILY_LOSS_LIMIT"),
    ({"active_halts": 1}, {"execution_halt_reason": "OPERATOR_PAUSE"}, "PAUSED",
     "OPERATOR_PAUSE"),
    ({"active_halts": 1}, {"execution_halt_reason": "BROKER_EVENT_REQUIRES_MANUAL_REVIEW"},
     "HALTED", "BROKER_EVENT_REQUIRES_MANUAL_REVIEW"),
])
def test_the_status_line_states_and_their_precedence(overrides, page, state, reason):
    line = system_line(heartbeat_status(**overrides), page_status(**page), NOW)
    assert (line["state"], line["reason"]) == (state, reason)
    assert line["rules_since"] == NOW - timedelta(hours=6)


def test_the_status_line_words_name_the_cause_and_when_entries_resume():
    halted = system_line(heartbeat_status(daily_loss_halt_today=True), page_status(
        daily_halt_at=datetime(2026, 10, 1, 19, 24, tzinfo=UTC)), NOW)
    assert halted["text"] == ("Daily loss limit reached at 15:24 ET. Open trades were sold; new "
                              "entries resume at 00:00 ET.")
    assert halted["resume_at"] == datetime(2026, 10, 2, 4, tzinfo=UTC)  # NY midnight
    soft = system_line(heartbeat_status(), page_status(
        soft_limit_at=datetime(2026, 10, 1, 15, 2, tzinfo=UTC)), NOW)
    assert soft["text"].startswith("Daily loss reached the −2.00% soft limit at 11:02 ET: no new "
                                   "entries today")
    drop = system_line(heartbeat_status(), page_status(
        pacing_wait_at=NOW, pacing_wait_reason="MARKET_DROP",
        pacing_median_1h_return=D("-0.0312")), NOW)
    assert drop["text"] == ("Market drop: new entries wait while the median coin is down 2% in "
                            "an hour (now −3.12%).")
    cpi = system_line(heartbeat_status(), page_status(
        pacing_wait_at=NOW, pacing_wait_reason="MACRO_EVENT_WINDOW", pacing_release_kind="CPI",
        pacing_release_until=datetime(2026, 10, 1, 13, 15, tzinfo=UTC)), NOW)
    assert cpi["text"] == "CPI release window: no new entries until 09:15 ET."
    assert cpi["resume_at"] == datetime(2026, 10, 1, 13, 15, tzinfo=UTC)


def test_the_limits_bar_spans_seven_sixths_of_the_hard_limit():
    closed = [{"pnl_usd": D("-260.97"), "fees_pending": True},
              {"pnl_usd": None, "fees_pending": True}]
    live = [{"pnl_usd": D("-48.13")}, {"pnl_usd": None}]
    limits = day_limits(page_status(day_start_equity=D("9917")), live, closed)
    assert (limits["pnl_usd"], limits["realized_usd"], limits["open_usd"]) == (
        D("-309.10"), D("-260.97"), D("-48.13"))
    assert (limits["open_unpriced"], limits["fees_pending"]) == (1, 2)
    # Scale 3.5%: soft at 2/3.5, hard at 3/3.5, the loss 3.117% at 0.8905 of the bar.
    assert (limits["soft_at"], limits["hard_at"], limits["loss_fraction"]) == (
        D("0.5714"), D("0.8571"), D("0.8905"))
    assert (limits["soft_pct"], limits["hard_pct"], limits["pnl_pct"]) == (
        D("2.00"), D("3.00"), D("-3.12"))
    assert (limits["soft_limit_usd"], limits["hard_limit_usd"]) == (D("-198.34"), D("-297.51"))
    gain = day_limits(page_status(), [{"pnl_usd": D("40")}], [])
    assert gain["loss_fraction"] == D("0.0000") and gain["pnl_pct"] == D("0.40")
    beyond = day_limits(page_status(), [], [{"pnl_usd": D("-900"), "fees_pending": False}])
    assert beyond["loss_fraction"] == D("1.0000")  # The bar never overflows.
    unknown = day_limits(page_status(day_start_equity=None), [], [])
    assert unknown["hard_at"] is None and unknown["pnl_pct"] is None
    legacy = day_limits(page_status(soft_loss_pct=None), [], [])
    assert legacy["soft_at"] is None and legacy["hard_at"] == D("0.8571")


def test_open_risk_is_the_planned_risk_against_the_crypto_cap():
    risk = open_risk(page_status(), [{"risk_usd": D("41")}, {"risk_usd": D("59")},
                                     {"risk_usd": None}], None)
    assert (risk["risk_usd"], risk["risk_pct"], risk["cap_pct"], risk["fraction"],
            risk["unknown"]) == (D("100"), D("1.00"), D("2.00"), D("0.5000"), 1)
    none = open_risk(page_status(), [], None)
    assert (none["risk_pct"], none["fraction"], none["trades"]) == (D("0.00"), D("0.0000"), 0)


def test_regime_words_leave_unknown_parts_out():
    words = regime_words("UP/HIGH/BROAD/SELLOFF")
    assert words == {"tag": "UP/HIGH/BROAD/SELLOFF",
                     "words": "Bitcoin uptrend, high volatility, most coins above their 20-day "
                              "average",
                     "short": "Uptrend, high volatility, sell-off", "selloff": True}
    partial = regime_words("UNKNOWN/NORMAL/UNKNOWN/UNKNOWN")
    assert (partial["words"], partial["short"], partial["selloff"]) == (
        "normal volatility", "normal volatility", None)
    assert regime_words(None) is None
    regimes = [{"day": "2026-09-30", "tag": "UP/HIGH/BROAD/SELLOFF",
                "worst_hour_start": datetime(2026, 9, 30, 18, tzinfo=UTC),
                "worst_hour_pct": D("-3.21")}]
    market = market_block(regimes, page_status(), date(2026, 10, 1))
    assert (market["day"], market["is_today"], market["worst_hour_pct"], market["pacing_rule"]) \
        == ("2026-09-30", False, D("-3.2"), True)
    assert market_block([], None, date(2026, 10, 1)) == {"day": None, "pacing_rule": False}
    at_entry = entry_market(page_trade(1, regime_median_coin_1h="DOWN",
                                       regime_median_coin_1h_pct=D("-1.27"),
                                       regime_day_tag="UP/HIGH/BROAD/SELLOFF"))
    assert at_entry["words"] == ("Median coin −1.3% the hour before · day: uptrend, high "
                                 "volatility, sell-off")
    assert entry_market(page_trade(1)) is None


def test_the_traded_levels_are_the_plans_and_r_uses_the_plans_risk():
    row = trade_row(1, risk="20")  # research stop 98, target 105, risk 20
    traded = traded_row(row, page_trade(1))
    assert (traded["planned_stop"], traded["planned_target"], traded["planned_risk_usd"]) == (
        D("96"), D("106"), D("41"))
    assert (traded["research_stop"], traded["research_target"]) == (D("98"), D("105"))
    trade = closed_trade(traded)
    assert trade["planned_stop"] == D("96") and trade["pnl_r"] == D("10") / D("41")
    unchanged = traded_row(row, page_trade(1, plan=False))
    assert (unchanged["planned_stop"], unchanged["planned_risk_usd"]) == (D("98"), D("20"))
    plan = trade_plan(row, page_trade(1))
    assert (plan["research_stop"], plan["stop"], plan["research_target"], plan["target"]) == (
        D("98"), D("96"), D("105"), D("106"))
    assert plan["stop_rule"] == "2× the hourly range (2.00%) below the entry"
    assert plan["target_rule"] == "Capped at 1.5R above the entry" and plan["window_hours"] == 24
    kept = trade_plan(row, page_trade(1, stop_basis="RESEARCH_STOP",
                                      target_basis="RESEARCH_TARGET"))
    assert kept["stop_rule"].startswith("Research stop kept")
    assert kept["target_rule"] == "Research target kept: under the 1.5R cap"
    assert trade_plan(row, page_trade(1, plan=False)) is None


def test_the_story_adds_the_plan_and_the_waits_before_the_buy():
    row = traded_row(trade_row(1), page_trade(1))
    trade = closed_trade(row)
    plan = trade_plan(trade_row(1), page_trade(1, admitted_at=NOW - timedelta(hours=5, minutes=30)))
    waits = [{"trade_no": 1, "wait_kind": "PACING", "reason": "MARKET_DROP",
              "first_at": NOW - timedelta(hours=5, minutes=14),
              "last_at": NOW - timedelta(hours=5, minutes=11), "waits": 4,
              "worst_median_1h_return": D("-0.0234"), "release_kind": None}]
    pick = decision_row("PICK", NOW - timedelta(hours=7), actor="RESEARCH_AGENT", agent="muse",
                        entry=D("100"), stop=D("98"), target=D("105"), run_no=3, note="Reason.")
    story = trade_story(row, trade, [pick], plan=plan, waits=waits)
    kinds = [e["kind"] for e in story["events"]]
    assert kinds == ["PICK", "PLAN", "WAIT", "BUY", "LEVELS_SET", "EXIT", "RESULT"]
    plan_line, wait_line = story["events"][1], story["events"][2]
    assert plan_line["text"] == "Plan set: stop 98 → 96, target 105 → 106, 24-hour window"
    assert plan_line["actor"] == "APP"
    assert wait_line["text"] == "Entry waited: market dropping" and wait_line["actor"] == "PACING"
    assert wait_line["note"] == "4 checks · Median coin −2.34% over an hour at the worst"
    assert story["events"][4]["text"] == "Stop set at 96, target 106"  # the traded levels
    assert story["levels"][0]["stop"] == D("96")
    macro = wait_event({**waits[0], "reason": "MACRO_EVENT_WINDOW", "release_kind": "FOMC",
                        "waits": 1, "worst_median_1h_return": None})
    assert (macro["text"], macro["note"]) == ("Entry waited: FOMC release window", "1 check")


def test_results_split_by_market_and_by_rules_and_hide_small_averages():
    def closed_item(no, day, pnl, r, pending=False):
        at = datetime(2026, 9, day, 18, tzinfo=UTC)
        return {"trade_no": no, "exit_at": at, "pnl_usd": D(pnl), "pnl_r": D(r),
                "fees_pending": pending}

    closed = [closed_item(1, 28, "-10", "-0.5"), closed_item(2, 28, "5", "0.25", True),
              closed_item(3, 29, "8", "0.4"), closed_item(4, 30, "-3", "-0.1")]
    regimes = [{"day": "2026-09-28", "tag": "UP/HIGH/BROAD/SELLOFF"},
               {"day": "2026-09-29", "tag": "UP/NORMAL/BROAD/NO_SELLOFF"}]
    current = tuple(page_status()["rules_" + f] for f in (
        "risk_policy", "trade_plan_policy", "entry_pacing_policy", "stop_limit_policy"))
    v3 = ("JEV_MANAGED_RISK_V3", None, None, None)
    keys = {1: v3, 2: v3, 3: current, 4: current}
    out = results(closed, [], regimes, page_status(), keys)
    assert (out["selloff_days"]["trades"], out["selloff_days"]["days"],
            out["selloff_days"]["pnl_usd"]) == (2, 1, D("-5"))
    assert (out["other_days"]["trades"], out["untagged"]) == (1, 1)
    rules = out["rules"]
    assert (rules["earlier"]["trades"], rules["current"]["trades"]) == (2, 2)
    assert rules["earlier"]["verified"] == 1 and rules["earlier"]["avg_net_r"] is None
    assert rules["current"]["since"] == NOW - timedelta(hours=6)
    assert (rules["earlier"]["first_day"], rules["earlier"]["last_day"]) == (
        "2026-09-28", "2026-09-28")
    many = [closed_item(10 + i, 29, "1", "0.1") for i in range(30)]
    big = results(many, [], regimes, page_status(), dict.fromkeys(range(10, 40), current))
    assert big["rules"]["current"]["avg_net_r"] == D("0.100")  # 30 fee-verified: shown
    nothing = results(closed, [], [], None, keys)
    assert nothing["rules"]["current"]["trades"] == 0 and nothing["rules"]["earlier"][
        "trades"] == 4


def test_the_after_sale_block_has_no_data_source_yet_and_is_omitted():
    doc = build_dashboard(snapshot(trades=[trade_row(1)], page_trades=[page_trade(1)],
                                   page_status=page_status()))
    trade = doc["past"]["closed_trades"][0]
    assert trade["after_exit"] is None and trade["plan"]["stop"] == "96"
    assert doc["system"]["state"] == "STOPPED" and doc["results"]["rules"]["current"][
        "trades"] == 1


def test_limits_measure_the_day_from_the_v2_basis_when_an_equity_follows_it():
    v2 = page_status(day_start_equity=D("9590.78"),
                     day_start_basis="ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2",
                     day_start_observed_at=NOW - timedelta(hours=11), equity_usd=D("9612.40"),
                     equity_at=NOW - timedelta(hours=1), baseline_corrected_at=NOW - timedelta(
                         hours=10), baseline_corrected_from=D("9877.3"))
    trades = [{"pnl_usd": D("-50"), "fees_pending": False}]
    limits = day_limits(v2, [], trades)
    assert (limits["measure"], limits["pnl_usd"], limits["realized_usd"]) == (
        "ACCOUNT_EQUITY", D("21.62"), D("-50"))
    assert limits["baseline_corrected_from_usd"] == D("9877.3")
    stale = day_limits({**v2, "equity_at": NOW - timedelta(hours=12)}, [], trades)
    assert (stale["measure"], stale["pnl_usd"]) == ("TRADES", D("-50"))  # Equity before basis.
    row = day_limits(page_status(day_start_basis="ALPACA_LAST_EQUITY", equity_usd=D("9612.4"),
                                 equity_at=NOW), [], trades)
    assert row["measure"] == "TRADES"


def test_v5_words_name_each_question_its_probability_and_streak():
    from catalyst_lab.experiment_report import v5_repeated, v5_words

    def q(p, verdict, effect="RESET", streak=0):
        return {"p": D(p), "verdict": verdict, "effect": effect, "streak": streak}

    assert v5_words({"invalidation": q("0.08", "NO")}, "HELD") == "Invalidation met? no (0.08)"
    assert v5_words({"invalidation": q("0.86", "YES", "COUNTED", 2),
                     "news": q("0.5", "UNCERTAIN", "NEUTRAL")}, "HELD") == (
        "Invalidation met? yes (0.86), 2 of 3 · news contradicts? unsure (0.50)")
    assert v5_words({"invalidation": q("0.91", "YES", "CONFIRMED")}, "FLAGGED") == (
        "Invalidation met? yes (0.91), 3 of 3: flagged for an early exit")
    assert v5_words({}, "FAILED") == "Review failed"
    assert v5_repeated({"v5_ps": [D("0.2"), D("0.05")], "v5_verdicts": {"NO", "UNCERTAIN"}},
                       "4+") == "Held: invalidation met? no or unsure, 4+ reviews in a row " \
                                "(p 0.05–0.20)"


def test_a_withdrawn_soft_latch_leaves_trading_with_its_words():
    line = system_line(heartbeat_status(), page_status(
        soft_limit_withdrawn_latch_at=datetime(2026, 10, 1, 7, 10, tzinfo=UTC),
        soft_limit_withdrawn_at=NOW - timedelta(hours=4)), NOW)
    assert (line["state"], line["reason"]) == ("TRADING", "SOFT_LIMIT_WITHDRAWN")
    assert line["text"] == ("New entries allowed. The soft limit of 03:10 ET was withdrawn after "
                            "the day's start was corrected.")
    kept = system_line(heartbeat_status(), page_status(soft_limit_at=NOW, soft_limit_kept=True),
                       NOW)
    assert kept["state"] == "SOFT_LIMIT" and kept["text"].endswith("start was corrected.")
