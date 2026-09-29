# Phase 4.1 triage — Railway Step 4 scope

Assessed before implementation, 2026-09-19. Classification describes the starting
checkout; completion evidence is recorded separately in STEP-4-REPORT.md.
The earlier Phase 4.1 corrective table remains in PHASE-4.1-TRIAGE.md.

| Claim / requirement | Classification | Evidence / permitted gap |
| --- | --- | --- |
| Frozen V1 trigger, sizing, risk gates | ALREADY-IMPLEMENTED | Existing trigger/risk code and earlier corrective tests; no modifications authorized. |
| Pinned Jev and official skill | ALREADY-IMPLEMENTED | JEV-ADAPTER.md and jev-model-pin.json; preserve pins. |
| Exact receipts, typed ai_decisions and hash-chain enforcement | ALREADY-IMPLEMENTED | Migration 008, jev_store.py, test_jev_review.py. |
| Narrow catalyst_jev role | ALREADY-IMPLEMENTED | Migration 008 and startup privilege check; retain restrictions on Railway. |
| Immutable hash-addressed evidence/source excerpts | REAL-GAP | Add strict market-only records, source hashes and timestamp validation. |
| New evidence per revision, server-loaded context in hash | REAL-GAP | Add immutable context bindings; no caller override of deadline, purpose or policy. |
| Entry intent bound to bundle, decision and V1 | REAL-GAP | Store-only table and POST path; no consumer, arm state or authorization. |
| Research-only TRIAGE selection policy | SPEC-TEXT-ONLY | Write bounded selection/insufficient-evidence policy; do not wire selection to trading. |
| Distinct Jev cohort in review records/reports | REAL-GAP | Persist cohort identity and expose explicitly grouped review reporting; existing baseline remains untouched. |
| Railway as supervisor, app owns eventual loop | SPEC-TEXT-ONLY | Owner selected Railway; worker/queue/event wiring remain Steps 5–7 and forbidden now. |
| Railway review-surface config and Postgres role provisioning | REAL-GAP | Add isolated service entrypoint, deployment config and owner-only database provisioning. |
| Review startup validates every Gate 1 input | REAL-GAP | Explicit configuration schema; missing/invalid values prevent startup. No implementation of unapproved live behavior. |
| Local private .env with Keychain fallback | REAL-GAP | Add restricted local loading; production never reads .env; example contains names only. |
| Authenticated review observation surface and health | REAL-GAP | Separate API from candidate/risk application; health is not trading readiness. |
| Deployed URL and credential injection proof | REAL-GAP | Deferred by the subsequent local-first owner instruction; do not claim from local tests. |
| Database append-only/hash checkpoint after migration | REAL-GAP | Test new tables and roles; verify exported chain before/after local migration. |
| Admission/authorization/entry consumers, Steps 5–7 | SPEC-TEXT-ONLY | Explicitly excluded; no implementation or automated continuation into them. |
| Keychain desktop approval gate | SPEC-TEXT-ONLY | Superseded for Railway; existing local no-prompt behavior retained as fallback. |

Only the real gaps inside Step 4 are implementation scope. No new strategy version,
research rework cap, execution authority or model-driven exit is introduced.
