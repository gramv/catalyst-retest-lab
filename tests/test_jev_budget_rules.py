"""Package jev-budget as pure rules: ``JEV_SPEND_GUARD_V1``'s configuration, prices, tiers and
calendar, ``CRYPTO_MAINTENANCE_V3``'s record and cadence, and the watchdog's alerts.

No database, broker, provider or network: exact Decimal arithmetic and fixed instants only.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from catalyst_lab import crypto_maintenance as cm
from catalyst_lab import jev_budget as jb
from catalyst_lab import maintenance_dossier as md
from catalyst_lab.account_risk import FIXED_EXIT_ARM, JEV_MANAGED_ARM
from catalyst_lab.cloud_runtime import STATUS_KEYS
from catalyst_lab.managed_ops import jev_budget_alarms, status_alarms
from catalyst_lab.managed_service import STATUS_FIELDS
from catalyst_lab.market import NY

V3_CRYPTO = {"market": "CRYPTO", "report_schema_version": "AGENT_RESEARCH_REPORT_V3"}
V3_RECORD = {
    **cm.CRYPTO_MAINTENANCE_V2.record(), "policy_id": "CRYPTO_MAINTENANCE_V3",
    "spend_guard": "JEV_SPEND_GUARD_V1", "throttled_review_bar_seconds": 300,
    "tight_review_bar_seconds": 900,
}
B = D("50")


# --- CRYPTO_MAINTENANCE_V3 -----------------------------------------------------------------------


def test_v3_is_v2_plus_the_spend_guard_exactly_and_is_what_admission_records():
    assert cm.CRYPTO_MAINTENANCE_V3.record() == V3_RECORD
    assert V3_RECORD["review_bar_seconds"] == 60  # NORMAL: V2's cadence.
    for change in ({"throttled_review_bar_seconds": 120}, {"tight_review_bar_seconds": 600},
                   {"spend_guard": "JEV_SPEND_GUARD_V2"}, {"review_bar_seconds": 300},
                   {"answer_rule": "MAINTENANCE_ANSWER_RULE_V1"}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
            cm.MaintenancePolicyV3(**{**V3_RECORD, **change})
    # A V3 policy_id on anything but V3's exact record is refused (never read as V1 or V2).
    for broken in ({**cm.CRYPTO_MAINTENANCE_V2.record(), "policy_id": "CRYPTO_MAINTENANCE_V3"},
                   {**V3_RECORD, "extra": 1}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_MAINTENANCE_POLICY_REQUIRED"):
            cm.policy_from_record(broken)
    v3 = cm.recorded_policy({"maintenance_policy": V3_RECORD})
    assert type(v3) is cm.MaintenancePolicyV3 and v3 == cm.CRYPTO_MAINTENANCE_V3
    assert cm.active({"maintenance_policy": V3_RECORD}) and cm.guarded(v3)
    assert not cm.guarded(cm.CRYPTO_MAINTENANCE_V2) and not cm.guarded(cm.CRYPTO_MAINTENANCE)
    # Admission recorded V3 in the maintained arm until package trade-plan, which records V4
    # (V3 plus the stop-raise guards, still guarded), and from package jev-b1 V5 (still
    # guarded: EXHAUSTED withholds its reviews); the control arm is never maintained.
    assert cm.ADMITTED_MAINTENANCE is cm.CRYPTO_MAINTENANCE_V5 and cm.guarded(
        cm.CRYPTO_MAINTENANCE_V4) and cm.guarded(cm.CRYPTO_MAINTENANCE_V5)
    assert cm.admission_fields(V3_CRYPTO, JEV_MANAGED_ARM) == {
        "maintenance_policy": cm.CRYPTO_MAINTENANCE_V5.record(),
        "partial_entry_policy": cm.CRYPTO_PARTIAL_ENTRY.record()}
    assert cm.admission_fields(V3_CRYPTO, FIXED_EXIT_ARM) == {
        "partial_entry_policy": cm.CRYPTO_PARTIAL_ENTRY.record()}
    # What Jev reads and how its answer is read are V2's: context and questions V5, rule V2.
    assert md.answer_reader(V3_RECORD) is md.read_answer_v2
    assert (v3.context_version, v3.question_version) == (
        "JEV_MANAGED_POSITION_CONTEXT_V5", "JEV_MANAGED_POSITION_QUESTIONS_V5")
    assert cm.MAINTENANCE_VERSIONS == (
        "CRYPTO_MAINTENANCE_V1", "CRYPTO_MAINTENANCE_V2", "CRYPTO_MAINTENANCE_V3",
        "CRYPTO_MAINTENANCE_V4", "CRYPTO_MAINTENANCE_V5")  # V4: trade-plan; V5: jev-b1.


def test_the_tier_sets_v3s_routine_cadence_and_exhausted_allows_no_review():
    v3 = cm.CRYPTO_MAINTENANCE_V3
    assert [v3.review_bar_seconds_for(t) for t in (jb.NORMAL, jb.THROTTLED, jb.TIGHT)] == [
        60, 300, 900]
    for none in (jb.EXHAUSTED, jb.UNAVAILABLE, None, "normal"):
        assert v3.review_bar_seconds_for(none) is None
    assert jb.REVIEW_BAR_SECONDS == {"NORMAL": 60, "THROTTLED": 300, "TIGHT": 900,
                                     "EXHAUSTED": None}
    assert cm.BAR_REASONS == {900: "BAR_15M", 300: "BAR_5M", 60: "BAR_1M"}


def test_five_minute_bars_are_due_at_their_boundary_and_events_at_once():
    opened = datetime(2026, 9, 20, 14, 0, 30, tzinfo=UTC)

    def due(now, served, triggers=(), last=None):
        return cm.due_reasons(now=now, opened_at=opened, served_bar_end=served,
                              unserved_triggers=list(triggers), news_revision=1,
                              served_news_revision=1, shocks=[], last_requested_at=last,
                              bar_seconds=300)

    served = datetime(2026, 9, 20, 14, 5, tzinfo=UTC)
    for minute in (6, 7, 8, 9):  # Completed minutes that are not 5-minute bars: nothing due.
        at = datetime(2026, 9, 20, 14, minute, 1, tzinfo=UTC)
        assert due(at, served, last=served) == ([], False)
    at = datetime(2026, 9, 20, 14, 10, 0, tzinfo=UTC)
    assert due(at, served, last=served) == (["BAR_5M"], False)
    # An event is due between bars, after V1's one-minute floor (near the target at once).
    at = datetime(2026, 9, 20, 14, 7, 1, tzinfo=UTC)
    assert due(at, served, [("R_MILESTONE", "2")], last=served) == (["R_MILESTONE"], False)
    inside = served + timedelta(seconds=20)
    assert due(inside, served, [("R_MILESTONE", "2")], last=served) == ([], False)
    assert due(inside, served, [("NEAR_TARGET", "111")], last=served) == (["NEAR_TARGET"], True)


# --- Configuration -------------------------------------------------------------------------------


def test_settings_are_exact_decimals_with_defaults_and_refusals_name_the_variable():
    assert jb.ENV_NAMES == ("JEV_MONTHLY_BUDGET_USD", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD",
                            "JEV_BYTES_PER_TOKEN")
    config = jb.SpendConfig.from_env({"JEV_MONTHLY_BUDGET_USD": "50"}, required=True)
    assert config == jb.SpendConfig(D("50"), D("0.042"), D("3"))
    assert config.record() == {"monthly_budget_usd": "50",
                               "price_per_million_input_tokens_usd": "0.042",
                               "bytes_per_token": "3"}
    full = {"JEV_MONTHLY_BUDGET_USD": "37.50", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD": "0.05",
            "JEV_BYTES_PER_TOKEN": "3.5"}
    assert jb.SpendConfig.from_env(full, required=True) == jb.SpendConfig(
        D("37.50"), D("0.05"), D("3.5"))
    assert jb.SpendConfig.from_env({}, required=False) is None
    with pytest.raises(jb.SpendConfigError) as missing:
        jb.SpendConfig.from_env({}, required=True)
    assert (missing.value.code, missing.value.name) == (
        "JEV_SPEND_SETTING_MISSING", "JEV_MONTHLY_BUDGET_USD")
    bad = {"JEV_MONTHLY_BUDGET_USD": ["0", "-1", "1e3", "50.001", " 50", "50 ", "abc", "NaN",
                                      "100000.01", "0.001", ""],
           "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD": ["0", "1000.5", "0.0000001", "Infinity",
                                                      "0.04x"],
           "JEV_BYTES_PER_TOKEN": ["0.5", "10.5", "3.0001", "-3", "three"]}
    for name, values in bad.items():
        for value in values:
            env = {**full, name: value}
            if value == "" and name == "JEV_MONTHLY_BUDGET_USD":
                with pytest.raises(jb.SpendConfigError, match="JEV_SPEND_SETTING_MISSING"):
                    jb.SpendConfig.from_env(env, required=True)
                continue
            with pytest.raises(jb.SpendConfigError) as refused:
                jb.SpendConfig.from_env(env, required=True)
            assert (refused.value.code, refused.value.name) == ("JEV_SPEND_SETTING_INVALID", name)
            assert value.strip() not in str(refused.value) or not value.strip()
    # Without a budget (reviews disabled on a Mac), a stray price is still refused, not ignored.
    with pytest.raises(jb.SpendConfigError) as stray:
        jb.SpendConfig.from_env({"JEV_BYTES_PER_TOKEN": "0"}, required=False)
    assert stray.value.name == "JEV_BYTES_PER_TOKEN"
    for field in ({"monthly_budget_usd": 50}, {"bytes_per_token": D("0")},
                  {"price_per_million_input_tokens_usd": D("NaN")}):
        with pytest.raises(jb.SpendConfigError):
            jb.SpendConfig(**{"monthly_budget_usd": D("50"), **field})


def test_cost_is_exact_from_the_request_bytes():
    config = jb.SpendConfig(B)
    # The real V2 check's mean maintenance request (13,565 bytes): 4,521.67 tokens.
    assert config.tokens(13565).quantize(D("0.01")) == D("4521.67")
    assert config.cost(13565) == D("0.00018991")
    assert config.cost(13565 * 1440 * 30) == D("8.204112")  # Per trade, every minute, 30 days.
    assert config.cost(0) == 0
    # A dollar per megabyte (the meter tests' configuration): exact.
    megabyte = jb.SpendConfig(D("1"), D("3"), D("3"))
    assert megabyte.cost(980_000) == D("0.98") and megabyte.cost(1) == D("0.000001")
    assert jb.usd(D("0.00018991")) == "0.0002" and jb.usd(D("12.34565")) == "12.3456"


def test_request_kinds_follow_the_question_set():
    kinds = {
        "CHART_PICK_QUESTIONS_V1": "SELECTION", "NEWS_PICK_QUESTIONS_V2": "SELECTION",
        "BOTH_PICK_QUESTIONS_V2": "SELECTION", "SKEPTIC_QUESTIONS_V2": "SELECTION",
        "MUSE_JEV_COMPARATIVE_QUALITY_V3": "QUALITY",
        "JEV_MANAGED_POSITION_QUESTIONS_V5": "MAINTENANCE",
        "JEV_MANAGED_POSITION_QUESTIONS_V3": "MAINTENANCE",
        "JEV_DAY_REVIEW_QUESTIONS_V2": "DAY_REVIEW", "JEV_EARLY_EXIT_QUESTIONS_V1": "EARLY_EXIT",
        "PROVIDER_HEALTH_V1": "HEALTH_PROBE", "JEV_EVIDENCE_DIAGNOSTIC_V1": "OTHER", None: "OTHER",
    }
    for version, kind in kinds.items():
        assert jb.kind_of(version) == kind, version
    assert set(kinds.values()) == set(jb.KINDS)


# --- The tier ------------------------------------------------------------------------------------


def tier(mtd, p1, previous=None):
    return jb.tier_for(month_to_date=D(mtd), projection_per_minute=D(p1), budget=B,
                       previous_tier=previous)[0]


def test_the_tiers_at_their_exact_boundaries():
    assert jb.policy_record() == {
        "policy_id": "JEV_SPEND_GUARD_V1", "meter_version": "JEV_SPEND_METER_V1",
        "timezone": "America/New_York", "reserve_fraction": "0.02",
        "throttle_above_fraction": "0.98", "normal_at_or_below_fraction": "0.93",
        "tight_from_fraction": "0.90", "window_seconds": 86400,
        "review_bar_seconds": {"NORMAL": 60, "THROTTLED": 300, "TIGHT": 900, "EXHAUSTED": None},
        "provider_call": "HTTP_STATUS_PRESENT_OR_TRANSPORT_FAILURE",
        "request_bytes": "OCTET_LENGTH_OF_REQUEST_JSON",
        "attribution": "RECEIPT_STARTED_AT_NEW_YORK_MONTH"}
    assert {k: str(v) for k, v in jb.thresholds(B).items()} == {
        "throttle_above_usd": "49.00", "normal_at_or_below_usd": "46.50",
        "tight_from_usd": "45.00", "exhausted_from_usd": "49.00"}
    # NORMAL while the per-minute projection is at or below 98% of the budget.
    assert tier("10", "49.00") == jb.NORMAL
    assert tier("10", "49.000001") == jb.THROTTLED
    # TIGHT only while throttling, from 90% spent; per-minute continues when it fits.
    assert tier("44.99", "49.5") == jb.THROTTLED
    assert tier("45.00", "49.5") == jb.TIGHT
    assert tier("45.00", "49.00") == jb.NORMAL
    # EXHAUSTED from 98% spent, whatever the projection (2% reserve).
    assert tier("48.999999", "48.999999", jb.TIGHT) == jb.TIGHT
    assert tier("49.00", "49.00") == jb.EXHAUSTED
    assert tier("60", "0", jb.NORMAL) == jb.EXHAUSTED
    assert jb.tier_for(month_to_date=D("49"), projection_per_minute=D("0"), budget=B,
                       previous_tier=None)[1] == "MONTH_TO_DATE_AT_RESERVE"


def test_throttling_ends_only_at_or_below_93_percent():
    for previous in (jb.THROTTLED, jb.TIGHT, jb.EXHAUSTED):
        assert tier("10", "49.00", previous) == jb.THROTTLED  # Never NORMAL above 93%.
        assert tier("10", "46.500001", previous) == jb.THROTTLED
        assert tier("10", "46.50", previous) == jb.NORMAL
        assert tier("46", "46.50", previous) == jb.NORMAL
        assert tier("45", "46.51", previous) == jb.TIGHT
    for previous in (None, jb.NORMAL):
        assert tier("10", "49.00", previous) == jb.NORMAL


# --- The New York calendar -----------------------------------------------------------------------


def test_months_and_days_are_new_york_calendar_ones():
    late = datetime(2026, 10, 1, 3, 59, tzinfo=UTC)  # 23:59 on September 30 in New York.
    assert jb.month_label(late) == "2026-09"
    assert jb.month_start(late) == datetime(2026, 9, 1, tzinfo=NY)
    assert jb.next_month_start(late) == datetime(2026, 10, 1, tzinfo=NY)
    assert jb.day_start(late) == datetime(2026, 9, 30, tzinfo=NY)
    early = datetime(2026, 10, 1, 4, 1, tzinfo=UTC)  # 00:01 on October 1.
    assert jb.month_label(early) == "2026-10" and jb.day_start(early).day == 1
    # December rolls into January; November starts on EDT and ends on EST.
    december = datetime(2026, 12, 31, 12, tzinfo=UTC)
    assert jb.next_month_start(december) == datetime(2027, 1, 1, tzinfo=NY)
    november = datetime(2026, 11, 15, tzinfo=UTC)
    length = jb.next_month_start(november).astimezone(UTC) - jb.month_start(november).astimezone(
        UTC)
    assert length.total_seconds() == 30 * 86400 + 3600  # The hour DST gives back on Nov 1.


# --- Status and watchdog --------------------------------------------------------------------------


def test_the_watchdog_alerts_a_tier_change_for_thirty_minutes_only():
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)

    def section(tier, since):
        return {"available": True, "tier": tier,
                "tier_since": since.isoformat() if since else None}

    assert jev_budget_alarms(None, now) == []  # No guard (an older release, reviews disabled).
    assert jev_budget_alarms(section("NORMAL", now), now) == []
    for tier_name, code in (("THROTTLED", "JEV_BUDGET_THROTTLED"), ("TIGHT", "JEV_BUDGET_TIGHT"),
                            ("EXHAUSTED", "JEV_BUDGET_EXHAUSTED")):
        assert jev_budget_alarms(section(tier_name, now), now) == [code]
        assert jev_budget_alarms(section(tier_name, now - timedelta(seconds=1800)), now) == [code]
        assert jev_budget_alarms(section(tier_name, now - timedelta(seconds=1801)), now) == []
        # An unreadable time fails closed: the change is alerted.
        assert jev_budget_alarms({"available": True, "tier": tier_name,
                                  "tier_since": "never"}, now) == [code]
        assert jev_budget_alarms({"available": True, "tier": tier_name,
                                  "tier_since": "2026-09-20T12:00:00"}, now) == [code]
    for broken in ({"available": False, "tier": "UNAVAILABLE"}, "nope", [],
                   {"available": True, "tier": "SOMETHING"}, {"tier": "NORMAL"}):
        assert jev_budget_alarms(broken, now) == ["JEV_BUDGET_STATUS_UNAVAILABLE"]
    status = {"worker_state": "RUNNING", "jev_budget": section("EXHAUSTED", now)}
    assert "JEV_BUDGET_EXHAUSTED" in status_alarms(status, now, {
        "tick_max_age_seconds": 15, "reconciliation_max_age_seconds": 90,
        "research_max_age_seconds": 180})
    assert jb.ALERT_SECONDS == 1800
    assert "jev_budget" in STATUS_FIELDS and "jev_budget" in STATUS_KEYS
