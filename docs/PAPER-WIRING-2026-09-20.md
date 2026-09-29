# Paper wiring and operational handoff — September 20, 2026

This checkpoint implements the requested research-to-paper-trade loop in the isolated
`catalyst-paper-wiring` checkout. Schema **14** and the new wiring were exercised in disposable
PostgreSQL databases. The existing credential-holding worker and account ledger were preserved.
Nothing in this checkpoint activates a new account executor, installs LaunchAgents, migrates
the existing ledger, authenticates the repaired Codex CLI, or demonstrates trading performance.

## Implemented flow

1. **External Muse** researches stocks and crypto with public technical/news evidence, proposes
   levels, and submits reports. Its durable private SQLite spool retains provider jobs, output
   delivery attempts and report/evidence/news work across restarts. Stable IDs prevent an uncertain
   result from being rerolled for a more favorable answer. It targets 20–30 supported contenders
   without padding; fewer or no contenders is a valid outcome. The app has no discovery scanner.
2. **Selection Jev** receives versioned economic and technical evidence, bounded source excerpts,
   and materially revised answers to evidence requests. The explicit policy separates a fresh
   packet's 60-second age limit from its 1,800-second review-validity window. New quality-gated
   records require technical provenance and level justification. Rejection stays rejection;
   the maximum ten selections is a cap, not a quota or permission to trade.
3. **The app** owns acknowledged market streams, the frozen US trigger, independent sizing and
   account risk, exact paper requests, protection and reconciliation. Every POST/DELETE/PATCH
   still needs its own committed, exact, unused five-second capability. A process-lifetime
   database lease covers account identity and the legacy executor lock; ownership is checked
   again at the final transport boundary. Loss of ownership requires a new process and clean
   startup reconciliation before submissions can resume.
4. **Management Jev** sees the original selection/research, economic link, technical evidence,
   current position and protection, recent bars/news and bounded management history. New material
   news and near-target progress can schedule a review independently of the old bar/news key.
   It selects only validated price options; protective work proceeds independently of model work.
   Tightening/extension never changes size or removes the hard exit deadline.
5. **Recovery and account safety** latch every crypto HALTED recovery result as a durable entry
   halt while preserving residual inventory/protection work. The daily cancel-and-flatten path
   includes legacy and managed exposure, including accounts with only legacy positions. Pending
   exits, late partial fills and unknown submissions retain durable ownership through restart.
   An unavailable legacy safety pass blocks entries without stopping managed protection.
6. **Measurement** retains fill provenance, explicitly supplied USD fees and append-only fee
   evidence/corrections. Missing fee evidence remains unknown. Net P&L requires verified costs
   and reconciled inventory; noncash/base-asset fees are accounted for without subtracting the
   same inventory loss twice. Engineering cohorts remain separate from strategy performance.

The main modules are `muse_worker.py`, `muse_codex.py`, `research_cycle.py`,
`position_monitor.py`, `managed_execution.py`, `managed_account_safety.py`,
`executor_lease.py`, `crypto_liquidity.py`, `managed_measurement.py` and `managed_ops.py`.
These changes preserve the frozen US baseline and do not upgrade historical setups to new policies.
India remains research-only.

## Local evidence and its limits

The retained proof is outside the repository at:

```text
~/.local/share/codex/reviews/catalyst-retest-lab/2026-09-20-wiring-proof/proof.json
```

It reports `ENGINEERING_FIXTURE_ONLY`, mocked Jev/Alpaca Paper, synthetic market data,
and a real isolated PostgreSQL ledger. It completed **20 contenders, 40 research receipts,
10 selections, two management receipts, four fills and two CLOSED lifecycles** (one stock,
one crypto). Nine exact risk claims were consumed. The fixture broker ended with no positions,
no active risk reservations, and clean reconciliation. **Actual Jev calls and actual orders: zero.**

The audit chain verifies **420 events**, with retained head:

