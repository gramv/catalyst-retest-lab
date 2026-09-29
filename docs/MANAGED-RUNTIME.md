# Local managed-paper worker

Actual launch evidence and current ports/database are recorded in
[REAL-MANAGED-SESSION-2026-09-19.md](REAL-MANAGED-SESSION-2026-09-19.md).
That historical run used an app discovery scanner, which the owner has now rejected.
The current implementation accepts external Muse reports; see
[MUSE-RESEARCH-BOUNDARY.md](MUSE-RESEARCH-BOUNDARY.md) for the correction and runtime boundary.

This is an opt-in executable for the separately labeled `JEV_MANAGED_PAPER_V1` engineering cohort. It does not change the frozen US `CATALYST_RETEST_V1` trigger. It does not install schema, start the existing account runtime, or prove unattended operation. Railway deployment remains deferred.

The process runs separate loops for mechanical execution/protection, separate US-stock and crypto-US market streams, broker reconciliation, the authenticated paper `trade_updates` stream, Jev research/position review, and heartbeats. A slow Jev request cannot block the protection loop. Startup and each broker-stream reconnect require clean reconciliation. Missing stream acknowledgement, an unhealthy reviewer heartbeat, expired reconciliation, or an audit/protection error blocks new entries. Protective recovery continues independently.

The research loop also runs Gate 1's synthetic health probe (`ReviewWorker.tick`'s own
`probe_due`/`probe` step, package jev-breaker), gated on the reviewer heartbeat exactly like the
standalone worker gates its own probe, through the worker's `Gate1Runtime` passed to
`ManagedRuntime` explicitly (`gate1`) rather than reached through the reviewer. Before this fix,
only a separately running `review_worker` process polling the same scope could close a tripped
breaker; the managed app never probed, so three consecutive provider failures silently and
permanently stopped every selection and position review while `last_research_tick` kept
advancing. `status()` now reports `jev_breaker` (`state`, `epoch`, `blocked_until`) and
`jev_calls_today` (`attempts` and `receipts` since local New York midnight, every credential
scope in the database; visibility only, the owner set no daily cap), each independently
degrading to `null` on its own read failure rather than failing the whole status response. The
watchdog raises `JEV_BREAKER_OPEN` while the breaker is not `CLOSED`. No schema change; see
[packages/jev-breaker.md](packages/jev-breaker.md).

The second Jev role is wired by `build_runtime_from_env()`: `PositionMonitor` receives the original thesis/disproof/source context, actual broker inventory and protection, fresh quotes, and completed one-minute bars. It uses the same pinned provider adapter but a distinct position question set and receipt-bound context, compiled to a fixed byte budget (see the position review policy below). A separate read-only data connection fetches bars so it does not interfere with the quote/trade streams. Research selection and position review run as separate concurrent tasks, so a broad selection batch cannot queue position reviews behind it. Completed-bar requests are coalesced per symbol/minute. The model response is followed by another broker/quote snapshot before the controller considers any amendment.

`ResearchCycle` inputs are durable. Muse supplies researched sources and theses to the cycle interface; this executable does not invent sources or run a second research agent. Its research loop discovers active cycles from `managed_events`, records Jev answers, publishes the bounded selected list, and lets the execution loop load those durable selected packets. A restart does not depend on an in-memory list of approvals. India is never admitted to execution.


Market-data authority is the authenticated Alpaca stream, not REST latest-trade polling. The fixed endpoints are stock IEX/SIP and crypto `us`; stock IEX remains explicitly non-consolidated. Exact quote/trade subscription acknowledgements are required per symbol. Every received trade for a WATCHING setup is recorded in the hash-chained event log, then consumed in arrival order using an immutable completion event. Later ticks cannot overwrite a prior stop touch. The bounded queue holds at most 5,000 pending prints in the explicit engineering profile. A disconnect, malformed/out-of-order market stream, overflow, or processing age beyond five seconds blocks entry and invalidates affected WATCHING setups with `DATA_FEED_FAILURE`; reconnection cannot silently resurrect them. Restart also invalidates existing WATCHING setups because the missing interval cannot be reconstructed from a latest quote. The one exception is a setup admitted under `CRYPTO_GAP_RESUME_V1` (every crypto setup of `CRYPTO_ALPACA_TRIGGER_V1` from 2026-09-27): a gap or restart holds it instead, and once the stream is back one read of Alpaca's completed one-minute bars over the unobserved window resumes it only if no trade reached its entry meanwhile (section below). Filled positions continue mechanical protection and calendar exits.

REST is retained for open-position bar context, asset metadata, and broker reconciliation. The worker does not claim to receive consolidated exchange prints when configured for IEX, and it does not invent prints during a connection gap. `market_poll_seconds` remains the explicit idle-subscription discovery interval; it does not authorize REST-driven entries. Stream delivery, provider connectivity, and continuous unattended availability still require a supervised real-provider acceptance run.

## Explicit local configuration

Supply these environment variables through the approved local launch environment. Database URLs are secrets; do not commit or print their values.

| Variable | Required content |
| --- | --- |
| `MANAGED_ENVIRONMENT` | Exactly `local_test` |
| `MANAGED_DATABASE_URL` | Restricted `catalyst_risk` connection to the prepared **isolated** database |
| `JEV_WORKER_DATABASE_URL` | Restricted `catalyst_jev` connection to the same database |
| `MANAGED_RUNTIME_POLICY_JSON` | All fields of `RuntimePolicy`; the explicit engineering profile below |
| `MANAGED_SOURCE_POLICY_JSON` | All fields of `SourcePolicy`; explicit IEX/SIP choice and request bounds |
| `MANAGED_CYCLE_POLICY_JSON` | All fields of `CyclePolicy` |
| `MANAGED_POSITION_POLICY_JSON` | All fields of `ManagedPolicy`; exact engineering values below |
| `MANAGED_POSITION_REVIEW_SECONDS` | `10` |
| `MANAGED_MANAGEMENT_REVIEWS` | Exactly `ENABLED` or `DISABLED`; required, no default, anything else stops startup. `DISABLED` (plan 0.10 staging) sends no position to the management Jev: each open position gets one `POSITION_REVIEW_SKIPPED {reason: MANAGEMENT_REVIEWS_DISABLED}` per lifecycle, no bars are fetched for it, protection and mechanical exits run unchanged, and status reports `management_review_enabled: false`. The same switch governs `CRYPTO_MAINTENANCE_V1` and `_V2` reviews (packages maintenance and answer-rules). Operator `ENGINEERING_TEST` setups are never reviewed in either setting |
| `JEV_REVIEW_POLICY_JSON` | The complete approved Gate 1 JSON from `review_config.APPROVED_GATE1`; missing/different fields fail closed |
| `JEV_CREDENTIAL_SLOT` | Non-secret logical slot identifier, e.g. `local-managed-test` |
| `JEV_WORKER_MAX_INFLIGHT` | Explicit review concurrency, e.g. `5` |
| `JEV_WORKER_POLL_SECONDS` | Explicit worker setting, e.g. `1` |
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Existing paper credentials injected into the launch process; never into research payloads |

The executable app additionally requires every input below. These are server launch settings;
the HTTP caller supplies proposed research levels, but cannot supply classifications, quantities
or policy overrides. Proposed levels remain subject to independent execution validation.

