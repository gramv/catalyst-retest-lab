"""UNCHANGED_PLAN_REPLAY_V1 wired to package maintenance's own events: applied
MAINTENANCE_DECISION stop/target raises and agreed EXIT_FLAG_RESOLVED early exits.

Fixture evidence only: disposable per-test databases, the fake paper venue, a scripted bar
source and a mock Jev transport (tests.maintenance_fixtures, package maintenance's own). No
broker, provider, network or owner-ledger contact.
"""

from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

from fastapi.testclient import TestClient

from catalyst_lab import exit_flags
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.unchanged_plan import (
    EARLY_EXIT,
    STOP_AND_TARGET_RAISE,
    STOP_RAISE,
    TARGET_RAISE,
    maintenance_exit_changes,
    maintenance_level_changes,
    maintenance_replay_aggregates,
    maintenance_replay_page,
    setup_level_changes,
    setups_with_level_changes,
    unchanged_plan_comparisons,
)
from tests.maintenance_fixtures import bodies, maintainer, open_trade, quote
from tests.maintenance_fixtures import mt as mt  # noqa: F401
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_service import TOKEN, FixtureCycle


def reviewed(mt, kit, *, symbol="SOL/USD", bid="106", seconds=1):
    """One maintenance review of ``symbol`` at ``bid`` (+1R at 105.20 triggers it).

    ``seconds`` must exceed ``CRYPTO_MAINTENANCE.min_review_interval_seconds`` (60) between two
    reviews of the *same* trade, or the second one is not yet due.
    """
    mt[1].now += timedelta(seconds=seconds)
    kit.prices.set(symbol, bid)
    from tests.maintenance_fixtures import run_pass

    return run_pass(mt, kit)


class FakeBars:
    """Deterministic canned minute bars, keyed by symbol; never touches stop or target unless
    the test explicitly asks for a row that does."""

    def __init__(self, rows_by_symbol=None):
        self.rows_by_symbol = rows_by_symbol or {}

    def flat(self, symbol, start, hours, price="105"):
        self.rows_by_symbol.setdefault(symbol, []).extend(
            {"t": (start + timedelta(minutes=15 * i)).isoformat(), "o": price, "h": price,
             "l": price, "c": price, "v": "1"}
            for i in range(int(hours * 4) + 1)
        )
        return self

    def minute_bars(self, symbol, start, end):
        return [r for r in self.rows_by_symbol.get(symbol, [])
                if start <= datetime.fromisoformat(r["t"]) < end]


# --- maintenance_level_changes: reads MAINTENANCE_DECISION APPLIED, never the current state ----


