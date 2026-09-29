"""Shared fixtures for the research kit's learning-loop tests (package learning-kit).

Offline only: fixture contexts, Coinbase candle rows, lessons and news pages. Named apart
from package learning-app's ``tests/learning_fixtures.py`` so the two never collide.
"""

from datetime import UTC, date, datetime, timedelta

from catalyst_lab.research_schedule import ResearchSchedule
from research_agent import build

NOW = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)  # 08:30 New York, the morning after DAY.
DAY = date(2026, 9, 28)
DAY_START = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
SCHEDULE = ResearchSchedule("America/New_York", ("08:00",), 60)
FAKE_TOKEN = "fixture-learning-kit-token-never-written"  # noqa: S105 - fixture
AGENT = ["--agent-id", "claude", "--agent-version", "kit-test-1"]


def hour_rows(start, closes, *, volumes=None, first_open=None):
    """Coinbase rows [time, low, high, open, close, volume], each open the previous close."""
    rows, previous = [], first_open if first_open is not None else closes[0]
    for index, close in enumerate(closes):
        t = int((start + timedelta(hours=index)).timestamp())
        volume = volumes[index] if volumes else 10
        rows.append([t, str(min(previous, close)), str(max(previous, close)), str(previous),
                     str(close), str(volume)])
        previous = close
    return rows


def context_v2(symbols, *, now=NOW, lessons=None, quotes=None):
    slot = SCHEDULE.latest_at_or_before(now)
    quote_at = (now - timedelta(seconds=20)).isoformat()
    quotes = quotes or {}
    return {
        "context_version": "RESEARCH_CONTEXT_V2", "as_of": now.isoformat(),
        "schedule": {"current_run_slot": SCHEDULE.local(slot).isoformat(),
                     "current_run_valid_until_limit":
                         SCHEDULE.local(SCHEDULE.validity_limit(slot)).isoformat()},
        "report_format": {"guidelines_version": build.GUIDELINES_V6_VERSION,
                          "guidelines_sha256": build.GUIDELINES_V6_SHA256,
                          "max_report_age_seconds": 60},
        "universe": {"count": len(symbols), "coins": [
            {"symbol": symbol, "bid": quotes.get(symbol, ("99.99", "100.01"))[0],
             "ask": quotes.get(symbol, ("99.99", "100.01"))[1], "quote_at": quote_at,
             "price_increment": "0.01", "spread_bps": "2.00",
             "volume_24h": {"base": "1000", "usd": "100000.00", "completed_hour_bars": 24}}
            for symbol in symbols], "excluded": {}, "issues": []},
        "open_trades": [], "pending_reviews": [], "recent_outcomes": {},
        "trade_authorized": False,
        "lessons": lessons,
    }


def market_json(rows_by_coin, *, retrieved_at):
    return {"retrieved_at": retrieved_at.isoformat(),
            "coinbase": {coin: {"quote_increment": "0.01", "candles_1h": rows,
                                "candles_1d": [], "ticker": {}}
                         for coin, rows in rows_by_coin.items()},
            "excluded": {}}


def pending_trade(setup_id="0b5d9d44-1f55-4a5e-9f8a-2f3c7a1e9b10", **changes):
    return {"setup_id": setup_id, "symbol": "SOL/USD", "entry_at": "2026-09-28T10:17:00+00:00",
            "entry_price": "100", "exit_at": "2026-09-28T15:42:00+00:00", "exit_price": "96",
            "exit_reason": "STOP", "r_net": "-1.04", "notable_reasons": ["STOP"], **changes}


def pending_mover(symbol="ETH/USD", *, was_miss=True, return_pct="8.20",
                  move_start_at="2026-09-28T13:00:00+00:00"):
    return {"symbol": symbol, "day": DAY.isoformat(), "return_pct": return_pct,
            "move_start_at": move_start_at, "was_miss": was_miss}


def lessons_fixture(*, trades=(), movers=(), recorded=(DAY,), outlook_status="GRADED",
                    outlook_day=DAY, windows=None):
    return {
        "lessons_version": "RESEARCH_LESSONS_V1", "agent_id": "claude",
        "as_of": NOW.isoformat(), "scorecard_day": DAY.isoformat(),
        "windows": windows or {"1d": None, "7d": None, "30d": None},
        "outlook": {"status": outlook_status,
                    "day": outlook_day.isoformat() if outlook_day else None},
        "recent_days": [{"day": day.isoformat(), "universe_count": 3, "measured_count": 3,
                         "mover_share": "0.33",
                         "movers": [{"symbol": row["symbol"]} for row in movers],
                         "factors": {}} for day in recorded],
        "pending_post_mortems": {"trades": list(trades), "movers": list(movers)},
    }


def news_page(excerpt, *, published="2026-09-28T09:00:00Z"):
    head = (f'<meta property="article:published_time" content="{published}">'
            if published else "")
    return f"<html><head>{head}</head><body><p>{excerpt}</p></body></html>"
