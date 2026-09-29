"""research_agent.build: the AGENT_RESEARCH_REPORT_V3 builder.

Offline only (httpx.MockTransport for news verification). The central claim this file
checks: a report this module builds is one the app's OWN report-V3 parser and dossier
compiler (imported directly from catalyst_lab, not through research_agent's wrapper)
accept without modification -- not just that research_agent's own code is internally
consistent.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_V3_SHA256, MUSE_GUIDELINES_V3_VERSION
from catalyst_lab.research_dossier_v3 import compile_pick_dossier
from catalyst_lab.research_report_v3 import UniverseSnapshot, check_pick, parse_report_v3
from catalyst_lab.research_schedule import ResearchSchedule
from research_agent import build, levels
from research_agent import context as context_module
from research_agent.market import Bar

D = Decimal
NOW = datetime(2026, 9, 27, 12, 30, 0, tzinfo=UTC)
SCHEDULE = ResearchSchedule("America/New_York", ("08:00",), 60)


# --- Shared fixture builders -----------------------------------------------------------------

def make_context(symbols_quotes, *, now=NOW, schedule=SCHEDULE):
    slot = schedule.latest_at_or_before(now)
    coins = [
        {"symbol": symbol, "bid": bid, "ask": ask, "quote_at": quote_at,
         "price_increment": "0.01", "min_order_size": "0.01", "quantity_increment": "0.01"}
        for symbol, (bid, ask, quote_at) in symbols_quotes.items()
    ]
    return {
        "context_version": "RESEARCH_CONTEXT_V1", "as_of": now.isoformat(),
        "schedule": {"current_run_slot": schedule.local(slot).isoformat(),
                    "current_run_valid_until_limit":
                        schedule.local(schedule.validity_limit(slot)).isoformat()},
        "report_format": {"guidelines_version": MUSE_GUIDELINES_V3_VERSION,
                          "guidelines_sha256": MUSE_GUIDELINES_V3_SHA256},
        "universe": {"count": len(coins), "coins": coins, "excluded": {}, "issues": []},
        "open_trades": [], "pending_reviews": [], "recent_outcomes": {}, "trade_authorized": False,
    }


def make_sol_setup(market_retrieved_at):
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5  # The window low: the rule-A stop reference.
    lows[15], highs[15] = 95, 95.5  # A held pivot 5% below mid=100: the entry.
    highs[10] = 130  # The window's true peak, not at the last bar: the target.
    bars = [
        Bar(started_at=market_retrieved_at - timedelta(hours=4 * (20 - i)),
           open=D(str(lo)), high=D(str(hi)), low=D(str(lo)), close=D(str(lo)), volume=D(10))
        for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))
    ]
    setup, tried = levels.find_setup({"4h": bars}, mid=D(100), increment=D("0.01"),
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is not None, tried
    return setup


def make_market_data(coin, *, market_retrieved_at, bid="100.09", ask="100.11", ticker_time=None):
    ticker_time = ticker_time or market_retrieved_at
    return {
        "retrieved_at": market_retrieved_at.isoformat(),
        "coinbase": {coin: {"ticker": {"bid": bid, "ask": ask, "time": ticker_time.isoformat()}}},
        "excluded": {},
    }


def _independently_reverify(report, *, now=NOW, agent_id="claude", schedule=SCHEDULE):
    """Re-runs the app's OWN parser, admission-time check and dossier compiler on a
    built report, exactly as the real HTTP route would, independent of
    research_agent.build's own (identical, but separately written) validation call."""
    intake = parse_report_v3(report, schedule=schedule)
    universe = UniverseSnapshot(frozenset(pick["symbol"] for pick in report["picks"]),
                                now, "TEST_UNIVERSE")
    dossiers = []
    for item in intake.picks:
        assert item.code is None, (item.code, item.errors)
        checked, expiry = check_pick(item, intake=intake, universe=universe,
                                     expires_at=intake.valid_until, now=now)
        assert checked.code is None, (checked.code, checked.errors)
        dossiers.append(
            compile_pick_dossier(checked, now=now, agent_id=agent_id, valid_until=expiry)
        )
    return intake, dossiers


# --- The central integration claim: a CHART-only pick the app's own models accept ---------

def test_build_report_produces_a_chart_pick_the_apps_own_models_accept():
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    quote_at = (NOW - timedelta(seconds=30)).isoformat()
    ctx = make_context({"SOL/USD": ("100.09", "100.11", quote_at)})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)

    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, agent_id="claude",
                                agent_version="test-1.0", now=NOW)

    assert result.rejected == () and len(result.accepted) == 1
    assert result.report is not None and len(result.report["picks"]) == 1
    pick = result.report["picks"][0]
    assert pick["kind"] == "CHART" and pick["symbol"] == "SOL/USD"
    assert pick["levels"]["stop"] == "89.64" and pick["levels"]["target"] == "130"

    intake, dossiers = _independently_reverify(result.report)
    assert len(dossiers) == 1
    assert dossiers[0].manifest["state_bytes"] <= 11_000
    assert dossiers[0].manifest["rationale"]["bytes"] <= 3_000


