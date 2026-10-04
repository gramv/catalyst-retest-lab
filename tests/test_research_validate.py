"""``RESEARCH_REPORT_VALIDATE_V1`` and ``RESEARCH_GUIDELINES_ROUTE_V1`` (package agent-api).

The dry run gives exactly report-V3 intake's verdict (every refusal byte for byte, and intake's
``item_results`` for an acceptable report), writes nothing and calls no Jev; its admission
warnings are what admission then does; it is bound to the agent like the report route and
rate limited per credential. The guidelines route serves the text the research context names.

Fixture evidence only: per-test disposable PostgreSQL databases, a mock Jev transport that
records every request, fixture Alpaca asset and market-data transports, the fixture paper
venue and the real routes in process. No broker, provider, network or owner-ledger contact.
"""

import asyncio
import copy
import hashlib
import json
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import muse_guidelines
from catalyst_lab import system_check as sc
from catalyst_lab.agent_identity import Principal
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.managed_app import AppSettings, create_application
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.managed_store import ManagedStore
from catalyst_lab.muse_guidelines import (
    MUSE_GUIDELINES_V6,
    MUSE_GUIDELINES_V6_SHA256,
    MUSE_GUIDELINES_V6_VERSION,
)
from catalyst_lab.research_cycle import CyclePolicy, ResearchIntake
from catalyst_lab.research_report_v3 import PICK_CODES, V3Intake
from catalyst_lab.research_validate import (
    CHECKS,
    ReportValidator,
    ValidateLimiter,
    principal_key,
)
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_research_context import (
    CREDENTIALS,
    T0,
    Closing,
    Feeds,
    context_client,
    service_for,
    silent_cycle,
)
from tests.test_research_context import context_lab as context_lab
from tests.test_research_report_v3 import (
    AGENTS,
    ENVELOPE,
    LEGACY,
    NOW,
    OPERATOR,
    ROUTE,
    SCHEDULE,
    STATUS,
    UNIVERSE,
    bearer,
    cycle_of,
    events,
    pick,
    refused_picks,
    report_v3,
    v3_intake,
)
from tests.test_research_report_v3 import lab as lab
from tests.test_system_check import (
    DEFAULT,
    HOURLY,
    SHORT,
    TIGHT,
    at_price,
    publish_v3,
    system_runtime,
    two_slots,
    v3_pick,
)
from tests.test_system_check import market as market
from tests.test_system_check import state as setup_state

ROOT = Path(__file__).resolve().parents[1]
VALIDATE = "/api/v1/lab/research-reports/validate"
GUIDELINES = "/api/v1/lab/research-guidelines"
CLAUDE, INSTINCT = bearer(AGENTS["claude"]), bearer(AGENTS["instinct"])
# What the 200 body shares with intake's 202 receipt.
SHARED = ("cycle_id", "expires_at", "submitted_count", "contender_count", "rejected_count",
          "skipped_count", "item_results")


class FixtureContext:
    """What the validator reads from the research context, over the intake tests' fixture
    universe: report V3's schedule and universe (as ``v3_intake``), every coin on a 0.01 grid
    and, by default, no market data (no live-price check)."""

    def __init__(self, universe=UNIVERSE, schedule=SCHEDULE, *, now=NOW):
        self.schedule, self.symbols, self.now = schedule, frozenset(universe), now
        self.views = 0

    def intake(self):
        return v3_intake(self.symbols, self.schedule, now=self.now)

    def admission_view(self):
        self.views += 1
        return {s: {"symbol": s, "price_increment": D("0.01")} for s in self.symbols}, None


def client(lab, *, context=None, limiter=None, submit_v3=None):
    """The real routes over ``lab``'s cycle: the report route (intake) and its dry run."""
    validator = ReportValidator(lab.cycle, context or FixtureContext(), repo=lab.cycle.repo,
                                max_seconds=86400)
    submit_v3 = submit_v3 or v3_intake()
    return TestClient(create_managed_app(
        lab.cycle, lab.cycle.store, api_token=LEGACY, runtime_status=lambda: {},
        report_submit=lambda raw: asyncio.to_thread(
            lab.cycle.start_report, raw, max_seconds=86400, v3=submit_v3),
        report_validate=lambda raw: asyncio.to_thread(validator.validate, raw),
        validate_limiter=limiter or ValidateLimiter(10_000),
        status_token=STATUS, operator_token=OPERATOR, agent_tokens=AGENTS))


