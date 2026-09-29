"""``GET /api/v1/lab/research-context``: what a research agent reads before a scheduled run.

Owner decisions of 2026-09-26: crypto only; trades execute on Alpaca Paper, so picks must be
coins Alpaca can trade (active, tradable USD pairs, stablecoins excluded; PAXG kept); one
research run a day at 08:00 New York time; the agent may research the whole market but picks
from this list. The context carries, as of one instant:

* the next scheduled runs (``research_schedule``) and the report V3 limits;
* the tradable ``universe``: per coin the latest bid, ask, last trade (price and time), spread
  in bps, Alpaca's price increment, minimum order size and quantity increment, and the volume
  of the last 24 completed hours when available, each with its data-source label;
* the caller's own ``open_trades`` (with the next review time under a review version, and the
  recorded window under the window versions of package review-window), ``pending_reviews``
  (package day-review: the continue-or-exit reviews and Jev exit flags awaiting the caller's
  answer, as ``GET /api/v1/lab/reviews`` lists them) and ``recent_outcomes`` (closed trades of
  the last 7 days, the last run's picks);
* from ``RESEARCH_CONTEXT_V2`` (package learning-app, 2026-09-28) the caller's ``lessons``
  (``lessons.RESEARCH_LESSONS_V1``): its own scorecard lines, graded outlooks, the recent
  movers and its pending post-mortems, read from the ledger; ``null`` for the status credential.

Read-only and GET-only. Asset metadata is one ``GET /v2/assets`` (crypto, active) through the
read-only paper transport and the account's request-budget governor, as a research read,
cached for an hour. Quotes and trades come from the Alpaca market-data source (latest quotes
and latest trades for all symbols, one request each per batch), cached for at most 5 seconds;
24-hour volume is summed from completed 1-hour bars and cached until the next hour. Nothing
here authorizes, sizes or checks a trade, and unavailable asset data fails closed (HTTP 503).
"""

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from catalyst_lab.agent_identity import agent_fields
from catalyst_lab.alpaca import AlpacaReadOnly
from catalyst_lab.broker_budget import RESEARCH, request_priority
from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.lessons import safe_lessons
from catalyst_lab.managed_engineering import ENGINEERING_PURPOSE
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.market import MarketDataError, timestamp
from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_V6_SHA256, MUSE_GUIDELINES_V6_VERSION
from catalyst_lab.repository import json_safe
from catalyst_lab.research_dossier import RATIONALE_BUDGET_BYTES, STATE_BUDGET_BYTES
from catalyst_lab.research_report_v3 import (
    MAX_PICKS,
    MAX_SKIPPED,
    REPORT_SCHEMA_V3,
    TARGET_PICKS,
    ResearchCapabilityUnavailable,
    UniverseSnapshot,
    V3Intake,
)
from catalyst_lab.research_selection_topk import (
    NOT_RANKED,
    RANKED,
    RANKING_KIND,
    REPLACEMENT_KIND,
    REPLACEMENT_PUBLISHED,
    SKIPPED_KIND,
    VETOED,
    ranking_key_for,
)
from catalyst_lab.scan_sources import CRYPTO_PATHS, ScanSourceError
from catalyst_lab.system_check import SUPERSEDED_BY_NEW_RESEARCH

# RESEARCH_CONTEXT_V2 (package learning-app, 2026-09-28): every V1 field unchanged plus
# ``lessons``; V1 was served before this release.
CONTEXT_VERSION = "RESEARCH_CONTEXT_V2"
# Owner list of 2026-09-26: USD-pegged and euro stablecoins are never picks; PAXG (gold) stays.
STABLECOINS = frozenset({"USDC", "USDT", "USDG", "DAI", "PYUSD", "USDP", "TUSD", "FDUSD", "EURC"})
ASSET_SOURCE = "ALPACA_PAPER_V2_ASSETS_CRYPTO_ACTIVE"
QUOTE_SOURCE = "ALPACA_MARKET_DATA_CRYPTO_US_LATEST_QUOTES_AND_TRADES"
VOLUME_SOURCE = "ALPACA_MARKET_DATA_CRYPTO_US_1HOUR_BARS"
VOLUME_METHOD = "SUM_OF_24_COMPLETED_1H_BARS_NOTIONAL_VWAP_ELSE_CLOSE_TIMES_VOLUME"
ASSET_CACHE_SECONDS = 3600
QUOTE_CACHE_SECONDS = 5
VOLUME_RETRY_SECONDS = 60
RECENT_DAYS = 7
MAX_CLOSED_TRADES = 50
R_BASIS = "TEST_R_GROSS_PNL_OVER_RESERVED_PLANNED_RISK"
UNIVERSE_UNAVAILABLE = "RESEARCH_UNIVERSE_UNAVAILABLE"
_USD_PAIR = re.compile(r"([A-Z0-9]{1,16})/USD")
_ASSET_FIELDS = ("symbol", "class", "status", "tradable", "price_increment",
                 "min_order_size", "min_trade_increment")
