"""Shared fixtures for the research kit's research-loop tests (package research-loop-kit):
update runs under RESEARCH_SCHEDULE_V2 and the daily run's derivatives context.

Offline only: fixture research contexts in the app's RESEARCH_CONTEXT_V3 shape (the contract
package research-loop-app gave), Coinbase candle rows, and OKX and Hyperliquid answers.
"""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from catalyst_lab.research_schedule import ResearchSchedule
from research_agent import build

TWO_HOURLY = tuple(f"{hour:02d}:00" for hour in range(0, 24, 2))
V2 = ResearchSchedule("America/New_York", TWO_HOURLY, 60, "08:00")
V1 = ResearchSchedule("America/New_York", TWO_HOURLY, 60)
UPDATE_AT = datetime(2026, 9, 29, 14, 5, tzinfo=UTC)  # 10:05 New York: the 10:00 update run.
MARKET_AT = UPDATE_AT - timedelta(minutes=3)
FAKE_TOKEN = "fixture-research-loop-kit-token-never-written"  # noqa: S105 - fixture
UPDATE_VERSION = "claude-as-muse-update-v1-09.29"
AGENT = ["--agent-id", "muse", "--agent-version", UPDATE_VERSION]
BID, ASK = "100.09", "100.11"  # Mid 100.10 for every fixture coin.
# The 1-hour setup setup_rows gives under INTRADAY_V2 at mid 100.10 (1h/20, rule A).
SETUP = {"entry_trigger": "99.60", "max_entry_price": "99.75", "stop": "89.64",
         "target": "130"}


def schedule_block(schedule, now):
    """The context's ``schedule`` exactly as the app serves it: V1's keys, and under V2
    ``daily``, ``current_run_kind`` and ``next_run_kinds`` in the contract's order."""
    current = schedule.latest_at_or_before(now)
    block = {**schedule.as_dict(), "current_run_slot": schedule.local(current).isoformat()}
    if schedule.daily:
        block["current_run_kind"] = schedule.run_kind(current)
    block["current_run_valid_until_limit"] = schedule.local(
        schedule.validity_limit(current)).isoformat()
    upcoming = schedule.next_runs(now)
    block["next_runs"] = [schedule.local(run).isoformat() for run in upcoming]
    if schedule.daily:
        block["next_run_kinds"] = [schedule.run_kind(run) for run in upcoming]
    return block


def context_v3(symbols, *, now=UPDATE_AT, schedule=V2, watching=(), open_trades=(),
               version="RESEARCH_CONTEXT_V3", quotes=None):
    """A ``RESEARCH_CONTEXT_V3``: V2 unchanged plus ``watching_setups``."""
    quote_at = (now - timedelta(seconds=20)).isoformat()
    quotes = quotes or {}
    body = {
        "context_version": version, "as_of": now.isoformat(),
        "caller": {"role": "muse", "agent_id": "muse"},
        "schedule": schedule_block(schedule, now),
        "report_format": {"schema_version": "AGENT_RESEARCH_REPORT_V3",
                          "guidelines_version": build.GUIDELINES_V6_VERSION,
                          "guidelines_sha256": build.GUIDELINES_V6_SHA256,
                          "max_report_age_seconds": 60},
        "universe": {"count": len(symbols), "coins": [
            {"symbol": symbol, "bid": quotes.get(symbol, (BID, ASK))[0],
             "ask": quotes.get(symbol, (BID, ASK))[1], "quote_at": quote_at,
             "spread_bps": "2.00", "price_increment": "0.01", "min_order_size": "0.01",
             "quantity_increment": "0.01",
             "volume_24h": {"base": "1000", "usd": "100000.00", "completed_hour_bars": 24}}
            for symbol in symbols], "excluded": {}, "issues": []},
        "open_trades": list(open_trades), "pending_reviews": [], "recent_outcomes": {},
        "lessons": None, "trade_authorized": False,
    }
    if version == "RESEARCH_CONTEXT_V3":
        body["watching_setups"] = list(watching)
    return body


def watching_row(symbol, levels, *, run_slot="2026-09-29T08:00:00-04:00",
                 expires_at="2026-09-30T09:00:00-04:00"):
    """One ``watching_setups`` row, exactly the app's shape."""
    coin = symbol.split("/")[0]
    return {"setup_id": str(uuid5(NAMESPACE_URL, f"fixture-setup:{symbol}:{levels}")),
            "symbol": symbol, "levels": {key: str(value) for key, value in levels.items()},
            "run_slot": run_slot, "expires_at": expires_at, "signal_id": f"RA260929-{coin}"}


def open_trade(symbol, *, state="OPEN"):
    """One ``open_trades`` row (``research_context._open_trades``' shape)."""
    return {"setup_id": str(uuid5(NAMESPACE_URL, f"fixture-trade:{symbol}")), "symbol": symbol,
            "signal_id": f"RA260928-{symbol.split('/')[0]}", "state": state, "entry": "99.7",
            "stop": "89.64", "target": "130", "quantity": "1",
            "opened_at": "2026-09-29T09:00:00+00:00", "levels": dict(SETUP),
            "unrealized_pnl_usd": "0.39", "unrealized_pnl_basis": "LATEST_BID_MINUS_AVERAGE_ENTRY",
            "review_at": None, "continuations": None, "holding_window_seconds": None}


def hourly(lows, highs, *, end):
    """Coinbase 1-hour rows ``[time, low, high, open, close, volume]``, the last one ending at
    ``end`` (on the hour)."""
    first = end - timedelta(hours=len(lows))
    return [[int((first + timedelta(hours=index)).timestamp()), str(low), str(high), str(low),
             str(low), "1"] for index, (low, high) in enumerate(zip(lows, highs, strict=True))]


