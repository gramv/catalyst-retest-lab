# Railway deployment: the managed engine as a 24/7 experiment

**PAPER TRADING — SIMULATED. Not real money.** One Alpaca Paper account, the managed engine
only, every broker POST/DELETE/PATCH behind its exact one-use five-second risk authorization.

**Status (2026-09-27, package cloud):** prepared and proven locally; **nothing is deployed**.
No Railway account, project, service, variable or URL has been created by this work, no Railway
CLI has been installed and no Railway or GitHub API has been called. Section 11 separates what
the local proof shows from what only a real deployment can show. The owner performs every step
below; secrets are entered privately and never pasted into chat.

The owner chose Railway on 2026-09-19 ([RAILWAY-REVIEW-DEPLOYMENT.md](RAILWAY-REVIEW-DEPLOYMENT.md),
the earlier review surface, never deployed). On 2026-09-27 the owner asked to deploy the engine
and present it as an experiment. The public read-only page is package experiment-page
(section 2.4); this document covers the engine, its provisioning and operations.

## Contents

1. What runs where
2. The project: services, the IaC file, legacy files (2.5: the nightly `jobs` cron)
3. Variables
4. Account, plan and cost (4.1: Jev's monthly budget)
5. First deployment, step by step
6. Exactly one executor
7. Operating it (7.8: guarded schema migrations; the first is 025, `JEV_LIVE_REVIEW_POLICY_V2`)
8. Muse against the cloud
9. The first supervised day
10. Rollback
11. Proven locally, and only provable on Railway
12. References

## 1. What runs where

| Where | What | When |
| --- | --- | --- |
| Muse (outside the app) | All research. Muse is the one research agent and the only outside caller: it calls the trader's HTTPS API with its own agent token (research context, research reports, position news, its answers to 24-hour reviews, and exit flags it raises or answers). The API walkthrough is [MUSE-CONNECTION.md](MUSE-CONNECTION.md) (package muse-connection) | on Muse's own schedule |
| Railway, service `trader` | The managed engine: every worker loop (protection, reconciliation, the research intake and Jev's selection reviews, Jev's maintenance review of every open trade every minute, the 24-hour reviews, exit flags) and the API with token auth. Jev (TypeSafe) is called only from here | 24/7 |
| Railway, service `ops` | The watchdog (the Mac watchdog's alarm rules every 30 s), an audit checkpoint every hour, a verified and pruned `pg_dump` backup every day, alerts through the notify path | 24/7 |
| Railway, service `postgres` | The account ledger (`catalyst_lab` database), Railway's managed PostgreSQL 18 | 24/7 |
| Railway, service `experiment` | The public read-only experiment page (package experiment-page) | 24/7 |
| Railway, service `jobs` | The nightly learning jobs (package learning-app, section 2.5): shadow outcomes, maintenance replays, the day's market reality and scorecard, and after each week the weekly review; then it exits | a cron, 05:30 UTC daily |
| Railway, service `provision` | The one-off provisioner (and password rotation); exists only while you run it | minutes |
| This Mac | Only the owner's steps: the Railway CLI, `scripts/cloud_secrets.py`, `ledger-retire`, copies and restore drills of backups. Nothing on the Mac takes part in research or trading | when you run them |

Nothing in the cloud depends on the Mac. On a day Muse sends no report, no new trade is
admitted, but open trades are still maintained: protection, stops, targets, time exits and Jev's
maintenance review every minute (`MANAGED_MANAGEMENT_REVIEWS=ENABLED`) all run in `trader`. A
24-hour review whose proposing agent does not answer is decided by Jev alone
(`AGENT_SILENT_JEV_ALONE`, package day-review), so a silent Muse never blocks an exit decision.

## 2. The project

### 2.1 `.railway/railway.ts` is the project definition

Railway's Config as Code (`railway.json` / `railway.toml`) is deprecated: "New services cannot
opt into Config as Code" and existing files stop being read on 2026-12-01
([config-as-code](https://docs.railway.com/config-as-code)). The project is therefore defined
with Railway Infrastructure as Code in [`.railway/railway.ts`](../.railway/railway.ts)
([IaC](https://docs.railway.com/infrastructure-as-code),
[reference](https://docs.railway.com/infrastructure-as-code/reference)). The Railway CLI evaluates
it locally with Node, shows a plan (`railway config plan`) and changes the project only when you
confirm `railway config apply`. Railway does not read the file during deploys.

Two rules of IaC matter here:

- **Omitting a service or a variable deletes it on the next apply.** Every secret is therefore
  declared with `preserve()` ("keep the value that is already set in Railway"). No secret value
  is written in the file; `scripts/cloud_secrets.py` and you set them, and applies never touch
  them.
- **The engine settings come from `deploy/private-paper.example.json`.** The trader's variables
  are that file's `environment` map (the same names and values the Mac launcher injects) minus
  the secrets and the Mac-only `TYPESAFE_ENV_FILE`, with four cloud overrides:
  `CATALYST_ENVIRONMENT=railway`, `MANAGED_HTTP_PORT=8080`, `JEV_CREDENTIAL_SLOT=paper-railway`
  and `MANAGED_MANAGEMENT_REVIEWS=ENABLED` (owner decision: Jev monitors every trade every
  minute; the Mac example keeps `DISABLED`). Changing an engine setting means changing that file
  or the override, committing, `railway config apply`, and redeploying the trader; the quick
  way to switch maintenance off is in section 9.

The `provision` service is in the file only while `CATALYST_PROVISION` is set in your shell
(`initial`, `rotate-passwords`, or `migrate` with its three `CATALYST_MIGRATE_*` values, 7.8); a
plain `railway config apply` afterwards deletes it with its admin connection.

### 2.2 Services

| Service | Image and start | Runs as | Network | Volume | Replicas, restart |
| --- | --- | --- | --- | --- | --- |
| `postgres` | `ghcr.io/railwayapp-templates/postgres-ssl:18` (the SDK's `postgres()` helper) | PostgreSQL | private, TLS; a public TCP proxy that step 5.9 removes | its own data volume | Railway-managed |
| `trader` | `Dockerfile.managed`, `python -m catalyst_lab.cloud_entry trader` | uid 10001 after the entrypoint | private `trader.railway.internal:8080` and a public Railway domain | `trader-state` at `/data` (1 GB) | 1; ON_FAILURE, 100 retries; overlap 0 s, draining 30 s; healthcheck `/health` |
| `ops` | `Dockerfile.managed`, `python -m catalyst_lab.cloud_entry ops` | uid 10001 after the entrypoint | private only (no domain) | `ops-state` at `/data` (4 GB) | 1; same as the trader; no healthcheck (a worker) |
| `experiment` | `Dockerfile.experiment` (package experiment-page) | see 2.4 | public Railway domain | none | 1; ON_FAILURE; healthcheck `/health` |
| `jobs` | `Dockerfile.managed`, `python -m catalyst_lab.cloud_entry jobs` | uid 10001 after the entrypoint | private only (no domain) | none | cron `30 5 * * *` (UTC); NEVER; no healthcheck (it exits) |
| `provision` | `Dockerfile.managed`, `python -m catalyst_lab.cloud_entry provision initial` | uid 10001 | private only | none | 1; NEVER (runs once) |

The image (`Dockerfile.managed`): `python:3.12-slim`, `tzdata`, the PostgreSQL project's
`postgresql-client-18` (the apt key's fingerprint is checked in the build; `PG_MAJOR` is a build
argument and must stay equal to Railway's Postgres major), the locked runtime dependencies with
their hashes (`deploy/requirements.managed.txt`, exported from `uv.lock`), the package and the
sealed release identity. No secret is a build argument or a file in the image.

**Why the container starts as root.** Railway mounts a volume owned by root and documents that
an image running as a non-root user cannot write to it
([volumes reference](https://docs.railway.com/volumes/reference); its fix, `RAILWAY_RUN_UID=0`,
would run everything as root). `catalyst_lab.cloud_entry` therefore runs first as root, imports
only the standard library, and before it creates or chowns anything requires Railway's
`RAILWAY_VOLUME_MOUNT_PATH` with `CATALYST_STATE_DIR` equal to it or inside it, reached through
real directories only (`CLOUD_VOLUME_REQUIRED`, `CLOUD_STATE_DIR_NOT_ON_VOLUME`,
`CLOUD_STATE_DIR_NOT_A_DIRECTORY` otherwise: a symlink on the volume cannot lead root off it).
It then hands `/data` (and only it) to the image's `catalyst` user (uid/gid 10001) with mode
0700, drops all groups and both ids permanently (it checks that root cannot come back), and
`exec`s the component with that user's `HOME`. No application code runs as root, and the
component keeps PID 1, so Railway's SIGTERM reaches it directly.

### 2.3 Legacy files (never deployed)

The root `railway.json`, `Dockerfile.review` and `deploy/railway.review-worker.json` belong to
the earlier review surface of 2026-09-19 ([RAILWAY-REVIEW-DEPLOYMENT.md](RAILWAY-REVIEW-DEPLOYMENT.md)).
They were never deployed, are Config as Code (not accepted for new services, not read after
2026-12-01) and are not part of this deployment. They are left unchanged in the repository, but
`scripts/cloud_release.py` removes a root `railway.json` or `railway.toml` from every staged
release (`excluded_config_as_code` in its output), so no deploy of this project can pick up their
build or start settings.

### 2.4 The `experiment` service

The public live dashboard (package experiment-page; [its record](packages/experiment-page.md)).

The page shows:
- the overall, today and past-day figures;
- the live open trades, with Jev's last action on each;
- Muse's and Jev's status and decisions, in a feed of the latest 20.

It polls its own JSON every 5 seconds.

- **Build:** `Dockerfile.experiment` at the repository root. It runs as a non-root user, runs `pip install .`, and holds no secrets.
- **Start:** `python -m catalyst_lab.experiment_page --host 0.0.0.0`.
  - The port comes from the `PORT` variable Railway injects, else 8080.
  - No `--port` and no shell are needed; Railway runs the command in exec form.
- **Health check:** `GET /health`. It answers 200 only when the database answers as `catalyst_public`, otherwise 503.
- **Deployment:** one replica, restart `ON_FAILURE`, and a public Railway domain (`railway domain --service experiment`).
- **Variables** (`.railway/railway.ts`):
  - `EXPERIMENT_DATABASE_URL`: the read-only connection as role `catalyst_public`, with `PUBLIC_DATABASE_PASSWORD` (which lives on `ops`) by reference.
  - `EXPERIMENT_RESEARCH_SCHEDULE_JSON`: a reference to the trader's `MANAGED_RESEARCH_SCHEDULE_JSON`, so "next run" always follows the engine's own schedule.
  - Optionally `EXPERIMENT_TITLE`. The default is "AI crypto trading — live".
- **Never set on this service:**
  - any `APCA_*` variable or `TYPESAFE_API_KEY` (the service refuses both at start);
  - a database URL for any other role.
- **It cannot trade.**
  - It holds no broker or Jev key, and has no broker or HTTP client.
  - It makes no outbound call. Current prices are the freshest ones the ledger already holds.
  - `catalyst_public` has a connection limit of 8 and can read only the four `lab.public_dashboard_*` views and execute three helper functions. It cannot read a base table, write anything, or see any order, account or request data.
- **Order:** the ledger must be at schema 24 before this service starts, since migration 024 creates the views and the role. Until then `/health` answers 503. The provisioner grants the role LOGIN.

### 2.5 The `jobs` service (package learning-app)

The nightly learning jobs of the learning loop ([LEARNING-LOOP-PLAN.md](LEARNING-LOOP-PLAN.md)
section 7; [the package record](packages/learning-app.md)). Owner decision of 2026-09-28: a
Railway cron service, not a loop inside the trader, which stays the only executor.

- **Schedule:** `cronSchedule` `30 5 * * *`. Railway evaluates cron schedules in UTC, so 05:30
  UTC is 01:30 in New York in summer and 00:30 in winter: always after the New York midnight
  that ends the day being scored ([cron jobs](https://docs.railway.com/reference/cron-jobs):
  "schedules are based on UTC"; runs "must be at least 5 minutes apart"; execution times "can
  vary by a few minutes").
- **One run:** `python -m catalyst_lab.cloud_entry jobs` runs these steps once, in order, each
  independent, and logs one `JOBS STEP step=... result=OK|FAILED|SKIPPED code=...` line each:
  1. `shadow_outcomes`: `PICK_SHADOW_OUTCOME_V1` for every pick whose window plus the 24-hour
     hold has passed (report-V3 cycles of the last 40 days);
  2. `maintenance_replays`: `UNCHANGED_PLAN_REPLAY_V1` and `DAY_REVIEW_DECISION_REPLAY_V1`
     counterfactuals that are now complete;
  3. `market_reality`: `MARKET_REALITY_V1` of the previous New York day;
  4. `scorecard`: `DAILY_SCORECARD_V1` of the previous New York day;
  5. `weekly_review`: `WEEKLY_REVIEW_V1` of the Monday-to-Sunday week that ended (Monday's run
     records it).

  Every record is an immutable ledger event keyed by its day or week, so a rerun records
  nothing twice. Steps 3 and 4 also catch up the two days before the previous one when they are
  missing, and step 5 records a finished week on the first run after it.
- **Exit:** 0 after the steps, whatever they logged (a refused configuration exits 2). Railway
  expects a cron service to "execute a task, and terminate as soon as that task is finished,
  leaving no open resources", skips a run while the previous one is still going, and does not
  terminate a deployment itself (same page). So a step not started after 20 minutes is skipped
  (`JOB_TIME_LIMIT`) and the process ends itself after 30 minutes at the latest.
- **Restart policy `NEVER`:** Railway's default is ON_FAILURE with 10 retries
  ([restart policy](https://docs.railway.com/deployments/restart-policy)); the cron page is silent
  on restarts, so the file sets NEVER explicitly: a failed run waits for the next night instead
  of repeating.
- **Variables:** `CATALYST_ENVIRONMENT=railway` and `MANAGED_DATABASE_URL`, the trader's
  `catalyst_risk` connection with `${{trader.RISK_DATABASE_PASSWORD}}` by reference: no new
  secret, and no migration (every record is an event in `lab.managed_events` through the
  audited append). `cloud_config.jobs_config` refuses by name any Alpaca or TypeSafe key, any
  role or agent token, the Jev worker's connection and every other role's password
  (`JOBS_MUST_NOT_HOLD_TRADING_SECRETS`), and any unknown engine variable.
- **It cannot trade:** no broker or Jev key and no broker client; its only outbound calls are
  Alpaca's public, keyless crypto bars (`data.alpaca.markets`, 1-minute and 1-hour). It sends
  nothing to Jev. No volume and no state: the ledger is its state.
- **Cost:** a few minutes of CPU a night, under $0.10 a month.

## 3. Variables

Secrets live only in Railway variables. "Generated" ones are created by
`scripts/cloud_secrets.py` on your Mac and piped into `railway variable set NAME --stdin`
([railway variable](https://docs.railway.com/cli/variable)): they are never printed, typed or
stored except Muse's agent token file (section 8). "Owner" ones are entered by you in the
dashboard and **sealed** ([sealed variables](https://docs.railway.com/variables#sealed-variables)):
Railway then never shows or returns them again.

| Name | Service | Secret | Source |
| --- | --- | --- | --- |
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | trader | yes | owner, sealed (Alpaca **Paper** key; a non-paper key ID is refused at startup) |
| `TYPESAFE_API_KEY` | trader | yes | owner, sealed |
| `MANAGED_API_TOKEN` | trader | yes | generated, then sealed (5.5). Never issued to anyone: it is the legacy identity (the one credential that may still submit unversioned legacy reports), so it stays out of every hand-off. Muse uses its agent token |
| `MANAGED_STATUS_TOKEN` | trader and ops (the same value) | yes | generated; GET-only status access |
| `MANAGED_OPERATOR_TOKEN` | trader | yes | generated (reserved role) |
| `MANAGED_AGENT_TOKENS_JSON` | trader | yes | generated: exactly `{"muse": "<token>"}` (the identity screens match a report's agent to its credential's own ID, so the ID is exactly `muse`); the token is also written to your local file for Muse |
| `RISK_DATABASE_PASSWORD`, `JEV_DATABASE_PASSWORD` | trader | yes | generated; read by the DSN references |
| `APP_DATABASE_PASSWORD`, `BACKUP_DATABASE_PASSWORD`, `OPERATOR_DATABASE_PASSWORD`, `PUBLIC_DATABASE_PASSWORD` | ops | yes | generated; read by the DSN references (the public one by `experiment`) |
| `MANAGED_DATABASE_URL` | trader | contains a reference | railway.ts: `postgresql://catalyst_risk:${{RISK_DATABASE_PASSWORD}}@${{postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/catalyst_lab?sslmode=require` |
| `MANAGED_DATABASE_URL` | jobs | contains a reference | railway.ts: the same connection, with `${{trader.RISK_DATABASE_PASSWORD}}` |
| `CATALYST_ENVIRONMENT` (`railway`) | jobs | no | railway.ts |
| `JEV_WORKER_DATABASE_URL` | trader | contains a reference | railway.ts: role `catalyst_jev` |
| `AUDIT_DATABASE_URL`, `BACKUP_DATABASE_URL`, `OPERATOR_DATABASE_URL` | ops | contain references | railway.ts: roles `catalyst_app`, `catalyst_backup`, `catalyst_operator` |
| `EXPERIMENT_DATABASE_URL` | experiment | contains a reference | railway.ts: role `catalyst_public` |
| `EXPERIMENT_RESEARCH_SCHEDULE_JSON` | experiment | no | railway.ts: a reference to the trader's `MANAGED_RESEARCH_SCHEDULE_JSON` |
| `EXPERIMENT_TITLE` | experiment (optional) | no | owner; default "AI crypto trading — live" |
| `MIGRATION_DATABASE_URL` and the six passwords | provision (temporary) | references | railway.ts: `${{postgres.DATABASE_URL}}?sslmode=require` and `${{trader.X}}` / `${{ops.X}}`; in `migrate` mode (7.8) only `MIGRATION_DATABASE_URL` |
| Engine settings (`MANAGED_*`, `JEV_*` policies, `CATALYST_ENVIRONMENT`) | trader | no | railway.ts, from `deploy/private-paper.example.json` |
| `JEV_MONTHLY_BUDGET_USD` (50), `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD` (0.042), `JEV_BYTES_PER_TOKEN` (3) | trader | no | railway.ts, from `deploy/private-paper.example.json` (engine settings; package jev-budget). Required in railway mode: the monthly Jev budget and its price estimate (section 4.1) |
| `MANAGED_CRYPTO_WINDOW_JSON` (`{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}`) | trader | no | railway.ts, from `deploy/private-paper.example.json` (engine setting; package review-window). Required in railway mode: the trade window new report-V3 crypto trades record (`CRYPTO_WINDOW_REVIEW_V1` / `CRYPTO_WINDOW_HOLD_V1`); each trade keeps the window it started with |
| `PORT` (8080), `CATALYST_STATE_DIR` (`/data`) | trader, ops | no | railway.ts |
| `MANAGED_STATUS_API` | ops | no | railway.ts: `http://${{trader.RAILWAY_PRIVATE_DOMAIN}}:8080` |
| `CLOUD_BACKUP_RETENTION_DAYS` (14) | ops | no | railway.ts |
| `CLOUD_NOTIFY_JSON` | ops | no | owner, optional (section 7.4) |
| `NOTIFY_PING_URL` | ops | yes | owner, optional, sealed (section 7.4) |

Every DSN uses `sslmode=require`: the connection is encrypted (Railway's Postgres image serves
TLS with its own certificate, which `require` does not verify; the private network is WireGuard
as well). The DB passwords are URL-safe (`token_urlsafe`), so the templates need no escaping.
The entrypoint runs every component with `HOME=/nonexistent`, the image user's home: libpq 18
(psycopg's and `pg_dump`'s) looks for a client certificate under `$HOME/.postgresql` on every
TLS connection and refuses the connection when that path cannot be searched, as root's `/root`
cannot be by uid 10001 (`tests/test_cloud_tls.py`).

**Sealing.** Seal the three owner keys and `MANAGED_API_TOKEN`; you may seal the other
generated tokens too, which no other variable references. Leave the six `*_DATABASE_PASSWORD`
variables unsealed: other variables read them through `${{...}}` references, and Railway
documents what sealing hides but not whether a sealed value resolves inside another variable's
reference. If a reference ever resolved empty, the service would refuse to start (a missing
password fails the role check), never connect with a wrong identity. They are still never
printed by any of this tooling. Railway documents that the CLI does not return sealed *values*,
not whether `railway variable list` still names a sealed variable; before any later run of
`scripts/cloud_secrets.py`, run it with `--dry-run` and check that every secret already set
shows `SKIP`. If a sealed token shows `GENERATE`, stop: a real run would replace it.

Railway's `secret()` generator is documented only as a *template* function evaluated when a
template is deployed ([templates](https://docs.railway.com/templates/create)); the IaC docs
document no generator, and the Railway CLI's IaC plan compares a generated variable as changed
on every apply (its `src/iac/change_set.rs`), so a later apply could rotate a password under a
running service. That is why the values are generated by the script and kept with `preserve()`.

**Startup refusals (railway mode).** The trader and ops refuse to start (exit 2, one code-only
log line) when: a `.env` file exists in the working directory or the image;
`TYPESAFE_ENV_FILE` is set; any secret is missing or a placeholder (`REQUIRED...`); an engine
setting is missing, invalid (the same checks as the private config v2 preflight; for example
`CLOUD_ENGINE_SETTING_MISSING JEV_MONTHLY_BUDGET_USD` or `CLOUD_ENGINE_SETTING_INVALID
JEV_BYTES_PER_TOKEN`) or unknown
(any `MANAGED_`/`JEV_`/`APCA_`/`TYPESAFE_`/`CLOUD_`/`NOTIFY_` name the profile does not use:
a typo can never fall back silently to a default); two role tokens are equal; the Alpaca key is
not a paper key or an Alpaca endpoint is redirected; a DSN names another role or database or
lacks TLS; `PORT` differs from `MANAGED_HTTP_PORT`; state is not on the Railway volume; or the
image is not a sealed release. The ops service also refuses if it holds any broker, Jev or trader
credential.

## 4. Account, plan and cost

Use the **Hobby** plan: $5 a month, which includes $5 of resource usage; usage above that is
billed on top ([plans](https://docs.railway.com/pricing/plans)). Rates: RAM $10 per GB-month,
CPU $20 per vCPU-month, volume storage $0.15 per GB-month, egress $0.05 per GB. Hobby limits
(48 GB RAM, 48 vCPU per service, 5 GB per volume) are far above this project's needs.

Estimated average usage (Railway meters actual use per minute; these are estimates, not
measurements):

| Service | RAM (avg) | CPU (avg) | Per month |
| --- | --- | --- | --- |
| trader | 0.30–0.45 GB | 0.03–0.08 vCPU | $3.60–$6.10 |
| ops | 0.10–0.15 GB | ~0.01 vCPU (a backup burst once a day) | $1.20–$1.70 |
| postgres | 0.10–0.20 GB | 0.01–0.03 vCPU | $1.20–$2.60 |
| experiment | 0.08–0.12 GB | ~0.01 vCPU | $1.00–$1.40 |
| jobs | 0.2 GB for a few minutes a night | a few CPU-minutes a night | < $0.10 |
| volumes and backups | 1–6 GB | | $0.15–$0.90 |
| egress (Alpaca, TypeSafe, pages) | < 2 GB | | < $0.10 |
| **Total usage** | | | **about $7–$13** |

So expect a monthly bill of roughly **$7–$13** (the $5 subscription counts towards it). Set a
usage limit in the Railway billing settings so a runaway cannot surprise you
([cost control](https://docs.railway.com/pricing/cost-control)); the ops alarms and the trader's
own lease and crash-loop rules keep a failure from spinning. The owner's decision of 2026-09-28
is a **$15 email alert and a $20 hard limit** for Railway. Know what the hard limit does: in
Railway's words, "once your resource usage hits the specified hard limit, all your workloads will
be taken offline". The trader stops with the rest: only the stop-limit orders resting at Alpaca
protect open positions until you raise the limit (no app-side target, time exit, maintenance or
stop replacement runs), so treat the $15 alert as the signal to act.

**Storage is the constraint to watch, not Jev dollars.** Every maintenance review appends its
request, receipt, answers and decision to the ledger and to its audit chain: about 65 KB per
review measured on the fixture venue (11 KB requests; about 75 KB at the real 13.5 KB requests,
package jev-budget). At the per-minute cadence that is about 110 MB a day per open maintained
trade, and at the guard's full $50 (about 245,000 reviews a month) about 16–19 GB a month. The
Hobby plan's volumes stop at 5 GB ([plans](https://docs.railway.com/pricing/plans)): the
Postgres volume holds about 45–55 trade-days of per-minute reviews (nine to eleven days with
five maintained trades open around the clock), and the 14 daily backups on the 4 GB ops volume
run out sooner. Watch the database size (`railway volume list`) and decide before it fills: fewer
maintained trades, a lower cadence, the Pro plan (volumes up to 1 TB, a larger subscription) or
a smaller ledger footprint per review (a separate package).

### 4.1 Jev: the monthly budget (`JEV_SPEND_GUARD_V1`, package jev-budget)

The owner's total is **$70 a month at most**: Railway above (alert $15, hard limit $20) and Jev
at most **$50** (`JEV_MONTHLY_BUDGET_USD=50`). TypeSafe publishes jev-1.13 at $0.042 per million
input tokens, output free, the context counted once per request
([pricing](https://www.typesafeai.org/guides/jev-pricing)). The trader meters every call from
its exact request bytes at one token per three bytes (`JEV_BYTES_PER_TOKEN=3`, a conservative
estimate) and `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD=0.042`. A maintenance review (context V5,
13.6 KB) costs about $0.00019; one trade reviewed every minute costs about **$8.20 a month**
(30 days; $8.48 in a 31-day month), a research run of 20 picks about $0.007 and a 24-hour review
about $0.0002.

Trades admitted from this package on record `CRYPTO_MAINTENANCE_V3`: Jev reviews them every
minute while the month's projection fits the budget, and the guard throttles only routine
reviews when it does not:

| Tier | When (B = the budget, P1 = month-to-date plus the last 24 hours' per-minute demand for the rest of the month) | Routine maintenance reviews |
| --- | --- | --- |
| `NORMAL` | P1 at or below 98% of B (or, once throttled, back at or below 93%) | every completed minute |
| `THROTTLED` | P1 above 98% of B | every 5 completed minutes |
| `TIGHT` | throttled and 90% of B already spent | every 15 completed minutes |
| `EXHAUSTED` | 98% of B spent (the last 2% is kept for selection, 24-hour reviews and early exits) | none |

Event reviews (milestones, near the target or stop, news, a Bitcoin shock) still run in every
tier except `EXHAUSTED`; stops, targets and protection never depend on the tier; selection,
24-hour reviews and early exits are never throttled. Estimated month with N maintained trades
open around the clock (a research run of 20 picks a day, four event reviews per trade a day, a
30-day month; month-long simulation of the guard with the measured request sizes):

| Open maintained trades | Without the guard | With the guard | What changes |
| --- | --- | --- | --- |
| 2 | $16.66 | $16.66 | nothing: every minute all month |
| 4 | $33.13 | $33.13 | nothing: every minute all month |
| 5 | $41.36 | $41.36 | nothing (the most that stays per-minute all month) |
| 6 | $49.59 | $46.66 | every 5 minutes for the first 2.2 days, then every minute |
| 7 | $57.83 | $46.65 | every 5 minutes for the first 7.3 days, then every minute |

So $50 covers **five maintained trades around the clock at the per-minute cadence**; from six the
month starts throttled until the rest of it fits (in a 31-day month: $17.22, $34.23, $42.74 and
$46.65 for 2, 4, 5 and 7). With demand that changes every day the guard changed tier two to six
times a month in simulation. The status (7.1) shows the month-to-date spend, the projections,
the budget and the tier; each change into `THROTTLED`, `TIGHT` or `EXHAUSTED` is an off-host
alert (7.4). These are estimates: TypeSafe's own usage page is the bill. To recalibrate, divide
the request bytes the status reports for a period (`month_request_bytes`) by the input tokens
TypeSafe reports for the same period and set `JEV_BYTES_PER_TOKEN` to the result (runbook
"Monthly Jev budget"). To change the budget, set `JEV_MONTHLY_BUDGET_USD` (a trader redeploy)
and commit the same value in `deploy/private-paper.example.json`.

## 5. First deployment, step by step

Run everything from the repository root on this Mac. Nothing below prints a secret.

**5.1 Tools.** Node 22 or newer (`node --version`; Homebrew's is fine). The Railway CLI 5.42.1
or newer, installed by you (`brew install railway`, or `npm install -g @railway/cli`), then
`railway login` ([login](https://docs.railway.com/cli/login)). The IaC SDK pinned in
`.railway/package.json`:

```sh
npm install --prefix .railway --no-package-lock   # .railway/node_modules is ignored by git
```

**5.2 Project.** Create a project and link this directory to it and its `production`
environment: `railway init --name catalyst-retest-lab` (or create it in the dashboard), then
`railway link`.

**5.3 Plan and apply.**

```sh
railway config plan     # expect: create postgres, trader, ops, experiment, jobs, two volumes
railway config apply    # confirm the plan it shows
```

The trader's variables include the monthly Jev budget's three settings (`JEV_MONTHLY_BUDGET_USD`,
`JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD`, `JEV_BYTES_PER_TOKEN`, section 4.1): on a first
deployment the apply sets them before the first `railway up`, as it sets every engine setting.
(For a project whose trader already runs a release without them, see 7.6.)

**5.4 Generated secrets.** Choose a private directory outside the repository and outside
`~/Documents` and iCloud Drive for Muse's agent token, then:

```sh
mkdir -m 700 -p ~/.local/share/catalyst-retest-lab/cloud
python3 scripts/cloud_secrets.py --dry-run \
  --agent-token-file ~/.local/share/catalyst-retest-lab/cloud/muse-agent-token
python3 scripts/cloud_secrets.py \
  --agent-token-file ~/.local/share/catalyst-retest-lab/cloud/muse-agent-token
```

The script prints only names and actions (`GENERATE`, `SKIP`, `ROTATE`). Running it again skips
everything already set. A secret present on only some of its services is refused until you
pass `--rotate NAME`. The agent ID is `muse` by default (`--agent-id`); keep it. The file is
Muse's credential. Keep it where it is for now: Muse gets it (with the trader's `https://`
domain, as [MUSE-CONNECTION.md](MUSE-CONNECTION.md) describes) only after the first supervised
day's checks pass (section 9: status, streams, a clean reconciliation), and never through chat;
nobody opens it. No other token leaves Railway: not `MANAGED_API_TOKEN` (never issued), not the
status or operator tokens.

**5.5 Owner secrets.** In the dashboard, service `trader` → Variables: add `APCA_API_KEY_ID`,
`APCA_API_SECRET_KEY` (the **Paper** account) and `TYPESAFE_API_KEY`, then use each variable's
three-dot menu → **Seal**. Seal the generated `MANAGED_API_TOKEN` the same way; optionally seal
the other generated tokens too (section 3, not the DB passwords). Never paste these values into
chat or a file in the repository.

**5.6 Check.** `railway config plan` must now report no changes: the preserved values are kept.

**5.7 Stage the release.** `railway up` uploads local files and gives the build no git SHA, so
the release is staged from a clean commit:

```sh
python3 scripts/cloud_release.py --out /tmp/catalyst-release-$(git rev-parse --short HEAD)
```

It refuses a dirty working tree, exports the committed tree with `git archive` (never ignored
files or `.env`) and writes `release-source.json` = {commit, package SHA-256}. The image build
recomputes the package hash and fails if the uploaded package differs
(`python -m catalyst_lab.cloud_release seal`); the running app then reports that commit as
`release_commit` in its status.

**5.8 Provision the ledger (once).**

```sh
CATALYST_PROVISION=initial railway config plan    # expect: create service provision
CATALYST_PROVISION=initial railway config apply
railway up /tmp/catalyst-release-<commit> --path-as-root --service provision --ci
railway logs --service provision --lines 20
```

The log must end with one JSON line: `"result": "PROVISIONED"`, `"schema_version"` (23 before
package experiment-page merges, 24 after), the `ledger_id`, the login roles
(`catalyst_risk`, `catalyst_jev`, `catalyst_app`, `catalyst_operator`, `catalyst_backup`, and
`catalyst_public` once migration 024 exists) and the NOLOGIN roles. A refusal names its code
(`PROVISION_LAB_SCHEMA_EXISTS`, `PROVISION_DATABASE_NOT_EMPTY`, `PROVISION_ROLES_ALREADY_EXIST`,
`PROVISION_PASSWORD_MISSING <NAMES>`, ...). The provisioner only ever provisions an **empty**
database; it never migrates an existing ledger (the Mac ledger moves nowhere: it is retired,
step 5.10, and its history stays on the Mac). Then remove the service with its admin
connection:

```sh
railway config apply    # without CATALYST_PROVISION: confirm "Delete service provision"
```

What the provisioner did, in one transaction on a fresh `catalyst_lab` database (C collation,
like a local ledger): refused anything but an empty database and a cluster without the lab
roles; applied `schema.sql` and every numbered migration as `lab_owner`, a NOLOGIN superuser
(so ownership matches a local ledger and the local restore drill accepts cloud backups);
created `catalyst_backup`, read-only and narrow: USAGE on `lab`, SELECT on its tables and
sequences, and the same on every later one through `lab_owner`'s default privileges (what
`pg_dump` of the ledger reads; no predefined role, so not `pg_authid`'s password verifiers); set
SCRAM verifiers (never plaintext; statement logging suppressed on its connection) and LOGIN for
exactly the cloud roles; set every other lab role NOLOGIN; set server-side TCP keepalives on
the database (a vanished trader's lease is released in about a minute); checked that no login
role can alter the schema, rewrite the audit chain or read `pg_authid`; and appended the
audited `CLOUD_LEDGER_PROVISIONED` event (`role: ACCOUNT_LEDGER`, `ledger_id`, release). That
event is the cloud equivalent of the Mac's `LEDGER.json` marker: the trader refuses to start
without exactly one.

**5.9 Remove Postgres's public TCP proxy.** The IaC `postgres()` helper creates a TCP proxy on
5432 for a new database (the Railway CLI's `src/iac/compiler.rs`). Nothing here needs it: all
services use the private network. In the dashboard, `postgres` → Settings → Networking, delete
the TCP proxy, then run `railway config plan`: it must show no networking change (per the CLI's
`src/iac/change_set.rs`, a database node without a networking block "keeps whatever exposure it
has", so later applies do not re-create it). If you keep it, the exposure is a password-protected
endpoint: Railway's generated 32-character superuser password over SCRAM with TLS, and runtime
roles that have no SUPERUSER, CREATEROLE or CREATEDB.

**5.10 Retire the Mac ledger.** The Mac must never run an executor for this account again, and
a fresh cloud ledger must not start while the account holds positions it does not know.

1. Pause, then make the account flat while the Mac app still runs. First
   `managed_ops operator pause --reason "Moving to the cloud"`, so no new entry is admitted
   between the flatten and the stop. Then, in the Alpaca **Paper** dashboard: no open position
   and no open order. If anything is open, close it with `managed_ops operator flatten-all
   --reason "Moving to the cloud"` (runbook "First supervised managed trade", step 8) and wait
   until `operator status` shows `pending_flatten_count` 0 and the dashboard shows nothing
   open.
2. Stop the Mac app and everything that would restart it: with LaunchAgents,
   `launchctl bootout gui/$(id -u)/local.catalyst.paper.app` and `.muse`, and the watchdog,
   audit and backup agents (`local.catalyst.paper.<component>`); in a foreground terminal,
   Ctrl-C. Leave the ledger's own cluster running (its `ledger` agent or component starts only
   the PostgreSQL server, never a migration): the next step checks the executor through it.
   Check the Paper dashboard once more: still no open position and no open order. A fresh cloud
   ledger refuses entries while the account holds a position or an open order it does not know
   (`tests/test_cloud_fresh_ledger.py`); closed, historical orders never block it.
3. Retire the ledger. The command first proves that the Mac executor is gone, and refuses
   (changing nothing) otherwise:

   ```sh
   ./run catalyst-lab ledger-retire \
     --ledger-dir ~/.local/share/catalyst-retest-lab/managed-real-20260919-a \
     --reason MOVED_TO_CLOUD
   ```

   On the ledger's own socket it checks that the lock every executor holds for its whole life
   is free (`LEDGER_EXECUTOR_RUNNING` otherwise), that no session of the app's logins
   (`catalyst_risk`, `catalyst_jev`) exists (`LEDGER_APP_SESSIONS_PRESENT`: an app still
   starting), and that nothing is bound to the app's port 8780 (`LEDGER_APP_PORT_IN_USE`; pass
   `--app-port` if your private config's `MANAGED_HTTP_PORT` differs). It holds that lock while
   it writes the marker, so no executor can start in between, and checks it still holds it
   afterwards (`LEDGER_EXECUTOR_LOCK_LOST`: retired, but look at the Mac by hand). An app that
   was already starting (past the launcher's check, not yet connected or bound) cannot slip
   through either: once it takes the lease it re-reads the marker, releases the lease and stops
   (`LEDGER_RETIRED`). A stopped cluster is refused (`LEDGER_CLUSTER_NOT_RUNNING`): the command
   never starts one. It keeps
   every key of `LEDGER.json`, adds `retired_at` and `retired_reason`, and writes the marker
   atomically (mode 0600); it changes no database row and calls no broker. A second run is
   refused (`LEDGER_ALREADY_RETIRED`). From then on `run_managed_private.py --component app` and
   `--component muse` refuse with `LEDGER_RETIRED`, `ledger-migrate` refuses
   `OWNER_LEDGER_RETIRED`, preflight reports it, and the ledger stays protected and readable
   (the `ledger` component still starts its cluster for reading, exports and backups).
4. Keep a final Mac backup (runbook "Database backup and restore drill"); then the ledger's
   cluster may be stopped too.

**5.11 Deploy.**

```sh
railway up /tmp/catalyst-release-<commit> --path-as-root --service trader --ci
railway up /tmp/catalyst-release-<commit> --path-as-root --service ops --ci
railway up /tmp/catalyst-release-<commit> --path-as-root --service experiment --ci
railway up /tmp/catalyst-release-<commit> --path-as-root --service jobs --ci
railway domain --service trader --port 8080
railway domain --service experiment
```

([railway up](https://docs.railway.com/cli/up), [railway domain](https://docs.railway.com/cli/domain).)
Deploy `trader` and `ops` from the same staged directory: the ops watchdog alarms
`RELEASE_CODE_MISMATCH` while they differ.

**5.12 Check.** `railway logs --service trader --lines 20` shows `TRADER STARTING
release_commit=...` then `TRADER SERVING ledger_id=...`; `railway logs --service ops` shows
`OPS TICK alarms=...`. Then the first supervised day (section 9). Muse gets the trader's
`https://` domain and its token file (5.4, section 8) only once that section's checks before any
research pass: until then it holds no credential for this deployment.

## 6. Exactly one executor

**The layers.**

1. **One replica.** `replicas: 1` for the trader, and a volume: "Replicas cannot be used with
   volumes" ([volumes reference](https://docs.railway.com/volumes/reference)).
2. **Stop before start on redeploys.** By default Railway replaces a deployment "with a slight
   overlap for zero downtime", sending the old one SIGTERM once the new one is online, with "0
   seconds to gracefully shutdown before being forcefully stopped with a SIGKILL"
   ([deployments reference](https://docs.railway.com/deployments/reference)). A volume changes
   that: "we prevent multiple deployments from being active and mounted to the same service. This
   means that there will be a small amount of downtime when re-deploying a service that has a
   volume attached, even if there is a healthcheck endpoint configured"
   ([healthchecks](https://docs.railway.com/deployments/healthchecks)). The trader's volume
   (`trader-state`, which also holds its crash-loop state) therefore makes Railway stop the old
   trader before the new one starts. On top, `overlapSeconds: 0` and `drainingSeconds: 30`
   ([teardown](https://docs.railway.com/deployments/deployment-teardown): the
   `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` / `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` settings) give the
   old trader 30 seconds after SIGTERM to stop its loops and close its lease connection, the
   same 30 seconds the Mac LaunchAgent gave it (`ExitTimeOut 30`).
3. **The database lease, whatever Railway does.** Every trader takes the account executor lease
   (a PostgreSQL advisory lock keyed by the broker account) before it trades, holds it for its
   whole life and re-checks it before every broker send. A second trader is refused
   `ACCOUNT_EXECUTOR_ALREADY_RUNNING` (`tests/test_executor_lease.py`,
   `tests/test_cloud_runtime.py`, and the local proof with two processes). In railway mode a
   trader that finds the lease held does not exit: it keeps answering `/health`, logs
   `EXECUTOR_LEASE_WAITING` once a minute and takes over when the holder is gone (the local
   proof: trader B waits, trader A gets SIGTERM, B serves 1–2 seconds later). If a trader's host
   vanishes without closing its connection, the database's TCP keepalives (set by the
   provisioner) drop that session and release the lease in about a minute.

**Health.** `/health` answers 200 from the moment the port is bound, so Railway's deployment
healthcheck never fails a trader that is waiting for the database, the ledger identity or the
lease. It never claims trading readiness: while starting it returns
`{"status": "ok", "phase": "STARTING", ...}`, afterwards the app's constant
`{"status", "alpaca", "surface", "cohort", "supervised"}` (after a database probe). Readiness
(`entry_ready`, `executor_ownership`, reconciliation, streams, halts) is only in the
token-protected `GET /api/v1/lab/status`. Railway calls the healthcheck only at deployment start
([healthchecks](https://docs.railway.com/deployments/healthchecks)); the ops watchdog is the
continuous check. The static pages `/`, `/lab.js`, `/lab.css`, `/results` and `/results.js` are
constant strings that fetch data only with a token typed into the page; every other route
requires a bearer token (`tests/test_cloud_surface.py` enumerates all of them).

**Binding.** Only a process actually on Railway (`RAILWAY_ENVIRONMENT_ID` present) listens
beyond loopback: `[::]:8080`, dual stack, because Railway's private DNS answers with IPv6 only in
environments created before 2025-10-16
([private networking](https://docs.railway.com/networking/private-networking/how-it-works)).
Everywhere else, including the local proof, the trader binds `127.0.0.1`.

**Exit codes and restarts** (`restartPolicyType: ON_FAILURE`, `restartPolicyMaxRetries: 100`;
Railway's default is 10, and paid plans allow any number,
[restart policy](https://docs.railway.com/deployments/restart-policy)):

| Exit | Meaning | Railway |
| --- | --- | --- |
| 75 | The executor lease was lost (a PostgreSQL restart, a terminated session). The runtime stops every loop and exits within 30 s | restarts it (non-zero); the new process waits for the database and the lease, then reconciles before it protects or enters |
| 0 | The crash-loop breaker tripped: the fifth start within ten minutes | leaves it stopped ("Completed", an exit 0) on purpose |
| 0 | SIGTERM on a redeploy or a stop | the deployment is being replaced |
| 2 | A refused configuration or an unsealed image | restarts it; five starts in ten minutes trip the breaker |
| 1 | Startup kept failing for ten minutes (database, broker identity, build) | restarts it |
| 3 | The HTTP server could not start | restarts it |

The breaker's start records live on the volume (`/data/launcher-state/`, 0600 files in a 0700
directory), so they survive container restarts; the local proof shows a sixth start in a new
process still stopped. To recover after fixing the cause: wait ten minutes and
`railway redeploy --service trader`, or delete the start records from the stopped service's
volume and redeploy:
`railway volume files --volume trader-state delete /launcher-state/trader.starts.json`
([railway volume](https://docs.railway.com/cli/volume)). While the trader is stopped, broker-side
stop-limits and bracket legs still protect open positions; app-side exits resume with it. The
ops watchdog reports the stopped trader (`STATUS_UNAVAILABLE`), and the notifier forwards it.

## 7. Operating it

### 7.1 Status

The status is `GET https://<trader domain>/api/v1/lab/status` with the status token. The ops
service holds that token, so the simplest checks run there and never print it: `status` prints
the supervised-day fields (including `trade_maintenance`, `day_reviews` and `jev_budget`: the
month-to-date Jev spend, today's, the projections, the budget and the tier, section 4.1),
`positions` each open trade's stop, target, a raised stop still being replaced (`stop_replace`)
and next 24-hour review:

```sh
railway ssh --service ops -- python -m catalyst_lab.cloud_runtime status
railway ssh --service ops -- python -m catalyst_lab.cloud_runtime positions
```

The trader's public `/health` answers liveness only, from memory: it opens no database
session and no thread, so traffic to it cannot slow status, Muse, the watchdog or the trader's
own writes (package cloud-hardening; the Mac app's loopback `/health` still reads the ledger).

The ops watchdog's latest alarms are in `/data/alarms.json` on the ops volume
(`railway ssh --service ops -- cat /data/alarms.json`) and in `railway logs --service ops`.
`railway ssh` needs an SSH key registered with Railway, and a service without a public domain
(ops) is reached by its service instance ID ([railway ssh](https://docs.railway.com/cli/ssh)).
These commands expect the SSH session to see the service's variables (the container
environment); Railway does not document that explicitly, so the first check is part of the
first supervised day. If it does not, `railway logs --service ops` and the notifier remain.

### 7.2 Operator commands

The operator commands of the runbook run inside the ops container, which holds
`OPERATOR_DATABASE_URL` (role `catalyst_operator`: no table grants, only the audited functions);
the trader never holds it:

```sh
railway ssh --service ops -- python -m catalyst_lab.managed_ops operator status
railway ssh --service ops -- python -m catalyst_lab.managed_ops operator pause --reason "..."
railway ssh --service ops -- python -m catalyst_lab.managed_ops operator flatten-all --reason "..."
railway ssh --service ops -- python -m catalyst_lab.managed_ops operator list-halts
railway ssh --service ops -- python -m catalyst_lab.managed_ops operator release-halt <id> --reason "..."
railway ssh --service ops -- python -m catalyst_lab.managed_ops operator resume --reason "..."
```

Their semantics are exactly the runbook's ("First supervised managed trade", step 8): pause
blocks new entries at once; `flatten-all` records the request and the running trader's next
account-safety tick cancels and closes everything, each broker call with its own one-use
authorization.

**Clearing an operator-only protection latch.** An invariant protection failure
(`PROTECTION_TICK_FAILED` of an invariant, such as a conflicting broker execution) and
`MANAGED_PROTECTION_PERSISTENT_FAILURE` never clear themselves; on the Mac the owner clears them
with `managed_ops clear-protection-latch`. In the cloud the same command runs in the trader's
own shell, over the trader's `MANAGED_DATABASE_URL` (the operator role has no way to write it):

```sh
railway ssh --service trader -- python -m catalyst_lab.cloud_runtime clear-protection-latch \
  --reason "What was checked, and why it is safe to clear (10 to 500 characters)"
```

It appends one audited `OPERATOR_PROTECTION_LATCH_CLEARED` event (`operator:
RAILWAY_TRADER_SHELL`; a retried command is the same event) and changes nothing else: no broker
call. The running trader then releases only the operator-only latches recorded before that
event. A durable execution halt (the persistent failure writes one) still needs its own
`operator release-halt` on ops after a clean reconciliation. Clear a latch only after you have
checked the positions and orders at Alpaca yourself; refusals name a code only
(`CLOUD_LATCH_CLEAR_REFUSED: OPERATOR_REASON_REQUIRED`, `... CLOUD_SECRET_MISSING
MANAGED_DATABASE_URL`).

### 7.3 Backups and checkpoints

The ops volume holds `audit-checkpoints/` (a full export weekly, incremental ones hourly, one
hash chain) and `backups/<UTC timestamp>/` (`catalyst_lab.dump`, `roles.sql` without passwords,
`manifest.json`), verified after each run and pruned to 14 days, always keeping the newest
verified one. The format is the Mac backup's, so `ledger_ops verify-manifest`, `prune` and the
restore `drill` accept it unchanged (a cloud backup restored in the local drill: PASSED in the
local proof). Failures raise `AUDIT_CHECKPOINT_FAILED` / `BACKUP_FAILED` (retried after 5 and 30
minutes).

**Off-host copy and drill.** Copy a backup to this Mac and drill it (runbook "Database backup and
restore drill"):

```sh
mkdir -m 700 -p ~/.local/share/catalyst-retest-lab/cloud-backups
railway volume files --volume ops-state download /backups/<timestamp> \
  ~/.local/share/catalyst-retest-lab/cloud-backups/<timestamp>
chmod 700 ~/.local/share/catalyst-retest-lab/cloud-backups/<timestamp>
chmod 600 ~/.local/share/catalyst-retest-lab/cloud-backups/<timestamp>/*
./run python -m catalyst_lab.ledger_ops drill --backup ~/.local/share/catalyst-retest-lab/cloud-backups/<timestamp> \
  --expected-manifest-sha256 <manifest_sha256 from /data/alarms.json on ops>
```

(`scp -r <ops-instance-id>@ssh.railway.com:/data/backups/<timestamp> ...` works too.)

The drill runs the backup's tools and server locally: a PostgreSQL 18 backup needs PostgreSQL 18
on this Mac (this Mac has 14; `localdb` finds the binaries on `PATH`). As a second layer, enable
Railway's own volume backups on `postgres` (Settings → Backups → Daily; kept 6 days,
[volume backups](https://docs.railway.com/volumes/backups)).

**A backup now, with its hashes** (package cloud-migrate). `/data/alarms.json` names a backup's
`manifest_sha256` only for the one tick that took it; the next tick, 30 seconds later, replaces
it. For a backup whose hashes you keep, take one on demand:

```sh
railway ssh --service ops -- python -m catalyst_lab.cloud_entry backup
```

It is exactly the daily backup (as `catalyst_backup`, into `/data/backups/<UTC time>`, verified,
pruned like the others), and it prints one JSON line: `backup`, `schema_version`, `audit_seq`,
`audit_head`, `event_count`, `manifest_sha256` and `migration_backup_reference` (7.8). Use
`cloud_entry backup`, not `cloud_runtime backup`: the SSH shell is root, and `cloud_entry` drops
to the ops service's own user first, so the files are owned like the daily ones (run as root,
`cloud_runtime backup` refuses `CLOUD_BACKUP_AS_ROOT_REFUSED`). Refusals and failures print a
code only (`CLOUD_BACKUP_REFUSED: ...`, `CLOUD_BACKUP_FAILED: ...`). Its `manifest_sha256` and
`audit_head` are the `--expected-manifest-sha256` and `--expected-head` of `verify-manifest` and
`drill` above.

### 7.4 Off-host alerts

Optional, as on the Mac (runbook "Off-host alerts"): create a Healthchecks.io check (period 30 s,
grace a few minutes), then set on `ops`:

- `CLOUD_NOTIFY_JSON` =
  `{"provider": "HEALTHCHECKS", "allowed_hosts": ["hc-ping.com"], "reminder_minutes": 30, "daily_head_hour_utc": 6, "timeout_seconds": 5}`
  (the private config's section without `ping_url_file` and `local_notification`);
- `NOTIFY_PING_URL` = the check's ping URL, sealed (paste it into the dashboard, or
  `pbpaste | railway variable set NOTIFY_PING_URL --service ops --skip-deploys --stdin`).

Redeploy ops. Every clean tick pings; new alarms go to `/fail`. The Healthchecks "no ping" alarm
is what covers a dead ops service or a Railway outage. A change of the Jev budget's tier
(section 4.1) raises `JEV_BUDGET_THROTTLED`, `JEV_BUDGET_TIGHT` or `JEV_BUDGET_EXHAUSTED` for 30
minutes: one `/fail`, then the success ping resumes while the tier stays in the status;
`JEV_BUDGET_STATUS_UNAVAILABLE` stays while the trader cannot read its meter (maintained trades
wait meanwhile).

### 7.5 Rotating secrets

1. `operator pause`. For a trader database password (`RISK_DATABASE_PASSWORD`,
   `JEV_DATABASE_PASSWORD`) the account must also be **flat**: `operator flatten-all` first if
   anything is open, until `operator status` shows `pending_flatten_count` 0 and the Alpaca
   Paper dashboard shows no position and no order. Why: from step 3 until the trader's
   redeploy in step 4 has finished, the running trader still holds the old password while
   PostgreSQL accepts only the new one, so every new database session of the trader is refused.
   That fails closed, but completely: no authorization, no protection write, no reconciliation,
   no recorded fill; an open position would have only its stop resting at Alpaca, and after 30
   seconds a durable persistent-failure halt. Flat, the gap costs nothing. For the other
   secrets confirm every position is protected (7.1 `positions`).
2. `python3 scripts/cloud_secrets.py --agent-token-file <file> --rotate NAME` (repeatable).
3. For a `*_DATABASE_PASSWORD`, change the role's verifier too:
   `CATALYST_PROVISION=rotate-passwords railway config apply`, then
   `railway up <staged release> --path-as-root --service provision --ci`; its log must show
   `"result": "ROTATED"` (it re-applies every cloud role's verifier from the current variables:
   unchanged passwords stay valid); then `railway config apply` to delete the provision service.
4. Redeploy the services that read the rotated value at once (the trader for its tokens and
   passwords, ops for the status token and its passwords, experiment for the public password):
   until they restart, the old value is the one they hold.
5. Confirm a clean reconciliation, then `operator resume`. Revoke old provider keys at the
   provider. Owner keys (Alpaca, TypeSafe) are edited in the dashboard (sealed variables can be
   edited, never read).

### 7.6 New releases

Commit, stage (5.7), then `railway up` the trader and ops from the same staged directory, and
the `jobs` service from it too (2.5). The
trader restarts through its full startup (stop-before-start, lease, reconciliation). A release
that adds a numbered migration cannot run on the cloud ledger until the owner has migrated it
(the trader's, ops' and jobs' role checks refuse a schema mismatch): that release follows 7.8,
not this plain order.

**The first release with the learning loop (package learning-app).** No migration (schema stays
24), no new trader or ops variable, one new service. In this order:

1. Stage the release from the merged commit (5.7).
2. `railway config plan`: expect exactly one change, *create service `jobs`* (its two variables,
   the cron schedule and restart NEVER; no volume, no domain). Then `railway config apply`.
3. `railway up <staged> --path-as-root --service trader --ci`, then `--service ops`, then
   `--service jobs`, all from the same directory. The trader serves the two new research-agent
   routes (`/market-outlooks`, `/post-mortems`), the research context V2 with lessons and
   guidelines V6, and reports `last_reconciliation_at` as the last clean reconciliation; ops
   gets the owner's learning commands (7.7). The experiment page is unchanged: no redeploy.
4. Check on Railway:
   - `railway config plan` shows no change.
   - The `jobs` service's settings show the cron schedule `30 5 * * *` and restart policy Never.
     Whether a deploy also starts one run is not documented; a run at any time is harmless (every
     record is idempotent).
   - After the first 05:30 UTC: `railway logs --service jobs` shows `JOBS STARTING
     release_commit=...`, five `JOBS STEP` lines and `JOBS DONE failed=...`, and the run ends
     (the deployment shows as completed). On the first night `market_reality` may log
     `REALITY_UNIVERSE_UNAVAILABLE` for a catch-up day that ended before any report or outlook
     recorded a universe; that is expected.
   - The reference resolved: no `JOBS STEP ... code=CLOUD_...` or `...ROLE...` failure, and a
     step logs `RECORDED` or `ALREADY_RECORDED`.
   - `railway ssh --service ops -- python -m catalyst_lab.cloud_runtime scorecard` prints the
     summary (the ops SSH session must see `AUDIT_DATABASE_URL`).
   - `railway ssh --service ops -- python -m catalyst_lab.cloud_runtime status` shows
     `last_reconciliation_at` every time, and the ops log no longer shows a lone
     `RECONCILIATION_STALE` between two clean ticks.
   - The research context with Muse's token answers `context_version: RESEARCH_CONTEXT_V2` and
     `report_format.guidelines_version: MUSE_RESEARCH_GUIDELINES_V6`.

**The first release with the monthly Jev budget (package jev-budget)** adds three trader
variables (section 4.1; `railway config plan` shows exactly those three additions on `trader`).
The release and the variables must arrive together: this release refuses to start without
them (`CLOUD_ENGINE_SETTING_MISSING`), and an older release refuses them as unknown engine
variables (`CLOUD_UNKNOWN_ENGINE_VARIABLE`), so a restart of either one without the other exits
2, and five such starts in ten minutes trip the crash-loop breaker (section 6). Set them without
restarting the running trader, then deploy the release, then apply:

```sh
railway variable set JEV_MONTHLY_BUDGET_USD=50 --service trader --skip-deploys
railway variable set JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD=0.042 --service trader --skip-deploys
railway variable set JEV_BYTES_PER_TOKEN=3 --service trader --skip-deploys
railway up ...           # the new release, as above
railway config plan      # must now show no change on trader
```

**The first release with the trade window (package review-window)** adds one trader variable,
`MANAGED_CRYPTO_WINDOW_JSON` (the deploy example's 240 minutes; `railway config plan` shows
exactly that addition on `trader`). As with the budget, the release and the variable must
arrive together (`CLOUD_ENGINE_SETTING_MISSING` without it, `CLOUD_UNKNOWN_ENGINE_VARIABLE` on an
older release):

```sh
railway variable set 'MANAGED_CRYPTO_WINDOW_JSON={"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240}' --service trader --skip-deploys
railway up ...           # the new release, as above
railway config plan      # must now show no change on trader
```

Trades open at the deploy keep the 24-hour versions they recorded; admissions after it record
the window. Changing the window later (2, 6, 12 or 24 hours) is a change of this variable (a
trader redeploy), not new code.

### 7.7 Learning records and the owner's commands (package learning-app)

The jobs service (2.5) records one `MARKET_REALITY`, one `DAILY_SCORECARD` and, weekly, one
`WEEKLY_REVIEW` event; they are read in the ops shell, over `AUDIT_DATABASE_URL` (role
`catalyst_app`: SELECT on the lab tables, no insert into any managed table, and every
transaction opened READ ONLY), never with a trader credential:

```sh
railway ssh --service ops -- python -m catalyst_lab.cloud_runtime scorecard [--day YYYY-MM-DD] [--full]
railway ssh --service ops -- python -m catalyst_lab.cloud_runtime market-review [--day YYYY-MM-DD]
railway ssh --service ops -- python -m catalyst_lab.cloud_runtime weekly-review [--week-end YYYY-MM-DD] [--now]
```

Each prints a short plain summary, then JSON. The day defaults to yesterday in New York, the week
to the latest recorded review. With no recorded event the command computes the same record
read-only, marked `"recorded": false` (`market-review` then reads Alpaca's public bars; `--now`
computes the latest finished week). Nothing is written. The owner's morning chat message is built
from `scorecard` and `market-review`, and Monday's from `weekly-review`; the public page is
unchanged. A `PROPOSE` verdict carries a contract-text draft for a named version: it changes
nothing until the owner approves it and it ships as a package.

### 7.8 Guarded schema migrations (package cloud-migrate)

The cloud ledger changes schema only through this owner-run step: the cloud counterpart of the
Mac's `catalyst-lab ledger-migrate` (runbook "Guarded migration"), with the same guards and a
fresh backup named first. It runs as the one-off `provision` service, the only place the
Postgres superuser connection ever exists, never on a runtime service:

```
python -m catalyst_lab.cloud_provision migrate --expect-current N --target M --backup <reference>
```

**What it refuses, changing nothing** (each refusal is one log line,
`CLOUD_MIGRATION_REFUSED: <CODE>`; a database failure is `CLOUD_MIGRATION_FAILED SQLSTATE <code>`;
never a password, a connection string or driver text):

- the backup reference missing or malformed (`MIGRATION_BACKUP_REFERENCE_REQUIRED`,
  `MIGRATION_BACKUP_REFERENCE_INVALID`), at another schema than `--expect-current`
  (`BACKUP_SCHEMA_MISMATCH`), more than 2 hours old (`BACKUP_TOO_OLD`) or more than 5 minutes
  ahead of the clock (`BACKUP_TAKEN_IN_THE_FUTURE`);
- `--target` not ahead (`LEDGER_TARGET_NOT_AHEAD`), a file between the two versions missing from
  the release, such as an unknown target (`LEDGER_MIGRATION_FILES_MISSING`), or `--target` not
  the release's own schema (`MIGRATION_TARGET_NOT_THIS_RELEASE`: a ledger left at any other
  version would refuse both this release and the one before it);
- a connection that is not the superuser (`PROVISION_SUPERUSER_REQUIRED`), a database that is
  not the provisioned cloud ledger (`CLOUD_LEDGER_IDENTITY_REQUIRED`), a ledger not at exactly
  `--expect-current` (`LEDGER_VERSION_MISMATCH`, also a second run of the same step);
- a backup that does not hold this ledger's own history: the event at the backup's audit
  sequence must be in this chain with the backup's head hash (`BACKUP_HEAD_NOT_IN_LEDGER`) and
  not be newer than the backup (`BACKUP_REFERENCE_INCONSISTENT`).

**What it does.** In ONE transaction, holding the audit lock every append takes (the running
trader's writes wait; the wait is bounded: `MIGRATION_LOCK_TIMEOUT` after 15 s, and
`MIGRATION_STATEMENT_TIMEOUT` after 300 s for one statement): applies the files in order as
`lab_owner`, then checks that the schema recorded the target (`LEDGER_TARGET_NOT_RECORDED`),
that no audit event was removed or rewritten (`AUDIT_HISTORY_REWRITTEN`), that a role a file
created stays NOLOGIN (a new cloud login role is refused, `MIGRATION_NEW_LOGIN_ROLE`: only
provisioning sets passwords), that no login role can alter the schema, rewrite the audit chain or
read `pg_authid` (`PROVISION_ROLE_TOO_BROAD`), that `lab_owner` still owns everything
(`PROVISION_OWNERSHIP_UNEXPECTED`), and that `catalyst_backup` can still read every relation, or
the daily backup would fail (`MIGRATION_BACKUP_GRANTS_INCOMPLETE`). It then appends the audited
`CLOUD_LEDGER_MIGRATED` event (the versions, each file's SHA-256, the backup reference,
`events_after_backup`, the release) and commits. Any refusal or failure rolls back every file. A
second session then reads the committed version and event back (`MIGRATION_NOT_VERIFIED`
otherwise). The log's last line is one JSON object with `"result": "MIGRATED"`.

**The backup it names** is the one 7.3's `cloud_entry backup` takes and prints: its
`migration_backup_reference` is `<backup name>.<schema>.<audit sequence>.<audit head>.<manifest
SHA-256>`, for example `20260929T013000Z.24.123456.<64 hex>.<64 hex>`. The command can see the
ledger, not the ops volume: it proves the backup is this ledger's recent history at the right
schema, and the event records the manifest hash, which `verify-manifest` checks against the files
(on ops, or on this Mac after 7.3's download). Should the migration ever need undoing, that
backup is restored into a new database (section 10, "Losing the cloud ledger"); a restore loses
the `events_after_backup` the event records and everything after them.

#### Migration 025 and `JEV_LIVE_REVIEW_POLICY_V2`: the order

The first release with a migration: 025 (the Jev review policy V2's runtime scope; contract text
in CONTRACT-RESOLUTIONS, 2026-09-28). Why the order below and no other:

- **The release pins schema 25.** Its trader, ops and jobs refuse a schema-24 ledger (the role
  checks). Deployed before the migration, the trader would stop the running one (stop-before-
  start), then retry for ten minutes and exit (`STARTUP_RETRY`), again and again, while only the
  stops resting at Alpaca protect the open trades. So never the release first.
- **The running release survives the migration, but cannot restart.** It checks the schema only
  when it starts. 025 appends no event and replaces only two functions: `lab.register_review_scope`,
  which it calls at startup only, and `lab.store_research_report`, the review service's intake,
  which the trader never calls. It keeps trading on schema 25 until it is replaced. But a restart
  of the old trader (a crash, a lost lease) would wait in `STARTUP_RETRY` until the new release
  is deployed, the old ops' hourly audit checkpoint refuses schema 25 (`AUDIT_CHECKPOINT_FAILED`
  until ops is redeployed), and the old jobs' 05:30 UTC run would refuse it. So deploy the
  release at once after the migration: minutes, not hours.
- **The variable comes last.** `JEV_REVIEW_POLICY_JSON` stays V1 in the deploy example until the
  release runs on schema 25: an older release refuses V2 at startup (`CLOUD_ENGINE_SETTING_INVALID
  MANAGED_APP_OR_REVIEW_CONFIGURATION`), and V2's scope needs 025. The new release accepts both,
  so switching (and switching back) is one variable.
- No pause is needed: the release's restart is 7.6's usual one (setups waiting for an entry
  resume, `CRYPTO_GAP_RESUME_V1`; open trades keep their stops at Alpaca).

**Steps** (from a checkout of the release commit: its `.railway/railway.ts` has the migrate mode):

1. Stage the release (5.7): `python3 scripts/cloud_release.py --out /tmp/catalyst-release-<commit>`.
2. A fresh backup on ops (7.3):

   ```sh
   railway ssh --service ops -- python -m catalyst_lab.cloud_entry backup
   ```

   Expect `"result": "MATCH"` and `"schema_version": 24`; copy `migration_backup_reference`.
   Optionally download that backup and `verify-manifest` it on this Mac with its
   `manifest_sha256` and `audit_head` (7.3). Steps 3 and 4 must follow within 2 hours.
3. Create the provision service in migrate mode, with the reference from step 2:

   ```sh
   export CATALYST_MIGRATE_EXPECT_CURRENT=24 CATALYST_MIGRATE_TARGET=25
   export CATALYST_MIGRATE_BACKUP='<migration_backup_reference>'
   CATALYST_PROVISION=migrate railway config plan    # expect: create service provision only
   CATALYST_PROVISION=migrate railway config apply
   ```

   The plan shows its start command, `python -m catalyst_lab.cloud_entry provision migrate
   --expect-current 24 --target 25 --backup <reference>`, and one variable,
   `MIGRATION_DATABASE_URL` (no role password: a migration sets none). A missing or malformed
   value stops the plan (`CATALYST_PROVISION=migrate needs ...`); the three `CATALYST_MIGRATE_*`
   values are read only together with `CATALYST_PROVISION=migrate`.
4. Run it:

   ```sh
   railway up /tmp/catalyst-release-<commit> --path-as-root --service provision --ci
   railway logs --service provision --lines 20
   ```

   Expect one JSON line: `"result": "MIGRATED"`, `"before": 24`, `"after": 25`,
   `"migrations": [{"version": 25, "file": "025_jev_review_policy_v2.sql", "sha256": ...}]`, the
   backup, `events_after_backup`, `"broker_requests": 0`. On a refusal or failure nothing
   changed: the ledger is still at 24 and the running release keeps trading; stop here, fix the
   cause (`BACKUP_TOO_OLD`: back to step 2, then step 3 with the new reference) and run step 4
   again, or delete the provision service (step 6) and leave the release undeployed. The one
   ambiguous ending is a lost connection at the commit (a `CLOUD_MIGRATION_FAILED` with no
   JSON line) or `MIGRATION_NOT_VERIFIED`: run step 4 again; `LEDGER_VERSION_MISMATCH` then
   means the migration had committed (go on with step 5), a `MIGRATED` line that it had not.
5. At once, deploy the release to the three services that pin the schema (the experiment page
   pins none and needs no redeploy):

   ```sh
   railway up /tmp/catalyst-release-<commit> --path-as-root --service trader --ci
   railway up /tmp/catalyst-release-<commit> --path-as-root --service ops --ci
   railway up /tmp/catalyst-release-<commit> --path-as-root --service jobs --ci
   ```

   Check: `railway logs --service trader --lines 20` shows `TRADER SERVING`, and
   `railway ssh --service ops -- python -m catalyst_lab.cloud_runtime status` shows
   `schema_version` 25, the new `release_commit` and `jev_breaker.policy_version`
   `JEV_LIVE_REVIEW_POLICY_V1` (still V1's scope, so its breaker keeps its epoch). An
   `AUDIT_CHECKPOINT_FAILED` raised by the old ops between steps 4 and 5 clears at its next
   checkpoint.
6. Delete the provision service, which holds the admin connection: `unset
   CATALYST_MIGRATE_EXPECT_CURRENT CATALYST_MIGRATE_TARGET CATALYST_MIGRATE_BACKUP`, then a plain
   `railway config apply` (confirm "Delete service provision"; the plan shows no other change).
7. Switch to V2 once step 5 checks out: one committed change to `deploy/private-paper.example.json`,
   its `JEV_REVIEW_POLICY_JSON` set to exactly V2's JSON (the file's formatting round-trips, so
   the diff is that one line; V2's keys are in V1's order, so within it only the version and the
   two budgets differ):

   ```sh
   ./run python -c 'import json, pathlib; from catalyst_lab.review_config import APPROVED_GATE1_V2
   f = pathlib.Path("deploy/private-paper.example.json"); d = json.loads(f.read_text())
   d["environment"]["JEV_REVIEW_POLICY_JSON"] = json.dumps(APPROVED_GATE1_V2)
   f.write_text(json.dumps(d, indent=2) + "\n")'
   git diff --stat    # 1 file changed, 1 insertion(+), 1 deletion(-)
   ```

   Run `./run pytest -q tests/test_railway_spec.py tests/test_cloud_config.py` (the spec accepts
   either approved policy there) and commit. Then `railway config plan` (expect exactly one
   change: the trader's `JEV_REVIEW_POLICY_JSON`) and `railway config apply`, which redeploys the
   trader with it (the same image). Check the status again: `jev_breaker.policy_version`
   `JEV_LIVE_REVIEW_POLICY_V2`, `epoch` 0 (V2 registers its own scope, with its own breaker,
   closed). From then on each attempt's permit lasts 8 s inside the unchanged 10 s deadline, and
   each Jev request records `reliability_policy_version` V2. Back to V1: revert that commit and
   apply again; V1's scope and its history are untouched.

**Rollback.** After step 4 the ledger stays at 25: no release built on schema 24 runs on it
again (its role checks refuse 25), so a bad release is fixed forward, in a new commit at schema
25. The step 2 backup is for losing the ledger, not for undoing a release.

**Later migrations** follow the same steps with their own versions. Before choosing this order,
check the two things that made it safe for 025: that the running release keeps working on the
new schema until it is replaced (a migration that drops or renames something the running code
reads needs `operator pause`, or the trader stopped, first), and that no file creates a cloud
login role (refused here; provisioning sets passwords).

## 8. Muse against the cloud

Muse does all research, outside the app, and is its only outside caller. It talks only to the
trader's public HTTPS domain (`railway domain --service trader`), with its own agent token in
`Authorization: Bearer`. [MUSE-CONNECTION.md](MUSE-CONNECTION.md) (package muse-connection) is
the API walkthrough; the cloud side of it is:

- **One credential.** `MANAGED_AGENT_TOKENS_JSON` is exactly `{"muse": "<token>"}`. The token is
  the file `scripts/cloud_secrets.py` wrote (5.4): absolute, outside the repository,
  `~/Documents` and iCloud Drive, mode 0600. You hand that file to Muse; it is never pasted into
  chat, a prompt or a repository. Muse never receives `MANAGED_API_TOKEN` (the legacy identity,
  never issued), the status token or the operator token.
- **Only Muse's routes, only as Muse.** The agent token is accepted only on the research-agent
  routes (research context and reports, its cycles' outputs and evidence, positions and their
  news, its 24-hour reviews and their answers, raising and answering exit flags), and only for
  records of agent `muse`: a report whose `agent` block names another agent, or a review of another agent's
  trade, is refused `403 AGENT_IDENTITY_MISMATCH`. Every other route answers 401 or 403.
- **Jev stays inside the app.** The trader alone calls Jev (TypeSafe); Muse sends its research
  and its answers and never sees a TypeSafe credential, and selection reviews stay blind to
  which agent proposed a pick.
- **Rotating Muse's token:** `scripts/cloud_secrets.py --agent-token-file <file> --rotate
  MANAGED_AGENT_TOKENS_JSON` replaces the variable and the file together (7.5); redeploy the
  trader, then hand Muse the new file. The old token stops working with the redeploy.

## 9. The first supervised day

Watch it live for the first day, as the runbook's "First supervised managed trade" prescribes
(its steps, with the commands of section 7). Before admitting any research:

- Status (7.1): `worker_state` `RUNNING`; `executor_ownership` `EXCLUSIVE`;
  `last_reconciliation_at` within 30 s and `entry_ready` `true` (a clean startup reconciliation
  against the paper account); `trade_stream_connected` `true`; `market_streams.CRYPTO` `true`
  once a pair is subscribed; `error_code` `null`; `schema_version` the provisioned one;
  `release_commit` the staged commit; `management_review_enabled` `true` (the cloud's
  `MANAGED_MANAGEMENT_REVIEWS=ENABLED`); `jev_breaker.state` `CLOSED`.
- `operator status`: `active_count` 0 and `pending_flatten_count` 0.
- `/data/alarms.json` on ops: no alarm (the Mac watchdog's `MUSE_*` alarms watch a Mac-hosted
  Muse worker; in the cloud Muse is an outside caller and has no alarm here).
- Railway: exactly one active deployment of `trader`.

Only now hand Muse the trader's `https://` domain and its token file (5.4, section 8). Then let
Muse send its first report ([MUSE-CONNECTION.md](MUSE-CONNECTION.md)) and watch the path
WATCHING → ENTRY_PENDING → ORDER_SUBMITTED → OPEN → CLOSED in the status and the Alpaca Paper
dashboard. `positions` shows each open trade as the trader holds it (stop, target, a raised
stop still being replaced, the maintenance version, the next 24-hour review):

```sh
railway ssh --service ops -- python -m catalyst_lab.cloud_runtime positions
```

**Jev's maintenance, every minute.** Once a trade is open, Jev reviews it at every completed
minute (`CRYPTO_MAINTENANCE_V2`, package answer-rules; checked with the real Jev on a simulated
venue, `artifacts/real-jev-v2-2026-09-28`). Trades admitted from package jev-budget on record
`CRYPTO_MAINTENANCE_V3`: the same reviews, every minute while the monthly Jev budget allows it
(section 4.1); `jev_budget` in the status should show `tier` `NORMAL` on the first day. In the
status, `trade_maintenance` shows `management_reviews` `ENABLED`, a current `last_pass_at`,
`failing_reviews` (empty when healthy) and the pending exit flags; `day_reviews` shows the
24-hour reviews. The ops watchdog raises the same maintenance and day-review alarms as the Mac
watchdog.

**Watch the first stop replacement at Alpaca.** This path has never been verified against
Alpaca for crypto. When a maintenance answer raises the stop, `positions` shows the new `stop`
with `stop_replace` set; the protection loop then replaces every resting stop-limit of the
position, in place (`PATCH_REPLACE`: one price-only PATCH per order) or by cancelling it and
placing a new one (`CANCEL_THEN_PLACE`), each call under its own one-use authorization. Until the
broker shows the raised levels the trader enforces the raised stop itself: a bid at or below it
exits at once (`STOP_CROSSED_DURING_REPLACE`). It is done when `stop_replace` is empty again
(`STOP_REPLACED` records the path that ran). In the Alpaca Paper dashboard the position must then
have resting stop-limit orders at the raised stop covering its whole quantity: no order left at
the old stop, no duplicate, no position without a stop. If it does not (or `stop_replace` stays
set for more than a few minutes, or `error_code` names a protection latch), apply the abort rule
below, then switch maintenance off.

**Switching maintenance off quickly** (if maintenance misbehaves). The switch is a trader
redeploy, and **no trader runs during a redeploy**: from the old trader's stop to the new one's
first reconciliation (a minute or two; longer if Railway rebuilds the image) nothing manages a
position, nothing exits one at its target or time, nothing replaces a stop. Only the stop-limit
orders resting at Alpaca protect it. So first make sure they do:

1. `operator pause --reason "maintenance off"` (7.2): no new entries meanwhile.
2. Check every open position, both ways:
   - `positions` (7.1): no position with `stop_replace` set (a raised stop still being
     replaced) and none with `protection_state` `HALTED` (its protective controller stopped);
   - the Alpaca Paper dashboard: each open position has resting stop-limit orders at its
     current stop covering its whole quantity, and no order left at an older stop.

   If either check fails for any position, do not redeploy onto it: run `operator flatten-all
   --reason "maintenance off"` and wait for `operator status` to show `pending_flatten_count` 0
   with `last_outcome: COMPLETED` and the dashboard to show nothing open (runbook step 8). Then
   continue.
3. `railway variable set MANAGED_MANAGEMENT_REVIEWS=DISABLED --service trader`. Without
   `--skip-deploys` this starts the new trader deployment at once.
4. Change the same override in `.railway/railway.ts` to `"DISABLED"` and commit, so that no
   later `railway config apply` switches it back (`railway config plan` must show no change;
   the spec test accepts either value).
5. Wait for the new trader: the status shows `worker_state` `RUNNING`, `executor_ownership`
   `EXCLUSIVE`, a fresh `last_reconciliation_at` and `management_review_enabled` `false`. Then
   `operator resume` if you want entries again.

What it does to open trades after the redeploy: every open trade keeps the stop and target it
has at that moment, and protection, targets, time exits and the partial-entry rule run
unchanged. Jev is asked nothing more: no maintenance review (one `POSITION_REVIEW_SKIPPED` per
trade), each open trade's next 24-hour review exits it at T (`JEV_REVIEWS_DISABLED`), and an exit
flag ends with no answer while the trade stays. Switching back is the same steps with `ENABLED`.

**Halt and flatten.** `operator pause --reason "..."` stops new entries at once (protection
keeps running). `operator flatten-all --reason "..."` closes everything on the trader's next
account-safety tick; confirm with `operator status` (`pending_flatten_count` 0,
`last_outcome: COMPLETED`) and the Alpaca Paper dashboard.

**Abort rule** (unchanged from the runbook): if the status `error_code` names a protection latch
(`PROTECTION_TICK_FAILED` or `MANAGED_PROTECTION_PERSISTENT_FAILURE`) for more than 10 seconds
while a position is open, run `operator flatten-all` and `operator pause` at once and leave the
trader running (it executes the flatten). If the trader itself is down, close the position in the
Alpaca Paper dashboard. Record the abort as a dated PHASES.md note.

## 10. Rollback

- **A bad release:** stage the previous good commit and `railway up` the trader and ops from it
  (or redeploy the previous deployment in the dashboard). The schema changes only through
  provisioning and the owner's guarded migration (7.8), so any release built on the ledger's
  current schema runs against it; after a migration, none built on an older schema does, and a
  bad release is fixed forward.
- **A bad variable change:** revert the file, `railway config apply`, redeploy.
- **Stop trading in the cloud:** `operator pause`, `operator flatten-all`, confirm flat at
  Alpaca, then remove the trader's deployment (`railway down --service trader`, or in the
  dashboard). Keep `postgres` and `ops` running for evidence and backups, and copy the latest
  backup off Railway (7.3).
- **Back to the Mac:** only after the cloud trader is removed and the account is flat. The retired
  Mac ledger stays retired (its history ends at the move); run the Mac engine on a new local
  ledger, never on the cloud ledger and never on the retired one. Un-retiring a marker by hand is
  an owner decision outside this tooling.
- **Losing the cloud ledger:** restore the newest verified backup into a new, empty database with
  the runbook's "Genuine restore" procedure (the owner, with a drill first); reconcile against the
  broker before anything trades.

## 11. Proven locally, and only provable on Railway

Local evidence: `artifacts/cloud-2026-09-27/` (the proof and the IaC validation) and the tests
`tests/test_cloud_*.py`, `tests/test_railway_spec.py`, `tests/test_ledger_retire.py`,
`tests/test_research_agent_cloud.py`. They are fixtures and a disposable PostgreSQL 14 on this Mac,
not a Railway deployment and not the broker.

| Claim | Local proof | Only on Railway |
| --- | --- | --- |
| Provisioning an empty database; refusing a provisioned or non-empty one; SCRAM logins; statement logging off; ownership; audited identity | yes (PostgreSQL 14) | the same on PostgreSQL 18, over the private network |
| TLS with `sslmode=require` for every connection (the provisioner through the entrypoint, trader, ops, `pg_dump`), plain TCP rejected; the entrypoint's `HOME` for libpq | yes (a self-signed certificate; psycopg's libpq 18, `pg_dump` 14) | Railway's certificate; `pg_dump` 18 under `HOME=/nonexistent` |
| Startup refusals (`.env`, `TYPESAFE_ENV_FILE`, placeholders, missing secrets, unknown names) | yes | Railway's variable resolution, sealed values inside references |
| `/health` while starting and serving, without a database session once serving; status only with the token; every route authenticated | yes | Railway's healthcheck and domain routing |
| A second trader refused by the lease, waiting, then taking over; exit 75 on lease loss | yes (two processes) | Railway's stop-before-start with a volume; overlap 0 and draining 30 honored |
| Crash-loop state on the volume surviving a restart | yes (a temp "volume") | on a Railway volume, owned by uid 10001 after the entrypoint |
| The entrypoint refuses a state directory off the volume (or reached through a symlink) before it creates or chowns anything | yes (tests; the chown and the drop injected, this Mac's tests never run as root) | the real root branch on Railway |
| Ops: watchdog alarms, hourly checkpoint, daily verified backup, restore drill of that backup; the backup role reads only `lab` (never `pg_authid`) | yes | `pg_dump 18` against Railway's Postgres 18 with those grants; volume persistence |
| An invalid `MANAGED_MANAGEMENT_REVIEWS` refuses the trader at once; either valid value passes the spec | yes (tests) | the trader's log on Railway |
| Clearing an operator-only protection latch from the trader's shell | yes (tests: the running runtime releases the latch) | `railway ssh` seeing the trader's variables |
| `ledger-retire` refuses while the Mac executor, an app session or the app port is alive, holds the executor lock while retiring | yes (disposable cluster) | the owner's Mac ledger on the day |
| Release identity: staged commit sealed and reported as `release_commit` | yes (staged tree, sealed locally) | the Dockerfile build on Railway |
| The IaC file evaluates and type-checks against the real SDK 3.11.0 | yes (offline) | `railway config plan/apply` and Railway accepting every deploy field |
| Fresh ledger vs an account with history | yes (mock venue) | the real Alpaca Paper account's state |
| `MANAGED_MANAGEMENT_REVIEWS=ENABLED` accepted by the cloud profile; the owner's `positions` view | yes (tests; the proof's trader has no position monitor) | Jev's maintenance of a real trade; the first stop replacement at Alpaca for crypto (never verified) |
| Muse's token: exactly `{"muse": ...}`, accepted only on the agent routes | yes (tests) | Muse calling the Railway domain (package muse-connection) |
| The `jobs` cron (package learning-app): each step independent, exit 0, the 30-minute end, refusals of broker and Jev keys; the IaC service compiles with the real SDK | yes (tests on disposable ledgers; the real SDK offline) | Railway running it at 05:30 UTC and ending the deployment; the `${{trader.RISK_DATABASE_PASSWORD}}` reference resolving on another service; Alpaca's public bars from Railway |
| The monthly Jev budget (package jev-budget): the meter from request bytes, the tiers, V3's cadence, the alerts, refusals of a missing or invalid setting | yes (tests on disposable ledgers; a month-long simulation of the guard) | TypeSafe's bill against the meter's estimate (recalibrate `JEV_BYTES_PER_TOKEN`); a real month's tier changes and alerts |
| The guarded migration (package cloud-migrate): the ops shell's backup and its reference, then 24 to 25 on a Railway-shaped ledger provisioned at 24 while events keep arriving; every refusal (backup, versions, files, ledger, lock, faulty files) changing nothing; the migrated ledger's next backup restored in the drill at 25 | yes (tests on disposable PostgreSQL 14) | PostgreSQL 18; the provision service's start command and reference; `railway ssh` running `cloud_entry backup` as uid 10001 with the volume and variables in view |
| `JEV_LIVE_REVIEW_POLICY_V2`: its own scope, 8-second permits, a 5 s answer kept under V2 and cut off under V1, V1 unchanged | yes (tests, mock provider) | TypeSafe's stalls against the 8 s budget |
| Trading (streams, fills, exits) | no: the proof has no broker | the first supervised day |

## 12. References

Railway documentation, read 2026-09-27:
[Infrastructure as Code](https://docs.railway.com/infrastructure-as-code),
[IaC reference](https://docs.railway.com/infrastructure-as-code/reference),
[railway config](https://docs.railway.com/cli/config),
[Config as Code (deprecated)](https://docs.railway.com/config-as-code),
[deployments reference](https://docs.railway.com/deployments/reference),
[deployment teardown](https://docs.railway.com/deployments/deployment-teardown),
[healthchecks](https://docs.railway.com/deployments/healthchecks),
[restart policy](https://docs.railway.com/deployments/restart-policy),
[start command](https://docs.railway.com/deployments/start-command),
[volumes reference](https://docs.railway.com/volumes/reference),
[volume backups](https://docs.railway.com/volumes/backups),
[variables](https://docs.railway.com/variables),
[variables reference](https://docs.railway.com/variables/reference),
[template functions](https://docs.railway.com/templates/create),
[PostgreSQL](https://docs.railway.com/databases/postgresql),
[private networking](https://docs.railway.com/networking/private-networking/how-it-works),
[TCP proxy](https://docs.railway.com/networking/tcp-proxy),
[plans and pricing](https://docs.railway.com/pricing/plans),
[railway up](https://docs.railway.com/cli/up),
[railway variable](https://docs.railway.com/cli/variable),
[railway domain](https://docs.railway.com/cli/domain),
[railway ssh](https://docs.railway.com/cli/ssh),
[railway logs](https://docs.railway.com/cli/logs),
[cron jobs](https://docs.railway.com/reference/cron-jobs) and
[cron, workers and queues](https://docs.railway.com/guides/cron-workers-queues) (read
2026-09-28, package learning-app).
The IaC SDK: npm `railway` 3.11.0 (its published types, `dist/iac/index.d.ts`). The Railway CLI
source read for IaC behavior: `github.com/railwayapp/cli`, `src/iac/compiler.rs`,
`src/iac/change_set.rs`, `src/iac/eval.rs`.
