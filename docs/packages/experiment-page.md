# Package experiment-page (public live dashboard)

Branch `pkg/2026-09-27-experiment-page`, started from `6aa1ae6`, brought up to
`work/2026-09-24-product-plan` at `7443686` (package answer-rules) before the final suite. The
merge had no conflicts.

- **Owner request, 2026-09-27:** "can we deploy it somewhere and present as an experiment".
- **Owner correction, the same day,** after the first design (a long experiment write-up with
  questions, rules, notes and statistics): "you kind of over did the page ... i want it simple
  overall figures today's figures past figures simple one no disclaimers ... jev and muse or
  research agent status and their decisions and overview of it", with live updates of trades.

The package is now a simple, public, read-only live dashboard of the paper-trading system. It
runs as its own Railway service, `experiment`.

- **Keys.** It holds no broker key and no Jev/TypeSafe key, and calls no exchange or provider.
  Every figure, including the current price of an open trade, is what the ledger already holds.
- **Database login.** It reads the ledger through a new least-privilege role, `catalyst_public`
  (migration 024), which can read only four sanitized views.
- **Deployment.** Nothing is deployed by this package. The cloud package owns the Railway
  runtime, the provisioner that gives `catalyst_public` its LOGIN, and the deployment document.
- **Evidence.** Everything is fixture evidence from disposable PostgreSQL clusters. No owner
  ledger was opened and no broker, TypeSafe or network call was made.

## Summary

- **Migration 024** (DDL only, schema 24) adds:
  - role `catalyst_public`: NOLOGIN, connection limit 8, USAGE on schema `lab`;
  - three pure helpers the views call, which never raise: `lab.public_decimal(jsonb)`,
    `lab.public_timestamp(jsonb)` and `lab.public_line(jsonb)` (one line of agent text, at most
    160 characters);
  - five internal helper views, granted to nobody;
  - four public views, SELECT to `catalyst_public` only:
    - `lab.public_dashboard_status`: heartbeat, halts, the paper account's last recorded
      equity, Jev's last call and calls today;
    - `lab.public_dashboard_trades`: one row per trade, with P&L components, the freshest ledger
      price of an open trade, Jev's last action and last level change;
    - `lab.public_dashboard_runs`: one row per research run, with Jev's ranking counts;
    - `lab.public_dashboard_decisions`: every decision the page lists, one row each;
  - one index, `managed_events(kind, event_seq)`.

  It writes no data row and no event. The only row is its own `schema_migrations` version.
