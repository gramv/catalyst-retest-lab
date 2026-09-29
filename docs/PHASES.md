# Implementation ledger

Strategy: `CATALYST_RETEST_V1`; source rulebook frozen 2026-09-17. From 2026-09-24 the running
system is the managed engine under versioned policies; V1 is the archived baseline (see below).

## September 24 — owner ruling, product plan and baseline snapshot

Owner ruling (chat, 2026-09-24): there is one paper account; the numeric risk rules are not fixed
and may be changed by version where they are too restrictive. Research agents (Muse, Instinct,
Grogbot and similar) must include detailed reasoning for every pick, and that reasoning must reach
Jev. The approved plan is `~/.claude/plans/enchanted-floating-shore.md` (context, cross-cutting
rules, owner decisions R1–R8, Phases 0–4, forex research-only, release gates G0–G6). Design
consequence: only the managed engine runs on the account; V1 stays the archived frozen baseline;
the control cohort is a randomized fixed-exit arm inside the managed engine; changed rules ship as
`JEV_MANAGED_RISK_V2`. The ruling and the approved numbers are recorded in CONTRACT-RESOLUTIONS.md.

State verified 2026-09-23/24: the Mac rebooted at 22:22 on Sep 23 and every project PostgreSQL
cluster shut down cleanly; nothing listens on 8765, 8768, 8769 or 8770; the live managed ledger is
schema 13 while the code requires 14; broker credentials were process-local and are gone; git works
again after the earlier Xcode-license failure cleared.

Defects verified in code at this checkpoint (fixes are in progress on the plan's Phase 0 branch):
managed admission refuses any cycle with ten or fewer approvals (`research_cycle.py` never sets the
quality flag that migration 014 requires); neither engine treats a `held` stop leg as bracket
protection, although Alpaca staff statements say the stop leg stays `held` after a full fill
(unverified); an off-grid crypto stop or target makes `plan_crypto_recovery` raise every tick and
latches the runtime error; the management-review state embeds the bar window twice and exceeds the
12,000-byte cap on any real position; a rejected amendment PATCH becomes an exit; `pending_replace`
after our own PATCH reads as incomplete protection and flattens; broker polling (~180 GET/min with
zero setups) exceeds Alpaca's 200/min limit; two fixtures build sessions from the wall clock and
fail 22:00–02:00 ET (24 tests).

The Sep 22–23 crypto test series (`artifacts/muse-crypto-test-2026-09-22`, `-v2-2026-09-22`,
`-v2.1-2026-09-23`): Claude acted as Muse against the older report path; 19 real `jev-1.13.0`
calls in total — v1: 12 packets, 9 REJECTED / 3 NEEDS_REVIEW; v2: 7 packets, 3 REJECTED /
4 NEEDS_REVIEW; v2.1 never ran (empty output directory). Zero selections, orders or fills. Richer
evidence improved the component answers but never flipped the overall verdict.

Baseline snapshot: commit `d3e9ea8` on branch `work/2026-09-24-product-plan`, tag
`snapshot-2026-09-24` (817 files: migrations 009–014, the managed, review, Muse and Jev-validation
modules, scripts, docs and artifacts). Hygiene: `.gitignore` now excludes runtime/private files and
`.raw` page captures; seven audit exports over 1 MB moved to
`~/.local/share/catalyst-retest-lab/evidence/` with SHA-256 manifests left in place;
`tests/test_repository_hygiene.py` enforces the 1 MB cap and rejects credential-shaped content.
Nothing was pushed, no owner ledger was touched and no service was started. Full suite at the
snapshot: **1,208 passed / 24 failed** (the fixture time bomb only); Ruff clean.

## September 24 — owner-ledger guard (plan package 0.2)

`localdb.start` now migrates only a fresh cluster. An existing cluster with pending migrations is
refused with `PENDING_MIGRATION_REQUIRES_OWNER_STEP`; a cluster started only for that check is
stopped again. An owner `LEDGER.json` marker (`ACCOUNT_LEDGER` | `ARCHIVED`) refuses before initdb
or migration, and an unreadable or unknown marker fails closed. The CLI `--local-dir` default is
now `~/.local/share/catalyst-retest-lab/dev`. `setup-mac.sh` initializes that directory explicitly
and reports `.env` keys as present or empty, without value lengths. The new owner step
`ledger-migrate --expect-current N --target M --backup-manifest PATH` connects only to an already
running cluster. It applies `N+1..M` in one owner transaction under the audit-append lock and
reports the schema version and audit head/count before and after. A changed head is flagged, a
removed or rewritten audit row rolls the batch back, and `ARCHIVED` ledgers are refused.

Evidence is local only: `tests/test_localdb_guard.py` uses disposable `/tmp` clusters with
`LAB_FIXTURE` events. It includes real migration 014 applied to a schema-13 fixture ledger, with
the audit head unchanged and independently re-verified. No owner ledger was opened, migrated or
marked. Writing `LEDGER.json` for the account and archived ledgers remains the owner's step in
package 0.8. One-off research scripts that call `localdb.start` on an existing older ledger now
refuse, as planned. Validation: **1,261 tests passed** (02:18–02:20 ET), Ruff clean.

## September 24 — admission fix and research-loop isolation (plan package 1.1)

Fixture and disposable-PostgreSQL evidence only; no provider, broker, service or owner-ledger
contact. Defect: `approved_packets` set `quality_required` only above the selection limit, while
migration 014 refuses every `MUSE_JEV_RESEARCH_SELECTION_V2` packet without that boolean, so any
cycle with 10 or fewer approvals failed admission with `QUALITY_POLICY_REQUIRED`. Publication
now sets `quality_required=false` and `selection_limit` when unpublished approvals fit, and is
add-only: a cycle records at most `selection_limit` selections, a published packet keeps its slot
even after supersession, only unpublished approvals compete for the slots left, and QUALITY
reviews/ranking run only when they exceed them. A late evidence revision can no longer rewrite
a stored packet (`IDEMPOTENCY_CONTENT_MISMATCH`). Historical unflagged selections are not
rewritten and remain unadmittable.

The runtime isolates research cycles (one `RESEARCH_CYCLE_FAULT` per cycle and code), records
`RUNTIME_ADMISSION_REFUSED` once per runtime, selection and actual reason instead of a masked
reason every tick, and appends `RESEARCH_ADMISSION_DECLINED` for permanent refusals (stored
binding/integrity failures, expiry, `TICKER_ALREADY_ATTEMPTED`) so they are neither retried nor
subscribed. Mocked complete cycles with 8, 10, 11 and 20 approvals each admit, fill and close one
stock and one crypto setup through `lab.managed_review_failure`. Full suite: **1,245 passed**;
Ruff clean. Actual Jev/broker acceptance is unchanged and still outstanding.

## September 24 — crypto price-grid protection fix (plan 3.1, grid part)

`plan_crypto_recovery` no longer raises for off-grid levels. The app-managed target is never
grid-checked; time, target, grace, authorized and residual exits are decided before any grid step;
only the native stop-limit sent to the broker is rounded **up** to the next grid price, recorded as
`stop_snapped_to_grid` with both values in the `PROTECTION_PLAN` event and any halt record. If the
rounded stop would sit at or above a fresh bid, or at or above the target, the plan is HALTED
`CRYPTO_STOP_UNSNAPPABLE` (latched as `MANAGED_CRYPTO_CRYPTO_STOP_UNSNAPPABLE`); the stored stop is
never rewritten. Admission refuses off-grid crypto levels with `CRYPTO_LEVEL_OFF_PRICE_GRID` at a
cost of at most one broker read per refused packet (`CRYPTO_ADMISSION_REFUSED` recorded once per
receipt; `CRYPTO_PRECISION_UNAVAILABLE` when the asset has no usable increment;
`CRYPTO_ASSET_METADATA` recorded once per symbol, New York day and increment). Entry eligibility
also rejects off-grid levels without a broker read; any `CryptoExecutionError` at entry becomes
`RISK_REJECTED` with its code; intake rejects a report that contains an off-grid crypto item when
same-day asset metadata exists. Rounding a legacy stop up to the grid follows the approved plan's
recommendation for its ruling 15 and stays adjustable by the owner (CONTRACT-RESOLUTIONS.md).

Evidence is fixture-only: `tests/test_crypto_price_grid.py` (15 tests), 12 new planner tests in
`tests/test_crypto_execution.py`, and a 20,000-run randomized planner check with no exceptions.
Merged as `f0c3703`; branch suite **1,261 passed**, Ruff clean.

## September 24 — fixture clock (plan package 0.3, tests only)

Wall-clock fixtures no longer straddle a New York date. `tests/clock.py` clips `now ± hours`
sessions inside `now`'s NY date and moves a fixture-owned clock forward (never back, at most
20 minutes) when it is within 10 minutes of NY midnight; database `clock_timestamp()`
deadlines use real time, so pinning a past date cannot work. Applied to the cross-engine legacy
candidate, the review-storage candidate and the `ManagedVenue` clock; three measurement
assertions now read `as_of` from the venue clock. `tests/test_fixture_clock.py` sweeps every
quarter hour plus near-midnight instants (normal and both DST dates) through pure `validate()`.
Real-clock suite: **1,250 passed** at 02:37 ET, outside the failure window. A scratch
forward-clock simulation passed all 232 targeted tests at 22:30, 23:30, 23:57, 00:05 and
01:30 ET; the baseline failed 4 cross-engine tests at 22:30 and 66 venue-clock tests at 23:57
(flatten 23:54, crypto cutoff 23:50). Fixture/local PostgreSQL evidence only. Residual:
`test_jev_paper` cannot validate 23:54–00:00 ET (research deadline is real time + 60 s).

## September 24 — halt release and operator controls (schema 15)

Migration `015_operator_controls.sql` renames the halt table to `lab.execution_halt_records` and
recreates `lab.execution_halts` as an auto-updatable view of unreleased records, so all nine
readers and the positional `execution.halt()` INSERT are unchanged. Halt rows are never updated
or deleted; releases are append-only, audited rows in `lab.execution_halt_releases`. The new
`catalyst_operator` login has no table grants and acts only through audited functions:
`operator_pause` (an `OPERATOR_PAUSE` halt that blocks entry paths, never exits; also granted to
`catalyst_risk`), `operator_resume` (pauses only), `operator_release_halt`,
`operator_accept_residual` (`RESIDUAL_ACCEPTED`), `operator_request_flatten_all` and
`operator_list_halts`. CLI: `python -m catalyst_lab.managed_ops operator {list-halts,pause,
resume,release-halt,flatten-all}` over `OPERATOR_DATABASE_URL` or `--database-url`.

A release trigger enforces: unreleased halt; reason of at least 10 characters; the latest
reconciliation of either engine after the halt is clean and committed by a later transaction;
no unresolved frozen or managed authorization claim; `BROKER_EVENT_REQUIRES_MANUAL_REVIEW` needs a
later `CORRECTION` event correcting an event of the same broker order; `MANAGED_CRYPTO_*RESIDUAL*`
needs a `RESIDUAL_ACCEPTED` record. Release is operator-only and never automatic.

Evidence is local disposable PostgreSQL fixtures only (`tests/test_operator_controls.py`),
including a populated schema-14 ledger migrated to 15 with an unchanged audit head, event count
and audit matches. No owner ledger was migrated and no broker was called. Local trust auth over
the 0700 socket means role separation guards against code bugs, not the OS user. Not yet done:
flatten requests are recorded but not executed; the managed crypto planner does not read
`RESIDUAL_ACCEPTED`, so a released dust halt re-latches while the dust remains; as with any
halt, a managed setup that triggers during a pause is recorded `RISK_REJECTED`. Validation:
**1,252 tests passed** (18 new) and Ruff clean. Two `test_execution.py` TRUNCATE cases now
target `execution_halt_records`, because PostgreSQL refuses TRUNCATE of any view as "not a table".

## September 24 — broker request-budget governor and per-cause latches (plan 0.4/0.5)

`broker_budget.py`: a per-account token bucket (150 requests/min; protective reads may use up to
180/min so a burst cannot block protection; lower classes leave 10/20/40% of the bucket to the
classes above) with priority classes protective reads and recovery > the entry-time fresh account
read > account snapshot > reconciliation > research/liquidity reads; one shared broker snapshot
(positions, nested open orders, account, capital activities) per 5-second interval, refreshed early
on a trade-update fill, shared by account safety and `_broker_view`; caches for terminal orders,
capital activities (60 s, now feeding the −3% daily-halt input), the calendar per New York date and
asset metadata (60 s). `managed_latches.py` replaces the single sticky runtime error with per-cause,
per-setup latches: `PROTECTION_TICK_FAILED` clears after K consecutive clean ticks (default 10,
allowed 3–10) plus a clean reconciliation after the last failure; a continuous protection failure
longer than 30 s with a position or working entry open becomes the durable halt
`MANAGED_PROTECTION_PERSISTENT_FAILURE`; 429s and timeouts become `REST_DEGRADED` (entries
blocked, protection never latched); invariant failures clear only through the audited
`managed_ops clear-protection-latch`; every latch and clear is an event. Latches are per process.

Evidence is fixture-only: 29 budget tests and 24 latch tests. With 10 WATCHING + 10 OPEN setups the
governed runtime made 491 requests in 10 simulated minutes (at most 59 in any minute, 48/min
steady) against 5,286 per minute ungoverned; a 120-second run on a real disposable ledger matched
the simulation's per-path counts. Merged as `65bf010` after resolving two "both added" conflicts
with packages 0.6 and 3.1 (imports and the `managed_ops` command block). Not covered: HTTP status
and the watchdog still expose only `error` (plan 4.5); market-data sources make requests outside
this budget; a DELETE/PATCH with an unknown response whose recovery GET fails stays pending (plan 3.1).

## September 24 — review dossier, per-item intake and agent selection rationale (plan 1.2, 1.2b)

Fixture and disposable-PostgreSQL evidence only (mock Jev transport, mock paper venue); no
provider, broker, service or owner-ledger contact and no migration (schema stays 15).

Selection Jev now reviews a code-built `REVIEW_DOSSIER_V1` (`research_dossier.py`) that intake
stores as the packet `state`; admission SQL already binds the receipt's request state to that
stored state, so the reviewed input and the packet stay one object. It holds every text field
and excerpt untruncated, the code-computed metrics over all bars, only the bars the levels or
rationale claims cite (all 20–64 were embedded before, and 64 alone exceeded the review cap),
and the rationale without the agent's confidence. Budget: `jev_review` refuses review states
over 12,000 bytes; measured wrappers inside that cap are 0 bytes (SKEPTIC) and 29 bytes
(QUALITY `{"candidate":…,"muse_rank":30}`), so the ceiling is 11,971 and the chosen state
budget is 11,000 (971 bytes of headroom for a later second-stage wrapper), with a 3,000-byte
ceiling for the rationale inside it. Sizes are the escaped JSON Jev receives (non-ASCII 6
bytes, astral 12). An oversized item is rejected `DOSSIER_OVER_BUDGET` with its measured
bytes, never truncated. Each accepted item appends a `RESEARCH_DOSSIER` manifest: budgets,
state bytes and SHA-256 (= `evidence_hash`), per-section and per-source sizes and hashes,
included/omitted bar IDs with hashes, rationale bytes and citations, `truncated: []`.
Measured: a typical 21-bar fixture item with rationale is 3,764 bytes; 64 bars + 6,059 excerpt
characters + an 875-byte rationale is 10,879. Offline recompilation of the 36 archived items of
the Sep 19–20 managed-intake reports (no provider or database) gives 2,538–4,826 bytes (median
4,639) with none rejected; those items carry no rationale, since it did not exist yet.

Intake validates the envelope and each item separately. A failing item appends
`RESEARCH_ITEM_REJECTED_AT_INTAKE` (index, signal ID, code, field-path errors, item SHA-256;
invalid content is kept only as that hash) and its siblings proceed; the 202 response and
`RESEARCH_STARTED` carry `item_results`. An invalid envelope, credential-like content anywhere
(screened with `jev_review`'s patterns over the whole body), staleness/expiry, or no acceptable
item still refuses the whole report with nothing stored; 422 bodies name field paths and codes,
never values. The crypto off-grid intake check became per item (safe: the report hash depends
only on submitted content, so replays are unaffected). A pinned test shows a legacy report
hashes exactly as under the previous intake code, so recorded reports keep replaying.

`selection_rationale` (`AGENT_SELECTION_RATIONALE_V1`): 1–8 claims, each citing at least one
source or bar retained in the same item (unknown, repeated or missing citations are rejected
with the exact path), why-now, why-these-levels, why-over-peers, what-would-change-my-mind,
0–5 known risks and `agent_confidence`. The review state carries it as
`rationale` (labelled `UNVERIFIED_PROPOSER_CLAIMS`), so it is covered by `evidence_hash`;
`agent_confidence` lives only in the packet body's `selection_rationale` for analytics and
never reaches any Jev request (checked on SKEPTIC and QUALITY traffic). `AGENT_RESEARCH_REPORT_V2`
bodies require it; unversioned legacy bodies may omit it and store `null`, never synthesized.
`MUSE_RESEARCH_GUIDELINES_V2` (V1 text unchanged and still importable, SHA-256 `0c04bbea…`)
requires the block and the Codex report schema makes it non-null; `muse_worker.py` needed no
change (its unversioned payload is accepted and the rationale validated when present).

Deviations and open items: the plan's "64 bars + 8,000 excerpt characters fit" cannot hold under
the 12,000-byte cap, because 8,000 excerpt characters with canonical per-source metadata are
9,600–9,900 bytes before any other field; such an item measures 12,077–12,315 bytes and is
rejected, as tested. "Reserved" is a ceiling for the rationale inside the 11,000 total, not a
partition. No claim-level objection IDs: V2 answers are question-level (1.6/1.7). Follow-up
revisions (`submit_evidence`, unchanged here) carry the original rationale verbatim and are not
re-budgeted, so a follow-up that replaces sources can leave claim citations dangling and a
revision state between 11,971 and 12,000 bytes would still overflow the QUALITY wrapper (1.6).
Identical content under a new report ID still gets a fresh vote (1.5). The review state keeps
the pre-existing constant `technical_context.origin: EXTERNAL_MUSE_RESEARCH` (1.3 should
neutralize it once several agents report).

Validation: `tests/test_review_dossier.py` (8), `tests/test_selection_rationale.py` (36, including
admission SQL binding and a setup record for a rationale-bearing crypto packet) and three new
`tests/test_muse_report_cycle.py` cases; full suite **1,380 passed** (47 new), Ruff clean.

## September 24 — database backup and restore drill (plan package 4.6)

New `ledger_ops.py` (`python -m catalyst_lab.ledger_ops backup | drill | verify-manifest | prune`).
A backup holds three files. `roles.sql` is `pg_dumpall --roles-only --no-role-passwords`.
`catalyst_lab.dump` is `pg_dump --format=custom --snapshot`, taken from one exported
`REPEATABLE READ READ ONLY` snapshot. `manifest.json` records the audit head, sequence, event
count, schema version and every table's row count, all read in that same snapshot, plus both
SHA-256 values, the cluster system identifier and the tool versions. Files are mode 0600 in a
mode-0700 directory named by UTC time, and an existing backup is never overwritten. The source
cluster is only read; it is never started, migrated or written. A root that carries `LEDGER.json`
needs `--allow-owner-ledger`, and an unreadable marker is refused even with the flag.

The drill restores into a disposable private-socket cluster. It uses `localdb.start`'s initdb
arguments but never calls `localdb.start`. It applies the roles script first, then runs one
`pg_restore --create` as `lab_owner`. The roles script may contain only allowlisted statement
shapes, and only the bootstrap `CREATE ROLE lab_owner;` line is dropped. `pg_restore` creates the
triggers after loading the data, so no stamp, audit or guard trigger rewrites a stored hash;
`--disable-triggers` is not used. The drill then checks:
- roles, derived from the migrations up to the manifest's version: each is restricted and can log
  in;
- the app role's `Repository.check_role` fields;
- that `lab_owner` alone owns the schema;
- the schema version and every table count;
- head, sequence and count, plus `verify_events` against the manifest head;
- that every archived trigger is present and enabled;
- that UPDATE and DELETE of the head are refused;
- that a rolled-back app-role append chains from the head.

After the checks the drill destroys the cluster. Its scope is `AUDIT_AND_LEDGER_RESTORE_DRILL`,
never trading readiness. Optional `--expected-head` and `--expected-manifest-sha256` values act as
independent anchors. `prune` keeps 14 days and deletes only timestamp-named backups whose hashes
verify, never the newest verified one. `docs/OPERATIONS-RUNBOOK.md` gains the backup/restore
section. It documents the owner-only genuine restore and the `LEDGER_RESTORED_FROM_BACKUP` system
event appended as `catalyst_app` afterwards; that event is documented, not implemented.

Evidence is fixture and local PostgreSQL only: `tests/test_ledger_backup.py` (19 tests) on
disposable `/tmp` clusters with LAB_FIXTURE candidate, Jev receipt and system events.
- The manifest head equals the live head.
- Appends committed during a backup reach neither the manifest nor the dump.
- A normal drill passes every check and leaves its scratch root empty.
- A tampered dump or roles file fails both `verify-manifest` and the drill.
- A forged manifest (a count or the head) fails the drill and the independent anchors.
- Forged roles lines (a psql meta-command, `COPY ... TO PROGRAM`, `PASSWORD`) never reach psql.
- `prune` keeps the newest verified backup and skips unverifiable ones.
- No backup file holds a credential-shaped string or a password hash.
- A schema-13 ledger drills cleanly, with no operator role and the code-version check not
  applicable.

Removing `--snapshot` or `--no-role-passwords` makes the tests fail. The runbook's manual restore
and event append were rehearsed verbatim on a disposable cluster. No owner ledger was opened,
backed up or restored. Not built: the daily LaunchAgent and the encrypted off-host copy (an owner
ruling; never iCloud). Validation: **1,405 tests passed** (03:47–03:51 ET), Ruff clean.

## September 24 — immutable release, private config v2, ledger LaunchAgent (plan 0.7 + 4.1 ledger)

`scripts/build_release.py` builds a release from a clean tag or commit. It exports with
`git archive` to `~/.local/share/catalyst-retest-lab/releases/<commit>/` and creates the release's
own venv with `uv sync --locked`, using an explicit `UV_PROJECT_ENVIRONMENT` and copied files. The
release's own interpreter then reports the import path and hash. The build writes `release.json`
`{commit, source_sha256, built_at, python}` and removes every write bit. It refuses dirty trees,
invalid refs and existing release directories, and a failed build removes only its own directory.
`code_version` now hashes the imported `catalyst_lab` package (sorted package-relative
`.py`/`.sql`, length-framed, symlinks counted) instead of assuming a checkout at `parents[2]`. The
runtime status adds `release_commit` from a matching `release.json`.

Private configuration v2 (`config_version: 2`, exact keys) adds `status`, `operator`, `release`,
`ledger`, `backup`, `notify` (an empty placeholder for 4.2) and `logs`. The three tokens live only
in separate files, and `managed_service.authenticate` maps them to roles. Muse gets its
report/evidence/news routes, the position list and the output feeds (the Muse client's allowlist).
Status is GET-only. Operator is reserved and gets 403 everywhere. Without role tokens, the single
legacy token keeps its previous access unchanged. A v1 file loads with a `deprecation` field for
inspection and operator commands, but rendering and launching refuse it.

The launcher (`launch_component`, `scripts/run_managed_private.py`) does five things:

- It records every start. The fifth start within ten minutes trips a crash-loop breaker: an
  alarm file, then exit 0 so launchd stops restarting. It resets after ten quiet minutes.
- It verifies the release hash, location and metadata (`RELEASE_HASH_MISMATCH` and so on).
- It sends child output through a supervising parent into mode-0600 logs rotated by size
  (default 20 MiB × 5), never `/dev/null`.
- Before the app or Muse starts, it waits for the database up to `ledger.wait_seconds` (default
  120 s) with 1–15 s backoff.
- It runs the new `ledger` component as a foreground `postgres -D … -k …` (SIGTERM becomes a fast
  shutdown), and only for an `ACCOUNT_LEDGER`-marked directory.

Rendering refuses an existing destination and an unverified release. It writes six plists in start
order ledger → app → Muse → watchdog → audit → backup. The backup plist is a documented
placeholder: `managed_ops backup` refuses with `BACKUP_NOT_IMPLEMENTED` until 4.6. Preflight
reports the release check, config version, token separation and ledger marker, and still says
`ready_to_trade: false`. The watchdog reads status with the status token and adds
`RELEASE_CODE_MISMATCH` and crash-loop alarms.

Evidence is fixture-only: `tests/test_managed_ops.py` (36 tests, 22 new or rewritten for v2) and
`tests/test_build_release.py` (7 tests, a throwaway git repository with the uv step replaced). No
release was built under the owner path, no LaunchAgent was installed or loaded, no service was
started and no owner ledger was touched. The September 20 package (v1) was not modified; with this
code it fails closed and must be re-rendered from a v2 configuration. Validation: **1,415 tests
passed** (29 new; full suite at 03:48 ET), Ruff clean.

A separate local smoke check (not fixture evidence, no service and no network) built a real release
of commit `847f286` into a scratch directory with `UV_OFFLINE=1` (uv cache only). Only runtime
dependencies were installed, and no file kept a write bit. `release.json` recorded
`source_sha256` `d70ff1e8…`, CPython 3.12.7. Checks run with the release's own interpreter:
`code_version` reported that commit (`MATCH`), `verify_release` passed, and six plists rendered
and passed `plutil -lint`. `run_managed_private.py` from the release passed the release check and
then the backup placeholder refused. The same launch from the working copy, at the same commit
and hash, was refused with `RELEASE_PATH_MISMATCH`.

## September 24 — protection failure isolation and held bracket legs (plan 3.1 rest, 2.5 rule A)

Fixture and disposable-PostgreSQL evidence only; no broker, provider, service or owner-ledger
contact, and no schema change. **Held legs (2.5, rule A):** one pure classifier,
`broker_ledger.classify_bracket`, now serves the frozen V1 `RiskSafety.check_protection` and the
managed stock controller. A bracket is protected only when the parent is `filled`, the
take-profit leg is active (`new|accepted|partially_filled|accepted_for_bidding`) and the stop leg
is active or `held`, each a sell covering the position at an authorized price; both legs `held`
is not protected, and a partial fill is flattened at once as before. Owned legs in
`pending_new|pending_replace|held` (for example our own replacement, or a take-profit not yet
released) are `PROTECTION_TRANSITIONING` for 5 seconds from first sight (managed: durable
`protection_transitioning_since` plus one `PROTECTION_TRANSITIONING` event; V1: in-process), with
no mutation or flatten inside the grace; afterwards the existing cancel-and-flatten path runs.
Both fake venues now model a full fill as take-profit `new` and stop `held` (the Alpaca staff
description), a partial fill as both `held`; the review snapshot accepts a held stop. This is
fixture evidence of the recommended rule only: ruling R2 (one real `TEST-` bracket read after a
full fill, or written Alpaca confirmation) still gates any V1 or managed-stock acceptance run.

**Isolation (3.1):** each queued print is evaluated alone — a fault records
`RUNTIME_TRIGGER_FAILURE{setup_id, print_event_seq, code}`, revokes only that setup
(`TRIGGER_EVALUATION_FAILED`) and consumes the print, while other prints and every setup's
protection still run; a failed market-gap revocation keeps that market's gap (entries blocked),
latches the trigger scope and retries next tick. Account data is never an exit reason for an open
position: in `manage`, a failed or strict-refused account snapshot (e.g. a `JNLC` journal →
`UNSUPPORTED_CAPITAL_ACTIVITY`), unreadable account numbers or a missing same-day baseline (NY
midnight before reconciliation) set `account_risk = UNEVALUABLE:<code>` (one
`ACCOUNT_RISK_UNEVALUABLE` event per code and NY day), which blocks `accept_management` and the
start of accepted plans while mechanical protection continues; `account_snapshot` stays strict,
so entry authorization and account safety still refuse. A `DAILY_RISK_HALT` still exits. A
rejected price amendment records `AMENDMENT_REJECTED`, reverts the desired levels to the
broker-acknowledged ones and closes the plan — no retry, no exit; `PROTECTION_REJECTED` remains
only for a rejected crypto protective order (proposed only for uncovered inventory). A gate
refusal in `dispatch` records `AUTHORIZATION_NOT_CLAIMED{decision_id, action, code}` and never
latches: an unclaimed entry becomes `RISK_REJECTED` and releases its reservation, an unclaimed
POST moves the state revision so the next attempt uses a fresh client order ID, and a
concurrently claimed decision is recovered, never resent. Review snapshots use
`_broker_view(read_only=True)`: no dispatch, acknowledgement, link, expiry or ingest. The
protection view now also resolves unacknowledged PATCH/DELETE decisions (claimed: looked up and
acknowledged; unclaimed and expired: `UNSENT_AUTHORIZATION_EXPIRED`), so a crash can no longer
block the same change forever. A started amendment not dispatched by its review deadline expires
as `MANAGEMENT_EXPIRED(NOT_DISPATCHED)`, reverting only the undispatched level; a dispatched part
completes. While an accepted plan waits to start, a protective re-POST is checked against the
stop in force, not the desired one, so a deferred plan can no longer raise
`STOP_WIDENING_REFUSED`. `PROTECTION_PLAN` is keyed by
`protection-plan:{setup}:{lifecycle}:{digest}` (state, reason, details, proposals, residual;
body unchanged), so unchanged ticks append nothing; monitor failures carry the setup and code
once per runtime, setup and code.

Evidence: `tests/test_bracket_protection.py` (28 tests, including all 1,734 parent/stop/target
status combinations of the shared classifier and both engines through the fake venues) and
`tests/test_managed_failure_isolation.py` (16 tests: trigger and gap isolation, JNLC, deferred
plan re-protection, NY-midnight baseline, amendment 422, stale-revision refusal without latch,
read-only review, undispatched and never-sent amendments, 600 unchanged ticks). Validation:
**1,430 tests passed** (44 new), Ruff clean. Not covered here: real Alpaca leg statuses (R2),
asynchronous replace rejection after a 200 and the amendment lifecycle (3.4), exit recovery
(3.6).

## September 24 — account-risk policies, capacity and margin awareness (plan 2.1–2.3, schema 16)

Fixture and disposable-PostgreSQL evidence only (LAB_FIXTURE rows, fake paper venue, fake Jev); no
broker, provider, service or owner-ledger contact; no owner ledger was migrated. Migration
`016_account_risk_policy.sql` adds the append-only, audited `lab.account_risk_policies` with three
seeded rows (`CATALYST_RETEST_V1` and `MUSE_JEV_MANAGED_TEST_V1` reproduce the rules in force
before; `JEV_MANAGED_RISK_V2` carries the approved 2026-09-24 numbers) and
`lab.account_risk_failure`, which the V1 engine, the managed engine and all three reservation
triggers now ask (stricter-of correlation across policies, then account and market caps). The view
`lab.account_risk_reservations` gains market, venue and policy columns (legacy rows map to the
seeded policies); V1's frozen equality block is reproduced verbatim and tested byte for byte. V1
startup refuses `RISK_MAX_PER_SECTOR/THEME` values other than its row, which removes the
divergence where V1 approved an entry the trigger then refused (candidate stuck in
`TRIGGER_CONFIRMED`).

The managed engine sizes and budgets from the setup's policy row, records the policy and a
randomized arm (`FIXED_EXIT` 30% under V2, by SHA-256 of the setup id) at admission, refuses
unclassified symbols before the ticker/day attempt is spent, and turns capacity rejections into a
60-second, once-per-window `RISK_CAPACITY_DEFERRED` that keeps the setup WATCHING under V2. US day
positions may use the 2× intraday multiple; crypto stays cash-only. After sizing, both engines check
the broker's `multiplier`/`buying_power`/`non_marginable_buying_power`/`crypto_status` (reject,
never resize). Broker refusals are classified with sanitized evidence; a managed entry's margin
refusal forces re-reconciliation only. The position monitor never reviews a `FIXED_EXIT` setup.
Owner crypto buckets are a V2 classification import (unlisted symbols refused). The ledger is bound
to one paper account at its first clean reconciliation. Choices made where the plan left detail
open are listed in CONTRACT-RESOLUTIONS.md (2026-09-24, "Implemented").

New tests: `test_account_risk_policy.py` (seed numbers, immutability and audit, the verbatim V1
block, an exhaustive 1,794-check parity run over 0–3 existing reservations × V1/managed engines ×
sector/theme overlaps × limits 1–2 in which the Python check, the trigger outcome and an
independent oracle agree and no exception escapes, the `RISK_MAX_PER_SECTOR=2` regression, and a
populated schema-15 ledger migrated to 16 with every historical audited row still verifying),
`test_capacity_policy.py`, `test_randomized_arm.py`, `test_ledger_binding.py`,
`test_broker_rejections.py`; the SPY golden case (`test_risk.py`) still sizes 13 shares under the
V1 row and a buying-power shortfall reserves nothing. Not done here: analytics split by arm, and
actual broker evidence of the margin fields (an owner-run read-only account GET).
Validation: **1,586 tests passed** (61 new; full suite at 16:16 ET after merging the branch head 794247a, which added three small conflicts: the PHASES entries, the appended `AppSettings` fields and the `managed_execution` imports/constants, all resolved by keeping both sides); Ruff clean. The package was built by a worktree agent that was terminated by an API rate limit after its own green suite (1,447 passed at base 65bf010); its uncommitted work was committed and merged by the coordinator.

## September 24 — private credential injection (plan 0.9 script)

Fixture evidence only (pytest temporary directories, an injected prompt); no provider, broker,
service or owner-directory contact, and no owner secret was written. `scripts/set_private_secret.py`
injects each secret the private stack needs: the Alpaca Paper keys into the configuration's
`environment` map (rewritten atomically with every other key unchanged and re-validated first),
the TypeSafe key into the file named by `TYPESAFE_ENV_FILE` in the exact single-line format
`jev_secrets` reads, and the Muse, status and operator tokens into their configured files (typed,
or generated in-process). Values are read at a no-echo prompt, never on the command line, never
printed or logged; every file is owner-only mode 0600 in an owner-only directory, created
exclusively, replaced only with `--replace`, and symlinks are refused. `check` reports presence and
protection only, never a value or its length. `docs/OPERATIONS-RUNBOOK.md` gains the credentials
and rotation section (part of 4.8). Not built: the Codex sandbox canary and the injection itself,
both owner actions (0.9, after the guarded migration 0.8). Validation:
`tests/test_set_private_secret.py` (11 tests, including `jev_secrets.typesafe_key()` reading the
written file); full suite **1,597 passed** (11 new; 16:24–16:28 ET), Ruff clean.

## September 24 — daily backup component wired to ledger_ops (plan 4.6 follow-up)

`managed_ops backup` (the `backup` LaunchAgent component, daily `StartInterval`) no longer
refuses with `BACKUP_NOT_IMPLEMENTED`: it derives the ledger root from the configured marker
file (refusing a layout whose data or socket directory `ledger_ops` could not derive), runs
`ledger_ops.backup` with the deliberate owner-ledger flag, verifies the new backup against its
own manifest hash, then prunes with the configured retention. `ledger_ops` refusals surface as
the launcher's own codes. The restore drill stays an owner command. Fixture evidence only
(`tests/test_managed_ops.py`, backup/prune/verify replaced by fakes; no cluster, no owner
directory); full suite **1,597 passed** (16:29–16:33 ET), Ruff clean.

## September 24 — owner migration rehearsal, schema 13 → 16 (plan 0.8 preparation)

Fixture evidence only (a disposable `/tmp` cluster with LAB_FIXTURE halts, reconciliation runs,
managed events, Jev requests and receipts); no owner ledger, broker or provider contact.
`tests/test_owner_migration_rehearsal.py` marks the ledger `ACCOUNT_LEDGER` first, migrates it
through `catalyst-lab ledger-migrate --expect-current 13 --target 16` exactly as the owner will,
and pins the maintenance-window expectation: the result carries
`AUDIT_HEAD_CHANGED_BY_MIGRATION` with exactly three appended events, the audited policy rows
seeded by migration 016, while every earlier event, hash, audited row and halt row is unchanged,
the app and risk roles accept the migrated ledger, the marker keeps refusing `dev-init`, and a
second run is refused with `LEDGER_VERSION_MISMATCH`. The plan's step-7 wording ("head and event
count unchanged") is therefore corrected to "+3 and nothing else" in the runbook, which gains the
guarded-migration section with the exact commands. Validation: the new test (1) and Ruff; the
full suite runs with the next merge.

## September 24 — agent identity and attribution, minimal form (plan 1.3, attribution of 1.8)

Fixture and disposable-PostgreSQL evidence only (a mock Jev transport that keeps the exact
request bytes, a mock paper venue); no provider, broker, service or owner-ledger contact. No
migration, table or column (schema stays 15), and no registry, revocation table or scope
matrix: the owner is still choosing between the lean and the full 1.3 path, and this is the
part both need.

**Reports.** `AGENT_RESEARCH_REPORT_V2` bodies require
`agent {agent_id, agent_version, guidelines_version, guidelines_sha256, run_id}` with exact
keys (ID `^[a-z][a-z0-9_-]{1,31}$`, version at most 32 characters of `[A-Za-z0-9._+-]`,
lowercase SHA-256, UUID run). A V2 body without it, or a legacy body with it, is refused whole
(422 `AGENT_IDENTITY_REQUIRED` / `AGENT_IDENTITY_REQUIRES_V2`); a malformed field is 422 with
its path, before any credential check. Legacy bodies canonicalize exactly as before, so the
1.2b pinned report hash `9ac48d4b…` still holds.

**Credentials without a registry.** Private config v2 gains an optional top-level
`agents: [{agent_id, token_file}]` (exact keys, unique IDs, one file per agent, distinct from
the Muse, status and operator files). Absent means none, so existing v2 files still load; the
template and the example carry `[]`. The launcher reads the files, requires distinct values
(`AGENT_TOKENS_NOT_SEPARATED`) and injects `MANAGED_AGENT_TOKENS_JSON` into the app only,
never into Muse, argv or logs; preflight reports each agent's token state without values.
`AppSettings.agent_tokens` (appended, default `None`, hidden from `repr`) feeds
`create_managed_app(agent_tokens=…)`. `authenticate` compares the bearer with every token
and maps it to one principal: an agent token gets Muse's routes as its agent;
`MANAGED_API_TOKEN` stays the legacy identity, agent `muse`, the only credential allowed
unversioned legacy reports, and keeps full access in single-token mode. A report naming
another agent, a legacy body from an agent token, or the legacy token naming an agent other
than `muse` is 403 `AGENT_IDENTITY_MISMATCH` with nothing stored. Evidence claims, evidence
revisions and position news reach only cycles and setups recorded for the credential's agent
(unattributed legacy ones only the legacy token), refused before the body is read. Reads are
not restricted by agent (an open owner ruling).

**Ledger.** A V2 cycle ID is `uuid5(284306f0-…, "<agent_id>:<report_id>")` (pinned in a test),
so two agents cannot collide on a report ID; legacy cycles keep the report ID.
`RESEARCH_STARTED` and every `RESEARCH_PACKET` body carry `agent` (null for legacy) outside
`state`, so it follows the packet into `RESEARCH_SELECTED` and `managed_setups.record_json`;
admission SQL accepts it unchanged. V2 receipts' `evidence_identity` adds `agent_id` and
`agent_version`; legacy identities are unchanged, so in-flight legacy receipts still verify.
The packet body's `research_origin` is `EXTERNAL_RESEARCH_AGENT` for V2 and `EXTERNAL_MUSE`
for legacy.

**Blind review and the origin label.** The review state's constant
`technical_context.origin` is now `EXTERNAL_RESEARCH_AGENT` for all new intake, legacy
included, so identical content from any agent or the legacy token is an identical state and
evidence hash. This does not touch recorded reports: the replay hash covers only the submitted
report and stored packets are never recompiled. A test records a legacy report under the
former label `EXTERNAL_MUSE_RESEARCH`, then with the current code shows the pinned canonical
hash, an idempotent replay returning the stored packets and evidence hashes, and a review and
selection that still bind. So the neutral label was not restricted to V2 bodies. QUALITY's
wrapper key `muse_rank` is bound by migration 014 and identical for every agent, so it names
no proposer and was left unchanged.

**Worker.** `muse_worker` gains `--agent-id` and `--agent-version` (defaults `muse` and
`LEGACY_UNDECLARED`). Run as a service it now submits V2 reports whose agent block the worker
adds (guidelines version and SHA-256 from `muse_guidelines.py`, a run ID derived from the
installation and job), never the model; the Codex output schema is unchanged. A `MuseWorker`
built without an agent ID still submits legacy bodies. It follows only its own cycles, plus
unattributed ones when it is `muse`.

**Analytics (read-only, no new route).** `/analytics/research` items carry attribution
(`AGENT_ATTRIBUTED` or `LEGACY_UNATTRIBUTED`, agent ID and version, guidelines version and
hash) and intake-rejection counts, plus page-scoped `agent_groups` by agent ID, version and
guidelines hash with additive counts and one legacy bucket (never counted as Muse).
`rationale_claim_support_rate` is null with a note: SKEPTIC V1 has no claim-level answer
(`unsupported_inference` judges the whole packet; plan 1.7 adds `rationale_support`).
`/cycles`, `/setups`, `/positions`, `/results` and `/history/results` carry `attribution`,
`agent_id` and `agent_version`.

Deviations and open items:

- Evidence, claim and news bodies carry no agent block; for those routes the identity checked
  is the recorded agent of the cycle or setup (ownership).
- Agent version and guidelines hash are self-declared and unverified (no registry).
- `agent_groups` cover one page; counts are additive across pages.
- Revocation means removing the `agents` entry and relaunching the app.
- Daily rollups are not split by agent.
- Configured tokens are not screened out of report bodies (agent tokens have no fixed prefix
  for `_privacy_check`).
- Full-path items not built: `FOREX`, `/agents/me/outputs`, an operator-only global feed,
  a legacy-token end date.
- The monitoring dossier (a concurrent package) must keep the packet body's `agent` block out
  of the management Jev input; the current `position_monitor` allowlist does.

Validation: `tests/test_agent_identity.py` (38 tests, each on its own disposable database) and
2 new `tests/test_managed_ops.py` tests. Existing V2 fixtures now carry an agent block and
address V2 cycles by the derived ID; one origin assertion moved to the neutral label. Six
source mutations each fail the new tests: agent block in the state, no report credential
check, no ownership check, shared cycle ID, the former origin label, no receipt identity.
Full suite **1,565 passed** (40 new), Ruff clean. A first run on the shared session ledger
showed an existing fragility: `test_managed_service`'s `FixtureCycle.outputs` reads only the
first 100 ledger events, so enough earlier events hide its own; the new tests were isolated
instead of changing that helper.

Merge: the branch head c21b6a7 (account-risk policies, the 0.9 script, the backup wiring and the migration rehearsal) was merged into this package with two conflicts kept both ways (the appended `AppSettings` field and the September 24 PHASES entries); full suite after the merge **1,638 passed** (17:00 ET), Ruff clean.

## September 24 — size-capped position dossier (plan package 3.2)

Fixture and disposable-PostgreSQL evidence only (synthetic, clearly labelled inputs; mock
paper venue; mock Jev transport); no provider, broker, service or owner-ledger contact and
no migration (schema stays 15). No real Jev call was made: the real-provider series for
gate G4 is written but not run (see the end of this entry).

Defect reproduced: at the runtime's size the V2 management review cannot be sent. A 64-bar
one-minute window split by the monitor into 20 recent and 44 structural bars, near-limit
thesis/disproof and research text, six 1,200-character news items, a full rationale, eight
history events and five stop plus five target options gives a 45,119-byte V2 state, and
`jev_review` refuses it (`EVIDENCE_TOO_LONG`, cap 12,000). The bar window alone with one-line
texts is 20,992 bytes: each V2 bar object is about 259 bytes.

Fix: `JEV_MANAGED_POSITION_CONTEXT_V3` / `JEV_MANAGED_POSITION_QUESTIONS_V3`. The new pure
`managed_dossier.py` (`MANAGED_POSITION_DOSSIER_V1`) compiles the state deterministically to
the policy's `state_byte_budget` (11,000; the 12,000 cap stays). It sends the thesis and
disproof once, never truncated; compact research (catalyst, levels, economic relationship and
technical prose clipped to 300 characters with `{kept_chars, total_chars, sha256_prefix}`
markers, selection-time code-computed metrics, the rationale's claims with cited source/bar
IDs plus why-now, why-these-levels and what-would-change-my-mind, never `agent_confidence`);
selection judgments as `{choice, top_p}`; the last four history rows `{seq, action, reason,
stop, target}` (a judgment and its authorized plan fold into one row); R and tick distances,
a descriptive regime label and a remaining-time bucket (`POSITION_METRICS_V2`); all bars once
in one pipe-separated table under one provider/feed header (about 44 bytes a row; refs `R..`
recent, `S..` structural); options (same generator as V2, so the same choices) as
`{option_id, price, bar}` with readable provenance and distances in the question text; news
with adverse/withdrawn items first, four current and two original, excerpts clipped to 600.
Over budget, a fixed ladder reduces one item at a time (technical prose, economic
relationship, supporting excerpts, history, supporting items, structural bars that back no
option, rationale texts, last adverse excerpts); adverse items and options are never dropped.
If nothing fits, `PositionMonitor.review` writes `POSITION_REVIEW_SKIPPED{CONTEXT_BUDGET_UNSATISFIABLE}`
once per lifecycle and returns; protection is untouched. The review request stores, inside
the hashed context but outside the prompt, a manifest of every input (status, hashes,
kept/total characters, reasons, bar and option references, budget and bytes used) and
`retained` ledger references (event sequences and receipt IDs, never copies).
`POSITION_REVIEW_BARS` now keeps the whole review window (was the last 20 bars) under a
content-derived key, so every option-backing bar is in the ledger and referenced. Excursions
for the dossier come from one SQL aggregate (`sampled_excursions`,
`SAMPLED_EXCURSION_SQL_AGGREGATE_V1`) instead of rescanning every per-second sample in
Python; a parity test matches `managed_measurement` field for field, including a two-fill
average, malformed, foreign-lifecycle and out-of-range samples.

Measured with the fixtures: the maximal production fixture compiles to 10,620 bytes (64 or
60 bars) after 52 single-item reductions of 7 kinds; it keeps the adverse items at 600
characters, the 20 recent bars and the 4 structural bars that back options, and drops the
supporting news, the history and 40 structural bars. The same window with short texts is
7,508 bytes with no reduction and all 64 rows; short texts with the six full-length news
items are 10,930 bytes after two reductions. Everything the ladder removes is listed in the
manifest.

Profile: `ManagedPolicy` gained `context_version` and `state_byte_budget` (defaults keep
stored V1/V2 policies loading). The runtime and `managed_ops` preflight accept exactly
`{"quote_max_age_seconds":5,"context_max_age_seconds":60,"max_options":5,"max_bars":20,
"max_news":4,"max_structural_bars":64,"max_history":4,"context_version":
"JEV_MANAGED_POSITION_CONTEXT_V3","state_byte_budget":11000}`; `deploy/private-paper.example.json`
carries it. Replay: the V2 builder and the V1/V2 question sets are byte-identical to the
baseline (context and template hashes pinned against commit `794247a`), and a pending V2
review recovered by a V3 monitor consumes the stored receipt with no new provider call.

Validation: `tests/test_managed_dossier.py` (25 tests: the defect at builder and monitor
level with the runtime's call shape, 60/64-bar V3 fit, column encoding and mixed bar
durations, determinism across hash seeds and input order, adverse-before-supporting and
never-truncated options over budgets 12,000→1,000, manifest coverage of every input hash,
confidence and agent identity never sent, receipt binding plus tampering of state, manifest
and retained references, V2 pins and replay, the skip event, the runtime bar window, the G4
harness with a local transport, SQL parity) and production-sized V3 cases added to
`test_managed_review.py` (5), `test_position_monitor.py`, `test_monitor_context_scheduling.py`
and `test_managed_monitor_recovery.py` (2). Full suite **1,559 passed** (34 new; 1,525 at
`794247a`), Ruff clean.

Real provider (gate G4, not run): `scripts/prove_managed_jev.py --production-calls 30` sends
production-sized synthetic V3 contexts to the real provider and records per-call receipts,
p50/p95 latency, the overflow count and the invalid-response count, with no broker request
and no action applied. `--fixture-provider` exercises the same harness with a local mock
transport and a fixture key; that output is labelled and is not provider evidence. The owner
runs the real series.

Deviations and open items: the plan's section limits (4+2 excerpts of 600 characters, a full
rationale, 1,000-character thesis and disproof, 64 bars, 10 options) cannot all be sent at
their maxima within 11,000 bytes; the ladder decides deterministically and the manifest
records it. Adverse excerpts may be shortened to 300 characters as the final step. The
regime label uses level distances only (no range threshold; package 3.3 owns cadence and its
numbers). Option distances appear in the question text rather than the state. Why-over-peers
and known risks are not sent to the management review. Sections that the owner may want to
tune (600/300/120/160 characters, two original items) are constants of the versioned
dossier, not policy fields.