def test_build_report_matches_its_own_internal_validation():
    """research_agent.build already runs every pick through the same app models before
    including it (_validate_pick); the dossier byte count it records must equal what a
    fresh, independent re-check computes."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (NOW - timedelta(seconds=30)).isoformat())})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)
    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, agent_id="claude",
                                agent_version="test-1.0", now=NOW)
    _intake, dossiers = _independently_reverify(result.report)
    assert result.accepted[0].dossier_bytes == dossiers[0].manifest["state_bytes"]


# --- A BOTH pick: a fresh (<=48h) verified catalyst also passes the app's models ----------

CATALYST_HTML = ("<html><head><meta property=\"article:published_time\" "
                 "content=\"{published}\"></head><body>"
                 "<p>The exchange listed a new spot market for the token today.</p>"
                 "</body></html>")


def _news_client(html_text):
    def handle(request):
        return httpx.Response(200, text=html_text)
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_build_report_produces_a_both_pick_with_a_fresh_verified_catalyst():
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    quote_at = NOW - timedelta(seconds=30)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", quote_at.isoformat())})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)
    published = (quote_at - timedelta(hours=12)).isoformat()
    news_by_coin = {"SOL": [build.NewsItem(
        coin="SOL", bucket="catalysts", kind="CATALYST",
        claim="The exchange listed a new spot market for the token today.",
        label=None, url="https://news.example/listing",
        excerpt="The exchange listed a new spot market for the token today.",
        published_at=published,
    )]}

    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, news_by_coin=news_by_coin,
                                agent_id="claude", agent_version="test-1.0", now=NOW,
                                verify_client=_news_client(CATALYST_HTML.format(published=published)))

    assert result.rejected == (), result.rejected
    pick = result.report["picks"][0]
    assert pick["kind"] == "BOTH"
    assert pick["sources"] and pick["sources"][0]["url"] == "https://news.example/listing"
    assert "published 12 hours before this report" in pick["reasoning"]["why_now"]
    _independently_reverify(result.report)  # Also accepted by the app's own models.


def test_build_pick_caps_claims_at_eight_even_with_many_news_items():
    """selection_rationale.claims is capped at 8 by the app's own schema
    (catalyst_lab.muse_reports.SelectionRationale); build_pick always sends 3 fixed
    technical claims (C1-C3), so at most 5 news-sourced claims may be added. A coin with
    more verified risk items than that must still produce an accepted pick, not one the
    app's models reject for having too many claims."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    verified = tuple(
        build.VerifiedItem(
            item=build.NewsItem(coin="SOL", bucket="risks", kind="RISK",
                                claim=f"Risk claim number {i}.", label=f"Risk {i}",
                                url=f"https://x.example/{i}", excerpt="e", published_at=None),
            retrieved_at=NOW.isoformat(),
        )
        for i in range(8)
    )
    news = build.CoinNews(catalyst=None, background=(), risks=verified)
    pick = build.build_pick(
        coin="SOL", symbol="SOL/USD", setup=setup, agent_mid=D("100"),
        agent_price_at=(NOW - timedelta(seconds=30)).isoformat(),
        coinbase_ticker={"bid": "100.09", "ask": "100.11", "time": market_retrieved_at.isoformat()},
        market_retrieved_at=market_retrieved_at, news=news, now=NOW,
    )
    assert len(pick["selection_rationale"]["claims"]) == 8
    outcome = build._validate_pick(0, pick, now=NOW, agent_id="claude",
                                   valid_until=NOW + timedelta(hours=20))
    assert outcome.error is None, outcome.error


def test_a_coin_missing_its_quote_timestamp_is_skipped_not_a_whole_build_crash():
    """A malformed context row (a mid price but no quote_at) must skip that one coin,
    never raise out of build_report and abort every other coin's pick."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (NOW - timedelta(seconds=30)).isoformat())})
    ctx["universe"]["coins"][0]["quote_at"] = None  # Malformed: mid exists, timestamp does not.
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)
    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, agent_id="claude",
                                agent_version="test-1.0", now=NOW)
    assert result.report is None and result.accepted == () and result.rejected == ()
    assert result.skipped[0]["symbol"] == "SOL/USD"


def make_setup_with_target(market_retrieved_at, target_high):
    """The hand-verified rule-A scenario with a variable target, so different coins get
    distinguishable reward:risk values while everything else stays fixed."""
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = 95, 95.5
    highs[10] = target_high
    bars = [
        Bar(started_at=market_retrieved_at - timedelta(hours=4 * (20 - i)),
           open=D(str(lo)), high=D(str(hi)), low=D(str(lo)), close=D(str(lo)), volume=D(10))
        for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))
    ]
    setup, tried = levels.find_setup({"4h": bars}, mid=D(100), increment=D("0.01"),
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is not None, tried
    return setup


def test_build_report_never_exceeds_the_servers_30_pick_cap_by_default():
    """AGENT_RESEARCH_REPORT_V3's envelope refuses the WHOLE report over MAX_PICKS (30)
    picks (catalyst_lab.research_report_v3.ReportEnvelopeV3). A whole-market run with
    more than 30 qualifying coins must still cap itself by default, keeping the highest
    reward:risk setups, not lose the entire morning's report to a size refusal."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    quote_at = (NOW - timedelta(seconds=30)).isoformat()
    coins = [f"C{i:02}" for i in range(32)]  # More than MAX_PICKS (30).
    ctx = make_context({f"{coin}/USD": ("100.09", "100.11", quote_at) for coin in coins})
    market_data = {"retrieved_at": market_retrieved_at.isoformat(), "coinbase": {}, "excluded": {}}
    # C00 has the lowest target (weakest reward:risk); C31 the highest.
    levels_by_coin = {
        coin: (make_setup_with_target(market_retrieved_at, 130 + i), [])
        for i, coin in enumerate(coins)
    }

    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin=levels_by_coin, agent_id="claude",
                                agent_version="test-1.0", now=NOW)

    assert result.rejected == (), result.rejected
    assert len(result.report["picks"]) == 30
    kept_symbols = {pick["symbol"] for pick in result.report["picks"]}
    assert "C00/USD" not in kept_symbols and "C01/USD" not in kept_symbols  # The weakest two.
    assert "C31/USD" in kept_symbols  # The strongest, kept.
    limit_reason = "Report already at its pick limit."
    limited = [row for row in result.skipped if row["reason"] == limit_reason]
    assert {row["symbol"] for row in limited} == {"C00/USD", "C01/USD"}
    _independently_reverify(result.report)  # 30 picks is still within the server's own cap.


