# Package cloud — the managed engine as a 24/7 Railway service (prepared, not deployed)

**PAPER TRADING — SIMULATED. Not real money. LOCAL FIXTURE EVIDENCE ONLY: nothing is deployed.**

Branch `pkg/2026-09-27-cloud`, from `work/2026-09-24-product-plan` at `6aa1ae6` (schema 23),
with the work branch at `7443686` (package answer-rules) merged in. Owner request 2026-09-27:
"can we deploy it somewhere and present as an experiment"; Railway chosen 2026-09-19. The public
read-only page is package experiment-page, built in parallel. This file carries the text the
coordinator merges into `docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md`; neither file is
edited on this branch. No migration, no rule change, no broker order path.

## Summary

- **Railway profile** (`CATALYST_ENVIRONMENT=railway`, new `cloud_config.py`, `cloud_runtime.py`,
  `cloud_entry.py`): configuration from variables only, validated by the private config v2
  engine checks (`managed_ops.environment_findings`, extracted from `preflight` unchanged).
  Refuses a present `.env`, `TYPESAFE_ENV_FILE`, missing secrets, placeholders, unknown
  engine-looking variables, non-paper keys, DSNs of the wrong role or without TLS, state off
  the Railway volume and an unsealed image. Two services from one image: **trader** (today's
  app) and **ops** (the watchdog's alarm rules, hourly audit checkpoints, daily verified pg_dump
  backups on its volume, alerts through notify with the ping URL from a secret variable).
- **Exactly one executor**: one replica; a volume on the trader, so Railway stops the old
  deployment before starting the new one; overlap 0 s, draining 30 s; and the existing DB
  executor lease, which a railway-mode trader waits for (answering `/health`) instead of
  exiting. Exit 75 on lease loss (ON_FAILURE restarts it), exit 0 from the crash-loop breaker,
  whose state lives on the volume.
- **Provisioner** (`python -m catalyst_lab.cloud_provision initial|rotate-passwords`), run inside
  Railway as a temporary service: empty database only; every migration as a NOLOGIN superuser
  `lab_owner`; SCRAM verifiers with statement logging off; LOGIN only for the cloud roles;
  `catalyst_backup` (read-only); the audited `CLOUD_LEDGER_PROVISIONED` identity, which the
  trader requires; `catalyst_public` provisioned once migration 024 exists, skipped with a note
  before.
- **Retiring the Mac ledger**: `catalyst-lab ledger-retire --ledger-dir <dir> --reason
  MOVED_TO_CLOUD` (marker only; atomic; keeps the old content). App and Muse then refuse
  `LEDGER_RETIRED`.
- **Fresh ledger vs an account with history**: already correct in the code; now proven.
- **Release identity**: `scripts/cloud_release.py` stages a clean commit with
  `release-source.json`; the image build seals it; status reports `release_commit`.
- **Deployment definition**: Railway Infrastructure as Code, `.railway/railway.ts` (Config as Code
  is deprecated for new services); `Dockerfile.managed`; the owner's secrets script
  `scripts/cloud_secrets.py`; the owner's guide `docs/RAILWAY-DEPLOYMENT.md`.
- **Muse and Jev** (owner decisions relayed 2026-09-27): Muse does all research and is the only
  outside caller, with exactly one agent credential, `{"muse": <token>}`; `MANAGED_API_TOKEN` is
  generated, sealed and never issued. Jev stays inside the trader, and the cloud sets
  `MANAGED_MANAGEMENT_REVIEWS=ENABLED` (Jev monitors every trade every minute).
- **Research kit hardening**: HTTPS required off loopback; owner-only token files.
- **Local proof**: `scripts/cloud_local_proof.py`, evidence in `artifacts/cloud-2026-09-27/`.

## PHASES entry (paste as written)

### 2026-09-27 — The managed engine on Railway: runtime profile, provisioning, one executor, retiring the Mac ledger (package cloud) (PREPARED, LOCAL FIXTURE EVIDENCE ONLY, NOT DEPLOYED)

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

## CONTRACT-RESOLUTIONS text (paste as written)

### 2026-09-27 — Running the managed engine on Railway is deployment, not a rule change (package cloud)

