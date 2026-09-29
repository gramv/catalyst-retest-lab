"""Reconstructing and recording report-V3 pick outcomes against a real (disposable) ledger."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from catalyst_lab.pick_outcomes import (
    SHADOW_EVENT_KIND,
    PickRecord,
    cycle_picks,
    parse_bars,
    pick_outcome_aggregates,
    pick_outcome_page,
    rank_bucket,
    ready_at,
    record_shadow_outcome,
    run_shadow_outcome_job,
    simulate_pick,
)
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from catalyst_lab.research_selection_topk import NOT_RANKED, RANKED, VETOED
from tests.test_execution import er as er  # noqa: F401 -- ``mx`` needs this fixture in scope
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx, packet  # noqa: F401

GEN = datetime(2026, 1, 1, tzinfo=UTC)
RUN_SLOT = GEN
EXPIRES = GEN + timedelta(hours=1)


def agent_block(agent_id="claude"):
    return {"agent_id": agent_id, "agent_version": "v1", "guidelines_version": "g1",
            "guidelines_sha256": "0" * 64, "run_id": str(uuid4())}


def started(store, *, cycle_id, contender_count=3, agent_id="claude"):
    body = {
        "cycle_id": str(cycle_id), "report_schema_version": REPORT_SCHEMA_V3,
        "run_slot": RUN_SLOT.isoformat(), "agent": agent_block(agent_id),
        "contender_count": contender_count, "submitted_count": contender_count,
        "rejected_count": 0, "expires_at": EXPIRES.isoformat(),
        "research_origin": "EXTERNAL_RESEARCH_AGENT",
    }
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_STARTED", body, key=f"research:{cycle_id}:start")
    return body


def pick_packet(store, *, cycle_id, item_key, symbol, kind="CHART", levels=None,
                agent_current_price="100", agent_id="claude", revision=1, agent_rank=1,
                selection_policy="JEV_TOP_K_SELECTION_V1", generated_at=GEN, expires_at=EXPIRES):
    levels = levels or {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
                        "target": "111"}
    body = {
        "cycle_id": str(cycle_id), "item_key": item_key, "signal_id": item_key,
        "revision": revision, "market": "CRYPTO", "symbol": symbol, "rank": agent_rank,
        "agent": agent_block(agent_id), "selection_policy": selection_policy,
        "levels": levels,
        "state": {"market": "CRYPTO", "symbol": symbol, "kind": kind,
                  "agent_current_price": agent_current_price,
                  "agent_price_at": generated_at.isoformat()},
        "created_at": generated_at.isoformat(), "expires_at": expires_at.isoformat(),
        "evidence_hash": "fixture-hash-" + item_key, "report_schema_version": REPORT_SCHEMA_V3,
        "run_slot": RUN_SLOT.isoformat(),
    }
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_PACKET", body,
                   key=f"research:{cycle_id}:{item_key}:{revision}:packet")
    return body


def ranking(store, *, cycle_id, entries, k=10):
    body = {"policy": "JEV_TOP_K_SELECTION_V1", "cycle_id": str(cycle_id),
            "run_slot": RUN_SLOT.isoformat(), "k": k, "entries": entries,
            "quality_policy": "MUSE_JEV_COMPARATIVE_QUALITY_V3", "uncertain_penalty": "10",
            "complete": True, "counts": {
                RANKED: sum(e["status"] == RANKED for e in entries),
                VETOED: sum(e["status"] == VETOED for e in entries),
                NOT_RANKED: sum(e["status"] == NOT_RANKED for e in entries),
            }}
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_RANKING", body, key=f"research:ranking:{cycle_id}")
    return body


def rank_entry(item_key, *, status, rank=None, agent_rank=1, veto_reasons=(), reason=None):
    return {"item_key": item_key, "revision": 1, "rank": rank, "adjusted_score": "80.0000",
            "quality_score": "80.0000", "quality_category": "ADEQUATE", "status": status,
            "veto_reasons": list(veto_reasons), "uncertain": [], "dissent": "APPROVE",
            "receipt_id": None, "quality_receipt_id": None, "symbol": item_key,
            "kind": "CHART", "question_set_version": "CHART_PICK_QUESTIONS_V1",
            "dissent_tied": False, "agent_rank": agent_rank, "reason": reason}


def selected(store, *, cycle_id, pick_body, selection_event_seq_holder):
    # Production code's ``ResearchCycle._event`` always stamps a top-level ``cycle_id`` onto
    # every ``RESEARCH_*`` event, in addition to whatever the packet itself carries; cycle_picks
    # (like research_context.py's own reader) filters on that top-level field.
    with store.transaction() as conn:
        event = store.event(
            conn, "RESEARCH_SELECTED", {"cycle_id": str(cycle_id), "packet": pick_body}
        )
    selection_event_seq_holder.append(event["event_seq"])
    return event


def declined(store, *, selection_event_seq, reason, cycle_id, item_key, symbol):
    body = {"selection_event_seq": selection_event_seq, "reason": reason, "cycle_id": str(cycle_id),
           "item_key": item_key, "symbol": symbol, "run_slot": RUN_SLOT.isoformat()}
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_ADMISSION_DECLINED", body)
    return body


# --- cycle_picks: every fate ------------------------------------------------------------------


def test_cycle_picks_covers_vetoed_not_ranked_and_ranked_not_selected(mx):  # noqa: F811
    engine, venue, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=3)
    for key in ("CRYPTO:AAA/USD", "CRYPTO:BBB/USD", "CRYPTO:CCC/USD"):
        pick_packet(store, cycle_id=cycle_id, item_key=key, symbol=key.split(":")[1])
    ranking(store, cycle_id=cycle_id, entries=[
        rank_entry("CRYPTO:AAA/USD", status=VETOED, veto_reasons=["PRICES_CONSISTENT_NO"]),
        rank_entry("CRYPTO:BBB/USD", status=NOT_RANKED, reason="MISSING_VALID_REVIEW"),
        rank_entry("CRYPTO:CCC/USD", status=RANKED, rank=11),  # ranked, but outside top-10: never
    ])                                                          # published (no RESEARCH_SELECTED)
    picks = {p.item_key: p for p in cycle_picks(engine.repo, cycle_id)}
    assert set(picks) == {"CRYPTO:AAA/USD", "CRYPTO:BBB/USD", "CRYPTO:CCC/USD"}
    assert picks["CRYPTO:AAA/USD"].ranking_status == VETOED
    assert picks["CRYPTO:AAA/USD"].ranking_reasons == ["PRICES_CONSISTENT_NO"]
    assert picks["CRYPTO:AAA/USD"].selected is False
    assert picks["CRYPTO:BBB/USD"].ranking_status == NOT_RANKED
    assert picks["CRYPTO:BBB/USD"].ranking_reasons == ["MISSING_VALID_REVIEW"]
    assert picks["CRYPTO:CCC/USD"].ranking_status == RANKED
    assert picks["CRYPTO:CCC/USD"].selected is False
    assert picks["CRYPTO:CCC/USD"].jev_rank == 11
    for pick in picks.values():
        assert rank_bucket(pick) in {VETOED, NOT_RANKED, "NOT_SELECTED"}


def test_an_intake_rejected_item_never_appears(mx):  # noqa: F811
    engine, _, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=1)
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_ITEM_REJECTED_AT_INTAKE",
                   {"cycle_id": str(cycle_id), "index": 0, "signal_id": "sig-0",
                    "code": "PRICE_NOT_POSITIVE", "errors": [], "item_sha256": "x" * 64})
    assert cycle_picks(engine.repo, cycle_id) == []


def test_a_selected_and_admitted_pick_links_its_real_setup(mx):  # noqa: F811
    engine, venue, _ = mx
    store = engine.store
    # engine.admit's own admission-integrity check binds a packet to the exact
    # RESEARCH_SELECTED event at ``packet["selection_event_seq"]`` *and* requires the LATEST
    # RESEARCH_PACKET of this (cycle_id, item_key) to still carry a matching evidence_hash;
    # admit it right after ``packet()`` writes its own (correctly hashed) RESEARCH_PACKET,
    # before this fixture's V3-shaped tracking events reuse the same item_key and would
    # otherwise become the "latest" one admit() checks against.
    real_packet = packet(mx, "BTC/USD")
    cycle_id, item_key = real_packet["cycle_id"], real_packet["item_key"]
    sid = engine.admit(real_packet)
    started(store, cycle_id=cycle_id, contender_count=1)
    pick_body = pick_packet(store, cycle_id=cycle_id, item_key=item_key, symbol="BTC/USD")
    ranking(store, cycle_id=cycle_id, entries=[rank_entry(item_key, status=RANKED, rank=1)])
    seq_holder = []
    selected(store, cycle_id=cycle_id, pick_body={**pick_body, "rank": 1, "agent_rank": 1,
                                                  "replacement_for": None},
            selection_event_seq_holder=seq_holder)
    picks = cycle_picks(engine.repo, cycle_id)
    assert len(picks) == 1
    assert picks[0].setup_id == str(sid)
    assert picks[0].selection_status == "ADMITTED"
    assert picks[0].selected is True
    assert rank_bucket(picks[0]) == "TOP_K"


def test_a_selected_pick_declined_by_the_system_check_is_recorded(mx):  # noqa: F811
    engine, _, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=1)
    item_key = "CRYPTO:ETH/USD"
    pick_body = pick_packet(store, cycle_id=cycle_id, item_key=item_key, symbol="ETH/USD")
    ranking(store, cycle_id=cycle_id, entries=[rank_entry(item_key, status=RANKED, rank=1)])
    seq_holder = []
    selected(store, cycle_id=cycle_id, pick_body={**pick_body, "rank": 1, "agent_rank": 1,
                                                  "replacement_for": None},
            selection_event_seq_holder=seq_holder)
    declined(store, selection_event_seq=seq_holder[0], reason="PRICE_MISMATCH",
            cycle_id=cycle_id, item_key=item_key, symbol="ETH/USD")
    picks = cycle_picks(engine.repo, cycle_id)
    assert picks[0].selection_status == "DECLINED"
    assert picks[0].decline_code == "PRICE_MISMATCH"
    assert picks[0].setup_id is None


# --- run_shadow_outcome_job ---------------------------------------------------------------------


class FakeBars:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol

    def minute_bars(self, symbol, start, end):
        rows = self.rows_by_symbol.get(symbol, [])
        return [r for r in rows if start <= datetime.fromisoformat(r["t"]) < end]


def never_touch_bars(symbol, minutes=60, base=GEN):
    return {symbol: [
        {"t": (base + timedelta(minutes=m)).isoformat(), "o": "500", "h": "501", "l": "499",
         "c": "500", "v": "1"}
        for m in range(0, minutes, 10)
    ]}


def test_job_skips_a_pick_not_yet_ready_and_is_idempotent_once_recorded(mx):  # noqa: F811
    engine, _, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=1)
    item_key = "CRYPTO:SOL/USD"
    pick_packet(store, cycle_id=cycle_id, item_key=item_key, symbol="SOL/USD",
               generated_at=GEN, expires_at=EXPIRES)
    bars = FakeBars(never_touch_bars("SOL/USD"))

    early_summary = run_shadow_outcome_job(store, bars, now=EXPIRES)  # window not yet + 24h
    assert early_summary.recorded == 0 and early_summary.not_yet_ready == 1

    ready_now = ready_at(cycle_picks(engine.repo, cycle_id)[0])
    summary = run_shadow_outcome_job(store, bars, now=ready_now)
    assert summary.recorded == 1
    with engine.repo.connect() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM lab.managed_events WHERE kind=%s", (SHADOW_EVENT_KIND,)
        ).fetchone()["n"]
    assert count == 1

    replay = run_shadow_outcome_job(store, bars, now=ready_now)
    assert replay.recorded == 0 and replay.already_recorded == 1
    with engine.repo.connect() as conn:
        count_again = conn.execute(
            "SELECT count(*) AS n FROM lab.managed_events WHERE kind=%s", (SHADOW_EVENT_KIND,)
        ).fetchone()["n"]
    assert count_again == 1  # Never duplicated.


def test_job_computes_a_correct_never_triggered_outcome(mx):  # noqa: F811
    engine, _, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=1)
    item_key = "CRYPTO:DOGE/USD"
    pick_packet(store, cycle_id=cycle_id, item_key=item_key, symbol="DOGE/USD",
               levels={"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
                      "target": "111"}, generated_at=GEN, expires_at=EXPIRES)
    bars = FakeBars(never_touch_bars("DOGE/USD"))  # price never near 100
    pick = cycle_picks(engine.repo, cycle_id)[0]
    run_shadow_outcome_job(store, bars, now=ready_at(pick))
    page = pick_outcome_page(engine.repo)
    assert len(page["items"]) == 1
    outcome = page["items"][0]["shadow"]
    assert outcome["triggered"] is False
    assert outcome["outcome"] == "NEVER_TRIGGERED_VALIDITY_EXPIRED"
    assert page["items"][0]["real"] is None


def test_job_computes_a_correct_24_hour_hold_exit_at_the_fetch_boundary(mx):  # noqa: F811
    """Regression: BAR_FETCH_BUFFER. The walk's own hold deadline is ``trigger_bar.start +
    24h``; the job's fetch window ends at ``ready_at(pick) = window_end + 24h [+ buffer]``.
    With a window barely wider than the trigger bar itself (so ``window_end`` sits right on
    top of ``trigger_bar.start``, leaving no incidental slack) and the nearest usable bar a
    few minutes *after* the exact hold deadline, only the buffer reaches it: without it, the
    fetch's own ``end`` falls short of that bar and the outcome is always DATA_INCOMPLETE.
    """
    engine, _, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=1)
    item_key = "CRYPTO:HOLD/USD"
    generated_at = GEN
    expires_at = GEN + timedelta(seconds=1)  # As tight as the window can be around the trigger.
    pick_packet(store, cycle_id=cycle_id, item_key=item_key, symbol="HOLD/USD",
               levels={"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95",
                      "target": "111"}, generated_at=generated_at, expires_at=expires_at)
    pick = cycle_picks(engine.repo, cycle_id)[0]
    trigger_at = generated_at  # The window's only bar (1 second wide): triggers immediately.
    hold_deadline = trigger_at + timedelta(hours=24)  # What the walk itself needs a bar at/after.
    rows = [
        {"t": trigger_at.isoformat(), "o": "100", "h": "100.2", "l": "99", "c": "100", "v": "1"},
        # The nearest usable bar sits 2 minutes after the exact deadline: inside
        # BAR_FETCH_BUFFER (5 minutes) but well outside the window's own ~1-second slack, so
        # only the buffer -- not incidental window width -- can reach it.
        {"t": (hold_deadline + timedelta(minutes=2)).isoformat(), "o": "105", "h": "106",
         "l": "104", "c": "105", "v": "1"},
    ]
    bars = FakeBars({"HOLD/USD": rows})
    run_shadow_outcome_job(store, bars, now=ready_at(pick))
    page = pick_outcome_page(engine.repo)
    assert len(page["items"]) == 1
    outcome = page["items"][0]["shadow"]
    assert outcome["triggered"] is True
    assert outcome["outcome"] == "HOLD_24H_EXIT"
    assert outcome["data_complete"] is True  # Would be False (DATA_INCOMPLETE) before the fix.
    assert outcome["gross_r"] is not None


# --- pick_outcome_aggregates ---------------------------------------------------------------------


def test_aggregates_report_counts_never_a_bare_mean_and_bucket_by_rank(mx):  # noqa: F811
    engine, _, _ = mx
    store = engine.store
    cycle_id = uuid4()
    started(store, cycle_id=cycle_id, contender_count=2)
    vetoed_key, ranked_key = "CRYPTO:XRP/USD", "CRYPTO:ADA/USD"
    pick_packet(store, cycle_id=cycle_id, item_key=vetoed_key, symbol="XRP/USD")
    pick_packet(store, cycle_id=cycle_id, item_key=ranked_key, symbol="ADA/USD")
    ranking(store, cycle_id=cycle_id, entries=[
        rank_entry(vetoed_key, status=VETOED, veto_reasons=["SETUP_ALREADY_BROKEN_YES"]),
        rank_entry(ranked_key, status=RANKED, rank=11),
    ])
    bars = FakeBars({**never_touch_bars("XRP/USD"), **never_touch_bars("ADA/USD")})
    for pick in cycle_picks(engine.repo, cycle_id):
        run_shadow_outcome_job(store, bars, now=ready_at(pick), cycle_id=cycle_id)
    aggregates = pick_outcome_aggregates(engine.repo, group_by=("rank_bucket",))
    by_bucket = {item["rank_bucket"]: item for item in aggregates["items"]}
    assert by_bucket[VETOED]["count"] == 1
    assert by_bucket["NOT_SELECTED"]["count"] == 1
    for item in aggregates["items"]:
        assert "count" in item and isinstance(item["count"], int)
        assert item["real_count"] == 0  # Never traded: no real economics to report.


def test_aggregates_exclude_engineering_setups(mx):  # noqa: F811
    # A full ``managed_engineering`` enrollment (its own admission path, distinct from a
    # research-report pick) is out of scope here; this targets ``pick_outcome_aggregates``'s
    # own exclusion of any recorded pick whose PickRecord carries ``engineering=True`` --
    # exactly what ``cycle_picks`` sets from ``is_engineering(setup.record_json)`` when a
    # pick's admitted setup happens to be one.
    engine, _, _ = mx
    store = engine.store
    levels = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
    pick = PickRecord(
        cycle_id=str(uuid4()), run_slot=RUN_SLOT.isoformat(), item_key="CRYPTO:LTC/USD",
        revision=1, signal_id="sig-ltc", symbol="LTC/USD", kind="CHART", levels=levels,
        agent_current_price="100", agent_price_at=GEN.isoformat(), generated_at=GEN,
        window_end=EXPIRES, agent_id="claude", agent_version="v1",
        attribution="AGENT_ATTRIBUTED", selection_policy="JEV_TOP_K_SELECTION_V1",
        question_set_version=None, selected=True, selection_status="ADMITTED",
        decline_code=None, replacement_for=None, jev_rank=1, agent_rank=1,
        ranking_status=RANKED, ranking_reasons=[], review_disposition=None, review_reason=None,
        skip_reason=None, setup_id=None, arm="JEV_MANAGED", engineering=True,
    )
    bars = parse_bars(never_touch_bars("LTC/USD")["LTC/USD"])
    simulation = simulate_pick(pick.levels, bars, window_start=GEN, window_end=EXPIRES)
    with store.transaction() as conn:
        record_shadow_outcome(store, conn, pick, simulation)
    aggregates = pick_outcome_aggregates(engine.repo)
    assert aggregates["items"] == []  # The only pick recorded is engineering-only: excluded.
