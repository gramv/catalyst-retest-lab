# Operations runbook

Plan package 4.8 adds the other sections (handover, reboot recovery, alerts, operator actions).
This file holds the backup and restore section (package 4.6), the credentials and secret
rotation section (package 0.9 script; the rotation procedure from 4.8) and the guarded
migration of the account ledger (package 0.8).

**Railway (package cloud, 2026-09-27):** running the managed engine 24/7 on Railway instead of
this Mac (the services, provisioning, secrets, retiring the Mac ledger with
`catalyst-lab ledger-retire`, the one-executor rules, operator commands, backups and the first
supervised day) is [RAILWAY-DEPLOYMENT.md](RAILWAY-DEPLOYMENT.md). Prepared and proven locally;
nothing is deployed. The sections below describe the Mac-hosted private config v2; once the Mac
ledger is retired, its app and Muse components refuse to start (`LEDGER_RETIRED`).

## Database backup and restore drill

`python -m catalyst_lab.ledger_ops` backs up a ledger cluster, verifies a backup, proves that it
restores, and applies retention. It uses only the local PostgreSQL tools (`pg_dump`, `pg_dumpall`,
`pg_restore`, `psql`, `initdb`, `pg_ctl`). It makes no broker, provider or network call. It never
starts, migrates or writes to the cluster it backs up.

Recovery point targets: 24 hours for the database (one backup per day) and 1 hour for audit
evidence. The hourly figure comes from the audit checkpoint component (`interval_seconds: 3600`;
see "Audit checkpoint verification and evidence recovery" in `PAPER-WIRING-2026-09-20.md`), not
from this tool.

### What a backup contains

Each run creates `<destination>/<UTC timestamp>/` (for example `20260924T031500Z`) with mode 0700.
An existing directory is never overwritten (`BACKUP_DESTINATION_EXISTS`). It holds three mode-0600
files:

- `roles.sql`: `pg_dumpall --roles-only --no-role-passwords`. It holds every role with its
  attributes and memberships. It never holds a password or a password hash.
- `catalyst_lab.dump`: `pg_dump --format=custom --snapshot=<id>` of the `catalyst_lab` database.
- `manifest.json`: `format`, `scope`, `taken_at`, `cluster_system_identifier`,
  `schema_version`, `audit_head`, `audit_seq`, `event_count`, `table_counts` (every table),
  `dump_sha256`, `roles_sha256` and `tool_versions`. It holds no connection string, path or
  credential.

The backup opens one `REPEATABLE READ READ ONLY` transaction and exports its snapshot. It reads the
schema version, the audit head (`SELECT event_hash, seq FROM lab.trade_events ORDER BY seq DESC
LIMIT 1`), the event count and every table's row count in that transaction, and `pg_dump` imports
the same snapshot. Events appended while the backup runs are in neither the dump nor the manifest,
so a backup can run while the runtime keeps writing.

### Commands

```sh
PY='/REQUIRED/release/venv/bin/python'
ROOT='/REQUIRED/ledger-directory'          # holds postgres/ and socket/
BACKUPS='/REQUIRED/private-backups'        # mode 0700, on this Mac; never iCloud
"$PY" -m catalyst_lab.ledger_ops backup --root "$ROOT" --destination "$BACKUPS"
"$PY" -m catalyst_lab.ledger_ops verify-manifest --backup "$BACKUPS/<timestamp>" \
  --expected-head "$INDEPENDENT_HEAD" --expected-manifest-sha256 "$MANIFEST_SHA256"
"$PY" -m catalyst_lab.ledger_ops drill --backup "$BACKUPS/<timestamp>" \
  --expected-head "$INDEPENDENT_HEAD" --expected-manifest-sha256 "$MANIFEST_SHA256"
"$PY" -m catalyst_lab.ledger_ops prune --destination "$BACKUPS" --dry-run
"$PY" -m catalyst_lab.ledger_ops prune --destination "$BACKUPS"
```

Every command prints one JSON object. A refusal exits non-zero with
`{"command", "result": "REFUSED", "code"}`. The two `--expected-*` values are optional
independent anchors. The head is the one recorded from the read-only audit export (plan 0.8 step
3). `manifest_sha256` is printed by `backup` and should be kept outside the backup directory.

- **Owner ledgers.** If `--root` has a `LEDGER.json`, the backup is refused
  (`OWNER_LEDGER_REQUIRES_ALLOW_FLAG`) unless `--allow-owner-ledger` is given. That flag is the only
  way this tool reads an owner ledger, and only the owner passes it. An unreadable marker is still
  refused. A backup destination or drill scratch root that has `LEDGER.json` is always refused.
- **Running cluster only.** In the maintenance window (plan 0.8 steps 2–4), start the ledger with
  `pg_ctl -D "$ROOT/postgres" -l "$ROOT/postgres.log" -w start`. Never use `localdb.start` or
  `catalyst-lab dev-init` for this. A stopped cluster is refused with `LEDGER_CLUSTER_NOT_RUNNING`,
  and the tool leaves it stopped.
- **`verify-manifest`** recomputes both SHA-256 values and checks the roles allowlist. It restores
  nothing. It needs private files (owner, mode 0600, directory 0700), like `verify-checkpoint`.

### Restore order: why stored hashes survive

The drill first applies the roles script with `psql -X -v ON_ERROR_STOP=1 --single-transaction`.
It then makes one `pg_restore --exit-on-error --create` run as `lab_owner`, the owner of every table.
`pg_restore` does its work in three stages:

1. It creates the schema, tables, functions and views.
2. It loads the rows with `COPY`.
3. It creates indexes, constraints and triggers.

While rows load in stage 2, no `BEFORE INSERT` trigger exists yet: not `stamp_event`, `audit_jev`
or any guard. So nothing restamps `seq`, `previous_hash`, `event_hash`, `created_at` or
`event_body`, and no trigger appends extra audit rows. `--disable-triggers` is not used. It applies
only to data-only restores and needs a superuser on the restored side.

The initdb bootstrap role is already `lab_owner`, the superuser the drill restores as. The drill
therefore removes the `CREATE ROLE lab_owner;` line and the `ALTER ROLE lab_owner WITH ...;` line
from the roles script and applies everything else unchanged: the restore role keeps its bootstrap
attributes. On a Mac ledger that ALTER line only repeats them; a cloud ledger (package cloud)
keeps `lab_owner` NOLOGIN, which must not reach the drill cluster (2026-09-27; before, the ALTER
line was applied). Generated columns (`request_hash`, `response_hash`, `record_purpose`) are
recomputed from the same stored inputs.

### What the drill checks

The drill creates a disposable cluster under `--scratch-root` (default `/tmp`, kept short because of
the Unix-socket path limit). It uses the same initdb arguments and private-socket settings as
`localdb.start`, with trust authentication on a 0700 socket and no TCP. It never calls
`localdb.start`, because that would migrate the restore. The bytes it restores are a private copy
of exactly the bytes it hashed. The drill passes only if all of the following hold:

- Both file hashes match. The roles script contains only allowlisted `SET`, `CREATE ROLE`,
  `ALTER ROLE ... WITH <attributes>` and `GRANT <role> TO <role>` lines, with no password, no psql
  meta-command and no second statement on a line.
- The archive's database is `catalyst_lab`, and the drill cluster's system identifier differs from
  the manifest's.
- Every role that the migrations up to the manifest's schema version create exists, and so does
  every role in the roles script. At schema 15 to 20 these are `catalyst_app`, `catalyst_risk`,
  `catalyst_jev`, `catalyst_review`, `catalyst_review_operator`, `catalyst_reporting` and
  `catalyst_operator`; a schema-13 ledger has no `catalyst_operator`.
- Each of those roles:
  - has none of SUPERUSER, CREATEROLE, CREATEDB, REPLICATION or BYPASSRLS;
  - has no CREATE on schema `lab`;
  - cannot UPDATE, DELETE or TRUNCATE `lab.trade_events`;
  - can log in if it is a login role.
- `catalyst_app` passes the `Repository.check_role` fields against the manifest's schema version.
  `Repository.check_role()` itself also runs when that version equals the code's. A pre-migration
  backup reports `NOT_APPLICABLE_SCHEMA_<n>_CODE_<m>` for it.
- `lab_owner` owns the `lab` schema and all of its relations and functions.
- The schema version, the audit head, the sequence number, the event count and every table's row
  count equal the manifest.
- `catalyst_lab.audit.verify_events` passes over the restored events, and its head equals the
  manifest head.
- Every trigger in the archive exists and is enabled. This includes `stamp_event`,
  `immutable_rows` and `immutable_truncate` on `lab.trade_events`.
- An UPDATE and a DELETE of the restored head are refused with
  `append-only relation: mutation forbidden`.
- A probe append by `catalyst_app` gets `seq = audit_seq + 1` and `previous_hash = audit_head`.
  The probe is rolled back.

Afterwards the drill stops the cluster and deletes it. If the server will not stop, the directory
is left in place and reported as `DRILL_SCRATCH_NOT_DESTROYED`.

The report's scope is `AUDIT_AND_LEDGER_RESTORE_DRILL`. The drill proves that a restore works. It
says nothing about broker positions, orders, reservations, a running session or trading readiness.

The drill runs the backup's SQL as the disposable cluster's superuser. Before drilling a copy that
has left this Mac, check it with `--expected-manifest-sha256`.

### Retention and off-host copies

`prune` deletes a directory only if all of the following hold:

- its name is a timestamp;
- it is not a symlink;
- it holds exactly the three files;
- its manifest and both file hashes verify, and its name matches `taken_at`;
- it is older than the retention (default 14 days).

`prune` always keeps the newest verified backup, even when it is older than the retention.
Directories that fail verification, and any other entries, are reported and never touched. Run it
with `--dry-run` first.

The daily LaunchAgent rendered by `managed_ops render-supervisor` (plan 0.7/4.1) runs
`managed_ops backup`, which performs `backup`, then `verify-manifest` against the new manifest's
hash, then `prune` with the configured retention (wired 2026-09-24; the ledger root is the
directory of the configured marker file). The off-host destination is an
owner ruling, and it must never be iCloud. Until both exist, backups are manual and exist only on
this Mac.

### Genuine restore (owner only)

A drill never replaces a ledger. After a real loss of the ledger database:

1. Stop every component (runtime, API, dashboard, Muse intake, audit), and confirm that no process
   holds the executor lease. Open broker positions are reconciled against the broker, never against
   the backup.
2. Drill the chosen backup with both independent anchors.
3. Restore into a new, empty directory. Never restore over an existing ledger. Use the same steps
   as the drill:

   ```sh
   NEW='/REQUIRED/new-ledger-directory'   # absolute path that must not exist yet
   B="$BACKUPS/<timestamp>"
   mkdir -m 700 "$NEW" "$NEW/socket"
   initdb -D "$NEW/postgres" -U lab_owner --auth-local=trust --auth-host=reject \
     --encoding=UTF8 --no-locale
   printf "\nlisten_addresses = ''\nport = 55437\nunix_socket_directories = '%s'\nunix_socket_permissions = 0700\n" \
     "$NEW/socket" >> "$NEW/postgres/postgresql.conf"
   pg_ctl -D "$NEW/postgres" -l "$NEW/postgres.log" -w start
   ADMIN="host='$NEW/socket' port=55437 dbname=postgres user=lab_owner"
   grep -vx -e 'CREATE ROLE lab_owner;' -e 'ALTER ROLE lab_owner WITH .*;' "$B/roles.sql" |
     psql -X -q -v ON_ERROR_STOP=1 --single-transaction -d "$ADMIN" -f -
   pg_restore --exit-on-error --create -d "$ADMIN" "$B/catalyst_lab.dump"
   ```

