# Measurement and public reporting — local implementation

2026-09-19. Schema 7 is running locally. The separate read-only dashboard is at
http://127.0.0.1:8766. It is not a public HTTPS deployment.

## Measurement

- Every broker fill retains its own source event. The projection uses broker-event quantities/prices,
  fill references and order/candidate attribution. Partial fills are weighted; duplicates do not add P&L.
- Gross closed P&L is sale proceeds minus entry cost. Fees, market impact, latency slippage and queue
  position are not modeled. `net_r` and conservative-adjustment fields remain unavailable.
- Market subscriptions continue after the trigger. Trade ticks, sampled quotes and minute bars retain
  provider/feed/timestamps. New snapshots and calendar rows must match their immutable audit source.
- MFE/MAE use in-position printed trades and minute bars fully contained within the holding interval.
  Quotes are not excursion prices. Updated bars supersede their earlier version in measurement only.
  Excursions are price differences per share from entry VWAP; favorable is nonnegative, adverse nonpositive.
- Snapshot coverage is explicitly OBSERVED_ONLY or NO_OBSERVATIONS. Gaps are not zeros. IEX is visibly
  IEX-only, not consolidated. The older SPY engineering trade had no in-position snapshots; none were invented.
- A fixed SQL function alone rebuilds the disposable `trades` projection. App/reporting roles cannot
  directly insert/update/delete it. Source ledgers, projection checkpoints and daily revisions remain append-only.
  Projection changes are audited with their input sequence boundary; refresh with no new evidence is a no-op.
- Broker correction/bust events mark the affected result REVIEW_REQUIRED. Affected aggregates/curve are
  withheld rather than quietly omitting that trade. Reconciliation requires an explicit future correction procedure.
- Rollups run after the persisted exchange close and catch up after restart, by candidate session/version.
  Changed results append a daily revision. The strategy is intraday; these are session-cohort rollups, not a
  general overnight cashbook. The background worker checks every 15 seconds and reports failures in health.

## Official R remains a pending owner ruling

Three explicitly labeled calculations exist: actual filled risk (filled quantity × entry VWAP-minus-stop),
planned filled risk (filled quantity × M-minus-stop), and original authorized order risk. They differ on
slippage and partial fills. No one has been silently selected. Official average R, R curve and MFE_R/MAE_R
remain null until a reporting denominator is approved. Tests select a named definition explicitly.

The headline equity curve is inception baseline plus verified strategy gross P&L, not account cash.
Engineering P&L, deposits/withdrawals and manual research cannot reset or alter it. Selecting a statistics
window changes aggregate metrics but never resets the all-time curve or all-time closed-trade drawdown.

## Public surface

The dashboard process has no broker credentials or execution client and requires `catalyst_reporting`.
It shows verified US/Alpaca Paper by default; all engineering records are excluded. Candidate identities,
detail pages and CSV use DB-enforced official-close disclosure, with missing calendar data hidden.
Aggregate statistics may update intraday without ticker-level records. Late-added records are marked.
The two historical Phase 1 rejected smoke candidates remain visible with their original fixture theses;
no history was deleted or relabeled. There are no completed strategy trades in the current ledger.

Research tabs are separate MUSE_MANUAL ledgers. The operator CLI imports completed India/Crypto records,
with currency and original source reference; reported-price P&L is computed locally but explicitly unverified.
Corrections append a linked event and replacement observation. Publication begins the next New York day.
There is no cross-market headline, currency conversion, manual US import or Muse outcome-write endpoint.

## Run and deploy

```sh
./run catalyst-lab dashboard --port 8766
./run catalyst-lab measure
./run catalyst-lab research-import --file /path/to/operator-owned-research.json
# To correct a manual record, add --correction-of ORIGINAL_EVENT_UUID
```

The `serve` command runs measurement alongside the configured market/broker workers. `measure` performs
one local refresh/rollup without broker requests. Keep one market worker process per database.

`Dockerfile.dashboard` packages only the read-only web surface on port 8080. For public deployment:
choose a host/domain, provision a reachable PostgreSQL database with migrations applied by its owner,
set a TLS/SCRAM-protected `REPORTING_DATABASE_URL` for the reporting role, and terminate HTTPS at the host.
Do not expose the private API or pass broker/Muse credentials into the dashboard container. Keep the
trusted ingestion/measurement worker connected to the same ledger and configure retained off-host backups.
Local Unix-socket trust is not a cloud authentication design. No host/database migration is authorized
by merely building the container. Public hosting and secure Muse connection remain pending selection.

## Verification

292 tests pass with disposable PostgreSQL and external TCP blocked; lint and JavaScript syntax pass.
Wheel contents include migrations and static assets. Docker deployment itself was not tested.
Browser checks cover desktop/mobile (390px without document overflow), research tabs, candidate detail,
late-added labels, filters and curve controls; no browser console errors were observed.

Before local migration: direct paper GETs confirmed zero positions/orders; local exposure/reservations
were zero. Database dump and full audit export are retained outside the repository in
`~/.local/share/catalyst-retest-lab/runtime/phase56-backup-20260919T130238Z/`.
After restart: clean reconciliation, measurement worker healthy, historical baselines intact, zero
exposure. The engineering projection shows 5 shares, 759.46 entry, 759.43 exit, -0.15 gross dollars,
excluded from the headline. Its earlier FAILED_CLOSED outcome is unchanged. No new orders were sent.

Machine-readable local evidence: [phase56-runtime-evidence.json](phase56-runtime-evidence.json).
