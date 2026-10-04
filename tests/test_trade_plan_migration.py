"""Migration 027 (package trade-plan; owner approval 2026-10-02 "ok go ahead with migration
027"): ``lab.managed_planned_stop`` and the reservation guard reading it.

Disposable PostgreSQL clusters only (``/tmp``); no owner ledger is touched.
"""

import tempfile
from pathlib import Path

import psycopg

from catalyst_lab import localdb

MIGRATION_027 = Path(localdb.__file__).with_name("migrations") / "027_trade_plan_stop.sql"


def apply_migration_027(root):
    """Apply 027 as the owner to a disposable cluster at schema 26 and assert it appended no
    audit event (no row changes); returns the audit head."""
    owner_url = localdb.connection_url(root, "lab_owner")
    with psycopg.connect(owner_url) as conn:
        head = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 26
        conn.execute(MIGRATION_027.read_text())
    with psycopg.connect(owner_url) as conn:
        after = conn.execute(
            "SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        assert conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0] == 27
    assert after == head
    return after


def test_027_is_022s_guard_with_the_planned_stop_and_one_new_function():
    text = MIGRATION_027.read_text()
    guard_022 = (MIGRATION_027.with_name("022_crypto_size_hold.sql")).read_text()
    start = guard_022.index("CREATE OR REPLACE FUNCTION lab.guard_managed_reservation()")
    body = guard_022[start:guard_022.index("END $$;", start) + len("END $$;")]
    assert body.replace("(s.record_json->'levels'->>'stop')::numeric",
                        "lab.managed_planned_stop(s.setup_id)") in text
    assert "(s.record_json->'levels'->>'stop')::numeric) IS NOT NULL" not in text
    assert text.count("lab.managed_planned_stop(s.setup_id)") == 3
    # 026 left the guard as 022 wrote it, so 027 replaces 022's body.
    assert "guard_managed_reservation" not in MIGRATION_027.with_name(
        "026_risk_v4.sql").read_text()
    assert text.rstrip().endswith("INSERT INTO lab.schema_migrations(version) VALUES(27);")


def test_027_applies_to_a_schema_26_ledger_without_an_audit_event():
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.config import SCHEMA_VERSION
    from catalyst_lab.repository import Repository
    from tests.test_operator_controls import start_cluster_at
    from tests.test_risk_v4 import apply_migration_026

    assert SCHEMA_VERSION == 31
    with tempfile.TemporaryDirectory(prefix="catalyst-027-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            start_cluster_at(root, 25)
            apply_migration_026(root)
            apply_migration_027(root)
            # This release's schema is 31: 028, 029 and 030 add no row; 031 its four V5 rows.
            from tests.test_selection_topk_v3 import apply_migrations_after_027

            apply_migrations_after_027(root)
            Repository(localdb.connection_url(root)).check_role()
            risk = RiskRepository(localdb.connection_url(root, "catalyst_risk"))
            risk.check_role()
            with risk.connect() as conn:
                assert conn.execute("SELECT to_regprocedure('lab.managed_planned_stop(uuid)') "
                                    "IS NOT NULL AS ok").fetchone()["ok"]
                # An unknown setup has no planned stop (the guard then refuses, as before).
                assert conn.execute("SELECT lab.managed_planned_stop(gen_random_uuid()) AS s"
                                    ).fetchone()["s"] is None
        finally:
            if (root / "postgres" / "postmaster.pid").exists():
                localdb.stop(root)