def table_counts(repo):
    """Rows in every base table of the ledger, read as its owner."""
    url = repo.database_url.replace("user=catalyst_risk", "user=lab_owner").replace(
        "user=catalyst_app", "user=lab_owner")
    with psycopg.connect(url) as conn:
        tables = [row[0] for row in conn.execute(
            """SELECT quote_ident(table_schema)||'.'||quote_ident(table_name)
            FROM information_schema.tables WHERE table_type='BASE TABLE'
            AND table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1""")]
        counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                  for name in tables}
    assert "lab.managed_events" in counts
    return counts


# --- The verdict is intake's ---------------------------------------------------------------------

def corpus():
    """Report bodies for every intake outcome at ``NOW``: acceptable reports (one with every
    per-pick code), each envelope refusal, the whole-report refusals and no acceptable pick."""
    stale = report_v3([pick(0)], generated_at=(NOW - timedelta(seconds=61)).isoformat())
    stale["context_as_of"] = (NOW - timedelta(minutes=5)).isoformat()
    stale["picks"][0]["agent_price_at"] = (NOW - timedelta(minutes=2)).isoformat()
    cases = {
        "every pick code": report_v3(
            [pick(0)] + [value for _, value in refused_picks()] + [pick(20, kind="CHART")]),
        "all accepted": report_v3([pick(0), pick(1, kind="NEWS"), pick(2, kind="CHART")]),
        "no acceptable pick": report_v3([pick(0, "USDT/USD"), pick(1, qty="1")]),
        "only a schema failure": report_v3([pick(1, qty="1")]),
        "stale": stale,
        "future": report_v3([pick(0)], generated_at=(NOW + timedelta(seconds=1)).isoformat()),
        "another agent": report_v3([pick(0)], agent_id="instinct"),
    }
    for name, mutate in (
        ("duplicate symbol", lambda raw: raw["picks"].append(pick(1, raw["picks"][0]["symbol"]))),
        ("duplicate signal", lambda raw: raw["picks"].append(
            pick(1, signal_id=raw["picks"][0]["signal_id"]))),
        ("e-mail", lambda raw: raw["picks"][0]["reasoning"].update(risks="Mail ops@example.com")),
        ("contract address", lambda raw: raw["picks"][0]["reasoning"].update(
            thesis="Contract 0x" + "ab" * 20 + " is the token.")),
    ):
        raw = report_v3([pick(0)])
        mutate(raw)
        cases[name] = raw
    for name, (mutate, _) in ENVELOPE.items():
        raw = report_v3([pick(0), pick(1)])
        mutate(raw)
        cases["envelope: " + name] = raw
    return cases


