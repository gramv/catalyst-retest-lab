from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab.domain import Candidate
from catalyst_lab.market import Observation, Session
from catalyst_lab.trigger import TriggerPolicy, advance
from tests.conftest import NOW


@pytest.fixture
def engine(raw):
    candidate = Candidate.model_validate(raw)
    session = Session(NOW.date(), NOW.replace(hour=9, minute=30), NOW.replace(hour=16))
    policy = TriggerPolicy()
    context = {"state": "VALIDATED"}

    def step(kind=None, *, seconds=0, healthy=True, **fields):
        nonlocal context
        now = NOW + timedelta(seconds=seconds)
        obs = Observation(kind, raw["ticker"], now.isoformat(), "iex", **fields) if kind else None
        context, reason = advance(candidate, session, context, obs, now, healthy, policy)
        return context, reason

    return step


def test_exact_touch_confirms_with_limit_at_max(engine):
    assert engine()[0]["state"] == "WATCHING"
    assert engine("quote", bid=D("99.99"), ask=D("100.01"))[0]["state"] == "WATCHING"
    state, _ = engine("trade", seconds=1, price=D("100"), trade_id="1")
    assert state["state"] == "TRIGGER_CONFIRMED"
    assert state["entry_intent"] == {
        "type": "limit",
        "side": "buy",
        "limit_price": "100.15",
        "stop": "99.00",
        "target": "102.45",
        "time_in_force": "day",
        "order_class": "bracket",
    }


@pytest.mark.parametrize("price", ["99", "98.99"])
def test_stop_print_precedes_touch_and_chase_invalidation(engine, price):
    engine("quote", bid=D("100.20"), ask=D("100.21"))
    context, reason = engine("trade", seconds=1, price=D(price))
    assert context["state"] == "INVALIDATED" and reason == "STOP_TRADED_BEFORE_TRIGGER"


@pytest.mark.parametrize("kind", ["quote", "bar", "bar_update"])
def test_quote_or_bar_cannot_touch(engine, kind):
    engine("quote", bid=D("99.99"), ask=D("100.01"))
    fields = {"bid": D("99.01"), "ask": D("99.02")} if kind == "quote" else {"price": D("99.02")}
    context, _ = engine(kind, seconds=1, **fields)
    assert context["state"] == "WATCHING" and not context["touched"]


@pytest.mark.parametrize(
    ("ask", "state"), [("100.15", "TRIGGER_CONFIRMED"), ("100.1501", "INVALIDATED")]
)
def test_max_entry_boundary(engine, ask, state):
    engine("quote", bid=D(ask) - D(".01"), ask=D(ask))
    context, reason = engine("trade", seconds=1, price=D("100"))
    assert context["state"] == state
    if state == "INVALIDATED":
        assert reason == "PRICE_BEYOND_MAX_ENTRY"


@pytest.mark.parametrize(
    ("bid", "ask", "state"),
    [("99.95", "100.05", "TRIGGER_CONFIRMED"), ("99.949", "100.051", "WATCHING")],
)
def test_spread_is_basis_points_not_ratio(engine, bid, ask, state):
    engine("quote", bid=D(bid), ask=D(ask))
    assert engine("trade", seconds=1, price=D("100"))[0]["state"] == state


def test_wide_spread_defers_chase_decision_until_other_conditions_hold(engine):
    engine("quote", bid=D("99"), ask=D("101"))
    assert engine("trade", seconds=1, price=D("100"))[0]["state"] == "WATCHING"
    context, reason = engine("quote", seconds=2, bid=D("100.20"), ask=D("100.21"))
    assert context["state"] == "INVALIDATED" and reason == "PRICE_BEYOND_MAX_ENTRY"


@pytest.mark.parametrize(("seconds", "state"), [(5, "TRIGGER_CONFIRMED"), (5.001, "WATCHING")])
def test_quote_freshness_boundary(engine, seconds, state):
    engine("quote", bid=D("99.99"), ask=D("100.01"))
    assert engine("trade", seconds=seconds, price=D("100"))[0]["state"] == state


def test_feed_failure_cannot_confirm_and_late_recovery_cannot_erase_gap(engine):
    engine("quote", bid=D("99.99"), ask=D("100.01"))
    assert engine("trade", seconds=1, price=D("100"), healthy=False)[0]["state"] == "WATCHING"
    context, reason = engine("quote", seconds=6, bid=D("99.99"), ask=D("100.01"))
    assert context["state"] == "INVALIDATED" and reason == "DATA_FEED_FAILURE"


def test_stalled_worker_invalidates_even_if_next_packet_is_fresh(engine):
    engine("quote", bid=D("99.99"), ask=D("100.01"))
    context, reason = engine("quote", seconds=11, bid=D("99.99"), ask=D("100.01"))
    assert context["state"] == "INVALIDATED" and reason == "DATA_FEED_FAILURE"


def test_touch_waits_for_fresh_quote_without_new_touch(engine):
    engine("quote", bid=D("99.99"), ask=D("100.01"))
    assert engine("trade", seconds=6, price=D("100"))[0]["state"] == "WATCHING"
    assert (
        engine("quote", seconds=7, bid=D("99.99"), ask=D("100.01"))[0]["state"]
        == "TRIGGER_CONFIRMED"
    )


