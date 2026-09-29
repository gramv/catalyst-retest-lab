# Daily research procedure

**Paper trading only.** This is the procedure a scheduled Claude session follows every day to
research the crypto market and to learn from the results (`docs/LEARNING-LOOP-PLAN.md`
sections 5, 5b and 5c; `MUSE_RESEARCH_GUIDELINES_V6`). Research happens outside the app
(`docs/MUSE-RESEARCH-BOUNDARY.md`); this kit calls the app's HTTP API with the agent's own
token and never edits the app.

The day has two halves:

- **Morning** (the 08:00 America/New_York run, `docs/CRYPTO-AGENT-LOOP.md` 4.1): read the
  lessons and the research checklist, research every coin, send a `MARKET_OUTLOOK_V1` for
  every coin, then send one `AGENT_RESEARCH_REPORT_V3`.
- **Evening** (after the app has recorded the New York day that just ended): see what moved,
  research why, send the post-mortems as `POST_MORTEM_V1` notes, and update the checklist.

Between them, when the app's schedule runs research every 2 hours, a short **intraday run**
answers each of the other slots (00:00, 02:00, … 22:00 New York, all but 08:00) with a
chart-first report built under the `INTRADAY_V2` profile. See "Intraday run (every 2 hours)"
below.

Everything is deterministic and mechanical except the research itself (morning step 7,
evening step 4), which only a research session can do: Claude reading and judging real
pages. Follow the steps in order. Each writes into one run folder, so a whole run can be
inspected afterward.

## Two ways to run the day

**A. Two sessions.**

- The morning session starts at 08:00 New York.
- The evening session reviews day D after **02:00 New York** on day D+1. The app's nightly
  job (`30 5 * * *` UTC: 01:30 New York in summer, 00:30 in winter) records day D's market
  reality: every coin's move, the movers, where each move started, and the outlook grades.
  Post-mortems of D's movers are refused until it has (`REALITY_DAY_NOT_RECORDED`).
- Folders: the morning uses `runs/<D>`, the evening `runs/<D>/evening`.

**B. One session at 07:15 New York.** It runs the evening steps for yesterday, then the
checklist update, then the morning steps for today, so the checklist the morning exports
already holds last night's evidence.

- Yesterday's evening folder is `runs/<yesterday>/evening`; today's morning folder is
  `runs/<today>`.
- Send the outlook and the report **after 07:00**. A run's report and outlook may be prepared
  at most 60 minutes (the schedule's grace) before the run they answer. From 07:00 the kit
  declares today's 08:00 run (`build.run_slot_for`). Before 07:00 it would declare
  yesterday's run, whose picks expire at 09:00 today.

## Before you start

- **The app's base URL** (`--base-url`):
  - the Railway trader's `https://` address when the app runs in the cloud (see
    docs/RAILWAY-DEPLOYMENT.md);
  - or `http://127.0.0.1:<port>` for an app on this Mac.

  The kit refuses plain `http://` to any other host.
- **The agent's bearer token**, in one of two places:
  - an owner-only mode-0600 file (`RESEARCH_AGENT_TOKEN_FILE`, preferred; the kit refuses a
    file anyone else can read);
  - or an environment variable (`RESEARCH_AGENT_TOKEN`).

  **Never paste the token into a command, a file this procedure writes, or your own
  output.**
- **`--agent-id` and `--agent-version`** for this agent. The app's patterns apply: the ID is
  lowercase letters, digits, `_` or `-` (2-32 characters, starting with a letter); the version
  is at most 32 letters, digits, `.`, `_`, `+` or `-`. `build` refuses anything else before it
  builds (`AGENT_ID_INVALID`, `AGENT_VERSION_INVALID`), since the intake would refuse the whole
  report. Bump the version whenever your method changes (guidelines V6).
- **Run every command from the repository root** (or a checkout of it) with `./run`, so the
  project's own Python and dependencies are used:
  `./run python -m research_agent.run <command> ...`.
- **The run folder.** All the steps of one half share a run folder. Pass `--run-dir`
  explicitly and use the same path for every step, or rely on the defaults:
  - the morning: `runs/<YYYY-MM-DD>` in America/New_York's date (make sure you do not cross
    midnight ET partway through);
  - the evening commands that take `--day`: `runs/<day>/evening`;
  - an intraday run has no default: always pass its own folder (see "Intraday run").
- **The research checklist** lives outside the repository, in an owner-only folder
  (`--state-dir`, default `~/.local/share/catalyst-retest-lab/muse-state/`).

## Morning

### 1. Read the research context

```sh
./run python -m research_agent.run context --base-url "$BASE_URL" \
  --token-file "$TOKEN_FILE" --run-dir "$RUN_DIR"
```

This is `GET /api/v1/lab/research-context`. It returns:

- the coins Alpaca can trade right now (USD pairs, no stablecoins), each with a live
  bid/ask/spread/increment;
- your own open trades and your recent outcomes;
- the schedule and the report limits;
- your **lessons** (`RESEARCH_CONTEXT_V2`).