def test_the_verdict_is_intakes_byte_for_byte_on_every_outcome(lab):
    web = client(lab)
    outcomes = {}
    for name, raw in corpus().items():
        # The dry run first: had it recorded anything, intake would replay instead of decide.
        dry = web.post(VALIDATE, json=raw, headers=CLAUDE)
        real = web.post(ROUTE, json=raw, headers=CLAUDE)
        outcomes[name] = real.status_code
        if real.status_code != 202:
            assert (dry.status_code, dry.content) == (real.status_code, real.content), name
            continue
        assert dry.status_code == 200, name
        body, receipt = dry.json(), real.json()
        assert {k: body[k] for k in SHARED} == {k: receipt[k] for k in SHARED}, name
        assert json.dumps(body["item_results"]) == json.dumps(receipt["item_results"])
        assert (body["status"], body["validation_version"], body["checked_at"]) == (
            "RESEARCH_REPORT_VALIDATED", "RESEARCH_REPORT_VALIDATE_V1", NOW.isoformat())
        assert (body["already_recorded"], body["recorded"], body["trade_authorized"]) == (
            False, False, False)
        # Once recorded, the dry run answers what intake answers a resend: the stored receipt.
        again = web.post(VALIDATE, json=raw, headers=CLAUDE).json()
        assert again["already_recorded"] is True
        assert {k: again[k] for k in SHARED} == {k: receipt[k] for k in SHARED}
        changed = copy.deepcopy(raw)
        changed["picks"][0]["agent_confidence"] = "0.99"
        dry = web.post(VALIDATE, json=changed, headers=CLAUDE)
        real = web.post(ROUTE, json=changed, headers=CLAUDE)
        assert (dry.status_code, dry.content) == (real.status_code, real.content) == (
            422, b'{"detail":"REPORT_IDEMPOTENCY_CONTENT_MISMATCH"}')
        if name == "every pick code":
            codes = {r["code"] for r in body["item_results"] if r["status"] == "REJECTED"}
            assert codes == set(PICK_CODES)  # Every per-pick refusal, each intake's own.
    # Both acceptable reports, and every kind of refusal, were exercised.
    assert [name for name, status in outcomes.items() if status == 202] == [
        "every pick code", "all accepted"]
    assert {status for status in outcomes.values()} == {202, 403, 422}


def test_an_expired_report_and_the_transport_limits_are_refused_alike(lab):
    lab.now[0] = NOW + timedelta(seconds=30)
    expired = report_v3([pick(0)], valid_until=(NOW + timedelta(seconds=20)).isoformat())
    web = client(lab)
    for body in (expired, [expired], "not an object"):
        dry = web.post(VALIDATE, json=body, headers=CLAUDE)
        real = web.post(ROUTE, json=body, headers=CLAUDE)
        assert (dry.status_code, dry.content) == (real.status_code, real.content)
        assert dry.status_code == 422
    assert web.post(VALIDATE, json=expired, headers=CLAUDE).json() == {
        "detail": "RESEARCH_REPORT_EXPIRED"}
    for content in (b"{", b'{"a": 1, "a": 2}', b" " * 1_048_576, b" " * 1_048_577):
        headers = {**CLAUDE, "Content-Type": "application/json"}
        dry = web.post(VALIDATE, content=content, headers=headers)
        real = web.post(ROUTE, content=content, headers=headers)
        assert (dry.status_code, dry.content) == (real.status_code, real.content)
    assert dry.status_code == 413 and dry.json() == {"detail": "RESEARCH_REPORT_TOO_LARGE"}


def test_without_its_capabilities_the_dry_run_answers_as_intake(lab):
    raw = report_v3([pick(0)])
    for capability, code in (
        (v3_intake(schedule=None), "RESEARCH_SCHEDULE_NOT_CONFIGURED"),
        (V3Intake(SCHEDULE, lambda: None), "RESEARCH_UNIVERSE_UNAVAILABLE"),
    ):
        context = FixtureContext()
        context.intake = lambda capability=capability: capability
        web = client(lab, context=context, submit_v3=capability)
        dry, real = (web.post(path, json=raw, headers=CLAUDE) for path in (VALIDATE, ROUTE))
        assert (dry.status_code, dry.json()) == (real.status_code, real.json()) == (
            503, {"detail": code})
    unconfigured = TestClient(create_managed_app(
        lab.cycle, lab.cycle.store, api_token=LEGACY, runtime_status=lambda: {},
        status_token=STATUS, operator_token=OPERATOR, agent_tokens=AGENTS))
    reply = unconfigured.post(VALIDATE, json=raw, headers=CLAUDE)
    assert (reply.status_code, reply.json()) == (
        503, {"detail": "RESEARCH_REPORT_VALIDATE_NOT_CONFIGURED"})
    assert events(lab.cycle, cycle_of(raw)) == []


