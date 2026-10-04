"""Build ``AGENT_RESEARCH_REPORT_V3``: literal claims only, every excerpt re-verified,
every pick checked with the app's own models before it is offered for submission.

This is the one module that imports ``catalyst_lab``, deliberately: it validates its own
output with the app's real models and dossier compiler, exactly what
research3/builder3.py (the reference implementation) does. Nothing here is imported back
by the app; the dependency is one-way, the same shape as the app's own research boundary
(``docs/MUSE-RESEARCH-BOUNDARY.md``): research happens outside the app, which only ever
receives a finished report over HTTP.

News handling (round 3 of 2026-09-27 found research subagents inventing
``retrieved_at`` and paraphrasing excerpts — see ``sources.py``): every ``news.json``
excerpt is re-fetched and checked (``sources.verify_excerpt``) before it can appear in a
pick; a catalyst counts only when it was first made public within 48 hours before the
pick's ``agent_price_at`` (``is_fresh_catalyst``) — older verified news may still appear
as background on a CHART pick; it just does not turn the pick BOTH.

Session mode (``build_report(..., session=True)``, CLI ``build --session``): the
supervised session harness (``scripts/agent_research_session.py``) has no ``GET
/api/v1/lab/research-context`` route (its ``create_managed_app`` call passes no
``research_context``), so a session test has no real context to build from — only the
offline development fallback. ``session=True`` accepts that (or any context missing a
configured schedule) instead of refusing, and — verified by reading the harness's own
``run_submit``, which fills exactly these three with ``dict.setdefault`` — omits
``run_slot``/``context_as_of``/``valid_until`` from the envelope so the harness's own
``submit`` command fills them from its actual configured schedule at real submission
time, which is more correct than anything this module could compute without seeing it.
Every pick is still built and checked exactly as in the normal path. A session report is
for the harness's own ``submit`` only; this module's ``SESSION_MARKER_FILENAME`` and
``submit.py``/``run.py`` refuse to send one to a live app.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from catalyst_lab.agent_identity import AGENT_ID, AGENT_VERSION
from catalyst_lab.research_dossier_v3 import compile_pick_dossier
from catalyst_lab.research_evidence import rationale_reference_errors
from catalyst_lab.research_report_v3 import MAX_PICKS, REPORT_SCHEMA_V3, _parse_pick
from catalyst_lab.research_schedule import ResearchSchedule
from research_agent import context as context_module
from research_agent import derivatives as derivatives_module
from research_agent import evidence as evidence_module
from research_agent import lessons, levels, sources
from research_agent.market import TIMEFRAMES

TIMEFRAME_WORDS = {"1h": "1-hour", "2h": "2-hour", "4h": "4-hour", "6h": "6-hour", "1d": "daily"}
CATALYST_FRESHNESS = timedelta(hours=48)
# Under the server's 24h cap (REPORT_VALIDITY_OVER_24_HOURS). 23 h 50 min from 2026-09-29, so
# the daily run's picks live until about the next daily run (owner, 2026-09-28 evening: picks
# "live for 24 hours"); it was 20 h, as builder3.py used. The schedule's own limit still caps it.
DEFAULT_VALIDITY = timedelta(hours=23, minutes=50)
# Written into the run folder by `build --session` (see run.py); its presence is what
# this package's own `submit` checks to refuse sending a session-only report to a live app.
SESSION_MARKER_FILENAME = "SESSION_ONLY"
OPEN_TRADE_SKIP_REASON = "A trade on this coin is open; its own review decides it."
EXCLUDED_SKIP_REASON = "Left out by the operator's evidence filter (--exclude)."


# The operator's evidence filter (owner direction 2026-10-02: no new pick without solid
# evidence). ``exclude``: coins never offered as new picks, e.g. the coins that never filled on
# Alpaca paper (POL, LDO and WIF to 2026-10-02). ``max_entry_distance``: a new pick whose
# maximum entry sits further under the live Alpaca mid than this fraction is left out (far
# entries rarely trigger: 7% of picks 3%+ away, against 57% within 2%, 2026-09-27 to 10-01).
# Neither applies to ``first_coins`` (an update's adjusted picks: setups the agent already
# holds). Both are recorded in the report's ``skipped`` rows and the update's ``left_out``.
@dataclass(frozen=True)
class EvidenceFilter:
    exclude: frozenset = frozenset()
    max_entry_distance: Decimal | None = None
    # Method v8 (``research_agent/evidence.py``), each optional and for new picks only:
    # the coin's Alpaca 24-hour USD volume must reach this unless the agent filled a trade of
    # it in the context's window; the live mid may sit at most this far under the coin's
    # 20-day average (a fraction; 0 = at or above it); a market-wide drop of at least this
    # much over two hours (the median coin or BTC) leaves every new coin out of this run.
    min_alpaca_volume_usd: Decimal | None = None
    trend_floor: Decimal | None = None
    selloff: Decimal | None = None

    @staticmethod
    def parse_exclude(text):
        """``"pol, ldo,WIF"`` -> ``frozenset({"POL", "LDO", "WIF"})``; blank -> empty."""
        coins = frozenset(part.strip().upper() for part in (text or "").split(",")
                          if part.strip())
        if any(not coin.isalnum() for coin in coins):
            raise ValueError("EXCLUDE_COINS_INVALID")
        return coins

    @staticmethod
    def _percent(text, *, lowest, highest, code):
        try:
            pct = Decimal(str(text).strip())
        except (ArithmeticError, ValueError):
            raise ValueError(code) from None
        if not pct.is_finite() or not lowest <= pct <= highest:
            raise ValueError(code)
        return pct / 100

    @classmethod
    def parse_distance_pct(cls, text):
        """``"2.5"`` (a percent) -> ``Decimal("0.025")``; refuses anything but 0 < x <= 50."""
        value = cls._percent(text, lowest=Decimal("0"), highest=Decimal(50),
                             code="MAX_ENTRY_DISTANCE_INVALID")
        if value == 0:
            raise ValueError("MAX_ENTRY_DISTANCE_INVALID")
        return value

    @classmethod
    def parse_trend_floor_pct(cls, text):
        """``"-2"`` (a percent, −50..50) -> ``Decimal("-0.02")``: how far under its 20-day
        average a coin may trade (0: at or above it)."""
        return cls._percent(text, lowest=Decimal(-50), highest=Decimal(50),
                            code="TREND_FLOOR_INVALID")

    @classmethod
    def parse_selloff_pct(cls, text):
        """``"2"`` (a percent, 0 < x <= 50) -> ``Decimal("0.02")``: the two-hour drop that
        counts as a sell-off."""
        value = cls._percent(text, lowest=Decimal(0), highest=Decimal(50),
                             code="SELLOFF_INVALID")
        if value == 0:
            raise ValueError("SELLOFF_INVALID")
        return value

    @staticmethod
    def parse_usd(text):
        """``"5000"`` -> ``Decimal("5000")``; refuses a negative or non-numeric amount."""
        try:
            amount = Decimal(str(text).strip().replace(",", ""))
        except (ArithmeticError, ValueError):
            raise ValueError("MIN_ALPACA_VOLUME_INVALID") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("MIN_ALPACA_VOLUME_INVALID")
        return amount

    def distance_reason(self, distance):
        return (f"Entry {distance * 100:.2f}% under the price; the operator's evidence limit "
                f"is {self.max_entry_distance * 100:.2f}% (--max-entry-distance-pct).")

    def volume_reason(self, volume, window):
        figure = f"${volume:,.0f}" if volume is not None else "unknown"
        days = f" in the last {window} days" if window else ""
        return (f"Alpaca 24 h volume {figure} is under the evidence minimum "
                f"${self.min_alpaca_volume_usd:,.0f} and the agent has no fill on Alpaca{days}.")

    def trend_reason(self, trend):
        if trend is None:
            return "No 20 completed daily bars to judge the trend against its 20-day average."
        return (f"{trend * 100:+.1f}% vs its 20-day average: a pullback in a downtrend "
                f"(the evidence floor is {self.trend_floor * 100:+.1f}%).")

    def selloff_reason(self, state):
        median, btc = state.get("median_2h"), state.get("btc_2h")
        return (f"Sell-off in progress: the median coin {evidence_module.pct(median, 2)} and BTC "
                f"{evidence_module.pct(btc, 2)} in 2 h (limit -{self.selloff * 100:.2f}%); "
                "no new picks this run.")
# The guidelines the live research context serves from package learning-app (391a720): V5 byte
# for byte, then the learning loop. A live run declares whatever the context's report_format
# names; only a context without one (session mode) falls back to these, pinned here because
# this branch predates the app's constant. tests/test_research_agent_build.py compares them
# with catalyst_lab.muse_guidelines once both packages are merged.
GUIDELINES_V6_VERSION = "MUSE_RESEARCH_GUIDELINES_V6"
GUIDELINES_V6_SHA256 = "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d"
FIXED_TECHNICAL_CLAIMS = 3  # C1 (entry bar), C2 (stop bar), C3 (target bar): always sent.
# selection_rationale.claims is capped at 8 (catalyst_lab.muse_reports.SelectionRationale);
# the three fixed technical claims above leave room for at most 5 news-sourced ones.
MAX_NEWS_CLAIMS = 8 - FIXED_TECHNICAL_CLAIMS
NEWS_BUCKETS = {"catalysts": "CATALYST", "risks": "RISK", "fundamentals": None}
_FUNDAMENTAL_KINDS = frozenset({"NOVELTY", "ECONOMIC_LINK", "TECHNICAL"})


class BuildError(Exception):
    """A whole-build refusal (missing schedule, offline context, ...)."""


class NewsFormatError(Exception):
    """``news.json`` is structurally invalid; the message names the offending path."""


def plain(value):
    """A Decimal as a fixed-point string ("1E-9" reads as "0.000000001"): matches
    research3/builder3.py's ``plain`` and ``research_context.py``'s ``_plain``."""
    value = Decimal(value)
    return (format(value.normalize(), "f") if value != value.to_integral()
            else format(value.quantize(Decimal(1)), "f"))


def _when(moment):
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# --- news.json: parsing --------------------------------------------------------------------

@dataclass(frozen=True)
class NewsItem:
    coin: str
    bucket: str  # "catalysts" | "risks" | "fundamentals" (input organization only)
    kind: str  # a RationaleClaim kind: CATALYST, RISK, NOVELTY, ECONOMIC_LINK or TECHNICAL
    claim: str
    label: str | None  # a short risk label, folded into known_risks; else unused
    url: str
    excerpt: str
    published_at: str | None  # as declared in news.json, before re-verification


def parse_news(raw):
    """``news.json``'s ``{"coins": {SYMBOL: {"catalysts"/"risks"/"fundamentals": [...]}}}``
    into ``{coin: [NewsItem, ...]}``. Raises ``NewsFormatError`` naming the bad path; this
    is a friendly local check, not the app's own privacy/format screen (which still runs
    later, in ``compile_pick_dossier``, on anything that reaches a pick)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("coins"), dict):
        raise NewsFormatError("news.json: top level must be an object with a 'coins' object")
    by_coin = {}
    for coin, doc in raw["coins"].items():
        if not isinstance(coin, str) or not coin:
            raise NewsFormatError("news.json.coins: keys must be non-empty coin symbols")
        if not isinstance(doc, dict):
            raise NewsFormatError(f"news.json.coins.{coin}: must be an object")
        items = []
        for bucket, required_kind in NEWS_BUCKETS.items():
            raw_items = doc.get(bucket) or []
            if not isinstance(raw_items, list):
                raise NewsFormatError(f"news.json.coins.{coin}.{bucket}: must be a list")
            for index, raw_item in enumerate(raw_items):
                items.append(_parse_news_item(coin, bucket, required_kind, raw_item,
                                              f"coins.{coin}.{bucket}[{index}]"))
        by_coin[coin] = items
    return by_coin


