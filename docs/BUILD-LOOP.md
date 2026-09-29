# Build, test, verify, advance

Latest checkpoint: [managed stock/crypto wiring](MANAGED-WIRING-2026-09-19.md).
The owner-authorized local engineering workflow now connects research, evidence rework,
selection Jev, live-print monitoring, shared risk, both paper adapters, contextual management
Jev and independent measurement. Code/fixture verification and real Jev contract checks are
recorded there. The actual broker acceptance/deployment boundary remains explicit. Earlier
Step4-only checkpoints below are historical and do not disable the separate opt-in profile.
Do not run an unattended trading loop or deploy it based on fixture proof alone.


Updated 2026-09-19. Workspace: `~/Projects/catalyst-retest-lab`.

**Latest owner scope, 2026-09-19: US stocks and crypto paper execution; India research-only.**
Muse remains external; Jev selection/monitoring belongs to the app. The owner expanded
beyond the earlier Step 4-only build scope, then requested a plan to reach end-to-end
testing. [TEST-READINESS-PLAN.md](TEST-READINESS-PLAN.md) is the staged implementation
and acceptance order. This checkpoint does not start an automation or enable trading.
Local testing still precedes Railway deployment and the real Muse connection. Existing
risk guards remain mandatory; Gate 1 values stay provisional until their agreed tests.

1. Read PHASES.md, V4.2-BUILD.md, this checkpoint and the latest user rulings.
2. Select the earliest unfinished gate with all required inputs available.
3. Read existing code/evidence, classify what is already present, and implement only a real gap.
4. Run targeted tests; then the required full suite and lint for meaningful backend changes.
5. Verify observable behavior, preserve append-only evidence, and record what was proven.
6. Advance only after the gate passes. At a missing contract/credential/ruling, stop that dependent
   work and report the exact missing input. Never bypass, fabricate, or repeatedly ask an unchanged question.
7. Continue independent, already-authorized work while waiting. Do not place another paper trade as
   an implementation convenience; provider acceptance must stay within its exact authorized scope.

## Current checkpoint

**Current implementation:** [US-JEV-INTEGRATION.md](US-JEV-INTEGRATION.md). The owner
delegated test-policy choices. US admission evidence and a separate receipt-bound US
paper-test bridge are implemented and fixture-tested (schema 12). Liquidity/management
level-source choices are no longer owner-input blockers. Actual Alpaca acceptance,
managed amendments and crypto execution remain distinct gates. Earlier entries below
are historical and do not override this update.

- Phase 5–6 local measurement/dashboard implemented, schema 7 migrated after audit/DB backup,
  browser checks passed. See PHASE-5-6.md and phase56-runtime-evidence.json.
- Official R denominator and public HTTPS host are still awaiting owner input. Do not choose silently.
- Final owner Part A is authoritative: CATALYST_RETEST_V1 only, printed touch, reward/risk at M,
  exact-version evaluation fallback permitted if the authenticated model list contains only aliases.
- Step 0: corrective checklist classified; four real gaps fixed. Full suite **302 passed** on
  2026-09-19; lint clean. New normal/early-close, reward/risk and authorization-expiry tests included.
  See PHASE-4.1-TRIAGE.md and phase41-runtime-evidence.json. Runtime restarted clean, no new orders.
- Steps 1–3: **COMPLETE**. Authenticated discovery and explicit-version evaluation pinned
  jev-1.13.0. Official skill pinned at 65a39f393687675ce170e6094757de20370365b9. Schema 8 stores
  exact request/response receipts and typed judgments through a restricted review role. Full suite
  **343 passed**; three real synthetic engineering reviews verified; p50 478.27 / p95 778.14 ms (n=3).
  See JEV-ADAPTER.md, jev-model-pin.json and jev-adapter-provider-evidence.json.
- Step 4 storage, explicit Gate 1 configuration, cohort observations and Railway preparation are
  implemented. The local Muse-role fixture walkthrough uses a disposable database and no broker
  calls. See STEP-4-REPORT.md and step4-local-proof.json for current validation and chain checkpoint.
  The owner-authorized private local .env now works. A real Jev engineering review completed;
  receipt/chain verified, final status NEEDS_REVIEW, no broker requests. Decimal precision
  projection and test credential isolation were fixed; full suite now **433 passed**.
  See STEP-4-REAL-JEV.md, including the preserved failed first attempt.
  No deployment, persistent review worker or Steps 5–7 implementation is claimed.
- Key stored in Keychain (service catalyst-retest-lab.typesafe, account jev-review), but background
  reads hit desktop access control. Background lookups now fail closed without prompting. Successful
  probes used the owner-supplied key in a transient child environment. Do not claim unattended secrets.
- Official TypeSafe research: JEV-RESEARCH-2026-09-19.md. Do not repeat vendor aliases in code/config/docs.
- Prior FAILED_CLOSED engineering run is unchanged; no nominal acceptance pass or repeat run is claimed.

## Continuation loop

Codex heartbeat `catalyst-gated-build-loop` remains PAUSED. The new plan is an
implementation sequence, not a resumed recurring job. Do not run the obsolete broad
heartbeat prompt. A build heartbeat is not an application worker or an unattended
trading service. No schedule was started by the owner's planning request.

## Latest completed milestone — research selection

Stage 1 local engineering report/selection/readback is implemented and tested using the
owner-approved `JEV_SKEPTIC_RESEARCH_TEST_V1`. 496 tests pass. The 90-item fixture
walkthrough and nine real TypeSafe calls have verified receipts and audit exports; zero
candidates/orders/fills/risk decisions in both proofs. See RESEARCH-REPORT-SELECTION.md.
Historical next gate at that checkpoint was Stage 2: durable worker and full provisional Gate 1
reliability. The supervised report runner is not that worker. No background automation
was enabled, and the live runtime ledger remains at its earlier schema checkpoint.


## Latest checkpoint — durable worker and private shortlist

Stage 2 is implemented and locally tested under the provisional inputs. **538 tests,
16 browser checks, clean lint**. Real subprocess kill/restart proof, six-case real-Jev
quality diagnostic and hash exports are recorded in REVIEW-WORKER-LOCAL.md. The separate
private shortlist does not expose engineering fixtures on the public dashboard.
External clock/supervisor proof and final reviewer acceptance remain outstanding.

Next dependent gate is US admission policy/evidence and conditional execution integration.
Local liquidity coverage and the source of eligible management levels await explicit
policy selection; crypto rules remain unspecified. Independent review/UI work is done.
No trading run, Railway deployment, Muse connection or recurring Codex job was started.
The existing Codex heartbeat remains paused; do not execute its obsolete broad prompt.
