"""TOPK_REPLACEMENT_V1 (package replacement, plan phase 3b) and its visibility fixes.

A top-K pick declined at admission is replaced by Jev's next-ranked pick in the decline's own
ledger transaction; the research context shows each pick's true status and the ranking; the
setups list keeps a report-V3 setup's entry type and system check.

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue, a
scripted mock Jev transport, a fixture universe and Alpaca market data behind an httpx
MockTransport. No broker, provider, network or owner-ledger contact.
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from catalyst_lab import research_selection_topk as topk
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_engineering import ENGINEERING_SELECTION_POLICY
from catalyst_lab.managed_runtime import (
    PERMANENT_ADMISSION_REFUSALS,
    TOPK_PERMANENT_ADMISSION_REFUSALS,
    ManagedRuntime,
    decline_selection,
    engineering_runtime_policy,
    permanent_refusal,
    replaces_on_decline,
)
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from tests.test_agent_research_session import TOPK_SCRIPT, session_script, topk_report
from tests.test_agent_research_session import make_session as make_session
from tests.test_agent_research_session import submit as submit_to_session
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_managed_runtime import reviewed_cycle
from tests.test_research_context import Feeds, context_client, service_for
from tests.test_research_report_v3 import (
    AGENT,
    SCHEDULE,
    STATUS,
    bearer,
    cycle_of,
    pick,
    report_v3,
    v3_intake,
)
from tests.test_selection_b1 import b1_selection as b1_selection
from tests.test_selection_b1 import classify, review_failure
from tests.test_selection_b2 import b2_selection as b2_selection
from tests.test_selection_topk import ranking_of, topk_cycle
from tests.test_selection_topk import run as publish
from tests.test_system_check import CREDENTIALS, HOURLY, publish_v3, two_slots, v3_pick
from tests.test_system_check import market as market

TOPK = topk.TOPK_POLICY
K = 5
K5 = topk.TopKRule(TOPK, K)
# Eight ranked picks with strictly falling QUALITY_V3 scores (``levels`` here are the four
# score levels; rank n is An), one vetoed pick and one whose review failed. Every pick has the
# fixture prices: entry 100, max entry 100.10, stop 95, target 111, agent's price 100.50.
LADDER = {
    "A1/USD": {"levels": (2, 2, 2, 2)},
    "A2/USD": {"levels": (2, 2, 2, 1)},
    "A3/USD": {"levels": (2, 2, 1, 1)},
    "A4/USD": {"levels": (2, 1, 1, 1)},
    "A5/USD": {"levels": (1, 1, 1, 1)},
    "A6/USD": {"levels": (1, 1, 1, 0)},
    "A7/USD": {"levels": (1, 1, 0, 0)},
    "A8/USD": {"levels": (1, 0, 0, 0)},
    "V1/USD": {"labels": {"news_stale": "YES"}},
    "N1/USD": {"kind": "NEWS", "plan": (500,)},
}
TOP = ["A1/USD", "A2/USD", "A3/USD", "A4/USD", "A5/USD"]
PASSING = ("100.49", "100.51")  # Mid 100.50, the agent's price: entry 100 is a PULLBACK.
MISMATCH = ("110.00", "110.02")  # Mid 9.5% above the agent's price: PRICE_MISMATCH.
OFF_GRID = {"entry_trigger": "100.005", "max_entry_price": "100.10", "stop": "95",
            "target": "111"}  # Entry off Alpaca's 0.01 increment.
SUPERSEDED = "SUPERSEDED_BY_NEW_RESEARCH"


def key(symbol):
    return "CRYPTO:" + symbol


def submit(cycle, script, now, *, agent_id=AGENT, run_slot=None, picks=None):
    """One report V3 whose picks are ``script``'s symbols in order; ``picks`` overrides fields
    of a symbol's pick; a ``run_slot`` uses the hourly fixture schedule (30-minute validity)."""
    values = [pick(i, symbol, kind=spec.get("kind", "BOTH"), now=now,
                   **(picks or {}).get(symbol, {}))
              for i, (symbol, spec) in enumerate(script.items())]
    fields, schedule = {}, SCHEDULE
    if run_slot is not None:
        fields = {"run_slot": run_slot.isoformat(),
                  "valid_until": (now + timedelta(minutes=30)).isoformat()}
        schedule = HOURLY
    raw = report_v3(values, now=now, agent_id=agent_id, **fields)
    result = cycle.start_report(raw, max_seconds=86400,
                                v3=v3_intake(universe=set(script), schedule=schedule, now=now))
    assert result["rejected_count"] == 0
    return cycle_of(raw)


def ladder(mx, *, run_slot=None, picks=None):
    """The LADDER report under top-K with K = 5, reviewed, ranked and published."""
    engine, venue, _ = mx
    cycle, _ = topk_cycle(mx, LADDER, rule=K5)
    cycle_id = submit(cycle, LADDER, venue.now, run_slot=run_slot, picks=picks)
    publish(cycle, cycle_id)
    for symbol in LADDER:
        classify(engine, symbol)
    return cycle, cycle_id


