# Package gap-resume — pending setups survive a stream gap or an app restart (plan phase 8, reliability part)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-gap-resume`, from `work/2026-09-24-product-plan` at `8b96ec0` (schema 23).
Plan: `docs/CRYPTO-AGENT-LOOP.md` section 5 ("Alpaca stream drops or the app restarts → Resume
from the ledger; fetch fills missed while down; resting stop-limits stayed at Alpaca; pending
setups resume only if price did not reach the entry meanwhile") with sections 4.4 and 7 (phase 8,
restart tests), owner-approved 2026-09-26. Builds on `docs/packages/crypto-trigger.md`
(`CRYPTO_ALPACA_TRIGGER_V1`, recorded as `trigger_version` in the WATCHING state), the fill
backfill after reconnect (plan 4.4, PHASES September 24) and unattended safety (lease loss exits
75, PHASES 2026-09-26). This file carries the text the coordinator merges into `docs/PHASES.md`
and `docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch. No migration.

## PHASES entry (paste as written)

### 2026-09-27 — Pending setups survive a stream gap or a restart: `CRYPTO_GAP_RESUME_V1` (plan phase 8 reliability, package gap-resume) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, the fake paper venue with
Alpaca-shaped FILL account activities, fake clocks, fake trade-updates and market-stream
sockets, scripted Alpaca crypto quotes and one-minute bars behind an `httpx` MockTransport and
the real read-only market source, a mock Jev transport. No broker, provider, network, launchd
or owner-ledger contact; no migration (schema stays 23) and no SQL change. The package sends
no broker order of its own: it reads bars (GET) and ends or resumes setups in the ledger; every
broker POST, DELETE and PATCH keeps its exact one-use five-second authorization. Only crypto
setups of `CRYPTO_ALPACA_TRIGGER_V1` admitted from now on are affected; every other setup (V1,
V2, B1, B2, US stocks, operator engineering setups and every crypto setup admitted earlier) is
still revoked `DATA_FEED_FAILURE` on a gap and at startup, with today's records.

- **Version `CRYPTO_GAP_RESUME_V1`** (new `gap_resume.py`): admission records
  `gap_resume_version` in the WATCHING state of every crypto setup of `CRYPTO_ALPACA_TRIGGER_V1`;
  the runtime keys off that field only.
- **Held, not revoked.** A market gap (the stream ended, or `start()`, which gaps both markets)
  holds such a setup where today it is revoked (at the next protection tick; inside `start()`
  before any loop starts): one `GAP_RESUME_PENDING` (window, basis), no
  trigger evaluated (the quote pass skips it; its queued prints are consumed
  `GAP_CHECK_PENDING` instead of being evaluated or aged into a revocation), so no entry can
  follow. The market's gap still clears only once every WATCHING setup of it is revoked or held
  with its record written (a failed record keeps entries blocked and is retried, as a failed
  revocation is today).
- **Window.** In process: from the gap (the runtime's time of the `RUNTIME_MARKET_GAP`). After a
  restart: from the previous runtime's last recorded observation of the market: the `gap_resume`
  section of its last `RUNTIME_HEARTBEAT` (new, taken under the runtime lock: `as_of` when the
  market was observed, else the start of the gap it had open, else unknown → admission). Never
  later than an earlier hold still open in the ledger (a runtime that died before its check) or
  than the setup's earliest queued print never evaluated; never before admission.
- **One check.** When the symbol is acknowledged on the stream again, trade updates are
  connected and this process's reconciliation is clean and fresh, and the minute in which the
  stream came back has closed plus 30 s (`BAR_SETTLE_SECONDS`), the protection tick reads
  Alpaca's completed one-minute bars from the window's minute to the last minute that closed
  30 s ago (new `AlpacaMarketSource.window_bars`, GET-only route, taking the tick's one
  market-data REST read of `LivePriceReader`, new `spend_read`: at most one check a tick) and
  every print the stream delivered for the setup since the window's start (the tail after the
  last bar was watched live). Exact Decimal comparisons of the lowest traded price: bars
  unavailable, incomplete or malformed → revoked `DATA_FEED_FAILURE`; else at or below the stop
  → `INVALIDATED` `STOP_TRADED_DURING_GAP`; else at or below the entry trigger → revoked
  `ENTRY_REACHED_DURING_GAP` (no late entry); else `GAP_RESUMED` and the trigger evaluates it
  again. Evidence (`gap_resume`): window, bars read (count, first/last, lowest low and when,
  SHA-256), prints considered, lowest price, decision, gap basis. A gap that begins during the
  read discards the result; a setup whose window ends while held expires as today.
- **Missed entry fills.** For a setup of this version, a protection tick that finds the broker
  holding its coins with no recorded buy fill (the trade-updates stream missed it in an outage
  or a restart) runs the existing REST fill backfill once before the fail-closed first-fill
  rule, so the fill is recorded from Alpaca's activities (`ALPACA_PAPER_REST_BACKFILL`) and
  protected once. Before, a new process's protection tick, which starts beside the stream loop,
  could reach that position before the reconnect's backfill and halt
  `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE` and force an exit (the "Not covered" item of the
  September 24 fill-backfill entry). Every other setup keeps that rule unchanged.
- **Status and watchdog.** `status()` (and the HTTP status) carries `gap_resume` (held setups
  with window, `stream_back_at` and `due_at`; each market's observation); `RUNTIME_HEARTBEAT`
  records it (its `as_of` alone is not a change). The watchdog raises
  `GAP_RESUME_CHECK_OVERDUE` when a setup has been held more than 300 s and
  `GAP_RESUME_STATUS_UNAVAILABLE` for an unreadable section; the other alarms are unchanged.

Evidence: `tests/test_gap_resume.py` (19 tests): the rule at exact Decimal boundaries (low at
the trigger, one hundred-millionth above it, at the stop), unavailable, incomplete, malformed,
duplicate or out-of-window bars and an unreadable print failing closed, tail prints, the window
arithmetic and the restart bound; the watchdog boundary at 300 s and its fail-closed cases; the
explicit-window bar read (one GET, the open minute excluded, invalid arguments refused). Through
the runtime on a disposable ledger: an in-process crypto-stream and trade-updates drop with a
maintained trade and five setups (held; the V2 setup revoked exactly as today; an entry filled
while trade updates were down backfilled and protected by the protection tick; the ask and a
print at the entry after the reconnect buying nothing; one bar read per setup resuming one,
revoking one `ENTRY_REACHED_DURING_GAP`, invalidating one `STOP_TRADED_DURING_GAP` and revoking
one `DATA_FEED_FAILURE`; the resumed setup triggering again); an abrupt restart (no
`RUNTIME_STOPPED`, lease released) where the successor's first protection tick, before any
stream, backfills the fill made while down and protects it once, the maintained trade keeps its
resting stop-limit and levels, maintenance raises its stop with one claimed PATCH, and each held
setup is checked once from the previous runtime's last heartbeat; restart windows bounded by a
gap the last heartbeat reported open, by an earlier runtime's open hold and by an unevaluated
queued print (which alone decides `ENTRY_REACHED_DURING_GAP`); V2, US-stock and pre-version V3
setups revoked exactly as today in process and at restart; the HTTP status and the overdue
alarm; expiry during a gap; a gap during the bar read; one market-data read per tick shared with
the trigger; the fill backfill for this version only; a failed hold record retried or released.
No order is ever sent twice; the audit chain verifies. No existing test changed. Full suite and
ruff: see "Validation".

## CONTRACT-RESOLUTIONS text (paste as written)

### Gap resume version `CRYPTO_GAP_RESUME_V1` (2026-09-27, package gap-resume)

Owner-approved plan `docs/CRYPTO-AGENT-LOOP.md` section 5 (2026-09-26): "Alpaca stream drops or
the app restarts → Resume from the ledger; fetch fills missed while down; resting stop-limits
stayed at Alpaca; pending setups resume only if price did not reach the entry meanwhile". A
research run's setups wait up to about 24 hours for their entry, and until now one stream blip
or restart (including the lease-loss exit 75 that launchd restarts) revoked every WATCHING
setup `DATA_FEED_FAILURE`. A named version under the owner's 2026-09-24 ruling. V1, the managed
gap revocation of every other setup, `CRYPTO_ALPACA_TRIGGER_V1`, `CRYPTO_24H_HOLD_V1` (its hard
exit from the first recorded buy fill), the authorization gate, every SQL function and every
stored record keep their definitions; no migration.

**Scope.** A crypto setup admitted under `CRYPTO_ALPACA_TRIGGER_V1` (from a packet whose
`market` is `CRYPTO` and whose `report_schema_version` is `AGENT_RESEARCH_REPORT_V3`, any
selection rule). Its WATCHING state records `gap_resume_version: CRYPTO_GAP_RESUME_V1` in the
admitting transaction; the runtime keys off that state field only. Definitions: T =
`entry_trigger`, S = `stop`; a minute bar is Alpaca's completed one-minute crypto bar (trades)
from `/v1beta3/crypto/us/bars`.

**Gap.** A market gap is what `ManagedRuntime.market_gap` records (`RUNTIME_MARKET_GAP`): the
market stream ended (disconnect, provider error, overflow, subscription mismatch or timeout,
out-of-order data), or a runtime started (`RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION`, both
markets). Its bound (the start of the unobserved window) is the runtime's time of the gap; at a
startup it is the previous runtime's last recorded observation of the market: the `gap_resume`
section of the newest `RUNTIME_HEARTBEAT` written by another runtime, taken under that
runtime's lock at `as_of`: `as_of` when the market was observed then (connected, subscriptions
acknowledged, no unprocessed gap), else the start of the gap it had open (`unobserved_since`),
else (unknown, no such heartbeat, a heartbeat without the section, `as_of` after now, or the
ledger unreadable) no bound.

**Hold.** Where today's gap revocation runs (the protection tick after a gap; inside `start()`
before any worker loop starts), each WATCHING setup of this version in the gapped
market is held instead of revoked: its window start is the gap's bound, taken no later than the
`window_start` of any `GAP_RESUME_PENDING` of the setup after its last `GAP_RESUMED` (an earlier
hold still open) and no later than the trade time of its earliest queued `MARKET_PRINT` never
consumed, and no earlier than its admission (with no bound: its admission). One
`GAP_RESUME_PENDING` per hold records it; a setup already held keeps its window through further
gaps. While held no trigger is evaluated for it: the quote-driven pass skips it and each queued
print is consumed `GAP_CHECK_PENDING` (not evaluated, not aged into a revocation). The market's
gap, which blocks entries, clears only when every WATCHING setup of the market is revoked or
held with its record written; a failure is retried the next tick.

**Check** (once per hold). It runs at a protection tick when all hold: the setup's symbol is
acknowledged on the market stream (no newer gap since the tick that first saw it back, R);
the trade-updates stream is connected; this process's reconciliation is clean and at most
`reconcile_seconds` old; now ≥ R rounded up to the minute + 30 s (`BAR_SETTLE_SECONDS`); the
setup's entry window is open (otherwise it expires as today); and the tick's one market-data
REST read (`LivePriceReader`, shared with admission and the crypto trigger) is still available.
It reads the minute bars starting in [the window start's minute, the last minute that closed at
least 30 s before now) with one GET, and every `MARKET_PRINT` of the setup (prints the stream
delivered) with a trade time from the window start to now. Decision on the lowest traded price
L over those bar lows and prints, exact Decimal comparisons, in this order:

1. The bar read failed, was incomplete (a pagination limit or cycle, an invalid row, an
   unexpected symbol), returned a malformed bar (not one minute, low above high or outside
   open and close, non-positive, duplicate, outside the window), or a recorded print is
   unreadable: `DATA_FEED_FAILURE` (revoked, fail closed, as today).
2. L ≤ S: `STOP_TRADED_DURING_GAP` (invalidated).
3. L ≤ T: `ENTRY_REACHED_DURING_GAP` (revoked: the price reached the entry while the system
   could not act; there is no late entry).
4. Otherwise (including no trade at all): `GAP_RESUMED`; the setup is WATCHING under its
   trigger again.

If a new gap of the market begins while the bars are read, the result is discarded and the
setup waits for the stream again. The records are written under the shared lock only while the
setup is still WATCHING: `GAP_RESUMED` (key `gap-resume:resumed:<setup_id>:<runtime_id>:
<pending_since>`, body the evidence); the `INVALIDATED` revision with `reason` and
`gap_resume`; or `REVOKE {reason, gap_resume}` (keyed likewise) with the `INVALIDATED` revision
carrying `revoked` and `revocation_reason`. Evidence `gap_resume`: `version`, `decision`,
`checked_at`, `window` (`start`, `end`, `bars_start`, `bars_end`, `stream_back_at`,
`bar_settle_seconds`), `levels` (T, S), `bars` (`source`, `timeframe`, `count`, `first_at`,
`last_at`, `lowest_low`, `lowest_low_at`, `sha256`, `issues`, `problems`), `prints` (`count`,
`lowest`, `lowest_at`, `lowest_trade_id`, `lowest_event_seq`), `lowest`, `gap` (`reason`,
`started_at`, `basis`, `pending_since`, `runtime_id`).

**Missed entry fills.** For a setup of this version, when `manage` finds the broker holding its
coins before it is marked open and no buy fill of it is recorded, it runs the REST fill backfill
(`rest_backfill`, the reconnect's own) once before the first-fill rule; the backfill records
every missed fill it finds (deduplicated by broker order and cumulative quantity), and if the
setup's first buy fill is then recorded the hold's clock starts from it. Otherwise, and on any
failure, the rule applies as before (`CRYPTO_FIRST_FILL_TIME_UNAVAILABLE`, halt and exit). Every
other setup keeps the rule unchanged.

**Status.** `status()` carries `gap_resume`: `version`, `as_of`, `markets` (`observed`,
`unobserved_since`), `pending_count`, `oldest_pending_since`, `overdue_after_seconds` (300) and
the held setups (at most 50). `RUNTIME_HEARTBEAT` records it; its `as_of` alone never makes a
heartbeat a change. The watchdog raises `GAP_RESUME_CHECK_OVERDUE` when the oldest held setup was
held more than 300 s ago (an unreadable or future time too) and `GAP_RESUME_STATUS_UNAVAILABLE`
for an unreadable section.

## Files

New: `src/catalyst_lab/gap_resume.py` (the version, the window and restart bounds, the decision,
the ledger reads and writes), `tests/test_gap_resume.py` (19 tests), this file.
Changed:
- `src/catalyst_lab/managed_runtime.py`: `GapCheck`; `market_gap` records each market's gap
  bound, unobserved period and gap count (at startup the restart bound from the ledger,
  `_restart_bound`); `_invalidate_market_gaps` holds setups of this version
  (`_hold_for_gap_check`) and revokes every other as before; `_run_gap_checks`, `_gap_check`,
  `_gap_bars`, `_gap_check_ready`, `_gap_pending`, `_release_gap_check`; the tick runs the checks
  after supersession; `_process_print` consumes a held setup's prints `GAP_CHECK_PENDING`; the
  quote pass skips held setups; `status()` `gap_resume` (`_gap_resume_status`); the heartbeat
  signature ignores its `as_of`. `start()` itself is unchanged.
- `src/catalyst_lab/managed_execution.py`: `admit` records `gap_resume_version`
  (`gap_resume.admission_fields`); `manage` runs `_backfill_unrecorded_entry_fill` for a setup
  of this version before the first-fill rule.
- `src/catalyst_lab/scan_sources.py`: `AlpacaMarketSource.window_bars` (explicit window, GET);
  `_bars` accepts an explicit `start` (every existing caller unchanged).
- `src/catalyst_lab/system_check.py`: `LivePriceReader.spend_read` (the per-tick REST read for
  another market-data GET; reads unchanged).
- `src/catalyst_lab/managed_service.py`: `STATE_FIELDS` lists `gap_resume_version`,
  `STATUS_FIELDS` lists `gap_resume`.
- `src/catalyst_lab/managed_app.py`: the HTTP status maps `gap_resume`.
- `src/catalyst_lab/managed_ops.py`: `gap_resume_alarms` (`GAP_RESUME_CHECK_OVERDUE`,
  `GAP_RESUME_STATUS_UNAVAILABLE`) in `status_alarms`.
- Docs: `docs/MANAGED-RUNTIME.md`, `docs/OPERATIONS-RUNBOOK.md` (restart section, watchdog
  table), `docs/API-CONTRACT.md`.

## Validation

All in the worktree's own venv (`XDG_DATA_HOME=~/.local/share/catalyst-wt-gap-resume`).

- `tests/test_gap_resume.py`: 19 passed.
- Related suites during the work (`test_managed_runtime`, `test_managed_failure_isolation`,
  `test_crypto_trigger`, `test_maintenance_runtime`, `test_audit_volume`, `test_selection_b1`,
  `test_unattended_safety`, `test_fill_backfill`, `test_managed_day_policy`, `test_system_check`,
  `test_scan_sources`, `test_managed_service`, `test_managed_ops`, `test_crypto_size_hold`,
  `test_trade_maintenance`, `test_maintenance_session`, `test_replacement`,
  `test_managed_execution`, `test_managed_runtime_latches`, `test_complete_managed_cycle`,
  `test_managed_app`): 635 passed.
- The restart proof fails with the pre-halt fill backfill disabled (the successor halts
  `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE`), so that change is what makes "fills missed while down
  are backfilled" hold when the protection tick runs first.
- **Full suite at `90529d2`** (the final code; later commits change docs only): **3,317 passed,
  0 failed** (11 min 31 s; the baseline 3,298 plus the 19 new tests), about 9.5 GB free before
  the run.
- `ruff check src tests`: all checks passed.

## Deviations and decisions

1. **Trade bars only.** The rule reads trade bars (the brief's bar lows) and the stream's
   prints. An ask that touched the entry during the gap without a trade at or below it is not
   visible; Alpaca's historical crypto quotes could close that as a further version.
2. **Window end.** A minute bar exists only once its minute has closed, so the check waits for
   the minute in which the stream came back to close plus 30 s, reads bars up to the last minute
   closed 30 s before the check, and covers the rest (at most about 90 s, watched live by the
   stream while the setup was held) with the prints the stream delivered: every print at or below
   the trigger is recorded (only prints above it are coalesced). A quote-only touch in that tail is
   not acted on either; once resumed, a touch that is still there is a live trigger.
3. **30 s settle** (`BAR_SETTLE_SECONDS`) before the last minute is read, so a bar Alpaca has not
   published yet cannot hide a trade. The real publication delay is not measured (open item).
4. **The first minute is read whole.** A minute cannot be split, so a trade earlier in the
   window's first minute (before the gap, or before admission) counts. The rule can only end
   more setups because of it, never fewer.
5. **Restart window from the heartbeat.** "The previous process's last recorded heartbeat or
   observation, never later than the gap" is implemented as the heartbeat's new `gap_resume`
   section (taken atomically under the runtime lock), bounded by an open earlier hold and by
   the setup's queued prints never evaluated. Observations recorded after the last heartbeat
   (for example a print summary) are not used to start the window later, so a window may start
   up to about a minute before the previous process stopped: more bars read, never fewer. A
   heartbeat from an earlier release has no such section and gives no bound (admission).
6. **Incomplete bars decide first.** With the read incomplete, the setup is revoked
   `DATA_FEED_FAILURE` even if a bar it did get shows the stop; both end it.
7. **Readiness for the check.** The brief's conditions (symbol acknowledged, trade updates
   authenticated and connected, a clean reconciliation of this process, at most
   `reconcile_seconds` old). The check does not wait for `ready()` (research health, latches):
   it only reads bars and writes the ledger; an entry after resumption still needs `ready()`.
8. **Shared read budget.** The check takes `LivePriceReader`'s one REST read per protection tick
   (the existing market-data bound), running before admission and the trigger in the tick, so
   while checks remain an admission or trigger REST fallback waits a tick (the stream is still
   read every tick).
9. **Missed entry fills, this version only.** The pre-halt REST backfill is part of this version
   (plan 5, "fetch fills missed while down"); it is the reconnect's account-wide backfill, so a
   missed fill of another setup found by it is recorded too, exactly as a reconnect would record
   it. Without it, the proof of "fills missed while down are backfilled" held only when the
   trade-updates stream connected before the first protection tick, which production does not
   guarantee.
10. **Startup unchanged.** `start()` is untouched: `market_gap` computes the restart bound when its
    reason is `RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION`.
11. **A failed hold record keeps the gap open.** Like a failed revocation today, entries stay
    blocked until the record is written; the setup is held (no trigger) from the first attempt.
12. **No new event for the stop outcome.** `STOP_TRADED_DURING_GAP` is an `INVALIDATED` revision
    carrying `reason` and `gap_resume`, as the trigger's own stop invalidations are; the two
    revocations go through the existing keyed revoke path.

## Open items and questions

- **Live proof.** Everything here is fixture evidence: Alpaca's real bar publication delay,
  its bars for thin coins and a real restart under launchd (exit 75, reboot) are still to be
  observed in the supervised paper run, after the owner-run migration of the live ledger.
- **Quote touches during a gap** (deviation 1): a further named version could read Alpaca's
  historical crypto quotes for the window.
- **The print deadline outside a gap.** A WATCHING setup whose print waits more than 5 s because
  the runtime is not ready (research heartbeat failing, reconciliation stale, a latch) is still
  revoked `DATA_FEED_FAILURE` (`PRINT_PROCESSING_DEADLINE`), for this version too once resumed.
  That is not a market gap and is unchanged here; it could be held the same way.
- **Other setups' missed entry fills.** Setups outside this version still halt
  `CRYPTO_FIRST_FILL_TIME_UNAVAILABLE` if the protection tick sees an entry fill the stream
  missed before a reconnect's backfill records it.
- **Overdue threshold.** 300 s; a long stream outage raises it together with
  `CRYPTO_MARKET_STREAM_LOST`. The owner may prefer another value.