Moving the managed engine from the owner's Mac to Railway changes where and how the process is
supervised, provisioned and observed. It changes no trading rule: admission, selection, the
trigger, sizing, account risk, protection, maintenance, the 24-hour review, exits, the daily
halt, the operator controls and every broker call's exact one-use five-second authorization are
the same code with the same settings: the deploy example's `environment` map, with
`CATALYST_ENVIRONMENT=railway`, `MANAGED_HTTP_PORT=8080`, the Jev breaker's label
`JEV_CREDENTIAL_SLOT=paper-railway` and `MANAGED_MANAGEMENT_REVIEWS=ENABLED` as the only
overrides. The last one is an owner decision (Jev monitors every trade every minute), made with
the existing staging switch of plan 0.10: it lets Jev take part exactly as the already recorded
versions define (`CRYPTO_MAINTENANCE_V2` and `CRYPTO_24H_REVIEW_V2` of package answer-rules,
`EARLY_EXIT_AGREEMENT_V1`), and `DISABLED` is those versions' recorded behavior without Jev.
**No named version is recorded.** Muse is the only research agent and outside caller in the
cloud, with the one agent credential `{"muse": <token>}`; the legacy `MANAGED_API_TOKEN` is
issued to no one. That is access, not a rule.

Behavior that differs by environment is limited to supervision and operations, and none of it
decides an entry, an exit, a size or a risk limit:

- In railway mode a trader that finds the account executor lease held waits for it (answering
  `/health` with `phase: STARTING`) instead of failing its startup; the lease itself, its
  re-check before every send and exit 75 on its loss are unchanged.
- The railway trader requires the audited `CLOUD_LEDGER_PROVISIONED` identity instead of a
  `LEDGER.json` marker, binds `[::]:$PORT` only when running on Railway, and keeps its
  crash-loop state on the Railway volume.
- The cloud's database roles: only `catalyst_risk`, `catalyst_jev`, `catalyst_app`,
  `catalyst_operator`, `catalyst_backup` (new, read-only, provisioner-created) and
  `catalyst_public` can log in; `lab_owner` is a NOLOGIN superuser; the other lab roles are
  NOLOGIN. The grants of every role are those of the migrations.
- A retired Mac ledger (`LEDGER.json` with `retired_at`, `retired_reason`) refuses its app and
  Muse (`LEDGER_RETIRED`) and its migration (`OWNER_LEDGER_RETIRED`).

## Files

New:

- `src/catalyst_lab/cloud_config.py`: the Railway profile's validation (trader, ops, notify).
- `src/catalyst_lab/cloud_runtime.py`: the railway trader (starting responder, lease wait,
  socket handover), the ops loop, the owner's `status` and `positions` commands.
- `src/catalyst_lab/cloud_entry.py`: the container entrypoint (volume handover, permanent drop).
- `src/catalyst_lab/cloud_provision.py`: the provisioner (`initial`, `rotate-passwords`).
- `src/catalyst_lab/cloud_release.py`: release sealing and verification.
- `scripts/cloud_release.py` (stage a release), `scripts/cloud_secrets.py` (owner secrets),
  `scripts/cloud_local_proof.py` (the local proof).
- `.railway/railway.ts`, `.railway/package.json` (the IaC definition, the pinned SDK).
- `Dockerfile.managed`, `deploy/requirements.managed.txt`.
- `docs/RAILWAY-DEPLOYMENT.md`, this file, `artifacts/cloud-2026-09-27/`.
- Tests: `tests/cloud_fixtures.py`, `tests/test_cloud_backup.py`, `test_cloud_config.py`,
  `test_cloud_entry.py`, `test_cloud_fresh_ledger.py`, `test_cloud_provision.py`,
  `test_cloud_release.py`, `test_cloud_runtime.py`, `test_cloud_secrets.py`,
  `test_cloud_surface.py`, `test_cloud_tls.py`, `test_ledger_retire.py`, `test_railway_spec.py`,
  `test_research_agent_cloud.py`.

Changed:

- `src/catalyst_lab/managed_ops.py`: `environment_findings` extracted from `preflight`
  (unchanged behavior); app and Muse refuse a retired ledger; preflight reports it.
