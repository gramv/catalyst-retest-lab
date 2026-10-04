"""RESEARCH_REVIEW_BREAKER_GATE_V1 (owner, 2026-09-29; docs/CONTRACT-RESOLUTIONS.md): a top-K
pick's reviews start only while the Jev circuit breaker shared with the trade reviews is CLOSED.

Fixture and disposable-PostgreSQL evidence only. One Gate1Runtime scope and one reviewer serve
the research cycle, a stand-in for the per-minute trade reviews and the research loop's recovery
probes, as ``build_runtime_from_env`` shares ``worker.reviewer``. A scripted mock Jev transport
and the mock paper venue; no provider, broker, service or owner ledger is touched.
"""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab import research_selection_topk as topk
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.jev_contract import SKEPTIC, strict_json
from catalyst_lab.jev_review import JevReviewer
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import ResearchCycle
from catalyst_lab.research_ranking import QUALITY_V3
from catalyst_lab.research_selection_b1 import B1_POLICY, B2_POLICY, SelectionRule
from catalyst_lab.review_config import APPROVED_GATE1_V2, Gate1Inputs
from catalyst_lab.review_runtime import HEALTH_STATE, ClockSample, Gate1Runtime
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_managed_runtime import Source
from tests.test_research_reports import reply
from tests.test_review_worker import health_reply
from tests.test_selection_b1 import no_sleep
from tests.test_selection_topk import (
    WINDOW,
    bodies,
    ranking_of,
    rows_of,
    run,
    submit_v3,
    symbol_of,
    topk_provider,
)

TOPK_V2 = topk.TOPK_POLICY_V2
GATE = "RESEARCH_REVIEW_BREAKER_GATE_V1"
DEFERRED = "RESEARCH_REVIEWS_DEFERRED"
CREDENTIALS = AlpacaCredentials("PKRUNTIMEFIXTURE", "test-only-secret")
# Six report V3 picks under JEV_TOP_K_SELECTION_V2 with K = 5: one veto, five ranked.
SCRIPT = {
    "GA/USD": {"kind": "NEWS", "quality": "STRONG", "levels": (2, 2, 2, 2)},
    "GB/USD": {"kind": "CHART", "quality": "ADEQUATE", "levels": (2, 2, 1, 1)},
    "GC/USD": {"kind": "BOTH", "labels": {"already_priced": "HIGH"}},
    "GD/USD": {"kind": "BOTH", "labels": {"setup_already_broken": "YES"}},
    "GE/USD": {"kind": "NEWS", "quality": "WEAK", "levels": (1, 0, 0, 0)},
    "GF/USD": {"kind": "CHART", "quality": "WEAK", "levels": (0, 0, 0, 0)},
}
# Derived by hand (12.5 per quality level; ALREADY_PRICED_HIGH costs 10): rank order.
RANKED = ["GA/USD", "GB/USD", "GC/USD", "GE/USD", "GF/USD"]
OTHER = {  # A second agent's report answering the same run.
    "GX/USD": {"kind": "CHART", "quality": "STRONG", "levels": (2, 2, 2, 2)},
    "GY/USD": {"kind": "NEWS", "quality": "ADEQUATE", "levels": (1, 1, 1, 1)},
}


def provider(research_calls, mode):
    """Research picks are answered by the top-K script (and recorded in ``research_calls``);
    the recovery probe's synthetic health check and the trade-review stand-in answer with the
    status in ``mode``, and every call reaching the transport is counted by its kind."""
    picks = topk_provider({**SCRIPT, **OTHER}, research_calls)

    def handle(request):
        body = strict_json(request.content)
        if body["state"] == HEALTH_STATE:
            mode["probe_calls"] += 1
            status = mode["probe"]
            return httpx.Response(status, json=health_reply() if status == 200 else {})
        if "symbol" not in body["state"]:
            mode["trade_calls"] += 1
            status = mode["trade"]
            return httpx.Response(status, json=reply() if status == 200 else {})
        return picks(request)

    return handle