- **The service** (`python -m catalyst_lab.experiment_page`, FastAPI):
  - Routes: `/` (the page), `/api/public/experiment` and `/api/public/experiment/{section}`
    (JSON), `/health`, `/experiment.js`, `/experiment.css`, `/favicon.svg`.
  - The page is a small HTML shell with the first JSON document embedded as an inert
    `application/json` block. `/experiment.js`, the only script, renders every section with DOM
    calls (never `innerHTML`) and polls the JSON every 5 s. Without JavaScript the page links to
    the JSON.
  - Content-Security-Policy: `default-src 'none'; script-src 'self'; style-src 'self'; img-src
    'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  - The JSON is cached for 5 s on the server. A failed rebuild serves the last build, marked
    `stale`, for at most 5 minutes, then answers 503.
- **Defense in depth.**
  - The service refuses to start when any `APCA_*` variable or `TYPESAFE_API_KEY` is present.
    Only the variable names are printed, never a value.
  - Every build checks that the login is `catalyst_public`, cannot read a base table and cannot
    create objects.
  - Every read is one `REPEATABLE READ, READ ONLY` transaction with a 15 s statement timeout.
- **Display definitions** are `EXPERIMENT_DASHBOARD_V1` (below). They are display rules, not
  trading or measurement rules.
- **Fixture banner.** A fixture database always shows a red "FIXTURE DATA — not real results"
  banner. This comes from `create_experiment_app(fixture_data=True)`, a function argument only.
  Tests and the screenshot script set it; no environment variable or command-line flag can.
  There is no other banner, disclaimer or note.

## What the page shows

One page, dense, phone-friendly, light and dark.

1. **Header.** The title (`EXPERIMENT_TITLE`, default "AI crypto trading — live"), a status pill
   (Running, Halted or Stopped) and "updated Xs ago".
2. **Overall.** Total P&L in $ and R, closed trades (won and lost), win rate, open trades (with
   their open P&L) and "Paper account" with the last equity the ledger recorded, when it has one.
3. **Today** (the New York day). P&L in $ and R of trades closed today, trades opened and
   closed, wins and losses, and research picks today with how many Jev selected.
4. **Live trades.** One row per open trade: coin with a "Jev-managed" or "Fixed exit" tag,
   entry, current price (with its age), stop, target, P&L in $ and R, time in the trade, and
   Jev's last action with its time and confidence ("Held · 1 min ago · 74%"). When the last
   action is a hold, the last level change follows ("Last change: Raised stop to breakeven
   (328.841) · 4 min ago"). A fixed-exit trade says "Fixed stop and target".
5. **Agents.**
   - One card per research agent, named as the ledger records it: last run, picks in the last
     run (selected by Jev, traded), the next scheduled run and the number of runs. Below it,
     its 24-hour review answers and exit flags, each with its one-line reason.
   - Jev's card: health (OK, Breaker open, Unavailable or No calls yet), last call, calls today,
     whether trade reviews are on, and the last selection (selected, passed, vetoed). Below it,
     Jev's latest decisions: selections, maintenance actions and 24-hour decisions, in plain
     words with a small confidence percentage.
   - Below the cards, a collapsible table of each agent's latest picks: coin, kind, entry,
     stop, target, a one-line why from the thesis, and Jev's verdict and rank. It keeps the
     reader's open or closed choice across refreshes.
6. **Latest decisions.** The latest 20 decisions of both, newest first, one line each, with the
   actor (Jev or the agent's name).
7. **Past days.** A small cumulative P&L line (inline SVG, no library) and per-day rows of
   trades, wins, P&L in $ and in R, plus a collapsible list of recent closed trades with their
   exit reason.

The first design's banner, questions, "how it works", rules, honest notes, "too early to tell",
managed-versus-control results, rule-version list and `PUBLIC_EXPERIMENT_REPORT_V1` statistics
rule are gone. The page states no review cadence; if one is shown again it must come from the
trade's recorded maintenance version (V2: every completed 1-minute bar).

## `EXPERIMENT_DASHBOARD_V1` (display definitions)

- **Trades in scope.** Report-V3 crypto setups of the managed cohort with a Jev receipt and at
  least one recorded buy fill. `ENGINEERING_TEST` setups and the engineering enrollment policy
  are never included. Trades carry opaque numbers (the audit order of their first buy fill).
- **P&L of a closed trade.** Net of fees when every fee is known: verified, coin inventory
  reconciled and no `LAB_FIXTURE` evidence. Otherwise the gross fill P&L, with a small
  "fees pending" tag. Nothing is estimated.
- **P&L of an open trade.** Held quantity × (the freshest price the ledger holds − the average
  entry). The price is the bid of the newer of the latest position sample and the latest
  maintenance quote. No price in the ledger: unknown.
- **R.** P&L ÷ (filled buy quantity × (admitted max entry − admitted initial stop)).
- **Win.** A closed trade whose P&L is above zero. The win rate counts only trades whose P&L is
  known.
- **Today.** The New York calendar day. A trade belongs to the day it closed (P&L, wins) or
  opened (opened count); a research run to the day of its slot.
- **Status.** Stopped when no runtime heartbeat is newer than 180 s (the runtime writes one at
  least every 60 s); else Halted with an unreleased execution halt or today's daily loss halt;
  else Running.
- **Jev's health.** Breaker open when the last heartbeat reports the breaker open or half-open;
  Unavailable when Jev's last call did not return a valid answer; OK otherwise; No calls yet
  before the first call.
- **Decisions.** A research run gives two lines: the agent's picks ("Sent 20 picks (run 4)")
  and Jev's selection ("Selected 10 of 20 (run 4): DOT, UNI, AAVE, ARB, RENDER +5 more", with
  the vetoed and unranked counts). Jev reviews an open trade every minute, so a trade's
  consecutive holds are one line with their count ("Held (9 reviews in a row)"); a "+" marks a
  count that may continue past the 300 decisions read. Per trade the view keeps the latest 60
  maintenance decisions.
- **Words.** An agent's reasons are one line of at most 160 characters. Codes pass only in their
  code shape (`^[A-Z][A-Z0-9_]{1,63}$`), agent names only as identifiers
  (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`) and symbols only as `XXX/USD`; anything else becomes
  unknown.
