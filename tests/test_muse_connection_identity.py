"""Muse's credential for the deployed app (package muse-connection, docs/MUSE-CONNECTION.md).

The owner's architecture (2026-09-28): Muse is the only outside caller of the deployed app and
Jev stays inside it. The deployment gives Muse its own research-agent token as agent ``muse``
(``MANAGED_AGENT_TOKENS_JSON = {"muse": <token>}``, the only entry); the role tokens stay
separate and the legacy ``MANAGED_API_TOKEN`` is never issued. These tests prove, in exactly
that configuration, that Muse's token

* submits ``AGENT_RESEARCH_REPORT_V3`` with Muse's own declared ``agent_version``,
* sees and answers the 24-hour review of a trade it proposed,
* posts position news on that trade and raises its own early-exit flag, and
* reaches the research-agent routes only;

that no other research-agent credential is needed; that the unissued legacy token acts for the
same agent (so it must stay unissued); and why the agent ID must be exactly ``muse``.

Fixture evidence only: disposable PostgreSQL databases, the fixture paper venue, a mock Jev
transport and the real routes in process. No broker, provider, network or owner-ledger contact.
"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab import day_review as dr
from catalyst_lab.agent_identity import agent_cycle_id, validated_agent_tokens
from catalyst_lab.managed_app import AppSettings, configured_agents
from catalyst_lab.managed_classification import CLASSIFICATION_POLICY
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.position_news import PositionNewsService
from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
from catalyst_lab.research_dossier_v3 import identity_paths
from tests.day_review_fixtures import (
    answer_body,
    at_request,
    events,
    flag_body,
    kit,
    managed_arm,
    open_trade,
    run,
    source,
    state,
)
from tests.maintenance_fixtures import mt as mt
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_research_report_v3 import lab as lab
from tests.test_research_report_v3 import pick, report_v3, v3_intake

_ = managed_arm
ROUTE = "/api/v1/lab/research-reports"
# Fixture tokens only (32+ characters, no whitespace), one per role, as the deployment has them.
LEGACY = "fixture-muse-connection-legacy-token-" + "l" * 16
STATUS = "fixture-muse-connection-status-token-" + "s" * 16
OPERATOR = "fixture-muse-connection-operator-token-" + "o" * 16
MUSE = "fixture-muse-connection-muse-agent-token-" + "m" * 16
MUSE_VERSION = "muse-2026.09.28"  # Muse's own declared version, never LEGACY_UNDECLARED.
SETTINGS = AppSettings(8799, LEGACY, 86400, (), CLASSIFICATION_POLICY, (),
                       status_token=STATUS, operator_token=OPERATOR,
                       agent_tokens={"muse": MUSE})


def bearer(token):
    return {"Authorization": "Bearer " + token}


def deployed(cycle, store, **services):
    """The app exactly as the deployment configures its credentials."""
    return TestClient(create_managed_app(
        cycle, store, api_token=SETTINGS.token, runtime_status=lambda: {},
        status_token=SETTINGS.status_token, operator_token=SETTINGS.operator_token,
        agent_tokens=SETTINGS.agent_tokens, **services,
    ))


# --- The configuration ------------------------------------------------------------------------


def test_the_deployment_needs_one_research_agent_credential_and_it_is_muses():
    assert dict(SETTINGS.agent_tokens) == {"muse": MUSE}
    assert MUSE not in repr(SETTINGS)
    # The agents that answer reviews: Muse only. The legacy identity is agent ``muse`` too, so
    # no other research-agent credential has to exist for a review to reach Muse.
    assert configured_agents(SETTINGS) == {"muse"}
    assert configured_agents(replace(SETTINGS, agent_tokens=None)) == {"muse"}
    # Muse's token must differ from every role token; the legacy token cannot be left out.
    for reused in (LEGACY, STATUS, OPERATOR):
        with pytest.raises(ValueError, match="^SEPARATE_AGENT_TOKENS_REQUIRED$"):
            validated_agent_tokens({"muse": reused}, reserved=(LEGACY, STATUS, OPERATOR))
    with pytest.raises(ValueError, match="^REQUIRED_MANAGED_APP_CONFIGURATION_INVALID$"):
        replace(SETTINGS, token="")


# --- Report V3 with Muse's own version ----------------------------------------------------------


def test_muses_token_submits_report_v3_with_its_own_agent_version(lab):
    web = deployed(lab.cycle, lab.cycle.store, report_submit=lambda raw: asyncio.to_thread(
        lab.cycle.start_report, raw, max_seconds=SETTINGS.report_seconds, v3=v3_intake()))
    raw = report_v3([pick(0)], agent_id="muse")
    raw["agent"]["agent_version"] = MUSE_VERSION
    reply = web.post(ROUTE, json=raw, headers=bearer(MUSE))
    assert reply.status_code == 202, reply.text
    body = reply.json()
    assert (body["agent_id"], body["agent_version"]) == ("muse", MUSE_VERSION)
    assert body["cycle_id"] == agent_cycle_id("muse", raw["report_id"])
    assert body["report_schema_version"] == "AGENT_RESEARCH_REPORT_V3"
    assert [r["status"] for r in body["item_results"]] == ["ACCEPTED"]
    [started] = [e["body"] for e in lab.cycle.outputs(body["cycle_id"], limit=1000)
                 if e["kind"] == "RESEARCH_STARTED"]
    assert started["agent"]["agent_version"] == MUSE_VERSION
    # Muse's token acts for ``muse`` only: another agent's report, or one without an agent
    # block, is refused before intake with nothing stored.
    other = report_v3([pick(0)], agent_id="claude")
    unattributed = {k: v for k, v in report_v3([pick(0)], agent_id="muse").items()
                    if k not in {"schema_version", "agent"}}
    for refused in (other, unattributed):
        reply = web.post(ROUTE, json=refused, headers=bearer(MUSE))
        assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    assert lab.cycle.outputs(agent_cycle_id("claude", other["report_id"])) == []
    # The role tokens never report.
    for token in (STATUS, OPERATOR):
        assert web.post(ROUTE, json=raw, headers=bearer(token)).status_code == 403
    # The unissued legacy token is the same agent: it could report as ``muse`` too, which is
    # why the deployment never hands it out.
    legacy = report_v3([pick(1)], agent_id="muse")
    assert web.post(ROUTE, json=legacy, headers=bearer(LEGACY)).status_code == 202


# --- Reviews, position news and exit flags on Muse's own trade -----------------------------------


def reviews_app(mt, k):
    engine, venue, _ = mt
    cycle = ResearchCycle(engine.repo, k.reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    return deployed(cycle, engine.store, trade_reviews=k.service,
                    position_news=PositionNewsService(engine.store, clock=lambda: venue.now))


def pending(web, token):
    reply = web.get("/api/v1/lab/reviews", headers=bearer(token))
    assert reply.status_code == 200, reply.text
    return reply.json()["items"]


@pytest.mark.usefixtures("managed_arm")
def test_muses_token_sees_and_answers_the_24_hour_review_of_its_trade(mt):
    engine, _, _ = mt
    sid = open_trade(mt, agent_id="muse")
    k = kit(mt, agents=configured_agents(SETTINGS))
    web = reviews_app(mt, k)
    at_request(mt, sid)
    run(mt, k, bid="106")
    [request] = events(engine, dr.REQUESTED, sid)
    assert request["addressee"] == {"agent_id": "muse", "basis": "PROPOSING_AGENT"}
    [item] = pending(web, MUSE)
    assert (item["kind"], item["review_id"], item["round"]) == (
        "DAY_REVIEW", request["review_id"], "FIRST")
    assert item["answer_route"] == f"/api/v1/lab/reviews/{request['review_id']}/answer"
    assert pending(web, STATUS) == []  # The status token sees nothing to answer.
    assert pending(web, LEGACY) == [item]  # The unissued legacy token is agent muse as well.
    reply = web.post(item["answer_route"], json=answer_body(), headers=bearer(MUSE))
    assert reply.status_code == 200, reply.text
    ack = reply.json()
    assert (ack["status"], ack["round"], ack["decision"], ack["trade_authorized"]) == (
        "REVIEW_ANSWER_RECORDED", "FIRST", "CONTINUE", False)
    [recorded] = events(engine, dr.AGENT_ANSWER, sid)
    assert recorded["agent_id"] == "muse"
    assert pending(web, MUSE) == []  # Answered.


@pytest.mark.usefixtures("managed_arm")
def test_muses_token_answers_a_jev_exit_flag_on_its_trade_with_the_guides_example(mt):
    from tests.test_early_exit import jev_flags
    from tests.test_muse_connection_examples import examples

    engine, _, _ = mt
    sid = open_trade(mt, agent_id="muse")
    k = kit(mt, agents=configured_agents(SETTINGS))
    web = reviews_app(mt, k)
    flag = jev_flags(mt, k, sid)
    run(mt, k, bid="106")
    [asked] = events(engine, dr.FLAG_ASKED, sid)
    assert asked["addressee"] == {"agent_id": "muse", "basis": "PROPOSING_AGENT"}
    [item] = pending(web, MUSE)
    assert (item["kind"], item["flag_id"], item["raised_by"]) == (
        "EXIT_FLAG", flag["flag_id"], "JEV")
    reply = web.post(item["answer_route"], json=examples()["exit-flag-answer"],
                     headers=bearer(MUSE))
    assert reply.status_code == 200, reply.text
    assert (reply.json()["status"], reply.json()["decision"]) == (
        "EXIT_FLAG_ANSWER_RECORDED", "EXIT")
    [answered] = events(engine, dr.FLAG_AGENT_ANSWER, sid)
    assert answered["agent_id"] == "muse"


@pytest.mark.usefixtures("managed_arm")
def test_muses_token_posts_news_and_raises_an_exit_flag_on_its_trade(mt):
    engine, _, _ = mt
    sid = open_trade(mt, agent_id="muse")
    k = kit(mt, agents=configured_agents(SETTINGS))
    web = reviews_app(mt, k)
    route = f"/api/v1/lab/positions/{sid}/news"
    context = web.get(route, headers=bearer(MUSE)).json()
    assert context["lifecycle_id"] == state(mt, sid)["lifecycle_id"]
    news = {"news_id": str(uuid4()), "lifecycle_id": context["lifecycle_id"],
            "expected_news_revision": context["news_revision"],
            "sources": [source(mt, "Synthetic fixture: the coin's main exchange listed a new "
                                   "pair.", stance="SUPPORTS")]}
    reply = web.post(route, json=news, headers=bearer(MUSE))
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == "POSITION_NEWS_RECORDED"
    assert reply.json()["position_modified"] is False
    flag = web.post(f"/api/v1/lab/positions/{sid}/exit-flag",
                    json=flag_body(context["lifecycle_id"]), headers=bearer(MUSE))
    assert flag.status_code == 200, flag.text
    assert flag.json()["status"] == "EXIT_FLAG_RAISED"
    [raised] = events(engine, dr.FLAG_RAISED, sid)
    assert raised["side"] == "AGENT" and raised["raised_by"]["agent_id"] == "muse"
    # News that names the posting agent never reaches Jev's contexts.
    named = {**news, "news_id": str(uuid4()),
             "sources": [{**news["sources"][0], "source_id": "muse-note-1"}]}
    reply = web.post(route, json=named, headers=bearer(MUSE))
    assert (reply.status_code, reply.json()) == (422, {"detail": "AGENT_IDENTITY_IN_NEWS"})


# --- Route scope ---------------------------------------------------------------------------------


def test_muses_token_reaches_the_research_agent_routes_and_no_other():
    """Probes every authenticated route of the app with Muse's token. The routes Muse may use
    are exactly the ones docs/MUSE-CONNECTION.md lists (tests/test_muse_connection_examples.py
    checks that table against this same set)."""
    from tests.test_muse_connection_examples import documented_routes, reachable_routes

    reachable, refused = reachable_routes(MUSE, status=STATUS, operator=OPERATOR,
                                          legacy=LEGACY, agent_tokens={"muse": MUSE})
    assert reachable == documented_routes()
    # Among the refused reads: the picks and results readbacks and analytics (open item).
    for route in (("GET", "/api/v1/lab/status"), ("GET", "/api/v1/lab/cycles"),
                  ("GET", "/api/v1/lab/cycles/{cycle_id}/picks"),
                  ("GET", "/api/v1/lab/setups"), ("GET", "/api/v1/lab/results/picks"),
                  ("GET", "/api/v1/lab/analytics/research")):
        assert route in refused


# --- Why the agent ID is exactly ``muse`` --------------------------------------------------------


def test_only_the_id_muse_screens_the_name_muse_out_of_what_jev_reads():
    """The blindness screens look for the credential's own agent ID as a whole word. Under the
    ID ``muse`` a pick, a review answer or position news that names Muse is refused; under any
    other ID the same text would pass the screen and reach Jev."""
    text = {"reasoning": {"thesis": "Muse sees a held swing low.", "why_now": "Fresh."}}
    assert identity_paths(text, "muse", "picks[0]") == ["picks[0].reasoning.thesis"]
    assert identity_paths(text, "muse-cloud", "picks[0]") == []
    answer = {"what_changed": "MUSE expects a retest.", "next_24h": "A retest.",
              "proves_wrong": "A close below the entry.", "sources": []}
    assert dr.identity_leaks(answer, "muse") == ["what_changed"]
    assert dr.identity_leaks(answer, "muse-cloud") == []
