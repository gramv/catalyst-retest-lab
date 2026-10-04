# Your first strategy: write → history test → shadow → scorecard → (paper)

**PAPER TRADING ONLY.** This walkthrough takes one idea through the whole ladder on the local
stack ([QUICKSTART.md](QUICKSTART.md) first). The SDK reference is
[STRATEGY-PLUGINS.md](STRATEGY-PLUGINS.md).

**Read this first.** The three plug-ins in `examples/strategies/` (`EXAMPLE_MA_CROSS_V1`,
`EXAMPLE_DONCHIAN_BREAKOUT_V1`, `EXAMPLE_RSI_REVERSION_V1`) are **teaching examples, not
profitable strategies**. None has passed a history test on real bars. The project's own
breakout, `BREAKOUT_7D_VOL2X_V1`, failed rung 1 on real data: −0.23 R per trade after costs over
271 trades, with all nine variants negative. Expect your first ideas to fail too; the tools exist
to tell you so early and cheaply.

## 1. What a strategy is

One Python file with one function and one record:

- `signals(bars, context)` gets completed 1-hour bars of one coin (Decimal prices, ascending,
  UTC) and returns trade proposals for the signal times in `(context["since"], context["until"]]`.
  It must be pure: no network, no files, no clock, no randomness.
- `STRATEGY = sdk.mechanical_strategy(...)` names it (`MY_FIRST_V1`) and declares its variants.

Everything else is shared and identical in the history test, the shadow and paper trading: the
marketable entry right after the signal bar closes, `CRYPTO_TRADE_PLAN_V1`'s exits (a stop 2%
below the fill widened to two hourly ranges, a 1.5R target, 24 hours), fees on both legs, the
slippage model, sizing and the risk limits.

## 2. Get a skeleton

```
docker compose run --rm catalyst new-strategy MY_FIRST --dir /my-strategies
```

This writes `my-strategies/my_first_v1.py` on your machine (on Linux add
`--user "$(id -u):$(id -g)"` after `--rm`). It holds a working placeholder rule: an hourly close
above the prior 24 hours' high. It never overwrites a file and refuses a built-in's name.

## 3. Write the rule — and declare the variants before you test

Open `my-strategies/my_first_v1.py`. Change the rule in `signals`, and decide now which settings
you will compare:

```python
LOOKBACK_HOURS = 24
HISTORY_VARIANTS = (
    {"variant_id": "LOOKBACK_24", "lookback": 24},
    {"variant_id": "LOOKBACK_48", "lookback": 48},
    {"variant_id": "LOOKBACK_72", "lookback": 72},
    {"variant_id": "LOOKBACK_12", "lookback": 12},
)
DEFAULT_VARIANT_ID = "LOOKBACK_24"   # the rule you are registering


def signals(bars, context):
    lookback = int((context.get("parameters") or {}).get("lookback", LOOKBACK_HOURS))
    ...                                # use `lookback` instead of the constant


STRATEGY = sdk.mechanical_strategy(
    ...,
    history_variants=HISTORY_VARIANTS,
    history_hours=72 + 2,              # enough bars for the longest variant
)
```

Why up front: every variant is a *trial*. The history tester records each one in a trials ledger
and the deflated Sharpe ratio charges you for all of them. Adding variants after looking at
results is the classic way to fool yourself; the ledger makes it visible. Four or more variants
also let it compute the probability of backtest overfitting (PBO).

Put the numbers your rule looked at in `facts={...}` of `sdk.marketable_proposal`: they are
recorded with every signal. The three examples show an indicator (RSI in Decimal), a channel and
moving averages, each commented line by line.

## 4. History test (rung 1)

First a smoke run on generated sample bars — offline, a few seconds, never evidence:

```
docker compose run --rm catalyst history-test --source synthetic \
    --plugins-dir /my-strategies --strategy MY_FIRST_V1 \
    --start 2026-05-01 --end 2026-09-01 --is-days 30 --oos-days 15 \
    --out /data/history-tests/my-first-synthetic
```

Then the real test on public bars (keyless, cached in the `history` volume; a year of hourly and
minute bars for many coins takes a while the first time):

```
docker compose run --rm catalyst history-test --source alpaca \
    --plugins-dir /my-strategies --strategy MY_FIRST_V1 \
    --out /data/history-tests/my-first-v1
```