- **Next research run.** From `RESEARCH_SCHEDULE_V1` (`EXPERIMENT_RESEARCH_SCHEDULE_JSON`, else
  the owner's daily 08:00 New York run of 2026-09-26).

## Files changed

| File | Change |
| --- | --- |
| `src/catalyst_lab/migrations/024_public_experiment.sql` | New: role, helpers, views, index (DDL only) |
| `src/catalyst_lab/config.py` | `SCHEMA_VERSION = 24` |
| `src/catalyst_lab/experiment_report.py` | New: the dashboard document (Decimal arithmetic, plain words, feed folding) |
| `src/catalyst_lab/experiment_html.py` | New: the HTML shell and the escaped embedded JSON |
| `src/catalyst_lab/experiment_page.py` | New: app factory, credential refusal, login check, 5 s cache, headers, CLI |
| `src/catalyst_lab/static/experiment.js` | New: the page's only script (DOM rendering, 5 s polling) |
| `src/catalyst_lab/static/experiment.css`, `experiment-icon.svg` | New: self-contained style (light and dark, phone cards) and icon |
| `Dockerfile.experiment` | New: non-root image, installs the package, holds no secret |
| `scripts/experiment_page_screenshots.py` | New: disposable-cluster screenshots through headless Chrome |
| `tests/experiment_fixtures.py` | New: fixture ledger builder and the demo experiment |
| `tests/test_public_experiment_views.py` | New: role limits, DDL-only, exclusions, parity with the private readers, view contents |
| `tests/test_experiment_report.py` | New: display definitions and wording |
| `tests/test_experiment_page.py` | New: shell, refusals, headers, script checks, cache, port, leak scan |
| `tests/test_owner_migration_rehearsal.py`, `test_operator_flatten.py`, `test_selection_b2.py`, `test_managed_engineering.py`, `test_operator_controls.py`, `test_selection_topk_v2.py` | Schema pins 23 -> 24; the stepwise tests apply 024 after 023, as 023 did after 022 |
| `tests/test_ledger_backup.py` | `catalyst_public` added to the expected role set |
| `artifacts/experiment-page-2026-09-27/` | Nine fixture screenshots, `checks.json`, README |
| `docs/packages/experiment-page.md` | This record |

No file owned by the parallel packages was edited:

- cloud: `managed_ops.py`, `managed_app.py`, `run_managed_private.py`, `cli.py`,
  `cloud_provision`, the Railway configs and docs;
- answer-rules: `crypto_maintenance.py`, `maintenance_dossier.py`, `day_review*.py`,
  `trade_maintenance.py`, `jev_contract.py`, `exit_flags.py`, `agent_research_session.py`.

The fixtures only call `crypto_maintenance.admission_fields`, read-only, so fixture trades record
what admission records.

**Renumbering at merge.** If another package also takes 024, rename this file to the next free
number, and change its `schema_migrations` value, `SCHEMA_VERSION` and the pins listed above
together. Nothing in the views depends on the number.

## Migration 024 in detail

- **Scope.** A managed-cohort crypto setup whose admission record is `AGENT_RESEARCH_REPORT_V3`,
  has a Jev receipt, and is not `ENGINEERING_TEST` or the engineering enrollment policy. A
  research run is a `RESEARCH_STARTED` of report V3.
  - Report-V3 runs carry no engineering marker. Every managed research cycle stamps `purpose:
    ENGINEERING_TEST` as a legacy Jev-cohort label, so that field cannot separate them.
  - Operator engineering enrollments are never report V3 and are excluded twice: by receipt and
    by purpose.
- **Numbers, not identifiers.** Trades are numbered by the audit sequence of their first buy
  fill, research runs by their `RESEARCH_STARTED` sequence. No UUID leaves the database.
- **Fees** follow `managed_analytics.resolved_fill_costs` link for link: a fill's own fee, then
  its `FILL_COST_CORRECTION` chain, where every link must supersede the one before it; amounts
  must be exact and non-negative; the policy version, setup, fill sequence, source, reference,
  time order and coin quantity are checked. A bad link keeps that fill's fee unknown.

  **Known difference.** The two SHA-256 bindings (the fill binding and the evidence hash) must
  be present as 64-hex strings but are not recomputed in SQL, which would need Python's
  canonical JSON. A correction with a well-formed but wrong hash is therefore unknown on the
  private page and known here. Only a write outside the engine's own import path could create
  one; the private readers stay authoritative.
- **Net P&L and planned-filled risk** are exact numeric, as `managed_measurement` computes them.
  Tests assert equality for six fee scenarios.
- **Prices.** An open trade's price is read from `POSITION_MARKET_SNAPSHOT` (bid, else price) and
  `MAINTENANCE_DECISION.quote.bid`, whichever is newer, with its time. A closed trade has none.
- **Jev's actions** come from `MAINTENANCE_DECISION` (outcome, action, `answers.action.top_p`,
  levels after, first basis) and `DAY_REVIEW_DECISION` (outcome, the last round's
  `summary.decision.top_p`, code). Raw answers, receipts, requests and dossiers stay private.
- **Agent text.** A pick's `state.thesis`, an agent answer's `what_changed` and an agent flag's
  `reasons.what_changed`, through `lab.public_line`. Source excerpts, sources, the full report,
  hashes, references and the other answer fields stay private.
- **Never exposed:** account identity or hash, broker, client or fill IDs, request or response
  bodies, tokens, raw Jev/TypeSafe answers or receipts, source excerpts and any UUID. The
  heartbeat gives only its time, `management_reviews` and the Jev breaker state.
