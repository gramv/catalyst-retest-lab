"""Package plugin-c3: ``STRATEGY_PAPER_PATH_V1`` (a promoted mechanical strategy's signal through
the same admission, system check, trade plan, risk gate and order path as a research pick) and the
promotion gate (``STRATEGY_PROMOTION_V1`` / ``STRATEGY_DEMOTION_V1``, ``MANAGED_STRATEGIES_JSON``).

Fixture evidence only: per-test disposable PostgreSQL databases built from the real migrations
(schema 31), the fake paper venue, canned hourly bars and no Jev. No broker, provider, network or
owner-ledger contact.
"""

import io
import json
from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import cloud_runtime, managed_ops, strategies, strategy_paper, system_check
from catalyst_lab import trade_plan as tp
from catalyst_lab.account_risk import MANAGED_RISK_V5_POLICY_ID, STRATEGY_RISK_CAP
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_execution import ManagedExecution
from catalyst_lab.managed_runtime import permanent_refusal, research_v3
from catalyst_lab.strategies import core, sdk
from catalyst_lab.strategy_paper import PromotionRefused
from tests.maintenance_fixtures import (
    Bars,
    bodies,
    decisions,
    entry_order,
    fill,
    quote,
    stop_orders,
)
from tests.maintenance_fixtures import (
    events as events_of,
)
from tests.maintenance_fixtures import mt as mt  # noqa: F401
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission  # noqa: F401
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import observation
from tests.test_selection_b1 import classify
from tests.test_system_check import at_price

V5 = MANAGED_RISK_V5_POLICY_ID
SID = "TEST_PAPER_SIGNAL_V1"
pytestmark = [pytest.mark.trade_plan, pytest.mark.usefixtures("pre_jev_b1_admission")]

# A history report entry and a shadow cell that meet the ladder (history_test.promotion_check).
PASSING_ENTRY = {
    "walk_forward": {"out_of_sample": {"trades": 40, "mean_net_r": "0.21",
                                       "ci90_mean_net_r": [0.05, 0.37]}},
    "deflated_sharpe": {"walk_forward_oos": {"dsr": 0.97}}, "pbo": {"pbo": 0.2},
}
FAILING_ENTRY = {
    "walk_forward": {"out_of_sample": {"trades": 243, "mean_net_r": "-0.259",
                                       "ci90_mean_net_r": [-0.43, -0.09]}},
    "deflated_sharpe": {"walk_forward_oos": {"dsr": 0.007}}, "pbo": {"pbo": 0.36},
}
PASSING_CELL = {"label": "SHADOW", "trades": 31, "mean_net_r": "0.12", "signals": 33}


def signals(bars, context):
    """The fixture rule: a signal on every completed bar in the window that closes at or above
    100 (each one marketable at its close)."""
    return [sdk.marketable_proposal(SID, context["symbol"], bars, bar,
                                    facts={"close": str(bar.close)})
            for _, bar, _ in sdk.signal_bars(bars, context) if bar.close >= D(100)]


TEST_STRATEGY = sdk.mechanical_strategy(
    name="TEST_PAPER_SIGNAL", version=1, description="Fixture: a close at or above 100.",
    signals=signals, trigger_rule="MARKETABLE_AT_SIGNAL (fixture)", history_hours=30,
    entry_types=frozenset({core.BREAKOUT, core.IMMEDIATE}))


@pytest.fixture
def registry():
    """The registry with the fixture strategy added, restored afterwards."""
    saved = dict(strategies.REGISTRY)
    strategies.REGISTRY[SID] = TEST_STRATEGY
    yield strategies.REGISTRY
    strategies.REGISTRY.clear()
    strategies.REGISTRY.update(saved)


def report_file(tmp_path, entry, strategy_id=SID):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"run_id": "HISTORY_TEST_V1:fixture", "inputs": {
        "start": "2025-10-01", "end": "2026-10-01", "source": "fixture", "universe": ["SOL/USD"],
        "fee_model": "alpaca-taker"}, "strategies": {strategy_id: entry}}))
    return path


def engine_of(mt, *, configured=(SID,), policy=V5):  # noqa: F811
    engine, venue, reviews = mt
    built = ManagedExecution(engine.repo, engine.broker, policy=engine.policy,
                             clock=engine.now, review_store=reviews, risk_policy_id=policy,
                             trade_plan_enabled=True, paper_strategies=configured)
    assert built.reconcile()["clean"]
    return built


