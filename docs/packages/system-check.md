# Package system-check — the system's check of selected V3 picks and run supersession (plan phase 3a)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-system-check`, from `work/2026-09-24-product-plan` at `676183d`. Plan:
`docs/CRYPTO-AGENT-LOOP.md` sections 2, 3, 4.3, 4.4 and 5 (owner-approved 2026-09-26) and the
owner decisions relayed for this package on 2026-09-27: the system check happens after Jev's
selection, independently of the agent and Jev; a selected pick that fails it is replaced by
Jev's next-ranked pick (a later package, not built here; the permanent refusal codes below are
its trigger); breakout entries trade only after a backtest earns them, until then they are
tracked and not traded; stops must be at least 2% below max entry. This file carries the text
the coordinator merges into `docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md`; neither file
is edited on this branch.

## PHASES entry (paste as written)

### 2026-09-27 — System check of selected V3 picks and run supersession (plan phase 3a, package system-check) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
mock Jev transport, a fixture universe, and Alpaca market-data answers from an `httpx`
MockTransport behind the real read-only market source. No broker, provider, network or
owner-ledger contact; no migration (schema stays 20); admission SQL unchanged. V1, V2, B1, B2
and operator engineering admission are unchanged; only selections whose packet says
`report_schema_version: AGENT_RESEARCH_REPORT_V3` are affected.

- **System check (`SYSTEM_CHECK_V1`, new `system_check.py`, called from
  `ManagedExecution.admit`).** After every existing admission check and the broker price grid,
  outside the shared lock and before the setup row that spends the symbol slot, a V3 pick is
  checked, in this order, the first failure naming a permanent refusal:
  `STOP_DISTANCE_BELOW_MINIMUM` ((max entry − stop) / max entry below
  `MINIMUM_CRYPTO_STOP_FRACTION = Decimal("0.02")`; from the levels alone, so no live price is
  read for it), then on the live price `PRICE_MISMATCH` (|mid − agent's current price| /
  agent's price above 5%), `STOP_ALREADY_HIT` (bid at or below the stop, or the last trade at or
  below it) and `BREAKOUT_NOT_ENABLED`. Entry type from the mid: `PULLBACK` (entry trigger more
  than 0.2% of the mid below it), `IMMEDIATE` (within ±0.2%, bounds included), `BREAKOUT`
  (more than 0.2% above). Equality at 5% and 2% passes. Decisions compare exact Decimal
  products.
- **Live price (`LivePriceReader`, owned by the runtime).** The stream observation's bid and ask
  when its quote is at most 5 s old by its own timestamp; otherwise one REST latest-quote read
  (`/v1beta3/crypto/us/latest/quotes`) through the runtime's `AlpacaMarketSource`, declared a
  research read, fresh by its read time, its own timestamp recorded. At most one REST read per
  protection tick and one per symbol per 5 s. The last trade is the stream's. Nothing usable is
  `LIVE_PRICE_UNAVAILABLE`: transient, audited once per runtime, selection and reason with the
  attempts, retried every tick, never declined.