def _parse_news_item(coin, bucket, required_kind, raw_item, path):
    if not isinstance(raw_item, dict):
        raise NewsFormatError(f"{path}: must be an object")
    claim = raw_item.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        raise NewsFormatError(f"{path}.claim: required non-empty string")
    kind = raw_item.get("kind")
    allowed = {required_kind} if required_kind else _FUNDAMENTAL_KINDS
    if kind not in allowed:
        raise NewsFormatError(f"{path}.kind: must be one of {sorted(allowed)}")
    source = raw_item.get("source")
    if not isinstance(source, dict):
        raise NewsFormatError(f"{path}.source: required object")
    url = source.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise NewsFormatError(f"{path}.source.url: required public https:// URL")
    excerpt = source.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise NewsFormatError(f"{path}.source.excerpt: required non-empty string")
    published_at = source.get("published_at")
    if published_at is not None and not isinstance(published_at, str):
        raise NewsFormatError(f"{path}.source.published_at: must be a string or null")
    label = raw_item.get("label")
    if label is not None and not isinstance(label, str):
        raise NewsFormatError(f"{path}.label: must be a string or null")
    return NewsItem(coin=coin, bucket=bucket, kind=kind, claim=claim.strip(), label=label,
                    url=url, excerpt=excerpt, published_at=published_at)