4. Confirm that the restored head, sequence number and count equal the manifest. Then, before any
   executor starts, append `LEDGER_RESTORED_FROM_BACKUP` through the normal audited path. This is a
   `SYSTEM_EVENT` inserted as `catalyst_app`, the same INSERT that `Repository.append_event`
   performs. `lab.stamp_event` chains it onto the restored head. The payload carries:
   - the backup directory name;
   - `manifest_sha256`;
   - the restored `audit_head`, `audit_seq` and `event_count`;
   - the drill's `result`;
   - the head and count of the newest verified audit checkpoint;
   - the reason for the restore.

   ```sh
   psql -X -v ON_ERROR_STOP=1 \
     -d "host='$NEW/socket' port=55437 dbname=catalyst_lab user=catalyst_app" \
     -c "INSERT INTO lab.trade_events(strategy_version, event_type, payload_json)
         VALUES ('CATALYST_RETEST_V1', 'SYSTEM_EVENT', jsonb_build_object(
           'kind', 'LEDGER_RESTORED_FROM_BACKUP', 'backup', '<timestamp>',
           'manifest_sha256', '<sha256>', 'restored_audit_head', '<head>',
           'restored_audit_seq', <seq>, 'restored_event_count', <count>,
           'drill_result', 'PASSED', 'checkpoint_head', '<head>',
           'checkpoint_event_count', <count>, 'reason', '<why>'))"
   ```

   Events after the backup's head survive only in the audit checkpoints. They stay verified
   evidence files and are never re-inserted, because re-inserting would give them new hashes.
5. Write `LEDGER.json` for the new directory and point the private configuration at it. Admit new
   reports only after a clean startup reconciliation.

No code in this package writes that event, restores over an owner ledger or touches an owner ledger
without `--allow-owner-ledger`.

## Credentials and secret rotation

Every secret is an owner-only mode-0600 file inside the owner-only private directory, and the
LaunchAgent plists carry only the configuration path.

| Secret | Where it lives | Read by |
| --- | --- | --- |
| Alpaca Paper key ID and secret | `environment.APCA_API_KEY_ID` and `environment.APCA_API_SECRET_KEY` in the private configuration | the app launcher (`AlpacaCredentials.from_env`) |
| TypeSafe key | the file named by `environment.TYPESAFE_ENV_FILE` (an absolute path in the private directory; one line `TYPESAFE_API_KEY=…`) | `jev_secrets.typesafe_key()`; without this setting it falls back to the working directory's `.env` |
| Muse, status and operator tokens | `muse.token_file`, `status.token_file`, `operator.token_file` | the app (all three), the Muse worker (muse), the watchdog (status) |

`scripts/set_private_secret.py` writes them. Values are typed at a no-echo prompt or generated
in-process, never passed on the command line (so they reach no shell history or process list),
never printed or logged, and `check` reports only presence and protection, never a value or its
length:

```
python scripts/set_private_secret.py --config /PRIVATE/private.json alpaca
python scripts/set_private_secret.py --config /PRIVATE/private.json typesafe
python scripts/set_private_secret.py --config /PRIVATE/private.json token muse --generate
python scripts/set_private_secret.py --config /PRIVATE/private.json token status --generate
python scripts/set_private_secret.py --config /PRIVATE/private.json token operator --generate
python scripts/set_private_secret.py --config /PRIVATE/private.json check
```

An existing secret is replaced only with `--replace`, atomically and in the same directory;
symlinks are refused; the configuration is re-validated before it replaces the old file. Nothing
here contacts the broker or provider: a written credential is proved only by the supervised launch
(plan 0.10). Run `managed_ops preflight` afterwards; it reports missing values by name only.

Rotation, in the same order for every secret:

1. `managed_ops operator pause`: no new entries; protection and exits keep running.
2. Confirm the account is flat or every position is protected (the status endpoint and a
   read-only broker check).
3. Generate the new value at the provider (Alpaca dashboard, TypeSafe console), or with
   `--generate` for a role token.
4. Inject it with `--replace`.
5. Restart the components that read it: the app for the Alpaca keys, the TypeSafe key and any
   token; the Muse worker for the muse token; the watchdog for the status token.
6. Confirm a clean reconciliation, then `managed_ops operator resume`.
7. Revoke the old value at the provider.

A leaked key is rotated immediately, before anything else. The ledger is append-only and cannot be
scrubbed, so a leak is recorded as a dated note in PHASES.md, never edited away. Before Muse runs
unattended, verify that its pinned read-only Codex sandbox cannot read the private directory; if it
can, run Muse as a separate macOS user (plan 0.9 canary, an owner action).

## Guarded migration of the account ledger (plan 0.8, owner action, one maintenance window)

(The Mac ledger's step. The cloud ledger's counterpart, `cloud_provision migrate` with a fresh
ops backup named first, is RAILWAY-DEPLOYMENT.md 7.8.)

The live managed ledger `~/.local/share/catalyst-retest-lab/managed-real-20260919-a` is at
schema 20: the owner migrated it from 13 on 2026-09-25, before the first real managed trade. The
code requires schema 23 (migration 023, package topk-v2, 2026-09-27). The procedure below was
rehearsed on populated disposable ledgers by `tests/test_owner_migration_rehearsal.py` (13 to the
current schema, 20 to 21, and 21 to 23); the expected numbers are that test's assertions. The schema-8 V1 runtime ledger is never migrated:
mark it `ARCHIVED`, which refuses both migration and initdb.

1. Nothing is running: `lsof -iTCP -sTCP:LISTEN` shows nothing on 8765, 8768–8770 or 8780, no
   catalyst or project `postgres -D` process exists, and `launchctl list | grep local.catalyst`
   is empty (the old `app.pid` is stale).
2. Write the marker first, so `dev-init` is blocked from here on: `LEDGER.json` in the ledger
   root, mode 0600, containing `{"role": "ACCOUNT_LEDGER"}`. `ledger-migrate` accepts this marker.
3. Start only the cluster, with `pg_ctl` and never `localdb.start` or `dev-init`:
   `pg_ctl -D <ledger>/postgres -l <ledger>/postgres.log -w start` (its socket is
   `<ledger>/socket`, port 55437 on the socket only).
4. Backup and restore drill (section above):
   `python -m catalyst_lab.ledger_ops backup --root <ledger> --destination <backups> --allow-owner-ledger`
   then `python -m catalyst_lab.ledger_ops drill --backup <backups>/<timestamp> --scratch-root /tmp`.
   The manifest records the audit head, sequence and event count: that is H0.
5. Broker read-only check (Alpaca Paper dashboard or a read-only account/positions GET): flat and
   no open orders. Any exposure stops the procedure.
6. Migrate, in one owner transaction:
   `./run catalyst-lab ledger-migrate --local-dir <ledger> --expect-current 20 --target 23 --backup-manifest <backups>/<timestamp>/manifest.json`
7. Read the result before anything else. Expected from 20: `before` 20, `after` 23,
   `broker_requests` 0, and `"warning": "AUDIT_HEAD_CHANGED_BY_MIGRATION"` with `event_count`
   exactly `event_count_before + 2`. 021 and 023 are DDL-only; 022 appends the two audited rows
   described below, and the first new event's `previous_hash` is H0. For reference, a ledger at
   13 would take `--expect-current 13 --target 23` and add exactly `event_count_before + 5`. Migrations 014, 015, 017, 018, 019, 020 and 021 are DDL-only; 016
   seeds the three audited `lab.account_risk_policies` rows (`CATALYST_RETEST_V1`,
   `MUSE_JEV_MANAGED_TEST_V1`, `JEV_MANAGED_RISK_V2`) and 022 appends two more audited rows
   (`JEV_MANAGED_RISK_V3` and its CRYPTO row in `lab.account_risk_market_terms`), each with its
   own audit event, so H0 still sits at its sequence and every earlier event, hash, audited row
   and halt row is unchanged (`execution_halts` is a view over the renamed table from 015
   onward). A ledger already at 21 migrates with `--expect-current 21 --target 23`: exactly
   `event_count_before + 2`, the first new event's `previous_hash` is H0. Any other delta means
   stop, restore from the backup, and report; the transaction has already rolled back on
   `AUDIT_HISTORY_REWRITTEN`.
8. Confirm with `python -m catalyst_lab.managed_ops preflight --config <private.json> --check-database`:
   the database check reports schema 23 (MATCH) and the role checks pass. A second
   `ledger-migrate` is refused with `LEDGER_VERSION_MISMATCH`; `dev-init` is refused by the marker.

Credential injection (section above) and the first supervised launch (plan 0.10) follow this
window; nothing in it contacts the broker or provider except the read-only check in step 5.

## Off-host alerts (plan 4.2)

The watchdog (`managed_ops watchdog-once`, run on a loop by the `watchdog` LaunchAgent
component) can forward its own alarm codes to an owner-provided Healthchecks.io-style ping
URL, and can raise a local macOS notification. Both are off by default: an empty
`"notify": {}` section in the private configuration means no off-host notifier, and that is
a fully valid, complete configuration — nothing below is required to run the watchdog.

### What it does

- **Dead-man ping.** Every clean watchdog tick POSTs a success ping to the configured URL.
  This covers a dead Mac, a dead app, or dead Wi-Fi: none of those can be reported by this
  process itself, so Healthchecks' own "no ping arrived in time" alarm is the actual signal,
  not anything this code sends.
- **Alarm forwarding.** A new alarm code since the last tick posts to `<url>/fail`
  immediately, with the current alarm codes. While the same alarms persist, `/fail` repeats
  only every `reminder_minutes`, so a stuck problem does not spam the channel.
- **`JEV_BREAKER_OPEN`** (package jev-breaker) is one such code: `status_alarms` raises it
  whenever the running app's `jev_breaker.state` is not `CLOSED`, and clears it once the
  managed research loop's own health probing closes the breaker again (or an operator
  restarts the standalone `review_worker`, the other path that can close it). It carries no
  evidence, exactly like every other code here. See
  [MANAGED-RUNTIME.md](MANAGED-RUNTIME.md) and [packages/jev-breaker.md](packages/jev-breaker.md).