@pytest.fixture
def gated(mx, er):
    """The research cycle and the runtime run on the venue's fixture clock; the reviewer, like
    the production worker's, on real time, since its permits expire by the database clock."""
    engine, venue, receipts = mx
    runtime = Gate1Runtime(
        receipts,
        Gate1Inputs(copy.deepcopy(APPROVED_GATE1_V2)),
        credential_slot="gate-" + uuid4().hex[:16],
        worker_id=uuid4(),
        clock_health=lambda: ClockSample(0, 0, datetime.now(UTC)),
    )
    research_calls = []
    mode = {"probe": 200, "trade": 200, "probe_calls": 0, "trade_calls": 0}
    reviewer = JevReviewer(
        receipts,
        runtime.policy,
        runtime=runtime,
        key_provider=lambda: FIXTURE_KEY,
        transport=httpx.MockTransport(provider(research_calls, mode)),
        sleep=no_sleep,
    )
    return SimpleNamespace(er=er, engine=engine, venue=venue, runtime=runtime,
                           reviewer=reviewer, calls=research_calls, mode=mode)


def cycle_for(gated, rule=None):
    cycle = ResearchCycle(gated.engine.repo, gated.reviewer, WINDOW,
                          clock=lambda: gated.venue.now,
                          selection=topk.TopKRule(TOPK_V2, 5) if rule is None else rule)
    cycle.record_selection_rule(runtime_id=str(uuid4()))
    return cycle


def sync(gated):
    """The database checks review deadlines and worker heartbeats against its own clock: keep
    the fixture clock at or after real time and the review worker's heartbeat fresh."""
    gated.venue.now = max(gated.venue.now, datetime.now(UTC))
    gated.runtime.heartbeat("RUNNING")


def trade_args(gated):
    """A review as the per-minute position, maintenance and day reviews send one: through the
    same reviewer and runtime scope, not a research pick."""
    return dict(
        request_id=uuid4(),
        identity={"diagnostic_id": str(uuid4())},
        state={"source": "Stand-in for a per-minute trade review", "sample": uuid4().hex},
        question_set=SKEPTIC,
        expires_at=gated.venue.now + timedelta(seconds=10),
        purpose="ENGINEERING_TEST",
    )


def trip(gated):
    """Trade reviews fail three times in a row (a 529 is retried twice): the breaker opens."""
    sync(gated)
    gated.mode["trade"] = 529
    failed = asyncio.run(gated.reviewer.jev_review(**trade_args(gated)))
    gated.mode["trade"] = 200
    state = gated.runtime.state()
    assert failed.reason == "HTTP_529" and state["state"] == "OPEN"
    return state


def bump(gated, **fields):
    """Virtual time in the disposable database only: an appended VIRTUAL_TIME_FIXTURE state row
    (as tests/test_managed_jev_breaker.py does), never an UPDATE or DELETE."""
    state = Gate1Runtime.state(gated.runtime) | fields
    owner = gated.er.database_url.replace("user=catalyst_app", "user=lab_owner")
    with psycopg.connect(owner) as conn:
        conn.execute(
            "INSERT INTO lab.review_breaker_events(scope_id,state_json,reason) "
            "VALUES(%s,%s,'VIRTUAL_TIME_FIXTURE')",
            (gated.runtime.scope_id, Jsonb(json_safe(state))),
        )


def past():
    return datetime.now(UTC) - timedelta(seconds=1)


def managed(gated, cycle):
    """The managed runtime around the real research cycle: its research loop's tick."""
    runtime = ManagedRuntime(gated.engine, cycle, Source(), CREDENTIALS,
                             engineering_runtime_policy(), clock=lambda: gated.venue.now,
                             reviewer_heartbeat=lambda: True, gate1=gated.runtime)
    runtime.research_healthy = True
    return runtime


def research_tick(gated, runtime):
    """One research-loop tick (``ManagedRuntime._research_loop``): a due recovery probe, then
    every live cycle's research tick and publication. A cycle fault fails the test."""
    sync(gated)

    async def tick():
        await runtime.probe_once()
        await runtime._research_pass()

    asyncio.run(tick())
    assert not rows_of_kind(gated, "RESEARCH_CYCLE_FAULT")


def rows_of_kind(gated, kind):
    with gated.engine.repo.connect() as conn:
        return conn.execute("SELECT * FROM lab.managed_events WHERE kind=%s ORDER BY event_seq",
                            (kind,)).fetchall()


def research_requests(gated, cycle_id):
    """The cycle's research review requests: each one reached the breaker's permit."""
    with gated.reviewer.store.connect() as conn:
        return conn.execute(
            """SELECT q.request_id,r.outcome,r.error_code FROM lab.jev_requests q
            LEFT JOIN lab.jev_receipts r USING(request_id)
            WHERE q.evidence_identity->>'cycle_id'=%s ORDER BY q.created_at,r.attempt""",
            (cycle_id,),
        ).fetchall()


