"""Shared fixture builders for package learning-app's tests (fixture evidence only).

Disposable PostgreSQL databases (``er``/``mx``), a fixture universe and schedule, and fixture bar
readers. No broker, provider, network or owner-ledger contact.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab.authorization import RiskRepository
from catalyst_lab.learning_intake import LearningIntake, LearningPolicy
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.research_report_v3 import UniverseSnapshot
from tests.test_managed_service import FixtureCycle
from tests.test_research_report_v3 import AGENTS, LEGACY, OPERATOR, SCHEDULE, STATUS, agent_block

NOW = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)  # 08:30 in New York, 30 minutes after the run.
UNIVERSE = ("BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD")


def source(source_id="src-1", *, published_at=None, retrieved_at=None, excerpt=None):
    return {
        "source_id": source_id, "url": "https://example.com/news/item",
        "excerpt": excerpt or "The exchange listed the token for spot trading today.",
        "retrieved_at": (retrieved_at or NOW - timedelta(minutes=5)).isoformat(),
        "published_at": published_at.isoformat() if published_at else None,
    }


def coin(symbol, direction="UP", confidence="0.7", **changes):
    value = {"symbol": symbol, "direction": direction, "confidence": confidence,
             "expected_move_pct": "3.5",
             "reasons": [{"kind": "TECHNICAL", "text": "Held the 4-hour support twice.",
                          "source": None}]}
    value.update(changes)
    return value


def skipped(symbol, reason="No usable bars this morning."):
    return {"symbol": symbol, "direction": "SKIPPED", "skip_reason": reason}


def outlook(*, now=NOW, agent_id="claude", coins=None, **changes):
    slot = SCHEDULE.latest_at_or_before(now)
    body = {
        "schema_version": "MARKET_OUTLOOK_V1",
        "outlook_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "run_slot": SCHEDULE.local(slot).isoformat(),
        "horizon_hours": 24,
        "agent": agent_block(agent_id),
        "market": {
            "summary": "Risk appetite is steady; Bitcoin holds its range ahead of the data.",
            "btc": {"direction": "UP", "confidence": "0.6"},
            "eth": {"direction": "FLAT", "confidence": "0.5"},
            "factors": [{"name": "US CPI", "note": "Released 08:30 ET, in line.",
                         "source": source("cpi-1")}],
            "events": [{"at": (now + timedelta(hours=6)).isoformat(),
                        "what": "Token unlock of 2% of supply.", "source": None}],
        },
        "coins": coins if coins is not None else [
            coin("BTC/USD", "UP", "0.7"), coin("ETH/USD", "FLAT", "0.5"),
            coin("SOL/USD", "DOWN", "0.65"), skipped("DOGE/USD"),
        ],
    }
    body.update(changes)
    return body


def post_mortem(items, *, now=NOW, agent_id="claude", **changes):
    body = {"schema_version": "POST_MORTEM_V1", "note_id": str(uuid4()),
            "generated_at": now.isoformat(), "agent": agent_block(agent_id), "items": items}
    body.update(changes)
    return body


def mover_item(symbol, day, *, cause="COIN_NEWS", knowable=True, sources=None, **changes):
    value = {"subject": {"kind": "MOVER", "symbol": symbol, "day": day}, "cause": cause,
             "knowable_before_move": knowable,
             "summary": "The listing was announced before the move started.",
             "sources": [source("listing-1", published_at=NOW - timedelta(days=1, hours=6),
                                retrieved_at=NOW - timedelta(minutes=10))]
             if sources is None else sources,
             "pre_move_technicals": "Tight 4-hour range under resistance; volume rising."}
    value.update(changes)
    return value


def trade_item(setup_id, *, cause="NO_NEWS", knowable=None, sources=(), **changes):
    value = {"subject": {"kind": "TRADE", "setup_id": str(setup_id)}, "cause": cause,
             "knowable_before_move": knowable, "summary": "The setup failed on its own.",
             "sources": list(sources), "pre_move_technicals": "Pullback into a held swing low."}
    value.update(changes)
    return value


def intake_for(store, now, *, universe=UNIVERSE, schedule=SCHEDULE):
    def snapshot():
        return UniverseSnapshot(frozenset(universe), now[0], "LAB_FIXTURE_UNIVERSE")
    return LearningIntake(store, clock=lambda: now[0],
                          policy=LearningPolicy(max_age_seconds=60, schedule=schedule),
                          universe=snapshot)


def client_for(store, intake):
    return TestClient(create_managed_app(
        FixtureCycle(store.repo, store), store, api_token=LEGACY, runtime_status=lambda: {},
        status_token=STATUS, operator_token=OPERATOR, agent_tokens=AGENTS,
        learning_intake=intake))


def record_reality(store, day, movers, **body):
    """A recorded MARKET_REALITY event as the nightly job writes it (only what intake reads)."""
    record = {"reality_version": "MARKET_REALITY_V1", "day": day, "movers": movers,
              "grades": [], "coins": [], **body}
    with store.transaction() as conn:
        return store.event(conn, "MARKET_REALITY", record, key=f"market-reality:{day}")


def mover(symbol, return_pct, move_start_at, *, top_up=False, top_down=False):
    return {"symbol": symbol, "return_pct": return_pct,
            "move_start_at": move_start_at.isoformat(), "top_up": top_up,
            "top_down": top_down, "big_move": abs(D(return_pct)) >= 5,
            "calls": {}, "missed_by": []}


@pytest.fixture
def learning(er):
    """A managed store on its own disposable database with the learning intake on a clock."""
    store = ManagedStore(RiskRepository(
        er.database_url.replace("user=catalyst_app", "user=catalyst_risk")))
    now = [NOW]
    intake = intake_for(store, now)
    return SimpleNamespace(store=store, now=now, intake=intake,
                           client=client_for(store, intake))


def bearer(token):
    return {"Authorization": "Bearer " + token}


class FakeBars:
    """Canned public bars by symbol and timeframe; ``fail`` symbols raise like a transport."""

    def __init__(self, minutes=None, hours=None, fail=()):
        self.minutes, self.hours, self.fail = minutes or {}, hours or {}, set(fail)
        self.calls = []

    def bars(self, symbol, start, end, timeframe):
        self.calls.append((symbol, timeframe, start, end))
        if symbol in self.fail:
            raise RuntimeError("PUBLIC_BAR_CONNECTION_ERROR")
        rows = (self.minutes if timeframe == "1Min" else self.hours).get(symbol, [])
        return [r for r in rows if start <= datetime.fromisoformat(r["t"]) < end]

    def minute_bars(self, symbol, start, end):
        return self.bars(symbol, start, end, "1Min")

    def close(self):
        self.closed = True


def minute_rows(start, closes, *, spread="0"):
    """One 1-minute bar per close, each opening at the previous close (the first at its own
    close), high and low the larger and smaller of open and close widened by ``spread``."""
    rows, previous = [], None
    for index, close in enumerate(closes):
        close = D(str(close))
        open_ = previous if previous is not None else close
        high, low = max(open_, close) + D(spread), min(open_, close) - D(spread)
        rows.append({"t": (start + timedelta(minutes=index)).isoformat(), "o": str(open_),
                     "h": str(high), "l": str(low), "c": str(close), "v": "1"})
        previous = close
    return rows


def hour_rows(start, hours, volume="10", price="100"):
    return [{"t": (start + timedelta(hours=h)).isoformat(), "o": price, "h": price, "l": price,
             "c": price, "v": volume, "vw": price} for h in range(hours)]


# --- A scored day on the fixture paper venue ----------------------------------------------------


def close_attributed(mx, symbol, agent_id="claude", *, verified=True):
    """An agent's crypto trade, entered at max entry and sold at its target, with verified
    (non-fixture) fee evidence for both fills when ``verified``."""
    from catalyst_lab.managed_analytics import import_fill_cost_correction
    from tests.test_agent_identity import attributed, fixture_agent
    from tests.test_managed_analytics import correction, fills
    from tests.test_managed_execution import observation

    engine, venue, _ = mx
    raw = attributed(mx, symbol, fixture_agent(agent_id, "1.0"))
    sid = engine.admit(raw)
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    entry = venue.orders_of("buy")[-1]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    reached = observation(mx, trade_price="111", bid="111", ask="111.01")
    engine.manage(sid, reached)
    engine.manage(sid, reached)
    target = venue.orders_of("sell", "market")[-1]
    engine.ingest(venue.fill(target["id"], target["qty"], price="111"))
    engine.manage(sid, reached)
    assert engine._load(sid)[1]["state"] == "CLOSED"
    if verified:
        for fill in fills(engine, sid):
            import_fill_cost_correction(
                engine.store, correction(fill, venue.now, fee_usd="0.05",
                                         source="BROKER_STATEMENT"), recorded_at=venue.now)
    return sid, raw


def v3_cycle(store, raw, *, run_slot, agent_id="claude", extra_picks=(), rejected=()):
    """Report-V3 research events around an admitted fixture packet ``raw``: the start (with
    intake results), V3 packets, the top-K ranking and the selection, as intake writes them."""
    cycle_id = raw["cycle_id"]
    agent = agent_block(agent_id)
    picks = [{"item_key": raw["item_key"], "symbol": raw["symbol"], "kind": "CHART",
              "status": "RANKED", "rank": 1, "levels": raw["levels"],
              "why_over_peers": "Chosen among this run's rule-A 4-hour setups.",
              "timeframe_seconds": 14400, "price": "100.50"}, *extra_picks]
    results = [{"index": i, "status": "ACCEPTED", "item_key": p["item_key"]}
               for i, p in enumerate(picks)]
    results += [{"index": len(picks) + i, "status": "REJECTED", "code": code}
                for i, code in enumerate(rejected)]
    with store.transaction() as conn:
        store.event(conn, "RESEARCH_STARTED", {
            "cycle_id": cycle_id, "report_schema_version": "AGENT_RESEARCH_REPORT_V3",
            "run_slot": run_slot.isoformat(), "agent": agent, "item_results": results,
            "submitted_count": len(results), "contender_count": len(picks),
            "research_origin": "EXTERNAL_RESEARCH_AGENT"}, key=f"research:{cycle_id}:start")
        for pick in picks:
            store.event(conn, "RESEARCH_PACKET", {
                "cycle_id": cycle_id, "item_key": pick["item_key"], "revision": 1,
                "signal_id": pick["item_key"], "market": "CRYPTO", "symbol": pick["symbol"],
                "agent": agent, "selection_policy": "JEV_TOP_K_SELECTION_V2",
                "levels": pick["levels"], "created_at": run_slot.isoformat(),
                "expires_at": (run_slot + timedelta(hours=20)).isoformat(),
                "report_schema_version": "AGENT_RESEARCH_REPORT_V3",
                "run_slot": run_slot.isoformat(),
                "state": {"kind": pick["kind"], "agent_current_price": pick["price"],
                          "agent_price_at": run_slot.isoformat(),
                          "technical_context": {"observed_facts": {"observations": {
                              "timeframe_seconds": pick.get("timeframe_seconds")}}}},
                "selection_rationale": {"why_over_peers": pick["why_over_peers"],
                                        "agent_confidence": {"level": "MEDIUM",
                                                             "basis": "fixture basis"}},
            }, key=f"research:{cycle_id}:{pick['item_key']}:1:v3-packet")
        entries = [{"item_key": p["item_key"], "revision": 1, "rank": p.get("rank"),
                    "status": p["status"], "veto_reasons": p.get("veto_reasons", []),
                    "reason": p.get("reason"), "symbol": p["symbol"], "kind": p["kind"],
                    "agent_rank": i + 1} for i, p in enumerate(picks)]
        store.event(conn, "RESEARCH_RANKING", {
            "policy": "JEV_TOP_K_SELECTION_V2", "cycle_id": cycle_id,
            "run_slot": run_slot.isoformat(), "k": 10, "entries": entries, "complete": True,
            "counts": {}}, key=f"research:ranking:{cycle_id}")
        store.event(conn, "RESEARCH_SELECTED", {"cycle_id": cycle_id, "packet": {
            **{k: v for k, v in raw.items() if k != "selection_event_seq"}, "agent": agent,
            "report_schema_version": "AGENT_RESEARCH_REPORT_V3"}})
    return cycle_id


def shadow(store, cycle_id, item_key, *, net_r, triggered=True, outcome="TARGET",
           symbol="BTC/USD", selected=True, agent_id="claude"):
    body = {"pick": {"cycle_id": cycle_id, "item_key": item_key, "revision": 1,
                     "symbol": symbol, "selected": selected, "agent_id": agent_id,
                     "engineering": False},
            "outcome": {"outcome": outcome, "triggered": triggered, "data_complete": True,
                        "net_r": net_r, "gross_r": net_r}}
    with store.transaction() as conn:
        store.event(conn, "PICK_SHADOW_OUTCOME", body,
                    key=f"pick-shadow-outcome:{cycle_id}:{item_key}:1")
