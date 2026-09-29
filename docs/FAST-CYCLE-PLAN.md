# Fast-cycle plan: research every 2 hours, shorter trade windows (draft for the owner)

**Paper trading only.** Owner, 2026-09-28: "we need to do shorter windows than 24 hours, maybe a
couple of hours … we'll run the research agent every two hours … the goal is to test as many
trades as possible so we'll clean the engine. The strategy can be changed going forward, but the
system should be resilient for incoming/outgoing … if we do more trades we'll see more issues;
then we can decide about the window: two hours, six, twelve or twenty-four."

The purpose is **engine hardening through volume**, not profit. Every rule change below is a new
named version recorded in CONTRACT-RESOLUTIONS; setups admitted before it keep their rules.

## 1. What changes

| # | Change | Kind | Notes |
| --- | --- | --- | --- |
| 1 | **Research every 2 hours** (12 runs a day, 00:00, 02:00, … 22:00 New York) | Setting | The app already takes up to 24 runs a day (`MANAGED_RESEARCH_SCHEDULE_JSON`). Picks expire at the next run, and a newer run's selection replaces older unfilled picks (`RESEARCH_RUN_SUPERSESSION_V1`). |
| 2 | **Trade window as a setting: `CRYPTO_WINDOW_REVIEW_V1`** | Rule (new version) | See below. |
| 3 | **Intraday research procedure** for 2-hour horizons | Research (new guidelines version) | See below. The full news and outlook research stays once a day (08:00). |
| 4 | **Who runs research every 2 hours** | Decision | See below. |
| 5 | **Capacity checks**: Jev budget, storage, fees | Watch | See below. |

**Change 2 in detail.**
- Today `CRYPTO_24H_REVIEW_V2` reviews a trade 24 hours after its first fill; Jev and the agent
  decide whether to continue for another 24 hours or exit, and a fail-safe exit follows 5 minutes
  after the review window. The new version keeps exactly that machinery but reads the **window
  length from settings**, recorded on each setup at admission (start: 240 minutes, decided 2026-09-28 under the owner's "you decide the window"; `MANAGED_CRYPTO_WINDOW_JSON`).
- The agent's answer stays optional: a silent agent means Jev decides alone, as today.
- Comparing 2, 6, 12 or 24 hours later is a settings change, not new code, and each trade keeps
  the window it started with.

**Change 3 in detail.** Today's picks use daily levels: UNI's target was +24% and LTC's +17%,
which no 2-hour window can reach. The intraday run:
- scans the whole market on 1–15-minute bars and puts entries near the current price (pullbacks
  of about 0.3–1%);
- sizes the stop for 2 hours (about 1–2%) with a target that clears the round-trip fees (0.5%)
  plus at least 1R;
- writes a short cited rationale.

It becomes `MUSE_RESEARCH_GUIDELINES_V7`, with an intraday section. Lessons change emphasis, never
coverage, as today.

## 2. Who runs the research agent every 2 hours (owner decision)

- **A. A scheduled Claude session on the owner's Mac every 2 hours** (recommended to start).
  - It uses the research kit's intraday procedure, and I set it up.
  - It needs the Mac awake, and it uses Claude usage outside the $70 app budget: about
    12 runs a day, each shorter than today's full run.
- **B. The owner's real Muse**, if it can run on a 2-hour schedule. Nothing else changes.
- **C. Both**: Muse at 08:00 for the full daily research, and the scheduled session for the other
  11 runs.

## 3. Capacity (measured 2026-09-28)

- **Jev budget.**
  - Each open trade gets a review every minute, about $0.0002 each, so about $0.29 per trade-day.
    Five trades open around the clock is about $43 a month.
  - Selections are 12 runs × about 40 calls, about $4 a month.
  - The spend guard (`JEV_SPEND_GUARD_V1`) slows reviews before the $50 cap.
- **Storage.**
  - The database is 140 MB after day 1.
  - The biggest steady user is the runtime heartbeat (about 23 MB a day, stored twice).
  - A maintenance review is about 10 KB, plus its audit copy and Jev request.
  - With about five trades open around the clock, the 5 GB volume lasts about 3 weeks. Shrinking
    the heartbeat record is the cheapest fix; do it within the first week.
- **Fees.** 0.25% per side at Alpaca; a 2-hour target must clear 0.5% before any profit.
- **Jev provider stalls.** Bursts of 1–3 minutes several times an hour on 2026-09-28. More trades
  means more reviews lost during a burst (safe: stops are at Alpaca). Ask TypeSafe; the longer
  budget, `JEV_LIVE_REVIEW_POLICY_V2` with migration 025 and the guarded cloud migration, is
  built and waits for the owner-run migration (RAILWAY-DEPLOYMENT.md 7.8).

## 4. Resilience: what more trades will exercise

Entries and partial fills, native stop placement, stop raises (fixed today), bid-touch fallback
exits, window reviews and exits, restarts with open trades (verified today), Jev breaker bursts,
feed gaps, and the risk gate under concurrent trades. Each issue found goes into NEXT-BUILD-PLAN
with its evidence, as today.

## 5. The trailing question (UNI, LTC) — for the learning loop

Both first trades were cut early, then rose:

| Trade | Entry | Exit | High after exit | Last seen | Original stop / target |
| --- | --- | --- | --- | --- | --- |
| UNI | 8.786 | 8.820 | 9.004 | 8.887 | 8.478 / 10.93 |
| LTC | 68.865 | 68.941 | 70.81 | 69.16 | 65.24 / 80.25 |

- Last seen at 17:55 UTC.
- Jev trailed UNI's stop three times in 50 minutes, twice to near the entry.
- Both exits were the app's bid-touch fallback: the bid reached the stop, but no trade did.
- Tonight's jobs run (`UNCHANGED_PLAN_REPLAY_V1`) replays both with their original plans, and the
  evening post-mortem asks whether the stop options (15-minute swing lows) or the bid-touch rule
  should change.
- Two trades are not a sample. The fast cycle supplies the sample, and any change is a named
  version.

## 6. Rollout

1. Build `CRYPTO_WINDOW_REVIEW_V1` (settings-driven window) with tests; full suite.
2. Build the intraday research procedure and guidelines V7 in the research kit, and dry-run it
   against the live context without submitting.
3. Set up who runs research (decision 2).
4. Switch the schedule to 2-hourly and the window to 120 minutes, then deploy.
5. Watch for two or three days; report trades, issues and costs daily.
6. Compare windows: the replay engine can already walk a pick's bars to an exit, so 2, 6, 12 and
   24-hour windows can be compared offline on the same picks before switching the live setting.

## 7. Decisions for the owner

1. **Window:** start at 2 hours, with continuation allowed (Jev may extend by another window) or
   a strict exit at 2 hours?
2. **Who runs research every 2 hours:** A, B or C above?
3. **Size of each run:** about 10 picks per run and at most 5 trades open at once? (Account risk
   limits apply regardless.)
4. **Bid-touch exit and trailing:** observe with more trades first (recommended), or change now as
   a named version?