def test_reads_an_applied_stop_raise(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert decision["outcome"] == "APPLIED"
    [change] = maintenance_level_changes(engine.repo, sid)
    assert change.change_kind == STOP_RAISE
    assert change.source_kind == "MAINTENANCE_DECISION"
    assert change.old_stop == D(decision["levels_before"]["stop"]) == D("95")
    assert change.new_stop == D(decision["levels_after"]["stop"])
    assert change.new_stop > change.old_stop
    assert change.old_target == change.new_target == D("111")  # Only the stop moved.
    assert change.at == datetime.fromisoformat(decision["decided_at"])


def test_reads_an_applied_target_raise(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_TARGET", target="first")
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    [change] = maintenance_level_changes(engine.repo, sid)
    assert change.change_kind == TARGET_RAISE
    assert change.old_target == D(decision["levels_before"]["target"]) == D("111")
    assert change.new_target == D(decision["levels_after"]["target"])
    assert change.new_target > change.old_target
    assert change.old_stop == change.new_stop == D("95")


def test_reads_both_levels_raised_together(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP_AND_TARGET", stop="last", target="last")
    reviewed(mt, kit)
    [change] = maintenance_level_changes(engine.repo, sid)
    assert change.change_kind == STOP_AND_TARGET_RAISE
    assert change.new_stop > change.old_stop and change.new_target > change.old_target


def test_a_held_decision_produces_no_level_change(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)  # Default script: HOLD.
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert decision["outcome"] == "HELD"
    assert maintenance_level_changes(engine.repo, sid) == []


def test_a_refused_decision_produces_no_level_change(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    # An Insufficient-evidence action is refused (UNCERTAIN_JUDGMENT) under V1 and V2 alike.
    # (Under V1, RAISE_STOP with stop_option KEEP was refused as contradictory; under
    # CRYPTO_MAINTENANCE_V2, admitted from package answer-rules, it is held.)
    kit.jev.script = {"trade_reason": "INTACT", "action": "Insufficient evidence",
                      "stop_option": "KEEP", "target_option": "KEEP"}
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    assert (decision["outcome"], decision["code"]) == ("REFUSED", "UNCERTAIN_JUDGMENT")
    assert maintenance_level_changes(engine.repo, sid) == []


# --- maintenance_exit_changes: reads an agreed EXIT_FLAG_RESOLVED, from the flag's own evidence -


def test_reads_an_agreed_early_exit(mt):  # noqa: F811
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("FLAG_EARLY_EXIT", reason="BROKEN")
    reviewed(mt, kit)
    [flag] = bodies(engine, "EXIT_FLAG_RAISED", sid)
    with engine.store.transaction() as conn:
        exit_flags.resolve_exit_flag(
            engine.store, conn, flag_id=flag["flag_id"], outcome="EXIT_AGREED",
            answered_by={"agent_id": "claude"}, answer={"note": "fixture agree"},
            resolved_at=venue.now,
        )
    [change] = maintenance_exit_changes(engine.repo, sid)
    assert change.change_kind == EARLY_EXIT
    assert change.actually_exited is True
    assert change.new_stop is None and change.new_target is None
    assert change.old_stop == D(flag["evidence"]["levels"]["stop"]) == D("95")
    assert change.old_target == D(flag["evidence"]["levels"]["target"]) == D("111")
    assert change.at == venue.now
    assert change.source_kind == "EXIT_FLAG_RESOLVED"


def test_a_declined_flag_produces_no_exit_change(mt):  # noqa: F811
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("FLAG_EARLY_EXIT", reason="BROKEN")
    reviewed(mt, kit)
    [flag] = bodies(engine, "EXIT_FLAG_RAISED", sid)
    with engine.store.transaction() as conn:
        exit_flags.resolve_exit_flag(
            engine.store, conn, flag_id=flag["flag_id"], outcome="EXIT_NOT_AGREED",
            answered_by={"agent_id": "claude"}, answer={"note": "fixture decline"},
            resolved_at=venue.now,
        )
    assert maintenance_exit_changes(engine.repo, sid) == []


def test_a_pending_flag_produces_no_exit_change(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("FLAG_EARLY_EXIT", reason="BROKEN")
    reviewed(mt, kit)
    assert bodies(engine, "EXIT_FLAG_RAISED", sid)  # The flag was raised...
    assert maintenance_exit_changes(engine.repo, sid) == []  # ...but never resolved.


# --- setup_level_changes: the union, in time order ----------------------------------------------


def test_setup_level_changes_unions_both_sources_in_time_order(mt):  # noqa: F811
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit)
    # A second review needs no stop replacement in progress: complete the PATCH replace first
    # (the reference pattern in test_trade_maintenance.py's own stop-raise test).
    engine.manage(sid, quote(mt, "106"))
    engine.manage(sid, quote(mt, "106"))
    assert engine._load(sid)[1].get("stop_replace") is None
    kit.jev.answer("FLAG_EARLY_EXIT", reason="BROKEN")
    # A second review of the same trade needs > min_review_interval_seconds (60) since the
    # last one, and a fresh due reason: bid near the raised stop is an unserved NEAR_STOP.
    reviewed(mt, kit, bid="103.5", seconds=65)
    [flag] = bodies(engine, "EXIT_FLAG_RAISED", sid)
    with engine.store.transaction() as conn:
        exit_flags.resolve_exit_flag(
            engine.store, conn, flag_id=flag["flag_id"], outcome="EXIT_AGREED",
            answered_by={"agent_id": "claude"}, answer={}, resolved_at=venue.now,
        )
    changes = setup_level_changes(engine.repo, sid)
    assert [c.change_kind for c in changes] == [STOP_RAISE, EARLY_EXIT]
    assert changes[0].at < changes[1].at


# --- unchanged_plan_comparisons: replays a MAINTENANCE_DECISION change end to end ---------------


def test_unchanged_plan_comparisons_replays_a_maintenance_decision(mt):  # noqa: F811
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    at = datetime.fromisoformat(decision["decided_at"])
    bars = FakeBars().flat("SOL/USD", at, hours=25, price="105")
    comparisons = unchanged_plan_comparisons(engine.repo, bars, sid, now=at + timedelta(hours=25))
    assert len(comparisons) == 1
    outcome = comparisons[0]
    assert outcome.change_kind == STOP_RAISE
    assert outcome.original_stop == D("95")
    assert outcome.data_complete is True  # Flat bars: rides to the 24-hour hold exit.


# --- Cross-setup discovery, listing and aggregates -----------------------------------------------


def test_setups_with_level_changes_finds_a_maintained_setup(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit)
    found = setups_with_level_changes(engine.repo)
    assert [row["setup_id"] for row in found] == [sid]
    assert found[0]["symbol"] == "SOL/USD" and found[0]["arm"] == "JEV_MANAGED"


def test_setups_with_level_changes_setup_id_filter(mt):  # noqa: F811
    engine, _, _ = mt
    sid = open_trade(mt)
    other = open_trade(mt, symbol="BTC/USD")
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit, symbol="SOL/USD")
    reviewed(mt, kit, symbol="BTC/USD")
    found = setups_with_level_changes(engine.repo, setup_id=sid)
    assert [row["setup_id"] for row in found] == [sid]
    assert other not in [row["setup_id"] for row in found]


def test_maintenance_replay_page_and_aggregates(mt):  # noqa: F811
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    at = datetime.fromisoformat(decision["decided_at"])
    bars = FakeBars().flat("SOL/USD", at, hours=25, price="105")
    now = at + timedelta(hours=25)

    page = maintenance_replay_page(engine.repo, bars, now=now)
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert row["setup_id"] == str(sid) and row["arm"] == "JEV_MANAGED"
    assert row["replay"]["change_kind"] == STOP_RAISE

    scoped = maintenance_replay_page(engine.repo, bars, now=now, setup_id=sid)
    assert len(scoped["items"]) == 1
    unrelated = maintenance_replay_page(engine.repo, bars, now=now, setup_id=uuid4())
    assert unrelated["items"] == []  # A different setup_id finds nothing.

    aggregates = maintenance_replay_aggregates(engine.repo, bars, now=now)
    assert aggregates["group_by"] == ["change_kind", "arm"]
    [group] = aggregates["items"]
    assert (group["change_kind"], group["arm"], group["count"]) == (STOP_RAISE, "JEV_MANAGED", 1)
    assert "mean_r_difference" in group and "helped_rate" in group
    assert aggregates["setups_considered"] == 1


def test_maintenance_replay_aggregates_rejects_an_unknown_dimension(mt):  # noqa: F811
    engine, _, _ = mt
    try:
        maintenance_replay_aggregates(engine.repo, FakeBars(), now=datetime.now().astimezone(),
                                      group_by=("not_a_real_dimension",))
    except ValueError as exc:
        assert str(exc) == "INVALID_MAINTENANCE_AGGREGATE_DIMENSIONS"
    else:
        raise AssertionError("should have refused an unknown dimension")


# --- API routes ------------------------------------------------------------------------------


def maintenance_app(mt, bar_reader):
    engine, venue, _ = mt
    cycle = FixtureCycle(engine.repo, engine.store)
    # The fixture venue owns its own advanced-by-hand clock; the route must read "now" through
    # the same clock; a real wall-clock read here would not agree with fixture event times.
    return TestClient(create_managed_app(
        cycle, engine.store, api_token=TOKEN, runtime_status=lambda: {}, bar_reader=bar_reader,
        clock=lambda: venue.now,
    ))


def test_maintenance_routes_answer_503_without_a_configured_bar_reader(mt):  # noqa: F811
    client = maintenance_app(mt, None)
    auth = {"Authorization": "Bearer " + TOKEN}
    assert client.get("/api/v1/lab/results/maintenance", headers=auth).status_code == 503
    assert client.get(
        "/api/v1/lab/results/maintenance/aggregates", headers=auth
    ).status_code == 503


def test_maintenance_routes_require_auth_and_serve_a_replay(mt):  # noqa: F811
    engine, venue, _ = mt
    sid = open_trade(mt)
    kit = maintainer(mt)
    kit.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, kit)
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
    at = datetime.fromisoformat(decision["decided_at"])
    bars = FakeBars().flat("SOL/USD", at, hours=25, price="105")
    venue.now += timedelta(hours=25)  # Past the change's own 24-hour hold deadline.
    client = maintenance_app(mt, bars)
    assert client.get("/api/v1/lab/results/maintenance").status_code == 401
    auth = {"Authorization": "Bearer " + TOKEN}
    page = client.get("/api/v1/lab/results/maintenance", headers=auth)
    assert page.status_code == 200
    assert page.json()["items"][0]["setup_id"] == str(sid)
    scoped = client.get(f"/api/v1/lab/results/maintenance?setup_id={sid}", headers=auth)
    assert len(scoped.json()["items"]) == 1
    aggregates = client.get("/api/v1/lab/results/maintenance/aggregates", headers=auth)
    assert aggregates.status_code == 200
    assert aggregates.json()["items"][0]["change_kind"] == STOP_RAISE
    assert TOKEN not in page.text and TOKEN not in aggregates.text