# --- news.json: re-fetch and verify, then the 48-hour catalyst rule ------------------------

@dataclass(frozen=True)
class VerifiedItem:
    item: NewsItem
    retrieved_at: str  # this verification's own fetch time (RFC3339), never invented


def verify_news(items, *, client=None, now):
    """Re-fetches and checks every item's excerpt (``sources.verify_excerpt``).

    Returns ``(kept, dropped)``; ``dropped`` entries are ``{"coin", "bucket", "claim",
    "reason"}`` for the run summary. This does not apply the 48-hour catalyst rule —
    that depends on each pick's own ``agent_price_at`` and is applied by
    ``select_coin_news``/``is_fresh_catalyst``.
    """
    kept, dropped = [], []
    for item in items:
        result = sources.verify_excerpt(item.url, item.excerpt, client=client)
        if not result.ok:
            reason = result.status + (f" ({result.detail})" if result.detail else "")
            dropped.append({"coin": item.coin, "bucket": item.bucket, "claim": item.claim[:80],
                            "reason": reason})
            continue
        kept.append(VerifiedItem(item=item, retrieved_at=now.isoformat()))
    return kept, dropped


def is_fresh_catalyst(item, *, reference):
    """A CATALYST item counts only if first made public within 48h before ``reference``
    (the pick's ``agent_price_at``) — never invented, and never just "recently seen"."""
    if item.published_at is None:
        return False
    try:
        published = datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        return False
    age = reference - published.astimezone(UTC)
    return timedelta(0) <= age <= CATALYST_FRESHNESS


@dataclass(frozen=True)
class CoinNews:
    catalyst: VerifiedItem | None = None
    background: tuple = ()  # stale catalysts + fundamentals: may appear on a CHART pick
    risks: tuple = ()


def select_coin_news(verified_items, *, reference):
    """Sorts verified items into the primary (freshest, <=48h) catalyst, background
    (stale catalysts and fundamentals), and risks, for one pick's ``reference`` instant."""
    catalysts = [v for v in verified_items if v.item.bucket == "catalysts"]
    risks = [v for v in verified_items if v.item.bucket == "risks"]
    fundamentals = [v for v in verified_items if v.item.bucket == "fundamentals"]
    fresh = [v for v in catalysts if is_fresh_catalyst(v.item, reference=reference)]
    stale = [v for v in catalysts if v not in fresh]
    catalyst = max(fresh, key=lambda v: v.item.published_at) if fresh else None
    return CoinNews(catalyst=catalyst, background=tuple(stale + fundamentals), risks=tuple(risks))


def _catalyst_age_wording(published_at, *, reference):
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(UTC)
    hours = round((reference - published).total_seconds() / 3600)
    unit = "hour" if hours == 1 else "hours"
    return f"published {hours} {unit} before this report"


def _source_obj(source_id, verified):
    item = verified.item
    return {"source_id": source_id, "url": item.url, "excerpt": item.excerpt,
            "published_at": item.published_at, "retrieved_at": verified.retrieved_at}


# --- One pick from a level setup, an Alpaca quote and (maybe) verified news ----------------

def _structure_note(setup, timeframe_word):
    entry_bar, stop_bar = setup.bars[setup.entry_index], setup.bars[setup.stop_index]
    entry_when, stop_when = _when(entry_bar.started_at), _when(stop_bar.started_at)
    if setup.rule == "A":
        return (f"No cited bar after {entry_when} has traded below {plain(setup.entry)}, and "
                f"none of the {setup.window} cited {timeframe_word} bars has traded below "
                f"{plain(stop_bar.low)} (the {stop_when} low, the lowest of them), so the stop "
                f"{plain(setup.stop)} sits under the whole cited structure.")
    return (f"No cited bar after {entry_when} has traded below {plain(setup.entry)}, and no "
            f"cited bar after {stop_when} has traded below that bar's low of "
            f"{plain(stop_bar.low)}: the structure starts there (earlier cited bars may have "
            "traded lower, before it formed).")


def _stop_basis(setup, timeframe_word):
    stop_bar = setup.bars[setup.stop_index]
    stop_when = _when(stop_bar.started_at)
    if setup.rule == "A":
        return (f"0.4% under {plain(stop_bar.low)}, the lowest low of all {setup.window} "
                f"cited {timeframe_word} bars ({stop_when})")
    return f"0.4% under {plain(stop_bar.low)}, the {stop_when} low that has held since"


