# Package jev-breaker (plan phase 0)

Workspace: git worktree `.claude/worktrees/pkg-jev-breaker`, branch
`pkg/2026-09-26-jev-breaker`, from `work/2026-09-24-product-plan` at `80eb952`. Scope: make the
managed app recover Jev's circuit breaker, and make Jev's health visible. Owner-approved plan:
`docs/CRYPTO-AGENT-LOOP.md` section 7, phase 0 ("Jev breaker recovery"). Per the ground rules for
this package, `docs/PHASES.md` and `docs/CONTRACT-RESOLUTIONS.md` are not edited directly; the
text below is ready to paste into each, in their own style, when the owner or coordinator merges
this package.

## PHASES.md entry (not yet applied there)

```markdown
## 2026-09-26 — Managed-path Jev breaker recovery and health visibility (FIXTURE EVIDENCE ONLY)

- Defect (found by reading code, no incident): migration 011's breaker
  (`lab.review_attempt_permit`, the `lab.complete_review_attempt` trigger) opens after three
  consecutive provider failures and closes only after two successful synthetic health probes.
  Probes are sent only by `ReviewWorker.tick` (`review_worker.py:164-165`, via
  `Gate1Runtime.probe_due`/`probe`). `managed_runtime.build_runtime_from_env` used
  `worker.reviewer`, `worker.store` and `worker.heartbeat` but never `worker.runtime`, so the
  managed app never probed. Once three provider calls failed, selection and position reviews
  stopped permanently and silently: `last_research_tick` kept advancing (the research loop was
  running fine; every review inside it was refused `CIRCUIT_OPEN`), and neither
  `ManagedRuntime.status()` nor the watchdog alarms showed the breaker at all. Recovery needed a
  human to notice and start (or restart) a standalone `review_worker` process against the same
  scope.
- Fix: `ManagedRuntime._research_loop` now runs the same probe step inside its "research"
  periodic tick, before that tick's shortlist work (`ManagedRuntime.probe_once`, gated on
  `research_healthy`): if `gate1.probe_due(now)`, `await gate1.probe(research.reviewer, now)`.
  `gate1` is the worker's own `Gate1Runtime`, injected into `ManagedRuntime.__init__` explicitly
  (`gate1=worker.runtime` in `build_runtime_from_env`) rather than reached through
  `research.reviewer.runtime`; a runtime built without one (most engineering fixtures) never
  probes and behaves exactly as before. The permit semantics are unchanged: a probe still needs
  this worker's own `lab.review_worker_status` row to read RUNNING, which the existing
  `heartbeat_once` loop maintains via the same `reviewer_heartbeat` the probe is gated on.
- Visibility: `ManagedRuntime.status()` gains `jev_breaker` (`state`, `epoch`, `blocked_until`)
  and `jev_calls_today` (`attempts`: `lab.jev_requests` rows, one per `jev_review` call including
  health probes; `receipts`: `lab.jev_receipts` rows, which can exceed attempts because of
  retries; both since local New York midnight, across every credential scope in the database --
  only one engine is ever live against it in practice; visibility only, the owner set no daily
  cap). `Gate1Runtime.calls_today` adds the query, reusing the worker's existing `catalyst_jev`
  role (already granted `SELECT` on both tables by migration 008); no new grant, no migration.
  Either field independently degrades to `null` on its own read failure, so a database hiccup on
  this secondary read cannot take down the rest of the status payload -- including the breaker
  fields themselves, the exact failure mode this package exists to prevent. Both fields are
  plumbed through `managed_app.create_application.status()`'s mapping and through
  `managed_service.STATUS_FIELDS`, an allowlist that filters the real HTTP response and would
  otherwise silently strip them.
- Alarm: `managed_ops.status_alarms` raises `JEV_BREAKER_OPEN` (codes only) whenever the status's
  `jev_breaker.state` is not `CLOSED` (covers both `OPEN` and `HALF_OPEN`, since ordinary reviews
  are refused in both), reaching `notify.py` through the existing generic alarm-forwarding
  mechanism unchanged. Absent when `jev_breaker` is missing or `null` (an older release, or a
  runtime with no `gate1` configured), so this cannot become a false positive on a status shape
  that predates the field.
- Evidence: `tests/test_managed_jev_breaker.py` (new, disposable per-test PostgreSQL database and
  a scripted mock Jev transport, exactly like `tests/test_review_worker.py`'s own breaker tests
  but exercised through `ManagedRuntime.probe_once`/`status()` instead of `ReviewWorker.tick`) --
  an OPEN breaker gets probed and closes after the required two successes, with an ordinary
  (non-probe) review then resuming `RECORDED`; no probe while `CLOSED`; no probe while
  `research_healthy` is `False` or `gate1` is `None`; the status fields' exact shape and today's
  attempt/receipt counts, including the New York midnight cutoff (proved by shifting the query
  time forward 24 hours rather than by fabricating backdated rows); `status()` degrading the two
  fields alone when `gate1.store.connect` is broken. One added test each in
  `tests/test_managed_ops.py` (the alarm, present for `OPEN`/`HALF_OPEN`, absent once `CLOSED` or
  missing) and `tests/test_managed_app.py` (the real FastAPI `/api/v1/lab/status` round trip,
  proving `STATUS_FIELDS` does not strip the two keys). Three existing tests
  (`tests/test_managed_runtime.py`, `tests/test_selection_b1.py` -- shared by
  `tests/test_selection_b2.py`, `tests/test_managed_engineering.py`) construct a bare
  `SimpleNamespace` stand-in for `ReviewWorker` when exercising `build_runtime_from_env`;
  each now also carries a `runtime=` attribute so the new `worker.runtime` read does not raise,
  and one asserts the wiring by identity (`run.gate1 is fake_review.runtime`). Targeted runs
  covering every touched file plus every directly related one (`test_managed_jev_breaker.py`,
  `test_managed_app.py`, `test_managed_ops.py`, `test_managed_runtime.py`,
  `test_managed_engineering.py`, `test_selection_b1.py`, `test_selection_b2.py`,
  `test_review_worker.py`, `test_managed_service.py`) passed: 943 tests, 0 failures. Ruff is
  clean. Several packages were building in parallel on the same disk during this work and the
  coordinator asked each package to run targeted tests only and report in; the coordinator runs
  the full suite centrally afterward -- see its own record for that number, not this file.
- Unchanged: no migration, no schema change, no new database role or grant. `docs/V4.2-BUILD.md`
  and the frozen V1 trigger are untouched; Phase 4's TTL, previous-close capture and TEST-
  exclusion rules are untouched; the breaker's own policy values (`breaker_failure_threshold: 3`,
  `breaker_recovery_successes: 2`, `breaker_cooldown_seconds: 30`,
  `recovery_probe_spacing_seconds: 1`) are unchanged -- this package makes the managed app run
  the existing mechanism, not a new one. The standalone `review_worker` process remains a second,
  independent way to probe and close the same breaker; running one alongside the managed app is
  unaffected and still works exactly as before.
```

