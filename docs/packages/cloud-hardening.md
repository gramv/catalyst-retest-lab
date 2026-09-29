# Package cloud-hardening — the review's findings fixed before the first deploy

**PAPER TRADING — SIMULATED. Not real money. LOCAL FIXTURE EVIDENCE ONLY: nothing is deployed.**

Branch `pkg/2026-09-28-cloud-hardening`, from integration-6 at `c868ca2` (package cloud merged
with experiment-page, muse-connection and session-fixes; schema 24). An independent read-only
review of package cloud found no secret leak, no live-trading path and no way to run two cloud
traders; everything here is hardening it asked for. No migration, no rule or risk change, no new
broker call: every broker POST/DELETE/PATCH keeps its exact one-use five-second authorization.
This file carries the text the coordinator merges into `docs/PHASES.md`.

## What changed, finding by finding

1. **`/health` on the public trader domain opened a database session per request** (as
   `catalyst_risk`, TLS + SCRAM, on a thread-pool thread), so a flood could take the
   connections and threads that status, Muse, the watchdog and the trader's own authorization
   and protection writes need. In railway mode the trader's `/health` now answers liveness from
   memory, on the event loop: no thread, no database session
   (`create_managed_app(health_check_database=False)`, passed by the cloud trader's default
   builder and the local proof). Chosen over a cached, single-flight probe because nothing on
   Railway reads the database answer: Railway's healthcheck passes during `STARTING` by design,
   and readiness is the token-protected status. The Mac app's loopback `/health` is unchanged.
2. **`MANAGED_MANAGEMENT_REVIEWS` was not validated by the profile.** `DISABLE`, `enabled` or
   `TRUE` passed the configuration and then failed every startup, ten minutes at a time, for up
   to about 17 hours of restarts. `managed_ops.environment_findings` (shared by the cloud profile
   and the Mac preflight) now requires exactly `ENABLED` or `DISABLED`: the trader refuses at
   once, exit 2, `CLOUD_ENGINE_SETTING_INVALID MANAGED_MANAGEMENT_REVIEWS`.
3. **`catalyst_backup` was a member of `pg_read_all_data`, which reads `pg_authid`'s SCRAM
   verifiers** (reproduced in a test). It now has USAGE on `lab`, SELECT on `lab`'s tables and
   sequences, and `lab_owner`'s default privileges for later ones: what `pg_dump` of the ledger
   reads (every relation of the ledger is in `lab`; a test asserts it). The provisioner refuses
   any login role that can read `pg_authid` (`PROVISION_ROLE_TOO_BROAD`). Backup, manifest
   verification and the local restore drill pass with the narrow role; the role is denied
   `pg_authid`.
4. **`cloud_entry` chowned `CATALYST_STATE_DIR` as root before checking it was on the volume.**
   The root branch now requires `RAILWAY_VOLUME_MOUNT_PATH` and a state directory equal to it or
   inside it, reached through real directories only, before it creates or chowns anything
   (`CLOUD_VOLUME_REQUIRED`, `CLOUD_STATE_DIR_NOT_ON_VOLUME`, `CLOUD_STATE_DIR_NOT_A_DIRECTORY`,
   `CLOUD_VOLUME_UNAVAILABLE`): a symlink planted on the volume cannot lead root off it.
5. **`ledger-retire` could succeed while the Mac app still ran.** It now proves the Mac executor
   is stopped, on the ledger's own socket (`localdb.retire_stopped_ledger`): the cluster must be
   running (`LEDGER_CLUSTER_NOT_RUNNING`; it never starts one), the lock every executor holds
   for its whole life must be free (`LEDGER_EXECUTOR_RUNNING`), no session of the app's logins
   `catalyst_risk`/`catalyst_jev` may exist (`LEDGER_APP_SESSIONS_PRESENT`: an app starting),
   and nothing may be bound to the app's port, 8780 by default (`LEDGER_APP_PORT_IN_USE`, a
   bind probe: no connection). It holds the lock while it writes the marker, so no executor can
   start in between, and checks it still holds it afterwards (`LEDGER_EXECUTOR_LOCK_LOST`).
   Chosen because the lock is the executors' own mutual exclusion; the sessions and port checks
   cover an app that is starting or serving without it. The one gap no outside check can see, an
   app past the launcher's check but between connections, is closed in the app: the launcher
   passes the marker (`CATALYST_LEDGER_MARKER`) and `ManagedRuntime.acquire_ownership` re-reads it
   once the lease is held, releasing the lease and stopping with `LEDGER_RETIRED` (an unreadable
   marker fails closed; the cloud trader has no marker and reads nothing).
