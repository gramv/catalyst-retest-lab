"""Public live dashboard: a read-only web service over the ``catalyst_public`` views.

    python -m catalyst_lab.experiment_page --host 0.0.0.0     # port: $PORT, else 8080

Configuration (environment):

* ``EXPERIMENT_DATABASE_URL`` (required): a PostgreSQL URL for the ``catalyst_public`` role of
  migration 024, which can read only the four sanitized ``lab.public_dashboard_*`` views.
* ``EXPERIMENT_TITLE`` (optional): the page title; default "AI crypto trading - live".
* ``EXPERIMENT_RESEARCH_SCHEDULE_JSON`` (optional): the research schedule, in the engine's
  ``MANAGED_RESEARCH_SCHEDULE_JSON`` format, for the "next run" figure; default daily 08:00
  America/New_York (the owner's decision of 2026-09-26).

Paper only and read-only. The service holds no broker or Jev/TypeSafe key, never calls an
exchange, and refuses to start when any ``APCA_*`` variable or ``TYPESAFE_API_KEY`` is present
(defense in depth). Every database session is read-only, and the service refuses a login that
is not ``catalyst_public`` or that can read a base table. The JSON is cached for
``CACHE_SECONDS``; the page's own script (the only script the Content-Security-Policy allows)
polls it every five seconds. For an opened trade's price chart the reader's browser, not this
service, reads public 5-minute bars from ``PRICE_HISTORY_ORIGIN`` without any key; that is the
one other origin the policy's ``connect-src`` allows. Fonts (IBM Plex, SIL OFL) are served here.

``fixture_data=True`` (a ``create_experiment_app`` argument only; no environment variable or
command-line flag reaches it) adds a "FIXTURE DATA - not real results" banner to the page and
the JSON. Tests and screenshots set it; production never can.
"""

import argparse
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from psycopg.rows import dict_row

from catalyst_lab.experiment_html import render_page
from catalyst_lab.experiment_report import DASHBOARD_VERSION, build_dashboard, read_snapshot

ROLE = "catalyst_public"
CACHE_SECONDS = 5
GZIP_MIN_BYTES = 1024
STALE_LIMIT_SECONDS = 300
DEFAULT_PORT = 8080
SCHEDULE_ENV = "EXPERIMENT_RESEARCH_SCHEDULE_JSON"
STATIC = Path(__file__).with_name("static")
FONTS = STATIC / "fonts"  # IBM Plex Sans and Mono (SIL Open Font License 1.1, OFL.txt).
FONT_FILES = {"IBMPlexSans-Regular.woff2", "IBMPlexSans-Medium.woff2",
              "IBMPlexSans-SemiBold.woff2", "IBMPlexMono-Regular.woff2",
              "IBMPlexMono-Medium.woff2"}
# The one other origin the page's script may read: public 5-minute crypto bars for a trade's
# price chart, requested by the reader's browser without any key. This service calls nothing.
PRICE_HISTORY_ORIGIN = "https://data.alpaca.markets"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       f"font-src 'self'; connect-src 'self' {PRICE_HISTORY_ORIGIN}; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
}
SECTIONS = ("status", "overall", "today", "live_trades", "agents", "feed", "past")
ROLE_CHECK = """SELECT current_user::text AS role, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
    r.rolreplication, r.rolbypassrls,
    has_table_privilege('lab.managed_events', 'SELECT') AS reads_base_table,
    has_schema_privilege('lab', 'CREATE') AS creates_in_lab
    FROM pg_roles r WHERE r.rolname = current_user"""


class ExperimentRefused(RuntimeError):
    """A refusal code; ``names`` lists environment variable names only, never a value."""

    def __init__(self, code, names=()):
        self.code, self.names = code, tuple(names)
        super().__init__(code + (": " + ", ".join(self.names) if self.names else ""))


def refuse_trading_credentials(environ):
    """Refuse when a broker (``APCA_*``) or Jev/TypeSafe (``TYPESAFE_API_KEY``) variable is set."""
    names = sorted(n for n in environ if n.startswith("APCA_") or n == "TYPESAFE_API_KEY")
    if names:
        raise ExperimentRefused("EXPERIMENT_REFUSES_TRADING_CREDENTIALS", names)


def connect(database_url):
    return psycopg.connect(
        database_url, row_factory=dict_row, connect_timeout=5,
        options="-c timezone=UTC -c statement_timeout=15000 -c default_transaction_read_only=on",
    )


def verify_role(conn):
    """The login must be ``catalyst_public`` and unable to read a base table or create."""
    row = conn.execute(ROLE_CHECK).fetchone()
    broad = [k for k in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication",
                         "rolbypassrls", "reads_base_table", "creates_in_lab") if row[k]]
    if row["role"] != ROLE or broad:
        raise ExperimentRefused("EXPERIMENT_REQUIRES_CATALYST_PUBLIC_ROLE")


class ReportCache:
    """The last built page and report; rebuilt at most once per ``seconds``. A failed rebuild
    serves the previous build, marked stale, for at most ``STALE_LIMIT_SECONDS``."""

    def __init__(self, build, seconds, *, monotonic=time.monotonic):
        self.build, self.seconds, self.monotonic = build, seconds, monotonic
        self.lock = threading.Lock()
        self.value, self.built_at, self.builds = None, None, 0

    def get(self):
        with self.lock:
            now = self.monotonic()
            if self.value is not None and now - self.built_at < self.seconds:
                return self.value
            try:
                value = self.build()
            except (psycopg.Error, ExperimentRefused, OSError):
                if self.value is None or now - self.built_at > STALE_LIMIT_SECONDS:
                    raise
                return {**self.value, "stale": True}
            self.value, self.built_at, self.builds = value, now, self.builds + 1
            return value


