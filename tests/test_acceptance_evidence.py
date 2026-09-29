"""Fixture rehearsal for the read-only acceptance-evidence collector (plan 0.10, gate G3).

Drives a full managed lifecycle through ``ManagedExecution`` (reusing the helpers in
``tests/test_managed_execution.py`` and ``tests/test_position_monitor.py``) plus a handful of
``ManagedRuntime`` event-writing methods called directly, so genuine subscription-acknowledgement
and print evidence exists without standing up the full async WebSocket loop. Runs the collector
and the CLI script against the result over a real ``catalyst_review`` connection (never
``catalyst_app``) on a disposable database.
"""

import json
import re
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from catalyst_lab.acceptance_evidence import (
    MANIFEST_VERSION,
    RoleRefused,
    SetupNotFound,
    _absent,
    build_checks,
    canonical_json,
    collect_acceptance_evidence,
    fee_section,
    fills_section,
    halts_section,
    protection_section,
    risk_decision_section,
    rows_sha256,
    scan_for_credential_shapes,
    stream_subscription_section,
    verify_audit_export,
)
from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.managed_runtime import ManagedRuntime, engineering_runtime_policy
from scripts import managed_acceptance_evidence as script
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx
from tests.test_managed_execution import observation, packet
from tests.test_position_monitor import opened

FIXTURE_CREDS = AlpacaCredentials("PKFIXTURE000000000001", "fixture-no-real-provider-secret")
G3_CHECK_NAMES = {
    "SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER",
    "RISK_DECISION_CLAIMED_WITHIN_TTL",
    "FILL_QUANTITY_MATCHES_AUTHORIZED_QUANTITY",
    "PROTECTION_ACKNOWLEDGED_FOR_FILL",
    "EXIT_FILL_CLOSES_POSITION",
    "ZERO_RESIDUAL_NO_ACTIVE_RESERVATION",
    "ZERO_RESIDUAL_NO_OPEN_POSITION_EVENT",
    "ZERO_RESIDUAL_NO_HALTS",
    "ZERO_RESIDUAL_CLEAN_RECONCILIATION_AFTER_EXIT",
}
# Migration 019 grants catalyst_review SELECT on lab.execution_halts and lab.trade_events, so
# every section and the audit chain are readable under the tool's own role. The privilege probe
# is unchanged: ``test_without_the_019_grants_both_sections_fail_closed_as_before`` revokes both
# grants on a disposable database and shows the pre-019 fail-closed behaviour.


def _with_role(database_url, role):
    """``engine.repo`` (a ``RiskRepository``) already carries ``user=catalyst_risk``, not
    ``catalyst_app``, so this replaces whatever role is present rather than assuming one."""
    return re.sub(r"user=\w+", f"user={role}", database_url)


def review_url(mx):
    engine, _, _ = mx
    return _with_role(engine.repo.database_url, "catalyst_review")


def reporting_url(mx):
    engine, _, _ = mx
    return _with_role(engine.repo.database_url, "catalyst_reporting")


def _connect(url):
    return psycopg.connect(url, row_factory=dict_row, options="-c timezone=UTC", connect_timeout=5)


def _light_runtime(mx, symbol, *, market):
    """A ``ManagedRuntime`` wired to the fixture's real database, used only to call its plain
    event-writing methods (``_event``, ``_append_trade``, ``_consume_trade``) directly — the
    same code a real run's async loops call, without needing a simulated WebSocket handshake.
    """
    engine, venue, _ = mx
    run = ManagedRuntime(
        engine,
        None,
        SimpleNamespace(policy=SimpleNamespace(stock_feed="iex")),
        FIXTURE_CREDS,
        engineering_runtime_policy(),
        clock=lambda: venue.now,
        reviewer_heartbeat=lambda: True,
    )
    assert run._event("RUNTIME_STREAM_CONNECTED", {"stream": "trade_updates"})
    feed = "iex" if market == "US" else "CRYPTO_US"
    assert run._event(
        "RUNTIME_MARKET_CONNECTED", {"market": market, "feed": feed, "symbols": [symbol]}
    )
    return run


