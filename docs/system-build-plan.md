# Trading System — Build Plan v3 (frozen for implementation)

## Separation of concerns

- **Muse = researcher + hypothesis generator.** Sends structured candidates. Permissions: `candidate:create`, `candidate:read`, `analytics:read`. There are NO endpoints like `/execute`, `/place-order`, or `/override` — Muse cannot touch orders, ever.
- **Rules engine (this system) = risk manager.** Validates, watches, confirms triggers, sizes, executes.
- **Alpaca Paper = execution venue.**
- **Database = source of truth.** Tamper-evident, append-only (enforced — see Audit integrity).

## Strategy versioning

- Current strategy: **CATALYST_RETEST_V1** (the v3 rulebook, frozen 2026-09-17).
- Every candidate, order, fill, and trade is tagged `strategy_version`. New strategy version → new tag; old results stay attributed to the version that produced them.

## Candidate state machine (frozen)

```
RECEIVED
    ↓
VALIDATING
    ├── REJECTED
    ↓
VALIDATED
    ↓
WATCHING
    ├── INVALIDATED
    ├── EXPIRED_UNTRIGGERED
    ↓
TRIGGER_CONFIRMED
    ↓
RISK_CHECK
    ├── RISK_REJECTED
    ↓
ORDER_SUBMITTED
    ├── BROKER_REJECTED
    ├── CANCELED
    ↓
PARTIALLY_FILLED
    ↓
FILLED
    ↓
OPEN
    ↓
TARGET_EXIT / STOP_EXIT / TIME_EXIT / EMERGENCY_EXIT
    ↓
CLOSED
```

Key correction from v2: **VALIDATED does not mean ordered.** Validation only means *eligible to watch*. The system watches the candidate and submits the bracket **only after the actual CATALYST_RETEST_V1 trigger confirms** (the retest price action prints). A resting limit order placed at validation time could fill hours later when the setup has deteriorated — that path is prohibited.

## What Muse sends (per candidate)

```json
{
  "strategy_version": "CATALYST_RETEST_V1",
  "signal_id": "unique-per-day-per-ticker, e.g. 2026-09-18-INTC-01",
  "market": "US",
  "ticker": "INTC",
  "entry_trigger": 157.35,
  "max_entry_price": 157.55,
  "stop": 155.95,
  "target": 160.30,
  "catalyst": "EARNINGS | ...",
  "thesis": "one line",
  "disproof": "one line"
}
```

- `max_entry_price`: if the trigger confirms but price has already jumped past this level, **skip it** — don't turn a correct thesis into a terrible entry. (Guideline: ~0.15% above trigger for liquid large caps; Muse sets it per candidate.)
- No `size_shares` — the system computes size. No endpoint for Muse to override anything.

## Validation rules (hard checks, all must pass)

1. Complete, sane levels: stop < entry_trigger < target (longs); minimum 1:2 reward-to-risk from trigger
2. US equity, tradable on Alpaca
3. `signal_id` unique — **one attempt per ticker per day** (duplicate signal IDs rejected)
4. Candidate has an expiration time (end of regular session if not specified)
5. **Session rule:** candidates may be *received* premarket; *order submission* only during regular session
6. Market open per **exchange calendar** (Alpaca calendar API — handles early closes and holidays)
7. Quote freshness: quote timestamp within tolerance
8. Maximum spread (bps) at validation
9. Minimum dollar volume (average daily)
10. Data-feed health: feed up and current, or reject with `DATA_FEED_FAILURE`
11. Broker reconciliation completed this session; **no unexplained broker positions** (see Startup reconciliation)
12. Correlation: no second open position in the same theme/sector — one theme, one shared risk budget
13. Exposure caps and daily halt (see Risk, below)

Any failure → `REJECTED` with the specific failed rule, stored immutably.

## Watching → trigger → order

- `WATCHING`: system observes live quotes for the candidate during the regular session.
- `TRIGGER_CONFIRMED`: the CATALYST_RETEST_V1 trigger prints (first retest of the level) **at or below `max_entry_price`** → else `INVALIDATED` (reason: `PRICE_BEYOND_MAX_ENTRY`).
- `RISK_CHECK`: final risk validation against *current* equity and exposure (see Risk).
- `ORDER_SUBMITTED`: bracket order (entry limit + stop-loss + take-profit legs, DAY TIF) to Alpaca Paper.
- **V1: whole shares only.** Fractional+bracket combinations must be acceptance-tested before ever enabling fractionals.

## Risk (percentage-based, frozen)

Percentages of equity — never hardcoded dollars:

- **Risk per trade:** 1.00% × current equity (=$50 on $5,000)
- **Max open planned risk:** 2.00% × equity (=$100 on $5,000)
- **Daily halt:** −3.00% × start-of-day equity (=−$150 on $5,000)
- Position size: `shares = (1.00% × equity) / (entry_trigger − stop)`, whole shares, rounded down
- Optional upper dollar cap configurable (not required for V1)

