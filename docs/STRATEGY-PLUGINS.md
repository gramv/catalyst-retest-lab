# Strategy plug-ins (`STRATEGY_REGISTRY_V1`, `STRATEGY_SDK_V1`)

**PAPER TRADING ONLY.** A strategy plug-in is tested on history and in shadow, and reaches the
paper account only through a promotion that the owner approves. Nothing here can send a live
order, and no plug-in has an order path of its own: every paper order goes through the same
admission, system check, trade plan, pacing, account risk and exact one-use risk authorization
as every other setup.

Strategies are plug-ins: any strategy can be written against the SDK, tested on history, then
run on live data in shadow before it is considered for paper.

## Write your first strategy

1. **Get a skeleton.** From the checkout:

   ```
   ./run catalyst-lab new-strategy MY_BREAKOUT --dir ~/my-strategies
   ```

   This writes `~/my-strategies/my_breakout_v1.py`: one mechanical strategy `MY_BREAKOUT_V1` at
   stage `SHADOW`, written against `catalyst_lab.strategies.sdk` only, with a placeholder rule
   (an hourly close above the prior 24 hours' high). It never overwrites a file and refuses a
   built-in's name.

2. **Write the rule.** Edit `signals(bars, context)`:
   - `bars`: ascending, completed 1-hour bars of one coin (`start`, `open`, `high`, `low`,
     `close`, `volume`; UTC; Decimals) — at least `history_hours` of them before the window.
   - `context`: `symbol`, `since`, `until` (answer only for signals whose time, the end of a bar,
     is in `(since, until]`; `sdk.signal_bars` walks exactly those) and, in the history tester,
     `parameters` (one of your declared variants).
   - Return a list of proposals. `sdk.marketable_proposal(STRATEGY.strategy_id, symbol, bars,
     bar, facts={...})` builds the one shape the paper path supports, `MARKETABLE_AT_SIGNAL`:
     buy right after the bar closes, with `CRYPTO_TRADE_PLAN_V1`'s exits (stop 2% below the fill
     widened to two hourly ranges, target 1.5R, 24 hours). Put the numbers your rule looked at
     in `facts`; they are recorded with every signal.
   - Keep it pure: no I/O, no clock, no randomness. Use `Decimal`.
   - Declare `history_variants` (and `DEFAULT_VARIANT_ID` for the registered one) **before** the
     first history test. Every variant is a trial in the trials ledger; adding variants after
     seeing results is exactly what the deflated Sharpe counts against you.
   - `simulate` defaults to `sdk.simulate_marketable_proposal`, the shared simulation the
     breakout uses; you rarely need your own.

