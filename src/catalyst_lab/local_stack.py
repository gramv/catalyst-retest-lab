"""The local Docker stack (``LOCAL_STACK_V1``, package oss-packaging, 2026-10-03): the entrypoint
of every service in ``compose.yaml``. **PAPER TRADING ONLY.**

    python -m catalyst_lab.local_stack secrets      # one-shot: generate the stack's passwords
    python -m catalyst_lab.local_stack provision    # one-shot: provision the disposable ledger
    python -m catalyst_lab.local_stack trader       # the simulated venue (default) or Alpaca paper
    python -m catalyst_lab.local_stack page         # the public page (catalyst_public, read-only)
    python -m catalyst_lab.local_stack jobs         # the nightly learning jobs, once
    python -m catalyst_lab.local_stack scorecard    # strategies in shadow, and the day's scorecard
    python -m catalyst_lab.local_stack promote-strategy --strategy ID --history-report PATH \\
        --owner-ruling TEXT [--owner-override-reason TEXT]
    python -m catalyst_lab.local_stack demote-strategy --strategy ID --reason TEXT \\
        --owner-ruling TEXT
    python -m catalyst_lab.local_stack health URL   # a container healthcheck

What it guarantees:

* **A disposable ledger only.** ``provision`` runs ``cloud_provision.provision`` -- the same
  roles, grants, ownership checks and audited ``CLOUD_LEDGER_PROVISIONED`` event as the cloud --
  with the platform ``LOCAL_COMPOSE``, against the compose ``db`` service only. Every other
  component first reads that event and refuses a ledger that is not ``LOCAL_COMPOSE``
  (``LOCAL_STACK_LEDGER_NOT_LOCAL``), so pointing this stack at any other ledger fails closed.
  A ledger behind this release's schema is refused (``LOCAL_STACK_LEDGER_SCHEMA_BEHIND``): reset
  it with ``docker compose down -v`` (it is disposable) -- this stack never migrates.
* **No superuser credential in an app container.** ``secrets`` writes the Postgres superuser
  password to the admin volume (mounted only by ``db``, ``secrets`` and ``provision``) and one
  password per login role to its own folder of the roles volume; each app container mounts only
  the folders of the roles it logs in as.
* **No key by default.** The default trader is the simulated venue (``SIMULATED_VENUE_V1``): the
  strategy shadow run continuously -- every registered and drop-in mechanical strategy's signals
  on completed hourly bars, their outcomes simulated on 1-minute bars by the shared strategy
  core, recorded in the ledger -- with no broker, no order and no AI. It refuses to start when
  any ``APCA_*`` or TypeSafe variable is present (``SIMULATED_VENUE_REFUSES_CREDENTIALS``).
  ``CATALYST_SIM_BARS`` picks its bars: ``public`` (Alpaca's keyless public crypto bars; the
  default) or ``synthetic`` (``sample_market``: generated, offline, never evidence).
* **Alpaca paper only with the user's own key.** ``CATALYST_VENUE=alpaca-paper`` (the compose
  profile ``alpaca-paper``, with the user's own ``.env.paper``) runs the managed paper engine:
  the defaults of ``deploy/local/managed-defaults.json`` (``NO_AI_MODE_V1``), overridden by the
  engine settings in the environment, validated exactly as startup validates them
  (``managed_ops.environment_findings`` and ``ai_mode``), the role connections built here, the
  role tokens generated once into the trader's own secret folder. The broker key must be an
  Alpaca **paper** key on the fixed paper endpoint (``alpaca.AlpacaCredentials``); every order
  still needs the exact one-use risk authorization. ``--check`` validates and stops.

Output is JSON lines of codes, counts and names: never a password, token, key or DSN.
"""

import argparse
import http.server
import json
import os
import secrets as _secrets
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

