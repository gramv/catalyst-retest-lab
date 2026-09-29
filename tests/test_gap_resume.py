"""CRYPTO_GAP_RESUME_V1 (package gap-resume, plan phase 8 reliability): pending crypto setups
survive a market-stream gap or an app restart when price did not reach the entry meanwhile.

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue (with
Alpaca-shaped FILL account activities for the REST fill backfill), fake clocks, fake stream
sockets, scripted Alpaca crypto quotes and one-minute bars behind an httpx MockTransport and
the real read-only market source, and a mock Jev transport. No broker, provider, network,
supervisor or owner-ledger contact; nothing here is broker acceptance.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest

from catalyst_lab import gap_resume as gr
from catalyst_lab import managed_runtime
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.audit import verify_events
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.executor_lease import AccountExecutorLease, FencedAuthorizationGate
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_broker import ManagedPaperBroker
from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
from catalyst_lab.managed_ops import (
    GAP_RESUME_OVERDUE_SECONDS,
    gap_resume_alarms,
    status_alarms,
)
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from catalyst_lab.managed_service import STATE_FIELDS, STATUS_FIELDS
from catalyst_lab.managed_store import ManagedAuthorizationGate
from catalyst_lab.scan_sources import AlpacaMarketSource, SourceIssue, SourcePolicy
from catalyst_lab.setup_scan import CompletedBar
from tests.maintenance_fixtures import MaintenanceVenue, maintainer
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import packet
from tests.test_managed_runtime import Research

T0 = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)
LEVELS = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"}
CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
SOURCE_POLICY = SourcePolicy("iex", 3, 50, 10000, 10, 5)
IDENTITY = hashlib.sha256(b"fixture-gap-resume-paper-account").hexdigest()
BARS = "/v1beta3/crypto/us/bars"
QUOTES = "/v1beta3/crypto/us/latest/quotes"
AUTHORIZED = {"stream": "authorization", "data": {"status": "authorized"}}
LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}


def minute(at):
    return at.replace(second=0, microsecond=0)


def bar(start, low, high=None):
    """A completed one-minute bar opening and closing at its high."""
    low = D(str(low))
    high = D(str(high)) if high is not None else max(low, D("100.60"))
    return CompletedBar(start, start + timedelta(minutes=1), high, high, low, high, D("3"),
                        "ALPACA", "CRYPTO_US", f"ALPACA:CRYPTO_US:X/USD:bar:{start}", True)


def window(start, bars_start, bars_end, back=None):
    return {"start": start, "bars_start": bars_start, "bars_end": bars_end,
            "stream_back_at": back or bars_end}


def verdict(lows, *, prints=(), issues=(), start=T0, bars_end=None, checked_at=None):
    """The rule over one minute bar per low from T0's minute (None: no trade that minute)."""
    bars_end = bars_end or minute(start) + timedelta(minutes=len(lows))
    bars = [bar(minute(start) + timedelta(minutes=i), low)
            for i, low in enumerate(lows) if low is not None]
    return gr.decide(LEVELS, window=window(start, minute(start), bars_end), bars=bars,
                     issues=issues, prints=list(prints),
                     checked_at=checked_at or bars_end + timedelta(seconds=45))


def printed(price, at, trade_id="p-1"):
    return {"trade_price": price, "trade_at": at.isoformat(), "trade_id": trade_id,
            "event_seq": 7}


# --- The rule (pure) -----------------------------------------------------------------------------

def test_the_lowest_traded_price_decides_at_exact_decimal_boundaries():
    resumed = verdict(["100.2", None, "100.00000001", "101"])
    assert resumed.decision == "RESUMED" and resumed.reason is None
    evidence = resumed.evidence
    assert evidence["version"] == "CRYPTO_GAP_RESUME_V1"
    assert (evidence["bars"]["count"], evidence["bars"]["lowest_low"]) == (3, "100.00000001")
    assert evidence["bars"]["lowest_low_at"] == (T0 + timedelta(minutes=2)).isoformat()
    assert evidence["lowest"] == "100.00000001" and evidence["bars"]["sha256"]
    assert evidence["window"]["start"] == T0.isoformat()
    assert evidence["window"]["bar_settle_seconds"] == 30
    assert evidence["levels"] == {"entry_trigger": "100", "stop": "95"}
    # Exactly the entry trigger: the price reached the entry while nothing could act.
    assert verdict(["100.2", "100", "101"]).decision == "ENTRY_REACHED_DURING_GAP"
    assert verdict(["100.2", "95.00000001"]).decision == "ENTRY_REACHED_DURING_GAP"
    # Exactly the stop: invalidated, whatever else the window shows.
    assert verdict(["99", "95", "101"]).decision == "STOP_TRADED_DURING_GAP"
    assert verdict(["94.5", "99"]).evidence["lowest"] == "94.5"
    # A thin coin with no trade in the whole window: nothing reached anything.
    quiet = verdict([None, None, None])
    assert quiet.decision == "RESUMED" and quiet.evidence["bars"]["count"] == 0
    assert quiet.evidence["lowest"] is None


def test_unavailable_incomplete_or_malformed_bars_fail_closed():
    issue = SourceIssue("CRYPTO", "X/USD", "SCAN_HTTP_503", BARS)
    failed = verdict(["101"], issues=(issue,))
    assert failed.decision == "DATA_FEED_FAILURE"
    assert failed.evidence["bars"]["issues"] == ["SCAN_HTTP_503"]
    assert verdict(["101"], issues=("BAR_PAGINATION_LIMIT",)).decision == "DATA_FEED_FAILURE"
    # Even a stop in the bars read: with the read incomplete, the setup ends DATA_FEED_FAILURE.
    assert verdict(["94"], issues=(issue,)).decision == "DATA_FEED_FAILURE"
    start, end = minute(T0), minute(T0) + timedelta(minutes=3)
    cases = {
        "INVALID_BAR": CompletedBar(start, start + timedelta(minutes=1), D("101"), D("100.5"),
                                    D("101"), D("101"), D("1"), "A", "F", "x", True),
        "BAR_NOT_ONE_MINUTE": CompletedBar(start, start + timedelta(minutes=5), D("101"),
                                           D("101"), D("101"), D("101"), D("1"), "A", "F",
                                           "x", True),
        "BAR_OUTSIDE_WINDOW": bar(end, "101"),
    }
    for problem, odd in cases.items():
        result = gr.decide(LEVELS, window=window(T0, start, end), bars=[bar(start, "101"), odd],
                           issues=(), prints=[], checked_at=end + timedelta(seconds=40))
        assert result.decision == "DATA_FEED_FAILURE"
        assert result.evidence["bars"]["problems"] == [problem]
    twice = gr.decide(LEVELS, window=window(T0, start, end),
                      bars=[bar(start, "101"), bar(start, "101")], issues=(), prints=[],
                      checked_at=end + timedelta(seconds=40))
    assert twice.evidence["bars"]["problems"] == ["DUPLICATE_BAR"]
    empty = gr.decide(LEVELS, window=window(T0, end, end), bars=[], issues=(), prints=[],
                      checked_at=end + timedelta(seconds=40))
    assert empty.evidence["bars"]["problems"] == ["WINDOW_NOT_COVERED"]
    bad_print = verdict(["101"], prints=[{"trade_price": "x", "trade_at": T0.isoformat()}])
    assert bad_print.decision == "DATA_FEED_FAILURE"


