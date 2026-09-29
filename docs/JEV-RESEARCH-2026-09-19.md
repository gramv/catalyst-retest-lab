# Jev research and implementation implications

Official documentation checked 2026-09-19. No authenticated provider calls yet; the owner will
supply a secure credential location. No measured latency, account model list or model pin exists.

## What is verified from the vendor's documentation

| Finding | Consequence for this lab |
| --- | --- |
| One supplied state is evaluated against independent typed questions | Muse retrieves evidence. Batch independent SKEPTIC questions; compose dependent steps in code or submit a new state |
| Choice yields a label/distribution; Score yields a rubric-weighted value; Noul yields a yes/no probability | Preserve each actual type and complete provider result. A rubric score is not a numerical calculator |
| Choice and Score confidence is calculated from their probability distributions; Noul has no confidence field | Preserve the raw confidence field and expose it as decision_confidence. Do not invent Noul confidence or trade-win probabilities |
| Jev accepts text/structured state, not images/audio/video | Send compact factual excerpts and app context; no screenshots or full-article dumps |
| Numeric/date reasoning and large/adversarial state are documented weaknesses | Code owns prices, counts, expiry and risk. Neutral short packets; source text cannot grant tools or permissions |

Sources: [API](https://docs.typesafe.ai/api), [State](https://docs.typesafe.ai/concepts/state),
[Confidence](https://docs.typesafe.ai/confidence),
[Limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

## Access and pinning discrepancy

The documented version is `jev-1.13.0`. The models page says `GET /v1/models` currently lists aliases,
while explicit version IDs may be accepted without appearing there. That differs from the build
instruction requiring an exact version returned by the list. The latest final Part A resolves this: if the account returns only aliases, a harmless evaluation explicitly requesting `jev-1.13.0`, with an exact matching response model, may establish the pin. This supersedes the intermediate stop-at-aliases reply. Do not substitute an alias or claim account access without a credential.

Documented limits are 1,200 requests/minute and 250,000 tokens/second; the vendor says these can change.
The 64k total/32k state-plus-largest-question context limits are ceilings, not a good packet target.
Versioned requests and receipts are essential for comparing experiments. [Models](https://docs.typesafe.ai/models).

## Adapter and runtime design

The HTTP contract is state/model/questions in; answers/model/usage out. Question-map keys are identifiers,
not the question text. Store exact JSON receipts plus their hashes, timing and actual returned model.
The adapter must validate all expected question IDs, answer types and permissible options before policy
composition. A successful HTTP response is not an execution permission. [API](https://docs.typesafe.ai/api).

The official skill supports Codex and other agent environments. Its generic documented channel is
`npx skills add typesafe-ai/skills --skill typesafe-ai`. Inspect and pin the repository revision before
installation; do not enable silent updates. Installation is deferred behind the access gate and does
not prove the external Muse runtime can run workers or consume events. [Agent skill](https://docs.typesafe.ai/agent-skill).

My implementation recommendation: a trusted executable adapter available to Muse, immutable evidence
revisions, authenticated receipts, durable outbox/consumer and bounded intents. Keep the trading app
out of the research loop. When new evidence supersedes an approval, revoke the old grant immediately;
model review never pauses protective execution. This is a design recommendation, not a vendor capability claim.

## Reliability and evidence

429/529 are documented retryable errors. Bound backoff/jitter by the decision deadline; a circuit breaker
must yield NEEDS_REVIEW rather than favorable defaults. Retain each transport attempt without counting
retries as independent favorable votes. [API](https://docs.typesafe.ai/api).

Measure queue delay, review service latency, complete Muse round-trip, intent validation and broker
acknowledgement separately. Report observed p50/p95 with sample counts and failures. Vendor benchmark
speed does not establish this application's event-to-order latency. Use SKEPTIC first on a fixed candidate
set; a TRIAGE rejection log must preserve all discarded movers before shortlist comparisons are credible.

Privacy: the vendor states no training on customer inputs and offers enterprise zero-data-retention.
That does not establish ZDR for this account. Market-only payloads remain required. No claim about
this account's retention, SLA or security certification has been independently verified.
[Legal documentation](https://docs.typesafe.ai/legal), [Models](https://docs.typesafe.ai/models).

## Current readiness

Research complete for the initial integration contract. Access verification, official skill installation,
model pin, approved templates, provider latency measurements and the Muse background interface remain
unverified. No Jev calls, active Jev trades or Jev performance results are being claimed.
