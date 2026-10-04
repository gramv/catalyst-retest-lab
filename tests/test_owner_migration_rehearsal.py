"""Rehearsal of the owner's maintenance-window step (plan 0.8): a populated schema-13 account
ledger migrated to the current schema through the CLI, exactly as the live ledger will be.

Disposable /tmp cluster and LAB_FIXTURE rows only; no owner ledger, broker or provider contact.
The live ledger (managed-real-20260919-a) was left at schema 13 by the 2026-09-23 reboot.
"""

import json
import tempfile
from pathlib import Path

import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.cli import main
from catalyst_lab.config import SCHEMA_VERSION
from catalyst_lab.repository import Repository
from tests.test_localdb_guard import audit_head, ledger_migrate, mark, schema_version
from tests.test_operator_controls import audit_state, populate_schema14, start_cluster_at

LIVE_LEDGER_VERSION = 13
# Migrations 014, 015, 017, 018, 019, 020 and 021 are DDL-only; 016 seeds exactly these
# audited policy rows, and 022 appends two more: JEV_MANAGED_RISK_V3 and its CRYPTO terms;
# 026 three: JEV_MANAGED_RISK_V4, its CRYPTO terms and its daily limits; 031 four:
# JEV_MANAGED_RISK_V5, its terms, its daily limits and its strategy cap (package plugin-c3).
SEEDED_POLICY_ROWS = 3
V3_POLICY_EVENTS = 2
V4_POLICY_EVENTS = 3
V5_POLICY_EVENTS = 4