def test_prints_the_stream_delivered_cover_the_tail_after_the_last_bar():
    end = minute(T0) + timedelta(minutes=3)
    tail = end + timedelta(seconds=20)  # After the last bar read, before the check.
    reached = verdict(["101", "101", "101"], prints=[printed("99.99", tail)])
    assert reached.decision == "ENTRY_REACHED_DURING_GAP"
    assert reached.evidence["prints"] == {
        "count": 1, "lowest": "99.99", "lowest_at": tail.isoformat(),
        "lowest_trade_id": "p-1", "lowest_event_seq": 7}
    assert reached.evidence["bars"]["lowest_low"] == "101" and reached.evidence["lowest"] == (
        "99.99")
    stop = verdict(["101"], prints=[printed("94", end - timedelta(seconds=90))])
    assert stop.decision == "STOP_TRADED_DURING_GAP"
    # A print before the window (evaluated live earlier) or after the check is not the gap's.
    early = verdict(["101"], prints=[printed("99", T0 - timedelta(seconds=1)),
                                     printed("99", end + timedelta(minutes=5))])
    assert early.decision == "RESUMED" and early.evidence["prints"]["count"] == 0


def test_window_arithmetic_minutes_settle_and_bounds():
    assert gr.due_at(T0 + timedelta(seconds=10)) == T0 + timedelta(seconds=90)
    assert gr.due_at(T0) == T0 + timedelta(seconds=30)  # Back exactly on a minute boundary.
    assert gr.bar_window(T0 + timedelta(seconds=45), T0 + timedelta(minutes=5, seconds=29)) == (
        T0, T0 + timedelta(minutes=4))
    assert gr.bar_window(T0, T0 + timedelta(minutes=5, seconds=30)) == (
        T0, T0 + timedelta(minutes=5))
    admitted = (T0 - timedelta(minutes=30)).isoformat()
    gap = T0
    assert gr.window_start(admitted, gap) == gap
    assert gr.window_start(admitted, None) == T0 - timedelta(minutes=30)  # Unknown: admission.
    assert gr.window_start(admitted, T0 - timedelta(hours=2)) == T0 - timedelta(minutes=30)
    assert gr.window_start(admitted, gap, open_start=T0 - timedelta(minutes=9)) == (
        T0 - timedelta(minutes=9))
    assert gr.window_start(admitted, gap, unconsumed_print_at=T0 - timedelta(seconds=3)) == (
        T0 - timedelta(seconds=3))
    assert gr.window_start(admitted, gap, open_start=T0 + timedelta(minutes=1)) == gap


def heartbeat(as_of, *, observed=True, since=None, runtime="previous-runtime"):
    return {"event_seq": 41, "body": {
        "runtime_id": runtime,
        "gap_resume": {"as_of": as_of.isoformat(), "markets": {
            "CRYPTO": {"observed": observed,
                       "unobserved_since": since.isoformat() if since else None},
            "US": {"observed": False, "unobserved_since": None}}}}}


def test_the_restart_bound_is_the_previous_runtimes_last_recorded_observation():
    now = T0 + timedelta(minutes=10)
    bound, basis = gr.restart_bound(heartbeat(T0), "CRYPTO", now)
    assert bound == T0
    assert basis == {"basis": "PREVIOUS_RUNTIME_HEARTBEAT", "heartbeat_event_seq": 41,
                     "heartbeat_runtime_id": "previous-runtime", "as_of": T0.isoformat(),
                     "observed": True, "unobserved_since": None}
    # A gap was open at that heartbeat: never later than the gap.
    earlier = T0 - timedelta(minutes=4)
    assert gr.restart_bound(heartbeat(T0, observed=False, since=earlier), "CRYPTO", now)[0] == (
        earlier)
    # Unknown, missing, unreadable or impossible evidence: no bound (the window starts at the
    # setup's admission).
    assert gr.restart_bound(heartbeat(T0, observed=False), "CRYPTO", now)[0] is None
    assert gr.restart_bound(heartbeat(T0), "US", now)[0] is None
    assert gr.restart_bound(None, "CRYPTO", now) == (
        None, {"basis": "NO_PREVIOUS_RUNTIME_HEARTBEAT"})
    older = {"event_seq": 3, "body": {"runtime_id": "old", "market_streams": {"CRYPTO": True}}}
    assert gr.restart_bound(older, "CRYPTO", now) == (None, {
        "basis": "PREVIOUS_HEARTBEAT_WITHOUT_OBSERVATION_RECORD", "heartbeat_event_seq": 3,
        "heartbeat_runtime_id": "old"})
    assert gr.restart_bound(heartbeat(now + timedelta(seconds=1)), "CRYPTO", now)[0] is None


def test_the_version_applies_to_crypto_trigger_v1_setups_only():
    v3 = {"market": "CRYPTO", "report_schema_version": "AGENT_RESEARCH_REPORT_V3"}
    assert gr.admission_fields(v3) == {"gap_resume_version": "CRYPTO_GAP_RESUME_V1"}
    assert gr.admission_fields({**v3, "market": "US_STOCKS"}) == {}
    assert gr.admission_fields({"market": "CRYPTO"}) == {}
    assert gr.applies({"gap_resume_version": "CRYPTO_GAP_RESUME_V1"})
    assert not gr.applies({"trigger_version": "CRYPTO_ALPACA_TRIGGER_V1"})
    assert not gr.applies(None)
    assert "gap_resume_version" in STATE_FIELDS and "gap_resume" in STATUS_FIELDS


def test_the_watchdog_raises_an_overdue_check_and_fails_closed():
    now = T0 + timedelta(hours=1)

    def section(oldest, count=1):
        return {"pending_count": count, "oldest_pending_since": oldest}

    assert GAP_RESUME_OVERDUE_SECONDS == gr.CHECK_OVERDUE_SECONDS == 300
    assert gap_resume_alarms(None, now) == []  # An app without the field.
    assert gap_resume_alarms(section(None, 0), now) == []
    assert gap_resume_alarms(section((now - timedelta(seconds=300)).isoformat()), now) == []
    for overdue in ((now - timedelta(seconds=301)).isoformat(),
                    (now + timedelta(seconds=1)).isoformat(), "not-a-time",
                    now.replace(tzinfo=None).isoformat()):
        assert gap_resume_alarms(section(overdue), now) == ["GAP_RESUME_CHECK_OVERDUE"]
    for broken in ("nope", {"oldest_pending_since": None}, section(None, 2),
                   {"pending_count": "1", "oldest_pending_since": None}):
        assert gap_resume_alarms(broken, now) == ["GAP_RESUME_STATUS_UNAVAILABLE"]
    status = {"gap_resume": section((now - timedelta(minutes=6)).isoformat())}
    assert "GAP_RESUME_CHECK_OVERDUE" in status_alarms(status, now, {
        "tick_max_age_seconds": 60, "reconciliation_max_age_seconds": 60,
        "research_max_age_seconds": 60})


class MarketData:
    """Alpaca crypto latest quotes and one-minute bars behind the real read-only market
    source (MockTransport); every request is recorded, a symbol in ``fail`` answers 503."""

    def __init__(self):
        self.quotes, self.bars, self.fail, self.requests = {}, {}, set(), []

    def minutes(self, symbol, first, count, low="100.40", high="100.70", lows=None):
        """``count`` one-minute bars from ``first``; ``lows`` maps a minute offset to its low."""
        rows = self.bars.setdefault(symbol, [])
        for i in range(count):
            at = first + timedelta(minutes=i)
            value = D(str((lows or {}).get(i, low)))
            top = max(value, D(str(high)))
            rows.append({"t": at.isoformat().replace("+00:00", "Z"), "o": str(top),
                         "h": str(top), "l": str(value), "c": str(top), "v": "2"})

    def bar_requests(self, symbol=None):
        return [p for path, p in self.requests
                if path == BARS and (symbol is None or p["symbols"] == symbol)]

    def handle(self, request):
        assert request.method == "GET" and request.url.host == "data.alpaca.markets"
        params = dict(request.url.params)
        self.requests.append((request.url.path, params))
        symbol = params["symbols"]
        if request.url.path == QUOTES:
            rows = {}
            if symbol in self.quotes:
                bid, ask, at = self.quotes[symbol]
                rows[symbol] = {"t": at.isoformat().replace("+00:00", "Z"), "bp": bid, "ap": ask}
            return httpx.Response(200, json={"quotes": rows})
        assert request.url.path == BARS and params["timeframe"] == "1Min"
        if symbol in self.fail:
            return httpx.Response(503, json={"message": "fixture outage"})
        start = datetime.fromisoformat(params["start"])
        end = datetime.fromisoformat(params["end"])
        rows = [r for r in self.bars.get(symbol, [])
                if start <= datetime.fromisoformat(r["t"].replace("Z", "+00:00")) <= end]
        return httpx.Response(200, json={"bars": {symbol: rows}, "next_page_token": None})


