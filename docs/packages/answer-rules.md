# Package answer-rules — Jev's answers read by the question that decides, and a trade maintained every minute

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE + REAL-JEV LOOP EVIDENCE.**

Branch `pkg/2026-09-27-answer-rules`, from `work/2026-09-24-product-plan` at `8dc02d6` (schema
23; phases 0-8 merged). Trigger: the first real-Jev run of trade maintenance (plan phase 5) and
the 24-hour review (plan phase 6), round 5 of 2026-09-27, committed with this package in
`artifacts/real-jev-loop-2026-09-27/` (its `README.md` describes the run). Scope extended the same
day by the owner, relayed by the coordinator: "jev should moniter trade every minute it is day
trade can you change the setting. it should have context of trade as well so it can decide what
to do better", folded into the same V2 versions. This file carries the text the coordinator
merges into `docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this
branch. **No migration** (schema stays 23): state fields and append-only events only.

## PHASES entry (paste as written)

### 2026-09-27 — Jev's answers read by the question that decides, and maintained trades reviewed every minute: `CRYPTO_MAINTENANCE_V2`, `CRYPTO_24H_REVIEW_V2` (package answer-rules) (FIXTURE EVIDENCE + REAL-JEV LOOP EVIDENCE)

**The run** (`artifacts/real-jev-loop-2026-09-27/`). The session harness with the real pinned
`jev-1.13.0` (31 calls, all valid first attempts) and its simulated venue, bars and fills; no
broker, no owner ledger, no real order. Selection (top-K V2, K 10): nine picks ranked, SOL vetoed
`NEWS_STALE_YES`. Nine admitted; PEPE in the control arm (target exit). Six maintained trades
(WIF, CRV, DOT, LINK, then RENDER and LTC) each had one maintenance review at +1R, and all six
were **refused `CONTRADICTORY_MANAGEMENT_ANSWERS`**: Jev answered HOLD (0.38-0.49) with
`stop_option` S1 (breakeven, 0.46-0.55) against KEEP, and V1's reader demands that the four
independently answered questions agree (HOLD needs both options KEEP), although `stop_option` is
explicitly conditional ("to use if the stop is raised"). The 24-hour reviews: CRV agreed exit
(the agent EXIT, Jev EXIT 0.97); **WIF was sold although both sides said continue** (the agent
CONTINUE, Jev's decision CONTINUE 0.60, but its target answer tied, KEEP 0.44 = T1 0.44, and the
tie made the whole answer uncertain: `JEV_ANSWER_UNUSABLE`, exit); DOT and LINK exited on Jev
alone (EXIT 0.58 each; the harness's review clock, which kept moving with the wall clock,
closed their answer windows about a second after their requests; fixed in the harness here). The agent's early-exit flag on
RENDER was answered STAY 0.51 against EXIT 0.44: not agreed. The review contexts were built on
the harness's synthetic flat bars (no news, Bitcoin 0% on every window, +1R at minute 0), so
Jev's hesitation says little about real markets; what the run shows is how code reads Jev.

**What changed** (named versions under the owner's 2026-09-24 ruling; every V1 version, reader,
context, question text and stored record unchanged):
- **`CRYPTO_MAINTENANCE_V2`** with **`MAINTENANCE_ANSWER_RULE_V2`**: `action` decides. HOLD
  holds and FLAG_EARLY_EXIT raises the flag whatever the other answers say; a raise uses an
  option answer only when it is the unique most probable offered option (KEEP, a tie,
  Insufficient evidence or an unknown ID keeps that level, recorded in `option_use`; a raise with
  no usable part is held, `NO_USABLE_OPTION`). `trade_reason` is informational. An Insufficient
  or tied `action` changes nothing (`UNCERTAIN_JUDGMENT`, as V1).
- **Every minute** (the owner's extension): V2 reviews a maintained trade at every completed
  1-minute bar (`BAR_1M`) plus V1's events, never twice inside a minute except near the target:
  **one Jev call per maintained trade per minute, up to about 1,440 per trade per day**, plus a
  few event reviews. At most one review in flight per trade: a minute that completes while the
  trade's review is still being answered is skipped and recorded
  (`MAINTENANCE_REVIEW_SKIPPED`, `REVIEW_SKIPPED_IN_FLIGHT`), never reviewed late. A pass runs its
  Jev calls concurrently, at most the research cycle's `max_inflight` at once (5 in the deploy
  example), nearest to the stop first; a waiting trade's request and deadline start only when
  its turn comes.
- **More context** (the owner's extension): `JEV_MANAGED_POSITION_CONTEXT_V5` (V4 plus the last
  60 completed 1-minute bars and `review_history`: the trade's last 5 maintenance reviews with
  their triggers, Jev's answers and top probabilities, the price of a chosen option and the
  outcome and code) and `JEV_MANAGED_POSITION_QUESTIONS_V5` (V4's texts; the trade-reason
  question names the two sections, and the option questions say that code uses an option only
  when the maintenance action raises that level, where V4 said code refuses an inconsistent
  combination; labels unchanged; template hash pinned). The 1-minute bars
  are read GET-only over REST through the existing read-only source, once per completed minute
  and coin (the managed stream carries trades and quotes, not bars).
- **`CRYPTO_24H_REVIEW_V2`** with **`DAY_REVIEW_ANSWER_RULE_V2`**: `decision` decides. EXIT exits
  and ignores the option answers; CONTINUE raises a level only to a unique most probable offered
  option and otherwise keeps it, never exiting over an option answer; an Insufficient or tied
  decision, or CONTINUE with the reason uniquely BROKEN, stays unusable (exit, as V1). Agreement,
  the discussion round and the early-exit agreement are V1's. Its context
  `JEV_DAY_REVIEW_CONTEXT_V2` adds the same `review_history`, and `JEV_DAY_REVIEW_QUESTIONS_V2`
  names it as history (V1's evidence text classifies every history section) and says code uses
  an option only on CONTINUE.
- **Admission** records both V2 versions in the maintained arm of a report-V3 crypto setup; the
  control arm keeps `CRYPTO_24H_HOLD_V1` and the partial-entry rule. A setup that recorded V1
  keeps V1's cadence, contexts, questions and readers forever: each Jev request carries its
  setup's policy record, and code reads the answer by that record.
- **Harness**: `execute --simulate-prints --maintenance-minutes N` (1-10) runs N-1 further minute
  reviews per maintained trade on its own maintenance clock, so a real-Jev session can watch the
  cadence and the history. The review clock now holds between `review` steps: it moves only with
  `review --at` or `--minutes N` (forward only) and no longer follows the wall clock while the
  agent answers, so round 5's lost DOT and LINK answer windows cannot recur (harness only; the
  runtime has no simulated clock).

**Replay of the run** (`tests/test_answer_rules_replay.py`, from the stored response bytes, the
export's hash and audit chain verified): V1's readers reproduce every recorded outcome; V2 holds
all six maintenance answers and continues WIF with the stop raised to S1 (breakeven 0.23424, the
apply checks passing at the recorded quote) and the target kept.

**Evidence.** 227 new tests: `tests/test_answer_rules.py` (195: exact records and dispatch, every
action or decision against every option state beside V1's reading of the same answers, the
cadence and in-flight rules, contexts V5 and V2, the pinned templates),
`test_answer_rules_replay.py` (7), `test_answer_rules_flows.py` (14: maintenance V2 end to end on
the fixture venue, a V1-recorded setup still refusing HOLD with S1, the minute cadence, the skip,
the bound), `test_answer_rules_day_review.py` (10: the WIF answer continuing with the stop
replaced by PATCH, the V1 setup exiting on it, the discussion round and a restart under V2, the
harness's consecutive minute reviews, its review clock holding while the agent answers), and one
source test (1-minute bars GET-only). Eight
existing tests stay V1's evidence, pinned to V1 admission; five assertions moved to the V2
records and two tests were adapted (see the package file's Validation, with the full suite and
ruff).

**Not shown.** A real-Jev session of the minute cadence and context V5 (the harness can now run
one); real market bars in any review context; the cost of about 1,440 calls per trade per day; a
supervised paper session. The owner ledger and runtime were not touched.

## CONTRACT-RESOLUTIONS text (paste as written)

### Answer rules, minute maintenance and review history: `CRYPTO_MAINTENANCE_V2`, `MAINTENANCE_ANSWER_RULE_V2`, `JEV_MANAGED_POSITION_CONTEXT_V5`, `JEV_MANAGED_POSITION_QUESTIONS_V5`, `CRYPTO_24H_REVIEW_V2`, `DAY_REVIEW_ANSWER_RULE_V2`, `JEV_DAY_REVIEW_CONTEXT_V2`, `JEV_DAY_REVIEW_QUESTIONS_V2` (2026-09-27, package answer-rules)

Evidence of the flaw: the first real-Jev run of plan phases 5 and 6
(`artifacts/real-jev-loop-2026-09-27/`): six maintenance reviews refused as contradictory for
HOLD with a conditional stop option, and a 24-hour review both sides wanted to continue sold
because an unused option answer was tied. The pinned TypeSafe guidance ("Compose and verify")
says independent questions over the same state cannot see one another's answers, speculative
premises are stated explicitly, code consumes the applicable answers, and uncertainty on unused
branches is ignored. Owner direction the same day (relayed by the coordinator): Jev monitors a
day trade every minute with the trade's context. Named versions under the owner's 2026-09-24
ruling. `CRYPTO_MAINTENANCE_V1`, `CRYPTO_24H_REVIEW_V1`, `JEV_MANAGED_POSITION_CONTEXT_V4` /
`QUESTIONS_V4`, `JEV_DAY_REVIEW_CONTEXT_V1` / `QUESTIONS_V1`, V1's readers, the early-exit
agreement, context and questions, `EARLY_EXIT_FLAG_V1`, `CRYPTO_24H_HOLD_V1`,
`CRYPTO_PARTIAL_ENTRY_V1`, the authorization gate, every SQL function and every stored record
keep their definitions; no migration.

**Scope and dispatch.** A setup admitted from a packet whose `market` is `CRYPTO` and whose
`report_schema_version` is `AGENT_RESEARCH_REPORT_V3`, in the randomized `JEV_MANAGED` arm,
records `maintenance_policy` `CRYPTO_MAINTENANCE_V2` and `holding_policy` `CRYPTO_24H_REVIEW_V2`
(exact records below) in the admitting transaction; the `FIXED_EXIT` arm records
`CRYPTO_24H_HOLD_V1` and `CRYPTO_PARTIAL_ENTRY_V1` as before. A state recording an altered V2
record is refused. Everything version-specific follows the recorded record: the cadence, the
contexts and questions, and the reader. Each Jev request's context carries its setup's exact
policy record as `policy` and the answer is read by that record's rule
(`maintenance_dossier.answer_reader`, `day_review.review_reader`), so a request recovered after a
restart is read by the rule it was asked under. A setup that recorded V1 keeps V1 in every
respect. Event bodies (`MAINTENANCE_TRIGGER`, `POSITION_REVIEW_REQUEST`'s trigger and identity,
`MAINTENANCE_DECISION`, `MANAGED_JEV_JUDGMENT`, `MAINTENANCE_OPENED`,
`MAINTENANCE_ENTRY_COMPLETED`, `DAY_REVIEW_REQUESTED`, the review's Jev request identity,
`DAY_REVIEW_DECISION`, unavailability notices) carry the setup's own `policy_id`;
`MAINTENANCE_BTC_SHOCK` keeps `CRYPTO_MAINTENANCE_V1` (V2 uses V1's shock rule unchanged).

**`CRYPTO_MAINTENANCE_V2`** (`crypto_maintenance.py`; record: `CRYPTO_MAINTENANCE_V1`'s with
`policy_id` `CRYPTO_MAINTENANCE_V2`, `context_version` `JEV_MANAGED_POSITION_CONTEXT_V5`,
`question_version` `JEV_MANAGED_POSITION_QUESTIONS_V5`, `review_bar_seconds` 60 and `answer_rule`
`MAINTENANCE_ANSWER_RULE_V2`; every other number V1's: near target and near stop 0.005, the
once-a-minute floor 60 s, answer age 60 s, review deadline 10 s, stop bid margin 0.005, swing-low
margin 0.01, five stop and five target options, swing span 2, BTC/USD 3% in 900 s,
`EARLY_EXIT_FLAG_V1`).

*Cadence.* A review is due at every completed 1-minute bar (reason `BAR_1M`: the latest
UTC-aligned minute boundary after the open and after the minute last served) and at once on
V1's events (the first +nR milestones, the bid within 0.5% of the target or the stop, agent news,
a 3% Bitcoin move within 15 minutes); it waits while the last request is under 60 s old unless a
near-target trigger is among its reasons. At most one review is in flight per trade: when the
latest completed minute ended after the trade's previous request and at or before that
request's decision (or while it is undecided), that minute is skipped: one
`MAINTENANCE_REVIEW_SKIPPED` (key `maintenance-review-skipped:<setup>:<lifecycle>:<bar_end>`;
body `policy_id`, `lifecycle_id`, `code` `REVIEW_SKIPPED_IN_FLIGHT`, `bar_end`,
`in_flight_request_id`, `in_flight_requested_at`, `in_flight_decided_at`, `recorded_at`), and
the minute counts as served (never reviewed late); other reasons still make a review due.
Volume: one Jev call per maintained trade per completed minute, up to about 1,440 per trade per
day, plus event reviews.

*Concurrency.* A maintenance pass orders its due trades nearest to their stop first (a trade
without a stream quote last) and runs their Jev calls concurrently, at most `max_inflight` at
once: the runtime passes `CyclePolicy.max_inflight` (`MANAGED_CYCLE_POLICY_JSON`, the research
cycle's in-flight limit, 1-30); a component built without one uses 10. A trade waits for a free
slot in that order, and its request (context, deadline, `POSITION_REVIEW_REQUEST`) is built and
recorded only when its turn comes.

*Context and questions.* `JEV_MANAGED_POSITION_CONTEXT_V5` (≤ 11,000 bytes, manifest
`MAINTENANCE_DOSSIER_V2`): every V4 section byte for byte with `context_version` replaced, plus
`price_action.bars_1m` (the last 60 completed 1-minute bars of the coin in V4's row format
`end_age_minutes|open|high|low|close|volume`, read GET-only through the read-only position
source, once per completed minute and coin; missing bars stop the review as missing 15-minute
bars do) and `review_history` (the lifecycle's last 5 `MAINTENANCE_DECISION`s, oldest first:
`minutes_ago` of the request, `trigger_reasons`, `action`, `trade_reason`, `stop_option` and
`target_option` each `{choice, top_p}` or null when Jev gave none, a chosen offered option with
its `price` at that review, `outcome` and `code`; `review_history_omitted` counts older ones).
Over budget: the oldest 1-minute rows first (15 stay), then V4's ladder, then the oldest reviews
of the history (2 stay); still over, the review is skipped as V4's. Options are never truncated.
`JEV_MANAGED_POSITION_QUESTIONS_V5` (stage TRACKING): V4's four questions and every label,
option text and instruction, with two exceptions. `trade_reason` names "completed 1-minute bars
in `price_action.bars_1m`" and "`review_history` is your own last maintenance reviews of this
trade with what code did with each: all are history, not new facts". The last sentence of
`stop_option` and `target_option`, V4's "All questions are independent; code refuses an
inconsistent combination and re-checks every level against the live price before applying it.",
is "All questions are independent. Code uses an option only when your maintenance action raises
that level, and re-checks every level against the live price before applying it." (the action is
named in words: question IDs are not sent to the model). Template hash with no options offered:
`412aa2363d907b50c646a9b593f27b4ffc4f99811904f4a68302b6bc7cce0233` (pinned in
`maintenance_dossier.QUESTIONS_V5_TEMPLATE_SHA256`, checked at import, and in the tests). No V5
question set has been sent to the real Jev.

*`MAINTENANCE_ANSWER_RULE_V2`* (`maintenance_dossier.read_answer_v2`). An answer is usable
("unique") when its reported choice has the highest probability, no other choice has the same
probability (exact comparison, as V1) and it is not Insufficient evidence. Another question set:
`INVALID_MANAGEMENT_ANSWER`. `action` not unique (tied or Insufficient):
`UNCERTAIN_JUDGMENT`, no change. HOLD: `HELD`; FLAG_EARLY_EXIT: `FLAGGED`
(`EARLY_EXIT_FLAG_V1`, the reason answer recorded, not required to be BROKEN); neither uses an
option answer. RAISE_STOP / RAISE_TARGET / RAISE_STOP_AND_TARGET: each level the action raises
rises to its option answer only when that answer is unique and an ID the context offered;
otherwise the level stays with `code` `STOP_OPTION_NOT_USABLE` / `TARGET_OPTION_NOT_USABLE` and
`reason` `KEEP`, `TIED`, `INSUFFICIENT_EVIDENCE` or `UNKNOWN_OPTION`. The usable parts pass V1's
apply checks unchanged (`APPLIED` or `REFUSED` with their code); a raise with no usable part is
`HELD`, `code` `NO_USABLE_OPTION`. `trade_reason` is informational. A decision body adds
`answer_rule` and `option_use` (per level: `choice` as given, `raise_to`, `code`, `reason`, or
`NOT_RAISED_BY_ACTION`); V1's bodies keep exactly their fields. `review_status` stays the
transport's whole-answer flag (any tie or Insufficient answer).

**`CRYPTO_24H_REVIEW_V2`** (`crypto_holding.py`; record: `CRYPTO_24H_REVIEW_V1`'s with
`policy_id` `CRYPTO_24H_REVIEW_V2`, `context_version` `JEV_DAY_REVIEW_CONTEXT_V2`,
`question_version` `JEV_DAY_REVIEW_QUESTIONS_V2` and `answer_rule` `DAY_REVIEW_ANSWER_RULE_V2`;
every number V1's: review interval 86,400 s, request lead 1,800 s, agent reply 900 s, Jev answer
1,800 s, review window 4,500 s, grace 300 s, Jev call deadline 60 s, retry 60 s, exit reasons
`DAY_REVIEW_EXIT` and `DAY_REVIEW_DEADLINE_EXIT`, `AGENT_REVIEW_ANSWER_V1`,
`EARLY_EXIT_AGREEMENT_V1`). The clock, the fail-safe, the request, the agent's answer, agreement,
the discussion round, the timeouts, applying and the hard exits are V1's; the review routes and
the research context serve trades under either version.

*`JEV_DAY_REVIEW_CONTEXT_V2`*: V1's context byte for byte with `context_version` replaced, plus
`review_history` and `review_history_omitted` as context V5's. Over budget, after V1's ladder
(the agent's sources, then V4's ladder) the oldest reviews of the history go (2 stay).
*`JEV_DAY_REVIEW_QUESTIONS_V2`* (stage TRACKING): V1's questions, labels and texts, with two
exceptions. The trade-reason evidence says "`selection_answers` are your own answers when the
pick was selected, `level_changes` are earlier stop and target changes and `review_history` is
your own last maintenance reviews of this trade with what code did with each: all are history,
not new facts". The option questions' sentence "All questions are independent; code refuses an
inconsistent combination and re-checks every level against the live price before applying it."
is "All questions are independent. Code uses an option only when you choose CONTINUE, and
re-checks every level against the live price before applying it." Template hashes with no
options offered: agent answered
`e898940cd0bd5d1a6d94b623523075cad3fca6a1e621b5d9911e462e2b08d90c`, not answered
`89cf92cb531a15890f17bc15f8a56b8c72affcb5b72406e05d3c27a8647f0fb4` (pinned in
`day_review_dossier.QUESTIONS_V2_TEMPLATE_SHA256`, checked at import, and in the tests). No V2
question set has been sent to the real Jev.

*`DAY_REVIEW_ANSWER_RULE_V2`* (`day_review.read_review_answer_v2`; "unique" as above). Another
question set: `INVALID_REVIEW_ANSWER`. `decision` not unique: `UNCERTAIN_JUDGMENT`, unusable (the
review exits, `JEV_ANSWER_UNUSABLE`). EXIT: usable; the option answers are recorded and never
used. CONTINUE with `trade_reason` BROKEN as its unique answer: `CONTRADICTORY_REVIEW_ANSWERS`,
unusable (V1's rule kept). CONTINUE otherwise: usable; each level rises to its option answer only
when that is unique and offered (then V1's apply checks); KEEP keeps it; a tie, Insufficient
evidence or an unknown ID keeps it with `code` `STOP_OPTION_NOT_USABLE` / `TARGET_OPTION_NOT_USABLE`
and never makes the answer unusable. `agent_case` and otherwise `trade_reason` are informational.
The result record (`JevReviewAnswerV2`: V1's fields plus `answer_rule`, `raise_stop_to`,
`raise_target_to`, `option_use`) is read back by its `answer_rule`; the result's `chosen` holds
only the options code would raise to. The early-exit reader (`JEV_EARLY_EXIT_QUESTIONS_V1`) is
unchanged.

**Status and results.** `trade_maintenance.policy_id` and `day_reviews.policy_id` name the
version admission records; both add `open_trades_by_policy`. `unchanged_plan` classifies an
applied maintenance decision by the levels it moved (identical for every V1 decision; a V2
RAISE_STOP_AND_TARGET with only the target usable is a target raise).

## Files

New: `tests/test_answer_rules.py`, `tests/test_answer_rules_replay.py`,
`tests/test_answer_rules_flows.py`, `tests/test_answer_rules_day_review.py`,
`artifacts/real-jev-loop-2026-09-27/` (the run's export, unchanged, and its `README.md`), this
file.

Changed: `src/catalyst_lab/crypto_maintenance.py` (`CRYPTO_MAINTENANCE_V2`, `MaintenancePolicyV2`,
`ADMITTED_MAINTENANCE`, `policy_from_record`/`recorded_policy` dispatch, `recorded_policy_id`,
`BAR_1M`, `due_reasons(bar_seconds=)`, `in_flight_at`, the skip event and code, `1Min`),
`src/catalyst_lab/maintenance_dossier.py` (context V5 compiler, questions V5 and their pin,
`read_answer_v2`, `answer_reader`, `questions_for`, `compile_state_for`, `review_history_row`),
`src/catalyst_lab/trade_maintenance.py` (version dispatch, the minute cadence and skip, the
bounded pipeline, 1-minute bars, the review history, per-setup `policy_id`s, status),
`src/catalyst_lab/crypto_holding.py` (`CRYPTO_24H_REVIEW_V2`, `CryptoReviewPolicyV2`,
`ADMITTED_REVIEW`, `review_policy_from_record`), `src/catalyst_lab/day_review.py`
(`read_review_answer_v2`, `JevReviewAnswerV2`, `review_reader`, `option_to_raise`,
`answer_from_record` dispatch), `src/catalyst_lab/day_review_dossier.py` (context V2, questions V2
and their pins, `review_questions_for`), `src/catalyst_lab/trade_review.py` (both versions in the
scans, per-setup policy in requests and decisions, V2 contexts, dispatch, status),
`src/catalyst_lab/managed_execution.py` (`MAINTENANCE_OPENED`'s `policy_id`),
`src/catalyst_lab/managed_runtime.py` (the maintenance bound, the configuration hash names the
admitted versions), `src/catalyst_lab/scan_sources.py` (`1Min` bars), `src/catalyst_lab/unchanged_plan.py`
(classification by moved levels), `scripts/agent_research_session.py` (the maintenance clock and
reviewer, `--maintenance-minutes`, 1-minute simulated bars, the bound, admitted versions in the
configuration), `docs/MANAGED-RUNTIME.md`, `docs/API-CONTRACT.md`, `docs/OPERATIONS-RUNBOOK.md`.
Tests changed: `tests/maintenance_fixtures.py` (`v1_admission`, `shaped_answer`, exact answers,
the bound), `tests/day_review_fixtures.py` (exact answers), `tests/test_trade_maintenance.py`,
`tests/test_day_review.py`, `tests/test_maintenance_replay.py`, `tests/test_maintenance_rules.py`,
`tests/test_day_review_rules.py`, `tests/test_maintenance_runtime.py`,
`tests/test_maintenance_session.py`, `tests/test_day_review_session.py` (see Validation).

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-answer-rules`).