- **Portability.** Plain PostgreSQL 14-18 features only. The migration does not rely on the
  `public` schema, and `catalyst_public` gets no CREATE anywhere (tests check `lab` and
  `public`).

## Railway service definition

This section is for docs/RAILWAY-DEPLOYMENT.md; the coordinator carries it into
`.railway/railway.ts`.

- **Service:** `experiment`.
- **Build:** `Dockerfile.experiment` at the repository root: non-root user, `pip install .`,
  no secrets.
- **Start command:** `python -m catalyst_lab.experiment_page --host 0.0.0.0`. The port comes
  from `$PORT`, else 8080, so no `--port` and no shell are needed.
- **Health check:** `GET /health`. It answers 200 only when the database answers as
  `catalyst_public`; otherwise 503.
- **Deployment:** one replica, restart `ON_FAILURE`, a public domain.
- **Variables:**

  | Variable | Status | Value |
  | --- | --- | --- |
  | `EXPERIMENT_DATABASE_URL` | Required | Built by the cloud package from role `catalyst_public` and the Railway-generated `PUBLIC_DATABASE_PASSWORD` |
  | `EXPERIMENT_TITLE` | Optional | The page title (default "AI crypto trading — live") |
  | `EXPERIMENT_RESEARCH_SCHEDULE_JSON` | Optional, new | The research schedule, same format and value as the engine's `MANAGED_RESEARCH_SCHEDULE_JSON`, for "next run". Default: daily 08:00 America/New_York. An invalid value refuses to start |

  The first design's `EXPERIMENT_PRICE_REPLAY` is gone: the service makes no outbound call.
- **Never set** on this service: any `APCA_*` variable, `TYPESAFE_API_KEY`, `.env`, or a
  database URL for any other role. The service refuses the first two at start.
- **Role.** `catalyst_public` is created NOLOGIN by migration 024. The provisioner must run
  after the migrations and only `ALTER ROLE catalyst_public LOGIN PASSWORD ...`. The cloud
  branch's `cloud_provision.LOGIN_ROLES` already does this, from 024 on.
  - Connection limit 8; the service opens one short connection per rebuild (at most one
    every 5 s) or health check.
  - It can read only the four `lab.public_dashboard_*` views and execute the three helpers. It
    cannot read any base table, write, create or call any other function.
- **It cannot trade.** No broker or Jev key, no broker or HTTP client, no outbound call, and a
  database login that cannot write anywhere or read any order, account or request data.
- **Order.** The ledger must be at schema 24 before this service starts, since the views must
  exist; until then `/health` answers 503.

## Validation

All fixture evidence, on disposable PostgreSQL 14 clusters. No owner ledger, broker or TypeSafe
call.

- **Package tests:**

  ```
  XDG_DATA_HOME=~/.local/share/catalyst-wt-experiment-page ./run pytest -q \
    tests/test_public_experiment_views.py tests/test_experiment_report.py tests/test_experiment_page.py
  ```

  Result: **88 passed**.
  - 34 view tests: role limits, DDL-only, exclusion of `ENGINEERING_TEST` and non-V3 setups,
    fixture fee evidence, exact parity with `managed_measurement` (6 fee scenarios) and with
    `pick_outcomes.cycle_picks`, run counts, agent-name and thesis shaping, no excerpt in any
    view, the freshest price, Jev's last action and last change, the 60-per-trade cap, status,
    and one real engine-driven trade.
  - 30 display tests: net and gross P&L, open P&L, New York days, the status pill, Jev's health,
    the schedule, the wording, the feed folding and the run lines.
  - 24 service tests, including a reachable database without the views (it starts, then
    answers 503), the script checks (no HTML injection, fetches only its own JSON) and a leak
    scan of every response.
- **Tests following the schema bump, and the hygiene checks:** the package tests with
  `tests/test_owner_migration_rehearsal.py`, `test_operator_flatten.py`,
  `test_operator_controls.py`, `test_selection_topk_v2.py`, `test_selection_b2.py`,
  `test_managed_engineering.py`, `test_ledger_backup.py`, `test_repository_hygiene.py`,
  `test_localdb_guard.py`, `test_contract.py` and `test_frozen_rulings.py`: **833 passed**.
- **Full suite** (through the shared guard script, after the merge of `7443686`): see the final
  commit message for the count.
- **Lint:** `XDG_DATA_HOME=~/.local/share/catalyst-wt-experiment-page ./run ruff check src
  tests` passed with no errors. `scripts/experiment_page_screenshots.py` is clean too.
- **Entrypoint end to end.** A disposable cluster, `python -m catalyst_lab.experiment_page
  --host 127.0.0.1`, with `PORT` and `EXPERIMENT_RESEARCH_SCHEDULE_JSON` from the environment:
  `/health` answered 200 as `catalyst_public`; the page, `/experiment.js` and the JSON were
  served with the configured title and no `Server` header; with `APCA_API_KEY_ID` set, the
  process exited 2 and printed only the variable's name.