def test_window_bars_are_one_get_of_the_explicit_window_without_the_open_minute():
    data = MarketData()
    now = T0 + timedelta(minutes=10, seconds=40)
    source = AlpacaMarketSource(CREDENTIALS, SOURCE_POLICY, lambda: now,
                                transport=httpx.MockTransport(data.handle))
    data.minutes("AAA/USD", T0, 12)  # Twelve minutes; the window ends after the tenth.
    try:
        bars, issues = source.window_bars("CRYPTO", "AAA/USD", start=T0,
                                          end=T0 + timedelta(minutes=10))
        assert issues == () and len(bars) == 10
        assert bars[-1].end_at == T0 + timedelta(minutes=10)  # The bar at the end is open.
        [params] = data.bar_requests()
        assert params == {"symbols": "AAA/USD", "timeframe": "1Min", "start": T0.isoformat(),
                          "end": (T0 + timedelta(minutes=10)).isoformat(), "limit": "10000",
                          "sort": "asc"}
        data.fail.add("AAA/USD")
        failed, issues = source.window_bars("CRYPTO", "AAA/USD", start=T0,
                                            end=T0 + timedelta(minutes=10))
        assert failed == () and [i.code for i in issues] == ["SCAN_HTTP_503"]
        for bad in ({"market": "US"}, {"start": T0 + timedelta(seconds=1)},
                    {"end": T0}, {"start": T0.replace(tzinfo=None)}):
            arguments = {"market": "CRYPTO", "start": T0, "end": T0 + timedelta(minutes=2),
                         **bad}
            with pytest.raises(ValueError, match="UNSUPPORTED_WINDOW_BAR_SOURCE"):
                source.window_bars(arguments.pop("market"), "AAA/USD", **arguments)
    finally:
        source.close()


# --- The runtime: disposable PostgreSQL, fake venue, fake streams --------------------------------

class GapVenue(MaintenanceVenue):
    """The maintenance fake venue plus Alpaca-shaped FILL account activities (paginated like
    Alpaca's), so the REST fill backfill of a new trade-updates connection can read a fill the
    stream never delivered."""

    def __init__(self):
        super().__init__()
        self.activities = []

    def fill(self, broker_id, qty, *, price="100", fee_qty="0"):
        update = super().fill(broker_id, qty, price=price, fee_qty=fee_qty)
        order = self.orders[broker_id]
        self.activities.append({
            "activity_type": "FILL", "id": f"{len(self.activities):020d}-{broker_id}",
            "order_id": broker_id, "symbol": order["symbol"].replace("/", ""),
            "side": order["side"], "type": update["event"], "qty": str(qty), "price": price,
            "cum_qty": order["filled_qty"],
            "leaves_qty": str(D(order["qty"]) - D(order["filled_qty"])),
            "order_status": order["status"], "transaction_time": self.now.isoformat(),
        })
        return update

    def handle(self, request):
        params = dict(request.url.params)
        if request.url.path != "/v2/account/activities" or params.get("activity_types") != "FILL":
            return super().handle(request)
        after = datetime.fromisoformat(params["after"].replace("Z", "+00:00"))
        rows = [a for a in self.activities
                if datetime.fromisoformat(a["transaction_time"]) > after]
        if params.get("page_token"):
            rows = rows[[a["id"] for a in rows].index(params["page_token"]) + 1:]
        return httpx.Response(200, json=rows[: int(params["page_size"])])


class Process:
    """One app process as ``build_runtime_from_env`` wires it: its own executor lease (a
    PostgreSQL session), a lease-fenced authorization gate, the controller, the read-only
    market source and the runtime with the maintenance component."""

    def __init__(self, er, venue, data, *, maintenance=True):
        self.venue = venue
        self.risk = RiskRepository(
            er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
        self.reviews = JevStore(er.database_url.replace("user=catalyst_app", "user=catalyst_jev"))
        self.lease = AccountExecutorLease(self.risk, lambda: IDENTITY)
        self.broker = ManagedPaperBroker(CREDENTIALS, FencedAuthorizationGate(
            ManagedAuthorizationGate(self.risk, clock=lambda: venue.now), self.lease
        ), transport=httpx.MockTransport(venue.handle))
        self.engine = ManagedExecution(
            self.risk, self.broker, policy=engineering_execution_policy(),
            clock=lambda: venue.now, review_store=self.reviews,
        )
        self.source = AlpacaMarketSource(CREDENTIALS, SOURCE_POLICY, lambda: venue.now,
                                         transport=httpx.MockTransport(data.handle))
        self.mx = (self.engine, venue, self.reviews)
        self.kit = maintainer(self.mx) if maintenance else None
        self.run = ManagedRuntime(
            self.engine, Research(), self.source, CREDENTIALS, engineering_runtime_policy(),
            clock=lambda: venue.now, reviewer_heartbeat=lambda: True,
            executor_lease=self.lease,
            maintenance=self.kit.maintenance if self.kit is not None else None,
        )

    def die(self):
        """The process ends abruptly: no RUNTIME_STOPPED, no clean shutdown; its database
        session goes with it, which releases the executor lease."""
        self.lease.connection.close()

    def close(self):
        for resource in (self.broker, self.source):
            resource.close()


class NoThread:
    """``start()`` launches the worker loops; these tests drive every loop by hand instead."""

    def __init__(self, target, args, daemon):
        self.target = target

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class TradeSocket:
    """The paper trade-updates stream: authorized, listening, then the given updates."""

    def __init__(self, run, updates=()):
        self.run, self.sent = run, []
        self.frames = [AUTHORIZED, LISTENING,
                       *({"stream": "trade_updates", "data": u} for u in updates)]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, timeout):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        self.run.stop_event.set()  # Ends the session read loop; the tests clear it again.
        raise TimeoutError


class MarketSocket:
    """Alpaca's crypto market stream: connected, authenticated, an acknowledgement of exactly
    the subscription the runtime asked for, then the given messages."""

    def __init__(self, run, messages=()):
        self.run, self.messages, self.sent = run, list(messages), []
        self.greetings = [[{"T": "success", "msg": "connected"}],
                          [{"T": "success", "msg": "authenticated"}]]
        self.subscribed, self.acknowledged = set(), 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def close(self):
        pass

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("action") == "subscribe":
            self.subscribed |= set(message["trades"])
        elif message.get("action") == "unsubscribe":
            self.subscribed -= set(message["trades"])

    def recv(self, timeout):
        if self.greetings:
            return json.dumps(self.greetings.pop(0))
        changes = sum(m.get("action") in {"subscribe", "unsubscribe"} for m in self.sent)
        if changes != self.acknowledged:
            self.acknowledged = changes
            symbols = sorted(self.subscribed)
            return json.dumps([{"T": "subscription", "trades": symbols, "quotes": symbols}])
        if self.messages:
            return json.dumps([self.messages.pop(0)])
        self.run.stop_event.set()
        raise TimeoutError


