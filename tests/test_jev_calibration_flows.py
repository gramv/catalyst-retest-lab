"""JEV_CALIBRATION_V1 on real maintenance reviews (package jev-b3): a V4 trade answered through
a mock Jev (its stored action judgment gives P(FLAG_EARLY_EXIT)) beside a V5 trade (its Noul
answers), joined to canned public minute bars once the 24-hour window is complete.

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars and a
mock Jev transport. No broker, provider, network or owner-ledger contact; no real TypeSafe call.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import jev_calibration as jc
from catalyst_lab import trade_plan
from tests.day_review_fixtures import events, managed_arm, open_trade, state
from tests.learning_fixtures import FakeBars, minute_rows
from tests.maintenance_fixtures import mt as mt  # noqa: F401
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_jev_b1_flows import maintain, to_bar, v5_kit

pytestmark = pytest.mark.usefixtures("managed_arm")
_ = managed_arm


def test_real_v4_and_v5_reviews_are_calibrated_with_their_stated_probabilities(
        mt, monkeypatch):  # noqa: F811
    engine, venue, _ = mt
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V4)
    older = open_trade(mt, "ETH/USD")
    monkeypatch.setattr(cm, "ADMITTED_MAINTENANCE", cm.CRYPTO_MAINTENANCE_V5)
    newer = open_trade(mt, "SOL/USD")
    k = v5_kit(mt)
    k.jev.p = {"invalidation_met": 0.35, "news_contradicts": 0.05}
    k.jev.exact = {"MAINTENANCE": {"action": {"HOLD": 0.7, "FLAG_EARLY_EXIT": 0.2}}}
    k.prices.set("ETH/USD", "101")
    to_bar(mt, 900)
    maintain(mt, k)
    [v4] = events(engine, "MAINTENANCE_DECISION", older)
    [v5] = events(engine, "MAINTENANCE_DECISION", newer)
    assert v4["policy_id"] == "CRYPTO_MAINTENANCE_V4" and v4["receipt_ids"]
    assert v5["answers"] == {"invalidation_met": {"p": "0.35", "verdict": "UNCERTAIN"}}
    with engine.repo.connect() as conn:
        setups = {row["setup_id"]: row for row in conn.execute(
            "SELECT setup_id, record_json FROM lab.managed_setups").fetchall()}
    plan = {sid: trade_plan.initial_levels(
        {k_: D(v) for k_, v in setups[sid]["record_json"]["levels"].items()}, state(mt, sid))
        for sid in (older, newer)}
    start = venue.now - timedelta(minutes=30)
    # ETH rises through +1R (and the plan target) at once; SOL falls through its plan stop.
    reader = FakeBars(minutes={
        "ETH/USD": minute_rows(start, [str(plan[older]["target"] + 1)] * 1600),
        "SOL/USD": minute_rows(start, [str(plan[newer]["stop"] - 1)] * 1600)})
    early = jc.record_calibration(engine.store, reader, now=venue.now + timedelta(hours=23))
    assert early["maintenance_pending"] == 2 and early["maintenance_recorded"] == 0
    summary = jc.record_calibration(engine.store, reader, now=venue.now + timedelta(hours=25))
    assert summary["maintenance_recorded"] == 2 and summary["invalid"] == 0
    records = {r["setup_id"]: r for r in jc.calibration_records(engine.repo,
                                                                source=jc.MAINTENANCE)}
    v4_record, v5_record = records[str(older)], records[str(newer)]
    assert v4_record["probability_source"] == "RECEIPT_JUDGMENT"
    [flag] = v4_record["forecasts"]
    assert (flag["question"], D(flag["p"]), flag["version"]) == (
        "action:FLAG_EARLY_EXIT", D("0.2"), "CRYPTO_MAINTENANCE_V4")
    assert flag["event_value"] is False  # +1R first.
    assert v4_record["outcome"]["first_touch_plan"]["reason"] == "TARGET"
    assert v4_record["levels"]["plan_stop"] == str(plan[older]["stop"])
    [invalidation] = v5_record["forecasts"]
    assert (invalidation["question"], invalidation["p"], invalidation["verdict"]) == (
        "invalidation_met", "0.35", "UNCERTAIN")
    assert invalidation["event_value"] is True  # The plan stop first.
    assert v5_record["asked_bar_end"] == v5["asked_bar_end"]
    assert v5_record["state_hash"] == v5["state_hash"]
    assert v5_record["receipt_ids"] == v5["receipt_ids"]
    assert jc.record_calibration(engine.store, reader, now=venue.now + timedelta(hours=25))[
        "maintenance_recorded"] == 0