Read `context.json`. The `universe.coins` list is the *only* set of coins you may pick from,
and the outlook needs an entry for every one of them.

If the app is not reachable yet (development only), add `--offline` instead of
`--base-url`/`--token-file`. This uses Alpaca's public quotes with no key, but `build`
refuses to turn the result into a submittable report (it has no schedule or report format)
unless you pass `--session`. See "Testing against a session harness" near the end of this
file for that separate workflow.

### 2. Read the lessons

```sh
./run python -m research_agent.run lessons --run-dir "$RUN_DIR"
```

This prints your own scorecard lines plainly, for 1, 7 and 30 days:

- how often price came back to your entries at each distance;
- how each timeframe, level rule and pick kind did on its shadow outcomes;
- your graded outlooks (direction hit rate, calibration, misses and false alarms);
- the post-mortems you still owe.

It writes `emphasis.json`: **ordering hints only**, such as "rank setups closer than 2%
first". A hint needs at least 5 resolved picks in two buckets and a spread of at least 0.20
between the best and the worst.

If the app could not read your lessons this run (`lessons.available: false`, code
`LESSONS_UNAVAILABLE`; the rest of the context is still served), `lessons` says so and writes
an `emphasis.json` without hints. `build --lessons` then builds exactly as without it.
Research the whole market as usual.

Lessons change emphasis, never coverage (guidelines V6):

- Study the whole universe, and aim for 20 picks.
- Never drop a coin, a pick kind or a timeframe because of a lesson.
- Never try to change a trading rule.
- Judge a setup type only from the cases it explains: leave out trades whose post-mortem
  cause is `MARKET_WIDE` or `SURPRISE`, and read every rate with its count.

### 3. Export the research checklist

```sh
./run python -m research_agent.run checklist export --run-dir "$RUN_DIR"
```

This writes `checklist-export.json`: the `ACTIVE` checklist items. The outlook worksheet
shows them per coin (TECHNICAL items match a coin's current state tags) and for every coin
(the news, event, market and source items). They set what you check first. They never
remove a coin, a source or a check. `checklist show` prints the whole checklist, candidates
and retired items included.

### 4. Fetch market data

```sh
./run python -m research_agent.run market --run-dir "$RUN_DIR"
```

Coinbase's public candles (no key) for every coin the context listed: 300 hours of 1-hour
candles and 60 days of daily candles, from which `levels` derives 2h/4h/6h bars. That is
what the default profile, `DAILY_V1`, needs; `market.json` records the profile. It also
records exactly when this was fetched (`retrieved_at`). Every later step treats a bar as
available only if it was fully complete by that instant, never by whatever time it happens
to be read later.

### 5. Find level setups

```sh
./run python -m research_agent.run levels --run-dir "$RUN_DIR"
```

For each coin, this tries rule A then rule B, timeframes 4h, 6h, 1d, 2h, 1h, and windows of
20, 24 and 30 bars, and keeps the first qualifying setup. These are the default profile's
rules (`DAILY_V1`, `--profile daily`); the intraday runs use `INTRADAY_V2`, which tries the
same timeframes shortest first with entries from 0.3% below the mid (see "Intraday run").

- **Entry**: a swing low that has *held* (no later cited bar traded below it), 0.6%-6%
  below the current mid.
- **Max entry**: entry × 1.0015, rounded up to the coin's price increment.
- **Stop**:
  - rule A: 0.4% under the lowest low of every cited bar;
  - rule B (only if A finds nothing): 0.4% under a held *lower* pivot. Earlier bars may have
    traded lower before that pivot formed, and the report says so.
- **Stop distance**: at least 2% of max entry.
- **Target**: the window's own highest high, *provided the most recent bar did not make
  it*. A level price has not yet pulled back from is not proven resistance. If the latest
  bar is making a fresh high, there is no valid target this way: that coin is a breakout
  setup, and breakouts stay disabled here (`docs/CRYPTO-AGENT-LOOP.md` 4.4).
- **Reward:risk**: at least 2 at max entry.

`levels.json` records, per coin, the profile (`"profile": "DAILY_V1"`) and either the setup
or every rule/timeframe/window tried and why it did not qualify. `build` refuses a
`--profile` other than the one `levels.json` records (`LEVELS_PROFILE_MISMATCH`), and
`build-notes.json` records it too. A coin sitting near the top of its recent range after a
rally usually shows up here as "the window high is its most recent bar". That is the
accurate reason, not a bug to work around.

### 6. Build the outlook worksheet

```sh
./run python -m research_agent.run outlook --run-dir "$RUN_DIR"
```

`outlook.json` holds one entry per universe coin, pre-filled with the facts the kit already
has:

- the price;
- the day's level setup and the distances from price to its entry, stop and target (with
  the scorecard's distance bucket);
- the 24-hour and 7-day return, the volume against its 7-day average, and the technical
  state tags;
- any items of `news.json` cited for the coin;
- the matching checklist items.

It also has a `market` section with Bitcoin's and Ether's state. The answers are left for
you. Running `outlook` again keeps every answer already given, refreshes the facts (for
example, once `news.json` exists) and follows the universe: a coin that left it is dropped
and listed, a new one is added empty.

### 7. Research, and fill the outlook (the step this kit cannot do)

For **every** coin the context lists (the owner wants the whole market covered, not a
narrowed subset), research prices, technicals, news and fundamentals using the web and any
public source. This is real research, not a template: read actual pages, judge what is
genuinely new, and do not force a catalyst onto a coin that does not have one. A CHART-only
pick (no catalyst) is a completely normal, good outcome.

For every fact you plan to cite:

- **Never invent a time.** Read `published_at` from the page's own metadata (an
  `article:published_time`/`og:article:published_time` meta tag, a JSON-LD
  `datePublished`, or a `<time datetime=...>` tag) or leave it `null`. Never use the
  moment you happened to read the page, and never guess a time zone offset a page does
  not give you.