def to_hour(mt, seconds=120):  # noqa: F811
    """The venue clock just after the next hour boundary (the signal pass reads that hour)."""
    venue = mt[1]
    venue.now = venue.now.replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1, seconds=seconds)
    mt[0].reconciled_at = None
    assert mt[0].reconcile()["clean"]


class Reader:
    """Canned completed 1-hour bars (Alpaca's raw keys) per symbol, built at read time."""

    def __init__(self, closes):
        self.closes, self.calls, self.fail = closes, [], set()

    def bars(self, symbol, start, end, timeframe):
        self.calls.append((symbol, start, end, timeframe))
        if symbol in self.fail:
            raise RuntimeError("fixture read failure")
        rows, at = [], start
        last = self.closes.get(symbol, "99")
        while at + core.HOUR <= end:
            close = D(last) if at + core.HOUR == end else D("99")
            rows.append({"t": at.isoformat(), "o": str(close), "h": str(close + D("0.4")),
                         "l": str(close - D("0.4")), "c": str(close), "v": "10"})
            at += core.HOUR
        return rows


def source_of(engine, mt, closes, ids=(SID,), symbols=("SOL/USD",)):  # noqa: F811
    return strategy_paper.StrategySignalSource(
        engine.store, Reader(closes), tuple(ids), lambda: mt[1].now,
        engine._crypto_price_increment, universe=lambda conn, now: list(symbols))


def promote(engine, tmp_path, *, entry=PASSING_ENTRY, cell=PASSING_CELL, override=None,
            strategy_id=SID):
    return strategy_paper.promote(
        engine.store, strategy_id, history_report=report_file(tmp_path, entry, strategy_id),
        owner_ruling_ref="LAB_FIXTURE owner ruling", owner_override_reason=override,
        shadow_cell=cell, now=engine.now())


def selections(engine):
    with engine.repo.connect() as conn:
        rows = conn.execute("""SELECT event_seq, body FROM lab.managed_events
            WHERE kind='RESEARCH_SELECTED' ORDER BY event_seq""").fetchall()
    return [{**r["body"]["packet"], "selection_event_seq": r["event_seq"]} for r in rows]


def admit(engine, mt, packet, bid="99.99", ask="100.01"):  # noqa: F811
    venue = mt[1]
    return engine.admit(packet, live_quote=at_price(bid, ask, at=venue.now),
                        hourly_range=tp.HourlyRangeReader(Bars(venue), clock=lambda: venue.now))


def signalled(engine, mt, tmp_path, symbols=("SOL/USD",)):  # noqa: F811
    """Promoted, classified, one signal per coin recorded and selected; the packets."""
    promote(engine, tmp_path)
    for symbol in symbols:
        classify(engine, symbol)
    to_hour(mt)
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]
    summary = source_of(engine, mt, dict.fromkeys(symbols, "100"), symbols=symbols).run_once()
    assert summary["selected"] == len(symbols), summary
    return selections(engine)


# --- Configuration ------------------------------------------------------------------------------


def test_managed_strategies_json_is_default_off_and_strict():
    assert strategy_paper.configured_strategies({}) == ()
    assert strategy_paper.configured_strategies({"MANAGED_STRATEGIES_JSON": " "}) == ()
    assert strategy_paper.configured_strategies(
        {"MANAGED_STRATEGIES_JSON": '["BREAKOUT_7D_VOL2X_V1"]'}) == ("BREAKOUT_7D_VOL2X_V1",)
    for raw in ('"BREAKOUT_7D_VOL2X_V1"', '["breakout"]', '["A_V1","A_V1"]', "[1]", "{",
                json.dumps([f"S{i}_V1" for i in range(21)])):
        with pytest.raises(ValueError, match="MANAGED_STRATEGIES_JSON_INVALID"):
            strategy_paper.configured_strategies({"MANAGED_STRATEGIES_JSON": raw})


