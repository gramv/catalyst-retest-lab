"""AGENT_RESEARCH_WITHDRAWAL_V1: an agent withdraws its own unfilled picks (package
research-loop-app, docs/RESEARCH-LOOP-V2.md 3.3).

``POST /api/v1/lab/research-withdrawals`` with the agent's own token revokes its report-V3
setups still WATCHING and declines its unadmitted selections, coin by coin, in one transaction
under the shared lock. Setups past WATCHING and other agents' records are never touched, and no
broker call is made.

Fixture evidence only: per-test disposable PostgreSQL databases, the fake paper venue, a mock
Jev transport and a fixture universe. No broker, provider, network or owner-ledger contact.
"""

import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from catalyst_lab.agent_identity import Principal, legacy_principal
from catalyst_lab.audit import verify_events
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.research_withdrawal import (
    MAX_WITHDRAWAL_ITEMS,
    WITHDRAWAL_BODY_LIMIT,
    ResearchWithdrawals,
)
from catalyst_lab.system_check import AdmissionRefused
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation
from tests.test_replacement import LADDER, ladder, replacements, runtime_for, selected
from tests.test_system_check import (
    at_price,
    bodies,
    event_count,
    publish_v3,
    rows,
    state,
    two_slots,
    v3_pick,
)
from tests.test_system_check import market as market

ROUTE = "/api/v1/lab/research-withdrawals"
WITHDRAWN = "WITHDRAWN_BY_RESEARCH"
LEGACY = "fixture-withdrawal-legacy-muse-token-abcdefghijk"
STATUS = "fixture-withdrawal-status-token-abcdefghijklmnopq"
OPERATOR = "fixture-withdrawal-operator-token-abcdefghijklmno"
AGENTS = {"claude": "fixture-withdrawal-claude-agent-token-abcdefgh",
          "instinct": "fixture-withdrawal-instinct-agent-token-abcdef"}
REASON = "The 1-hour close broke the swing low the entry was built on."


def bearer(token):
    return {"Authorization": "Bearer " + token}


def client(mx, *, configured=True):
    engine, venue, _ = mx
    service = ResearchWithdrawals(engine.store, clock=lambda: venue.now) if configured else None
    return TestClient(create_managed_app(
        SimpleNamespace(repo=engine.repo), engine.store, api_token=LEGACY,
        runtime_status=lambda: {}, status_token=STATUS, operator_token=OPERATOR,
        agent_tokens=AGENTS, research_withdrawals=service))


def withdrawal(symbols, *, agent_id="claude", withdrawal_id=None, reason=REASON):
    return {"schema_version": "AGENT_RESEARCH_WITHDRAWAL_V1",
            "withdrawal_id": withdrawal_id or str(uuid4()),
            "agent": {"agent_id": agent_id, "agent_version": "fixture-1"},
            "items": [{"symbol": symbol, "reason": reason} for symbol in symbols]}


def live(mx):
    return at_price("100.49", "100.51", at=mx[1].now)


def opened(mx, packet):
    """Admit, trigger and fill ``packet``: an OPEN trade (past WATCHING)."""
    engine, venue, _ = mx
    sid = engine.admit(packet, live_quote=live(mx))
    touch = observation(mx, trade_price="100", bid="99.99", ask="100.01")
    assert engine.observe_trigger(sid, touch)["outcome"] == "APPROVED"
    entry = next(o for o in venue.orders_of("buy") if o["symbol"] == packet["symbol"])
    engine.ingest(venue.fill(entry["id"], entry["qty"]))
    engine.manage(sid, observation(mx))
    assert state(engine, sid)["state"] == "OPEN"
    return sid


def declined_seqs(engine):
    return {b["selection_event_seq"] for b in bodies(engine, "RESEARCH_ADMISSION_DECLINED")}


# --- The effect ---------------------------------------------------------------------------------


