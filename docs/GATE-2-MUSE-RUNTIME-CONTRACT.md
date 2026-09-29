# Gate 2 — Railway app ownership and reviewer interface

Updated 2026-09-19. Owner selected **Railway**, then requested local testing before
deployment or the Muse connection. The earlier unselected-host gate is superseded.
The owner subsequently authorized the app-owned worker build. A local research worker,
durable output cursor and heartbeat are now implemented and tested; see REVIEW-WORKER-LOCAL.md.
No judgment touches admission or authorization; Railway and actual Muse connection remain deferred.

## Confirmed ownership

Muse provides scheduled jobs and shell execution. It does **not** provide a persistent
authenticated tool-invocation host for an external Python worker. No Muse-hosted
worker or unsupported remote agent API is assumed. Muse owns research and excerpts.

The application research loop is prepared to run on Railway under Railway supervision, with
Railway Postgres for durable records and runtime service variables for secrets.
The reviewer consumes durable output over authenticated HTTPS. It does not supervise
the application worker and does not receive TypeSafe or broker credentials.

## Implemented local Step 4 interface

| Caller / method | Current behavior |
| --- | --- |
| Research client: POST `/api/v1/evidence-bundles` with write token | Stores bounded source excerpts for an existing candidate; context/purpose/deadline are loaded by the server |
| Trusted adapter harness: `bound_review_args(...)`, then `JevReviewer.jev_review(...)` | Loads the exact stored packet and invokes the existing single adapter; immutable receipts and typed observations use catalyst_jev |
| Research client: POST `/api/v1/entry-intents` with write token | Stores a bundle/decision/V1 binding; status STORED_ONLY_NOT_AUTHORIZED; no interpretation of approval |
| Reviewer: GET `/api/v1/review-observations?cohort=...&after_seq=...` with read token | Reads durable cohort-specific observations in event-sequence order |
| Reviewer: GET `/api/v1/evidence-bundles/{hash}` with read token | Retrieves exact stored evidence/context for inspection |

There is no entry-intent GET, consumer, scheduler or arm logic. The web service does
not invoke TypeSafe; the separate app-owned review worker or explicitly supervised
local adapter harness does so with the private credential. In fixture mode it performs
no external request. The observation API is implemented and tested in-process, not
yet exposed on public HTTPS.

The reviewer must persist each consumed output before advancing its own cursor.
Event sequence plus receipt/decision IDs permit deduplication. This read interface
is not an acknowledgement protocol, authenticated event channel or proof of delivery.
Polling a historical record cannot make it current or confer execution permission.

## Future invocation, durability and health — forbidden Steps 5–7

After explicit authorization, the app-owned worker would claim a durable job with
lease/fencing; load approved evidence, revisions and question template; and invoke
the adapter in-process. It would preserve the original request ID across only permitted
transport retries, retain exact receipts, and commit a durable result delivery before
the review deadline. The reviewer would consume output independently on its schedule.

Required future properties include queue deduplication, expiration, revision
supersession, restart recovery, durable breaker/probe state and authenticated heartbeat.
The provisionally approved five-second heartbeat/fifteen-second deadline describes
worker health, not proof that Muse polled. Recovery must redeliver an existing result
without calling Jev for a fresh vote. Unknown provider outcome means NEEDS_REVIEW.
Model outages must not stop the independent broker/protection/calendar processes.
None of this future wiring is implemented or implicitly authorized by the storage API.

## Secrets and privileges

Railway injects `TYPESAFE_API_KEY` at launch. Production never reads local files or
Keychain. Local-only testing permits a private .env under the latest owner ruling;
see Gate 3. No secret enters request JSON, receipts, evidence, queues, logs or client
output. Do not dump process environments or include secrets in command arguments.

`catalyst_review` can invoke only the two fixed storage functions and read bounded
evidence/observation surfaces. It cannot write receipts or trading tables and cannot
read intents. The trusted adapter uses the existing restricted `catalyst_jev` role.
Migration/admin credentials stay outside both runtime services. Separate read/write
API tokens grant no broker, risk, worker-control or receipt-writing permission.

## Explicit assumptions and outstanding proof

| Assumption / requirement | Current evidence |
| --- | --- |
| Railway hosts this service and supervises its process | Owner decision and config only; deployment deferred |
| Railway Postgres supports the migrations and restricted roles | Disposable PostgreSQL tests pass; Railway verification outstanding |
| Service variables reach the correct process without disclosure | Production injection loader tested with fixtures; owner variable not entered here |
| Real TypeSafe key/model usable from the eventual service | Fresh local .env call to pinned model verified; Railway egress remains unproven |
| Reviewer can issue HTTPS requests securely from scheduled shell jobs | Runtime capability stated; actual client credentials, schedule and connectivity untested |
| Reviewer persists its cursor and deduplicates consumed records | Required caller behavior; no connected Muse client exists |
| Clocks, review deadlines and shared worker health are trustworthy | Required future runtime checks; input validation is not runtime proof |
| Queue/result delivery survives restarts without repeated votes | Not implemented; Steps 5–7 remain blocked |
| Checkpoints/backups survive database-host loss | Local chain verified; off-host retention/restore unproven |

No unattended operation is claimed. **No judgment touches admission or authorization.**
