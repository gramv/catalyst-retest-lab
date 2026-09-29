import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from catalyst_lab.config import MUSE_SCOPES, STRATEGY_VERSION, Settings
from catalyst_lab.domain import BatchEnvelope, Policy
from catalyst_lab.measurement import Measurements, MeasurementWorker
from catalyst_lab.repository import Repository

MAX_BODY_BYTES = 65536


def no_evidence(candidate, now):
    return None


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def check_json(value):
    if isinstance(value, str) and (
        "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("Unsupported Unicode in JSON")
    if isinstance(value, dict):
        for key, child in value.items():
            check_json(key)
            check_json(child)
    if isinstance(value, list):
        for child in value:
            check_json(child)


def create_app(
    settings: Settings | None = None,
    *,
    provider=no_evidence,
    policy: Policy | None = None,
    clock=None,
    market_runtime=None,
):
    settings = settings or Settings.from_env()
    repo = Repository(settings.database_url)
    now = clock or (lambda: datetime.now(UTC))
    measurements = Measurements(repo)
    measurement_worker = MeasurementWorker(measurements, clock=now)
    risk_runtime = None
    jev_admission_worker = None
    if settings.risk_database_url and not settings.read_only_market_data:
        raise ValueError("Risk authorization requires the market and broker monitors")
    if settings.read_only_market_data and market_runtime is None:
        from catalyst_lab.alpaca import AlpacaCredentials, AlpacaPaperClient
        from catalyst_lab.broker_runtime import BrokerMonitor
        from catalyst_lab.runtime import MarketRuntime, paper_account_identity
        from catalyst_lab.trigger import TriggerPolicy
        from catalyst_lab.watcher import Watcher

        trigger_policy = TriggerPolicy(
            settings.max_spread_bps, settings.feed_failure_tolerance_seconds
        )
        executor_lease = None
        if settings.risk_database_url:
            from catalyst_lab.authorization import AuthorizationGate, RiskRepository
            from catalyst_lab.executor_lease import AccountExecutorLease, FencedAuthorizationGate
            from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
            from catalyst_lab.risk import RiskEngine, RiskPolicy
            from catalyst_lab.risk_runtime import RiskRuntime

            risk_repo = RiskRepository(settings.risk_database_url)
            risk_repo.require_same_database(repo)
            risk_policy = RiskPolicy(
                settings.risk_authorization_ttl_seconds,
                settings.risk_baseline_source,
                settings.risk_max_per_sector,
                settings.risk_max_per_theme,
                settings.max_spread_bps,
            )
            executor_lease = AccountExecutorLease(
                risk_repo, lambda: paper_account_identity(client)
            )
            client = RiskAuthorizedPaperClient(
                AlpacaCredentials.from_env(),
                FencedAuthorizationGate(AuthorizationGate(risk_repo, clock=now), executor_lease),
                settings.market_data_feed,
            )
        else:
            client = AlpacaPaperClient(AlpacaCredentials.from_env(), settings.market_data_feed)
        monitor = BrokerMonitor(
            risk_repo if settings.risk_database_url else repo, client, clock=now
        )
        market_runtime = MarketRuntime(
            client,
            Watcher(repo, trigger_policy, feed=settings.market_data_feed),
            settings.diagnostic_symbols,
            clock=now,
            broker_monitor=monitor,
            measurements=measurements,
            executor_lease=executor_lease,
        )
        if settings.risk_database_url:
            engine = RiskEngine(
                risk_repo,
                client,
                risk_policy,
                clock=now,
                ready=lambda: (
                    monitor.ready()
                    and market_runtime.ownership_ready()
                    and market_runtime.connected
                    and market_runtime.authenticated
                    and market_runtime.error is None
                ),
            )
            risk_runtime = RiskRuntime(engine)
            monitor.reconciler.baseline_recorder = engine.capture_reconciled_baseline
            monitor.risk_runtime = risk_runtime
            monitor.ledger = risk_runtime
            market_runtime.risk_runtime = risk_runtime
        if settings.us_admission_policy:
            from catalyst_lab.us_admission import USAdmissionEvidence, admission_profile

            profile = admission_profile(settings.us_admission_policy)
            if policy is not None and policy != profile.validation_policy:
                raise ValueError("US admission policy conflicts with injected thresholds")
            policy = profile.validation_policy
            market_runtime.admission_provider = USAdmissionEvidence(
                repo,
                client,
                profile,
                ready=lambda: monitor.ready() and market_runtime.ownership_ready(),
                feed_healthy=lambda: (
                    market_runtime.connected
                    and market_runtime.authenticated
                    and market_runtime.error is None
                ),
                clock=now,
            )
            if settings.jev_paper_policy:
                from catalyst_lab.jev_paper import JevAdmissionWorker, JevPaperAdmission

                jev_admission_worker = JevAdmissionWorker(
                    JevPaperAdmission(
                        risk_repo,
                        market_runtime.admission_provider,
                        policy_id=settings.jev_paper_policy,
                        clock=now,
                    ),
                    poll_seconds=float(settings.jev_admission_poll_seconds),
                )
    if market_runtime is not None and provider is no_evidence:
        provider = market_runtime.validation_evidence

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(repo.check_role)
        try:
            if market_runtime is not None:
                await run_in_threadpool(market_runtime.start)
                measurement_worker.start()
                if jev_admission_worker:
                    jev_admission_worker.start()
            yield
        finally:
            if market_runtime is not None:
                if jev_admission_worker:
                    await run_in_threadpool(jev_admission_worker.stop)
                await run_in_threadpool(measurement_worker.stop)
                await run_in_threadpool(market_runtime.stop)

    app = FastAPI(title="Catalyst Retest Lab", version="0.1.0", lifespan=lifespan)
    app.state.repository = repo
    app.state.market_runtime = market_runtime
    bearer = HTTPBearer(auto_error=False)

    def require(scope):
        def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
            if credentials is None or not hmac.compare_digest(
                credentials.credentials.encode(), settings.muse_api_token.encode()
            ):
                raise HTTPException(401, "Invalid or missing Muse credentials")
            if scope not in MUSE_SCOPES:
                raise HTTPException(403, "Insufficient scope")

        return authorize

    @app.exception_handler(psycopg.Error)
    async def database_failure(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @app.get("/health")
    def health():
        with repo.connect() as conn:
            conn.execute("SELECT 1")
        return {
            "status": "ok",
            "alpaca": "paper",
            "strategy_version": STRATEGY_VERSION,
            "time": now().isoformat(),
            "phase": "RISK_GATED"
            if risk_runtime
            else "EXECUTION_LOCKED"
            if market_runtime
            else "LAB_CORE",
            "broker_connected": market_runtime.status()["broker_connected"]
            if market_runtime
            else False,
            "trading_enabled": risk_runtime is not None,
            "submission_mode": "RISK_DECISION_REQUIRED" if risk_runtime else "DISABLED",
            "measurement": {
                "worker_alive": bool(
                    measurement_worker.thread and measurement_worker.thread.is_alive()
                ),
                "error": measurement_worker.error,
            },
            "jev_paper_admission": {
                "policy": settings.jev_paper_policy,
                "worker_alive": bool(
                    jev_admission_worker
                    and jev_admission_worker.thread
                    and jev_admission_worker.thread.is_alive()
                ),
                "error": jev_admission_worker.error if jev_admission_worker else None,
                "cohort": "JEV_US_SELECTED_FIXED_ENGINEERING" if jev_admission_worker else None,
            },
            "market_data": market_runtime.status()
            if market_runtime
            else ("UNCONNECTED" if provider is no_evidence else "INJECTED_TEST_PROVIDER"),
        }

    @app.post(
        "/api/v1/candidates", status_code=201, dependencies=[Depends(require("candidate:create"))]
    )
    async def submit(request: Request):
        if settings.jev_paper_policy:
            # An explicitly selected Jev test deployment cannot admit an unreviewed
            # candidate through the legacy Muse route. Baseline deployments keep V1.
            with repo.connect() as conn:
                repo.append_event(
                    conn,
                    "SYSTEM_EVENT",
                    {
                        "kind": "JEV_ADMISSION_BYPASS_REFUSED",
                        "policy": settings.jev_paper_policy,
                    },
                )
            raise HTTPException(409, "REVIEWED_RESEARCH_REPORT_REQUIRED")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise HTTPException(413, "Candidate body exceeds 64 KiB")
        try:
            raw = json.loads(body, parse_constant=invalid_constant, object_pairs_hook=unique_object)
            json.dumps(raw, allow_nan=False)
            check_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "A valid finite JSON body is required") from None
        if isinstance(raw, dict) and "candidates" in raw:
            try:
                batch = BatchEnvelope.model_validate(raw)
            except ValidationError:
                raise HTTPException(
                    422,
                    "Require strategy_version, date (YYYY-MM-DD), "
                    "and 1–50 candidates; no extra envelope fields",
                ) from None
            return await run_in_threadpool(
                repo.submit_batch, batch, now(), provider, policy, decision_clock=now
            )
        return await run_in_threadpool(
            repo.submit, raw, now(), provider, policy, decision_clock=now
        )

    @app.get("/api/v1/candidates", dependencies=[Depends(require("candidate:read"))])
    def candidates(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), date: date | None = None
    ):
        session_date = date.isoformat() if date else None
        return {
            "items": repo.list_candidates(limit, offset, session_date),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/candidates/{candidate_id}", dependencies=[Depends(require("candidate:read"))])
    def candidate(candidate_id: UUID):
        record = repo.get_candidate(candidate_id)
        if not record:
            raise HTTPException(404, "Candidate not found")
        return record

    @app.get(
        "/api/v1/candidates/{candidate_id}/events",
        dependencies=[Depends(require("candidate:read"))],
    )
    def events(candidate_id: UUID):
        if not repo.get_candidate(candidate_id):
            raise HTTPException(404, "Candidate not found")
        return {"items": repo.candidate_events(candidate_id)}

    @app.get("/api/v1/analytics", dependencies=[Depends(require("analytics:read"))])
    def analytics():
        return repo.analytics()

    @app.get("/api/v1/stats", dependencies=[Depends(require("analytics:read"))])
    def stats(
        days: int | None = Query(None, ge=1, le=3650),
        version: str | None = Query(None, max_length=64),
    ):
        from catalyst_lab.analytics import Analytics

        return Analytics(repo, clock=now).stats(days=days, version=version)

    @app.get("/api/v1/trades", dependencies=[Depends(require("analytics:read"))])
    def trades(
        days: int | None = Query(None, ge=1, le=3650),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        from catalyst_lab.analytics import Analytics

        return {"items": Analytics(repo, clock=now).trades(days=days, limit=limit, offset=offset)}

    @app.get("/api/v1/market-data", dependencies=[Depends(require("analytics:read"))])
    def market_data():
        return market_runtime.status(private=True) if market_runtime else {"enabled": False}

    @app.get("/api/v1/execution", dependencies=[Depends(require("analytics:read"))])
    def execution_status():
        return {
            **repo.execution_status(),
            "trading_enabled": risk_runtime is not None,
            "submission_mode": "RISK_DECISION_REQUIRED" if risk_runtime else "DISABLED",
        }

    @app.get("/api/v1/risk", dependencies=[Depends(require("analytics:read"))])
    def risk_status():
        return (
            risk_runtime.status()
            if risk_runtime
            else {"configured": False, "submission_mode": "DISABLED"}
        )

    return app
