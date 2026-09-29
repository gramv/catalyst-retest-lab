# Package results (plan phase 7)

Branch `pkg/2026-09-27-results` from `work/2026-09-24-product-plan` at `20aeff4` (schema 22,
full suite 3,041 passed). The coordinator later merged `work/2026-09-24-product-plan` into
this branch (merge commit, no conflicts), bringing in package maintenance (plan phase 5,
`docs/packages/maintenance.md`: `MAINTENANCE_DECISION` and `EXIT_FLAG_RAISED`/
`EXIT_FLAG_RESOLVED`); a follow-up commit set on this branch then wires
`UNCHANGED_PLAN_REPLAY_V1` to those events (see the second half of this file). Scope:
`docs/CRYPTO-AGENT-LOOP.md` section 4.8 (every pick tracked; results net of Alpaca's fees) and
4.6.8 (unchanged-plan comparison, "Measuring maintenance"), plus the "Result" entity in
section 3. **Paper only. No order, no broker credential, no risk authorization anywhere in
this package.** Every test runs against disposable PostgreSQL, the maintenance package's own
fixture venue/bar source/mock Jev transport (`tests/maintenance_fixtures.py`) and, for the one
HTTP-facing piece (`public_crypto_bars.py`), an `httpx.MockTransport` — fixture evidence only;
the real public Alpaca crypto-bars endpoint has not been read from this environment (see
"Deviations" below). No migration: everything fits the existing append-only
`lab.managed_events` audit path; `SCHEMA_VERSION` stays 22.

## Text for docs/PHASES.md (not applied there; the package instructions ask for it here)

> ## 2026-09-27 — Pick shadow outcomes, unchanged-plan replay and results views (plan phase 7, package results) (FIXTURE EVIDENCE ONLY)
>
> Every report-V3 pick of every run — selected and traded, selected but declined by the
> system check, ranked but not selected, vetoed, not ranked — is now tracked to a shadow
> outcome: an offline, deterministic job (`scripts/run_pick_shadow_outcomes.py`) fetches
> Alpaca's *public* crypto minute bars (no API key) once a pick's own window plus a 24-hour
> hold has fully elapsed, and appends one immutable `PICK_SHADOW_OUTCOME` event recording
> whether it would have triggered, then stopped, hit target or exited at 24 hours, in R at the
> max entry price, gross and net of an assumed Alpaca tier-1 taker fee
> (`PICK_SHADOW_OUTCOME_V1`; the method, its same-bar-ambiguity rule and its fee assumption are
> stated in every recorded outcome, never left implicit). A pick that actually traded keeps its
> own real, verified `managed_measurement` figures alongside the shadow ones. New read-only
> routes (`GET /api/v1/lab/cycles/{cycle_id}/picks`, `GET /api/v1/lab/results/picks`, `GET
> /api/v1/lab/results/picks/aggregates`) and a small `/results` page on the existing private
> dashboard surface it: today's picks with rank/status/outcome, open trades, closed trades net
> of fees, and grouped aggregates by agent, Jev rank/replacement/not-selected, pick kind,
> managed-vs-control arm and selected-vs-not (counts always shown, never a bare mean). A
> separate pure function, `unchanged_plan.replay_unchanged_plan`
> (`UNCHANGED_PLAN_REPLAY_V1`), replays the same minute bars using the levels in force
> *before* a detected maintenance change, to say what the unchanged plan would have done and
> the R difference against what actually happened. It reads every change from three sources,
> unioned per setup in time order: the pre-existing `JEV_MANAGED_EXITS_V1` amendment mechanism
> (`MANAGEMENT_PLAN_AUTHORIZED`); package maintenance's `CRYPTO_MAINTENANCE_V1`
> (`MAINTENANCE_DECISION`, `outcome: "APPLIED"` only, read from the decision's own recorded
> `levels_before`/`levels_after`); and an agreed early exit under `EARLY_EXIT_FLAG_V1` (an
> `EXIT_FLAG_RESOLVED` `EXIT_AGREED`, read against its own flag's recorded `evidence.levels`) —
> in every case from the event bodies themselves, never the setup's state at read time. Two
> more read-only routes, `GET /api/v1/lab/results/maintenance` (per trade with `setup_id`,
> across every trade without it) and its `/aggregates` (totals by change kind and arm, counts
> beside every mean), and a small dashboard section, surface it; both need an injected
> `bar_reader` (a new optional `create_managed_app` parameter, `clock` alongside it) and
> compute live rather than from a persisted event. Fixing this package's own bar-fetch-window
> boundary (the fetch for a 24-hour-hold exit excluded the one bar needed to price it,
> `DATA_INCOMPLETE` at every deadline until now) also corrects `run_shadow_outcome_job`'s own
> 24-hour-exit case for picks. No migration (schema stays 22); no order, broker credential or
> risk authorization anywhere in this package.
>
> Evidence: 86 new tests (`tests/test_pick_outcomes.py`, `tests/test_pick_outcomes_ledger.py`,
> `tests/test_public_crypto_bars.py`, `tests/test_unchanged_plan.py`,
> `tests/test_run_pick_shadow_outcomes.py`, `tests/test_managed_service_results.py`,
> `tests/test_maintenance_replay.py`), covering the simulation's boundaries (never triggered,
> stop-before-trigger invalidation, triggered then stop/target/24-hour exit, same-bar
> ambiguity, the validity-end boundary), the fee-assumption arithmetic in Decimal, ledger
> reconstruction of every pick fate against a real (disposable) managed lifecycle, job
> idempotency and readiness gating, the results/aggregate routes' counts-first shape and
> engineering-setup exclusion, the unchanged-plan replay for a stop raise and a target raise
> (helped and hurt), the dashboard page's absence of secrets or `innerHTML`, and — on the
> maintenance package's own fixture venue, driving a real admit/trigger/fill/review flow, not
> hand-inserted events — every `MAINTENANCE_DECISION` outcome (stop, target, both raised;
> `HELD`/`REFUSED` producing no change), an agreed vs. declined vs. pending exit flag, the
> three-source union in time order, and both new routes (503 without a configured
> `bar_reader`, 401 without auth, a served replay). Full suite and ruff: see "Verification"
> below.

## Text for docs/CONTRACT-RESOLUTIONS.md (not applied there)

> ### Results: `PICK_SHADOW_OUTCOME_V1`, `UNCHANGED_PLAN_REPLAY_V1` (package results, 2026-09-27)
>
> - **`PICK_SHADOW_OUTCOME_V1`** (`pick_outcomes.py`) is the named method for every report-V3
>   pick's shadow outcome, computed offline from Alpaca's public crypto minute bars (no API
>   key; `public_crypto_bars.py`), never a live quote or a fill:
>   - **Window**: `[report generated_at, the pick's own packet expiry)` — the *same* rule for
>     every pick of a run, selected or not, so the comparison is fair; this is deliberately
>     **not** an admitted setup's actual WATCHING window (which starts later, once Jev and the
>     system check finish).
>   - **Touch** = a bar's low at or below a level (never a bar's open/close, never an
>     intra-bar path). The entry trigger is checked before the stop; a bar whose low reaches
>     the stop is read as "stop before (or without) a valid trigger," `NEVER_TRIGGERED_STOP_FIRST`,
>     even though a low that reaches the stop numerically also passes the (higher) trigger — the
>     conservative reading, matching the running system's own "stop invalidation takes priority
>     before confirmation." A pick whose window elapses with no bar reaching its entry trigger is
>     `NEVER_TRIGGERED_VALIDITY_EXPIRED`.
>   - **Fill**: once triggered, the assumed fill price is the pick's own **max entry price** (a
>     limit order there is never filled worse than that — the same assumption the running
>     system's own entry order makes).
>   - **Exit**: the first bar at or after the fill whose low is at or below the stop or whose
>     high is at or above the target decides the exit (`STOP` or `TARGET`); a bar that reaches
>     **both** in the same minute is resolved conservatively as the **stop**
>     (`same_bar_ambiguous: true`, counted, never silently split or averaged). Neither hit by 24
>     hours after the fill (`CRYPTO_24H_HOLD_V1`'s own horizon; report V3 is crypto-only) exits
>     there, `HOLD_24H_EXIT`, priced at the next bar's open at or after the deadline. Bars ending
>     before a boundary that must be checked answer `DATA_INCOMPLETE` — fail-closed; no exit is
>     ever invented from a gap in Alpaca's own bar history.
>   - **R**: computed at the max entry price exactly as `official_r` (owner ruling R5): `(exit −
>     entry) / (entry − stop)`, so no trade-size assumption is needed. `net_r` additionally
>     subtracts an assumed fee: Alpaca's published crypto **tier-1 taker fee (0.25%)** on both
>     the entry and the exit leg (`FEE_ASSUMPTION_VERSION =
>     "ALPACA_CRYPTO_TIER1_TAKER_BOTH_LEGS_V1"`), stated explicitly on every recorded outcome as
>     an assumption never read from any account. A pick that actually traded keeps its own real,
>     verified `managed_measurement` figures (`official_r`, `net_r`, `verified_cash_fee_usd`, …)
>     alongside — read fresh at query time, never frozen into the immutable shadow event, since
>     fee evidence can arrive after a position closes.
>   - **Scope**: only intake-accepted picks (a recorded `RESEARCH_PACKET`) are simulated; a pick
>     rejected at intake never had usable levels. Recorded append-only as `PICK_SHADOW_OUTCOME`
>     events (idempotency key `pick-shadow-outcome:<cycle_id>:<item_key>:<revision>`), `setup_id`
>     set on the event row when the pick was admitted (so it also surfaces on that setup's own
>     `/positions/{id}/timeline`), `null` otherwise; safe and idempotent to recompute (skipped,
>     never duplicated, unless `--force`).
>   - **`rank_bucket`** (a display grouping, not a new ledger field): `TOP_K` (published, not a
>     replacement), `REPLACEMENT` (published via `TOPK_REPLACEMENT_V1`), `NOT_SELECTED` (ranked,
>     never published), `VETOED`, `NOT_RANKED`, or `NO_RANKING_RULE` for a pick reviewed under a
>     non-top-K selection rule (V2/B1/B2), which has no Jev rank to bucket by.
> - **`UNCHANGED_PLAN_REPLAY_V1`** (`unchanged_plan.py`) replays the same minute bars from a
>   detected change onward using the levels in force **before** that change, reusing the same
>   `pick_outcomes.walk_to_exit` stop/target/24-hour-hold rule (including the
>   same-bar-ambiguity-as-stop rule) — and reports the counterfactual exit, its own
>   (assumed-fee) R, and the difference against the trade's real `official_r`
>   (`r_difference = actual_r − unchanged_net_r`; positive means the change helped).
>   `setup_level_changes` unions three sources per setup, in time order (a given setup uses at
>   most one of the first two in practice, but nothing assumes that):
>   - `JEV_MANAGED_EXITS_V1`'s `MANAGEMENT_PLAN_AUTHORIZED` amendment (pre-dates package
>     maintenance): the new levels from that event, the old ones from the setup's `STATE` row
>     immediately before it (`stop_target_changes`).
>   - Package maintenance's `CRYPTO_MAINTENANCE_V1`: one `MAINTENANCE_DECISION` per review,
>     `outcome: "APPLIED"` only (`REFUSED`, `HELD`, `FLAGGED`, `FAILED` and `DISCARDED` change
>     nothing and are not replayed) — old and new levels read straight from that decision's own
>     `levels_before`/`levels_after`, never the setup's state at read time, since it may have
>     moved again since (`maintenance_level_changes`). The change kind follows the decision's
>     own `action` (`RAISE_STOP`, `RAISE_TARGET`, `RAISE_STOP_AND_TARGET`).
>   - An agreed early exit under `EARLY_EXIT_FLAG_V1`: one `EXIT_FLAG_RESOLVED`
>     `outcome: "EXIT_AGREED"`, joined to its own `EXIT_FLAG_RAISED` by `flag_id`; the original
>     levels are that flag's own recorded `evidence.levels` (the levels in force the moment it
>     was raised, never re-derived from the setup's state at read time)
>     (`maintenance_exit_changes`). A flag that is still pending, or resolved
>     `EXIT_NOT_AGREED`/`NO_ANSWER_IN_TIME`/`LIFECYCLE_ENDED`, changed nothing.
>   A widened level (refused by the running code already) is never read as a raise. This
>   module still documents, but does not build, the hook for the maintenance package's own
>   not-yet-existing continue-or-exit event kind (plan phase 6, the 24-hour review): construct
>   a `LevelChange` the same way (an exit: `actually_exited=True`, `new_stop`/`new_target` left
>   `None`; a continue that also raises a level: the same shape as a maintenance-decision
>   raise), and pass it through `replay_unchanged_plan` unchanged — no change needed there.
> - **Maintenance-replay routes and their bar-fetch fix**: `GET /api/v1/lab/results/maintenance`
>   (optional `setup_id`; `after`/`limit=50`, paginated over setups, not rows) and `GET
>   /api/v1/lab/results/maintenance/aggregates` (optional `group_by`, subset of
>   `change_kind,arm,agent_id`, default `change_kind,arm`; `limit=500` setups) both need an
>   injected `bar_reader` (`create_managed_app`'s new optional parameter; 503
>   `MAINTENANCE_REPLAY_NOT_CONFIGURED` without one) and a `clock` (default the wall clock,
>   injectable like every other stateful component in this codebase). Neither route persists
>   anything: every replay is computed live, one bar fetch per change, on every call — a
>   documented, deliberate tradeoff at today's low review volume, not a design that scales to a
>   heavily-trafficked deployment without adding caching. Building this exposed a real
>   off-by-one at the fetch boundary shared with `pick_outcomes.run_shadow_outcome_job`'s own
>   24-hour-exit case: a bar-reader fetch window is `[start, end)`, exclusive of `end`, so a
>   fetch asked for bars ending exactly at a hold deadline could never return the one bar
>   `walk_to_exit` needs to price that exit, and the outcome was always `DATA_INCOMPLETE` right
>   at the boundary. Fixed by `pick_outcomes.BAR_FETCH_BUFFER` (5 minutes), added to both fetch
>   windows; `pick_outcomes.ready_at` (the job's own readiness gate) now includes it too, so the
>   gate and the fetch agree.
> - **Results views**: `GET /api/v1/lab/results/picks/aggregates` groups every recorded,
>   non-`ENGINEERING_TEST` pick outcome by any subset of `agent_id`, `rank_bucket`, `pick_kind`,
>   `arm`, `selected` and `continuation_decision` (default all six). Per group: `count`;
>   `outcome_counts` (a tally of the shadow outcome classification); `shadow_r_count`,
>   `shadow_win_rate`, `mean_shadow_gross_r`, `mean_shadow_net_r` from only the picks with a
>   definitive (`data_complete`) shadow R; `real_count`, `real_win_rate`,
>   `mean_real_official_r` from only the subset that actually traded and closed with a known
>   `official_r`. `continuation_decision` is always `null` today — a named hook for the 24-hour
>   review package, never fabricated. A statistic with no known input is `null`, never a
>   default of zero.

## What changed

- `src/catalyst_lab/pick_outcomes.py` (new): the bar/exit-walk simulation
  (`Bar`/`parse_bars`/`walk_to_exit`, shared with `unchanged_plan.py`), the full pick
  simulation (`simulate_pick`/`PickSimulation`), Decimal R arithmetic (`r_values`) and the fee
  assumption, read-only ledger reconstruction of every report-V3 pick's fate
  (`PickRecord`/`cycle_picks`, generalizing `research_context.py`'s own per-cycle,
  per-caller `_last_run` to every cycle and every agent — deliberately **not** a refactor of
  that module, to avoid touching a file several concurrent packages are also changing), the
  append-only event writer (`record_shadow_outcome`) and job runner
  (`run_shadow_outcome_job`/`JobSummary`), and the read views
  (`pick_outcome_page`/`pick_outcome_aggregates`/`rank_bucket`).
- `src/catalyst_lab/public_crypto_bars.py` (new): `PublicCryptoBarReader`, a keyless,
  paginated fetch of Alpaca's public `v1beta3/crypto/us/bars`; its own transport allows
  exactly that one GET route and refuses to send a credential-shaped header even if supplied.
- `src/catalyst_lab/unchanged_plan.py` (new): `LevelChange`/`replay_unchanged_plan`/
  `UnchangedPlanOutcome` (pure); `stop_target_changes` (`MANAGEMENT_PLAN_AUTHORIZED` + the
  prior `STATE` row), `maintenance_level_changes` (`MAINTENANCE_DECISION` `APPLIED`, from its
  own recorded levels), `maintenance_exit_changes` (`EXIT_FLAG_RESOLVED` `EXIT_AGREED`, from
  its flag's own recorded evidence) and `setup_level_changes` (their union, in time order);
  `unchanged_plan_comparisons` (the DB+bar-reader wiring for one setup, now over the union,
  with `BAR_FETCH_BUFFER` fixing the hold-deadline fetch boundary); cross-setup discovery and
  aggregation (`setups_with_level_changes`, `maintenance_replay_rows`/`_page`/`_aggregates`).
- `scripts/run_pick_shadow_outcomes.py` (new): the offline CLI, for a cycle, a date range or
  everything pending; reads only `MANAGED_DATABASE_URL` from the owner's existing private
  config (never `APCA_*`).
- `src/catalyst_lab/managed_service.py`: five new authenticated GET routes
  (`/api/v1/lab/cycles/{cycle_id}/picks`, `/api/v1/lab/results/picks` + `/aggregates`,
  `/api/v1/lab/results/maintenance` + `/aggregates`, none on the research-agent route
  allowlist — same access as the existing `/results`/`/results/aggregates`); a new `/results`
  + `/results.js` dashboard page (own inline styles; no shared `lab.css`/`lab.js`/`HTML`
  edits, to keep this package's footprint away from files other concurrent packages may also
  touch); `create_managed_app` gained optional `bar_reader` and `clock` parameters.
- `scripts/serve_managed_dashboard.py`: wires a real `PublicCryptoBarReader()` as `bar_reader`
  so the maintenance-replay routes are actually functional on the owner's read-only dashboard.
- `docs/API-CONTRACT.md`: documents the five new routes and both named methods.
- Tests (86, listed in the PHASES text above).

## Deviations and open items

1. **The real Alpaca `v1beta3/crypto/us/bars` endpoint has not been read from this
   environment.** Its shape (keyed bar rows, `t`/`o`/`h`/`l`/`c`/`v`, `next_page_token`
   pagination) is assumed from the *existing*, authenticated `scan_sources.CRYPTO_PATHS`
   usage of the very same route family (`research_context.py`'s own 24-hour-volume read
   already calls it, with credentials); whether it is genuinely reachable **without** any
   key, from wherever the scheduled job eventually runs, is not independently confirmed here
   — the same category of open item `docs/packages/fees-net-r.md` recorded for Alpaca's fee
   activities. The owner should run `scripts/run_pick_shadow_outcomes.py` once, unauthenticated,
   against a real recorded cycle and compare a few bars against the Alpaca dashboard before
   trusting it unattended.
2. **The shadow-outcome window (`generated_at` → packet expiry, then +24h) is this package's
   own modeling choice**, not a separately owner-ratified methodology; it is the most direct
   reading of plan 4.8's "did price reach its entry trigger before its validity ended," applied
   identically to every pick so the selected-vs-not comparison is fair. If the owner wants a
   different window (for example, starting only at Jev's selection time), it is a small,
   isolated change to `cycle_picks`'/`simulate_pick`'s callers.
3. **The maintenance-replay routes compute live, never from a persisted event** — a documented
   tradeoff (see the CONTRACT-RESOLUTIONS text above), acceptable at today's low review volume.
   If review volume grows, the natural next step mirrors `PICK_SHADOW_OUTCOME`: an offline job
   that appends one immutable replay event per change, with the routes reading from the ledger
   instead of fetching bars per request.
4. **The continue-or-exit hook (plan phase 6, the 24-hour review) is documented, not tested**,
   because that event kind does not exist in the codebase yet (confirmed: no code writes it).
   `unchanged_plan.py`'s own docstring and `CONTINUE_EXIT_DECISION` name the exact
   `LevelChange` shape that package should construct; the early-exit half of the same hook
   *is* now built and tested, against package maintenance's own `EXIT_FLAG_RESOLVED`.
5. **`/results`'s "today's picks" section** finds the latest tracked run by scanning the most
   recent few cycles client-side (`GET /cycles` then up to five `GET
   /cycles/{id}/picks` calls, stopping at the first non-empty one) rather than a dedicated
   "latest report-V3 cycle" endpoint — simple and correct, not the most efficient possible
   shape; fine at today's cycle volume.
6. **A real off-by-one was found and fixed while building the maintenance-replay routes** (see
   the CONTRACT-RESOLUTIONS text above, "Maintenance-replay routes and their bar-fetch fix"):
   it silently affected `run_shadow_outcome_job`'s own `HOLD_24H_EXIT` case too, before this
   follow-up -- no earlier commit in this package had a test that exercised a shadow pick's
   24-hour-exit path against a bar-reader fetch boundary specifically, so nothing else depended
   on the broken behavior. A targeted regression test now pins the fixed boundary down exactly
   (`test_job_computes_a_correct_24_hour_hold_exit_at_the_fetch_boundary`,
   `tests/test_pick_outcomes_ledger.py`; verified by hand to fail with `DATA_INCOMPLETE` when
   `BAR_FETCH_BUFFER` is reverted).
7. **Not built** (out of the stated scope): any change to `research_context.py`,
   `research_selection_topk.py`, `managed_execution.py`, `managed_analytics.py`,
   `managed_funnel.py`, `trade_maintenance.py` or `exit_flags.py` (all read only, via their
   existing public functions or event shapes); a migration (none needed); any live-order code
   path or the prohibited endpoint literal (never referenced anywhere in this package,
   including tests).

## Verification

- `XDG_DATA_HOME=~/.local/share/catalyst-wt-results ./run pytest -q` — see the branch head
  commit message for the exact full-suite count; run before every commit that touches source.
- `XDG_DATA_HOME=~/.local/share/catalyst-wt-results ./run ruff check src tests`
- Targeted subset re-run after every source change in this package:
  `tests/test_pick_outcomes.py tests/test_pick_outcomes_ledger.py
  tests/test_public_crypto_bars.py tests/test_unchanged_plan.py
  tests/test_run_pick_shadow_outcomes.py tests/test_managed_service_results.py
  tests/test_maintenance_replay.py tests/test_managed_service.py tests/test_managed_dashboard.py
  tests/test_managed_analytics.py tests/test_managed_measurement.py tests/test_managed_fees.py
  tests/test_maintenance_rules.py tests/test_trade_maintenance.py tests/test_maintenance_runtime.py
  tests/test_maintenance_session.py`
