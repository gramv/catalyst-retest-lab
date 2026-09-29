# Alpaca Paper read-only connectivity check

Checked on 2026-09-17, approximately 17:19 America/New_York, using credentials supplied by the user
for this check. Credentials were loaded into the check process environment, never printed, and not
saved in this repository or a new credential file. Only GET requests were sent. Redirects were disabled.

| Check | Observed result |
| --- | --- |
| Paper account | HTTP 200; ACTIVE; USD |
| Equity / cash | $10,000 / $10,000 |
| Buying power | $40,000; not used as equity or a risk budget |
| Account/trading/user suspension flags | All false |
| Open positions | HTTP 200; zero |
| Open orders | HTTP 200; zero |
| AAPL asset eligibility | HTTP 200; active, tradable, us_equity |
| Exchange clock | HTTP 200 on retry; regular session closed |
| Next regular session | 2026-09-18, 09:30–16:00 America/New_York |
| Exchange calendar | HTTP 200 on retry; seven-day range retrieved |
| IEX latest AAPL quote | HTTP 200; timestamp 2026-09-17T20:00:00.00283137Z |
| SIP latest AAPL quote | HTTP 403; this check did not establish SIP access |

The first clock/calendar attempts encountered connection/response errors; explicit retries succeeded.
At approximately 17:19 ET, the IEX quote was about 79 minutes old, with bid 318.06 and ask 351.72.
Retrieving it proves endpoint access, not current quote freshness or executable market quality.
It must not be admitted as a fresh trading quote. Streaming continuity and in-session quote quality
have not been tested.

The calendar implies a 15:55 ET flatten deadline for the next regular session under the frozen
close-minus-five-minutes rule. No time-exit worker is implemented or scheduled yet.

## Boundaries and next work

- No order was submitted, modified, canceled or closed. Bracket acceptance and fill behavior are untested.
- This check did not wire credentials into the running API or complete startup reconciliation.
  The Phase 1 server still correctly reports broker_connected=false and trading_enabled=false.
- The frozen risk policy uses actual equity, not buying power. At the observed $10,000 equity,
  its nominal risk budget would be $100 per trade and $200 total planned open risk. The daily halt
  additionally requires the session's captured start-of-day equity. The account was not reset to $5,000.
- Next: Phase 2 market-data/calendar providers, quote freshness and spread checks, observer and the
  exact first-retest definition. That revised trigger section has not yet been supplied.
- Follow with broker streaming/reconciliation, brackets and the final risk gate before enabling execution;
  then measurements, the public HTTPS dashboard and secure Muse integration.

Primary endpoint references:
[account](https://docs.alpaca.markets/us/v1.1/reference/getaccount-1),
[calendar](https://docs.alpaca.markets/us/v1.1/reference/getcalendar-1),
[quote feeds](https://docs.alpaca.markets/us/reference/stocklatestquotes-1).
