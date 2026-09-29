# Package crypto-size-hold — trade size for the $10,000 account, 24-hour crypto holding, crypto sectors, skip-and-replace for an open coin (plan phase 4b)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-crypto-size-hold`, from `work/2026-09-24-product-plan` at `e9c6300`. Plan:
`docs/CRYPTO-AGENT-LOOP.md` sections 4.4, 4.6.4, 4.7 and 8 (owner-approved 2026-09-26,
decision D2) and the owner decisions relayed for this package on 2026-09-27: "It has $10k, size
it accordingly" (5–10 trades must fit in the cash at once); stops at least 2% below max entry;
hold up to 24 hours, then the agent and Jev decide whether to continue, a later phase, so until
then exit at 24 hours; no midnight close; at most 3 open trades per sector, and BTC, ETH and SOL
may all be open at once; a pick for a coin that already has an open trade is skipped and Jev's
next pick replaces it. Builds on `docs/packages/system-check.md`, `replacement.md` and
`selection-topk.md`. This file carries the text the coordinator merges into `docs/PHASES.md` and
`docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch. Migration 022.

## PHASES entry (paste as written)

### 2026-09-27 — Crypto trade size, 24-hour hold, crypto sectors and skip-and-replace for an open coin (plan phase 4b, package crypto-size-hold; schema 22) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
scripted mock Jev transport, fixture universes, and disposable `/tmp` clusters for the migration
rehearsals. No broker, provider, network or owner-ledger contact; every broker mutation keeps
its exact one-use five-second authorization. Nothing is activated on the owner's account:
`MANAGED_RISK_POLICY_ID` and `MANAGED_CLASSIFICATION_POLICY` stay owner settings (the deploy
example now names `JEV_MANAGED_RISK_V3` and `ALPACA_CRYPTO_SECTORS_V1`), and the owner ledger
needs the owner-run `ledger-migrate` to schema 22. The plan's phase-4 "supervised paper trade"
is still to do.

- **Migration `022_crypto_size_hold.sql`** (schema 22). A new audited, immutable table
  `lab.account_risk_market_terms` (a market a MANAGED policy sizes in equity slices; a row can
  only be written in the transaction that inserts its own policy row, so V1, V2 and every
  earlier row can never gain terms), the `JEV_MANAGED_RISK_V3` row and its CRYPTO terms (the
  only two rows added, each with its own audit event), `lab.slice_sizing_failure` (the SQL size
  check), `lab.account_risk_failure` replaced (a policy's per-theme limit in a market is its
  terms' `max_per_theme` there, else the row's; everything else as in 016) and
  `lab.guard_managed_reservation` replaced (016's statements byte for byte behind one slice
  branch). V1's reservation triggers, `lab.stamp_risk_decision` and every existing row are
  untouched.
- **`JEV_MANAGED_RISK_V3` sizing** (`account_risk.slice_size`, `ManagedExecution._slice_entry`).
  A crypto entry is the largest multiple of the coin's quantity increment whose notional is at
  most 10% of current equity, whose planned risk qty × (M − S) is at most 0.5% of equity, and
  which the cash available to crypto can pay: `min(equity, cash − unfilled reserved notional,
  non-marginable buying power)`; a bought quantity is not counted twice. A stop closer than 2%
  below M is refused. The one account check runs on the actual planned risk, which the
  reservation holds as its budget. Crypto open risk is capped at 5% of equity and three open
  trades per sector (theme); US numbers, the 60-second cooldown and the 30% fixed-exit arm are
  V2's. The decision context adds `sizing`; `binding_constraint` is `NOTIONAL`, `RISK` or
  `CAPITAL`.
- **`CRYPTO_24H_HOLD_V1`** (`crypto_holding.py`). A crypto setup admitted from a report-V3
  packet records `holding_policy` at admission and gets no New York day policy: no midnight
  entry cutoff (it may enter until its own expiry, the next run) and no midnight flatten. Its
  hard exit is 24 hours after the first buy fill, reason `HOLD_24H_EXIT`, isolated for the later
  continue-or-exit review. Every other setup keeps `CRYPTO_NY_DAY_PAPER_V1` and `TIME_EXIT`.
- **`ALPACA_CRYPTO_SECTORS_V1`** (`managed_classification.py`). A built-in classification policy
  covering all 33 Alpaca USD pairs in eleven public-category sectors; a coin not on the list is
  `CRYPTO_OTHER`, so no pick of a classified pair waits on `CORRELATION_UNKNOWN` for want of a
  sector (a pair Alpaca lists after startup waits for the next restart; open item). The owner's
  `MANAGED_CRYPTO_BUCKETS_JSON`, when supplied, overrides it (the import is then exactly the V2
  bucket import); `MANAGED_CRYPTO_CLASSIFICATIONS_JSON` still chooses the symbols.
- **A coin with an open trade** (`managed_runtime.TOPK_PERMANENT_ADMISSION_REFUSALS`). For top-K
  selections `ACTIVE_SYMBOL_ALREADY_MANAGED` is a permanent refusal: the pick is declined and
  `TOPK_REPLACEMENT_V1` publishes Jev's next-ranked pick. V2, B1 and B2 selections keep waiting.

Evidence: `tests/test_crypto_size_hold.py` (29 tests, 33 cases): the V3 row and terms carry the
owner's numbers and are audited, V1/V2/legacy rows gain no terms, terms refused for any existing
policy, a later transaction, a frozen row or a US market; `slice_size` at the owner's examples
(2% stop: 10 coins, $1,000, $20; 6% stop: 8.3333 coins, $833.33, $49.9998; 5% and 10% stops;
cash-limited; whole-coin and cent increments; a meme-coin price; one coin above the slice;
exactly 2% passes, 1.99% refused; invalid inputs); SQL/Python parity over 480 cases (908 SQL
checks: the SQL accepts every Python size and its cash-free maximum, and refuses one increment
more); engine entries sized, reserved (budget = planned risk) and authorized (one limit order at
M, one claim); cash caps and bought trades not counted twice; increments; a short stop refused at
entry; ten 6%-stop trades then `MAX_OPEN_PLANNED_RISK` at $499.998; ten 2%-stop trades using
exactly $10,000; three per sector with BTC, ETH and SOL together, a fourth meme coin deferred
until one closes; a V2 reservation's one-per-theme rule binding a V3 entry; the reservation guard
equal to 016's statements plus one branch; forged V3 reservations refused while V2 keeps its
fixed budget; an exhaustive exploration (5,355 checks over 764 reachable ledgers of V1, V2 and V3
reservations) in which V1, the legacy row and V2 answer exactly as migration 016's function
recreated beside the live one (3,060 checks) and V3 as an independent oracle; the hold's exact
record; a V3 position opened at 23:00 New York staying open through the NY-day flatten and
midnight, then cancelled and sold `HOLD_24H_EXIT` at exactly 24 hours while a report-V2 setup is
flattened at 23:55 with `TIME_EXIT`; entries after the midnight cutoff until the setup's expiry;
the first fill's clock kept through a later fill and a restart; all 33 pairs classified once,
`CRYPTO_OTHER`, supersession of `CRYPTO_SHARED`, no read of unlisted pairs, the owner's buckets
overriding, launch settings; a top-K pick for a coin with an open trade declined and replaced by
rank 6 (then admitted), a V2 pick still waiting; and a top-K V3 pick admitted on a live stream
quote under V3, sized (9.8039 coins, $49.99989 risk), reserved and authorized.
`tests/test_owner_migration_rehearsal.py`: a populated schema-21 ledger migrates to 22 through
the CLI with exactly two appended events (the V3 row, then its terms), each hash-chained to the
one before, the old head kept at its sequence, and every V1, legacy and V2 row, open reservation
and account-risk answer unchanged; the 13-to-current rehearsal now counts five appended rows;
the 20-to-21 rehearsal stays DDL-only and is followed by 21 to 22. Schema pins moved to 22 in
five existing modules. Full suite and ruff: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### `JEV_MANAGED_RISK_V3`, `CRYPTO_24H_HOLD_V1`, `ALPACA_CRYPTO_SECTORS_V1` and the top-K open-coin decline (2026-09-27, package crypto-size-hold)

Owner decisions of 2026-09-26 (D2: fit the $10,000 paper account, 10% slices, at most 0.5% risk
per trade, at most 5% total open crypto risk) and relayed 2026-09-27: 5–10 trades must fit in
the cash at once; stops at least 2% below max entry; hold up to 24 hours, after which the agent
and Jev decide whether to continue (a later phase; until it exists, exit at 24 hours); no
midnight close; at most 3 open trades per sector, and BTC, ETH and SOL may all be open at once;
a pick for a coin that already has an open trade is skipped and Jev's next pick replaces it.
Named versions under the owner's 2026-09-24 ruling. `CATALYST_RETEST_V1`,
`MUSE_JEV_MANAGED_TEST_V1`, `JEV_MANAGED_RISK_V2`, `CRYPTO_NY_DAY_PAPER_V1`, the classification
policies `MANAGED_SERVER_CLASSIFICATIONS_TEST_V1` and `_V2`, `SYSTEM_CHECK_V1`,
`TOPK_REPLACEMENT_V1` and every stored row keep their definitions and bytes; each setup keeps the
account-risk policy and holding policy recorded at its admission.

**`JEV_MANAGED_RISK_V3`** (a row of `lab.account_risk_policies`, migration 022): engine
`MANAGED`; `risk_pct` 0.005; `account_cap_pct` 0.05; `market_caps` US_STOCKS 0.03, CRYPTO 0.05,
FOREX 0; `max_per_sector` 2 and `max_per_theme` 1 with `sector_limited_markets` {US_STOCKS};
leverage allowed with `intraday_buying_power_multiple` 2 (US day positions only); capacity
cooldown 60 s; fixed-exit arm 30%; `owner_ruling_ref` "CRYPTO-AGENT-LOOP 2026-09-26 D2;
crypto-size-hold 2026-09-27". Its CRYPTO market terms (a row of
`lab.account_risk_market_terms`): `sizing_method` `EQUITY_SLICE_RISK_CAPPED_V1`, `notional_pct`
0.10, `max_per_theme` 3, `min_stop_fraction` 0.02, `owner_ruling_ref` "CRYPTO-AGENT-LOOP
2026-09-26 D2 and 4.4; crypto-size-hold 2026-09-27". The −3% cancel-and-flatten daily halt is
unchanged (it is not a row value). US entries under V3 are sized and limited exactly as under V2.

*Market terms.* `lab.account_risk_market_terms` (`policy_id`, `market`, `sizing_method`,
`notional_pct`, `max_per_theme`, `min_stop_fraction`, `owner_ruling_ref`, `event_seq`; primary
key policy and market) is audited like the policy rows and immutable. A row is accepted only
for market CRYPTO and only in the transaction that inserts its own `MANAGED` policy row
(`MARKET_TERMS_REQUIRE_NEW_MANAGED_POLICY` otherwise), so no existing policy ever gains terms.
Only migrations and explicit owner steps insert them.

*Crypto sizing.* M = max entry, S = stop, E = current equity from the entry's fresh account
read, inc = the coin's broker `min_trade_increment`. A stop with M − S < 0.02 × M is refused
`STOP_DISTANCE_BELOW_MINIMUM` (terminal). Otherwise qty = inc × min(⌊0.10 × E / (M × inc)⌋,
⌊0.005 × E / ((M − S) × inc)⌋, ⌊C / (M × inc)⌋) with exact decimal floor divisions, where
C = max(0, min(E, cash − U, non-marginable buying power)) and U = Σ over open reservations of
(reserved quantity − the setup's recorded buy fills, at least 0) × its max entry (a frozen V1
reservation counts in full). A quantity below the coin's `min_order_size` is
`INSUFFICIENT_BUYING_POWER` (a capacity deferral, 60 s) when the slice and risk terms alone allow
at least the minimum, else `ZERO_SHARE_SIZE` (terminal). The planned risk P = qty × (M − S); the
account check `lab.account_risk_failure('JEV_MANAGED_RISK_V3','ALPACA_PAPER','CRYPTO', sector,
theme, E, P)` runs on P, and the reservation's `budget` and `planned_risk` both equal P. Binding
constraint: `CAPITAL` when the cash term is the smallest, else `RISK` when the risk term is below
the slice term, else `NOTIONAL`. The entry decision's context adds `sizing`: `{method, equity,
notional_pct, risk_pct, min_stop_fraction, notional_cap, risk_cap, available_cash, increment,
slice_qty, risk_qty, cash_qty, qty, notional, planned_risk, binding, unfilled_reserved_notional,
min_order_size}`. Examples at E = $10,000, M = $100, inc = 0.0001: a 2% stop buys 10 coins,
$1,000 notional, $20 risk; a 6% stop buys 8.3333 coins, $833.33, $49.9998; ten 2%-stop trades
use $10,000; ten 6%-stop trades hold $499.998 of risk and an eleventh is `MAX_OPEN_PLANNED_RISK`.

*Limits.* The account check sums every open reservation's budget (a V3 crypto reservation
counts its planned risk; every other reservation its fixed budget, as before): total at most
0.05 E (`MAX_OPEN_PLANNED_RISK`), crypto at most 0.05 E (`MARKET_RISK_CAP`), forex 0. The theme
limit is the smallest of the new entry's policy limit in its market and the limit of each open
same-theme reservation's policy in that reservation's market, a policy's limit in a market being
its terms' `max_per_theme` there when it has terms, else the row's `max_per_theme` (V3 crypto 3;
V3 US, V2, V1 and the legacy row 1); sector limits are 016's. Crypto classifications are sector
`CRYPTO` with the coin's sector as theme, so the V3 limit is three open trades per crypto sector,
and an open V2 reservation's one-per-theme rule still binds a V3 entry in its theme. For every
policy without terms each answer equals migration 016's.

*SQL checks.* `lab.slice_sizing_failure(policy, market, equity, qty, M, S)` returns
`SLICE_TERMS_UNKNOWN` (no MANAGED policy with terms for the market), `INVALID_SLICE_SIZING_INPUT`,
`STOP_DISTANCE_BELOW_MINIMUM`, `SLICE_NOTIONAL_EXCEEDED` (qty × M > notional_pct × equity),
`SLICE_RISK_EXCEEDED` (qty × (M − S) > risk_pct × equity), else NULL. For a setup in a market
that its decision's policy has terms for, `lab.guard_managed_reservation` requires the
`catalyst_risk` role, an approved, unexpired ENTRY decision of the same setup in the same
transaction, a MANAGED policy, budget = planned risk, no `lab.slice_sizing_failure`, the current
classification's sector and theme, max entry = the setup's M, planned risk = qty × (M − S) and qty
= the decision payload's (else `INVALID_MANAGED_RISK_RESERVATION`), then the account check (its
code). Every other reservation runs migration 016's statements byte for byte.
`account_risk.slice_size` returns the largest size `lab.slice_sizing_failure` accepts within the
cash (tested for parity).

**`CRYPTO_24H_HOLD_V1`** (`crypto_holding.py`). Scope: a crypto setup admitted from an
`AGENT_RESEARCH_REPORT_V3` packet, under any selection rule (the scope of `SYSTEM_CHECK_V1`).
Recorded in its WATCHING state at admission as `holding_policy` = `{policy_id:
"CRYPTO_24H_HOLD_V1", max_hold_seconds: 86400, exit_reason: "HOLD_24H_EXIT"}` and read back from
that state only; `crypto_day_policy`, `crypto_entry_deadline` and `crypto_flat_deadline` are null
whatever `MANAGED_CRYPTO_DAY_POLICY_JSON` says. Entries: until the setup's own expiry (its
pick's validity, bounded by the next research run; `RESEARCH_RUN_SUPERSESSION_V1` retires it when
the next run's shortlist goes live), with no midnight entry cutoff. Hard exit: `hard_exit_at` =
the first recorded buy fill + 86,400 s of elapsed time (partial fills included; a later fill or a
restart never restarts or extends it; inventory without a recorded first fill halts
`CRYPTO_FIRST_FILL_TIME_UNAVAILABLE` and exits, as before). No midnight flatten. At
`hard_exit_at` the exit reason is `HOLD_24H_EXIT` (state `exit_requested`, protection plan, the
CANCEL and EXIT decisions, the CLOSED reason): the resting stop-limit is cancelled and the
position sold at market, each under its own one-use authorization. Stops, targets, the −3%
daily halt and operator flatten close it at any time, unchanged. The later continue-or-exit
review (plan 4.6.4) replaces this module's decision only. Every other setup keeps
`CRYPTO_NY_DAY_PAPER_V1` (or no day policy) and `TIME_EXIT`.

**`ALPACA_CRYPTO_SECTORS_V1`** (a value of `MANAGED_CLASSIFICATION_POLICY`). Built-in sectors
for every Alpaca USD crypto pair of 2026-09-26 (33, stablecoins excluded), from public category
data (the categories CoinGecko and CoinMarketCap publish for each coin):

| Sector (theme) | Coins |
| --- | --- |
| `LARGE_CAP_L1` | BTC, ETH, SOL |
| `SMART_CONTRACT_L1` | ADA, AVAX, DOT, XTZ |
| `PAYMENTS` | BCH, LTC, XRP |
| `MEME` | BONK, DOGE, PEPE, SHIB, TRUMP, WIF |
| `DEFI` | AAVE, CRV, HYPE, LDO, SKY, SUSHI, UNI, YFI |
| `ORACLE_DATA_INFRA` | GRT, LINK |
| `AI_COMPUTE` | FIL, RENDER |
| `LAYER2_SCALING` | ARB, POL |
| `REAL_WORLD_ASSETS` | ONDO |
| `GOLD_BACKED` | PAXG |
| `WEB3_APPLICATIONS` | BAT |

Each classified crypto symbol (those `MANAGED_CRYPTO_CLASSIFICATIONS_JSON` names or startup
discovers, and every crypto symbol classified before) gets sector `CRYPTO` and its built-in
sector as theme, or `CRYPTO_OTHER` when the list does not name it; earlier crypto themes
(`CRYPTO_SHARED`, owner buckets, the `CRYPTO_UNLISTED` marker) are superseded by appended rows,
as the bucket import does. A listed coin that is neither configured nor classified before is not
imported (a pair Alpaca no longer lists never fails startup). When the owner supplies
`MANAGED_CRYPTO_BUCKETS_JSON`, it overrides the built-in list completely: the import is exactly
the `MANAGED_SERVER_CLASSIFICATIONS_V2` import, including its `unlisted` rule. Returning from
sectors or buckets to the V1 shared theme stops startup (`EXISTING_CLASSIFICATION_CONFLICT`), as
before.

**Top-K open-coin decline.** For selections under `JEV_TOP_K_SELECTION_V1` only,
`ACTIVE_SYMBOL_ALREADY_MANAGED` (a non-terminal managed setup of the coin exists: a watching,
working, open or closing trade) is added to `TOPK_PERMANENT_ADMISSION_REFUSALS`. The runtime
declines the selection (`RESEARCH_ADMISSION_DECLINED`, reason `ACTIVE_SYMBOL_ALREADY_MANAGED`) and
`TOPK_REPLACEMENT_V1` publishes Jev's next-ranked pick in the same transaction
(`declined_code` `ACTIVE_SYMBOL_ALREADY_MANAGED`). V2, B1 and B2 selections keep the transient
refusal (retried each tick, audited once per runtime and reason).

**Deploy example.** `MANAGED_RISK_POLICY_ID` `JEV_MANAGED_RISK_V3` and
`MANAGED_CLASSIFICATION_POLICY` `ALPACA_CRYPTO_SECTORS_V1` (with
`MANAGED_CRYPTO_CLASSIFICATIONS_JSON` `null`, every tradable USD pair).

## Sector list: provenance and choices

Assigned by the builder (package crypto-size-hold, 2026-09-27) from the public category labels
CoinGecko and CoinMarketCap publish for each coin ("Layer 1 (L1)", "Smart Contract Platform",
"Payments", "Meme", "Decentralized Finance (DeFi)", "Oracle", "Artificial Intelligence (AI)",
"DePIN", "Layer 2 (L2)", "Real World Assets (RWA)", "Tokenized Gold"), from the builder's
knowledge of those pages. **The pages were not fetched: this package makes no network calls,
so the owner should confirm the list** (a change is a new list version, or an owner bucket file
that overrides it). Choices where a coin carries several labels:

- BTC is in `LARGE_CAP_L1` with ETH and SOL (not `PAYMENTS`): the three largest layer-1 chains,
  which the owner said may all be open at once; a three-per-sector limit allows exactly that.
- DOGE is in `MEME` (its primary label), not `PAYMENTS`.
- HYPE is in `DEFI`: Hyperliquid's chain exists to run its perpetuals exchange, and its pages
  list both layer-1 and exchange/derivatives labels; it trades with DeFi.
- SKY (formerly MKR, the Sky/Maker lending and USDS protocol) is in `DEFI`.
- FIL (decentralized storage) joins RENDER (GPU rendering, AI) in `AI_COMPUTE`, the
  decentralized compute and storage (DePIN) group; GRT (data indexing) joins LINK in
  `ORACLE_DATA_INFRA`.
- ONDO (tokenized treasuries) is `REAL_WORLD_ASSETS`; PAXG (tokenized gold) is `GOLD_BACKED`;
  BAT (Brave browser advertising) is `WEB3_APPLICATIONS`. Single-coin sectors never bind the
  three-per-sector limit (one open trade per coin already applies).

## Files

New: `src/catalyst_lab/migrations/022_crypto_size_hold.sql`, `src/catalyst_lab/crypto_holding.py`,
`tests/test_crypto_size_hold.py` (29 tests, 33 cases), this file.
Changed: `src/catalyst_lab/account_risk.py` (`MANAGED_RISK_V3_POLICY_ID`, `MarketTerms`, terms
on `AccountRiskPolicy` and `load_policy`, `slice_size`, `slice_sizing_failure`),
`src/catalyst_lab/managed_execution.py` (`admit`: the holding policy for report-V3 crypto;
`authorize_entry`: the slice branch, `_slice_entry`, `_unfilled_reserved_notional`, the
`sizing` context key; `manage`: the 24-hour clock and `HOLD_24H_EXIT`),
`src/catalyst_lab/crypto_execution.py` (`plan_crypto_recovery(time_exit_reason=...)`, default
`TIME_EXIT`), `src/catalyst_lab/managed_classification.py` (`ALPACA_CRYPTO_SECTORS_V1`,
`SECTOR_CLASSIFICATION_POLICY`, `CRYPTO_BUCKET_POLICIES`, the built-in import),
`src/catalyst_lab/managed_app.py` (optional owner buckets with the sectors),
`src/catalyst_lab/managed_runtime.py` (`ACTIVE_SYMBOL_ALREADY_MANAGED` in
`TOPK_PERMANENT_ADMISSION_REFUSALS`), `src/catalyst_lab/managed_service.py` (`holding_policy`
in `STATE_FIELDS`), `src/catalyst_lab/config.py` (`SCHEMA_VERSION` 22),
`deploy/private-paper.example.json`, `docs/MANAGED-RUNTIME.md`, `docs/OPERATIONS-RUNBOOK.md`,
`docs/API-CONTRACT.md`. Tests: `test_owner_migration_rehearsal.py` (new 21-to-22 rehearsal; the
13-to-current and 20-to-21 rehearsals updated), `test_account_risk_policy.py` (four seeded rows,
the deploy example names V3), `test_operator_flatten.py`, `test_managed_engineering.py`,
`test_selection_b2.py` (apply 022 last with the exact-append check), `test_operator_controls.py`
(schema pin 22), `test_managed_execution.py` (the fake venue's per-symbol asset overrides,
default empty).

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-crypto-size-hold`), one
pytest process at a time.

