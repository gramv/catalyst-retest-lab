# Phase 4 validation — 2026-09-17

September 18 update: [the first real paper proving run](PROVING-RUN-2026-09-18.md) accepted one
bracket, partially filled 5 shares, and flattened safely through PROTECTION_FAILURE. Broker and
local exposure returned to zero. The immutable result is **FAILED_CLOSED**; nominal acceptance
is still pending. No further attempt is queued.

Status at the September 17 checkpoint below: implemented and tested locally;
**controlled Alpaca Paper acceptance NOT RUN** at that time.

The three final user rulings are now implemented. See
[FROZEN-RULINGS-VALIDATION.md](FROZEN-RULINGS-VALIDATION.md) for the subsequent 276-test run,
schema 6, baseline capture and engineering-evidence separation. The schema 5 evidence below is the
earlier checkpoint, retained for history.

## Local acceptance evidence

Tests use disposable real PostgreSQL databases, the restricted app/risk roles, fake broker HTTP
behind the actual authorization transport, and synthetic trade updates. The autouse test guard
blocks all external IPv4/IPv6 TCP connections. Tests cannot submit to an external broker.

| Acceptance | Evidence |
| --- | --- |
| M-minus-S whole-share sizing, zero-share skip, no leverage, live equity | `test_frozen_sizing_and_no_leverage`, `test_risk_uses_broker_equity_at_check_and_persists_atomic_reservation` |
| Atomic reservation under simultaneous triggers | Five concurrent RISK_CHECKs admit exactly two full 1% reservations; rollback injection leaves no partial decision/state/reservation |
| Correlation and working-order exposure | Same sector rejected, different sector admitted; configurable sector/theme count tested |
| Durable −3% realized + unrealized halt | −$200 realized plus −$100 unrealized on a $10,000 baseline cancels working entries, flattens positions, blocks restart on that date and admits a candidate in the next clean session |
| No decision means no submission | Service, direct client and direct transport denial tests; app role cannot create authorizations; DB rejects ORDER_SUBMITTED without same-transaction reservation |
| Fresh exact-request capability | Payload tampering, duplicate JSON keys, expiration, reuse and outside-session dispatch rejected before network I/O |
| Timeout and process-loss recovery | Acceptance followed by timeout, inconclusive 404, and death before receipt persistence all avoid a second entry POST; close-timeout recovery avoids duplicate flattening |
| Protective safety | Rejected stop, missing stop, held protection on partial entry and fill-during-cancel cases automatically flatten with independently authorized requests |
| Calendar exits | Authorized flatten at close minus five minutes for both 16:00 and 13:00 official closes |
| Reconciliation | Broker differences recorded; known pending receipts close readiness without being mislabeled permanent unexplained exposure; later resolved snapshot must be clean |
| Audit enforcement | App/risk UPDATE, DELETE and TRUNCATE denied for every new risk ledger table; imports and all dispatch/recovery steps reference hash-chained events |
| Paper-only endpoint and credentials | Existing Phase 3 endpoint/key/redirect/transport tests retained; prohibited endpoint literal absent |

Full suite: **265 passed**, 19.47 seconds. `ruff check`, `ruff format --check` and whitespace checks
passed. The only warnings are two upstream Starlette test-client deprecations. Existing Phase 3
submission-denial and endpoint/key rejection tests remain green. Exact authorized-key/token scanning
and the prohibited-endpoint scan both passed; no secrets were written to the repository.

## Persistent local migration and restart

Stopped the identified local API process, exported/verified its 91-event audit chain, and saved a
PostgreSQL custom-format backup outside the repository at
`~/.local/share/catalyst-retest-lab/runtime/backups/pre-phase4-20260917T230240Z`.
Applied migration 005 and verified both `catalyst_app` and `catalyst_risk` restrictions.

[Runtime evidence](phase-4-runtime.json): schema version 5, API restarted, authenticated market and
trade-updates streams, clean startup reconciliation, 45-second reconciliation interval. Both roles'
attempted audit UPDATEs were rejected by PostgreSQL. The post-restart 96-event chain verified with
head `9d1302269c0d2eae3d4ec15ea6cc714bd2e03651843fa4cf785781aeeebffff3`.
Subsequent observer events naturally extend that chain.

At verification, the persistent DB contained zero risk decisions, reservations, orders and fills.
`GET /api/v1/risk` returned `configured: false`, `submission_mode: DISABLED`. The API remained
`EXECUTION_LOCKED`; no risk policy or test classification was imported into its running ledger.

## Actual provider evidence

[Read-only preflight](phase-4-preflight.json), observed 2026-09-17 at 22:50:13 UTC:
active Alpaca Paper account, equity $10,000, previous-close equity $10,000, zero positions,
zero open orders, zero capital activities that day. The session was closed; next official opening
was 2026-09-18 at 09:30 ET. Broker mutations sent: **0**.

This proves connectivity and the reported empty account only. It does not prove bracket acceptance,
fill processing with a real execution, protective-leg behavior, or a real flatten.

## Outstanding acceptance gates

The user froze all three choices: TTL five seconds, broker previous-close equity captured at startup
reconciliation, and an excluded TEST- engineering candidate. No policy question remains pending.
The operator path is documented in [ENGINEERING-ACCEPTANCE.md](ENGINEERING-ACCEPTANCE.md).

During a regular session, execute the one authorized paper bracket through
risk decision → submission → actual fill → audited flatten; verify the full event chain and empty
broker/local exposure, then return to normal gated operation. Use the explicitly labeled engineering
admission, actual printed trigger and normal risk authorization. No future task or scheduler has
been created to enroll a candidate automatically.

Phase 4 must not be described as fully accepted until this actual paper loop and the final flat-state
verification have succeeded. The later runtime checkpoint records the configured gate accurately.
