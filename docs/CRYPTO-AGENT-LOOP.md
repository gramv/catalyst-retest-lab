# Crypto research-and-trading loop — plan

**PAPER TRADING — SIMULATED. Not real money.** Owner direction of 2026-09-26 (restated in
section 1). **Plan only: nothing here is built or activated yet.** This version replaces the
earlier drafts of the same day. Owner decisions that shaped it: trades are not limited to the few
coins with the tightest spreads; 24 hours is a continue-or-exit review, not a forced close; Jev's
selection comes before the system check; trades execute on Alpaca Paper; trade size fits the
$10,000 paper account.

## 1. What the owner asked for

1. Research agents study the crypto market — news, the web, technicals, prices across the whole
   market — and pick **20 coins for the day** from the coins Alpaca can trade. Claude does this
   until the app is deployed; afterwards Muse, Instinct and other agents do it.
2. For each coin the agent gives the current price and everything about the trade: entry, max
   entry, stop, target, reward-to-risk, and why it chose that coin and those prices.
3. **Jev** reads all 20 — the reasoning and every price — and picks the **top 5 to 10**.
4. The **system** then checks and recalculates everything independently and sets up the triggers
   on **Alpaca Paper**.
5. When a trade triggers, the **monitoring Jev** maintains it: trails the stop and the target
   while the trade goes our way.
6. After **24 hours** the agent and Jev decide together whether to **continue or exit**; a
   continued trade gets another 24 hours, and the cycle repeats.

## 2. The loop

```
 daily 08:00 ET ─ research run ──────────────────────────────────────────────────────────┐
 │ agent reads the research context (Alpaca's crypto list with live prices, open trades,  │
 │ recent outcomes) → researches → submits 20 picks: reasoning + current price, entry,    │
 │ max entry, stop, target, reward-to-risk                                                │
 └──────────────┬──────────────────────────────────────────────────────────────────────────┘
                ▼
 Jev selection: reads each pick exactly as the agent sent it → vetoes only the clearly wrong
 → scores and ranks the rest → top 10 (at least 5) go to the system check
                ▼
 system check: live Alpaca price vs agent's price · stop not already hit · reward-to-risk at
 max entry · stop distance · fees · tradable · capacity · size
 → a pick that fails is replaced by Jev's next-ranked pick (all 20 stay tracked)
                ▼
 setups on Alpaca Paper: triggers at the picks' prices until the next research run ·
 random 30% control group (fixed stop/target) / 70% maintained by Jev
                ▼
 trade maintenance (section 4.6): stop placed at fill → monitoring Jev every 15 min and on
 events (hold / raise stop / raise target / exit flag) → 24-hour review (agent's view → Jev →
 one discussion round if they differ) → continue for 24 h or exit
 stops, targets and the −3% daily loss halt close trades at any time
```

## 3. Entities

