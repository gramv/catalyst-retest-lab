# Jev validation: first bounded run

The diagnostic harness is built and the first real-provider run is complete. The results show
useful evidence interpretation alongside mistakes and a malformed response. They do not establish
that the previous 30 crypto rejections were all correct, or that Jev adds trading value.

## What ran

- 60 explicitly synthetic engineering cases: 30 US-stock and 30 crypto scenarios, grouped into
  20 families. Related variants stay together in development or holdout, 30 cases each.
- An independent AI reviewer labeled the shuffled packets before provider evaluation. Six
  ambiguous cases remain excluded from accuracy denominators. The remaining 54 references have
  author/reviewer agreement; they are provisional AI references, not human-validated ground truth.
- One real `jev-1.13.0` call per case using `JEV_EVIDENCE_DIAGNOSTIC_V1`, with six parallel typed
  questions: novelty, economic support, technical coherence, and an evidence ID for each.
- Code combines the answers into supported, contradicted or needs-evidence diagnostics. A
  diagnostic carries no order, selection, size or risk authority. The application's existing
  selection policy and historical decisions were not changed.
- The run manifest froze cases, references, exact questions and randomized order before the
  calls. No prompt tuning, response normalization, repeated votes or favorable-answer selection.

Run ID: `01c0cb53-30bc-4db8-aff7-c88b89b01ca4`. Manifest frozen at
2026-09-20 16:05:02 UTC; isolated provider database stopped at 16:05:35 UTC.

## Results

59/60 provider responses passed the strict contract. One citation probability distribution summed
to 0.99 and remained an invalid response. All 60 original responses were retained.
Across all cases, the composed diagnostics were 21 supported, 22 contradicted, 16 needs-evidence,
and one provider failure. These are synthetic evidence judgments, not approved trades.

| Split | Cases | Agreed references | Valid scored cases | Matching composed judgments | Including service failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | 30 | 27 | 27 | 26/27 (96.3%) | 26/27 |
| Holdout | 30 | 27 | 26 | 22/26 (84.6%) | 22/27 (81.5%) |
| Holdout US | 15 | 14 | 13 | 12/13 (92.3%) | 12/14 |
| Holdout crypto | 15 | 13 | 13 | 10/13 (76.9%) | 10/13 |

Held-out reference groups were ten supported, ten contradicted and seven insufficient. Jev's
composed results matched 9/10, 9/10 and 4/7 respectively, counting the service failure in its
original reference denominator. One contradicted case was marked supported, two insufficient
cases were marked contradicted, and one supported case became uncertain because its citation
choice was tied. One additional insufficient case had the malformed provider response.

Raw held-out component agreement was novelty 26/26, economic support 26/26, and technical
coherence 23/26 among valid, agreed cases. Citation gating lowers novelty agreement to 25/26.
All selected citation IDs belonged to the blind reviewer's provisional allowed sets, but one
was tied: ID membership alone neither proves a valid unique citation nor proves its interpretation.

The descriptive Wilson interval for held-out composed agreement is 66.5%–93.8%. Variants within
families are correlated, so this is not a generalization guarantee or a profit-confidence interval.
The small per-market denominators do not support a stock-versus-crypto performance conclusion.

## What the errors teach us

In a held-out fictional crypto case, the claim said target 110 was below overhead rejection 106;
Jev marked technical coherence supported. In a development case, it supported a claim that stop
98 was below observed support 97. Two missing-overhead cases became contradicted rather than
insufficient. These are reproducible records of this run, not explanations invented for older votes.

There is also a benchmark design limitation: those level comparisons were present as numbers,
but their comparison booleans were not precomputed even though the instructions discouraged
model calculations. This confounds model interpretation with a responsibility that belongs to
code. A future version must supply deterministic comparison results and use fresh held-out
families. This frozen benchmark was not repaired and rerun to improve its score.

This run evaluates a new explicit-claim diagnostic, not the old holistic selector. It cannot
retroactively prove the prior SUI, ONDO or other real-crypto rejections correct. No numerical
promotion threshold was preregistered, so no pass/promotion decision is claimed.

## Shadow comparison implementation

`jev_shadow.py` now provides a separate, append-only SQLite observation ledger with frozen case
hashes, provenance, venue, decision, entry/maximum price, stop, target, expiry, quantity and explicit
cost assumptions. It computes the same deterministic baseline path for accepted and rejected
eligible cases; the treatment retains accepted cases. Service failures and uncertain selections
remain separate from completed accept/reject comparisons.

The crypto evaluator reuses the existing mechanical trigger without constructing an executor.
It requires a later fresh quote and displayed size for simulated entry, caps limit entry prices,
handles partial fills, latency assumptions, fees, expiry and stop gaps, and marks ambiguous bars.
Unknown fees produce unknown net results. Restart, duplicate-event, integrity, future-evidence,
prospective-registration and venue checks are covered by tests.

