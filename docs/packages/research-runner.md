# Package research-runner — the daily research-agent toolkit (plan phase 8)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-27-research-runner`, from `work/2026-09-24-product-plan` at
`20aeff4`. Plan: `docs/CRYPTO-AGENT-LOOP.md` sections 1, 2 and 4.1; the research boundary
of `docs/MUSE-RESEARCH-BOUNDARY.md`. This file carries the text the coordinator merges
into `docs/PHASES.md`; that file is not edited on this branch. This package adds no new
named contract rule, so there is nothing to merge into `docs/CONTRACT-RESOLUTIONS.md`.

## PHASES entry (paste as written)

### 2026-09-27 — Research-agent daily toolkit (plan phase 8, package research-runner) (FIXTURE EVIDENCE ONLY)

Fixture evidence only: `httpx.MockTransport` stands in for every network boundary in
every test — the app's `research-context`/`research-reports` routes, Coinbase's public
candles, Alpaca's public quotes, and arbitrary news pages. No real network access in any
test, no broker or provider contact, no owner-ledger contact. 115 tests, ruff clean
(`src tests research_agent`).

- New top-level package `research_agent/` (outside `src/catalyst_lab`; never imported by
  the app; it imports `catalyst_lab.research_report_v3`/`research_dossier_v3` only to
  validate its own output with the app's real models — the same one-way research
  boundary `docs/MUSE-RESEARCH-BOUNDARY.md` already draws: research happens outside the
  app, which only ever receives a finished report over HTTP). Modules: `context` (`GET
  /api/v1/lab/research-context`; an offline Alpaca-public-quotes development fallback
  that `build` explicitly refuses to turn into a submittable report), `market`
  (Coinbase's public 1-hour and daily candles, aggregated to 2h/4h/6h, keeping only bars
  complete as of the recorded retrieval instant — never a later wall-clock read),
  `levels` (the level rules; see the deviation below), `sources` (visible-text
  extraction, exact excerpt cutting, re-fetch verification against the live page, and
  publish-time metadata reading — never invents a time or a paraphrase), `build` (the
  report-V3 builder: literal-only claims, the 48-hour catalyst-freshness rule, and every
  assembled pick checked against the app's own `_parse_pick`/`compile_pick_dossier`
  before it may reach `report.json`), `submit` (the package's one side-effecting call:
  `POST /api/v1/lab/research-reports`), `token` (bearer-token loading from a file or
  environment variable, never printed or logged), and `run` (the CLI, `python -m
  research_agent.run {context,market,levels,build,validate,submit,all}`; `all` runs only
  the deterministic steps and never calls `submit`).