def build_pick(*, coin, symbol, setup, agent_mid, agent_price_at, coinbase_ticker,
               market_retrieved_at, news, now, signal_id=None, lesson_note=None,
               evidence_note=None):
    """One ``AgentPick``-shaped dict (``catalyst_lab.research_report_v3.AgentPick``),
    not yet checked against the app's models — ``build_report`` does that next.
    ``lesson_note`` (``lessons.lesson_sentence``) is added to ``why_over_peers`` when a lesson
    ranked this pick early (guidelines V6: name the lesson behind a choice); ``evidence_note``
    (``evidence.sentence``: trend, volume, fills, market) is added to ``why_now``."""
    timeframe_word = TIMEFRAME_WORDS[setup.timeframe]
    entry_bar = setup.bars[setup.entry_index]
    stop_bar = setup.bars[setup.stop_index]
    target_bar = setup.bars[setup.target_index]
    bar_ids = [bar.bar_id(coin, setup.timeframe) for bar in setup.bars]
    claims = [
        {"claim_id": "C1", "kind": "TECHNICAL",
         "text": (f"The {timeframe_word} bar that started {_when(entry_bar.started_at)} made "
                  f"a low of {plain(entry_bar.low)}."),
         "supported_by": {"source_ids": [], "bar_ids": [bar_ids[setup.entry_index]]}},
        {"claim_id": "C2", "kind": "TECHNICAL",
         "text": (f"The {timeframe_word} bar that started {_when(stop_bar.started_at)} made a "
                  f"low of {plain(stop_bar.low)}."),
         "supported_by": {"source_ids": [], "bar_ids": [bar_ids[setup.stop_index]]}},
        {"claim_id": "C3", "kind": "TECHNICAL",
         "text": (f"The {timeframe_word} bar that started {_when(target_bar.started_at)} made "
                  f"a high of {plain(target_bar.high)}."),
         "supported_by": {"source_ids": [], "bar_ids": [bar_ids[setup.target_index]]}},
    ]
    source_list, known_risks, catalyst_claim = [], [], None
    coin_key = coin.lower()
    if news.catalyst is not None:
        source_id = f"{coin_key}-catalyst"
        source_list.append(_source_obj(source_id, news.catalyst))
        claims.append({"claim_id": f"C{len(claims) + 1}", "kind": news.catalyst.item.kind,
                       "text": news.catalyst.item.claim,
                       "supported_by": {"source_ids": [source_id], "bar_ids": []}})
        catalyst_claim = news.catalyst.item.claim
    news_claims = 1 if news.catalyst is not None else 0
    for index, verified in enumerate(news.background):
        if news_claims >= MAX_NEWS_CLAIMS:
            break
        source_id = f"{coin_key}-bg{index + 1}"
        source_list.append(_source_obj(source_id, verified))
        claims.append({"claim_id": f"C{len(claims) + 1}", "kind": verified.item.kind,
                       "text": verified.item.claim,
                       "supported_by": {"source_ids": [source_id], "bar_ids": []}})
        news_claims += 1
    for index, verified in enumerate(news.risks):
        if news_claims >= MAX_NEWS_CLAIMS:
            break
        source_id = f"{coin_key}-risk{index + 1}"
        source_list.append(_source_obj(source_id, verified))
        claims.append({"claim_id": f"C{len(claims) + 1}", "kind": "RISK",
                       "text": verified.item.claim,
                       "supported_by": {"source_ids": [source_id], "bar_ids": []}})
        news_claims += 1
        if verified.item.label:
            known_risks.append(verified.item.label[:120])

    kind = "BOTH" if news.catalyst is not None else "CHART"
    below_pct = (agent_mid - setup.entry) / agent_mid * 100
    stop_pct = (setup.max_entry - setup.stop) / setup.max_entry * 100
    structure = _structure_note(setup, timeframe_word)
    stop_basis = _stop_basis(setup, timeframe_word)
    base_symbol = symbol.split("/")[0]

    thesis = " ".join(filter(None, [
        f"Catalyst (quoted in the sources): {catalyst_claim}" if catalyst_claim else None,
        (f"Chart: {base_symbol} trades {below_pct:.1f}% above {plain(setup.entry)}, the low of "
         f"the {timeframe_word} bar that started {_when(entry_bar.started_at)}. {structure} "
         f"The plan buys at up to {plain(setup.max_entry)}, stops at {plain(setup.stop)} and "
         f"targets {plain(setup.target)} (the {_when(target_bar.started_at)} {timeframe_word} "
         f"high): {setup.reward_risk:.2f}R at the maximum entry."),
    ]))[:1000]

    why_now = " ".join(filter(None, [
        _catalyst_age_wording(news.catalyst.item.published_at, reference=now) + "."
        if news.catalyst is not None and news.catalyst.item.published_at else None,
        (f"Price is {below_pct:.1f}% above a {timeframe_word} low that has held since "
         f"{_when(entry_bar.started_at)}; a pullback of that size fits the report's "
         "validity window."),
        evidence_note,
    ]))[:600]

    why_these_levels = (
        f"Entry {plain(setup.entry)} is the {_when(entry_bar.started_at)} {timeframe_word} low; "
        f"max entry {plain(setup.max_entry)} is 0.15% above it; stop {plain(setup.stop)} is "
        f"{stop_basis}; target {plain(setup.target)} is the window's own high, at the "
        f"{_when(target_bar.started_at)} {timeframe_word} bar — no cited bar is higher. Stop "
        f"distance {stop_pct:.2f}% of max entry (minimum 2%); reward/risk "
        f"{setup.reward_risk:.2f} at max entry."
    )[:600]

    risk_claims = " ".join(verified.item.claim for verified in news.risks)
    risks_text = (
        "A crypto-wide sell-off can take price through the stop; a fast drop can fill the stop "
        "worse than planned; the entry may not fill." + (f" {risk_claims}" if risk_claims else "")
    )[:600]

    invalidation = (
        f"After {_when(now)}: a trade at or below {plain(setup.stop)} before the entry fills, "
        f"or a {timeframe_word} close below {plain(setup.stop)} after it."
    )[:400]

    why_over_peers = " ".join(filter(None, [
        f"Chosen among this run's rule-{setup.rule} {timeframe_word} setups by held-low "
        "proximity to price and reward/risk.",
        lesson_note,
    ]))[:500]
    what_would_change_my_mind = (
        f"A {timeframe_word} close below {plain(setup.stop)}, or a move through "
        f"{plain(setup.entry)} before the entry fills."
    )[:300]
    strong = setup.rule == "A" and setup.reward_risk >= Decimal("2.5")
    confidence_level = "MEDIUM" if strong else "LOW"
    if news.risks:
        confidence_level = "LOW"
    known_risks = (["Thin order book on a crypto pair", "Crypto-wide sell-off risk",
                    "The entry may not fill"] + known_risks)[:5]

    bars_json = [
        {"bar_id": bar_ids[index], "started_at": bar.started_at.isoformat(),
         "open": plain(bar.open), "high": plain(bar.high), "low": plain(bar.low),
         "close": plain(bar.close), "volume": plain(bar.volume)}
        for index, bar in enumerate(setup.bars)
    ]
    ticker_at = _ticker_time(coinbase_ticker) or market_retrieved_at
    technical_evidence = {
        "schema_version": "MUSE_OBSERVED_TECHNICALS_V1",
        "provider": "COINBASE_EXCHANGE_PUBLIC_API",
        "venue": "COINBASE",
        "feed": ("1-hour candles" if setup.timeframe == "1h"
                else (f"1-hour candles aggregated to {timeframe_word} bars"
                      if setup.timeframe != "1d" else "daily candles")),
        "source_url": f"https://api.exchange.coinbase.com/products/{coin}-USD/candles",
        "retrieved_at": max(market_retrieved_at, ticker_at).isoformat(),
        "timeframe_seconds": TIMEFRAMES[setup.timeframe],
        "bars": bars_json,
        "quote": ({"observed_at": ticker_at.isoformat(), "bid": plain(coinbase_ticker["bid"]),
                   "ask": plain(coinbase_ticker["ask"])} if coinbase_ticker.get("bid") else None),
        "level_references": {
            "entry_trigger": {"bar_id": bar_ids[setup.entry_index], "field": "low",
                              "rationale": "Entry at this bar's low."},
            "max_entry_price": {"bar_id": bar_ids[setup.entry_index], "field": "low",
                                "rationale": "0.15% above this bar's low."},
            "stop": {"bar_id": bar_ids[setup.stop_index], "field": "low",
                    "rationale": "0.4% under this bar's low."},
            "target": {"bar_id": bar_ids[setup.target_index], "field": "high",
                      "rationale": "At this bar's high, the window's own high."},
        },
    }
    agent_confidence = ("0.4" if news.risks else
                        "0.6" if news.catalyst is not None else
                        "0.55" if confidence_level == "MEDIUM" else "0.45")
    return {
        "signal_id": signal_id or f"RA{now:%y%m%d}-{coin}",
        "symbol": symbol,
        "kind": kind,
        "agent_current_price": plain(agent_mid),
        "agent_price_at": agent_price_at,
        "levels": {"entry_trigger": plain(setup.entry), "max_entry_price": plain(setup.max_entry),
                   "stop": plain(setup.stop), "target": plain(setup.target)},
        "stated_reward_risk": plain(setup.reward_risk.quantize(Decimal("0.01"), ROUND_DOWN)),
        "reasoning": {"thesis": thesis, "why_now": why_now, "why_these_levels": why_these_levels,
                     "risks": risks_text, "invalidation": invalidation},
        "selection_rationale": {
            "claims": claims, "why_now": why_now, "why_these_levels": why_these_levels[:500],
            "why_over_peers": why_over_peers,
            "what_would_change_my_mind": what_would_change_my_mind,
            "known_risks": known_risks,
            "agent_confidence": {"level": confidence_level,
                                 "basis": (f"{timeframe_word} rule-{setup.rule} structure, "
                                          f"{setup.reward_risk:.2f}R at max entry")[:200]},
        },
        "sources": source_list,
        "technical_evidence": technical_evidence,
        "agent_confidence": agent_confidence,
    }


