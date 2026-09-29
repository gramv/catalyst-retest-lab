"""Crypto price-grid isolation: off-grid levels never leave a filled position unprotected.

Disposable PostgreSQL, the fake paper venue and fixture Jev only; no network or real broker.
"""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from catalyst_lab import managed_eligibility, managed_execution
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.managed_eligibility import validate_managed_eligibility
from catalyst_lab.market import MarketDataError
from catalyst_lab.research_cycle import ResearchIntake
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_jev_review import FIXTURE_KEY
from tests.test_managed_app import NOW as REPORT_NOW
from tests.test_managed_app import body as report_body
from tests.test_managed_eligibility import NOW as ELIGIBILITY_NOW
from tests.test_managed_eligibility import PROFILE, EvidenceBroker
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_research_cycle import POLICY as CYCLE_POLICY
from tests.test_research_reports import reply

# Research proposals may carry 12 decimals; the fixture venue's grid is 0.01.
OFF_GRID = {
    "entry_trigger": "100",
    "max_entry_price": "100.10",
    "stop": "95.005",
    "target": "111.005",
}


def reviewed_packet(mx, levels, symbol="BTC/USD"):
    """`tests.test_managed_execution.packet` with explicit levels; same review path."""
    engine, venue, reviews = mx
    now = venue.now
    excerpt = "Synthetic verified release for test only."
    raw = {
        "cycle_id": str(uuid4()),
        "item_key": "CRYPTO:" + symbol,
        "revision": 1,
        "symbol": symbol,
        "market": "CRYPTO",
        "levels": dict(levels),
        "thesis": "Synthetic new product with supported demand; engineering fixture only.",
        "disproof": "Synthetic product withdrawal.",
        "sources": [
            {
                "source_id": "fixture-release",
                "url": "https://example.org/fixture",
                "excerpt": excerpt,
                "content_hash": digest(excerpt),
                "retrieved_at": now.isoformat(),
            }
        ],
        "expires_at": (now + timedelta(minutes=20)).isoformat(),
        "review_valid_until": (now + timedelta(seconds=60)).isoformat(),
    }
    state = {k: raw[k] for k in ("market", "symbol", "levels", "thesis", "disproof", "sources")}
    raw["state"] = copy.deepcopy(state)
    raw["evidence_hash"] = digest(encoded(state))
    with engine.store.transaction() as conn:
        engine.store.event(conn, "RESEARCH_PACKET", copy.deepcopy(raw))
    reviewer = JevReviewer(
        reviews,
        ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 1, 0.25, 1000, 30),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply())),
        key_provider=lambda: FIXTURE_KEY,
    )
    result = asyncio.run(
        reviewer.jev_review(
            request_id=uuid4(),
            identity={
                "cycle_id": raw["cycle_id"],
                "candidate_revision": 1,
                "research_item_key": raw["item_key"],
                "evidence_hash": raw["evidence_hash"],
            },
            state=state,
            question_set=SKEPTIC,
            expires_at=now + timedelta(seconds=10),
            purpose="ENGINEERING_TEST",
        )
    )
    assert result.status == "RECORDED"
    raw["receipt_id"] = result.receipt_ids[0]
    with engine.store.transaction() as conn:
        event = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": copy.deepcopy(raw)})
        classification = system_event(
            engine.repo,
            conn,
            "CLASSIFICATION_IMPORTED",
            {"ticker": symbol, "source": "LAB_FIXTURE"},
        )
        conn.execute(
            "INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
            (classification["seq"], symbol, symbol, symbol, "LAB_FIXTURE"),
        )
    raw["selection_event_seq"] = event["event_seq"]
    return raw


def asset_reads(venue):
    return sum(m == "GET" and path.startswith("/v2/assets/") for m, path, _ in venue.calls)


def events(engine, kind, setup_id=None):
    with engine.repo.connect() as conn:
        rows = conn.execute(
            """SELECT body FROM lab.managed_events WHERE kind=%s
            AND setup_id IS NOT DISTINCT FROM %s ORDER BY event_seq""",
            (kind, setup_id),
        ).fetchall()
    return [row["body"] for row in rows]


def without_grid_checks(monkeypatch):
    """Simulate a setup admitted and entered before any grid check existed."""
    for module in (managed_execution, managed_eligibility):
        monkeypatch.setattr(module, "off_grid_levels", lambda *args: {})


