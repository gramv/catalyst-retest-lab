# Three-market research selection — local test milestone

2026-09-19. Owner approved the exact composition below **for local testing only**.
This milestone implements Muse-style report intake → app-hosted Jev review → authenticated
final-list readback. It does not enable trading, a persistent worker, deployment, or the
real Muse connection. India remains research-only. Fixed-exit CATALYST_RETEST_V1 is unchanged.

## Approved policy

Identifier: `JEV_SKEPTIC_RESEARCH_TEST_V1`.

- SELECTED only when verdict=APPROVE, news_stale=NO, unsupported_inference=NO,
  already_priced=LOW or MEDIUM, with every answer present, valid, uniquely chosen and current.
- An explicit verdict REJECT is REJECTED, even when another answer is insufficient.
- Otherwise insufficient/missing evidence, conflicting judgments, tied choices, provider
  unavailability, invalid response or receipt-integrity failure produce NEEDS_REVIEW.
- No confidence cutoff, probability-as-win-forecast, forced selection count, or ranking by
  confidence. All submitted items remain visible. This is the finished-report SKEPTIC
  policy, not approval of the earlier 300-to-20 raw-mover TRIAGE proposal.

Every report and result in this milestone is `ENGINEERING_TEST` / `JEV_ENGINEERING_TEST`.
The API accepts TEST-prefixed reports only. The baseline and active-trade cohorts do not
acquire these results. No research approval constitutes a candidate admission or risk grant.

## Interface and persistence

`POST /api/v1/research-reports` requires the review write token. A report binds an immutable
report ID, sequential revision, unique submission ID, market, timeframe, structured generation
and expiry timestamps, and 1–50 items. Thirty-item batches are tested for US_STOCKS, CRYPTO
and INDIA. Each item carries a signal, symbol, LONG direction, thesis/disproof/economic
relationship, proposed levels and short original-source excerpts with hashes/provenance.
No client quantities, approval flags or arbitrary order fields are accepted.

`GET /api/v1/research-reports/{report_id}?revision=N` requires the review read token.
Omitting revision returns the latest. It exposes each original item, exact typed judgments,
request/receipt IDs, immutable recorded disposition, effective current status and counts.
`authorizes_entry` and `execution_enabled` are always false. Current reports are not public.
The supervised runner invokes the existing `jev_review` adapter with an explicitly bounded
concurrency input. The HTTP service does not run the model or a background worker.

Migration 010 adds immutable research_reports, research_report_items, research_outcomes,
research_controls and research_question_sets. Every inserted row has a hash-chain event;
application roles lack direct INSERT/UPDATE/DELETE/TRUNCATE privileges. Restricted security-
definer functions insert reports and derive outcomes. The narrow Jev role remains unable
to create candidates, orders, fills or risk decisions. All migration tests used disposable
PostgreSQL; the existing running database was not migrated.

The database binds reviews to the exact item/evidence hash, report revision, market, purpose,
cohort, deadline, policy and pinned SKEPTIC question hash. Stale revisions cannot win a race.
Duplicate deliveries cannot obtain another model vote. Renaming source/signal IDs, reordering
sources, refreshing retrieval time or changing decimal spelling does not create new material.
Material revisions can receive a new review without extending original report expiry.
Semantic paraphrase detection is not claimed; Muse must preserve faithful original evidence.

All timing inputs are explicitly supplied through Gate1Inputs. Report intake sets the
10-second review deadline and 60-second evidence/context maximum age; queue time consumes
that original window. Source publication time remains provenance, not a question to Jev.
Expired and superseded outcomes stay historical only. Late/unknown work never becomes consent.
The full shared breaker, health probes, clock-health policy and durable worker remain stage 2.

## Receipt recovery and integrity

The exact raw provider receipt now commits before deriving `ai_decisions`. A projection
failure returns NEEDS_REVIEW and retains the response. Local idempotent projection recovery
reads only the stored bytes and performs no provider call. Once an unavailable research
outcome is recorded, projection recovery does not silently promote it to SELECTED.

