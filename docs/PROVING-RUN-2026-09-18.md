# Controlled Alpaca Paper proving run — September 18, 2026

**PAPER TRADING — SIMULATED. Not real money.**

**Outcome: CLOSED, engineering result FAILED_CLOSED.** One real paper bracket was accepted after
a real printed-trade trigger and an atomic risk decision. Five of thirteen shares filled. The
protective-safety guard found the bracket legs held during the partial fill, canceled the entry
remainder and protective legs, and flattened the five shares. Broker and local exposure are zero.
This proves the real partial-fill safety path. It is not a nominal CONTROLLED_ACCEPTANCE pass.
No second attempt was made or queued; the immutable result was not relabeled.

## Approved run and preflight

- Signal: `TEST-SPY-2026-09-18`; database candidate UUID `e3544411-98a8-42f0-b77d-04a69588ba19`.
- Long SPY, T=$761.00, M=$761.50, S=$759.00, P=$765.00, bracket DAY TIF.
- Operator path: `ENGINEERING_PLUMBING_ADMISSION_ONLY`. Immutable purpose `ENGINEERING_TEST`.
- The user approved the existing lifecycle: maximum 300-second observation/entry deadline,
  immediate controlled flatten after a fill, with protective safety taking precedence.
  Enrollment was 10:14:18 ET; stored deadline 10:19:18.317747 ET. The requested DAY expiry was
  capped by that engineering deadline. This run does not establish a real 15:55 calendar exit.
- Preflight verified the complete 1,321-event chain and preserved the earlier retained checkpoint.
  Broker/local positions, working orders, reservations, pending exits and halts were zero.
- Broker current equity and previous-close equity were $10,000. The persisted September 18 baseline
  remains $10,000 at event 497, referencing session reconciliation 496; it was not recalculated.
- Exactly one approved server classification was imported at event 1322: SPY, sector BROAD_MARKET,
  theme US_LARGE_CAP_INDEX. `instrument_class: ETF` is preserved in the immutable source provenance;
  the existing classification schema has sector/theme columns and no separate instrument_class column.

## Real trigger and authorization

At **10:14:21.566553 ET**, an Alpaca IEX trade printed **$759.46**, above S and at or below T.
The trigger evaluation at 10:14:21.617995 ET used bid $759.25 / ask $759.48, spread
**3.0288 bps**, quote age **0.132064 seconds**, with healthy feed and no active feed failure.
The ask was below M. This was an actual printed trade, not a synthetic observation or quote touch.
IEX-only provenance remains visible in the audit; this is not consolidated SIP evidence.

The risk engine independently read $10,000 current broker equity and computed:

- Risk sizing: floor($100 / ($761.50 − $759.00)) = 40 shares.
- No-leverage cap: floor($10,000 / $761.50) = **13 shares**, maximum entry notional **$9,899.50**.
- Planned order risk **$32.50**; full 1% budget reservation **$100**.
- Correlation passed: no existing sector/theme exposure, approved mapping as above, maximum one
  per sector/theme. Combined reserved risk moved $0 → $100, below the $200 cap.
- Entry decision `356b2696-0872-4ad1-84a2-998c08caa415` stamped at 10:14:22.516264 ET,
  expires at 10:14:27.516264 ET, consumed at 10:14:22.535193 ET.
- All five broker mutations—entry, three cancellations, and market flatten—had committed,
  exact-request, single-use risk authorizations consumed within their five-second lifetimes.

Bracket parent: `a7a6d027-8185-4666-84e7-8300203b9a80`, limit buy 13 at $761.50,
stop $759.00, take-profit $765.00, DAY. Exactly one entry bracket was submitted.

## Fill and protective exit

| Broker timestamp (ET) | Action | Shares | Price |
| --- | --- | ---: | ---: |
| 10:14:22.966790 | Partial entry fill | 5 | $759.46 |
| 10:14:25.278542 | Market safety exit fill | 5 | $759.43 |

At event 1403, the system requested `PROTECTION_FAILURE`: the parent was partially filled and
both sell legs were `held`, so the existing safety rule did not treat them as active protection.
It canceled the unfilled eight-share remainder and both held legs before submitting the five-share
market close `79f77b44-dc04-47b5-a1a6-7fd5b87fc358`. No leg rejection is alleged; their observed
status was held. The safety path ran before the ordinary engineering flatten could own the exit.

The candidate closed at event 1429. The reservation released at 1430, broker-flat completion was
recorded at 1431, and the engineering result **FAILED_CLOSED** was recorded at 1432. The implemented
acceptance condition requires a CONTROLLED_ACCEPTANCE exit; this exit was PROTECTION_FAILURE.
The safety response succeeded, but the nominal acceptance condition did not.

