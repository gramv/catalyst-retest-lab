# Phase 4 — risk-authorized Alpaca Paper execution

**PAPER TRADING — SIMULATED. Not real money.**

Implemented locally. Actual controlled Alpaca Paper acceptance is **not complete**. The user's three
final rulings are implemented: five-second TTL, a reconciled previous-close baseline, and an excluded
TEST- engineering candidate. No performance analytics or dashboard work is included.

## Risk and atomicity

The Phase 4 ruling takes precedence over the earlier sizing and daily-halt paragraphs; see
[CONTRACT-RESOLUTIONS.md](CONTRACT-RESOLUTIONS.md). Sizing uses live Alpaca equity:

`risk_qty = floor((0.01 × equity) / (M − S))`

Whole-share quantity is capped by equity/M and available unborrowed cash/M. Available cash subtracts
unfilled local entry commitments; broker cash already reflects filled entries. A zero result produces
`ZERO_SHARE_SIZE`. Margin buying power is never treated as cash. No configured equity constant exists.

One PostgreSQL transaction and advisory lock commit RISK_CHECK, the decision, full 1% reservation,
immutable bracket intent and ORDER_SUBMITTED. Database triggers independently validate sizing,
request levels, correlation and the 2% combined reservation ceiling. Reservations include working
orders and filled exposure. Partial cancellation cannot release an existing position's risk.

The HTTP/Muse role cannot insert risk decisions or reservations. A separate `catalyst_risk` role can
append them, but cannot UPDATE/DELETE/TRUNCATE. Every new ledger row references a hash-chained event.
The ORDER_SUBMITTED database guard requires a valid decision and reservation from that same transaction.

PostgreSQL and the broker cannot share a distributed atomic commit. Before network I/O, the transport
commits a one-use claim bound to the exact decision, method, path and JSON body. The fixed paper host,
PK credential format, regular session for POSTs, expiration, reservation and halt state are checked at
dispatch. Redirects, proxy inheritance, automatic POST retries and arbitrary routes are disabled.
No boolean bypass exists, and the original Phase 3 client remains unconditionally GET-only.

## Classification and configuration

Sector/theme mappings are immutable operator imports, never Muse fields. Updates append another
mapping with provenance; existing reservations retain the classification under which they were approved.
Default limits are one active entry/position per sector AND theme; builder settings expose both limits.

An operator JSON file contains rows with exactly `ticker`, `sector`, `theme`, `source`:

```json
[{"ticker":"AAPL","sector":"Technology","theme":"Devices","source":"operator-reviewed mapping"}]
```

The example is a format illustration, not an imported production mapping. Import with:

```sh
./run catalyst-lab risk-import --file /absolute/path/to/operator-mapping.json
```

The command uses `RISK_DATABASE_URL`, or the private local risk role, validates the entire file first
and appends all mappings in one transaction. It makes no broker requests and has no Muse HTTP route.

The runtime requires `RISK_DATABASE_URL` plus market/broker observation. An unset risk URL preserves
read-only mode. `RISK_AUTHORIZATION_TTL_SECONDS=5` and `RISK_BASELINE_SOURCE=BROKER_PREVIOUS_CLOSE`
are frozen defaults; conflicting settings fail startup. Correlation settings are
`RISK_MAX_PER_SECTOR` and `RISK_MAX_PER_THEME`, both default 1. See `.env.example` for variable names.
Since migration 016 (2026-09-24) both must equal the archived `CATALYST_RETEST_V1` row in
`lab.account_risk_policies` (1 and 1); any other value fails startup (CONTRACT-RESOLUTIONS.md).
The app and risk roles must point to the same database; production must provision separate credentials.

Startup/current-session reconciliation captures Alpaca `last_equity` once, with an audit reference
to the reconciliation. Repeated polls and restarts preserve the existing baseline. RISK_CHECK refuses
a missing baseline rather than calculating one mid-session. PostgreSQL stamps each decision with
an exact five-second lifetime; the final HTTP gate also rechecks the supporting quote's freshness.

## Daily halt and exits

The risk monitor checks account-wide trading loss, including unrealized exposure, every five seconds
and again at RISK_CHECK. It uses live equity minus the durable baseline and external cash transfers;
broker unrealized P&L separates realized and unrealized components for the halt audit. Unresolved
capital journals/transfers fail closed. No R/MFE/MAE or public performance metrics are calculated.

At loss ≤3% of the saved baseline, the session halt commits before exits. It cancels working orders
and flattens all open paper positions, as explicitly requested in Phase 4. Exit requests survive restart.
New entry authorizations are blocked for the rest of that session. A new calendar session uses a new
baseline, but still needs clean reconciliation and no unresolved exposure or pending exits.

Flattening first cancels entry/protective orders, confirms they are no longer working, then reads the
current broker quantity and authorizes one market close. It does not reuse the pre-cancel quantity.
All cancellation and closing requests have immutable risk decisions. Unknown closes are looked up
before another attempt; only a known terminal close permits a new residual-quantity close.

Missing or rejected stop/target protection invokes that same flatten workflow. Partially filled
entries with held children are treated as unprotected: Alpaca activates bracket children only after
the entry is fully filled. Protective cancellation races are tracked until both the broker and fill
ledger are flat. [Alpaca order documentation](https://docs.alpaca.markets/us/docs/orders-at-alpaca).

Calendar exits use official close minus five minutes, including early closes. POSTs remain restricted
to the regular session. A venue outage, unknown submission or missing execution evidence can prevent
immediate flattening; the durable exit remains pending and new entries are blocked. The code cannot
guarantee a fill while the broker is unavailable, and does not claim zero time without protection.

## Unknown submissions and restart

Each bracket has a deterministic client order ID. A timeout or uncertain response retains the full
reservation and looks up that ID. A found order is attached to the original intent; no second POST is
sent. An inconclusive 404 does not prove rejection and never frees risk or authorizes a blind retry.
The same recovery runs after restart. [Alpaca client-ID lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid).

Raw trade updates are stored first and projected idempotently by execution ID. Early events waiting
for a known authorized receipt are retained and replayed once association is possible. REST snapshots
do not manufacture fill rows. Missing execution history after a disconnect stays a reconciliation
failure until evidence is resolved; no automatic fabricated-fill or halt-clearing path exists.

Reconciliation still runs every 45 seconds. In-flight authorization differences are logged and close
the readiness gate while receipt recovery runs; they do not prematurely latch an unexplained-order
halt. Once the claim is resolved, a clean comparison is required. Truly unexplained broker exposure
continues to latch the durable Phase 3 halt.

The production observer holds a database session lease, permitting one runtime owner. Multiple API
workers cannot run the observer on the same database. Keep one broker/risk runtime per account.

## Completion boundary

See [PHASE-4-VALIDATION.md](PHASE-4-VALIDATION.md) for test evidence and unrun acceptance. Real Muse
admission still requires production asset/liquidity evidence and approved quality settings. No live
trading path, public deployment, analytics, public dashboard or ongoing unattended acceptance job is
included in this phase.