def _ticker_time(ticker):
    value = (ticker or {}).get("time")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


# --- The whole report: every coin's setup, checked with the app's own models --------------

@dataclass(frozen=True)
class PickOutcome:
    coin: str
    symbol: str
    pick: dict | None  # None when the app's own models rejected it
    dossier_bytes: int | None
    error: str | None


@dataclass(frozen=True)
class BuildResult:
    report: dict | None  # None when zero picks survived
    accepted: tuple  # PickOutcome, in report order
    rejected: tuple  # PickOutcome with .error set (dropped, never submitted)
    skipped: tuple  # {"symbol", "reason"} — coins with no qualifying setup
    notes: tuple  # free-form strings (dropped news items, etc.) for the run summary
    lessons: dict | None = None  # the emphasis applied, for build-notes.json (None: none)
    derivatives: dict | None = None  # what the derivatives context did (None: not given)


def _validate_pick(index, pick, *, now, agent_id, valid_until):
    item = _parse_pick(index, pick)
    coin = pick.get("symbol", "?").split("/")[0]
    if item.pick is None:
        return PickOutcome(coin=coin, symbol=pick.get("symbol", "?"), pick=None,
                           dossier_bytes=None, error=f"{item.code}")
    try:
        dossier = compile_pick_dossier(item, now=now, agent_id=agent_id, valid_until=valid_until)
    except Exception as exc:  # DossierRejected, or a SENSITIVE_EVIDENCE_REJECTED ValueError
        return PickOutcome(coin=coin, symbol=pick["symbol"], pick=None, dossier_bytes=None,
                           error=str(getattr(exc, "code", exc)))
    return PickOutcome(coin=coin, symbol=pick["symbol"], pick=pick,
                       dossier_bytes=dossier.manifest["state_bytes"], error=None)