def start(proc, monkeypatch):
    """``ManagedRuntime.start()`` exactly as the entry point calls it (lease, both markets'
    startup gap, holds or revocations, reconciliation, RUNTIME_STARTED), threads left out."""
    with monkeypatch.context() as patch:
        patch.setattr(managed_runtime.threading, "Thread", NoThread)
        proc.run.start()


def trade_stream(run, *updates):
    """A new trade-updates connection: its REST fill backfill, then its reconciliation."""
    run.connector = lambda *_, **__: TradeSocket(run, updates)
    run.stream_session()
    run.stop_event.clear()  # The connection stays open; its read loop would keep going.


def market_stream(run, *messages):
    """A new crypto market-stream connection with every desired symbol acknowledged."""
    socket = MarketSocket(run, messages)
    run.connector = lambda *_, **__: socket
    run.market_stream_session("CRYPTO")
    run.stop_event.clear()
    return socket


def quote(symbol, bid, ask, at):
    return {"T": "q", "S": symbol, "bp": bid, "ap": ask, "t": at.isoformat()}


def trade(symbol, price, at, trade_id):
    return {"T": "t", "S": symbol, "p": price, "i": trade_id, "t": at.isoformat()}


def drop_trade_stream(run):
    """What ``_stream_loop`` does when the trade-updates connection ends."""
    with run.lock:
        run.connected, run.socket, run.reconciled_at = False, None, None
        run.execution.reconciled_at = None
    run._event("RUNTIME_STREAM_DISCONNECTED", {"reason": "PAPER_STREAM_UNAVAILABLE"})


def rows(engine, kind, setup_id=None):
    with engine.repo.connect() as conn:
        found = conn.execute(
            """SELECT * FROM lab.managed_events WHERE kind=%s
            AND (%s::uuid IS NULL OR setup_id=%s::uuid) ORDER BY event_seq""",
            (kind, setup_id, setup_id),
        ).fetchall()
    return found


def bodies(engine, kind, setup_id=None):
    return [row["body"] for row in rows(engine, kind, setup_id)]


def state(engine, sid):
    return engine._load(sid)[1]


def sent(venue, method="POST"):
    """Every order the venue accepted or refused for ``method``, as (symbol, type, side)."""
    result = []
    for m, path, content in venue.calls:
        if m != method:
            continue
        if method == "POST":
            payload = json.loads(content)
            result.append((payload["symbol"], payload["type"], payload["side"],
                           payload["client_order_id"]))
        else:
            result.append(path)
    return result


def admitted(mx, symbols):
    """Report-V3 picks through intake, the mock selection Jev and publication, admitted on a
    live pullback quote (entry 100, max entry 100.10, stop 95, target 111)."""
    from tests.maintenance_fixtures import admit_many

    return dict(zip(symbols, admit_many(mx, symbols), strict=True))


def trigger_entry(mx, sid):
    """A print at the entry trigger with the ask at the max entry: a limit buy is sent."""
    from tests.maintenance_fixtures import trigger

    trigger(mx, sid)
    return next(o for o in mx[1].orders.values()
                if o["symbol"] == mx[0]._load(sid)[0]["symbol"] and o["side"] == "buy")


def tick_until_resolved(run, venue, limit=20):
    for _ in range(limit):
        if not run.gap_checks:
            return
        run.execution_once()
        venue.now += timedelta(seconds=1)
    raise AssertionError("GAP_CHECKS_STILL_PENDING")


@pytest.fixture
def lab(er):
    """One fake venue and one fake market-data host; processes come and go on one ledger."""
    venue, data, started = GapVenue(), MarketData(), []

    def process(**options):
        started.append(Process(er, venue, data, **options))
        return started[-1]

    yield SimpleNamespace(venue=venue, data=data, process=process)
    for proc in started:
        proc.lease.close()
        proc.close()


HELD = ("RES/USD", "ENT/USD", "STP/USD", "BAD/USD")  # Resumes, entry reached, stop, no bars.


def script_gap_bars(data, first, count=12):
    """One-minute bars from ``first``: RES stays above the entry, ENT trades at 99.97 (the
    entry is 100), STP at 94.95 (the stop is 95) and BAD's bar request answers 503."""
    data.minutes("RES/USD", first, count, low="100.40")
    data.minutes("ENT/USD", first, count, low="100.40", lows={1: "99.97"})
    data.minutes("STP/USD", first, count, low="100.40", lows={1: "94.95"})
    data.fail.add("BAD/USD")


def session_up(proc, *, touching=()):
    """Trade updates connected (backfill, reconciliation), the crypto stream acknowledged with
    a quote for every symbol (the ask at the entry for ``touching``), research healthy."""
    run, venue = proc.run, proc.venue
    trade_stream(run)
    at = venue.now
    symbols = sorted(run._desired_symbols("CRYPTO"))
    messages = []
    for symbol in symbols:
        if symbol == "BTC/USD":
            messages.append(quote(symbol, "60000", "60010", at))
        elif symbol == "SOL/USD":
            messages.append(quote(symbol, "101", "101.03", at))
        elif symbol in touching:
            messages.append(quote(symbol, "99.95", "99.99", at))  # A touch of the entry 100.
        else:
            messages.append(quote(symbol, "100.49", "100.51", at))
    market_stream(run, *messages)
    run.heartbeat_once()
    return symbols


