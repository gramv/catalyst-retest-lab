"""docs/MUSE-CONNECTION.md stays honest (package muse-connection; package agent-api).

Every fenced block tagged ``json muse-example:<name>`` is a request body a research agent sends;
each is validated here by the app's own models and parsers, the way the tests of those modules
do: the report-V3 parser, the per-pick checks, the pick dossier compiler and the system check;
the review-answer and exit-flag models; the position-news parser. Every block tagged
``json muse-response:<name>`` must be exactly what the app builds, except the pending-reviews
example, whose every object must have exactly the keys the app's own items have. The route
table, the limits table, the deployed settings the guide quotes and every link are checked too.

Fixture evidence only: pure checks, a stub application for route and size probes, and
disposable PostgreSQL databases for the real intake, the dry run, the research context and the
reviews. No broker, provider, network or owner-ledger contact.
"""

import asyncio
import copy
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import annotated_types
import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from catalyst_lab import crypto_holding, exit_flags, trade_review
from catalyst_lab import day_review as dr
from catalyst_lab import system_check as sc
from catalyst_lab.agent_identity import agent_cycle_id
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.learning_intake import (
    MAX_OUTLOOK_COINS,
    OUTLOOK_BODY_LIMIT,
    POST_MORTEM_BODY_LIMIT,
    LearningIntake,
    LearningPolicy,
    PostMortemEnvelope,
    PostMortemItem,
)
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_V6_SHA256, MUSE_GUIDELINES_V6_VERSION
from catalyst_lab.muse_reports import RationaleClaim, SelectionRationale
from catalyst_lab.position_news import PositionNewsService
from catalyst_lab.research_dossier import (
    JEV_STATE_CAP_BYTES,
    RATIONALE_BUDGET_BYTES,
    STATE_BUDGET_BYTES,
)
from catalyst_lab.research_dossier_v3 import compile_pick_dossier
from catalyst_lab.research_report_v3 import (
    MAX_PICKS,
    MAX_SKIPPED,
    MAX_VALIDITY,
    TARGET_PICKS,
    AgentPick,
    PickTechnicalEvidence,
    ReportEnvelopeV3,
    SkippedCoin,
    UniverseSnapshot,
    check_pick,
    declared_agent,
    parse_report_v3,
)
from catalyst_lab.research_schedule import ResearchSchedule
from catalyst_lab.research_validate import VALIDATIONS_PER_MINUTE, ReportValidator, ValidateLimiter
from catalyst_lab.research_withdrawal import (
    WITHDRAWAL_BODY_LIMIT,
    ResearchWithdrawal,
    ResearchWithdrawals,
    WithdrawalItem,
    parse_withdrawal,
)
from catalyst_lab.research_withdrawal import declared_agent as withdrawal_agent
from catalyst_lab.review_storage import SourceExcerpt
from tests.day_review_fixtures import AGENTS as REVIEW_AGENTS
from tests.day_review_fixtures import managed_arm as managed_arm
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_research_report_v3 import lab as lab
from tests.test_research_report_v3 import v3_intake

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "MUSE-CONNECTION.md"
EXAMPLES = {"report-v3-chart", "review-answer", "exit-flag-answer", "exit-flag", "position-news",
            "market-outlook", "post-mortem", "research-withdrawal",
            # Package agent-api: a NEWS pick and a BOTH pick.
            "report-v3-news", "report-v3-both"}
RESPONSES = {"report-receipt", "review-answer-receipt", "exit-flag-answer-receipt",
             "exit-flag-receipt", "position-news-receipt", "market-outlook-receipt",
             "post-mortem-receipt", "research-withdrawal-receipt",
             # Package agent-api: the dry run, a new agent's research context, pending reviews.
             "report-validation", "validation-warnings", "research-context", "reviews"}
# The schedule the guide's examples were written for: one 08:00 run a day. The deployed one
# (section 5) is RESEARCH_SCHEDULE_V2 from 2026-09-29: that daily run plus an update run at
# every other even hour (every 2 hours, all full runs, ran 2026-09-28).
SCHEDULE = ResearchSchedule("America/New_York", ("08:00",), 60)
DEPLOYED_SCHEDULE = ResearchSchedule(
    "America/New_York", tuple(f"{hour:02d}:00" for hour in range(0, 24, 2)), 60, "08:00")
# The report example's fixture context: the moment it arrives, its universe and live price.
ARRIVAL = datetime(2026, 9, 28, 12, 10, 5, tzinfo=UTC)
UNIVERSE = frozenset({"SOL/USD", "BTC/USD", "ETH/USD", "DOGE/USD"})
LIVE = sc.LiveQuote("SOL/USD", D("150.08"), D("150.14"), ARRIVAL, sc.STREAM_SOURCE, ARRIVAL)
# The report examples' coins at arrival: bid, ask and Alpaca's price increment.
MARKET = {"SOL/USD": ("150.08", "150.14", "0.01"), "ETH/USD": ("4118.2", "4118.8", "0.1"),
          "BTC/USD": ("112350", "112380", "1")}
# The limits the routes enforce inline (probed below, not imported).
REPORT_BODY_LIMIT = 1_048_576
SMALL_BODY_LIMIT = 32_768
EXCERPT_TOTAL_LIMIT = 8_000
# The deployment's trade window (deploy/private-paper.example.json, which the cloud copies).
DEPLOYED_WINDOW = json.loads((ROOT / "deploy" / "private-paper.example.json").read_text())[
    "environment"]["MANAGED_CRYPTO_WINDOW_JSON"]
CLAUDE = REVIEW_AGENTS["claude"]  # The review fixtures' agent token.
# Fixture tokens for the probes (32+ characters, no whitespace).
LEGACY = "fixture-muse-guide-legacy-token-" + "l" * 16
STATUS = "fixture-muse-guide-status-token-" + "s" * 16
OPERATOR = "fixture-muse-guide-operator-token-" + "o" * 16
MUSE = "fixture-muse-guide-muse-agent-token-" + "m" * 16


# --- Reading the guide ---------------------------------------------------------------------------


