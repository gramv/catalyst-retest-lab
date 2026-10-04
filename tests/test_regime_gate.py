"""Package risk-pacing, A2: ``CRYPTO_ENTRY_PACING_V1`` (regime_gate.py). At most two new crypto
entries per rolling 30 minutes, none while the streamed coins' median 1-hour return is at most
-2% (or unknown), none from 15 minutes before to 45 minutes after a scheduled CPI or FOMC
release. Every block is a WAIT (one per setup and minute), never a refusal.

Fixture evidence only: disposable PostgreSQL, the fake paper venue, fixture prices and fixture
calendars (the engine tests never depend on the real calendar's dates).
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from catalyst_lab import regime_gate
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_execution import ManagedExecution
from catalyst_lab.managed_service import STATE_FIELDS
from catalyst_lab.regime_gate import (
    BREADTH_UNAVAILABLE,
    CALENDAR_EXPIRED,
    MACRO_WINDOW,
    MARKET_DROP,
    RATE_LIMIT,
    EntryPacing,
    MacroCalendar,
    MarketBreadthWindow,
)
from tests.test_crypto_trigger import TOUCH, quote_only, rows, state, v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_managed_execution import observation, packet
from tests.test_system_check import market as market  # noqa: F401
from tests.test_system_check import stream, system_runtime

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
COINS = ("AAA/USD", "BBB/USD", "CCC/USD")


def feed(window, now, moves, coins=COINS):
    """Each coin at 100 an hour ago and 100 x (1 + move) now (one minute apart from others)."""
    for coin, move in zip(coins, moves, strict=True):
        window.observe(coin, D(100), now - timedelta(hours=1))
        window.observe(coin, D(100) * (1 + D(move)), now)


def quiet_calendar(events=()):
    return MacroCalendar("LAB_FIXTURE_CALENDAR", date(2099, 12, 31), tuple(events))


# --- The market window ------------------------------------------------------------------------


@pytest.mark.parametrize("moves,median,blocked", [
    (("-0.02", "-0.02", "-0.02"), "-0.02", True),          # Exactly -2%: blocked.
    (("-0.0199", "-0.03", "-0.0199"), "-0.0199", False),   # Just above: passes.
    (("-0.05", "-0.01", "0.03"), "-0.01", False),          # One coin's crash is not the market.
    (("-0.05", "-0.04", "0.10"), "-0.04", True),
])
def test_the_median_one_hour_return_gates_at_minus_two_percent(moves, median, blocked):
    window = MarketBreadthWindow()
    feed(window, T0, moves)
    value, evidence = window.breadth(T0)
    assert value == D(median) and evidence["coins"] == 3
    assert (value <= regime_gate.DROP_THRESHOLD) is blocked


def test_an_even_number_of_coins_takes_the_mean_of_the_middle_two():
    window = MarketBreadthWindow()
    feed(window, T0, ("-0.01", "-0.03", "0.02", "-0.04"), COINS + ("DDD/USD",))
    assert window.breadth(T0)[0] == D("-0.02")
    assert regime_gate.median([]) is None


def test_fewer_than_three_measurable_coins_is_unavailable():
    window = MarketBreadthWindow()
    feed(window, T0, ("0.01", "0.01"), COINS[:2])
    window.observe("CCC/USD", D(100), T0)  # No price from an hour ago.
    value, evidence = window.breadth(T0)
    assert value is None and evidence["coins"] == 2 and evidence["min_coins"] == 3


def test_freshness_bounds_of_the_latest_and_the_hour_ago_price():
    window = MarketBreadthWindow()
    # The hour-ago price may be 60 to 70 minutes old; the latest at most 5 minutes old.
    window.observe("AAA/USD", D(100), T0 - timedelta(minutes=70))
    window.observe("AAA/USD", D(99), T0 - timedelta(minutes=5))
    window.observe("BBB/USD", D(100), T0 - timedelta(minutes=70, seconds=1))
    window.observe("BBB/USD", D(99), T0)
    window.observe("CCC/USD", D(100), T0 - timedelta(minutes=60))
    window.observe("CCC/USD", D(99), T0 - timedelta(minutes=5, seconds=1))
    assert set(window.returns(T0)) == {"AAA/USD"}
    assert window.returns(T0)["AAA/USD"][0] == D("-0.01")


def test_the_last_price_of_each_minute_is_kept_and_old_prices_are_pruned():
    window = MarketBreadthWindow()
    window.observe("AAA/USD", D(100), T0 - timedelta(minutes=61))
    window.observe("AAA/USD", D(90), T0 - timedelta(minutes=60, seconds=50))  # Same minute.
    window.observe("AAA/USD", D(95), T0 - timedelta(minutes=60, seconds=55))
    window.observe("AAA/USD", D(99), T0)
    window.observe("AAA/USD", D(-1), T0)  # Ignored.
    # The minute keeps its latest print (90 at :10), not the one that arrived after it (:05).
    assert window.returns(T0)["AAA/USD"][0] == D(99) / D(90) - 1
    window.observe("AAA/USD", D(98), T0 + timedelta(minutes=90))
    assert window.returns(T0 + timedelta(minutes=90)) == {}  # The old prices are gone.


# --- The calendar -----------------------------------------------------------------------------


def test_the_calendar_file_parses_covers_the_owners_period_and_says_to_extend_it():
    calendar = MacroCalendar.load()
    raw = json.loads(regime_gate.CALENDAR_FILE.read_text())
    assert calendar.version == "US_MACRO_CALENDAR_V1" and "MUST BE EXTENDED" in raw["note"]
    assert calendar.covers_through == date(2026, 12, 31)  # BLS 2027 CPI dates not yet out.
    assert raw["coverage"]["FOMC"]["through"] == "2027-03-31"
    kinds = {}
    for kind, instant, _ in calendar.events:
        kinds.setdefault(kind, []).append(instant)
    # CPI at 08:30 New York, FOMC statements at 14:00 New York (EDT and EST both).
    assert kinds["CPI"] == [datetime(2026, 10, 14, 12, 30, tzinfo=UTC),
                            datetime(2026, 11, 10, 13, 30, tzinfo=UTC),
                            datetime(2026, 12, 10, 13, 30, tzinfo=UTC)]
    assert kinds["FOMC"] == [datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
                             datetime(2026, 12, 9, 19, 0, tzinfo=UTC),
                             datetime(2027, 1, 27, 19, 0, tzinfo=UTC),
                             datetime(2027, 3, 17, 18, 0, tzinfo=UTC)]


def test_the_macro_window_runs_from_fifteen_minutes_before_to_forty_five_after():
    calendar = MacroCalendar.load()
    cpi = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert calendar.window(cpi - timedelta(minutes=15, seconds=1)) is None
    assert calendar.window(cpi - timedelta(minutes=15))["kind"] == "CPI"
    assert calendar.window(cpi + timedelta(minutes=44, seconds=59))["kind"] == "CPI"
    assert calendar.window(cpi + timedelta(minutes=45)) is None
    fomc = datetime(2026, 12, 9, 19, 0, tzinfo=UTC)
    assert calendar.window(fomc)["label"].startswith("FOMC statement")
    assert not calendar.expired(datetime(2026, 12, 31, 23, 0, tzinfo=UTC))  # 18:00 New York.
    assert calendar.expired(datetime(2027, 1, 1, 5, 0, tzinfo=UTC))  # 00:00 New York.


def test_an_invalid_calendar_file_is_refused(tmp_path):
    raw = json.loads(regime_gate.CALENDAR_FILE.read_text())
    for change in ({"version": "OTHER"}, {"timezone": "UTC"},
                   {"coverage": {"CPI": raw["coverage"]["CPI"]}},
                   {"events": [{"kind": "NFP", "date": "2026-11-06", "time": "08:30"}]},
                   {"events": [{"kind": "CPI", "date": "2027-02-10", "time": "08:30"}]}):
        path = tmp_path / "calendar.json"
        path.write_text(json.dumps({**raw, **change}))
        with pytest.raises(ValueError, match="^MACRO_CALENDAR_INVALID$"):
            MacroCalendar.load(path)


# --- The engine -------------------------------------------------------------------------------


def paced(mx, *, calendar=None, moves=("0", "0", "0")):
    """The fixture engine with pacing: a flat three-coin market and a quiet calendar."""
    engine, venue, _ = mx
    pacing = EntryPacing(MarketBreadthWindow(), calendar or quiet_calendar())
    engine.entry_pacing = pacing
    if moves is not None:
        feed(pacing.window, venue.now, moves)
    return pacing


def refeed(mx, pacing, moves=("0", "0", "0")):
    """A market as a continuous stream would leave it at the new time (a fresh window: the
    fixture's two prices per coin cannot be appended out of order)."""
    pacing.window = MarketBreadthWindow()
    feed(pacing.window, mx[1].now, moves)


def admit(mx, symbol):
    return mx[0].admit(packet(mx, symbol, expires_at=mx[1].now + timedelta(hours=3)))


def under_v3(mx):
    """The fixture engine admitting under JEV_MANAGED_RISK_V3, so three entries fit in the
    account (the legacy fixture policy holds only two)."""
    engine, venue, reviews = mx
    managed = ManagedExecution(engine.repo, engine.broker, policy=engine.policy,
                               clock=engine.now, review_store=reviews,
                               risk_policy_id="JEV_MANAGED_RISK_V3")
    assert managed.reconcile()["clean"]
    return managed, venue, reviews


def waits(engine, sid):
    return rows(engine, regime_gate.WAIT_EVENT, sid)


def decisions(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute("SELECT outcome,reason FROM lab.managed_risk_decisions "
                            "WHERE setup_id=%s", (sid,)).fetchall()


def test_admission_records_the_version_for_crypto_setups_of_a_paced_engine_only(mx):
    engine, _, _ = mx
    plain = admit(mx, "BTC/USD")
    assert regime_gate.STATE_FIELD not in state(engine, plain)
    paced(mx)
    crypto, stock = admit(mx, "ETH/USD"), admit(mx, "SPY")
    [v3] = v3_setups(mx, ["AAA/USD"]).values()
    assert state(engine, crypto)[regime_gate.STATE_FIELD] == "CRYPTO_ENTRY_PACING_V1"
    assert state(engine, v3)[regime_gate.STATE_FIELD] == "CRYPTO_ENTRY_PACING_V1"
    assert regime_gate.STATE_FIELD not in state(engine, stock)
    assert regime_gate.STATE_FIELD in STATE_FIELDS
    with pytest.raises(ValueError, match="EXPLICIT_ENTRY_PACING_REQUIRED"):
        ManagedExecution(engine.repo, engine.broker, policy=engine.policy, clock=engine.now,
                         review_store=mx[2], entry_pacing=object())


def test_two_entries_per_rolling_thirty_minutes_then_the_third_waits_and_enters_later(mx):
    mx = under_v3(mx)
    engine, venue, _ = mx
    pacing = paced(mx)
    first, second, third = (admit(mx, s) for s in ("ONE/USD", "TWO/USD", "THREE/USD"))
    start = venue.now
    assert engine.observe_trigger(first, observation(mx))["outcome"] == "APPROVED"
    venue.now += timedelta(minutes=10)
    refeed(mx, pacing)
    assert engine.reconcile()["clean"]
    decision = engine.observe_trigger(second, observation(mx))
    assert decision is not None, [w["body"] for w in waits(engine, second)]
    assert decision["outcome"] == "APPROVED"
    assert decision["context"]["entry_pacing"] == {
        "version": "CRYPTO_ENTRY_PACING_V1", "decided_at": venue.now.isoformat()}
    calls = len(venue.calls)
    assert engine.observe_trigger(third, observation(mx)) is None
    assert engine.observe_trigger(third, observation(mx)) is None  # The same minute.
    assert len(venue.calls) == calls  # Held back before any broker read.
    assert state(engine, third)["state"] == "WATCHING" and decisions(engine, third) == []
    [wait] = waits(engine, third)
    assert wait["body"]["reason"] == RATE_LIMIT
    assert wait["body"]["detail"] == {"entries": 2, "max_entries": 2, "window_seconds": 1800}
    minute = venue.now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    assert wait["idempotency_key"] == f"crypto-entry-pacing-wait:{third}:{minute}"
    # 29:59 after the first entry it still counts; at 30:00 it leaves the window.
    venue.now = start + timedelta(minutes=29, seconds=59)
    refeed(mx, pacing)
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(third, observation(mx)) is None
    assert len(waits(engine, third)) == 2
    venue.now = start + timedelta(minutes=30)
    refeed(mx, pacing)
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(third, observation(mx))["outcome"] == "APPROVED"
    assert verify_events(engine.repo.export_events())["valid"]


def test_rejected_decisions_and_us_entries_do_not_count(mx):
    engine, venue, _ = mx
    paced(mx)
    venue.equity = "10000"
    stock = admit(mx, "SPY")
    assert regime_gate.STATE_FIELD not in state(engine, stock)
    with engine.repo.connect() as conn:
        assert regime_gate.recent_entries(conn, venue.now) == 0
    first = admit(mx, "ONE/USD")
    assert engine.observe_trigger(first, observation(mx))["outcome"] == "APPROVED"
    with engine.repo.connect() as conn:
        assert regime_gate.recent_entries(conn, venue.now) == 1
        assert regime_gate.recent_entries(conn, venue.now + timedelta(minutes=30)) == 0


def test_a_falling_market_holds_entries_and_a_recovered_one_lets_them_in(mx):
    engine, venue, _ = mx
    pacing = paced(mx, moves=("-0.03", "-0.02", "-0.025"))
    sid = admit(mx, "DROP/USD")
    assert engine.observe_trigger(sid, observation(mx)) is None
    [wait] = waits(engine, sid)
    assert wait["body"]["reason"] == MARKET_DROP
    assert wait["body"]["detail"]["median_1h_return"] == "-0.025"
    assert state(engine, sid)["state"] == "WATCHING" and decisions(engine, sid) == []
    venue.now += timedelta(minutes=1)
    refeed(mx, pacing, ("-0.03", "-0.019", "0.01"))
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"


def test_no_market_data_waits_fail_safe(mx):
    engine, venue, _ = mx
    paced(mx, moves=None)
    sid = admit(mx, "EMPTY/USD")
    calls = len(venue.calls)
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert len(venue.calls) == calls
    [wait] = waits(engine, sid)
    assert wait["body"]["reason"] == BREADTH_UNAVAILABLE and wait["body"]["detail"]["coins"] == 0


def test_a_scheduled_release_holds_entries_from_fifteen_before_to_forty_five_after(mx):
    engine, venue, _ = mx
    release = venue.now + timedelta(minutes=15)
    pacing = paced(mx, calendar=quiet_calendar([("CPI", release, "LAB_FIXTURE CPI")]))
    sid = admit(mx, "MACRO/USD")
    assert engine.observe_trigger(sid, observation(mx)) is None
    [wait] = waits(engine, sid)
    assert (wait["body"]["reason"], wait["body"]["detail"]["kind"]) == (MACRO_WINDOW, "CPI")
    venue.now = release + timedelta(minutes=45)
    refeed(mx, pacing)
    assert engine.reconcile()["clean"]
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"


def test_an_expired_calendar_or_a_missing_configuration_waits(mx):
    engine, venue, _ = mx
    pacing = paced(mx, calendar=MacroCalendar("LAB_FIXTURE", date(2000, 1, 1), ()))
    sid = admit(mx, "OLDCAL/USD")
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert waits(engine, sid)[0]["body"]["reason"] == CALENDAR_EXPIRED
    pacing.calendar = quiet_calendar()
    engine.entry_pacing = None  # A setup of the version on an engine without pacing.
    venue.now += timedelta(minutes=1)
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert waits(engine, sid)[1]["body"]["detail"] == {"configured": False}


def test_setups_admitted_before_the_version_are_not_gated(mx):
    engine, venue, _ = mx
    old = admit(mx, "OLD/USD")  # An engine without pacing.
    paced(mx, moves=None)  # Then pacing with no market data, which holds every new setup.
    new = admit(mx, "NEW/USD")
    assert engine.observe_trigger(new, observation(mx)) is None
    assert engine.observe_trigger(old, observation(mx))["outcome"] == "APPROVED"
    assert waits(engine, old) == []


def test_an_entry_approved_during_the_reads_makes_the_next_wait_under_the_lock(mx, monkeypatch):
    engine, venue, _ = mx
    paced(mx)
    first, second, third = (admit(mx, s) for s in ("L1/USD", "L2/USD", "L3/USD"))
    assert engine.observe_trigger(first, observation(mx))["outcome"] == "APPROVED"
    snapshot = engine.account_snapshot

    def entered_meanwhile(**options):
        result = snapshot(**options)
        monkeypatch.setattr(engine, "account_snapshot", snapshot)
        assert engine.observe_trigger(second, observation(mx))["outcome"] == "APPROVED"
        return result

    monkeypatch.setattr(engine, "account_snapshot", entered_meanwhile)
    assert engine.observe_trigger(third, observation(mx)) is None
    assert waits(engine, third)[0]["body"]["reason"] == RATE_LIMIT
    assert decisions(engine, third) == []


def test_a_report_v3_quote_touch_waits_then_enters(mx):
    engine, venue, _ = mx
    paced(mx, moves=None)
    [sid] = v3_setups(mx, ["AAA/USD"]).values()
    assert engine.observe_trigger(sid, quote_only(mx, *TOUCH)) is None
    assert waits(engine, sid)[0]["body"]["trigger"]["bid"] == TOUCH[0]
    assert not rows(engine, "TRIGGER_CONFIRMED", sid)
    refeed(mx, engine.entry_pacing)
    assert engine.observe_trigger(sid, quote_only(mx, *TOUCH))["outcome"] == "APPROVED"


def test_the_runtime_stream_feeds_the_market_window(mx, market):
    engine, venue, _ = mx
    pacing = paced(mx, moves=None)
    run = system_runtime(mx, market, ["AAA/USD", "BBB/USD"])
    stream(run, "AAA/USD", "99", "101", at=venue.now)
    run.market_message("CRYPTO", {"T": "t", "S": "BBB/USD", "p": "50", "i": 7,
                                  "t": venue.now.isoformat()})
    later = venue.now + timedelta(hours=1)
    assert set(pacing.window.returns(venue.now)) == set()  # No hour-ago price yet.
    pacing.window.observe("AAA/USD", D(98), later)
    assert pacing.window.returns(later)["AAA/USD"][0] == D("-0.02")


# --- The seed: completed one-minute bars after a start or a gap, and the basket --------------


def bar(close, end_at):
    return SimpleNamespace(close=D(close), end_at=end_at, completed=True)


class Bars:
    """A fixture bar reader: each coin's closes move linearly from 100 to 100 x (1 + move)
    over the 70 minutes before ``now``; ``failing`` coins return an issue and no bar."""

    def __init__(self, now, moves, failing=(), raising=()):
        self.now, self.moves, self.failing, self.raising = now, moves, failing, raising
        self.reads = []

    def __call__(self, symbol, start, end):
        self.reads.append((symbol, start, end))
        if symbol in self.raising:
            raise RuntimeError("fixture")
        if symbol in self.failing or symbol not in self.moves:
            return (), ("FIXTURE_BARS_UNAVAILABLE",)
        move, bars, at = D(self.moves[symbol]), [], start + timedelta(minutes=1)
        while at <= end:
            elapsed = D((self.now - at).total_seconds()) / D(3600)
            bars.append(bar(D(100) * (1 + move * (1 - elapsed)), at))
            at += timedelta(minutes=1)
        return tuple(bars), ()


def test_the_seed_makes_the_median_available_at_once_and_runs_once_a_minute():
    pacing = EntryPacing(MarketBreadthWindow(), quiet_calendar())
    now = T0 + timedelta(seconds=30)
    reader = Bars(now, {c: "-0.03" for c in COINS})
    assert pacing.window.breadth(now)[0] is None  # A fresh start: nothing measurable.
    record = regime_gate.refresh(pacing, reader, set(COINS), now)
    assert record["seeded"] == list(COINS) and record["failed"] == {} and record["reads"] == 3
    assert [(s, start, end) for s, start, end in reader.reads][0] == (
        "AAA/USD", T0 - timedelta(minutes=70), T0)  # 70 minutes of completed bars.
    value, evidence = pacing.window.breadth(now)
    assert evidence["coins"] == 3 and value <= regime_gate.DROP_THRESHOLD  # About -3%.
    assert regime_gate.refresh(pacing, reader, set(COINS), now + timedelta(seconds=59)) is None
    assert len(reader.reads) == 3  # Not due within the minute.
    # Streamed coins keep their own fresh prices: the next refresh reads nothing for them.
    later = now + timedelta(seconds=60)
    for coin in COINS:
        pacing.window.observe(coin, D(97), later)
    assert regime_gate.refresh(pacing, reader, set(COINS), later)["reads"] == 0


def test_a_failed_seed_leaves_the_gate_waiting(mx):
    engine, venue, _ = mx
    pacing = paced(mx, moves=None)
    reader = Bars(venue.now, {"AAA/USD": "0"}, failing={"BBB/USD"}, raising={"CCC/USD"})
    record = regime_gate.refresh(pacing, reader, set(COINS), venue.now)
    assert record["seeded"] == ["AAA/USD"]
    assert record["failed"] == {"BBB/USD": ["FIXTURE_BARS_UNAVAILABLE"],
                                "CCC/USD": ["RUNTIMEERROR"]}
    sid = admit(mx, "SEEDFAIL/USD")
    assert engine.observe_trigger(sid, observation(mx)) is None
    assert waits(engine, sid)[0]["body"]["reason"] == BREADTH_UNAVAILABLE


def test_after_a_restart_a_seeded_window_lets_the_first_trigger_enter(mx):
    engine, venue, _ = mx
    pacing = paced(mx, moves=None)
    regime_gate.refresh(pacing, Bars(venue.now, {c: "0.01" for c in COINS}), set(COINS),
                        venue.now)
    sid = admit(mx, "SEEDED/USD")
    assert engine.observe_trigger(sid, observation(mx))["outcome"] == "APPROVED"


def test_fewer_than_three_streamed_coins_add_the_reference_basket():
    assert regime_gate.seed_symbols({"AAA/USD"}) == ["AAA/USD", "BTC/USD", "ETH/USD", "SOL/USD"]
    assert regime_gate.seed_symbols(set()) == list(regime_gate.BASKET)
    assert regime_gate.seed_symbols(set(COINS)) == list(COINS)  # Three streamed: no basket.
    pacing = EntryPacing(MarketBreadthWindow(), quiet_calendar())
    now = T0
    reader = Bars(now, {"AAA/USD": "0", "BTC/USD": "0.01", "ETH/USD": "-0.01",
                        "SOL/USD": "0.02"})
    record = regime_gate.refresh(pacing, reader, {"AAA/USD"}, now)
    assert record["seeded"] == ["AAA/USD", "BTC/USD", "ETH/USD"]  # Three reads a tick.
    assert regime_gate.refresh(pacing, reader, {"AAA/USD"}, now)["seeded"] == ["SOL/USD"]
    value, evidence = pacing.window.breadth(now)
    assert evidence["coins"] == 4 and value is not None
    # The basket is not streamed: it is refreshed from bars, only from its latest price on.
    reader.reads.clear()
    pacing.window.observe("AAA/USD", D(100), now + timedelta(minutes=3))  # Streamed: fresh.
    later = now + timedelta(minutes=3)
    reader.now = later
    record = regime_gate.refresh(pacing, reader, {"AAA/USD"}, later)
    assert [r[0] for r in reader.reads] == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert all(start == now and end == later for _, start, end in reader.reads)
    assert pacing.window.breadth(later)[1]["coins"] == 4


def test_seeded_bars_merge_with_stream_prices_and_the_latest_in_a_minute_wins():
    window = MarketBreadthWindow()
    window.observe("AAA/USD", D(101), T0 + timedelta(seconds=30))
    window.seed("AAA/USD", [(D(100), T0 - timedelta(minutes=60)), (D(99), T0)])
    assert window.returns(T0 + timedelta(seconds=30))["AAA/USD"][0] == D("0.01")
    window.seed("AAA/USD", [(D(0), T0), (D(5), datetime(2026, 1, 1))])  # Ignored: invalid.
    assert window.returns(T0 + timedelta(seconds=30))["AAA/USD"][0] == D("0.01")


def test_the_runtime_seeds_on_its_tick_and_again_after_a_gap(mx, market, monkeypatch):
    engine, venue, _ = mx
    pacing = paced(mx, moves=None)
    run = system_runtime(mx, market, list(COINS))
    reader = Bars(venue.now, {c: "0" for c in COINS})
    monkeypatch.setattr(run.source, "window_bars",
                        lambda market_, symbol, start, end: reader(symbol, start, end),
                        raising=False)
    run.execution_once()
    assert pacing.last_refresh["seeded"] == list(COINS)
    assert pacing.window.breadth(venue.now)[0] == D(0)
    reads = len(reader.reads)
    run.execution_once()  # Within the minute: no read.
    assert len(reader.reads) == reads
    run.market_gap("CRYPTO", "FIXTURE_GAP")
    assert pacing.next_refresh_at is None  # Due at once after the gap.
    monkeypatch.setattr(run.source, "window_bars",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
                        raising=False)
    run._refresh_entry_pacing()  # A failing source never raises out of the tick.


def test_a_large_seed_spreads_over_ticks_three_reads_each():
    pacing = EntryPacing(MarketBreadthWindow(), quiet_calendar())
    coins = [f"C{i}/USD" for i in range(7)]
    reader = Bars(T0, {c: "0" for c in coins})
    first = regime_gate.refresh(pacing, reader, set(coins), T0)
    assert (first["reads"], len(first["pending"])) == (3, 4) and pacing.next_refresh_at is None
    second = regime_gate.refresh(pacing, reader, set(coins), T0 + timedelta(seconds=1))
    assert (second["reads"], len(second["pending"])) == (3, 1)
    third = regime_gate.refresh(pacing, reader, set(coins), T0 + timedelta(seconds=2))
    assert (third["reads"], third["pending"]) == (1, [])
    assert pacing.next_refresh_at == T0 + timedelta(seconds=62)  # Then once a minute.
    assert pacing.window.breadth(T0 + timedelta(seconds=2))[1]["coins"] == 7