| Variable | Required content |
| --- | --- |
| `MANAGED_HTTP_PORT` | Explicit local port, integer 1024–65535; the executable binds only `127.0.0.1` |
| `MANAGED_API_TOKEN` | Private Bearer token of at least 32 characters, injected at launch; never place it in documentation, browser storage or URLs |
| `MANAGED_REPORT_MAX_SECONDS` | Required report-validity bound, 1–86400 seconds from Muse generation time; never extends Muse expiry or the existing review/entry deadlines |
| `MANAGED_CRYPTO_CLASSIFICATIONS_JSON` | Explicit crypto symbol list, `[]` to import none, or `null` to classify all broker-supported active USD pairs; metadata only, no opportunity screening |
| `MANAGED_CLASSIFICATION_POLICY` | `MANAGED_SERVER_CLASSIFICATIONS_TEST_V1` (every crypto symbol shares one theme), `MANAGED_SERVER_CLASSIFICATIONS_V2` (owner crypto buckets) or `ALPACA_CRYPTO_SECTORS_V1` (built-in crypto sectors, the deploy example's value since 2026-09-27; below) |
| `MANAGED_CRYPTO_BUCKETS_JSON` | Required with the V2 classification policy, optional with `ALPACA_CRYPTO_SECTORS_V1` (the owner's buckets then override the built-in sectors: the import is exactly the V2 bucket import), refused with any other policy: `{"buckets": {"L1": ["BTC/USD", …], …}, "unlisted": "REFUSE"}`; `"unlisted": "CRYPTO_OTHER"` is the owner's explicit default bucket |
| `MANAGED_US_CLASSIFICATIONS_JSON` | Explicit array of `{ticker, sector, theme}` rows for execution eligibility; `[]` for crypto-only execution |
| `MANAGED_RISK_POLICY_ID` | A `MANAGED` row of `lab.account_risk_policies` for new admissions, e.g. `JEV_MANAGED_RISK_V3` (the deploy example's value since 2026-09-27, migration 022) or `JEV_MANAGED_RISK_V2`; an unknown id or a `FROZEN_V1` row fails startup |
| `MANAGED_SELECTION_RULE` | Optional. Absent or empty = `MUSE_JEV_RESEARCH_SELECTION_V2` (the code default); `MUSE_JEV_RESEARCH_SELECTION_B1_V1` or `MUSE_JEV_RESEARCH_SELECTION_B2_V1` is an owner activation (CONTRACT-RESOLUTIONS.md). `JEV_TOP_K_SELECTION_V1` or `JEV_TOP_K_SELECTION_V2` (K from `MANAGED_TOPK_SELECTION_JSON`, no floor); V2 (the 48-hour news question and the 0.70 veto, migration 023) is the deploy example's value from 2026-09-27, after `artifacts/real-jev-topk-2026-09-27/` and `artifacts/news-stale-check-2026-09-27/`; a private config takes it only when the owner copies it. Exact values only; anything else stops startup |
| `MANAGED_SELECTION_QUALITY_FLOOR` | Required with B1 or B2: `WEAK`, `ADEQUATE` or `STRONG`; must be absent or empty with V2 |
| `MANAGED_CRYPTO_WINDOW_JSON` | Optional on a Mac, required on Railway (package review-window). Exactly `{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": N}`, N an integer 60–1440 in steps of 15; the deploy example sets 240. Present: new report-V3 crypto admissions record `CRYPTO_WINDOW_REVIEW_V1` (maintained arm) or `CRYPTO_WINDOW_HOLD_V1` (control arm) and `holding_window_seconds`; absent: `CRYPTO_24H_REVIEW_V2` / `CRYPTO_24H_HOLD_V1` exactly as before. Anything else stops startup. Each trade keeps the window it recorded (CONTRACT-RESOLUTIONS.md, 2026-09-28) |

Broker metadata import provides eligibility/classification only. It never ranks symbols,
fetches technical/news research, or creates candidate reports. Under the V1 classification
policy every configured crypto symbol shares the `CRYPTO` sector and `CRYPTO_SHARED` theme. Under
the V2 policy each owner bucket is a theme (sector `CRYPTO`); the owner's buckets explicitly
supersede earlier crypto themes, and an unlisted symbol is refused at admission with
`CORRELATION_UNKNOWN` (an earlier shared-theme row gets a `CRYPTO_UNLISTED` marker) unless the
owner chose `CRYPTO_OTHER`. Under `ALPACA_CRYPTO_SECTORS_V1` (package crypto-size-hold) each
crypto symbol classified (the configured or discovered pairs, and every crypto symbol classified
before) takes its built-in sector as its theme (sector `CRYPTO`), and a coin the list does not
name takes `CRYPTO_OTHER`, so no pick of a classified pair waits on `CORRELATION_UNKNOWN` (a
pair Alpaca lists after startup is classified at the next restart); the import supersedes
earlier crypto themes (`CRYPTO_SHARED`, owner buckets, the refusal marker) as the bucket import
does, and never reads a listed pair that is neither configured nor classified before. The
sectors, from public category data (docs/packages/crypto-size-hold.md): `LARGE_CAP_L1` BTC ETH
SOL; `SMART_CONTRACT_L1` ADA AVAX DOT XTZ; `PAYMENTS` BCH LTC XRP; `MEME` BONK DOGE PEPE SHIB
TRUMP WIF; `DEFI` AAVE CRV HYPE LDO SKY SUSHI UNI YFI; `ORACLE_DATA_INFRA` GRT LINK;
`AI_COMPUTE` FIL RENDER; `LAYER2_SCALING` ARB POL; `REAL_WORLD_ASSETS` ONDO; `GOLD_BACKED` PAXG;
`WEB3_APPLICATIONS` BAT. Going back from sectors or buckets to the V1 shared theme stops startup
(`EXISTING_CLASSIFICATION_CONFLICT`), as before. Stock classifications are never inferred or
accepted from Muse.
Existing conflicting stock classifications stop startup; identical imports are idempotent.
Research reports may contain an unmapped symbol, but admission refuses it before the ticker/day
attempt is spent, and execution cannot pass without the independent server-owned classification
and broker eligibility checks.

### Account-risk policies (migration 016)

Every account-risk check (both engines and every reservation trigger) asks
`lab.account_risk_failure` against a row of `lab.account_risk_policies`. Rows are immutable and
inserted only by a migration or an explicit owner step; a rule change is a new row.

| Policy | Engine | Per trade | Open risk | Market caps | Sector / theme | Leverage | Capacity | Fixed-exit arm |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `CATALYST_RETEST_V1` | FROZEN_V1 (archived) | 1% | 2% | US 2% | 1 / 1 | none | terminal | none |
| `MUSE_JEV_MANAGED_TEST_V1` | MANAGED (schema-13 numbers) | 1% | 2% | US 2%, crypto 2% | 1 / 1 (both markets) | none | terminal | 0% |
| `JEV_MANAGED_RISK_V2` | MANAGED (approved 2026-09-24) | 0.5% | 5% | US 3%, crypto 2%, forex 0 | 2 (US only) / 1 | 2× intraday for US day positions; crypto cash-only | 60 s cooldown, non-terminal | 30% |
| `JEV_MANAGED_RISK_V3` | MANAGED (migration 022, 2026-09-27) | US: 0.5% budget; crypto: 10% equity slice, at most 0.5% planned risk | 5% | US 3%, crypto 5%, forex 0 | US 2 / 1; crypto 3 per sector (theme) | as V2; crypto cash-only | 60 s cooldown, non-terminal | 30% |

Each setup keeps the policy recorded at admission (`risk_policy_id` in its state; setups
admitted before 016 are `MUSE_JEV_MANAGED_TEST_V1`). Correlation is the stricter of the new entry's
row and the row of every open same-sector/theme reservation. A capacity rejection
(`CORRELATION_LIMIT`, `MAX_OPEN_PLANNED_RISK`, `MARKET_RISK_CAP`, `INSUFFICIENT_BUYING_POWER`)
under a row with a cooldown keeps the setup WATCHING, records `RISK_CAPACITY_DEFERRED` once per
reason and window, and skips its triggers until the cooldown ends; every other rejection is
terminal. After sizing, the entry is checked against the broker's own buying power (stocks:
`min(buying_power, multiple × equity)`; crypto: `non_marginable_buying_power`, `crypto_status`
ACTIVE); a shortfall rejects and never resizes. The arm (`FIXED_EXIT` or `JEV_MANAGED`) is fixed
at admission from `SHA-256(setup UUID) mod 100`; the position monitor never reviews a
`FIXED_EXIT` setup, whose bracket, protection and mechanical exits run unchanged. The ledger is
bound to one paper account at its first clean reconciliation (`lab.ledger_account_binding`); a
different account is refused at startup.

### Equity-slice sizing, `JEV_MANAGED_RISK_V3` (migration 022, package crypto-size-hold)

A row may carry market terms (`lab.account_risk_market_terms`, audited and immutable, written
only in the transaction that inserts its own MANAGED row, so no earlier policy ever gains
them). V3's CRYPTO terms (`EQUITY_SLICE_RISK_CAPPED_V1`): notional at most 10% of current
equity, planned risk qty × (M − S) at most the row's 0.5% of equity, three open trades per
crypto theme (sector), and stops at least 2% below the maximum entry (`STOP_DISTANCE_BELOW_MINIMUM`,
terminal, at entry; `SYSTEM_CHECK_V1` already refuses report-V3 picks at admission). The size is
the largest multiple of the coin's quantity increment within the slice, the risk cap and the
cash available to crypto: `min(equity, cash − unfilled reserved notional, broker
non-marginable buying power)`, where a reservation's quantity already bought (its recorded buy
fills) is not counted again, so ten $1,000 trades fit in $10,000 whether or not they have
filled. A capital-limited zero size is `INSUFFICIENT_BUYING_POWER` (deferred); a slice or risk cap
below one increment is `ZERO_SHARE_SIZE`. The account check then runs on the actual planned
risk, which the reservation holds as its budget (`budget = planned_risk`); the caps sum budgets,
so a V3 crypto trade counts what it risks ($20 at a 2% stop, at most $50). The decision context
adds `sizing` (method, caps, cash, increment, the slice, risk and cash quantities, notional,
planned risk, binding `NOTIONAL`, `RISK` or `CAPITAL`). `lab.slice_sizing_failure` is the SQL
check the reservation trigger asks; `account_risk.slice_size` returns the largest size it accepts
(tested for parity). US entries under V3 keep V2's fixed 0.5% budget. V1, the legacy row and V2
answer exactly as under migration 016 (tested against 016's function recreated beside the live
one), and a V2 reservation's one-per-theme rule still binds a V3 entry in its theme.

TypeSafe credentials are loaded by the existing approved `typesafe_key()` route: local ignored mode-0600 `.env`, then noninteractive Keychain fallback. The worker fails before claiming research when the credential is absent. No credential is written to a receipt, source artifact, or configuration example. This worker has no alternate brokerage destination.

The explicit local testing policy values are:

```json
{
  "profile": "MUSE_JEV_MANAGED_TEST_V1",
  "execution_tick_seconds": 1,
  "market_poll_seconds": 1,
  "research_poll_seconds": 1,
  "heartbeat_seconds": 5,
  "reconcile_seconds": 30,
  "stream_open_timeout_seconds": 8,
  "stream_read_timeout_seconds": 1,
  "reconnect_seconds": 1,
  "max_reconnect_seconds": 30,
  "shutdown_timeout_seconds": 5,
  "market_queue_capacity": 5000
}
```

Source policy:

```json
{"stock_feed":"iex","timeout_seconds":3,"batch_size":50,"page_size":10000,"max_pages":10,"lookback_padding_bars":5}
```

Cycle policy:

```json
{"selection_limit":10,"review_deadline_seconds":10,"claim_lease_seconds":15,"max_packet_age_seconds":60,"max_inflight":5}
```

Position review policy (`JEV_MANAGED_POSITION_CONTEXT_V3`, plan package 3.2). The launcher
and `managed_ops` preflight accept exactly this tuple, all nine fields; the earlier five-field
V2 profile is refused:

```json
{"quote_max_age_seconds":5,"context_max_age_seconds":60,"max_options":5,"max_bars":20,"max_news":4,"max_structural_bars":64,"max_history":4,"context_version":"JEV_MANAGED_POSITION_CONTEXT_V3","state_byte_budget":11000}
```

Under V3 the review state is compiled by `managed_dossier.py` (`MANAGED_POSITION_DOSSIER_V1`)
to at most `state_byte_budget` encoded bytes; `jev_review` still refuses anything over 12,000.
V2 could not run at production size: it embedded the runtime's 60-bar window as full bar
objects (about 257 bytes each) next to untruncated research text and news, and every real
review failed `EVIDENCE_TOO_LONG` (the production-sized fixture measures 45,119 bytes).

- Sent once: thesis and disproof (never truncated); catalyst and levels; the economic
  relationship and the proposer's technical analysis clipped to 300 characters with a hash
  marker (`kept_chars`, `total_chars`, `sha256_prefix`); the code-computed technical metrics
  from selection time; the proposer's rationale in compact form (claims with their cited
  source and bar IDs, why now, why these levels, what would change the proposer's mind;
  `agent_confidence`, why-over-peers and known risks are not sent); selection judgments as
  `{choice, top_p}`; the last `max_history` management rows `{seq, action, reason, stop,
  target}`; SQL-aggregated sampled excursions; position, quote and protection facts with
  code-computed R and tick distances, a descriptive regime label (`NEAR`, `NORMAL`, `CLEAR`,
  `UNKNOWN`) and a remaining-time bucket. R is the entry fill minus the research stop.