def guide():
    if not GUIDE.exists():
        pytest.skip("history doc not shipped in the public export")
    return GUIDE.read_text(encoding="utf-8")


def blocks(tag):
    """``{name: parsed JSON}`` of every ```` ```json <tag>:<name> ```` block, names unique."""
    found = re.findall(r"^```json " + tag + r":([a-z0-9-]+)\n(.*?)\n```$", guide(), re.M | re.S)
    names = [name for name, _ in found]
    assert len(names) == len(set(names)), names
    return {name: strict_json(text) for name, text in found}


def examples():
    return blocks("muse-example")


def responses():
    return blocks("muse-response")


def marked(name):
    """The text between ``<!-- name:start -->`` and ``<!-- name:end -->``."""
    [section] = re.findall(rf"<!-- {name}:start -->\n(.*?)<!-- {name}:end -->", guide(), re.S)
    return section


# The research-agent routes, frozen here so the token-scope test still runs where the guide is
# not shipped (the public export); the guide's section 3 table must list exactly these.
MUSE_ROUTES = frozenset({
    ("GET", "/api/v1/lab/cycles/{cycle_id}/outputs"), ("GET", "/api/v1/lab/outputs"),
    ("GET", "/api/v1/lab/positions"), ("GET", "/api/v1/lab/positions/{setup_id}/news"),
    ("GET", "/api/v1/lab/research-context"), ("GET", "/api/v1/lab/research-guidelines"),
    ("GET", "/api/v1/lab/reviews"), ("POST", "/api/v1/lab/cycles/{cycle_id}/evidence"),
    ("POST", "/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim"),
    ("POST", "/api/v1/lab/exit-flags/{flag_id}/answer"), ("POST", "/api/v1/lab/market-outlooks"),
    ("POST", "/api/v1/lab/positions/{setup_id}/exit-flag"),
    ("POST", "/api/v1/lab/positions/{setup_id}/news"), ("POST", "/api/v1/lab/post-mortems"),
    ("POST", "/api/v1/lab/research-reports"), ("POST", "/api/v1/lab/research-reports/validate"),
    ("POST", "/api/v1/lab/research-withdrawals"),
    ("POST", "/api/v1/lab/reviews/{review_id}/answer"),
})


def test_the_guide_lists_exactly_the_frozen_research_agent_routes():
    assert documented_routes() == MUSE_ROUTES


def documented_routes():
    """The routes of the guide's section 3 table, as ``(method, path template)``."""
    rows = re.findall(r"^\| `(GET|POST) (/api/v1/lab/[^`]+)` \|", marked("muse-routes"), re.M)
    assert len(rows) == len(set(rows))
    return set(rows)


def table(name):
    """``{first cell: second cell}`` of a marked two-column table, header rows dropped."""
    rows = [line.split("|")[1:-1] for line in marked(name).splitlines() if line.startswith("|")]
    return {first.strip(): second.strip() for first, second in rows[2:]}


# --- A stub application: every route, no database -----------------------------------------------


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])


def stub_app(*, legacy=LEGACY, status=STATUS, operator=OPERATOR, agent_tokens=None):
    """The real route table and authentication over stubs. A handler that needs more than the
    stubs answers 500 (never 401 or 403), so it still shows that the route was reachable."""
    repo = SimpleNamespace(require_same_database=lambda other: None, connect=_Conn)
    store = SimpleNamespace(repo=repo, outputs=lambda after, limit, exclude_kinds=(): [])
    cycle = SimpleNamespace(repo=repo, outputs=lambda cycle_id, after, limit: [],
                            claim_evidence_tasks=lambda cycle_id, **body: [])

    async def submit(payload):
        return {"status": "FIXTURE_REPORT_RECORDED"}

    async def validate(payload):
        return {"status": "FIXTURE_REPORT_VALIDATED"}

    news = SimpleNamespace(context=lambda setup_id: {}, submit=lambda *a, **k: {})
    learning = SimpleNamespace(submit_outlook=lambda body: {},
                               submit_post_mortem=lambda body, principal: {})
    withdrawals = SimpleNamespace(withdraw=lambda body, principal: {})
    app = create_managed_app(
        cycle, store, api_token=legacy, runtime_status=lambda: {}, report_submit=submit,
        position_news=news, status_token=status, operator_token=operator,
        agent_tokens=agent_tokens if agent_tokens is not None else {"muse": MUSE},
        learning_intake=learning, research_withdrawals=withdrawals, report_validate=validate,
        validate_limiter=ValidateLimiter(10_000))
    return app, TestClient(app, raise_server_exceptions=False)


def api_routes(app):
    return sorted((method, route.path) for route in app.routes
                  if isinstance(route, APIRoute) and route.path.startswith("/api/")
                  for method in route.methods)


def concrete(path):
    return re.sub(r"\{[a-z_]+\}", lambda _: str(uuid4()), path)


def reachable_routes(token, **tokens):
    """``(reachable, refused)``: every authenticated route of the app, probed with ``token``.
    Refused means 403 ``TOKEN_ROLE_NOT_PERMITTED``; anything else means the role may call it."""
    app, client = stub_app(**tokens)
    reachable, refused = set(), set()
    for method, path in api_routes(app):
        reply = client.request(method, concrete(path), json={},
                               headers={"Authorization": "Bearer " + token})
        assert reply.status_code != 401, (method, path)
        if reply.status_code == 403 and reply.json() == {"detail": "TOKEN_ROLE_NOT_PERMITTED"}:
            refused.add((method, path))
        else:
            reachable.add((method, path))
    return reachable, refused


# --- The examples --------------------------------------------------------------------------------


def test_the_guide_carries_exactly_the_expected_examples_and_receipts():
    assert set(examples()) == EXAMPLES
    assert set(responses()) == RESPONSES