```text
c05f133e0cd522652767a988f36cd6d3a1b882b3d3c6f191e94fa06811cc1e57
```

**Final combined suite: 1,076 tests passed** in 169.77 seconds, with two existing dependency
deprecation warnings. Ruff and diff checks passed. The final worker/operations subset passed
31 tests; mixed historical-cycle checks passed 51; analytics/API checks passed 55. These
subsets overlap the combined suite and must not be added to it. Logs are retained beside
`proof.json` as `final-full-pytest.txt` and `final-ruff.txt`. The proof is plumbing and recovery
evidence, not a return, fill-quality or reliability study.

The earlier [September 19 real Muse cycle](MUSE-REAL-CYCLE-2026-09-19.md) is separate:
three hypotheses, five actual Jev reviews, all three final dispositions REJECTED, zero orders.
The still earlier [36-pair technical pre-screen](REAL-MANAGED-SESSION-2026-09-19.md) produced
no eligible setups and no Jev calls. Neither establishes real managed entry/amendment/exit acceptance.

## Explicit paper engineering settings

The [nonsecret example](../deploy/private-paper.example.json) supplies each policy explicitly.
It is a configuration template, not an activated configuration or a calibrated trading strategy.

| Setting | Example and consequence |
| --- | --- |
| Research | Fresh packet age 60 seconds; review validity 1,800 seconds; technical evidence required; up to ten selections and five simultaneous reviews |
| Crypto day policy | `CRYPTO_NY_DAY_PAPER_V1`; no new entries after 23:50 America/New_York; flat deadline 23:55; maximum elapsed hold 86,400 seconds, anchored to the actual first fill and capped at the admission day's flat deadline |
| Restart/partial fills | Saved policy/deadline survives restart; later partials do not extend the holding window; missing first-fill timing halts entry and starts protective exit recovery |
| Management schedule | `JEV_MONITOR_SCHEDULING_ENGINEERING_V1`; five-second minimum interval; near-target review at 0.8 of entry-to-target progress; scheduling does not authorize an amendment |
| Crypto liquidity | `CRYPTO_LIQUIDITY_PAPER_V1`; 60 completed bars, minimum dollar volume 1,000,000, maximum participation 0.01, assumed round-trip cost 50 bps, maximum estimated cost/risk 0.25 |
| US classification | `[]` intentionally disables stock admission until the operator supplies real independent sector/theme mappings; Muse cannot supply those risk classifications |
| Crypto classification | `null` allows broker-eligible USD-pair metadata; it does not initiate app-side discovery or waive liquidity checks |
| Proposed API | `http://127.0.0.1:8780`, shared by the future combined app, Muse and watchdog; existing ports are not repointed |

The cost assumption is an admission estimate, not a verified fee or promise of execution quality.
The new day/liquidity/scheduling policies are explicit opt-ins for new records; historical defaults
and existing setup provenance remain intact. Real spread, depth, fill behavior, transaction costs,
latency and research quality still require supervised paper observation and calibration.

## Private configuration and static preflight

**Updated September 24 (plan package 0.7): immutable release and private configuration v2.**
Launches no longer run from a working copy, and one token no longer covers Muse, status and the
watchdog. A v1 file still loads, marked with a `deprecation` field and reported by preflight as
`CONFIG_VERSION_2_REQUIRED`; rendering and launching refuse it (`PRIVATE_CONFIG_V2_REQUIRED`).

All commands below are handoff instructions; they were not used to activate the existing account.
Choose reviewed paths outside the repository. Do not source the JSON or put credential values into
shell commands, chat or logs.

### Build an immutable release

From a clean checkout (any modified, staged or untracked file is refused), build a release from a
tag or commit:

```sh
./run python scripts/build_release.py --ref REQUIRED_RELEASE_TAG_OR_COMMIT
```

