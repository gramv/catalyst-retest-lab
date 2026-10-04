# Research loop V2: a daily run plus 2-hourly updates (owner direction, 2026-09-28)

**Paper trading only.** This document is the design and build brief; nothing in it is deployed
until the packages below pass the full suite and the owner has been told.

## 1. Owner direction (2026-09-28, evening, by voice; paraphrased)

- **08:00 New York, the daily run.** Muse reviews yesterday's coins (keeps the relevant ones,
  drops the rest) and does fresh in-depth research: news, charts, open interest, funding,
  fundamentals. It sends about 20 picks with all of that research to Jev. Jev ranks them and the
  best 10 become setups (owner's answer: "Jev's best 10"; no database change). They stay live for
  24 hours.
- **Every 2 hours, an update.** Muse checks the setups that have not filled and the rest of the
  market.
  - For a setup whose plan has changed (for example a new entry and stop), Muse sends the
    adjusted pick. Jev reviews it, and the setup is replaced only if Jev selects it.
  - For a setup that is no longer relevant, Muse withdraws it.
  - Coins that now qualify go to Jev as new picks.
- **Open trades** are unchanged: Jev's per-minute maintenance and the 4-hour window review.
- Jev does not set prices. Muse proposes levels; Jev reviews and ranks them, as today.

Why (the evidence of 2026-09-28):
- Five 2-hourly runs re-created mostly the same setups at the same levels, each with only 2–3
  hours to fill. Three trades filled that day.
- A good daily pick (CRV, entry 0.3209, target 0.3729; CRV reached 0.3829) needs hours to
  fill, not one 2-hour window.
- The same-day study found that open interest, funding and momentum signals did not flag
  movers before they moved. They go to Jev as context, and the learning loop measures whether
  they help; the kit does not use them to choose coins.

## 2. Interim until the loop is live (owner's answer: "One daily run, 24h picks")

From the 2026-09-29 08:00 run: `MANAGED_RESEARCH_SCHEDULE_JSON` = `{"timezone":
"America/New_York", "runs": ["08:00"], "grace_minutes": 60}` (`RESEARCH_SCHEDULE_V1`), and
the kit's default validity is 23 h 50 min. The 2-hourly runs stop after the 06:00 slot.
(CONTRACT-RESOLUTIONS, 2026-09-29.)

## 3. Named versions (each recorded in CONTRACT-RESOLUTIONS when built)

### 3.1 `RESEARCH_SCHEDULE_V2`: built, `5fe6d46`

V1's JSON plus `"daily": "HH:MM"`, one of `runs`. For example:
`{"timezone": "America/New_York", "runs": ["00:00", "02:00", …, "22:00"], "daily": "08:00",
"grace_minutes": 60}`.
- `run_kind(slot)` is `FULL` for the daily run and `UPDATE` for every other run. Under V1,
  every run is `FULL`.
- `validity_limit(slot)` is the next full run after the slot, plus the grace. A report answering
  any run can therefore stay valid until the next daily run's grace, and still at most 24 hours
  after `generated_at`.
- Every other rule is V1's. A schedule without `daily` is V1, unchanged.

### 3.2 `RESEARCH_RUN_SUPERSESSION_V2` (package research-loop-app)

It applies to report-V3 selections and setups while the configured schedule is V2. Under V1,
`RESEARCH_RUN_SUPERSESSION_V1` applies unchanged.
- A `WATCHING` V3 setup with `run_slot` r is superseded only when a newer published V3
  selection exists (`RESEARCH_SELECTED` with `run_slot` > r) that is either:
  - from a **full** run (any symbol): the daily run replaces yesterday's set, as V1 does; or
  - for **the same symbol**, from any run: an adjusted pick replaces that coin's setup.
- An unexpired V3 selection that has no setup and no decline is treated the same way.
- Everything else stays until its own `expires_at`.
- Recording is as in V1:
  - a `REVOKE` keyed `research:superseded:setup:<setup_id>` with `run_slot`,
    `superseded_by_run_slot` and `supersession_rule: RESEARCH_RUN_SUPERSESSION_V2`, plus the
    `INVALIDATED` revision;
  - a `RESEARCH_ADMISSION_DECLINED` for an unadmitted selection.
- It uses the same lock and ordering as V1 (before admission, in the protection tick).
  Built: a V3 selection is offered for admission only in a tick whose supersession pass read
  it, so one published while a tick runs waits for the next tick. An adjusted pick therefore
  never meets the setup it replaces at admission, where `ACTIVE_SYMBOL_ALREADY_MANAGED` would
  decline it.
- An adjusted pick that Jev does not select supersedes nothing: the old setup stays.
- Setups in any other state are never touched.

### 3.3 `AGENT_RESEARCH_WITHDRAWAL_V1` (package research-loop-app)

`POST /api/v1/lab/research-withdrawals`, called with the agent's own bearer token (the report
submission auth: a research-agent token, or the legacy token acting for `muse`).