def checked_report(name="report-v3-chart"):
    """A report example through intake exactly as ``ResearchCycle._start_report_v3`` checks
    it at ``ARRIVAL``: envelope, per-pick checks and the REVIEW_DOSSIER_V3 compile."""
    raw = examples()[name]
    intake = parse_report_v3(raw, schedule=SCHEDULE)
    expires = min(intake.valid_until, intake.generated_at + timedelta(seconds=86400))
    universe = UniverseSnapshot(UNIVERSE, ARRIVAL, "LAB_FIXTURE_UNIVERSE")
    [item] = intake.picks
    item, expiry = check_pick(item, intake=intake, universe=universe, expires_at=expires,
                              now=ARRIVAL)
    assert (item.code, item.errors) == (None, ())
    dossier = compile_pick_dossier(item, now=ARRIVAL, agent_id="muse",
                                   valid_until=intake.canonical["valid_until"])
    return raw, intake, item, expiry, dossier


def test_the_report_example_passes_intake_the_dossier_and_the_system_check():
    raw, intake, item, expiry, dossier = checked_report()
    assert declared_agent(raw)["agent_id"] == "muse"
    assert raw["agent"]["guidelines_version"] == MUSE_GUIDELINES_V6_VERSION
    assert raw["agent"]["guidelines_sha256"] == MUSE_GUIDELINES_V6_SHA256
    assert 0 <= (ARRIVAL - intake.generated_at).total_seconds() <= 60  # The report's age.
    assert intake.context_as_of <= intake.generated_at
    assert intake.valid_until == min(intake.generated_at + MAX_VALIDITY,
                                     SCHEDULE.validity_limit(intake.run_slot))
    pick = item.pick
    assert (pick.kind, pick.sources, len(pick.technical_evidence.bars)) == ("CHART", [], 20)
    assert expiry == intake.valid_until
    assert dossier.manifest["state_bytes"] <= STATE_BUDGET_BYTES
    assert dossier.manifest["rationale"]["bytes"] <= RATIONALE_BUDGET_BYTES
    # Blind: the agent ID never reaches Jev as a word. (The bar format's wire name,
    # MUSE_OBSERVED_TECHNICALS_V1, does: every agent's chart pick carries it.)
    assert not re.search(r"(?<![A-Za-z0-9_])muse(?![A-Za-z0-9_])", json.dumps(dossier.state),
                         re.IGNORECASE)
    # The levels the system checks after Jev's selection: a pullback on the price increment,
    # the stop at least 2% under max entry, 2R or more at max entry, the stated ratio.
    levels = {k: D(v) for k, v in raw["picks"][0]["levels"].items()}
    assert levels["stop"] < levels["entry_trigger"] <= levels["max_entry_price"] < levels[
        "target"]
    assert all(value % D("0.01") == 0 for value in levels.values())
    reward_risk = (levels["target"] - levels["max_entry_price"]) / (
        levels["max_entry_price"] - levels["stop"])
    assert reward_risk >= 2 and round(reward_risk, 2) == D(raw["picks"][0]["stated_reward_risk"])
    packet = {"market": "CRYPTO", "run_slot": intake.canonical["run_slot"],
              "levels": item.canonical["levels"], "state": dossier.state}
    check = sc.evaluate(packet, LIVE, now=ARRIVAL)
    assert (check["result"], check["code"], check["entry_type"]) == ("PASSED", None, "PULLBACK")


def test_the_report_example_is_accepted_by_the_route_with_the_documented_receipt(lab):
    """The real intake on a disposable ledger, with Muse's token: 202 and exactly the receipt
    section 9.2 shows."""
    raw = examples()["report-v3-chart"]
    lab.now[0] = ARRIVAL
    web = TestClient(create_managed_app(
        lab.cycle, lab.cycle.store, api_token=LEGACY, runtime_status=lambda: {},
        status_token=STATUS, operator_token=OPERATOR, agent_tokens={"muse": MUSE},
        report_submit=lambda body: asyncio.to_thread(
            lab.cycle.start_report, body, max_seconds=86400,
            v3=v3_intake(universe=UNIVERSE, now=ARRIVAL))))
    reply = web.post("/api/v1/lab/research-reports", json=raw,
                     headers={"Authorization": "Bearer " + MUSE})
    assert reply.status_code == 202, reply.text
    receipt = reply.json()
    assert receipt == responses()["report-receipt"]
    assert receipt["cycle_id"] == agent_cycle_id("muse", raw["report_id"])
    again = web.post("/api/v1/lab/research-reports", json=raw,
                     headers={"Authorization": "Bearer " + MUSE})
    assert again.json() == {**receipt, "idempotent_replay": True}  # An exact retry replays.


def test_the_review_answer_examples_are_valid_answers():
    found = examples()
    now = datetime(2026, 9, 29, 13, 5, 12, tzinfo=UTC)
    answer = dr.validate_review_answer(found["review-answer"], agent_id="muse", now=now,
                                       suggestions_allowed=True)
    assert (answer["decision"], answer["suggested_stop"], len(answer["sources"])) == (
        "CONTINUE", "149.10", 1)
    flag_answer = dr.validate_review_answer(found["exit-flag-answer"], agent_id="muse",
                                            now=now, suggestions_allowed=False)
    assert (flag_answer["decision"], flag_answer["sources"]) == ("EXIT", [])
    # A Jev flag's answer may not suggest levels; the review answer's would be refused there.
    with pytest.raises(ValueError, match="^SUGGESTED_LEVELS_NOT_ALLOWED$"):
        dr.validate_review_answer(found["review-answer"], agent_id="muse", now=now,
                                  suggestions_allowed=False)


def test_the_exit_flag_example_is_a_valid_flag():
    flag = dr.validate_exit_flag(examples()["exit-flag"], agent_id="muse",
                                 now=datetime(2026, 9, 28, 20, 15, 2, tzinfo=UTC))
    assert flag["schema_version"] == dr.AGENT_EXIT_FLAG_VERSION
    assert [s["stance"] for s in flag["sources"]] == ["ADVERSE"]


def test_the_position_news_example_passes_the_parser():
    class Reached(Exception):
        pass

    class Store:
        def transaction(self):
            raise Reached  # Past every check that needs no ledger.

    service = PositionNewsService(Store(),
                                  clock=lambda: datetime(2026, 9, 28, 18, 5, tzinfo=UTC))
    with pytest.raises(Reached):
        service.submit(str(uuid4()), examples()["position-news"], agent_id="muse")


