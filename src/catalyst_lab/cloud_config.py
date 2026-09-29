"""Railway runtime profile (``CATALYST_ENVIRONMENT=railway``, package cloud, 2026-09-27).

Configuration comes only from environment variables. The engine settings keep the names of the
private config v2 ``environment`` map and are validated by the same checks
(``managed_ops.environment_findings``); the secrets are separate variables (Railway service
variables, sealed where possible). There is no private config file, no token file, no ``.env``
and no macOS Keychain: startup refuses a present ``.env``, ``TYPESAFE_ENV_FILE``, a missing secret,
a placeholder, an unknown engine-looking variable and state that is not on the Railway volume.

Refusals are ``CloudConfigError`` with a code and, at most, variable *names*; never a value.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from catalyst_lab.agent_identity import validated_agent_tokens
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.managed_ops import ENV_NAMES, environment_findings, package_directory

RAILWAY = "railway"
ENVIRONMENT = "CATALYST_ENVIRONMENT"
STATE_DIR = "CATALYST_STATE_DIR"
DATABASE_NAME = "catalyst_lab"
ENGINE_PREFIXES = ("MANAGED_", "JEV_", "APCA_", "ALPACA_", "TYPESAFE_", "CLOUD_", "NOTIFY_")
ROLE_TOKENS = ("MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN", "MANAGED_OPERATOR_TOKEN")
AGENT_TOKENS = "MANAGED_AGENT_TOKENS_JSON"
# The deploy example sets these optional engine settings; in the cloud they are required, so a
# dropped or misspelled variable can never silently fall back to another selection rule or
# policy default.
# MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON is deliberately not required: owner decision 2026-09-25
# (CONTRACT-RESOLUTIONS, "crypto liquidity gate not applied on the Alpaca paper venue") leaves it
# out of the paper configuration, because Alpaca's thin crypto venue fails its volume rule for
# every coin while paper fills do not depend on that volume. The first Railway deploy required it
# (copied from the deploy example) and refused ARB and SUSHI entries (2026-09-28).
CLOUD_REQUIRED_OPTIONAL = frozenset({
    "MANAGED_CRYPTO_DAY_POLICY_JSON", "MANAGED_MONITOR_TRIGGER_POLICY_JSON",
    "MANAGED_RESEARCH_SCHEDULE_JSON",
    "MANAGED_SELECTION_RULE", "MANAGED_TOPK_SELECTION_JSON",
    # The trade window (package review-window): a dropped variable would silently fall back to
    # the 24-hour versions.
    "MANAGED_CRYPTO_WINDOW_JSON",
    # The monthly Jev budget and its price estimate (JEV_SPEND_GUARD_V1, package jev-budget):
    # the cloud never falls back to the local defaults.
    "JEV_MONTHLY_BUDGET_USD", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD", "JEV_BYTES_PER_TOKEN",
})
TRADER_SECRETS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY", *ROLE_TOKENS,
                  AGENT_TOKENS, "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL")
# Role passwords the Railway DSN templates read (``.railway/railway.ts``): allowed, never used
# directly by the code.
TRADER_PASSWORDS = frozenset({"RISK_DATABASE_PASSWORD", "JEV_DATABASE_PASSWORD"})
OPS_PASSWORDS = frozenset({"APP_DATABASE_PASSWORD", "BACKUP_DATABASE_PASSWORD",
                           "OPERATOR_DATABASE_PASSWORD", "PUBLIC_DATABASE_PASSWORD"})
ALPACA_ENDPOINT_NAMES = frozenset({"APCA_API_BASE_URL", "ALPACA_BASE_URL", "APCA_DATA_BASE_URL"})
TRADER_NAMES = (ENV_NAMES | set(ROLE_TOKENS) | {AGENT_TOKENS} | TRADER_PASSWORDS
                | ALPACA_ENDPOINT_NAMES) - {"TYPESAFE_ENV_FILE"}
OPS_REQUIRED = ("MANAGED_STATUS_TOKEN", "MANAGED_STATUS_API", "AUDIT_DATABASE_URL",
                "BACKUP_DATABASE_URL")
OPS_OPTIONAL = ("OPERATOR_DATABASE_URL", "CLOUD_BACKUP_RETENTION_DAYS", "CLOUD_NOTIFY_JSON",
                "NOTIFY_PING_URL")
OPS_NAMES = frozenset(OPS_REQUIRED) | frozenset(OPS_OPTIONAL) | OPS_PASSWORDS
# The ops service never holds broker, Jev or trader credentials (least privilege).
TRADING_SECRETS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY",
                   "MANAGED_API_TOKEN", "MANAGED_OPERATOR_TOKEN", AGENT_TOKENS,
                   "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL", *sorted(TRADER_PASSWORDS))
# The nightly jobs service (package learning-app): only the ledger connection it records the
# learning events through (the trader's catalyst_risk role, by reference); never a broker key,
# the Jev key, a role or agent token, the Jev worker's connection or another role's password.
JOBS_NAMES = frozenset({"MANAGED_DATABASE_URL"})
JOBS_FORBIDDEN = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY",
                  *ROLE_TOKENS, AGENT_TOKENS, "JEV_WORKER_DATABASE_URL",
                  *sorted(TRADER_PASSWORDS), *sorted(OPS_PASSWORDS), "AUDIT_DATABASE_URL",
                  "BACKUP_DATABASE_URL", "OPERATOR_DATABASE_URL")
ROLE_OF = {"MANAGED_DATABASE_URL": "catalyst_risk", "JEV_WORKER_DATABASE_URL": "catalyst_jev",
           "AUDIT_DATABASE_URL": "catalyst_app", "BACKUP_DATABASE_URL": "catalyst_backup",
           "OPERATOR_DATABASE_URL": "catalyst_operator"}
SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
PRIVATE_HOST = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.railway\.internal")
PUBLIC_BIND = "::"  # Dual stack: Railway's private network is IPv6-only in legacy environments.
LOOPBACK = "127.0.0.1"
NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class CloudConfigError(ValueError):
    """A refusal code plus the variable names involved (names are not secret)."""

    def __init__(self, code, names=()):
        super().__init__(code)
        self.code = code
        self.names = tuple(sorted(n for n in names if isinstance(n, str) and NAME.fullmatch(n)))

    def message(self):
        return self.code + (" " + ",".join(self.names) if self.names else "")


@dataclass(frozen=True)
class CloudRuntime:
    """What every cloud component shares: where it is, where its state lives, how it binds."""

    on_railway: bool
    state_dir: Path
    bind_host: str


@dataclass(frozen=True)
class TraderConfig:
    runtime: CloudRuntime
    port: int
    database_url: str = field(repr=False)
    database_wait_seconds: int = 300


@dataclass(frozen=True)
class JobsConfig:
    """The nightly jobs service: where it runs and its one ledger connection (no state)."""

    on_railway: bool
    database_url: str = field(repr=False)


@dataclass(frozen=True)
class OpsConfig:
    runtime: CloudRuntime
    status_api: str
    status_token: str = field(repr=False)
    audit_database_url: str = field(repr=False)
    backup_database_url: str = field(repr=False)
    operator_database_url: str | None = field(default=None, repr=False)
    backup_retention_days: int = 14
    notify: dict = field(default_factory=dict)
    notify_ping_url: str | None = field(default=None, repr=False)


def _placeholder(value):
    return isinstance(value, str) and (value.startswith("REQUIRED") or "/REQUIRED/" in value)


def refuse_local_secret_sources(environ, *, cwd=None, release_root=None):
    """No ``.env`` in the working directory or the image, and no local key-file setting."""
    if "TYPESAFE_ENV_FILE" in environ:
        raise CloudConfigError("TYPESAFE_ENV_FILE_FORBIDDEN", ["TYPESAFE_ENV_FILE"])
    roots = {Path(cwd) if cwd is not None else Path.cwd()}
    roots.add(Path(release_root) if release_root is not None else package_directory().parents[1])
    for root in roots:
        if os.path.lexists(root / ".env"):
            raise CloudConfigError("DOTENV_PRESENT")


def cloud_runtime(environ, *, cwd=None, release_root=None):
    """Railway mode, no local secret source, and state on the Railway volume when on Railway."""
    if environ.get(ENVIRONMENT) != RAILWAY:
        raise CloudConfigError("RAILWAY_MODE_REQUIRED", [ENVIRONMENT])
    refuse_local_secret_sources(environ, cwd=cwd, release_root=release_root)
    on_railway = bool(environ.get("RAILWAY_ENVIRONMENT_ID"))
    raw = environ.get(STATE_DIR, "")
    if not raw or _placeholder(raw) or not Path(raw).is_absolute() or "\x00" in raw:
        raise CloudConfigError("CLOUD_STATE_DIR_REQUIRED", [STATE_DIR])
    state = Path(os.path.normpath(raw))
    volume = environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if on_railway and not volume:
        # Crash-loop state and (for the trader) Railway's stop-before-start rule need a volume.
        raise CloudConfigError("CLOUD_VOLUME_REQUIRED", [STATE_DIR])
    if volume and not state.is_relative_to(Path(os.path.normpath(volume))):
        raise CloudConfigError("CLOUD_STATE_DIR_NOT_ON_VOLUME", [STATE_DIR])
    # Only a process that is actually on Railway listens beyond loopback.
    return CloudRuntime(on_railway, state, PUBLIC_BIND if on_railway else LOOPBACK)


def unknown_names(environ, allowed):
    return sorted(name for name in environ
                  if name.startswith(ENGINE_PREFIXES) and name not in allowed)


def database_url(environ, name, *, on_railway):
    """A role DSN: the expected restricted role, a password, the ledger database, SSL off-box."""
    value = environ.get(name, "")
    if not value or _placeholder(value):
        raise CloudConfigError("CLOUD_SECRET_MISSING", [name])
    from psycopg.conninfo import conninfo_to_dict

    try:
        parts = conninfo_to_dict(value)
    except Exception:
        raise CloudConfigError("CLOUD_DATABASE_URL_INVALID", [name]) from None
    host = parts.get("host") or ""
    if (parts.get("user") != ROLE_OF[name] or not parts.get("password")
            or parts.get("dbname") != DATABASE_NAME or not host):
        raise CloudConfigError("CLOUD_DATABASE_URL_INVALID", [name])
    if (on_railway or not host.startswith("/")) and parts.get("sslmode") not in SSL_MODES:
        raise CloudConfigError("CLOUD_DATABASE_SSL_REQUIRED", [name])
    return value


def _token(environ, name):
    value = environ.get(name, "")
    if not value or _placeholder(value):
        raise CloudConfigError("CLOUD_SECRET_MISSING", [name])
    if len(value) < 32 or any(c.isspace() for c in value):
        raise CloudConfigError("CLOUD_TOKEN_INVALID", [name])
    return value


def trader_config(environ, *, cwd=None, release_root=None):
    """Validate the whole trader environment before anything connects anywhere."""
    runtime = cloud_runtime(environ, cwd=cwd, release_root=release_root)
    unknown = unknown_names(environ, TRADER_NAMES)
    if unknown:
        raise CloudConfigError("CLOUD_UNKNOWN_ENGINE_VARIABLE", unknown)
    missing = [name for name in TRADER_SECRETS
               if not environ.get(name) or _placeholder(environ[name])]
    if missing:
        raise CloudConfigError("CLOUD_SECRET_MISSING", missing)
    tokens = [_token(environ, name) for name in ROLE_TOKENS]
    if len(set(tokens)) != len(tokens):
        raise CloudConfigError("ROLE_TOKENS_NOT_SEPARATED", ROLE_TOKENS)
    try:
        agents = validated_agent_tokens(strict_json(environ[AGENT_TOKENS]), reserved=tokens)
    except (TypeError, ValueError):
        raise CloudConfigError("AGENT_TOKENS_INVALID", [AGENT_TOKENS]) from None
    if not agents:
        raise CloudConfigError("AGENT_TOKENS_INVALID", [AGENT_TOKENS])
    key = environ["TYPESAFE_API_KEY"]
    if any(c.isspace() for c in key):
        raise CloudConfigError("CLOUD_SECRET_INVALID", ["TYPESAFE_API_KEY"])
    from catalyst_lab.alpaca import AlpacaCredentials

    try:  # The paper-only key prefix and the fixed paper endpoints, exactly as startup checks.
        _paper_credentials(environ, AlpacaCredentials)
    except ValueError:
        raise CloudConfigError("ALPACA_PAPER_CREDENTIALS_INVALID",
                               ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]) from None
    for name in ("MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL"):
        database_url(environ, name, on_railway=runtime.on_railway)
    engine = {name: environ[name] for name in ENV_NAMES if name in environ}
    absent, invalid = environment_findings(engine)
    absent = sorted(set(absent) | {name for name in CLOUD_REQUIRED_OPTIONAL
                                   if not engine.get(name) or _placeholder(engine[name])})
    if absent:
        raise CloudConfigError("CLOUD_ENGINE_SETTING_MISSING", absent)
    if invalid:
        raise CloudConfigError("CLOUD_ENGINE_SETTING_INVALID", invalid)
    try:
        port = int(environ.get("PORT", ""))
    except ValueError:
        raise CloudConfigError("CLOUD_PORT_REQUIRED", ["PORT"]) from None
    if not 1024 <= port <= 65535 or str(port) != engine["MANAGED_HTTP_PORT"]:
        raise CloudConfigError("CLOUD_PORT_MISMATCH", ["PORT", "MANAGED_HTTP_PORT"])
    return TraderConfig(runtime, port, environ["MANAGED_DATABASE_URL"])


def _paper_credentials(environ, credentials_type):
    """``AlpacaCredentials.from_env``'s checks against this mapping: fixed paper endpoints."""
    from catalyst_lab.alpaca import DATA_ENDPOINT
    from catalyst_lab.config import PAPER_ENDPOINT

    for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL"):
        if environ.get(name, PAPER_ENDPOINT) != PAPER_ENDPOINT:
            raise ValueError
    if environ.get("APCA_DATA_BASE_URL", DATA_ENDPOINT) != DATA_ENDPOINT:
        raise ValueError
    return credentials_type(environ["APCA_API_KEY_ID"], environ["APCA_API_SECRET_KEY"])


