# Muse to first Jev: 30-crypto batch and criteria audit

2026-09-20. Actual public market data and actual TypeSafe calls. Research-only, paper architecture; no execution connection.

## Outcome

42 crypto pairs screened; 34 had primary-source coverage; 30 distinct proposals submitted over authenticated HTTP and accepted with HTTP 202. All 30 received actual `jev-1.13.0` calls. Results: **23 REJECTED, 7 NEEDS_REVIEW, 0 selected**. There were 27 valid receipts and 3 invalid probability distributions. No unchanged-evidence rerolls, no approval quota, and no selection-rule changes were used for this batch.

Each proposal includes observed entry trigger, maximum entry, support stop, target, 20 recent completed five-minute candles plus any older level anchors, source excerpts, economic relationship, and limitations. These are broad review contenders; the number 30 does not mean 30 executable or high-quality trades.

No managed setups, risk decisions, orders or fills were created. Ports8770/8780 were offline at the initial check. This bounded review used a separate ledger on port8781 with no broker credentials or executor. The review service and its PostgreSQL cluster were stopped cleanly after export. The second Jev position monitor was not exercised.

## What blocked selection

The unchanged categorical rule requires all four: overall APPROVE; news_stale NO; unsupported_inference NO; anticipation LOW or MEDIUM. There is no hidden confidence threshold. Ranking only runs after more than ten candidates already pass, so ranking caused no rejection here.

Of 27 valid provider responses:

- 26 overall verdicts were REJECT and one was Insufficient evidence.
- 23 classified news as stale; four classified it as new.
- 21 classified anticipation HIGH; three MEDIUM; three insufficient.
- Three found unsupported inference; 24 did not.

UNI, AAVE, ONDO and SUI were classified as having new information. UNI and AAVE still had HIGH anticipation. ONDO and SUI passed all three component evidence choices but received an overall REJECT. The model supplies typed answers rather than a narrative rejection rationale, so a more specific unstated reason must not be invented.