def test_the_other_receipts_are_exactly_what_the_app_builds():
    found = responses()
    review = found["review-answer-receipt"]
    assert trade_review.TradeReviewService._ack({"body": {
        "review_id": review["review_id"], "round": review["round"],
        "answer": {"decision": review["decision"]}, "received_at": review["received_at"],
    }}, replay=False) == review
    flag_answer = found["exit-flag-answer-receipt"]
    assert trade_review.TradeReviewService._ack({"body": {
        "flag_id": flag_answer["flag_id"], "answer": {"decision": flag_answer["decision"]},
        "received_at": flag_answer["received_at"],
    }}, replay=False) == flag_answer
    flag = found["exit-flag-receipt"]
    assert trade_review.TradeReviewService._flag_ack({"body": {
        key: flag[key] for key in ("flag_id", "setup_id", "raised_at", "answer_due_at")
    }}, created=True, replay=False) == flag
    window = datetime.fromisoformat(flag["answer_due_at"]) - datetime.fromisoformat(
        flag["raised_at"])
    assert window == timedelta(seconds=exit_flags.ANSWER_WINDOW_SECONDS)
    news = found["position-news-receipt"]
    assert PositionNewsService._ack({"event_seq": news["news_revision"],
                                     "body": {"evidence_hash": news["evidence_hash"]}},
                                    replay=False) == news


def test_the_learning_examples_are_recorded_with_the_documented_receipts(lab):
    """Package learning-app: the outlook and the post-mortem through the real intake on a
    disposable ledger at the documented arrival, with a recorded reality for the movers."""
    store = lab.cycle.store
    intake = LearningIntake(store, clock=lambda: datetime(2026, 9, 28, 12, 10, 5, tzinfo=UTC),
                            policy=LearningPolicy(max_age_seconds=60, schedule=SCHEDULE),
                            universe=lambda: UniverseSnapshot(UNIVERSE, ARRIVAL,
                                                              "LAB_FIXTURE_UNIVERSE"))
    web = TestClient(create_managed_app(
        lab.cycle, store, api_token=LEGACY, runtime_status=lambda: {}, status_token=STATUS,
        operator_token=OPERATOR, agent_tokens={"muse": MUSE}, learning_intake=intake))
    headers = {"Authorization": "Bearer " + MUSE}
    found, expected = examples(), responses()
    reply = web.post("/api/v1/lab/market-outlooks", json=found["market-outlook"],
                     headers=headers)
    assert reply.status_code == 202, reply.text
    receipt = reply.json()
    assert receipt == {**expected["market-outlook-receipt"], "event_seq": receipt["event_seq"]}
    movers = [
        {"symbol": "SOL/USD", "return_pct": "7.0000", "move_start_at": "2026-09-27T14:00:00+00:00",
         "top_up": True, "top_down": False, "big_move": True, "calls": {}, "missed_by": []},
        {"symbol": "DOGE/USD", "return_pct": "-6.0000",
         "move_start_at": "2026-09-27T19:30:00+00:00", "top_up": False, "top_down": True,
         "big_move": True, "calls": {}, "missed_by": []},
    ]
    with store.transaction() as conn:
        store.event(conn, "MARKET_REALITY", {"reality_version": "MARKET_REALITY_V1",
                                             "day": "2026-09-27", "movers": movers},
                    key="market-reality:2026-09-27")
    reply = web.post("/api/v1/lab/post-mortems", json=found["post-mortem"], headers=headers)
    assert reply.status_code == 202, reply.text
    receipt = reply.json()
    assert receipt == {**expected["post-mortem-receipt"], "event_seq": receipt["event_seq"]}


def test_the_withdrawal_example_is_recorded_with_the_documented_response(mx):
    """Package research-loop-app: the example through the real service on a disposable ledger,
    with Muse's own SOL/USD setup watching; the response is the documented one but for the
    ledger's own setup ID."""
    from tests.test_system_check import at_price, publish_v3, two_slots, v3_pick

    engine, venue, _ = mx
    raw = examples()["research-withdrawal"]
    withdrawal, _ = parse_withdrawal(raw)
    assert withdrawal_agent(raw) == {"agent_id": "muse", "agent_version": "muse-2026.09.29"}
    assert [item.symbol for item in withdrawal.items] == ["SOL/USD", "DOGE/USD"]
    assert WITHDRAWAL_BODY_LIMIT == REPORT_BODY_LIMIT  # The report route's limit.
    old, _ = two_slots(venue.now)
    packet = publish_v3(mx, [v3_pick(0, "SOL/USD", venue.now)], run_slot=old,
                        agent_id="muse")["SOL/USD"]
    sid = engine.admit(packet, live_quote=at_price("100.49", "100.51", at=venue.now))
    web = TestClient(create_managed_app(
        SimpleNamespace(repo=engine.repo), engine.store, api_token=LEGACY,
        runtime_status=lambda: {}, status_token=STATUS, operator_token=OPERATOR,
        agent_tokens={"muse": MUSE},
        research_withdrawals=ResearchWithdrawals(engine.store, clock=lambda: venue.now)))
    reply = web.post("/api/v1/lab/research-withdrawals", json=raw,
                     headers={"Authorization": "Bearer " + MUSE})
    assert reply.status_code == 200, reply.text
    documented = responses()["research-withdrawal-receipt"]
    documented["results"][0]["setup_ids"] = [str(sid)]
    assert reply.json() == documented


# --- Package agent-api: NEWS and BOTH picks, the dry run, the context and the reviews -------------

# The research context of a new agent: the instant, a two-coin fixture universe (and one
# stablecoin it excludes), each coin's quote, last trade and 24 completed hourly bars.
CONTEXT_AS_OF = datetime(2026, 9, 28, 12, 5, 12, tzinfo=UTC)