def test_a_stale_catalyst_falls_back_to_a_chart_pick_but_still_appears_as_background():
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    quote_at = NOW - timedelta(seconds=30)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", quote_at.isoformat())})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)
    published = (quote_at - timedelta(hours=49)).isoformat()  # Older than 48h: not a catalyst.
    news_by_coin = {"SOL": [build.NewsItem(
        coin="SOL", bucket="catalysts", kind="CATALYST",
        claim="The exchange listed a new spot market for the token today.",
        label=None, url="https://news.example/listing",
        excerpt="The exchange listed a new spot market for the token today.",
        published_at=published,
    )]}
    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, news_by_coin=news_by_coin,
                                agent_id="claude", agent_version="test-1.0", now=NOW,
                                verify_client=_news_client(CATALYST_HTML.format(published=published)))
    assert result.rejected == (), result.rejected
    pick = result.report["picks"][0]
    assert pick["kind"] == "CHART"  # Not BOTH: the catalyst is stale.
    assert pick["sources"]  # But it still appears, as background evidence.
    assert pick["sources"][0]["url"] == "https://news.example/listing"


# --- A coin with no qualifying setup is skipped, not silently dropped ----------------------

def test_a_coin_with_no_setup_is_skipped_with_a_reason():
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (NOW - timedelta(seconds=30)).isoformat())})
    market_data = make_market_data("SOL", market_retrieved_at=NOW - timedelta(minutes=5))
    result = build.build_report(
        context=ctx, market_data=market_data,
        levels_by_coin={"SOL": (None, ["4h/20 rule A: no held higher low"])},
        agent_id="claude", agent_version="test-1.0", now=NOW,
    )
    assert result.report is None and result.accepted == ()
    assert result.skipped == ({"symbol": "SOL/USD",
                               "reason": build.skip_reason(["4h/20 rule A: no held higher low"])},)


def test_the_skip_reason_names_what_the_levels_profile_tried():
    tried = ["1h/30 rule B: the window high is its most recent bar"]
    daily = ("No qualifying pullback setup on any tried rule/timeframe/window (20-30 bars, "
             "4h/6h/1d/2h/1h). Closest miss: 1h/30 rule B: the window high is its most recent "
             "bar")
    assert build.skip_reason(tried) == daily  # The default, DAILY_V1: word for word as before.
    assert build.skip_reason(tried, levels.DAILY_V1) == daily
    assert build.skip_reason(tried, levels.INTRADAY_V1) == daily.replace("4h/6h/1d/2h/1h",
                                                                         "1h/2h/4h")
    assert build.skip_reason(tried, levels.INTRADAY_V2) == daily.replace("4h/6h/1d/2h/1h",
                                                                         "1h/2h/4h/6h/1d")
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (NOW - timedelta(seconds=30)).isoformat())})
    result = build.build_report(
        context=ctx, market_data=make_market_data("SOL", market_retrieved_at=NOW),
        levels_by_coin={"SOL": (None, tried)}, agent_id="claude", agent_version="test-1.0",
        now=NOW, profile=levels.INTRADAY_V1)
    assert result.skipped[0]["reason"] == build.skip_reason(tried, levels.INTRADAY_V1)


# --- Whole-build refusals: offline context, missing schedule -------------------------------

def test_build_refuses_the_offline_context():
    offline = {
        "context_version": context_module.OFFLINE_CONTEXT_VERSION, "as_of": NOW.isoformat(),
        "universe": {}, "excluded": {},
    }
    with pytest.raises(build.BuildError, match="REAL_RESEARCH_CONTEXT_REQUIRED"):
        build.build_report(context=offline, market_data={"retrieved_at": NOW.isoformat(),
                                                          "coinbase": {}},
                           levels_by_coin={}, agent_id="claude", agent_version="v1", now=NOW)


def test_build_refuses_a_context_with_no_schedule():
    ctx = make_context({"SOL/USD": ("100.09", "100.11", NOW.isoformat())})
    ctx["schedule"] = None
    with pytest.raises(build.BuildError, match="RESEARCH_CONTEXT_HAS_NO_SCHEDULE"):
        build.build_report(context=ctx, market_data={"retrieved_at": NOW.isoformat(),
                                                      "coinbase": {}},
                           levels_by_coin={}, agent_id="claude", agent_version="v1", now=NOW)


