"""Private, supervised paper-lab surface; no raw order or policy mutation endpoint."""

import asyncio
import hmac
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from catalyst_lab.agent_identity import (
    Principal,
    agent_fields,
    legacy_principal,
    validated_agent_tokens,
)
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.learning_intake import (
    LEARNING_EVENT_KINDS,
    OUTLOOK_BODY_LIMIT,
    POST_MORTEM_BODY_LIMIT,
)
from catalyst_lab.learning_intake import declared_agent as learning_agent
from catalyst_lab.managed_analytics import (
    managed_daily_rollups,
    managed_result_aggregates,
    paginated_managed_results,
)
from catalyst_lab.managed_engineering import ENGINEERING_PURPOSE, is_engineering
from catalyst_lab.managed_funnel import execution_quality, research_funnel
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.managed_store import COHORT
from catalyst_lab.muse_reports import ResearchReportRejected
from catalyst_lab.pick_outcomes import cycle_picks, pick_outcome_aggregates, pick_outcome_page
from catalyst_lab.repository import json_safe
from catalyst_lab.research_cycle import ResearchThesis
from catalyst_lab.research_evidence import canonical_sources
from catalyst_lab.research_report_v3 import ResearchCapabilityUnavailable, declared_agent
from catalyst_lab.unchanged_plan import maintenance_replay_aggregates, maintenance_replay_page

STATE_FIELDS = {
    "state", "revision", "qty", "stop", "target", "fill_price", "entry_fill_price", "exit_reason",
    "exit_requested", "revoked", "revocation_reason", "admitted_at", "closed_at",
    "last_error", "protection_state", "amendment_expires_at",
    "lifecycle_id", "hard_exit_at", "crypto_day_policy",
    "crypto_entry_deadline", "crypto_flat_deadline",
    # Set at admission (migration 016): account-risk policy and randomized control arm.
    "risk_policy_id", "arm", "capacity_deferred_until", "capacity_deferred_reason",
    # Set at admission of a report-V3 crypto pick: CRYPTO_ALPACA_TRIGGER_V1 (package
    # crypto-trigger) decides its trigger, and CRYPTO_GAP_RESUME_V1 (package gap-resume) holds
    # it for a bar check on a market gap instead of revoking it.
    "trigger_version", "gap_resume_version",
    # STALE_PRINT_ABOVE_TRIGGER_V1 (owner approval 2026-09-28): a late print above the entry
    # trigger is consumed instead of revoking the setup.
    "stale_print_version",
    # Set at admission of a report-V3 pick (SYSTEM_CHECK_V1): its entry type and the check's
    # evidence (``result`` PASSED, ``code``, the checks and the live price it used).
    "entry_type", "system_check",
    # Set at admission of a report-V3 crypto pick (CRYPTO_24H_HOLD_V1, migration 022 package):
    # the holding policy whose 24-hour hard exit is ``hard_exit_at``; under the window versions
    # (package review-window) also the window it recorded, in seconds.
    "holding_policy", "holding_window_seconds",
    # Set at admission of a report-V3 crypto pick in the JEV_MANAGED arm (package maintenance):
    # CRYPTO_MAINTENANCE_V1 and CRYPTO_PARTIAL_ENTRY_V1, a raised stop still being replaced at
    # the broker, and a partly filled entry's retention and cancel reason.
    "maintenance_policy", "partial_entry_policy", "stop_replace",
    "partial_entry_retained_at", "partial_entry_cancel",
    # Set at open under CRYPTO_24H_REVIEW_V1 (package day-review) and the other review versions:
    # T of the next review and the number of continues so far.
    "day_review_at", "continuations",
}
STATUS_FIELDS = {
    "worker_state", "last_cycle_at", "last_reconciliation_at", "trade_stream_connected",
    "market_feed_connected", "management_review_enabled", "error_code", "mode", "entry_ready",
    "research_healthy", "last_protection_tick",
    "market_streams", "market_data_authority", "executor_ownership", "account_safety_healthy",
    "required_market_streams", "schema_version", "code_version", "configuration_hash",
    "release_commit", "operator_flatten", "jev_breaker", "jev_calls_today",
    "execution_halts", "exit_refusal_alarms", "trade_maintenance", "day_reviews",
    "gap_resume", "jev_budget",
}


def scan_readback(body):
    """Keep scanner exclusions distinct from subsequent model decisions."""
    scan = body.get("scan", {})
    decisions = scan.get("decisions", [])
    return {
        "screened": len(decisions),
        "technical_exclusions": sum(d.get("disposition") == "REJECTED" for d in decisions),
        "needs_evidence": sum(d.get("disposition") == "NEEDS_EVIDENCE" for d in decisions),
        "items": [{"symbol": d["symbol"], "disposition": d["disposition"],
                   "reasons": d.get("reasons", [])} for d in decisions],
    }


