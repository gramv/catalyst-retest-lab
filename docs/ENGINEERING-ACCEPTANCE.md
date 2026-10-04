# Controlled engineering acceptance

**PAPER TRADING — SIMULATED. Not real money.**

The user authorized one actual Alpaca Paper bracket → fill → flatten to verify the plumbing.
Its signal starts with `TEST-`, its `record_purpose` is `ENGINEERING_TEST`, and `strategy_eligible`
is false. It must never appear in strategy performance or the public dashboard. This path is
implemented and simulated locally. The September 18 real paper run
partially filled and closed through protective safety with result FAILED_CLOSED. It did not pass
the nominal controlled-exit acceptance condition; no second attempt is queued.

## Operator path

1. Run the normal market/broker observer with the separate risk-role connection configured. The
   frozen policy is TTL 5 seconds and broker previous-close equity. Startup reconciliation must
   save that session's baseline and complete cleanly; market and trade-updates streams must be healthy.
2. Import a reviewed server-owned sector/theme mapping using `risk-import`. Keep credentials only
   in the environment. The account and local reservations must be empty for controlled acceptance.
3. During the regular session, prepare one engineering candidate JSON file with current, sensible
   whole-cent T/M/S/P levels, the reserved `TEST-` prefix, `catalyst: ENGINEERING_TEST`, and explicit
   thesis/disproof text identifying it as plumbing verification. No quantity is supplied. The usual
   long-level and 2R-from-T checks still apply; sizing remains 1% of live equity divided by M-minus-S.
4. Enroll it locally:

```sh
./run catalyst-lab engineering-acceptance --file /absolute/path/to/test-candidate.json
```

There is **no HTTP engineering, execution or override endpoint**. The CLI only enrolls a candidate;
its broker client is GET-only. The existing worker alone watches the real market feed and dispatches
through the normal risk authorizer. It requires a real printed trade ≤T, current ask ≤M, fresh quotes,
acceptable spread and a healthy feed. A quote or invented observation cannot stand in for a trade.

Engineering admission records the actual asset model, calendar and quote, plus an explicit
`ENGINEERING_PLUMBING_ADMISSION_ONLY` decision. It does not invent a research catalyst or fabricate
liquidity evidence for production admission. The asset lookup uses Alpaca's fixed paper endpoint.
[Alpaca asset reference](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id).

After a fill, the worker requests an audited `CONTROLLED_ACCEPTANCE` cancel/flatten. Each cancellation
and market close requires its own five-second risk authorization. If the entry remains unfilled,
the default 60-second engineering deadline cancels it; `--timeout-seconds` accepts 1–300 seconds.
This is a bounded plumbing-test timeout, not a strategy time-exit change. No test starts at or after
the session's close-minus-five-minute deadline, and enrollment never queues an after-hours order.

The worker records `PASSED` only after the controlled market exit is filled and the broker/local
position and reservation are cleared. An unentered/canceled test is `NOT_ENTERED`; a closed trade
without that controlled exit is `FAILED_CLOSED`. Unknown submissions and outages retain risk and
pending recovery rather than declaring success. No second engineering bracket is automatically
created, and enrollment refuses a second test once an engineering broker order has been recorded.

Poll the private `/api/v1/risk` diagnostics and the candidate's existing status/event endpoints.
The normal risk gate remains required afterward. Verify the complete audit export and the final
broker orders/positions before reporting real-paper acceptance complete.

## Evidence separation

The DB derives candidate purpose from the reserved prefix (case-insensitive for exclusion), including
malformed rejected TEST- inputs. The operator run itself requires uppercase `TEST-`. Purpose is
inherited by orders, fills and trade projections; candidate audit payloads carry the classification
before hashing. No role used by the app may relabel or delete the resulting history.

Private operational diagnostics retain engineering records. Account-wide exposure, reconciliation,
protective safety and daily halts include them. Engineering attempts use a separate ticker/day claim
namespace, preserving the strategy's original one-attempt rule for strategy candidates.

Strategy consumers use `lab.strategy_candidates`, `lab.strategy_orders`, `lab.strategy_fills` and
`lab.strategy_trades`. The `catalyst_reporting` role can read these views and cannot read underlying
tables. The existing analytics endpoint uses those filtered relations. Phase 5 projections and the
Phase 6 dashboard must use this reporting boundary; neither is implemented by this change.