- **Wheel check.** `uv build --wheel` ships `static/experiment.js`, `experiment.css`,
  `experiment-icon.svg` and the migration.
- **Screenshots:**

  ```
  XDG_DATA_HOME=~/.local/share/catalyst-wt-experiment-page ./run python -m scripts.experiment_page_screenshots
  ```

  Nine JPEGs in `artifacts/experiment-page-2026-09-27/`: empty and demo, 1440 and 390 px, light
  and dark, plus the demo with its lists opened. `checks.json` records, on all nine: no sideways
  scrolling, no clipped table, no console error, one script that rendered and polled the JSON,
  and the fixture banner.

## Text for docs/PHASES.md (not applied there)

> ## 2026-09-27 - Public live dashboard (package experiment-page): FIXTURE EVIDENCE ONLY
>
> A public, read-only live dashboard of the paper-trading system, simplified at the owner's
> request the same day. One page shows overall figures, today's figures (New York day), live
> trades with Jev's last action, the research agents' and Jev's status and decisions, a feed of
> the latest decisions, and past days with a cumulative P&L line. The browser polls the JSON
> every 5 s; the server caches it for 5 s. No banner, disclaimer or note, except a fixture
> banner that production cannot enable.
>
> It runs as the Railway service `experiment` (`python -m catalyst_lab.experiment_page`,
> `Dockerfile.experiment`) with one database login, `catalyst_public`. Migration 024 (DDL only,
> schema 24) creates that role NOLOGIN; it can read only four sanitized `lab.public_dashboard_*`
> views and execute three pure helpers. The views never expose identifiers, bodies, tokens, raw
> Jev answers or source excerpts; trades and runs carry opaque numbers. Engineering trades and
> fixtures are excluded; P&L is net of verified fees, else gross marked "fees pending".
>
> The service holds no broker or Jev key, calls no exchange, refuses to start with `APCA_*` or
> `TYPESAFE_API_KEY` set, checks its login on every build, reads in `READ ONLY` transactions and
> serves one same-origin script under a strict Content-Security-Policy.
>
> Evidence: 88 new tests on disposable PostgreSQL, including exact parity with
> `managed_measurement` and `cycle_picks` and one engine-driven trade; schema pins moved to 24;
> nine fixture screenshots (`artifacts/experiment-page-2026-09-27/`). Not deployed; no owner
> ledger, broker or provider was touched.

## Text for docs/CONTRACT-RESOLUTIONS.md (not applied there)

> ### Display version `EXPERIMENT_DASHBOARD_V1` (2026-09-27, package experiment-page)
>
> **Authority.** The owner's request of 2026-09-27 for a simple public page of the system. A
> display rule only: no trading rule, stored record or measurement version changes, and no
> statistical claim is made, so no statistics rule is needed.
>
> - **Scope.** Report-V3 crypto trades of the managed cohort with a Jev receipt and a recorded
>   buy fill; never `ENGINEERING_TEST` or the engineering enrollment policy.
> - **P&L.** Net of fees when every fee is verified, the coin inventory reconciles and no fee
>   evidence is `LAB_FIXTURE`; otherwise gross fill P&L marked "fees pending". An open trade's
>   P&L uses the freshest bid the ledger holds; without one it is unknown. Nothing is
>   estimated.
> - **R.** P&L over filled buy quantity × (admitted max entry − admitted initial stop).
> - **Win.** P&L above zero. **Today.** The New York day.
> - **Status.** Stopped without a runtime heartbeat in 180 s; Halted with an unreleased
>   execution halt or today's daily loss halt; otherwise Running.

## Decisions to confirm

1. **Scope.** Only report-V3 crypto trades appear. Earlier managed trades (V2 reports, the
   2026-09-25 engineering trade) are excluded, and every report-V3 run counts.
2. **Gross while fees are pending.** A closed trade with unknown fees shows its gross fill P&L
   with a "fees pending" tag, and that gross figure is included in the totals, win rate and
   past days. The alternative is to leave such trades out of the totals until fees arrive.
3. **Today's P&L** is realized P&L of trades closed today; open P&L is shown separately (Overall
   and Live trades).
4. **Folding Jev's holds.** With reviews every minute, the feed shows a trade's consecutive
   holds as one line with a count, and each research run as two lines (picks sent, Jev's
   selection). Listing every decision literally would fill the feed with holds within minutes.
5. **Published amounts.** The page publishes paper P&L, entry, stop, target and current price
   of open trades, the paper account's equity, the agents' one-line reasons and Jev's
   confidence, updated about every 5 s.