# --- Session mode: for the harness (scripts/agent_research_session.py), which has no
# GET /api/v1/lab/research-context route --------------------------------------------------

def make_offline_context(symbols_quotes, *, now=NOW):
    """The shape context.offline_universe() produces: no schedule, no report_format."""
    return {
        "context_version": context_module.OFFLINE_CONTEXT_VERSION, "as_of": now.isoformat(),
        "universe": {symbol: {"bid": bid, "ask": ask, "quote_at": quote_at}
                    for symbol, (bid, ask, quote_at) in symbols_quotes.items()},
        "excluded": {},
    }


def test_session_mode_accepts_the_offline_context_and_omits_envelope_scheduling_fields():
    """Verified against scripts/agent_research_session.py's run_submit: it fills
    run_slot/context_as_of/valid_until with dict.setdefault, so a session report must
    OMIT these keys (not set them to null) for the harness to fill them correctly."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    quote_at = (NOW - timedelta(seconds=30)).isoformat()
    ctx = make_offline_context({"SOL/USD": ("100.09", "100.11", quote_at)})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)

    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, agent_id="fable",
                                agent_version="test-1.0", now=NOW, session=True)

    assert result.rejected == () and len(result.accepted) == 1
    assert result.report is not None
    for key in ("run_slot", "context_as_of", "valid_until"):
        assert key not in result.report, key
    # The rest of the envelope, and every pick, are exactly as the normal path builds them.
    assert result.report["schema_version"] == "AGENT_RESEARCH_REPORT_V3"
    assert result.report["agent"]["agent_id"] == "fable"
    # Without a report format the kit declares the guidelines the live context serves (V6).
    assert (result.report["agent"]["guidelines_version"],
            result.report["agent"]["guidelines_sha256"]) == (
        "MUSE_RESEARCH_GUIDELINES_V6",
        "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d")
    pick = result.report["picks"][0]
    assert pick["kind"] == "CHART" and pick["symbol"] == "SOL/USD"
    # Every pick still passes the app's own per-pick models, exactly as in the normal path.
    outcome = build._validate_pick(0, pick, now=NOW, agent_id="fable",
                                   valid_until=NOW + timedelta(hours=20))
    assert outcome.error is None, outcome.error


def test_session_mode_is_a_strict_superset_the_normal_path_still_works_unchanged():
    """session=True must not change behavior at all when a real schedule IS available."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (NOW - timedelta(seconds=30)).isoformat())})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)
    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, agent_id="claude",
                                agent_version="test-1.0", now=NOW, session=True)
    assert "run_slot" in result.report and "context_as_of" in result.report
    assert "valid_until" in result.report


def test_plain_build_without_session_still_refuses_the_offline_context():
    ctx = make_offline_context({"SOL/USD": ("100.09", "100.11", NOW.isoformat())})
    with pytest.raises(build.BuildError, match="REAL_RESEARCH_CONTEXT_REQUIRED"):
        build.build_report(context=ctx, market_data={"retrieved_at": NOW.isoformat(),
                                                      "coinbase": {}},
                           levels_by_coin={}, agent_id="claude", agent_version="v1", now=NOW)


def test_validate_report_needs_default_valid_until_when_the_envelope_omits_it():
    report = {"agent": {"agent_id": "fable", "agent_version": "test-1.0"}, "picks": [],
              "skipped": []}
    with pytest.raises(build.BuildError, match="REPORT_MISSING_VALID_UNTIL"):
        build.validate_report(report, now=NOW)
    result = build.validate_report(report, now=NOW, default_valid_until=NOW + timedelta(hours=20))
    assert result.report is None and result.accepted == () and result.rejected == ()


# --- The budget refusal: an over-budget pick is dropped, never silently included ----------

def _minimal_news_pick(*, now, excerpt_len=50, num_sources=1, reasoning=None, rationale=None):
    sources = [
        {"source_id": f"s{i}", "url": f"https://news.example/{i}", "excerpt": "x" * excerpt_len,
         "published_at": None, "retrieved_at": now.isoformat()}
        for i in range(num_sources)
    ]
    claims = [{"claim_id": "C1", "kind": "CATALYST", "text": "A claim citing the source.",
              "supported_by": {"source_ids": ["s0"], "bar_ids": []}}]
    return {
        "signal_id": "TESTPICK-1", "symbol": "SOL/USD", "kind": "NEWS",
        "agent_current_price": "100.00", "agent_price_at": now.isoformat(),
        "levels": {"entry_trigger": "95", "max_entry_price": "95.15", "stop": "89.64",
                   "target": "130"},
        "stated_reward_risk": "6.32",
        "reasoning": reasoning or {
            "thesis": "Thesis text.", "why_now": "Why now.",
            "why_these_levels": "Why these levels.", "risks": "Risks.",
            "invalidation": "After X: a trade at or below 89.64.",
        },
        "selection_rationale": rationale or {
            "claims": claims, "why_now": "Why now.", "why_these_levels": "Why.",
            "why_over_peers": "Peers.", "what_would_change_my_mind": "A close below stop.",
            "known_risks": [], "agent_confidence": {"level": "LOW", "basis": "x"},
        },
        "sources": sources, "agent_confidence": "0.5",
    }


