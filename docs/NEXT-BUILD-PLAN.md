# Next build plan (after the learning loop) — noted 2026-09-28

**Paper trading only.** Owner, 2026-09-28: "note the points we will add it later … keep it in
next build plan". These come **after** the learning loop (`docs/LEARNING-LOOP-PLAN.md`) and
after a week or two of live data. Each change lands as its own named version and is measured
against the version before it; none changes a recorded trade.

## Found on the first live day (2026-09-28): status

| Finding | Status |
| --- | --- |
| A late print above the entry cancels a valid setup | **Fixed**, `STALE_PRINT_ABOVE_TRIGGER_V1` (e3becf1, 13:33 UTC). Applies from the 2026-09-29 run. |
| The app waits 1–3 s before it checks a late print | **Fixed**: reused connections and a print that wakes the pass (f8705e1, 15:20 UTC). The pass cycle went from 2.65 s to 1.30 s. |
| Jev maintenance reviews time out at 3 s | **Fixed**: a transport failure is retried once (e806163, 15:43 UTC), and `JEV_LIVE_REVIEW_POLICY_V2` (8 s per attempt) has been live since 20:57 UTC: migration 025 was run with the owner's authorization at 20:52 UTC, release d60963e, variable bb0e6f9. Provider-side bursts of 1–3 minutes remain; the next burst shows whether 8 s answers them (none in 18:57–21:10 UTC, 131 calls, 0 failures). |
| A stop replacement cancels its own new stop-limit | **Fixed**: an order being replaced is not a second reservation (a93827a, 16:09 UTC). |
| The dashboard's "Paper account $0.00" | Page-level fix in progress (dashboard polish). The view fix needs a migration. |
| The protection-plan record stays REPLACING after a replacement | Open (code-only fix). |
| A rejected replacement takes the PROTECTION_REJECTED exit while the old stop still protects | Open (noted in CONTRACT-RESOLUTIONS). |
| Both first trades closed through `STOP_LIMIT_NOT_FILLED`, not the native stop | Open, a strategy question for the owner (a named version). The app marks the stop breached when a fresh **bid** reaches it and sells at market 5 s later if the native stop-limit has not filled. Alpaca's stop-limit triggers on **trades**, and on these thin pairs the bid touched the stop without a triggering trade. LTC sold at 68.941 against a stop of 68.681 (15:16 UTC); UNI sold at 8.8202 against 8.8164 (17:09 UTC). Both were near break-even after fees. Consider requiring a trade print, or a bid that stays through the stop. |
| **No guarded cloud migration command** (RAILWAY-DEPLOYMENT.md) | **Built and used once** (package cloud-migrate): `cloud_provision migrate` as the provision service with a verified ops backup named first (RAILWAY-DEPLOYMENT.md 7.8). Its first use was migration 025 (Jev V2), 2026-09-28 20:52 UTC, under the owner's one-time authorization. The equity view and later migrations each need their own. |

The findings below are the record as found.

**The app itself waits 1–3 s before it checks a late print** (measured 2026-09-28, 13:50 UTC).

*What happened.* Every print the runtime writes on its own is evaluated by the protection pass
(`execution_tick_seconds` = 1). That pass first runs the account-safety tick, market-gap checks,
research supersession, gap-resume checks and admission, and only then the queued prints; it
then sleeps a second. In the 12 hours to 13:50 UTC, the 12 individually written prints waited
0.39–3.12 s in the app (mean 1.76 s) between being written and being consumed.
- Five setups were revoked `DATA_FEED_FAILURE` that day: XRP, DOGE, DOT, RENDER and ADA. For
  DOGE, DOT and ADA the feed alone delivered the print in 3.5–3.8 s, under the 5 s limit; the
  app's own wait pushed it over. Only XRP (5.27 s) and RENDER (5.38 s) were over the limit on
  arrival.
- DOT's second late print, queued before the first was evaluated, appended a second `REVOKE`
  for the already-revoked setup (12:10:51). Harmless, but check it in the same change.
- It still matters under `STALE_PRINT_ABOVE_TRIGGER_V1`: a late print **at or below** the entry
  trigger (the buy moment itself) still revokes the setup, by design. On these thin pairs, 11 of
  the 204 prints for watched setups in those 12 hours (5.4%) arrived more than 2 s late.
- ADA (13:41 UTC) and RENDER (13:36 UTC), admitted before the version, were revoked this way
  after it was deployed.

*Cause, found the same day.* `Repository.connect()` opened a new TLS connection for every use:
22.5 ms each on Railway, and 32 per pass with five watched setups. The rest of the pass is
small.

