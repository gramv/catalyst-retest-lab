# Muse research boundary — 2026-09-19

Owner correction: Muse scans and researches outside the application. The application
receives Muse's reports and hosts Jev review, trigger monitoring, risk, execution and
position management. The previous managed `research-scans` intake put discovery in the
wrong layer and could exclude candidates before Jev received any research.

## Current flow

1. External Muse (Codex during supervised testing) researches markets and submits up to
   30 ordered contenders with technical analysis, news/source excerpts and proposed levels.
2. App-hosted selection Jev evaluates each report item with the existing pinned SKEPTIC
   question set and receipt verification. Existing policy publishes at most ten selections.
   Rejection remains rejection; ambiguity emits durable evidence tasks back to Muse.
3. Muse supplies material evidence revisions. Original expiry stays fixed; unchanged
   source rerolls remain disallowed. The source/receipt/event history remains append-only.
4. The application watches reviewed setups and independently validates actual broker
   eligibility, calendar/expiry, quote/feed health, trigger prints, spread, levels and risk.
   Proposed Muse levels are not evidence that any execution check has passed.
5. App-hosted management Jev receives position context. Code validates bounded stop/target
   choices and risk authorizes mutations. Protection/reconciliation/time exits continue
   independently of model availability. India remains research-only.

No discovery scanner is constructed by the current application. `AlpacaMarketSource`
provides GET-only metadata and observations and has no `collect_and_scan` method.
`AlpacaScanSource` and `ResearchCycle.start` remain standalone historical/fixture utilities;
they have no executable app route. Historical scan events are not rewritten or deleted.

## Authenticated report contract

`POST /api/v1/lab/research-reports`, Bearer auth, maximum request size 1 MiB.
Response is HTTP 202 with `MUSE_REPORT_RECORDED`, `cycle_id`, `contender_count`, original
bounded `expires_at`, `idempotent_replay`, polling URL, and `trade_authorized: false`.
The body has exactly:

- `report_id`: UUID; exact retries reuse it, changed content is rejected.
- `generated_at`, `valid_until`: timezone-aware RFC3339 timestamps. Freshness is computed
  by code against the existing required cycle policy; delivery does not restart the clock.
- `items`: 1–30 unique signals and market/symbol pairs, ordered by Muse's research priority.
  Twenty to thirty is the research target, not a requirement to pad weak candidates.

Each item has exactly these fields:

| Field | Contract |
| --- | --- |
| `signal_id` | Unique research signal identifier |
| `market` | `US_STOCKS`, `CRYPTO`, or `INDIA` |
| `symbol` | Instrument symbol |
| `direction` | `LONG` |
| `catalyst` | Short catalyst label |
| `thesis`, `disproof`, `economic_relationship` | Market-only text, each up to 1,000 characters |
| `technical_analysis` | Muse's external analysis, up to 2,000 characters |
| `levels` | Positive finite decimal `entry_trigger`, `max_entry_price`, `stop`, `target` |
| `sources` | 1–8 exact excerpts with `source_id`, public HTTPS `url`, `excerpt` (≤1,200 characters), `retrieved_at`, nullable `published_at` |

The server hashes source excerpts itself. Missing publication time stays null, never an
invented current timestamp. All fields are allowlisted; quantity, endpoint, account-risk,
classification and approval overrides are refused. PII/credential-like content fails
before persistence; validation responses do not echo rejected input.

`GET /api/v1/lab/cycles/{cycle_id}/outputs` returns the durable research packets, answers,
receipts, evidence tasks and selected list. The existing evidence-revision endpoint appends
new source-backed revisions. Source material remains untrusted research data.

## Files changed

- `muse_reports.py`: typed external-report schema, using the existing source/level contracts.
- `research_cycle.py`: direct immutable report intake, receipt-compatible packets, idempotency,
  deadline bounds and external-research provenance; existing selection/evidence loop reused.
- `managed_app.py`: removes ScanSubmitter and discovery configuration, composes report intake.
- `managed_service.py`: replaces the scan POST route with report POST; distinguishes historical
  scan diagnostics from current Muse reports.
- `scan_sources.py`: separates read-only observations from the standalone discovery utility.
- `managed_runtime.py`: constructs only observation sources, retains a separate position-bar
  context window; no scanner profile is required for the runtime.
- `.env.example`: required variable names become `MANAGED_REPORT_MAX_SECONDS` and
  `MANAGED_CRYPTO_CLASSIFICATIONS_JSON`; no values or credentials added.
- `scripts/serve_muse_intake.py`: local intake/readback process connected to the existing
  worker queue; no model evaluator or account executor starts in this process.
- Tests: direct report intake, no scanner dependency, privacy/auth, stale/duplicate/concurrent
  reports, source follow-ups, India boundary, and full two-market fixture lifecycle.
- Runbook/API/README/AGENTS: records owner-corrected ownership and current launch settings.

## Local runtime

The corrected external-Muse intake is running at **http://127.0.0.1:8770**, with the
existing private token, on the same managed database as the current worker.
`scripts/serve_muse_intake.py` starts only HTTP intake/readback. `ResearchIntake` has no
provider evaluator or execution method. It appends immutable reports; the existing
app-owned worker consumes them, calls Jev and owns all later execution checks.
No second account executor, credential transfer or broker restart was needed.

Live read-only/invalid-request proof at 2026-09-19T21:28:01 UTC confirms health, an
absent discovery route (404), unauthorized intake refusal (401), invalid report refusal
(422), the existing worker RUNNING with broker updates connected, and zero local positions.
No synthetic report was inserted into the real ledger. Evidence is retained in
`artifacts/muse-intake-correction-2026-09-19/runtime-proof.json`.

Port 8768 is the preserved internal worker API with its previously loaded HTTP routes;
its historical scan route remains in that old process until a supported restart.
Do not use it for discovery. Port 8770 is the current Muse-facing local application.
The 8769 read-only dashboard is an older serving process; use 8770 for the updated page.
The executable source uses observation-only data clients on its next launch. The old
worker's current policy and the new intake policy match exactly; TTL/risk/trigger rules
were not changed. No unattended-operation claim or external deployment is made.

Fixture tests exercise the two-process handoff through shared PostgreSQL, then actual
review code. The complete fixture cycle starts with an external report and covers
selection, both markets, managed exits and zero exposure. A new real-provider trade is
not claimed by this architecture correction.

Current-worker read-only checkpoint during this correction: broker reconciliation event 668,
2026-09-19T21:25:25.374925+00:00, clean with zero discrepancies; API reported zero local positions.
The 669-event log verified with head
`4ba1bb26b4bd1ba40bf12141f8577f87171eb5fa0e3208fb239a3cbb4c8a4aad`.
The worker continues appending events, so this is a retained checkpoint, not a permanent head.

Five browser checks passed on the new local surface, with no JavaScript errors. Desktop
and mobile screenshots are retained alongside the runtime proof. A later read-only ledger
checkpoint verified 777 events with head
`cd9a2734ec7bfc37ad6c54249ba89a5b0d0bc10241f4432e94bc367f7c9475ef`.

## Validation result

Final full suite: **953 passed**, two dependency deprecation warnings, 155.52 seconds.
`ruff check src tests scripts/serve_muse_intake.py` and `git diff --check` passed.
The complete external-report fixture proves 20 contenders, ten selections, one US-stock
and one crypto lifecycle, managed exits, four mock-provider fills, and zero remaining
exposure. These are simulated provider interactions, not actual Alpaca fills.