def deferred(gated, cycle_id):
    return rows_of(gated.engine, DEFERRED, cycle_id)


def untouched(cycle, cycle_id):
    """Nothing research-side was claimed, decided, scored, ranked or published."""
    return not any(bodies(cycle, cycle_id, kind) for kind in (
        "RESEARCH_CLAIM", "RESEARCH_DECISION", "RESEARCH_QUALITY", "RESEARCH_RANKING",
        "RESEARCH_SELECTED"))


def reviewed(gated, cycle, cycle_id, script):
    """Every pick decided and scored once from a valid review, the ranking complete and the
    top K published in rank order."""
    decisions = bodies(cycle, cycle_id, "RESEARCH_DECISION")
    quality = bodies(cycle, cycle_id, "RESEARCH_QUALITY")
    packets = {p["item_key"]: p for p in bodies(cycle, cycle_id, "RESEARCH_PACKET")}
    assert sorted(d["item_key"].split(":", 1)[1] for d in decisions) == sorted(script)
    assert {d["disposition"] for d in decisions} <= {"RANKABLE", "VETOED"}
    assert all(d["request_id"] == cycle._topk_decision_request(packets[d["item_key"]])
               for d in decisions)  # The claim's own request ID: the gate reads the same one.
    assert sorted(q["item_key"].split(":", 1)[1] for q in quality) == sorted(script)
    assert {q["status"] for q in quality} == {"SCORED"}
    assert all(r["outcome"] == "VALID" for r in research_requests(gated, cycle_id))
    return ranking_of(gated.engine, cycle_id)["body"]


# --- The breaker open: the picks wait -----------------------------------------------------------


def test_while_the_breaker_is_open_no_research_review_starts_and_the_picks_wait(gated):
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    runtime = managed(gated, cycle)
    state = trip(gated)
    for _ in range(3):
        research_tick(gated, runtime)
    assert gated.runtime.state()["state"] == "OPEN"  # Its 30-second cooldown is still running.
    # Nothing was claimed, sent, refused or recorded for any pick: no CIRCUIT_OPEN anywhere.
    assert gated.calls == [] and research_requests(gated, cycle_id) == []
    assert untouched(cycle, cycle_id)
    # One visible event for this opening, in the ledger and the cycle outputs.
    [row] = deferred(gated, cycle_id)
    packets = bodies(cycle, cycle_id, "RESEARCH_PACKET")
    assert row["idempotency_key"] == (
        f"research:{cycle_id}:reviews-deferred:{gated.runtime.scope_id}:1")
    assert row["body"] == {
        "cycle_id": cycle_id, "gate": GATE, "breaker_state": "OPEN", "breaker_epoch": 1,
        "blocked_until": state["blocked_until"], "runtime_scope": gated.runtime.scope_id,
        "review_policy": "JEV_LIVE_REVIEW_POLICY_V2", "waiting_picks": len(SCRIPT),
        "review_deadline": min(cycle._topk_deadline(p) for p in packets).isoformat(),
    }
    assert bodies(cycle, cycle_id, DEFERRED) == [row["body"]]


def test_once_the_probes_close_the_breaker_the_picks_are_reviewed_ranked_and_published(gated):
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    runtime = managed(gated, cycle)
    trip(gated)
    research_tick(gated, runtime)
    assert untouched(cycle, cycle_id)
    # Past the cooldown: the loop's first probe answers and the breaker is HALF_OPEN, so the
    # pass that follows it in the same tick still starts nothing.
    bump(gated, blocked_until=past())
    research_tick(gated, runtime)
    assert gated.runtime.state()["state"] == "HALF_OPEN" and gated.mode["probe_calls"] >= 1
    assert gated.calls == [] and untouched(cycle, cycle_id)
    # The second probe closes it, and the same tick reviews, ranks and publishes the picks.
    bump(gated, next_probe_at=past())
    research_tick(gated, runtime)
    assert gated.runtime.state()["state"] == "CLOSED"
    ranking = reviewed(gated, cycle, cycle_id, SCRIPT)
    assert len(gated.calls) == 2 * len(SCRIPT)  # One kind review and one QUALITY_V3 each.
    assert (ranking["complete"], ranking["counts"]) == (
        True, {"RANKED": 5, "VETOED": 1, "NOT_RANKED": 0})
    assert [symbol_of(e) for e in ranking["entries"] if e["status"] == "RANKED"] == RANKED
    selected = bodies(cycle, cycle_id, "RESEARCH_SELECTED")
    assert [s["packet"]["symbol"] for s in selected] == RANKED
    assert [s["packet"]["rank"] for s in selected] == [1, 2, 3, 4, 5]
    assert len(deferred(gated, cycle_id)) == 1  # HALF_OPEN kept the epoch: no second event.