def test_a_well_formed_small_pick_passes_the_apps_validator():
    outcome = build._validate_pick(0, _minimal_news_pick(now=NOW), now=NOW, agent_id="claude",
                                   valid_until=NOW + timedelta(hours=20))
    assert outcome.error is None and outcome.pick is not None


def test_the_budget_refusal_drops_an_over_budget_pick():
    long_claim = "C" * 300
    claims = [{"claim_id": f"claim{i}", "kind": "RISK", "text": long_claim,
              "supported_by": {"source_ids": ["s0"], "bar_ids": []}} for i in range(8)]
    oversized_rationale = {
        "claims": claims, "why_now": "N" * 500, "why_these_levels": "L" * 500,
        "why_over_peers": "P" * 500, "what_would_change_my_mind": "W" * 300,
        "known_risks": ["R" * 120] * 5, "agent_confidence": {"level": "LOW", "basis": "b"},
    }
    pick = _minimal_news_pick(now=NOW, rationale=oversized_rationale)
    outcome = build._validate_pick(0, pick, now=NOW, agent_id="claude",
                                   valid_until=NOW + timedelta(hours=20))
    assert outcome.error is not None and "DOSSIER_OVER_BUDGET" in outcome.error
    assert outcome.pick is None


def test_build_report_drops_a_rejected_pick_and_records_why(monkeypatch):
    """A pick the app's own validator rejects never reaches the report, and the reason
    is recorded in .rejected / .notes -- never silently dropped."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (NOW - timedelta(seconds=30)).isoformat())})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)

    def fail_everything(index, pick, *, now, agent_id, valid_until):
        return build.PickOutcome(coin="SOL", symbol="SOL/USD", pick=None, dossier_bytes=None,
                                 error="FORCED_TEST_REJECTION")

    monkeypatch.setattr(build, "_validate_pick", fail_everything)
    result = build.build_report(context=ctx, market_data=market_data,
                                levels_by_coin={"SOL": (setup, [])}, agent_id="claude",
                                agent_version="test-1.0", now=NOW)
    assert result.report is None
    assert len(result.rejected) == 1 and result.rejected[0].error == "FORCED_TEST_REJECTION"
    assert any("FORCED_TEST_REJECTION" in note for note in result.notes)


# --- News parsing, verification and the 48-hour catalyst rule ------------------------------

def test_parse_news_accepts_a_well_formed_document():
    raw = {"coins": {"SOL": {
        "catalysts": [{"claim": "A listing.", "kind": "CATALYST",
                       "source": {"url": "https://news.example/a", "excerpt": "text",
                                 "published_at": "2026-09-26T00:00:00Z"}}],
        "risks": [{"claim": "A risk.", "kind": "RISK", "label": "Short label",
                  "source": {"url": "https://news.example/b", "excerpt": "text"}}],
        "fundamentals": [],
    }}}
    parsed = build.parse_news(raw)
    assert len(parsed["SOL"]) == 2
    assert parsed["SOL"][0].bucket == "catalysts" and parsed["SOL"][1].label == "Short label"


@pytest.mark.parametrize("break_it", [
    lambda raw: raw["coins"]["SOL"]["catalysts"][0].__setitem__("claim", ""),
    lambda raw: raw["coins"]["SOL"]["catalysts"][0].__setitem__("kind", "RISK"),
    lambda raw: raw["coins"]["SOL"]["catalysts"][0]["source"].__setitem__("url", "http://x.example"),
    lambda raw: raw["coins"]["SOL"]["catalysts"][0]["source"].__setitem__("excerpt", ""),
])
def test_parse_news_rejects_malformed_items(break_it):
    raw = {"coins": {"SOL": {"catalysts": [{"claim": "A listing.", "kind": "CATALYST",
                                            "source": {"url": "https://news.example/a",
                                                      "excerpt": "text"}}]}}}
    break_it(raw)
    with pytest.raises(build.NewsFormatError):
        build.parse_news(raw)


def test_verify_news_drops_an_unverified_excerpt_and_records_why():
    item = build.NewsItem(coin="SOL", bucket="risks", kind="RISK", claim="A risk claim.",
                          label=None, url="https://news.example/x",
                          excerpt="This text is not on the page.", published_at=None)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="<p>Something else entirely.</p>")))
    kept, dropped = build.verify_news([item], client=client, now=NOW)
    assert kept == [] and len(dropped) == 1 and dropped[0]["coin"] == "SOL"


def test_is_fresh_catalyst_at_the_48_hour_boundary():
    reference = NOW
    exactly_48h = build.NewsItem(coin="X", bucket="catalysts", kind="CATALYST", claim="c",
                                 label=None, url="https://x.example", excerpt="e",
                                 published_at=(reference - timedelta(hours=48)).isoformat())
    just_over_at = reference - timedelta(hours=48, seconds=1)
    just_over = build.NewsItem(coin="X", bucket="catalysts", kind="CATALYST", claim="c",
                               label=None, url="https://x.example", excerpt="e",
                               published_at=just_over_at.isoformat())
    in_the_future = build.NewsItem(coin="X", bucket="catalysts", kind="CATALYST", claim="c",
                                   label=None, url="https://x.example", excerpt="e",
                                   published_at=(reference + timedelta(hours=1)).isoformat())
    no_time = build.NewsItem(coin="X", bucket="catalysts", kind="CATALYST", claim="c", label=None,
                             url="https://x.example", excerpt="e", published_at=None)
    assert build.is_fresh_catalyst(exactly_48h, reference=reference) is True
    assert build.is_fresh_catalyst(just_over, reference=reference) is False
    assert build.is_fresh_catalyst(in_the_future, reference=reference) is False
    assert build.is_fresh_catalyst(no_time, reference=reference) is False


def test_catalyst_age_wording_singular_and_plural():
    reference = NOW
    one_hour = build._catalyst_age_wording((reference - timedelta(hours=1)).isoformat(),
                                           reference=reference)
    twelve_hours = build._catalyst_age_wording((reference - timedelta(hours=12)).isoformat(),
                                               reference=reference)
    assert one_hour == "published 1 hour before this report"
    assert twelve_hours == "published 12 hours before this report"


def test_select_coin_news_buckets_catalysts_risks_and_fundamentals():
    reference = NOW
    fresh = build.VerifiedItem(
        item=build.NewsItem(coin="X", bucket="catalysts", kind="CATALYST", claim="fresh",
                            label=None, url="https://x.example/1", excerpt="e",
                            published_at=(reference - timedelta(hours=1)).isoformat()),
        retrieved_at=reference.isoformat())
    stale = build.VerifiedItem(
        item=build.NewsItem(coin="X", bucket="catalysts", kind="CATALYST", claim="stale",
                            label=None, url="https://x.example/2", excerpt="e",
                            published_at=(reference - timedelta(hours=200)).isoformat()),
        retrieved_at=reference.isoformat())
    fundamental = build.VerifiedItem(
        item=build.NewsItem(coin="X", bucket="fundamentals", kind="NOVELTY", claim="background",
                            label=None, url="https://x.example/3", excerpt="e", published_at=None),
        retrieved_at=reference.isoformat())
    risk = build.VerifiedItem(
        item=build.NewsItem(coin="X", bucket="risks", kind="RISK", claim="a risk", label="Risk",
                            url="https://x.example/4", excerpt="e", published_at=None),
        retrieved_at=reference.isoformat())
    news = build.select_coin_news([fresh, stale, fundamental, risk], reference=reference)
    assert news.catalyst is fresh
    assert set(news.background) == {stale, fundamental}
    assert news.risks == (risk,)


def test_an_agent_block_the_intake_would_refuse_stops_the_build_before_anything_is_built():
    """The first real session run (2026-09-28) built a report with a 34-character agent
    version; the intake refused the whole report (STRING_PATTERN_MISMATCH at
    agent.agent_version). The kit now checks the agent block against the app's patterns."""
    market_retrieved_at = NOW - timedelta(minutes=5)
    setup = make_sol_setup(market_retrieved_at)
    quote_at = (NOW - timedelta(seconds=30)).isoformat()
    ctx = make_context({"SOL/USD": ("100.09", "100.11", quote_at)})
    market_data = make_market_data("SOL", market_retrieved_at=market_retrieved_at)
    kwargs = dict(context=ctx, market_data=market_data, levels_by_coin={"SOL": (setup, [])},
                  now=NOW)
    with pytest.raises(build.BuildError, match="^AGENT_VERSION_INVALID"):
        build.build_report(agent_id="claude", agent_version="claude-research-2026-09-28-v2check",
                           **kwargs)
    with pytest.raises(build.BuildError, match="^AGENT_ID_INVALID"):
        build.build_report(agent_id="Claude", agent_version="test-1.0", **kwargs)
    result = build.build_report(agent_id="claude", agent_version="a" * 32, **kwargs)
    assert result.report["agent"]["agent_version"] == "a" * 32  # the limit itself is allowed
    edited = {**result.report, "agent": {**result.report["agent"], "agent_version": "a" * 33}}
    with pytest.raises(build.BuildError, match="^AGENT_VERSION_INVALID"):
        build.validate_report(edited, now=NOW)



