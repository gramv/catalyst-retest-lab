"""Package pass-speed (2026-09-28): the protection pass evaluates a queued print sooner.

On Railway a new TLS database connection took about 22 ms, and the pass opened 32 of them with
five watched setups, so a print waited 0.4-3.1 s in the app before evaluation. The trader's
repository now reuses connections, and a queued print wakes the pass. No trading rule changes.
"""

import threading
import time
from datetime import timedelta

import psycopg
import pytest

from catalyst_lab.repository import Repository, ReusableConnection
from tests.test_crypto_trigger import v3_setups
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_managed_execution import mx as mx  # noqa: F401
from tests.test_system_check import feed as feed  # noqa: F401
from tests.test_system_check import market as market  # noqa: F401
from tests.test_system_check import system_runtime


@pytest.fixture
def reusing(er):
    repo = Repository(er.database_url, reuse_connections=True)
    try:
        yield repo
    finally:
        repo.close_idle()


def pid(conn):
    return conn.info.backend_pid


def xid_status(er, xid):
    with er.connect() as conn:
        return conn.execute("SELECT txid_status(%s) AS s", (xid,)).fetchone()["s"]


def test_without_the_option_every_connection_is_new_and_closed(er):
    conn = er.connect()
    assert type(conn) is psycopg.Connection
    with conn:
        conn.execute("SELECT 1")
    assert conn.closed


def test_a_clean_block_commits_and_the_connection_is_reused(er, reusing):
    with reusing.connect() as conn:
        assert isinstance(conn, ReusableConnection)
        first = pid(conn)
        xid = conn.execute("SELECT txid_current() AS x").fetchone()["x"]
    assert xid_status(er, xid) == "committed"
    with reusing.connect() as again:
        assert pid(again) == first


def test_an_exception_rolls_back_propagates_and_still_reuses(er, reusing):
    with pytest.raises(RuntimeError, match="BOOM"):
        with reusing.connect() as conn:
            first = pid(conn)
            xid = conn.execute("SELECT txid_current() AS x").fetchone()["x"]
            raise RuntimeError("BOOM")
    assert xid_status(er, xid) == "aborted"
    with reusing.connect() as again:
        assert pid(again) == first


def test_a_failed_commit_closes_the_connection_and_raises(reusing):
    with pytest.raises(psycopg.OperationalError, match="COMMIT_FIXTURE"):
        with reusing.connect() as conn:
            def failing_commit():
                raise psycopg.OperationalError("COMMIT_FIXTURE")
            conn.commit = failing_commit
    assert conn.closed and reusing._idle == []


def test_nested_blocks_get_separate_connections(reusing):
    with reusing.connect() as outer, reusing.connect() as inner:
        assert pid(outer) != pid(inner)
    assert len(reusing._idle) == 2


def test_a_connection_used_without_with_is_never_handed_out_again(reusing):
    """The executor lease's pattern: it keeps the connection for its session locks."""
    with reusing.connect():
        pass
    held = reusing.connect()  # It may be the idle one; from now on it is the caller's alone.
    held.autocommit = True
    assert held.execute("SELECT pg_try_advisory_lock(42) AS ok").fetchone()["ok"]
    try:
        with reusing.connect() as other:
            assert pid(other) != pid(held)
        assert all(conn is not held for conn in reusing._idle)
    finally:
        held.close()


def test_changed_session_state_is_never_reused(reusing):
    with reusing.connect() as conn:
        conn.execute("SELECT 1")
        conn.commit()
        conn.autocommit = True
    assert conn.closed and reusing._idle == []
    with reusing.connect() as conn:
        conn.row_factory = psycopg.rows.tuple_row
    assert conn.closed and reusing._idle == []


def test_transaction_scoped_settings_do_not_reach_the_next_user(reusing):
    with reusing.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SELECT set_config('catalyst.fixture', 'kept?', true)")
    with reusing.connect() as conn:
        row = conn.execute("""SELECT current_setting('transaction_read_only') AS ro,
            current_setting('catalyst.fixture', true) AS v""").fetchone()
        assert row["ro"] == "off" and row["v"] in (None, "")


def test_idle_connections_are_capped(reusing):
    opened = [reusing.connect() for _ in range(Repository.REUSE_IDLE_MAX + 2)]
    for conn in opened:
        with conn:
            conn.execute("SELECT 1")
    assert len(reusing._idle) == Repository.REUSE_IDLE_MAX
    assert sum(conn.closed for conn in opened) == 2


def test_a_dead_idle_connection_is_replaced_before_it_is_handed_out(er, reusing):
    with reusing.connect() as conn:
        dead = pid(conn)
    with er.connect() as admin:
        assert admin.execute("SELECT pg_terminate_backend(%s) AS ok", (dead,)).fetchone()["ok"]
    reusing._idle[-1]._released -= Repository.REUSE_PING_AFTER_SECONDS + 1
    with reusing.connect() as fresh:
        assert pid(fresh) != dead
        assert fresh.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_long_idle_and_old_connections_are_closed_not_reused(reusing):
    with reusing.connect() as a, reusing.connect() as b:
        pass
    a._released -= Repository.REUSE_IDLE_SECONDS + 1
    b._opened -= Repository.REUSE_LIFETIME_SECONDS + 1
    with reusing.connect() as fresh:
        assert fresh is not a and fresh is not b
    assert a.closed and b.closed


def test_close_idle_closes_every_idle_connection(reusing):
    with reusing.connect() as a, reusing.connect() as b:
        pass
    reusing.close_idle()
    assert a.closed and b.closed and reusing._idle == []


# --- The protection pass wakes for a queued print -------------------------------------------


def trade(run, venue, symbol, price, *, seconds):
    venue.now += timedelta(seconds=30)
    assert run.reconcile_once()
    run.market_message("CRYPTO", {"T": "t", "S": symbol, "p": price, "i": int(seconds * 1000),
                                  "t": (venue.now - timedelta(seconds=seconds)).isoformat()})


def test_a_queued_print_wakes_the_pass_and_a_coalesced_one_does_not(mx, market):
    engine, venue, _ = mx
    v3_setups(mx, ["AAA/USD"])
    run = system_runtime(mx, market, ["AAA/USD"])
    trade(run, venue, "AAA/USD", "102.5", seconds=0)  # Fresh and above the trigger: coalesced.
    assert not run.print_queued.is_set()
    trade(run, venue, "AAA/USD", "99.5", seconds=0)  # At or below the trigger: queued.
    assert run.print_queued.is_set()


def test_the_protection_loop_runs_a_pass_at_once_when_woken(mx, market):
    run = system_runtime(mx, market, ["AAA/USD"])
    assert run.policy.execution_tick_seconds == 1
    calls = []
    run.execution_once = lambda: calls.append(time.monotonic())
    thread = threading.Thread(target=run._protection_loop, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        woken = time.monotonic()
        run.print_queued.set()
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2 and calls[1] - woken < 0.5  # Not the one-second sleep.
    finally:
        run.stop()
        thread.join(timeout=3)
    assert not thread.is_alive()
