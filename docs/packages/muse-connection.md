# Package muse-connection — connecting Muse to the deployed app

**PAPER TRADING — SIMULATED. Not real money. FIXTURE EVIDENCE ONLY.**

Branch `pkg/2026-09-28-muse-connection`, from `work/2026-09-24-product-plan` at `7443686`.

Owner decisions of 2026-09-28:

- "you will not do research going forward muse will do it ... Once we deploy muse will access
  our app with api's and do research and communicate with jev."
- Relayed by the coordinator: "jev also stays in app muse will be the only outsider".

This file carries the text the coordinator merges into `docs/PHASES.md`. Neither that file nor
`docs/CONTRACT-RESOLUTIONS.md` is edited on this branch. No runtime code, schema, migration,
setting or rule changed.

## Summary

- **`docs/MUSE-CONNECTION.md`** is the one guide a person or an agent follows to connect Muse to
  the app deployed on Railway. It covers:
  - the connection and Muse's credential;
  - Muse's exact route list;
  - the daily loop: research context, research under `MUSE_RESEARCH_GUIDELINES_V5`, report V3,
    reading Jev's decisions, position news, maintenance readback, exit flags and the 24-hour
    reviews;
  - timing, what Muse cannot do, errors, retries and limits;
  - the `research_agent/` kit as a reference client;
  - tested examples of every request body and receipt, and open items.

  Every route, field, limit, time window and error code in it was checked against the code
  (`src/catalyst_lab/managed_service.py` and the modules it calls). The long
  `docs/API-CONTRACT.md` is linked for detail.
- **`tests/test_muse_connection_examples.py`** (17 tests) keeps the guide honest:
  - It validates every `json muse-example:<name>` block with the app's own parsers and models:
    `parse_report_v3`, `check_pick`, `compile_pick_dossier` and `system_check.evaluate` for the
    report; `validate_review_answer` for both answer bodies; `validate_exit_flag` for the flag;
    `PositionNewsService.submit` for the news.
  - The report example goes through the real route and intake, with Muse's token, on a
    disposable database. The receipt must equal the guide's section 9.2 byte for byte, and an
    exact resend must replay it.
  - The other receipts must equal what `TradeReviewService` and `PositionNewsService` build.
  - It checks the route table by probing every authenticated route of the app, the limits table
    against the models' constraints and the routes' inline limits, the deploy settings the guide
    quotes, and every link and anchor.
- **`tests/test_muse_connection_identity.py`** (7 tests) proves the credential decision below.

## The identity and token decision

**Give Muse its own research-agent token as agent `muse`:
`MANAGED_AGENT_TOKENS_JSON = {"muse": "<MUSE_TOKEN>"}`, the only entry. Do not give Muse the
legacy `MANAGED_API_TOKEN`.**

The three options, as the code resolves them (`agent_identity.py`,
`managed_service.create_managed_app`, `trade_review.addressee_for`,
`managed_app.configured_agents`):

1. **Legacy `MANAGED_API_TOKEN`.** It acts for agent `muse`, and it accepts any declared
   `agent_version`: `LEGACY_UNDECLARED` is only the old Muse worker's command-line default,
   never enforced by the server. So it could submit report V3, answer reviews addressed to
   `muse` and post news on Muse's trades. But it also sends unversioned, unattributed legacy
   reports, acts on unattributed legacy records, and reaches every route whenever the status
   and operator tokens are not configured. The cloud configuration also still needs a
   non-empty `MANAGED_AGENT_TOKENS_JSON`, so this option would leave a second credential
   configured anyway.
2. **Muse's own token as agent `muse`** (chosen). It does all of the above, scoped to the
   research-agent routes for agent `muse` in every configuration, and nothing more. Reviews of
   Muse's trades are addressed to `muse` (`PROPOSING_AGENT`).
3. **Muse's own token under another ID.** It works mechanically. But the blindness screens
   (`AGENT_IDENTITY_IN_PICK`, `_IN_NEWS`, `_IN_ANSWER`) search for the credential's own ID as a
   whole word, so text naming "Muse" would reach Jev unflagged. It would also split Muse's
   attribution from the `muse` identity.

What the deployment needs, and nothing more:

- **Role tokens, never given to Muse.**
  - `MANAGED_API_TOKEN`, required by the app at startup.
  - `MANAGED_STATUS_TOKEN` and `MANAGED_OPERATOR_TOKEN`, required by package cloud's
    `trader_config`, like a non-empty agent map; `{"muse": ...}` alone satisfies that.
- **`MANAGED_API_TOKEN` stays unissued and sealed.** It is the legacy identity of `muse`, so it
  could act as Muse.
- **No other research-agent credential.** `configured_agents()` is `{"muse"}` with or without
  Muse's entry, and no app setting names another agent. `fable` exists only in the local session
  harness.

The evidence, all in `tests/test_muse_connection_identity.py`, in exactly the cloud's token
shape (separate role tokens plus `{"muse": ...}`):

