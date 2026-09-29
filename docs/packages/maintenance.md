# Package maintenance — Jev maintains open crypto trades (plan phase 5)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-maintenance`, from `work/2026-09-24-product-plan` at `20aeff4` (schema
22). Plan: `docs/CRYPTO-AGENT-LOOP.md` section 4.6 (4.6.1, 4.6.2, the Jev side of 4.6.3, 4.6.5,
4.6.6, 4.6.7; owner-approved 2026-09-26) with sections 2, 3, 5 and 8. Builds on
`docs/packages/crypto-trigger.md` and `docs/packages/crypto-size-hold.md` (the same scoping
pattern: a version recorded in the WATCHING state at admission, everything keyed off that state).
This file carries the text the coordinator merges into `docs/PHASES.md` and
`docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch. No migration.

## PHASES entry (paste as written)

### 2026-09-27 — Trade maintenance: the monitoring Jev maintains open crypto trades (plan phase 5, package maintenance) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue
(extended in the tests with a refused price replace and a refused stop-limit while a buy works),
a scripted bar source, a mock Jev transport and the session harness's `SESSION_SIMULATION`
venue. No broker, provider, network or owner-ledger contact; no migration (schema stays 22); no
SQL changed. Every broker POST, DELETE and PATCH keeps its exact one-use five-second
authorization; the research loop never touches the broker. Only crypto setups admitted from
report-V3 packets in the `JEV_MANAGED` arm from now on are affected: V1, V2, B1, B2, operator
engineering setups, every setup admitted before this version and the 30% `FIXED_EXIT` control
arm keep today's behaviour exactly. `MANAGED_MANAGEMENT_REVIEWS=DISABLED` sends nothing. A
real-Jev maintenance session (`--jev typesafe`) and Alpaca paper checks of the two broker
fallbacks are still to do.

- **Versions** (`crypto_maintenance.py`): `CRYPTO_MAINTENANCE_V1` and `CRYPTO_PARTIAL_ENTRY_V1`,
  recorded at admission for a report-V3 crypto setup: `partial_entry_policy` in both arms
  (coordinator's integration change: plan 4.6.1 is how every trade opens) and
  `maintenance_policy` when its randomized arm is `JEV_MANAGED`; `JEV_MANAGED_POSITION_CONTEXT_V4` and
  `JEV_MANAGED_POSITION_QUESTIONS_V4` (`maintenance_dossier.py`); `EARLY_EXIT_FLAG_V1`
  (`exit_flags.py`).
- **Opening (4.6.1).** The stop-limit rests at Alpaca and the target is watched by the app from
  the first fill (as before). A partial fill is protected at once for the filled quantity; the
  rest of the entry keeps working until ten minutes after the first fill, or until a fresh quote
  leaves the entry zone (ask above the max entry, bid at or below the stop), and is then
  cancelled under its own authorization with that reason (`PARTIAL_ENTRY_TIMEOUT`,
  `PARTIAL_ENTRY_ABOVE_MAX_ENTRY`, `PARTIAL_ENTRY_AT_OR_BELOW_STOP`). If the broker refuses the
  protection while the rest works, the rest is cancelled first and protection follows
  (`PARTIAL_ENTRY_PROTECTION_REFUSED`) instead of today's flatten. `MAINTENANCE_OPENED` records
  the average entry, the initial stop and target, the risk per coin (max entry minus stop), the
  planned reward-to-risk and the first 24-hour review time; `MAINTENANCE_ENTRY_COMPLETED` the
  final average and quantity.
- **Cadence (4.6.2)** (`trade_maintenance.TradeMaintenance`, the runtime's research loop, about
  once a second): a review at every completed 15-minute bar, and at once on the first +1R, +2R…
  milestone, the bid within 0.5% of the target or of the stop (once per level), agent news
  (`POSITION_NEWS`) and a 3% Bitcoin move within 15 minutes (`MAINTENANCE_BTC_SHOCK`: every
  maintained trade, nearest to its stop first). At most one review a minute per trade, except
  near the target. Reviews start once the entry is complete. The runtime subscribes BTC/USD on
  its crypto stream while a maintained setup is active and keeps Bitcoin's last 15 minutes.
- **What Jev reads (V4, ≤ 11,000 bytes).** The pick as the agent priced and argued it, Jev's own
  selection answers, the trade now (entry, bid/ask, P&L, best and worst move and milestone in R,
  time in trade and to the 24-hour review), every level change and why, recent 15-minute and
  1-hour bars, the coin against Bitcoin (1 h, 4 h, 24 h, since entry), news since entry and the
  code's options. A fixed ladder fits it; if it cannot, the review is skipped once.
- **Options (code computes, Jev chooses).** Stops: breakeven once price has been at least 1R
  above entry; 15-minute (24 h) and 1-hour (72 h) swing lows above the current stop and at least
  1% below the bid. Targets: 1-hour and 4-hour swing highs (7 days), the 24-hour and 7-day highs
  above the current target. At most five each, on the coin's price increment.
- **Answers and checks.** HOLD, RAISE_STOP, RAISE_TARGET, RAISE_STOP_AND_TARGET or
  FLAG_EARLY_EXIT; uncertain or inconsistent answers change nothing. Before applying (under the
  shared lock): the new stop above the old one and at least 0.5% below the bid, the new target
  above the bid and the old target, the answer less than 60 s old, the new level not crossed
  since the request; anything else is refused and logged, and the trade keeps its levels.
- **Applying.** A raised target takes effect at once. A raised stop is recorded as
  `stop_replace` and the protection loop replaces the resting stop-limit with a price-only PATCH
  (`AMEND`, its own authorization); if the broker refuses it, cancel then place. Until the broker
  order carries the raised stop the app enforces it (a fresh bid at or below it sells at market:
  `STOP_CROSSED_DURING_REPLACE`). `STOP_REPLACED` records the path that ran. The target sells
  the whole position.
- **Exit flag, Jev side (4.6.3).** FLAG_EARLY_EXIT (with the trade reason BROKEN) appends
  `EXIT_FLAG_RAISED` (who, reasons, receipts, levels, quote); the trade keeps its stop and
  target; the status alarms `EXIT_FLAG_PENDING`. `exit_flags.pending_exit_flags` and
  `resolve_exit_flag` are the record phase 6 consumes and resolves (EXIT_AGREED requests the
  market sell).
- **Edge cases (4.6.7).** A stop crossed or a trade closed while Jev answers discards the review
  (`POSITION_REVIEW_OBSOLETE`); a level already crossed is refused; many trades are prepared
  nearest to their stop first and reviewed concurrently; Jev unavailable keeps every level,
  records `FAILED`, raises `MAINTENANCE_REVIEW_FAILING` and waits for the next scheduled review
  (breaker recovery is the existing probe).
- **Measurement hook.** One `MAINTENANCE_DECISION` per review (APPLIED, REFUSED, HELD, FLAGGED,
  FAILED or DISCARDED) with old and new levels, options, decision and answer times, bid/ask/last
  at the decision, receipts and trigger reasons, for the results package (plan 4.6.8).
- **Session harness.** `execute --simulate-prints` gives each maintained trade one maintenance
  review over `SESSION_SIMULATION` bars (price moved to +1R) with fixture V4 answers (the script's
  `"maintenance"` key) or the real model under `--jev typesafe`.

Evidence: `tests/test_maintenance_rules.py` (38), `tests/test_trade_maintenance.py` (32),
`tests/test_maintenance_runtime.py` (15), `tests/test_maintenance_session.py` (3): 88 new tests,
listed under "Validation" below. No existing test changed. Full suite and ruff: see
"Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### Trade maintenance: `CRYPTO_MAINTENANCE_V1`, `CRYPTO_PARTIAL_ENTRY_V1`, `JEV_MANAGED_POSITION_CONTEXT_V4`, `JEV_MANAGED_POSITION_QUESTIONS_V4`, `EARLY_EXIT_FLAG_V1` (2026-09-27, package maintenance)

Owner decisions of 2026-09-26 (`docs/CRYPTO-AGENT-LOOP.md` 4.6, approved): when a trade
triggers, the monitoring Jev maintains it (reviews every 15 minutes and on events; code-computed
stop and target options; Jev only chooses; code checks every change), a partial fill is
protected at once and the rest of the entry works for at most 10 minutes, Jev can flag a trade
for an early-exit review, and a 30% control group keeps its original stop and target. Named
versions under the owner's 2026-09-24 ruling. `JEV_MANAGED_EXITS_V1`,
`JEV_MANAGED_POSITION_CONTEXT_V3`/`QUESTIONS_V3` (and V1, V2),
`JEV_MONITOR_SCHEDULING_ENGINEERING_V1`, `CRYPTO_ALPACA_TRIGGER_V1`, `CRYPTO_24H_HOLD_V1`,
`JEV_MANAGED_RISK_V3`, the crypto protection plan's default behaviour, the authorization gate,
every SQL function and every stored record keep their definitions; no migration.

**Scope.** A setup admitted from a packet whose `market` is `CRYPTO` and whose
`report_schema_version` is `AGENT_RESEARCH_REPORT_V3` (the scope of `CRYPTO_ALPACA_TRIGGER_V1`)
records, in the admitting transaction, `partial_entry_policy`
(`{policy_id: "CRYPTO_PARTIAL_ENTRY_V1", max_remainder_seconds: 600}`) in both randomized arms
(`assign_arm`, fixed at admission), since plan 4.6.1 is how every trade opens and the control
arm must enter exactly as the maintained arm does, and `maintenance_policy` (the exact
`crypto_maintenance.CRYPTO_MAINTENANCE` record below) only in the `JEV_MANAGED` arm; protection,
reviews and the runtime key off those fields only (a state with altered numbers is refused). A
`FIXED_EXIT` setup (the control arm, plan 4.6.6) is never maintained: no reviews, no changes, no
flags. Operator `ENGINEERING_TEST` setups are never report V3.

**`CRYPTO_MAINTENANCE_V1`** (record: `review_bar_seconds` 900, `near_target_fraction` 0.005,
`near_stop_fraction` 0.005, `min_review_interval_seconds` 60, `answer_max_age_seconds` 60,
`review_deadline_seconds` 10, `stop_bid_margin` 0.005, `swing_low_price_margin` 0.01,
`max_stop_options` 5, `max_target_options` 5, `swing_span` 2, `benchmark_symbol` BTC/USD,
`benchmark_shock_fraction` 0.03, `benchmark_window_seconds` 900, plus the context, question and
flag versions). Definitions: E = average entry (the setup's buy fills), M = max entry, S0 =
initial stop, R = M − S0 (the risk per coin, plan 4.6.1 and 4.8), "price" = the bid (the sell
side that triggers stops and targets).

*Cadence.* `TradeMaintenance.run_pass`, in the runtime's research loop about once a second, for
open maintained trades whose entry is complete (`MAINTENANCE_ENTRY_COMPLETED` recorded), with no
exit requested and no stop replacement in progress, one request at a time per trade:

1. With the stream's current quote (its age is recorded, never bounded: an unchanged quote is not
   re-sent), each trigger is recorded once as `MAINTENANCE_TRIGGER`: `R_MILESTONE` n for every
   whole n with bid ≥ E + n × R (each n once per lifecycle); `NEAR_TARGET` when bid ≥ target ×
   0.995 (once per target level); `NEAR_STOP` when stop < bid ≤ stop × 1.005 (once per stop
   level).
2. A review is due for: a completed 15-minute bar (UTC boundary after the open and after the
   last request's bar), an unserved trigger, a news revision above the last request's
   (`POSITION_NEWS`), or a `MAINTENANCE_BTC_SHOCK` after the last request whose price time is at
   or after the open. It waits while the last request is less than 60 s old unless an unserved
   `NEAR_TARGET` is among its reasons.
3. Bitcoin: the runtime subscribes BTC/USD (quotes and trades) on its authenticated crypto stream
   while any maintained setup is active and keeps each second's low, high and last (quote mids
   and prints). A shock is the latest price at least 3% above the window's lowest or 3% below its
   highest within the last 15 minutes (exact products); it is recorded once, then the window
   restarts at the shock. A market gap or restart empties the window.
4. Due trades are prepared in order of (bid − stop) / bid, nearest to the stop first (a trade
   without a stream quote last), then reviewed concurrently; each request records its reasons,
   `priority_rank` and the facts it serves.

*Request.* A read-only broker view (the position, every protective stop-limit covering it, no
working entry), a quote fresh by read time (the stream's if received within 5 s, else one REST
latest-quote read per pass and at most one per symbol per 5 s, its own `LivePriceReader` on the
position source), completed 15-minute bars (96) and 1-hour bars (168) of the coin and of BTC/USD
(each fetched once per bar boundary, GET only), the options, the V4 context and
`POSITION_REVIEW_REQUEST` (deadline 10 s, never past the 24-hour exit). No review while the bid
is at or below the stop.

*Options.* Code computes, Jev chooses; every price on the coin's price increment (stops rounded
down, breakeven and targets up). Swing low (high): a completed bar whose low (high) is strictly
below (above) those of the two completed bars on either side. Stops, highest first (`S1`…), at
most five: breakeven = E rounded up, offered once the best bid since the open (per-second
position samples, recorded milestones, the current bid) is at least E + R, and only above the
current stop and at least 0.5% below the bid; swing lows of the 15-minute bars of the last 24
hours and of the 1-hour bars of the last 72 hours, above the current stop and at most bid × 0.99.
Breakeven keeps its place; the other places go to the highest swing lows. Targets, nearest first
(`T1`…), at most five: the 24-hour high and the 7-day high (highest completed 15-minute or 1-hour
bar high in that span) and the swing highs of the 1-hour bars and of 4-hour bars aggregated from
them (UTC-aligned) of the last 7 days, above the current target and the bid. The 24-hour and
7-day highs keep their places; the others go to the swing highs nearest above the current target.

*Answer* (`JEV_MANAGED_POSITION_QUESTIONS_V4`): HOLD; RAISE_STOP (a stop option, target KEEP);
RAISE_TARGET (a target option, stop KEEP); RAISE_STOP_AND_TARGET (both); FLAG_EARLY_EXIT (both
KEEP, trade reason BROKEN). An Insufficient-evidence or tied answer, an action inconsistent with
the option answers, a BROKEN trade reason without the flag (or the flag without BROKEN), or an
option the context never offered changes nothing (`REFUSED` with `UNCERTAIN_JUDGMENT`,
`CONTRADICTORY_MANAGEMENT_ANSWERS` or `UNKNOWN_OPTION`).

*Checks before applying* (under the shared ledger lock, in this order; the first failure refuses
the whole answer and the trade keeps its levels): a fresh quote (`CURRENT_QUOTE_UNAVAILABLE`);
the trade open in the request's lifecycle without an exit (`POSITION_NOT_OPEN`); no stop
replacement in progress (`STOP_REPLACE_IN_PROGRESS`); account risk evaluable
(`ACCOUNT_RISK_UNEVALUABLE`); stop and target unchanged since the request
(`LEVELS_CHANGED_SINCE_REVIEW`); the answer (its receipt's completion) less than 60 s old
(`ANSWER_TOO_OLD`); each new level on the increment (`OFF_PRICE_INCREMENT`); a new stop above the
old (`STOP_NOT_ABOVE_CURRENT`), not reached by any bid since the request
(`STOP_LEVEL_CROSSED`) and at most bid × 0.995 (`STOP_TOO_CLOSE_TO_BID`; exactly 0.5% passes); a
new target above the bid (`TARGET_NOT_ABOVE_PRICE`), above the old target
(`TARGET_NOT_ABOVE_CURRENT`) and not reached by any bid since the request
(`TARGET_LEVEL_CROSSED`). Bids since the request: the protection loop's per-second position
samples after the request time, this process's observations and the current bid.

*Applying.* The target changes in the state at once; the protection loop sells the whole position
when the bid reaches it (no partial sales). A raised stop changes the state and records
`stop_replace` (`change_id` = the request, `path` PATCH_REPLACE, from, to). The protection loop
plans `REPLACE_STOP`: one price-only `PATCH /v2/orders/{id}` (`AMEND`, payload `stop_price` and
`limit_price` = the native stop and one increment below, exactly what a new stop-limit would
carry) per resting stop-limit, each under its own exact one-use five-second authorization; the
execution layer accepts a crypto PATCH only for a maintained setup's owned stop-limit carrying
the desired native levels. If the broker refuses it, `STOP_REPLACE_FALLBACK` switches the path to
CANCEL_THEN_PLACE (today's `TIGHTEN_STOP` cancel, then a new stop-limit). Until every resting
stop-limit carries the raised stop and covers the position, a fresh bid at or below it requests
the exit at once (`STOP_CROSSED_DURING_REPLACE`: cancel the resting protection, sell at market).
`STOP_REPLACED` records the path that ran and clears `stop_replace`.

*Outcomes and the measurement hook.* Exactly one `MAINTENANCE_DECISION` per request (key
`maintenance-decision:<request>`): `outcome` APPLIED, REFUSED, HELD, FLAGGED, FAILED (Jev
unavailable: breaker, provider or deadline; the code is the review's) or DISCARDED (the trade
closed, began exiting or its stop was crossed while Jev answered), `code`, `action`,
`trade_reason`, `answers` (choice and top probability), `stop` and `target` (`old`, `new`,
`option_id`, `bases`), `levels_before`, `levels_after`, `stop_replace_path`, `requested_at`,
`answered_at`, `decided_at`, `quote` (bid, ask, mid, last and their times and source at the
decision), `receipt_ids`, `trigger_reasons`, `entry`, `risk_per_coin`, `qty`, and for a flag
`flag_id`/`flag_created`. A done-marker follows: `MANAGED_JEV_JUDGMENT` (V4 body with `outcome`
and `policy_id`), or `POSITION_REVIEW_OBSOLETE` for a discarded review. A request recorded before
a crash is resumed from its recorded receipt (never a second vote; the 60-second rule then
applies) or closed once its deadline has passed.

*Edge cases (4.6.7).* A stop crossed while Jev answers discards the review
(`STOP_CROSSED_DURING_REVIEW`; the stop fires on its own); a level already crossed is refused;
many trades are prepared nearest to their stop first and reviewed concurrently; Jev unavailable
keeps every level, records FAILED and waits for the next scheduled review (its triggers count as
served; the existing breaker probe recovers the provider). `MANAGED_MANAGEMENT_REVIEWS=DISABLED`
sends nothing (one `POSITION_REVIEW_SKIPPED` per lifecycle; no bars fetched, no triggers
recorded); protection and the partial-entry rule run unchanged. The runtime status carries
`trade_maintenance` (`open_trades`, `pending_exit_flags`, `oldest_exit_flag_raised_at`,
`exit_flags`, `failing_reviews`, `last_pass_at`); the watchdog raises `EXIT_FLAG_PENDING`,
`MAINTENANCE_REVIEW_FAILING` and, for an unreadable section, `MAINTENANCE_STATUS_UNAVAILABLE`.

**`CRYPTO_PARTIAL_ENTRY_V1`.** For a setup that records it, while its entry order works after a
partial fill: the planner protects the filled quantity at once (a stop-limit per unprotected
quantity) and keeps the rest of the entry working (`retain_entry`) until the first of: 600 s
after the first fill (`PARTIAL_ENTRY_TIMEOUT`, exactly 600 s cancels), a fresh quote (exchange
time within 5 s) with the ask above M (`PARTIAL_ENTRY_ABOVE_MAX_ENTRY`) or the bid at or below
the stop (`PARTIAL_ENTRY_AT_OR_BELOW_STOP`), or an unknown first fill
(`PARTIAL_ENTRY_FIRST_FILL_UNKNOWN`). The reason is recorded once (`partial_entry_cancel`,
`PARTIAL_ENTRY_REMAINDER_CANCEL`) and the rest is cancelled under its own authorization with that
reason; any exit cancels it too. A protection the broker refuses while the rest works records
`PARTIAL_ENTRY_PROTECTION_REFUSED`: the rest is cancelled first (`CANCEL_ENTRY_BEFORE_PROTECT`)
and protection is sent again under a fresh client order ID; a refusal with nothing working keeps
today's `PROTECTION_REJECTED` flatten. `MAINTENANCE_OPENED` (at the first fill) and
`MAINTENANCE_ENTRY_COMPLETED` (FILLED or REMAINDER_CANCELLED) record the open.

**`JEV_MANAGED_POSITION_CONTEXT_V4`** (`maintenance_dossier.py`, dossier `MAINTENANCE_DOSSIER_V1`,
state budget 11,000 encoded bytes; `jev_review` still refuses over 12,000). Sent:
`review_trigger` (reasons, priority rank); `original_pick` (kind, agent's current price and its
age, levels, stated reward-to-risk, `thesis`, `why_now`, `why_these_levels`, `risks`, `disproof`
= the pick's invalidation; never the agent's name, confidence, sources or bars);
`selection_answers` (pick review and quality receipts as `{choice, top_p}`); `trade` (entry,
qty, initial and current stop and target, max entry, risk per coin, bid, ask, quote age, P&L,
best, worst and milestone in R, stop and target in R from entry, distances in %, spread,
minutes in the trade and to the 24-hour review); `level_changes` (every applied change, newest
last: minutes ago, kind, old, new, option, bases, reasons; at most 8, the rest counted);
`price_action` (the newest 16 completed 15-minute and 24 one-hour bars, one row each, and
`vs_btc`: the coin's and Bitcoin's % move over 1 h, 4 h, 24 h and since entry); `news_since_entry`
(at most 4 of the lifecycle's `POSITION_NEWS` sources, adverse and withdrawn first, excerpts to
600 characters); `options` (every option, never truncated). Over budget, one step at a time:
shorten supporting excerpts to 300, drop the oldest level changes (3 stay), drop the oldest
1-hour bars (12 stay), shorten `why_now`/`why_these_levels`/`risks` to 200, drop supporting news,
drop the oldest 15-minute bars (8 stay), shorten adverse excerpts to 300; still over, the review
is skipped once (`POSITION_REVIEW_SKIPPED` `CONTEXT_BUDGET_UNSATISFIABLE`). Thesis and disproof
are never shortened. The request stores the manifest (every input, included, truncated or
omitted, with hashes) and the full option records beside the state.

**`JEV_MANAGED_POSITION_QUESTIONS_V4`** (stage TRACKING; the fixed texts are in
`maintenance_dossier.py`): `trade_reason` (INTACT, WEAKENED, BROKEN), `action` (HOLD,
RAISE_STOP, RAISE_TARGET, RAISE_STOP_AND_TARGET, FLAG_EARLY_EXIT), `stop_option` and
`target_option` (KEEP or an option ID; each option's text is its price, basis, bar age, distance
from the bid and R from entry, code-computed). Every choice offers Insufficient evidence.

**`EARLY_EXIT_FLAG_V1`** (`exit_flags.py`). `EXIT_FLAG_RAISED`: `flag_id`, `setup_id`,
`lifecycle_id`, `side` (JEV or AGENT), `raised_by` (Jev: request, receipts, context hash),
`reasons`, `evidence` (levels, quote, entry, R), `raised_at`, `answer_due_at` (15 minutes),
`trade_levels_unchanged: true`. One pending flag per side and lifecycle (a repeated answer
reaffirms it). `pending_exit_flags(conn, setup_id=, lifecycle_id=, side=)` lists unresolved
flags of trades still open in the flag's lifecycle. `resolve_exit_flag(store, conn, flag_id=,
outcome=, answered_by=, answer=, resolved_at=)` appends one `EXIT_FLAG_RESOLVED` (EXIT_AGREED,
EXIT_NOT_AGREED, NO_ANSWER_IN_TIME or LIFECYCLE_ENDED; a second is refused); EXIT_AGREED sets
`exit_requested` `EARLY_EXIT_AGREED` in the same transaction while the trade is open and not
exiting, and the protection loop cancels the stop-limit and sells at market under one-use
authorizations. Asking the other side and deciding is plan phase 6.

## Files

New: `src/catalyst_lab/crypto_maintenance.py` (versions, numbers, pure rules: triggers,
schedule, swing points, options, checks, partial-entry reasons, Bitcoin window),
`src/catalyst_lab/maintenance_dossier.py` (context V4, questions V4, answer reading),
`src/catalyst_lab/trade_maintenance.py` (the review orchestrator, decisions, recovery, status),
`src/catalyst_lab/exit_flags.py`, `tests/maintenance_fixtures.py`,
`tests/test_maintenance_rules.py`, `tests/test_trade_maintenance.py`,
`tests/test_maintenance_runtime.py`, `tests/test_maintenance_session.py`, this file.

Changed: `src/catalyst_lab/managed_execution.py` (`admit` records the versions;
`manage` calls `_maintenance_open_records`, `_maintenance_stop_watch` and `_partial_entry` and
passes the new planner options; `dispatch` and `_amendment_rejected` fall back through
`_partial_entry_protection_refused` and `_stop_replace_refused`; `_authorize_mutation` accepts a
crypto PATCH only for a maintained stop-limit); `src/catalyst_lab/crypto_execution.py`
(`plan_crypto_recovery(retain_entry=, entry_cancel_reason=, replace_stop=,
protect_after_entries=)`, defaults unchanged; `CRYPTO_AMEND`/PATCH proposals;
`native_stop_levels`); `src/catalyst_lab/managed_runtime.py` (`maintenance=`, the Bitcoin window,
BTC/USD subscription, `_position_pass` routing, `maintenance_observation`, status,
`build_runtime_from_env` wiring); `src/catalyst_lab/scan_sources.py` (`timeframe_bars`, GET
only; one-minute requests unchanged); `src/catalyst_lab/managed_service.py` (`STATE_FIELDS`,
`STATUS_FIELDS`); `src/catalyst_lab/managed_app.py` (status passes `trade_maintenance`);
`src/catalyst_lab/managed_ops.py` (`maintenance_alarms`); `scripts/agent_research_session.py`;
`docs/MANAGED-RUNTIME.md`, `docs/API-CONTRACT.md`, `docs/OPERATIONS-RUNBOOK.md`.

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-maintenance`).

