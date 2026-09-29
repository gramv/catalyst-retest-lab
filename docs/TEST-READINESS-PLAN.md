# Plan: complete local Jev paper-trading tests

Current checkpoint: [US-JEV-INTEGRATION.md](US-JEV-INTEGRATION.md). Research intake,
app-owned review worker, US admission evidence and the separate US engineering bridge
are locally tested. The starting-point inventory below is historical. Actual Alpaca
US acceptance is the next external proof; managed exits and crypto remain later stages.
No additional owner confirmation is requested for the local choices in that checkpoint.

2026-09-19. Planning artifact; no worker, admission gate or order behavior is enabled
by this document. Latest owner scope: **US stocks and crypto paper execution; Indian
stocks research-only**. Muse researches externally; Jev selection and position reviews
run inside the app. Local acceptance precedes Railway deployment and Muse connection.

## Starting point

The latest recorded full run is 433 passing tests and 14 browser checks. These establish
component behavior, not a complete trading workflow. Reuse the existing validation,
trigger, risk engine, broker ledger, reconciliation, receipt adapter and public dashboard.

| Area | Current evidence | Remaining gap |
| --- | --- | --- |
| US execution core | Local transaction/race tests; prior real engineering partial fill and protective flatten | Complete ordinary admission and nominal end-to-end acceptance |
| Live admission | `runtime.py:validation_evidence` explicitly refuses with VALIDATION_CONTEXT_INCOMPLETE | Assemble asset, liquidity, classification, feed, account and calendar evidence |
| Jev | Exact model jev-1.13.0, pinned skill, real review receipts | Application batch selection, durable worker and execution integration |
| Gate 1 reliability | Complete required-input validation | Approved timing, shared breaker/probes, jitter and end-to-end deadline behavior in worker/adapter |
| Step 4 evidence/intents | Schema 9 passes in disposable PostgreSQL; immutable bindings | Running DB remains at earlier schema; intents are inert and must not be retroactively armed |
| Position management | Mechanical protection, halt and calendar exits | Risk-authorized stop/target amendments and recovery |
| Crypto | Research display only | Market policy, execution/data adapter, fractional precision and protection controller |
| India | Research display only | Include in research report/selection pipeline; never execute |
| Dashboard | Simplified read-only public pages | Private current shortlist/positions and separate managed cohorts |

## 1. Research reports → Jev → final list

Build immutable, revisioned report intake for each market and research timeframe,
with per-item evidence, thesis, disproof and supplied levels. Preserve the US executable
candidate contract; a research item is not automatically a trading candidate. Research
records for crypto and India must not be disguised as US candidates to pass storage checks.

Reuse `review_storage.py`, `review_service.py`, `jev_contract.py`, `jev_store.py` and
`jev_review.py`; add numbered migrations and report/selection modules where needed.
Bind every selected/rejected/needs-evidence item to its actual receipt and policy version.
Expose authenticated report status/final-list reads for Muse and the private dashboard.
Paths and token scopes are to be specified in API-CONTRACT.md before implementation;
no raw order, quantity or risk-policy write scope is introduced.

Build SKEPTIC first for finished Muse reports. The separate original TRIAGE requirement
(300 raw movers → at most 20 research items) follows; do not silently apply its cap to
the 25–30 already-researched instruments. No shortlist padding or probability-as-win-rate.

**Pass gate:** a fixture report of 30 items in each market produces a complete accounted-for
result set; reject/ambiguity/missing review never becomes selection. Replayed and mismatched
revisions fail. Raw source text cannot change policy. A 300-mover TRIAGE fixture meets its
cap with receipts for every judgment. India cannot create an executable intent.

**Usable result:** test Muse-style report review and readback locally, without broker orders.

## 2. Durable app worker and approved reliability behavior

Add a PostgreSQL-backed queue/outbox, one fenced review chain per candidate/position,
append-only lifecycle events and restart recovery. Coordination must preserve table-role
restrictions; do not add UPDATE privileges to immutable ledgers for worker convenience.
Keep broker/protection processes independent of the Jev worker.

Apply all explicitly supplied Gate 1 values, including: only 429/529 retry; three total
attempts; 250 ms exponential backoff capped at 2 seconds with equal jitter; shared breaker
after three failed attempts; 30-second cooldown; one recovery probe at a time and two
valid successes; 3-second batch/attempt timeout; 10-second total event-to-result deadline;
60-second context/evidence age; no grace; 5-second worker heartbeat/15-second lapse.
The full source of values remains GATE-1-RELIABILITY-PROPOSAL.md and ReviewSettings.
These are provisionally accepted for testing, not silently promoted to final settings.