def test_an_agent_withdraws_only_its_own_unfilled_picks(mx):
    engine, venue, _ = mx
    now = venue.now
    old, _ = two_slots(now)
    mine = publish_v3(mx, [v3_pick(i, s, now) for i, s in enumerate(
        ("AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD"))], run_slot=old)
    theirs = publish_v3(mx, [v3_pick(i, s, now) for i, s in enumerate(
        ("FFF/USD", "GGG/USD"))], run_slot=old, agent_id="instinct")
    watching = engine.admit(mine["AAA/USD"], live_quote=live(mx))
    trade = opened(mx, mine["CCC/USD"])
    expired = engine.admit(mine["DDD/USD"], live_quote=live(mx))
    engine.revoke(expired, "FIXTURE_ENDED")  # A setup already over: nothing to withdraw.
    other = engine.admit(theirs["FFF/USD"], live_quote=live(mx))
    web = client(mx)
    calls = len(venue.calls)
    body = withdrawal(["AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD", "EEE/USD", "FFF/USD",
                       "GGG/USD"])
    reply = web.post(ROUTE, json=body, headers=bearer(AGENTS["claude"]))
    assert reply.status_code == 200, reply.text
    results = [
        {"symbol": "AAA/USD", "result": "WITHDRAWN", "setup_ids": [str(watching)],
         "selections_declined": 0},
        {"symbol": "BBB/USD", "result": "WITHDRAWN", "setup_ids": [], "selections_declined": 1},
        {"symbol": "CCC/USD", "result": "NOT_WATCHING", "setup_ids": [str(trade)],
         "selections_declined": 0},
        *({"symbol": s, "result": "NONE", "setup_ids": [], "selections_declined": 0}
          for s in ("DDD/USD", "EEE/USD", "FFF/USD", "GGG/USD")),
    ]
    assert reply.json() == {
        "status": "RESEARCH_WITHDRAWAL_RECORDED", "schema_version": "AGENT_RESEARCH_WITHDRAWAL_V1",
        "withdrawal_id": body["withdrawal_id"], "agent_id": "claude",
        "agent_version": "fixture-1", "results": results, "idempotent_replay": False,
        "trade_authorized": False}
    agent = {"agent_id": "claude", "agent_version": "fixture-1"}
    evidence = {"withdrawal_id": body["withdrawal_id"], "withdrawal_reason": REASON,
                "agent": agent}
    # The watching setup: revoked through the revoke path, INVALIDATED.
    [revoke] = [r for r in rows(engine, "REVOKE") if r["setup_id"] == watching]
    assert revoke["body"] == {"reason": WITHDRAWN, **evidence}
    assert revoke["idempotency_key"] == f"research:withdrawn:{body['withdrawal_id']}:{watching}"
    current = state(engine, watching)
    assert (current["state"], current["revoked"], current["revocation_reason"]) == (
        "INVALIDATED", True, WITHDRAWN)
    # The unadmitted selection: declined under the runtime's key, never offered again.
    pending = mine["BBB/USD"]
    [declined] = [r for r in rows(engine, "RESEARCH_ADMISSION_DECLINED")]
    assert declined["idempotency_key"] == (
        f"research:admission-declined:{pending['selection_event_seq']}")
    assert declined["body"] == {"cycle_id": pending["cycle_id"], "item_key": pending["item_key"],
                                "revision": 1, "receipt_id": pending["receipt_id"],
                                "selection_event_seq": pending["selection_event_seq"],
                                "reason": WITHDRAWN, **evidence}
    # Past WATCHING, already over, and the other agent's records: untouched.
    assert state(engine, trade)["state"] == "OPEN" and not state(engine, trade).get("revoked")
    assert state(engine, other)["state"] == "WATCHING" and not state(engine, other).get("revoked")
    assert theirs["GGG/USD"]["selection_event_seq"] not in declined_seqs(engine)
    # One record of the request; no broker call.
    [record] = rows(engine, "RESEARCH_WITHDRAWAL")
    assert record["setup_id"] is None
    assert record["idempotency_key"] == f"research-withdrawal:claude:{body['withdrawal_id']}"
    assert {k: v for k, v in record["body"].items() if k != "request_sha256"} == {
        "schema_version": "AGENT_RESEARCH_WITHDRAWAL_V1", "withdrawal_id": body["withdrawal_id"],
        "agent": agent, "items": body["items"], "results": results,
        "received_at": venue.now.isoformat()}
    assert re.fullmatch(r"[0-9a-f]{64}", record["body"]["request_sha256"])
    assert len(venue.calls) == calls
    assert verify_events(engine.repo.export_events())["valid"]
    # The research context tells the agent what became of each pick.
    from tests.test_research_context import Feeds, service_for

    service, _ = service_for(engine.repo, lambda: venue.now, Feeds(lambda: venue.now))
    context = service.context(Principal("muse", "claude"))
    picks = {p["symbol"]: p for p in context["recent_outcomes"]["last_run"]["picks"]}
    assert (picks["AAA/USD"]["status"], picks["AAA/USD"]["setup_reason"]) == (
        "INVALIDATED", WITHDRAWN)
    assert (picks["BBB/USD"]["status"], picks["BBB/USD"]["decline_code"]) == (
        "DECLINED", WITHDRAWN)
    assert context["watching_setups"] == []  # AAA was the only one watching.


