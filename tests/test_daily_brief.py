"""DAILY_BRIEF_V1 and MISSED_TRADEABLE_V1 (package learning-loop2): the daily speed of the
learning loop -- observe and explain, research attention only.

Fixture evidence only: hand-built bars (no network), per-test disposable PostgreSQL databases.
No broker, Jev, provider or owner ledger.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import daily_brief as db
from catalyst_lab.learning_intake import day_bounds
from catalyst_lab.pick_outcomes import parse_bars
from catalyst_lab.strategies import BREAKOUT_7D_VOL2X_V1
from tests.learning_fixtures import (
    FakeBars,
    learning,  # noqa: F401 -- fixture
    minute_rows,
    mover,
    record_reality,
)
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401

DAY = date(2026, 9, 27)
START, END = day_bounds(DAY)  # 04:00 UTC to 04:00 UTC the next day.
SIGNAL_BAR = START + timedelta(hours=10)  # 14:00 UTC: the breakout bar.
SIGNAL_AT = SIGNAL_BAR + timedelta(hours=1)
READY = SIGNAL_AT + timedelta(minutes=15) + timedelta(hours=24) + timedelta(minutes=5)
UNIVERSE = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "BRK/USD"]


def hour(at, o, h, low, c, v="10"):
    return {"t": at.isoformat(), "o": str(o), "h": str(h), "l": str(low), "c": str(c),
            "v": str(v), "vw": str(c)}


def breakout_hours():
    """240 flat hours at 100 before the day, then a close at 110 on 1,000 volume at 14:00 UTC
    (above the 7-day high of 100, 24 h volume 1,230 against a 240 daily mean: a fresh signal),
    then flat at 110 for the rest of the day."""
    rows = [hour(START - timedelta(hours=n), 100, 100, 100, 100) for n in range(240, 0, -1)]
    at = START
    while at < END:
        if at < SIGNAL_BAR:
            rows.append(hour(at, 100, 100, 100, 100))
        elif at == SIGNAL_BAR:
            rows.append(hour(at, 100, 110, 100, 110, v="1000"))
        else:
            rows.append(hour(at, 110, 110, 110, 110))
        at += timedelta(hours=1)
    return rows


def target_minutes():
    """From the signal: 110, then up to 114 (the plan's 1.5R target is 113.3)."""
    return minute_rows(SIGNAL_AT, [110, 111, 112, 113, 114])


# --- Pure: the missed-tradeable computation on hand-built bars ------------------------------------


def test_the_breakout_strategy_would_have_entered_the_mover_and_its_net_r_after_fees():
    bars = parse_bars(breakout_hours())
    minutes = parse_bars(target_minutes())
    calls = []

    def fetch(signal_at, ready):
        calls.append((signal_at, ready))
        return [b for b in minutes if signal_at <= b.start < ready]

    rows = db.tradeable(BREAKOUT_7D_VOL2X_V1, "BRK/USD", bars, DAY, now=READY,
                        minute_bars_for=fetch)
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_at"] == SIGNAL_AT and row["status"] == "SIMULATED"
    assert calls == [(SIGNAL_AT, READY)]
    result = row["result"]
    # Entry 110 (the first minute's open); stop: the plan's 2% minimum (107.8) is below the
    # 2 x hourly-range floor; target 1.5R = 113.3; gross 1.5R; fee 0.25% on both legs.
    assert result["outcome"] == "TARGET" and result["fill_price"] == "110"
    assert result["plan"]["stop"] == "107.80" and result["plan"]["target"] == "113.300"
    assert D(str(result["gross_r"])) == D("1.5")
    # 1.5 - 0.0025 x 223.3 / 2.2 = 1.24625, rounded half-even.
    assert D(str(result["net_r"])) == D("1.2462")
    summary = db.mover_tradeable_summary({"BREAKOUT_7D_VOL2X_V1": rows}, [])
    assert summary == {"signals": 1, "simulated_entries": 1, "pending": False,
                       "best_net_r": D("1.2462"), "missed_tradeable": True}
    # Traded that day: not a miss.
    assert not db.mover_tradeable_summary({"X": rows}, ["setup-1"])["missed_tradeable"]


def test_a_signal_whose_hold_has_not_passed_is_pending_and_reads_no_minutes():
    bars = parse_bars(breakout_hours())

    def never(*_):
        raise AssertionError("no minute bars before the hold has passed")

    rows = db.tradeable(BREAKOUT_7D_VOL2X_V1, "BRK/USD", bars, DAY,
                        now=READY - timedelta(seconds=1), minute_bars_for=never)
    assert [r["status"] for r in rows] == ["PENDING"]
    summary = db.mover_tradeable_summary({"S": rows}, [])
    assert summary["pending"] and summary["best_net_r"] is None
    assert not summary["missed_tradeable"]
    # A flat coin never signals.
    flat = parse_bars([hour(START - timedelta(hours=n), 100, 100, 100, 100)
                       for n in range(240, -20, -1)])
    assert db.tradeable(BREAKOUT_7D_VOL2X_V1, "FLAT/USD", flat, DAY, now=READY,
                        minute_bars_for=never) == []


def test_only_registered_mechanical_strategies_are_simulated():
    ids = [s.strategy_id for s in db.mechanical_strategies()]
    assert "BREAKOUT_7D_VOL2X_V1" in ids and "PULLBACK_V1" not in ids


def test_sector_clusters_need_two_movers_moving_the_same_way():
    movers = [{"symbol": "A/USD", "sector": "L1", "return_pct": "6"},
              {"symbol": "B/USD", "sector": "L1", "return_pct": "5.5"},
              {"symbol": "C/USD", "sector": "L1", "return_pct": "-7"},
              {"symbol": "D/USD", "sector": "MEME", "return_pct": "9"}]
    assert db.sector_clusters(movers) == [
        {"sector": "L1", "direction": "UP", "coins": 2, "symbols": ["A/USD", "B/USD"],
         "mean_return_pct": D("5.75")}]


def test_pre_move_facts_use_only_bars_that_ended_before_the_move():
    rows = [hour(START - timedelta(hours=n), 100, 102, 98, 100, v="10")
            for n in range(170, 0, -1)]
    rows += [hour(START, 100, 101, 99, 101, v="50"), hour(START + timedelta(hours=1), 101, 140,
                                                         101, 139, v="9999")]
    bars = parse_bars(rows)
    facts = db.pre_move_facts(bars, START + timedelta(hours=1, minutes=30))
    assert facts["status"] == "MEASURED" and facts["close"] == "101"
    assert facts["as_of"] == START + timedelta(hours=1)  # The 05:00 bar (the move) is excluded.
    assert facts["below_7d_high_pct"] == D("0.9901")  # 102 over 101.
    assert facts["above_7d_low_pct"] == D("3.0612")  # 101 over 98.
    assert facts["return_24h_pct"] == D("1.0000")  # 100 open 24 h earlier to 101.
    assert db.pre_move_facts(bars, START - timedelta(days=30))["status"] == "NO_BARS_BEFORE_MOVE"


def test_hour_medians_and_selloff_hours():
    coins = {s: parse_bars([hour(START, 100, 100, 97, 97), hour(START + timedelta(hours=1),
                                                                 97, 98, 97, 98)])
             for s in UNIVERSE[:5]}
    rows = db.hour_medians(coins, UNIVERSE[:5], DAY)
    assert len(rows) == 24 and rows[0]["median_return_pct"] == D("-3.0000")
    assert rows[0]["new_york_hour"] == "00:00" and rows[2]["median_return_pct"] is None
    assert [r["new_york_hour"] for r in db.selloff_hours(rows)] == ["00:00"]
    thin = db.hour_medians({s: coins[s] for s in UNIVERSE[:4]}, UNIVERSE[:4], DAY)
    assert db.selloff_hours(thin) == []  # Fewer than five coins: not measured.


def test_breadth_and_knowability_words():
    coins = [{"status": "MEASURED", "return_pct": v} for v in ("2", "-3", "0.5", "1.5")]
    assert db.breadth(coins + [{"status": "NO_BARS"}]) == {
        "measured": 4, "up": 2, "down": 1, "flat": 1, "positive": 3,
        "median_return_pct": D("1.0000")}
    assert db.knowability(None, []) == "PENDING_AGENT_POST_MORTEM"
    note = {"cause": "COIN_NEWS", "knowable_before_move": True}
    assert db.knowability(note, ["muse"]) == "KNOWABLE_AND_MISSED"
    assert db.knowability(note, []) == "KNOWABLE_AND_EXPECTED"
    assert db.knowability({**note, "knowable_before_move": False}, []) == \
        "KNOWABLE_NOT_ACTIONABLE"
    assert db.knowability({**note, "knowable_before_move": None}, []) == "KNOWABILITY_UNKNOWN"


def test_focus_items_are_research_attention_only():
    items = db.focus_items(
        clusters=[{"sector": "L1", "direction": "UP", "coins": 3, "symbols": ["A", "B", "C"],
                   "mean_return_pct": D(6)}],
        selloffs=[{"new_york_hour": "14:00", "median_return_pct": D("-2.5")}],
        regime={"btc_trend": {"label": "DOWN"}}, knowable_before=2, pending_queue=4,
        strategy_hits={"BREAKOUT_7D_VOL2X_V1": {"movers_with_signal": 1, "movers": 5,
                                                "simulated": 1, "mean_net_r": D("1.2")}})
    assert [i["kind"] for i in items] == ["SECTOR_ATTENTION", "CATALYST_ATTENTION",
                                          "SETUP_ATTENTION", "MARKET_CONTEXT", "MARKET_CONTEXT",
                                          "AGENT_QUEUE"]
    assert {i["scope"] for i in items} == {"RESEARCH_ATTENTION_ONLY"}
    assert "named version" in items[2]["text"]  # A setup note never becomes a rule.
    for item in items:  # Attention words only: no filter, cap or exclusion.
        assert not any(word in item["text"].lower() for word in ("exclude", "drop ", "only"))


# --- The ledger: records, idempotency, catch-up -------------------------------------------------


def reality_body():
    movers = [mover("BRK/USD", "10.0", SIGNAL_BAR, top_up=True),
              mover("ETH/USD", "-6.0", START + timedelta(hours=3), top_down=True)]
    movers[1]["missed_by"] = ["muse"]
    coins = [{"symbol": s, "status": "MEASURED", "return_pct": r}
             for s, r in (("BTC/USD", "1"), ("ETH/USD", "-6"), ("SOL/USD", "2"),
                          ("DOGE/USD", "0.2"), ("AVAX/USD", "-1"), ("BRK/USD", "10"))]
    return movers, {"universe": {"symbols": UNIVERSE}, "coins": coins,
                    "factors": {"btc_return_pct": "1", "eth_return_pct": "-6", "sectors": []}}


def reader():
    return FakeBars(minutes={"BRK/USD": target_minutes()}, hours={"BRK/USD": breakout_hours()})


def test_the_brief_records_once_and_explains_without_inventing_reasons(learning):  # noqa: F811
    movers, body = reality_body()
    record_reality(learning.store, DAY.isoformat(), movers, **body)
    now = END + timedelta(hours=1, minutes=30)  # 01:30 New York the next night.
    status, seq = db.record_brief(learning.store, reader(), DAY, now=now)
    assert status == "RECORDED"
    assert db.record_brief(learning.store, reader(), DAY, now=now) == ("ALREADY_RECORDED", seq)
    brief = db.recorded_brief(learning.store.repo, DAY)["body"]
    assert brief["brief_version"] == "DAILY_BRIEF_V1" and brief["speed"] == \
        "DAILY_OBSERVE_AND_EXPLAIN"
    items = {m["symbol"]: m for m in brief["movers"]["items"]}
    # No post-mortem yet: the cause is pending an agent, never written by the brief.
    assert items["BRK/USD"]["why"] == {"post_mortem": None,
                                       "knowability": "PENDING_AGENT_POST_MORTEM"}
    assert items["BRK/USD"]["pullback"] == "NOT_DETERMINABLE_WITHOUT_RESEARCH_LEVELS"
    # The signal at 15:00 UTC has not finished its 24-hour hold at 01:30 New York.
    assert items["BRK/USD"]["tradeable"]["pending"] is True
    assert items["BRK/USD"]["strategies"]["BREAKOUT_7D_VOL2X_V1"][0]["status"] == "PENDING"
    assert brief["missed"]["outlook_misses"] == ["ETH/USD"]
    assert brief["missed"]["pending_post_mortem"] == ["BRK/USD", "ETH/USD"]
    assert brief["ours"]["scorecard"] == "NOT_RECORDED"
    assert brief["market"]["breadth"]["up"] == 2 and brief["market"]["regime_tag"] == \
        "NOT_RECORDED"
    assert {i["scope"] for i in brief["research_focus"]} == {"RESEARCH_ATTENTION_ONLY"}
    assert brief["text"][0].startswith("Daily brief 2026-09-27")
    assert any("Mover BRK/USD 10.0%" in line for line in brief["text"])
    assert brief["agent_queue"]["needs_agent"][0].startswith("POST_MORTEM_V1")
    view = db.agent_view(brief)
    assert "ours" not in view and view["movers"][0]["knowability"] == \
        "PENDING_AGENT_POST_MORTEM"


def test_the_missed_tradeable_record_waits_for_every_hold_then_records_once(learning):  # noqa: F811
    movers, body = reality_body()
    record_reality(learning.store, DAY.isoformat(), movers, **body)
    early = END + timedelta(hours=10)
    with pytest.raises(db.BriefUnavailable, match="MISSED_NOT_READY"):
        db.compute_missed(learning.store.repo, reader(), DAY, now=early)
    now = db.missed_ready_at(DAY) + timedelta(hours=1)
    status, seq = db.record_missed(learning.store, reader(), DAY, now=now)
    assert status == "RECORDED"
    assert db.record_missed(learning.store, reader(), DAY, now=now) == ("ALREADY_RECORDED", seq)
    record = db.recorded_missed(learning.store.repo, DAY)["body"]
    assert record["missed_version"] == "MISSED_TRADEABLE_V1" and record["day_tag"] == "UNKNOWN"
    lines = {line["symbol"]: line for line in record["movers"]}
    assert lines["BRK/USD"]["missed_tradeable"] is True
    assert D(str(lines["BRK/USD"]["best_net_r"])) == D("1.2462")
    assert lines["ETH/USD"]["signals"] == 0 and not lines["ETH/USD"]["missed_tradeable"]
    totals = record["totals"]
    assert (totals["movers"], totals["with_signal"], totals["simulated_entries"],
            totals["positive_entries"], totals["missed_tradeable"]) == (2, 1, 1, 1, 1)
    # The next night's brief carries the final figures of the day before.
    nxt = DAY + timedelta(days=1)
    record_reality(learning.store, nxt.isoformat(), [], universe={"symbols": UNIVERSE},
                   coins=[], factors={})
    brief = db.compute_brief(learning.store.repo, reader(), nxt, now=now)
    assert brief["missed"]["previous_day_final"]["missed_tradeable_symbols"] == ["BRK/USD"]


def test_the_step_catches_up_older_days_and_never_fails_on_days_before_any_universe(
        learning):  # noqa: F811
    movers, body = reality_body()
    record_reality(learning.store, DAY.isoformat(), movers, **body)
    now = datetime(2026, 10, 3, 5, 30, tzinfo=UTC)
    code, details = db.missed_step(learning.store, reader(), now)
    # No research universe was ever recorded in this fixture: every day but 09-27 (whose
    # reality is recorded) can never be measured, is not a failure and uses no budget.
    assert details["2026-09-27"] == "RECORDED"
    assert details["2026-09-19"] == "MISSED_UNIVERSE_UNAVAILABLE"
    assert code is None and "DEFERRED" not in details.values()
    code, again = db.missed_step(learning.store, reader(), now)
    assert "2026-09-27" not in again  # Recorded: never tried again.
    # Days that fail (here: a reality recorded for days whose bars cannot be read) use the
    # budget: at most four per run, the rest DEFERRED to the next night.
    for back in range(1, 7):
        day = DAY - timedelta(days=back)
        record_reality(learning.store, day.isoformat(), [], universe={"symbols": ["X/USD"]},
                       coins=[], factors={})
    code, details = db.missed_step(learning.store, FakeBars(fail={"X/USD"}), now)
    assert code == "MISSED_BARS_UNAVAILABLE"
    assert list(details.values()).count("MISSED_BARS_UNAVAILABLE") == db.MISSED_BACKFILL_PER_RUN
    assert details["2026-09-25"] == details["2026-09-26"] == "DEFERRED"
    # The second night after a day: 01:30 New York on the 29th records the 27th.
    assert db.missed_days(datetime(2026, 9, 29, 5, 30, tzinfo=UTC))[-1] == date(2026, 9, 27)
    assert db.missed_days(datetime(2026, 9, 29, 4, 0, tzinfo=UTC))[-1] == date(2026, 9, 26)


def test_a_failed_bar_read_records_nothing(learning):  # noqa: F811
    movers, body = reality_body()
    record_reality(learning.store, DAY.isoformat(), movers, **body)
    failing = FakeBars(hours={"BRK/USD": breakout_hours()}, fail={"SOL/USD"})
    with pytest.raises(db.BriefUnavailable, match="BRIEF_BARS_UNAVAILABLE"):
        db.record_brief(learning.store, failing, DAY, now=END + timedelta(hours=2))
    assert db.recorded_brief(learning.store.repo, DAY) is None
    with pytest.raises(db.BriefUnavailable, match="BRIEF_UNIVERSE_UNAVAILABLE"):
        db.compute_brief(learning.store.repo, reader(), DAY - timedelta(days=5),
                         now=END + timedelta(hours=2))