- **Daily audit head.** Once per UTC day, at `daily_head_hour_utc`, the success payload also
  carries the current audit sequence number and head hash, read read-only from the audit
  database. This is off-host retention of the chain head; it never triggers a database write
  and never blocks the ping itself (a database or query failure is silently treated as "no
  head available this tick").
- **Local notification.** When `local_notification` is `true`, an alarm transition or
  reminder also raises a macOS Notification Center banner (`osascript`), with the same
  alarm codes as the off-host payload.
- **Never evidence, never trading.** Every payload is compact JSON — codes and counts only:
  `{"alarms": [...codes...], "audit_seq": n, "audit_head_hash": "...",
  "release_commit": "...", "at": "<iso timestamp>"}`. Never a symbol, price, thesis excerpt,
  credential, or the ping URL itself. A failed off-host notification (network, DNS, a
  non-2xx response, a malformed ping file) is recorded locally as the alarm code
  `NOTIFY_FAILED`, in the same `alarms.json` the watchdog already writes; it is never turned
  into a halt, a retry against the broker, or any other change to a trading decision.

### Configuration

Add a `notify` section to the private configuration (`config_version: 2`):

```json
"notify": {
  "provider": "HEALTHCHECKS",
  "ping_url_file": "/REQUIRED/private-paper/healthchecks-ping-url",
  "allowed_hosts": ["hc-ping.com"],
  "reminder_minutes": 30,
  "daily_head_hour_utc": 6,
  "timeout_seconds": 5,
  "local_notification": true
}
```

All seven keys are required once the section is non-empty (`{}` disables it; there is no
partial configuration). `ping_url_file` must be an absolute path; the file itself must be
owner-only, mode 0600, and hold exactly one `https://` URL whose host is in `allowed_hosts`
— anything else is refused, both when the private configuration loads and again on every
send. `managed_ops preflight` reports `notify.state` as `NOT_CONFIGURED`, `CONFIGURED` or
`INVALID`, and never the URL itself.

### Owner setup

1. Create a Healthchecks.io check (a self-hosted instance works too; put its ping host in
   `allowed_hosts`). Set its **period** to the watchdog's `interval_seconds` (the template
   default is 30 s) and its **grace time** generously above that — a few missed ticks' worth,
   for example 5 minutes — so one slow tick does not itself page.
2. Optional: in that check's integrations, add Pushover (or any other channel Healthchecks
   supports) for a loud, acknowledgeable alert. This is entirely configured on Healthchecks'
   side; nothing in this codebase talks to Pushover directly.
3. Copy the check's ping URL. Write it, and only it, into a new owner-only file:
   ```sh
   umask 077
   printf '%s' 'https://hc-ping.com/<your-uuid>' > /REQUIRED/private-paper/healthchecks-ping-url
   chmod 600 /REQUIRED/private-paper/healthchecks-ping-url
   ```
   The file must hold nothing else: no query string, no second line worth reading (a
   genuine trailing newline from `printf`/`echo` is tolerated and stripped).
4. Add the `notify` section above to the private configuration, matching the file's path and
   the ping host in `allowed_hosts`. Restart the watchdog component (or run
   `managed_ops watchdog-once --config <path>` once) and confirm both `alarms.json` (beside
   the configured `alarm_file`) and the check's Healthchecks page show a recent success.
5. **Owner proof, both required before relying on this for real:**
   - Kill the app (stop the `local.catalyst.paper.app` process however you normally would,
     leaving the watchdog itself running) and confirm Healthchecks pages you once the
     check's grace time elapses.
   - Turn off Wi-Fi on the Mac and confirm the same: no ping can reach Healthchecks, so its
     own missing-ping detection is what alerts you, not this process.

### Rotating the URL

Replace the file's content (the same atomic pattern as any other secret rotation in this
runbook: write to a new file, `chmod 600`, then move it into place) and restart the watchdog
component. The notifier reads the file fresh on every send, so in practice a rotated file
takes effect on the very next tick either way; restarting is still the supported procedure,
for the same reason every other credential in this project is rotated by restarting the
component that reads it (see "Credentials and secret rotation" above).

### State

All notifier state — the last known alarm codes, the last reminder time, the last UTC date a
daily head was sent — lives in one owner-only mode-0600 JSON file beside the watchdog's
`alarm_file` (named `<alarm_file stem>.notify-state.json`). A missing or corrupt state file
resets safely: the next tick behaves as "no prior alarms, never notified," which costs at
most one extra `/fail` ping, never a missed one.

## Monthly Jev budget (`JEV_SPEND_GUARD_V1`, package jev-budget)

Fixture evidence only so far (`tests/test_jev_budget_*.py` and a month-long simulation); no real
month has been metered yet. The owner's total is $70 a month at most: Railway (a $15 alert and a
$20 hard limit) and Jev at most `JEV_MONTHLY_BUDGET_USD` (50 in the deploy example).

- **What it does.** The trader meters every Jev call from its exact request bytes
  (`lab.jev_requests.request_json`; every receipt with an HTTP status, or a transport failure, is
  a call, retries included) at `JEV_BYTES_PER_TOKEN` (3) bytes a token and
  `JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD` (0.042), by New York calendar month. Maintained trades
  admitted from this package on (`CRYPTO_MAINTENANCE_V3`) are reviewed every completed minute
  while the month fits the budget (`NORMAL`), every 5 minutes (`THROTTLED`) when month-to-date
  plus the last 24 hours' per-minute demand for the rest of the month exceeds 98% of it, every
  15 minutes (`TIGHT`) when throttled with 90% spent, and not at all (`EXHAUSTED`) from 98%
  spent. Throttling ends when that projection is back at or below 93%. Event reviews run in every
  tier but `EXHAUSTED`. Selection, 24-hour reviews, early exits, stops, targets and protection are
  never throttled; trades admitted under V1 or V2 keep their cadence.
- **Where to see it.** The status's `jev_budget` (`cloud_runtime status` in the cloud):
  `tier`, `tier_since`, `routine_review_seconds`, `month_to_date_usd`, `today_usd`,
  `projection_usd` (month-end at the last 24 hours' pace), `projection_per_minute_usd` (what
  decides the tier), `budget_usd`, `thresholds_usd`, `month_by_kind_usd`, `month_calls`,
  `month_request_bytes` and `month_estimated_input_tokens`. Each tier change is a
  `JEV_BUDGET_TIER_CHANGED` event and each configuration a runtime starts with a
  `JEV_SPEND_GUARD_CONFIGURED` event (outputs route, no setup).
- **Alerts.** Entering `THROTTLED`, `TIGHT` or `EXHAUSTED` raises `JEV_BUDGET_THROTTLED`,
  `JEV_BUDGET_TIGHT` or `JEV_BUDGET_EXHAUSTED` for 30 minutes (one off-host `/fail`); a return
  to `NORMAL` raises nothing. `JEV_BUDGET_STATUS_UNAVAILABLE` stays while the meter cannot be
  read for more than two minutes: V3 trades are then not reviewed (fail closed on spend).
- **Settings.** The budget is required whenever `MANAGED_MANAGEMENT_REVIEWS=ENABLED` (and always
  in the cloud, where all three are required); a missing or invalid value refuses startup and
  the preflight names the variable. The budget takes at most two decimals; the price is
  0.000001–1000 and bytes per token 1–10.
- **Recalibrate from TypeSafe's usage page** (the bill is TypeSafe's, not this estimate): for one
  period, divide the request bytes the status reports (`month_request_bytes`, or the difference of
  two readings) by the input tokens TypeSafe reports for the same period, and set
  `JEV_BYTES_PER_TOKEN` to that (up to three decimals; keep it a little below the measured value
  to stay conservative). If TypeSafe changes its price, set the price. Either change re-prices the
  whole month at the next start (a calibration, recorded as a new `JEV_SPEND_GUARD_CONFIGURED`).
- **Raise or lower the budget** by setting `JEV_MONTHLY_BUDGET_USD` (on Railway a trader redeploy;
  commit the same value in `deploy/private-paper.example.json` so the next `railway config apply`
  keeps it). `EXHAUSTED` lifts once month-to-date is below 98% of the new budget.
- **When exhausted.** Nothing more is asked of Jev about open V3 trades until the month rolls
  over (or the budget rises): one `POSITION_REVIEW_SKIPPED` per trade (`JEV_BUDGET_EXHAUSTED`),
  milestones and other triggers are still recorded and are reviewed at once when reviews resume.
  Their stops and targets stay as they are and protection runs unchanged.

## Restarts, halts and refused closes (plan phase 0, 2026-09-26)

Fixture evidence only so far (`tests/test_unattended_safety.py`); none of this has been
exercised under launchd, against the owner ledger or at Alpaca.

### Lost executor lease: the app exits 75 and launchd restarts it

The app holds the account executor lease (a PostgreSQL advisory lock on its own session) for
its whole life. If that session goes away — a PostgreSQL restart, a terminated backend, a
dropped socket — the next protection tick (at most a second later) finds the lease lost. The
process then:

1. sends nothing more (every broker send re-checks the lease; the tick runs nothing else);
2. appends `RUNTIME_EXECUTOR_OWNERSHIP_LOST` to `lab.managed_events` (three attempts; when the
   database itself is down this can fail) and writes one code-only line,
   `RUNTIME_EXECUTOR_OWNERSHIP_LOST code=… exit_code=75 recorded=…`, to the app's rotating log;
3. stops every worker loop and the HTTP server, and exits with status **75** — at the latest
   30 seconds later, even if the graceful shutdown hangs.

launchd's `KeepAlive {SuccessfulExit: false}` restarts it after the 10-second throttle. The
launcher waits for the ledger (bounded by `ledger.wait_seconds`), and the new process takes the
lease and reconciles before it protects or enters anything. Nothing to do by hand unless the
restarts repeat. The lease is never taken back inside the old process, so two executors never
act on the account. While the app is down, broker-side stop-limits and bracket legs still
protect positions; app-side exits (the crypto target, time exits, the −3% daily halt, the
stop-limit market fallback) resume with the new process.

### What a restart or a market-stream drop does to pending setups (package gap-resume)

Fixture evidence only so far (`tests/test_gap_resume.py`); not yet exercised under launchd,
against the owner ledger or at Alpaca.

- **Crypto setups from report-V3 picks admitted from 2026-09-27 on** (`gap_resume_version`
  `CRYPTO_GAP_RESUME_V1` in their state) are **held**, not revoked, when the crypto stream drops
  or the app (re)starts: one `GAP_RESUME_PENDING` each, and nothing can trigger or enter for
  them meanwhile. After a restart their window starts at the previous process's last recorded
  observation (its last heartbeat's `gap_resume.as_of`, at most about a minute before it
  stopped), or earlier if that process had a gap or a hold still open.
- Once the stream is back (the symbol acknowledged), trade updates are connected and the
  reconciliation is clean, the app waits for the minute to close plus 30 s and then reads
  Alpaca's one-minute bars for the window, one setup per protection tick. Each setup ends one
  way: `GAP_RESUMED` (watching again), invalidated `STOP_TRADED_DURING_GAP`, revoked
  `ENTRY_REACHED_DURING_GAP` (price reached the entry while nothing could act: no late entry),
  or revoked `DATA_FEED_FAILURE` (the bars could not be read or were incomplete). A healthy
  reconnect resolves a hold in about two minutes. A pick whose window ends meanwhile expires as
  before.
- **Every other WATCHING setup** (report V2, B1, B2, US stocks, engineering setups and crypto
  setups admitted before this package) is still revoked `DATA_FEED_FAILURE` at once, as before.
- **Fills while down.** The new process's first protection tick may run before the trade-updates
  stream is up. For a setup of this version it reads a missed entry fill from Alpaca's fill
  activities (the same REST backfill a reconnect runs) before the first-fill rule, and protects
  it once. Other setups still halt `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE` and exit if the protection
  tick sees such a fill before the reconnect's backfill has recorded it.
