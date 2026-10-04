"""research_agent.derivatives, ``derivatives`` and ``build --derivatives``: the daily run's
derivatives context (docs/RESEARCH-LOOP-V2.md 3.6, package research-loop-kit).

Offline only: OKX and Hyperliquid are an ``httpx.MockTransport``; every built pick is checked
again with the app's own report-V3 parser, intake checks and dossier compiler, imported
directly from catalyst_lab.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from catalyst_lab.research_dossier_v3 import compile_pick_dossier
from catalyst_lab.research_report_v3 import UniverseSnapshot, check_pick, parse_report_v3
from research_agent import build, derivatives, levels, run
from research_agent import context as context_module
from research_agent import market as market_module
from research_agent import token as token_module
from tests.research_loop_kit_fixtures import (
    FETCHED_AT,
    FUNDING_MS,
    LATEST_OI_MS,
    V2,
    context_v3,
    derivatives_entry,
    fake_fetch,
    funding_history,
    market_doc,
    oi_history,
    setup_rows,
)

D = Decimal
NOW = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)  # 08:30 New York: the daily run.
MARKET_AT = NOW - timedelta(minutes=3)
AGENT_ID, AGENT_VERSION = "muse", "claude-as-muse-v6-09.29"
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv(token_module.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(token_module.TOKEN_ENV, raising=False)


# --- The providers on a MockTransport -----------------------------------------------------------

INSTRUMENTS = [{"instId": "SOL-USDT-SWAP", "state": "live"},
               {"instId": "PEPE-USDT-SWAP", "state": "live"},
               {"instId": "DOGE-USDT-SWAP", "state": "live"},
               {"instId": "ARB-USDT-SWAP", "state": "suspend"},
               {"instId": "SOL-USD-SWAP", "state": "live"}]
META = {"universe": [{"name": "SOL", "szDecimals": 2}, {"name": "kPEPE", "szDecimals": 0},
                     {"name": "DOGE", "szDecimals": 0},
                     {"name": "ARB", "szDecimals": 1, "isDelisted": True}]}


def providers(*, oi=None, instruments_status=200, oi_status=None, okx_code=None):
    """OKX and Hyperliquid on one MockTransport; every request is recorded."""
    seen = []
    oi = oi or {}
    oi_status = oi_status or {}

    def handle(request):
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "www.okx.com" and request.method == "GET":
            if path == "/api/v5/public/instruments":
                return httpx.Response(instruments_status,
                                      json={"code": "0", "msg": "", "data": INSTRUMENTS})
            if path == "/api/v5/rubik/stat/contracts/open-interest-history":
                inst = request.url.params["instId"]
                if inst in oi_status:
                    return httpx.Response(oi_status[inst], text="upstream error")
                code = (okx_code or {}).get(inst, "0")
                return httpx.Response(200, json={"code": code, "msg": "",
                                                 "data": oi.get(inst, oi_history())})
        if host == "api.hyperliquid.xyz" and request.method == "POST" and path == "/info":
            body = json.loads(request.content)
            if body["type"] == "meta":
                return httpx.Response(200, json=META)
            if body["type"] == "fundingHistory":
                return httpx.Response(200, json=funding_history(body["coin"]))
        return httpx.Response(404, json={"error": "not a fixture route"})

    return seen, REAL_CLIENT(transport=httpx.MockTransport(handle))


def clock():
    return FETCHED_AT


def fetch(coins, **kwargs):
    seen, client = providers(**kwargs)
    doc = derivatives.fetch_derivatives(coins, client=client, clock=clock, okx_sleep=0,
                                        hyperliquid_sleep=0)
    return seen, doc


def test_fetch_reads_open_interest_and_funding_only_from_the_public_endpoints():
    seen, doc = fetch(["SOL"])
    assert doc["schema"] == derivatives.SCHEMA and doc["fetched_at"] == FETCHED_AT.isoformat()
    entry = doc["coins"]["SOL"]
    assert entry["omitted"] == {}
    assert entry["okx"]["inst_id"] == "SOL-USDT-SWAP" and entry["okx"]["period"] == "15m"
    assert entry["okx"]["retrieved_at"] == FETCHED_AT.isoformat()
    assert entry["okx"]["history"] == oi_history()
    assert entry["hyperliquid"]["coin"] == "SOL"
    assert entry["hyperliquid"]["history"] == funding_history("SOL")
    requests = [(request.method, request.url.host, request.url.path) for request in seen]
    assert requests == [
        ("GET", "www.okx.com", "/api/v5/public/instruments"),
        ("POST", "api.hyperliquid.xyz", "/info"),
        ("GET", "www.okx.com", "/api/v5/rubik/stat/contracts/open-interest-history"),
        ("POST", "api.hyperliquid.xyz", "/info")]
    assert dict(seen[0].url.params) == {"instType": "SWAP"}
    assert dict(seen[2].url.params) == {"instId": "SOL-USDT-SWAP", "period": "15m",
                                        "limit": "100"}
    assert json.loads(seen[1].content) == {"type": "meta"}
    since = int((FETCHED_AT - timedelta(hours=6)).timestamp() * 1000)
    assert json.loads(seen[3].content) == {"type": "fundingHistory", "coin": "SOL",
                                           "startTime": since}
    assert all("authorization" not in request.headers for request in seen)  # No key, ever.


def test_coins_without_a_market_are_omitted_never_guessed():
    _seen, doc = fetch(["PEPE", "XTZ", "ARB"])
    pepe = doc["coins"]["PEPE"]
    assert pepe["okx"]["inst_id"] == "PEPE-USDT-SWAP"
    assert pepe["hyperliquid"]["coin"] == "kPEPE"  # Hyperliquid's thousand-unit contract.
    assert doc["coins"]["XTZ"] == {"okx": None, "hyperliquid": None, "omitted": {
        "okx": "NO_OKX_USDT_SWAP: OKX lists no live XTZ-USDT-SWAP",
        "hyperliquid": "NO_HYPERLIQUID_PERPETUAL: Hyperliquid lists neither XTZ nor kXTZ"}}
    arb = doc["coins"]["ARB"]  # Suspended on OKX, delisted on Hyperliquid.
    assert arb["okx"] is None and arb["hyperliquid"] is None
    assert set(arb["omitted"]) == {"okx", "hyperliquid"}
    assert derivatives.omitted(doc) == {"XTZ": doc["coins"]["XTZ"]["omitted"],
                                        "ARB": arb["omitted"]}


def test_one_coins_failed_read_leaves_the_others():
    _seen, doc = fetch(["SOL", "DOGE", "PEPE"], oi_status={"DOGE-USDT-SWAP": 500},
                       okx_code={"PEPE-USDT-SWAP": "51001"})
    assert doc["coins"]["DOGE"]["omitted"] == {"okx": "OKX_HTTP_500"}
    assert doc["coins"]["DOGE"]["hyperliquid"]["coin"] == "DOGE"
    assert doc["coins"]["PEPE"]["omitted"] == {"okx": "OKX_CODE_51001"}
    assert doc["coins"]["SOL"]["omitted"] == {}


def test_an_unreadable_instrument_list_omits_every_okx_figure():
    _seen, doc = fetch(["SOL", "DOGE"], instruments_status=503)
    for coin in ("SOL", "DOGE"):
        assert doc["coins"][coin]["omitted"] == {
            "okx": "OKX_INSTRUMENTS_UNAVAILABLE: OKX_HTTP_503"}
        assert doc["coins"][coin]["hyperliquid"] is not None


def test_no_coin_with_a_setup_means_no_request():
    seen, client = providers()
    doc = derivatives.fetch_derivatives([], client=client, clock=clock)
    assert doc["coins"] == {} and seen == []


def test_hyperliquid_names_are_the_coin_or_its_thousand_unit_contract():
    names = derivatives.hyperliquid_names(META)
    assert names == {"SOL", "kPEPE", "DOGE"}  # ARB is delisted.
    assert derivatives.hyperliquid_name("SOL", names) == "SOL"
    assert derivatives.hyperliquid_name("PEPE", names) == "kPEPE"
    assert derivatives.hyperliquid_name("ARB", names) is None
    assert derivatives.okx_swaps(INSTRUMENTS) == {"SOL-USDT-SWAP", "PEPE-USDT-SWAP",
                                                  "DOGE-USDT-SWAP", "SOL-USD-SWAP"}


# --- The measures ------------------------------------------------------------------------------

def summary_of(oi=None, funding=None, *, coin="SOL", name=None):
    entry = derivatives_entry(oi=oi, funding=funding, name=name or coin,
                              inst_id=f"{coin}-USDT-SWAP")
    return derivatives.summarize(coin, entry)


def test_open_interest_changes_over_4_and_24_hours_in_coin_terms():
    summary = summary_of(oi_history("1000", h4_ccy="990", h24_ccy="950"))
    oi = summary.oi
    assert oi.latest.ts == LATEST_OI_MS and oi.latest.oi_ccy == D("1000")
    assert oi.h4.ts == LATEST_OI_MS - 4 * 3600 * 1000 and oi.h4.oi_ccy == D("990")
    assert oi.h24.ts == LATEST_OI_MS - 24 * 3600 * 1000 and oi.h24.oi_ccy == D("950")
    assert oi.change_4h == D("1.01")  # 1000 / 990 - 1 = 1.0101...%
    assert oi.change_24h == D("5.26")  # 1000 / 950 - 1 = 5.263...%
    assert derivatives.readable(summary)["oi_change_24h_pct"] == "5.26"


def test_a_missing_row_uses_the_one_before_it_and_says_so():
    summary = summary_of(oi_history("1000", h4_ccy="990", skip={16}))
    oi = summary.oi
    assert oi.h4.ts == LATEST_OI_MS - (4 * 60 + 15) * 60 * 1000  # One period earlier.
    assert oi.change_4h == D("0.00")
    sentence = derivatives.neutral_sentence(summary, ["sol-oi"], funding=False)
    assert "was unchanged (0.00%) over 4 hours 15 minutes" in sentence


def test_a_row_of_zero_open_interest_is_an_unpublished_period_not_a_figure():
    """OKX serves a period it has not computed yet as 0 (the daily run of 2026-10-01: the newest
    rows of ten swaps). The latest figure is the newest published row, and a history with no
    published row gives the coin no open-interest figure."""
    history = oi_history("1000", h4_ccy="990", h24_ccy="950")
    period = 15 * 60 * 1000
    unpublished = [[str(LATEST_OI_MS + step * period), "0", "0", "0"] for step in (2, 1)]
    oi = summary_of(unpublished + history).oi
    assert oi.latest.ts == LATEST_OI_MS and oi.latest.oi_ccy == D("1000")
    assert (oi.change_4h, oi.change_24h) == (D("1.01"), D("5.26"))
    zero_h4 = [row if index != 16 else [row[0], "0", "0", "0"]
               for index, row in enumerate(history)]
    oi = summary_of(zero_h4).oi  # The 4-hour row is unpublished: the one before it is used.
    assert oi.h4.ts == LATEST_OI_MS - (4 * 60 + 15) * 60 * 1000 and oi.change_4h == D("0.00")
    nothing = summary_of([[row[0], "0", "0", "0"] for row in history],
                         funding=funding_history("SOL", "0.0000125"))
    assert nothing.oi is None and nothing.funding is not None
    assert derivatives.readable(nothing)["oi_change_24h_pct"] is None


def test_a_short_history_has_no_24_hour_change():
    oi = summary_of(oi_history(rows=50)).oi
    assert oi.h24 is None and oi.change_24h is None and oi.change_4h is not None


def test_the_latest_funding_is_hourly_and_read_exactly():
    funding = summary_of(funding=funding_history("SOL", "0.0000125")).funding
    assert funding.time_ms == FUNDING_MS and funding.rate == D("0.0000125")
    assert funding.rate_text == "0.0000125" and funding.pct_per_hour == D("0.00125")


@pytest.mark.parametrize("rate, h24, crowded", [
    ("0.0001", "950", True),  # Exactly 0.01% per hour, open interest up 5.26%.
    ("0.00025", "999", True),
    ("0.0000999", "950", False),  # Just under 0.01% per hour.
    ("0.0001", "1050", False),  # Open interest down over 24 hours.
    ("0.0001", "999.99", False),  # Up 0.001%: 0.00% as written, not "up".
    ("-0.0002", "950", False),  # Shorts pay: not crowded longs.
])
def test_crowded_long_positioning(rate, h24, crowded):
    summary = summary_of(oi_history("1000", h24_ccy=h24), funding_history("SOL", rate))
    assert summary.crowded is crowded


def test_crowded_needs_both_figures():
    assert not summary_of(oi_history()).crowded
    assert not summary_of(funding=funding_history("SOL", "0.001")).crowded
    assert not summary_of(oi_history(rows=50), funding_history("SOL", "0.001")).crowded


def test_a_malformed_derivatives_file_is_refused_with_its_path():
    with pytest.raises(derivatives.DerivativesFormatError, match="not a RESEARCH_AGENT"):
        derivatives.load({"coins": {}})
    doc = {"schema": derivatives.SCHEMA, "coins": {"SOL": derivatives_entry(oi=[["x"]])}}
    with pytest.raises(derivatives.DerivativesFormatError, match="coins.SOL.okx.history"):
        derivatives.load(doc)
    bad_time = derivatives_entry(funding=funding_history())
    bad_time["hyperliquid"]["retrieved_at"] = "2026-09-29T11:50:07"  # No offset.
    with pytest.raises(derivatives.DerivativesFormatError, match="retrieved_at"):
        derivatives.load({"schema": derivatives.SCHEMA, "coins": {"SOL": bad_time}})


# --- The pick: cited sources, a RISK claim or one sentence ---------------------------------------

def plain_pick(extra_claims=0, thesis=None, sources=0):
    """A built pick's shape (the fields ``variants`` reads and writes)."""
    claims = [{"claim_id": f"C{index}", "kind": "TECHNICAL", "text": "A bar made a low.",
               "supported_by": {"source_ids": [], "bar_ids": ["SOL-1h-x"]}}
              for index in range(1, 4 + extra_claims)]
    return {"symbol": "SOL/USD", "levels": {"entry_trigger": "99.6"},
            "reasoning": {"thesis": thesis or "Chart thesis.", "why_now": "Now.",
                          "why_these_levels": "Levels.", "risks": "Risks.",
                          "invalidation": "Invalid."},
            "selection_rationale": {"claims": claims, "known_risks": ["A", "B", "C"]},
            "sources": [{"source_id": f"s{index}"} for index in range(sources)]}


