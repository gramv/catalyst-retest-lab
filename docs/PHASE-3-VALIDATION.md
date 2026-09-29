# Phase 3 validation — 2026-09-17

**Submission remains disabled. No broker order was submitted, modified, canceled or flattened.**

## Local acceptance

`./run pytest -q`: **219 passed** against real disposable PostgreSQL databases, in 9.09 seconds.
Two upstream Starlette deprecation warnings remain. `./run ruff check src tests` and
`git diff --check`: passed. Tests deny external IPv4/IPv6 TCP connections; broker clients and
streams use explicit fake transports. No real credential is used in the suite.

| Acceptance requirement | Evidence |
| --- | --- |
| Refuse nonpaper endpoint/keys | Derived prohibited URL, redirected destinations, wrong WebSocket host, nonpaper/unknown key identifiers and private transport bypass attempts rejected before transport |
| Submission impossible while disabled | Service blocks even a permissive injected client; client methods always raise; transport refuses POST/PUT/PATCH/DELETE; forged environment/instance flags do not enable it |
| Correct bracket | Buy LIMIT at M, stop S, target P, DAY, regular session only; positive integer shares only; unsupported precision rejected, no equity sizing |
| Stream fill/state handling | Binary paper frames, auth/subscription, pre-ACK updates, partial/direct full fills, individual execution prices, retransmission/conflict handling, nanosecond ordering, rejection/cancellation reasons, late fills and known time-exit close |
| Broker-vs-DB mismatch | Each unknown/missing/changed order and position quantity discrepancy logged; no auto-repair; durable halt survives process restart and later clean snapshots |
| Startup gate | Default closed; clean current-process reconciliation plus stream connection required; restart/freshness tested; WATCHING cannot begin before clean reconciliation |
| Reconciliation cadence | 30–60s configuration bounds; 45-second worker start cadence includes elapsed request time; concurrent stream updates make comparison inconclusive |
| Calendar flatten deadline | Normal 16:00→15:55 and early 13:00→12:55; no early intent, exactly one due intent across repeated ticks; wrong/missing session rejected; attempted dispatch blocked with zero requests |
| Append-only audit | All eight execution relations reject app UPDATE/DELETE/TRUNCATE; owner TRUNCATE blocked by triggers; raw event retained on projection rollback; full-chain verification |
| No prohibited URL in tree | Source scan covers source, tests and documentation without introducing the prohibited literal |

The initial full run found an ambiguous fill-aggregation column, which was fixed before the passing
run. The active checkout is now `~/Projects/catalyst-retest-lab`, cloned with its
Git history after repeated iCloud source-file read timeouts. The original Documents copy was preserved.
The persistent lab database migrated from schema 3 to 4 and passed restricted-role verification.

## Actual Alpaca Paper evidence

Sanitized captured responses: [phase-3-runtime-evidence.json](phase-3-runtime-evidence.json).

At **22:19:57 UTC**, the local API authenticated to paper REST, authenticated/subscribed to
the paper `trade_updates` stream, connected its IEX market-data stream, and completed clean startup
reconciliation. The account reported ACTIVE/USD with $10,000 equity/cash. Broker and local orders
and positions were all empty. No fills were generated.

Four recorded reconciliation starts were 22:19:57.025522, 22:20:42.030876, 22:21:27.031542 and
22:22:12.041926 UTC: intervals **45.005354, 45.000666 and 45.010384 seconds**. Each was clean.

After a controlled process restart, at **22:23:09 UTC** a distinct process run ID had completed a
new `STARTUP_RECONCILIATION`, and both streams were connected again. The database still contained
zero broker orders/fills/positions. No duplicate submission could occur because the HTTP mutation
path does not exist. This checks an actual empty-account restart; mid-trade behavior is covered
with synthetic broker history in disposable databases, not an actual open Alpaca trade.

The running API reports `phase: EXECUTION_LOCKED`, `trading_enabled: false`, healthy monitor workers,
clean reconciliation and `watch_permitted: true`. This permission still depends on market feed health
and the other watcher rules. Production candidate admission remains `VALIDATION_CONTEXT_INCOMPLETE`,
because liquidity/classification and final risk evidence are not yet implemented. The regular session
was closed during these checks; this is not full-session market-data observation evidence.

The real calendar returned official close **16:00 ET** and computed flatten time **15:55 ET**.
Early-close scheduling was tested from an Alpaca-format calendar fixture. No actual position was
flattened; Phase 3 intentionally emits only a due intent and a submission-denied event.

## Audit checkpoint and remaining boundary

A 36-event export was independently verified at 22:23:26 UTC, with head:

`411bd0e7f683598a4ebaf130da2cf62468b08538bcd3cde3b59df9026205627f`

The ignored local export is `exports/20260917T222326236122Z.jsonl`; its manifest is adjacent.
The running monitor continues appending, so this is a verified prefix, not a fixed final event count.
Automated off-host export retention is not configured.

Phase 4 must provide atomic current-equity sizing/reservations, working-order exposure, correlation
budgets, realized-plus-unrealized daily halt, protective-exit-safe cancellations and final authorization.
It must resolve planned risk at an M-priced entry versus the frozen T-minus-S sizing formula before
enabling broker writes. Fill-race/uncertain-submission recovery and actual bracket acceptance also
remain necessary. P&L/R/MFE/MAE and the public dashboard remain Phases 5–6.

The local service listens at `http://127.0.0.1:8765`. It is not publicly deployed or connected to Muse.
Credentials were provided only through the process environment. Restarting later requires secure
environment provisioning again; no persistent credential loader was added to the repository.
