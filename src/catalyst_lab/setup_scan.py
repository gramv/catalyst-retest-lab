"""Deterministic, non-authorizing technical/news research screening.

This module never calls a broker or a model. A contender is research input, not a
trigger, an admission, or an order. The US CATALYST_RETEST_V1 trigger is unchanged.
The named engineering profile is a separate, explicit research policy for local
paper tests; its thresholds are not claims of predictive performance.

Geometry uses confirmed observed pivots: T is the latest swing low, S the most
recent earlier lower swing low, and P the nearest observed swing high above M.
M is T plus the explicitly configured entry allowance, rounded down to the asset
price increment. A farther target is never substituted merely to manufacture 2R.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse

D = Decimal


@dataclass(frozen=True)
class CompletedBar:
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    provider: str
    feed: str
    source_id: str
    completed: bool


@dataclass(frozen=True)
class QuoteSnapshot:
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    provider: str
    feed: str
    source_id: str
    healthy: bool


@dataclass(frozen=True)
class NewsEvidence:
    source_id: str
    url: str
    excerpt: str
    published_at: datetime
    retrieved_at: datetime
    content_hash: str
    primary_source: bool
    asset_relevant: bool
    novelty: str  # NEW_FACT / PREVIOUSLY_KNOWN / UNVERIFIED; research annotation
    stance: str  # SUPPORTS / ADVERSE / NEUTRAL / WITHDRAWN; research annotation


@dataclass(frozen=True)
class ScanAsset:
    asset_id: str
    symbol: str
    market: str  # US / CRYPTO / INDIA
    bars: tuple[CompletedBar, ...]
    quote: QuoteSnapshot | None
    news: tuple[NewsEvidence, ...]
    tradable: bool
    price_increment: Decimal
    quantity_increment: Decimal
    minimum_order_size: Decimal
    session_open: datetime | None
    session_close: datetime | None


@dataclass(frozen=True)
class MarketPolicy:
    market: str
    session_rule: str  # EXCHANGE_CALENDAR / CONTINUOUS
    quantity_rule: str  # WHOLE_SHARES / VENUE_INCREMENT
    execution_scope: str  # PAPER_CANDIDATE / RESEARCH_ONLY
    max_spread_bps: Decimal
    max_quote_age_seconds: int
    min_window_dollar_volume: Decimal
    flatten_minutes: int


@dataclass(frozen=True)
class ScanPolicy:
    policy_id: str
    markets: tuple[MarketPolicy, ...]
    bar_seconds: int
    lookback_bars: int
    max_bar_age_seconds: int
    pivot_width: int
    fast_sma_bars: int
    slow_sma_bars: int
    atr_bars: int
    volume_recent_bars: int
    min_relative_volume: Decimal
    max_support_distance_bps: Decimal
    max_entry_allowance_bps: Decimal
    min_reward_risk: Decimal
    max_news_age_seconds: int
    max_news_retrieval_age_seconds: int
    max_excerpt_characters: int
    shortlist_target_min: int
    shortlist_limit: int

    def __post_init__(self):
        integers = (
            self.bar_seconds, self.lookback_bars, self.max_bar_age_seconds,
            self.pivot_width, self.fast_sma_bars, self.slow_sma_bars, self.atr_bars,
            self.volume_recent_bars, self.max_news_age_seconds,
            self.max_news_retrieval_age_seconds, self.max_excerpt_characters,
            self.shortlist_target_min, self.shortlist_limit,
        )
        if any(type(n) is not int or n <= 0 for n in integers):
            raise ValueError("SCAN_POLICY_POSITIVE_INTEGER_REQUIRED")
        if not self.policy_id or not self.markets:
            raise ValueError("SCAN_POLICY_ID_AND_MARKETS_REQUIRED")
        if len({p.market for p in self.markets}) != len(self.markets):
            raise ValueError("SCAN_POLICY_DUPLICATE_MARKET")
        if not (20 <= self.shortlist_target_min <= self.shortlist_limit <= 30):
            raise ValueError("SCAN_SHORTLIST_BOUNDS_REQUIRED")
        if not (self.fast_sma_bars < self.slow_sma_bars <= self.lookback_bars):
            raise ValueError("SCAN_SMA_WINDOWS_INVALID")
        if not (self.atr_bars < self.lookback_bars
                and self.volume_recent_bars < self.lookback_bars
                and self.pivot_width * 2 + 3 <= self.lookback_bars):
            raise ValueError("SCAN_LOOKBACK_TOO_SMALL")
        if any(not _number(v) or v < 0 for v in (
            self.min_relative_volume, self.max_support_distance_bps,
            self.max_entry_allowance_bps, self.min_reward_risk,
        )) or self.min_reward_risk < 2:
            raise ValueError("SCAN_NUMERIC_POLICY_INVALID")
        for p in self.markets:
            if p.market not in {"US", "CRYPTO", "INDIA"}:
                raise ValueError("SCAN_MARKET_UNSUPPORTED")
            if p.quantity_rule not in {"WHOLE_SHARES", "VENUE_INCREMENT"}:
                raise ValueError("SCAN_QUANTITY_POLICY_INVALID")
            if p.execution_scope not in {"PAPER_CANDIDATE", "RESEARCH_ONLY"}:
                raise ValueError("SCAN_EXECUTION_SCOPE_INVALID")
            if (not _number(p.max_spread_bps) or p.max_spread_bps <= 0
                    or not _number(p.min_window_dollar_volume)
                    or p.min_window_dollar_volume <= 0
                    or type(p.max_quote_age_seconds) is not int or p.max_quote_age_seconds <= 0
                    or type(p.flatten_minutes) is not int or p.flatten_minutes < 0):
                raise ValueError("SCAN_MARKET_POLICY_INVALID")
            if p.market == "CRYPTO":
                if p.session_rule != "CONTINUOUS" or p.flatten_minutes != 0:
                    raise ValueError("CRYPTO_HAS_NO_STOCK_SESSION")
            elif p.session_rule != "EXCHANGE_CALENDAR":
                raise ValueError("STOCK_CALENDAR_REQUIRED")
            if p.market == "US" and (
                p.quantity_rule != "WHOLE_SHARES" or p.flatten_minutes != 5
                or p.max_spread_bps > 10 or p.max_quote_age_seconds > 5
            ):
                raise ValueError("FROZEN_US_BOUNDARY_CHANGED")
            if p.market == "INDIA" and p.execution_scope != "RESEARCH_ONLY":
                raise ValueError("INDIA_RESEARCH_ONLY")


def engineering_scan_policy() -> ScanPolicy:
    """Explicit local-test knobs; calling code must opt in by constructing this profile.

    60 completed one-minute bars; 5/20 SMA uptrend, 14-bar ATR, recent five-bar
    volume at least its preceding 55-bar mean, support within 2.5%, fresh original
    news within 48h. Ranking is RVOL, observed reward/risk, dollar volume, number
    of distinct fresh primary sources, narrower spread, then stable asset id.
    """
    return ScanPolicy(
        policy_id="MUSE_TECH_NEWS_SCAN_ENGINEERING_V1",
        markets=(
            MarketPolicy("US", "EXCHANGE_CALENDAR", "WHOLE_SHARES", "PAPER_CANDIDATE",
                         D("10"), 5, D("1000000"), 5),
            MarketPolicy("CRYPTO", "CONTINUOUS", "VENUE_INCREMENT", "PAPER_CANDIDATE",
                         D("10"), 5, D("1000000"), 0),
            MarketPolicy("INDIA", "EXCHANGE_CALENDAR", "WHOLE_SHARES", "RESEARCH_ONLY",
                         D("10"), 5, D("1000000"), 5),
        ),
        bar_seconds=60, lookback_bars=60, max_bar_age_seconds=90, pivot_width=2,
        fast_sma_bars=5, slow_sma_bars=20, atr_bars=14, volume_recent_bars=5,
        min_relative_volume=D("1"), max_support_distance_bps=D("250"),
        max_entry_allowance_bps=D("15"), min_reward_risk=D("2"),
        max_news_age_seconds=172800, max_news_retrieval_age_seconds=3600,
        max_excerpt_characters=1200, shortlist_target_min=20, shortlist_limit=30,
    )


@dataclass(frozen=True)
class SetupGeometry:
    entry_trigger: Decimal
    max_entry_price: Decimal
    stop: Decimal
    target: Decimal
    reward_risk: Decimal
    trigger_bar_id: str
    stop_bar_id: str
    target_bar_id: str


@dataclass(frozen=True)
class ScanDecision:
    asset_id: str
    symbol: str
    market: str
    disposition: str
    reasons: tuple[str, ...]
    policy_id: str
    execution_scope: str
    geometry: SetupGeometry | None
    metrics: dict[str, Decimal]
    source_ids: tuple[str, ...]
    evidence_hash: str
    rank: int | None


@dataclass(frozen=True)
class ScanBatch:
    policy_id: str
    generated_at: datetime
    decisions: tuple[ScanDecision, ...]
    contenders: tuple[ScanDecision, ...]
    target_minimum_met: bool
    research_only: bool

    def to_dict(self) -> dict:
        return _json(asdict(self))


def _number(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _aware(value: Any) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    return value


def _evidence_hash(asset: ScanAsset) -> str:
    import json

    return sha256(json.dumps(_json(asdict(asset)), sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class SetupScanner:
    def __init__(self, policy: ScanPolicy):
        self.policy = policy

    def scan(self, assets: tuple[ScanAsset, ...] | list[ScanAsset], now: datetime) -> ScanBatch:
        if not _aware(now):
            raise ValueError("SCAN_TIMEZONE_REQUIRED")
        decisions = []
        seen = set()
        for asset in assets:
            if (asset.market, asset.asset_id) in seen:
                decisions.append(self._decision(asset, ("DUPLICATE_ASSET",), None, {}))
                continue
            seen.add((asset.market, asset.asset_id))
            decisions.append(self._scan_asset(asset, now))
        eligible = sorted((d for d in decisions if d.disposition == "CONTENDER"), key=lambda d: (
            -d.metrics["relative_volume"], -d.metrics["reward_risk"],
            -d.metrics["window_dollar_volume"], -d.metrics["fresh_primary_sources"],
            d.metrics["spread_bps"], d.market, d.asset_id,
        ))
        ranks = {(d.market, d.asset_id): i for i, d in enumerate(eligible, 1)}
        ranked = []
        for d in decisions:
            if d.disposition == "CONTENDER":
                rank = ranks[(d.market, d.asset_id)]
                d = replace(d, rank=rank)
                if rank > self.policy.shortlist_limit:
                    d = replace(d, disposition="RANKED_OUT", reasons=("SHORTLIST_CAP",))
            ranked.append(d)
        contenders = tuple(sorted((d for d in ranked if d.disposition == "CONTENDER"),
                                  key=lambda d: d.rank))
        return ScanBatch(self.policy.policy_id, now, tuple(ranked), contenders,
                         len(contenders) >= self.policy.shortlist_target_min, True)

    def _decision(self, asset, reasons, geometry, metrics):
        policy = next((p for p in self.policy.markets if p.market == asset.market), None)
        evidence_codes = {"NEWS_EVIDENCE_REQUIRED", "NO_FRESH_PRIMARY_CATALYST",
                          "NEWS_PROVENANCE_INVALID", "NEWS_COVERAGE_UNCERTAIN"}
        disposition = "CONTENDER" if not reasons else (
            "NEEDS_EVIDENCE" if set(reasons) <= evidence_codes else "REJECTED"
        )
        return ScanDecision(
            asset.asset_id, asset.symbol, asset.market, disposition, tuple(dict.fromkeys(reasons)),
            self.policy.policy_id, policy.execution_scope if policy else "RESEARCH_ONLY",
            geometry, metrics,
            tuple(b.source_id for b in asset.bars) + tuple(n.source_id for n in asset.news)
            + ((asset.quote.source_id,) if asset.quote else ()),
            _evidence_hash(asset), None,
        )

    def _scan_asset(self, asset: ScanAsset, now: datetime) -> ScanDecision:
        policy = next((p for p in self.policy.markets if p.market == asset.market), None)
        if policy is None:
            return self._decision(asset, ("UNSUPPORTED_MARKET",), None, {})
        reasons = []
        metrics = {}
        if not asset.asset_id or not asset.symbol:
            reasons.append("ASSET_IDENTITY_REQUIRED")
        if not asset.tradable:
            reasons.append("ASSET_NOT_TRADABLE")
        if any(not _number(n) or n <= 0 for n in (
            asset.price_increment, asset.quantity_increment, asset.minimum_order_size,
        )):
            reasons.append("INVALID_VENUE_INCREMENTS")
        elif policy.quantity_rule == "WHOLE_SHARES" and (
            asset.quantity_increment != 1 or asset.minimum_order_size != 1
        ):
            reasons.append("WHOLE_SHARES_REQUIRED")
        if policy.session_rule == "EXCHANGE_CALENDAR":
            if (not _aware(asset.session_open) or not _aware(asset.session_close)
                    or asset.session_open >= asset.session_close):
                reasons.append("EXCHANGE_CALENDAR_REQUIRED")
            elif not asset.session_open <= now < (
                asset.session_close - timedelta(minutes=policy.flatten_minutes)
            ):
                reasons.append("OUTSIDE_ENTRY_SESSION")
        quote = asset.quote
        if quote is None:
            reasons.append("QUOTE_REQUIRED")
        elif (not _aware(quote.timestamp) or not _number(quote.bid) or not _number(quote.ask)
              or quote.bid <= 0 or quote.ask < quote.bid):
            reasons.append("INVALID_QUOTE")
        else:
            age = D(str((now - quote.timestamp).total_seconds()))
            metrics["quote_age_seconds"] = age
            if age < 0 or age > policy.max_quote_age_seconds:
                reasons.append("STALE_OR_FUTURE_QUOTE")
            spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000
            metrics["spread_bps"] = spread
            if spread > policy.max_spread_bps:
                reasons.append("MAX_SPREAD")
            if not quote.healthy:
                reasons.append("DATA_FEED_FAILURE")
            if not quote.provider or not quote.feed or not quote.source_id:
                reasons.append("QUOTE_PROVENANCE_REQUIRED")
        bars = asset.bars[-self.policy.lookback_bars:]
        bar_errors = self._bar_errors(bars, now)
        reasons.extend(bar_errors)
        news_errors, fresh_sources = self._news_errors(asset.news, now)
        reasons.extend(news_errors)
        metrics["fresh_primary_sources"] = D(fresh_sources)
        if bar_errors:
            return self._decision(asset, reasons, None, metrics)
        volumes = [b.volume for b in bars]
        recent = self.policy.volume_recent_bars
        baseline = sum(volumes[:-recent]) / len(volumes[:-recent])
        rvol = (sum(volumes[-recent:]) / recent) / baseline if baseline else D("0")
        dollar_volume = sum(b.close * b.volume for b in bars)
        fast = sum(b.close for b in bars[-self.policy.fast_sma_bars:]) / self.policy.fast_sma_bars
        slow = sum(b.close for b in bars[-self.policy.slow_sma_bars:]) / self.policy.slow_sma_bars
        atrs = [max(b.high - b.low, abs(b.high - prior.close), abs(b.low - prior.close))
                for prior, b in zip(bars[:-1], bars[1:], strict=True)]
        atr = sum(atrs[-self.policy.atr_bars:]) / self.policy.atr_bars
        metrics.update(relative_volume=rvol, window_dollar_volume=dollar_volume,
                       fast_sma=fast, slow_sma=slow, atr=atr)
        if baseline == 0 or rvol < self.policy.min_relative_volume:
            reasons.append("INSUFFICIENT_RELATIVE_VOLUME")
        if dollar_volume < policy.min_window_dollar_volume:
            reasons.append("INSUFFICIENT_DOLLAR_LIQUIDITY")
        if fast <= slow or bars[-1].close < fast:
            reasons.append("LONG_TREND_NOT_SUPPORTED")
        geometry = None
        if "INVALID_VENUE_INCREMENTS" not in reasons:
            geometry, geometry_errors = self._geometry(bars, asset.price_increment)
            reasons.extend(geometry_errors)
        if geometry:
            metrics["reward_risk"] = geometry.reward_risk
            if geometry.reward_risk < self.policy.min_reward_risk:
                reasons.append("MIN_REWARD_RISK_AT_MAX_ENTRY")
            if quote and _number(quote.ask) and quote.ask > 0:
                distance = (quote.ask - geometry.entry_trigger) / geometry.entry_trigger * 10000
                metrics["support_distance_bps"] = distance
                if distance < 0:
                    reasons.append("SUPPORT_ALREADY_LOST")
                elif distance > self.policy.max_support_distance_bps:
                    reasons.append("TOO_FAR_FROM_OBSERVED_SUPPORT")
        return self._decision(asset, reasons, geometry, metrics)

    def _bar_errors(self, bars, now):
        errors = []
        if len(bars) != self.policy.lookback_bars:
            return ["INSUFFICIENT_COMPLETED_BARS"]
        for index, b in enumerate(bars):
            if not b.completed or not _aware(b.start_at) or not _aware(b.end_at):
                errors.append("UNCONFIRMED_BAR")
                continue
            if b.end_at > now or b.end_at <= b.start_at:
                errors.append("INCOMPLETE_OR_FUTURE_BAR")
            if (b.end_at - b.start_at).total_seconds() != self.policy.bar_seconds:
                errors.append("BAR_INTERVAL_MISMATCH")
            if index and b.start_at != bars[index - 1].end_at:
                errors.append("BAR_GAP_OR_ORDER_INVALID")
            if any(not _number(v) or v <= 0 for v in (b.open, b.high, b.low, b.close)):
                errors.append("INVALID_OHLCV")
            elif (not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
                  or not _number(b.volume) or b.volume < 0):
                errors.append("INVALID_OHLCV")
            if not b.provider or not b.feed or not b.source_id:
                errors.append("BAR_PROVENANCE_REQUIRED")
        if len({b.source_id for b in bars}) != len(bars):
            errors.append("DUPLICATE_BAR_SOURCE")
        if len({(b.provider, b.feed) for b in bars}) != 1:
            errors.append("MIXED_BAR_FEEDS")
        if _aware(bars[-1].end_at):
            age = (now - bars[-1].end_at).total_seconds()
            if age < 0 or age > self.policy.max_bar_age_seconds:
                errors.append("STALE_COMPLETED_BARS")
        return errors

    def _news_errors(self, news, now):
        if not news:
            return ["NEWS_EVIDENCE_REQUIRED"], 0
        errors = []
        fresh = set()
        for n in news:
            url = urlparse(n.url)
            if (not n.source_id or url.scheme != "https" or not url.netloc
                    or url.username or url.password or not n.excerpt
                    or len(n.excerpt) > self.policy.max_excerpt_characters
                    or n.content_hash != sha256(n.excerpt.encode()).hexdigest()
                    or not _aware(n.published_at) or not _aware(n.retrieved_at)
                    or n.published_at > n.retrieved_at or n.retrieved_at > now):
                errors.append("NEWS_PROVENANCE_INVALID")
                continue
            if n.novelty not in {"NEW_FACT", "PREVIOUSLY_KNOWN", "UNVERIFIED"} or (
                n.stance not in {"SUPPORTS", "ADVERSE", "NEUTRAL", "WITHDRAWN"}
            ):
                errors.append("NEWS_PROVENANCE_INVALID")
                continue
            if not n.asset_relevant:
                continue
            if n.stance == "WITHDRAWN":
                errors.append("SOURCE_WITHDRAWN")
            age = (now - n.published_at).total_seconds()
            retrieval_age = (now - n.retrieved_at).total_seconds()
            if age > self.policy.max_news_age_seconds:
                continue
            if retrieval_age > self.policy.max_news_retrieval_age_seconds:
                errors.append("NEWS_COVERAGE_UNCERTAIN")
                continue
            if n.primary_source and n.novelty == "NEW_FACT" and n.stance == "SUPPORTS":
                fresh.add(n.url)
            if n.stance == "ADVERSE":
                errors.append("MATERIAL_ADVERSE_NEWS")
        if not fresh:
            errors.append("NO_FRESH_PRIMARY_CATALYST")
        return errors, len(fresh)

    def _geometry(self, bars, tick):
        width = self.policy.pivot_width
        lows = []
        highs = []
        for i in range(width, len(bars) - width):
            neighbours = bars[i - width:i] + bars[i + 1:i + width + 1]
            if all(bars[i].low < b.low for b in neighbours):
                lows.append(i)
            if all(bars[i].high > b.high for b in neighbours):
                highs.append(i)
        if not lows:
            return None, ["OBSERVED_SUPPORT_REQUIRED"]
        t_index = lows[-1]
        trigger = bars[t_index].low
        previous_lower = [i for i in lows[:-1] if bars[i].low < trigger]
        if not previous_lower:
            return None, ["OBSERVED_STOP_STRUCTURE_REQUIRED"]
        s_index = previous_lower[-1]
        stop = bars[s_index].low
        maximum = (trigger * (1 + self.policy.max_entry_allowance_bps / 10000) / tick
                   ).to_integral_value(rounding=ROUND_FLOOR) * tick
        targets = [i for i in highs if bars[i].high > maximum]
        if not targets:
            return None, ["OBSERVED_TARGET_REQUIRED"]
        p_index = min(targets, key=lambda i: (bars[i].high, i))
        target = bars[p_index].high
        if any(value % tick != 0 for value in (trigger, stop, target)):
            return None, ["OBSERVED_PRICE_OFF_VENUE_INCREMENT"]
        if not stop < trigger <= maximum < target:
            return None, ["OBSERVED_GEOMETRY_INVALID"]
        return SetupGeometry(trigger, maximum, stop, target, (target - maximum) / (maximum - stop),
                             bars[t_index].source_id, bars[s_index].source_id,
                             bars[p_index].source_id), []