- **Evidence.** A permanent refusal is final for its receipt: one `SYSTEM_CHECK_REFUSED`
  (key `system-check-refused:<receipt_id>`), and the runtime's `RUNTIME_ADMISSION_REFUSED` and
  `RESEARCH_ADMISSION_DECLINED` carry the same `system_check` object (bid, ask, mid, last and
  their times and sources, the deviation, entry offset and type, the stop distance, the
  thresholds and every check's `PASS`/`FAIL`/`NOT_EVALUATED`). An admitted V3 setup's WATCHING
  state records `system_check` (`PASSED`) and `entry_type` for the later trigger versions and
  measurement. The four check codes and `SUPERSEDED_BY_NEW_RESEARCH` are in
  `PERMANENT_ADMISSION_REFUSALS`; `system_check.SYSTEM_CHECK_REFUSALS` names the four the
  replacement package should replace.
- **Run supersession (`RESEARCH_RUN_SUPERSESSION_V1`).** Every protection tick, before
  admission and whatever the readiness, while a V3 setup is WATCHING or a V3 selection awaits
  admission, the runtime reads the newest `run_slot` of any published V3 selection (a tick
  without V3 work reads nothing more). Older V3 setups still WATCHING are revoked
  `SUPERSEDED_BY_NEW_RESEARCH`
  through `ManagedExecution.revoke` (only while still WATCHING under the shared lock; one keyed
  REVOKE and its INVALIDATED revision); older, unexpired V3 selections not yet admitted get one
  `RESEARCH_ADMISSION_DECLINED` with that reason. Open positions, working entries and V2 cycles
  are untouched; repeated passes write nothing. Admission itself also refuses a superseded V3
  pick under the lock that serializes publication.

Evidence: `tests/test_system_check.py` (41 new tests): each check at its boundaries (exactly 5%
vs just above and below, ±0.2% vs just beyond, exactly 2% vs just below), the stop hit by the
bid or the last trade, the evidence fields; the reader (a stream quote exactly 5 s old is live,
a staler one costs one REST read reused for 5 s, one REST read per tick, a failed read waits
5 s, unusable REST quotes, the research-class declaration); admission end to end through V3
intake, the mock Jev and admission SQL (pullback and immediate admitted with their evidence,
breakout, mismatch, stop-hit by bid and by last and short-stop refused and recorded, a
recorded refusal final without another read, a missing price transient with nothing recorded,
no symbol slot spent, V2 unchanged with no read, a superseded run refused before any read);
the runtime (no ledger read on a tick without V3 work, admission on a fresh stream quote with
no REST read, a stale stream quote costing
exactly one REST read, no live data retried and never declined, a permanent refusal declined
with its evidence and never retried, and a new run retiring the previous run's watching setup
and pending selection while an open position, a working entry and V2 stay untouched,
idempotently). Two research-v3 tests that admitted V3 packets without a live price now pass
one. Full suite and ruff: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### `SYSTEM_CHECK_V1` and `RESEARCH_RUN_SUPERSESSION_V1` (2026-09-27, package system-check)

Owner decisions relayed 2026-09-27 (`docs/CRYPTO-AGENT-LOOP.md` sections 4.3 and 4.4): the
system check happens after Jev's selection, independently of the agent and Jev; a selected pick
that fails it is to be replaced by Jev's next-ranked pick (a later package); breakout entries
trade only after a backtest earns them, and until then are tracked and not traded; stops must
be at least 2% below max entry; a run's picks may trigger until the next run's shortlist goes
live. These are named versions under the owner's 2026-09-24 ruling. They apply only to
`AGENT_RESEARCH_REPORT_V3` selections; V1, the V2, B1 and B2 selection rules, operator
`ENGINEERING_TEST` enrollment, the admission SQL, every trigger, size and exit rule and every
stored packet keep their definitions.

**`SYSTEM_CHECK_V1`.** Scope: a selection whose packet has `report_schema_version:
AGENT_RESEARCH_REPORT_V3`, at admission, after every existing admission check (geometry and
reward-to-risk at max entry, expiry, crypto entry window, halts, selection binding, admission
SQL, classification, active symbol) and the broker price grid, and before the setup row that
spends the symbol slot. A V3 packet without a positive agent price, an aware `run_slot` or the
CRYPTO market is `APP_REVIEWED_PACKET_REQUIRED`. Definitions: entry = `entry_trigger`, M =
`max_entry_price`, S = `stop`, A = the pick's `agent_current_price`; mid = (bid + ask) / 2 of the
live quote. Checks in this order; the first failure is the refusal code and every evaluable
check is recorded as `PASS` or `FAIL` (else `NOT_EVALUATED`):

1. `STOP_DISTANCE_BELOW_MINIMUM` when (M − S) / M < 0.02 (`MINIMUM_CRYPTO_STOP_FRACTION`).
   Evaluated from the levels before any live price is read; a failure reads none.
2. Live price. Bid, ask and quote time from the runtime's authenticated stream observation for
   the symbol when 0 ≤ now − quote time ≤ 5 s (`LIVE_PRICE_MAX_AGE_SECONDS`); otherwise one
   REST read of Alpaca's crypto latest quote through the runtime's market source, live by its
   read time (the quote's own time is recorded, not bounded; a quote timestamped after its read,
   crossed, non-positive or missing is unusable). REST reads are bounded to one per protection
   tick (`REST_QUOTE_READS_PER_TICK`) and one per symbol per 5 s (a success is reused for 5 s, a
   failure waits 5 s). Last = the stream's last trade (price, time, trade ID), when there is
   one. With no usable quote the admission is refused `LIVE_PRICE_UNAVAILABLE`, a transient
   refusal: nothing is recorded for the receipt, the runtime audits it once per runtime,
   selection and reason with the attempts, and every later tick retries.
3. `PRICE_MISMATCH` when |mid − A| / A > 0.05 (`PRICE_MISMATCH_FRACTION`).
4. `STOP_ALREADY_HIT` when bid ≤ S, or last ≤ S.
5. `BREAKOUT_NOT_ENABLED` when the entry type is `BREAKOUT`. Entry type: `PULLBACK` when
   entry − mid < −0.002 × mid, `BREAKOUT` when entry − mid > 0.002 × mid, `IMMEDIATE` otherwise
   (`ENTRY_TYPE_BAND_FRACTION` 0.002; exactly ±0.2% is `IMMEDIATE`). Only `PULLBACK` and
   `IMMEDIATE` are traded.

Comparisons use exact Decimal products (80-digit context), never rounded ratios; exactly 5% and
exactly 2% pass. Recorded fractions (`stop_distance_fraction`, `price_deviation_fraction`,
`entry_offset_fraction`) are rounded half-even to 12 decimal places for reading only. Codes 1,
3, 4 and 5 are permanent (`PERMANENT_ADMISSION_REFUSALS`; the four are
`system_check.SYSTEM_CHECK_REFUSALS`, the replacement trigger): the receipt's one
`SYSTEM_CHECK_REFUSED` (key `system-check-refused:<receipt_id>`: `reason`, `receipt_id`,
`selection_event_seq`, `cycle_id`, `item_key`, `symbol`, `run_slot`, `system_check`) makes the
refusal final (a later admission re-raises it without reading a price), and the runtime
declines the selection, its `RUNTIME_ADMISSION_REFUSED` and `RESEARCH_ADMISSION_DECLINED`
carrying the same `system_check` object. A pick that passes is admitted as before; its WATCHING
state adds `system_check` (result `PASSED`) and `entry_type`. The `system_check` object:
`version` (`SYSTEM_CHECK_V1`), `checked_at`, `result` (`PASSED`, `REFUSED`, `RETRY`), `code`,
`checks` (`stop_distance`, `price_match`, `stop_not_hit`, `entry_type_traded`), `levels`,
`agent_current_price`, `agent_price_at`, the three fractions, the three thresholds,
`entry_type`, `live` (`quote_source` `ALPACA_STREAM` or `ALPACA_REST_LATEST_QUOTE`, `bid`,
`ask`, `mid`, `quote_at`, `read_at`, `quote_age_seconds`, `last`, `last_at`, `last_trade_id`,
`last_source`) and, for `RETRY`, `live_price_attempts`.

**`RESEARCH_RUN_SUPERSESSION_V1`.** The newest run is the latest `run_slot` among all published
V3 selections (`RESEARCH_SELECTED` whose packet is report V3), whatever became of them. Every
protection tick, before admission and whatever the runtime's readiness, while a V3 setup is
`WATCHING` or a V3 selection awaits admission (otherwise nothing is read): each V3 setup still
`WATCHING` whose `run_slot` is older is revoked `SUPERSEDED_BY_NEW_RESEARCH` through the revoke
path (only while still WATCHING under the shared lock: one `REVOKE` keyed
`research:superseded:setup:<setup_id>` with `run_slot`, `superseded_by_run_slot` and
`supersession_rule`, and its `INVALIDATED` revision with `revoked: true` and
`revocation_reason`); each unexpired V3 selection with an older `run_slot`, no setup and no
decline gets one
`RESEARCH_ADMISSION_DECLINED` (key `research:admission-declined:<selection_event_seq>`) with the
same reason and fields. Admission also refuses such a selection with
`SUPERSEDED_BY_NEW_RESEARCH` (permanent), read under the shared lock that also serializes
publication. Setups in any other state (working entries, open positions, closing, closed) and
every non-V3 selection and setup are never touched. With nothing superseded nothing is written;
each retirement is written once.

## Files

New: `src/catalyst_lab/system_check.py` (rules, reader, the newest-run ledger read),
`tests/test_system_check.py` (41 tests), this file.
Changed: `src/catalyst_lab/managed_execution.py` (`admit` takes `live_quote` for V3 packets and
runs the V3 ledger checks and `_system_check`; `_v3_ledger_checks`, `_system_check`,
`_refuse_system_check`, `_recorded_system_check`; `revoke` gains keyword-only `watching_only`,
`details` and `key` and returns whether it revoked, unchanged for existing calls);
`src/catalyst_lab/managed_runtime.py` (the permanent codes, `live_prices` reader,
`_live_quote`, `_retire_superseded_research` every tick, `live_quote` passed for V3 packets
only, refusal and decline bodies carry `AdmissionRefused` evidence, `_decline`);
`tests/test_research_report_v3.py` and `tests/test_research_context.py` (their V3 admissions
now pass a live quote); `docs/MANAGED-RUNTIME.md`, `docs/API-CONTRACT.md`.

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-system-check`),
one pytest process at a time, with about 10 GB free before each full run.

- **Full suite at `d2bd4a9`: 2,747 passed, 0 failed** (7 min 49 s; baseline 2,706 plus the 41
  new tests). `ruff check src tests`: all checks passed.
- An earlier full run (at `383dfdf`) had one failure,
  `tests/test_broker_budget.py::test_ten_watching_and_ten_open_setups_stay_under_150_requests_per_minute`:
  its simulated execution's ledger double answers only the broker-view queries and rejects any
  other, and the first version of the supersession pass read the newest run on every tick.
  The pass now reads it only while V3 work is waiting (`d2bd4a9`); that test passes unchanged.
- Targeted runs during the work: the runtime, V3, engineering, execution, latch, cross-engine,
  price-grid, capacity, audit-volume, failure-isolation, hardening, unattended-safety,
  research-context and complete-cycle modules, all passing after the two research-v3 test
  updates listed under Deviations.

## Deviations

1. **Check order.** The stop-distance check comes first, from the levels alone, so a pick the
   levels already refuse never costs a live-price read and its code never depends on whether a
   price was available; the live checks follow in the table's order. Every evaluable check is
   recorded, so a pick failing several shows all of them; the code names the first.
2. **After the existing checks.** The new checks run after every existing admission check and
   the price grid, so existing codes, their order and their recording are unchanged for V3 picks
   (for example `CRYPTO_LEVEL_OFF_PRICE_GRID` is still recorded as before and is still not in
   `PERMANENT_ADMISSION_REFUSALS`; the selection-topk package may add it).
3. **Freshness.** A stream quote is live when its own timestamp is at most 5 s old (the existing
   trigger's test). A REST quote is live by its read time, following plan 4.4 ("a quote that has
   not changed is still current"): a thin coin's unchanged quote may carry an old timestamp,
   which is recorded (`quote_age_seconds`), not bounded.
4. **"The shared broker/market snapshot."** The runtime holds no quote-bearing shared snapshot:
   the request budget's broker snapshot holds account, positions, orders and activities, and the
   research-context quote cache lives in the app and fills only when an agent reads the context.
   The live price is therefore the stream observation, else the one REST read.
5. **Request budget.** The account request budget governs only the trading host; market-data
   reads go to the data host, which it does not count. The REST quote read is declared in the
   research class (`request_priority(RESEARCH)`) and bounded here instead: one per protection
   tick and one per symbol per 5 s. "The next tick retries" therefore means the stream at once
   and the REST read again after at most 5 s.
6. **Last trade.** The one REST read is the quote, so "last" is the stream's last trade (from the
   current stream session, whatever its age) and `null` when there is none, in which case
   `STOP_ALREADY_HIT` is judged on the bid alone.
7. **Final refusals.** A permanent refusal is recorded once per receipt (`SYSTEM_CHECK_REFUSED`,
   like the grid's `CRYPTO_ADMISSION_REFUSED`), so a direct re-admission re-raises the code
   without reading a price. `LIVE_PRICE_UNAVAILABLE` leaves no receipt record; only the
   runtime's existing once-per-reason audit keeps its first attempt's evidence.
8. **Supersession at admission too.** Besides the runtime pass, admission refuses a V3 pick from
   a run older than any published V3 selection, under the lock publication also takes, so a
   selection published between the pass and the admission loop cannot admit a superseded pick.
   The pass runs whatever the readiness (it only writes the ledger) and reads the newest run
   only while a V3 setup watches or a V3 selection waits, from the tick's own active setups and
   selected packets, so a tick without V3 work costs no extra ledger read (the broker-budget
   simulation's strict ledger double, which answers only the broker-view queries, still passes
   unchanged).
9. **"Newer run" means published.** A run whose every pick later fails the check still
   supersedes the previous one (replacement from Jev's ranking is the later package). Only
   unexpired older selections are declined; an expired one can never be admitted anyway.
10. **`revoke`** gained keyword-only `watching_only`, `details` and `key` and returns a boolean;
    every existing call behaves as before (REVOKE body `{"reason"}` and the same transition).
11. **Two research-v3 tests changed.** `tests/test_research_report_v3.py` (the V3 entry test) and
    the `enter` helper of `tests/test_research_context.py` admitted V3 packets without a live
    price, which now fails closed; each now passes a pullback quote at the agent's own price.
    Their assertions are unchanged (one assertion added: the entry type). No V2, B1, B2 or
    engineering test changed.
12. **No new code for malformed V3 packets.** A V3 packet without a positive agent price, an
    aware `run_slot` or the CRYPTO market is `APP_REVIEWED_PACKET_REQUIRED` (already permanent).
13. **`PERMANENT_ADMISSION_REFUSALS`** is extended by a separate statement after its literal
    (`| system_check.PERMANENT_REFUSALS`), so the selection-topk package's additions to the
    literal merge without touching the same lines.

## Open items and questions

- **Replacement** (later package): replace a selected pick refused with a code in
  `system_check.SYSTEM_CHECK_REFUSALS` by Jev's next-ranked pick; `SUPERSEDED_BY_NEW_RESEARCH`
  is permanent but ends the run, so it should not trigger a replacement.
- **Dependence on selection-topk:** the check reads `report_schema_version`, `run_slot`,
  `state.agent_current_price` and `state.agent_price_at` from the selected packet (copied from
  `RESEARCH_PACKET` today by `_selected_body`); a changed publication must keep them.
- **Read-outs:** `/api/v1/lab/setups` lists only allowlisted state fields
  (`managed_service.STATE_FIELDS`), so `entry_type` and `system_check` show in `/outputs` and the
  position timeline but not there; the research context's `last_run` shows a declined pick as
  `SELECTED` and a superseded setup's `setup_reason` as `null` (it reads `reason`, not
  `revocation_reason`). One-line follow-ups outside this package's scope.
- **Owner live read test:** the REST latest-quote path has only been exercised against a mock
  transport; one supervised read against Alpaca's market data would prove it.
- **Entry type is fixed at admission.** The current trigger (a print at or below the entry)
  still decides the entry; if price later rises above the entry trigger an IMMEDIATE or PULLBACK
  pick behaves as today. The crypto trigger version (phase 4) should read `entry_type`.
- **Breakouts that ran away** (plan 4.3) are covered by `BREAKOUT_NOT_ENABLED` while breakouts
  are off; enabling breakouts needs that check and a trigger version.
- **Not in this package** (plan 4.3): fees and spread in R, tradability now, sector cap,
  capacity and size (phase 4 and the existing entry-time checks).
- **Thin coins:** an unchanged stream quote older than 5 s costs one REST read per admission;
  the phase-4 crypto trigger needs its own read-time freshness rule (a named version).