- **Cut the excerpt exactly as printed**: same case, same punctuation, same curly or
  straight quotes, from the page's own visible text. Do not paraphrase, summarize, or
  "clean up" the wording. Round 3 of the 2026-09-27 real-Jev runs failed partly because
  a research subagent invented future `retrieved_at` times and paraphrased about half
  its excerpts. `build`, `outlook-submit` and `postmortem-submit` re-fetch and check every
  excerpt against the live page and drop or refuse anything that does not match. A
  dropped catalyst is a wasted pick, so get it right the first time.
- **A catalyst must be genuinely fresh**: first made public within 48 hours before your
  `agent_price_at` (the moment you priced the coin), by its own `published_at` or an
  excerpt that itself dates the disclosure. Older news can still be cited as background
  on a CHART pick. It just does not turn the pick into BOTH, and do not claim it is new.
- Every number your claim states must appear in the excerpt (or the bar) it cites. Every
  claim is either a quoted fact, a computed fact you actually computed, or a labelled
  inference; never blur the three.
- Skip a coin plainly when it has nothing worth citing; do not force a weak claim to hit
  a target pick count.

**The news for the report.** Write what you found as `news.json` in the run folder, one
entry per coin under `coins`, each item with its own `kind`:

```json
{
  "coins": {
    "SOL": {
      "catalysts": [
        {"claim": "The exchange listed a new spot market for SOL today.",
         "kind": "CATALYST",
         "source": {"url": "https://example.com/article",
                    "excerpt": "The exchange listed a new spot market for SOL today.",
                    "published_at": "2026-09-27T05:30:00+00:00"}}
      ],
      "risks": [
        {"claim": "A large wallet moved a significant balance to an exchange.",
         "kind": "RISK", "label": "Whale wallet moved to an exchange",
         "source": {"url": "https://example.com/risk-article",
                    "excerpt": "...", "published_at": null}}
      ],
      "fundamentals": []
    }
  }
}
```

The rules for `news.json`:

- The item kinds:
  - `catalysts` items must have `kind: "CATALYST"`;
  - `risks` items `kind: "RISK"` (an optional short `label` feeds the report's
    `known_risks`);
  - `fundamentals` items `kind: "NOVELTY"`, `"ECONOMIC_LINK"` or `"TECHNICAL"` (background
    support, never the primary catalyst).
- The source:
  - `source.url` must be a public `https://` page;
  - `source.excerpt` is the exact cut text;
  - `source.published_at` is the page's own time or `null`.
- A coin with nothing to add can be left out of `news.json` entirely, or given empty lists.

**The outlook, for every coin.** Fill each entry of `outlook.json`:

- `direction`: `UP`, `DOWN` or `FLAT` over the 24 hours after the app receives the
  outlook, with a `confidence` from 0 to 1. Or `SKIPPED` with a `skip_reason` (1-200
  characters) and nothing else.
- `expected_move_pct`: optional, the expected size of the move in percent.
- `reasons`: at most 4, each `{kind, text, source}`:
  - `kind` is `NEWS`, `EVENT`, `TECHNICAL`, `FUNDAMENTAL` or `MARKET`;
  - `text` is 1-200 characters;
  - **every `NEWS` or `EVENT` reason needs a `source`**: `{url, excerpt, published_at}`,
    under the rules above.

Also fill the `market` section:

- `summary` (1-600 characters): Bitcoin and Ether, sectors such as L1s, DeFi, memes and AI,
  macro, and the stock market's pull on crypto, such as Nasdaq and S&P futures and COIN and
  MSTR;
- `btc` and `eth`: a direction (`UP`, `DOWN` or `FLAT`) and a confidence;
- at most 12 `factors` (`{name, note, source}`) and 20 `events` (`{at, what, source}`,
  `at` a time from a source or `null`).

The outlook is graded once, on its forward window, from receipt to 24 hours later:

- a move under 1.5% is FLAT;
- a hit is the right direction, whatever the confidence;
- a miss is a 5%+ move you called FLAT, the other way or SKIPPED;
- a false alarm is UP or DOWN at confidence 0.6 or more with a move under 1.5% or the
  other way.