6. **Exit label.** `BROKER_EXIT` is labelled "Stop hit". It means the position went flat at the
   broker without an app-requested exit: normally the resting stop, but a manual close in
   Alpaca's interface would look the same.
7. **Next run** uses `EXPERIMENT_RESEARCH_SCHEDULE_JSON`, else daily 08:00 New York. The schedule
   lives only in the engine's environment, so the cloud package should set the same value on
   both services.

## Open items and findings

- **Fees may stay pending.** Net P&L needs verified fees. If Alpaca's fee activities are not
  parsed as the fees-net-r package assumes (its open item 1, unconfirmed), every closed trade
  shows gross P&L with "fees pending".
- **Equity.** "Paper account" shows the equity recorded with the latest risk decision; it
  updates only when the engine makes one, so its time is shown.
- **Hash bindings.** See the known difference in the migration section.
- **Performance is unmeasured at scale.** The views ran on small fixture ledgers only. Each
  rebuild (at most one per 5 s, whatever the number of viewers) reads the latest 60 maintenance
  decisions of every trade; with hundreds of closed trades that is tens of thousands of rows.
  The index covers the kind-ordered reads; a later version could limit old closed trades.
- **API-CONTRACT.md.** The public routes are documented here, not there, to keep this branch
  away from a file other packages append to. A one-line pointer can be added at merge.
- **Merge-time test pins.** The cloud branch's tests may pin role sets or schema numbers that
  now include `catalyst_public` and 24.
- **Unrelated lint.** `ruff check scripts` reports one long line in
  `scripts/import_managed_costs.py` (from the 2026-09-24 baseline); the required
  `ruff check src tests` is clean.

## Update 2026-09-28: position size, unknown zero equity, folded failures, polish

**Owner, 2026-09-28:** "the dashboard doesn't show how many we bought and also the dashboard
needs a bit not a bit a lot of UI UX polish". Page-level only: no view, migration or trading
rule changes. The recorded `EXPERIMENT_DASHBOARD_V1` definitions (scope, P&L, R, win, today,
status) are unchanged; these are additions.

- **Size.** An open trade shows the quantity it holds (`open_qty`, else bought − sold), its
  value at entry (quantity × average entry) and its value now (quantity × the freshest ledger
  price). A closed trade shows the quantity it bought and its value at entry. Overall adds
  the open trades' value at entry ("invested"). Trades are labelled Long: every trade opens
  with a buy fill.
- **Paper account.** Protective decisions (stop changes, cancels, exits) record equity 0
  (`managed_execution._authorize_mutation`), so the page showed "$0.00" after every stop
  change. Equity that is zero or missing is now unknown ("—").
- **Repeated reviews.** A trade's consecutive reviews with the same unchanged outcome (held,
  failed, refused or discarded) are one line with their count, for example "UNI/USD: Review
  failed (5 in a row)". Level changes, flags and 24-hour reviews are never folded. The "+" also
  marks a streak cut by the view's 60 reviews of its trade.
- **Closed trades** are listed latest exit first, each with the running realized total.
- **Layout.**
  - Headline tiles: total P&L with a cumulative P&L line per closed trade (hover or keyboard
    readout), open P&L with the amount invested, win rate, and the paper account.
  - Today's strip, then the live trades: coin, side and size, entry → now, stop and target
    with a range bar, P&L in $ and R, time in trade and the next 24-hour review, and Jev's last
    action.
  - A visible closed-trades table, the agent cards beside the latest decisions, the latest
    picks, and past days with a bar per day.
  - Tables become cards below 1160 px, with no sideways scrolling at 390 px. Light and dark
    themes keep text contrast of at least 4.5:1.
- **Asset URLs** carry a content version (`/experiment.css?v=…`), so a browser never pairs a
  new page with an older cached stylesheet (cached for an hour) or script.
- **Later, needs a view change:** the status view takes the equity of the latest risk
  decision, so after a protective decision the page shows "—" until the next entry decision.
  Selecting the latest decision with equity above 0 would show the last real equity instead.

## Update 2026-09-28 (second): the redesign and the trade detail

