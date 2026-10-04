# Learning loop — plan (2026-09-28, awaiting owner approval)

**Paper trading only.** Owner, 2026-09-28: "can we think of learning loop as well everyday
based on results … plan it". This plan adds a daily loop that learns from results, and a
weekly loop that turns strong evidence into rule proposals. Nothing in it places an order,
changes a live setting on its own, or edits a recorded decision.

## 1. The idea in one paragraph

Every night the system writes a **scorecard** of the day: what was picked, what Jev chose, what
filled, what each trade earned after fees, and whether Jev's choices and minute-by-minute moves
beat the alternatives it did not take. Every morning **Muse reads its own lessons** before
researching and adjusts *how* it researches, never *what* it covers. Every week a **review**
checks the accumulated evidence against thresholds fixed in advance; only when a threshold is
met does it propose a **rule change, as a named version, for the owner to approve**.

## 2. Guardrails (fixed before any data is seen)

1. **Daily changes are research-side only.** Muse may change its own emphasis every day: which
   setups it ranks first, preferred distance to entry, timeframes, sources. Trading rules do not
   change daily. These rules include Jev's questions and thresholds, the system check, sizing,
   stops, the maintenance cadence and the 24-hour review.
2. **Rule changes only as named versions** in `docs/REFERENCE-RULES.md`, and only after
   the owner approves. Trades keep the version they were admitted under, so every comparison
   stays clean.
3. **The baselines stay.** The randomized 30% control arm (fixed stop and target, no Jev) and
   the shadow outcome of every pick Jev passed on are never removed. They are what "better"
   is measured against.
4. **Minimum evidence before any rule proposal** (section 6). With a few trades a day, one day
   is mostly luck, and chasing it would erase what the experiment measures.
5. **No narrowing of scope.** Lessons change emphasis and ranking; Muse still covers the whole
   crypto universe with 20+ picks per run (owner, 2026-09-26).
6. **Every lesson Muse applies is written down**, in its run notes and, when its method changes,
   as a new `agent_version`. Every result can be traced to the research approach that produced it.

## 3. What already exists, and the gaps

| Piece | Status |
| --- | --- |
| Per-trade official R after verified fees (`managed_measurement`, package fees-net-r) | Exists |
| `PICK_SHADOW_OUTCOME_V1`: what every pick would have done, selected or not (`pick_outcomes.py`) | Exists as a job; **not scheduled in the cloud** |
| `UNCHANGED_PLAN_REPLAY_V1`: what each trade would have done without Jev's stop/target changes (`unchanged_plan.py`) | Exists (computed on read) |
| Arm, selection and agent aggregates; research funnel; daily roll-ups (`managed_analytics`, `/api/v1/lab/results*`, `/analytics/*`) | Exist; **status/operator tokens only** (Muse gets 403) |
| Muse's own view: `recent_outcomes` in the research context (7 days of closed trades, last run's per-pick status) | Exists; **no lessons, no shadow or replay results** |
| Jev's per-pick answers, confidences, vetoes, maintenance decisions | In the ledger |
| A daily scorecard, Muse's lessons, a weekly review, proposal format | **Missing** (this plan) |

## 4. Daily scorecard — `DAILY_SCORECARD_V1` (a measurement definition, no trading rule)

Computed once a night for the New York day just ended (at 00:30 America/New_York), and on
demand. It covers that day, the last 7 days and the last 30 days, with counts beside every rate:

1. **Funnel:** picks sent → accepted → ranked or vetoed → selected → admitted by the system
   check → entry triggered → filled → closed. Each drop gets its reason: veto code, system-check
   code, expiry without a trigger.
2. **Outcomes:** per trade, R after fees, win or loss, exit reason (target, stop, 24-hour review,
   early exit, time), hold time, best and worst excursion.
3. **Jev's selection value:** selected versus passed picks on their shadow outcomes (mean net R,
   hit rate); how vetoed picks would have done.
4. **Jev's maintenance value:** each stop or target change and each 24-hour decision against the
   unchanged-plan replay, by decision type.
5. **Arms:** Jev-managed versus control, actual net R.
6. **Research craft, for Muse:** fill rate and shadow R by distance from price to entry
   (0–1%, 1–2%, 2–3%, 3%+), by timeframe (1h/2h/4h/6h/1d), by rule A/B, by pick kind
   (CHART/NEWS/BOTH), by sector; excerpt drop rate, stale-news vetoes, dossier-size rejections.