def status_api(value, *, on_railway):
    """``http://<service>.railway.internal:<port>`` on Railway, loopback for a local proof."""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        allowed = PRIVATE_HOST.fullmatch(host) if on_railway else host == LOOPBACK
        if (url.scheme != "http" or not allowed or not url.port
                or not 1024 <= url.port <= 65535 or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError):
        raise CloudConfigError("CLOUD_STATUS_API_INVALID", ["MANAGED_STATUS_API"]) from None


def ops_config(environ, *, cwd=None, release_root=None):
    """The ops service: status token, private status URL, audit and backup roles, notify."""
    runtime = cloud_runtime(environ, cwd=cwd, release_root=release_root)
    held = [name for name in TRADING_SECRETS if name in environ]
    if held:
        raise CloudConfigError("OPS_MUST_NOT_HOLD_TRADING_SECRETS", held)
    unknown = unknown_names(environ, OPS_NAMES)
    if unknown:
        raise CloudConfigError("CLOUD_UNKNOWN_ENGINE_VARIABLE", unknown)
    missing = [name for name in OPS_REQUIRED
               if not environ.get(name) or _placeholder(environ[name])]
    if missing:
        raise CloudConfigError("CLOUD_SECRET_MISSING", missing)
    token = _token(environ, "MANAGED_STATUS_TOKEN")
    api = status_api(environ["MANAGED_STATUS_API"], on_railway=runtime.on_railway)
    urls = {name: database_url(environ, name, on_railway=runtime.on_railway)
            for name in ("AUDIT_DATABASE_URL", "BACKUP_DATABASE_URL")}
    operator = None
    if environ.get("OPERATOR_DATABASE_URL"):
        operator = database_url(environ, "OPERATOR_DATABASE_URL", on_railway=runtime.on_railway)
    retention = environ.get("CLOUD_BACKUP_RETENTION_DAYS", "14")
    if not retention.isdigit() or not 1 <= int(retention) <= 365:
        raise CloudConfigError("CLOUD_BACKUP_RETENTION_INVALID", ["CLOUD_BACKUP_RETENTION_DAYS"])
    notify, url = notify_settings(environ)
    return OpsConfig(runtime, api, token, urls["AUDIT_DATABASE_URL"],
                     urls["BACKUP_DATABASE_URL"], operator, int(retention), notify, url)


