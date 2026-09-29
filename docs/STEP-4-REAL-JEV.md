# Real Jev local follow-up — 2026-09-19

**The owner-authorized .env route works. One complete engineering review returned
from jev-1.13.0 in 458.43 ms; its exact receipt and audit chain verify. Full suite:
433 passed; lint passed. No judgment touches admission or authorization.**

The builder acted as the Muse-side caller. The provider response was real; the
candidate, source and market context were explicitly fictional engineering fixtures.
No broker call, real trade, deployment or Muse connection was performed.

## Credential and scope

The owner explicitly authorized a one-time transfer of the key already supplied in
this conversation into the local .env. The file is mode 0600, ignored by Git and
untracked, and excluded from deployment. The existing loader now reads it. No desktop
Keychain prompt or owner-at-desk step is required. No persistent chat-history reader
was added; subsequent calls use only the normal credential loader.

The real-call report and exported ledger were checked for the credential and contain
none. One older unit test did mistakenly load the real .env and its failed equality
assertion exposed the key in tool output. Test credential isolation now prevents
reading the owner's file or injected key; subsequent test output was also redacted
before display. The temporary credential should be revoked/replaced as the owner
already planned. This incident is not concealed as a clean credential-handling run.

## What happened, including the failed attempt

The first real call reached the adapter's valid-response path, but PostgreSQL rejected
the derived judgment: Python/driver float conversions could alter the provider's
decimal values before comparison with raw receipt JSON. The transaction rolled back.
There were no committed receipts, judgments, intents, orders, fills or risk decisions
from that call. Its raw response was not retained; neither its verdict nor latency
is claimed. The failed request and an appended engineering failure event remain in
the isolated audit log, with [failure evidence](step4-local-provider-attempt-1.json).

Three deterministic regression cases reproduced the error for Choice, Noul and Score.
The store now derives `answer_json`, `probability` and `decision_confidence` directly
from the stored receipt inside PostgreSQL. Exact bytes, hashes, triggers and role
restrictions remain unchanged. No new threshold, approximation tolerance or override
was introduced. The three regressions now pass.

The failed review was not resubmitted or turned into a new favorable vote. The next
call used a separate engineering candidate with a materially different **source
withdrawal** packet. Its result was retained exactly, without changing questions or
rerunning to obtain a preferred answer.

## Actual successful-call judgments

| Question | Real returned answer | decision_confidence |
| --- | --- | --- |
| `already_priced` | Insufficient evidence | 0.63 |
| `news_stale` | NO | 0.27 |
| `unsupported_inference` | NO | 0.90 |
| `verdict` | APPROVE | 0.46 |

Overall adapter result: **NEEDS_REVIEW** because one required answer was insufficient.
Confidence is not trade-win probability. The source-withdrawal case still received
an APPROVE verdict; this is a useful warning against interpreting successful typed
API output as validated research judgment. No template or policy was silently changed.

The intent POST returned **STORED_ONLY_NOT_AUTHORIZED**. The candidate remained
VALIDATED. Duplicate intent and mismatched bundle returned 409, intent reads 405,
and candidate admission through the review service 404. Local orders, fills and risk
decisions were all zero. Cohort: **JEV_ENGINEERING_TEST**.

The app recorded **458.434207946993 ms** for one retained provider request. Four
question rows share that one receipt; they are not four latency samples. With n=1,
p50/p95 would both equal that observation and do not establish service reliability.
The earlier failed call has no retained timing and is excluded, not counted as success.

## Receipt and audit checkpoints

Successful real-call receipt:

```text
model: jev-1.13.0
receipt: 5ab043d0-3690-4da0-aa89-c339cfb0a05b
request hash: a347e13765825127a09d4f8a6f480ddd318157b687761424929e6acb85a9ec75
response hash: b0dc21cdfd877b7916ecb0f72d3fcbad5fc272ba88193d34e581249126f985b2
receipt verified: true
audit events: 14
audit head: 7eba8d3573e78f0a89d9a855b85e8446d6020939010e6cb0c970faa60b0a80d8
```

Exact export: `/tmp/catalyst-step4-proof-wyyyd6b8/step4-events.jsonl` (0600).
The isolated database was stopped after export. This is not the main trading ledger
or a current broker-account exposure assertion.

Failed-attempt checkpoint after recording the failure:

```text
audit events: 9
audit head: 1291e0e9ec7ab9fb23f488bdda0eeae237a1d4f7c9b12aabc81bbf862a9967e9
raw response retained: false
```

The original fixture proof is preserved in [step4-local-fixture-proof.json](step4-local-fixture-proof.json).
The real result is [step4-local-proof.json](step4-local-proof.json). No historic outcome
or failed attempt was rewritten as a success.

## Checks and remaining boundary

The focused adapter/storage/configuration tests passed **131** cases. The full suite
then passed **433** in 37.02 seconds, with two dependency deprecation warnings. Lint
passed. Tests prohibit external TCP connections and isolate local credentials.

Deployment, connecting the actual Muse runtime, continuous workers, event consumers,
entry/admission/authorization wiring and Steps 5–7 remain deferred or forbidden under
the current scope. The stored key does not enable any of them. The current result
proves a supervised local provider/storage path, not unattended operation or an edge.