NEUTRAL = dict(oi=oi_history("1000", h4_ccy="990", h24_ccy="1050"),
               funding=funding_history("SOL", "0.0000125"))
CROWDED = dict(oi=oi_history("1000", h4_ccy="990", h24_ccy="950"),
               funding=funding_history("SOL", "0.00025"))


def test_neutral_figures_are_cited_sources_and_one_thesis_sentence():
    pick = plain_pick()
    [(new, info), *_rest], reasons = derivatives.variants(pick, summary_of(**NEUTRAL))
    assert reasons == [] and info == {"placed": "THESIS_TEXT", "crowded": False,
                                      "source_ids": ["sol-oi", "sol-funding"]}
    assert pick == plain_pick()  # Copied, never changed in place.
    oi_source, funding_source = new["sources"]
    assert oi_source == {
        "source_id": "sol-oi", "url": derivatives.OKX_OPEN_INTEREST_URL,
        "excerpt": "OKX SOL-USDT-SWAP open interest in SOL (oiCcy, 15m rows): 1000 at "
                   "2026-09-29 11:45 UTC; 990 at 2026-09-29 07:45 UTC; 1050 at 2026-09-28 "
                   "11:45 UTC.",
        "published_at": "2026-09-29T11:45:00+00:00",  # The latest row's own time.
        "retrieved_at": "2026-09-29T11:50:07+00:00"}  # The read, to the second.
    assert funding_source == {
        "source_id": "sol-funding", "url": derivatives.HYPERLIQUID_INFO_URL,
        "excerpt": "Hyperliquid SOL fundingHistory, latest entry: fundingRate 0.0000125 at "
                   "2026-09-29 11:00 UTC.",
        "published_at": "2026-09-29T11:00:00+00:00",
        "retrieved_at": "2026-09-29T11:50:07+00:00"}
    assert new["reasoning"]["thesis"] == (
        "Chart thesis. Derivatives context (sources sol-oi, sol-funding): OKX open interest in "
        "SOL rose 1.01% over 4 hours and fell 4.76% over 24 hours (computed from the cited "
        "figures); the latest Hyperliquid funding was 0.00125% per hour.")
    assert new["selection_rationale"] == pick["selection_rationale"]  # No claim for neutral.
    assert {key: value for key, value in new.items() if key not in {"sources", "reasoning"}} \
        == {key: value for key, value in pick.items() if key not in {"sources", "reasoning"}}


