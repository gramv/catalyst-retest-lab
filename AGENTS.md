# Catalyst Retest Lab

Read README.md, docs/system-build-plan.md, docs/API-CONTRACT.md and docs/PHASES.md first.
This is a separate project from Vector Wrap; do not reuse its credentials, databases or runtime.

- Final v4.2 Part A (docs/V4.2-BUILD.md) overrides conflicting drafts: V1 touch trigger, admission reward/risk at M, no new entries at calendar flatten time. Jev implementation is gated on real credential/model verification.
- Frozen v3 plus the explicit Phase 4 rulings in docs/CONTRACT-RESOLUTIONS.md control safety.
  Phase 4 sizes on M-minus-S and cancels AND flattens on the daily halt. Preserve the source plan.
- Keep all execution paper-only. Do not add a live-order code path or the prohibited endpoint literal,
  including in tests or docs. Derive a forbidden-string assertion from the paper constant if needed.
- Muse can create/read candidates and read analytics only. Never accept size or execution overrides.
- Phase 3 transport remains GET-only. The separate Phase 4 transport must require a committed,
  unexpired, exact-request, one-use risk decision for every POST/DELETE. No flag bypass is permitted.
  The default runtime is read-only. Frozen Phase 4 choices: TTL exactly 5 seconds; broker previous-close
  equity captured once during startup/session reconciliation; TEST- engineering acceptance excluded
  from strategy reporting. Risk configuration enables only per-decision authorization, never a bypass.
  Preserve immutable engineering provenance. Reporting must use the strategy_* filtered views, while
  account risk/reconciliation must include engineering exposure. See docs/ENGINEERING-ACCEPTANCE.md.
  Keep unavailable evidence fail-closed and fixtures explicitly labeled in disposable databases.
- WATCHING requires acknowledged trade/quote/bar subscriptions. Use the frozen mechanical trigger in
  docs/system-build-plan.md; never invent a breakout/reclaim rule. Watching also requires clean
  current-process/current-session reconciliation and an authenticated trade-updates stream.
  New candidates stop at TRIGGER_CONFIRMED without an explicitly configured risk engine.
- Use Decimal arithmetic, whole shares, strategy version tags and independently computed outcomes.
- Append audit/correction events; never rewrite event history or edit a user's recorded decisions.
- Evolve schema with numbered migrations; application credentials must not own tables or schema.
- Run `./run pytest -q` for meaningful backend changes and `./run ruff check src tests`.
  The helper keeps runtime dependencies outside Documents/iCloud. Local DB files and credentials also
  live outside this repository. Never print tokens or commit secrets. In a git worktree, always run
  `XDG_DATA_HOME=~/.local/share/catalyst-wt-<name> ./run …` so the worktree gets its own venv: the
  shared venv is an editable install of the main checkout and `./run` re-syncs it.
- Never run `catalyst-lab dev-init` (or anything that calls `localdb.start`) against an owner ledger
  (`~/.local/share/catalyst-retest-lab/runtime`, `managed-real-*`, or any `LEDGER.json`-marked
  directory); it auto-applies migrations. Owner ledgers migrate only through the explicit,
  owner-run `ledger-migrate` step with a backup first.
- Maintain PHASES.md and distinguish fixtures/local PostgreSQL tests from broker/public-deployment proof.

- Jev Step 1–3 evidence: docs/JEV-ADAPTER.md and docs/jev-model-pin.json. Read the pinned official
  TypeSafe skill at ~/.local/share/catalyst-retest-lab/runtime/typesafe-skill/.agents/skills/typesafe-ai/SKILL.md
  for Jev work. Do not silently update it or promote SKEPTIC questions to an authorizing policy.
  Runtime code must never extract credentials from chat history. The owner explicitly authorized
  a one-time transfer of the already supplied TypeSafe key into .env; that bootstrap is complete.
  Future calls use the local credential loader. Latest owner instruction allows a local ignored,
  owner-readable mode-0600 .env; present .env takes precedence and fails closed if invalid.
  Keychain remains a noninteractive fallback. Railway uses secured service variables only;
  production must never read .env. Never print credentials or request them in chat.
- Owner direction 2026-09-19 expands the build beyond Step 4: external Muse research reports,
  app-hosted Jev selection/monitoring, paper execution for US stocks and crypto, India research-only,
  and contextual stop/target management. See docs/MUSE-JEV-MULTIMARKET.md for current gaps and
  pending market/provider choices. This is authorization to build, not proof these paths work.
  Preserve the frozen V1 baseline; managed exits require a separate tested exit policy/cohort.
  Rule changes are allowed only as named versions recorded in docs/CONTRACT-RESOLUTIONS.md (owner
  ruling 2026-09-24); never edit V1's definition or any history. Every broker change
  still requires the risk gate. Local testing first; deployment/Muse connection are deferred.
  Do not migrate or insert fixtures into the existing runtime DB for local proofs.
- Latest delegated local engineering choices and implemented US Jev bridge are in
  docs/US-JEV-INTEGRATION.md. Do not re-ask for those settings. The opt-in profile is
  separate from the frozen baseline and never upgrades historical inert intents.
  Preserve pending-review revocation checks at both DB risk reservation and dispatch.
  Actual Alpaca acceptance, managed amendments and crypto are still separate gates.

