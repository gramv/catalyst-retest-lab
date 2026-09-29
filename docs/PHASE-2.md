# Phase 2 — Watch and trigger

The frozen mechanical definition is appended to system-build-plan.md. Muse selects the research
hypothesis; the observer does not invent a breakout/reclaim sequence.

## Implemented boundary

The optional runtime reads the paper account and exchange calendar, and subscribes to explicit
IEX or SIP trade, quote and one-minute bar streams. It contains GET-only REST methods. There is
no broker submission, replacement, cancellation, liquidation, or live-trading path.

A validated candidate starts WATCHING only in its regular session after the observer receives
acknowledgement of trade/quote/bar subscriptions. Current printed trades can establish a touch;
quotes and bars cannot. A stop print invalidates before confirmation. Once touched, a healthy
fresh quote meeting spread/ask bounds confirms the trigger; an otherwise eligible ask above M
invalidates it. The audit records the future bracket intent: buy limit M, stop S, target P, DAY.
This phase stops at TRIGGER_CONFIRMED. RISK_CHECK and orders require Phases 3 and 4 together.

Snapshots carry provider, feed, original nanosecond timestamp, normalized timestamp, type and
observation hash. Watch checkpoints and snapshots are append-only; each is tied to the global
hash-chained event log. Deduplication, observation persistence and state transitions share one
transaction. A failed transaction can be retried without a partial trigger. Quote ordering uses
provider nanoseconds, while exact observation deduplication includes trade IDs.

The observer checks expiration once per second, including disconnect periods. Session close
comes from the exchange calendar; no session is synthesized for holidays. If the calendar network
call is unavailable, the observer can use the calendar evidence already stored by validation. A started watch retains its calendar and policy so restart recovery needs neither a
successful network call nor a policy change. An unstarted candidate may expire directly from
VALIDATED, rather than falsely claiming it was watched.

## Operational data-quality policy

- `TRIGGER_MAX_SPREAD_BPS`: default 10; ratio multiplied by 10,000 before comparison.
- Quote maximum age: frozen at 5 seconds, inclusive. Future timestamps are not fresh.
- `FEED_FAILURE_TOLERANCE_SECONDS`: builder default 5. An outage lasting at least this
  interval invalidates the watch; this configured value is recorded at watch start.
- Printed trades must be post-watch, non-future and received within 5 seconds. This is the
  current-data health boundary, preventing delayed/replayed prints from creating a trigger.
- Missing, crossed or stale quotes are unhealthy. Wide but otherwise valid quotes simply
  defer confirmation. A later valid quote cannot erase an outage that already exceeded tolerance.
- On restart, the gap starts at the last durable check. A short gap may recover within tolerance;
  a longer gap invalidates. Touch history survives. A worker lease prevents duplicate observers.
- An active watch cannot switch feeds. IEX and SIP are never blended or silently substituted.
- Trade correction/cancellation notifications are recorded as new system events. They never edit
  an earlier snapshot or confirmation; execution-phase handling remains a gate before orders.

## Running the read-only observer

Start the local database with `./run catalyst-lab dev-init`. Provision secrets via environment
variables in the launching process; `.env` files are not automatically loaded.

Required when enabling the observer: `ALPACA_READ_ONLY_ENABLED=1`, `APCA_API_KEY_ID`,
`APCA_API_SECRET_KEY`. The fixed paper and data URLs are shown in `.env.example`.
`ALPACA_DATA_FEED=iex` is the default. `MARKET_DATA_SYMBOLS=AAPL` optionally subscribes a
symbol for diagnostics without creating a candidate or trade. No such diagnostic symbol is
subscribed unless configured. Use `./run catalyst-lab serve` after provisioning environment.

Public `/health` separates account REST connectivity, WebSocket authentication, subscription
count, fresh quote count, session status and execution disabled. Authenticated
`GET /api/v1/market-data` adds sanitized account figures and quote provenance. A connection
outside market hours does not imply fresh prices. IEX coverage is labeled `IEX_ONLY`.

Candidate admission remains fail-closed. The live observer does not fabricate ADV, classification,
reconciliation or risk evidence. An otherwise sane candidate receives DATA_FEED_FAILURE while
transport is unavailable, or STARTUP_RECONCILIATION_REQUIRED when transport is connected but
complete validation evidence remains unavailable. Tests inject explicitly labeled evidence to
exercise validation through watch and trigger. Do not submit real daily candidates until the
full evidence provider and reconciliation gate are implemented: rejected attempts consume the day.

REST refresh runs in a separate thread every 45 seconds so network waits do not block stream
handling. This is an account/calendar refresh, **not broker reconciliation**. Stream disconnects
and provider failures are audited with sanitized codes; credential frames are never logged.
Both HTTP and WebSocket redirects are disabled. No supplied endpoint can redirect authentication.

## Remaining work before execution

Final risk/reservations and durable daily halts; sector/theme and liquidity evidence; startup and
continuous broker reconciliation; bracket outbox and stable client order IDs; protective-leg and
partial-fill recovery; trade_updates; calendar flattening; trade correction handling. The formula
based on T−S must also be reconciled with worst-case fills at M before enabling any bracket.
