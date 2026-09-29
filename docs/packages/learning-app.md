# Package learning-app — the learning loop's app and cloud half

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-28-learning-app`, from `work/2026-09-24-product-plan` at `15dc5b0` (schema
24; the live Railway deployment's branch with the jev-budget, watchdog and deploy fixes). Plan:
[LEARNING-LOOP-PLAN.md](../LEARNING-LOOP-PLAN.md) sections 2, 4, 5, 5b, 5c, 6 and 7, approved by
the owner on 2026-09-28 ("go with your recommendations, build it"), with the coordinator's shared
definitions of 2026-09-28 (the research checklist rules, forward-window grading, SKIPPED outlook
entries, `was_miss`). The research kit's half (`research_agent/`) is package learning-kit, built
against this package's contract. This file carries the text the coordinator merges into
`docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch.
**No migration** (schema stays 24): every new record is an immutable `lab.managed_events` row
with no setup, through the audited append.

Owner decisions of 2026-09-28 (the plan's section 9, accepted recommendations):

1. The nightly jobs run as a Railway cron service `jobs`, not inside the trader.
2. The minimum samples are exactly those of plan section 6.
3. The morning summary reaches the owner as a chat message: this package provides the owner
   commands a scheduled Claude task will call.
4. Muse sees only its own sanitized lessons; the results routes stay closed to agent tokens.

## Summary

- **Outlook** (`MARKET_OUTLOOK_V1`, `POST /api/v1/lab/market-outlooks`, research agents): every
  universe coin's expected direction and confidence (or SKIPPED with a reason), Bitcoin's and
  Ether's, and a cited market view. One immutable record, graded once on its forward 24-hour
  window.
- **Post-mortems** (`POST_MORTEM_V1`, `POST /api/v1/lab/post-mortems`): up to 30 cited causes
  about the agent's own closed trades and recorded movers, with whether the cause was public
  before the move started (a claim of `true` needs a source published before it).
- **Market reality** (`MARKET_REALITY_V1`, nightly): each New York day's move of every universe
  coin (return, excursions, move start, volume against 7 days), the movers, Bitcoin, Ether,
  sector and volume factors, the outlooks whose windows ended that day graded (hits,
  calibration, misses, false alarms) and each big mover's misses by agent.
- **Scorecard** (`DAILY_SCORECARD_V1`, nightly): 1, 7 and 30 days, overall and per agent, with
  counts beside every rate: the funnel with a reason for every drop, results after verified
  fees, Jev's selection on shadow outcomes, research craft, causes; overall also maintenance by
  decision type against the recorded replays, arms and costs.
- **Replays**: the jobs record `UNCHANGED_PLAN_REPLAY_V1` counterfactuals (method unchanged)
  once complete, and the new `DAY_REVIEW_DECISION_REPLAY_V1` for each 24-hour decision; actual R
  is always read live.
- **Lessons** (`RESEARCH_CONTEXT_V2` with `RESEARCH_LESSONS_V1`): the research context adds the
  caller's own scorecard lines, graded outlooks, the last seven days' movers and its pending
  post-mortems; never another agent's data, Jev's answers, the arm or costs.
- **Guidelines** (`MUSE_RESEARCH_GUIDELINES_V6`): V5 plus the learning loop as instructions,
  served by the context; V5 declarations stay accepted.
- **Weekly review** (`WEEKLY_REVIEW_V1`): the plan's five pre-registered tests (level moves per
  type), a seeded 90% bootstrap, `KEEP`, `PROPOSE` (with a contract-text draft) or
  `NOT_ENOUGH_DATA`.
- **The `jobs` cron** (`LEARNING_JOBS_V1`): 05:30 UTC daily, each step independent, exit 0; only
  the trader's `catalyst_risk` connection by reference, no broker or Jev key.
- **Owner commands** in the ops shell: `scorecard`, `market-review`, `weekly-review` over the
  read-only `catalyst_app` connection.
- **Event feed**: `GET /api/v1/lab/outputs`, open to research agents, leaves every learning
  record out for their credentials (decision 4); the status credential reads them all.
- **Watchdog false alarm fixed**: the status's `last_reconciliation_at` is the last completed
  clean reconciliation; `ready()` is unchanged.

## PHASES entry (paste as written)