def _with_derivatives(outcome, summary, *, index, now, agent_id, valid_until, record):
    """``outcome`` with the coin's derivatives context: the first of
    ``derivatives.variants`` (richest first) the app's own models accept, else ``outcome``
    unchanged. The context never costs a pick and never changes which coins are picked, their
    order or their levels. ``record`` collects what happened, for build-notes.json."""
    if summary is None:
        record["missing"].append(outcome.symbol)
        return outcome
    candidates, reasons = derivatives_module.variants(outcome.pick, summary)
    for augmented, info in candidates:
        # The citation check intake makes after the schema (check_pick's
        # CITATION_UNRESOLVED): every claim must cite sources or bars retained in the pick.
        problems = rationale_reference_errors(
            augmented["selection_rationale"],
            source_ids=[source["source_id"] for source in augmented["sources"]],
            bar_ids=[bar["bar_id"] for bar in (augmented.get("technical_evidence") or {})
                     .get("bars") or []],
            prefix=f"picks[{index}].selection_rationale")
        checked = _validate_pick(index, augmented, now=now, agent_id=agent_id,
                                 valid_until=valid_until)
        if not problems and checked.error is None:
            record["attached"].append({"symbol": outcome.symbol, **info,
                                       "without": reasons or None})
            return checked
        reasons.append(f"{checked.error or problems[0]['code']} with "
                       f"{', '.join(info['source_ids'])}")
    record["left_out"].append({"symbol": outcome.symbol,
                               "reason": "; ".join(reasons) + "; the pick is sent without "
                                         "the derivatives context"})
    return outcome


def run_slot_for(schedule, now):
    """``(run_slot, valid_until_limit)`` (RFC3339 strings, or ``None``) for a report or
    outlook generated at ``now``.

    The run it answers is the latest scheduled run at or before ``now``, or the next one once
    ``now`` is within the schedule's grace before it: a run's report may be prepared that
    early (``RUN_SLOT_IN_FUTURE`` only before that), and one sent at 07:15 for the 08:00 run
    must answer 08:00, not yesterday's run, whose validity ends an hour after 08:00. Computed
    with the app's own ``ResearchSchedule`` from the context's schedule definition at ``now``;
    a context without the definition gives its own ``current_run_slot`` and limit."""
    schedule = schedule or {}
    if not (schedule.get("timezone") and schedule.get("runs")):
        return schedule.get("current_run_slot"), schedule.get("current_run_valid_until_limit")
    definition = ResearchSchedule(schedule["timezone"], tuple(schedule["runs"]),
                                  schedule.get("grace_minutes", 60), schedule.get("daily"))
    upcoming = definition.next_after(now)
    slot = upcoming if now >= upcoming - definition.grace else definition.latest_at_or_before(now)
    return (definition.local(slot).isoformat(),
            definition.local(definition.validity_limit(slot)).isoformat())


def _valid_until(now, limit_text):
    candidate = now + DEFAULT_VALIDITY
    if not limit_text:
        return candidate
    try:
        limit = datetime.fromisoformat(limit_text)
    except ValueError:
        return candidate
    return min(candidate, limit)


def _agent_price_at(alpaca_quote):
    value = (alpaca_quote or {}).get("quote_at")
    if not value:
        raise BuildError("NO_QUOTE_TIMESTAMP_FOR_AGENT_PRICE_AT")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def skip_reason(tried, profile=levels.DEFAULT_PROFILE):
    """The report's ``skipped`` reason for a coin with no setup, naming the windows and
    timeframes ``profile`` tried (``levels.json`` records the profile per coin)."""
    if not tried:
        return "No completed Coinbase bar series available for this coin."
    return ("No qualifying pullback setup on any tried rule/timeframe/window "
            f"({profile.windows_text} bars, {profile.timeframes_text}). "
            f"Closest miss: {tried[-1]}")[:200]


def check_agent(agent_id, agent_version):
    """The report's ``agent`` block against the app's own patterns
    (``catalyst_lab.agent_identity``), before anything is built: the intake refuses the whole
    report otherwise (422 ``INVALID_MUSE_REPORT``, ``STRING_PATTERN_MISMATCH``), which the
    per-pick checks cannot catch."""
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise BuildError(f"AGENT_ID_INVALID: the agent ID must match {AGENT_ID.pattern}")
    if not isinstance(agent_version, str) or not AGENT_VERSION.fullmatch(agent_version):
        raise BuildError("AGENT_VERSION_INVALID: the agent version must match "
                         f"{AGENT_VERSION.pattern} (at most 32 characters)")


