# Step 4 — local Muse-role walkthrough and storage implementation

2026-09-19. **433 tests passed; lint passed. Railway and the real Muse connection are
deferred by the owner's local-first instruction. No judgment touches admission or
authorization.** The frozen strategy remains `CATALYST_RETEST_V1`.

## Real Jev follow-up

The owner clarified that the builder should create the private .env from the already
supplied key, without a desktop step. This is complete (0600, ignored/untracked).
A real response from **jev-1.13.0** took **458.43 ms**. Its raw receipt and 14-event
audit chain verify. Verdict APPROVE plus an Insufficient evidence answer produced
the overall **NEEDS_REVIEW** result; the intent stayed STORED_ONLY_NOT_AUTHORIZED.

The first real call exposed a decimal precision mismatch in judgment persistence.
The fix projects exact values directly from receipt JSON, keeping the immutable
database checks unchanged. The failed attempt is preserved separately; its raw
response was lost by the rolled-back transaction and is not claimed as a valid receipt.
Full details, real checkpoint and the test-isolation correction:
[STEP-4-REAL-JEV.md](STEP-4-REAL-JEV.md). Machine evidence: [step4-local-proof.json](step4-local-proof.json).

## What the earlier fixture walkthrough proved

The builder acted as the Muse-side caller against the real FastAPI handlers and a
fresh disposable PostgreSQL database. Market/calendar/account inputs and Jev's response
were explicitly synthetic engineering fixtures. This was an in-process HTTP/ASGI test,
not a public deployment or a real Jev/provider call.

| Step | Observed result |
| --- | --- |
| Seed an isolated TEST- candidate through the existing fixture path | VALIDATED, no broker order; existing runtime database untouched |
| Submit source excerpts as Muse | HTTP 201; immutable bundle/context hashes, revision and server deadline returned |
| Load exact stored state through the trusted adapter loader | Bound to candidate, bundle, context, policy, purpose and V1 |
| Evaluate via simulated Jev | Raw receipt verified; verdict REJECT, other questions Insufficient evidence; effective result NEEDS_REVIEW |
| Store an intent referencing that decision | HTTP 201, STORED_ONLY_NOT_AUTHORIZED; candidate remains VALIDATED |
| Repeat the intent | HTTP 409 |
| Link a different bundle | HTTP 409 |
| Read entry intents | HTTP 405; application DB role also denied SELECT |
| Attempt candidate admission through review service | HTTP 404 |
| Inspect isolated database | Zero orders, zero fills, zero risk decisions; no broker network requests |

The rejected review intentionally demonstrates that storing an intent is **not**
interpreting approval. There is no reader, consuming role, scheduler or arm logic.
The code does not invoke risk authorization, alter candidate state or submit an order.

Preserved machine-readable observations: [step4-local-fixture-proof.json](step4-local-fixture-proof.json).
The fixture uses `JEV_ENGINEERING_TEST`, not strategy results. Its latency is fixture
timing and must not be reported as measured TypeSafe latency. Historical provider
verification remains in JEV-ADAPTER.md. The subsequent real-call evidence is linked above.

## Validation and audit checkpoint

Latest `./run pytest -q --tb=short`: **433 passed**, 2 dependency deprecation warnings,
37.02 seconds. `./run ruff check src tests scripts`: **passed**. The 87 Step 4 tests,
343 earlier tests and three new precision regressions are green.

Coverage includes real database hash enforcement, rejected mutations/truncation,
exact source preservation, null/natural-language timestamp rejection, concurrent
revision inserts, unchanged-content replay, binding mismatch, expiry, credential
precedence/permissions, production no-file loading, explicit policy requirements,
auth scopes, role separation, and preservation of the existing audit chain through
provisioning. These are local tests, not full Gate 1 worker acceptance.

The initial full regression found the schema compatibility constant still expecting
version 8; it now expects migration 9. No trading logic changed. Schema 9 was installed
only in disposable test databases. The existing running database remains unchanged.

Local engineering proof checkpoint:

```text
valid: true
events: 14
head: bdce8d37cf13877cca7adc66a9425654a37a1391869046bb0cce1978fdc9f175
```

The exact JSONL export is retained with mode 0600 at
`/tmp/catalyst-step4-proof-wkhrgh6e/step4-events.jsonl`; that disposable database has
been stopped. This checkpoint describes the engineering proof only, not the primary
trading ledger, current broker exposure, or off-host backup readiness.