3. **Test it on history.**

   ```
   ./run catalyst-lab history-test --plugins-dir ~/my-strategies --strategy MY_BREAKOUT_V1
   ```

   The report (`REPORT.md`, `results.json`) shows every variant net of fees and slippage, the
   walk-forward out-of-sample result, the deflated Sharpe, PBO and breakdowns (see "Running the
   history tester" below). Rung 1 of the ladder is met when the walk-forward out-of-sample
   trades are at least 30 with a positive mean net R, the lower end of its 90% interval above
   zero, a deflated Sharpe of at least 0.95 and a PBO of at most 0.5. Most ideas fail here; that
   is the point.

4. **Run it in shadow.** Point the nightly jobs at the folder (`CATALYST_STRATEGY_PLUGINS_DIR`,
   or install it as a package, below). The `shadow_outcomes` step records its live signals on
   completed bars and each signal's simulated outcome, with no orders. The scorecard's and the
   weekly review's `strategies` section shows it beside the others, labelled `SHADOW`, with its
   count against the 30-trade minimum.

5. **Ask the owner for paper.** When the history test and shadow meet the ladder, the owner may
   promote it (below). Nothing promotes itself.

Worked examples live in `examples/strategies/` (package oss-packaging adds two to the first):
`EXAMPLE_MA_CROSS_V1` (a 12/48-hour mean-close cross), `EXAMPLE_DONCHIAN_BREAKOUT_V1` (a fresh
close above the prior 48-hour high, three channel variants) and `EXAMPLE_RSI_REVERSION_V1`
(Wilder's RSI crossing back above 30 while the 72-hour mean rises, computed in Decimal). They run
through the history tester and the shadow on fixture and generated bars
(`tests/test_strategy_plugin_loader.py`, `tests/test_example_strategies.py`). They are teaching
examples, not recommendations: none has passed a history test on real bars. The step-by-step
walkthrough on the local Docker stack is [FIRST-STRATEGY.md](FIRST-STRATEGY.md).

## Where plug-ins come from (`STRATEGY_PLUGIN_LOADER_V1`)

- **Built-ins**: the modules in `src/catalyst_lab/strategies/`, registered in its `__init__.py`.
- **A folder**: `CATALYST_STRATEGY_PLUGINS_DIR` (the runtime, the jobs and the history tester
  read it; the history tester also takes `--plugins-dir`). Every `*.py` file not starting with
  `_` is imported in name order as `catalyst_strategy_plugins.<stem>`. Several folders may be
  given, separated by `:` (package oss-packaging; the local stack loads
  `/app/examples/strategies:/my-strategies`).
- **An installed package**: an entry point in the group `catalyst_lab.strategies`, e.g. in its
  `pyproject.toml`:

  ```
  [project.entry-points."catalyst_lab.strategies"]
  my_breakout = "my_package.my_breakout_v1"
  ```

  An entry may load a `Strategy`, a module, or a callable returning one or several.

A module offers `STRATEGY` (one record) or `STRATEGIES` (a sequence). Every record is checked
before it joins the registry: it must be a `Strategy` (which checks its own id, version, entry
types, plan rules, stage and sources), must not take a built-in's id or name, must not declare
`LIVE_PAPER` or a `live_trigger` (a plug-in reaches paper only through the owner's promotion
record), and its `signals(bars, context)` / `simulate(proposal, minute_bars, *, fee_rate)` must
take the interface's arguments. A refused plug-in is reported with a `PLUGIN_*` code and never
registered; the built-ins keep working. Importing a plug-in runs its code: only folders and
packages you installed are read.

## The SDK (`STRATEGY_SDK_V1`, `catalyst_lab.strategies.sdk`)

The stable surface a plug-in imports; a later change is `STRATEGY_SDK_V2`, never an edit:
`Strategy`, `mechanical_strategy(...)`, the stages (`HISTORY_TEST`, `SHADOW`), `MECHANICAL`, the
entry types (`PULLBACK`, `IMMEDIATE`, `BREAKOUT`), `CRYPTO_TRADE_PLAN_V1`, `Bar`, `signal_bars`,
`marketable_proposal`, `simulate_marketable_proposal`, `MARKETABLE_AT_SIGNAL`, and the decision
core's helpers `hourly_range_at`, `mean_range`, `slippage_estimate`, `plan_levels`,
`walk_to_exit`, `r_values`, `touches_entry`, `reaches_stop`, `reaches_target`.

`mechanical_strategy` fills in the defaults: source `MECHANICAL`, plan `CRYPTO_TRADE_PLAN_V1`,
stage `SHADOW`, `simulate_marketable_proposal`, entry types `BREAKOUT` (declare `IMMEDIATE` too
when your signal may come with the price already at the collar), `history_hours` 216.

## What a strategy is (the record)

| Field | Meaning |
|---|---|
| `strategy_id`, `name`, `version` | `NAME_V<n>`, e.g. `BREAKOUT_7D_VOL2X_V1`. A changed rule is a new id, never an edit. |
| `description` | One sentence: what it buys and how it exits. |
| `entry_types` | `PULLBACK`, `IMMEDIATE`, `BREAKOUT` (`strategies/core.py`). The system check refuses a setup whose live entry type the strategy does not trade. |
| `trigger_rule` | The named rule that decides its entry. A live research strategy also gives `live_trigger(state)`. |
| `plan_rules` | The trade-plan version its stop, target and window follow. Today only `CRYPTO_TRADE_PLAN_V1`. |
| `jev_questions` | The Jev question sets it uses (none for a mechanical strategy). |
| `sources` | `RESEARCH_REPORT` (research agents' report-V3 picks) and/or `MECHANICAL` (its own signals). |
| `stage` | `HISTORY_TEST`, `SHADOW` or `LIVE_PAPER` (built-in research strategies only; a mechanical strategy's paper stage is its promotion record). |
| `signals(bars, context)` | Mechanical only. Pure: completed bars in, proposals out. |
| `simulate(proposal, minute_bars, *, fee_rate)` | Mechanical only. Pure: the proposal's outcome on 1-minute bars, built from the shared core. |
| `parameters` | The numbers of the rule, recorded for the reader. |
| `history_variants` | Mechanical only, optional. The variants the history tester tries, declared before any run. |
| `history_hours` | The completed 1-hour bars `signals` needs before the first bar it evaluates (default 216). |

## The registered strategies

- **`PULLBACK_V1`** (`LIVE_PAPER`, `RESEARCH_REPORT`). Research agents' report-V3 crypto picks,
  Jev's top-K selection, `SYSTEM_CHECK_V1`, `CRYPTO_TRADE_PLAN_V1`, and a pullback or immediate
  touch of the entry trigger under the trigger version the setup recorded. Every pick without a
  `strategy_id` is `PULLBACK_V1`.
- **`BREAKOUT_7D_VOL2X_V1`** (`SHADOW`, `MECHANICAL`). An hourly close above the highest high of
  the prior 7 days on a 24-hour volume at least 2× the 7-day mean; marketable entry;
  `CRYPTO_TRADE_PLAN_V1`'s exits. Its history test failed rung 1 (−0.23 R per trade
  after costs); it stays in shadow as a measurement.

## The promotion ladder

1. **History test** (walk-forward, every variant counted, deflated Sharpe, PBO) — rung 1 as above.
2. **Shadow**: live signals on completed bars, simulated fills, no orders, at least **30
   simulated trades** with a positive mean net R.
3. **Paper** (`STRATEGY_PAPER_PATH_V1`): two gates, both the owner's.
   - **Promotion record.** The owner runs

     ```
     managed_ops promote-strategy --config PATH --strategy MY_BREAKOUT_V1 \
         --history-report <out>/results.json --owner-ruling "my ruling: MY_BREAKOUT_V1 to paper"
     ```

     (on Railway: `railway ssh --service trader -- python -m catalyst_lab.cloud_runtime
     promote-strategy --strategy ... --history-report - --owner-ruling ... < results.json`).
     It appends one audited `STRATEGY_PROMOTION` event (`STRATEGY_PROMOTION_V1`) naming the
     strategy id, the SHA-256 of the history report, its figures, the ledger's own shadow
     scorecard cell, `history_test.promotion_check`'s result and the owner's ruling. It is
     **refused** unless that check says `ELIGIBLE_FOR_OWNER_PAPER_REVIEW` — except with
     `--owner-override-reason "..."` (10–500 characters), which is recorded in the event for
     everyone to see. `demote-strategy --strategy ID --reason "..." --owner-ruling "..."` appends
     a `STRATEGY_DEMOTION`: from then on the strategy's packets are refused and its setups still
     waiting for an entry are revoked; open positions keep their protection and exits.
   - **Configuration.** The runtime's `MANAGED_STRATEGIES_JSON` lists it (a JSON list of ids;
     absent or empty is the default: nothing). Both are needed: a listed strategy without an
     active promotion records nothing; a promoted one that is not listed is never admitted.

Changes to a promoted strategy are a new id (`..._V2`) and climb the ladder again.

## The paper path (`STRATEGY_PAPER_PATH_V1`)

For each listed, promoted strategy, once per completed hour (90 seconds after the hour), the
runtime reads each coin's completed 1-hour bars from Alpaca's keyless public endpoint (the
shadow's source), runs the strategy's own `signals` for the hour that just closed, and for each
fresh `MARKETABLE_AT_SIGNAL` proposal still inside its 15-minute window appends a
`STRATEGY_SIGNAL` event and a `RESEARCH_SELECTED` packet (selection policy
`STRATEGY_SIGNAL_SELECTION_V1`, no Jev receipt). The packet then takes the research picks' path:

- **Levels** (on the coin's broker grid): entry trigger = max entry = the signal close + 0.5%
  (rounded up); stop 2% below; a research target at reward/risk 2. The marketable entry is the
  existing trigger touching that collar on the next fresh quote; the order is a limit at the
  collar.
- **Admission**: the ledger checks, the broker grid, `SYSTEM_CHECK_V1` (price within 5% of the
  signal close, stop not hit, the strategy's entry types), `CRYPTO_TRADE_PLAN_V1` (stop widened
  to two hourly ranges, target capped at 1.5R, 24 hours), and the admission SQL's check of the
  signal and the promotion (`STRATEGY_NOT_PROMOTED`, `STRATEGY_DEMOTED`,
  `STRATEGY_SIGNAL_BINDING_FAILURE`, `REVIEW_EXPIRED` after 15 minutes).
- **Entry**: `CRYPTO_ENTRY_PACING_V1`, the daily soft and hard limits, the account-risk policy
  and the one-use five-second authorization of every broker change. Under `JEV_MANAGED_RISK_V5`
  each mechanical strategy may hold at most **0.5% of equity** of open planned risk (and every
  crypto trade still counts toward the 2% crypto cluster cap); an entry over it waits
  (`STRATEGY_RISK_CAP`, a capacity reason).
- **Jev**: none for the selection. An open trade in the maintained arm keeps the maintenance
  reviews exactly as a research trade does.
- **Results**: setups record `strategy_id`, `strategy_version`, `strategy_path`, the promotion and
  the signal; results group by strategy, and the shadow keeps measuring the same strategy on the
  same bars, so shadow and paper R can be read side by side.

## Running the history tester (`HISTORY_TEST_V1`)

Offline: keyless public bars, no orders, no broker, no Jev, no ledger. From the checkout:

```
./run catalyst-lab history-test                       # every registered strategy, 12 months
./run catalyst-lab history-test --strategy BREAKOUT_7D_VOL2X_V1 \
    --start 2025-10-01 --end 2026-10-01 --source coinbase --fees coinbase-taker-10k-50k
./run catalyst-lab history-test --plugins-dir ~/my-strategies --strategy MY_BREAKOUT_V1
./run python -m catalyst_lab.history_test --help      # the same tool
```

- **Inputs**: `--strategy` (repeatable), `--plugins-dir`, `--universe` (`alpaca`: the 33 Alpaca
  USD pairs; `coinbase`: those with a Coinbase USD product; or a comma list), `--start`/`--end`
  (UTC days; default the 365 days ending two days ago), `--source` (`alpaca` public bars,
  `coinbase` public candles, or `synthetic`: `SYNTHETIC_SAMPLE_BARS_V1` generated sample bars for
  tutorials and tests -- offline, labelled `SYNTHETIC`, its promotion check always `NONE`, its
  trials in `TRIALS-synthetic.jsonl`, and refused by `promote-strategy`), `--fees` (`alpaca-taker` 0.25%/leg default, `alpaca-maker` 0.15%,
  Coinbase tiers), `--no-slippage`, `--equity` (account simulation, default 10,000),
  `--is-days`/`--oos-days` (60/30), `--offline` (cache only), `--shadow-cell` (a shadow cell for
  the promotion check).
- **Cache**: `~/.local/share/catalyst-history-cache/<source>/<timeframe>/<SYMBOL>/` (one file per
  month of 1-hour bars, per day of 1-minute bars; only ended chunks). Requests are spaced
  (0.35 s Alpaca, 0.15 s Coinbase) and retried on 429/5xx.
- **Output**: `~/.local/share/catalyst-handoff/history-tests/<date>/` (`REPORT.md`,
  `results.json`, `trials.jsonl`) and the cumulative trials ledger `../TRIALS.jsonl`; the
  deflated Sharpe uses the number of distinct variants in that ledger. `results.json` is what
  `promote-strategy --history-report` takes (its SHA-256 is recorded).
- **What it reports**: every variant's net R per trade (fees and slippage on both legs) with a
  day-bootstrap 90% interval, win rate, drawdown in R and Sharpe; the walk-forward selection's
  out-of-sample trades and the registered rule in the same windows; deflated Sharpe and PBO
  (CSCV); breakdowns by window, month, prior-day `MARKET_REGIME_V1` and coin; an account
  simulation under `JEV_MANAGED_RISK_V4` sizing, the 2% crypto cluster cap, three per sector,
  one per coin, the 2% soft limit (realized) and `CRYPTO_ENTRY_PACING_V1`.
- A strategy without `signals` (`PULLBACK_V1`, fed by research agents and Jev) is skipped with
  `NO_MECHANICAL_PROXY_IN_CORE`.

## What is shared code (backtest = shadow = live)

`strategies/core.py` holds the functions every path calls:

| Function | Live | Shadow / replay |
|---|---|---|
| `touches_entry`, `reaches_stop` | the Alpaca trigger (`crypto_trigger`) and the Coinbase reference trigger (`coinbase_trigger`): prints and quotes | `pick_outcomes.simulate_pick` via `pullback_bar_event` (a bar's low) |
| `reaches_stop`, `reaches_target` | the open position's stop-breach marks (`stop_breach` V2/V3, `stop_execution`'s Coinbase print run, the engine's V1 bid mark, the partial entry's stop) and the protection planner's target touch (`crypto_execution`) | the exit walk's stop and target |
| `range_floor`, `target_cap` | `trade_plan.plan` (admission, also of strategy signals) | `plan_levels` in the marketable simulation |
| `walk_to_exit`, `r_values` | — | pick shadow outcomes, replays, Jev calibration, strategy shadow, history tester |
| `breakout_signals` (`BreakoutRule`), `simulate_marketable` | the paper path's signal source (through the plug-in's `signals`) | strategy shadow, history tester |
| `slippage_estimate`, `cost_r` (`HALF_SPREAD_PLUS_VOLATILITY_V1`) | recorded on each paper signal's proposal | strategy shadow, history tester |

Still not shared: the position management of a live trade (`crypto_maintenance`'s stop and target
options) and the fill model (live fills are real; shadow and history fills are 1-minute bar
opens). Live-only concerns — orders, reconciliation, the risk gate — stay in `managed_execution`.

## Where `strategy_id` appears

- **Report V3 pick**: optional `strategy_id`. Absent means `PULLBACK_V1`. Present, it must be a
  registered strategy at `LIVE_PAPER` fed by `RESEARCH_REPORT`; otherwise the pick is refused
  `INVALID_RESEARCH_ITEM` (`STRATEGY_NOT_REGISTERED` or `STRATEGY_NOT_OPEN_TO_REPORTS`). Jev never
  sees it.
- **Strategy signal packet**: `strategy_id`, `strategy_source` `MECHANICAL`, `strategy_path`,
  `signal_event_seq`, `promotion_event_seq`.
- **Setup state**: admission stamps `strategy_id` and `strategy_version` on every report-V3-shaped
  crypto setup, and `strategy_path`, `promotion_event_seq`, `signal_event_seq` on a strategy
  signal's setup. Older setups read as `PULLBACK_V1`.
- **Results**: `result_dimensions.dimensions(...)["by_strategy"]`; the scorecard's
  `overall.strategies` and the weekly review's `strategies` (`live_paper`, `shadow`, `ladder`).
- **Events**: `STRATEGY_SHADOW_SIGNAL`/`STRATEGY_SHADOW_OUTCOME` (shadow), `STRATEGY_SIGNAL`
  (paper path), `STRATEGY_PROMOTION`/`STRATEGY_DEMOTION` (the owner's ladder records), all in
  `lab.managed_events` without a setup.