| Test | What it proves |
| --- | --- |
| `test_the_deployment_needs_one_research_agent_credential_and_it_is_muses` | `configured_agents` is `{"muse"}`; Muse's token must differ from every role token; the legacy token cannot be left out |
| `test_muses_token_submits_report_v3_with_its_own_agent_version` | 202 with `agent_version` `muse-2026.09.28` and the `muse` cycle ID. Another agent's report and an unattributed body are 403 `AGENT_IDENTITY_MISMATCH`; the role tokens are 403; the unissued legacy token can also report as `muse` |
| `test_muses_token_sees_and_answers_the_24_hour_review_of_its_trade` | The review of Muse's trade is addressed `{"agent_id": "muse", "basis": "PROPOSING_AGENT"}`; Muse's token lists it, answers it (`REVIEW_ANSWER_RECORDED`) and is recorded as `muse`; the status token sees nothing; the legacy token sees the same item |
| `test_muses_token_answers_a_jev_exit_flag_on_its_trade_with_the_guides_example` | A Jev exit flag on Muse's trade is asked of `muse`, and the guide's own answer example is recorded (`EXIT_FLAG_ANSWER_RECORDED`) |
| `test_muses_token_posts_news_and_raises_an_exit_flag_on_its_trade` | Position news (`POSITION_NEWS_RECORDED`) and Muse's own flag (`EXIT_FLAG_RAISED`, side `AGENT`, raised by `muse`); news naming `muse` is 422 `AGENT_IDENTITY_IN_NEWS` |
| `test_muses_token_reaches_the_research_agent_routes_and_no_other` | The token reaches exactly the guide's route table; status, cycles, picks, setups, results and analytics are 403 |
| `test_only_the_id_muse_screens_the_name_muse_out_of_what_jev_reads` | Under the ID `muse` the screens catch "Muse"; under `muse-cloud` they do not |

Follow-ups in package cloud's files, which this package must not edit:

- `scripts/cloud_secrets.py` defaults `--agent-id` to `claude`. Run it with `--agent-id muse`,
  adding `--rotate MANAGED_AGENT_TOKENS_JSON` if `{"claude": ...}` is already set. The token
  file it writes is what the owner hands to Muse, never through chat.
- `docs/RAILWAY-DEPLOYMENT.md` calls `MANAGED_API_TOKEN` "the legacy Muse identity; unused
  while Muse is not deployed" and generates `{"claude": ...}`. Both should now read: Muse gets
  `{"muse": ...}`, and `MANAGED_API_TOKEN` stays unissued.

## Validation

Only documentation and tests changed. As instructed, the full suite was not run: the new tests,
the related suites and ruff were.

| Command (prefix `XDG_DATA_HOME=~/.local/share/catalyst-wt-muse-connection`) | Result |
| --- | --- |
| `./run pytest -q tests/test_muse_connection_examples.py tests/test_muse_connection_identity.py` | 24 passed |
| `./run pytest -q tests/test_muse_connection_examples.py tests/test_muse_connection_identity.py tests/test_research_report_v3.py tests/test_agent_identity.py tests/test_day_review.py tests/test_early_exit.py tests/test_position_news.py tests/test_research_context.py tests/test_managed_ops.py` | 245 passed |
| `./run pytest -q tests/test_research_agent_*.py tests/test_answer_rules_day_review.py tests/test_selection_topk_v2.py` | 189 passed |
| `./run ruff check src tests` | All checks passed |

Fixture evidence only:

- per-test disposable PostgreSQL databases, the fixture paper venue and a mock Jev transport;
- a stub application for the route and size probes.

No broker, provider, network, Railway or owner-ledger contact. Not verified here:

- Anything on Railway: no deployment exists, and package cloud is not merged into this base.
  Its configuration rules are cited from its branch (`pkg/2026-09-27-cloud`, `cloud_config.py`
  and `cloud_secrets.py`), not tested here.
- A real Muse and real Jev answers.
- The reference kit's 60-second problem (open item 5), which was found by reading
  `research_agent/build.py` and `submit.py`, not run end to end.

## PHASES entry (paste as written)

### 2026-09-28 — Connecting Muse to the deployed app: the connection guide and its tests (package muse-connection) (FIXTURE EVIDENCE ONLY)

Owner decisions of 2026-09-28: Muse does all research from now on; once the app is deployed,
Muse reaches it through its API. Muse is the only outside caller, and Jev stays in the app.

New `docs/MUSE-CONNECTION.md` is the one guide for connecting Muse to the Railway deployment.
It covers:

- the connection and Muse's credential, and Muse's exact routes;
- the daily loop: research context, `MUSE_RESEARCH_GUIDELINES_V5`, report V3 and its receipt,
  per-pick and whole-report codes, retries, reading Jev's top-K decisions, position news, the
  per-minute maintenance readback, exit flags and the 24-hour reviews;
- timing, what Muse cannot do, limits, the `research_agent/` kit as a reference client, and
  tested examples.

It was checked against the code, not only against `docs/API-CONTRACT.md`.

