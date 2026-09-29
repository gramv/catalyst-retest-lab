# Catalyst Retest Lab — project checkpoints (the README until 2026-09-28)

The dated checkpoints below were the repository README until 2026-09-28, when the README became the
project's public front page. They describe their own time, not the current state; the live record
is [PHASES.md](PHASES.md).

**PAPER TRADING — SIMULATED. Not real money.**

**License.** MIT, see [LICENSE](../LICENSE). The IBM Plex fonts in `src/catalyst_lab/static/fonts/` are
under the SIL Open Font License 1.1 ([OFL.txt](../src/catalyst_lab/static/fonts/OFL.txt)). The run
evidence (`artifacts/`: captured pages, provider responses, ledger exports) and every credential
stay private; two replay tests skip without `artifacts/`. Execution is paper-only by design.

**Status 2026-09-24.** The owner ruled that there is one paper account and that numeric risk
rules are versioned, not frozen. The approved product plan (research agents → Jev selection →
execution → monitoring Jev) is summarized in [PHASES.md](PHASES.md) and
[CONTRACT-RESOLUTIONS.md](CONTRACT-RESOLUTIONS.md). Only the managed engine will run on the
account; `CATALYST_RETEST_V1` is the archived baseline. Nothing has been running since the
2026-09-23 reboot. The paragraphs below are dated checkpoints that describe their own time, not the
current state.

A paper-only trading laboratory: a frozen US `CATALYST_RETEST_V1` baseline and a
separate local US-stock/crypto engineering workflow. India remains research-only.
This repository implements the core, watcher, execution machinery and **Phase 4 risk engine**,
using FastAPI, Python and PostgreSQL. The risk engine authorizes each exact broker mutation with a
committed, unexpired, one-use database decision. The default runtime remains read-only. Phase 4's
controlled real Alpaca Paper acceptance trade is outstanding; local tests do not replace it.
Phase 5–6 measurement and a separate read-only dashboard are implemented locally; official R and public HTTPS deployment remain pending. See [PHASE-5-6.md](PHASE-5-6.md).
Existing Vector Wrap projects are separate.

**September 20 Muse research:** [versioned guidelines](MUSE-GUIDELINES.md) are
embedded in external research, evidence-follow-up and position-news prompts. The
[US stock dossier](MUSE-US-STOCKS-2026-09-20.md) retains a 50-stock technical
screen, primary-news review of 37 stocks, 25 research cases and eight priority
watchlist names for September 21. Sunday preparation is not a Jev selection or
an order. No worker activation occurred. Full suite: **1,232 passed**, Ruff clean.

**September 20 Jev validation:** [bounded diagnostic results](JEV-VALIDATION-2026-09-20.md)
cover 60 synthetic cases, a frozen independent AI review, 60 real Jev calls and a standalone
crypto shadow evaluator/public-feed collector. Held-out agreement was 22/26 valid agreed cases
(22/27 including one malformed response). This does not validate trading performance or the
prior real-candidate rejections. The existing selector and broker runtime were not activated
or changed by this diagnostic.

**September 20 implementation handoff:** [paper wiring and operations](PAPER-WIRING-2026-09-20.md)
documents schema **14**, the external Muse worker, richer selection/management context,
account-wide recovery, executor ownership, opt-in crypto day/liquidity policies, verified-cost
reporting, and private launch/watchdog/audit tooling. The isolated PostgreSQL proof completed
20 fixture contenders, 40 research receipts, 10 selections, two management receipts,
four fills and two closed lifecycles; its 420-event audit chain verifies. **No actual provider
calls or orders were made by this proof.** **Final combined verification: 1,076 tests passed; Ruff clean.**

These changes have **not been activated** on the credential-holding worker at port8768
or the separate Muse intake at port8770. The proposed combined backend uses port8780 only
after a controlled handover. Secure startup injection, real US sector/theme classifications,
provider authentication and actual new-workflow fill/management/exit acceptance remain gates.
Use the [nonsecret configuration example](../deploy/private-paper.example.json) and handoff
instructions; do not start a second account executor. Historical checkpoints follow.