- Bars: one table under a single provider/feed header, one pipe-separated row per bar
  (about 45 bytes: ref, end age, OHLCV). The newest `max_bars` are refs `R01..` (stop and
  target options); the rest of the window are `S01..` structural bars (target options only).
  Nothing is sent twice.
- Options: identical to V2 for the same bars; each is `{option_id, price, bar}` in the state,
  with readable provenance and code-computed tick and R distances in the option's question
  text. Options are never truncated or removed.
- News: every distinct current and original source is an input; adverse and withdrawn items
  are selected first, then up to `max_news` current and 2 original items, each excerpt
  clipped to 600 characters. An original item already present as current evidence is sent
  once.
- Over budget, a fixed ladder applies one reduction at a time until the state fits: omit
  the technical prose, clip the economic relationship to 120 characters, clip supporting
  excerpts to 300, drop history rows (oldest first), drop supporting items, drop structural
  bars that back no option (oldest first), clip rationale texts to 160, omit the economic
  relationship, and last clip adverse excerpts to 300. Adverse or withdrawn items are never
  dropped. If the ladder cannot fit, the review is skipped and
  `POSITION_REVIEW_SKIPPED{CONTEXT_BUDGET_UNSATISFIABLE}` is written once per lifecycle
  (with the smallest attempt's manifest); protection and mechanical exits are unaffected.
- Stored with `POSITION_REVIEW_REQUEST`, inside the hashed context but never sent: the
  manifest (every input with status `INCLUDED`, `TRUNCATED`, `OMITTED`, `ABSENT` or `PARTIAL`,
  hashes, kept/total characters, reasons, the bar and option references, budget and bytes
  used) and `retained` ledger references (setup, selection, news and bar event sequences,
  selection receipt IDs, the excursion sample range, history sequences), never copies.
- `POSITION_REVIEW_BARS` now records the whole review window (up to 64 bars, not the last
  20) under a content-derived key, so every bar that can back an option is in the ledger;
  the request's `retained.market_bars` names that row.
- Stored V1/V2 contexts and judgments keep their exact question sets, so crash recovery of
  a pending V2 review still consumes the original receipt after the upgrade.

`scripts/prove_managed_jev.py --production-calls 30` is the owner-run real-provider check
for gate G4 (production-sized synthetic contexts; p50/p95, overflow and invalid-response
counts). It has not been run against the provider by the implementation.

After preparing the isolated schema and supplying the complete configuration, the foreground supervised command is:

```sh
./run python -m catalyst_lab.managed_app
```

Do not run it against the existing account database as a test shortcut. This command has **not** been launched against real Alpaca by these implementation tests. The launcher does not apply migrations. Missing configuration, wrong roles/schema, missing credentials, or database mismatch abort startup. SIGINT/SIGTERM stops worker loops; it does not assert that positions are flat. Check broker/local reconciliation and any remaining exposure explicitly before stopping a session with positions.

The FastAPI lifespan starts the composed runtime and stops it before closing every owned
connection, including the separate position-monitor bar source. The lower-level
`managed_runtime` module remains a worker building block; use `managed_app` for the complete
authenticated input/output surface and operator classification initialization.

## Operator flatten (migration 019)

`python -m catalyst_lab.managed_ops operator flatten-all --reason "…"` (the `catalyst_operator`
login) records a durable, audited `lab.operator_flatten_requests` row and prints
`broker_action: PENDING_ACCOUNT_SAFETY_TICK` with its `request_seq`; the command sends nothing to
the broker. The running app's account-safety tick (`ManagedAccountSafety.tick`, every execution
tick) reads `lab.pending_operator_flatten_requests` (requests without a `COMPLETED` completion)
and runs the daily halt's own sequence, adding no mutation path:

- It records an `OPERATOR_FLATTEN` execution halt through the existing halt writer (once while
  unreleased), so admission, triggers, entry claims and engineering enrollment all refuse with
  `RISK_HALT`.
- It invalidates WATCHING setups (`reason: OPERATOR_FLATTEN`) and sets `exit_requested:
  OPERATOR_FLATTEN` on every working managed setup that is not already exiting (a setup already
  exiting keeps its first reason). Legacy V1 exposure gets durable `OPERATOR_FLATTEN` exit
  requests, processed exactly like the daily halt's. The setup's protective controller then
  cancels open orders first and closes after they are cancelled; every cancel and every close is
  its own exact one-use five-second authorization.