VERSION = "LOCAL_STACK_V1"
VENUE_VERSION = "SIMULATED_VENUE_V1"
PLATFORM = "LOCAL_COMPOSE"
DATABASE = "catalyst_lab"
ADMIN_DIR_ENV, ROLES_DIR_ENV = "CATALYST_ADMIN_SECRET_DIR", "CATALYST_ROLES_SECRET_DIR"
DEFAULT_ADMIN_DIR, DEFAULT_ROLES_DIR = "/run/catalyst-admin", "/run/catalyst-roles"
ADMIN_FILE = "postgres-password"
DB_HOST_ENV, DB_PORT_ENV = "CATALYST_DB_HOST", "CATALYST_DB_PORT"
VENUE_ENV, SIM_BARS_ENV, SIM_SYMBOLS_ENV = "CATALYST_VENUE", "CATALYST_SIM_BARS", \
    "CATALYST_SIM_SYMBOLS"
SIMULATED, ALPACA_PAPER = "simulated", "alpaca-paper"
VENUES = (SIMULATED, ALPACA_PAPER)
PUBLIC_BARS, SYNTHETIC_BARS = "public", "synthetic"
DEFAULT_SIM_SYMBOLS = ("BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD")
HEALTH_PORT = 8081
PASS_OFFSET = timedelta(seconds=90)  # After the hour, as the paper path reads completed bars.
DATABASE_WAIT_SECONDS = 120
DEFAULTS_FILE = Path(__file__).resolve().parents[2] / "deploy" / "local" / "managed-defaults.json"
DEFAULTS_ENV = "CATALYST_MANAGED_DEFAULTS"
# The folder (in the roles volume) of each login role, and its password variable.
ROLE_FOLDERS = {"catalyst_risk": "risk", "catalyst_jev": "jev", "catalyst_app": "app",
                "catalyst_operator": "operator", "catalyst_backup": "backup",
                "catalyst_public": "public"}
TRADER_TOKENS = ("MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN", "MANAGED_OPERATOR_TOKEN")
TOKEN_FOLDER = "trader-tokens"
CREDENTIAL_NAMES = ("TYPESAFE_API_KEY", "TYPESAFE_ENV_FILE")

EXIT_REFUSED, EXIT_FAILED = 2, 1


class LocalStackError(RuntimeError):
    """A refusal code plus names (never a value)."""

    def __init__(self, code, names=()):
        super().__init__(code)
        self.code = code
        self.names = tuple(sorted(str(n) for n in names))


def log(component, event, **fields):
    line = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "component": component,
            "event": event, **fields}
    print(json.dumps(line, sort_keys=True, default=str), flush=True)


# --- secrets ---------------------------------------------------------------------------------


def admin_dir(environ):
    return Path(environ.get(ADMIN_DIR_ENV) or DEFAULT_ADMIN_DIR)


def roles_dir(environ):
    return Path(environ.get(ROLES_DIR_ENV) or DEFAULT_ROLES_DIR)


def _new_secret():
    return _secrets.token_urlsafe(36)  # 48 URL-safe characters (cloud_provision.PASSWORD).


def _write_new(path, value, mode):
    """Create ``path`` with ``value`` (never overwrite); return False when it already existed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    os.chmod(path, mode)
    return True


def read_secret(path):
    """A secret file's value: a regular file (never a symlink), one non-empty line."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise LocalStackError("LOCAL_STACK_SECRET_MISSING", [Path(path).name]) from None
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise LocalStackError("LOCAL_STACK_SECRET_INVALID", [Path(path).name])
        value = stream.read(4096).strip()
    if not value or any(c.isspace() for c in value):
        raise LocalStackError("LOCAL_STACK_SECRET_INVALID", [Path(path).name])
    return value


def role_password_file(environ, role):
    from catalyst_lab.cloud_provision import LOGIN_ROLES

    return roles_dir(environ) / ROLE_FOLDERS[role] / LOGIN_ROLES[role]


