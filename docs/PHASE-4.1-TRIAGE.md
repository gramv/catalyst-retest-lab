# Phase 4.1 prerequisite triage — final Part A

2026-09-19. Uses the corrective checklist supplied by the owner and the final Part A, which supersedes
conflicting architectural drafts. There is one executable strategy: CATALYST_RETEST_V1. A regular-session
printed touch completes the retest; no breakout/reclaim or new strategy version has been invented.

| Item | Classification at review | Resolution / evidence |
| --- | --- | --- |
| Printed-touch retest with live quote/feed/session checks | ALREADY-IMPLEMENTED | `trigger.py`, `test_exact_touch_confirms_with_limit_at_max`; exact historical touch retained |
| Sizing at M, whole shares and no leverage | ALREADY-IMPLEMENTED | `risk.py`/`risk_math.py`; live broker equity, M-minus-S sizing and no-leverage tests |
| Minimum 2:1 reward/risk at executable entry M | REAL-GAP | Earlier code used T. Latest owner ruling applied to new validation and rechecked by risk; exact 2:1 boundary tests. Old decisions untouched |
| Basis-point conversion | ALREADY-IMPLEMENTED | Spread ratio × 10,000; boundary test in `test_trigger.py` |
| Hard spread ceiling of 10 bps | REAL-GAP | Builder settings previously could relax the limit. Trigger/risk policy now rejects a setting above 10; stricter settings still supported |
| Atomic open/working-order reservations and correlation | ALREADY-IMPLEMENTED | Transaction locks, 1%/2% budgets; concurrent reservation and rollback tests |
| Idempotent submission/unknown outcome recovery | ALREADY-IMPLEMENTED | One-use exact-request decisions, stable client ID, broker lookup before recovery; timeout/restart tests |
| Partial fill and cancellation races | ALREADY-IMPLEMENTED | Late-fill/cancel-race/held-or-missing-protection recovery tests; real partial-fill safety flatten verified. Nominal controlled outcome remains FAILED_CLOSED |
| Calendar-aware protective flatten | ALREADY-IMPLEMENTED | Normal/early-close fixture tests; calendar close minus five minutes |
| No new admission, confirmation or dispatch at flatten deadline | REAL-GAP | Checked in validation, watcher, atomic risk check and final transport; normal/early-close exact-boundary tests |
| Candidate expiry rechecked after authorization | REAL-GAP | Entry capability carries min(candidate expiry, flatten deadline); consumption rechecks it even within the 5-second TTL |
| Independent measurements | ALREADY-IMPLEMENTED | Phase 5 projection/snapshot/rollup and disclosure tests; running schema 7. Official R definition remains an owner reporting ruling |
| Earlier draft's separate corrected strategy | SPEC-TEXT-ONLY | Withdrawn by final Part A: no new strategy/version implementation |
| 24-hour exit language | SPEC-TEXT-ONLY | No additional US overnight policy: intraday official-calendar flatten remains binding; no new 24-hour holding rule implemented |

Only real gaps were implemented. No risk-budget, stop/target, quantity, model-driven exit or broker-endpoint
policy was loosened. The normal-session 15:55 wording is applied through the existing official-calendar
rule, retaining the explicitly required early-close behavior.

Validation commands: `./run pytest -q`, `./run ruff check src tests`. See BUILD-LOOP.md for the final test
count and current gate. A synthetic fixture's target was moved to 102.45 for T=100/M=100.15/S=99 so the
fixture satisfies exactly 2:1 at M; this edits disposable tests only, never historical candidates.

Final verification: **302 tests passed**, lint clean; runtime evidence in [phase41-runtime-evidence.json](phase41-runtime-evidence.json). Audit event 3309 records the owner ruling. Broker and local exposure remain zero. No broker order was submitted during this work.