**Daily halt uses realized + unrealized.** Halt trips when `realized_PnL_today + open_unrealized_PnL ≤ −3% × start_of_day_equity`. On trip: reject new candidates, cancel unfilled working orders, keep protective exits in place, log `DAILY_RISK_HALT` system event. No automatic panic liquidation of open positions (that would be a separate, explicit rule).

## Broker monitoring

- **Primary:** Alpaca `trade_updates` websocket — fills, partial fills, cancellations, rejections. Every event persisted immediately.
- **Reconciliation:** REST poll every **30–60 seconds** comparing broker state vs local state; mismatches logged as `BROKER_MISMATCH` system events and surfaced.
- **Startup reconciliation (mandatory, every restart):**
  1. Query Alpaca open positions
  2. Query Alpaca open orders
  3. Compare against database
  4. Reconcile differences, record a `STARTUP_RECONCILIATION` event
  5. Only then permit new trading
  6. If the broker holds a position the database doesn't know about → `RISK_HALT` until manually reconciled

## Time exits

- **Never hardcoded.** `flatten_time = official_close − 5 minutes`, from the Alpaca exchange calendar. A 1:00pm early close → 12:55pm flatten automatically.
- At flatten time: close open US positions at market, exit reason `TIME_EXIT`.

## MFE/MAE (market data, not broker events)

- P&L and fills come from broker events. **MFE/MAE require price observations during the trade** — so while a position is open, the system stores market snapshots (1-minute bars; 1-second if the builder wants precision).
- Every measurement carries provenance: `data_provider`, `data_feed`, `market_data_timestamp`. (Alpaca free equity data is IEX-only, not consolidated — that limitation must be visible in analytics, not hidden.)
- `market_snapshots` table: candidate/trade reference, timestamp, price, bid, ask, spread_bps, volume, feed.

## Audit integrity (tamper-evident, enforced)

- `trade_events` is append-only **by database enforcement**, not by convention:
  - Separate DB role for the application: `INSERT` + `SELECT` only — no `UPDATE`, no `DELETE`
  - Database trigger rejecting any UPDATE/DELETE on the table
  - Each event: `event_id` (UUID), `trade_id`, `event_type`, `payload_json`, `created_at`, `previous_hash`, `event_hash` (hash-chained — tampering breaks the chain)
  - Corrections are new `CORRECTION` events referencing the original `event_id`
- Periodic exported snapshots/backups of the event log.
- Public credibility depends on this table being untouchable — now it actually is.

## Database tables

- `candidates` — everything Muse sent + strategy_version + received_at
- `validation_decisions` — candidate_id, passed/failed, failed_rule, decided_at
- `risk_decisions` — candidate_id, equity, risk_pct, risk_dollars, planned_risk, theme_exposure_before/after, computed_qty, decision, reason
- `orders` — candidate_id, alpaca_order_id, bracket legs, status (no fills here)
- `fills` — fill_id, order_id, qty, price, timestamp (every partial fill its own row)
- `market_snapshots` — price/bid/ask/spread/volume/feed observations during open trades
- `trade_events` — append-only, hash-chained (the audit log)
- `trades` — materialized from trade_events (never hand-written)
- `system_events` — websocket_disconnect, reconnect, broker_mismatch, daily_halt, startup_reconciliation, data_feed_failure, risk_halt
- `daily_stats` — nightly rollup

## Evidence quality (protect the statistics)

- Every trade carries `execution_source`: `ALPACA_PAPER` (broker-verified) vs `MUSE_MANUAL` (India/crypto results posted by Muse until their execution engines exist).
- **Headline stats default to broker-verified US results only.** Manual/simulated results are shown separately, never merged into the headline figure.
- Dashboard homepage defaults to **Verified automated strategy (US / Alpaca Paper)**. Separate tabs: **Research — Crypto**, **Research — India**. Nobody should see a combined +12R and assume it's one homogeneous automated strategy.

## Paper-fill honesty in analytics

- Alpaca paper doesn't model market impact, latency slippage, or queue position. The dashboard will eventually show `broker_paper_pnl` alongside `conservative_adjusted_pnl`.
- Schema-ready now, model later: `observed_spread`, `assumed_slippage`, `adjusted_entry`, `adjusted_exit`, `adjusted_r` fields exist from day one.

## Public dashboard

- Homepage: verified US/Alpaca Paper — equity curve (R and $), win rate, avg R, profit factor, max drawdown, per-catalyst, per-strategy-version.
- Tabs for Research — Crypto and Research — India (clearly labeled manual/simulated).
- Trade log: every trade **and** every rejected/expired candidate.
- Header everywhere: "PAPER TRADING — SIMULATED. Not real money."
- Honesty rules: everything shown, curve never resets, everything timestamped, today's picks visible only after the close.

## Safety (non-negotiable)

1. Paper endpoint `https://paper-api.alpaca.markets` hardcoded; refuse to start against anything else.
2. **No live-trading code path exists** — the live URL appears nowhere in the codebase.
3. Alpaca keys in environment variables only; never logged, never returned by any endpoint.
4. Muse's API permissions: `candidate:create`, `candidate:read`, `analytics:read` — nothing more.

