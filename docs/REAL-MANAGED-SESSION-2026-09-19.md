# Real managed worker: broker handoff and technical pre-screen

PAPER TRADING — SIMULATED. Not real money.

## Outcome

The real managed worker started locally at 21:03 UTC on September 19. The older
executor was stopped after a fresh flat-account reconciliation and audit verification.
The original schema-8 database was preserved. The new schema-13 ledger contains real
provider observations only, with no fixture candidates, model approvals or fills.

This session reached authenticated Alpaca account access, the paper trade-updates
stream, startup/continuous reconciliation, and a complete technical scan of the
broker's 36 active tradable USD crypto pairs. No pair passed the existing scanner.
Consequently there were **zero Jev reviews, executable setups, risk grants, orders or
fills in this session**. This is not a claim that the entire crypto market has no
opportunities, nor that Jev rejected this batch. The full paper-fill/management/exit
acceptance remains unproven.

## Credential and execution ownership

The owner's existing authenticated project process was verified by UID and working
directory. Only the two required Alpaca fields were transferred in memory into the
replacement process environment. No provider secret was printed or saved to disk.
TypeSafe continues using the existing ignored mode-0600 local credential file.

The prior executor (PID 36988) exited before the replacement began trading work.
The replacement API is loopback port 8768, PID 56858 at this checkpoint. The old
read-only dashboard remains separate. The replacement is a supervised local process,
not a Railway deployment or a verified unattended supervisor. Alpaca credentials are
currently process-local: a future fresh launch still needs secure environment
injection. Do not claim this one-time handoff established durable credential storage.

New database: `~/.local/share/catalyst-retest-lab/managed-real-20260919-a`.
The app uses `catalyst_risk`; the Jev worker uses restricted `catalyst_jev`. The private
dashboard uses `catalyst_app` with read-only transactions and rejects every write verb.
No migration or fixture insertion touched the original runtime ledger.

## Real observations

- Broker equity and previous-close baseline: **$9,999.85**, not a configured $10,000.
- Baseline persisted once for September 19 with source `ALPACA_LAST_EQUITY`.
- Research cycle: `2deee8f0-3689-4050-b242-a2f38f9ec720`.
- 36 pairs screened with real venue metadata, quotes, trades and completed bars.
- Initial pre-screen intentionally supplied no research-news packets. All 36 had at
  least one independent technical/data failure; adding news alone could not qualify them.
- 31 had stale/future quotes; 30 exceeded 10 bps spread; 25 had insufficient completed
  bars; 6 had bar gaps/order violations; 1 had stale completed bars. Reasons overlap.
- BTC's observed quote was fresh (about 1.43 seconds, 1.68 bps), but its completed-bar
  series failed the continuity check. Thus the earlier three-quote stale sample was
  not treated as proof that the feed is always stale.
- No thresholds, geometry, provider timestamps or missing bars were altered to force a trade.

The actual market-stream watcher subscribes after a valid selection. There were no
selections, so crypto trade/quote subscription and trigger delivery were **not proven**
by this run. The connected broker trade-updates stream is a different connection.
An unauthenticated diagnostic subscribe returned 401; it did not establish feed quality
and was not substituted for the authenticated execution stream.

Original-source research was checked separately, including Solana's current changelog.
It was not presented as a complete 20–30-candidate research batch or used to invent
missing technical eligibility. No unchanged Jev packet was rerolled for approval.

## Changes and visible evidence

- `managed_runtime.py`: exposes the actual reconciliation timestamp, clearing it on
  a market gap. This small code fix is tested; the already-running executor has not
  been restarted solely to reload an observability change.
- `managed_service.py`: adds scanner counts and per-instrument reasons to cycle
  readback and a collapsed dashboard section. Scanner failures are not Jev rejections.
- `scripts/serve_managed_dashboard.py`: serves the current real ledger at
  <http://127.0.0.1:8769>, without a broker credential or a second execution worker.
  Worker status comes from the running API; reconciliation time comes from its
  persisted clean reconciliation event. Loss of worker status displays UNAVAILABLE.
- Regression tests cover reconciliation timestamp invalidation, scanner/model
  distinction, read-only database transactions, auth and refused dashboard write verbs.

The entire previously collected suite passed **941 tests**; the two subsequently added
dashboard tests also passed (**943 tests total**). Lint and `git diff --check` passed.
Ten browser checks passed against the **real-data** dashboard, including mobile layout,
all 36 exclusion rows, running worker status, zero positions and no browser errors.

Artifacts: `artifacts/real-managed-session-2026-09-19/` contains broker preflight,
handoff checkpoint, complete scan observations, browser proof, desktop/mobile screenshots,
event export and independent hash verification.

## Audit checkpoint

At 21:12:04 UTC the new ledger verified through **297 events**:

`68e8d5c4b2a9ea0d7ba8e7a2e60855bf0edd9cabdefe00f4f6afe8fa2f4efbc8`

Reconciliation event 290 at 21:11:47 UTC: clean; zero broker orders, zero broker
positions, no mismatches. Local setups, fills and risk reservations were also zero.
The worker continues appending heartbeats/reconciliation, so this is a retained
checkpoint, not an assertion that the chain has stopped growing.

The preserved original ledger verified at handoff with head:

`35e1fe8b503c41491bfa040e5c9615362ed79de8f4907331ee3e7058c8548ea9`

No calendar exit, protective amendment, test R, MFE or MAE is claimed without a fill.
India remains research-only. This launch is crypto-only; US session acceptance,
Railway deployment and the external Muse connection remain separate work.
