# Crypto paper execution and protection contract

This module is a deterministic planner, not a second broker client. It imports no HTTP
library, credentials or database. The execution controller persists each exact proposal,
reserves account risk, obtains a committed five-second one-use decision, and dispatches
through the paper-only transport. It must serialize each position lifecycle. This code
does not change the frozen US `CATALYST_RETEST_V1` strategy.

## Confirmed venue mechanics

Alpaca documents crypto market, limit and stop-limit orders, GTC/IOC duration and
fractional quantities. Asset metadata supplies minimum quantity and price increments;
USD pairs are the scope of this implementation. Crypto is continuous, without margin
or short selling. A buy fee can reduce the received asset, so exit inventory comes from
the broker position and available quantity, not gross entry fills. The adapter uses
simple GTC limits for entry and native GTC stop-limits for protection. It does not
assume native crypto brackets, OCO, stop-market or trailing-stop orders.
[Official crypto order and asset documentation](https://docs.alpaca.markets/us/docs/crypto-trading).

Independent full-size stop and target orders would reserve the same inventory twice.
Therefore the target is app-managed. On a target, deadline or approved exit, the app
cancels working entry/protection, reconciles terminal cancellation or late fills, then
proposes a market sell for the remaining available inventory. A stop-limit can trigger
without filling after a gap: after the explicitly configured grace, the same
cancel/reconcile/market-exit sequence applies. Native protection alone cannot guarantee
a fill or safety during application downtime.
[Alpaca order types and reservation behavior](https://docs.alpaca.markets/us/docs/orders-at-alpaca).

The replacement API says acknowledgement does not prove replacement: the old order can
fill during the race. Its fractional quantity support is not sufficiently explicit for
this crypto path. This implementation does not use PATCH. Tightening a crypto stop uses
cancel, reconciliation and new protection for the current residual. This introduces a
transient protection gap, which must be measured during controlled paper acceptance;
fixture tests cannot establish the broker latency or remove that gap.
[Order replacement reference](https://docs.alpaca.markets/us/reference/patchorderbyorderid-1).

## Controller interface

- `CryptoAsset.from_broker(asset_json)` validates active, tradable, fractionable crypto
  metadata. Quantities round down to its increment. Off-grid prices are rejected for new
  setups at intake, admission and entry; for an already open position the recovery planner
  never raises — it rounds the broker stop-limit up to the next grid price and records it, or
  returns a HALTED `CRYPTO_STOP_UNSNAPPABLE` plan when the rounded stop would sit at or above a
  fresh bid or the target (2026-09-24, plan 3.1).
- `build_limit_entry(asset, quantity, maximum_entry, operation_key=...)` constructs an
  exact simple buy request. The caller alone computes and reserves quantity.
- `build_stop_limit(...)` and `build_market_exit(...)` produce simple sell payloads.
- `CryptoSnapshot` supplies one fresh reconciled position, available quantity, all
  lifecycle orders, ownership, quotes and durable uncertain-submission lookup state.
- `CryptoProtectionPolicy` requires snapshot freshness, quote freshness and a stop-limit
  non-fill deadline as explicit inputs. There is no implicit stock session cutoff.
- `plan_crypto_recovery(...)` returns a `RecoveryPlan` containing zero or more
  `MutationProposal` values, or a reconciliation/halt instruction. A proposal is never
  evidence of submission or a fill.

The controller must durably record the first stop breach and any target/exit decision.
Once exit is requested, keep `exit_requested=True` across later snapshots even if the
price retreats. Preserve the desired stop and its previous floor across revisions so
no cancellation or restart permits a wider stop. Feed and broker interruptions block
entries while protection/reconciliation continue independently of Jev.

Partial fills receive protection for net available inventory while the remaining entry
is canceled. A late entry fill gets only the additional uncovered protection. Pending
entry cancellation does not suppress protection for inventory already received.
Unknown POST results require lookup by the original stable client order ID plus fresh
order/position reconciliation. If still absent, the planner refuses a new ID: retry, if
the controller permits it after reconciliation, uses the original persisted request
and ID with a new exact risk decision. Repeated planning is not dispatch permission.

No exit is proposed while an existing exit is working. Cancel/fill races recompute
residual quantity before another sell. Unexpected short inventory, foreign orders or
residuals below broker minimum are explicit halts; dust is never reported as zero
exposure. Oversubscribed owned sells are canceled and reconciled before any new sell.
Fees, rejected protection, uncertain outcomes and all proposals belong in the
controller's append-only audit chain.

## Guarded HTTP and streaming evidence boundary

`managed_broker.ManagedPaperBroker` supplies the concrete HTTP integration for the
controller. It receives credentials and an `AuthorizationGate` instance from the
runtime. Its underlying `AuthorizedPaperTransport` checks the fixed destination and
claims an exact committed decision before every POST, DELETE or allowed PATCH.
PATCH accepts price fields only; market ownership and monotonic stop/target policy
remain additional controller/risk checks. An acknowledgement is retained as broker
evidence, never treated as completion of a replacement or cancellation. Timeouts and
server errors produce an unknown outcome without a transport retry.

Account readback excludes owner identifiers. Crypto asset reads percent-encode the
pair separator. `normalize_trade_update` validates authenticated stock/crypto events,
preserves provider nanoseconds, and uses the execution event's incremental quantity
and price rather than cumulative quantity or weighted average. Fill events without
an execution ID are rejected. Non-fill messages without provider event IDs use a
clearly identified local payload hash for deduplication. Payloads retain market/order
fields with an explicit list of removed account/secret fields; these sanitized
copies are not described as unmodified responses.
[Official trade-update protocol](https://docs.alpaca.markets/us/docs/websocket-streaming).

## Evidence boundary

`tests/test_crypto_execution.py` covers payload precision, partial fills, late fills,
target cancellation, stop-limit gaps, rejected protection, tightening, deadline exits,
unknown dispatches, residuals and reversal prevention. These are pure local fixtures.
The module itself cannot place an order. Real Alpaca Paper acceptance, latency and
runtime supervision require the separately authorized controller acceptance run.