def opened_with_runtime_evidence(mx, symbol="SPY"):
    """Admit, write genuine subscription acks, persist a real ``MARKET_PRINT``, then trigger,
    fill and protect — an OPEN (not yet closed) lifecycle with the full pre-fill evidence trail.
    """
    engine, venue, _ = mx
    run = _light_runtime(mx, symbol, market="US")
    p = packet(mx, symbol)
    sid = engine.admit(p)
    with engine.repo.connect() as conn:
        setup_row = engine.store.setup(conn, sid)
        state_row = engine.store.state(conn, sid)
    printed = {**observation(mx), "trade_id": str(uuid4())}
    run._append_trade({**setup_row, "state": state_row}, printed)
    with engine.repo.connect() as conn:
        print_row = conn.execute(
            "SELECT * FROM lab.managed_events WHERE setup_id=%s AND kind='MARKET_PRINT' "
            "ORDER BY event_seq DESC LIMIT 1",
            (sid,),
        ).fetchone()
    decision = engine.observe_trigger(sid, printed)
    assert decision["outcome"] == "APPROVED"
    run._consume_trade(print_row, "TRIGGER_CHECKED")
    entry = venue.orders_of("buy")[0]
    assert engine.ingest(venue.fill(entry["id"], entry["qty"], price=entry["limit_price"]))
    engine.manage(sid, printed)
    return str(sid)


def closed_stock_lifecycle(mx, symbol="SPY"):
    """The above, driven on to a native bracket target fill (OCO cancels the stop leg) and a
    clean reconciliation — mirrors tests/test_complete_managed_cycle.py's US_STOCKS exit path.
    """
    engine, venue, _ = mx
    sid = opened_with_runtime_evidence(mx, symbol)
    targets = [
        o
        for o in venue.orders_of("sell", "limit")
        if o["symbol"] == symbol and o["status"] == "new"
    ]
    target = targets[0]
    assert engine.ingest(venue.fill(target["id"], target["qty"], price=target["limit_price"]))
    # A full fill leaves the take-profit leg "new" and the stop-loss leg "held" (the documented
    # Alpaca bracket behavior; broker_ledger.classify_bracket treats "held" as still active/
    # protected). Alpaca's own OCO then cancels the sibling once the other leg fills, regardless
    # of which of those two statuses it was sitting in.
    for stop in venue.orders_of("sell", "stop"):
        if stop["symbol"] == symbol and stop["status"] in ("new", "held"):
            stop["status"] = "canceled"
    # manage()'s return value is a bare status string on some paths (_manage_stock), so the
    # state is re-read the same way tests/test_complete_managed_cycle.py does.
    engine.manage(sid, observation(mx, bid=target["limit_price"], ask=target["limit_price"]))
    assert engine._load(sid)[1]["state"] == "CLOSED"
    reconciliation = engine.reconcile()
    assert reconciliation["clean"]
    return sid


def _export_rows(mx):
    engine, _, _ = mx
    return engine.repo.export_events()


def _admission_seq(mx, sid):
    engine, _, _ = mx
    with engine.repo.connect() as conn:
        return conn.execute(
            "SELECT event_seq FROM lab.managed_setups WHERE setup_id=%s", (sid,)
        ).fetchone()["event_seq"]


# ---------------------------------------------------------------------------
# Role boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["catalyst_app", "catalyst_risk", "lab_owner"])
def test_forbidden_roles_are_refused(mx, role):
    engine, _, _ = mx
    url = _with_role(engine.repo.database_url, role)
    with _connect(url) as conn, pytest.raises(RoleRefused):
        collect_acceptance_evidence(conn, latest_closed=True)


