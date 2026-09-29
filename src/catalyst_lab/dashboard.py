"""Public read-only surface; never loads broker credentials or execution clients."""

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from catalyst_lab.analytics import Analytics
from catalyst_lab.config import REPORTING_R_METHOD
from catalyst_lab.repository import Repository, json_safe

STATIC = Path(__file__).with_name("static")


def csv_cell(value):
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


def create_dashboard_app(database_url, *, clock=None, r_method=REPORTING_R_METHOD):
    repo = Repository(database_url)
    analytics = Analytics(repo, clock=clock, r_method=r_method)

    @asynccontextmanager
    async def lifespan(app):
        with repo.connect() as conn:
            role = conn.execute("SELECT current_user AS role").fetchone()["role"]
            if role != "catalyst_reporting":
                raise RuntimeError(
                    "Public dashboard requires the dedicated read-only reporting role"
                )
        yield

    app = FastAPI(
        title="Catalyst Retest Lab — public paper dashboard",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    @app.exception_handler(psycopg.Error)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Reporting database unavailable"})

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        with repo.connect() as conn:
            conn.execute("SELECT 1 FROM lab.reporting_status")
        return {
            "status": "ok",
            "alpaca": "paper",
            "surface": "PUBLIC_READ_ONLY",
            "trading_enabled": False,
        }

    @app.get("/api/public/stats")
    def stats(
        days: int | None = Query(None, ge=1, le=3650),
        version: str | None = Query(None, max_length=64),
    ):
        return analytics.stats(days=days, version=version)

    @app.get("/api/public/candidates")
    def candidates(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        state: str | None = Query(None, max_length=40),
        version: str | None = Query(None, max_length=64),
    ):
        return analytics.log(public=True, limit=limit, offset=offset, state=state, version=version)

    @app.get("/api/public/candidates/{candidate_id}")
    def detail(candidate_id: UUID):
        result = analytics.detail(candidate_id)
        if result is None:
            raise HTTPException(404, "Published candidate not found")
        return result

    @app.get("/api/public/trades")
    def trades(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        return {"items": analytics.trades(public=True, limit=limit, offset=offset)}

    @app.get("/api/public/daily")
    def daily(limit: int = Query(90, ge=1, le=3650)):
        with repo.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT ON(session_date,strategy_version) *
                FROM lab.daily_stats WHERE market='US' AND execution_source='ALPACA_PAPER'
                ORDER BY session_date DESC,strategy_version,revision DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        return {"items": json_safe(rows)}

    @app.get("/api/public/research/{market}")
    def research(market: str, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        from catalyst_lab.research import read_research

        if market not in {"crypto", "india"}:
            raise HTTPException(404, "Research market not found")
        return read_research(repo, market.upper(), limit=limit, offset=offset)

    @app.get("/api/public/export.csv")
    def export():
        # Stream all published candidates through pages; no hidden-current rows enter exports.
        fields = [
            "session_date",
            "ticker",
            "strategy_version",
            "catalyst",
            "state",
            "reason",
            "entry_trigger",
            "max_entry_price",
            "stop",
            "target",
            "thesis",
            "disproof",
            "received_at",
            "publish_after",
            "late_added",
        ]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(fields)
        offset = 0
        while True:
            page = analytics.log(public=True, limit=200, offset=offset)
            for row in page["items"]:
                writer.writerow([csv_cell(row.get(k)) for k in fields])
            offset += len(page["items"])
            if offset >= page["total"] or not page["items"]:
                break
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=published-candidate-log.csv"},
        )

    return app