def test_the_pinned_v6_guidelines_match_the_apps_own_constant_once_merged():
    """build pins V6's version and SHA-256 (package learning-app, 391a720) because this
    branch predates the app's constant; after both packages merge, they must agree."""
    from catalyst_lab import muse_guidelines

    if not hasattr(muse_guidelines, "MUSE_GUIDELINES_V6_SHA256"):
        pytest.skip("guidelines V6 is not in this checkout yet (package learning-app)")
    assert (build.GUIDELINES_V6_VERSION, build.GUIDELINES_V6_SHA256) == (
        muse_guidelines.MUSE_GUIDELINES_V6_VERSION, muse_guidelines.MUSE_GUIDELINES_V6_SHA256)


# --- build with lessons: ordering hints only (plan 5, guidelines V6) -------------------------

def _hint(dimension, prefer, text):
    return {"dimension": dimension, "prefer": prefer, "window": "7d", "metric": "m",
            "evidence": {}, "text": text}


def _setup_at_entry(market_retrieved_at, entry_low, *, timeframe_hours=4):
    """The rule-A scenario with the entry pivot at ``entry_low`` (mid 100), so coins sit
    at chosen distances from their entry."""
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = entry_low, entry_low + 0.5
    highs[10] = 130
    bars = [
        Bar(started_at=market_retrieved_at - timedelta(hours=timeframe_hours * (20 - i)),
            open=D(str(lo)), high=D(str(hi)), low=D(str(lo)), close=D(str(lo)), volume=D(10))
        for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))
    ]
    setup, tried = levels.find_setup({"4h": bars}, mid=D(100), increment=D("0.01"),
                                     rules=("A",), timeframes=("4h",), windows=(20,))
    assert setup is not None, tried
    return setup