(default: the 33 Alpaca USD pairs, the 365 days ending two days ago, 60/30-day walk-forward
windows, Alpaca's 0.25% taker fee per leg; `--fees` and `--source coinbase` change them.)

Read `REPORT.md` top to bottom:

1. **Every variant (full range)** — signals, trades, mean gross R, the fee and slippage cost in R,
   **mean net R** with a 90% day-bootstrap interval, win rate, drawdown in R, Sharpe. A rule whose
   gross R does not clear about 0.3 R of costs per trade cannot work at these fees.
2. **Walk-forward (out-of-sample only)** — in each window the variant that did best *before* the
   window is traded *in* it. These concatenated out-of-sample trades are the honest estimate.
3. **Overfitting checks** — the **deflated Sharpe ratio** is the probability that the
   out-of-sample Sharpe is real once you account for how many variants were tried (≥ 0.95 is
   the bar). **PBO** is the probability that the variant that looks best in-sample is below the
   median out-of-sample (≤ 0.5 is the bar; needs four or more variants).
4. **Breakdowns** by window, month, prior-day market regime and coin — where the result comes
   from. A result carried by one coin or one week is not a strategy.
5. **Account simulation** — the same trades through the real sizing (0.5% risk per trade, 10%
   equity slices), the 2% crypto open-risk cap, three per sector, one per coin, the daily loss
   limit and entry pacing.

Rung 1 is met only when the walk-forward out-of-sample trades are **at least 30**, their **mean
net R and the lower end of its 90% interval are above zero**, the **deflated Sharpe ≥ 0.95** and
**PBO ≤ 0.5**. The report's "Promotion check" line says which rung you met.

What the examples teach on the sample bars (a random walk with regimes; the command above, May
to August 2026): the registered rules lose roughly their costs, −0.24 to −0.38 R per trade, with
fees and slippage near 0.3 R — on a market with no edge, costs are what you measure. Look at `EXAMPLE_RSI_REVERSION_V1`'s `RSI14_25_TREND72` variant: about +0.07 R, on
nine trades. That is what noise looks like; the interval, the trade minimum and the deflated
Sharpe are there to catch exactly that. A synthetic run's promotion check always says `NONE`.

## 5. Shadow (rung 2)

Your folder is already in the trader's plug-in path (`/my-strategies`). Restart it to load the
new file:

```
docker compose restart trader
docker compose logs -f trader      # SIMULATED_PASS ... "signals": n, "outcomes": m
```

Every hour the simulated venue runs each shadow strategy's `signals` on the last completed bars,
records each new signal once, and — 24 hours plus the entry window later — simulates its outcome
on 1-minute bars with the same code the history tester used. No order is ever placed.

Note: the first pass backfills the last 40 days, so the first cell overlaps your history test.
The shadow rung is about *new* signals: judge it on signals recorded after you added the
strategy, and on at least 30 of them.

## 6. Read the scorecard

```
docker compose run --rm catalyst stack scorecard
```

Your strategy's cell shows `signals`, `trades`, `outcome_counts` (`STOP`, `TARGET`,
`HOLD_24H_EXIT`), `mean_net_r`, `mean_net_r_after_slippage`, `win_rate` and its `status`
(`NOT_ENOUGH_DATA` until 30 trades, then `MEASURED`). Rung 2 needs at least 30 simulated trades
with a positive mean net R (after slippage). Shadow R is a 1-minute-bar approximation with an
assumed fee, never a fill.

## 7. Paper (rung 3) — the deployment owner's decision

Nothing promotes itself. With your own Alpaca **paper** account set up
([QUICKSTART.md](QUICKSTART.md), step 5), the owner of this deployment (you, locally) records a
promotion against a **real-bars** history report:

```
docker compose run --rm catalyst stack promote-strategy --strategy MY_FIRST_V1 \
    --history-report /data/history-tests/my-first-v1/results.json \
    --owner-ruling "my ruling 2026-10-10: MY_FIRST_V1 to paper"
```

It appends one audited `STRATEGY_PROMOTION` event and is **refused** unless the report and the
ledger's own shadow cell meet the ladder (`PROMOTION_LADDER_NOT_MET`, with the failing checks
printed). A synthetic report is always refused (`PROMOTION_HISTORY_REPORT_SYNTHETIC`), even with
an override. `--owner-override-reason "..."` exists for a deliberate exception and is recorded
for everyone to see.

Then list it for the engine in `.env.paper` and restart the profile:

```
MANAGED_STRATEGIES_JSON=["MY_FIRST_V1"]
```

Both are needed: a listed strategy without a promotion records nothing; a promoted one that is
not listed is never admitted. Each signal then takes the shared path — admission, the system
check, the trade plan, pacing, the daily limits, `JEV_MANAGED_RISK_V5`'s 0.5%-of-equity cap per
mechanical strategy and the one-use authorization of every order.

To stop it: `docker compose run --rm catalyst stack demote-strategy --strategy MY_FIRST_V1
--reason "..." --owner-ruling "..."`. Open positions keep their protection and exits.

## 8. Changing a rule

A changed rule is a new strategy: copy the file, call it `MY_FIRST_V2`, and climb the ladder
again. Never edit a promoted rule in place — trades keep the version they were admitted under,
and the evidence belongs to that version.