- Targeted during the work: `tests/test_crypto_size_hold.py` 33 passed;
  `test_owner_migration_rehearsal`, `test_operator_controls` and `test_localdb_guard` 48 passed;
  the migration tests of `test_operator_flatten`, `test_managed_engineering`, `test_selection_b2`
  passing; `test_capacity_policy`, `test_account_risk_policy`, `test_managed_execution`,
  `test_managed_day_policy`, `test_managed_app` 80 passed.
- **Full suite at `57b85d2` (the last code and test change; later commits are docs only):
  2,997 passed, 0 failed** (11 min 24 s; baseline 2,963 plus 34 new: 33 in
  `tests/test_crypto_size_hold.py` and the 21-to-22 rehearsal), about 10 GB free before
  the run.
- `ruff check src tests`: all checks passed.

## Deviations

1. **The V3 numbers live in two audited rows, not one.** The owner's size rule has numbers the
   016 row has no column for (the 10% slice, the per-market theme limit, the 2% minimum stop),
   and an audited table never gains a column, so they are a CRYPTO row of the new
   `lab.account_risk_market_terms`, written in the same transaction as the V3 row. The audit
   head therefore moves by exactly two events (the policy row, then its terms), both asserted in
   the rehearsal.
2. **Theme limits are per market.** "US numbers unchanged" (one per theme) and "three per crypto
   sector" cannot share the row's single `max_per_theme`, so a market's terms override it.
   `lab.account_risk_failure` changed in one clause (a LEFT JOIN of the terms and the new entry's
   own limit); V1, the legacy row and V2 answer exactly as 016's function (exhaustively tested,
   including ledgers holding V3 reservations). A policy whose own theme limit exceeds 1 (only
   LAB_FIXTURE rows today) now sees an open V3 crypto reservation's limit as 3, not the V3 row's 1,
   which is the intended stricter-of rule.
