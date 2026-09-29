# Gate 3 — Owner-approved credential routes

Updated 2026-09-19. The owner superseded the mandatory Keychain-access confirmation
with Railway service variables and a private local .env route. Do not request the
old confirmation or change Keychain permissions. Local testing precedes deployment.

## Local testing

The owner may place **`TYPESAFE_API_KEY`** in an ignored `.env` at the repository root,
or provide only the path of a private file through `TYPESAFE_ENV_FILE`. Do not send the
key in chat. Create/save the file privately on the Mac and set file permissions to
**0600**; it must be a regular file owned by the current user, not a symlink. The
names-only `.env.example` is safe to inspect but supplies no credential or defaults.

Loader precedence: present private .env → process-injected variable → existing
noninteractive Keychain fallback. A present file with wrong permissions, missing or
duplicate key, empty value or invalid content fails closed; it does not fall through.
No shell interpolation or command execution is performed while parsing the file.
No environment contents or key values are returned in an error.

The existing Keychain item remains service `catalyst-retest-lab.typesafe`, account
`jev-review`. Its background lookup deliberately disables desktop interaction. No
permission changes, prompt workaround or script-based approval is introduced.
The owner subsequently authorized one-time transfer of the previously shared credential
into the private local .env. That setup is complete and a real local Jev response has
been recorded and verified. No desktop action is needed for this local credential route.
See [real-call follow-up](STEP-4-REAL-JEV.md), including the test-isolation fault found
and corrected while exercising the new .env.

## Railway, after local validation and deployment resumption

Open Railway project/environment → review service → **Variables** → **New Variable**.
Use exactly **`TYPESAFE_API_KEY`** and enter the value privately in Railway. Add the
variable, optionally seal it from its menu, and apply staged changes when deploying
the review service. Do not add it to Postgres, a repository file or a build argument.
See [Railway deployment preparation](RAILWAY-REVIEW-DEPLOYMENT.md) for the full role
and configuration requirements, and [Railway variables](https://docs.railway.com/variables).

In the Railway environment, only injected process variables are read. The code does
not open .env, consult Keychain, or reuse any chat-supplied secret. Git, Railway upload
and Docker exclusions prevent local environment files being included in deployment.
Missing credentials prevent review-service startup with a fixed missing-credential
error. That validates presence, not provider acceptance.

Neither the owner-entered Railway variable nor noninteractive Railway provider access
has been demonstrated. Deployment remains deferred under the latest instruction.
No judgment touches admission or authorization; Step 4 does not create a background
model loop, admission path or trading authority.