def test_a_body_that_is_not_report_v3_is_refused_and_nothing_is_recorded(lab):
    from tests.test_review_dossier import fixture_agent
    from tests.test_review_dossier import item as v2_item
    from tests.test_review_dossier import report as v2_report

    web = client(lab)
    v2 = v2_report([v2_item(0, now=NOW)], now=NOW, agent=fixture_agent("claude"))
    reply = web.post(VALIDATE, json=v2, headers=CLAUDE)
    assert (reply.status_code, reply.json()) == (422, {"detail": "REPORT_V3_REQUIRED"})
    legacy = {k: v for k, v in v2.items() if k not in {"schema_version", "agent"}}
    reply = web.post(VALIDATE, json=legacy, headers=bearer(LEGACY))
    assert (reply.status_code, reply.json()) == (422, {"detail": "REPORT_V3_REQUIRED"})
    # An agent credential never acts for an unattributed body (the report route's rule).
    reply = web.post(VALIDATE, json=legacy, headers=CLAUDE)
    assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    with lab.cycle.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_events").fetchone()["n"] == 0


# --- Nothing written, no Jev --------------------------------------------------------------------

def test_the_dry_run_writes_no_row_and_calls_no_jev(lab):
    context = FixtureContext()
    web = client(lab, context=context)
    reports = corpus()
    before = table_counts(lab.cycle.repo)
    answered = {name: web.post(VALIDATE, json=raw, headers=CLAUDE).status_code
                for name, raw in reports.items()}
    assert set(answered.values()) == {200, 403, 422}
    assert table_counts(lab.cycle.repo) == before
    for raw in reports.values():
        if isinstance(raw.get("agent"), dict):
            cycle_id = cycle_of(raw)
            assert events(lab.cycle, cycle_id) == []  # No cycle, packet or receipt.
            with pytest.raises(ValueError, match="^RESEARCH_CYCLE_MISSING$"):
                asyncio.run(lab.cycle.tick(cycle_id))  # Nothing for the reviewer to review.
    assert lab.calls == [] and table_counts(lab.cycle.repo) == before
    assert context.views == 2  # The warnings read the context once per acceptable report.
    # A recorded report: the real intake writes; its dry runs afterwards write nothing more.
    raw = reports["all accepted"]
    assert web.post(ROUTE, json=raw, headers=CLAUDE).status_code == 202
    recorded = table_counts(lab.cycle.repo)
    assert recorded != before
    for _ in range(3):
        assert web.post(VALIDATE, json=raw, headers=CLAUDE).json()["already_recorded"] is True
    assert table_counts(lab.cycle.repo) == recorded
    assert lab.calls == []  # Intake itself never calls Jev either; the cycle's tick does.


# --- Credentials ---------------------------------------------------------------------------------