Selection and readback verify receipt/request/judgment audit envelopes. Tampering suppresses
the effective selection and leaves the original recorded outcome intact. Full exported-chain
verification and independently retained checkpoints are separate controls; this is not a
claim that a database superuser cannot rewrite the entire database and all backups.

## Validation evidence

- Full suite: **496 passed**, two dependency deprecation warnings; lint passed.
- 63 report-specific tests cover 30-item batches in all three markets, composition, low
  confidence, insufficient evidence, 401/422/429/529, revisions, concurrent duplicates,
  response expiry/supersession, unknown in-flight restart, projection failure/recovery,
  integrity tampering, restricted roles, source/schema rejection and atomic batch rollback.
- Independent authenticated ASGI walkthrough: **90 fixture items**, 30 per market; each
  market returned 10 SELECTED, 10 REJECTED, 10 NEEDS_REVIEW. All 90 receipts verified.
- Real provider walkthrough: **nine synthetic engineering items**, three per market,
  against **jev-1.13.0**. Jev returned **REJECT for all nine**; the app retained all nine
  rejections. Nothing was rerun to seek approval. p50 **522.44 ms**, p95 **636.92 ms**, n=9.
  This measures HTTP evaluation latency, not end-to-end trading latency or judgment accuracy.
- Both walkthroughs: zero executable candidates, orders, fills and risk decisions. No
  broker client was constructed. Clusters stopped after export. No runtime ledger changes.

Real provider inputs were explicitly fictional supported/contradicted/incomplete cases,
not market research or actionable picks. Rejection of a fictional supported case is not
interpreted as evidence of trading edge or model calibration. Fixture-selected cases prove
code paths; they are not claimed as real-provider approvals.

| Checkpoint | Events | SHA-256 head |
| --- | ---: | --- |
| 90-item fixture | 727 | `1d486591b17892c20212c7b8e53980b0f1ac310b73b293b14d47a04afb9267ba` |
| Nine real Jev calls | 79 | `85be734c8664aa2da540c3f62d9222af21f7107a532d5835a4e010c273e5aa36` |

Artifacts: [fixture proof](../artifacts/research-reports/fixture-2026-09-19-proof.json),
[real-provider proof](../artifacts/research-reports/typesafe-2026-09-19-proof.json),
[fixture audit](../artifacts/research-reports/fixture-2026-09-19-audit.json),
[real-provider audit](../artifacts/research-reports/typesafe-2026-09-19-audit.json).
Local artifact files have mode 0600 and contain no request authentication headers.

Repeat the bounded supervised proof:

```sh
./run python scripts/research_report_proof.py --provider fixture --items-per-market 30
./run python scripts/research_report_proof.py --provider typesafe --items-per-market 3
```

The real-provider mode loads the existing private local credential, never a CLI key.

## File-by-file changes

| File | Delivery |
| --- | --- |
| migrations/010_research_reports.sql | Immutable reports/items/outcomes/controls/templates, exact binding, selection functions, integrity-aware readback, grants |
| research_reports.py | Strict report schema, content normalization, authenticated-store helpers and supervised bounded review runner |
| review_service.py | Authenticated report POST/GET, bounded request size and honest health surface |
| config.py | Schema compatibility 10; no running-ledger migration |
| jev_store.py | Material duplicate rejection; raw receipt before projection; idempotent recovery and receipt verification |
| jev_review.py | Retained receipt and safe NEEDS_REVIEW on projection failure |
| tests/test_research_reports.py | 63 database/API/provider-fixture and recovery cases |
| tests/test_review_storage.py | Schema provisioning expectation updated |
| scripts/research_report_proof.py | Repeatable isolated authenticated walkthrough and audited exports |
| docs/API-CONTRACT.md | Exact new routes and storage-only execution boundary |
| docs/CONTRACT-RESOLUTIONS.md, PHASES.md, BUILD-LOOP.md, TEST-READINESS-PLAN.md, COHORTS.md | Approval, evidence, current scope and next gates |

Next: stage 2's persistent review worker and complete provisional Gate 1 reliability tests.
Production-quality US liquidity evidence, managed level selection, crypto policy and official
R remain separate decisions before their dependent execution work. No judgment in this
milestone touches candidate admission, risk authorization, a stop, target or broker order.