def create_managed_app(cycle, execution_store, *, api_token, runtime_status, report_submit=None,
                       position_news=None, status_token=None, operator_token=None,
                       agent_tokens=None, research_context=None, trade_reviews=None,
                       bar_reader=None, clock=None, health_check_database=True,
                       learning_intake=None):
    """Inject an actual runtime status callback; serving this page starts no worker.

    ``health_check_database`` (default True, the Mac app on loopback): ``GET /health`` reads the
    ledger once per request. The Railway trader passes False (package cloud-hardening): its
    ``/health`` is on a public domain, so it answers liveness from memory, without a thread or
    a database session, and a flood of it cannot take the connections and threads that status,
    Muse, the watchdog and the trader's own authorization and protection writes need.
    Readiness stays in the token-protected status.

    ``clock`` (default ``lambda: datetime.now(UTC)``) is read by the maintenance-replay routes
    (the only ones that bound a live bar fetch by "now"); tests inject a fixture clock so its
    "now" agrees with the fixture events it is comparing against, exactly as every other
    stateful component in this codebase (``ManagedExecution``, ``TradeMaintenance``, ...) takes
    an injectable clock rather than reading the wall clock directly.

    ``research_context`` (research_context.ResearchContextService) serves the read-only
    ``GET /api/v1/lab/research-context``; without it that route answers 503.

    ``trade_reviews`` (trade_review.TradeReviewService, package day-review) serves the agent's
    24-hour reviews and early exits: ``GET /api/v1/lab/reviews`` (its pending requests),
    ``POST /api/v1/lab/reviews/{review_id}/answer``, ``POST /api/v1/lab/exit-flags/{flag_id}/
    answer`` and ``POST /api/v1/lab/positions/{setup_id}/exit-flag``; without it they answer
    503. A research credential sees and answers only requests addressed to its own agent.

    ``bar_reader`` (``public_crypto_bars.PublicCryptoBarReader`` in production; no credential,
    no network access from this process otherwise) serves the pick and maintenance shadow-
    outcome/replay reads that need Alpaca's public minute bars
    (``GET /api/v1/lab/results/maintenance`` and its ``/aggregates``); without it those two
    routes answer 503. ``GET /api/v1/lab/results/picks`` and its ``/aggregates`` need no
    ``bar_reader``: they only read already-recorded ``PICK_SHADOW_OUTCOME`` events.

    ``learning_intake`` (learning_intake.LearningIntake, package learning-app) records a research
    agent's ``MARKET_OUTLOOK_V1`` (``POST /api/v1/lab/market-outlooks``) and ``POST_MORTEM_V1``
    (``POST /api/v1/lab/post-mortems``); without it both answer 503. Neither reaches Jev, and the
    results routes stay closed to agent tokens.

    With ``status_token`` and ``operator_token`` (private config v2) every token has one role:
    ``api_token`` is Muse's, the status token is GET-only and the operator token is reserved.
    Without them the single ``api_token`` keeps its previous access unchanged.

    ``agent_tokens`` (``{agent_id: token}``, plan 1.3) adds one research-agent credential per
    agent with Muse's routes. ``api_token`` stays the legacy identity: agent ``muse``, and
    the only credential that may submit unversioned legacy reports. A report's
    ``agent.agent_id`` must be the credential's agent, and evidence, claims and position
    news only reach cycles and setups recorded for that agent (403 AGENT_IDENTITY_MISMATCH).
    """
    if not isinstance(api_token, str) or len(api_token) < 32 or any(c.isspace() for c in api_token):
        raise ValueError("PRIVATE_LAB_TOKEN_REQUIRED")
    clock = clock or (lambda: datetime.now(UTC))
    separated = status_token is not None or operator_token is not None
    if separated and (
        any(not isinstance(t, str) or len(t) < 32 or any(c.isspace() for c in t)
            for t in (status_token, operator_token))
        or len({api_token, status_token, operator_token}) != 3
    ):
        raise ValueError("SEPARATE_ROLE_TOKENS_REQUIRED")
    agents = validated_agent_tokens(
        agent_tokens, reserved=(api_token, status_token, operator_token)
    )
    credentials = [(api_token, legacy_principal())]
    if separated:
        credentials += [(status_token, Principal("status")),
                        (operator_token, Principal("operator"))]
    credentials += [(token, Principal("muse", agent_id))
                    for agent_id, token in sorted(agents.items())]
    credentials = [(("Bearer " + token).encode(), principal) for token, principal in credentials]
    repo = execution_store.repo
    repo.require_same_database(cycle.repo)
    app = FastAPI(title="Catalyst managed paper lab", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def headers(request, call_next):
        if sum(len(k) + len(v) for k, v in request.scope.get("headers", [])) > 16384:
            return JSONResponse({"detail": "HEADERS_TOO_LARGE"}, status_code=431)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        })
        return response

    # Muse and every research agent: its report, evidence and position-news routes, the
    # position list its news work reads, and the output feeds (the same routes MuseHttp
    # allows), plus the read-only research context that report-V3 agents read first (the
    # Muse worker does not call it). Status: GET only. Operator: reserved for the operator
    # HTTP of plan 4.3; no route accepts it yet.
    muse_routes = frozenset({
        ("POST", "/api/v1/lab/research-reports"),
        ("GET", "/api/v1/lab/research-context"),  # Read-only (2026-09-26, report V3).
        ("GET", "/api/v1/lab/outputs"),
        ("GET", "/api/v1/lab/cycles/{cycle_id}/outputs"),
        ("POST", "/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim"),
        ("POST", "/api/v1/lab/cycles/{cycle_id}/evidence"),
        ("GET", "/api/v1/lab/positions"),
        ("GET", "/api/v1/lab/positions/{setup_id}/news"),
        ("POST", "/api/v1/lab/positions/{setup_id}/news"),
        # 24-hour reviews and early exits (package day-review): the caller's own only.
        ("GET", "/api/v1/lab/reviews"),
        ("POST", "/api/v1/lab/reviews/{review_id}/answer"),
        ("POST", "/api/v1/lab/exit-flags/{flag_id}/answer"),
        ("POST", "/api/v1/lab/positions/{setup_id}/exit-flag"),
        # The learning loop (package learning-app): the agent's own outlook and post-mortems.
        ("POST", "/api/v1/lab/market-outlooks"),
        ("POST", "/api/v1/lab/post-mortems"),
    })

    def authenticate(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        # Compare with every token so timing does not reveal which credential matched.
        matched = [principal for bearer, principal in credentials
                   if hmac.compare_digest(supplied, bearer)]
        if len(matched) != 1:
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")
        principal = matched[0]
        route = (request.method, getattr(request.scope.get("route"), "path", None))
        # Without role tokens the single legacy token keeps its previous full access.
        if not ((principal.legacy and not separated)
                or (principal.role == "muse" and route in muse_routes)
                or (principal.role == "status" and request.method == "GET")):
            raise HTTPException(403, "TOKEN_ROLE_NOT_PERMITTED")
        request.state.principal = principal

    def recorded_agent(sql, key):
        """(found, agent_id) of a cycle or setup; agent_id None = unattributed legacy."""
        with repo.connect() as conn:
            row = conn.execute(sql, (key,)).fetchone()
        agent = row["agent"] if row else None
        return row is not None, agent.get("agent_id") if isinstance(agent, dict) else None

    def require_owner(request, sql, key):
        """A missing cycle or setup is left to the handler's own refusal."""
        found, owner = recorded_agent(sql, key)
        if found and not request.state.principal.acts_for(owner):
            raise HTTPException(403, "AGENT_IDENTITY_MISMATCH")

    cycle_agent = """SELECT body->'agent' AS agent FROM lab.managed_events
        WHERE kind='RESEARCH_STARTED' AND body->>'cycle_id'=%s ORDER BY event_seq LIMIT 1"""
    setup_agent = "SELECT record_json->'agent' AS agent FROM lab.managed_setups WHERE setup_id=%s"

    @app.exception_handler(psycopg.Error)
    async def db_error(request, exc):
        return JSONResponse({"detail": "LAB_DATABASE_UNAVAILABLE"}, status_code=503)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        reason = str(exc)
        return JSONResponse({"detail": reason if re.fullmatch(r"[A-Z_]{1,80}", reason)
                             else "INVALID_LAB_REQUEST"}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML

    @app.get("/lab.js")
    def javascript():
        return Response(JS, media_type="text/javascript")

    @app.get("/lab.css")
    def stylesheet():
        return Response(CSS, media_type="text/css")

    @app.get("/results", response_class=HTMLResponse)
    def results_page():
        return RESULTS_HTML

    @app.get("/results.js")
    def results_javascript():
        return Response(RESULTS_JS, media_type="text/javascript")

    health_body = {"status": "ok", "alpaca": "paper", "surface": "PRIVATE_ENGINEERING_LAB",
                   "cohort": COHORT, "supervised": True}
    if health_check_database:
        @app.get("/health")
        def health():
            with repo.connect() as conn:
                conn.execute("SELECT 1 FROM lab.managed_events LIMIT 1")
            return health_body
    else:
        @app.get("/health")
        async def health():
            return health_body  # Liveness only: no thread, no database session.

    @app.get("/api/v1/lab/status", dependencies=[Depends(authenticate)])
    def status():
        state = runtime_status()
        if not isinstance(state, dict):
            raise ValueError("RUNTIME_STATUS_UNAVAILABLE")
        return json_safe({"cohort": COHORT, "supervised": True,
                          **{k: v for k, v in state.items() if k in STATUS_FIELDS}})

    @app.get("/api/v1/lab/outputs", dependencies=[Depends(authenticate)])
    def outputs(request: Request, after: int = Query(0, ge=0),
                limit: int = Query(100, ge=1, le=1000)):
        # Package learning-app: a research-agent credential never reads the learning records
        # here (owner decision of 2026-09-28: its own sanitized lessons only, through the
        # research context). The status credential and an unseparated legacy token read all.
        principal = request.state.principal
        if principal.role == "muse" and not (principal.legacy and not separated):
            rows = execution_store.outputs(after=after, limit=limit,
                                           exclude_kinds=LEARNING_EVENT_KINDS)
        else:
            rows = execution_store.outputs(after=after, limit=limit)
        return json_safe({"items": rows, "next_cursor": rows[-1]["event_seq"] if rows else after})

    @app.get("/api/v1/lab/cycles", dependencies=[Depends(authenticate)])
    def cycles():
        with repo.connect() as conn:
            rows = conn.execute("""SELECT event_seq,body,recorded_at FROM lab.managed_events
                WHERE kind='RESEARCH_STARTED' ORDER BY event_seq DESC LIMIT 50""").fetchall()
        return json_safe({"items": [{
            "cycle_id": r["body"]["cycle_id"], "contender_count": r["body"]["contender_count"],
            "expires_at": r["body"]["expires_at"], "recorded_at": r["recorded_at"],
            "cohort": COHORT,
            "research_origin": r["body"].get("research_origin", "LEGACY_APP_SCAN"),
            **agent_fields(r["body"].get("agent")),
            "scan": scan_readback(r["body"]) if "scan" in r["body"] else None,
        } for r in rows]})

    @app.post("/api/v1/lab/research-reports", dependencies=[Depends(authenticate)], status_code=202)
    async def research_report(request: Request):
        if report_submit is None:
            raise HTTPException(503, "MUSE_REPORT_INTAKE_NOT_CONFIGURED")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1048576:
                raise HTTPException(413, "RESEARCH_REPORT_TOO_LARGE")
            chunks.append(chunk)
        try:
            payload = strict_json(b"".join(chunks))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "INVALID_MUSE_REPORT") from None
        try:
            # A V2 report names its agent, which must be the credential's; an unversioned
            # legacy report (no agent block) is accepted only from the legacy credential.
            agent = declared_agent(payload)
            if not request.state.principal.acts_for(agent["agent_id"] if agent else None):
                raise HTTPException(403, "AGENT_IDENTITY_MISMATCH")
            # 202 carries per-item `item_results`; a refused report stores nothing and its
            # 422 names codes and field paths only, never submitted values.
            return json_safe(await report_submit(payload))
        except ResearchReportRejected as exc:
            return JSONResponse(json_safe(exc.detail()), status_code=422)
        except ResearchCapabilityUnavailable as exc:
            # Report V3 without its schedule or tradable universe: nothing stored, retryable.
            raise HTTPException(503, exc.code) from None

    async def learning_record(request, *, limit, too_large, invalid, submit):
        """A learning body (package learning-app): bounded, strict JSON, its agent block
        validated and bound to the credential before intake, refusals as report refusals."""
        if learning_intake is None:
            raise HTTPException(503, "LEARNING_INTAKE_NOT_CONFIGURED")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, too_large)
            chunks.append(chunk)
        try:
            payload = strict_json(b"".join(chunks))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, invalid) from None
        try:
            agent = learning_agent(payload, invalid)
            if not request.state.principal.acts_for(agent["agent_id"]):
                raise HTTPException(403, "AGENT_IDENTITY_MISMATCH")
            return json_safe(await asyncio.to_thread(submit, payload))
        except ResearchReportRejected as exc:
            return JSONResponse(json_safe(exc.detail()), status_code=422)
        except ResearchCapabilityUnavailable as exc:
            raise HTTPException(503, exc.code) from None

    @app.post("/api/v1/lab/market-outlooks", dependencies=[Depends(authenticate)],
              status_code=202)
    async def market_outlook(request: Request):
        """MARKET_OUTLOOK_V1: the agent's own morning outlook, one immutable record."""
        return await learning_record(
            request, limit=OUTLOOK_BODY_LIMIT, too_large="MARKET_OUTLOOK_TOO_LARGE",
            invalid="INVALID_MARKET_OUTLOOK",
            submit=lambda body: learning_intake.submit_outlook(body))

    @app.post("/api/v1/lab/post-mortems", dependencies=[Depends(authenticate)],
              status_code=202)
    async def post_mortem(request: Request):
        """POST_MORTEM_V1: up to 30 cited causes about the agent's own trades and movers."""
        principal = request.state.principal
        return await learning_record(
            request, limit=POST_MORTEM_BODY_LIMIT, too_large="POST_MORTEM_TOO_LARGE",
            invalid="INVALID_POST_MORTEM",
            submit=lambda body: learning_intake.submit_post_mortem(body, principal=principal))

    @app.get("/api/v1/lab/research-context", dependencies=[Depends(authenticate)])
    def research_context_route(request: Request):
        """Read-only: tradable universe, schedule and the caller's own trades and outcomes."""
        if research_context is None:
            raise HTTPException(503, "RESEARCH_CONTEXT_NOT_CONFIGURED")
        try:
            return json_safe(research_context.context(request.state.principal))
        except ResearchCapabilityUnavailable as exc:
            raise HTTPException(503, exc.code) from None

    @app.get("/api/v1/lab/cycles/{cycle_id}/outputs", dependencies=[Depends(authenticate)])
    def cycle_outputs(cycle_id: UUID, after: int = Query(0, ge=0),
                      limit: int = Query(100, ge=1, le=1000)):
        rows = cycle.outputs(str(cycle_id), after=after, limit=limit)
        return json_safe({"items": rows, "next_cursor": rows[-1]["event_seq"] if rows else after})

    @app.get("/api/v1/lab/cycles/{cycle_id}/picks", dependencies=[Depends(authenticate)])
    def cycle_picks_route(cycle_id: UUID):
        # Package results (plan 4.8): every report-V3 pick of this cycle -- selected and
        # traded, ranked but not selected, vetoed, not ranked, declined by the system check --
        # with its current rank/status. Empty for a non-report-V3 (or unknown) cycle_id; no
        # shadow price outcome here (that needs the pick's window plus the 24-hour hold to
        # have elapsed -- see GET /api/v1/lab/results/picks).
        return json_safe({"cycle_id": str(cycle_id),
                          "items": [pick.to_dict() for pick in cycle_picks(repo, cycle_id)]})

    @app.get("/api/v1/lab/positions/{setup_id}/news", dependencies=[Depends(authenticate)])
    def position_news_context(setup_id: UUID):
        if position_news is None:
            raise HTTPException(503, "POSITION_NEWS_NOT_CONFIGURED")
        return json_safe(position_news.context(str(setup_id)))

    @app.post("/api/v1/lab/positions/{setup_id}/news", dependencies=[Depends(authenticate)])
    async def post_position_news(setup_id: UUID, request: Request):
        if position_news is None:
            raise HTTPException(503, "POSITION_NEWS_NOT_CONFIGURED")
        await asyncio.to_thread(require_owner, request, setup_agent, str(setup_id))
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 32768:
                raise HTTPException(413, "EVIDENCE_TOO_LARGE")
            chunks.append(chunk)
        try:
            raw = strict_json(b"".join(chunks))
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "INVALID_POSITION_NEWS") from None
        # The posting agent's ID is screened out of the sources Jev will read.
        return json_safe(await asyncio.to_thread(
            position_news.submit, str(setup_id), raw,
            agent_id=request.state.principal.agent_id,
        ))

    async def review_call(method, *args):
        """A TradeReviewService call off the event loop; its refusals as HTTP codes."""
        from catalyst_lab.trade_review import ReviewAccessDenied, ReviewConflict, ReviewNotFound

        if trade_reviews is None:
            raise HTTPException(503, "TRADE_REVIEWS_NOT_CONFIGURED")
        try:
            return json_safe(await asyncio.to_thread(getattr(trade_reviews, method), *args))
        except ReviewNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except ReviewAccessDenied:
            raise HTTPException(403, "AGENT_IDENTITY_MISMATCH") from None
        except ReviewConflict as exc:
            code = str(exc)
            raise HTTPException(409, code if re.fullmatch(r"[A-Z_]{1,80}", code)
                                else "REVIEW_CONFLICT") from None

    async def review_body(request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 32768:
                raise HTTPException(413, "REVIEW_ANSWER_TOO_LARGE")
            chunks.append(chunk)
        try:
            body = strict_json(b"".join(chunks))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "INVALID_REVIEW_ANSWER") from None

    @app.get("/api/v1/lab/reviews", dependencies=[Depends(authenticate)])
    async def pending_reviews(request: Request):
        """The caller's pending 24-hour reviews and Jev exit flags (cheap to poll)."""
        return await review_call("pending", request.state.principal)

    @app.post("/api/v1/lab/reviews/{review_id}/answer", dependencies=[Depends(authenticate)])
    async def answer_review(review_id: UUID, request: Request):
        body = await review_body(request)
        return await review_call("answer_review", request.state.principal, str(review_id), body)

    @app.post("/api/v1/lab/exit-flags/{flag_id}/answer", dependencies=[Depends(authenticate)])
    async def answer_exit_flag(flag_id: UUID, request: Request):
        body = await review_body(request)
        return await review_call("answer_flag", request.state.principal, str(flag_id), body)

    @app.post("/api/v1/lab/positions/{setup_id}/exit-flag", dependencies=[Depends(authenticate)])
    async def raise_exit_flag(setup_id: UUID, request: Request):
        body = await review_body(request)
        return await review_call("raise_flag", request.state.principal, str(setup_id), body)

    async def evidence_body(request, maximum=32768):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum:
                raise HTTPException(413, "EVIDENCE_TOO_LARGE")
            chunks.append(chunk)
        try:
            body = strict_json(b"".join(chunks))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "INVALID_EVIDENCE_PACKET") from None

    @app.post("/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim",
              dependencies=[Depends(authenticate)])
    async def claim_evidence(cycle_id: UUID, request: Request):
        await asyncio.to_thread(require_owner, request, cycle_agent, str(cycle_id))
        body = await evidence_body(request, maximum=2048)
        if set(body) != {"claimant", "lease_seconds", "limit"}:
            raise HTTPException(422, "INVALID_EVIDENCE_CLAIM")
        if not callable(getattr(cycle, "claim_evidence_tasks", None)):
            raise HTTPException(503, "EVIDENCE_TASKS_NOT_CONFIGURED")
        tasks = await asyncio.to_thread(cycle.claim_evidence_tasks, str(cycle_id), **body)
        return json_safe({"tasks": tasks})

    @app.post("/api/v1/lab/cycles/{cycle_id}/evidence", dependencies=[Depends(authenticate)])
    async def evidence(cycle_id: UUID, request: Request):
        await asyncio.to_thread(require_owner, request, cycle_agent, str(cycle_id))
        body = await evidence_body(request)
        try:
            required = {"item_key", "revision", "sources", "thesis", "disproof",
                        "economic_relationship"}
            # ``selection_rationale`` (selection rule B2 items only; the cycle validates it
            # and refuses it for every other policy) revises the reviewed rationale.
            optional_keys = ("task_id", "technical_facts", "selection_rationale")
            if (not required <= set(body) or set(body) - required - set(optional_keys)
                or type(body["revision"]) is not int or body["revision"] < 1):
                raise ValueError
            if not isinstance(body["item_key"], str) or not 1 <= len(body["item_key"]) <= 100:
                raise ValueError
            if not isinstance(body["sources"], list):
                raise ValueError
            now = cycle.clock() if callable(getattr(cycle, "clock", None)) else datetime.now(UTC)
            sources = canonical_sources(body["sources"], now=now)
            thesis = ResearchThesis(body["thesis"], body["disproof"], body["economic_relationship"])
            optional = {key: body[key] for key in optional_keys if key in body}
            if "task_id" in optional:
                optional["task_id"] = str(UUID(optional["task_id"]))
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise HTTPException(422, "INVALID_EVIDENCE_PACKET") from None
        packet = await asyncio.to_thread(
            cycle.submit_evidence, str(cycle_id), body["item_key"], revision=body["revision"],
            sources=tuple(sources), thesis=thesis, **optional,
        )
        return json_safe({"status": "EVIDENCE_RECORDED", "cycle_id": str(cycle_id),
                          "revision": packet["revision"], "evidence_hash": packet["evidence_hash"],
                          "expires_at": packet["expires_at"], "trade_authorized": False})

    def setup_rows(before, limit):
        with repo.connect() as conn:
            rows = conn.execute("""SELECT s.setup_id,s.symbol,s.market,
                s.strategy_version,s.cohort,s.event_seq,
                s.expires_at,s.record_json,t.body AS state,
                (SELECT e.body FROM lab.managed_events e WHERE e.setup_id=s.setup_id
                  AND e.kind='MANAGED_JEV_JUDGMENT' ORDER BY e.event_seq DESC LIMIT 1) AS judgment
                FROM lab.managed_setups s LEFT JOIN lab.managed_states t USING(setup_id)
                WHERE (%s::bigint IS NULL OR s.event_seq<%s)
                ORDER BY s.event_seq DESC LIMIT %s""", (before, before, limit)).fetchall()
        return [{"setup_id": r["setup_id"], "symbol": r["symbol"], "market": r["market"],
                 "cursor_event_seq": r["event_seq"],
                 "strategy_version": r["strategy_version"], "cohort": r["cohort"],
                 "engineering": is_engineering(r["record_json"]),
                 **agent_fields(r["record_json"].get("agent")),
                 "expires_at": r["expires_at"], "levels": r["record_json"]["levels"],
                 "state": {k: v for k, v in (r["state"] or {}).items() if k in STATE_FIELDS},
                 "latest_management": r["judgment"]} for r in rows]

    @app.get("/api/v1/lab/setups", dependencies=[Depends(authenticate)])
    def setups(before: int | None = Query(None, ge=1), limit: int = Query(200, ge=1, le=1000)):
        rows = setup_rows(before, limit)
        return json_safe({"cohort": COHORT, "items": rows,
                          "next_cursor": rows[-1]["cursor_event_seq"] if rows else None})

    def measured_positions(before, limit, closed):
        with repo.connect() as conn:
            rows = conn.execute("""SELECT s.setup_id,s.symbol,s.market,s.cohort,
                s.event_seq AS cursor_event_seq,s.record_json->'agent' AS agent,
                coalesce(s.record_json->>'purpose'=%s,false) AS engineering,
                t.body AS state,
                sum(CASE f.side WHEN 'buy' THEN f.qty ELSE -f.qty END) AS gross_fill_balance,
                (SELECT e.body->>'qty' FROM lab.managed_events e
                 WHERE e.setup_id=s.setup_id AND e.kind='BROKER_POSITION'
                 ORDER BY (e.body->>'occurred_at')::timestamptz DESC NULLS LAST,
                          e.event_seq DESC LIMIT 1) AS broker_qty,
                sum(f.qty) FILTER(WHERE f.side='buy') AS bought,
                sum(f.qty*f.price) FILTER(WHERE f.side='buy') AS buy_notional,
                sum(f.qty*f.price) FILTER(WHERE f.side='sell') AS sell_notional,
                sum(f.fee_usd) AS fees,bool_and(f.fee_usd IS NOT NULL) AS fees_complete,
                min(f.filled_at) AS opened_at,max(f.filled_at) AS latest_fill_at
                FROM lab.managed_setups s JOIN lab.managed_fills f USING(setup_id)
                LEFT JOIN lab.managed_states t USING(setup_id)
                WHERE (%s::bigint IS NULL OR s.event_seq<%s)
                AND ((t.body->>'state'='CLOSED')=%s)
                GROUP BY s.setup_id,t.body ORDER BY s.event_seq DESC LIMIT %s""",
                (ENGINEERING_PURPOSE, before, before, closed, limit)).fetchall()
        result = []
        for row in rows:
            row.update(agent_fields(row.pop("agent")))
            bought = row.pop("bought") or Decimal(0)
            buy = row.pop("buy_notional") or Decimal(0)
            sell = row.pop("sell_notional") or Decimal(0)
            fees = row.pop("fees")
            broker_qty = row.pop("broker_qty")
            state = row["state"] or {}
            row["open_qty"] = Decimal(broker_qty) if broker_qty is not None else (
                Decimal(state["qty"]) if state.get("qty") is not None else None
            )
            row["quantity_source"] = "BROKER_POSITION" if broker_qty is not None else (
                "EXECUTION_STATE" if state.get("qty") is not None else "UNKNOWN"
            )
            closed = bought > 0 and row["open_qty"] == 0 and state.get("state") == "CLOSED"
            row["closed"] = closed
            row["fill_price"] = buy / bought if bought else None
            row["gross_pnl_usd"] = sell - buy if closed else None
            row["net_pnl_usd"] = sell - buy - fees if closed and row["fees_complete"] else None
            row["state"] = {k: v for k, v in (row["state"] or {}).items() if k in STATE_FIELDS}
            row["execution_source"] = "ALPACA_PAPER"
            result.append(row)
        return result

    @app.get("/api/v1/lab/positions", dependencies=[Depends(authenticate)])
    def positions(before: int | None = Query(None, ge=1), limit: int = Query(200, ge=1, le=1000)):
        rows = measured_positions(before, limit, False)
        return json_safe({"cohort": COHORT,
                          "items": [r for r in rows if r["open_qty"] != 0],
                          "next_cursor": rows[-1]["cursor_event_seq"] if rows else None})

    @app.get("/api/v1/lab/results", dependencies=[Depends(authenticate)])
    def results(before: int | None = Query(None, ge=1), limit: int = Query(200, ge=1, le=1000)):
        page = measured_positions(before, limit, True)
        rows = [r for r in page if r["closed"]]
        for row in rows:
            row["measurement"] = managed_measurement(repo, row["setup_id"])
            row["fees_complete"] = row["measurement"]["fees_verified"]
            row["net_pnl_usd"] = row["measurement"]["net_pnl"]
            row["test_r"] = row["measurement"]["test_r"]
            row["net_r"] = row["measurement"]["net_r"]
            row["official_r"] = row["measurement"]["official_r"]
        return json_safe({"cohort": COHORT, "baseline_included": False,
                          "items": rows,
                          "next_cursor": page[-1]["cursor_event_seq"] if page else None})

    @app.get("/api/v1/lab/history/results", dependencies=[Depends(authenticate)])
    def historical_results(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return json_safe(paginated_managed_results(repo, after_event_seq=after, limit=limit))

    @app.get("/api/v1/lab/results/aggregates", dependencies=[Depends(authenticate)])
    def result_aggregates(group_by: str | None = Query(None)):
        dimensions = tuple(group_by.split(",")) if group_by else None
        kwargs = {} if dimensions is None else {"group_by": dimensions}
        return json_safe(managed_result_aggregates(repo, **kwargs))

    @app.get("/api/v1/lab/results/picks", dependencies=[Depends(authenticate)])
    def pick_outcomes_route(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        # Package results (plan 4.8): every report-V3 pick's recorded PICK_SHADOW_OUTCOME_V1 --
        # a 1-minute-bar approximation of what it would have done -- next to its real
        # measurement when it actually traded. Only picks whose window plus the 24-hour hold
        # has fully elapsed have a recorded outcome (scripts/run_pick_shadow_outcomes.py).
        return json_safe(pick_outcome_page(repo, after_event_seq=after, limit=limit))

    @app.get("/api/v1/lab/results/picks/aggregates", dependencies=[Depends(authenticate)])
    def pick_outcome_aggregates_route(group_by: str | None = Query(None)):
        dimensions = tuple(group_by.split(",")) if group_by else None
        kwargs = {} if dimensions is None else {"group_by": dimensions}
        return json_safe(pick_outcome_aggregates(repo, **kwargs))

    @app.get("/api/v1/lab/results/maintenance", dependencies=[Depends(authenticate)])
    def maintenance_replay_route(setup_id: str | None = Query(None, max_length=36),
                                 after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
        # Package results (plan 4.6.8, "Measuring maintenance"): every applied maintenance
        # stop/target raise and every agreed early exit, replayed against the levels the
        # decision itself recorded -- per trade with setup_id, across every trade without it.
        if bar_reader is None:
            raise HTTPException(503, "MAINTENANCE_REPLAY_NOT_CONFIGURED")
        return maintenance_replay_page(
            repo, bar_reader, now=clock(),
            setup_id=UUID(setup_id) if setup_id is not None else None,
            after_event_seq=after, limit=limit,
        )

    @app.get("/api/v1/lab/results/maintenance/aggregates", dependencies=[Depends(authenticate)])
    def maintenance_replay_aggregates_route(group_by: str | None = Query(None),
                                            limit: int = Query(500, ge=1, le=500)):
        if bar_reader is None:
            raise HTTPException(503, "MAINTENANCE_REPLAY_NOT_CONFIGURED")
        dimensions = tuple(group_by.split(",")) if group_by else ("change_kind", "arm")
        return maintenance_replay_aggregates(repo, bar_reader, now=clock(),
                                             group_by=dimensions, limit=limit)

    @app.get("/api/v1/lab/analytics/research", dependencies=[Depends(authenticate)])
    def funnel(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return research_funnel(repo, after=after, limit=limit)

    @app.get("/api/v1/lab/analytics/daily", dependencies=[Depends(authenticate)])
    def daily(start: date | None = None, end: date | None = None):
        return json_safe(managed_daily_rollups(repo, start_date=start, end_date=end))

    @app.get("/api/v1/lab/positions/{setup_id}/timeline", dependencies=[Depends(authenticate)])
    def timeline(setup_id: UUID, after: int = Query(0, ge=0),
                 limit: int = Query(100, ge=1, le=1000)):
        # Include the originating report/revisions/selection along with execution events.
        with repo.connect() as conn:
            setup = execution_store.setup(conn, str(setup_id))
            rows = conn.execute("""SELECT event_seq,kind,body,recorded_at FROM lab.managed_events
                WHERE event_seq>%s AND (setup_id=%s OR body->>'cycle_id'=%s)
                ORDER BY event_seq LIMIT %s""",
                (after, setup_id, str(setup["cycle_id"]), limit)).fetchall()
        return json_safe({"setup_id": str(setup_id), "cohort": COHORT, "items": rows,
                          "next_cursor": rows[-1]["event_seq"] if rows else after})

    @app.get("/api/v1/lab/positions/{setup_id}/measurement", dependencies=[Depends(authenticate)])
    def position_measurement(setup_id: UUID):
        with repo.connect() as conn:
            setup = execution_store.setup(conn, str(setup_id))
        return json_safe({"setup_id": str(setup_id), "cohort": setup["cohort"],
                          "engineering": is_engineering(setup["record_json"]),
                          "measurement": managed_measurement(repo, str(setup_id)),
                          "execution_quality": execution_quality(repo, str(setup_id))})

    return app


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Catalyst Paper Lab</title>
<link rel="stylesheet" href="/lab.css"><script defer src="/lab.js"></script></head>
<body><main><header><div><span class="eyebrow">CATALYST / PRIVATE LAB</span>
<h1>Research to paper trade.</h1></div>
<span class="badge">SUPERVISED · ENGINEERING TEST</span></header>
<p class="banner">PAPER TRADING — SIMULATED. Not real money.</p>
<section id="connect"><label for="token">Private lab access</label>
<input id="token" type="password" autocomplete="off" placeholder="Local access token">
<button id="connect-button">Connect</button><span id="message" role="status">Disconnected</span>
</section><section class="steps"><article><span>01 / MUSE</span><strong id="contenders">—</strong>
<p>Researched contenders</p></article><article><span>02 / DECISION JEV</span>
<strong id="selected">—</strong><p>Selected · evidence checked</p></article>
<article><span>03 / APP + RISK</span><strong id="watching">—</strong>
<p>Watching for entry</p></article>
<article><span>04 / MANAGEMENT JEV</span><strong id="open">—</strong><p>Open paper positions</p>
</article></section><section class="section-head"><h2>Trade session</h2>
<span id="runtime">Worker status unavailable</span></section>
<div class="table-wrap"><table><thead><tr><th>Instrument</th><th>State</th><th>Entry</th>
<th>Stop</th><th>Target</th><th>Latest Jev</th></tr></thead>
<tbody id="trades"></tbody></table></div>
<p id="empty">Connect to see the audited session.</p><section class="section-head">
<h2>Evidence requests</h2><span>Muse resolves these; no forced approvals</span></section>
<ul id="requests"></ul><details id="legacy-scan" hidden>
<summary id="scan-summary">Historical scan</summary>
<p>Historical diagnostic only. Current research reports come from Muse.</p>
<div class="table-wrap"><table><thead><tr><th>Instrument</th><th>Scanner result</th>
<th>Reason</th></tr></thead><tbody id="scan-items"></tbody></table></div></details>
<details><summary>Audit activity</summary><ul id="audit"></ul></details>
<footer>JEV_MANAGED_PAPER_V1 · Separate from the frozen baseline · India: research only</footer>
</main></body></html>"""

CSS = """*{box-sizing:border-box}body{margin:0;background:#f4f5f1;color:#17221d;
font:15px/1.5 system-ui,sans-serif}main{max-width:1180px;margin:0 auto;padding:48px 28px}
header,.section-head{display:flex;justify-content:space-between;align-items:center;gap:20px}
.eyebrow,.steps article>span{font-size:11px;letter-spacing:.12em;color:#607269;font-weight:650}
h1{font-size:36px;letter-spacing:-1.4px;margin:8px 0 20px}h2{font-size:19px;margin:0}
.badge{font-size:11px;border:1px solid #c9d1c9;border-radius:20px;padding:8px 12px;
white-space:nowrap}
.banner{background:#e7eddf;border-left:3px solid #6b8058;padding:12px 16px;font-size:13px}
#connect{display:flex;align-items:center;gap:12px;margin:26px 0 32px}input,button{font:inherit;
padding:10px 12px;border:1px solid #cbd1c9;border-radius:6px}input{background:white;min-width:260px}
button{background:#203c2d;color:#fff;cursor:pointer}#message{font-size:12px;color:#607269}
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:40px}
.steps article{background:white;border:1px solid #dde2d9;padding:22px;border-radius:9px}
strong{display:block;font-size:38px;font-weight:550;letter-spacing:-1px;margin:10px 0}
.steps p{font-size:12px;margin:0;color:#607269}.section-head{margin:28px 0 14px}
.section-head>span{font-size:12px;color:#607269}.table-wrap{overflow-x:auto}table{width:100%;
border-collapse:collapse;background:white;border:1px solid #dde2d9}th,td{text-align:left;
padding:14px 16px;border-bottom:1px solid #e7ebe3;font-size:13px}th{font-size:10px;
text-transform:uppercase;letter-spacing:.08em;color:#607269;font-weight:550}#empty{font-size:13px;
color:#607269;padding:12px}ul{padding-left:20px}li{margin:8px 0;font-size:13px}
details{margin-top:26px;
border-top:1px solid #d8dfd4;padding-top:18px}summary{cursor:pointer;color:#607269;font-size:13px}
footer{font-size:11px;color:#687a6d;margin-top:42px;border-top:1px solid #d8dfd4;padding-top:18px}
@media(max-width:760px){main{padding:24px 16px}header,.section-head,#connect{align-items:flex-start;
flex-direction:column}.steps{grid-template-columns:repeat(2,1fr)}h1{font-size:28px}input{width:100%}}
"""

JS = """'use strict';
let token = '', busy = false;
const el = id => document.getElementById(id);
async function get(path) {
  const response = await fetch('/api/v1/lab/' + path, {
    headers: {Authorization: 'Bearer ' + token}, cache: 'no-store'});
  if (!response.ok) throw new Error('Access or data unavailable (' + response.status + ')');
  return response.json();
}
function cell(row, value) { const td = document.createElement('td');
  td.textContent = value == null ? '—' : String(value); row.append(td); }
function list(target, lines) { el(target).replaceChildren();
  for (const text of lines) { const li = document.createElement('li');
    li.textContent = text; el(target).append(li); } }
async function refresh() {
  if (!token || busy) return;
  busy = true;
  try {
    const [status, cycles, setups, positions] = await Promise.all([
      get('status'), get('cycles'), get('setups'), get('positions')]);
    const latest = cycles.items[0];
    const scan = latest && latest.scan;
    el('legacy-scan').hidden = !scan;
    el('scan-summary').textContent = scan ? 'Historical app scan · ' +
      scan.screened + ' screened · ' +
      scan.technical_exclusions + ' technical exclusions · ' + scan.needs_evidence +
      ' awaiting evidence' : 'Muse research';
    el('scan-items').replaceChildren();
    for (const item of (scan ? scan.items : [])) {
      const row = document.createElement('tr');
      cell(row, item.symbol); cell(row, item.disposition);
      cell(row, item.reasons.join(', ')); el('scan-items').append(row);
    }
    const output = latest ? await get('cycles/' + latest.cycle_id + '/outputs?limit=1000')
                          : {items: []};
    const revisions = new Map(), decisions = new Map();
    for (const event of output.items) {
      if (event.kind === 'RESEARCH_PACKET') revisions.set(event.body.item_key,event.body.revision);
      if (event.kind === 'RESEARCH_DECISION') decisions.set(event.body.item_key,event.body);
    }
    const selected = output.items.filter(e => e.kind === 'RESEARCH_SELECTED' &&
      revisions.get(e.body.packet.item_key) === e.body.packet.revision);
    el('contenders').textContent = latest ? latest.contender_count : '0';
    el('selected').textContent = selected.length;
    el('watching').textContent = setups.items.filter(s => s.state.state === 'WATCHING').length;
    el('open').textContent = positions.items.length;
    el('runtime').textContent = (status.worker_state || 'Unavailable') + ' · Management review: ' +
      (status.management_review_enabled === true ? 'configured' : 'off');
    el('trades').replaceChildren();
    for (const s of setups.items) { const tr = document.createElement('tr');
      [s.symbol + ' · ' + s.market + (s.engineering ? ' · ENGINEERING TEST' : ''), s.state.state,
       s.state.fill_price || s.state.entry_fill_price || s.levels.max_entry_price,
       s.state.stop || s.levels.stop, s.state.target || s.levels.target,
       s.latest_management ? s.latest_management.action : 'No judgment yet']
       .forEach(v => cell(tr,v));
      el('trades').append(tr); }
    el('empty').textContent = setups.items.length ? '' : 'No setup has entered the paper session.';
    const requests = [...decisions.values()].filter(d => d.disposition === 'NEEDS_REVIEW' &&
      revisions.get(d.item_key) === d.revision);
    list('requests', requests.length ? requests.map(d => d.item_key + ': ' +
      d.evidence_tasks.map(t => t.task.replaceAll('_',' ').toLowerCase()).join('; '))
      : ['No outstanding evidence requests in this cycle.']);
    list('audit', output.items.slice(-12).reverse().map(e => '#' + e.event_seq + ' · ' +
      e.kind.replaceAll('_',' ').toLowerCase()));
    el('message').textContent = 'Connected · refreshed ' + new Date().toLocaleTimeString();
  } catch (error) { el('message').textContent = error.message; }
  finally { busy = false; }
}
el('connect-button').addEventListener('click', () => {
  token = el('token').value; el('token').value = ''; refresh();
});
setInterval(refresh, 5000);
"""

RESULTS_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalyst Paper Lab — Results</title>
<script defer src="/results.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f1;color:#17221d;
font:15px/1.5 system-ui,sans-serif}main{max-width:1180px;margin:0 auto;padding:40px 24px}
a.back{font-size:12px;color:#607269;text-decoration:none}h1{font-size:26px;letter-spacing:-1px;
margin:6px 0 4px}.banner{background:#e7eddf;border-left:3px solid #6b8058;padding:10px 14px;
font-size:13px;margin-bottom:20px}
#connect{display:flex;align-items:center;gap:12px;margin:10px 0 28px}input,button{font:inherit;
padding:9px 11px;border:1px solid #cbd1c9;border-radius:6px}
input{background:#fff;min-width:240px}button{background:#203c2d;color:#fff;cursor:pointer}
#message{font-size:12px;color:#607269}
section{margin:30px 0}h2{font-size:16px;margin:0 0 4px}.subtle{font-size:12px;color:#607269;
margin:0 0 10px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;
background:#fff;border:1px solid #dde2d9}th,td{text-align:left;padding:10px 12px;
border-bottom:1px solid #e7ebe3;font-size:12.5px;white-space:nowrap}
th{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#607269;font-weight:550}
.empty{font-size:13px;color:#607269;padding:10px 0}
.group-controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.group-controls button{background:#fff;color:#203c2d;border:1px solid #cbd1c9;
padding:5px 10px;font-size:12px}
.group-controls button.active{background:#203c2d;color:#fff}
footer{font-size:11px;color:#687a6d;margin-top:40px;border-top:1px solid #d8dfd4;
padding-top:16px}
@media(max-width:760px){main{padding:24px 16px}#connect{align-items:flex-start;
flex-direction:column}input{width:100%}}
</style></head>
<body><main>
<a class="back" href="/">&larr; Session</a>
<h1>Results</h1>
<p class="banner">PAPER TRADING — SIMULATED. Not real money. Every pick's shadow outcome is a
1-minute-bar approximation, not a fill or a quote; a traded pick's own real, fee-verified
measurement is shown next to it.</p>
<section id="connect"><label for="token">Private lab access</label>
<input id="token" type="password" autocomplete="off" placeholder="Local access token">
<button id="connect-button">Connect</button><span id="message" role="status">Disconnected</span>
</section>

<section><h2>Today's picks</h2>
<p class="subtle" id="picks-meta">Rank, status and outcome of the latest tracked research run.</p>
<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Kind</th><th>Agent</th>
<th>Jev rank</th><th>Rank bucket</th><th>Status</th></tr></thead>
<tbody id="picks-body"></tbody></table></div>
<p class="empty" id="picks-empty">No tracked report-V3 run yet.</p></section>

<section><h2>Open trades</h2>
<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Agent</th><th>Entry</th>
<th>Stop</th><th>Target</th><th>Qty</th><th>Opened</th></tr></thead>
<tbody id="open-body"></tbody></table></div>
<p class="empty" id="open-empty">No open paper positions.</p></section>

<section><h2>Closed trades, net of fees</h2>
<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Agent</th><th>Fees</th>
<th>Net P&amp;L</th><th>Official R</th><th>Net R</th></tr></thead>
<tbody id="closed-body"></tbody></table></div>
<p class="empty" id="closed-empty">No closed trades yet.</p></section>

<section><h2>Results aggregates</h2>
<p class="subtle">Every closed managed setup, net of Alpaca's fees. Counts first: a mean over
a handful of trades is not a track record.</p>
<div class="table-wrap"><table><thead><tr><th>Market</th><th>Arm</th><th>Agent</th><th>Count</th>
<th>Win rate</th><th>Mean gross R</th><th>Mean net R</th><th>Mean official R</th>
<th>Fees, verified</th></tr></thead><tbody id="results-agg-body"></tbody></table></div>
<p class="empty" id="results-agg-empty">No closed managed setups yet.</p></section>

<section><h2>Pick outcome aggregates</h2>
<p class="subtle">Every tracked pick's shadow outcome (1-minute-bar approximation), grouped —
so Jev's picks can be compared with the ones it passed on. <code>real_count</code> is only the
subset that actually traded.</p>
<div class="group-controls" id="pick-agg-groups"></div>
<div class="table-wrap"><table><thead><tr><th>Group</th><th>Count</th><th>Shadow win rate</th>
<th>Mean shadow gross R</th><th>Mean shadow net R</th><th>Real count</th>
<th>Mean real official R</th></tr></thead><tbody id="pick-agg-body"></tbody></table></div>
<p class="empty" id="pick-agg-empty">No recorded shadow outcomes yet.</p></section>

<section><h2>Maintenance replay</h2>
<p class="subtle">Every applied stop/target raise and every agreed early exit, replayed against
the plan's original levels (<code>UNCHANGED_PLAN_REPLAY_V1</code>) — did the change help or
hurt, in R, against what actually happened? By change kind and arm.</p>
<div class="table-wrap"><table><thead><tr><th>Change kind</th><th>Arm</th><th>Count</th>
<th>With a known R difference</th><th>Mean R difference</th><th>Helped rate</th></tr></thead>
<tbody id="maint-agg-body"></tbody></table></div>
<p class="empty" id="maint-agg-empty">No replayed maintenance changes yet.</p></section>

<footer>JEV_MANAGED_PAPER_V1 · PICK_SHADOW_OUTCOME_V1 · UNCHANGED_PLAN_REPLAY_V1 ·
Continue/exit aggregates await the 24-hour-review package.</footer>
</main></body></html>"""

RESULTS_JS = """'use strict';
let token = '', busy = false;
const PICK_GROUPS = ['rank_bucket', 'pick_kind', 'agent_id', 'arm', 'selected'];
let pickGroup = 'rank_bucket';
const el = id => document.getElementById(id);
async function get(path) {
  const response = await fetch('/api/v1/lab/' + path, {
    headers: {Authorization: 'Bearer ' + token}, cache: 'no-store'});
  if (!response.ok) throw new Error('Access or data unavailable (' + response.status + ')');
  return response.json();
}
function cell(row, value) { const td = document.createElement('td');
  td.textContent = value == null ? '—' : String(value); row.append(td); }
function fillRows(bodyId, emptyId, rows, columns) {
  el(bodyId).replaceChildren();
  el(emptyId).hidden = rows.length > 0;
  for (const item of rows) { const tr = document.createElement('tr');
    for (const column of columns) cell(tr, column(item));
    el(bodyId).append(tr); }
}
async function latestTrackedPicks() {
  const cycles = await get('cycles');
  for (const c of cycles.items.slice(0, 5)) {
    const page = await get('cycles/' + c.cycle_id + '/picks');
    if (page.items.length) return {cycleId: c.cycle_id, items: page.items};
  }
  return {cycleId: null, items: []};
}
function renderPickGroupButtons() {
  el('pick-agg-groups').replaceChildren();
  for (const group of PICK_GROUPS) {
    const button = document.createElement('button');
    button.textContent = group.replaceAll('_', ' ');
    button.className = group === pickGroup ? 'active' : '';
    button.addEventListener('click', () => { pickGroup = group; refresh(); });
    el('pick-agg-groups').append(button);
  }
}
async function refresh() {
  if (!token || busy) return;
  busy = true;
  try {
    renderPickGroupButtons();
    const [picks, positions, results, resultsAgg, pickAgg] = await Promise.all([
      latestTrackedPicks(), get('positions'), get('results'), get('results/aggregates'),
      get('results/picks/aggregates?group_by=' + pickGroup),
    ]);
    // A separate try/catch: this route answers 503 until an operator configures a bar
    // reader, and that should not blank out every other section's just-fetched data.
    let maintAgg = {items: []};
    try { maintAgg = await get('results/maintenance/aggregates'); } catch (error) { /* 503 */ }
    el('picks-meta').textContent = picks.cycleId
      ? 'Cycle ' + picks.cycleId + ' · ' + picks.items.length + ' picks'
      : 'No tracked report-V3 run yet.';
    fillRows('picks-body', 'picks-empty', picks.items, [
      p => p.symbol, p => p.kind, p => p.agent_id || 'legacy', p => p.jev_rank,
      p => p.rank_bucket, p => p.selection_status || p.ranking_status || 'AWAITING_REVIEW',
    ]);
    fillRows('open-body', 'open-empty', positions.items, [
      p => p.symbol, p => p.agent_id || 'legacy', p => p.fill_price, p => p.state.stop,
      p => p.state.target, p => p.open_qty, p => p.opened_at,
    ]);
    fillRows('closed-body', 'closed-empty', results.items, [
      p => p.symbol, p => p.agent_id || 'legacy', p => (p.fees_complete ? 'verified' : 'unknown'),
      p => p.net_pnl_usd, p => p.official_r, p => p.net_r,
    ]);
    fillRows('results-agg-body', 'results-agg-empty', resultsAgg.items, [
      p => p.market, p => p.arm, p => p.agent_id || 'legacy', p => p.count, p => p.win_rate,
      p => p.mean_gross_r, p => p.mean_net_r, p => p.mean_official_r,
      p => p.total_fees_usd,
    ]);
    fillRows('pick-agg-body', 'pick-agg-empty', pickAgg.items, [
      p => p[pickGroup], p => p.count, p => p.shadow_win_rate, p => p.mean_shadow_gross_r,
      p => p.mean_shadow_net_r, p => p.real_count, p => p.mean_real_official_r,
    ]);
    fillRows('maint-agg-body', 'maint-agg-empty', maintAgg.items, [
      p => p.change_kind, p => p.arm, p => p.count, p => p.r_difference_count,
      p => p.mean_r_difference, p => p.helped_rate,
    ]);
    el('message').textContent = 'Connected · refreshed ' + new Date().toLocaleTimeString();
  } catch (error) { el('message').textContent = error.message; }
  finally { busy = false; }
}
el('connect-button').addEventListener('click', () => {
  token = el('token').value; el('token').value = ''; refresh();
});
setInterval(refresh, 15000);
"""
