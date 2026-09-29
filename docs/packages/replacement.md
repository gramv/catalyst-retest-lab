# Package replacement — Jev's next-ranked pick replaces a declined top-K pick (plan phase 3b)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-replacement`, from `work/2026-09-24-product-plan` at `9b1bf10`. Plan:
`docs/CRYPTO-AGENT-LOOP.md` sections 2, 3, 4.2, 4.3 and 5 (owner-approved 2026-09-26) and the
owner decision relayed for this package on 2026-09-27: "a pick that fails is replaced by Jev's
next-ranked pick that passes, so the run keeps up to 10 live picks". Builds on
`docs/packages/selection-topk.md` (`RESEARCH_RANKING`, `ResearchCycle.publish_ranked`) and
`docs/packages/system-check.md` (`SYSTEM_CHECK_V1`, `RESEARCH_RUN_SUPERSESSION_V1`). This file
carries the text the coordinator merges into `docs/PHASES.md` and
`docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch.

## PHASES entry (paste as written)

### 2026-09-27 — Replacement of declined top-K picks and three visibility fixes (plan phase 3b, package replacement) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
scripted mock Jev transport, a fixture universe, Alpaca market data behind an httpx
MockTransport and the session harness over the same. No broker, provider, network or
owner-ledger contact; no migration (schema stays 21); admission SQL unchanged. Nothing is
activated: replacement applies only to cycles under `JEV_TOP_K_SELECTION_V1`, which stays the
owner's switch (`MANAGED_SELECTION_RULE`).

- **Rule `TOPK_REPLACEMENT_V1`** (`ResearchCycle.replace_declined`, `research_cycle.py`). When
  the runtime declines a top-K selection for a permanent admission refusal other than
  `SUPERSEDED_BY_NEW_RESEARCH`, the same ledger transaction, under the shared lock every
  publication takes, walks the cycle's `RESEARCH_RANKING` to the next RANKED entry not yet
  selected or skipped and publishes it through `publish_ranked(..., replacement_for=<declined
  item>)`. Entries `publish_ranked` refuses (`DUPLICATE_SYMBOL_IN_RUN`, `REVIEW_EXPIRED`,
  `TOPK_ENTRY_SKIPPED`, `TOPK_RANKING_BINDING_FAILURE`) are passed over and listed. One
  `RESEARCH_REPLACEMENT` per declined pick (key
  `research:<cycle_id>:<item_key>:<revision>:replacement`): `PUBLISHED` with the replacement, or
  `EXHAUSTED` (`TOPK_RANKING_EXHAUSTED`). No decision once the cycle or the declined pick has
  expired or a newer `run_slot` has been published. The replacement is admitted on a later
  tick through the normal path (admission SQL, price grid, `SYSTEM_CHECK_V1`) and, if it fails,
  is replaced in turn. A cycle never has more than K live picks.
- **Runtime** (`managed_runtime.py`): `decline_selection` commits the decline and the decision
  together or not at all. A refused decision (a cycle-level code such as
  `RESEARCH_POLICY_CHANGED`) writes neither, is audited once per runtime, selection and code as
  `RUNTIME_REPLACEMENT_FAULT` without a latch, and is retried every tick. For top-K selections
  only, the recorded price-grid refusals (`CRYPTO_LEVEL_OFF_PRICE_GRID`,
  `CRYPTO_PRECISION_UNAVAILABLE`, already final for their receipt) now decline and so are
  replaced; every other rule keeps retrying them each tick as before.
- **Visibility fixes.** Research context `last_run`: each pick's true `status` (a declined pick
  is no longer `SELECTED`), `selection_status` (`SELECTED`, `ADMITTED`, `DECLINED`,
  `REPLACED_BY`, `SUPERSEDED`, `EXPIRED`), `decline_code`, `replaced_by`,
  `replacement_outcome`, `replacement_for`, `jev_rank`, `ranking_status`, `ranking_reasons`,
  `skip_reason` and a retired setup's `setup_reason`; the run adds `selection_policy` and
  `ranking` (`policy`, `k`, `counts`, `complete`). `/setups` (and `/positions`, `/results`,
  which share the allowlist) keep a V3 setup's `entry_type` and `system_check`.
- **Session harness** (`scripts/agent_research_session.py`): `execute` declines and replaces
  top-K picks with the runtime's own functions; its admission rows report the replacement and
  `decisions.json` lists each cycle's `replacements`.

Evidence: `tests/test_replacement.py` (20 tests): a declined rank-2 pick replaced by rank K+1
in the decline's own transaction (decline, replacement selection and decision in that order;
admission SQL accepts the replacement) and admitted on a later tick to an authorized entry
(limit at max entry); a chain of two failures walking to rank 7; a skipped entry never walked,
a duplicate in the run and an expired candidate passed over and listed; exhaustion recorded
once per declined pick; no replacement after a newer run, for the supersession pass, or once
the declined pick or the whole cycle has expired; one decision across repeated ticks, a
restarted runtime, the research pass and a repeated decline; four simultaneous declines (one
pick twice) giving three replacements, ranks 6, 7, 8 in lock order, never more than K live
picks; a refused decision (another cycle policy) writing nothing, latching nothing, audited
once and succeeding on the next tick; V3-under-V2, legacy V2, B1 and B2 declines never
replaced (runtime and research side); a top-K off-grid pick declined and replaced while a V2
off-grid pick keeps being retried; the research context's statuses, ranks, replacement chain,
ranking summary, EXPIRED and SUPERSEDED picks and a retired setup's reason; `/setups` listing a
V3 setup's `entry_type` and `system_check` (and not a V2 setup's); the session harness
replacing a breakout-refused pick and exporting the decision. No existing test changed. Full
suite and ruff: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### Replacement rule `TOPK_REPLACEMENT_V1` (2026-09-27, package replacement)

Owner decision relayed 2026-09-27 (`docs/CRYPTO-AGENT-LOOP.md` 4.3 and 5): "a pick that fails is
replaced by Jev's next-ranked pick that passes, so the run keeps up to 10 live picks". A named
version under the owner's 2026-09-24 ruling. It applies only to cycles whose `RESEARCH_STARTED`
names `JEV_TOP_K_SELECTION_V1`. `JEV_TOP_K_SELECTION_V1`, `SYSTEM_CHECK_V1`,
`RESEARCH_RUN_SUPERSESSION_V1`, V1, the V2, B1 and B2 rules, operator `ENGINEERING_TEST`
enrollment, the admission SQL and every stored packet keep their definitions; no migration.

**Trigger.** The runtime declines a top-K selection (`RESEARCH_ADMISSION_DECLINED`) for a
permanent admission refusal: a code of `PERMANENT_ADMISSION_REFUSALS` (among them the system
check's `STOP_DISTANCE_BELOW_MINIMUM`, `PRICE_MISMATCH`, `STOP_ALREADY_HIT` and
`BREAKOUT_NOT_ENABLED`, geometry and reward-to-risk at max entry `INVALID_OR_EXPIRED_SETUP`,
migration 021's `TOPK_VETOED`, `TOPK_SCORE_MISMATCH` and `TOPK_RANKING_BINDING_FAILURE`, and the
earlier binding and receipt codes) or, for top-K selections only, the price-grid refusals
`CRYPTO_LEVEL_OFF_PRICE_GRID` and `CRYPTO_PRECISION_UNAVAILABLE`, which are final for their
receipt (`TOPK_PERMANENT_ADMISSION_REFUSALS`). Transient refusals (`LIVE_PRICE_UNAVAILABLE`,
halts, `ACTIVE_SYMBOL_ALREADY_MANAGED`, `CORRELATION_UNKNOWN`, the crypto entry window, …)
never decline and so never replace.

**No decision** (nothing recorded) when the decline is `SUPERSEDED_BY_NEW_RESEARCH` (the run is
over); when at the decline the cycle (`RESEARCH_STARTED.expires_at`) or the declined pick (its
review deadline, a report V3 pick's packet expiry) has expired; or when a V3 selection of a
later `run_slot` than the cycle's has been published (the newest run as
`RESEARCH_RUN_SUPERSESSION_V1` reads it).

**The walk.** In the decline's own ledger transaction, under the shared advisory lock that
also serializes every publication and admission: go down the cycle's `RESEARCH_RANKING` (only
the event keyed `research:ranking:<cycle_id>`) in entry order; pass by every entry that is not
RANKED, is already selected (declined picks included) or is recorded as
`RESEARCH_SELECTION_SKIPPED`; publish the first remaining entry through
`ResearchCycle.publish_ranked(conn, cycle_id, item_key, replacement_for=<declined item_key>)`.
An entry it refuses with `DUPLICATE_SYMBOL_IN_RUN`, `REVIEW_EXPIRED`, `TOPK_ENTRY_SKIPPED` or
`TOPK_RANKING_BINDING_FAILURE` is listed in `passed_over` and the walk continues; the walk
writes no `RESEARCH_SELECTION_SKIPPED` of its own. It stops at the first publication or at the
end of the ranking. Any other refusal concerns the cycle (for example
`RESEARCH_POLICY_CHANGED`): the transaction commits nothing, neither the decline nor the
decision; the runtime appends `RUNTIME_REPLACEMENT_FAULT` (`runtime_id`, `cycle_id`,
`item_key`, `selection_event_seq`, `declined_code`, `code`) once per runtime, selection and code
without latching entries, the selection stays offered, and every tick retries both.

**Record.** One `RESEARCH_REPLACEMENT` per declined pick, key
`research:<cycle_id>:<item_key>:<revision>:replacement`: `{cycle_id, replacement_rule:
"TOPK_REPLACEMENT_V1", declined_item_key, declined_revision, declined_symbol, declined_rank
(Jev's), declined_code, declined_selection_event_seq, decline_event_seq, outcome:
PUBLISHED|EXHAUSTED, code (null, or TOPK_RANKING_EXHAUSTED), replacement_item_key,
replacement_revision, replacement_symbol, replacement_rank, replacement_selection_event_seq
(each null when EXHAUSTED), passed_over: [{item_key, rank, symbol, code}], ranking_event_seq, k,
run_slot}`. The replacement is an ordinary top-K `RESEARCH_SELECTED` whose packet's
`replacement_for` names the declined item, so the chain reads both ways. A decline already
recorded is kept as recorded (its reason decides) and an existing decision is returned
unchanged: repeated ticks, a restart or a repeated decline never write a second decision or
publish a second replacement. The decline, the replacement's selection and the decision commit
together or not at all.

**Invariants and admission.** A cycle has at most K live selections (selected, not declined,
not expired, its run not superseded): ranks 1..K are published as before and every later
publication in the cycle is the one replacement of one decline. The replacement is admitted on
a later tick through the normal path (admission SQL with migration 021's ranking binding, the
broker price grid and `SYSTEM_CHECK_V1`); if it is declined too it is replaced in turn, further
down the ranking.

**Visibility (same package).** The research context's `last_run` shows each pick's true status
(`status`, `selection_status` `SELECTED`/`ADMITTED`/`DECLINED`/`REPLACED_BY`/`SUPERSEDED`/
`EXPIRED`, `decline_code`, `replaced_by`, `replacement_outcome`, `replacement_for`, `jev_rank`,
`ranking_status`, `ranking_reasons`, `skip_reason`) and the ranking's `policy`, `k`, `counts`
and `complete`; `/setups`, `/positions` and `/results` list `entry_type` and `system_check`
among a setup's state fields.

## Files

New: `tests/test_replacement.py` (20 tests), this file.
Changed: `src/catalyst_lab/research_selection_topk.py` (the rule's names, codes and key),
`src/catalyst_lab/research_cycle.py` (`replace_declined`), `src/catalyst_lab/managed_runtime.py`
(`TOPK_PERMANENT_ADMISSION_REFUSALS`, `permanent_refusal`, `replaces_on_decline`,
`decline_selection`, `REPLACEMENT_FAULT_EVENT`; `_admission_refused` and `_decline` use them),
`src/catalyst_lab/research_context.py` (the last run's statuses and ranking),
`src/catalyst_lab/managed_service.py` (`STATE_FIELDS`), `scripts/agent_research_session.py`
(the session's decline path and export), `docs/API-CONTRACT.md`, `docs/MANAGED-RUNTIME.md`.

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-replacement`), one
pytest process at a time.

