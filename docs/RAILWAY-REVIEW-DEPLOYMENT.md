# Railway review surface — prepared, deployment deferred

Owner selected Railway on 2026-09-19, then requested local testing before deployment
or the Muse connection. **No Railway service, database or URL has been created or
deployed by this Step 4 work.** These are the later deployment instructions.

`railway.json` selects `Dockerfile.review`, starts
`python -m catalyst_lab.review_service`, checks `/health`, and configures one replica
with bounded on-failure restarts. The image uses an unprivileged OS user. Only package
source and packaging metadata are copied; environment files, local credentials,
artifacts and exports are excluded. No trading service or loop starts in this image.
Railway documents these controls in its [configuration reference](https://docs.railway.com/config-as-code/reference).

## Exact owner credential step, when deployment resumes

In Railway **Project Settings → Shared Variables**, select the deployment environment,
add **`TYPESAFE_API_KEY`**, and enter its value privately. Share it only with the review API
and the separate review worker. The services receive a reference to the shared secret;
no value belongs in this repository or chat. Use **Seal** where available and apply the
staged changes when deployment is authorized. Production does not read `.env`.
[Railway variables](https://docs.railway.com/variables).

The application also requires all of these explicit service inputs:

| Variable | Required source / value |
| --- | --- |
| `CATALYST_ENVIRONMENT` | `railway` |
| `REVIEW_DATABASE_URL` | Private Postgres connection using **catalyst_review**, never the owner/admin role |
| `REVIEW_WRITE_TOKEN` | Distinct securely generated token, at least 32 characters; evidence/intent insertion only |
| `REVIEW_READ_TOKEN` | Distinct securely generated token, at least 32 characters; reviewer observation/evidence reads only |
| `JEV_RESEARCH_POLICY_ID` | `MUSE_JEV_ACTIVE_V1` |
| `JEV_REVIEW_POLICY_JSON` | Every field in `deploy/review-policy.testing.json`, explicitly supplied as JSON |
| `TYPESAFE_API_KEY` | Owner-entered secured service variable; never a build argument |

The policy file is a **non-secret testing input template**, not an automatic runtime
fallback. Missing fields, extra fields or values outside the provisional Gate 1
agreement refuse startup. The full input object now governs the separately tested local research worker, including
shared breaker, immutable queue and heartbeat. See REVIEW-WORKER-LOCAL.md. These local
checks do not establish Railway supervisor, latency, clock health or delivery behavior.
No broker keys, risk database connection or Alpaca enablement belongs on this service.

## Postgres provisioning and role separation

When authorized to deploy, add Railway PostgreSQL. Its generated service variables
provide the private connection references. [Railway PostgreSQL](https://docs.railway.com/databases/postgresql).
Use a separate, one-off owner migration process with these runtime-only variables:
`MIGRATION_DATABASE_URL` (the Postgres owner reference), `REVIEW_DATABASE_PASSWORD`
`JEV_DATABASE_PASSWORD` and `REVIEW_OPERATOR_DATABASE_PASSWORD` (three distinct generated
secrets, each at least 32 characters).
Run `python -m catalyst_lab.review_deploy`, then remove that temporary process.
Do not place owner credentials or migration commands on the review web service.

The provisioner applies numbered migrations and sets SCRAM credentials for
`catalyst_review`, `catalyst_jev` and `catalyst_review_operator`. It sends password verifiers rather than plaintext
passwords in SQL and suppresses statement logging on its dedicated connection.
Connect the web service as `catalyst_review`, using the same private host/database
and its separately generated password. The existing `catalyst_jev` credential is
reserved for the trusted adapter process; the observation API cannot write receipts.
Local tests verify narrow grants and chain preservation. Actual Railway grants,
connectivity and restart behavior still require deployment-time checks.

No app role owns tables. Evidence/context/intent rows reject UPDATE, DELETE and
TRUNCATE and extend the existing hash chain. The storage role has no direct table
INSERT grants: only the fixed security-definer evidence/intent/report storage functions. It cannot read
entry_intents, change candidate state, reserve risk or call the broker. Existing
`catalyst_jev` receipt grants remain narrow. Owner/admin access must stay out of runtime
services; an independently retained audit checkpoint is still necessary against
privileged administrator tampering.

## Readiness and local operation

`/health` confirms configuration and restricted database access, with
`surface: RESEARCH_STORAGE_ONLY`, `provider_access: NOT_PROBED`, and false flags
for trading, intent consumption, worker loop and judgment authorization. It does
not attest to TypeSafe access or trading readiness. Railway health checks gate rollout,
not the correctness of trading behavior. [Railway health checks](https://docs.railway.com/deployments/healthchecks).

Production never reads `.env` or macOS Keychain. For local work, a private `.env`
containing `TYPESAFE_API_KEY` may be selected with `TYPESAFE_ENV_FILE`, or placed at
the repository root. It must be a regular, non-symlink file owned by the current
user with **mode 0600**. It is ignored by Git and excluded from builds. A present
invalid/empty file fails closed; it does not silently fall back. When no file is
present, an injected environment key precedes the existing no-prompt Keychain fallback.
The names-only `.env.example` contains no usable credentials or runtime defaults.

Repeat the local Muse-role storage proof with:

```sh
./run python scripts/step4_local_proof.py --provider fixture
```

Only after the owner provisions the private local credential:

```sh
./run python scripts/step4_local_proof.py --provider typesafe
```

Both runs create disposable PostgreSQL engineering records; neither imports broker
execution machinery. The fixture run makes no provider request. The TypeSafe mode
uses the pinned adapter for one engineering packet and keeps the exact receipt.
That legacy proof does not establish the complete Gate 1 worker or trading acceptance.
The newer worker proof is documented in REVIEW-WORKER-LOCAL.md.


## Separate app-owned worker service (prepared, not deployed)

Use the same image/codebase and Postgres. Point the worker service's Railway config path
to `/deploy/railway.review-worker.json`; its start command is
`python -m catalyst_lab.review_worker`. The worker does not expose a public port or use
an HTTP liveness check. Supervisor exit/restart and the database-backed worker heartbeat
must both be checked at deployment. The API's HTTP health cannot stand in for worker health.

Required worker-only settings:

| Variable | Meaning |
| --- | --- |
| `CATALYST_ENVIRONMENT` | `railway`; never load local credential files |
| `TYPESAFE_API_KEY` | Reference to the privately entered shared secret above |
| `JEV_WORKER_DATABASE_URL` | Private PostgreSQL DSN using catalyst_jev only |
| `JEV_REVIEW_POLICY_JSON` | Full explicitly supplied provisional Gate 1 object |
| `JEV_CREDENTIAL_SLOT` | Non-secret stable name shared by workers using the same credential |
| `JEV_WORKER_MAX_INFLIGHT` | Explicit bounded concurrency (1–50); calibrate with queue deadlines |
| `JEV_WORKER_POLL_SECONDS` | Explicit positive polling interval, at most the 5-second heartbeat period |

Do not give this service broker keys, risk DB credentials, owner credentials or the operator
DSN. The operator connects separately as catalyst_review_operator, which can only append
review halt/resume events through its fixed function. Its secret is not shared with Muse.
The view/API provides durable results for Muse's own cursor-persisting HTTPS poller.
Actual Muse integration, Railway URL, off-host backups and deployment acceptance are pending.
