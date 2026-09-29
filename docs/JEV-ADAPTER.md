# Jev access, pinned skill and receipt adapter

Verified 2026-09-19. v4.2 Steps 1–3 are implemented and tested. Steps 4–7 are not complete.
No Jev judgment is connected to trading admission or broker authorization yet.

## Access and exact model

Authenticated model discovery returned HTTP 200 and two aliases, without an explicit version.
The final Part A permits an explicit-version evaluation in this case. A synthetic evaluation requested
`jev-1.13.0` and the provider reported exactly `jev-1.13.0`; HTTP 200, 529.34 ms.
The application pins that version. The pin evidence is [jev-model-pin.json](jev-model-pin.json).
Raw access-test responses are preserved with mode 0600 outside the repository in
`~/.local/share/catalyst-retest-lab/runtime/typesafe-access/`.

The temporary owner-supplied key was stored in macOS Keychain under service
`catalyst-retest-lab.typesafe`, account `jev-review`. No key was written into source, configuration,
receipts or log files. Background Keychain reads encountered a desktop access prompt; that probe was
stopped. Background lookup now disables interaction and fails closed. The successful adapter probes
used the authorized credential only in a transient child-process environment. Persistent unattended
credential retrieval is **not proven** and needs a supported secret-manager/runtime path or desktop
Keychain approval. The key's vendor-issued format was verified by authentication, not an assumed prefix.

## Official skill

Installed through the official `npx skills` channel, installer version `1.7.0`, source commit
`65a39f393687675ce170e6094757de20370365b9`. The installed file SHA-256 is
`71ea90d7906c6554c4f4c460ef7361b2d26f59116ccdae986dc6d997b9389f52`.

The unmodified skill and lockfile are isolated at
`~/.local/share/catalyst-retest-lab/runtime/typesafe-skill/`.
Read `.agents/skills/typesafe-ai/SKILL.md` there when working on this integration.
No automatic skill updates are enabled; no changes were made to other projects or global agent skills.
The skill was read, followed by its official API, Choice and citation-check references.

## Implemented boundary

- `JevReviewer.jev_review` is the sole application evaluation transport to the fixed TypeSafe endpoint.
  Redirects and environment-derived proxies are disabled. Keys are never part of evidence or receipts.
- `jev_contract.py` pins the model and templates. All Choice questions include Insufficient evidence.
  Choice distributions, selected label, Score rubric/weighted value, Noul probability, usage, answer
  sets, duplicate JSON keys and non-finite values are checked in code. No confidence is a win forecast.
- Research question sets and their template hashes (each a named version; none is ever edited):
  `SKEPTIC_QUESTIONS_V1` (`abd65f517fb0ff324f70b07290326c780d81f16a0df11e3548221aff8b0b5cd5`;
  selection rules V2 and B1) and `SKEPTIC_QUESTIONS_V2`
  (`754d70c80c68a4ea9f453d998ae34b64f004070dc118daee2e5dbe8a27e6722e`; selection rule B2, added
  2026-09-25: V1's `news_stale`, `already_priced` and `verdict` verbatim, `unsupported_inference`
  replaced by `mechanism_contradicted`, `inference_labelled` and `factual_claims_supported`) in
  `jev_contract.py`; `MUSE_JEV_COMPARATIVE_QUALITY_V1` and `_V2` in `research_ranking.py`. The
  selection hashes are also pinned in admission SQL (migrations 014, 018 and 020). Texts and
  provenance: CONTRACT-RESOLUTIONS.md.
- SKEPTIC questions are batched. The adapter records judgments without granting trading permission.
  Insufficient evidence, explicit review requests and tied Choice distributions return NEEDS_REVIEW.
  Rejection labels remain in the raw receipt even when another question needs evidence.
- `jev_requests` stores exact outbound JSON and context binding metadata before sending. Its input and
  template hashes, original deadline and purpose remain immutable. Duplicate packet/request attempts
  are audited and do not call the provider again, including concurrent callers and after restarts.
  A crash after request persistence cannot silently produce a fresh vote on restart.
- `jev_receipts` stores exact response bytes, status, timestamps, actual model when parseable, latency
  and request linkage. Every retry has a separate receipt for the same exact request. Malformed or
  expired replies are retained and do not create actionable judgments. Transport failures have an
  explicit missing-response receipt. If the provider echoes the credential, response storage is
  deliberately suppressed and CREDENTIAL_ECHO_BLOCKED is logged; it is never called an exact receipt.
- `ai_decisions` normalizes each valid answer; `jev_decisions` joins its candidate/context, stage,
  policy/template and model provenance. Inserts must match the provider's original answer.
