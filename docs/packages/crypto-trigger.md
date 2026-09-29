# Package crypto-trigger — the crypto trigger version for Alpaca's thin market (plan phase 4, part a)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-crypto-trigger`, from `work/2026-09-24-product-plan` at `e9c6300`. Plan:
`docs/CRYPTO-AGENT-LOOP.md` sections 4.4, 4.5, 5 and 8 (owner-approved 2026-09-26) and the owner
decisions relayed for this package on 2026-09-27: Alpaca's crypto venue is thin (on 2026-09-26
only 3 of 33 USD pairs were within 10 bps, 32 of 33 within 1%), prints are sparse, and an
unchanged quote is not re-sent, so its own timestamp can be minutes old while it is still the
current quote; to trade the picks, crypto on Alpaca gets a new trigger version. Builds on
`docs/packages/system-check.md` (`LivePriceReader`, `entry_type`), `docs/packages/selection-topk.md`
and `docs/packages/replacement.md`. This file carries the text the coordinator merges into
`docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch.

## PHASES entry (paste as written)

### 2026-09-27 — Crypto trigger version `CRYPTO_ALPACA_TRIGGER_V1` (plan phase 4a, package crypto-trigger) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue, a
mock Jev transport, a fixture universe, and Alpaca market data behind an `httpx` MockTransport
and the real read-only market source. No broker, provider, network or owner-ledger contact; no
migration (the schema is unchanged by this package) and no change to any SQL: the entry quote's
five-second check is the Python authorization gate's. Only crypto setups admitted from report-V3
packets from now on are affected; V1, V2, B1, B2, operator engineering setups and every crypto
setup admitted before this version keep today's trigger exactly.

- **Rule `CRYPTO_ALPACA_TRIGGER_V1`** (new `crypto_trigger.py`, pure). Touch: a print at or
  below the entry trigger T, or a fresh ask at or below T. Confirmation: a fresh ask at or below
  the max entry M, spread (ask − bid) / mid at most `MAX_SPREAD_BPS = 100` (1%; exactly 1%
  passes), a healthy feed, the setup inside its window; the order stays a limit at M. Fresh =
  received on the stream or read over REST at most 5 s ago; the quote's own timestamp is
  recorded, never bounded. Before the trigger: a print at or below the stop
  (`STOP_TRADED_BEFORE_TRIGGER`) or a fresh bid at or below it (`STOP_QUOTED_BEFORE_TRIGGER`)
  invalidates; a touch whose ask is above M invalidates (`PRICE_BEYOND_MAX_ENTRY`, after the
  spread check, as today); a spread above 1% at a touch, or a print touch without a fresh quote,
  waits for the next touch and is recorded (`CRYPTO_TRIGGER_WAIT`, once per setup, reason and
  minute). Decisions compare exact Decimal products.
- **Admission** records `trigger_version: CRYPTO_ALPACA_TRIGGER_V1` in the WATCHING state of a
  crypto setup from a report-V3 packet (any selection rule, the scope of `SYSTEM_CHECK_V1`);
  everything afterwards keys off that state field. `/setups`, `/positions` and `/results` list it.
- **Freshness by read time.** The runtime keeps each stream quote's receipt time (apart from the
  observation rows, so no other record changes). The system check's `LivePriceReader` gained a
  read-time mode: the stream quote if received at most 5 s ago (its `read_at` is the receipt),
  else one REST latest-quote read, fresh by its read time. Trigger and admission reads share the
  reader's limits: one REST read per protection tick and one per symbol per 5 s; stale symbols
  are served in rotation (the symbol read longest ago first). `SYSTEM_CHECK_V1` reads are
  unchanged.
- **Quote-driven evaluation.** Every protection tick, after the queued prints, each WATCHING
  setup of this version is evaluated against its symbol's latest fresh quote: a quoted stop
  invalidates whenever the symbol's market stream is ready (a ledger write only); a touch is
  handed to `observe_trigger` only while entries are ready and outside a capacity cooldown. With
  no setup of this version nothing is read; while nothing touches nothing is written. A print
  touch of this version is confirmed on the same fresh quote (a REST read if the stream's is
  stale) and waits instead of revoking the setup when there is none; its prints above the trigger
  need no quote to be quiet (coalesced). A quote-pass fault latches entries (trigger scope).
- **Entry authorization.** Under the shared lock the version is re-evaluated; a touch that aged
  out while the entry was sized waits (no decision) instead of today's terminal rejection. The
  decision context's `quote_at`, which the gate requires to be at most 5 s old at the claim, is
  the read time; `quote_exchange_at`, `quote_read_at`, `quote_read_basis`, `quote_source` and
  `trigger_version` are recorded beside it.
- **Evidence.** `TRIGGER_CONFIRMED` adds `trigger_version` and a `crypto_trigger` object (touch
  `PRINT`/`QUOTE`, bid, ask, spread in bps, last trade, exchange and read times, read basis and
  quote source); a quote touch carries no `trade_*` fields. The `INVALIDATED` revision and
  `CRYPTO_TRIGGER_WAIT` carry the same object.

Evidence: `tests/test_crypto_trigger.py` (28 tests): the rules at their boundaries (print and
quote touches, exactly 1% vs 1.01%, read-time freshness at 5 s vs just beyond and its fallback,
stop print, quoted stop and stale quoted stop, a touch above max entry vs a wide spread, ignored
and stale prints, the decision quote times, the scope); the reader's read-time mode and its
shared budget; on the fixture venue a print touch with its evidence, a quote touch authorized by
its read time with a 3-minute-old exchange stamp, the gate still refusing a claim 6 s after the
read, the spread boundary with its once-a-minute wait and a later touch, the three invalidations
with evidence and a wide spread that waits, a touch gone stale under the lock (no decision), old
setups unchanged (a 25 bps spread still waits, body and context as today); through the runtime a
top-K pick admitted on a stream quote, quote-touched 10 s later on a quote stamped at admission
and received 4 s before the tick, authorized, claimed, filled and protected; a stale stream
quote costing one REST read (a failed read waits 5 s, no fresh quote means no trigger, then the
REST quote triggers by its read time); a print touch confirmed on one REST read; a print touch
with no quote waiting where the old version is revoked; a quoted stop invalidating while entries
are not ready; ten watching setups sharing one REST read per tick in rotation with nothing
written; admission and trigger sharing the tick's read; nothing read without a setup of this
version; quote-less prints above the trigger coalesced for this version only. No existing test
changed. Full suite and ruff: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### Trigger version `CRYPTO_ALPACA_TRIGGER_V1` (2026-09-27, package crypto-trigger)

Owner decisions of 2026-09-26 and 2026-09-27 (`docs/CRYPTO-AGENT-LOOP.md` 4.4, 4.5, 5 and 8):
Alpaca's crypto venue is thin (on 2026-09-26 only 3 of 33 USD pairs were within 10 bps, 32 of 33
within 1%); prints are sparse, and a quote that has not changed is not re-sent, so its exchange
timestamp can be minutes old while it is still the current quote. To trade the picks, crypto on
Alpaca gets a new trigger version: a 1% spread cap, quote freshness by read time and an ask touch
as well as a printed trade. A named version under the owner's 2026-09-24 ruling. V1
(`CATALYST_RETEST_V1`'s frozen trigger), the managed trigger of every other setup (V2, B1, B2 and
operator `ENGINEERING_TEST` setups, and every crypto setup admitted before this version),
`SYSTEM_CHECK_V1`, the authorization gate, every SQL function and every stored record keep their
definitions; no migration.

**Scope.** A setup admitted from a packet whose `market` is `CRYPTO` and whose
`report_schema_version` is `AGENT_RESEARCH_REPORT_V3` (any selection rule; the scope of
`SYSTEM_CHECK_V1`). Its WATCHING state records `trigger_version: CRYPTO_ALPACA_TRIGGER_V1` in the
admitting transaction; the trigger, the entry re-check, the runtime's evaluation and the decision
context key off that state field only. Definitions: T = `entry_trigger`, M = `max_entry_price`,
S = `stop`; mid = (bid + ask) / 2; spread = (ask − bid) / mid.

**Freshness by read time.** A quote is fresh when it was received on the runtime's authenticated
stream, or read over REST (Alpaca's crypto latest quote), at most 5 s ago
(`QUOTE_MAX_AGE_SECONDS`; 0 ≤ now − read time ≤ 5). Its exchange timestamp is recorded, never
bounded; a quote stamped after its read time, crossed, non-positive or malformed is unusable.
The runtime records each stream quote's receipt time; the quote comes from the system check's
`LivePriceReader` in its read-time mode: the stream quote when received at most 5 s ago (its
read time is the receipt), else one REST latest-quote read, fresh by its read time and reused
for less than 5 s. Trigger and admission reads share the reader's limits (one REST read per
protection tick, `REST_QUOTE_READS_PER_TICK`; one per symbol per 5 s; a failed read waits 5 s);
the trigger pass offers the tick's read to the symbol read longest ago (stream receipt or REST
attempt) first. An observation without a recorded read time (a direct caller) uses its exchange
timestamp as the read time, which can only understate it.

**Evaluation**, in this order, of one observation (a print, a quote, or both) while the setup is
WATCHING and inside its window (expiry and the crypto entry deadline are checked first, as
today):

1. A print timestamped before admission or after now is not evaluated (as today).
2. A print at or below S invalidates: `STOP_TRADED_BEFORE_TRIGGER` (no quote needed).
3. An unhealthy feed invalidates: `DATA_FEED_FAILURE`.
4. A fresh bid at or below S invalidates: `STOP_QUOTED_BEFORE_TRIGGER` (with or without a touch,
   whatever the spread).
5. Touch: a print at or below T at most 5 s old (`PRINT`), else a fresh ask at or below T
   (`QUOTE`). Neither: no touch, nothing recorded.
6. A touch without a fresh quote waits: `FRESH_QUOTE_UNAVAILABLE`.
7. A spread above 1% waits: `SPREAD_ABOVE_MAXIMUM` (`MAX_SPREAD_BPS` = 100; the decision is the
   exact product (ask − bid) × 20000 ≤ 100 × (ask + bid), so exactly 1% passes). It never
   invalidates, even with the ask above M (as today's spread check precedes the max-entry check).
8. An ask above M invalidates: `PRICE_BEYOND_MAX_ENTRY` (only a print touch can see one).
9. Otherwise the trigger is confirmed and the entry is authorized as today (risk, capacity,
   size, one-use five-second decision); the order is a limit buy at M.

Pullback and immediate entries only: breakouts are refused at admission (`BREAKOUT_NOT_ENABLED`).
An invalidation is recorded only while the setup is still WATCHING under the shared lock: its
`INVALIDATED` revision carries `reason` and `crypto_trigger`. A wait writes one
`CRYPTO_TRIGGER_WAIT` per setup, reason and UTC minute (key
`crypto-trigger-wait:<setup_id>:<reason>:<minute>`, only while WATCHING): `{reason, touch,
trigger_version, crypto_trigger}`; the setup keeps WATCHING and the next touch is evaluated
afresh.

**Where it is evaluated.**
- *Queued prints* (the runtime's print path): a print at or below S invalidates as today; a
  print at or below T is evaluated with the freshest quote (the reader, read-time mode); a print
  above T is evaluated alone (no read). Without a fresh quote a print touch waits
  (`FRESH_QUOTE_UNAVAILABLE`, with the reader's `quote_attempts`); the setup is never revoked for
  a missing quote (today's `QUOTE_UNAVAILABLE_AT_PRINT` revocation does not apply to this
  version). A print above T needs no quote to be a quiet print (it joins
  `MARKET_PRINT_SUMMARY` under the other quiet-print conditions). The print processing deadline
  and the consumption record are unchanged (`TRIGGER_CHECKED`).
- *Quote-driven, every protection tick* (after the queued prints and admission, on the tick's
  active setups): each WATCHING setup of this version whose symbol's market stream is ready
  (connected, subscription acknowledged, no gap) is evaluated against the freshest quote; with
  none, nothing happens. A quoted stop invalidates whatever the runtime's readiness (a ledger
  write only); a touch or a wait is handed to `observe_trigger` only while entries are ready,
  outside a capacity cooldown, and a wait at most once per setup, reason and minute per runtime.
  No setup of this version: nothing is read. Nothing touching: nothing is written. A fault
  latches entries (trigger scope) and ends the pass; protection of every setup still runs.
- *Under the shared lock* (`authorize_entry`, after the entry-time broker reads): the
  evaluation is repeated at the lock's time. A touch that aged out meanwhile (its print or quote
  now more than 5 s old) waits (`TOUCH_NOT_CURRENT`, or `FRESH_QUOTE_UNAVAILABLE`, recorded) and
  no decision is written; an invalidation code, a closed crypto entry window
  (`CRYPTO_ENTRY_WINDOW_CLOSED`), reward-to-risk at M below 2 (`MIN_REWARD_RISK`) and malformed
  evidence (`INVALID_MARKET_EVIDENCE`) end the setup `RISK_REJECTED`, as today's re-check does.

**Entry authorization.** The entry decision's context `quote_at`, which the authorization gate
requires to be 0–5 s old when the decision is claimed, is the quote's read time; the context
also records `quote_exchange_at` (the quote's own timestamp), `quote_read_at`,
`quote_read_basis` (`STREAM_RECEIPT`, `REST_READ`, `RECORDED_READ_TIME` or `QUOTE_TIMESTAMP`),
`quote_source` and `trigger_version`. The gate, its five seconds and the one-use claim are
unchanged, and a claim more than 5 s after the read is refused as before. Every other setup's
context is unchanged.

**Evidence object** `crypto_trigger` (in `TRIGGER_CONFIRMED`, the `INVALIDATED` revision and
`CRYPTO_TRIGGER_WAIT`): `version`, `evaluated_at`, `touch` (`PRINT` or `QUOTE`: the entry touch,
or for the stop invalidations the print or quote that reached the stop; else null), `levels`
(T, M, S), `print` (`price`, `at`, `trade_id`, `age_seconds`, or null), `bid`, `ask`,
`spread_bps` ((ask − bid) / mid × 10,000, rounded half-even to 4 places for reading),
`max_spread_bps` (100), `quote_source`, `quote_at` (exchange time), `quote_read_at`,
`quote_read_basis`, `quote_read_age_seconds`, `quote_age_seconds`, `quote_max_age_seconds` (5),
`quote_fresh`, `quote_code` (null, `QUOTE_MISSING`, `QUOTE_INVALID`, `QUOTE_AFTER_READ` or
`QUOTE_NOT_FRESH`), `last`, `last_at`, `last_trade_id` (the stream's last trade, else the print)
and, when the reader found no fresh quote, `quote_attempts`. `TRIGGER_CONFIRMED` of this version
is the observation plus `trigger_version` and `crypto_trigger`; the observation carries the
print's `trade_price`, `trade_at` and `trade_id` only for a print touch, and `quote_read_at` and
`quote_source` beside `quote_at`.

## Files

New: `src/catalyst_lab/crypto_trigger.py` (the rule, the evidence, the decision quote times and
the runtime's observation), `tests/test_crypto_trigger.py` (28 tests), this file.
Changed: `src/catalyst_lab/managed_execution.py` (`admit` records `trigger_version`;
`observe_trigger` and `_trigger_failure` route setups of this version to `_observe_crypto_trigger`
and `_crypto_trigger_failure`; `authorize_entry` waits on the version's wait codes, writes the
version's `TRIGGER_CONFIRMED` body and the decision quote times; new `_crypto_verdict`,
`_locked_crypto_verdict`, `_record_trigger_wait`, `_note_trigger_wait`, `_trigger_confirmed`);
`src/catalyst_lab/managed_runtime.py` (`quote_received` receipt times, `market_message` and
`market_gap`, `_quiet_print` and `_process_print` for this version, `_crypto_trigger_quote`,
`_crypto_print_observation`, `_quote_read_order`, the quote-driven pass
`_evaluate_quote_triggers`, `live_prices.new_tick()` once per tick);
`src/catalyst_lab/system_check.py` (`LivePriceReader.read(..., by_read_time=, received_at=)`,
`rest_attempted_at`; `SYSTEM_CHECK_V1` reads unchanged); `src/catalyst_lab/managed_service.py`
(`STATE_FIELDS` lists `trigger_version`); `docs/MANAGED-RUNTIME.md`, `docs/API-CONTRACT.md`.

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-crypto-trigger`), one
pytest process at a time, with about 11 GB free before each full run.