3. **Cash for V3 crypto no longer double-counts bought trades.** The existing sizing subtracts
   every open reservation's notional from the broker's cash, although a filled trade has already
   left the cash; with $1,000 slices that would cap filled trades at five. V3 subtracts only the
   unfilled part of each reservation and also takes the broker's non-marginable buying power
   (which nets open orders), so ten $1,000 trades fit in $10,000 whether or not they have filled,
   and an order authorized but not yet at the broker is still counted. V2 keeps the old formula.
   One narrow race remains: an entry authorized just before the account read, not yet netted
   by the broker at that read and filled before the sizing transaction, is counted neither as
   unfilled nor as spent, so the cash can be overstated by that one fill for that one decision;
   the broker then refuses an order it cannot fund (a margin refusal forces a fresh
   reconciliation), and the engine never resizes.
4. **The 2% minimum stop is enforced at entry for every crypto setup under V3**, not only report-
   V3 picks (which `SYSTEM_CHECK_V1` already refuses at admission): an operator `ENGINEERING_TEST`
   enrollment or a report-V2 pick under V3 needs a stop at least 2% below M
   (`STOP_DISTANCE_BELOW_MINIMUM`, terminal, `RISK_REJECTED`), and the reservation guard refuses
   a closer one. Recorded in the policy as asked, and applied for consistency.