def single(mx, symbol, *, agent_id, run_slot=None):
    """Another agent's one-pick top-K report, published."""
    engine, venue, _ = mx
    cycle, _ = topk_cycle(mx, {symbol: {}}, rule=K5)
    cycle_id = submit(cycle, {symbol: {}}, venue.now, agent_id=agent_id, run_slot=run_slot)
    publish(cycle, cycle_id)
    return cycle_id


def events(engine, kind, cycle_id=None):
    with engine.repo.connect() as conn:
        rows = conn.execute("SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (kind,)).fetchall()
    return [r for r in rows if cycle_id is None or r["body"].get("cycle_id") == cycle_id]


def replacements(engine, cycle_id=None):
    return [r["body"] for r in events(engine, "RESEARCH_REPLACEMENT", cycle_id)]


def selected(engine, cycle_id):
    """The cycle's published picks by symbol, as the runtime offers them for admission."""
    return {r["body"]["packet"]["symbol"]: {**r["body"]["packet"],
                                            "selection_event_seq": r["event_seq"]}
            for r in events(engine, "RESEARCH_SELECTED", cycle_id)}


def live(engine, cycle_id, now):
    """The cycle's live picks: published, not declined and not expired (its run is current)."""
    declined = {r["body"]["selection_event_seq"]
                for r in events(engine, "RESEARCH_ADMISSION_DECLINED")}
    return sorted(symbol for symbol, packet in selected(engine, cycle_id).items()
                  if packet["selection_event_seq"] not in declined
                  and now < datetime.fromisoformat(packet["review_valid_until"]))


def runtime_for(mx, market, research, symbols):
    """A ready runtime over the fixture venue whose research cycle is ``research``."""
    engine, venue, _ = mx
    runtime = ManagedRuntime(engine, research, market.source, CREDENTIALS,
                             engineering_runtime_policy(), clock=lambda: venue.now,
                             reviewer_heartbeat=lambda: True)
    runtime.connected = runtime.research_healthy = True
    assert runtime.reconcile_once()
    runtime.market_connected["CRYPTO"] = True
    runtime.market_subscriptions["CRYPTO"] = set(symbols)
    assert runtime.ready()
    return runtime


def quote(runtime, venue, prices):
    """Fresh stream quotes, as the runtime's crypto stream delivers them."""
    for symbol, (bid, ask) in prices.items():
        runtime.market_message("CRYPTO", {"T": "q", "S": symbol, "bp": bid, "ap": ask,
                                          "t": venue.now.isoformat()})


def refuse(runtime, packet, code="PRICE_MISMATCH"):
    """The runtime's own handling of an admission refusal: audit, decline, replacement."""
    runtime._admission_refused(packet, ValueError(code))


# --- Replacement through the runtime --------------------------------------------------------------

def test_a_declined_rank_two_pick_is_replaced_by_rank_k_plus_one_with_its_decline(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    first = selected(engine, cycle_id)
    assert sorted(first) == TOP and [first[s]["rank"] for s in TOP] == [1, 2, 3, 4, 5]
    runtime = runtime_for(mx, market, cycle, list(LADDER))
    quote(runtime, venue, {s: MISMATCH if s == "A2/USD" else PASSING for s in TOP})
    runtime.execution_once()
    assert {s["symbol"] for s in engine.store.active()} == {"A1/USD", "A3/USD", "A4/USD",
                                                             "A5/USD"}
    [decline] = events(engine, "RESEARCH_ADMISSION_DECLINED")
    [replacement] = events(engine, "RESEARCH_REPLACEMENT")
    assert (decline["body"]["reason"], decline["body"]["system_check"]["code"]) == (
        "PRICE_MISMATCH", "PRICE_MISMATCH")
    a6 = selected(engine, cycle_id)["A6/USD"]
    ranking = ranking_of(engine, cycle_id)
    assert replacement["idempotency_key"] == f"research:{cycle_id}:CRYPTO:A2/USD:1:replacement"
    assert replacement["body"] == {
        "cycle_id": cycle_id, "replacement_rule": "TOPK_REPLACEMENT_V1",
        "declined_item_key": key("A2/USD"), "declined_revision": 1, "declined_symbol": "A2/USD",
        "declined_rank": 2, "declined_code": "PRICE_MISMATCH",
        "declined_selection_event_seq": first["A2/USD"]["selection_event_seq"],
        "decline_event_seq": decline["event_seq"], "outcome": "PUBLISHED", "code": None,
        "replacement_item_key": key("A6/USD"), "replacement_revision": 1,
        "replacement_symbol": "A6/USD", "replacement_rank": 6,
        "replacement_selection_event_seq": a6["selection_event_seq"], "passed_over": [],
        "ranking_event_seq": ranking["event_seq"], "k": K,
        "run_slot": ranking["body"]["run_slot"]}
    # One transaction: the decline, the replacement's selection, the decision, in that order.
    assert decline["event_seq"] < a6["selection_event_seq"] < replacement["event_seq"]
    assert (a6["rank"], a6["replacement_for"], a6["k"], a6["agent_rank"]) == (
        6, key("A2/USD"), K, 6)
    assert review_failure(engine, a6) is None  # Admission SQL binds it to the ranking.
    assert live(engine, cycle_id, venue.now) == ["A1/USD", "A3/USD", "A4/USD", "A5/USD",
                                                 "A6/USD"]
    assert runtime.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def test_the_replacement_is_admitted_on_a_later_tick_to_an_authorized_entry(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    runtime = runtime_for(mx, market, cycle, ["A2/USD", "A6/USD"])
    quote(runtime, venue, {"A2/USD": MISMATCH, "A6/USD": PASSING})
    runtime.execution_once()  # A2 declined and replaced; A6 is offered from the next tick.
    assert engine.store.active() == []
    runtime.execution_once()  # The normal path: admission SQL, the price grid, the check.
    [setup] = engine.store.active()
    a6 = selected(engine, cycle_id)["A6/USD"]
    assert (setup["symbol"], str(setup["receipt_id"])) == ("A6/USD", a6["receipt_id"])
    assert setup["record_json"]["replacement_for"] == key("A2/USD")
    assert setup["state"]["entry_type"] == "PULLBACK"
    assert setup["state"]["system_check"]["result"] == "PASSED"
    risk = engine.observe_trigger(setup["setup_id"], observation(
        mx, trade_price="100", bid="99.99", ask="100.01"))
    assert risk["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == "A6/USD")
    assert D(entry["limit_price"]) == D("100.10")
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_chain_of_failures_walks_further_down_the_ranking(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    runtime = runtime_for(mx, market, cycle, ["A2/USD", "A6/USD", "A7/USD"])
    quote(runtime, venue, {"A2/USD": MISMATCH, "A6/USD": MISMATCH, "A7/USD": PASSING})
    for _ in range(3):
        runtime.execution_once()  # A2 -> A6; A6 -> A7; A7 admitted.
    first, second = replacements(engine, cycle_id)
    assert (first["declined_symbol"], first["replacement_symbol"], first["replacement_rank"]) == (
        "A2/USD", "A6/USD", 6)
    assert (second["declined_symbol"], second["declined_rank"], second["replacement_symbol"],
            second["replacement_rank"]) == ("A6/USD", 6, "A7/USD", 7)
    chain = selected(engine, cycle_id)
    assert (chain["A6/USD"]["replacement_for"], chain["A7/USD"]["replacement_for"]) == (
        key("A2/USD"), key("A6/USD"))
    assert [s["symbol"] for s in engine.store.active()] == ["A7/USD"]
    assert live(engine, cycle_id, venue.now) == ["A1/USD", "A3/USD", "A4/USD", "A5/USD",
                                                 "A7/USD"]


def test_skipped_and_duplicate_entries_are_passed_over_then_the_ranking_is_exhausted(mx, market):
    engine, venue, _ = mx
    # Another agent's report for the same run selected A3 first: this cycle skips A3 at
    # publication (recorded) and never walks it.
    single(mx, "A3/USD", agent_id="instinct")
    cycle, cycle_id = ladder(mx)
    assert sorted(selected(engine, cycle_id)) == ["A1/USD", "A2/USD", "A4/USD", "A5/USD",
                                                  "A6/USD"]
    [skip] = events(engine, "RESEARCH_SELECTION_SKIPPED")
    assert (skip["body"]["item_key"], skip["body"]["reason"]) == (
        key("A3/USD"), "DUPLICATE_SYMBOL_IN_RUN")
    # A third agent's report for the run selects A7 after this cycle published.
    single(mx, "A7/USD", agent_id="grogbot")
    runtime = runtime_for(mx, market, cycle, [])
    refuse(runtime, selected(engine, cycle_id)["A2/USD"])
    [first] = replacements(engine, cycle_id)
    assert first["passed_over"] == [{"item_key": key("A7/USD"), "rank": 7, "symbol": "A7/USD",
                                     "code": "DUPLICATE_SYMBOL_IN_RUN"}]
    assert (first["outcome"], first["replacement_symbol"], first["replacement_rank"]) == (
        "PUBLISHED", "A8/USD", 8)
    # The next decline finds nothing left: A3 skipped, A7 a duplicate, A8 taken.
    refuse(runtime, selected(engine, cycle_id)["A4/USD"])
    second = replacements(engine, cycle_id)[1]
    assert {k: second[k] for k in (
        "declined_symbol", "declined_code", "outcome", "code", "replacement_item_key",
        "replacement_revision", "replacement_symbol", "replacement_rank",
        "replacement_selection_event_seq", "passed_over")} == {
        "declined_symbol": "A4/USD", "declined_code": "PRICE_MISMATCH", "outcome": "EXHAUSTED",
        "code": "TOPK_RANKING_EXHAUSTED", "replacement_item_key": None,
        "replacement_revision": None, "replacement_symbol": None, "replacement_rank": None,
        "replacement_selection_event_seq": None,
        "passed_over": [{"item_key": key("A7/USD"), "rank": 7, "symbol": "A7/USD",
                         "code": "DUPLICATE_SYMBOL_IN_RUN"}]}
    # Recorded once per declined pick; the walk writes no skip record of its own.
    refuse(runtime, selected(engine, cycle_id)["A4/USD"])
    assert len(replacements(engine, cycle_id)) == 2
    assert len(events(engine, "RESEARCH_SELECTION_SKIPPED")) == 1
    assert sorted(selected(engine, cycle_id)) == ["A1/USD", "A2/USD", "A4/USD", "A5/USD",
                                                  "A6/USD", "A8/USD"]
    assert live(engine, cycle_id, venue.now) == ["A1/USD", "A5/USD", "A6/USD", "A8/USD"]


def test_an_expired_candidate_is_passed_over(mx, market):
    engine, venue, _ = mx
    soon = (venue.now + timedelta(minutes=10)).isoformat()
    cycle, cycle_id = ladder(mx, picks={"A6/USD": {"valid_until": soon}})
    runtime = runtime_for(mx, market, cycle, [])
    venue.now += timedelta(minutes=11)  # A6's own validity has passed; the others' has not.
    refuse(runtime, selected(engine, cycle_id)["A2/USD"])
    [replacement] = replacements(engine, cycle_id)
    assert replacement["passed_over"] == [{"item_key": key("A6/USD"), "rank": 6,
                                           "symbol": "A6/USD", "code": "REVIEW_EXPIRED"}]
    assert (replacement["replacement_symbol"], replacement["replacement_rank"]) == ("A7/USD", 7)


def test_no_replacement_for_a_superseded_run_or_after_a_newer_run(mx, market):
    engine, venue, _ = mx
    old, new = two_slots(venue.now)
    cycle, cycle_id = ladder(mx, run_slot=old)
    runtime = runtime_for(mx, market, cycle, [])
    single(mx, "B1/USD", agent_id="instinct", run_slot=new)  # The next run's shortlist.
    # A permanent refusal of the older run's pick after that: declined, never replaced.
    refuse(runtime, selected(engine, cycle_id)["A2/USD"])
    [declined] = events(engine, "RESEARCH_ADMISSION_DECLINED")
    assert declined["body"]["reason"] == "PRICE_MISMATCH"
    assert replacements(engine) == [] and sorted(selected(engine, cycle_id)) == TOP
    # The supersession pass declines the rest of the older run; none is replaced either.
    runtime._retire_superseded_research()
    reasons = {r["body"]["item_key"]: r["body"]["reason"]
               for r in events(engine, "RESEARCH_ADMISSION_DECLINED")}
    assert reasons == {key(s): "PRICE_MISMATCH" if s == "A2/USD" else SUPERSEDED for s in TOP}
    assert replacements(engine) == [] and sorted(selected(engine, cycle_id)) == TOP
    assert not replaces_on_decline(selected(engine, cycle_id)["A1/USD"], SUPERSEDED, cycle)
    with engine.store.transaction() as conn:  # The research side decides nothing either.
        for row in events(engine, "RESEARCH_ADMISSION_DECLINED"):
            assert cycle.replace_declined(conn, row) is None


def test_no_replacement_once_the_cycles_picks_have_expired(mx, market):
    engine, venue, _ = mx
    soon = (venue.now + timedelta(minutes=10)).isoformat()
    cycle, cycle_id = ladder(mx, picks={"A2/USD": {"valid_until": soon}})
    runtime = runtime_for(mx, market, cycle, [])
    picks = selected(engine, cycle_id)
    venue.now += timedelta(minutes=11)  # The declined pick itself has expired.
    refuse(runtime, picks["A2/USD"], "INVALID_OR_EXPIRED_SETUP")
    assert replacements(engine) == [] and sorted(selected(engine, cycle_id)) == TOP
    venue.now += timedelta(hours=21)  # Every pick of the cycle has expired.
    refuse(runtime, picks["A3/USD"])
    assert replacements(engine) == [] and sorted(selected(engine, cycle_id)) == TOP
    assert len(events(engine, "RESEARCH_ADMISSION_DECLINED")) == 2


def test_one_replacement_per_declined_pick_across_ticks_and_a_restart(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    runtime = runtime_for(mx, market, cycle, ["A2/USD"])
    quote(runtime, venue, {"A2/USD": MISMATCH})
    for _ in range(3):
        runtime.execution_once()

    def counts():
        return {kind: len(events(engine, kind)) for kind in (
            "RESEARCH_ADMISSION_DECLINED", "RESEARCH_REPLACEMENT", "RESEARCH_SELECTED",
            "RUNTIME_REPLACEMENT_FAULT")}

    once = counts()
    assert once == {"RESEARCH_ADMISSION_DECLINED": 1, "RESEARCH_REPLACEMENT": 1,
                    "RESEARCH_SELECTED": 6, "RUNTIME_REPLACEMENT_FAULT": 0}
    # A restart: a new runtime (a new runtime ID) over the same ledger, its ticks, the research
    # pass's publication, and the same decline offered again directly.
    restarted = runtime_for(mx, market, cycle, ["A2/USD"])
    assert restarted.runtime_id != runtime.runtime_id
    quote(restarted, venue, {"A2/USD": MISMATCH})
    for _ in range(2):
        restarted.execution_once()
    publish(cycle, cycle_id)
    a2 = selected(engine, cycle_id)["A2/USD"]
    assert restarted._decline(a2, a2["selection_event_seq"], "PRICE_MISMATCH") is True
    decline, again = decline_selection(
        engine.store, cycle, {"reason": "IGNORED_WHEN_RECORDED"},
        f"research:admission-declined:{a2['selection_event_seq']}")
    assert counts() == once
    [recorded] = events(engine, "RESEARCH_REPLACEMENT")
    assert again["event_seq"] == recorded["event_seq"]
    assert decline["body"]["reason"] == "PRICE_MISMATCH"  # The recorded decline decides.
    assert live(engine, cycle_id, venue.now) == ["A1/USD", "A3/USD", "A4/USD", "A5/USD",
                                                 "A6/USD"]


def test_concurrent_declines_never_exceed_k_live_picks(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    runtime = runtime_for(mx, market, cycle, [])
    picks = selected(engine, cycle_id)
    targets = [picks["A1/USD"], picks["A2/USD"], picks["A3/USD"], picks["A1/USD"]]
    barrier, errors = threading.Barrier(len(targets)), []

    def decline(packet):
        try:
            barrier.wait()
            refuse(runtime, packet)
        except Exception as exc:  # Surfaced below.
            errors.append(exc)

    threads = [threading.Thread(target=decline, args=(packet,)) for packet in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and runtime.error is None
    made = replacements(engine, cycle_id)
    # One decision per declined pick; serialized by the shared lock, each saw the earlier ones.
    assert sorted(r["declined_symbol"] for r in made) == ["A1/USD", "A2/USD", "A3/USD"]
    assert [r["replacement_rank"] for r in made] == [6, 7, 8]
    assert len(events(engine, "RESEARCH_ADMISSION_DECLINED")) == 3
    assert live(engine, cycle_id, venue.now) == ["A4/USD", "A5/USD", "A6/USD", "A7/USD",
                                                 "A8/USD"]
    refuse(runtime, picks["A4/USD"])  # Nothing is left to publish: four live picks remain.
    assert replacements(engine, cycle_id)[-1]["outcome"] == "EXHAUSTED"
    assert live(engine, cycle_id, venue.now) == ["A5/USD", "A6/USD", "A7/USD", "A8/USD"]


def test_a_refused_replacement_decision_writes_nothing_and_is_retried(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    # This runtime's research was started with another cycle policy: it cannot decide.
    changed = ResearchCycle(engine.repo, cycle.reviewer,
                            CyclePolicy(10, 10, 15, 60, 30, review_validity_seconds=601),
                            clock=lambda: venue.now, selection=K5)
    runtime = runtime_for(mx, market, changed, ["A2/USD"])
    quote(runtime, venue, {"A2/USD": MISMATCH})
    runtime.execution_once()
    runtime.execution_once()
    a2 = selected(engine, cycle_id)["A2/USD"]
    [fault] = events(engine, "RUNTIME_REPLACEMENT_FAULT")
    assert fault["body"] == {"runtime_id": runtime.runtime_id, "cycle_id": cycle_id,
                             "item_key": key("A2/USD"),
                             "selection_event_seq": a2["selection_event_seq"],
                             "declined_code": "PRICE_MISMATCH", "code": "RESEARCH_POLICY_CHANGED"}
    # Neither the decline nor a replacement was written; nothing is latched; still offered.
    assert events(engine, "RESEARCH_ADMISSION_DECLINED") == [] and replacements(engine) == []
    assert runtime.error is None and not runtime.latches.blocking()
    assert a2["selection_event_seq"] in [p["selection_event_seq"]
                                         for p in runtime._selected_packets()]
    runtime.research = cycle  # With the cycle's own policy the next tick does both.
    runtime.execution_once()
    [decline] = events(engine, "RESEARCH_ADMISSION_DECLINED")
    [replacement] = replacements(engine, cycle_id)
    assert (replacement["decline_event_seq"], replacement["replacement_symbol"]) == (
        decline["event_seq"], "A6/USD")


@pytest.fixture
def v3_under_v2(mx):
    """Report V3 picks selected under the default V2 rule (package system-check's fixture)."""
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    chosen = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now), v3_pick(1, "BBB/USD", venue.now)],
                        run_slot=old)
    return engine, venue, None, None, list(chosen.values()), None


@pytest.fixture
def legacy_v2(mx):
    cycle, cycle_id = reviewed_cycle(mx, 1)
    return mx[0], mx[1], cycle, cycle_id, cycle.approved_packets(cycle_id), None


@pytest.mark.parametrize("fixture", ["v3_under_v2", "legacy_v2", "b1_selection", "b2_selection"])
def test_v2_b1_and_b2_declines_are_never_replaced(fixture, request, mx, market):
    engine, _, _, _, chosen, _ = request.getfixturevalue(fixture)
    research, _ = topk_cycle(mx, LADDER, rule=K5)  # A runtime able to replace top-K picks.
    runtime = runtime_for(mx, market, research, [])
    packet = chosen[0]
    assert packet["selection_policy"] != TOPK
    assert not replaces_on_decline(packet, "INVALID_OR_EXPIRED_SETUP", research)
    before = len(events(engine, "RESEARCH_SELECTED"))
    refuse(runtime, packet, "INVALID_OR_EXPIRED_SETUP")
    [decline] = events(engine, "RESEARCH_ADMISSION_DECLINED")
    assert decline["body"] == {
        "cycle_id": packet["cycle_id"], "item_key": packet["item_key"],
        "revision": packet["revision"], "receipt_id": packet["receipt_id"],
        "selection_event_seq": packet["selection_event_seq"],
        "reason": "INVALID_OR_EXPIRED_SETUP"}
    assert replacements(engine) == [] and len(events(engine, "RESEARCH_SELECTED")) == before
    with engine.store.transaction() as conn:
        assert research.replace_declined(conn, decline) is None


def test_a_top_k_pick_off_the_price_grid_is_declined_and_replaced(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx, picks={"A2/USD": {"levels": OFF_GRID}})
    runtime = runtime_for(mx, market, cycle, ["A2/USD"])
    quote(runtime, venue, {"A2/USD": PASSING})
    runtime.execution_once()
    runtime.execution_once()
    [refused] = events(engine, "CRYPTO_ADMISSION_REFUSED")
    [decline] = events(engine, "RESEARCH_ADMISSION_DECLINED")
    assert refused["body"]["reason"] == decline["body"]["reason"] == "CRYPTO_LEVEL_OFF_PRICE_GRID"
    [replacement] = replacements(engine, cycle_id)
    assert (replacement["declined_code"], replacement["replacement_symbol"]) == (
        "CRYPTO_LEVEL_OFF_PRICE_GRID", "A6/USD")
    a2 = selected(engine, cycle_id)["A2/USD"]
    for code in ("CRYPTO_LEVEL_OFF_PRICE_GRID", "CRYPTO_PRECISION_UNAVAILABLE"):
        assert code not in PERMANENT_ADMISSION_REFUSALS
        assert code in TOPK_PERMANENT_ADMISSION_REFUSALS
        assert permanent_refusal(a2, code)
    assert PERMANENT_ADMISSION_REFUSALS < TOPK_PERMANENT_ADMISSION_REFUSALS


def test_other_rules_keep_retrying_an_off_grid_pick_unchanged(mx, market):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    packet = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now, levels=OFF_GRID)],
                        run_slot=old)["AAA/USD"]
    research, _ = topk_cycle(mx, LADDER, rule=K5)
    runtime = runtime_for(mx, market, research, ["AAA/USD"])
    quote(runtime, venue, {"AAA/USD": PASSING})
    runtime.execution_once()
    runtime.execution_once()
    [refused] = events(engine, "RUNTIME_ADMISSION_REFUSED")
    assert refused["body"]["reason"] == "CRYPTO_LEVEL_OFF_PRICE_GRID"
    assert events(engine, "RESEARCH_ADMISSION_DECLINED") == [] and replacements(engine) == []
    assert [p["selection_event_seq"] for p in runtime._selected_packets()] == [
        packet["selection_event_seq"]]
    assert not permanent_refusal(packet, "CRYPTO_LEVEL_OFF_PRICE_GRID")


# --- Visibility: the research context and the setups list ---------------------------------------

def test_the_research_context_shows_each_picks_true_status_and_the_ranking(mx, market):
    engine, venue, _ = mx
    old, new = two_slots(venue.now)
    single(mx, "A3/USD", agent_id="instinct", run_slot=old)  # A3 is skipped at publication.
    cycle, cycle_id = ladder(mx, run_slot=old)
    runtime = runtime_for(mx, market, cycle, ["A1/USD", "A2/USD", "A4/USD", "A5/USD"])
    quote(runtime, venue, {"A1/USD": PASSING, "A2/USD": MISMATCH, "A4/USD": MISMATCH,
                           "A5/USD": MISMATCH})
    runtime.execution_once()  # A1 admitted; A2 -> A7, A4 -> A8, A5 exhausted; A6 waits.
    now = [venue.now]
    service, _ = service_for(engine.repo, lambda: now[0], Feeds(lambda: now[0]))
    principal = SimpleNamespace(role="muse", agent_id=AGENT, legacy=False)

    def last_run():
        run = service.context(principal)["recent_outcomes"]["last_run"]
        return run, {p["symbol"]: p for p in run["picks"]}

    run, by = last_run()
    assert run["cycle_id"] == cycle_id and run["selection_policy"] == TOPK
    assert run["ranking"] == {"policy": TOPK, "k": K, "complete": True,
                              "counts": {"RANKED": 8, "VETOED": 1, "NOT_RANKED": 1}}
    fields = ("status", "selection_status", "jev_rank", "ranking_status", "decline_code",
              "replaced_by", "replacement_outcome", "replacement_for")
    assert {s: tuple(p[f] for f in fields) for s, p in by.items()} == {
        "A1/USD": ("WATCHING", "ADMITTED", 1, "RANKED", None, None, None, None),
        "A2/USD": ("REPLACED_BY", "REPLACED_BY", 2, "RANKED", "PRICE_MISMATCH", key("A7/USD"),
                   "PUBLISHED", None),
        "A3/USD": ("SKIPPED", None, 3, "RANKED", None, None, None, None),
        "A4/USD": ("REPLACED_BY", "REPLACED_BY", 4, "RANKED", "PRICE_MISMATCH", key("A8/USD"),
                   "PUBLISHED", None),
        "A5/USD": ("DECLINED", "DECLINED", 5, "RANKED", "PRICE_MISMATCH", None, "EXHAUSTED",
                   None),
        "A6/USD": ("SELECTED", "SELECTED", 6, "RANKED", None, None, None, None),
        "A7/USD": ("SELECTED", "SELECTED", 7, "RANKED", None, None, None, key("A2/USD")),
        "A8/USD": ("SELECTED", "SELECTED", 8, "RANKED", None, None, None, key("A4/USD")),
        "V1/USD": ("VETOED", None, None, "VETOED", None, None, None, None),
        "N1/USD": ("NOT_RANKED", None, None, "NOT_RANKED", None, None, None, None),
    }
    assert (by["A3/USD"]["skip_reason"], by["A3/USD"]["selected"]) == (
        "DUPLICATE_SYMBOL_IN_RUN", False)
    assert by["V1/USD"]["ranking_reasons"] == ["NEWS_STALE_YES"]
    assert by["N1/USD"]["ranking_reasons"] == ["HTTP_500"]
    assert by["A1/USD"]["ranking_reasons"] == [] and by["A1/USD"]["setup_state"] == "WATCHING"
    assert all(p["selected"] for s, p in by.items() if s not in {"A3/USD", "V1/USD", "N1/USD"})
    # Past the picks' validity, the ones still waiting show EXPIRED.
    now[0] = venue.now + timedelta(minutes=31)
    _, by = last_run()
    assert {s: by[s]["status"] for s in ("A6/USD", "A7/USD", "A8/USD")} == dict.fromkeys(
        ("A6/USD", "A7/USD", "A8/USD"), "EXPIRED")
    assert by["A1/USD"]["status"] == "WATCHING"
    # The next run goes live: the waiting picks are SUPERSEDED (never replaced) and the
    # watching setup is retired, its reason shown.
    now[0] = venue.now
    single(mx, "B1/USD", agent_id="grogbot", run_slot=new)
    runtime._retire_superseded_research()
    _, by = last_run()
    assert {s: (by[s]["status"], by[s]["decline_code"], by[s]["replacement_outcome"])
            for s in ("A6/USD", "A7/USD", "A8/USD")} == dict.fromkeys(
        ("A6/USD", "A7/USD", "A8/USD"), ("SUPERSEDED", SUPERSEDED, None))
    assert (by["A1/USD"]["status"], by["A1/USD"]["selection_status"],
            by["A1/USD"]["setup_reason"]) == ("INVALIDATED", "ADMITTED", SUPERSEDED)
    assert len(replacements(engine, cycle_id)) == 3


def test_setups_keep_the_entry_type_and_system_check_of_a_v3_setup(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)
    runtime = runtime_for(mx, market, cycle, ["A1/USD"])
    quote(runtime, venue, {"A1/USD": PASSING})
    runtime.execution_once()
    v2_cycle, v2_cycle_id = reviewed_cycle(mx, 1)
    v2_packet = v2_cycle.approved_packets(v2_cycle_id)[0]
    classify(engine, v2_packet["symbol"])
    engine.admit(v2_packet)
    service, _ = service_for(engine.repo, lambda: venue.now, Feeds(lambda: venue.now))
    web = context_client(cycle, engine.store, service)
    reply = web.get("/api/v1/lab/setups", headers=bearer(STATUS))
    assert reply.status_code == 200
    items = {item["symbol"]: item for item in reply.json()["items"]}
    v3 = items["A1/USD"]["state"]
    assert (v3["state"], v3["entry_type"]) == ("WATCHING", "PULLBACK")
    check = v3["system_check"]
    assert (check["version"], check["result"], check["code"], check["entry_type"]) == (
        "SYSTEM_CHECK_V1", "PASSED", None, "PULLBACK")
    assert set(check["checks"].values()) == {"PASS"}
    assert check["live"]["quote_source"] == "ALPACA_STREAM"
    v2 = items[v2_packet["symbol"]]["state"]
    assert v2["state"] == "WATCHING" and "entry_type" not in v2 and "system_check" not in v2



def test_the_session_harness_replaces_a_declined_top_k_pick_as_the_runtime_does(make_session,
                                                                               tmp_path):
    session = make_session(selection_rule="TOPK", topk_k=5, management_reviews="DISABLED",
                           fixture=session_script.FixtureScript(TOPK_SCRIPT))
    raw = topk_report(datetime.now(UTC))
    # SOL, Jev's rank 2: the agent's own price 99.50 puts its entry 100 above the session's
    # simulated mid, a breakout the system check refuses for good.
    next(p for p in raw["picks"] if p["symbol"] == "SOL/USD")["agent_current_price"] = "99.50"
    submit_to_session(session, raw)
    session.tick()
    first = {row["symbol"]: row["admission"]
             for row in session.execute(simulate_prints=False)["admissions"]}
    assert {s: a["outcome"] for s, a in first.items()} == {
        "BTC/USD": "ADMITTED", "SOL/USD": "REFUSED", "XRP/USD": "ADMITTED",
        "UNI/USD": "ADMITTED", "LINK/USD": "ADMITTED"}
    assert first["SOL/USD"] == {
        "outcome": "REFUSED", "code": "BREAKOUT_NOT_ENABLED", "declined": True,
        "replacement_fault": None,
        "replacement": {"outcome": "PUBLISHED", "code": None,
                        "replacement_item_key": key("AVAX/USD"), "replacement_rank": 6,
                        "passed_over": []}}
    second = session.execute(simulate_prints=False)["admissions"]
    assert [(row["symbol"], row["admission"]["outcome"]) for row in second] == [
        ("AVAX/USD", "ADMITTED")]
    output = tmp_path / "export"
    assert session.export(output)["audit"]["valid"]
    [cycle] = json.loads((output / "decisions.json").read_text())["cycles"]
    [decision] = cycle["replacements"]
    assert (decision["declined_item_key"], decision["declined_code"],
            decision["replacement_item_key"]) == (
        key("SOL/USD"), "BREAKOUT_NOT_ENABLED", key("AVAX/USD"))
    items = {item["symbol"]: item for item in cycle["items"]}
    [avax] = items["AVAX/USD"]["revisions"]
    assert (avax["selection"]["replacement_for"], avax["setup"]["state"]) == (
        key("SOL/USD"), "WATCHING")
    [sol] = items["SOL/USD"]["revisions"]
    assert [r["kind"] for r in sol["admission_refusals"]] == [
        "RUNTIME_ADMISSION_REFUSED", "RESEARCH_ADMISSION_DECLINED"]

# --- Pure pieces -------------------------------------------------------------------------------

def test_the_rule_names_codes_and_key():
    assert (topk.REPLACEMENT_RULE, topk.REPLACEMENT_KIND, topk.RANKING_EXHAUSTED) == (
        "TOPK_REPLACEMENT_V1", "RESEARCH_REPLACEMENT", "TOPK_RANKING_EXHAUSTED")
    assert (topk.REPLACEMENT_PUBLISHED, topk.REPLACEMENT_EXHAUSTED) == ("PUBLISHED", "EXHAUSTED")
    assert topk.PASSED_OVER_REFUSALS == {"TOPK_ENTRY_SKIPPED", "DUPLICATE_SYMBOL_IN_RUN",
                                         "REVIEW_EXPIRED", "TOPK_RANKING_BINDING_FAILURE"}
    assert topk.replacement_key_for("c", "CRYPTO:X/USD", 1) == (
        "research:c:CRYPTO:X/USD:1:replacement")
    research = SimpleNamespace(replace_declined=lambda conn, decline: None)
    top_k = {"selection_policy": TOPK}
    assert replaces_on_decline(top_k, "PRICE_MISMATCH", research)
    assert not replaces_on_decline(top_k, SUPERSEDED, research)
    assert not replaces_on_decline(top_k, "PRICE_MISMATCH", SimpleNamespace())
    for policy in ("MUSE_JEV_RESEARCH_SELECTION_V2", "MUSE_JEV_RESEARCH_SELECTION_B1_V1",
                   "MUSE_JEV_RESEARCH_SELECTION_B2_V1", ENGINEERING_SELECTION_POLICY, None):
        packet = {"selection_policy": policy}
        assert not replaces_on_decline(packet, "PRICE_MISMATCH", research)
        assert not permanent_refusal(packet, "CRYPTO_LEVEL_OFF_PRICE_GRID")
        assert permanent_refusal(packet, "PRICE_MISMATCH")
    assert not permanent_refusal(top_k, "LIVE_PRICE_UNAVAILABLE")
    assert not permanent_refusal(top_k, "CORRELATION_UNKNOWN")