@pytest.fixture
def legacy(mx, monkeypatch):
    engine, venue, _ = mx
    without_grid_checks(monkeypatch)
    sid = engine.admit(reviewed_packet(mx, OFF_GRID))
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"
    entry = venue.orders_of("buy")[0]
    assert entry["limit_price"] == "100.10"
    assert engine.ingest(venue.fill(entry["id"], entry["qty"]))
    return sid


# Admission


def test_off_grid_levels_refused_at_admission_once_with_recorded_evidence(mx):
    engine, venue, _ = mx
    p = reviewed_packet(mx, OFF_GRID)
    before = asset_reads(venue)
    with pytest.raises(ValueError, match="^CRYPTO_LEVEL_OFF_PRICE_GRID$"):
        engine.admit(p)
    assert asset_reads(venue) == before + 1
    # The runtime retries every tick; the recorded refusal answers without a broker read.
    with pytest.raises(ValueError, match="^CRYPTO_LEVEL_OFF_PRICE_GRID$"):
        engine.admit(p)
    assert asset_reads(venue) == before + 1
    assert events(engine, "CRYPTO_ADMISSION_REFUSED") == [
        {
            "reason": "CRYPTO_LEVEL_OFF_PRICE_GRID",
            "receipt_id": str(p["receipt_id"]),
            "selection_event_seq": p["selection_event_seq"],
            "cycle_id": p["cycle_id"],
            "symbol": "BTC/USD",
            "price_increment": "0.01",
            "off_grid_levels": {"stop": "95.005", "target": "111.005"},
        }
    ]
    assert events(engine, "CRYPTO_ASSET_METADATA") == [
        {"symbol": "BTC/USD", "price_increment": "0.01", "source": "ALPACA_PAPER_ASSET"}
    ]
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.managed_setups").fetchone()
    assert not venue.orders
    assert verify_events(engine.repo.export_events())["valid"]


def test_on_grid_admission_reads_grid_once_and_replay_needs_no_read(mx):
    engine, venue, _ = mx
    p = packet(mx)
    before = asset_reads(venue)
    sid = engine.admit(p)
    assert asset_reads(venue) == before + 1
    assert engine.admit(p) == sid and asset_reads(venue) == before + 1
    assert engine._load(sid)[1]["state"] == "WATCHING"
    assert not events(engine, "CRYPTO_ADMISSION_REFUSED")


def test_ledger_refusal_comes_before_any_broker_read(mx):
    engine, venue, _ = mx
    p = reviewed_packet(mx, OFF_GRID)
    with engine.store.transaction() as conn:
        engine._latch_execution_halt(conn, "FIXTURE_OPERATOR_HALT", {"fixture": True})
    before = asset_reads(venue)
    with pytest.raises(ValueError, match="^RISK_HALT$"):
        engine.admit(p)
    assert asset_reads(venue) == before


def test_missing_price_metadata_is_final_but_a_transport_failure_is_retried(mx, monkeypatch):
    engine, _, _ = mx
    reads = []

    def unavailable(symbol):
        reads.append(symbol)
        raise MarketDataError("ALPACA_HTTP_429")

    def incomplete(symbol):
        reads.append(symbol)
        return {"symbol": symbol, "class": "crypto", "status": "active", "tradable": True}

    p = packet(mx)
    monkeypatch.setattr(engine.broker, "asset", unavailable)
    with pytest.raises(MarketDataError):
        engine.admit(p)
    assert not events(engine, "CRYPTO_ADMISSION_REFUSED")
    monkeypatch.setattr(engine.broker, "asset", incomplete)
    for _ in range(2):
        with pytest.raises(ValueError, match="^CRYPTO_PRECISION_UNAVAILABLE$"):
            engine.admit(p)
    assert reads == ["BTC/USD", "BTC/USD"]


# Entry


def test_entry_eligibility_refuses_off_grid_crypto_levels_with_code():
    broker = EvidenceBroker()
    candidate = {"market": "CRYPTO", "symbol": "BTC/USD", "levels": dict(OFF_GRID)}
    result = validate_managed_eligibility(broker, candidate, ELIGIBILITY_NOW, PROFILE)
    assert not result["passed"] and result["reason"] == "CRYPTO_LEVEL_OFF_PRICE_GRID"
    assert result["evidence"]["price_increment"] == "0.01" and broker.calls == ["asset"]
    on_grid = {**candidate, "levels": {**OFF_GRID, "stop": "95.01", "target": "111.01"}}
    assert validate_managed_eligibility(broker, on_grid, ELIGIBILITY_NOW, PROFILE)["passed"]


