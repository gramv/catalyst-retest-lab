# Package: unattended safety (plan phase 0, 2026-09-26)

Branch `pkg/2026-09-26-unattended-safety`, from `work/2026-09-24-product-plan` at `80eb952`.
Scope: the ways the managed app could stop protecting positions, or stop trading, without anyone
noticing (plan `docs/CRYPTO-AGENT-LOOP.md`, phase 0; trades execute on Alpaca Paper, owner
decision D1). Paper only: no live-trading path, no new mutation path, and every broker
POST/DELETE/PATCH still needs its exact one-use five-second authorization. No schema change
and no migration: new state fields live in the existing `STATE` event body, and new event kinds
are ordinary appended `lab.managed_events` rows.

This file carries the PHASES entry and the CONTRACT-RESOLUTIONS text for the coordinator to
merge; this package does not edit `docs/PHASES.md` or `docs/CONTRACT-RESOLUTIONS.md`.

## PHASES entry

### 2026-09-26 — Unattended safety: lease loss, refused closes, halt and Muse alarms (FIXTURE EVIDENCE ONLY)

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

## CONTRACT-RESOLUTIONS text

### 2026-09-26 — Unattended safety: refused-close retries, lease loss and watchdog alarms (plan phase 0)

Operational safety rules for the managed engine on the one Alpaca Paper account. They change
no strategy, trigger, size or exit rule and no named rule version; V1 and every recorded rule
stay unchanged. Paper only; every broker mutation keeps its exact one-use five-second
authorization.

1. **Refused closes.** After the n-th consecutive broker refusal of a managed setup's close
   (market sell), crypto or stock, in or out of an operator flatten, the next close may be
   authorized no earlier than `min(1 s × 2^(n−1), 60 s)` after that refusal: 1, 2, 4, 8, 16,
   32, then every 60 s, without limit while the setup is working (`EXIT_RETRY_BASE_SECONDS =
   1`, `EXIT_RETRY_CAP_SECONDS = 60`). Each refusal records `EXIT_REFUSED` and exactly one new
   state revision (`exit_refusals`, `exit_retry_after`, `exit_retry_of`), so each retry has a
   fresh client order ID and its own one-use five-second authorization. A close accepted by
   the broker ends the streak. A close ID already used by an approved POST is never re-sent;
   one new revision is recorded instead.
2. **Refused-close alarm.** N = 5 (`EXIT_REFUSAL_ALARM_THRESHOLD`). The fifth consecutive
   refusal of a working setup appends the durable `EXIT_REFUSAL_ALARM` event. While any working
   setup has five or more consecutive refusals, the runtime status lists it in
   `exit_refusal_alarms` and the watchdog raises `EXIT_REFUSED_REPEATEDLY`, plus
   `EXIT_REFUSED_REPEATEDLY_CRYPTO` or `EXIT_REFUSED_REPEATEDLY_US_STOCKS`. Retries continue
   at the 60 s cap; the alarm asks the owner to look, it stops nothing.
3. **Lost executor lease.** Losing the account executor lease is terminal for the process: the
   runtime appends `RUNTIME_EXECUTOR_OWNERSHIP_LOST`, sends nothing more, stops every loop and
   exits with status 75 (`EXECUTOR_OWNERSHIP_LOST_EXIT_CODE`, sysexits `EX_TEMPFAIL`), after a
   graceful shutdown or at the latest after 30 s (`FATAL_EXIT_GRACE_SECONDS`). The lease is
   never re-acquired in-process; only a new process may take it, through startup
   reconciliation.
4. **Watchdog codes.** `EXECUTION_HALT_ACTIVE` plus `HALT_<KIND>` for every active halt kind
   (unreleased execution halts and today's `DAILY_RISK_HALT`; a kind that cannot form a code of
   at most 64 characters becomes `HALT_KIND_UNRECOGNIZED`), `EXECUTION_HALT_STATUS_UNAVAILABLE`
   when the halts cannot be read, and the refused-close codes above. `MUSE_WORK_FAILED` from
   the provider-job table is raised only by a current, stuck job; failed jobs are counted in
   the watchdog result (`muse_provider_jobs`) without an alarm.

## Deviations

- **The first retry waits one second, also during an operator flatten.** The flatten's own
  crypto re-arm retried on the very next tick without backoff. It is superseded by the common
  retry, which records the new revision at refusal time; the existing flatten test now
  advances its fixture clock by 1 s before the retried close and reads `exit_retry_of`
  (renamed `..._is_retried_after_its_backoff_...`). Historical `flatten_exit_retry_of` fields
  stay as recorded.