- Each tick evaluates what is left: active managed setups, legacy exposure, unresolved
  authorization claims (`lab.unresolved_authorization_claims()`), the latest cancel or close of
  each setup since the request, and broker positions and open orders no ledger owns. When
  nothing is left, confirmed on a fresh broker read, every pending request gets one
  `OPERATOR_FLATTEN_EXECUTED` managed event and one `COMPLETED` row in
  `lab.operator_flatten_completions` (request, runtime, start and finish, cancels and closes
  attempted and acknowledged, residual codes, outcome).
- Otherwise the request stays pending. A refused cancel or close (`BROKER_REFUSED`) or a lost
  response (`BROKER_RESPONSE_UNKNOWN`) is recorded at once as a `PARTIAL` row; exposure only the
  operator or the broker can clear (`MANAGED_PROTECTION_HALTED`, `UNOWNED_BROKER_POSITION`,
  `UNOWNED_BROKER_ORDER`) once nothing is in flight; a failed pass as `FAILED` with its code.
  Rows are keyed per request, runtime and condition, so a lasting condition is written once, not
  once per tick. A refused close, crypto or stock, is retried by its controller under the
  refused-close backoff described below (since 2026-09-26; the flatten-only crypto re-arm is
  gone): one new state revision per refusal, then a fresh client order ID and its own
  authorization per retry. An unknown close is looked up by its client ID and never resent.
  Failures reach only the runtime's self-clearing latches (`REST_DEGRADED`,
  `ACCOUNT_SAFETY_TICK_FAILED`).
- A second request while one is pending is recorded and coalesced: the same pass serves every
  pending request, and each gets its own completion rows.
- The `OPERATOR_FLATTEN` halt keeps the reconciliation release rule: `operator release-halt`
  after a clean reconciliation recorded after the halt, never while any flatten request is
  pending (`OPERATOR_FLATTEN_PENDING`). `operator resume` releases pauses only.

`operator status` and `operator list-halts` list the requests with their latest outcome
(`list-halts` shows pending ones; `--all` and `status` also show completed ones). The runtime
status carries `operator_flatten` (`pending_count`, `oldest_pending_request_seq`,
`oldest_pending_requested_at`, `residual`), and the watchdog raises `FLATTEN_PENDING` once the
oldest pending request is older than 60 seconds. Evidence is fixture-only
(`tests/test_operator_flatten.py`); no real flatten has run.

## Unattended safety (plan phase 0, 2026-09-26)

Fixture evidence only (`tests/test_unattended_safety.py`); operator steps are in
`OPERATIONS-RUNBOOK.md` ("Restarts, halts and refused closes").

- **Lost executor lease.** Ownership loss is terminal for the process. The protection tick
  checks the lease first; when the check fails, or a claim or send inside the tick finds the
  lease gone (the lease sets `lost_code`), the runtime runs nothing more, appends
  `RUNTIME_EXECUTOR_OWNERSHIP_LOST` (`code`, `action: PROCESS_EXIT`, `exit_code: 75`,
  `detected_at`), sets its stop event so every loop ends, and calls the entry point's exit hook
  (`install_fatal_exit`). `python -m catalyst_lab.managed_app` stops its uvicorn server and
  exits 75; `python -m catalyst_lab.managed_runtime` exits 75 too; a daemon timer ends either
  with 75 after 30 s if the graceful shutdown hangs. The supervisor restarts the process
  through normal startup; the lease is never re-acquired in-process.
- **Refused closes.** A close (market sell) the broker refuses appends `EXIT_REFUSED` and one
  new state revision carrying `exit_refusals`, `exit_retry_after` and `exit_retry_of`. No
  close is authorized before `exit_retry_after`: 1 s after the first refusal, then 2, 4, 8,
  16, 32 and every 60 s. Each retry uses that revision's fresh client order ID and its own
  one-use five-second authorization. The fifth consecutive refusal of a working setup appends
  `EXIT_REFUSAL_ALARM`. An accepted close resets the streak. A close ID already used by an
  approved POST (accepted then cancelled or expired, never sent) is never re-sent: one new
  revision is recorded and the close goes out on a later tick. The stock controller records a
  revision when the exit reason changes, not on every tick. This applies to target, time,
  daily-halt, protection-rejected, `STOP_LIMIT_NOT_FILLED` and operator-flatten closes alike.
- **Status.** `GET /api/v1/lab/status` carries `execution_halts` (`available`, `count`,
  sorted `kinds`, `oldest_halt_seq`: unreleased `lab.execution_halts` rows plus today's New
  York `DAILY_RISK_HALT`) and `exit_refusal_alarms` (setup, symbol, market, `refusals`,
  `exit_requested`, `retry_after` for every working setup refused five or more times in a row).
  `entry_ready` still reflects the runtime only: halts refuse entries in the ledger and at the
  authorization gate, and the watchdog raises `EXECUTION_HALT_ACTIVE` with `HALT_<KIND>` codes,
  `EXECUTION_HALT_STATUS_UNAVAILABLE`, and `EXIT_REFUSED_REPEATEDLY` with `_CRYPTO` or
  `_US_STOCKS`.
- **Muse jobs.** The watchdog raises `MUSE_WORK_FAILED` from the provider-job table only for a
  current job (no failure marker, no later job of its lane, no run logged by its lane since it
  started) older than `muse_max_age_seconds`; failed jobs are counted in the alarm file's
  `muse_provider_jobs` instead. A failed latest run still raises it until a later run succeeds.

## Report-V3 system check and run supersession (package system-check, 2026-09-27)

Fixture evidence only (`tests/test_system_check.py`); rules in
[packages/system-check.md](packages/system-check.md) (`SYSTEM_CHECK_V1`,
`RESEARCH_RUN_SUPERSESSION_V1`). Only selections whose packet says `report_schema_version:
AGENT_RESEARCH_REPORT_V3` are affected; V1, V2, B1, B2 and operator engineering packets are
admitted exactly as before and never read a live price.

- **Where.** `ManagedExecution.admit` runs the system check after every existing admission
  check and the broker price grid, outside the shared lock, and before the setup row that spends
  the symbol slot. The runtime passes `live_quote` for V3 packets only.
- **Checks** (all permanent refusals, in this order): `STOP_DISTANCE_BELOW_MINIMUM` ((max entry −
  stop) / max entry below `MINIMUM_CRYPTO_STOP_FRACTION` 2%; levels only, no price read), then on
  the live price `PRICE_MISMATCH` (|mid − agent's current price| / agent's price above 5%),
  `STOP_ALREADY_HIT` (bid at or below the stop, or the last trade at or below it) and
  `BREAKOUT_NOT_ENABLED`. Entry type from the mid: `PULLBACK` (entry trigger more than 0.2%
  below), `IMMEDIATE` (within ±0.2%, bounds included), `BREAKOUT` (more than 0.2% above;
  refused until a backtest earns breakouts). Equality at 5% and 2% passes.
- **Live price** (`LivePriceReader`). The runtime's stream observation when its quote is at most
  5 s old by its own timestamp; otherwise one REST latest-quote read through the runtime's
  market source (`/v1beta3/crypto/us/latest/quotes`), declared a research read, fresh by its
  read time with the quote's own timestamp recorded. At most one REST read per protection tick
  and one per symbol per 5 s (a success is reused for 5 s, a failure waits 5 s). The last trade
  is the stream's, when there is one. Nothing usable is `LIVE_PRICE_UNAVAILABLE`, transient:
  audited once per runtime, selection and reason with the attempts, retried every tick, never
  declined.
- **Evidence.** A permanent refusal appends `SYSTEM_CHECK_REFUSED` once per receipt (key
  `system-check-refused:<receipt_id>`, like the price-grid refusal), and the runtime's
  `RUNTIME_ADMISSION_REFUSED` and `RESEARCH_ADMISSION_DECLINED` carry the same `system_check`
  object. An admitted V3 setup's WATCHING state records `system_check` (result `PASSED`) and
  `entry_type`.
- **Retirement.** Every protection tick, before admission and whatever the readiness, while a
  V3 setup is WATCHING or a V3 selection awaits admission, the runtime reads the newest
  `run_slot` of any published V3 selection (a tick without V3 work reads nothing more). Each
  older V3 setup still WATCHING is revoked `SUPERSEDED_BY_NEW_RESEARCH` through
  `ManagedExecution.revoke` (`watching_only`, under the shared lock, one REVOKE keyed
  `research:superseded:setup:<id>` plus its INVALIDATED revision); each older, unexpired V3
  selection not yet admitted or declined gets one `RESEARCH_ADMISSION_DECLINED` with the same
  reason. Both carry `run_slot`,
  `superseded_by_run_slot` and `supersession_rule`. Open positions, working entries and V2
  cycles are never touched, and nothing is written when nothing is superseded. Admission also
  refuses a superseded V3 pick under the lock that serializes publication, so no race admits
  one. A failed pass latches entries (trigger scope) like a failed market-gap revocation.