## CONTRACT-RESOLUTIONS.md entry (not yet applied there)

```markdown
## 2026-09-26 — Jev breaker visibility: exact field definitions (package jev-breaker)

Two status fields are new; their exact shape, since neither was specified numerically by the
owner, is recorded here so later work (dashboards, alarms, analytics) can rely on it without
re-deriving it from code.

`jev_breaker` reports exactly three of the breaker's seven durable fields: `state` (`CLOSED`,
`OPEN` or `HALF_OPEN`), `epoch` (increments on every `OPEN` transition; distinguishes an attempt
that was in flight when the circuit opened from the next generation) and `blocked_until` (the
cooldown deadline, `null` when `CLOSED`). `failures`, `successes`, `probe_id`, `probe_until` and
`next_probe_at` are internal to the recovery mechanics and are not exposed; add them to
`ManagedRuntime._jev_health` if a future package needs them, rather than reaching for
`gate1.state()` directly from a new call site.

`jev_calls_today` counts two things since local New York midnight, recomputed on every
`status()` call (not cached, not batched with the heartbeat's own dedup): `attempts` is the
count of `lab.jev_requests` rows (one per `jev_review()` call, whether a real selection/position
review or a synthetic health probe); `receipts` is the count of `lab.jev_receipts` rows (one per
attempt inside a call's retry loop, so it can exceed `attempts`). The count is global to the
database -- every credential scope and record purpose, not filtered to this runtime's own
`scope_id` -- because under the 2026-09-24 ruling only one engine is ever live against a given
account database, so a per-scope filter would add a join without changing the number in
practice. A future package that runs more than one live scope against the same database should
revisit this before relying on the count as per-engine.

The watchdog alarm `JEV_BREAKER_OPEN` fires whenever `jev_breaker.state` is present and not
`CLOSED` -- both `OPEN` and `HALF_OPEN` are alarmed, because `lab.review_attempt_permit` refuses
every non-probe attempt in either state (`CIRCUIT_OPEN`); only `CLOSED` means ordinary reviews go
through. A missing or `null` `jev_breaker` (a runtime with no `gate1` configured, or an older
release's status shape) never raises the alarm; it is not evidence that the breaker is healthy,
only that this runtime does not report it, and `RESEARCH_UNHEALTHY` remains the correct signal
for "no reviews are happening" in that case.

Not a rule change to Phase 4 or the frozen breaker policy: `breaker_failure_threshold` (3),
`breaker_recovery_successes` (2), `breaker_cooldown_seconds` (30) and
`recovery_probe_spacing_seconds` (1) are the same values `review_config.APPROVED_GATE1` already
pinned; this package only makes the managed research loop exercise the existing mechanism, on
the existing schedule (`research_poll_seconds`, the same interval the loop already polls active
research cycles on -- there is no separate, independently configurable probe interval).
```