def test_stop_after_unconfirmed_touch_invalidates(engine):
    engine("quote", bid=D("99"), ask=D("101"))
    engine("trade", seconds=1, price=D("100"))
    assert engine("trade", seconds=2, price=D("99"))[1] == "STOP_TRADED_BEFORE_TRIGGER"


def test_same_nanosecond_distinct_stop_print_is_not_discarded(engine):
    engine("quote", bid=D("99"), ask=D("101"))
    engine("trade", seconds=1, price=D("100.01"), trade_id="1")
    assert (
        engine("trade", seconds=1, price=D("99"), trade_id="2")[1] == "STOP_TRADED_BEFORE_TRIGGER"
    )


def test_premarket_no_watch_and_early_close_expires(raw):
    candidate = Candidate.model_validate(raw)
    session = Session(NOW.date(), NOW.replace(hour=9, minute=30), NOW.replace(hour=13))
    assert session.flatten_time == NOW.replace(hour=12, minute=55)
    context, _ = advance(
        candidate, session, {"state": "VALIDATED"}, None, NOW.replace(hour=9), True, TriggerPolicy()
    )
    assert context["state"] == "VALIDATED"
    context, _ = advance(
        candidate, session, context, None, NOW.replace(hour=13), True, TriggerPolicy()
    )
    assert context["state"] == "EXPIRED_UNTRIGGERED"


def test_candidate_expiration_wins_over_a_touch(raw):
    candidate = Candidate.model_validate(raw | {"expires_at": NOW + timedelta(seconds=1)})
    session = Session(NOW.date(), NOW.replace(hour=9), NOW.replace(hour=16))
    context, _ = advance(
        candidate, session, {"state": "VALIDATED"}, None, NOW, True, TriggerPolicy()
    )
    obs = Observation(
        "trade", raw["ticker"], (NOW + timedelta(seconds=1)).isoformat(), "iex", price=D("100")
    )
    context, _ = advance(
        candidate, session, context, obs, NOW + timedelta(seconds=1), True, TriggerPolicy()
    )
    assert context["state"] == "EXPIRED_UNTRIGGERED"


@pytest.mark.parametrize("offset", [-1, 1])
def test_pre_watch_and_future_prints_do_not_trigger(raw, offset):
    candidate = Candidate.model_validate(raw)
    session = Session(NOW.date(), NOW.replace(hour=9), NOW.replace(hour=16))
    quote = Observation(
        "quote", raw["ticker"], NOW.isoformat(), "iex", bid=D("99.99"), ask=D("100.01")
    )
    context, _ = advance(
        candidate, session, {"state": "VALIDATED"}, quote, NOW, True, TriggerPolicy()
    )
    obs = replace(
        quote,
        kind="trade",
        price=D("99"),
        provider_timestamp=(NOW + timedelta(seconds=offset)).isoformat(),
    )
    context, _ = advance(candidate, session, context, obs, NOW, True, TriggerPolicy())
    assert context["state"] == "WATCHING" and not context["touched"]


def test_fresh_out_of_order_stop_is_not_hidden(raw):
    candidate = Candidate.model_validate(raw)
    session = Session(NOW.date(), NOW.replace(hour=9), NOW.replace(hour=16))
    context, _ = advance(
        candidate, session, {"state": "VALIDATED"}, None, NOW, True, TriggerPolicy()
    )
    trade = Observation(
        "trade", raw["ticker"], (NOW + timedelta(seconds=2)).isoformat(), "iex", price=D("100.5")
    )
    context, _ = advance(
        candidate, session, context, trade, NOW + timedelta(seconds=2), True, TriggerPolicy()
    )
    older = replace(
        trade, provider_timestamp=(NOW + timedelta(seconds=1)).isoformat(), price=D("99")
    )
    context, reason = advance(
        candidate, session, context, older, NOW + timedelta(seconds=3), True, TriggerPolicy()
    )
    assert context["state"] == "INVALIDATED" and reason == "STOP_TRADED_BEFORE_TRIGGER"


def test_older_quote_cannot_replace_newest_ask(raw):
    candidate = Candidate.model_validate(raw)
    session = Session(NOW.date(), NOW.replace(hour=9), NOW.replace(hour=16))
    context, _ = advance(
        candidate, session, {"state": "VALIDATED"}, None, NOW, True, TriggerPolicy()
    )
    latest = Observation(
        "quote",
        raw["ticker"],
        (NOW + timedelta(seconds=2)).isoformat(),
        "iex",
        bid=D("100.2"),
        ask=D("100.21"),
    )
    context, _ = advance(
        candidate, session, context, latest, NOW + timedelta(seconds=2), True, TriggerPolicy()
    )
    older = replace(
        latest,
        provider_timestamp=(NOW + timedelta(seconds=1)).isoformat(),
        bid=D("99.99"),
        ask=D("100.01"),
    )
    context, _ = advance(
        candidate, session, context, older, NOW + timedelta(seconds=3), True, TriggerPolicy()
    )
    touch = Observation(
        "trade", raw["ticker"], (NOW + timedelta(seconds=3)).isoformat(), "iex", price=D("100")
    )
    context, reason = advance(
        candidate, session, context, touch, NOW + timedelta(seconds=3), True, TriggerPolicy()
    )
    assert reason == "PRICE_BEYOND_MAX_ENTRY"