- Targeted during the work: `tests/test_crypto_trigger.py` 28 passed; `test_system_check`,
  `test_replacement`, `test_research_report_v3`, `test_research_context`, `test_managed_runtime`
  and `test_managed_execution` 193 passed; `test_agent_research_session`, `test_selection_topk`,
  `test_broker_budget`, `test_audit_volume`, `test_managed_hardening`,
  `test_managed_failure_isolation`, `test_unattended_safety`, `test_complete_managed_cycle`,
  `test_acceptance_evidence` and `test_managed_service` all passing.
- **Full suite at `f4a399a`: 2,991 passed, 0 failed** (10 min 34 s; baseline 2,963 plus the 28
  new tests), about 11 GB free before the run.
- **Confirming full suite at `2abca92`** (the final code; later commits touch this file only):
  **2,991 passed, 0 failed** (11 min 3 s), about 10 GB free before the run.
- `ruff check src tests`: all checks passed.

## Deviations

1. **Scope: every report-V3 crypto setup, any selection rule.** The brief says "report-V3
   packets (the top-K loop)". A V3 report is also accepted under the V2, B1 and B2 rules, and
   `SYSTEM_CHECK_V1` (whose `entry_type` this version relies on) applies to all of them, so the
   version applies to every crypto setup from a V3 packet. The parallel package crypto-size-hold
   uses the same scope for `CRYPTO_24H_HOLD_V1`. Restricting it to top-K is one line
   (`crypto_trigger.applies`).
