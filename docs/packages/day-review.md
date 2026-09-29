# Package day-review — the 24-hour review and early exits (plan phase 6)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-day-review`, from `work/2026-09-24-product-plan` at `8b96ec0` (schema
23). Plan: `docs/CRYPTO-AGENT-LOOP.md` 4.6.3 (early exit, both sides) and 4.6.4 (the 24-hour
review) in full, with 4.6.5-4.6.8 and the section-5 rows about 24-hour reviews, silent agents and
Jev down (owner-approved 2026-09-26). Builds on `docs/packages/maintenance.md` (phase 5: the
monitoring Jev, its options, apply checks and `exit_flags`) and `docs/packages/crypto-size-hold.md`
(`CRYPTO_24H_HOLD_V1`, whose forced 24-hour exit this package replaces for the maintained arm
only). This file carries the text the coordinator merges into `docs/PHASES.md` and
`docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch. **No migration** (schema
stays 23): append-only events and state fields only.

## PHASES entry (paste as written)

### 2026-09-27 — The 24-hour review and early exits: the agent and Jev decide together whether an open crypto trade continues (plan phase 6, package day-review) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
scripted bar source, a mock Jev transport, the real agent routes in process and the session
harness's `SESSION_SIMULATION` venue on a shifted review clock. No broker, provider, network or
owner-ledger contact; no migration (schema stays 23); no SQL changed. The day reviews never call
the broker: an exit is an exit request and a raised stop a `stop_replace`, and the protection
loop's cancel, PATCH and market sell each keep their exact one-use five-second authorization.
Only report-V3 crypto setups admitted in the maintained `JEV_MANAGED` arm from now on are
affected: the 30% `FIXED_EXIT` control arm still exits at 24 hours (`CRYPTO_24H_HOLD_V1`), and
V1, V2, B1, B2, operator engineering setups and every setup admitted before this version
(maintained ones included) keep today's behaviour. A real-Jev session and a supervised paper
session are still to do.

- **Versions.** `CRYPTO_24H_REVIEW_V1` (`crypto_holding.py`, the maintained arm's holding policy,
  recorded at admission), `JEV_DAY_REVIEW_CONTEXT_V1` / `JEV_DAY_REVIEW_QUESTIONS_V1` and
  `JEV_EARLY_EXIT_CONTEXT_V1` / `JEV_EARLY_EXIT_QUESTIONS_V1` (`day_review_dossier.py`),
  `AGENT_REVIEW_ANSWER_V1`, `AGENT_EXIT_FLAG_V1` and `EARLY_EXIT_AGREEMENT_V1`
  (`day_review.py`), and `MUSE_RESEARCH_GUIDELINES_V5` (V4 plus how an agent answers reviews and
  flags; the research context serves it).
- **The clock.** T (`day_review_at`) is 24 hours after the first buy fill, then every 24 hours
  after a continue; the state counts `continuations`. Nothing exits at T by itself.
  `hard_exit_at` is a fail-safe, T + 80 minutes (Jev 30 + discussion 15 + Jev 30 + 5 minutes'
  grace): if no review has decided by then the protection loop sells (`DAY_REVIEW_DEADLINE_EXIT`).
- **The review** (`trade_review.DayReviews`, the runtime's research loop, its own periodic
  task). At T − 30 min `DAY_REVIEW_REQUESTED` goes to the proposing agent (else the configured
  research agent whose report arrived last; none: Jev alone) with the trade now, every level
  change, all news since entry and the code's options; the agent pulls it from `GET
  /api/v1/lab/reviews` or the research context's `pending_reviews` and answers by T
  (`AGENT_REVIEW_ANSWER_V1`: continue or exit, what changed, what it expects, what would prove it
  wrong, optional suggested stop and target, 0-8 sources). At T Jev answers over the original
  pick, the trade and the agent's answer (never its name or confidence): the trade reason
  intact/weakened/broken, whether the agent's case holds, continue or exit, and the stop and
  target options (phase 5's). Agreement decides; a disagreement opens one discussion round (the
  agent sees Jev's answers and replies within 15 minutes, Jev answers finally; still
  disagreeing, or no reply: exit). No agent answer: Jev alone. Jev unable to answer within 30
  minutes, or an answer code cannot use: exit. One `DAY_REVIEW_DECISION` per review.
- **Continue and exit.** Continue: a new 24-hour plan (T and the fail-safe 24 hours on, the
  count + 1, no limit); Jev's options pass phase 5's checks or are refused while the trade
  continues on its levels; a raised stop is replaced at the broker by the protection loop. Exit:
  `exit_requested` `DAY_REVIEW_EXIT`, the stop-limit cancelled and the position sold at market.
- **Early exits** (`EARLY_EXIT_AGREEMENT_V1`). The agent's own flag (`POST
  /api/v1/lab/positions/{setup_id}/exit-flag`) goes to Jev at once; a maintenance Jev flag goes
  to the agent at once (`EXIT_FLAG_ASKED`, answered at `/api/v1/lab/exit-flags/{flag_id}/answer`).
  Both say exit (or both have flagged): `EARLY_EXIT_AGREED`, a market sell. Otherwise, or no
  answer in 15 minutes: the trade keeps its stop and target. One `EARLY_EXIT_DECISION` each.
- **Hard exits and restarts.** A stop, target, the daily halt, operator flatten or any exit
  request discards a pending review and ends pending flags; a closed trade's leftovers are swept.
  Everything resumes from the ledger: a Jev request asked before a crash is answered from its
  recorded receipt (never a second vote), decisions and exits happen once.
- **Measurement hook** (plan 4.6.8). Every decision records the unchanged-plan `LevelChange`
  fields (`change_kind` `CONTINUE_EXIT_DECISION` or `EARLY_EXIT`, time, old and new stop and
  target, whether it exited, bid/ask/mid/last), both sides' answers and Jev's receipts;
  `trade_review.decision_records` reads them for the results package.
- **Session harness.** `execute --simulate-prints --hold-open`, `review --at request|review |
  --minutes N [--bid P]` on a shifted review clock, the agent's `reviews`, `review-answer`,
  `flag-answer` and `exit-flag`, and fixture answers (`day_review`, `day_review_final`,
  `early_exit`).

Evidence: `tests/test_day_review.py` (30), `tests/test_early_exit.py` (11),
`tests/test_day_review_rules.py` (16), `tests/test_day_review_session.py` (4): 61 new tests,
listed under "Validation" below. Three `tests/test_crypto_size_hold.py` tests now pin or read the
randomized arm and one `tests/test_selection_topk_v2.py` assertion moved to the V5 test. Full
suite and ruff: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### The 24-hour review and early exits: `CRYPTO_24H_REVIEW_V1`, `JEV_DAY_REVIEW_CONTEXT_V1`, `JEV_DAY_REVIEW_QUESTIONS_V1`, `AGENT_REVIEW_ANSWER_V1`, `EARLY_EXIT_AGREEMENT_V1`, `AGENT_EXIT_FLAG_V1`, `JEV_EARLY_EXIT_CONTEXT_V1`, `JEV_EARLY_EXIT_QUESTIONS_V1`, `MUSE_RESEARCH_GUIDELINES_V5` (2026-09-27, package day-review)

Owner decisions of 2026-09-26 (`docs/CRYPTO-AGENT-LOOP.md` 4.6.3 and 4.6.4, approved): after 24
hours the research agent and Jev decide together whether a trade continues or exits, with one
discussion round when they disagree (disputed: exit), Jev alone when the agent is silent, exit
when Jev cannot answer within 30 minutes, a continued trade getting a new 24-hour plan with no
continuation limit and no midnight close; either side may start an early exit, which needs both
within 15 minutes; the 30% control group keeps the fixed 24-hour exit. Named versions under the
owner's 2026-09-24 ruling. `CRYPTO_24H_HOLD_V1`, `CRYPTO_MAINTENANCE_V1` and its context,
question and flag versions (`EARLY_EXIT_FLAG_V1` included), `JEV_MANAGED_RISK_V3`, the
authorization gate, every SQL function and every stored record keep their definitions; no
migration.

**Scope.** A setup admitted from a packet whose `market` is `CRYPTO` and whose
`report_schema_version` is `AGENT_RESEARCH_REPORT_V3` records `holding_policy` in the admitting
transaction by its randomized arm (fixed at admission): the exact `CRYPTO_24H_REVIEW_V1` record
below in the `JEV_MANAGED` arm, `CRYPTO_24H_HOLD_V1` in the `FIXED_EXIT` arm (unchanged). The
review, the early-exit agreement and every rule below key off that recorded state only (a state
with altered numbers is refused). A setup that recorded `CRYPTO_24H_HOLD_V1` (the control arm,
and every report-V3 crypto setup admitted before this version, maintained or not) keeps it: its
24-hour exit, and for a maintained one its Jev flags pending until the trade closes, as before.

**`CRYPTO_24H_REVIEW_V1`** (`crypto_holding.py`; record: `review_interval_seconds` 86400,
`request_lead_seconds` 1800, `agent_reply_seconds` 900, `jev_answer_seconds` 1800,
`review_window_seconds` 4500, `deadline_grace_seconds` 300, `jev_call_deadline_seconds` 60,
`jev_retry_seconds` 60, `exit_reason` `DAY_REVIEW_EXIT`, `deadline_exit_reason`
`DAY_REVIEW_DEADLINE_EXIT`, `context_version` `JEV_DAY_REVIEW_CONTEXT_V1`, `question_version`
`JEV_DAY_REVIEW_QUESTIONS_V1`, `answer_version` `AGENT_REVIEW_ANSWER_V1`, `early_exit_version`
`EARLY_EXIT_AGREEMENT_V1`). Like `CRYPTO_24H_HOLD_V1`: no New York day policy, entries until the
setup's own expiry, no midnight flatten. F = the first recorded buy fill, c = `continuations`.

*Clock.* At the first fill the state records `day_review_at` T = F + 86,400 × (c + 1) s,
`continuations` 0 and `hard_exit_at` = T + 4,500 + 300 s (the fail-safe). A later fill or a
restart never moves them; a delayed earlier fill tightens both. Nothing exits at T. At
`hard_exit_at` with no exit requested the exit reason is `DAY_REVIEW_DEADLINE_EXIT` (state,
protection plan, CANCEL and EXIT decisions, CLOSED reason); an exit already requested keeps its
own reason. Stops, targets, the −3% daily halt and operator flatten close the trade at any time.

*Review n* (n = c + 1; `review_id` = uuid5(URL, `day-review:<setup>:<lifecycle>:<n>`)), run by
`trade_review.DayReviews` about once a second for open trades under the version, one ledger read
per trade (events by setup and kind); every write is a keyed append-only event under the shared
ledger lock:

1. *Request* (at T − 1,800 s, or at the first pass after it): `DAY_REVIEW_REQUESTED` to the
   addressee: the proposing agent (the setup's `agent.agent_id`) while its credential is
   configured; else the configured research agent with the latest `RESEARCH_STARTED`; else none
   (`NO_ACTIVE_AGENT`). The configured agents are the legacy credential's `muse` and every
   research-agent credential (the app sets them at start; unknown, the proposing agent is asked).
   Body: `review_id`, `review_number`, `continuations`, `review_at`, `requested_at`,
   `agent_answer_due_at` = T, `addressee` (`agent_id`, `basis`), `answer_schema`,
   `answer_route`, `trade` (entry, quantity, stop, target, original levels, risk per coin =
   M − S0, opened time, hours in the trade, bid, ask, quote time, unrealized P&L, P&L in R),
   `level_changes`, `news_since_entry` (every source of the lifecycle's `POSITION_NEWS`),
   `options` (the stop and target options of `CRYPTO_MAINTENANCE_V1`'s rules at the request, or
   null when bars or the quote are unavailable), `original_pick`.
2. *The agent's answer* (`AGENT_REVIEW_ANSWER_V1`, below): round FIRST while now < T and Jev has
   not been asked; recorded `DAY_REVIEW_AGENT_ANSWER`. Only the addressee's credential may answer.
3. *Jev at T* (`DAY_REVIEW_JEV_REQUEST` then `DAY_REVIEW_JEV_RESULT`): the
   `JEV_DAY_REVIEW_CONTEXT_V1` state and `JEV_DAY_REVIEW_QUESTIONS_V1`, purpose
   `ENGINEERING_TEST`, each attempt's deadline min(ask + 60 s, the round's window end). Result
   `ANSWERED` (usable), `UNUSABLE` (answered, but see "reading") or `FAILED` (no answer: breaker,
   provider, deadline, a context that could not be built: quote, bars or broker metadata
   unavailable, or the 11,000-byte budget unsatisfiable). A failed attempt is retried 60 s after
   it, inside the round's window. The receipts are verified against the exact context, question
   set, request and deadline, as `CRYPTO_MAINTENANCE_V1`'s are.
4. *Decision after Jev's first answer*: unusable, EXIT (`JEV_ANSWER_UNUSABLE`); no agent answer,
   Jev's decision (`AGENT_SILENT_JEV_ALONE`, or `NO_AGENT_JEV_ALONE` without an addressee); the
   same decision as the agent's, that decision (`AGREED`); otherwise a discussion round.
5. *Discussion* (`DAY_REVIEW_DISCUSSION`, `reply_due_at` = opened + 900 s): the agent's pending
   request shows round DISCUSSION with Jev's answers (choice and top probability), their fixed
   meanings and the options Jev chose; its one reply (round DISCUSSION) is due by `reply_due_at`.
   No reply by then: EXIT (`DISCUSSION_NO_AGENT_REPLY`; a reply recorded at the deadline is never
   overruled). Then Jev's final answer (window: the reply + 1,800 s): unusable, EXIT; the same
   decision as the reply, that decision (`AGREED_AFTER_DISCUSSION`); otherwise EXIT
   (`DISAGREED_AFTER_DISCUSSION`).
6. *Timeouts*: no usable or unusable first answer by T + 1,800 s, or final answer by the reply +
   1,800 s: EXIT (`JEV_UNAVAILABLE`). `MANAGED_MANAGEMENT_REVIEWS=DISABLED`: Jev is never asked;
   at T the decision is EXIT (`JEV_REVIEWS_DISABLED`).

*Applying* (one `DAY_REVIEW_DECISION` per review, key `day-review-decision:<review_id>`, in the
transaction that changes the state). The trade closed or in another lifecycle: `DISCARDED`
(`POSITION_CLOSED_DURING_REVIEW`); an exit already requested: `DISCARDED`
(`EXIT_IN_PROGRESS_DURING_REVIEW`). A CONTINUE within 60 s of `hard_exit_at` becomes EXIT
(`REVIEW_WINDOW_EXCEEDED`). EXIT: `exit_requested` `DAY_REVIEW_EXIT`; the protection loop cancels
the stop-limit and sells at market, each under its own one-use authorization, one exit order per
state revision. CONTINUE: `continuations` n, `day_review_at` T + 86,400 s, `hard_exit_at` that
plus 4,800 s; Jev's chosen stop and target options (from the recorded request's options) pass,
in order, a fresh quote, no stop replacement in progress, account risk evaluable, the stop and
target unchanged since Jev was asked (`LEVELS_CHANGED_SINCE_REVIEW`) and
`crypto_maintenance.check_change` (the answer under 60 s old, the increment, a new stop above the
old one, not reached by any bid since the ask and at most bid × 0.995, a new target above the bid
and the old target and not reached since the ask); if they pass, the target changes and a raised
stop is `stop_replace` (`change_id` the review, path `PATCH_REPLACE`) replaced at the broker by
the protection loop exactly as a maintenance raise; if not, `level_change` `REFUSED` with the
code and the trade continues on its levels. Body: `outcome` CONTINUE, EXIT or DISCARDED, `code`,
`levels_before`, `levels_after`, `level_change` (`APPLIED`, `REFUSED`, `NONE`),
`level_change_code`, `stop` and `target` (`old`, `new`, `option_id`, `bases`),
`next_review_at`, `hard_exit_at`, `continuations_before`/`_after`, `quote` at the decision (bid,
ask, mid, last and their times and source), `agent` (addressee and each round's answer: event,
agent ID, answer ID, decision, time), `jev` (every attempt: round, request, status, code,
receipts, answer, chosen options) and `measurement` (below).

*Hard exits* (plan 4.6.5): any exit request (stop-limit fallback, target, daily halt, operator
flatten, a raised stop crossed during its replacement, an agreed early exit) discards the
pending review at the next pass; a closed trade's pending review and flags are discarded and
ended by a sweep (once after start, then for each trade that leaves the open set). A restart
resumes from the ledger: a request asked before it is answered from its recorded receipt, or,
without one and after its deadline plus 5 s, recorded `FAILED` (`REQUEST_INTERRUPTED`) and
retried inside the window.

**`JEV_DAY_REVIEW_CONTEXT_V1`** (`day_review_dossier.py`, ≤ 11,000 encoded bytes): every section
of `JEV_MANAGED_POSITION_CONTEXT_V4` (the original pick, Jev's selection answers, the trade now,
every level change and why, including a review's raised levels, reason `DAY_REVIEW`; recent 15-
minute and 1-hour bars and the coin against Bitcoin; news since entry; the options) with
`context_version` replaced, `review_trigger` `{reasons: [DAY_REVIEW], review_number, round}`,
plus `review` (`round` FIRST or FINAL, `review_number`, `continuations`, `hours_in_trade`,
`agent_answer_status` ANSWERED, NO_ANSWER or NO_AGENT), `agent_answer` (the agent's first
answer: decision, the three texts, suggested stop and target, at most 3 of its sources with
excerpts cut to 300 characters; null when there is none) and, in the final round, `discussion`
(`your_first_answer`: Jev's first choices and top probabilities with their meanings;
`agent_reply`, as `agent_answer`). Never the agent's name, token or confidence; the agent's texts
and source IDs are refused at intake if they name it, and news since entry is shown without its
source IDs (position news does not check them). Over budget, the agent's sources are
dropped first (the last listed first), then V4's ladder; the agent's texts, the thesis and the
disproof are never shortened; still over, the attempt fails.

**`JEV_DAY_REVIEW_QUESTIONS_V1`** (stage TRACKING; fixed texts in `day_review_dossier.py`,
template hashes pinned in `tests/test_day_review_rules.py`): `trade_reason` (INTACT, WEAKENED,
BROKEN), `agent_case` (HOLDS, DOES_NOT_HOLD; asked only when `agent_answer_status` is ANSWERED),
`decision` (CONTINUE, EXIT), `stop_option` and `target_option` (KEEP or an option ID, each
option's text its price, basis, bar age, distance from the bid and R from entry, code-computed).
Every choice offers Insufficient evidence. *Reading* (code): an Insufficient-evidence or tied
answer (`UNCERTAIN_JUDGMENT`), EXIT with an option other than KEEP or CONTINUE with BROKEN
(`CONTRADICTORY_REVIEW_ANSWERS`), an option never offered (`UNKNOWN_OPTION`) or a question set
other than the one asked (`INVALID_REVIEW_ANSWER`) is unusable.

**`AGENT_REVIEW_ANSWER_V1`**: exactly `schema_version`, `answer_id` (UUID; an exact retry
returns the stored answer, changed content under it is refused), `decision` (CONTINUE or EXIT),
`what_changed` (1-600 characters), `next_24h` (1-600), `proves_wrong` (1-400), optional
`suggested_stop` and `suggested_target` (positive decimal strings, CONTINUE on a review only),
optional `sources` (0-8, the position-news format). Refused: a text or source ID naming the
answering agent (whole word, any case; `AGENT_IDENTITY_IN_ANSWER`), credentials, e-mail or 0x
addresses (`SENSITIVE_EVIDENCE_REJECTED`), more than 12,000 bytes of JSON (`EVIDENCE_TOO_LONG`).

**`EARLY_EXIT_AGREEMENT_V1`** (`day_review.py`; record: `answer_window_seconds` 900,
`flag_version` `EARLY_EXIT_FLAG_V1`, `agent_flag_version` `AGENT_EXIT_FLAG_V1`, `answer_version`
`AGENT_REVIEW_ANSWER_V1`, `context_version` `JEV_EARLY_EXIT_CONTEXT_V1`, `question_version`
`JEV_EARLY_EXIT_QUESTIONS_V1`, `exit_reason` `EARLY_EXIT_AGREED`, `jev_call_deadline_seconds` 60,
`jev_retry_seconds` 60). Applies to trades under `CRYPTO_24H_REVIEW_V1`, with
`EARLY_EXIT_FLAG_V1`'s record (`exit_flags.py`, unchanged: one pending flag per side and
lifecycle, `answer_due_at` = raised + 900 s, the trade's levels never changed by a flag):

- *The agent flags* (`AGENT_EXIT_FLAG_V1` at `POST /api/v1/lab/positions/{setup_id}/exit-flag`,
  only by the agent that answers for the trade, see the addressee rule; `EXIT_FLAG_RAISED` side
  AGENT with `raised_by` agent ID, `flag_ref` and request hash, `reasons` the three texts,
  `evidence` the levels and sources). Jev is asked at once (`EXIT_FLAG_JEV_REQUEST`/`RESULT`,
  `JEV_EARLY_EXIT_CONTEXT_V1`, `JEV_EARLY_EXIT_QUESTIONS_V1`, failed attempts retried after 60 s
  until `answer_due_at`). Jev EXIT: `EXIT_AGREED`; STAY: `EXIT_NOT_AGREED`; unusable:
  `EXIT_NOT_AGREED` (`JEV_ANSWER_UNUSABLE`); nothing by `answer_due_at`: `NO_ANSWER_IN_TIME`
  (`JEV_UNAVAILABLE`); reviews DISABLED: `NO_ANSWER_IN_TIME` at once (`JEV_REVIEWS_DISABLED`).
- *Jev flags* (a maintenance review's FLAG_EARLY_EXIT, side JEV): the addressee is asked at once
  (`EXIT_FLAG_ASKED`, `answer_due_at` the flag's) and answers with an `AGENT_REVIEW_ANSWER_V1`
  (no suggested levels) at `POST /api/v1/lab/exit-flags/{flag_id}/answer`
  (`EXIT_FLAG_AGENT_ANSWER`). EXIT: `EXIT_AGREED`; CONTINUE: `EXIT_NOT_AGREED`; nothing by
  `answer_due_at`: `NO_ANSWER_IN_TIME` (an answer recorded at the deadline is never overruled);
  no addressee: `NO_ANSWER_IN_TIME` at once (`NO_ACTIVE_AGENT`).
- *Both sides flagged* (a pending flag of each side in the lifecycle): both said exit,
  `EXIT_AGREED` (`BOTH_SIDES_FLAGGED`) for both flags.
- *A hard exit or a closed trade* ends every pending flag `LIFECYCLE_ENDED`.

Each resolution is `exit_flags.resolve_exit_flag` (`EXIT_FLAG_RESOLVED`; `EXIT_AGREED` requests
the exit `EARLY_EXIT_AGREED` in the same transaction while the trade is open and not exiting,
and the protection loop cancels the stop-limit and sells at market under one-use
authorizations) plus one `EARLY_EXIT_DECISION` (key the first flag): `flag_ids`, `sides`,
`outcome`, `code`, `decided_at`, `quote`, `levels`, `agent` (event, agent ID, answer ID,
decision, time), `jev` (request, status, code, receipts, answer), `exit_requested` and
`measurement` (null for `LIFECYCLE_ENDED`).

**`AGENT_EXIT_FLAG_V1`**: exactly `schema_version`, `flag_ref` (UUID; an exact retry returns the
flag, changed content is refused), `lifecycle_id` (the trade's), `what_changed`, `next_24h`,
`proves_wrong` and optional `sources`, with the answer's limits and refusals.

**`JEV_EARLY_EXIT_CONTEXT_V1`** (≤ 11,000 bytes): V4's sections without options, `review_trigger`
`{reasons: [AGENT_EXIT_FLAG]}`, `review` (`round`, `flagged_minutes_ago`, `hours_in_trade`,
`continuations`) and `agent_flag` (the flag's texts and up to 3 sources, as `agent_answer`).
**`JEV_EARLY_EXIT_QUESTIONS_V1`** (stage TRACKING, fixed, template hash pinned): `trade_reason`
(INTACT, WEAKENED, BROKEN), `agent_case` (HOLDS, DOES_NOT_HOLD), `exit_now` (EXIT, STAY).
Uncertain, STAY with BROKEN (`CONTRADICTORY_EARLY_EXIT_ANSWERS`) or another set is unusable.

**Measurement hook** (plan 4.6.8). Every CONTINUE or EXIT decision and every early-exit
resolution records `measurement`: `change_kind` (`CONTINUE_EXIT_DECISION` or `EARLY_EXIT`),
`at`, `old_stop`, `old_target` (in force at the decision), `new_stop`, `new_target` (null unless
raised), `actually_exited`, `quote` (bid, ask, mid, last and times), and
`continuation_decision`/`continuations_before`/`_after` or `early_exit_outcome`.
`trade_review.decision_records(repository, setup_id)` returns them in order with the outcome,
code, source event and both sides' answers.

**Routes** (research agents' route list; the caller's own requests only; status GET sees none):
`GET /api/v1/lab/reviews` (`items`, `poll_hint_seconds` 60, `request_lead_seconds` 1800),
`POST /api/v1/lab/reviews/{review_id}/answer`, `POST /api/v1/lab/exit-flags/{flag_id}/answer`,
`POST /api/v1/lab/positions/{setup_id}/exit-flag`; the research context's `pending_reviews` is
the same list and `open_trades` add `review_at` and `continuations`. Codes: `docs/API-CONTRACT.md`.
Status `day_reviews` and the watchdog's `DAY_REVIEW_JEV_FAILING` / `DAY_REVIEW_STATUS_UNAVAILABLE`.

**`MUSE_RESEARCH_GUIDELINES_V5`** (`muse_guidelines.py`): V4's text byte for byte followed by
"Reviews and exit flags (V5, 2026-09-27)" (SHA-256
`58f6434da9bb463ef01010533aa935bdbdf18ab215788b1de6def847341b9121`); the research context's
`report_format` serves V5 from 2026-09-27. V1-V4 stay importable and unchanged.

## Files

New: `src/catalyst_lab/day_review.py` (versions, the agent's answers and flags, Jev answer
reading, the decision table, the measurement record), `src/catalyst_lab/day_review_dossier.py`
(the two contexts and question sets), `src/catalyst_lab/trade_review.py` (`DayReviews`,
`TradeReviewService`, ledger views, `decision_records`), `tests/day_review_fixtures.py`,
`tests/test_day_review.py`, `tests/test_early_exit.py`, `tests/test_day_review_rules.py`,
`tests/test_day_review_session.py`, this file.

Changed: `src/catalyst_lab/crypto_holding.py` (`CRYPTO_24H_REVIEW_V1`, `admission_policy`,
`recorded_hold_policy` dispatch, `hard_exit_deadline`, `review_fields`, `time_exit_reason`;
`CRYPTO_24H_HOLD_V1` unchanged), `src/catalyst_lab/managed_execution.py` (`admit` records the
arm's holding policy; `manage` computes the review's fail-safe and records T and the count;
`DAY_REVIEW_DEADLINE_EXIT` among the planner's exit reasons; `MAINTENANCE_OPENED`'s
`first_24h_review_at` is T), `src/catalyst_lab/trade_maintenance.py` (`_changes` a staticmethod
that also lists a review's raised levels; the context's `review_at` is T),
`src/catalyst_lab/managed_service.py` (the four routes, `STATE_FIELDS` `day_review_at` and
`continuations`, `STATUS_FIELDS` `day_reviews`), `src/catalyst_lab/managed_app.py` (the review
service, `configured_agents`, the runtime's agents, status), `src/catalyst_lab/managed_runtime.py`
(`day_reviews`, its periodic task, status, factory), `src/catalyst_lab/managed_ops.py`
(`day_review_alarms`), `src/catalyst_lab/research_context.py` (`pending_reviews`, open trades'
`review_at`/`continuations`, serves V5), `src/catalyst_lab/muse_guidelines.py` (V5),
`scripts/agent_research_session.py`, `docs/API-CONTRACT.md`, `docs/MANAGED-RUNTIME.md`,
`docs/MUSE-GUIDELINES.md`, `docs/OPERATIONS-RUNBOOK.md`. Tests changed:
`tests/test_crypto_size_hold.py` (three tests: the 24-hour exit and first-fill clock tests pin
the control arm, the top-K admission test expects the arm's holding policy),
`tests/test_selection_topk_v2.py` (the served-version assertion moved to the V5 test).

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-day-review`).