The global audit chain keeps both operational and strategy evidence intact. Engineering evidence is
excluded from strategy reporting by provenance, not erased from the audit.

## Managed engineering-test enrollment (plan 0.10, ruling R6 option B)

**PAPER TRADING — SIMULATED. Fixture evidence only so far.** Everything below was proven on
disposable PostgreSQL with the fake paper venue and fake Jev transports
(`tests/test_managed_engineering.py`). Nothing here has contacted Alpaca or TypeSafe, and no
owner ledger was touched. The first real run is the owner's supervised 0.10 session
(runbook).

None of about 100 real Jev reviews has selected anything, so plan 0.10 needs a way to run the
first real managed trade that doesn't wait for an organic selection. The owner enrolls **one**
crypto setup from a command-line tool. It runs through the normal managed engine with every
execution gate in place, and it is kept out of every performance claim. This is the managed
counterpart of the V1 `TEST-` acceptance above. After it, the plan waits for an organic
selection (option A).

### What the owner runs

```sh
python -m catalyst_lab.managed_engineering enroll --config <private.json> --symbol BTC/USD \
  --entry-trigger <T> --max-entry <M> --stop <S> --target <P> \
  --expires-minutes <N> --reason "<why this test, at least 10 characters>"
python -m catalyst_lab.managed_engineering status --config <private.json>
```

There is no HTTP route for enrollment, so Muse and every other research agent cannot reach it.
The tool prints the enrollment event sequence, the packet's selection event sequence and its
packet ID (the `RESEARCH_SELECTED` event ID). It never prints a connection string or credential.
Refusals print as `ENGINEERING_ENROLLMENT_REFUSED: <CODE>`.

### Why the operator login (`catalyst_operator`)

Enrollment increases risk: it creates a setup that can trade without a Jev selection. It
therefore follows migration 015's operator controls, where risk-increasing decisions (releasing
a halt, resuming) belong to the operator login alone.

- `catalyst_operator` has no table grants. It acts only through the SECURITY DEFINER function
  `lab.operator_enroll_managed_engineering`, which only it may execute. That function also
  refuses any other session up front, including the owner's own `lab_owner` login.
- `catalyst_risk` was the alternative (the precedent is `managed_ops clear-protection-latch`,
  which writes an operator event with the risk role). It was rejected because it is the running
  app's own role, and the app is the process that serves Muse. If `catalyst_risk` could write
  an enrollment, a bug in the app could create a tradable setup that no Jev and no operator
  approved.
- A guard trigger on `lab.managed_events` refuses a `MANAGED_ENGINEERING_ENROLLED` event, or
  any packet carrying the enrollment policy or the `ENGINEERING_TEST` purpose, from every
  session other than `catalyst_operator`. `catalyst_app`, `catalyst_review` and `catalyst_jev`
  cannot insert managed events at all.
- The tool connects with `--database-url`, else `OPERATOR_DATABASE_URL`, else the
  configuration's `MANAGED_DATABASE_URL` with the user replaced by `catalyst_operator` and any
  password dropped. That last form is the local trust socket of the ledger. As 015 notes, role
  separation over trust authentication guards against code bugs, not against the OS user.

### What one enrollment writes (migration 017)

It writes two audited rows in one transaction, and nothing else:

1. `MANAGED_ENGINEERING_ENROLLED`. Body: `signal_id` (`TEST-…`, generated as
   `TEST-MANAGED-<12 hex>` unless `--signal-id` is given), `symbol`, `market: CRYPTO`, `levels`
   `{entry_trigger, max_entry_price, stop, target}` as exact decimal strings, `expires_at`
   (database clock plus N minutes), `cycle_id`, `reason`, `operator_role` (the session login),
   `selection_policy`, `purpose`, `execution_scope`, `grid_check`, `price_increment` and
   `jev_review: NONE_ENGINEERING_TEST`.
2. `RESEARCH_SELECTED`, a packet the runtime's normal selection loop picks up. Keys:
   `selection_policy: MANAGED_ENGINEERING_ENROLLMENT_V1`, `purpose: ENGINEERING_TEST`,
   `execution_scope: PAPER_ONLY`, `receipt_id: null`, `enrollment_event_seq` and the
   enrollment's symbol, levels, cycle and expiry. `thesis` and `disproof` are fixed text
   naming the setup as plumbing verification, and `sources` is empty.