The script exports the committed tree with `git archive` (never the working copy or ignored files)
to `~/.local/share/catalyst-retest-lab/releases/<commit>/` and refuses an existing directory there.
It creates the release's own `venv` with `uv sync --locked` (runtime dependencies only, with
`UV_PROJECT_ENVIRONMENT` and `UV_LINK_MODE=copy` set explicitly so no shared or cached files are
reused). The release's own interpreter then reports where `catalyst_lab` imports from (the exported
`src`) and its hash. `release.json` records `{commit, source_sha256, built_at, python}`, and every
file and directory loses its write permission. A failed build removes only the directory it created.
Nothing is installed, started, migrated or pushed. `source_sha256` is SHA-256 over the package's
`.py`/`.sql` files: sorted package-relative paths, length-framed, `__pycache__` ignored, and any
symlink counted. The exported tree and the imported package therefore hash identically. Static
dashboard assets are not covered. The app status reports that hash as `code_version`; it reports
`release_commit` from the `release.json` at the release root only when the hashes match.

### Configuration v2

```sh
RELEASE="$HOME/.local/share/catalyst-retest-lab/releases/REQUIRED_COMMIT"
PY="$RELEASE/venv/bin/python"
PRIVATE='/REQUIRED/private-paper'
umask 077
mkdir -p "$PRIVATE"
chmod 700 "$PRIVATE"
install -m 600 "$RELEASE/deploy/private-paper.example.json" "$PRIVATE/launch.json"
```

`"$PY" -m catalyst_lab.managed_ops template --destination "$PRIVATE"` prints the same layout with
the `release` section already filled when it runs from a verified release. The loader requires
exactly these keys:

| Section | Keys and meaning |
| --- | --- |
| `config_version` | `2` |
| `environment` | The allowlisted names as before, **without** `MANAGED_API_TOKEN` |
| `muse`, `watchdog`, `audit` | As before; `watchdog.token_file` is gone (the watchdog uses the status token) |
| `status`, `operator` | `token_file` each (see the role table below) |
| `release` | `directory` (the built release) and `source_sha256` (copied from its `release.json`) |
| `ledger` | `data_directory`, `socket_directory`, `pg_ctl`, `marker_file` (its `LEDGER.json`), `wait_seconds` (1–600, default 120) |
| `backup` | `directory`, `retention_days` (1–365, default 14); used by plan package 4.6 |
| `notify` | `{}`; a placeholder for off-host alerts (plan package 4.2), must stay empty |
| `logs` | `directory`, `max_bytes` (64 KiB–1 GiB, default 20 MiB), `backups` (1–20, default 5) |
| `agents` | Optional (2026-09-24, plan 1.3): a list of `{agent_id, token_file}` research-agent credentials, one file per agent; `[]` or absent means none |

Edit that private copy. Replace every `REQUIRED` credential, connection and path placeholder.
The account-risk and Jev connections must target the same reviewed ledger with their restricted
roles; the audit connection uses the restricted `catalyst_app` role. The schema must already exist;
the launcher does not provision or migrate a database. Supply paper-only Alpaca credentials
privately. Private configuration/token files must be regular files owned by the user, mode0600,
with no symlink or extra hard link. Their directories should be mode0700.

Create three **different** tokens (at least 32 characters, no whitespace), one per file, written
straight to the file and never printed, pasted into a command line or shared in chat:

```sh
for ROLE in muse status operator; do
  "$PY" -c 'import secrets; print(secrets.token_urlsafe(48))' > "$PRIVATE/$ROLE-token"
  chmod 600 "$PRIVATE/$ROLE-token"
done
```

| Token file | Used by | Accepted for |
| --- | --- | --- |
| `muse.token_file` | Muse (`--token-file`); injected into the app as `MANAGED_API_TOKEN` | POST research reports; GET the output feed and cycle outputs; POST evidence-task claims and evidence; GET positions; GET/POST position news. These are exactly the routes the Muse HTTP client allows |
| `status.token_file` | watchdog; the private page | every authenticated GET; never a POST |
| `operator.token_file` | nothing yet | no route (reserved for the operator HTTP of plan package 4.3) |
| `agents[].token_file` | one research agent each (for example Instinct or Grogbot); injected into the app as `MANAGED_AGENT_TOKENS_JSON` | Muse's routes, acting only as its own `agent_id`: V2 reports naming that agent, and claims, evidence and position news for its own cycles and setups |