- Resting stop-limits stay at Alpaca across the restart; the new process finds them by client
  order ID and never places a second one.

### The crash-loop breaker exits 0 on purpose

The launcher (`scripts/run_managed_private.py`, `managed_ops.crash_loop_guard`) records each
start. The fifth start of a component within ten minutes writes
`launcher-state/crash-loop-<component>.json` and exits **0**, so that launchd's
`SuccessfulExit: false` rule stops restarting it. The watchdog then reports
`CRASH_LOOP_BREAKER_TRIPPED` and `CRASH_LOOP_<COMPONENT>` (and, for the app, stale ticks and
`WORKER_STOPPED`). Every restart counts, lease-loss restarts included, so a PostgreSQL that
restarts five times in ten minutes leaves the app stopped with only broker-side protection.

To recover: fix the cause (look at `<logs.directory>/<component>.log`), then either wait ten
minutes and start the component once (`launchctl kickstart gui/$(id -u)/local.catalyst.paper.app`),
or delete that component's `launcher-state/<component>.starts.json` after the fix and start it.
The first normal start clears the alarm file.

**Pending owner decision:** keep this stop-and-alert behaviour, or let the launcher keep
retrying with a growing delay (for example 1, 2, 4 … minutes) so that a longer outage heals by
itself. Until the owner decides, the breaker stays as it is.

### Watchdog codes for halts, refused closes, held setups and Muse jobs

| Code | Meaning | What to do |
| --- | --- | --- |
| `EXECUTION_HALT_ACTIVE` plus `HALT_<KIND>` per kind | A halt refuses every new entry (for example `HALT_OPERATOR_FLATTEN`, `HALT_OPERATOR_PAUSE`, `HALT_MANAGED_UNEXPLAINED_BROKER_POSITION`, `HALT_DAILY_RISK_HALT` for the rest of the New York day). Protection keeps running. The status carries `execution_halts` (`count`, `kinds`, `oldest_halt_seq`). | `managed_ops operator list-halts`; release through `operator release-halt` (or `resume` for a pause) only after the cause is fixed and reconciled. The daily halt ends by itself with the New York day. |
| `EXECUTION_HALT_STATUS_UNAVAILABLE` | The app could not read the halts from the ledger. | Treat as halted until the ledger is readable; check the database. |
| `EXIT_REFUSED_REPEATEDLY` plus `_CRYPTO` / `_US_STOCKS` | A working position's close was refused five or more times in a row. The app keeps retrying (1, 2, 4 … s, then every 60 s); the status lists it in `exit_refusal_alarms` with its reason and next retry. The position may have no broker-side stop left. | Look at the `EXIT_REFUSED` events' `reason` codes; check the account and asset at Alpaca. Close the position by hand at Alpaca if needed; the app then reconciles it. |
| `MUSE_WORK_FAILED` | The latest Muse run failed, or a current provider job has run longer than `muse_max_age_seconds`. It clears when a later run succeeds. Jobs that failed earlier no longer keep it on; the alarm file counts them in `muse_provider_jobs` (`failed`, `stuck`). | Check `muse.log`; a rising `failed` count with no alarm means individual jobs are failing and Muse moved on. |
| `EXIT_FLAG_PENDING` | The monitoring Jev or an agent flagged an open maintained trade for an early-exit review (`EXIT_FLAG_RAISED`, package maintenance). The trade keeps its stop and target; nothing is sold until the flag is resolved as agreed. The status lists `trade_maintenance.exit_flags`. For a trade under `CRYPTO_24H_REVIEW_V1` (package day-review) the other side is asked at once and the flag resolves within 15 minutes (`EXIT_FLAG_RESOLVED`, `EARLY_EXIT_DECISION`), so the alarm is expected for up to 15 minutes. | Read the flag's `reasons` and `evidence` (outputs route). A trade under `CRYPTO_24H_REVIEW_V1` needs nothing; for a longer alarm check `day_reviews` in the status and that the runtime's day-review task runs. For a trade admitted before that version, the owner decides: resolve it through `exit_flags.resolve_exit_flag` (EXIT_AGREED requests the market sell; EXIT_NOT_AGREED keeps the trade), or use `operator flatten-all` to close everything. |
| `MAINTENANCE_REVIEW_FAILING` | A maintained trade's last review failed (Jev unavailable: breaker open, provider error or deadline). Every stop and target is kept; the next scheduled review retries; `JEV_BREAKER_OPEN` usually accompanies it. | Check `jev_breaker` and `jev_calls_today` in the status; it clears when a later review of that trade succeeds. |
| `MAINTENANCE_STATUS_UNAVAILABLE` | The app could not read its maintenance status. | Check the database; treat flags as unknown until it is readable. |
| `DAY_REVIEW_JEV_FAILING` | A 24-hour review (`CRYPTO_24H_REVIEW_V1`, package day-review) is undecided and its latest Jev attempt failed (breaker open, provider error, deadline, bars or quote unavailable). It is retried every minute; 30 minutes after T without an answer the trade exits at market (`DAY_REVIEW_EXIT`, code `JEV_UNAVAILABLE`). The status lists `day_reviews.failing_reviews`. | Check `jev_breaker` and `jev_calls_today`; nothing else is needed: the trade keeps its stop and target until the review decides. |
| `DAY_REVIEW_STATUS_UNAVAILABLE` | The app could not read its day-review status. | Check the database. If the day-review task stops for more than 80 minutes after a trade's T, the protection loop's fail-safe sells it (`DAY_REVIEW_DEADLINE_EXIT`). |
| `GAP_RESUME_CHECK_OVERDUE` | A pending crypto setup has been held for its gap check (`CRYPTO_GAP_RESUME_V1`) for more than 300 s: the crypto stream has not come back with its symbol, trade updates are down, or the reconciliation is not clean. It cannot enter while held. The status lists it in `gap_resume.pending` with its window and `due_at`. `CRYPTO_MARKET_STREAM_LOST` or `BROKER_STREAM_LOST` usually accompanies it. | Check the market and trade streams and the reconciliation (`last_reconciliation_at`, halts). It clears when the check runs; if the stream cannot come back, the setup expires with its window. |
| `GAP_RESUME_STATUS_UNAVAILABLE` | The app's `gap_resume` status section was unreadable. | Treat held setups as unknown; check the app. |
| `JEV_BUDGET_THROTTLED`, `JEV_BUDGET_TIGHT`, `JEV_BUDGET_EXHAUSTED` | The monthly Jev budget's tier changed (package jev-budget, section "Monthly Jev budget"): routine maintenance reviews of V3 trades now run every 5 or 15 minutes, or not at all. Raised for 30 minutes after the change. | Read `jev_budget` in the status. Nothing is required: it is the budget working. To buy per-minute reviews back, raise `JEV_MONTHLY_BUDGET_USD`; if TypeSafe's usage page shows less than the estimate, recalibrate `JEV_BYTES_PER_TOKEN`. |
| `JEV_BUDGET_STATUS_UNAVAILABLE` | The trader could not read its Jev spend meter for more than two minutes; V3 trades are not reviewed meanwhile (their stops and targets stay; protection runs). | Check the database; it clears on the next good read. |
| `RECONCILIATION_STALE` | No reconciliation has completed clean for more than 90 seconds. From package learning-app the status's `last_reconciliation_at` is the last clean completion (never null merely because a pass is running), so a read inside a pass no longer raises it; entries still wait for a current clean reconciliation (`entry_ready`). | Check `BROKER_STREAM_LOST`, halts and the trader's log for `RUNTIME_RECONCILIATION_FAILED`. |

## The learning loop's nightly records (package learning-app)

Plan [LEARNING-LOOP-PLAN.md](LEARNING-LOOP-PLAN.md); records and rules in
[packages/learning-app.md](packages/learning-app.md). Paper only: nothing here trades, changes a
rule or reaches Jev.

- **What runs.** In the cloud, the Railway cron service `jobs` at 05:30 UTC
  ([RAILWAY-DEPLOYMENT.md](RAILWAY-DEPLOYMENT.md) 2.5): shadow outcomes, maintenance replays, the
  previous New York day's `MARKET_REALITY` and `DAILY_SCORECARD`, and after each week the
  `WEEKLY_REVIEW`. Each is one immutable event with no setup; a rerun records nothing twice, and
  the reality and the scorecard catch up two missed days.
- **Reading them.** `railway ssh --service ops -- python -m catalyst_lab.cloud_runtime
  scorecard | market-review | weekly-review` (plain summary, then JSON; a read-only preview when
  nothing is recorded yet). The same functions read a Mac ledger with `AUDIT_DATABASE_URL` set to
  its `catalyst_app` connection.
- **A failed step** logs `JOBS STEP step=<name> result=FAILED code=<code>` and the others still
  run. The codes: `REALITY_BARS_UNAVAILABLE` (a coin's public bars could not be read: nothing is
  recorded for that day, the next night retries), `REALITY_UNIVERSE_UNAVAILABLE` (no report or
  outlook had recorded a universe before that day ended), `REPLAY_BARS_UNAVAILABLE` (retried the
  next night), `DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT` (every step fails; the next night
  retries), `JOB_TIME_LIMIT` (a step not started after 20 minutes; the run ends after 30). None
  of them affects trading: the trader never waits for the jobs.
- **Muse's side.** Muse posts its morning outlook (`MARKET_OUTLOOK_V1`) and its post-mortems
  (`POST_MORTEM_V1`) through its own token and reads its sanitized lessons in the research
  context; a refused outlook or post-mortem names a code, never the content.
- **A proposal.** A weekly `PROPOSE` verdict carries a contract-text draft for a named version.
  It changes nothing until the owner approves it in chat and it ships as a normal package with
  tests and a `CONTRACT-RESOLUTIONS` entry.

## Acceptance evidence for a managed trade (plan 0.10 tool)

`scripts/managed_acceptance_evidence.py` collects and hashes one managed setup's evidence from
the append-only ledger into a `MANAGED_ACCEPTANCE_EVIDENCE_V1` manifest, so gate G3 (the first
supervised managed trade) is proved from the audit trail rather than hand-collected. It is
strictly read-only: it opens the database connection it is given, issues only `SELECT`
statements, and never opens a second connection, applies a migration, or contacts the broker or
provider itself. The pure functions it calls live in `catalyst_lab.acceptance_evidence`, which
`tests/test_acceptance_evidence.py` rehearses against a disposable ledger.

### Role: `catalyst_review`, never `catalyst_app`/`catalyst_risk`/`lab_owner`

The tool refuses to run as `catalyst_app`, `catalyst_risk` or `lab_owner`, and accepts only
`catalyst_review` or `catalyst_reporting`. In practice, use `catalyst_review`: it is the role
migrations 009–016 grant read access to every `lab.managed_*` table and view, the account-risk
policy and ledger-binding tables, and the research/review tables, and migration 019 grants
`SELECT` on `lab.execution_halts` and `lab.trade_events`. `catalyst_reporting` only holds
the frozen V1 `strategy_*`/`public_*` grants (migrations 006–007) — no managed table at all, not
even `lab.managed_setups` — so the tool refuses it immediately with a clear reason rather than
returning an empty manifest.

Connect over the ledger's own private socket, trust-authenticated like every other role here (no
password):

```
--database-url "host='<ledger>/socket' port=55437 dbname=catalyst_lab user=catalyst_review"
```

