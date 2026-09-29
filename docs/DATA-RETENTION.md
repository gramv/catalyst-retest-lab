# Data retention (owner decision delegated, 2026-09-28)

Owner, 2026-09-28: "we don't need to retain for months and months … is it needed for training …
you decide how long we need to retain the data".

## The constraint

The ledger is tamper-evident. Every `lab.trade_events` row carries the hash of the one before it,
and triggers refuse UPDATE, DELETE and TRUNCATE on the event tables. The app's roles cannot
delete, and removing rows would break the proof that history was not altered (AGENTS.md: "never
rewrite event history"). So retention means **writing less, and archiving closed periods with a
verified checkpoint**, never deleting rows in place.

## Measured on 2026-09-28

- The database was 140 MB after the first live day, on a 5 GB volume.
- The largest writer was `RUNTIME_HEARTBEAT`: a regression wrote one every 5 s instead of once a
  minute, about 23 MB a day, plus its copy in `trade_events`. Fixed by making a pass timestamp
  not count as a status change.
- A per-minute maintenance review is about 10 KB in `managed_events`
  (`POSITION_REVIEW_REQUEST` about 9 KB plus the decision). Its context is also in the Jev request
  and the audit copies.

## Policy

| Data | Kept | Why |
| --- | --- | --- |
| Trades, orders, fills, fees, P&L, risk decisions and halts | Forever | The experiment's results and audit. Small. |
| Research reports, picks, Jev's answers and receipts, outlooks, post-mortems, scorecards | Forever | The training and learning data. Small. |
| Heartbeats, reconciliations, print summaries, market snapshots | 30 days in the database, then archived | Only needed to debug recent behaviour. |
| Full per-minute review contexts (the bars and quotes sent to Jev) | 90 days in full, then their hash and key figures | Rebuildable from market data; the hash keeps the proof. |

## How (to build; none of it deletes rows in place)

1. **Write less (done).** One heartbeat a minute. Next, store each review's context once and
   reference it by hash; today it is written up to four times. Do this before the fast cycle
   multiplies reviews.
2. **Archive closed periods.**
   - An owner-run command, like `ledger-migrate` and with a backup first, exports a closed period
     (all of its setups terminal) to a compressed, hash-verified file kept off the database.
   - It records an archive checkpoint (last sequence and hash) so `verify_events` continues from
     the checkpoint, then removes that period under the owner role.
   - It is built together with the missing guarded cloud migration command (NEXT-BUILD-PLAN).
3. **When.** After the heartbeat fix, growth is a few MB a day at today's volume. Start archiving
   when the database passes about 60% of the volume, or monthly once the fast cycle runs.
