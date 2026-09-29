"""Railway Step 4 storage/observation API. No loops, model calls or broker imports."""

import hmac
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from catalyst_lab.jev_contract import strict_json
from catalyst_lab.jev_secrets import CredentialUnavailable, typesafe_key
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import ReviewConfigurationError, ReviewSettings
from catalyst_lab.review_storage import ReviewStorage


def create_review_app(settings=None, *, credential_provider=typesafe_key):
    settings = settings or ReviewSettings.from_env()
    storage = ReviewStorage(settings.database_url)
    reports = ResearchReports(settings.database_url, settings.gate1)

    @asynccontextmanager
    async def lifespan(app):
        key = credential_provider()
        if not key or not isinstance(key, str) or any(c.isspace() for c in key):
            raise CredentialUnavailable("MISSING_TYPESAFE_CREDENTIAL")
        del key
        await run_in_threadpool(storage.check_role)
        yield

    app = FastAPI(title="Catalyst review storage — PAPER ONLY", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def review_screen():
        # Public shell contains no data or embedded credentials. Every data route needs auth.
        return FileResponse(Path(__file__).with_name("static") / "review.html")

    @app.get("/review-assets/{name}", include_in_schema=False)
    def review_asset(name: str):
        if name not in {"review.js", "review.css", "dashboard.css"}:
            raise HTTPException(404, "ASSET_NOT_FOUND")
        return FileResponse(Path(__file__).with_name("static") / name)

    def auth(token):
        def check(value: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
            if value is None or not hmac.compare_digest(value.credentials.encode(), token.encode()):
                raise HTTPException(401, "INVALID_REVIEW_CREDENTIALS")

        return check

    write = auth(settings.write_token)
    read = auth(settings.read_token)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid(request, exc):
        # Pydantic's default error body includes the offending input. Never echo evidence/secrets.
        return JSONResponse(status_code=422, content={"detail": "INVALID_REVIEW_PAYLOAD"})

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        if isinstance(exc, psycopg.errors.UniqueViolation):
            return JSONResponse(status_code=409, content={"detail": "DUPLICATE_REVIEW_RECORD"})
        if isinstance(exc, (psycopg.errors.RaiseException, psycopg.IntegrityError)):
            return JSONResponse(
                status_code=409, content={"detail": "REVIEW_BINDING_OR_CONTEXT_REJECTED"}
            )
        return JSONResponse(status_code=503, content={"detail": "REVIEW_DATABASE_UNAVAILABLE"})

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse(status_code=422, content={"detail": "INVALID_REVIEW_PAYLOAD"})

    async def body(request, *, limit=16384):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, "REVIEW_PAYLOAD_TOO_LARGE")
        return strict_json(bytes(data))

    @app.get("/health")
    def health():
        storage.check_role()
        return {
            "status": "ok",
            "alpaca": "paper",
            "strategy_version": "CATALYST_RETEST_V1",
            "surface": "RESEARCH_STORAGE_ONLY",
            "research_report_review": "SEPARATE_APP_WORKER_TEST_ONLY",
            "trading_enabled": False,
            "judgments_authorize_entry": False,
            "intent_consumer": False,
            "worker_loop": False,
            "provider_access": "NOT_PROBED",
            "gate1_policy": settings.gate1.values["version"],
            "time": datetime.now(UTC).isoformat(),
        }

    @app.post("/api/v1/evidence-bundles", dependencies=[Depends(write)], status_code=201)
    async def evidence_post(request: Request):
        return await run_in_threadpool(storage.store_evidence, await body(request), settings)

    @app.get("/api/v1/evidence-bundles/{bundle_hash}", dependencies=[Depends(read)])
    def evidence_get(bundle_hash: str):
        row = storage.evidence(bundle_hash)
        if row is None:
            raise HTTPException(404, "EVIDENCE_NOT_FOUND")
        return row

    @app.post("/api/v1/entry-intents", dependencies=[Depends(write)], status_code=201)
    async def intent_post(request: Request):
        return await run_in_threadpool(storage.store_intent, await body(request))

    @app.get("/api/v1/review-observations", dependencies=[Depends(read)])
    def observations(
        cohort: Literal["JEV_ACTIVE_V1", "JEV_ENGINEERING_TEST"],
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        return storage.observations(cohort=cohort, after_seq=after_seq, limit=limit)

    @app.post("/api/v1/research-reports", dependencies=[Depends(write)], status_code=201)
    async def reports_post(request: Request):
        result = await run_in_threadpool(reports.submit, await body(request, limit=1048576))
        if not result["accepted"]:
            return JSONResponse(status_code=409, content=result)
        return result

    @app.get("/api/v1/research-reports/{report_id}", dependencies=[Depends(read)])
    def reports_get(report_id: UUID, revision: Annotated[int | None, Query(ge=1)] = None):
        result = reports.report(report_id, revision)
        if result is None:
            raise HTTPException(404, "RESEARCH_REPORT_NOT_FOUND")
        return result

    @app.get("/api/v1/research-reports", dependencies=[Depends(read)])
    def reports_index(
        market: Literal["US_STOCKS", "CRYPTO", "INDIA"],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        return reports.index(market, limit)

    @app.get("/api/v1/research-output", dependencies=[Depends(read)])
    def output_events(
        after_seq: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=200)] = 100
    ):
        return reports.outputs(after_seq, limit)

    @app.get("/api/v1/review-workers", dependencies=[Depends(read)])
    def worker_status():
        return reports.workers()

    return app


def main():
    import uvicorn

    try:
        settings = ReviewSettings.from_env()
        ReviewStorage(settings.database_url).check_role()
    except (ReviewConfigurationError, CredentialUnavailable) as exc:
        # These exceptions contain only our fixed machine-readable codes.
        raise SystemExit(f"Review startup refused: {exc}") from None
    except Exception:
        raise SystemExit("Review startup refused: DATABASE_OR_ROLE_UNAVAILABLE") from None
    uvicorn.run(
        create_review_app(settings), host="0.0.0.0", port=int(os.environ.get("PORT", "8080"))
    )


if __name__ == "__main__":
    main()