Give 0.8 only where you would be right about four times in five.

### 8. Send the outlook

```sh
./run python -m research_agent.run outlook-check --run-dir "$RUN_DIR" \
  --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
./run python -m research_agent.run outlook-submit --run-dir "$RUN_DIR" \
  --base-url "$BASE_URL" --token-file "$TOKEN_FILE" \
  --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
```

Send it right after it is filled, **before the report** (guidelines V6): it is graded from
the moment the app receives it, and a mover counts as your miss only against an outlook
recorded before the move started.

`outlook-check` makes every check without sending anything (`outlook-check.json`). With
`--base-url` and `--token-file` it reads the research context again first, as the send does;
without them it checks against `context.json`. `outlook-submit` first reads the research
context again and saves it as `context.json` (so `outlook` and `build` see the universe and
quotes the app serves now), then refuses to send when:

- any coin is unfilled, or skipped without a reason;
- a coin joined the universe since the worksheet was built (run `outlook` again: it now
  reads the saved context, keeps your answers and adds the coin; fill it);
- a `NEWS` or `EVENT` reason has no source;
- a cited excerpt does not verify on its live page;
- a `published_at` is not one of the page's own metadata times (use one of those, or
  `null`);
- your agent ID appears in your own text;
- anything else `MARKET_OUTLOOK_V1` refuses.

A coin that left the universe is dropped from what is sent, with a note.

The kit stamps `generated_at` just before the POST and saves the exact bytes it sends in
`outlook-sent.json`, before the POST. `outlook-submit.json` records every attempt and its
outcome, a transport error included. HTTP 202 `MARKET_OUTLOOK_RECORDED` gives the forward
window and the day whose reality grades the outlook.

Sending again is safe:

- **No answer** (a timeout, a dropped connection or a 5xx): run `outlook-submit` again. It
  first resends the saved bytes unchanged. A 202 settles it; it is a replay if the first
  send had landed. Only `MARKET_OUTLOOK_STALE_OR_FUTURE`, which proves the first send never
  landed, leads to a rebuild under the same `outlook_id`.
- **Refused** (a 4xx, or 503): nothing was stored. Fix the worksheet and run
  `outlook-submit` again, under the same `outlook_id`.
- **Recorded**: `outlook-submit` sends nothing more. That includes a 422
  `OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH`, which means an earlier send with this
  `outlook_id` reached the app.
- **`--new-id`** is only for a deliberately changed outlook. The app records it as a second
  outlook and grades it separately.

### 9. Build the report

```sh
./run python -m research_agent.run build --run-dir "$RUN_DIR" --news "$RUN_DIR/news.json" \
  --lessons "$RUN_DIR/emphasis.json" --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
```

(Omit `--news` for a CHART-only run.) `build`:

- re-fetches and verifies every excerpt in `news.json`;
- drops anything that fails verification, is unverifiable, or (for a catalyst) is older
  than 48 hours before that pick's `agent_price_at`, recording why in `build-notes.json`;
- writes literal-only claims and reasoning and assembles each pick;
- checks every pick against the app's own report-V3 models and dossier compiler before it
  is allowed into `report.json`. A pick the app's own models reject (for example
  `DOSSIER_OVER_BUDGET`) is dropped and recorded in `build-notes.json`, never silently
  included.

With `--lessons`, `build` ranks the picks by the emphasis hints **after** applying its own
30-pick limit, so the same coins are picked with or without them; only the order changes.
Each pick a hint ranked early says so in its `why_over_peers`. `build-notes.json` records
the hints and the order before and after.

Read the printed summary and `build-notes.json`. If a coin you expected is missing, check
why:

- it was skipped: no qualifying level setup (see `levels.json`);
- or it was rejected: the app's models refused the assembled pick (see `build-notes.json`).

### 10. Validate

```sh
./run python -m research_agent.run validate --run-dir "$RUN_DIR"
```

This re-checks `report.json` against the app's own models one more time: useful if you
hand-edit `report.json`, or just as a final check before submitting. `validate.json` lists
what would be accepted and rejected, and each accepted pick's dossier byte size.

### 11. Submit the report

```sh
./run python -m research_agent.run submit --run-dir "$RUN_DIR" --base-url "$BASE_URL" \
  --token-file "$TOKEN_FILE"
```

This is `POST /api/v1/lab/research-reports` with the agent's bearer token. Read
`submit.json`:

- **HTTP 202 with `MUSE_REPORT_RECORDED`**: the app accepted the envelope. Jev's review and
  selection happen afterward, independently; this kit has no part in them and no visibility
  into their outcome.
- **422**: the whole report was refused. Read the body for the code and field paths, fix
  `news.json` or the build inputs, and re-run `build`, `validate` and `submit`.

`submit` stamps `generated_at` just before the POST (the app refuses a report over 60
seconds old) and keeps the `report_id`. Never retry `submit` with changed content under the
same `report.json`: a changed body under the same `report_id` is refused. Rebuild first.

