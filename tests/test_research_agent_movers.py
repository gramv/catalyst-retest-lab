"""research_agent.movers and the ``movers`` command: what moved over one New York day.

Offline only: fixture Coinbase candles, httpx.MockTransport for the fetcher, and a
monkeypatched fetch for the CLI. No real network access anywhere in this file.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from research_agent import market, movers, run
from research_agent import market as market_module

D = Decimal
DAY = date(2026, 9, 28)
DAY_START = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)  # Midnight in New York (EDT).
DAY_END = DAY_START + timedelta(hours=24)
RETRIEVED_AT = DAY_END + timedelta(minutes=5)


def rows(start, closes, *, volumes=None, first_open=None):
    out, previous = [], first_open if first_open is not None else closes[0]
    for index, close in enumerate(closes):
        volume = volumes[index] if volumes else 10
        t = int((start + timedelta(hours=index)).timestamp())
        out.append([t, str(min(previous, close)), str(max(previous, close)), str(previous),
                    str(close), str(volume)])
        previous = close
    return out


def coin_rows(day_closes, *, baseline_close=100, day_volume=10, baseline_volume=10):
    """168 flat baseline hours at ``baseline_close``, then the day's 24 closes."""
    baseline = rows(DAY_START - timedelta(days=7), [baseline_close] * 168,
                    volumes=[baseline_volume] * 168)
    day = rows(DAY_START, day_closes, volumes=[day_volume] * len(day_closes),
               first_open=baseline_close)
    return baseline + day


def ramp(final, *, start=100, at=10):
    """Flat at ``start`` for ``at`` hours, then a step to ``final`` held to the end."""
    return [start] * at + [final] * (24 - at)


def fetched(coin_rows_by_coin, *, excluded=None, retrieved_at=RETRIEVED_AT):
    return {"retrieved_at": retrieved_at.isoformat(),
            "coinbase": {coin: {"candles_1h": r} for coin, r in coin_rows_by_coin.items()},
            "excluded": excluded or {}}


# --- Windows ---------------------------------------------------------------------------------

@pytest.mark.parametrize("day, hours", [
    (date(2026, 9, 28), 24),
    (date(2026, 11, 1), 25),  # Daylight saving ends in New York.
    (date(2027, 3, 14), 23),  # Daylight saving starts.
])
def test_a_new_york_day_is_its_own_number_of_hours(day, hours):
    start, end = movers.day_window(day)
    assert (end - start) == timedelta(hours=hours)
    assert start.hour in (4, 5)  # Midnight in New York, in UTC.


def test_the_last_hours_end_at_the_last_whole_hour_before_retrieval():
    start, end = movers.hours_window(datetime(2026, 9, 28, 15, 42, tzinfo=UTC), 24)
    assert end == datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
    assert start == end - timedelta(hours=24)
    with pytest.raises(movers.MoversError):
        movers.hours_window(end, 0)


def test_a_day_that_has_not_ended_is_refused():
    start, end = movers.day_window(DAY)
    with pytest.raises(movers.MoversError, match="DAY_NOT_OVER"):
        movers.check_day_over(end, end - timedelta(seconds=1), DAY)
    movers.check_day_over(end, end, DAY)  # Exactly at its end is fine.


# --- Selecting the movers --------------------------------------------------------------------

def test_the_top_five_each_way_and_every_five_percent_move():
    returns = {"A": D(9), "B": D(7), "C": D(3), "D": D(2), "E": D(1), "F": D("0.5"),
               "G": D(-1), "H": D(-6), "Z": D(0)}
    chosen = movers.select_movers(returns)
    assert list(chosen) == ["A", "B", "H", "C", "D", "E", "G"]  # Largest move first.
    assert chosen["A"] == {"reasons": ["TOP_UP", "RETURN_AT_LEAST_5_PCT"], "rank": 1}
    assert chosen["H"] == {"reasons": ["TOP_DOWN", "RETURN_AT_LEAST_5_PCT"], "rank": 1}
    assert chosen["G"] == {"reasons": ["TOP_DOWN"], "rank": 2}  # Only two coins fell.
    assert "F" not in chosen and "Z" not in chosen  # Sixth riser; a coin that did not move.


def test_a_five_percent_move_outside_the_top_five_is_still_a_mover():
    returns = {f"U{i}": D(10 + i) for i in range(6)}  # Six rises of 10%+.
    chosen = movers.select_movers(returns)
    assert chosen["U0"] == {"reasons": ["RETURN_AT_LEAST_5_PCT"], "rank": None}
    assert len(chosen) == 6


# --- movers.json -----------------------------------------------------------------------------