def _lesson_build(emphasis, *, max_picks=None, coins=("FAR", "MID", "NEAR")):
    market_retrieved_at = NOW - timedelta(minutes=5)
    quote_at = (NOW - timedelta(seconds=30)).isoformat()
    ctx = make_context({f"{coin}/USD": ("99.99", "100.01", quote_at) for coin in coins})
    entries = {"FAR": 95, "MID": 98.5, "NEAR": 99.2}  # 5%, 1.5% and 0.8% below the mid.
    levels_by_coin = {coin: (_setup_at_entry(market_retrieved_at, entries[coin]), [])
                      for coin in coins}
    market_data = {"retrieved_at": market_retrieved_at.isoformat(), "coinbase": {},
                   "excluded": {}}
    return build.build_report(context=ctx, market_data=market_data, levels_by_coin=levels_by_coin,
                              agent_id="claude", agent_version="test-1.0", now=NOW,
                              emphasis=emphasis, max_picks=max_picks)


def test_lessons_reorder_picks_and_say_so_but_never_change_which_coins_are_picked():
    plain = _lesson_build(None)
    hint = _hint("distance_bucket", ["0-1%", "1-2%"],
                 "entries 0-1% from price reached the entry 6 of 8 times, 3%+ 0 of 9, in the "
                 "last 7 days: rank entries 0-1% from price, entries 1-2% from price first")
    ranked = _lesson_build((hint,))
    before = [pick["symbol"] for pick in plain.report["picks"]]
    after = [pick["symbol"] for pick in ranked.report["picks"]]
    assert sorted(before) == sorted(after)  # The same coins, with or without the lesson.
    assert after == ["NEAR/USD", "MID/USD", "FAR/USD"]
    peers = {pick["symbol"]: pick["selection_rationale"]["why_over_peers"]
             for pick in ranked.report["picks"]}
    assert "Ranked early by a lesson from recent results: entries 0-1%" in peers["NEAR/USD"]
    assert "lesson" not in peers["FAR/USD"]  # Not promoted, so no lesson is claimed for it.
    assert all("rule-A" in text for text in peers.values())  # The scorecard still reads it.
    assert ranked.lessons["order_after"] == after
    assert ranked.lessons["promoted"] == {"NEAR/USD": ["distance_bucket"],
                                          "MID/USD": ["distance_bucket"]}
    _independently_reverify(ranked.report)


def test_the_pick_limit_is_applied_before_any_lesson():
    """With room for two picks, the same two coins make the report with or without a hint
    that prefers the third: a lesson never drops a coin."""
    hint = _hint("distance_bucket", ["0-1%"], "entries 0-1% from price reached the entry "
                 "more often: rank them first")
    plain, ranked = _lesson_build(None, max_picks=2), _lesson_build((hint,), max_picks=2)
    assert ({p["symbol"] for p in plain.report["picks"]}
            == {p["symbol"] for p in ranked.report["picks"]})


def test_a_lesson_that_would_cost_a_pick_its_budget_is_left_out_not_the_pick(monkeypatch):
    hint = _hint("distance_bucket", ["0-1%"], "entries 0-1% from price: rank them first")
    real = build._validate_pick

    def refuse_lesson_wording(index, pick, **kwargs):
        if "lesson" in pick["selection_rationale"]["why_over_peers"]:
            return build.PickOutcome(coin="X", symbol=pick["symbol"], pick=None,
                                     dossier_bytes=None, error="DOSSIER_OVER_BUDGET")
        return real(index, pick, **kwargs)

    monkeypatch.setattr(build, "_validate_pick", refuse_lesson_wording)
    result = _lesson_build((hint,), coins=("NEAR",))
    assert [pick["symbol"] for pick in result.report["picks"]] == ["NEAR/USD"]
    assert any("lesson wording was left out" in note for note in result.notes)
    assert result.lessons["promoted"] == {}