This is matched-opportunity simulation, not a portfolio simulator. Shared capital, correlated
exposure, account allocation, portfolio drawdown and clustered uncertainty intervals are not
implemented. Neither second-Jev exit management nor stock execution is evaluated by this crypto
shadow module. Those remain separate portions of the full protocol.

The standalone CLI now registers supplied frozen cases, imports observations, exports metrics
and performs bounded public collection. It does not discover symbols or create trade cases.
An actual 25-second Coinbase probe acknowledged BTC-USD/ETH-USD subscriptions and retained
505 messages: 456 tickers and 48 heartbeats, plus the subscription acknowledgment. It created
no cases or simulated fills. The ticker stream cannot establish a complete trade tape;
454 sequence jumps were flagged as coverage unproven rather than claimed as confirmed losses.
See [the probe receipt](../artifacts/jev-validation-2026-09-20/feed-probe/summary.json) and
[Coinbase's ticker-channel specification](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels).
The probe ended normally; no collection job is left running.

Operational entry points: `./run python scripts/jev_shadow.py --help` documents `register`,
`ingest`, `report`, `collect` and `probe`. Keep its SQLite store outside the checkout, for example
`~/.local/share/catalyst-retest-lab/jev-validation/shadow.sqlite3`. Collection accepts explicit
USD products, an existing eligible prospective case and a 1–300 second duration. `probe` needs
no cases and only saves public messages. Every capture requires a fresh artifact directory.
Future benchmark runs use `scripts/run_jev_validation.py --help` with frozen cases, blind packets,
labels and reviewer provenance. Reusing this exposed holdout cannot establish new validation.

## Evidence and integrity

The full artifact directory is [jev-validation-2026-09-20](../artifacts/jev-validation-2026-09-20/).
Primary records:

- [Frozen manifest](../artifacts/jev-validation-2026-09-20/real-run/manifest.json)
- [Blind review provenance](../artifacts/jev-validation-2026-09-20/independent-labels.provenance.json)
- [Exact provider receipts](../artifacts/jev-validation-2026-09-20/real-run/provider-receipts.json)
- [Corrected metrics](../artifacts/jev-validation-2026-09-20/real-run/metrics-v2.json)
- [Accounting correction record](../artifacts/jev-validation-2026-09-20/real-run/metrics-correction.json)
- [Audit checkpoint](../artifacts/jev-validation-2026-09-20/real-run/audit-checkpoint.json)
- [Execution counts](../artifacts/jev-validation-2026-09-20/real-run/execution-counts.json)

The audit chain verifies at 475 events, head
`b44c0eda4bddbe0ccb08e5dbde3a17b56ffc63e872fd1087d97ac75d3bb1e7f3`.
Orders, fills, risk grants and managed setups are all zero in this isolated run. The owner
database was not migrated or populated. The provider database is stopped.

Post-run code review corrected component scoring to distinguish raw labels from citation-gated
results, made missing planned arms count even when no calls return, and ensured interrupted
runs export their partial results before cleanup. The runner now verifies blind-packet/label
hashes and exact case states before provider calls. This run's original binding was independently
rechecked against the reviewer's frozen provenance; it passes. Original metrics remain retained,
and `metrics-v2.json` records the accounting correction without new provider calls. Group outcomes
and denominators are unchanged.

## Remaining evidence

The next performance experiment needs newly researched, prequalified Muse packets frozen before
forward observations. The earlier 30 proposals have expired and were not extended. No synthetic
case was submitted as a trade. A public-feed probe is connectivity evidence only.

Fresh independent cases with deterministic technical comparisons, independently adjudicated real
source packets, a locked portfolio allocation/evaluation policy, multiple future market sessions,
and a separate second-Jev exit comparison are still needed. An initial 200-opportunity checkpoint
is a research starting point, not an automatic pass.

Actual paper entry/fill/protection/exit acceptance is also separate. This turn did not launch a
broker session; Alpaca credentials were absent from both the current shell and the project
`.env`. The established software fixtures do not substitute for a new actual paper-provider fill.

See [the full protocol](JEV-VALIDATION-PROTOCOL.md) for those remaining stages.

## Software verification

`./run pytest -q`: **1,226 passed**, with two existing dependency deprecation warnings.
After the collector's final reporting/permission fixes and two additional tests, the entire
new benchmark/validation/shadow/feed test group passed **149/149**. These counts overlap;
they are not added together. `./run ruff check src tests scripts/run_jev_validation.py
scripts/jev_shadow.py` and `git diff --check` pass. Both CLI help commands execute successfully.
The 475-event provider chain and 505-message public-feed chain were independently recomputed
from their exported files; both retained heads match. PostgreSQL reports the evaluation
database stopped. Software checks are distinct from future strategy-performance evidence.