def test_the_dry_run_needs_the_agents_own_credential(lab):
    web = client(lab)
    raw = report_v3([pick(0)])
    assert web.post(VALIDATE, json=raw).status_code == 401
    assert web.post(VALIDATE, json=raw, headers=bearer("x" * 40)).status_code == 401
    for token in (STATUS, OPERATOR):
        reply = web.post(VALIDATE, json=raw, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (403, {"detail": "TOKEN_ROLE_NOT_PERMITTED"})
    # Another agent's credential, or the legacy one, never validates for this agent.
    for token in (AGENTS["instinct"], LEGACY):
        reply = web.post(VALIDATE, json=raw, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    assert web.post(VALIDATE, json=raw, headers=CLAUDE).status_code == 200
    # The legacy credential acts for its own agent, muse, as on the report route.
    muse = report_v3([pick(0)], agent_id="muse")
    assert web.post(VALIDATE, json=muse, headers=bearer(LEGACY)).status_code == 200
    reply = web.post(VALIDATE, json=muse, headers=CLAUDE)
    assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    with lab.cycle.repo.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lab.managed_events").fetchone()["n"] == 0


def test_the_dry_run_is_rate_limited_per_credential_and_one_at_a_time(lab):
    clock = [1000.0]
    limiter = ValidateLimiter(3, clock=lambda: clock[0])
    web = client(lab, limiter=limiter)
    raw = report_v3([pick(0)])
    for _ in range(3):
        assert web.post(VALIDATE, json=raw, headers=CLAUDE).status_code == 200
    refused = web.post(VALIDATE, json=raw, headers=CLAUDE)
    assert (refused.status_code, refused.json()) == (
        429, {"detail": "RESEARCH_REPORT_VALIDATE_RATE_LIMITED"})
    assert refused.headers["Retry-After"] == "60"
    assert refused.headers["Cache-Control"] == "no-store"
    # Another credential has its own allowance; the report route has no such limit.
    other = report_v3([pick(0)], agent_id="instinct")
    assert web.post(VALIDATE, json=other, headers=INSTINCT).status_code == 200
    assert web.post(ROUTE, json=raw, headers=CLAUDE).status_code == 202
    clock[0] += 45
    assert web.post(VALIDATE, json=raw, headers=CLAUDE).headers["Retry-After"] == "15"
    clock[0] += 15  # The first three have left the minute; refused calls never counted.
    for _ in range(3):
        assert web.post(VALIDATE, json=raw, headers=CLAUDE).status_code == 200
    # One at a time: while this credential's validation runs, another waits a second.
    clock[0] += 60
    with limiter.hold(principal_key(Principal("muse", "claude"))):
        busy = web.post(VALIDATE, json=raw, headers=CLAUDE)
        assert web.post(VALIDATE, json=other, headers=INSTINCT).status_code == 200
    assert (busy.status_code, busy.headers["Retry-After"]) == (429, "1")
    assert web.post(VALIDATE, json=raw, headers=CLAUDE).status_code == 200
    with pytest.raises(ValueError, match="^VALIDATE_RATE_LIMIT_INVALID$"):
        ValidateLimiter(0)


# --- The admission warnings are what admission does -----------------------------------------------

GRID = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95.005", "target": "111.005"}
LOW_REWARD = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "105"}
ORDER = {"entry_trigger": "100", "max_entry_price": "99.90", "stop": "95", "target": "111"}
GRID_AND_REWARD = {"entry_trigger": "100", "max_entry_price": "100.10", "stop": "95.005",
                   "target": "105"}
# symbol: (agent price, levels, bid, ask, last trade, the code admission refuses with)
PICKS = {
    "PUL/USD": ("100.50", DEFAULT, "100.49", "100.51", None, None),
    "IMM/USD": ("100.10", DEFAULT, "100.09", "100.11", None, None),
    "BRK/USD": ("99.50", DEFAULT, "99.49", "99.51", None, "BREAKOUT_NOT_ENABLED"),
    "MIS/USD": ("100.50", DEFAULT, "106.00", "106.02", None, "PRICE_MISMATCH"),
    "BID/USD": ("98.00", TIGHT, "97.50", "97.60", None, "STOP_ALREADY_HIT"),
    "LST/USD": ("98.00", TIGHT, "97.60", "97.70", "97.50", "STOP_ALREADY_HIT"),
    "STP/USD": ("100.50", SHORT, "100.49", "100.51", None, "STOP_DISTANCE_BELOW_MINIMUM"),
    "GRD/USD": ("100.50", GRID, "100.49", "100.51", None, "CRYPTO_LEVEL_OFF_PRICE_GRID"),
    "LRR/USD": ("100.50", LOW_REWARD, "100.49", "100.51", None, "INVALID_OR_EXPIRED_SETUP"),
    "ORD/USD": ("100.50", ORDER, "100.49", "100.51", None, "INVALID_OR_EXPIRED_SETUP"),
    "TWO/USD": ("100.50", GRID_AND_REWARD, "100.49", "100.51", None,
                "INVALID_OR_EXPIRED_SETUP"),
    # An earlier run's setup still WATCHING: this run supersedes it before admission.
    "WAT/USD": ("100.50", DEFAULT, "100.49", "100.51", None, None),
    # An earlier run's trade still OPEN: never superseded, so the coin stays taken.
    "OPN/USD": ("100.50", DEFAULT, "100.49", "100.51", None, "ACTIVE_SYMBOL_ALREADY_MANAGED"),
    # No quote: no live check is evaluated; admission waits for a price (transient).
    "NOQ/USD": ("100.50", DEFAULT, None, None, None, None),
}


class VenueFeeds:
    """The research context's fixture sources for ``PICKS``: every coin on the fixture venue's
    0.01 grid, and each coin's quote and last trade at the venue's time."""

    def __init__(self, clock):
        self.clock, self.reads = clock, []

    def paper(self, request):
        assert (request.method, request.url.path) == ("GET", "/v2/assets")
        return httpx.Response(200, json=[
            {"symbol": s, "class": "crypto", "status": "active", "tradable": True,
             "price_increment": "0.01", "min_order_size": "0.0001",
             "min_trade_increment": "0.000000001"} for s in PICKS])

    def data(self, request):
        path, symbols = request.url.path, request.url.params["symbols"].split(",")
        at = self.clock().isoformat()
        self.reads.append(path.rsplit("/", 1)[-1])
        if path.endswith("/latest/quotes"):
            return httpx.Response(200, json={"quotes": {
                s: {"t": at, "bp": float(PICKS[s][2]), "ap": float(PICKS[s][3])}
                for s in symbols if PICKS[s][2] is not None}})
        assert path.endswith("/latest/trades")
        return httpx.Response(200, json={"trades": {
            s: {"t": at, "p": float(PICKS[s][4] or PICKS[s][2]), "s": 1, "i": 9}
            for s in symbols if PICKS[s][2] is not None}})


def picks_for(now):
    return {symbol: v3_pick(i, symbol, now, agent=agent, levels=levels)
            for i, (symbol, (agent, levels, *_)) in enumerate(PICKS.items())}


def test_every_admission_warning_is_what_admission_then_does(mx, market):
    engine, venue, _ = mx
    now = venue.now
    old, new = two_slots(now)
    # The earlier run: WAT/USD still WATCHING, OPN/USD filled and OPEN.
    earlier = publish_v3(mx, [v3_pick(0, "WAT/USD", now), v3_pick(1, "OPN/USD", now)],
                         run_slot=old)
    live = at_price("100.49", "100.51", at=now)
    engine.admit(earlier["WAT/USD"], live_quote=live)
    opened = engine.admit(earlier["OPN/USD"], live_quote=live)
    touch = observation(mx, trade_price="100", bid="99.99", ask="100.01")
    assert engine.observe_trigger(opened, touch)["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == "OPN/USD")
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(opened, observation(mx))
    assert setup_state(engine, opened)["state"] == "OPEN"
    # The agent validates this run's report first, against the context's fresh prices.
    feeds = VenueFeeds(lambda: venue.now)
    context, _ = service_for(engine.repo, lambda: venue.now, feeds, schedule=HOURLY)
    intake = ResearchIntake(engine.repo, CyclePolicy(10, 10, 15, 60, 30),
                            clock=lambda: venue.now)
    validator = ReportValidator(intake, context, repo=engine.repo, max_seconds=86400)
    picks = picks_for(now)
    raw = report_v3(list(picks.values()), now=now, run_slot=new.isoformat(),
                    valid_until=(now + timedelta(minutes=30)).isoformat())
    before = table_counts(engine.repo)
    body = validator.validate(raw)
    assert table_counts(engine.repo) == before
    assert {r["status"] for r in body["item_results"]} == {"ACCEPTED"}  # Intake checks none.
    warned = dict(zip(picks, body["admission_warnings"], strict=True))
    assert feeds.reads == ["quotes", "trades"]  # One cached read for every coin.
    # Then the same picks go through intake, Jev (mocked) and publication, and the runtime's
    # supersession pass runs before admission, as every protection tick does.
    selected = {}
    for half in (list(picks)[:7], list(picks)[7:]):
        selected.update(publish_v3(mx, [picks[s] for s in half], run_slot=new))
    run = system_runtime(mx, market, list(PICKS))
    run._retire_superseded_research()

    def no_price(symbol):
        raise sc.LivePriceUnavailable([{"source": "ALPACA_STREAM", "code": "STREAM_QUOTE_MISSING"}])

    for symbol, (_, _, bid, ask, last, code) in PICKS.items():
        codes = [w["code"] for w in warned[symbol]["warnings"]]
        reader = no_price if bid is None else at_price(bid, ask, at=now, last=last)
        if code is None and bid is not None:
            sid = engine.admit(selected[symbol], live_quote=reader)
            assert setup_state(engine, sid)["state"] == "WATCHING" and codes == [], symbol
            assert warned[symbol]["entry_type"] == setup_state(engine, sid)["entry_type"]
            assert warned[symbol]["not_evaluated"] == []
            continue
        with pytest.raises(ValueError) as refused:
            engine.admit(selected[symbol], live_quote=reader)
        if bid is None:  # Transient: no warning, the live checks listed as not evaluated.
            assert str(refused.value) == "LIVE_PRICE_UNAVAILABLE" and codes == []
            assert warned[symbol]["not_evaluated"] == [
                {"check": name, "reason": "LIVE_PRICE_UNAVAILABLE"}
                for name in ("PRICE_MATCH", "STOP_NOT_HIT", "ENTRY_TYPE")]
            continue
        assert codes and codes[0] == str(refused.value) == code, symbol
    # Every check of the route was exercised, and each names admission's own code.
    seen = {w["check"]: w["code"] for entry in warned.values() for w in entry["warnings"]}
    assert seen == dict(CHECKS)
    assert warned["TWO/USD"]["warnings"][0]["reward_risk_at_max_entry"] == "0.9617"
    assert warned["LRR/USD"]["warnings"][0]["reward_risk_at_max_entry"] == "0.9608"
    assert [w["check"] for w in warned["TWO/USD"]["warnings"]] == ["LEVELS", "PRICE_GRID"]
    assert warned["GRD/USD"]["warnings"] == [{
        "check": "PRICE_GRID", "code": "CRYPTO_LEVEL_OFF_PRICE_GRID",
        "path": "picks[7].levels", "price_increment": "0.01", "levels": ["stop", "target"]}]
    assert warned["OPN/USD"]["warnings"] == [{
        "check": "ACTIVE_SYMBOL", "code": "ACTIVE_SYMBOL_ALREADY_MANAGED",
        "path": "picks[12].symbol", "setup_state": "OPEN"}]
    assert warned["LST/USD"]["warnings"][0]["live_last"] == "97.5"
    assert warned["STP/USD"]["not_evaluated"] == [
        {"check": name, "reason": "STOP_DISTANCE_FIRST"}
        for name in ("PRICE_MATCH", "STOP_NOT_HIT", "ENTRY_TYPE")]


def test_a_symbol_outside_the_universe_and_a_schema_failure_evaluate_what_they_can(lab):
    body = client(lab).post(VALIDATE, json=report_v3([
        pick(0), pick(1, "NEW/USD"), pick(2, qty="1")]), headers=CLAUDE).json()
    assert [r["status"] for r in body["item_results"]] == ["ACCEPTED", "REJECTED", "REJECTED"]
    fine, outside, schema = body["admission_warnings"]
    live = [{"check": n, "reason": "LIVE_PRICE_UNAVAILABLE"}
            for n in ("PRICE_MATCH", "STOP_NOT_HIT", "ENTRY_TYPE")]
    assert fine == {"index": 0, "signal_id": "V3SIGNALMARKER-00", "entry_type": None,
                    "warnings": [], "not_evaluated": live}
    outside_reason = "SYMBOL_NOT_IN_UNIVERSE"
    assert outside["not_evaluated"] == [{"check": "PRICE_GRID", "reason": outside_reason}] + [
        {**n, "reason": outside_reason} for n in live]
    assert schema == {"index": 2, "signal_id": "V3SIGNALMARKER-02", "entry_type": None,
                      "warnings": [], "not_evaluated": [{"check": "ALL",
                                                         "reason": "PICK_SCHEMA_INVALID"}]}


# --- The application wiring ----------------------------------------------------------------------

def test_the_app_validates_through_intake_and_the_contexts_cached_prices(er):
    repo = RiskRepository(er.database_url.replace("user=catalyst_app", "user=catalyst_risk"))
    now = [T0]
    feeds = Feeds(lambda: now[0])
    service, _ = service_for(repo, lambda: now[0], feeds)
    runtime = SimpleNamespace(
        execution=SimpleNamespace(repo=repo, store=ManagedStore(repo), broker=Closing()),
        research=silent_cycle(repo, lambda: now[0]), now=lambda: now[0],
        credentials=CREDENTIALS, source=Closing(), start=lambda: None, stop=lambda: None,
        status=lambda: {},
    )
    settings = AppSettings(8799, LEGACY, 86400, (), CLASSIFICATION_POLICY, (),
                           research_schedule=SCHEDULE)
    app = create_application(runtime, settings, research_context=service,
                             source_factory=lambda *a, **k: pytest.fail("No scanner"))
    web = TestClient(app)
    assert web.get("/api/v1/lab/research-context", headers=bearer(LEGACY)).status_code == 200
    raw = report_v3([pick(0, "AAA/USD", now=now[0]), pick(1, "CCC/USD", now=now[0])],
                    now=now[0], agent_id="muse")
    before = table_counts(repo)
    reply = web.post(VALIDATE, json=raw, headers=bearer(LEGACY))
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert [r["status"] for r in body["item_results"]] == ["ACCEPTED", "ACCEPTED"]
    first, second = body["admission_warnings"]
    # AAA/USD: mid 101.10, 1.1% above the entry, 0.6% from the agent's price: a pullback.
    assert (first["entry_type"], first["warnings"], first["not_evaluated"]) == (
        "PULLBACK", [], [])
    assert second["not_evaluated"][0] == {"check": "PRICE_MATCH",
                                          "reason": "LIVE_PRICE_UNAVAILABLE"}  # No CCC quote.
    # The context's own cached reads: nothing more than the context GET above made.
    assert (feeds.count("assets"), feeds.count("quotes"), feeds.count("trades")) == (1, 1, 1)
    assert table_counts(repo) == before
    assert web.post(ROUTE, json=raw, headers=bearer(LEGACY)).json()["item_results"] == body[
        "item_results"]


# --- The guidelines route ------------------------------------------------------------------------

def test_the_guidelines_are_the_text_the_context_names(context_lab):
    lab = context_lab
    web = context_client(silent_cycle(lab.repo, lambda: lab.now[0]), ManagedStore(lab.repo),
                         lab.service)
    context = web.get("/api/v1/lab/research-context", headers=CLAUDE).json()
    reply = web.get(GUIDELINES, headers=CLAUDE)
    assert reply.status_code == 200 and reply.headers["Cache-Control"] == "no-store"
    body = reply.json()
    assert list(body) == ["route_version", "guidelines_version", "guidelines_sha256", "text",
                          "trade_authorized"]
    assert (body["route_version"], body["trade_authorized"]) == (
        "RESEARCH_GUIDELINES_ROUTE_V1", False)
    fmt = context["report_format"]
    assert (body["guidelines_version"], body["guidelines_sha256"]) == (
        fmt["guidelines_version"], fmt["guidelines_sha256"])
    assert hashlib.sha256(body["text"].encode("utf-8")).hexdigest() == body["guidelines_sha256"]
    # The pinned V6 text and hash, served from the package itself (the image copies src/).
    assert (body["guidelines_version"], body["text"]) == (
        MUSE_GUIDELINES_V6_VERSION, MUSE_GUIDELINES_V6)
    assert body["guidelines_sha256"] == MUSE_GUIDELINES_V6_SHA256 == (
        "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d")
    assert Path(muse_guidelines.__file__).resolve().is_relative_to(ROOT / "src" / "catalyst_lab")


def test_the_guidelines_need_a_credential(lab):
    web = client(lab)
    assert web.get(GUIDELINES).status_code == 401
    assert web.get(GUIDELINES, headers=bearer("y" * 40)).status_code == 401
    reply = web.get(GUIDELINES, headers=bearer(OPERATOR))
    assert (reply.status_code, reply.json()) == (403, {"detail": "TOKEN_ROLE_NOT_PERMITTED"})
    for token in (AGENTS["claude"], AGENTS["instinct"], LEGACY, STATUS):
        assert web.get(GUIDELINES, headers=bearer(token)).json()["text"] == MUSE_GUIDELINES_V6
