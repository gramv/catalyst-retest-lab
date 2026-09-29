# Package fees-net-r (plan phase 0)

Branch `pkg/2026-09-26-fees-net-r` from `work/2026-09-24-product-plan` at `80eb952`. Scope:
Alpaca fee reads (GET only), net P&L/R, the owner's official-R ruling, and results
aggregates. Reads and reports on real fees and real fills wherever they exist; **every
test in this package runs against disposable PostgreSQL and a fake Alpaca activities
endpoint — fixture evidence only.** No real broker or TypeSafe/Jev call, no network,
no owner ledger touched.

## Text for docs/PHASES.md (not applied there; the package instructions ask for it here)

> ## 2026-09-26 — Fees net of Alpaca, official R, results aggregates (package fees-net-r): FIXTURE EVIDENCE ONLY
>
> Reads Alpaca's own `CFEE`/`FEE` account activities (GET `/v2/account/activities`, the
> existing read-only transport and request-budget governor), matches each to a managed
> fill by broker order id and time, and appends the fee as cost evidence through the
> existing `FILL_COST_CORRECTION` event path (source `ALPACA_PAPER_ACTIVITY`). Adds
> `net_r` (net P&L over the reservation's authorized-quantity risk) and `official_r`
> (owner ruling R5: net P&L over the *filled* quantity times admitted max entry minus
> admitted initial stop) to every measurement, and a new results-aggregate view (count,
> win rate, mean gross/net/official R, total fees) grouped by market, arm, selection
> policy, question set and agent.
>
> Evidence: `tests/test_managed_fees.py` (22 cases), reproducing the shape of the
> 2026-09-25 first real managed trade (`artifacts/first-managed-trade-2026-09-25/`) with
> a fake Alpaca activities endpoint: net ≈ −$73.77 here (task/owner reference ≈ −$74.03;
> the real entry fills' individual quantity split was never published, only their sum,
> so this fixture's own even split is close but not bit-exact), official R ≈ −1.475
> (reference ≈ −1.48), gross R ≈ −1.256 (reference ≈ −1.26) — all well inside the
> reference figures' own stated precision. Disposable PostgreSQL and a fake broker
> throughout; the exact wire shape of a real Alpaca fee activity, and in particular
> whether an in-kind (coin) fee is reported as documented in `normalize_fee_activity`
> below, is **not independently confirmed in this environment**. Real fee confirmation
> needs an owner-run read of the live paper account's `/v2/account/activities` (or the
> Alpaca dashboard) for a fee-bearing fill, compared against this module's parsing.
> Full suite and ruff: see "Verification" below.

## Text for docs/CONTRACT-RESOLUTIONS.md (not applied there)

> ### Fees-net-r: official R, fee sources, aggregate definitions (package fees-net-r, 2026-09-26)
>
> - **Official R (owner ruling R5) is implemented for managed reporting**:
>   `official_r = net_pnl / (filled_buy_qty * (admitted_max_entry_price − admitted_initial_stop))`.
>   "Filled" is the sum of a setup's recorded `buy` fill quantities (`managed_fills`);
>   "admitted max entry"/"admitted initial stop" are read from the setup's immutable
>   admission record (`managed_setups.record_json.levels`), never the reservation's
>   authorized quantity and never a later trailing-stop amendment recorded only in the
>   setup's state. `config.REPORTING_R_METHOD` (frozen V1/archived, Phase 5) is untouched;
>   this is a new, managed-only constant (`managed_measurement.OFFICIAL_R_METHOD =
>   "MANAGED_OFFICIAL_R_PLANNED_FILLED_V1"`).
>   - `test_r` (existing: `gross_pnl / reservation.planned_risk`, the authorized-quantity
>     denominator) is kept, unchanged, labelled `ENGINEERING_ALTERNATIVE_AUTHORIZED_
>     QUANTITY_DENOMINATOR`. It equals `official_r`'s denominator exactly whenever a
>     setup's entry fills completely (the common case; they differ only after a partial
>     fill capacity-binds a smaller authorized quantity than what later actually fills).
>   - New: `net_r = net_pnl / reservation.planned_risk` — the net analogue of `test_r`,
>     same (authorized-quantity) denominator, so the two R values across the codebase
>     that share a denominator (`test_r`, `net_r`) are always directly comparable.
> - **Fee sources**: Alpaca's own `CFEE`/`FEE` account activities, read GET-only via
>   `AlpacaReadOnly.fee_activities_since` (paginated exactly like the existing
>   `fill_activities_since`), matched to a fill by broker order id and closest
>   transaction time (`managed_analytics.match_fee_activity`, 5-second tolerance) and
>   appended as `FILL_COST_CORRECTION` evidence with source `ALPACA_PAPER_ACTIVITY`
>   (added to `managed_analytics.SOURCES`). A confirmed-zero activity (both cash and
>   in-kind amounts explicitly zero) is accepted as evidence of no fee, the same as any
>   other amount; an activity that matches no fill, or a fill that already carries a
>   different latest correction, is counted and skipped, never guessed or overwritten.
>   A fill with **no** matching activity at all stays unknown (fail-closed): its net P&L,
>   and so its setup's, stays `null` until real evidence arrives or an operator imports
>   one through the existing `scripts/import_managed_costs.py` path.
> - **Fixture cost sources are never counted as verified real-account performance in an
>   aggregate.** `managed_measurement`'s own per-fill fields are unchanged (a disposable
>   database's `LAB_FIXTURE` correction verifies a fill's fee there exactly as before,
>   for every existing test) — this rule applies only to `managed_result_aggregates`:
>   a closed setup whose cost evidence includes any `LAB_FIXTURE` source is still counted
>   (`count`, visible) but excluded from `mean_net_r`, `mean_official_r` and
>   `total_fees_usd`, and separately flagged in `fixture_tainted_count`.
> - **Results aggregates** (`managed_result_aggregates`, `GET /api/v1/lab/results/
>   aggregates?group_by=market,arm,...`): grouped by any subset of `market`, `arm`
>   (`FIXED_EXIT`/`JEV_MANAGED`), `selection_policy`, `question_set_version` and
>   `agent_id` (default: all five). Per group: `count` (every non-`ENGINEERING_TEST`
>   closed setup); `win_rate` and `mean_gross_r` from setups with known gross P&L (no fee
>   evidence needed); `mean_net_r`, `mean_official_r` and `total_fees_usd` from setups
>   with verified, non-fixture-tainted fees. A statistic with no known input is `null`,
>   never a default of zero. `ENGINEERING_TEST` setups are counted separately
>   (`engineering_count`), never inside a group, matching every other analytics view.