## Test-only arithmetic

Gross fill P&L = 5 × ($759.43 − $759.46) = **−$0.15**. The observed flat broker equity is
**$9,999.85**. These report calculations do not model fees or claim net-performance analytics.

Using the approved full-order planned risk of **$32.50**, gross test R = −$0.15 / $32.50 =
**−0.004615 R**. Only 5 of 13 shares filled. For a distinct fill-based reference, the actual filled
entry-to-stop risk was 5 × ($759.46 − $759.00) = $2.30, giving **−0.065217 R** on that denominator.
These denominators are explicit and are not interchangeable.

No strategy result was created: candidate/order/fill/trade strategy reporting views all contain
zero rows for this test. Phase 5 analytics remain unimplemented; API net_r remains unset. The
arithmetic above is an audit-derived engineering report, excluded from strategy/dashboard results.

## Observed state transitions

Timestamps below are database append times; broker fill times are separately listed above.
The entry never reached fully FILLED: partial fill followed by canceled remainder led to OPEN.

| Event | Timestamp ET | State | Reason |
| --- | --- | --- | --- |
| 1324 | 10:14:18.824455 | RECEIVED | — |
| 1325 | 10:14:18.825741 | VALIDATING | — |
| 1328 | 10:14:18.829479 | VALIDATED | — |
| 1330 | 10:14:19.680694 | WATCHING | — |
| 1388 | 10:14:21.649564 | TRIGGER_CONFIRMED | CATALYST_RETEST_V1 |
| 1389 | 10:14:22.505779 | RISK_CHECK | — |
| 1393 | 10:14:22.523070 | ORDER_SUBMITTED | RISK_AUTHORIZED_DISPATCH_PENDING |
| 1402 | 10:14:23.014347 | PARTIALLY_FILLED | BROKER_FILL |
| 1422 | 10:14:25.165735 | OPEN | PARTIAL_ENTRY_REMAINDER_TERMINATED |
| 1428 | 10:14:28.341519 | EMERGENCY_EXIT | BROKER_EXIT_FILL |
| 1429 | 10:14:28.341927 | CLOSED | EMERGENCY_EXIT |

## Final exposure and reconciliation

At **10:16:48.838456 ET**, independent GET-only Alpaca Paper queries confirmed zero open
orders and zero positions. Local position quantity, active reservations and pending exits were zero.
No execution halt or daily halt was present. The observer remains in RISK_DECISION_REQUIRED mode;
there is no queued retry and the existing one-test guard prevents another engineering enrollment.

Formatted extracts of actual stored reconciliation rows, not invented process stdout:

```text
10:13:43.743413 ET seq=1321 clean=true discrepancies=0 broker_positions=0 broker_orders=0
10:14:28.850842 ET seq=1433 clean=true discrepancies=0 broker_positions=0 broker_orders=0
10:15:13.747910 ET seq=1434 clean=true discrepancies=0 broker_positions=0 broker_orders=0
10:15:58.816478 ET seq=1435 clean=true discrepancies=0 broker_positions=0 broker_orders=0
10:16:43.814628 ET seq=1436 clean=true discrepancies=0 broker_positions=0 broker_orders=0
```

## Retained evidence

- [Full global audit chain](~/.local/share/catalyst-retest-lab/runtime/proving-run-20260918T141349Z/audit.jsonl)
- [Verified manifest](~/.local/share/catalyst-retest-lab/runtime/proving-run-20260918T141349Z/audit.manifest.json)
- [Complete candidate event slice](~/.local/share/catalyst-retest-lab/runtime/proving-run-20260918T141349Z/candidate-events.json)
- [Postflight fills, decisions, claims, broker receipts and reconciliation](~/.local/share/catalyst-retest-lab/runtime/proving-run-20260918T141349Z/postflight.json)
- [Preflight snapshot](~/.local/share/catalyst-retest-lab/runtime/proving-run-20260918T141349Z/preflight.json)
- [Approved and stored classification](~/.local/share/catalyst-retest-lab/runtime/proving-run-20260918T141349Z/classification.json)

The complete export verifies **1436 events**, preserving the preflight checkpoint.
Candidate event slices alone are not a standalone full-chain verification.

Snapshot hash head:

```text
97983ec514fb2b625c7ce30ad54a2ac5d3550cbd010f27cdedab61525cd76184
```

The observer can append later reconciliation events; this retained export/head is the report checkpoint.
No execution code, lifecycle, risk rule, state history or classification beyond the one approved SPY
row was changed. This run is stopped and reported; any further acceptance work is a separate action.