2. **Stream freshness by receipt, in a new reader mode.** The system check's reader judges a
   stream quote by its own timestamp (that is `SYSTEM_CHECK_V1`'s definition, unchanged); the
   brief's "stream if received ≤5 s ago" is this version's rule, so the reader gained a read-time
   mode and the runtime records each stream quote's receipt time. Both modes share the REST
   read, its cache and both limits.
3. **Fallback read time.** An observation without a recorded read time (tests, direct callers,
   the session harness's simulated print) uses its exchange timestamp as the read time. A quote
   cannot be read before it exists, so the fallback never accepts a quote the read-time rule
   would refuse; the existing fixture observation shape keeps working unchanged.
4. **Waits instead of rejections under the lock.** Today a trigger re-check that fails after the
   entry-time broker reads (for example a quote that turned 5 s old meanwhile) ends the setup
   `RISK_REJECTED`. Under this version such a no-trigger outcome is a recorded wait with no
   decision, as the brief's "no fresh quote means no trigger" reads; final codes stay final. The
   gate is unchanged: a claim more than 5 s after the read is still refused (and, as today, ends
   the setup `RISK_REJECTED` with `ENTRY_AUTHORIZATION_REVOKED`).
5. **No revocation for a missing quote at a print.** Today's print path revokes a setup when a
   print arrives with no stream quote (`QUOTE_UNAVAILABLE_AT_PRINT`). On a thin coin a print may
   precede any quote, so under this version the print is confirmed on a REST read or waits; a
   quote-less print above the trigger is quiet. The print deadline (a print older than 5 s when
   processed revokes the setup) is unchanged for every setup.
6. **Ordering "as today".** The spread check precedes the max-entry invalidation, so a wide spread
   with the ask above M waits (today's order). A quoted stop invalidates with or without an entry
   touch and whatever the spread, as the brief states; a stop print needs no quote.
7. **Quoted stops invalidate while entries are not ready.** Invalidation writes only the ledger,
   so the quote pass applies it whenever the symbol's market stream is ready, even if entries are
   not (reconciliation, trade updates, latches); touches wait for readiness. A print at the stop
   keeps today's path (it waits in the queue for readiness or its deadline).
8. **Waits are recorded once per setup, reason and minute** (`CRYPTO_TRIGGER_WAIT`), not once per
   touch, to bound audit volume on a quote that keeps touching with a wide spread; the brief says
   only "record it". The quote pass records no wait when it has no fresh quote (on a thin coin
   that is the normal idle state); a print touch does.
9. **Quote-pass faults latch, they do not revoke.** A print whose evaluation raises revokes its
   setup (today; its evidence is consumed). A quote touch is re-evaluated every tick, so a fault
   in the quote pass latches entries (trigger scope, the existing clear rules) and ends the pass,
   so no later setup is sized without a fresh reconciliation.
10. **Fair rotation of the one REST read.** With more than five stale symbols, plain iteration
    would serve the first five forever (each read is reused for 5 s); the pass orders setups by
    the symbol's last read (receipt or REST attempt), oldest first. The bounds are the reader's,
    unchanged.
11. **`LivePriceReader.new_tick()` once at the start of every protection tick** (it was called
    only when entries were ready, just before admission), so trigger and admission reads share
    one budget per tick. Admission reads first.
12. **A quote touch's `TRIGGER_CONFIRMED` has no `trade_price`, `trade_at` or `trade_id`**; the
    stream's last trade is in `crypto_trigger.last*`. Readers that key on the print
    (`acceptance_evidence.trigger_print_section`, `managed_funnel.execution_quality`'s
    `trigger_to_first_fill_seconds`) report no matched print, or null, for a quote touch rather
    than an unrelated older print.
13. **`STATE_FIELDS` lists `trigger_version`** (so `/setups`, `/positions` and `/results` show
    it). The INVALIDATED revision's `crypto_trigger` evidence is read through the outputs and the
    position timeline, like its `reason`.
14. **No migration and no SQL change.** The entry quote's five-second check is in
    `ManagedAuthorizationGate.claim` (Python, context `quote_at`); no SQL reads `quote_at`.

## Open items and questions

- **Scope confirmation** (deviation 1): all report-V3 crypto setups, or top-K selections only?
- **Gate margin.** A quote read almost 5 s before the claim can still be refused at the gate
  (terminal, as today). The quote pass sees a new stream quote within about a second, and a REST
  read is fresh when used, so the exposure is mainly a print touch confirmed on a stream quote
  received 4–5 s earlier. A margin (for example preferring a REST read when the receipt is older
  than 3 s at a touch) would be a further named version; not built.
- **Quoted stop on a wide book.** A thin coin's bid can sit far below the last trade; a fresh bid
  at the stop invalidates whatever the spread, as the brief states. The owner may want the stop
  quote judged within the spread cap, or on the mid.
- **Market-data read rate.** With five or more stale watching symbols the pass reads REST once
  per protection tick (about 60 per minute to the data host), within Alpaca's documented
  market-data limits but not proven against the real API; the supervised live read test from
  package system-check covers the same endpoint.
- **Analytics follow-up.** `managed_funnel.execution_quality` times the trigger from
  `trade_at`; for a quote touch it could use `crypto_trigger.evaluated_at` or `quote_read_at`
  (outside the trigger path, not changed here).
- **Breakouts.** This version assumes pullback and immediate entries (breakouts are refused at
  admission). Enabling breakouts needs a breakout touch rule as a further named version.
- **Stream gaps.** A crypto market-stream gap still revokes every WATCHING setup
  (`DATA_FEED_FAILURE`), as today. Plan section 5 ("pending setups resume only if price did not
  reach the entry meanwhile") is not part of this version; on a venue whose stream reconnects
  often it would keep more picks alive.
- **Live proof.** Everything here is fixture evidence; the plan's phase-4 proof (a supervised
  paper trade) is still to do, after the owner-run migration of the live ledger.