## File-by-file implementation

| File | Change |
| --- | --- |
| `src/catalyst_lab/migrations/009_review_storage.sql` | Immutable review_contexts, evidence_bundles and entry_intents; hash checks/audit triggers; fixed insertion functions; receipt binding; restricted storage role; cohort views |
| `src/catalyst_lab/config.py` | Schema compatibility version 9 only |
| `src/catalyst_lab/review_storage.py` | Strict evidence/intent contracts, exact excerpts, structured timestamps, trusted context loading and cohort reads; no intent reader |
| `src/catalyst_lab/review_service.py` | Separate authenticated storage/observation API, input-safe errors and fail-closed health/startup; no broker or worker imports |
| `src/catalyst_lab/review_config.py` | All Gate 1 inputs explicitly required; approved testing values checked; broker configuration rejected on review service |
| `src/catalyst_lab/jev_secrets.py` | Private local .env precedence, safe failure, unchanged no-prompt Keychain fallback; Railway process injection only |
| `src/catalyst_lab/jev_store.py` | Follow-up fix: derive judgment JSON/probability/confidence directly from the exact stored receipt, without float conversion |
| `src/catalyst_lab/review_deploy.py` | Separate owner-only migration/provisioner with restricted SCRAM role credentials; no web startup migration |
| `scripts/step4_local_proof.py` | Repeatable, explicitly selected fixture/TypeSafe engineering walkthrough in a disposable database |
| `tests/test_review_storage.py` | Database, API, binding, immutability, concurrency, cohort and exact evidence checks |
| `tests/test_review_config.py` | Required-policy, secret-loader, missing-key, environment and service-isolation tests |
| `tests/test_jev_review.py` | Follow-up regressions: exact Choice/Noul/Score decimal precision |
| `tests/conftest.py` | Isolate tests from the owner's .env and injected TypeSafe key |
| `railway.json` | Review-only start command, Dockerfile, health check and restart configuration |
| `Dockerfile.review` | Unprivileged review-service image; no environment-file copy |
| `.dockerignore` | Build-context allowlist excluding local secrets and artifacts |
| `.railwayignore` | Upload exclusions for Git metadata, environment files, exports and local artifacts |
| `.env.example` | Variable names only; no usable credentials or values |
| `deploy/review-policy.testing.json` | Explicit non-secret provisional policy input template; not a runtime default |
| `docs/PHASE-4.1-STEP4-TRIAGE.md` | Pre-implementation requirement classification for this bounded Step 4 |
| `docs/TRIAGE-RESEARCH-POLICY.md` | Bounded research-only selection/insufficient-evidence policy, text only |
| `docs/COHORTS.md` | Active, engineering, historical-unassigned and frozen-baseline attribution |
| `docs/RAILWAY-REVIEW-DEPLOYMENT.md` | Deferred deployment, role provisioning, variable setup and local run instructions |
| `docs/GATE-1-RELIABILITY-PROPOSAL.md` | Retains provisional agreed values; distinguishes config/storage tests from future runtime acceptance |
| `docs/GATE-2-MUSE-RUNTIME-CONTRACT.md` | Railway owns future worker; reviewer consumes durable outputs; explicit unproven assumptions |
| `docs/GATE-3-BACKGROUND-CREDENTIAL-ACCESS.md` | Latest .env/service-variable routes supersede the old required Keychain approval |
| `docs/API-CONTRACT.md` | Exact separate Step 4 routes, payload limits, scopes and non-authorizing behavior |
| `docs/CONTRACT-RESOLUTIONS.md` | Append latest owner scope, local-first and credential rulings |
| `docs/BUILD-LOOP.md` | Current local Step 4 checkpoint; no continuation into forbidden steps |
| `docs/V4.2-BUILD.md` | Current gate ledger and latest owner precedence |
| `docs/PHASES.md` | Distinguishes schema 9 disposable validation from existing schema 8 runtime |
| `AGENTS.md` | Carries forward current credential and Step 4 boundaries |
| `README.md` | Local proof command and links to current evidence |
| `docs/step4-local-proof.json` | Actual isolated walkthrough results and checkpoint |
| `docs/step4-local-fixture-proof.json` | Preserved original simulated-provider walkthrough |
| `docs/step4-local-provider-attempt-1.json` | Failed first real call, partial audit checkpoint and explicit missing-response record |
| `docs/STEP-4-REAL-JEV.md` | Real-call follow-up, observed judgments, precision fix and current checkpoint |
| `docs/STEP-4-REPORT.md` | This delivery report |