- All four new source tables are append-only by grants plus mutation/truncate triggers. Each inserted
  row has a database-created hash-chained event. Receipt verification checks exact-byte hashes,
  evidence/template hashes, normalized judgment completeness and audit envelopes.
- `catalyst_jev` has SELECT/INSERT only on review records, with no order, candidate, risk or general
  event-write authority. Its narrowly scoped trigger stamps only review audit rows. Muse/app credentials
  cannot write provider receipts. These are trusted-adapter observations, not TypeSafe signatures.
- Reliability policy values are required constructor inputs, with no enabled runtime defaults.
  Bounded exponential backoff+jitter applies only to 429/529; a persistent receipt-derived circuit
  survives restarts. Total HTTP time is bounded, and obsolete responses cannot become valid decisions.
  From 2026-09-25 a 200 whose body fails validation (`INVALID_PROVIDER_RESPONSE`, for example
  `INVALID_DISTRIBUTION_SUM`, 3 of 24 real calls that day) gets one further attempt on the same
  path, inside `max_attempts` and the deadline; each attempt keeps its own receipt and the invalid
  body is retained as received, never normalised. A credential echo is never retried.
  From 2026-09-27 (`JEV_RESPONSE_PRECISION_V1`) a distribution may miss 1 by up to 0.005 per
  outcome and a score may miss its distribution's weighted sum by up to 0.005 × (1 + Σ levels):
  the error of printing each number at two decimals, which jev-1.13.0 does (22 of 25 real
  quality bodies that day were 0.01 off). Answers stay exactly as received.
- A short-payload ceiling and credential/email/address checks provide additional protection. The
  market-only evidence allowlist and trusted bundle resolver remain Step 4 work; this adapter is not
  exposed as a general-purpose Muse HTTP endpoint.

## Validation and provider evidence

Full suite: **343 passed**, two upstream deprecation warnings, 40.52 seconds. Ruff and diff checks pass.
The 41 new adapter tests cover typed primitives, uncertainty, duplicate/concurrent calls, privacy,
HTTP/transport failure, timeouts, 529 circuit/restart/recovery, role separation, immutable tables and
privileged receipt tampering in a disposable database. All automated tests block external TCP.

Three additional **real TypeSafe** calls used short synthetic engineering SKEPTIC packets, each with
one attempt and a ten-second engineering deadline (`ENGINEERING_ACCESS_PROBE_V1`). These are not the
unanswered live-review policy or a trading acceptance run. All three returned valid typed responses
and verified receipts. One supported packet had APPROVE; two unsupported packets had REJECT, with
Insufficient evidence on an independent question and consequently NEEDS_REVIEW overall.

App-observed HTTP latency: **p50 478.27 ms / p95 778.14 ms**, sample count **3**. This tiny engineering
sample is not an SLA or event-to-order latency measurement. Exact judgments, receipt IDs and hashes:
[jev-adapter-provider-evidence.json](jev-adapter-provider-evidence.json).

After these calls the audit chain verified through event 3402 with head
`8a1877374ea3fb68d61c26e92c9e407ca1fc8b3989af372b74f703e845ddc757`.
Background reconciliation continues to append events, so this is a retained checkpoint.
No candidate, entry intent, risk authorization or broker order was created by these probes.

Schema 8 was applied after a consistent PostgreSQL snapshot backup and matching verified log export:
`~/.local/share/catalyst-retest-lab/runtime/jev-backup-20260919T140126053951Z/`.
Its retained prefix has 3381 events and head
`7ed29bf1f6e4f507b36837247ab26680fe7deb0a26c29939ed93ffb965fd2661`.
Existing API/observer/dashboard processes remain running; this additive migration does not enable AI
trading or change the paper risk gate. No nominal Phase 4 acceptance or unattended operation is claimed.

## Next gate

Before activating entry intents, resolve the requested live-review reliability/authorization values
and TRIAGE selection policy. Muse's supported runtime/invocation interface is also still unknown.
Step 4 must bind immutable evidence, revisions and a server-loaded context to authenticated receipts;
Step 5 must prove durable delivery/heartbeat/restart behavior in the actual Muse runtime. Step 6 must
link grants to fresh coded trigger/risk checks. Step 7 acceptance and separate cohort reporting remain
outstanding. The reviewed-question template is not a promoted authorizing research policy.

Primary references: [official skill](https://docs.typesafe.ai/agent-skill),
[API](https://docs.typesafe.ai/api), [models](https://docs.typesafe.ai/models),
[Choice](https://docs.typesafe.ai/primitives/choice),
[citation checking](https://docs.typesafe.ai/cookbooks/citation_check).
