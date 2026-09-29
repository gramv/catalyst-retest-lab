# Gate 1 — Live-review reliability, expiry and selection

Reviewed 2026-09-19. **APPROVED PROVISIONALLY FOR TESTING ONLY. Step 4 locally unpaused.**

These are proposed adapter/runtime controls, not changes to `CATALYST_RETEST_V1`.
No judgment touches admission or authorization. No values in this document have been
installed as live behavior. “FINAL” below means an existing owner requirement;
“PROVISIONAL” means a value accepted by the reviewer for testing only. Final approval
requires overload, expiry, competing-revision and restart tests against these values.
The later owner instruction authorizes Step 4 storage/configuration and local testing only.
The full future live-review runtime and Steps 5–7 remain forbidden.

## Later owner-authorized local implementation checkpoint

The paragraphs above and original proposal below preserve the earlier Step 4 scope.
The owner's subsequent app-hosted multi-market build direction superseded that limit.
The full agreed input object now governs the **local research worker**, with overload,
expiry, competing-revision, halt, heartbeat and restart tests. See REVIEW-WORKER-LOCAL.md.
The policy remains provisional; external clock/supervisor validation and final reviewer
acceptance are not inferred from passing local tests. No judgment authorizes trading.

## Required inputs and proposed values

Proposed policy identifier: `JEV_LIVE_REVIEW_POLICY_V1` (**PROVISIONAL**, not promoted).
Every named value below must be supplied explicitly to the future policy/runtime
constructors. This includes enum choices, fixed requirements and dependency scopes,
not just numbers. Missing, invalid or inconsistent inputs must prevent startup of the
review worker. No fallback environment values, class defaults or inherited probe
settings. Step 4 now requires the complete input object in `Gate1Inputs`; validation of
that object does not implement the future shared breaker, queue, heartbeat or selection loop.

| Required input | Proposed value | Flag | One-line justification |
| --- | --- | --- | --- |
| `retry_http_statuses` | Exactly `{429, 529}` | FINAL | The owner permits retries only for these two responses. |
| `max_attempts` | 3 total: initial call plus at most 2 retries | PROVISIONAL | Allows brief overload recovery while bounding cost and delay. |
| `backoff_base_ms` | 250 | PROVISIONAL | Starts with a short pause without immediately repeating an overloaded request. |
| `backoff_multiplier` | 2 | PROVISIONAL | Increases spacing after repeated overload. |
| `backoff_cap_ms` | 2,000 | PROVISIONAL | Prevents a retry delay from consuming most of the review window. |
| `jitter_mode`, `jitter_lower_fraction` | Equal jitter; uniform from 0.5 to 1.0 times the bounded backoff | PROVISIONAL | Disperses simultaneous clients without a zero-delay retry. |
| `retry_after_policy` | Respect a valid provider minimum; abandon retry if it exceeds the backoff cap or remaining deadline | PROVISIONAL | Never retry earlier than requested or extend the original review deadline. |
| `breaker_scope` | Provider + exact model + non-secret credential-slot identifier, shared across workers | PROVISIONAL | Workers using the same provider access must share overload state. |
| `breaker_failure_threshold` | 3 consecutive failed provider attempts | PROVISIONAL | A short repeated failure burst stops new review traffic. |
| `breaker_failure_classes` | HTTP errors, transport failures/timeouts, malformed responses, model mismatch and credential-echo violations | PROVISIONAL | Unusable provider responses count as unavailable service, irrespective of retry eligibility. |
| `breaker_cooldown_seconds` | 30 | PROVISIONAL | Gives overload time to clear without a long blind recovery interval. |
| `half_open_max_inflight`, `half_open_max_attempts` | 1 shared probe; 1 attempt per probe | PROVISIONAL | Prevents a recovery stampede or nested retry storm. |
| `breaker_recovery_successes`, `recovery_probe_spacing_seconds` | 2 consecutive valid probes, at least 1 second apart | PROVISIONAL | Requires repeat recovery evidence before reopening normal traffic. |
| `recovery_probe_policy` | Fixed synthetic, non-authorizing health packet; unique health-check IDs; no candidate packets | PROVISIONAL | Recovery checks cannot become repeated votes on a thesis. |
| `question_timeout_seconds`, `batch_timeout_seconds` | 3 each, sharing one batched HTTP-attempt timer | PROVISIONAL | Independent questions stay batched; a missing answer fails the complete batch. |
| `overall_review_deadline_seconds` | 10 from the original trusted event creation time | PROVISIONAL | Bounds queueing, credential access, evaluation, persistence and result delivery together. |
| `evidence_max_age_seconds`, `context_max_age_seconds` | 60 each | PROVISIONAL | Gives a completed review a short useful life without treating research as a per-tick decision. |
| `expiry_grace_seconds` | 0; `now >= deadline` is expired | PROVISIONAL | Avoids a hidden grace period that revives obsolete work. |
| `max_clock_offset_ms` | 250; unknown clock health fails closed | PROVISIONAL | Limits disagreement between the event producer, worker and decision consumer. |
| `same_candidate_max_active_chains` | 1 | PROVISIONAL | Serializes substantive review work for each candidate. |
| `selection_policy` | Exact current revision tuple, then first valid on-time committed result; conflicting results become NEEDS_REVIEW | PROVISIONAL | Prevents selecting a favorable answer or a higher confidence score from competing reviews. |
| `material_supersession_policy` | New material evidence/setup revision supersedes older work; routine quotes alone do not | FINAL | Preserves the owner's distinction between material changes and normal market updates. |
| `duplicate_policy` | Audit and reject; reuse the original request ID for transport retries | FINAL | A repeated event or packet cannot manufacture another favorable vote. |
| `heartbeat_period_seconds`, `heartbeat_deadline_seconds` | 5 and 15 | PROVISIONAL | Lets the caller detect a stopped worker within three missed intervals. |

