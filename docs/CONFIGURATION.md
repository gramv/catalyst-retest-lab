# Configuration: every setting a deployer chooses

**PAPER TRADING ONLY.** This page lists the choices you make when you run Catalyst Lab — the
local stack, or the managed paper engine on your own Alpaca paper account — with the default a
newcomer gets and the value of the project's **reference deployment** (the owner's own paper
account on Railway). The rule versions and their numbers are summarized in
[REFERENCE-RULES.md](REFERENCE-RULES.md). **The reference values are one deployment's decisions, not requirements for
yours**; the safety rules below the line are not choices at all.

Every rule is a *named version* (`NAME_V<n>`). A setting picks a version; it never edits one. A
trade keeps the versions it was admitted under, whatever the setting says later.

Where settings live:

- **Local stack**: `compose.yaml` (`environment:` of each service) and, for the opt-in managed
  engine, your own `.env.paper` (from `deploy/local/alpaca-paper.env.example`) layered over
  `deploy/local/managed-defaults.json`.
- **Cloud (Railway)**: service variables, validated by `cloud_config`.
- **Mac launcher**: the private config v2's `environment` map (`deploy/private-paper.example.json`).

Startup validates every setting with the same constructors the engine uses and refuses a missing,
unknown or half-configured one by name — never by value.

## 1. The local stack