Merge: the branch head 5cb7239 (account-risk policies, agent identity, the 0.9 script, the backup wiring and the migration rehearsal) was merged into this package with one conflict kept both ways (the September 24 PHASES entries); full suite after the merge **1,672 passed** (17:16 ET), Ruff clean.

## September 24 — fill backfill after reconnect and audit volume (plan 4.4, 4.7)

Fixture and disposable-PostgreSQL evidence only (fake venues, a synthetic recorded tape); no
broker, provider, service or owner-ledger contact, no migration and no new column; every
broker mutation still needs its exact one-use authorization (this package adds reads only).

**Fill backfill (4.4).** Alpaca FILL account activities carry no execution id, so fills are
now deduplicated on (broker order id, cumulative filled quantity) in both engines: a REST fill
is `rest-fill:{order}:{cumulative}`, and a late stream copy of an execution already recorded
at that cumulative quantity is recognised (same quantity and price) instead of raising
`FILL_EXCEEDS_ORDER_QTY`; a copy with another quantity or price halts
`CONFLICTING_FILL_EVIDENCE` (V1) or is refused `CONFLICTING_BROKER_EXECUTION` (managed). New
`AlpacaReadOnly.fill_activities_since` pages `GET /v2/account/activities?activity_types=FILL&
after=…&direction=asc` 100 rows at a time (a repeated page token or more than 10,000 rows is
refused). V1 `BrokerMonitor`: after the listen acknowledgement and before the stream counts as
connected, it reads each non-terminal linked order, then every FILL activity after the last
recorded broker timestamp less 5 minutes, and records each missed execution or newer order
state once as a `BROKER_REST_BACKFILL` event carrying the raw REST object (with its
`broker_events` row, so the V1 trade projection counts a backfilled fill exactly once); an
activity of an unknown order is quarantined `UNEXPLAINED_BROKER_ORDER` as from the stream,
unless a claimed, unresolved V1 authorization for the symbol may explain it (left to receipt
recovery and reconciliation). `WEBSOCKET_RECONNECT` carries the backfill counts. Managed:
`ManagedExecution.rest_backfill` runs once per new trade-updates connection, before that
connection's reconciliation (`_reconcile_once_locked`, protective-and-recovery budget class):
the same reads, a `BROKER_REST_BACKFILL` event plus `managed_fills` row (source
`ALPACA_PAPER_REST_BACKFILL`, fee unknown) per missed fill, the newer state of each linked
order, and a REST-sourced `BROKER_POSITION` (`ALPACA_REST_POSITIONS`) only where the new
fills explain the broker position (stocks exactly; crypto less by under the bought quantity,
for a fee taken in the asset). An unexplained position is not adopted: reconciliation stays
unclean and entries blocked. Activities of unknown orders are ignored and left to
reconciliation. A failed backfill never halts: V1 stays unconnected (`REST_DEGRADED` for 429s
and timeouts, else `FILL_BACKFILL_UNAVAILABLE`); managed records
`RUNTIME_RECONCILIATION_FAILED{FILL_BACKFILL_UNAVAILABLE, code}`, latches `REST_DEGRADED` for
429s and timeouts, and stays unreconciled until a later pass backfills.

