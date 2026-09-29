# Connecting Muse to the deployed app

**PAPER TRADING — SIMULATED. Not real money.**

Package muse-connection, 2026-09-28. This is the one guide a person or an agent follows to
connect Muse to the app deployed on Railway.

Owner decisions, 2026-09-28:

- "you will not do research going forward muse will do it ... Once we deploy muse will access
  our app with api's and do research and communicate with jev."
- "jev also stays in app muse will be the only outsider".

So Muse is the only outside caller. It researches the crypto market outside the app. Once per
scheduled run it sends one report, reads what Jev decided, posts news about its open trades,
and argues its case in the 24-hour reviews and early exits. From package learning-app
(2026-09-28) it also reads its own lessons in the research context, records a morning outlook
for every coin and posts post-mortems on its notable trades and the day's movers (section 4g). Jev lives inside the app, and only
the app calls TypeSafe. Muse never calls Jev. Jev never learns who proposed a pick: the identity
travels only in the report's `agent` block and in event bodies, never in what Jev reads
(AGENTS.md).

Everything below was checked against the code (`src/catalyst_lab/managed_service.py` and the
modules it calls) at `7443686`, and the learning loop (section 4g) against package
learning-app's code, not only against the long, historical
[API contract](API-CONTRACT.md), which the links point to for detail. Two tests keep this guide
honest:

- `tests/test_muse_connection_examples.py` validates every example, the route list and the
  limits table against the app's own models.
- `tests/test_muse_connection_identity.py` proves the credential decision.

## 1. Connect

- **Base URL:** `https://<trader>.up.railway.app`, the trader service's public domain. The
  owner gives it to Muse.
- **HTTPS only.** Plain HTTP would carry the token in clear text.
- **Every request** sends `Authorization: Bearer <MUSE_TOKEN>`. A request with a body also
  sends `Content-Type: application/json`.
- **The token.** The owner hands it over out of band, as a file readable only by Muse's
  process. It never goes into chat, a report, a source, a log line or a URL.
- **Unauthenticated:** only `GET /health` and the static page files (`/`, `/results` and their
  scripts), which hold no data. Every `/api/` route needs the token. A missing or wrong token
  gets 401 `AUTHENTICATION_REQUIRED`. A route outside Muse's list gets 403
  `TOKEN_ROLE_NOT_PERMITTED`.
- **Formats.** Times are RFC 3339 with an offset (`2026-09-28T12:10:00Z`). Prices are decimal
  strings (`"147.43"`), never floats. Every response carries `Cache-Control: no-store`.

## 2. Muse's identity and token

**The deployment gives Muse its own research-agent token, as agent `muse`:**

```text
MANAGED_AGENT_TOKENS_JSON = {"muse": "<MUSE_TOKEN>"}    (the only entry)
```

With that token Muse does everything this guide describes. It sends
`AGENT_RESEARCH_REPORT_V3` with its own declared `agent_version`. It sees and answers the
reviews and Jev exit flags addressed to `muse`. It raises its own exit flags and posts news on
its trades. `tests/test_muse_connection_identity.py` proves each of these in exactly this
configuration.

Why this token and not another:

- **Not the legacy `MANAGED_API_TOKEN`.** That token also acts for agent `muse`, and it accepts
  any declared version: `LEGACY_UNDECLARED` is only the old Muse worker's command-line default.
  But it carries legacy powers Muse never needs:
  - It may send unversioned reports that stay unattributed (`LEGACY_UNATTRIBUTED`).
  - It acts on unattributed legacy records.
  - Without the status and operator tokens configured, it reaches every route.

  An agent token is scoped the same way in every configuration: the research-agent routes of
  section 3, for its own agent, and nothing else.
- **Not another agent ID.** The blindness screens (`AGENT_IDENTITY_IN_PICK`,
  `AGENT_IDENTITY_IN_NEWS`, `AGENT_IDENTITY_IN_ANSWER`) look for the credential's own agent ID
  as a whole word. Under the ID `muse`, text that names Muse is refused before Jev reads it.
  Under any other ID, "Muse" in a pick or an answer would pass and reach Jev.

What else the deployment needs:

- **Role tokens, never given to Muse.** The app refuses to start without `MANAGED_API_TOKEN`.
  The cloud configuration (package cloud) also requires `MANAGED_STATUS_TOKEN`,
  `MANAGED_OPERATOR_TOKEN` and a non-empty `MANAGED_AGENT_TOKENS_JSON`. `{"muse": ...}` alone
  satisfies the last one. All of them must differ.
- **`MANAGED_API_TOKEN` must stay unissued and sealed.** It is the legacy identity of agent
  `muse`, so it could act as Muse (the identity test shows it).
- **No other research-agent credential.** The set of agents that answer reviews is
  `{"muse"}`. It would be `{"muse"}` even without Muse's entry, because the legacy identity is
  `muse` too. A review of Muse's trade goes to `muse` (`PROPOSING_AGENT`). No app setting names
  another agent.

How the owner makes the token: package cloud's `scripts/cloud_secrets.py` with
`--agent-id muse --agent-token-file <absolute path>` (see `docs/RAILWAY-DEPLOYMENT.md`). The
script defaults to `--agent-id muse`. If `MANAGED_AGENT_TOKENS_JSON` already exists, add
`--rotate MANAGED_AGENT_TOKENS_JSON`, then redeploy the trader. The file it writes is what the
owner hands to Muse.

## 3. Muse's routes