def fills(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT side,qty,price,source FROM lab.managed_fills WHERE setup_id=%s
            ORDER BY filled_at,event_seq""", (sid,)).fetchall()


def halts(engine):
    with engine.repo.connect() as conn:
        return [r["reason"] for r in conn.execute(
            "SELECT reason FROM lab.execution_halts").fetchall()]


def entry_decisions(engine, sid):
    with engine.repo.connect() as conn:
        return conn.execute(
            """SELECT action,outcome,reason FROM lab.managed_risk_decisions
            WHERE setup_id=%s AND action='ENTRY' ORDER BY event_seq""", (sid,)).fetchall()


def orders_of(venue, symbols):
    return [o for o in sent(venue) if o[0] in set(symbols)]


def test_a_stream_gap_holds_the_setups_until_one_bar_check_resumes_or_ends_each(
    lab, monkeypatch,
):
    from tests.maintenance_fixtures import open_trade, stop_orders

    venue, data = lab.venue, lab.data
    proc = lab.process()
    start(proc, monkeypatch)
    engine, run = proc.engine, proc.run
    sol = open_trade(proc.mx, "SOL/USD")  # Maintained, its stop-limit resting at the venue.
    [sol_stop] = stop_orders(proc.mx, "SOL/USD")
    setups = admitted(proc.mx, [*HELD, "FIL/USD"])
    old = engine.admit(packet(proc.mx, "OLD/USD"))  # Report V2: today's trigger and gap rule.
    entry = trigger_entry(proc.mx, setups["FIL/USD"])  # Its limit buy works at the venue.
    session_up(proc)
    run.execution_once()
    assert run.ready() and run.error is None
    before = list(sent(venue))
    sol_orders = orders_of(venue, ["SOL/USD"])

    # The crypto stream drops, and trade updates with it.
    venue.now += timedelta(seconds=2)
    gap_at = venue.now
    drop_trade_stream(run)
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    held = {str(setups[s]) for s in HELD}
    assert set(run.gap_checks) == held and "CRYPTO" not in run.market_gaps
    for symbol in HELD:
        [pending] = bodies(engine, "GAP_RESUME_PENDING", setups[symbol])
        assert (pending["window_start"], pending["gap_started_at"], pending["gap_reason"]) == (
            gap_at.isoformat(), gap_at.isoformat(), "MARKET_STREAM_DISCONNECTED_OR_GAP")
        assert pending["basis"] == {"basis": "RUNTIME_MARKET_GAP"}
        assert pending["version"] == "CRYPTO_GAP_RESUME_V1"
        assert state(engine, setups[symbol])["state"] == "WATCHING"
    # Every other setup: today's revocation, byte for byte.
    assert bodies(engine, "REVOKE", old) == [{"reason": "DATA_FEED_FAILURE"}]
    assert (state(engine, old)["state"], state(engine, old)["revocation_reason"]) == (
        "INVALIDATED", "DATA_FEED_FAILURE")
    status = run.status()["gap_resume"]
    assert status["pending_count"] == 4 and status["oldest_pending_since"] == gap_at.isoformat()
    assert status["markets"]["CRYPTO"] == {"observed": False,
                                           "unobserved_since": gap_at.isoformat()}
    assert {p["setup_id"] for p in status["pending"]} == held

    # While down: FIL's entry fills; protection keeps running and backfills it.
    venue.now += timedelta(seconds=20)
    venue.fill(entry["id"], entry["qty"])
    for _ in range(3):
        venue.now += timedelta(seconds=1)
        run.execution_once()
    fil = setups["FIL/USD"]
    assert state(engine, fil)["state"] == "OPEN"
    [fill] = bodies(engine, "BROKER_REST_BACKFILL", fil)[:1]
    assert fill["source"] == "ALPACA_REST_FILL_ACTIVITY"
    assert [o[1] for o in orders_of(venue, ["FIL/USD"])] == ["limit", "stop_limit"]
    assert sol_stop["status"] == "new" and orders_of(venue, ["SOL/USD"]) == sol_orders

    # The streams come back; ENT's ask is at its entry now and a print touches it too.
    script_gap_bars(data, minute(gap_at))
    venue.now += timedelta(seconds=15)
    session_up(proc, touching={"ENT/USD"})
    back = venue.now
    run.market_message("CRYPTO", trade("ENT/USD", "99.98", back, 9001))
    run.execution_once()  # Held: nothing is evaluated for them, the touch buys nothing.
    assert orders_of(venue, HELD) == [] and set(run.gap_checks) == held
    assert [b["reason"] for b in bodies(engine, "MARKET_PRINT_CONSUMED", setups["ENT/USD"])] == [
        "GAP_CHECK_PENDING"]
    assert data.bar_requests() == []  # Not due before the minute of the return has settled.
    venue.now = gr.due_at(back)
    run.reconcile_once()  # The 30-second reconciliation loop keeps it fresh.
    tick_until_resolved(run, venue)

    resumed = setups["RES/USD"]
    [evidence] = bodies(engine, "GAP_RESUMED", resumed)
    assert evidence["decision"] == "RESUMED" and evidence["window"]["start"] == gap_at.isoformat()
    assert evidence["window"]["bars_start"] == minute(gap_at).isoformat()
    assert evidence["bars"]["lowest_low"] == "100.40" and evidence["bars"]["count"] >= 2
    assert state(engine, resumed)["state"] == "WATCHING"
    reached = state(engine, setups["ENT/USD"])
    assert (reached["state"], reached["revocation_reason"]) == (
        "INVALIDATED", "ENTRY_REACHED_DURING_GAP")
    [revoked] = bodies(engine, "REVOKE", setups["ENT/USD"])
    assert revoked["reason"] == "ENTRY_REACHED_DURING_GAP"
    assert revoked["gap_resume"]["bars"]["lowest_low"] == "99.97"
    assert revoked["gap_resume"]["prints"]["lowest"] == "99.98"
    stopped = state(engine, setups["STP/USD"])
    assert (stopped["state"], stopped["reason"]) == ("INVALIDATED", "STOP_TRADED_DURING_GAP")
    assert stopped["gap_resume"]["lowest"] == "94.95"
    [failed] = bodies(engine, "REVOKE", setups["BAD/USD"])
    assert failed["reason"] == "DATA_FEED_FAILURE"
    assert failed["gap_resume"]["bars"]["issues"] == ["SCAN_HTTP_503"]
    for symbol in HELD:  # Each setup was checked once, over its whole window.
        [request] = data.bar_requests(symbol)
        assert request["start"] == minute(gap_at).isoformat()
    assert run.status()["gap_resume"]["pending_count"] == 0

    venue.now += timedelta(seconds=1)
    run.reconcile_once()
    run.market_message("CRYPTO", quote("RES/USD", "99.95", "99.99", venue.now))
    run.execution_once()
    # Resumed means WATCHING under its trigger again: a live touch is confirmed and goes to the
    # entry's risk authorization (here the fixture policy's 2% crypto cap refuses a third trade
    # while SOL and FIL are open).
    [confirmed] = bodies(engine, "TRIGGER_CONFIRMED", resumed)
    assert confirmed["crypto_trigger"]["touch"] == "QUOTE"
    assert [(d["action"], d["reason"]) for d in entry_decisions(engine, resumed)] == [
        ("ENTRY", "MAX_OPEN_PLANNED_RISK")]
    assert orders_of(venue, [*HELD, "OLD/USD"]) == []  # Never a late entry.
    placed = [o[3] for o in sent(venue)]
    assert len(placed) == len(set(placed))  # No order was ever sent twice.
    assert sent(venue)[: len(before)] == before
    assert state(engine, sol)["state"] == "OPEN" and sol_stop["status"] == "new"
    assert [(f["source"], f["qty"]) for f in fills(engine, fil)] == [
        ("ALPACA_PAPER_REST_BACKFILL", D(entry["qty"]))]  # Once, although backfilled twice.
    assert halts(engine) == [] and run.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def heartbeats_of(engine, run):
    return [r for r in rows(engine, "RUNTIME_HEARTBEAT")
            if r["body"]["runtime_id"] == run.runtime_id]


def test_a_restart_backfills_keeps_protection_and_checks_each_pending_setup_once(
    lab, monkeypatch,
):
    from tests.maintenance_fixtures import open_trade, stop_orders

    venue, data = lab.venue, lab.data
    first = lab.process()
    start(first, monkeypatch)
    engine = first.engine
    sol = open_trade(first.mx, "SOL/USD")  # Maintained; its stop-limit rests at the venue.
    [sol_stop] = stop_orders(first.mx, "SOL/USD")
    sol_levels = {k: state(engine, sol)[k] for k in ("stop", "target")}
    setups = admitted(first.mx, [*HELD, "FIL/USD"])
    old = engine.admit(packet(first.mx, "OLD/USD"))  # Report V2: today's gap rule.
    entry = trigger_entry(first.mx, setups["FIL/USD"])  # Working at the venue.
    session_up(first)
    first.run.execution_once()
    assert first.run.ready() and first.run.error is None
    venue.now += timedelta(seconds=3)
    first.run.heartbeat_once()  # The last one it writes.
    last = heartbeats_of(engine, first.run)[-1]["body"]["gap_resume"]
    observed_at = datetime.fromisoformat(last["as_of"])
    assert last["markets"]["CRYPTO"] == {"observed": True, "unobserved_since": None}
    venue.now += timedelta(seconds=2)
    first.die()  # Abruptly, mid-session: threads gone, no RUNTIME_STOPPED, lease released.
    before = list(sent(venue))

    # Nothing runs: FIL's entry fills at the venue; prices move (the bars).
    venue.now += timedelta(seconds=30)
    venue.fill(entry["id"], entry["qty"])
    venue.now += timedelta(minutes=2)
    script_gap_bars(data, minute(observed_at))

    second = lab.process()  # The supervisor's new process: same ledger, same venue.
    start(second, monkeypatch)
    run = second.run
    held = {str(setups[s]) for s in HELD}
    assert set(run.gap_checks) == held
    for symbol in HELD:
        [pending] = bodies(engine, "GAP_RESUME_PENDING", setups[symbol])
        assert pending["window_start"] == observed_at.isoformat()  # The last observation.
        assert pending["gap_reason"] == "RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION"
        assert pending["runtime_id"] == run.runtime_id
        assert pending["basis"] == {
            "basis": "PREVIOUS_RUNTIME_HEARTBEAT",
            "heartbeat_event_seq": heartbeats_of(engine, first.run)[-1]["event_seq"],
            "heartbeat_runtime_id": first.run.runtime_id, "as_of": observed_at.isoformat(),
            "observed": True, "unobserved_since": None}
    assert bodies(engine, "REVOKE", old) == [{"reason": "DATA_FEED_FAILURE"}]  # As today.
    assert rows(engine, "RUNTIME_STOPPED") == []

    # Its loops start together; the protection tick may well run before any stream is up. It
    # reads FIL's missed fill from REST and protects it once; SOL keeps its resting stop.
    run.execution_once()
    fil = setups["FIL/USD"]
    assert state(engine, fil)["state"] == "OPEN" and halts(engine) == []
    assert [f["source"] for f in fills(engine, fil)] == ["ALPACA_PAPER_REST_BACKFILL"]
    [fil_stop] = stop_orders(second.mx, "FIL/USD")
    assert stop_orders(second.mx, "SOL/USD") == [sol_stop] and sol_stop["status"] == "new"
    assert {k: state(engine, sol)[k] for k in ("stop", "target")} == sol_levels

    # Trade updates connect (their backfill finds nothing new), then the crypto stream with
    # ENT's ask at its entry: held setups are not evaluated, so nothing is bought.
    session_up(second, touching={"ENT/USD"})
    assert [f["source"] for f in fills(engine, fil)] == ["ALPACA_PAPER_REST_BACKFILL"]
    back = venue.now
    run.execution_once()
    assert orders_of(venue, HELD) == [] and data.bar_requests() == []
    venue.now = gr.due_at(back)
    run.reconcile_once()
    tick_until_resolved(run, venue)
    [evidence] = bodies(engine, "GAP_RESUMED", setups["RES/USD"])
    assert evidence["window"]["start"] == observed_at.isoformat()
    assert evidence["gap"]["reason"] == "RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION"
    assert state(engine, setups["RES/USD"])["state"] == "WATCHING"
    assert state(engine, setups["ENT/USD"])["revocation_reason"] == "ENTRY_REACHED_DURING_GAP"
    assert state(engine, setups["STP/USD"])["reason"] == "STOP_TRADED_DURING_GAP"
    assert state(engine, setups["BAD/USD"])["revocation_reason"] == "DATA_FEED_FAILURE"
    for symbol in HELD:
        [request] = data.bar_requests(symbol)
        assert request["start"] == minute(observed_at).isoformat()

    # CRYPTO_MAINTENANCE_V1 continues in the new process: +1R is reviewed, the stop raised,
    # and the protection tick replaces the resting stop-limit once (a claimed PATCH).
    second.kit.jev.answer("RAISE_STOP", stop="first")
    venue.now += timedelta(seconds=1)
    run.market_message("CRYPTO", quote("SOL/USD", "106", "106.03", venue.now))
    asyncio.run(run._position_pass())
    [decision] = bodies(engine, "MAINTENANCE_DECISION", sol)
    assert decision["outcome"] == "APPLIED"
    run.execution_once()
    assert sol_stop["status"] == "replaced"
    [raised] = stop_orders(second.mx, "SOL/USD")
    assert D(raised["stop_price"]) == D(decision["stop"]["new"])
    run.execution_once()
    assert [b["path"] for b in bodies(engine, "STOP_REPLACED", sol)] == ["PATCH_REPLACE"]
    assert sent(venue, "PATCH") == ["/v2/orders/" + sol_stop["id"]]

    # Nothing was ever sent twice: one entry each for SOL and FIL, one stop-limit each.
    assert sent(venue)[: len(before)] == before
    assert [o[1:3] for o in orders_of(venue, ["SOL/USD"])] == [("limit", "buy"),
                                                              ("stop_limit", "sell")]
    assert [o[1:3] for o in orders_of(venue, ["FIL/USD"])] == [("limit", "buy"),
                                                              ("stop_limit", "sell")]
    assert orders_of(venue, [*HELD, "OLD/USD"]) == []
    placed = [o[3] for o in sent(venue)]
    assert len(placed) == len(set(placed)) and fil_stop["status"] == "new"
    assert halts(engine) == [] and run.error is None
    assert verify_events(engine.repo.export_events())["valid"]


def test_a_restart_window_is_never_later_than_the_gap_the_last_heartbeat_reported(
    lab, monkeypatch,
):
    venue, data = lab.venue, lab.data
    first = lab.process(maintenance=False)
    start(first, monkeypatch)
    engine = first.engine
    sid = admitted(first.mx, ["RES/USD"])["RES/USD"]
    session_up(first)
    first.run.execution_once()
    venue.now += timedelta(seconds=5)
    gap_at = venue.now
    first.run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    venue.now += timedelta(seconds=2)
    first.run.heartbeat_once()  # Before any tick held the setup: the gap is still open.
    last = heartbeats_of(engine, first.run)[-1]["body"]["gap_resume"]
    assert last["markets"]["CRYPTO"] == {"observed": False,
                                         "unobserved_since": gap_at.isoformat()}
    assert bodies(engine, "GAP_RESUME_PENDING", sid) == []
    first.die()
    venue.now += timedelta(minutes=1)
    data.minutes("RES/USD", minute(gap_at), 5, low="100.40")
    second = lab.process(maintenance=False)
    start(second, monkeypatch)
    [pending] = bodies(engine, "GAP_RESUME_PENDING", sid)
    assert pending["window_start"] == gap_at.isoformat()  # Not the later heartbeat.
    assert pending["basis"]["as_of"] == (gap_at + timedelta(seconds=2)).isoformat()
    session_up(second)
    back = venue.now
    second.run.execution_once()
    venue.now = gr.due_at(back)
    second.run.reconcile_once()
    tick_until_resolved(second.run, venue)
    [resumed] = bodies(engine, "GAP_RESUMED", sid)
    assert resumed["window"]["bars_start"] == minute(gap_at).isoformat()


def test_an_open_earlier_hold_and_an_unevaluated_print_bound_the_restart_window(
    lab, monkeypatch,
):
    venue, data = lab.venue, lab.data
    first = lab.process(maintenance=False)
    start(first, monkeypatch)
    engine, run = first.engine, first.run
    held = admitted(first.mx, ["HLD/USD"])["HLD/USD"]
    session_up(first)
    run.execution_once()
    venue.now += timedelta(seconds=5)
    gap_at = venue.now
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()  # Held, recorded.
    venue.now += timedelta(seconds=10)
    printed_setup = admitted(first.mx, ["PRT/USD"])["PRT/USD"]  # After the gap: never held.
    session_up(first)
    run.execution_once()  # HLD's stream is back; its check is not due yet.
    assert set(run.gap_checks) == {str(held)}
    venue.now += timedelta(seconds=4)
    printed_at = venue.now
    run.market_message("CRYPTO", trade("PRT/USD", "99.99", printed_at, 777))  # Queued...
    venue.now += timedelta(seconds=1)
    run.heartbeat_once()  # ...but no tick evaluates it: the process dies first.
    observed = datetime.fromisoformat(heartbeats_of(engine, run)[-1]["body"]["gap_resume"]["as_of"])
    first.die()
    venue.now += timedelta(minutes=1)
    for symbol in ("HLD/USD", "PRT/USD"):  # No bar reaches either entry.
        data.minutes(symbol, minute(gap_at), 6, low="100.40")

    second = lab.process(maintenance=False)
    start(second, monkeypatch)
    [_, reheld] = bodies(engine, "GAP_RESUME_PENDING", held)
    assert reheld["window_start"] == gap_at.isoformat() < observed.isoformat()
    assert reheld["open_gap_window_start"] == gap_at.isoformat()
    [pending] = bodies(engine, "GAP_RESUME_PENDING", printed_setup)
    assert pending["window_start"] == printed_at.isoformat() < observed.isoformat()
    assert pending["unconsumed_print_at"] == printed_at.isoformat()
    second.run.execution_once()  # The old print is consumed unevaluated, never aged out.
    assert [b["reason"] for b in bodies(engine, "MARKET_PRINT_CONSUMED", printed_setup)] == [
        "GAP_CHECK_PENDING"]
    session_up(second)
    back = venue.now
    second.run.execution_once()
    venue.now = gr.due_at(back)
    second.run.reconcile_once()
    tick_until_resolved(second.run, venue)
    assert bodies(engine, "GAP_RESUMED", held)[0]["window"]["start"] == gap_at.isoformat()
    [revoked] = bodies(engine, "REVOKE", printed_setup)  # The print itself decided it.
    assert revoked["reason"] == "ENTRY_REACHED_DURING_GAP"
    assert revoked["gap_resume"]["bars"]["lowest_low"] == "100.40"
    assert revoked["gap_resume"]["prints"]["lowest"] == "99.99"
    assert orders_of(venue, ["HLD/USD", "PRT/USD"]) == []


def test_every_other_setup_keeps_todays_revocation_on_a_gap_and_at_restart(lab, monkeypatch):
    venue = lab.venue
    first = lab.process(maintenance=False)
    start(first, monkeypatch)
    engine, run = first.engine, first.run

    def others(tag):
        """A report-V2 crypto setup, a US stock and a report-V3 crypto setup admitted before
        this version (its state has the trigger version but no ``gap_resume_version``)."""
        with monkeypatch.context() as patch:
            patch.setattr(gr, "admission_fields", lambda packet: {})
            earlier = admitted(first.mx, [f"PR{tag}/USD"])[f"PR{tag}/USD"]
        return [earlier, engine.admit(packet(first.mx, f"OL{tag}/USD")),
                engine.admit(packet(first.mx, f"SP{tag}"))]

    in_process = others("A")
    current = admitted(first.mx, ["CUR/USD"])["CUR/USD"]
    assert state(engine, in_process[0])["trigger_version"] == "CRYPTO_ALPACA_TRIGGER_V1"
    assert all("gap_resume_version" not in state(engine, sid) for sid in in_process)
    assert state(engine, current)["gap_resume_version"] == "CRYPTO_GAP_RESUME_V1"
    session_up(first)
    run.execution_once()
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.market_gap("US", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    at_restart = others("B")
    first.die()
    venue.now += timedelta(seconds=30)
    second = lab.process(maintenance=False)
    start(second, monkeypatch)
    for sid in [*in_process, *at_restart]:
        assert bodies(engine, "REVOKE", sid) == [{"reason": "DATA_FEED_FAILURE"}]
        revoked = state(engine, sid)
        assert (revoked["state"], revoked["revoked"], revoked["revocation_reason"]) == (
            "INVALIDATED", True, "DATA_FEED_FAILURE")
        assert "gap_resume" not in revoked and bodies(engine, "GAP_RESUME_PENDING", sid) == []
    assert set(second.run.gap_checks) == {str(current)}
    first_hold, second_hold = bodies(engine, "GAP_RESUME_PENDING", current)
    assert second_hold["window_start"] == first_hold["window_start"]  # Still the first gap.
    assert second_hold["open_gap_window_start"] == first_hold["window_start"]


def test_status_and_the_watchdog_report_held_setups_and_an_overdue_check(lab, monkeypatch):
    from fastapi.testclient import TestClient

    from catalyst_lab.managed_app import AppSettings, create_application
    from catalyst_lab.managed_classification import CLASSIFICATION_POLICY

    venue, data = lab.venue, lab.data
    proc = lab.process(maintenance=False)
    start(proc, monkeypatch)
    engine, run = proc.engine, proc.run
    sid = admitted(proc.mx, ["RES/USD"])["RES/USD"]
    session_up(proc)
    run.execution_once()
    token = "fixture-gap-resume-status-token-not-used-outside-tests"
    run.research.repo = engine.repo  # The app checks the cycle shares the runtime's ledger.
    client = TestClient(create_application(run, AppSettings(
        8799, token, 300, (), CLASSIFICATION_POLICY, ())))

    def http_status():
        response = client.get("/api/v1/lab/status", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        return response.json()

    watchdog = {"tick_max_age_seconds": 3600, "reconciliation_max_age_seconds": 3600,
                "research_max_age_seconds": 3600}
    quiet = http_status()["gap_resume"]
    assert (quiet["pending_count"], quiet["pending"], quiet["oldest_pending_since"]) == (
        0, [], None)
    venue.now += timedelta(seconds=1)
    gap_at = venue.now
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    status = http_status()
    section = status["gap_resume"]
    assert section["pending_count"] == 1 and section["oldest_pending_since"] == gap_at.isoformat()
    [item] = section["pending"]
    assert (item["setup_id"], item["symbol"], item["window_start"], item["due_at"]) == (
        str(sid), "RES/USD", gap_at.isoformat(), None)
    assert section["overdue_after_seconds"] == 300
    assert "GAP_RESUME_CHECK_OVERDUE" not in status_alarms(
        status, gap_at + timedelta(seconds=300), watchdog)
    assert "GAP_RESUME_CHECK_OVERDUE" in status_alarms(
        status, gap_at + timedelta(seconds=301), watchdog)
    # The heartbeat records the section; its instant alone is not a change.
    run.heartbeat_once()
    written = len(heartbeats_of(engine, run))
    venue.now += timedelta(seconds=5)
    run.heartbeat_once()
    assert len(heartbeats_of(engine, run)) == written
    assert heartbeats_of(engine, run)[-1]["body"]["gap_resume"]["pending_count"] == 1
    data.minutes("RES/USD", minute(gap_at), 5, low="100.40")
    session_up(proc)
    back = venue.now
    run.execution_once()
    assert http_status()["gap_resume"]["pending"][0]["due_at"] == gr.due_at(back).isoformat()
    venue.now = gr.due_at(back)
    run.reconcile_once()
    tick_until_resolved(run, venue)
    status = http_status()
    assert status["gap_resume"]["pending_count"] == 0
    assert not {"GAP_RESUME_CHECK_OVERDUE", "GAP_RESUME_STATUS_UNAVAILABLE"} & set(
        status_alarms(status, venue.now + timedelta(hours=1), watchdog))


def test_a_setup_whose_window_ends_during_the_gap_expires_as_today(lab, monkeypatch):
    venue, data = lab.venue, lab.data
    proc = lab.process(maintenance=False)
    start(proc, monkeypatch)
    engine, run = proc.engine, proc.run
    sid = admitted(proc.mx, ["RES/USD"])["RES/USD"]
    session_up(proc)
    run.execution_once()
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    assert set(run.gap_checks) == {str(sid)}
    venue.now = engine._load(sid)[0]["expires_at"] + timedelta(seconds=1)
    run.execution_once()
    expired = state(engine, sid)
    assert (expired["state"], expired["reason"]) == ("EXPIRED_UNTRIGGERED", "ENTRY_DEADLINE")
    run.execution_once()
    assert run.gap_checks == {} and data.bar_requests() == []
    assert bodies(engine, "GAP_RESUMED", sid) == [] and bodies(engine, "REVOKE", sid) == []


def test_a_gap_during_the_bar_read_discards_the_check_then_a_resumed_setup_enters(
    lab, monkeypatch,
):
    venue, data = lab.venue, lab.data
    proc = lab.process(maintenance=False)
    start(proc, monkeypatch)
    engine, run = proc.engine, proc.run
    sid = admitted(proc.mx, ["RES/USD"])["RES/USD"]
    session_up(proc)
    run.execution_once()
    gap_at = venue.now
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    data.minutes("RES/USD", minute(gap_at), 10, low="100.40")
    session_up(proc)
    back = venue.now
    run.execution_once()
    venue.now = gr.due_at(back)
    run.reconcile_once()
    read = proc.source.window_bars

    def read_across_a_gap(*args, **kwargs):
        result = read(*args, **kwargs)
        run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")  # While it read.
        return result

    monkeypatch.setattr(proc.source, "window_bars", read_across_a_gap)
    run.execution_once()
    assert len(data.bar_requests()) == 1 and bodies(engine, "GAP_RESUMED", sid) == []
    assert run.gap_checks[str(sid)].stream_back_at is None  # Waits for the stream again.
    monkeypatch.setattr(proc.source, "window_bars", read)
    run.execution_once()  # The new gap: still held, one record per hold.
    assert len(bodies(engine, "GAP_RESUME_PENDING", sid)) == 1
    session_up(proc)
    again = venue.now
    run.execution_once()
    venue.now = gr.due_at(again)
    run.reconcile_once()
    tick_until_resolved(run, venue)
    [resumed] = bodies(engine, "GAP_RESUMED", sid)
    assert resumed["window"]["start"] == gap_at.isoformat() and len(data.bar_requests()) == 2
    # WATCHING again under its trigger, with room in the account: a live touch enters.
    venue.now += timedelta(seconds=1)
    run.reconcile_once()
    run.market_message("CRYPTO", quote("RES/USD", "99.95", "99.99", venue.now))
    run.execution_once()
    [(symbol, kind, side, _)] = orders_of(venue, ["RES/USD"])
    assert (kind, side) == ("limit", "buy")
    assert [d["outcome"] for d in entry_decisions(engine, sid)] == ["APPROVED"]
    assert verify_events(engine.repo.export_events())["valid"]


def test_held_checks_share_the_ticks_one_market_data_read(lab, monkeypatch):
    venue, data = lab.venue, lab.data
    proc = lab.process(maintenance=False)
    start(proc, monkeypatch)
    engine, run = proc.engine, proc.run
    symbols = ["AAA/USD", "BBB/USD", "CCC/USD"]
    setups = admitted(proc.mx, symbols)
    session_up(proc)
    run.execution_once()
    gap_at = venue.now
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    for symbol in symbols:
        data.minutes(symbol, minute(gap_at), 5, low="100.40")
    session_up(proc)
    back = venue.now
    run.execution_once()
    venue.now = gr.due_at(back)
    run.reconcile_once()
    per_tick = []
    for _ in range(4):
        seen = len(data.requests)
        run.execution_once()
        per_tick.append([path for path, _ in data.requests[seen:]])
    # One market-data read a tick: while checks remain they take it, so the crypto trigger's
    # REST quote read for the setups already resumed (their stream quotes are stale) waits.
    assert per_tick == [[BARS], [BARS], [BARS], [QUOTES]]
    assert sorted(p["symbols"] for p in data.bar_requests()) == symbols
    assert all(len(bodies(engine, "GAP_RESUMED", setups[s])) == 1 for s in symbols)


def test_an_unrecorded_entry_fill_is_backfilled_for_this_version_only(lab, monkeypatch):
    venue = lab.venue
    proc = lab.process(maintenance=False)
    start(proc, monkeypatch)
    engine = proc.engine
    current = admitted(proc.mx, ["NEW/USD"])["NEW/USD"]
    with monkeypatch.context() as patch:
        patch.setattr(gr, "admission_fields", lambda packet: {})
        earlier = admitted(proc.mx, ["PRE/USD"])["PRE/USD"]
    entries = {sid: trigger_entry(proc.mx, sid) for sid in (earlier, current)}
    # Every other setup: today's fail-closed first-fill rule, although REST has the fill.
    venue.now += timedelta(seconds=10)
    venue.fill(entries[earlier]["id"], entries[earlier]["qty"])  # Trade updates are down.
    venue.now += timedelta(seconds=1)
    proc.run.execution_once()
    assert fills(engine, earlier) == []
    assert state(engine, earlier)["exit_requested"] == "CRYPTO_FIRST_FILL_TIME_UNAVAILABLE"
    assert halts(engine) == ["CRYPTO_FIRST_FILL_TIME_UNAVAILABLE"]
    # This version: the protection tick reads the missed fill from REST before that rule.
    venue.fill(entries[current]["id"], entries[current]["qty"])
    venue.now += timedelta(seconds=1)
    proc.run.execution_once()
    opened = state(engine, current)
    assert opened["state"] == "OPEN" and not opened.get("exit_requested")
    assert [f["source"] for f in fills(engine, current)] == ["ALPACA_PAPER_REST_BACKFILL"]
    assert [o[1:3] for o in orders_of(venue, ["NEW/USD"])] == [("limit", "buy"),
                                                              ("stop_limit", "sell")]


def test_a_failed_hold_record_keeps_the_gap_open_and_is_retried_or_released(
    lab, monkeypatch,
):
    venue = lab.venue
    proc = lab.process(maintenance=False)
    start(proc, monkeypatch)
    engine, run = proc.engine, proc.run
    setups = admitted(proc.mx, ["RET/USD", "END/USD"])
    session_up(proc)
    run.execution_once()
    record, failing = gr.record_pending, {"RET/USD", "END/USD"}

    def unavailable(store, setup_id, body, key):
        if body["symbol"] in failing:
            raise RuntimeError("FIXTURE_LEDGER_WRITE_FAILED")
        return record(store, setup_id, body, key)

    monkeypatch.setattr(gr, "record_pending", unavailable)
    gap_at = venue.now
    run.market_gap("CRYPTO", "MARKET_STREAM_DISCONNECTED_OR_GAP")
    run.execution_once()
    # Held at once (no trigger), but the market's gap stays open until the record exists,
    # exactly as a failed revocation keeps it open today: entries stay blocked.
    assert set(run.gap_checks) == {str(s) for s in setups.values()}
    assert "CRYPTO" in run.market_gaps and not run.ready()
    assert bodies(engine, "GAP_RESUME_PENDING") == []
    failing.discard("RET/USD")
    venue.now += timedelta(seconds=1)
    run.execution_once()  # Retried: the same hold, its window still the gap.
    [pending] = bodies(engine, "GAP_RESUME_PENDING", setups["RET/USD"])
    assert pending["window_start"] == gap_at.isoformat() and "CRYPTO" in run.market_gaps
    venue.now = engine._load(setups["END/USD"])[0]["expires_at"] + timedelta(seconds=1)
    run.execution_once()  # END expires as today (RET too: both were admitted together).
    run.execution_once()
    assert run.gap_checks == {} and "CRYPTO" not in run.market_gaps
    assert state(engine, setups["END/USD"])["state"] == "EXPIRED_UNTRIGGERED"
    assert run.status()["gap_resume"]["pending_count"] == 0
