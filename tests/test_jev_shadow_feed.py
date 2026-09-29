"""Public collector contract tests; deterministic fake sockets never perform network I/O."""

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalyst_lab.jev_shadow import ShadowCase, ShadowStore
from catalyst_lab.jev_shadow_feed import (
    ENDPOINT,
    VENUE,
    CoinbaseTickerFeed,
    bounded_seconds,
    collect_public_feed,
    digest,
    eligible_cases,
    encoded,
    feed_sessions,
    products_list,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
PRODUCTS = ("BTC-USD", "ETH-USD")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jev_shadow.py"
SPEC = importlib.util.spec_from_file_location("standalone_jev_shadow_cli", SCRIPT)
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def acknowledgement():
    return {"type": "subscriptions", "channels": [
        {"name": name, "product_ids": list(PRODUCTS)} for name in ("ticker", "heartbeat")]}


def ticker(second=1, **changes):
    return {"type": "ticker", "product_id": "BTC-USD", "sequence": second,
            "trade_id": second, "time": (NOW + timedelta(seconds=second)).isoformat(),
            "price": "100", "best_bid": "99.99", "best_ask": "100.01",
            "best_bid_size": "2", "best_ask_size": "3", **changes}


def ready():
    parser = CoinbaseTickerFeed(PRODUCTS, started_at=NOW)
    parser.consume(acknowledgement(), received_at=NOW)
    return parser


def shadow_case(**changes):
    raw = {
        "case_id": "feed-case", "symbol": "BTC/USD", "venue": VENUE,
        "as_of": NOW.isoformat(), "decision_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        "exit_deadline": (NOW + timedelta(minutes=2)).isoformat(),
        "entry_trigger": "100", "max_entry_price": "101", "stop": "95", "target": "114",
        "quantity": "1", "bid": "99.99", "ask": "100.01", "quote_at": NOW.isoformat(),
        "observed_dollar_volume": "100000", "liquidity_at": NOW.isoformat(),
        "policy": {"price_ceiling": "150", "minimum_dollar_volume": "10000",
                   "maximum_participation": "0.01", "assumed_round_trip_cost_bps": "20",
                   "maximum_cost_to_risk": "0.25", "slippage_bps": "0",
                   "fee_bps_per_side": None, "entry_latency_ms": 0},
        "evidence": [{"evidence_id": "test-level", "available_at": NOW.isoformat(),
                      "event_at": NOW.isoformat(), "content": {"engineering_fixture": True}}],
        "level_evidence_ids": ["test-level"], "jev_decision": "REJECT",
        "jev_policy_version": "FIXTURE_ONLY", "jev_receipt_hash": "fixture",
        "session_id": "fixture-session", "event_id": "fixture-event", "cohort": "PROSPECTIVE",
        **changes,
    }
    return ShadowCase.from_dict(raw)


class FakeClock:
    value = 0

    def now(self):
        return NOW + timedelta(seconds=self.value)

    def monotonic(self):
        return self.value


class FakeSocket:
    def __init__(self, clock, messages, *, fail=False):
        self.clock, self.messages, self.fail = clock, list(messages), fail
        self.sent, self.calls = [], []

    def connector(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, body):
        self.sent.append(json.loads(body))

    def recv(self, *, timeout):
        if self.messages:
            second, body = self.messages.pop(0)
            self.clock.value = second
            return encoded(body)
        self.clock.value += timeout
        if self.fail:
            raise ConnectionError("fixture disconnect")
        raise TimeoutError


def test_parser_requires_ack_and_uses_exact_bid_ask_sizes():
    parser = ready()
    tick = parser.consume(ticker(), received_at=NOW + timedelta(seconds=1))
    assert tick.feed_healthy
    obs = tick.observation("frozen-case")
    assert obs.case_id == "frozen-case" and obs.venue == VENUE
    assert str(obs.ask_size) == "3" and str(obs.bid_size) == "2"
    assert obs.at == obs.quote_at
    unacknowledged = CoinbaseTickerFeed(PRODUCTS, started_at=NOW)
    assert not unacknowledged.consume(ticker(), received_at=obs.at).feed_healthy


def test_duplicates_do_not_add_liquidity_and_conflicts_latch_fault():
    parser = ready()
    first = ticker()
    parser.consume(first, received_at=NOW + timedelta(seconds=1))
    assert parser.consume(first, received_at=NOW + timedelta(seconds=2)) is None
    assert parser.flags["DUPLICATE_TICK"] == 1
    assert not parser.faults["BTC-USD"]
    assert parser.consume({**first, "price": "101"},
                          received_at=NOW + timedelta(seconds=2)) is None
    assert "DUPLICATE_CONFLICT" in parser.faults["BTC-USD"]


def test_trade_gap_and_regression_never_recover_by_next_fresh_tick():
    parser = ready()
    parser.consume(ticker(), received_at=NOW + timedelta(seconds=1))
    assert not parser.consume(ticker(2, trade_id=3),
                              received_at=NOW + timedelta(seconds=2)).feed_healthy
    assert "BATCHED_OR_MISSING_MATCHES" in parser.faults["BTC-USD"]
    assert parser.consume(ticker(2, sequence=0),
                          received_at=NOW + timedelta(seconds=2)) is None
    assert "OUT_OF_ORDER_TICK" in parser.faults["BTC-USD"]
    assert not parser.consume(ticker(3, trade_id=4),
                              received_at=NOW + timedelta(seconds=3)).feed_healthy


def test_noncontiguous_ticker_sequence_flags_unproven_coverage():
    parser = ready()
    parser.consume(ticker(), received_at=NOW + timedelta(seconds=1))
    parsed = parser.consume(ticker(2, sequence=100), received_at=NOW + timedelta(seconds=2))
    assert parsed.feed_healthy
    assert parser.flags["SEQUENCE_COVERAGE_UNPROVEN"] == 1
    assert parser.summary()["complete_trade_tape_verified"] is False


def test_missing_sizes_stay_missing_and_fail_closed():
    parser = ready()
    body = ticker()
    del body["best_ask_size"]
    parsed = parser.consume(body, received_at=NOW + timedelta(seconds=1))
    assert parsed.ask_size is None and not parsed.feed_healthy
    assert "MISSING_DISPLAYED_SIZE" in parser.faults["BTC-USD"]


@pytest.mark.parametrize("body", [
    ticker(best_bid="NaN"), ticker(best_ask="99"), ticker(sequence=True),
    ticker(price=100.0), ticker(best_ask_size="-1"), ticker(time="not-a-time"),
])
def test_malformed_tickers_never_become_shadow_observations(body):
    parser = ready()
    assert parser.consume(body, received_at=NOW + timedelta(seconds=1)) is None
    assert parser.summary()["observed_continuity_fault"]


def test_future_stale_and_absent_heartbeat_latch_fail_closed():
    parser = ready()
    assert parser.consume(ticker(2), received_at=NOW + timedelta(seconds=1)) is None
    parser.check_timeouts(NOW + timedelta(seconds=6))
    assert "NO_HEARTBEAT" in parser.faults["BTC-USD"]
    parser.consume({"type": "heartbeat", "product_id": "BTC-USD", "sequence": 1,
                    "last_trade_id": 1, "time": (NOW + timedelta(seconds=6)).isoformat()},
                   received_at=NOW + timedelta(seconds=6))
    assert "NO_HEARTBEAT" in parser.faults["BTC-USD"]


@pytest.mark.parametrize("seconds", [0, 301, -1, 1.5, True, "25"])
def test_duration_is_bounded_before_network(seconds):
    with pytest.raises(ValueError, match="SECONDS"):
        bounded_seconds(seconds)


@pytest.mark.parametrize("products", [[], ["BTC-USD", "BTC-USD"], ["BTC/USD"], ["ALL"],
                                      ["BTC-EUR"], ["btc-usd"]])
def test_only_explicit_bounded_unique_products(products):
    with pytest.raises(ValueError, match="PRODUCTS"):
        products_list(products)


def test_raw_probe_retains_exact_messages_chain_and_never_creates_store(tmp_path):
    clock = FakeClock()
    messages = [(0, acknowledgement()), (1, ticker()), (2, ticker(2))]
    socket = FakeSocket(clock, messages)
    result = collect_public_feed(products=PRODUCTS, seconds=3, output_dir=tmp_path / "probe",
                                 connector=socket.connector, clock=clock.now,
                                 monotonic=clock.monotonic)
    assert result["mode"] == "RAW_PROBE" and result["bound_case_ids"] == []
    assert result["appended_shadow_observations"] == 0
    assert result["creates_cases"] is result["creates_orders"] is False
    assert result["raw_message_count"] == 3
    assert socket.calls[0][0] == ENDPOINT and socket.calls[0][1]["proxy"] is None
    assert socket.sent == [{"type": "subscribe", "product_ids": list(PRODUCTS),
                            "channels": ["ticker", "heartbeat"]}]
    lines = (tmp_path / "probe/messages.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    head = "0" * 64
    for row, (_, message) in zip(rows, messages, strict=True):
        actual_hash = row.pop("hash")
        assert row["previous_hash"] == head
        assert row["wire_text"] == encoded(message)
        assert actual_hash == digest(encoded(row))
        head = actual_hash
    assert result["raw_head_hash"] == head
    assert not list(tmp_path.rglob("*.sqlite3"))
    assert all(path.stat().st_mode & 0o777 == 0o600
               for path in (tmp_path / "probe").iterdir())
    with pytest.raises(FileExistsError):
        collect_public_feed(products=PRODUCTS, seconds=3, output_dir=tmp_path / "probe",
                            connector=socket.connector, clock=clock.now,
                            monotonic=clock.monotonic)


def test_disconnect_is_recorded_and_never_reconnects(tmp_path):
    clock = FakeClock()
    socket = FakeSocket(clock, [(0, acknowledgement())], fail=True)
    result = collect_public_feed(products=PRODUCTS, seconds=3, output_dir=tmp_path / "probe",
                                 connector=socket.connector, clock=clock.now,
                                 monotonic=clock.monotonic)
    assert result["status"] == "CONNECTION_FAILED_OR_CLOSED"
    assert len(socket.calls) == 1
    assert result["observed_continuity_fault"]


def test_collection_only_uses_eligible_prospective_same_venue_cases(tmp_path):
    clock = FakeClock()
    with ShadowStore(tmp_path / "test.sqlite3", clock=clock.now) as store:
        store.register(shadow_case())
        store.register(shadow_case(case_id="synthetic", cohort="ENGINEERING"))
        store.register(shadow_case(case_id="other-venue", venue="OTHER"))
        store.register(shadow_case(case_id="failed-cost", quantity="100000"))
        selected, excluded = eligible_cases(store, PRODUCTS)
        assert [c.case_id for c in selected] == ["feed-case"]
        assert len(excluded) == 3
        socket = FakeSocket(clock, [(0, acknowledgement()), (1, ticker()), (2, ticker(2))])
        result = collect_public_feed(products=PRODUCTS, seconds=3, output_dir=tmp_path / "capture",
                                     store=store, connector=socket.connector, clock=clock.now,
                                     monotonic=clock.monotonic)
        assert result["appended_shadow_observations"] == 2
        assert not store.observations("synthetic")
        assert len(feed_sessions(store)["events"]) == 2
        assert not feed_sessions(store)["interrupted_session_ids"]
        assert store.evaluate("feed-case", as_of=clock.now()).baseline.state == "OPEN"
        assert store.evaluate("feed-case", as_of=clock.now()).treatment is None


def test_cli_register_ingest_report_and_help_are_callable(tmp_path, capsys):
    c = shadow_case(cohort="ENGINEERING")
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps([c.to_dict()]))
    store_path = tmp_path / "shadow.sqlite3"
    assert CLI.main(["register", "--store", str(store_path), "--cases", str(cases_file)]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["items"][0]["eligible"]
    parser = ready()
    parsed = parser.consume(ticker(), received_at=NOW + timedelta(seconds=1))
    observation = parsed.observation(c.case_id)
    observations_file = tmp_path / "observations.jsonl"
    observations_file.write_text(json.dumps(observation.to_dict()) + "\n")
    assert CLI.main(["ingest", "--store", str(store_path),
                     "--observations", str(observations_file)]) == 0
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["inserted"] == 1
    output = tmp_path / "report.json"
    assert CLI.main(["report", "--store", str(store_path), "--output", str(output)]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["retained_head_hash"] == ingested["retained_head_hash"]
    assert reported["cases"][0]["baseline"]["state"] == "UNFILLED"
    assert json.loads(output.read_text()) == reported
    with pytest.raises(SystemExit) as code:
        CLI.main(["--help"])
    assert code.value.code == 0


def test_cli_bounds_and_other_database_refusal(tmp_path, capsys):
    with pytest.raises(SystemExit) as code:
        CLI.main(["probe", "--products", "BTC-USD", "--seconds", "301",
                  "--output-dir", str(tmp_path / "bad")])
    assert code.value.code == 2
    assert not (tmp_path / "bad").exists()
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE owner_data (value TEXT)")
    assert CLI.main(["report", "--store", str(other)]) == 2
    assert "NOT_AN_ISOLATED_SHADOW_STORE" in capsys.readouterr().out
    with sqlite3.connect(other) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("owner_data",)]


def test_historical_cli_report_filters_future_decisions_consistently(tmp_path, capsys):
    earlier = shadow_case(cohort="ENGINEERING")
    later = shadow_case(cohort="ENGINEERING", case_id="later-case",
                        decision_at=(NOW + timedelta(seconds=10)).isoformat())
    path = tmp_path / "history.sqlite3"
    with ShadowStore(path) as store:
        store.register(earlier)
        store.register(later)
    assert CLI.main(["report", "--store", str(path),
                     "--as-of", (NOW + timedelta(seconds=5)).isoformat()]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [case["case_id"] for case in report["cases"]] == ["feed-case"]
    assert report["metrics"]["cohorts"]["ENGINEERING"]["cases"] == 1


def test_invalid_subscription_shape_and_message_type_fail_closed():
    parser = ready()
    assert parser.consume({"type": "subscriptions", "channels": [{"product_ids": None}]},
                          received_at=NOW) is None
    assert parser.consume({"type": []}, received_at=NOW) is None
    assert parser.flags["INVALID_SUBSCRIPTION_ACK"] == 1
    assert parser.flags["INVALID_MESSAGE_TYPE"] == 1