def test_crowded_positioning_is_a_risk_claim_citing_both_sources():
    [(new, info), _without_label], _reasons = derivatives.variants(plain_pick(),
                                                                   summary_of(**CROWDED))
    assert info == {"placed": "RISK_CLAIM", "crowded": True,
                    "source_ids": ["sol-oi", "sol-funding"]}
    claim = new["selection_rationale"]["claims"][-1]
    assert claim == {
        "claim_id": "C4", "kind": "RISK",
        "text": "Crowded long positioning: Hyperliquid funding was 0.025% per hour at "
                "2026-09-29 11:00 UTC (at or above 0.01%) while OKX open interest in SOL rose "
                "5.26% over 24 hours and rose 1.01% over 4 hours (computed from the cited "
                "figures).",
        "supported_by": {"source_ids": ["sol-oi", "sol-funding"], "bar_ids": []}}
    assert len(claim["text"]) <= 300
    assert new["selection_rationale"]["known_risks"][-1] == derivatives.CROWDED_LABEL
    assert new["reasoning"] == plain_pick()["reasoning"]  # The claim carries the figures.
    assert _without_label[0]["selection_rationale"]["known_risks"] == ["A", "B", "C"]


def test_crowded_positioning_with_eight_claims_goes_in_the_risks_text():
    [(new, info), *_rest], _reasons = derivatives.variants(plain_pick(extra_claims=5),
                                                           summary_of(**CROWDED))
    assert info["placed"] == "RISKS_TEXT" and len(new["selection_rationale"]["claims"]) == 8
    assert new["reasoning"]["risks"].startswith("Risks. Crowded long positioning:")
    assert new["reasoning"]["risks"].endswith("(sources sol-oi, sol-funding)")


