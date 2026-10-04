"""RESEARCH_RUN_SUPERSESSION_V2 under RESEARCH_SCHEDULE_V2 (package research-loop-app).

docs/RESEARCH-LOOP-V2.md 3.2: a report-V3 setup still WATCHING, or a V3 selection still offered
for admission, answering run r is superseded only by a published V3 selection of a later run
that is a full (daily) run, whatever its symbol, or is for the same symbol, whatever the run's
kind. Under RESEARCH_SCHEDULE_V1 (or without a schedule) RESEARCH_RUN_SUPERSESSION_V1 applies
exactly as before (tests/test_system_check.py).

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue, a mock
Jev transport, a fixture universe and a mock Alpaca market-data transport behind the real
read-only market source. No broker, provider, network or owner-ledger contact.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from catalyst_lab import system_check as sc
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_app import AppSettings, create_application
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_ranking import QUALITY
from catalyst_lab.research_schedule import ResearchSchedule
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_managed_runtime import reviewed_cycle
from tests.test_research_cycle import quality_response, response
from tests.test_research_report_v3 import cycle_of, report_v3, v3_intake
from tests.test_selection_b1 import classify, no_sleep
from tests.test_system_check import (
    HOURLY,
    at_price,
    bodies,
    event_count,
    publish_v3,
    rows,
    state,
    stream,
    system_runtime,
    two_slots,
    v3_pick,
)
from tests.test_system_check import market as market

V1_RULE, V2_RULE = "RESEARCH_RUN_SUPERSESSION_V1", "RESEARCH_RUN_SUPERSESSION_V2"
SUPERSEDED = "SUPERSEDED_BY_NEW_RESEARCH"


def daily_at(slot):
    """RESEARCH_SCHEDULE_V2 over HOURLY's runs whose full daily run is ``slot``'s hour."""
    return ResearchSchedule("UTC", HOURLY.runs, HOURLY.grace_minutes,
                            daily=slot.astimezone(UTC).strftime("%H:%M"))


def rejected_v3(mx, picks, *, run_slot):
    """A report-V3 cycle Jev reviews and rejects: no RESEARCH_SELECTED is published."""
    engine, venue, receipts = mx

    def reject(request):
        body = strict_json(request.content)
        quality = set(body["questions"]) == set(QUALITY.questions)
        return httpx.Response(200, json=quality_response() if quality
                              else response(verdict="REJECT"))

    reviewer = JevReviewer(
        receipts, ReliabilityPolicy("SUPERSESSION_V2_FIXTURE", 10, 2, 0.01, 1000, 30),
        transport=httpx.MockTransport(reject), key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now, sleep=no_sleep,
    )
    cycle = ResearchCycle(engine.repo, reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    raw = report_v3(picks, now=venue.now, run_slot=run_slot.isoformat(),
                    valid_until=(venue.now + timedelta(minutes=30)).isoformat())
    assert cycle.start_report(raw, max_seconds=86400, v3=v3_intake(
        universe={p["symbol"] for p in picks}, schedule=HOURLY, now=venue.now)
    )["contender_count"] == len(picks)
    asyncio.run(cycle.tick(cycle_of(raw)))
    assert cycle.approved_packets(cycle_of(raw)) == []


def live(mx):
    return at_price("100.49", "100.51", at=mx[1].now)


def revoke_of(engine, setup_id):
    [row] = [r for r in rows(engine, "REVOKE") if r["setup_id"] == setup_id]
    return row


# --- The rule, pure ----------------------------------------------------------------------------


def test_the_configured_schedule_names_the_version_and_the_rule_reads_kinds_and_symbols():
    slot = datetime(2026, 9, 29, 12, tzinfo=UTC)  # 08:00 New York.
    loop = ResearchSchedule("America/New_York", tuple(f"{h:02d}:00" for h in range(0, 24, 2)),
                            60, daily="08:00")
    v1 = ResearchSchedule("America/New_York", loop.runs, 60)
    assert sc.supersession_version(loop) == V2_RULE
    assert sc.supersession_version(v1) == sc.supersession_version(None) == V1_RULE
    update, later, tomorrow = (slot + timedelta(hours=2), slot + timedelta(hours=4),
                               slot + timedelta(days=1))
    newer = [(update, "AAA/USD"), (later, "AAA/USD"), (later, "BBB/USD")]
    # The same coin: the latest later run of either kind supersedes it.
    assert sc.superseding_run_slot(loop, slot, "AAA/USD", newer) == later
    assert sc.superseding_run_slot(loop, update, "AAA/USD", newer) == later
    # Another coin of an update run supersedes nothing; nor does the pick's own run.
    assert sc.superseding_run_slot(loop, slot, "CCC/USD", newer) is None
    assert sc.superseding_run_slot(loop, later, "AAA/USD", newer) is None
    # The next daily run supersedes every older pick, whatever its coin.
    assert sc.superseding_run_slot(loop, later, "CCC/USD", [*newer, (tomorrow, "ZZZ/USD")]) == (
        tomorrow)


# --- The runtime pass and admission --------------------------------------------------------------


def test_v2_an_update_run_supersedes_only_the_same_coin(mx, market):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    engine.configure_research_schedule(daily_at(old))  # ``old`` is the daily run.
    daily = publish_v3(mx, [v3_pick(i, s, now) for i, s in enumerate(
        ("AAA/USD", "BBB/USD", "CCC/USD"))], run_slot=old)
    adjusted_sid = engine.admit(daily["AAA/USD"], live_quote=live(mx))
    kept_sid = engine.admit(daily["BBB/USD"], live_quote=live(mx))
    run = system_runtime(mx, market, ["AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD"])
    run._retire_superseded_research()  # Nothing newer yet.
    assert rows(engine, "REVOKE") == [] and bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []
    # The update run: an adjusted AAA and a new coin.
    update = publish_v3(mx, [v3_pick(0, "AAA/USD", now), v3_pick(1, "DDD/USD", now)],
                        run_slot=new)
    before = event_count(engine)
    run._retire_superseded_research()
    assert event_count(engine) == before + 2  # One REVOKE and its INVALIDATED revision.
    revoke = revoke_of(engine, adjusted_sid)
    assert revoke["body"] == {"reason": SUPERSEDED, "run_slot": daily["AAA/USD"]["run_slot"],
                              "superseded_by_run_slot": new.isoformat(),
                              "supersession_rule": V2_RULE}
    assert revoke["idempotency_key"] == f"research:superseded:setup:{adjusted_sid}"
    retired = state(engine, adjusted_sid)
    assert (retired["state"], retired["revoked"], retired["revocation_reason"]) == (
        "INVALIDATED", True, SUPERSEDED)
    # Another coin of the daily run, watching or not yet admitted, stays until its expiry.
    assert state(engine, kept_sid)["state"] == "WATCHING"
    assert not state(engine, kept_sid).get("revoked")
    assert bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []
    assert {p["symbol"] for p in run._selected_packets()} == {"CCC/USD", "AAA/USD", "DDD/USD"}
    # The next tick admits the adjusted pick, the new coin and the daily run's waiting pick.
    for symbol in ("AAA/USD", "CCC/USD", "DDD/USD"):
        stream(run, symbol, at=venue.now)
    run.execution_once()
    watching = {s["symbol"]: s for s in engine.store.active()}
    assert set(watching) == {"AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD"}
    assert str(watching["AAA/USD"]["receipt_id"]) == update["AAA/USD"]["receipt_id"]
    assert bodies(engine, "RUNTIME_ADMISSION_REFUSED") == [] and run.error is None
    assert len(rows(engine, "REVOKE")) == 1
    assert verify_events(engine.repo.export_events())["valid"]


def test_v2_a_full_run_supersedes_every_older_pick_and_nothing_else(mx, market):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    engine.configure_research_schedule(daily_at(new))  # ``new`` is the next daily run.
    previous = publish_v3(mx, [v3_pick(i, s, now) for i, s in enumerate(
        ("AAA/USD", "BBB/USD", "CCC/USD"))], run_slot=old)
    watching = engine.admit(previous["AAA/USD"], live_quote=live(mx))
    opened = engine.admit(previous["CCC/USD"], live_quote=live(mx))
    touch = observation(mx, trade_price="100", bid="99.99", ask="100.01")
    assert engine.observe_trigger(opened, touch)["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == "CCC/USD")
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(opened, observation(mx))
    assert state(engine, opened)["state"] == "OPEN"
    # A report-V2 setup watching: never a V3 pick, never superseded.
    v2_cycle, v2_cycle_id = reviewed_cycle(mx, 1)
    v2_packet = v2_cycle.approved_packets(v2_cycle_id)[0]
    classify(engine, v2_packet["symbol"])
    v2_setup = engine.admit(v2_packet)
    run = system_runtime(mx, market, ["AAA/USD", "BBB/USD", "CCC/USD", "EEE/USD"])
    publish_v3(mx, [v3_pick(9, "EEE/USD", now)], run_slot=new)
    before = event_count(engine)
    run._retire_superseded_research()
    assert event_count(engine) == before + 3  # REVOKE, INVALIDATED, one decline.
    evidence = {"superseded_by_run_slot": new.isoformat(), "supersession_rule": V2_RULE}
    assert revoke_of(engine, watching)["body"] == {
        "reason": SUPERSEDED, "run_slot": previous["AAA/USD"]["run_slot"], **evidence}
    [declined] = bodies(engine, "RESEARCH_ADMISSION_DECLINED")
    pending = previous["BBB/USD"]
    assert declined == {"cycle_id": pending["cycle_id"], "item_key": pending["item_key"],
                        "revision": 1, "receipt_id": pending["receipt_id"],
                        "selection_event_seq": pending["selection_event_seq"],
                        "reason": SUPERSEDED, "run_slot": pending["run_slot"], **evidence}
    assert state(engine, opened)["state"] == "OPEN" and not state(engine, opened).get("revoked")
    assert state(engine, v2_setup)["state"] == "WATCHING"
    run._retire_superseded_research()  # Each retirement is written once.
    assert event_count(engine) == before + 3 and run.error is None


def test_v2_an_adjusted_pick_jev_does_not_select_supersedes_nothing(mx, market):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    engine.configure_research_schedule(daily_at(old))
    daily = publish_v3(mx, [v3_pick(0, "AAA/USD", now)], run_slot=old)
    sid = engine.admit(daily["AAA/USD"], live_quote=live(mx))
    run = system_runtime(mx, market, ["AAA/USD"])
    rejected_v3(mx, [v3_pick(0, "AAA/USD", now)], run_slot=new)  # Jev rejects the adjustment.
    before = event_count(engine)
    run._retire_superseded_research()
    assert event_count(engine) == before  # Nothing retired, nothing written.
    assert state(engine, sid)["state"] == "WATCHING" and not state(engine, sid).get("revoked")
    assert rows(engine, "REVOKE") == [] and bodies(engine, "RESEARCH_ADMISSION_DECLINED") == []
    assert run.error is None


def test_admission_refuses_exactly_what_the_configured_version_supersedes(mx):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    daily = publish_v3(mx, [v3_pick(i, s, now) for i, s in enumerate(
        ("AAA/USD", "BBB/USD", "CCC/USD"))], run_slot=old)
    publish_v3(mx, [v3_pick(5, "AAA/USD", now)], run_slot=new)  # An update run's adjusted AAA.
    engine.configure_research_schedule(daily_at(old))
    with pytest.raises(sc.AdmissionRefused) as caught:
        engine.admit(daily["AAA/USD"], live_quote=live(mx))
    assert caught.value.code == SUPERSEDED
    assert caught.value.details == {"run_slot": daily["AAA/USD"]["run_slot"],
                                    "superseded_by_run_slot": new.isoformat(),
                                    "supersession_rule": V2_RULE}
    # Under V2 another coin of the daily run is admitted after the update run.
    assert state(engine, engine.admit(daily["BBB/USD"], live_quote=live(mx)))["state"] == (
        "WATCHING")
    # Under V1 (configured or absent) any newer run refuses it, exactly as before.
    for schedule in (HOURLY, None):
        engine.configure_research_schedule(schedule)
        with pytest.raises(sc.AdmissionRefused) as caught:
            engine.admit(daily["CCC/USD"], live_quote=live(mx))
        assert caught.value.details == {"run_slot": daily["CCC/USD"]["run_slot"],
                                        "superseded_by_run_slot": new.isoformat(),
                                        "supersession_rule": V1_RULE}
    with pytest.raises(ValueError, match="^EXPLICIT_RESEARCH_SCHEDULE_REQUIRED$"):
        engine.configure_research_schedule({"timezone": "UTC", "runs": ["08:00"]})


def test_a_v1_schedule_keeps_supersession_v1_and_never_defers_admission(mx, market):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    engine.configure_research_schedule(HOURLY)
    previous = publish_v3(mx, [v3_pick(i, s, now) for i, s in enumerate(
        ("AAA/USD", "BBB/USD"))], run_slot=old)
    watching = engine.admit(previous["AAA/USD"], live_quote=live(mx))
    run = system_runtime(mx, market, ["AAA/USD", "BBB/USD", "EEE/USD"])
    publish_v3(mx, [v3_pick(9, "EEE/USD", now)], run_slot=new)
    run._retire_superseded_research()
    assert run._supersession_checked is None
    evidence = {"superseded_by_run_slot": new.isoformat(), "supersession_rule": V1_RULE}
    assert revoke_of(engine, watching)["body"] == {
        "reason": SUPERSEDED, "run_slot": previous["AAA/USD"]["run_slot"], **evidence}
    [declined] = bodies(engine, "RESEARCH_ADMISSION_DECLINED")
    assert (declined["reason"], declined["supersession_rule"]) == (SUPERSEDED, V1_RULE)


def test_v2_a_selection_published_during_a_tick_waits_for_its_supersession_pass(mx, market):
    """The pass reads, the adjusted pick is published, then admission runs: the pick waits a
    tick instead of meeting the setup it replaces (ACTIVE_SYMBOL_ALREADY_MANAGED)."""
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    engine.configure_research_schedule(daily_at(old))
    daily = publish_v3(mx, [v3_pick(0, "AAA/USD", now)], run_slot=old)
    sid = engine.admit(daily["AAA/USD"], live_quote=live(mx))
    run = system_runtime(mx, market, ["AAA/USD"])
    stream(run, "AAA/USD", at=venue.now)
    retire, published = run._retire_superseded_research, []

    def retire_then_publish(tick=None):
        retire(tick)
        if not published:
            published.append(publish_v3(mx, [v3_pick(1, "AAA/USD", now)], run_slot=new))

    run._retire_superseded_research = retire_then_publish
    run.execution_once()
    assert published and run._supersession_checked == frozenset()
    assert state(engine, sid)["state"] == "WATCHING"
    assert bodies(engine, "RUNTIME_ADMISSION_REFUSED") == []  # Not tried in this tick.
    run.execution_once()  # Its pass retires the old setup, then admission takes the new pick.
    assert state(engine, sid)["revocation_reason"] == SUPERSEDED
    [setup] = engine.store.active()
    assert str(setup["receipt_id"]) == published[0]["AAA/USD"]["receipt_id"]
    assert bodies(engine, "RUNTIME_ADMISSION_REFUSED") == [] and run.error is None


def test_v2_a_tick_without_v3_work_adds_no_ledger_read():
    from tests.test_managed_runtime import ready, runtime

    run = runtime()  # A watching V2-style setup and no selection: nothing a run could retire.
    run.execution.research_schedule = daily_at(datetime(2026, 9, 29, 12, tzinfo=UTC))

    def connect():
        raise AssertionError("NO_LEDGER_READ_EXPECTED")

    run.execution.repo = SimpleNamespace(connect=connect)
    ready(run)
    run.execution_once()
    assert run.error is None and run.execution.managed and not run.latches.blocking()
    assert run._supersession_checked == frozenset()


def test_v2_a_failed_pass_admits_no_v3_selection(mx, market, monkeypatch):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    engine.configure_research_schedule(daily_at(old))
    publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)
    run = system_runtime(mx, market, ["AAA/USD"])
    stream(run, "AAA/USD", at=venue.now)

    def broken(conn, after):
        raise RuntimeError("fixture ledger read fault")

    monkeypatch.setattr("catalyst_lab.managed_runtime.newer_v3_selections", broken)
    run.execution_once()
    assert engine.store.active() == [] and run._supersession_checked == frozenset()
    assert run.error is not None  # Latched like a failed V1 pass; protection still ran.


# --- The app wires the configured schedule -------------------------------------------------------


class Closing:
    def __init__(self):
        self.policy = SimpleNamespace(batch_size=5)

    def close(self):
        pass


def test_the_app_gives_the_execution_the_schedule_intake_uses(mx):
    engine, venue, _ = mx
    loop = ResearchSchedule("America/New_York", tuple(f"{h:02d}:00" for h in range(0, 24, 2)),
                            60, daily="08:00")
    runtime = SimpleNamespace(
        execution=engine, research=SimpleNamespace(repo=engine.repo), now=lambda: venue.now,
        credentials=AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-provider-secret"),
        source=Closing(), start=lambda: None, stop=lambda: None, status=lambda: {},
    )
    settings = AppSettings(8799, "fixture-supersession-legacy-token-abcdefghij", 86400, (),
                           CLASSIFICATION_POLICY, (), research_schedule=loop)
    app = create_application(runtime, settings)
    assert engine.research_schedule is loop
    assert app.state.research_context.schedule is loop
    create_application(runtime, AppSettings(8799, settings.token, 86400, (),
                                            CLASSIFICATION_POLICY, ()))
    assert engine.research_schedule is None  # Without the setting: V1, as before.
