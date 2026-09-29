# Phase 1 validation — 2026-09-17

## Automated checks

`./run pytest -q`: **101 passed** using Python 3.12.7 and real PostgreSQL 14.14, in an isolated
temporary cluster. No tests use SQLite. Two upstream Starlette deprecation warnings were emitted;
there were no test failures. `./run ruff check src tests`: **passed**.

Coverage includes the integration batch shape, readback/date polling, all preliminary validation
rules, schema rejection, server-computed risk, the spec example's mislabeled reward/risk,
duplicate/ticker-day claims, concurrent submission, serialized hash chains, rollback,
corrections, app-role privilege checks, DB mutation triggers, export and offline verification.

The database rejects application-role UPDATE/DELETE/TRUNCATE, trigger disabling, table dropping,
owner-role assumption and direct trade projection writes. Tests also exercise mutation triggers
using the owner role, without disabling those triggers. The owner remains an administrative trust boundary.

## Actual loopback HTTP smoke test

The local API was started against the private persistent development database:

- `GET /health`: **200**, `alpaca: paper`, `strategy_version: CATALYST_RETEST_V1`, time present,
  `broker_connected: false`, `trading_enabled: false`.
- A dated two-candidate batch: **201**. Synthetic symbol `LABSMOKE` was rejected with
  `MIN_REWARD_RISK`; synthetic symbol `LABSAFE` was rejected with `DATA_FEED_FAILURE`.
- Date-filtered polling: **200**, both rejection records returned.
- Analytics: zero verified trades; two rejected candidates; performance metrics explicitly unimplemented.
- Audit export: **11 events**, offline verification passed against the retained head:
  `d6f5d9cbfa0c01d2ca641c4c5ff6634987517833eea282220fbeb56f059d609e`.

Export is local at `exports/20260917T211329148801Z.jsonl` (gitignored). This report retains its checkpoint,
but no off-host backup destination or export schedule has been configured.

## Evidence boundaries

Successful validation uses labeled synthetic evidence in tests. The normal server has no market/broker
provider, so otherwise valid submissions fail closed. No Alpaca keys were used, no broker orders were
submitted, and no external fills, quotes or positions were verified. Public hosting, Muse vault setup,
trigger observation, execution, final risk reservations, measurement and dashboard remain later work.