The heartbeat values are included for the runtime contract in Gate 2. They are **not**
permission to build Step 5. Breaker-open, deadline, credential and heartbeat failures
must produce explicit unavailable/NEEDS_REVIEW outcomes, never implied approval.

## Retry and breaker semantics

For retry number `k`, starting at 1, the proposed backoff is
`b = min(2000 ms, 250 ms × 2^(k−1))`; draw uniformly from `[b/2, b]`.
Thus the two allowed retries wait 125–250 ms and 250–500 ms before considering any
valid `Retry-After` minimum. A larger provider minimum replaces the draw only if it
fits both the 2,000 ms cap and original deadline. Otherwise finish NEEDS_REVIEW.
An invalid `Retry-After` is recorded and the bounded jitter schedule applies.

There is no retry for 401, 422, other HTTP statuses, redirects, DNS/TLS/socket errors,
timeouts, invalid JSON, invalid typed answers or unexpected model versions. A timed-out
request has an unknown remote outcome; it must not become a new independent evaluation.
SDK, HTTP transport, proxy and job scheduler retries must not add attempts underneath
this policy. Every permitted retry uses identical request bytes and the same request
ID, with a distinct immutable attempt receipt. No fallback model is enabled.

Count failures in a durable, serialized attempt-completion order within the breaker
scope. A valid typed response from the pinned model resets the failure streak even
if its answer is REJECT or Insufficient evidence: availability is separate from
agreement. Local duplicates, stale candidates and credential lookup failures do not
count as provider failures; they still fail the review closed and are recorded.

After the third counted failure, stop new provider attempts for 30 seconds, including
remaining retries. Existing in-flight responses are retained but cannot close the
breaker. At cooldown expiry, a durable lease permits one synthetic health probe at
a time. Two consecutive valid probes close the breaker; any failed probe reopens it
for 30 seconds. Restarting a worker must not reset the breaker, probe lease or streak.
Probe receipts are engineering diagnostics and cannot authorize a candidate.

## Deadlines and irreversible staleness