def jobs_config(environ, *, cwd=None, release_root=None):
    """The nightly jobs service (package learning-app): Railway mode, no local secret source,
    none of the broker, Jev or other trading credentials (refused by name), no unknown engine
    variable, and the trader's ``catalyst_risk`` connection with TLS. It keeps no state: every
    record it makes is an idempotent ledger event, so it needs no volume."""
    if environ.get(ENVIRONMENT) != RAILWAY:
        raise CloudConfigError("RAILWAY_MODE_REQUIRED", [ENVIRONMENT])
    refuse_local_secret_sources(environ, cwd=cwd, release_root=release_root)
    held = [name for name in JOBS_FORBIDDEN if name in environ]
    if held:
        raise CloudConfigError("JOBS_MUST_NOT_HOLD_TRADING_SECRETS", held)
    unknown = unknown_names(environ, JOBS_NAMES)
    if unknown:
        raise CloudConfigError("CLOUD_UNKNOWN_ENGINE_VARIABLE", unknown)
    on_railway = bool(environ.get("RAILWAY_ENVIRONMENT_ID"))
    url = database_url(environ, "MANAGED_DATABASE_URL", on_railway=on_railway)
    return JobsConfig(on_railway, url)


def notify_settings(environ):
    """``CLOUD_NOTIFY_JSON`` (the private config's notify section without the file and the
    desktop banner) plus the secret ``NOTIFY_PING_URL``; both absent means no notifier."""
    from catalyst_lab.notify import NotifyError, check_ping_url, validate_notify_section

    raw, url = environ.get("CLOUD_NOTIFY_JSON"), environ.get("NOTIFY_PING_URL")
    if not raw and not url:
        return {}, None
    try:
        section = strict_json(raw or "")
        validate_notify_section(section, environment_url=True)
        if not section:
            raise NotifyError("NOTIFY_CONFIG_INVALID")
        return section, check_ping_url(url or "", section["allowed_hosts"])
    except (TypeError, ValueError):
        raise CloudConfigError("CLOUD_NOTIFY_INVALID",
                               ["CLOUD_NOTIFY_JSON", "NOTIFY_PING_URL"]) from None


def summary(config):
    """A redacted view of a validated configuration, for logs and evidence."""
    if isinstance(config, JobsConfig):
        return {"on_railway": config.on_railway, "component": "jobs"}
    runtime = config.runtime
    common = {"on_railway": runtime.on_railway, "bind_host": runtime.bind_host,
              "state_dir": str(runtime.state_dir)}
    if isinstance(config, TraderConfig):
        return {**common, "component": "trader", "port": config.port}
    return {**common, "component": "ops", "status_api": config.status_api,
            "backup_retention_days": config.backup_retention_days,
            "notify": "CONFIGURED" if config.notify else "NOT_CONFIGURED",
            "operator_database": config.operator_database_url is not None}


def dumps(value):
    return json.dumps(value, sort_keys=True)
