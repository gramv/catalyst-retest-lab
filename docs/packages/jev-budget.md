# Package jev-budget — the owner's monthly Jev budget (`JEV_SPEND_METER_V1`, `JEV_SPEND_GUARD_V1`, `CRYPTO_MAINTENANCE_V3`)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-28-jev-budget`, from `work/2026-09-24-product-plan` at `f3e95b8` (schema
24), with the work branch merged at `9ef5227`. Owner, 2026-09-28: "my monthly budget for this
including jev is 50$", then "I CAN DO 70 AT MAX": $70 a month in total, split by the coordinator
into Jev at most **$50** (`JEV_MONTHLY_BUDGET_USD`) and Railway a $15 alert with a $20 hard
limit. The owner also wants Jev to monitor every open trade every minute
(`CRYPTO_MAINTENANCE_V2`). This file carries the text the coordinator merges into
`docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch. No
migration.

## Summary

- **Meter (`JEV_SPEND_METER_V1`, `jev_budget.py`).** Derived, never written: every TypeSafe call
  is already in `lab.jev_requests` (the exact bytes posted) and `lab.jev_receipts` (one per
  attempt). A receipt with an HTTP status, or a transport failure, is a call; it costs
  `octet_length(request_json) / JEV_BYTES_PER_TOKEN` tokens (default 3 bytes a token) at
  `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD` (default 0.042), in the New York calendar month and
  day of its `started_at`. Month-to-date, today, the last 24 hours, a month-end projection at the
  current cadence and the per-minute projection that decides the tier, all exact Decimal. A
  restart rebuilds the same totals from the ledger; afterwards only receipts with a higher
  `event_seq` are read.
- **Guard (`JEV_SPEND_GUARD_V1`).** Tiers from the budget B, month-to-date MTD and the
  per-minute projection P1: `EXHAUSTED` from MTD ≥ 98% of B; otherwise throttling when P1 > 98%
  of B, held until P1 ≤ 93%; throttled `TIGHT` from MTD ≥ 90%, else `THROTTLED`; otherwise
  `NORMAL`. Each change is a `JEV_BUDGET_TIER_CHANGED` event and an off-host alert
  (`JEV_BUDGET_THROTTLED`, `_TIGHT`, `_EXHAUSTED`, for 30 minutes); each configuration a runtime
  starts with is a `JEV_SPEND_GUARD_CONFIGURED` event. An unreadable meter keeps the last decision
  for two minutes, then fails closed (`UNAVAILABLE`, `JEV_BUDGET_STATUS_UNAVAILABLE`).
- **`CRYPTO_MAINTENANCE_V3`.** V2 with the routine cadence set by the tier: every completed
  minute (`NORMAL`, `BAR_1M`), 5 minutes (`THROTTLED`, `BAR_5M`), 15 minutes (`TIGHT`,
  `BAR_15M`), none (`EXHAUSTED`). Event reviews run in every tier below `EXHAUSTED`. Admission
  records V3 in the maintained arm; V1 and V2 setups keep their recorded cadence.
- **Owner's view.** The runtime status's `jev_budget` (`GET /api/v1/lab/status`, and
  `cloud_runtime status` in the cloud): tier, month-to-date, today, both projections, budget,
  thresholds, spend by kind, request bytes and estimated tokens. The public page is unchanged.
- **Configuration.** `JEV_MONTHLY_BUDGET_USD=50`, `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD=0.042`
  and `JEV_BYTES_PER_TOKEN=3` in the deploy example, so `.railway/railway.ts` passes them to the
  trader; required in railway mode (and on a Mac whenever `MANAGED_MANAGEMENT_REVIEWS=ENABLED`),
  refused by name when missing or invalid.

What $50 buys (month-long simulation of the guard with the measured request sizes: a
maintenance review 13,565 bytes, $0.00019; a research run of 20 picks a day; four event reviews
per trade a day; 30-day month):

| Maintained trades open around the clock | Without the guard | With the guard | Cadence |
| --- | --- | --- | --- |
| 2 | $16.66 | $16.66 | every minute all month |
| 4 | $33.13 | $33.13 | every minute all month |
| 5 | $41.36 | $41.36 | every minute all month (the most $50 covers) |
| 6 | $49.59 | $46.66 | 5 minutes for the first 2.2 days, then every minute |
| 7 | $57.83 | $46.65 | 5 minutes for the first 7.3 days, then every minute |

In a 31-day month: $17.22, $34.23, $42.74 and $46.64 for 2, 4, 5 and 7. With the number of
open trades changing every day (random 3–8, 5–8, 0–7) the guard changed tier two to six times a
month and ended the month between $28.63 and $43.28.

## PHASES entry (paste as written)

### 2026-09-28 — The owner's monthly Jev budget: `JEV_SPEND_METER_V1`, `JEV_SPEND_GUARD_V1`, `CRYPTO_MAINTENANCE_V3` (package jev-budget) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, Jev requests and receipts
appended through the restricted `catalyst_jev` store, the fake paper venue, scripted bars, a mock
Jev transport, a stub guard where a test sets the tier, and a month-long simulation of the guard
(scratch, not committed). No broker, provider, network or owner-ledger contact; no migration
(schema stays 24); no SQL changed. Every broker POST, DELETE and PATCH keeps its exact one-use
five-second authorization. Owner, 2026-09-28: $70 a month at most, Jev at most $50
(`JEV_MONTHLY_BUDGET_USD`), Railway a $15 alert and a $20 hard limit.

- **Meter** (`jev_budget.SpendMeter`, `JEV_SPEND_METER_V1`): derived from `lab.jev_requests` and
  `lab.jev_receipts`; a receipt with an HTTP status or a transport failure is a call priced at
  its exact request bytes / 3 tokens × $0.042 per million (both configurable); New York month
  and day of `started_at`; incremental by receipt `event_seq`, rebuilt exactly after a restart.
- **Guard** (`jev_budget.SpendGuard`, `JEV_SPEND_GUARD_V1`): `EXHAUSTED` from 98% of the budget
  spent; throttling above a 98% per-minute projection, held until 93%; `TIGHT` while throttled
  from 90% spent; else `NORMAL`. `JEV_BUDGET_TIER_CHANGED` and `JEV_SPEND_GUARD_CONFIGURED`
  events; `UNAVAILABLE` (fail closed) after two minutes without a readable meter.
- **`CRYPTO_MAINTENANCE_V3`**: V2 with the routine cadence 1, 5 or 15 completed minutes by tier,
  none when exhausted; events below `EXHAUSTED`; recorded at admission in the maintained arm.
  Each request's trigger and Jev identity record the guard's facts and `routine_weight`, which
  the meter reads back so throttling never lowers the per-minute projection.
- **Status and alerts**: `jev_budget` in the runtime status and `cloud_runtime status`; the
  watchdog raises `JEV_BUDGET_THROTTLED`, `_TIGHT` or `_EXHAUSTED` for 30 minutes after a change
  and `JEV_BUDGET_STATUS_UNAVAILABLE`.
- **Settings**: `JEV_MONTHLY_BUDGET_USD` (50), `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD` (0.042),
  `JEV_BYTES_PER_TOKEN` (3) in the deploy example; required in railway mode and, on a Mac, the
  budget whenever reviews are `ENABLED`; refused by name.

Evidence: `tests/test_jev_budget_rules.py` (10), `tests/test_jev_budget_meter.py` (9),
`tests/test_jev_budget_flows.py` (14), `tests/test_jev_budget_config.py` (6) and one test in
`tests/test_railway_spec.py`: 40 new tests. Five existing tests changed where they pinned the
version admission records (now V3), the runtime tests' environment helper gains the budget, the
shared fixtures give maintenance a spend guard, and the V2 flow tests admit under V2
(`v2_admission`), which keeps V2 exactly. Full suite: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### The owner's monthly Jev budget: `JEV_SPEND_METER_V1`, `JEV_SPEND_GUARD_V1`, `CRYPTO_MAINTENANCE_V3` (2026-09-28, package jev-budget)

**Authority.** Owner, 2026-09-28: "my monthly budget for this including jev is 50$", raised the
same day to "I CAN DO 70 AT MAX": $70 a month in total, which the coordinator split into Jev at
most $50 (`JEV_MONTHLY_BUDGET_USD=50`) and Railway a $15 alert and a $20 hard limit. The owner
also wants Jev to review every open trade every minute (`CRYPTO_MAINTENANCE_V2`). Named versions
under the owner's 2026-09-24 ruling. `CRYPTO_MAINTENANCE_V1` and `_V2` (records, readers,
cadences and events), `JEV_MANAGED_POSITION_CONTEXT_V4`/`_V5`, `_QUESTIONS_V4`/`_V5`,
`MAINTENANCE_ANSWER_RULE_V2`, `CRYPTO_24H_REVIEW_V1`/`_V2`, `EARLY_EXIT_AGREEMENT_V1`, the
selection rules, the risk versions, the authorization gate, every SQL function and every stored
record keep their definitions; no migration. The 2026-09-26 decision of no daily Jev call cap
stands: nothing caps calls; the monthly budget changes only the routine maintenance cadence of
trades admitted under V3.

**Why a new maintenance version, not a guard over V2.** V2's record says a review at every
completed minute. A runtime guard over V2 would maintain setups that recorded V2 differently from
their record, which the named-version rule forbids. V3 records at admission that its routine
cadence follows `JEV_SPEND_GUARD_V1`; the guard's configuration (budget, price, bytes per token)
is deployment configuration, recorded in its own events, and each review records the tier it
ran under. V2 setups still open when V3 is deployed keep every minute; their spend counts.

**`JEV_SPEND_METER_V1`** (`jev_budget.SpendMeter`). A provider call is a `lab.jev_receipts` row
with `http_status` present or outcome `TRANSPORT_FAILURE` (retries, HTTP errors and rate-limit
refusals included; `CIRCUIT_OPEN`, `CREDENTIAL_UNAVAILABLE` and an `EXPIRED` attempt without a
status sent nothing). Its request bytes are `octet_length(request_json)` of its request (the
exact ASCII body `jev_review` posts). Tokens = bytes / `JEV_BYTES_PER_TOKEN`; USD = tokens ×
`JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD` / 1,000,000 (Decimal, 50 digits). A call belongs to the
New York calendar day and month of the receipt's `started_at`. Kind by question set:
`*_PICK_QUESTIONS_*` and `SKEPTIC_QUESTIONS_*` SELECTION, `MUSE_JEV_COMPARATIVE_QUALITY_*`
QUALITY, `JEV_MANAGED_POSITION_QUESTIONS_*` MAINTENANCE, `JEV_DAY_REVIEW_QUESTIONS_*`
DAY_REVIEW, `JEV_EARLY_EXIT_QUESTIONS_*` EARLY_EXIT, `PROVIDER_HEALTH_*` HEALTH_PROBE, else
OTHER. A call's weight is its request identity's `spend_guard.routine_weight` (an integer 1–15)
or 1. At start the meter reads the calls since the earlier of the month's start and 24 hours ago,
newest first by receipt `event_seq` in chunks, stopping one hour past that bound (receipts are
appended when their attempt ends, within a review deadline of its start); afterwards only
receipts with a higher `event_seq` (appended under the audit lock, so never visible out of
order). Snapshot at `now`: month-to-date (calls, bytes, USD, by kind), today, the last 24 hours'
bytes W and weighted bytes W1, days left in the month d (exact seconds to the next New York
month over 86,400), projection = MTD + USD(W) × d, per-minute projection P1 = MTD + USD(W1) × d.

**`JEV_SPEND_GUARD_V1`** (`jev_budget.SpendGuard`; record `policy_record()`: reserve 0.02,
throttle above 0.98, normal at or below 0.93, tight from 0.90, window 86,400 s, cadences NORMAL
60, THROTTLED 300, TIGHT 900, EXHAUSTED none, timezone America/New_York). Evaluated at most every
5 s by the maintenance pass and the status. With B the budget and the last recorded tier as
"previous":

1. `EXHAUSTED` when MTD ≥ B × 0.98 (the last 2% is a reserve for selection, 24-hour reviews and
   early exits, which the guard never stops).
2. Throttling: when the previous tier is none or `NORMAL`, P1 > B × 0.98; otherwise P1 > B × 0.93.
3. Throttling: `TIGHT` when MTD ≥ B × 0.90, else `THROTTLED`.
4. Otherwise `NORMAL`.

P1 counts a routine review made every k minutes as k per-minute reviews, so throttling does not
lower it and the tier does not oscillate; the hysteresis keeps demand noise from flipping it.
Rationale for the numbers: the plan line is the budget less the reserve (98%), so a month that
fits per-minute never ends in `EXHAUSTED`; `TIGHT` only while the per-minute projection does not
fit (at 90% spent with a fitting projection the reviews stay per-minute, "while the budget
allows it"); 5 and 15 minutes are the existing 5-minute grid and V1's cadence. A change of tier
appends `JEV_BUDGET_TIER_CHANGED` (no setup; key `jev-budget-tier:<previous event seq>:<tier>`;
body `version`, `tier`, `rule`, `previous_tier`, `previous_event_seq`, `at`,
`review_bar_seconds`, `budget_usd`, `thresholds_usd`, `estimate` and the snapshot's public
fields). Each configuration a runtime starts with that differs from the last recorded one
appends `JEV_SPEND_GUARD_CONFIGURED` (`configuration`: the policy record and the estimate). A
meter that cannot be read keeps the last decision for 120 s, then the guard answers
`UNAVAILABLE` (not a tier, never recorded): guarded trades are not reviewed. The status section
`jev_budget` is described in `docs/API-CONTRACT.md`. The watchdog raises
`JEV_BUDGET_THROTTLED`, `JEV_BUDGET_TIGHT` or `JEV_BUDGET_EXHAUSTED` while `tier_since` is at most
1,800 s old (one off-host `/fail` per change; `NORMAL` raises nothing) and
`JEV_BUDGET_STATUS_UNAVAILABLE` for an unavailable or unreadable section. Settings:
`JEV_MONTHLY_BUDGET_USD` (0.01–100,000, at most two decimals), `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD`
(0.000001–1,000, default 0.042), `JEV_BYTES_PER_TOKEN` (1–10, at most three decimals, default 3);
plain decimals only. Required in railway mode; on a Mac the budget is required whenever
`MANAGED_MANAGEMENT_REVIEWS=ENABLED` (without reviews no guard is built and Jev is asked nothing
about open trades).

**`CRYPTO_MAINTENANCE_V3`** (`crypto_maintenance.py`; record: `CRYPTO_MAINTENANCE_V2`'s with
`policy_id` `CRYPTO_MAINTENANCE_V3`, `spend_guard` `JEV_SPEND_GUARD_V1`,
`throttled_review_bar_seconds` 300 and `tight_review_bar_seconds` 900; `review_bar_seconds` 60 is
the `NORMAL` cadence). Scope: V2's (a report-V3 crypto setup in the `JEV_MANAGED` arm records it
at admission from this package on; the `FIXED_EXIT` arm is never maintained). Each maintenance
pass takes one decision of the guard; a V3 trade's routine bar is 60, 300 or 900 s by tier
(reasons `BAR_1M`, `BAR_5M`, `BAR_15M`, UTC-aligned, V2's served-bar and in-flight-skip rules at
that cadence), plus V2's event reviews (milestones, near target or stop, news, a Bitcoin shock)
with V1's one-minute floor and near-target exemption. `EXHAUSTED`, or a guard that cannot decide
(none configured, failing, or unreadable), sends nothing for V3 trades: one
`POSITION_REVIEW_SKIPPED` per lifecycle and episode (`reason` `JEV_BUDGET_EXHAUSTED` or
`JEV_BUDGET_UNAVAILABLE`, `policy_id`, `spend_guard`, `code`); triggers are still recorded and
are served when reviews resume; stops, targets and protection run unchanged. A V3 request's
trigger and Jev identity add `spend_guard` (`version`, `tier`, `tier_event_seq`,
`review_bar_seconds`, `routine_weight`: the cadence in minutes for a request whose reasons include
the routine bar, else 1); what Jev is sent (context V5, questions V5) and how its answer is read
(`MAINTENANCE_ANSWER_RULE_V2`) are V2's.

## Files

New: `src/catalyst_lab/jev_budget.py` (settings, meter, tiers, guard, status),
`tests/test_jev_budget_rules.py`, `tests/test_jev_budget_meter.py`,
`tests/test_jev_budget_flows.py`, `tests/test_jev_budget_config.py`, this file.

Changed: `src/catalyst_lab/crypto_maintenance.py` (`CRYPTO_MAINTENANCE_V3`,
`MaintenancePolicyV3`, `guarded`, `BAR_5M`; admission records V3),
`src/catalyst_lab/trade_maintenance.py` (`spend_guard`, the per-pass decision, V3's cadence,
withheld reviews, the guard's facts in each request), `src/catalyst_lab/managed_runtime.py`
(`spend_guard`, `jev_budget` in the status, the heartbeat signature, `build_runtime_from_env`
wiring and configuration hash), `src/catalyst_lab/managed_app.py` and `managed_service.py` (the
status field), `src/catalyst_lab/managed_ops.py` (settings, preflight, `jev_budget_alarms`),
`src/catalyst_lab/cloud_config.py` (required in the cloud), `src/catalyst_lab/cloud_runtime.py`
(`status` prints it), `deploy/private-paper.example.json` (the three settings),
`scripts/agent_research_session.py` (the session's own guard at the example's budget),
`tests/maintenance_fixtures.py` and `tests/day_review_fixtures.py` (a fixture guard,
`v2_admission`), `tests/test_answer_rules.py`, `tests/test_answer_rules_day_review.py`,
`tests/test_answer_rules_flows.py`, `tests/test_maintenance_rules.py`,
`tests/test_maintenance_session.py`, `tests/test_trade_maintenance.py`,
`tests/test_managed_runtime.py`, `tests/test_railway_spec.py`, `docs/RAILWAY-DEPLOYMENT.md`,
`docs/OPERATIONS-RUNBOOK.md`, `docs/MUSE-CONNECTION.md`, `docs/API-CONTRACT.md`.

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-jev-budget`).