## Replacement of declined top-K picks (package replacement, 2026-09-27)

Fixture evidence only (`tests/test_replacement.py`); rule in
[packages/replacement.md](packages/replacement.md) (`TOPK_REPLACEMENT_V1`). Only selections of
cycles under `JEV_TOP_K_SELECTION_V1` are affected; V1, V2, B1, B2 and operator engineering
declines are written exactly as before and never replaced.

- **When.** The runtime declines a top-K selection for a permanent admission refusal other
  than `SUPERSEDED_BY_NEW_RESEARCH` (the supersession pass never replaces). For top-K
  selections only, the recorded price-grid refusals `CRYPTO_LEVEL_OFF_PRICE_GRID` and
  `CRYPTO_PRECISION_UNAVAILABLE` (final for their receipt) are permanent too
  (`TOPK_PERMANENT_ADMISSION_REFUSALS`); every other rule keeps retrying them each tick.
- **How.** `decline_selection` writes the `RESEARCH_ADMISSION_DECLINED` and calls
  `ResearchCycle.replace_declined` in the same ledger transaction, under the shared advisory
  lock that also serializes publication and admission: the cycle's next-ranked pick not yet
  selected or skipped that `publish_ranked` accepts is published with `replacement_for`, and one
  `RESEARCH_REPLACEMENT` (`PUBLISHED` or `EXHAUSTED`) records the decision. No decision once the
  cycle or the declined pick has expired or a newer `run_slot` has been published. A decline
  already recorded is kept and its reason decides, and an existing decision is returned
  unchanged, so repeated ticks and restarts add nothing.
- **Afterwards.** The replacement joins the admission queue on the next tick and crosses the
  normal path (admission SQL, the price grid, `SYSTEM_CHECK_V1`); if it is declined too it is
  replaced in turn. A cycle never has more than K live picks.
- **Faults.** A refused decision (a cycle-level code such as `RESEARCH_POLICY_CHANGED`) commits
  nothing, neither the decline nor the decision: the runtime appends `RUNTIME_REPLACEMENT_FAULT`
  once per runtime, selection and code, latches nothing, and the still-offered selection is
  refused and retried every tick (a recorded system-check refusal re-raises without a price
  read). A database error latches entries like any failed audit write.
- **Session harness.** `scripts/agent_research_session.py execute` uses the same functions, so a
  session replaces declined top-K picks as the runtime does; its admission rows report the
  replacement and `decisions.json` lists each cycle's `replacements`.

## Crypto size and 24-hour hold (package crypto-size-hold, 2026-09-27, migration 022)

Fixture evidence only (`tests/test_crypto_size_hold.py`); rules in
[packages/crypto-size-hold.md](packages/crypto-size-hold.md). Sizing and limits are in
"Equity-slice sizing" above; the classification in "Explicit local configuration".

- **Holding (`CRYPTO_24H_HOLD_V1`, `crypto_holding.py`).** A crypto setup admitted from an
  `AGENT_RESEARCH_REPORT_V3` packet (any selection rule, the scope of `SYSTEM_CHECK_V1`) records
  `holding_policy` (`{policy_id, max_hold_seconds: 86400, exit_reason: HOLD_24H_EXIT}`) in its
  WATCHING state and gets no New York day policy: `crypto_day_policy`, `crypto_entry_deadline`
  and `crypto_flat_deadline` are null even when `MANAGED_CRYPTO_DAY_POLICY_JSON` is set. It may
  enter until its own expiry (its pick's validity, the next research run; supersession retires
  it earlier), with no midnight entry cutoff. Its `hard_exit_at` is 24 hours after the first
  recorded buy fill (partial fills included; a later fill or a restart never moves it), with no
  midnight flatten. At `hard_exit_at` the exit reason is `HOLD_24H_EXIT` (state
  `exit_requested`, the protection plan, the CANCEL and EXIT decisions, the CLOSED reason): the
  stop-limit is cancelled and the position sold at market under the usual one-use
  authorizations. The later continue-or-exit review (plan 4.6.4) replaces only this module's
  decision. Stops, targets, the −3% daily halt and operator flatten close it at any time, as
  before. Every other setup keeps `CRYPTO_NY_DAY_PAPER_V1` (or no day policy) and `TIME_EXIT`.
  From package day-review (below) a new report-V3 crypto setup records this hold only in the
  control arm (`FIXED_EXIT`); the maintained arm records `CRYPTO_24H_REVIEW_V1`, and from
  package answer-rules `CRYPTO_24H_REVIEW_V2`.
- **A coin with an open trade.** For top-K selections only, `ACTIVE_SYMBOL_ALREADY_MANAGED` (a
  non-terminal managed setup of the coin exists) is a permanent refusal
  (`TOPK_PERMANENT_ADMISSION_REFUSALS`): the pick is declined at once and `TOPK_REPLACEMENT_V1`
  publishes Jev's next-ranked pick. V2, B1 and B2 selections keep waiting (retried each tick,
  audited once) until the other setup closes or the pick expires.

## Crypto trigger version `CRYPTO_ALPACA_TRIGGER_V1` (package crypto-trigger, 2026-09-27)

Fixture evidence only (`tests/test_crypto_trigger.py`); rule in
[packages/crypto-trigger.md](packages/crypto-trigger.md). Only crypto setups admitted from a
report-V3 packet (any selection rule) from now on are affected: admission records
`trigger_version: CRYPTO_ALPACA_TRIGGER_V1` in their WATCHING state. V1, V2, B1, B2, operator
engineering setups and crypto setups admitted earlier keep today's trigger exactly (a print at or
below the trigger, a quote at most 5 s old by its own timestamp, the 10 bps spread cap).

- **Rule.** Touch: a print at or below the entry trigger, or a fresh ask at or below it.
  Confirmation: a fresh ask at or below the max entry, spread at most 1% (`MAX_SPREAD_BPS` 100,
  exactly 1% passes), a healthy feed, the setup inside its window; the order stays a limit at max
  entry. A print or a fresh bid at or below the stop invalidates (`STOP_TRADED_BEFORE_TRIGGER`,
  `STOP_QUOTED_BEFORE_TRIGGER`); a touch whose ask is above the max entry invalidates
  (`PRICE_BEYOND_MAX_ENTRY`, after the spread check); a wide spread or a print touch without a
  fresh quote waits for the next touch (`CRYPTO_TRIGGER_WAIT`, once per setup, reason and minute).
- **Freshness by read time.** A quote is fresh when the runtime received it on the stream, or
  read it over REST, at most 5 s ago; its own timestamp is recorded, never bounded. The runtime
  keeps each stream quote's receipt time (not in the observation rows, so `MARKET_PRINT` and every
  other record are unchanged) and reads through the system check's `LivePriceReader` in its
  read-time mode: the stream quote if received within 5 s, else one REST latest-quote read. The
  reader's limits are shared with admission: `new_tick()` runs once at the start of every
  protection tick, one REST read per tick (admission first), one per symbol per 5 s, and the
  trigger pass offers the read to the symbol read longest ago first.
- **Quote-driven pass.** Every protection tick, after admission and the queued prints and before
  protection, each WATCHING setup of this version whose symbol's market stream is ready is
  evaluated against its freshest quote (`_evaluate_quote_triggers`, on the tick's active setups:
  no extra ledger read). A quoted stop invalidates whatever the runtime's readiness (a ledger write
  only); a touch or a wait goes to `observe_trigger` only while entries are ready and outside a
  capacity cooldown. No setup of this version: nothing is read. Nothing touching: nothing is
  written. A fault latches entries (trigger scope) and ends the pass; protection still runs.