- Targeted during the work: `tests/test_replacement.py` 20 passed; `test_selection_topk`,
  `test_research_report_v3`, `test_research_context`, `test_managed_service` and
  `test_system_check` 339 passed; `test_managed_runtime` and `test_agent_research_session`
  passing.
- **Full suite at `2989487` (plus this record and a test comment): 2,963 passed, 0 failed**
  (9 min 55 s; baseline 2,943 plus the 20 new tests), about 8.4 GB free before the run.
- `ruff check src tests`: all checks passed. (`ruff check scripts` still reports one
  pre-existing line length in `scripts/import_managed_costs.py`, untouched here.)

## Deviations

1. **Price grid.** The brief's trigger is "any code in `PERMANENT_ADMISSION_REFUSALS`" with the
   price grid among its examples, but the grid refusals are not in that set (the system-check
   package kept them out; V2, B1 and B2 still retry them every tick). They are final for their
   receipt already, so for top-K selections only they now decline, and are replaced
   (`TOPK_PERMANENT_ADMISSION_REFUSALS`). Every other rule is unchanged.
2. **"Classification-permanent".** Read as "any other code classified permanent".
   `CORRELATION_UNKNOWN` (a coin outside the owner's crypto buckets) stays transient: the owner
   can add the bucket and restart. Such a pick neither declines nor is replaced (open question).
3. **The exceptions record nothing.** For supersession, expiry and a newer run no
   `RESEARCH_REPLACEMENT` is written, so the outcomes stay the brief's `PUBLISHED | EXHAUSTED`;
   the decline's reason, the cycle's expiry and the newer run's selections explain them.
4. **"The cycle's picks have expired"** is checked at the decline as the cycle's expiry or the
   declined pick's own review deadline: a pick past its own validity expired, it did not fail.
   A candidate past its deadline is only passed over (`REVIEW_EXPIRED`).
5. **Passed-over entries** are listed in the decision only, never recorded as
   `RESEARCH_SELECTION_SKIPPED`; a later decision tries them again (the refusals are permanent,
   so it passes them again). `TOPK_RANKING_BINDING_FAILURE` of one entry is passed over rather
   than stopping the walk, so one broken entry cannot block every decline of the run (the
   initial publication of ranks 1..K is unchanged and still stops on it). Skipped and
   already-selected entries are not tried and not listed.
6. **Walking from the top.** The walk starts at rank 1 and passes the taken entries; after the
   first publication ranks 1..K are all selected or skipped, so this is the next free rank.
7. **Faults roll the decline back too**, keeping "the same transaction": the pick stays
   offered and each tick retries (a recorded system-check refusal re-raises without a price
   read). The fault is `RUNTIME_REPLACEMENT_FAULT`, once per runtime, selection and code, and
   latches nothing; a database error latches entries like any failed audit write.
8. **Session harness** changed although the brief did not name it, so a session (for example
   the owner's real-Jev run of 20 picks) replaces declined picks exactly as the runtime.
9. **Context fields.** `status` keeps an admitted pick's setup state (the existing contract and
   its test), so `ADMITTED` appears as `selection_status`. `REPLACED_BY` is a status value and
   `replaced_by` names the item. Unselected top-K picks after the ranking show `NOT_SELECTED`,
   `SKIPPED`, `VETOED` or `NOT_RANKED` instead of the review disposition (`RANKABLE`).
   `setup_reason` now falls back to the revocation reason (the system-check package's open
   item), and `selected` is always a boolean. `EXPIRED` uses the selection's review deadline.
10. **`/setups`** lists the whole `system_check` object, not only its result code, by adding
    `entry_type` and `system_check` to `STATE_FIELDS` as the brief says; `/positions` and
    `/results` use the same allowlist and show them too.

## Open items and questions

- **`CORRELATION_UNKNOWN`** under top-K: should a pick of a coin outside the owner's buckets
  decline and be replaced (it cannot be admitted until the owner adds a bucket and restarts)?
- **Same coin with an open trade** (plan 5: "new pick skipped"): `ACTIVE_SYMBOL_ALREADY_MANAGED`
  stays transient, so such a pick waits (holding its slot) until the trade closes or the pick
  expires; skip-and-replace belongs to the phase-4 tradability checks.
- **"At least 5 whenever Jev ranked enough picks"** holds while the ranking has entries left;
  once exhausted a run can fall below 5 live picks (recorded as `EXHAUSTED`).
- **Admission SQL does not bind `replacement_for`**: it binds a packet to its ranking entry
  only, so a rank-above-K selection published outside the runtime's walk (a direct
  `publish_ranked` call) is equally admissible. Binding such selections to a
  `RESEARCH_REPLACEMENT` would need migration 022; not built, as no migration was expected.
- **Unchanged from earlier packages:** K applies per agent report, not per run; duplicate
  symbols across agents keep the first selection; the admission queue lists at most 30
  selections (declined ones are excluded before the limit; replacements add selections); the
  real-Jev run of 20 picks is still to do.