5. **Scopes.** The holding policy follows the packet (report-V3 crypto, any selection rule; the
   `SYSTEM_CHECK_V1` scope, agreed with package crypto-trigger, whose `CRYPTO_ALPACA_TRIGGER_V1`
   uses the same scope). Sizing follows the setup's recorded `risk_policy_id` (every setup
   admitted while the runtime names V3). The open-coin decline follows the selection rule
   (top-K only, because only top-K is replaced).
6. **"An open trade"** is the existing `ACTIVE_SYMBOL_ALREADY_MANAGED` rule: any non-terminal
   managed setup of the coin, so a pick is also declined when another cycle's setup of the coin
   is still WATCHING (for example a V2 cycle, or an older run not yet retired). Within one run
   `DUPLICATE_SYMBOL_IN_RUN` already prevents two picks of a coin, and the supersession pass runs
   before admission in each tick.
7. **Exit reason `HOLD_24H_EXIT`** (the plan's "24-hour exit"; the later review will write
   "24-hour review exit"). `plan_crypto_recovery` takes `time_exit_reason` (default `TIME_EXIT`)
   so the protection plan and the CANCEL/EXIT decisions carry the same name as the state.
8. **Classification is a new policy value, `ALPACA_CRYPTO_SECTORS_V1`**, rather than a default for
   the V2 bucket policy (whose contract requires owner buckets). "Owner-overridable" is a full
   override: with `MANAGED_CRYPTO_BUCKETS_JSON` the import is exactly the V2 bucket import,
   including its `unlisted` rule, not a per-coin merge with the built-in list. The built-in import
   classifies every crypto symbol classified before as well as the configured ones, and never
   reads (or fails on) a listed pair that is neither.