No Jev request, receipt or judgment exists for either row. Migration 017 is DDL only; its
changes:

- `lab.managed_setups.receipt_id` loses `NOT NULL`. No column is added, so no historical row's
  `to_jsonb` or audit match changes.
- A CHECK allows a NULL receipt only for that policy and purpose, on a CRYPTO setup, and it
  forbids the engineering purpose on any receipt-bearing setup.
- A unique index allows one setup per enrollment.
- A second guard trigger lets a receipt-free setup be inserted only as the exact packet of a
  valid enrollment, with its own columns repeating that packet.
- `lab.managed_review_failure` is replaced. A packet under the enrollment policy goes to
  `lab.managed_engineering_failure`. Every other packet runs migration 014's body statement for
  statement (a test compares the stored function source with 014's text).

Admission accepts an enrollment packet only if it is the exact audited `RESEARCH_SELECTED`
packet written for an audited enrollment by `catalyst_operator`. It must also name the same
signal, symbol, levels, cycle and expiry; carry no receipt; have unexpired levels that still
pass the reward/risk rule; and no other engineering setup may be active. Otherwise it is
refused with one of these codes:

- `SELECTION_INTEGRITY_FAILURE`
- `ENGINEERING_ENROLLMENT_BINDING_FAILURE`
- `ENGINEERING_ENROLLMENT_EXPIRED`
- `ENGINEERING_LEVELS_INVALID`
- `MIN_REWARD_RISK`
- `ENGINEERING_TEST_ALREADY_ACTIVE`

### Refused at enrollment

| Code | Rule |
| --- | --- |
| `ENGINEERING_ENROLLMENT_CRYPTO_ONLY` | Symbol is not `^[A-Z0-9]{1,16}/USD$`; stocks are refused |
| `ENGINEERING_LEVELS_INVALID` | Levels are not exact plain decimals with `0 < S < T <= M < P` |
| `MIN_REWARD_RISK` | `P − M < 2 × (M − S)`: reward/risk below 2 at the maximum entry (the admission rule) |
| `CRYPTO_LEVEL_OFF_PRICE_GRID` | A level is off the broker price increment that admission recorded (`CRYPTO_ASSET_METADATA`) in the last day |
| `ENGINEERING_EXPIRY_INVALID` | `--expires-minutes` outside 1–240 |
| `OPERATOR_REASON_REQUIRED`, `…_TOO_SHORT`, `…_INVALID`, `…_TOO_LONG`, `…_CREDENTIAL_SHAPED` | Reason is empty, under 10 characters, multi-line or over 2,000 characters (the tool says `…_INVALID`, the database `…_TOO_LONG`), or shaped like a credential (the append-only ledger could never remove it) |
| `ENGINEERING_SIGNAL_ID_INVALID`, `ENGINEERING_SIGNAL_ALREADY_ENROLLED` | Not an uppercase `TEST-` signal, or the signal was used before |
| `RISK_HALT`, `DAILY_RISK_HALT`, `ACCOUNT_EXIT_PENDING` | Any unreleased execution halt (including an operator pause), today's daily halt, or a pending exit |
| `CORRELATION_UNKNOWN` | The symbol has no usable server classification |
| `ACTIVE_SYMBOL_ALREADY_MANAGED` | Another managed setup on the symbol is still active |
| `ENGINEERING_TEST_ALREADY_ACTIVE` | Another enrollment is still active |
| `ENGINEERING_ENROLLMENT_OPERATOR_ONLY`, `OPERATOR_PRIVILEGE_REQUIRED`, `OPERATOR_ROLE_REQUIRED` | Not the operator login |

An enrollment stays active in two cases:

- while its setup is not terminal;
- before admission, until it expires or admission refuses it for good (a
  `RESEARCH_ADMISSION_DECLINED` event, or a crypto refusal recorded under
  `crypto-admission-refused:engineering:<enrollment seq>`).

Without recorded broker metadata the grid check is deferred. Admission then performs the live
broker check exactly as for any crypto packet, and an off-grid refusal is final for that
enrollment.

### Every gate stays in place

Once enrolled, the setup takes the normal managed path:

- **Admission.** Server classification is required. One active setup per crypto symbol (the
  crypto form of the ticker/day attempt). The configured `MANAGED_RISK_POLICY_ID` row (the
  approved `JEV_MANAGED_RISK_V2`) and the randomized `FIXED_EXIT`/`JEV_MANAGED` arm are
  recorded. The broker price grid is checked.
- **Trigger.** An acknowledged stream print at or below T, with a fresh quote, ask at or below
  M and spread within 10 bps.
- **Authorization.** An exact one-use five-second risk decision and claim for every
  POST/DELETE/PATCH.
- **Risk and protection.** Sizing, market caps, correlation and buying power come from the
  policy row. The crypto day deadlines, the stop-limit protection, the mechanical target, stop
  and time exits, and the −3% daily cancel-and-flatten halt all apply.

Jev is never involved. No selection review exists, and the position monitor withholds every
management review of an `ENGINEERING_TEST` setup whatever its arm and whatever
`MANAGED_MANAGEMENT_REVIEWS` says. It writes `POSITION_REVIEW_SKIPPED {reason:
ENGINEERING_TEST}` once per lifecycle and fetches no bars for it.

### Evidence separation

- **Flagged.** Setup, position, result, measurement and execution-quality payloads carry
  `engineering: true`. The dashboard's trade table marks the row `ENGINEERING TEST`.
- **Excluded from aggregates.** `managed_daily_rollups` (`/api/v1/lab/analytics/daily`) never
  puts the setup in an item. It reports the setup only as `engineering_count`, with
  `engineering_scope: ENGINEERING_TEST_SETUPS_EXCLUDED_FROM_AGGREGATES`. The research funnel
  never counts it as a conversion. The results pages keep it listed, flagged, so the
  plumbing evidence stays visible.
- **Included where safety needs it.** Account risk (`lab.account_risk_reservations`, caps,
  correlation), reconciliation, protection, halts and the daily halt treat the setup like any
  other. The flag comes from `record_json.purpose`, which only the operator function writes
  and which migration 017 ties to receipt-free setups.

**One engineering order at a time.** This is one at a time, not one ever: once the setup is
terminal, or the enrollment has expired or been finally refused, a new enrollment is accepted.
V1's acceptance allowed a single engineering broker order per ledger; the managed test
deliberately does not, because a real paper run may need a second supervised attempt, which
the owner must enroll explicitly.

### Choices made where the plan left detail open

- The enrollment login is `catalyst_operator` (above). The connection derivation drops any
  password.
- `--expires-minutes` is bounded to 1–240. This is a supervised-session run limit, not a
  strategy rule; an open position keeps its own exits. The expiry uses the database clock,
  truncated to the second.
- Levels must be plain decimal strings (no exponents), checked by the same rule in the tool
  and in SQL.
- The grid check uses metadata recorded in the last day, the intake rule; otherwise admission
  performs the live check.
- Halts, pending exits, missing classification and an active setup on the symbol refuse at
  enrollment so the operator learns early. Admission checks all of them again.
- Admission identifies an engineering packet by its enrollment sequence instead of a receipt,
  both for idempotency and for the final crypto-refusal key. The runtime's selection query
  skips an already admitted enrollment packet, so it is not offered again each tick.
- `ENGINEERING_ENROLLMENT_BINDING_FAILURE`, `ENGINEERING_ENROLLMENT_EXPIRED`,
  `ENGINEERING_LEVELS_INVALID` and `MIN_REWARD_RISK` are permanent admission refusals.
  `ENGINEERING_TEST_ALREADY_ACTIVE` is transient.
- The staging switch `MANAGED_MANAGEMENT_REVIEWS` (`ENABLED`/`DISABLED`, required, no default)
  was added with this package at the coordinator's request, because an organic selection during
  the supervised session must not be reviewed either. The engineering skip applies in both
  settings.

Not done here:

- the acceptance-evidence script `scripts/managed_acceptance_evidence.py`;
- broker execution of `managed_ops operator flatten-all`, which records an audited request but
  reports `broker_action: NONE_UNTIL_ACCOUNT_SAFETY_WIRING` (see the runbook's abort rule);
- any real paper run.
