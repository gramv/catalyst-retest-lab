"""GET-only Alpaca observations, plus a standalone legacy research utility.

Credentials are supplied by the caller, never loaded here. US feed is explicit
(IEX is not consolidated); crypto is fixed to Alpaca's US data venue. India has
no Alpaca collection path. News is supplied as retained Muse source evidence.

Reference mechanics checked against official documentation on 2026-09-19:
https://docs.alpaca.markets/us/reference/stockbars
https://docs.alpaca.markets/us/reference/cryptobars-1
https://docs.alpaca.markets/us/reference/get-v2-assets-1
https://docs.alpaca.markets/us/docs/crypto-trading
https://docs.alpaca.markets/us/docs/orders-at-alpaca
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import httpx

from catalyst_lab.alpaca import DATA_ENDPOINT, AlpacaCredentials
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.market import NY, MarketDataError, Session, decimal, timestamp
from catalyst_lab.setup_scan import (
    CompletedBar,
    NewsEvidence,
    QuoteSnapshot,
    ScanAsset,
    ScanBatch,
    ScanPolicy,
    SetupScanner,
    _json,
)

D = Decimal
STOCK_PATHS = {"bars": "/v2/stocks/bars", "quotes": "/v2/stocks/quotes/latest",
               "trades": "/v2/stocks/trades/latest"}
CRYPTO_PATHS = {"bars": "/v1beta3/crypto/us/bars",
                "quotes": "/v1beta3/crypto/us/latest/quotes",
                "trades": "/v1beta3/crypto/us/latest/trades"}


class ScanSourceError(Exception):
    """Sanitized machine reason; no provider body, headers, or exception repr."""


class ScanReadOnlyTransport(httpx.BaseTransport):
    def __init__(self, delegate: httpx.BaseTransport):
        self.delegate = delegate

    def handle_request(self, request):
        url = request.url
        paths = {
            "paper-api.alpaca.markets": {"/v2/assets", "/v2/calendar"},
            "data.alpaca.markets": set(STOCK_PATHS.values()) | set(CRYPTO_PATHS.values()),
        }
        if (request.method != "GET" or url.scheme != "https"
                or url.port not in {None, 443} or url.userinfo
                or url.path not in paths.get(url.host, set())):
            raise ScanSourceError("SCAN_GET_ENDPOINT_NOT_ALLOWED")
        if not re.fullmatch(r"PK[A-Z0-9]{8,62}", request.headers.get("APCA-API-KEY-ID", "")):
            raise ScanSourceError("SCAN_PAPER_CREDENTIAL_REQUIRED")
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


@dataclass(frozen=True)
class SourcePolicy:
    stock_feed: str
    timeout_seconds: int
    batch_size: int
    page_size: int
    max_pages: int
    lookback_padding_bars: int

    def __post_init__(self):
        if self.stock_feed not in {"iex", "sip"}:
            raise ValueError("SCAN_EXPLICIT_STOCK_FEED_REQUIRED")
        if any(type(v) is not int or v <= 0 for v in (
            self.timeout_seconds, self.batch_size, self.page_size, self.max_pages,
            self.lookback_padding_bars,
        )) or self.batch_size > 200 or self.page_size > 10000:
            raise ValueError("SCAN_SOURCE_POLICY_INVALID")


@dataclass(frozen=True)
class BarContextWindow:
    """Position context request size; contains no candidate-selection rules."""

    lookback_bars: int
    bar_seconds: int

    def __post_init__(self):
        if type(self.lookback_bars) is not int or not 1 <= self.lookback_bars <= 1000 or (
            type(self.bar_seconds) is not int or self.bar_seconds != 60
        ):
            raise ValueError("EXPLICIT_BAR_CONTEXT_WINDOW_REQUIRED")


@dataclass(frozen=True)
class SimpleWindow:
    """A bar count for ``timeframe_bars`` (no padding beyond the source policy's)."""

    lookback_bars: int


@dataclass(frozen=True)
class ScanUniverse:
    stock_symbols: tuple[str, ...]
    include_crypto: bool
    crypto_symbols: tuple[str, ...] | None  # None discovers all active USD pairs.

    def __post_init__(self):
        if type(self.include_crypto) is not bool:
            raise ValueError("SCAN_CRYPTO_UNIVERSE_FLAG_REQUIRED")
        if self.crypto_symbols and not self.include_crypto:
            raise ValueError("SCAN_CRYPTO_UNIVERSE_DISABLED")
        if len(set(self.stock_symbols)) != len(self.stock_symbols) or (
            self.crypto_symbols is not None
            and len(set(self.crypto_symbols)) != len(self.crypto_symbols)
        ):
            raise ValueError("SCAN_DUPLICATE_UNIVERSE_SYMBOL")


@dataclass(frozen=True)
class LatestPrintedTrade:
    symbol: str
    trade_id: str
    price: Decimal
    size: Decimal
    timestamp: datetime
    provider: str
    feed: str
    source_id: str


@dataclass(frozen=True)
class SourceIssue:
    market: str
    symbol: str | None
    code: str
    route: str


@dataclass(frozen=True)
class UniverseExclusion:
    market: str
    symbol: str
    asset_id: str
    reason: str


@dataclass(frozen=True)
class SourceReceipt:
    route: str
    parameters: dict
    requested_at: datetime
    received_at: datetime
    http_status: int | None
    response_hash: str | None


@dataclass(frozen=True)
class SourcedScan:
    batch: ScanBatch
    assets: tuple[ScanAsset, ...]
    latest_trades: tuple[LatestPrintedTrade, ...]
    source_issues: tuple[SourceIssue, ...]
    universe_exclusions: tuple[UniverseExclusion, ...]
    receipts: tuple[SourceReceipt, ...]
    started_at: datetime
    completed_at: datetime
    universe_complete: bool
    stock_feed_limitation: str

    def to_dict(self):
        return _json(asdict(self))


class AlpacaMarketSource:
    """Read-only observations/metadata for supplied symbols; no discovery scanner."""

    def __init__(self, credentials: AlpacaCredentials, source_policy: SourcePolicy,
                 clock: Callable[[], datetime], *, transport=None):
        if not isinstance(credentials, AlpacaCredentials):
            raise ValueError("SUPPLIED_PAPER_CREDENTIALS_REQUIRED")
        self.policy, self.clock = source_policy, clock
        self._client = httpx.Client(
            headers={"APCA-API-KEY-ID": credentials.key_id,
                     "APCA-API-SECRET-KEY": credentials.secret},
            timeout=source_policy.timeout_seconds, follow_redirects=False, trust_env=False,
            transport=ScanReadOnlyTransport(transport or httpx.HTTPTransport()),
        )
        self._receipts: list[SourceReceipt] = []

    def close(self):
        self._client.close()

    def _get(self, route: str, params: dict):
        base = PAPER_ENDPOINT if route in {"/v2/assets", "/v2/calendar"} else DATA_ENDPOINT
        before = self.clock()
        try:
            response = self._client.get(base + route, params=params)
        except httpx.HTTPError:
            self._receipts.append(
                SourceReceipt(route, dict(params), before, self.clock(), None, None)
            )
            raise ScanSourceError("SCAN_CONNECTION_ERROR") from None
        self._receipts.append(SourceReceipt(
            route, dict(params), before, self.clock(), response.status_code,
            sha256(response.content).hexdigest(),
        ))
        if response.status_code != 200:
            raise ScanSourceError(f"SCAN_HTTP_{response.status_code}")
        try:
            return json.loads(response.content, parse_float=D,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise ScanSourceError("SCAN_INVALID_JSON") from None

    def completed_bars(self, market: str, symbol: str, scan_policy: ScanPolicy | BarContextWindow):
        """One asset's completed bar context for a position review; read-only."""
        if market not in {"US", "CRYPTO"} or scan_policy.bar_seconds != 60:
            raise ValueError("UNSUPPORTED_POSITION_BAR_SOURCE")
        now = self.clock()
        self._receipts = []
        session = None
        if market == "US":
            session, errors = self._session(now)
            if errors:
                return (), tuple(errors)
        bars, errors = self._bars(market, (symbol,), scan_policy, now, session)
        return tuple(bars.get(symbol, ())), tuple(errors)

    # CRYPTO_MAINTENANCE_V1 (package maintenance): the completed 15-minute and 1-hour bars a
    # maintenance review reads. The same GET-only route and pagination as the one-minute bars.
    # CRYPTO_MAINTENANCE_V2 (package answer-rules) also reads the last 60 completed 1-minute
    # bars (context V5), through the same route.
    TIMEFRAMES = {"15Min": 900, "1Hour": 3600, "1Min": 60}

    def timeframe_bars(self, market: str, symbol: str, *, timeframe: str, count: int):
        """The last ``count`` completed ``timeframe`` bars of one crypto pair; read-only."""
        seconds = self.TIMEFRAMES.get(timeframe)
        if market != "CRYPTO" or seconds is None or type(count) is not int or not 1 <= count <= 500:
            raise ValueError("UNSUPPORTED_TIMEFRAME_BAR_SOURCE")
        self._receipts = []
        window = SimpleWindow(count)
        bars, errors = self._bars(market, (symbol,), window, self.clock(), None,
                                  timeframe=timeframe, seconds=seconds)
        return tuple(bars.get(symbol, ())), tuple(errors)

    def window_bars(self, market: str, symbol: str, *, start: datetime, end: datetime):
        """Completed one-minute bars of one crypto pair that start in ``[start, end)``; read-only.

        CRYPTO_GAP_RESUME_V1 (package gap-resume): the bars of a gap's unobserved window. The
        same GET-only route and pagination as every other bar read; ``start`` and ``end`` are
        aware minute boundaries and a bar that had not closed by ``end`` is never returned. Any
        request, page or row failure is reported as a ``SourceIssue`` (bars incomplete).
        """
        if (market != "CRYPTO" or not isinstance(start, datetime)
                or not isinstance(end, datetime) or start.tzinfo is None or end.tzinfo is None
                or not start < end or start.second or start.microsecond or end.second
                or end.microsecond):
            raise ValueError("UNSUPPORTED_WINDOW_BAR_SOURCE")
        self._receipts = []
        bars, errors = self._bars(market, (symbol,), SimpleWindow(1), end, None, start=start)
        return tuple(bars.get(symbol, ())), tuple(errors)

    def current_observations(self, universe: ScanUniverse):
        """Lightweight research/worker snapshots; no history, account or order request.

        Explicit symbols only. A missing quote/trade returns no usable observation
        and a durable-recordable SourceIssue; the caller must not reuse stale cache.
        Returned print timestamps/IDs must still pass execution-time freshness checks.
        """
        if universe.include_crypto and universe.crypto_symbols is None:
            raise ValueError("EXPLICIT_OBSERVER_SYMBOLS_REQUIRED")
        self._receipts = []
        observations, issues = {}, []
        markets = [("US", universe.stock_symbols)]
        if universe.include_crypto:
            markets.append(("CRYPTO", universe.crypto_symbols or ()))
        for market, symbols in markets:
            for offset in range(0, len(symbols), self.policy.batch_size):
                batch = symbols[offset:offset + self.policy.batch_size]
                quotes, q_errors = self._latest(market, batch, "quotes")
                trades, t_errors = self._latest(market, batch, "trades")
                issues.extend(q_errors + t_errors)
                for symbol in batch:
                    quote, trade = quotes.get(symbol), trades.get(symbol)
                    if quote is None or trade is None:
                        continue
                    observations[(market, symbol)] = {
                        "trade_price": str(trade.price), "trade_at": trade.timestamp.isoformat(),
                        "trade_id": trade.trade_id, "quote_at": quote.timestamp.isoformat(),
                        "bid": str(quote.bid), "ask": str(quote.ask), "feed_healthy": True,
                        "data_provider": "ALPACA", "data_feed": quote.feed,
                        "source_ids": [quote.source_id, trade.source_id],
                        "retrieved_at": self.clock().isoformat(),
                    }
        return observations, tuple(issues)

    def _metadata(self, market):
        asset_class = "us_equity" if market == "US" else "crypto"
        try:
            rows = self._get("/v2/assets", {"asset_class": asset_class, "status": "active"})
            if not isinstance(rows, list):
                raise ScanSourceError("INVALID_ASSET_LIST")
            result = {}
            for row in rows:
                if (not isinstance(row, dict) or row.get("class") != asset_class
                        or not isinstance(row.get("symbol"), str) or not row.get("id")):
                    raise ScanSourceError("INVALID_ASSET_METADATA")
                if row["symbol"] in result:
                    raise ScanSourceError("DUPLICATE_ASSET_METADATA")
                result[row["symbol"]] = row
            return result, []
        except ScanSourceError as exc:
            return {}, [SourceIssue(market, None, str(exc), "/v2/assets")]

    def _session(self, now):
        day = now.astimezone(NY).date()
        route = "/v2/calendar"
        try:
            rows = self._get(route, {"start": day.isoformat(), "end": day.isoformat()})
            if not isinstance(rows, list) or len(rows) > 1:
                raise ScanSourceError("INVALID_SCAN_CALENDAR")
            if not rows:
                return None, [SourceIssue("US", None, "EXCHANGE_CLOSED", route)]
            session = Session.from_calendar(rows[0])
            if session.session_date != day:
                raise ScanSourceError("INVALID_SCAN_CALENDAR")
            return session, []
        except ScanSourceError as exc:
            return None, [SourceIssue("US", None, str(exc), route)]
        except Exception:
            return None, [SourceIssue("US", None, "INVALID_SCAN_CALENDAR", route)]

    def _bars(self, market, symbols, scan_policy, now, session, *, timeframe="1Min", seconds=60,
              start=None):
        route = (STOCK_PATHS if market == "US" else CRYPTO_PATHS)["bars"]
        count = scan_policy.lookback_bars + self.policy.lookback_padding_bars
        # An explicit ``start`` (``window_bars``) is kept as given; ``now`` is then its end.
        if start is None and seconds == 60:
            start = now.replace(second=0, microsecond=0) - timedelta(minutes=count)
        elif start is None:  # UTC-aligned boundaries of the longer timeframe.
            elapsed = int(now.timestamp())
            boundary = datetime.fromtimestamp(elapsed - elapsed % seconds, tz=now.tzinfo)
            start = boundary - timedelta(seconds=seconds * count)
        if session:
            start = max(start, session.opens)
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start.isoformat(),
                  "end": now.isoformat(), "limit": self.policy.page_size, "sort": "asc"}
        feed = self.policy.stock_feed if market == "US" else "CRYPTO_US"
        if market == "US":
            params.update(feed=self.policy.stock_feed, adjustment="raw")
        result, seen_tokens, issues = {s: [] for s in symbols}, set(), []
        for _ in range(self.policy.max_pages):
            try:
                page = self._get(route, params)
                if not isinstance(page, dict) or not isinstance(page.get("bars"), dict):
                    raise ScanSourceError("INVALID_BAR_RESPONSE")
                if set(page["bars"]) - set(symbols):
                    raise ScanSourceError("UNEXPECTED_BAR_SYMBOL")
                for symbol, rows in page["bars"].items():
                    if not isinstance(rows, list):
                        raise ScanSourceError("INVALID_BAR_RESPONSE")
                    for row in rows:
                        try:
                            at = timestamp(row["t"])
                            end = at + timedelta(seconds=seconds)
                            if end > now:
                                continue  # A current unclosed bar is not technical evidence.
                            if at < start or (session and not session.opens <= at < session.closes):
                                continue
                            bar = CompletedBar(
                                at, end, *(decimal(row[k]) for k in ("o", "h", "l", "c")),
                                decimal(row["v"], positive=False), "ALPACA", feed,
                                f"ALPACA:{feed}:{symbol}:bar:{at.isoformat()}", True,
                            )
                            result[symbol].append(bar)
                        except Exception:
                            issues.append(SourceIssue(market, symbol, "INVALID_BAR_ROW", route))
                token = page.get("next_page_token")
                if not token:
                    return result, issues
                if not isinstance(token, str) or token in seen_tokens:
                    raise ScanSourceError("BAR_PAGINATION_CYCLE")
                seen_tokens.add(token)
                params["page_token"] = token
            except ScanSourceError as exc:
                return result, issues + [SourceIssue(market, s, str(exc), route) for s in symbols]
        return result, issues + [
            SourceIssue(market, s, "BAR_PAGINATION_LIMIT", route) for s in symbols
        ]

    def _latest(self, market, symbols, kind):
        route = (STOCK_PATHS if market == "US" else CRYPTO_PATHS)[kind]
        params = {"symbols": ",".join(symbols)}
        feed = self.policy.stock_feed if market == "US" else "CRYPTO_US"
        if market == "US":
            params["feed"] = self.policy.stock_feed
        try:
            data = self._get(route, params)
            if not isinstance(data, dict) or not isinstance(data.get(kind), dict):
                raise ScanSourceError(f"INVALID_{kind.upper()}_RESPONSE")
            if set(data[kind]) - set(symbols):
                raise ScanSourceError(f"UNEXPECTED_{kind.upper()}_SYMBOL")
            result, issues = {}, []
            for symbol in symbols:
                try:
                    row = data[kind][symbol]
                    at = timestamp(row["t"])
                    source_id = f"ALPACA:{feed}:{symbol}:{kind}:{at.isoformat()}"
                    if kind == "quotes":
                        result[symbol] = QuoteSnapshot(decimal(row["bp"]), decimal(row["ap"]),
                                                      at, "ALPACA", feed, source_id, True)
                    else:
                        result[symbol] = LatestPrintedTrade(
                            symbol, str(row["i"]), decimal(row["p"]),
                            decimal(row["s"], positive=False),
                            at, "ALPACA", feed, source_id,
                        )
                except Exception:
                    issues.append(SourceIssue(market, symbol, f"MISSING_OR_INVALID_{kind.upper()}",
                                              route))
            return result, issues
        except ScanSourceError as exc:
            return {}, [SourceIssue(market, s, str(exc), route) for s in symbols]


class AlpacaScanSource(AlpacaMarketSource):
    """Standalone research utility; never instantiated by the application runtime."""

    def collect_and_scan(
        self, scan_policy: ScanPolicy, universe: ScanUniverse,
        news_by_asset: Mapping[tuple[str, str], tuple[NewsEvidence, ...]],
    ) -> SourcedScan:
        if scan_policy.bar_seconds != 60:
            raise ValueError("SCAN_SOURCE_REQUIRES_ONE_MINUTE_POLICY")
        self._receipts = []
        started = self.clock()
        if started.tzinfo is None:
            raise ValueError("SCAN_SOURCE_TIMEZONE_REQUIRED")
        issues, excluded, assets, trades = [], [], [], []
        universe_complete = True
        markets = []
        if universe.stock_symbols:
            markets.append(("US", universe.stock_symbols))
        if universe.include_crypto:
            markets.append(("CRYPTO", universe.crypto_symbols))
        for market, requested in markets:
            metadata, metadata_issues = self._metadata(market)
            issues.extend(metadata_issues)
            if metadata_issues:
                universe_complete = False
            if requested is None:
                requested = []
                for symbol, row in sorted(metadata.items()):
                    if not symbol.endswith("/USD"):
                        reason = "NON_USD_PAIR_OUTSIDE_SCAN_SCOPE"
                    elif row.get("status") != "active" or row.get("tradable") is not True:
                        reason = "ASSET_NOT_TRADABLE"
                    else:
                        requested.append(symbol)
                        continue
                    excluded.append(UniverseExclusion(market, symbol, str(row.get("id", "")),
                                                        reason))
                requested = tuple(requested)
            valid_symbols = []
            for symbol in requested:
                pattern = r"[A-Z][A-Z0-9.-]{0,14}" if market == "US" else (
                    r"[A-Z0-9]{1,16}/USD"
                )
                if not isinstance(symbol, str) or not re.fullmatch(pattern, symbol):
                    issues.append(SourceIssue(market, symbol, "INVALID_SCAN_SYMBOL", "/v2/assets"))
                else:
                    valid_symbols.append(symbol)
                if symbol not in metadata:
                    issues.append(SourceIssue(market, symbol, "ASSET_METADATA_UNAVAILABLE",
                                              "/v2/assets"))
            session = None
            if market == "US":
                session, calendar_issues = self._session(started)
                issues.extend(calendar_issues)
            bars_by_symbol, quotes_by_symbol, trades_by_symbol = {}, {}, {}
            for offset in range(0, len(valid_symbols), self.policy.batch_size):
                symbols = tuple(valid_symbols[offset:offset + self.policy.batch_size])
                bars, bar_issues = self._bars(market, symbols, scan_policy, started, session)
                bars_by_symbol.update(bars)
                issues.extend(bar_issues)
            # Quotes are fetched after slow historical pagination, then evaluated at completion.
            for offset in range(0, len(valid_symbols), self.policy.batch_size):
                symbols = tuple(valid_symbols[offset:offset + self.policy.batch_size])
                quotes, quote_issues = self._latest(market, symbols, "quotes")
                quotes_by_symbol.update(quotes)
                issues.extend(quote_issues)
                latest, trade_issues = self._latest(market, symbols, "trades")
                trades_by_symbol.update(latest)
                issues.extend(trade_issues)
            for symbol in requested:
                row = metadata.get(symbol, {})
                quote = quotes_by_symbol.get(symbol)
                bar_rows = bars_by_symbol.get(symbol, ())
                if market == "CRYPTO":
                    try:
                        increments = tuple(decimal(row[k]) for k in (
                            "price_increment", "min_trade_increment", "min_order_size",
                        ))
                    except (KeyError, ValueError, MarketDataError):
                        increments = (D("0"), D("0"), D("0"))
                        issues.append(SourceIssue(market, symbol, "CRYPTO_PRECISION_UNAVAILABLE",
                                                  "/v2/assets"))
                else:
                    # Broker's published US limit/stop precision, not a made-up asset field.
                    # A window crossing $1 uses the conservative cent increment throughout.
                    observed = [b.high for b in bar_rows] + ([quote.ask] if quote else [])
                    increments = ((D("0.01") if observed and max(observed) >= 1
                                   else D("0.0001")), D("1"), D("1"))
                assets.append(ScanAsset(
                    str(row.get("id") or f"unresolved:{market}:{symbol}"), symbol, market,
                    tuple(bar_rows), quote, tuple(news_by_asset.get((market, symbol), ())),
                    row.get("tradable") is True and row.get("status") == "active",
                    *increments, session.opens if session else None,
                    session.closes if session else None,
                ))
                if symbol in trades_by_symbol:
                    trades.append(trades_by_symbol[symbol])
        completed = self.clock()
        batch = SetupScanner(scan_policy).scan(assets, completed)
        # Collector failures cannot be hidden by enough rows from an earlier successful page.
        decisions = []
        for decision in batch.decisions:
            failures = [i.code for i in issues if i.market == decision.market
                        and i.symbol in {None, decision.symbol}]
            if failures:
                decision = replace(decision, disposition="REJECTED", rank=None,
                                   reasons=tuple(dict.fromkeys((*decision.reasons, *failures))))
            decisions.append(decision)
        # Refill the bounded shortlist if a previously ranked item had a source failure.
        eligible = sorted((d for d in decisions if d.disposition in {"CONTENDER", "RANKED_OUT"}),
                          key=lambda d: d.rank)
        ranks = {(d.market, d.asset_id): i for i, d in enumerate(eligible, 1)}
        reranked = []
        for d in decisions:
            if (d.market, d.asset_id) in ranks:
                rank = ranks[(d.market, d.asset_id)]
                inside = rank <= scan_policy.shortlist_limit
                d = replace(d, rank=rank, disposition="CONTENDER" if inside else "RANKED_OUT",
                            reasons=() if inside else ("SHORTLIST_CAP",))
            reranked.append(d)
        decisions = reranked
        contenders = tuple(sorted((d for d in decisions if d.disposition == "CONTENDER"),
                                  key=lambda d: d.rank))
        batch = replace(batch, decisions=tuple(decisions), contenders=contenders,
                        target_minimum_met=len(contenders) >= scan_policy.shortlist_target_min)
        return SourcedScan(
            batch, tuple(assets), tuple(trades), tuple(issues), tuple(excluded),
            tuple(self._receipts), started, completed, universe_complete,
            "IEX_ONLY_NOT_CONSOLIDATED" if self.policy.stock_feed == "iex" else "SIP_CONSOLIDATED",
        )