def test_a_resend_replays_and_a_changed_body_under_the_same_id_conflicts(mx):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    mine = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)
    sid = engine.admit(mine["AAA/USD"], live_quote=live(mx))
    web = client(mx)
    body = withdrawal(["AAA/USD"])
    first = web.post(ROUTE, json=body, headers=bearer(AGENTS["claude"])).json()
    assert first["results"][0]["setup_ids"] == [str(sid)]
    before = event_count(engine)
    again = web.post(ROUTE, json=body, headers=bearer(AGENTS["claude"]))
    assert again.status_code == 200 and again.json() == {**first, "idempotent_replay": True}
    padded = {**body, "items": [{"symbol": "AAA/USD", "reason": f"  {REASON} "}]}
    assert web.post(ROUTE, json=padded, headers=bearer(AGENTS["claude"])).json() == {
        **first, "idempotent_replay": True}  # The same content once whitespace is trimmed.
    changed = withdrawal(["AAA/USD"], withdrawal_id=body["withdrawal_id"], reason="Other.")
    reply = web.post(ROUTE, json=changed, headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (409, {"detail": "WITHDRAWAL_ID_CONFLICT"})
    assert event_count(engine) == before
    # A new withdrawal finds nothing left; another agent's ID space is its own.
    later = web.post(ROUTE, json=withdrawal(["AAA/USD"]), headers=bearer(AGENTS["claude"]))
    assert later.json()["results"] == [
        {"symbol": "AAA/USD", "result": "NONE", "setup_ids": [], "selections_declined": 0}]
    theirs = withdrawal(["AAA/USD"], agent_id="instinct", withdrawal_id=body["withdrawal_id"])
    reply = web.post(ROUTE, json=theirs, headers=bearer(AGENTS["instinct"]))
    assert reply.status_code == 200 and reply.json()["idempotent_replay"] is False


# --- Refusals -----------------------------------------------------------------------------------


def test_credentials_roles_and_identity_are_checked_first(mx):
    engine, _, _ = mx
    web = client(mx)
    body = withdrawal(["AAA/USD"])
    assert web.post(ROUTE, json=body).status_code == 401
    assert web.post(ROUTE, json=body, headers=bearer("x" * 48)).status_code == 401
    for token in (STATUS, OPERATOR):
        reply = web.post(ROUTE, json=body, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (403, {"detail": "TOKEN_ROLE_NOT_PERMITTED"})
    for token in (AGENTS["instinct"], LEGACY):  # Neither acts for agent ``claude``.
        reply = web.post(ROUTE, json=body, headers=bearer(token))
        assert (reply.status_code, reply.json()) == (403, {"detail": "AGENT_IDENTITY_MISMATCH"})
    assert rows(engine, "RESEARCH_WITHDRAWAL") == []
    # The legacy credential acts for agent ``muse``.
    reply = web.post(ROUTE, json=withdrawal(["AAA/USD"], agent_id="muse"), headers=bearer(LEGACY))
    assert reply.status_code == 200 and reply.json()["agent_id"] == "muse"
    unconfigured = client(mx, configured=False).post(ROUTE, json=body,
                                                     headers=bearer(AGENTS["claude"]))
    assert (unconfigured.status_code, unconfigured.json()) == (
        503, {"detail": "RESEARCH_WITHDRAWAL_NOT_CONFIGURED"})


def item(symbol="AAA/USD", reason=REASON, **extra):
    return {"symbol": symbol, "reason": reason, **extra}


INVALID = {
    "no agent": (lambda b: b.pop("agent"), [{"path": "agent", "code": "MISSING"}]),
    "report agent block": (
        lambda b: b["agent"].update(guidelines_version="MUSE_RESEARCH_GUIDELINES_V6"),
        [{"path": "agent.guidelines_version", "code": "EXTRA_FORBIDDEN"}]),
    "wrong schema": (lambda b: b.update(schema_version="AGENT_RESEARCH_WITHDRAWAL_V2"),
                     [{"path": "schema_version", "code": "LITERAL_ERROR"}]),
}
INVALID_ITEMS = {
    "no items": ([], [{"path": "items", "code": "TOO_SHORT"}]),
    "too many": ([item(f"C{i:02}/USD") for i in range(MAX_WITHDRAWAL_ITEMS + 1)],
                 [{"path": "items", "code": "TOO_LONG"}]),
    "duplicate": ([item(), item()], [{"path": "items", "code": "DUPLICATE_SYMBOL_IN_WITHDRAWAL"}]),
    "long reason": ([item(reason="x" * 301)], [{"path": "items[0].reason",
                                                "code": "STRING_TOO_LONG"}]),
    "blank reason": ([item(reason="   ")], [{"path": "items[0].reason",
                                             "code": "STRING_TOO_SHORT"}]),
    "bad symbol": ([item(symbol="xrp/usd")], [{"path": "items[0].symbol",
                                               "code": "STRING_PATTERN_MISMATCH"}]),
    "extra field": ([item(quantity="1")], [{"path": "items[0].quantity",
                                            "code": "EXTRA_FORBIDDEN"}]),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_identity_block_is_refused_before_the_credential_check(mx, case):
    engine, _, _ = mx
    body = withdrawal(["AAA/USD"])
    mutate, errors = INVALID[case]
    mutate(body)
    reply = client(mx).post(ROUTE, json=body, headers=bearer(AGENTS["instinct"]))
    assert (reply.status_code, reply.json()) == (
        422, {"detail": "INVALID_RESEARCH_WITHDRAWAL", "errors": errors})
    assert rows(engine, "RESEARCH_WITHDRAWAL") == []


@pytest.mark.parametrize("case", sorted(INVALID_ITEMS))
def test_invalid_items_are_refused_whole_with_paths_and_codes(mx, case):
    engine, _, _ = mx
    items, errors = INVALID_ITEMS[case]
    body = {**withdrawal([]), "items": items}
    reply = client(mx).post(ROUTE, json=body, headers=bearer(AGENTS["claude"]))
    assert (reply.status_code, reply.json()) == (
        422, {"detail": "INVALID_RESEARCH_WITHDRAWAL", "errors": errors})
    assert "xrp" not in reply.text and "x" * 20 not in reply.text  # Paths and codes only.
    assert rows(engine, "RESEARCH_WITHDRAWAL") == []


def test_sensitive_content_bad_json_and_size_are_refused(mx):
    engine, _, _ = mx
    web = client(mx)
    headers = {**bearer(AGENTS["claude"]), "Content-Type": "application/json"}
    leaked = withdrawal(["AAA/USD"], reason="Ask trader@example.com before re-entering.")
    reply = web.post(ROUTE, json=leaked, headers=headers)
    assert (reply.status_code, reply.json()) == (422, {
        "detail": "SENSITIVE_EVIDENCE_REJECTED",
        "errors": [{"path": "items[0].reason", "code": "SENSITIVE_EVIDENCE_REJECTED"}]})
    assert "example.com" not in reply.text
    for content in (b"not json", b"[]", b'{"a": 1, "a": 2}'):
        reply = web.post(ROUTE, content=content, headers=headers)
        assert (reply.status_code, reply.json()) == (422, {"detail": "INVALID_RESEARCH_WITHDRAWAL"})
    at_limit = web.post(ROUTE, content=b" " * WITHDRAWAL_BODY_LIMIT, headers=headers)
    assert (at_limit.status_code, at_limit.json()) == (
        422, {"detail": "INVALID_RESEARCH_WITHDRAWAL"})
    over = web.post(ROUTE, content=b" " * (WITHDRAWAL_BODY_LIMIT + 1), headers=headers)
    assert (over.status_code, over.json()) == (413, {"detail": "RESEARCH_WITHDRAWAL_TOO_LARGE"})
    assert rows(engine, "RESEARCH_WITHDRAWAL") == []


# --- Admission and replacement after a withdrawal ---------------------------------------------


def test_admission_refuses_a_selection_withdrawn_while_it_was_being_admitted(mx):
    engine, venue, _ = mx
    old, _ = two_slots(venue.now)
    mine = publish_v3(mx, [v3_pick(0, "AAA/USD", venue.now)], run_slot=old)
    body = withdrawal(["AAA/USD"])
    service = ResearchWithdrawals(engine.store, clock=lambda: venue.now)
    assert service.withdraw(body, principal=Principal("muse", "claude"))["results"][0][
        "selections_declined"] == 1
    # The runtime read the selection before the withdrawal and admits it now.
    with pytest.raises(AdmissionRefused) as caught:
        engine.admit(mine["AAA/USD"], live_quote=live(mx))
    assert (caught.value.code, caught.value.details) == (
        WITHDRAWN, {"withdrawal_id": body["withdrawal_id"]})
    assert engine.store.active() == []
    with pytest.raises(PermissionError):  # The service also refuses another agent's body.
        service.withdraw(withdrawal(["AAA/USD"]), principal=legacy_principal())


def test_a_withdrawn_top_k_pick_is_never_replaced(mx, market):
    engine, venue, _ = mx
    cycle, cycle_id = ladder(mx)  # Top-K, K = 5: A1-A5 published, A6 next in the ranking.
    picks = selected(engine, cycle_id)
    service = ResearchWithdrawals(engine.store, clock=lambda: venue.now)
    receipt = service.withdraw(withdrawal(["A2/USD"]), principal=Principal("muse", "claude"))
    assert receipt["results"][0]["selections_declined"] == 1
    assert replacements(engine, cycle_id) == []
    runtime = runtime_for(mx, market, cycle, list(LADDER))
    declines = len(bodies(engine, "RESEARCH_ADMISSION_DECLINED"))
    # The runtime's own refusal of the raced admission, whatever its code: nothing replaced.
    for refusal in (AdmissionRefused(WITHDRAWN, {"withdrawal_id": "x"}),
                    ValueError("PRICE_MISMATCH")):
        runtime._admission_refused(picks["A2/USD"], refusal)
    assert replacements(engine, cycle_id) == []
    assert len(bodies(engine, "RESEARCH_ADMISSION_DECLINED")) == declines
    assert "A6/USD" not in selected(engine, cycle_id) and runtime.error is None