## What changed

- `src/catalyst_lab/alpaca.py`: `AlpacaReadOnly.fee_activities_since` — paginated GET of
  `/v2/account/activities?activity_types=CFEE,FEE`, mirroring `fill_activities_since`.
  No transport/allowlist change: `/v2/account/activities` was already GET-only allowed.
- `src/catalyst_lab/managed_analytics.py`: `normalize_fee_activity`, `match_fee_activity`,
  `import_alpaca_fee_activities` (the read-matching-import pipeline, pure and unit
  tested without a database where possible); `FEE_ACTIVITY_SOURCE` added to `SOURCES`;
  `managed_result_aggregates` (new); `paginated_managed_results` now also flattens
  `test_r`/`net_r`/`official_r`.
- `src/catalyst_lab/managed_measurement.py`: `official_r`, `net_r`, `planned_filled_risk`
  and `official_r_method`/`test_r_label` added to `managed_measurement`'s return; one new
  read of the setup's admitted `levels` (`lab.managed_setups.record_json`). Every
  existing field is unchanged.
- `src/catalyst_lab/managed_execution.py`: `ManagedExecution.fee_backfill` — the bounded,
  best-effort periodic/on-demand entry point (own window anchored to the last completed
  run, `RESEARCH` broker-read priority so it never competes with protective reads).
- `src/catalyst_lab/managed_runtime.py`: one line in `start()`'s thread list
  (`fee_import_once`, reusing the existing `reconcile_seconds` cadence — no new
  `RuntimePolicy` field, so `test_runtime_policy_has_no_silent_constructor_defaults`
  stays exactly as strict as before) plus the small wrapper method itself.
- `src/catalyst_lab/managed_service.py`: `GET /api/v1/lab/results/aggregates`
  (`?group_by=`, comma-separated dimensions); `/results` also flattens `test_r`/`net_r`/
  `official_r` per item.
- `scripts/import_alpaca_fees.py` (new): the on-demand CLI counterpart of
  `fee_backfill`, for an operator with a private v2 configuration. Never places, amends
  or cancels an order; never migrates, seeds or inserts a fixture into a database.
- `docs/API-CONTRACT.md`: documents the new fields and the new route.
- Tests: `tests/test_managed_fees.py` (new, 22 cases — see below); small additive fixture
  support in `tests/test_managed_execution.py` (`ManagedVenue.fee_activities`, and an
  optional `levels=` override on the `packet()` helper, default `None` so every existing
  call is unchanged).
