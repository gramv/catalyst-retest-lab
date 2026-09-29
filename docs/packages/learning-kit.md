# Package learning-kit — the research side of the learning loop

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-28-learning-kit`, from `work/2026-09-24-product-plan` at `15dc5b0`. The plan
is `docs/LEARNING-LOOP-PLAN.md`, sections 2, 5, 5b and 5c, approved by the owner on
2026-09-28. The package is built against package learning-app's contract, all on
`pkg/2026-09-28-learning-app`:

- `docs/API-CONTRACT.md`, "Learning loop", at `133a7a4`, plus `lessons.available` at
  `65b5e17`;
- the intake at `99d961d`;
- `MUSE_RESEARCH_GUIDELINES_V6` at `391a720`.

It also follows the coordinator's decisions of 2026-09-28, sent to both packages.

This file carries the text the coordinator merges into `docs/PHASES.md`. Neither that file
nor `docs/CONTRACT-RESOLUTIONS.md` is edited on this branch. No app code, schema, migration,
setting or trading rule changed. Only `research_agent/`, its tests and its documentation did.

## Summary

The external research kit `research_agent/` gains the research side of the learning loop:
the morning outlook, the evening review of what moved and why, Muse's own research
checklist, and the lessons.

1. **Excerpt matching fix.**
   - The bug: `sources.visible_text` drops an inline tag without a space, but
     `verify_excerpt` read every tag as a space. So an exact cut such as `rose <b>12%</b>,`
     came back `NEAR_MATCH` (0.87), and `build` dropped the item. The research helpers hit
     this on 2026-09-28.
   - The fix: verification now also searches the visible text exactly as `cut_excerpt`
     cuts it. It keeps the two texts it searched before, and searches each one separately.
   - Nothing is loosened: the comparison and thresholds are unchanged, and a match that
     spanned the seam between two texts, which the old code accepted, no longer verifies.
2. **`movers --day YYYY-MM-DD` (or `--hours N`) → `movers.json`.** It uses Coinbase public
   1-hour candles for every coin in the context's universe, over the New York day (23 or 25
   hours across a daylight-saving change) and the 7 days before it. Per coin:
   - the return, and the largest move up and down;
   - the volume per hour against the 7-day average;
   - the bar where the move started, by two rules: the contract's `MARKET_REALITY_V1` rule
     (the trough before the high) on 1-hour bars, and the first bar past half of the move
     (the rule this task specified);
   - the technical state at the day's start.

   The movers are the 5 largest rises, the 5 largest falls and every coin with a return of
   5% or more either way. A bar counts only if it had ended at retrieval. A day that has not
   ended is refused before any fetch.
3. **`outlook`, `outlook-check`, `outlook-submit` → `MARKET_OUTLOOK_V1`.**
   - The worksheet has one entry per universe coin, pre-filled with:
     - the price and the distances to the day's levels (with the scorecard's distance
       bucket);
     - the return, the volume and the technical state tags;
     - the cited `news.json` items;
     - the matching checklist items.

     Rebuilding keeps every answer already filled.
   - The send refuses:
     - an unfilled coin, or a SKIPPED coin without a reason;
     - an uncited NEWS or EVENT reason;
     - the agent's ID in its own text;
     - everything the contract refuses.
   - It reads the research context again before sending, so a coin that joined the universe
     is caught before the POST.
   - It re-fetches every cited page: the excerpt must verify, and `published_at` must be one
     of the page's own metadata times.
   - It stamps `generated_at` just before the POST and never prints the token.
   - It saves the exact bytes it sends before the POST, and records every outcome,
     including transport errors.
   - After no answer, it resends the saved bytes unchanged first, and rebuilds under the
     same `outlook_id` only on `MARKET_OUTLOOK_STALE_OR_FUTURE`.
   - It never sends a recorded outlook again; `--new-id` is only for a deliberate second
     outlook.
   - It saves the context it reads as `context.json`, for `outlook` and `build`.
4. **`postmortem`, `postmortem-check`, `postmortem-submit` → `POST_MORTEM_V1`.**
   - The subjects are exactly the context's `lessons.pending_post_mortems`, with `was_miss`
     as the app gives it: notable trades, then movers (misses first), then the optional
     trades with no notable reason.
   - A pending entry with a gap keeps its subject, with a note. For example, a trade with
     no recorded exit fill has its window end when the lessons were read. No single subject
     can refuse the worksheet.
   - The worksheet is built only once `lessons.recent_days` records the day. Otherwise it
     stops with `REALITY_DAY_NOT_RECORDED` rather than build a partial worksheet.
   - Each subject gets:
     - its price window;
     - the bar holding the app's reference time;
     - the kit's measures of the move;
     - the computed pre-move technical state, with a draft of at most 300 characters.
   - The **knowable-before-move helper** compares each cited `published_at` with the app's
     reference time: `move_start_at` for a mover, the first entry fill for a trade. It
     suggests `true` only when a source was published at or before that time, which is the
     app's `KNOWABLE_BEFORE_MOVE_UNSUPPORTED` rule. It never guesses a missing time.
   - The notes carry up to 30 items each. Items already accepted are not resent.
5. **`checklist show | update | export`.** This is Muse's own memory, in an owner-only state
   folder outside the repository. The rules are guidelines V6's:
   - **TECHNICAL** patterns are scored on the whole universe. The occurrences are the
     start-of-day state tags from `movers.json`. The hits and the mover share come from the
     app's `recent_days`, never recomputed. A pattern is judged by **lift**: `ACTIVE` at 10
     or more occurrences with lift ≥ 2.0 overall and over the last 10; `RETIRED` below 1.25.
   - **NEWS_TYPE, EVENT, MARKET_FACTOR and SOURCE_QUALITY** patterns come only from the
     post-mortems the app accepted, as `postmortem-submit.json` records them, and only
     from notable trades and movers. The latest accepted item of a subject counts. A hit is
     `knowable_before_move: true`. `ACTIVE` at a hit rate of 50% or more, overall and over
     the last 10; `RETIRED` below 40%.
   - A retired pattern may return under the same rule. Every change is logged.
   - Evidence is append-only, idempotent and audited: a changed occurrence is corrected, and
     one no longer given is withdrawn, each by a new entry. Nothing is edited.
   - The export only orders checks.
6. **`lessons` → `emphasis.json`; `build --lessons`.**
   - `lessons` prints `RESEARCH_LESSONS_V1` plainly:
     - fill and reach rates by distance, and results by timeframe, rule and kind;
     - excerpt drops, stale-news vetoes and causes;
     - the outlook's hit rate and calibration, misses and false alarms;
     - the pending post-mortems.
   - It writes ordering hints, which need at least 5 resolved picks in two buckets and a
     0.20 spread.
   - `build --lessons` applies its 30-pick limit first, so the same coins are picked with or
     without hints. It then orders the picks by the hints. Each promoted pick says so in
     `why_over_peers`, without ever writing a `rule-A` or `rule-B` tag, because the
     scorecard reads those. The applied hints are recorded in `build-notes.json`.
7. **The procedure and prompts.**
   - `DAILY_PROCEDURE.md` has a Morning part and an Evening part, and both ways to run a
     day: two sessions (08:00, and after 02:00 New York), or one at 07:15 that reviews
     yesterday first.
   - The morning sends the outlook before the report, as guidelines V6 says.
   - Every existing citation rule is kept word for word.
   - `DAILY_PROMPT.md` has a morning prompt, an evening prompt and a combined 07:15 prompt.
   - `docs/OPERATIONS-RUNBOOK.md`'s kit section lists the new commands.
8. **Two fixes the loop needed.**
   - **`build` declares the coming run when inside its grace.** It took the context's
     `current_run_slot`, so a report prepared at 07:15 answered yesterday's 08:00 run and
     expired at 09:00. `build.run_slot_for` now uses the app's `ResearchSchedule`: within
     the grace before a run, it answers that run. The outlook uses the same rule.
   - **Session builds declare guidelines V6.** `build` pins V6's version and SHA-256 for
     session mode.

## How it is wired to the app's contract

Everything is wired. Nothing is left waiting on learning-app.

| Kit step | App contract | Checked against |
| --- | --- | --- |
| `outlook-submit` | `POST /api/v1/lab/market-outlooks`, `MARKET_OUTLOOK_V1` | Local checks that mirror `learning_intake`. After the merge, also `learning_intake.MarketOutlook` itself (`records.app_model_problems`). |
| `postmortem-submit` | `POST /api/v1/lab/post-mortems`, `POST_MORTEM_V1` | The same, with `PostMortemEnvelope` and `PostMortemItem` |
| `lessons`, `postmortem`, `checklist update` | `RESEARCH_CONTEXT_V2` `lessons` (`RESEARCH_LESSONS_V1`) | The contract's field names; fixtures in its shape |
| the same, when `lessons.available` is `false` (65b5e17) | `{…, available: false, code: "LESSONS_UNAVAILABLE"}` | `context.usable_lessons` means "no lessons this run": `lessons` says so and writes an empty `emphasis.json`, and `build --lessons` builds unchanged. `postmortem` has no subjects and never overwrites a worksheet with answers. `checklist update` skips the TECHNICAL scoring and says so. |
| checklist rules | Guidelines V6, "Research checklist" | V6's text (391a720): the same numbers. Post-mortem evidence comes only from the items the app accepted (`postmortem-submit.json`), as the app's own lessons count them. |
| `build` session fallback | `MUSE_RESEARCH_GUIDELINES_V6`, `075acc02…` | A pin, compared with the app's constant after the merge |

Four tests compare the kit with learning-app's code. They skip on this branch, because
`catalyst_lab.learning_intake` and V6 are not in it:

- the outlook body against `MarketOutlook`;
- the notes against `PostMortemEnvelope` and `PostMortemItem`;
- the pinned limits and vocabularies (230 coins, 2 MiB, 30 items, 512 KiB, the reason kinds,
  the directions and the causes);
- the V6 pin.

All four were run against learning-app's code by placing its `learning_intake.py` and
`muse_guidelines.py` in this worktree for the run only, then restoring them. They passed
both times:

- at `99d961d`, with every outlook, post-mortem and build test (83 passed);
- at `65b5e17`, with all 289 research-agent tests (none skipped);
- after the review fixes, at learning-app's `6127482`, with all 300 research-agent tests
  (none skipped).

## Deviations and choices (all approved by the coordinator on 2026-09-28)

1. **Sources.** The kit requires a source on NEWS and EVENT reasons; the contract makes them
   optional.
2. **Publish times.** Outlook and post-mortem sources are fetched again, and a non-null
   `published_at` must equal one of the page's own metadata times. `build`'s `news.json`
   path is unchanged.
3. **`outlook-submit` reads the research context again** before sending.
4. **The outlook is sent before the report**, as guidelines V6 says. The task listed it
   after `submit`.
5. **`movers.json` has two move-start fields.** `move_start` follows `MARKET_REALITY_V1`'s
   rule on 1-hour bars; `half_move_bar` is the task's rule. Both are for reading. The
   helper uses only the app's `move_start_at`.
6. **Post-mortem subjects are exactly the app's pending list.** The kit never adds a
   subject: the app refuses a mover it did not record.
7. **The evening timing.**
   - The evening runs in `runs/<day>/evening`, after 02:00 New York.
   - It stops unless `recent_days` records the day.
   - When the latest graded outlook is from an earlier day, that is a note, not a stop.
     With forward-window grading, a day can simply have no outlook due for grading.
8. **The kit's reading of V6: "the last 10 occurrences" are counted in whole days.** A
   TECHNICAL day brings many occurrences at once, with no order among them. So the window
   takes whole days, newest first, until it holds at least 10 (`checklist.recent`).
9. **Retirement applies to ACTIVE patterns only, for every kind.** A candidate simply stays
   a candidate. For TECHNICAL, V6 says so explicitly; for the factor kinds it says only
   "RETIRED when the last-10 hit rate falls below 40%".
10. **`build` and the outlook answer the coming run inside its grace**
    (`build.run_slot_for`, with the app's `ResearchSchedule`). From 07:00 they declare
    today's 08:00 run. Without this, a 07:15 report answers yesterday's slot: it expires at
    09:00, and it cannot supersede the earlier run's setups, because supersession needs a
    newer slot. The behaviour changes only inside the grace window.
    `test_a_report_prepared_within_the_grace_answers_the_coming_run` builds a report at 07:15
    New York and passes the app's own intake check; the outlook's run slot is tested the
    same way.
11. **Kit constants (research-side, not trading rules).**
    - The hint evidence: 5 resolved picks, a 0.20 spread, and the 7-day lines before the
      30-day ones.
    - The hint metrics: the shadow trigger rate by distance, the shadow hit rate otherwise.
    - The technical state tags:
      - `VOLUME_SPIKE`: 24-hour volume at least 2x its 7-day average;
      - `NEAR_7D_HIGH` and `NEAR_7D_LOW`: within 2%;
      - `TREND_UP_7D` and `TREND_DOWN_7D`: 10% or more;
      - `TIGHT_RANGE_24H`: a range of 3% or less.
12. **An undecided required subject blocks the send.** Required means every notable trade
    and every mover. It fails closed, so nothing owed is silently left out. A subject that
    cannot be researched can still be decided: `NO_NEWS` or `SURPRISE`, with
    `knowable_before_move: null`. A trade with no notable reason is optional: it is not sent
    unless decided, and it never counts in the checklist.

## Review fixes (read-only review pass, 2026-09-28)

A review of `15dc5b0..b1e72c2` found four defects and one minor one. Each is fixed with a
test.

1. **Outlook resend after a lost reply.**
   - The defect: every `outlook-submit` rebuilt the body (new `retrieved_at` and
     `generated_at`). So after a lost reply the app answered
     `OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH`, and the advice to use `--new-id` recorded a
     second graded outlook. `outlook-submit.json` was not written after a transport error.
   - The fix: the exact bytes are saved before the POST. After no answer they are resent
     unchanged first; only `MARKET_OUTLOOK_STALE_OR_FUTURE` leads to a rebuild under the
     same ID. A recorded outlook is never sent again, and `--new-id` warns that it adds a
     second outlook. Every outcome is written.
   - Tests: `test_a_send_with_no_answer_is_resent_byte_for_byte_and_recorded_once`,
     `..._found_stale_proves_the_first_never_landed_...`,
     `test_an_idempotency_refusal_means_already_recorded_...` and
     `test_a_refused_outlook_is_fixed_and_sent_again_under_the_same_id`.
2. **A pending trade with a null exit blocked every post-mortem.**
   - The defect: `PENDING_TRADE_INVALID` refused the whole worksheet, and `Decimal("None")`
     crashed.
   - The fix: the subject is kept, with its window ending at `lessons.as_of`, no kit
     measure and a note. Every Decimal conversion is guarded, and no subject refuses the
     worksheet.
   - Tests: `test_a_trade_with_no_recorded_exit_keeps_its_subject_and_blocks_nothing` and
     `test_malformed_pending_fields_are_notes_never_a_whole_worksheet_refusal`.
3. **Checklist factor evidence came from the raw worksheet.**
   - The defect: that counted refused and unsent items, `true`s the app would reject, and
     non-notable trades, and a rebuild could lose factors.
   - The fix:
     - The evidence now comes only from items the app ACCEPTED in `postmortem-submit.json`,
       which stores each sent item's factors and the app's answer.
     - TRADE subjects count only when notable, and `undecided()` no longer requires the
       optional trades.
     - The worksheet is refused as input.
     - Corrections are audited: a later accepted note corrects or withdraws by appending,
       never editing.
   - Tests: `test_only_items_the_app_accepted_count_...`,
     `test_a_later_accepted_note_corrects_and_withdraws_...`,
     `test_a_rebuilt_movers_day_corrects_its_own_day_only`,
     `test_a_trade_with_no_notable_reason_is_optional_...` and
     `test_cli_update_refuses_an_unknown_input_and_the_worksheet`.
4. **The universe-change advice.**
   - The defect: "run `outlook` again" rebuilt from the old `context.json`, so the new
     coin never appeared.
   - The fix: `outlook-submit` (and `outlook-check` with `--base-url`) save the context
     they read as `context.json`, and `outlook` then adds the coin.
   - Test: the extended `test_cli_outlook_submit_refuses_before_posting`.
5. **Minor: 1 and 0 were accepted as `knowable_before_move`.** Only `true`, `false` or
   `null` are accepted now, as in the app (`test_knowable_before_move_is_only_true_false_or_null`).

## Files

- **New in `research_agent/`:**
  - `technicals.py`
  - `movers.py`
  - `outlook.py`
  - `postmortem.py`
  - `checklist.py`
  - `lessons.py`
  - `records.py`
- **Changed in `research_agent/`:**
  - `sources.py`: the excerpt fix, `check_excerpt`, `page_published_times`, `check_source`
    and `check_sources`;
  - `market.py`: `fetch_hourly_span`;
  - `submit.py`: `post_json`, and the outlook and post-mortem routes;
  - `context.py`: V2, `lessons_of` and `usable_lessons`;
  - `build.py`: `--lessons` ordering, `run_slot_for`, the V6 pin, and the run folder's
    `run_id`;
  - `run.py`: the new commands;
  - `__init__.py`;
  - `DAILY_PROCEDURE.md`;
  - `DAILY_PROMPT.md`.
- **Docs:**
  - `docs/OPERATIONS-RUNBOOK.md`: the kit section;
  - this file.
- **Tests, new:**
  - `tests/learning_kit_fixtures.py`
  - `test_research_agent_technicals.py`
  - `test_research_agent_movers.py`
  - `test_research_agent_checklist.py`
  - `test_research_agent_outlook.py`
  - `test_research_agent_postmortem.py`
  - `test_research_agent_lessons.py`
  - `test_research_agent_daily_flow.py`: the whole day through the CLI.
- **Tests, extended:**
  - `test_research_agent_sources.py`
  - `test_research_agent_submit.py`
  - `test_research_agent_context.py`
  - `test_research_agent_build.py`

## Validation

| Command (prefix `XDG_DATA_HOME=~/.local/share/catalyst-wt-learning-kit`) | Result |
| --- | --- |
| `./run pytest -q tests/test_research_agent_*.py` | 296 passed, 4 skipped (the merge-time checks) |
| The same, with learning-app's `learning_intake.py` and `muse_guidelines.py` (`6127482`) placed in the worktree for the run | 300 passed, none skipped |
| The full suite, guarded (`./run pytest -q -p no:cacheprovider`), on `68c4e60` | 4,190 passed, 5 skipped (the 4 merge-time checks, and one existing `test_cloud_provision` skip), 12 min 33 s |
| `./run ruff check src tests research_agent` | All checks passed |

`ruff check scripts` flags one line of `scripts/import_managed_costs.py` at 101 characters
(E501). It is outside `src` and `tests` (the checks `AGENTS.md` requires), this package did not
touch the file, and the error is already present at `15dc5b0`.

Fixture evidence only:

- `httpx.MockTransport` and monkeypatched fetches stand in for every network boundary: the
  app's routes, Coinbase and the news pages;
- the checklist is exercised in temporary owner-only folders.

Nothing was contacted: no broker, provider, Railway, live app or owner ledger, and the
default `muse-state` folder was not touched.

## PHASES entry (paste as written)

### 2026-09-28 — The research side of the learning loop: movers, the morning outlook, the evening post-mortems, the research checklist and lessons in the kit (package learning-kit) (FIXTURE EVIDENCE ONLY)

The owner approved the learning loop on 2026-09-28 (`docs/LEARNING-LOOP-PLAN.md`). This
package is its research side, in `research_agent/`, wired to package learning-app's contract
(`MARKET_OUTLOOK_V1`, `POST_MORTEM_V1`, `RESEARCH_CONTEXT_V2` lessons, guidelines V6).

**Morning:**

- `lessons` prints the agent's own scorecard lines and writes ordering hints. `build
  --lessons` ranks picks by them only after its 30-pick limit, so no coin is ever dropped
  because of a lesson.
- `checklist export`.
- `outlook` builds a worksheet with one entry per universe coin, pre-filled with the kit's
  facts. `outlook-submit` sends it before the report. It refuses unfilled coins, uncited
  news and event reasons, and publish times that are not the page's own. It reads the
  context again first.

**Evening**, after the nightly job has recorded the day:

- `movers` shows what moved in the whole universe.
- `postmortem` builds a worksheet for exactly the post-mortems the app says are owed, with
  a knowable-before-move helper that suggests true only with a source published by the
  app's reference time. `postmortem-submit` sends the notes.
- `checklist update` scores patterns by guidelines V6: TECHNICAL by lift against the app's
  mover share, the factor kinds by hit rate. It counts only the post-mortems the app
  accepted. Every correction is appended, never edited.

**Fixes:**

- Cutting and verifying excerpts now agree across inline tags next to punctuation.
- A report prepared within the grace before a run answers that run.
- Session builds declare V6.

**From a review pass:**

- An outlook resend after a lost reply sends the same bytes and is never recorded twice.
- A pending trade with no recorded exit keeps its subject and blocks nothing.
- The checklist counts only accepted, notable post-mortems.
- `outlook` sees a coin that joined the universe.

**Procedure.** `DAILY_PROCEDURE.md` and `DAILY_PROMPT.md` document both ways to run a day:
two sessions, or one at 07:15.

**Evidence.** Fixture evidence only. The merge-time tests that compare the kit with the
app's own models and constants pass against learning-app's code at `6127482`.

**Not shown.** Any real run: no call to the app, Coinbase or a news page was made through
these commands.

## CONTRACT-RESOLUTIONS text

None. The package is a client of learning-app's contract and changes no rule, version,
threshold or route. The checklist rules are guidelines V6's. The kit's constants (hint
evidence, state tags) are research-side emphasis, which the plan leaves to Muse
(section 2.1).

## Open items

1. **`docs/MUSE-CONNECTION.md` section 8.** It is not edited here, because learning-app owns
   that file in this round; the coordinator applies this text at integration. Section 8 is
   unchanged on learning-app's branch at `6127482`.

   Replace its step table with:

   ```markdown
   | Step | What it does |
   | --- | --- |
   | `context` | `GET /api/v1/lab/research-context` with the token, into `context.json` (V2: with `lessons`) |
   | `lessons` | Prints the lessons plainly; writes `emphasis.json`, ordering hints for `build` |
   | `checklist export` / `update` / `show` | The research checklist, Muse's own memory outside the app (guidelines V6's rules) |
   | `market` | Public Coinbase candles for the listed coins |
   | `levels` | A pullback setup per coin, by the level rules |
   | `outlook`, `outlook-check`, `outlook-submit` | The morning outlook for every coin; `POST /api/v1/lab/market-outlooks`, sent before the report |
   | `build` | The report V3, every excerpt re-fetched and checked, every pick run through the app's own models; `--lessons` orders picks by the hints |
   | `validate` | Re-checks `report.json` with the app's models |
   | `submit` | `POST /api/v1/lab/research-reports` |
   | `movers` | What moved over a New York day, for the evening |
   | `postmortem`, `postmortem-check`, `postmortem-submit` | The evening post-mortems of `lessons.pending_post_mortems`; `POST /api/v1/lab/post-mortems` |
   ```

   Then three more changes in the same section:

   - After the table, add: "The kit's three writes are `submit`, `outlook-submit` and
     `postmortem-submit`."
   - Under "Claude-specific", replace "Step 4 of the procedure, the web research written into
     `news.json`" with "Morning step 7 and evening step 4 of the procedure, the web research".
   - Under "Missing for Muse", delete the bullet that begins "`build` stamps
     `generated_at` when it starts". It is fixed on this base: `submit` restamps just before
     the POST, and so does `outlook-submit`.
2. **Two data sources for movers.** The kit's movers come from Coinbase 1-hour bars, the
   app's from Alpaca 1-minute bars. So the kit's `movers.json` can differ at the margin
   from the app's recorded movers.
   - This affects reading only. Subjects, `was_miss`, the hits and the mover share all come
     from the app.
   - One small bias remains: a coin the kit tags but the app does not measure counts as a
     TECHNICAL occurrence without a hit.
3. **The session harness** has no outlook, lessons or post-mortem support. Its workflow
   covers the report only, as before.
4. **First real run.** Check these the first time the kit runs against the deployed app,
   after learning-app is merged and deployed:
   - `outlook-submit`'s receipt;
   - an evening run after the nightly job, with its `REALITY_DAY_NOT_RECORDED` stop;
   - the real `published_at` metadata of the news pages Muse cites.
5. **Not tooled.** Plan 5b.3's news-date checks (when Jev vetoes a pick as stale) are in
   the procedure, but not tooled. The factor table of plan 5c is the app's
   (`recent_days.factors`); the kit does not compute its own.