The [September 19 managed worker session](REAL-MANAGED-SESSION-2026-09-19.md)
recorded Alpaca Paper authentication and clean reconciliation, with a private read-only
dashboard at port8769. Its initial 36-pair scan produced no setups or Jev judgments.
The later [external Muse cycle](MUSE-REAL-CYCLE-2026-09-19.md) submitted three hypotheses
and retained five real Jev calls; all three ended REJECTED, with zero orders. These are
separate dated observations, not a current health check or evidence of trading performance.

Latest correction: [Muse owns discovery and research](MUSE-RESEARCH-BOUNDARY.md).
The current Muse-facing app at <http://127.0.0.1:8770> accepts external research reports
directly and shares the existing worker queue. It does not run a discovery scanner
or require scanner approval before Jev. Historical scan checkpoints below are retained as
history; they do not describe the corrected intake. Runtime reload status is in that report.

Latest integration milestone: [stock/crypto managed wiring](MANAGED-WIRING-2026-09-19.md).
The opt-in local workflow connects external Muse reports, evidence revisions, selection Jev,
live-print watchers, shared risk, paper adapters and a separate contextual management Jev.
939 backend tests, 12 browser checks and lint pass. The complete fixture cycle and simpler
private page are verified locally. Real-provider Jev
contract calls passed; actual Alpaca acceptance for this new workflow is still outstanding.
The existing account runtime/database is unchanged. India remains research-only.

The owner has expanded the requested build to app-hosted Jev selection and contextual trade
management, with paper execution for US stocks/crypto and research-only Indian stocks. The frozen
baseline remains US-only; the separate managed engineering profile includes crypto. Earlier
architecture checkpoints are retained in [the architecture history](MUSE-JEV-MULTIMARKET.md).

Latest local checkpoint: app-owned research worker, durable output polling, private shortlist
and failure/restart tests are implemented. **538 backend tests and 16 browser checks pass.**
Six new fixed real-Jev diagnostic cases matched their expected research dispositions; this
is not evidence of trading performance. [Worker proof and current boundaries](REVIEW-WORKER-LOCAL.md).
No new trading integration or Railway deployment is enabled by this milestone.

## Current behavior

- Authenticated single/batch Muse candidate intake, dated polling, readback, event history and counts.
- Every candidate in an authenticated, valid request becomes an immutable record,
  including candidate schema failures and duplicate attempts. Malformed envelopes,
  invalid/oversized transport requests and unauthorized requests are rejected before intake.
- `RECEIVED → VALIDATING → REJECTED | VALIDATED`; successful validation does not create an order.
- Durable WATCHING observer, mechanical printed-trade trigger, stop/chase/feed invalidations,
  calendar expiry and audited limit-at-max-entry intent.
- Whole-share DAY brackets, limit at M, stop at S and target at P; no Muse sizing input.
  Sizing uses live broker equity and `M − S`, with no leverage. The original Phase 3 client remains
  locked. The separate Phase 4 transport requires an exact, unused risk decision for every mutation.
- Atomic 1% reservations include working entries and filled positions. A PostgreSQL lock and
  database guards enforce the 2% combined budget and server-owned sector/theme limits.
- Durable 3% daily loss halt cancels working orders, flattens open positions and blocks new entries
  for the session. This follows the user's Phase 4 ruling, which supersedes the original v3 halt.
- Uncertain submissions recover by stable client-order-ID lookup before any further action.
  Missing/rejected protection invokes an audited cancel/flatten workflow; unresolved exits block entries.
- Paper trade-updates authentication/subscription, immutable raw events, individual fills,
  deduplication, candidate transitions, and event-derived open orders/positions.
- Startup and 45-second reconciliation detect every discrepancy without auto-repair. Each restart
  must reconcile cleanly before watching. Unknown exposure produces a durable risk halt.
- Calendar close-minus-five-minute exits for owned strategy positions. In Phase 4, each cancellation
  and market close has its own risk authorization. Without risk configuration these remain locked.
- Preliminary checks for levels, 2R minimum, max entry, exchange-calendar session and expiry,
  asset eligibility, feed health, quote age, spread, dollar volume, reconciliation,
  correlation, planned exposure, and realized-plus-unrealized daily loss.