- New tests (40): rules 10 (V3's exact record, admission, readers and cadence by tier; 5-minute
  bars and events in between; settings as exact Decimals with refusals naming the variable; cost
  from request bytes; kinds; every tier boundary and the hysteresis; New York months, days and
  DST; the watchdog's 30-minute alert window and fail-closed cases); meter 9 (only provider
  calls priced, at their exact bytes and by kind; month rollover on the New York calendar with
  the window spanning it; incremental reads, a restart and a chunked load giving identical
  totals; routine weights in the per-minute projection; each tier from real spend with the
  status fields and the audit chain; a tier recorded once and each configuration change;
  throttling held until 93% across a restart; an unreadable meter kept 120 s then fail closed;
  a guard without a ledger and eight threads); flows 14 (every minute, 5 and 15 minutes by tier;
  a tier change at the next routine bar; events at once when throttled or tight; exhausted sends
  nothing, records the milestone, skips once and resumes with the missed milestone; exhausted
  leaves the stop-limit resting and the target sells without Jev; no guard or a failing guard
  withholds V3 only; a V2 setup keeps every minute; V2 and V3 in one pass; the real guard
  exhausted by real spend, recorded and alerted; throttled reviews read back with weight 5; the
  runtime status and heartbeat signature; `cloud_runtime status`); configuration 6 (the example's
  values; the cloud refusing each missing or invalid setting by name; the Mac preflight; the
  runtime factory); `test_railway_spec` 1 (the trader gets the three settings through the
  example's derivation, the cloud profile requires them, ops never holds them).
- Existing suites re-run during the work (all passed): the maintenance, answer-rules, day-review,
  early-exit, gap-resume, replay, Muse-connection identity, public-experiment views, managed ops,
  app, service and runtime, unattended safety, cloud config and runtime, Railway spec, session
  harness and selection B1/B2 suites.
- **Full suite at `36bd10c`** (this branch with the work branch merged at `9ef5227`; the
  later commit changes this file only): **4,031 passed, 1 failed, 1 skipped** (16 min 42 s),
  about 3 GB free. The one failure, `tests/test_experiment_page.py::test_the_demo_dashboard`
  (`today` picks 0, expected 20), is a wall-clock dependency outside this package: its demo
  places the latest research run at 12:00 UTC while the page counts the New York day, so it
  fails from midnight to 10:00 New York. It fails the same way on the untouched work branch
  (`9ef5227`, run at 01:10 New York). Skipped: `tests/test_cloud_provision.py:220`, as on the
  work branch.
- `ruff check src tests`: all checks passed.

## Deviations and choices (please confirm)

1. **A new maintenance version** rather than a guard over V2 (see the contract text): V2
   setups open when V3 is deployed keep every minute.
2. **Tier thresholds tuned.** The brief's example throttled at a projection above the budget and
   tightened at 90% spent. Here the plan line is 98% (the budget less the reserve), throttling
   holds until 93%, and `TIGHT` needs the projection still over the line: a month that fits
   per-minute never ends exhausted or tight.
3. **The per-minute projection decides the tier**, not the current pace: a throttled review
   counts as the per-minute reviews it replaced (`routine_weight`), so throttling does not
   switch itself off. The displayed `projection_usd` is the current pace.
4. **Exhausted stops event reviews too**, as the brief's "event reviews run in every tier below
   exhausted" reads; triggers are recorded and reviewed when reviews resume.
5. **Calls counted conservatively**: every attempt with an HTTP status (429s and 5xx included) or
   a transport failure.
6. **Alerts are windowed** (30 minutes after a change), so a long throttled or exhausted stretch
   does not keep the dead-man check failing or repeat every reminder period; the tier stays in
   the status. A return to `NORMAL` raises nothing.
7. **The Mac requires the budget whenever reviews are `ENABLED`**; the cloud always.
8. **No migration**: derived from existing rows; events carry the rest. The cloud ledger has no
   migration command yet, so a migration would have blocked deployment.

## Open items

- **TypeSafe's usage page is the source of truth.** The meter is an estimate: bytes over a
  conservative 3 bytes a token, every attempt counted. After the first days, divide the status's
  `month_request_bytes` (or the difference between two readings) by the input tokens TypeSafe
  reports for the same period and set `JEV_BYTES_PER_TOKEN` to it, slightly lower to stay
  conservative (runbook "Monthly Jev budget"). If TypeSafe bills by UTC month, its month and the
  meter's New York month differ by four or five hours at each end.
- **No real month metered yet**: no real-provider or Railway run of the guard; the first cloud
  month is the check of the estimate, the tier changes and the alerts.
- **Rollout order on an existing trader**: the release and the three variables must land
  together (the old release refuses them as unknown, the new one refuses to start without them);
  `docs/RAILWAY-DEPLOYMENT.md` 7.6 gives the order. A first deployment sets them with the first
  `railway config apply` (5.3).
- **Found: storage, not Jev dollars, is the cloud's constraint.** A maintenance review appends
  about 65 KB to the ledger and its audit chain (measured on the fixture venue with 11 KB
  requests; about 75 KB at the real 13.5 KB), about 110 MB a day per maintained trade at the
  per-minute cadence and 16–19 GB a month at the full $50. Railway's Hobby volumes stop at 5 GB:
  the Postgres volume holds about nine to eleven days of five per-minute trades, and 14 daily
  backups on the 4 GB ops volume fill sooner. Needs a decision (fewer maintained trades, a lower
  cadence, the Pro plan or a smaller per-review footprint) before the ledger nears 5 GB.
- **Found: Railway's hard limit takes every workload offline** ("all your workloads will be
  taken offline"), the trader included; only the resting stop-limits then protect open positions.
  The $15 alert is the signal to act before it.
- **Found: `jev_calls_today` scans two whole tables every status call.** `Gate1Runtime.calls_today`
  counts `lab.jev_requests` and `lab.jev_receipts` by an unindexed time on every `status()` (the
  heartbeat every 5 s, the watchdog every 30 s), and the public status view does the same per page
  load. At per-minute volumes these scans grow by about 300,000 rows a month. Not changed here;
  the meter reads incrementally instead.
- **Volatile demand under-uses the budget**: with the open-trade count changing daily the
  simulated months ended at $28–$43 of $50 while some days were throttled, because the tier looks
  at the last 24 hours' demand. A longer window or a budget-pacing variant would be a new
  version.
- **Sessions** (`scripts/agent_research_session.py`) run V3 with their own guard at the example's
  budget over the session database, which stays `NORMAL`: they do not see the owner's real spend.