### 12. Write the run notes

Before finishing, write (or append to) a short plain-text note in the run folder:

- how many coins were researched;
- how many picks were built, how many were skipped or rejected, and the main reasons;
- whether `news.json` had any dropped excerpts;
- the outlook result (coins filled, skipped) and the submit result;
- **every lesson or checklist item you applied**, and whether your method changed (if it
  did, bump `agent_version`).

Keep it honest: a thin morning (few or no qualifying setups) is a real and useful result,
not a failure to paper over.

## Evening

Day D is the New York day that just ended. Run these after 02:00 New York on day D+1, or at
the start of the 07:15 combined session. Set `DAY=D` and `EVENING_DIR=runs/<D>/evening`.

### 1. Read the research context again

```sh
./run python -m research_agent.run context --base-url "$BASE_URL" \
  --token-file "$TOKEN_FILE" --run-dir "$EVENING_DIR"
```

This is a fresh read, in the evening folder, so the morning's `context.json` stays what the
report was built from. Its lessons now carry:

- day D's reality (`recent_days`);
- the outlook grades;
- the post-mortems still owed (`pending_post_mortems`): your notable trades (stop-outs, wins
  above 1.5R, early exits, 24-hour exits) and day D's movers, with `was_miss` marking your
  misses.

### 2. See what moved

```sh
./run python -m research_agent.run movers --day "$DAY" --run-dir "$EVENING_DIR"
```

Coinbase's public 1-hour candles for every coin in the context's universe, over day D and
the 7 days before it. Per coin, `movers.json` gives:

- the return and the largest move up and down from the day's open;
- the volume per hour against the 7 days before;
- where the move started (the app's rule on 1-hour bars, and the first bar past half of
  the move);
- the technical state at the day's start.

The movers are the 5 largest rises, the 5 largest falls and every 5%+ move, each with its
bars and its state just before the move. A bar counts only if it had ended when it was
retrieved. A day that has not ended is refused.

This is the kit's own view, for reading. The app's record, on Alpaca's 1-minute bars,
decides the post-mortem subjects and their reference times.

### 3. Build the post-mortem worksheet

```sh
./run python -m research_agent.run postmortem --day "$DAY" --run-dir "$EVENING_DIR"
```

This stops with `REALITY_DAY_NOT_RECORDED` until the context's lessons record day D. Read
the context again after the nightly job; never build from a partial record.

If the app could not serve the lessons this run (`available: false`), there are no subjects
this run. `postmortem` writes a worksheet with none, or leaves an existing one (with your
answers) untouched. The owed post-mortems stay pending for the next run, and `checklist
update` skips the TECHNICAL scoring and says so.

`postmortem.json` holds one subject for every owed post-mortem, in this order:

1. notable trades (stop-outs, wins above 1.5R, early exits, 24-hour exits);
2. the movers, misses first;
3. the trades with no notable reason. These are optional (`required: false`): guidelines V6
   asks for every notable trade and every mover, so an undecided optional trade is simply
   not sent.

A pending entry the app recorded with a gap keeps its subject, with a note, and never
blocks the others. For example, a trade flattened outside the engine has no exit fill: its
window ends when the lessons were read, and the kit does not measure its move.

Each subject has:

- its price window (1-hour bars);
- its **reference time**: the app's `move_start_at` for a mover, the first entry fill for a
  trade;
- the bar that holds it;
- the kit's measures of the move;
- the computed technical state just before it, with a plain-text draft
  (`pre_move_technicals_draft`).

### 4. Research why it moved (the step this kit cannot do)

For each subject, search the news around the move's start (a mover) or from entry to exit (a
trade), under **every citation rule of morning step 7**: excerpts cut verbatim, publish
times only from the page's own metadata, and nothing invented. Then fill:

- **`cause`**:
  - `COIN_NEWS`: a hack, a listing, an unlock, a lawsuit;
  - `MARKET_WIDE`: a Bitcoin or macro move, regulation, an exchange outage;
  - `NO_NEWS`: the setup, technicals or flows alone;
  - `SURPRISE`: nothing was knowable.
- **`knowable_before_move`**:
  - `true` only with a cited source whose `published_at` is at or before the reference
    time (otherwise the app refuses the item: `KNOWABLE_BEFORE_MOVE_UNSUPPORTED`);
  - `false` when the cause came out after the move began;
  - `null` when you cannot tell.

  Only information public before a move counts as a signal.
- **`summary`**: 1-400 characters.
- **`sources`**: at most 4 `{url, excerpt, published_at}`. `COIN_NEWS` and `MARKET_WIDE`
  need one.
- **`pre_move_technicals`**: 1-300 characters, the state before the move (a range breakout,
  a support bounce, a volume spike, the trend, the distance from recent highs or lows).
  Start from the kit's draft and add your read of the chart.