class ContextFeeds:
    """The fixture Alpaca sources of the context example (asset list and market data)."""

    assets = [
        {"symbol": "BTC/USD", "class": "crypto", "status": "active", "tradable": True,
         "price_increment": "1", "min_order_size": "0.0001",
         "min_trade_increment": "0.000000001"},
        {"symbol": "SOL/USD", "class": "crypto", "status": "active", "tradable": True,
         "price_increment": "0.01", "min_order_size": "0.01",
         "min_trade_increment": "0.000000001"},
        {"symbol": "USDT/USD", "class": "crypto", "status": "active", "tradable": True,
         "price_increment": "0.0001", "min_order_size": "1",
         "min_trade_increment": "0.000000001"},
    ]
    quotes = {"BTC/USD": (112350, 112380), "SOL/USD": (150.08, 150.14)}
    trades = {"BTC/USD": 112362, "SOL/USD": 150.11}
    hourly = {"BTC/USD": (112000, 12.5, 112100.0), "SOL/USD": (150, 2500, 150.2)}

    def paper(self, request):
        assert (request.method, request.url.path) == ("GET", "/v2/assets")
        return httpx.Response(200, json=self.assets)

    def data(self, request):
        path, symbols = request.url.path, request.url.params["symbols"].split(",")
        if path.endswith("/latest/quotes"):
            return httpx.Response(200, json={"quotes": {
                s: {"t": "2026-09-28T12:05:11.482Z", "bp": self.quotes[s][0],
                    "ap": self.quotes[s][1]} for s in symbols}})
        if path.endswith("/latest/trades"):
            return httpx.Response(200, json={"trades": {
                s: {"t": "2026-09-28T12:05:10.917Z", "p": self.trades[s], "s": 0.5, "i": 7}
                for s in symbols}})
        assert path.endswith("/bars")
        end = datetime.fromisoformat(request.url.params["end"])
        start = end - timedelta(hours=24)
        return httpx.Response(200, json={"bars": {s: [
            {"t": (start + timedelta(hours=h)).isoformat(), "o": 1, "h": 1, "l": 1,
             "c": self.hourly[s][0], "v": self.hourly[s][1], "vw": self.hourly[s][2]}
            for h in range(24)] for s in symbols}, "next_page_token": None})


def context_service(repo, clock, schedule):
    from tests.test_research_context import service_for

    service, _ = service_for(repo, clock, ContextFeeds(), schedule=schedule)
    return service


def muse_app(lab, **services):
    return TestClient(create_managed_app(
        lab.cycle, lab.cycle.store, api_token=LEGACY, runtime_status=lambda: {},
        status_token=STATUS, operator_token=OPERATOR, agent_tokens={"muse": MUSE}, **services))


def pick_check(name):
    """A report example's one pick through intake, its dossier and the system check at
    ``ARRIVAL`` against ``MARKET``: the pick, its dossier and the check."""
    raw, intake, item, expiry, dossier = checked_report(name)
    bid, ask, _ = MARKET[item.pick.symbol]
    live = sc.LiveQuote(item.pick.symbol, D(bid), D(ask), ARRIVAL, sc.STREAM_SOURCE, ARRIVAL)
    packet = {"market": "CRYPTO", "run_slot": intake.canonical["run_slot"],
              "levels": item.canonical["levels"], "state": dossier.state}
    return raw, intake, item, dossier, sc.evaluate(packet, live, now=ARRIVAL)


@pytest.mark.parametrize(("name", "kind"), [("report-v3-news", "NEWS"),
                                            ("report-v3-both", "BOTH")])
def test_the_news_and_both_examples_pass_intake_the_dossier_and_the_system_check(name, kind):
    raw, intake, item, dossier, check = pick_check(name)
    pick = item.pick
    assert pick.kind == kind and pick.sources
    assert (pick.technical_evidence is not None) is (kind == "BOTH")
    assert (raw["agent"]["guidelines_version"], raw["agent"]["guidelines_sha256"]) == (
        MUSE_GUIDELINES_V6_VERSION, MUSE_GUIDELINES_V6_SHA256)
    assert 0 <= (ARRIVAL - intake.generated_at).total_seconds() <= 60
    assert dossier.manifest["state_bytes"] <= STATE_BUDGET_BYTES
    assert dossier.manifest["rationale"]["bytes"] <= RATIONALE_BUDGET_BYTES
    assert not re.search(r"(?<![A-Za-z0-9_])muse(?![A-Za-z0-9_])", json.dumps(dossier.state),
                         re.IGNORECASE)
    # The 48-hour catalyst rule: first made public within 48 hours before agent_price_at.
    for source in pick.sources:
        assert timedelta(0) <= pick.agent_price_at - source.published_at <= timedelta(hours=48)
    # Claims: CATALYST claims cite sources, TECHNICAL claims cite bars.
    for claim in raw["picks"][0]["selection_rationale"]["claims"]:
        cited = claim["supported_by"]
        if claim["kind"] in {"CATALYST", "NOVELTY"}:
            assert cited["source_ids"] and not cited["bar_ids"]
        if claim["kind"] == "TECHNICAL":
            assert cited["bar_ids"] and not cited["source_ids"]
    # The levels the system checks after Jev's selection, and the ones the dry run warns on.
    levels = {k: D(v) for k, v in raw["picks"][0]["levels"].items()}
    increment = D(MARKET[pick.symbol][2])
    assert levels["stop"] < levels["entry_trigger"] <= levels["max_entry_price"] < levels[
        "target"]
    assert all(value % increment == 0 for value in levels.values())
    reward_risk = (levels["target"] - levels["max_entry_price"]) / (
        levels["max_entry_price"] - levels["stop"])
    assert reward_risk >= 2 and round(reward_risk, 2) == D(raw["picks"][0]["stated_reward_risk"])
    assert (check["result"], check["code"], check["entry_type"]) == ("PASSED", None, "PULLBACK")
    if kind == "BOTH":  # Guidelines V4's levels, read from the bars themselves.
        bars = pick.technical_evidence.bars
        entry_bar = next(i for i, bar in enumerate(bars) if bar.low == levels["entry_trigger"])
        assert all(bar.low > levels["entry_trigger"] for bar in bars[entry_bar + 1:])
        lowest = min(bar.low for bar in bars)
        assert levels["stop"] == (lowest * D("0.996")).quantize(increment, rounding="ROUND_DOWN")
        assert levels["target"] == max(bar.high for bar in bars) != bars[-1].high