def test_a_long_thesis_puts_the_sentence_in_why_now_and_fuller_ones_add_nothing():
    [(new, info), *_rest], _reasons = derivatives.variants(plain_pick(thesis="x" * 900),
                                                           summary_of(**NEUTRAL))
    assert info["placed"] == "WHY_NOW_TEXT" and new["reasoning"]["thesis"] == "x" * 900
    full = plain_pick(thesis="x" * 1000)
    full["reasoning"]["why_now"] = "y" * 600
    candidates, reasons = derivatives.variants(full, summary_of(**NEUTRAL))
    assert candidates == [] and len(reasons) == 3


def test_leaner_variants_follow_the_full_one_and_one_source_can_fit():
    candidates, _reasons = derivatives.variants(plain_pick(), summary_of(**NEUTRAL))
    assert [info["source_ids"] for _pick, info in candidates] == [
        ["sol-oi", "sol-funding"], ["sol-oi"], ["sol-funding"]]
    candidates, reasons = derivatives.variants(plain_pick(sources=7), summary_of(**NEUTRAL))
    assert [info["source_ids"] for _pick, info in candidates] == [["sol-oi"], ["sol-funding"]]
    assert reasons == ["no room for 2 more sources (8 at most)"]


def test_a_thousand_unit_contract_says_so_in_its_excerpt():
    summary = summary_of(funding=funding_history("kPEPE", "0.0000125"), coin="PEPE",
                         name="kPEPE")
    [(new, _info), *_rest], _reasons = derivatives.variants(plain_pick(), summary)
    assert new["sources"][-1]["excerpt"].startswith(
        "Hyperliquid kPEPE (1,000 PEPE) fundingHistory, latest entry: fundingRate 0.0000125")
    assert new["sources"][-1]["source_id"] == "pepe-funding"