- `src/catalyst_lab/localdb.py`: `ledger_retirement`, `retire_ledger`; `migrate_ledger` refuses
  a retired ledger.
- `src/catalyst_lab/cli.py`: `catalyst-lab ledger-retire`.
- `src/catalyst_lab/ledger_ops.py`: `backup_database` (password DSN) sharing the backup body;
  the drill drops the bootstrap owner's ALTER line.
- `src/catalyst_lab/notify.py`: the environment-URL section and `check_ping_url`.
- `src/catalyst_lab/managed_app.py`: `serve()` shared by `main()` (unchanged loopback) and the
  railway trader.
- `scripts/run_managed_private.py`: shows this project's refusal codes.
- `research_agent/submit.py`, `context.py`, `token.py`, `DAILY_PROCEDURE.md`, `DAILY_PROMPT.md`;
  `tests/test_research_agent_cli.py`, `test_research_agent_token.py` (token files made 0600).
- `docs/OPERATIONS-RUNBOOK.md`: pointer to the Railway guide; the drill's owner-role lines; the
  research kit's HTTPS rule.
- `.dockerignore`, `.railwayignore`, `.gitignore`.

Not touched: the root `railway.json`, `Dockerfile.review`, `deploy/railway.review-worker.json`
(the legacy review surface), every file owned by packages experiment-page and answer-rules, the
migrations.

## Validation

Targeted (worktree venv `XDG_DATA_HOME=~/.local/share/catalyst-wt-cloud`):

```sh
./run pytest -q tests/test_cloud_*.py tests/test_ledger_retire.py tests/test_railway_spec.py \
  tests/test_research_agent_*.py tests/test_managed_ops.py tests/test_managed_app.py tests/test_notify.py
./run pytest -q tests/test_ledger_backup.py tests/test_localdb_guard.py tests/test_owner_migration_rehearsal.py
./run ruff check src tests
```

Results: the package's tests pass (see the full suite below); ruff clean on `src tests`.

Full suite (shared guard script, after merging `work/2026-09-24-product-plan` at `7443686`):
**3,831 passed** (exit 0, 12 min 56 s) on `03562cc`. The run before the HOME fix, on `fe51f8e`,
gave 3,823 passed; the eight new tests are the TLS test and the entrypoint's root branch.