def test_unlisted_role_is_refused(mx):
    engine, _, _ = mx
    url = _with_role(engine.repo.database_url, "catalyst_jev")
    with _connect(url) as conn, pytest.raises(RoleRefused):
        collect_acceptance_evidence(conn, latest_closed=True)


def test_reporting_role_is_refused_cleanly(mx):
    """catalyst_reporting is an allowed role name (it is genuinely read-only) but the frozen V1
    strategy_*/public_* grants it holds carry none of the managed-engine tables, not even
    lab.managed_setups — verified empirically, not assumed. The collector refuses it with a
    clear reason rather than crashing on a raw permission-denied error or silently returning a
    manifest with every section absent."""
    sid = closed_stock_lifecycle(mx)
    with _connect(reporting_url(mx)) as conn, pytest.raises(RoleRefused, match="managed_setups"):
        collect_acceptance_evidence(conn, setup_id=sid)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_lifecycle_every_available_section_present_and_checks_pass(mx):
    sid = closed_stock_lifecycle(mx)
    audit_rows = _export_rows(mx)
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(
            conn, setup_id=sid, audit_export_rows=audit_rows
        )
    assert manifest["manifest_version"] == MANIFEST_VERSION
    assert manifest["setup_id"] == sid

    for name, section in manifest["sections"].items():
        if name == "broker_snapshot":
            continue  # Optional; not supplied in this test.
        assert section["present"], (name, section["summary"])
    # Readable under catalyst_review since migration 019: no active halt, and it is checked.
    assert manifest["sections"]["halts"]["summary"] == {"active_halt_count": 0, "reasons": []}

    checks = {c["name"]: c for c in manifest["checks"]}
    assert set(checks) == G3_CHECK_NAMES
    for name, check in checks.items():
        assert check["passed"] is True, (name, check["detail"])

    assert "halts" not in manifest["unknowns"]
    assert manifest["unknowns"]["fees"]["reason"] == "FEE_NOT_REPORTED_BY_BROKER_EVENT"

    audit = manifest["audit"]
    assert audit["present"] is True and audit["source"] == "AUDIT_EXPORT"
    chain = audit["chain"]
    assert chain["valid"] is True
    assert chain["head_before"]["event_seq"] == manifest["audit"]["admission_event_seq"]
    assert chain["head_after"]["event_seq"] >= chain["head_before"]["event_seq"]
    assert chain["head_before"]["hash"] != chain["head_after"]["hash"]


def test_audit_chain_is_read_from_the_ledger_under_the_review_role(mx):
    """Without --audit-export the collector verifies lab.trade_events itself (migration 019
    grant) and reaches exactly the heads an owner export gives."""
    sid = closed_stock_lifecycle(mx)
    with _connect(review_url(mx)) as conn:
        direct = collect_acceptance_evidence(conn, setup_id=sid)
    with _connect(review_url(mx)) as conn:
        exported = collect_acceptance_evidence(
            conn, setup_id=sid, audit_export_rows=_export_rows(mx)
        )
    audit = direct["audit"]
    assert audit["present"] is True and audit["source"] == "LAB_TRADE_EVENTS"
    assert audit["export_supplied"] is False and "reason" not in audit
    assert audit["chain"]["valid"] is True
    assert audit["chain"] == exported["audit"]["chain"]
    assert direct["sections"] == exported["sections"]


def test_an_active_halt_is_read_and_fails_the_zero_residual_check(mx):
    engine, _, _ = mx
    sid = closed_stock_lifecycle(mx)
    with psycopg.connect(_with_role(engine.repo.database_url, "catalyst_operator")) as conn:
        conn.execute("SELECT lab.operator_pause('Acceptance evidence fixture pause')")
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid)
    halts = manifest["sections"]["halts"]
    assert halts["present"] is True
    assert halts["summary"] == {"active_halt_count": 1, "reasons": ["OPERATOR_PAUSE"]}
    checks = {c["name"]: c for c in manifest["checks"]}
    assert checks["ZERO_RESIDUAL_NO_HALTS"]["passed"] is False
    assert checks["ZERO_RESIDUAL_NO_HALTS"]["detail"] == "1 active halt(s) recorded."