- New tests (61): `tests/test_day_review.py` 30 (the request at T − 30 min to the proposing agent
  only, with the trade, news and options; other tokens can neither see nor answer; agreement to
  continue with raised levels, the PATCH replacement under a claimed authorization, the moved T
  and fail-safe, review 2; agreement to exit and the one market sell; the discussion round with
  Jev's reasons shown, agreement after it, still disagreeing, a silent agent in the round; a
  silent agent at T (Jev alone, no `agent_case`, a late answer refused); Jev down 30 minutes
  (retries a minute apart, the alarm, exit at exactly T + 30 min); three unusable answers; the
  fail-safe exit at T + 80 min and not before; a level change refused by the checks while the
  trade continues; two continues counting and moving the clock; four hard exits discarding the
  review; a trade closed while Jev answers; two restarts (receipt recovered without a second
  vote and one sale after another restart; an interrupted request lapsed and retried); the active
  research agent answering for a gone proposer; no agent (Jev alone); DISABLED; the control arm
  and a report-V2 setup never reviewed (the control arm exits `HOLD_24H_EXIT`); the research
  context's `pending_reviews` and open trades; the budget at production size; the measurement
  records of a continue, an early exit and an exit; a discussion reply at the deadline never
  overruled; missing bars as a retried failure);
  `tests/test_early_exit.py` 11 (an agent flag Jev agrees with, sold, measured, idempotent; Jev
  disagreeing; Jev unavailable for 15 minutes; DISABLED; a Jev flag the agent agrees with, rejects
  or ignores (with a late answer refused); both sides flagging; a hard exit ending a pending
  flag; who may flag (403 for another agent, lifecycle mismatch, the agent's name in the text,
  404, the control arm); a maintained trade admitted before the version keeping today's pending
  Jev flag and `HOLD_24H_EXIT`);
  `tests/test_day_review_rules.py` 16 (the versions' exact records, math, dispatch and scope; the
  agent's answers and flags with every refusal; Jev answer reading; pinned question-set template
  hashes; the decision table; the measurement record; the budget ladder, determinism and the
  unsatisfiable budget; status fields and alarms; the runtime pass, status and setting check; the
  factory; the app wiring and routes; guidelines V5 byte-identity with V4 and its document);
  `tests/test_day_review_session.py` 4 (a top-K session holding trades open, the agent answering
  over HTTP, continue with a replaced stop and exit with a clean residual; the discussion round
  and an agent early exit; fixture labels; CLI parsing).
