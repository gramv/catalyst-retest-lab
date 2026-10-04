# Review cohort attribution

Step 4, 2026-09-19. All executable strategy references remain `CATALYST_RETEST_V1`.

| Cohort | Meaning | Reporting boundary |
| --- | --- | --- |
| `JEV_ACTIVE_V1` | Non-engineering research packets bound to `MUSE_JEV_ACTIVE_V1` | Review observations only in Step 4; not proof of an active execution integration |
| `JEV_ENGINEERING_TEST` | Server-derived ENGINEERING_TEST candidates and synthetic plumbing reviews | Excluded from strategy results and public dashboard |
| `JEV_REVIEW_UNASSIGNED` | Historical non-engineering reviews without a Step 4 bundle binding | Kept separate; never retroactively assigned to the active cohort |
| Frozen Jev-free baseline | Existing broker-derived V1 strategy results without an active-policy execution grant | Existing strategy reporting views remain unchanged |
| `JEV_US_SELECTED_FIXED_ENGINEERING` | Explicit US engineering enrollment under `JEV_US_SELECTED_FIXED_TEST_V1` | Fixed mechanical exits; excluded from strategy statistics; private status bound to reviewed item |

Schema 12's bridge is a separate explicit test configuration requiring fresh receipt,
worker health, validation, actual trigger and risk authorization. Old inert Step 4
intents remain inert. The Step 4 descriptions below record that earlier implementation
boundary.

The server derives purpose from the immutable candidate; callers cannot choose a
cohort. Contexts, evidence bundles and inert intents each store purpose, cohort and
strategy version in their audited rows. Bound Jev requests carry those same identifiers.
Receipts and individual `ai_decisions` inherit the binding through request/receipt IDs;
the `review_observations` and `jev_cohort_decisions` views make it explicit without
altering historical rows or raw provider responses.

`GET /api/v1/review-observations` requires an explicit cohort filter, returns the
cohort on every item and states `strategy_results: false`. The local proving report
uses `JEV_ENGINEERING_TEST` throughout. No mixed aggregate of baseline, active reviews
and engineering observations is supplied.

There are no Jev-authorized trades in Step 4: an intent is only an immutable stored
request. Future execution attribution must be attached at the authoritative execution
boundary and tested before active results can enter any trade report. Research review
presence alone must never relabel an old or independently executed trade.

## Local report milestone

The owner-approved `JEV_SKEPTIC_RESEARCH_TEST_V1` selection uses `JEV_ENGINEERING_TEST`
for every report, item, request and outcome. The server requires TEST-prefixed report
keys and stamps ENGINEERING_TEST; clients cannot choose a performance cohort. US_STOCKS,
CRYPTO and INDIA remain separate market labels. These local research outcomes never
enter trade statistics, the public dashboard or the Jev-free baseline. No currency P&L
is inferred from proposed levels.


## Attribution for the owner-approved architecture

Research worker and private screen rows remain `JEV_ENGINEERING_TEST`, with no performance
aggregation. The intended execution comparisons are three separate groups: frozen Jev-free
V1 with fixed exits; Jev-selected V1 with fixed exits; Jev-selected V1 with a separately
versioned managed-exit policy. The last two execution groups are not enabled by research
selection. Their attribution must be assigned atomically at execution authorization, never
inferred later from the presence of a review. US/crypto and manual India remain distinct;
no cross-currency or engineering/strategy combined headline is produced.