def test_setup_admitted_before_the_check_is_refused_at_entry_with_code(mx, monkeypatch):
    engine, venue, _ = mx
    monkeypatch.setattr(managed_execution, "off_grid_levels", lambda *args: {})
    sid = engine.admit(reviewed_packet(mx, OFF_GRID))
    monkeypatch.undo()
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "REJECTED" and decision["reason"] == "CRYPTO_LEVEL_OFF_PRICE_GRID"
    assert engine._load(sid)[1]["state"] == "RISK_REJECTED" and not venue.orders_of("buy")


@pytest.mark.parametrize(
    "fault,reason",
    [("metadata", "CRYPTO_INCREMENT_METADATA_REQUIRED"), ("limit", "PRICE_OFF_BROKER_INCREMENT")],
)
def test_crypto_execution_error_at_entry_becomes_risk_rejected(mx, monkeypatch, fault, reason):
    engine, venue, _ = mx
    if fault == "limit":
        without_grid_checks(monkeypatch)
        sid = engine.admit(reviewed_packet(mx, {**OFF_GRID, "max_entry_price": "100.105"}))
    else:
        sid = engine.admit(packet(mx))
        real, reads = engine.broker.asset, []

        def asset(symbol):
            # Eligibility's read is complete; the entry's own later read lacks an increment.
            reads.append(symbol)
            data = real(symbol)
            return data if len(reads) == 1 else {
                k: v for k, v in data.items() if k != "min_trade_increment"
            }

        monkeypatch.setattr(engine.broker, "asset", asset)
    decision = engine.observe_trigger(sid, observation(mx))
    assert decision["outcome"] == "REJECTED" and decision["reason"] == reason
    state = engine._load(sid)[1]
    assert state["state"] == "RISK_REJECTED" and state["reason"] == reason
    assert not venue.orders_of("buy")
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.managed_active_reservations").fetchone()


# Protection and exits for a filled legacy off-grid position


def test_legacy_off_grid_position_gets_raised_native_stop_recorded(mx, legacy):
    engine, venue, _ = mx
    plan = engine.manage(legacy, observation(mx))
    assert plan.state == "PROTECTION_REQUIRED"
    stop = venue.orders_of("sell", "stop_limit")[0]
    assert (stop["stop_price"], stop["limit_price"]) == ("95.01", "95.00")
    assert engine._load(legacy)[1]["stop"] == "95.005"  # Desired level is not rewritten.
    assert events(engine, "PROTECTION_PLAN", legacy)[-1]["details"] == {
        "stop_snapped_to_grid": True,
        "requested_stop": "95.005",
        "requested_stop_limit": "94.995",
        "price_increment": "0.01",
        "native_stop": "95.01",
        "native_stop_limit": "95.00",
    }
    assert engine.manage(legacy, observation(mx)).state == "PROTECTED"
    assert len(venue.orders_of("sell", "stop_limit")) == 1
    with engine.repo.connect() as conn:
        assert not conn.execute("SELECT 1 FROM lab.execution_halts").fetchone()
    assert verify_events(engine.repo.export_events())["valid"]


def test_legacy_off_grid_target_exit_closes_position(mx, legacy):
    engine, venue, _ = mx
    engine.manage(legacy, observation(mx))
    touched = observation(mx, bid="111.005", ask="111.02")
    assert engine.manage(legacy, touched).reason == "TARGET_EXIT"
    assert venue.orders_of("sell", "stop_limit")[0]["status"] == "canceled"
    engine.manage(legacy, touched)
    closing = venue.orders_of("sell", "market")[0]
    assert engine._load(legacy)[1]["exit_requested"] == "TARGET_EXIT"
    engine.ingest(venue.fill(closing["id"], closing["qty"], price="111"))
    engine.manage(legacy, observation(mx))
    assert engine._load(legacy)[1]["state"] == "CLOSED"


def test_legacy_off_grid_time_exit(mx, legacy):
    engine, venue, _ = mx
    engine.manage(legacy, observation(mx))
    with engine.store.transaction() as conn:
        state = engine.store.state(conn, legacy)
        engine.store.transition(conn, legacy, state["state"], hard_exit_at=venue.now.isoformat())
    assert engine.manage(legacy, observation(mx)).reason == "TIME_EXIT"
    engine.manage(legacy, observation(mx))
    assert engine._load(legacy)[1]["exit_requested"] == "TIME_EXIT"
    assert venue.orders_of("sell", "market")