# --- build --derivatives: the app's own models, and the same picks ------------------------------

COINS = ["SOL/USD", "ETH/USD", "DOGE/USD"]


def build_inputs():
    ctx = context_v3(COINS, now=NOW)
    rows = setup_rows(end=MARKET_AT.replace(minute=0))
    raw_market = market_doc({"SOL": rows, "ETH": rows, "DOGE": rows}, retrieved_at=MARKET_AT)
    levels_by_coin = {}
    for symbol in COINS:
        coin = symbol.split("/")[0]
        series = market_module.all_series(raw_market["coinbase"][coin], retrieved_at=MARKET_AT,
                                          timeframes=levels.INTRADAY_V2.timeframes)
        quote = context_module.coin_quote(ctx, symbol)
        levels_by_coin[coin] = levels.find_setup(series, mid=context_module.mid_price(quote),
                                                 increment=D("0.01"), profile=levels.INTRADAY_V2)
    return ctx, raw_market, levels_by_coin


def summaries():
    doc = {"schema": derivatives.SCHEMA, "coins": {
        "SOL": derivatives_entry(**CROWDED),
        "ETH": derivatives_entry(**NEUTRAL, name="ETH", inst_id="ETH-USDT-SWAP"),
        "DOGE": {"okx": None, "hyperliquid": None,
                 "omitted": {"okx": "NO_OKX_USDT_SWAP: OKX lists no live DOGE-USDT-SWAP"}}}}
    doc["coins"]["ETH"]["hyperliquid"]["history"] = funding_history("ETH", "0.0000125")
    return derivatives.load(doc)