- **Prints.** A queued print at or below the entry trigger is confirmed on the same fresh quote (a
  REST read when the stream's is stale); without one the setup waits and is never revoked for a
  missing quote (today's `QUOTE_UNAVAILABLE_AT_PRINT` revocation does not apply to this version).
  A print above the trigger needs no quote to be quiet (coalesced into `MARKET_PRINT_SUMMARY`
  under the other quiet-print conditions). The stop print path and the 5-second print processing
  deadline are unchanged.
- **Entry.** `authorize_entry` re-evaluates the version under the shared lock; a touch that aged
  out during the entry-time broker reads waits (no decision) instead of ending the setup. The
  decision context's `quote_at`, which the authorization gate requires to be at most 5 s old at
  the claim, is the quote's read time, with `quote_exchange_at`, `quote_read_at`,
  `quote_read_basis`, `quote_source` and `trigger_version` beside it. The gate is unchanged.

## Trade maintenance `CRYPTO_MAINTENANCE_V1` (package maintenance, 2026-09-27)

Fixture evidence only (`tests/test_maintenance_rules.py`, `test_trade_maintenance.py`,
`test_maintenance_runtime.py`, `test_maintenance_session.py`); rules in
[packages/maintenance.md](packages/maintenance.md). Only crypto setups admitted from a report-V3
packet from now on are affected: admission records `partial_entry_policy` in both arms (every
trade opens the same way, plan 4.6.1) and `maintenance_policy` in the `JEV_MANAGED` arm only.
The `FIXED_EXIT` control arm is never maintained; V1, V2, B1, B2, operator engineering setups
and earlier setups keep today's behaviour (today's position monitor still reviews every other
open position). From package answer-rules the maintained arm records `CRYPTO_MAINTENANCE_V2`
instead (minute cadence, context V5, V2's answer rule: see the section after the 24-hour
reviews); a setup that recorded `CRYPTO_MAINTENANCE_V1` keeps everything below.

- **Opening (`CRYPTO_PARTIAL_ENTRY_V1`).** A partial fill is protected at once; the rest of the
  entry works until 10 minutes after the first fill, or until a fresh quote leaves the entry
  zone, then is cancelled under its own authorization with the reason. A protection refused
  while it works cancels the rest first, then protects. `MAINTENANCE_OPENED` and
  `MAINTENANCE_ENTRY_COMPLETED` record the open.
- **Reviews.** `trade_maintenance.TradeMaintenance`, wired by `build_runtime_from_env` on the
  read-only position source and the worker's reviewer, runs in the research loop's position
  pass beside today's monitor (a slow Jev never blocks protection). A review at every completed
  15-minute bar and at once on a first +nR milestone, the bid within 0.5% of the target or the
  stop, agent news or a 3% Bitcoin move within 15 minutes (every maintained trade, nearest to
  its stop first); at most one a minute per trade except near the target. Context
  `JEV_MANAGED_POSITION_CONTEXT_V4` (≤ 11,000 bytes), questions
  `JEV_MANAGED_POSITION_QUESTIONS_V4`; code computes the stop and target options and checks every
  answer under the shared lock before applying it. One `MAINTENANCE_DECISION` per review.
- **Bitcoin.** While a maintained setup is active the crypto stream also subscribes BTC/USD
  (quotes and trades, acknowledged like any symbol); the runtime keeps its last 15 minutes in
  memory (emptied by a gap or restart). No Bitcoin position is needed.
- **Applying.** The research loop never calls the broker. A raised target changes the state;
  a raised stop is recorded as `stop_replace` and the protection loop replaces each resting
  stop-limit with a price-only PATCH (`AMEND`, its own one-use authorization), falling back to
  cancel-then-place if the broker refuses it; until the broker's order carries the raised stop
  a fresh bid at or below it sells at market (`STOP_CROSSED_DURING_REPLACE`).
- **Flags and alarms.** A Jev early-exit flag appends `EXIT_FLAG_RAISED` and keeps the levels;
  `status()` reports `trade_maintenance` and the watchdog raises `EXIT_FLAG_PENDING`,
  `MAINTENANCE_REVIEW_FAILING` (Jev unavailable: every level kept, the next scheduled review
  retries, the breaker probe recovers) and `MAINTENANCE_STATUS_UNAVAILABLE`.
- **Staging switch.** `MANAGED_MANAGEMENT_REVIEWS=DISABLED` sends nothing for maintained trades
  either (one `POSITION_REVIEW_SKIPPED` per lifecycle, no bars, no triggers); protection and the
  partial-entry rule run unchanged. A runtime without the maintenance component records
  `POSITION_REVIEW_SKIPPED {MAINTENANCE_NOT_CONFIGURED}` and never reviews a maintained trade
  with another version.

## 24-hour reviews and early exits `CRYPTO_24H_REVIEW_V1` (package day-review, 2026-09-27)

Fixture evidence only (`tests/test_day_review.py`, `test_early_exit.py`,
`test_day_review_rules.py`, `test_day_review_session.py`); rules in
[packages/day-review.md](packages/day-review.md), routes in
[API-CONTRACT.md](API-CONTRACT.md#24-hour-reviews-and-early-exits-package-day-review-2026-09-27).
Only crypto setups admitted from a report-V3 packet in the maintained (`JEV_MANAGED`) arm from
now on are affected: admission records `holding_policy` `CRYPTO_24H_REVIEW_V1` instead of
`CRYPTO_24H_HOLD_V1`. The 30% control arm keeps the fixed 24-hour exit; every older setup
(maintained ones admitted under `CRYPTO_24H_HOLD_V1` included: their Jev flags stay pending
until they close, as before) keeps today's behaviour. No migration. From package answer-rules
the maintained arm records `CRYPTO_24H_REVIEW_V2` (next section): the same review with V2's
answer rule and context; a setup that recorded V1 keeps V1.

- **Clock.** At the first fill the state records `day_review_at` (T = first fill + 24 h) and
  `continuations: 0`; `hard_exit_at` is the fail-safe T + 80 minutes (Jev 30 + discussion 15 +
  Jev 30 + 5 minutes' grace). No exit happens at T by itself. If no review has decided by the
  fail-safe (the component is not running), the protection loop sells at market with
  `DAY_REVIEW_DEADLINE_EXIT`; the fail-safe never renames an exit already requested. A continue
  moves T and the fail-safe 24 hours on and counts the continuation (no limit); a delayed
  earlier fill tightens both, a restart never moves them.
- **The review.** `trade_review.DayReviews`, wired by `build_runtime_from_env` on the read-only
  position source and the worker's reviewer, runs in the research loop as its own periodic task
  (`_day_review_pass`, about once a second; a Jev call never delays a maintenance review or
  protection). At T − 30 min it records `DAY_REVIEW_REQUESTED` for the agent that proposed the
  trade (else the configured research agent whose report arrived last; the app sets the
  configured agents at start); the agent pulls it from `GET /api/v1/lab/reviews` or the research
  context and answers by T. At T Jev answers `JEV_DAY_REVIEW_QUESTIONS_V1` over
  `JEV_DAY_REVIEW_CONTEXT_V1` (≤ 11,000 bytes, never the agent's name or confidence).
  Agreement decides; a disagreement opens one discussion round (the agent's reply within 15
  minutes, then Jev's final answer; still disagreeing: exit). No agent answer: Jev alone. Jev
  unable to answer within 30 minutes (each attempt ≤ 60 s, retried after a minute; breaker and
  provider failures count), or an answer code cannot use: exit. Exactly one
  `DAY_REVIEW_DECISION` per review.
- **Continue and exit.** Continue: the state gets the new T, fail-safe and count; Jev's chosen
  stop and target options pass phase 5's checks (`cm.check_change`, the answer under a minute
  old, the levels unchanged since the ask) or are refused with the trade continuing on its
  levels; a raised stop is `stop_replace` and the protection loop replaces the stop-limit by
  PATCH (else cancel-then-place) under its own one-use authorization. Exit: `exit_requested`
  `DAY_REVIEW_EXIT`; the protection loop cancels the stop-limit and sells at market, one order
  per revision (never twice).
- **Early exits (`EARLY_EXIT_AGREEMENT_V1`).** The agent's flag
  (`POST /api/v1/lab/positions/{setup_id}/exit-flag`) is put to Jev at once
  (`JEV_EARLY_EXIT_QUESTIONS_V1` over `JEV_EARLY_EXIT_CONTEXT_V1`); a maintenance Jev flag is put
  to the agent at once (`EXIT_FLAG_ASKED`, answered through `/exit-flags/{flag_id}/answer`).
  Both say exit (or each side has flagged): `EXIT_AGREED`, `exit_requested`
  `EARLY_EXIT_AGREED`, market sell. Otherwise, or no answer within 15 minutes: the trade stays
  with its levels. One `EARLY_EXIT_DECISION` per resolution.
- **Hard exits.** A stop, target, the daily halt, operator flatten or any other exit request
  discards a pending review (`DISCARDED`) and ends pending flags (`LIFECYCLE_ENDED`); a trade
  that closed is swept once after start and whenever it leaves the open set. A coin that Alpaca
  stops listing has no exit of its own yet (see the package's open items).
- **Restart.** Everything is read back from the ledger: a Jev request recorded before a crash is
  answered from its recorded receipt (never a second vote) or, once its deadline passed, counted
  as a failed attempt and retried inside the window; decisions and exits are keyed and happen
  once.
- **Status and staging switch.** `status()` reports `day_reviews` (reviews in progress and their
  phase, failing Jev attempts, pending flags by side); the watchdog raises
  `DAY_REVIEW_JEV_FAILING` and, unreadable, `DAY_REVIEW_STATUS_UNAVAILABLE`.
  `MANAGED_MANAGEMENT_REVIEWS=DISABLED` asks Jev nothing: a review still asks the agent and then
  exits at T (`JEV_REVIEWS_DISABLED`), and an agent's flag ends unanswered (the trade stays).

## Answer rules, minute maintenance and review history: `CRYPTO_MAINTENANCE_V2`, `CRYPTO_24H_REVIEW_V2` (package answer-rules, 2026-09-27)

Fixture evidence and a replay of the first real-Jev loop's stored answers
(`tests/test_answer_rules.py`, `test_answer_rules_replay.py`, `test_answer_rules_flows.py`,
`test_answer_rules_day_review.py`); rules and evidence in
[packages/answer-rules.md](packages/answer-rules.md). Admission records both V2 versions in the
maintained (`JEV_MANAGED`) arm of a report-V3 crypto setup; the control arm keeps
`CRYPTO_24H_HOLD_V1` and the partial-entry rule. Each setup keeps the versions it recorded: a
setup that recorded V1 keeps V1's cadence, contexts, questions and readers (dispatch by the
recorded policy, and by the policy record inside each Jev request's context, so a restart reads
an answer by the rule it was asked under). No migration.

- **Reading Jev's answers (V2).** The questions are independent and answered together, so no
  answer sees another. Maintenance (`MAINTENANCE_ANSWER_RULE_V2`): `action` decides; HOLD
  holds, FLAG_EARLY_EXIT raises the flag whatever the reason answer, and a raise uses an option
  answer only when it is the unique most probable offered option (KEEP, a tie, Insufficient
  evidence or an unknown ID keeps that level; a raise with no usable part is `HELD`,
  `NO_USABLE_OPTION`). An Insufficient or tied `action` still changes nothing
  (`UNCERTAIN_JUDGMENT`). The 24-hour review (`DAY_REVIEW_ANSWER_RULE_V2`): `decision` decides;
  EXIT exits whatever the option answers; CONTINUE raises only unique offered options and never
  exits over an option answer; an Insufficient or tied decision, or CONTINUE with the reason
  uniquely BROKEN, is unusable (exit, as V1). Decision bodies record `answer_rule` and
  `option_use` (per level: the answer, the option used, why not). `review_status` stays the
  transport's whole-answer flag (any tie or Insufficient answer), which V2 does not act on.
- **Every minute.** `CRYPTO_MAINTENANCE_V2` reviews a trade at every completed 1-minute bar
  (reason `BAR_1M`) and at once on V1's events, never twice inside a minute except near the
  target. That is one Jev call per maintained trade per minute: up to about 1,440 calls per
  trade per day, plus event reviews. At most one review is in flight per trade: a minute that
  completes while the trade's previous review is still being answered is skipped and recorded
  once (`MAINTENANCE_REVIEW_SKIPPED`, code `REVIEW_SKIPPED_IN_FLIGHT`), never reviewed late.
- **Bounded passes.** A maintenance pass runs its Jev calls concurrently, at most
  `CyclePolicy.max_inflight` at once (`MANAGED_CYCLE_POLICY_JSON`, the research cycle's own
  in-flight limit: 5 in the deploy example; 10 when a component is built without one). Trades
  wait for a slot nearest to their stop first, and a waiting trade's request (with its
  10-second deadline) is recorded only when its turn comes. Ten trades take at most two waves
  of 10 seconds at the example's limit.
- **What Jev reads.** Maintenance: `JEV_MANAGED_POSITION_CONTEXT_V5` (V4 plus
  `price_action.bars_1m`, the last 60 completed 1-minute bars, and `review_history`, the trade's
  last 5 maintenance reviews: minutes ago, trigger reasons, Jev's answers with their top
  probabilities and the price of a chosen option, the outcome and its code) with
  `JEV_MANAGED_POSITION_QUESTIONS_V5` (V4's texts; the trade-reason question names the two new
  sections, and the option questions say code uses an option only when the maintenance action
  raises that level). The 1-minute bars are read over REST through the same read-only position
  source (GET only; the managed stream carries trades and quotes, not bars), once per completed
  minute and coin. The 24-hour review: `JEV_DAY_REVIEW_CONTEXT_V2` (V1 plus the same
  `review_history`) with `JEV_DAY_REVIEW_QUESTIONS_V2` (V1's texts; the trade-reason evidence
  names the history as history, and the option questions say code uses an option only on
  CONTINUE). Both stay within 11,000 bytes: over budget the oldest 1-minute
  rows go first (15 stay), then V4's ladder, then the oldest history (2 stay).
- **Status.** `trade_maintenance` and `day_reviews` report `policy_id` (the version admission
  records now) and `open_trades_by_policy`. The Bitcoin shock event keeps V1's `policy_id`
  (V2 uses V1's shock rule unchanged).
- **Unchanged.** Options, apply checks, stop replacement, flags, the 24-hour clock, agreement,
  the discussion round, the early-exit agreement and every broker request's exact one-use
  five-second authorization.

## Gap resume `CRYPTO_GAP_RESUME_V1` (package gap-resume, 2026-09-27)

Fixture evidence only (`tests/test_gap_resume.py`); rule in
[packages/gap-resume.md](packages/gap-resume.md). Plan section 5: "Alpaca stream drops or the app
restarts: resume from the ledger; fetch fills missed while down; resting stop-limits stayed at
Alpaca; pending setups resume only if price did not reach the entry meanwhile." Admission records
`gap_resume_version` in the WATCHING state of every crypto setup of `CRYPTO_ALPACA_TRIGGER_V1`;
every other setup is still revoked `DATA_FEED_FAILURE` on a gap and at startup, unchanged.

- **Held, not revoked.** A market gap (`market_gap`: the stream ended, or `start()` for both
  markets) holds such a setup where today it is revoked (the next protection tick; inside
  `start()` before any loop starts): one `GAP_RESUME_PENDING`, no trigger
  evaluated for it (the quote pass skips it; its queued prints are consumed `GAP_CHECK_PENDING`,
  never aged into a revocation), so no entry can follow. The market's gap clears, as today, once
  every WATCHING setup of it is revoked or held with its record written.
- **Window.** In process: from the gap. After a restart: from the previous runtime's last
  recorded observation of the market (the `gap_resume` section of its last `RUNTIME_HEARTBEAT`,
  taken under the runtime lock: `as_of` when the market was observed, else the start of the gap
  it had open, else unknown and the window starts at admission). Never later than an earlier
  hold still open in the ledger or than the setup's earliest queued print never evaluated; never
  before admission.
- **One check.** When the setup's symbol is acknowledged on the stream again, trade updates are
  connected and this process's reconciliation is clean and fresh, and the minute in which the
  stream came back has closed plus 30 s, the protection tick reads Alpaca's completed one-minute
  bars from the window's minute to the last minute that closed 30 s ago (GET through the runtime's
  read-only market source, `AlpacaMarketSource.window_bars`; it takes the tick's one market-data
  REST read of `LivePriceReader`, so at most one check a tick), plus every print the stream
  delivered for the setup since the window's start. Lowest traded price at or below the stop:
  `STOP_TRADED_DURING_GAP` (invalidated); at or below the entry trigger:
  `ENTRY_REACHED_DURING_GAP` (revoked, no late entry); bars unavailable, incomplete or malformed:
  `DATA_FEED_FAILURE` (revoked); else `GAP_RESUMED` and the trigger evaluates it again. A gap
  that begins during the read discards the result. A setup whose window ends while held expires
  as today (`manage`).
- **Missed entry fills.** For a setup of this version, a protection tick that finds the broker
  holding its coins with no recorded buy fill (the trade-updates stream missed it: an outage or a
  restart) runs the REST fill backfill once before the fail-closed first-fill rule, so the fill is
  recorded from Alpaca's activities and protected, instead of halting
  `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE`. Every other setup keeps that rule unchanged.
- **Status and alarm.** `status()` carries `gap_resume` (held setups with their window and due
  time, each market's observation as of one instant); `RUNTIME_HEARTBEAT` records it (its
  `as_of` alone is not a change). The watchdog raises `GAP_RESUME_CHECK_OVERDUE` when a setup has
  been held more than 300 s.

## Authenticated Muse interface

The local dashboard is a static shell until a private token is entered. The token is kept
only in page memory. All `/api/v1/lab/*` routes require `Authorization: Bearer <token>`;
the health route reports service/database liveness, not an assertion that trading is ready.
Runtime status distinguishes acknowledged stock/crypto streams, broker updates, review
configuration, protection ticks and entry readiness.

`POST /api/v1/lab/research-reports` receives Muse's external shortlist directly.
It accepts `report_id`, `generated_at`, `valid_until`, and 1–30 ordered `items`.
Each item supplies market, symbol, signal ID, LONG direction, catalyst, thesis, disproof,
economic relationship, technical analysis, proposed T/M/S/P levels, and original-source
excerpts with provenance. See [the exact contract](MUSE-RESEARCH-BOUNDARY.md).
There is no discovery scan endpoint in the current application and no scanner prerequisite
for Jev. Reports can mix US stocks, crypto and India; India remains research-only.

Acknowledgments include the durable cycle ID and polling URL. Exact replay returns the
prior record; changed content under the same ID fails. Input order preserves Muse's research
priority; the existing Jev conjunction selects at most ten approved items without a forced
quota. Publication waits for each contender's recorded outcome, not network completion order.
Use `GET /api/v1/lab/cycles/{cycle_id}/outputs?after=<cursor>` or `/api/v1/lab/outputs`
for decisions and evidence tasks. `POST /api/v1/lab/cycles/{cycle_id}/evidence` appends
material evidence revisions without extending the original deadline. No client may submit
quantities, raw orders, approval flags or policy overrides.

The app stores Muse's claims as external research, never as proof that execution checks
passed. It validates source chronology and privacy, retains exact excerpts/hashes and all
receipts, then independently rechecks eligibility, session, live trigger, fresh market data
and risk before any order. Receiving a report or selecting a candidate places no order.
Historical scan records stay visible as historical diagnostics in the immutable ledger.

### Selection rule B2 (migration 020, schema 20)

With `MANAGED_SELECTION_RULE=MUSE_JEV_RESEARCH_SELECTION_B2_V1` and a floor, startup records
`RESEARCH_SELECTION_RULE_ACTIVATED` (with `question_set_version: SKEPTIC_QUESTIONS_V2`) and new
cycles are B2 cycles for their whole life. Their items are reviewed with
`SKEPTIC_QUESTIONS_V2`; five components decide (`news_stale` NO, `already_priced` LOW or
MEDIUM, `mechanism_contradicted` NO, `inference_labelled` YES, `factual_claims_supported`
SUPPORTED) and the verdict is recorded as dissent only. Every approval needs its QUALITY_V2
category at or above the floor, as under B1. An item without a selection rationale is
`RATIONALE_REQUIRED` and never sent to the provider. No B1 shadow is written for a B2 cycle
(its receipts carry no `unsupported_inference`); decisions say `shadow:
NOT_APPLICABLE_QUESTION_SET`.

B2 objections and the evidence-task requirement each one produces: `NEWS_STALE_*` and
`ALREADY_PRICED_*` → `FETCH_PRIOR_DISCLOSURES`; `MECHANISM_CONTRADICTED_*` →
`RESOLVE_CONTRADICTION`; `INFERENCE_LABELLED_*` → `LABEL_INFERENCE`;
`FACTUAL_CLAIMS_SUPPORTED_PARTIALLY_SUPPORTED`/`_UNSUPPORTED` (and `_INSUFFICIENT`/`_TIED`) →
`RECITE_FACTUAL_CLAIMS` ("every number and fact in a claim must be stated by its cited excerpt
or bar; cite additional bars or sources, or trim the claim"); `RATIONALE_REQUIRED` →
`SUPPLY_SELECTION_RATIONALE`; anything else → `RESOLVE_RESEARCH_OBJECTION`. A B2 evidence
revision may carry `selection_rationale`; re-cited or trimmed claims alone are material, so no
new source is needed to answer `RECITE_FACTUAL_CLAIMS` (API-CONTRACT.md, "Evidence task claims
and material revisions"). Admission SQL refuses a B2 packet that fails its components
(`B2_COMPONENTS_REQUIRED`) or has no rationale (`RATIONALE_REQUIRED`), both permanent refusals,
besides every B1-style binding, activation and floor check. The ledger must be at schema 20
(owner-run `ledger-migrate`); the owner's live ledger is not migrated by this change.

### Evidence for an already open position

`GET /api/v1/lab/positions/{setup_id}/news` returns the current lifecycle ID, evidence
revision, sources, immutable original thesis and hard exit. Submit new evidence using
`POST` to that same path with **only**:

```json
{
  "news_id": "<new UUID>",
  "lifecycle_id": "<current lifecycle UUID>",
  "expected_news_revision": 123,
  "sources": [
    {
      "source_id": "<original source identifier>",
      "url": "<public HTTPS source URL without query credentials>",
      "excerpt": "<exact short market excerpt>",
      "published_at": "<aware ISO timestamp>",
      "retrieved_at": "<aware ISO timestamp>",
      "content_hash": "<SHA-256 of UTF-8 excerpt>",
      "primary_source": true,
      "asset_relevant": true,
      "novelty": "NEW_FACT",
      "stance": "ADVERSE"
    }
  ]
}
```

The example is a shape, not a valid submission. Use the revision actually returned by
GET. Sources are limited to eight, each excerpt to 1,200 characters, and the request to
32 KiB; hash, aware timestamps and privacy checks happen before persistence. Allowed
novelty values are `NEW_FACT`, `PREVIOUSLY_KNOWN`, `UNVERIFIED`; stance is `SUPPORTS`,
`ADVERSE`, `NEUTRAL`, `WITHDRAWN`.

This path continues after the original research cycle expires, but only for the matching
open position lifecycle before its unchanged hard exit. It appends `POSITION_NEWS` with
a hash of the bound record. It does not edit thesis, setup, expiry, quantities, stops,
targets or execution intents. Exact retries return the existing receipt; changed content
under the same news ID, stale revisions and unchanged-excerpt rerolls are rejected.

The position monitor uses the newest audit-sequenced evidence groups, deduplicated by
excerpt hash, while keeping the original thesis separately. It retains at most eight
sources for context assembly; the V3 position policy sends at most four current and two
original items per model call, adverse and withdrawn evidence first, each excerpt clipped
to 600 characters, and records the rest in the review manifest. All sources remain in the
ledger. New material evidence changes the
context revision, so an older in-flight model response cannot amend the position.
Closed or mismatched lifecycles cannot be reopened through this endpoint.

`GET /api/v1/lab/positions/{setup_id}/measurement` returns independent fill-based
measurements and sampled excursion provenance. Closed `/api/v1/lab/results` rows contain
the same `measurement` object. Test R uses original planned risk; missing fees, missing
price observations and feed coverage limitations remain explicit. These engineering
results remain in `JEV_MANAGED_PAPER_V1`, separate from the frozen strategy baseline.

## Evidence and limits

The scanner uses Decimal arithmetic, completed bars, observed support/stop/resistance, actual quote spread and freshness, original-source news provenance, and explicit test thresholds. It records all screened assets and rejection/evidence-gap reasons. It never substitutes a farther resistance to manufacture 2R. The shortlist has a maximum of 30; it is never padded to satisfy a quota. IEX data is labeled `IEX_ONLY_NOT_CONSOLIDATED`; crypto bars/quotes use the fixed Alpaca US feed and broker-supplied precision metadata. Missing data is recorded, not interpreted as absence of market opportunities.

The source adapter is structurally GET-only and paginates historical bars. It drops unfinished bars, rejects incomplete pagination, retains provider timestamps/trade identifiers, and exposes source request hashes without headers or credentials. It does not use aggregated coin-ranking prices as executable quotes.

The focused scanner/source/runtime/hardening tests pass **82 tests** locally. They cover stock/crypto separation, price geometry, observed resistance, stale/missing evidence, early-close flatten boundaries, pagination, failed sources, exact endpoint restrictions, durable handoff discovery, stream authentication/subscription, preservation of every received queued print, queue overload and disconnect handling, pre-acknowledgement fills, out-of-order broker inventory, deferred amendment expiry, cancellation races, and independence from slow Jev work. They do not establish broker acceptance, a profitable strategy, a running supervisor, or public deployment. The complete integration suite and broker acceptance are reported separately by the main implementation report.