def create_experiment_app(database_url, *, title=None, fixture_data=False, schedule=None,
                          cache_seconds=CACHE_SECONDS, environ=None, clock=None,
                          monotonic=time.monotonic):
    """The public dashboard app. ``fixture_data`` is for tests and screenshots only;
    ``schedule`` is a ``research_schedule.ResearchSchedule`` or its constructor keywords."""
    refuse_trading_credentials(os.environ if environ is None else environ)
    if not isinstance(database_url, str) or not database_url.strip():
        raise ExperimentRefused("EXPERIMENT_DATABASE_URL_REQUIRED")
    clock = clock or (lambda: datetime.now(UTC))

    def build():
        with connect(database_url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            verify_role(conn)
            snapshot = read_snapshot(conn)
        document = build_dashboard(snapshot, title=title, fixture_data=fixture_data,
                                   schedule=schedule)
        return {"document": document, "html": render_page(document), "stale": False}

    cache = ReportCache(build, cache_seconds, monotonic=monotonic)

    @asynccontextmanager
    async def lifespan(app):
        # The wrong login refuses to start. Any database error (unreachable, or a ledger not yet
        # at schema 24) only answers 503 until it is fixed: every build checks the login again.
        try:
            with connect(database_url) as conn:
                verify_role(conn)
        except psycopg.Error:
            pass
        yield

    app = FastAPI(title="Public experiment page", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.cache = cache
    # The page polls the whole document every 5 s, trade events included: compressed ~7:1.
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(psycopg.Error)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Experiment data unavailable"})

    @app.exception_handler(ExperimentRefused)
    async def refused(request, exc):
        return JSONResponse(status_code=503, content={"detail": exc.code})

    cached = {"Cache-Control": f"public, max-age={cache_seconds}"}

    def payload(value, body):
        extra = {"served_at": clock().isoformat()}
        if value.get("stale"):
            extra["stale"] = True
        if fixture_data:
            extra["fixture_data"] = True
            extra["data_label"] = value["document"]["data_label"]
        return {**body, **extra}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(cache.get()["html"], headers=cached)

    @app.get("/experiment.css")
    def stylesheet():
        return FileResponse(STATIC / "experiment.css", media_type="text/css",
                            headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/experiment.js")
    def script():
        return FileResponse(STATIC / "experiment.js", media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=300"})

    @app.get("/favicon.svg")
    def icon():
        return FileResponse(STATIC / "experiment-icon.svg", media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/fonts/{name}")
    def font(name: str):
        if name == "OFL.txt":
            return FileResponse(FONTS / name, media_type="text/plain; charset=utf-8",
                                headers={"Cache-Control": "public, max-age=86400"})
        if name not in FONT_FILES:
            raise HTTPException(404, "Unknown font")
        return FileResponse(FONTS / name, media_type="font/woff2",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/health")
    def health():
        with connect(database_url) as conn:
            verify_role(conn)
            conn.execute("SELECT as_of FROM lab.public_dashboard_status").fetchone()
        return {"status": "ok", "surface": "PUBLIC_EXPERIMENT_READ_ONLY", "role": ROLE,
                "paper_only": True, "trading_enabled": False,
                "dashboard_version": DASHBOARD_VERSION, "fixture_data": bool(fixture_data)}

    @app.get("/api/public/experiment")
    def everything():
        value = cache.get()
        return JSONResponse(payload(value, value["document"]), headers=cached)

    @app.get("/api/public/experiment/{section}")
    def section(section: str):
        if section not in SECTIONS:
            raise HTTPException(404, "Unknown section")
        value = cache.get()
        document = value["document"]
        body = {"dashboard_version": document["dashboard_version"],
                "as_of": document["as_of"], section: document[section]}
        return JSONResponse(payload(value, body), headers=cached)

    return app


def parse_args(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    raw_port = environ.get("PORT")
    try:
        default_port = int(raw_port) if raw_port not in (None, "") else DEFAULT_PORT
    except ValueError:
        raise SystemExit("PORT must be a whole number") from None
    parser = argparse.ArgumentParser(description="Public, read-only experiment page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port,
                        help="default: $PORT when set, else 8080")
    return parser.parse_args(argv)


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    try:
        refuse_trading_credentials(environ)
    except ExperimentRefused as exc:
        print(f"Refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    args = parse_args(argv, environ)
    url = environ.get("EXPERIMENT_DATABASE_URL", "")
    if not url.strip():
        print("Refusing to start: EXPERIMENT_DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(2)
    schedule = None
    if environ.get(SCHEDULE_ENV):
        from catalyst_lab.research_schedule import ResearchSchedule

        try:
            schedule = ResearchSchedule.from_json(environ[SCHEDULE_ENV])
        except ValueError:
            print(f"Refusing to start: {SCHEDULE_ENV} is not a valid research schedule",
                  file=sys.stderr)
            raise SystemExit(2) from None
    app = create_experiment_app(url, title=environ.get("EXPERIMENT_TITLE"), schedule=schedule,
                                environ=environ)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips="*", server_header=False)


if __name__ == "__main__":
    main()