def build_with(**kwargs):
    ctx, raw_market, levels_by_coin = build_inputs()
    return build.build_report(context=ctx, market_data=raw_market, levels_by_coin=levels_by_coin,
                              agent_id=AGENT_ID, agent_version=AGENT_VERSION, now=NOW,
                              profile=levels.INTRADAY_V2, **kwargs)


def reverify(report):
    """The app's own parser, intake checks and dossier compiler, as the route runs them."""
    intake = parse_report_v3(report, schedule=V2)
    universe = UniverseSnapshot(frozenset(COINS), NOW, "TEST_UNIVERSE")
    dossiers = {}
    for item in intake.picks:
        assert item.code is None, (item.code, item.errors)
        checked, expiry = check_pick(item, intake=intake, universe=universe,
                                     expires_at=intake.valid_until, now=NOW)
        assert checked.code is None, (checked.code, checked.errors)
        dossiers[item.pick.symbol] = compile_pick_dossier(checked, now=NOW, agent_id=AGENT_ID,
                                                          valid_until=expiry)
    return dossiers


def test_built_picks_with_the_context_pass_the_apps_own_models():
    result = build_with(derivatives=summaries())
    by_symbol = {pick["symbol"]: pick for pick in result.report["picks"]}
    dossiers = reverify(result.report)
    assert all(dossier.manifest["state_bytes"] <= 11_000 for dossier in dossiers.values())
    sol = by_symbol["SOL/USD"]
    assert sol["selection_rationale"]["claims"][-1]["kind"] == "RISK"
    assert [source["source_id"] for source in sol["sources"]] == ["sol-oi", "sol-funding"]
    assert dossiers["SOL/USD"].manifest["rationale"]["cited_source_ids"] == ["sol-funding",
                                                                             "sol-oi"]
    eth = by_symbol["ETH/USD"]
    assert "Derivatives context (sources eth-oi, eth-funding)" in eth["reasoning"]["thesis"]
    assert by_symbol["DOGE/USD"]["sources"] == []  # No figures: nothing added.
    record = result.derivatives
    assert {row["symbol"] for row in record["attached"]} == {"ETH/USD", "SOL/USD"}
    assert record["left_out"] == [{"symbol": "DOGE/USD", "reason": "no OKX or Hyperliquid "
                                   "figures for this coin; the pick is sent without the "
                                   "derivatives context"}]
    assert record["missing"] == []