### Halts and the audit chain (readable from migration 019)

Until migration 019, `catalyst_review` could read neither `lab.execution_halts` nor
`lab.trade_events`, and the tool reported both sections absent. Migration 019 grants the role
`SELECT` on both (read-only: it still cannot write either, and the view shows unreleased halt
records only). The tool's existing `has_table_privilege` probe decides at run time, so nothing
has to be configured:

- **`halts`** reads `lab.execution_halts` and reports `present: true` with the active halt count
  and reasons; the `ZERO_RESIDUAL_NO_HALTS` check passes only when no halt is active (an operator
  pause or an unreleased `OPERATOR_FLATTEN` halt fails it).
- **`audit`** reports `present: true`. Without `--audit-export` the collector reads
  `lab.trade_events` itself (`source: LAB_TRADE_EVENTS`) and calls
  `catalyst_lab.audit.verify_events` twice: once for the whole chain (`head_after`) and once for
  the prefix ending at the setup's own admission event (`head_before`), so a tampered or
  truncated row anywhere in between is caught, never silently accepted. `--audit-export` stays
  available: a file from the existing, separate, `catalyst_app`-authenticated command

  ```
  ./run catalyst-lab export --local-dir <ledger> --file /private/evidence/<timestamp>.jsonl
  ```

  is verified instead when supplied (`source: AUDIT_EXPORT`), for example to bind the evidence
  to an export retained elsewhere.

On a ledger before migration 019 both sections fall back to the earlier behaviour, fail closed
and never assumed clean: `halts` is `present: false` with reason
`ROLE_CANNOT_READ_LAB_EXECUTION_HALTS` (its check fails), and `audit` is `present: false` with
reason `ROLE_CANNOT_READ_LAB_TRADE_EVENTS` unless `--audit-export` is supplied.

Every other section in the plan's list — subscription/print evidence, `ENTRY_ELIGIBILITY`,
`RISK_CHECK`, the risk decision and its 5-second TTL, the claim, `BROKER_ACK`, fills (including
REST-backfilled ones), `BROKER_POSITION`, protection (`PROTECTION_PLAN`, the stop/target
acknowledgements, `PROTECTION_TRANSITIONING`), the exit request and exit fill, the `STATE`
history through `CLOSED`, the account-wide `BROKER_RECONCILIATION` events, and the released
reservation — comes from tables `catalyst_review` can read, and is present whenever the real run
produced it. `stream_subscriptions` (the `RUNTIME_MARKET_CONNECTED`/`RUNTIME_STREAM_CONNECTED`
acknowledgements) is written only by the running `ManagedRuntime`; a fixture or historical setup
admitted without that runtime in front of it will show this section absent too, honestly, not as
a role-boundary gap.

### Running it

```
python scripts/managed_acceptance_evidence.py \
  --database-url "host='<ledger>/socket' port=55437 dbname=catalyst_lab user=catalyst_review" \
  --latest-closed \
  --output /private/evidence/<setup-id> \
  --audit-export /private/evidence/<timestamp>.jsonl \
  --broker-snapshot /private/evidence/broker-snapshot.json
```

Use `--setup-id <uuid>` instead of `--latest-closed` to target a specific setup (for example a
historical one, once the owner has decided it is the trade to submit as G3 evidence).
`--output` must be a directory that does not yet exist; the script creates it mode 0700 and
writes `evidence.json` (the manifest, canonical/compact JSON — the same bytes that are hashed)
and `evidence.sha256` inside it, both mode 0600. It prints only the per-section presence, the
check results and the audit heads to stdout — never the full manifest, and never a credential or
account identifier (every section is scanned for credential-shaped strings, via the same patterns
`catalyst_lab.audit` and the repository-hygiene test share, before anything is written; a match
refuses the write entirely, which should never happen against real data).

Exit codes: `0` every check passed; `2` the manifest was collected and written but at least one
check failed or fell back to unavailable (a failed or incomplete check is itself part of the
record — it still gets written); `1` the tool refused outright (wrong role, or the requested
setup is not visible to this role) and nothing was written.

### Capturing `--broker-snapshot` (owner action, read-only)

The script never contacts the broker. Capture the snapshot separately with the existing GET-only
`AlpacaReadOnly` client (paper credentials only; this reads positions and open orders, nothing
else, and the result holds no credential or account identifier):

```python
import json
from datetime import UTC, datetime
from catalyst_lab.alpaca import AlpacaCredentials, AlpacaReadOnly

client = AlpacaReadOnly(AlpacaCredentials.from_env())
try:
    snapshot = {
        "captured_at": datetime.now(UTC).isoformat(),
        "positions": client.positions(),
        "open_orders": client.open_orders(),
    }
finally:
    client.close()
with open("/private/evidence/broker-snapshot.json", "x") as f:
    json.dump(snapshot, f, indent=2, default=str)
```

The collector compares this against the setup's symbol only: `broker_snapshot.summary` reports
`positions_for_symbol`, `open_orders_for_symbol` and `flat_and_no_open_orders`, so the owner can
see whether the broker's own closing state (not just the local ledger's) agrees the position and
its orders are gone.

### What a passing manifest looks like

Every section present (`broker_snapshot` only when a snapshot was supplied) and every check in
the `checks` list `true`, `ZERO_RESIDUAL_NO_HALTS` included — so the exit code is `0`. An
`OPERATOR_FLATTEN` halt left from an abort (next section) keeps that check failing until the
operator releases it. The `unknowns` object names every section that came back unavailable and,
separately, whether fees were fully known (`managed_fills.fee_usd` — "unknown" is explicit here
whenever the broker event never reported a fee, and gross P&L is used unless fees are fully
known). `audit.present` is `true` and `audit.chain.valid` is `true` only when the whole chain,
from the setup's admission event through the last row read (or exported), verified with no
tamper or gap; `audit.source` names which chain was verified.

### Abort rule (plan 0.10, during the supervised run itself — not this tool's job)

While watching the first supervised trade live: if protection ever reports latched (the runtime's
`PROTECTION_TICK_FAILED`/`MANAGED_PROTECTION_PERSISTENT_FAILURE` latch, or the position's own
protection state) for more than 10 seconds while a position is still open, flatten immediately
through the operator CLI (`managed_ops operator flatten-all --reason ...`) rather than waiting;
the running app's next account-safety tick executes it (the abort procedure is step 8 of the
next section). This acceptance-evidence tool is run afterward, to prove what happened; it does
not watch the live session or decide when to abort it.

## First supervised managed trade (plan 0.10)