def test_without_the_019_grants_both_sections_fail_closed_as_before(mx):
    """The existing privilege probe needs no change: on a ledger without the grants (revoked
    here, on a disposable database) both sections are reported unavailable, never assumed."""
    engine, _, _ = mx
    sid = closed_stock_lifecycle(mx)
    with psycopg.connect(_with_role(engine.repo.database_url, "lab_owner")) as conn:
        conn.execute("REVOKE SELECT ON lab.execution_halts,lab.trade_events FROM catalyst_review")
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid)
    assert manifest["sections"]["halts"]["present"] is False
    assert manifest["unknowns"]["halts"] == "ROLE_CANNOT_READ_LAB_EXECUTION_HALTS"
    checks = {c["name"]: c for c in manifest["checks"]}
    assert checks["ZERO_RESIDUAL_NO_HALTS"]["passed"] is False
    audit = manifest["audit"]
    assert audit["present"] is False and audit["chain"] is None and audit["source"] is None
    assert audit["reason"] == "ROLE_CANNOT_READ_LAB_TRADE_EVENTS"


def test_manifest_matches_across_two_independent_collections(mx):
    sid = closed_stock_lifecycle(mx)
    with _connect(review_url(mx)) as conn:
        first = collect_acceptance_evidence(conn, setup_id=sid)
    with _connect(review_url(mx)) as conn:
        second = collect_acceptance_evidence(conn, setup_id=sid)
    assert canonical_json(first) == canonical_json(second)
    assert rows_sha256(first) == rows_sha256(second)


def test_latest_closed_resolves_to_the_setup_just_closed(mx):
    sid = closed_stock_lifecycle(mx)
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, latest_closed=True)
    assert manifest["setup_id"] == sid


def test_setup_not_found_is_explicit(mx):
    with _connect(review_url(mx)) as conn, pytest.raises(SetupNotFound):
        collect_acceptance_evidence(conn, setup_id=str(uuid4()))


def test_no_closed_setup_is_explicit(mx):
    with _connect(review_url(mx)) as conn, pytest.raises(SetupNotFound):
        collect_acceptance_evidence(conn, latest_closed=True)


# ---------------------------------------------------------------------------
# CLI script, byte-identical output, private permissions
# ---------------------------------------------------------------------------


def test_script_writes_byte_identical_private_evidence_across_two_runs(mx, tmp_path):
    sid = closed_stock_lifecycle(mx)
    out1, out2 = tmp_path / "run1", tmp_path / "run2"
    rc1 = script.main(
        ["--database-url", review_url(mx), "--setup-id", sid, "--output", str(out1)]
    )
    rc2 = script.main(
        ["--database-url", review_url(mx), "--setup-id", sid, "--output", str(out2)]
    )
    # rc == 0: every check passes under catalyst_review, halts included (migration 019).
    assert rc1 == 0 and rc2 == 0
    body1, body2 = (out1 / "evidence.json").read_bytes(), (out2 / "evidence.json").read_bytes()
    assert body1 == body2
    assert (out1 / "evidence.sha256").read_bytes() == (out2 / "evidence.sha256").read_bytes()
    import hashlib

    assert (out1 / "evidence.sha256").read_text().strip() == hashlib.sha256(body1).hexdigest()
    assert oct(out1.stat().st_mode)[-3:] == "700"
    assert oct((out1 / "evidence.json").stat().st_mode)[-3:] == "600"
    assert oct((out1 / "evidence.sha256").stat().st_mode)[-3:] == "600"


def test_script_refuses_to_reuse_an_existing_output_directory(mx, tmp_path):
    sid = closed_stock_lifecycle(mx)
    out = tmp_path / "run"
    out.mkdir()
    with pytest.raises(SystemExit):
        script.main(["--database-url", review_url(mx), "--setup-id", sid, "--output", str(out)])