Pre-existing screenshots/artifacts and dirty work were preserved. No commit, deploy,
real paper order, runtime database migration or broad build-loop restart occurred.

## Phase 4.1 triage table

The original item-by-item corrective table and its historical 302-test checkpoint
remain in [PHASE-4.1-TRIAGE.md](PHASE-4.1-TRIAGE.md). All four previously identified
corrective gaps are implemented and their tests remain green. No frozen rule was
rewritten in this Step 4 work.

The new scope's complete starting-state table is
[PHASE-4.1-STEP4-TRIAGE.md](PHASE-4.1-STEP4-TRIAGE.md). Current disposition:

| Requirement | Starting classification | Current outcome |
| --- | --- | --- |
| Frozen V1 trigger/risk/sizing, pinned model/skill, receipt audit, narrow Jev role | ALREADY-IMPLEMENTED | Preserved; regression tests pass |
| Immutable evidence/source hashes and server context/revisions | REAL-GAP | Implemented and locally tested |
| Inert bundle/decision/V1 entry-intent storage | REAL-GAP | Implemented and locally tested; no reader |
| Research TRIAGE selection | SPEC-TEXT-ONLY | Bounded policy written; executable selection not built |
| Distinct cohort observations | REAL-GAP | Implemented with explicit filter and labels |
| Railway owns eventual loop | SPEC-TEXT-ONLY | Owner decision documented; no loop installed |
| Review service, role provisioning and Railway preparation | REAL-GAP | Files and local tests complete; deployment deferred |
| Explicit Gate 1 inputs, private .env and production injection | REAL-GAP | Locally tested; real .env credential works; Railway secret injection remains unproven |
| Review-only health/authentication | REAL-GAP | Locally tested; no public URL |
| Deployed credential/URL proof | REAL-GAP | Deferred by owner; not claimed |
| Hash-chain/append-only checkpoint | REAL-GAP | Verified on isolated engineering records |
| Entry/admission/authorization and Steps 5–7 | SPEC-TEXT-ONLY | Explicitly excluded; no implementation |
| Earlier mandatory Keychain desktop approval | SPEC-TEXT-ONLY | Superseded; no permissions changed |

## TRIAGE policy and cohort definition

[Full TRIAGE policy text](TRIAGE-RESEARCH-POLICY.md): up to 300 inputs and at most
20 research selections. Valid, complete support may enter the research shortlist;
contrary answers are retained as cuts; missing/uncertain evidence goes to
NEEDS_EVIDENCE, never implied approval. Proposed deterministic selection uses first
valid committed sequence, ticker and candidate ID. No confidence-as-win-probability
ranking. This text needs policy promotion before execution and has no trading veto.

`JEV_ACTIVE_V1` identifies bound active-policy research, not proof of an executed
active trade. `JEV_ENGINEERING_TEST` isolates all plumbing fixtures.
`JEV_REVIEW_UNASSIGNED` preserves historical unbound review attribution. The frozen
Jev-free trade baseline is unchanged; no active-trade attribution is manufactured.
See [COHORTS.md](COHORTS.md).

## Railway summary and next local check

Deployment URL: **none — not deployed, per the owner's local-first instruction**.
Railway prep starts only the review storage API, against Railway Postgres as
`catalyst_review`; the trusted adapter retains `catalyst_jev`. Owner migration
credentials are separate. No production .env is read or included in the image.

Later owner key step: Railway project/environment → review service → Variables →
New Variable → name **TYPESAFE_API_KEY** → enter the value privately → Add, optionally
Seal → apply staged changes when deploying. The exact setup and official references
are in [RAILWAY-REVIEW-DEPLOYMENT.md](RAILWAY-REVIEW-DEPLOYMENT.md).

The private local credential is now provisioned and the real-provider check has run.
The full follow-up is linked above. No desktop action is needed to continue supervised
local testing. Further provider traffic must remain within explicit test scope; the
credential does not enable continuous operation or lift the entry-wiring restrictions.

**No judgment touches admission or authorization.** No entry wiring, intent consumer,
scheduler, admission change, risk-authority change or Steps 5–7 was added. Deployment
and the actual Muse connection remain later work.
