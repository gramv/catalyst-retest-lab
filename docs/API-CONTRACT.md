# Muse API contract — Phases 1–4

The earlier sections retain the frozen baseline and historical review contracts. The
[managed schema-14 contract](#managed-schema-14-source-contract) at the end describes
the separate managed application's current source; it is not an activation record.

Implements the candidate intake and polling portions of [integration-spec.md](integration-spec.md).
Conflicts with the frozen build plan are resolved in [REFERENCE-RULES.md](REFERENCE-RULES.md).

Base URL for the local lab: `http://127.0.0.1:8765`.
Except `/health` and the local OpenAPI UI, all routes require `Authorization: Bearer <Muse token>`.
The token has exactly `candidate:create`, `candidate:read`, `analytics:read`.
The service has no order, override, broker-management, state-mutation or correction endpoint.

Schema 12 test-mode exception: when explicitly configured with
`JEV_PAPER_POLICY=JEV_US_SELECTED_FIXED_TEST_V1`, legacy `POST /api/v1/candidates`
returns 409 `REVIEWED_RESEARCH_REPORT_REQUIRED` and records a refusal. Reports arrive
through the authenticated review API; the app-owned bridge enrolls exact reviewed
engineering items. It creates no new client order/quantity permission. Private report
items include `paper_execution` (null if unenrolled) with cohort, outcome, state, open
quantity, expiry and revocation reason. Selection remains distinct from a risk grant
and from a broker fill.

| Method | Path | Scope | Behavior |
| --- | --- | --- | --- |
| GET | `/health` | None | Reports service/DB liveness, paper venue, strategy and submission mode |
| POST | `/api/v1/candidates` | candidate:create | Accepts dated batch envelope or single candidate; HTTP 201 for accepted or rejected candidate records |
| GET | `/api/v1/candidates` | candidate:read | Date-filtered log including rejections; `date=YYYY-MM-DD`, `limit=1..200`, `offset>=0` |
| GET | `/api/v1/candidates/{id}` | candidate:read | Original body, state, reason, expiry and evidence source |
| GET | `/api/v1/candidates/{id}/events` | candidate:read | Candidate event history with hashes |
| GET | `/api/v1/analytics` | analytics:read | US/Paper trade count and candidate counts; performance metrics explicitly unimplemented |
| GET | `/api/v1/stats` | analytics:read | Strategy statistics from closed trades (`days=1..3650`, `version`); TEST- engineering records excluded; official R stays null until the denominator ruling is implemented |
| GET | `/api/v1/trades` | analytics:read | Derived strategy trades with fill VWAPs and observed excursions (`days`, `limit=1..200`, `offset`) |
| GET | `/api/v1/market-data` | analytics:read | Read-only feed/account/worker diagnostics and admission gates |
| GET | `/api/v1/execution` | analytics:read | Event-derived orders/positions, last reconciliation and durable halt reasons; no mutations |
| GET | `/api/v1/risk` | analytics:read | Risk configuration state, active reservations and last durable daily halt; no mutations |

Transport failures: 400 invalid/ambiguous/non-finite JSON or unsupported Unicode, 401 unauthorized,
413 over 64 KiB, 422 malformed batch envelope. Transport failures do not create candidate records.
Unknown fields, including `size_shares`, are immutable `INVALID_SCHEMA` rejections.
Expiry is optional in requests, stored in the validation decision, and defaults to the provider's
official close. No exchange-close time is hardcoded in runtime validation.

Canonical batch envelope (1–50 candidates, all committed atomically). Each item retains its original
body, plus separately stored envelope date/version/batch ID/index. A candidate with invalid fields
is recorded as rejected without preventing other candidates in the batch from validating.
The date must match the current New York date; it does not override the server's session calendar.

```json
{
  "strategy_version": "CATALYST_RETEST_V1",
  "date": "2026-09-18",
  "candidates": [{
    "signal_id": "2026-09-18-INTC-01",
    "market": "US",
    "ticker": "INTC",
    "entry_trigger": 101.00,
    "max_entry_price": 101.15,
    "stop": 96.50,
    "target": 109.00,
    "risk_reward": "1:2.4",
    "catalyst": "EARNINGS",
    "thesis": "one-line reason",
    "disproof": "what would prove this trade wrong"
  }]
}
```

This exact sample is rejected with `MIN_REWARD_RISK`: `(109−101)/(101−96.5) ≈ 1.78`.
`risk_reward` is retained as an optional research label, never used in risk calculations.
Batch response: `{batch_id, date, strategy_version, items: [candidate records]}`.
Single-candidate requests return the record directly.

Single-candidate form from the build plan (numbers are illustrative, not a recommendation):

```json
{
  "strategy_version": "CATALYST_RETEST_V1",
  "signal_id": "2026-09-18-INTC-01",
  "market": "US",
  "ticker": "INTC",
  "entry_trigger": 157.35,
  "max_entry_price": 157.55,
  "stop": 155.95,
  "target": 160.30,
  "catalyst": "EARNINGS",
  "thesis": "one line",
  "disproof": "one line"
}
```

Strategy `catalyst` accepts the 14 named categories from the integration spec. `ENGINEERING_TEST`
is reserved for the TEST- operator acceptance path and cannot label a strategy signal.
All monetary calculations use decimal arithmetic. Numeric strings and JSON numbers are accepted.
Ticker input is normalized to uppercase before ticker/day claim checks.
Read endpoints are private; public post-close disclosure restrictions belong to the future dashboard.

Each candidate record includes `candidate_id`, `signal_id`, `ticker`, `date`, `strategy_version`,
`state` (the exact v3 state), `status` (Muse-facing), `rejection_reason`, `notes`, `payload_json`,
`submission_context`, `received_at`, `expires_at` and `evidence_source`.
`record_purpose` and `strategy_eligible` are server-derived, immutable provenance fields. TEST-
engineering records remain visible in private audit/status polling but are excluded from strategy
analytics and the future dashboard's restricted reporting views. Muse cannot set these fields.
Readback returns `status: rejected`, `status: expired`, `status: not_entered`, `status: open`,
or `status: closed_target | closed_stop | closed_time | closed_other`. The exact `state`
distinguishes VALIDATED, WATCHING, TRIGGER_CONFIRMED and INVALIDATED. `state_reason` exposes
trigger/expiry/invalidation/broker reasons; confirmation is not a fill. `fill_price` and `exit_price`
are weighted averages of individual broker execution prices; `size_shares` is actual cumulative
entry fills. They stay null without fills. `mfe_r`, `mae_r` and `net_r` remain null until Phase 5.
Preliminary sizing is never presented as a filled position. Canceled partial entries remain open
exposure; a terminal entry cancellation must not hide an existing position.

Poll by date; no persistent Muse connection is required. Retrying a signal records a duplicate rejection
instead of placing or replacing an order. Use the returned candidate ID to read the original decision.

## Rule identifiers

`INVALID_SCHEMA`, `CANDIDATE_DATE_MISMATCH`, `STRATEGY_VERSION_MISMATCH`,
`DUPLICATE_SIGNAL_ID`, `TICKER_ALREADY_ATTEMPTED`, `INVALID_LEVELS`,
`MIN_REWARD_RISK`, `INVALID_MAX_ENTRY`, `DATA_FEED_FAILURE`, `POLICY_NOT_CONFIGURED`,
`ASSET_NOT_TRADABLE`, `MARKET_SESSION_CLOSED`, `INVALID_EXPIRATION`, `ENTRY_WINDOW_CLOSED`, `STALE_QUOTE`,
`INVALID_QUOTE`, `MAX_SPREAD`, `MIN_DOLLAR_VOLUME`, `STARTUP_RECONCILIATION_REQUIRED`,
`INVALID_ACCOUNT_EVIDENCE`, `DAILY_RISK_HALT`, `CORRELATION_UNKNOWN`,
`CORRELATION_LIMIT`, `ZERO_SHARE_SIZE`, `MAX_OPEN_PLANNED_RISK`, `VALIDATION_CONTEXT_INCOMPLETE`.

Checks stop at the first failed rule. Duplicate claims precede strategy validation. A recognized
ticker consumes that day's attempt even if another field fails schema validation. This is the
conservative reading of one attempt per ticker per day.

## Read-only market diagnostics (Phase 2)

`GET /api/v1/market-data` requires `analytics:read`. It reports sanitized paper account values,
stream subscription state, latest quote provenance, feed coverage, freshness and admission gate.
`GET /health` exposes connection/worker status without balances, tickers or credentials.
`submission_mode` identifies the actual dispatch boundary. No market-ingestion or trigger-override
route is exposed.

Watching reasons: `STOP_TRADED_BEFORE_TRIGGER`, `PRICE_BEYOND_MAX_ENTRY`, `DATA_FEED_FAILURE`,
`CANDIDATE_EXPIRED`. A successful trigger carries `CATALYST_RETEST_V1` and a bracket intent in
the private event log. Without explicit risk configuration, candidates remain TRIGGER_CONFIRMED.

## Execution diagnostics (Phase 3)

`GET /api/v1/execution` is authenticated and read-only. `positions` and `orders` are derived from
immutable broker events; `last_reconciliation` includes its process ID, timestamps, comparison
snapshot and discrepancies. This historical record alone does not authorize watching: `/health`
reports the current monitor's connection, reconciliation freshness and `watch_permitted` status.
Startup reconciliation is required after every restart and each new session; snapshots expire
after 60 seconds. The normal polling cadence is 45 seconds. Recorded mismatches latch a halt,
including across restarts; no automatic correction or halt-clearing endpoint exists in Phase 3.

Health reports `phase: EXECUTION_LOCKED` / `submission_mode: DISABLED` for the read-only observer,
or `phase: RISK_GATED` / `submission_mode: RISK_DECISION_REQUIRED` when the separate risk engine is
configured. `trading_enabled` reports whether that gated risk runtime is configured; it is diagnostic,
never an authorization flag. In RISK_GATED mode only an exact, unexpired, one-use authorized request
can reach Alpaca Paper. All public routes remain read-only except candidate intake.

Clean reconciliation alone does not admit a candidate: the production asset/liquidity evidence
provider and data-quality policy are still incomplete. No missing evidence is replaced with test data.

## Final risk decisions (Phase 4)

`RISK_REJECTED` maps to `status: rejected` and exposes its actual risk reason. Common reasons include
`CORRELATION_LIMIT`, `CORRELATION_UNKNOWN`, `MAX_OPEN_PLANNED_RISK`, `ZERO_SHARE_SIZE`,
`DAILY_RISK_HALT`, `RISK_HALT`, `RISK_EXIT_PENDING`, `DAY_START_EQUITY_REQUIRED`,
`STALE_ACCOUNT_EVIDENCE`, `BROKER_RISK_EVIDENCE_UNAVAILABLE` and `CAPITAL_ACTIVITY_RECONCILIATION_REQUIRED`.
The risk engine recomputes size from live broker equity; preliminary quantities never authorize orders.
The authorization TTL is frozen at five seconds. The day-start baseline comes only from broker
previous-close equity captured during startup/session reconciliation and is preserved for that session.
`ENGINEERING_TEST_NOT_ENROLLED` rejects a TEST- candidate without an operator acceptance-run record.

`ORDER_SUBMITTED` is committed with the risk decision, reservation and dispatch intent. Its initial
reason is `RISK_AUTHORIZED_DISPATCH_PENDING`; it is not proof of broker acceptance or a fill.
Immutable claim/result events record acceptance, rejection or uncertainty. A timeout retains the
reservation and invokes broker lookup. Muse cannot retry the original order through a status route.


## Phase 5–6 read surfaces

Private Bearer-scoped `analytics:read`: `GET /api/v1/stats?days=30&version=...` and
`GET /api/v1/trades?days=30&limit=100&offset=0`. Candidate readback includes measurement coverage/feed
provenance. Official R/MFE_R/MAE_R are null pending an approved denominator; net R/cost adjustments are
unmodeled. Numbers are decimal strings, unavailable values are null, never fabricated zero outcomes.

Separate read-only dashboard (local port 8766; public host pending): `/`, `/health`,
`/api/public/stats`, `/api/public/candidates`, `/api/public/candidates/{uuid}`, `/api/public/trades`,
`/api/public/daily`, `/api/public/research/crypto`, `/api/public/research/india`, `/api/public/export.csv`.
Candidate lists accept state/version/limit/offset; trades and research accept limit/offset. Individual
US records and CSV are hidden until the stored official session close; aggregates may remain current.
Engineering records never enter these views. No execution/override or manual-result mutation route is exposed.

Research import is an operator-only CLI using source prices/quantity, not caller-computed outcomes.


## Step 4 isolated review storage surface

This is a separate service (`python -m catalyst_lab.review_service`), not an expansion
of the existing candidate API or Muse token. It has distinct write/read Bearer tokens.
No judge output changes admission or authorization. Local proof only; no public URL yet.

| Method / route | Scope | Result |
| --- | --- | --- |
| GET `/health` | Public | Storage/configuration health; provider NOT_PROBED; all execution flags false |
| POST `/api/v1/evidence-bundles` | Review write | Immutable bundle and server context hashes, revision, cohort and deadline |
| GET `/api/v1/evidence-bundles/{hash}` | Review read | Exact stored bundle and context |
| POST `/api/v1/entry-intents` | Review write | STORED_ONLY_NOT_AUTHORIZED; never an entry grant |
| GET `/api/v1/review-observations?cohort=JEV_ENGINEERING_TEST&after_seq=0` | Review read | Durable typed observations with cohort and sequence cursor |

Evidence body: `candidate_id` UUID, positive integer `revision`, and 1–8 `sources`.
Each source has `source_id`, public HTTPS `url` (no credential/query/fragment), exact
`excerpt` (1–1200 characters), RFC3339 `retrieved_at`, and optional RFC3339 `published_at`.
Excerpts total at most 8,000 characters; the source JSON and overall request also have
byte limits. No natural-language date field, computed metric, caller deadline, policy,
purpose, cohort, size or arbitrary execution field is accepted. Numbers/dates quoted
inside an original excerpt stay verbatim evidence; they never become trusted calculations.

The server loads the existing candidate and validation/calendar context, stamps the
purpose/policy and bounds expiry by the approved 60-second evidence/context age,
candidate expiry and calendar flatten time. Candidate context must already exist;
this service cannot create it. A new substantive revision appends a new bundle.
Changing only retrieval timestamps cannot create another review of unchanged content.

Intent body contains only `intent_id` UUID, `evidence_bundle_hash`, `ai_decision_id`
UUID and `strategy_version: CATALYST_RETEST_V1`. The decision must have a valid
recorded receipt bound to that same evidence/context/revision. Storing a record does
not test whether an answer approves a trade, reserve risk or change candidate state.
There is no intent GET, worker or consuming role. Duplicate IDs and binding conflicts
return 409, invalid payloads 422, missing/wrong scope 401; response errors omit inputs.

See COHORTS.md. The frozen trading API and risk capabilities are unchanged.

## Local research-report testing (selection policy)

This surface is separate from executable US candidates. All reports in this milestone
are server-tagged ENGINEERING_TEST / JEV_ENGINEERING_TEST and require a TEST- report key.
Selection never authorizes an order; India always remains research-only.

- `POST /api/v1/research-reports` (review write token): atomic immutable report revision,
  1–50 researched items, market US_STOCKS / CRYPTO / INDIA, source excerpts and supplied
  levels. Duplicate submissions/revisions return 409 with a persisted control event.
- `GET /api/v1/research-reports/{report_id}?revision=N` (review read token): exact revision
  and item records; omitting revision reads the latest. Each item includes its recorded
  disposition, effective current status, receipt reference and typed answers. Nothing is
  silently omitted from the final list. The response always has `authorizes_entry:false`.
- The trusted local harness invokes `review_report` in the app process through the existing
  Jev adapter; no public model-call, worker-control, raw order or selection-override endpoint.

Policy `JEV_SKEPTIC_RESEARCH_TEST_V1`: SELECTED only for APPROVE + news_stale NO +
unsupported_inference NO + already_priced LOW/MEDIUM. A valid REJECT verdict yields
REJECTED. Insufficient/missing evidence, ties, inconsistent APPROVE answers, unavailable
provider or incomplete projection yields NEEDS_REVIEW. No confidence cutoff, quota or
trade-win interpretation. This policy is approved for local research testing only.


## App-owned review worker and private shortlist (local test checkpoint)

Separate review service; trading API scopes above are unchanged. `/` serves an empty
private UI shell. A read token is entered in the browser and retained only in tab memory.
Reports and receipts never enter the unauthenticated shell or public performance pages.

| Method | Path | Credential | Behavior |
| --- | --- | --- | --- |
| GET | `/api/v1/research-reports?market=US_STOCKS&limit=50` | Review read token | Latest revision of up to 100 report IDs in the explicit US_STOCKS, CRYPTO or INDIA market |
| GET | `/api/v1/research-output?after_seq=0&limit=100` | Review read token | Durable REPORT_RECEIVED and RESEARCH_OUTCOME events; increasing audit cursor, maximum 200 per page |
| GET | `/api/v1/review-workers` | Review read token | Running/stopped/down/clock-unhealthy/failed/halted status with heartbeat timestamps |

The consumer persists next_cursor only after it has processed the page. Empty pages leave
it unchanged. Polling is read-only. Events carry expiry and cohort and never authorize entry.
A new material revision produces a refresh event even before its review completes. A caller
must re-read current report validity before using an old result; a historical SELECTED label
is not current approval. The write token grants none of these read permissions.

The app worker uses a separate catalyst_jev database connection and shares breaker state
by provider, exact model and non-secret credential slot. Operator halt/resume is a separate
DB capability, not a Muse/API endpoint. Health's worker_loop=false describes the API process;
worker health is read from the authenticated status endpoint.


## Managed schema-14 source contract

This is the implemented contract of the separate managed engineering application, not
proof that a running process has loaded it or that schema 14 has been activated. Earlier
runtime checkpoints in [MUSE-RESEARCH-BOUNDARY.md](MUSE-RESEARCH-BOUNDARY.md) are historical.
The frozen baseline API above retains its own routes and credentials.

All `/api/v1/lab/` routes require `Authorization: Bearer <private lab token>`. The static
UI shell and `/health` do not contain private research. `/health` reports DB liveness,
paper venue, cohort and supervision; it does not establish executor readiness. Responses
are `no-store`. JSON bodies reject duplicate keys and non-finite values. Authentication
failure is 401, a credential used outside its role is 403 `TOKEN_ROLE_NOT_PERMITTED`, a
research credential acting for another agent is 403 `AGENT_IDENTITY_MISMATCH` (see
[Agent identity and credentials](#agent-identity-and-credentials-2026-09-24-plan-13-minimal-form)),
oversized bodies are 413, invalid evidence is 422, unavailable configured capabilities or
database are 503. Errors omit rejected content and credentials.

Muse owns external discovery and research. There is no discovery-scan, raw-order,
quantity, policy override, cost-correction, or activation endpoint in this service.
Report acceptance, evidence claims, selection and news acceptance never authorize a
broker mutation. India remains research-only; managed results exclude the frozen baseline.

### Reports and reproducible technical evidence

`POST /api/v1/lab/research-reports` accepts at most 1 MiB and returns HTTP 202 with
`status: MUSE_REPORT_RECORDED`, `cycle_id`, `contender_count` (accepted items),
`submitted_count`, `rejected_count`, `item_results`, fixed `expires_at`,
`idempotent_replay`, `research_origin`, `agent_id`, `agent_version` (both `null` for a
legacy report), `polling_url`, and `trade_authorized: false`.
The body contains `report_id` (UUID), timezone-aware `generated_at` and `valid_until`,
1–30 ordered `items`, and optional `schema_version: "AGENT_RESEARCH_REPORT_V2"`. A V2
body also requires the `agent` block described below; an unversioned legacy body may not
carry one. An `AGENT_RESEARCH_REPORT_V3` body (`picks` instead of `items`) follows [its own
contract](#report-v3-agent_research_report_v3) below. Use the returned `cycle_id` for every cycle route: for a V2 body it is derived
from the agent and report ID, not the report ID itself.
Exact retries reuse the report ID and return the stored response, `item_results`
included; changed content fails with `REPORT_IDEMPOTENCY_CONTENT_MISMATCH` (the report
hash covers every item, including rationale and confidence, and the agent block). Original
generation time and configured maximum age bound expiry; arrival does not restart the
clock. Signal IDs and market/symbol pairs must be unique within the report.

Each item contains `signal_id`, `market` (`US_STOCKS`, `CRYPTO`, `INDIA`), `symbol`,
`direction: LONG`, `catalyst`, `thesis`, `disproof`, `economic_relationship`,
`technical_analysis`, `levels`, `sources`, optional `technical_evidence`, and
`selection_rationale` (required on every item of an `AGENT_RESEARCH_REPORT_V2` body;
optional in unversioned legacy bodies, whose items without it are stored with
`selection_rationale: null` and never given a synthesized one). Levels contain positive
finite decimal `entry_trigger`, `max_entry_price`, `stop`, and `target`; quantity and
authority overrides are not accepted. Thesis/disproof/economic text is bounded to 1,000
characters each; technical analysis to 2,000.

#### Per-item intake results (2026-09-24, plan 1.2)

Each item is validated on its own. A failing item is recorded as
`RESEARCH_ITEM_REJECTED_AT_INTAKE` (`index`, `signal_id` when well formed, `code`,
`errors`, and `item_sha256`; content that failed validation is stored only as that hash)
while its siblings are recorded and reviewed. `item_results` lists every submitted item
in order: accepted entries carry `index`, `signal_id`, `status: ACCEPTED`, `item_key`,
`revision: 1`, `evidence_hash` and `dossier_bytes`; rejected entries carry `index`,
`signal_id`, `status: REJECTED`, `code` and `errors`. Item codes include
`INVALID_RESEARCH_ITEM`, `SELECTION_RATIONALE_REQUIRED`, `SELECTION_RATIONALE_INVALID`,
`CRYPTO_LEVEL_OFF_PRICE_GRID` (against same-day recorded asset metadata; previously this
refused the whole report), `INVALID_SOURCE_EVIDENCE`, `TECHNICAL_EVIDENCE_REQUIRED`,
`LEVEL_OBSERVATION_MISMATCH`, `FUTURE_TECHNICAL_EVIDENCE` and `DOSSIER_OVER_BUDGET`.
Accepted items keep their report position as `rank`.

The whole report is refused with HTTP 422 and nothing stored for an invalid envelope
(`INVALID_MUSE_REPORT`, duplicates, bad timestamps or expiry), credential-like or
personal content anywhere (`SENSITIVE_EVIDENCE_REJECTED`), staleness/expiry, or when
no item is acceptable (then `detail` is the first item's code, `INVALID_MUSE_REPORT`
for a schema failure). 422 bodies are `{"detail": code}` plus `errors` (report-level)
or `item_results`. Every error entry names a field path and a code, for example
`{"path": "items[3].selection_rationale.claims[0].supported_by.source_ids[1]",
"code": "RATIONALE_REFERENCE_UNKNOWN"}`; submitted values are never echoed and
non-schema dictionary keys are masked as `*`. Budget errors also carry `bytes` and
`budget_bytes`.

#### Agent identity and credentials (2026-09-24, plan 1.3 minimal form)

Every `AGENT_RESEARCH_REPORT_V2` body carries the proposing agent:

| Field | Contract |
| --- | --- |
| `agent.agent_id` | `^[a-z][a-z0-9_-]{1,31}$`; must be the agent of the submitting credential |
| `agent.agent_version` | `^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$` (at most 32 characters) |
| `agent.guidelines_version` | `^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$` |
| `agent.guidelines_sha256` | 64 lowercase hexadecimal characters |
| `agent.run_id` | UUID of the agent's research run |

Exactly these keys. A V2 body without the block, or a legacy body with one, is refused
whole with 422 `INVALID_MUSE_REPORT` and `errors` path `agent`, code
`AGENT_IDENTITY_REQUIRED` or `AGENT_IDENTITY_REQUIRES_V2`; a malformed field is 422 with
its path (for example `{"path": "agent.agent_id", "code": "STRING_PATTERN_MISMATCH"}`).
Validation answers before the credential check. There is no registry in this form: the
version and guideline fields are recorded as the agent declares them, not verified.

Credentials: each configured research-agent token (private config v2 `agents`, one token
file per agent) acts for exactly its `agent_id` and has Muse's routes (from 2026-09-26 these
include the read-only `GET /api/v1/lab/research-context`, and from 2026-09-29
`POST /api/v1/lab/research-withdrawals`, which acts only on the caller's own picks). The
legacy Muse token (`MANAGED_API_TOKEN`, `muse.token_file`) acts for agent `muse` (the Muse
worker's default version is `LEGACY_UNDECLARED`) and is the only credential that may submit
an unversioned legacy report. Any other combination is 403
`AGENT_IDENTITY_MISMATCH` with nothing stored: a report naming another agent, a legacy
body from an agent credential, or the legacy token naming an agent other than `muse`.
The same 403 guards the writes that act on a recorded cycle or setup: evidence-task
claims, evidence revisions and position news are accepted only from the credential of
the cycle's (setup's) recorded agent, and an unattributed legacy cycle only from the
legacy credential. Their bodies are unchanged and carry no agent block. Reads are not
restricted by agent.

Cycle IDs: a V2 report's cycle is `uuid5(284306f0-6a31-5d71-a753-f854db41ccfa,
"<agent_id>:<report_id>")` (the namespace is `uuid5(NAMESPACE_URL,
"urn:catalyst-retest-lab:agent-research-cycle:v1")`), so two agents reusing a report ID
never share a cycle. A legacy report keeps `cycle_id = report_id`.

Attribution and blind review: the validated block is stored as `agent` in
`RESEARCH_STARTED` and in every `RESEARCH_PACKET` body outside `state`, so it follows the
packet into `RESEARCH_SELECTED` and the setup record; the Jev receipt's
`evidence_identity` adds `agent_id` and `agent_version`. It is never part of a review
state or any Jev request. The state's `technical_context.origin` is the same
`EXTERNAL_RESEARCH_AGENT` for every proposer and for new legacy intake (states recorded
earlier keep `EXTERNAL_MUSE_RESEARCH`). The packet body's `research_origin` is
`EXTERNAL_RESEARCH_AGENT` for V2 reports and `EXTERNAL_MUSE` for legacy ones. Legacy
reports record `agent: null` and read as `LEGACY_UNATTRIBUTED`, never as Muse.

#### Selection rationale (`AGENT_SELECTION_RATIONALE_V1`)

| Field | Contract |
| --- | --- |
| `claims` | 1–8 objects: `claim_id` (`[A-Za-z0-9_-]{1,32}`, unique), `kind` (`CATALYST`, `NOVELTY`, `ECONOMIC_LINK`, `TECHNICAL`, `RISK`), `text` (1–300 characters), `supported_by` {`source_ids` (≤8), `bar_ids` (≤8)} |
| `why_now`, `why_these_levels`, `why_over_peers` | 1–500 characters each; peers are named by symbol only |
| `what_would_change_my_mind` | 1–300 characters |
| `known_risks` | 0–5 strings of 1–120 characters |
| `agent_confidence` | `level` (`LOW`, `MEDIUM`, `HIGH`) and `basis` (1–200 characters) |

Every claim must cite at least one `source_id` of the same item's `sources` or `bar_id`
of its `technical_evidence.bars`; an empty, unknown or repeated citation rejects the
item (`CLAIM_SUPPORT_REQUIRED`, `RATIONALE_REFERENCE_UNKNOWN`,
`DUPLICATE_RATIONALE_REFERENCE`) with the exact path. All rationale text passes the
same privacy screen as the rest of the report. The rationale is part of the review
state and so of `evidence_hash`: an edited rationale is new review input, an unchanged
one cannot obtain another vote. `agent_confidence` is stored in the packet's
`selection_rationale` for analytics only and is never part of any Jev request.

#### Review dossier (`REVIEW_DOSSIER_V1`)

Selection Jev reviews exactly the stored packet `state`, which intake builds as a
deterministic dossier: every text field and source excerpt untruncated, code-computed
technical metrics over all bars, only the bars cited by the levels or rationale claims,
and the rationale (`state.rationale`, labelled `UNVERIFIED_PROPOSER_CLAIMS`) without
`agent_confidence`. The encoded state (JSON with non-ASCII escaped, the bytes Jev
receives) must fit 11,000 bytes and the rationale 3,000 of them; otherwise the item is
rejected `DOSSIER_OVER_BUDGET` and never truncated. (Jev's hard cap on a review state is
12,000 bytes; the largest current wrapper, the comparative QUALITY review, adds 29.)
Each accepted item also appends `RESEARCH_DOSSIER` with the manifest: budgets, state
bytes and SHA-256, per-section bytes and hashes, per-source excerpt sizes, included and
omitted bar IDs with hashes, rationale bytes and citations, and an empty `truncated`
list. The full submitted report, all bars included, stays in `RESEARCH_STARTED.report`.

Every source contains `source_id`, public HTTPS `url`, exact `excerpt` (at most 1,200
characters), timezone-aware `retrieved_at`, and nullable `published_at`. Unknown
publication time remains JSON `null`; it is never replaced with retrieval time.
There must be 1–8 sources with unique IDs. Retrieval cannot be in the future and a known
publication time cannot follow retrieval. The server computes `content_hash` from the
excerpt, preserves the URL and normalizes times to UTC. Follow-up evidence and position
news also accept an optional matching `content_hash` and optional annotations
`primary_source`, `asset_relevant` (booleans), `novelty` (`NEW_FACT`, `PREVIOUSLY_KNOWN`,
`UNVERIFIED`), and `stance` (`SUPPORTS`, `ADVERSE`, `NEUTRAL`, `WITHDRAWN`). A supplied
incorrect hash is rejected. These annotations remain untrusted evidence metadata.

`technical_evidence` is an optional schema extension for compatibility; an explicitly
configured research policy can require it. Its exact fields are:

| Field | Contract |
| --- | --- |
| `schema_version` | `MUSE_OBSERVED_TECHNICALS_V1` |
| `provider`, `venue`, `feed`, `source_url`, `retrieved_at` | Named provenance, HTTPS source and aware retrieval time |
| `timeframe_seconds` | Integer 60–86,400; no implied aggregation |
| `bars` | 20–64 ordered completed observations, each with unique `bar_id`, `started_at`, positive finite OHLC and nonnegative `volume` |
| `quote` | Nullable object with `observed_at`, positive `bid` and `ask`; crossed quotes rejected |
| `level_references` | Exactly `entry_trigger`, `stop`, `target`; each names a retained `bar_id`, OHLC `field`, and bounded `rationale` |

Referenced OHLC values must equal the proposed levels. Code retains the observations
and their hash and computes descriptive SMA, volume, spread, age and gap metrics. The
review dossier carries the metrics, the hash and only the cited bars; the others stay
in the stored report and are listed in the dossier manifest.
Missing quote or zero prior volume yields nullable corresponding metrics. Submitted
observations are external research, not authenticated executable market data; fresh
broker eligibility, quotes, liquidity, trigger, expiry and risk checks remain mandatory.

### Research context and `AGENT_RESEARCH_REPORT_V3` (2026-09-26, package research-v3)

Crypto only; picks must be coins Alpaca Paper can trade; one
research run a day at 08:00 New York time (a second run can be switched on); 20 picks per run.
Jev reads each pick exactly as the agent sent it and the independent system check comes after
Jev's selection, at admission (package system-check, [below](#system-check-at-admission-system_check_v1-2026-09-27)),
so report-V3 intake validates format only. Fixture evidence only so far; intake and the context
authorize, size or check no trade.

#### `GET /api/v1/lab/research-context`

Read-only, on Muse's route list: every research-agent credential and the legacy Muse
credential; the status credential may read it (GET only); the operator credential is 403.
Decimals are fixed-point strings and times RFC 3339. Without the service the route is 503
`RESEARCH_CONTEXT_NOT_CONFIGURED`.

| Field | Content |
| --- | --- |
| `context_version`, `as_of` | `RESEARCH_CONTEXT_V3` from package research-loop-app (2026-09-29; `RESEARCH_CONTEXT_V2` from package learning-app before it, `RESEARCH_CONTEXT_V1` before that) and the instant described |
| `caller` | `role`, and `agent_id` for a research or legacy credential |
| `schedule` | `RESEARCH_SCHEDULE_V1` (`version`, `timezone`, `runs`, `grace_minutes`), `current_run_slot` (latest run at or before `as_of`), `current_run_valid_until_limit`, `next_runs` (the next occurrence of each run), in the schedule's zone; `null` without `MANAGED_RESEARCH_SCHEDULE_JSON`. Under `RESEARCH_SCHEDULE_V2` also `daily`, `current_run_kind` and `next_run_kinds` ([below](#research-context-v3-watching-setups-and-the-v2-schedule-research_context_v3)) |
| `report_format` | `schema_version`, guidelines version and SHA-256 (`MUSE_RESEARCH_GUIDELINES_V6` from package learning-app), `picks_target` 20, `picks_max` 30, `skipped_max` 200, dossier and rationale budgets, `max_report_age_seconds`, `report_max_seconds` |
| `universe` | `count`, `coins`, `excluded`, `sources`, `issues` (below) |
| `open_trades` | the caller's open positions: `setup_id`, `symbol`, `signal_id`, `state`, average `entry`, current `stop` and `target`, `quantity`, `opened_at`, original `levels`, `unrealized_pnl_usd` at the latest bid (`null` without a quote), and (package day-review) `review_at` (T of the next review) and `continuations` under `CRYPTO_24H_REVIEW_V1`, `_V2` or `CRYPTO_WINDOW_REVIEW_V1`, else `null`, and (package review-window) `holding_window_seconds`, the window a trade under `CRYPTO_WINDOW_REVIEW_V1` or `CRYPTO_WINDOW_HOLD_V1` recorded at admission, else `null` |
| `watching_setups` | package research-loop-app (V3 only): the caller's own report-V3 setups still `WATCHING`, which `open_trades` never lists ([below](#research-context-v3-watching-setups-and-the-v2-schedule-research_context_v3)) |
| `pending_reviews` | package day-review: the caller's pending 24-hour reviews and Jev exit flags, exactly the `items` of `GET /api/v1/lab/reviews` ([below](#24-hour-reviews-and-early-exits-package-day-review-2026-09-27)); `[]` for the status credential |
| `recent_outcomes` | `closed_trades` of the last 7 days (at most 50, newest first; `exit_reason`, `entry`, `exit`, `quantity`, `gross_pnl_usd`, `net_pnl_usd`, `fees_verified`, `r` = the measurement's `test_r` with `r_basis`) and `last_run` (the caller's latest research cycle, each pick with its status) |
| `lessons` | package learning-app (V2 only): the caller's own sanitized learning record, `RESEARCH_LESSONS_V1` ([below](#research-context-v2-lessons-research_context_v2-research_lessons_v1)); `null` for the status credential |
| `trade_authorized` | `false` |

The universe is every active, tradable Alpaca crypto asset quoted in USD (`XXX/USD`) whose
base is not a stablecoin (USDC, USDT, USDG, DAI, PYUSD, USDP, TUSD, FDUSD, EURC; PAXG stays)
and whose price increment, minimum order size and quantity increment are positive; `excluded`
lists the stablecoins and counts the rest. Each coin carries `symbol`, `bid`, `ask`,
`quote_at`, `spread_bps` ((ask − bid) / mid × 10,000, two decimals), `last_trade_price`,
`last_trade_at`, `price_increment`, `min_order_size`, `quantity_increment` and `volume_24h`
(`base`, `usd`, `completed_hour_bars`, or `null`). Sources, each labelled in `sources`: the
asset list is one `GET /v2/assets?asset_class=crypto&status=active` through the read-only
paper transport and the account's request-budget governor as a research read, cached for an
hour; quotes and trades are the Alpaca crypto market-data latest quotes and latest trades, one
request each per `batch_size` symbols, cached under 5 seconds; 24-hour volume is summed over
the 24 completed 1-hour bars (notional VWAP × volume, else close × volume), cached until the
next hour and retried after 60 seconds when it failed. A missing quote or volume is an
`issues` entry, never a zero; an unreadable or empty asset list, including a read refused by
the budget, is 503 `RESEARCH_UNIVERSE_UNAVAILABLE`.

The caller's own records: a research credential sees its agent's setups and cycles; the legacy
credential sees agent `muse` and unattributed legacy records; the status credential sees none.
Operator `ENGINEERING_TEST` setups never appear. A last-run pick's `status` is
`REJECTED_AT_INTAKE` (with `intake_code`), else its setup's state once admitted, else, once
published, its `selection_status` (below), else under top-K once the cycle is ranked `SKIPPED`
(recorded `RESEARCH_SELECTION_SKIPPED`), `NOT_SELECTED` (ranked, never published), `VETOED` or
`NOT_RANKED`, else the latest review disposition, else `AWAITING_REVIEW` (package replacement,
2026-09-27: a declined pick is no longer shown as `SELECTED`).

Each pick also carries `selected` (published), `selection_status` (`SELECTED` awaiting
admission, `ADMITTED`, `DECLINED` at admission, `REPLACED_BY` (declined and replaced),
`SUPERSEDED` (declined `SUPERSEDED_BY_NEW_RESEARCH`) or `EXPIRED` (past its review deadline,
neither admitted nor declined); null when never published), `decline_code`, `replaced_by` (the
replacement's item key), `replacement_outcome` (`PUBLISHED` or `EXHAUSTED`, from the pick's
`RESEARCH_REPLACEMENT`), `replacement_for` (the item this pick replaced), `jev_rank`,
`ranking_status` (`RANKED`, `VETOED`, `NOT_RANKED`) and `ranking_reasons` (veto reasons, or the
NOT_RANKED code) from the cycle's ranking, `skip_reason`, `setup_state` and `setup_reason` (the
setup's reason, exit request or revocation reason). `last_run` adds `selection_policy` and
`ranking`: `{policy, k, counts: {RANKED, VETOED, NOT_RANKED}, complete}` of the cycle's
`RESEARCH_RANKING`, or null before the ranking and for other rules.

#### Report V3 (`AGENT_RESEARCH_REPORT_V3`)

Sent to `POST /api/v1/lab/research-reports`; V2 and legacy bodies keep their contract. The
envelope is checked as a whole:

| Field | Contract |
| --- | --- |
| `schema_version` | `AGENT_RESEARCH_REPORT_V3` |
| `report_id` | UUID; the cycle is `uuid5(agent, report_id)` as for V2, and an exact retry returns the stored answer |
| `generated_at`, `valid_until` | RFC 3339 with an offset; `generated_at < valid_until <= generated_at + 24 h`, and `valid_until <=` the next scheduled run after `run_slot` plus the grace (under `RESEARCH_SCHEDULE_V2`, the next full daily run after it) |
| `run_slot` | a scheduled run instant (any offset); the report may be generated at most the grace before it |
| `context_as_of` | the `as_of` of the context used; not after `generated_at` |
| `agent` | V2's agent block, required (the credential check is V2's) |
| `picks` | 1–30 (target 20) |
| `skipped` | 0–200 `{symbol, reason}` (reason 1–200 characters); a symbol at most once and never also picked |

Each pick:

| Field | Contract |
| --- | --- |
| `signal_id` | as V2; not reviewed |
| `symbol` | a universe symbol exactly (for example `BTC/USD`) |
| `kind` | `NEWS`, `CHART` or `BOTH` |
| `agent_current_price`, `agent_price_at` | positive decimal; RFC 3339, not after `generated_at` |
| `levels` | `entry_trigger`, `max_entry_price`, `stop`, `target`, positive decimals |
| `stated_reward_risk` | positive decimal |
| `valid_until` | optional; after `generated_at` and at most the report's |
| `reasoning` | `thesis` (≤ 1,000 characters), `why_now`, `why_these_levels`, `risks` (≤ 600 each), `invalidation` (≤ 400), none empty |
| `selection_rationale` | `AGENT_SELECTION_RATIONALE_V1`; every claim cites this pick's sources or bars |
| `sources` | 0–8 under V2's source rules; `NEWS` and `BOTH` need at least one |
| `technical_evidence` | `MUSE_OBSERVED_TECHNICALS_V1` with 20–64 completed bars; `CHART` and `BOTH` need it; `level_references` are optional (any of the four levels), must name a sent bar and are never compared with the levels |
| `agent_confidence` | 0–1; analytics only, never reviewed |

Intake checks each pick in this order and names it by the first failure; its siblings
proceed and it is recorded as `RESEARCH_ITEM_REJECTED_AT_INTAKE`:

| Code | Meaning (error-entry codes) |
| --- | --- |
| `INVALID_RESEARCH_ITEM` | schema or type error, with field paths |
| `PRICE_NOT_POSITIVE` | the only schema errors are prices at or below zero |
| `SYMBOL_NOT_IN_UNIVERSE` | not a coin of the current tradable universe |
| `NEWS_SOURCES_REQUIRED`, `TECHNICAL_EVIDENCE_REQUIRED` | the kind's sources or bars are missing (both listed when both are) |
| `CITATION_UNRESOLVED` | `CLAIM_SUPPORT_REQUIRED`, `RATIONALE_REFERENCE_UNKNOWN`, `DUPLICATE_RATIONALE_REFERENCE` |
| `PICK_VALIDITY_INVALID` | `PICK_VALID_UNTIL_AFTER_REPORT`, `PICK_VALID_UNTIL_NOT_AFTER_GENERATED`, `PICK_EXPIRED_AT_INTAKE` |
| `AGENT_PRICE_TIME_INVALID` | `AGENT_PRICE_AFTER_REPORT` |
| `INVALID_SOURCE_EVIDENCE`, `FUTURE_TECHNICAL_EVIDENCE` | a source or the bars retrieved after intake |
| `AGENT_IDENTITY_IN_PICK` | the agent ID, as a whole word in any case, in agent-written reviewed text (source excerpts and URLs, the symbol and schema literals are not screened) |
| `DOSSIER_OVER_BUDGET` | the reviewed state over 11,000 bytes or its rationale over 3,000, with `bytes` and `budget_bytes`; never truncated |

There is no geometry, reward-to-risk, stop-distance, price-grid or live-price check at intake.
The whole report is refused with nothing stored for: 422 `INVALID_MUSE_REPORT` with field
errors (schema codes, `AGENT_IDENTITY_REQUIRED`, `INVALID_REPORT_EXPIRY`,
`REPORT_VALIDITY_OVER_24_HOURS`, `REPORT_VALIDITY_AFTER_NEXT_RUN`, `RUN_SLOT_NOT_SCHEDULED`,
`RUN_SLOT_IN_FUTURE`, `CONTEXT_AFTER_REPORT`, `DUPLICATE_SKIPPED_SYMBOL`,
`SKIPPED_SYMBOL_ALSO_PICKED`); 422 `DUPLICATE_SIGNAL_IN_REPORT`, `DUPLICATE_SYMBOL_IN_REPORT`,
`SENSITIVE_EVIDENCE_REJECTED`, `RESEARCH_REPORT_STALE_OR_FUTURE`, `RESEARCH_REPORT_EXPIRED`,
`REPORT_IDEMPOTENCY_CONTENT_MISMATCH`; 422 with the first pick's code (`INVALID_MUSE_REPORT`
for a schema failure) and `item_results` when no pick is acceptable; 403
`AGENT_IDENTITY_MISMATCH`; 503 `RESEARCH_SCHEDULE_NOT_CONFIGURED`,
`RESEARCH_UNIVERSE_UNAVAILABLE` or `RESEARCH_V3_INTAKE_NOT_CONFIGURED`. An exact retry is
answered from the ledger without reading the universe.

The 202 response is V2's plus `report_schema_version`, `run_slot`, `skipped_count` and
`review_validity: PACKET_EXPIRY`; accepted `item_results` add the pick's `expires_at`. A pick's
packet expires at its `valid_until` (else the report's), bounded by `generated_at` plus
`MANAGED_REPORT_MAX_SECONDS` (which may be 86,400). Its review stays valid until that expiry
(`review_valid_until` = `expires_at`), not V2's `review_validity_seconds`.

Review dossier `REVIEW_DOSSIER_V3` (the packet `state`, sent to Jev unchanged): `market`
(`CRYPTO`), `symbol`, `kind`, `agent_current_price`, `agent_price_at`, `levels`,
`stated_reward_risk`, `valid_until` (the pick's, else the report's), `thesis`, `why_now`,
`why_these_levels`, `risks`, `disproof` (the pick's `invalidation`, under the name the
configured question sets and the admission and position code use), `sources` (canonical, with
`content_hash`), `technical_context` (`origin: EXTERNAL_RESEARCH_AGENT`, `execution_checks:
PENDING_INDEPENDENT_APP_VALIDATION` and, when sent, `observed_facts` with every submitted bar,
`bar_selection: ALL_SUBMITTED_BARS`, the observations hash and V1's code-computed metrics) and
`rationale` (`UNVERIFIED_PROPOSER_CLAIMS`, without the confidence). Values are in canonical
JSON form (a `+00:00` offset is written `Z`). It never contains the agent's identity, either
confidence or the signal ID. `RESEARCH_DOSSIER` records the manifest with `dossier_version:
REVIEW_DOSSIER_V3`, `field_map`, every bar ID and the omitted fields. `RESEARCH_STARTED` adds
`report_schema_version`, `dossier_version`, `review_validity`, `run_slot`, `context_as_of`,
`skipped_count`, `research_schedule` and `universe` (source, read time and symbols);
`RESEARCH_PACKET` adds the same version fields, `run_slot` and the analytics-only
`agent_confidence` outside `state`. Under the V2, B1 and B2 rules, V3 cycles are reviewed and
published exactly as V2 cycles; under `JEV_TOP_K_SELECTION_V1` they are ranked (below). Either way
their selections cross the same admission SQL, then the system check below.

#### Selection rule `JEV_TOP_K_SELECTION_V1` (2026-09-27, package selection-topk)

Active only when the owner sets `MANAGED_SELECTION_RULE=JEV_TOP_K_SELECTION_V1` (K from
`MANAGED_TOPK_SELECTION_JSON`, exactly `{"k": N}`, N 5–10, default 10; a quality floor refuses
startup). Its question sets, veto and uncertain labels, score and ranking are recorded in
REFERENCE-RULES.md. Output changes:

- **Intake**: under top-K only report V3 is accepted. A V2 or legacy body is refused whole with
  422 `{"detail": "REPORT_V3_REQUIRED"}` and nothing stored. `RESEARCH_STARTED.selection_rule`
  is `{selection_policy, k, question_sets, quality_policy, activation_event_id,
  activation_event_seq}`; startup's `RESEARCH_SELECTION_RULE_ACTIVATED` carries `k`,
  `question_sets` (`NEWS`, `CHART`, `BOTH` → `{version, template_hash}`), `quality_policy`,
  `quality_template_hash`, `quality_categories` and `uncertain_penalty`.
- **Reviews**: each pick gets one review with its kind's question set and one
  `MUSE_JEV_COMPARATIVE_QUALITY_V3` review; there are no evidence tasks, and
  `POST …/evidence` for a top-K item answers 422 `EVIDENCE_REVISION_NOT_APPLICABLE`.
  `RESEARCH_DECISION` has `disposition` `RANKABLE`, `VETOED`, `NOT_RANKED` or `EXPIRED`,
  `reason` (`TOPK_COMPONENTS_PASSED`, `TOPK_COMPONENTS_UNCERTAIN`, `TOPK_VETOED` or the failure
  code), `reasons`, `veto_reasons`, `uncertain`, `dissent`, `dissent_tied`,
  `question_set_version`, `pick_kind`, `selection_policy` and `evidence_tasks: []`.
  `RESEARCH_QUALITY` has `quality_policy` `MUSE_JEV_COMPARATIVE_QUALITY_V3`, `status` `SCORED`
  or `NOT_SCORED`, `reason`, `score` (a 0–100 four-decimal string), `category` (or null) and
  `answers`. Since `RESEARCH_REVIEW_BREAKER_GATE_V1` (below), both reviews start only while
  the Jev circuit breaker is closed.
- **`RESEARCH_RANKING`** (one per cycle, idempotency key `research:ranking:<cycle_id>`, in the
  cycle outputs): `{policy: "JEV_TOP_K_SELECTION_V1", cycle_id, run_slot, k, entries,
  quality_policy, uncertain_penalty, complete, counts: {RANKED, VETOED, NOT_RANKED}}`. Each entry
  `{item_key, revision, rank, adjusted_score, quality_score, quality_category, status:
  RANKED|VETOED|NOT_RANKED, veto_reasons, uncertain, dissent, receipt_id, quality_receipt_id,
  symbol, kind, question_set_version, dissent_tied, agent_rank, reason}`: `rank` 1..n for RANKED
  entries and null otherwise, scores as four-decimal strings (null when unknown), `reason` the
  code of a NOT_RANKED entry (a failed QUALITY review's code is prefixed `QUALITY_`;
  `REVIEW_DEADLINE_PASSED` when a pick had no review by its deadline). Entries list RANKED
  picks by rank, then VETOED, then NOT_RANKED picks in the agent's order. `complete` is false
  when the ranking was written at the review deadline with a pick unreviewed.
- **Publication**: ranks 1..K become `RESEARCH_SELECTED` in rank order. A pick whose symbol
  another cycle of the same `run_slot` already selected, or whose review expired, is recorded
  as `RESEARCH_SELECTION_SKIPPED` `{cycle_id, item_key, revision, symbol, rank, reason:
  DUPLICATE_SYMBOL_IN_RUN|REVIEW_EXPIRED, run_slot, selected_in_cycle_id, selected_event_seq,
  ranking_event_seq}` and the next-ranked pick takes its place. The selected packet keeps every
  report V3 field (`report_schema_version`, `run_slot`, `state`, `levels`, `review_validity`,
  …) and adds `question_set_version`, `rank` (Jev's), `agent_rank` (the pick's own item order,
  `rank` in V2/B1/B2 packets), `adjusted_score`, `quality_score`, `quality_category`,
  `uncertain`, `dissent`, `dissent_tied`, `quality_policy`, `quality_receipt_id`,
  `quality_receipt_ids`, `ranking_event_id`, `ranking_event_seq`, `k` and `replacement_for`
  (null for ranks 1..K; the declined entry's item key when `TOPK_REPLACEMENT_V1`, below,
  publishes a replacement through `ResearchCycle.publish_ranked`, which refuses an entry
  recorded as skipped, `TOPK_ENTRY_SKIPPED`, or a symbol another cycle of the run already
  selected, `DUPLICATE_SYMBOL_IN_RUN`).
- **Admission** (migration 021): new permanent refusal codes `TOPK_VETOED`,
  `TOPK_SCORE_MISMATCH` and `TOPK_RANKING_BINDING_FAILURE`; a top-K packet otherwise answers
  migration 013's binding codes and B1/B2's `SELECTION_RULE_NOT_ACTIVATED`,
  `SELECTION_QUESTION_POLICY_MISMATCH`, `QUALITY_RECEIPT_REQUIRED` and
  `QUALITY_RECEIPT_BINDING_FAILURE`.

#### Top-K reviews wait for a closed Jev breaker (`RESEARCH_REVIEW_BREAKER_GATE_V1`, 2026-09-29)

Fixture evidence only; the rule is in docs/REFERENCE-RULES.md. No new route. In a top-K
cycle (V1 or V2), a pick's kind review and QUALITY_V3 review start only while the Jev circuit
breaker, which the research reviews share with the trade reviews, is `CLOSED`.
- **While it is `OPEN` or `HALF_OPEN`**, the pick has no `RESEARCH_DECISION` or
  `RESEARCH_QUALITY` yet (its research-context `status` stays `AWAITING_REVIEW`). The cycle
  outputs carry one `RESEARCH_REVIEWS_DEFERRED` per breaker opening: `{cycle_id, gate:
  "RESEARCH_REVIEW_BREAKER_GATE_V1", breaker_state, breaker_epoch, blocked_until,
  runtime_scope, review_policy, waiting_picks, review_deadline}`, with idempotency key
  `research:<cycle_id>:reviews-deferred:<runtime_scope>:<breaker_epoch>`. `review_deadline` is
  the earliest ranking deadline of the waiting picks.
- **Once it closes**, the picks are reviewed, ranked and published as above.
- **A pick still unreviewed at its deadline** is NOT_RANKED `REVIEW_DEADLINE_PASSED` in a
  ranking with `complete: false`, as before.
- **A refusal that still happens** (the breaker opening between the check and the call) keeps
  its code, `CIRCUIT_OPEN` or `QUALITY_CIRCUIT_OPEN`, as before, and is not retried.

V2, B1 and B2 cycles are unchanged: a refused review is still `NEEDS_REVIEW` `CIRCUIT_OPEN`
with an evidence task.

#### System check at admission (`SYSTEM_CHECK_V1`, 2026-09-27)

Package system-check (fixture evidence only). No new
route and no request field: these are admission outcomes, read through `/outputs`,
`/cycles/{cycle_id}/outputs` (the `RESEARCH_*` events) and `/positions/{setup_id}/timeline`. A
V3 selection (packet `report_schema_version: AGENT_RESEARCH_REPORT_V3`) that passes every
existing admission check and the price grid is checked against the live Alpaca price before its
setup is created; V2 and legacy selections are unchanged.

| Code | Kind | When |
| --- | --- | --- |
| `STOP_DISTANCE_BELOW_MINIMUM` | permanent | (`max_entry_price` − `stop`) / `max_entry_price` < 0.02 (checked first, from the levels alone) |
| `PRICE_MISMATCH` | permanent | \|live mid − `agent_current_price`\| / `agent_current_price` > 0.05 |
| `STOP_ALREADY_HIT` | permanent | live bid ≤ `stop`, or the last trade ≤ `stop` |
| `BREAKOUT_NOT_ENABLED` | permanent | entry type `BREAKOUT`: `entry_trigger` above the mid by more than 0.2% of the mid |
| `SUPERSEDED_BY_NEW_RESEARCH` | permanent | a V3 selection of a later `run_slot` has been published (under `RESEARCH_SCHEDULE_V2`: of a later full run, or of a later run for the same symbol, `RESEARCH_RUN_SUPERSESSION_V2`) |
| `WITHDRAWN_BY_RESEARCH` | permanent | the proposing agent withdrew the selection ([`AGENT_RESEARCH_WITHDRAWAL_V1`](#post-apiv1labresearch-withdrawals-agent_research_withdrawal_v1)) |
| `LIVE_PRICE_UNAVAILABLE` | transient | no stream quote at most 5 s old and no usable REST latest quote; retried next tick |

The mid is (bid + ask) / 2. Entry type: `PULLBACK` (entry more than 0.2% below the mid),
`IMMEDIATE` (within ±0.2%), `BREAKOUT` (more than 0.2% above). Equality at 5% and 2% passes.
A permanent refusal declines the selection (`RESEARCH_ADMISSION_DECLINED`, never offered again)
and is final for its receipt: `SYSTEM_CHECK_REFUSED` (`reason`, `receipt_id`,
`selection_event_seq`, `cycle_id`, `item_key`, `symbol`, `run_slot`, `system_check`). The
runtime's `RUNTIME_ADMISSION_REFUSED` and `RESEARCH_ADMISSION_DECLINED` add the same
`system_check` object; a supersession adds `run_slot`, `superseded_by_run_slot` and
`supersession_rule` instead. An admitted V3 setup's state adds `system_check` and `entry_type`
(`PULLBACK` or `IMMEDIATE`); since package replacement both are among the allowlisted state
fields that `/setups`, `/positions` and `/results` list (other setups do not have them).

`system_check` object: `version` (`SYSTEM_CHECK_V1`), `checked_at`, `result` (`PASSED`,
`REFUSED` or `RETRY`), `code`, `checks` (`stop_distance`, `price_match`, `stop_not_hit`,
`entry_type_traded`: each `PASS`, `FAIL` or `NOT_EVALUATED`), `levels`,
`agent_current_price`, `agent_price_at`, `stop_distance_fraction`, `price_deviation_fraction`,
`entry_offset_fraction` (signed; fractions rounded to 12 places for reading, decisions exact),
`entry_type`, the thresholds (`minimum_stop_fraction` 0.02, `price_mismatch_fraction` 0.05,
`entry_type_band_fraction` 0.002), `live` (`quote_source` `ALPACA_STREAM` or
`ALPACA_REST_LATEST_QUOTE`, `bid`, `ask`, `mid`, `quote_at`, `read_at`, `quote_age_seconds`,
`last`, `last_at`, `last_trade_id`, `last_source`; `null` when not read) and, for `RETRY`,
`live_price_attempts` (per source: `STREAM_QUOTE_MISSING`, `STREAM_QUOTE_STALE`,
`STREAM_QUOTE_INVALID`; for the REST read the market source's own code, such as
`SCAN_HTTP_<status>`, `SCAN_CONNECTION_ERROR` or `MISSING_OR_INVALID_QUOTES`, else
`REST_QUOTE_INVALID`, `REST_QUOTE_IN_FUTURE`, `REST_RETRY_WAIT` (with `last_code`) or
`REST_READ_LIMIT_THIS_TICK`).

Run supersession (`RESEARCH_RUN_SUPERSESSION_V1`): once a V3 selection of a newer `run_slot`
is published, each older V3 setup still `WATCHING` becomes `INVALIDATED` with `revoked: true`
and `revocation_reason: SUPERSEDED_BY_NEW_RESEARCH` (one `REVOKE` with `run_slot`,
`superseded_by_run_slot` and `supersession_rule`), and each older, unexpired V3 selection not
yet admitted is declined with that reason. Open positions, working entries and V2 cycles are
untouched. Under `RESEARCH_SCHEDULE_V2`, `RESEARCH_RUN_SUPERSESSION_V2` applies instead
([below](#run-supersession-under-research_schedule_v2-research_run_supersession_v2)).

#### Replacement of declined top-K picks (`TOPK_REPLACEMENT_V1`, 2026-09-27)

Package replacement (fixture evidence only). No new
route or request field; the outcomes are read through `/cycles/{cycle_id}/outputs`, `/outputs`
and the research context. Only cycles under `JEV_TOP_K_SELECTION_V1` are affected.

When the runtime declines a top-K selection (`RESEARCH_ADMISSION_DECLINED`) for a permanent
refusal other than `SUPERSEDED_BY_NEW_RESEARCH`, the same ledger transaction publishes the
cycle's next-ranked pick as its replacement: the first entry of the cycle's `RESEARCH_RANKING`
that is RANKED, not yet selected and not recorded as skipped, and that `publish_ranked` accepts.
The replacement is an ordinary `RESEARCH_SELECTED` whose packet's `replacement_for` names the
declined item; it is admitted on a later tick through the normal path (admission SQL, the
price grid, the system check) and, if declined too, is replaced in turn. For top-K selections
the price-grid refusals `CRYPTO_LEVEL_OFF_PRICE_GRID` and `CRYPTO_PRECISION_UNAVAILABLE`
(final for their receipt) are declined too; other rules keep retrying them. Nothing is
replaced once the cycle or the declined pick has expired, or once a V3 selection of a later
`run_slot` has been published.

Each decision is one `RESEARCH_REPLACEMENT` (key
`research:<cycle_id>:<item_key>:<revision>:replacement`, in the cycle outputs):
`{cycle_id, replacement_rule: "TOPK_REPLACEMENT_V1", declined_item_key, declined_revision,
declined_symbol, declined_rank, declined_code, declined_selection_event_seq,
decline_event_seq, outcome: PUBLISHED|EXHAUSTED, code, replacement_item_key,
replacement_revision, replacement_symbol, replacement_rank, replacement_selection_event_seq,
passed_over, ranking_event_seq, k, run_slot}`. `code` is null for `PUBLISHED` and
`TOPK_RANKING_EXHAUSTED` for `EXHAUSTED` (every `replacement_*` field null). `passed_over`
lists `{item_key, rank, symbol, code}` for each entry `publish_ranked` refused on the way
(`DUPLICATE_SYMBOL_IN_RUN`, `REVIEW_EXPIRED`, `TOPK_ENTRY_SKIPPED`,
`TOPK_RANKING_BINDING_FAILURE`). There is at most one decision per declined pick. When a
decision is refused for a cycle-level reason (for example `RESEARCH_POLICY_CHANGED`), neither
the decline nor anything else is written, the runtime appends `RUNTIME_REPLACEMENT_FAULT`
(`runtime_id`, `cycle_id`, `item_key`, `selection_event_seq`, `declined_code`, `code`) once per
runtime, selection and code, and the next tick retries.

#### Replacement under the research loop (`TOPK_REPLACEMENT_V2`, 2026-09-29)

No new
route or field. For a top-K cycle accepted while the schedule was `RESEARCH_SCHEDULE_V2` (the
`research_schedule` its `RESEARCH_STARTED` recorded), `TOPK_REPLACEMENT_V1`'s decision applies
with two differences: the run is over only once a V3 selection of a later **daily (full)** run
is published, not any later run; and an entry whose symbol a later run has already selected is
passed over with code `SELECTED_BY_LATER_RUN` (that newer pick supersedes this run's under
`RESEARCH_RUN_SUPERSESSION_V2`). The decision records `replacement_rule:
"TOPK_REPLACEMENT_V2"`; its other fields are V1's. Cycles accepted under `RESEARCH_SCHEDULE_V1`
keep V1.

#### Crypto size and 24-hour hold (`JEV_MANAGED_RISK_V3`, `CRYPTO_24H_HOLD_V1`, 2026-09-27)

Package crypto-size-hold (fixture evidence only; migration 022). No new route or request field: the HTTP caller still
supplies proposed levels only, never a size, classification or holding choice.

- **Entry decisions under `JEV_MANAGED_RISK_V3` (crypto).** The decision `context.budget` is the
  entry's planned risk qty × (M − S) (the reservation's `budget` equals its `planned_risk`), and
  `context.binding_constraint` is `NOTIONAL` (the 10% equity slice), `RISK` (the 0.5% planned-risk
  cap) or `CAPITAL` (the cash available to crypto) when approved. The context adds `sizing`:
  `{method: "EQUITY_SLICE_RISK_CAPPED_V1", equity, notional_pct, risk_pct, min_stop_fraction,
  notional_cap, risk_cap, available_cash, increment, slice_qty, risk_qty, cash_qty, qty,
  notional, planned_risk, binding, unfilled_reserved_notional, min_order_size}` (decimals as
  strings). Decisions of every other policy and market have no `sizing` key and are unchanged.
  New refusal: `STOP_DISTANCE_BELOW_MINIMUM` (terminal; a stop closer than 2% below M).
  Capacity deferrals keep their codes (`CORRELATION_LIMIT` for a full sector,
  `MAX_OPEN_PLANNED_RISK`/`MARKET_RISK_CAP`, `INSUFFICIENT_BUYING_POWER`).
- **Setup state.** A report-V3 crypto setup's state adds `holding_policy`
  (`{policy_id: "CRYPTO_24H_HOLD_V1", max_hold_seconds: 86400, exit_reason: "HOLD_24H_EXIT"}`)
  and carries `crypto_day_policy`, `crypto_entry_deadline` and `crypto_flat_deadline` as `null`;
  `holding_policy` is among the allowlisted state fields that `/setups`, `/positions` and
  `/results` list (other setups do not have it). Once filled, `hard_exit_at` is 24 hours after
  the first buy fill. At that time `exit_requested`, the protection plan, the CANCEL and EXIT
  decisions' `reason` and the CLOSED state's `reason` are `HOLD_24H_EXIT` (every other setup keeps
  `TIME_EXIT`). From package day-review (2026-09-27) only the control arm (`FIXED_EXIT`) of a
  new report-V3 crypto setup records `CRYPTO_24H_HOLD_V1`; the maintained arm records
  `CRYPTO_24H_REVIEW_V1` ([below](#24-hour-reviews-and-early-exits-package-day-review-2026-09-27)).
- **A coin with an open trade.** A top-K selection refused `ACTIVE_SYMBOL_ALREADY_MANAGED` is
  declined (`RESEARCH_ADMISSION_DECLINED` with that `reason`) and replaced as above; the
  `RESEARCH_REPLACEMENT` names it as `declined_code`. V2, B1 and B2 selections keep waiting (one
  `RUNTIME_ADMISSION_REFUSED` per runtime and reason, no decline).
- **Classification.** With `ALPACA_CRYPTO_SECTORS_V1` a crypto classification row is sector
  `CRYPTO`, theme = the coin's built-in sector or `CRYPTO_OTHER`; the import's
  `CLASSIFICATION_IMPORTED` bodies name `classification_policy: ALPACA_CRYPTO_SECTORS_V1`,
  `unlisted_rule: CRYPTO_OTHER` and the superseded classification.

#### Crypto trigger version `CRYPTO_ALPACA_TRIGGER_V1` (2026-09-27)

Package crypto-trigger (fixture evidence only). No new
route or request field; the outcomes are read through `/setups`, `/outputs` and
`/positions/{setup_id}/timeline`. It applies to crypto setups admitted from a report-V3 packet
(any selection rule); every other setup's trigger and records are unchanged.

- **State.** Such a setup's WATCHING state records `trigger_version:
  "CRYPTO_ALPACA_TRIGGER_V1"`, listed by `/setups`, `/positions` and `/results` (other setups do
  not have it).
- **Rule.** Touch: a print at or below `entry_trigger`, or a fresh ask at or below it. Confirmed
  on a fresh ask at or below `max_entry_price` with (ask − bid) / mid at most 1% (exactly 1%
  passes); the entry order is a limit at `max_entry_price`. Fresh: received on the stream or read
  over REST at most 5 s ago, whatever the quote's own timestamp.
- **`INVALIDATED`** revision `reason`: `STOP_TRADED_BEFORE_TRIGGER` (a print at or below
  `stop`), `STOP_QUOTED_BEFORE_TRIGGER` (a fresh bid at or below `stop`), `PRICE_BEYOND_MAX_ENTRY`
  (a touch whose fresh ask is above `max_entry_price`, spread within 1%) or `DATA_FEED_FAILURE`;
  the revision adds `crypto_trigger` (below).
- **`CRYPTO_TRIGGER_WAIT`** (setup event; key `crypto-trigger-wait:<setup_id>:<reason>:<UTC
  minute>`, at most one per setup, reason and minute): `{reason, touch, trigger_version,
  crypto_trigger}`. `reason`: `SPREAD_ABOVE_MAXIMUM` (a touch with the spread above 1%),
  `FRESH_QUOTE_UNAVAILABLE` (a print touch with no fresh quote; `crypto_trigger.quote_attempts`
  lists the reader's attempts, as in the system check's `live_price_attempts`) or
  `TOUCH_NOT_CURRENT` (the touch aged past 5 s while the entry was being sized). The setup keeps
  WATCHING; no decision is written.
- **`TRIGGER_CONFIRMED`** body: the observation (a print touch: `trade_price`, `trade_at`,
  `trade_id`; a quote touch has none of these) with `bid`, `ask`, `quote_at` (the quote's own
  time), `quote_read_at`, `quote_source`, `feed_healthy`, `data_provider`, `data_feed`, the
  stream's `last_price`, `last_at`, `last_trade_id` when known, plus `trigger_version` and
  `crypto_trigger`.
- **Entry decision context** (`lab.managed_risk_decisions.context`): `quote_at` is the quote's
  read time (the gate requires it to be at most 5 s old at the claim); `quote_exchange_at`,
  `quote_read_at`, `quote_read_basis`, `quote_source` and `trigger_version` are added. Other
  setups' contexts are unchanged.

`crypto_trigger` object: `version` (`CRYPTO_ALPACA_TRIGGER_V1`), `evaluated_at`, `touch`
(`PRINT` or `QUOTE`: the entry touch, or the print or quote that reached the stop; else null),
`levels` (`entry_trigger`, `max_entry_price`, `stop`), `print` (`price`, `at`, `trade_id`,
`age_seconds`, or null), `bid`, `ask`, `spread_bps` ((ask − bid) / mid × 10,000, four places for
reading; decisions are exact), `max_spread_bps` (`"100"`), `quote_source` (`ALPACA_STREAM`,
`ALPACA_REST_LATEST_QUOTE`, or a direct caller's), `quote_at`, `quote_read_at`,
`quote_read_basis` (`STREAM_RECEIPT`, `REST_READ`, `RECORDED_READ_TIME`, or `QUOTE_TIMESTAMP`
when no read time was recorded and the quote's own time stands in), `quote_read_age_seconds`,
`quote_age_seconds`, `quote_max_age_seconds` (5), `quote_fresh`, `quote_code` (null,
`QUOTE_MISSING`, `QUOTE_INVALID`, `QUOTE_AFTER_READ` or `QUOTE_NOT_FRESH`), `last`, `last_at`,
`last_trade_id` (the stream's last trade, else the print) and, when present, `quote_attempts`.

#### Gap resume `CRYPTO_GAP_RESUME_V1` (2026-09-27)

Package gap-resume (fixture evidence only). No new route or
request field; read through `/setups`, `/outputs`, `/positions/{setup_id}/timeline` and the
status. It applies to the setups of `CRYPTO_ALPACA_TRIGGER_V1` admitted from now on; every other
setup is still revoked `DATA_FEED_FAILURE` on a market gap, with today's records.

- **State.** Such a setup's WATCHING state records `gap_resume_version:
  "CRYPTO_GAP_RESUME_V1"`, listed by `/setups`, `/positions` and `/results`.
- **`GAP_RESUME_PENDING`** (setup event, once per hold; key `gap-resume:pending:<setup_id>:
  <runtime_id>:<pending_since>`): `{version, runtime_id, market, symbol, gap_reason,
  gap_started_at, basis, open_gap_window_start, unconsumed_print_at, admitted_at, window_start,
  pending_since}`. `gap_reason` is the `RUNTIME_MARKET_GAP` reason
  (`RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION` at a startup); `basis.basis` is
  `RUNTIME_MARKET_GAP`, `PREVIOUS_RUNTIME_HEARTBEAT` (with `heartbeat_event_seq`,
  `heartbeat_runtime_id`, `as_of`, `observed`, `unobserved_since`),
  `NO_PREVIOUS_RUNTIME_HEARTBEAT`, `PREVIOUS_HEARTBEAT_WITHOUT_OBSERVATION_RECORD` or
  `LEDGER_UNAVAILABLE` (the last three: `gap_started_at` null, the window starts at admission).
- **Held.** While held no trigger is evaluated: its queued prints are consumed with
  `MARKET_PRINT_CONSUMED.reason` `GAP_CHECK_PENDING`.
- **Outcome** (one per hold): `GAP_RESUMED` (setup event, the evidence below; the setup keeps
  WATCHING); an `INVALIDATED` revision with `reason` `STOP_TRADED_DURING_GAP` and `gap_resume`;
  or `REVOKE` `{reason, gap_resume}` with `reason` `ENTRY_REACHED_DURING_GAP` or
  `DATA_FEED_FAILURE`, then the `INVALIDATED` revision with `revoked` and `revocation_reason`.

`gap_resume` evidence: `version`, `decision` (`RESUMED`, `STOP_TRADED_DURING_GAP`,
`ENTRY_REACHED_DURING_GAP`, `DATA_FEED_FAILURE`), `checked_at`, `window` (`start`, `end`,
`bars_start`, `bars_end`, `stream_back_at`, `bar_settle_seconds` 30), `levels` (`entry_trigger`,
`stop`), `bars` (`source` `ALPACA_CRYPTO_US_BARS_1MIN`, `timeframe` `1Min`, `count`,
`first_at`, `last_at`, `lowest_low`, `lowest_low_at`, `sha256` of the bars read, `issues` (the
source's codes), `problems`), `prints` (`count`, `lowest`, `lowest_at`, `lowest_trade_id`,
`lowest_event_seq`), `lowest` (the lowest traded price the check saw) and `gap` (`reason`,
`started_at`, `basis`, `pending_since`, `runtime_id`).

#### Pick shadow outcomes and results views (`PICK_SHADOW_OUTCOME_V1`, `UNCHANGED_PLAN_REPLAY_V1`, package results, 2026-09-27)

Fixture evidence only. Two new,
purely read-only capabilities on top of the existing routes above: no request field, no
broker call, no order and no risk authorization anywhere in this package.

**Every report-V3 pick, tracked** (`GET /api/v1/lab/cycles/{cycle_id}/picks`, no auth
change, not on the research-agent route list — same access as `/results`). Lists every
intake-accepted pick of the cycle (selected and admitted, selected then declined by the
system check, ranked but never selected, vetoed, not ranked), each with `symbol`, `kind`,
`levels`, `agent_current_price`/`agent_price_at`, `agent_id`/`agent_version`/`attribution`,
`selected`, `selection_status`, `decline_code`, `replacement_for`, `jev_rank`, `agent_rank`,
`ranking_status`, `ranking_reasons`, `skip_reason`, `setup_id` (once admitted), `arm` and
`rank_bucket` (`TOP_K`, `REPLACEMENT`, `NOT_SELECTED`, `VETOED`, `NOT_RANKED` or
`NO_RANKING_RULE` for a non-top-K cycle). `[]` for an unknown or non-report-V3 cycle.

**The shadow outcome** (`scripts/run_pick_shadow_outcomes.py`, an offline job, not a
runtime thread): for every such pick, once its own window (report `generated_at` through
its packet's own `expires_at`) plus a 24-hour hold has fully elapsed, fetches Alpaca's
*public* crypto minute bars (`v1beta3/crypto/us/bars`, no API key — see
`public_crypto_bars.py`) and appends one immutable `PICK_SHADOW_OUTCOME` event keyed to
the pick (idempotent per cycle/item/revision; safe to re-run or schedule daily). Method,
stated plainly as a 1-minute-bar approximation, never a fill, a quote or a tick
reconstruction: a touch is a bar's low at or below a level; the entry trigger is checked
before the stop, so a bar reaching the stop is read as "stop before a valid trigger" even
though it numerically also reached the (higher) trigger; a triggered pick is assumed
filled at the **max entry price** (a limit order there is never filled worse); the first
bar whose low is at or below the stop or whose high is at or above the target decides the
exit, a bar that reaches both in the same minute is resolved conservatively as the
**stop** and counted (`same_bar_ambiguous`); neither hit by 24 hours after the fill exits
there, priced at the next bar's open; bars ending before a required boundary answer
`DATA_INCOMPLETE`, never a guessed exit. R is computed at the max entry price (the
`official_r` convention, so no trade-size assumption is needed); `net_r` additionally
assumes Alpaca's crypto tier-1 **taker** fee (0.25%) on both legs
(`FEE_ASSUMPTION`/`ALPACA_CRYPTO_TIER1_TAKER_BOTH_LEGS_V1`, stated explicitly as an
assumption, never read from any account). A pick that actually traded keeps its own
verified `managed_measurement` figures, read fresh alongside the shadow ones, never
overwritten by them.

`GET /api/v1/lab/results/picks` lists recorded shadow outcomes (`pick`, `shadow`, and
`real` — the setup's current `managed_measurement`, `null` if never traded). `GET
/api/v1/lab/results/picks/aggregates` groups every recorded, non-`ENGINEERING_TEST` pick
outcome by any subset of `agent_id`, `rank_bucket`, `pick_kind`, `arm`, `selected` and
`continuation_decision` (default all six; `continuation_decision` is always `null` today
— a named hook for the 24-hour-review package, never fabricated data) and reports, per
group: `count`; `outcome_counts` (a tally of the shadow `outcome` classification);
`shadow_r_count`, `shadow_win_rate`, `mean_shadow_gross_r` and `mean_shadow_net_r` from
only the picks with a definitive (`data_complete`) shadow R; `real_count`,
`real_win_rate` and `mean_real_official_r` from only the subset that actually traded and
closed with a known `official_r`. A statistic with no known input is `null`, never a
default of zero — counts are always shown, never a bare mean.

**Unchanged-plan replay** (`unchanged_plan.py`, plan 4.6.8, "Measuring maintenance"): a pure
function, `replay_unchanged_plan`, that replays the same minute bars from a detected change
onward using the levels in force *before* that change (never the moved ones), to say what
the unchanged plan would have done and the R difference against the trade's real
`official_r`. Reads every change from three sources, unioned per setup in time order
(`setup_level_changes`): the pre-existing `JEV_MANAGED_EXITS_V1` amendment mechanism (a
`MANAGEMENT_PLAN_AUTHORIZED` event paired with the setup's `STATE` row immediately before
it); package maintenance's `CRYPTO_MAINTENANCE_V1` (one `MAINTENANCE_DECISION` per review,
`outcome: "APPLIED"` only — `REFUSED`/`HELD`/`FLAGGED`/`FAILED`/`DISCARDED` changed nothing
— read from the decision's own `levels_before`/`levels_after`, never the setup's current
state); and an agreed early exit under `EARLY_EXIT_FLAG_V1` (one `EXIT_FLAG_RESOLVED`
`outcome: "EXIT_AGREED"`, the original levels from its own `EXIT_FLAG_RAISED`'s recorded
`evidence.levels`). A widened level is never read as a raise; a raised-but-undecided or
declined flag is not a change.

`GET /api/v1/lab/results/maintenance` (optional `setup_id`, `after`, `limit=50`; needs an
injected `bar_reader` — 503 `MAINTENANCE_REPLAY_NOT_CONFIGURED` without one) lists every
replayed change (per trade with `setup_id`, across every trade with at least one without
it): `setup_id`, `symbol`, `arm`, `agent_id` and the replay itself (change kind, original
levels, the counterfactual exit, `unchanged_gross_r`/`unchanged_net_r`, `actual_r`,
`r_difference`, `data_complete`, `limitations`). `GET
/api/v1/lab/results/maintenance/aggregates` (optional `group_by`, subset of
`change_kind,arm,agent_id`, default `change_kind,arm`; `limit=500` setups considered) totals
by group: `count`; `r_difference_count`, `mean_r_difference` and `helped_rate` (the share
with a positive difference) from only the changes with a known `r_difference`; counts always
shown, never a bare mean. Both routes compute live (no persisted event, one bar fetch per
replayed change per call — acceptable at today's low review volume, not a design that scales
to a heavily-trafficked deployment without adding caching) through `create_managed_app`'s new
`clock` parameter (default the wall clock; a test or a future caller may inject another).

**`/results`** (no auth change; same static-shell pattern as `/`): today's picks
(rank/status/outcome, via `/cycles/{id}/picks`), open trades (`/positions`), closed trades
net of fees (`/results`) and both aggregate views, on the existing private read-only
dashboard app. No new framework; its own small inline stylesheet, no shared-file changes.

### Evidence task claims and material revisions

`POST /api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim` accepts exactly
`{"claimant":"worker-name","lease_seconds":30,"limit":10}` (maximum 2 KiB).
The claimant must be nonempty, lease duration is 1–300 seconds, and batch size is
bounded to 1–30. The response is `{"tasks":[...]}`. Tasks carry `task_id`, item/revision,
requested evidence and original expiry; claims are durably leased to prevent parallel
duplicate research. Expired, resolved and currently leased tasks are skipped. Claims and
evidence revisions are accepted only from the credential of the cycle's recorded agent
(otherwise 403 `AGENT_IDENTITY_MISMATCH`, checked before the body).

`POST /api/v1/lab/cycles/{cycle_id}/evidence` accepts at most 32 KiB with required
`item_key`, positive integer `revision`, `sources`, `thesis`, `disproof`, and
`economic_relationship`, plus optional `task_id` (UUID), `technical_facts` and, for
selection rule B2 items only, `selection_rationale`.
Current workers return the claimed `task_id`; omission remains supported for historical
callers. A supplied task must match the current item/revision and remain unresolved
and unexpired. New evidence increments the current revision by one and requires
materially new source content. Changing only annotations or replaying prior source
content cannot cause another model vote; the original deadline stays fixed.

Under selection rule B2 (`MUSE_JEV_RESEARCH_SELECTION_B2_V1`, migration 020) a revision may
replace the reviewed rationale with `selection_rationale`, an `AGENT_SELECTION_RATIONALE_V1`
block checked as at intake (schema, privacy, the 3,000-byte rationale and 11,000-byte state
budgets). Its claims may cite this revision's `sources` and the `bar_id`s of the report's own
`technical_evidence`; a newly cited bar is added to the reviewed observed facts. A B2
revision is material with new source content **or** with rationale claims and citations (kind,
text, cited sources and bars; not claim IDs, order, other rationale fields or
`agent_confidence`) that no earlier revision of the item had, so re-citing or trimming claims
answers `RECITE_FACTUAL_CLAIMS` with the same sources. The effective rationale, new or
carried, may cite only what the revised state shows. B2 refusals (422, detail code):
`MATERIAL_NEW_EVIDENCE_REQUIRED` (neither new sources nor new claims),
`SELECTION_RATIONALE_INVALID`, `CLAIM_SUPPORT_REQUIRED`, `RATIONALE_REFERENCE_UNKNOWN`,
`DUPLICATE_RATIONALE_REFERENCE`, `DOSSIER_OVER_BUDGET` and `SENSITIVE_EVIDENCE_REJECTED`.
For V2 and B1 items nothing changes: `selection_rationale` is refused
`RATIONALE_REVISION_NOT_APPLICABLE` and new source content is required
(`MATERIAL_NEW_SOURCE_EVIDENCE_REQUIRED`). Items of a `JEV_TOP_K_SELECTION_V1` cycle take no
revision at all (422 `EVIDENCE_REVISION_NOT_APPLICABLE`): each pick is reviewed once.

Decisions of a B2 cycle carry `selection_policy`, `question_set_version`
(`SKEPTIC_QUESTIONS_V2`), `dissent` (the verdict, never blocking), `dissent_tied`, `reasons`
and `shadow: NOT_APPLICABLE_QUESTION_SET`. Their objection codes and task requirements (each
B2 requirement has `required_fields` and `instructions`):

| Objection codes | Requirement (`task`) |
| --- | --- |
| `NEWS_STALE_YES`, `ALREADY_PRICED_HIGH` and their `_INSUFFICIENT`/`_TIED` | `FETCH_PRIOR_DISCLOSURES` (unchanged) |
| `MECHANISM_CONTRADICTED_YES`, `_INSUFFICIENT`, `_TIED` | `RESOLVE_CONTRADICTION` |
| `INFERENCE_LABELLED_NO`, `_INSUFFICIENT`, `_TIED` | `LABEL_INFERENCE` |
| `FACTUAL_CLAIMS_SUPPORTED_PARTIALLY_SUPPORTED`, `_UNSUPPORTED`, `_INSUFFICIENT`, `_TIED` | `RECITE_FACTUAL_CLAIMS`: every number and fact in a claim must be stated by its cited excerpt or bar; cite additional bars or sources, or trim the claim |
| `RATIONALE_REQUIRED` (no rationale; never sent to Jev) | `SUPPLY_SELECTION_RATIONALE` |
| any other code | `RESOLVE_RESEARCH_OBJECTION` (unchanged) |

An approved B2 decision's reason is `B2_COMPONENTS_PASSED`; others are `UNRESOLVED_EVIDENCE`
or `COMPONENTS_NOT_PASSED` with the codes above in `reasons`.

Optional `technical_facts` contains `observed_at` (aware and not future), nonempty
`timeframe`, `summary` (1–2,000 characters), `facts` (1–32 strings, each 1–300 characters),
and `source_ids` referencing this revision's sources. These are Muse-supplied claims,
retained separately from code-computed observations. A refreshed fact block supersedes
old observed-fact context; without it, retained observations are marked not refreshed.

The response contains `status: EVIDENCE_RECORDED`, `cycle_id`, `revision`,
`evidence_hash`, original `expires_at`, and `trade_authorized: false`. The task resolution
is appended atomically with the new packet. An exact retry of an already resolved task
and its current revision returns the retained packet; different retry content fails.

### Position news and monitoring readback

`GET /api/v1/lab/positions/{setup_id}/news` returns the current lifecycle, news revision,
original thesis, retained sources, state and hard exit time. `POST` at the same path
accepts exactly `news_id` (UUID), `lifecycle_id` (UUID), `expected_news_revision`, and
`sources`, at most 32 KiB. It requires an open matching lifecycle, its current revision,
materially new source content, and time remaining before the fixed hard exit. Exact
`news_id` retries return the original receipt; changed content is rejected. Only the
credential of the setup's recorded agent may post (otherwise 403
`AGENT_IDENTITY_MISMATCH`); the legacy credential posts for unattributed setups.
The posting agent's ID may not appear, as a whole word in any case, in an agent-written
source field (the source ID and times; excerpts and URLs are third-party text): 422
`AGENT_IDENTITY_IN_NEWS`, with nothing stored. Position sources reach Jev's maintenance
and 24-hour-review contexts, and Jev never sees the agent.

The news receipt contains `POSITION_NEWS_RECORDED`, `news_revision`, `evidence_hash`,
`idempotent_replay`, `trade_authorized: false`, and `position_modified: false`.
Subsequent app-owned monitoring retains original and new evidence, actual position and
protection, management history, and code-computed progress, distances, remaining time
and risk. Versioned scheduling can review new bars, material news, material position or
protection changes, and near-target progress. Model receipt recovery does not reroll a
vote; an existing target or time exit remains effective while a review is pending.

### Read routes and pagination

All cursors are audit event sequences, not array offsets. Persist a cursor only after
processing its page. Decimal values are serialized as strings; unknown measurements
remain `null`. The recent list routes and historical routes use different directions:

| GET route | Query and continuation |
| --- | --- |
| `/api/v1/lab/outputs` | `after=0`, `limit=100` (1–1,000); ascending events, next `after=next_cursor`; an empty page preserves the cursor. A research-agent credential never receives the learning record kinds (`MARKET_OUTLOOK`, `POST_MORTEM`, `MARKET_REALITY`, `DAILY_SCORECARD`, `WEEKLY_REVIEW`, `UNCHANGED_PLAN_REPLAY`, `DAY_REVIEW_DECISION_REPLAY`; package learning-app): the query leaves them out, so its pages and cursors cover only the other events and paging never stalls on them (the single legacy token without role tokens keeps its full access) |
| `/api/v1/lab/research-context` | No cursor: one read-only snapshot for the caller (see [the research context](#get-apiv1labresearch-context)) |
| `/api/v1/lab/cycles` | Most recent 50 research cycles; historical scan diagnostics are identified separately from external Muse reports |
| `/api/v1/lab/cycles/{cycle_id}/outputs` | `after=0`, `limit=100` (1–1,000); ascending research packets, dossier manifests, intake rejections, decisions, receipts, tasks and selection events; empty page preserves cursor |
| `/api/v1/lab/cycles/{cycle_id}/picks` | No cursor: every report-V3 pick of this cycle (any selection rule) with its current rank/status (see [pick shadow outcomes](#pick-shadow-outcomes-and-results-views-pick_shadow_outcome_v1-unchanged_plan_replay_v1-package-results-2026-09-27)); `[]` for an unknown or non-report-V3 cycle |
| `/api/v1/lab/setups` | Optional positive `before`, `limit=200` (1–1,000); descending setup-admission sequence; next `before=next_cursor`, `null` on an empty page |
| `/api/v1/lab/positions` | Same descending `before` contract, open positions only; cursor advances over the underlying page even if filtered items are empty |
| `/api/v1/lab/results` | Same descending `before` contract, closed results and audited measurements; cursor advances over the underlying page even if filtered items are empty |
| `/api/v1/lab/history/results` | `after=0`, `limit=100` (1–500); all historical closed managed setups in ascending current-state event sequence; next `after=next_cursor`, `null` at end |
| `/api/v1/lab/analytics/research` | `after=0`, `limit=100` (1–500); ascending research-start sequence, latest revision dispositions, selected/admitted/entered counts, reason counts and receipt latency, each cycle's agent attribution and intake-rejection count, plus `agent_groups` for the page; empty page preserves cursor |
| `/api/v1/lab/analytics/daily` | Optional inclusive `start` and `end` dates (`YYYY-MM-DD`); all matching daily aggregates, no cursor or recent-page cap |
| `/api/v1/lab/results/aggregates` | Optional `group_by` (comma-separated subset of `market,arm,selection_policy,question_set_version,agent_id`; default all five); one row per combination present among closed setups, no cursor |
| `/api/v1/lab/results/picks` | `after=0`, `limit=100` (1–500); ascending recorded `PICK_SHADOW_OUTCOME_V1` events, real measurement joined in when the pick traded; empty page preserves cursor |
| `/api/v1/lab/results/picks/aggregates` | Optional `group_by` (comma-separated subset of `agent_id,rank_bucket,pick_kind,arm,selected,continuation_decision`; default all six); one row per combination present among recorded pick outcomes, no cursor |
| `/api/v1/lab/results/maintenance` | Optional `setup_id` (one trade), `after=0`, `limit=50` (1–500); ascending setups with a replayed change, computed live (503 without a configured `bar_reader`); next `before=next_cursor` over setups, not rows |
| `/api/v1/lab/results/maintenance/aggregates` | Optional `group_by` (comma-separated subset of `change_kind,arm,agent_id`; default `change_kind,arm`), `limit=500` setups considered; computed live (503 without a configured `bar_reader`), no cursor |
| `/api/v1/lab/positions/{setup_id}/timeline` | `after=0`, `limit=100` (1–1,000); ascending setup events plus its originating cycle's research events; empty page preserves cursor |
| `/api/v1/lab/positions/{setup_id}/measurement` | Current fill/cost measurement and execution-quality diagnostics for one setup |

Agent attribution (plan 1.3/1.8, read-only, no new route): `/cycles`, `/setups`,
`/positions`, `/results` and `/history/results` items carry `attribution`
(`AGENT_ATTRIBUTED` or `LEGACY_UNATTRIBUTED`), `agent_id` and `agent_version` (both
`null` for legacy). Each `/analytics/research` item adds `guidelines_version` and
`guidelines_sha256`. Its `agent_groups` (scope `PAGE`) group that page's cycles by
`agent_id`, `agent_version` and `guidelines_sha256` with additive counts (cycles,
contenders, intake rejections, selected, admitted, entered, review receipts, packets with
a rationale, plus market, outcome and reason counters), so pages can be summed; legacy
cycles form one bucket with null agent fields. `rationale_claim_support_rate` is `null`
with `rationale_claim_support_note`: no Jev answer judges rationale claims under
`SKEPTIC_QUESTIONS_V1` (`unsupported_inference` covers the whole packet); the claim-level
answer arrives with approval rule V3 (plan 1.7). `SKEPTIC_QUESTIONS_V2`
(`factual_claims_supported`, rule B2 cycles only) is such an answer; the funnel does not read
it yet, so the rate stays `null`.

Historical results and daily aggregates carry `baseline_included: false` and
`PAPER_ENGINEERING_OBSERVATIONS_NOT_VALIDATED_STRATEGY_PERFORMANCE`. Daily rows group
by first entry-fill date (otherwise admission date) in `America/New_York`, market,
cohort and strategy. They expose setup/entered/closed counts, state and terminal reason
counts, failure-reason event counts, gross/net known counts and known-subset sums.
Daily `net_pnl_usd` stays null if any closed trade's net is unknown; it is not silently
reported as the sum of the known subset. Historical list membership and latest outcomes
can change as work completes; these reads are not immutable snapshot exports.
Operator `ENGINEERING_TEST` setups (plan 0.10) have no HTTP route: they are enrolled only
with `python -m catalyst_lab.managed_engineering`, carry `engineering: true` on setup,
position, result and measurement payloads, and never enter a daily item
(`/analytics/daily` reports them as `engineering_count`).

For closed `/results`, `fees_complete` equals `measurement.fees_verified`, including
valid append-only operator cost corrections bound to exact original fills. Net P&L
requires known fees and reconciled inventory. A base-asset fee must have explicit quantity
and USD evidence; its inventory loss is already reflected in cash flows and is not
subtracted again as a cash fee. Corrections never rewrite fills, change execution quantity,
or arrive through a Muse mutation route. Measurements expose cost evidence and correction
IDs, sampled-excursion coverage, and missing-data limitations. Execution-quality timing
and slippage diagnostics are descriptive observations, not proof of strategy performance.

Package fees-net-r (2026-09-26, fixture evidence only):
fee evidence can also come from Alpaca's own `CFEE`/`FEE` account activities, read GET-only
and matched to a fill by broker order id and time (source `ALPACA_PAPER_ACTIVITY` on the
correction, alongside the existing operator `BROKER_ACTIVITY`/`BROKER_STATEMENT`/
`LAB_FIXTURE`). Alpaca's crypto rows carry no order id; from 2026-09-29 such a row binds by
its amount to exactly one fill (`ALPACA_FEE_MATCH_V2`, docs/REFERENCE-RULES.md), and a
row that fits more than one fill is counted `ambiguous`, never guessed. Every measurement (`/results`, `/history/results`,
`/positions/{setup_id}/measurement`) now also carries `net_r` (net P&L over the
reservation's authorized-quantity risk, the net analogue of the existing `test_r`) and
`official_r` (the owner's R5 ruling: net P&L over the *filled* quantity times admitted
max entry minus admitted initial stop, `official_r_method:
"MANAGED_OFFICIAL_R_PLANNED_FILLED_V1"`, plus `planned_filled_risk` for that denominator).
`test_r` is unchanged and now carries `test_r_label:
"ENGINEERING_ALTERNATIVE_AUTHORIZED_QUANTITY_DENOMINATOR"`. `/results` and
`/history/results` items flatten all three (`test_r`, `net_r`, `official_r`) alongside
the existing `net_pnl_usd`. `GET /api/v1/lab/results/aggregates` groups every closed,
non-`ENGINEERING_TEST` setup by any subset of `market`, `arm`, `selection_policy`,
`question_set_version` and `agent_id` (default all five) and reports, per group: `count`;
`win_rate` and `mean_gross_r` (from setups with known gross P&L; no fee evidence needed);
`mean_net_r`, `mean_official_r` and `total_fees_usd` (from setups with verified fees whose
cost evidence carries no `LAB_FIXTURE` source — a fixture-tainted closed setup is still
counted and separately reported in `fixture_tainted_count`, never silently counted as
verified real-account performance). A statistic with no known input is `null`, never a
default of zero; `engineering_count` and `engineering_scope` match every other analytics
view.

### Runtime metadata

`GET /api/v1/lab/status` includes cohort/supervision plus allowlisted runtime diagnostics:
`worker_state`, `mode`, `entry_ready`, `research_healthy`, `account_safety_healthy`,
`last_cycle_at`, `last_reconciliation_at` (from package learning-app, 2026-09-28: when the last
reconciliation pass completed clean, never cleared while a pass runs or after a failure, so it
only ages; `entry_ready` still requires a current clean reconciliation), `last_protection_tick`,
`trade_stream_connected`, `market_feed_connected`, `market_streams`,
`required_market_streams`, `market_data_authority`, `management_review_enabled`,
`executor_ownership`, `error_code`, `schema_version`, `code_version`, and
`configuration_hash` when provided by the runtime. Metadata identifies loaded code,
schema and effective configuration without exposing credentials. Readiness and ownership
must be assessed from current authenticated status, not inferred from HTTP liveness or
this source contract. Serving these routes alone starts no account executor.

`trade_maintenance` (package maintenance, 2026-09-27; `null` without the maintenance
component): `policy_id` (the version admission records: `CRYPTO_MAINTENANCE_V3` from package
jev-budget, `CRYPTO_MAINTENANCE_V2` from package answer-rules, `CRYPTO_MAINTENANCE_V1` before),
`open_trades_by_policy` (open maintained trades
by the version each recorded; package answer-rules), `management_reviews`, `open_trades`,
`pending_exit_flags`, `oldest_exit_flag_raised_at`, `exit_flags` (`flag_id`, `setup_id`,
`side`, `raised_at`), `failing_reviews` (`setup_id`, `symbol`, `code`, `decided_at` of every
maintained open trade whose last review failed) and `last_pass_at`; `{"available": false}` when
its read fails. The watchdog raises `EXIT_FLAG_PENDING`, `MAINTENANCE_REVIEW_FAILING` and
`MAINTENANCE_STATUS_UNAVAILABLE` from it.

`day_reviews` (package day-review, 2026-09-27; `null` without the day-review component):
`policy_id` (the version admission records: `CRYPTO_WINDOW_REVIEW_V1` while
`MANAGED_CRYPTO_WINDOW_JSON` is set, package review-window; otherwise `CRYPTO_24H_REVIEW_V2` from
package answer-rules, `CRYPTO_24H_REVIEW_V1` before), `window_minutes` (package review-window:
the setting's window, `null` without it), `open_trades_by_policy` (package answer-rules),
`early_exit_policy_id` (`EARLY_EXIT_AGREEMENT_V1`), `management_reviews`, `open_trades` (open
trades under either review version), `reviews_in_progress`
(`setup_id`, `symbol`, `review_id`, `review_number`, `review_at`, `phase`: `AWAITING_AGENT`,
`AWAITING_JEV`, `DISCUSSION`, `AWAITING_JEV_FINAL` or `DECIDING`), `failing_reviews` (`setup_id`,
`symbol`, `code`, `recorded_at` of every undecided review whose latest Jev attempt failed),
`pending_exit_flags` (`{JEV, AGENT}` counts) and `last_pass_at`; `{"available": false}` when its
read fails. The watchdog raises `DAY_REVIEW_JEV_FAILING` and `DAY_REVIEW_STATUS_UNAVAILABLE`.

`gap_resume` (package gap-resume, 2026-09-27): `version` (`CRYPTO_GAP_RESUME_V1`), `as_of` (the
instant the section was taken, under the runtime lock), `markets` (`US` and `CRYPTO`, each
`observed` and `unobserved_since`, null when unknown), `pending_count`, `oldest_pending_since`,
`overdue_after_seconds` (300) and `pending` (at most 50 held setups, oldest first: `setup_id`,
`market`, `symbol`, `gap_reason`, `pending_since`, `window_start`, `recorded`, `stream_back_at`,
`due_at`). The watchdog raises `GAP_RESUME_CHECK_OVERDUE` when the oldest held setup has waited
more than 300 s, and `GAP_RESUME_STATUS_UNAVAILABLE` for an unreadable section.

`jev_budget` (package jev-budget, 2026-09-28; `null` without a spend guard, which a runtime
builds whenever `JEV_MONTHLY_BUDGET_USD` is set and always in railway mode): `version`
(`JEV_SPEND_GUARD_V1`), `available`, `tier` (`NORMAL`, `THROTTLED`, `TIGHT` or `EXHAUSTED`;
`UNAVAILABLE` with `available: false` and a `code` when the meter cannot be read),
`routine_review_seconds` (60, 300, 900 or null), `budget_usd`, `estimate`
(`monthly_budget_usd`, `price_per_million_input_tokens_usd`, `bytes_per_token`),
`evaluated_at`, and when available `rule`, `tier_since`, `tier_event_seq`, `thresholds_usd`
(`throttle_above_usd`, `normal_at_or_below_usd`, `tight_from_usd`, `exhausted_from_usd`),
`month` (New York calendar month), `month_to_date_usd`, `today_usd`, `projection_usd` (month-end
at the last 24 hours' pace), `projection_per_minute_usd` (month-end if every guarded trade were
reviewed every minute; decides the tier), `month_calls`, `month_request_bytes`,
`month_estimated_input_tokens`, `month_by_kind_usd` (`SELECTION`, `QUALITY`, `MAINTENANCE`,
`DAY_REVIEW`, `EARLY_EXIT`, `HEALTH_PROBE`, `OTHER`), `last_24h_request_bytes`,
`last_24h_per_minute_equivalent_bytes`, `days_left_in_month` and `receipt_watermark`. USD amounts
are decimal strings rounded to 0.0001 for display. The watchdog raises `JEV_BUDGET_THROTTLED`,
`JEV_BUDGET_TIGHT` or `JEV_BUDGET_EXHAUSTED` for 30 minutes after `tier_since`, and
`JEV_BUDGET_STATUS_UNAVAILABLE` for an unavailable or unreadable section.

### Trade maintenance readback (`CRYPTO_MAINTENANCE_V1`, package maintenance, 2026-09-27)

Rules: [REFERENCE-RULES.md](REFERENCE-RULES.md). No route and no request field was
added; a maintained trade is read through the existing routes. `/setups`, `/positions` and
`/results` states list `maintenance_policy` and `partial_entry_policy` (recorded at admission for
a report-V3 crypto setup in the `JEV_MANAGED` arm; absent for every other setup),
`stop_replace` (a raised stop not yet carried by the broker's stop-limit: `change_id`, `path`
`PATCH_REPLACE` or `CANCEL_THEN_PLACE`, `from_stop`, `to_stop`, `applied_at`, and
`patch_refused` after a fallback), `partial_entry_retained_at` and `partial_entry_cancel`. A
setup's `latest_management` is its newest `MANAGED_JEV_JUDGMENT`, which for a maintained trade
adds `outcome` and `policy_id`. The outputs and timeline routes carry the maintenance events:
`MAINTENANCE_OPENED`, `MAINTENANCE_ENTRY_COMPLETED`, `PARTIAL_ENTRY_RETAINED`,
`PARTIAL_ENTRY_REMAINDER_CANCEL`, `PARTIAL_ENTRY_PROTECTION_REFUSED`, `MAINTENANCE_TRIGGER`,
`MAINTENANCE_BTC_SHOCK` (no setup), `POSITION_REVIEW_REQUEST` (context
`JEV_MANAGED_POSITION_CONTEXT_V4`, or `_V5` under `CRYPTO_MAINTENANCE_V2`), `MAINTENANCE_DECISION`
(one per review: `outcome` APPLIED, REFUSED, HELD, FLAGGED, FAILED or DISCARDED, `code`, old and
new levels, options, the quote at the decision, receipts, answer and decision times; under V2
also `answer_rule` and `option_use`, and HELD with code `NO_USABLE_OPTION` for a raise whose
option answers could not be used), `MAINTENANCE_REVIEW_SKIPPED` (V2: a minute that completed
while the trade's review was in flight, code `REVIEW_SKIPPED_IN_FLIGHT`; V3: a routine bar of its
tier), `STOP_REPLACED`,
`STOP_REPLACE_FALLBACK`,
`STOP_CROSSED_DURING_REPLACE`, `EXIT_FLAG_RAISED` and `EXIT_FLAG_RESOLVED`. Agent news posted to
`POST /api/v1/lab/positions/{setup_id}/news` for a maintained trade triggers a review at once
(at most one a minute). A research agent never supplies a stop, target or option: the code
computes them and Jev chooses.

Under `CRYPTO_MAINTENANCE_V3` (package jev-budget, 2026-09-28; rules in
[REFERENCE-RULES.md](REFERENCE-RULES.md)) the requests, context, questions and decisions
are V2's; a `POSITION_REVIEW_REQUEST`'s trigger (and its Jev identity) adds `spend_guard`
(`version`, `tier`, `tier_event_seq`, `review_bar_seconds`, `routine_weight`), its routine
reason is `BAR_1M`, `BAR_5M` or `BAR_15M` by tier, and a trade the budget does not allow to be
reviewed gets one `POSITION_REVIEW_SKIPPED` per lifecycle and episode (`reason`
`JEV_BUDGET_EXHAUSTED` or `JEV_BUDGET_UNAVAILABLE`, `policy_id`, `spend_guard`, `code`). The
outputs route also carries `JEV_BUDGET_TIER_CHANGED` and `JEV_SPEND_GUARD_CONFIGURED` (no setup).

### 24-hour reviews and early exits (package day-review, 2026-09-27)

Rules and versions: [REFERENCE-RULES.md](REFERENCE-RULES.md) (plan
`docs/CRYPTO-AGENT-LOOP.md` 4.6.3 and 4.6.4). Scope: an open trade whose state records
`holding_policy` `CRYPTO_24H_REVIEW_V1`, (from package answer-rules) `CRYPTO_24H_REVIEW_V2` or
(from package review-window, while `MANAGED_CRYPTO_WINDOW_JSON` is set) `CRYPTO_WINDOW_REVIEW_V1`,
i.e. a report-V3 crypto setup admitted in the maintained (`JEV_MANAGED`) arm from this version
on; the routes are the same under all three. Under `CRYPTO_WINDOW_REVIEW_V1` T comes every
recorded window (4 hours in the deploy example) instead of every 24 hours, and the stored
request adds `holding_window_seconds`. The control arm (`CRYPTO_24H_HOLD_V1`, exit at 24 hours;
`CRYPTO_WINDOW_HOLD_V1`, exit at the window) and every older setup have no reviews: its routes
list nothing for them and refuse their flags. Every route below is on the research agents'
route list: a research credential,
and the legacy credential for agent `muse`, sees and answers only requests addressed to its own
agent (403 `AGENT_IDENTITY_MISMATCH` otherwise); the status credential may `GET` (it sees
nothing); the operator credential is 403. Without the service the routes answer 503
`TRADE_REVIEWS_NOT_CONFIGURED`. Nothing here authorizes, sizes or places anything
(`trade_authorized: false`); decisions are made by the runtime, and every broker request that
follows (a replaced stop, a cancel, a market sell) needs its own exact one-use five-second
authorization.

Who is asked: the agent that proposed the trade while its credential is configured, else the
configured research agent whose report arrived last (`addressee.basis` `PROPOSING_AGENT` or
`ACTIVE_RESEARCH_AGENT`), else nobody (`NO_ACTIVE_AGENT`: Jev decides alone and a Jev flag ends
unanswered at once).

**`GET /api/v1/lab/reviews`.** The caller's pending requests, cheap to poll (one read of the open
trades under the version and their review events): `{as_of, poll_hint_seconds: 60,
request_lead_seconds: 1800, items, trade_authorized: false}`, `items` ordered by `answer_due_at`.
The request for a review appears 30 minutes before T, so an agent polling at least every five
minutes has at least 25 minutes to answer. Items:

| `kind` | Fields |
| --- | --- |
| `DAY_REVIEW` | `review_id`, `setup_id`, `symbol`, `lifecycle_id`, `round` (`FIRST` until T; `DISCUSSION` for 15 minutes after Jev disagreed), `answer_due_at`, `review_at` (T), `review_number` (1 at the first T), `answer_route`, `answer_schema` (`AGENT_REVIEW_ANSWER_V1`), `request` (the stored request: `trade` with entry, quantity, stop, target, original levels, risk per coin, opened time, hours in the trade, bid, ask, quote time, unrealized P&L and P&L in R at the request; `level_changes`; `news_since_entry` (every source of the trade's position news); `options` (the stop and target options code computed at the request, or `null`); `original_pick` (kind, the agent's price and time, levels, stated reward-to-risk, thesis, why now, why these levels, risks, disproof)); in round `DISCUSSION` also `your_first_answer` and `jev_first_answer` (`answers`: each question's `choice` and `top_p`; `meanings`: each choice's fixed text; `chosen`: the option records Jev chose, under `CRYPTO_24H_REVIEW_V2` only the ones code would raise to: a unique most probable offered option) |
| `EXIT_FLAG` | `flag_id`, `setup_id`, `symbol`, `lifecycle_id`, `raised_by` (`JEV`), `raised_at`, `answer_due_at` (15 minutes after the flag), `answer_route`, `answer_schema`, `jev_reasons` (`trade_reason`, `answers`, `trigger_reasons`; on a `CRYPTO_MAINTENANCE_V5` trade also `question` — `invalidation_met` or `news_contradicts` — and `if_unanswered`: `EXIT` means no answer by `answer_due_at` sells the trade, `KEEP` keeps it), `trade` (the levels, quote, entry and risk per coin at the flag) |

**`POST /api/v1/lab/reviews/{review_id}/answer`** (at most 32 KiB) accepts an
`AGENT_REVIEW_ANSWER_V1` for the round open now:

| Field | Contract |
| --- | --- |
| `schema_version` | `AGENT_REVIEW_ANSWER_V1` |
| `answer_id` | UUID; an exact retry returns the stored answer (`idempotent_replay: true`), changed content under it is 409 `IDEMPOTENCY_CONTENT_MISMATCH` |
| `decision` | `CONTINUE` or `EXIT` |
| `what_changed`, `next_24h`, `proves_wrong` | 1-600, 1-600 and 1-400 characters: what changed, what the agent expects in the next 24 hours, what would prove its decision wrong |
| `suggested_stop`, `suggested_target` | optional decimal strings, `CONTINUE` only; shown to Jev, who still chooses only among the code's options |
| `sources` | optional, 0-8, the position-news source format |

No other field (no confidence). The texts must not name the agent (422
`AGENT_IDENTITY_IN_ANSWER`), and the answer is refused like evidence for credentials, e-mail or
0x addresses (`SENSITIVE_EVIDENCE_REJECTED`) and over 12,000 bytes of JSON
(`EVIDENCE_TOO_LONG`). Refusals: 404 `REVIEW_NOT_FOUND` (no open trade has it); 409
`REVIEW_CLOSED` (decided, discarded or an exit under way), `REVIEW_ANSWER_WINDOW_CLOSED` (after T,
or after the discussion's 15 minutes) or `REVIEW_ALREADY_ANSWERED`; 422 for a malformed answer
(`REVIEW_ANSWER_FIELDS_INVALID`, `REVIEW_ANSWER_SCHEMA_REQUIRED`, `REVIEW_DECISION_INVALID`,
`ANSWER_ID_INVALID`, `ANSWER_TEXT_INVALID`, `SUGGESTED_LEVEL_INVALID`,
`SUGGESTED_LEVELS_NOT_ALLOWED`, source codes). Receipt: `status: REVIEW_ANSWER_RECORDED`,
`review_id`, `round`, `decision`, `received_at`, `idempotent_replay`, `trade_authorized: false`,
`position_modified: false`.

**`POST /api/v1/lab/exit-flags/{flag_id}/answer`** answers a Jev flag with an
`AGENT_REVIEW_ANSWER_V1` (`EXIT` agrees, `CONTINUE` keeps the trade; suggested levels are 422
`SUGGESTED_LEVELS_NOT_ALLOWED`) within its 15 minutes: 404 `EXIT_FLAG_NOT_FOUND`; 409
`EXIT_FLAG_NOT_YET_ASKED`, `EXIT_FLAG_ALREADY_ANSWERED`, `EXIT_FLAG_RESOLVED` or
`EXIT_FLAG_ANSWER_WINDOW_CLOSED`. Receipt `EXIT_FLAG_ANSWER_RECORDED`.

**`POST /api/v1/lab/positions/{setup_id}/exit-flag`** raises the agent's own flag, an
`AGENT_EXIT_FLAG_V1`: `schema_version`, `flag_ref` (UUID, the idempotency key), `lifecycle_id`
(the trade's, from `GET /positions/{setup_id}/news`), `what_changed`, `next_24h`,
`proves_wrong` and 0-8 `sources`, with the answer's limits. Only the agent answering for the
trade may flag it (403). 404 `POSITION_NOT_FOUND`; 409 `EARLY_EXIT_NOT_AVAILABLE` (not under
the version), `POSITION_NOT_OPEN`, `POSITION_LIFECYCLE_MISMATCH`, `EXIT_IN_PROGRESS`,
`IDEMPOTENCY_CONTENT_MISMATCH`. Receipt: `status` `EXIT_FLAG_RAISED` (or
`EXIT_FLAG_ALREADY_PENDING` with the pending flag), `flag_id`, `setup_id`, `raised_at`,
`answer_due_at`, `idempotent_replay`. Jev is asked at once; the trade keeps its stop and target.

**Readback.** `/setups`, `/positions` and `/results` states list `holding_policy`,
`holding_window_seconds` (package review-window: the window a `CRYPTO_WINDOW_REVIEW_V1` or
`CRYPTO_WINDOW_HOLD_V1` trade recorded at admission; absent under the 24-hour versions),
`day_review_at` (T of the next review) and `continuations`; `hard_exit_at` of a trade under the
version is its fail-safe (T + 80 minutes: the longest a review can take plus 5 minutes). The
outputs and timeline routes carry `DAY_REVIEW_REQUESTED`, `DAY_REVIEW_AGENT_ANSWER`,
`DAY_REVIEW_JEV_REQUEST` (context `JEV_DAY_REVIEW_CONTEXT_V1`), `DAY_REVIEW_JEV_RESULT`
(`ANSWERED`, `UNUSABLE` or `FAILED`), `DAY_REVIEW_DISCUSSION`, `DAY_REVIEW_DECISION` (exactly one
per review), `EXIT_FLAG_RAISED`, `EXIT_FLAG_ASKED`, `EXIT_FLAG_AGENT_ANSWER`,
`EXIT_FLAG_JEV_REQUEST` (context `JEV_EARLY_EXIT_CONTEXT_V1`), `EXIT_FLAG_JEV_RESULT`,
`EXIT_FLAG_RESOLVED`, `EARLY_EXIT_DECISION` and `DAY_REVIEW_UNAVAILABLE`. A decision's
`measurement` is the unchanged-plan hook (`change_kind` `CONTINUE_EXIT_DECISION` or
`EARLY_EXIT`, `at`, `old_stop`, `old_target`, `new_stop`, `new_target`, `actually_exited`,
`quote` with bid, ask, mid and last); a trade exited by a review closes with `reason`
`DAY_REVIEW_EXIT` (the fail-safe: `DAY_REVIEW_DEADLINE_EXIT`), by an early exit
`EARLY_EXIT_AGREED`.

### Learning loop: outlooks, post-mortems and lessons (package learning-app, 2026-09-28)

Plan: [LEARNING-LOOP-PLAN.md](LEARNING-LOOP-PLAN.md) (sections 2, 4–7). **Paper trading only.** Nothing here places or changes an order,
changes a trading rule or reaches Jev: outlooks, post-mortems and lessons are research-side
records, and Jev's dossiers and questions are unchanged. Every record is one immutable
`lab.managed_events` row with no setup (`setup_id` null), appended through the audited append;
there is no migration.

Both routes below are on the research agents' route list: every research-agent credential, and
the legacy credential for agent `muse`. The status credential (GET only) and the operator
credential answer 403 `TOKEN_ROLE_NOT_PERMITTED`; the results routes stay closed to agent
tokens. The body's `agent` block (the report V2/V3 block: `agent_id`, `agent_version`,
`guidelines_version`, `guidelines_sha256`, `run_id`, exactly, same patterns) must name the
credential's agent, else 403 `AGENT_IDENTITY_MISMATCH` with nothing stored. Without the learning
intake the routes answer 503 `LEARNING_INTAKE_NOT_CONFIGURED`. Bodies are strict JSON (no
duplicate keys, no NaN or infinity); an unparsable body is 422 with the route's `INVALID_*` code.
Refusals never echo submitted values: a 422 body is `{"detail": code}` plus `errors` (at most 20
`{path, code}`) and, for post-mortems, `item_results`. Times are RFC 3339 with an offset. Text
fields are stripped of surrounding whitespace before their limits are checked.

**Cited source** (both routes): the report's source object exactly. `source_id`
(`[A-Za-z0-9_.-]{1,64}`), `url` (public `https`, no user info, query or fragment, at most 1,000
characters), `excerpt` (1–1,200 characters, verbatim from the page, not blank), `retrieved_at`
(not after the server's receipt time), `published_at` (from the page's own metadata, or `null`;
never after `retrieved_at`). No other key. The server adds `content_hash` (SHA-256 of the
excerpt) and stores both times in UTC. Excerpts and URLs are third-party text: the identity
screen skips them; the credential, e-mail and 0x-address screen does not.

#### `POST /api/v1/lab/market-outlooks` (`MARKET_OUTLOOK_V1`)

At most 2 MiB. One immutable record per `(agent_id, outlook_id)`: the morning test of plan 5c,
written during the research run, before the day's reality is known.

| Field | Contract |
| --- | --- |
| `schema_version` | `MARKET_OUTLOOK_V1` |
| `outlook_id` | UUID. An identical retry returns the stored receipt (`idempotent_replay: true`) without re-checking time or universe; a different body under the same ID is 422 `OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH` (as reports) |
| `generated_at` | the report freshness rule: 0 ≤ receipt time − `generated_at` ≤ the context's `max_report_age_seconds` (60), else 422 `MARKET_OUTLOOK_STALE_OR_FUTURE` |
| `run_slot` | a scheduled run instant (`RESEARCH_SCHEDULE_V1`, any offset), as in report V3; `generated_at` at most the schedule's grace before it |
| `horizon_hours` | `24` exactly |
| `agent` | the report agent block |
| `market` | exactly `summary`, `btc`, `eth`, `factors`, `events` |
| `market.summary` | 1–600 characters |
| `market.btc`, `market.eth` | `{direction, confidence}`: `direction` `UP`, `DOWN` or `FLAT`; `confidence` a decimal from 0 to 1 (at most 10 decimal places) |
| `market.factors` | 0–12 `{name, note, source}`: `name` 1–80 characters, `note` 1–300, `source` a cited source (optional or `null`) |
| `market.events` | 0–20 `{at, what, source}`: `at` RFC 3339 or `null`, `what` 1–300 characters, `source` optional or `null` |
| `coins` | exactly one entry per coin of the current tradable universe (the research context's `universe`), no other coin, no duplicate; at most 230 entries, the most coins one report can name (30 picks plus 200 skipped), which is never below the universe the context serves |
| `coins[].symbol` | exactly as the universe lists it (`BTC/USD`) |
| `coins[].direction` | `UP`, `DOWN`, `FLAT` or `SKIPPED` |
| `coins[].confidence` | UP, DOWN, FLAT: required, 0–1 as above. SKIPPED: absent or `null` |
| `coins[].skip_reason` | SKIPPED: required, 1–200 characters. Otherwise absent or `null` |
| `coins[].expected_move_pct` | optional (`null` or absent): the expected size of the move in percent, 0–100, at most 4 decimal places; absent or `null` for SKIPPED |
| `coins[].reasons` | 0–4 `{kind, text, source}` (empty or absent for SKIPPED): `kind` `NEWS`, `EVENT`, `TECHNICAL`, `FUNDAMENTAL` or `MARKET`; `text` 1–200 characters; `source` optional or `null` |

Refusals, nothing stored:

- 422 `INVALID_MARKET_OUTLOOK` with field errors: schema codes, `AGENT_IDENTITY_REQUIRED`,
  `RUN_SLOT_NOT_SCHEDULED`, `RUN_SLOT_IN_FUTURE`, `SKIP_REASON_REQUIRED`,
  `SKIPPED_COIN_FIELDS_NOT_ALLOWED`, `CONFIDENCE_REQUIRED`, `SKIP_REASON_NOT_ALLOWED`,
  `DUPLICATE_COIN_IN_OUTLOOK`, `COIN_NOT_IN_UNIVERSE`, `OUTLOOK_COINS_INCOMPLETE` (path `coins`:
  a universe coin has no entry), `INVALID_SOURCE_EVIDENCE` (a source retrieved after receipt);
- 422 `SENSITIVE_EVIDENCE_REJECTED` (credentials, e-mail or 0x addresses anywhere; paths listed);
- 422 `AGENT_IDENTITY_IN_OUTLOOK`: the agent ID, as a whole word in any case, in agent-written
  text (`market.summary`, factor names and notes, event texts, skip reasons, reason texts,
  source IDs; paths listed), the report V3 rule;
- 422 `MARKET_OUTLOOK_STALE_OR_FUTURE`, `OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH`;
- 403 `AGENT_IDENTITY_MISMATCH`; 413 `MARKET_OUTLOOK_TOO_LARGE`;
- 503 `RESEARCH_SCHEDULE_NOT_CONFIGURED`, `RESEARCH_UNIVERSE_UNAVAILABLE`,
  `LEARNING_INTAKE_NOT_CONFIGURED`.

The universe is the report's cached, budgeted read, taken only for a new outlook whose format
passed. 202 receipt: `{status: "MARKET_OUTLOOK_RECORDED", schema_version, outlook_id, agent_id,
agent_version, run_slot, generated_at, received_at, window_start, window_end, grading_day,
coin_count, skipped_count, universe_count, event_seq, idempotent_replay, trade_authorized:
false}`. The outlook's forward window is `[window_start, window_end)` = `[received_at,
received_at + horizon_hours)`; `grading_day` is the New York day whose `MARKET_REALITY_V1` grades
it: the day whose end is the first day end at or after `window_end` (the first nightly run after
the window has fully elapsed).

Record: one `MARKET_OUTLOOK` event, idempotency key `market-outlook:<agent_id>:<outlook_id>`:
the canonical outlook (sources with `content_hash`, times in UTC), `received_at`,
`window_start`, `window_end`, `grading_day`, `outlook_hash` (SHA-256 of the canonical outlook)
and the universe it was checked against (`source`, `fetched_at`, `symbols`).

**How the night grades it** (`MARKET_REALITY_V1`, the nightly job; thresholds are version
constants). No outlook is graded on a price move that happened before it was written.

- *Forward window and return*: `[received_at, received_at + 24 h)`. A coin's forward return is
  the first 1-minute open at or after `received_at` to the last close before the window ends
  (Alpaca's public crypto bars). Each outlook is graded once, in the `MARKET_REALITY_V1` of its
  `grading_day` (the first nightly run after its window has fully elapsed); the grade records the
  outlook (`outlook_id`, `event_seq`, `received_at`) and the window it used.
- *Actual direction*: FLAT when |forward return| < 1.5%, otherwise UP or DOWN.
- *Direction hit*: the outlook's direction equals the actual direction, whatever the
  confidence. SKIPPED coins, and coins without bars in the window, are not compared.
- *Calibration*: the compared coins by confidence bucket, [0, 0.2), [0.2, 0.4), [0.4, 0.6),
  [0.6, 0.8), [0.8, 1]: count, hits, hit rate and mean confidence.
- *Miss*: |forward return| ≥ 5% while the coin's outlook was FLAT, the opposite direction or
  SKIPPED.
- *False alarm*: UP or DOWN with confidence ≥ 0.6, while |forward return| < 1.5% or the move went
  the other way.
- *Movers* are measured on the New York day (00:00 to 24:00 America/New_York; the day's first
  1-minute open to its last close): the five largest risers, the five largest fallers and every
  coin with |day return| ≥ 5%. `move_start_at` is where the day's move started: for a rising coin
  the bar of the lowest low at or before the bar of the day's high, for a falling coin the bar of
  the highest high at or before the bar of the day's low (the latest such bar on a tie). A mover
  `was_miss` for an agent when |day return| ≥ 5% and the agent's latest outlook recorded at or
  before the mover's `move_start_at` whose 24-hour window covers `move_start_at` said FLAT, the
  opposite direction or SKIPPED, or no such outlook exists. An outlook written after the move
  began never counts for or against it. A top-5 mover under 5% is a mover and a post-mortem
  subject, never a miss.

#### `POST /api/v1/lab/post-mortems` (`POST_MORTEM_V1`)

At most 512 KiB. One immutable record per `(agent_id, note_id)`: a batch of up to 30 items,
each accepted or refused on its own (its siblings proceed).

| Field | Contract |
| --- | --- |
| `schema_version` | `POST_MORTEM_V1` |
| `note_id` | UUID. An identical retry returns the stored receipt; a different body under the same ID is 422 `POST_MORTEM_IDEMPOTENCY_CONTENT_MISMATCH` |
| `generated_at` | the report freshness rule, else 422 `POST_MORTEM_STALE_OR_FUTURE` |
| `agent` | the report agent block |
| `items` | 1–30 |

Each item:

| Field | Contract |
| --- | --- |
| `subject` | `{"kind": "TRADE", "setup_id": "<UUID>"}` or `{"kind": "MOVER", "symbol": "SOL/USD", "day": "YYYY-MM-DD"}` (`day` a New York date) |
| `cause` | `COIN_NEWS` (a hack, a listing, an unlock, a lawsuit), `MARKET_WIDE` (a Bitcoin or macro move, regulation, an exchange outage), `NO_NEWS` (the setup, technicals or flows alone) or `SURPRISE` (nothing was knowable) |
| `knowable_before_move` | `true`, `false` or `null` (unknown) |
| `summary` | 1–400 characters |
| `sources` | 0–4 cited sources; `COIN_NEWS` and `MARKET_WIDE` need at least one |
| `pre_move_technicals` | 1–300 characters: the technical state before the move (range breakout, support bounce, volume spike, trend, distance from recent highs or lows) |

Item checks in this order; the first failure names the item:

1. `INVALID_POST_MORTEM_ITEM`: schema, with field errors.
2. `DUPLICATE_SUBJECT_IN_NOTE`: a subject already given earlier in the same note.
3. A TRADE subject: `TRADE_NOT_FOUND`; `TRADE_OF_ANOTHER_AGENT` (the setup's recorded agent is
   not the caller's; the legacy credential also owns unattributed legacy trades);
   `TRADE_NOT_CLOSED` (never entered, or not closed yet). A MOVER subject:
   `REALITY_DAY_NOT_RECORDED` (no `MARKET_REALITY_V1` for that day); `NOT_A_RECORDED_MOVER`
   (not one of that day's movers).
4. `POST_MORTEM_SOURCES_REQUIRED`: `COIN_NEWS` or `MARKET_WIDE` without a source.
5. `INVALID_SOURCE_EVIDENCE`: a source retrieved after receipt.
6. `AGENT_IDENTITY_IN_POST_MORTEM`: the agent ID in `summary`, `pre_move_technicals` or a source
   ID.
7. `KNOWABLE_BEFORE_MOVE_UNSUPPORTED`: `knowable_before_move: true` needs at least one source
   whose `published_at` is at or before the item's reference time: for a MOVER that day's
   `move_start_at` of the coin (none recorded: never supported), for a TRADE its first entry
   fill (the cause was public before the trade was entered).

Whole-note refusals, nothing stored: 422 `INVALID_POST_MORTEM` (envelope schema, with field
errors), `SENSITIVE_EVIDENCE_REJECTED`, `POST_MORTEM_STALE_OR_FUTURE`,
`POST_MORTEM_IDEMPOTENCY_CONTENT_MISMATCH`, and, when no item is acceptable, 422 with the first
item's code and `item_results`; 403 `AGENT_IDENTITY_MISMATCH`; 413 `POST_MORTEM_TOO_LARGE`; 503
`LEARNING_INTAKE_NOT_CONFIGURED`.

202 receipt: `{status: "POST_MORTEM_RECORDED", schema_version, note_id, agent_id, received_at,
accepted_count, rejected_count, item_results, event_seq, idempotent_replay, trade_authorized:
false}`; each `item_results` entry is `{index, status: ACCEPTED|REJECTED, subject_key, code,
errors}` (`code` and `errors` for a rejected item), `subject_key` `TRADE:<setup_id>` or
`MOVER:<day>:<symbol>` (null for a malformed subject).

Record: one `POST_MORTEM` event, key `post-mortem:<agent_id>:<note_id>`: the accepted items
(canonical, each with `subject_key` and `reference_at`), every item result, `received_at` and
`note_hash`. A later note may cover the same subject again: the latest accepted item for a
subject is the one the scorecard and the lessons read; every note stays recorded.

#### Research context V2: lessons (`RESEARCH_CONTEXT_V2`, `RESEARCH_LESSONS_V1`)

From this release `GET /api/v1/lab/research-context` answers `context_version:
"RESEARCH_CONTEXT_V2"`: every V1 field unchanged (`report_format` names
`MUSE_RESEARCH_GUIDELINES_V6`) plus `lessons`, read from the ledger at request time. The status
credential reads `lessons: null`. Lessons are the caller's own and sanitized: no other agent's
data, no Jev answers or receipts, no randomized arm, no costs, no `ENGINEERING_TEST` setup. They
never reach Jev. Decimals are strings; a rate or mean with no input is `null`, never 0; counts
are always given.

| Field | Content |
| --- | --- |
| `lessons_version` | `RESEARCH_LESSONS_V1` |
| `agent_id`, `as_of` | the caller's agent and the instant read |
| `available` | `true`; `false` (then only `lessons_version`, `agent_id`, `as_of`, `available` and `code: "LESSONS_UNAVAILABLE"`) when a learning record cannot be read into lessons: the context is still served and research continues; a database outage answers 503 like the rest of the context |
| `scorecard_day` | the New York day of the latest recorded `DAILY_SCORECARD_V1`, or `null` before the first |
| `windows` | `{"1d": LINES, "7d": LINES, "30d": LINES}`: the caller's own lines of that scorecard (`null` when there is none yet) |
| `outlook` | the caller's outlook results (below) |
| `recent_days` | the last 7 recorded `MARKET_REALITY_V1` days, newest first (below): market data, the same for every agent |
| `pending_post_mortems` | `{trades, movers}` still without an accepted post-mortem item from the caller (below) |

`LINES` (one window; a pick belongs to the window its report was received in, a trade to the
window it closed in):

| Field | Content |
| --- | --- |
| `start`, `end` | the window, `[start, end)` in New York days (1, 7 or 30 days ending with `scorecard_day`) |
| `funnel` | `{cycles, picks_sent, accepted, rejected_at_intake, rejection_codes, ranked, vetoed, veto_reasons, not_ranked, not_ranked_reasons, selected, not_selected, skipped, skip_reasons, declined, decline_codes, expired_before_admission, awaiting_admission, admitted, expired_untriggered, invalidated, invalidation_reasons, risk_rejected, watching, triggered, filled, closed, open}`: counts, and `*_codes` / `*_reasons` as `{code: count}` |
| `results` | `{trades_closed, wins, losses, win_rate, r_net_count, mean_r_net, sum_r_net, fees_unverified, exit_reasons, mean_hold_hours}`: wins and losses by gross P&L; `r_net` = R after verified fees (the official R: net P&L over filled quantity × (max entry − initial stop)); trades whose fees are not yet verified count in `fees_unverified` and in no R figure |
| `selection` | `{selected, passed, vetoed, not_ranked}`, each `{picks, shadow_recorded, pending, triggered, shadow_r_count, mean_shadow_r_net, shadow_hit_rate}` from the picks' `PICK_SHADOW_OUTCOME_V1` (assumed fees), plus `selected_minus_passed_mean_shadow_r_net`; `passed` = ranked but not selected |
| `fill_rate_by_distance` | `{"0-1%", "1-2%", "2-3%", "3%+", "UNKNOWN"}`: distance from the agent's price to the entry trigger, abs(price − entry) / price |
| `results_by_timeframe` | `{"1h", "2h", "4h", "6h", "1d", "OTHER", "NONE"}`: the pick's `technical_evidence.timeframe_seconds` (`NONE` without bars) |
| `results_by_rule` | `{"A", "B", "UNDECLARED"}`: the kit's level rule, read from `rule-A` / `rule-B` in the pick's `why_over_peers` or confidence basis |
| `results_by_kind` | `{"CHART", "NEWS", "BOTH"}` |
| `results_by_sector` | `{sector: ...}`: the coin's crypto classification (`ALPACA_CRYPTO_SECTORS_V1`, or the owner's buckets) |
| `excerpt_drop_rate` | `{picks_sent, dropped, rate}`: picks refused at intake for their sources (`INVALID_SOURCE_EVIDENCE`); excerpts a kit dropped before sending are not in the ledger |
| `stale_news_vetoes` | `{news_picks_ranked, vetoed, rate}`: NEWS and BOTH picks vetoed `NEWS_STALE_YES` |
| `dossier_size_rejections` | `{picks_sent, rejected, rate}`: picks refused `DOSSIER_OVER_BUDGET` |
| `causes` | `{notable_trades, with_post_mortem, by_cause, knowable_before_move}`: the window's notable closed trades (a stop-out, a win above 1.5R, an early exit or a 24-hour exit) and the cause of the latest accepted post-mortem of each, `{cause: count}` and `{"true", "false", "null": count}`, so craft lessons can leave out `MARKET_WIDE` and `SURPRISE` cases |

Each bucket of the `fill_rate_by_*` / `results_by_*` maps (only buckets with picks are listed):
`{picks, admitted, filled, fill_rate, shadow_recorded, shadow_triggered, shadow_trigger_rate,
shadow_r_count, mean_shadow_r_net, shadow_hit_rate}`. `fill_rate` = filled ÷ admitted (real
trades); `shadow_trigger_rate` = shadow triggered ÷ shadow recorded (every pick, selected or not,
on Alpaca's minute bars: whether price reached the entry at all).

`outlook` (every figure from the forward-window grades above):

| Field | Content |
| --- | --- |
| `status` | `GRADED`, or `NO_GRADED_OUTLOOK_YET` before the caller's first graded outlook |
| `day` | the `grading_day` of the latest graded outlook |
| `outlook_id`, `received_at`, `window_start`, `window_end` | the latest graded outlook and the window it was graded on |
| `compared`, `hits`, `hit_rate`, `skipped` | its direction hits |
| `calibration` | `[{bucket, count, hits, hit_rate, mean_confidence}]`, buckets `0.0-0.2`, `0.2-0.4`, `0.4-0.6`, `0.6-0.8`, `0.8-1.0` |
| `misses` | `[{symbol, return_pct, move_start_at, outlook_direction, outlook_confidence}]`: `return_pct` the forward return, `move_start_at` where the move started inside the window, `outlook_direction` `FLAT`, the opposite or `SKIPPED` |
| `false_alarms` | `[{symbol, return_pct, move_start_at, outlook_direction, outlook_confidence}]` |
| `graded_outlooks` | the caller's outlooks graded in the last 7 days, newest first: `[{outlook_id, received_at, window_start, window_end, grading_day, compared, hits, hit_rate, misses, false_alarms}]` (`misses` and `false_alarms` as counts) |
| `by_window` | `{"7d": W, "30d": W}`, `W` = `{outlooks_graded, compared, hits, hit_rate, misses, false_alarms, calibration}` summed over the outlooks graded in the window (by `grading_day`) |

`recent_days` entries: `{day, universe_count, measured_count, mover_share, movers, factors}`;
`movers` `[{symbol, return_pct, move_start_at, top_up, top_down, big_move}]`; `mover_share` =
movers ÷ measured coins; `factors` `{btc_return_pct, eth_return_pct, sectors: [{sector, coins,
mean_return_pct}], total_volume_usd, total_volume_vs_7d_avg}`.

`pending_post_mortems`:

- `trades`: the caller's trades closed in the last 7 days: `{setup_id, symbol, entry_at,
  entry_price, exit_at, exit_price, exit_reason, r_net, notable_reasons}` (`notable_reasons` ⊂
  `STOP`, `WIN_OVER_1_5R`, `EARLY_EXIT`, `TWENTY_FOUR_HOUR_EXIT`; `r_net` null until the fees are
  verified; the guidelines ask for a post-mortem on every notable trade).
- `movers`: the latest recorded reality day's movers: `{symbol, day, return_pct, move_start_at,
  was_miss}` (`return_pct` the New York-day return; `was_miss` for the caller, as defined
  above).

#### Nightly records: `MARKET_REALITY_V1`, `DAILY_SCORECARD_V1`, `WEEKLY_REVIEW_V1` and the recorded replays

Written only by the nightly `jobs` service (package learning-app), each an immutable `lab.managed_events` row with no setup, keyed so a rerun never records
twice. They add no route: the status credential reads them on `GET /api/v1/lab/outputs`, the
owner through `cloud_runtime scorecard | market-review | weekly-review` in the ops shell, and a
research agent only its own share through `lessons` (the outputs feed leaves every learning
record out for a research-agent credential, its own outlooks and post-mortems included: it
has their receipts).

| Kind | Key | Body (main fields) |
| --- | --- | --- |
| `MARKET_REALITY` | `market-reality:<day>` | `reality_version`, `day`, `window`, `computed_at`, `thresholds`, `universe` (`symbols`, `count`, where it was recorded), `bars` (source and windows), `measured_count`, `unmeasured`, `coins` (per coin: `status` `MEASURED` or `NO_BARS`, `sector`, `open`, `close`, `return_pct`, `high`, `low`, `max_up_pct`, `max_down_pct`, `move_start_at`, `move_extreme_at`, bar counts, `volume_base`, `volume_usd`, `volume_7d_avg_base`, `volume_vs_7d_avg`), `movers` (`symbol`, `return_pct`, `move_start_at`, `top_up`, `top_down`, `big_move`, `calls` per outlook agent, `missed_by`), `mover_share`, `factors`, `outlook_agents`, `grades` (per graded outlook: the outlook and its window, `compared`, `hits`, `hit_rate`, `skipped`, `unmeasured`, `calibration`, `misses`, `false_alarms`, `market_calls`, `coins`), `limitations` |
| `DAILY_SCORECARD` | `daily-scorecard:<day>` | `scorecard_version`, `day`, `computed_at`, `windows` (`1d`, `7d`, `30d`: `start`, `end`, `overall` and `agents` per agent, the lessons' `LINES`; `overall` adds `maintenance` by decision type, `arms`, `costs`, `engineering_excluded` and, in `1d`, `trades` and the causes' items), `method`, `limitations` |
| `WEEKLY_REVIEW` | `weekly-review:<week_end>` | `review_version`, `week_start`, `week_end`, `evidence_until`, `computed_at`, `method` (resamples, interval ranks, generator, seed rule, minimums), `tests` (per test: `test_id`, `question`, `compares`, `minimum`, `samples`, `means`, `effect`, `interval_90`, `evidence`, `verdict` `KEEP`, `PROPOSE` or `NOT_ENOUGH_DATA`, `shortfall`, `current_version`, `proposal` with `version` and `text`), `week` (the week's scorecard summary), `limitations` |
| `UNCHANGED_PLAN_REPLAY` | `unchanged-plan-replay:<source_event_seq>` | the `UNCHANGED_PLAN_REPLAY_V1` outcome of one applied raise or agreed early exit, once complete, with `setup_id`, `symbol`, `arm`, `agent_id`; never the trade's actual R (read live). `UNCHANGED_PLAN_REPLAY_V2` (package review-window) for a trade that recorded a window: the unchanged plan holds for that window instead of 24 hours |
| `DAY_REVIEW_DECISION_REPLAY` | `day-review-decision-replay:<source_event_seq>` | `DAY_REVIEW_DECISION_REPLAY_V1`: one 24-hour decision against the one not taken (`decision`, `alternative`, the counterfactual exit and its R), once complete. `DAY_REVIEW_DECISION_REPLAY_V2` (package review-window) for a `CRYPTO_WINDOW_REVIEW_V1` trade: an EXIT is compared with continuing for the recorded window (`alternative` `CONTINUE_WINDOW_ON_LEVELS_IN_FORCE`, plus `window_seconds`) |

### Research loop V2: withdrawals, supersession and the context V3 (package research-loop-app, 2026-09-29)

The app side of [the research loop](RESEARCH-LOOP-V2.md): a daily full run plus update runs
(`RESEARCH_SCHEDULE_V2`). Rules and authority:
[REFERENCE-RULES](REFERENCE-RULES.md), 2026-09-29. Fixture evidence only. Nothing here
sizes, orders or authorizes a trade, and nothing reaches Jev.

#### `POST /api/v1/lab/research-withdrawals` (`AGENT_RESEARCH_WITHDRAWAL_V1`)

A research agent withdraws its own unfilled report-V3 picks. On the research-agent routes: every
research-agent credential and the legacy Muse credential (acting for `muse`); the status and
operator credentials are 403 `TOKEN_ROLE_NOT_PERMITTED`. Without the service the route answers
503 `RESEARCH_WITHDRAWAL_NOT_CONFIGURED`.

| Field | Contract |
| --- | --- |
| `schema_version` | `AGENT_RESEARCH_WITHDRAWAL_V1` |
| `withdrawal_id` | A UUID; the idempotency key, per agent |
| `agent` | Exactly `agent_id` (the credential's agent) and `agent_version`, with the report agent block's patterns |
| `items` | 1–30 `{symbol, reason}`: unique symbols in the universe form (`XRP/USD`, at most 32 characters), the reason trimmed, 1–300 characters |

No other key is accepted. The body is at most 1,048,576 bytes (413
`RESEARCH_WITHDRAWAL_TOO_LARGE`). The agent block is validated first (422
`INVALID_RESEARCH_WITHDRAWAL` with its paths), then bound to the credential (403
`AGENT_IDENTITY_MISMATCH`), then the whole body is screened for credentials, e-mail and 0x
addresses (422 `SENSITIVE_EVIDENCE_REJECTED`) and validated (422 `INVALID_RESEARCH_WITHDRAWAL`,
`errors` with paths and codes such as `DUPLICATE_SYMBOL_IN_WITHDRAWAL`; never the submitted
values). A refusal stores nothing.

Effect, per item, in one ledger transaction under the shared lock: each of the caller's own
report-V3 setups on the symbol still `WATCHING` becomes `INVALIDATED` with `revoked: true` and
`revocation_reason: WITHDRAWN_BY_RESEARCH` (one `REVOKE` keyed
`research:withdrawn:<withdrawal_id>:<setup_id>` with `reason`, `withdrawal_id`,
`withdrawal_reason` and `agent`); each of its own report-V3 selections on the symbol still
offered for admission (unexpired, no setup, no decline) gets one `RESEARCH_ADMISSION_DECLINED`
(key `research:admission-declined:<selection_event_seq>`: `cycle_id`, `item_key`, `revision`,
`receipt_id`, `selection_event_seq`, `reason: WITHDRAWN_BY_RESEARCH`, `withdrawal_id`,
`withdrawal_reason`, `agent`) and is never offered again or replaced. "Own" is the `agent`
block the pick's packet recorded (the caller's agent; unattributed legacy records for the legacy
credential). Setups past `WATCHING`, closed ones, non-V3 ones and every other agent's records are
never touched. No broker call is made.

The 200 response: `status: RESEARCH_WITHDRAWAL_RECORDED`, `schema_version`, `withdrawal_id`,
`agent_id`, `agent_version`, `results`, `idempotent_replay`, `trade_authorized: false`. One
result per item, in order: `{symbol, result, setup_ids, selections_declined}` with `result`
`WITHDRAWN` (the revoked setups and the number of declined selections), `NOT_WATCHING` (nothing
withdrawn: the caller's setup on the symbol has left `WATCHING` and is still live; its IDs) or
`NONE`. One `RESEARCH_WITHDRAWAL` event (no setup, key
`research-withdrawal:<agent_id>:<withdrawal_id>`) records `schema_version`, `withdrawal_id`,
`agent`, `request_sha256`, `items`, `results` and `received_at`. An identical resend returns the
stored response with `idempotent_replay: true`; a different body under the same ID is 409
`WITHDRAWAL_ID_CONFLICT`. Admission refuses a withdrawn selection, even one it was admitting at
that moment, with `WITHDRAWN_BY_RESEARCH` (permanent; the refusal names the `withdrawal_id`).
In the research context's `last_run`, a withdrawn setup reads `INVALIDATED` with `setup_reason:
WITHDRAWN_BY_RESEARCH`, and a withdrawn selection `DECLINED` with `decline_code:
WITHDRAWN_BY_RESEARCH`.

#### Run supersession under `RESEARCH_SCHEDULE_V2` (`RESEARCH_RUN_SUPERSESSION_V2`)

While `MANAGED_RESEARCH_SCHEDULE_JSON` names a `daily` run, a V3 setup still `WATCHING` or a V3
selection still offered for admission that answers run `r` is retired only when a published V3
selection of a later run is from a full (daily) run, whatever its symbol, or is for the same
symbol, whatever the run's kind. It is recorded exactly as `RESEARCH_RUN_SUPERSESSION_V1` (the
same keys, `SUPERSEDED_BY_NEW_RESEARCH`, `run_slot`, `superseded_by_run_slot`) with
`supersession_rule: RESEARCH_RUN_SUPERSESSION_V2`, and admission refuses such a selection the
same way. Everything else stays until its own expiry; an adjusted pick Jev does not select
supersedes nothing. A V3 selection is offered for admission from the tick after the one in which
it was published at the latest, once a supersession pass has read it.

#### Research context V3: watching setups and the V2 schedule (`RESEARCH_CONTEXT_V3`)

`context_version: "RESEARCH_CONTEXT_V3"`: every V2 field unchanged, plus:

| Field | Content |
| --- | --- |
| `watching_setups` | The caller's own report-V3 setups whose state is `WATCHING`, by symbol: exactly `setup_id`, `symbol`, `levels` (`entry_trigger`, `max_entry_price`, `stop`, `target`, decimal strings), `run_slot` (in the schedule's zone; as recorded without a schedule), `expires_at` and `signal_id`. `[]` without any, and for the status credential. `open_trades` is unchanged and lists filled trades only |
| `schedule` (V2 only) | In this order: `version` (`RESEARCH_SCHEDULE_V2`), `timezone`, `runs`, `grace_minutes`, `daily`, `current_run_slot`, `current_run_kind` (`FULL` or `UPDATE`), `current_run_valid_until_limit` (the next full run after the current one plus the grace), `next_runs`, `next_run_kinds` (one kind per entry of `next_runs`). Under `RESEARCH_SCHEDULE_V1` the block is exactly V2's |

### Stop-limit fallback and operator pause (`CRYPTO_STOP_BREACH_V2`, `OPERATOR_PAUSE_ENTRY_WAIT_V1`, package exit-and-pause, 2026-09-29)

Rules: [REFERENCE-RULES.md](REFERENCE-RULES.md) (2026-09-29). Fixture evidence only
(`tests/test_stop_breach.py`, `tests/test_pause_wait.py`). No route and no request field was
added; both are read through the existing routes.

- **State** (`/setups`, `/positions`, `/results`):
  - `stop_breach_version`: `CRYPTO_STOP_BREACH_V2` on every crypto setup admitted from this
    release; absent on older setups (V1) and on stocks.
  - `stop_breach_marks` (V2): `stop` (the stop measured), `stop_since` (when the protection pass
    first measured it), and the held-bid mark `bid_since`, `bid`, `bid_quote_at` (null without
    one).
  - `stop_breached_at`: the breach time. Under V1 it is the first fresh bid at or below the stop;
    under V2 the pass that established the breach, null again after a stop change. Newly listed
    for both.
  - `stop_breach_evidence` (V2): the `STOP_BREACH_ESTABLISHED` body below; null until a breach
    and after a stop change.
  - `pause_wait_version`: `OPERATOR_PAUSE_ENTRY_WAIT_V1` on every setup admitted from this
    release.
- **`STOP_BREACH_ESTABLISHED`** (setup event on the outputs and timeline routes; one per stop
  measurement, key `stop-breach-established:<setup_id>:<lifecycle_id>:<stop_since>`): `version`,
  `lifecycle_id`, `stop`, `stop_since`, `breach_evidence` (`TRADE_PRINT` or `BID_HELD`),
  `evidence_at` and `evidence_price` (the print's trade time and price, or the establishing
  quote's time and bid), `trade_id` and `print_age_seconds` (a print), `held_since`,
  `held_seconds` and `first_bid` (a held bid), `established_at`, `fallback_seconds` (5) and
  `fallback_at`. The fallback's close keeps the exit reason `STOP_LIMIT_NOT_FILLED` under both
  versions (protection plan, CANCEL and EXIT decisions, `exit_requested`, CLOSED reason).
- **`OPERATOR_PAUSE_ENTRY_WAIT`** (setup event; at most one per setup and UTC minute, key
  `operator-pause-entry-wait:<setup_id>:<minute>`): `reason` (`OPERATOR_PAUSE`), `version`,
  `halt_ids` (the unreleased pauses), `trigger` (the trigger's `trade_price`, `trade_at`,
  `trade_id`, `bid`, `ask`, `quote_at` and `quote_read_at`, those present) and `waited_at`. No
  `TRIGGER_CONFIRMED` and no risk decision are written, and the state stays `WATCHING`. A setup
  without the version is still refused at a trigger during a pause (`RISK_REJECTED`, reason
  `RISK_HALT`), as is every setup under any other halt; under the daily-loss halt the reason is
  `DAILY_RISK_HALT`.
- The status's `execution_halts`, the watchdog's `HALT_OPERATOR_PAUSE` and the operator commands
  are unchanged.


### Coinbase as the reference market (`CRYPTO_COINBASE_TRIGGER_V1`, `CRYPTO_STOP_BREACH_V3`, package coinbase-reference, 2026-09-29)

Rules: [REFERENCE-RULES.md](REFERENCE-RULES.md) (2026-09-29). Fixture evidence only
(`tests/test_coinbase_reference.py`). No route and no request field was added. Report V3, the
levels and the system check at admission are unchanged: an agent's picks are read exactly as
before, and the entry order is still a limit at `max_entry_price` on Alpaca paper.

- **State** (`/setups`, `/positions`, `/results`), for a report-V3 crypto pick of a coin with a
  Coinbase USD product admitted from this release: `trigger_version`
  `CRYPTO_COINBASE_TRIGGER_V1`, `stop_breach_version` `CRYPTO_STOP_BREACH_V3` and
  `reference_product` (the Coinbase product, such as `BTC-USD`). Other setups are unchanged
  (`CRYPTO_ALPACA_TRIGGER_V1` and `CRYPTO_STOP_BREACH_V2` for a coin without a product).
- **`INVALIDATED`** revision `reason` under this trigger: `STOP_TRADED_BEFORE_TRIGGER` (a Coinbase
  print at or below `stop`), `STOP_QUOTED_BEFORE_TRIGGER` (a fresh Coinbase bid at or below it)
  or `DATA_FEED_FAILURE` (Alpaca's feed at a touch); never `PRICE_BEYOND_MAX_ENTRY`.
- **`CRYPTO_TRIGGER_WAIT`** `reason` adds `COINBASE_FEED_UNHEALTHY` (the coin's Coinbase feed is
  not healthy) and `ALPACA_ASK_ABOVE_MAX_ENTRY` (Coinbase touched; Alpaca's ask is above
  `max_entry_price`); `trigger_version` names this version. `FRESH_QUOTE_UNAVAILABLE`,
  `SPREAD_ABOVE_MAXIMUM` and `TOUCH_NOT_CURRENT` concern Alpaca's confirming quote.
- **`crypto_trigger`** (in `TRIGGER_CONFIRMED`, `INVALIDATED` and waits): V1's fields for Alpaca's
  confirming quote, plus `reference`: `provider` (`COINBASE_EXCHANGE`), `product_id`, `healthy`,
  `code`, `as_of`, `heartbeat_received_at`, `low_print` and `touch_print` (`price`, `at`,
  `received_at`, `trade_id`, `age_seconds`), `bid`, `ask`, `quote_at`, `quote_received_at`,
  `quote_age_seconds`, `quote_read_age_seconds`, `quote_max_age_seconds`,
  `print_max_age_seconds`, `quote_fresh`, `quote_code`. `TRIGGER_CONFIRMED` of this version is
  the observation (Alpaca's quote and the `reference` record) plus `trigger_version` and
  `crypto_trigger`; it has no `trade_price`. The entry decision's context adds
  `reference_product`.
- **`MARKET_PRINT_CONSUMED`** `reason` `ALPACA_PRINT_NOT_TRIGGER_EVIDENCE`: an Alpaca print of a
  setup of this version, consumed unevaluated.
- **`STOP_BREACH_ESTABLISHED`** and `stop_breach_evidence` under V3: `version`
  `CRYPTO_STOP_BREACH_V3`, `breach_evidence` `COINBASE_PRINT` (with `received_at`), or V2's
  `TRADE_PRINT` / `BID_HELD` with their fields, `fallback` (true for V2's evidence, read while the
  Coinbase feed was unhealthy) and `reference` (the feed's health then); the other fields and the
  key are V2's.
- **Runtime events:** `RUNTIME_REFERENCE_CONNECTED` (`provider`, `channels`, `products`,
  `refused`) when the requested products settle; `RUNTIME_REFERENCE_GAP` (`provider`, `reason`,
  `code`) when a feed session ends on an error.
- **Status** (`GET /api/v1/lab/status`): `reference_feed` (null without the feed):
  `provider`, `endpoint`, `channels`, `available`, `connected`, `sessions`, `connected_at`,
  `disconnected_at`, `wanted`, `requested`, `acknowledged`, `refused`, `healthy`, `unhealthy`
  (product: code), `unhealthy_since`, `tape_gaps`, `last_message_age_seconds`,
  `oldest_heartbeat_age_seconds`, `heartbeat_max_age_seconds` (3), `clock_tolerance_seconds` (3),
  `tape_gap_hold_seconds` (10), `as_of`. The watchdog raises `REFERENCE_FEED_UNHEALTHY` after 300 s
  of `unhealthy_since` and `REFERENCE_FEED_STATUS_UNAVAILABLE` for an unreadable section.

### Unfilled crypto entries (`CRYPTO_ENTRY_WORKING_LIMIT_V1`, package entry-working-limit, 2026-09-29)

Rules: [REFERENCE-RULES.md](REFERENCE-RULES.md) (2026-09-29). Fixture evidence only
(`tests/test_entry_working.py`). No route and no request field was added; everything is read
through the existing routes.

- **State** (`/setups`, `/positions`, `/results`), on every crypto setup admitted from this
  release:
  - `entry_working_version`: `CRYPTO_ENTRY_WORKING_LIMIT_V1`; absent on older setups and stocks.
  - `entry_acknowledged_at`: when the app recorded the broker's acknowledgement of the entry
    (the start of its 300 s fill window); absent before the entry.
  - `entry_stop_marks`: the stop check's marks before a fill (`stop`, `stop_since`, `bid_since`,
    `bid`, `bid_quote_at`); present only once a held-bid mark started.
  - `entry_working_cancel`: `reason` and `decided_at` of the decision that ended the entry (or its
    remainder); absent otherwise.
- **`ENTRY_WORKING_CANCEL`** (setup event on the outputs and timeline routes; at most one per
  lifecycle, key `entry-working-cancel:<setup_id>:<lifecycle_id>`): `version`, `reason`,
  `lifecycle_id`, `entry_order_ids`, `acknowledged_at`, `fill_window_seconds` (300),
  `window_ends_at`, `order_age_seconds`, `filled_qty`, `remaining_qty`, `stop`, `stop_evidence`
  (null for the window; for a stop: `stop_rule`, `stop`, `stop_since`, `breach_evidence`
  (`COINBASE_PRINT`, `TRADE_PRINT` or `BID_HELD`) with its fields as in `STOP_BREACH_ESTABLISHED`,
  and `fallback` and `reference` under `CRYPTO_STOP_BREACH_V3`), `decided_at`.
- **Reasons.** `ENTRY_NOT_FILLED` (nothing filled 300 s after the acknowledgement) and
  `STOP_CROSSED_BEFORE_FILL` (the stop's evidence traded first) are the `CLOSED` revision's
  `reason` of a setup whose entry they ended with nothing filled, and the reason of its
  `PROTECTION_PLAN` (`state` `CANCELING`) and CANCEL decisions. `ENTRY_REMAINDER_NOT_FILLED` is
  the reason of the cancel of a partly filled entry's rest (`CRYPTO_PARTIAL_ENTRY_V1`), also in
  `PARTIAL_ENTRY_REMAINDER_CANCEL` and `partial_entry_cancel`; that trade stays open. A setup
  closed this way has no fill: like an entry that expired (`ENTRY_EXPIRED`), it is listed by
  `/setups` but never by `/positions` or `/results`, and it is not a trade on the public
  dashboard.
- `PARTIAL_ENTRY_RETAINED` and `PARTIAL_ENTRY_REMAINDER_CANCEL` of a setup of this version add
  `fill_window_ends_at`.

### Report dry run and research guidelines (package agent-api, 2026-09-29)

An external
research agent that may not run the kit calls this API directly. Two research-agent routes
serve such an agent. Fixture evidence only (`tests/test_research_validate.py`,
`tests/test_muse_connection_examples.py`); not deployed. The report route, intake, selection,
admission and execution are unchanged, and no schema, migration or setting was added.

#### `POST /api/v1/lab/research-reports/validate` (`RESEARCH_REPORT_VALIDATE_V1`)

The report-V3 intake's verdict for a body, with admission warnings; nothing is recorded.

- **Credentials.** On the research-agent route list: each agent token and the legacy token;
  the status token (GET only) and the operator token are 403 `TOKEN_ROLE_NOT_PERMITTED`. The
  body's `agent` block is bound to the credential exactly as on the report route (403
  `AGENT_IDENTITY_MISMATCH`).
- **Order.** 503 `RESEARCH_REPORT_VALIDATE_NOT_CONFIGURED` without the service; then the rate
  limit, per credential: one validation at a time and at most 12 in any rolling 60 seconds (429
  `RESEARCH_REPORT_VALIDATE_RATE_LIMITED`, `Retry-After` in whole seconds; a refused call is
  not counted); then the report route's own reading of the body, through the same handler code:
  at most 1,048,576 bytes (413 `RESEARCH_REPORT_TOO_LARGE`), strict JSON object (422
  `INVALID_MUSE_REPORT`), the agent binding. A body that is not `AGENT_RESEARCH_REPORT_V3` is 422
  `REPORT_V3_REQUIRED`.
- **The verdict** is `ResearchIntake.validate_report`: report-V3 intake's own steps in intake's
  order, through the same methods (`_v3_intake`, the replay check, the universe snapshot, the
  selection rule's activation, `_v3_verdict` with the report's age and expiry, each pick's
  `check_pick` and `compile_pick_dossier` with the byte budgets). Every refusal is intake's,
  status and body byte for byte (the report V3 section above). A recorded `report_id` with the
  same content answers the stored receipt's verdict (`already_recorded: true`); changed content
  is 422 `REPORT_IDEMPOTENCY_CONTENT_MISMATCH`.
- **200 body:** `status` (`RESEARCH_REPORT_VALIDATED`), `validation_version`, `checked_at`,
  `cycle_id`, `already_recorded`, `expires_at`, `submitted_count`, `contender_count`,
  `rejected_count`, `skipped_count`, `item_results` (exactly the 202 receipt's),
  `admission_warnings`, `recorded: false`, `trade_authorized: false`.
- **`admission_warnings`**, one entry per pick in order: `index`, `signal_id`, `entry_type`
  (`PULLBACK`, `IMMEDIATE`, `BREAKOUT` or null), `warnings` and `not_evaluated`. Each warning
  is `{check, code, path, ...}` in admission's order, so the first names admission's refusal:

  | `check` | `code` | Rule | Fields |
  | --- | --- | --- | --- |
  | `LEVELS` | `INVALID_OR_EXPIRED_SETUP` | `0 < stop < entry_trigger <= max_entry_price < target` and `target - max_entry_price >= 2 × (max_entry_price - stop)` (`ManagedExecution.admit`) | `rule`, or `reward_risk_at_max_entry` (4 places) and `minimum_reward_risk` |
  | `ACTIVE_SYMBOL` | `ACTIVE_SYMBOL_ALREADY_MANAGED` | a crypto setup of the coin (matched as admission matches: no slash, upper case) in a non-terminal state, any agent's, other than a report-V3 setup still `WATCHING` that this pick's run supersedes under the configured schedule's `RESEARCH_RUN_SUPERSESSION_V1` or `_V2` | `setup_state` |
  | `PRICE_GRID` | `CRYPTO_LEVEL_OFF_PRICE_GRID` | `crypto_execution.off_grid_levels` with the coin's `price_increment` | `price_increment`, `levels` |
  | `STOP_DISTANCE`, `PRICE_MATCH`, `STOP_NOT_HIT`, `ENTRY_TYPE` | `STOP_DISTANCE_BELOW_MINIMUM`, `PRICE_MISMATCH`, `STOP_ALREADY_HIT`, `BREAKOUT_NOT_ENABLED` | `system_check.evaluate` (`SYSTEM_CHECK_V1`) on the pick's levels, agent price and run slot, as admission calls it | `stop_distance_fraction` and `minimum_stop_fraction`; `live_mid`, `price_deviation_fraction` and `price_mismatch_fraction`; `live_bid` and `live_last`; `live_mid`, `entry_offset_fraction` and `entry_type_band_fraction` |

  `not_evaluated` entries are `{check, reason}`: `ALL` with `PICK_SCHEMA_INVALID`;
  `PRICE_GRID` and the three live checks with `SYMBOL_NOT_IN_UNIVERSE`; `PRICE_GRID` with
  `PRICE_INCREMENT_UNAVAILABLE` (an unusable increment; the universe lists none); the live
  checks with `LIVE_PRICE_UNAVAILABLE` (no usable quote: 0 < bid <= ask and an aware quote time
  not after the read) or `STOP_DISTANCE_FIRST` (the system check reads no price for a short
  stop).
- **Reads.** The ledger twice, outside any transaction and without the shared advisory lock:
  intake's replay check and one read of the active crypto setups of the report's coins. The
  research context's own caches for the universe (asset list, at most an hour old) and the
  latest quotes and trades (one read for every coin, under 5 seconds old, shared with
  `GET /research-context`); a market-data failure is `LIVE_PRICE_UNAVAILABLE`, never a pass.
- **Writes.** None: no event, cycle, packet, dossier or idempotency record, no Jev request and
  so no Jev spend. `ResearchIntake` holds no reviewer.

#### `GET /api/v1/lab/research-guidelines` (`RESEARCH_GUIDELINES_ROUTE_V1`)

The research guidelines text the app enforces. On the research-agent route list; the status
token may read it (GET only); the operator token is 403. The body is constant for a release:

| Field | Content |
| --- | --- |
| `route_version` | `RESEARCH_GUIDELINES_ROUTE_V1` |
| `guidelines_version` | `MUSE_RESEARCH_GUIDELINES_V6`, the research context's `report_format.guidelines_version` |
| `guidelines_sha256` | the SHA-256 of `text`'s UTF-8 bytes, the context's `report_format.guidelines_sha256` (`075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d`) |
| `text` | the guidelines text, from the package (`muse_guidelines.RESEARCH_GUIDELINES`, the V6 constant the Railway image carries with `src/`) |
| `trade_authorized` | `false` |

### The crypto stream's capacity (`CRYPTO_STREAM_CAPACITY_V1`, six-day operation, 2026-09-29)

Rules: [REFERENCE-RULES.md](REFERENCE-RULES.md) (2026-09-29). Fixture evidence only
(`tests/test_managed_runtime.py`). No route and no request field was added.

- **`CRYPTO_STREAM_CAPACITY_WAIT`** (outputs route; at most one per selection, key
  `stream-capacity:<selection_event_seq>`): `version`, `symbol`, `capacity` (15),
  `selection_event_seq`. The offered pick is held back because the crypto stream's 15 coins are
  taken by active setups, Bitcoin and earlier picks; it is admitted as usual once a slot frees up.
- **Status** (`GET /api/v1/lab/status`): `crypto_stream`: `version`, `capacity` (15), `wanted`
  (the number of coins the crypto stream's plan subscribes) and `held_back` (the held-back picks'
  coins, in selection order); `available: false` in place of the last two when the plan cannot
  be read.
- **`RUNTIME_MARKET_GAP`** with `reason` `MARKET_STREAM_PROVIDER_ERROR`: `code` is the provider's
  numeric code, `ALPACA_STREAM_<n>` (`ALPACA_STREAM_405`: symbol limit exceeded), or
  `ALPACA_STREAM_ERROR` without one; never the provider's message text.
