# Real-world research and observation session

Completed 2026-09-19, Saturday. **Paper only. No trade was opened.**

Codex acted as Muse: retrieved original issuer evidence and real completed daily bars,
submitted one authenticated report to the app, and let the existing pinned Jev worker
evaluate it. This was real TypeSafe inference on real market evidence, not a fake-provider
fixture. The exchange was closed; old bars were never converted into live quotes or prints.

## Research and decisions

| Stock | Original evidence / research hypothesis | Recorded result |
| --- | --- | --- |
| AAPL | Previously announced product availability; no incremental demand surprise established | NEEDS_REVIEW — CONFLICTING_JUDGMENTS |
| NVDA | Australian infrastructure partnership; incremental booked revenue and an unpriced catalyst not established | NEEDS_REVIEW — CONFLICTING_JUDGMENTS |
| LEN | Post-earnings reversal hypothesis weakened by declining orders; no demand turnaround evidence | NEEDS_REVIEW — UNCERTAIN_JUDGMENT |

Original sources: [Apple](https://www.apple.com/newsroom/2026/09/get-ready-to-experience-iphone-18-pro-the-new-apple-watch-lineup-and-airpods-5/),
[NVIDIA](https://nvidianews.nvidia.com/news/nvidia-expands-ai-infrastructure-capacity-in-partnership-with-australias-data-center-ecosystem),
[Lennar](https://newsroom.lennar.com/2026-09-16-Lennar-Reports-Third-Quarter-2026-Results).
Issuer publication dates are retained separately; unknown publication times were left null.
Exact short excerpts, retrieval timestamps and response hashes were retained. Market context
is Yahoo Finance daily bars, explicitly distinct from executable Alpaca IEX observations.

An engineering reference geometry used the last completed close as T, M at 0.15% above T
rounded up to cents, the last daily low as S and the observed one-month high as P. These
are analyst watchlist references, not a new strategy, approved orders or profitability claims.
No target was moved to make the minimum reward/risk rule pass.

| Stock | T | M | S | P | Reward/risk at M |
| --- | ---: | ---: | ---: | ---: | ---: |
| AAPL | 336.13 | 336.64 | 332.53 | 338.49 | 0.450 — fails 2R |
| NVDA | 222.27 | 222.61 | 218.03 | 234.76 | 2.653 |
| LEN | 76.43 | 76.55 | 75.70 | 89.65 | 15.412 — distant historical reference, not an expected return |

All three final verdict answers were APPROVE, but other independent answers prevented
selection. AAPL/NVDA were judged stale and highly priced-in; LEN had insufficient evidence
for already-priced assessment. The owner-approved composition correctly yielded no selections.
This exposes an important judgment-quality limitation: the verdict alone remains unsafe.
No question was changed and no packet was rerun to obtain a favorable answer.

## Verified behavior

- Exact model: `jev-1.13.0`. Three attempts, three valid and integrity-verified receipts.
- Measured provider latency: p50 **476.11 ms**, p95 **534.72 ms**, n=3. This is not
  event-to-order latency or a representative latency benchmark.
- Real authenticated localhost HTTP intake/readback and restricted-role PostgreSQL.
- Original dispositions remained NEEDS_REVIEW; all three current-validity states became
  EXPIRED after the original 60-second evidence deadline. No Monday carry-forward grants.
- Research DB candidates/orders/fills/risk decisions: **0 / 0 / 0 / 0**.
- Broker and local positions, working orders and reservations remained zero. Paper
  equity/cash were **$9,999.85**, unchanged by this run.
- Existing account reconciliations at **18:57:22.282654**, **18:58:07.369885** and
  **18:58:52.270482 UTC** were clean, with empty broker positions/orders and no discrepancies.
- Alpaca calendar: next session **2026-09-21 09:30–16:00 ET**, flatten **15:55 ET**.
- **584 backend tests passed**, two upstream deprecation warnings; lint and diff checks passed.
- **11 real-data browser checks passed**, desktop and mobile. Fixed long market-data
  excerpts overflowing the evidence dialog via `overflow-wrap:anywhere`.

## Audit and retained artifacts

Review audit: **41 events**, verified head:

```text
36fac60e3b0f518c0c977021ea047dd1fdb9ff2873b82640450ec0001e93b1c1
```

Account audit checkpoint: **3,796 events**, verified head:

```text
60b75503152e0ea31708a4efdc385726f95d02f18c4e1489833d220480187647
```

The account monitor continues appending normal reconciliation events, so this is a
timestamped checkpoint rather than a permanently current head.

Artifacts are in `artifacts/real-world-2026-09-19/`: submitted report, source/bar provenance,
review receipts through the audit export, initial/expired readbacks, durable outputs,
before/after account snapshots, summary, browser checks and screenshots. The separate
private database is retained under `~/.local/share/catalyst-retest-lab/real-world-2026-09-19`.
The bounded runner refuses an existing session directory to prevent accidental review rerolls.

## Limits and next execution test

This proves the real research → Jev → recorded shortlist path and observes the existing
broker monitor. It does **not** prove a new paper entry, fill, managed exit or trailing stop.
There was no open trade to monitor. The research worker was stopped after its batch;
the temporary review UI/database were stopped after inspection. The existing account
observer and dashboard continue separately.

The account-connected runtime remains schema 8; the new integration was tested on an
isolated schema-12 research database. The account runtime was not migrated or restarted,
and the research process held no broker-mutation capability. The next regular-session
execution acceptance still requires a backed-up rollout of the tested integration,
current-process reconciliation, real fresh Alpaca data, issuer classifications and a
new qualifying research packet. Today's expired packets cannot authorize that test.
Model-driven trailing and target amendments remain unimplemented; mechanical protection
continues independently of Jev. India remains research-only; crypto execution is not tested here.