def build_report(*, context, market_data, levels_by_coin, news_by_coin=None, agent_id,
                 agent_version, run_id=None, now=None, verify_client=None, max_picks=None,
                 session=False, emphasis=None, profile=levels.DEFAULT_PROFILE,
                 derivatives=None, first_coins=(), evidence=None):
    """Every coin's level setup plus verified news, checked and assembled into one
    ``AGENT_RESEARCH_REPORT_V3``. Requires a real (non-offline) research context with a
    configured schedule: the report needs its ``run_slot``, which the offline fallback
    does not carry. Raises ``BuildError`` for that unless ``session=True`` (module
    docstring): the report is still built and every pick still checked, but
    ``run_slot``/``context_as_of``/``valid_until`` are omitted from the envelope for the
    session harness's own ``submit`` to fill. A coin whose pick the app's own models
    reject is dropped (see ``.rejected``), never silently included, in either mode.

    ``emphasis`` (``lessons.load_emphasis``' hints) only reorders: the pick limit is applied
    in this function's own order first, so the same coins are picked with or without it;
    the picks are then ranked by the hints, and each pick a hint ranked early says so in its
    ``why_over_peers``. If that wording would make the app's models refuse a pick that
    passes without it, the pick is kept without it and a note says so: a lesson never costs
    a coin.

    ``profile`` is the research profile ``levels_by_coin`` was found with (one of
    ``levels.PROFILES``); only the skipped coins' reasons depend on it (the timeframes and
    windows they name).

    ``derivatives`` (``derivatives.load``'s ``{coin: Summary}``, CLI ``build --derivatives``)
    adds each pick's derivatives context after the pick passed on its own, and keeps it only
    when the app's models accept the result: the picks, their order and their levels are the
    same with or without it (``_with_derivatives``). ``first_coins`` (update mode's adjusted
    picks) are built before the usual order; empty, the order is unchanged. ``evidence``
    (``EvidenceFilter``) leaves out, with its reason and before the pick limit, a coin the
    operator excluded or whose entry sits too far under the live mid; never a first coin.
    """
    check_agent(agent_id, agent_version)
    if context_module.is_offline(context) and not session:
        raise BuildError(
            "REAL_RESEARCH_CONTEXT_REQUIRED: build needs the app's own research context "
            "(schedule, report_format), not the offline development fallback; pass "
            "session=True (CLI: build --session) to build for the session harness "
            "instead, which has no research-context route to read one from"
        )
    now = now or datetime.now(UTC)
    run_id = run_id or uuid4()
    market_retrieved_at = datetime.fromisoformat(market_data["retrieved_at"])
    schedule = context.get("schedule")
    run_slot, validity_limit = run_slot_for(schedule, now)
    if not run_slot and not session:
        raise BuildError("RESEARCH_CONTEXT_HAS_NO_SCHEDULE: cannot set the report's run_slot")
    report_format = context.get("report_format") or {}
    valid_until = _valid_until(now, validity_limit)

    news_by_coin = news_by_coin or {}
    # The server refuses the WHOLE report over MAX_PICKS (30) picks (ReportEnvelopeV3),
    # so a run that qualifies more coins than that must still submit something: default
    # to the real schema limit (never unlimited) and, when trimming is needed, keep the
    # rule-A and higher reward:risk setups first (research3/builder3.py's own ordering),
    # not an arbitrary alphabetical prefix.
    effective_max_picks = MAX_PICKS if max_picks is None else max_picks
    accepted, skipped, rejected, notes, ranked = [], [], [], [], {}
    derivative_record = ({"attached": [], "left_out": [], "missing": []}
                         if derivatives is not None else None)
    with_setup = sorted(
        ((coin, setup) for coin, (setup, _tried) in levels_by_coin.items() if setup is not None),
        key=lambda pair: (pair[0] not in first_coins, pair[1].rule != "A",
                          -pair[1].reward_risk, pair[0]),
    )
    for coin, (setup, tried) in sorted(levels_by_coin.items()):
        if setup is None:
            skipped.append({"symbol": f"{coin}/USD", "reason": skip_reason(tried, profile)})

    # The guidelines: a coin held open is decided by its own review and is not picked again
    # (the app declines such a pick, ACTIVE_SYMBOL_ALREADY_MANAGED, and its place is lost).
    open_symbols = context_module.open_trade_symbols(context)
    # Method v8: the market's two-hour state, once per build, for the sell-off gate and the
    # evidence sentence; None when no evidence filter is given.
    state = evidence_module.market_state(market_data) if evidence is not None else None
    selloff = (evidence is not None and evidence.selloff is not None
               and evidence_module.in_selloff(state, evidence.selloff))
    for coin, setup in with_setup:
        symbol = f"{coin}/USD"
        if symbol in open_symbols:
            skipped.append({"symbol": symbol, "reason": OPEN_TRADE_SKIP_REASON})
            continue
        filtered = evidence is not None and coin not in first_coins
        if filtered and selloff:
            skipped.append({"symbol": symbol, "reason": evidence.selloff_reason(state)})
            continue
        if filtered and coin in evidence.exclude:
            skipped.append({"symbol": symbol, "reason": EXCLUDED_SKIP_REASON})
            continue
        if len(accepted) >= effective_max_picks:
            skipped.append({"symbol": symbol, "reason": "Report already at its pick limit."})
            continue
        alpaca_quote = context_module.coin_quote(context, symbol)
        agent_mid = context_module.mid_price(alpaca_quote) if alpaca_quote else None
        # Both checked together: a well-formed context never has one without the other
        # (research_context.py's own quote_at is null exactly when bid/ask are), but a
        # malformed or hand-built context must be skipped here, never crash the whole
        # build over one coin (_agent_price_at below assumes quote_at is present).
        if agent_mid is None or not (alpaca_quote or {}).get("quote_at"):
            skipped.append({"symbol": symbol,
                            "reason": "No live Alpaca quote (or its timestamp) for this "
                                     "coin in the context."})
            continue
        if filtered and evidence.max_entry_distance is not None:
            distance = (Decimal(str(agent_mid)) - setup.max_entry) / Decimal(str(agent_mid))
            if distance > evidence.max_entry_distance:
                skipped.append({"symbol": symbol, "reason": evidence.distance_reason(distance)})
                continue
        raw_coin = (market_data.get("coinbase") or {}).get(coin) or {}
        evidence_note = None
        if evidence is not None:
            # The facts for every pick's text; the v8 checks for new coins only.
            volume = evidence_module.alpaca_volume_usd(context, symbol)
            fills, _last_fill, window = evidence_module.fill_history(context, symbol)
            trend = evidence_module.trend_vs_average(raw_coin, agent_mid,
                                                     retrieved_at=market_retrieved_at)
            if (filtered and evidence.min_alpaca_volume_usd is not None and fills == 0
                    and (volume is None or volume < evidence.min_alpaca_volume_usd)):
                skipped.append({"symbol": symbol,
                                "reason": evidence.volume_reason(volume, window)})
                continue
            if filtered and evidence.trend_floor is not None and (
                    trend is None or trend < evidence.trend_floor):
                skipped.append({"symbol": symbol, "reason": evidence.trend_reason(trend)})
                continue
            evidence_note = evidence_module.sentence(trend=trend, volume=volume, fills=fills,
                                                     window=window, state=state)
        news_items = news_by_coin.get(coin) or []
        verified = []
        if news_items:
            verified, dropped = verify_news(news_items, client=verify_client, now=now)
            notes.extend(f"{coin}: dropped news item ({d['reason']}): {d['claim']}"
                        for d in dropped)
        coin_news = select_coin_news(verified, reference=_agent_price_at(alpaca_quote))
        attributes = lessons.pick_attributes(
            mid=agent_mid, setup=setup, kind="BOTH" if coin_news.catalyst is not None else "CHART")
        promoted = lessons.promoted_by(attributes, emphasis or ())

        def assembled(note, coin=coin, setup=setup, agent_mid=agent_mid,
                      alpaca_quote=alpaca_quote, raw_coin=raw_coin, coin_news=coin_news,
                      symbol=symbol, evidence_note=evidence_note):
            pick = build_pick(coin=coin, symbol=symbol, setup=setup, agent_mid=agent_mid,
                              agent_price_at=alpaca_quote["quote_at"],
                              coinbase_ticker=raw_coin.get("ticker") or {},
                              market_retrieved_at=market_retrieved_at, news=coin_news, now=now,
                              lesson_note=note, evidence_note=evidence_note)
            return _validate_pick(len(accepted) + len(rejected), pick, now=now,
                                  agent_id=agent_id, valid_until=valid_until)

        outcome = assembled(lessons.lesson_sentence(promoted))
        if outcome.error and promoted:
            without = assembled(None)
            if without.error is None:
                notes.append(f"{coin}: the lesson wording was left out of why_over_peers "
                             f"({outcome.error} with it); the pick is unchanged otherwise.")
                outcome, promoted = without, []
        if outcome.error:
            rejected.append(outcome)
            notes.append(f"{coin}: dropped by the app's own validator ({outcome.error}).")
            continue
        if derivatives is not None:
            outcome = _with_derivatives(outcome, derivatives.get(coin),
                                        index=len(accepted) + len(rejected), now=now,
                                        agent_id=agent_id, valid_until=valid_until,
                                        record=derivative_record)
        accepted.append(outcome)
        # An update's adjusted picks (``first_coins``) stay first under any emphasis (package
        # learning-loop2: ``update --lessons`` orders only the rest).
        ranked[outcome.symbol] = ((coin not in first_coins,
                                   *lessons.rank_key(attributes, emphasis or ())), len(ranked),
                                  [hint["dimension"] for hint in promoted])

    applied = None
    if emphasis:
        order_before = [outcome.symbol for outcome in accepted]
        accepted.sort(key=lambda outcome: ranked[outcome.symbol][:2])
        applied = {"hints": list(emphasis), "order_before": order_before,
                   "order_after": [outcome.symbol for outcome in accepted],
                   "promoted": {symbol: dims for symbol, (_k, _i, dims) in ranked.items()
                                if dims}}
    if not accepted:
        return BuildResult(report=None, accepted=(), rejected=tuple(rejected),
                           skipped=tuple(skipped), notes=tuple(notes), lessons=applied,
                           derivatives=derivative_record)
    report = {
        "schema_version": REPORT_SCHEMA_V3,
        "report_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "agent": {"agent_id": agent_id, "agent_version": agent_version,
                 # Without a context's report format (session mode) the kit declares the
                 # guidelines the live context serves: V6 (V5 plus the learning loop).
                 "guidelines_version": report_format.get("guidelines_version")
                 or GUIDELINES_V6_VERSION,
                 "guidelines_sha256": report_format.get("guidelines_sha256")
                 or GUIDELINES_V6_SHA256,
                 "run_id": str(run_id)},
        "picks": [outcome.pick for outcome in accepted],
        "skipped": skipped,
    }
    if schedule:
        # A real, configured schedule: include the real envelope fields, exactly as
        # before. Without one (only reachable at all when session=True), these three
        # keys are left OUT of the dict entirely -- not set to null, which
        # dict.setdefault would treat as already present -- for the session harness's
        # own `submit` (scripts/agent_research_session.py's run_submit) to fill from
        # its actual configured schedule at real submission time.
        report["run_slot"] = run_slot
        report["context_as_of"] = context.get("as_of")
        report["valid_until"] = valid_until.isoformat()
    return BuildResult(report=report, accepted=tuple(accepted), rejected=tuple(rejected),
                       skipped=tuple(skipped), notes=tuple(notes), lessons=applied,
                       derivatives=derivative_record)