- DB claims enforce one attempt per ticker per New York calendar day, including rejected
  attempts, and globally unique signal IDs. Duplicate requests receive new rejected records.
- PostgreSQL supplies event identity, sequence, timestamp, predecessor and SHA-256 hash.
  One transaction commits candidate intake, decisions and event history atomically.
- A restricted application role has only SELECT/INSERT on implemented input tables;
  UPDATE, DELETE and TRUNCATE are denied. Triggers independently reject those mutations.
- All requested tables are present. Orders, fills, observations, watch checkpoints, reconciliation,
  intents and halts are persisted with append-only enforcement and audit references.
  Trade projections derive from broker events and market observations; daily rollups append revisions.
  The reporting role cannot mutate source ledgers or the projection.
- Audit export and independent verification commands, with an optional retained head hash.

The default server has **no complete candidate-admission evidence provider** and rejects otherwise
valid candidates. The observer connects to Alpaca and performs reconciliation. Production admission
still needs asset/liquidity evidence and approved data-quality policy. Tests inject labeled
`LAB_FIXTURE` evidence into disposable databases; fixtures never enter the running ledger or headline
statistics. No boolean bypass can enable submission. See [PHASE-4.md](PHASE-4.md) for the current
authorization boundary and frozen operational choices; [PHASE-2.md](PHASE-2.md) covers the
read-only observer setup.

## Run locally

Prerequisites: Python 3.12+, `uv`, and local PostgreSQL binaries (`initdb`, `pg_ctl`).

```sh
cd '~/Projects/catalyst-retest-lab'
./run catalyst-lab dev-init
./run catalyst-lab serve
```

The read-only dashboard runs with `./run catalyst-lab dashboard --port 8766` at <http://127.0.0.1:8766>.

The API binds only to `127.0.0.1:8765`. Open <http://127.0.0.1:8765/docs> for the local API UI.
The development database uses its own `~/.local/share/catalyst-retest-lab/dev/postgres`
cluster (the `--local-dir` default), outside Documents/iCloud, with a private Unix socket and
no TCP listener. It does not touch existing PostgreSQL services. A random Muse token is saved
with mode 0600 in `~/.local/share/catalyst-retest-lab/dev/muse-token`.
Use it as a Bearer token; do not commit or paste it into logs.
For deployment, provision password/SCRAM or equivalent authentication and use the restricted role;
the local private-socket trust setup is only for this workstation.

`dev-init` migrates only a fresh cluster. An existing cluster with pending migrations is refused
(`PENDING_MIGRATION_REQUIRES_OWNER_STEP`), and an owner-written mode-0600 `LEDGER.json` marker
(`{"role": "ACCOUNT_LEDGER"}` or `{"role": "ARCHIVED"}`) refuses initialization and migration
outright; an unreadable marker fails closed. Always name an owner ledger with `--local-dir`.
Its schema changes only through the explicit owner step, run against a cluster already started
with `pg_ctl` (the command never starts or initializes one):

```sh
./run catalyst-lab ledger-migrate --local-dir DIR --expect-current N --target M \
  --backup-manifest PATH
```

It refuses unless the ledger is exactly at `N`, every migration file through `M` exists, the
backup manifest is a non-empty regular file owned by the current user and the ledger is not
`ARCHIVED`. Migrations `N+1..M` run in one owner transaction under the audit-append lock. The JSON
result reports the schema version and the audit head/event count before and after. A batch that
changes the head carries a `warning`; one that removes or rewrites audit rows is rolled back.
To upgrade a development cluster, use the same step or recreate the `dev` directory.

The `run` helper synchronizes locked dependencies into
`~/.local/share/catalyst-retest-lab/venv`, outside Documents/iCloud. On macOS it also clears
inherited hidden flags on Python `.pth` files, which otherwise prevent editable imports.
Python bytecode is also cached outside iCloud to prevent startup stalls on evicted cache files.
The active checkout was cloned with its Git history out of Documents/iCloud after repeated source
file read timeouts. The original `Documents/Catalyst Retest Lab` copy was preserved unchanged.