def setup_rows(end=None):
    """80 hours whose last 20 hold INTRADAY_V2's 1-hour setup at mid 100.10 (``SETUP``): the
    window low 90 (hour 65), the window high 130 (hour 70) and the held entry 99.60 (hour 75),
    0.5% below the mid."""
    lows, highs = [100] * 80, [101] * 80
    lows[65], highs[65] = 90, 90.5
    highs[70] = 130
    lows[75], highs[75] = 99.6, 100.1
    return hourly(lows, highs, end=end or MARKET_AT.replace(minute=0))


def rally_rows(end=None):
    """80 hours with no setup: every window's high is its most recent bar."""
    lows = [100] * 80
    highs = [101] * 79 + [102]
    return hourly(lows, highs, end=end or MARKET_AT.replace(minute=0))


def two_structure_rows(end=None):
    """120 hours with two setups: the 1-hour one INTRADAY_V2 finds first (entry 99.60, stop
    96.61, target 107) and, on 4-hour bars, the one a daily run finds (entry 97.00, stop
    89.64, target 130)."""
    end = end or MARKET_AT.replace(minute=0)
    lows, highs = [100] * 120, [101] * 120
    lows[45], highs[45] = 90, 90.5  # The 4-hour window's low: the 4-hour stop.
    highs[60] = 130  # The 4-hour window's high: the 4-hour target.
    lows[104], highs[104] = 97, 97.5  # The 1-hour windows' low, and the 4-hour entry.
    highs[108] = 107  # The 1-hour windows' high: the 1-hour target.
    lows[112], highs[112] = 99.6, 100.1  # The 1-hour entry.
    return hourly(lows, highs, end=end)


def market_doc(rows_by_coin, *, retrieved_at=MARKET_AT, excluded=None, profile="INTRADAY_V2"):
    """``market.json`` as ``market`` writes it for the daily fetch (300 hours, 60 days)."""
    return {"profile": profile, "retrieved_at": retrieved_at.isoformat(),
            "coinbase": {coin: {"quote_increment": "0.01", "candles_1h": rows, "candles_1d": [],
                                "ticker": {"bid": BID, "ask": ASK,
                                           "time": retrieved_at.isoformat()}}
                         for coin, rows in rows_by_coin.items()},
            "excluded": dict(excluded or {})}


def fake_fetch(rows_by_coin, *, excluded=None, calls=None, retrieved_at=MARKET_AT):
    """``market.fetch_coinbase``'s fake: the requested coins' rows, and every other requested
    coin excluded, as the real fetch lists a coin it could not read."""
    def fetch(coins, **kwargs):
        if calls is not None:
            calls.append((sorted(coins), kwargs))
        found = {coin: rows for coin, rows in rows_by_coin.items() if coin in set(coins)}
        missing = {coin: (excluded or {}).get(coin, "NO_ONLINE_COINBASE_USD_PRODUCT")
                   for coin in coins if coin not in found}
        doc = market_doc(found, excluded=missing, retrieved_at=retrieved_at)
        doc.pop("profile")  # 'market' adds the profile itself.
        return doc
    return fetch


# --- OKX and Hyperliquid answers ---------------------------------------------------------------

LATEST_OI_MS = int(datetime(2026, 9, 29, 11, 45, tzinfo=UTC).timestamp() * 1000)
FUNDING_MS = int(datetime(2026, 9, 29, 11, 0, tzinfo=UTC).timestamp() * 1000) + 76
FETCHED_AT = datetime(2026, 9, 29, 11, 50, 7, 123456, tzinfo=UTC)


def oi_history(latest_ccy="1000", h4_ccy="990", h24_ccy="950", *, latest_ms=LATEST_OI_MS,
               rows=100, skip=()):
    """OKX's rows ``[ts, oi, oiCcy, oiUsd]``, newest first, one per 15 minutes; the rows 4 and
    24 hours before the latest carry ``h4_ccy`` and ``h24_ccy``, every other row
    ``latest_ccy``. Row indexes in ``skip`` are left out (a gap)."""
    found = []
    for index in range(rows):
        if index in skip:
            continue
        ccy = {0: latest_ccy, 16: h4_ccy, 96: h24_ccy}.get(index, latest_ccy)
        ts = latest_ms - index * 15 * 60 * 1000
        found.append([str(ts), str(int(float(ccy) * 10)), ccy, f"{float(ccy) * 100:.1f}"])
    return found


def funding_history(name="SOL", rate="0.0000125", *, time_ms=FUNDING_MS):
    return [{"coin": name, "fundingRate": "0.0000100", "premium": "0.0001",
             "time": time_ms - 3600 * 1000},
            {"coin": name, "fundingRate": rate, "premium": "0.00031234", "time": time_ms}]


def derivatives_entry(*, oi=None, funding=None, name="SOL", inst_id="SOL-USDT-SWAP",
                      retrieved_at=FETCHED_AT):
    """One coin of ``derivatives.json`` as ``derivatives`` writes it."""
    from research_agent import derivatives
    return {"okx": None if oi is None else {
                "inst_id": inst_id, "period": "15m", "url": derivatives.OKX_OPEN_INTEREST_URL,
                "retrieved_at": retrieved_at.isoformat(), "history": oi},
            "hyperliquid": None if funding is None else {
                "coin": name, "url": derivatives.HYPERLIQUID_INFO_URL,
                "retrieved_at": retrieved_at.isoformat(), "history": funding},
            "omitted": {}}