- New tests (227): `tests/test_answer_rules.py` 195 (the exact V1 and V2 records, their refusal of
  altered values and their dispatch; 125 maintenance cases, every action against every stop and
  target option state, with V1's reading of the same answers; the uncertain action; the reason
  informational; 50 review cases, every decision against every option state, with V1's reading;
  unclear decisions and the BROKEN continue; informational reason and agent case; record
  round-trips; the cadence and in-flight rules; context V5 byte-identical to V4 in V4's sections,
  its ladder and the unsatisfiable budget; questions V5 and V2 against V4 and V1, differing only
  in the named sentences, and their pins; context V2 against V1);
  `tests/test_answer_rules_replay.py` 7 (the stored export: manifest hash,
  audit chain, the selection counts, the six maintenance answers, WIF, CRV/DOT/LINK, the RENDER
  flag); `tests/test_answer_rules_flows.py` 14; `tests/test_answer_rules_day_review.py` 10
  (the last reproduces round 5's lost answer window: two trades 5 s apart, the second asked at
  `--at review`, a ten-minute answer; it fails with the old wall-following clock, where the
  agent's pending list is empty, and passes through agreement with the held clock);
  `tests/test_maintenance_runtime.py` one (1-minute bars GET-only and completed).
- Eight existing tests kept as V1's evidence by pinning them to V1 admission (`v1_admission`,
  as every setup admitted before this package): in `test_trade_maintenance.py` the 15-minute
  cadence, the milestones, near target and stop, the Bitcoin shock, the PATCH stop raise and the
  V1 consistency refusals (three cases); in `test_day_review.py` the agreed continue and the
  EXIT-with-option case of the unusable-answer test. Five assertions moved to the V2 records
  (the admitted records and policy IDs in `test_maintenance_rules.py`,
  `test_day_review_rules.py`, `test_trade_maintenance.py`, `test_maintenance_session.py` and
  `test_day_review_session.py`); two tests adapted: `test_maintenance_replay.py`'s refused
  decision uses an uncertain action (refused under both), and `test_maintenance_runtime.py`'s
  unsupported timeframe example is `5Min` (`1Min` is now supported).