**PAPER TRADING — SIMULATED. Owner action, supervised, crypto first, management reviews
disabled.** Ruling R6, option B: the owner enrolls one `ENGINEERING_TEST` crypto setup from the
command line, and every execution gate stays in force. The design and its evidence separation
are in [ENGINEERING-ACCEPTANCE.md](ENGINEERING-ACCEPTANCE.md#managed-engineering-test-enrollment-plan-010-ruling-r6-option-b).
This procedure has been rehearsed only with fixtures (`tests/test_managed_engineering.py`),
not against Alpaca.

Preconditions:

- The ledger is at the current schema (22; section above) and marked `ACCOUNT_LEDGER`.
- Credentials are injected (the credentials section).
- An immutable release is built from the approved commit (`scripts/build_release.py`), and the
  private config v2 names it.
- The config's `environment` has:
  - `MANAGED_MANAGEMENT_REVIEWS: "DISABLED"`. The value is required and has no default;
    anything other than `ENABLED` or `DISABLED` stops startup.
  - `MANAGED_RISK_POLICY_ID: "JEV_MANAGED_RISK_V2"`, or the deploy example's
    `JEV_MANAGED_RISK_V3` (section "Crypto size and 24-hour hold" below). Under V3 an enrolled
    crypto setup is sized as a 10% equity slice capped at 0.5% risk, and its stop must be at
    least 2% below the max entry, or the entry is refused `STOP_DISTANCE_BELOW_MINIMUM`.
  - `MANAGED_US_CLASSIFICATIONS_JSON: "[]"` (crypto only).
  - A crypto classification that covers the chosen pair. Under the V2 bucket policy the pair
    must be in a bucket, or enrollment refuses it with `CORRELATION_UNKNOWN`.
- Run the session well before 23:50 New York time. The crypto day policy closes entries 10
  minutes and flattens 5 minutes before midnight.

1. **Nothing else trades.** No other executor or engine runs against the account: the
   executor lease refuses a second one, and V1 is never launched. Muse is not started for this
   session (recommended). Otherwise an organic selection would compete for the same account
   risk; it would still run under the same `DISABLED` switch.
2. **Start the ledger, then run preflight.** Start the ledger (the `ledger` component, or
   `pg_ctl` as in the section above). Then run
   `python -m catalyst_lab.managed_ops preflight --config <private.json> --check-database`.
   The database check must report schema 20 (MATCH) and the release check `VERIFIED`.
3. **Start the app in the foreground, from the release**, in its own terminal:
   `<release>/venv/bin/python <release>/scripts/run_managed_private.py --config <private.json> --component app`.
   It stays in the foreground; its output goes to the rotating private logs. Ctrl-C stops the
   worker loops but does **not** flatten anything.
4. **Confirm readiness before enrolling.** Read the status with the release interpreter. This
   is a GET-only loopback request with the status token, which it never prints:

   ```sh
   <release>/venv/bin/python - <private.json> <<'EOF'
   import json, sys, httpx
   from catalyst_lab.managed_ops import load_private_config, role_token
   config = load_private_config(sys.argv[1])
   token = role_token(config["status"]["token_file"])
   status = httpx.get(config["watchdog"]["api"] + "/api/v1/lab/status", timeout=3,
                      trust_env=False, headers={"Authorization": "Bearer " + token}).json()
   keys = ("worker_state", "executor_ownership", "last_reconciliation_at", "entry_ready",
           "trade_stream_connected", "market_streams", "management_review_enabled",
           "error_code", "schema_version", "release_commit")
   print(json.dumps({k: status.get(k) for k in keys}, indent=1))
   EOF
   ```

   Proceed only if all of these hold:

   - `worker_state` is `RUNNING` and `executor_ownership` is `EXCLUSIVE` (the executor lease
     is held).
   - `last_reconciliation_at` is within the last 30 seconds and `entry_ready` is `true` (clean
     startup reconciliation).
   - `trade_stream_connected` is `true`, and `market_streams.CRYPTO` becomes `true` once the
     pair is subscribed.
   - `management_review_enabled` is `false`, `error_code` is `null` and `schema_version` is
     19.

   Also check two commands:

   - `OPERATOR_DATABASE_URL=<operator DSN> python -m catalyst_lab.managed_ops operator list-halts`
     must show `active_count` 0 (zero halts, no operator pause) and `pending_flatten_count` 0.
   - `python -m catalyst_lab.managed_ops watchdog-once --config <private.json>` must report no
     alarm other than `MUSE_HEARTBEAT_STALE_OR_UNAVAILABLE`, which is expected while Muse is
     not running.
5. **Choose the levels from the live market.**
   - T is at or just below the current trade, so that an acknowledged print at or below T is
     likely soon.
   - M is a few price increments above T. It is the limit price: at the trigger the ask must
     be at or below M and the spread at most 10 bps.
   - S is below nearby structure.
   - P satisfies `P − M >= 2 × (M − S)`.

   Every level must sit on the broker's price increment. Admission refuses off-grid levels,
   and that refusal is final for the enrollment, so enroll again with on-grid levels. No
   quantity is entered: the risk engine sizes 0.5% of equity at risk on `M − S`, cash-only for
   crypto, within the policy's caps.
6. **Enroll**, with the operator login:

   ```sh
   <release>/venv/bin/python -m catalyst_lab.managed_engineering enroll --config <private.json> \
     --symbol BTC/USD --entry-trigger <T> --max-entry <M> --stop <S> --target <P> \
     --expires-minutes 60 --reason "Plan 0.10 first supervised managed trade"
   ```

   Record the printed `enrollment_event_seq`, `selection_event_seq`, `packet_id`, `signal_id`
   and `expires_at`. A refusal prints `ENGINEERING_ENROLLMENT_REFUSED: <CODE>`; the codes are
   in ENGINEERING-ACCEPTANCE.md.
7. **Watch.**
   - `python -m catalyst_lab.managed_engineering status --config <private.json>` shows the
     setup state.
   - The dashboard's trade table marks the row `ENGINEERING TEST`.
   - The step-4 status must stay healthy throughout.

   The app admits the packet on a tick once the pair's quote and trade subscription is
   acknowledged. The expected states are WATCHING → ENTRY_PENDING → ORDER_SUBMITTED → OPEN →
   CLOSED. A setup that is not entered ends as EXPIRED_UNTRIGGERED at expiry, or as
   INVALIDATED (stop printed before the trigger, price beyond M, a feed gap). Either outcome is
   valid evidence. While OPEN, the stop-limit protection is at the broker, and the target, stop
   and time exits are mechanical. Jev reviews nothing; the position monitor records
   `POSITION_REVIEW_SKIPPED`.
8. **Abort rule.** Abort if the status `error_code` names a protection latch
   (`PROTECTION_TICK_FAILED`, or the durable `MANAGED_PROTECTION_PERSISTENT_FAILURE`) for more
   than 10 seconds while the engineering position is open. Leave the app running: it executes
   the flatten.
   1. Record the audited request, over the operator login:
      `OPERATOR_DATABASE_URL=<operator DSN> python -m catalyst_lab.managed_ops operator flatten-all --reason "Plan 0.10 abort: protection latched"`.
      It prints `recorded: true`, `broker_action: PENDING_ACCOUNT_SAFETY_TICK` and the
      request's `request_seq`. The command itself sends nothing to the broker.
   2. Block new entries at once: `managed_ops operator pause --reason "Plan 0.10 abort"`. The
      pause is durable even if the app is down; the flatten's own halt follows on the next tick.
   3. What the next account-safety tick (about one second) does, through the daily halt's own
      path (migration 019):
      - records an `OPERATOR_FLATTEN` execution halt, which blocks every entry;
      - invalidates every WATCHING setup and gives every working managed setup an exit request;
      - the setup's protective controller then cancels its open orders first (the crypto
        stop-limit; a stock bracket's legs) and, once they are cancelled, sends the close — each
        cancel and each close with its own exact one-use five-second authorization; legacy V1
        exposure, if any, gets durable exit requests processed the same way;
      - every tick compares both ledgers with the broker; once nothing is left (confirmed on a
        fresh broker read) it appends `OPERATOR_FLATTEN_EXECUTED` and one `COMPLETED` row in
        `lab.operator_flatten_completions`. A crypto position normally completes within a few
        ticks of the close's fill.
   4. Confirm completion: `managed_ops operator status` shows `pending_flatten_count` 0 and the
      request with `last_outcome: COMPLETED` and `last_residual_codes: []`; the Alpaca Paper
      dashboard shows no position and no open order; the watchdog reports no
      `FLATTEN_PENDING`.
   5. If it does not complete, the request stays pending and every tick carries on; after 60
      seconds the watchdog raises `FLATTEN_PENDING`. `operator status` shows the latest
      history row:
      - `PARTIAL` with `BROKER_REFUSED`: the broker refused a cancel or close; the next tick
        authorizes it again (a close under a fresh client order ID).
      - `PARTIAL` with `BROKER_RESPONSE_UNKNOWN` (and `AUTHORIZATION_UNRESOLVED`): the response
        was lost; each tick looks the order up by its client ID and never sends it twice.
      - `PARTIAL` with `MANAGED_PROTECTION_HALTED`: the controller stopped on its own halt, for
        example crypto dust below the broker's minimum order size. Close it in the Alpaca Paper
        dashboard if the broker allows; the dust halt keeps its own residual-acceptance release.
      - `PARTIAL` with `UNOWNED_BROKER_POSITION` or `UNOWNED_BROKER_ORDER`: exposure neither
        ledger owns. The app never touches it; close or cancel it in the Alpaca Paper dashboard
        and the next tick completes.
      - `FAILED`: the pass itself failed, for example `ALPACA_HTTP_429` or
        `BROKER_RETRY_AFTER` during a rate limit. The runtime latches only `REST_DEGRADED` or
        `ACCOUNT_SAFETY_TICK_FAILED`, both of which clear themselves; the pass is retried.
      Nothing executes a request while the app is stopped: restart it (its first tick carries
      on) or close everything in the Alpaca Paper dashboard.
   6. Release, only after `COMPLETED`: `operator list-halts` shows the `OPERATOR_FLATTEN` halt
      with its `release_blockers`. It is released like any reconciliation halt, with
      `managed_ops operator release-halt <halt_id> --reason "…"`, once a clean reconciliation
      has been recorded after the halt (the runtime reconciles every 30 seconds); the release
      is refused with `OPERATOR_FLATTEN_PENDING` while any flatten request is pending. Then
      `managed_ops operator resume --reason "…"` clears the pause from step 2 (`resume` never
      releases a flatten halt).
   7. Record the abort as a dated PHASES.md note.
9. **Close out.** Confirm all of these before stopping anything:
   - `managed_engineering status` shows `active: false` and `setup_state: CLOSED`, or an
     unentered terminal state.
   - The Alpaca Paper dashboard shows no position and no open order for the pair.
   - `operator list-halts` shows none.
   - `watchdog-once` reports no new alarm.

   Only then stop the app with Ctrl-C. Then collect the evidence, without credentials:
   - an audit checkpoint (the `audit` component or `export_checkpoint`) with the head before
     and after;
   - the step-4 status output;
   - the `managed_engineering status` output;
   - the broker order and position screens.

   Record it in PHASES.md, kept apart from the fixture evidence, together with the
   `scripts/managed_acceptance_evidence.py` manifest (section above).

Enabling management reviews later is a separate owner step, taken only after the G4
real-provider proof (`scripts/prove_managed_jev.py --production-calls 30`). Set
`MANAGED_MANAGEMENT_REVIEWS: "ENABLED"` in the private config and restart the app from the
release; the status then reports `management_review_enabled: true`. Engineering setups are
never reviewed in either setting. After this test, the plan waits for an organic selection
(ruling R6, option A).

## Crypto size and 24-hour hold (`JEV_MANAGED_RISK_V3`, migration 022)

**PAPER TRADING — SIMULATED. Not real money.** Package crypto-size-hold (plan phase 4b,
2026-09-27); rules in [packages/crypto-size-hold.md](packages/crypto-size-hold.md). Fixture
evidence only: no supervised paper trade has run under these rules yet.

Adopting it (owner steps):

1. Migrate the ledger to schema 23 (the guarded migration above). 022 appends
   `JEV_MANAGED_RISK_V3` and its CRYPTO terms and changes no existing row.
2. In the private config's `environment` (both are the deploy example's values):
   `MANAGED_RISK_POLICY_ID: "JEV_MANAGED_RISK_V3"` and
   `MANAGED_CLASSIFICATION_POLICY: "ALPACA_CRYPTO_SECTORS_V1"`. Keep
   `MANAGED_CRYPTO_CLASSIFICATIONS_JSON: "null"` so every tradable USD pair is classified at
   startup (a coin the built-in list does not name becomes `CRYPTO_OTHER`). To use your own
   sectors instead, add `MANAGED_CRYPTO_BUCKETS_JSON` (the V2 bucket JSON): it overrides the
   built-in list completely, including its `unlisted` rule.
3. Run preflight, then restart the app: the classification import runs at startup and appends
   one `CLASSIFICATION_IMPORTED` per changed symbol (earlier `CRYPTO_SHARED` rows are
   superseded, never rewritten). The startup checkpoint reports `policy_id`
   `ALPACA_CRYPTO_SECTORS_V1` (or `MANAGED_SERVER_CLASSIFICATIONS_V2` when your buckets
   override) and `unlisted_rule` `CRYPTO_OTHER`.

What changes:

- New admissions record `risk_policy_id: JEV_MANAGED_RISK_V3`. Setups admitted before keep
  their own policy, sizing, limits and day policy until they close.
- A crypto entry is 10% of current equity (about $1,000 on $10,000), smaller when the stop is far
  (never more than 0.5% of equity at risk, $50) or when cash is short, rounded down to the coin's
  quantity increment. Ten 2%-stop trades fit in $10,000 at once. Open crypto planned risk is
  capped at 5% ($500); at most three open trades per sector (BTC, ETH and SOL share
  `LARGE_CAP_L1` and may all be open together). A stop closer than 2% below the max entry is
  refused (`STOP_DISTANCE_BELOW_MINIMUM`). US numbers are V2's.
- A report-V3 crypto position is sold at market 24 hours after its first fill, reason
  `HOLD_24H_EXIT`, with no midnight close; it may enter until its pick's expiry (the next research
  run), past midnight. Stops, targets, the −3% daily halt and operator flatten still close it at
  any time. Other crypto setups keep the New York day policy and `TIME_EXIT`.
- A top-K pick for a coin that already has an open trade is declined
  (`ACTIVE_SYMBOL_ALREADY_MANAGED`) and replaced by Jev's next-ranked pick.

Reading the evidence:

- The entry decision's context has `budget` (the trade's planned risk), `binding_constraint`
  (`NOTIONAL`, `RISK` or `CAPITAL`) and `sizing` (the caps, the cash available to crypto, the
  unfilled reserved notional, the increment, the three candidate quantities and the result).
- A deferral (`RISK_CAPACITY_DEFERRED`, 60-second cooldown) names the full constraint:
  `CORRELATION_LIMIT` (the sector holds three), `MAX_OPEN_PLANNED_RISK` or `MARKET_RISK_CAP`
  (5% open), `INSUFFICIENT_BUYING_POWER` (no cash left for a slice).
- `/api/v1/lab/setups` lists a report-V3 crypto setup's `holding_policy`; `hard_exit_at` is its
  24-hour exit once it has filled.

Going back: set `MANAGED_RISK_POLICY_ID` to `JEV_MANAGED_RISK_V2` and restart; V3 setups keep V3
until they close. The classification cannot return to the V1 shared theme (startup refuses
`EXISTING_CLASSIFICATION_CONFLICT`); supply owner buckets instead.

## Supervised research-agent session

`scripts/agent_research_session.py` runs one supervised local session: an external research
agent (`fable`) submits a real `AGENT_RESEARCH_REPORT_V2` report over the actual HTTP intake,
the selection Jev reviews it, evidence tasks are answered over HTTP, and selections are admitted
and executed against the test fixtures' paper venue with clearly simulated prints. It is a
session tool, not the production launcher, and it never touches an owner ledger or a broker:

