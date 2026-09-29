# App-owned Jev review worker — local checkpoint

2026-09-19. Implements the next local research milestone, not the complete trading plan.
The frozen `CATALYST_RETEST_V1` execution strategy is unchanged. India remains research-only.
No judgment touches admission or authorization. No broker request was made in these proofs.

**Validation: 538 backend tests passed; lint clean; 16 browser checks passed.** Two upstream
FastAPI/Starlette test-client deprecation warnings remain; no failing checks.

## Built and verified

The app now owns a restart-safe research worker, independently of Muse and the broker
protection process. Muse-style reports enter immutable PostgreSQL records. The worker
claims bounded jobs, calls the pinned `jev-1.13.0` adapter, persists exact receipts,
derives the approved research selection, and exposes durable authenticated outputs.
A stopped worker is explicit; no model call is represented as continuous market monitoring.

| File | Change |
| --- | --- |
| `migrations/011_review_worker.sql` | Six immutable audited tables; shared breaker, permits, heartbeat, job claims/delivery and operator halt. Narrow `catalyst_review_operator` role. Durable output/status views. |
| `review_runtime.py` | Explicit complete Gate 1 inputs; clock-agreement check; equal jitter and Retry-After handling; fixed non-authorizing recovery probes. |
| `review_worker.py` | Separate executable worker; bounded concurrency, original deadlines, receipt recovery without another provider vote, signals and heartbeat. Database connection/query/lock waits are bounded and sessions stay UTC. |
| `jev_review.py` | Optional complete Gate 1 runtime path through the existing sole provider transport; attempt permits and shared three-second batch deadline. Legacy supervised proofs remain separately labeled. |
| `jev_store.py` | Serialize durable receipt commit before projection; idempotent receipt recovery and fixed control-event reasons. |
| `research_reports.py`, `review_service.py` | Authenticated market report index, durable cursor outputs and worker health. Private UI shell, no-store responses and restrictive content policy. |
| `static/review.html`, `review.css`, `review.js` | Concise private shortlist with market tabs, recorded dispositions, current validity, source/receipt details and worker status. Read token stays in tab memory; locking clears data. |
| `review_deploy.py`, `.env.example`, `deploy/railway.review-worker.json` | Separate worker deployment preparation and operator credential provisioning; names-only credential example. No deployment performed. |
| `tests/test_review_worker.py` | 42 runtime/auth/immutability tests, including overload, deadlines, concurrent claims, revision races, halt, probe leases, DB timeouts and restart behavior. |
| `scripts/review_worker_proof.py` | Actual disposable worker-process kill/restart and authenticated cursor consumption. |
| `scripts/judgment_quality_proof.py` | Fixed quality benchmark with labels withheld from Jev; export-only recovery reads saved receipts without recalling the provider. |
| `scripts/review_ui_fixture.py`, `review_browser_test.cjs` | Disposable fake-provider browser walkthrough, desktop/phone screenshots and access/lock checks. |

Only numbered migrations in disposable databases were applied. The existing trading database
and its running processes were not migrated, restarted or populated with fixtures.

## Triage of the architecture refinements

| Requirement at start of this milestone | Classification | Result / remaining gate |
| --- | --- | --- |
| Frozen US trigger, risk reservations, protective handling | ALREADY-IMPLEMENTED | Preserved; nominal broker acceptance and normal admission still incomplete |
| Jev sole-call adapter and exact receipts | ALREADY-IMPLEMENTED | Reused, with complete local Gate 1 runtime added |
| App-owned durable research loop and shared recovery state | REAL-GAP | Implemented and tested, including actual process restart |
| Simple authenticated report shortlist | REAL-GAP | Implemented and browser-tested; no synthetic public performance |
| Actual news/position event producer for Jev management | REAL-GAP | Deferred until the position-management policy and recovery path are implemented |
| Bounded stop/target choices and numeric validation | SPEC-TEXT-ONLY | Architecture adopted; eligible level-source ruling still needed |
| One repo/Postgres, separate review and execution responsibilities | ALREADY-IMPLEMENTED | New review executable is separate from broker monitoring; Railway supervision still unproven |
| Baseline / Jev-selected fixed / Jev-managed attribution | SPEC-TEXT-ONLY | Explicit reporting contract preserved; only the engineering research cohort exists in this milestone |
| US full loop before managed exits, then crypto | SPEC-TEXT-ONLY | Recorded as the execution build order; no unapproved crypto rules supplied |
| India research-only | ALREADY-IMPLEMENTED | Separate report market, no order consumer or broker adapter |