The Muse token is the legacy identity: agent `muse`, and the only token that may submit
unversioned legacy reports. A token acting for another agent receives 403
`AGENT_IDENTITY_MISMATCH` (see [API-CONTRACT.md](API-CONTRACT.md)). Agent tokens must differ
from each other and from the three role tokens, and never reach Muse's environment.

An unknown token receives 401; a known token outside its role receives 403
`TOKEN_ROLE_NOT_PERMITTED`. The launcher injects the three tokens into the app process only
(`MANAGED_API_TOKEN`, `MANAGED_STATUS_TOKEN`, `MANAGED_OPERATOR_TOKEN`) and refuses to start it
if any file is missing or two tokens are equal. Muse receives none of them in its environment,
only its own token file path. An app started with only `MANAGED_API_TOKEN` (legacy scripts, v1)
keeps the previous single-token access unchanged.

The example uses **only `TYPESAFE_ENV_FILE`** for Jev. Create that owner-readable mode0600 file
privately with the expected `TYPESAFE_API_KEY` entry. No inline TypeSafe key is included in the
template; do not leave a second conflicting key source. The loader reads the file as data and
does not execute shell content. A present invalid credential file fails closed.

Set `muse.command[0]` to an explicit working executable. The independently repaired installation
at `~/.local/share/catalyst-retest-lab/muse-codex/node_modules/.bin/codex`
reported **0.155.1** and its `exec --help` was checked. No authentication or provider request was
performed by that check. The example deliberately uses an absolute `REQUIRED` placeholder rather
than the broken global `codex`. Keep the supplied live-web/read-only/ephemeral arguments and
`--ignore-user-config`; source and app credentials are not passed to the research child.

```sh
"$PY" -m catalyst_lab.managed_ops preflight --config "$PRIVATE/launch.json"
```

This prints only configuration names, dependency availability, expected schema and verification
scope, plus four v2 reports. `config_version` and any `deprecation`. `release`: the imported and
configured hashes, the imported release commit and `launch_check`, which is `VERIFIED` or the
launcher's refusal code. `token_separation`: each role file `PRIVATE_VALID` or
`UNAVAILABLE_OR_INVALID`, whether the three values differ, never a value. `ledger`: whether
`LEDGER.json` marks the directory `ACCOUNT_LEDGER`, and whether `pg_ctl`/`postgres` and an
initialized data directory are present. Placeholders appear as `section.key` in
`missing_configuration_names`. Run it with the release's own interpreter. From a working copy the
release check reports `RELEASE_HASH_MISMATCH`. It does not load broker credentials or call a
provider. Executable presence is separate from authentication. It always reports
`ready_to_trade: false`. The old `--checkout` argument is accepted and ignored. For an explicitly
selected ledger, the optional read-only schema check is:

```sh
"$PY" -m catalyst_lab.managed_ops preflight \
  --config "$PRIVATE/launch.json" --check-database
```

That command does not migrate, reconcile or prove the three roles share the correct account ledger.
Secure startup injection for the existing account has not been completed by this implementation.

### Prepared local package (September 20, superseded)

The private review package is already rendered at
`~/.local/share/catalyst-retest-lab/paper-wiring-review-20260920`.
It has four uninstalled LaunchAgents, a private configuration with a newly generated local API
token and matching token file, a redacted preflight report, and a handover README. Its dedicated
Python environment is under `~/.local/share/catalyst-retest-lab/wiring-release/catalyst-retest-lab/venv`;
the plists point to the original project checkout and the repaired CLI above. The active worker's
environment and credentials were not used to prepare it.