The final commit (Muse's credential, `MANAGED_MANAGEMENT_REVIEWS=ENABLED`, the owner's
`positions` command, the guide's first-day additions) was checked, as the coordinator directed,
with the targeted tests and ruff only; the coordinator's integration run covers the full suite.
Targeted: `tests/test_cloud_*.py`, `test_ledger_retire.py`, `test_railway_spec.py`,
`test_research_agent_*.py`, `test_repository_hygiene.py`: 258 passed; ruff clean on `src tests`
and the three cloud scripts. The changed `.railway/railway.ts` was evaluated again with the real
SDK 3.11.0 (the trader's `MANAGED_MANAGEMENT_REVIEWS` is the literal `ENABLED`; the resources
and variable kinds are unchanged) and type-checked (exit 0; negative control exit 2), recorded
in `railway-spec-validation.json`.

Local proof: `./run python scripts/cloud_local_proof.py run --out "$PWD/artifacts/cloud-2026-09-27"`
(recorded result `COMPLETED` on `5873ae6`; `artifacts/cloud-2026-09-27/README.md` explains every
step).

IaC validation (offline, recorded in `artifacts/cloud-2026-09-27/railway-spec-validation.json`):
the real SDK `railway@3.11.0` (tarball integrity equal to the npm registry's) evaluated
`.railway/railway.ts` under Node 26 with the CLI's evaluation shape: 4 services, the database and
2 volumes, the provision service only with `CATALYST_PROVISION`; `tsc --strict` (TypeScript
5.9.3) against the SDK's types: exit 0; a negative control (`ON_FAILURES`) is rejected.

## PostgreSQL 15–18 review (migrations and provisioner, static)

Railway's IaC `postgres()` helper deploys PostgreSQL 18; this Mac has 14, so nothing here ran
on 15–18. Reviewed by reading:

- **15, public schema.** `schema.sql`'s `REVOKE CREATE ON SCHEMA public FROM PUBLIC` is a no-op
  on 15+ (already revoked); nothing is created in `public` except `pgcrypto`, by the superuser.
- **15, locale provider.** The provisioner names `LOCALE_PROVIDER libc` from 15 on, so an
  ICU-initialized server still yields the C collation of a local ledger.
- **16, CREATEROLE/ADMIN.** Roles are created and granted by a superuser (`lab_owner` under
  `SET ROLE`); the one membership (`GRANT catalyst_app TO catalyst_risk`) keeps the default
  INHERIT and SET options; no runtime role creates or sets roles.
- **16+, pg_dumpall.** `GRANT ... WITH INHERIT TRUE GRANTED BY ...`, `SET transaction_timeout`
  (17+) and `\restrict` lines match the drill's roles allowlist. A roles script from 17+ fails on
  a 14 server (`transaction_timeout`), and `pg_restore` 14 cannot read newer archives: drilling
  a cloud backup needs PostgreSQL 18 tools on the Mac.
- **17, maintenance `search_path`.** The only expression index uses a built-in operator
  (`(body->>'cycle_id')`); no materialized view; SECURITY DEFINER functions pin
  `search_path=pg_catalog,lab`.
- **18, generated columns.** All three are `STORED` with qualified `public.digest`, so 18's
  virtual default does not apply.
- **18, MD5.** No MD5 anywhere: SCRAM verifiers only.
- **Catalogs.** `pg_roles`, `pg_locks` (advisory keys), `pg_db_role_setting`, `pg_database`
  columns used are stable through 18. Not verified: `pg_control_system()` stays executable by a
  non-superuser on 18 (it is on 14); if not, the backup fails visibly with `BACKUP_FAILED`.

## Decisions (please confirm)

1. **Secrets are generated by `scripts/cloud_secrets.py`, not Railway's `secret()`.** Railway
   documents `secret()` only for templates; the IaC CLI plans a generator variable as changed on
   every apply, which could rotate a password under a running service. Approved by the
   coordinator with the script conditions (never printed, idempotent, `--rotate`, 0600 agent
   file outside the repo and ~/Documents).
2. **DB role passwords stay unsealed** (referenced by DSN templates; Railway does not document
   sealed values inside references). The three owner keys and `MANAGED_API_TOKEN` are sealed;
   the other tokens can be. Railway does not document whether `railway variable list` still
   names a sealed variable, so the guide has the owner dry-run `cloud_secrets.py` before any
   later run and stop if a sealed token shows `GENERATE`.
3. **The container starts as root in `cloud_entry` only** (Railway volumes are root-owned);
   everything else runs as uid 10001.
4. **Bind `[::]` (dual stack), not `0.0.0.0`**, and only when `RAILWAY_ENVIRONMENT_ID` is present:
   legacy Railway environments resolve private DNS to IPv6 only.
5. **The waiting trader waits for the lease indefinitely** (health 200 `STARTING`); other startup
   failures are retried for ten minutes, then exit 1.
6. **`restartPolicyMaxRetries: 100`** (Railway's default is 10; the windowed crash-loop breaker is
   the real stop, and a small lifetime cap would stop the trader after a few lease losses over
   weeks).
7. **`catalyst_backup` is created by the provisioner, not a migration** (a cloud-only operational
   role; no schema version change, no conflict with migration 024). `lab_owner` is a NOLOGIN
   superuser that owns the schema, as on a local ledger.
8. **The restore drill drops the bootstrap owner's ALTER line** (runbook updated).
9. **`JEV_CREDENTIAL_SLOT=paper-railway`** in the cloud (a label of the Jev breaker's scope).
10. **The trader's volume exists mainly to make Railway stop before start** (it also holds the
    crash-loop state).
11. **The quick switch to DISABLED is `railway variable set ... --service trader`** (it redeploys
    the trader at once), followed by the same change in `.railway/railway.ts`, so a later apply
    does not switch it back.
12. **The owner's `positions` command** (ops shell, status token, GET-only) was added so the
    first stop replacement can be watched from the app's side as well as at Alpaca.
13. **The components' `HOME` is the runtime user's (`/nonexistent`)**, set by the entrypoint;
    without it libpq 18 fails every TLS connection (found and fixed in this package).

## Open items: proven locally vs only on Railway

Proven locally (fixtures, disposable PostgreSQL 14, mock venue): everything in the PHASES entry's
evidence list and `artifacts/cloud-2026-09-27/README.md`.

Only provable on Railway (the first deployment and the first supervised day):

- `railway config plan/apply` of `.railway/railway.ts` (the SDK evaluates and type-checks; the
  CLI's acceptance of the deploy fields the IaC prose does not list: restart policy, max
  retries, overlap, draining).
- The image build (PGDG key and package, hashed requirements, the seal step) and the root
  handover of the volume.
- Stop-before-start with a volume on a redeploy; the healthcheck during the lease wait; domain
  routing to `[::]:8080`.
- Reference resolution: `${{...}}` inside DSN literals, cross-service references, sealed values.
- `railway ssh` seeing the service variables (the owner's status and operator commands).
- PostgreSQL 18: the migrations, the provisioner, `pg_dump`/`pg_dumpall` 18 as
  `catalyst_backup`, `pg_control_system()` for a non-superuser.
- Deleting Postgres's TCP proxy and it staying deleted.
- The trader against the real Alpaca Paper account and TypeSafe: streams, reconciliation of the
  account's real history, entries, exits.

Other open items:

- **Cloud migrations.** The provisioner only provisions an empty database. A release with a new
  numbered migration cannot run against the cloud ledger until the owner has migrated it (the
  local `ledger-migrate` needs the Mac's socket and owner role). So provision from a release that
  already holds package experiment-page's migration 024: its `catalyst_public` login (and
  `PUBLIC_DATABASE_PASSWORD`, which `scripts/cloud_secrets.py` already generates) then comes with
  the provisioning. *Resolved 2026-09-28 by package cloud-migrate:* the guarded step is
  `cloud_provision migrate`, run as the provision service with a fresh ops backup named first
  (RAILWAY-DEPLOYMENT.md 7.8); the first use is migration 025.
- **Drilling cloud backups on the Mac** needs PostgreSQL 18 tools (the coordinator is asking the
  owner).
- **Experiment section** of `docs/RAILWAY-DEPLOYMENT.md` (2.4) is left for package
  experiment-page; its start command must not contain `$PORT` (exec form), agreed as option (b).
- **Crash-loop breaker policy** (stop and alert vs growing retries) remains the owner decision
  recorded 2026-09-26; the cloud keeps "stop and alert".
- **The first maintenance stop replacement at Alpaca for crypto** (`PATCH_REPLACE` or
  `CANCEL_THEN_PLACE`) has never been verified against Alpaca; the guide's first supervised day
  watches it and says how to switch maintenance off.
- **Claude's research kit.** `research_agent/` and the runbook's research-kit section still
  describe Claude's kit (pre-existing docs; this package only hardened the kit). Under the owner
  decision that Muse does all research they may be retired or relabelled; this package does not
  do that.
- **The local proof's trader has no position monitor**, so it shows the railway profile with the
  example's `DISABLED` setting; `ENABLED` is covered by the spec test (the cloud profile accepts
  it) and by the work branch's real-Jev V2 check (`artifacts/real-jev-v2-2026-09-28`).

## Owner steps

`docs/RAILWAY-DEPLOYMENT.md` section 5: install Node 22+ and the Railway CLI 5.42.1+, `railway
login`, `npm install --prefix .railway --no-package-lock`, `railway init`/`link`, `railway
config plan` and `apply`, `scripts/cloud_secrets.py`, the three sealed owner keys, stage the
release, provision (`CATALYST_PROVISION=initial`), delete Postgres's TCP proxy, stop the Mac app
and confirm the account is flat, `catalyst-lab ledger-retire`, deploy trader and ops (and
experiment), domains, the first supervised day (section 9), and hand Muse the trader's
`https://` domain and its token file (section 8; the API walkthrough is `docs/MUSE-CONNECTION.md`,
package muse-connection). `MANAGED_API_TOKEN` is never handed to anyone.