- The ledger is a new disposable cluster in `/tmp/catalyst-session-*`; any other path or an
  existing directory is refused. `stop` (or Ctrl-C) exports and stops the cluster (`--keep`
  leaves it running).
- The venue is `ManagedVenue` behind a mock transport: no broker credential, no network call.
  Every trigger print, quote, completed bar, crypto asset-metadata row (1e-8 price, lot and
  minimum-order grids unless `--sim-price-increment` is given), order and fill it creates is
  labelled `SESSION_SIMULATION`.
- Classifications are the owner buckets under `MANAGED_SERVER_CLASSIFICATIONS_V2` (L1,
  PAYMENTS, DEFI, INFRA; `unlisted: REFUSE`). No stock is classified, so stock picks are refused
  at admission (`CORRELATION_UNKNOWN`). New setups use `JEV_MANAGED_RISK_V2`, including its
  randomized 30% `FIXED_EXIT` arm, which is never reviewed.
- Four session tokens (legacy Muse, status, operator, `fable`) are written to mode-0600 files
  under `<root>/tokens`. No command prints one.

Commands, from the repository root. `./run` changes into the repository first, so give
absolute paths to files.

```sh
ROOT=/tmp/catalyst-session-$(date +%H%M%S)
# Terminal 1: runs until stop or Ctrl-C (add --port 0 for an ephemeral port).
./run python scripts/agent_research_session.py start --root "$ROOT" --port 8791 --jev fixture
#   options: --fixture-script /abs/fixture.json  --max-jev-calls 100
#            --selection-rule B1|B2|TOPK|TOPK2 --topk-k 10 --quality-floor ADEQUATE
#              (TOPK2 = JEV_TOP_K_SELECTION_V2, the deploy example's rule)
#            --management-reviews DISABLED
#            --extra-crypto-pairs ARB/USD,BONK/USD  (further Alpaca USD pairs, CRYPTO_OTHER)
# Terminal 2: the agent, then the operator.
./run python scripts/agent_research_session.py submit --root "$ROOT" --report /abs/report.json
./run python scripts/agent_research_session.py tick --root "$ROOT"
./run python scripts/agent_research_session.py answer --root "$ROOT" --cycle ID --task ID \
  --evidence /abs/evidence.json
./run python scripts/agent_research_session.py unavailable --root "$ROOT" --cycle ID --task ID \
  --code PRIMARY_SOURCE_UNAVAILABLE
./run python scripts/agent_research_session.py execute --root "$ROOT" --simulate-prints
# 24-hour reviews (package day-review): keep maintained trades open, then walk the review.
./run python scripts/agent_research_session.py execute --root "$ROOT" --simulate-prints \
  --hold-open
./run python scripts/agent_research_session.py review --root "$ROOT" --at request
./run python scripts/agent_research_session.py reviews --root "$ROOT"
./run python scripts/agent_research_session.py review-answer --root "$ROOT" --review-id ID \
  --decision CONTINUE --what-changed "..." --next-24h "..." --proves-wrong "..."
./run python scripts/agent_research_session.py review --root "$ROOT" --at review
./run python scripts/agent_research_session.py exit-flag --root "$ROOT" --setup-id ID \
  --what-changed "..." --next-24h "..." --proves-wrong "..."
./run python scripts/agent_research_session.py flag-answer --root "$ROOT" --flag-id ID \
  --decision EXIT --what-changed "..." --next-24h "..." --proves-wrong "..."
./run python scripts/agent_research_session.py review --root "$ROOT" --minutes 1
./run python scripts/agent_research_session.py export --root "$ROOT" --output /abs/new-dir
./run python scripts/agent_research_session.py stop --root "$ROOT"
```

- `--fixture-script` answers by symbol: `{"SOL/USD": ["APPROVE", "NO", "NO", "LOW"]}` gives the
  SKEPTIC verdict, `news_stale`, `unsupported_inference` and `already_priced`, with an optional
  fifth element for the QUALITY category. The object form
  `{"skeptic": [[...], [...]], "quality": "STRONG", "management": "TIGHTEN_STOP"}` answers
  successive reviews (evidence revisions) in order. For `--selection-rule B2` cycles, whose
  reviews use `SKEPTIC_QUESTIONS_V2`, the key `"skeptic_v2"` takes six labels (or a list of
  six-label rows): `news_stale`, `already_priced`, `mechanism_contradicted`,
  `inference_labelled`, `factual_claims_supported`, `verdict`. `"*"` replaces the default for
  unlisted symbols: `APPROVE/NO/NO/LOW`, `NO/LOW/NO/YES/SUPPORTED/NEEDS_REVIEW`, `ADEQUATE` and
  `HOLD`. `status` prints the configuration, the counts and the Jev calls used.
- `--selection-rule B2` (with `--quality-floor`) records the B2 activation; `tick` then prints,
  for B2 cycles, the B2 disposition and reasons, the five component labels (`NS`, `AP`, `MC`,
  `IL`, `FC`) and the verdict as dissent instead of the V2 and B1-shadow columns (neither applies
  to V2 receipts), and each open task's requirement instructions. A `RECITE_FACTUAL_CLAIMS`
  task is answered with an evidence file that repeats the item's `sources`, `thesis`,
  `disproof` and `economic_relationship` and carries the corrected `selection_rationale`: under
  B2, re-cited or trimmed claims are new evidence without a new source.
- `submit` sets `generated_at` to now, fills `agent.run_id` when it is missing and posts as
  `fable`. It prints the 202 body or the 422 codes with field paths.
- `tick` runs every open cycle's review and publication. It prints, per item, the V2
  disposition and reason, the B1 shadow disposition and reasons, the dissent verdict, the
  receipt, the provider latency and the review window left, then the open evidence tasks with
  their objection codes and the Jev calls used. `--json` prints the full report. There is no
  background loop: Jev is called only by `tick` and, for one management review per simulated
  position, by `execute --simulate-prints`.
- A selection is admissible only inside the cycle's review window: 30 minutes from the report's
  receipt (`review_validity_seconds` 1800, the value in `deploy/private-paper.example.json`),
  never extended by an evidence revision. Evidence tasks are answered inside that window, as in
  production.
- `execute` admits every selected, unexpired packet and reports each refusal code. With
  `--simulate-prints`, each WATCHING crypto setup gets a `SESSION_SIMULATION` trigger print,
  the risk gate, a simulated entry fill, protection, one management review through the
  position monitor, then a target exit, close and reconciliation. It prints the risk decision,
  state path and residual per setup.
- `unavailable` claims the task and records the declaration in `<root>/agent/unavailable.jsonl`.
  The managed API has no unavailable route, so the task stays open until it expires; the
  declaration appears in the export.
- `export` writes `events.json` (with `verify_events`), `decisions.json`,
  `research-funnel.json` (the analytics route, status token), `selection-replay.json` (as
  `catalyst_review`), and one `scripts/managed_acceptance_evidence.py` manifest per closed
  setup. A session has no market or trade-updates stream, so each manifest fails
  `SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER` and exits 2.
- The Jev cap, `--max-jev-calls` (default 100 since 2026-09-27; 40 before), counts every
  provider HTTP call, retries included. The next call is refused with `JEV_CALL_CAP_REACHED` and
  never sent. Budget one SKEPTIC call per item revision (a second when a reply fails validation;
  B2 items without a rationale make none), one QUALITY call per approval under B1 or B2 (under V2
  only when more than ten approvals compete), two calls per pick under TOPK or TOPK2 (pick questions,
  then QUALITY_V3, each retried once on an invalid body), and one call per management review.
- Maintenance (`CRYPTO_MAINTENANCE_V1`, package maintenance): under `--selection-rule TOPK`
  (report V3) a crypto setup in the `JEV_MANAGED` arm is maintained. With `--simulate-prints`
  it gets one maintenance review instead of the management review: the simulated price moves to
  +1R above the entry, the milestone triggers the review over `SESSION_SIMULATION` 15-minute
  and 1-hour bars, the monitoring Jev answers, the code checks and applies the answer, and a
  raised stop is replaced at the simulated venue before the target exit. The fixture script's
  `"maintenance"` key answers it (`HOLD`, `RAISE_STOP`, `RAISE_TARGET`,
  `RAISE_STOP_AND_TARGET` or `FLAG_EARLY_EXIT`; default `HOLD`; a raise takes the first code
  option). `execute` prints the outcome, the trigger reasons, the levels after it and the
  replacement path; budget one Jev call per maintained position. `--management-reviews
  DISABLED` sends nothing (the review is reported as skipped). From package answer-rules the
  maintained arm records `CRYPTO_MAINTENANCE_V2` (a review every completed minute, context V5
  with the last 60 1-minute bars and the trade's last 5 reviews, V2's answer rule); from package
  jev-budget `CRYPTO_MAINTENANCE_V3`, whose session guard runs with the deploy example's budget
  over the session's own database and stays `NORMAL` (V2's minute cadence).
  `execute --simulate-prints --maintenance-minutes N` (1-10, default 1) runs N-1 further
  reviews of each maintained trade after the first, moving the session's maintenance clock one
  minute before each (`BAR_1M`), so a real-Jev session can watch the cadence and the history;
  budget N Jev calls per maintained position. The maintenance clock is separate from the review
  clock (`review`); the venue stays on the wall clock.
