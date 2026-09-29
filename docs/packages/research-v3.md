# Package research-v3 — research context API and report V3 (plan phase 1)

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-26-research-v3`, from `work/2026-09-24-product-plan` at `80eb952`. Plan:
`docs/CRYPTO-AGENT-LOOP.md` sections 1, 2, 3, 4.1 and 4.2 (owner-approved 2026-09-26). This
file carries the text the coordinator merges into `docs/PHASES.md` and
`docs/CONTRACT-RESOLUTIONS.md`; neither file is edited on this branch.

## PHASES entry (paste as written)

### 2026-09-26 — Research context API and report V3 (plan phase 1, package research-v3) (FIXTURE EVIDENCE ONLY)

Fixture and disposable-PostgreSQL evidence only: per-test databases, a mock Jev transport, a
fixture Alpaca asset list behind the read-only paper transport and the request-budget
governor, a fixture Alpaca market-data source (MockTransport) and the fixture paper venue. No
broker, provider, network or owner-ledger contact; no migration (schema stays 20); admission
SQL unchanged. The plan's live read test of the context against Alpaca Paper is still to do.

- **Research context** (`GET /api/v1/lab/research-context`, `research_context.py`, on Muse's
  route list, read-only): `as_of`; the schedule with the current run and the next runs; the
  report V3 limits; the tradable universe (active, tradable Alpaca crypto USD pairs without
  the nine owner-listed stablecoins, PAXG kept) with bid, ask, last trade, spread in bps, price
  increment, minimum order size, quantity increment and 24-hour volume, each with a source
  label; the caller's open trades (entry, stop, target, quantity, opened at, unrealized P&L at
  the bid), `pending_reviews: []` and recent outcomes (closed trades of 7 days with exit reason
  and `test_r`; the last run's picks with status). Asset list: one `GET /v2/assets` (crypto,
  active) through `ReadOnlyPaperTransport` over the account's `BrokerBudget`, a research read,
  cached an hour, fail-closed 503 when refused or empty. Quotes and trades: the Alpaca
  market-data latest quotes and trades, one request each per batch, cached under 5 s. Volume:
  the 24 completed hourly bars, cached until the next hour, retried after 60 s on failure.
- **`AGENT_RESEARCH_REPORT_V3`** (`research_report_v3.py`) on `POST /api/v1/lab/research-reports`
  (V2 and legacy unchanged): envelope with `run_slot`, `context_as_of`, the V2 agent block,
  1–30 picks and 0–200 skipped coins; picks with kind, the agent's price and its time, levels,
  stated reward-to-risk, optional validity, structured reasoning, the V1 rationale, 0–8
  sources, bars and an analytics-only confidence. Intake is format only, per pick, with its own
  code for each refusal (12 pick codes) and siblings proceeding; no geometry, reward-to-risk,
  stop-distance, price-grid or live-price check. Validity: `valid_until` at most the next
  scheduled run after `run_slot` plus the grace and at most 24 h after `generated_at`; each
  packet expires at its pick's validity; the review stays valid until that expiry.
- **`REVIEW_DOSSIER_V3`** (`research_dossier_v3.py`): the pick as sent (kind, every price, the
  stated reward-to-risk, the reasoning, the rationale, the sources and every bar) within the
  11,000-byte budget, without the agent's identity, either confidence or the signal ID;
  `invalidation` reaches Jev as `disproof`; the manifest records a field map. Over-budget picks
  are refused, never truncated.
- **Selection**: V3 cycles run under the configured rule unchanged (V2 by default; B1 or B2
  when activated) and their selections cross the unchanged admission SQL to an authorized
  entry, shown for V2 and B2.
- **Schedule** `MANAGED_RESEARCH_SCHEDULE_JSON` (`RESEARCH_SCHEDULE_V1`, `research_schedule.py`):
  strict JSON, invalid refuses startup (`AppSettings.from_env`) and preflight; optional
  (absent: report V3 answers 503). Deploy example: `{"timezone": "America/New_York", "runs":
  ["08:00"], "grace_minutes": 60}` and `MANAGED_REPORT_MAX_SECONDS` 86400 (was 1800).
- **Guidelines** `MUSE_RESEARCH_GUIDELINES_V3` (SHA-256
  `5c0afc834ef2dc698f0cf80f2fc790c80d7193ef824ce04a39381350c8366947`), a new section of
  `docs/MUSE-GUIDELINES.md` checked byte for byte against the code; V1 (`0c04bbea…`) and V2
  (`f4081b07…`) unchanged; the Muse worker still embeds V2.

Evidence: `tests/test_research_report_v3.py` (36) and `tests/test_research_context.py` (44),
80 new tests: V3 end to end through intake, dossier, review, selection, admission SQL and an
authorized entry (V2 rule and B2 rule); every pick code in one report with siblings
proceeding; every envelope refusal; exact replay without a universe read; 503 capabilities;
HTTP identity binding; the packet-expiry review lifetime against V2's 60-second window; the
context's universe filtering, caches, budget (a read refused before I/O), auth and roles, and
only the caller's trades and outcomes from real fills; schedule parsing, DST and invalid
cases; app and ops configuration; guideline immutability. Targeted existing modules (V2
intake, dossier, rationale, identity, service, app, ops, guidelines, complete cycle, worker,
hygiene, safety): 273 passed, 0 failed. The B1 and B2 modules' run was stopped by the
coordinator's disk-space interruption after 190 of their roughly 700 cases, all passing, so
those modules still need the full suite. Ruff clean. The full suite was not run here on the
coordinator's instruction (disk under 1 GB); the coordinator runs it.

## CONTRACT-RESOLUTIONS text (paste as written)

### `RESEARCH_SCHEDULE_V1`, `AGENT_RESEARCH_REPORT_V3`, `REVIEW_DOSSIER_V3` and `MUSE_RESEARCH_GUIDELINES_V3` (2026-09-26, package research-v3)

Owner decisions of 2026-09-26 (`docs/CRYPTO-AGENT-LOOP.md`): crypto only for now; trades
execute on Alpaca Paper, so picks must be coins Alpaca can trade (USD pairs, no stablecoins),
while research may use anything from the whole market; one research run a day at 08:00 New
York time, a second run at 20:00 switchable; 20 picks per run; Jev reads each pick exactly as
the agent sent it — the reasoning and every price, never the agent's name or confidence — and
the system check comes after Jev's selection. These are named versions under the owner's
2026-09-24 ruling; nothing earlier is edited. `AGENT_RESEARCH_REPORT_V2` and legacy bodies,
`REVIEW_DOSSIER_V1`, `MUSE_RESEARCH_GUIDELINES_V1` and `_V2` (hashes `0c04bbea…` and
`f4081b07…`), the selection rules V2, B1 and B2, both SKEPTIC question sets, the admission SQL
and every stored packet keep their definitions and bytes.

**`RESEARCH_SCHEDULE_V1`** (`MANAGED_RESEARCH_SCHEDULE_JSON`). Exact JSON object:
`timezone` (an IANA zone), `runs` (1–24 wall-clock `HH:MM` times, strictly ascending) and
optional `grace_minutes` (integer 0–720, default 60, shorter than the smallest gap between
consecutive runs). Any other key, type or value refuses startup and is reported by preflight;
absent, report V3 is refused with 503 `RESEARCH_SCHEDULE_NOT_CONFIGURED`. A run time missing
on a daylight-saving day is skipped that day; an ambiguous one uses its first occurrence.
Configured value: `{"timezone": "America/New_York", "runs": ["08:00"], "grace_minutes": 60}`.

**`AGENT_RESEARCH_REPORT_V3`**. Envelope: `schema_version`, `report_id` (UUID), `generated_at`,
`valid_until`, `run_slot` (a scheduled run instant), `context_as_of`, the V2 `agent` block
(required, bound to the credential as for V2), `picks` (1–30, target 20) and `skipped` (0–200
`{symbol, reason ≤ 200}`, a symbol once and never also picked). Rules: `generated_at <
valid_until <= generated_at + 24 h`; `valid_until <=` the next scheduled run after `run_slot`
plus the grace; `generated_at >= run_slot −` the grace; `context_as_of <= generated_at`; the
existing report age limit and exact-replay idempotency apply. Pick: `signal_id`, `symbol`,
`kind` (NEWS, CHART, BOTH), `agent_current_price`, `agent_price_at`, `levels` (`entry_trigger`,
`max_entry_price`, `stop`, `target`), `stated_reward_risk`, optional `valid_until` (after
`generated_at`, at most the report's), `reasoning` (`thesis` ≤ 1,000, `why_now` ≤ 600,
`why_these_levels` ≤ 600, `risks` ≤ 600, `invalidation` ≤ 400 characters),
`selection_rationale` (`AGENT_SELECTION_RATIONALE_V1`), `sources` (0–8, V2's rules; NEWS and
BOTH need one), `technical_evidence` (`MUSE_OBSERVED_TECHNICALS_V1`, 20–64 completed bars;
CHART and BOTH need it; level references optional and never compared with the levels) and
`agent_confidence` (0–1, analytics only). Intake validates format only, each pick on its own
in this order, the first failure naming it: `INVALID_RESEARCH_ITEM` (schema),
`PRICE_NOT_POSITIVE`, `SYMBOL_NOT_IN_UNIVERSE` (the research-context universe, read at most
once an hour), `NEWS_SOURCES_REQUIRED` / `TECHNICAL_EVIDENCE_REQUIRED`, `CITATION_UNRESOLVED`,
`PICK_VALIDITY_INVALID`, `AGENT_PRICE_TIME_INVALID`, `INVALID_SOURCE_EVIDENCE` /
`FUTURE_TECHNICAL_EVIDENCE`, `AGENT_IDENTITY_IN_PICK`, `DOSSIER_OVER_BUDGET`; siblings proceed.
There is no level-geometry, reward-to-risk, stop-distance, price-grid or live-price check at
intake. A pick's packet expires at its `valid_until` (else the report's), bounded by
`generated_at` plus `MANAGED_REPORT_MAX_SECONDS`, and its review stays valid until that expiry
(`review_validity: PACKET_EXPIRY`), not V2's `review_validity_seconds`. Until the Jev top 5–10
package, V3 cycles go through the configured selection rule unchanged.

**`REVIEW_DOSSIER_V3`**. The reviewed state of a V3 pick: `market` (CRYPTO), `symbol`, `kind`,
`agent_current_price`, `agent_price_at`, `levels`, `stated_reward_risk`, `valid_until`,
`thesis`, `why_now`, `why_these_levels`, `risks`, `disproof` (the pick's `invalidation`),
`sources`, `technical_context` (origin, pending independent checks and, when sent, every
submitted bar with V1's code-computed descriptive metrics) and `rationale` (without the
confidence), in canonical JSON form. Never the agent's identity, either confidence or the
signal ID; the agent ID as a whole word in agent-written reviewed text refuses the pick. It
must fit 11,000 bytes (rationale 3,000) and is never truncated. The manifest records
`field_map`, every bar ID and the omitted fields.

**`MUSE_RESEARCH_GUIDELINES_V3`** (SHA-256
`5c0afc834ef2dc698f0cf80f2fc790c80d7193ef824ce04a39381350c8366947`), the runtime text in
`docs/MUSE-GUIDELINES.md`: the daily run (read the context first, research the whole market,
20 picks from Alpaca's list or skipped coins with reasons), the report and pick fields,
current prices, levels on the price increment with the stop at least 2% below max entry and
reward-to-risk of at least 2 at max entry (checked by the system after Jev, so failing picks
are wasted), the expected reasoning, blindness (never the agent's name; confidences never
shown to Jev) and the budget. It is for report V3 agents; the Muse worker keeps V2.

## Files

New: `src/catalyst_lab/research_schedule.py`, `research_report_v3.py`,
`research_dossier_v3.py`, `research_context.py`; `tests/test_research_report_v3.py`,
`tests/test_research_context.py`; this file.
Changed: `research_cycle.py` (V3 dispatch and intake; `_review_deadline` gives V3 packets
their expiry and `_claim` and `_review` now call it, identical for V2; `_selected_body` copies
only research fields the state has, identical for every V2 and legacy state),
`managed_service.py` (the route, V3-aware identity check, 503 mapping), `managed_app.py`
(schedule setting, context service wiring over the runtime's credentials and budget, cleanup),
`managed_ops.py` (optional env name and preflight validation), `muse_guidelines.py` (V3
constants only), `deploy/private-paper.example.json`, `docs/API-CONTRACT.md`,
`docs/MUSE-GUIDELINES.md`.

## Deviations

1. **`invalidation` is reviewed as `disproof`.** The unchanged admission SQL requires the
   selected packet's `thesis`, `disproof` and `sources` to equal the reviewed state's, and
   `ManagedExecution.admit` and the position monitor read `thesis` and `disproof`; the
   configured question sets also say "disproof". Keeping the agent's field name would have
   meant editing shared execution code or a migration. The text is unchanged and
   `field_map` records the mapping; the reasoning fields are top-level in the state.
2. **Timestamps and decimals are canonical, not byte-identical.** Values reach Jev as the
   validated canonical JSON (for example a `+00:00` offset is written `Z`, text is stripped
   of surrounding whitespace), as for V2.
3. **The dossier adds V1's code-computed bar metrics** (SMA 5 and 20, volume ratio, dollar
   volume, spread, ages, gaps) beside every submitted bar. They are descriptive, labelled and
   the same as V2's; they are not checks.
4. **Every bar is shown**, unlike V2 (cited bars only), so 64 bars plus text rarely fit the
   budget; the guidelines advise 20–30 bars. Over budget is refused, never truncated.
5. **The identity screen** checks the submitting agent's ID only (intake knows no other
   agent's), as a whole word in any case, and skips source excerpts and URLs (third-party
   text), the symbol and schema literals. An agent whose ID is a common word (`instinct`) must
   avoid it in its own text; another agent's name is not refused.
6. **`run_slot` may lead by at most the grace** (`RUN_SLOT_IN_FUTURE`): the plan did not say
   how early a run may be prepared. Skipped coins are format-checked only (not against the
   universe).
7. **No price-grid check at V3 intake** (V2 checks recorded metadata): the plan puts the grid
   in the system check. Admission still refuses off-grid crypto levels
   (`CRYPTO_LEVEL_OFF_PRICE_GRID`) and bad geometry or reward-to-risk below 2 at max entry
   (`INVALID_OR_EXPIRED_SETUP`) after selection; the 2% minimum stop distance is enforced
   nowhere yet.
8. **Deploy example `MANAGED_REPORT_MAX_SECONDS` 1800 → 86400**, as instructed. It also lets V2
   packets live until the agent's own `valid_until` (up to 24 h, so admitted V2 setups can
   watch longer); V2's review window stays `review_validity_seconds` (1800 in the example).
9. **`r` in recent outcomes is the measurement's `test_r`** (gross P&L over the reserved
   planned risk), labelled `r_basis`; the fees-net-r package's official net R should replace
   it at merge.
10. **The schedule is optional**, not required: absent keeps V2 deployments valid and makes
    report V3 answer 503. It is recorded in each V3 cycle's `RESEARCH_STARTED`
    (`research_schedule`) but is not part of the runtime's configuration hash
    (`managed_runtime.py` untouched, as it belongs to other packages).

## Open items and questions

- **Question-set fit.** B2's `mechanism_contradicted` and `inference_labelled` ask about
  `economic_relationship`, which a V3 dossier does not carry, so under B2 those components may
  answer Insufficient evidence (NEEDS_REVIEW). V2's and B1's questions fit (thesis, excerpts,
  disproof). The Jev top 5–10 package's news and chart question sets should replace this.
- **Evidence tasks.** Under V2/B1/B2 a NEEDS_REVIEW V3 pick gets the existing evidence tasks; a
  revision through the evidence route takes V2's revision contract (it adds
  `economic_relationship` to the reviewed state; only B2 re-checks the budget). Should V3
  cycles be single-shot until the top 5–10 package?
- **Live read test** of the context against Alpaca Paper (plan phase 1 "proven by") is an
  owner step; this package made no network call.
- **Volume** is Alpaca's own venue volume (thin, per the 2026-09-25 decision on the liquidity
  gate), from its 1-hour crypto bars; it is not a whole-market figure.
- **Universe freshness.** Intake checks the universe cached for up to an hour; a coin delisted
  inside the hour can pass intake and is refused at admission by the live broker check.
- **Guidelines V3** are not injected into the Muse Codex worker (it still produces V2
  reports); moving that worker to V3 is a later package.
- **Pre-existing order dependence** (not changed):
  `tests/test_managed_service.py::test_research_and_trade_outputs_use_durable_cursor` reads only
  the first 100 events of the shared session ledger and fails when ledger-writing modules run
  before it in a hand-picked order; it fails identically at `80eb952` and passes in the normal
  alphabetical full-suite order. The new tests use per-test databases so they cannot shift it.
- **Status token** can read the context (every GET route is open to status by design); it sees
  the universe and schedule and none of any agent's trades.