def test_populated_schema13_ledger_migrates_to_current_through_the_cli(monkeypatch, capsys,
                                                                       tmp_path):
    manifest = tmp_path / "manifest.json"  # ledger_ops backup writes the real one (step 4).
    manifest.write_text(json.dumps({"purpose": "LAB_FIXTURE"}))
    with tempfile.TemporaryDirectory(prefix="catalyst-0-8-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, LIVE_LEDGER_VERSION)
            populate_schema14(root)  # Halts, reconciliation runs, managed events, receipts.
            mark(root, "ACCOUNT_LEDGER")  # Marked first: dev-init is blocked from here on.
            before, audited_before, halts_before, version_before = audit_state(root)
            head_before, count_before = audit_head(root)
            assert version_before == LIVE_LEDGER_VERSION and before["valid"]
            assert len(halts_before) == 1
            assert all(n == ok for n, ok in audited_before.values())
            populated = {table for table, (n, _) in audited_before.items() if n}
            assert {"review_operator_events", "managed_events", "jev_requests",
                    "jev_receipts"} <= populated
            with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
                localdb.start(root)

            # Step 6 of the maintenance window, as the owner runs it.
            monkeypatch.setattr(
                "sys.argv", ledger_migrate(root, LIVE_LEDGER_VERSION, SCHEMA_VERSION, manifest)
            )
            main()
            result = json.loads(capsys.readouterr().out)

            # Step 7: the head moves by exactly the three seeded policy rows, 022's two V3 rows
            # and 026's three V4 rows, nothing else.
            appended = (SEEDED_POLICY_ROWS + V3_POLICY_EVENTS + V4_POLICY_EVENTS
                        + V5_POLICY_EVENTS)
            assert (result["before"], result["after"]) == (LIVE_LEDGER_VERSION, SCHEMA_VERSION)
            assert result["warning"] == "AUDIT_HEAD_CHANGED_BY_MIGRATION"
            assert result["audit_head_before"] == head_before
            assert (result["event_count_before"], result["event_count"]) == (
                count_before, count_before + appended)
            assert result["broker_requests"] == 0
            after, audited_after, halts_after, version_after = audit_state(root)
            assert version_after == schema_version(root) == SCHEMA_VERSION == 31
            assert after["valid"] and after["event_count"] == count_before + appended
            assert audit_head(root) == (result["audit_head_after"], count_before + appended)
            # Every historical audited row still verifies; the rename in 015 keeps every halt.
            assert {t: v for t, v in audited_after.items() if t in audited_before} == audited_before
            assert audited_after["account_risk_policies"] == (SEEDED_POLICY_ROWS + 3,) * 2
            assert audited_after["account_risk_market_terms"] == (3, 3)
            assert audited_after["account_risk_daily_limits"] == (2, 2)
            assert audited_after["ledger_account_binding"] == (0, 0)
            assert audited_after["operator_flatten_completions"] == (0, 0)  # 019: DDL only.
            assert halts_after == halts_before
            with psycopg.connect(localdb.connection_url(root, "lab_owner")) as conn:
                kept = conn.execute(
                    "SELECT event_hash FROM lab.trade_events ORDER BY seq OFFSET %s LIMIT 1",
                    (count_before - 1,),
                ).fetchone()[0]
                policies = conn.execute(
                    "SELECT policy_id FROM lab.account_risk_policies ORDER BY policy_id"
                ).fetchall()
                roles = {r[0] for r in conn.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname LIKE 'catalyst_%'"
                ).fetchall()}
            assert kept == head_before  # The old head is untouched at its sequence.
            assert [p[0] for p in policies] == [
                "CATALYST_RETEST_V1", "JEV_MANAGED_RISK_V2", "JEV_MANAGED_RISK_V3",
                "JEV_MANAGED_RISK_V4", "JEV_MANAGED_RISK_V5", "MUSE_JEV_MANAGED_TEST_V1"]
            assert "catalyst_operator" in roles  # Added by 015 for the release path.
            # The application roles accept the migrated ledger (preflight --check-database MATCH).
            Repository(localdb.connection_url(root)).check_role()
            RiskRepository(localdb.connection_url(root, "catalyst_risk")).check_role()
            # The marker still blocks dev-init, and a second run of the same step is refused.
            with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
                localdb.start(root)
            with pytest.raises(SystemExit, match="LEDGER_VERSION_MISMATCH"):
                main()
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def test_populated_schema20_ledger_migrates_to_21_through_the_cli_with_the_head_unchanged(
    monkeypatch, capsys, tmp_path,
):
    """Migration 021 (selection rule top-K) on a schema-20 ledger holding V2, B1 and B2
    selections: DDL only, so the audit head and event count stay exactly as they were, every
    earlier selection routes to the same admission answer, and top-K runs afterwards (once the
    owner's next step, 021 to 22, has appended exactly migration 022's two V3 rows)."""
    from datetime import UTC, datetime

    from catalyst_lab.managed_store import ManagedStore
    from tests.test_crypto_size_hold import MIGRATION_022_EVENTS, assert_migration_022_appended
    from tests.test_risk_v4 import V4_AUDIT_EVENTS
    from tests.test_risk_v5 import V5_AUDIT_ROWS
    from tests.test_selection_b1 import replay_script
    from tests.test_selection_b2 import failures, packet_variants, selected_packets
    from tests.test_selection_topk import run_topk_fixture_cycle

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"purpose": "LAB_FIXTURE"}))
    with tempfile.TemporaryDirectory(prefix="catalyst-021-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 20)
            populate_schema14(root)  # Halts, reconciliation runs, managed events, receipts.
            risk_url = localdb.connection_url(root, "catalyst_risk")
            jev_url = localdb.connection_url(root, "catalyst_jev")
            now = datetime.now(UTC)
            with monkeypatch.context() as patch:
                # The V2, B1 and B2 writers are unchanged; only the role check's schema pin is
                # relaxed to write this pre-migration fixture.
                patch.setattr("catalyst_lab.authorization.SCHEMA_VERSION", 20)
                replay_script.run_fixture_cycles(risk_url, jev_url, now=now)
                replay_script.run_fixture_b2_cycle(risk_url, jev_url, now=now)
                store = ManagedStore(RiskRepository(risk_url))
            variants = packet_variants(store, selected_packets(risk_url))
            before_routes = failures(risk_url, variants)
            mark(root, "ACCOUNT_LEDGER")  # The owner ledger marker blocks dev-init.
            proof, audited, halts, version = audit_state(root)
            head_before, count_before = audit_head(root)
            assert version == 20 and proof["valid"] and all(n == ok for n, ok in audited.values())
            with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
                localdb.start(root)

            monkeypatch.setattr("sys.argv", ledger_migrate(root, 20, 21, manifest))
            main()
            result = json.loads(capsys.readouterr().out)

            assert (result["before"], result["after"]) == (20, 21)
            assert "warning" not in result  # DDL only: nothing was appended to the history.
            assert result["audit_head_before"] == result["audit_head_after"] == head_before
            assert result["event_count_before"] == result["event_count"] == count_before
            assert result["broker_requests"] == 0
            after, audited_after, halts_after, version_after = audit_state(root)
            assert version_after == schema_version(root) == 21 and after == proof
            assert audit_head(root) == (head_before, count_before)
            assert audited_after == audited and halts_after == halts
            assert failures(risk_url, variants) == before_routes  # Every earlier route kept.
            # The next owner step, 21 to 26: exactly migration 022's two V3 rows and 026's
            # three V4 rows are appended (023, 024 and 025 are DDL only).
            monkeypatch.setattr("sys.argv", ledger_migrate(root, 21, SCHEMA_VERSION, manifest))
            main()
            step = json.loads(capsys.readouterr().out)
            assert (step["before"], step["after"]) == (21, SCHEMA_VERSION) == (21, 31)
            assert step["warning"] == "AUDIT_HEAD_CHANGED_BY_MIGRATION"
            assert step["event_count"] == count_before + MIGRATION_022_EVENTS + len(
                V4_AUDIT_EVENTS) + len(V5_AUDIT_ROWS)
            assert assert_migration_022_appended(
                localdb.connection_url(root, "lab_owner"), head_before,
                later=V4_AUDIT_EVENTS + V5_AUDIT_ROWS) == step["audit_head_after"]
            assert failures(risk_url, variants) == before_routes  # 022 changes no route.
            Repository(localdb.connection_url(root)).check_role()
            RiskRepository(risk_url).check_role()
            # Top-K runs on the migrated ledger and its selections cross the new branch.
            cycle_id = run_topk_fixture_cycle(risk_url, jev_url, now=now)
            published = selected_packets(risk_url, cycle_id)
            assert [p["rank"] for p in published] == [1, 2, 3, 4, 5]
            assert failures(risk_url, published) == [None] * 5
            with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
                localdb.start(root)
            with pytest.raises(SystemExit, match="LEDGER_VERSION_MISMATCH"):
                main()  # A second run of the same step is refused.
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)