def test_script_refuses_forbidden_role_without_writing_anything(mx, tmp_path):
    engine, _, _ = mx
    url = engine.repo.database_url.replace("user=catalyst_app", "user=catalyst_risk")
    out = tmp_path / "run"
    rc = script.main(["--database-url", url, "--latest-closed", "--output", str(out)])
    assert rc == 1
    assert not out.exists()


def test_script_with_audit_export_and_broker_snapshot(mx, tmp_path):
    engine, _, _ = mx
    sid = closed_stock_lifecycle(mx)
    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        "\n".join(json.dumps(row) for row in engine.repo.export_events()) + "\n"
    )
    snapshot_path = tmp_path / "broker-snapshot.json"
    snapshot_path.write_text(
        json.dumps({"captured_at": "2026-09-24T12:00:00Z", "positions": [], "open_orders": []})
    )
    out = tmp_path / "run"
    rc = script.main(
        [
            "--database-url",
            review_url(mx),
            "--setup-id",
            sid,
            "--output",
            str(out),
            "--audit-export",
            str(export_path),
            "--broker-snapshot",
            str(snapshot_path),
        ]
    )
    assert rc == 0  # Every check passes, halts included (migration 019).
    manifest = json.loads((out / "evidence.json").read_bytes())
    assert manifest["audit"]["chain"]["valid"] is True
    assert manifest["audit"]["source"] == "AUDIT_EXPORT"
    assert manifest["sections"]["broker_snapshot"]["present"] is True
    assert manifest["sections"]["broker_snapshot"]["summary"]["flat_and_no_open_orders"] is True


# ---------------------------------------------------------------------------
# Required negative/edge behaviors
# ---------------------------------------------------------------------------


def test_tampered_audit_export_fails_the_chain_check(mx):
    sid = closed_stock_lifecycle(mx)
    admission_seq = _admission_seq(mx, sid)
    rows = list(_export_rows(mx))
    tampered = [dict(row) for row in rows]
    tampered[-1] = {**tampered[-1], "event_hash": "0" * 64}

    result = verify_audit_export(tampered, admission_seq)
    assert result["valid"] is False
    assert result["error"]

    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid, audit_export_rows=tampered)
    assert manifest["audit"]["chain"]["valid"] is False
    assert manifest["audit"]["chain"]["error"]


def test_gapped_export_fails_the_chain_check(mx):
    """Removing a row from the middle breaks contiguity, not just the admission lookup — the
    whole-chain verification catches it first, exactly like a tampered event does."""
    sid = closed_stock_lifecycle(mx)
    rows = [dict(row) for row in _export_rows(mx)]
    admission_seq = _admission_seq(mx, sid)
    without_admission = [row for row in rows if row["seq"] != admission_seq]
    result = verify_audit_export(without_admission, admission_seq)
    assert result["valid"] is False
    assert result["error"]
    assert result["head_after"] is None  # The chain itself is broken, no head is trustworthy.


def test_admission_seq_not_present_in_an_otherwise_valid_export_fails_the_chain_check(mx):
    """A complete, internally consistent export (e.g. from a different setup's admission
    reference, or a typo) that simply never contains the requested admission sequence."""
    closed_stock_lifecycle(mx)
    rows = [dict(row) for row in _export_rows(mx)]
    bogus_seq = max(row["seq"] for row in rows) + 1_000_000
    result = verify_audit_export(rows, bogus_seq)
    assert result["valid"] is False
    assert result["error"] == "ADMISSION_EVENT_SEQ_NOT_IN_EXPORT"
    assert result["head_after"] is not None  # The whole-chain head is still reported.