**Credential decision.** The deployment gives Muse its own research-agent token as agent `muse`,
`MANAGED_AGENT_TOKENS_JSON = {"muse": ...}`, the only entry, and never the legacy
`MANAGED_API_TOKEN`.

- The agent token submits report V3 with Muse's own `agent_version`, answers the reviews and
  Jev exit flags addressed to `muse`, raises Muse's exit flags and posts its news.
- It is scoped to the research-agent routes in every configuration.
- Only the ID `muse` lets the blindness screens catch the name.
- The role tokens stay required and are never issued. `MANAGED_API_TOKEN` also acts for `muse`,
  so it stays unissued and sealed.
- No other research-agent credential is needed: `configured_agents()` is `{"muse"}`.

**Evidence.** 24 new tests.

- `tests/test_muse_connection_examples.py` (17):
  - every example body through the app's own parsers and models;
  - the report example through the real route with Muse's token, its receipt equal to the
    guide's;
  - the other receipts equal to what the app builds;
  - the route table against a probe of every route;
  - the limits table against the models and the routes' inline limits;
  - the quoted deploy settings, and every link.
- `tests/test_muse_connection_identity.py` (7): the credential decision in the cloud's exact
  token shape.

The new tests and the related suites pass (245 and 189), and ruff is clean. The full suite was
not run, since only docs and tests changed.

**Found.**

- `MANAGED_MANAGEMENT_REVIEWS` is `DISABLED` in the deploy example the cloud copies. Muse's
  review answers and exit flags have no effect until the owner enables it.
- Muse's token cannot read `/cycles/{id}/picks`, results, analytics or timelines.
- No alarm notices a missed Muse run.
- The reference kit stamps `generated_at` when `build` starts, so its own `submit` to the
  deployed app (60-second report age) usually fails.
- Package cloud's secrets script defaults to agent `claude`.

**Not shown.** Railway, a real Muse and real Jev answers. The owner ledger was not touched.

## CONTRACT-RESOLUTIONS text

None. This package documents the current contract and adds tests; no rule, version, threshold,
route or access changed, so there is nothing to merge into `docs/CONTRACT-RESOLUTIONS.md`.

## Open items

1. **Reviews are off in the deploy example.** `MANAGED_MANAGEMENT_REVIEWS=DISABLED` in
   `deploy/private-paper.example.json`, which the cloud copies. The 24-hour request still
   reaches Muse, but every review exits at T (`JEV_REVIEWS_DISABLED`), Muse's exit flags end
   `NO_ANSWER_IN_TIME`, and there is no Jev maintenance. The owner decides when to set `ENABLED`.
   `test_the_guide_quotes_the_deployed_settings` fails when the example changes, so the guide is
   updated with it.
2. **Reads Muse lacks.** Muse's routes, the plan 1.3 list plus the research context and the
   reviews, exclude:
   - `/cycles/{cycle_id}/picks`;
   - `/results`, `/results/picks` and `/results/maintenance`;
   - `/analytics/*`;
   - `/positions/{setup_id}/timeline` and `/measurement`;
   - `/cycles` and `/setups`.

   Muse gets its latest run's per-pick status from the research context, any cycle's events
   from `/cycles/{cycle_id}/outputs`, and a trade's maintenance by scanning `/outputs`.
   AGENTS.md says Muse may "read analytics", but the existing tests assert that agent
   credentials are refused analytics. Opening any of these is an access decision for the owner;
   this package changes none.
3. **No alarm for a missed run.** The app has no alarm when Muse's daily report does not arrive.
   The Mac's `MUSE_*` alarms watched the old local worker's spool.
4. **Package cloud's wording and default.**
   - `docs/RAILWAY-DEPLOYMENT.md` still describes `MANAGED_API_TOKEN` as "unused while Muse is
     not deployed" and generates `{"claude": ...}`.
   - `scripts/cloud_secrets.py` defaults `--agent-id` to `claude` and writes one agent per map.

   The guide gives the commands for `muse`.
5. **The reference kit's report age.** `research_agent/build.py` stamps `generated_at` when
   `build` starts, before it re-fetches the news pages, and `submit.py` posts the report
   unchanged. The deployed app refuses a report more than 60 seconds old
   (`RESEARCH_REPORT_STALE_OR_FUTURE`), so the kit's own `submit` works only if `build`,
   `validate` and `submit` all finish within 60 seconds. The session harness restamps at its own
   submit, which hid this. The fix, restamping just before the `POST`, belongs to the kit's
   owners: package cloud and package session-fixes both edit `research_agent/` now.
6. **The legacy token also acts for `muse`.** Retiring or narrowing `MANAGED_API_TOKEN` would be
   a code and access change for the owner. Until then it stays unissued and sealed.
7. **Observation, not a defect.** Every chart pick's bars carry the wire-format name
   `MUSE_OBSERVED_TECHNICALS_V1`, from every agent, so Jev reads the word "MUSE" as part of a
   format name. It names the format, not the proposer, and the screens treat it as a schema
   literal. Renaming it would be a new wire version.