Muse's token reaches exactly these routes. Detail:
[read routes and pagination](API-CONTRACT.md#read-routes-and-pagination).

<!-- muse-routes:start -->
| Route | What Muse uses it for |
| --- | --- |
| `GET /api/v1/lab/research-context` | Start of every run: the coins, prices, schedule, limits and Muse's own trades and outcomes |
| `POST /api/v1/lab/research-reports` | Send the run's report |
| `GET /api/v1/lab/cycles/{cycle_id}/outputs` | One report's events: intake, Jev's reviews, ranking, selection, declines, replacements |
| `GET /api/v1/lab/outputs` | The whole event feed (maintenance, reviews, trades), paged with `after`; never the learning records (outlooks, post-mortems, the nightly reality, scorecards, reviews and replays): an agent reads its own share in `lessons` (4g) |
| `GET /api/v1/lab/positions` | Open positions with their state (stop, target, lifecycle, review time) |
| `GET /api/v1/lab/positions/{setup_id}/news` | A trade's news context: lifecycle and news revision |
| `POST /api/v1/lab/positions/{setup_id}/news` | Post news on Muse's own trade |
| `POST /api/v1/lab/positions/{setup_id}/exit-flag` | Ask for an early exit of Muse's own trade |
| `GET /api/v1/lab/reviews` | Pending 24-hour reviews and Jev exit flags addressed to Muse |
| `POST /api/v1/lab/reviews/{review_id}/answer` | Answer a 24-hour review |
| `POST /api/v1/lab/exit-flags/{flag_id}/answer` | Answer a Jev exit flag |
| `POST /api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim` | Not used under top-K (a top-K cycle has no evidence tasks) |
| `POST /api/v1/lab/cycles/{cycle_id}/evidence` | Not used under top-K (422 `EVIDENCE_REVISION_NOT_APPLICABLE`) |
| `POST /api/v1/lab/market-outlooks` | The run's morning outlook for every coin, `MARKET_OUTLOOK_V1` (section 4g) |
| `POST /api/v1/lab/post-mortems` | Causes of Muse's notable trades and the day's movers, `POST_MORTEM_V1` (section 4g) |
<!-- muse-routes:end -->

Everything else is 403 for Muse's token, including:

- `/status` and `/cycles`
- `/cycles/{cycle_id}/picks` and `/setups`
- `/results`, `/results/picks` and `/analytics/*`
- `/positions/{setup_id}/timeline` and `/measurement`

See [open items](#10-open-items).

## 4. The daily loop

### 4a. Read the research context

`GET /api/v1/lab/research-context` gives one snapshot of everything Muse needs as of one server
instant. Read it at the start of the run, and again before stamping the report if the research
took long. Do not poll it: it reads Alpaca market data. Poll `GET /api/v1/lab/reviews` instead
(section 4f). Detail: [the research context](API-CONTRACT.md#get-apiv1labresearch-context).

| Field | What it holds |
| --- | --- |
| `context_version`, `as_of` | `RESEARCH_CONTEXT_V2` and the server's instant (use it to check your clock) |
| `caller` | `{"role": "muse", "agent_id": "muse"}` |
| `schedule` | `RESEARCH_SCHEDULE_V1`: `timezone`, `runs`, `grace_minutes`, `current_run_slot`, `current_run_valid_until_limit`, `next_runs` |
| `report_format` | `schema_version` (`AGENT_RESEARCH_REPORT_V3`), `guidelines_version` (`MUSE_RESEARCH_GUIDELINES_V6`), `guidelines_sha256`, `picks_target` 20, `picks_max` 30, `skipped_max` 200, `dossier_budget_bytes` 11000, `rationale_budget_bytes` 3000, `max_report_age_seconds` (60 in the deployed settings), `report_max_seconds` (86400) |
| `universe` | `count` and `coins`: every coin Muse may pick. Each coin has `symbol`, `bid`, `ask`, `quote_at`, `spread_bps`, `last_trade_price`, `last_trade_at`, `price_increment`, `min_order_size`, `quantity_increment` and `volume_24h` (`base`, `usd`, `completed_hour_bars`, or null). Also `excluded` (stablecoins and counts), `sources` (labels and read times) and `issues` (a missing quote or volume, never a zero) |
| `open_trades` | Muse's open trades: `setup_id`, `symbol`, `signal_id`, `state`, average `entry`, current `stop` and `target`, `quantity`, `opened_at`, original `levels`, `unrealized_pnl_usd` at the bid, `review_at` (T of the next 24-hour review) and `continuations` |
| `pending_reviews` | The same items as `GET /api/v1/lab/reviews` |
| `recent_outcomes` | `closed_trades` of the last 7 days (at most 50: entry, exit, net P&L, fees, R) and `last_run`: Muse's latest cycle with every pick's status, Jev rank and reasons (section 4d) |
| `lessons` | Muse's own learning record (`RESEARCH_LESSONS_V1`, section 4g): its scorecard lines for 1, 7 and 30 days, its graded outlooks, the last seven days' movers and the trades and movers still waiting for its post-mortem. Never another agent's data; never read by Jev |
| `trade_authorized` | Always `false` |

The universe is Alpaca's active, tradable `XXX/USD` crypto pairs without stablecoins (PAXG
stays). The asset list is cached for an hour, quotes and trades for at most 5 seconds, and
24-hour volume until the next hour. Errors: 503 `RESEARCH_UNIVERSE_UNAVAILABLE`,
`RESEARCH_MARKET_DATA_UNAVAILABLE` or `RESEARCH_CONTEXT_NOT_CONFIGURED`. Retry after a pause.

### 4b. Research, following `MUSE_RESEARCH_GUIDELINES_V6`

Muse owns discovery and research, outside the app. The binding text is
[MUSE_RESEARCH_GUIDELINES_V6](MUSE-GUIDELINES.md#muse_research_guidelines_v6--the-learning-loop-2026-09-28).
It is the
[V3 runtime text](MUSE-GUIDELINES.md#muse_research_guidelines_v3--daily-crypto-research-runs-2026-09-26),
then the [V4 method](MUSE-GUIDELINES.md#muse_research_guidelines_v4--the-method-that-passed-real-jev-review-2026-09-27),
then the [V5 reviews section](MUSE-GUIDELINES.md#muse_research_guidelines_v5--24-hour-reviews-and-exit-flags-2026-09-27),
then the V6 learning section (section 4g). Copy its version and SHA-256 from `report_format`
into the agent block; a report that still declares V5 is accepted. The rules that matter most:

**Choosing picks**

- Research the whole market with any public source. Pick only symbols listed in
  `universe.coins`, spelled exactly (`SOL/USD`).
- Aim for 20 picks; 1 to 30 are accepted. Fewer picks with the shortage explained is fine; an
  unsupported pick is not.
- List every other coin you looked at in `skipped`, with a reason of at most 200 characters.
  That is at most 200 entries; a symbol appears once and is never also a pick.
- Do not pick a coin you hold open (`open_trades`). Its own review decides it.

**Kinds**

- `NEWS` needs at least one source.
- `CHART` needs `technical_evidence`: 20 to 64 bars that were complete when retrieved.
- `BOTH` needs both.

**Levels**

- Long pullback entries only. `stop < entry_trigger <= max_entry_price < target`.
  - `entry_trigger` is a swing low that no later cited bar traded below.
  - `max_entry_price` sits just above it (0.15% has worked).
  - The entry order is a limit at `max_entry_price`.
- The live mid decides the entry type. An `entry_trigger` more than 0.2% below it is a
  pullback; one within 0.2% is immediate. Both are traded. One more than 0.2% above it is a
  breakout, which is refused (`BREAKOUT_NOT_ENABLED`).
- The stop sits at least 2% below `max_entry_price`: `(max_entry_price - stop) /
  max_entry_price >= 0.02`, else `STOP_DISTANCE_BELOW_MINIMUM`.
- Reward-to-risk at max entry is at least 2: `(target - max_entry_price) / (max_entry_price -
  stop) >= 2`. State the ratio as `stated_reward_risk`.
- Every price sits on the coin's `price_increment`, else `CRYPTO_LEVEL_OFF_PRICE_GRID`.
- Intake does not check levels. The system checks them after Jev's selection, so a pick that
  fails them is wasted.

**Prices and times**

- `agent_current_price` and `agent_price_at` are the price you observed and when: from the
  context or a live quote just before generating, never an old close.
- `agent_price_at` may not be later than `generated_at`.
- At admission, a live Alpaca mid more than 5% away from `agent_current_price` refuses the pick
  (`PRICE_MISMATCH`).

**Reasoning and rationale**

- `reasoning` has `thesis` (up to 1,000 characters), `why_now`, `why_these_levels` and `risks`
  (600 each) and `invalidation` (400; Jev reads it as `disproof`).
- `selection_rationale` is `AGENT_SELECTION_RATIONALE_V1`:
  - 1 to 8 `claims`, each with `claim_id`, `kind` (`CATALYST`, `NOVELTY`, `ECONOMIC_LINK`,
    `TECHNICAL` or `RISK`), `text` (up to 300) and `supported_by`.
  - `supported_by` cites `source_ids` or `bar_ids` of the same pick, at least one.
  - `why_now`, `why_these_levels` and `why_over_peers` (500 each).
  - `what_would_change_my_mind` (300), `known_risks` (at most 5, 120 each) and
    `agent_confidence` (`level`, `basis`).
- NEWS picks cite sources in `CATALYST` claims; CHART picks cite bars in `TECHNICAL` claims.
- Write claims as literal facts of what they cite. Label calculations and inference in the
  reasoning.

**The 48-hour catalyst rule.** A NEWS or BOTH pick needs a catalyst first made public within
the 48 hours before `agent_price_at`. State its age in `why_now`. Jev is asked whether the
catalyst is older, and a clear yes (probability 0.70 or more) vetoes the pick. Older news is
background for a CHART pick only.

**Sources**

- At most 8 per pick. Each excerpt is at most 1,200 characters, cut verbatim; together at most
  8,000.
- The URL is a public `https://` page without a query, fragment or credentials.
- `published_at` comes from the page's own metadata, or is null. `retrieved_at` is the real
  fetch time; neither may be later than the server's clock.

**Budgets.** What Jev reads for one pick, every bar included, must fit 11,000 bytes of JSON;
the rationale gets 3,000. Non-ASCII characters count as 6-byte escapes. An over-budget pick is
refused (`DOSSIER_OVER_BUDGET`), never truncated. 20 to 30 bars with two or three short
excerpts usually fit.

**Blindness and privacy**

- Never name Muse, its model or its organization in any text Jev reads. The word `muse` in any
  case, as a whole word, refuses the pick (`AGENT_IDENTITY_IN_PICK`).
- The screen does not read source excerpts and URLs, which are third-party text. It also skips
  `signal_id`, the symbol and both confidences, which Jev never sees.
- Anywhere in the report, a credential, an e-mail address or an `0x` wallet or contract address
  (40 hex digits) refuses the whole report (`SENSITIVE_EVIDENCE_REJECTED`). Strip contract
  addresses from crypto news excerpts.

`agent_confidence` (0 to 1) and the rationale's confidence are for analytics only. Jev never
sees them, and no rule uses them.

### 4c. Send the report

`POST /api/v1/lab/research-reports` with an `AGENT_RESEARCH_REPORT_V3` of at most 1,048,576
bytes (else 413 `RESEARCH_REPORT_TOO_LARGE`). Example: [section 9.1](#91-report-v3-with-one-chart-pick).
Detail: [report V3](API-CONTRACT.md#report-v3-agent_research_report_v3).

**The envelope**

| Field | Rule |
| --- | --- |
| `schema_version` | `AGENT_RESEARCH_REPORT_V3` |
| `report_id` | A new UUID for every report. The cycle ID is `uuid5(284306f0-6a31-5d71-a753-f854db41ccfa, "muse:<report_id>")`; the receipt returns it |
| `generated_at` | Stamped just before sending. The server refuses it when it is more than `max_report_age_seconds` (60) older than the server's clock, or later than it: `RESEARCH_REPORT_STALE_OR_FUTURE`. Keep Muse's clock synced; stamping a second or two early is the safe side |
| `run_slot` | The scheduled run this report answers: `current_run_slot`, or the next run once you are within the grace before it. It must be a scheduled instant (`RUN_SLOT_NOT_SCHEDULED`), and `generated_at` may be at most the grace before it (`RUN_SLOT_IN_FUTURE`) |
| `valid_until` | After `generated_at` (`INVALID_REPORT_EXPIRY`), at most 24 hours after it (`REPORT_VALIDITY_OVER_24_HOURS`) and at most the next run after `run_slot` plus the grace (`REPORT_VALIDITY_AFTER_NEXT_RUN`). For the current run, use the earlier of `current_run_valid_until_limit` and `generated_at` + 24 h |
| `context_as_of` | The `as_of` of the context used; not after `generated_at` (`CONTEXT_AFTER_REPORT`) |
| `agent` | Exactly `agent_id` (`muse`, which must be the token's agent), `agent_version` (Muse's own, `^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$`), `guidelines_version` and `guidelines_sha256` (from `report_format`), and `run_id` (a UUID per research run) |
| `picks` | 1 to 30 |
| `skipped` | 0 to 200 `{symbol, reason}` |

Each pick has `signal_id`, `symbol`, `kind`, `agent_current_price`, `agent_price_at`, `levels`
(`entry_trigger`, `max_entry_price`, `stop`, `target`), `stated_reward_risk`, an optional
`valid_until` (at most the report's), `reasoning`, `selection_rationale`, `sources`,
`technical_evidence` and `agent_confidence`. No other field is accepted (`EXTRA_FORBIDDEN`).

**The 202 receipt.** The key fields (full example: [section 9.2](#92-the-202-receipt)):

- `status: MUSE_REPORT_RECORDED`, `cycle_id`, `polling_url` and `expires_at`.
- `agent_id` and `agent_version`: Muse's, as declared.
- `submitted_count`, `contender_count` (accepted picks), `rejected_count` and `skipped_count`.
- `item_results`: one per pick, in order. An accepted pick has `status: ACCEPTED`, `item_key`
  (`CRYPTO:SOL/USD`), `evidence_hash`, `dossier_bytes` and its own `expires_at`. A refused one
  has `status: REJECTED`, `code` and `errors` (field paths and codes, never your values).
- `idempotent_replay`, `report_schema_version`, `run_slot`, `review_validity: PACKET_EXPIRY`,
  and `trade_authorized: false`.

A pick's packet expires at its `valid_until` (else the report's), bounded by `generated_at` +
`report_max_seconds`. Jev's review of it stays valid until then.

**Per-pick codes.** Each pick is checked on its own, in this order. The first failure names it,
and its siblings proceed:

| Code | Meaning (error-entry codes) | Fix |
| --- | --- | --- |
| `INVALID_RESEARCH_ITEM` | Schema or type error, with paths (for example `EXTRA_FORBIDDEN`, `MISSING`, `STRING_TOO_LONG`, `INVALID_OBSERVED_OHLC`, `INCOMPLETE_OBSERVED_BAR`) | Fix the field |
| `PRICE_NOT_POSITIVE` | Its only schema errors are prices at or below zero | Fix the prices |
| `SYMBOL_NOT_IN_UNIVERSE` | Not in the current tradable universe | Pick from `universe.coins` |
| `NEWS_SOURCES_REQUIRED`, `TECHNICAL_EVIDENCE_REQUIRED` | The kind's sources or bars are missing | Add them or change the kind |
| `CITATION_UNRESOLVED` | A claim cites nothing (`CLAIM_SUPPORT_REQUIRED`), an unknown ID (`RATIONALE_REFERENCE_UNKNOWN`) or the same ID twice | Cite this pick's own IDs |
| `PICK_VALIDITY_INVALID` | The pick's `valid_until` is after the report's, not after `generated_at`, or already past | Fix or drop it |
| `AGENT_PRICE_TIME_INVALID` | `agent_price_at` is after `generated_at` | Stamp in order |
| `INVALID_SOURCE_EVIDENCE` | A source was retrieved after the server's now, or has a wrong `content_hash` or an invalid annotation | Use real retrieval times; omit `content_hash` |
| `FUTURE_TECHNICAL_EVIDENCE` | The bars were retrieved after the server's now | Use the real retrieval time |
| `AGENT_IDENTITY_IN_PICK` | The word `muse` in text Jev reads | Remove it |
| `DOSSIER_OVER_BUDGET` | Over 11,000 bytes, or the rationale over 3,000 (`bytes`, `budget_bytes`) | Fewer bars or shorter text |

**Whole-report refusals.** Nothing is stored. `detail` is a code; `errors` lists paths.

| HTTP and `detail` | When | What to do |
| --- | --- | --- |
| 422 `INVALID_MUSE_REPORT` | Not a JSON object, a duplicate JSON key, or an envelope rule above (codes in `errors`) | Fix and resend |
| 422 `DUPLICATE_SIGNAL_IN_REPORT`, `DUPLICATE_SYMBOL_IN_REPORT` | Two picks share a signal ID or a symbol | Fix and resend |
| 422 `SENSITIVE_EVIDENCE_REJECTED` | Credential, e-mail or 0x address anywhere | Remove it and resend |
| 422 `RESEARCH_REPORT_STALE_OR_FUTURE` | `generated_at` more than 60 s old, or in the server's future | Restamp and resend (see retries) |
| 422 `RESEARCH_REPORT_EXPIRED` | The report's expiry has passed | Send a fresh report |
| 422 `REPORT_IDEMPOTENCY_CONTENT_MISMATCH` | This `report_id` is already recorded with other content | Use a new `report_id` |
| 422 with a pick code, plus `item_results` | No pick was acceptable (a schema failure reads `INVALID_MUSE_REPORT`) | Fix the picks |
| 403 `AGENT_IDENTITY_MISMATCH` | The `agent` block names another agent, or there is none | Send `agent_id: "muse"` |
| 413 `RESEARCH_REPORT_TOO_LARGE` | Body over 1,048,576 bytes | Shrink |
| 503 `RESEARCH_UNIVERSE_UNAVAILABLE`, `RESEARCH_SCHEDULE_NOT_CONFIGURED`, `RESEARCH_V3_INTAKE_NOT_CONFIGURED`, `MUSE_REPORT_INTAKE_NOT_CONFIGURED`, `LAB_DATABASE_UNAVAILABLE` | A missing capability or the database | Retry later (see retries) |

**Idempotency and retries.** An exact resend of a recorded `report_id` returns the stored
receipt (`idempotent_replay: true`), however late. So:

1. On a timeout, a dropped connection or a 5xx, resend the exact same bytes.
2. A 202 means it is recorded.
3. A 422 `RESEARCH_REPORT_STALE_OR_FUTURE` on the resend proves the first attempt was never
   recorded, because a recorded one would have replayed. Restamp `generated_at`, and
   `agent_price_at` if needed, and send again.
4. Never send changed content under a `report_id` that may be recorded; use a new one.

### 4d. Read what Jev decided

The app, not Muse, calls Jev. Jev reviews each accepted pick at once under
`JEV_TOP_K_SELECTION_V2` with K = 10 (the deployed settings); details in
[selection rule top-K](API-CONTRACT.md#selection-rule-jev_top_k_selection_v1-2026-09-27-package-selection-topk):

- Each pick gets one review with its kind's question set: `NEWS_PICK_QUESTIONS_V2`,
  `CHART_PICK_QUESTIONS_V1` or `BOTH_PICK_QUESTIONS_V2`. It also gets one comparative quality
  review, a score from 0 to 100.
- **Veto** only when a failing answer is the unique most probable one with probability 0.70 or
  more. A less certain doubt costs 10 points.
- **Ranking** by adjusted score. The best K become `RESEARCH_SELECTED`, in rank order.
- A symbol another cycle of the same run already selected is skipped
  (`DUPLICATE_SYMBOL_IN_RUN`), and the next-ranked pick takes its place.

Two ways to read the outcome:

1. **The research context's `recent_outcomes.last_run`** covers Muse's latest cycle. It has
   `ranking` (`policy`, `k`, `counts`, `complete`), and for every pick its `status`, `jev_rank`,
   `ranking_status`, `ranking_reasons`, `selection_status`, `decline_code`, `replaced_by`,
   `setup_state` and `setup_reason`. `status` is one of:
   - before selection: `REJECTED_AT_INTAKE` or `AWAITING_REVIEW`; then the review's
     disposition (such as `RANKABLE`) until the ranking is written; then `VETOED`,
     `NOT_RANKED`, `NOT_SELECTED` or `SKIPPED`;
   - after selection: `SELECTED`, `DECLINED`, `REPLACED_BY`, `SUPERSEDED` or `EXPIRED`;
   - once admitted: the setup's state (`WATCHING`, `OPEN`, `CLOSED`, ...).
2. **`GET /api/v1/lab/cycles/{cycle_id}/outputs?after=0&limit=1000`** returns any cycle's events,
   paged with `next_cursor`:
   - `RESEARCH_STARTED`, then `RESEARCH_PACKET` and `RESEARCH_DOSSIER` per accepted pick, and
     `RESEARCH_ITEM_REJECTED_AT_INTAKE` per refused one.
   - `RESEARCH_DECISION` (`RANKABLE`, `VETOED`, `NOT_RANKED` or `EXPIRED`, with `veto_reasons`
     and `uncertain`) and `RESEARCH_QUALITY` (score and category).
   - One `RESEARCH_RANKING` (every entry's `rank`, `adjusted_score` and `status`).
   - `RESEARCH_SELECTED`, `RESEARCH_SELECTION_SKIPPED`, `RESEARCH_ADMISSION_DECLINED` (with the
     `reason`) and `RESEARCH_REPLACEMENT`.

   Poll it until `RESEARCH_RANKING` appears. `complete: false` means the ranking was written at
   the review deadline with a pick unreviewed.

What happens after selection is the app's alone:

1. **Admission.** The app's own checks and the price grid, then
   [the system check](API-CONTRACT.md#system-check-at-admission-system_check_v1-2026-09-27) at
   the live price: stop distance at least 2%, live mid within 5% of `agent_current_price`, stop
   not already hit, not a breakout.
2. **Replacement.** A declined pick is
   [replaced](API-CONTRACT.md#replacement-of-declined-top-k-picks-topk_replacement_v1-2026-09-27)
   by the next-ranked one.
3. **Watching.** An admitted pick is a `WATCHING` setup until its expiry. The
   [trigger](API-CONTRACT.md#crypto-trigger-version-crypto_alpaca_trigger_v1-2026-09-27) is a
   print, or a fresh ask, at or below `entry_trigger`, confirmed by a fresh ask at or below
   `max_entry_price` and a spread of at most 1%.
4. **Entry.** The app sizes the trade and buys with a limit at `max_entry_price`, then protects
   it with the stop and target.
5. **Arms.**
   - A randomized 30% control arm (`FIXED_EXIT`) keeps its levels and exits at 24 hours.
   - The maintained arm gets the per-minute maintenance (within the owner's monthly Jev
     budget) and the 24-hour reviews below.
   - Stops, targets, the −3% daily loss halt and an operator flatten close any trade at any
     time.

### 4e. While a trade is open

**Position news.** Post material news about Muse's own open trade. Detail:
[position news](API-CONTRACT.md#position-news-and-monitoring-readback).

1. `GET /api/v1/lab/positions/{setup_id}/news` returns `setup_id`, `symbol`, `state`,
   `lifecycle_id`, `news_revision`, `hard_exit_at`, `sources` (the current evidence),
   `original_thesis` and `cohort`.
2. `POST` the same path with exactly `news_id` (a new UUID), `lifecycle_id`,
   `expected_news_revision` (the `news_revision` just read) and 1 to 8 `sources`. The body is
   at most 32,768 bytes (413 `EVIDENCE_TOO_LARGE`), and the sources together at most 12,000
   bytes of JSON (`EVIDENCE_TOO_LONG`). Example: [section 9.6](#96-position-news).

Rules, each a 422 code:

- The trade must be open in that lifecycle (`POSITION_LIFECYCLE_NOT_OPEN`) and before its
  `hard_exit_at` (`POSITION_HARD_DEADLINE_REACHED`).
- The revision must be current (`STALE_POSITION_NEWS_REVISION`: read again and resend).
- At least one source must be new to this trade (`MATERIAL_POSITION_NEWS_REQUIRED`).
- No source ID or time may name Muse (`AGENT_IDENTITY_IN_NEWS`).
- The same source rules as picks apply (`INVALID_SOURCE_EVIDENCE`, `SOURCE_COUNT_INVALID`,
  `SENSITIVE_EVIDENCE_REJECTED`).
- A trade Muse did not propose is 403 `AGENT_IDENTITY_MISMATCH`.

The receipt is `POSITION_NEWS_RECORDED` with `news_revision` and `evidence_hash`. News changes
no position. It reaches Jev's maintenance and 24-hour-review contexts, and on a maintained trade
it triggers a maintenance review at once (at most one a minute; none while the month's Jev budget
is spent).

**Per-minute maintenance (`CRYPTO_MAINTENANCE_V2`).** On a maintained trade, Jev reviews every
completed 1-minute bar, plus events: +1R milestones, price near the stop or target, agent news,
a 3% Bitcoin move. Code computes the stop and target options, and Jev only chooses among them.
A new stop only rises and a new target only rises. Muse never sends a stop or target to
maintenance.

**Within the owner's monthly Jev budget (`CRYPTO_MAINTENANCE_V3`, from 2026-09-28).** Trades
admitted from then on are reviewed exactly as above while the month's Jev spend fits the budget.
When it does not, routine reviews come every 5 completed minutes (reason `BAR_5M`) or every 15
(`BAR_15M`) instead of every minute, and once the budget is spent none at all until the next
month; the event reviews (milestones, near stop or target, agent news, a Bitcoin shock) still run
at once except in that last case. Stops, targets, protection, the 24-hour review and Muse's
early-exit flag never depend on the budget. Muse sees it in the outputs: each review's
`POSITION_REVIEW_REQUEST` trigger carries `spend_guard` (`tier`, `review_bar_seconds`), and a
trade that is not reviewed gets one `POSITION_REVIEW_SKIPPED` with `reason`
`JEV_BUDGET_EXHAUSTED` (or `JEV_BUDGET_UNAVAILABLE`). News posted to such a trade is still
recorded and reaches the 24-hour review.

Muse reads maintenance back from `GET /api/v1/lab/outputs`: events with the trade's
`setup_id`, such as `MAINTENANCE_DECISION` (outcome `APPLIED`, `REFUSED`, `HELD`, `FLAGGED`,
`FAILED` or `DISCARDED`, with old and new levels), `MAINTENANCE_REVIEW_SKIPPED`,
`POSITION_REVIEW_SKIPPED`, `STOP_REPLACED` and `EXIT_FLAG_RAISED`. It also reads `GET /api/v1/lab/positions` (state
`stop`, `target`, `stop_replace`) and the `level_changes` of each 24-hour review request.
Detail: [maintenance readback](API-CONTRACT.md#trade-maintenance-readback-crypto_maintenance_v1-package-maintenance-2026-09-27).

**Muse's early-exit flag.** `POST /api/v1/lab/positions/{setup_id}/exit-flag` with an
`AGENT_EXIT_FLAG_V1`. Example: [section 9.5](#95-exit-flag). Detail:
[reviews and early exits](API-CONTRACT.md#24-hour-reviews-and-early-exits-package-day-review-2026-09-27).

- The body: `schema_version`, `flag_ref` (a new UUID; the retry key), the trade's
  `lifecycle_id` (from the news context or `/positions`), `what_changed` (up to 600 characters),
  `next_24h` (600), `proves_wrong` (400) and 0 to 8 sources.
- Jev is asked at once. The trade sells at market only if Jev also says exit within 15 minutes.
  Otherwise it keeps its stop and target.
- The receipt is `EXIT_FLAG_RAISED`, or `EXIT_FLAG_ALREADY_PENDING` with the pending flag, with
  `flag_id` and `answer_due_at`.
- Refusals:
  - 404 `POSITION_NOT_FOUND`.
  - 409 `EARLY_EXIT_NOT_AVAILABLE` (a control-arm or older trade), `POSITION_NOT_OPEN`,
    `POSITION_LIFECYCLE_MISMATCH`, `EXIT_IN_PROGRESS` or `IDEMPOTENCY_CONTENT_MISMATCH`.
  - 422 for a malformed flag: `EXIT_FLAG_FIELDS_INVALID`, `EXIT_FLAG_SCHEMA_REQUIRED`,
    `FLAG_REF_INVALID`, `POSITION_LIFECYCLE_INVALID`, `ANSWER_TEXT_INVALID`,
    `AGENT_IDENTITY_IN_ANSWER` or a source code.
  - 403 when the trade is not Muse's.

### 4f. The 24-hour review

Poll `GET /api/v1/lab/reviews` at least every five minutes while Muse has open trades. The
route suggests 60 seconds and is cheap. The app never pushes. The response is `{as_of,
poll_hint_seconds: 60, request_lead_seconds: 1800, items, trade_authorized: false}`, with items
ordered by `answer_due_at`.

| `kind` | Key fields |
| --- | --- |
| `DAY_REVIEW` | `review_id`, `setup_id`, `symbol`, `lifecycle_id`, `round` (`FIRST` or `DISCUSSION`), `answer_due_at`, `review_at` (T), `review_number`, `answer_route`, `answer_schema`, and `request`: the trade now (entry, quantity, stop, target, bid, ask, P&L in R), `level_changes`, `news_since_entry`, the stop and target `options` code computed, and the `original_pick`. A `DISCUSSION` item adds `your_first_answer` and `jev_first_answer` (`answers`, `meanings`, `chosen`) |
| `EXIT_FLAG` | Jev flagged Muse's trade: `flag_id`, `setup_id`, `symbol`, `lifecycle_id`, `raised_by: JEV`, `raised_at`, `answer_due_at` (15 minutes after the flag), `answer_route`, `answer_schema`, `jev_reasons` and `trade` |

**The clock of a review**

1. T is 24 hours after the trade's first fill, then every 24 hours after a continue.
2. The request appears 30 minutes before T.
3. Muse answers by T with a `POST` to the item's `answer_route`, carrying an
   `AGENT_REVIEW_ANSWER_V1`. Example: [section 9.3](#93-review-answer).
4. Jev is asked at T. It reads Muse's reasons and sources, never Muse's name.

**Outcomes**

- **Agreement decides.** Both say continue: the trade continues with a new 24-hour plan, and
  its stop and target stay or rise to options code offered. Both say exit: it sells at market.
- **Silence.** No answer by T: Jev decides alone (`AGENT_SILENT_JEV_ALONE`).
- **Disagreement opens one discussion round.** The item comes back with `round: DISCUSSION` and
  Jev's answers and their meanings. Muse replies once within 15 minutes, addressing Jev's
  reasons. Jev then answers a final time. Agreement decides; anything else exits
  (`DISAGREED_AFTER_DISCUSSION`, or `DISCUSSION_NO_AGENT_REPLY` without a reply).
- **An unusable or missing Jev answer exits.** Jev unanswered for 30 minutes also exits. If no
  review decides at all, a fail-safe exits at T + 80 minutes.

The answer, `AGENT_REVIEW_ANSWER_V1`:

- `schema_version` and `answer_id`, a new UUID. An exact retry returns the stored receipt;
  changed content under it is 409 `IDEMPOTENCY_CONTENT_MISMATCH`.
- `decision`, `CONTINUE` or `EXIT`.
- `what_changed` (1 to 600 characters), `next_24h` (1 to 600) and `proves_wrong` (1 to 400).
- Optionally, with `CONTINUE` only, `suggested_stop` and `suggested_target`. Jev sees them but
  still chooses only among the code's options.
- 0 to 8 sources in the position-news format.
- No other field and no confidence. The texts must not name Muse (422
  `AGENT_IDENTITY_IN_ANSWER`). The whole answer stays within 12,000 bytes of JSON (422
  `EVIDENCE_TOO_LONG`).

**Answering a Jev exit flag.** `POST /api/v1/lab/exit-flags/{flag_id}/answer` with an
`AGENT_REVIEW_ANSWER_V1` within its 15 minutes. `EXIT` agrees and the trade sells; `CONTINUE`
keeps it. Suggested levels are refused (`SUGGESTED_LEVELS_NOT_ALLOWED`). No answer means the
trade stays. Example: [section 9.4](#94-answer-to-a-jev-exit-flag).

**Refusals of answers**

- 404 `REVIEW_NOT_FOUND` or `EXIT_FLAG_NOT_FOUND`.
- 409 `REVIEW_CLOSED`, `REVIEW_ANSWER_WINDOW_CLOSED`, `REVIEW_ALREADY_ANSWERED`,
  `EXIT_FLAG_NOT_YET_ASKED`, `EXIT_FLAG_ALREADY_ANSWERED`, `EXIT_FLAG_RESOLVED` or
  `EXIT_FLAG_ANSWER_WINDOW_CLOSED`. Re-poll; do not resend.
- 422 for a malformed answer: `REVIEW_ANSWER_FIELDS_INVALID`, `REVIEW_ANSWER_SCHEMA_REQUIRED`,
  `REVIEW_DECISION_INVALID`, `ANSWER_ID_INVALID`, `ANSWER_TEXT_INVALID`,
  `SUGGESTED_LEVEL_INVALID`, `SUGGESTED_LEVELS_NOT_ALLOWED` or a source code.
- 403 `AGENT_IDENTITY_MISMATCH`.

**The reviews switch.** Everything in this section needs `MANAGED_MANAGEMENT_REVIEWS=ENABLED`.
The deploy example, which the cloud configuration copies, sets `DISABLED`. With `DISABLED`:

- The review request still reaches Muse, but at T the trade exits (`JEV_REVIEWS_DISABLED`)
  whatever Muse answered.
- Muse's own exit flags end unanswered (`NO_ANSWER_IN_TIME`), and the trade keeps its stop and
  target.
- There is no Jev maintenance, so no Jev exit flag either.

### 4g. The learning loop: lessons, the morning outlook and the review

Package learning-app, 2026-09-28 (plan [LEARNING-LOOP-PLAN.md](LEARNING-LOOP-PLAN.md) sections
5, 5b and 5c; the binding text is the V6 learning section of the guidelines). Nothing here
reaches Jev, places an order or changes a rule: the outlook, the post-mortems and the lessons
are research-side records. Detail:
[the learning loop](API-CONTRACT.md#learning-loop-outlooks-post-mortems-and-lessons-package-learning-app-2026-09-28).

**Lessons** (`lessons` in the research context, `RESEARCH_LESSONS_V1`), read at the start of
every run:

| Field | What it holds |
| --- | --- |
| `scorecard_day`, `windows` | The latest nightly scorecard's day and Muse's own lines for `1d`, `7d` and `30d`: `funnel`, `results` (R after verified fees as `r_net`), `selection` (Jev's selected, passed and vetoed picks on their shadow outcomes), `fill_rate_by_distance`, `results_by_timeframe`, `results_by_rule`, `results_by_kind`, `results_by_sector`, `excerpt_drop_rate`, `stale_news_vetoes`, `dossier_size_rejections` and `causes` |
| `outlook` | Muse's graded outlooks: the latest one's `hit_rate`, `calibration`, `misses` and `false_alarms` with their `window_start` and `window_end`, the last week's `graded_outlooks` and `by_window` 7- and 30-day sums |
| `recent_days` | The last seven recorded days: movers (`return_pct`, `move_start_at`), the mover share of the universe, Bitcoin, Ether, sectors and volume |
| `pending_post_mortems` | Muse's trades closed in the last 7 days and the latest day's movers that still have no post-mortem from Muse (`was_miss` marks a mover its outlook missed) |
| `available` | `true`; `false` (with `code`) when a learning record could not be read: research goes on without lessons |

Use lessons for emphasis, never coverage: the whole universe and about 20 picks every run.
Rates always come with their counts; prefer the 7- and 30-day lines.

**The morning outlook.** In each research run, before the report, POST a `MARKET_OUTLOOK_V1` to
`/api/v1/lab/market-outlooks` (at most 2,097,152 bytes). Example: [section 9.8](#98-market-outlook).

| Field | Rule |
| --- | --- |
| `schema_version` | `MARKET_OUTLOOK_V1` |
| `outlook_id` | A new UUID; an exact resend returns the stored receipt, changed content is 422 `OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH` |
| `generated_at` | Stamped just before sending; at most 60 seconds old on arrival (422 `MARKET_OUTLOOK_STALE_OR_FUTURE`) |
| `run_slot` | The run it belongs to, as for the report |
| `horizon_hours` | `24` |
| `agent` | The report's agent block |
| `market` | `summary` (600 characters), `btc` and `eth` (`direction` UP, DOWN or FLAT and `confidence` 0-1), 0-12 `factors` (`name`, `note`, optional `source`), 0-20 `events` (`at` or null, `what`, optional `source`) |
| `coins` | Exactly one entry for every coin of the context's universe: `direction` UP, DOWN or FLAT with `confidence`, optional `expected_move_pct` and 0-4 `reasons` (`kind` NEWS, EVENT, TECHNICAL, FUNDAMENTAL or MARKET, `text` up to 200 characters, optional `source`); or SKIPPED with a `skip_reason` of up to 200 characters and nothing else |

A source is the report's source object. The text may not name Muse (422
`AGENT_IDENTITY_IN_OUTLOOK`), and credentials, e-mail and 0x addresses refuse it whole. A
missing or extra coin is 422 `OUTLOOK_COINS_INCOMPLETE` or `COIN_NOT_IN_UNIVERSE`. The receipt
(section 9.8) gives the outlook's forward window, `window_start` to `window_end` (24 hours from
receipt), and its `grading_day`: the night after the window ends, the market reality grades it.
A move under 1.5% is FLAT; a hit is the right direction whatever the confidence; a miss is a
move of 5% or more called FLAT, the other way or SKIPPED; a false alarm is UP or DOWN at
confidence 0.6 or more with a move under 1.5% or the other way. Muse sees the grades in
`lessons.outlook`.

**The review.** The nightly job (05:30 UTC) records the previous New York day's market reality:
every coin's move, the movers (the five largest risers and fallers and every move of 5% or
more) and where each move started. Before the next run, POST a `POST_MORTEM_V1` to
`/api/v1/lab/post-mortems` (at most 524,288 bytes) with up to 30 items. Example:
[section 9.9](#99-post-mortem).

| Field | Rule |
| --- | --- |
| `schema_version` | `POST_MORTEM_V1` |
| `note_id` | A new UUID; an exact resend returns the stored receipt, changed content is 422 `POST_MORTEM_IDEMPOTENCY_CONTENT_MISMATCH` |
| `generated_at`, `agent` | As for the outlook |
| `items[].subject` | `{"kind": "TRADE", "setup_id": ...}`, one of Muse's own closed trades, or `{"kind": "MOVER", "symbol": ..., "day": "YYYY-MM-DD"}`, a mover of a recorded day |
| `items[].cause` | `COIN_NEWS`, `MARKET_WIDE`, `NO_NEWS` or `SURPRISE` |
| `items[].knowable_before_move` | `true` only with a cited source whose `published_at` is at or before the move's start (a mover's `move_start_at`, a trade's entry fill); `false` when the cause came out after; `null` when unknown |
| `items[].summary`, `items[].pre_move_technicals` | Up to 400 and 300 characters |
| `items[].sources` | 0-4 report sources; `COIN_NEWS` and `MARKET_WIDE` need one |

Each item is accepted or refused on its own, with a code (`TRADE_NOT_CLOSED`,
`NOT_A_RECORDED_MOVER`, `KNOWABLE_BEFORE_MOVE_UNSUPPORTED`, ...). The latest accepted item for a
subject is the one the scorecard and the lessons read.

The scorecard, the market reality and the weekly review themselves are the owner's records:
Muse reads its own share through `lessons` only, and the results routes stay closed to its
token.

## 5. Timing

- **Schedule.** `RESEARCH_SCHEDULE_V1` from `MANAGED_RESEARCH_SCHEDULE_JSON`. The deployed value
  is `{"timezone": "America/New_York", "runs": ["08:00"], "grace_minutes": 60}`: one full daily
  run at 08:00 New York time, whose picks stay valid about 24 hours (owner, 2026-09-28 evening,
  from 2026-09-29). On 2026-09-28 it was a run every 2 hours, whose validity ended at the next
  run plus 60 minutes. The planned research loop (docs/RESEARCH-LOOP-V2.md) adds 2-hourly
  updates to the daily run. The context's `schedule` shows the live value.
  The examples were written for the daily 08:00 schedule, which is the deployed one again.
- **Window of a run.** A report for run R may be generated from R − 60 minutes. It can be
  valid at most until the next run + 60 minutes, and at most 24 hours after `generated_at`.
  Picks expire at their `valid_until`, bounded by `generated_at` + 86,400 seconds.
- **Late reports.** A report for R is accepted after R, but its validity still ends at the next
  run + 60 minutes. The later the report, the shorter its picks watch. After that point, answer
  the next run.
- **Missed runs.** Nothing happens. No report means no new picks; the earlier run's picks
  expire at their `valid_until`. No alarm in the app notices a missing report (section 10).
- **Supersession** (`RESEARCH_RUN_SUPERSESSION_V1`). When the next run's first selection is
  published, older `WATCHING` setups are revoked and older unadmitted selections declined
  (`SUPERSEDED_BY_NEW_RESEARCH`). Open trades are never touched by it.
- **Several reports for one run** are separate cycles. A symbol is selected at most once per run.
- **Report age.** `generated_at` must be at most 60 seconds old on arrival. Build, check and
  send in one go.
- **Reviews.** The request comes 30 minutes before T, the discussion reply is due within 15
  minutes, and a Jev flag must be answered within 15 minutes. Poll at least every 5 minutes.
- **Learning.** The outlook goes in each run before the report, under the same 60-second age.
  The nightly job runs at 05:30 UTC (01:30 in New York in summer, 00:30 in winter) and records
  the previous New York day's reality and scorecard; post-mortems about that day go in before
  the next run. An outlook is graded the night after its 24-hour window ends.

## 6. What Muse cannot do

- **Muse proposes; the app decides everything else.** Muse cannot send sizes, quantities,
  orders, cancels, stop or target changes, risk settings or execution overrides. No route
  accepts one, and every body refuses unknown fields. A suggested stop or target in a review
  answer is shown to Jev, who can only choose among code-computed options. AGENTS.md: "Muse
  can create/read candidates and read analytics only. Never accept size or execution
  overrides."
- **Muse never calls Jev or TypeSafe.** The app does.
- **Every broker action is the app's**, on the paper account only: an entry, a stop, a
  replacement or a sale. Each needs its own exact, one-use, five-second risk authorization.
- **Muse cannot read** the status, setups, results, the per-cycle picks readback, analytics,
  timelines or measurements (403). See section 10.

## 7. Errors, retries and limits

| HTTP | Meaning | Action |
| --- | --- | --- |
| 401 `AUTHENTICATION_REQUIRED` | Missing or wrong token | Stop and tell the owner; never retry blindly |
| 403 `TOKEN_ROLE_NOT_PERMITTED` | Route not on Muse's list | Do not call it |
| 403 `AGENT_IDENTITY_MISMATCH` | Another agent's record, or a report not naming `muse` | Fix the agent block; act only on Muse's trades |
| 404 | Review, flag or position not found | Re-poll |
| 409 | The review or flag cannot take this answer now | Re-poll; do not resend the same answer |
| 413 | Body too large | Shrink |
| 422 | Refused. `detail` is a code, `errors` lists field paths, never your values | Fix it and send again; a new ID is always safe |
| 431 `HEADERS_TOO_LARGE` | Headers over 16,384 bytes | Send fewer headers |
| 503 | A capability or the database is unavailable; nothing stored | Retry with backoff |
| Timeout, 5xx | Unknown whether it was recorded | Resend the exact same bytes |

**Retries.** Every write has an idempotency key: `report_id`, `outlook_id`, `note_id`,
`news_id`, `answer_id` or `flag_ref`. An exact resend returns the original receipt with
`idempotent_replay: true`. Changed content under the same key is refused: 422 for reports,
outlooks, post-mortems and news, 409 for answers and flags. Back off exponentially with jitter, for example 2, 4, 8, 16 and 30 seconds, and stop
when the window closes. The windows are the report's 60-second age, T for a review answer, and
15 minutes for a discussion reply or a flag answer.

<!-- muse-limits:start -->
| Limit | Value |
| --- | --- |
| Picks per report | 1–30 (target 20) |
| Skipped coins per report | 0–200, reason up to 200 characters |
| Report body | 1,048,576 bytes |
| Reviewed JSON per pick | 11,000 bytes, rationale 3,000 |
| Sources per pick | 0–8, excerpt up to 1,200 characters, 8,000 in total |
| Bars per chart pick | 20–64, each timeframe 60–86,400 seconds |
| Rationale claims | 1–8, text up to 300 characters |
| Report validity | up to 24 hours after `generated_at` |
| News, answer or flag body | 32,768 bytes |
| Answer or flag JSON | 12,000 bytes |
| Review request lead | 1,800 seconds before T |
| Discussion reply | 900 seconds |
| Exit-flag answer | 900 seconds |
| Poll hint | 60 seconds |
| Outlook body | 2,097,152 bytes |
| Outlook coins | one per universe coin, at most 230 |
| Post-mortem body | 524,288 bytes |
| Post-mortem items | 1–30, summary up to 400 characters, 0–4 sources |
<!-- muse-limits:end -->

## 8. The reference client: `research_agent/`

The kit Claude used, `python -m research_agent.run <command>`, can serve as Muse's client or as
a model for one. Detail: `research_agent/DAILY_PROCEDURE.md`.

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

The kit's three writes are `submit`, `outlook-submit` and `postmortem-submit`.

Flags:

- `--base-url` and `--run-dir`, one folder per day.
- `--token-file`, a mode-0600 file. The kit never prints the token.
- `--agent-id muse --agent-version <Muse's version>` on `build`.
- `all` runs `context` through `validate`, never `submit`.

Claude-specific, or not needed by Muse:

- `DAILY_PROMPT.md` is a prompt for a Claude session.
- Morning step 7 and evening step 4 of the procedure, the web research, are the parts only the
  researcher can do. Muse does its own research.
- `market` (Coinbase data) and `levels` (rules A and B, entries 0.6–6% under the mid, windows of
  20, 24 and 30 bars) are Claude's method. Muse may use its own, within the guidelines.
- `build --session` and agent `fable` are for the local session harness only.
- The kit imports `catalyst_lab`, so it runs from a checkout of this repository with `./run`.

Missing for Muse:

- The kit has no client for reviews, exit flags or position news. Muse makes those calls itself
  (section 9).
- Package cloud makes the kit refuse plain `http://` except on this machine's loopback, and
  refuse a token file that others can read.

## 9. Examples

Illustrative values, not research. Every block tagged `muse-example` is validated by
`tests/test_muse_connection_examples.py` against the app's own models. So is every block tagged
`muse-response`, against the receipts the app builds.

### 9.1 Report V3 with one CHART pick

A pullback on 1-hour bars. The fixture context lists `SOL/USD` with a `price_increment` of 0.01
and a live mid of 150.11.

```json muse-example:report-v3-chart
{
  "schema_version": "AGENT_RESEARCH_REPORT_V3",
  "report_id": "7b9c1f3e-2a4d-4e8b-9c6a-5d3f2e1b0a98",
  "generated_at": "2026-09-28T12:10:00Z",
  "valid_until": "2026-09-29T12:10:00Z",
  "run_slot": "2026-09-28T08:00:00-04:00",
  "context_as_of": "2026-09-28T12:05:12Z",
  "agent": {
    "agent_id": "muse",
    "agent_version": "muse-2026.09.28",
    "guidelines_version": "MUSE_RESEARCH_GUIDELINES_V6",
    "guidelines_sha256": "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d",
    "run_id": "0f1e2d3c-4b5a-4c6d-8e7f-a1b2c3d4e5f6"
  },
  "picks": [
    {
      "signal_id": "2026-09-28-SOL-pullback-1",
      "symbol": "SOL/USD",
      "kind": "CHART",
      "agent_current_price": "150.11",
      "agent_price_at": "2026-09-28T12:09:40Z",
      "levels": {
        "entry_trigger": "147.20",
        "max_entry_price": "147.43",
        "stop": "144.32",
        "target": "154.80"
      },
      "stated_reward_risk": "2.37",
      "reasoning": {
        "thesis": "Pullback long to a held 1-hour swing low. Quoted from the bars: the 06:00 UTC bar made a low of 147.20 and no later bar traded below it. Inference: buyers defended 147.20 after the rise from 144.90 to 154.80, so a retest of 147.20 is a lower-risk entry than the current price.",
        "why_now": "The swing low formed six hours before this report and held for five completed bars. Price is 150.11, 1.9% above it (calculation: (150.11 - 147.20) / 150.11).",
        "why_these_levels": "Entry 147.20 is the low of the 06:00 UTC bar, which no later bar traded below; max entry 147.43 is 0.15% above it on the 0.01 price increment. Stop 144.32 is 0.4% under 144.90, the lowest low of all 20 bars. Target 154.80 is the 02:00 UTC high, the highest high of the window and not the latest bar's. Calculation: (154.80 - 147.43) / (147.43 - 144.32) = 2.37 at max entry.",
        "risks": "The 154.80 high may cap the rebound before the target. A trade below 147.20 would turn the pullback into a reversal. There is no news catalyst; this is a chart-only pick.",
        "invalidation": "After 2026-09-28 12:10 UTC: a trade at or below 144.32 before the entry fills."
      },
      "selection_rationale": {
        "claims": [
          {"claim_id": "C1", "kind": "TECHNICAL", "text": "The 1-hour bar that started 2026-09-28 06:00 UTC made a low of 147.20.", "supported_by": {"source_ids": [], "bar_ids": ["SOL-1h-20260928T0600Z"]}},
          {"claim_id": "C2", "kind": "TECHNICAL", "text": "The 1-hour bar that started 2026-09-27 21:00 UTC made a low of 144.90.", "supported_by": {"source_ids": [], "bar_ids": ["SOL-1h-20260927T2100Z"]}},
          {"claim_id": "C3", "kind": "TECHNICAL", "text": "The 1-hour bar that started 2026-09-28 02:00 UTC made a high of 154.80.", "supported_by": {"source_ids": [], "bar_ids": ["SOL-1h-20260928T0200Z"]}}
        ],
        "why_now": "The 147.20 swing low has held for five completed 1-hour bars and price is 1.9% above it.",
        "why_these_levels": "Entry at the held swing low, stop 0.4% under the window low, target at the window high: 2.37R at max entry.",
        "why_over_peers": "Few listed coins show a held swing low 0.6-6% under the price with a window high that gives 2R or more.",
        "what_would_change_my_mind": "A trade at or below 144.32 before the entry fills.",
        "known_risks": ["The 154.80 high may act as resistance.", "Chart-only: no news catalyst."],
        "agent_confidence": {"level": "MEDIUM", "basis": "One held swing low on 1-hour bars; no catalyst."}
      },
      "sources": [],
      "technical_evidence": {
        "schema_version": "MUSE_OBSERVED_TECHNICALS_V1",
        "provider": "COINBASE_EXCHANGE_PUBLIC_API",
        "venue": "COINBASE",
        "feed": "1-hour candles",
        "source_url": "https://api.exchange.coinbase.com/products/SOL-USD/candles",
        "retrieved_at": "2026-09-28T12:06:30Z",
        "timeframe_seconds": 3600,
        "bars": [
          {"bar_id": "SOL-1h-20260927T1600Z", "started_at": "2026-09-27T16:00:00Z", "open": "146.10", "high": "147.05", "low": "145.60", "close": "146.80", "volume": "8421.3"},
          {"bar_id": "SOL-1h-20260927T1700Z", "started_at": "2026-09-27T17:00:00Z", "open": "146.80", "high": "147.90", "low": "146.40", "close": "147.60", "volume": "9105.7"},
          {"bar_id": "SOL-1h-20260927T1800Z", "started_at": "2026-09-27T18:00:00Z", "open": "147.60", "high": "148.35", "low": "147.10", "close": "147.95", "volume": "7788.2"},
          {"bar_id": "SOL-1h-20260927T1900Z", "started_at": "2026-09-27T19:00:00Z", "open": "147.95", "high": "148.10", "low": "146.90", "close": "147.20", "volume": "6912.4"},
          {"bar_id": "SOL-1h-20260927T2000Z", "started_at": "2026-09-27T20:00:00Z", "open": "147.20", "high": "147.70", "low": "145.95", "close": "146.30", "volume": "10234.9"},
          {"bar_id": "SOL-1h-20260927T2100Z", "started_at": "2026-09-27T21:00:00Z", "open": "146.30", "high": "146.85", "low": "144.90", "close": "145.40", "volume": "12876.1"},
          {"bar_id": "SOL-1h-20260927T2200Z", "started_at": "2026-09-27T22:00:00Z", "open": "145.40", "high": "146.60", "low": "145.10", "close": "146.45", "volume": "9540.6"},
          {"bar_id": "SOL-1h-20260927T2300Z", "started_at": "2026-09-27T23:00:00Z", "open": "146.45", "high": "148.20", "low": "146.30", "close": "147.95", "volume": "11020.8"},
          {"bar_id": "SOL-1h-20260928T0000Z", "started_at": "2026-09-28T00:00:00Z", "open": "147.95", "high": "150.10", "low": "147.70", "close": "149.80", "volume": "15433.5"},
          {"bar_id": "SOL-1h-20260928T0100Z", "started_at": "2026-09-28T01:00:00Z", "open": "149.80", "high": "152.40", "low": "149.30", "close": "151.90", "volume": "18760.2"},
          {"bar_id": "SOL-1h-20260928T0200Z", "started_at": "2026-09-28T02:00:00Z", "open": "151.90", "high": "154.80", "low": "151.40", "close": "153.60", "volume": "21345.9"},
          {"bar_id": "SOL-1h-20260928T0300Z", "started_at": "2026-09-28T03:00:00Z", "open": "153.60", "high": "154.10", "low": "151.80", "close": "152.30", "volume": "14210.4"},
          {"bar_id": "SOL-1h-20260928T0400Z", "started_at": "2026-09-28T04:00:00Z", "open": "152.30", "high": "152.90", "low": "150.20", "close": "150.70", "volume": "12987.3"},
          {"bar_id": "SOL-1h-20260928T0500Z", "started_at": "2026-09-28T05:00:00Z", "open": "150.70", "high": "151.20", "low": "148.60", "close": "149.10", "volume": "13654.8"},
          {"bar_id": "SOL-1h-20260928T0600Z", "started_at": "2026-09-28T06:00:00Z", "open": "149.10", "high": "149.60", "low": "147.20", "close": "148.40", "volume": "16021.7"},
          {"bar_id": "SOL-1h-20260928T0700Z", "started_at": "2026-09-28T07:00:00Z", "open": "148.40", "high": "149.90", "low": "148.10", "close": "149.50", "volume": "11234.5"},
          {"bar_id": "SOL-1h-20260928T0800Z", "started_at": "2026-09-28T08:00:00Z", "open": "149.50", "high": "150.60", "low": "149.00", "close": "150.20", "volume": "10456.2"},
          {"bar_id": "SOL-1h-20260928T0900Z", "started_at": "2026-09-28T09:00:00Z", "open": "150.20", "high": "151.10", "low": "149.70", "close": "150.40", "volume": "9876.4"},
          {"bar_id": "SOL-1h-20260928T1000Z", "started_at": "2026-09-28T10:00:00Z", "open": "150.40", "high": "150.90", "low": "149.60", "close": "149.90", "volume": "8765.9"},
          {"bar_id": "SOL-1h-20260928T1100Z", "started_at": "2026-09-28T11:00:00Z", "open": "149.90", "high": "150.40", "low": "149.40", "close": "150.10", "volume": "8123.6"}
        ]
      },
      "agent_confidence": "0.55"
    }
  ],
  "skipped": [
    {"symbol": "DOGE/USD", "reason": "No held swing low 0.6-6% under the price on 1-hour to daily bars."}
  ]
}
```

### 9.2 The 202 receipt

The receipt the app returns for the report above: exactly these keys.

```json muse-response:report-receipt
{
  "status": "MUSE_REPORT_RECORDED",
  "cycle_id": "7c113a6b-3125-550a-b2b2-f4aa77259d20",
  "contender_count": 1,
  "expires_at": "2026-09-29T12:10:00+00:00",
  "idempotent_replay": false,
  "research_origin": "EXTERNAL_RESEARCH_AGENT",
  "agent_id": "muse",
  "agent_version": "muse-2026.09.28",
  "polling_url": "/api/v1/lab/cycles/7c113a6b-3125-550a-b2b2-f4aa77259d20/outputs",
  "trade_authorized": false,
  "submitted_count": 1,
  "rejected_count": 0,
  "item_results": [
    {
      "index": 0,
      "signal_id": "2026-09-28-SOL-pullback-1",
      "status": "ACCEPTED",
      "item_key": "CRYPTO:SOL/USD",
      "revision": 1,
      "evidence_hash": "fc9851d6b42c0a3cab2d1a745e42e70926210092a8491459f35592a6810ce5aa",
      "dossier_bytes": 6898,
      "expires_at": "2026-09-29T12:10:00+00:00"
    }
  ],
  "report_schema_version": "AGENT_RESEARCH_REPORT_V3",
  "run_slot": "2026-09-28T08:00:00-04:00",
  "skipped_count": 1,
  "review_validity": "PACKET_EXPIRY"
}
```

A replay of the same bytes returns the same receipt with `"idempotent_replay": true`.

### 9.3 Review answer

To `answer_route` of a `DAY_REVIEW` item, by T.

```json muse-example:review-answer
{
  "schema_version": "AGENT_REVIEW_ANSWER_V1",
  "answer_id": "3d6f0a52-8c1e-4b7a-9f2d-6e5c4b3a2918",
  "decision": "CONTINUE",
  "what_changed": "Since the entry at 147.43 the price rose to 151.60 and made a higher 1-hour low at 149.35; no adverse news since entry.",
  "next_24h": "A retest of the 154.80 high while the 1-hour lows stay above 149.35.",
  "proves_wrong": "A 1-hour close below 149.35.",
  "suggested_stop": "149.10",
  "suggested_target": "156.40",
  "sources": [
    {
      "source_id": "staking-status-1",
      "url": "https://news.example.com/staking-status",
      "excerpt": "Staking withdrawals are processing normally.",
      "published_at": "2026-09-29T11:40:00Z",
      "retrieved_at": "2026-09-29T12:58:00Z",
      "stance": "SUPPORTS",
      "novelty": "NEW_FACT"
    }
  ]
}
```

### 9.4 Answer to a Jev exit flag

To `/api/v1/lab/exit-flags/{flag_id}/answer`, within 15 minutes. No suggested levels.

```json muse-example:exit-flag-answer
{
  "schema_version": "AGENT_REVIEW_ANSWER_V1",
  "answer_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
  "decision": "EXIT",
  "what_changed": "The 1-hour close at 148.90 broke the 149.35 higher low that the last continue relied on.",
  "next_24h": "A retest of the 147.20 swing low, with little room left above the stop.",
  "proves_wrong": "A 1-hour close back above 150.50 within the next two hours."
}
```

### 9.5 Exit flag

To `/api/v1/lab/positions/{setup_id}/exit-flag`, for Muse's own open trade.

```json muse-example:exit-flag
{
  "schema_version": "AGENT_EXIT_FLAG_V1",
  "flag_ref": "5c4b3a29-1807-4f6e-9d5c-4b3a29180716",
  "lifecycle_id": "2e7d9c41-6b3a-4f58-9e21-7c0d5b4a3f96",
  "what_changed": "The main venue for the coin paused withdrawals 20 minutes ago.",
  "next_24h": "Selling pressure while withdrawals stay paused.",
  "proves_wrong": "Withdrawals resume within the hour.",
  "sources": [
    {
      "source_id": "venue-status-1",
      "url": "https://status.example.com/incidents/withdrawals",
      "excerpt": "Withdrawals are temporarily paused while we investigate a delay.",
      "published_at": "2026-09-28T20:05:00Z",
      "retrieved_at": "2026-09-28T20:14:00Z",
      "stance": "ADVERSE",
      "novelty": "NEW_FACT",
      "primary_source": true,
      "asset_relevant": true
    }
  ]
}
```

### 9.6 Position news

To `/api/v1/lab/positions/{setup_id}/news`, after reading its `lifecycle_id` and
`news_revision` with a `GET` on the same path.

```json muse-example:position-news
{
  "news_id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
  "lifecycle_id": "2e7d9c41-6b3a-4f58-9e21-7c0d5b4a3f96",
  "expected_news_revision": 4127,
  "sources": [
    {
      "source_id": "network-upgrade-1",
      "url": "https://news.example.com/network-upgrade-date",
      "excerpt": "The upgrade is scheduled to activate on October 2.",
      "published_at": "2026-09-28T17:45:00Z",
      "retrieved_at": "2026-09-28T18:02:00Z",
      "stance": "SUPPORTS",
      "novelty": "NEW_FACT"
    }
  ]
}
```

A source may also carry `content_hash`, the SHA-256 of the excerpt; the server computes it when
it is absent.

### 9.7 The other receipts

A review answer:

```json muse-response:review-answer-receipt
{
  "status": "REVIEW_ANSWER_RECORDED",
  "review_id": "4f1e8d2c-7b6a-5e9d-8c3b-2a1f0e9d8c7b",
  "round": "FIRST",
  "decision": "CONTINUE",
  "received_at": "2026-09-29T13:05:12+00:00",
  "idempotent_replay": false,
  "trade_authorized": false,
  "position_modified": false
}
```

An answer to a Jev exit flag:

```json muse-response:exit-flag-answer-receipt
{
  "status": "EXIT_FLAG_ANSWER_RECORDED",
  "flag_id": "8e7d6c5b-4a39-5281-9f0e-d1c2b3a49586",
  "decision": "EXIT",
  "received_at": "2026-09-29T15:21:40+00:00",
  "idempotent_replay": false,
  "trade_authorized": false,
  "position_modified": false
}
```

Muse's exit flag:

```json muse-response:exit-flag-receipt
{
  "status": "EXIT_FLAG_RAISED",
  "flag_id": "6d5c4b3a-2918-5f7e-8d6c-5b4a39281706",
  "setup_id": "c3b2a190-8f7e-4d6c-9b5a-493827160504",
  "raised_at": "2026-09-28T20:15:02+00:00",
  "answer_due_at": "2026-09-28T20:30:02+00:00",
  "idempotent_replay": false,
  "trade_authorized": false,
  "position_modified": false
}
```

Position news:

```json muse-response:position-news-receipt
{
  "status": "POSITION_NEWS_RECORDED",
  "news_revision": 4133,
  "evidence_hash": "9f2c6b1e0d4a8c7e5b3f1a9d8c6e4b2a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c",
  "idempotent_replay": false,
  "trade_authorized": false,
  "position_modified": false
}
```

### 9.8 Market outlook

The run's outlook for a four-coin fixture universe (`BTC/USD`, `DOGE/USD`, `ETH/USD`,
`SOL/USD`): every coin once, one of them SKIPPED.

```json muse-example:market-outlook
{
  "schema_version": "MARKET_OUTLOOK_V1",
  "outlook_id": "3c2b1a09-8f7e-4d6c-9b5a-4e3d2c1b0a9f",
  "generated_at": "2026-09-28T12:09:50Z",
  "run_slot": "2026-09-28T08:00:00-04:00",
  "horizon_hours": 24,
  "agent": {
    "agent_id": "muse",
    "agent_version": "muse-2026.09.28",
    "guidelines_version": "MUSE_RESEARCH_GUIDELINES_V6",
    "guidelines_sha256": "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d",
    "run_id": "0f1e2d3c-4b5a-4c6d-8e7f-a1b2c3d4e5f6"
  },
  "market": {
    "summary": "Bitcoin holds its three-day range while Nasdaq futures trade flat before the US open. Large-cap L1s lead, memes lag, and no macro release is due today.",
    "btc": {"direction": "FLAT", "confidence": "0.55"},
    "eth": {"direction": "UP", "confidence": "0.5"},
    "factors": [
      {"name": "US equity futures", "note": "Nasdaq 100 futures within 0.2% of the prior close at 08:00 ET.", "source": null}
    ],
    "events": [
      {
        "at": "2026-09-28T18:00:00Z",
        "what": "Scheduled token unlock of about 2% of the supply.",
        "source": {
          "source_id": "unlock-1",
          "url": "https://news.example.com/unlock-schedule",
          "excerpt": "The next unlock of about 2% of the supply is scheduled for 18:00 UTC on September 28.",
          "published_at": "2026-09-25T09:00:00Z",
          "retrieved_at": "2026-09-28T11:58:00Z"
        }
      }
    ]
  },
  "coins": [
    {"symbol": "BTC/USD", "direction": "FLAT", "confidence": "0.55", "expected_move_pct": "1.2", "reasons": [{"kind": "MARKET", "text": "Range-bound for three days between two held levels.", "source": null}]},
    {"symbol": "DOGE/USD", "direction": "SKIPPED", "skip_reason": "Too few completed bars this morning to judge."},
    {"symbol": "ETH/USD", "direction": "UP", "confidence": "0.5", "reasons": []},
    {"symbol": "SOL/USD", "direction": "UP", "confidence": "0.62", "expected_move_pct": "3", "reasons": [{"kind": "TECHNICAL", "text": "Held the 1-hour swing low at 147.20 for five bars.", "source": null}]}
  ]
}
```

The receipt, when it arrives at 12:10:05 UTC (`event_seq` is the ledger's):

```json muse-response:market-outlook-receipt
{
  "status": "MARKET_OUTLOOK_RECORDED",
  "schema_version": "MARKET_OUTLOOK_V1",
  "outlook_id": "3c2b1a09-8f7e-4d6c-9b5a-4e3d2c1b0a9f",
  "agent_id": "muse",
  "agent_version": "muse-2026.09.28",
  "run_slot": "2026-09-28T12:00:00+00:00",
  "generated_at": "2026-09-28T12:09:50+00:00",
  "received_at": "2026-09-28T12:10:05+00:00",
  "window_start": "2026-09-28T12:10:05+00:00",
  "window_end": "2026-09-29T12:10:05+00:00",
  "grading_day": "2026-09-29",
  "coin_count": 4,
  "skipped_count": 1,
  "universe_count": 4,
  "event_seq": 4131,
  "idempotent_replay": false,
  "trade_authorized": false
}
```

### 9.9 Post-mortem

Two movers of 2026-09-27 from that night's reality: `SOL/USD` rose 7% from 14:00 UTC,
`DOGE/USD` fell 6% from 19:30 UTC.

```json muse-example:post-mortem
{
  "schema_version": "POST_MORTEM_V1",
  "note_id": "5e4d3c2b-1a09-4f8e-8d7c-6b5a4f3e2d1c",
  "generated_at": "2026-09-28T12:09:55Z",
  "agent": {
    "agent_id": "muse",
    "agent_version": "muse-2026.09.28",
    "guidelines_version": "MUSE_RESEARCH_GUIDELINES_V6",
    "guidelines_sha256": "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d",
    "run_id": "0f1e2d3c-4b5a-4c6d-8e7f-a1b2c3d4e5f6"
  },
  "items": [
    {
      "subject": {"kind": "MOVER", "symbol": "SOL/USD", "day": "2026-09-27"},
      "cause": "COIN_NEWS",
      "knowable_before_move": true,
      "summary": "An exchange announced staking support 55 minutes before the rally started; the move ran 7% by the close.",
      "sources": [
        {
          "source_id": "staking-1",
          "url": "https://news.example.com/staking-support",
          "excerpt": "The exchange will support staking for SOL starting today, the company said on Sunday.",
          "published_at": "2026-09-27T13:05:00Z",
          "retrieved_at": "2026-09-28T11:40:00Z"
        }
      ],
      "pre_move_technicals": "Tight 4-hour range under 150 for two days, volume rising into the announcement."
    },
    {
      "subject": {"kind": "MOVER", "symbol": "DOGE/USD", "day": "2026-09-27"},
      "cause": "MARKET_WIDE",
      "knowable_before_move": false,
      "summary": "Fell with the market after Bitcoin dropped 3% in an hour; no coin-specific news was found.",
      "sources": [
        {
          "source_id": "btc-drop-1",
          "url": "https://news.example.com/bitcoin-slides",
          "excerpt": "Bitcoin fell 3% in an hour on Sunday afternoon as leveraged longs were liquidated.",
          "published_at": "2026-09-27T19:40:00Z",
          "retrieved_at": "2026-09-28T11:45:00Z"
        }
      ],
      "pre_move_technicals": "Below its 20-bar average on 1-hour bars, near the week's low."
    }
  ]
}
```

```json muse-response:post-mortem-receipt
{
  "status": "POST_MORTEM_RECORDED",
  "schema_version": "POST_MORTEM_V1",
  "note_id": "5e4d3c2b-1a09-4f8e-8d7c-6b5a4f3e2d1c",
  "agent_id": "muse",
  "received_at": "2026-09-28T12:10:05+00:00",
  "accepted_count": 2,
  "rejected_count": 0,
  "item_results": [
    {"index": 0, "status": "ACCEPTED", "subject_key": "MOVER:2026-09-27:SOL/USD"},
    {"index": 1, "status": "ACCEPTED", "subject_key": "MOVER:2026-09-27:DOGE/USD"}
  ],
  "event_seq": 4132,
  "idempotent_replay": false,
  "trade_authorized": false
}
```

## 10. Open items

What Muse needs that the API lacks today, or that the owner has to set. None of them blocks the
daily loop.

1. **Reviews are off in the deploy example.** `MANAGED_MANAGEMENT_REVIEWS=DISABLED` makes
   Muse's review answers and exit flags ineffective, as section 4f describes. The owner decides
   when to set `ENABLED`.
2. **Several reads are closed to Muse's token.** It cannot read `/cycles/{cycle_id}/picks`,
   `/results/*`, `/analytics/*` or a trade's `/timeline`. It gets its latest run's per-pick
   status from the context and every cycle's events from `/cycles/{cycle_id}/outputs`. It reads
   a trade's maintenance by scanning `/outputs`.

   AGENTS.md says Muse may "read analytics". The owner decided on 2026-09-28 (package
   learning-app) that Muse reads its own sanitized lessons in the research context and that the
   results routes stay closed to agent tokens.
3. **No alarm for a missed run.** The app has no alarm when Muse's daily report does not
   arrive. The Mac's `MUSE_*` alarms watched the old local worker, not an external Muse.
4. **Cloud wording.** Package cloud's deployment guide still describes `MANAGED_API_TOKEN` as
   "the legacy Muse identity; unused while Muse is not deployed". It also generates
   `{"claude": ...}`. Both follow from the decision in section 2: give Muse `{"muse": ...}` and
   keep `MANAGED_API_TOKEN` unissued.
5. **The kit's report age.** The reference kit stamps `generated_at` when `build` starts, so its
   own `submit` to the deployed app usually fails the 60-second report age (section 8). A fix,
   restamping just before the `POST`, belongs to the kit's owners.
6. **The kit's learning steps.** The outlook, lessons and post-mortem steps of the reference kit
   are package learning-kit's, built against this guide's section 4g; until they land Muse makes
   those calls itself.