- **`factors`**: for the research checklist, never sent. A list of `{kind, key,
  description}`:
  - `kind` is `NEWS_TYPE`, `EVENT`, `MARKET_FACTOR` or `SOURCE_QUALITY`;
  - `key` is a short name such as `EXCHANGE_LISTING` or `TOKEN_UNLOCK`;
  - record the factors behind the cause.

  Technical patterns are scored from `movers.json`, not from here.

When Jev vetoed a pick as stale news, or a news pick did badly, also find when the
development was first made public and note whether your own dating was wrong.

### 5. Check

```sh
./run python -m research_agent.run postmortem-check --day "$DAY" --run-dir "$EVENING_DIR" \
  --agent-id "$AGENT_ID"
```

This writes the **knowable-before-move helper**'s suggestion into each subject
(`knowable_helper`). It compares each cited source's `published_at` with the reference
time:

- it suggests `true` only when a source was published at or before it;
- it suggests `false` when every source came after;
- otherwise it suggests nothing, because it never guesses a missing time.

It also shows how a decided mover reads in plan 5c's terms (`KNOWABLE_AND_MISSED`,
`KNOWABLE_NOT_ACTIONABLE`, `SURPRISE`, `NO_NEWS`). **You decide**; the helper only compares
times. The check also lists every problem the send would refuse, with each source fetched
again.

### 6. Send the post-mortems

```sh
./run python -m research_agent.run postmortem-submit --day "$DAY" --run-dir "$EVENING_DIR" \
  --base-url "$BASE_URL" --token-file "$TOKEN_FILE" \
  --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
```

Nothing is sent while a required subject is undecided, or while any check fails. Undecided
optional trades are left out, with a note. The post-mortems go as `POST_MORTEM_V1` notes of
up to 30 items, and each item is accepted or refused on its own.

`postmortem-submit.json` records every note: what was sent, the factors behind each item
(which are never sent), and the app's answer item by item. A second send resends only the
items not accepted yet.

### 7. Update the research checklist

```sh
./run python -m research_agent.run checklist update --from "$EVENING_DIR/movers.json" \
  --from "$EVENING_DIR/postmortem-submit.json" --lessons "$EVENING_DIR/context.json"
```

The checklist is your own memory, outside the app, in the owner-only state folder. Its rules
are guidelines V6's:

- **TECHNICAL patterns** are scored on the whole universe:
  - an occurrence is a coin carrying a state tag at the start of the New York day
    (`movers.json`);
  - a hit is that coin being one of the app's movers that day (`recent_days`);
  - a pattern becomes `ACTIVE` at 10 or more occurrences with a lift of 2.0 or more (its
    hit rate over the day's mover share), overall and over its last 10;
  - an `ACTIVE` pattern is `RETIRED` when its last-10 lift falls below 1.25.
- **`NEWS_TYPE`, `EVENT`, `MARKET_FACTOR` and `SOURCE_QUALITY` patterns** come only from
  the post-mortems the app **accepted** (`postmortem-submit.json`). The worksheet is
  refused as input: a refused or unsent item is no evidence. A trade with no notable reason
  does not count, and when the app accepted a later note on a subject, the latest counts:
  - an occurrence is a factor behind a cause;
  - a hit is `knowable_before_move: true`;
  - a pattern becomes `ACTIVE` at 10 or more occurrences with a 50% hit rate, overall and
    over the last 10;
  - it is `RETIRED` below 40% over the last 10.
- "The last 10 occurrences" are counted in whole days, newest first, until they hold at
  least 10.
- Only an `ACTIVE` pattern is retired; a candidate stays a candidate. A retired pattern may
  become `ACTIVE` again under the same rule.
- Every change is logged.
- Re-applying the same files changes nothing.
- Evidence is never edited. An occurrence an input now scores differently is corrected, and
  one it no longer lists is withdrawn, each by a new entry.

The checklist only orders what you check first. It never narrows coverage.

### 8. Write the evening notes

Append a short note in the evening folder:

- the subjects reviewed and their causes;
- the misses that were knowable before the move and what to check differently;
- the post-mortem send result;
- the checklist changes that `checklist update` printed.

## Intraday run (every 2 hours)

**Paper trading only.** When the app's schedule takes research every 2 hours (00:00, 02:00,
… 22:00 New York), one short run answers each slot except 08:00, which stays the full daily
run above (Morning, or the 07:15 session). The aim is many trades to harden the engine
(`docs/FAST-CYCLE-PLAN.md`), so the run is chart-first and quick:

- **no** lessons, checklist, outlook, evening review or post-mortems (they stay in the 08:00
  run and the evening review);
- a news check only for the picks, and only if time allows;
- finish within about 30 minutes.

It uses the research profile **`INTRADAY_V2`** (`--profile intraday` on `market`, `levels`
and `build`):

- the daily profile's timeframes, shortest first: 1h, 2h, 4h, 6h, 1d (`DAILY_V1`: 4h, 6h,
  1d, 2h, 1h);