def test_legacy_off_grid_local_stop_exits_after_grace(mx, legacy):
    engine, venue, _ = mx
    engine.manage(legacy, observation(mx))
    engine.manage(legacy, observation(mx, bid="95", ask="95.01"))
    venue.now += timedelta(seconds=3)
    assert engine.manage(legacy, observation(mx, bid="95", ask="95.01")).reason == (
        "STOP_LIMIT_NOT_FILLED"
    )
    engine.manage(legacy, observation(mx, bid="95", ask="95.01"))
    assert engine._load(legacy)[1]["exit_requested"] == "STOP_LIMIT_NOT_FILLED"
    assert venue.orders_of("sell", "market")


def test_unsnappable_stop_halts_entries_without_exception_and_position_still_exits(mx, legacy):
    engine, venue, _ = mx
    plan = engine.manage(legacy, observation(mx, bid="95.008", ask="95.02"))
    assert plan.state == "HALTED" and plan.reason == "CRYPTO_STOP_UNSNAPPABLE"
    assert not venue.orders_of("sell") and engine.reconciled_at is None
    with engine.repo.connect() as conn:
        halt = conn.execute("SELECT * FROM lab.execution_halts").fetchone()
    assert halt["reason"] == "MANAGED_CRYPTO_CRYPTO_STOP_UNSNAPPABLE"
    assert halt["payload_json"]["native_stop"] == "95.01"
    assert halt["payload_json"]["bid"] == "95.008"
    state = engine._load(legacy)[1]
    assert state["protection_state"] == "HALTED"
    assert state["last_error"] == "CRYPTO_STOP_UNSNAPPABLE"
    with pytest.raises(ValueError, match="^RISK_HALT$"):
        engine.admit(packet(mx, "ETH/USD"))
    # The latch blocks entries only; the local stop still exits the position.
    engine.manage(legacy, observation(mx, bid="95", ask="95.01"))
    venue.now += timedelta(seconds=3)
    assert engine.manage(legacy, observation(mx, bid="95", ask="95.01")).reason == (
        "STOP_LIMIT_NOT_FILLED"
    )
    assert venue.orders_of("sell", "market")


def test_on_grid_crypto_protection_is_unchanged_and_records_no_grid_details(mx):
    engine, venue, _ = mx
    sid = engine.admit(packet(mx))
    engine.observe_trigger(sid, observation(mx))
    entry = venue.orders_of("buy")[0]
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    stop = venue.orders_of("sell", "stop_limit")[0]
    assert (stop["stop_price"], stop["limit_price"]) == ("95", "94.99")
    assert events(engine, "PROTECTION_PLAN", sid) == [
        {"state": "PROTECTION_REQUIRED", "reason": "UNCOVERED_BROKER_INVENTORY"}
    ]


# Intake


def muse_report(symbol, **levels):
    raw = report_body()
    raw["items"][0].update(
        market="CRYPTO",
        symbol=symbol,
        levels={
            "entry_trigger": "106",
            "max_entry_price": "106.1",
            "stop": "104",
            "target": "111",
            **levels,
        },
    )
    return raw


def test_intake_refuses_off_grid_crypto_item_against_recorded_metadata(mx):
    engine, _, _ = mx
    intake = ResearchIntake(engine.repo, CYCLE_POLICY, clock=lambda: REPORT_NOW)
    # No recorded metadata yet: accepted, and admission performs the live check.
    unknown = muse_report("ETH/USD", stop="104.005")
    first = intake.start_report(unknown, max_seconds=300)
    engine.admit(packet(mx))  # Records BTC/USD's live broker increment.
    engine.admit(packet(mx, "ETH/USD"))
    off = muse_report("BTC/USD", stop="104.005")
    with pytest.raises(ValueError, match="^CRYPTO_LEVEL_OFF_PRICE_GRID$"):
        intake.start_report(off, max_seconds=300)
    assert intake.outputs(off["report_id"]) == []
    assert intake.start_report(muse_report("BTC/USD"), max_seconds=300)["contender_count"] == 1
    # An already accepted report still replays idempotently.
    replay = intake.start_report(unknown, max_seconds=300)
    assert replay["idempotent_replay"] and replay["cycle_id"] == first["cycle_id"]
    stock = report_body()
    stock["items"][0]["levels"]["stop"] = "104.005"  # Stock tick rules are out of scope.
    assert intake.start_report(stock, max_seconds=300)["contender_count"] == 1