**Owner, 2026-09-28,** on the headline-tile design: "it looks AI-ish, not a well designed stuff
… you can do better … it can show more details about the trade". Page-level only: no view,
migration or trading rule changes. The `EXPERIMENT_DASHBOARD_V1` definitions are unchanged;
the definitions below are additions (also in `experiment_report.py`'s docstring).

- **Design.** A report-and-terminal hybrid: IBM Plex Sans for text and IBM Plex Mono for
  figures, prices, times and codes (tabular), a 12/13/15/20/28 px scale, uppercase letter-spaced
  section labels, hairline rules instead of boxes, one accent (ink blue), gains a deep green and
  losses a muted red. No gradients, glows, shadows, pills or coloured card backgrounds. Light by
  default, dark by `prefers-color-scheme`; text keeps a contrast of 4.5:1 or more in both.
- **Fonts.** Self-hosted in `src/catalyst_lab/static/fonts` (Sans 400/500/600, Mono 400/500,
  woff2, 45–67 KB each) from IBM's `@ibm/plex-sans@1.1.0` and `@ibm/plex-mono@1.1.0`, with the
  SIL Open Font License 1.1 (`OFL.txt`). Served at `/fonts/{name}` (a fixed list; anything else
  404), cached for a year; the page preloads the two regular faces.
- **Layout.**
  1. Masthead: title, run status (a dot and a word), "updated Xs ago" and the paper account.
  2. Summary strip: total P&L ($ and R, closed count, fees pending), today (P&L, R, W–L, win
     rate, closed and opened, fees pending), open (unrealized P&L, positions, invested), win
     rate (W–L), trades (closed and open), research (last run → next run, the schedule, today's
     picks and selections).
  3. Positions: coin (with the arm and trade number), size (quantity, $ at entry), entry (and
     when), last (the ledger price and its age), stop and target (each with the distance from
     the price), P&L ($, % and R), time in trade, next 24-hour review, Jev's last action with its
     confidence and the last level change.
  4. Closed trades: size, entry → exit, exit reason, held, fees, P&L ($ and R), the running
     total and when it closed; "Show all".
  5. Research beside Jev. Research: schedule, last and next run, runs, today's picks and
     selections, the latest runs (time, picks, selected, traded) and the agents' 24-hour
     answers and exit flags. Jev: health, breaker, trade reviews on or off, calls today, failed
     reviews today, last call, the last selection, and a monospace decision log (time · coin ·
     action · confidence; holds and failures folded).
  6. Latest picks (collapsible), then Performance: daily P&L bars with the running total as a
     line (inline SVG) and the per-day table.

  The combined "Latest decisions" list is now split by actor between the research and Jev
  columns; the JSON `feed` section is unchanged. Nothing else was removed. Below 1180 px the
  trade tables become stacked rows (coin and P&L, then the levels) and research and Jev stack;
  below 900 px the picks do too; at 390 px nothing scrolls sideways.
- **Trade detail.** Every trade row expands (a `button` with `aria-expanded` and
  `aria-controls`; click or keyboard) into:
  - facts: value at entry (and now), the buy limit, stop and target from planned to current;
  - a price chart drawn by the page's script: 5-minute bars (coarser past 3.4 days) from an
    hour before the entry to an hour after the exit (or now), the entry line and marker, the
    stop as a step line through each recorded change, the target dashed, the exit or the last
    price, and a lane of Jev's reviews (holds grey, changes accent, failures amber, flags red).
    If the bars cannot be read it draws the levels alone and says "price history unavailable";
  - the timeline (below).
- **Price history.** The reader's browser, not the service, reads public crypto bars from
  `https://data.alpaca.markets/v1beta3/crypto/us/bars` with no key, cookie or referrer, sending
  only the coin, the window and the bar size. The Content-Security-Policy is now
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self';
  connect-src 'self' https://data.alpaca.markets; base-uri 'none'; form-action 'none';
  frame-ancestors 'none'`. The service still makes no outbound call and holds no key.
- **Trade events** (`events` on each live and listed closed trade, oldest first): the agent's
  pick (levels and its one-line why) and Jev's selection and rank, the buy (quantity, average
  price, limit = the admitted max entry, $), the stop and target set at entry, Jev's reviews
  (repeats folded with their span; a raise as old → new with its basis and confidence),
  24-hour reviews, the agents' answers and exit flags, the exit with its reason in plain words,
  and the fees and P&L. Read by one more query on the decisions view for the shown trades
  (`read_snapshot`'s `trade_decisions`), in the same read-only transaction. Codes stay private.
- **Trade levels** (`levels`): the stop and target from the entry and a step at each recorded
  level. The view keeps a trade's latest 60 reviews; for a trade with 60, a "not shown" line
  marks the earlier reviews, the streak that starts at the oldest listed review gets a "+", and
  the stop before the first recorded level is drawn dotted (`known: false`), because an older
  change may be missing. An open trade's last change always comes from the trades view.
- **Failed reviews today:** listed reviews of the shown trades that Jev did not answer (outcome
  FAILED: an error or a timeout, which the public data does not tell apart) on the New York
  day, "+" when a trade's 60 listed reviews all fall today.
- **Also:** the schedule in words (`agents.schedule`: "Every 2 hours", "Daily at 08:00"), the
  latest eight runs (`agents.runs`, `runs_total`), Jev's log of 12 lines, and gzip for responses
  over 1 KB (the polled document with events is about 7:1 smaller).
- **Tests:** `tests/test_experiment_report.py`, `tests/test_experiment_page.py` and
  `tests/test_public_experiment_views.py`: events and levels (story order, folding, old → new,
  the 60-review gap, fees pending), the schedule, runs, failed reviews today, the CSP, the
  script's only two fetches and their query, the disclosure and chart hooks, the fonts and
  gzip. With `tests/test_repository_hygiene.py`: **117 passed**; `ruff check src tests` clean.
  The full suite was not run for this page-level change.
- **Screenshots** are local only (not committed): desktop 1440 light and dark with a trade
  opened, phone 390 light with a trade opened, the empty page, and a verification page with
  UNI/USD and LTC/USD trades at real prices, whose charts drew the public bars.
- **Open items.**
  - The decisions view keeps a trade's latest 60 reviews (reviews run every minute), so a trade
    open for more than an hour shows only its last hour of reviews: an earlier stop raise of a
    closed trade is missing from its timeline and its chart shows that span dotted. A view
    change (a branch for applied level changes, not limited to 60) would restore them.
  - Failed and timed-out reviews cannot be told apart from the public view.
  - The chart reads a third-party public endpoint from the reader's browser; if Alpaca limits
    it (200 requests a minute per reader), the chart falls back to the levels.
  - `scripts/experiment_page_screenshots.py` still counts the old sections in its
    `checks.json` (`feedItems`, `agentCards`); its captures are otherwise unaffected.
  - Not deployed; it deploys only the experiment service.

### Follow-ups (owner, 2026-09-28: "Dashboard looks great")

Page-level only; no view or migration change.

1. **Headings.** "The headings of the main labels are a little small, they are not
   highlighted": each section now opens with a 2 px rule and a short accent tab, and its name
   in 17 px semibold ink (16 px on a phone), with its count beside it in the lighter tone.
   Field labels inside sections keep the small uppercase style.
2. **Research in more detail** (research runs every 2 hours):
   - **The current cycle** (`agents.cycle`) is the newest run slot Jev has ranked. A newer
     run's published selection supersedes an older run's picks (`RESEARCH_RUN_SUPERSESSION_V1`).
     The page shows the cycle's slot, when it was sent and selected, and when it stays valid
     until. That limit is the schedule's `validity_limit` (the next run plus the grace); the
     research kit sets reports to it, and a report's own earlier limit is not in the public
     views.
   - Its picks are listed in Jev's order: entry, stop and target, and Jev's verdict with its
     rank. Each has a status: in trade #N or closed #N (with that trade's P&L), no entry yet,
     expired, not selected, or no verdict.
   - **Entry vs the latest price.** The latest price is the ledger's for a coin in a trade.
     Otherwise it is the latest public one-minute bar (`/latest/bars`), which the reader's
     browser reads for the cycle's coins once a minute, only while that table is on screen.
   - `agents.pending_run` is a newer run that Jev has not ranked yet ("awaiting Jev").
   - `agents.today_runs` lists the New York day's runs by slot: sent, picks, selected and
     traded.
   - `agents.latest_run` replaces the branch's own `agents.runs`.
3. **Jev's decisions scroll, in two scopes** (`agents.jev.cycle`, `agents.jev.today`). "This
   cycle" runs from the current cycle's selection: its per-pick verdicts, then reviews, level
   changes, 24-hour reviews, and the trades' entries and exits. "Today" is the New York day,
   with each run's selection as one line.
   - Both are newest first with repeats folded, at most 150 lines each.
   - They are read by one bounded query on the decisions view (`DAY_DECISIONS`, at most 2,000
     rows since the earlier of the day's start and the cycle's selection), plus the cycle's
     picks (`CYCLE_PICKS`).
   - The list has a fixed height (460 px, 380 px on a phone). It is keyboard-focusable and
     keeps the reader's scroll position across refreshes. It returns to the top when the scope
     or the cycle changes; a new selection becomes "This cycle" on the next refresh. The
     toggle is two buttons with `aria-pressed`.
   - A fold fix: a trade's older buy line no longer closes a hold streak that the view's 60
     reviews cut, so that streak keeps its "+".
   - "Failed reviews today" now counts from the day's decisions.
- **Tests:** the targeted set (with `tests/test_repository_hygiene.py`) passes, **125 passed**;
  `ruff check src tests` is clean.
- **Checked in headless Chrome:**
  - the log scrolls, keeps its scroll position across a data refresh and resets on a scope
    change;
  - the toggle works by click, Enter and Space, and "This cycle" is disabled before the first
    selection;
  - no latest-price read happens until the cycle's table is on screen.
- **Open items:**
  - Jev's selection confidence per pick is not in the public views (the ranking's scores are
    private), so the verdict shows its rank only.
  - "Watching" cannot be confirmed from public data (admission declines are private), so a
    selected pick without a trade says "No entry yet".