- **No migration.** Everything fits the existing `FILL_COST_CORRECTION` event path and a
  read-only aggregate query; `SCHEMA_VERSION` stays 20 (code) / unmigrated at 13 on the
  live ledger, as documented in AGENTS.md and the September 24 ruling — this package
  changes no schema, so it does not touch the owner migration rehearsal's pinned numbers.

## Tests added (`tests/test_managed_fees.py`)

`normalize_fee_activity` (cash/in-kind/confirmed-zero, and 13 malformed-row cases,
parametrized) · `match_fee_activity` (order + closest time, tolerance, ties, unknown
order, no fills) · the paginated activity reader (mirrors the existing
`fill_activities_since` test in `tests/test_fill_backfill.py`: paging, an incomplete
read, a 429, an aware-window requirement) · reproducing the first real trade (fake
activities endpoint, net/official R/gross R against both this fixture's own exact
arithmetic and the task's approximate reference figures) · idempotent re-import ·
a missing activity (that one fill's fee stays unknown, net stays `null`, gross is
unaffected) · a stock fill with the `FEE` activity type, one confirmed-zero and one
small nonzero fee · the aggregates view, including the fixture-taint exclusion and its
visibility · the pure matcher/importer against plain dicts with no database.

## Deviations and open items

1. **The exact wire shape of a real Alpaca fee activity is an assumption**, documented
   in `normalize_fee_activity`'s docstring and stated explicitly in every place this
   package's evidence is fixture-only: `net_amount` as the signed USD ledger impact
   (`"0"` when charged entirely in-kind), a positive `qty`+`price` for an in-kind fee.
   This was not, and could not be, checked against a real account from here. The owner
   should read one fee-bearing fill's activities from the live paper account (dashboard
   or `GET /v2/account/activities?activity_types=CFEE,FEE`) and compare against this
   before trusting `fee_backfill`'s output on the real ledger.
2. **The real first trade's exact entry-fill split is unknown** (the artifact publishes
   only the two prices and their combined quantity, not each fill's own share), so the
   fixture test's own numbers (net ≈ −$73.77) are close to, but not bit-exact against,
   the task's stated reference figures (≈ −$74.03). Both the test's own arithmetic and
   its tolerance against the reference figures are asserted; see the test's comments.
3. **`fee_backfill`'s periodic wiring reuses `reconcile_seconds`** rather than adding a
   dedicated interval, specifically to avoid touching `RuntimePolicy` (every field there
   is deliberately required with no default — `test_runtime_policy_has_no_silent_
   constructor_defaults` enforces this) and to keep this package's footprint in
   `managed_runtime.py` to the smallest possible diff, since jev-breaker and
   unattended-safety are concurrently changing that file's exit paths. If a dedicated,
   independently-tunable interval is wanted later, it is a small follow-up.
4. **`arm`'s exact values** (`FIXED_EXIT`/`JEV_MANAGED`, `account_risk.py`) are read
   directly from setup state for the aggregate's `arm` dimension; this package added no
   new arm logic and does not re-verify the randomized-arm assignment itself (already
   covered by `tests/test_randomized_arm.py`).
5. **`question_set_version`** is read from `record_json` as B1/B2 selection already
   writes it; a legacy or pre-B1 setup reports `null` there, grouped together under that
   `null` key rather than excluded.
6. Not built (out of the stated scope): a dedicated migration/table (the existing
   `FILL_COST_CORRECTION` path covered every need); wiring `scripts/import_alpaca_fees.py`
   into a scheduler (it is a one-shot operator command, like
   `scripts/import_managed_costs.py`); any change to `managed_runtime.py`/`managed_ops.py`/
   `managed_execution.py`'s exit paths (jev-breaker/unattended-safety); any change to
   research intake (research-v3).

## Verification

- `XDG_DATA_HOME=~/.local/share/catalyst-wt-fees-net-r ./run pytest -q` — see the final
  commit message for the exact count; run before every commit that touches source.
- `XDG_DATA_HOME=~/.local/share/catalyst-wt-fees-net-r ./run ruff check src tests`
- Targeted subset re-run after every source change in this package:
  `tests/test_managed_execution.py tests/test_managed_measurement.py
  tests/test_managed_analytics.py tests/test_managed_runtime.py tests/test_fill_backfill.py
  tests/test_owner_migration_rehearsal.py tests/test_contract.py tests/test_managed_service.py
  tests/test_managed_app.py tests/test_frozen_rulings.py tests/test_managed_fees.py`
