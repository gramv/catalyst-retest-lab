"""Research schedule configuration and the read-only research-context API.

Fixture evidence only: a fixture asset list behind the read-only paper transport and the
request-budget governor, a fixture Alpaca market-data source (MockTransport), per-test
disposable PostgreSQL databases, the fixture paper venue and a mock Jev. No broker,
provider, network or owner-ledger contact.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from catalyst_lab.alpaca import AlpacaCredentials, ReadOnlyPaperTransport
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.broker_budget import BrokerBudget, BudgetPolicy, BudgetTransport
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_app import AppSettings, build_app_from_env, create_application
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_ops import (
    ENV_NAMES,
    OPTIONAL_ENV,
    REQUIRED_ENV,
    load_private_config,
    preflight,
)
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.muse_guidelines import (
    MUSE_GUIDELINES,
    MUSE_GUIDELINES_V1_SHA256,
    MUSE_GUIDELINES_V2,
    MUSE_GUIDELINES_V2_SHA256,
    MUSE_GUIDELINES_V3,
    MUSE_GUIDELINES_V3_SHA256,
    MUSE_GUIDELINES_V3_VERSION,
    MUSE_GUIDELINES_VERSION,
)
from catalyst_lab.research_context import (
    ASSET_SOURCE,
    STABLECOINS,
    CryptoAssetReader,
    ResearchContextService,
    tradable_universe,
)
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3, ResearchCapabilityUnavailable
from catalyst_lab.research_schedule import ResearchSchedule
from catalyst_lab.scan_sources import AlpacaMarketSource, SourcePolicy
from catalyst_lab.system_check import LiveQuote
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_research_report_v3 import (
    AGENTS,
    LEGACY,
    OPERATOR,
    SCHEDULE,
    STATUS,
    bearer,
    pick,
    report_v3,
    v3_on_venue,
)
from tests.test_selection_b1 import classify

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
ROUTE = "/api/v1/lab/research-context"
T0 = datetime(2026, 9, 26, 12, 30, 10, tzinfo=UTC)  # 08:30:10 in New York.


# --- Schedule configuration ---------------------------------------------------------------------

def test_the_owner_schedule_parses_and_names_its_runs():
    schedule = ResearchSchedule.from_json(
        '{"timezone": "America/New_York", "runs": ["08:00"], "grace_minutes": 60}')
    assert schedule == ResearchSchedule("America/New_York", ("08:00",), 60) == SCHEDULE
    assert ResearchSchedule.from_json(
        '{"timezone": "America/New_York", "runs": ["08:00"]}').grace_minutes == 60
    assert schedule.as_dict() == {"version": "RESEARCH_SCHEDULE_V1",
                                  "timezone": "America/New_York", "runs": ["08:00"],
                                  "grace_minutes": 60}
    now = datetime(2026, 9, 26, 14, 30, tzinfo=UTC)  # 10:30 in New York.
    slot = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert schedule.latest_at_or_before(now) == slot and schedule.is_slot(slot)
    assert not schedule.is_slot(slot + timedelta(minutes=1))
    assert not schedule.is_slot(slot + timedelta(microseconds=1))
    assert schedule.next_after(slot) == schedule.next_after(now) == slot + timedelta(days=1)
    assert schedule.next_runs(now) == [slot + timedelta(days=1)]
    assert schedule.validity_limit(slot) == slot + timedelta(days=1, minutes=60)
    assert schedule.local(slot).isoformat() == "2026-09-26T08:00:00-04:00"


def test_a_second_run_and_the_grace_bound_the_validity():
    twice = ResearchSchedule("America/New_York", ("08:00", "20:00"), 60)
    morning = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    evening = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    assert twice.next_runs(morning + timedelta(hours=1)) == [evening, morning + timedelta(days=1)]
    assert twice.validity_limit(morning) == evening + timedelta(hours=1)
    assert twice.latest_at_or_before(evening - timedelta(seconds=1)) == morning


def test_schedule_v2_names_the_daily_run_and_keeps_picks_until_the_next_one():
    """RESEARCH_SCHEDULE_V2 (docs/RESEARCH-LOOP-V2.md): V1's runs plus the full daily run; a
    report answering any run is valid until the next daily run plus the grace."""
    every_two_hours = [f"{hour:02d}:00" for hour in range(0, 24, 2)]
    loop = ResearchSchedule.from_json(json.dumps({
        "timezone": "America/New_York", "runs": every_two_hours, "daily": "08:00",
        "grace_minutes": 60}))
    assert loop.version == "RESEARCH_SCHEDULE_V2" and loop.daily == "08:00"
    assert loop.as_dict() == {"version": "RESEARCH_SCHEDULE_V2", "timezone": "America/New_York",
                              "runs": every_two_hours, "grace_minutes": 60, "daily": "08:00"}
    daily = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)  # 08:00 in New York.
    update, last_update = daily + timedelta(hours=2), daily + timedelta(hours=22)
    tomorrow = daily + timedelta(days=1)
    assert loop.run_kind(daily) == "FULL"
    assert loop.run_kind(update) == loop.run_kind(last_update) == "UPDATE"
    assert loop.next_full_after(daily) == loop.next_full_after(update) == tomorrow
    assert loop.validity_limit(daily) == loop.validity_limit(update) == tomorrow + timedelta(
        hours=1)
    assert loop.validity_limit(last_update) == tomorrow + timedelta(hours=1)
    assert loop.next_after(daily) == update and loop.is_slot(update)  # V1's slots, unchanged.
    v1 = ResearchSchedule("America/New_York", tuple(every_two_hours), 60)
    assert v1.version == "RESEARCH_SCHEDULE_V1" and "daily" not in v1.as_dict()
    assert v1.run_kind(update) == "FULL" and v1.next_full_after(daily) == update
    assert v1.validity_limit(daily) == update + timedelta(hours=1)
    assert ResearchSchedule(**{k: v for k, v in loop.as_dict().items() if k != "version"}) == loop


def test_daylight_saving_days_skip_missing_runs_and_use_the_first_ambiguous_one():
    night = ResearchSchedule("America/New_York", ("02:30",), 60)
    # 2026-03-08 02:30 does not exist in New York; the next run is the following day's.
    before = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    assert night.next_runs(before, 2) == [datetime(2026, 3, 9, 6, 30, tzinfo=UTC),
                                          datetime(2026, 3, 10, 6, 30, tzinfo=UTC)]
    assert night.validity_limit(datetime(2026, 3, 7, 7, 30, tzinfo=UTC)) == datetime(
        2026, 3, 9, 7, 30, tzinfo=UTC)
    early = ResearchSchedule("America/New_York", ("01:30",), 60)
    # 2026-11-01 01:30 happens twice; the run is the first (daylight time, UTC-4).
    assert early.next_after(datetime(2026, 11, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC)
    assert not early.is_slot(datetime(2026, 11, 1, 6, 30, tzinfo=UTC))


INVALID_SCHEDULES = {
    "unknown key": '{"timezone": "America/New_York", "runs": ["08:00"], "cron": "0 8 * * *"}',
    "missing runs": '{"timezone": "America/New_York"}',
    "missing timezone": '{"runs": ["08:00"]}',
    "no runs": '{"timezone": "America/New_York", "runs": []}',
    "string runs": '{"timezone": "America/New_York", "runs": "08:00"}',
    "bad time": '{"timezone": "America/New_York", "runs": ["8:00"]}',
    "hour 24": '{"timezone": "America/New_York", "runs": ["24:00"]}',
    "seconds": '{"timezone": "America/New_York", "runs": ["08:00:00"]}',
    "unsorted": '{"timezone": "America/New_York", "runs": ["20:00", "08:00"]}',
    "duplicate": '{"timezone": "America/New_York", "runs": ["08:00", "08:00"]}',
    "unknown zone": '{"timezone": "Mars/Base", "runs": ["08:00"]}',
    "zone path": '{"timezone": "../etc/passwd", "runs": ["08:00"]}',
    "zone directory": '{"timezone": "America", "runs": ["08:00"]}',
    "negative grace": '{"timezone": "America/New_York", "runs": ["08:00"], "grace_minutes": -1}',
    "grace over cap": '{"timezone": "UTC", "runs": ["08:00"], "grace_minutes": 721}',
    "grace not int": '{"timezone": "UTC", "runs": ["08:00"], "grace_minutes": 60.0}',
    "grace bool": '{"timezone": "UTC", "runs": ["08:00"], "grace_minutes": true}',
    "grace spans gap": '{"timezone": "UTC", "runs": ["08:00", "08:30"], "grace_minutes": 30}',
    "grace spans wrap": '{"timezone": "UTC", "runs": ["00:10", "23:30"], "grace_minutes": 40}',
    "duplicate key": '{"timezone": "UTC", "timezone": "UTC", "runs": ["08:00"]}',
    "not an object": '["America/New_York", ["08:00"]]',
    "empty": "",
    "daily not a run": '{"timezone": "UTC", "runs": ["08:00", "20:00"], "daily": "09:00"}',
    "daily null": '{"timezone": "UTC", "runs": ["08:00"], "daily": null}',
    "daily list": '{"timezone": "UTC", "runs": ["08:00"], "daily": ["08:00"]}',
    "daily bad time": '{"timezone": "UTC", "runs": ["08:00"], "daily": "8:00"}',
    "twenty-five runs": json.dumps({"timezone": "UTC", "runs": [
        f"{h:02}:{m:02}" for h in range(24) for m in (0, 30)][:25], "grace_minutes": 0}),
}


@pytest.mark.parametrize("case", sorted(INVALID_SCHEDULES))
def test_invalid_schedules_are_refused(case):
    with pytest.raises(ValueError, match="^RESEARCH_SCHEDULE_INVALID$"):
        ResearchSchedule.from_json(INVALID_SCHEDULES[case])


def test_grace_just_under_the_gap_is_accepted():
    assert ResearchSchedule("UTC", ("08:00", "08:30"), 29).grace_minutes == 29
    assert ResearchSchedule("UTC", ("00:10", "23:30"), 39).runs == ("00:10", "23:30")
    assert ResearchSchedule("UTC", tuple(f"{h:02}:00" for h in range(24)), 59)


def app_env(monkeypatch, **extra):
    values = {
        "MANAGED_HTTP_PORT": "8799",
        "MANAGED_API_TOKEN": LEGACY,
        "MANAGED_CRYPTO_CLASSIFICATIONS_JSON": "[]",
        "MANAGED_REPORT_MAX_SECONDS": "86400",
        "MANAGED_CLASSIFICATION_POLICY": CLASSIFICATION_POLICY,
        "MANAGED_US_CLASSIFICATIONS_JSON": "[]",
        **extra,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MANAGED_RESEARCH_SCHEDULE_JSON", raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_app_settings_read_the_schedule_and_refuse_startup_when_invalid(monkeypatch):
    app_env(monkeypatch)
    settings = AppSettings.from_env()
    assert settings.research_schedule is None and settings.report_seconds == 86400
    app_env(monkeypatch, MANAGED_RESEARCH_SCHEDULE_JSON=json.dumps(
        {"timezone": "America/New_York", "runs": ["08:00", "20:00"], "grace_minutes": 60}))
    assert AppSettings.from_env().research_schedule.runs == ("08:00", "20:00")
    built = []
    for bad in ('{"timezone": "America/New_York"}', "", "null"):
        app_env(monkeypatch, MANAGED_RESEARCH_SCHEDULE_JSON=bad)
        with pytest.raises(ValueError, match="^REQUIRED_MANAGED_APP_CONFIGURATION"):
            AppSettings.from_env()
        with pytest.raises(ValueError, match="^REQUIRED_MANAGED_APP_CONFIGURATION"):
            build_app_from_env(runtime_builder=lambda: built.append(True))
    assert built == []  # Refused before any runtime, broker or database is built.
    app_env(monkeypatch, MANAGED_REPORT_MAX_SECONDS="86401")
    with pytest.raises(ValueError, match="^REQUIRED_MANAGED_APP_CONFIGURATION"):
        AppSettings.from_env()
    with pytest.raises(ValueError, match="^REQUIRED_MANAGED_APP_CONFIGURATION_INVALID$"):
        AppSettings(8799, LEGACY, 300, (), CLASSIFICATION_POLICY, (),
                    research_schedule={"timezone": "America/New_York"})


def private_example(tmp_path, name="example.json", **environment):
    config = json.loads((ROOT / "deploy/private-paper.example.json").read_text())
    config["environment"].update(environment)
    private = tmp_path / name
    private.write_text(json.dumps(config))
    private.chmod(0o600)
    return load_private_config(private)


def test_ops_lists_the_optional_schedule_and_preflight_validates_it(tmp_path):
    name = "MANAGED_RESEARCH_SCHEDULE_JSON"
    assert name in ENV_NAMES and name in OPTIONAL_ENV and name not in REQUIRED_ENV
    config = private_example(tmp_path)
    env = config["environment"]
    # The deployed schedule: one 08:00 run a day again from 2026-09-29, its picks live about
    # 24 hours (owner, 2026-09-28 evening: the interim until the research loop's 2-hourly
    # updates; every 2 hours ran 2026-09-28). The code's default (SCHEDULE) is the same value.
    assert ResearchSchedule.from_json(env[name]) == ResearchSchedule(
        "America/New_York", ("08:00",), 60)
    assert SCHEDULE == ResearchSchedule("America/New_York", ("08:00",), 60)
    assert env["MANAGED_REPORT_MAX_SECONDS"] == "86400"
    assert name not in preflight(config, ROOT)["invalid_configuration_names"]
    for index, bad in enumerate(('{"timezone": "America/New_York", "runs": ["8:00"]}',
                                 '{"timezone": "America/New_York", "runs": ["08:00"], "x": 1}')):
        broken = private_example(tmp_path, f"broken-{index}.json", **{name: bad})
        assert name in preflight(broken, ROOT)["invalid_configuration_names"]
    config["environment"].pop(name)
    assert name not in preflight(config, ROOT)["missing_configuration_names"]


def test_guidelines_v3_is_new_and_v1_v2_stay_byte_identical():
    assert MUSE_GUIDELINES_V1_SHA256 == (
        "0c04bbeae02f363d6aea050d26ced803af9a93d3664bc755b863a2d973166482")
    assert MUSE_GUIDELINES_V2_SHA256 == (
        "f4081b0792767c78ae339a9b500a70892e49f80f07c01dfcdddded827b029dd4")
    # The Muse worker keeps V2 (its V2 and legacy reports); report V3 agents use V3.
    assert (MUSE_GUIDELINES_VERSION, MUSE_GUIDELINES) == (
        "MUSE_RESEARCH_GUIDELINES_V2", MUSE_GUIDELINES_V2)
    assert MUSE_GUIDELINES_V3_VERSION == "MUSE_RESEARCH_GUIDELINES_V3"
    for text in ("/api/v1/lab/research-context", REPORT_SCHEMA_V3, "20 picks",
                 "at least 2% below max_entry_price", "at least 2", "after Jev's selection",
                 "invalidation", "Never name yourself", "11,000 bytes", "under 3,000",
                 "skipped", "run_slot", "never shown to Jev", "0x wallet"):
        assert text in " ".join(MUSE_GUIDELINES_V3.split()), text
    document = (ROOT / "docs" / "MUSE-GUIDELINES.md").read_text()
    runtime = document.split("<!-- runtime-guidelines-v3:start -->\n", 1)[1].split(
        "<!-- runtime-guidelines-v3:end -->", 1)[0]
    assert runtime == MUSE_GUIDELINES_V3
    assert f"Version: **{MUSE_GUIDELINES_V3_VERSION}**" in document
    assert MUSE_GUIDELINES_V3_SHA256 in document


# --- The tradable universe --------------------------------------------------------------------

def asset(symbol, **changes):
    row = {"symbol": symbol, "class": "crypto", "status": "active", "tradable": True,
           "price_increment": "0.01", "min_order_size": "0.0001",
           "min_trade_increment": "0.000000001", "id": "fixture-asset-id",
           "name": "Fixture Asset Name"}
    row.update(changes)
    return row


ASSETS = [
    asset("BTC/USD", price_increment="1"), asset("ETH/USD", price_increment="0.1"),
    asset("AAA/USD"), asset("BBB/USD"), asset("CCC/USD"), asset("PAXG/USD"),
    *(asset(coin + "/USD") for coin in sorted(STABLECOINS)),
    asset("BTC/USDT"), asset("BTC/USDC"), asset("ETH/BTC"),
    asset("OLD/USD", status="inactive"), asset("HALT/USD", tradable=False),
    asset("NOGRID/USD", price_increment=None), asset("ZERO/USD", min_order_size="0"),
    asset("EQTY/USD", **{"class": "us_equity"}), {"class": "crypto"},
]
TRADABLE = ["AAA/USD", "BBB/USD", "BTC/USD", "CCC/USD", "ETH/USD", "PAXG/USD"]


def test_the_universe_is_active_tradable_usd_crypto_without_stablecoins():
    assert STABLECOINS == {"USDC", "USDT", "USDG", "DAI", "PYUSD", "USDP", "TUSD", "FDUSD",
                           "EURC"}
    coins, excluded = tradable_universe(ASSETS)
    assert [c["symbol"] for c in coins] == TRADABLE  # PAXG (gold) is kept.
    btc = coins[2]
    assert (btc["price_increment"], btc["min_order_size"], btc["quantity_increment"]) == (
        D("1"), D("0.0001"), D("0.000000001"))
    assert excluded == {
        "stablecoins": sorted(coin + "/USD" for coin in STABLECOINS),
        "non_usd_quote_count": 3, "inactive_or_untradable_count": 2,
        "unusable_metadata": ["EQTY/USD", "NOGRID/USD", "ZERO/USD"], "malformed_count": 1,
    }


# --- The context service over fixture sources ---------------------------------------------------

class Feeds:
    """Fixture paper asset list and Alpaca crypto market data; counts every request."""

    def __init__(self, clock, assets=ASSETS):
        self.clock, self.assets, self.calls = clock, assets, []
        self.missing_quote = {"CCC/USD"}
        self.bars_status = 200

    def paper(self, request):
        assert (request.method, request.url.path) == ("GET", "/v2/assets")
        assert dict(request.url.params) == {"asset_class": "crypto", "status": "active"}
        self.calls.append("assets")
        return httpx.Response(200, json=self.assets)

    def data(self, request):
        path, symbols = request.url.path, request.url.params["symbols"].split(",")
        at = self.clock().isoformat()
        if path.endswith("/latest/quotes"):
            self.calls.append("quotes")
            return httpx.Response(200, json={"quotes": {
                s: {"t": at, "bp": 101.0, "ap": 101.2} for s in symbols
                if s not in self.missing_quote}})
        if path.endswith("/latest/trades"):
            self.calls.append("trades")
            return httpx.Response(200, json={"trades": {
                s: {"t": at, "p": 101.1, "s": 0.5, "i": 7} for s in symbols}})
        assert path.endswith("/bars") and request.url.params["timeframe"] == "1Hour"
        self.calls.append("bars")
        if self.bars_status != 200:
            return httpx.Response(self.bars_status, json={"message": "fixture outage"})
        end = datetime.fromisoformat(request.url.params["end"])
        start = datetime.fromisoformat(request.url.params["start"])
        assert end - start == timedelta(hours=24) and end.minute == end.second == 0
        rows = [{"t": (start + timedelta(hours=h)).isoformat(), "o": 100, "h": 102, "l": 99,
                 "c": 101, "v": 2, "vw": 100.5, "n": 3} for h in range(24)]
        rows.append({"t": end.isoformat(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 999, "vw": 1})
        return httpx.Response(200, json={"bars": {s: rows for s in symbols if s != "ETH/USD"},
                                         "next_page_token": None})

    def count(self, kind):
        return self.calls.count(kind)


def service_for(repo, clock, feeds, *, budget=None, schedule=SCHEDULE):
    budget = budget or BrokerBudget(clock=clock)
    reader = CryptoAssetReader(CREDENTIALS, transport=budget.transport(
        httpx.MockTransport(feeds.paper)))
    market = AlpacaMarketSource(CREDENTIALS, SourcePolicy("iex", 5, 50, 1000, 2, 5), clock,
                                transport=httpx.MockTransport(feeds.data))
    service = ResearchContextService(
        repo, clock=clock, schedule=schedule, asset_reader=reader, market_source=market,
        report_format={"max_report_age_seconds": 60, "report_max_seconds": 86400})
    return service, budget


@pytest.fixture
def context_lab(er):
    now = [T0]
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    feeds = Feeds(lambda: now[0])
    service, budget = service_for(repo, lambda: now[0], feeds)
    return SimpleNamespace(repo=repo, now=now, feeds=feeds, service=service, budget=budget)


def context_client(cycle, store, service):
    return TestClient(create_managed_app(
        cycle, store, api_token=LEGACY, runtime_status=lambda: {}, status_token=STATUS,
        operator_token=OPERATOR, agent_tokens=AGENTS, research_context=service))


def silent_cycle(repo, clock):
    reviewer = JevReviewer(
        JevStore(repo.database_url.replace("user=catalyst_risk", "user=catalyst_jev")),
        ReliabilityPolicy("RESEARCH_CONTEXT_FIXTURE", 10, 1, 0.25, 1000, 30),
        key_provider=lambda: "fixture-no-provider-secret",
        transport=httpx.MockTransport(lambda r: pytest.fail("No review in context tests")),
        clock=clock,
    )
    return ResearchCycle(repo, reviewer, CyclePolicy(10, 10, 15, 60, 30), clock=clock)


def test_context_lists_the_tradable_universe_with_market_data_and_sources(context_lab):
    lab = context_lab
    web = context_client(silent_cycle(lab.repo, lambda: lab.now[0]),
                         ManagedStore(lab.repo), lab.service)
    reply = web.get(ROUTE, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 200
    body = reply.json()
    # RESEARCH_CONTEXT_V2 from package learning-app: V1's fields plus the caller's lessons.
    assert body["context_version"] == "RESEARCH_CONTEXT_V2" and body["as_of"] == T0.isoformat()
    assert body["lessons"]["agent_id"] == "claude" and body["lessons"]["windows"] is None
    assert body["caller"] == {"role": "muse", "agent_id": "claude"}
    assert body["trade_authorized"] is False and body["pending_reviews"] == []
    universe = body["universe"]
    assert universe["count"] == len(TRADABLE)
    assert [c["symbol"] for c in universe["coins"]] == TRADABLE
    assert universe["excluded"]["stablecoins"] == sorted(c + "/USD" for c in STABLECOINS)
    btc = next(c for c in universe["coins"] if c["symbol"] == "BTC/USD")
    assert btc == {
        "symbol": "BTC/USD", "bid": "101.0", "ask": "101.2", "quote_at": T0.isoformat(),
        "spread_bps": "19.78", "last_trade_price": "101.1", "last_trade_at": T0.isoformat(),
        "price_increment": "1", "min_order_size": "0.0001",
        "quantity_increment": "0.000000001",
        "volume_24h": {"base": "48", "usd": "4824.00", "completed_hour_bars": 24},
    }
    ccc = next(c for c in universe["coins"] if c["symbol"] == "CCC/USD")
    assert (ccc["bid"], ccc["ask"], ccc["spread_bps"]) == (None, None, None)
    assert ccc["last_trade_price"] == "101.1"
    eth = next(c for c in universe["coins"] if c["symbol"] == "ETH/USD")
    assert eth["volume_24h"] == {"base": "0", "usd": "0.00", "completed_hour_bars": 0}
    assert {"symbol": "CCC/USD", "code": "MISSING_OR_INVALID_QUOTES"} in universe["issues"]
    sources = universe["sources"]
    assert sources["assets"] == {"label": ASSET_SOURCE, "fetched_at": T0.isoformat(),
                                 "cache_seconds": 3600}
    assert sources["quotes_and_trades"]["cache_seconds"] == 5
    assert sources["volume_24h"]["window_end"] == "2026-09-26T12:00:00+00:00"
    assert sources["volume_24h"]["available"] is True
    assert "Fixture Asset Name" not in reply.text and "fixture-asset-id" not in reply.text
    schedule = body["schedule"]
    assert schedule["current_run_slot"] == "2026-09-26T08:00:00-04:00"
    assert schedule["current_run_valid_until_limit"] == "2026-09-27T09:00:00-04:00"
    assert schedule["next_runs"] == ["2026-09-27T08:00:00-04:00"]
    assert body["report_format"]["schema_version"] == REPORT_SCHEMA_V3
    assert body["report_format"]["picks_target"] == 20
    assert body["report_format"]["max_report_age_seconds"] == 60
    assert body["open_trades"] == [] and body["recent_outcomes"]["closed_trades"] == []
    assert body["recent_outcomes"]["last_run"] is None


def test_asset_quote_and_volume_caches_and_the_research_budget(context_lab):
    lab = context_lab
    principal = SimpleNamespace(role="muse", agent_id="claude", legacy=False)
    lab.service.context(principal)
    assert lab.feeds.calls == ["assets", "quotes", "trades", "bars"]
    assert lab.budget.bucket.sent["RESEARCH_AND_LIQUIDITY"] == 1  # Through the governor.
    lab.now[0] = T0 + timedelta(seconds=4.9)
    lab.service.context(principal)
    assert lab.feeds.calls == ["assets", "quotes", "trades", "bars"]  # Quotes under 5 s.
    lab.now[0] = T0 + timedelta(seconds=5)
    lab.service.context(principal)
    assert (lab.feeds.count("quotes"), lab.feeds.count("trades")) == (2, 2)
    assert (lab.feeds.count("assets"), lab.feeds.count("bars")) == (1, 1)
    lab.now[0] = T0 + timedelta(minutes=30)  # A new completed hour: volume refreshes.
    lab.service.context(principal)
    assert (lab.feeds.count("assets"), lab.feeds.count("bars")) == (1, 2)
    lab.now[0] = T0 + timedelta(seconds=3600)  # About an hour: the asset list refreshes.
    lab.service.context(principal)
    assert lab.feeds.count("assets") == 2
    assert lab.budget.bucket.sent["RESEARCH_AND_LIQUIDITY"] == 2
    snapshot = lab.service.universe_snapshot()
    assert snapshot.symbols == frozenset(TRADABLE) and snapshot.source == ASSET_SOURCE
    assert lab.feeds.count("assets") == 2  # Report intake reuses the cached list.


def test_an_exhausted_budget_or_failed_asset_read_fails_closed(context_lab):
    lab = context_lab
    tiny = BrokerBudget(BudgetPolicy(requests_per_minute=1, burst_capacity=1,
                                     protective_requests_per_minute=1),
                        clock=lambda: lab.now[0])
    service, _ = service_for(lab.repo, lambda: lab.now[0], lab.feeds, budget=tiny)
    web = context_client(silent_cycle(lab.repo, lambda: lab.now[0]),
                         ManagedStore(lab.repo), service)
    reply = web.get(ROUTE, headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (503, {"detail": "RESEARCH_UNIVERSE_UNAVAILABLE"})
    assert lab.feeds.calls == []  # Refused before any I/O.
    assert tiny.bucket.denied["RESEARCH_AND_LIQUIDITY"] == 1
    with pytest.raises(ResearchCapabilityUnavailable, match="RESEARCH_UNIVERSE_UNAVAILABLE"):
        service.intake().snapshot()
    lab.feeds.assets = [asset(c + "/USD") for c in STABLECOINS]  # Nothing tradable.
    with pytest.raises(ResearchCapabilityUnavailable, match="RESEARCH_UNIVERSE_UNAVAILABLE"):
        lab.service.universe_snapshot()
    broken = ResearchContextService(lab.repo, clock=lambda: lab.now[0], asset_reader=None)
    with pytest.raises(ResearchCapabilityUnavailable, match="RESEARCH_UNIVERSE_UNAVAILABLE"):
        broken.universe_snapshot()


def test_a_volume_outage_leaves_prices_and_retries_after_a_minute(context_lab):
    lab = context_lab
    lab.feeds.bars_status = 500
    principal = SimpleNamespace(role="muse", agent_id="claude", legacy=False)
    body = json.loads(json.dumps(lab.service.context(principal)))
    assert all(c["volume_24h"] is None for c in body["universe"]["coins"])
    assert body["universe"]["coins"][0]["bid"] == "101.0"
    assert {"symbol": None, "code": "VOLUME_24H_SCAN_HTTP_500"} in body["universe"]["issues"]
    assert body["universe"]["sources"]["volume_24h"]["available"] is False
    lab.now[0] = T0 + timedelta(seconds=59)
    lab.service.context(principal)
    assert lab.feeds.count("bars") == 1
    lab.feeds.bars_status = 200
    lab.now[0] = T0 + timedelta(seconds=60)
    body = lab.service.context(principal)
    assert lab.feeds.count("bars") == 2 and body["universe"]["coins"][0]["volume_24h"]


@pytest.mark.parametrize("value", [0, 5.5, 7200, True])
def test_context_cache_limits_are_explicit(context_lab, value):
    with pytest.raises(ValueError, match="RESEARCH_CONTEXT_POLICY_INVALID"):
        ResearchContextService(context_lab.repo, clock=lambda: T0, quote_cache_seconds=value)
    with pytest.raises(ValueError, match="RESEARCH_CONTEXT_POLICY_INVALID"):
        ResearchContextService(context_lab.repo, clock=lambda: T0,
                               asset_cache_seconds=value if value != 5.5 else 30)


def test_context_needs_a_token_and_only_research_and_status_roles_read_it(context_lab):
    lab = context_lab
    web = context_client(silent_cycle(lab.repo, lambda: lab.now[0]),
                         ManagedStore(lab.repo), lab.service)
    assert web.get(ROUTE).status_code == 401
    assert web.get(ROUTE, headers=bearer("x" * 48)).status_code == 401
    assert web.get(ROUTE, headers=bearer(OPERATOR)).status_code == 403
    assert web.post(ROUTE, headers=bearer(AGENTS["claude"])).status_code == 405
    for token in (LEGACY, *AGENTS.values()):
        assert web.get(ROUTE, headers=bearer(token)).status_code == 200
    status = web.get(ROUTE, headers=bearer(STATUS)).json()
    assert status["caller"] == {"role": "status", "agent_id": None}
    assert status["open_trades"] == [] and status["recent_outcomes"]["last_run"] is None
    assert LEGACY not in web.get(ROUTE, headers=bearer(LEGACY)).text


# --- The caller's own trades and outcomes, from real intake through fills -----------------------

def enter(mx, packet):
    engine, venue, _ = mx
    classify(engine, packet["symbol"])
    # Package system-check: a V3 packet is admitted only with a live price (a pullback here:
    # entry 100 is 0.5% under the mid 100.50, the agent's own price).
    live = LiveQuote(packet["symbol"], D("100.49"), D("100.51"), venue.now, "ALPACA_STREAM",
                     venue.now)
    sid = engine.admit(packet, live_quote=lambda symbol: live)
    trigger = D(packet["levels"]["entry_trigger"])
    observed = observation(mx, trade_price=str(trigger), bid=str(trigger - D(".01")),
                           ask=str(trigger + D(".01")))
    assert engine.observe_trigger(sid, observed)["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == packet["symbol"])
    assert engine.ingest(venue.fill(entry["id"], entry["qty"], price=entry["limit_price"]))
    engine.manage(sid, observed)
    assert engine._load(sid)[1]["state"] == "OPEN"
    return sid, D(entry["qty"])


def close_at_target(mx, sid, symbol):
    engine, venue, _ = mx
    reached = observation(mx, bid="111", ask="111.01")
    engine.manage(sid, reached)
    engine.manage(sid, reached)
    close = [o for o in venue.orders_of("sell", "market") if o["symbol"] == symbol][0]
    engine.ingest(venue.fill(close["id"], close["qty"], price="111"))
    engine.manage(sid, reached)
    assert engine._load(sid)[1]["state"] == "CLOSED"


def selected(cycle, cycle_id):
    asyncio.run(cycle.tick(cycle_id))
    return {p["symbol"]: p for p in cycle.approved_packets(cycle_id)}


def test_each_agent_sees_only_its_own_open_trades_and_recent_outcomes(mx):
    engine, venue, _ = mx
    claude, claude_cycle, _ = v3_on_venue(mx, ["AAA/USD", "BBB/USD"])
    packets = selected(claude, claude_cycle)
    open_sid, open_qty = enter(mx, packets["AAA/USD"])
    closed_sid, closed_qty = enter(mx, packets["BBB/USD"])
    close_at_target(mx, closed_sid, "BBB/USD")
    instinct, instinct_cycle, _ = v3_on_venue(mx, ["CCC/USD"], agent_id="instinct")
    other_sid, _ = enter(mx, selected(instinct, instinct_cycle)["CCC/USD"])
    now = [venue.now]
    feeds = Feeds(lambda: now[0])
    service, _ = service_for(engine.repo, lambda: now[0], feeds)
    web = context_client(claude, engine.store, service)

    mine = web.get(ROUTE, headers=bearer(AGENTS["claude"])).json()
    [trade] = mine["open_trades"]
    assert (trade["setup_id"], trade["symbol"], trade["state"]) == (
        str(open_sid), "AAA/USD", "OPEN")
    assert (D(trade["entry"]), trade["stop"], trade["target"]) == (D("100.10"), "95", "111")
    assert D(trade["quantity"]) == open_qty > 0 and trade["opened_at"]
    assert D(trade["unrealized_pnl_usd"]) == open_qty * (D("101.0") - D("100.10"))
    assert trade["levels"]["max_entry_price"] == "100.10"
    [closed] = mine["recent_outcomes"]["closed_trades"]
    assert (closed["setup_id"], closed["symbol"]) == (str(closed_sid), "BBB/USD")
    assert closed["exit_reason"] and D(closed["exit"]) == D("111")
    assert D(closed["quantity"]) == closed_qty
    assert D(closed["gross_pnl_usd"]) == closed_qty * (D("111") - D("100.10"))
    assert D(closed["r"]) > 2 and closed["r_basis"] == "TEST_R_GROSS_PNL_OVER_RESERVED_PLANNED_RISK"
    last_run = mine["recent_outcomes"]["last_run"]
    assert last_run["cycle_id"] == claude_cycle and last_run["agent_id"] == "claude"
    assert last_run["report_schema_version"] == REPORT_SCHEMA_V3
    assert [(p["symbol"], p["status"], p["review"], p["selected"]) for p in last_run["picks"]] == [
        ("AAA/USD", "OPEN", "APPROVED", True), ("BBB/USD", "CLOSED", "APPROVED", True)]

    theirs = web.get(ROUTE, headers=bearer(AGENTS["instinct"])).json()
    assert [t["setup_id"] for t in theirs["open_trades"]] == [str(other_sid)]
    assert theirs["recent_outcomes"]["closed_trades"] == []
    assert theirs["recent_outcomes"]["last_run"]["cycle_id"] == instinct_cycle
    # The legacy credential acts for agent muse and unattributed records only: none here.
    legacy = web.get(ROUTE, headers=bearer(LEGACY)).json()
    assert legacy["open_trades"] == [] and legacy["recent_outcomes"]["last_run"] is None
    status = web.get(ROUTE, headers=bearer(STATUS)).json()
    assert status["open_trades"] == [] and status["recent_outcomes"]["closed_trades"] == []
    # A trade closed more than seven days before the context is no longer recent.
    now[0] = venue.now + timedelta(days=7, minutes=1)
    later = web.get(ROUTE, headers=bearer(AGENTS["claude"])).json()
    assert later["recent_outcomes"]["closed_trades"] == []


def test_last_run_statuses_cover_intake_refusals_and_pending_reviews(mx):
    engine, venue, _ = mx
    cycle, cycle_id, _ = v3_on_venue(mx, ["AAA/USD", "BBB/USD"])
    now = [venue.now]
    feeds = Feeds(lambda: now[0])
    service, _ = service_for(engine.repo, lambda: now[0], feeds)
    principal = SimpleNamespace(role="muse", agent_id="claude", legacy=False)
    run = service.context(principal)["recent_outcomes"]["last_run"]
    assert [(p["symbol"], p["status"]) for p in run["picks"]] == [
        ("AAA/USD", "AWAITING_REVIEW"), ("BBB/USD", "AWAITING_REVIEW")]
    raw = report_v3([pick(0, "AAA/USD", now=venue.now), pick(1, "USDT/USD", now=venue.now)],
                    now=venue.now)
    from tests.test_research_report_v3 import v3_intake

    cycle.start_report(raw, max_seconds=86400, v3=v3_intake(universe={"AAA/USD"},
                                                            now=venue.now))
    run = service.context(principal)["recent_outcomes"]["last_run"]
    assert [(p["symbol"], p["status"], p["intake_code"]) for p in run["picks"]] == [
        ("AAA/USD", "AWAITING_REVIEW", None),
        ("USDT/USD", "REJECTED_AT_INTAKE", "SYMBOL_NOT_IN_UNIVERSE")]
    assert run["run_slot"] == raw["run_slot"] and run["submitted_count"] == 2


# --- Application wiring ---------------------------------------------------------------------------

class Closing:
    def __init__(self):
        self.closed = False
        self.policy = SourcePolicy("iex", 5, 50, 1000, 2, 5)

    def close(self):
        self.closed = True


def test_the_app_reads_assets_through_the_paper_transport_and_budget_and_closes_them(er):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    now = [T0]
    budget = BrokerBudget(clock=lambda: now[0])
    runtime = SimpleNamespace(
        execution=SimpleNamespace(repo=repo, store=ManagedStore(repo), broker=Closing()),
        research=silent_cycle(repo, lambda: now[0]), now=lambda: now[0],
        credentials=CREDENTIALS, source=Closing(), broker_budget=budget,
        start=lambda: None, stop=lambda: None, status=lambda: {},
    )
    feeds = Feeds(lambda: now[0])
    created = []

    def source_factory(credentials, policy, *, clock):
        created.append(AlpacaMarketSource(credentials, policy, clock,
                                          transport=httpx.MockTransport(feeds.data)))
        return created[-1]

    settings = AppSettings(8799, LEGACY, 86400, (), CLASSIFICATION_POLICY, (),
                           research_schedule=SCHEDULE)
    app = create_application(runtime, settings, source_factory=source_factory)
    service = app.state.research_context
    assert created == []  # Nothing is opened before the first context or V3 report.
    reader = service._source("assets")
    assert isinstance(reader, CryptoAssetReader)
    transport = reader._client._transport
    assert isinstance(transport, ReadOnlyPaperTransport)
    assert isinstance(transport.delegate, BudgetTransport) and transport.delegate.budget is budget
    transport.delegate.delegate = httpx.MockTransport(feeds.paper)  # No network in tests.
    with TestClient(app) as web:
        reply = web.get(ROUTE, headers=bearer(LEGACY))
        assert reply.status_code == 200 and reply.json()["universe"]["count"] == len(TRADABLE)
        assert reply.json()["report_format"]["report_max_seconds"] == 86400
        assert budget.bucket.sent["RESEARCH_AND_LIQUIDITY"] == 1 and len(created) == 1
        report = report_v3([pick(0, "AAA/USD", now=now[0])], now=now[0], agent_id="muse")
        accepted = web.post("/api/v1/lab/research-reports", json=report, headers=bearer(LEGACY))
        assert accepted.status_code == 202 and accepted.json()["contender_count"] == 1
        assert budget.bucket.sent["RESEARCH_AND_LIQUIDITY"] == 1  # The cached asset list.
    assert created[0]._client.is_closed and reader._client.is_closed


def test_without_the_governor_or_schedule_the_app_answers_503(er):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    runtime = SimpleNamespace(
        execution=SimpleNamespace(repo=repo, store=ManagedStore(repo), broker=Closing()),
        research=silent_cycle(repo, lambda: T0), now=lambda: T0, credentials=object(),
        source=Closing(), start=lambda: None, stop=lambda: None, status=lambda: {},
    )
    settings = AppSettings(8799, LEGACY, 86400, (), CLASSIFICATION_POLICY, ())
    app = create_application(runtime, settings, source_factory=lambda *a, **k: pytest.fail(
        "No market source without a request"))
    with TestClient(app) as web:
        reply = web.get(ROUTE, headers=bearer(LEGACY))
        assert (reply.status_code, reply.json()) == (
            503, {"detail": "RESEARCH_UNIVERSE_UNAVAILABLE"})
        report = report_v3([pick(0, "AAA/USD")], agent_id="muse")
        reply = web.post("/api/v1/lab/research-reports", json=report, headers=bearer(LEGACY))
        assert (reply.status_code, reply.json()) == (
            503, {"detail": "RESEARCH_SCHEDULE_NOT_CONFIGURED"})
