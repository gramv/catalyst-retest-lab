"""Second-Jev context -> genuine receipt -> bounded paper amendment integration."""

import asyncio
import copy
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import JEV_MODEL, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_review import CONTEXT_VERSION, CompletedBar, ManagedContext, ManagedPolicy
from catalyst_lab.position_monitor import PositionMonitor
from tests import managed_dossier_fixtures as fx
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import admit_enter, observation
from tests.test_managed_execution import mx as mx


def opened(mx, symbol="SPY"):
    engine, venue, _ = mx
    sid, _ = admit_enter(mx, symbol)
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    return sid


def bars(mx):
    now = mx[1].now
    return (
        CompletedBar(
            str(uuid4()),
            now - timedelta(minutes=2),
            now - timedelta(minutes=1),
            D(104),
            D(115),
            D(102),
            D(105),
            D(2000),
            "LAB_FIXTURE",
            "FIXTURE",
        ),
    )


def monitor(mx, action="TIGHTEN_STOP", hook=None, policy=None):
    """A monitor with a fixture provider; ``policy`` defaults to the pre-3.2 V2 profile."""
    engine, venue, store = mx
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        if hook:
            hook()
        chosen = {
            "thesis_status": "INTACT",
            "action": action,
            "stop_option": "STOP_1" if action in {"TIGHTEN_STOP", "TIGHTEN_AND_EXTEND"} else "KEEP",
            "target_option": "TARGET_1"
            if action in {"EXTEND_TARGET", "TIGHTEN_AND_EXTEND"}
            else "KEEP",
        }
        answers = {
            key: {
                "type": "choice",
                "choice": chosen[key],
                "confidence": 0.8,
                "probabilities": {option: float(option == chosen[key]) for option in q["criteria"]},
            }
            for key, q in body["questions"].items()
        }
        return httpx.Response(
            200,
            json={
                "model": JEV_MODEL,
                "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    reviewer = JevReviewer(
        store,
        ReliabilityPolicy("MANAGED_MONITOR_FIXTURE", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(provider),
        key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now,
    )
    return PositionMonitor(
        engine,
        reviewer,
        policy=policy or ManagedPolicy(5, 10, 5, 20, 4),
        review_seconds=10,
        clock=lambda: venue.now,
        management_reviews="ENABLED",
    ), calls


def context_from_ledger(engine, sid):
    with engine.repo.connect() as conn:
        row = conn.execute(
            """SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind='POSITION_REVIEW_REQUEST' ORDER BY event_seq DESC LIMIT 1""",
            (sid,),
        ).fetchone()
    body = row["body"]
    return ManagedContext(encoded(body["context"]), body["context_hash"])


@pytest.mark.parametrize(
    "action,patches",
    [
        ("TIGHTEN_STOP", 1),
        ("EXTEND_TARGET", 1),
        ("TIGHTEN_AND_EXTEND", 2),
    ],
)
def test_second_jev_context_receipt_risk_patch_and_replacement_tracking(mx, action, patches):
    engine, venue, store = mx
    sid = opened(mx)
    watcher, calls = monitor(mx, action)
    observed = observation(mx, bid="108", ask="108.01")
    completed = bars(mx)
    result = asyncio.run(
        watcher.review(sid, observed, completed, fresh_observation=lambda: observed)
    )
    assert result.status == "RECORDED" and len(result.receipt_ids) == 1
    assert store.verify(result.receipt_ids[0])["valid"]
    assert len(calls) == 1
    assert calls[0]["state"]["thesis"]
    assert calls[0]["state"]["original_fill_price"] == "100"
    assert calls[0]["state"]["eligible_options"]["stop"][0]["price"] == "102"
    assert not any(k in encoded(calls[0]) for k in ("account_number", "account_id", "secret"))
    engine.manage(sid, observed)
    assert sum(method == "PATCH" for method, _, _ in venue.calls) == patches
    # Next mechanical tick discovers the newly assigned broker IDs, never claims
    # replacement acknowledgement alone means the old order could not have filled.
    engine.manage(sid, observed)
    assert not venue.orders_of("sell", "market")
    assert engine.reconcile()["clean"]
    assert (
        asyncio.run(watcher.review(sid, observed, completed, fresh_observation=lambda: observed))
        is None
    )
    assert len(calls) == 1
    with engine.repo.connect() as conn:
        claims = conn.execute("""SELECT count(*) AS n FROM lab.managed_risk_decisions d
            JOIN lab.managed_claims USING(decision_id) WHERE d.action='AMEND'""").fetchone()["n"]
    assert claims == patches and verify_events(engine.repo.export_events())["valid"]


def test_forged_answers_with_genuine_hold_receipt_do_not_patch(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    watcher, _ = monitor(mx, "HOLD")
    observed = observation(mx, bid="108", ask="108.01")
    original = asyncio.run(
        watcher.review(sid, observed, bars(mx), fresh_observation=lambda: observed)
    )
    ctx = context_from_ledger(engine, sid)
    answers = copy.deepcopy(original.answers)
    for key, chosen in (("action", "TIGHTEN_STOP"), ("stop_option", "STOP_1")):
        answers[key]["choice"] = chosen
        answers[key]["probabilities"] = {
            k: float(k == chosen) for k in answers[key]["probabilities"]
        }
    forged = replace(original, answers=answers)
    assert engine.accept_management(sid, ctx, forged, watcher.snapshot(sid, observed)) is False
    engine.manage(sid, observed)
    assert not any(method == "PATCH" for method, _, _ in venue.calls)


def test_expired_or_late_review_leaves_broker_protection_unchanged(mx):
    engine, venue, _ = mx
    sid = opened(mx)

    def advance_time():
        venue.now += timedelta(seconds=11)

    watcher, _ = monitor(mx, "TIGHTEN_STOP", advance_time)
    before = {o["id"]: o.get("stop_price") for o in venue.orders_of("sell", "stop")}
    result = asyncio.run(
        watcher.review(
            sid,
            observation(mx, bid="108", ask="108.01"),
            bars(mx),
            fresh_observation=lambda: observation(mx, bid="108", ask="108.01"),
        )
    )
    assert result.receipt_ids
    engine.manage(sid, observation(mx, bid="108", ask="108.01"))
    assert not any(method == "PATCH" for method, _, _ in venue.calls)
    assert {o["id"]: o.get("stop_price") for o in venue.orders_of("sell", "stop")} == before


def test_closed_position_while_model_runs_cannot_amend_or_reopen(mx):
    engine, venue, _ = mx
    sid = opened(mx)
    stop = venue.orders_of("sell", "stop")[0]
    target = venue.orders_of("sell", "limit")[0]

    def closed():
        engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
        stop["status"] = "canceled"
        engine.manage(sid, observation(mx))

    watcher, _ = monitor(mx, "TIGHTEN_STOP", closed)
    result = asyncio.run(
        watcher.review(
            sid,
            observation(mx, bid="108", ask="108.01"),
            bars(mx),
            fresh_observation=lambda: observation(mx, bid="108", ask="108.01"),
        )
    )
    assert result.receipt_ids
    assert engine._load(sid)[1]["state"] == "CLOSED"
    assert not any(method == "PATCH" for method, _, _ in venue.calls)
    assert len(venue.orders_of("buy")) == 1


def test_jev_outage_does_not_disable_mechanical_crypto_target_exit(mx):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    watcher, _ = monitor(mx)
    watcher.reviewer._transport = httpx.MockTransport(lambda request: httpx.Response(529))
    observed = observation(mx, bid="108", ask="108.01")
    result = asyncio.run(
        watcher.review(sid, observed, bars(mx), fresh_observation=lambda: observed)
    )
    assert result.status == "NEEDS_REVIEW"
    engine.manage(sid, observation(mx, bid="111", ask="111.01"))
    engine.manage(sid, observation(mx, bid="111", ask="111.01"))
    assert venue.orders_of("sell", "market")


def test_crypto_second_jev_tightens_native_stop_and_extends_local_target(mx):
    engine, venue, _ = mx
    sid = opened(mx, "BTC/USD")
    watcher, _ = monitor(mx, "TIGHTEN_AND_EXTEND")
    observed = observation(mx, bid="108", ask="108.01")
    original = venue.orders_of("sell", "stop_limit")[0]
    reviewed = asyncio.run(
        watcher.review(sid, observed, bars(mx), fresh_observation=lambda: observed)
    )
    assert reviewed.status == "RECORDED"
    engine.manage(sid, observed)
    assert original["status"] == "canceled"
    engine.manage(sid, observed)
    stops = venue.orders_of("sell", "stop_limit")
    assert len(stops) == 2 and D(stops[-1]["stop_price"]) == D(102)
    assert D(stops[-1]["qty"]) == D(original["qty"])
    assert D(engine._load(sid)[1]["target"]) == D(115)
    assert not any(method == "PATCH" for method, _, _ in venue.calls)
    assert not venue.orders_of("sell", "market")
    assert len(venue.orders_of("buy")) == 1


def test_v3_production_window_review_fits_budget_and_patches(mx):
    """The runtime's 60-bar window, passed as recent and structural, with the V3 profile."""
    from catalyst_lab.managed_runtime import engineering_monitor_policy

    engine, venue, store = mx
    sid = opened(mx)
    policy = engineering_monitor_policy()
    watcher, calls = monitor(mx, "TIGHTEN_AND_EXTEND", policy=policy)
    observed = observation(mx, bid="108", ask="108.01")
    window = fx.monitor_bars(venue.now)
    result = asyncio.run(watcher.review(
        sid, observed, window, fresh_observation=lambda: observed, structural_bars=window,
    ))
    assert result.status == "RECORDED" and len(calls) == 1
    state = calls[0]["state"]
    assert state["context_version"] == CONTEXT_VERSION
    assert len(encoded(state).encode()) <= policy.state_byte_budget
    assert len(state["bars"]["rows"]) == 60 and state["bars"]["omitted_rows"] == 0
    stop = state["eligible_options"]["stop"][0]
    target = state["eligible_options"]["target"][0]
    assert (stop["price"], stop["bar"], target["price"], target["bar"]) == (
        "107.10", "R20", "113.20", "S39",
    )
    assert state["original_research"]["levels"]["stop"] == "95"
    assert state["computed_position"]["initial_risk_per_unit"] == "5"
    context = context_from_ledger(engine, sid)
    manifest, retained = context.data["manifest"], context.data["retained"]
    assert manifest["within_budget"] and manifest["state_bytes"] == len(encoded(state))
    assert retained["selection_receipts"]["eligibility"] == engine._load(sid)[0]["record_json"][
        "receipt_id"]
    with engine.repo.connect() as conn:
        bars_row = conn.execute(
            "SELECT kind,body FROM lab.managed_events WHERE event_seq=%s",
            (retained["market_bars"]["event_seq"],),
        ).fetchone()
    assert bars_row["kind"] == "POSITION_REVIEW_BARS" and len(bars_row["body"]["bars"]) == 60
    assert retained["market_bars"]["sha256"] == digest(encoded(bars_row["body"]))
    backing = [e for e in manifest["entries"] if e["section"] == "eligible_options"][0]
    persisted = {b["observation_id"] for b in bars_row["body"]["bars"]}
    assert {s["observation_id"] for s in backing["sources"].values()} <= persisted
    engine.manage(sid, observed)
    assert sum(method == "PATCH" for method, _, _ in venue.calls) == 2
    assert D(engine._load(sid)[1]["stop"]) == D("107.10")
    assert D(engine._load(sid)[1]["target"]) == D("113.20")
    assert verify_events(engine.repo.export_events())["valid"]