def test_a_report_prepared_within_the_grace_answers_the_coming_run():
    """A run's report may be prepared up to the grace (60 minutes) before it. At 07:15 New
    York the context's current run is yesterday's 08:00, whose validity ends at 09:00 today;
    the report must answer today's 08:00 run (the 07:15 combined evening-and-morning run)."""
    at = datetime(2026, 9, 29, 11, 15, tzinfo=UTC)  # 07:15 in New York.
    schedule = {**SCHEDULE.as_dict(), "current_run_slot": "2026-09-28T08:00:00-04:00",
                "current_run_valid_until_limit": "2026-09-29T09:00:00-04:00"}
    assert build.run_slot_for(schedule, at) == ("2026-09-29T08:00:00-04:00",
                                                 "2026-09-30T09:00:00-04:00")
    assert build.run_slot_for(schedule, at - timedelta(minutes=45))[0] == (
        "2026-09-28T08:00:00-04:00")  # 06:30: before the grace, still yesterday's run.
    assert build.run_slot_for(schedule, at + timedelta(minutes=75))[0] == (
        "2026-09-29T08:00:00-04:00")  # 08:30.
    assert build.run_slot_for({"current_run_slot": "x", "current_run_valid_until_limit": "y"},
                              at) == ("x", "y")  # No schedule definition: the context's own.
    # RESEARCH_SCHEDULE_V2: an update run's report is valid until the next daily run's grace.
    loop = {"version": "RESEARCH_SCHEDULE_V2", "timezone": "America/New_York",
            "runs": [f"{hour:02d}:00" for hour in range(0, 24, 2)], "daily": "08:00",
            "grace_minutes": 60}
    assert build.run_slot_for(loop, at + timedelta(hours=2, minutes=10)) == (
        "2026-09-29T10:00:00-04:00", "2026-09-30T09:00:00-04:00")  # 09:25: the 10:00 update.

    market_retrieved_at = at - timedelta(minutes=5)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (at - timedelta(seconds=30)).isoformat())},
                       now=at)
    ctx["schedule"] = schedule
    result = build.build_report(
        context=ctx, market_data=make_market_data("SOL", market_retrieved_at=market_retrieved_at),
        levels_by_coin={"SOL": (make_sol_setup(market_retrieved_at), [])}, agent_id="claude",
        agent_version="test-1.0", now=at)
    assert result.report["run_slot"] == "2026-09-29T08:00:00-04:00"
    assert datetime.fromisoformat(result.report["valid_until"]) == at + build.DEFAULT_VALIDITY
    _independently_reverify(result.report, now=at)  # The app's own intake accepts it.


TWO_HOURLY = ResearchSchedule("America/New_York",
                              tuple(f"{hour:02d}:00" for hour in range(0, 24, 2)), 60)


def make_intraday_setup(market_retrieved_at):
    """A 1-hour INTRADAY_V2 setup: its entry (99.60) 0.5% below the context's mid (100.10),
    inside 0.3%-6% and too close for DAILY_V1's 0.6% minimum."""
    lows, highs = [100] * 20, [101] * 20
    lows[5], highs[5] = 90, 90.5
    lows[15], highs[15] = 99.6, 100.1
    highs[10] = 130
    bars = [Bar(started_at=market_retrieved_at - timedelta(hours=20 - i), open=D(str(lo)),
                high=D(str(hi)), low=D(str(lo)), close=D(str(lo)), volume=D(10))
            for i, (lo, hi) in enumerate(zip(lows, highs, strict=True))]
    daily, _ = levels.find_setup({"1h": bars}, mid=D("100.10"), increment=D("0.01"))
    assert daily is None
    setup, tried = levels.find_setup({"1h": bars}, mid=D("100.10"), increment=D("0.01"),
                                     profile=levels.INTRADAY_V2)
    assert setup is not None and setup.timeframe == "1h", tried
    return setup


def test_an_intraday_report_answers_the_current_two_hour_run():
    """Every 2 hours (00:00-22:00 New York, 60-minute grace): a report built at 10:05
    answers the 10:00 run and stays valid until the next run plus the grace (13:00); one
    started within the hour before 12:00 answers 12:00. The app's own intake accepts it."""
    at = datetime(2026, 9, 29, 14, 5, tzinfo=UTC)  # 10:05 in New York.
    slot = TWO_HOURLY.latest_at_or_before(at)
    schedule = {**TWO_HOURLY.as_dict(), "current_run_slot": TWO_HOURLY.local(slot).isoformat(),
                "current_run_valid_until_limit":
                    TWO_HOURLY.local(TWO_HOURLY.validity_limit(slot)).isoformat()}
    assert build.run_slot_for(schedule, at) == ("2026-09-29T10:00:00-04:00",
                                                 "2026-09-29T13:00:00-04:00")
    assert build.run_slot_for(schedule, at + timedelta(minutes=40))[0] == (
        "2026-09-29T10:00:00-04:00")  # 10:45.
    assert build.run_slot_for(schedule, at + timedelta(minutes=60))[0] == (
        "2026-09-29T12:00:00-04:00")  # 11:05: within the grace before 12:00.

    market_retrieved_at = at - timedelta(minutes=5)
    ctx = make_context({"SOL/USD": ("100.09", "100.11", (at - timedelta(seconds=30)).isoformat())},
                       now=at, schedule=TWO_HOURLY)
    ctx["schedule"] = schedule
    result = build.build_report(
        context=ctx, market_data=make_market_data("SOL", market_retrieved_at=market_retrieved_at),
        levels_by_coin={"SOL": (make_intraday_setup(market_retrieved_at), [])},
        agent_id="muse", agent_version="claude-as-muse-intraday-v1-09.28", now=at,
        max_picks=10, profile=levels.INTRADAY_V2)
    report = result.report
    assert report["run_slot"] == "2026-09-29T10:00:00-04:00"
    assert report["valid_until"] == "2026-09-29T13:00:00-04:00"
    pick = report["picks"][0]
    assert pick["technical_evidence"]["timeframe_seconds"] == 3600
    assert pick["levels"]["entry_trigger"] == "99.6" and "1-hour" in pick["reasoning"]["thesis"]
    _independently_reverify(report, now=at, agent_id="muse", schedule=TWO_HOURLY)