- Suites re-run during the work: 465 passed (`test_trade_maintenance`, `test_maintenance_*`,
  `test_day_review*`, `test_early_exit`, `test_agent_research_session`, `test_unchanged_plan`,
  `test_crypto_size_hold`, `test_managed_runtime`, `test_managed_app`,
  `test_managed_service_results`, `test_managed_execution`, `test_crypto_execution`,
  `test_position_monitor`, `test_position_news`, `test_research_context`,
  `test_managed_engineering`).
- **Full suite at `806f6eb` (after the coordinator's follow-up: the V5 and review-V2 option
  texts and the held harness review clock; the next commit is docs only): 3,701 passed, 0
  failed** (13 min 16 s; baseline 3,474 plus the 227 new tests), through the coordinator's
  guarded, locked suite script, about 6.5 GB free before the run. Before the follow-up, at
  `0de87a3`: 3,700 passed, 0 failed.
- `ruff check src tests research_agent`: all checks passed.

## Deviations and choices (please confirm)

1. **The review's V2 changes its context and questions too.** The first brief kept V1's context
   and questions for `CRYPTO_24H_REVIEW_V2`; the owner's extension added the review history to
   it. V1's evidence text lists and classifies the history sections ("both are history, not new
   facts"), so an unnamed new section of Jev's own earlier answers could read as new evidence
   (`JEV_DAY_REVIEW_QUESTIONS_V2`).