def generate_secrets(environ):
    """Create every missing secret file; existing ones are kept (a restart changes nothing).

    The admin password is world-readable inside its own volume (the Postgres image reads it as
    its ``postgres`` user); every role folder is 0700 and its file 0600 (the app user's)."""
    from catalyst_lab.cloud_provision import LOGIN_ROLES

    created = []
    admin = admin_dir(environ)
    admin.mkdir(parents=True, exist_ok=True)
    if _write_new(admin / ADMIN_FILE, _new_secret(), 0o644):
        created.append(ADMIN_FILE)
    for role, variable in sorted(LOGIN_ROLES.items()):
        folder = roles_dir(environ) / ROLE_FOLDERS[role]
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        if _write_new(folder / variable, _new_secret(), 0o600):
            created.append(variable)
    tokens = roles_dir(environ) / TOKEN_FOLDER
    tokens.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(tokens, 0o700)
    for name in TRADER_TOKENS:
        if _write_new(tokens / name, _new_secret(), 0o600):
            created.append(name)
    return {"result": "SECRETS_READY", "created": created,
            "kept": len(LOGIN_ROLES) + 1 + len(TRADER_TOKENS) - len(created)}


# --- connections -------------------------------------------------------------------------------


def _conninfo(environ, *, user, password, dbname):
    from psycopg.conninfo import make_conninfo

    return make_conninfo(host=environ.get(DB_HOST_ENV) or "db",
                         port=environ.get(DB_PORT_ENV) or "5432", dbname=dbname, user=user,
                         password=password, sslmode="disable", connect_timeout="10",
                         application_name="catalyst-local-stack")


def admin_url(environ, dbname="postgres"):
    password = read_secret(admin_dir(environ) / ADMIN_FILE)
    return _conninfo(environ, user="postgres", password=password, dbname=dbname)


def role_url(environ, role):
    return _conninfo(environ, user=role, password=read_secret(role_password_file(environ, role)),
                     dbname=DATABASE)


def wait_for(url, *, max_seconds=DATABASE_WAIT_SECONDS):
    from catalyst_lab.managed_ops import wait_for_database

    try:
        return wait_for_database(url, max_seconds=max_seconds)
    except ValueError:
        raise LocalStackError("LOCAL_STACK_DATABASE_UNAVAILABLE") from None


def local_identity(url):
    """The ledger's ``CLOUD_LEDGER_PROVISIONED`` identity, refused unless it is a local stack
    ledger at this release's schema."""
    import psycopg

    from catalyst_lab.cloud_provision import ProvisionError, ledger_identity
    from catalyst_lab.config import SCHEMA_VERSION

    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            identity = ledger_identity(conn)
            version = conn.execute("SELECT max(version) FROM lab.schema_migrations").fetchone()[0]
    except ProvisionError as exc:
        raise LocalStackError(exc.code) from None
    except psycopg.Error:
        raise LocalStackError("LOCAL_STACK_LEDGER_UNREADABLE") from None
    if identity.get("platform") != PLATFORM:
        raise LocalStackError("LOCAL_STACK_LEDGER_NOT_LOCAL")
    if version != SCHEMA_VERSION:
        raise LocalStackError("LOCAL_STACK_LEDGER_SCHEMA_BEHIND" if version < SCHEMA_VERSION
                              else "LOCAL_STACK_LEDGER_SCHEMA_AHEAD")
    return identity


# --- provision ---------------------------------------------------------------------------------


def provision_ledger(environ, *, provision=None, now=None):
    """Provision the disposable ledger once; afterwards check it and change nothing."""
    import psycopg

    from catalyst_lab import cloud_provision
    from catalyst_lab.cloud_provision import LOGIN_ROLES

    url = admin_url(environ)
    wait_for(url)
    passwords = {variable: read_secret(role_password_file(environ, role))
                 for role, variable in LOGIN_ROLES.items()}
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                                  (DATABASE,)).fetchone() is not None
        provisioned = False
        if exists:
            with psycopg.connect(admin_url(environ, DATABASE), connect_timeout=10) as conn:
                provisioned = conn.execute(
                    "SELECT to_regnamespace('lab') IS NOT NULL").fetchone()[0]
    except psycopg.Error:
        raise LocalStackError("LOCAL_STACK_DATABASE_UNAVAILABLE") from None
    if provisioned:
        identity = local_identity(role_url(environ, "catalyst_risk"))
        return {"result": "ALREADY_PROVISIONED", "platform": identity["platform"],
                "ledger_id": identity["ledger_id"], "schema_version": identity["schema_version"]}
    try:
        result = (provision or cloud_provision.provision)(url, passwords, now=now,
                                                         platform=PLATFORM)
    except cloud_provision.ProvisionError as exc:
        raise LocalStackError(exc.code, exc.names) from None
    return {k: result[k] for k in ("result", "schema_version", "ledger_id", "login_roles",
                                   "audit_seq")} | {"platform": PLATFORM}