def test_build_movers_measures_every_coin_and_details_the_movers():
    data = fetched({
        "SOL": coin_rows(ramp(110), day_volume=30),  # +10%, volume 3x
        "ETH": coin_rows(ramp(97)),  # -3%
        "BTC": coin_rows([100] * 24),  # flat
    }, excluded={"NEW": "NO_COINBASE_USD_CANDLES"})
    result = movers.build_movers(["BTC/USD", "ETH/USD", "SOL/USD", "NEW/USD"], data,
                                 start=DAY_START, end=DAY_END, day=DAY)

    assert result["schema"] == "RESEARCH_AGENT_MOVERS_V1"
    assert result["window"] == {"kind": "NEW_YORK_DAY", "day": "2026-09-28",
                                "timezone": "America/New_York", "start": DAY_START.isoformat(),
                                "end": DAY_END.isoformat(), "hours": 24}
    assert result["excluded"] == {"NEW": "NO_COINBASE_USD_CANDLES"}
    sol = result["coins"]["SOL"]
    assert (sol["return_pct"], sol["up_excursion_pct"], sol["down_excursion_pct"]) == (
        "10.00", "10.00", "0.00")
    assert (sol["bars_expected"], sol["bars_used"], sol["baseline_bars"]) == (24, 24, 168)
    assert sol["volume_ratio_7d"] == "3.00"
    assert sol["move_start"]["bar_id"] == "SOL-1h-20260928T1400Z"
    assert sol["move_start"]["hours_into_window"] == 10
    assert result["coins"]["BTC"]["move_start"] is None  # No move, no start.

    by_coin = {row["coin"]: row for row in result["movers"]}
    assert set(by_coin) == {"SOL", "ETH"}  # BTC did not move at all.
    assert by_coin["SOL"]["reasons"] == ["TOP_UP", "RETURN_AT_LEAST_5_PCT"]
    assert by_coin["ETH"]["reasons"] == ["TOP_DOWN"]
    assert len(by_coin["SOL"]["bars"]) == 24
    assert by_coin["SOL"]["pre_move"]["as_of"] == "2026-09-28T14:00:00+00:00"
    assert "TIGHT_RANGE_24H" in result["coins"]["SOL"]["pre_window"]["tags"]


def test_a_bar_that_had_not_ended_at_retrieval_never_counts():
    # Retrieved one minute before the day's last bar ended (hours window, so not refused).
    early = DAY_END - timedelta(minutes=1)
    data = fetched({"SOL": coin_rows(ramp(110, at=23))}, retrieved_at=early)
    result = movers.build_movers(["SOL/USD"], data, start=DAY_START, end=DAY_END)
    sol = result["coins"]["SOL"]
    assert sol["bars_used"] == 23 and sol["return_pct"] == "0.00"  # The 110 bar is left out.
    assert result["movers"] == []


def test_a_coin_with_no_completed_bar_in_the_window_is_excluded_with_its_reason():
    data = fetched({"OLD": rows(DAY_START - timedelta(days=7), [5] * 168)})
    result = movers.build_movers(["OLD/USD"], data, start=DAY_START, end=DAY_END, day=DAY)
    assert result["excluded"] == {"OLD": "NO_COMPLETED_BARS_IN_WINDOW"}
    assert result["coins"] == {}


# --- The fetcher: explicit spans in pieces of at most 300 hours ------------------------------

def test_fetch_hourly_span_asks_in_pieces_and_excludes_a_coin_without_candles():
    seen = []

    def handle(request):
        if "NOPE-USD" in request.url.path:
            return httpx.Response(404)
        seen.append((request.url.params["start"], request.url.params["end"]))
        return httpx.Response(200, json=[[int(DAY_START.timestamp()), "1", "2", "1", "2", "5"]])

    client = httpx.Client(transport=httpx.MockTransport(handle))
    start = DAY_START - timedelta(hours=400)
    result = market.fetch_hourly_span(["SOL", "NOPE"], start=start, end=DAY_END, client=client,
                                      now=RETRIEVED_AT, sleep=0)
    assert seen == [(start.isoformat(), (start + timedelta(hours=300)).isoformat()),
                    ((start + timedelta(hours=300)).isoformat(), DAY_END.isoformat())]
    assert len(result["coinbase"]["SOL"]["candles_1h"]) == 2
    assert result["excluded"] == {"NOPE": "NO_COINBASE_USD_CANDLES"}
    assert result["retrieved_at"] == RETRIEVED_AT.isoformat()
    with pytest.raises(market.MarketDataError):
        market.fetch_hourly_span(["SOL"], start=DAY_END, end=DAY_END, client=client, sleep=0)


# --- The command -----------------------------------------------------------------------------

def _context(symbols):
    return {"context_version": "RESEARCH_CONTEXT_V1",
            "universe": {"count": len(symbols), "coins": [{"symbol": s} for s in symbols]}}


def test_cli_movers_for_a_day_writes_movers_json_in_that_days_evening_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "runs" / "2026-09-28" / "evening"
    folder.mkdir(parents=True)
    (folder / "context.json").write_text(json.dumps(_context(["SOL/USD", "ETH/USD"])))
    calls = []

    def fake_fetch(coins, *, start, end, now, **_kw):
        calls.append((coins, start, end, now))
        return fetched({"SOL": coin_rows(ramp(110)), "ETH": coin_rows(ramp(90))},
                       retrieved_at=now)

    monkeypatch.setattr(market_module, "fetch_hourly_span", fake_fetch)
    code = run.main(["movers", "--day", "2026-09-28", "--now", RETRIEVED_AT.isoformat()])
    assert code == 0
    assert calls == [(["ETH", "SOL"], DAY_START - timedelta(days=7), DAY_END, RETRIEVED_AT)]
    written = json.loads((folder / "movers.json").read_text())
    assert [row["coin"] for row in written["movers"]] == ["ETH", "SOL"]  # -10% and +10%.


def test_cli_movers_refuses_a_day_that_has_not_ended_before_any_fetch(tmp_path, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr(market_module, "fetch_hourly_span",
                        lambda *a, **k: pytest.fail("must not fetch"))
    code = run.main(["--run-dir", str(tmp_path), "movers", "--day", "2026-09-28",
                     "--now", (DAY_END - timedelta(hours=1)).isoformat()])
    assert code == 2 and "DAY_NOT_OVER" in capsys.readouterr().err


def test_cli_movers_needs_a_day_or_hours(tmp_path):
    with pytest.raises(SystemExit):
        run.main(["--run-dir", str(tmp_path), "movers"])
    with pytest.raises(SystemExit):
        run.main(["--run-dir", str(tmp_path), "movers", "--day", "28-09-2026"])