2. **The option questions of V5 and the review's V2 say what V2's rules do** (coordinator
   follow-up, before merge): V4's and the review V1's last option sentence, "code refuses an
   inconsistent combination", is not true under V2, so V5 says code uses an option only when the
   maintenance action raises that level, and the review's V2 only when Jev chooses CONTINUE.
   The action is named in words, not by its question ID (the pinned skill: question IDs are not
   sent to the model). V4's and the review V1's texts are byte-identical to before (their stored
   judgments replay). Both new question sets were unreleased: never sent to the real Jev.
3. **KEEP on a raise versus KEEP on a continue.** A maintenance raise whose option answer is KEEP
   keeps the level with `STOP_OPTION_NOT_USABLE` (reason `KEEP`): the action asked for a raise.
   A review CONTINUE with KEEP keeps the level with no code: CONTINUE lets each level stay or
   rise.
4. **The in-flight skip is decided from the ledger.** Maintenance passes are sequential (the
   runtime awaits each), so a minute cannot be reviewed while the trade's review runs; the first
   pass after it compares the minute's end with the previous request's request and decision
   times. This is restart-safe (no memory needed) and records each skipped minute once. Only the
   latest completed minute is ever a candidate, as in V1: minutes that complete while no pass
   runs at all (a stalled runtime) are neither reviewed nor recorded.