# --- the simulated venue --------------------------------------------------------------------


def sim_symbols(environ, bars):
    raw = environ.get(SIM_SYMBOLS_ENV)
    if bars == SYNTHETIC_BARS and not raw:
        from catalyst_lab.sample_market import SYMBOLS

        return list(SYMBOLS)
    symbols = (sorted({s.strip().upper() for s in raw.split(",") if s.strip()}) if raw
               else list(DEFAULT_SIM_SYMBOLS))
    from catalyst_lab.history_test import universe_for

    try:
        universe_for(",".join(symbols))
    except ValueError:
        raise LocalStackError("LOCAL_STACK_SIM_SYMBOLS_INVALID", [SIM_SYMBOLS_ENV]) from None
    if bars == SYNTHETIC_BARS:
        from catalyst_lab.sample_market import COINS

        if set(symbols) - set(COINS):
            raise LocalStackError("LOCAL_STACK_SIM_SYMBOLS_INVALID", [SIM_SYMBOLS_ENV])
    return symbols


def sim_reader(environ):
    bars = environ.get(SIM_BARS_ENV) or PUBLIC_BARS
    if bars == PUBLIC_BARS:
        from catalyst_lab.public_crypto_bars import PublicCryptoBarReader
        from catalyst_lab.strategy_shadow import BAR_SOURCE

        return bars, PublicCryptoBarReader(), BAR_SOURCE
    if bars == SYNTHETIC_BARS:
        from catalyst_lab.sample_market import SOURCE_LABEL, SyntheticBarReader

        return bars, SyntheticBarReader(), SOURCE_LABEL
    raise LocalStackError("LOCAL_STACK_SIM_BARS_UNKNOWN", [SIM_BARS_ENV])


def refuse_credentials(environ, code):
    names = sorted(n for n in environ if n.startswith("APCA_") or n in CREDENTIAL_NAMES)
    if names:
        raise LocalStackError(code, names)