def test_the_context_never_changes_the_picks_their_order_or_levels():
    plain, with_context = build_with(), build_with(derivatives=summaries())
    assert [pick["symbol"] for pick in plain.report["picks"]] == [
        pick["symbol"] for pick in with_context.report["picks"]]
    for before, after in zip(plain.report["picks"], with_context.report["picks"], strict=True):
        for field in ("symbol", "kind", "levels", "stated_reward_risk", "technical_evidence",
                      "agent_current_price", "agent_price_at", "agent_confidence"):
            assert before[field] == after[field], field
    assert plain.skipped == with_context.skipped and plain.derivatives is None


def test_a_pick_the_app_would_refuse_with_the_context_is_sent_without_it(monkeypatch):
    """The dossier budget (11,000 bytes) can refuse the context: the richest variant the
    app's models accept is kept, else none; the pick itself always stays."""
    real = build._validate_pick

    def over_budget_with(ids):
        def check(index, pick, **kwargs):
            cited = {source["source_id"] for source in pick["sources"]}
            if ids <= cited:
                return build.PickOutcome(coin="x", symbol=pick["symbol"], pick=None,
                                         dossier_bytes=None, error="DOSSIER_OVER_BUDGET")
            return real(index, pick, **kwargs)
        return check

    monkeypatch.setattr(build, "_validate_pick", over_budget_with({"eth-oi", "eth-funding"}))
    record = build_with(derivatives=summaries()).derivatives
    [eth] = [row for row in record["attached"] if row["symbol"] == "ETH/USD"]
    assert eth["source_ids"] == ["eth-oi"]
    assert eth["without"] == ["DOSSIER_OVER_BUDGET with eth-oi, eth-funding"]
    # A crowded claim is never cut down to one of its figures: without both, nothing.
    monkeypatch.setattr(build, "_validate_pick", over_budget_with({"sol-oi"}))
    result = build_with(derivatives=summaries())
    [sol] = [row for row in result.derivatives["left_out"] if row["symbol"] == "SOL/USD"]
    assert sol["reason"].startswith("DOSSIER_OVER_BUDGET with sol-oi, sol-funding; "
                                    "DOSSIER_OVER_BUDGET with sol-oi, sol-funding")
    [pick] = [pick for pick in result.report["picks"] if pick["symbol"] == "SOL/USD"]
    assert pick["sources"] == []


# --- The CLI: derivatives, then build --derivatives ---------------------------------------------