def validate_report(report, *, now=None, agent_id=None, default_valid_until=None):
    """Re-checks an already-built ``report.json`` against the app's own models: every
    pick through ``_parse_pick``/``compile_pick_dossier`` (as ``build_report`` does while
    building), for a hand-edited or previously saved report. Returns the same per-pick
    shape as ``build_report`` without needing the context or market data again.

    ``report["valid_until"]`` is required unless ``default_valid_until`` is given (an
    aware ``datetime``): a session-mode report deliberately omits it (see the module
    docstring), and its picks still need *some* ``valid_until`` to check the dossier
    budget against, even though the real one is only decided when the session harness's
    own ``submit`` fills the envelope.
    """
    now = now or datetime.now(UTC)
    agent = report.get("agent") or {}
    check_agent(agent.get("agent_id"), agent.get("agent_version"))
    agent_id = agent_id or agent.get("agent_id")
    raw_valid_until = report.get("valid_until")
    if raw_valid_until:
        try:
            valid_until = datetime.fromisoformat(raw_valid_until)
        except ValueError as exc:
            raise BuildError(f"REPORT_INVALID_VALID_UNTIL: {exc}") from None
    elif default_valid_until is not None:
        valid_until = default_valid_until
    else:
        raise BuildError(
            "REPORT_MISSING_VALID_UNTIL: pass default_valid_until for a session-only "
            "report with no envelope valid_until"
        )
    outcomes = [
        _validate_pick(index, pick, now=now, agent_id=agent_id, valid_until=valid_until)
        for index, pick in enumerate(report.get("picks") or [])
    ]
    accepted = tuple(outcome for outcome in outcomes if outcome.error is None)
    rejected = tuple(outcome for outcome in outcomes if outcome.error is not None)
    return BuildResult(report=report if accepted else None, accepted=accepted,
                       rejected=rejected, skipped=tuple(report.get("skipped") or ()), notes=())