Body, exactly these keys:

```json
{"schema_version": "AGENT_RESEARCH_WITHDRAWAL_V1", "withdrawal_id": "<UUID>",
 "agent": {"agent_id": "muse", "agent_version": "<the agent's version>"},
 "items": [{"symbol": "XRP/USD", "reason": "<1-300 characters>"}]}
```

- `agent` has exactly `agent_id` (the credential's) and `agent_version`, with the report
  block's patterns. It is not the report's five-key block.
- 1–30 items, unique symbols in the universe form (`XRP/USD`); the reason is trimmed.
- The body limit is the report route's, 1,048,576 bytes. The report route has no app-level
  rate limit, so neither has this one.

Effect, per item, in one ledger transaction under the shared lock:
- each of **the calling agent's own** `WATCHING` V3 setups for that symbol is revoked
  `WITHDRAWN_BY_RESEARCH` through the revoke path (one `REVOKE` keyed
  `research:withdrawn:<withdrawal_id>:<setup_id>` with `withdrawal_id`, `withdrawal_reason`
  and the `agent` block, and its `INVALIDATED` revision);
- each of the agent's own unexpired, unadmitted V3 selections for that symbol gets
  `RESEARCH_ADMISSION_DECLINED` `WITHDRAWN_BY_RESEARCH` (the runtime's decline key, so it is
  never offered again; no `TOPK_REPLACEMENT_V1` decision follows).
- "Own" is the `agent` block the pick's packet recorded (in the setup record and the
  `RESEARCH_SELECTED` packet), read as the research context reads the caller's own trades.

Rules:
- A setup in any other state (entry working, open, closing, closed), and any other agent's
  setup or selection, is never touched.
- Admission refuses a selection declined this way (`WITHDRAWN_BY_RESEARCH`) even when it was
  admitting it at the same moment.
- The response is 200:
  `{"status": "RESEARCH_WITHDRAWAL_RECORDED", "schema_version", "withdrawal_id", "agent_id",
  "agent_version", "results", "idempotent_replay", "trade_authorized": false}`, with one result
  per item: `{"symbol", "result", "setup_ids", "selections_declined"}`.
  - `WITHDRAWN`: the revoked setup IDs and the number of declined selections.
  - `NOT_WATCHING`: nothing withdrawn; the agent's live setup on that coin has left
    `WATCHING` (entry working, open or closing). Its IDs are listed.
  - `NONE`: nothing of the agent's for that coin.
- A repeated `withdrawal_id` with an identical body returns the stored result
  (`idempotent_replay: true`). A different body gets 409 `WITHDRAWAL_ID_CONFLICT`.