def test_the_news_and_both_examples_are_accepted_by_the_route(lab):
    lab.now[0] = ARRIVAL
    web = muse_app(lab, report_submit=lambda body: asyncio.to_thread(
        lab.cycle.start_report, body, max_seconds=86400,
        v3=v3_intake(universe=UNIVERSE, now=ARRIVAL)))
    for name in ("report-v3-news", "report-v3-both"):
        reply = web.post("/api/v1/lab/research-reports", json=examples()[name],
                         headers={"Authorization": "Bearer " + MUSE})
        assert reply.status_code == 202, reply.text
        assert [r["status"] for r in reply.json()["item_results"]] == ["ACCEPTED"]


def test_the_validation_examples_are_what_the_route_answers(lab):
    """The chart report's dry run at ``ARRIVAL`` against the context example's prices: exactly
    section 9.13's body, its ``item_results`` those of the receipt (section 9.2); then the same
    report, unchanged ``report_id``, with a short stop and an off-grid target."""
    lab.now[0] = ARRIVAL
    service = context_service(lab.cycle.repo, lambda: lab.now[0], SCHEDULE)
    validator = ReportValidator(lab.cycle, service, repo=lab.cycle.repo, max_seconds=86400)
    web = muse_app(lab, report_validate=lambda body: asyncio.to_thread(validator.validate, body))
    headers = {"Authorization": "Bearer " + MUSE}
    raw = examples()["report-v3-chart"]
    reply = web.post("/api/v1/lab/research-reports/validate", json=raw, headers=headers)
    assert reply.status_code == 200, reply.text
    assert reply.json() == responses()["report-validation"]
    assert reply.json()["item_results"] == responses()["report-receipt"]["item_results"]
    warned = copy.deepcopy(raw)
    warned["picks"][0]["levels"].update(stop="146.00", target="154.805")
    reply = web.post("/api/v1/lab/research-reports/validate", json=warned, headers=headers)
    assert reply.status_code == 200, reply.text  # Nothing was recorded under this report_id.
    [entry] = reply.json()["admission_warnings"]
    assert entry == responses()["validation-warnings"]
    assert reply.json()["item_results"][0]["status"] == "ACCEPTED"  # Intake checks no level.


def test_the_context_example_is_what_a_new_agent_reads(lab):
    service = context_service(lab.cycle.repo, lambda: CONTEXT_AS_OF, DEPLOYED_SCHEDULE)
    reply = muse_app(lab, research_context=service).get(
        "/api/v1/lab/research-context", headers={"Authorization": "Bearer " + MUSE})
    assert reply.status_code == 200, reply.text
    assert reply.json() == responses()["research-context"]
    assert examples()["report-v3-chart"]["context_as_of"] == "2026-09-28T12:05:12Z"


def same_keys(documented, real, path="$"):
    """Every documented object has exactly the real one's keys, recursively; a list is checked
    by its first element, and a documented non-empty list needs a non-empty real one."""
    if isinstance(documented, dict):
        assert isinstance(real, dict), path
        assert set(documented) == set(real), (path, sorted(set(documented) ^ set(real)))
        for key, value in documented.items():
            same_keys(value, real[key], f"{path}.{key}")
    elif isinstance(documented, list) and documented:
        assert isinstance(real, list) and real, path
        same_keys(documented[0], real[0], path + "[0]")


def reviews_item(kind):
    [item] = [i for i in responses()["reviews"]["items"] if i["kind"] == kind]
    return item


@pytest.mark.usefixtures("pre_jev_b1_admission")  # V4's maintenance (before jev-b1).
@pytest.mark.usefixtures("managed_arm")
def test_the_reviews_example_day_review_has_the_apps_keys(mt):
    """A window review request (the deployment's CRYPTO_WINDOW_REVIEW_V1, 4 hours) of a trade
    with one maintenance stop raise and the guide's own position news, 30 minutes before T."""
    from tests.day_review_fixtures import at_request, kit, open_trade, run
    from tests.maintenance_fixtures import maintainer, quote
    from tests.test_maintenance_replay import reviewed

    engine, venue, _ = mt
    engine.crypto_window = crypto_holding.parse_window_setting(DEPLOYED_WINDOW)
    sid = open_trade(mt)
    maintenance = maintainer(mt)
    maintenance.jev.answer("RAISE_STOP", stop="last")  # The lowest raise: options stay above.
    reviewed(mt, maintenance)
    for _ in range(4):
        engine.manage(sid, quote(mt, "106"))
    news = PositionNewsService(engine.store, clock=lambda: venue.now)
    context = news.context(str(sid))
    body = {**examples()["position-news"], "lifecycle_id": context["lifecycle_id"],
            "expected_news_revision": context["news_revision"]}
    news.submit(str(sid), body, agent_id="claude")
    k = kit(mt)
    at_request(mt, sid)
    run(mt, k, bid="106")
    reply = k.client.get("/api/v1/lab/reviews", headers={"Authorization": "Bearer " + CLAUDE})
    real = reply.json()
    documented = responses()["reviews"]
    same_keys({k: v for k, v in documented.items() if k != "items"},
              {k: v for k, v in real.items() if k != "items"})
    [item] = real["items"]
    same_keys(reviews_item("DAY_REVIEW"), item)
    assert (documented["poll_hint_seconds"], documented["request_lead_seconds"]) == (
        trade_review.POLL_HINT_SECONDS, crypto_holding.REQUEST_LEAD_SECONDS)
    # The documented clock is the app's: the request 30 minutes before T, the answer due at T,
    # the window 4 hours after the first fill.
    example = reviews_item("DAY_REVIEW")
    request = example["request"]
    assert (item["request"]["policy_id"], item["request"]["holding_window_seconds"]) == (
        request["policy_id"], request["holding_window_seconds"]) == (
        "CRYPTO_WINDOW_REVIEW_V1", 14400)
    at = {key: datetime.fromisoformat(request[key]) for key in (
        "review_at", "requested_at", "agent_answer_due_at")}
    assert at["review_at"] - at["requested_at"] == timedelta(
        seconds=crypto_holding.REQUEST_LEAD_SECONDS)
    assert example["answer_due_at"] == example["review_at"] == request["agent_answer_due_at"]
    assert at["review_at"] - datetime.fromisoformat(request["trade"]["opened_at"]) == timedelta(
        seconds=14400)
    assert item["answer_due_at"] == item["review_at"]