- **Deviation from the reference scripts** (`artifacts/real-jev-topk-2026-09-27/README.md`;
  the research-agent-method memory note). The level rules port research3/levels2.py, the
  script the real Jev accepted twice on 2026-09-27, with one change to target selection.
  That script's target search could settle on a nearby pivot high while a higher bar sat
  elsewhere in the same cited window — exactly the gap real-Jev run 2 caught (AVAX, ARB
  and BAT vetoed `LEVELS_SUPPORTED_BY_BARS_NO` for targeting the latest bar's high, "not
  real resistance"; BTC and PEPE for "a higher high in the same window"). `levels.py`'s
  target is now always the cited window's true highest high, and is refused outright
  when the most recent bar made or tied it. This is one rule that removes both failure
  modes at once, at the cost of treating "the latest bar just made a fresh high" as no
  target found (a breakout setup, which stays disabled per the owner's standing choice,
  `docs/CRYPTO-AGENT-LOOP.md` 4.4) rather than searching for a lesser, weaker target.
- New `news.json` input schema (per coin, `catalysts`/`risks`/`fundamentals` buckets,
  each item with `claim`, `kind` and a `source` of `url`/`excerpt`/`published_at`) for a
  research session's own findings; `build.parse_news` validates its shape and
  `build.verify_news` re-fetches and checks every excerpt before anything from it can
  reach a pick, dropping and recording anything that fails, is unverifiable, or (for a
  catalyst) is older than 48 hours before that pick's `agent_price_at`.
- **Two structural checks added during review, beyond the reference scripts**, both
  caught by hand-tracing the schema rather than by a failing test, then covered by one:
  `selection_rationale.claims` is capped at 8 by the app's own schema
  (`catalyst_lab.muse_reports.SelectionRationale`); a coin with several verified news
  items plus the three fixed technical claims (C1-C3) could otherwise build a pick the
  app refuses outright for having too many claims, so `build_pick` now caps news-sourced
  claims at 5. Separately, `AGENT_RESEARCH_REPORT_V3`'s envelope refuses the **whole**
  report over `MAX_PICKS` (30) picks; a genuine whole-market run (the owner's own
  scope — 33+ coins on 2026-09-27) qualifying more than 30 setups would previously have
  built a report the server rejects entirely. `build_report` now defaults its pick limit
  to the schema's real `MAX_PICKS` (never unlimited) and, when trimming is needed, keeps
  the rule-A and higher reward:risk setups first (research3/builder3.py's own ordering),
  not an arbitrary alphabetical prefix.
- `research_agent/DAILY_PROCEDURE.md` (the step-by-step daily sequence, the verification
  rules, and the round-3 lesson — invented times and paraphrased excerpts — that
  `sources.py` exists to prevent) and `research_agent/DAILY_PROMPT.md` (the prompt a
  scheduler hands a fresh Claude session).

Evidence: `tests/test_research_agent_market.py` (14: the completed-bar cut inclusive
exactly at the retrieval instant, an hour-gap exclusion, Coinbase's product/candles/
ticker fetch entirely mocked). `tests/test_research_agent_levels.py` (22: held-low, the
0.6%/6% entry band and the 2% stop/2R reward-risk boundaries all at their exact edges,
coarser-increment rounding, rule B's held lower pivot with rule A tried first on every
timeframe, and the two target-refusal cases above with a dedicated test showing the code
picks the true window maximum in a case where the original script's nearest-pivot search
would not). `tests/test_research_agent_sources.py` (18: exact excerpt cutting including
curly quotes, the four verification states with the NEAR_MATCH overlap boundary computed
exactly at 0.8, publish-time extraction from a meta tag, JSON-LD (including `@graph`) and
a `<time>` tag with that priority order proven directly, and the never-invented-offset
rule on a bare date and an offset-less time). `tests/test_research_agent_context.py`
(12). `tests/test_research_agent_build.py` (22: the central claim — a built report is
independently re-verified against `catalyst_lab.research_report_v3.parse_report_v3`/
`check_pick` and `research_dossier_v3.compile_pick_dossier` called directly, not just
through research_agent's own wrapper — plus the BOTH/CHART split at the exact 48-hour
catalyst boundary, the age wording ("published N hours before this report"), the dossier
budget refusal on an over-length rationale, a forced-rejection path proving a refused
pick is recorded in `.rejected`/`.notes` (never silently dropped), the 8-claim cap holding
with 8 verified risk items, a malformed context row (a mid price but no quote timestamp)
skipping only that coin rather than crashing the whole build, and 32 qualifying coins
correctly trimmed to the server's 30-pick cap keeping the highest reward:risk setups).
`tests/test_research_agent_submit.py` (6, including that the bearer token is a header only,
never the request body). `tests/test_research_agent_token.py` (8, including that a
loaded token is never printed). `tests/test_research_agent_cli.py` (10: the full CLI
driven end to end from raw Coinbase hourly candles through a build the app's models
accept and a clean `validate`, plus `all` never calling `submit` and no fixture token
ending up in any written file). `tests/test_research_agent_no_secrets.py` (3: the
forbidden live-endpoint literal, derived from the app's own paper constant and never
spelled out, is absent from every module's source; no broker-order-placing or
risk-authorization `catalyst_lab` module is imported by any of them).

## Files

New: `research_agent/__init__.py`, `context.py`, `market.py`, `levels.py`, `sources.py`,
`build.py`, `submit.py`, `token.py`, `run.py`, `DAILY_PROCEDURE.md`, `DAILY_PROMPT.md`;
`tests/test_research_agent_{market,levels,sources,context,build,submit,token,cli,
no_secrets}.py` (9 files, 115 tests); `docs/packages/research-runner.md` (this file);
`docs/OPERATIONS-RUNBOOK.md` (a new "Research-agent daily kit" section, how to run it by
hand). No file under `src/catalyst_lab` is changed. `research_agent` reads
`catalyst_lab.research_report_v3`, `research_dossier_v3` and `muse_guidelines`; it writes
to none of them and is not imported by any of them.

## What a scheduled session must still do

This kit builds and checks everything mechanical; it cannot do the research itself. A
scheduled session still has to: read the context and actually research the web for
news, technicals and fundamentals on every coin the context lists
(`DAILY_PROCEDURE.md` step 4, covering the whole market per owner feedback, not a
narrowed subset); judge what is a genuine, fresh catalyst versus background or nothing
worth citing; write `news.json`; and read the build/validate output to decide whether
the report looks right before submitting. The two real-Jev runs on 2026-09-27
(`artifacts/real-jev-topk-2026-09-27/README.md`) show research judgment still matters
even with mechanical checks in place: run 2's five vetoes were all judgment calls about
target quality — the exact class of error this package's target-selection fix
addresses — but a research session can still hand this kit a technically-valid setup
that is a poor trade, or a paraphrased excerpt this kit correctly refuses to submit,
losing that pick for the day.

## Open items and questions

- **No real HTTP proof yet.** Every test uses `httpx.MockTransport`; nothing here has
  called the app's actual `/api/v1/lab/research-context` or `/research-reports`, fetched
  a real Coinbase candle, read a real news page, or been reviewed by the real Jev. The
  first live run, once the app and a real agent token exist, is the actual proof this
  kit works outside a fixture — this package is engineering plumbing, not that proof.
- **`build` refuses the offline context on purpose** (it has no schedule or report
  format to build a submittable envelope from). A "preview report before the app is
  running" workflow would need a separate, explicitly-labeled path — not built here.
- **News source discovery is entirely the research session's job.** This kit verifies
  and dates what it is given; it does not search, rank or suggest sources on its own.
- **`run_slot`, `context_as_of` and the `valid_until` cap are taken from the context
  exactly as read**, not recomputed from a local copy of the schedule engine — correct
  as long as `context` and `build` run close together within the same morning. A long
  gap between them (for example resuming yesterday's run folder) could make the server
  refuse the report (`RUN_SLOT_IN_FUTURE`/`REPORT_VALIDITY_AFTER_NEXT_RUN`) rather than
  silently building a wrong one; re-running `context` first is the fix, not a code
  change.
- **The 20/24/30-bar windows and the 4h/6h/1d/2h/1h timeframe order carry over unchanged**
  from the scripts that passed real review twice; the owner can adjust either later as
  a deliberate, named change.
- **`agent_version`/`agent-id` are supplied by the caller on every run**, not fixed in
  this package; a scheduler or the owner picks them per the private `agents` config.