def _prepared_run(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "2026-09-29"
    token_path = tmp_path / "token"
    token_path.write_text("fixture-derivatives-token")
    token_path.chmod(0o600)
    ctx = context_v3(COINS + ["XTZ/USD"], now=NOW)
    monkeypatch.setattr(context_module, "fetch_context", lambda *a, **k: ctx)
    rows = setup_rows(end=MARKET_AT.replace(minute=0))
    monkeypatch.setattr(market_module, "fetch_coinbase", fake_fetch(
        {"SOL": rows, "ETH": rows, "DOGE": rows}, retrieved_at=MARKET_AT))
    for argv in (["context", "--base-url", "https://app.example", "--token-file",
                  str(token_path)], ["market", "--profile", "intraday"],
                 ["levels", "--profile", "intraday"]):
        assert run.main([*argv, "--run-dir", str(run_dir)]) == 0
    return run_dir


def test_the_derivatives_command_reads_the_coins_with_a_setup(tmp_path, monkeypatch, capsys):
    run_dir = _prepared_run(tmp_path, monkeypatch)
    seen, client = providers()
    monkeypatch.setattr(derivatives.httpx, "Client", lambda **kw: client)
    monkeypatch.setattr(derivatives.time, "sleep", lambda seconds: None)
    assert run.main(["derivatives", "--run-dir", str(run_dir), "--now",
                     FETCHED_AT.isoformat()]) == 0
    doc = json.loads((run_dir / "derivatives.json").read_text())
    assert sorted(doc["coins"]) == ["DOGE", "ETH", "SOL"]  # XTZ has no setup: not read.
    assert doc["coins"]["ETH"]["omitted"] == {
        "okx": "NO_OKX_USDT_SWAP: OKX lists no live ETH-USDT-SWAP",
        "hyperliquid": "NO_HYPERLIQUID_PERPETUAL: Hyperliquid lists neither ETH nor kETH"}
    assert doc["coins"]["SOL"]["measures"] == {
        "oi_change_4h_pct": "1.01", "oi_change_24h_pct": "5.26",
        "oi_latest_at": "2026-09-29T11:45:00+00:00", "funding_pct_per_hour": "0.00125",
        "funding_at": "2026-09-29T11:00:00.076000+00:00", "crowded": False}
    assert {request.url.host for request in seen} == {"www.okx.com", "api.hyperliquid.xyz"}
    assert "OKX open interest for 2, Hyperliquid funding for 2" in capsys.readouterr().out


def test_build_with_derivatives_records_them_and_build_without_is_unchanged(tmp_path,
                                                                            monkeypatch):
    run_dir = _prepared_run(tmp_path, monkeypatch)
    agent = ["--profile", "intraday", "--agent-id", AGENT_ID, "--agent-version", AGENT_VERSION,
             "--now", NOW.isoformat()]
    assert run.main(["build", "--run-dir", str(run_dir), *agent]) == 0
    plain_notes = json.loads((run_dir / "build-notes.json").read_text())
    assert "derivatives" not in plain_notes
    assert list(plain_notes) == ["profile", "accepted", "rejected", "skipped", "notes",
                                 "lessons"]
    plain_report = json.loads((run_dir / "report.json").read_text())
    doc = {"schema": derivatives.SCHEMA, "fetched_at": FETCHED_AT.isoformat(),
           "coins": {"SOL": derivatives_entry(**CROWDED)}}
    (run_dir / "derivatives.json").write_text(json.dumps(doc))
    assert run.main(["build", "--run-dir", str(run_dir), *agent, "--derivatives",
                     str(run_dir / "derivatives.json")]) == 0
    notes = json.loads((run_dir / "build-notes.json").read_text())
    assert notes["accepted"] == plain_notes["accepted"]
    assert notes["derivatives"]["file"] == str(run_dir / "derivatives.json")
    assert [row["symbol"] for row in notes["derivatives"]["attached"]] == ["SOL/USD"]
    assert notes["derivatives"]["missing"] == ["DOGE/USD", "ETH/USD"]
    report = json.loads((run_dir / "report.json").read_text())
    assert [pick["levels"] for pick in report["picks"]] == [
        pick["levels"] for pick in plain_report["picks"]]
    assert run.main(["validate", "--run-dir", str(run_dir), "--now", NOW.isoformat()]) == 0


def test_build_refuses_a_file_that_is_not_derivatives_json(tmp_path, monkeypatch, capsys):
    run_dir = _prepared_run(tmp_path, monkeypatch)
    (run_dir / "news.json").write_text(json.dumps({"coins": {}}))
    assert run.main(["build", "--run-dir", str(run_dir), "--profile", "intraday", "--agent-id",
                     AGENT_ID, "--agent-version", AGENT_VERSION, "--derivatives",
                     str(run_dir / "news.json")]) == 2
    assert "not a RESEARCH_AGENT_DERIVATIVES_V1 file" in capsys.readouterr().err
