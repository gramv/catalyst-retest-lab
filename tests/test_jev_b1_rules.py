"""CRYPTO_MAINTENANCE_V5's pure rules (package jev-b1): the V6 questions and their pinned
template hashes, the policy record, MAINTENANCE_ANSWER_RULE_V3 and every streak transition, the
news bookkeeping, the cadence, the code-checked disproof parts, the observed buckets and the
CONTEXT_V6 compiler with its 3,072-byte budget.

Pure functions only: no database, broker, provider or network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import maintenance_v5 as mv5
from catalyst_lab.jev_contract import INSUFFICIENT, digest, encoded
from catalyst_lab.maintenance_dossier import review_history_row
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable, encoded_bytes
from tests.maintenance_fixtures import bars_series

NOW = datetime(2026, 10, 3, 14, 7, 30, tzinfo=UTC)
OPENED = datetime(2026, 10, 3, 9, 40, 10, tzinfo=UTC)

# --- The questions ----------------------------------------------------------------------------


def test_the_v6_questions_are_two_nouls_with_pinned_texts_and_no_action_or_price_choice():
    both = mv5.question_set(mv5.QUESTIONS)
    one = mv5.question_set((mv5.INVALIDATION_MET,))
    # The pins (also in maintenance_v5, checked at import): a changed text is a new version.
    assert both.template_hash == (
        "d7917e3bdff30049e540939643bff244ef5e9fa257407fab8672ebcb44baca57")
    assert one.template_hash == (
        "f829c00df3137423d8dd7669c8e3780070e97919d29bf220d99fdc319d2bd410")
    assert (both.version, both.stage) == ("JEV_MANAGED_POSITION_QUESTIONS_V6", "TRACKING")
    questions = both.questions
    assert list(questions) == ["invalidation_met", "news_contradicts"]
    assert questions["invalidation_met"]["instructions"].startswith(
        "Has the condition stated in `pick.disproof` happened, according to `observed`?")
    assert questions["news_contradicts"]["instructions"].startswith(
        "Does an item in `news_new` report a fact that contradicts `pick.thesis` or "
        "`pick.why_now`?")
    for question in questions.values():
        assert question["type"] == "noul" and set(question["criteria"]) == {"true", "false"}
        text = encoded(question)
        # One judgment each: no action, level, option or price to choose, no Insufficient.
        for word in ("RAISE", "HOLD", "FLAG", "option", "target", "Choose", "select",
                     INSUFFICIENT):
            assert word not in text, word
        # No default bias: the false criterion is the true one negated, word for word.
        true, false = question["criteria"]["true"], question["criteria"]["false"]
        assert false in (true.replace("has happened", "has not happened"),
                         true.replace("An item", "No item"))
    assert one.questions == {"invalidation_met": questions["invalidation_met"]}
    with pytest.raises(ValueError, match="V6_QUESTION_NAMES_INVALID"):
        mv5.question_set((mv5.NEWS_CONTRADICTS,))  # Invalidation is always asked.
    assert mv5.asked_for({"news_new": []}) == ("invalidation_met",)
    assert mv5.asked_for({"news_new": [{"id": "N1"}]}) == ("invalidation_met",
                                                           "news_contradicts")


# --- The record -------------------------------------------------------------------------------


def test_the_v5_record_is_v4s_numbers_plus_its_own_and_round_trips_exactly():
    v4, v5 = cm.CRYPTO_MAINTENANCE_V4.record(), cm.CRYPTO_MAINTENANCE_V5.record()
    assert v5 == {**v4, "policy_id": "CRYPTO_MAINTENANCE_V5",
                  "context_version": "JEV_MANAGED_POSITION_CONTEXT_V6",
                  "question_version": "JEV_MANAGED_POSITION_QUESTIONS_V6",
                  "answer_rule": "MAINTENANCE_ANSWER_RULE_V3", "review_bar_seconds": 900,
                  "hour_bar_seconds": 3600, "confirm_bar_seconds": 300, "confirm_bars": 2,
                  "btc_move_fraction": "0.03", "yes_at_or_above": "0.80",
                  "no_at_or_below": "0.20", "confirm_yes": 3, "news_max_asks": 3,
                  "state_byte_budget": 3072, "invalidation_if_unanswered": "EXIT",
                  "news_if_unanswered": "KEEP"}
    policy = cm.policy_from_record(v5)
    assert policy == cm.CRYPTO_MAINTENANCE_V5 and cm.is_v5(policy)
    assert cm.ADMITTED_MAINTENANCE is cm.CRYPTO_MAINTENANCE_V5
    assert not any(cm.is_v5(p) for p in (cm.CRYPTO_MAINTENANCE, cm.CRYPTO_MAINTENANCE_V2,
                                          cm.CRYPTO_MAINTENANCE_V3, cm.CRYPTO_MAINTENANCE_V4))
    # The raise guards stay (the 24-hour/window review's continue applies them) and the spend
    # guard still governs (EXHAUSTED withholds V5 reviews).
    assert cm.raise_guards(policy, hourly_range=D(1), last_raise_at=None) is not None
    assert cm.guarded(policy)
    for broken in ({**v5, "confirm_yes": 2}, {**v5, "yes_at_or_above": "0.7"},
                   {**v5, "invalidation_if_unanswered": "KEEP"},
                   {k: v for k, v in v5.items() if k != "news_max_asks"},
                   {**v5, "extra": 1}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
            cm.policy_from_record(broken)
    # Earlier records still read back as themselves.
    assert cm.policy_from_record(v4) == cm.CRYPTO_MAINTENANCE_V4
    assert "JEV_MANAGED_POSITION_CONTEXT_V6" in cm.CONTEXT_VERSIONS


# --- MAINTENANCE_ANSWER_RULE_V3 ---------------------------------------------------------------


@pytest.mark.parametrize("p,expected", [
    (1, "YES"), (0.99, "YES"), (0.8, "YES"), (0.79, "UNCERTAIN"), (0.5, "UNCERTAIN"),
    (0.21, "UNCERTAIN"), (0.2, "NO"), (0.05, "NO"), (0, "NO")])
def test_the_thresholds_are_inclusive_on_the_printed_probability(p, expected):
    assert mv5.verdict(p) == expected


def test_the_reader_needs_exactly_the_asked_nouls():
    asked = ("invalidation_met", "news_contradicts")
    good = {"invalidation_met": {"type": "noul", "noul": 0.83},
            "news_contradicts": {"type": "noul", "noul": 0.2}}
    answer = mv5.read_answer(good, asked)
    assert answer.code is None and answer.answer_rule == "MAINTENANCE_ANSWER_RULE_V3"
    assert answer.verdicts == {"invalidation_met": {"p": "0.83", "verdict": "YES"},
                               "news_contradicts": {"p": "0.2", "verdict": "NO"}}
    for broken in ({"invalidation_met": good["invalidation_met"]},  # One missing.
                   {**good, "extra": {"type": "noul", "noul": 0.5}},
                   {**good, "invalidation_met": {"type": "choice", "choice": "YES"}},
                   {**good, "invalidation_met": {"type": "noul", "noul": 1.2}},
                   {**good, "invalidation_met": {"type": "noul", "noul": "0.9"}},
                   None):
        assert mv5.read_answer(broken, asked).code == "INVALID_MANAGEMENT_ANSWER"


# --- Streaks ----------------------------------------------------------------------------------

B = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)


def bar(n):
    """The n-th completed 5-minute bar after B."""
    return (B + timedelta(minutes=5 * n)).isoformat()


def run(answers):
    """Fold ``(verdict, bar, hash)`` answers; the effects in order and the final streak."""
    streak, effects = mv5.Streak(), []
    for answer, at, state_hash in answers:
        streak, effect = mv5.step(streak, answer, bar_end=at, state_hash=state_hash)
        effects.append(effect)
    return effects, streak


def test_three_yeses_on_three_new_bars_confirm_and_the_streak_starts_again():
    effects, streak = run([("YES", bar(0), "h0"), ("YES", bar(1), "h1"), ("YES", bar(2), "h2")])
    assert effects == ["COUNTED", "COUNTED", "CONFIRMED"]
    assert streak == mv5.Streak(0, bar(2), "h2")
    effects, streak = run([("YES", bar(0), "h0"), ("YES", bar(1), "h1"), ("YES", bar(2), "h2"),
                           ("YES", bar(3), "h3")])
    assert effects[-1] == "COUNTED" and streak.count == 1


def test_a_retry_or_a_same_bar_re_ask_never_counts():
    effects, streak = run([("YES", bar(0), "h0"), ("YES", bar(0), "h0"),  # Retry.
                           ("YES", bar(0), "h0b"),  # Same bar, new facts (e.g. news).
                           ("YES", bar(1), "h1")])
    assert effects == ["COUNTED", "NOT_COUNTED_SAME_BAR", "NOT_COUNTED_SAME_BAR", "COUNTED"]
    assert streak.count == 2
    # An answer on an earlier bar than the streak's last (out of order) does not count either.
    effects, _ = run([("YES", bar(2), "h2"), ("YES", bar(1), "h1")])
    assert effects == ["COUNTED", "NOT_COUNTED_SAME_BAR"]


def test_a_repeat_of_the_same_context_on_a_new_bar_does_not_count():
    effects, streak = run([("YES", bar(0), "h"), ("YES", bar(1), "h"), ("YES", bar(2), "h2")])
    assert effects == ["COUNTED", "NOT_COUNTED_SAME_CONTEXT", "COUNTED"]
    assert streak.count == 2


def test_no_resets_uncertain_neither_adds_nor_resets():
    effects, streak = run([("YES", bar(0), "h0"), ("YES", bar(1), "h1"), ("NO", bar(2), "h2")])
    assert effects == ["COUNTED", "COUNTED", "RESET"] and streak == mv5.Streak(0, bar(2), "h2")
    # A YES on the bar of the NO that reset the streak is a same-bar re-ask.
    effects, streak = run([("NO", bar(0), "h0"), ("YES", bar(0), "h0b"), ("YES", bar(1), "h1")])
    assert effects == ["RESET", "NOT_COUNTED_SAME_BAR", "COUNTED"] and streak.count == 1
    effects, streak = run([("YES", bar(0), "h0"), ("UNCERTAIN", bar(1), "h1"),
                           ("YES", bar(2), "h2"), ("UNCERTAIN", bar(3), "h3"),
                           ("YES", bar(4), "h4")])
    assert effects == ["COUNTED", "NEUTRAL", "COUNTED", "NEUTRAL", "CONFIRMED"]
    effects, streak = run([("UNCERTAIN", bar(0), "h0"), ("NO", bar(1), "h1")])
    assert effects == ["NEUTRAL", "RESET"] and streak.count == 0


def decision(verdicts, n, *, outcome="HELD", news=(), state_hash=None):
    return {"verdicts": {q: {"verdict": v} for q, v in verdicts.items()},
            "asked_bar_end": bar(n), "state_hash": state_hash or f"h{n}",
            "news_shown": list(news), "outcome": outcome}


def test_the_history_folds_answered_decisions_only_and_tracks_unsettled_news():
    history = mv5.fold([
        decision({"invalidation_met": "YES"}, 0),
        {"verdicts": None, "outcome": "FAILED"},  # No answer: nothing.
        decision({"invalidation_met": "YES"}, 1, outcome="DISCARDED"),  # Trade gone: nothing.
        decision({"invalidation_met": "YES", "news_contradicts": "YES"}, 2, news=("Na", "Nb")),
        decision({"invalidation_met": "UNCERTAIN", "news_contradicts": "NO"}, 3, news=("Nb",)),
    ])
    assert history.streaks["invalidation_met"].count == 2
    assert history.streaks["news_contradicts"] == mv5.Streak(0, bar(3), "h3")
    assert history.confirm_from() == datetime.fromisoformat(bar(2))
    # Nb was answered NO: settled. Na was asked once: still unsettled.
    assert history.unsettled("Na") and not history.unsettled("Nb") and history.unsettled("Nc")
    # Three asks settle an item whatever the answers (three asks are what a confirmation needs).
    history = mv5.fold([decision({"invalidation_met": "NO", "news_contradicts": v}, n,
                                 news=("Nx",)) for n, v in enumerate(
                                     ("UNCERTAIN", "YES", "UNCERTAIN"))])
    assert history.news_asks == {"Nx": 3} and not history.unsettled("Nx")
    # A confirmation settles the items it judged.
    history = mv5.fold([decision({"invalidation_met": "NO", "news_contradicts": "YES"}, n,
                                 news=("Ny",) if n < 2 else ("Ny", "Nz")) for n in range(3)])
    assert not history.unsettled("Ny") and not history.unsettled("Nz")
    assert history.confirm_from() is None  # Every streak is back at zero.


# --- Cadence ----------------------------------------------------------------------------------


def due(now, *, served=None, news=False, btc=False, confirm=None, last=None):
    return mv5.due_reasons(now=now, opened_at=OPENED, served=served or {}, news_changed=news,
                           btc_move=btc, confirm_from=confirm, last_requested_at=last)


def test_a_routine_review_at_every_completed_15_minute_bar_and_at_once_on_an_hour():
    at = datetime(2026, 10, 3, 9, 45, 1, tzinfo=UTC)
    assert due(OPENED + timedelta(seconds=30)) == []  # The open bar: nothing completed since.
    assert due(at) == ["BAR_15M"]
    served = mv5.served_marks(at)
    assert served == {"bar_end": "2026-10-03T09:45:00+00:00",
                      "bar_1h_end": "2026-10-03T09:00:00+00:00",
                      "bar_5m_end": "2026-10-03T09:45:00+00:00"}
    assert due(at + timedelta(minutes=5), served=served) == []  # No per-minute or 5-minute.
    assert due(at + timedelta(minutes=10), served=served) == []
    hour = datetime(2026, 10, 3, 10, 0, 2, tzinfo=UTC)
    assert due(hour, served=served) == ["BAR_15M", "BAR_1H"]
    assert due(hour + timedelta(minutes=3), served=mv5.served_marks(hour)) == []


def test_news_and_a_bitcoin_move_review_at_once_but_never_inside_a_minute():
    at = datetime(2026, 10, 3, 9, 52, 0, tzinfo=UTC)
    served = mv5.served_marks(datetime(2026, 10, 3, 9, 45, 1, tzinfo=UTC))
    assert due(at, served=served, news=True) == ["AGENT_NEWS"]
    assert due(at, served=served, btc=True) == ["BTC_MOVE"]
    assert due(at, served=served, news=True, last=at - timedelta(seconds=59)) == []
    assert due(at, served=served, news=True, last=at - timedelta(seconds=60)) == ["AGENT_NEWS"]
    # 3% of Bitcoin's price at the trade's last review, either way.
    assert not mv5.btc_moved(D("61799.99"), D("60000"))
    assert mv5.btc_moved(D("61800"), D("60000")) and mv5.btc_moved(D("58200"), D("60000"))
    assert not mv5.btc_moved(D("58200.01"), D("60000"))
    assert not mv5.btc_moved(None, D("60000")) and not mv5.btc_moved(D("60000"), None)


def test_confirm_mode_re_asks_on_the_next_two_completed_5_minute_bars_only():
    yes_at = datetime(2026, 10, 3, 10, 15, 0, tzinfo=UTC)  # A counted yes on the 10:15 bar.
    served = mv5.served_marks(yes_at + timedelta(seconds=1))
    first = yes_at + timedelta(minutes=5, seconds=1)
    assert due(first, served=served, confirm=yes_at) == ["CONFIRM_5M"]
    assert due(first + timedelta(minutes=1), served=mv5.served_marks(first),
               confirm=yes_at) == []  # That bar is served.
    second = yes_at + timedelta(minutes=10, seconds=1)
    assert due(second, served=mv5.served_marks(first), confirm=yes_at) == ["CONFIRM_5M"]
    third = yes_at + timedelta(minutes=15, seconds=1)  # The next 15-minute bar, not confirm.
    assert due(third, served=mv5.served_marks(second), confirm=yes_at) == ["BAR_15M"]
    assert due(yes_at + timedelta(minutes=20, seconds=1), served=mv5.served_marks(third),
               confirm=yes_at) == []
    assert due(first, served=served, confirm=None) == []  # No running streak: no confirm.


# --- Code-checked parts of the disproof -------------------------------------------------------


def test_prices_in_the_disproof_are_found_and_timeframes_percentages_and_multiples_are_not():
    text = ("A 15-minute close below 0.412, or a 3% drop, or 2x volume selling, or losing "
            "1.5R, or no reclaim of $0.45 within 4h, or a 1-hour close under 0.398 and 0.412.")
    assert mv5.disproof_levels(text, D("0.43")) == [D("0.412"), D("0.45"), D("0.398")]
    assert mv5.disproof_levels("an hourly close below 95.", D("100")) == [D("95")]
    assert mv5.disproof_levels("a close below $1,234.50 on the daily", D("1300")) == [
        D("1234.50")]
    # Out of 0.5-1.5x the entry trigger: not a price of this coin (a count, a date, BTC).
    assert mv5.disproof_levels("BTC under 60000 or 2 closes below 40", D("100")) == []
    assert mv5.disproof_levels("below 1, 2, 3, 4 or 5 dollars", D("3")) == [
        D("2"), D("3"), D("4")]  # 1 and 5 are outside 1.5-4.5.
    assert mv5.disproof_levels("levels 90 91 92 93 94 95", D("100")) == [
        D("90"), D("91"), D("92"), D("93")]  # At most four.
    assert mv5.disproof_levels(None, D("100")) == []
    assert mv5.disproof_hours(text) == [D("4")]  # "15-minute" and "1-hour close" are bars.
    assert mv5.disproof_hours("no new high within 6 hours, or flat for 2 days") == [
        D("6"), D("48")]
    assert mv5.disproof_hours("a 4h candle close below 90 or the 1h low") == []


def checks(**overrides):
    values = {"levels": [D("95")], "hours": [D("6")], "last_15m": D("94.9"),
              "last_1h": D("95"), "lows_since_entry": None, "minutes_in_trade": 360}
    values.update(overrides)
    return mv5.disproof_checks(**values)


def test_code_states_its_result_for_each_condition_it_can_check():
    assert checks() == [
        "last completed 15-minute close below 95: yes",
        "last completed 1-hour close below 95: no",  # At the level is not below it.
        "a 15-minute low below 95 since entry: unknown",
        "in the trade for at least 6 hours: yes",
    ]
    assert checks(minutes_in_trade=359)[-1] == "in the trade for at least 6 hours: no"


# --- Observed buckets -------------------------------------------------------------------------


@pytest.mark.parametrize("value,label", [
    (D("-1"), "LOSS_0.75R_OR_MORE"), (D("-0.75"), "LOSS_0.75R_OR_MORE"),
    (D("-0.74"), "LOSS_0.25R_TO_0.75R"), (D("-0.25"), "LOSS_0.25R_TO_0.75R"),
    (D("-0.24"), "NEAR_ENTRY"), (D("0.24"), "NEAR_ENTRY"), (D("0.25"), "GAIN_0.25R_TO_0.75R"),
    (D("0.75"), "GAIN_0.75R_TO_1.5R"), (D("1.49"), "GAIN_0.75R_TO_1.5R"),
    (D("1.5"), "GAIN_1.5R_OR_MORE"), (None, "UNKNOWN")])
def test_price_against_entry_is_bucketed_in_r(value, label):
    assert mv5.bucket(value, mv5.R_BUCKETS, mv5.R_TOP) == label


def test_the_other_buckets():
    assert [mv5.bucket(D(v), mv5.BTC_BUCKETS, mv5.BTC_TOP) for v in
            ("-2", "-1.99", "-0.5", "0", "0.5", "2")] == [
        "SHARP_DROP", "DOWN", "DOWN", "FLAT", "UP", "SHARP_UP"]
    assert [mv5.bucket(D(v), mv5.VS_BTC_BUCKETS, mv5.VS_BTC_TOP) for v in
            ("-3", "-1", "0.99", "1", "3")] == [
        "MUCH_WEAKER", "WEAKER", "IN_LINE", "STRONGER", "MUCH_STRONGER"]
    assert [mv5.bucket(m, mv5.TIME_BUCKETS, mv5.TIME_TOP) for m in (59, 60, 239, 720, 1440)] == [
        "UNDER_1H", "1H_TO_4H", "1H_TO_4H", "12H_TO_24H", "24H_OR_MORE"]
    assert [mv5.bucket(D(v), mv5.RANGE_BUCKETS, mv5.RANGE_TOP) for v in
            ("0.49", "0.5", "1.99", "2", "3")] == [
        "UNDER_0.5_HOURLY_RANGES", "0.5_TO_1_HOURLY_RANGES", "1_TO_2_HOURLY_RANGES",
        "2_TO_3_HOURLY_RANGES", "3_HOURLY_RANGES_OR_MORE"]


# --- The CONTEXT_V6 compiler ------------------------------------------------------------------

PICK = {"thesis": "T" * 1000, "why_now": "W" * 600,
        "disproof": "An hourly close below 95, or no move within 6 hours." + " " * 347,
        "why_these_levels": "never sent", "risks": "never sent", "agent_current_price": "100.5",
        "sources": [{"excerpt": "never sent"}], "levels": {"stop": "95"}}
LEVELS = {"entry_trigger": D("100"), "max_entry_price": D("100.10"), "stop": D("95"),
          "target": D("111")}


def news_item(n, *, stance="SUPPORTS", chars=600):
    text = f"Synthetic fixture news item {n}. " + "x" * chars
    return {"content_hash": digest(text), "source_id": f"src-{n}", "excerpt": text[:chars],
            "url": "https://example.org/n", "stance": stance,
            "received_at": (NOW - timedelta(minutes=20 * n)).isoformat()}


def compiled(now=NOW, news=(), pick=PICK, bid=D("101"), **overrides):
    values = dict(
        now=now, symbol="SOL/USD", pick=pick, plan_levels=LEVELS, stop=D("95"),
        entry=D("100.10"), risk=D("5.10"), bid=bid, opened_at=OPENED,
        bars_15m=bars_series(now, 96, seconds=900, base=D("100.5")),
        bars_1h=bars_series(now, 48, seconds=3600, base=D("100.5"), spread=D("1")),
        btc_15m=bars_series(now, 96, seconds=900, base=D("61000"), symbol="BTC/USD"),
        btc_1h=bars_series(now, 48, seconds=3600, base=D("61000"), symbol="BTC/USD"),
        hourly_range=D("2"), news=list(news))
    values.update(overrides)
    return mv5.compile_state(**values)


def test_the_state_is_the_pick_verbatim_observed_words_and_new_news_only():
    result = compiled(news=[news_item(1, stance="ADVERSE")])
    state = result.state
    assert set(state) == {"context_version", "symbol", "as_of_bar_end", "pick", "observed",
                          "news_new"}
    assert state["context_version"] == "JEV_MANAGED_POSITION_CONTEXT_V6"
    assert state["as_of_bar_end"] == "2026-10-03T14:05:00+00:00"  # The completed 5-minute bar.
    assert state["pick"] == {"thesis": PICK["thesis"], "why_now": PICK["why_now"],
                             "disproof": PICK["disproof"],
                             "levels": {"entry_trigger": "100", "max_entry_price": "100.1",
                                        "stop": "95", "target": "111"}}
    observed = state["observed"]
    assert observed["price_vs_entry"] == "NEAR_ENTRY"  # (101 - 100.10) / 5.10 = 0.18R.
    assert observed["stop"] == "AT_PLAN_STOP"
    assert observed["last_15m_close_vs_stop"] == "ABOVE"
    assert observed["distance_to_stop"] == "3_HOURLY_RANGES_OR_MORE"  # (101 - 95) / 2 = 3.
    assert observed["btc_last_1h"] == "FLAT" and observed["coin_vs_btc_since_entry"] == "IN_LINE"
    assert observed["time_in_trade"] == "4H_TO_12H"
    assert observed["disproof_checks"] == [
        "last completed 15-minute close below 95: no",
        "last completed 1-hour close below 95: no",
        "a 15-minute low below 95 since entry: no",
        "in the trade for at least 6 hours: no",
    ]
    [item] = state["news_new"]
    assert item["id"] == result.news_shown[0] and item["id"].startswith("N")
    assert (item["stance"], item["received"], item["host"]) == ("ADVERSE", "15M_TO_1H",
                                                                "example.org")
    # No raw bars, earlier answers, selection answers or extra pick fields anywhere.
    text = encoded(state)
    for absent in ("never sent", "bars_15m", "selection", "review_history", "agent_current",
                   "open", "volume", "\"high\""):
        assert absent not in text, absent
    assert result.state_hash == digest(encoded(state))
    assert encoded_bytes(state) <= 3072 and result.manifest["within_budget"]
    assert compiled(news=[news_item(1, stance="ADVERSE")]) == result  # Deterministic.


def test_the_hash_changes_with_the_bar_and_a_disproof_level_breach_is_stated():
    a = compiled()
    later = compiled(now=NOW + timedelta(minutes=5))
    assert a.state["as_of_bar_end"] != later.state["as_of_bar_end"]
    assert a.state_hash != later.state_hash
    assert compiled(now=NOW + timedelta(seconds=40)).state_hash == a.state_hash  # Same bar.
    # A 15-minute bar closing under the disproof's level: code says so, Jev judges the rest.
    low = compiled(bars_15m=bars_series(NOW, 96, seconds=900, base=D("94.5"), spread=D("0.1")),
                   bid=D("96"))
    assert low.state["observed"]["disproof_checks"][0] == (
        "last completed 15-minute close below 95: yes")
    assert low.state["observed"]["last_15m_close_vs_stop"] == "BELOW"
    assert low.state["observed"]["price_vs_entry"] == "LOSS_0.75R_OR_MORE"


def test_the_budget_ladder_shortens_and_drops_news_but_never_the_pick():
    items = [news_item(n, stance="ADVERSE" if n == 0 else "SUPPORTS") for n in range(6)]
    result = compiled(news=items)
    assert encoded_bytes(result.state) <= 3072
    state = result.state
    assert state["pick"]["thesis"] == PICK["thesis"] and state["pick"]["why_now"] == PICK[
        "why_now"] and state["pick"]["disproof"] == PICK["disproof"]
    steps = result.manifest["budget_steps"]
    assert steps[:2] == ["REDUCE_NEWS_EXCERPTS", "OMIT_NEWS_ITEM"]
    # The adverse item comes first and is kept; dropped items were not shown (stay unsettled).
    assert state["news_new"][0]["stance"] == "ADVERSE"
    assert len(result.news_shown) < len(items)
    omitted = [n for n in result.manifest["news"] if n["status"] == "OMITTED"]
    assert omitted and all(n["id"] not in result.news_shown for n in omitted)
    assert all(len(n["excerpt"]) <= 150 for n in state["news_new"])


def test_a_pick_that_cannot_fit_is_skipped_never_shortened():
    huge = {**PICK, "thesis": "\u00e9" * 1000}  # Non-ASCII: 6 bytes a character as sent.
    with pytest.raises(ContextBudgetUnsatisfiable) as caught:
        compiled(pick=huge)
    assert caught.value.budget == 3072 and caught.value.state_bytes > 3072


def test_without_news_and_with_short_texts_the_state_is_small():
    pick = {"thesis": "THESIS-00: the listing adds a new source of spot demand.",
            "why_now": "WHY-NOW: the notice is three hours old.",
            "disproof": "INVALIDATION-00: an hourly close below 95."}
    result = compiled(pick=pick)
    assert result.state["news_new"] == [] and result.news_shown == ()
    assert encoded_bytes(result.state) < 1500


# --- The 24-hour/window review's history shows V5 answers -------------------------------------


def test_a_v5_review_in_the_day_reviews_history_shows_its_verdicts_and_older_rows_are_unchanged():
    now = NOW
    old = {"requested_at": (now - timedelta(minutes=5)).isoformat(),
           "trigger_reasons": ["BAR_1M"], "outcome": "HELD", "code": None,
           "answers": {"action": {"choice": "HOLD", "top_p": "0.71"}}, "option_prices": {}}
    assert review_history_row(old, now) == {
        "minutes_ago": 5, "trigger_reasons": ["BAR_1M"], "outcome": "HELD", "code": None,
        "action": {"choice": "HOLD", "top_p": "0.71"}, "trade_reason": None,
        "stop_option": None, "target_option": None}
    v5 = {**old, "answers": {"invalidation_met": {"p": "0.9", "verdict": "YES"}}}
    row = review_history_row(v5, now)
    assert row["invalidation_met"] == {"p": "0.9", "verdict": "YES"}
    assert row["action"] is None and "news_contradicts" not in row