TypeSafe returns answers for a question map in one evaluation response; there is no
documented per-question cancellation interface. The proposed three-second question
timeout is therefore a **shared batch-attempt deadline**, not a claimed provider
feature or a reason to serialize questions. No partial answer set is accepted.
[TypeSafe API reference](https://docs.typesafe.ai/api).

The proposed timing rules are:

- `review_deadline_at = min(original_event_created_at + 10s, evidence_valid_until,
  context_captured_at + 60s, candidate_expiry, calendar_flatten_at)`.
- A fresh evidence revision may have `evidence_valid_until` no later than
  `evidence_generated_at + 60s`, the candidate's original expiry, and calendar flatten.
  An earlier source/coverage deadline wins. Relabeling unchanged evidence with a new
  timestamp is not a refresh.
- Queue time, secret retrieval, template/context loading, all attempts/backoff,
   verification, receipt commit and delivery to the app-owned durable review inbox consume
  the same original ten-second window. Each HTTP attempt gets at most the smaller
  of three seconds and the remaining time. A backlog does not restart the clock.
- An otherwise valid result not committed **and delivered** before that deadline is
  permanently stale. Retain the exact late receipt and append the stale outcome;
  do not turn it into a decision on redelivery or after restart.
- For a result completed and delivered on time, its proposed maximum applicability is
  `receipt_usable_until = min(evidence_valid_until, context_captured_at + 60s,
  candidate_expiry, calendar_flatten_at)`. This deadline is derived from the original
  evidence/context, never from response arrival. No new intent may reuse it at or
  after that time. Any future armed intent must expire no later than this bound.
- Material source corrections, mismatched candidate/setup/evidence revisions,
  invalidation, a closed position, or an account halt immediately prevent use of an
  affected result. A later favorable response cannot restore the old eligibility.
  Supersession and invalidation are new audit rows; stored receipts are never edited.

The 60-second proposal concerns evidence coverage and review applicability. It is
not a maximum publication age for every source: retained earlier disclosures can be
relevant context. It also does not relax the frozen five-second quote freshness,
five-second risk authorization TTL, spread limit, mechanical trigger, sizing or
session exit. A future executor still re-reads current market and risk state.
Calendar flatten uses the existing official-close-minus-five-minutes calculation,
15:55 ET on normal sessions. This document changes no strategy rule.

## Selection when reviews compete on one candidate

Use the app's authoritative tuple `(candidate_id, candidate_revision, setup_revision,
evidence_revision, research_policy_version, question_set_version, context_snapshot_id)`.
Do not sort mixed revision numbers and guess which packet is newer; require an exact
match to the current server-owned binding.

1. Expiry, invalidation, closure, halt and material supersession take precedence over
   any model answer. Stale or mismatched work stays in the audit record only.
2. Allow one active substantive chain per candidate. A trusted material revision
   supersedes the old chain; obsolete in-flight results cannot win by arriving first.
3. Within the current binding, the first valid, complete, on-time committed review is
   the sole selectable result. Enforce uniqueness atomically. Duplicate events or
   repeat requests are rejected and audited, not re-evaluated.
4. If conflicting independently persisted results for that same binding are discovered,
   append a conflict event and mark the selection NEEDS_REVIEW. Do not cherry-pick by
   approval label, probability, confidence, model latency or latest arrival.
5. Missing evidence or ambiguity cannot be replaced with an unchanged rerun. A new
   substantive evidence revision never extends the candidate's original expiry.
   The reviewer struck the rework-cycle cap on 2026-09-19; no numeric rework limit
   is imposed by this contract.

This is same-candidate selection only. It neither promotes the current SKEPTIC
template to an authorizing policy nor defines ranking between different TRIAGE movers.

## Implementation boundary and approval record

The current adapter already requires a smaller `ReliabilityPolicy` constructor with
version, deadline, attempt budget, backoff base, failure threshold and cooldown. It
does **not** implement this entire proposal: capped equal jitter, the shared half-open
probe protocol, full event-to-inbox deadline, evidence lifetimes, revision selection
and heartbeat are not established live behavior. See [current adapter evidence](JEV-ADAPTER.md).

The previous three engineering calls are too small a sample to validate these live
values. After approval, a later authorized implementation must test overload, expiry,
competing revisions and restart behavior against the agreed values before wiring entry.

Reviewer verdict, 2026-09-19: **all proposed values accepted provisionally for testing**,
under `JEV_LIVE_REVIEW_POLICY_V1`; the rework-cycle cap is removed. Final approval:
**pending the specified runtime test evidence**. The subsequent owner instruction selected
Railway and superseded the Keychain-confirmation gate. Step 4 storage/configuration is now
tested locally, including evidence expiry and competing revision inserts. Those tests do
not clear the complete live-worker reliability gate. Entry wiring, admission logic,
authorization changes and Steps 5–7 remain forbidden. Deployment/Muse connection are deferred.
