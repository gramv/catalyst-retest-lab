# Phase 2 validation — 2026-09-17

## Local engineering checks

`./run pytest -q`: **152 passed** against a real, isolated PostgreSQL 14.14 cluster, in 2.60s.
Two upstream Starlette deprecation warnings remain; no test failures.
`./run ruff check src tests`: **passed**.

New coverage includes printed-trade-only touches, inclusive trigger/max-ask boundaries, correct
basis-point conversion, five-second quote freshness, stop-first precedence, wide spreads,
feed failures, delayed recovery, early-close/expiry behavior, timestamp nanosecond ordering,
late/future/pre-watch prints, durable restart recovery, observation deduplication, transaction
rollback, immutable snapshots/checkpoints, frozen per-watch policy and single-observer leases.
Adapter tests verify GET-only destinations, redirects disabled, sanitized errors and secret-safe
representations. Stream tests verify authentication and full subscription acknowledgement before
watching, along with exact message conversion. API tests distinguish connection from admission.
No test writes an order or turns fixture data into a verified trade.

The first run was temporarily blocked by an iCloud-evicted migration file. Downloading the local
source/test files resolved that file-read stall. Python bytecode now also lives outside iCloud
to avoid an observed startup stall on an evicted cache file. Tests subsequently completed successfully.

## Actual provider and running API evidence

At **2026-09-17 21:47 UTC**, the local API at `http://127.0.0.1:8765` reported:

- `/health`: HTTP 200, paper, CATALYST_RETEST_V1, WATCH_TRIGGER phase.
- Paper REST authentication: connected; account ACTIVE/USD, equity and cash $10,000.
- IEX WebSocket: authenticated; AAPL trade/quote/bar subscriptions acknowledged.
- Both observer worker threads alive; no current provider error.
- Exchange calendar: official close 16:00 ET; flatten deadline computed as 15:55 ET.
- Regular session closed; zero incoming observations/fresh quotes during this check.
- Independent GET checks: **zero open broker positions and zero open broker orders**.
- `trading_enabled: false`; admission gate `STARTUP_RECONCILIATION_REQUIRED`.

Sanitized machine-readable evidence: [phase-2-runtime-evidence.json](phase-2-runtime-evidence.json).
The observer was launched with environment-only keys for this process; keys were not written to
project files. A future launch needs secure environment provisioning again.

The persistent database's 13-event chain verified with head:
`1fb61efe65cdfb308d95b0f60d4dc55ae930ab22243d1f8ce92cffb4d70f6c91`.
No broker fills or trade projections exist. No full-session streaming/trigger observation,
order acceptance, reconciliation, P&L or MFE/MAE result is claimed. The runtime is local only;
public HTTPS deployment and Muse connection remain outstanding.

After the final-code restart at **21:52 UTC**, the API again verified REST access, stream
subscription and both worker threads, with execution disabled. The expanded 21-event chain verified:
`212cd5d7052b77cd49ca8a300579907fa4478e4768341852f2ccbd26d9f63666`.
The earlier 13-event checkpoint above remains a verified prefix; restart events were appended.
A staged-file scan found neither the supplied Alpaca credentials nor the local Muse token.