@pytest.mark.usefixtures("pre_jev_b1_admission")  # V4's maintenance (before jev-b1).
@pytest.mark.usefixtures("managed_arm")
def test_the_reviews_example_exit_flag_has_the_apps_keys(mt):
    from tests.day_review_fixtures import kit, open_trade, run
    from tests.test_early_exit import jev_flags

    engine, _, _ = mt
    engine.crypto_window = crypto_holding.parse_window_setting(DEPLOYED_WINDOW)
    sid = open_trade(mt)
    k = kit(mt)
    jev_flags(mt, k, sid)
    run(mt, k, bid="106")
    real = k.client.get("/api/v1/lab/reviews", headers={"Authorization": "Bearer " + CLAUDE})
    [item] = real.json()["items"]
    example = reviews_item("EXIT_FLAG")
    same_keys(example, item)
    window = datetime.fromisoformat(example["answer_due_at"]) - datetime.fromisoformat(
        example["raised_at"])
    assert window == timedelta(seconds=exit_flags.ANSWER_WINDOW_SECONDS)


# --- Routes, limits, settings and links ----------------------------------------------------------


def test_the_route_table_is_exactly_what_a_research_agent_token_reaches():
    reachable, refused = reachable_routes(MUSE)
    assert reachable == documented_routes()
    assert refused and not reachable & refused
    # The status token reads (GET only); the operator token reaches nothing yet.
    status_reachable, _ = reachable_routes(STATUS)
    assert all(method == "GET" for method, _ in status_reachable)
    assert reachable_routes(OPERATOR)[0] == set()


def bounds(model, name):
    """(minimum, maximum) of a pydantic field's length or value constraints."""
    metadata = model.model_fields[name].metadata
    low = next((m.min_length for m in metadata if isinstance(m, annotated_types.MinLen)),
               next((m.ge for m in metadata if isinstance(m, annotated_types.Ge)), None))
    high = next((m.max_length for m in metadata if isinstance(m, annotated_types.MaxLen)),
                next((m.le for m in metadata if isinstance(m, annotated_types.Le)), None))
    return low, high


def test_the_limits_table_is_the_codes():
    picks, skipped = bounds(ReportEnvelopeV3, "picks"), bounds(ReportEnvelopeV3, "skipped")
    bars, frame = (bounds(PickTechnicalEvidence, "bars"),
                   bounds(PickTechnicalEvidence, "timeframe_seconds"))
    claims = bounds(SelectionRationale, "claims")
    assert picks == (1, MAX_PICKS) and skipped[1] == MAX_SKIPPED
    assert table("muse-limits") == {
        "Picks per report": f"{picks[0]}–{picks[1]} (target {TARGET_PICKS})",
        "Skipped coins per report":
            f"0–{skipped[1]}, reason up to {bounds(SkippedCoin, 'reason')[1]} characters",
        "Report body": f"{REPORT_BODY_LIMIT:,} bytes",
        "Reviewed JSON per pick":
            f"{STATE_BUDGET_BYTES:,} bytes, rationale {RATIONALE_BUDGET_BYTES:,}",
        "Sources per pick": f"0–{bounds(AgentPick, 'sources')[1]}, excerpt up to "
                            f"{bounds(SourceExcerpt, 'excerpt')[1]:,} characters, "
                            f"{EXCERPT_TOTAL_LIMIT:,} in total",
        "Bars per chart pick":
            f"{bars[0]}–{bars[1]}, each timeframe {frame[0]}–{frame[1]:,} seconds",
        "Rationale claims": f"{claims[0]}–{claims[1]}, text up to "
                            f"{bounds(RationaleClaim, 'text')[1]} characters",
        "Report validity":
            f"up to {MAX_VALIDITY // timedelta(hours=1)} hours after `generated_at`",
        "News, answer or flag body": f"{SMALL_BODY_LIMIT:,} bytes",
        "Answer or flag JSON": f"{JEV_STATE_CAP_BYTES:,} bytes",
        "Review request lead": f"{crypto_holding.REQUEST_LEAD_SECONDS:,} seconds before T",
        "Discussion reply": f"{crypto_holding.AGENT_REPLY_SECONDS:,} seconds",
        "Exit-flag answer": f"{exit_flags.ANSWER_WINDOW_SECONDS:,} seconds",
        "Poll hint": f"{trade_review.POLL_HINT_SECONDS} seconds",
        "Outlook body": f"{OUTLOOK_BODY_LIMIT:,} bytes",
        "Outlook coins": f"one per universe coin, at most {MAX_OUTLOOK_COINS}",
        "Post-mortem body": f"{POST_MORTEM_BODY_LIMIT:,} bytes",
        "Post-mortem items": f"{bounds(PostMortemEnvelope, 'items')[0]}–"
                             f"{bounds(PostMortemEnvelope, 'items')[1]}, summary up to "
                             f"{bounds(PostMortemItem, 'summary')[1]} characters, "
                             f"0–{bounds(PostMortemItem, 'sources')[1]} sources",
        "Withdrawal body": f"{WITHDRAWAL_BODY_LIMIT:,} bytes",
        "Withdrawal items": f"{bounds(ResearchWithdrawal, 'items')[0]}–"
                            f"{bounds(ResearchWithdrawal, 'items')[1]}, reason up to "
                            f"{bounds(WithdrawalItem, 'reason')[1]} characters",
        "Report validations": f"{VALIDATIONS_PER_MINUTE} a minute per credential, one at a "
                              f"time; body {REPORT_BODY_LIMIT:,} bytes",
    }