| Entity | Holds | Created by | States |
| --- | --- | --- | --- |
| Agent | id, name, token, guidelines version | owner | active / revoked |
| Research context | Alpaca's tradable crypto list with live price, spread and recent volume; open trades; pending reviews; recent outcomes | system, per run | — |
| Research run | agent, run time, context snapshot, picks, coins skipped with reasons | agent | submitted |
| Pick | coin; kind (news / chart / both); agent's current price and time; entry, max entry, stop, target; stated reward-to-risk; valid until; reasoning; sources; bars; confidence (never shown to Jev) | agent | submitted → reviewed → vetoed / ranked → selected / not selected → checked → live / rejected / replaced |
| Jev selection review | answers, vetoes, quality score and category, rank, receipt | Jev | valid / failed |
| Pick check | live price and deviation, reward-to-risk at max entry, stop distance, fees and spread in R, entry type, tradability, size, result | system | pass / reject with reason |
| Shortlist | the run's live picks, Jev ranks and replacements | system | live → superseded by the next run |
| Setup | pick, entry type, levels, window, size, arm | system | watching → triggered → open → closed; or expired / invalidated / cancelled / skipped |
| Order and fill (Alpaca Paper) | type, price, quantity, fee, time, one-use authorization | system | placed → filled / partly filled / cancelled / refused |
| Position | quantity, average entry, initial and current stop and target, opened at, next 24-hour review, continuation count, P&L, best and worst move | system | open → (exit review) → open / closing → closed |
| Maintenance review | trigger, dossier, options offered, Jev's answer, change applied or refused, receipt | Jev | applied / held / refused / flagged |
| Level change | old and new stop or target, option chosen, review, authorization | system | — |
| Exit flag | who raised it (agent or Jev), reasons, the other side's answer | agent / Jev | raised → agreed (exit) / not agreed (stay) |
| 24-hour review | trade state, agent's answer, Jev's answer, discussion round, decision, new stop and target | agent + Jev | requested → answered / timed out → agreed / discussed → continue / exit |
| Exit | reason (stop, target, raised stop, early exit, 24-hour exit, daily loss halt, operator), fills | system | — |
| Result | gross and net P&L (Alpaca's fees), R, best and worst move, hold time, continuations, agent, Jev rank, arm, and what the unchanged plan would have done | system | — |
| Audit event | every step above, hash-chained and append-only | database | — |

## 4. Logic

### 4.1 Research run
- Default: one run a day at 08:00 ET ("20 coins for that day"); a second run at 20:00 ET can be
  switched on. A run's picks can trigger until the next run's shortlist goes live.
- The agent first reads `GET /api/v1/lab/research-context`: the coins Alpaca can trade (USD
  pairs, no stablecoins — 33 on 2026-09-26) with live Alpaca prices, its open trades, pending
  24-hour reviews and recent outcomes. It may use anything in its research (news, other
  exchanges' prices and volume); its picks must be coins in that list.
- One report per run (`AGENT_RESEARCH_REPORT_V3`): 20 picks (up to 30) with the fields in
  section 3, plus the coins it looked at and skipped, each with a reason.

### 4.2 Jev selection (before the system check)
- Jev reviews each pick separately (its input limit, about 12 KB, is too small for all 20 at
  once). It reads the pick exactly as the agent sent it: the reasoning, sources and bars, and
  every price — the agent's current price, entry, max entry, stop, target and reward-to-risk.
  It never sees the agent's name or the agent's confidence.
- Fixed questions. News picks: is the news stale, already priced, contradicted; are the claims
  supported. Chart picks: do the bars support the levels, is the setup already broken, are the
  claims supported. For every pick: do the prices make sense together (entry, stop and target
  consistent with the reasoning and the stated reward-to-risk). Every pick gets a quality score.
- A pick is vetoed only for a definite "wrong" answer (claims unsupported, news stale or
  contradicted, levels unsupported, setup already broken, prices inconsistent). Uncertain answers
  only lower the score.
- Jev ranks every pick it did not veto. Ties: category, then earlier submission, then the
  agent's own rank. When several agents pick the same coin, the higher-scored pick wins.
- Jev's top 10 go to the system check.

### 4.3 System check (after Jev's selection)
Independently of the agent and Jev, for each selected pick in Jev's rank order:
- **Live price** from Alpaca: more than 5% away from the agent's price → rejected
  (`PRICE_MISMATCH`); the stop has already traded → rejected; a breakout whose price is already
  above the max entry → rejected (ran away).
- **Entry type** from the live price: entry below it = pullback (buy when price comes down to
  it); within 0.2% = immediate; above = breakout (buy when price rises through it).
- **Levels**: stop < entry ≤ max entry < target; reward-to-risk at max entry ≥ 2; stop at least
  2% below max entry; prices on Alpaca's price increment for the coin.
- **Costs**: Alpaca's fees and the spread in R, recorded with the pick.
- **Tradable**: coin tradable at Alpaca now; no open trade in the coin; sector cap; capacity;
  size (4.4).
- **Replacement**: a pick that fails is replaced by Jev's next-ranked pick that passes, so the
  run keeps up to 10 live picks — at least 5 whenever Jev ranked enough picks. Every rejection
  keeps its reason; all 20 picks stay tracked for measurement.

### 4.4 Setups, entries and size
- A setup watches its trigger until the next run's shortlist goes live, then expires if it has
  not triggered.
- Pullback and immediate entries trade now. Breakout entries trade once breakout support is
  switched on (owner's earlier choice: only after a minute-bar backtest shows an edge after
  fees); until then breakout picks are tracked, not traded.
- **Trigger** (crypto version for Alpaca's thin market): the price reaches the entry — a trade
  prints at or through it, or the ask comes down to it; the ask is at or below the max entry;
  the spread is at most 1%; the quote was read from Alpaca within the last 5 seconds (a quote
  that has not changed is still current). The order is a limit at the max entry, so a fill is
  never worse than that.
- **Size** (owner: fit the $10,000 account): each trade gets an equal slice of 10% of equity —
  about $1,000 — so up to 10 trades fit in the cash. A trade whose stop is far away is sized down
  so it never risks more than 0.5% of equity ($50). With the 2% minimum stop, a trade risks
  between $20 and $50. Total open crypto risk is capped at 5%.
- One open trade per coin; at most three open trades per sector (sectors from public category
  data: large-cap chains, DeFi, meme coins, infrastructure…); the daily loss halt applies.
- At admission 30% of setups are randomly put in the control group (section 4.6.6); the other
  70% are maintained by Jev.

### 4.5 Execution on Alpaca Paper
- Entry: a limit buy at the max entry (good until cancelled), under a one-use 5-second
  authorization — the path that ran the first real trade on 2026-09-25.
- Protection: a stop-limit rests at Alpaca at the stop; the target is watched by the app, which
  sells when the bid reaches it. If the stop-limit does not fill in a fast drop, the app cancels
  it and sells at market after 2 seconds (proven on 2026-09-25).
- Fees: Alpaca charges them (0.15% maker, 0.25% taker; the buy fee comes out of the coins
  received) and the system reads them from Alpaca's fee records into every result.
- Every order, change and cancel needs its own one-use authorization; there is no live-trading
  path.

### 4.6 Trade maintenance

#### 4.6.1 Opening and protection
- The moment the entry fills, the stop-limit is placed at Alpaca and the target is armed in the
  app, each change under its own authorization. A partial fill is protected at once for the filled
  quantity; the rest of the entry order stays for up to 10 minutes, or until price leaves the
  entry zone (above the max entry or at the stop), and is then cancelled.
- Recorded at open: average entry, initial stop and target, initial risk per coin (max entry −
  stop), planned reward-to-risk, and the first 24-hour review time.

#### 4.6.2 Monitoring reviews
**When.** Every 15 minutes (each completed 15-minute bar), and at once when:
- price comes within 0.5% of the target — raise the target or let it be hit?
- price reaches +1R, +2R, +3R… above entry for the first time — lock in some of the gain?
- price comes within 0.5% of the stop — is the reason for the trade gone?
- an agent posts news about the coin;
- Bitcoin moves 3% within 15 minutes — every open trade is reviewed, nearest to its stop first.

Never more than once a minute per trade, except near the target.

**What Jev reads** (the maintenance dossier, within Jev's input limit):
- the original pick: the agent's reasoning and prices, and Jev's own selection answers;
- the trade now: entry, current price, P&L in R, time in trade, best and worst move so far, every
  stop and target change and why;
- price action: recent 15-minute and 1-hour bars, and the coin's move against Bitcoin;
- news about the coin since entry;
- the options the code computed.

**Options** (computed by code; Jev chooses, it never invents a price):
- Stop: keep; the entry price (breakeven), offered once price is at least 1R above entry; recent
  swing lows on 15-minute and 1-hour bars that are above the current stop and at least 1% below
  the current price. At most five.
- Target: keep; the next resistance levels above the current target — recent swing highs on
  1-hour and 4-hour bars, the 24-hour high and the 7-day high. At most five.

**Jev's answer**: hold; raise the stop to an option; raise the target to an option; both; or
flag the trade for an early-exit review (4.6.3).

**Checks before applying** (code): the new stop is above the old stop and at least 0.5% below
the current bid; the new target is above the current price and the old target; the answer is
less than 60 seconds old and price has not crossed the new level meanwhile. Anything else is
refused and logged, and the trade keeps its levels.

**Applying at Alpaca**: raising the target is instant (the app watches it). Raising the stop
replaces the resting stop-limit — cancel, then place the new one — during which the app watches
price every second and sells at market if the new stop is crossed. Alpaca's order-replace will
be tested on paper to remove that moment.

**Target reached**: the whole position is sold (no partial sales). The near-target review is
Jev's chance to raise the target first.

#### 4.6.3 Early exit (between 24-hour reviews)
- Either side can start one: the agent posts an exit flag with reasons and news, or Jev flags the
  reason for the trade as broken in a monitoring review.
- The other side is asked at once and has 15 minutes. Both say exit → sell at market. Otherwise
  the trade stays with its stop and target; the flag and both answers are recorded.
- No answer from the other side in time → the trade stays (an early exit needs both).

#### 4.6.4 24-hour review
T is 24 hours after entry, then every 24 hours after a continue.
1. **T − 30 min**: the system sends the review request to the agent that proposed the trade (or
   the active research agent), with the full trade state and all news since entry.
2. **By T**: the agent answers continue or exit, with reasons: what changed, what it expects in
   the next 24 hours, and what would prove it wrong. For continue it may suggest a stop and a
   target; Jev chooses from the code's options.
3. **At T**: Jev reads the original pick, the trade state and the agent's answer, and answers:
   is the reason for the trade intact, weakened or broken; does the agent's case hold; continue
   or exit; if continue, which stop and target options.
4. **Agreement** → done. **Disagreement** → one discussion round: the agent sees Jev's reasons
   and answers once more within 15 minutes, then Jev answers finally. Still disagreeing → exit
   (a disputed trade does not get another day).
5. **Timeouts**: no agent answer by T → Jev decides alone. Jev unable to answer within 30
   minutes → exit.

**Continue** means a new 24-hour plan: the stop stays or moves up (never down), the target stays
or moves up, the next review is due 24 hours later and the continuation count goes up by one.
There is no limit on continuations unless the owner sets one. There is no midnight close.
**Exit** means sell at market with reason "24-hour review exit"; both answers are stored with the
result.

#### 4.6.5 Hard exits (no discussion, any time)
Stop hit (or the stop-limit fallback); target hit; −3% daily loss halt; operator flatten; coin no
longer tradable at Alpaca (sell at the first chance).

#### 4.6.6 Control group (30%)
Original stop and target only: no monitoring changes, no early exits, exit at 24 hours if the
stop or target has not closed it first. This is the baseline that shows whether Jev's
maintenance adds money or costs money.

#### 4.6.7 Maintenance edge cases
- Price gaps through the stop during a Jev review → the stop fires; the review is discarded.
- Jev picks a level price has already crossed → refused; next review.
- Many trades need review at once (market shock) → reviewed in parallel, nearest to the stop
  first.
- The proposing agent is gone → the active research agent answers; if none, Jev decides alone.
- Alpaca refuses an exit order → retried with a fresh order and growing waits, and an alarm
  after repeated refusals (phase 0 fix).
- Jev unavailable during monitoring → trades keep their stops and targets; breaker recovery
  retries; alert.

#### 4.6.8 Measuring maintenance
For every stop or target change, every early exit and every continue-or-exit decision, the
system also computes what the unchanged plan would have done on the same prices, so each kind of
decision is measured in R. Managed vs control compares the whole approach.

### 4.7 Risk and safety
Daily loss halt at −3% of start-of-day equity ($300 on $10,000): cancel pending setups and close
everything. Unchanged: paper only, no live-trading path, one-use authorizations, append-only
hash-chained audit, one executor, fixtures only in disposable databases.

### 4.8 Measurement
- Every pick of every run is tracked on live Alpaca prices — selected, not selected, rejected by
  the check — so Jev's picks can be compared with the ones it passed on.
- Every trade is reported net of Alpaca's fees in R (official R: filled quantity × (max entry −
  initial stop)), by agent, Jev rank, pick kind, managed vs control, and continue vs exit
  decisions.

## 5. Real-market situations

| Situation | What the system does |
| --- | --- |
| Agent's current price is stale or invented | Jev sees the agent's price; the system check after selection compares it with Alpaca's live price: more than 5% off → rejected and replaced by Jev's next pick |
| Price hits the stop before the entry triggers | Setup invalidated; nothing bought |
| Price runs away above the max entry | No chase; setup waits and expires at the next run |
| Price never reaches the entry | Expires at the next run; counted in the agent's record |
| Fast drop through the stop | Stop-limit may not fill; the app sells at market after 2 seconds; the loss can exceed 1R and is recorded |
| Entry triggers, then instantly reverses | Normal stop-out |
| Only part of the order fills | Protect the filled part at once; cancel the rest after 10 minutes |
| Wide spread on a quiet coin | Trigger needs the spread within 1% and the ask at or below the max entry; the limit caps the price |
| Big move in our favour | Milestone review: Jev may raise the stop (breakeven or a swing low) and the target |
| Target near while momentum continues | Near-target review: Jev may raise the target (and the stop) first; otherwise sold at target |
| Breaking bad news on an open coin | Agent flags it → Jev reviews at once → both agree → exit |
| Agent and Jev disagree on an early exit | Trade stays with its stop and target; both views recorded |
| Whole market dumps together | Every trade reviewed, nearest to stop first; stops fire; the −3% halt closes all |
| Agent and Jev disagree at 24 hours | One discussion round; still disagreeing → exit |
| Agent silent at a 24-hour review | Jev decides alone |
| Jev down at a 24-hour review | Exit after 30 minutes |
| Alpaca stream drops or the app restarts | Resume from the ledger; fetch fills missed while down; resting stop-limits stayed at Alpaca; pending setups resume only if price did not reach the entry meanwhile |
| Alpaca refuses an order | Entry: setup ends with the reason; exit: retried with growing waits, alarm after repeats |
| Coin delisted at Alpaca | No new setups; an open trade is sold at the first chance |
| Jev unavailable | Breaker recovery retries; trades keep their stops and targets; no new shortlist until Jev is back; alert |
| Research agent late or missing | Earlier setups expire on schedule; open trades still maintained; alert |
| Too few good picks | Only picks Jev did not veto and that pass the check go live |
| Same coin picked again while its trade is open | New pick skipped; the 24-hour review decides the open trade |
| Several agents pick the same coin | Higher Jev score wins; the other is tracked |
| No cash or risk room when a trigger fires | Trigger skipped and recorded (`CAPACITY_FULL`) |
| Mac asleep or rebooted (until deployment) | Supervisor restarts; restart handling as above; alert |

## 6. What already exists

| Piece | Status |
| --- | --- |
| Alpaca Paper crypto execution: limit entry, resting stop-limit, app-watched target, market-exit fallback | exists, proven on 2026-09-25 |
| Hash-chained audit, one-use 5-second authorization gate, risk policy table, operator controls, backups | exists |
| Agent intake, per-agent tokens, blind Jev review with reasoning, receipts | exists; report V3 and the research context are new |
| Jev question infrastructure, quality score, selection rules | exists; the top 5–10 rule and the news/chart question sets are new |
| Monitoring review with code-computed stop and target options | exists; cadence, events, breakeven option and exit flags are new |
| Crypto trigger, size and holding versions for this plan | new (named versions, 4.4 and 4.6.4) |
| 24-hour review with agent, Jev and a discussion round | new |
| Results net of Alpaca's fees by agent, rank and arm, with unchanged-plan comparisons | new |

## 7. Build order

| Phase | Delivers | Proven by |
| --- | --- | --- |
| 0 | Fixes from the 2026-09-26 review: Jev breaker recovery, lease-loss restart, retries for refused exits, halt and breaker alarms, Alpaca fees in results | fixture tests |
| 1 | Research context API (Alpaca crypto list, live prices, open trades), report V3 | fixture tests; live read test |
| 2 | Jev top 5–10 selection with the news/chart question sets | fixture run; one real-Jev run of 20 picks |
| 3 | System check after selection, replacements, windows, supersession | fixture tests |
| 4 | Crypto versions: trigger, size (10% slices, 0.5% cap, 5% total), sector caps, 24-hour holding | fixture tests; supervised paper trade |
| 5 | Trade maintenance: monitoring reviews, options, exit flags, stop replacement at Alpaca | fixture and real-Jev session |
| 6 | 24-hour review with the discussion round | fixture and real session |
| 7 | Results net of fees, unchanged-plan comparisons, dashboard | fixture and first real week |
| 8 | Claude's scheduled research runs, supervisor, alerts, restart tests, 72-hour soak | soak report |
| 9 | Minute-bar backtests (pullback, breakout); first supervised week; then unattended | study and week report |

## 8. Decisions

**Decided by the owner (2026-09-26):**
- **D1 — execution:** Alpaca Paper (the existing API).
- **D2 — size:** fit the $10,000 paper account: 10% slices (about $1,000 a trade), at most 0.5%
  risk per trade, at most 5% total open crypto risk.

**Rule versions this needs** (under the owner's 2026-09-24 ruling that rules change as named
versions; V1 and earlier versions stay unchanged): crypto trigger for Alpaca's thin market (1%
spread cap, quote freshness by read time, ask touch); crypto size (10% slices, 0.5% cap, 5% crypto
total); 24-hour continue-or-exit holding with no midnight close; top 5–10 selection with news and
chart question sets.

**Defaults in this plan (say if any should change):**
- Research: daily run at 08:00 ET; 20 picks from Alpaca's crypto list.
- Selection: Jev's top 10 (at least 5 when enough are ranked); replacements from Jev's ranking.
- Maintenance: reviews every 15 minutes plus the events in 4.6.2; breakeven offered after +1R;
  new stops at least 0.5% below the bid; no partial sales; early exits need both agent and Jev.
- 24-hour review: agent asked 30 minutes ahead; one discussion round; disputed → exit; Jev down
  for 30 minutes → exit; no continuation limit.
- Control group: 30% (earlier owner choice), exits at 24 hours.
- Other: at most three open trades per sector; long only; breakouts after the backtest (earlier
  owner choice); −3% daily loss halt (existing rule).