def test_paper_levels_are_a_collar_above_the_signal_on_the_grid():
    levels = strategy_paper.paper_levels("100", "0.01")
    assert levels == {"entry_trigger": "100.50", "max_entry_price": "100.50", "stop": "98.49",
                      "target": "104.52"}
    t, m, s, p = (D(levels[k]) for k in ("entry_trigger", "max_entry_price", "stop", "target"))
    # Admission's rule (reward/risk 2 at M) and SYSTEM_CHECK_V1's 2% minimum stop distance.
    assert p - m >= 2 * (m - s) and m - s >= D("0.02") * m and 0 < s < t <= m < p
    odd = strategy_paper.paper_levels("0.123456", "0.0001")
    assert odd["entry_trigger"] == "0.1241"  # 0.12407... rounded up to the grid.
    for bad in (("0", "0.01"), ("100", "0"), ("NaN", "0.01")):
        with pytest.raises(ValueError, match="PAPER_LEVELS_INVALID"):
            strategy_paper.paper_levels(*bad)


# --- The promotion gate -------------------------------------------------------------------------


def test_promotion_is_refused_below_the_ladder_and_an_override_is_recorded(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    with pytest.raises(PromotionRefused) as refused:
        promote(engine, tmp_path, entry=FAILING_ENTRY, cell=PASSING_CELL)
    assert refused.value.code == "PROMOTION_LADDER_NOT_MET"
    assert refused.value.evidence["promotion_check"]["rung"] == "NONE"
    with pytest.raises(PromotionRefused, match="PROMOTION_LADDER_NOT_MET"):
        promote(engine, tmp_path, cell={"trades": 4, "mean_net_r": "0.5"})  # Shadow too short.
    with pytest.raises(PromotionRefused, match="OWNER_OVERRIDE_REASON_INVALID"):
        promote(engine, tmp_path, entry=FAILING_ENTRY, override="short")
    assert bodies(engine, strategy_paper.PROMOTION_EVENT) == []
    result = promote(engine, tmp_path, entry=FAILING_ENTRY,
                     override="Owner insists: paper-test the plumbing at minimum size")
    assert result["override"] is True and result["rung"] == "NONE"
    [body] = bodies(engine, strategy_paper.PROMOTION_EVENT)
    assert body["owner_override_reason"].startswith("Owner insists")
    assert body["rung"] == "NONE" and body["promotion_check"]["record_only"] is True
    assert len(body["history_report_sha256"]) == 64
    assert body["history_report"]["walk_forward_oos"]["trades"] == 243
    assert body["shadow_summary"] == PASSING_CELL
    assert body["strategy_id"] == SID and body["strategy_version"] == 1
    assert body["strategy_source"] == "MECHANICAL" and body["version"] == "STRATEGY_PROMOTION_V1"
    with pytest.raises(PromotionRefused, match="STRATEGY_ALREADY_PROMOTED"):
        promote(engine, tmp_path)
    assert verify_events(engine.repo.export_events())["valid"]


def test_promotion_refuses_unknown_research_and_unreported_strategies(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    with pytest.raises(PromotionRefused, match="STRATEGY_NOT_REGISTERED"):
        promote(engine, tmp_path, strategy_id="NOPE_V1")
    with pytest.raises(PromotionRefused, match="STRATEGY_NOT_PAPER_ELIGIBLE"):
        promote(engine, tmp_path, strategy_id="PULLBACK_V1")  # Research picks: not mechanical.
    with pytest.raises(PromotionRefused, match="PROMOTION_HISTORY_ENTRY_MISSING"):
        strategy_paper.promote(engine.store, SID, history_report=report_file(
            tmp_path, PASSING_ENTRY, "OTHER_V1"), owner_ruling_ref="LAB_FIXTURE ruling",
            shadow_cell=PASSING_CELL, now=engine.now())
    with pytest.raises(PromotionRefused, match="PROMOTION_HISTORY_REPORT_UNREADABLE"):
        strategy_paper.promote(engine.store, SID, history_report=tmp_path / "missing.json",
                               owner_ruling_ref="LAB_FIXTURE ruling", now=engine.now())
    for ruling in ("", "x", "ruling " + "Bearer " + "a" * 40):  # A credential shape is refused.
        with pytest.raises(PromotionRefused, match="OWNER_RULING_REF_REQUIRED"):
            strategy_paper.promote(engine.store, SID, history_report=report_file(
                tmp_path, PASSING_ENTRY), owner_ruling_ref=ruling, shadow_cell=PASSING_CELL,
                now=engine.now())
    assert bodies(engine, strategy_paper.PROMOTION_EVENT) == []


def test_the_shadow_cell_comes_from_the_ledger_by_default(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    path = report_file(tmp_path, PASSING_ENTRY)
    with pytest.raises(PromotionRefused) as refused:  # No shadow outcomes recorded yet.
        strategy_paper.promote(engine.store, SID, history_report=path,
                               owner_ruling_ref="LAB_FIXTURE ruling", now=engine.now())
    assert refused.value.evidence["promotion_check"]["rung"] == "HISTORY_TEST_PASSED"
    start = engine.now() - timedelta(days=20)
    with engine.store.transaction() as conn:
        for i in range(30):
            at = (start + timedelta(hours=i)).isoformat()
            engine.store.event(conn, "STRATEGY_SHADOW_OUTCOME", {
                "strategy_id": SID, "symbol": "SOL/USD", "signal_at": at,
                "result": {"outcome": "TARGET", "gross_r": "1.5", "net_r": "1.4"}},
                key=f"fixture-outcome:{i}")
    result = strategy_paper.promote(engine.store, SID, history_report=path,
                                    owner_ruling_ref="LAB_FIXTURE ruling", now=engine.now())
    assert result["rung"] == "ELIGIBLE_FOR_OWNER_PAPER_REVIEW" and not result["override"]
    [body] = bodies(engine, strategy_paper.PROMOTION_EVENT)
    assert body["shadow_summary"]["trades"] == 30 and body["shadow_summary"]["label"] == "SHADOW"


def test_demotion_needs_a_promotion_and_reopens_the_gate(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    with pytest.raises(PromotionRefused, match="STRATEGY_NOT_PROMOTED"):
        strategy_paper.demote(engine.store, SID, reason="nothing to demote yet",
                              owner_ruling_ref="LAB_FIXTURE ruling", now=engine.now())
    first = promote(engine, tmp_path)
    demoted = strategy_paper.demote(engine.store, SID, reason="Weekly review: shadow and paper "
                                    "disagree", owner_ruling_ref="LAB_FIXTURE ruling",
                                    now=engine.now())
    assert demoted["promotion_event_seq"] == first["event_seq"]
    with engine.repo.connect() as conn:
        assert strategy_paper.active_promotion(conn, SID) is None
    again = promote(engine, tmp_path)  # A later promotion is a new record, never an edit.
    assert again["event_seq"] > demoted["event_seq"]
    with engine.repo.connect() as conn:
        assert strategy_paper.active_promotion(conn, SID)["event_seq"] == again["event_seq"]


def test_the_owner_commands_append_the_records(mt, tmp_path, registry, monkeypatch):  # noqa: F811
    engine = engine_of(mt)
    config = {"environment": {"MANAGED_DATABASE_URL": "unused-in-this-test"}}
    result = managed_ops.promote_strategy(config, SID, report_file(tmp_path, FAILING_ENTRY),
                                          "LAB_FIXTURE ruling", repository=engine.repo,
                                          owner_override_reason="Owner insists on a paper "
                                                                "plumbing test")
    assert result["action"] == "promote-strategy" and result["mode"] == "PAPER_ONLY"
    with pytest.raises(ValueError, match="PROMOTION_HISTORY_REPORT_REQUIRED"):
        managed_ops.promote_strategy(config, SID, None, "LAB_FIXTURE ruling",
                                     repository=engine.repo)
    out = io.StringIO()
    monkeypatch.setattr(cloud_runtime.cloud_config, "database_url", lambda *a, **k: "fixture")
    code = cloud_runtime.strategy_ladder("demote-strategy", SID, owner_ruling="LAB_FIXTURE ruling",
                                         reason="Railway shell demotion test",
                                         environ={}, out=out, repository=engine.repo)
    assert code == 0 and json.loads(out.getvalue())["action"] == "demote-strategy"
    out = io.StringIO()
    code = cloud_runtime.strategy_ladder(
        "promote-strategy", SID, owner_ruling="LAB_FIXTURE ruling", history_report="-",
        environ={}, out=out, repository=engine.repo,
        stdin=io.BytesIO(report_file(tmp_path, PASSING_ENTRY).read_bytes()))
    assert code == 2 and out.getvalue().strip() == (
        "CLOUD_STRATEGY_LADDER_REFUSED: PROMOTION_LADDER_NOT_MET")  # No ledger shadow cell.
    assert [b["operator"] for b in bodies(engine, strategy_paper.DEMOTION_EVENT)] == [
        "RAILWAY_OPS_SHELL"]
    assert [b["operator"] for b in bodies(engine, strategy_paper.PROMOTION_EVENT)] == [
        "LOCAL_OWNER_CLI"]


# --- The signal source --------------------------------------------------------------------------


def test_only_a_promoted_configured_strategy_records_signals(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    classify(engine, "SOL/USD")
    to_hour(mt)
    source = source_of(engine, mt, {"SOL/USD": "100"})
    summary = source.run_once()
    assert summary["strategies_not_promoted"] == 1 and "selected" not in summary
    assert bodies(engine, strategy_paper.SIGNAL_EVENT) == []
    promote(engine, tmp_path)
    assert source.run_once() == summary  # The hour was scanned: nothing is re-read.
    to_hour(mt)
    summary = source.run_once()
    assert (summary["selected"], summary["signals"], summary["coins"]) == (1, 1, 1)
    [signal] = bodies(engine, strategy_paper.SIGNAL_EVENT)
    assert signal["path_version"] == "STRATEGY_PAPER_PATH_V1" and signal["strategy_id"] == SID
    assert signal["levels"]["entry_trigger"] == "100.50" and signal["price_increment"] == "0.01"
    [packet] = selections(engine)
    assert packet["selection_policy"] == "STRATEGY_SIGNAL_SELECTION_V1"
    assert packet["receipt_id"] is None and packet["strategy_id"] == SID
    assert packet["report_schema_version"] == "AGENT_RESEARCH_REPORT_V3"
    [signal_row] = [r for r in events_of(engine, strategy_paper.SIGNAL_EVENT)]
    assert packet["signal_event_seq"] == signal_row["event_seq"]
    assert strategies.is_signal_packet(packet) and not research_v3(packet)
    # The same hour again from a new source: idempotent by key.
    again = source_of(engine, mt, {"SOL/USD": "100"}).run_once()
    assert again["already_recorded"] == 1 and len(selections(engine)) == 1
    # Unregistered and research strategies are never scanned.
    to_hour(mt)
    other = source_of(engine, mt, {"SOL/USD": "100"}, ids=("NOPE_V1", "PULLBACK_V1")).run_once()
    assert (other["strategies_unregistered"], other["strategies_not_eligible"]) == (1, 1)


def test_stale_failed_and_unpriced_signals_are_not_proposed(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    promote(engine, tmp_path)
    to_hour(mt, seconds=16 * 60)  # Past the 15-minute marketable window.
    stale = source_of(engine, mt, {"SOL/USD": "100"}).run_once()
    assert stale["stale"] == 1 and "selected" not in stale
    to_hour(mt)
    source = source_of(engine, mt, {"SOL/USD": "100", "ETH/USD": "100"},
                       symbols=("ETH/USD", "SOL/USD"))
    source.reader.fail.add("ETH/USD")
    failed = source.run_once()
    assert failed["bar_fail"] == 1 and failed["selected"] == 1
    source.reader.fail.clear()
    retried = source.run_once()  # A failed read leaves the hour open: retried, once.
    assert retried["selected"] == 1 and retried["already_recorded"] == 1
    to_hour(mt)
    unpriced = strategy_paper.StrategySignalSource(
        engine.store, Reader({"SOL/USD": "100"}), (SID,), lambda: mt[1].now, lambda s: None,
        universe=lambda conn, now: ["SOL/USD"]).run_once()
    assert unpriced["increment_unavailable"] == 1


# --- End to end: signal -> admission -> risk gate -> order -> protection ------------------------


def test_a_promoted_signal_enters_through_the_same_admission_risk_gate_and_protection(
        mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    venue = mt[1]
    [packet] = signalled(engine, mt, tmp_path)
    sid = admit(engine, mt, packet)
    state = engine._load(sid)[1]
    # Stamped: the strategy, the path, its promotion and signal; V5; the report-V3 versions.
    assert (state["strategy_id"], state["strategy_version"]) == (SID, 1)
    assert state["strategy_path"] == "STRATEGY_PAPER_PATH_V1"
    assert state["promotion_event_seq"] == packet["promotion_event_seq"]
    assert state["signal_event_seq"] == packet["signal_event_seq"]
    assert state["risk_policy_id"] == V5
    assert state["system_check"]["result"] == "PASSED"
    assert state["entry_type"] == "BREAKOUT"  # 100.50 is 0.5% above the live mid.
    assert state["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    # CRYPTO_TRADE_PLAN_V1: the stop widened to two hourly ranges, the target capped at 1.5R.
    plan = state["trade_plan"]
    assert plan["research_levels"]["stop"] == "98.49"
    assert D(state["stop"]) <= D("98.49") and plan["policy"]["policy_id"] == "CRYPTO_TRADE_PLAN_V1"
    assert D(state["target"]) <= D(plan["target_cap"])
    with engine.repo.connect() as conn:
        setup = conn.execute("SELECT * FROM lab.managed_setups WHERE setup_id=%s",
                             (sid,)).fetchone()
    assert setup["receipt_id"] is None and setup["strategy_version"] == (
        "CRYPTO_STRUCTURAL_RETEST_TEST_V1")
    assert engine.admit(packet, live_quote=at_price("99.99", "100.01", at=venue.now)) == sid
    # The marketable entry: the next fresh quote at or below the collar confirms; the order is a
    # limit at the collar, authorized once under V5 with the per-strategy cap.
    assert engine.observe_trigger(sid, observation(mt, trade_price="101", bid="100.99",
                                                   ask="101.01")) is None
    decision = engine.observe_trigger(sid, observation(mt, trade_price="100.2", bid="100.19",
                                                       ask="100.21"))
    assert decision["outcome"] == "APPROVED", decision
    assert decision["payload"]["limit_price"] == "100.50"
    assert decision["context"]["risk_policy_id"] == V5
    order = entry_order(mt, "SOL/USD")
    with engine.repo.connect() as conn:
        held = conn.execute("SELECT * FROM lab.managed_reservations WHERE setup_id=%s",
                            (sid,)).fetchone()
    assert held["planned_risk"] == held["budget"] <= D("50")  # 0.5% of 10,000.
    fill(mt, order, order["qty"], price="100.21")
    engine.manage(sid, quote(mt, "100.20"))
    engine.manage(sid, quote(mt, "100.20"))
    assert engine._load(sid)[1]["state"] == "OPEN"
    [stop] = stop_orders(mt, "SOL/USD")
    assert stop["stop_price"] == state["stop"]
    # Every broker change had its own approved, one-use decision.
    actions = [(d["action"], d["outcome"], d["claimed"]) for d in decisions(engine, sid)]
    assert ("ENTRY", "APPROVED", True) in actions and ("PROTECT", "APPROVED", True) in actions
    assert all(claimed for _, outcome, claimed in actions if outcome == "APPROVED")
    assert verify_events(engine.repo.export_events())["valid"]


def test_the_per_strategy_cap_holds_a_second_mechanical_entry(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    packets = signalled(engine, mt, tmp_path, symbols=("SOL/USD", "AVAX/USD"))
    sids = [admit(engine, mt, p) for p in packets]
    first = engine.observe_trigger(sids[0], observation(mt, trade_price="100.2", bid="100.19",
                                                        ask="100.21"))
    assert first["outcome"] == "APPROVED"
    # The first trade holds most of the 0.5% mechanical cap; the second waits (capacity).
    second = engine.observe_trigger(sids[1], observation(mt, trade_price="100.2", bid="100.19",
                                                         ask="100.21"))
    assert second["outcome"] == "REJECTED" and second["reason"] == STRATEGY_RISK_CAP
    assert second["context"]["binding_constraint"] == "STRATEGY_RISK_CAP"
    waiting = engine._load(sids[1])[1]
    assert waiting["state"] == "WATCHING" and waiting["capacity_deferred_reason"] == (
        STRATEGY_RISK_CAP)


def test_a_demotion_revokes_waiting_setups_and_refuses_new_packets(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    [packet] = signalled(engine, mt, tmp_path)
    sid = admit(engine, mt, packet)
    strategy_paper.demote(engine.store, SID, reason="Owner: stop the paper test now",
                          owner_ruling_ref="LAB_FIXTURE ruling", now=engine.now())
    with engine.repo.connect() as conn:
        failure = conn.execute("SELECT lab.managed_review_failure(%s::jsonb) AS r",
                               (json.dumps(engine._load(sid)[0]["record_json"]),)).fetchone()
    assert failure["r"] == "STRATEGY_DEMOTED"
    engine.manage(sid, quote(mt, "100.20"))
    assert engine._load(sid)[1]["state"] == "INVALIDATED"
    assert permanent_refusal(packet, "STRATEGY_DEMOTED")


def test_admission_refuses_unconfigured_forged_and_unpromoted_packets(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    [packet] = signalled(engine, mt, tmp_path)
    with pytest.raises(ValueError, match="^STRATEGY_NOT_CONFIGURED$"):
        admit(engine_of(mt, configured=()), mt, packet)
    assert permanent_refusal(packet, "STRATEGY_NOT_CONFIGURED")
    # A packet whose levels differ from its signal breaks the durable binding first; one written
    # directly with different levels is refused by the admission SQL itself.
    with pytest.raises(ValueError, match="^DURABLE_SELECTION_BINDING_REQUIRED$"):
        admit(engine, mt, {**packet, "levels": {**packet["levels"], "target": "110.00"}})
    forged = {k: v for k, v in packet.items() if k != "selection_event_seq"}
    forged["levels"] = {**forged["levels"], "target": "110.00"}
    forged["state"] = {**forged["state"], "levels": forged["levels"]}
    with engine.store.transaction() as conn:
        row = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": forged},
                                 key="fixture-forged-selection")
    with pytest.raises(ValueError, match="^STRATEGY_SIGNAL_BINDING_FAILURE$"):
        admit(engine, mt, {**forged, "selection_event_seq": row["event_seq"]})
    # A promotion record without the ladder's rung or an override never admits.
    with engine.store.transaction() as conn:
        bogus = engine.store.event(conn, strategy_paper.PROMOTION_EVENT, {
            "version": "STRATEGY_PROMOTION_V1", "strategy_id": SID, "strategy_source": "MECHANICAL",
            "history_report_sha256": "a" * 64, "shadow_summary": {}, "rung": "NONE",
            "owner_ruling_ref": "LAB_FIXTURE"}, key="fixture-bogus-promotion")
        unpromoted = {k: v for k, v in packet.items() if k != "selection_event_seq"}
        unpromoted["promotion_event_seq"] = bogus["event_seq"]
        row = engine.store.event(conn, "RESEARCH_SELECTED", {"packet": unpromoted},
                                 key="fixture-unpromoted-selection")
    with pytest.raises(ValueError, match="^STRATEGY_NOT_PROMOTED$"):
        admit(engine, mt, {**unpromoted, "selection_event_seq": row["event_seq"]})
    assert verify_events(engine.repo.export_events())["valid"]


def test_strategy_selections_are_never_read_as_research_runs(mt, tmp_path, registry):  # noqa: F811
    engine = engine_of(mt)
    signalled(engine, mt, tmp_path)
    with engine.repo.connect() as conn:
        assert system_check.newest_v3_run_slot(conn) is None
        assert system_check.newer_v3_selections(conn, engine.now() - timedelta(days=1)) == []


def test_a_strategy_entry_waits_while_entries_are_paced(mt, tmp_path, registry):  # noqa: F811
    from catalyst_lab import regime_gate
    from tests.test_regime_gate import paced, refeed, waits

    engine = engine_of(mt)
    [packet] = signalled(engine, mt, tmp_path)
    pacing = paced((engine, mt[1], mt[2]), moves=("-0.03", "-0.03", "-0.03"))  # Falling market.
    sid = admit(engine, mt, packet)
    assert engine._load(sid)[1][regime_gate.STATE_FIELD] == "CRYPTO_ENTRY_PACING_V1"
    touch = observation(mt, trade_price="100.2", bid="100.19", ask="100.21")
    assert engine.observe_trigger(sid, touch) is None and len(waits(engine, sid)) == 1
    assert decisions(engine, sid) == []  # No decision, reservation or order while held.
    mt[1].now += timedelta(minutes=2)
    refeed(mt, pacing)
    engine.reconciled_at = None
    assert engine.reconcile()["clean"]
    decision = engine.observe_trigger(sid, observation(mt, trade_price="100.2", bid="100.19",
                                                       ask="100.21"))
    assert decision["outcome"] == "APPROVED"