def test_still_open_position_reports_the_residual_checks_as_failed(mx):
    sid = opened_with_runtime_evidence(mx)
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid)
    checks = {c["name"]: c["passed"] for c in manifest["checks"]}
    assert checks["EXIT_FILL_CLOSES_POSITION"] is False
    assert checks["ZERO_RESIDUAL_NO_ACTIVE_RESERVATION"] is False
    assert checks["ZERO_RESIDUAL_NO_OPEN_POSITION_EVENT"] is False
    assert manifest["sections"]["state_closed"]["summary"]["is_closed"] is False
    assert manifest["sections"]["reservations"]["summary"]["active_reservation_present"] is True


def test_still_open_position_also_reuses_the_position_monitor_opened_helper(mx):
    """Same residual-failure shape reached through tests/test_position_monitor.py::opened, the
    helper the task names explicitly, to prove the collector does not depend on how the OPEN
    state was reached."""
    sid = opened(mx, "AAPL")
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid)
    checks = {c["name"]: c["passed"] for c in manifest["checks"]}
    assert checks["EXIT_FILL_CLOSES_POSITION"] is False
    assert checks["ZERO_RESIDUAL_NO_ACTIVE_RESERVATION"] is False


def test_missing_fee_is_an_explicit_unknown(mx):
    sid = closed_stock_lifecycle(mx)
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(conn, setup_id=sid)
    fees = manifest["sections"]["fees"]
    assert fees["present"] is True
    assert fees["summary"]["fees_fully_known"] is False
    assert fees["summary"]["unknown_fee_count"] == fees["summary"]["fill_count"] > 0
    assert manifest["unknowns"]["fees"] == {
        "reason": "FEE_NOT_REPORTED_BY_BROKER_EVENT",
        "unknown_fill_ids": fees["summary"]["unknown_fill_ids"],
    }


def test_no_credential_shaped_string_or_account_identifier_in_output(mx):
    sid = closed_stock_lifecycle(mx)
    with _connect(review_url(mx)) as conn:
        manifest = collect_acceptance_evidence(
            conn, setup_id=sid, audit_export_rows=_export_rows(mx)
        )
    assert scan_for_credential_shapes(manifest) == []
    text = canonical_json(manifest)
    assert FIXTURE_CREDS.secret not in text
    assert FIXTURE_CREDS.key_id not in text
    assert "fixture-paper-account" not in text  # ManagedVenue's opaque account id (alpaca.py).


# ---------------------------------------------------------------------------
# Pure-function unit tests (no database) for branches the fixture lifecycle does not exercise.
# ---------------------------------------------------------------------------


def test_halts_section_reports_unavailable_without_guessing():
    section = halts_section((), available=False)
    assert section == {
        "present": False,
        "event_seqs": [],
        "sha256": None,
        "summary": {"reason": "ROLE_CANNOT_READ_LAB_EXECUTION_HALTS"},
    }


def test_halts_section_reports_available_and_empty():
    section = halts_section((), available=True)
    assert section["present"] is True
    assert section["summary"] == {"active_halt_count": 0, "reasons": []}


def test_fee_section_partial_known_fees():
    fills = [
        {"fill_id": "a", "fee_usd": None},
        {"fill_id": "b", "fee_usd": "0.42"},
    ]
    section = fee_section(fills)
    assert section["summary"]["fees_fully_known"] is False
    assert section["summary"]["unknown_fill_ids"] == ["a"]
    assert section["summary"]["total_known_fee_usd"] == "0.42"


def test_protection_section_crypto_plan_after_fill():
    events = [
        {
            "event_seq": 10,
            "kind": "BROKER_ACK",
            "body": {"action": "PROTECT", "order": {"stop_price": "95"}},
            "recorded_at": "2026-09-24T00:00:10Z",
        },
        {
            "event_seq": 12,
            "kind": "PROTECTION_PLAN",
            "body": {"state": "PROTECTED", "reason": "NATIVE_STOP_ACTIVE"},
            "recorded_at": "2026-09-24T00:00:12Z",
        },
    ]
    section = protection_section(events, first_fill_event_seq=5)
    assert section["present"] is True
    assert section["summary"]["protected"] is True
    assert section["summary"]["native_protect_ack_after_fill_event_seq"] == 10
    assert section["summary"]["last_plan_state"] == "PROTECTED"