7. **Costs:** Jev spend (`JEV_SPEND_METER_V1`), trading fees, Railway usage.

Stored as one immutable `DAILY_SCORECARD` event per day, so it is audited, restart-safe and
never rewritten.

## 5. Muse's morning lessons — research side

- **Research context V2** adds a `lessons` section: this agent's own scorecard lines (1, 2, 3
  and 6 above; per agent, sanitized, no other agent's data). Muse already reads the context at
  the start of every run, so no new route or credential is needed. The results routes stay
  closed to agent tokens.
- **`MUSE_RESEARCH_GUIDELINES_V6`** (the app serves the version and its SHA-256; Muse must
  declare it) adds how to use lessons:
  - adjust emphasis, never coverage;
  - state in `why_over_peers` when a lesson drove a choice;
  - record the lessons applied in the run notes;
  - bump `agent_version` when the method changes.
- **Research kit** (`research_agent/`) gains a `lessons` step between `context` and `build`. It
  prints the lessons and a suggested emphasis (for example, "entries more than 3% from price
  filled 0 of 9 times in 7 days: rank closer setups first"), and `build` orders picks by it. The
  real Muse applies the same guidelines with its own code.

Example of a lesson and what it may change:
- **Allowed:** "4-hour setups reached target 4 of 6 times, daily setups 0 of 5 → prefer 4-hour
  windows when both qualify."
- **Not allowed:** "drop daily setups entirely" (narrows scope) or "raise the stop buffer"
  (a trading rule).

## 5b. Web research inside the loop (Muse's side, same citation rules as its research)

Numbers alone can teach the wrong lesson. A stop-out caused by an exchange hack is not
evidence against the setup, and a win carried by a market-wide rally is not evidence for it.
So Muse also learns from the web in three places. It uses the same rules as its daily research:
- exact excerpts cut from the page;
- publish times from the page's own metadata, never invented;
- every excerpt re-verified by the kit.

1. **Post-mortems on notable trades.** These are every stop-out, every win above 1.5R, every
   early exit and every 24-hour exit. Muse searches the news in the trade's window, from entry to
   exit, and records a cited cause:
   - `COIN_NEWS`: a hack, a listing, an unlock, a lawsuit;
   - `MARKET_WIDE`: a Bitcoin or macro move, regulation, an exchange outage;
   - `NO_NEWS_FOUND`: the setup itself failed or worked.

   The scorecard then separates "the setup was wrong" from "the market moved", so craft lessons
   come only from the right cases.
2. **A daily market-regime tag.** Each New York day gets a short cited market summary: Bitcoin's
   and Ether's moves, and major crypto or macro events such as an FOMC decision, ETF flows or an
   outage. Lessons are compared within regimes, for example "pullback setups filled and worked on
   up-trend days, and failed on risk-off days", instead of mixing them.
3. **Checking Jev's news calls.** When Jev vetoes a pick as old news, or a news pick
   underperforms, Muse looks up when the development was really first made public. It records
   whether its own dating was wrong. This is a research-craft lesson, such as "this outlet
   republishes old news with new dates".

Web findings are **explanations, never rule evidence**. They decide which trades a craft lesson
may count, but a rule proposal (section 6) still needs its numeric minimum sample. The app, Jev
and the trading rules never browse the web: Jev sees only the dossier Muse sends, so the
trading path stays deterministic and auditable. The cost is research time only, with no Jev
calls.

## 5c. Daily market review: what moved, why, and what we missed (owner, 2026-09-28)

The owner asked for this: "every day … what coins moved and why and what did we miss …
identify the patterns … the factors that are moving the market … test data … and reality …
adjust its next day's research plan".

The sections above study only our own picks. This studies **the whole market every day**,
including the moves we did not catch. It works as a daily forecast test.

**Morning, the test (written before the day starts).** During the 08:00 research run, Muse
records a short **outlook for every coin in the universe**, not only its picks:
- expected direction over the next 24 hours (up, down or flat) and a confidence;
- the reasons, as short cited items: news, scheduled events (unlocks, upgrades, listings,
  macro), technical state (near support or resistance, range squeeze, trend, volume);
- a short **market outlook**: Bitcoin and Ether, sectors (L1s, DeFi, memes, AI), and macro,
  including the stock market's influence on crypto (Nasdaq and S&P futures, crypto stocks such
  as COIN and MSTR).