## Build order

1. **Phase 1 — Lab core (no Alpaca):** schema + hash-chained event log + `POST /api/v1/candidates` + validation engine. Prove with tests that bad candidates are rejected with reasons.
2. **Phase 2 — Watch & trigger:** market-data ingestion, WATCHING state, trigger evaluation with `max_entry_price`.
3. **Phase 3 — Execution:** Alpaca Paper brackets (whole shares), trade-updates stream, 30–60s reconciliation, startup reconciliation, calendar-based time exits.
4. **Phase 4 — Risk engine:** server-side sizing, correlation limits, realized+unrealized daily halt.
5. **Phase 5 — Measurement:** R/MFE/MAE from snapshots + fills, daily rollup, stats endpoints.
6. **Phase 6 — Dashboard:** public pages, evidence-quality separation.
7. **Phase 7 — Connect Muse:** end-to-end with real daily candidates.

## Acceptance test (before calling it done)

1. `GET /health` → ok, `alpaca: paper`, strategy version.
2. POST a deliberately bad candidate (e.g., 1:1 risk-reward) → `REJECTED` with rule name; appears in the log.
3. POST a good candidate → `VALIDATED` → `WATCHING`; **no order exists at the broker yet**.
4. Simulate/await trigger → `TRIGGER_CONFIRMED` → `RISK_CHECK` → bracket appears in Alpaca **paper** dashboard.
5. Kill and restart the app mid-trade → startup reconciliation runs, no duplicate orders, `RISK_HALT` if broker state is unexplained.
6. Close the trade → `fills` rows, correct R, MFE/MAE from snapshots with feed provenance.
7. Dashboard loads publicly, shows the trade under Verified; manual results (if any) under Research tabs only.
8. Attempt an UPDATE on `trade_events` as the app role → rejected by the database.
9. Codebase contains no reference to the live trading URL.

## Milestone

Build a paper-only US-equities trading laboratory for CATALYST_RETEST_V1 that accepts structured Muse research candidates; independently validates market, strategy and risk rules; watches validated candidates for the exact first-retest trigger; independently calculates position size from current equity; submits orders only after trigger and final risk validation; receives Alpaca Paper execution updates through streaming; reconciles broker state continuously and after every restart; stores candidates, validations, orders, fills, market snapshots and system events in a tamper-evident append-only audit log; computes P&L, R, MFE and MAE without accepting Muse-calculated outcomes; and contains no executable path to live trading.

---

*Companion doc: `integration-spec.md` (exact JSON for what Muse sends and reads back).*
*History: v1 (initial) → v2 (review: candidate/trigger separation, server sizing, immutable log, versioning, US-only V1) → v3 (review: full state machine, % risk, unrealized halt, calendar exits, fast reconcile, snapshot MFE/MAE, enforced append-only, evidence quality).*

---

## Trigger definition — CATALYST_RETEST_V1 (frozen)

Supplied by the user on 2026-09-17, after the original v3 plan and integration contract.

V1 trades **longs only**.

Per candidate: `T` = entry_trigger, `M` = max_entry_price, `S` = stop, `P` = target, with S < T ≤ M < P.

**TRIGGER_CONFIRMED** when ALL of the following hold:
1. State is `WATCHING`, current time is within the regular session (exchange calendar), candidate not expired.
2. A printed trade satisfies `trade_price ≤ T` — the level has been touched.
3. At confirmation, `current_ask ≤ M` — the move hasn't run away; no chasing vertical moves.
4. Spread check: `(ask − bid) / mid_price ≤ max_spread_bps` (default 10 bps; builder-exposed config).
5. Quote freshness: newest quote timestamp no older than 5 seconds.
6. Data feed healthy (no active `DATA_FEED_FAILURE`).

Then → `RISK_CHECK` (size recomputed from current equity; exposure caps; realized+unrealized daily halt) → on pass → `ORDER_SUBMITTED` as a bracket: **entry leg = limit buy at `M`** (so we can never pay more than the max even if price jumps between trigger and submission), stop leg at `S`, take-profit leg at `P`, DAY TIF.

**INVALIDATED** while `WATCHING` when:
- A printed trade satisfies `trade_price ≤ S` before any trigger → reason `STOP_TRADED_BEFORE_TRIGGER` (setup broke down first)
- Trigger conditions met except `current_ask > M` → reason `PRICE_BEYOND_MAX_ENTRY`
- Data feed unhealthy beyond tolerance → reason `DATA_FEED_FAILURE`

**EXPIRED_UNTRIGGERED**: session close or candidate expiry arrives with no trigger.

Note on the "retest" name: the research hypothesis (fresh catalyst + level retest) is Muse's *candidate selection* logic. The *execution trigger* is the mechanical definition above. Any change to trigger mechanics = a new strategy version, never a silent edit to V1.