def test_stream_subscription_section_absent_without_runtime_events():
    assert stream_subscription_section(
        [], market="CRYPTO", symbol="BTC/USD", trigger_event_seq=5
    ) == {
        "present": False,
        "event_seqs": [],
        "sha256": None,
        "summary": {"reason": "NO_RUNTIME_SUBSCRIPTION_EVENT_OBSERVED_FOR_THIS_SETUP"},
    }


def test_fill_quantity_check_compares_numerically_not_by_text():
    """Two partial fills summing to Decimal("79.0") must still match an authorized qty of "79" —
    equal in value, not in text. This is the exact bug the live prove_managed_wiring.py rehearsal
    would not have caught (it only ever produces a single full fill)."""
    decisions = [
        {
            "decision_id": uuid4(),
            "action": "ENTRY",
            "outcome": "APPROVED",
            "reason": "RISK_APPROVED",
            "method": "POST",
            "path": "/v2/orders",
            "payload": {"qty": "79"},
            "context": {},
            "equity": "10000",
            "expires_at": "2026-09-24T00:00:05Z",
            "created_at": "2026-09-24T00:00:00Z",
            "event_seq": 10,
            "claimed": True,
        }
    ]
    fills = [
        {
            "fill_id": "f1",
            "broker_order_id": "o1",
            "side": "buy",
            "qty": "39.5",
            "price": "100",
            "filled_at": "2026-09-24T00:00:01Z",
            "fee_usd": None,
            "source": "ALPACA_PAPER",
            "event_seq": 11,
        },
        {
            "fill_id": "f2",
            "broker_order_id": "o1",
            "side": "buy",
            "qty": "39.5",
            "price": "100",
            "filled_at": "2026-09-24T00:00:02Z",
            "fee_usd": None,
            "source": "ALPACA_PAPER",
            "event_seq": 12,
        },
    ]
    risk_decision = risk_decision_section(decisions, trigger_event_seq=1)
    assert risk_decision["summary"]["entry_qty"] == "79"
    fills_summary = fills_section(fills, [])
    assert fills_summary["summary"]["buy_qty"] == "79.0"  # Confirms the formatting mismatch.

    absent = _absent("TEST_STUB")
    sections = {
        "stream_subscriptions": absent,
        "trigger_print": absent,
        "entry_eligibility": absent,
        "risk_check": absent,
        "risk_decision": risk_decision,
        "broker_ack": absent,
        "fills": fills_summary,
        "broker_position": absent,
        "protection": absent,
        "exit": absent,
        "state_closed": absent,
        "reconciliation": absent,
        "halts": halts_section((), available=False),
        "reservations": absent,
        "fees": absent,
        "broker_snapshot": absent,
    }
    checks = {c["name"]: c for c in build_checks(sections)}
    assert checks["FILL_QUANTITY_MATCHES_AUTHORIZED_QUANTITY"]["passed"] is True


def test_stream_subscription_section_after_trigger_is_not_satisfied():
    events = [
        {
            "event_seq": 20,
            "kind": "RUNTIME_MARKET_CONNECTED",
            "body": {"market": "US", "symbols": ["SPY"]},
            "recorded_at": "2026-09-24T00:00:20Z",
        },
        {
            "event_seq": 21,
            "kind": "RUNTIME_STREAM_CONNECTED",
            "body": {"stream": "trade_updates"},
            "recorded_at": "2026-09-24T00:00:21Z",
        },
    ]
    section = stream_subscription_section(
        events, market="US_STOCKS", symbol="SPY", trigger_event_seq=5
    )
    assert section["present"] is True
    assert section["summary"]["acknowledged_before_trigger"] is False