@pytest.mark.parametrize(("route", "limit", "code", "invalid"), [
    ("/api/v1/lab/research-reports", REPORT_BODY_LIMIT, "RESEARCH_REPORT_TOO_LARGE",
     "INVALID_MUSE_REPORT"),
    ("/api/v1/lab/positions/{setup_id}/news", SMALL_BODY_LIMIT, "EVIDENCE_TOO_LARGE",
     "INVALID_POSITION_NEWS"),
    ("/api/v1/lab/positions/{setup_id}/exit-flag", SMALL_BODY_LIMIT, "REVIEW_ANSWER_TOO_LARGE",
     "INVALID_REVIEW_ANSWER"),
    ("/api/v1/lab/reviews/{review_id}/answer", SMALL_BODY_LIMIT, "REVIEW_ANSWER_TOO_LARGE",
     "INVALID_REVIEW_ANSWER"),
    ("/api/v1/lab/exit-flags/{flag_id}/answer", SMALL_BODY_LIMIT, "REVIEW_ANSWER_TOO_LARGE",
     "INVALID_REVIEW_ANSWER"),
    ("/api/v1/lab/market-outlooks", OUTLOOK_BODY_LIMIT, "MARKET_OUTLOOK_TOO_LARGE",
     "INVALID_MARKET_OUTLOOK"),
    ("/api/v1/lab/post-mortems", POST_MORTEM_BODY_LIMIT, "POST_MORTEM_TOO_LARGE",
     "INVALID_POST_MORTEM"),
    ("/api/v1/lab/research-withdrawals", REPORT_BODY_LIMIT, "RESEARCH_WITHDRAWAL_TOO_LARGE",
     "INVALID_RESEARCH_WITHDRAWAL"),
    ("/api/v1/lab/research-reports/validate", REPORT_BODY_LIMIT, "RESEARCH_REPORT_TOO_LARGE",
     "INVALID_MUSE_REPORT"),
], ids=["report", "news", "flag", "review_answer", "flag_answer", "outlook", "post_mortem",
        "withdrawal", "validate"])
def test_the_body_limits_are_the_routes(route, limit, code, invalid):
    _, client = stub_app()
    headers = {"Authorization": "Bearer " + MUSE, "Content-Type": "application/json"}
    path = concrete(route)
    at_limit = client.post(path, content=b" " * limit, headers=headers)
    assert (at_limit.status_code, at_limit.json()) == (422, {"detail": invalid})
    over = client.post(path, content=b" " * (limit + 1), headers=headers)
    assert (over.status_code, over.json()) == (413, {"detail": code})


def test_the_excerpt_total_and_the_answer_size_are_the_models():
    pick = examples()["report-v3-chart"]["picks"][0]
    retrieved = "2026-09-28T12:00:00Z"
    sizes = [1143] * 7  # 8,001 characters in total, each excerpt within its 1,200.
    over = [{"source_id": f"s{i}", "url": f"https://news.example.com/{i}",
             "excerpt": "x" * size, "published_at": None, "retrieved_at": retrieved}
            for i, size in enumerate(sizes)]
    with pytest.raises(ValidationError, match="EXCERPT_BUDGET_EXCEEDED"):
        AgentPick.model_validate({**pick, "sources": over})
    over[0]["excerpt"] = "x" * 1142  # Exactly 8,000.
    assert AgentPick.model_validate({**pick, "sources": over})
    answer = examples()["review-answer"]
    heavy = [{**answer["sources"][0], "source_id": f"s{i}", "excerpt": "y" * 1200}
             for i in range(8)]  # Every count and length within its own limit.
    full = {key: "z" * limit for key, limit in dr.TEXT_LIMITS.items()}
    with pytest.raises(ValueError, match="^EVIDENCE_TOO_LONG$"):
        dr.validate_review_answer({**answer, **full, "sources": heavy}, agent_id="muse",
                                  now=datetime(2026, 9, 29, 13, 5, tzinfo=UTC),
                                  suggestions_allowed=True)


def test_the_guide_quotes_the_deployed_settings():
    """Sections 4 and 5 quote deploy/private-paper.example.json, which the cloud copies."""
    env = json.loads((ROOT / "deploy" / "private-paper.example.json").read_text())["environment"]
    text = guide()
    assert f"`{env['MANAGED_RESEARCH_SCHEDULE_JSON']}`" in text
    assert ResearchSchedule.from_json(env["MANAGED_RESEARCH_SCHEDULE_JSON"]) == DEPLOYED_SCHEDULE
    assert "The examples were written for the daily 08:00 schedule" in text
    assert json.loads(env["MANAGED_CYCLE_POLICY_JSON"])["max_packet_age_seconds"] == 60
    assert env["MANAGED_REPORT_MAX_SECONDS"] == "86400"
    assert env["MANAGED_SELECTION_RULE"] == "JEV_TOP_K_SELECTION_V2"
    assert json.loads(env["MANAGED_TOPK_SELECTION_JSON"]) == {"k": 10}
    # Section 4f and open item 1: the example keeps the reviews switch off.
    assert env["MANAGED_MANAGEMENT_REVIEWS"] == "DISABLED"
    assert "`MANAGED_MANAGEMENT_REVIEWS=ENABLED`" in text
    # Package agent-api: the deployment itself (.railway/railway.ts overrides the example) runs
    # the reviews ENABLED, every 4 hours (CRYPTO_WINDOW_REVIEW_V1, the example's window).
    spec = (ROOT / ".railway" / "railway.ts").read_text()
    assert 'MANAGED_MANAGEMENT_REVIEWS: "ENABLED"' in spec
    assert json.loads(env["MANAGED_CRYPTO_WINDOW_JSON"]) == {
        "version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}
    assert f"`MANAGED_CRYPTO_WINDOW_JSON = {env['MANAGED_CRYPTO_WINDOW_JSON']}`" in text
    assert "reviews are ENABLED" in text and "every 4 hours" in text


def slug(heading):
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path):
    return {slug(line.lstrip("#")) for line in path.read_text(encoding="utf-8").splitlines()
            if re.match(r"#{1,6} ", line)}


def test_every_link_in_the_guide_resolves():
    links = re.findall(r"\]\(([^)\s]+)\)", guide())
    assert links
    for link in links:
        target, _, anchor = link.partition("#")
        path = GUIDE if not target else (GUIDE.parent / target)
        assert path.is_file(), link
        if anchor:
            assert anchor in anchors(path), link