- Existing suites re-run during the work (687 passed): `test_managed_execution`,
  `test_crypto_execution`, `test_crypto_size_hold`, `test_crypto_trigger`,
  `test_managed_runtime`, `test_managed_service`, `test_managed_app`, `test_managed_ops`,
  `test_unattended_safety`, `test_selection_topk`, `test_selection_topk_v2`, `test_replacement`,
  `test_system_check`, `test_operator_flatten`, `test_managed_hardening`,
  `test_complete_managed_cycle`, `test_position_news`, `test_exit_attribution`,
  `test_managed_day_policy`, `test_randomized_arm`, `test_position_monitor`,
  `test_managed_dossier`; `test_trade_maintenance`, `test_maintenance_*`,
  `test_agent_research_session`, `test_research_context` also passing.
- **Full suite at `205d674`: 3,359 passed, 0 failed** (11 min 49 s; baseline 3,298 plus the 61
  new tests), about 9.4 GB free before the run. One code change followed it (`526a362`: the two
  new contexts show news without its agent-written source IDs); after it the 61 package tests
  and the 88 maintenance tests were re-run (149 passed). Later commits are docs only.
- `ruff check src tests`: all checks passed.

## Deviations and choices (please confirm)

1. **The 24-hour hold is replaced per arm, at admission.** The maintained arm's setups record
   `CRYPTO_24H_REVIEW_V1` instead of `CRYPTO_24H_HOLD_V1`; nothing about earlier setups changes,
   so a maintained trade admitted before this version still exits at 24 hours and its Jev flags
   stay pending (today's behaviour).
2. **T is anchored to the first fill** (T, T + 24 h, T + 48 h ...), not to the moment a continue
   was decided (at most 75 minutes later). "Every 24 hours after a continue" read as a fixed daily
   cadence: deterministic from the ledger and restart-safe.
3. **The fail-safe.** A review decides within 75 minutes of T by construction; the
   `hard_exit_at` fail-safe (T + 80 min, `DAY_REVIEW_DEADLINE_EXIT`) only acts when the review
   component is not running, so no trade is held past its plan without a recorded continue. A
   continue decided within 60 s of the fail-safe becomes an exit.
4. **"Jev unable to answer within 30 minutes"** is per Jev answer: 30 minutes from T for the
   first, 30 minutes from the agent's reply for the final. Each attempt has a 60-second deadline
   and a failed one is retried a minute later. A context that cannot be built (no quote, no bars,
   broker metadata) counts as Jev unable to answer.
5. **An answer code cannot use is not retried**: an uncertain or contradictory Jev answer at a
   review is an exit (a continue needs Jev's clear continue and options), at an early exit it is
   "not agreed" (the trade stays). Code's consistency rules: EXIT needs both options KEEP;
   CONTINUE (or STAY) with the reason BROKEN contradicts.
6. **No reply in the discussion round exits at once** without a final Jev call: with the
   disagreement standing, the outcome is exit whatever Jev answers finally (the agent's last word
   is still its first answer), so the call is not spent.
7. **"Does the agent's case hold"** is asked only when the agent answered (otherwise there is no
   case to judge); agreement is decided on the decisions alone, the case answer is recorded.
8. **The agent's suggested stop and target are prices**, shown to Jev beside the code's options;
   Jev chooses only options, and the chosen levels pass phase 5's checks; a refused level change
   keeps the trade's levels and the continue stands.
9. **Addressee.** "The active research agent" is the configured research-agent credential whose
   report arrived last; "revoked or gone" means its credential is no longer configured (the app
   sets the configured agents at start). The addressee is fixed when the request (or the ask of
   a Jev flag) is recorded. An agent may flag only a trade it currently answers for.
10. **Early exits need a pass of the runtime**: the other side is asked on the next pass (about a
    second); an agent's answer or flag is recorded by the route and acted on by the runtime, so
    every decision has one writer and a fresh quote.
11. **Both sides flagging** (each with a pending flag in the lifecycle) counts as both saying
    exit.
12. **Budget and size.** The agent's reasons are never shortened in Jev's input; at most 3 of its
    sources are shown, cut to 300 characters, and they go first when over budget. A whole answer
    or flag is at most 12,000 bytes of JSON (the evidence limit).
13. **The fail-safe never renames an exit already requested** (the time exit of
    `CRYPTO_24H_HOLD_V1` still does, as before, for its own setups).
14. **No migration**: event bodies and state fields only; broker requests reuse the existing
    decisions and gate.

## Open items and questions

- **A real-Jev session** (owner or coordinator): `start --jev typesafe --selection-rule TOPK2`,
  `execute --simulate-prints --hold-open`, `review --at request`, answer with `review-answer`,
  `review --at review` (one or two Jev calls per trade), plus an `exit-flag` and a `review`.
- **A supervised paper session** on Alpaca Paper (the review's PATCH of a raised stop and its
  market exit reuse phase 5's and the hold's paths; the PATCH of a crypto stop-limit is still
  unverified at Alpaca, maintenance's open item).
- **A coin that Alpaca stops listing** has no exit of its own (plan 4.6.5): today `manage`
  refuses the asset metadata of an untradable coin and latches, so no exit order could be sent
  anyway. The review discards itself on any exit request, so a later delisting exit will discard
  reviews without change here. Not built.
- **Repeated Jev flags**: after an early exit is not agreed, a maintenance review that still
  finds the reason BROKEN flags again at the next review (every 15 minutes at most), and the agent
  is asked again. A cooldown would be a new flag version.
- **Results package**: `trade_review.decision_records` and the `measurement` bodies map directly
  to `LevelChange`; `pick_outcome_aggregates`' `continuation_decision` can read
  `measurement.continuation_decision` of each setup's latest `DAY_REVIEW_DECISION`.
- **Watchdog**: `EXIT_FLAG_PENDING` (package maintenance) now fires for up to 15 minutes on every
  flag of a reviewed trade while it is being answered; unchanged here.
- **Found in passing (not changed here):** position news (`POST /positions/{id}/news`) never
  checks an agent-written `source_id` for the agent's name, and `JEV_MANAGED_POSITION_CONTEXT_V4`
  (maintenance) shows news with its source IDs, so an agent that named itself in one would reach
  Jev there. The day-review contexts omit them; a fix for V4 is a new context version or an
  intake check in position news.