| Setting (service) | Values | Newcomer default |
| --- | --- | --- |
| `CATALYST_VENUE` (trader) | `simulated` (`SIMULATED_VENUE_V1`: strategies in shadow, no broker) or `alpaca-paper` (the managed engine, profile `alpaca-paper`) | `simulated` |
| `CATALYST_SIM_BARS` (trader, jobs) | `public` (Alpaca's keyless public crypto bars) or `synthetic` (`SYNTHETIC_SAMPLE_BARS_V1`: generated, offline, never evidence) | `public` |
| `CATALYST_SIM_SYMBOLS` (trader) | comma list of `X/USD` pairs (`synthetic`: BTC, ETH, SOL, DOGE only) | `BTC/USD,ETH/USD,SOL/USD,DOGE/USD` |
| `CATALYST_STRATEGY_PLUGINS_DIR` | one folder or several separated by `:` (`STRATEGY_PLUGIN_LOADER_V1`) | `/app/examples/strategies:/my-strategies` |
| `CATALYST_HISTORY_TEST_OUT`, `CATALYST_HISTORY_CACHE_DIR` | history-test report and bar-cache folders | `/data/history-tests`, `/data/history-cache` (the `history` volume) |
| page `ports` | where the read-only page listens on your machine | `127.0.0.1:8080` |

The stack generates its database passwords (`local_stack secrets`) and never accepts them from
you; the ledger is disposable (`docker compose down -v`) and is refused if its schema is behind
the code (`LOCAL_STACK_LEDGER_SCHEMA_BEHIND`).

## 2. The managed paper engine

| Choice | Setting | Versions / values | Newcomer default (`managed-defaults.json`) | Reference deployment |
| --- | --- | --- | --- | --- |
| **AI mode** | `CATALYST_AI_MODE` | `JEV_AI_MODE_V1` (research agents + Jev), `NO_AI_MODE_V1` (mechanical plug-ins only, code-only exits). Absent = `JEV_AI_MODE_V1`. | `NO_AI_MODE_V1` | absent (`JEV_AI_MODE_V1`) |
| **Risk policy** | `MANAGED_RISK_POLICY_ID` | rows of `lab.account_risk_policies`: `JEV_MANAGED_RISK_V2` … `_V5` (below) | `JEV_MANAGED_RISK_V5` | `JEV_MANAGED_RISK_V4` in the committed example |
| **Strategies on paper** | `MANAGED_STRATEGIES_JSON` | JSON list of promoted strategy ids (`STRATEGY_PAPER_PATH_V1`) | `[]` (none) | absent (none) |
| **Selection rule** (research picks) | `MANAGED_SELECTION_RULE`, `MANAGED_TOPK_SELECTION_JSON`, `MANAGED_SELECTION_QUALITY_FLOOR` | `MUSE_JEV_RESEARCH_SELECTION_V2` (absent), `…_B1_V1`, `…_B2_V1`, `JEV_TOP_K_SELECTION_V1/_V2/_V3` with `{"k": n}` | not allowed (no AI) | `JEV_TOP_K_SELECTION_V2`, `{"k": 10}` |
| **Maintenance** (Jev reviews open trades) | `MANAGED_MANAGEMENT_REVIEWS` | `ENABLED` / `DISABLED`. Admission records `CRYPTO_MAINTENANCE_V5` for maintained trades (fixed in this release) | `DISABLED` (required by no-AI) | `ENABLED` |
| **Trade window** | `MANAGED_CRYPTO_WINDOW_JSON` | `{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 60–1440, step 15}` | 1440 | 240 |
| **Stop execution / maker entry** | `MANAGED_CRYPTO_EXECUTION_JSON` | `{"stop_execution": null \| "CRYPTO_STOP_EXECUTION_V1", "maker_entry": null \| "CRYPTO_MAKER_ENTRY_V1"}` | both `null` | both `null` |
| **Day policy** | `MANAGED_CRYPTO_DAY_POLICY_JSON` | `CRYPTO_NY_DAY_PAPER_V1` (entry cutoff and flatten before New York midnight for day-policy setups) | as reference | `CRYPTO_NY_DAY_PAPER_V1` |
| **Liquidity gate** | `MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON` | `CRYPTO_LIQUIDITY_PAPER_V1` or absent | absent | absent (Alpaca's thin crypto venue fails it for every coin) |
| **Coin sectors** | `MANAGED_CLASSIFICATION_POLICY`, `MANAGED_CRYPTO_BUCKETS_JSON`, `MANAGED_CRYPTO_CLASSIFICATIONS_JSON` | `ALPACA_CRYPTO_SECTORS_V1` or your own buckets | `ALPACA_CRYPTO_SECTORS_V1` | `ALPACA_CRYPTO_SECTORS_V1` |
| **Research schedule** | `MANAGED_RESEARCH_SCHEDULE_JSON` | `RESEARCH_SCHEDULE_V1/_V2` | not allowed (no AI) | every 2 hours, daily run 08:00 New York |
| **Jev budget** | `JEV_MONTHLY_BUDGET_USD`, `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD`, `JEV_BYTES_PER_TOKEN` | `JEV_SPEND_GUARD_V1` | not allowed (no AI) | $50, 0.042, 3 |
| **Jev review policy** | `JEV_REVIEW_POLICY_JSON` | `JEV_LIVE_REVIEW_POLICY_V2` | present (the worker is composed; it has nothing to review) | `JEV_LIVE_REVIEW_POLICY_V2` |
| **Engine timing** | `MANAGED_RUNTIME_POLICY_JSON`, `MANAGED_SOURCE_POLICY_JSON`, `MANAGED_CYCLE_POLICY_JSON`, `MANAGED_POSITION_POLICY_JSON`, `MANAGED_MONITOR_TRIGGER_POLICY_JSON` | engineering profiles; the position policy must equal the engine's | as reference | `deploy/private-paper.example.json` |

### Risk policies (one row each; a new row is a new version)

| Version | What it adds | |
| --- | --- | --- |
| `JEV_MANAGED_RISK_V2` | the first managed policy | earlier |
| `JEV_MANAGED_RISK_V3` | crypto sized in 10%-of-equity slices, each capped at 0.5% planned risk; 5% crypto open-risk cap; three per crypto sector | earlier |
| `JEV_MANAGED_RISK_V4` | V3 with a **2%** crypto open-risk cluster cap; daily limits in the policy: **soft 2%** (no new entries), **hard 3%** (cancel and flatten) | reference |
| `JEV_MANAGED_RISK_V5` | V4 plus **0.5% of equity** open risk per mechanical strategy (`STRATEGY_RISK_CAP`) | newcomer default |

### Versions fixed in this release (not settings)

These are recorded on every setup they apply to. Changing one is a code change and a new name:
`CRYPTO_TRADE_PLAN_V1` (stop 2% below the fill widened to two hourly ranges, target capped at
1.5R, 24 hours; admitted once migration 027 is present), `CRYPTO_ENTRY_PACING_V1` (two entries per
30 minutes, a median-coin drop gate), `SYSTEM_CHECK_V1`, `STALE_PRINT_ABOVE_TRIGGER_V1`, the
trigger versions (`CRYPTO_ALPACA_TRIGGER_V1`, `CRYPTO_COINBASE_TRIGGER_V1`), the stop-breach
versions (`CRYPTO_STOP_BREACH_V*`) and `CRYPTO_MAINTENANCE_V5`'s raise guards (from V4).

### Exclusions

- **Days left out of performance statistics** (`STATS_EXCLUSION_V1`): an audited, append-only
  record per New York day, made by the deployment's owner (`cloud_runtime exclude-from-stats`).
  The account's balance, equity and fees always include every trade.
- **Engineering test trades** (`TEST-` enrollments) never enter strategy reporting, while
  account risk and reconciliation always include them.
- **The liquidity gate** above, and **forex** (research-only in the reference deployment).

## 3. AI mode in detail

`NO_AI_MODE_V1` (`ai_mode.py`, `AI_MODE_SETTING_V1`) means no AI judge anywhere:

- **Selection**: only promoted mechanical plug-ins (`STRATEGY_SIGNAL_SELECTION_V1`, no Jev
  receipt). The research-report routes answer 503 `MUSE_REPORT_INTAKE_NOT_CONFIGURED`. The
  plug-ins scan every Alpaca USD pair of `ALPACA_CRYPTO_SECTORS_V1`.
- **Maintenance**: every setup is admitted in the `FIXED_EXIT` arm (`arm_method`
  `NO_AI_MODE_V1`): the broker-held stop, the plan's target, the trade window
  (`CRYPTO_WINDOW_HOLD_V1`), the daily limits and the operator's flatten. No maintenance question
  is ever asked.
- **Refused at startup** (half-configured AI): a TypeSafe key or key file, management reviews not
  `DISABLED`, any Jev selection rule, K, quality floor or budget setting, research-agent tokens or
  a research schedule (`NO_AI_MODE_*` codes), and any unknown mode value (`AI_MODE_UNKNOWN`).

`JEV_AI_MODE_V1` is the reference deployment: research agents send picks with cited evidence over
a token-scoped API, Jev ranks them and reviews open trades, and a randomized 30% `FIXED_EXIT`
control arm measures what the reviews add. It needs a TypeSafe key, agent tokens, a research
schedule and a Jev budget.

---

## Not configurable (safety rules)

- Paper only: the broker endpoints are fixed in code; `APCA_API_BASE_URL`, `ALPACA_BASE_URL` and
  `APCA_DATA_BASE_URL` may only repeat the paper values (the local stack refuses them outright).
- Every broker POST, PATCH and DELETE needs a committed, unexpired (5 seconds), exact-request,
  one-use risk authorization. No setting or flag bypasses it.
- Research agents can create and read candidates and read analytics; they never set size or
  trigger execution.
- The ledger is append-only and hash-chained; schema changes are numbered migrations; application
  roles never own tables.
- One executor per paper account.