- 24-hour reviews and early exits (`CRYPTO_24H_REVIEW_V1`, from package answer-rules `_V2`,
  `EARLY_EXIT_AGREEMENT_V1`, package day-review): `execute --simulate-prints --hold-open` keeps
  each maintained report-V3 crypto
  trade open after its maintenance review. `review` moves the session's review clock (forward
  only; the venue and the protection loop stay on the wall clock): `--at request` to 30 minutes
  before the earliest open trade's T, `--at review` to T, `--minutes N` on by N minutes. From
  package answer-rules the clock then stays exactly there until the next `--at` or `--minutes`
  (a plain `review` does not move it), so an answer window never closes while the agent or the
  operator is answering; before the first move it is the wall clock. An exit that a `review` step
  causes (a 24-hour exit or an agreed early exit) is followed by one reconciliation, as
  `execute` does after a target exit, so the export's acceptance evidence can show the account
  clean after it (the printed exit line ends `reconciliation clean`). `--bid`
  sets the simulated bid (default: the trade's last simulated quote, +1R). It runs one pass of
  the day reviews and early exits, lets the protection loop act on what was decided (a raised
  stop replaced at the simulated venue, an exit sold at the simulated bid) and prints each
  trade's request, answers, Jev results, discussion, decision and flags, the agent's pending
  requests and the Jev calls used. The agent's side goes over HTTP with the `fable` token:
  `reviews` lists its pending requests, `review-answer` answers a review round, `exit-flag`
  raises its own flag (Jev is asked on the next `review`, which should run before the clock is
  moved past the flag's 15 minutes) and `flag-answer` answers a Jev flag; each takes
  `--sources /abs/sources.json` (0-8 sources). The fixture script answers the review with
  `"day_review"` (labels for `trade_reason`, `agent_case`, `decision`, `stop_option`,
  `target_option`; default `INTACT`, `HOLDS`, `CONTINUE`, `KEEP`, `KEEP`; an option label is
  `KEEP`, `first` or `last`), the final round with `"day_review_final"` and an agent's flag with
  `"early_exit"` (`trade_reason`, `agent_case`, `exit_now`; default `WEAKENED`, `HOLDS`, `EXIT`).
  Budget one Jev call per review round and one per agent flag.
- `--jev typesafe` sends reviews to the real pinned TypeSafe model and spends provider
  credits. Only the coordinator or the owner runs it. The key is loaded only by
  `catalyst_lab.jev_secrets.typesafe_key()` (the repository's `.env`, then the Keychain)
  inside the session process, so start it from the checkout that holds the owner's `.env`.
  `--fixture-script` is refused in that mode.

Evidence so far is fixture-only (`tests/test_agent_research_session.py`).

## Research-agent daily kit (research_agent/, package research-runner)

`research_agent/` is a standalone toolkit that makes the daily research run (owner loop,
`docs/CRYPTO-AGENT-LOOP.md` 4.1) repeatable without a throw-away script. It lives outside
`src/catalyst_lab`, is never imported by the app, and holds no execution authority: it
reads public market data and the app's own research context, builds one
`AGENT_RESEARCH_REPORT_V3`, and POSTs it with the agent's own token. The full step-by-step
sequence a scheduled research session follows is `research_agent/DAILY_PROCEDURE.md`; the
prompt a scheduler hands that session is `research_agent/DAILY_PROMPT.md`. This section is
how to run the kit by hand from the repository root.

Every command shares one dated run folder (default `runs/<YYYY-MM-DD>` in
America/New_York's date; pass `--run-dir` explicitly to pin it):

```sh
RUN_DIR=runs/$(TZ=America/New_York date +%F)
TOKEN_FILE=/abs/path/outside/the/repo/agent-token.txt   # mode-0600, never committed
BASE_URL=http://127.0.0.1:8000     # wherever the app runs; the Railway trader: https://<domain>

# 1. The research context (GET /api/v1/lab/research-context, the agent's own token).
./run python -m research_agent.run context --run-dir "$RUN_DIR" \
  --base-url "$BASE_URL" --token-file "$TOKEN_FILE"
#   --offline instead of --base-url/--token-file: Alpaca's public quotes, no key,
#   development only -- 'build' refuses to turn this into a submittable report.

# 2. Coinbase's public candles (no key) for every coin the context listed.
./run python -m research_agent.run market --run-dir "$RUN_DIR"

# 3. The level rules (research3/levels2.py's rules, the ones the real Jev accepted
#    twice on 2026-09-27, with the target-selection fix in docs/packages/research-runner.md).
./run python -m research_agent.run levels --run-dir "$RUN_DIR"

# 4. Research news/fundamentals by hand (DAILY_PROCEDURE.md morning step 7) and write news.json
#    next to the run folder -- this is the one step the kit cannot do itself.

# 5. Build the report: re-verifies every news.json excerpt, applies the 48-hour catalyst
#    rule, and checks every pick against the app's own models before report.json exists.
./run python -m research_agent.run build --run-dir "$RUN_DIR" --news /abs/path/news.json \
  --agent-id claude --agent-version 2026-09-28-r1
#   Omit --news for a CHART-only run. Read build-notes.json for anything skipped/rejected.

# 6. Re-check report.json against the app's models one more time.
./run python -m research_agent.run validate --run-dir "$RUN_DIR"

# 7. POST /api/v1/lab/research-reports, one of the kit's three network writes (with
#    outlook-submit and postmortem-submit below). It stamps generated_at at the moment of
#    sending (the intake refuses a report more than 60 s old) and saves that into
#    report.json first, so the file is what was sent.
./run python -m research_agent.run submit --run-dir "$RUN_DIR" \
  --base-url "$BASE_URL" --token-file "$TOKEN_FILE"

# Steps 1-3 and 5-6 in one command (never 7): python -m research_agent.run all
#   --run-dir "$RUN_DIR" --base-url "$BASE_URL" --token-file "$TOKEN_FILE" \
#   --news /abs/path/news.json --agent-id claude --agent-version 2026-09-28-r1
```

- `build` and `validate` check the report's agent block against the app's own patterns
  first (`catalyst_lab.agent_identity`: an ID of 2-32 lowercase characters, a version of at
  most 32), refusing `AGENT_ID_INVALID` or `AGENT_VERSION_INVALID`. The intake would refuse
  the whole report (422 `STRING_PATTERN_MISMATCH`), which the per-pick checks cannot catch
  (found on the first real session run, 2026-09-28).
- The token is read from `--token-file` (preferred) or the `RESEARCH_AGENT_TOKEN`
  environment variable, exactly once per command, and is never printed, logged, or
  written into any file this kit produces (`tests/test_research_agent_no_secrets.py`,
  `test_research_agent_cli.py`). Since package cloud (2026-09-27) the kit refuses a token
  file that is not a regular 0600 file of this user, and a plain `http://` base URL for any
  host but `127.0.0.1`/`localhost`: the Railway trader is `https://`
  (`tests/test_research_agent_cloud.py`, [RAILWAY-DEPLOYMENT.md](RAILWAY-DEPLOYMENT.md) §8).
- `context.json`, `market.json` and `levels.json` are plain JSON and safe to inspect by
  hand; `levels.json` records, per coin, either the qualifying setup or every
  rule/timeframe/window combination tried and why it did not qualify (a coin near the
  top of its range after a rally typically shows "the window high is its most recent
  bar" — a breakout setup, which stays disabled, not a bug).
- `--profile` on `market`, `levels`, `build` and `all` picks the research profile:
  `daily` (`DAILY_V1`, the default and the 08:00 run: 4h/6h/1d/2h/1h bars, entries 0.6-6%
  below the mid), `intraday` (`INTRADAY_V2`, the 2-hourly runs: the same bars shortest
  first, 1h/2h/4h/6h/1d, entries 0.3-6% below the mid, the daily fetch; it finds a setup for
  every coin the daily profile does, a 1-hour one where it qualifies) or `intraday-v1`
  (`INTRADAY_V1`, kept for comparison: 1h/2h/4h bars, entries 0.3-3%, a week of 1-hour
  candles and no daily candles). The stop, 2% stop-distance and 2R rules are the same in
  all three. `market.json`, `levels.json`
  (per coin) and `build-notes.json` record the profile; `levels` refuses market data
  fetched for a narrower profile (`MARKET_DATA_PROFILE_MISMATCH`) and `build` a profile
  other than `levels.json`'s (`LEVELS_PROFILE_MISMATCH`). The intraday run's steps are in
  `research_agent/DAILY_PROCEDURE.md`, "Intraday run (every 2 hours)" (package intraday-kit,
  2026-09-28; fixture evidence only).
- `--now` on `context`, `market`, `build` and `validate` overrides the clock (RFC3339)
  for a reproducible run; omit it for a real one.
- This kit adds no broker-order code path at all: it is read-only market data plus
  report building, and `submit` writes only to the app's research intake, never to a
  broker. Fixture-only evidence so far (`docs/packages/research-runner.md`); no real
  HTTP call or real Coinbase fetch has been made through it yet.
- `build --session` accepts `context --offline`'s output and builds against the
  supervised session harness (`scripts/agent_research_session.py`, "Supervised
  research-agent session" above), which has no research-context route but does run
  real selection/Jev-review code — the first real proof of this kit, before the owner's
  app is running. Submit that report with the harness's own `submit --report`, never
  this kit's `submit` (which refuses a `--session` report outright). See "Testing
  against a session harness" in `research_agent/DAILY_PROCEDURE.md`.

### The learning loop in the kit (package learning-kit, 2026-09-28)

`research_agent/DAILY_PROCEDURE.md` has the full morning and evening sequence, and the two
ways to run a day: two sessions (08:00, and after 02:00 New York), or one at 07:15 that
reviews yesterday before today's research. By hand:

```sh
AGENT="--agent-id claude --agent-version 2026-09-29-r1"
# Morning, in $RUN_DIR, after 'context':
./run python -m research_agent.run lessons --run-dir "$RUN_DIR"           # emphasis.json
./run python -m research_agent.run checklist export --run-dir "$RUN_DIR"
# ... market, levels, then:
./run python -m research_agent.run outlook --run-dir "$RUN_DIR"           # outlook.json
#   research, write news.json, fill outlook.json for every coin, then:
./run python -m research_agent.run outlook-check --run-dir "$RUN_DIR" $AGENT
./run python -m research_agent.run outlook-submit --run-dir "$RUN_DIR" $AGENT \
  --base-url "$BASE_URL" --token-file "$TOKEN_FILE"                        # before the report
./run python -m research_agent.run build --run-dir "$RUN_DIR" --news "$RUN_DIR/news.json" \
  --lessons "$RUN_DIR/emphasis.json" $AGENT                                 # then validate, submit

# Evening for day D, after the app's nightly job has recorded it (after 02:00 New York):
EVENING_DIR=runs/$D/evening
./run python -m research_agent.run context --run-dir "$EVENING_DIR" \
  --base-url "$BASE_URL" --token-file "$TOKEN_FILE"
./run python -m research_agent.run movers --day "$D" --run-dir "$EVENING_DIR"
./run python -m research_agent.run postmortem --day "$D" --run-dir "$EVENING_DIR"
#   research why each subject moved, fill postmortem.json, then:
./run python -m research_agent.run postmortem-check --day "$D" --run-dir "$EVENING_DIR" \
  --agent-id claude
./run python -m research_agent.run postmortem-submit --day "$D" --run-dir "$EVENING_DIR" \
  $AGENT --base-url "$BASE_URL" --token-file "$TOKEN_FILE"
./run python -m research_agent.run checklist update --from "$EVENING_DIR/movers.json" \
  --from "$EVENING_DIR/postmortem-submit.json" --lessons "$EVENING_DIR/context.json"
```

- `postmortem` refuses with `REALITY_DAY_NOT_RECORDED` until the context's lessons record the
  day; read the context again after the nightly job (`30 5 * * *` UTC) rather than build a
  partial worksheet.
- Both sends re-fetch every cited page: the excerpt must verify, and a `published_at` must be
  one of the page's own metadata times. `outlook-submit` also reads the research context
  again and saves it as `context.json`, so a coin that joined the universe is caught before
  the POST and `outlook` can add it.
- `outlook-submit` saves the exact bytes it sends (`outlook-sent.json`) and every outcome
  (`outlook-submit.json`). After no answer it resends those bytes unchanged first, and it
  never sends a recorded outlook again. `--new-id` is only for a deliberate second outlook.
- The checklist counts post-mortem evidence only from the items the app accepted
  (`postmortem-submit.json`), never from the worksheet.
- The research checklist is kept outside the repository in an owner-only folder
  (`--state-dir`, default `~/.local/share/catalyst-retest-lab/muse-state/`; directory 0700,
  file 0600, written whole and renamed). It is not a ledger. `checklist show` prints it.
- Lessons and the checklist only reorder research and picks: `build --lessons` applies its
  30-pick limit before any hint, so the same coins are picked either way.
- Fixture evidence only (`docs/packages/learning-kit.md`): no real app, Coinbase or news
  call has been made through these commands yet.