*Built on `pkg/2026-09-28-pass-speed`, not deployed (awaiting the owner).* No trading rule
changes; the 5 s rule and the pass order stay.
- The trader's repository reuses connections (locally 32 → 0 new connections per pass, 76 →
  11 ms).
- A queued print wakes the protection pass.

See PHASES, 2026-09-28, "Pass speed".

**Built and deployed (e3becf1, 2026-09-28 13:33 UTC): `STALE_PRINT_ABOVE_TRIGGER_V1`.** It
applies to setups admitted from the 2026-09-29 08:00 run on; the rest of this entry is the
original finding.

**A late price print above the entry cancels a valid setup.**

*What happened.* A WATCHING setup's market print is written and evaluated on its own when it
arrives more than `QUIET_PRINT_MAX_AGE_SECONDS` (2 s) after the trade, when it has no quote, or
when it arrives while entries are blocked. It is then revoked `DATA_FEED_FAILURE` when its age
exceeds 5 s by evaluation time (`managed_runtime._process_print`, `PRINT_PROCESSING_DEADLINE`).

This happens even when the print is **above the entry trigger**, where it can neither trigger nor
invalidate the setup. On Railway, Alpaca's crypto trade feed delivered the individually recorded
prints 3.4 s after the trade at the median, and 5.3 s at worst (6 prints in 6 hours).
- XRP (run 2) was revoked this way at 11:16 UTC. Its print was at 1.488, and its entry trigger
  1.4513.

*Proposal: a named version, e.g. `STALE_PRINT_ABOVE_TRIGGER_V1`.*
- A stale print strictly above the entry trigger is consumed as harmless, and the setup keeps
  WATCHING.
- A stale print at or below the trigger (or the stop) still revokes, exactly as now, so the
  fail-closed rule stays where it protects something.
- The 5-second freshness rule for prints that can trigger is unchanged.
- No trade path changes.

**Jev maintenance reviews time out at 3 s** (found 2026-09-28, 14:31 UTC, on the first open
trades).
- `JEV_LIVE_REVIEW_POLICY_V1` sets `question_timeout_seconds` = `batch_timeout_seconds` = 3, and
  `overall_review_deadline_seconds` = 10. Transport failures are not retried.
- With about 7.5k input tokens per maintenance review, valid answers took 0.6 s at the median
  and up to 2.95 s. In the first 16 minutes of open trades, 6 of 30 calls (20%) ended at 3.0 s
  as `PROVIDER_TRANSPORT_FAILURE`.
- A failed review changes nothing: the trade keeps its levels and its stop at Alpaca. The
  watchdog raises `MAINTENANCE_REVIEW_FAILING`.
- By hour (UTC), 2026-09-28:
  | Hour | Calls | Failed | Valid answers |
  | --- | --- | --- | --- |
  | 05 (selection) | 36 | 0 | p50 1.5 s |
  | 11 (selection) | 26 | 0 | p50 2.0 s, max 3.8 s |
  | 14 (trade reviews) | 60 | 13 | p50 0.57 s |
  | 15 (to 15:13) | 29 | 11 | p50 0.54 s |
  - The failing calls stall rather than slow down.
  - Selection calls are not held to the 3 s cap (one valid answer took 3.8 s). The cap binds
    calls made through the runtime's attempt permit.
- The breaker opened at 15:11 UTC after four failures in a row (`JEV_BREAKER_OPEN`), blocked two
  calls, and closed again 40 s later through its recovery probes.
- The breaker is shared, so if trade reviews open it during a selection, a pick whose review is
  blocked or fails is left unranked (`MISSING_VALID_REVIEW` / `PROVIDER_UNAVAILABLE`). That means
  fewer picks, never a wrong one.
