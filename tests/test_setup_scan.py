from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from hashlib import sha256

import pytest

from catalyst_lab.setup_scan import (
    CompletedBar,
    NewsEvidence,
    QuoteSnapshot,
    ScanAsset,
    ScanPolicy,
    SetupScanner,
    engineering_scan_policy,
)

NOW = datetime(2026, 9, 18, 18, 0, tzinfo=UTC)


@pytest.fixture
def policy():
    return engineering_scan_policy()


@pytest.fixture
def asset():
    bars = []
    for i in range(60):
        close = D("100") + D(i) / 10
        bars.append(CompletedBar(
            NOW - timedelta(minutes=60 - i), NOW - timedelta(minutes=59 - i),
            close - D("0.05"), close + D("0.2"), close - D("0.2"), close,
            D("2000") if i >= 55 else D("1000"),
            "FIXTURE", "TEST_FEED", f"bar-{i}", True,
        ))
    for index, low in ((20, "100"), (44, "103.9"), (55, "105")):
        bars[index] = replace(bars[index], low=D(low))
    for index, high in ((30, "112"), (48, "110")):
        bars[index] = replace(bars[index], high=D(high))
    excerpt = "Fixture issuer reports a new product is available to paying customers."
    news = NewsEvidence(
        "issuer-source", "https://issuer.example/announcement", excerpt,
        NOW - timedelta(hours=1), NOW - timedelta(seconds=10), sha256(excerpt.encode()).hexdigest(),
        True, True, "NEW_FACT", "SUPPORTS",
    )
    return ScanAsset(
        "fixture-asset", "TEST", "US", tuple(bars),
        QuoteSnapshot(D("105.99"), D("106.01"), NOW, "FIXTURE", "TEST_FEED", "quote-1", True),
        (news,), True, D("0.01"), D("1"), D("1"),
        NOW.replace(hour=13, minute=30), NOW.replace(hour=20),
    )


def scan(asset, policy, now=NOW):
    return SetupScanner(policy).scan([asset], now).decisions[0]


def test_contender_uses_observed_structure_and_decimal_metrics(asset, policy):
    result = scan(asset, policy)
    assert result.disposition == "CONTENDER"
    assert result.geometry.entry_trigger == D("105")
    assert result.geometry.max_entry_price == D("105.15")
    assert result.geometry.stop == D("103.9")
    assert result.geometry.target == D("110")
    assert result.geometry.target_bar_id == "bar-48"
    assert result.geometry.reward_risk == D("4.85") / D("1.25")
    assert result.metrics["relative_volume"] == D("2")
    assert result.metrics["spread_bps"] == D("0.02") / D("106") * 10000
    assert result.policy_id == "MUSE_TECH_NEWS_SCAN_ENGINEERING_V1"
    assert len(result.evidence_hash) == 64


def test_nearest_observed_resistance_blocks_manufactured_two_r(asset, policy):
    bars = list(asset.bars)
    bars[48] = replace(bars[48], high=D("106"))
    result = scan(replace(asset, bars=tuple(bars)), policy)
    assert result.geometry.target == D("106")  # Never skip this obstacle to claim 112.
    assert result.geometry.reward_risk < 2
    assert result.disposition == "REJECTED"
    assert "MIN_REWARD_RISK_AT_MAX_ENTRY" in result.reasons


def test_rank_cap_all_assets_accounted_for_no_ai_confidence(asset, policy):
    assets = [replace(asset, asset_id=f"asset-{i:02}", symbol=f"TEST{i}") for i in range(40)]
    batch = SetupScanner(policy).scan(list(reversed(assets)), NOW)
    assert len(batch.decisions) == 40
    assert len(batch.contenders) == 30
    assert batch.contenders[0].asset_id == "asset-00"
    assert batch.contenders[-1].rank == 30
    assert sum(d.disposition == "RANKED_OUT" for d in batch.decisions) == 10
    assert batch.target_minimum_met and batch.research_only
    assert "confidence" not in repr(batch.to_dict())
    assert batch.to_dict()["contenders"][0]["geometry"]["entry_trigger"] == "105"


def test_shortlist_never_padded(asset, policy):
    batch = SetupScanner(policy).scan([asset], NOW)
    assert len(batch.contenders) == 1
    assert not batch.target_minimum_met
    assert batch.research_only


