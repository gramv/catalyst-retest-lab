# Frozen Phase 4 rulings — implementation evidence

All three choices are recorded in [CONTRACT-RESOLUTIONS.md](CONTRACT-RESOLUTIONS.md) and implemented.
The actual Alpaca Paper bracket/fill/flatten acceptance remains **NOT RUN**; the market is closed.

## Local verification

Full suite: **276 passed**, 28.15 seconds. Ruff lint and formatting passed. The only warnings are
two upstream Starlette test-client deprecations. Tests use private PostgreSQL databases and fake
broker HTTP; an autouse guard prohibits external TCP connections.

New acceptance coverage proves:

- Exactly five seconds between database-stamped decision and expiry; conflicting TTL/baseline
  policies fail. The final transport also rejects an entry if its supporting quote has become stale.
- Startup reconciliation captures previous-close equity with an audit reference. A restart with
  different reported equity does not change that session's baseline; the next session captures a
  new value. Risk checks refuse a missing baseline rather than inventing one mid-day.
- An enrolled TEST- candidate travels through VALIDATED, an actual simulated printed-trade trigger,
  the ordinary risk authorizer, fake broker bracket, entry fill and controlled market flatten.
  Every step is audited; one bracket and one closing market order are recorded.
- Its candidate, order, fills and a reserved trade projection are excluded from strategy reporting.
  The reporting role cannot read base tables. Generated purpose cannot be overridden or relabeled;
  even a malformed rejected TEST- signal stays excluded. The full audit chain remains verifiable.
- Engineering exposure reserves risk normally. A test attempt does not consume the strategy's
  ticker/day attempt. An unenrolled TEST- signal cannot submit, and a completed/submitted controlled
  test cannot automatically create another bracket. Unfilled tests time out, cancel and report
  NOT_ENTERED instead of success.
- Both app and risk roles are denied UPDATE/DELETE/TRUNCATE on the new engineering run/result
  tables. The original Phase 3 no-authorization submission-denial tests remain green.

No strategy reward/risk, trigger, sizing, exposure or daily-halt rule was changed by engineering
admission. It is explicitly a non-strategy plumbing path and records no invented liquidity evidence.
No performance analytics or dashboard has been implemented.

## Runtime verification

Before migration 006, the 178-event schema 5 audit chain verified and a private PostgreSQL backup
was saved at `~/.local/share/catalyst-retest-lab/runtime/backups/pre-frozen-rulings-20260918T000336Z`.
The previous audit head was
`c9527a745bea5e603fc9de267a830b8e76156c40a511404cfb1eb696451a0df0`.

[Runtime evidence](frozen-rulings-runtime.json), observed 2026-09-18 at 00:05:22 UTC (September 17 ET):

- Schema 6, `phase: RISK_GATED`, `submission_mode: RISK_DECISION_REQUIRED`, TTL 5 seconds.
- Startup reconciliation clean; both broker trade updates and IEX market-data streams authenticated.
- September 17 session baseline $10,000 from `ALPACA_LAST_EQUITY`, recorded at event 183 with a
  reference to startup reconciliation event 182. The worker's later polls preserve this row.
- Zero broker positions/orders and zero local orders, fills, risk decisions, engineering runs or
  active reservations. A real-calendar closed-session enrollment attempt returned
  `REGULAR_SESSION_REQUIRED` before any candidate insert or broker mutation.
- The complete 183-event chain verified with head
  `8d0d161202934b4c18d3ffb361179750a80cbdbe6d340fe56fbfbee87d39bb43`.

`trading_enabled: true` now accurately describes a configured risk runtime, not a bypass: every
mutating request still requires its exact, committed, unexpired, unused decision. Credentials
remain solely in the process environment. No engineering candidate or broker order is queued.

The authorized controlled paper run still requires a regular session. The next opening reported by
Alpaca is 2026-09-18 at 09:30 ET. This work does not create a future task or scheduler to perform it.