- *Proposal (owner's call):* a named `JEV_LIVE_REVIEW_POLICY_V2` with an 8 s question and batch
  timeout, inside the unchanged 10 s overall deadline. It is not only a setting:
  - `review_config.APPROVED_GATE1` pins V1 exactly;
  - migration 011's `lab.register_review_scope` also accepts only V1's exact JSON, so V2 needs a
    migration (owner-run, backup first);
  - it also needs the Railway variable and a trader redeploy.

## Improvements, in the recommended order

| # | Improvement | Why | Notes |
| --- | --- | --- | --- |
| 1 | **History tester** (backtesting on years of minute bars) | Test an idea (breakouts, stop rules, cadence) in minutes instead of waiting weeks for live samples. It is what the weekly review needs to judge proposals quickly. | Build on the existing bar-walk engine (`pick_outcomes.walk_to_exit`, used by `PICK_SHADOW_OUTCOME_V1` and `UNCHANGED_PLAN_REPLAY_V1`), with public Alpaca and Coinbase history. Watch for look-ahead and survivorship bias. |
| 2 | **Maker entry orders on Alpaca** | Entries resting in the book pay 0.15% instead of 0.25% taker. That is the cheapest cost cut available, since fees are about a quarter of a trade's risk with 2% stops. | A new entry-execution version. Measure the fill rate against price improvement, because a resting order can miss. |
| 3 | **Bitcoin-exposure cap** (correlation-aware risk) | Most coins move with Bitcoin, so 10 open longs are close to one large Bitcoin bet. | A new risk version: cap the total beta-weighted exposure, or cap the number of highly correlated trades open at once. |
| 4 | **Event-driven research** | Once-a-day research is slow for a 24/7 market. When a coin starts moving or major news breaks, Muse researches right away. | Builds on the learning loop's movers measurement; needs its own trigger and budget rules. |
| 5 | **Richer data** | Funding rates, open interest, order-book depth, on-chain flows: factors the daily market review may show to matter. | Prefer free public sources. Add each as a cited research input first, and as a rule input only with evidence. |
| 6 | **Simple baseline** | Prove the AI adds value beyond the rules, for example "buy every valid pullback, fixed exits", run in shadow alongside the real picks. | Shadow only; no orders. |
| 7 | **Simulated second venue on real Coinbase/Binance prices** | Test shorting and more coins, with realistic maker and taker fees, without a broker, so any later venue choice rests on evidence. | Internal paper venue: fills against the live order book, the venue's own fee schedule, funding for simulated perpetual futures. |
| 8 | **Venue choice, only with evidence from 7** | Profiting from falls needs a futures venue. For US residents the options are CME futures through a broker, Coinbase's futures, or Kraken's US derivatives. | A separate, larger project. A switch restarts the experiment's comparisons. |

## Open items carried over from 2026-09-28

- **Storage.** A maintenance review adds about 65–75 KB to the ledger, so the 5 GB Hobby volume
  fills in about 9–11 days at five trades reviewed every minute. Decide soon on one of: a smaller
  per-review footprint, a lower cadence, or a larger plan.
- **Verified at Alpaca for crypto, 2026-09-28:** the first stop replacement. At 15:00:40 UTC Jev
  raised LTC's stop from 65.239 to 68.68085, and the app sent a price-only PATCH of the
  stop-limit. Alpaca acknowledged it with the new stop, and a clean reconciliation followed.
  Still unverified: a sell stop-limit placed while a buy limit is still open.
- **Protection-plan record after a stop replacement.** `PROTECTION_PLAN` is keyed by the plan's
  digest within a lifecycle. So PROTECTED → REPLACING → PROTECTED records no second PROTECTED,
  and the latest plan event stays REPLACING (LTC, 15:00:42 UTC).
  - Readers of the latest plan (the operator-flatten report's `protection_plan` detail,
    acceptance evidence) show a stale state.
  - The flatten codes only single out HALTED, and a HALTED plan also latches a durable halt.
  - Fix: key a plan event by its predecessor too, so a return to an earlier plan is recorded.
- **Entry distance (for more trades; owner's call).** Intraday picks sit 2–4% below the price,
  so entries fill mostly on dips (2026-09-28: one fill in the four hours to 21:40 UTC).
  - The app requires a stop at least 2% under the max entry and a cited target at least 2R
    above it, and Jev checks that the bars show each level.
  - A computed 2% stop (research kit `INTRADAY_V3`) was vetoed 7 of 12 times on its one live
    run, 2026-09-28 20:00 slot, and reverted (PHASES).
  - Options, each a named version:
    - an intraday risk version with a smaller minimum stop distance (at 1%, the 0.25%-a-side
      fees would be about 0.5R a trade);
    - a breakout entry type (disabled until a backtest enables it);
    - a larger top-K (today 5–10) for more watched setups.
- **Off-host alerts** are not configured (`notify=NOT_CONFIGURED`), so the watchdog cannot yet
  reach the owner's phone.
- **Research kit:** a pick over the 11 KB dossier budget is refused rather than trimmed
  (SHIB on 2026-09-28). Trim bars and notes instead.
- **Dashboard:** "today's picks" counts a run by its run slot's New York day, so a report that
  answers yesterday's slot shows 0. It should count by the day the report was received.
- **Performance:** `jev_calls_today` scans two whole tables on every status call; make it
  incremental, as the Jev spend meter already is.
- **Tests:** re-run the full suite at the integration commit. The last run was stopped by the
  low-disk guard.
- **The retired Mac ledger's database cluster is still running.** Stop it after a final backup
  (runbook).