- an entry 0.3%-6% below the current mid (`DAILY_V1`: 0.6%-6%);
- everything else exactly as in morning step 5: windows of 20, 24 and 30 bars, rule A then
  rule B, max entry × 1.0015, the stop 0.4% under its bar, a stop at least 2% under the max
  entry, the window's own high as the target (never the latest bar's), and reward:risk of
  at least 2. The stop distance and reward:risk are the app's rules; no profile changes
  them.

So it finds a setup for every coin the daily profile would, from the same market data, and
where a coin has a qualifying 1-hour setup it keeps that one, closer to price. Rule A still
comes first across every timeframe, as in every profile: a 1-hour rule-B setup never
displaces a rule-A one on longer bars.

The first intraday profile, `INTRADAY_V1` (1h, 2h and 4h only, entries 0.3%-3% below the
mid, a week of 1-hour candles), stays available as `--profile intraday-v1` for comparison.
It finds far fewer setups: 1- to 4-hour structure within 3% of price rarely allows the 2%
minimum stop (on 2026-09-28's 33 coins, 2 setups where the daily profile found 12).

**Before you start.** Everything in "Before you start" above applies (the base URL, the
token file you never read, `./run` from the repository root), plus:

- **Its own run folder**: `runs/<YYYY-MM-DD>/intraday-<HHMM>`, the New York date and slot it
  answers (for example `runs/2026-09-29/intraday-1000`). Pass it as `--run-dir` to every
  step. Never use the 08:00 run's folder (`runs/<YYYY-MM-DD>`) or another slot's: each
  folder is one research run (`run.json` holds the run ID the report's agent block
  declares) with one report.
- **The agent identity** the intraday prompt gives (`DAILY_PROMPT.md`), a version of at most
  32 characters.
- **The slot it answers.** `build` declares the run slot from the context's schedule
  (`build.run_slot_for`): the latest scheduled run at or before now, or the next one once
  now is within the schedule's grace (60 minutes) before it. Start at the slot (10:00 answers
  10:00; so does any time up to 10:59) or in the hour before it (09:15 answers 10:00). The
  picks stay valid until the next run plus the grace (a report for 10:00: until 13:00), and
  once a newer run's selection is published, the app revokes the older run's setups still
  waiting for an entry (`RESEARCH_RUN_SUPERSESSION_V1`; open positions are untouched).

### 1. Read the research context

```sh
./run python -m research_agent.run context --base-url "$BASE_URL" \
  --token-file "$TOKEN_FILE" --run-dir "$RUN_DIR"
```

Check `schedule.runs` in `context.json`: it must list the 2-hourly runs. If it still lists
only `08:00`, **stop and say so**: a report built now would answer the 08:00 run.

### 2. Fetch market data

```sh
./run python -m research_agent.run market --profile intraday --run-dir "$RUN_DIR"
```

The daily run's own fetch, for every coin the context lists: 300 hours of Coinbase 1-hour
candles (for the 1h, 2h, 4h and 6h bars) and 60 days of daily candles. `market.json` records
`"profile": "INTRADAY_V2"`. Data fetched with `--profile intraday-v1` (a week of 1-hour
candles, no daily candles) lacks the 6h and daily bars, and `levels` refuses it for this
profile (`MARKET_DATA_PROFILE_MISMATCH`).

### 3. Find level setups

```sh
./run python -m research_agent.run levels --profile intraday --run-dir "$RUN_DIR"
```

The `INTRADAY_V2` rules above. Every row of `levels.json` records `"profile":
"INTRADAY_V2"`. A coin with no 1-hour setup falls through to 2h, 4h, 6h and daily bars. On
the shortest bars the most common miss is the 2% minimum stop: a held low near price often
sits in a window whose lowest low is too close under it. Few or no setups is a real result:
price may have run away from its recent lows, or the latest bar may be making a fresh high.
Never loosen a rule to make picks.

### 4. Build the report

```sh
./run python -m research_agent.run build --profile intraday --max-picks 10 \
  --run-dir "$RUN_DIR" --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
```

CHART-only picks are normal here. `build` keeps at most 10 picks, rule A first, then the
highest reward:risk. It refuses a `--profile` other than the one `levels.json` records
(`LEVELS_PROFILE_MISMATCH`), and `build-notes.json` records `"profile": "INTRADAY_V2"`, so
every pick says which profile produced it. Check `report.json`'s `run_slot`: it must be the
slot this run answers. When no pick survives, `build` writes no report: write the run notes
(step 8) and stop; there is nothing to send.

### 5. A quick news check (only if time allows)

For the picks in `report.json` only, look for news under **every citation rule of morning
step 7**: publish times only from the page's own metadata or `null`, excerpts cut exactly as
printed, and a catalyst only when first made public within 48 hours. Write what you found
as `news.json` in the run folder, and build again with it:

```sh
./run python -m research_agent.run build --profile intraday --max-picks 10 \
  --run-dir "$RUN_DIR" --news "$RUN_DIR/news.json" \
  --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
```

The same coins are picked with or without news (the limit comes first); news only turns a
pick BOTH or adds its background and risks. Skip this step when time is short.

### 6. Validate