- Latest owner-authorized managed wiring is documented in docs/MANAGED-WIRING-2026-09-19.md.
  Schema13 adds a separate JEV_MANAGED_PAPER_V1 local engineering cohort for stocks/crypto;
  India remains research-only. No frozen US trigger rewrite. Every POST/DELETE/PATCH still
  requires exact one-use risk authorization. Both engines share account risk and US ticker/day
  attempt ownership (historical: from 2026-09-24 only the managed engine runs live; see the last
  section). Model choices are bounded completed-bar price IDs; protective code runs
  independently. Use actual acknowledged streams for entry prints, never latest-REST polling.
  The existing owner runtime database is schema8 and was intentionally not migrated by these
  isolated proofs. Do not confuse mock-provider fills with actual Alpaca acceptance.
- Historical (2026-09-19; nothing has run since the 2026-09-23 reboot — see the last section):
  docs/REAL-MANAGED-SESSION-2026-09-19.md. The original
  schema8 executor is stopped, not migrated; the active crypto-only managed worker
  uses ~/.local/share/catalyst-retest-lab/managed-real-20260919-a, API8768 and private
  read-only dashboard8769. Do not start another account executor alongside it.
  Credentials are held in the current worker environment only; a fresh launch needs
  secure environment injection. Preserve the running worker and reconcile before
  any shutdown. The first 36-pair technical pre-screen produced no eligible setups,
  no Jev judgments and no orders. This does not complete actual fill/exit acceptance.

- Owner correction 2026-09-19: Muse exclusively owns discovery, scanning, technical/news research
  outside the app. Current managed intake is POST /api/v1/lab/research-reports, accepting Muse's
  proposed levels and analysis, then app-hosted Jev review. Never restore the app scan endpoint
  or make a scanner prerequisite for review. The old scanner remains only a standalone/historical
  test utility. App market streams, position bars, trigger checks, independent risk and broker
  protection remain required. See docs/MUSE-RESEARCH-BOUNDARY.md. Do not restart the credential-
  holding real worker just to load edits; current-runtime limitations must be reported honestly.

- Historical (2026-09-19): the local Muse-facing intake was http://127.0.0.1:8770, started with
  scripts/serve_muse_intake.py. It shares the real worker DB but starts no broker/provider
  worker. Port8768 was the internal credential-holding executor API; do not call its
  legacy discovery route. External research reports feed the existing durable Jev loop.
  No new real reports/trades were inserted for the architecture-correction proof.

- Owner ruling 2026-09-24 and current state (details: docs/CONTRACT-RESOLUTIONS.md, the
  September 24 entry in docs/PHASES.md, and the approved plan at
  ~/.claude/plans/enchanted-floating-shore.md):
  - One paper account only. Numeric risk rules are not frozen for new work; they change only as
    named versions (`JEV_MANAGED_RISK_V2` is the approved first one). V1 stays archived unchanged.
  - Only the managed engine runs on the account; no V1 executor is launched against it. The
    control cohort is a randomized fixed-exit arm inside the managed engine.
  - Forex is research-only. Research agents (Muse, Instinct, Grogbot, …) must send a structured
    `selection_rationale` with every pick, and it must reach Jev. Since plan 1.3 (minimal form,
    2026-09-24) every `AGENT_RESEARCH_REPORT_V2` body also carries an `agent` block, each agent
    has its own token (private config `agents` list), cycle IDs derive from agent and report ID,
    and Jev never sees the agent: identity lives in event bodies and `evidence_identity` only.
  - Runtime state (2026-09-27): the owner migrated the live managed ledger 13 → 20 on
    2026-09-25 and ran the first real managed paper trade; the app has been stopped since. The
    code now requires schema 23 (owner-run `ledger-migrate --expect-current 20 --target 23`,
    runbook "Guarded migration"); broker credentials are process-local and must be injected
    again. Relaunch only through the guarded migration, credential injection, a single executor
    and a clean reconciliation.
  - Crypto research-and-trading loop (owner direction 2026-09-26, plan
    docs/CRYPTO-AGENT-LOOP.md): phases 0–8 are built on `work/2026-09-24-product-plan`
    (research context and report V3, Jev top-K selection — `JEV_TOP_K_SELECTION_V2` in the
    deploy example —, system check and replacement, crypto trigger/size/sectors, trade
    maintenance, the 24-hour review and early exits, results and pick tracking, gap/restart
    resume, and the external research kit `research_agent/`). Fixture evidence plus real-Jev
    session runs only (artifacts/real-jev-topk-2026-09-27/, artifacts/news-stale-check-2026-09-27/,
    artifacts/real-jev-loop-2026-09-27/); no broker proof of the new rules yet. Each package's
    record is in docs/packages/ and its rules in docs/CONTRACT-RESOLUTIONS.md.
  - Baseline snapshot: commit d3e9ea8, branch `work/2026-09-24-product-plan`, tag
    `snapshot-2026-09-24`. Package work happens on branches from it and is merged after the full
    suite passes; nothing is pushed without the owner.
