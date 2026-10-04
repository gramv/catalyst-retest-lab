# Reference rules: the rule versions and their numbers

**PAPER TRADING ONLY.** A short summary of the named rule versions the code implements and the
numbers each one fixes. The settings you choose (and the reference deployment's values) are in
[CONFIGURATION.md](CONFIGURATION.md); the code named beside each rule is the authority.

Every rule is a *named version* (`NAME_V<n>`). A changed rule is a new name, never an edit of an
old one, and a trade keeps the versions it was admitted under.

## Safety rules (every deployment, never configurable)

- Paper only: the broker endpoints are fixed in code (`config.py`).
- Every broker POST, PATCH and DELETE needs a committed, unexpired (5 seconds), exact-request,
  one-use risk authorization (`risk.py`, `risk_dispatch.py`). No flag bypasses it.
- Research agents create and read candidates and read analytics; they never set size or execute.
- The ledger is append-only and hash-chained; schema changes are numbered migrations; application
  roles own no tables. One executor per paper account.
- Engineering `TEST-` enrollments are excluded from strategy reporting and included in account
  risk and reconciliation (docs/ENGINEERING-ACCEPTANCE.md).

## Account risk (`lab.account_risk_policies`, `account_risk.py`)

| Version | Numbers |
| --- | --- |
| `JEV_MANAGED_RISK_V2` | 0.5% of equity planned risk per trade; 5% total open planned risk; US 3%, crypto 2%, forex 0; two per US sector, one per theme; stocks up to 2× equity intraday, crypto cash only; −3% daily halt cancels and flattens; capacity refusals wait 60 s |
| `JEV_MANAGED_RISK_V3` | V2, with crypto sized in 10%-of-equity slices, each at most 0.5% planned risk; crypto open-risk cap 5%; three open trades per crypto sector; stop at least 2% below the max entry M; 30% randomized fixed-exit arm |
| `JEV_MANAGED_RISK_V4` | V3 with every crypto position in one 2% open-risk cluster; daily limits in the policy: soft 2% (no new entries for the rest of the New York day), hard 3% (cancel and flatten) |
| `JEV_MANAGED_RISK_V5` | V4 plus 0.5% of equity open risk per mechanical strategy (`STRATEGY_RISK_CAP`) |
| `RISK_SESSION_BASELINE_V2` | the day's starting equity (V4 and later) is the account equity at the first clean reconciliation after New York midnight, recorded once per day |

## Crypto entry

| Version | Rule |
| --- | --- |
| `SYSTEM_CHECK_V1` | checks a research pick's geometry before it watches; traded entry types `PULLBACK` and `IMMEDIATE`; reward-to-risk at least 2R at M |
| `CRYPTO_ALPACA_TRIGGER_V1` / `CRYPTO_COINBASE_TRIGGER_V1` | the entry trigger is a print at or below the trigger price, on Alpaca / on Coinbase's public feed (`coinbase_feed.py`) |
| `STALE_PRINT_ABOVE_TRIGGER_V1` | a print more than 5 s old and above the trigger is ignored instead of revoking the setup |
| `CRYPTO_ENTRY_PACING_V1` | at most 2 crypto entries per rolling 30 minutes; wait while the median 1-hour return of the streamed coins is −2% or worse, while fewer than 3 coins can be measured, and from 15 minutes before to 45 minutes after a US CPI or FOMC release (`macro_calendar_v1.json`) |
| `CRYPTO_ENTRY_WORKING_LIMIT_V1` | an unfilled entry works 300 s; a trade at or below the stop before the fill cancels it |
| `CRYPTO_STREAM_CAPACITY_V1` | the crypto stream carries at most 15 coins (Alpaca's 30 channels) |
| `CRYPTO_MAKER_ENTRY_V1` (setting) | a pullback entry rests at min(entry trigger, bid) instead of M |

## Crypto plan, exits and maintenance

| Version | Rule |
| --- | --- |
| `CRYPTO_TRADE_PLAN_V1` | stop = min(research stop, trigger − 2 × the mean hourly range of the last 24 hours); target capped at 1.5R; 24-hour window |
| `CRYPTO_WINDOW_REVIEW_V1` / `CRYPTO_WINDOW_HOLD_V1` | the trade window (60–1440 minutes, setting); review or plain hold at its end |
| `CRYPTO_STOP_BREACH_V2` / `_V3` | the market fallback sells only on a trade at or below the stop, or the bid held there 15 s (V3: Coinbase prints decide, V2 as fallback) |
| `CRYPTO_STOP_BREACH_V4` | the native stop-limit's limit is 0.5% under the stop |
| `CRYPTO_STOP_EXECUTION_V1` (setting) | the app decides the stop on Coinbase prints (2 trades within 5 s, or 3 s at the stop) and exits with a collared IOC sell, then market |
| `CRYPTO_MAINTENANCE_V4` | no stop raise before +1R, never within 2 hourly ranges of the bid, at most one raise per 15 minutes, breakeven includes 0.25% fees each way |
| `CRYPTO_MAINTENANCE_V5` | the reviewer answers two yes/no questions (the pick's stated disproof happened; new news contradicts the thesis); p ≥ 0.80 is YES, three counted YES in a row raise an early-exit flag; code owns every level |

## Selection

| Version | Rule |
| --- | --- |
| `JEV_TOP_K_SELECTION_V2` | the reviewer selects up to K picks per research run (reference K = 10) |
| `JEV_TOP_K_SELECTION_V3` | narrow per-pick checks (vetoed at `thesis_contradicted` ≥ 0.70 or half the claims ≤ 0.30), then one shuffled comparative review; select p ≥ 0.60 up to K, or none |
| `RESEARCH_SCHEDULE_V2` | one full daily run plus update runs (reference: every 2 hours, full run 08:00 New York); a pick is valid at most 24 hours |
| `STRATEGY_SIGNAL_SELECTION_V1` | mechanical plug-ins select themselves, no reviewer (`NO_AI_MODE_V1`) |

## Strategies and measurement

| Version | Rule |
| --- | --- |
| `STRATEGY_REGISTRY_V1`, `STRATEGY_SDK_V1`, `STRATEGY_PLUGIN_LOADER_V1` | strategies are plug-ins with a name and version (docs/STRATEGY-PLUGINS.md) |
| `HISTORY_TEST_V1` | walk-forward 60/30 days; rung 1 needs ≥ 30 out-of-sample trades, mean net R > 0 with the 90% interval's low end > 0, deflated Sharpe ≥ 0.95 and PBO ≤ 0.5 |
| `HALF_SPREAD_PLUS_VOLATILITY_V1` | slippage per leg: half the Abdi–Ranaldo spread (1–50 bp) plus 0.5 × hourly range / price / √60 |
| `STRATEGY_SHADOW_V1` | a strategy's signals and outcomes recorded without orders |
| `STRATEGY_PROMOTION_V1` / `STRATEGY_PAPER_PATH_V1` | a strategy trades on paper only when promoted (audited event) and listed in `MANAGED_STRATEGIES_JSON` |
| `JEV_CALIBRATION_V1` | each reviewer probability recorded next to its outcome; weekly reliability, Brier score and ranking lift (record only) |
| `STATS_EXCLUSION_V1` | an audited, append-only record of New York days left out of performance statistics; account figures include every trade |

## Modes and local tools

| Version | Rule |
| --- | --- |
| `AI_MODE_SETTING_V1` | `JEV_AI_MODE_V1` (research agents and the reviewer) or `NO_AI_MODE_V1` (mechanical plug-ins only, fixed exits, half-configured AI refused at startup) |
| `SIMULATED_VENUE_V1` | the local stack's default trader: strategies in shadow on public bars, no broker |
| `SYNTHETIC_SAMPLE_BARS_V1` | generated sample bars for tutorials; a history test on them can never promote a strategy |