## Deviations and open items

- **`attempts`/`receipts` definition.** `lab.review_attempt_permits` also uses the word
  "attempt" and would seem the more literal match, but it has no `created_at` (only
  `expires_at`, a TTL, and `event_seq`, which needs a join to `lab.trade_events` for a
  timestamp), and a permit row is never inserted for an attempt the breaker refused before
  issuing one (`CIRCUIT_OPEN`, `WORKER_HEARTBEAT_DOWN`, `OPERATOR_HALTED`, ...). Counting
  `lab.jev_requests`/`lab.jev_receipts` instead (both have direct timestamp columns) is simpler
  to query and, since a receipt is written even for a refused attempt, gives more useful
  visibility into total call volume against Jev -- the owner's stated reason for wanting the
  count at all. If the owner instead wants "how many attempts actually got a permit" specifically,
  that is a different, addable query (join through `lab.trade_events`), not a change to this one.
- **Global, not per-scope, count.** Recorded above as a CONTRACT-RESOLUTIONS choice rather than
  hidden in code, since it is the kind of thing that quietly stops being true later.
- **No new alarm for "breaker status unavailable."** When `gate1.state()` itself fails,
  `jev_breaker` is `null` and no alarm fires for that specifically. In practice a failure there
  shares its cause (the review database being unreachable) with `reviewer_heartbeat()` failing
  too, which already flips `research_healthy` to `False` and raises the existing
  `RESEARCH_UNHEALTHY` alarm on the same tick -- so this was not left as a silent gap, just not
  duplicated with a second code for the same underlying cause. Worth revisiting only if a future
  change makes the two reads independent (for example, moving `calls_today` off the worker's own
  `catalyst_jev` connection onto a separate role).
- **Probe cadence tracks `research_poll_seconds`.** There is no separate
  `MANAGED_JEV_PROBE_SECONDS` or similar; the probe runs on the same cadence as the research
  pass it now precedes, mirroring how the standalone worker's own `poll_seconds` already governs
  both job-claiming and probing together. Add a dedicated setting only if the owner wants the
  managed app to probe on a different schedule than it polls for research cycles.
- **Alarm severity/paging is unchanged.** `JEV_BREAKER_OPEN` is forwarded exactly like every
  other code already in `status_alarms` -- no special-cased urgency, reminder cadence, or local
  notification behavior. If a tripped breaker should page differently than, say,
  `RESEARCH_TICK_STALE`, that is a `notify.py` policy change outside this package's scope.
- **Not tested: the real TypeSafe provider.** Every test here uses a scripted mock transport and
  a disposable PostgreSQL database, per the ground rules for this package. The mechanism being
  fixed (Gate 1's breaker and its recovery probe) is itself frozen, existing, already-tested code
  reused from `review_runtime.py`/`review_worker.py`; this package changes only where it is
  called from and what is reported about it.
- **`docs/API-CONTRACT.md`'s "Runtime metadata" section (around line 612) enumerates
  `GET /api/v1/lab/status`'s fields and is not on this package's edit list, but it is already
  stale independent of this change -- it lists neither `operator_flatten` nor `release_commit`,
  both already in `managed_service.STATUS_FIELDS` before this package touched it -- and is now
  also missing `jev_breaker` and `jev_calls_today`. Left alone here since it was not named in
  the ground rules and the staleness predates this package; worth a separate pass.
- **Untouched by this package:** the lease-loss/restart, refused-exit-retry and halt-alarm work
  in `managed_runtime.py`/`managed_ops.py` (unattended-safety), fees/net measurement (fees-net-r)
  and the research context API/report V3 (research-v3) all run in parallel on the same base
  commit; this package's edits to shared files (`ManagedRuntime.status()`, `status_alarms`,
  `managed_app.create_application.status()`, `managed_service.STATUS_FIELDS`) are additive and
  placed to avoid overlapping the hunks those packages described adding, but a small textual
  merge there is still expected and was flagged to unattended-safety directly.
