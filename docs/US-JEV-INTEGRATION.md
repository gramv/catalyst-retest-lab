# US Jev selection → paper execution: local integration

2026-09-19. The owner delegated remaining implementation choices. Missing code is
builder work, not a request for another owner confirmation. US fixed-exit proof comes
first, managed exits next, crypto after that. India remains research-only.

## Implemented

| File | Change |
| --- | --- |
| `us_admission.py` | Read-only asset/calendar/liquidity/classification/account evidence, reconciliation/feed gates and provenance |
| `alpaca.py` | Fixed-host GET-only paginated raw daily bars; incomplete/repeated pagination fails closed |
| `repository.py`, `domain.py` | Provider I/O before audit/account lock; fresh validation after lock; atomic batches and provenance |
| `migrations/012_jev_us_paper.sql` | Immutable admissions/revocations, narrow grants, exact review binding; DB guards on risk decisions and dispatch claims |
| `jev_paper.py` | Separate admission worker; selected-item handoff; durable revocation and pending-entry cancellation |
| `risk.py`, `authorization.py`, `risk_runtime.py` | Recheck review at reservation/dispatch; mechanical risk/protection/exits remain final authority |
| `config.py`, `api.py` | Explicit opt-in, required poll input, worker health; refuse unreviewed legacy candidate POST in Jev mode |
| `research_reports.py`, `static/review.*` | Private shortlist shows separately labeled paper enrollment/state |
| `.env.example` | Configuration names only, no credentials |
| `tests/test_us_admission.py`, `tests/test_jev_paper.py` | Evidence, receipt, expiry, permission, risk, race and complete fixture-loop tests |

Python paths are under `src/catalyst_lab/`. No public raw-order/quantity/override API was
added. Old Step 4 intents remain inert. Review/Jev roles cannot insert execution bindings
or risk decisions. Migrations/proofs used disposable databases only; existing account
runtime database/processes were not migrated or restarted.

## Chosen local-test policies

- `US_PAPER_ADMISSION_TEST_V1`: minimum **$20M** average daily dollar volume over
  **20 completed exchange sessions**. Mean(raw daily volume × provider VWAP), complete
  coverage required. IEX is explicitly permitted and labeled **IEX_ONLY**: observed feed
  volume, not consolidated-market volume. These provisional operational thresholds do
  not change frozen V1 price/risk rules.
- `JEV_US_SELECTED_FIXED_TEST_V1`: consume only a current US engineering selection,
  valid exact receipt, healthy Gate-1 worker scope, closed breaker and no operator halt.
  Each item is attempted once. Historical SELECTED records are not retroactively armed.
- Explicit admission polling: **1 second** for local testing. No default enables it.
- Candidate deadline: earlier of original evidence expiry and calendar close minus five
  minutes. Gate-1 evidence remains bounded to 60s; the separate committed risk capability
  remains **5 seconds**. No expiry extension.
- Selection supplies neither size nor an order. Normal validation, printed trigger,
  fresh quotes/feed/session, broker equity, server classification, no leverage and atomic
  1%/2%/−3% limits apply. Unknown classification rejects; none were invented.
- Review loss/revision/expiry durably revokes pending eligibility. Cancellation targets
  the entry through risk authorization. A fill winning the race keeps protection or
  mechanically flattens under `PROTECTION_FAILURE`. AI loss after a fully protected fill
  does not request an early exit or remove its bracket.

Future management choice: Jev chooses IDs of source-backed eligible levels supplied
by Muse. Code rejects stop widening, increased size/risk and deadline extensions. This
needs its own tested amendment/recovery policy and cohort. No numerical trailing formula,
PATCH path or model-driven early-exit path was introduced here.

## Cohort and configuration

`JEV_US_SELECTED_FIXED_ENGINEERING` uses TEST-JEV-prefixed signals and immutable
`ENGINEERING_TEST` provenance. Candidate context/admission bind item, receipt and policy;
orders/fills inherit engineering classification. Private reports join that cohort/state.
Public strategy views exclude it. Frozen Jev-free history remains unchanged.

An isolated local deployment explicitly supplies the usual market/application/risk
configuration plus:

```text
US_ADMISSION_POLICY=US_PAPER_ADMISSION_TEST_V1
JEV_PAPER_POLICY=JEV_US_SELECTED_FIXED_TEST_V1
JEV_ADMISSION_POLL_SECONDS=1
```

The separate review worker still requires every Gate-1 input. Both services use the
same migrated database with separate restricted roles. Current-process reconciliation
and authenticated market/broker streams gate watching and submission. Jev uses the
existing private ignored mode-0600 local `.env`; no key appears in these settings, logs
or receipts. Railway variables remain the future deployment route. Do not point fixture
scripts at the account runtime.

## Evidence

Regression: **584 backend tests passed**, two upstream Starlette deprecation warnings;
lint clean. Private screen: **16 browser checks passed**, no browser errors. Screenshots
are fictional engineering fixtures, not account performance.

`artifacts/us-jev-integration/fixture-proof.json` contains the full chain and fills.
Scope: **fake Jev + fake Alpaca, real disposable PostgreSQL**. The synthetic calendar
allows the fixture session; it does not claim the real exchange was open on Saturday.
Actual watcher/risk/transport-guard/ledger/flatten code ran: no order before the print,
86 whole shares filled at $100.10 and closed at $100.10, zero remaining broker positions
and local reservations, clean final reconciliation. Bracket limit: $100.15. No real
broker order was sent.

Recorded checkpoint: **59 events**, head
`e60cdd6ca0f84dad324d43b78fbcc25ab04651936b871f74601badd508337523`.
The JSON export is authoritative if the disposable proof is rerun.

Tests cover parallel admissions, three concurrent risk checks bounded to 2%, stopped
worker before risk and between decision/dispatch, material revisions, expiry, rejected
or uncertain judgments, receipt tampering, DB-claim bypass, legacy-route bypass, missing
risk decision, append-only permissions, cancel/fill races and protected positions during
AI failure. Existing risk/trigger/protection regressions remain green.

## Remaining gates

Actual Alpaca US acceptance needs a regular market session and clean account preflight.
Saturday fixtures do not satisfy it; the prior real engineering run is unchanged. No
artificial print or overridden price will force acceptance.

Managed amendments and crypto execution remain implementation work, not missing Jev
credentials or another owner-policy question. They follow US proof in the chosen sequence.
Crypto needs separate session/precision/order/protection rules sharing account risk; it
cannot reuse US DAY brackets. India never executes. Railway and actual Muse connection
remain deferred until local acceptance. No unattended claim, recurring Codex job or broker
mutation was made during this milestone.