Current probe retry logic is not that complete policy: replace it deliberately, retain
only the single provider-call choke point, and test exact values. Batch independent
questions, bound parallel work explicitly and count queue time against each original
deadline. Overload must expire work honestly, not reset its clock when dequeued.

Persist exact provider receipts before deriving judgments so projection failure cannot
lose a received response. Missing/unknown provider outcome stays NEEDS_REVIEW; restarting
must not obtain a new favorable vote for unchanged evidence. Source publication time can
be historical; review freshness concerns the approved evidence/context revision, not
pretending an old disclosure was just published.

**Pass gate:** overload, timeout, malformed output, receipt projection failure, competing
revisions, clock-health failure and restart tests. Healthy/failed worker state is explicit.
Durable output resumes at a cursor without duplicate side effects. Emit measured p50/p95
and sample size; model latency alone is not event-to-order latency. Reviewer finalization
of Gate 1 follows the agreed test evidence.

## 3. Complete US admission and connect conditional entries

Implement `runtime.py:validation_evidence` using authenticated asset metadata, documented
liquidity observations, server-owned sector/theme mappings, current quotes, exchange
calendar and reconciled broker/account state. Record source and freshness for every input.
Do not remove VALIDATION_CONTEXT_INCOMPLETE until its actual evidence requirements pass.

Add an execution-capable authorization path distinct from legacy inert Step 4 intents.
Bind grant to selected research, immutable receipt, candidate/setup revision, purpose,
cohort and expiry. New evidence or invalidation revokes eligibility. Never promote old
STORED_ONLY_NOT_AUTHORIZED rows by installing a consumer.

At the frozen V1 printed-touch trigger, revalidate current ask, spread, quote, feed,
session/flatten deadline and review health. The existing atomic risk transaction remains
the sole authorizer: live equity, worst-entry sizing at M, no leverage, sector/theme limits,
1% reserved trade budget, 2% shared exposure and durable 3% daily halt. Consume the exact
risk decision within its five-second lifetime. Database/order transport guards stay intact.

**Pass gate:** approved research alone places no order; approval + exact trigger + valid
risk can submit through a fake broker. Jev-approved but risk-rejected loses. Missing/stale
approval, excessive ask, expired trigger, halt, duplicate event, parallel triggers and
late responses all fail safely. Calendar normal/early-close and uncertain submit tests pass.

## 4. First full local US paper acceptance

Act as Muse through the actual report interface. Start with fixtures and a disposable DB,
then a clearly labeled engineering packet with the real Jev provider. After passing those,
perform a bounded real Alpaca Paper run with current valid geometry and an engineering
candidate excluded from all strategy/managed performance. Never reuse the old dated SPY
candidate or manufacture a trigger to force a fill.

Preflight chain, current policy, credentials, feed, session, reconciliation and exposure.
Exercise report → Jev receipt → selection → watching → real trigger → risk reservation →
paper submit/fill → mechanical exit → zero broker/local exposure. Record non-trigger,
expiry and rejection honestly; they pass their branches but do not count as a successful
fill/close acceptance. A partial-fill protective flatten is a recovery test, not the
nominal full-loop pass. No per-trade confirmation loop is added to authorized paper trading.

**Pass gate:** exact request/receipt/order/fill links, full audit export and retained head,
clean final reconciliation, no duplicate order after timeout/restart, engineering exclusion.
A broker or data outage leaves the run incomplete rather than relaxed into a pass.

## 5. Jev-managed stops and targets

Use a separate versioned exit policy and cohort; preserve the fixed-exit V1 baseline.
Jev evaluates news and movement context and chooses HOLD, TIGHTEN_STOP, ADJUST_TARGET
or NEEDS_REVIEW. Do not invent a percentage or ATR trail. The exact level-selection
contract is a policy decision listed below; Jev must not be asked to calculate levels.

Add fresh position context, source-news revisions, expiring typed management intents,
exact risk authorization and a constrained amendment transport. Current mutation code
supports POST/DELETE; generic unrestricted PATCH is not an acceptable shortcut. Re-read
remaining quantity and acknowledged exits; only confirmed changes appear as applied.