9. **The deploy example also switches the classification policy.** Under
   `MANAGED_SERVER_CLASSIFICATIONS_TEST_V1` every crypto pair shares one theme, so V3's
   three-per-sector limit would allow three crypto trades in total.
10. **Existing tests changed** where the brief requires (schema pins, the migration rehearsals'
    audit-head deltas, the seeded policy count, the deploy example's policy) and in the three
    migration tests that apply every migration up to the current schema: they keep their
    DDL-only assertions at 21 and then apply 022 with the exact-append assertion. The fake venue
    gained per-symbol asset metadata overrides (empty by default).
11. **The session harness is unchanged**: `scripts/agent_research_session.py` still simulates
    `JEV_MANAGED_RISK_V2` with its own owner buckets.

## Open items and questions

- **Owner steps:** migrate the owner ledger to 22 (`ledger-migrate`, OPERATIONS-RUNBOOK.md),
  switch the private config to `JEV_MANAGED_RISK_V3` and `ALPACA_CRYPTO_SECTORS_V1`, and run the
  plan's phase-4 supervised paper trade. No V3 entry has reached Alpaca yet.
- **Confirm the sector list** (not fetched; see provenance). Any change is a new list version or
  an owner bucket file.
- **The continue-or-exit review** (plan 4.6.4) is to replace `HOLD_24H_EXIT` (`crypto_holding.py`).
- **A coin Alpaca lists after startup** has no classification until the next restart, so a pick
  of it waits on `CORRELATION_UNKNOWN` (transient; under top-K it holds its slot until expiry).
  Options: classify unknown crypto symbols as `CRYPTO_OTHER` on the fly, or make
  `CORRELATION_UNKNOWN` a top-K decline (the replacement package's open question too).
- **The research context's `open_trades`** do not show `holding_policy` or `hard_exit_at`; an
  agent might want the exit time. Not changed here (outside this package's files).
- **The session harness** could simulate V3 and the built-in sectors so an owner session sizes as
  production does.
- **Partial entries** (plan 4.6.1: cancel the unfilled rest after 10 minutes) are not in this
  package; a partially filled V3 entry's rest stays until the setup's expiry, as before.
- **Merge with package crypto-trigger:** agreed line ownership in `admit`, `authorize_entry` and
  `STATE_FIELDS`; its trigger accepts this package's direct `observe_trigger` fixtures unchanged
  (confirmed by that package). It may add migration 023 on top of 022.