- Refusals store nothing: 401 and 403 as for reports (403 `AGENT_IDENTITY_MISMATCH` when the
  agent block is not the credential's); 413 `RESEARCH_WITHDRAWAL_TOO_LARGE`; 422
  `INVALID_RESEARCH_WITHDRAWAL` with field paths and codes, or `SENSITIVE_EVIDENCE_REJECTED`;
  503 `RESEARCH_WITHDRAWAL_NOT_CONFIGURED`.
- One `RESEARCH_WITHDRAWAL` event (key `research-withdrawal:<agent_id>:<withdrawal_id>`)
  records the request and its results. Agent identity lives in event bodies only and never
  reaches Jev.
- **Permission change (owner direction "cancels irrelevant ones"):** Muse may withdraw its own
  unfilled candidates. A `WATCHING` setup has no broker order, so no size, order or execution
  effect exists, and the route makes no broker call; Muse's other limits are unchanged.

### 3.4 Research context (package research-loop-app)

`RESEARCH_CONTEXT_V3`: every V2 field unchanged, plus:
- `watching_setups`: the caller's own report-V3 setups whose state is `WATCHING`, by symbol;
  `[]` when there are none and for the status credential. Each row is exactly `{"setup_id",
  "symbol", "levels": {"entry_trigger", "max_entry_price", "stop", "target"}, "run_slot",
  "expires_at", "signal_id"}`: levels as decimal strings, `run_slot` in the schedule's zone
  (New York), `expires_at` RFC 3339. The owner is decided as for withdrawals.
- `open_trades` is unchanged. It lists filled trades only (it joins the fills), so a
  `WATCHING` setup never appears there; the design's first draft said otherwise.
- `schedule` under V2 only: keys in the order `version` (`RESEARCH_SCHEDULE_V2`), `timezone`,
  `runs`, `grace_minutes`, `daily`, `current_run_slot`, `current_run_kind` (`FULL` or
  `UPDATE`), `current_run_valid_until_limit`, `next_runs` (unchanged: RFC 3339 strings) and
  `next_run_kinds` (one kind per entry of `next_runs`). Under V1 the block is exactly V2's.
- `current_run_slot` is the latest run at or before `as_of`. A report may answer the next run
  within the grace instead, so the kit computes the kind of the slot it actually answers.
- The version follows the file's own convention (V2 was V1 plus `lessons`). The kit accepts
  V3 from package research-loop-kit on; both ship together.

### 3.5 Research kit, update mode (package research-loop-kit)

`python -m research_agent.run update --profile intraday …` runs these steps:
1. **Context.** It refuses unless the schedule is V2 and the answered slot is an `UPDATE` run.
2. **Market** (the profile's data).
3. **Review of the agent's own `WATCHING` setups** (from `watching_setups`, 3.4). Each coin's
   setup is found again:
   - **none** → withdraw, with the first failing rule as the reason;
   - **found, but materially different** (entry trigger moved at least 0.25%, or the stop or
     target moved at least 0.5%) → an adjusted pick;
   - **found and unchanged** → nothing.
4. **New coins**: no non-closed setup, and a qualifying setup now → new picks, in the profile's
   order.
5. **Output.**
   - It writes `withdrawal.json` (when there is anything to withdraw) and `report.json` (when
     there is at least one pick, capped by `--max-picks`, default 8), and validates both.
   - `submit` sends the withdrawal first, then the report.
   - An update with nothing to change sends nothing and says so.

### 3.6 Research kit, daily-run derivatives context (package research-loop-kit)

- For the daily run's picks, the kit reads:
  - OKX's public open-interest history (`/api/v5/rubik/stat/contracts/open-interest-history`,
    `<COIN>-USDT-SWAP`, 15-minute);
  - Hyperliquid's public funding history (`POST /info`, `fundingHistory`).
- Both are free and reachable from the Mac. Binance and Bybit refuse US connections.
- Each pick gets its OI change over 4 and 24 hours and its latest funding as a cited source.
  Crowded positioning is written as a `RISK` claim; neutral figures go in the reasoning text.
  Nothing is written without the fetched numbers behind it.
- It is context for Jev only; the kit does not choose coins by it.

## 4. Unchanged

- Jev's question sets and `JEV_TOP_K_SELECTION_V2` (K = 10 per run).
- The app's risk rules (2% minimum stop, 2R), trade maintenance, the 4-hour window, and every
  trigger, size and exit rule.
- Every stored record.

No migration is expected. If one turns out to be needed, the build stops and the owner is asked.

## 5. Rollout

1. Build the two packages on branches, with tests. Run the full suite in worktrees
   (`XDG_DATA_HOME=~/.local/share/catalyst-wt-<name>`), then merge.
2. Dry-run the kit's update mode against the live context without submitting.
3. Deploy the release (trader, ops, jobs, experiment). Then switch
   `MANAGED_RESEARCH_SCHEDULE_JSON` to V2 (the 12 two-hourly runs plus `"daily": "08:00"`) with
   a pinned config plan.
4. Timers:
   - 07:13: the daily run (unchanged, about 20 picks);
   - :07 of each other odd hour: `update`;
   - the day watch's missed-run guard learns the V2 slots.
5. Report to the owner. Measure fills per setup-hour, adjustments, withdrawals and Jev's
   acceptance of adjusted picks against the interim daily-only days.