class Health:
    """The last pass, served as ``GET /health`` JSON (a container healthcheck reads it)."""

    def __init__(self, **fields):
        self.lock = threading.Lock()
        self.state = {"status": "STARTING", **fields}

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def serve(self, host="0.0.0.0", port=HEALTH_PORT):
        health = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 -- the stdlib's name.
                if self.path != "/health":
                    self.send_error(404)
                    return
                body = json.dumps(health.snapshot(), sort_keys=True, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def next_pass(now):
    """The next pass: ``PASS_OFFSET`` after the next full hour."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    at = hour + PASS_OFFSET
    return at if at > now else at + timedelta(hours=1)


def simulated_pass(store, reader, *, now, symbols, bar_source):
    from catalyst_lab.strategy_shadow import run_strategy_shadow

    return run_strategy_shadow(store, reader, now=now, symbols=symbols, bar_source=bar_source)


def simulated_venue(environ, *, clock=lambda: datetime.now(UTC), sleep=time.sleep,
                    passes=None, store_factory=None, serve_health=True):
    """``SIMULATED_VENUE_V1``: one shadow pass now, then one after every hour; never an order."""
    from catalyst_lab import strategies

    refuse_credentials(environ, "SIMULATED_VENUE_REFUSES_CREDENTIALS")
    bars, reader, bar_source = sim_reader(environ)
    symbols = sim_symbols(environ, bars)
    url = role_url(environ, "catalyst_risk")
    wait_for(url)
    identity = local_identity(url)
    if store_factory is None:
        from catalyst_lab.authorization import RiskRepository
        from catalyst_lab.managed_store import ManagedStore

        store = ManagedStore(RiskRepository(url))
    else:
        store = store_factory(url)
    loaded = strategies.ensure_plugins_loaded(environ)
    shadow = [s.strategy_id for s in strategies.mechanical(strategies.SHADOW)]
    health = Health(venue=VENUE_VERSION, bars=bar_source, symbols=symbols, strategies=shadow,
                    orders="NONE_SIMULATED_VENUE", ledger_id=identity["ledger_id"])
    if serve_health:
        health.serve()
    log("trader", "SIMULATED_VENUE_STARTED", venue=VENUE_VERSION, bars=bar_source,
        coins=len(symbols), strategies=",".join(shadow),
        plugin_errors=len(loaded.errors), orders="NONE")
    count = 0
    try:
        while passes is None or count < passes:
            now = clock()
            try:
                code, details = simulated_pass(store, reader, now=now, symbols=symbols,
                                               bar_source=bar_source)
            except Exception as exc:  # noqa: BLE001 -- logged as a code; the next pass retries.
                code, details = type(exc).__name__.upper(), {}
            count += 1
            health.update(status="OK" if code is None else "DEGRADED", last_pass_at=now,
                          last_code=code, passes=count, last=details)
            log("trader", "SIMULATED_PASS", code=code, **{
                k: v for k, v in details.items() if isinstance(v, (int, str))})
            if passes is not None and count >= passes:
                break
            sleep(max(1.0, (next_pass(clock()) - clock()).total_seconds()))
    finally:
        reader.close()
    return health.snapshot()


# --- the managed paper engine (the user's own Alpaca paper key) ---------------------------------


def managed_defaults(environ):
    path = Path(environ.get(DEFAULTS_ENV) or DEFAULTS_FILE)
    try:
        document = json.loads(path.read_text())
        values = document["environment"]
        if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise LocalStackError("LOCAL_STACK_MANAGED_DEFAULTS_INVALID") from None
    return values


def managed_environment(environ):
    """The engine environment: the defaults, then the engine settings the user set (their
    ``.env.paper``), the role connections and the trader's generated tokens. Validated exactly
    as startup validates it; refusals name variables only."""
    from catalyst_lab import ai_mode
    from catalyst_lab.alpaca import AlpacaCredentials
    from catalyst_lab.managed_ops import ENV_NAMES, environment_findings

    engine = dict(managed_defaults(environ))
    engine.update({k: v for k, v in environ.items() if k in ENV_NAMES})
    for name in ("MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL", *TRADER_TOKENS):
        if name in environ:  # Built here from the stack's own secrets, never supplied.
            raise LocalStackError("LOCAL_STACK_SETTING_NOT_ALLOWED", [name])
    if engine.get("CATALYST_ENVIRONMENT") == "railway":
        raise LocalStackError("LOCAL_STACK_SETTING_NOT_ALLOWED", ["CATALYST_ENVIRONMENT"])
    engine["MANAGED_DATABASE_URL"] = role_url(environ, "catalyst_risk")
    engine["JEV_WORKER_DATABASE_URL"] = role_url(environ, "catalyst_jev")
    tokens = roles_dir(environ) / TOKEN_FOLDER
    for name in TRADER_TOKENS:
        engine[name] = read_secret(tokens / name)
    try:
        ai_mode.require(engine)
    except ai_mode.AiModeError as exc:
        raise LocalStackError(exc.code, exc.names) from None
    missing, invalid = environment_findings(engine)
    if missing or invalid:
        raise LocalStackError("LOCAL_STACK_ENGINE_SETTINGS_INVALID", [*missing, *invalid])
    for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "APCA_DATA_BASE_URL"):
        if name in environ:  # The endpoints are fixed in code; none is configurable here.
            raise LocalStackError("LOCAL_STACK_SETTING_NOT_ALLOWED", [name])
    try:
        AlpacaCredentials(engine["APCA_API_KEY_ID"], engine["APCA_API_SECRET_KEY"])
    except (KeyError, ValueError):
        raise LocalStackError("ALPACA_PAPER_CREDENTIALS_INVALID",
                              ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]) from None
    return engine


def managed_summary(engine):
    from catalyst_lab import ai_mode

    return {"venue": "ALPACA_PAPER", "ai_mode": ai_mode.mode_from_env(engine),
            "risk_policy_id": engine.get("MANAGED_RISK_POLICY_ID"),
            "management_reviews": engine.get("MANAGED_MANAGEMENT_REVIEWS"),
            "strategies": engine.get("MANAGED_STRATEGIES_JSON", "[]"),
            "port": engine.get("MANAGED_HTTP_PORT")}


def alpaca_paper(environ, *, check_only=False, serve=None, build=None, process_environ=None):
    """The managed paper engine on the local ledger, bound to the container's loopback."""
    engine = managed_environment(environ)
    url = engine["MANAGED_DATABASE_URL"]
    wait_for(url)
    identity = local_identity(url)
    summary = managed_summary(engine) | {"ledger_id": identity["ledger_id"]}
    if check_only:
        return summary
    from catalyst_lab import managed_app

    # The managed app reads its settings from the process environment (tests pass a dict).
    (os.environ if process_environ is None else process_environ).update(engine)
    log("trader", "ALPACA_PAPER_STARTING", **summary)
    app, settings = (build or managed_app.build_app_from_env)(health_check_database=False)
    (serve or managed_app.serve)(app, settings, host="127.0.0.1")
    return summary


def trader(environ, *, check_only=False):
    venue = environ.get(VENUE_ENV) or SIMULATED
    if venue not in VENUES:
        raise LocalStackError("LOCAL_STACK_VENUE_UNKNOWN", [VENUE_ENV])
    if venue == ALPACA_PAPER:
        return alpaca_paper(environ, check_only=check_only)
    if check_only:
        refuse_credentials(environ, "SIMULATED_VENUE_REFUSES_CREDENTIALS")
        bars, reader, source = sim_reader(environ)
        reader.close()
        return {"venue": VENUE_VERSION, "bars": source, "symbols": sim_symbols(environ, bars)}
    return simulated_venue(environ)


# --- page, jobs and the owner's commands --------------------------------------------------------


def page(environ, argv=()):
    from catalyst_lab import experiment_page

    refuse_credentials(environ, "PAGE_REFUSES_TRADING_CREDENTIALS")
    url = role_url(environ, "catalyst_public")
    wait_for(url)
    child = {k: v for k, v in environ.items() if k not in (ADMIN_DIR_ENV,)}
    child["EXPERIMENT_DATABASE_URL"] = url
    child.setdefault("EXPERIMENT_TITLE", "Catalyst Lab - local paper stack")
    experiment_page.main(["--host", "0.0.0.0", *argv], environ=child)


def jobs(environ, *, now=None):
    from catalyst_lab.cloud_runtime import jobs_store
    from catalyst_lab.learning_jobs import run_jobs

    refuse_credentials(environ, "JOBS_REFUSE_TRADING_CREDENTIALS")
    url = role_url(environ, "catalyst_risk")
    wait_for(url)
    local_identity(url)
    _, reader, _ = sim_reader(environ)
    try:
        results = run_jobs(jobs_store(url), reader, now=now or datetime.now(UTC),
                           on_result=lambda r: log("jobs", "STEP", step=r.name, result=r.result,
                                                   code=r.code))
    finally:
        reader.close()
    failed = [r.name for r in results if r.result != "OK"]
    log("jobs", "DONE", failed=len(failed), steps=len(results))
    return results


def scorecard(environ, *, now=None, out=None):
    """The strategies' shadow cells (the ladder's shadow rung) and yesterday's scorecard."""
    from catalyst_lab import strategies
    from catalyst_lab.cloud_runtime import scorecard as cloud_scorecard
    from catalyst_lab.managed_ops import readonly_audit_repository
    from catalyst_lab.strategy_shadow import shadow_cells

    out = out or sys.stdout
    url = role_url(environ, "catalyst_app")
    wait_for(url)
    local_identity(url)
    strategies.ensure_plugins_loaded(environ)
    repo = readonly_audit_repository(url)
    now = now or datetime.now(UTC)
    cells = shadow_cells(repo, end=now)
    print("Strategies in shadow (simulated fills; no order was placed):", file=out)
    print(json.dumps(cells, indent=1, sort_keys=True, default=str), file=out)
    return cloud_scorecard(environ=environ, out=out, repository=repo, now=now)


def strategy_ladder(environ, args, *, out=None, now=None):
    """The owner's promotion or demotion of a strategy on the local ledger (``LOCAL_OWNER_CLI``)."""
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.managed_store import ManagedStore
    from catalyst_lab.strategy_paper import PromotionRefused, demote, promote

    out = out or sys.stdout
    url = role_url(environ, "catalyst_risk")
    wait_for(url)
    local_identity(url)
    store = ManagedStore(RiskRepository(url))
    stamp = now or datetime.now(UTC)
    try:
        if args.command == "promote-strategy":
            result = promote(store, args.strategy, history_report=Path(args.history_report),
                             owner_ruling_ref=args.owner_ruling,
                             owner_override_reason=args.owner_override_reason,
                             operator="LOCAL_OWNER_CLI", now=stamp)
        else:
            result = demote(store, args.strategy, reason=args.reason,
                            owner_ruling_ref=args.owner_ruling, operator="LOCAL_OWNER_CLI",
                            now=stamp)
    except PromotionRefused as exc:
        if exc.evidence:
            print(json.dumps(exc.evidence, sort_keys=True, default=str), file=out)
        raise LocalStackError(exc.code) from None
    print(json.dumps(result, sort_keys=True), file=out)
    return result


def health(url):
    """A container healthcheck: 0 when ``url`` answers 200 within 5 seconds."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 -- local URL.
            return 0 if response.status == 200 else 1
    except Exception:  # noqa: BLE001 -- unhealthy, whatever the reason.
        return 1


# --- the command line --------------------------------------------------------------------------


def parser():
    p = argparse.ArgumentParser(prog="python -m catalyst_lab.local_stack",
                                description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("secrets")
    sub.add_parser("provision")
    t = sub.add_parser("trader")
    t.add_argument("--check", action="store_true", help="validate the configuration and stop")
    sub.add_parser("page")
    sub.add_parser("jobs")
    sub.add_parser("scorecard")
    pr = sub.add_parser("promote-strategy")
    pr.add_argument("--strategy", required=True)
    pr.add_argument("--history-report", required=True)
    pr.add_argument("--owner-ruling", required=True)
    pr.add_argument("--owner-override-reason")
    de = sub.add_parser("demote-strategy")
    de.add_argument("--strategy", required=True)
    de.add_argument("--reason", required=True)
    de.add_argument("--owner-ruling", required=True)
    h = sub.add_parser("health")
    h.add_argument("url")
    return p


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    args = parser().parse_args(argv)
    try:
        if args.command == "health":
            raise SystemExit(health(args.url))
        if args.command == "secrets":
            result = generate_secrets(environ)
            log("secrets", result.pop("result"), **result)
        elif args.command == "provision":
            log("provision", "LEDGER_READY", **provision_ledger(environ))
        elif args.command == "trader":
            result = trader(environ, check_only=args.check)
            if args.check:
                log("trader", "CONFIGURATION_VALID", **result)
        elif args.command == "page":
            page(environ)
        elif args.command == "jobs":
            results = jobs(environ)
            raise SystemExit(EXIT_FAILED if any(r.result != "OK" for r in results) else 0)
        elif args.command == "scorecard":
            raise SystemExit(scorecard(environ))
        else:
            strategy_ladder(environ, args)
    except LocalStackError as exc:
        log(args.command, "REFUSED", code=exc.code, names=",".join(exc.names))
        raise SystemExit(EXIT_REFUSED) from None


if __name__ == "__main__":
    main()