5. **The bound is the research cycle's `max_inflight`** (5 in the deploy example): ten trades
   take at most two waves of the 10-second deadline. A component built without one (fixtures)
   uses 10.
6. **History entries carry the chosen option's price**: option IDs are renumbered at every
   review, so "S1" a minute ago may be a different price now.
7. **`review_status` is unchanged**: the transport still flags any tied or Insufficient answer
   `NEEDS_REVIEW`; V2's rule decides what it means (a V2 decision can be `APPLIED` with
   `review_status` `NEEDS_REVIEW`).
8. **The Bitcoin shock event keeps `CRYPTO_MAINTENANCE_V1`** as its `policy_id`: one market fact
   for every maintained trade, detected by V1's rule, which V2 keeps.
9. **Status.** `policy_id` in `trade_maintenance` and `day_reviews` now names the version
   admission records; `open_trades_by_policy` shows what is open under each.
10. **Results.** `unchanged_plan` now classifies an applied maintenance decision by the levels it
    moved; for V1 decisions this is exactly the action's name.
11. **Harness.** The maintenance component has its own clock and reviewer (the wall clock plus
    the minutes `--maintenance-minutes` moved), separate from the 24-hour review's clock, so
    `answered_at` and the apply checks stay consistent; with the default (1) the harness behaves
    as before.
