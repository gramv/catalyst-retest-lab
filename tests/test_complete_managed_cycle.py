"""Complete local engineering pipeline; real DB, fake Jev and paper venue transport.

The selection packets here are produced by ResearchCycle itself, never hand-built
admission fixtures. All provider interactions are intercepted by MockTransport.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_ranking import QUALITY
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_position_monitor import bars as management_bars
from tests.test_position_monitor import monitor
from tests.test_research_cycle import quality_response, response


def external_muse_item(index, now):
    crypto = index % 2 == 1
    return {
        "signal_id": f"TEST-MUSE-{index:02}",
        "market": "CRYPTO" if crypto else "US_STOCKS",
        "symbol": f"CRYPT{index:02}/USD" if crypto else f"TEST{index:02}",
        "direction": "LONG", "catalyst": "PRODUCT",
        "thesis": "Fixture new product availability supports a technical retest hypothesis.",
        "disproof": "Issuer withdrawal or loss of structural support invalidates the thesis.",
        "economic_relationship": "Fixture customers buy the issuer product.",
        "technical_analysis": "External Muse research proposes the observed support and target.",
        "levels": {"entry_trigger": "105", "max_entry_price": "105.16",
                   "stop": "103.9", "target": "110"},
        "sources": [{"source_id": "issuer-source", "url": "https://issuer.example/announcement",
                     "excerpt": "Fixture issuer reports a new product available to customers.",
                     "published_at": (now - timedelta(hours=1)).isoformat(),
                     "retrieved_at": now.isoformat()}],
    }


def run_complete_cycle(mx, *, count=20):
    """Run the exact tested pipeline and return JSON-safe, clearly simulated proof.

    The caller supplies the isolated integration fixture; this helper never loads
    credentials or opens a network connection. Audit rows remain in engine.repo
    for an engineering proof runner to export before fixture cleanup. Every item
    is approved; a comparative QUALITY cutoff is expected only above the limit.
    """
    engine, venue, receipts = mx
    now = venue.now
    limit = 10
    items = [external_muse_item(i, now) for i in range(count)]
    provider_calls, skeptic_calls, quality_calls = [], [], []

    def selection_provider(request):
        provider_calls.append(request.content)
        body = strict_json(request.content)
        if set(body["questions"]) == set(QUALITY.questions):
            quality_calls.append(body)
            symbol = body["state"]["candidate"]["symbol"]
            index = int(symbol.split("/")[0][-2:])
            return httpx.Response(200, json=quality_response(2 if index < 10 else 1))
        skeptic_calls.append(body)
        return httpx.Response(200, json=response())

    reviewer = JevReviewer(
        receipts,
        ReliabilityPolicy("COMPLETE_CYCLE_FIXTURE", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(selection_provider),
        key_provider=lambda: FIXTURE_KEY,
        clock=lambda: venue.now,
    )
    cycle = ResearchCycle(
        engine.repo, reviewer, CyclePolicy(limit, 10, 15, 60, 30), clock=lambda: venue.now
    )
    cycle_id = str(uuid4())
    receipt = cycle.start_report({
        "report_id": cycle_id, "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=5)).isoformat(), "items": items,
    }, max_seconds=300)
    assert receipt["contender_count"] == count and not receipt["trade_authorized"]
    decisions = asyncio.run(cycle.tick(cycle_id))
    ranked = count > limit
    assert len(decisions) == len(skeptic_calls) == count
    assert len(quality_calls) == (count if ranked else 0)
    assert all(d["disposition"] == "APPROVED" for d in decisions)
    chosen = cycle.approved_packets(cycle_id)
    assert len(chosen) == min(count, limit)
    assert {p["market"] for p in chosen} == {"US_STOCKS", "CRYPTO"}
    with engine.repo.connect() as conn:
        # Every published packet, not just the two executed below, passes admission SQL.
        assert [
            conn.execute(
                "SELECT lab.managed_review_failure(%s) AS reason", (Jsonb(json_safe(p)),)
            ).fetchone()["reason"]
            for p in chosen
        ] == [None] * len(chosen)
    assert all(
        p["quality_required"] is ranked and p["selection_limit"] == limit for p in chosen
    )
    if ranked:
        assert [p["quality_rank"] for p in chosen] == list(range(1, limit + 1))
        assert {p["quality_candidate_count"] for p in chosen} == {count}
    else:
        assert not any("quality_receipt_id" in p for p in chosen)
    assert not venue.orders
    selected = [
        next(p for p in chosen if p["market"] == market) for market in ("US_STOCKS", "CRYPTO")
    ]
    setups = []
    for p in selected:
        receipts.verify(p["receipt_id"])
        with engine.store.transaction() as conn:
            event = system_event(
                engine.repo,
                conn,
                "CLASSIFICATION_IMPORTED",
                {
                    "ticker": p["symbol"],
                    "source": "COMPLETE_CYCLE_FIXTURE",
                },
            )
            conn.execute(
                "INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                (
                    event["seq"],
                    p["symbol"],
                    p["symbol"],
                    p["symbol"],
                    "COMPLETE_CYCLE_FIXTURE",
                ),
            )
        sid = engine.admit(p)  # Exact real ResearchCycle output crosses the risk SQL boundary.
        levels = p["levels"]
        trigger = D(levels["entry_trigger"])
        observed = observation(
            mx, trade_price=str(trigger), bid=str(trigger - D(".01")), ask=str(trigger + D(".01"))
        )
        risk = engine.observe_trigger(sid, observed)
        assert risk["outcome"] == "APPROVED"
        entry = next(o for o in venue.orders_of("buy") if o["symbol"] == p["symbol"])
        assert entry["client_order_id"] == risk["payload"]["client_order_id"]
        assert D(entry["limit_price"]) == D(levels["max_entry_price"])
        assert engine.ingest(venue.fill(entry["id"], entry["qty"], price=entry["limit_price"]))
        engine.manage(sid, observed)
        assert engine._load(sid)[1]["state"] == "OPEN"
        setups.append((sid, p, entry))

    with engine.repo.connect() as conn:
        assert conn.execute(
            "SELECT sum(budget) AS n FROM lab.account_risk_reservations"
        ).fetchone()["n"] == D(200)
    lifecycles = []
    for sid, p, entry in setups:
        watcher, calls = monitor(mx, "TIGHTEN_AND_EXTEND")
        observed = observation(mx, bid="108", ask="108.01")
        # This completed post-entry bar supplies a higher observed stop and target.
        completed = tuple(
            replace(b, low=D(106), close=D(108), open=D(107)) for b in management_bars(mx)
        )
        result = asyncio.run(
            watcher.review(
                sid,
                observed,
                completed,
                fresh_observation=lambda observed=observed: observed,
            )
        )
        assert result.status == "RECORDED" and len(calls) == 1
        assert receipts.verify(result.receipt_ids[0])["valid"]
        engine.manage(sid, observed)
        engine.manage(sid, observed)  # Crypto: terminal cancel -> native replacement.
        state = engine._load(sid)[1]
        assert D(state["stop"]) == D(106) and D(state["target"]) == D(115)
        if p["market"] == "US_STOCKS":
            targets = [
                o
                for o in venue.orders_of("sell", "limit")
                if o["symbol"] == p["symbol"] and o["status"] == "new"
            ]
            target = targets[0]
            engine.ingest(venue.fill(target["id"], target["qty"], price="115"))
            for stop in venue.orders_of("sell", "stop"):
                if stop["symbol"] == p["symbol"] and stop["status"] == "new":
                    stop["status"] = "canceled"  # Native bracket sibling cancel.
            engine.manage(sid, observation(mx, bid="115", ask="115.01"))
        else:
            reached = observation(mx, bid="115", ask="115.01")
            engine.manage(sid, reached)
            engine.manage(sid, reached)
            close = [o for o in venue.orders_of("sell", "market") if o["symbol"] == p["symbol"]][0]
            engine.ingest(venue.fill(close["id"], close["qty"], price="115"))
            engine.manage(sid, reached)
        assert engine._load(sid)[1]["state"] == "CLOSED"
        lifecycles.append(
            {
                "setup_id": sid,
                "market": p["market"],
                "symbol": p["symbol"],
                "selection_receipt_id": p["receipt_id"],
                "management_receipt_id": result.receipt_ids[0],
                "quantity": entry["qty"],
                "entry_fill_price": entry["limit_price"],
                "exit_fill_price": "115",
                "original_levels": p["levels"],
                "managed_stop": "106",
                "managed_target": "115",
                "state": "CLOSED",
            }
        )
    assert venue._position_rows() == []
    reconciliation = engine.reconcile()
    assert reconciliation["clean"]
    total_receipts = len(provider_calls) + len(lifecycles)
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.account_risk_reservations").fetchone()
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_fills").fetchone()["n"] == 4
        assert (
            conn.execute("SELECT count(*) AS n FROM lab.jev_receipts").fetchone()["n"]
            == total_receipts
        )
        claim_count = conn.execute("SELECT count(*) AS n FROM lab.managed_claims").fetchone()["n"]
        assert claim_count >= 8
    checkpoint = verify_events(engine.repo.export_events())
    assert checkpoint["valid"] and checkpoint["event_count"] > 10 * count
    return {
        "proof_kind": "ENGINEERING_FIXTURE_ONLY",
        "providers": "MOCKED_JEV_AND_ALPACA_PAPER",
        "database": "ISOLATED_REAL_POSTGRES",
        "market_data": "SYNTHETIC_ENGINEERING_FIXTURE",
        "external_orders_created": 0,
        "cycle_id": cycle_id,
        "observed_at": now.isoformat(),
        "muse_contender_count": len(items),
        "research_origin": "EXTERNAL_MUSE",
        "research_receipt_count": len(provider_calls),
        "skeptic_receipt_count": len(skeptic_calls),
        "quality_receipt_count": len(quality_calls),
        "selected_count": len(chosen),
        "selected_symbols": [p["symbol"] for p in chosen],
        "management_receipt_count": len(lifecycles),
        "total_receipt_count": total_receipts,
        "fill_count": 4,
        "risk_claim_count": claim_count,
        "lifecycles": lifecycles,
        "broker_position_count": 0,
        "active_risk_reservation_count": 0,
        "reconciliation_clean": reconciliation["clean"],
        "audit": checkpoint,
    }


def test_twenty_muse_reports_ten_selected_execution_monitor_exit_and_audit(mx):
    proof = run_complete_cycle(mx)
    assert len(proof["lifecycles"]) == 2
    assert proof["research_receipt_count"] == 40 and proof["total_receipt_count"] == 42


def test_eight_approvals_admit_without_quality(mx):
    # 4 US + 4 crypto: supply below capacity needs no comparative cutoff, yet the stock
    # and crypto selections still cross lab.managed_review_failure to a closed trade.
    proof = run_complete_cycle(mx, count=8)
    assert proof["skeptic_receipt_count"] == 8 and proof["quality_receipt_count"] == 0
    assert proof["selected_count"] == 8
    assert {lifecycle["market"] for lifecycle in proof["lifecycles"]} == {"US_STOCKS", "CRYPTO"}
    assert all(lifecycle["state"] == "CLOSED" for lifecycle in proof["lifecycles"])
    assert proof["broker_position_count"] == proof["active_risk_reservation_count"] == 0


@pytest.mark.parametrize("count,quality_calls", [(10, 0), (11, 11)])
def test_selection_limit_boundaries_admit_through_review_sql(mx, count, quality_calls):
    # Exactly at the limit: no QUALITY calls. One above: every approval is ranked.
    proof = run_complete_cycle(mx, count=count)
    assert proof["skeptic_receipt_count"] == count
    assert proof["quality_receipt_count"] == quality_calls
    assert proof["selected_count"] == 10
    assert len(proof["lifecycles"]) == 2 and proof["reconciliation_clean"]