- **Stock closes: one revision per exit-reason change, not per tick.** Needed so that each
  retry has exactly one new revision. An accepted stock close still working on the next tick
  is still cancelled and re-sent, as before (market orders fill at once in regular hours); the
  re-send now waits one tick for a new revision instead of reusing a spent ID (see open items).
- **Process exit needed the entry points.** Terminating the process required
  `managed_app.main` (it now builds `uvicorn.Server` itself instead of `uvicorn.run`, keeping
  the same bind, logging and startup-failure status 3) and `managed_runtime.main`. The app CLI
  test was adapted.
- **Muse test fixture.** The watchdog tests' hand-made spool lacked `provider_jobs.id` and the
  `state` table; they now use `MuseSpool`'s own schema. The watchdog result gained
  `muse_provider_jobs`.
- **`ready()` unchanged.** Halts are reported and alarmed, not added to `ready()`: entries are
  already refused by the ledger, and a transient-refusal admission test depends on it.

## Open items and questions for the owner

1. **Crash-loop breaker policy** (pending owner decision, documented in the runbook): keep
   stop-and-alert (exit 0 after the fifth start in ten minutes, launchd stops restarting) or
   retry with a growing delay. Each lease-loss restart counts as a start, so a PostgreSQL that
   flaps five times in ten minutes trips the breaker and leaves positions to their broker-side
   stop-limits and brackets.
2. **Refused cancels are not backed off.** A refused `DELETE` (a stop-limit cancel before a
   crypto close, a bracket-leg cancel before a stock close) is still re-authorized every tick.
   Same shape of fix is possible; not in this package's scope.
3. **Unknown close responses.** A close whose response was lost (`BROKER_UNKNOWN`, 5xx,
   timeout) is looked up by client ID and never re-sent; if the broker never received it the
   setup stays in `RECOVERY_PENDING` with no app-side close. Deliberate (no duplicate orders),
   but it has no alarm.
4. **A working stock close is cancelled and re-sent on the next tick** if it has not filled
   (existing behaviour, now a three-tick cycle). Should the controller await its own close, as
   the crypto controller does?
5. **The refused-close alarm clears when a close is accepted**, before it fills. Fine for
   market orders; say if it should hold until the fill.
6. **Lease-loss evidence during a database outage.** When PostgreSQL itself is down, the
   `RUNTIME_EXECUTOR_OWNERSHIP_LOST` event cannot be written; only the component log line
   remains, and the launcher's database wait then delays the restart.
7. **Unproven here:** launchd restarting on status 75, uvicorn's shutdown timing, and Alpaca's
   refusal behaviour for closes. The first supervised session should include a deliberate
   ledger restart (lease loss) and a check that the watchdog shows `EXECUTION_HALT_ACTIVE`.

## Validation

Fixture evidence only (disposable PostgreSQL, the fake paper venue). Targeted files only, each
in its own pytest run, one at a time, in this worktree on 2026-09-26, with free disk checked
before each run (2.5 to 2.8 GiB). The full suite was not run here; the coordinator runs it once
on the integrated branch.

| File | Result |
| --- | --- |
| `tests/test_unattended_safety.py` (new) | 15 passed |
| `tests/test_operator_flatten.py` (changed) | 15 passed |
| `tests/test_managed_ops.py` (changed) | 45 passed |
| `tests/test_managed_app.py` (changed) | 14 passed |
| `tests/test_bracket_protection.py` | 28 passed |
| `tests/test_managed_hardening.py` | 9 passed |
| `tests/test_managed_failure_isolation.py` | 16 passed |
| `tests/test_exit_attribution.py` | 13 passed |
| `tests/test_managed_account_safety.py` | 5 passed |
| `tests/test_audit_volume.py` | 6 passed |
| `tests/test_managed_runtime_latches.py` | 24 passed |
| `tests/test_complete_managed_cycle.py` | 4 passed |

Total 194 passed, 0 failed; no fix was needed. `ruff check src tests`: clean. Also exercising
the changed code but left to the full suite: `tests/test_managed_execution.py`,
`tests/test_managed_runtime.py`, `tests/test_managed_service.py`,
`tests/test_legacy_executor_ownership.py` and `tests/test_managed_engineering.py`.