def test_the_deferred_event_is_recorded_once_per_cycle_and_breaker_opening(gated):
    cycle = cycle_for(gated)
    first = submit_v3(cycle, SCRIPT, gated.venue.now)
    second = submit_v3(cycle, OTHER, gated.venue.now, agent_id="instinct")
    runtime = managed(gated, cycle)

    def epochs(cycle_id):
        return [(r["body"]["breaker_epoch"], r["body"]["waiting_picks"])
                for r in deferred(gated, cycle_id)]

    trip(gated)
    for _ in range(3):
        research_tick(gated, runtime)
    assert (epochs(first), epochs(second)) == ([(1, 6)], [(1, 2)])
    bump(gated, blocked_until=past())
    research_tick(gated, runtime)  # A good probe: HALF_OPEN, the same opening.
    assert gated.runtime.state()["state"] == "HALF_OPEN"
    assert (epochs(first), epochs(second)) == ([(1, 6)], [(1, 2)])
    # A failed recovery probe opens the breaker again: a new opening, one more event each.
    gated.mode["probe"] = 503
    bump(gated, next_probe_at=past())
    research_tick(gated, runtime)
    assert (gated.runtime.state()["state"], gated.runtime.state()["epoch"]) == ("OPEN", 2)
    research_tick(gated, runtime)
    assert (epochs(first), epochs(second)) == ([(1, 6), (2, 6)], [(1, 2), (2, 2)])
    # Closed after two good probes: both cycles are reviewed and nothing more is deferred. (The
    # failed probe also spaced the next one by a second: skip that wait too.)
    gated.mode["probe"] = 200
    bump(gated, blocked_until=past(), next_probe_at=past())
    research_tick(gated, runtime)
    assert (gated.runtime.state()["state"], gated.runtime.state()["epoch"]) == ("HALF_OPEN", 2)
    bump(gated, next_probe_at=past())
    research_tick(gated, runtime)
    assert gated.runtime.state()["state"] == "CLOSED"
    reviewed(gated, cycle, first, SCRIPT)
    reviewed(gated, cycle, second, OTHER)
    research_tick(gated, runtime)
    assert (len(epochs(first)), len(epochs(second))) == (2, 2)
    assert len(gated.calls) == 2 * (len(SCRIPT) + len(OTHER))


def test_picks_still_waiting_at_their_deadline_are_ranked_review_deadline_passed(gated):
    """The breaker never closes in time: each pick expires as any unreviewed pick does."""
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    trip(gated)
    sync(gated)
    assert run(cycle, cycle_id) == [] and len(deferred(gated, cycle_id)) == 1
    packets = bodies(cycle, cycle_id, "RESEARCH_PACKET")
    gated.venue.now = max(cycle._topk_deadline(p) for p in packets)
    assert run(cycle, cycle_id) == []
    ranking = ranking_of(gated.engine, cycle_id)["body"]
    assert (ranking["complete"], ranking["counts"]) == (
        False, {"RANKED": 0, "VETOED": 0, "NOT_RANKED": len(SCRIPT)})
    assert {e["reason"] for e in ranking["entries"]} == {topk.REVIEW_DEADLINE_PASSED}
    assert not bodies(cycle, cycle_id, "RESEARCH_DECISION")
    assert not bodies(cycle, cycle_id, "RESEARCH_QUALITY")
    assert not bodies(cycle, cycle_id, "RESEARCH_SELECTED")
    assert gated.calls == [] and research_requests(gated, cycle_id) == []