**Pass gate:** rejected/missing/late amendment, partial fills, cancellation/replacement
races, unknown acknowledgement, restart, stop/target simultaneous fills and accidental
reversal prevention. Widening risk or postponing the mandatory exit is refused. Closed
positions cannot be revived. Missing news/model output preserves mechanical protection.
Then perform a separate engineering paper amendment acceptance before enabling management.

## 6. Crypto paper execution

Implement after its market policy is explicit. Reuse receipts, immutable lifecycle,
risk accounting and dispatch authorization; use a dedicated crypto execution/data adapter.
Do not coerce crypto into US whole-share/DAY/session rules or invent native bracket support.
US and crypto sharing an account share its equity and atomic exposure budget; do not allow
2% separately for each market. Unknown broker exposure continues to halt reconciliation.

**Pass gate:** symbol eligibility, decimal increments/minimums, stale data, stop-limit
non-fill/gap recovery, concurrent US+crypto exposure, cancellation/fill races, restart,
daily halt and defined holding deadline. Then real crypto paper entry/protection/exit
acceptance, with verified zero residual exposure and an excluded engineering audit trail.

India remains research-only throughout, including regression tests against accidental
order admission. No India broker or paper execution simulator is part of this plan.

## 7. Private UI, Railway, then Muse connection

Keep the public dashboard concise and delayed per existing disclosure rules. Add a private
view with final picks, review status, actual positions, acknowledged stop/target changes
and reasons. Show research state separately from order/fill state. Keep baseline, Jev-entry,
Jev-managed, engineering and manual/research evidence separated, with explicit currencies.

After local acceptance, deploy API, app-owned review worker and independent broker/risk
processes on Railway with Postgres and restricted roles. Use service-variable secrets;
no deployed .env. Test supervisor restart, durable delivery, DB backup/restore, hash
checkpoint verification and authenticated output polling. Then connect Muse's supported
scheduled shell/HTTPS caller. Do not assume a Muse-hosted worker or unsupplied agent API.

**Pass gate:** Muse sends a report and can recover the same final list after a restart;
worker failure blocks new active-policy admissions while broker protection continues.
Only claim unattended operation after persistent processing, secrets, delivery, restart
recovery and decision deadlines are demonstrated on the deployed host.

## Decisions to settle before dependent execution

These do not block report storage, fake-provider tests or independent UI work.

| Decision | Why needed / boundary |
| --- | --- |
| Research approval/ranking composition | SKEPTIC composition approved and implemented for local tests as JEV_SKEPTIC_RESEARCH_TEST_V1. Raw-mover TRIAGE policy remains text only; no hidden confidence threshold or quota. |
| US liquidity evidence policy | Minimum dollar volume, lookback, feed coverage and evidence age must be explicit; thin IEX coverage cannot masquerade as consolidated volume. |
| Jev-selected price levels | Specify the allowed source/candidate set of stop/target levels (for example source-annotated levels versus app-derived structure), action bounds and news freshness. Contextual judgment is authorized; a numeric formula is not supplied. |
| Crypto market policy | Define trigger/entry rules, quantity precision, liquidity/spread requirements, protection fallback, correlation mapping, holding expiry and 24/7 daily-halt reset/baseline. No unapproved strategy version or implicit US-policy extension. |
| Official R denominator | Existing reporting supports alternatives; freeze which measure is headline, while retaining fills, actual dollar P&L and explicit engineering risk basis. |

## Implementation/test loop

For each numbered stage: map the existing code → implement one real gap → run targeted
failure tests → full suite and lint for backend changes → verify observable behavior →
retain receipt/audit evidence → update PHASES.md → advance only when its gate passes.
Provider acceptance is separate from fixture success. Missing policy blocks only dependent
work. Credentials never enter tests, receipts, artifacts, command output or chat.

Stage 1 is now locally verified: **30 items per market with Jev-backed selection/readback
and no broker mutations**. See [RESEARCH-REPORT-SELECTION.md](RESEARCH-REPORT-SELECTION.md).
Stage 2 worker/reliability and the private shortlist are now locally implemented; see
[REVIEW-WORKER-LOCAL.md](REVIEW-WORKER-LOCAL.md). Full suite: **538 passed**, plus 16
browser checks. Reviewer finalization and deployment-specific clock/supervisor proof remain.
Next execution milestone: **approved US liquidity evidence and conditional entry integration**. The first end-to-end trading milestone is
stage 4, then managed exits and crypto. No schedule or background build loop is started
by creating this plan.
