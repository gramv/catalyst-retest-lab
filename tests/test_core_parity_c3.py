"""Package plugin-c3, item 5: the remaining live touch and stop/target checks on the shared core.

The Coinbase reference trigger's touches and stop invalidation (``coinbase_trigger``), the
stop-breach marks (``stop_breach`` V2/V3 print and bid evidence, ``stop_execution``'s print run,
the engine's V1 bid mark, the partial entry's stop) and the protection planner's target touch
(``crypto_execution``) now call ``strategies.core`` (``touches_entry``, ``reaches_stop``,
``reaches_target``). Behaviour is byte-identical: the predicates are the operators the modules
used, an independent restatement of the pre-core Coinbase rule agrees on random cases, and every
existing test of those modules passes unchanged. Pure; no database, broker or network.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import coinbase_trigger, crypto_maintenance, stop_breach, stop_execution
from catalyst_lab.strategies import core

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ADMITTED = NOW - timedelta(minutes=30)
LEVELS = {"entry_trigger": D("100"), "max_entry_price": D("100.10"), "stop": D("95")}


def test_the_core_predicates_are_the_operators_the_live_modules_used():
    rng = random.Random(31)
    values = [D(v) for v in ("94.99", "95", "95.01", "99.99", "100", "100.01", "111", "0.0001")]
    values += [D(rng.randint(1, 20000)) / 100 for _ in range(200)]
    for price in values:
        for level in values[:8]:
            assert core.touches_entry(price, level) is (price <= level)
            assert core.reaches_stop(price, level) is (price <= level)
            assert core.reaches_target(price, level) is (price >= level)
            assert (not core.reaches_stop(price, level)) is (price > level)


def pre_core_reference_outcome(levels, facts, now, admitted_at):
    """``reference_verdict``'s decision as written before package plugin-c3 (inline operators),
    restated independently on the same parsed evidence."""
    t, s = levels["entry_trigger"], levels["stop"]
    low = coinbase_trigger._reference_print(facts.get("low_print"))
    touch = coinbase_trigger._reference_print(facts.get("touch_print"))
    bid, ask, _, _, fresh, _ = coinbase_trigger._reference_quote(facts, now)
    if low is not None and admitted_at <= low[1] <= now and low[0] <= s:
        return "INVALIDATE", "STOP_TRADED_BEFORE_TRIGGER"
    if fresh and bid <= s:
        return "INVALIDATE", "STOP_QUOTED_BEFORE_TRIGGER"
    if facts.get("healthy") is not True:
        return "WAIT", "COINBASE_FEED_UNHEALTHY"
    print_touch = (touch is not None and touch[0] <= t and touch[1] >= admitted_at
                   and 0 <= (now - touch[1]).total_seconds()
                   <= coinbase_trigger.REFERENCE_PRINT_MAX_AGE_SECONDS)
    if not print_touch and not (fresh and ask <= t):
        return "NO_TOUCH", None
    return "TOUCH", None


def random_reference(rng):
    def price():
        return str(rng.choice(["94.9", "95", "95.1", "99.9", "100", "100.1", "100.5"]))

    def print_at():
        return (NOW - timedelta(seconds=rng.choice([0, 1, 5, 6, 3600]))).isoformat()

    facts = {"healthy": rng.random() < 0.8, "product_id": "SOL-USD"}
    if rng.random() < 0.7:
        bid = D(price())
        facts.update(bid=str(bid), ask=str(bid + D(rng.choice(["0", "0.1", "0.6"]))),
                     quote_at=print_at(), quote_received_at=print_at())
    for name in ("low_print", "touch_print"):
        if rng.random() < 0.6:
            at = print_at()
            facts[name] = {"price": price(), "at": at, "received_at": at, "trade_id": "1"}
    return facts


def test_the_coinbase_reference_verdict_matches_the_pre_core_rule():
    rng = random.Random(2026_10_03)
    seen = set()
    for _ in range(2000):
        facts = random_reference(rng)
        verdict = coinbase_trigger.reference_verdict(LEVELS, facts, now=NOW, admitted_at=ADMITTED)
        expected = pre_core_reference_outcome(LEVELS, facts, NOW, ADMITTED)
        assert (verdict.outcome, verdict.reason) == expected, facts
        seen.add(expected)
    assert len(seen) == 5  # Every branch was exercised.


def test_the_partial_entry_stop_matches_the_operator():
    rng = random.Random(7)
    opened = NOW - timedelta(minutes=1)
    for _ in range(300):
        bid = D(rng.randint(9400, 9600)) / 100
        reason = crypto_maintenance.partial_entry_cancel_reason(
            opened_at=opened, now=NOW, bid=bid, ask=D("100"), max_entry=D("100.10"),
            stop=D("95"))
        assert (reason == crypto_maintenance.PARTIAL_ENTRY_AT_OR_BELOW_STOP) is (bid <= D("95"))


class Routed(Exception):
    """Raised by a spy: proof that a module asked the core."""


@pytest.mark.parametrize("name", ["touches_entry", "reaches_stop", "reaches_target"])
def test_each_moved_check_asks_the_core(monkeypatch, name):
    calls = []

    def spy(price, level):
        calls.append((price, level))
        raise Routed(name)

    monkeypatch.setattr(core, name, spy)
    at = NOW.isoformat()
    if name in {"touches_entry", "reaches_stop"}:
        facts = {"healthy": True, "bid": "99", "ask": "99.1", "quote_at": at,
                 "quote_received_at": at}
        with pytest.raises(Routed):  # The Coinbase reference trigger.
            coinbase_trigger.reference_verdict(LEVELS, facts, now=NOW, admitted_at=ADMITTED)
    if name == "reaches_stop":
        state = {"stop": "95", "stop_breach_marks": {"stop": "95", "stop_since": at,
                                                     "bid_since": None, "bid": None,
                                                     "bid_quote_at": None}}
        with pytest.raises(Routed):  # CRYPTO_STOP_BREACH_V2's bid evidence.
            stop_breach.evaluate(state, observation={}, bid=D("94"), now=NOW)
        with pytest.raises(Routed):  # Its print evidence.
            stop_breach._print_evidence(state["stop_breach_marks"], {
                "feed_healthy": True, "trade_price": "94", "trade_at": at}, D("95"), NOW)

        class View:
            prints = (type("P", (), {"price": D("94"), "traded_at": NOW, "trade_id": "1"})(),)

        with pytest.raises(Routed):  # CRYPTO_STOP_EXECUTION_V1's Coinbase print run.
            stop_execution.reference_confirmation(View(), {"stop_since": at}, D("95"), NOW)
        with pytest.raises(Routed):  # The partial entry's stop.
            crypto_maintenance.partial_entry_cancel_reason(
                opened_at=NOW, now=NOW, bid=D("94"), ask=D("94.1"), max_entry=D("100"),
                stop=D("95"))
    if name == "reaches_target":
        from tests.test_crypto_execution import plan

        with pytest.raises(Routed):  # The protection planner's target touch.
            plan()
    assert calls


def test_the_planner_target_touch_is_inclusive_as_before():
    from tests.test_crypto_execution import plan, snap

    assert plan(snap(bid=D(120))).reason == "TARGET_EXIT"  # At the target: touched.
    assert plan(snap(bid=D("119.99"))).reason != "TARGET_EXIT"