**Audit volume (4.7).** Still one event each: broker messages, fills, links and acks, risk
decisions, claims and results, state transitions, halts and operator actions, Jev requests and
receipts, research packets and selections, unclean reconciliations and every print that could
trigger or invalidate. Coalesced (fewer events, each still hash-chained): (1) a managed print
above the entry trigger that arrives at most 2 s old, with a quote, inside the entry window
(US: in session before the flatten time) while the runtime is ready joins one
`MARKET_PRINT_SUMMARY` per symbol and UTC minute {count, first/last trade id and time,
high, low, setup ids} instead of `MARKET_PRINT` plus `MARKET_PRINT_CONSUMED`; every other
print takes the unchanged per-print path, so a print stale on arrival still revokes the setup;
summaries are written when their minute closes (heartbeat, the next minute's print, runtime
stop). (2) `RUNTIME_HEARTBEAT` is written on change (timestamps and counters ignored) or
every 60 s; the heartbeat still runs every 5 s, and the review-worker heartbeat is unchanged.
(3) Position samples under `FIRST_VALID_OBSERVATION_PER_RECEIVED_SECOND_ON_CHANGE_V2`: the
same first valid observation per second, written only when the price, position quantity or
preceding entry fills changed, plus a keyframe at least every 60 s; an unchanged second takes
one lock-free read. `managed_measurement` reports the sampling methods present (a ledger with
only per-second rows keeps its label byte-identical) and one limitation line for the new
version. (4) Audit checkpoints are incremental: after the first, `export_checkpoint` writes
only the events after the previous checkpoint's head and records it (`verify_events` gained
`start_seq`/`start_hash`); a full export is written weekly (or on request) and must contain
the chain so far; `verify_checkpoint` on the directory verifies every checkpoint as one chain
and fails on a gap, reordering, altered content or a truncated tail; a checkpoint containing a
credential-shaped string (the hygiene test's patterns, now shared from `audit.py`) is refused
before any byte is written, and nothing is written when no event follows the head.
`PROTECTION_PLAN` was already coalesced (3.1); the V1 watcher's per-second `checked_at`
writes stay deferred while V1 is archived.

Evidence: `tests/test_fill_backfill.py` (14: reader pagination; V1 and managed outage fill
recorded once with the late stream copy not counted, partial fill across the outage, cancel
during it, more than 100 activities, a 429 that keeps watching unready without a halt, V1
quarantine of an unknown order and conflicting duplicate, managed backfill of a closed setup's
missed exit), `tests/test_audit_volume.py` (6) and `tests/test_audit_checkpoints.py` (6). The
recorded tape (`tests/fixtures/managed_print_tape.json`, 1,173 synthetic rows) replayed through
the coalesced and per-event writers on two disposable ledgers gives identical triggers,
invalidations and revocations (one entry authorized, a stop invalidation, a stale-print and a
quote-less revocation, two setups still watching) while writing 6 prints, their 6
consumptions and 8 minute summaries instead of 628 prints and 628 consumptions.
Change-only sampling gives the same observed MFE/MAE as per-second sampling. Events per
simulated minute for 10 WATCHING + 10 OPEN setups (writer-level harness on a disposable
ledger: real print writer and queue consumer, position sampler, heartbeat, reconciliation;
`manage` adds nothing on unchanged ticks): per-event writers **1,816**; coalesced **196**
steady (186 in the first minute), under a budget of 250. Validation: **1,551 tests passed**
(26 new), Ruff clean. Not covered: real Alpaca activity fields and `after` semantics (documentation
only); a fill the protection tick sees during an outage before the reconnect still meets the
existing fail-closed crypto first-fill rule; a quiet print whose evaluation would have been
delayed past 5 s no longer revokes (a stale arrival still does); at a deadline crossing the
expiry reason can read `ENTRY_DEADLINE` instead of `SETUP_EXPIRED`.

Merge: the branch head e1d2d43 (account-risk policies, agent identity, the V3 position dossier, the 0.9 script, the backup wiring and the migration rehearsal) was merged into this package with one conflict kept both ways (the September 24 PHASES entries) and one cross-package fix by the coordinator: the dossier package's `sampled_excursions` SQL aggregate reported the per-second sampling constant, so it now reports the sampling methods present in the aggregated rows, exactly as `managed_measurement()` does, and its parity test still passes. Full suite after the merge **1,698 passed** (17:27 ET), Ruff clean. Review note for later tightening: the crypto branch of the REST position check accepts a broker quantity short of the fill-explained quantity by anything less than the bought quantity as an in-kind fee; a fee bound would be stricter.

## September 24 — plan execution status at 17:30 ET (waves 1–3 merged)

Branch `work/2026-09-24-product-plan`, head e19deb5, 57 commits since the baseline snapshot
d3e9ea8 (`snapshot-2026-09-24`); nothing pushed. Full suite **1,698 passed**, Ruff clean. Every
package was built in its own worktree by an Opus 5.5 agent, reviewed and merged by the
coordinator after the full suite passed on the merge commit; all evidence in this series is
fixture and disposable-PostgreSQL evidence, with no broker, provider, service or owner-ledger
contact. Merged: 0.2 owner-ledger guard, 0.3 fixture clock, 0.4/0.5 request governor and
latches, 0.6 operator controls (schema 15), 0.7 immutable release and config v2, 0.9 credential
script, 1.1 admission fix, 1.2/1.2b review dossier and selection rationale, 1.3 agent identity
(minimal), 2.1–2.3 account-risk policies, randomized fixed-exit arm and margin awareness (schema
16), 2.5/3.1 held-leg classifier and protection isolation, 3.2 size-capped position dossier,
4.4 fill backfill, 4.6 backup and restore drill plus the daily backup component, 4.7 audit
volume, and the schema 13 → 16 migration rehearsal for 0.8. Not built yet: 1.7 approval rule V3
(awaiting the owner's lean/full choice), 3.5 management measurement, 4.2 off-host alerts
(needs the owner's account rulings), the 0.10 acceptance-evidence script, and the full-path-only
items (2.4, 3.4, 3.6, 4.3, 4.5, 4.10, 4.11). Owner actions now possible, in order: 0.8 guarded
migration of the live ledger (runbook), 0.9 credential injection (runbook), 0.10 first supervised
crypto trade with management reviews disabled; the G4 real-provider proof
(`scripts/prove_managed_jev.py --production-calls 30`); ruling R2 (one real `TEST-` bracket)
before any stock acceptance run.

## September 24 — off-host alerts (plan package 4.2)

Added `catalyst_lab/notify.py`: a dead-man ping and alarm forwarding to an owner-provided
Healthchecks.io-style ping URL, generic across accounts — the specific account/provider
choice stays with the owner, per the task. Private config v2 gains a real `notify` section
(exact keys `provider`, `ping_url_file`, `allowed_hosts`, `reminder_minutes`,
`daily_head_hour_utc`, `timeout_seconds`, `local_notification`); `{}` still disables it and
remains the default in both `config_template` and `deploy/private-paper.example.json`, so
every existing configuration and test is unaffected. The ping URL lives only in an
owner-only mode-0600 file named by the config, read fresh through `private_bytes` on every
send, never logged, printed or put in argv; its content must be exactly one `https` URL
whose host is in `allowed_hosts`, checked both when the configuration loads and at every
send.

`watchdog_once` sends a success ping every clean tick (the dead-man signal for a dead Mac,
app or Wi-Fi, none of which the process could report itself), forwards new alarm codes to
`<url>/fail` immediately and repeats every `reminder_minutes` while they persist, and once
per UTC day at `daily_head_hour_utc` adds the current audit sequence number and head hash to
the success payload — read-only, via the same `readonly_audit_repository` helper
`managed_ops` already uses elsewhere, attempted at most once a day and never allowed to block
the ping itself. Every payload is codes and counts only
(`{"alarms": [...], "audit_seq": n, "audit_head_hash": "...", "release_commit": "...",
"at": iso}`); a dedicated test proves no evidence text, symbol, price or credential can pass
the redaction gate. A failed off-host send, a broken local notification, or a misconfigured
section all become exactly one alarm code, `NOTIFY_FAILED`, written into the watchdog's own
`alarms.json`; nothing here can become a halt or change a trading decision. An optional
macOS Notification Center alert (`local_notification`) mirrors the same code-only text
through a fixed `osascript` argv, tested with a fake runner. `managed_ops preflight` now
reports `notify.state` as `NOT_CONFIGURED`, `CONFIGURED` or `INVALID`, never the URL.
`docs/OPERATIONS-RUNBOOK.md` gains the owner setup steps: creating the Healthchecks.io check
with the watchdog's interval and a grace period, optional Pushover forwarding (configured
entirely on Healthchecks' side), writing the ping file, the two required owner proofs (app
killed, Wi-Fi off), and URL rotation.

New `tests/test_notify.py` (19 tests: config validation including bad host/scheme/relative
path/missing keys, ping-URL-file content, payload redaction, the Healthchecks-style client
against both an injected sender and `httpx.MockTransport`, the local notification, and the
full per-tick state machine — transition, reminder, recovery, the once-daily head, corrupt-
or-missing-state recovery) plus seven minimal additions to `tests/test_managed_ops.py` (the
watchdog wiring for the success/fail/notify-failed paths, the daily-head repository
injection using the existing `er` disposable-database fixture, and the three-state preflight
report); every existing `test_managed_ops.py` test passes unchanged. All new tests are
fixture-only — no real network call or subprocess anywhere. Full suite
`./run pytest -q` **1,724 passed** (1,698 baseline + 26 new), `./run ruff check src tests`
clean.

Not built here: the account/provider choice itself (owner action, by design); Pushover
forwarding (nothing to build — it is configured entirely on Healthchecks' own side); the two
owner-run proofs above, which need a live watchdog and a real Healthchecks check and so
cannot be exercised from fixtures.

## September 24 — acceptance-evidence tool (plan package 0.10 tool)

Fixture and disposable-PostgreSQL evidence only; no broker, provider, service or owner-ledger
contact, no migration. New `catalyst_lab.acceptance_evidence` (pure functions building a
`MANAGED_ACCEPTANCE_EVIDENCE_V1` manifest from already-fetched rows) and
`scripts/managed_acceptance_evidence.py` (`--database-url URL --setup-id ID|--latest-closed
--output DIR [--audit-export FILE] [--broker-snapshot FILE]`) collect and hash one managed
setup's evidence — stream/subscription acks, the triggering `MARKET_PRINT`/`TRIGGER_CONFIRMED`,
`ENTRY_ELIGIBILITY`, `RISK_CHECK`, the risk decision and its 5-second TTL, the claim, `BROKER_ACK`,
fills (including REST-backfilled ones), `BROKER_POSITION`, protection (`PROTECTION_PLAN`,
stop/target acks, `PROTECTION_TRANSITIONING`), the exit request and exit fill, the `STATE` history
to `CLOSED`, `BROKER_RECONCILIATION`, released reservations and fee evidence — into nine named G3
checks plus an `unknowns` list, so gate G3 is proved from the ledger rather than hand-collected.

The tool connects only as a read-only role and refuses `catalyst_app`/`catalyst_risk`/`lab_owner`
outright, as required. Checking the actual migration grants against a disposable cluster (not
assumed) found that `catalyst_review` — not `catalyst_reporting`, which holds only the frozen V1
`strategy_*`/`public_*` views and cannot even read `lab.managed_setups` — is the role with the
managed-engine grants, and that even `catalyst_review` cannot read `lab.trade_events` (so it
cannot compute the audit hash chain itself) or `lab.execution_halts`. Both gaps are handled
honestly rather than worked around: the `halts` section and its `ZERO_RESIDUAL_NO_HALTS` check
report unavailable and fail closed (a documented, deliberate limitation — closing it would need a
future owner-authorized migration granting `catalyst_review` that one `SELECT`, which this
package does not do); the audit-chain head instead comes from an optional `--audit-export` file
produced separately by the existing, already-owner-only `catalyst-lab export` command, verified
with `catalyst_lab.audit.verify_events` twice (once for the whole export, once for the prefix
ending at the setup's own admission event), so a tampered or truncated row is caught, never
silently accepted.

`tests/test_acceptance_evidence.py` (28 tests) drives a full lifecycle through `ManagedExecution`
directly (reusing `tests/test_managed_execution.py` and `tests/test_position_monitor.py::opened`)
plus a few plain `ManagedRuntime` event-writer calls (`_event`/`_append_trade`/`_consume_trade`,
no simulated WebSocket loop) so genuine subscription and print evidence exists, then runs the
collector and the script against it over a real `catalyst_review` connection: every available
section present and every check but the role-limited one passes, output is byte-identical across
two independent collections and across two script runs (including file mode 0700/0600), a
tampered or gapped audit export fails its chain check, a still-open lifecycle reports the
zero-residual checks failed, a missing fee is an explicit unknown, and no credential-shaped
string or account identifier appears anywhere in the output. `scripts/prove_managed_wiring.py
--evidence DIR` now also runs the collector against every closed lifecycle the fixture proof
produces (one `US_STOCKS`, one `CRYPTO`); running it live surfaced a real bug the synthetic
fixture had not (a fully-closed crypto position's `BROKER_POSITION` quantity nets to `"0.0000"`,
not the literal string `"0"`, so the flat check was comparing by text instead of value — fixed to
compare numerically, with a regression test for the analogous multi-fill quantity-matching case).
Full suite: **1,725 passed** (446 s), Ruff clean.

The real run is the owner's: the manifest this tool produces from the owner's first supervised
managed trade (0.10, still pending organic selection or an `ENGINEERING_TEST` enrollment) is the
actual G3 evidence: this package supplies and rehearses the tool, not that trade.

Merge: the branch head 939970b (off-host alerts) was merged into this package with two docs conflicts kept both ways (the appended runbook sections and the September 24 PHASES entries); full suite after the merge **1,752 passed** (19:50 ET), Ruff clean.

## September 24 — operator engineering-test enrollment and the management-review switch (plan 0.10, R6 option B; schema 17)

Fixture and disposable-PostgreSQL evidence only: the fake paper venue, fake Jev transports and
LAB_FIXTURE rows. There was no broker, provider, service or owner-ledger contact, no owner
ledger was migrated, and nothing was enrolled for real.

**Enrollment.** The owner enrolls one `ENGINEERING_TEST` managed crypto setup with
`python -m catalyst_lab.managed_engineering enroll --config … --symbol … --entry-trigger …
--max-entry … --stop … --target … --expires-minutes N --reason "…"`; `status --config …` lists
enrollments. The tool runs over the `catalyst_operator` login. Its SECURITY DEFINER function
writes one audited `MANAGED_ENGINEERING_ENROLLED` event and one `RESEARCH_SELECTED` packet under
`MANAGED_ENGINEERING_ENROLLMENT_V1` (`purpose: ENGINEERING_TEST`, `execution_scope:
PAPER_ONLY`, no Jev receipt, bound to the enrollment's sequence). The operator login was chosen
over `catalyst_risk` because enrollment increases risk, like a halt release (015), and
`catalyst_risk` is the running app's own role. A guard trigger refuses the enrollment kinds from
every other session.

**Migration 017 (DDL only, no audited row).**

- `lab.managed_setups.receipt_id` loses `NOT NULL`, with a CHECK that allows a NULL receipt for
  this policy and purpose on crypto only. No column is added; the migration test shows every
  historical row's `to_jsonb` and audit match unchanged.
- A unique index allows one setup per enrollment, and a guard trigger binds a receipt-free
  setup to its enrollment.
- `lab.managed_review_failure` is replaced. The engineering branch goes to
  `lab.managed_engineering_failure`, which checks the audited enrollment and selection, the same
  symbol, levels, cycle and expiry, no receipt, not expired, and no other active engineering
  setup. Every other packet runs migration 014's body statement for statement (a test compares
  the stored source with 014's text).
- `SCHEMA_VERSION` is 17. The 13 → 17 owner rehearsal still appends exactly three events, all
  from 016; the runbook's 0.8 target is now 17.

**Admission and runtime.** `ManagedExecution.admit` skips receipt verification only for that
policy, and there keys idempotency and the final crypto-refusal record on the enrollment
sequence. A test pins that reviewed packets still verify their receipts exactly as before. The
runtime's selection query no longer offers an admitted enrollment packet; a test shows three
further ticks add no event. The four permanent engineering refusal codes are listed.
Classification, the active-symbol attempt, the `JEV_MANAGED_RISK_V2` row and randomized arm, the
price grid, the stream trigger, the one-use five-second authorizations, protection and mechanical
exits are the normal paths.

**Staging switch.** The switch was added at the coordinator's request.
`MANAGED_MANAGEMENT_REVIEWS` is required, with no default, and must be exactly `ENABLED` or
`DISABLED`. It is in `managed_ops.ENV_NAMES`, and the deploy example sets it to `DISABLED`.

- Under `DISABLED`, `PositionMonitor.review` writes `POSITION_REVIEW_SKIPPED
  {MANAGEMENT_REVIEWS_DISABLED}` once per lifecycle and returns. The monitor loop fetches no bars
  for such a position, and status reports `management_review_enabled: false`.
- An `ENGINEERING_TEST` setup is never reviewed in either setting (`POSITION_REVIEW_SKIPPED
  {ENGINEERING_TEST}`), so Jev never receives the enrollment.

**Reporting.** Setup, position, result, measurement and execution-quality payloads carry
`engineering`. `managed_daily_rollups` excludes engineering setups from every item and reports
them only as `engineering_count`, and the research funnel never counts them. Account risk,
reconciliation, halts and protection include them; a test shows an open engineering position
capacity-deferring a same-theme entry under V2.

Every choice made where the plan left detail open is listed in `docs/ENGINEERING-ACCEPTANCE.md`
(managed section). `docs/OPERATIONS-RUNBOOK.md` gains "First supervised managed trade (plan
0.10)".

**Tests.** New: `tests/test_managed_engineering.py`, 56 tests:

- enrollment writes two audited rows and no Jev row;
- every app role, and the owner's own login, is refused; the risk role cannot forge enrollment
  events, engineering packets or receipt-free setups;
- 12 database and 14 CLI input refusals;
- halts, an active symbol, a second active enrollment and a reused signal are refused;
- seven forgery classes are refused by the review SQL itself;
- the byte-for-byte 014 body and receipt verification unchanged;
- the full `ManagedVenue` lifecycle: enroll, admit with policy and arm, touch trigger, entry
  fill, stop-limit protection, target exit, CLOSED, zero residual, a clean reconciliation, one
  claim per broker mutation, then a new enrollment accepted;
- reporting exclusion over the service routes;
- no re-offer by the runtime;
- both switch states, the refusal of missing or invalid values and the configuration hash;
- a populated schema-16 ledger migrated to 17 with every audited row, halt, head and count
  unchanged, then an enrollment after an audited halt release;
- the CLI never printing a credential;
- off-grid levels refused at admission (final) and then at enrollment.

Updated: `test_owner_migration_rehearsal.py` and `test_operator_controls.py` (schema 17 pins), and
the `test_position_monitor.py` and `test_managed_runtime.py` helpers (they now name the switch).

Validation: full suite **1,754 passed** (56 new; 19:49–19:55 ET on the final code), Ruff clean.
Two earlier full runs each had one failure:

- the repository hygiene scan caught a credential-shaped fixture literal in the new test; it is
  now assembled at run time;
- under three concurrent agent suites, one statement timeout occurred in the untouched
  review-worker path, which passes alone (42 of 42).

**Not covered.**

- The real 0.10 run.
- `scripts/managed_acceptance_evidence.py`.
- Broker execution of `managed_ops operator flatten-all`. It records an audited request but
  reports `broker_action: NONE_UNTIL_ACCOUNT_SAFETY_WIRING`. The scheduled follow-up runs it
  through `managed_account_safety`'s daily-halt cancel-and-flatten path, with one five-second
  authorization per cancel and close, consuming pending `lab.operator_flatten_requests`. Until
  then the runbook's abort is a manual Alpaca Paper close followed by an audited halt release.
- Preflight validation of the switch value (startup refuses it).
- AGENTS.md still says "code 16".

Merge: the branch head b846e26 (off-host alerts, acceptance evidence) was merged into this package with two docs conflicts kept both ways (runbook sections, PHASES entries); the coordinator added the `MANAGED_ENGINEERING_ENROLLMENT_V1` entry to CONTRACT-RESOLUTIONS.md and moved AGENTS.md to code 17. Full suite after the merge **1,808 passed** (20:03 ET), Ruff clean.

## September 24 — approval rule B1 in shadow, offline replay and owner activation (plan 1.7 lean form, schema 17)

Fixture and disposable-PostgreSQL evidence only (a scripted mock Jev transport and the mock paper
venue); no provider, broker, service or owner-ledger contact, and no real Jev call. The
coordinator chose the lean form of 1.7: rule B1 computed in shadow from the SKEPTIC receipts the
selection Jev already produces, an offline replay, and an activation switch the owner flips after
the replay. Option C (a second, context-aware veto request) and V3 `rationale_support` were not
built. The named version, rule text, vetoes, floor and provenance are in CONTRACT-RESOLUTIONS.md
(`MUSE_JEV_RESEARCH_SELECTION_B1_V1`); the owner steps are in MUSE-JEV-MULTIMARKET.md. V2 stays
the default and unchanged.

Verified first: V2 selects only when the overall verdict is APPROVE and the three components pass
(`research_cycle.selection_disposition`; SQL `013:165-169`, reached through 014). B1 keeps V2's
component tests exactly (`already_priced` LOW or MEDIUM) and drops only the verdict, which is
recorded as `dissent`.

- **Rule** (`research_selection_b1.py`, pure): `b1_disposition` on a verified receipt; an
  Insufficient or tied component is `UNRESOLVED_EVIDENCE`, a failing label
  `COMPONENTS_NOT_PASSED`, both NEEDS_REVIEW with the existing evidence tasks; the verdict never
  produces REJECTED. `selection_rule_from_env` reads `MANAGED_SELECTION_RULE` (V2 by default, or
  B1) and the floor `MANAGED_SELECTION_QUALITY_FLOOR` (`WEAK`, `ADEQUATE` or `STRONG`), required
  with B1 and refused with V2; any other value refuses startup.
- **QUALITY_V2** (`research_ranking.py`): V1's three score questions verbatim plus a
  `quality_category` Choice. The floor is a category check; confidence is never read. An
  insufficient or tied category is still recorded (RANKED, category null) and meets no floor.
- **Cycle** (`research_cycle.py`): new cycles carry the configured rule (a B1 `RESEARCH_STARTED`
  also stores a `selection_rule` block naming its floor and the activation event) and every cycle
  is evaluated under the rule it started with. Every recorded SKEPTIC receipt chain appends one
  `RESEARCH_SHADOW_DISPOSITION` in the decision's transaction, keyed by its final receipt and by
  `research_cycle_id` (outside the cycle feed); publication and admission never read it. Active
  B1: every approval gets a QUALITY_V2 judgment and items at or above the floor fill the slots
  left (score order above capacity). V2 cycle bodies, decisions and packets are as before.
- **Runtime** (`managed_runtime.py`): startup appends `RESEARCH_SELECTION_RULE_ACTIVATED` (rule,
  floor, runtime) when B1 is configured; a failed write latches the audit cause and leaves B1
  unactivated without stopping protection. The rule is part of the configuration hash.
  `SELECTION_RULE_NOT_ACTIVATED`, `B1_COMPONENTS_REQUIRED` and `QUALITY_FLOOR_NOT_MET` are
  permanent admission refusals.
- **Migration `017_selection_b1.sql`** (DDL only, schema 17): the previous
  `lab.managed_review_failure` is renamed `managed_review_failure_before_b1` and still judges
  every non-B1 packet; the new B1 branch reuses every 013 binding check except V2's answer
  conjunction and adds the activation, dissent, component, QUALITY_V2 and floor bindings; three
  evidence-free `lab.selection_replay_*` views (no `request_json`) readable by `catalyst_review`
  only. No audited row, table or column: the owner rehearsal still moves the audit head by exactly
  the three 016 policy rows.
- **Replay** (`scripts/replay_selection_policy.py`, read-only): `--database-url` (refuses
  `catalyst_app`, `catalyst_risk`, `lab_owner` and every role but `catalyst_review`; read-only UTC
  session), `--export PATH` (`CATALYST_SELECTION_REPLAY_EXPORT_V1`), `--dump-fixture PATH` (a
  disposable LAB_FIXTURE ledger) and `--print-export-sql` (the same query over the base tables
  for archived ledgers, run by the owner as
  `PGOPTIONS='-c default_transaction_read_only=on' psql -qAtX`; checked to reproduce the view
  export exactly and to refuse writes). Output: codes, identifiers and counts only.

Evidence: `tests/test_selection_b1.py` (193 tests): the full 144-case truth table of B1 against an
independently written V2 oracle, a tie on each question, invalid, missing and failed reviews;
configuration and startup (no floor refused, wiring and hash, activation at startup, a failed
activation write); shadow events once per receipt chain (a 529 retry coalesced), outside the cycle
feed and never selected or admitted; V2 bodies, SQL and privileges unchanged and the renamed
function byte-identical to its migration; active B1 selecting on components with REJECT and
NEEDS_REVIEW dissent while the floor excludes WEAK and insufficient categories; a genuine B1
packet admitted through `ManagedExecution.admit` with its entry authorized; 13 forged packets
refused with their codes (failing or tied component; missing, V1, inflated or foreign QUALITY;
changed score; lowered floor; rewritten dissent; claimed as V2) and tampered SKEPTIC and QUALITY
receipt bytes refused; activation required and recorded once per runtime; a cycle keeping its
rule across a switch in both directions; the replay's receipt checks and its skipping of
requests that name no research item; the replay of a fixture ledger, of its export and
through the CLI giving identical results that reproduce every recorded decision and shadow
disposition; role refusals and view grants; the base-table export query; the fixture dump; and a
populated schema-16 ledger migrated to 17 with the same audit head, every audited row verifying
and its V2 selection still admissible. On the fixture ledger (9 V2-cycle and 5 B1-cycle items)
V2 would select 3, B1 8 without a floor and 3/2/1 with a WEAK/ADEQUATE/STRONG floor, 5 of them
only under B1. After merging the product-plan head 939970b (plan 4.2): full suite
**1,917 passed** (19:56 ET), Ruff clean.

Deviations and open items:

- `already_priced` passes on LOW or MEDIUM, as in V2, not LOW only as the brief listed: the plan
  defines B1 as "the three components decide", and its 09-20 replay ("selects at most ONDO and
  SUI") needs MEDIUM, which both answered. LOW only would be one constant and one SQL line, as a
  new version.
- The replay reads through new views as `catalyst_review`: before 017 neither it nor
  `catalyst_reporting` could read receipts, and `catalyst_reporting` (the public-dashboard role)
  gets nothing. Archived ledgers without the views are replayed from the owner's own read-only
  export.
- The admission function delegates to the previous one instead of copying its body, so a branch
  added by another migration (for example the engineering enrollment) survives unchanged
  whichever lands first.
- Shadow events are keyed by `research_cycle_id`, not `cycle_id`, to keep them out of the cycle's
  working set and agent feed (an existing test pins that feed); the global feed carries them.
- Shadow cycles make no QUALITY_V2 call, so the replay's floor totals over the retained real
  ledgers will be empty until B1 runs active. The replay does not apply the per-cycle limit,
  expiry, eligibility, reward/risk or risk limits, and does not verify the audit chain.
- The launcher's private-config allowlist (`managed_ops.ENV_NAMES`, outside this package) must
  add both keys before they can be set through it; until then a launched runtime stays on V2. The
  runbook and AGENTS.md still name schema 16.
- Switching back to V2 affects new cycles only; in-flight B1 cycles finish under B1 within their
  report validity (the operator pause stops entries at once).
- The real replay of the retained research ledgers, and any real shadow cycles before
  activation, are pending the owner.

Merge: the branch head a17af27 (engineering enrollment, schema 17) was merged into this package; the migration was renumbered `018_selection_b1.sql` (schema 18: it renames the found `managed_review_failure`, now the 017 function with the engineering branch, to `managed_review_failure_before_b1` and delegates every non-B1 packet to it), the enrollment byte-for-byte test reads that renamed function, both populated-ledger migration tests run through 018, `MANAGED_SELECTION_RULE` and `MANAGED_SELECTION_QUALITY_FLOOR` were added to the launcher's optional environment names, and AGENTS.md and the runbook moved to schema 18 (the 0.8 window still appends exactly the three 016 policy events: 017 and 018 are DDL-only). Full suite after the merge: **1,999 passed** with the two migration tests then fixed for the renumbering and re-run green (268 in their four files), Ruff clean.

## September 24 — plan execution status at 20:30 ET (wave 4 merged, lean path chosen)

The owner chose the lean path ("lean, go", ~17:50 ET): the full-path-only items (2.4 symbol
halts, 3.4 option generator V2, 3.6 exit authority, 4.3 operator HTTP, 4.5 health semantics,
4.10 refactors, 4.11 dashboard disclosure) wait until a real week of paper trading justifies
them. Wave 4 then merged four packages built from ae8fef0: operator `ENGINEERING_TEST`
enrollment with the `MANAGED_MANAGEMENT_REVIEWS` staging switch (migration 017), selection
rule B1 in shadow with the offline replay and owner activation (migration 018), the read-only
acceptance-evidence tool for the first trade, and off-host alerts. Branch
`work/2026-09-24-product-plan`, head 0dcf8d4, schema 18; confirming full suite on that head
**2,001 passed** (20:14–20:20 ET), Ruff clean; nothing pushed; fixture and disposable-PostgreSQL
evidence only throughout. Known gaps carried into the next step: `managed_ops operator
flatten-all` only records its request (execution through the daily-halt path is being wired
as migration 019, together with `catalyst_review` read grants on `lab.execution_halts` and
`lab.trade_events` and a 1% fee bound on the crypto REST-position tolerance); the real B1 replay
over the owner's research ledgers, the G4 real-provider dossier proof, ruling R2 and the 0.8,
0.9 and 0.10 owner actions remain the owner's.

## September 24 — operator flatten execution, review read grants, crypto fee tolerance (plan 0.6/4.3 follow-up; schema 19)

Fixture and disposable-PostgreSQL evidence only: the fake paper venue (`ManagedVenue`,
`CountingVenue`), the account-safety test doubles and LAB_FIXTURE rows. There was no broker,
provider, service or owner-ledger contact, no owner ledger was migrated and no real flatten ran.

**Why.** Migration 015's `managed_ops operator flatten-all` only recorded a request and
reported `broker_action: NONE_UNTIL_ACCOUNT_SAFETY_WIRING`; nothing consumed it, so the plan 0.10
abort rule ("protection latched more than 10 seconds with a position open → flatten") was a
manual close in the Alpaca Paper dashboard.

**Migration `019_operator_flatten.sql` (DDL only, schema 19).**

- `lab.operator_flatten_completions`: append-only and audited like 013/015 (audit, immutability
  and truncate triggers). Columns: request sequence, runtime, start and finish, cancels and
  closes attempted and acknowledged, residual codes and outcome `COMPLETED|PARTIAL|FAILED`.
  CHECKs tie `COMPLETED` to an empty residual and keep codes UPPER_SNAKE; there is one
  `COMPLETED` per request, and a guard lets only `catalyst_risk` insert, never after `COMPLETED`.
- `lab.pending_operator_flatten_requests`: requests without a `COMPLETED` completion. The 015
  table, function and guard are unchanged, so a pending request keeps its 015 row exactly.
- 015's `lab.halt_release_blockers` is renamed `halt_release_blockers_before_019` and delegated
  to; the wrapper adds `OPERATOR_FLATTEN_PENDING` for an unreleased `OPERATOR_FLATTEN` halt while
  any request is pending.
- `lab.operator_list_flattens(include_completed)` for the operator login, which still has no
  table grant; `INSERT` on completions for `catalyst_risk` and `SELECT` for `catalyst_app`;
  `EXECUTE` on `lab.unresolved_authorization_claims()` for `catalyst_risk`.
- `GRANT SELECT ON lab.execution_halts` and `ON lab.trade_events TO catalyst_review`, read-only,
  for the acceptance-evidence tool (the reason is in the migration).
- The 13 → 19 owner rehearsal still appends exactly the three 016 events; the runbook's 0.8
  target is 19.

**Execution.** Every account-safety tick reads the pending view first. While a request is
pending it runs the daily halt's own sequence and adds no mutation path:

- It records an `OPERATOR_FLATTEN` halt through the existing halt writer.
- It invalidates WATCHING setups and gives every working managed setup that is not already
  exiting `exit_requested: OPERATOR_FLATTEN`. Legacy exposure gets durable exit requests (the V1
  exit-reason allowlist became a class attribute so the coordinator can add its reason).
- The protective controllers then cancel first and close after the cancels, each mutation with
  its own exact one-use five-second authorization.
- It evaluates what is left. Once nothing is (confirmed on a fresh broker read) every pending
  request gets one `OPERATOR_FLATTEN_EXECUTED` event and one `COMPLETED` row.
- A refused or unknown cancel or close is written at once as `PARTIAL`; exposure only the
  operator or the broker can clear (halted protection, positions or orders no ledger owns) once
  nothing is in flight; a failed pass as `FAILED` with its code. The request stays pending, the
  next tick carries on, and a lasting condition is written once per runtime.
- A refused crypto close gets one new state revision so its next authorization has a fresh
  client order ID; an unknown close is looked up, never resent.
- Failures still reach only the self-clearing `REST_DEGRADED` / `ACCOUNT_SAFETY_TICK_FAILED`
  latches.

Halt semantics chosen: the kind is `OPERATOR_FLATTEN`, and 015's reconciliation release rule
applies (`operator release-halt` after a clean reconciliation recorded after the halt, with no
unresolved claim), never while a flatten request is pending. `OPERATOR_PAUSE` semantics (release
by `resume`, which takes no evidence) were not reused, so entries resume after a flatten only on
a clean reconciliation; `resume` still releases pauses only. A second request while one is
pending is coalesced: it is recorded, never refused, served by the same pass and given its own
rows.

**Operator surface.** `flatten-all` now reports `broker_action: PENDING_ACCOUNT_SAFETY_TICK` and
the new `request_seq`. The new `operator status` action and `operator list-halts` list the
requests with their latest outcome. Runtime status and the status route carry
`operator_flatten`. The watchdog raises `FLATTEN_PENDING` once the oldest pending request is
older than 60 seconds; a future or unreadable time fails closed.

**Acceptance evidence.** Under `catalyst_review` the `halts` section and `ZERO_RESIDUAL_NO_HALTS`
are now real, and the fixture run exits 0. The halts privilege probe needed no change. The audit
section needed a minimal one: without `--audit-export` the collector now reads `lab.trade_events`
itself, in the export's exact shape, when the probe allows it, and reports `present`, `source`
and, when neither is available, `reason`. A supplied export still takes precedence.

**Crypto REST tolerance.** `ManagedExecution._rest_positions` accepted any broker shortfall
below the whole bought quantity as an in-kind fee. It now accepts a shortfall only on crypto,
and only up to 1% of the quantity bought; anything larger, and any excess, stays unexplained.

**Tests.** New `tests/test_operator_flatten.py`, 15 tests:

- no request, no action;
- the CLI-to-`COMPLETED` path for a crypto and a stock position plus a WATCHING setup through
  the real runtime tick: 3 cancels and 2 closes, one claim each, TTL at most 5 s, every DELETE
  before its symbol's close, nothing more afterwards;
- a refused close: `PARTIAL`, a retry with a fresh ID, then `COMPLETED` with no latch;
- an unknown close: looked up, never resent;
- a 429 storm: `FAILED` rows, a self-clearing `REST_DEGRADED` latch, then `COMPLETED`;
- unowned exposure: reported, never touched, then completed;
- crypto dust: `PARTIAL`, still pending;
- coalescing, with one halt and one close;
- the release refused while pending, `resume` not releasing, entries blocked until
  `release-halt`;
- `status` and `list-halts`;
- the watchdog boundaries at 59/60/61 s, plus the status route;
- append-only rows and role refusals;
- a populated schema-18 ledger migrated to 19 with the same head and count, every audited row
  verifying and the pending 015 request unchanged.

Also: `tests/test_acceptance_evidence.py` +3 (direct chain read, an active halt, the fail-closed
fallback without the grants), `tests/test_fill_backfill.py` +5 boundary cases (0 and exactly 1%
explained; 1% plus one increment, an excess and a stock shortfall unexplained). Schema pins moved
to 19 in the owner rehearsal and operator-controls tests (whose view-grant check now includes
`catalyst_review`), and the B1 and engineering migration tests now apply 019 too. `ManagedVenue`
gained a `reject_market_sells` knob.

**Deviations and open items.**

- Release goes through `release-halt`, not `resume` (above); `operator status` is new, because
  no such action existed.
- The crypto re-arm is flatten-scoped. A crypto close refused under the daily halt or a time
  exit is still never retried by the controller: the same revision yields the same client order
  ID, which an approved POST can never reuse. This is existing behaviour, recorded as a finding.
- Exposure no ledger owns, and crypto dust below the broker minimum, keep the request pending
  (`PARTIAL`, `FLATTEN_PENDING`) until closed outside the app. The known dust re-latch from the
  015 notes still applies.
- A request recorded before 019 is executed by the first tick after the upgrade: on a flat
  account, `COMPLETED` at once plus an `OPERATOR_FLATTEN` halt to release. The owner ledger is at
  schema 13 and has none.
- A close the broker keeps refusing is retried every tick, as a stock close already is; no
  backoff was added.
- Real broker behaviour of the flatten (acceptance of the cancels and closes, fill timing) is
  unproven.

Validation: full suite **2,024 passed** (23 new; 21:14–21:20 ET, 344 s), Ruff clean.

Merge: the branch head cad8da4 (wave 4 status entry) was merged into this package with one docs conflict kept both ways (the September 24 PHASES entries); full suite after the merge **2,024 passed** (21:28 ET), Ruff clean. Open defect recorded for the next safety package: outside a flatten, a broker-refused crypto close is not retried (the client order id follows the state revision); daily-halt, time and target exits still carry it.

## September 24 — supervised research-agent session tool (fixture evidence only)

Fixture and disposable-PostgreSQL evidence only: the scripted mock Jev transport and the test
fixtures' paper venue. There was no broker, provider, service or owner-ledger contact. The
`--jev typesafe` mode was not run by the implementation: it spends provider credits, and only
the coordinator or the owner runs it.

**What.** `scripts/agent_research_session.py` runs one supervised session. The research side is
the real product path; the venue is simulated.

- `start` creates a new `/tmp/catalyst-session-*` cluster; any other path or an existing
  directory is refused. It writes four session tokens to mode-0600 files. It seeds the owner
  crypto buckets under `MANAGED_SERVER_CLASSIFICATIONS_V2` (21 USD pairs, `unlisted: REFUSE`)
  through `initialize_managed_classifications`. It then builds `ManagedExecution` with
  `JEV_MANAGED_RISK_V2`, a `JevReviewer`, `ResearchCycle` with `CyclePolicy(10, 10, 15, 60, 30)`,
  the V3 position monitor and `create_managed_app` with a `fable` agent token. The app is
  served by uvicorn on 127.0.0.1; commands reach the session through a mode-0600 control socket.
- Operator commands are `tick`, `execute [--simulate-prints]`, `export`, `status` and `stop`.
  The agent's `submit`, `answer` and `unavailable` helpers speak HTTP with the `fable` token.
- The fixture Jev is a scripted mock transport. It answers SKEPTIC, QUALITY, QUALITY_V2 and
  management questions per symbol (defaults `APPROVE/NO/NO/LOW`, `ADEQUATE`, `HOLD`).
- The Jev cap (`--max-jev-calls`, default 40) is a counting transport wrapper: it refuses the
  (N+1)th provider call with `JEV_CALL_CAP_REACHED` and never sends it. A reviewer pre-check
  turns a spent cap into a `NEEDS_REVIEW` / `JEV_CALL_CAP_REACHED` decision without a receipt.
- Simulation labels, following the coordinator's venue refinement:
  - the venue answers every crypto pair with `SESSION_SIMULATION` metadata: 1e-8 price, lot and
    minimum-order grids, overridden by `--sim-price-increment`;
  - `CRYPTO_ASSET_METADATA` rows name `SESSION_SIMULATION` as source (a `SessionExecution`
    subclass that keeps the engine's event and key);
  - trigger prints, quotes and management-review bars carry provider and feed
    `SESSION_SIMULATION`, and venue orders and fill events carry `simulation`;
  - `AGENT_RESEARCH_SESSION_STARTED` records the session configuration in the ledger.
- `export` writes `events.json` with `verify_events`, `decisions.json`, the research funnel
  through the analytics route (status token), the selection replay as `catalyst_review`, and one
  acceptance-evidence manifest per closed setup via `scripts/managed_acceptance_evidence.py`.

**Choices.**

- The reviewer's reliability policy mirrors the Gate 1 numbers: a 10 s deadline, 3 attempts,
  0.25 s backoff and a 3-failure, 30 s breaker. Its deadline equals the cycle's review deadline,
  so the cycle policy needed no change.
- `tick` is an explicit command with no background loop, so every Jev call follows a command.
- Admission refusals are audited with the runtime's own events: `RUNTIME_ADMISSION_REFUSED`,
  plus `RESEARCH_ADMISSION_DECLINED` for permanent codes, with the session id as runtime id.
  `execute` therefore never retries a permanent refusal.

**Tests.** New `tests/test_agent_research_session.py`, 7 tests:

- the root rules, and the CLI refusing before it creates anything;
- configuration and fixture-script validation;
- the capped transport: the (N+1)th call is never sent, and per-call transports are closed;
- in-process, a spent cap: a clear decision, while protection and exits still close two
  positions with management reviews DISABLED;
- in-process, B1: its activation, the verdict recorded as dissent, a forced `JEV_MANAGED` arm, a
  `TIGHTEN_AND_EXTEND` management review applied, a target exit at the extended target, zero
  residual and the full export;
- end to end through the real CLI (loopback uvicorn on an ephemeral port): `submit` with a
  missing `run_id`; `tick` with V2 and the B1 shadow; an evidence task answered over HTTP;
  `unavailable`; `execute` closing both selections with zero residual; `export`; `stop`; and no
  token in any output or in `session.json`;
- SIGINT exports and stops the cluster.

**Deviations and open items.**

- The managed API has no "unavailable" route for evidence tasks, only claim and evidence.
  `unavailable` claims the task and records the declaration in the session directory; the task
  stays open until it expires, and the declaration is exported.
- The review window is 30 minutes from the report's receipt (`review_validity_seconds` 1800,
  the value in `deploy/private-paper.example.json`; the first draft used the 60 s default).
  Evidence answers are reviewed inside the same window, as in production.
- A 1e-8 grid refuses 9-decimal levels (for example `5.775e-06`). Pass
  `--sim-price-increment 0.000000001` for such pairs.
- No stock classification is seeded (the owner buckets are crypto), so stock selections are
  refused at admission; only crypto lifecycles are simulated.
- Every acceptance-evidence manifest exits 2: `SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER` cannot
  pass without streams, and none is fabricated.
- `/results` still reports `execution_source: ALPACA_PAPER`, and fills keep source
  `ALPACA_PAPER`; both are hardcoded in the app and engine and were not changed. The session's
  own labels and export manifests carry `SESSION_SIMULATION`.
- `JEV_MANAGED_RISK_V2`'s randomized 30% `FIXED_EXIT` arm gets no management review, by design.
  The end-to-end test accepts either arm; the in-process test forces `JEV_MANAGED`.

Validation: full suite **2,031 passed** (7 new; 373 s), Ruff clean.

## September 20 — 30 actual crypto proposals and Jev criteria audit

[30-candidate batch and criteria findings](MUSE-JEV-30-CRYPTO-2026-09-20.md): 42 public
crypto pairs screened, 34 with primary-source coverage, 30 submitted over authenticated HTTP,
and 30 actual pinned Jev calls. Recorded baseline: 23 REJECTED, 7 NEEDS_REVIEW, zero selected.
Three provider responses failed probability-sum validation; 27 were valid. No executable setups,
risk decisions, orders or fills. A separate schema14 research-only ledger verifies through 360
audit events and was stopped after export. The account executor remains offline/unactivated.

The audit found technical observations have no explicit first-Jev selection judgment, while the
policy requires a fresh supported catalyst and independently combines a broad verdict. No
selection threshold or question was relaxed. A narrow post-baseline correction restores explicit
REJECT precedence over uncertain component answers, makes receipt recovery consistent and binds
uncertain answer data to retained receipt bytes. Existing events are unchanged. Offline composition
replay changes only three labels (26 REJECTED, 4 NEEDS_REVIEW), with zero approvals or provider calls.

Validation after both fixes: **1,079 tests passed**, Ruff and diff checks clean. The additional
Muse test-only fix replaces an expired fixed deadline with its actual service clock.

## September 20 — actual first-Jev crypto research flow

[Real Muse-to-first-Jev session](MUSE-FIRST-JEV-2026-09-20.md):33 crypto pairs researched;
four actual proposals submitted through the current HTTP intake, seven real `jev-1.13.0`
calls, and three source-backed evidence revisions. SOL/AVAX ended REJECTED; LINK/ETH remain
NEEDS_REVIEW because overall approval conflicted with stale/uncertain component answers.
One initial ETH receipt failed probability-sum validation. Zero selected setups, orders or fills;
139 audit events independently verified. The old worker/intake were offline, so this used a new
schema14 research-only ledger and no account executor. The temporary session was stopped after
export. Actual trigger/execution/second-Jev acceptance remains outstanding.

## September 20 — paper wiring and private operations

[PAPER-WIRING-2026-09-20.md](PAPER-WIRING-2026-09-20.md) is the current implementation handoff.
Schema **14** is implemented and exercised in disposable PostgreSQL databases. The isolated
20-contender proof retained **40 research receipts, 10 selections, two management receipts,
four fills, two closed lifecycles, nine risk claims and 420 verified audit events**. Jev,
broker fills and market data were mocked; actual provider calls/orders were zero.
**Final combined suite: 1,076 passed** with two existing dependency deprecation warnings;
Ruff and diff checks passed. Targeted checks are included in that total.

Implemented: external durable Muse scheduling/delivery, technical/economic evidence validation,
fresh revision review with an explicit validity window, full-context position review and
near-target scheduling, account-wide mixed legacy/managed daily exits, durable crypto recovery
halts, account executor ownership, opt-in NY-day crypto deadlines and liquidity/cost gates,
append-only fee evidence and honest unknown net P&L, private configuration/preflight,
LaunchAgent rendering, a redacted watchdog, and audit checkpoint verification/copy recovery.
The frozen US trigger/risk rules and historical setup policy versions remain preserved.

The existing credential-holding worker8768 and intake8770 were not restarted, migrated or
reconfigured by this work. Proposed combined port8780 is unactivated. Secure injection,
independent US classifications (the example uses `[]`), provider authentication, controlled
handover, and actual paper fill/management/exit acceptance remain outstanding. The same-account
lease requires a shared ledger and does not fence an old unpatched process using another DB.
There is no paper profitability, live-money, or unattended reliability claim.

## Earlier dated checkpoints

Owner architecture correction (2026-09-19): [external Muse research boundary](MUSE-RESEARCH-BOUNDARY.md).
Current source removes the app discovery endpoint and accepts researched Muse reports directly.
Jev review/evidence revisions feed the existing independent watcher/risk/execution workflow.
The full two-market fixture now starts with an external 20-item report and no app scanner.
The corrected Muse-facing surface runs on port8770, sharing the existing worker queue.
The credential-holding account executor was preserved; no second executor or restart was needed.
Final correction validation: **953 tests passed**, five browser checks passed, lint clean.

Real managed runtime checkpoint: [September 19 local session](REAL-MANAGED-SESSION-2026-09-19.md).
The new worker now authenticates to Alpaca Paper and reconciles continuously. The old
executor was stopped while flat; its ledger remains intact. Real technical screening
covered all 36 eligible USD crypto pairs, with no qualifying setups and therefore no
Jev reviews or orders. 943 tests total and 10 real-dashboard browser checks pass.
Paper-fill/management acceptance remains pending; this is not another Jev-rejected batch.

Latest local milestone (2026-09-19): [MANAGED-WIRING-2026-09-19.md](MANAGED-WIRING-2026-09-19.md).
Schema13 in isolated databases wires the stock/crypto engineering workflow, two Jev roles,
evidence revisions, shared risk, continuous stream processing and protected amendments.
939 backend tests, 12 browser checks and lint pass. The fixture chain verifies through 300 events.
Full fixture cycles and real Jev contract checks are separate evidence. Existing account
runtime remains unchanged; real Alpaca acceptance and external deployment remain pending.
The historical observations below describe their original checkpoints, not current code gaps.

Real-data checks on 2026-09-19: [US research](REAL-WORLD-SESSION-2026-09-19.md)
and [crypto research/observation](CRYPTO-REAL-WORLD-2026-09-19.md) used real Jev calls
and retained all decisions. Crypto recorded 72 live market checks and three NEEDS_REVIEW
outcomes; no crypto order was placed. These observation checks do not complete crypto execution.

Muse follow-up: [research dispositions](CRYPTO-MUSE-FOLLOWUP-2026-09-19.md). Codex
rejected the submitted BTC/ETH ideas and supplied missing prior disclosures for SOL;
the actual new Jev assessment rejected SOL. All outcomes are retained, original
proposals stay expired, and no owner review is pending for this batch. The continued
research ledger verifies through 87 events. Crypto execution remains unimplemented.

| Phase | Status | Delivery / remaining work |
| --- | --- | --- |
| 1 — Lab core | Implemented locally | Schema, DB-enforced audit chain, single/batch candidate intake, preliminary validation, dated polling, restricted Muse routes, real PostgreSQL tests |
| 2 — Watch and trigger | Implemented locally; stream connected | Frozen mechanical trigger, durable observer/checkpoints, quote/trade/bar stream, expiry/invalidation, read-only paper/calendar adapter; no order submission |
| 3 — Execution | Implemented; original transport stays locked | Paper REST/stream, immutable whole-share DAY bracket intents, fill/state processing, 45s reconciliation, startup gate, calendar exits |
| 4 — Risk engine | Real paper partial-fill safety path verified; nominal acceptance pending | Live-equity M-minus-S sizing, atomic reservations, correlation mapping, durable cancel-and-flatten halt, exact-request authorization, uncertain-submit recovery; September 18 test closed safely with FAILED_CLOSED |
| 5 — Measurement | Implemented and running locally; official R pending | Fill-derived gross P&L, snapshot excursions/provenance, derived trades, append-only rollups; owner R denominator still needed |
| 6 — Dashboard | Local browser verified; public hosting pending | Separate read-only app, DB-enforced post-close disclosure, full candidate log, US/Paper defaults and separate manual research |
| 7 — Muse | External intake used; continuous worker implemented locally, unactivated | September 19 external reports reached real Jev; September 20 durable provider/spool/scheduling flow is locally tested. Secure launch, authentication and controlled handover remain pending. |

Separate external evidence: [2026-09-17 Alpaca Paper connectivity check](ALPACA-CONNECTIVITY-2026-09-17.md)
confirmed paper authentication, an active $10,000 account, zero open positions/orders, calendar access,
and IEX quote retrieval. SIP returned 403. Those initial read-only checks did not connect the runtime. The subsequent Phase 2 runtime/stream
verification is recorded in PHASE-2-VALIDATION.md. Phase 3 provider/restart/reconciliation evidence
is recorded in [PHASE-3-VALIDATION.md](PHASE-3-VALIDATION.md). The September 18
[controlled proving run](PROVING-RUN-2026-09-18.md) submitted one real paper bracket, partially filled
5 of 13 shares, then canceled and flattened through PROTECTION_FAILURE. Broker/local exposure
returned to zero; the immutable engineering result is FAILED_CLOSED, not a nominal acceptance pass.

## Latest checkpoint — app-owned research worker

2026-09-19: schema 11 in disposable databases only; **538 tests pass**, lint clean,
**16 private-screen browser checks pass**. Shared Gate 1 breaker/probe state, immutable
queue claims/outcomes, heartbeat, durable operator halt and output cursor are implemented.
An actual process kill/restart preserved an unknown request without a second model vote.
Six new fixed real-Jev evidence-support cases matched expected research dispositions;
all original receipts and earlier unfavorable experiments remain retained.

See [REVIEW-WORKER-LOCAL.md](REVIEW-WORKER-LOCAL.md) for file map, hash checkpoints,
latency, screenshots, limits and remaining policy decisions. Existing runtime DB and
trading processes were untouched. Gate 1 final reviewer approval and deployment proof
remain outstanding. The research worker is not a position monitor or a trading authorizer.

## Gates before later phases

- Integration document received and Phase 1 portions implemented; see API-CONTRACT.md and
  CONTRACT-RESOLUTIONS.md. Public HTTPS hosting and secure Muse connection remain outstanding.
- Specify operational data-quality thresholds and sources for sector/theme and average daily volume.
- Phase 4 choices are frozen: five-second TTL, previous-close equity captured at reconciliation,
  and a TEST- engineering candidate excluded from strategy reporting. See CONTRACT-RESOLUTIONS.md.
  The real September 18 run retained these gates and the approved 300-second engineering lifecycle.
- The first controlled run exercised a real trigger, authorization, partial fill and protective
  flatten. Review its FAILED_CLOSED outcome before any further acceptance work; no repeat is queued.
  See PROVING-RUN-2026-09-18.md. The nominal CONTROLLED_ACCEPTANCE branch remains unproven at Alpaca.
- Import server-owned sector/theme mappings and finish the production admission evidence provider.
  No fixture classifications, liquidity or account values may substitute for production evidence.
  The operator-only engineering path records its limited plumbing-test admission separately.
- Worst-case sizing on M-minus-S and cancel/flatten recovery are implemented and tested. The latest final Part A explicitly moves the 2R candidate check to M; old decisions remain intact. See PHASE-4.1-TRIAGE.md.
- Startup/current-session reconciliation and durable halts are implemented. The audited operator
  release procedure (schema 15, September 24 entry) is fixture-tested; there is still no
  automatic repair or release path.
- Automated local audit exports and offline evidence-copy verification are implemented in the
  September 20 tooling, but scheduling is unactivated. Configure independent off-host checkpoints
  and prove full database backup/restore separately; an audit JSONL copy is not a DB backup.
- Event projection privileges are restricted. Phase 5–6 local implementation/validation: PHASE-5-6.md.
- v4.2 follows V4.2-BUILD.md in order; missing provider access must not be assumed.

## Full-plan acceptance mapping

1. Health shape: implemented; reports paper configuration, REST/stream/quote health and submission mode.
2. Bad candidate rejected with immutable rule/log: implemented and locally tested.
3. Good candidate validates then WATCHING: observer and trigger tests use labeled fixtures; orders remain absent.
   Production admission still requires complete validation evidence after clean startup reconciliation.
4. Trigger → atomic risk → bracket dispatch: implemented and tested through fake paper HTTP with
   real PostgreSQL. One actual Alpaca Paper bracket, partial fill and protective flatten are now
   verified; the nominal controlled exit acceptance remains outstanding.
5. Restart reconciliation: implemented; synthetic mid-trade mismatches and persistent halts tested.
   Actual empty-account restart/reconciliation checked separately; no real mid-trade restart claim.
6. Broker fill rows and candidate state transitions: implemented, locally tested and verified in the
   engineering run. Its report-only gross P&L/R arithmetic is excluded from strategy results.
   Phase 5 now projects broker P&L and observed excursions; official R is pending an owner definition.
7. Verified/research dashboard implemented and browser-checked locally; public HTTPS deployment pending.
8. UPDATE as application role rejected by DB: tested; DELETE/TRUNCATE/DDL also tested.
9. Prohibited endpoint reference absent: source scan test, including docs.

Unit/database tests are local engineering evidence. Broker/stream checks are documented separately;
no trade, profitability, bracket acceptance or public-readiness claim follows from unit tests alone.

Phase 3 details and its explicit lock boundary: [PHASE-3.md](PHASE-3.md).
Phase 4 authorization design and remaining gates: [PHASE-4.md](PHASE-4.md).

## v4.2 checkpoint — 2026-09-19

Steps 0–3 complete: corrective work, verified exact TypeSafe model, pinned official skill and audited
review adapter. Full suite 343 passed; three real synthetic review receipts verified. Schema 8 is
applied to the existing runtime; existing paper execution remains separately gated.
See [JEV-ADAPTER.md](JEV-ADAPTER.md) for that historical checkpoint.

Step 4 now adds schema 9 immutable evidence/context storage, inert entry intents and separate
cohort observations, plus explicit configuration and Railway preparation. Schema 9 is tested only
in disposable databases; the existing runtime was not migrated or populated by this work.
The local Muse-role walkthrough is documented in [STEP-4-REPORT.md](STEP-4-REPORT.md).
Railway deployment and the Muse connection are deferred by the latest owner instruction.
No intent reader, live event/entry integration, Steps 5–7 or unattended operation is enabled.

Local provider follow-up: the owner-authorized .env loader authenticated a real engineering
review against jev-1.13.0. Its exact receipt and 14-event chain verify; final result is
NEEDS_REVIEW and the intent remains inert. A precision bug in judgment projection was fixed
without loosening DB checks; 433 tests pass. See STEP-4-REAL-JEV.md for both provider attempts.

## App-hosted Jev / three-market direction — 2026-09-19

The owner now wants paper execution for US stocks and crypto, India research-only, with Muse
external research and Jev inside the app selecting and managing trades based on news and
movement. This supersedes the prior Step 4-only build scope; it does not enable unbuilt
execution paths. Details and provider/policy gaps: [MUSE-JEV-MULTIMARKET.md](MUSE-JEV-MULTIMARKET.md).
The V1 fixed-exit baseline is preserved. No numeric trailing formula was supplied or invented.

The public dashboard has been simplified: four headline metrics, three clear market tabs,
expandable statistics/audit, full candidate log and detail/export retained. It continues to
show crypto execution as pending and India as research-only by owner decision. No admission,
order, risk, measurement or schema behavior was changed by this UI work.

The next implementation sequence is recorded in [TEST-READINESS-PLAN.md](TEST-READINESS-PLAN.md):
report/selection tests, durable worker, US admission and full paper acceptance, managed
exits, crypto, then deployment/Muse connection. This is a plan, not completed work.

## Research-report milestone — 2026-09-19

The owner approved the local research-selection composition. Migration 010 and the
separate review API now store immutable US-stock/crypto/India reports and return exact
Jev-backed dispositions. Supervised tests reviewed 30 items per market; nine additional
real Jev calls produced nine retained rejections. Full suite 496 passed; no orders.
Raw receipts now survive projection failure. Detail, file map and verified chain heads:
[RESEARCH-REPORT-SELECTION.md](RESEARCH-REPORT-SELECTION.md).

This completes the local engineering report/selection/readback milestone. A durable
worker and full Gate 1 runtime acceptance remain next. No entry/management wiring, main
runtime DB migration, deployment or actual Muse connection occurred. Schema 10 was
installed only in disposable tests. No unattended or full paper-loop readiness claim.

## Broad crypto research observation — 2026-09-19

The owner clarified the research funnel: Muse brings 20–30 contenders; Jev targets
5–10 final research selections without forcing approvals. One supervised pass screened
250 CoinGecko assets, investigated and submitted 20 distinct research packets, and
received 20 real, integrity-verified Jev receipts. Outcomes: 8 REJECTED, 12 NEEDS_REVIEW,
0 SELECTED. Muse retained five explicit evidence follow-ups; these are not approvals.

The isolated research ledger contains 266 verified events after Muse dispositions,
with zero executable candidates, orders, fills and risk decisions. The owner account
remained flat with clean reconciliation. No account database migration, crypto execution,
managed exits, deployment or continuous research process was enabled. Original research
deadlines expired normally. Script lint and both crypto/India-to-US isolation tests pass;
this is not a new full-suite run. Detail and checkpoint:
[CRYPTO-BROAD-RESEARCH-2026-09-19.md](CRYPTO-BROAD-RESEARCH-2026-09-19.md).

## Completed broad research cycle — 2026-09-19

At the owner's request, Muse supplied materially expanded evidence for INJ, UNI, SUI,
ONDO and FIL, including issuer/governance/token-rights sources and independent venue
observations. Five additional real Jev calls produced four REJECTED and one NEEDS_REVIEW
(ONDO). Muse closed all five without selection; all 20 original cases now have final
research dispositions. Total: 25 real assessments across 20 contenders, zero selections
and zero orders. INJ's favorable supporting answers conflicted with its explicit REJECT;
the unchanged policy honored the rejection and retained the disagreement.

The original expired report was not extended. Follow-up assessments are linked,
post-expiry research only. The same isolated ledger verifies at 337 events, head
`ce775a5b44481a538444bc45fd77d403ee4242c88a2de67aa76627f5127ad069`.
Account reconciliation remained clean and flat. Worker/database stopped after export.
Lint and both market-isolation tests passed. Crypto execution/protection is still
unimplemented; this completes a research cycle, not an execution cycle or integration.
See [CRYPTO-CYCLE-COMPLETE-2026-09-19.md](CRYPTO-CYCLE-COMPLETE-2026-09-19.md).

## Jev diagnostic validation — 2026-09-20

Built a 60-case synthetic benchmark covering US/crypto with family-separated development/holdout
sets and a frozen independent AI review. Six ambiguous references remain unscored. One real pinned
Jev call per case produced 59 valid responses and one malformed distribution. Held-out composed
agreement was 22/26 valid agreed cases, or 22/27 including service failure. This is provisional
synthetic evidence agreement, not verified trading edge or validation of the earlier 30 rejections.

The new reason-bearing diagnostic has no execution authority. A separate immutable crypto shadow
ledger evaluates matched accepted/rejected opportunities with explicit costs, sizes, limits,
partial fills, expiry, ambiguous bars and prospective freeze checks. Portfolio allocation,
prospective performance and second-Jev management evaluation remain open. No existing selector
policy was promoted or historical decision changed. The isolated provider chain verifies at 475
events with zero orders/fills/risk decisions; its database is stopped. Owner ledger unchanged.

Detail, exact receipts, reference limitations and corrected metrics:
[JEV-VALIDATION-2026-09-20.md](JEV-VALIDATION-2026-09-20.md).
Validation: full-suite snapshot 1,226 passed; latest focused new-module suite 149 passed after
two additional collector checks (overlapping counts), Ruff clean and diff check clean.
Actual bounded public-feed probe retained 505 messages and stopped; no prospective case or
broker order was created by it.

## Muse guideline binding and US stock preparation — 2026-09-20

Added `MUSE_RESEARCH_GUIDELINES_V1`, documented in [MUSE-GUIDELINES.md](MUSE-GUIDELINES.md)
and injected with its SHA-256 into the external command provider for research, evidence
follow-up and position-news jobs. Tests enforce document/runtime parity and unchanged
request/output schemas. Strategy, selector, risk, broker policy and worker activation
are unchanged.

Acting as Muse, researched 50 US equities with seven benchmarks, primary news for 37
equities, and completed five-minute data for 25 equities plus two benchmarks. The
[dated dossier](MUSE-US-STOCKS-2026-09-20.md) retains 25 research cases and eight priority
watchlist names. Raw observations, source chronology, calculation scripts, contrary
evidence, failed geometry examples and an external manifest are retained. Additional
Alpaca stock requests failed, so the descriptive screen uses labeled public Yahoo
observations; AAPL/NVDA closing prices were cross-checked against retrieved SIP data.
No proxy is represented as broker admission evidence.

This is Sunday preparation for September 21. Zero research-qualified executable proposals,
Jev calls, API submissions or broker orders were produced. Current-session evidence,
independent admission and actual paper lifecycle acceptance remain required. No continuous
research job was scheduled or credential-holding runtime changed. Full suite: 1,232 passed
with two existing dependency deprecation warnings; `ruff check src tests` clean.

## 2026-09-24 (evening ET) — research-agent session: fixture Jev and real Jev, one real report

Evidence: `artifacts/agent-session-2026-09-24/` (README, both columns, research inputs; the two
`events.json` exports are in `~/.local/share/catalyst-retest-lab/evidence/agent-session-2026-09-24/`
with SHA-256 in `evidence-manifest.sha256`). Tool: `scripts/agent_research_session.py` at 20bc37e
(review window 1,800 s, the deploy example's value).

- Agent `fable` submitted an 8-item crypto `AGENT_RESEARCH_REPORT_V2` (exact observed-bar levels,
  reward/risk 2.06–5.43, verbatim official excerpts, claim-level rationale) over the real HTTP
  intake with its own token; all 8 items accepted, dossiers 9,533–10,780 bytes.
- Fixture column (scripted Jev, 14 calls): three scripted objections became evidence tasks; a
  reworded answer without a new source was refused (`MATERIAL_NEW_SOURCE_EVIDENCE_REQUIRED`);
  answers with new sources were re-reviewed and approved; 6 selected, 6 admitted under
  `JEV_MANAGED_RISK_V2` (budget $50 on $10,000, sized on M − S), 6 simulated lifecycles
  WATCHING → ENTRY_PENDING → ORDER_SUBMITTED → OPEN → CLOSED at target, zero residual, clean
  reconciliation; fixed-exit arm took 3 of 6; managed arm: two HOLDs and one TIGHTEN_AND_EXTEND
  whose amendment reached the venue. Audit chain valid (460 events).
- Real-provider column (TypeSafe `jev-1.13.0`, 17 calls, every receipt VALID, 0.44–0.79 s each):
  first round 8 × NEEDS_REVIEW with evidence tasks; the agent answered all eight with new primary
  sources; final: BCH, UNI, AVAX REJECTED, five still NEEDS_REVIEW; 0 selected, nothing admitted.
  `unsupported_inference = YES` at 0.53–0.98 on every pick after revision; best APPROVE probability
  0.31. B1 shadow would have selected AVAX at revision 2 (the third revision flipped it to REJECT).
  Audit chain valid (262 events).
- Findings: (1) the managed API has no evidence-task "unavailable" route, so an agent's
  `NO_NEW_EVIDENCE` is only a local declaration and the task expires; (2) exact bar levels can be
  finer than the venue price grid (AVAX trigger 9.996504995; session grid 1e-8) — intake must
  round or reject against the venue grid; (3) the economic-relationship question is where every
  real pick died; the question set or guidelines should define "supported", or B1 with a quality
  floor is activated — `real-provider/selection-replay.json` is the first replayable data.
- Not proven here: any broker behaviour, streams, fees, or real fills. Fixture and real-provider
  columns are separate and never merged.

## September 25 — question set `SKEPTIC_QUESTIONS_V2` and selection rule B2 (schema 20)

Fixture and disposable-PostgreSQL evidence only: the scripted mock Jev transport and the test
fixtures' paper venue. This package made no provider, broker, service or owner-ledger contact
and no real Jev call. The real-provider evidence for the question set is
`artifacts/jev-rule-experiment-2026-09-25/` (24 real calls, made before this package); the
session that motivated it is `artifacts/agent-session-2026-09-24/`. The named versions, the
full question texts, the passing table and the provenance are in CONTRACT-RESOLUTIONS.md.
Nothing activates B2: V2 stays the default, and V1's question set, V2 and B1 are unchanged.

- **Question set** (`jev_contract.SKEPTIC_V2`, template hash `754d70c8…a27e6722e`): V1's
  `news_stale`, `already_priced` and `verdict` verbatim; `unsupported_inference` replaced by
  `mechanism_contradicted`, `inference_labelled` and `factual_claims_supported` (the
  experiment's follow-up texts exactly), all built with `choice()`.
- **Rule** (`research_selection_b2.py`, pure, B1's shape): the five components decide (NO;
  LOW or MEDIUM; NO; YES; SUPPORTED, each the unique most probable label); the verdict is
  `dissent` with a tie flag and never blocks. `UNRESOLVED_EVIDENCE` for Insufficient or tied
  components, `COMPONENTS_NOT_PASSED` for failing labels, `B2_COMPONENTS_PASSED` otherwise; no
  rationale is `RATIONALE_REQUIRED` with no provider call. Objection codes map to
  `FETCH_PRIOR_DISCLOSURES` (unchanged), `RESOLVE_CONTRADICTION`, `LABEL_INFERENCE`,
  `RECITE_FACTUAL_CLAIMS`, `SUPPLY_SELECTION_RATIONALE`, or V1's `RESOLVE_RESEARCH_OBJECTION`.
- **Configuration**: `SelectionRule` accepts `MUSE_JEV_RESEARCH_SELECTION_B2_V1` with the
  required floor (`MANAGED_SELECTION_RULE`, `MANAGED_SELECTION_QUALITY_FLOOR`); V2 stays the
  default. The activation event also names `SKEPTIC_QUESTIONS_V2` and its template hash.
- **Cycle** (`research_cycle.py`): B2 cycles review with V2, keep the rule they started with,
  record `selection_policy`, `question_set_version`, `dissent`, `reasons` and `shadow:
  NOT_APPLICABLE_QUESTION_SET` (no B1 shadow: V2 answers have no `unsupported_inference`), get a
  QUALITY_V2 judgment per approval and publish at or above the floor through the publication
  shared with B1 (B1's output unchanged). B2 revisions may carry `selection_rationale`; re-cited
  or trimmed claims are material without a new source (`MATERIAL_NEW_EVIDENCE_REQUIRED`
  otherwise); newly cited report bars reach the reviewer exactly as intake sends them; every
  citation must be visible in the revised state. V2 and B1 revisions are unchanged (a rationale
  there is `RATIONALE_REVISION_NOT_APPLICABLE`).
- **Reliability** (`jev_review.py`): a 200 that fails validation (`INVALID_PROVIDER_RESPONSE`)
  gets one further attempt within `max_attempts` and the deadline, each attempt its own receipt,
  the invalid body kept as received; credential echoes are not retried. Under the Gate 1
  runtime the retry holds its own attempt permit and the breaker counts both failures.
- **Migration `020_selection_b2.sql`** (DDL only, schema 20): the dispatcher sends B2 packets to
  `lab.managed_review_failure_b2`, B1 packets to migration 018's function (renamed
  `managed_review_failure_before_b2`, byte for byte) and every other packet to
  `managed_review_failure_before_b1`, exactly as 018 did. B2's branch reuses 013's binding checks
  (`managed_review_failure_b2_bindings`, with a six-answer receipt check in place of V1's), pins
  the V2 hash, and requires an intact B2 activation, a rationale, the five components and 018's
  QUALITY-floor block. The three replay views now include V2 requests (same columns and
  grants). No audited row, table or column: the owner rehearsal still moves the audit head by
  exactly the three 016 rows.
- **Replay** (`scripts/replay_selection_policy.py`): B2 over V2 receipts only; V2 and B1 over V1
  receipts only; otherwise `NOT_APPLICABLE` / `REPLAY_NOT_APPLICABLE_QUESTION_SET`.
  `--dump-fixture` adds a B2 cycle.
- **Session tool**: `--selection-rule B2`, six-label `skeptic_v2` fixture rows, B2 columns and
  requirement instructions in `tick`, B2 blocks and the rationale per revision in
  `decisions.json`, and `selection_rationale` in evidence files.

Evidence: `tests/test_selection_b2.py` (525 tests): the 432 component combinations, each under
six verdicts (tied ones included); every ordered tie of every component, alone and next to a
failing label; invalid, missing and failed reviews; cross-question-set isolation; every
objection code's requirement; configuration and runtime wiring; active B2 selecting with REJECT
and NEEDS_REVIEW dissent and no shadow; a genuine B2 packet admitted with its entry authorized;
16 forged packets refused with their codes; tampered SKEPTIC and QUALITY bytes; receipts of
another question set (V1, an altered same-shape template, a draft version); a missing and a
foreign activation; activation and cycle life in both directions; the invalid-distribution
retry in a cycle; `RATIONALE_REQUIRED` answered by a rationale revision and enforced in SQL;
the rationale-only answer to `RECITE_FACTUAL_CLAIMS` (non-material variants refused, idempotent
retry); rationale validation; V2 and B1 unchanged in code and over HTTP; newly cited bars;
migration 020's functions compared with 013, 017 and 018 byte for byte; the new dispatcher
equal to 018's on every non-B2 packet and variant; a populated schema-19 ledger migrated to 20
with the same audit head, every audited row verifying and every route unchanged; and the
replay. Also 7 retry tests in `tests/test_jev_review.py`, one under the Gate 1 runtime in
`tests/test_review_worker.py` and two in `tests/test_agent_research_session.py`, including the
end-to-end CLI session under B2 (one item admitted; one answered with a rationale-only revision,
re-reviewed, approved and admitted). Four migration tests now also apply 020 and pin schema 20,
and the G4 harness test counts the retried invalid receipts. Full suite: **2,566 passed**
(535 new), Ruff clean.

Deviations and open items:

- No `lab.research_question_sets` row for V2: the migration is DDL-only (no seeded row) and that
  table serves only the 2026-09-19 report flow, which never uses V2. The hash is pinned in code,
  in migration 020's B2 branch and in the docs.
- The replay views were widened in place (`CREATE OR REPLACE VIEW`, same columns and grants)
  instead of adding parallel B2 views, so one export format and one query cover every question
  set; a ledger before schema 20 still replays.
- The dispatcher calls `managed_review_failure_before_b1` directly for non-B1, non-B2 packets,
  as migration 018 did, and routes B1 to the renamed 018 function; its exception handler could
  never fire on that path, so results are identical (tested on every variant).
- Evidence-revision rationales are B2-only; a B2 revision's carried rationale must still cite
  only what the revised state shows. A thesis-only rewording still buys no vote.
- AGENTS.md's schema line still says code 19: it is an agent-instruction file, so the change is
  left to the owner or coordinator (code 20; migrations 014–020 pending on the live ledger).
- `/analytics/research` still reports `rationale_claim_support_rate: null`; it does not read
  `factual_claims_supported` yet.
- B2 has never run against the real provider in a session; the owner's live ledger was not
  migrated (owner-run `ledger-migrate`, target 20).

## 2026-09-25 — Strategy evidence: crypto search, stock earnings study, Jev question test; rule B2 merged

- `artifacts/gap-retest-backtest-2026-09-25/`: the agents' catalyst-retest rule is negative in all
  nine settings on 2024–2026 crypto daily bars (27 of 49 trades stopped out on the retest day).
- `artifacts/strategy-search-2026-09-25/`: 1,024 catalyst-proxy entry/stop/exit combinations plus
  trend, dip, flag and momentum families, in-sample/out-of-sample, monthly block bootstrap,
  random-entry benchmarks: no robust edge; BTC buy-and-hold beat every rule. A fill-day target
  look-ahead on daily bars fabricated 23 "winners" in the first pass; never credit a target on
  the fill day of a daily bar.
- `artifacts/stock-earnings-study-2026-09-25/`: 19,010 real earnings 8-K events (EDGAR
  timestamps, Nasdaq public prices, 1,109 stocks, 2019–2026). The retest rule loses (959 trades,
  14% wins, t −8). Holding after a strong positive reaction is a wash against the market
  (+0.3% / 0.0% adjusted over 20 / 60 days, not significant); stops make it negative; the only
  reliable effect is continued decline after bad news (−1.5% adjusted over 20 days, t −4.7),
  a short. Small-cap drift (+1% to +3%, t ≈ 2, before realistic costs) is a lead, not an edge.
  Decision: no ranker or Laya fine-tune on this data; no real money; no further build until a
  rule shows an edge on unseen data.
- `artifacts/jev-rule-experiment-2026-09-25/`: 24 real Jev calls; `unsupported_inference` answers
  YES 0.88–0.99 for every compliant thesis regardless of rationale or labelling; the split
  question set behaves as intended. Rule B2 (`SKEPTIC_QUESTIONS_V2`,
  `MUSE_JEV_RESEARCH_SELECTION_B2_V1`, migration 020, schema 20) merged at c74d2fd with the
  full suite green (2,566); fixture evidence only, never run against the real Jev yet.
- Short side (same artifact, `short_earnings_study.py`): sell after a bad reaction, hedged with
  SPY, hold 20 days: +0.94% per trade (t 2.9), +1.23% in the unseen years, block p 0.026 against
  the pre-set 0.01; liquid third +1.12%; two of eight years negative; halves at 5% borrowing;
  paper accounts cannot test borrowing. Misses the pre-declared bar. Strategy search stopped.
- Merger arbitrage (`artifacts/merger-arb-study-2026-09-25/`): 587 cash deals 2019–2026 built from
  EDGAR full-text search and filing histories (96% complete, median 84 days), entry spreads on 23
  still-listed 2025–2026 targets (median 1.55%), break loss about 35%: expected return about zero at
  the median spread and about 3% above cash only at upper-quartile spreads, on a small sample with no
  realised price paths (free sources drop delisted stocks). Verdict: an efficient market, not an
  edge; not grounds to build.

## 2026-09-25 — First real managed paper trade (plan 0.10, gate G3 path): REAL-BROKER EVIDENCE

Evidence: `artifacts/first-managed-trade-2026-09-25/` (README, acceptance manifest with SHA-256,
broker snapshot, audit-export manifest; the 22 MB audit export is in the private evidence
directory). Kept apart from every fixture result.

- Owner actions completed the same morning: guarded migration 13 → 20 (backup + restore drill
  passed; audit 6,635 → 6,638 events, exactly the seeded policy rows), private config v2, secrets
  through the no-echo tool, release `288ce85` built and verified, app launched from the release.
  Startup corrections: Jev worker role is `catalyst_jev`; `MKR/USD` no longer tradable at Alpaca.
  Owner decision: the crypto liquidity gate (`CRYPTO_LIQUIDITY_PAPER_V1`) is removed from the paper
  configuration because Alpaca's own venue trades a few thousand dollars an hour in Bitcoin and the
  1% participation rule capped any position at about $50; paper fills do not depend on that
  volume, real money on this venue would.
- Enrollment 1 (`TEST-MANAGED-B8E0CC129E88`, trigger 84,300): `EXPIRED_UNTRIGGERED` at 09:55 UTC.
- Enrollment 2 (`TEST-MANAGED-8429E2450107`, setup `f059e98a…`, trigger 84,590 / max 84,630 /
  stop 83,680 / target 86,530): triggered 09:59:41 UTC with acknowledged subscriptions, risk check
  under `JEV_MANAGED_RISK_V2` (equity $9,999.85, $50 budget, 0.052630789 BTC), authorization
  claimed within 5 s, limit order acknowledged, fills 84,595.25 and 84,605.52, position
  0.052499211 BTC (fee taken in BTC; fee amounts an explicit unknown), stop-limit resting at the
  broker (`PROTECTED / NATIVE_STOP_LIMIT_PRESENT`), management reviews DISABLED. 13:46 UTC:
  Bitcoin fell through 83,700, the stop-limit triggered but did not fill (`STOP_LIMIT_NOT_FILLED`),
  the controller cancelled it and sent an authorized market exit (83,675.06 and 83,616.29), flat,
  clean reconciliation, no halts, no reservation; broker snapshot flat with no open orders.
  Acceptance manifest: 9 of 9 checks PASS; audit chain valid, head 7,900 → 12,037.
- Defect found: the close was attributed `ENTRY_EXPIRED` although the exit request was
  `STOP_LIMIT_NOT_FILLED` throughout; the entry-expiry rule ran on the filled setup once its
  quantity reached zero. Safety unaffected, attribution wrong. Fix queued before any unattended
  run: never apply entry expiry to a filled setup; close with the exit reason that emptied it.
- Close-out: app healthy throughout, then stopped cleanly; ledger cluster left running.

## 2026-09-25 — Close-attribution fix for the first real trade's `ENTRY_EXPIRED` label (FIXTURE EVIDENCE ONLY)

The defect is real-broker evidence (`artifacts/first-managed-trade-2026-09-25/`, setup
`f059e98a…`, STATE events 12000–12002). The fix is fixture evidence only: disposable
PostgreSQL and the fake paper venue, with no broker, provider or owner-ledger contact.

- Cause: `ManagedExecution.manage` took zero broker quantity as proof that an entry never
  filled. Exit fills also empty a filled position, so on the tick that first saw zero quantity
  after the `STOP_LIMIT_NOT_FILLED` exit, the entry-window branch set `exit_requested:
  ENTRY_EXPIRED` just before the close check used it as the `CLOSED` reason.
- Fix: the entry-window expiry, and for the same reason the pre-fill `REVIEW_REVOKED` rule,
  apply only to an `ENTRY_PENDING` or `ORDER_SUBMITTED` setup with no fill evidence (no
  `opened_at`, no recorded buy fill, no broker-reported entry fill). Unfilled entries are
  cancelled and closed as before. A filled setup closes with the exit request that emptied it,
  or `BROKER_EXIT` when the broker's own order did. No state, event kind, reason, schema or
  migration change.
- Evidence: `tests/test_exit_attribution.py`, 13 cases. The real sequence (engineering
  enrollment, 60-minute window, fee taken in the asset, OPEN past the window, stop-limit not
  filled, cancel, two market fills) now closes `STOP_LIMIT_NOT_FILLED` in the state, in the
  results analytics (negative test R) and in the acceptance manifest; 6 of the 13 fail on the
  old code. Full suite 2,579 passed; ruff clean.
- Unchanged: the live ledger's closed record still reads `ENTRY_EXPIRED`; a correction there
  would be an appended, owner-run event. A time-exit deadline or a daily halt reached after a
  position is already empty still takes precedence over its exit request (not changed here).

## 2026-09-26 — Readiness review and crypto-only agent-loop design (DESIGN ONLY, NO CODE)

- Full test suite re-run at 80eb952: 2,579 passed, Ruff clean. No service, broker, provider or
  owner-ledger contact; the live ledger's PostgreSQL was up and no app was running.
- Readiness review for a reliable automated day-trading research system (four read-only audits
  plus code checks). Verified in code, not reproduced by tests: the managed app never sends the
  Jev health probe, so an open Jev circuit breaker never closes and nothing alarms; a lost
  executor lease leaves the app running without app-side protection and never restarting; a
  tripped crash-loop breaker exits 0 so launchd stops restarting (by design); a refused crypto
  exit outside a flatten is never retried and a refused stock exit is resent every second; the
  watchdog never reads `lab.execution_halts`; Alpaca fee activities (`CFEE`/`FEE`) are never
  read, so net P&L and net R are effectively never computed. The first real trade's recorded
  −$51.92 (−1.04R) excludes Alpaca's 0.25% crypto fees: about −$63 to −$74 (−1.26R to −1.48R) in
  cash.
- External facts checked: FINRA retired the pattern-day-trader rule on 2026-06-04 (Alpaca's
  intraday margin framework; the removed account fields are not read by this code); Alpaca
  tier-1 crypto fees are 0.15% maker / 0.25% taker; the free data plan is IEX-only for stocks.
  Live snapshot 2026-09-26 17:13 UTC: 3 of 33 Alpaca USD crypto pairs within 10 bps.
- Owner direction and plan: CONTRACT-RESOLUTIONS.md (2026-09-26) and
  [CRYPTO-AGENT-LOOP.md](CRYPTO-AGENT-LOOP.md), revised the same day after the owner's
  clarification (whole-market research of 20 coins a day, every selected pick traded, a 24-hour
  continue-or-exit review instead of a forced close). Owner decisions: execution on Alpaca Paper (33
  tradable USD crypto pairs without stablecoins on 2026-09-26) and size fitted to the $10,000
  account (10% slices, 0.5% risk cap, 5% total). Nothing is built or activated by this entry.

## 2026-09-26 — Managed-path Jev breaker recovery and health visibility (FIXTURE EVIDENCE ONLY)

- Defect (found by reading code, no incident): migration 011's breaker
  (`lab.review_attempt_permit`, the `lab.complete_review_attempt` trigger) opens after three
  consecutive provider failures and closes only after two successful synthetic health probes.
  Probes are sent only by `ReviewWorker.tick` (`review_worker.py:164-165`, via
  `Gate1Runtime.probe_due`/`probe`). `managed_runtime.build_runtime_from_env` used
  `worker.reviewer`, `worker.store` and `worker.heartbeat` but never `worker.runtime`, so the
  managed app never probed. Once three provider calls failed, selection and position reviews
  stopped permanently and silently: `last_research_tick` kept advancing (the research loop was
  running fine; every review inside it was refused `CIRCUIT_OPEN`), and neither
  `ManagedRuntime.status()` nor the watchdog alarms showed the breaker at all. Recovery needed a
  human to notice and start (or restart) a standalone `review_worker` process against the same
  scope.
- Fix: `ManagedRuntime._research_loop` now runs the same probe step inside its "research"
  periodic tick, before that tick's shortlist work (`ManagedRuntime.probe_once`, gated on
  `research_healthy`): if `gate1.probe_due(now)`, `await gate1.probe(research.reviewer, now)`.
  `gate1` is the worker's own `Gate1Runtime`, injected into `ManagedRuntime.__init__` explicitly
  (`gate1=worker.runtime` in `build_runtime_from_env`) rather than reached through
  `research.reviewer.runtime`; a runtime built without one (most engineering fixtures) never
  probes and behaves exactly as before. The permit semantics are unchanged: a probe still needs
  this worker's own `lab.review_worker_status` row to read RUNNING, which the existing
  `heartbeat_once` loop maintains via the same `reviewer_heartbeat` the probe is gated on.
- Visibility: `ManagedRuntime.status()` gains `jev_breaker` (`state`, `epoch`, `blocked_until`)
  and `jev_calls_today` (`attempts`: `lab.jev_requests` rows, one per `jev_review` call including
  health probes; `receipts`: `lab.jev_receipts` rows, which can exceed attempts because of
  retries; both since local New York midnight, across every credential scope in the database --
  only one engine is ever live against it in practice; visibility only, the owner set no daily
  cap). `Gate1Runtime.calls_today` adds the query, reusing the worker's existing `catalyst_jev`
  role (already granted `SELECT` on both tables by migration 008); no new grant, no migration.
  Either field independently degrades to `null` on its own read failure, so a database hiccup on
  this secondary read cannot take down the rest of the status payload -- including the breaker
  fields themselves, the exact failure mode this package exists to prevent. Both fields are
  plumbed through `managed_app.create_application.status()`'s mapping and through
  `managed_service.STATUS_FIELDS`, an allowlist that filters the real HTTP response and would
  otherwise silently strip them.
- Alarm: `managed_ops.status_alarms` raises `JEV_BREAKER_OPEN` (codes only) whenever the status's
  `jev_breaker.state` is not `CLOSED` (covers both `OPEN` and `HALF_OPEN`, since ordinary reviews
  are refused in both), reaching `notify.py` through the existing generic alarm-forwarding
  mechanism unchanged. Absent when `jev_breaker` is missing or `null` (an older release, or a
  runtime with no `gate1` configured), so this cannot become a false positive on a status shape
  that predates the field.
- Evidence: `tests/test_managed_jev_breaker.py` (new, disposable per-test PostgreSQL database and
  a scripted mock Jev transport, exactly like `tests/test_review_worker.py`'s own breaker tests
  but exercised through `ManagedRuntime.probe_once`/`status()` instead of `ReviewWorker.tick`) --
  an OPEN breaker gets probed and closes after the required two successes, with an ordinary
  (non-probe) review then resuming `RECORDED`; no probe while `CLOSED`; no probe while
  `research_healthy` is `False` or `gate1` is `None`; the status fields' exact shape and today's
  attempt/receipt counts, including the New York midnight cutoff (proved by shifting the query
  time forward 24 hours rather than by fabricating backdated rows); `status()` degrading the two
  fields alone when `gate1.store.connect` is broken. One added test each in
  `tests/test_managed_ops.py` (the alarm, present for `OPEN`/`HALF_OPEN`, absent once `CLOSED` or
  missing) and `tests/test_managed_app.py` (the real FastAPI `/api/v1/lab/status` round trip,
  proving `STATUS_FIELDS` does not strip the two keys). Three existing tests
  (`tests/test_managed_runtime.py`, `tests/test_selection_b1.py` -- shared by
  `tests/test_selection_b2.py`, `tests/test_managed_engineering.py`) construct a bare
  `SimpleNamespace` stand-in for `ReviewWorker` when exercising `build_runtime_from_env`;
  each now also carries a `runtime=` attribute so the new `worker.runtime` read does not raise,
  and one asserts the wiring by identity (`run.gate1 is fake_review.runtime`). Targeted runs
  covering every touched file plus every directly related one (`test_managed_jev_breaker.py`,
  `test_managed_app.py`, `test_managed_ops.py`, `test_managed_runtime.py`,
  `test_managed_engineering.py`, `test_selection_b1.py`, `test_selection_b2.py`,
  `test_review_worker.py`, `test_managed_service.py`) passed: 943 tests, 0 failures. Ruff is
  clean. Several packages were building in parallel on the same disk during this work and the
  coordinator asked each package to run targeted tests only and report in; the coordinator runs
  the full suite centrally afterward -- see its own record for that number, not this file.
- Unchanged: no migration, no schema change, no new database role or grant. `docs/V4.2-BUILD.md`
  and the frozen V1 trigger are untouched; Phase 4's TTL, previous-close capture and TEST-
  exclusion rules are untouched; the breaker's own policy values (`breaker_failure_threshold: 3`,
  `breaker_recovery_successes: 2`, `breaker_cooldown_seconds: 30`,
  `recovery_probe_spacing_seconds: 1`) are unchanged -- this package makes the managed app run
  the existing mechanism, not a new one. The standalone `review_worker` process remains a second,
  independent way to probe and close the same breaker; running one alongside the managed app is
  unaffected and still works exactly as before.
```

## 2026-09-26 — Unattended safety: lease loss, refused closes, halt and Muse alarms (FIXTURE EVIDENCE ONLY)

Fixture evidence only: disposable PostgreSQL, the fake paper venue (`ManagedVenue`), fake
clocks, stub entry points and temporary Muse spools. There was no broker, provider, supervisor
(launchd) or owner-ledger contact, and nothing here is broker acceptance.

- **Lost executor lease ends the process.** Before: `AccountExecutorLease.assert_owned` marked
  the lease lost and every protection tick raised `EXECUTOR_OWNERSHIP_LOST` before account
  safety and `manage()` ran; the tick only latched it, the loop kept going and the process
  never exited, so launchd never restarted it and app-side protection (crypto target, time
  exits, the −3% daily halt, the `STOP_LIMIT_NOT_FILLED` market fallback) stopped silently.
  A PostgreSQL restart was enough. Now the lease records `lost_code` (never set by a normal
  `close()`); the protection
  tick treats a failed ownership check, or a loss found by a claim or send inside the tick, as
  terminal: it runs nothing more, appends `RUNTIME_EXECUTOR_OWNERSHIP_LOST` (runtime, code,
  `PROCESS_EXIT`, exit code, detection time; three bounded attempts, since the database may be
  the cause; a code-only line also goes to the component log), sets the stop event so every
  loop ends, and calls the entry point's exit hook. `managed_app.main` now keeps its
  `uvicorn.Server`, stops it through `should_exit` and exits with status **75**;
  `managed_runtime.main` exits 75 too. A daemon timer ends the process with the same status if
  the graceful shutdown has not finished after **30 s**. The lease is never re-acquired
  in-process; the supervisor's new process goes through normal startup (lease, reconciliation,
  protection).
- **Refused closes are retried with a bounded backoff, in both markets.** Before: a
  broker-refused crypto close outside an operator flatten was never retried (its client order
  ID follows the state revision, and an approved POST's ID is never reused), although by then
  the stop-limit may already be cancelled; a refused stock close was re-sent about once a
  second, because the stock controller appended a `STATE` revision on every tick while an exit
  was requested. Now `ManagedExecution.dispatch` handles a refused `EXIT` like the refused
  `PROTECT` and `AMEND` cases: one transaction appends `EXIT_REFUSED` and one new revision with
  `exit_refusals`, `exit_retry_after` and `exit_retry_of`. No close is authorized before
  `exit_retry_after` (1 s after the first refusal, doubling, capped at 60 s); the retry uses the
  new revision's fresh client order ID and its own one-use five-second authorization. The fifth
  consecutive refusal of a working setup appends the durable `EXIT_REFUSAL_ALARM`; the runtime
  status lists the setup in `exit_refusal_alarms` until a close is accepted (which resets
  the streak) or the setup closes, and the watchdog raises `EXIT_REFUSED_REPEATEDLY`. A close ID
  already spent without a refusal (accepted then cancelled or expired, never sent, or refused
  before this change) gets one new revision instead of a reuse. The stock controller appends a
  revision only when the exit reason changes. The operator flatten no longer has its own
  crypto re-arm (`_rearm_refused_closes`, `flatten_exit_retry_of`): its refused closes take the
  same path, so a flatten close now also waits the backoff.
- **Halts are visible.** `ManagedRuntime.status()` carries `execution_halts` (`available`,
  `count`, sorted `kinds`, `oldest_halt_seq`): every unreleased `lab.execution_halts` row plus
  today's New York `lab.daily_risk_halts` row as kind `DAILY_RISK_HALT`. The watchdog raises
  `EXECUTION_HALT_ACTIVE` plus `HALT_<KIND>` per kind, and `EXECUTION_HALT_STATUS_UNAVAILABLE`
  when the ledger could not be read. `ready()` and `entry_ready` are unchanged (entry SQL and
  the authorization gate refuse on the halts; an existing admission test relies on it).
- **`MUSE_WORK_FAILED` is no longer sticky.** One provider job left without a result (every
  provider exception leaves one) kept the alarm on forever. Now only a *current* job that is
  stuck raises it: no `failed-job:` marker names it, no later job of its lane (the job ID
  prefix) has started, its lane has logged no run since it started, and it is older than
  `muse_max_age_seconds`. Other jobs without a result, and marked jobs, are failed: the watchdog
  result counts them in `muse_provider_jobs` (`stuck`, `failed`) without an alarm. The existing
  rule that a failed latest non-heartbeat run raises `MUSE_WORK_FAILED` until a later run
  succeeds is unchanged, so a provider that fails every job still alarms.
- **Crash-loop breaker documented** (`docs/OPERATIONS-RUNBOOK.md`): it deliberately exits 0;
  the owner decision between stop-and-alert and retry-with-backoff is pending.

Tests: new `tests/test_unattended_safety.py` (15 tests): lease lost before a tick and inside a
tick (nothing sent, one durable event, terminal, a successor process protects after normal
startup); all seven worker loops stop; the worker and app entry points exit 75, the app through
its server's graceful shutdown, with the 30 s hard deadline armed, and the app still exits 3
when it never started; the backoff schedule; refused crypto target, crypto
`STOP_LIMIT_NOT_FILLED` and stock time exits, each refused five or six times then accepted
(waits 1, 2, 4, 8, 16[, 32] s, a fresh ID, one new revision and exactly one claim with a TTL of
at most 5 s per attempt, the alarm at the fifth refusal in the ledger, status and watchdog,
cleared by the accepted close, then closed with its own reason); a spent stock close ID; halts
through the real HTTP status route into the watchdog; the fail-closed halt status and bounded
codes; the Muse stuck/failed distinction and an unreadable spool. Changed existing tests: the
flatten refused-close test waits the 1 s backoff and reads `exit_retry_of`; the app CLI test
checks `uvicorn.Config`/`uvicorn.Server`; the watchdog tests build their Muse spool with
`MuseSpool`'s own schema. Validation (targeted files only; the full suite runs once on the
integrated branch): 12 files, 194 passed, 0 failed; ruff clean. Details under "Validation".

## 2026-09-26 — Fees net of Alpaca, official R, results aggregates (package fees-net-r): FIXTURE EVIDENCE ONLY

Reads Alpaca's own `CFEE`/`FEE` account activities (GET `/v2/account/activities`, the
existing read-only transport and request-budget governor), matches each to a managed
fill by broker order id and time, and appends the fee as cost evidence through the
existing `FILL_COST_CORRECTION` event path (source `ALPACA_PAPER_ACTIVITY`). Adds
`net_r` (net P&L over the reservation's authorized-quantity risk) and `official_r`
(owner ruling R5: net P&L over the *filled* quantity times admitted max entry minus
admitted initial stop) to every measurement, and a new results-aggregate view (count,
win rate, mean gross/net/official R, total fees) grouped by market, arm, selection
policy, question set and agent.

Evidence: `tests/test_managed_fees.py` (22 cases), reproducing the shape of the
2026-09-25 first real managed trade (`artifacts/first-managed-trade-2026-09-25/`) with
a fake Alpaca activities endpoint: net ≈ −$73.77 here (task/owner reference ≈ −$74.03;
the real entry fills' individual quantity split was never published, only their sum,
so this fixture's own even split is close but not bit-exact), official R ≈ −1.475
(reference ≈ −1.48), gross R ≈ −1.256 (reference ≈ −1.26) — all well inside the
reference figures' own stated precision. Disposable PostgreSQL and a fake broker
throughout; the exact wire shape of a real Alpaca fee activity, and in particular
whether an in-kind (coin) fee is reported as documented in `normalize_fee_activity`
below, is **not independently confirmed in this environment**. Real fee confirmation
needs an owner-run read of the live paper account's `/v2/account/activities` (or the
Alpaca dashboard) for a fee-bearing fill, compared against this module's parsing.
Full suite and ruff: see the integration entry below (2,706 passed on the integrated head).

## 2026-09-26 — Research context API and report V3 (plan phase 1, package research-v3) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, a mock Jev transport, a
fixture Alpaca asset list behind the read-only paper transport and the request-budget
governor, a fixture Alpaca market-data source (MockTransport) and the fixture paper venue. No
broker, provider, network or owner-ledger contact; no migration (schema stays 20); admission
SQL unchanged. The plan's live read test of the context against Alpaca Paper is still to do.

- **Research context** (`GET /api/v1/lab/research-context`, `research_context.py`, on Muse's
  route list, read-only): `as_of`; the schedule with the current run and the next runs; the
  report V3 limits; the tradable universe (active, tradable Alpaca crypto USD pairs without
  the nine owner-listed stablecoins, PAXG kept) with bid, ask, last trade, spread in bps, price
  increment, minimum order size, quantity increment and 24-hour volume, each with a source
  label; the caller's open trades (entry, stop, target, quantity, opened at, unrealized P&L at
  the bid), `pending_reviews: []` and recent outcomes (closed trades of 7 days with exit reason
  and `test_r`; the last run's picks with status). Asset list: one `GET /v2/assets` (crypto,
  active) through `ReadOnlyPaperTransport` over the account's `BrokerBudget`, a research read,
  cached an hour, fail-closed 503 when refused or empty. Quotes and trades: the Alpaca
  market-data latest quotes and trades, one request each per batch, cached under 5 s. Volume:
  the 24 completed hourly bars, cached until the next hour, retried after 60 s on failure.
- **`AGENT_RESEARCH_REPORT_V3`** (`research_report_v3.py`) on `POST /api/v1/lab/research-reports`
  (V2 and legacy unchanged): envelope with `run_slot`, `context_as_of`, the V2 agent block,
  1–30 picks and 0–200 skipped coins; picks with kind, the agent's price and its time, levels,
  stated reward-to-risk, optional validity, structured reasoning, the V1 rationale, 0–8
  sources, bars and an analytics-only confidence. Intake is format only, per pick, with its own
  code for each refusal (12 pick codes) and siblings proceeding; no geometry, reward-to-risk,
  stop-distance, price-grid or live-price check. Validity: `valid_until` at most the next
  scheduled run after `run_slot` plus the grace and at most 24 h after `generated_at`; each
  packet expires at its pick's validity; the review stays valid until that expiry.
- **`REVIEW_DOSSIER_V3`** (`research_dossier_v3.py`): the pick as sent (kind, every price, the
  stated reward-to-risk, the reasoning, the rationale, the sources and every bar) within the
  11,000-byte budget, without the agent's identity, either confidence or the signal ID;
  `invalidation` reaches Jev as `disproof`; the manifest records a field map. Over-budget picks
  are refused, never truncated.
- **Selection**: V3 cycles run under the configured rule unchanged (V2 by default; B1 or B2
  when activated) and their selections cross the unchanged admission SQL to an authorized
  entry, shown for V2 and B2.
- **Schedule** `MANAGED_RESEARCH_SCHEDULE_JSON` (`RESEARCH_SCHEDULE_V1`, `research_schedule.py`):
  strict JSON, invalid refuses startup (`AppSettings.from_env`) and preflight; optional
  (absent: report V3 answers 503). Deploy example: `{"timezone": "America/New_York", "runs":
  ["08:00"], "grace_minutes": 60}` and `MANAGED_REPORT_MAX_SECONDS` 86400 (was 1800).
- **Guidelines** `MUSE_RESEARCH_GUIDELINES_V3` (SHA-256
  `5c0afc834ef2dc698f0cf80f2fc790c80d7193ef824ce04a39381350c8366947`), a new section of
  `docs/MUSE-GUIDELINES.md` checked byte for byte against the code; V1 (`0c04bbea…`) and V2
  (`f4081b07…`) unchanged; the Muse worker still embeds V2.

Evidence: `tests/test_research_report_v3.py` (36) and `tests/test_research_context.py` (44),
80 new tests: V3 end to end through intake, dossier, review, selection, admission SQL and an
authorized entry (V2 rule and B2 rule); every pick code in one report with siblings
proceeding; every envelope refusal; exact replay without a universe read; 503 capabilities;
HTTP identity binding; the packet-expiry review lifetime against V2's 60-second window; the
context's universe filtering, caches, budget (a read refused before I/O), auth and roles, and
only the caller's trades and outcomes from real fills; schedule parsing, DST and invalid
cases; app and ops configuration; guideline immutability. Targeted existing modules (V2
intake, dossier, rationale, identity, service, app, ops, guidelines, complete cycle, worker,
hygiene, safety): 273 passed, 0 failed. The B1 and B2 modules' run was stopped by the
coordinator's disk-space interruption after 190 of their roughly 700 cases, all passing, so
those modules still need the full suite. Ruff clean. The full suite was not run here on the
coordinator's instruction (disk under 1 GB); the coordinator runs it.

## 2026-09-26 — Plan phases 0 and 1 integrated on the work branch (FIXTURE EVIDENCE ONLY)

- Packages merged on `pkg/2026-09-26-integration`, each built in its own worktree from 80eb952:
  jev-breaker (ca7b303), fees-net-r (4e7208d), research-v3 (ed47f3b), unattended-safety
  (3db79c6). Conflicts in `managed_service.STATUS_FIELDS`, the `ManagedRuntime` status helpers and
  the `managed_execution` constants were resolved by keeping both sides. Two test expectations
  were updated: fees-net-r's fee-import loop makes eight runtime threads, and the lost-lease test
  now also shows that loop stopping.
- Full suite on the integrated head 6c46cf2: **2,706 passed**, 0 failed; Ruff clean. The work
  branch was fast-forwarded to it. No migration in these packages: schema stays 20, so the owner
  ledger needs no migration for them.
- Disk incident during the build: the drive had under 1 GB free (the space is used by other
  software on the Mac, not this project). Parallel full suites filled `/tmp`; runs were then
  serialized behind a free-space guard, and four disposable test clusters left by interrupted runs
  were stopped and removed. The owner ledger was never touched; its PostgreSQL kept running.
- Nothing is activated on the live system. Owner-run checks still to do: a read of the live
  account's fee activities (confirms the fee wire shape) and a live read of the research context
  against Alpaca Paper.
- Open owner decisions from these packages: the crash-loop breaker policy (stop and alert, or retry
  with growing delays); research-v3's questions (the `invalidation` text reaches Jev as
  `disproof`; whether V3 picks keep the evidence-task loop until the top 5–10 rule lands;
  `MANAGED_REPORT_MAX_SECONDS` 86,400 in the live configuration; B2's economic-relationship
  question has no V3 field).

## 2026-09-27 — Jev top 5–10 selection (plan phase 2, package selection-topk; schema 21) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, a scripted mock Jev
transport, the fixture research schedule and universe, the fixture paper venue and disposable
`/tmp` clusters for the migration rehearsals and the session CLI. No broker, provider, network
or owner-ledger contact. The plan's "one real-Jev run of 20 picks" is still to do (the session
harness now supports it). Nothing activates the rule: `MANAGED_SELECTION_RULE` stays the owner's
switch and V2 remains the default.

- **Question sets** (named versions in `jev_contract.py`, stage SKEPTIC, every Choice built with
  `choice()` so it offers Insufficient evidence): `NEWS_PICK_QUESTIONS_V1` (`news_stale`,
  `already_priced`, `mechanism_contradicted`, `factual_claims_supported`, `prices_consistent`,
  `verdict`), `CHART_PICK_QUESTIONS_V1` (`levels_supported_by_bars`, `setup_already_broken`,
  `factual_claims_supported`, `prices_consistent`, `verdict`) and `BOTH_PICK_QUESTIONS_V1`, the
  exact union. Every question names REVIEW_DOSSIER_V3 fields exactly (no
  `economic_relationship`; the invalidation is `disproof`); V1's `news_stale`, `already_priced`
  and `verdict` are verbatim. Quality: `MUSE_JEV_COMPARATIVE_QUALITY_V3` (`research_ranking.py`),
  because QUALITY_V2 judges "a new catalyst and its economic link", which a chart pick cannot
  have: four 0–2 scores (V1's `disproof_quality` verbatim) giving a 0–100 score, plus V2's
  category labels. All four template hashes are pinned in code and in migration 021.
- **Rule `JEV_TOP_K_SELECTION_V1`** (`research_selection_topk.py`, pure): veto only when a veto
  label is the unique most probable answer (`news_stale` YES, `mechanism_contradicted` YES,
  `factual_claims_supported` UNSUPPORTED, `levels_supported_by_bars` NO, `setup_already_broken`
  YES, `prices_consistent` NO); Insufficient evidence, ties, `already_priced` HIGH and
  PARTIALLY_SUPPORTED are uncertain and cost 10 points each (`max(0, quality − 10 × n)`); the
  verdict is dissent with its tie flag; a failed, invalid, missing or unbound review is
  NOT_RANKED with its own code. Ranking: adjusted score, category (STRONG > ADEQUATE > WEAK >
  Insufficient), earlier final review receipt, the agent's item order. K 5–10, default 10,
  `MANAGED_TOPK_SELECTION_JSON` (`{"k": 10}`).
- **Activation**: `MANAGED_SELECTION_RULE=JEV_TOP_K_SELECTION_V1` (a quality floor refuses
  startup); startup appends `RESEARCH_SELECTION_RULE_ACTIVATED` with K, the question-set versions
  and hashes and QUALITY_V3's; under it report V2, legacy and scanner intake is refused
  (`REPORT_V3_REQUIRED`, nothing stored) and report V3 cycles use top-K; every cycle keeps the
  rule it started with. The key is on the ops lists and in the deploy example (`{"k": 10}`, rule
  unset).
- **Cycle** (`research_cycle.py`): each pick gets one review with its kind's set and one
  QUALITY_V3 review, concurrently, with no evidence task (revisions refused
  `EVIDENCE_REVISION_NOT_APPLICABLE`). Once every pick has both, or its deadline passed (the
  pick's expiry, bounded by `review_validity_seconds` after receipt), one `RESEARCH_RANKING`
  (`research:ranking:<cycle_id>`) records every pick; ranks 1..K are published as
  `RESEARCH_SELECTED` through `ResearchCycle.publish_ranked(conn, cycle_id, item_key, *,
  replacement_for=None)`, the helper the system-check replacement will call. A symbol already
  selected in another cycle of the same run is skipped (`RESEARCH_SELECTION_SKIPPED`,
  `DUPLICATE_SYMBOL_IN_RUN`), as is an expired pick, and the next-ranked pick takes the place;
  `publish_ranked` itself refuses a skipped entry or a duplicate symbol, so a replacement can
  never reintroduce one. Only the event under the ranking key is ever read as the ranking.
- **Migration `021_selection_topk.sql`** (DDL only, schema 21): `lab.managed_review_failure`
  keeps migration 020's dispatching statements byte for byte behind one new top-K branch,
  `lab.managed_review_failure_topk`: migration 013's stored-binding checks for the pick review
  receipt of its kind (`managed_review_failure_topk_bindings`), the pinned question set of the
  kind, an intact top-K activation earlier than the report V3 cycle, no winning veto label in
  the stored bytes, the uncertain codes, the QUALITY_V3 score and category and the adjusted score
  recomputed from the stored bytes, and the packet as the RANKED entry of the cycle's ranking at
  its rank's position with the same scores and receipts. New permanent refusals `TOPK_VETOED`,
  `TOPK_SCORE_MISMATCH` and `TOPK_RANKING_BINDING_FAILURE`; entry dispatch re-runs the check.
- **Session harness** (`scripts/agent_research_session.py`): `--selection-rule TOPK
  [--topk-k N]`, report V3 intake over the session schedule (the deploy example's) and its
  simulated crypto universe, fixture rows for the pick sets (`"pick"`) and QUALITY_V3
  (`"quality"`, `"quality_levels"`), the ranking in `tick` and `decisions.json`, and `submit`
  filling a V3 report's run slot, context time and validity. With system-check merged,
  `execute` gives `SYSTEM_CHECK_V1` a `SESSION_SIMULATION` quote at each V3 pick's own
  `agent_current_price` (ask at the price, bid 5 bps under), the session's simulated market.

Evidence: `tests/test_selection_topk.py` (191 tests): exhaustive truth tables for the three sets
under five verdicts, every ordered tie, soft-margin vetoes, every NOT_RANKED code, the exact
decimal score and its clamped adjustment, every ranking tie-break, K and rule configuration,
activation blocks; on the fixture venue 20 V3 picks (six vetoes, four uncertain kinds, three
failed reviews) → 40 reviews → one ranking of the exact expected shape → ranks 1..10 published →
all ten cross admission SQL and are admitted → the top pick's entry authorized; publication
idempotency and `publish_ranked` (replacement rank 11 admissible; VETOED, NOT_RANKED, unknown,
skipped and duplicate entries refused); top-K packets keep every report V3 field the system
check reads; K = 5; the duplicate-symbol skip; the review deadline; the receipt-order
tie-break; 16 forged packets, vetoed and not-ranked picks, foreign receipts, other question
sets, a forged and a tampered ranking and tampered receipts refused by SQL, and a forged
ranking never read by the code; missing and foreign activations;
V2 and legacy refused (also over HTTP); a cycle keeps its rule both ways; the runtime factory and
preflight; migration 021's byte-for-byte reuse and grants; every V2, B1, B2 and engineering
packet routed as migration 020's dispatcher routes it. `tests/test_owner_migration_rehearsal.py`:
a populated schema-20 ledger with V2, B1 and B2 selections migrates to 21 through the CLI with
the audit head and event count unchanged and every route kept, then runs top-K.
`tests/test_agent_research_session.py`: top-K configuration and fixture rows, an in-process
top-K session through admission and export, V2 refused, and a top-K session over the real CLI.
Schema pins moved to 21 in five existing modules. 196 new tests; full suite 2,902 passed
(baseline 2,706), ruff clean.

## 2026-09-27 — System check of selected V3 picks and run supersession (plan phase 3a, package system-check) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
mock Jev transport, a fixture universe, and Alpaca market-data answers from an `httpx`
MockTransport behind the real read-only market source. No broker, provider, network or
owner-ledger contact; no migration (schema stays 20); admission SQL unchanged. V1, V2, B1, B2
and operator engineering admission are unchanged; only selections whose packet says
`report_schema_version: AGENT_RESEARCH_REPORT_V3` are affected.

- **System check (`SYSTEM_CHECK_V1`, new `system_check.py`, called from
  `ManagedExecution.admit`).** After every existing admission check and the broker price grid,
  outside the shared lock and before the setup row that spends the symbol slot, a V3 pick is
  checked, in this order, the first failure naming a permanent refusal:
  `STOP_DISTANCE_BELOW_MINIMUM` ((max entry − stop) / max entry below
  `MINIMUM_CRYPTO_STOP_FRACTION = Decimal("0.02")`; from the levels alone, so no live price is
  read for it), then on the live price `PRICE_MISMATCH` (|mid − agent's current price| /
  agent's price above 5%), `STOP_ALREADY_HIT` (bid at or below the stop, or the last trade at or
  below it) and `BREAKOUT_NOT_ENABLED`. Entry type from the mid: `PULLBACK` (entry trigger more
  than 0.2% of the mid below it), `IMMEDIATE` (within ±0.2%, bounds included), `BREAKOUT`
  (more than 0.2% above). Equality at 5% and 2% passes. Decisions compare exact Decimal
  products.
- **Live price (`LivePriceReader`, owned by the runtime).** The stream observation's bid and ask
  when its quote is at most 5 s old by its own timestamp; otherwise one REST latest-quote read
  (`/v1beta3/crypto/us/latest/quotes`) through the runtime's `AlpacaMarketSource`, declared a
  research read, fresh by its read time, its own timestamp recorded. At most one REST read per
  protection tick and one per symbol per 5 s. The last trade is the stream's. Nothing usable is
  `LIVE_PRICE_UNAVAILABLE`: transient, audited once per runtime, selection and reason with the
  attempts, retried every tick, never declined.
- **Evidence.** A permanent refusal is final for its receipt: one `SYSTEM_CHECK_REFUSED`
  (key `system-check-refused:<receipt_id>`), and the runtime's `RUNTIME_ADMISSION_REFUSED` and
  `RESEARCH_ADMISSION_DECLINED` carry the same `system_check` object (bid, ask, mid, last and
  their times and sources, the deviation, entry offset and type, the stop distance, the
  thresholds and every check's `PASS`/`FAIL`/`NOT_EVALUATED`). An admitted V3 setup's WATCHING
  state records `system_check` (`PASSED`) and `entry_type` for the later trigger versions and
  measurement. The four check codes and `SUPERSEDED_BY_NEW_RESEARCH` are in
  `PERMANENT_ADMISSION_REFUSALS`; `system_check.SYSTEM_CHECK_REFUSALS` names the four the
  replacement package should replace.
- **Run supersession (`RESEARCH_RUN_SUPERSESSION_V1`).** Every protection tick, before
  admission and whatever the readiness, while a V3 setup is WATCHING or a V3 selection awaits
  admission, the runtime reads the newest `run_slot` of any published V3 selection (a tick
  without V3 work reads nothing more). Older V3 setups still WATCHING are revoked
  `SUPERSEDED_BY_NEW_RESEARCH`
  through `ManagedExecution.revoke` (only while still WATCHING under the shared lock; one keyed
  REVOKE and its INVALIDATED revision); older, unexpired V3 selections not yet admitted get one
  `RESEARCH_ADMISSION_DECLINED` with that reason. Open positions, working entries and V2 cycles
  are untouched; repeated passes write nothing. Admission itself also refuses a superseded V3
  pick under the lock that serializes publication.

Evidence: `tests/test_system_check.py` (41 new tests): each check at its boundaries (exactly 5%
vs just above and below, ±0.2% vs just beyond, exactly 2% vs just below), the stop hit by the
bid or the last trade, the evidence fields; the reader (a stream quote exactly 5 s old is live,
a staler one costs one REST read reused for 5 s, one REST read per tick, a failed read waits
5 s, unusable REST quotes, the research-class declaration); admission end to end through V3
intake, the mock Jev and admission SQL (pullback and immediate admitted with their evidence,
breakout, mismatch, stop-hit by bid and by last and short-stop refused and recorded, a
recorded refusal final without another read, a missing price transient with nothing recorded,
no symbol slot spent, V2 unchanged with no read, a superseded run refused before any read);
the runtime (no ledger read on a tick without V3 work, admission on a fresh stream quote with
no REST read, a stale stream quote costing
exactly one REST read, no live data retried and never declined, a permanent refusal declined
with its evidence and never retried, and a new run retiring the previous run's watching setup
and pending selection while an open position, a working entry and V2 stay untouched,
idempotently). Two research-v3 tests that admitted V3 packets without a live price now pass
one. Full suite and ruff: see "Validation".

## 2026-09-27 — Plan phases 2 and 3a integrated on the work branch (FIXTURE EVIDENCE ONLY)

- Packages merged on `pkg/2026-09-27-integration`, each built in its own worktree from 676183d:
  system-check (81fb1a9) and selection-topk (867f156). The only conflict was in
  `docs/API-CONTRACT.md` (both sections kept: the top-K ranking first, then the system check).
- Integration fix d01eb83: the system check needs a live price before admitting a V3 pick, so
  two top-K end-to-end tests pass a live quote, and the session harness gives the check a quote
  from its simulated market (source `SESSION_SIMULATION`). The system check itself is unchanged.
- Full suite on the integrated head d01eb83: **2,943 passed**, 0 failed; Ruff clean. The work
  branch was fast-forwarded to it.
- Schema is now **21** (migration 021, DDL-only; the rehearsal migrates a populated schema-20
  ledger with the audit head unchanged). The owner ledger is still at 20: before the next live
  run the owner runs the guarded `ledger-migrate --expect-current 20 --target 21` with a backup.
- Nothing is activated: `MANAGED_SELECTION_RULE` still defaults to V2; switching to
  `JEV_TOP_K_SELECTION_V1` is an owner step. Still to do: a real-Jev run of 20 picks (about 40
  calls; confirms the provider accepts the full request size), and phase 3b (replacing a pick
  that fails the check with Jev's next-ranked pick through `ResearchCycle.publish_ranked`).
- Open items from the packages: duplicate symbols across agents in one run keep the first
  selection (the plan says the higher score should win; matters only with several agents);
  K applies per agent report, not per run; the research context shows declined picks as
  selected and no top-K ranks; `/setups` does not list `entry_type`; the REST live-price path
  is tested against a mock only.

## 2026-09-27 — Replacement of declined top-K picks and three visibility fixes (plan phase 3b, package replacement) (FIXTURE EVIDENCE ONLY)

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

Landed on the work branch by fast-forward to 5a7a89d (built on 9b1bf10, the work head; no other
merge in between): full suite **2,963 passed**, 0 failed; Ruff clean; no migration (schema 21).
Owner questions from the package: whether a top-K pick of a coin outside the owner's buckets
(`CORRELATION_UNKNOWN`, today transient) should be declined and replaced; whether a pick of a
coin with an open trade (`ACTIVE_SYMBOL_ALREADY_MANAGED`, today transient and holding its slot)
should be skipped as the plan says; a run whose ranking is exhausted can have fewer than 5 live
picks; admission SQL does not yet bind rank-above-K selections to their replacement record.

## 2026-09-27 — First real-Jev top-K runs; `JEV_RESPONSE_PRECISION_V1` (package jev-precision) (REAL PROVIDER, SIMULATED VENUE)

The owner asked for a real-Jev run of `JEV_TOP_K_SELECTION_V1` on a full report before making it
the configured rule. Claude acted as the research agent. Evidence:
`artifacts/real-jev-topk-2026-09-27/README.md` (reports, tick output, exports, research scripts).
Real pinned `jev-1.13.0`, K = 10, session harness with its simulated venue; no broker, no owner
ledger, no order, no admission.

**Run 1** (13 picks, code `e9c6300`): 1 ranked, 7 vetoed, 5 not ranked.
- 22 of 25 QUALITY_V3 bodies and 2 of 16 pick bodies were rejected as invalid. Every score was 0.01 from the weighted sum of its printed distribution, and every bad sum was 0.99 or 1.01: jev-1.13.0 prints two decimals.
- The 40-call cap ran out.
- Six `SETUP_ALREADY_BROKEN` vetoes were correct or invited by the research: cited bars had traded below four of the stops after their pivots, and the invalidation text carried no start time.

**Fixes** (this package):
- `JEV_RESPONSE_PRECISION_V1` (`jev_contract`), recorded in `docs/CONTRACT-RESOLUTIONS.md`. A body is valid within the error that two-decimal printing can produce. Answers stay as received, and recorded receipts keep their outcome.
- The session harness's `--max-jev-calls` default goes from 40 to 100.
- New harness option `--extra-crypto-pairs` puts further Alpaca USD pairs in `CRYPTO_OTHER`, as production does.
- The research rules were tightened on the agent side; see the artifact README.

**Run 2** (21 picks from the 33 Alpaca USD coins with a fresh quote; 12 skipped with reasons):
- 16 ranked, 5 vetoed, 0 not ranked.
- 42 real calls: exactly 2 per pick, no invalid body, no retry.
- Claims were `SUPPORTED` and the setup intact on all 21.
- Top 10: LTC, LINK, FIL, BONK, LDO, GRT, CRV, DOT, SHIB, DOGE. SUSHI, AAVE, ETH, SKY, WIF and XRP follow as replacements.
- The five vetoes (`LEVELS_SUPPORTED_BY_BARS_NO`: AVAX, ARB, BAT, BTC, PEPE) concern the choice of target. Some were close (P(NO) 0.51 and 0.56).
- The pick verdict (dissent) was REJECT on every pick of both runs; under top-K it never blocks.

Not shown:
- Admission, sizing, the live system check, trading and outcomes. The harness still simulates the V2 risk policy, so `execute` was not run.
- `selection-replay.json` has no top-K replay yet.

Tests: `tests/test_jev_response_precision.py` (14 tests: the real body byte for byte, bounds on
either side, unchanged checks). `tests/test_jev_review.py`'s invalid-distribution helper now sums
to 1.1, and three tests use the new harness option. Validation: see the integration entry.

## 2026-09-27 — Crypto trade size, 24-hour hold, crypto sectors and skip-and-replace for an open coin (plan phase 4b, package crypto-size-hold; schema 22) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
scripted mock Jev transport, fixture universes, and disposable `/tmp` clusters for the migration
rehearsals. No broker, provider, network or owner-ledger contact; every broker mutation keeps
its exact one-use five-second authorization. Nothing is activated on the owner's account:
`MANAGED_RISK_POLICY_ID` and `MANAGED_CLASSIFICATION_POLICY` stay owner settings (the deploy
example now names `JEV_MANAGED_RISK_V3` and `ALPACA_CRYPTO_SECTORS_V1`), and the owner ledger
needs the owner-run `ledger-migrate` to schema 22. The plan's phase-4 "supervised paper trade"
is still to do.

- **Migration `022_crypto_size_hold.sql`** (schema 22). A new audited, immutable table
  `lab.account_risk_market_terms` (a market a MANAGED policy sizes in equity slices; a row can
  only be written in the transaction that inserts its own policy row, so V1, V2 and every
  earlier row can never gain terms), the `JEV_MANAGED_RISK_V3` row and its CRYPTO terms (the
  only two rows added, each with its own audit event), `lab.slice_sizing_failure` (the SQL size
  check), `lab.account_risk_failure` replaced (a policy's per-theme limit in a market is its
  terms' `max_per_theme` there, else the row's; everything else as in 016) and
  `lab.guard_managed_reservation` replaced (016's statements byte for byte behind one slice
  branch). V1's reservation triggers, `lab.stamp_risk_decision` and every existing row are
  untouched.
- **`JEV_MANAGED_RISK_V3` sizing** (`account_risk.slice_size`, `ManagedExecution._slice_entry`).
  A crypto entry is the largest multiple of the coin's quantity increment whose notional is at
  most 10% of current equity, whose planned risk qty × (M − S) is at most 0.5% of equity, and
  which the cash available to crypto can pay: `min(equity, cash − unfilled reserved notional,
  non-marginable buying power)`; a bought quantity is not counted twice. A stop closer than 2%
  below M is refused. The one account check runs on the actual planned risk, which the
  reservation holds as its budget. Crypto open risk is capped at 5% of equity and three open
  trades per sector (theme); US numbers, the 60-second cooldown and the 30% fixed-exit arm are
  V2's. The decision context adds `sizing`; `binding_constraint` is `NOTIONAL`, `RISK` or
  `CAPITAL`.
- **`CRYPTO_24H_HOLD_V1`** (`crypto_holding.py`). A crypto setup admitted from a report-V3
  packet records `holding_policy` at admission and gets no New York day policy: no midnight
  entry cutoff (it may enter until its own expiry, the next run) and no midnight flatten. Its
  hard exit is 24 hours after the first buy fill, reason `HOLD_24H_EXIT`, isolated for the later
  continue-or-exit review. Every other setup keeps `CRYPTO_NY_DAY_PAPER_V1` and `TIME_EXIT`.
- **`ALPACA_CRYPTO_SECTORS_V1`** (`managed_classification.py`). A built-in classification policy
  covering all 33 Alpaca USD pairs in eleven public-category sectors; a coin not on the list is
  `CRYPTO_OTHER`, so no pick of a classified pair waits on `CORRELATION_UNKNOWN` for want of a
  sector (a pair Alpaca lists after startup waits for the next restart; open item). The owner's
  `MANAGED_CRYPTO_BUCKETS_JSON`, when supplied, overrides it (the import is then exactly the V2
  bucket import); `MANAGED_CRYPTO_CLASSIFICATIONS_JSON` still chooses the symbols.
- **A coin with an open trade** (`managed_runtime.TOPK_PERMANENT_ADMISSION_REFUSALS`). For top-K
  selections `ACTIVE_SYMBOL_ALREADY_MANAGED` is a permanent refusal: the pick is declined and
  `TOPK_REPLACEMENT_V1` publishes Jev's next-ranked pick. V2, B1 and B2 selections keep waiting.

Evidence: `tests/test_crypto_size_hold.py` (29 tests, 33 cases): the V3 row and terms carry the
owner's numbers and are audited, V1/V2/legacy rows gain no terms, terms refused for any existing
policy, a later transaction, a frozen row or a US market; `slice_size` at the owner's examples
(2% stop: 10 coins, $1,000, $20; 6% stop: 8.3333 coins, $833.33, $49.9998; 5% and 10% stops;
cash-limited; whole-coin and cent increments; a meme-coin price; one coin above the slice;
exactly 2% passes, 1.99% refused; invalid inputs); SQL/Python parity over 480 cases (908 SQL
checks: the SQL accepts every Python size and its cash-free maximum, and refuses one increment
more); engine entries sized, reserved (budget = planned risk) and authorized (one limit order at
M, one claim); cash caps and bought trades not counted twice; increments; a short stop refused at
entry; ten 6%-stop trades then `MAX_OPEN_PLANNED_RISK` at $499.998; ten 2%-stop trades using
exactly $10,000; three per sector with BTC, ETH and SOL together, a fourth meme coin deferred
until one closes; a V2 reservation's one-per-theme rule binding a V3 entry; the reservation guard
equal to 016's statements plus one branch; forged V3 reservations refused while V2 keeps its
fixed budget; an exhaustive exploration (5,355 checks over 764 reachable ledgers of V1, V2 and V3
reservations) in which V1, the legacy row and V2 answer exactly as migration 016's function
recreated beside the live one (3,060 checks) and V3 as an independent oracle; the hold's exact
record; a V3 position opened at 23:00 New York staying open through the NY-day flatten and
midnight, then cancelled and sold `HOLD_24H_EXIT` at exactly 24 hours while a report-V2 setup is
flattened at 23:55 with `TIME_EXIT`; entries after the midnight cutoff until the setup's expiry;
the first fill's clock kept through a later fill and a restart; all 33 pairs classified once,
`CRYPTO_OTHER`, supersession of `CRYPTO_SHARED`, no read of unlisted pairs, the owner's buckets
overriding, launch settings; a top-K pick for a coin with an open trade declined and replaced by
rank 6 (then admitted), a V2 pick still waiting; and a top-K V3 pick admitted on a live stream
quote under V3, sized (9.8039 coins, $49.99989 risk), reserved and authorized.
`tests/test_owner_migration_rehearsal.py`: a populated schema-21 ledger migrates to 22 through
the CLI with exactly two appended events (the V3 row, then its terms), each hash-chained to the
one before, the old head kept at its sequence, and every V1, legacy and V2 row, open reservation
and account-risk answer unchanged; the 13-to-current rehearsal now counts five appended rows;
the 20-to-21 rehearsal stays DDL-only and is followed by 21 to 22. Schema pins moved to 22 in
five existing modules. Full suite and ruff: see "Validation".

## 2026-09-27 — Crypto trigger version `CRYPTO_ALPACA_TRIGGER_V1` (plan phase 4a, package crypto-trigger) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
mock Jev transport, a fixture universe, and Alpaca market data behind an `httpx` MockTransport
and the real read-only market source. No broker, provider, network or owner-ledger contact; no
migration (the schema is unchanged by this package) and no change to any SQL: the entry quote's
five-second check is the Python authorization gate's. Only crypto setups admitted from report-V3
packets from now on are affected; V1, V2, B1, B2, operator engineering setups and every crypto
setup admitted before this version keep today's trigger exactly.

- **Rule `CRYPTO_ALPACA_TRIGGER_V1`** (new `crypto_trigger.py`, pure). Touch: a print at or
  below the entry trigger T, or a fresh ask at or below T. Confirmation: a fresh ask at or below
  the max entry M, spread (ask − bid) / mid at most `MAX_SPREAD_BPS = 100` (1%; exactly 1%
  passes), a healthy feed, the setup inside its window; the order stays a limit at M. Fresh =
  received on the stream or read over REST at most 5 s ago; the quote's own timestamp is
  recorded, never bounded. Before the trigger: a print at or below the stop
  (`STOP_TRADED_BEFORE_TRIGGER`) or a fresh bid at or below it (`STOP_QUOTED_BEFORE_TRIGGER`)
  invalidates; a touch whose ask is above M invalidates (`PRICE_BEYOND_MAX_ENTRY`, after the
  spread check, as today); a spread above 1% at a touch, or a print touch without a fresh quote,
  waits for the next touch and is recorded (`CRYPTO_TRIGGER_WAIT`, once per setup, reason and
  minute). Decisions compare exact Decimal products.
- **Admission** records `trigger_version: CRYPTO_ALPACA_TRIGGER_V1` in the WATCHING state of a
  crypto setup from a report-V3 packet (any selection rule, the scope of `SYSTEM_CHECK_V1`);
  everything afterwards keys off that state field. `/setups`, `/positions` and `/results` list it.
- **Freshness by read time.** The runtime keeps each stream quote's receipt time (apart from the
  observation rows, so no other record changes). The system check's `LivePriceReader` gained a
  read-time mode: the stream quote if received at most 5 s ago (its `read_at` is the receipt),
  else one REST latest-quote read, fresh by its read time. Trigger and admission reads share the
  reader's limits: one REST read per protection tick and one per symbol per 5 s; stale symbols
  are served in rotation (the symbol read longest ago first). `SYSTEM_CHECK_V1` reads are
  unchanged.
- **Quote-driven evaluation.** Every protection tick, after the queued prints, each WATCHING
  setup of this version is evaluated against its symbol's latest fresh quote: a quoted stop
  invalidates whenever the symbol's market stream is ready (a ledger write only); a touch is
  handed to `observe_trigger` only while entries are ready and outside a capacity cooldown. With
  no setup of this version nothing is read; while nothing touches nothing is written. A print
  touch of this version is confirmed on the same fresh quote (a REST read if the stream's is
  stale) and waits instead of revoking the setup when there is none; its prints above the trigger
  need no quote to be quiet (coalesced). A quote-pass fault latches entries (trigger scope).
- **Entry authorization.** Under the shared lock the version is re-evaluated; a touch that aged
  out while the entry was sized waits (no decision) instead of today's terminal rejection. The
  decision context's `quote_at`, which the gate requires to be at most 5 s old at the claim, is
  the read time; `quote_exchange_at`, `quote_read_at`, `quote_read_basis`, `quote_source` and
  `trigger_version` are recorded beside it.
- **Evidence.** `TRIGGER_CONFIRMED` adds `trigger_version` and a `crypto_trigger` object (touch
  `PRINT`/`QUOTE`, bid, ask, spread in bps, last trade, exchange and read times, read basis and
  quote source); a quote touch carries no `trade_*` fields. The `INVALIDATED` revision and
  `CRYPTO_TRIGGER_WAIT` carry the same object.

Evidence: `tests/test_crypto_trigger.py` (28 tests): the rules at their boundaries (print and
quote touches, exactly 1% vs 1.01%, read-time freshness at 5 s vs just beyond and its fallback,
stop print, quoted stop and stale quoted stop, a touch above max entry vs a wide spread, ignored
and stale prints, the decision quote times, the scope); the reader's read-time mode and its
shared budget; on the fixture venue a print touch with its evidence, a quote touch authorized by
its read time with a 3-minute-old exchange stamp, the gate still refusing a claim 6 s after the
read, the spread boundary with its once-a-minute wait and a later touch, the three invalidations
with evidence and a wide spread that waits, a touch gone stale under the lock (no decision), old
setups unchanged (a 25 bps spread still waits, body and context as today); through the runtime a
top-K pick admitted on a stream quote, quote-touched 10 s later on a quote stamped at admission
and received 4 s before the tick, authorized, claimed, filled and protected; a stale stream
quote costing one REST read (a failed read waits 5 s, no fresh quote means no trigger, then the
REST quote triggers by its read time); a print touch confirmed on one REST read; a print touch
with no quote waiting where the old version is revoked; a quoted stop invalidating while entries
are not ready; ten watching setups sharing one REST read per tick in rotation with nothing
written; admission and trigger sharing the tick's read; nothing read without a setup of this
version; quote-less prints above the trigger coalesced for this version only. No existing test
changed. Full suite and ruff: see "Validation".

## 2026-09-27 — Plan phase 4 and the real-Jev fixes integrated on the work branch; top-K activated in the deploy example

Merged on `pkg/2026-09-27-integration-4` from `e9c6300`:
- crypto-size-hold (`5288057`; migration 022, schema 22).
- crypto-trigger (`97f7806`; no migration).
- jev-precision (`2b49673`).

Code merged without conflicts. `docs/API-CONTRACT.md` and `docs/MANAGED-RUNTIME.md` conflicted only where both phase-4 packages added a section at the same place; both sections were kept.

The deploy example now sets `MANAGED_SELECTION_RULE=JEV_TOP_K_SELECTION_V1` (K = 10). The code default is still V2; see `docs/CONTRACT-RESOLUTIONS.md`, "Activation".

Two tests changed:
- `test_research_cycle`'s invalid distribution now sums to 1.1, since 1.01 is valid under `JEV_RESPONSE_PRECISION_V1`.
- `test_selection_topk` now expects the activated rule.

Validation: full suite **3,041 passed**, 0 failed (2,963 + 34 crypto-size-hold + 28 crypto-trigger + 16 jev-precision). Ruff is clean. Schema 22.

Phase 4's own proof, a supervised paper trade, is still to do. It is owner-run: it needs a ledger at schema 22, Alpaca paper keys injected into the process and a single executor.

Open owner items from the packages:
- Confirm the 33-coin sector list.
- Trigger scope: every report-V3 crypto setup (built) or top-K only.
- A coin Alpaca lists after startup waits (`CORRELATION_UNKNOWN`) until a restart.
- Not built yet: partial-entry cancel after 10 minutes (plan 4.6.1), resuming WATCHING after a stream gap, and quote-touch analytics.
- The session harness still simulates the V2 risk policy and owner buckets.

## 2026-09-27 — `JEV_TOP_K_SELECTION_V2`: the news-stale fix and the 70% veto (package topk-v2; schema 23) (FIXTURE EVIDENCE + ONE REAL-PROVIDER CHECK)

Owner instruction: "ok do it" (2026-09-27), after the diagnosis and live check in
`artifacts/news-stale-check-2026-09-27/`.

**The problem.** Round 3 of the real top-K runs was a report with news and fundamentals
(`artifacts/real-jev-topk-2026-09-27/run3/`, now committed with its research in
`research/round3/`). Its four news picks were all vetoed `NEWS_STALE_YES`, at 0.51–0.70.

**The cause.** A question mismatch, not Jev and not the news: V1's `news_stale` compares the
catalyst with "supplied prior disclosures", and a report V3 pick has none.

**The live check** (12 real `jev-1.13.0` calls, no venue, no database) confirmed it:
- Identical reruns flipped.
- The reworded question answered fresh news 1.00 fresh (ARB, UNI) and an old-news control 1.00
  stale (FIL).

**What changed** (all named versions; V1 and every stored event unchanged):
- **Question sets `NEWS_PICK_QUESTIONS_V2` and `BOTH_PICK_QUESTIONS_V2`.** `news_stale` asks
  whether the catalyst was first made public more than 48 hours before `agent_price_at`. The text
  is the one the live check sent; every other question is V1's.
- **Rule `JEV_TOP_K_SELECTION_V2`.** A veto label vetoes only at a probability of at least 0.70.
  Below that it is uncertain `<Q>_<LABEL>_UNSURE` and costs 10 points. CHART picks keep
  `CHART_PICK_QUESTIONS_V1`.
- **Migration `023_selection_topk_v2.sql`** (DDL only, schema 23). It adds
  `lab.managed_review_failure_topk_v2`, which is 021's branch with exactly the documented changes,
  and puts the V2 branch in front of 021's dispatcher.
- **`MUSE_RESEARCH_GUIDELINES_V4`**, served by the research context. It is V3 plus the method: news
  within 48 hours with its age stated, verbatim excerpts and real times, and the level and claim
  rules that passed.
- **Configuration and harness.** The deploy example activates V2. The harness takes
  `--selection-rule TOPK2`.

**Replay.** Round 3 recomputed offline with V2's answers for the four news picks puts ARB at rank
1 and SOL at rank 3, ranks 24 of 25 picks, and vetoes only LTC (stale, 0.90).

**Evidence.**
- `tests/test_selection_topk_v2.py` (54 tests):
  - The V2 texts written independently from the resolution, and byte-identical to the live
    check's requests.
  - V1's truth table at probability 1 under the V2 sets.
  - Every veto component at 1.0, 0.9, 0.70, 0.69, 0.54 and 0.51, with and without the
    exact-decimal parse.
  - The exact-decimal boundary (0.69999999999 is unsure, 0.70 vetoes), and the code order of
    unsure codes and their 10-point cost.
  - Rule configuration, and the activation, started and stored blocks with tampering refused.
  - A fixture V2 run of eight picks: the right set asked per kind; the exact expected ranking,
    with three clear vetoes and four unsure picks ranked; all five published packets accepted by
    admission SQL; the top pick admitted, triggered and authorized.
  - Forged V2 packets refused (unsure code dropped or renamed, V1 set or policy claimed, K, set
    dropped); clear vetoes never crossing SQL.
  - Migration 023 derived from 021's text; grants; DDL only.
  - The deploy example and preflight; the harness `TOPK2`.
  - Guidelines V4 = V3 + method, with the doc section and the context served.
- Seven existing tests follow the change:
  - Schema pins move to 23 in five modules, and three stepwise migration tests apply 023 after
    022.
  - V1's dispatcher test strips the V2 branch.
  - V1's unknown-version example is now `_V3`.
  - The deploy-example assertions expect V2.

Full suite and ruff: see the integration entry.

**Not shown.** No new real-Jev run of a whole report under V2 yet; the next research run will be
the first. Nothing was traded.

## 2026-09-27 — Research-agent daily toolkit (plan phase 8, package research-runner) (FIXTURE EVIDENCE ONLY)

Fixture evidence only: `httpx.MockTransport` stands in for every network boundary in
every test — the app's `research-context`/`research-reports` routes, Coinbase's public
candles, Alpaca's public quotes, and arbitrary news pages. No real network access in any
test, no broker or provider contact, no owner-ledger contact. 115 tests, ruff clean
(`src tests research_agent`).

- New top-level package `research_agent/` (outside `src/catalyst_lab`; never imported by
  the app; it imports `catalyst_lab.research_report_v3`/`research_dossier_v3` only to
  validate its own output with the app's real models — the same one-way research
  boundary `docs/MUSE-RESEARCH-BOUNDARY.md` already draws: research happens outside the
  app, which only ever receives a finished report over HTTP). Modules: `context` (`GET
  /api/v1/lab/research-context`; an offline Alpaca-public-quotes development fallback
  that `build` explicitly refuses to turn into a submittable report), `market`
  (Coinbase's public 1-hour and daily candles, aggregated to 2h/4h/6h, keeping only bars
  complete as of the recorded retrieval instant — never a later wall-clock read),
  `levels` (the level rules; see the deviation below), `sources` (visible-text
  extraction, exact excerpt cutting, re-fetch verification against the live page, and
  publish-time metadata reading — never invents a time or a paraphrase), `build` (the
  report-V3 builder: literal-only claims, the 48-hour catalyst-freshness rule, and every
  assembled pick checked against the app's own `_parse_pick`/`compile_pick_dossier`
  before it may reach `report.json`), `submit` (the package's one side-effecting call:
  `POST /api/v1/lab/research-reports`), `token` (bearer-token loading from a file or
  environment variable, never printed or logged), and `run` (the CLI, `python -m
  research_agent.run {context,market,levels,build,validate,submit,all}`; `all` runs only
  the deterministic steps and never calls `submit`).
- **Deviation from the reference scripts** (`artifacts/real-jev-topk-2026-09-27/README.md`;
  the research-agent-method memory note). The level rules port research3/levels2.py, the
  script the real Jev accepted twice on 2026-09-27, with one change to target selection.
  That script's target search could settle on a nearby pivot high while a higher bar sat
  elsewhere in the same cited window — exactly the gap real-Jev run 2 caught (AVAX, ARB
  and BAT vetoed `LEVELS_SUPPORTED_BY_BARS_NO` for targeting the latest bar's high, "not
  real resistance"; BTC and PEPE for "a higher high in the same window"). `levels.py`'s
  target is now always the cited window's true highest high, and is refused outright
  when the most recent bar made or tied it. This is one rule that removes both failure
  modes at once, at the cost of treating "the latest bar just made a fresh high" as no
  target found (a breakout setup, which stays disabled per the owner's standing choice,
  `docs/CRYPTO-AGENT-LOOP.md` 4.4) rather than searching for a lesser, weaker target.
- New `news.json` input schema (per coin, `catalysts`/`risks`/`fundamentals` buckets,
  each item with `claim`, `kind` and a `source` of `url`/`excerpt`/`published_at`) for a
  research session's own findings; `build.parse_news` validates its shape and
  `build.verify_news` re-fetches and checks every excerpt before anything from it can
  reach a pick, dropping and recording anything that fails, is unverifiable, or (for a
  catalyst) is older than 48 hours before that pick's `agent_price_at`.
- **Two structural checks added during review, beyond the reference scripts**, both
  caught by hand-tracing the schema rather than by a failing test, then covered by one:
  `selection_rationale.claims` is capped at 8 by the app's own schema
  (`catalyst_lab.muse_reports.SelectionRationale`); a coin with several verified news
  items plus the three fixed technical claims (C1-C3) could otherwise build a pick the
  app refuses outright for having too many claims, so `build_pick` now caps news-sourced
  claims at 5. Separately, `AGENT_RESEARCH_REPORT_V3`'s envelope refuses the **whole**
  report over `MAX_PICKS` (30) picks; a genuine whole-market run (the owner's own
  scope — 33+ coins on 2026-09-27) qualifying more than 30 setups would previously have
  built a report the server rejects entirely. `build_report` now defaults its pick limit
  to the schema's real `MAX_PICKS` (never unlimited) and, when trimming is needed, keeps
  the rule-A and higher reward:risk setups first (research3/builder3.py's own ordering),
  not an arbitrary alphabetical prefix.
- `research_agent/DAILY_PROCEDURE.md` (the step-by-step daily sequence, the verification
  rules, and the round-3 lesson — invented times and paraphrased excerpts — that
  `sources.py` exists to prevent) and `research_agent/DAILY_PROMPT.md` (the prompt a
  scheduler hands a fresh Claude session).

Evidence: `tests/test_research_agent_market.py` (14: the completed-bar cut inclusive
exactly at the retrieval instant, an hour-gap exclusion, Coinbase's product/candles/
ticker fetch entirely mocked). `tests/test_research_agent_levels.py` (22: held-low, the
0.6%/6% entry band and the 2% stop/2R reward-risk boundaries all at their exact edges,
coarser-increment rounding, rule B's held lower pivot with rule A tried first on every
timeframe, and the two target-refusal cases above with a dedicated test showing the code
picks the true window maximum in a case where the original script's nearest-pivot search
would not). `tests/test_research_agent_sources.py` (18: exact excerpt cutting including
curly quotes, the four verification states with the NEAR_MATCH overlap boundary computed
exactly at 0.8, publish-time extraction from a meta tag, JSON-LD (including `@graph`) and
a `<time>` tag with that priority order proven directly, and the never-invented-offset
rule on a bare date and an offset-less time). `tests/test_research_agent_context.py`
(12). `tests/test_research_agent_build.py` (22: the central claim — a built report is
independently re-verified against `catalyst_lab.research_report_v3.parse_report_v3`/
`check_pick` and `research_dossier_v3.compile_pick_dossier` called directly, not just
through research_agent's own wrapper — plus the BOTH/CHART split at the exact 48-hour
catalyst boundary, the age wording ("published N hours before this report"), the dossier
budget refusal on an over-length rationale, a forced-rejection path proving a refused
pick is recorded in `.rejected`/`.notes` (never silently dropped), the 8-claim cap holding
with 8 verified risk items, a malformed context row (a mid price but no quote timestamp)
skipping only that coin rather than crashing the whole build, and 32 qualifying coins
correctly trimmed to the server's 30-pick cap keeping the highest reward:risk setups).
`tests/test_research_agent_submit.py` (6, including that the bearer token is a header only,
never the request body). `tests/test_research_agent_token.py` (8, including that a
loaded token is never printed). `tests/test_research_agent_cli.py` (10: the full CLI
driven end to end from raw Coinbase hourly candles through a build the app's models
accept and a clean `validate`, plus `all` never calling `submit` and no fixture token
ending up in any written file). `tests/test_research_agent_no_secrets.py` (3: the
forbidden live-endpoint literal, derived from the app's own paper constant and never
spelled out, is absent from every module's source; no broker-order-placing or
risk-authorization `catalyst_lab` module is imported by any of them).

Integration: merged after package topk-v2, so the research context the kit reads serves
`MUSE_RESEARCH_GUIDELINES_V4`, whose method section states the same level and news rules the kit
applies. `/runs/` (the kit's default run folders) is now git-ignored. See the integration entry
for the full suite.

## 2026-09-27 — Trade maintenance: the monitoring Jev maintains open crypto trades (plan phase 5, package maintenance) (FIXTURE EVIDENCE ONLY)

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

Integration (coordinator):
- **Merge.** Merged after packages topk-v2 and research-runner, with no conflicts.
- **Partial-entry scope.** `CRYPTO_PARTIAL_ENTRY_V1` now applies to both arms, because plan 4.6.1
  is how every trade opens and the control arm must enter as the maintained arm does.
  Maintenance itself stays managed-arm only (plan 4.6.6).
- **Status fix.** The defect the package found is fixed: `ManagedRuntime.status()` compared the
  worker threads with a constant 7 while `start()` launches 8. `start()` now records
  `expected_workers`, and `test_unattended_safety` asserts that a healthy runtime reports its
  workers alive.
- **Tests.** Full suite on the merge: 3,298 passed before the scope change; the 88 maintenance
  tests pass after it. See the integration entry.

## 2026-09-27 — Pick shadow outcomes, unchanged-plan replay and results views (plan phase 7, package results) (FIXTURE EVIDENCE ONLY)

Every report-V3 pick of every run — selected and traded, selected but declined by the
system check, ranked but not selected, vetoed, not ranked — is now tracked to a shadow
outcome: an offline, deterministic job (`scripts/run_pick_shadow_outcomes.py`) fetches
Alpaca's *public* crypto minute bars (no API key) once a pick's own window plus a 24-hour
hold has fully elapsed, and appends one immutable `PICK_SHADOW_OUTCOME` event recording
whether it would have triggered, then stopped, hit target or exited at 24 hours, in R at the
max entry price, gross and net of an assumed Alpaca tier-1 taker fee
(`PICK_SHADOW_OUTCOME_V1`; the method, its same-bar-ambiguity rule and its fee assumption are
stated in every recorded outcome, never left implicit). A pick that actually traded keeps its
own real, verified `managed_measurement` figures alongside the shadow ones. New read-only
routes (`GET /api/v1/lab/cycles/{cycle_id}/picks`, `GET /api/v1/lab/results/picks`, `GET
/api/v1/lab/results/picks/aggregates`) and a small `/results` page on the existing private
dashboard surface it: today's picks with rank/status/outcome, open trades, closed trades net
of fees, and grouped aggregates by agent, Jev rank/replacement/not-selected, pick kind,
managed-vs-control arm and selected-vs-not (counts always shown, never a bare mean). A
separate pure function, `unchanged_plan.replay_unchanged_plan`
(`UNCHANGED_PLAN_REPLAY_V1`), replays the same minute bars using the levels in force
*before* a detected maintenance change, to say what the unchanged plan would have done and
the R difference against what actually happened. It reads every change from three sources,
unioned per setup in time order: the pre-existing `JEV_MANAGED_EXITS_V1` amendment mechanism
(`MANAGEMENT_PLAN_AUTHORIZED`); package maintenance's `CRYPTO_MAINTENANCE_V1`
(`MAINTENANCE_DECISION`, `outcome: "APPLIED"` only, read from the decision's own recorded
`levels_before`/`levels_after`); and an agreed early exit under `EARLY_EXIT_FLAG_V1` (an
`EXIT_FLAG_RESOLVED` `EXIT_AGREED`, read against its own flag's recorded `evidence.levels`) —
in every case from the event bodies themselves, never the setup's state at read time. Two
more read-only routes, `GET /api/v1/lab/results/maintenance` (per trade with `setup_id`,
across every trade without it) and its `/aggregates` (totals by change kind and arm, counts
beside every mean), and a small dashboard section, surface it; both need an injected
`bar_reader` (a new optional `create_managed_app` parameter, `clock` alongside it) and
compute live rather than from a persisted event. Fixing this package's own bar-fetch-window
boundary (the fetch for a 24-hour-hold exit excluded the one bar needed to price it,
`DATA_INCOMPLETE` at every deadline until now) also corrects `run_shadow_outcome_job`'s own
24-hour-exit case for picks. No migration (the schema is unchanged); no order, broker credential or
risk authorization anywhere in this package.

Evidence: 86 new tests (`tests/test_pick_outcomes.py`, `tests/test_pick_outcomes_ledger.py`,
`tests/test_public_crypto_bars.py`, `tests/test_unchanged_plan.py`,
`tests/test_run_pick_shadow_outcomes.py`, `tests/test_managed_service_results.py`,
`tests/test_maintenance_replay.py`), covering the simulation's boundaries (never triggered,
stop-before-trigger invalidation, triggered then stop/target/24-hour exit, same-bar
ambiguity, the validity-end boundary), the fee-assumption arithmetic in Decimal, ledger
reconstruction of every pick fate against a real (disposable) managed lifecycle, job
idempotency and readiness gating, the results/aggregate routes' counts-first shape and
engineering-setup exclusion, the unchanged-plan replay for a stop raise and a target raise
(helped and hurt), the dashboard page's absence of secrets or `innerHTML`, and — on the
maintenance package's own fixture venue, driving a real admit/trigger/fill/review flow, not
hand-inserted events — every `MAINTENANCE_DECISION` outcome (stop, target, both raised;
`HELD`/`REFUSED` producing no change), an agreed vs. declined vs. pending exit flag, the
three-source union in time order, and both new routes (503 without a configured
`bar_reader`, 401 without auth, a served replay). Full suite and ruff: see "Verification"
below.

Integration (coordinator): merged after topk-v2, research-runner and maintenance, with no
conflicts; the package's replay reads maintenance's merged events. See the integration entry
for the full suite.

## 2026-09-27 — Pending setups survive a stream gap or a restart: `CRYPTO_GAP_RESUME_V1` (plan phase 8 reliability, package gap-resume) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue with
Alpaca-shaped FILL account activities, fake clocks, fake trade-updates and market-stream
sockets, scripted Alpaca crypto quotes and one-minute bars behind an `httpx` MockTransport and
the real read-only market source, a mock Jev transport. No broker, provider, network, launchd
or owner-ledger contact; no migration (schema stays 23) and no SQL change. The package sends
no broker order of its own: it reads bars (GET) and ends or resumes setups in the ledger; every
broker POST, DELETE and PATCH keeps its exact one-use five-second authorization. Only crypto
setups of `CRYPTO_ALPACA_TRIGGER_V1` admitted from now on are affected; every other setup (V1,
V2, B1, B2, US stocks, operator engineering setups and every crypto setup admitted earlier) is
still revoked `DATA_FEED_FAILURE` on a gap and at startup, with today's records.

- **Version `CRYPTO_GAP_RESUME_V1`** (new `gap_resume.py`): admission records
  `gap_resume_version` in the WATCHING state of every crypto setup of `CRYPTO_ALPACA_TRIGGER_V1`;
  the runtime keys off that field only.
- **Held, not revoked.** A market gap (the stream ended, or `start()`, which gaps both markets)
  holds such a setup where today it is revoked (at the next protection tick; inside `start()`
  before any loop starts): one `GAP_RESUME_PENDING` (window, basis), no
  trigger evaluated (the quote pass skips it; its queued prints are consumed
  `GAP_CHECK_PENDING` instead of being evaluated or aged into a revocation), so no entry can
  follow. The market's gap still clears only once every WATCHING setup of it is revoked or held
  with its record written (a failed record keeps entries blocked and is retried, as a failed
  revocation is today).
- **Window.** In process: from the gap (the runtime's time of the `RUNTIME_MARKET_GAP`). After a
  restart: from the previous runtime's last recorded observation of the market: the `gap_resume`
  section of its last `RUNTIME_HEARTBEAT` (new, taken under the runtime lock: `as_of` when the
  market was observed, else the start of the gap it had open, else unknown → admission). Never
  later than an earlier hold still open in the ledger (a runtime that died before its check) or
  than the setup's earliest queued print never evaluated; never before admission.
- **One check.** When the symbol is acknowledged on the stream again, trade updates are
  connected and this process's reconciliation is clean and fresh, and the minute in which the
  stream came back has closed plus 30 s (`BAR_SETTLE_SECONDS`), the protection tick reads
  Alpaca's completed one-minute bars from the window's minute to the last minute that closed
  30 s ago (new `AlpacaMarketSource.window_bars`, GET-only route, taking the tick's one
  market-data REST read of `LivePriceReader`, new `spend_read`: at most one check a tick) and
  every print the stream delivered for the setup since the window's start (the tail after the
  last bar was watched live). Exact Decimal comparisons of the lowest traded price: bars
  unavailable, incomplete or malformed → revoked `DATA_FEED_FAILURE`; else at or below the stop
  → `INVALIDATED` `STOP_TRADED_DURING_GAP`; else at or below the entry trigger → revoked
  `ENTRY_REACHED_DURING_GAP` (no late entry); else `GAP_RESUMED` and the trigger evaluates it
  again. Evidence (`gap_resume`): window, bars read (count, first/last, lowest low and when,
  SHA-256), prints considered, lowest price, decision, gap basis. A gap that begins during the
  read discards the result; a setup whose window ends while held expires as today.
- **Missed entry fills.** For a setup of this version, a protection tick that finds the broker
  holding its coins with no recorded buy fill (the trade-updates stream missed it in an outage
  or a restart) runs the existing REST fill backfill once before the fail-closed first-fill
  rule, so the fill is recorded from Alpaca's activities (`ALPACA_PAPER_REST_BACKFILL`) and
  protected once. Before, a new process's protection tick, which starts beside the stream loop,
  could reach that position before the reconnect's backfill and halt
  `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE` and force an exit (the "Not covered" item of the
  September 24 fill-backfill entry). Every other setup keeps that rule unchanged.
- **Status and watchdog.** `status()` (and the HTTP status) carries `gap_resume` (held setups
  with window, `stream_back_at` and `due_at`; each market's observation); `RUNTIME_HEARTBEAT`
  records it (its `as_of` alone is not a change). The watchdog raises
  `GAP_RESUME_CHECK_OVERDUE` when a setup has been held more than 300 s and
  `GAP_RESUME_STATUS_UNAVAILABLE` for an unreadable section; the other alarms are unchanged.

Evidence: `tests/test_gap_resume.py` (19 tests): the rule at exact Decimal boundaries (low at
the trigger, one hundred-millionth above it, at the stop), unavailable, incomplete, malformed,
duplicate or out-of-window bars and an unreadable print failing closed, tail prints, the window
arithmetic and the restart bound; the watchdog boundary at 300 s and its fail-closed cases; the
explicit-window bar read (one GET, the open minute excluded, invalid arguments refused). Through
the runtime on a disposable ledger: an in-process crypto-stream and trade-updates drop with a
maintained trade and five setups (held; the V2 setup revoked exactly as today; an entry filled
while trade updates were down backfilled and protected by the protection tick; the ask and a
print at the entry after the reconnect buying nothing; one bar read per setup resuming one,
revoking one `ENTRY_REACHED_DURING_GAP`, invalidating one `STOP_TRADED_DURING_GAP` and revoking
one `DATA_FEED_FAILURE`; the resumed setup triggering again); an abrupt restart (no
`RUNTIME_STOPPED`, lease released) where the successor's first protection tick, before any
stream, backfills the fill made while down and protects it once, the maintained trade keeps its
resting stop-limit and levels, maintenance raises its stop with one claimed PATCH, and each held
setup is checked once from the previous runtime's last heartbeat; restart windows bounded by a
gap the last heartbeat reported open, by an earlier runtime's open hold and by an unevaluated
queued print (which alone decides `ENTRY_REACHED_DURING_GAP`); V2, US-stock and pre-version V3
setups revoked exactly as today in process and at restart; the HTTP status and the overdue
alarm; expiry during a gap; a gap during the bar read; one market-data read per tick shared with
the trigger; the fill backfill for this version only; a failed hold record retried or released.
No order is ever sent twice; the audit chain verifies. No existing test changed. Full suite and
ruff: see "Validation".

Integration (coordinator): merged after packages results and research-runner; one both-added
conflict in `docs/API-CONTRACT.md`, where both sections were kept. The coordinator accepted the
package's `managed_execution.manage()` change: for setups of this version only, a read-only REST
fill backfill runs once before the `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE` halt, so an entry that
filled while the app was down is recorded instead of being force-exited. Every other setup keeps
the immediate halt. See the integration entry for the full suite.

## 2026-09-27 — The 24-hour review and early exits: the agent and Jev decide together whether an open crypto trade continues (plan phase 6, package day-review) (FIXTURE EVIDENCE ONLY)

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

Integration (coordinator): merged last, after packages results and gap-resume. Eight files
conflicted where both sides added a field, route, alarm, import or doc section (`managed_service`,
`managed_app`, `managed_runtime`, `managed_execution`, `managed_ops`, `API-CONTRACT.md`,
`MANAGED-RUNTIME.md`, `OPERATIONS-RUNBOOK.md`); every addition was kept. The research kit's session
mode now declares `MUSE_RESEARCH_GUIDELINES_V5`, the version the context serves. See the
integration entry for the full suite.

## 2026-09-27 — Jev's answers read by the question that decides, and maintained trades reviewed every minute: `CRYPTO_MAINTENANCE_V2`, `CRYPTO_24H_REVIEW_V2` (package answer-rules) (FIXTURE EVIDENCE + REAL-JEV LOOP EVIDENCE)

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

## 2026-09-27 — Answer rules, minute maintenance and integration-5 integrated on the work branch

Branch `pkg/2026-09-27-integration-5`: integration-5 (6aa1ae6) plus package answer-rules
(da8dd22), merged at e993d77 without conflicts (git merged `OPERATIONS-RUNBOOK.md`, the one
file both sides changed). No migration: schema stays 23.

integration-5 carries three coordinator changes on top of 8dc02d6:
- Position news may not name the posting agent. `POST /api/v1/lab/positions/{setup_id}/news`
  refuses 422 `AGENT_IDENTITY_IN_NEWS`, before anything is stored, when the posting agent's ID
  appears as a whole word in any case in an agent-written source field (the source ID and
  times; excerpts and URLs are third-party text). Position sources reach Jev's maintenance
  and 24-hour-review contexts, and Jev never sees the agent: this is report V3's
  `AGENT_IDENTITY_IN_PICK` rule applied to post-entry news (`tests/test_agent_identity.py`).
- The runbook's guarded migration of the account ledger now reads
  `--expect-current 20 --target 23` (the live ledger has been at 20 since 2026-09-25).
- AGENTS.md's current-state bullets.

Full suite on the integrated branch at 68f31e7: **3,702 passed**, 2 warnings (deprecation, dependencies),
13 min 41 s; `ruff check src tests research_agent` clean.

Real-Jev check of the V2 versions on this branch (2026-09-28 UTC,
`artifacts/real-jev-v2-2026-09-28`, REAL PROVIDER, SIMULATED VENUE):
- 62 calls to the pinned TypeSafe model `jev-1.13.0`, all valid.
- 19 kit-built picks ranked by `JEV_TOP_K_SELECTION_V2` (0 vetoed).
- Four maintained trades, each reviewed at the +1R milestone and then at four completed 1-minute bars with a growing review history.
- 20 maintenance answers, none refused. Round 5's refused HOLD-with-S1 shape now holds. Three stops were raised to breakeven, each replaced at the venue.
- Four `CRYPTO_24H_REVIEW_V2` decisions: WIF continue agreed (stop raised), XRP continue agreed, CRV exit agreed, SOL continue by Jev alone (the agent silent on purpose, target raised).
- Limits:
  - synthetic bars and prices
  - the harness's V2 risk
  - two session-only acceptance failures, one of them because the harness's `review` step runs no reconciliation after the exit it causes (a harness gap)

## 2026-09-27 — The managed engine on Railway: runtime profile, provisioning, one executor, retiring the Mac ledger (package cloud) (PREPARED, LOCAL FIXTURE EVIDENCE ONLY, NOT DEPLOYED)

Owner request 2026-09-27: deploy the engine and present it as an experiment (Railway chosen
2026-09-19). Nothing was deployed: no Railway account, project, service, variable or URL, no
Railway CLI, no Railway or GitHub API call, no owner ledger, private file or credential touched.
Evidence: fixtures, disposable PostgreSQL 14 clusters (one shaped like Railway's service:
`postgres` superuser with a password, `railway` database, SCRAM for every role,
`log_statement = 'all'`, TLS with plain TCP rejected), the managed tests' mock paper venue, a
fixture Jev, and a local proof that runs the railway-mode trader and ops entrypoints as
processes. No migration (schema 23), no rule or risk change, no new broker call: every broker
POST/DELETE/PATCH keeps its exact one-use five-second authorization.

- **Railway profile.** `CATALYST_ENVIRONMENT=railway` takes configuration from variables only:
  the private config v2 engine settings keep their names (validated by the same checks as the
  preflight, now `managed_ops.environment_findings`; the deploy example's optional settings are
  required in the cloud), role DSNs are Railway reference templates to the Postgres service with
  `sslmode=require`, and the secrets are separate variables (`APCA_API_KEY_ID`,
  `APCA_API_SECRET_KEY`, `TYPESAFE_API_KEY`, `MANAGED_API_TOKEN`, `MANAGED_STATUS_TOKEN`,
  `MANAGED_OPERATOR_TOKEN`, `MANAGED_AGENT_TOKENS_JSON`, six role passwords). Startup refuses
  (exit 2, one code-only line) a `.env` in the working directory or image, `TYPESAFE_ENV_FILE`,
  a missing secret or placeholder, a missing, invalid or unknown engine setting, equal role
  tokens, a non-paper key or a redirected Alpaca endpoint, a DSN of another role or database or
  without TLS, `PORT` unequal to `MANAGED_HTTP_PORT`, state off the Railway volume, and an image
  that is not a sealed release; ops also refuses any broker, Jev or trader credential. Only a
  process on Railway binds beyond loopback (`[::]:$PORT`, dual stack); loopback stays the default.
- **Trader** (`python -m catalyst_lab.cloud_entry trader`): today's app. It records its start
  for the crash-loop breaker on the volume, validates everything, binds its port at once with a
  small responder (`/health` 200 `phase: STARTING`, every other route 503), waits for the
  database, requires the cloud ledger identity, builds the app and waits for the account
  executor lease for as long as another trader holds it (`EXECUTOR_LEASE_WAITING` once a minute),
  then hands the same listening socket to the app. `/health` never claims readiness; readiness
  stays in the token-protected status. Exit codes: 75 lease lost (Railway ON_FAILURE restarts),
  0 crash-loop breaker (stays stopped, deliberately), 0 SIGTERM, 2 refused configuration, 1 a
  startup that failed for ten minutes.
- **Ops** (`python -m catalyst_lab.cloud_entry ops`): every 30 s the Mac watchdog's alarm rules
  (`status_alarms`) against the trader's private status with the status token, plus
  `RELEASE_CODE_MISMATCH`; an audit checkpoint every hour and a `pg_dump` backup every day on its
  volume (new `ledger_ops.backup_database`: the Mac backup's format and manifest over a password
  DSN, the password only in the tools' environment, never an argv), verified and pruned to 14
  days; `AUDIT_CHECKPOINT_FAILED` / `BACKUP_FAILED` until a retry succeeds; the existing notify
  path with the ping URL from a secret variable (`notify` gains an environment-URL form).
- **Container.** `Dockerfile.managed`: `python:3.12-slim`, `tzdata`, PGDG
  `postgresql-client-18` (`PG_MAJOR` build argument; key fingerprint checked), hash-pinned
  requirements (`deploy/requirements.managed.txt` from `uv.lock`), no secret build argument. The
  container starts as root only in `catalyst_lab.cloud_entry` (standard library only), which gives
  the root-owned Railway volume to uid 10001, drops root permanently and `exec`s the component
  with that user's home as `HOME` (`/nonexistent`).
- **Found and fixed: root's `HOME`.** libpq 18 (bundled in psycopg's wheel; PostgreSQL 18's
  `pg_dump`) takes the home directory from `HOME`, looks for a client certificate under
  `$HOME/.postgresql` on every TLS connection and refuses the connection when that path cannot
  be searched. A component that kept the container's `HOME=/root` after
  the drop to uid 10001 would have failed every database connection (`sslmode=require`). The TLS
  test (`tests/test_cloud_tls.py`) shows the failure and the fix: the provisioner, run through the
  entrypoint's root branch, provisions over TLS.
- **Provisioner** (`catalyst_lab.cloud_provision initial`), run once inside Railway as a
  temporary service on the private network: creates `catalyst_lab` (C collation) and, in one
  transaction, refuses a non-empty database or existing lab roles, applies `schema.sql` and every
  migration as a NOLOGIN superuser `lab_owner`, creates `catalyst_backup` (member of
  `pg_read_all_data`), sets SCRAM verifiers (never plaintext; statement logging off) and LOGIN for
  `catalyst_risk`, `catalyst_jev`, `catalyst_app`, `catalyst_operator`, `catalyst_backup` (and
  `catalyst_public` once migration 024 exists; skipped with `CATALYST_PUBLIC_ROLE_ABSENT_SKIPPED`
  before), sets every other lab role NOLOGIN, sets server-side TCP keepalives on the database,
  checks role restrictions and ownership, and appends the audited `CLOUD_LEDGER_PROVISIONED`
  event (`role: ACCOUNT_LEDGER`, `ledger_id`, release), the cloud equivalent of `LEDGER.json`,
  which the trader requires. `rotate-passwords` replaces the verifiers from the current variables
  and appends `CLOUD_ROLE_PASSWORDS_ROTATED`. Existing ledgers are never migrated by it.
- **Retiring the Mac ledger.** `catalyst-lab ledger-retire --ledger-dir <dir> --reason
  MOVED_TO_CLOUD` keeps every marker key, adds `retired_at` and `retired_reason`, writes
  atomically (0600) and never runs twice. The launcher then refuses `--component app|muse` with
  `LEDGER_RETIRED` (now shown by `run_managed_private.py`), `ledger-migrate` refuses
  `OWNER_LEDGER_RETIRED`, preflight reports it; the ledger stays protected and readable.
- **Fresh ledger vs an account with history.** Proven with the mock venue on a never-reconciled
  ledger: an unknown open position fails the startup reconciliation and latches
  `MANAGED_UNEXPLAINED_BROKER_POSITION` (admission refused `RISK_HALT`); an unknown open order
  keeps it unreconciled (entry refused `STARTUP_RECONCILIATION_REQUIRED`, nothing sent) until the
  order is gone; closed historical orders never block. No code change was needed.
- **Release identity.** `scripts/cloud_release.py` stages a clean commit (`git archive`) with
  `release-source.json`; the image build (`python -m catalyst_lab.cloud_release seal`) fails on
  any difference and writes the Mac release's `release.json`, so status reports
  `release_commit`; railway mode verifies the sealed image instead of a host release directory.
- **Deployment definition.** Railway's Config as Code is deprecated ("New services cannot opt
  into Config as Code"), so the project is Infrastructure as Code: `.railway/railway.ts`
  (postgres, trader, ops, experiment, and the provision service only while `CATALYST_PROVISION`
  is set), every secret `preserve()`, engine settings derived from the deploy example. It
  evaluates with the real SDK (npm `railway` 3.11.0) and type-checks with TypeScript 5.9.3
  offline; `railway config plan/apply` was not run. `scripts/cloud_secrets.py` generates every
  token and role password into `railway variable set --stdin` (never printed; idempotent;
  `--rotate`) and writes Muse's agent token to an owner-chosen 0600 file.
- **Muse is the only outside caller; Jev runs every minute.** `MANAGED_AGENT_TOKENS_JSON` is
  exactly `{"muse": <token>}` (the identity screens match a report's agent to its credential's
  own ID); the owner hands Muse the 0600 file and never pastes it anywhere. `MANAGED_API_TOKEN`
  (the legacy identity) is generated, sealed and issued to no one. The cloud's one engine
  setting that differs from the deploy example beyond the environment's own labels is
  `MANAGED_MANAGEMENT_REVIEWS=ENABLED` (owner decision: Jev monitors every trade every minute;
  DISABLED means no maintenance, every 24-hour review exits at T and exit flags end
  unanswered). The guide's first supervised day watches the first maintenance stop replacement
  at Alpaca (`PATCH_REPLACE` or `CANCEL_THEN_PLACE`; never verified against Alpaca for crypto)
  and gives the exact steps to switch to DISABLED and what that does to open trades. The owner's
  ops-shell commands: `cloud_runtime status` (now with `trade_maintenance`, `day_reviews`) and
  `cloud_runtime positions` (stop, target, `stop_replace`, maintenance version, next review).
- **Restore drill.** The drill now drops the bootstrap owner's `ALTER ROLE lab_owner` line too,
  so a cloud backup (with `lab_owner` NOLOGIN) restores and passes locally; Mac backups are
  unaffected.
- **Research kit.** Refuses plain `http://` to any host but loopback and a token file anyone
  else can read (hardening only: in the cloud, research is Muse's, outside the app).
- **Local proof** (`artifacts/cloud-2026-09-27/`): provisioning through the entrypoint over TLS,
  both refusals, the startup refusals and the crash-loop breaker persisting across a restart,
  `/health` and token status, ops checkpoint, backup and a passing restore drill of that backup,
  every session of the ledger's roles over TLS, a second trader waiting for the lease, handover
  on SIGTERM, exit 75 on lease loss; every component with `HOME=/nonexistent`.

Open: every Railway behavior (section "Open items" of `docs/packages/cloud.md`), PostgreSQL 18
(this Mac has 14), trading in the cloud. Guide: `docs/RAILWAY-DEPLOYMENT.md`.

## 2026-09-28 — Connecting Muse to the deployed app: the connection guide and its tests (package muse-connection) (FIXTURE EVIDENCE ONLY)

Owner decisions of 2026-09-28: Muse does all research from now on; once the app is deployed,
Muse reaches it through its API. Muse is the only outside caller, and Jev stays in the app.

New `docs/MUSE-CONNECTION.md` is the one guide for connecting Muse to the Railway deployment.
It covers:

- the connection and Muse's credential, and Muse's exact routes;
- the daily loop: research context, `MUSE_RESEARCH_GUIDELINES_V5`, report V3 and its receipt,
  per-pick and whole-report codes, retries, reading Jev's top-K decisions, position news, the
  per-minute maintenance readback, exit flags and the 24-hour reviews;
- timing, what Muse cannot do, limits, the `research_agent/` kit as a reference client, and
  tested examples.

It was checked against the code, not only against `docs/API-CONTRACT.md`.

**Credential decision.** The deployment gives Muse its own research-agent token as agent `muse`,
`MANAGED_AGENT_TOKENS_JSON = {"muse": ...}`, the only entry, and never the legacy
`MANAGED_API_TOKEN`.

- The agent token submits report V3 with Muse's own `agent_version`, answers the reviews and
  Jev exit flags addressed to `muse`, raises Muse's exit flags and posts its news.
- It is scoped to the research-agent routes in every configuration.
- Only the ID `muse` lets the blindness screens catch the name.
- The role tokens stay required and are never issued. `MANAGED_API_TOKEN` also acts for `muse`,
  so it stays unissued and sealed.
- No other research-agent credential is needed: `configured_agents()` is `{"muse"}`.

**Evidence.** 24 new tests.

- `tests/test_muse_connection_examples.py` (17):
  - every example body through the app's own parsers and models;
  - the report example through the real route with Muse's token, its receipt equal to the
    guide's;
  - the other receipts equal to what the app builds;
  - the route table against a probe of every route;
  - the limits table against the models and the routes' inline limits;
  - the quoted deploy settings, and every link.
- `tests/test_muse_connection_identity.py` (7): the credential decision in the cloud's exact
  token shape.

The new tests and the related suites pass (245 and 189), and ruff is clean. The full suite was
not run, since only docs and tests changed.

**Found.**

- `MANAGED_MANAGEMENT_REVIEWS` is `DISABLED` in the deploy example the cloud copies. Muse's
  review answers and exit flags have no effect until the owner enables it.
- Muse's token cannot read `/cycles/{id}/picks`, results, analytics or timelines.
- No alarm notices a missed Muse run.
- The reference kit stamps `generated_at` when `build` starts, so its own `submit` to the
  deployed app (60-second report age) usually fails.
- Package cloud's secrets script defaults to agent `claude`.

**Not shown.** Railway, a real Muse and real Jev answers. The owner ledger was not touched.

## 2026-09-27 — Public live dashboard (package experiment-page; schema 24) (FIXTURE EVIDENCE ONLY)

A public, read-only live dashboard of the paper-trading system, simplified at the owner's
request the same day. One page shows overall figures, today's figures (New York day), live
trades with Jev's last action, the research agents' and Jev's status and decisions, a feed of
the latest decisions, and past days with a cumulative P&L line. The browser polls the JSON
every 5 s; the server caches it for 5 s. No banner, disclaimer or note, except a fixture
banner that production cannot enable.

It runs as the Railway service `experiment` (`python -m catalyst_lab.experiment_page`,
`Dockerfile.experiment`) with one database login, `catalyst_public`. Migration 024 (DDL only,
schema 24) creates that role NOLOGIN; it can read only four sanitized `lab.public_dashboard_*`
views and execute three pure helpers. The views never expose identifiers, bodies, tokens, raw
Jev answers or source excerpts; trades and runs carry opaque numbers. Engineering trades and
fixtures are excluded; P&L is net of verified fees, else gross marked "fees pending".

The service holds no broker or Jev key, calls no exchange, refuses to start with `APCA_*` or
`TYPESAFE_API_KEY` set, checks its login on every build, reads in `READ ONLY` transactions and
serves one same-origin script under a strict Content-Security-Policy.

Evidence: 88 new tests on disposable PostgreSQL, including exact parity with
`managed_measurement` and `cycle_picks` and one engine-driven trade; schema pins moved to 24;
nine fixture screenshots (`artifacts/experiment-page-2026-09-27/`). Not deployed; no owner
ledger, broker or provider was touched.

## 2026-09-28 — Cloud deployment, public dashboard and Muse connection integrated on the work branch (FIXTURE EVIDENCE ONLY, NOT DEPLOYED)

Branch `pkg/2026-09-28-integration-6`, from 7443686. It merges these packages:
- **cloud** (6da5470): the managed engine as Railway services, with the provisioner, `ledger-retire`, Infrastructure as Code and the owner guide.
- **session-fixes** (8fd9823):
  - The harness reconciles after an exit caused by a review step.
  - The research kit checks the agent block.
- **muse-connection** (b699503): `docs/MUSE-CONNECTION.md` plus 24 example and identity tests. It settled Muse's credential: `MANAGED_AGENT_TOKENS_JSON = {"muse": ...}` is the only entry, and the legacy token is never issued.
- **experiment-page** (c1ae412): the simple public live dashboard, migration 024, schema 24, role `catalyst_public`.

The coordinator made these changes during integration:
- The research kit's `submit` now stamps `generated_at` at the moment it sends. The intake refuses a report more than 60 s old, and the kit's own submit would usually have been refused (found by muse-connection).
- The dashboard's `EXPERIMENT_RESEARCH_SCHEDULE_JSON` references the trader's schedule, so "next run" can't drift.
- Section 2.4 of the deployment guide is filled in.

The merges had no conflicts.

**Full suite at c868ca2: 3,945 passed, 1 skipped, 0 failed**, 15 min 41 s. The skip is expected: the provisioner test for the pre-024 case. Ruff is clean.

An independent read-only review of package cloud found no secret leak, no live-trading path and no way to run two cloud traders. It found nine hardening items, which are being fixed in package cloud-hardening before any deployment.

Nothing is deployed. No owner ledger, broker or provider was touched.

## 2026-09-28 — Cloud hardening before the first deploy: public health without the database, the reviews switch validated, a narrow backup role, the volume checked before root touches it, a proven Mac retirement, the latch clear in the cloud (package cloud-hardening) (LOCAL FIXTURE EVIDENCE ONLY, NOT DEPLOYED)

An independent read-only review of package cloud found no secret leak, no live-trading path and
no way to run two cloud traders; this package fixes the hardening items it raised. Nothing was
deployed; no owner ledger, private file or credential was touched. No migration (schema 24), no
rule or risk change, no new broker call.

- The trader's public `/health` answers liveness from memory in railway mode: no database
  session, no thread (it used to open one of each per request). The Mac app is unchanged.
- `MANAGED_MANAGEMENT_REVIEWS` must be exactly `ENABLED` or `DISABLED` in the shared engine
  checks: a Railway trader refuses a typo at once (exit 2) instead of crash-looping.
- `catalyst_backup` no longer belongs to `pg_read_all_data` (which reads `pg_authid`'s SCRAM
  verifiers): USAGE on `lab`, SELECT on its tables and sequences, default privileges for later
  ones. The provisioner refuses any login role that can read `pg_authid`. Backup and restore
  drill pass; the role is denied `pg_authid`.
- The container entrypoint requires `RAILWAY_VOLUME_MOUNT_PATH` and the state directory on it,
  through real directories only, before it creates or chowns anything as root.
- `catalyst-lab ledger-retire` proves the Mac executor is stopped: the ledger's cluster running,
  the executors' lock free, no app login session, the app port unbound; the lock is held while
  the marker is written, and a launched app re-reads its marker once it holds the lease.
- `cloud_runtime clear-protection-latch` clears an operator-only protection latch from the
  trader's shell with `managed_ops clear-protection-latch`'s semantics (origin recorded as
  `RAILWAY_TRADER_SHELL`).
- The spec test accepts either reviews setting; the guide's quick switch now checks the stops
  first and says no trader runs during a redeploy; Mac retirement pauses first; trader database
  passwords rotate only while flat; Muse gets its token after the first-day checks.

Evidence: tests on disposable clusters and the local proof
(`artifacts/cloud-hardening-2026-09-28/`). Record: `docs/packages/cloud-hardening.md`.

No CONTRACT-RESOLUTIONS entry: nothing here changes a trading rule. The latch clear adds a place
to run an existing owner action; its event and the runtime's release are unchanged.

## 2026-09-28 — First `railway config apply`: the project exists; two plan drifts fixed (REAL RAILWAY PROJECT, NOTHING DEPLOYED)

The owner created the Railway project `catalyst-retest-lab` (environment `production`, Hobby plan, usage limits: $15 alert and $20 hard limit on compute). They retired the Mac ledger `managed-real-20260919-a` (MOVED_TO_CLOUD; executor proven stopped) and ran the first `railway config apply`. It created `postgres`, the `trader-state` and `ops-state` volumes, and the services `trader`, `ops` and `experiment`. No image is deployed and no database is provisioned yet.

The next `railway config plan` showed two drifts:
- **Destructive.** The volumes had no declared region, and Railway had placed them in `us-east4-eqdc4a`. The plan read that as "region → null" and flagged it destructive.
  - Fix: `.railway/railway.ts` now pins that region.
- **Cosmetic.** Railway stores a default restart policy (ON_FAILURE, 10 retries) as null, so a declared `ON_FAILURE` planned as a change on every apply.
  - Fix: the spec now declares only the non-default retry count (100 for trader and ops) and leaves the default policy undeclared. The behavior is unchanged: ON_FAILURE restarts exit 75, and exit 0 stays stopped (https://docs.railway.com/deployments/restart-policy).

After the fix, `railway config plan` reports "already up to date". `tests/test_railway_spec.py` passes 10 tests.

## 2026-09-28 — The owner's monthly Jev budget: `JEV_SPEND_METER_V1`, `JEV_SPEND_GUARD_V1`, `CRYPTO_MAINTENANCE_V3` (package jev-budget) (FIXTURE EVIDENCE ONLY)

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
(`v2_admission`), which keeps V2 exactly. Full suite at `36bd10c`: 4,031 passed, 1 skipped
and 1 failed. The failure was `test_experiment_page.py::test_the_demo_dashboard`, which is
clock-dependent and fails on the unchanged work branch too, from midnight to 10:00 New York. It
was fixed during integration (see the next entry).

## 2026-09-28 — First Railway deployment: the cloud ledger provisioned, trader, ops and dashboard running (REAL RAILWAY DEPLOYMENT, PAPER ACCOUNT, NO RESEARCH OR ORDERS YET)

The coordinator ran this overnight (the owner had turned on bypass permissions and asked for it). Muse's connection is left to the owner.

**Integration** (branch `pkg/2026-09-28-integration-7`, fast-forwarded into the work branch at `c48a7d2`):
- package jev-budget (`c2809cb`: `JEV_SPEND_METER_V1`, `JEV_SPEND_GUARD_V1`, `CRYPTO_MAINTENANCE_V3`; no migration).
- `b49b334`: `scripts/cloud_release.py` removes the legacy root `railway.json` (Config as Code for `Dockerfile.review` and `review_service`) from every staged release.
- A fix to `test_the_demo_dashboard`. It assumed the demo's last research run falls on the page's New York day, which it does not from midnight to 10:00 New York, so it failed on the unchanged work branch too.

**Tests:**
- The full suite ran at `36bd10c` (jev-budget with the work branch at `9ef5227`): 4,031 passed, 1 skipped, 1 failed. The failure is the clock-dependent test above.
- The only code changes between `36bd10c` and `c48a7d2` are the release script and the two test files. Their tests passed at `c48a7d2`, together with the Railway spec and the cloud and budget config tests (24 + 100).
- A full run at `c48a7d2` was stopped by the low-disk guard: 1.05 GB free, below the 1.2 GB floor. It is to be re-run once the owner frees disk space.

**Railway** (project `catalyst-retest-lab`, environment `production`, region `us-east4-eqdc4a`, Hobby plan):
- **Provisioning.** One pinned `CATALYST_PROVISION=initial` apply created the one-off `provision` service. The first image build of `Dockerfile.managed` passed its release seal and the PostgreSQL 18 client check. It printed `PROVISIONED`:
  - database `catalyst_lab`, schema 24, ledger_id `3d080591-1a89-45dd-a5e8-840e24bee527`, audit_seq 7;
  - LOGIN roles: `catalyst_app`, `catalyst_backup`, `catalyst_jev`, `catalyst_operator`, `catalyst_public`, `catalyst_risk`;
  - NOLOGIN roles: `catalyst_reporting`, `catalyst_review`, `catalyst_review_operator`;
  - 0 broker requests.

  A pinned destructive apply then deleted the `provision` service.
- **Budget variables.** A pinned apply added the three budget variables to the trader: `JEV_MONTHLY_BUDGET_USD=50`, `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD=0.042`, `JEV_BYTES_PER_TOKEN=3`.
- **Deploys.**
  - `experiment` from `b49b334`: its code is unchanged since. `/health` returns 200 as `catalyst_public`. Public domain: https://experiment-production-3da9.up.railway.app.
  - `trader` and `ops` from the release staged at `c48a7d2`:
    - the trader logged `TRADER STARTING bind=:: port=8080 release_commit=c48a7d2…` and, one second later, `TRADER SERVING lease_wait_attempts=0 ledger_id=3d080591…`;
    - ops logged `OPS STARTING notify=NOT_CONFIGURED`;
    - ops' first tick: `alarms=RESEARCH_TICK_STALE audit=WRITTEN backup=MATCH`. The Jev reviewer's heartbeat was not yet healthy in the trader's first minute.
    - 30 seconds later, and since: `alarms=NONE`.

    That means, among the watchdog's checks:
    - worker RUNNING, executor EXCLUSIVE;
    - fresh protection, reconciliation and research ticks;
    - trade-updates stream connected, research and account safety healthy;
    - Jev breaker CLOSED;
    - no error code, halt, flatten or budget alarm.
  - Trader public domain, for Muse: the trader service's Railway domain (not published here).
    - `/health` answers liveness only: `alpaca: paper`.
    - `/api/v1/lab/status` and `/api/v1/lab/research-context` return 401 without a token.
  - The public dashboard shows `RUNNING` with a fresh heartbeat.
  - Exactly one active deployment per service. `railway config plan` reports "already up to date".

**Found:**
- A pinned `railway config apply` can fail once with "Problem processing request" and succeed on retry.
- `env -u X railway config plan` fails the SDK's CLI version check.
- Railway-generated service domains are not IaC drift.

**Not yet done:**
- `railway ssh` needs an SSH key registered with Railway. That is an owner account change, not made. Until it is done, `cloud_runtime status`, `positions` and the operator commands, pause and flatten-all included, cannot be run in the ops container. So the status fields outside the watchdog's checks (`entry_ready`, `schema_version`, `management_review_enabled`) were not read.
- Off-host alerts are not configured (`notify=NOT_CONFIGURED`).
- Muse holds no credential for this deployment yet.
- Nothing has traded. No research report exists, and the paper account is flat.

## 2026-09-28 — The learning loop, app and cloud half: outlooks, post-mortems, the nightly reality, scorecard and weekly review, lessons in the research context (package learning-app) (FIXTURE EVIDENCE ONLY)

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

## 2026-09-28 — The owner's 2026-09-25 liquidity decision applied to the cloud (commit 0a49318, DEPLOYED)

On the first live night, the Railway trader refused ARB and SUSHI entries with
`CRYPTO_LIQUIDITY_UNAVAILABLE`.

**Cause.** The owner decided on 2026-09-25 (CONTRACT-RESOLUTIONS: "crypto liquidity gate not
applied on the Alpaca paper venue") to leave `MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON` out of the
paper configuration. The cloud package took the trader's settings from the deploy example, which
still carried the setting, and `cloud_config` required it.

**Why it refused everything.** `CRYPTO_LIQUIDITY_PAPER_V1` requires 60 contiguous 1-minute Alpaca
bars and $1,000,000 traded in them. On 2026-09-28, Alpaca's crypto volume was:
- UNI, the highest: about $81k in 24 hours;
- BTC: about $43k;
- the median coin: $3.3k.

So every coin fails the gate.

**Fix.**
- The setting is removed from the example, and the cloud no longer requires it.
- A test asserts that the trader has no gate.
- The Railway variable was deleted by a pinned destructive apply, which redeployed the trader.

The seven setups still WATCHING survived the restarts. AVAX's `PRICE_BEYOND_MAX_ENTRY` is the
normal no-chase rule. No trading rule changed: the policy stays available by adding the setting
back.

## 2026-09-28 — The research side of the learning loop: movers, the morning outlook, the evening post-mortems, the research checklist and lessons in the kit (package learning-kit) (FIXTURE EVIDENCE ONLY)

The owner approved the learning loop on 2026-09-28 (`docs/LEARNING-LOOP-PLAN.md`). This
package is its research side, in `research_agent/`, wired to package learning-app's contract
(`MARKET_OUTLOOK_V1`, `POST_MORTEM_V1`, `RESEARCH_CONTEXT_V2` lessons, guidelines V6).

**Morning:**

- `lessons` prints the agent's own scorecard lines and writes ordering hints. `build
  --lessons` ranks picks by them only after its 30-pick limit, so no coin is ever dropped
  because of a lesson.
- `checklist export`.
- `outlook` builds a worksheet with one entry per universe coin, pre-filled with the kit's
  facts. `outlook-submit` sends it before the report. It refuses unfilled coins, uncited
  news and event reasons, and publish times that are not the page's own. It reads the
  context again first.

**Evening**, after the nightly job has recorded the day:

- `movers` shows what moved in the whole universe.
- `postmortem` builds a worksheet for exactly the post-mortems the app says are owed, with
  a knowable-before-move helper that suggests true only with a source published by the
  app's reference time. `postmortem-submit` sends the notes.
- `checklist update` scores patterns by guidelines V6: TECHNICAL by lift against the app's
  mover share, the factor kinds by hit rate. It counts only the post-mortems the app
  accepted. Every correction is appended, never edited.

**Fixes:**

- Cutting and verifying excerpts now agree across inline tags next to punctuation.
- A report prepared within the grace before a run answers that run.
- Session builds declare V6.

**From a review pass:**

- An outlook resend after a lost reply sends the same bytes and is never recorded twice.
- A pending trade with no recorded exit keeps its subject and blocks nothing.
- The checklist counts only accepted, notable post-mortems.
- `outlook` sees a coin that joined the universe.

**Procedure.** `DAILY_PROCEDURE.md` and `DAILY_PROMPT.md` document both ways to run a day:
two sessions, or one at 07:15.

**Evidence.** Fixture evidence only. The merge-time tests that compare the kit with the
app's own models and constants pass against learning-app's code at `6127482`.

**Not shown.** Any real run: no call to the app, Coinbase or a news page was made through
these commands.

## 2026-09-28 — The learning loop integrated (packages learning-app and learning-kit), full suite 4,282 passed

Branch `pkg/2026-09-28-integration-8` joins three lines of work:
- the work branch at `e1aa5ea`, and later `0a49318`, the liquidity decision;
- package learning-app (`162b14c`);
- package learning-kit (`a83a87d`).

The coordinator also applied learning-kit's replacement text for `docs/MUSE-CONNECTION.md`
section 8. There were no conflicts.

- **Full suite at `a9107ec`:** 4,282 passed, 1 skipped, 0 failed in 12 min 43 s. The skip is the
  existing pre-024 provisioner test.
- **Research kit tests:** the merge-time checks against learning-app's models, limits and V6
  pin now run and pass. There are 373 kit, Muse-guide and cloud tests, with no skips.
- **Ruff:** clean.

Deployed the same morning (see the next entry). Fixture and disposable-PostgreSQL evidence only
until Railway runs the first nightly job.

## 2026-09-28 — The learning loop deployed to Railway (release 5d2a898, DEPLOYED)

**Before the deploy.** The paper account was flat and no position was open. Seven setups of run 1
were WATCHING.

**Steps:**
1. A pinned `railway config apply` created the `jobs` service. The plan showed only that change:
   - `Dockerfile.managed`, `python -m catalyst_lab.cloud_entry jobs`;
   - cron `30 5 * * *` and restart NEVER;
   - two variables, `CATALYST_ENVIRONMENT` and `MANAGED_DATABASE_URL` (catalyst_risk, password by
     reference);
   - no broker or Jev key.
2. The trader, ops and jobs were deployed from one staged release.

**After the deploy:**
- **Trader:** `TRADER SERVING` on `5d2a898`, `lease_wait_attempts=0`. The seven setups kept
  WATCHING across the restart.
- **Ops:** `alarms=NONE`.
- **Railway:** `railway config plan` reports up to date, with one active deployment per service.
- **Research context:** `RESEARCH_CONTEXT_V2`, with `lessons` (`RESEARCH_LESSONS_V1`, available;
  outlook `NO_GRADED_OUTLOOK_YET`) and guidelines `MUSE_RESEARCH_GUIDELINES_V6` (`075acc02…`).
- **Scorecard:** `cloud_runtime scorecard` in the ops shell prints the preview for 2026-09-27:
  1 run, 18 picks, 10 selected, 10 admitted, 0 filled.

**Not triggered by hand.** The first scheduled run of the jobs service is 2026-09-29 05:30 UTC. A
manual run now would record the 2026-09-27 scorecard permanently before the run's setups finish.
Check that run's `JOBS STARTING` / `JOBS STEP` / `JOBS DONE` lines then.

## 2026-09-28 — `STALE_PRINT_ABOVE_TRIGGER_V1` (owner approval): late prints above the entry trigger stop revoking crypto setups

On the first live day, four report-V3 crypto setups (DOGE, DOT and XRP of run 2) were revoked
`DATA_FEED_FAILURE` by prints that were five-plus seconds old but 3–7.5% above their entry
triggers, so they could never have triggered. Alpaca's crypto feed on Railway delivers the
individually recorded prints about 3.4 s after the trade at the median.

- **New module** `stale_print.py`, recorded at admission on every report-V3 crypto setup. In
  `managed_runtime._process_print`, a late print strictly above the entry trigger of such a setup
  is consumed as `STALE_PRINT_ABOVE_TRIGGER`, and the setup keeps watching. Everything else is
  unchanged, including late prints at or below the trigger, which still revoke.
- **Contract:** CONTRACT-RESOLUTIONS, 2026-09-28.
- **Tests:** `tests/test_stale_print.py` (7) and the related suites (170 passed).
- Full suite at `92871eb`: 4,289 passed, 1 skipped (the pre-024 provisioner test), 0 failed.
- **Deployed 2026-09-28 13:33 UTC:** release `e3becf1`.
  - Ops and jobs deployed first. The first trader upload failed on a TLS error from the Mac
    ("received fatal alert: BadRecordMac"), so the watchdog showed `RELEASE_CODE_MISMATCH` for
    about two minutes.
  - The retry succeeded: `TRADER SERVING` on `e3becf1`, and `alarms=NONE` from 13:33:55.
  - The seven WATCHING setups of run 2 kept watching under their recorded rule. The version
    applies from the next admitted run.


## 2026-09-28 — Pass speed: reused database connections and a print that wakes the protection pass (built, not deployed)

No trading rule changes: the five-second print deadline and every other rule are unchanged. The
app now reaches the deadline check sooner.

**Found on Railway (read-only probes on the ops service).**
- The protection pass (`execution_tick_seconds` = 1) ran every 2.66 s at the median, including
  its one-second sleep. This is measured from `protection_clean_ticks` between heartbeats.
- The 12 individually written prints of the day waited 0.39–3.12 s (mean 1.76 s) in the app
  between being written and being consumed. The market thread's own write took 60–90 ms.
- From ops to postgres: a query round trip took 1.7 ms, a one-query transaction 5.3 ms, and a
  new TLS connection with one query 22.5 ms. `Repository.connect()` opened a new connection for
  every use.
- Locally, with five watched report-V3 crypto setups, one pass opened 32 connections for 52
  queries. `manage` alone opened five per setup.
- Five setups were revoked `DATA_FEED_FAILURE` that day: XRP, DOGE, DOT, RENDER and ADA (and
  TRUMP at 14:19 UTC, whose print arrived 2.8 s late and waited about 2.3 s). UNI's entry print
  (14:29 UTC) arrived 2.9 s late and was evaluated at 4.5 s, 0.5 s inside the limit.
- *Correction to the entry above:* it was three setups, not four (DOGE, DOT and XRP; DOT's
  second late print appended a duplicate `REVOKE`).

**Changes (package pass-speed, branch `pkg/2026-09-28-pass-speed`).**
- `Repository(reuse_connections=True)`: a reusing repository keeps up to 8 idle connections.
  - `with` still commits, or rolls back on an exception, exactly as psycopg does. A failed
    commit closes the connection and raises.
  - A closed, broken, autocommit or re-factored connection, or one left in a transaction, is
    closed instead of reused. One idle over 5 s is pinged first; idle over 60 s or older than
    30 min is closed.
  - Auto-prepared statements stay off, as on fresh connections.
  - A connection used without `with` is never returned, so the executor lease still holds its
    own connection for its session advisory locks.
  - Every session setting in the code is transaction-scoped (`SET LOCAL`, `SET TRANSACTION`).
- Only the trader's live wiring (`build_runtime_from_env`) enables reuse. Every other
  repository, and every test fixture, keeps a new connection per use.
- A committed `MARKET_PRINT` sets `print_queued`, and the protection loop waits on it instead of
  sleeping out its second. `stop()` sets it too.
- **Local profile, same pass:** 32 new connections become 0, and the pass takes 11 ms instead
  of 76 ms. On Railway that removes about 0.7 s of connecting per pass with five watched setups,
  and more with open trades.

**Tests:** `tests/test_pass_speed.py` (14), and the related runtime suites: 438 passed.
- The first full suite at `9d0391a` had 4,302 passed, 1 skipped and 1 failed. The failure,
  `test_the_demo_dashboard`, also fails on the base commit. The demo's feed has 19 lines from
  14:00 to 16:03 UTC, until its oldest open Jev-managed trade has had its 24-hour review. The
  test now expects 19 or 20 from the builder's own rule (`1339d00`), checked at simulated
  09:00, 15:00 and 17:00 UTC; the 20-line cap is tested in `test_experiment_report`.
- Full suite at `1339d00`: 4,303 passed, 1 skipped (the pre-024 provisioner test), 0 failed.

**Not deployed; awaiting the owner.** Deploying restarts the trader while UNI and LTC are open.
They are protected by native stop-limit orders at Alpaca, and a restart with open trades has not
yet happened live. After a deploy, measure the pass period from heartbeats and the
`MARKET_PRINT` → `MARKET_PRINT_CONSUMED` waits with the same probes.

## 2026-09-28 — The pass-speed fix deployed, and a Jev transport failure retried once per review

**Pass speed deployed** (owner: "Deploy it"), release `f8705e1`, 15:20 UTC.
- Ops and jobs were deployed first, then the trader: `TRADER SERVING`, `lease_wait_attempts=0`.
- It was the first restart with an open trade (UNI), and the reconciliation after it was clean:
  1 position, 1 order.
- The protection pass period (heartbeats) went from 2.65 s to 1.30 s at the median, including
  the one-second sleep. The pass's own work went from about 1.65 s to about 0.3 s.

**Jev transport retry** (owner: "we need to fix current trade monitoring the reviews are
failing"). The contract entry is in CONTRACT-RESOLUTIONS, 2026-09-28.
- The failures are not network ones: from ops, TCP to api.typesafe.ai took 0–2 ms and TLS 4–7 ms
  over IPv4, and IPv6 fails at once. The stalled calls stall at the provider.
- Tests: `tests/test_jev_review.py` and `tests/test_review_worker.py` (101), and the related Jev
  suites (929). Full suite at `0845afc`: 4,310 passed, 1 skipped, 0 failed.

**Also found on the first live trades (see NEXT-BUILD-PLAN):**
- LTC's stop-limit touched but did not fill (15:16 UTC). `STOP_LIMIT_NOT_FILLED` cancelled it and
  sold at market at 68.941, entry 68.865: the first live fallback exit.
- UNI's stop raise at 15:31 UTC cancelled the new replacement order. The old one was still in
  the snapshot, so the sell reservation looked doubled (`SELL_RESERVATION_EXCEEDS_POSITION`).
  UNI was without a stop order at Alpaca for about 6 s, until `PROTECTION_REQUIRED` placed a new
  one.
- The dashboard's "Paper account" shows the equity on the latest risk decision, and protective
  decisions record 0.

## 2026-09-28 — Protection accounting: an order being replaced is not a second sell reservation

A fix for UNI's 15:31 UTC stop raise, in which the planner cancelled the new stop-limit (see the
entry above). The contract entry is in CONTRACT-RESOLUTIONS, 2026-09-28.
- Tests: `tests/test_crypto_execution.py` (47), and the related protection and maintenance
  suites (253).
- Full suite at `d153ede`: 4,317 passed, 1 skipped, 0 failed.

## 2026-09-28 — Heartbeat regression fixed; data retention decided

- **The regression.** `RUNTIME_HEARTBEAT` is meant to be written on a meaningful change or once
  a minute (plan 4.7). The day-review pass stamps `day_reviews.last_pass_at` on every heartbeat,
  so one was written every 5 s: 40 of 40 consecutive heartbeats differed only in that field.
  That came to about 23 MB a day, the ledger's largest writer.
- **The fix.** The signature leaves out `last_pass_at` of `day_reviews` and `trade_maintenance`.
  `tests/test_audit_volume.py` gains a test that fails without the fix.
- **Retention.** The owner delegated the decision; it is recorded in docs/DATA-RETENTION.md:
  write less, archive closed periods with a verified checkpoint, never delete in place.

## 2026-09-28 — Research schedule every 2 hours

The deployed schedule is 12 runs a day (owner setting; the entry in CONTRACT-RESOLUTIONS is
2026-09-28). `tests/test_research_context.py` and `tests/test_muse_connection_examples.py` pin
the deployed value; the guide's examples keep the daily schedule and say so. The tests that read
the deploy example pass: 331.

## 2026-09-28 — Research kit: intraday profiles for the 2-hourly runs (package intraday-kit)

`research_agent` gains named research profiles. They change the research method only; no app rule
changes.
- `DAILY_V1` is the default, byte-for-byte as before.
- `INTRADAY_V1` (`--profile intraday-v1`): 1h, 2h, 4h; entries 0.3–3% below the mid.
- `INTRADAY_V2` (`--profile intraday`): 1h, 2h, 4h, 6h, 1d; entries 0.3–6%. It covers every coin
  the daily profile finds and prefers a closer 1h entry when one qualifies.
- `market`, `levels`, `build` and `all` take `--profile`, and the outputs carry the profile name.
- DAILY_PROCEDURE.md gains the "Intraday run (every 2 hours)" section and DAILY_PROMPT.md its
  prompt (agent version `claude-as-muse-intraday-v1-09.28`; a test checks every prompt version
  against the app's 32-character pattern).
- **Dry run on 2026-09-28 18:48 UTC** (33 coins, not submitted):
  - daily: 12 setups, median entry 4.17% below the mid;
  - V2: 12 setups, median 4.14% (POL moved to a 1h entry 2.02% below);
  - V1: 2 setups, median 1.64%.
  - The app's 2% minimum stop and 2R target keep entries at held lows several percent below the
    price, so in a rising market trades come mainly on dips.
- **Tests:** research-agent tests 333 passed. They include coverage over 400 seeded markets:
  daily 78, V2 83, V1 21, and every daily or V1 find is also a V2 find.

## 2026-09-28 — The trade window as a setting: `CRYPTO_WINDOW_REVIEW_V1` and `CRYPTO_WINDOW_HOLD_V1`, 4 hours in the deploy example (package review-window) (FIXTURE EVIDENCE ONLY, NOT DEPLOYED)

Owner, 2026-09-28: "we need to do shorter windows than 24 hours … you decide the window". The
window decided for the owner is 4 hours (FAST-CYCLE-PLAN change 2). The contract entry is in
CONTRACT-RESOLUTIONS, 2026-09-28. Built on branch `worktree-agent-af3b88e005eb4b104` from
`12bfb13`.

- **Setting.** `MANAGED_CRYPTO_WINDOW_JSON` is parsed strictly (`crypto_holding`):
  - read by `build_runtime_from_env`;
  - checked by the Mac preflight and watchdog (`managed_ops`);
  - required on Railway (`cloud_config`).
  The deploy example sets `{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}`, and
  `.railway/railway.ts` derives it. Without the setting, admissions record `CRYPTO_24H_REVIEW_V2`
  and `CRYPTO_24H_HOLD_V1` exactly as before.
- **Admission.** A setup records `CRYPTO_WINDOW_REVIEW_V1` (maintained arm) or
  `CRYPTO_WINDOW_HOLD_V1` (control arm), plus `holding_window_seconds`. Each trade keeps the
  window it started with.
- **Readers that now use the setup's recorded window:**
  - the review clock, request, discussion, retry and decision (`trade_review.DayReviews` no
    longer times every review with V1's constants);
  - the fail-safe and the hold exit (`managed_execution`, through the recorded policy);
  - the research context's open trades and pending review requests;
  - the day-review status (`policy_id`, `window_minutes`);
  - the setup readback and the owner's position view;
  - `DAY_REVIEW_DECISION_REPLAY_V2`, `UNCHANGED_PLAN_REPLAY_V2`, the scorecard's method labels
    and the weekly review.
- **Jev's questions:** `JEV_DAY_REVIEW_QUESTIONS_V3`, V2's texts in the window's words (pinned).
  At 24 hours they equal V2's text.
- **Public dashboard:** window-neutral words only; the layout is unchanged.
- **Fixed on the way:** `WEEKLY_REVIEW_V1` raised `AttributeError` on any closed report-V3 trade,
  because it called a string method on the recorded policy dict. It now reads the policy ID.
- **Tests:**
  - `tests/test_review_window.py` (48, new);
  - the setting's checks in `tests/test_cloud_config.py` and `tests/test_railway_spec.py`;
  - the new words in `tests/test_experiment_report.py`;
  - `tests/test_early_exit.py`'s stand-in `admission_policy` takes the window argument.
  Targeted suites: the touched files and their neighbours (31 files: the day-review,
  answer-rules, early-exit, crypto hold, maintenance, execution, runtime, ops, cloud, Railway,
  research-context, learning, scorecard, weekly-review, replay and dashboard suites). Result:
  825 passed, 0 failed. `ruff check src tests` is clean. The full suite was not run (targeted
  tests only).
- **Not deployed.** The release and the Railway variable must arrive together
  (RAILWAY-DEPLOYMENT.md 7.6). There is no broker proof, and no V3 question set has been sent to
  the real Jev.

## 2026-09-28 — Deployed: the 4-hour trade window (release `6687372`, 19:28 UTC)

- Order, as in RAILWAY-DEPLOYMENT.md §7.6:
  1. `MANAGED_CRYPTO_WINDOW_JSON` = `{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes":
     240}` set on the trader with `--skip-deploys`;
  2. ops and jobs deployed from the staged release, then the trader.
- `TRADER SERVING` on `6687372`, `lease_wait_attempts=0`. The status reports `day_reviews`
  policy `CRYPTO_WINDOW_REVIEW_V1` with `window_minutes` 240. `railway config plan` reports
  the configuration up to date.
- The open GRT trade (admitted 19:17 UTC) keeps `CRYPTO_24H_REVIEW_V2`. Admissions from this
  deploy on record the 4-hour window.
- The same release carries the `WEEKLY_REVIEW_V1` fix (it no longer crashes on a closed report-V3
  trade), `UNCHANGED_PLAN_REPLAY_V2` and `DAY_REVIEW_DECISION_REPLAY_V2`. Full suite at
  `6687372` (the window branch): 4,402 passed, 1 skipped, 0 failed.
- The research kit's intraday profiles (`11a227a`) are local tooling; the 2-hourly timer uses
  `--profile intraday` (INTRADAY_V2) from the 16:00 slot on.

## 2026-09-28 — Public dashboard polish (owner: "the dashboard doesn't show how many we bought … a lot of UI UX polish")

Built by a helper agent on a page-level branch, then rebased onto `work` as
`pkg/2026-09-28-dashboard-polish`. No view or migration change; the record is
docs/packages/experiment-page.md, "2026-09-28 update".
- **Position size:** coins held, dollars at entry and now; closed trades show coins bought and
  dollars at entry.
- **Paper account:** zero or missing equity shows "—". Protective decisions record 0.
- **Feed:** consecutive held, failed, refused or discarded reviews of a trade fold into one line.
- **Redesign:** headline P&L with an inline-SVG cumulative line, trade cards with a stop–target
  bar, a visible closed-trades section, per-day bars, and a phone layout with no sideways scroll
  at 390 px. Light and dark themes both keep a contrast of 4.5:1 or more.
- **Tests:** experiment page, report and public views (101). Full suite at `daed609`: 4,330
  passed, 1 skipped, 0 failed.
- **Not deployed:** awaiting the owner. It deploys only the experiment service.

## 2026-09-28 — Public dashboard v2 (owner: "Dashboard looks great", plus three follow-ups)

The owner judged the first polish "AI-ish, not well designed … it can show more details about the
trade". The redesign, page-level only (no view or migration change; record in
docs/packages/experiment-page.md):
- **Style.** A report layout: IBM Plex Sans and Mono (self-hosted, OFL), hairline rules
  instead of cards, one accent colour, light and dark themes.
- **Top of the page.** A summary strip, and positions and closed tables with size, levels and
  distances.
- **Trade detail.** Each trade expands into a chart of Alpaca's public 5-minute bars, fetched by
  the reader's browser (CSP `connect-src` allows `data.alpaca.markets`). It shows the entry, the
  stop as a step line, the target, buy and sell markers and a lane of Jev's reviews, beside a
  timeline running from pick to exit.
- **Owner's follow-ups.**
  - Prominent section headings.
  - Research detail: the current cycle's picks with their distance from the price, Jev's verdict
    and status, and today's runs.
  - A scrollable Jev decision log with a "This cycle / Today" toggle, from one bounded day query
    on the existing decisions view.
- **Tests:** 173 passed across the experiment, public-view, hygiene and review-window suites,
  after rebasing onto the window release. The rebase conflict in the review wording kept the
  window-neutral words.

## 2026-09-28 — The guarded cloud migration and `JEV_LIVE_REVIEW_POLICY_V2` (migration 025, schema 25) (package cloud-migrate) (FIXTURE EVIDENCE ONLY, NOT DEPLOYED, NOT MIGRATED)

Built on the coordinator's instruction after the first live day. The contract entry is in
CONTRACT-RESOLUTIONS, 2026-09-28; the owner's steps are RAILWAY-DEPLOYMENT.md 7.8. Branch
`worktree-agent-a2fe1338e39b1d0c2` from `b2a8f03`.

- **The guarded cloud migration** (`CLOUD_MIGRATE_V1`): `python -m catalyst_lab.cloud_provision
  migrate --expect-current N --target M --backup <reference>`, owner-run as the one-off provision
  service (`CATALYST_PROVISION=migrate` plus three `CATALYST_MIGRATE_*` values in
  `.railway/railway.ts`; the service gets the admin connection and no role password).
  - Guards: `ledger-migrate`'s version checks, the release's own target schema and a fresh
    backup of this ledger (at most 2 hours old, at the expected schema, its head in this
    ledger's chain).
  - One transaction under the audit lock (bounded waits). Post-checks: target recorded, history
    intact, new roles NOLOGIN, no broad login role, `lab_owner` ownership, backup role's reach.
  - Then the audited `CLOUD_LEDGER_MIGRATED` event and a second-session check.
  - `cloud_entry` accepts exactly that argument shape.
- **The fresh backup it names**: `railway ssh --service ops -- python -m catalyst_lab.cloud_entry
  backup`. It is the daily backup's code, run as the ops user (root is refused), and it prints the
  hashes and the `migration_backup_reference`. It also fixes the doc's old hint, since
  `/data/alarms.json` shows a backup's hash for one 30-second tick only.
- **`JEV_LIVE_REVIEW_POLICY_V2`**: V1 with 8 s question and batch budgets.
  - `review_config` accepts exactly V1 or V2.
  - Migration 025: `lab.register_review_scope` takes V1 (unchanged scope) or V2 (its own scope,
    so 8-second permits); the research intake takes V2 with V1's timing; migration 010's intake
    is kept byte for byte.
  - `SCHEMA_VERSION` 25. The status's `jev_breaker` names `policy_version`.
  - The deploy example stays on V1 until the owner's step 7.
- **Schema pins moved to 25**: the rehearsal and fixture tests that apply 023 and 024 by name
  now apply 025 too.
- **Tests** (new: 89):
  - `tests/test_cloud_migrate.py` (27, new): the owner's flow on a Railway-shaped ledger
    provisioned at 24. It takes the ops backup, keeps appending events, migrates 24 to 25 with
    the real 025, verifies the chain, the event, the roles, ownership, the backup grants, the
    trader's role check and V2's registration, and refuses a second run. The next backup
    restores in the drill at 25. Every refusal leaves the ledger unchanged: the backup reference
    (missing, malformed, stale, future, wrong schema, foreign head, inconsistent), the versions
    and files (wrong expected version, unknown target, not ahead, not this release), not the
    superuser, not a cloud ledger, a held lock, six faulty migration files, a new login role.
    Plus the ops command's refusals, the entrypoint's shapes and the reference's one shape.
  - `tests/test_jev_review_policy_v2.py` (16, new): V2's own scope and stored policy; the
    database refusing anything between V1 and V2; permits of 8 s (V2) and 3 s (V1); a 5-second
    answer kept under V2 and cut at 3 s under V1 (then retried); the worker's 8 s database
    waits; the intake and the worker end to end under V2; 025's literals and stored functions
    against 010 and 011; 025 on a populated schema-24 ledger changing only its functions.
  - `tests/test_review_config.py` (+43), `tests/test_railway_spec.py` (+2: the migrate mode's
    start command and refusals; V2 accepted by the cloud profile, a mix refused),
    `tests/test_managed_jev_breaker.py` (+1: V2 named beside its own breaker).
  - Targeted run: the cloud, Railway spec, review, Jev, research-intake, ledger, local-migration
    guard, owner migration rehearsal, managed runtime, ops, public-view and experiment-report
    suites, plus the four edited rehearsal tests. Result: 760 passed, 1 skipped (the expected
    `catalyst_public` skip), 0 failed. `ruff check src tests` is clean. The full suite was not
    run (targeted tests only).
- **Not run on Railway and not migrated.** The cloud ledger is still at schema 24 with the live
  release on V1. The order is RAILWAY-DEPLOYMENT.md 7.8: fresh backup, owner-run migration, the
  release at once, then the variable. No real-Jev call, no broker proof, and no evidence yet that
  the provider's stalls answer within 8 s.

## 2026-09-28 — Migration 025 run on the cloud ledger; Jev review policy V2 live

The owner explicitly authorized me to run this one migration instead of running it themselves
(2026-09-28 ~16:50 New York: "You run it (I authorize)"). Later migrations need their own
authorization. Steps, per RAILWAY-DEPLOYMENT.md §7.8:

1. **Staging.** Release `d60963e` staged. It is the first release at schema 25, with the guarded
   migration command, migration 025 and `JEV_LIVE_REVIEW_POLICY_V2`.
2. **Fresh backup on ops at 20:51:10 UTC.**
   - The running ops release had no `cloud_entry backup` command yet (it ships in `d60963e`).
     So the backup ran through the deployed `ledger_ops.backup_database`, exactly as the daily
     backup: in the ops shell, dropped to the runtime user 10001 with its own `HOME` (as the
     entrypoint does; with root's `HOME` libpq refused TLS), verified with `verify_manifest`.
   - Result `MATCH`, schema 24, audit seq 29,520.
   - Reference
     `20260928T205110Z.24.29520.f0b011472acdd032e4f3f17c2f16874d0c2c0921e195425ec08d4647b0f0849c.af1836dca1182fbe053c41d52a6b245a36384ae429710c617261599d66a0364d`.
3. **The `provision` service in migrate mode.** The pinned plan was exactly "Create service
   provision", with one variable (`MIGRATION_DATABASE_URL`) and no role password.
4. **`railway up … --service provision`, at 20:52:59 UTC.**
   - Result `MIGRATED`, schema 24 → 25; `025_jev_review_policy_v2.sql` sha256
     `67b0585e…4793`.
   - `events_after_backup` 57, event count 29,577 → 29,578 (the audited
     `CLOUD_LEDGER_MIGRATED`), `broker_requests` 0.
5. **The release, trader first.**
   - Two uploads hit the Mac's intermittent TLS "BadRecordMac" / broken pipe and succeeded on
     retry; jobs went through first time.
   - `TRADER SERVING` on `d60963e` at 20:55:27 UTC, `lease_wait_attempts=0`.
   - Status: schema 25, `jev_breaker.policy_version` V1, and ops `alarms=NONE`.
6. **Cleanup.** The provision service (the admin connection) was deleted with a pinned plan,
   "Delete service provision" only; the plan was up to date after.
7. **V2 switch.** `bb0e6f9` sets the deploy example's `JEV_REVIEW_POLICY_JSON` to V2: one line,
   version plus 8 s question and batch budgets (railway spec and cloud config tests: 51
   passed).
   - The pinned plan was exactly the trader's `JEV_REVIEW_POLICY_JSON`. After the apply, the
     trader served again at 20:57:44 UTC.
   - Status: `jev_breaker.policy_version` `JEV_LIVE_REVIEW_POLICY_V2`, `CLOSED`, epoch 0 (V2's
     own scope).
   - The watchdog showed `STATUS_UNAVAILABLE` for one tick during the restart, then `NONE` at
     20:58:21 UTC.
   - The reconciliation at 20:59:16 UTC was clean (the open GRT position and its stop-limit).

Full suite for the migration branch: 4,491 passed, 1 skipped (at its own base). After the rebase
onto `236ae13`, the overlapping suites passed: 340.

### 2026-09-28 evening (New York): a missed research run, the fee-import record, storage

Six-day operation (owner mandate, 2026-09-28). No trading rule changes.

1. **The 17:07 research timer did not fire.** The session was busy at 17:07 New York, and a
   session timer that finds the session busy is skipped, not delayed. The 18:00 slot was run by
   hand at 17:41 (`INTRADAY_V2`, 12 picks, 21 skipped, HTTP 202); Jev selected 10 of them and
   the 9 superseded setups retired. The day watch now also reads the latest `RESEARCH_STARTED`
   run slot: from :25 of each odd New York hour (:55 before the 08:00 daily run) it reports
   `RESEARCH MISSING` for a next even slot with no report.
2. **Jev V2, first hours.** 18:57–20:52 UTC under V1: 118 calls, 0 failures (p99 3.6 s).
   20:57–21:10 UTC under V2: 13 calls, 0 failures. No provider burst yet on either side.
   Month-to-date Jev spend $0.09 of the $50 guard.
3. **Storage, measured 21:50 UTC.**
   - The database is 179 MB (`trade_events` 111 MB, `managed_events` 43 MB).
   - With one open trade (GRT) and ten watched setups, the audit chain grew 15 MB in 3 hours
     (5,865 rows). It mirrors every row of `managed_events`, `jev_requests`,
     `review_worker_events`, `ai_decisions` and the review tables, and stores each mirrored row
     twice (`payload_json` and the `event_body` text).
   - A per-minute maintenance review costs about 80 KB in all, so each open trade adds about
     100 MB a day. The 5 GB volume holds well over the six days at today's rate; the day watch
     and the morning report track it.
4. **Fee import recorded every run (fixed, `e992ac9`).**
   - `ALPACA_FEE_BACKFILL_COMPLETED` was appended by every 30 s run: 361 in 3 hours, 20% of
     the managed events, each mirrored into the audit chain.
   - Now a run is recorded only when its counts differ from the last recorded run's, or 30
     minutes (database time) after it, which keeps the window's anchor moving (window at most
     1 h 30 min). New test in `tests/test_managed_fees.py`.
   - Full suite in a clean worktree: 4,527 passed, 1 skipped. In the main checkout 9
     `test_cloud_runtime.py` tests refuse with `DOTENV_PRESENT`, by design: the owner's `.env`
     sits in the checkout the package is installed from. Full-suite gates run in worktrees.
   - Deployed 22:13 UTC to trader, ops and jobs (`TRADER SERVING` 22:13:13,
     `lease_wait_attempts=0`). The watchdog showed `RELEASE_CODE_MISMATCH` for one tick between
     the trader and ops uploads, then `NONE` at 22:13:53.
   - Reconciliations clean every 30 s after the restart; all 10 watched setups were held and
     resumed (`GAP_RESUME_PENDING`/`GAP_RESUMED` 10 each). Fee records: 26 in the 13 minutes
     before the deploy, none after.
5. **Why few entries fill.** In the four hours to 21:40 UTC only GRT filled: its entry was
   0.83% below the price, while the 16:00 run's median entry sat 3.3% below (the 18:00 run's
   2.3%). The kit already takes the nearest qualifying held low. What binds is rule A's stop
   (0.4% under the window's lowest low), often closer than the app's 2% minimum on 1-hour bars.
   A research-kit profile that sets such a stop at exactly the 2% minimum (still under every
   cited low) qualified 26 of 33 coins instead of 12 on the 18:00 run's data, 13 within 1.5% of
   the price instead of 4. It is being built as `INTRADAY_V3` in the kit; the app's rules are
   unchanged.

### 2026-09-28 — Research kit `INTRADAY_V3` for the 2-hourly runs

A research-method change for the kit only (`research_agent/`); no app rule changes. The app's
own minimums (a stop at least 2% under the max entry, at least 2R) bind every profile.
- **Why.** Few intraday entries filled (the evening entry above): `INTRADAY_V2`'s rule A drops
  a near entry when 0.4% under the window's lowest low is closer than 2% to the max entry.
- **The profile** (`--profile intraday`; V2 stays as `intraday-v2`, V1 as `intraday-v1`):
  - rule A's stop is floored at the 2% minimum (max entry x 0.98, rounded down to the price
    increment) when the 0.4% stop would be closer. The floor is lower than that stop, so it is
    still under every cited low. `LevelSetup.stop_floored` records it; rule B is never
    floored;
  - at the pick limit `build` keeps the entries nearest the mid first, then the higher
    reward:risk (`pick_order` "proximity");
  - a floored pick says so in its structure note, `why_these_levels` and
    `level_references.stop` ("2% under the max entry (the app's minimum stop distance), below
    … the lowest low of all N cited bars"). Unfloored picks keep the old wording word for word.
- **Dry run** on the saved data of the 18:00 run (offline, nothing sent):
  - 26 of 33 coins with a setup (V2: 12), 22 of them floored; 13 within 1.5% of the price
    (V2: 4); median entry 1.65% below the mid (V2: 2.30%);
  - the 12 picks: all within 1.5% (median 1.14%), 10 floored, stops 2.00–4.57%, the lowest
    reward:risk 2.02; `validate` 12 ok.
  - V2 rebuilt with the new code on the same data: `levels.json` byte-identical and the same
    12 picks.
- **Agent version** `claude-as-muse-intraday-v3-09.28`: the report does not carry the profile,
  so the version is what separates V3 picks from V2 ones in the learning loop.
- **Tests:** full suite 4,541 passed, 1 skipped (at `21f8a17`, in its worktree). On the work
  branch after the fee fix: research kit and safety 352 passed, hygiene and release 16.
- **Guidelines.** `MUSE_RESEARCH_GUIDELINES` (V4 text, served in V5/V6) describe the stop as
  0.4% under the lowest cited low. V3's floored stops sit further below it, and each pick
  states its stop exactly; the guidelines' text is unchanged.
- **Live from the 20:00 slot** (the timer at 19:07 New York, replaced at 18:37 as `7f762ef5`).
  No Jev or broker evidence yet: stops at the 2% minimum and targets near 2R are what the
  first runs test.

### 2026-09-28 — `INTRADAY_V3` live once, then reverted

- **The first V3 run** (the 20:00 slot, submitted by hand at 19:13 New York because the 19:07
  timer had not fired): 26 of 33 coins had a setup. The 12 picks had a median entry 1.35%
  below the price, 8 within 1.5%, and 10 of them had floored stops.
- **Jev's top-K selection** (23:14 UTC): 5 ranked, 7 vetoed.
  - All 7 vetoes were `LEVELS_SUPPORTED_BY_BARS_NO`, and all 7 picks had floored stops.
  - Of the 5 ranked, 4 carried `LEVELS_SUPPORTED_BY_BARS_NO_UNSURE` (a 10-point penalty). The
    only clean pass (BONK) had a 0.4% stop.
  - Every earlier ranking that day (08:00 twice, 14:00, 16:00, 18:00) had no vetoes.
- **Why.** The question asks whether the cited bars show the entry, stop and target as the pick
  describes them. A floored stop is computed from the max entry, not shown by any bar, so the
  veto is a fair reading. Rewording picks to pass the check would defeat it.
- **Bar-anchored alternatives on the same data.**
  - The nearest entry across rules A and B, with windows up to 40 bars: 14–20 setups, 2
    within 1.5% of the price, median 2.5–2.8%.
  - Windows of 36–40 bars put 3 picks over the 11 KB dossier budget.
  - With a stop at least 2% under the max entry and a cited target at least 2R above, intraday
    structure rarely allows an entry within about 1.5% of the price.
- **Reverted at 19:18 New York** (`61013b8`, `e253330`).
  - `--profile intraday` is `INTRADAY_V2` again, with its agent version
    `claude-as-muse-intraday-v1-09.28`. V3 stays in history (`686eb07`).
  - Kit, safety, hygiene and release tests: 353 passed. The 2-hourly timer is `90372506`.
  - The five V3 setups Jev selected stay valid until the 22:00 slot supersedes them.
- **More fills need an owner decision** on a named version (NEXT-BUILD-PLAN, "Entry distance"),
  not a research-method change.