def test_a_review_already_recorded_is_read_back_while_the_breaker_is_open(gated):
    """A tick interrupted after a pick's two calls left their requests and valid receipts
    but no decision or quality event: an open breaker does not hold that pick back, since
    reading it back sends nothing. The other picks wait."""
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    packet = next(p for p in bodies(cycle, cycle_id, "RESEARCH_PACKET")
                  if p["symbol"] == "GA/USD")
    sync(gated)
    deadline = gated.venue.now + timedelta(seconds=10)
    for request_id, identity, questions in (
        (cycle._topk_decision_request(packet), cycle._identity(packet),
         topk.question_set(packet["state"]["kind"], TOPK_V2)),
        (cycle._topk_quality_request(packet), cycle._quality_identity(packet), QUALITY_V3),
    ):
        done = asyncio.run(gated.reviewer.jev_review(
            request_id=request_id, identity=identity, state=packet["state"],
            question_set=questions, expires_at=deadline, purpose="ENGINEERING_TEST"))
        assert done.status == "RECORDED"
    assert len(gated.calls) == 2
    trip(gated)
    sync(gated)
    assert [d["disposition"] for d in asyncio.run(cycle.tick(cycle_id))] == ["RANKABLE"]
    assert cycle.approved_packets(cycle_id) == []  # The other picks are still unranked.
    [decision] = bodies(cycle, cycle_id, "RESEARCH_DECISION")
    [quality] = bodies(cycle, cycle_id, "RESEARCH_QUALITY")
    assert decision["item_key"] == quality["item_key"] == packet["item_key"]
    assert (decision["reason"], quality["status"]) == ("TOPK_COMPONENTS_PASSED", "SCORED")
    assert len(gated.calls) == 2  # Read back from the receipts: nothing new was sent.
    [row] = deferred(gated, cycle_id)
    assert row["body"]["waiting_picks"] == len(SCRIPT) - 1


# --- The race between the check and the call ---------------------------------------------------


def test_a_breaker_opening_after_the_tick_check_is_caught_before_each_call(gated):
    """The breaker opens between the tick's check and the calls: each review reads it again
    just before its call, so nothing is sent or recorded. The claims hold the picks for their
    lease, after which they are reviewed normally."""
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    reads = []

    def opens_after_the_first_read():
        state = Gate1Runtime.state(gated.runtime)
        if not reads:  # The tick's own check: CLOSED. The trade reviews trip it right after.
            bump(gated, state="OPEN", epoch=state["epoch"] + 1, failures=3, successes=0,
                 blocked_until=(datetime.now(UTC) + timedelta(seconds=30)).isoformat())
        reads.append(state["state"])
        return state

    gated.runtime.state = opens_after_the_first_read
    sync(gated)
    assert run(cycle, cycle_id) == []
    assert reads[0] == "CLOSED" and set(reads[1:]) == {"OPEN"}
    assert len(reads) == 1 + 2 * len(SCRIPT)  # Then once before each of the twelve calls.
    assert gated.calls == [] and research_requests(gated, cycle_id) == []
    assert len(bodies(cycle, cycle_id, "RESEARCH_CLAIM")) == len(SCRIPT)
    assert not bodies(cycle, cycle_id, "RESEARCH_DECISION")
    assert not bodies(cycle, cycle_id, "RESEARCH_QUALITY")
    [row] = deferred(gated, cycle_id)
    assert (row["body"]["breaker_state"], row["body"]["breaker_epoch"],
            row["body"]["waiting_picks"]) == ("OPEN", 1, len(SCRIPT))
    del gated.runtime.state
    bump(gated, state="CLOSED", failures=0, successes=0, blocked_until=None)
    sync(gated)
    gated.venue.now += timedelta(seconds=WINDOW.claim_lease_seconds)  # The leases lapse.
    run(cycle, cycle_id)
    ranking = reviewed(gated, cycle, cycle_id, SCRIPT)
    assert ranking["counts"] == {"RANKED": 5, "VETOED": 1, "NOT_RANKED": 0}


def test_a_refusal_in_the_last_race_window_is_recorded_as_before_and_never_retried(gated):
    """Both checks read CLOSED but the permit refuses: the breaker opened after the last check.
    The refusal is the review's record, as before this version: the ranking lists the pick
    NOT_RANKED ``CIRCUIT_OPEN`` (``QUALITY_CIRCUIT_OPEN`` had only its quality review been
    refused), and it is never retried."""
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    closed = {**trip(gated), "state": "CLOSED"}
    gated.runtime.state = lambda: closed
    sync(gated)
    run(cycle, cycle_id)
    decisions = bodies(cycle, cycle_id, "RESEARCH_DECISION")
    quality = bodies(cycle, cycle_id, "RESEARCH_QUALITY")
    assert {(d["disposition"], d["reason"]) for d in decisions} == {("NOT_RANKED",
                                                                      "CIRCUIT_OPEN")}
    assert {(q["status"], q["reason"]) for q in quality} == {("NOT_SCORED", "CIRCUIT_OPEN")}
    refused = research_requests(gated, cycle_id)
    assert len(refused) == 2 * len(SCRIPT)
    assert {(r["outcome"], r["error_code"]) for r in refused} == {("CIRCUIT_OPEN",
                                                                   "CIRCUIT_OPEN")}
    assert gated.calls == [] and not deferred(gated, cycle_id)
    ranking = ranking_of(gated.engine, cycle_id)["body"]
    assert (ranking["complete"], ranking["counts"]) == (
        True, {"RANKED": 0, "VETOED": 0, "NOT_RANKED": len(SCRIPT)})
    assert {e["reason"] for e in ranking["entries"]} == {"CIRCUIT_OPEN"}
    # The breaker really closes: the recorded refusals stay the picks' reviews.
    del gated.runtime.state
    bump(gated, state="CLOSED", failures=0, successes=0, blocked_until=None)
    sync(gated)
    gated.venue.now += timedelta(seconds=WINDOW.claim_lease_seconds)
    assert run(cycle, cycle_id) == []
    assert research_requests(gated, cycle_id) == refused and gated.calls == []