### 2026-09-28 — The learning loop, app and cloud half: outlooks, post-mortems, the nightly reality, scorecard and weekly review, lessons in the research context (package learning-app) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a mock
Jev, canned public bars, the real Railway IaC SDK 3.11.0 evaluated offline. No broker, provider,
network, Railway or owner-ledger contact; no migration (schema stays 24); no SQL changed; nothing
places an order, changes a trading rule or reaches Jev (Jev's dossiers and questions are
unchanged). Owner decisions of 2026-09-28: a Railway cron `jobs`; plan 6's minimum samples; the
morning summary as a chat message; Muse sees only its own lessons.

- **Routes** (research agents' list; status and operator 403): `POST /api/v1/lab/market-outlooks`
  (`MARKET_OUTLOOK_V1`: every universe coin once, SKIPPED with a reason; report source rules,
  freshness, run slot, identity and sensitive screens; one immutable `MARKET_OUTLOOK` event) and
  `POST /api/v1/lab/post-mortems` (`POST_MORTEM_V1`: up to 30 items about the agent's own closed
  trades or recorded movers, each accepted or refused with a code; `knowable_before_move: true`
  needs a source published at or before the move's start or the entry fill). The event feed
  `GET /api/v1/lab/outputs` leaves the learning records out for research-agent credentials.
- **Nightly records** (`jobs`): `PICK_SHADOW_OUTCOME_V1` (existing), recorded
  `UNCHANGED_PLAN_REPLAY_V1` and `DAY_REVIEW_DECISION_REPLAY_V1` counterfactuals, `MARKET_REALITY`
  (the New York day's moves, movers, factors, forward-window outlook grades, misses by agent),
  `DAILY_SCORECARD` (1/7/30 days, overall and per agent, counts beside every rate) and, after each
  week, `WEEKLY_REVIEW` (five pre-registered tests, a seeded 90% bootstrap, KEEP, PROPOSE with a
  contract-text draft, or NOT_ENOUGH_DATA). Each keyed by day or week; two missed days caught up.
- **Research context V2**: `lessons` (`RESEARCH_LESSONS_V1`, the caller's own and sanitized) and
  `MUSE_RESEARCH_GUIDELINES_V6` (V5 plus the learning loop; V5 declarations stay accepted).
- **Cloud**: `.railway/railway.ts` service `jobs` (same image, `30 5 * * *` UTC, restart NEVER,
  only the trader's `catalyst_risk` connection by reference); `cloud_config.jobs_config` refuses
  broker and Jev keys; `cloud_runtime jobs` exits 0 after independent steps (30-minute end);
  owner commands `scorecard`, `market-review`, `weekly-review` in the ops shell (read-only
  `catalyst_app`).
- **Status**: `last_reconciliation_at` is the last completed clean reconciliation (the watchdog's
  false `RECONCILIATION_STALE` inside a pass is gone); `ready()` is unchanged.

Evidence: the tests listed under "Validation" below. A real Railway cron run, Muse's first outlook
and post-mortem, and a week of real records are still to come.

## CONTRACT-RESOLUTIONS text (paste as written)

### The learning loop: `MARKET_OUTLOOK_V1`, `POST_MORTEM_V1`, `MARKET_REALITY_V1`, `DAILY_SCORECARD_V1`, `RESEARCH_CONTEXT_V2`, `RESEARCH_LESSONS_V1`, `MUSE_RESEARCH_GUIDELINES_V6`, `WEEKLY_REVIEW_V1`, `DAY_REVIEW_DECISION_REPLAY_V1`, `LEARNING_JOBS_V1` (2026-09-28, package learning-app)

Owner approval of 2026-09-28 of `docs/LEARNING-LOOP-PLAN.md` ("go with your recommendations,
build it"): a Railway cron `jobs`; the minimum samples of plan section 6 exactly; the morning
summary as a chat message; Muse's own sanitized lessons only, the results routes closed to agent
tokens. The coordinator's shared definitions of 2026-09-28 fix the checklist rules, the
forward-window grading, SKIPPED entries and `was_miss`. Named versions under the owner's ruling
of 2026-09-24. They are measurement definitions and research-side records: no trading rule, no
order, nothing sent to Jev; Jev's dossiers and questions, `UNCHANGED_PLAN_REPLAY_V1`,
`PICK_SHADOW_OUTCOME_V1`, `JEV_SPEND_METER_V1` and every other version keep their definitions.
No migration; every record below is one immutable `lab.managed_events` row with no setup,
appended through the audited append. Thresholds are version constants; a change is a new
version.

**`MARKET_OUTLOOK_V1`** (`learning_intake.py`, `POST /api/v1/lab/market-outlooks`). A
research-agent credential only; the body's agent block (the report's) names the credential's
agent. Fields: `schema_version`; `outlook_id` (UUID); `generated_at` (0 to the report age limit,
60 s, before receipt); `run_slot` (a scheduled run, `generated_at` at most the grace before it);
`horizon_hours` 24; `agent`; `market` = `summary` (≤ 600), `btc` and `eth` (`direction`
UP/DOWN/FLAT, `confidence` 0–1), 0–12 `factors` (`name` ≤ 80, `note` ≤ 300, optional source),
0–20 `events` (`at` or null, `what` ≤ 300, optional source); `coins` = exactly one entry per coin
of the current tradable universe (at most 230, the most coins one report can name): UP, DOWN or
FLAT with `confidence`, optional `expected_move_pct` (0–100) and 0–4 `reasons` (kind NEWS, EVENT,
TECHNICAL, FUNDAMENTAL or MARKET, text ≤ 200, optional source), or SKIPPED with `skip_reason` ≤
200 and nothing else. Sources are the report's. Refused whole, nothing stored, on a schema,
schedule, coverage or source failure (`INVALID_MARKET_OUTLOOK` with field codes), sensitive
content, the agent ID in agent-written text (`AGENT_IDENTITY_IN_OUTLOOK`), staleness, or changed
content under a recorded ID. Record `MARKET_OUTLOOK`, key `market-outlook:<agent_id>:<outlook_id>`,
with `received_at`, the forward window `[received_at, received_at + 24 h)`, its `grading_day`
(the New York day whose end is the first day end at or after the window's end) and the universe
it was checked against. An exact retry returns the stored receipt.

**`POST_MORTEM_V1`** (`learning_intake.py`, `POST /api/v1/lab/post-mortems`). `note_id`,
`generated_at` (the report age limit), `agent`, 1–30 items: `subject` (`TRADE` + `setup_id`, or
`MOVER` + `symbol` + New York `day`), `cause` (COIN_NEWS, MARKET_WIDE, NO_NEWS, SURPRISE),
`knowable_before_move` (true, false or null), `summary` ≤ 400, 0–4 report sources (COIN_NEWS and
MARKET_WIDE need one), `pre_move_technicals` ≤ 300. Each item is checked in order and refused on
its own: schema; duplicate subject; a TRADE must exist, belong to the caller (the legacy
credential also owns unattributed legacy trades), have an entry fill and be closed; a MOVER must
be a mover of a recorded `MARKET_REALITY_V1` day; sources; the agent ID in `summary`,
`pre_move_technicals` or a source ID; and `knowable_before_move: true` needs a cited source whose
`published_at` is at or before the reference time (a mover's `move_start_at`, a trade's first
entry fill). A note with no acceptable item is refused whole. Record `POST_MORTEM`, key
`post-mortem:<agent_id>:<note_id>`, with the accepted items (`subject_key`, `reference_at`) and
every item result; the latest accepted item for a subject is the one later readers use.

**`MARKET_REALITY_V1`** (`market_reality.py`). One record per New York day D, key
`market-reality:<D>`, recorded only after D ends plus 5 minutes, only when every coin's bars were
read, never twice. The universe: the latest universe a report-V3 intake or an outlook recorded,
read before D ended, plus every coin of an outlook graded that day. Bars: Alpaca's public crypto
bars (no key), 1-minute for prices, 1-hour for volume. Per coin: the return from the day's first
1-minute open to its last close (`return_pct`); `max_up_pct` and `max_down_pct` from that open to
the day's highest high and lowest low; `move_start_at` = for a rising day (close ≥ open) the bar
of the lowest low at or before the first bar of the day's high, for a falling day the bar of the
highest high at or before the first bar of the day's low, the latest such bar on a tie; volume =
the day's hourly base volume against the 7 previous New York days' daily average, and in USD
(volume × the bar's VWAP, else its close). A coin without bars is `NO_BARS`, never guessed.
Movers: the five largest positive returns, the five largest negative returns and every |return| ≥
5% (`big_move`). Factors: Bitcoin's and Ether's day, each sector's mean return (the coin's crypto
classification, else `ALPACA_CRYPTO_SECTORS_V1`, else `CRYPTO_OTHER`), total USD volume against
its 7-day average. Grades: every `MARKET_OUTLOOK_V1` whose `grading_day` is D, on its forward
window: per coin the return from the first 1-minute open at or after `received_at` to the last
close before the window's end; actual direction FLAT when |return| < 1.5%, else UP or DOWN; a
hit when the outlook's direction equals it, whatever the confidence (SKIPPED and unmeasured coins
are not compared); calibration by confidence bucket [0, 0.2), [0.2, 0.4), [0.4, 0.6), [0.6,
0.8), [0.8, 1]; a miss at |return| ≥ 5% while the outlook said FLAT, the opposite or SKIPPED; a
false alarm for UP or DOWN with confidence ≥ 0.6 and an actual direction other than it; Bitcoin
and Ether calls graded the same way. A big mover `was_miss` for an agent (`missed_by`) when the
agent's latest outlook received at or before the mover's `move_start_at` whose 24-hour window
covers it said FLAT, the opposite direction or SKIPPED, or when no such outlook exists
(`outlook_agents`: agents with an outlook in the 30 days before D's end; any other agent has none).

**`DAILY_SCORECARD_V1`** (`scorecard.py`). One record per New York day D, key
`daily-scorecard:<D>`, recorded only after D ends, never twice. Windows `1d`, `7d`, `30d`: the New
York days ending with D. A report-V3 cycle belongs to a window by its `run_slot`, a trade by its
state's `closed_at`, a Jev call by its receipt's `started_at`. `overall` and one set of lines per
agent (`LEGACY_UNATTRIBUTED` for records without one); operator `ENGINEERING_TEST` setups enter
nothing (`engineering_excluded`). Lines: `funnel` (cycles, picks sent, accepted, intake
rejections by code, ranked, vetoed by veto reason, not ranked by code, selected, ranked but not
selected, skipped by reason, declined at admission by code, expired before admission, awaiting
admission, admitted, expired untriggered, invalidated by reason, risk-rejected, watching,
triggered (a `TRIGGER_CONFIRMED`), filled, closed, open); `results` (closed trades, wins and
losses by gross P&L, win rate, `r_net` = official R with verified fees and no `LAB_FIXTURE` fee
source: count, mean, sum; fees unverified; exit reasons; mean hold hours from the first entry
fill); `selection` (the cycle's picks by `rank_bucket`: selected = TOP_K and REPLACEMENT, passed =
NOT_SELECTED, vetoed, not ranked; per group picks, shadow outcomes recorded and pending,
triggered, the mean and hit rate of complete shadow net R, and selected minus passed); research
craft (`fill_rate_by_distance`: |agent price − entry trigger| / agent price in 0–1%, 1–2%, 2–3%,
3%+ or UNKNOWN; `results_by_timeframe`: 3600, 7200, 14400, 21600, 86400 s as 1h, 2h, 4h, 6h, 1d,
else OTHER, NONE without bars; `results_by_rule`: A or B when exactly one of `rule-A` / `rule-B`
appears in the pick's `why_over_peers` or confidence basis, else UNDECLARED; `results_by_kind`;
`results_by_sector`; each bucket: picks, admitted, filled, fill rate (filled ÷ admitted), shadow
recorded, triggered and trigger rate, shadow R count, mean and hit rate), `excerpt_drop_rate`
(intake refusals `INVALID_SOURCE_EVIDENCE` per pick sent), `stale_news_vetoes` (`NEWS_STALE_YES`
per ranked NEWS or BOTH pick), `dossier_size_rejections` (`DOSSIER_OVER_BUDGET` per pick sent);
`causes` (notable closed trades: exit kind STOP for `BROKER_EXIT`, `STOP_LIMIT_NOT_FILLED`,
`STOP_CROSSED_DURING_REPLACE`, EARLY_EXIT for `EARLY_EXIT_AGREED`, TWENTY_FOUR_HOUR_EXIT for
`DAY_REVIEW_EXIT`, `DAY_REVIEW_DEADLINE_EXIT`, `HOLD_24H_EXIT`, `TIME_EXIT`, or a win above 1.5R
by official R, else gross P&L over the same denominator; the cause of each one's latest accepted
post-mortem). `overall` adds `maintenance` (every recorded replay of the window's trades by
decision type STOP_RAISE, TARGET_RAISE, STOP_AND_TARGET_RAISE, EARLY_EXIT, DAY_REVIEW_CONTINUE,
DAY_REVIEW_EXIT: count, the difference actual `r_net` − counterfactual net R where both are
known, its mean, helped and hurt), `arms` (per randomized arm: closed, wins, win rate, `r_net`
count, mean and sum, managed minus control), `costs` (Jev calls and request bytes by kind with the
meter's estimate, priced with the trader's latest recorded `JEV_SPEND_GUARD_CONFIGURED` estimate,
else the meter's defaults; verified cash fees; Railway usage is not in the ledger: null) and, in
`1d`, the per-trade lines (with arm) and the causes' items. A rate or mean with no input is null,
never zero; counts are always given.

**`RESEARCH_CONTEXT_V2`** (`research_context.py`). Every `RESEARCH_CONTEXT_V1` field unchanged,
`report_format` naming `MUSE_RESEARCH_GUIDELINES_V6`, plus `lessons`: `RESEARCH_LESSONS_V1` for a
research credential, null for the status credential. **`RESEARCH_LESSONS_V1`** (`lessons.py`),
read at request time: the caller's own lines of the latest `DAILY_SCORECARD_V1` (`1d`, `7d`,
`30d`; null where it has none); its outlook grades (the latest in full with its window, the last
7 days' graded outlooks, 7- and 30-day sums by grading day, calibration without the internal
sums); the last seven reality days' movers and factors (market data, the same for every agent);
its trades closed in the last 7 days and the latest reality day's movers with no accepted
post-mortem of its own (`was_miss` from the reality's `missed_by`, or any big mover when it had no
outlook). Never another agent's lines, grades, notes or trades, never a Jev answer or receipt,
the arm, costs or an `ENGINEERING_TEST` setup; never sent to Jev. `available: true`; a record
that cannot be read into lessons serves `{lessons_version, agent_id, as_of, available: false,
code: LESSONS_UNAVAILABLE}` and the context still answers; a database error answers 503 as
before.

**`MUSE_RESEARCH_GUIDELINES_V6`** (`muse_guidelines.py`). V5's text byte for byte followed by
"Learning (V6, 2026-09-28)" (SHA-256
`075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d`): lessons for emphasis, never
coverage (the whole universe, about 20 picks a run, no trading rule to change, MARKET_WIDE and
SURPRISE cases left out of craft lessons, days of one regime compared); the morning outlook for
every coin and how it is graded; the review of notable trades and movers under the
knowable-before-move and citation rules, and the news-dating check; the research checklist
(TECHNICAL patterns: an occurrence is a coin carrying the pattern's state tag at the start of the
New York day, a hit is that coin being a mover that day, lift = hit rate ÷ the universe's mover
share on the same days, ACTIVE at ≥ 10 occurrences with lift ≥ 2.0 overall and over the last 10,
RETIRED when an ACTIVE pattern's last-10 lift falls below 1.25; NEWS_TYPE, EVENT, MARKET_FACTOR
and SOURCE_QUALITY patterns from post-mortems: an occurrence is the factor recorded as the cause
of a mover or notable trade, a hit is `knowable_before_move: true`, ACTIVE at ≥ 10 occurrences
with a hit rate ≥ 50% overall and over the last 10, RETIRED when the last-10 hit rate falls below
40%; re-activation under the same rule; every change logged; ordering only, never coverage); the
lessons applied named in `why_over_peers` and the run notes, `agent_version` bumped when the
method changes. The research context serves V6 from this release; V1–V5 stay importable and
unchanged; report intake keeps recording whatever version a report declares, so V5 declarations
stay accepted, as V4 ones were during the V4-to-V5 switch.

**`WEEKLY_REVIEW_V1`** (`weekly_review.py`). One record per Monday-to-Sunday New York week, key
`weekly-review:<Sunday>`, recorded by the first nightly run after the week ends (normally
Monday's); the owner's command computes one on demand without recording. Evidence: everything
recorded before the week's end (a pick by its `generated_at`, a trade by its `closed_at`), each
trade's official R read live (verified fees, no `LAB_FIXTURE` source), `ENGINEERING_TEST` never.
Tests (effect positive = the current rule adds value; minimum samples of plan 6):
`SELECTION_VALUE` (mean complete shadow net R of selected minus passed picks; 40 and 40),
`MANAGEMENT_VALUE` (mean `r_net` of JEV_MANAGED minus FIXED_EXIT trades; 30 per arm),
`LEVEL_MOVES_STOP_RAISE`, `_TARGET_RAISE`, `_STOP_AND_TARGET_RAISE` (mean of `r_net` minus the
recorded `UNCHANGED_PLAN_REPLAY_V1` net R, per type; 30 changes), `DAY_REVIEW_DECISIONS` (mean of
`r_net` minus the recorded `DAY_REVIEW_DECISION_REPLAY_V1` net R; 20 decisions, with CONTINUE and
EXIT shown apart), `VETOES` (mean of passed minus vetoed picks; 15 vetoed). A group also needs
two values. Interval: a percentile bootstrap, 2,000 resamples with replacement (two groups
independently, paired differences as one), values exact to 1e-8, drawn by SplitMix64 (unbiased
by rejection) seeded with the first 8 bytes, big-endian, of SHA-256 of
`WEEKLY_REVIEW_V1|<week_end>|<test_id>`; the 90% interval is the 100th and the 1,900th sorted
resample effects. Verdict: `NOT_ENOUGH_DATA` below a minimum; `PROPOSE` when the interval's upper
end is below zero; else `KEEP`; `evidence` ADDS_VALUE (lower end above zero), INCONCLUSIVE or
LOSES_VALUE. A PROPOSE carries a draft naming the next version of the rule concerned (the most
common recorded selection, maintenance or 24-hour review version, `_V<n+1>`) with the evidence,
the proposed change (selection: the agent's own order among picks Jev did not veto; management:
no maintenance changes and no Jev exit flags; a level move type: no longer offered; 24-hour
decisions: exit at the review, or continue unless both sides say exit, by the worse decision;
vetoes: a definite wrong answer lowers the score instead of vetoing) and its scope. A draft
changes nothing until the owner approves it and it ships as a package.

**`DAY_REVIEW_DECISION_REPLAY_V1`** (`learning_replays.py`). Each `DAY_REVIEW_DECISION` whose
outcome is CONTINUE or EXIT, against the decision not taken, on Alpaca's public 1-minute bars: a
CONTINUE against exiting at the decision (the open of the first bar at or after it, within 15
minutes); an EXIT against continuing 24 hours on the levels in force at the decision (first touch
of the stop or target, a bar reaching both resolved as the stop, else the first open at or after
24 hours). R from the admitted max entry over (max entry − admitted initial stop) less the
assumed taker fee on both legs (`ALPACA_CRYPTO_TIER1_TAKER_BOTH_LEGS_V1`). Recorded once complete
(`DAY_REVIEW_DECISION_REPLAY`, key `day-review-decision-replay:<decision event_seq>`), never with
the trade's actual R.

**`LEARNING_JOBS_V1`** (`learning_jobs.py`, `cloud_runtime jobs`, the Railway cron `jobs` at
05:30 UTC). One run: `shadow_outcomes` (`PICK_SHADOW_OUTCOME_V1` over the report-V3 cycles of the
last 40 days), `maintenance_replays` (every complete `UNCHANGED_PLAN_REPLAY_V1` outcome recorded
once as `UNCHANGED_PLAN_REPLAY`, key `unchanged-plan-replay:<change event_seq>`, method
unchanged, and `DAY_REVIEW_DECISION_REPLAY_V1`), `market_reality` and `scorecard` (the previous
New York day, and the two days before it when missing), `weekly_review` (the latest finished
week when missing). Each step independent and logged with its code; a step not started after 20
minutes is skipped (`JOB_TIME_LIMIT`); the process ends after 30 minutes; exit 0 (2 for a refused
configuration). Its only credential is the trader's `catalyst_risk` connection; broker, Jev and
every other trading credential are refused by name.

**Visibility.** A research agent reads the learning records only as its own `lessons`. The event
feed `GET /api/v1/lab/outputs` (on the research agents' routes) leaves out every learning kind
(`MARKET_OUTLOOK`, `POST_MORTEM`, `MARKET_REALITY`, `DAILY_SCORECARD`, `WEEKLY_REVIEW`,
`UNCHANGED_PLAN_REPLAY`, `DAY_REVIEW_DECISION_REPLAY`) for a research-agent credential, in the
query itself so pages and cursors never stall on them; the status credential, and the single
legacy token when no role tokens are configured, read every event as before. No other event's
visibility changes.

**Status (not a rule).** `GET /api/v1/lab/status` `last_reconciliation_at` is the instant the last
reconciliation pass completed clean (`last_clean_reconciliation_at` in the runtime), never
cleared while a pass runs or after a failure; the watchdog's `RECONCILIATION_STALE` therefore
means no clean completion for 90 seconds. `ready()` and `entry_ready` still require the current
clean reconciliation exactly as before.

## Files

New: `src/catalyst_lab/learning_intake.py`, `market_reality.py`, `learning_replays.py`,
`scorecard.py`, `lessons.py`, `weekly_review.py`, `learning_jobs.py`, `learning_summary.py`;
`tests/learning_fixtures.py`, `tests/test_learning_intake.py`, `test_market_reality.py`,
`test_learning_guidelines.py`, `test_scorecard.py`, `test_lessons.py`, `test_weekly_review.py`,
`test_learning_jobs.py`, `test_reconciliation_status.py`; this file.

Changed: `src/catalyst_lab/managed_service.py` (the two routes on the research agents' list;
the outputs feed without the learning kinds for research agents), `managed_store.py` (`outputs`
with `exclude_kinds`, the unfiltered query unchanged),
`managed_app.py` (the learning intake; `public_status`, `last_reconciliation_at` from the last
clean reconciliation), `managed_runtime.py` (`last_clean_reconciliation_at`, heartbeat-volatile),
`research_context.py` (V2, lessons, serves V6), `muse_guidelines.py` (V6),
`public_crypto_bars.py` (`bars(..., timeframe)` with `1Min` and `1Hour`; `minute_bars` unchanged),
`cloud_config.py` (`jobs_config`), `cloud_runtime.py` (`jobs`, `scorecard`, `market-review`,
`weekly-review`), `cloud_entry.py` (component `jobs`, no volume), `.railway/railway.ts` (service
`jobs`); `docs/API-CONTRACT.md`, `docs/MUSE-CONNECTION.md`, `docs/MUSE-GUIDELINES.md`,
`docs/RAILWAY-DEPLOYMENT.md`, `docs/OPERATIONS-RUNBOOK.md`. Tests changed:
`tests/test_research_context.py` (context V2), `tests/test_day_review_rules.py` (the served-version
assertion moved to the V6 test), `tests/test_railway_spec.py` (service `jobs`),
`tests/test_muse_connection_examples.py` (routes, limits, V6 example, learning examples; the
stub store's `outputs` signature), `tests/test_managed_ops.py` (the same stub).

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-learning-app`).

- New tests: `test_learning_intake.py` 31, `test_market_reality.py` 14,
  `test_learning_guidelines.py` 4, `test_scorecard.py` 12, `test_lessons.py` 4,
  `test_weekly_review.py` 6, `test_learning_jobs.py` 10, `test_reconciliation_status.py` 2;
  plus 1 in `test_railway_spec.py` and 3 in `test_muse_connection_examples.py` (one test, two
  body-limit cases).
- The IaC file evaluated with the real SDK `railway@3.11.0` offline (the owner's
  `.railway/node_modules`): service `jobs` compiles to `deploy` = `{cronSchedule: "30 5 * * *",
  restartPolicyType: "NEVER", startCommand: "python -m catalyst_lab.cloud_entry jobs",
  numReplicas: 1}` with its two variables. TypeScript's strict check was not re-run (no `tsc` in
  that install).
- Full suite at `6127482` (the last code commit; this record's only later change is this
  line): **4,121 passed, 1 skipped** (`test_cloud_provision.py:220`, skipped by design since
  migration 024 merged) in 13 min 14 s. `./run ruff check src tests`: all checks passed.

## Deviations and choices (please confirm)

1. **Forward-window grading** (coordinator's correction): an outlook is graded on
   `[received_at, received_at + 24 h)` in the reality of the day that window ends, the night
   after; the plan's "night reality" stays the New York day for movers and factors. A morning
   outlook's grade therefore reaches the lessons about 42 hours after it was written.
2. **Full coverage**: every universe coin exactly once (SKIPPED allowed), refused otherwise; cap
   230 coins and a 2 MiB body (the coordinator's fix: never below the universe bound).
3. **Knowable before the move** is checked by the app: `true` needs a cited `published_at` at or
   before the move's start; for a TRADE the reference is its first entry fill (the plan did not
   define one for trades).
4. **The replays are recorded** as events by the jobs (the plan's "unchanged-plan replay" step),
   so the scorecard and the weekly review never refetch history; actual R stays live. The 24-hour
   decisions needed their own counterfactual (`DAY_REVIEW_DECISION_REPLAY_V1`): V1's hook for
   `CONTINUE_EXIT_DECISION` was never built, and extending V1 would have changed a recorded
   method.
5. **Catch-up**: the reality and the scorecard also record the two days before the previous one
   when missing, and the weekly review records a finished week on the first run after it, so one
   failed night loses nothing. Beyond three days a day is not recorded automatically.
6. **Cumulative evidence** for the weekly tests (plan 1: "checks the accumulated evidence"); the
   week itself is summarized from its Sunday scorecard.
7. **On demand is read-only**: the ops commands compute a preview when nothing is recorded (the
   ops role cannot write); only the cron records.
8. **`EARLY_EXIT`** appears in the scorecard's maintenance but is not a weekly test (plan 6 has no
   minimum for it).
9. **Excerpt drop rate** counts intake refusals for source evidence; drops inside an agent's kit
   are not in the ledger. **Rule A/B** is read from the kit's wording (`rule-A` / `rule-B`).
10. **The cron runs at 05:30 UTC** (the brief), after New York midnight all year, rather than the
    plan's 04:30.
11. **`RECONCILIATION_STALE`** now fires after 90 s without a clean completion instead of at any
    read inside or after an unclean pass; entries are gated exactly as before.
12. **A refused jobs configuration exits 2** (a visible failed run); otherwise the run exits 0.
13. **The event feed hides the learning records from research agents.** Found in the final
    review: `GET /api/v1/lab/outputs` is on the research agents' routes and returns every
    event, so without a filter any agent would have read the other agents' outlooks, notes and
    grades, the scorecards' arms and costs. Owner decision 4 (own sanitized lessons only) now
    holds there too; the research kit does not read the feed.

## Deploy (for the coordinator)

No migration, no new trader or ops variable. Stage the merged commit; `railway config plan` must
show exactly *create service `jobs`*; `railway config apply`; then `railway up` the trader, ops
and jobs from the same staged directory (the experiment page is unchanged). Checks on Railway:
the plan shows no change afterwards; the jobs service's cron `30 5 * * *` and restart Never; after
the first 05:30 UTC, `railway logs --service jobs` shows `JOBS STARTING`, five `JOBS STEP` lines,
`JOBS DONE` and a completed run (the reference `${{trader.RISK_DATABASE_PASSWORD}}` resolved);
`railway ssh --service ops -- python -m catalyst_lab.cloud_runtime scorecard` works (the SSH
session sees `AUDIT_DATABASE_URL`); `status` shows `last_reconciliation_at` on every read; the
research context answers `RESEARCH_CONTEXT_V2` and `MUSE_RESEARCH_GUIDELINES_V6`. Detail:
`docs/RAILWAY-DEPLOYMENT.md` 7.6.

## Open items

1. Only Railway can show the cron running and ending, the cross-service reference resolving, the
   ops SSH session's variables and Alpaca's public bars from Railway's network.
2. Package learning-kit: the kit's outlook, lessons and post-mortem steps; `research_agent` still
   declares V5 (accepted).
3. The scheduled Claude task that turns `scorecard`, `market-review` and `weekly-review` into the
   owner's morning and Monday messages (the coordinator's).
4. A day older than the catch-up window is never recorded automatically, and there is no command
   to record one (the ops role is read-only); a one-off would run in the trader's shell.
5. A catch-up night fetches up to about 200 public-bar requests; a rate-limit refusal fails that
   day, which the next night retries.
6. The weekly review reads every closed trade's measurement each week: fine for months, to
   revisit at thousands of trades.
7. Ledger storage (plan section 10) is unchanged by this package: the learning records add tens
   of kilobytes a day.
8. Pre-existing and unchanged: the feed and `GET /api/v1/lab/positions` still show research
   agents everything else they showed before (other agents' report events, Jev's reviews,
   `PICK_SHADOW_OUTCOME` rows, each position's `arm`). Whether to narrow them is a separate
   decision.
