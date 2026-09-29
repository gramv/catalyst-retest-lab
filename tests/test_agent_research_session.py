"""The supervised research-agent session tool (scripts/agent_research_session.py).

Fixture evidence only: the scripted mock Jev transport and the SESSION_SIMULATION paper venue
(the test fixtures' ManagedVenue). In-process tests use disposable template-clone databases;
the end-to-end test starts a fresh /tmp/catalyst-session-* cluster through the real CLI, with
a loopback uvicorn on an ephemeral port in the session process. No provider, broker or
owner-ledger contact.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from catalyst_lab import localdb
from catalyst_lab.account_risk import FIXED_EXIT_ARM, JEV_MANAGED_ARM
from catalyst_lab.audit import verify_events
from catalyst_lab.research_selection_b1 import ACTIVATION_KIND, B1_POLICY, B2_POLICY, V2_POLICY
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_review_dossier import fixture_agent, item, report

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "agent_research_session.py"
SPEC = importlib.util.spec_from_file_location("agent_research_session_under_test", SCRIPT)
session_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session_script)
ROUTE = "/api/v1/lab/research-reports"
ROLES = ("catalyst_app", "catalyst_risk", "catalyst_jev", "catalyst_review")
EXPORTED = ("events.json", "decisions.json", "research-funnel.json", "selection-replay.json",
            "acceptance-summary.json", "manifest.json")


def bearer(token):
    return {"Authorization": "Bearer " + token}


def crypto_item(symbol, index, now):
    """A review-dossier fixture item (levels 106/106.1/104/111) on an owner-bucket pair."""
    value = item(index, market="CRYPTO", now=now)
    value["symbol"], value["signal_id"] = symbol, f"TEST-SESSION-{index:02}"
    return value


def session_report(symbols, now):
    agent = fixture_agent(session_script.AGENT_ID, "SESSION-TEST-1")
    return report([crypto_item(symbol, i, now) for i, symbol in enumerate(symbols)],
                  now=now, agent=agent)


def as_role(er, role):
    return er.database_url.replace("user=catalyst_app", "user=" + role)


@pytest.fixture
def make_session(er):
    sessions = []

    def build(*, fixture=None, **options):
        tokens = {role: secrets.token_urlsafe(48) for role in session_script.TOKEN_ROLES}
        session = session_script.AgentResearchSession(
            urls={role: as_role(er, role) for role in ROLES},
            config=session_script.SessionConfig("fixture", **options),
            tokens=tokens, fixture=fixture,
        )
        sessions.append(session)
        return session

    yield build
    for session in sessions:
        session.close()


def submit(session, raw):
    reply = TestClient(session.app).post(
        ROUTE, json=raw, headers=bearer(session.tokens[session_script.AGENT_ID])
    )
    assert reply.status_code == 202, reply.text
    return reply.json()


# --- Pure rules -------------------------------------------------------------------------------


def test_session_root_must_be_a_new_short_tmp_catalyst_session_directory(tmp_path):
    name = "catalyst-session-t" + uuid4().hex[:8]
    root = session_script.new_session_root("/tmp/" + name)
    assert root == Path("/tmp").resolve() / name and not root.exists()
    refused = {
        "/tmp/other-" + uuid4().hex[:8]: "SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION",
        "/var/tmp/" + name: "SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION",
        "/tmp/nested/" + name: "SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION",
        name: "SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION",
        "/tmp/catalyst-session-" + "x" * 40: "SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION",
        str(tmp_path / name): "SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION",
    }
    for value, code in refused.items():
        with pytest.raises(session_script.SessionRefused, match=f"^{code}$"):
            session_script.new_session_root(value)
    existing = Path("/tmp") / ("catalyst-session-e" + uuid4().hex[:8])
    link = Path("/tmp") / ("catalyst-session-l" + uuid4().hex[:8])
    try:
        existing.mkdir(mode=0o700)
        link.symlink_to(tmp_path)
        for path in (existing, link):
            with pytest.raises(session_script.SessionRefused,
                               match="^SESSION_ROOT_ALREADY_EXISTS$"):
                session_script.new_session_root(str(path))
        # The CLI refuses before creating or starting anything, and says so with a code only.
        for path in (existing, Path("/tmp/not-a-session-" + uuid4().hex[:8])):
            assert session_script.main(["start", "--root", str(path), "--port", "0",
                                        "--jev", "fixture"]) == 2
        assert list(existing.iterdir()) == []
        # A client command never treats a directory that is not a private session as one.
        with pytest.raises(session_script.SessionRefused, match="^SESSION_FILE_MISSING$"):
            session_script.load_session(str(existing))
    finally:
        existing.rmdir()
        link.unlink()


def test_configuration_and_fixture_script_fail_closed():
    config = session_script.SessionConfig
    assert config("fixture").rule.policy == V2_POLICY
    assert config("fixture", selection_rule="B1", quality_floor="STRONG").rule.b1
    for options, code in (
        ({"selection_rule": "B1"}, "SELECTION_QUALITY_FLOOR_REQUIRED"),
        ({"quality_floor": "ADEQUATE"}, "SELECTION_QUALITY_FLOOR_REQUIRES_B1"),
        ({"max_jev_calls": 0}, "JEV_CALL_CAP_INVALID"),
        ({"management_reviews": "enabled"}, "MANAGEMENT_REVIEWS_SETTING_INVALID"),
        ({"sim_price_increment": "0"}, "SIMULATED_PRICE_INCREMENT_INVALID"),
        ({"report_max_seconds": 0}, "REPORT_MAX_SECONDS_INVALID"),
        ({"extra_crypto_pairs": ("USDC/USD",)}, "EXTRA_CRYPTO_PAIRS_INVALID"),  # Stablecoin.
        ({"extra_crypto_pairs": ("BTC/USD",)}, "EXTRA_CRYPTO_PAIRS_INVALID"),  # Owner bucket.
        ({"extra_crypto_pairs": ("arb/usd",)}, "EXTRA_CRYPTO_PAIRS_INVALID"),
        ({"extra_crypto_pairs": ("ARB/EUR",)}, "EXTRA_CRYPTO_PAIRS_INVALID"),
        ({"extra_crypto_pairs": ("ARB/USD", "ARB/USD")}, "EXTRA_CRYPTO_PAIRS_INVALID"),
        ({"extra_crypto_pairs": ["ARB/USD"]}, "EXTRA_CRYPTO_PAIRS_INVALID"),
    ):
        with pytest.raises(session_script.SessionRefused, match=f"^{code}$"):
            config("fixture", **options)
    script = session_script.FixtureScript
    parsed = script({
        "BTC/USD": ["APPROVE", "NO", "NO", "MEDIUM", "STRONG"],
        "SOL/USD": {"skeptic": [["APPROVE", "YES", "NO", "LOW"], ["APPROVE", "NO", "NO", "LOW"]],
                    "management": "TIGHTEN_STOP"},
        "*": {"skeptic": ["REJECT", "NO", "NO", "TIE:LOW/HIGH"]},
    })
    assert parsed.spec("BTC/USD")["quality"] == "STRONG"
    assert len(parsed.spec("SOL/USD")["skeptic"]) == 2
    assert parsed.spec("ETH/USD")["skeptic"] == (("REJECT", "NO", "NO", "TIE:LOW/HIGH"),)
    for raw in ({"X": ["MAYBE", "NO", "NO", "LOW"]}, {"X": ["APPROVE", "NO", "NO"]},
                {"X": {"skeptic": ["APPROVE", "NO", "NO", "LOW"], "quality": "GREAT"}},
                {"X": {"management": "SELL"}}, {"X": {"size": 1}}, ["APPROVE"]):
        with pytest.raises(session_script.SessionRefused, match="^FIXTURE_SCRIPT_INVALID$"):
            script(raw)


def test_capped_transport_refuses_the_next_call_and_never_sends_it():
    sent, closed = [], []

    def provider(request):
        sent.append(json.loads(request.content)["n"])
        return httpx.Response(200, json={"ok": True})

    class Recording(httpx.MockTransport):
        async def aclose(self):
            closed.append(True)

    async def calls(transport, count):
        results = []
        async with httpx.AsyncClient(transport=transport) as client:
            for n in range(count):
                body = {"questions": {}, "state": {}, "n": n}
                try:
                    results.append((await client.post("https://provider.invalid/v1",
                                                      json=body)).status_code)
                except session_script.JevCallCapReached as exc:
                    results.append(str(exc))
        return results

    meter = session_script.CallMeter(2)
    shared = session_script.CappedTransport(meter, httpx.MockTransport(provider))
    assert asyncio.run(calls(shared, 3)) == [200, 200, "JEV_CALL_CAP_REACHED"]
    assert sent == [0, 1]  # The third call never reached the provider transport.
    snapshot = meter.snapshot()
    assert (snapshot["used"], snapshot["remaining"], snapshot["refused"]) == (2, 0, 1)
    assert snapshot["refusals"][0]["layer"] == "TRANSPORT"
    # A refusal is a transport error, which JevReviewer records; it is never a response.
    assert issubclass(session_script.JevCallCapReached, httpx.TransportError)
    # The real-provider form: one transport per call, closed with its response.
    sent.clear()
    fresh = session_script.CallMeter(1)
    per_call = session_script.CappedTransport(fresh, lambda: Recording(provider),
                                              per_request=True)
    assert asyncio.run(calls(per_call, 2)) == [200, "JEV_CALL_CAP_REACHED"]
    assert sent == [0] and closed == [True]


# --- In-process sessions on disposable databases -------------------------------------------


def test_cap_turns_the_next_review_into_a_clear_decision_and_protection_still_runs(
    make_session,
):
    session = make_session(max_jev_calls=2, management_reviews="DISABLED")
    submit(session, session_report(["BTC/USD", "XRP/USD", "UNI/USD"], datetime.now(UTC)))
    ticked = session.tick()
    [cycle] = ticked["cycles"]
    items = {row["symbol"]: row for row in cycle["items"]}
    for symbol in ("BTC/USD", "XRP/USD"):
        assert items[symbol]["v2"]["disposition"] == "APPROVED" and items[symbol]["selected"]
        assert items[symbol]["receipt_id"] and items[symbol]["latency_ms"] is not None
    capped = items["UNI/USD"]
    assert capped["decision"] == {"disposition": "NEEDS_REVIEW",
                                  "reason": "JEV_CALL_CAP_REACHED", "policy": V2_POLICY}
    assert capped["receipt_id"] is None and capped["b1_shadow"] is None
    assert session.fixture.calls == ["SKEPTIC:BTC/USD", "SKEPTIC:XRP/USD"]
    calls = ticked["jev_calls"]
    assert (calls["used"], calls["refused"]) == (2, 1)
    assert calls["refusals"][0]["layer"] == "REVIEWER"
    [task] = ticked["open_evidence_tasks"]
    assert task["symbol"] == "UNI/USD" and task["objection_codes"] == ["JEV_CALL_CAP_REACHED"]
    text = session_script.render_tick(json.loads(json.dumps(ticked, default=str)))
    assert "Jev calls: 2 used of 2" in text and "JEV_CALL_CAP_REACHED" in text
    # A spent cap and DISABLED reviews never touch protection or the mechanical exit.
    executed = session.execute(simulate_prints=True)
    assert [row["admission"]["outcome"] for row in executed["admissions"]] == ["ADMITTED"] * 2
    assert len(executed["lifecycles"]) == 2
    for life in executed["lifecycles"]:
        assert life["final_state"] == "CLOSED" and life["residual"]["zero"], life
        assert life["management_review"]["outcome"] == "SKIPPED"
        assert life["management_review"]["reason"] == "MANAGEMENT_REVIEWS_DISABLED"
        assert life["reconciliation_after_exit"]["clean"]
    assert session.meter.snapshot()["used"] == 2


def test_b1_session_reviews_the_managed_position_and_exports_everything(
    make_session, monkeypatch, tmp_path,
):
    # Force the randomized arm so the management-review path runs on every run of this test.
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm",
                        lambda setup_id, pct: JEV_MANAGED_ARM)
    fixture = session_script.FixtureScript({"SOL/USD": {
        "skeptic": ["REJECT", "NO", "NO", "LOW"], "quality": "STRONG",
        "management": "TIGHTEN_AND_EXTEND",
    }})
    session = make_session(selection_rule="B1", quality_floor="ADEQUATE", fixture=fixture)
    assert session.activation["kind"] == ACTIVATION_KIND
    received = submit(session, session_report(["SOL/USD"], datetime.now(UTC)))
    [row] = session.tick()["cycles"][0]["items"]
    assert row["decision"] == {"disposition": "APPROVED", "reason": "B1_COMPONENTS_PASSED",
                               "policy": B1_POLICY}
    assert row["b1_shadow"]["dissent"] == "REJECT"  # The verdict is dissent under B1.
    assert row["v2"] == {"disposition": "REJECTED", "reason": "JEV_REJECTED",
                         "source": "RECEIPTS_UNDER_V2"}
    assert row["quality"]["category"] == "STRONG" and row["selected"]
    executed = session.execute(simulate_prints=True)
    [admission] = executed["admissions"]
    assert admission["admission"]["outcome"] == "ADMITTED"
    [life] = executed["lifecycles"]
    assert life["arm"] == JEV_MANAGED_ARM and life["risk_policy_id"] == "JEV_MANAGED_RISK_V2"
    assert life["trigger_print"]["data_provider"] == session_script.SIMULATION
    risk = life["risk_decision"]
    assert (risk["outcome"], risk["reason"], risk["binding_constraint"]) == (
        "APPROVED", "RISK_APPROVED", "RISK")
    # JEV_MANAGED_RISK_V2: 0.5% of the fixture venue's 10,000 equity over M - S = 2.1.
    assert D(risk["budget"]) == D(50) and D(risk["qty"]) == D("23.80952380")
    assert life["protection"]["protected"] and D(life["protection"]["stop"]) == D(104)
    review = life["management_review"]
    assert (review["outcome"], review["action"], review["jev_calls"]) == (
        "JUDGMENT", "TIGHTEN_AND_EXTEND", 1), json.dumps(review, default=str)
    stop, target = D(review["plan"]["stop"]), D(review["plan"]["target"])
    assert D(104) < stop < D(106) and target > D(111)
    after = life["protection_after_review"]
    assert after["protected"] and D(after["stop"]) == stop
    assert D(life["exit"]["fills"][-1]["price"]) == target
    assert life["exit"]["reason"] == "TARGET_EXIT" and life["final_state"] == "CLOSED"
    assert life["residual"]["zero"] and life["reconciliation_after_exit"]["clean"]
    assert life["state_path"][0] == "WATCHING" and life["state_path"][-1] == "CLOSED"
    assert fixture.calls == ["SKEPTIC:SOL/USD", "QUALITY_V2:SOL/USD", "MANAGEMENT:SOL/USD"]
    assert session.meter.snapshot()["used"] == 3
    with session.repo.connect() as conn:
        metadata = conn.execute("""SELECT body FROM lab.managed_events
            WHERE kind='CRYPTO_ASSET_METADATA'""").fetchall()
    # The engine's own row and key, with the simulated venue named as its source.
    [recorded] = [m["body"] for m in metadata]
    assert (recorded["symbol"], D(recorded["price_increment"]), recorded["source"]) == (
        "SOL/USD", D("0.00000001"), "SESSION_SIMULATION")

    output = tmp_path / "export"
    manifest = session.export(output)
    assert manifest["audit"]["valid"] and sorted(manifest["files"]) == sorted(EXPORTED)
    for name in EXPORTED:
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
    events = json.loads((output / "events.json").read_text())
    assert verify_events(events["events"])["valid"] and events["verification"]["valid"]
    [cycle] = json.loads((output / "decisions.json").read_text())["cycles"]
    assert cycle["cycle_id"] == received["cycle_id"]
    [revision] = cycle["items"][0]["revisions"]
    assert revision["decision"]["dissent"] == "REJECT" and revision["setup"]["state"] == "CLOSED"
    assert revision["quality"]["category"] == "STRONG" and revision["receipts"]
    funnel = json.loads((output / "research-funnel.json").read_text())
    [group] = funnel["pages"][0]["agent_groups"]
    assert (group["agent_id"], group["selected_count"], group["entered_count"]) == (
        session_script.AGENT_ID, 1, 1)
    replayed = json.loads((output / "selection-replay.json").read_text())
    [replay_item] = replayed["items"]
    assert (replay_item["v2"]["disposition"], replay_item["b1"]["disposition"]) == (
        "REJECTED", "APPROVED")
    acceptance = json.loads((output / "acceptance-summary.json").read_text())
    [(setup_id, evidence)] = acceptance["setups"].items()
    # No market stream exists in a session: only the subscription check can fail, honestly.
    assert evidence["exit_code"] == 2
    assert {name for name, passed in evidence["checks"].items() if not passed} == {
        "SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER"}
    assert (output / "acceptance" / setup_id / "evidence.json").exists()
    exported = "".join(p.read_text() for p in output.rglob("*") if p.is_file())
    assert not any(token in exported for token in session.tokens.values())


# --- The real CLI, end to end ----------------------------------------------------------------


@pytest.fixture
def session_root():
    root = Path("/tmp") / ("catalyst-session-t" + uuid4().hex[:8])
    yield root
    if (root / "postgres" / "postmaster.pid").exists():
        with contextlib.suppress(Exception):
            localdb.stop(root)
    shutil.rmtree(root, ignore_errors=True)


def cli(*args, timeout=240):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], cwd=REPOSITORY,
        capture_output=True, text=True, timeout=timeout,
    )


def wait_ready(root, process, log):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("session exited during startup: " + log.read_text()[-2000:])
        try:
            descriptor = json.loads((root / "session.json").read_text())
            if (root / "control.sock").exists():
                return descriptor
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    pytest.fail("session did not become ready: " + log.read_text()[-2000:])


def test_full_session_over_the_real_cli(session_root, tmp_path):
    script = tmp_path / "fixture.json"
    script.write_text(json.dumps({
        "SOL/USD": {"skeptic": [["APPROVE", "YES", "NO", "LOW"], ["APPROVE", "NO", "NO", "LOW"]]},
        "LTC/USD": ["APPROVE", "NO", "Insufficient evidence", "LOW"],
    }))
    log = tmp_path / "start.log"
    with open(log, "w") as stream:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "start", "--root", str(session_root), "--port", "0",
             "--jev", "fixture", "--fixture-script", str(script), "--max-jev-calls", "12"],
            cwd=REPOSITORY, stdout=stream, stderr=subprocess.STDOUT,
        )
    runs = []

    def run(*args):
        result = cli(*args)
        runs.append(result)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    try:
        descriptor = wait_ready(session_root, process, log)
        assert descriptor["agent"]["agent_id"] == "fable" and descriptor["http"]["port"] > 0
        assert stat.S_IMODE((session_root / "session.json").stat().st_mode) == 0o600
        for path in descriptor["token_files"].values():
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        now = datetime.now(UTC)
        raw = session_report(["BTC/USD", "SOL/USD", "LTC/USD"], now)
        del raw["agent"]["run_id"]  # The client helper fills it.
        report_file = tmp_path / "report.json"
        report_file.write_text(json.dumps(raw))
        submitted = json.loads(run("submit", "--root", session_root, "--report",
                                   report_file).stdout)
        assert submitted["http_status"] == 202
        body = submitted["body"]
        assert (body["agent_id"], body["contender_count"], body["trade_authorized"]) == (
            "fable", 3, False)
        cycle_id = body["cycle_id"]

        first = json.loads(run("tick", "--root", session_root, "--json").stdout)
        items = {row["symbol"]: row for row in first["cycles"][0]["items"]}
        btc, sol = items["BTC/USD"], items["SOL/USD"]
        assert btc["v2"]["disposition"] == "APPROVED" and btc["selected"]
        assert btc["b1_shadow"]["disposition"] == "APPROVED"
        assert (sol["v2"]["disposition"], sol["v2"]["reason"]) == (
            "NEEDS_REVIEW", "CONTRADICTORY_OR_UNSUPPORTED_ANSWERS")
        assert (sol["b1_shadow"]["reasons"], sol["b1_shadow"]["dissent"]) == (
            ["NEWS_STALE_YES"], "APPROVE")
        tasks = {row["symbol"]: row for row in first["open_evidence_tasks"]}
        task, unanswerable = tasks["SOL/USD"], tasks["LTC/USD"]
        assert task["objection_codes"] == ["NEWS_STALE_YES"] and task["answer_revision"] == 2
        assert "FETCH_PRIOR_DISCLOSURES" in task["requirements"]
        assert unanswerable["objection_codes"] == ["UNSUPPORTED_INFERENCE_INSUFFICIENT"]
        assert unanswerable["requirements"] == ["VERIFY_ECONOMIC_RELATIONSHIP"]
        table = run("tick", "--root", session_root).stdout
        assert "B1 SHADOW" in table and task["task_id"] in table and "Jev calls: 3" in table

        original = raw["items"][1]["sources"][0]
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps({
            "sources": [original, {
                "source_id": "prior-disclosure", "url": "https://issuer.example/disclosures",
                "excerpt": "Fixture prior disclosure: the product had not been announced "
                           "before this launch.",
                "published_at": (now - timedelta(days=30)).isoformat(),
                "retrieved_at": datetime.now(UTC).isoformat(),
            }],
            "thesis": "The launch is new information, confirmed against prior disclosures.",
            "disproof": "A prior disclosure of the same launch would make the news stale.",
            "economic_relationship": "The issuer sells the launched product.",
        }))
        answered = json.loads(run("answer", "--root", session_root, "--cycle", cycle_id,
                                  "--task", task["task_id"], "--evidence", evidence).stdout)
        assert answered["evidence"]["http_status"] == 200 and answered["revision"] == 2
        assert answered["evidence"]["body"]["status"] == "EVIDENCE_RECORDED"
        assert task["task_id"] in answered["claim"]["claimed_task_ids"]
        assert answered["claim"]["task_lease"]["claimant"] == "fable-session"

        declared = json.loads(run("unavailable", "--root", session_root, "--cycle", cycle_id,
                                  "--task", unanswerable["task_id"], "--code",
                                  "PRIMARY_SOURCE_UNAVAILABLE").stdout)
        assert (declared["status"], declared["code"], declared["trade_authorized"]) == (
            "UNAVAILABLE_RECORDED_IN_SESSION", "PRIMARY_SOURCE_UNAVAILABLE", False)
        # ``answer`` already leased every open task of the cycle to this agent (bulk claim).
        assert declared["claim"]["task_lease"]["claimant"] == "fable-session"

        second = json.loads(run("tick", "--root", session_root, "--json").stdout)
        sol = {row["symbol"]: row for row in second["cycles"][0]["items"]}["SOL/USD"]
        assert (sol["revision"], sol["v2"]["disposition"], sol["selected"]) == (
            2, "APPROVED", True)
        # No API route closes a task as unavailable: it stays open (leased) until it expires.
        assert [t["task_id"] for t in second["open_evidence_tasks"]] == [
            unanswerable["task_id"]]

        executed = json.loads(run("execute", "--root", session_root, "--simulate-prints",
                                  "--json").stdout)
        assert {row["symbol"]: row["admission"]["outcome"]
                for row in executed["admissions"]} == {"BTC/USD": "ADMITTED",
                                                        "SOL/USD": "ADMITTED"}
        closed = [life for life in executed["lifecycles"] if life["final_state"] == "CLOSED"]
        assert len(closed) == 2
        for life in closed:
            assert life["residual"]["zero"] and life["reconciliation_after_exit"]["clean"]
            assert life["risk_decision"]["outcome"] == "APPROVED"
            assert life["trigger_print"]["data_provider"] == "SESSION_SIMULATION"
            review = life["management_review"]
            if life["arm"] == FIXED_EXIT_ARM:  # The randomized control arm is never reviewed.
                assert (review["outcome"], review["reason"]) == ("SKIPPED", "FIXED_EXIT_ARM")
            else:
                assert (review["outcome"], review["action"]) == ("JUDGMENT", "HOLD")
        status = run("status", "--root", session_root).stdout
        assert "Jev calls:" in status and "'CLOSED': 2" in status

        output = tmp_path / "export"
        manifest = json.loads(run("export", "--root", session_root, "--output", output,
                                  "--json").stdout)
        assert manifest["audit"]["valid"] and sorted(manifest["files"]) == sorted(EXPORTED)
        events = json.loads((output / "events.json").read_text())["events"]
        assert verify_events(events)["valid"]
        funnel = json.loads((output / "research-funnel.json").read_text())
        [group] = funnel["pages"][0]["agent_groups"]
        assert (group["agent_id"], group["contender_count"], group["selected_count"],
                group["entered_count"]) == ("fable", 3, 2, 2)
        decisions = json.loads((output / "decisions.json").read_text())["cycles"][0]
        by_symbol = {i["symbol"]: i for i in decisions["items"]}
        sol_revisions = by_symbol["SOL/USD"]["revisions"]
        assert [r["revision"] for r in sol_revisions] == [1, 2]
        assert sol_revisions[0]["evidence_tasks"][0]["resolved_by_revision"] == 2
        [ltc_task] = by_symbol["LTC/USD"]["revisions"][0]["evidence_tasks"]
        assert ltc_task["resolved_by_revision"] is None
        assert [d["code"] for d in ltc_task["agent_declarations"]] == [
            "PRIMARY_SOURCE_UNAVAILABLE"]
        replayed = json.loads((output / "selection-replay.json").read_text())
        assert replayed["totals"]["items"] == 4
        assert len(manifest["acceptance_exit_codes"]) == 2

        stopped = run("stop", "--root", session_root)
        assert "cluster STOPPED" in stopped.stdout
        assert process.wait(timeout=60) == 0
        assert not (session_root / "postgres" / "postmaster.pid").exists()
        assert list((session_root / "exports").iterdir())  # stop exported once more.
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
    tokens = [Path(path).read_text().strip() for path in descriptor["token_files"].values()]
    printed = [log.read_text(), (session_root / "session.json").read_text()] + [
        r.stdout + r.stderr for r in runs]
    assert all(len(token) >= 64 for token in tokens)
    assert not any(token in text for token in tokens for text in printed)


def test_b2_configuration_and_six_label_fixture_script_fail_closed():
    config = session_script.SessionConfig
    rule = config("fixture", selection_rule="B2", quality_floor="STRONG").rule
    assert (rule.policy, rule.quality_floor, rule.b2) == (B2_POLICY, "STRONG", True)
    with pytest.raises(session_script.SessionRefused, match="^SELECTION_QUALITY_FLOOR_REQUIRED$"):
        config("fixture", selection_rule="B2")
    script = session_script.FixtureScript
    parsed = script({
        "BTC/USD": {"skeptic_v2": ["NO", "MEDIUM", "NO", "YES", "SUPPORTED", "REJECT"]},
        "SOL/USD": {"skeptic_v2": [["NO", "LOW", "NO", "YES", "PARTIALLY_SUPPORTED", "APPROVE"],
                                   ["NO", "LOW", "NO", "YES", "SUPPORTED", "APPROVE"]],
                    "quality": "STRONG"},
    })
    assert parsed.spec("BTC/USD")["skeptic_v2"] == (
        ("NO", "MEDIUM", "NO", "YES", "SUPPORTED", "REJECT"),)
    assert len(parsed.spec("SOL/USD")["skeptic_v2"]) == 2
    # Unlisted symbols and the V1 list form get the B2 default: every component passes.
    assert parsed.spec("ETH/USD")["skeptic_v2"] == (
        ("NO", "LOW", "NO", "YES", "SUPPORTED", "NEEDS_REVIEW"),)
    assert script({"X": ["APPROVE", "NO", "NO", "LOW"]}).spec("X")["skeptic_v2"] == (
        ("NO", "LOW", "NO", "YES", "SUPPORTED", "NEEDS_REVIEW"),)
    for raw in ({"X": {"skeptic_v2": ["NO", "LOW", "NO", "YES", "SUPPORTED"]}},
                {"X": {"skeptic_v2": ["NO", "LOW", "NO", "MAYBE", "SUPPORTED", "APPROVE"]}},
                {"X": {"skeptic_v2": ["NO", "LOW", "NO", "YES", "SUPPORTED", "APPROVE", "X"]}},
                {"X": {"skeptic_v2": "NO"}}):
        with pytest.raises(session_script.SessionRefused, match="^FIXTURE_SCRIPT_INVALID$"):
            script(raw)
    assert session_script._request_kind(
        session_script.SKEPTIC_V2.questions, {"symbol": "BTC/USD"}) == ("SKEPTIC_V2", "BTC/USD")


def test_b2_session_over_the_real_cli(session_root, tmp_path):
    """B2 end to end: one item approved and admitted; one item whose factual claims are only
    partly supported gets RECITE_FACTUAL_CLAIMS, is answered with a rationale-only revision,
    re-reviewed and approved."""
    script = tmp_path / "fixture.json"
    script.write_text(json.dumps({
        "BTC/USD": {"skeptic_v2": ["NO", "MEDIUM", "NO", "YES", "SUPPORTED", "NEEDS_REVIEW"],
                    "quality": "ADEQUATE"},
        "SOL/USD": {"skeptic_v2": [["NO", "LOW", "NO", "YES", "PARTIALLY_SUPPORTED", "APPROVE"],
                                   ["NO", "LOW", "NO", "YES", "SUPPORTED", "REJECT"]],
                    "quality": "STRONG"},
    }))
    log = tmp_path / "start.log"
    with open(log, "w") as stream:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "start", "--root", str(session_root), "--port", "0",
             "--jev", "fixture", "--fixture-script", str(script), "--max-jev-calls", "12",
             "--selection-rule", "B2", "--quality-floor", "ADEQUATE"],
            cwd=REPOSITORY, stdout=stream, stderr=subprocess.STDOUT,
        )
    runs = []

    def run(*args):
        result = cli(*args)
        runs.append(result)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    try:
        descriptor = wait_ready(session_root, process, log)
        assert (descriptor["configuration"]["selection_rule"],
                descriptor["configuration"]["quality_floor"]) == (B2_POLICY, "ADEQUATE")
        now = datetime.now(UTC)
        raw = session_report(["BTC/USD", "SOL/USD"], now)
        del raw["agent"]["run_id"]
        report_file = tmp_path / "report.json"
        report_file.write_text(json.dumps(raw))
        submitted = json.loads(run("submit", "--root", session_root, "--report",
                                   report_file).stdout)
        assert submitted["http_status"] == 202
        cycle_id = submitted["body"]["cycle_id"]

        first = json.loads(run("tick", "--root", session_root, "--json").stdout)
        [cycle] = first["cycles"]
        assert cycle["selection_policy"] == B2_POLICY and cycle["quality_floor"] == "ADEQUATE"
        items = {row["symbol"]: row for row in cycle["items"]}
        btc, sol = items["BTC/USD"], items["SOL/USD"]
        assert (btc["b2"]["disposition"], btc["b2"]["dissent"], btc["selected"]) == (
            "APPROVED", "NEEDS_REVIEW", True)
        assert btc["b2"]["components"] == {
            "news_stale": "NO", "already_priced": "MEDIUM", "mechanism_contradicted": "NO",
            "inference_labelled": "YES", "factual_claims_supported": "SUPPORTED"}
        assert btc["b2"]["shadow"] == "NOT_APPLICABLE_QUESTION_SET" and btc["b1_shadow"] is None
        assert btc["v2"]["reason"] == "REPLAY_NOT_APPLICABLE_QUESTION_SET"
        assert (sol["b2"]["disposition"], sol["b2"]["reasons"], sol["selected"]) == (
            "NEEDS_REVIEW", ["FACTUAL_CLAIMS_SUPPORTED_PARTIALLY_SUPPORTED"], False)
        [task] = first["open_evidence_tasks"]
        assert (task["symbol"], task["objection_codes"], task["requirements"]) == (
            "SOL/USD", ["FACTUAL_CLAIMS_SUPPORTED_PARTIALLY_SUPPORTED"],
            ["RECITE_FACTUAL_CLAIMS"])
        assert task["instructions"]["RECITE_FACTUAL_CLAIMS"].startswith(
            "Every number and fact in a claim must be stated by its cited excerpt or bar")
        table = run("tick", "--root", session_root).stdout
        assert "B2 (REASONS)" in table and "FC=PARTIALLY_SUPPORTED" in table
        assert "RECITE_FACTUAL_CLAIMS: Every number and fact" in table

        # A rationale-only revision: the same sources and thesis, the claim trimmed.
        item = raw["items"][1]
        rationale = json.loads(json.dumps(item["selection_rationale"]))
        rationale["claims"][0]["text"] = "The issuer launched the product."
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps({
            **{k: item[k] for k in ("sources", "thesis", "disproof", "economic_relationship")},
            "selection_rationale": rationale,
        }))
        answered = json.loads(run("answer", "--root", session_root, "--cycle", cycle_id,
                                  "--task", task["task_id"], "--evidence", evidence).stdout)
        assert (answered["evidence"]["http_status"], answered["revision"]) == (200, 2)
        assert answered["evidence"]["body"]["status"] == "EVIDENCE_RECORDED"

        second = json.loads(run("tick", "--root", session_root, "--json").stdout)
        sol = {row["symbol"]: row for row in second["cycles"][0]["items"]}["SOL/USD"]
        assert (sol["revision"], sol["b2"]["disposition"], sol["b2"]["dissent"],
                sol["selected"]) == (2, "APPROVED", "REJECT", True)
        assert second["open_evidence_tasks"] == []

        executed = json.loads(run("execute", "--root", session_root, "--json").stdout)
        assert {row["symbol"]: (row["revision"], row["admission"]["outcome"])
                for row in executed["admissions"]} == {"BTC/USD": (1, "ADMITTED"),
                                                       "SOL/USD": (2, "ADMITTED")}
        output = tmp_path / "export"
        manifest = json.loads(run("export", "--root", session_root, "--output", output,
                                  "--json").stdout)
        assert manifest["audit"]["valid"] and sorted(manifest["files"]) == sorted(EXPORTED)
        decisions = json.loads((output / "decisions.json").read_text())["cycles"][0]
        revisions = {i["symbol"]: i["revisions"] for i in decisions["items"]}
        assert [r["b2"]["disposition"] for r in revisions["SOL/USD"]] == [
            "NEEDS_REVIEW", "APPROVED"]
        first_sol, second_sol = revisions["SOL/USD"]
        assert first_sol["evidence_tasks"][0]["resolved_by_revision"] == 2
        assert first_sol["sources"] == second_sol["sources"]  # No new source was needed.
        assert second_sol["selection_rationale"]["claims"][0]["text"] == (
            "The issuer launched the product.")
        assert second_sol["selection"]["question_set_version"] == "SKEPTIC_QUESTIONS_V2"
        assert second_sol["setup"]["setup_id"]
        replayed = json.loads((output / "selection-replay.json").read_text())
        assert replayed["totals"]["b2"] == {"APPROVED": 2, "NEEDS_REVIEW": 1}
        assert replayed["totals"]["v2"] == {"NOT_APPLICABLE": 3}
        assert manifest["jev_calls"]["used"] == 5  # Three SKEPTIC_V2, two QUALITY_V2.
        stopped = run("stop", "--root", session_root)
        assert "cluster STOPPED" in stopped.stdout
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
    tokens = [Path(path).read_text().strip() for path in descriptor["token_files"].values()]
    printed = [log.read_text(), (session_root / "session.json").read_text()] + [
        r.stdout + r.stderr for r in runs]
    assert not any(token in text for token in tokens for text in printed)


def test_sigint_exports_and_stops_the_cluster(session_root, tmp_path):
    log = tmp_path / "start.log"
    with open(log, "w") as stream:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "start", "--root", str(session_root), "--port", "0",
             "--jev", "fixture", "--selection-rule", "B1", "--quality-floor", "WEAK",
             "--management-reviews", "DISABLED"],
            cwd=REPOSITORY, stdout=stream, stderr=subprocess.STDOUT,
        )
    try:
        wait_ready(session_root, process, log)
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=120) == 0, log.read_text()[-2000:]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    stopped = json.loads((session_root / "stopped.json").read_text())
    assert stopped["cluster"] == "STOPPED" and stopped["final_export"]
    final = Path(stopped["final_export"])
    assert final.parent == (session_root / "exports").resolve()
    manifest = json.loads((final / "manifest.json").read_text())
    configuration = manifest["configuration"]
    assert manifest["audit"]["valid"] and manifest["acceptance_exit_codes"] == {}
    assert (configuration["selection_rule"], configuration["quality_floor"],
            configuration["management_reviews"]) == (B1_POLICY, "WEAK", "DISABLED")
    events = json.loads((final / "events.json").read_text())["events"]
    assert ACTIVATION_KIND in "".join(e["event_body"] for e in events)
    assert not (session_root / "postgres" / "postmaster.pid").exists()
    assert not (session_root / "control.sock").exists()
    assert "SESSION_STOPPED" in log.read_text()


# --- Selection rule JEV_TOP_K_SELECTION_V1: report V3 picks, ranked once -----------------------


def test_topk_configuration_and_pick_fixture_rows_fail_closed():
    from catalyst_lab.research_selection_topk import TOPK_POLICY, TopKRule

    config = session_script.SessionConfig
    assert config("fixture", selection_rule="TOPK").rule == TopKRule(TOPK_POLICY, 10)
    assert config("fixture", selection_rule="TOPK", topk_k=5).rule == TopKRule(TOPK_POLICY, 5)
    for options, code in (
        ({"selection_rule": "TOPK", "quality_floor": "WEAK"},
         "SELECTION_QUALITY_FLOOR_NOT_APPLICABLE"),
        ({"selection_rule": "TOPK", "topk_k": 11}, "SELECTION_TOPK_INVALID"),
        ({"selection_rule": "TOPK", "topk_k": 4}, "SELECTION_TOPK_INVALID"),
        ({"topk_k": 7}, "SELECTION_TOPK_NOT_APPLICABLE"),
        ({"selection_rule": "B2", "quality_floor": "WEAK", "topk_k": 7},
         "SELECTION_TOPK_NOT_APPLICABLE"),
    ):
        with pytest.raises(session_script.SessionRefused, match=f"^{code}$"):
            config("fixture", **options)
    script = session_script.FixtureScript
    parsed = script({
        "BTC/USD": {"pick": {"news_stale": "YES", "verdict": "REJECT"}, "quality": "STRONG",
                    "quality_levels": [2, 2, 1, 0]},
        "SOL/USD": {"pick": {"levels_supported_by_bars": "TIE:YES/NO"}},
    })
    assert parsed.spec("BTC/USD")["pick"] == {"news_stale": "YES", "verdict": "REJECT"}
    assert parsed.spec("BTC/USD")["quality_levels"] == [2, 2, 1, 0]
    assert parsed.spec("ETH/USD")["pick"] == {} and parsed.spec("ETH/USD")["quality_levels"] is None
    for raw in ({"X": {"pick": {"unsupported_inference": "NO"}}},
                {"X": {"pick": {"news_stale": "MAYBE"}}}, {"X": {"pick": ["YES"]}},
                {"X": {"quality_levels": [2, 2, 2]}}, {"X": {"quality_levels": [3, 0, 0, 0]}},
                {"X": {"quality_levels": [1.0, 1, 1, 1]}}):
        with pytest.raises(session_script.SessionRefused, match="^FIXTURE_SCRIPT_INVALID$"):
            script(raw)
    kinds = {session_script._request_kind(qs.questions, {"symbol": "BTC/USD"})
             for qs in session_script.PICK_SETS.values()}
    assert kinds == {("TOPK_NEWS", "BTC/USD"), ("TOPK_CHART", "BTC/USD"),
                     ("TOPK_BOTH", "BTC/USD")}
    assert session_script._request_kind(session_script.QUALITY_V3.questions,
                                        {"symbol": "BTC/USD"}) == ("QUALITY_V3", "BTC/USD")


# Owner-bucket pairs the session classifies; picks outside them are refused at intake.
TOPK_PICKS = {"BTC/USD": "NEWS", "ETH/USD": "CHART", "SOL/USD": "BOTH", "XRP/USD": "NEWS",
              "UNI/USD": "CHART", "LINK/USD": "BOTH", "AVAX/USD": "NEWS"}
TOPK_SCRIPT = {
    "BTC/USD": {"quality": "STRONG", "quality_levels": [2, 2, 2, 2], "pick": {"verdict": "REJECT"}},
    "ETH/USD": {"pick": {"setup_already_broken": "YES"}, "quality": "STRONG"},  # Vetoed.
    "SOL/USD": {"pick": {"already_priced": "HIGH"}, "quality": "STRONG",
                "quality_levels": [2, 2, 2, 2]},  # Uncertain: 100 - 10.
    "XRP/USD": {"quality": "ADEQUATE"},
    "UNI/USD": {"pick": {"factual_claims_supported": "PARTIALLY_SUPPORTED"}},
    "LINK/USD": {"quality": "WEAK"},
    "AVAX/USD": {"quality": "WEAK", "quality_levels": [0, 0, 0, 0]},
}


def topk_report(now, *, agent_id=session_script.AGENT_ID, **fields):
    from tests.test_research_report_v3 import pick, report_v3

    picks = [pick(i, symbol, kind=kind, now=now) for i, (symbol, kind) in enumerate(
        TOPK_PICKS.items())]
    return report_v3(picks, now=now, agent_id=agent_id, skipped=[], **fields)


def test_topk_session_ranks_a_v3_report_publishes_k_and_exports_the_ranking(make_session,
                                                                           tmp_path):
    session = make_session(selection_rule="TOPK", topk_k=5, management_reviews="DISABLED",
                           fixture=session_script.FixtureScript(TOPK_SCRIPT))
    assert session.activation["body"]["selection_policy"] == "JEV_TOP_K_SELECTION_V1"
    assert session._configuration()["topk_k"] == 5
    received = submit(session, topk_report(datetime.now(UTC)))
    assert received["report_schema_version"] == "AGENT_RESEARCH_REPORT_V3"
    ticked = session.tick()
    [cycle] = ticked["cycles"]
    assert (cycle["selection_policy"], cycle["topk_k"]) == ("JEV_TOP_K_SELECTION_V1", 5)
    assert cycle["ranking"]["counts"] == {"RANKED": 6, "VETOED": 1, "NOT_RANKED": 0}
    rows = {row["symbol"]: row for row in cycle["items"]}
    ranks = {symbol: row["topk"]["rank"] for symbol, row in rows.items()}
    assert ranks == {"BTC/USD": 1, "SOL/USD": 2, "XRP/USD": 3, "UNI/USD": 4, "LINK/USD": 5,
                     "AVAX/USD": 6, "ETH/USD": None}
    assert rows["ETH/USD"]["topk"]["veto_reasons"] == ["SETUP_ALREADY_BROKEN_YES"]
    assert rows["SOL/USD"]["topk"]["uncertain"] == ["ALREADY_PRICED_HIGH"]
    assert (rows["SOL/USD"]["topk"]["quality_score"], rows["SOL/USD"]["topk"]["adjusted_score"]) \
        == ("100.0000", "90.0000")
    assert rows["BTC/USD"]["topk"]["dissent"] == "REJECT"  # Recorded; never blocks.
    assert {s for s, row in rows.items() if row["selected"]} == {
        "BTC/USD", "SOL/USD", "XRP/USD", "UNI/USD", "LINK/USD"}  # K = 5.
    assert rows["BTC/USD"]["v2"]["reason"] == "REPLAY_NOT_APPLICABLE_QUESTION_SET"
    assert ticked["open_evidence_tasks"] == []  # Top-K has no evidence loop.
    text = session_script.render_tick(json.loads(json.dumps(ticked, default=str)))
    assert "RANK" in text and "top-K: K 5; 6 ranked, 1 vetoed" in text
    assert "SETUP_ALREADY_BROKEN_YES" in text
    # Two reviews per pick: its kind's question set and QUALITY_V3.
    assert Counter(call.split(":")[0] for call in session.fixture.calls) == {
        "TOPK_NEWS": 3, "TOPK_CHART": 2, "TOPK_BOTH": 2, "QUALITY_V3": 7}
    executed = session.execute(simulate_prints=False)
    assert {row["symbol"]: row["admission"]["outcome"] for row in executed["admissions"]} == {
        s: "ADMITTED" for s in ("BTC/USD", "SOL/USD", "XRP/USD", "UNI/USD", "LINK/USD")}
    # The system check read the session's simulated quote at each pick's own current price
    # (100.50, entry 100: a pullback) and recorded it as simulation.
    for row in executed["admissions"]:
        state = session.engine._load(row["admission"]["setup_id"])[1]
        check = state["system_check"]
        assert (state["entry_type"], check["result"], check["live"]["quote_source"]) == (
            "PULLBACK", "PASSED", session_script.SIMULATION)
        assert (D(check["live"]["ask"]), D(check["live"]["bid"])) == (
            D("100.50"), D("100.44975"))
    output = tmp_path / "export"
    manifest = session.export(output)
    assert manifest["audit"]["valid"] and sorted(manifest["files"]) == sorted(EXPORTED)
    [exported] = json.loads((output / "decisions.json").read_text())["cycles"]
    assert exported["cycle_id"] == received["cycle_id"] and exported["selection_skips"] == []
    ranking = exported["ranking"]
    assert (ranking["policy"], ranking["k"], ranking["complete"]) == (
        "JEV_TOP_K_SELECTION_V1", 5, True)
    assert [e["item_key"] for e in ranking["entries"]][:6] == [
        "CRYPTO:" + s for s in ("BTC/USD", "SOL/USD", "XRP/USD", "UNI/USD", "LINK/USD",
                                "AVAX/USD")]
    items = {item["symbol"]: item for item in exported["items"]}
    [btc] = items["BTC/USD"]["revisions"]
    assert (btc["topk"]["rank"], btc["topk"]["status"]) == (1, "RANKED")
    assert (btc["selection"]["rank"], btc["selection"]["adjusted_score"],
            btc["selection"]["k"], btc["selection"]["replacement_for"]) == (
        1, "100.0000", 5, None)
    assert btc["setup"]["setup_id"] and btc["decision"]["disposition"] == "RANKABLE"
    [avax] = items["AVAX/USD"]["revisions"]
    assert avax["selection"] is None and avax["topk"]["rank"] == 6
    exported_text = "".join(p.read_text() for p in output.rglob("*") if p.is_file())
    assert not any(token in exported_text for token in session.tokens.values())


def test_extra_crypto_pairs_join_the_universe_in_the_crypto_other_bucket(make_session):
    from tests.test_research_report_v3 import pick, report_v3

    extra = ("ARB/USD", "BONK/USD")
    assert session_script.SessionConfig("fixture").universe_pairs == session_script.CRYPTO_PAIRS
    session = make_session(selection_rule="TOPK", topk_k=5, management_reviews="DISABLED",
                           extra_crypto_pairs=extra,
                           fixture=session_script.FixtureScript({"*": {"quality": "STRONG"}}))
    configuration = session._configuration()
    assert configuration["extra_crypto_pairs"] == list(extra)
    assert configuration["crypto_buckets"]["unlisted"] == "CRYPTO_OTHER"
    assert set(configuration["report_v3_universe"]["symbols"]) == set(
        session_script.CRYPTO_PAIRS) | set(extra)
    with session.repo.connect() as conn:
        themes = {row["ticker"]: row["theme"] for row in conn.execute(
            "SELECT ticker, theme FROM lab.current_classifications WHERE ticker = ANY(%s)",
            (["ARB/USD", "BONK/USD", "BTC/USD"],)).fetchall()}
    assert themes == {"ARB/USD": "CRYPTO_OTHER", "BONK/USD": "CRYPTO_OTHER", "BTC/USD": "L1"}
    now = datetime.now(UTC)
    received = submit(session, report_v3([pick(0, "ARB/USD", kind="CHART", now=now),
                                          pick(1, "BTC/USD", kind="CHART", now=now)],
                                         now=now, agent_id=session_script.AGENT_ID, skipped=[]))
    assert (received["submitted_count"], received["rejected_count"]) == (2, 0)
    [cycle] = session.tick()["cycles"]
    assert {row["symbol"]: row["topk"]["status"] for row in cycle["items"]} == {
        "ARB/USD": "RANKED", "BTC/USD": "RANKED"}


def test_without_extra_pairs_a_pick_outside_the_owner_buckets_is_refused(make_session):
    from tests.test_research_report_v3 import pick, report_v3

    session = make_session(selection_rule="TOPK", topk_k=5, management_reviews="DISABLED",
                           fixture=session_script.FixtureScript({"*": {"quality": "STRONG"}}))
    assert session._configuration()["crypto_buckets"]["unlisted"] == "REFUSE"
    now = datetime.now(UTC)
    received = submit(session, report_v3([pick(0, "ARB/USD", kind="CHART", now=now),
                                          pick(1, "BTC/USD", kind="CHART", now=now)],
                                         now=now, agent_id=session_script.AGENT_ID, skipped=[]))
    assert (received["submitted_count"], received["rejected_count"]) == (2, 1)
    [refused] = [row for row in received["item_results"] if row["status"] != "ACCEPTED"]
    assert refused["code"] == "SYMBOL_NOT_IN_UNIVERSE"


def test_v2_reports_are_refused_in_a_topk_session(make_session):
    session = make_session(selection_rule="TOPK")
    reply = TestClient(session.app).post(
        ROUTE, json=session_report(["BTC/USD"], datetime.now(UTC)),
        headers=bearer(session.tokens[session_script.AGENT_ID]))
    assert (reply.status_code, reply.json()) == (422, {"detail": "REPORT_V3_REQUIRED"})
    assert session.tick()["cycles"] == []


def test_topk_session_over_the_real_cli(session_root, tmp_path):
    """Top-K end to end: ``submit`` fills a V3 report's run slot, context time and validity
    from the session schedule; the fixture Jev reviews every pick; the ranking is written once;
    K picks are published and admitted; decisions.json carries the ranking."""
    script = tmp_path / "fixture.json"
    script.write_text(json.dumps(TOPK_SCRIPT))
    log = tmp_path / "start.log"
    with open(log, "w") as stream:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "start", "--root", str(session_root), "--port", "0",
             "--jev", "fixture", "--fixture-script", str(script), "--max-jev-calls", "20",
             "--selection-rule", "TOPK", "--topk-k", "5", "--management-reviews", "DISABLED"],
            cwd=REPOSITORY, stdout=stream, stderr=subprocess.STDOUT,
        )
    runs = []

    def run(*args):
        result = cli(*args)
        runs.append(result)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    try:
        descriptor = wait_ready(session_root, process, log)
        configuration = descriptor["configuration"]
        assert (configuration["selection_rule"], configuration["topk_k"]) == (
            "JEV_TOP_K_SELECTION_V1", 5)
        raw = topk_report(datetime.now(UTC))
        for field in ("run_slot", "context_as_of", "valid_until"):
            del raw[field]  # The client helper fills them from the session schedule.
        del raw["agent"]["run_id"]
        report_file = tmp_path / "report.json"
        report_file.write_text(json.dumps(raw))
        submitted = json.loads(run("submit", "--root", session_root, "--report",
                                   report_file).stdout)
        assert submitted["http_status"] == 202, submitted
        body = submitted["body"]
        assert (body["report_schema_version"], body["contender_count"]) == (
            "AGENT_RESEARCH_REPORT_V3", 7)
        ticked = json.loads(run("tick", "--root", session_root, "--json").stdout)
        [cycle] = ticked["cycles"]
        assert cycle["ranking"]["counts"] == {"RANKED": 6, "VETOED": 1, "NOT_RANKED": 0}
        table = run("tick", "--root", session_root).stdout
        assert "top-K: K 5" in table and "VETO / UNCERTAIN" in table
        executed = json.loads(run("execute", "--root", session_root, "--json").stdout)
        assert sorted(row["admission"]["outcome"] for row in executed["admissions"]) == [
            "ADMITTED"] * 5
        output = tmp_path / "export"
        manifest = json.loads(run("export", "--root", session_root, "--output", output,
                                  "--json").stdout)
        assert manifest["audit"]["valid"] and manifest["jev_calls"]["used"] == 14
        [decisions] = json.loads((output / "decisions.json").read_text())["cycles"]
        assert decisions["ranking"]["counts"]["RANKED"] == 6
        assert sum(1 for item in decisions["items"] if item["revisions"][0]["selection"]) == 5
        stopped = run("stop", "--root", session_root)
        assert "cluster STOPPED" in stopped.stdout
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
    tokens = [Path(path).read_text().strip() for path in descriptor["token_files"].values()]
    printed = [log.read_text(), (session_root / "session.json").read_text()] + [
        r.stdout + r.stderr for r in runs]
    assert not any(token in text for token in tokens for text in printed)
