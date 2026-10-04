"""CRYPTO_MAINTENANCE_V1, CRYPTO_PARTIAL_ENTRY_V1 and JEV_MANAGED_POSITION_CONTEXT_V4 /
QUESTIONS_V4 as pure rules (package maintenance, plan 4.6). No database, broker or provider."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab.account_risk import FIXED_EXIT_ARM, JEV_MANAGED_ARM
from catalyst_lab.jev_contract import INSUFFICIENT, encoded
from catalyst_lab.maintenance_dossier import (
    STATE_BYTE_BUDGET,
    compile_maintenance_state,
    maintenance_questions,
    read_answer,
)
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable, encoded_bytes
from catalyst_lab.managed_review import ManagedContext
from catalyst_lab.setup_scan import CompletedBar

NOW = datetime(2026, 9, 27, 15, 7, 30, tzinfo=UTC)
INC = D("0.01")


def bar(end, low, high, *, seconds=900, open_=None, close=None, volume="10", symbol="SOL/USD"):
    start = end - timedelta(seconds=seconds)
    low, high = D(str(low)), D(str(high))
    open_ = D(str(open_)) if open_ is not None else (low + high) / 2
    close = D(str(close)) if close is not None else (low + high) / 2
    return CompletedBar(start, end, open_, high, low, close, D(volume), "LAB_FIXTURE",
                        "FIXTURE", f"LAB_FIXTURE:{symbol}:{seconds}:{start.isoformat()}", True)


def series(count, *, seconds, base=D("104"), spread=D("0.5"), lows=None, highs=None,
           now=NOW, symbol="SOL/USD"):
    """``count`` completed bars ending at the last boundary before ``now``, oldest first; each
    bar spans base ± spread unless ``lows``/``highs`` (by index from the newest, 0 = newest)
    override it."""
    last_end = cm.floor_time(now, seconds)
    result = []
    for i in range(count):
        end = last_end - timedelta(seconds=seconds * (count - 1 - i))
        back = count - 1 - i
        low = D(str((lows or {}).get(back, base - spread)))
        high = D(str((highs or {}).get(back, base + spread)))
        result.append(bar(end, low, max(high, low), seconds=seconds, symbol=symbol))
    return result


# --- The versions and their scope ----------------------------------------------------------------

def test_the_versions_are_exact_and_scoped_to_report_v3_crypto_in_the_managed_arm():
    record = cm.CRYPTO_MAINTENANCE.record()
    assert record["policy_id"] == "CRYPTO_MAINTENANCE_V1"
    assert record["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V4"
    assert record["question_version"] == "JEV_MANAGED_POSITION_QUESTIONS_V4"
    assert (record["review_bar_seconds"], record["min_review_interval_seconds"],
            record["answer_max_age_seconds"], record["max_stop_options"],
            record["max_target_options"]) == (900, 60, 60, 5, 5)
    assert (record["near_target_fraction"], record["near_stop_fraction"],
            record["stop_bid_margin"], record["swing_low_price_margin"],
            record["benchmark_shock_fraction"]) == ("0.005", "0.005", "0.005", "0.01", "0.03")
    assert record["benchmark_symbol"] == "BTC/USD" and record["benchmark_window_seconds"] == 900
    assert cm.CRYPTO_PARTIAL_ENTRY.record() == {"policy_id": "CRYPTO_PARTIAL_ENTRY_V1",
                                                "max_remainder_seconds": 600}
    for change in ({"review_bar_seconds": 60}, {"near_target_fraction": "0.01"},
                   {"policy_id": "CRYPTO_MAINTENANCE_V2"}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
            cm.MaintenancePolicy(**{**record, **change})
    with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_PARTIAL_ENTRY_POLICY_REQUIRED"):
        cm.PartialEntryPolicy("CRYPTO_PARTIAL_ENTRY_V1", 599)
    v3 = {"market": "CRYPTO", "report_schema_version": "AGENT_RESEARCH_REPORT_V3"}
    fields = cm.admission_fields(v3, JEV_MANAGED_ARM)
    # From package answer-rules admission recorded CRYPTO_MAINTENANCE_V2 (the minute cadence,
    # context and questions V5 and its answer rule; every other number V1's), and from package
    # jev-budget CRYPTO_MAINTENANCE_V3 (V2 with the budget's cadence). V1's record above is
    # unchanged and still read back (tests/test_answer_rules.py). From package trade-plan:
    # CRYPTO_MAINTENANCE_V4 (V3 with the stop-raise guards). From package jev-b1:
    # CRYPTO_MAINTENANCE_V5 (V4 with two yes/no questions; tests/test_jev_b1_rules.py).
    assert fields == {"maintenance_policy": cm.CRYPTO_MAINTENANCE_V5.record(),
                      "partial_entry_policy": cm.CRYPTO_PARTIAL_ENTRY.record()}
    changed = {"policy_id", "answer_rule", "review_bar_seconds", "context_version",
               "question_version", "spend_guard", "throttled_review_bar_seconds",
               "tight_review_bar_seconds", "raise_min_r", "raise_range_multiple",
               "raise_spacing_seconds", "breakeven_fee_fraction", "breakeven_basis",
               "target_cap", "hour_bar_seconds", "confirm_bar_seconds", "confirm_bars",
               "btc_move_fraction", "yes_at_or_above", "no_at_or_below", "confirm_yes",
               "news_max_asks", "state_byte_budget", "invalidation_if_unanswered",
               "news_if_unanswered"}
    assert {k: v for k, v in fields["maintenance_policy"].items() if k not in changed} == {
        k: v for k, v in record.items() if k not in changed}
    # The control arm opens like the maintained arm (plan 4.6.1) but is never maintained (4.6.6).
    assert cm.admission_fields(v3, FIXED_EXIT_ARM) == {
        "partial_entry_policy": cm.CRYPTO_PARTIAL_ENTRY.record()}
    assert cm.admission_fields({**v3, "report_schema_version": "AGENT_RESEARCH_REPORT_V2"},
                               JEV_MANAGED_ARM) == {}
    assert cm.admission_fields({"market": "CRYPTO"}, JEV_MANAGED_ARM) == {}
    assert cm.admission_fields({**v3, "market": "US_STOCKS"}, JEV_MANAGED_ARM) == {}
    assert cm.active(fields) and cm.partial_entry_active(fields)
    assert not cm.active({}) and not cm.partial_entry_active({"state": "OPEN"})


def test_triggers_at_their_exact_boundaries():
    entry, risk = D("100.10"), D("5.10")
    assert cm.r_per_coin({"max_entry_price": "100.10", "stop": "95"}) == risk
    assert cm.milestone(D("105.19"), entry, risk) == 0
    assert cm.milestone(D("105.20"), entry, risk) == 1  # Exactly +1R.
    assert cm.milestone(D("115.40"), entry, risk) == 3
    assert cm.milestone(D("99"), entry, risk) == 0
    target, stop = D("111"), D("95")
    assert cm.near_target(D("110.445"), target)  # Exactly 0.5% below the target.
    assert not cm.near_target(D("110.444"), target)
    assert cm.near_stop(D("95.475"), stop)  # Exactly 0.5% above the stop.
    assert not cm.near_stop(D("95.476"), stop)
    assert not cm.near_stop(D("95"), stop)  # At the stop the stop fires; no review.


def test_partial_entry_is_kept_ten_minutes_inside_the_entry_zone():
    first = NOW
    reason = cm.partial_entry_cancel_reason
    common = {"max_entry": D("100.10"), "stop": D("95"), "opened_at": first}
    assert reason(now=first + timedelta(seconds=599), bid=D("100"), ask=D("100.10"),
                  **common) is None
    assert reason(now=first + timedelta(seconds=600), bid=D("100"), ask=D("100.10"),
                  **common) == "PARTIAL_ENTRY_TIMEOUT"
    assert reason(now=first, bid=D("100.10"), ask=D("100.11"), **common) \
        == "PARTIAL_ENTRY_ABOVE_MAX_ENTRY"
    assert reason(now=first, bid=D("95"), ask=D("95.01"), **common) \
        == "PARTIAL_ENTRY_AT_OR_BELOW_STOP"
    assert reason(now=first, bid=None, ask=None, **common) is None  # No fresh quote: bound only.
    assert reason(now=first, bid=None, ask=None, max_entry=D("100.10"), stop=D("95"),
                  opened_at=None) == "PARTIAL_ENTRY_FIRST_FILL_UNKNOWN"


# --- Bars and options ----------------------------------------------------------------------------

def test_swing_points_are_strict_two_bar_pivots_and_four_hour_bars_are_utc_aligned():
    bars = series(9, seconds=3600, lows={2: "101", 6: "100.5"}, highs={2: "106", 5: "106"})
    assert [b.low for b in cm.swing_lows(bars)] == [D("100.5"), D("101")]
    # Two equal highs two bars apart: each is strictly higher than its own neighbours.
    assert [b.high for b in cm.swing_highs(bars)] == [D("106"), D("106")]
    flat = series(5, seconds=3600)
    assert cm.swing_lows(flat) == [] and cm.swing_highs(flat) == []
    tie = series(5, seconds=3600, lows={2: "103", 3: "103"})
    assert cm.swing_lows(tie) == []  # A tie is not strictly lower.
    hourly = series(12, seconds=3600, highs={1: "110"})
    four = cm.aggregate(hourly, cm.FOUR_HOURS, now=NOW)
    assert all(b.start_at.hour % 4 == 0 and b.end_at - b.start_at == timedelta(hours=4)
               for b in four)
    assert all(b.end_at <= NOW for b in four)  # The unfinished bucket is dropped.
    assert max(b.high for b in four) == max(b.high for b in hourly if b.end_at <= four[-1].end_at)


def test_breakeven_is_offered_only_after_one_r_and_swing_lows_are_above_the_stop_and_1pct_below():
    lows_15m = series(96, seconds=900, base=D("105.5"), spread=D("0.2"),
                      lows={3: "103.004", 10: "105", 20: "96", 30: "94"})
    lows_1h = series(168, seconds=3600, base=D("104"), spread=D("0.5"),
                     lows={5: "101.337", 80: "99"})  # 80 hours back: outside the 72 hours.
    common = {"bars_15m": lows_15m, "bars_1h": lows_1h, "now": NOW, "bid": D("106"),
              "entry": D("100.10"), "current_stop": D("95"), "risk": D("5.10"),
              "increment": INC}
    options = cm.stop_options(best_bid=D("105.19"), **common)  # Not yet +1R: no breakeven.
    assert [(o.price, o.bases) for o in options] == [
        (D("103"), ("SWING_LOW_15M",)), (D("101.33"), ("SWING_LOW_1H",)),
        (D("96"), ("SWING_LOW_15M",)),
    ]  # 105 is less than 1% below 106 (104.94 is the bound); 94 is below the stop.
    assert [o.option_id for o in options] == ["S1", "S2", "S3"]
    options = cm.stop_options(best_bid=D("105.20"), **common)  # Exactly +1R once: breakeven.
    assert [(o.price, o.bases) for o in options] == [
        (D("103"), ("SWING_LOW_15M",)), (D("101.33"), ("SWING_LOW_1H",)),
        (D("100.10"), ("BREAKEVEN",)), (D("96"), ("SWING_LOW_15M",)),
    ]
    raised = cm.stop_options(best_bid=D("110"), **{**common, "current_stop": D("100.10")})
    assert D("100.10") not in [o.price for o in raised]  # Not above the current stop.
    many = series(96, seconds=900, base=D("105"), spread=D("0.2"),
                  lows={3: "103.9", 9: "103.5", 15: "103.1", 21: "102.7", 27: "102.3",
                        33: "101.9", 39: "101.5"})
    five = cm.stop_options(best_bid=D("110"), **{**common, "bars_15m": many, "bars_1h": []})
    assert len(five) == 5 and D("100.10") in [o.price for o in five]  # Breakeven keeps its place.
    assert [o.price for o in five] == [D("103.90"), D("103.50"), D("103.10"), D("102.70"),
                                       D("100.10")]
    assert all(cm.on_grid(o.price, INC) for o in five)


def test_targets_are_swing_highs_the_24_hour_and_7_day_highs_above_the_target_at_most_five():
    b15 = series(96, seconds=900, base=D("108"), spread=D("0.5"), highs={4: "112.345"})
    b1h = series(168, seconds=3600, base=D("108"), spread=D("0.5"),
                 highs={30: "111.2", 50: "113.3", 70: "114.1", 90: "116.6", 120: "118.05",
                        150: "117.7"})
    options = cm.target_options(bars_15m=b15, bars_1h=b1h, now=NOW, bid=D("109"),
                                current_target=D("111"), increment=INC)
    prices = [o.price for o in options]
    assert len(options) == 5 and prices == sorted(prices)
    by_price = {o.price: o.bases for o in options}
    assert by_price[D("112.35")] == ("HIGH_24H",)  # Rounded up to the increment.
    assert "HIGH_7D" in by_price[D("118.05")]
    assert D("111.20") in by_price and D("113.30") in by_price and D("114.10") in by_price
    assert D("116.60") not in by_price and D("117.70") not in by_price  # Beyond five.
    assert [o.option_id for o in options] == ["T1", "T2", "T3", "T4", "T5"]
    none = cm.target_options(bars_15m=b15, bars_1h=b1h, now=NOW, bid=D("109"),
                             current_target=D("120"), increment=INC)
    assert none == []  # Nothing above the current target.
    four_hour = series(168, seconds=3600, base=D("108"), spread=D("0.5"), highs={42: "112.9"})
    both = cm.target_options(bars_15m=[], bars_1h=four_hour, now=NOW, bid=D("109"),
                             current_target=D("111"), increment=INC)
    assert set(both[0].bases) >= {"HIGH_7D", "SWING_HIGH_1H", "SWING_HIGH_4H"}


# --- Checks before applying ----------------------------------------------------------------------

@pytest.mark.parametrize("change,code", [
    ({}, None),
    ({"answered_at": NOW - timedelta(seconds=60)}, "ANSWER_TOO_OLD"),
    ({"answered_at": NOW - timedelta(seconds=59, microseconds=999999)}, None),
    ({"new_stop": D("95")}, "STOP_NOT_ABOVE_CURRENT"),
    ({"new_stop": D("105.47")}, None),  # Exactly 0.5% below the bid (106 x 0.995) passes.
    ({"new_stop": D("105.48")}, "STOP_TOO_CLOSE_TO_BID"),
    ({"min_bid": D("103")}, "STOP_LEVEL_CROSSED"),  # The bid reached the new stop meanwhile.
    ({"new_stop": D("103.005")}, "OFF_PRICE_INCREMENT"),
    ({"new_stop": None, "new_target": D("106")}, "TARGET_NOT_ABOVE_PRICE"),
    ({"new_stop": None, "new_target": D("110.99")}, "TARGET_NOT_ABOVE_CURRENT"),
    ({"new_stop": None, "new_target": D("112"), "max_bid": D("112")}, "TARGET_LEVEL_CROSSED"),
    ({"new_stop": None, "new_target": D("112"), "max_bid": D("111.99")}, None),
])
def test_every_check_before_applying_refuses_at_its_boundary(change, code):
    values = {"old_stop": D("95"), "new_stop": D("103"), "old_target": D("111"),
              "new_target": None, "bid": D("106"), "min_bid": D("105.9"),
              "max_bid": D("106.5"), "answered_at": NOW - timedelta(seconds=5), "now": NOW,
              "increment": INC}
    values.update(change)
    assert cm.check_change(**values) == code


# --- The schedule --------------------------------------------------------------------------------

def test_the_review_schedule_bars_triggers_news_shocks_and_the_minute():
    opened = NOW - timedelta(minutes=20)
    boundary = cm.floor_time(NOW, 900)
    base = {"now": NOW, "opened_at": opened, "served_bar_end": boundary, "unserved_triggers": [],
            "news_revision": 5, "served_news_revision": 5, "shocks": [],
            "last_requested_at": NOW - timedelta(minutes=5)}
    assert cm.due_reasons(**base) == ([], False)
    assert cm.due_reasons(**{**base, "served_bar_end": boundary - timedelta(minutes=15)}) == (
        ["BAR_15M"], False)
    assert cm.due_reasons(**{**base, "served_bar_end": None,
                             "opened_at": boundary + timedelta(seconds=1)}) == ([], False)
    assert cm.due_reasons(**{**base, "news_revision": 9}) == (["AGENT_NEWS"], False)
    assert cm.due_reasons(**{**base, "shocks": [77]}) == (["BTC_SHOCK"], False)
    assert cm.due_reasons(**{**base, "unserved_triggers": [("R_MILESTONE", "1")]}) == (
        ["R_MILESTONE"], False)
    recent = {**base, "last_requested_at": NOW - timedelta(seconds=59)}
    assert cm.due_reasons(**{**recent, "unserved_triggers": [("NEAR_STOP", "95")]}) == ([], False)
    assert cm.due_reasons(**{**recent, "last_requested_at": NOW - timedelta(seconds=60),
                             "unserved_triggers": [("NEAR_STOP", "95")]}) == (["NEAR_STOP"], False)
    assert cm.due_reasons(**{**recent, "unserved_triggers": [("NEAR_TARGET", "111")]}) == (
        ["NEAR_TARGET"], True)  # Near the target the minute does not apply.


def test_many_trades_are_ordered_nearest_to_their_stop_first():
    items = [("far", D("110"), D("95")), ("near", D("100"), D("99")), ("mid", D("105"), D("100"))]
    assert cm.nearest_to_stop_first(items) == ["near", "mid", "far"]


def test_a_three_percent_bitcoin_move_within_fifteen_minutes_is_one_shock():
    window = cm.BenchmarkWindow()
    start = NOW
    window.observe(D("60000"), start)
    window.observe(D("61799"), start + timedelta(minutes=14))
    assert window.check(start + timedelta(minutes=14)) is None  # 2.998%.
    window.observe(D("61800"), start + timedelta(minutes=14, seconds=30))
    shock = window.check(start + timedelta(minutes=14, seconds=30))
    assert shock["direction"] == "UP" and D(shock["move_fraction"]) == D("0.03")
    assert window.check(start + timedelta(minutes=14, seconds=31)) is None  # Restarted.
    window.observe(D("61900"), start + timedelta(minutes=15))
    assert window.check(start + timedelta(minutes=15)) is None
    window.observe(D("60043"), start + timedelta(minutes=20))
    down = window.check(start + timedelta(minutes=20))
    assert down["direction"] == "DOWN" and down["reference_price"] == "61900"
    old = cm.BenchmarkWindow()
    old.observe(D("60000"), start)
    old.observe(D("62000"), start + timedelta(minutes=16))
    assert old.check(start + timedelta(minutes=16)) is None  # The low is 16 minutes old.
    old.observe(D("60100"), start + timedelta(minutes=17))
    old.reset()
    assert old.samples() == 0 and old.check(start + timedelta(minutes=17)) is None


# --- JEV_MANAGED_POSITION_CONTEXT_V4 and QUESTIONS_V4 --------------------------------------------

LABEL = "SYNTHETIC ENGINEERING FIXTURE, NOT A REAL OPPORTUNITY. "


def prose(topic, length):
    words = (f"{LABEL}{topic}: the listing added spot demand, hourly closes held the range low "
             "and volume expanded on advances while pullbacks came on lighter volume. ")
    return (words * (length // len(words) + 1))[:length - 1].rstrip() + "."


def pick(*, long=True):
    return {
        "kind": "BOTH", "agent_current_price": "100.50",
        "agent_price_at": (NOW - timedelta(hours=3)).isoformat(),
        "levels": {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
                   "target": "111"},
        "stated_reward_risk": "2.13",
        "thesis": prose("Thesis", 1000 if long else 120),
        "why_now": prose("Why now", 600 if long else 80),
        "why_these_levels": prose("Levels", 600 if long else 80),
        "risks": prose("Risks", 600 if long else 80),
        "disproof": prose("Invalidation", 400 if long else 60),
        "agent_confidence": "0.9", "sources": [{"excerpt": "never sent"}],
    }


def choice(chosen, options, top=0.83):
    rest = (1 - top) / (len(options) - 1)
    return {"type": "choice", "choice": chosen, "confidence": 0.8,
            "probabilities": {o: (top if o == chosen else rest) for o in options}}


def selection():
    pick_names = ("news_stale", "already_priced", "mechanism_contradicted",
                  "factual_claims_supported", "prices_consistent", "levels_supported_by_bars",
                  "setup_already_broken", "verdict")
    answers = {name: choice("NO", ["YES", "NO", INSUFFICIENT]) for name in pick_names}
    quality = {name: {"type": "score", "score": 1.7, "confidence": 0.9, "legend": {},
                      "probabilities": {"0": 0.05, "1": 0.2, "2": 0.75}}
               for name in ("evidence_support", "timing_specificity", "level_rationale",
                            "disproof_quality")}
    quality["quality_category"] = choice("STRONG", ["STRONG", "ADEQUATE", "WEAK", INSUFFICIENT])
    return {"pick_review": {"answers": answers}, "quality": {"answers": quality}}


def news(count=8):
    items = []
    for i in range(count):
        stance = "ADVERSE" if i in {2, 5} else "SUPPORTS"
        excerpt = prose(f"News {i}", 1200)
        items.append({"source_id": f"news-{i}", "url": f"https://example.org/n{i}",
                      "excerpt": excerpt, "content_hash": f"{i:064x}",
                      "published_at": (NOW - timedelta(hours=2, minutes=i)).isoformat(),
                      "retrieved_at": (NOW - timedelta(hours=1)).isoformat(),
                      "received_at": (NOW - timedelta(minutes=50 - i)).isoformat(),
                      "stance": stance, "novelty": "NEW_FACT"})
    return items


def trade():
    return {"entry": D("100.10"), "qty": D("19.6078"), "initial_stop": D("95"),
            "initial_target": D("111"), "max_entry": D("100.10"), "risk": D("5.10"),
            "stop": D("101.33"), "target": D("112.35"), "bid": D("106.02"),
            "ask": D("106.05"), "quote_at": NOW - timedelta(seconds=1),
            "opened_at": (NOW - timedelta(hours=5)).isoformat(),
            "review_at": (NOW + timedelta(hours=19)).isoformat(),
            "best_bid": D("107.4"), "worst_bid": D("99.2"), "milestone": 1}


def changes(count=10):
    return [{"at": (NOW - timedelta(minutes=200 - 15 * i)).isoformat(),
             "kind": "STOP" if i % 2 else "TARGET", "old": f"{95 + i}.00",
             "new": f"{96 + i}.00", "option": "S1", "bases": ["SWING_LOW_15M"],
             "reasons": ["BAR_15M", "R_MILESTONE"]} for i in range(count)]


def options_for(bars_15m, bars_1h, t):
    stops = cm.stop_options(bars_15m=bars_15m, bars_1h=bars_1h, now=NOW, bid=t["bid"],
                            entry=t["entry"], current_stop=D("95"), best_bid=t["best_bid"],
                            risk=t["risk"], increment=INC)
    targets = cm.target_options(bars_15m=bars_15m, bars_1h=bars_1h, now=NOW, bid=t["bid"],
                                current_target=D("111"), increment=INC)
    return {"stop": [o.record() for o in stops], "target": [o.record() for o in targets]}


def production_inputs():
    b15 = series(101, seconds=900, base=D("105"), spread=D("0.4"),
                 lows={3: "103.004", 9: "102.51", 15: "101.93", 21: "101.4", 27: "100.72"},
                 highs={5: "112.345"})
    b1h = series(173, seconds=3600, base=D("106"), spread=D("0.9"),
                 lows={6: "99.5"}, highs={30: "113.2", 50: "114.3", 70: "115.1", 120: "118.05"})
    t = trade()
    btc15 = series(101, seconds=900, base=D("61000"), spread=D("40"), symbol="BTC/USD")
    btc1h = series(173, seconds=3600, base=D("60500"), spread=D("200"), symbol="BTC/USD")
    return {"now": NOW, "symbol": "SOL/USD", "pick": pick(), "selection": selection(),
            "trade": t, "changes": changes(), "bars_15m": b15, "bars_1h": b1h,
            "btc_15m": btc15, "btc_1h": btc1h, "news": news(),
            "options": options_for(b15, b1h, t),
            "trigger": {"reasons": ["BAR_15M", "R_MILESTONE", "AGENT_NEWS"],
                        "priority_rank": 1}}


def test_the_v4_context_fits_the_budget_at_production_size_and_never_drops_an_option():
    inputs = production_inputs()
    assert len(inputs["options"]["stop"]) == 5 and len(inputs["options"]["target"]) == 5
    compiled = compile_maintenance_state(**inputs)
    size = encoded_bytes(compiled.state)
    assert size <= STATE_BYTE_BUDGET < 12_000
    assert compiled.manifest["within_budget"] and compiled.manifest["state_bytes"] == size
    state = compiled.state
    assert state["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V4"
    assert [o["price"] for o in state["options"]["stop"]] == [
        o["price"] for o in inputs["options"]["stop"]]
    assert [o["price"] for o in state["options"]["target"]] == [
        o["price"] for o in inputs["options"]["target"]]
    assert state["original_pick"]["thesis"] == inputs["pick"]["thesis"]  # Never shortened.
    assert state["original_pick"]["disproof"] == inputs["pick"]["disproof"]
    text = encoded(state)
    for secret in ("agent_confidence", "never sent", "signal_id"):
        assert secret not in text
    assert {n["ref"] for n in state["news_since_entry"]} <= {"N1", "N2", "N3", "N4"}
    assert all(n["stance"] == "ADVERSE" for n in state["news_since_entry"][:2])
    assert compiled.manifest["budget_steps"]  # The ladder ran at this size.
    trade_state = state["trade"]
    assert (trade_state["pnl_r"], trade_state["best_r"], trade_state["worst_r"]) == (
        "1.16", "1.43", "-0.18")
    assert trade_state["minutes_in_trade"] == 300
    assert set(state["price_action"]["vs_btc"]) == {"1h", "4h", "24h", "since_entry"}


def test_a_small_context_is_sent_whole_and_is_byte_identical_across_runs():
    inputs = {**production_inputs(), "pick": pick(long=False), "news": news(2),
              "changes": changes(2)}
    first, second = compile_maintenance_state(**inputs), compile_maintenance_state(**inputs)
    assert encoded(first.state) == encoded(second.state)
    assert first.manifest["budget_steps"] == []
    assert len(first.state["price_action"]["bars_15m"]["rows"]) == 16
    assert len(first.state["price_action"]["bars_1h"]["rows"]) == 24


def test_an_unsatisfiable_budget_skips_the_review_instead_of_cutting_further():
    with pytest.raises(ContextBudgetUnsatisfiable) as caught:
        compile_maintenance_state(budget=4_000, **production_inputs())
    assert caught.value.state_bytes > 4_000 and caught.value.manifest["within_budget"] is False


def context_for(options):
    state = compile_maintenance_state(**{**production_inputs(), "options": options}).state
    data = {"context_version": "JEV_MANAGED_POSITION_CONTEXT_V4", "identity": {},
            "state": state, "options": options, "expires_at": NOW.isoformat()}
    raw = encoded(data)
    from catalyst_lab.jev_contract import digest

    return ManagedContext(raw, digest(raw))


def test_the_v4_questions_offer_exactly_the_code_options_with_their_distances():
    inputs = production_inputs()
    context = context_for(inputs["options"])
    questions = maintenance_questions(context)
    assert questions.version == "JEV_MANAGED_POSITION_QUESTIONS_V4"
    assert questions.stage == "TRACKING"
    body = questions.questions
    assert set(body) == {"trade_reason", "action", "stop_option", "target_option"}
    assert set(body["action"]["criteria"]) == {"HOLD", "RAISE_STOP", "RAISE_TARGET",
                                              "RAISE_STOP_AND_TARGET", "FLAG_EARLY_EXIT",
                                              INSUFFICIENT}
    assert set(body["trade_reason"]["criteria"]) == {"INTACT", "WEAKENED", "BROKEN",
                                                    INSUFFICIENT}
    stop_ids = [o["option_id"] for o in inputs["options"]["stop"]]
    assert set(body["stop_option"]["criteria"]) == {"KEEP", INSUFFICIENT, *stop_ids}
    first = inputs["options"]["stop"][0]
    assert body["stop_option"]["criteria"]["S1"].startswith(first["price"] + ": ")
    assert "% below the bid" in body["stop_option"]["criteria"]["S1"]
    assert "R from entry" in body["target_option"]["criteria"]["T1"]
    assert maintenance_questions(context).template_hash == questions.template_hash


def answers(action, reason="INTACT", stop="KEEP", target="KEEP", *, tie=None):
    stops, targets = ["KEEP", "S1", "S2", INSUFFICIENT], ["KEEP", "T1", INSUFFICIENT]
    result = {
        "trade_reason": choice(reason, ["INTACT", "WEAKENED", "BROKEN", INSUFFICIENT]),
        "action": choice(action, list(cm.ACTIONS) + [INSUFFICIENT]),
        "stop_option": choice(stop, stops), "target_option": choice(target, targets),
    }
    if tie:
        result[tie]["probabilities"] = {k: 0.0 for k in result[tie]["probabilities"]}
        first, second = list(result[tie]["probabilities"])[:2]
        result[tie]["probabilities"][first] = result[tie]["probabilities"][second] = 0.5
        result[tie]["choice"] = first
    return result


OPTIONS = {"stop": [{"option_id": "S1"}, {"option_id": "S2"}], "target": [{"option_id": "T1"}]}


@pytest.mark.parametrize("given,code", [
    (answers("HOLD"), None),
    (answers("RAISE_STOP", stop="S2"), None),
    (answers("RAISE_TARGET", target="T1"), None),
    (answers("RAISE_STOP_AND_TARGET", stop="S1", target="T1"), None),
    (answers("FLAG_EARLY_EXIT", reason="BROKEN"), None),
    (answers("FLAG_EARLY_EXIT", reason="WEAKENED"), "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    (answers("HOLD", reason="BROKEN"), "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    (answers("RAISE_STOP"), "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    (answers("HOLD", stop="S1"), "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    (answers("FLAG_EARLY_EXIT", reason="BROKEN", stop="S1"),
     "CONTRADICTORY_MANAGEMENT_ANSWERS"),
    (answers(INSUFFICIENT), "UNCERTAIN_JUDGMENT"),
    (answers("RAISE_STOP", stop="S1", tie="stop_option"), "UNCERTAIN_JUDGMENT"),
])
def test_jevs_answer_is_read_by_the_consistency_rules(given, code):
    answer = read_answer(given, OPTIONS)
    assert answer.code == code
    if code is None:
        assert answer.changes == (answer.action in {"RAISE_STOP", "RAISE_TARGET",
                                                    "RAISE_STOP_AND_TARGET"})
        assert answer.flagged == (answer.action == "FLAG_EARLY_EXIT")
    else:
        assert not answer.changes and not answer.flagged


def test_an_option_the_context_never_offered_is_refused():
    assert read_answer(answers("RAISE_STOP", stop="S2"),
                       {"stop": [{"option_id": "S1"}], "target": []}).code == "UNKNOWN_OPTION"