CENT = Decimal("0.01")
D = Decimal


def _plain(value):
    """Decimals as fixed-point strings (``1E-9`` reads as ``0.000000001``), recursively."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return value.astimezone(UTC)


def _positive(value):
    try:
        number = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def tradable_universe(rows):
    """Alpaca crypto asset rows -> (coins, exclusions); pure.

    A coin is tradable when it is an active, tradable crypto asset quoted in USD (``XXX/USD``)
    whose base is not a stablecoin and whose price increment, minimum order size and quantity
    increment are positive numbers (admission needs them). Coins are sorted by symbol.
    """
    coins, stablecoins, unusable = [], [], []
    non_usd = inactive = malformed = 0
    for row in rows:
        symbol = row.get("symbol") if isinstance(row, dict) else None
        if not isinstance(symbol, str):
            malformed += 1
            continue
        pair = _USD_PAIR.fullmatch(symbol)
        if not pair:
            non_usd += 1
            continue
        if row.get("status") != "active" or row.get("tradable") is not True:
            inactive += 1
            continue
        if row.get("class") != "crypto":
            unusable.append(symbol)
            continue
        if pair[1] in STABLECOINS:
            stablecoins.append(symbol)
            continue
        increments = {key: _positive(row.get(key))
                      for key in ("price_increment", "min_order_size", "min_trade_increment")}
        if None in increments.values():
            unusable.append(symbol)
            continue
        coins.append({"symbol": symbol, "price_increment": increments["price_increment"],
                      "min_order_size": increments["min_order_size"],
                      "quantity_increment": increments["min_trade_increment"]})
    coins.sort(key=lambda coin: coin["symbol"])
    return coins, {
        "stablecoins": sorted(stablecoins),
        "non_usd_quote_count": non_usd,
        "inactive_or_untradable_count": inactive,
        "unusable_metadata": sorted(unusable),
        "malformed_count": malformed,
    }


class CryptoAssetReader(AlpacaReadOnly):
    """``GET /v2/assets?asset_class=crypto&status=active`` through ReadOnlyPaperTransport.

    In the application the delegate transport is the account's request-budget governor
    (``BrokerBudget.transport()``), so the read is counted and, when the budget or a
    Retry-After window refuses it, fails before any I/O. It is declared a research read.
    """

    def __init__(self, credentials, *, transport=None):
        super().__init__(credentials, "iex", transport=transport)

    def crypto_assets(self):
        with request_priority(RESEARCH):
            try:
                response = self._client.get(
                    PAPER_ENDPOINT + "/v2/assets",
                    params={"asset_class": "crypto", "status": "active"},
                )
            except httpx.HTTPError:
                raise MarketDataError("ALPACA_CONNECTION_ERROR") from None
        if response.status_code != 200:
            raise MarketDataError(f"ALPACA_HTTP_{response.status_code}")
        try:
            rows = json.loads(response.content, parse_float=D,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError
        except (ValueError, UnicodeError):
            raise MarketDataError("INVALID_ASSET_LIST") from None
        # Only eligibility and precision fields; never asset IDs or free-text names.
        return [{key: row.get(key) for key in _ASSET_FIELDS} for row in rows]


class ResearchContextService:
    """Builds the research context and gives report-V3 intake its schedule and universe.

    ``asset_reader`` and ``market_source`` are objects or zero-argument factories; a factory
    is called on first use (the app never opens a client it does not need), and whatever it
    created is closed by ``close``. ``repository`` is read-only here.
    """

    def __init__(self, repository, *, clock, schedule=None, asset_reader=None,
                 market_source=None, report_format=None,
                 asset_cache_seconds=ASSET_CACHE_SECONDS, quote_cache_seconds=QUOTE_CACHE_SECONDS,
                 reviews=None):
        if (
            not callable(clock)
            or isinstance(asset_cache_seconds, bool) or isinstance(quote_cache_seconds, bool)
            or not isinstance(asset_cache_seconds, int | float)
            or not isinstance(quote_cache_seconds, int | float)
            or not 60 <= asset_cache_seconds <= ASSET_CACHE_SECONDS
            or not 0 < quote_cache_seconds <= QUOTE_CACHE_SECONDS
        ):
            raise ValueError("RESEARCH_CONTEXT_POLICY_INVALID")
        self.repo, self.clock, self.schedule = repository, clock, schedule
        # trade_review.TradeReviewService (package day-review); None: no pending reviews.
        self.reviews = reviews
        self.report_format = dict(report_format or {})
        self.asset_cache_seconds = asset_cache_seconds
        self.quote_cache_seconds = quote_cache_seconds
        self._sources = {"assets": asset_reader, "market": market_source}
        self._created = []
        self._source_lock = threading.Lock()
        self._asset_lock = threading.Lock()
        self._quote_lock = threading.Lock()
        self._volume_lock = threading.Lock()
        self._assets = self._quotes = self._volumes = None

    # --- Sources ----------------------------------------------------------------------------

    def _source(self, name):
        with self._source_lock:
            value = self._sources[name]
            if value is None:
                raise ResearchCapabilityUnavailable(
                    UNIVERSE_UNAVAILABLE if name == "assets" else "RESEARCH_MARKET_DATA_UNAVAILABLE"
                )
            if callable(value) and not hasattr(value, "close"):
                try:
                    value = value()  # A factory: build once, own it.
                except ResearchCapabilityUnavailable:
                    raise
                except Exception:
                    raise ResearchCapabilityUnavailable(
                        UNIVERSE_UNAVAILABLE if name == "assets"
                        else "RESEARCH_MARKET_DATA_UNAVAILABLE"
                    ) from None
                self._sources[name] = value
                self._created.append(value)
            return value

    def close(self):
        with self._source_lock:
            created, self._created = self._created, []
        for client in created:
            client.close()

    # --- Universe (asset metadata, cached about an hour) --------------------------------------

    def _universe(self):
        now = _utc(self.clock())
        with self._asset_lock:
            cached = self._assets
            if cached is not None and 0 <= (
                now - cached["fetched_at"]
            ).total_seconds() < self.asset_cache_seconds:
                return cached
            try:
                rows = self._source("assets").crypto_assets()
                coins, excluded = tradable_universe(rows)
            except (MarketDataError, ValueError, TypeError, AttributeError):
                raise ResearchCapabilityUnavailable(UNIVERSE_UNAVAILABLE) from None
            if not coins:
                raise ResearchCapabilityUnavailable(UNIVERSE_UNAVAILABLE)
            self._assets = {"fetched_at": now, "coins": coins, "excluded": excluded}
            return self._assets

    def universe_snapshot(self):
        """The current tradable symbols for report-V3 intake (at most one read an hour)."""
        universe = self._universe()
        return UniverseSnapshot(
            frozenset(coin["symbol"] for coin in universe["coins"]),
            universe["fetched_at"], ASSET_SOURCE,
        )

    def intake(self):
        """Report-V3 intake dependencies; the universe is read only for a V3 body."""
        return V3Intake(self.schedule, self.universe_snapshot)

    # --- Quotes and trades (at most 5 seconds old) ------------------------------------------

    def _market(self, symbols):
        now = _utc(self.clock())
        key = tuple(symbols)
        with self._quote_lock:
            cached = self._quotes
            if cached is not None and cached["symbols"] == key and 0 <= (
                now - cached["fetched_at"]
            ).total_seconds() < self.quote_cache_seconds:
                return cached
            source = self._source("market")
            quotes, trades, issues = {}, {}, []
            size = source.policy.batch_size
            for offset in range(0, len(key), size):
                batch = key[offset:offset + size]
                for kind, target in (("quotes", quotes), ("trades", trades)):
                    rows, problems = source._latest("CRYPTO", batch, kind)
                    target.update(rows)
                    issues.extend({"symbol": p.symbol, "code": p.code} for p in problems)
            self._quotes = {"symbols": key, "fetched_at": now, "quotes": quotes,
                            "trades": trades, "issues": issues}
            return self._quotes

    # --- 24-hour volume (completed hourly bars, cached until the next hour) ------------------

    def _volume(self, symbols):
        now = _utc(self.clock())
        key = tuple(symbols)
        end = now.replace(minute=0, second=0, microsecond=0)
        with self._volume_lock:
            cached = self._volumes
            if cached is not None and cached["symbols"] == key and (
                # A good read serves its hour; a failed one is retried after a minute.
                (not cached["failed"] and cached["window_end"] == end)
                or (cached["failed"] and 0 <= (now - cached["fetched_at"]).total_seconds()
                    < VOLUME_RETRY_SECONDS)
            ):
                return cached
            start = end - timedelta(hours=24)
            source = self._source("market")
            totals = {symbol: {"base": D(0), "usd": D(0), "bar_count": 0} for symbol in key}
            failure = None
            try:
                for offset in range(0, len(key), source.policy.batch_size):
                    batch = key[offset:offset + source.policy.batch_size]
                    self._hourly_bars(source, batch, start, end, totals)
            except (ScanSourceError, MarketDataError, ValueError, TypeError, KeyError,
                    InvalidOperation) as exc:
                failure = str(exc) if re.fullmatch(r"[A-Z0-9_]{1,80}", str(exc)) else (
                    "VOLUME_UNAVAILABLE"
                )
            self._volumes = {
                "symbols": key, "fetched_at": now, "window_end": end, "window_start": start,
                "failed": failure is not None, "failure": failure,
                "totals": None if failure else totals,
            }
            return self._volumes

    @staticmethod
    def _hourly_bars(source, batch, start, end, totals):
        route = CRYPTO_PATHS["bars"]
        params = {"symbols": ",".join(batch), "timeframe": "1Hour", "start": start.isoformat(),
                  "end": end.isoformat(), "limit": source.policy.page_size, "sort": "asc"}
        seen = set()
        for _ in range(source.policy.max_pages):
            page = source._get(route, params)
            if not isinstance(page, dict) or not isinstance(page.get("bars"), dict):
                raise ScanSourceError("INVALID_BAR_RESPONSE")
            if set(page["bars"]) - set(batch):
                raise ScanSourceError("UNEXPECTED_BAR_SYMBOL")
            for symbol, rows in page["bars"].items():
                if not isinstance(rows, list):
                    raise ScanSourceError("INVALID_BAR_RESPONSE")
                for row in rows:
                    at = timestamp(row["t"])
                    if not start <= at or at + timedelta(hours=1) > end:
                        continue  # Only completed hours inside the 24-hour window.
                    volume = D(str(row["v"]))
                    price = row.get("vw")
                    price = D(str(price)) if price not in (None, 0) else D(str(row["c"]))
                    if not volume.is_finite() or volume < 0 or not price.is_finite():
                        raise ScanSourceError("INVALID_BAR_ROW")
                    totals[symbol]["base"] += volume
                    totals[symbol]["usd"] += volume * price
                    totals[symbol]["bar_count"] += 1
            token = page.get("next_page_token")
            if not token:
                return
            if not isinstance(token, str) or token in seen:
                raise ScanSourceError("BAR_PAGINATION_CYCLE")
            seen.add(token)
            params["page_token"] = token
        raise ScanSourceError("BAR_PAGINATION_LIMIT")

    # --- The context ---------------------------------------------------------------------------

    def context(self, principal):
        now = _utc(self.clock())
        universe = self._universe()
        symbols = [coin["symbol"] for coin in universe["coins"]]
        market = self._market(symbols)
        volume = self._volume(symbols)
        coins = [self._coin(coin, market, volume) for coin in universe["coins"]]
        agent_id, legacy = _owner(principal)
        issues = list(market["issues"])
        if volume["failed"]:
            issues.append({"symbol": None, "code": "VOLUME_24H_" + volume["failure"]})
        return json_safe(_plain({
            "context_version": CONTEXT_VERSION,
            "as_of": now,
            "caller": {"role": principal.role,
                       "agent_id": principal.agent_id if principal.role == "muse" else None},
            "schedule": self._schedule(now),
            "report_format": {
                "schema_version": REPORT_SCHEMA_V3,
                # V6 = V5 (V4 plus how an agent answers the 24-hour reviews and exit flags)
                # plus the learning loop (muse_guidelines, package learning-app).
                "guidelines_version": MUSE_GUIDELINES_V6_VERSION,
                "guidelines_sha256": MUSE_GUIDELINES_V6_SHA256,
                "picks_target": TARGET_PICKS, "picks_max": MAX_PICKS,
                "skipped_max": MAX_SKIPPED,
                "dossier_budget_bytes": STATE_BUDGET_BYTES,
                "rationale_budget_bytes": RATIONALE_BUDGET_BYTES,
                **self.report_format,
            },
            "universe": {
                "count": len(coins),
                "coins": coins,
                "excluded": universe["excluded"],
                "sources": {
                    "assets": {"label": ASSET_SOURCE, "fetched_at": universe["fetched_at"],
                               "cache_seconds": self.asset_cache_seconds},
                    "quotes_and_trades": {"label": QUOTE_SOURCE,
                                          "fetched_at": market["fetched_at"],
                                          "cache_seconds": self.quote_cache_seconds},
                    "volume_24h": {"label": VOLUME_SOURCE, "method": VOLUME_METHOD,
                                   "window_start": volume["window_start"],
                                   "window_end": volume["window_end"],
                                   "available": not volume["failed"]},
                },
                "issues": issues,
            },
            "open_trades": self._open_trades(agent_id, legacy, market),
            # Package day-review: the caller's pending 24-hour reviews and Jev exit flags.
            "pending_reviews": self.reviews.pending(principal)["items"]
            if self.reviews is not None else [],
            "recent_outcomes": self._recent_outcomes(agent_id, legacy, now),
            # RESEARCH_LESSONS_V1 (package learning-app): the caller's own, never another's.
            "lessons": safe_lessons(self.repo, agent_id, legacy=legacy, now=now)
            if agent_id is not None else None,
            "trade_authorized": False,
        }))

    def _schedule(self, now):
        schedule = self.schedule
        if schedule is None:
            return None
        current = schedule.latest_at_or_before(now)
        return {
            **schedule.as_dict(),
            "current_run_slot": schedule.local(current),
            "current_run_valid_until_limit": schedule.local(schedule.validity_limit(current)),
            "next_runs": [schedule.local(run) for run in schedule.next_runs(now)],
        }

    @staticmethod
    def _coin(coin, market, volume):
        quote, trade = market["quotes"].get(coin["symbol"]), market["trades"].get(coin["symbol"])
        spread = None
        if quote is not None and quote.ask >= quote.bid:
            mid = (quote.ask + quote.bid) / 2
            spread = ((quote.ask - quote.bid) / mid * 10000).quantize(CENT, ROUND_HALF_UP)
        totals = (volume["totals"] or {}).get(coin["symbol"]) if not volume["failed"] else None
        return {
            "symbol": coin["symbol"],
            "bid": quote.bid if quote else None,
            "ask": quote.ask if quote else None,
            "quote_at": quote.timestamp if quote else None,
            "spread_bps": spread,
            "last_trade_price": trade.price if trade else None,
            "last_trade_at": trade.timestamp if trade else None,
            "price_increment": coin["price_increment"],
            "min_order_size": coin["min_order_size"],
            "quantity_increment": coin["quantity_increment"],
            "volume_24h": None if totals is None else {
                "base": totals["base"],
                "usd": totals["usd"].quantize(CENT, ROUND_HALF_UP),
                "completed_hour_bars": totals["bar_count"],
            },
        }

    # --- The caller's own trades and outcomes -----------------------------------------------

    @staticmethod
    def _owned(alias):
        """SQL restricting setups (or research events) to the caller: its agent's records,
        and unattributed legacy records for the legacy credential only."""
        return (f"(({alias}->'agent'->>'agent_id')=%(agent)s OR "
                f"(%(legacy)s AND ({alias}->'agent'->>'agent_id') IS NULL))")

    def _open_trades(self, agent_id, legacy, market):
        if agent_id is None and not legacy:
            return []
        with self.repo.connect() as conn:
            rows = conn.execute(
                f"""SELECT s.setup_id,s.symbol,s.record_json->'levels' AS levels,
                s.record_json->>'signal_id' AS signal_id,t.body AS state,
                (SELECT e.body->>'qty' FROM lab.managed_events e
                 WHERE e.setup_id=s.setup_id AND e.kind='BROKER_POSITION'
                 ORDER BY (e.body->>'occurred_at')::timestamptz DESC NULLS LAST,
                          e.event_seq DESC LIMIT 1) AS broker_qty,
                sum(f.qty) FILTER(WHERE f.side='buy') AS bought,
                sum(f.qty*f.price) FILTER(WHERE f.side='buy') AS buy_notional,
                min(f.filled_at) FILTER(WHERE f.side='buy') AS first_fill
                FROM lab.managed_setups s JOIN lab.managed_fills f USING(setup_id)
                LEFT JOIN lab.managed_states t USING(setup_id)
                WHERE coalesce(t.body->>'state','')<>'CLOSED'
                AND coalesce(s.record_json->>'purpose','')<>%(engineering)s
                AND {self._owned('s.record_json')}
                GROUP BY s.setup_id,t.body ORDER BY s.event_seq""",
                {"agent": agent_id, "legacy": legacy, "engineering": ENGINEERING_PURPOSE},
            ).fetchall()
        trades = []
        for row in rows:
            state = row["state"] or {}
            qty = row["broker_qty"] if row["broker_qty"] is not None else state.get("qty")
            qty = D(str(qty)) if qty is not None else None
            bought = row["bought"] or D(0)
            if qty is None or qty <= 0 or bought <= 0:
                continue
            entry = row["buy_notional"] / bought
            quote = market["quotes"].get(row["symbol"])
            trades.append({
                "setup_id": row["setup_id"],
                "symbol": row["symbol"],
                "signal_id": row["signal_id"],
                "state": state.get("state"),
                "entry": entry,
                "stop": state.get("stop"),
                "target": state.get("target"),
                "quantity": qty,
                "opened_at": state.get("opened_at") or row["first_fill"],
                "levels": row["levels"],
                "unrealized_pnl_usd": (quote.bid - entry) * qty if quote else None,
                "unrealized_pnl_basis": "LATEST_BID_MINUS_AVERAGE_ENTRY" if quote else None,
                # CRYPTO_24H_REVIEW_V1 (package day-review): T of the next review and the
                # continues so far; null for every other trade.
                "review_at": state.get("day_review_at"),
                "continuations": state.get("continuations"),
                # The window versions (package review-window): the window the trade recorded
                # at admission, in seconds (the time between its reviews, or its fixed hold);
                # null under the 24-hour versions and for every other trade.
                "holding_window_seconds": state.get("holding_window_seconds"),
            })
        return trades

    def _recent_outcomes(self, agent_id, legacy, now):
        since = now - timedelta(days=RECENT_DAYS)
        closed, truncated, last_run = [], False, None
        if agent_id is not None or legacy:
            params = {"agent": agent_id, "legacy": legacy, "engineering": ENGINEERING_PURPOSE,
                      "since": since, "limit": MAX_CLOSED_TRADES + 1}
            with self.repo.connect() as conn:
                rows = conn.execute(
                    f"""SELECT s.setup_id,s.symbol,s.record_json->>'signal_id' AS signal_id,
                    t.body AS state,
                    coalesce((t.body->>'closed_at')::timestamptz,t.recorded_at) AS closed_at
                    FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
                    WHERE t.body->>'state'='CLOSED'
                    AND coalesce((t.body->>'closed_at')::timestamptz,t.recorded_at)>=%(since)s
                    AND EXISTS(SELECT 1 FROM lab.managed_fills f
                               WHERE f.setup_id=s.setup_id AND f.side='buy')
                    AND coalesce(s.record_json->>'purpose','')<>%(engineering)s
                    AND {self._owned('s.record_json')}
                    ORDER BY 5 DESC,s.event_seq DESC LIMIT %(limit)s""",
                    params,
                ).fetchall()
                started = conn.execute(
                    f"""SELECT event_seq,body,recorded_at FROM lab.managed_events
                    WHERE kind='RESEARCH_STARTED' AND NOT (body ? 'scan')
                    AND {self._owned('body')} ORDER BY event_seq DESC LIMIT 1""",
                    params,
                ).fetchone()
            truncated = len(rows) > MAX_CLOSED_TRADES
            for row in rows[:MAX_CLOSED_TRADES]:
                closed.append(self._closed_trade(row))
            if started is not None:
                last_run = self._last_run(started, now)
        return {"window_days": RECENT_DAYS, "since": since, "closed_trades": closed,
                "closed_trades_truncated": truncated, "last_run": last_run}

    def _closed_trade(self, row):
        state = row["state"] or {}
        measured = managed_measurement(self.repo, row["setup_id"])
        return {
            "setup_id": row["setup_id"],
            "symbol": row["symbol"],
            "signal_id": row["signal_id"],
            "opened_at": state.get("opened_at"),
            "closed_at": row["closed_at"],
            "exit_reason": state.get("reason") or state.get("exit_requested"),
            "entry": measured.get("entry_fill_average"),
            "exit": measured.get("exit_fill_average"),
            "quantity": measured.get("bought_qty"),
            "gross_pnl_usd": measured.get("gross_realized_pnl"),
            "net_pnl_usd": measured.get("net_pnl"),
            "fees_verified": measured.get("fees_verified"),
            "r": measured.get("test_r"),
            "r_basis": R_BASIS if measured.get("test_r") is not None else None,
        }

    def _last_run(self, started, now):
        """The caller's latest cycle, each pick with its true status (package replacement).

        ``status``: REJECTED_AT_INTAKE; else, once admitted, its setup's state; else, once
        published, its ``selection_status`` (SELECTED, DECLINED, REPLACED_BY, SUPERSEDED or
        EXPIRED); else, under top-K once ranked, SKIPPED, NOT_SELECTED, VETOED or NOT_RANKED;
        else the latest review disposition; else AWAITING_REVIEW. ``selection_status`` is
        ADMITTED for an admitted pick. A top-K run also shows each pick's Jev rank and ranking
        status, its replacement chain and the ranking's counts and K.
        """
        body = started["body"]
        cycle_id = body["cycle_id"]
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT event_seq,kind,body,idempotency_key FROM lab.managed_events
                WHERE body->>'cycle_id'=%s AND kind LIKE 'RESEARCH_%%' ORDER BY event_seq""",
                (cycle_id,),
            ).fetchall()
            setups = conn.execute(
                """SELECT s.symbol,t.body AS state FROM lab.managed_setups s
                LEFT JOIN lab.managed_states t USING(setup_id) WHERE s.cycle_id=%s
                ORDER BY s.event_seq""",
                (cycle_id,),
            ).fetchall()
        report = body.get("report") or {}
        submitted = report.get("picks") or report.get("items") or []
        packets, decisions, selected = {}, {}, {}
        declines, replacements, skipped, ranking = {}, {}, {}, None
        for row in rows:
            event, kind = row["body"], row["kind"]
            if kind == "RESEARCH_PACKET":
                packets[event["item_key"]] = event
            elif kind == "RESEARCH_DECISION":
                decisions[(event["item_key"], event["revision"])] = event
            elif kind == "RESEARCH_SELECTED":
                selected[event["packet"]["item_key"]] = (row["event_seq"], event["packet"])
            elif kind == "RESEARCH_ADMISSION_DECLINED":
                declines[event.get("selection_event_seq")] = event
            elif kind == REPLACEMENT_KIND:
                replacements[event.get("declined_item_key")] = event
            elif kind == SKIPPED_KIND:
                skipped[event.get("item_key")] = event
            elif kind == RANKING_KIND and row["idempotency_key"] == ranking_key_for(cycle_id):
                ranking = event  # Only the event under the ranking key is the ranking.
        entries = {e["item_key"]: e for e in (ranking or {}).get("entries") or []}
        by_symbol = {row["symbol"]: row["state"] or {} for row in setups}
        picks = []
        for result in body.get("item_results") or []:
            raw = submitted[result["index"]] if result["index"] < len(submitted) else {}
            symbol = raw.get("symbol") if isinstance(raw, dict) else None
            packet = packets.get(result.get("item_key"))
            if packet is not None:
                symbol = packet["symbol"]
            item_key = packet["item_key"] if packet else None
            decision = decisions.get((packet["item_key"], packet["revision"])) if packet else None
            setup = by_symbol.get(symbol) if packet else None
            seq, selection = selected.get(item_key, (None, None))
            decline = declines.get(seq) if selection is not None else None
            replacement = replacements.get(item_key) if decline is not None else None
            entry, skip = entries.get(item_key), skipped.get(item_key)
            selection_status = self._selection_status(selection, setup, decline, replacement,
                                                      now)
            if result["status"] != "ACCEPTED":
                status = "REJECTED_AT_INTAKE"
            elif setup is not None:
                status = setup.get("state")
            elif selection_status is not None:
                status = selection_status
            elif skip is not None:
                status = "SKIPPED"
            elif entry is not None:
                status = "NOT_SELECTED" if entry.get("status") == RANKED else entry.get("status")
            elif decision is not None:
                status = decision["disposition"]
            else:
                status = "AWAITING_REVIEW"
            picks.append({
                "index": result["index"],
                "signal_id": result.get("signal_id"),
                "symbol": symbol,
                "status": status,
                "intake": result["status"],
                "intake_code": result.get("code"),
                "review": decision["disposition"] if decision else None,
                "review_reason": decision["reason"] if decision else None,
                "selected": selection is not None,
                "selection_status": selection_status,
                "decline_code": decline.get("reason") if decline else None,
                "replaced_by": replacement.get("replacement_item_key") if replacement else None,
                "replacement_outcome": replacement.get("outcome") if replacement else None,
                "replacement_for": selection.get("replacement_for") if selection else None,
                "jev_rank": entry.get("rank") if entry else None,
                "ranking_status": entry.get("status") if entry else None,
                "ranking_reasons": self._ranking_reasons(entry),
                "skip_reason": skip.get("reason") if skip else None,
                "setup_state": setup.get("state") if setup else None,
                "setup_reason": (setup.get("reason") or setup.get("exit_requested")
                                 or setup.get("revocation_reason")) if setup else None,
                "expires_at": packet["expires_at"] if packet else None,
            })
        return {
            "cycle_id": cycle_id,
            "report_schema_version": body.get("report_schema_version"),
            "run_slot": body.get("run_slot"),
            "received_at": started["recorded_at"],
            "expires_at": body.get("expires_at"),
            "submitted_count": body.get("submitted_count"),
            "contender_count": body.get("contender_count"),
            "rejected_count": body.get("rejected_count"),
            **agent_fields(body.get("agent")),
            "selection_policy": body.get("selection_policy"),
            # The top-K ranking's summary (null before the ranking and for other rules).
            "ranking": None if ranking is None else {
                "policy": ranking.get("policy"),
                "k": ranking.get("k"),
                "counts": ranking.get("counts"),
                "complete": ranking.get("complete"),
            },
            "picks": picks,
        }

    @staticmethod
    def _selection_status(selection, setup, decline, replacement, now):
        """A published pick's fate: ADMITTED, SUPERSEDED, REPLACED_BY, DECLINED, EXPIRED or
        SELECTED; None for a pick never published."""
        if selection is None:
            return None
        if setup is not None:
            return "ADMITTED"
        if decline is not None:
            if decline.get("reason") == SUPERSEDED_BY_NEW_RESEARCH:
                return "SUPERSEDED"
            if replacement is not None and replacement.get("outcome") == REPLACEMENT_PUBLISHED:
                return "REPLACED_BY"
            return "DECLINED"
        deadline = selection.get("review_valid_until") or selection.get("expires_at")
        try:
            if deadline is not None and now >= datetime.fromisoformat(deadline):
                return "EXPIRED"
        except (TypeError, ValueError):
            pass
        return "SELECTED"

    @staticmethod
    def _ranking_reasons(entry):
        """A ranking entry's veto reasons (VETOED), its code (NOT_RANKED) or nothing (RANKED)."""
        if entry is None:
            return None
        if entry.get("status") == VETOED:
            return list(entry.get("veto_reasons") or [])
        if entry.get("status") == NOT_RANKED:
            return [entry.get("reason")]
        return []


def _owner(principal):
    """(agent_id, include_legacy) whose records the caller may see as its own."""
    if getattr(principal, "role", None) != "muse":
        return None, False
    return principal.agent_id, bool(principal.legacy)