def test_rank_uses_relative_volume_before_geometry_not_provider_judgment(asset, policy):
    lower_vol = replace(asset, asset_id="less-volume", bars=tuple(
        replace(b, volume=D("1000")) for b in asset.bars
    ))
    batch = SetupScanner(policy).scan([lower_vol, asset], NOW)
    assert batch.contenders[0].asset_id == asset.asset_id


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("completed", False, "UNCONFIRMED_BAR"),
    ("high", D("1"), "INVALID_OHLCV"),
    ("volume", D("-1"), "INVALID_OHLCV"),
    ("close", D("NaN"), "INVALID_OHLCV"),
    ("feed", "", "BAR_PROVENANCE_REQUIRED"),
    ("start_at", NOW, "INCOMPLETE_OR_FUTURE_BAR"),
])
def test_bad_bar_is_reasoned_rejection(asset, policy, field, value, reason):
    bars = list(asset.bars)
    bars[-1] = replace(bars[-1], **{field: value})
    result = scan(replace(asset, bars=tuple(bars)), policy)
    assert result.disposition == "REJECTED"
    assert reason in result.reasons
    assert result.geometry is None


def test_missing_bar_does_not_turn_partial_sample_into_signal(asset, policy):
    result = scan(replace(asset, bars=asset.bars[:-1]), policy)
    assert result.reasons == ("INSUFFICIENT_COMPLETED_BARS",)


def test_out_of_order_and_duplicate_bars_rejected(asset, policy):
    bars = list(asset.bars)
    bars[-2] = bars[-3]
    result = scan(replace(asset, bars=tuple(bars)), policy)
    assert "BAR_GAP_OR_ORDER_INVALID" in result.reasons
    assert "DUPLICATE_BAR_SOURCE" in result.reasons


def test_stale_bars_rejected(asset, policy):
    stale = tuple(replace(b, start_at=b.start_at - timedelta(minutes=5),
                          end_at=b.end_at - timedelta(minutes=5)) for b in asset.bars)
    assert "STALE_COMPLETED_BARS" in scan(replace(asset, bars=stale), policy).reasons


@pytest.mark.parametrize(("seconds", "valid"), [(5, True), (6, False), (-1, False)])
def test_quote_age_exact_boundary(asset, policy, seconds, valid):
    asset = replace(asset, quote=replace(asset.quote, timestamp=NOW - timedelta(seconds=seconds)))
    assert ("STALE_OR_FUTURE_QUOTE" not in scan(asset, policy).reasons) is valid


def test_spread_bps_conversion_and_feed_health(asset, policy):
    quote = replace(asset.quote, bid=D("105"), ask=D("106"), healthy=False)
    result = scan(replace(asset, quote=quote), policy)
    assert result.metrics["spread_bps"] == D("1") / D("105.5") * 10000
    assert {"MAX_SPREAD", "DATA_FEED_FAILURE"} <= set(result.reasons)


def test_invalid_and_absent_quotes_recorded(asset, policy):
    assert "QUOTE_REQUIRED" in scan(replace(asset, quote=None), policy).reasons
    inverted = replace(asset.quote, bid=D("107"), ask=D("106"))
    assert "INVALID_QUOTE" in scan(replace(asset, quote=inverted), policy).reasons


@pytest.mark.parametrize("close_hour", [20, 17])
def test_calendar_flatten_boundary_normal_and_early_close(asset, policy, close_hour):
    close = NOW.replace(hour=close_hour)
    before = close - timedelta(minutes=5, microseconds=1)
    boundary = close - timedelta(minutes=5)
    updated = replace(asset, session_close=close)
    assert "OUTSIDE_ENTRY_SESSION" not in scan(updated, policy, before).reasons
    assert "OUTSIDE_ENTRY_SESSION" in scan(updated, policy, boundary).reasons


def test_crypto_weekend_has_no_stock_close_and_accepts_venue_increments(asset, policy):
    weekend = NOW + timedelta(days=1)
    shift = weekend - NOW
    asset = replace(asset, market="CRYPTO", symbol="TEST/USD", session_open=None,
                    session_close=None, quantity_increment=D("0.000001"),
                    minimum_order_size=D("0.01"),
                    quote=replace(asset.quote, timestamp=weekend),
                    bars=tuple(replace(b, start_at=b.start_at + shift, end_at=b.end_at + shift)
                               for b in asset.bars),
                    news=tuple(replace(n, published_at=n.published_at + shift,
                                       retrieved_at=n.retrieved_at + shift) for n in asset.news))
    result = scan(asset, policy, weekend)
    assert result.disposition == "CONTENDER"
    assert result.execution_scope == "PAPER_CANDIDATE"