BCH, DOGE and SHIB responses each included one probability distribution summing to 0.99. The current provider contract requires distributions to sum to 1; these remain invalid rather than silently normalized. [TypeSafe API contract](https://docs.typesafe.ai/api).

## Is the policy too strict?

The implementation is narrower than the requested technical-plus-news selector. It supplies technical observations but asks no explicit technical-quality judgment; it requires fresh catalyst support even for technical retest hypotheses. The overall verdict independently repeats the evidence questions, which can produce inconsistent combinations. This is an architectural mismatch, not proof that lowering a cutoff would improve trading results.

The research also has real weaknesses. Much of the source coverage is old, scheduled, maintenance, roadmap, or evergreen material, deliberately labeled that way. Sparse source excerpts do not establish incremental token demand. Under the unactivated example cost policy, 22 proposals would exceed its 50bps round-trip-cost-to-risk allowance; 11 have less than 2R before costs, and four were above their proposed entry ceiling at observation. These are recorded research flags, not actual broker admission outcomes. Counts overlap. SUI has none of those descriptive flags, but liquidity, broker eligibility and trigger confirmation remain unverified.

Changing only the staleness gate would not approve this batch: every valid overall verdict was REJECT or insufficient. A proper next selection version should separately evaluate technical structure, evidence quality, news relevance/adverse risks, and strategy type, then compose those results in code. A fresh-catalyst strategy can retain its novelty gate; technical-led selection must be separately defined and evaluated. Neither a 5–10 selection target nor strong momentum should force an approval.

## Confirmed implementation defect

Fresh responses with explicit REJECT plus an uncertain component were labeled NEEDS_REVIEW, while recovering the same valid receipt could label them REJECTED. This contradicts documented rejection precedence. BONK, PEPE and WIF show this in the retained baseline. A narrow post-baseline correction now applies documented REJECT precedence only after a valid, receipt-bound answer. It also makes recovery classify the same answers consistently and compares uncertain results against the final receipt bytes. Historical events and this batch remain unchanged. Targeted cycle tests pass (26 research-cycle and 15 report-cycle tests).

An offline replay of the saved typed answers through the corrected composition produces 26 REJECTED and 4 NEEDS_REVIEW, still zero approved. It performs no new provider calls or database mutations. The recorded baseline below remains 23/7. [Replay artifact](../artifacts/muse-crypto-20-2026-09-20/post-fix-composition-replay.json).

## Code validation

Full backend suite after the fixes: **1,079 passed**, two dependency deprecation warnings; `./run ruff check src tests` clean; `git diff --check` clean. Regression cases cover explicit rejection plus uncertain evidence, receipt recovery without another vote, forged uncertain answers, and invalid provider distributions. The existing real-clock Muse delivery test also needed a deadline relative to its real clock instead of an already-expired fixed fixture timestamp; production Muse worker behavior was unchanged.

Source correction: `src/catalyst_lab/research_cycle.py`. Tests: `tests/test_research_cycle.py` and `tests/test_muse_worker.py`. No policy/questions/thresholds were relaxed and no existing runtime database was migrated.

## Submitted proposals and exact decisions

Prices are USD observations from this dated research batch, not active orders. Range-high targets are historical references; intervening resistance and attainability remain risks.

| Asset | Trigger | Max entry | Stop | Target | Gross R | Recorded outcome | Stale / anticipation / unsupported |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| XRP | 1.3899 | 1.3912899 | 1.3768 | 1.445 | 3.71 | REJECTED | YES / HIGH / NO |
| SOL | 108.55 | 108.65855 | 108 | 112.43 | 5.73 | REJECTED | YES / HIGH / YES |
| UNI | 8.6865 | 8.6951865 | 8.584 | 8.9341 | 2.15 | REJECTED | NO / HIGH / NO |
| AVAX | 10.957 | 10.967957 | 10.396 | 11.252 | 0.50 | REJECTED | YES / HIGH / YES |
| ADA | 0.22359 | 0.22381359 | 0.22046 | 0.23133 | 2.24 | REJECTED | YES / HIGH / NO |
| BCH | 247.82 | 248.06782 | 245.37 | 256.91 | 3.28 | NEEDS_REVIEW | invalid / invalid / invalid |
| YFI | 2162.28 | 2164.44228 | 2136.71 | 2287.8 | 4.45 | REJECTED | YES / HIGH / NO |
| AAVE | 135.55 | 135.68555 | 134.16 | 144.08 | 5.50 | REJECTED | NO / HIGH / NO |
| BONK | 0.00000295 | 0.00000295295 | 0.00000292 | 0.00000314 | 5.68 | NEEDS_REVIEW | YES / Insufficient evidence / NO |
| ARB | 0.20185 | 0.20205185 | 0.19652 | 0.21902 | 3.07 | REJECTED | YES / HIGH / NO |
| NEAR | 3.73 | 3.73373 | 3.6464 | 3.7686 | 0.40 | REJECTED | YES / HIGH / NO |
| XTZ | 0.351 | 0.351351 | 0.3352 | 0.381 | 1.84 | REJECTED | YES / HIGH / NO |
| RENDER | 1.5552 | 1.5567552 | 1.5292 | 1.6004 | 1.58 | REJECTED | YES / HIGH / NO |
| DOT | 1.1131 | 1.1142131 | 1.0955 | 1.1418 | 1.47 | REJECTED | YES / HIGH / NO |
| ETH | 2607.57 | 2610.17757 | 2575.2 | 2667.53 | 1.64 | REJECTED | YES / HIGH / NO |
| BTC | 80898.23 | 80979.12823 | 80465.01 | 81925 | 1.84 | REJECTED | YES / HIGH / NO |
| SUI | 0.8392 | 0.8400392 | 0.8195 | 0.8873 | 2.30 | REJECTED | NO / MEDIUM / NO |
| DOGE | 0.08573 | 0.08581573 | 0.08491 | 0.0913 | 6.06 | NEEDS_REVIEW | invalid / invalid / invalid |
| SHIB | 0.0000054 | 0.0000054054 | 0.00000533 | 0.0000057 | 3.91 | NEEDS_REVIEW | invalid / invalid / invalid |
| ONDO | 0.41297 | 0.41338297 | 0.40798 | 0.44133 | 5.17 | REJECTED | NO / MEDIUM / NO |
| PEPE | 0.00000395 | 0.00000395395 | 0.00000388 | 0.00000432 | 4.95 | NEEDS_REVIEW | YES / Insufficient evidence / NO |
| WIF | 0.1979 | 0.1980979 | 0.19577 | 0.21845 | 8.74 | NEEDS_REVIEW | YES / Insufficient evidence / NO |
| LDO | 0.40262 | 0.40302262 | 0.3948 | 0.42581 | 2.77 | REJECTED | YES / HIGH / NO |
| FIL | 0.9273 | 0.9282273 | 0.9135 | 1.1364 | 14.14 | REJECTED | YES / HIGH / NO |
| GRT | 0.020709 | 0.020729709 | 0.020398 | 0.02144 | 2.14 | REJECTED | YES / HIGH / YES |
| LINK | 12.29 | 12.30229 | 12.068 | 12.694 | 1.67 | REJECTED | YES / HIGH / NO |
| ATOM | 1.7218 | 1.7235218 | 1.6843 | 1.7824 | 1.50 | REJECTED | YES / MEDIUM / NO |
| HYPE | 91.58 | 91.67158 | 90.86 | 93.38 | 2.11 | NEEDS_REVIEW | YES / HIGH / NO |
| LTC | 57.393 | 57.450393 | 56.808 | 58.441 | 1.54 | REJECTED | YES / HIGH / NO |
| CRV | 0.3387 | 0.3390387 | 0.3294 | 0.3453 | 0.65 | REJECTED | YES / HIGH / NO |

## Source verification

All 12 source-pack-A excerpts and 21 of 22 source-pack-B excerpts were confirmed against direct HTTP responses. WIF was verified through web extraction; direct clients failed TLS negotiation before an HTTP response. ATOM initially failed a whitespace comparison at an inline tag boundary, then passed corrected direct extraction; both attempts are retained. Publication date-only fields remain distinct from exact timestamps.

## Retained evidence

Cycle: `b03abe45-cfb7-4c44-8704-0be18c35363d`. Submitted `2026-09-20T15:31:11.973173+00:00`; original expiry `2026-09-20T16:01:11.973173+00:00`.

Audit independently verifies **360 events**; head `1dd8bd2b63e58e247d94071b2d2ec56a2cf48666c78651b1445f147baf6e6531`.

- [Submitted report](../artifacts/muse-crypto-20-2026-09-20/report.json) and [HTTP acknowledgment](../artifacts/muse-crypto-20-2026-09-20/report-ack.json).
- [All recorded decisions](../artifacts/muse-crypto-20-2026-09-20/final-outputs.json) and [exact provider requests/receipts](../artifacts/muse-crypto-20-2026-09-20/provider-receipts-detailed.json).
- [Run summary](../artifacts/muse-crypto-20-2026-09-20/run-summary.json), [criteria counts](../artifacts/muse-crypto-20-2026-09-20/criteria-counts.json), and [proposed levels/technical flags](../artifacts/muse-crypto-20-2026-09-20/proposed-levels.json).
- [Source pack A](../artifacts/muse-crypto-20-2026-09-20/news-a.json), [source pack B](../artifacts/muse-crypto-20-2026-09-20/news-b.json), and [independent Sol criteria audit](../artifacts/muse-crypto-20-2026-09-20/criteria-audit.md).
- [Audit events](../artifacts/muse-crypto-20-2026-09-20/audit-events.json) and [checkpoint](../artifacts/muse-crypto-20-2026-09-20/audit-checkpoint.json).