Static preflight found no invalid policy settings and all required Python modules plus the Muse
executable. Broker credentials, restricted database connections, TypeSafe file, independent stock
classifications and provider authentication remain unverified or deliberately absent. The audit
read connection also remains a placeholder. No LaunchAgent is installed or started.

That package is a **v1** configuration. Its venv is an editable install of the mutable working
copy, its plists send output to `/dev/null`, it has no ledger component, and one token serves Muse
and the watchdog. With the September 24 code, its launches refuse with `PRIVATE_CONFIG_V2_REQUIRED`.
Replace it by building a release, writing a v2 configuration with three new tokens and rendering
into a new directory. It was not modified, and none of its token or credential files were read.

## Supervisor and watchdog preparation

```sh
"$PY" -m catalyst_lab.managed_ops render-supervisor \
  --config "$PRIVATE/launch.json" --destination "$PRIVATE/launchagents-REQUIRED_COMMIT"
```

Rendering refuses a v1 configuration, an unverified release (`release.json` missing or the tree's
hash differing from `release.source_sha256`) and an existing destination directory
(`RENDER_DESTINATION_EXISTS`). It exclusively creates six private plists in start order:
**ledger → app → Muse → watchdog → audit → backup**. It does not install or start them. Every plist
runs `$RELEASE/venv/bin/python $RELEASE/scripts/run_managed_private.py --config … --component …`
with the release as working directory. The plists contain the private config path, not secret
literals, and no `EnvironmentVariables`. Components get an allowlisted JSON environment and direct
argv; there is no shell `source`/`eval`. The ledger, app, Muse and watchdog use
`KeepAlive {SuccessfulExit: false}` with a 10-second throttle. The ledger and app run as
`ProcessType Standard` (Background throttles CPU and disk I/O) with `ExitTimeOut 30`. The audit
job runs every `audit.interval_seconds`. The backup job is a daily **placeholder**: `managed_ops
backup` refuses with `BACKUP_NOT_IMPLEMENTED` until plan package 4.6.

What the launcher does on every start:

1. **Crash-loop breaker.** Each start is recorded under `launcher-state/` beside the private
   config (directory 0700, files 0600), which works even when the configuration cannot be
   parsed. The fifth start within ten minutes writes `crash-loop-<component>.json` and exits 0,
   so launchd stops restarting. After ten quiet minutes the next start runs and clears the alarm.
   A tripped breaker is only released by that wait or by deleting its state file after the fix.
2. **Release check.** The imported package must hash to `release.source_sha256`
   (`RELEASE_HASH_MISMATCH`), live inside `release.directory` (`RELEASE_PATH_MISMATCH`) and match
   that release's `release.json` (`RELEASE_METADATA_MISSING` or `RELEASE_METADATA_MISMATCH`).
3. **Private logs.** Output goes to `<logs.directory>/<component>.log`, mode 0600, rotated at
   `logs.max_bytes` into `.1` to `.N`, and never to `/dev/null`. The launcher stays as the
   parent, pumps the child's stdout and stderr into the rotating log, forwards launchd's SIGTERM
   and returns the child's exit status. launchd's own `StandardOutPath`/`StandardErrorPath` is
   `<component>.launchd.log` in the same 0700 directory. The launcher tightens it to 0600 at
   start, and it only receives output from before the rotating log opens. A logging failure is
   counted, never allowed to stop the child.
4. **Ledger.** `postgres -D <data_directory> -k <socket_directory>` runs in the foreground from
   the `pg_ctl` installation, and launchd's SIGTERM becomes SIGINT (PostgreSQL fast shutdown).
   It starts only when `LEDGER.json` says `ACCOUNT_LEDGER`, so an `ARCHIVED` or unmarked cluster
   is never served. It never runs initdb or a migration.