The outlook is stored immutably, with its time, through the agent's own route
(`MARKET_OUTLOOK_V1`), so it can never be edited after the fact.

**Night, the reality.** The nightly job measures what actually happened for every coin:
- 24-hour return, largest move, volume change;
- the day's **top movers**: the 5 biggest risers, the 5 biggest fallers, and any move over 5%;
- where the move started.

It then compares each outlook with reality:
- direction hit rate;
- whether confidence was justified (calibration);
- **misses**: big moves with no expectation;
- **false alarms**: expected moves that did not happen.

**Why it moved (web research, Muse's side).** For each top mover and each miss, Muse searches
the news and records the cause with the usual citation rules. It also records **whether that
cause was public before the move started** (publish time against the bar where the move
began):
- `KNOWABLE_AND_MISSED`: the lesson is to research differently;
- `KNOWABLE_NOT_ACTIONABLE`: for example, the news came out after the move began;
- `SURPRISE`: nothing was knowable;
- `NO_NEWS`: the move came from technicals, flows or Bitcoin.

Each mover also gets its **pre-move technical state**: breakout from a range, bounce from
support, volume spike, trend, distance from recent highs or lows. This is what separates
learning from hindsight: only information that was public before the move counts as a signal.

**Factors of the day.** A daily factor table records Bitcoin's and Ether's moves, sector moves,
Bitcoin's share of the market, total volume, funding or open interest where a free public
source exists, major macro and crypto events, and the stock market. Over weeks this shows what
actually drives the coins we trade, for example "most daily moves follow Bitcoin, except
listings and unlocks".

**Patterns, and the next day's research plan.**
- Each night Muse updates a **research checklist**: which technicals and fundamentals to check
  first, which news types and sources matter, which scheduled events to watch.
- A pattern enters the checklist only after it has repeated, for example at least 10 times
  with a hit rate that holds.
- Each checklist item keeps its running hit rate and is retired when it stops working.
- Checklist changes are research-side, so they can apply the next morning. They never narrow
  coverage.

**When a miss points at a rule.** Some misses cannot be fixed by researching better, because
of a trading rule. For example, if most big moves are breakouts, and breakouts are disabled
until backtested (docs/CRYPTO-AGENT-LOOP.md 4.4), the miss count is evidence for the weekly
review. A rule change still needs a backtest, the minimum sample and the owner's approval.

**Stocks.** For now the review covers the crypto we trade, plus the stock-market factors that
move crypto. A full daily stocks review, what moved and why, can be added as research-only, or
when stock trading is turned on.

## 6. Weekly review — `WEEKLY_REVIEW_V1` (owner decides)

Every Monday at 00:30 New York, and on demand. It aggregates the week's scorecards and runs
tests that are fixed now, each with a minimum sample. Below the minimum, the verdict is **"not
enough data"**, never a guess:

| Question | Compares | Minimum sample |
| --- | --- | --- |
| Does Jev's selection add value? | Selected vs passed picks, shadow net R | 40 selected and 40 passed picks with complete shadow outcomes |
| Does Jev's management add value? | Jev-managed vs control arm, actual net R | 30 closed trades per arm |
| Do Jev's stop and target moves help? | Actual vs unchanged-plan replay, per decision type | 30 changes of that type |
| Is a 24-hour decision rule working? | Continue vs exit decisions against their replays | 20 decisions |
| Are vetoes right? | Vetoed picks' shadow net R vs passed picks' | 15 vetoed picks |

Each test reports the effect, a 90% bootstrap interval, the sample size and one verdict:
- **keep**;
- **propose named version X**, with the exact contract text;
- **not enough data**.

A proposal becomes a normal package once the owner approves it in chat: tests, a
`CONTRACT-RESOLUTIONS` entry, a deploy. Research-craft findings go straight back to section 5,
since they need no approval.

## 7. Where it runs (cloud)

- **A Railway cron service `jobs`** (same image and entrypoint, `cloud_entry jobs`), scheduled at
  04:30 UTC (00:30 New York). It runs the shadow outcome job, the unchanged-plan replay, the
  scorecard and, on Mondays, the weekly review, then exits.
  - It uses the trader's `catalyst_risk` connection by reference: no new secret and **no
    migration**, which matters because the cloud ledger has no migration path yet.
  - It is given no Alpaca or TypeSafe key and never touches the broker. Alpaca's public bars
    need no key.
  - Cost: a few minutes of compute a day, under $0.10 a month.
- **Why not inside the trader:** the trader is the only executor. Keeping batch work out of it
  keeps its loops and lease simple.
- **Owner delivery:**
  - each morning, a short private summary: yesterday's funnel and results, and what Muse will
    emphasize today;
  - each Monday, the review with any proposals.

  The public dashboard stays as simple as it is (owner, 2026-09-27).

## 8. Phases

| Phase | Work | Effort |
| --- | --- | --- |
| **1** | `scorecard.py` and `DAILY_SCORECARD_V1` with tests; the `jobs` cron service (shadow job, replay, scorecard); private morning summary | about half a day |
| **2** | Research context V2 `lessons`; `MUSE_RESEARCH_GUIDELINES_V6` (lessons, post-mortems, regime tag, news-date checks); the kit's `lessons` and `postmortem` steps | most of a day |
| **3** | `WEEKLY_REVIEW_V1` with the fixed tests and the proposal template | a few hours |
| **4** | Daily market review (5c): the `MARKET_OUTLOOK_V1` route and its immutable record; the nightly reality measurement over the whole universe (movers, misses, calibration, factor table); Muse's why-it-moved step and research checklist in the guidelines and the kit | about a day |

- The first scorecard is useful after the first closed trades, on day 1–2.
- The first research lessons arrive from day 2.
- The first weekly review runs on day 7. It will likely say "not enough data" for rule
  questions while already showing research-craft lessons.

## 9. Decisions for the owner

1. **Run the jobs as a Railway cron service** (recommended) or inside the trader.
2. **The minimum samples in section 6**: accept them or change them. Once data exists they
   should not move.
3. **How the morning summary reaches you:** a chat message, a private page, or email through the
   notifier (not configured yet).
4. **Muse's access:** its own sanitized lessons only (recommended), or the full results routes.

## 10. Related open items

- **Storage** (package jev-budget finding): each maintenance review adds about 65–75 KB to the
  ledger, so the 5 GB Hobby volume fills in roughly 9–11 days at five trades reviewed every
  minute. The learning loop adds little, but the ledger needs a decision soon: a smaller
  per-review footprint, a lower cadence, or a larger plan.
- **Watchdog:** a status read that lands inside the half-second reconciliation pass shows
  `RECONCILIATION_STALE`. This is a false alarm to fix with the next trader deploy.

## 11. Two speeds (owner, 2026-10-03; package learning-loop2)

The owner asked for overall learning — how the market did, what moved, why, what we missed —
and to adjust the strategy, with one correction: **two speeds**.

- **Daily: observe and explain.** `DAILY_BRIEF_V1` (nightly, `daily_brief`): the market in words
  (regime, BTC/ETH, breadth, sell-off hours), the movers and their sector clusters, why they
  moved (only from accepted post-mortems; otherwise queued for an agent), what we missed and
  whether it was knowable, whether our mechanical plug-ins would have traded each top mover and
  at what net R after fees (`MISSED_TRADEABLE_V1`, final once the holds have passed), how our
  picks and trades did against the market, and tomorrow's research focus. It may change only
  research attention; agents read its sanitized view and a 7-day post-mortem queue in their
  lessons, and every 2-hourly `update` reads the lessons (`--lessons`).
- **Weekly: decide, propose only.** `WEEKLY_REVIEW_V1` gains `learning`
  (`LEARNING_LOOP_WEEKLY_V1`): patterns at 10+ occurrences, strategy fit by regime (minimum 30
  per cell), shadow vs live, the Jev calibration summary, missed-tradeable totals by regime,
  management value by stop distance, time in trade and regime, after-exit paths, and
  `PROPOSED_NOT_APPLIED` items. A proposal still needs a history test, a named version in
  `docs/REFERENCE-RULES.md` and the owner's yes.

Also delivered: lesson hints by net R per resolved pick (L2), after-exit paths per trade (L4),
the management split (L6), the cloud/agent division of the post-mortem work (L7) and analytics
on the plan's traded levels.