def test_stock_fractional_metadata_rejected_and_india_remains_research(asset, policy):
    fractional = replace(asset, quantity_increment=D("0.001"))
    assert "WHOLE_SHARES_REQUIRED" in scan(fractional, policy).reasons
    india = scan(replace(asset, market="INDIA"), policy)
    assert india.disposition == "CONTENDER"
    assert india.execution_scope == "RESEARCH_ONLY"


def test_fresh_primary_catalyst_required(asset, policy):
    no_news = scan(replace(asset, news=()), policy)
    assert no_news.disposition == "NEEDS_EVIDENCE"
    old = replace(asset.news[0], published_at=NOW - timedelta(days=3))
    result = scan(replace(asset, news=(old,)), policy)
    assert result.disposition == "NEEDS_EVIDENCE"
    assert result.reasons == ("NO_FRESH_PRIMARY_CATALYST",)


def test_unchanged_news_or_secondary_headline_is_not_fresh_evidence(asset, policy):
    known = replace(asset.news[0], novelty="PREVIOUSLY_KNOWN")
    secondary = replace(asset.news[0], primary_source=False)
    irrelevant = replace(asset.news[0], asset_relevant=False)
    for news in (known, secondary, irrelevant):
        assert scan(replace(asset, news=(news,)), policy).disposition == "NEEDS_EVIDENCE"


def test_future_news_tampered_excerpts_and_missing_provenance_fail_closed(asset, policy):
    for news in (replace(asset.news[0], published_at=NOW + timedelta(hours=1)),
                 replace(asset.news[0], excerpt="changed text"),
                 replace(asset.news[0], url="http://issuer.example/news"),
                 replace(asset.news[0], published_at=NOW.replace(tzinfo=None))):
        assert "NEWS_PROVENANCE_INVALID" in scan(replace(asset, news=(news,)), policy).reasons


def test_adverse_or_withdrawn_evidence_never_omitted_from_good_story(asset, policy):
    for stance, reason in (("ADVERSE", "MATERIAL_ADVERSE_NEWS"),
                           ("WITHDRAWN", "SOURCE_WITHDRAWN")):
        contrary = replace(asset.news[0], source_id="contrary", stance=stance)
        result = scan(replace(asset, news=asset.news + (contrary,)), policy)
        assert result.disposition == "REJECTED"
        assert reason in result.reasons
        assert "contrary" in result.source_ids


def test_news_hash_binds_research_annotation_and_context(asset, policy):
    original = scan(asset, policy)
    changed = scan(replace(asset, tradable=False), policy)
    assert original.evidence_hash != changed.evidence_hash
    changed = scan(replace(asset, news=(replace(asset.news[0], novelty="UNVERIFIED"),)), policy)
    assert original.evidence_hash != changed.evidence_hash


def test_duplicate_asset_retained_as_rejection(asset, policy):
    batch = SetupScanner(policy).scan([asset, asset], NOW)
    assert len(batch.decisions) == 2
    assert len(batch.contenders) == 1
    assert batch.decisions[1].reasons == ("DUPLICATE_ASSET",)


def test_no_silent_constructor_inputs_or_frozen_us_changes(policy):
    from dataclasses import MISSING

    assert all(f.default is MISSING and f.default_factory is MISSING for f in fields(ScanPolicy))
    with pytest.raises(TypeError):
        ScanPolicy()
    with pytest.raises(ValueError, match="FROZEN_US_BOUNDARY_CHANGED"):
        replace(policy, markets=(replace(policy.markets[0], max_spread_bps=D("20")),))
    with pytest.raises(ValueError, match="INDIA_RESEARCH_ONLY"):
        replace(policy, markets=(replace(policy.markets[2], execution_scope="PAPER_CANDIDATE"),))
    with pytest.raises(ValueError, match="CRYPTO_HAS_NO_STOCK_SESSION"):
        replace(policy, markets=(replace(policy.markets[1], flatten_minutes=5),))


def test_naive_scan_time_rejected(asset, policy):
    with pytest.raises(ValueError, match="SCAN_TIMEZONE_REQUIRED"):
        scan(asset, policy, NOW.replace(tzinfo=None))