5. **Database wait.** Before the app or Muse starts, the launcher probes `MANAGED_DATABASE_URL`
   with bounded backoff (1, 2, 4, 8, then 15 s) for at most `ledger.wait_seconds`, then exits
   non-zero; launchd retries after its throttle. An unset DSN is refused outright. After a
   reboot the app therefore waits for the ledger instead of restarting every 10 s against a
   stopped database.

The watchdog performs a bounded loopback status read with the **status** token and atomic mode0600
writes to the configured alarm file. It reports stopped workers, stale
protection/reconciliation/research, lost required streams or executor ownership, account-safety
failure, stale Muse heartbeat, exhausted deliveries, expired pending results and failed/stalled
work. It also reports `RELEASE_CODE_MISMATCH` when the running app's `code_version` differs from
`release.source_sha256`, and `CRASH_LOOP_BREAKER_TRIPPED` / `CRASH_LOOP_<COMPONENT>` for every
tripped launcher. A fresh heartbeat cannot mask an exhausted outbox. It never includes
request/source bodies, raw provider errors or credentials. This is a persistent local alarm, not an
off-host notification service; a watchdog that is itself tripped leaves a stale alarm file, and the
off-host dead-man alert of plan package 4.2 covers that gap. A manual check, after an intentional
launch, is:

```sh
"$PY" -m catalyst_lab.managed_ops watchdog-once --config "$PRIVATE/launch.json"
```

## Controlled handover remains separate

The existing internal executor at port8768, read-only dashboard8769 and external intake8770 were
preserved. They were not queried or restarted for this implementation. Their loaded code is older
than this checkout; source edits alone do not update a running Python process. The proposed
port8780 is unactivated and must not become a second executor for the same paper account.

Before activating any rendered supervisor, prepare secure credentials, the reviewed schema and
configuration, and one durable account ledger. Reconcile the existing process/account, resolve
unknown exposure or pending exits, and stop the old executor through a controlled handover.
Then verify only one new executor owns that same account/ledger and startup reconciliation is
clean before admitting fresh reports. Preserve old evidence and never copy fixture records into
the account ledger. Any migration and process stop/start require their own deliberate operational
step; no such action was performed here.

The lease enforces exclusivity among participating executors on the **same PostgreSQL ledger**.
It is not broker-side distributed fencing: an old unpatched worker or another database can evade
that lock. It does not replace reconciling and stopping the old credential-holding process.
Actual Alpaca Paper entry, partial fill, amendment, hard exit, restart and fee/reconciliation
acceptance remain required before describing this installation as operating unattended.

## Audit checkpoint verification and evidence recovery

The rendered audit component uses a restricted connection in read-only transactions and writes
an exclusive JSONL checkpoint plus manifest containing count, chain head and file SHA-256.
For a deliberate one-shot export from the reviewed ledger:

```sh
"$PY" "$RELEASE/scripts/run_managed_private.py" \
  --config "$PRIVATE/launch.json" --component audit
```

Retain the expected head independently. Verification and recovery use local files only:

```sh
EXPORT='/REQUIRED/checkpoint.jsonl'
INDEPENDENT_HEAD='REQUIRED_INDEPENDENTLY_RETAINED_SHA256'
RECOVERED='/REQUIRED/new-private-recovery/checkpoint.jsonl'
"$PY" -m catalyst_lab.managed_ops verify-checkpoint \
  --file "$EXPORT" --expected-head "$INDEPENDENT_HEAD"
"$PY" -m catalyst_lab.managed_ops restore-audit \
  --file "$EXPORT" --destination "$RECOVERED" --expected-head "$INDEPENDENT_HEAD"
```

Recovery verifies the file hash, chain/count and independent head before copying evidence to a
new destination. Tests cover a disposable ledger, successful byte-for-byte recovery, incorrect
heads and tampering. The declared scope is `AUDIT_EVIDENCE_ONLY_NOT_DATABASE_BACKUP`.
This does not restore PostgreSQL, executable state, reservations or a running trading session.
Off-host retention and a full database backup/restore drill remain separate operational work.