```sh
./run pytest -q
./run ruff check src tests
./run catalyst-lab export
./run catalyst-lab verify --file exports/REPLACE_WITH_EXPORT.jsonl --expected-head RETAINED_HASH
./run catalyst-lab dev-stop
```

Export files are created exclusively rather than overwritten. Retain each manifest/head outside
the database host. The September 20 tooling can render an hourly local audit-export supervisor;
it has not been installed. Off-host backup retention and a full database restore remain unproven.
A hash chain cannot detect a privileged administrator rewriting the entire log without an
independently retained checkpoint; database owners can disable triggers. The app role cannot.

## Contract and implementation boundaries

The source plan is [system-build-plan.md](system-build-plan.md).
[PHASES.md](PHASES.md) tracks implemented versus future work.
[API-CONTRACT.md](API-CONTRACT.md) records the real endpoint paths and response behavior.

The original pasted v3 plan and received [integration-spec.md](integration-spec.md) are preserved.
The integration's older sizing/fractional/close-control paragraphs conflict with frozen v3;
[CONTRACT-RESOLUTIONS.md](CONTRACT-RESOLUTIONS.md) records v3's precedence and the implemented contract.
The exact frozen trigger supplied on 2026-09-17 is appended to the build plan.
Public HTTPS hosting and secure Muse connection setup are not provided by this local build.

The frozen trigger uses a 5-second quote tolerance and a configurable default 10 bps spread.
Minimum dollar volume and account-evidence age remain mandatory internal `Policy` inputs.
Values in tests are fixtures, not approved strategy settings. Sector/theme classifications and
market/account evidence must be supplied by server-owned providers, never by Muse.

Without risk configuration, `TRIGGER_CONFIRMED` remains the last automatically reachable success
state. With explicit Phase 4 policy and risk-role configuration, the worker can authorize and dispatch
confirmed candidates after current-session reconciliation and fresh broker evidence. The atomic
reservation and exact-request capability remain mandatory. Muse receives no execution endpoints.
The frozen authorization TTL is five seconds. Startup reconciliation saves broker previous-close
equity once per session. Controlled acceptance uses a TEST- engineering candidate excluded from
strategy reporting. [ENGINEERING-ACCEPTANCE.md](ENGINEERING-ACCEPTANCE.md) describes the operator
run path; [PHASE-4-VALIDATION.md](PHASE-4-VALIDATION.md) distinguishes local tests from actual
broker acceptance. The default service remains read-only unless a risk-role connection is configured.

## Primary technical references

- [Alpaca calendar](https://docs.alpaca.markets/us/v1.1/reference/getcalendar-1): official session times, including early closes.
- [Alpaca orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca): bracket construction requirements.
- [Alpaca trade updates](https://docs.alpaca.markets/us/docs/websocket-streaming): paper streaming protocol and execution events.
- [PostgreSQL triggers](https://www.postgresql.org/docs/current/sql-createtrigger.html): DB mutation enforcement.
- [PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html): transaction-scoped audit serialization.

Reading these references and passing local tests do not establish broker acceptance or strategy performance.

The new gated Jev build is tracked in [V4.2-BUILD.md](V4.2-BUILD.md) and [BUILD-LOOP.md](BUILD-LOOP.md). No Jev access, model pin or active integration is claimed before its gates pass.

The latest Step 4 is a separate storage/observation surface, tested locally before deployment:
[local report](STEP-4-REPORT.md), [research TRIAGE policy](TRIAGE-RESEARCH-POLICY.md),
[cohort attribution](COHORTS.md), and [deferred Railway setup](RAILWAY-REVIEW-DEPLOYMENT.md).
It cannot read or execute entry intents. No judgment touches admission or authorization.
For a credential-free, disposable engineering walkthrough, run
`./run python scripts/step4_local_proof.py --provider fixture`.

## Local three-market report test

The separate review API now accepts immutable Muse-style reports and exposes authenticated
Jev selection readback under the owner's approved local-only policy. All records in this
milestone are engineering tests; no trading admission or order authorization is connected.
See [research report evidence](RESEARCH-REPORT-SELECTION.md) for the 496-test checkpoint,
90-item fixture walkthrough, nine real Jev receipts and exact API/policy boundaries.
