# Phase 3 — Execution machinery, locked

**PAPER TRADING — SIMULATED. Not real money.**

This implements the requested order machinery while making every broker mutation impossible in
the shipped program. `trading_enabled` is an immutable false property. No environment variable,
API request, configuration boolean or runtime action can enable it. Introducing actual submission
requires a reviewed Phase 4 code change with atomic risk authorization and recovery behavior.

## Paper boundary and intent construction

`AlpacaPaperClient` uses the fixed paper HTTPS host for account, calendar, positions and orders,
and the fixed data host for quotes. Its HTTP transport independently permits only GET requests
to specific paths. Redirects, proxy environment settings, other hosts, ports and credential-bearing
URLs are rejected. Both market and paper trade streams use fixed allowlisted WebSocket URLs with
redirects disabled before authentication. Credentials come only from process environment variables;
representations and exception logs redact secrets. No supplied credentials are stored in this repo.

A conservative `PK` identifier allowlist rejects recognized nonpaper and unknown key formats before
transport. Prefixes are only visual hints, not cryptographic proof of environment. Successful
authentication at the fixed paper endpoint establishes the account binding. There is no way for
a mislabeled credential to change the destination.

`bracket(candidate, qty, candidate_id)` accepts an explicit positive Python integer, rejecting
booleans, strings, floats, decimals and fractions. It constructs a buy LIMIT at `max_entry_price`,
DAY TIF, stop at `stop`, take-profit at `target`, and no extended-hours execution. It never computes
quantity from equity. Frozen levels that exceed broker price precision are rejected rather than
silently rounded. Candidate-derived client IDs and unique immutable intents prevent repeated
preparation from changing the order; a conflicting quantity is rejected.

The internal preparation service requires `TRIGGER_CONFIRMED`. No public order-intent, submission,
cancellation, flatten, override or state-control route exists. Muse still cannot submit quantity.
Calling `ExecutionService.attempt()` logs `SUBMISSION_DENIED` and leaves the candidate unsubmitted.
Even a permissive injected client cannot pass the service gate. The real client and the underlying
HTTP transport also reject mutations independently.

## Broker event processing

`BrokerMonitor` authenticates the paper `trade_updates` stream, then acknowledges its subscription.
It accepts paper binary JSON frames and preserves updates arriving before the subscription ACK.
It records raw messages immediately in the global hash chain and `broker_events`, then projects
them inside a savepoint. Invalid projections roll back without losing the original broker evidence.

Exact message hashes suppress retransmissions. Execution IDs uniquely identify fills; conflicting
reuse is quarantined. Event quantity and event price produce each fill row, never the order's
cumulative filled quantity or average price. Order status uses provider nanosecond ordering, so a
late older message cannot regress a newer status. Unknown broker orders are recorded and halted,
never silently adopted. Fill-history gaps, unexpected fractions, excessive fills, changed orders,
negative positions and unsupported corrections require reconciliation/manual review. Unexpected
fractional fill facts are preserved honestly even though fractional order construction is forbidden.

The runtime never manufactures a submission receipt. Internal receipt-registration methods can
associate a previously submitted, known intent with exact broker IDs/legs. Tests explicitly seed
historical `ORDER_SUBMITTED` state in disposable databases. Entry and protective/time-exit IDs live
in `order_links`, associated with the entry order's trade ledger; fills retain their actual broker ID.

State handling includes direct full fills, partial fills, broker rejection, entry cancellation and
expiry. A partial entry canceled after receiving fills becomes `OPEN` with a protection-review halt,
not a terminal no-position state. Partial target exits leave the remaining exposure open. A trade
closes only after its recorded sell fills equal its buy fills and the entry is terminal. The exit fill
determines target, stop or time reason. Late fills after cancellation/closure restore visible exposure
and halt. Protective-leg termination while exposure remains also halts. There is no auto-correction.

## Reconciliation and startup

An independent worker compares account status, broker open positions and nested open orders with
event-derived database positions/orders every **45 seconds**. Configuration is limited to 30–60s.
Positions compare signed quantity. Orders compare identity, symbol, side, quantity, filled quantity,
type, prices and status; every difference receives a `BROKER_MISMATCH` event. The complete comparison
is retained in `reconciliation_runs`. A possibly truncated order response fails closed.

Each process gets a new run ID and starts unready. A clean current-process, current-session snapshot
is necessary before watching; it expires after 60 seconds. The trade-updates stream must also be
connected. Each restart and session rollover require reconciliation again. An update arriving during
the REST snapshot makes the comparison inconclusive and temporarily closes the gate instead of
latching a false mismatch. A real discrepancy latches `RISK_HALT` durably across restarts. A later
clean snapshot does not clear that halt. Phase 3 has no automatic repair or halt-release mechanism.

Reconciliation and the exit clock have separate threads so a slow REST call does not block the
exit scheduler. The existing single-observer database lease prevents duplicate runtime owners.
Unauthenticated feeds, failed calls and stale snapshots close admission/watching gates. Clean
reconciliation alone still leaves candidate admission at `VALIDATION_CONTEXT_INCOMPLETE`: real
liquidity/classification and Phase 4 risk evidence have not been implemented.

## Calendar exits and Phase 4 boundary

The timer uses the Alpaca calendar's exact session date and official close, subtracting five minutes.
Normal 16:00 ET sessions become 15:55; 13:00 early closes become 12:55. Missing or wrong-day calendars
do not invent a deadline. Only positive positions owned by this strategy produce market sell intents;
unexplained broker positions are never liquidated. The deadline and `TIME_EXIT_DUE` event are durable,
and repeated ticks/restarts cannot create another intent for the same candidate.

**Phase 3 fires the exit intent into the disabled submission gate. It does not send a market order,
cancel protective exits, flatten a position or pretend the trade is closed.** Tests cover both calendar
deadlines and separately simulate a known time-exit fill to verify `TIME_EXIT → CLOSED`. Real flattening
must be enabled with Phase 4's risk/exit authorization, protection management and fill-race controls.

## Persistence and readback

Migration 004 extends `orders`/`fills` and adds `order_intents`, `broker_events`, `order_links`,
`order_updates`, `reconciliation_runs` and `execution_halts`. All records reference hash-chained
events. The restricted application role has SELECT/INSERT only; UPDATE, DELETE and TRUNCATE are
denied. Database triggers independently reject row changes and truncation. Corrections must append
evidence, never rewrite history. Positions and current order states are views over this evidence.

`GET /api/v1/execution` exposes authenticated read-only ledger/reconciliation diagnostics. Candidate
readback now reports actual filled quantity and weighted entry/exit prices. P&L, R, MFE/MAE, trade
projections, daily statistics and the public dashboard remain later phases. Audit export/verification
continue to include all new events; automated off-host backups/checkpoints remain deployment work.

## Sources and evidence

- [Alpaca orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca): bracket and partial-fill semantics.
- [Alpaca trade updates](https://docs.alpaca.markets/us/docs/websocket-streaming): authentication, subscription and execution fields.
- [Alpaca key guidance](https://alpaca.markets/learn/api-key-security-best-practices-for-alpaca-builders): prefix hints and environment binding.
- [Alpaca calendar](https://docs.alpaca.markets/us/v1.1/reference/getcalendar-1): official session times.
- [Validation report](PHASE-3-VALIDATION.md): tests versus actual provider evidence, explicitly separated.