6. **No cloud path cleared an operator-only protection latch.** New owner command in the
   trader's shell: `railway ssh --service trader -- python -m catalyst_lab.cloud_runtime
   clear-protection-latch --reason "..."`, over the trader's own `MANAGED_DATABASE_URL` (the
   operator role has no function for it, and adding one would need a migration). It is
   `managed_ops.clear_protection_latch` itself: reason required (10 to 500 characters), one
   audited `OPERATOR_PROTECTION_LATCH_CLEARED` event, idempotent, now recording its origin
   (`operator`: `RAILWAY_TRADER_SHELL`; the Mac CLI keeps `LOCAL_OWNER_CLI`). A durable
   execution halt still needs `operator release-halt` on ops.
7. **The guide's quick switch to `DISABLED` would have turned the suite red.** The spec test
   now accepts `ENABLED` or `DISABLED` for that one key (both evaluated, both passing the cloud
   profile); everything else stays pinned.
8. **`start_trader_app`'s docstring** said the lease wait did not count against the ten-minute
   budget. It does: the wait itself is unbounded, but a different failure after a long wait ends
   startup at once (exit 1) and ON_FAILURE restarts it with a fresh budget. Docstring corrected;
   behavior unchanged.

Guide (`docs/RAILWAY-DEPLOYMENT.md`):

- Section 9's quick switch says plainly that no trader runs during a redeploy, and requires,
  before it, `positions` showing no pending `stop_replace` and no `HALTED` protection and Alpaca
  showing full-quantity stops at each current stop; otherwise `operator flatten-all` first.
- 5.10: `operator pause` before `flatten-all`; stop the app and every agent that would restart
  it; keep the ledger cluster up; retire through the new checks.
- 7.5: a trader database password is rotated only while paused and flat, and the trader is
  redeployed at once after `rotate-passwords`: in between, every new trader database session is
  refused (fail closed, but no authorization, protection write or reconciliation; with a
  position open, a durable persistent-failure halt after 30 seconds).
- 5.4, 5.12 and section 9 agree: Muse gets its token and the domain only after the first-day
  checks (status, streams, a clean reconciliation).
- 7.1 (the public `/health` is liveness from memory), 7.2 (the trader-shell latch clear), 5.8
  and section 11 (the narrow backup role; the new evidence). Section 2.4 is package
  experiment-page's, kept.
- `docs/MUSE-CONNECTION.md`: the secrets script's default agent ID is `muse` (it said `claude`).

## Files

- `src/catalyst_lab/managed_service.py`, `managed_app.py`, `cloud_runtime.py` (health mode; the
  owner's `clear-protection-latch`; docstring), `managed_ops.py` (the reviews switch; the latch
  clear's origin; the launcher passes the ledger marker to the app), `managed_runtime.py` (the
  marker re-read after the lease), `cloud_provision.py` (backup grants, `pg_authid` check),
  `cloud_entry.py` (volume check), `localdb.py` and `cli.py` (`retire_stopped_ledger`,
  `--app-port`).
- `scripts/cloud_local_proof.py` (the Railway trader's health mode; the backup role's step).
- Tests: `test_cloud_surface.py`, `test_cloud_config.py`, `test_cloud_runtime.py`,
  `test_cloud_provision.py`, `test_cloud_backup.py`, `test_cloud_entry.py`,
  `test_ledger_retire.py`, `test_managed_runtime_latches.py`, `test_railway_spec.py`.
- `docs/RAILWAY-DEPLOYMENT.md`, `docs/MUSE-CONNECTION.md`, this file,
  `artifacts/cloud-hardening-2026-09-28/`.

## Validation

- Targeted (worktree venv `XDG_DATA_HOME=~/.local/share/catalyst-wt-cloud-hardening`): the cloud,
  retire, spec, latch, managed ops/app/service/engineering, ledger backup, localdb guard,
  research-agent, Muse-connection and repository-hygiene tests: 536 passed, 1 skipped (the
  pre-024 `catalyst_public` skip test); `ruff check src tests` clean.
- Local proof on `c1055f5` (`artifacts/cloud-hardening-2026-09-28/`): `COMPLETED`, schema 24,
  with the backup role denied `pg_authid` and its backup verified and drilled.
- Full suite (shared guard script, after merging `work/2026-09-24-product-plan` at `2ea399f`):
  **3,983 passed, 1 skipped** (exit 0, 12 min 53 s) on `88df386`; the skip is the pre-024
  `catalyst_public` test. Only this record changed after it.

## PHASES entry (paste as written)

### 2026-09-28 — Cloud hardening before the first deploy: public health without the database, the reviews switch validated, a narrow backup role, the volume checked before root touches it, a proven Mac retirement, the latch clear in the cloud (package cloud-hardening) (LOCAL FIXTURE EVIDENCE ONLY, NOT DEPLOYED)

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

## Open items

- The static public pages (`/`, `/lab.js`, `/lab.css`, `/results`, `/results.js`) still run on
  the thread pool. They hold no data and touch no database, so a flood costs only brief thread
  time, not sessions; making them async would change the Mac app too, so it was left.
- The Mac app's marker re-read (after the lease) runs only for apps started by the private
  launcher, which passes `CATALYST_LEDGER_MARKER`; an app started by hand without it is covered
  by `ledger-retire`'s own checks alone.
- Everything Railway-only in `docs/packages/cloud.md` remains open.