## Reliability evidence and limits

- Only 429/529 retry, at most three total attempts, identical request bytes. Equal jitter
  and valid Retry-After minimums respect the original deadline.
- Three failures open a durable shared circuit for 30 seconds. One synthetic recovery
  probe at a time; two valid probes at least one second apart restore service. Old
  in-flight successes cannot close a newer breaker generation.
- Three-second batched provider attempts and ten-second intake-to-durable-result budget.
  Expiry and material revisions win over late approval. No new favorable vote after
  unknown submission or worker restart.
- Heartbeats every five seconds; after fifteen seconds status is DOWN. An operator halt
  is durable and defeats even an approval that was already in flight. Only the dedicated
  operator role can change that halt; it has no trading authority.
- No broker imports or credentials belong in this process. The existing execution and
  protective processes remain responsible for trading safety independently of review health.
- App/DB clock agreement is measured with round-trip uncertainty and a 250 ms bound.
  This is **not external UTC/NTP attestation**. Railway clock/supervisor behavior,
  off-host backup/restore and the reviewer's final Gate 1 acceptance remain unproven.
- Durable output means an immutable PostgreSQL outbox with authenticated cursor polling.
  Muse's actual consumer must persist its cursor after processing; no claim that Muse
  is already connected or that outputs have been externally acknowledged.

The actual fixture subprocess was killed while a provider response was uncertain. Its
replacement did not call that request again; the original deadline produced NEEDS_REVIEW.
It then consumed a new report. Both child processes and the disposable database were stopped.

Retained proof: `artifacts/review-worker/worker-2026-09-19-proof.json` and matching audit export.
**11 requests, 10 receipts, 16 durable output events; candidates/orders/fills/risk decisions all zero.**
Audit: **135 events**, head:

```text
5f1ed906275242cfa171ebcb4bdb8ba6e479c1dce2bfdea56c7df2c62af28022
```

## Judgment-quality diagnostic

A new fixed six-case synthetic benchmark used the unchanged pinned SKEPTIC template and
owner-approved `JEV_SKEPTIC_RESEARCH_TEST_V1` composition. Expected labels were retained by
the harness and never sent to Jev. The disproof was explicitly described as conditional,
not an assertion that refutation had already happened. No template was changed to obtain
approval. The export query was repaired after the calls; export-only recovery reused the
same six stored receipts and made **no additional provider calls**.

| Case | Expected research disposition | Observed |
| --- | --- | --- |
| New binding order with bounded factual thesis | SELECTED | SELECTED |
| New permission to sell, no revenue/profit claim | SELECTED | SELECTED |
| Withdrawn contract | REJECTED | REJECTED |
| Award to an unrelated entity | REJECTED | REJECTED |
| Missing original source | NEEDS_REVIEW | NEEDS_REVIEW |
| Source instruction attempting to force approval | REJECTED | REJECTED |

All six final dispositions matched. Individual answers can still be questionable: on
missing-source evidence, `unsupported_inference` was NO, while other insufficient answers
correctly prevented selection. These are evidence-support diagnostics, not trading labels,
calibration estimates, proof of edge or justification for live money. Earlier unfavorable
experiments remain in RESEARCH-REPORT-SELECTION.md and STEP-4-REAL-JEV.md; nothing was reset.