```sh
./run python -m research_agent.run validate --run-dir "$RUN_DIR"
```

### 7. Submit the report

```sh
./run python -m research_agent.run submit --run-dir "$RUN_DIR" --base-url "$BASE_URL" \
  --token-file "$TOKEN_FILE"
```

As in morning step 11: HTTP 202 `MUSE_REPORT_RECORDED` is accepted; on a 422, fix the cause
and run `build`, `validate` and `submit` again. Never resend changed content under the same
`report.json`.

### 8. Write the run notes

A few lines in the run folder: the slot answered (`run_slot`), how many coins had a setup,
the picks built, skipped and rejected with the main reasons, whether news was checked, the
submit result, and anything that did not work as written.

Without the news check, steps 1-4 and 6 are also one command (it never submits):

```sh
./run python -m research_agent.run all --profile intraday --max-picks 10 \
  --run-dir "$RUN_DIR" --base-url "$BASE_URL" --token-file "$TOKEN_FILE" \
  --agent-id "$AGENT_ID" --agent-version "$AGENT_VERSION"
```

## Testing against a session harness

This is a separate workflow, not part of the daily run above: a way to get a real proof of
this kit against real review logic before the owner's app is actually running.
`scripts/agent_research_session.py` runs a supervised local session (a disposable ledger, a
simulated venue, real selection/Jev-review code), but its `create_managed_app` call passes
no `research_context`. So `GET /api/v1/lab/research-context` answers 503 there: there is
no real context to read. The harness has no outlook, lessons or post-mortem support either;
this workflow covers the report only.

1. Start a session in another terminal (it runs until you stop it):
   ```sh
   ROOT=/tmp/catalyst-session-$(date +%H%M%S)
   ./run python scripts/agent_research_session.py start --root "$ROOT" --port 8791 \
     --jev fixture --extra-crypto-pairs <your researched coins not in the default 20>
   ```
   - `--jev fixture` is free. `--jev typesafe` spends real provider credits and is the
     owner's or coordinator's call, per `docs/OPERATIONS-RUNBOOK.md`'s "Supervised
     research-agent session".
   - The session's own report-V3 universe is its default owner-bucket pairs plus whatever
     `--extra-crypto-pairs` you add at start. It is **not** the same as the offline
     context's Alpaca-quote universe, and there is no route to read it back.
   - A pick outside it is refused `SYMBOL_NOT_IN_UNIVERSE` by the session's own real intake
     code, so keep your researched coins inside it, or add them with
     `--extra-crypto-pairs`.
2. Run this kit exactly as usual, with `--session` on `build` and agent identity `fable`
   (the session's one fixed agent, `AGENT_ID` in the harness):
   ```sh
   ./run python -m research_agent.run context --offline --run-dir "$RUN_DIR"
   ./run python -m research_agent.run market --run-dir "$RUN_DIR"
   ./run python -m research_agent.run levels --run-dir "$RUN_DIR"
   ./run python -m research_agent.run build --session --run-dir "$RUN_DIR" \
     --news /path/to/news.json --agent-id fable --agent-version test-session-1
   ./run python -m research_agent.run validate --run-dir "$RUN_DIR"
   ```
   `--session` builds and checks every pick exactly as the real path does, but the report
   deliberately has no `run_slot`/`context_as_of`/`valid_until`. This was verified by reading
   the harness's own `run_submit`, which fills exactly those three fields with
   `dict.setdefault` from its own configured schedule; that is more correct than anything
   this kit could compute without seeing it. Without a context's report format the report
   declares guidelines V6. The run folder is marked `SESSION_ONLY`.
3. **Submit with the harness's own command, never this kit's `submit`**, which refuses a
   `SESSION_ONLY`-marked run folder outright (exit code 2) and never makes the network
   call:
   ```sh
   ./run python scripts/agent_research_session.py submit --root "$ROOT" \
     --report "$RUN_DIR/report.json"
   ./run python scripts/agent_research_session.py tick --root "$ROOT"
   ```
   `tick` runs the real selection/Jev-review logic and prints each pick's disposition.
4. When done: `./run python scripts/agent_research_session.py stop --root "$ROOT"`.

A normal (non-session) `build` run in the same folder afterward clears the `SESSION_ONLY`
marker automatically.

## What this kit does not do

- It never sizes, authorizes, prices, or places a broker order; it has no broker-order code
  path at all. Everything after submission (Jev's selection, the system check, execution on
  Alpaca Paper) is the app's, not this kit's.
- It never invents a fact, a time, or an excerpt, and it never modifies the app's intake
  rules, question sets or selection logic.
- Lessons and the checklist change the order of research and picks, never coverage and
  never a trading rule. Outlooks and post-mortems are research records: they never reach
  Jev and change no trade.
- The offline `context --offline` fallback is for development only. `build` refuses to turn
  it into a submittable report unless `--session` is also given. Even then the result is for
  the session harness's own `submit` only (see above): this kit's own `submit` still
  refuses to send a `--session` report to a live app.