def test_populated_schema21_ledger_migrates_to_22_with_exactly_the_v3_policy_append(
    monkeypatch, capsys, tmp_path,
):
    """Migration 022 (package crypto-size-hold) on a populated schema-21 ledger holding a frozen
    V1 reservation and a legacy managed reservation. The owner's CLI step appends exactly two
    audit events, the JEV_MANAGED_RISK_V3 row and its CRYPTO terms, hash-chained to the old head
    (which stays at its sequence); every earlier policy row, open reservation and account-risk
    answer is unchanged, and the application roles accept the migrated ledger."""
    from tests.test_account_risk_policy import populate_reservations
    from tests.test_crypto_size_hold import MIGRATION_022_EVENTS, assert_migration_022_appended
    from tests.test_risk_v4 import V4_AUDIT_EVENTS
    from tests.test_risk_v5 import V5_AUDIT_ROWS

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"purpose": "LAB_FIXTURE"}))
    # (policy, market, sector, theme, equity, budget): V1, legacy and V2 questions whose
    # answers span every outcome against the two reservations populate_reservations writes.
    questions = [
        ("CATALYST_RETEST_V1", "US_STOCKS", "S_MIGV", "T_NEW", 10000, 100),
        ("CATALYST_RETEST_V1", "US_STOCKS", "S_NEW", "T_NEW", 10000, 100),
        ("MUSE_JEV_MANAGED_TEST_V1", "US_STOCKS", "S_NEW", "T_MIGM", 10000, 100),
        ("JEV_MANAGED_RISK_V2", "US_STOCKS", "S_NEW", "T_NEW", 10000, 50),
        ("JEV_MANAGED_RISK_V2", "CRYPTO", "CRYPTO", "T_NEW", 10000, 50),
        ("JEV_MANAGED_RISK_V2", "FOREX", "S_NEW", "T_NEW", 10000, 50),
    ]
    with tempfile.TemporaryDirectory(prefix="catalyst-022-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 21)
            populate_schema14(root)  # Halts, reconciliation runs, managed events, receipts.
            populate_reservations(root)  # A frozen V1 and a legacy managed reservation.
            mark(root, "ACCOUNT_LEDGER")  # The owner ledger marker blocks dev-init.
            before, audited_before, halts_before, version_before = audit_state(root)
            head_before, count_before = audit_head(root)
            assert version_before == 21 and before["valid"]
            assert all(n == ok for n, ok in audited_before.values())
            assert audited_before["account_risk_policies"] == (SEEDED_POLICY_ROWS,) * 2
            owner_url = localdb.connection_url(root, "lab_owner")
            risk_url = localdb.connection_url(root, "catalyst_risk")

            def kept_state():
                with psycopg.connect(owner_url) as conn:
                    policies = conn.execute(
                        """SELECT to_jsonb(p) FROM lab.account_risk_policies p
                        WHERE policy_id NOT IN ('JEV_MANAGED_RISK_V3','JEV_MANAGED_RISK_V4',
                        'JEV_MANAGED_RISK_V5')
                        ORDER BY policy_id""").fetchall()
                    reservations = conn.execute(
                        """SELECT to_jsonb(r) FROM lab.account_risk_reservations r
                        ORDER BY source""").fetchall()
                with psycopg.connect(risk_url) as conn:
                    answers = [conn.execute(
                        "SELECT lab.account_risk_failure(%s,'ALPACA_PAPER',%s,%s,%s,%s,%s)",
                        question).fetchone()[0] for question in questions]
                return policies, reservations, answers

            kept = kept_state()
            assert kept[2] == ["CORRELATION_LIMIT", "MAX_OPEN_PLANNED_RISK", "CORRELATION_LIMIT",
                               None, None, "MARKET_RISK_CAP"]
            assert [r[0]["policy_id"] for r in kept[1]] == [
                "CATALYST_RETEST_V1", "MUSE_JEV_MANAGED_TEST_V1"]
            with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
                localdb.start(root)

            monkeypatch.setattr("sys.argv", ledger_migrate(root, 21, SCHEMA_VERSION, manifest))
            main()
            result = json.loads(capsys.readouterr().out)

            assert (result["before"], result["after"]) == (21, SCHEMA_VERSION) == (21, 31)
            assert result["warning"] == "AUDIT_HEAD_CHANGED_BY_MIGRATION"
            assert result["audit_head_before"] == head_before
            appended = MIGRATION_022_EVENTS + len(V4_AUDIT_EVENTS) + len(
                V5_AUDIT_ROWS)  # 022, then 026, then 031.
            assert (result["event_count_before"], result["event_count"]) == (
                count_before, count_before + appended)
            assert result["broker_requests"] == 0
            # The exact audit-head change: the V3 row's event chained to the old head, then its
            # crypto terms' event chained to that, then 026's three V4 events.
            new_head = assert_migration_022_appended(owner_url, head_before,
                                                     later=V4_AUDIT_EVENTS + V5_AUDIT_ROWS)
            assert result["audit_head_after"] == new_head
            assert audit_head(root) == (new_head, count_before + appended)
            with psycopg.connect(owner_url) as conn:
                old_head = conn.execute(
                    "SELECT event_hash FROM lab.trade_events WHERE seq=%s", (count_before,)
                ).fetchone()[0]
            assert old_head == head_before  # The old head is untouched at its sequence.
            after, audited_after, halts_after, version_after = audit_state(root)
            assert version_after == schema_version(root) == 31 and after["valid"]
            assert after["event_count"] == count_before + appended
            unchanged = {t: v for t, v in audited_before.items() if t != "account_risk_policies"}
            assert {t: v for t, v in audited_after.items() if t in unchanged} == unchanged
            assert audited_after["account_risk_policies"] == (SEEDED_POLICY_ROWS + 3,) * 2
            assert audited_after["account_risk_market_terms"] == (3, 3)  # V3, V4 and V5.
            assert halts_after == halts_before
            # V1, the legacy row and V2 byte for byte; the open reservations keep their
            # policies; every V1, legacy and V2 account-risk answer is the same.
            assert kept_state() == kept
            Repository(localdb.connection_url(root)).check_role()
            RiskRepository(risk_url).check_role()
            with pytest.raises(RuntimeError, match="^OWNER_LEDGER_PROTECTED$"):
                localdb.start(root)
            with pytest.raises(SystemExit, match="LEDGER_VERSION_MISMATCH"):
                main()  # A second run of the same step is refused.
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