# --- Everything else is unchanged ----------------------------------------------------------------


def test_trade_reviews_meet_the_open_breaker_and_the_probes_close_it_as_before(gated):
    """The gate is the research pass's alone: a trade review still goes to the permit and is
    refused while the breaker is open, as before, and the research loop's probes still close
    it, after which trade reviews go out again."""
    cycle = cycle_for(gated)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    runtime = managed(gated, cycle)
    trip(gated)
    assert gated.mode["trade_calls"] == 3
    research_tick(gated, runtime)
    sync(gated)
    blocked = asyncio.run(gated.reviewer.jev_review(**trade_args(gated)))
    assert (blocked.status, blocked.reason) == ("NEEDS_REVIEW", "CIRCUIT_OPEN")
    assert gated.mode["trade_calls"] == 3  # Refused at its permit: it never went out.
    with gated.reviewer.store.connect() as conn:
        receipt = conn.execute(
            "SELECT outcome,error_code FROM lab.jev_receipts WHERE request_id=%s",
            (blocked.request_id,),
        ).fetchone()
    assert (receipt["outcome"], receipt["error_code"]) == ("CIRCUIT_OPEN", "CIRCUIT_OPEN")
    bump(gated, blocked_until=past())
    research_tick(gated, runtime)
    bump(gated, next_probe_at=past())
    research_tick(gated, runtime)
    assert gated.runtime.state()["state"] == "CLOSED" and gated.mode["probe_calls"] >= 2
    sync(gated)
    resumed = asyncio.run(gated.reviewer.jev_review(**trade_args(gated)))
    assert resumed.status == "RECORDED" and gated.mode["trade_calls"] == 4
    reviewed(gated, cycle, cycle_id, SCRIPT)


@pytest.mark.parametrize(
    "rule",
    [SelectionRule(), SelectionRule(B1_POLICY, "ADEQUATE"), SelectionRule(B2_POLICY, "ADEQUATE")],
    ids=["V2", "B1", "B2"],
)
def test_v2_b1_and_b2_cycles_are_not_gated(gated, rule):
    """Older rules keep their path byte for byte: an open breaker refuses their reviews, each
    decision is NEEDS_REVIEW ``CIRCUIT_OPEN`` with its evidence task (their recovery loop), and
    no RESEARCH_REVIEWS_DEFERRED is written."""
    cycle = cycle_for(gated, rule)
    cycle_id = submit_v3(cycle, SCRIPT, gated.venue.now)
    trip(gated)
    sync(gated)
    asyncio.run(cycle.tick(cycle_id))
    decisions = bodies(cycle, cycle_id, "RESEARCH_DECISION")
    assert len(decisions) == len(SCRIPT)
    assert {(d["disposition"], d["reason"]) for d in decisions} == {("NEEDS_REVIEW",
                                                                      "CIRCUIT_OPEN")}
    assert len(bodies(cycle, cycle_id, "RESEARCH_EVIDENCE_TASK")) == len(SCRIPT)
    assert {(r["outcome"], r["error_code"]) for r in research_requests(gated, cycle_id)} == {
        ("CIRCUIT_OPEN", "CIRCUIT_OPEN")}
    assert gated.calls == [] and not deferred(gated, cycle_id)


def test_the_gate_names_and_the_deferral_key():
    scope = "s" * 64
    assert (topk.REVIEW_GATE, topk.DEFERRED_KIND) == (GATE, DEFERRED)
    assert topk.deferral_key_for("c-1", scope, 3) == f"research:c-1:reviews-deferred:{scope}:3"