12. **The harness's review clock holds between steps** (coordinator follow-up, harness only):
    the wall clock until `review --at` or `--minutes` moves it ahead, then where the last such
    step put it (never behind the wall clock). A plain `review` pass does not move it, and a
    step validates every option before moving it. A side effect: the Jev breaker's 30-second
    cooldown is timed on this clock, so if three review calls in a row fail in one step, the next
    review call waits for a `review --minutes 1`. Found in passing, not changed: the review
    reviewer's receipts are dated on the review clock (about a day ahead of the wall clock), so
    three failed review calls would also hold the breaker open for the session's wall-clock
    reviewers (selection and maintenance), as before this package.
13. **Ties** are exact equality of the printed probabilities, as V1's rule; `0.94` and
    `0.9400000000000001` are not tied.

## Open items and questions

- **A real-Jev session of the minute cadence** (owner or coordinator): `start --jev typesafe
  --selection-rule TOPK2`, `execute --simulate-prints --hold-open --maintenance-minutes 5`, then
  `review --at request` / `--at review` as before. Budget five maintenance calls per maintained
  trade plus the reviews.
- **Real bars.** Every review context so far was built on the harness's synthetic bars; Jev's
  judgment of real 1-minute price action is untested.
- **Cost and limits of about 1,440 calls per trade per day** (plus event and 24-hour reviews):
  TypeSafe cost, the breaker's behaviour under that rate (the known "breaker never recovers"
  defect is untouched), and the Alpaca data API (one 1-minute-bar GET per coin per minute on top
  of today's reads).
- **The 10-second review deadline** is V1's; a slow provider now costs a minute's review rather
  than a quarter-hour's (skipped, recorded).
- **Not addressed:** the acceptance check `SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER` and unknown
  fees in harness sessions (properties of the simulated venue); the owner ledger (not touched,
  not migrated); a supervised paper session.