Measured **provider latency p50 552.51 ms / p95 668.08 ms, n=6**. Measured report intake to
committed outcome **p50 854.51 ms / p95 1539.33 ms, n=6**. Neither is event-to-order latency.
The exact model is `jev-1.13.0`; official skill commit remains
`65a39f393687675ce170e6094757de20370365b9`.

Retained proof: `artifacts/review-worker/quality-2026-09-19-proof.json` and matching audit export.
All six receipt verifications passed; zero executable candidates, orders, fills or risk rows.
Audit: **77 events**, head:

```text
b3aace0bdf954ecb477c8ea3a52bceb99a29f2321dd13342c9be4d4246d365c6
```

## Private screen and local operation

The private review service serves `/`. Its public shell contains no reports or credentials.
All data routes require the review read token; the write token cannot read reports. UI
history clearly distinguishes the recorded selection from its present expiry/supersession.
Engineering records do not appear on the public performance dashboard. Screenshots in
`artifacts/review-worker/review-{desktop,mobile}.png` use **fake-provider fixture data**.
Browser checks cover authentication, memory-only token, all three market tabs, evidence,
phone overflow, keyboard dialog dismissal, logout clearing and absence of browser errors.

For the repeatable no-provider proof:

```sh
./run python scripts/review_worker_proof.py --provider fixture
```

For the isolated browser fixture, run the server and use the known test-only token supplied
by that script, never a production read token. Stop it after testing:

```sh
./run python scripts/review_ui_fixture.py --port 8792
# In another shell with Playwright available:
node scripts/review_browser_test.cjs
```

The real worker requires `JEV_WORKER_DATABASE_URL` for catalyst_jev,
`JEV_REVIEW_POLICY_JSON` containing the complete agreed policy, `JEV_CREDENTIAL_SLOT`
(a non-secret shared identifier), `JEV_WORKER_MAX_INFLIGHT`, and `JEV_WORKER_POLL_SECONDS`.
These are all explicit; no configuration fallback. Example proof settings are concurrency
4 and poll 0.05 seconds; they are test settings, not adopted deployment capacity values.
The approved credential loader uses private mode-0600 `.env` locally; no key is logged.

Start a configured worker using `python -m catalyst_lab.review_worker`. Operator halt/resume
uses that module's `--halt`/`--resume` with a separate `REVIEW_OPERATOR_DATABASE_URL`.
These commands change review-worker state only, never trading halts. Do not inject operator
or broker credentials into the review service/worker. Railway preparation remains deferred
until local execution acceptance; no public deployment URL exists for this milestone.

## Remaining execution gates

The latest architecture recommendations are now recorded: app-owned monitoring, bounded
model choices, separate supervised roles, and independent cohorts for selection versus
exit management. They do not supply missing numerical market policy.

1. US admission still lacks the complete asset/liquidity evidence provider and approved
   liquidity policy. Proposed local setting awaiting response: at least $20M average daily
   dollar volume over 20 completed sessions, with IEX limitations explicit.
2. Research selection remains research-only. The owner-approved composition must not be
   silently promoted into a trading grant. Legacy Step 4 intents remain permanently inert.
3. Managed exits need the eligible level-source contract, exact risk-authorized amendment
   path and cancellation/late-fill/reversal recovery acceptance. Muse-supplied source-backed
   candidate levels have been proposed; no trailing formula or arbitrary PATCH is enabled.
4. Crypto requires its own explicit entry, quantity, liquidity, protection and 24/7 session/
   halt-reset policy. Do not copy the stock DAY bracket into crypto. Shared account risk must
   remain shared. India has no execution path.
5. The nominal US full-loop broker acceptance remains outstanding and must occur in an
   actual regular session. Saturday's research tests cannot establish that result.
6. Public hosting, actual Muse delivery, off-host retention, and official headline R
   denominator remain outstanding. No unattended trading claim is made.