- New tests (88): `tests/test_maintenance_rules.py` 38 (the versions' exact records and scope;
  milestone, near-target and near-stop boundaries; the partial-entry reasons at 599/600 s;
  strict swing points and UTC 4-hour aggregation; breakeven only after exactly +1R, swing lows
  above the stop and 1% below the bid, five at most with breakeven kept, increments; targets from
  24-hour/7-day highs and 1-hour/4-hour swing highs, five at most, nearest first; every check at
  its boundary; the schedule, the minute and the near-target exemption; nearest-to-stop order;
  the Bitcoin window at 2.998% vs exactly 3%, restart, age-out and reset; the V4 context at
  production size under 11,000 bytes with every option kept and thesis/disproof whole, byte
  identity, the unsatisfiable budget; the V4 question texts; every answer rule);
  `tests/test_trade_maintenance.py` 32 (on the fixture venue: admission scope for V3, V2 and the
  control arm; the open record; a partial fill protected at once, a second stop-limit for a later
  fill, the rest cancelled at exactly 10 minutes under its own claimed authorization; the zone
  exits; the refused-protection fallback; control arm and older setups cancelling at once; a bar
  review at each completed 15-minute bar only; milestones the first time only; near stop waiting
  for the minute and near target not; agent news; a Bitcoin shock reviewing two trades nearest
  to the stop first; a stop raise applied and replaced by an authorized, claimed PATCH, recorded
  as PATCH_REPLACE, reconciled clean; the refused replace falling back to cancel-then-place; the
  gap sale; an instant target raise and the whole position sold at it; both levels together;
  STOP_TOO_CLOSE_TO_BID, STOP_LEVEL_CROSSED and TARGET_NOT_ABOVE_PRICE refusals; uncertain and
  inconsistent answers; a recovered answer refused at 61 s; the Jev flag recorded, alerted,
  reaffirmed and resolved to a sale; a gap through the stop and a stop-out during the review
  discarded; Jev unavailable (HTTP 503, then an open breaker that sends nothing, then recovery);
  DISABLED; the control arm; the request under budget with production-size news);
  `tests/test_maintenance_runtime.py` 15 (planner defaults unchanged and the new options; PATCH
  proposals only for the amend; the execution layer refusing a PATCH for a non-maintained crypto
  stop; exit-flag helpers; BTC/USD subscription and window feed and reset; routing to maintenance
  vs today's monitor; the not-configured skip; the setting check; status fields and alarms; the
  factory wiring; the bar source's 15-minute request and the unchanged one-minute request; the
  runtime's position pass reviewing through maintenance and its protection tick replacing the
  stop);
  `tests/test_maintenance_session.py` 3 (a top-K session maintaining its trades with fixture
  answers, DISABLED, the fixture script's actions).
- Existing suites re-run during the work (759 passed): `test_managed_execution`,
  `test_crypto_execution`, `test_crypto_size_hold`, `test_crypto_trigger`,
  `test_managed_runtime`, `test_position_monitor`, `test_managed_dossier`, `test_managed_service`,
  `test_managed_app`, `test_managed_ops`, `test_unattended_safety`, `test_selection_topk`,
  `test_replacement`, `test_system_check`, `test_research_context`, `test_operator_flatten`,
  `test_managed_hardening`, `test_complete_managed_cycle`, `test_monitor_context_scheduling`,
  `test_managed_monitor_recovery`, `test_managed_failure_isolation`, `test_bracket_protection`,
  `test_scan_sources`, `test_managed_measurement`, `test_acceptance_evidence`,
  `test_managed_funnel`, `test_notify`; `test_agent_research_session` 15 passed.
- **Full suite at `ee6b641` (the last code and test change; later commits are docs only):
  3,129 passed, 0 failed** (10 min 24 s; baseline 3,041 plus the 88 new tests), about 11 GB
  free before the run.
- `ruff check src tests`: all checks passed.

## Deviations and choices (please confirm)

1. **Scope of the partial-entry rule — resolved at integration:** `CRYPTO_PARTIAL_ENTRY_V1`
   now applies to both arms (plan 4.6.1 describes how every trade opens). Originally: as
   briefed, `CRYPTO_PARTIAL_ENTRY_V1` applied to the
   maintained (`JEV_MANAGED`) arm only; the control arm keeps today's immediate cancel of an
   entry remainder. Applying it to the control arm as well would keep entry mechanics identical
   in both arms (a cleaner comparison); it is one line (`crypto_maintenance.admission_fields`).
2. **"Price" is the bid** for triggers, options and checks (the sell side that fires stops and
   targets); the plan says "price" throughout.
3. **Once per level.** Near-target and near-stop triggers fire once per target (stop) level; a
   raised level can trigger again. This bounds the near-target exemption from the minute.
4. **Breakeven** is the average entry rounded up to the increment (never a stop below entry) and
   also needs the apply rule (above the current stop, at least 0.5% below the bid); "has been
   at least 1R above entry" uses the best bid since the open.
5. **Swing points and windows** (the plan names no pivot rule): strictly beyond the two bars on
   each side; 15-minute lows of 24 hours, 1-hour lows of 72 hours, 1-hour and 4-hour highs of 7
   days; 4-hour bars aggregated from 1-hour bars. With more than five candidates, breakeven (or
   the 24-hour and 7-day highs) keep their places and the nearest swing points fill the rest.
6. **Reviews start once the entry is complete** (at most 10 minutes after the first fill).
7. **Stop replacement.** PATCH first: the PATCH-replace path (authorization, replacement
   tracking, reconciliation) is fixture-proven for stock bracket legs and now for crypto
   stop-limits, but Alpaca's crypto replace is unverified; a refusal falls back to
   cancel-then-place. The app enforces a raised stop from the moment it is applied until the
   broker's order carries it, on both paths.
8. **Protection refused while the rest of the entry works** cancels the rest first and protects
   again instead of flattening (a guard for a possible wash-trade refusal; unverified at Alpaca).
9. **Jev unavailable**: the review's triggers count as served, so a failing provider is asked
   again at the next scheduled review, not every minute.
10. **Uncertain or inconsistent answers are REFUSED** (no change), recorded like any refusal.
11. **Bitcoin window**: stream quotes and prints in one-second buckets, in memory (lost on
    restart or a crypto gap); a shock counts for a trade only when its price time is at or after
    the open.
12. **No migration**: event bodies and state fields only; the crypto PATCH uses the existing
    `AMEND`/`PATCH` decision rows and gate of migration 013.

## Open items and questions

- **Real-Jev maintenance session** (owner or coordinator): `start --jev typesafe --selection-rule
  TOPK`, then `execute --simulate-prints` (one maintenance review per maintained trade).
- **Alpaca paper checks** of the two broker fallbacks: a price-only replace of a crypto
  stop-limit, and a sell stop-limit while a buy limit of the same coin works.
- **Results (plan 4.6.8)**: the unchanged-plan counterfactual reads `MAINTENANCE_DECISION`.
- **Phase 6**: agent flags, asking the other side and the 24-hour review use `exit_flags`.
- **Thin coins**: triggers use the stream's current quote whatever its age (recorded); reviews
  take one bounded REST read when the stream quote is older than 5 s.
- **Shared position source**: `TradeMaintenance` and today's position monitor share the
  read-only position source (GET only, bars fetched once per bar boundary).
- **Found in passing (not changed here):** `ManagedRuntime.status()` reports `workers_alive`
  only when exactly 7 worker threads run, but `start()` launches 8 since the fee-import loop was
  added (`tests/test_unattended_safety.py` asserts 8), so the app would report
  `worker_state: STOPPED` and the watchdog `WORKER_STOPPED` while healthy.
