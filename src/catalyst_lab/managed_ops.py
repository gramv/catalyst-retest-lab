"""Private local launch, redacted watchdog, recoverable audit evidence and operator controls.

Supervisor rendering never installs/starts a service. Audit checkpoints recover
event evidence, not a PostgreSQL database or a running trading session. Operator
commands use a separate catalyst_operator login and audited database functions only.
Private configuration v2 binds every launch to an immutable release (scripts/build_release.py)
by the SHA-256 of the imported package, and separates the Muse, status and operator tokens.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from catalyst_lab.audit import ZERO_HASH, credential_findings, verify_events
from catalyst_lab.config import SCHEMA_VERSION
from catalyst_lab.jev_contract import strict_json

ENV_NAMES = frozenset({
    "MANAGED_ENVIRONMENT", "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL",
    "MANAGED_RUNTIME_POLICY_JSON", "MANAGED_SOURCE_POLICY_JSON", "MANAGED_CYCLE_POLICY_JSON",
    "MANAGED_POSITION_POLICY_JSON", "MANAGED_POSITION_REVIEW_SECONDS", "JEV_REVIEW_POLICY_JSON",
    "JEV_CREDENTIAL_SLOT", "JEV_WORKER_MAX_INFLIGHT", "JEV_WORKER_POLL_SECONDS",
    "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "MANAGED_HTTP_PORT", "MANAGED_API_TOKEN",
    "MANAGED_REPORT_MAX_SECONDS", "MANAGED_CRYPTO_CLASSIFICATIONS_JSON",
    "MANAGED_CLASSIFICATION_POLICY", "MANAGED_US_CLASSIFICATIONS_JSON",
    "MANAGED_CRYPTO_DAY_POLICY_JSON", "MANAGED_MONITOR_TRIGGER_POLICY_JSON",
    "MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON",
    "TYPESAFE_ENV_FILE", "TYPESAFE_API_KEY", "CATALYST_ENVIRONMENT",
    # Account-risk policy row (migration 016) and optional owner crypto buckets.
    "MANAGED_RISK_POLICY_ID", "MANAGED_CRYPTO_BUCKETS_JSON",
    "MANAGED_MANAGEMENT_REVIEWS",  # Staging switch ENABLED/DISABLED (plan 0.10), required.
    # Selection rule B1 activation (plan 1.7 lean form, migration 018); optional, V2 default.
    "MANAGED_SELECTION_RULE", "MANAGED_SELECTION_QUALITY_FLOOR",
    # Research run schedule (RESEARCH_SCHEDULE_V1, 2026-09-26); optional: report V3 needs it.
    "MANAGED_RESEARCH_SCHEDULE_JSON",
    # K of the top-K selection rules (migrations 021 and 023); optional, default {"k": 10}.
    "MANAGED_TOPK_SELECTION_JSON",
    # The trade window (CRYPTO_WINDOW_REVIEW_V1 / CRYPTO_WINDOW_HOLD_V1, package review-window);
    # optional: absent, admission records the 24-hour versions. Required on Railway.
    "MANAGED_CRYPTO_WINDOW_JSON",
    # The monthly Jev budget (JEV_SPEND_GUARD_V1, package jev-budget): the budget is required
    # when MANAGED_MANAGEMENT_REVIEWS is ENABLED; price and bytes per token default (0.042, 3).
    # The Railway profile requires all three (cloud_config.CLOUD_REQUIRED_OPTIONAL).
    "JEV_MONTHLY_BUDGET_USD", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD", "JEV_BYTES_PER_TOKEN",
})
OPTIONAL_ENV = frozenset({"MANAGED_CRYPTO_DAY_POLICY_JSON", "MANAGED_MONITOR_TRIGGER_POLICY_JSON",
                          "MANAGED_SELECTION_RULE", "MANAGED_SELECTION_QUALITY_FLOOR",
                          "MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON", "MANAGED_CRYPTO_BUCKETS_JSON",
                          "MANAGED_RESEARCH_SCHEDULE_JSON", "MANAGED_TOPK_SELECTION_JSON",
                          "MANAGED_CRYPTO_WINDOW_JSON",
                          "TYPESAFE_ENV_FILE", "TYPESAFE_API_KEY", "CATALYST_ENVIRONMENT",
                          "JEV_MONTHLY_BUDGET_USD", "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD",
                          "JEV_BYTES_PER_TOKEN"})
REQUIRED_ENV = ENV_NAMES - OPTIONAL_ENV
LIMIT = 131072

# Private configuration v2. Role tokens live only in their owner-only token files; the launcher
# injects them into the app process, so the v2 environment block may not carry an API token.
CONFIG_VERSION = 2
ENV_NAMES_V2 = ENV_NAMES - {"MANAGED_API_TOKEN"}
TOKEN_ROLES = ("muse", "status", "operator")
V1_DEPRECATION = "PRIVATE_CONFIG_V1_DEPRECATED_SINGLE_TOKEN_NO_RELEASE_USE_CONFIG_VERSION_2"
MUSE_KEYS = frozenset({"api", "token_file", "spool", "command", "us_interval_seconds",
                       "crypto_interval_seconds"})
WATCHDOG_KEYS = frozenset({"api", "alarm_file", "interval_seconds", "timeout_seconds",
                           "tick_max_age_seconds", "reconciliation_max_age_seconds",
                           "research_max_age_seconds", "muse_max_age_seconds"})
AUDIT_KEYS = frozenset({"database_url", "directory", "interval_seconds"})
V2_SECTIONS = {
    "muse": MUSE_KEYS, "status": frozenset({"token_file"}),
    "operator": frozenset({"token_file"}),  # Reserved for the future operator HTTP (plan 4.3).
    "watchdog": WATCHDOG_KEYS, "audit": AUDIT_KEYS,
    "release": frozenset({"directory", "source_sha256"}),
    "ledger": frozenset({"data_directory", "socket_directory", "pg_ctl", "marker_file",
                         "wait_seconds"}),
    "backup": frozenset({"directory", "retention_days"}),
    # Off-host alerts (plan 4.2): {} disables the notifier (the default and a fully valid,
    # complete configuration). Populated, every key below is required and each value is
    # validated by catalyst_lab.notify.validate_notify_section (mirrors NOTIFY_KEYS there),
    # called from _validate_v2 below; this tuple is not used for the exact-key check itself
    # because {} must also be accepted.
    "notify": frozenset({"provider", "ping_url_file", "allowed_hosts", "reminder_minutes",
                         "daily_head_hour_utc", "timeout_seconds", "local_notification"}),
    "logs": frozenset({"directory", "max_bytes", "backups"}),
}
# LaunchAgent start order. launchd has no dependencies, so app/muse also wait for the ledger.
COMPONENTS = ("ledger", "app", "muse", "watchdog", "audit", "backup")
RELEASE_METADATA = "release.json"
RELEASE_KEYS = frozenset({"commit", "source_sha256", "built_at", "python"})
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
COMMIT_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PLACEHOLDER = re.compile(r"REQUIRED[A-Z0-9_]*")
CRASH_LOOP_STARTS = 5
CRASH_LOOP_WINDOW_SECONDS = 600
DATABASE_BACKOFF_CAP_SECONDS = 15
# Plan 1.3 (minimal, no registry): optional top-level ``agents`` list of research-agent
# credentials, one token file per agent. ``muse.token_file`` stays the legacy identity
# (agent ``muse``, version LEGACY_UNDECLARED). Absent means no agent credentials.
AGENT_KEYS = frozenset({"agent_id", "token_file"})
OPTIONAL_V2_SECTIONS = frozenset({"agents"})


def private_bytes(path, *, limit=LIMIT):
    """No symlink or permissive config/token files; errors never include content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise ValueError
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError
            return raw
    except (OSError, ValueError):
        raise ValueError("PRIVATE_OWNER_MODE_0600_FILE_REQUIRED") from None


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or "\x00" in value:
        raise ValueError("ABSOLUTE_PRIVATE_PATH_REQUIRED")
    return value


def loopback_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "http" or url.hostname != "127.0.0.1"
                or not 1024 <= url.port <= 65535 or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("PRIVATE_LOOPBACK_API_REQUIRED") from None


def _integers(item, keys, low, high):
    if any(type(item[k]) is not int or not low <= item[k] <= high for k in keys):
        raise ValueError


def _validate_common(config, env_names):
    """Checks shared by v1 and v2: allowlisted environment, loopback API, direct Muse argv."""
    env = config["environment"]
    if (not isinstance(env, dict) or set(env) - env_names
            or any(not isinstance(v, str) or "\x00" in v for v in env.values())):
        raise ValueError
    muse, watch, audit = (config[k] for k in ("muse", "watchdog", "audit"))
    for item in (muse, watch):
        loopback_url(item["api"])
    if muse["api"].rstrip("/") != watch["api"].rstrip("/"):
        raise ValueError
    for value in (muse["token_file"], muse["spool"], watch["alarm_file"], audit["directory"]):
        _absolute(value)
    if (not isinstance(muse["command"], list) or not 1 <= len(muse["command"]) <= 32
            or any(not isinstance(v, str) or not v or "\x00" in v for v in muse["command"])
            or Path(muse["command"][0]).name in {"sh", "bash", "zsh", "fish", "env"}):
        raise ValueError
    for item, keys, low, high in (
        (muse, ("us_interval_seconds", "crypto_interval_seconds"), 60, 86400),
        (watch, ("interval_seconds",), 5, 300),
        (watch, ("timeout_seconds",), 1, 10),
        (watch, ("tick_max_age_seconds", "reconciliation_max_age_seconds",
                 "research_max_age_seconds", "muse_max_age_seconds"), 5, 3600),
        (audit, ("interval_seconds",), 60, 86400),
    ):
        _integers(item, keys, low, high)
    if not isinstance(audit["database_url"], str) or "\x00" in audit["database_url"]:
        raise ValueError


def _agent_token_files(config):
    """Exact ``{agent_id, token_file}`` entries with unique valid IDs; their token paths."""
    from catalyst_lab.agent_identity import AGENT_ID, MAX_AGENT_TOKENS

    agents = config.get("agents", [])
    if not isinstance(agents, list) or len(agents) > MAX_AGENT_TOKENS:
        raise ValueError
    for entry in agents:
        if (not isinstance(entry, dict) or set(entry) != AGENT_KEYS
                or not isinstance(entry["agent_id"], str)
                or not AGENT_ID.fullmatch(entry["agent_id"])):
            raise ValueError
    if len({entry["agent_id"] for entry in agents}) != len(agents):
        raise ValueError
    return [os.path.normpath(_absolute(entry["token_file"])) for entry in agents]


def _validate_v2(config):
    from catalyst_lab.localdb import LEDGER_MARKER

    if (set(config) - OPTIONAL_V2_SECTIONS != {"config_version", "environment", *V2_SECTIONS}
            or type(config["config_version"]) is not int
            or config["config_version"] != CONFIG_VERSION):
        raise ValueError
    for name, keys in V2_SECTIONS.items():
        if name == "notify":
            continue  # Special-cased below: {} is also valid (plan 4.2).
        if not isinstance(config[name], dict) or set(config[name]) != keys:
            raise ValueError
    from catalyst_lab.notify import validate_notify_section

    validate_notify_section(config["notify"])
    _validate_common(config, ENV_NAMES_V2)
    tokens = [os.path.normpath(_absolute(config[role]["token_file"])) for role in TOKEN_ROLES]
    tokens += _agent_token_files(config)
    if len(set(tokens)) != len(tokens):
        raise ValueError  # One file per role and agent: no two credentials share a token.
    release, ledger = config["release"], config["ledger"]
    for value in (release["directory"], ledger["data_directory"], ledger["socket_directory"],
                  ledger["pg_ctl"], ledger["marker_file"], config["backup"]["directory"],
                  config["logs"]["directory"]):
        _absolute(value)
    digest = release["source_sha256"]
    if not isinstance(digest, str) or not (
        SHA256_HEX.fullmatch(digest) or PLACEHOLDER.fullmatch(digest)
    ):
        raise ValueError
    if Path(ledger["marker_file"]).name != LEDGER_MARKER or Path(ledger["pg_ctl"]).name != "pg_ctl":
        raise ValueError
    _integers(ledger, ("wait_seconds",), 1, 600)
    _integers(config["backup"], ("retention_days",), 1, 365)
    _integers(config["logs"], ("max_bytes",), 65536, 1 << 30)
    _integers(config["logs"], ("backups",), 1, 20)
    return config


def load_private_config(path):
    """Exact keys. v2 (``config_version: 2``) is current. A v1 file still loads, marked with a
    ``deprecation`` field, for inspection and the audited operator commands; rendering and
    launching require v2."""
    try:
        config = strict_json(private_bytes(path))
        if not isinstance(config, dict):
            raise ValueError
        if "config_version" in config:
            return _validate_v2(config)
        if set(config) != {
            "version", "environment", "muse", "watchdog", "audit"
        } or type(config["version"]) is not int or config["version"] != 1:
            raise ValueError
        for name, keys in (("muse", MUSE_KEYS), ("watchdog", WATCHDOG_KEYS | {"token_file"}),
                           ("audit", AUDIT_KEYS)):
            if not isinstance(config[name], dict) or set(config[name]) != keys:
                raise ValueError
        _validate_common(config, ENV_NAMES)
        _absolute(config["watchdog"]["token_file"])
        return {**config, "deprecation": V1_DEPRECATION}
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError("INVALID_PRIVATE_LAUNCH_CONFIGURATION") from None


def config_template(private_root):
    """A v2 template; the release section is filled only when run from a verified release."""
    root = Path(_absolute(str(private_root)))
    identity = code_version()
    release = {"directory": "/REQUIRED/releases/COMMIT",
               "source_sha256": "REQUIRED_RELEASE_SOURCE_SHA256"}
    if identity["release_commit"]:
        release = {"directory": str(package_directory().parents[1]),
                   "source_sha256": identity["source_sha256"]}
    return {
        "config_version": CONFIG_VERSION,
        "environment": {name: "REQUIRED" for name in sorted(REQUIRED_ENV & ENV_NAMES_V2)},
        "muse": {"api": "http://127.0.0.1:8780", "token_file": str(root / "muse-token"),
                 "spool": str(root / "muse.sqlite"),
                 "command": ["/REQUIRED/muse-codex/node_modules/.bin/codex",
                             "-c", 'web_search="live"', "exec", "--ephemeral",
                             "--sandbox", "read-only", "--skip-git-repo-check",
                             "--ignore-user-config"],
                 "us_interval_seconds": 1800, "crypto_interval_seconds": 1800},
        "status": {"token_file": str(root / "status-token")},
        "operator": {"token_file": str(root / "operator-token")},
        "watchdog": {"api": "http://127.0.0.1:8780",
                     "alarm_file": str(root / "alarms.json"), "interval_seconds": 30,
                     "timeout_seconds": 3, "tick_max_age_seconds": 15,
                     "reconciliation_max_age_seconds": 90, "research_max_age_seconds": 180,
                     "muse_max_age_seconds": 600},
        "audit": {"database_url": "REQUIRED_RESTRICTED_READ_CONNECTION",
                  "directory": str(root / "audit-checkpoints"), "interval_seconds": 3600},
        "release": release,
        "ledger": {"data_directory": "/REQUIRED/ledger/postgres",
                   "socket_directory": "/REQUIRED/ledger/socket",
                   "pg_ctl": "/REQUIRED/postgresql/bin/pg_ctl",
                   "marker_file": "/REQUIRED/ledger/LEDGER.json", "wait_seconds": 120},
        "backup": {"directory": str(root / "backups"), "retention_days": 14},
        # Off-host alerts (plan 4.2), disabled by default. Populated shape (all required):
        # {"provider": "HEALTHCHECKS", "ping_url_file": "/abs/0600/file",
        #  "allowed_hosts": ["hc-ping.com"], "reminder_minutes": 30,
        #  "daily_head_hour_utc": 6, "timeout_seconds": 5, "local_notification": true}.
        "notify": {},
        "logs": {"directory": str(root / "logs"), "max_bytes": 20 * 1024 * 1024, "backups": 5},
        # Research-agent credentials, e.g. {"agent_id": "instinct", "token_file": ...}.
        "agents": [],
    }


def package_directory():
    """The directory of the catalyst_lab package this interpreter actually imported."""
    import catalyst_lab

    return Path(catalyst_lab.__file__).resolve().parent


def package_sha256(directory):
    """SHA-256 over the package's .py/.sql files (sorted relative paths, length-framed).

    Paths are relative to the package's parent, so the exported release tree and the imported
    package hash identically. A symlink anywhere in the package changes the hash.
    """
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("PACKAGE_DIRECTORY_REQUIRED")
    entries = []
    for current, directories, files in os.walk(root):  # Never follows directory symlinks.
        for name in directories + files:
            path = Path(current) / name
            relative = path.relative_to(root.parent).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries.append((relative, b"L", os.readlink(path).encode()))
            elif name in files and path.suffix in {".py", ".sql"}:
                if not stat.S_ISREG(mode):
                    raise ValueError("PACKAGE_FILE_NOT_REGULAR")
                entries.append((relative, b"F", path.read_bytes()))
        directories[:] = [d for d in directories if d != "__pycache__"]
    digest = hashlib.sha256()
    for relative, kind, data in sorted(entries):
        digest.update(b"\0".join((kind, relative.encode(), str(len(data)).encode(), b"")))
        digest.update(data)
    return digest.hexdigest()


def release_metadata(directory):
    """The release.json written by scripts/build_release.py; None when absent."""
    path = Path(directory) / RELEASE_METADATA
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("RELEASE_METADATA_INVALID") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError
        value = strict_json(raw)
        if (not isinstance(value, dict) or set(value) != RELEASE_KEYS
                or not isinstance(value["commit"], str) or not COMMIT_HEX.fullmatch(value["commit"])
                or not isinstance(value["source_sha256"], str)
                or not SHA256_HEX.fullmatch(value["source_sha256"])
                or not isinstance(value["built_at"], str) or not isinstance(value["python"], dict)):
            raise ValueError
        return value
    except (OSError, TypeError, ValueError):
        raise ValueError("RELEASE_METADATA_INVALID") from None


def code_version(package_dir=None):
    """Identity of the imported package, never of a checkout's git state.

    ``release_commit`` comes from the release.json at the release root (the directory that
    holds ``src/catalyst_lab``) and only when its recorded hash matches the imported code.
    """
    directory = Path(package_dir).resolve() if package_dir is not None else package_directory()
    source = package_sha256(directory)
    try:
        metadata = release_metadata(directory.parents[1])
    except ValueError:
        metadata = "INVALID"
    if not isinstance(metadata, dict):
        return {"source_sha256": source, "release_commit": None,
                "release_metadata": metadata or "ABSENT"}
    if metadata["source_sha256"] != source:
        return {"source_sha256": source, "release_commit": None, "release_metadata": "MISMATCH"}
    return {"source_sha256": source, "release_commit": metadata["commit"],
            "release_metadata": "MATCH"}


def verify_release(config, *, package_dir=None):
    """Refuse to launch code other than the configured, built, read-only release."""
    release = config["release"]
    package = Path(package_dir).resolve() if package_dir is not None else package_directory()
    source = package_sha256(package)
    if source != release["source_sha256"]:
        raise ValueError("RELEASE_HASH_MISMATCH")
    directory = Path(release["directory"]).resolve()
    if not package.is_relative_to(directory):
        raise ValueError("RELEASE_PATH_MISMATCH")
    metadata = release_metadata(directory)
    if metadata is None:
        raise ValueError("RELEASE_METADATA_MISSING")
    if metadata["source_sha256"] != source:
        raise ValueError("RELEASE_METADATA_MISMATCH")
    return {"release_commit": metadata["commit"], "source_sha256": source}


def _placeholder(value):
    return isinstance(value, str) and (value.startswith("REQUIRED") or "/REQUIRED/" in value)


def _preflight_environment(config):
    env = dict(config["environment"])
    if config.get("config_version") == CONFIG_VERSION:
        try:  # v2 supplies the Muse API token from its private file; it is never reported.
            env["MANAGED_API_TOKEN"] = role_token(config["muse"]["token_file"])
        except ValueError:
            env["MANAGED_API_TOKEN"] = "REQUIRED_PRIVATE_MUSE_TOKEN_FILE"
    return env


def _notify_preflight_report(section):
    """``notify: CONFIGURED|NOT_CONFIGURED|INVALID``, without the URL (plan 4.2).

    Re-validates from scratch (the caller's ``config`` may not have gone through
    ``load_private_config``/``_validate_v2``) and actually opens the ping-url file, the
    same way ``role_token`` is re-checked here for the muse/status/operator tokens.
    """
    if not section:
        return {"state": "NOT_CONFIGURED", "configured": False}
    from catalyst_lab.notify import read_ping_url, validate_notify_section

    try:
        validate_notify_section(section)
        read_ping_url(section["ping_url_file"], section["allowed_hosts"])
    except ValueError:
        return {"state": "INVALID", "configured": True}
    return {"state": "CONFIGURED", "configured": True, "provider": section["provider"],
            "local_notification": section["local_notification"]}


def _preflight_reports(config):
    """Release, token-separation, ledger-marker and notify reports: (reports, missing, invalid)."""
    from catalyst_lab.localdb import ledger_retirement, ledger_role

    if config.get("config_version") != CONFIG_VERSION:
        return ({"release": {"configured": False},
                 "token_separation": {"mode": "SINGLE_SHARED_TOKEN_V1", "separated": False},
                 "ledger": {"configured": False},
                 "notify": {"state": "NOT_CONFIGURED", "configured": False}},
                set(), {"CONFIG_VERSION_2_REQUIRED"})
    missing, invalid = set(), set()
    for section in V2_SECTIONS:
        for key, value in config[section].items():
            if _placeholder(value) or (key == "command" and _placeholder(value[0])):
                missing.add(f"{section}.{key}")
    identity = code_version()
    release = {"imported_source_sha256": identity["source_sha256"],
               "imported_release_commit": identity["release_commit"],
               "imported_release_metadata": identity["release_metadata"],
               "configured_source_sha256": config["release"]["source_sha256"],
               "hash_match": identity["source_sha256"] == config["release"]["source_sha256"]}
    try:
        verify_release(config)
        release["launch_check"] = "VERIFIED"
    except ValueError as exc:
        release["launch_check"] = str(exc)
        if not missing & {"release.directory", "release.source_sha256"}:
            invalid.add("RELEASE_NOT_VERIFIED")
    values, tokens = {}, {"mode": "ROLE_TOKENS_V2", "distinct_files": True}
    for role in TOKEN_ROLES:
        try:
            values[role] = role_token(config[role]["token_file"])
            tokens[role] = "PRIVATE_VALID"
        except ValueError:
            tokens[role] = "UNAVAILABLE_OR_INVALID"
            missing.add(role + ".token_file")
    agents = config.get("agents", [])
    if agents:  # Research-agent credentials (plan 1.3); values are never reported.
        tokens["agents"] = {}
    for entry in agents:
        try:
            values["agent:" + entry["agent_id"]] = role_token(entry["token_file"])
            tokens["agents"][entry["agent_id"]] = "PRIVATE_VALID"
        except ValueError:
            tokens["agents"][entry["agent_id"]] = "UNAVAILABLE_OR_INVALID"
            missing.add(f"agents.{entry['agent_id']}.token_file")
    expected = len(TOKEN_ROLES) + len(agents)
    tokens["distinct_values"] = (len(set(values.values())) == expected
                                 if len(values) == expected else None)
    tokens["separated"] = tokens["distinct_values"] is True
    if tokens["distinct_values"] is False:
        invalid.add("ROLE_TOKENS_NOT_SEPARATED")
    ledger = config["ledger"]
    try:
        marker = ledger_role(Path(ledger["marker_file"]).parent) or "ABSENT"
    except RuntimeError:
        marker = "INVALID"
    if marker != "ACCOUNT_LEDGER" and "ledger.marker_file" not in missing:
        invalid.add("LEDGER_ACCOUNT_MARKER_REQUIRED")
    try:
        retired = ledger_retirement(Path(ledger["marker_file"]).parent) is not None
    except RuntimeError:
        retired = None  # Unreadable: the launcher refuses it as well (fail closed).
    if retired is not False and marker == "ACCOUNT_LEDGER":
        invalid.add("LEDGER_RETIRED" if retired else "LEDGER_MARKER_UNREADABLE")
    pg_ctl = Path(ledger["pg_ctl"])
    ledger_report = {"marker": marker, "account_ledger_marked": marker == "ACCOUNT_LEDGER",
                     "retired": retired,
                     "pg_ctl_present": os.access(pg_ctl, os.X_OK),
                     "postgres_present": os.access(pg_ctl.with_name("postgres"), os.X_OK),
                     "data_directory_initialized":
                         (Path(ledger["data_directory"]) / "PG_VERSION").is_file(),
                     "database_wait_seconds": ledger["wait_seconds"]}
    notify_report = _notify_preflight_report(config["notify"])
    if notify_report["state"] == "INVALID":
        invalid.add("NOTIFY_CONFIGURATION_INVALID")
    return ({"release": release, "token_separation": tokens, "ledger": ledger_report,
             "notify": notify_report}, missing, invalid)


def environment_findings(env, *, api_port=None):
    """``(missing, invalid)`` names of an engine environment map; values are never reported.

    The checks of the private config v2 preflight, shared with the Railway profile (package
    cloud): placeholders and empty values are missing, and every policy is built with the same
    constructors startup uses. ``api_port`` (the Muse/watchdog loopback port of the private
    configuration) must equal ``MANAGED_HTTP_PORT`` when given.
    """
    missing = sorted(k for k in REQUIRED_ENV | set(env)
                     if not env.get(k) or env[k].startswith("REQUIRED")
                     or env[k].startswith("/REQUIRED/"))
    invalid = []
    for key, value in env.items():
        if key.endswith("_JSON") and not value.startswith("REQUIRED"):
            try:
                strict_json(value)
            except (ValueError, TypeError):
                invalid.append(key)
    if env.get("MANAGED_ENVIRONMENT") not in {"local_test", "supervised_paper"}:
        invalid.append("MANAGED_ENVIRONMENT")
    # The staging switch exactly as startup requires it (plan 0.10): ENABLED or DISABLED, no
    # case folding. Checked here so a Railway trader refuses at once instead of failing its
    # startup retries for ten minutes at a time (package cloud-hardening).
    from catalyst_lab.position_monitor import MANAGEMENT_REVIEWS_SETTINGS

    if ("MANAGED_MANAGEMENT_REVIEWS" not in missing
            and env.get("MANAGED_MANAGEMENT_REVIEWS") not in MANAGEMENT_REVIEWS_SETTINGS):
        invalid.append("MANAGED_MANAGEMENT_REVIEWS")
    # JEV_SPEND_GUARD_V1 (package jev-budget): each setting exactly as startup parses it, and the
    # budget whenever Jev reviews open trades (CRYPTO_MAINTENANCE_V3 trades wait without it).
    from catalyst_lab import jev_budget

    for key in jev_budget.ENV_NAMES:
        if key in env and key not in missing:
            try:
                jev_budget.parse_setting(key, env[key])
            except ValueError:
                invalid.append(key)
    if (env.get("MANAGED_MANAGEMENT_REVIEWS") == "ENABLED"
            and jev_budget.BUDGET_ENV not in env):
        missing = sorted({*missing, jev_budget.BUDGET_ENV})
    for key in ("MANAGED_API_TOKEN", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        if key in env and any(c.isspace() for c in env[key]):
            invalid.append(key)
    # Pure configuration constructors only: do not compose a runtime or load a key.
    from catalyst_lab.crypto_execution import CryptoDayPolicy
    from catalyst_lab.crypto_liquidity import CryptoLiquidityPolicy
    from catalyst_lab.managed_app import AppSettings
    from catalyst_lab.managed_review import ManagedPolicy
    from catalyst_lab.managed_runtime import RuntimePolicy, engineering_monitor_policy
    from catalyst_lab.position_monitor import MonitorTriggerPolicy
    from catalyst_lab.research_cycle import CyclePolicy
    from catalyst_lab.research_schedule import ResearchSchedule
    from catalyst_lab.review_config import Gate1Inputs
    from catalyst_lab.scan_sources import SourcePolicy

    for key, constructor in (
        ("MANAGED_RUNTIME_POLICY_JSON", RuntimePolicy),
        ("MANAGED_SOURCE_POLICY_JSON", SourcePolicy),
        ("MANAGED_CYCLE_POLICY_JSON", CyclePolicy),
        ("MANAGED_POSITION_POLICY_JSON", ManagedPolicy),
        ("MANAGED_CRYPTO_DAY_POLICY_JSON", CryptoDayPolicy),
        ("MANAGED_RESEARCH_SCHEDULE_JSON", ResearchSchedule),
    ):
        if key not in env or key in missing:
            continue
        try:
            value = constructor(**strict_json(env[key]))
            if key == "MANAGED_POSITION_POLICY_JSON" and value != engineering_monitor_policy():
                raise ValueError
        except (ValueError, TypeError, KeyError):
            invalid.append(key)
    # The trade window exactly as startup parses it (package review-window): exact keys, the
    # version CRYPTO_WINDOW_REVIEW_V1 and window_minutes an integer 60-1440 in steps of 15.
    from catalyst_lab.crypto_holding import WINDOW_ENV, parse_window_setting

    if WINDOW_ENV in env and WINDOW_ENV not in missing and WINDOW_ENV not in invalid:
        try:
            parse_window_setting(env[WINDOW_ENV])
        except ValueError:
            invalid.append(WINDOW_ENV)
    for key, constructor, decimals in (
        ("MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON", CryptoLiquidityPolicy,
         ("minimum_dollar_volume", "maximum_participation", "assumed_round_trip_cost_bps",
          "maximum_cost_to_risk")),
        ("MANAGED_MONITOR_TRIGGER_POLICY_JSON", MonitorTriggerPolicy,
         ("near_target_progress_fraction",)),
    ):
        if key not in env or env[key].startswith("REQUIRED"):
            continue
        try:
            raw = strict_json(env[key])
            for field in decimals:
                if raw[field] is not None:
                    raw[field] = Decimal(str(raw[field]))
            constructor(**raw)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            invalid.append(key)
    if env.get("MANAGED_RISK_POLICY_ID") and not env["MANAGED_RISK_POLICY_ID"].startswith(
        "REQUIRED"
    ) and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", env["MANAGED_RISK_POLICY_ID"]):
        invalid.append("MANAGED_RISK_POLICY_ID")  # The database row is checked at startup.
    # The selection rule exactly as startup reads it (research_selection_topk): an invalid K
    # names its own key, any other refusal (a floor with top-K, a missing or stray B1/B2
    # floor, an unknown rule) the rule's.
    from catalyst_lab.research_selection_topk import (
        TOPK_ENV,
        TOPK_INVALID,
        selection_rule_from_env,
    )

    rule_env = {
        key: env[key]
        for key in ("MANAGED_SELECTION_RULE", "MANAGED_SELECTION_QUALITY_FLOOR", TOPK_ENV)
        if key in env and not env[key].startswith("REQUIRED")
    }
    try:
        selection_rule_from_env(rule_env)
    except ValueError as exc:
        invalid.append(TOPK_ENV if str(exc) == TOPK_INVALID else "MANAGED_SELECTION_RULE")
    if not missing:
        try:
            crypto_symbols = strict_json(env["MANAGED_CRYPTO_CLASSIFICATIONS_JSON"])
            buckets = env.get("MANAGED_CRYPTO_BUCKETS_JSON")
            AppSettings(
                int(env["MANAGED_HTTP_PORT"]), env["MANAGED_API_TOKEN"],
                int(env["MANAGED_REPORT_MAX_SECONDS"]),
                tuple(crypto_symbols) if crypto_symbols is not None else None,
                env["MANAGED_CLASSIFICATION_POLICY"],
                tuple(strict_json(env["MANAGED_US_CLASSIFICATIONS_JSON"])),
                crypto_buckets=strict_json(buckets) if buckets else None,
            )
            Gate1Inputs(strict_json(env["JEV_REVIEW_POLICY_JSON"]))
            if (int(env["MANAGED_POSITION_REVIEW_SECONDS"]) != 10
                    or not 1 <= int(env["JEV_WORKER_MAX_INFLIGHT"]) <= 30
                    or not 0 < float(env["JEV_WORKER_POLL_SECONDS"]) <= 60
                    or (api_port is not None and int(env["MANAGED_HTTP_PORT"]) != api_port)):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            invalid.append("MANAGED_APP_OR_REVIEW_CONFIGURATION")
    return missing, invalid


def preflight(config, checkout=None, *, repository=None):
    """No broker call, no credential loader; DB inspection is explicit/injected.

    ``checkout`` is ignored and kept for compatibility: code identity is the imported package.
    """
    env = _preflight_environment(config)
    missing, invalid = environment_findings(env, api_port=urlsplit(config["muse"]["api"]).port)
    dependencies = {name: importlib.util.find_spec(name) is not None
                    for name in ("fastapi", "uvicorn", "psycopg", "httpx", "websockets")}
    command = config["muse"]["command"][0]
    dependencies["muse_executable_present"] = (
        os.access(command, os.X_OK) if Path(command).is_absolute()
        else shutil.which(command) is not None
    )
    dependencies["muse_authentication_verified"] = False
    actual_schema = None
    database = "NOT_REQUESTED"
    if repository is not None:
        try:
            with repository.connect() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                actual_schema = conn.execute(
                    "SELECT max(version) AS version FROM lab.schema_migrations"
                ).fetchone()["version"]
            database = "MATCH" if actual_schema == SCHEMA_VERSION else "SCHEMA_MISMATCH"
        except Exception:
            database = "UNAVAILABLE"
    reports, section_missing, section_invalid = _preflight_reports(config)
    missing = sorted(set(missing) | section_missing)
    invalid.extend(section_invalid)
    return {"schema_expected": SCHEMA_VERSION, "schema_actual": actual_schema,
            "database_check": database, "code": code_version(),
            "config_version": config.get("config_version", config.get("version")),
            "deprecation": config.get("deprecation"), **reports,
            "missing_configuration_names": missing,
            "invalid_configuration_names": sorted(set(invalid)),
            "dependencies": dependencies, "configuration_complete": not missing and not invalid,
            "broker_checked": False, "ready_to_trade": False,
            "scope": "STATIC_CONFIGURATION_AND_OPTIONAL_READ_ONLY_SCHEMA"}


def _private_directory(path):
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = target.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("PRIVATE_OWNER_DIRECTORY_REQUIRED")
    return target


def _exclusive(path, data):
    target = Path(path)
    _private_directory(target.parent)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def atomic_private_json(path, value):
    target = Path(path)
    directory = _private_directory(target.parent)
    fd, temporary = tempfile.mkstemp(prefix=".alarm-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        parent_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _new_private_directory(path):
    """Create a fresh owner-only directory; an existing one is never reused or overwritten."""
    target = Path(_absolute(str(path)))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.mkdir(target, 0o700)
    except FileExistsError:
        raise ValueError("RENDER_DESTINATION_EXISTS") from None
    return _private_directory(target)


def render_supervisor(config_path, config, destination, *, python=None):
    """Return reviewable LaunchAgent files only; no launchctl or service mutation.

    Every plist runs the configured read-only release (its own venv and launcher script), never
    a working copy. Files are listed in start order: ledger, app, Muse, watchdog, audit, backup.
    The backup plist is a placeholder until plan package 4.6 implements ``managed_ops backup``.
    """
    if config.get("config_version") != CONFIG_VERSION:
        raise ValueError("PRIVATE_CONFIG_V2_REQUIRED")
    config_path = _absolute(str(config_path))
    release = Path(_absolute(config["release"]["directory"]))
    expected = config["release"]["source_sha256"]
    try:
        metadata = release_metadata(release)
        if (metadata is None or metadata["source_sha256"] != expected
                or package_sha256(release / "src" / "catalyst_lab") != expected):
            raise ValueError
    except ValueError:
        raise ValueError("RELEASE_NOT_VERIFIED") from None
    python = _absolute(str(python or release / "venv" / "bin" / "python"))
    script = release / "scripts" / "run_managed_private.py"
    if not os.path.exists(python) or not script.is_file():
        raise ValueError("RELEASE_INCOMPLETE")
    logs = _private_directory(config["logs"]["directory"])  # launchd needs it to exist.
    destination = _new_private_directory(destination)
    paths, labels = [], []
    for component in COMPONENTS:
        # launchd's own stdio file only sees output before the launcher opens its rotating log.
        bootstrap = str(logs / f"{component}.launchd.log")
        value = {
            "Label": "local.catalyst.paper." + component,
            "ProgramArguments": [python, str(script), "--config", config_path,
                                 "--component", component],
            "WorkingDirectory": str(release), "Umask": 0o077,
            # Background QoS throttles CPU and disk I/O; the ledger and app are time-critical.
            "ProcessType": "Standard" if component in {"ledger", "app"} else "Background",
            "StandardOutPath": bootstrap, "StandardErrorPath": bootstrap,
        }
        if component in {"audit", "backup"}:
            interval = config["audit"]["interval_seconds"] if component == "audit" else 86400
            value.update(StartInterval=interval, RunAtLoad=False)
        else:
            value.update(KeepAlive={"SuccessfulExit": False}, RunAtLoad=True, ThrottleInterval=10)
        if component in {"ledger", "app"}:
            value["ExitTimeOut"] = 30
        path = destination / (value["Label"] + ".plist")
        _exclusive(path, plistlib.dumps(value))
        paths.append(str(path))
        labels.append(value["Label"])
    return {"files": paths, "start_order": labels, "installed": False, "started": False,
            "release_commit": metadata["commit"], "source_sha256": expected,
            "backup": "DAILY_LEDGER_OPS_BACKUP"}


# The watchdogs take ``now`` before reading the status, while the trader's protection and research
# loops tick every second as the status is built (and, on Railway, on another host's clock): a
# tick a few seconds after ``now`` is fresh, not stale. The first Railway run (2026-09-28) raised
# PROTECTION_TICK_STALE and RESEARCH_TICK_STALE on about four reads in ten for exactly this.
CLOCK_SKEW_TOLERANCE_SECONDS = 5


def status_alarms(status, now, policy):
    alarms = []
    if not isinstance(status, dict):
        return ["STATUS_UNAVAILABLE"]
    if status.get("worker_state") != "RUNNING":
        alarms.append("WORKER_STOPPED")
    for field, bound, code in (
        ("last_protection_tick", "tick_max_age_seconds", "PROTECTION_TICK_STALE"),
        ("last_reconciliation_at", "reconciliation_max_age_seconds", "RECONCILIATION_STALE"),
        ("last_cycle_at", "research_max_age_seconds", "RESEARCH_TICK_STALE"),
    ):
        try:
            timestamp = datetime.fromisoformat(status[field])
            if (timestamp.tzinfo is None
                    or not -CLOCK_SKEW_TOLERANCE_SECONDS
                    <= (now - timestamp).total_seconds() <= policy[bound]):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            alarms.append(code)
    if status.get("trade_stream_connected") is not True:
        alarms.append("BROKER_STREAM_LOST")
    if status.get("research_healthy") is not True:
        alarms.append("RESEARCH_UNHEALTHY")
    if status.get("account_safety_healthy") is not True:
        alarms.append("ACCOUNT_SAFETY_UNHEALTHY")
    if status.get("executor_ownership") != "EXCLUSIVE":
        alarms.append("EXECUTOR_OWNERSHIP_UNAVAILABLE")
    streams = status.get("market_streams", {})
    required = status.get("required_market_streams")
    if not isinstance(required, list) or any(v not in {"US", "CRYPTO"} for v in required):
        alarms.append("MARKET_STREAM_REQUIREMENTS_UNAVAILABLE")
    else:
        for market in required:
            if not isinstance(streams, dict) or streams.get(market) is not True:
                alarms.append(market + "_MARKET_STREAM_LOST")
    if status.get("error_code"):
        alarms.append("WORKER_REPORTED_ERROR")
    breaker = status.get("jev_breaker")
    if isinstance(breaker, dict) and breaker.get("state") not in (None, "CLOSED"):
        alarms.append("JEV_BREAKER_OPEN")
    alarms += flatten_alarms(status.get("operator_flatten"), now)
    alarms += execution_halt_alarms(status.get("execution_halts"))
    alarms += exit_refusal_alarms(status.get("exit_refusal_alarms"))
    alarms += maintenance_alarms(status.get("trade_maintenance"))
    alarms += day_review_alarms(status.get("day_reviews"))
    alarms += gap_resume_alarms(status.get("gap_resume"), now)
    alarms += jev_budget_alarms(status.get("jev_budget"), now)
    return sorted(set(alarms))


FLATTEN_PENDING_ALARM_SECONDS = 60


def flatten_alarms(flatten, now):
    """FLATTEN_PENDING once the oldest pending operator flatten is older than 60 s.

    An app without the field (older release) or with no pending request raises nothing; a
    pending request whose time is unreadable, or lies in the future, fails closed.
    """
    if not isinstance(flatten, dict) or flatten.get("oldest_pending_requested_at") is None:
        return []
    try:
        since = datetime.fromisoformat(flatten["oldest_pending_requested_at"])
        age = (now - since).total_seconds()
    except (TypeError, ValueError):
        return ["FLATTEN_PENDING"]
    if since.tzinfo is None or not 0 <= age <= FLATTEN_PENDING_ALARM_SECONDS:
        return ["FLATTEN_PENDING"]
    return []


EXECUTION_HALT_ALARM = "EXECUTION_HALT_ACTIVE"
HALT_KIND_PREFIX = "HALT_"


def execution_halt_alarms(halts):
    """EXECUTION_HALT_ACTIVE plus one ``HALT_<KIND>`` code per active halt kind.

    Every unreleased execution halt, and today's daily risk halt (kind DAILY_RISK_HALT),
    refuses new entries while protection keeps running, so a halted app is never reported
    healthy. An app without the field (older release) raises nothing; an unreadable or
    malformed field fails closed as EXECUTION_HALT_STATUS_UNAVAILABLE. A kind that cannot
    form a code of at most 64 characters becomes HALT_KIND_UNRECOGNIZED.
    """
    if halts is None:
        return []
    if (not isinstance(halts, dict) or halts.get("available") is not True
            or not isinstance(halts.get("kinds"), list)):
        return ["EXECUTION_HALT_STATUS_UNAVAILABLE"]
    alarms = [EXECUTION_HALT_ALARM] if halts["kinds"] else []
    for kind in halts["kinds"]:
        code = HALT_KIND_PREFIX + kind if isinstance(kind, str) else ""
        alarms.append(code if REFUSAL_CODE.fullmatch(code) else "HALT_KIND_UNRECOGNIZED")
    return alarms


EXIT_REFUSAL_ALARM = "EXIT_REFUSED_REPEATEDLY"


def exit_refusal_alarms(rows):
    """EXIT_REFUSED_REPEATEDLY (plus ``_CRYPTO`` / ``_US_STOCKS``) while any working setup's
    close has been refused five or more times in a row (the app's alarm threshold). An app
    without the field raises nothing; a malformed field fails closed."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        return [EXIT_REFUSAL_ALARM]
    alarms = [EXIT_REFUSAL_ALARM] if rows else []
    for row in rows:
        market = row.get("market") if isinstance(row, dict) else None
        if market in {"CRYPTO", "US_STOCKS"}:
            alarms.append(EXIT_REFUSAL_ALARM + "_" + market)
    return alarms


EXIT_FLAG_ALARM = "EXIT_FLAG_PENDING"
MAINTENANCE_FAILING_ALARM = "MAINTENANCE_REVIEW_FAILING"


def maintenance_alarms(section):
    """CRYPTO_MAINTENANCE_V1 (package maintenance): EXIT_FLAG_PENDING while an early-exit flag
    of an open trade is unresolved (plan 4.6.3, "alerted"), MAINTENANCE_REVIEW_FAILING while a
    maintained trade's last review failed (Jev unavailable; the trade keeps its stop and target).
    An app without the field raises nothing; an unreadable one fails closed."""
    if section is None:
        return []
    if not isinstance(section, dict) or section.get("available") is False:
        return ["MAINTENANCE_STATUS_UNAVAILABLE"]
    alarms = []
    flags = section.get("pending_exit_flags")
    if type(flags) is not int:
        return ["MAINTENANCE_STATUS_UNAVAILABLE"]
    if flags > 0:
        alarms.append(EXIT_FLAG_ALARM)
    failing = section.get("failing_reviews")
    if not isinstance(failing, list):
        return alarms + ["MAINTENANCE_STATUS_UNAVAILABLE"]
    if failing:
        alarms.append(MAINTENANCE_FAILING_ALARM)
    return alarms


DAY_REVIEW_FAILING_ALARM = "DAY_REVIEW_JEV_FAILING"


def day_review_alarms(section):
    """CRYPTO_24H_REVIEW_V1 (package day-review): DAY_REVIEW_JEV_FAILING while a 24-hour
    review's latest Jev attempt failed and it is undecided (Jev unavailable: the trade exits
    30 minutes after T unless an attempt succeeds). An app without the field raises nothing;
    an unreadable one fails closed (DAY_REVIEW_STATUS_UNAVAILABLE)."""
    if section is None:
        return []
    if not isinstance(section, dict) or section.get("available") is False:
        return ["DAY_REVIEW_STATUS_UNAVAILABLE"]
    failing = section.get("failing_reviews")
    if not isinstance(failing, list):
        return ["DAY_REVIEW_STATUS_UNAVAILABLE"]
    return [DAY_REVIEW_FAILING_ALARM] if failing else []


JEV_BUDGET_ALARMS = {"THROTTLED": "JEV_BUDGET_THROTTLED", "TIGHT": "JEV_BUDGET_TIGHT",
                     "EXHAUSTED": "JEV_BUDGET_EXHAUSTED"}
JEV_BUDGET_UNAVAILABLE_ALARM = "JEV_BUDGET_STATUS_UNAVAILABLE"
JEV_BUDGET_ALERT_SECONDS = 1800  # jev_budget.ALERT_SECONDS: a tier change alerts for 30 minutes.


def jev_budget_alarms(section, now):
    """JEV_SPEND_GUARD_V1 (package jev-budget): a tier change is an off-host alert. For 30
    minutes after the guard entered THROTTLED, TIGHT or EXHAUSTED (``tier_since``) the watchdog
    raises JEV_BUDGET_THROTTLED, _TIGHT or _EXHAUSTED, so the notifier sends one ``/fail`` for the
    change and the success ping resumes afterwards; the tier itself stays in the status. NORMAL
    raises nothing. JEV_BUDGET_STATUS_UNAVAILABLE while the guard cannot decide (guarded reviews
    wait) or the section is unreadable. An app without the field raises nothing; an unreadable
    ``tier_since`` fails closed (the tier's alert)."""
    if section is None:
        return []
    if not isinstance(section, dict) or section.get("available") is not True:
        return [JEV_BUDGET_UNAVAILABLE_ALARM]
    tier = section.get("tier")
    if tier == "NORMAL":
        return []
    code = JEV_BUDGET_ALARMS.get(tier)
    if code is None:
        return [JEV_BUDGET_UNAVAILABLE_ALARM]
    try:
        since = datetime.fromisoformat(section["tier_since"])
        age = (now - since).total_seconds()
    except (KeyError, TypeError, ValueError):
        return [code]
    if since.tzinfo is None or age <= JEV_BUDGET_ALERT_SECONDS:
        return [code]
    return []


GAP_RESUME_OVERDUE_ALARM = "GAP_RESUME_CHECK_OVERDUE"
GAP_RESUME_OVERDUE_SECONDS = 300  # gap_resume.CHECK_OVERDUE_SECONDS, which the status reports.


def gap_resume_alarms(section, now):
    """CRYPTO_GAP_RESUME_V1 (package gap-resume): GAP_RESUME_CHECK_OVERDUE once the oldest setup
    held for its gap check has waited more than 300 s (a healthy reconnect resolves one in
    about two minutes; a held setup cannot enter). An app without the field raises nothing; an
    unreadable field, or a held time that is unreadable or in the future, fails closed."""
    if section is None:
        return []
    if not isinstance(section, dict) or type(section.get("pending_count")) is not int:
        return ["GAP_RESUME_STATUS_UNAVAILABLE"]
    oldest = section.get("oldest_pending_since")
    if oldest is None:
        return [] if section["pending_count"] == 0 else ["GAP_RESUME_STATUS_UNAVAILABLE"]
    try:
        since = datetime.fromisoformat(oldest)
        age = (now - since).total_seconds()
    except (TypeError, ValueError):
        return [GAP_RESUME_OVERDUE_ALARM]
    if since.tzinfo is None or not 0 <= age <= GAP_RESUME_OVERDUE_SECONDS:
        return [GAP_RESUME_OVERDUE_ALARM]
    return []


# Muse provider jobs by lane (the job ID prefix) and the ``runs.kind`` values each lane logs:
# the worker's own kinds plus the lane loop's failure kind (``muse_worker.lane_loop``).
MUSE_JOB_LANE_RUN_KINDS = {
    "report-job": ("RESEARCH", "research"),
    "evidence-job": ("EVIDENCE", "EVIDENCE_CYCLE", "evidence"),
    "news-job": ("NEWS", "news"),
}


def _muse_provider_jobs(connection, stale):
    """``(stuck, failed)`` provider job counts of a Muse spool, from IDs and times only.

    A lane runs one provider job at a time, and every failure path logs a run or writes a
    ``failed-job:<id>`` marker. A job without a result is *current* while its lane is still on
    it: no marker names it, no later job of the lane has started, and the lane has logged no
    run since it started. A current job older than ``muse_max_age_seconds`` is stuck (an
    unreadable start time fails closed). Every other job without a result, and every marked
    job, has failed: counted in the watchdog result, never a lasting alarm.
    """

    def moment(value):
        try:
            at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return at if at.tzinfo is not None else None

    lane_of = "CASE WHEN instr(id,':')>0 THEN substr(id,1,instr(id,':')-1) ELSE id END"
    latest_job = {row[0]: moment(row[1]) for row in connection.execute(
        f"SELECT {lane_of} AS lane,max(started_at) FROM provider_jobs GROUP BY lane")}
    latest_run = {row[0]: moment(row[1]) for row in connection.execute(
        "SELECT kind,max(started_at) FROM runs WHERE kind!='HEARTBEAT' GROUP BY kind")}
    failed = {row[0] for row in connection.execute(
        "SELECT substr(key,12) FROM state WHERE key LIKE 'failed-job:%'")}
    stuck = 0
    for job_id, started_at in connection.execute(
            "SELECT id,started_at FROM provider_jobs WHERE result IS NULL OR completed_at IS NULL"):
        if job_id in failed:
            continue
        started = moment(started_at)
        lane = job_id.split(":", 1)[0] if isinstance(job_id, str) else ""
        later = [latest_job.get(lane)] + [
            latest_run.get(kind) for kind in MUSE_JOB_LANE_RUN_KINDS.get(lane, ())
        ]
        if started is not None and any(at is not None and at > started for at in later):
            failed.add(job_id)  # The lane moved on: the job failed or was abandoned.
        elif stale(started_at):
            stuck += 1
    return stuck, len(failed)


def muse_heartbeat_alarm(path, now, max_age):
    """Bounded, read-only status queries; never read source, request or result text."""
    return muse_spool_status(path, now, max_age)[0]


def muse_spool_status(path, now, max_age):
    """``(alarms, provider_jobs)`` of the Muse spool; ``provider_jobs`` is ``{"stuck": n,
    "failed": n}``, or None when the spool is unreadable. Only a current, stuck provider job
    raises MUSE_WORK_FAILED from the job table (plan phase 0, 2026-09-26); failed jobs stay
    visible as the count. The latest non-heartbeat run failing still raises MUSE_WORK_FAILED
    until a later run succeeds."""
    alarms = []

    def stale(value):
        try:
            at = datetime.fromisoformat(value)
            return at.tzinfo is None or not 0 <= (now - at).total_seconds() <= max_age
        except (TypeError, ValueError):
            return True

    try:
        metadata = Path(path).lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError
        uri = Path(path).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            deadline = time.monotonic() + 1
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            row = connection.execute(
                "SELECT max(started_at) FROM runs WHERE kind='HEARTBEAT' AND outcome='RUNNING'"
            ).fetchone()
            if stale(row[0]):
                alarms.append("MUSE_HEARTBEAT_STALE_OR_UNAVAILABLE")
            pending = connection.execute(
                "SELECT max(attempts),min(created_at),max(created_at),min(deadline) "
                "FROM outbox WHERE delivered_at IS NULL"
            ).fetchone()
            if pending[0] is not None:
                if pending[0] >= 5:  # MuseWorker's explicit current default / CLI contract.
                    alarms.append("MUSE_DELIVERY_EXHAUSTED")
                try:
                    expires = datetime.fromisoformat(pending[3])
                    expired = expires.tzinfo is None or expires <= now
                except (TypeError, ValueError):
                    expired = True
                if stale(pending[1]) or stale(pending[2]) or expired:
                    alarms.append("MUSE_DELIVERY_STALE")
            stuck, failed = _muse_provider_jobs(connection, stale)
            if stuck:
                alarms.append("MUSE_WORK_FAILED")
            latest = connection.execute(
                "SELECT outcome FROM runs WHERE kind!='HEARTBEAT' "
                "ORDER BY started_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            if latest and latest[0] not in {"NO_OP", "PREPARED", "DELIVERED", "RECORDED"}:
                alarms.append("MUSE_WORK_FAILED")
        return sorted(set(alarms)), {"stuck": stuck, "failed": failed}
    except Exception:
        return sorted(set(alarms + ["MUSE_HEARTBEAT_STALE_OR_UNAVAILABLE"])), None


def release_alarms(config, status, *, config_path=None):
    """The running app must report the configured release; tripped launchers stay visible."""
    alarms = []
    if config.get("config_version") == CONFIG_VERSION and isinstance(status, dict):
        if status.get("code_version") != config["release"]["source_sha256"]:
            alarms.append("RELEASE_CODE_MISMATCH")
    if config_path is not None:
        try:
            for path in sorted(launcher_directory(config_path).glob("crash-loop-*.json")):
                component = path.name.removeprefix("crash-loop-").removesuffix(".json")
                if component in COMPONENTS:
                    alarms += ["CRASH_LOOP_BREAKER_TRIPPED", "CRASH_LOOP_" + component.upper()]
        except OSError:
            alarms.append("LAUNCHER_STATE_UNAVAILABLE")
    return alarms


def _notify_tick(config, alarms, now, status, repository, transport):
    """Off-host alerting (plan 4.2): additive, best-effort, never affects trading.

    Returns extra alarm codes (``["NOTIFY_FAILED"]`` or ``[]``) for the caller to merge
    into this tick's alarm file. All the alerting logic lives in ``catalyst_lab.notify``;
    this only supplies what the watchdog already has (the config, this tick's alarms, the
    status response) plus, at most once a day, a read-only audit-head lookup so a broken
    or unreachable ledger can never block the dead-man ping itself.
    """
    from catalyst_lab import notify

    section = config.get("notify") or {}
    audit_head = None
    if notify.due_for_daily_head(section, config["watchdog"]["alarm_file"], now):
        try:
            repo = repository or readonly_audit_repository(config["audit"]["database_url"])
            with repo.connect() as conn:
                row = conn.execute(
                    "SELECT seq, event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
            if row is not None:
                audit_head = (row["seq"], row["event_hash"])
        except Exception:
            audit_head = None  # Best-effort only; the dead-man ping must still go out.
    extra, _sent = notify.watchdog_tick(
        section, alarms=alarms, now=now, alarm_file=config["watchdog"]["alarm_file"],
        audit_head=audit_head,
        release_commit=status.get("release_commit") if isinstance(status, dict) else None,
        transport=transport,
    )
    return extra


def watchdog_once(config, *, transport=None, now=None, config_path=None, repository=None):
    now = now or datetime.now(UTC)
    policy = config["watchdog"]
    status = None
    try:
        # v2 reads status with the GET-only status token; v1 used its single shared token.
        token = role_token(config["status"]["token_file"]
                           if config.get("config_version") == CONFIG_VERSION
                           else policy["token_file"])
        deadline = time.monotonic() + policy["timeout_seconds"]
        with httpx.Client(timeout=policy["timeout_seconds"], trust_env=False,
                          follow_redirects=False, transport=transport) as client:
            with client.stream("GET", loopback_url(policy["api"]) + "/api/v1/lab/status",
                               headers={"Authorization": "Bearer " + token}) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > LIMIT or time.monotonic() > deadline:
                        raise ValueError
                status = strict_json(bytes(body))
    except Exception:
        pass  # Deliberately discard provider text, token, URL and exception details.
    muse_alarms, muse_jobs = muse_spool_status(
        config["muse"]["spool"], now, policy["muse_max_age_seconds"]
    )
    alarms = status_alarms(status, now, policy) + muse_alarms + release_alarms(
        config, status, config_path=config_path
    )
    alarms = alarms + _notify_tick(config, alarms, now, status, repository, transport)
    # ``muse_provider_jobs`` keeps failed Muse jobs visible without a lasting alarm.
    result = {"checked_at": now.isoformat(), "alarms": sorted(set(alarms)),
              "mode": "PAPER_ONLY", "scope": "LOCAL_STATUS_WATCHDOG",
              "muse_provider_jobs": muse_jobs}
    atomic_private_json(policy["alarm_file"], result)
    return result


CHECKPOINT_SCOPE = "AUDIT_EVIDENCE_ONLY_NOT_DATABASE_BACKUP"
CHECKPOINT_FULL_EVERY = timedelta(days=7)  # The weekly full export (plan 4.7).
CHECKPOINT_LIMIT = 1024 * 1024 * 1024
CHECKPOINT_STAMP = "%Y%m%dT%H%M%S%fZ"


def _checkpoint_rows_after(repository, start_seq, start_hash):
    """Rows after ``start_seq``, shaped exactly like ``Repository.export_events``.

    The database must still hold ``start_hash`` at ``start_seq``: an audit log that no
    longer contains the previous checkpoint's head cannot be extended from it.
    """
    from catalyst_lab.repository import json_safe

    with repository.connect() as conn:
        anchor = conn.execute(
            "SELECT event_hash FROM lab.trade_events WHERE seq=%s", (start_seq,)
        ).fetchone()
        if anchor is None or anchor["event_hash"] != start_hash:
            raise ValueError("AUDIT_CHECKPOINT_CHAIN_MISMATCH")
        rows = conn.execute(
            "SELECT * FROM lab.trade_events WHERE seq>%s ORDER BY seq", (start_seq,)
        ).fetchall()
    result = []
    for row in rows:
        item = json_safe(row)
        item["created_at"] = row["created_at"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result.append(item)
    return result


def _checkpoint_meta(data_path, manifest):
    """The chain position a manifest records; pre-incremental manifests are full exports."""
    try:
        mode = manifest.get("mode", "FULL")
        start_seq = manifest.get("start_seq", 0)
        start_hash = manifest.get("start_hash", ZERO_HASH)
        count, head = manifest["event_count"], manifest["head_hash"]
        last_seq = manifest.get("last_seq", start_seq + count)
        if (
            mode not in {"FULL", "INCREMENTAL"}
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 0
                   for v in (start_seq, count, last_seq))
            or last_seq != start_seq + count
            or (mode == "FULL") != (start_seq == 0)
            or not isinstance(head, str) or not SHA256_HEX.fullmatch(head)
            or not isinstance(start_hash, str) or not SHA256_HEX.fullmatch(start_hash)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("AUDIT_CHECKPOINT_MANIFEST_INVALID") from None
    try:  # Only the chain order needs the time; a restored copy may carry another name.
        created = manifest.get("created_at")
        created_at = (
            datetime.fromisoformat(created) if isinstance(created, str)
            else datetime.strptime(data_path.stem, CHECKPOINT_STAMP).replace(tzinfo=UTC)
        )
        if created_at.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        created_at = None
    return {"file": data_path, "manifest": manifest, "mode": mode, "start_seq": start_seq,
            "start_hash": start_hash, "last_seq": last_seq, "head_hash": head,
            "created_at": created_at}


def _checkpoint_chain(directory):
    """Every checkpoint in ``directory``, in the order it was written."""
    chain = []
    for manifest_path in Path(directory).glob("*.manifest.json"):
        stem = manifest_path.name.removesuffix(".manifest.json")
        manifest = strict_json(private_bytes(manifest_path))
        item = _checkpoint_meta(manifest_path.with_name(stem + ".jsonl"), manifest)
        if item["created_at"] is None:
            raise ValueError("AUDIT_CHECKPOINT_MANIFEST_INVALID")
        chain.append(item)
    return sorted(chain, key=lambda item: (item["created_at"], item["file"].name))


def export_checkpoint(repository, directory, *, now=None, full=None):
    """Write the audit events after the previous checkpoint's head, chained to it.

    The first checkpoint in ``directory``, one requested with ``full=True`` and, by
    default, the first once the newest full export is seven days old (the weekly full
    export) hold the whole chain from the genesis. Every other checkpoint holds only the
    events after the previous checkpoint's head and records that head, so the files verify
    end to end as one chain (``verify_checkpoint`` on the directory). Nothing is written
    when no event follows the previous head. A checkpoint whose text matches a credential
    shape is refused before any byte is written; the refusal names no value.
    """
    now = now or datetime.now(UTC)
    target = _private_directory(directory)
    chain = _checkpoint_chain(target)
    previous = chain[-1] if chain else None
    fulls = [item["created_at"] for item in chain if item["mode"] == "FULL"]
    if full is None:
        full = previous is None or not fulls or now - max(fulls) >= CHECKPOINT_FULL_EVERY
    if full:
        start_seq, start_hash = 0, ZERO_HASH
        rows = repository.export_events()
    else:
        start_seq, start_hash = previous["last_seq"], previous["head_hash"]
        rows = _checkpoint_rows_after(repository, start_seq, start_hash)
    verified = verify_events(rows, start_seq=start_seq, start_hash=start_hash)
    if full and previous is not None and (
        previous["last_seq"] > verified["last_seq"]
        or (previous["last_seq"]
            and rows[previous["last_seq"] - 1]["event_hash"] != previous["head_hash"])
    ):
        raise ValueError("AUDIT_CHECKPOINT_CHAIN_MISMATCH")  # History differs from the chain.
    mode = "FULL" if full else "INCREMENTAL"
    if not rows and not full:
        return {"file": None, "written": False, **verified, "mode": mode,
                "scope": CHECKPOINT_SCOPE}
    raw = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in rows)
    if credential_findings(raw.decode()):
        raise ValueError("AUDIT_CHECKPOINT_CREDENTIAL_SHAPED_CONTENT")
    path = target / (now.strftime(CHECKPOINT_STAMP) + ".jsonl")
    manifest = {
        **verified, "mode": mode, "start_hash": start_hash, "created_at": now.isoformat(),
        "previous_checkpoint": previous["file"].name if previous else None,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "credential_scan": "NO_CREDENTIAL_SHAPED_CONTENT", "scope": CHECKPOINT_SCOPE,
    }
    _exclusive(path, raw)
    _exclusive(path.with_suffix(".manifest.json"), json.dumps(manifest, sort_keys=True).encode())
    return {"file": str(path), "written": True, **manifest}


def _verify_checkpoint_file(path, manifest, expected_head=None):
    meta = _checkpoint_meta(path, manifest)
    raw = private_bytes(path, limit=CHECKPOINT_LIMIT)
    if hashlib.sha256(raw).hexdigest() != manifest["file_sha256"]:
        raise ValueError("AUDIT_FILE_HASH_MISMATCH")
    rows = [json.loads(line) for line in raw.splitlines()]
    proof = verify_events(rows, expected_head or meta["head_hash"],
                          start_seq=meta["start_seq"], start_hash=meta["start_hash"])
    if proof["event_count"] != manifest["event_count"]:
        raise ValueError("AUDIT_EVENT_COUNT_MISMATCH")
    return meta, rows, proof


def verify_checkpoint(path, *, expected_head=None):
    """Verify one checkpoint file, or every checkpoint of a directory as one chain.

    A file verifies from the start its manifest records (the genesis for a full export).
    For a directory, each incremental checkpoint must start exactly at the previous
    checkpoint's head and each full export must contain that head, so a missing,
    reordered or altered checkpoint fails. ``expected_head`` is an independently retained
    head; for a directory it must equal the newest checkpoint's head.
    """
    path = Path(path)
    if not path.is_dir():
        manifest = strict_json(private_bytes(path.with_suffix(".manifest.json")))
        _, _, proof = _verify_checkpoint_file(path, manifest, expected_head)
        return {**proof, "independent_head_verified": expected_head is not None,
                "scope": CHECKPOINT_SCOPE}
    chain = _checkpoint_chain(path)
    if not chain:
        raise ValueError("AUDIT_CHECKPOINT_MISSING")
    last_seq, head = 0, ZERO_HASH
    for item in chain:
        meta, rows, _ = _verify_checkpoint_file(item["file"], item["manifest"])
        if meta["mode"] == "INCREMENTAL":
            if meta["start_seq"] != last_seq:
                raise ValueError("AUDIT_CHECKPOINT_CHAIN_GAP")
            if meta["start_hash"] != head:
                raise ValueError("AUDIT_CHECKPOINT_CHAIN_MISMATCH")
        elif last_seq > meta["last_seq"] or (
            last_seq and rows[last_seq - 1]["event_hash"] != head
        ):
            raise ValueError("AUDIT_CHECKPOINT_CHAIN_MISMATCH")
        last_seq, head = meta["last_seq"], meta["head_hash"]
    if expected_head is not None and expected_head != head:
        raise ValueError("AUDIT_CHECKPOINT_HEAD_MISMATCH")
    return {"valid": True, "checkpoint_count": len(chain), "event_count": last_seq,
            "last_seq": last_seq, "head_hash": head,
            "full_exports": sum(item["mode"] == "FULL" for item in chain),
            "independent_head_verified": expected_head is not None,
            "scope": CHECKPOINT_SCOPE}


def restore_audit_copy(path, destination, *, expected_head):
    """Verify and recover immutable evidence bytes; never write into a database."""
    proof = verify_checkpoint(path, expected_head=expected_head)
    source, destination = Path(path), Path(destination)
    _exclusive(destination, private_bytes(source, limit=1024 * 1024 * 1024))
    _exclusive(destination.with_suffix(".manifest.json"),
               private_bytes(source.with_suffix(".manifest.json")))
    return proof


def readonly_audit_repository(database_url):
    from catalyst_lab.repository import Repository

    class ReadOnlyAuditRepository(Repository):
        def connect(self):
            connection = super().connect()
            connection.execute("SET TRANSACTION READ ONLY")
            return connection

    return ReadOnlyAuditRepository(database_url)


OPERATOR_ROLE = "catalyst_operator"
OPERATOR_ACTIONS = ("list-halts", "pause", "resume", "release-halt", "flatten-all", "status")
REFUSAL_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def operator_repository(database_url):
    """Separate operator login: no table grants, only audited SECURITY DEFINER functions."""
    from catalyst_lab.repository import Repository

    class OperatorRepository(Repository):
        def check_role(self):
            with self.connect() as conn:
                row = conn.execute("""SELECT current_user AS role,rolsuper,rolcreaterole,
                    rolcreatedb,has_schema_privilege(current_user,'lab','CREATE') AS ddl,
                    EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE n.nspname='lab' AND c.relkind IN ('r','v','m','p')
                      AND has_table_privilege(current_user,c.oid,
                        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) AS tables,
                    to_regprocedure('lab.operator_list_halts(boolean)') IS NULL AS missing
                    FROM pg_roles WHERE rolname=current_user""").fetchone()
            if row["role"] != OPERATOR_ROLE or any(v for k, v in row.items() if k != "role"):
                raise RuntimeError("OPERATOR_ROLE_REQUIRED")

    return OperatorRepository(database_url)


def operator_command(repository, action, *, reason=None, halt_id=None, correction_seq=None,
                     accept_residual=False, include_released=False):
    """Each mutating call appends its own hash-chained event inside the database.

    ``flatten-all`` records the request only; the running app's next account-safety tick
    executes it (migration 019). ``list-halts`` and ``status`` also show the flatten requests
    (pending ones, and completed ones with ``--all`` or in ``status``) with their latest outcome.
    """
    from catalyst_lab.repository import json_safe

    if action not in OPERATOR_ACTIONS:
        raise ValueError("UNKNOWN_OPERATOR_ACTION")
    repository.check_role()
    with repository.connect() as conn:
        if action in {"list-halts", "status"}:
            rows = conn.execute(
                "SELECT * FROM lab.operator_list_halts(%s)",
                (include_released and action == "list-halts",),
            ).fetchall()
            flattens = conn.execute(
                "SELECT * FROM lab.operator_list_flattens(%s)",
                (include_released or action == "status",),
            ).fetchall()
            pending = sum(row["pending"] for row in flattens)
            result = {"halts": rows, "active_count": sum(not r["released"] for r in rows),
                      "flattens": flattens, "pending_flatten_count": pending}
            if action == "status":
                result = {
                    "active_count": result["active_count"],
                    "active_halts": [
                        {key: row[key] for key in ("halt_id", "reason", "release_requirement",
                                                   "release_blockers")}
                        for row in rows
                    ],
                    "pending_flatten_count": pending,
                    "flattens": flattens,
                }
        elif action == "pause":
            halt = conn.execute("SELECT lab.operator_pause(%s) AS id", (reason,)).fetchone()["id"]
            result = {"paused": True, "halt_id": halt, "scope": "ENTRY_ONLY"}
        elif action == "resume":
            ids = conn.execute("SELECT lab.operator_resume(%s) AS ids", (reason,)).fetchone()
            result = {"resumed": True, "released_halt_ids": ids["ids"]}
        elif action == "release-halt":
            if accept_residual:  # Explicit RESIDUAL_ACCEPTED record, same transaction.
                conn.execute("SELECT lab.operator_accept_residual(%s,%s)", (halt_id, reason))
            result = conn.execute(
                "SELECT lab.operator_release_halt(%s,%s,%s) AS result",
                (halt_id, reason, correction_seq),
            ).fetchone()["result"]
        else:
            request = conn.execute(
                "SELECT lab.operator_request_flatten_all(%s) AS id", (reason,)
            ).fetchone()["id"]
            recorded = conn.execute(
                "SELECT request_seq FROM lab.operator_list_flattens(true) WHERE request_id=%s",
                (request,),
            ).fetchone()
            # Recorded here; the running app's next account-safety tick cancels and closes.
            result = {"flatten_request_id": request, "recorded": True,
                      "broker_action": "PENDING_ACCOUNT_SAFETY_TICK",
                      "request_seq": recorded["request_seq"]}
    return json_safe({"action": action, "mode": "PAPER_ONLY", **result})


def operator_main(argv):
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--database-url", help="catalyst_operator DSN; else OPERATOR_DATABASE_URL")
    parser = argparse.ArgumentParser(
        prog="catalyst_lab.managed_ops operator",
        description="Audited operator controls over a separate catalyst_operator connection.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list-halts", parents=[shared]).add_argument(
        "--all", action="store_true", dest="include_released"
    )
    actions.add_parser("status", parents=[shared])
    for name in ("pause", "resume", "flatten-all"):
        actions.add_parser(name, parents=[shared]).add_argument("--reason", required=True)
    release = actions.add_parser("release-halt", parents=[shared])
    release.add_argument("halt_id", type=int)
    release.add_argument("--reason", required=True)
    release.add_argument("--correction-seq", type=int)
    release.add_argument("--accept-residual", action="store_true")
    args = parser.parse_args(argv)
    try:
        url = args.database_url or os.environ.get("OPERATOR_DATABASE_URL")
        if not url:
            raise RuntimeError("OPERATOR_DATABASE_URL_REQUIRED")
        result = operator_command(
            operator_repository(url), args.action, reason=getattr(args, "reason", None),
            halt_id=getattr(args, "halt_id", None),
            correction_seq=getattr(args, "correction_seq", None),
            accept_residual=getattr(args, "accept_residual", False),
            include_released=getattr(args, "include_released", False),
        )
    except Exception as exc:
        # Only this schema's own refusal codes are shown; DSNs and driver text never are.
        import psycopg

        code = None
        if isinstance(exc, psycopg.errors.RaiseException):
            code = getattr(exc.diag, "message_primary", None)
        elif isinstance(exc, psycopg.errors.InsufficientPrivilege):
            code = "OPERATOR_PRIVILEGE_REQUIRED"
        elif isinstance(exc, RuntimeError) and not isinstance(exc, psycopg.Error):
            code = str(exc)
        if code and REFUSAL_CODE.fullmatch(code):
            raise SystemExit("OPERATOR_COMMAND_REFUSED: " + code) from None
        raise SystemExit("OPERATOR_COMMAND_FAILED") from None
    print(json.dumps(result, sort_keys=True))
    return result


def clear_protection_latch(config, reason, *, repository=None, operator="LOCAL_OWNER_CLI"):
    """Append one audited OPERATOR_PROTECTION_LATCH_CLEARED event; change nothing else.

    The running runtime releases only operator-only protection latches (invariant
    failures and MANAGED_PROTECTION_PERSISTENT_FAILURE) whose own latch event precedes
    this one. Durable execution halts stay until their own release step. The key is
    derived from the ledger head and the reason, so a retried command is idempotent.
    ``operator`` records where the owner ran it: this Mac's CLI, or the Railway trader's
    shell (``cloud_runtime clear-protection-latch``, package cloud-hardening).
    """
    if operator not in {"LOCAL_OWNER_CLI", "RAILWAY_TRADER_SHELL"}:
        raise ValueError("OPERATOR_ORIGIN_INVALID")
    text = reason.strip() if isinstance(reason, str) else ""
    if not 10 <= len(text) <= 500 or any(ord(c) < 32 for c in text):
        raise ValueError("OPERATOR_REASON_REQUIRED")
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.managed_latches import OPERATOR_CLEAR_EVENT
    from catalyst_lab.managed_store import ManagedStore

    store = ManagedStore(
        repository or RiskRepository(config["environment"]["MANAGED_DATABASE_URL"])
    )
    with store.transaction() as conn:
        head = conn.execute(
            "SELECT coalesce(max(event_seq),0) AS seq FROM lab.managed_events WHERE kind<>%s",
            (OPERATOR_CLEAR_EVENT,),
        ).fetchone()["seq"]
        body = {
            "reason": text,
            "operator": operator,
            "scope": "OPERATOR_ONLY_PROTECTION_LATCHES",
            "ledger_head_event_seq": head,
        }
        key = (
            f"operator-protection-latch-clear:{head}:"
            + hashlib.sha256(text.encode()).hexdigest()[:16]
        )
        row = store.event(conn, OPERATOR_CLEAR_EVENT, body, key=key)
    return {"event_seq": row["event_seq"], "kind": OPERATOR_CLEAR_EVENT,
            "ledger_head_event_seq": head, "scope": body["scope"]}


def role_token(path):
    """One owner-only role token; the value is never logged, reported or put in argv."""
    try:
        token = private_bytes(path, limit=4096).decode().strip()
    except ValueError:
        raise ValueError("PRIVATE_ROLE_TOKEN_INVALID") from None
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("PRIVATE_ROLE_TOKEN_INVALID")
    return token


def role_tokens(config):
    tokens = {role: role_token(config[role]["token_file"]) for role in TOKEN_ROLES}
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("ROLE_TOKENS_NOT_SEPARATED")
    return tokens


def agent_tokens(config, roles):
    """``{agent_id: token}`` from the ``agents`` token files, each distinct from every other
    agent and role token (``roles`` from ``role_tokens``); values are never logged."""
    tokens = {entry["agent_id"]: role_token(entry["token_file"])
              for entry in config.get("agents", [])}
    if len(set(tokens.values())) != len(tokens) or set(tokens.values()) & set(roles.values()):
        raise ValueError("AGENT_TOKENS_NOT_SEPARATED")
    return tokens


def launcher_directory(config_path):
    """Launcher state sits beside the private config, so the breaker works even when the
    configuration itself cannot be parsed."""
    return Path(os.path.abspath(config_path)).parent / "launcher-state"


def crash_loop_guard(config_path, component, *, now):
    """Record this start. The 5th start within 10 minutes trips: an alarm file is written and
    the launcher exits 0, so launchd's KeepAlive(SuccessfulExit=false) stops restarting. Once
    the window has passed, the next start runs normally and clears the alarm."""
    record = {"component": component, "threshold": CRASH_LOOP_STARTS,
              "window_seconds": CRASH_LOOP_WINDOW_SECONDS, "mode": "PAPER_ONLY",
              "scope": "LOCAL_LAUNCHER_CRASH_LOOP_BREAKER"}
    try:
        directory = _private_directory(launcher_directory(config_path))
        state = directory / f"{component}.starts.json"
        alarm = directory / f"crash-loop-{component}.json"
        try:
            starts = strict_json(private_bytes(state))["starts"]
            if not isinstance(starts, list) or any(
                type(t) not in (int, float) for t in starts
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            starts = []  # A missing or damaged state file counts as no earlier starts.
        window = CRASH_LOOP_WINDOW_SECONDS
        starts = [t for t in starts if now - window < t <= now + window][-4 * CRASH_LOOP_STARTS:]
        starts.append(now)
        atomic_private_json(state, {**record, "starts": starts})
        count = sum(now - window < t <= now for t in starts)
        if count >= CRASH_LOOP_STARTS:
            record.update(tripped=True, starts_in_window=count,
                          tripped_at=datetime.fromtimestamp(now, UTC).isoformat(),
                          alarms=["CRASH_LOOP_BREAKER_TRIPPED", "CRASH_LOOP_" + component.upper()])
            atomic_private_json(alarm, record)
            return record
        if os.path.lexists(alarm):
            alarm.unlink()
        return {**record, "tripped": False, "starts_in_window": count}
    except (OSError, ValueError):
        # Without durable start records a crash loop cannot be bounded: stop instead.
        return {**record, "tripped": True, "alarms": ["CRASH_LOOP_STATE_UNAVAILABLE"]}


class RotatingLog:
    """Owner-only (0600) append-only log rotated by size into ``.1`` … ``.N``; never /dev/null."""

    def __init__(self, path, *, max_bytes, backups):
        self.path = Path(path)
        _private_directory(self.path.parent)
        self.max_bytes, self.backups = max_bytes, backups
        self.fd, self.size, self.dropped = None, 0, 0
        self._open()
        if self.size >= self.max_bytes:
            self._rotate()

    def _open(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("PRIVATE_LOG_FILE_REQUIRED")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        self.fd, self.size = fd, info.st_size

    def _rotate(self):
        self.close()
        for index in range(self.backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            if os.path.lexists(older):
                os.replace(older, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        self._open()

    def write(self, data):
        """Never raises: a logging failure must not stop or block the supervised process."""
        view = memoryview(bytes(data))
        try:
            if self.fd is None:
                self._open()
            while view:
                if self.size and self.size + len(view) > self.max_bytes:
                    self._rotate()
                written = os.write(self.fd, view[:max(self.max_bytes - self.size, 1)])
                self.size += written
                view = view[written:]
        except (OSError, ValueError):
            self.dropped += len(view)
            self.close()

    def event(self, name, **fields):
        details = " ".join(f"{k}={v}" for k, v in sorted(fields.items()) if v is not None)
        line = f"{datetime.now(UTC).isoformat()} LAUNCHER {name} {details}".rstrip()
        self.write(line.encode() + b"\n")

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def run_logged(argv, env, log, *, term_signal=signal.SIGTERM, popen=subprocess.Popen):
    """Supervise one child: its stdout and stderr go to the rotating private log, launchd's
    SIGTERM is forwarded as ``term_signal`` and the child's exit status is returned."""
    child, pending = None, []

    def forward(signum, _frame):
        if child is None:
            pending.append(signum)
            return
        try:
            child.send_signal(term_signal if signum == signal.SIGTERM else signum)
        except OSError:
            pass

    handled = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {signum: signal.signal(signum, forward) for signum in handled}
    try:
        child = popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, close_fds=True)
        for signum in pending:
            forward(signum, None)
        log.event("CHILD_STARTED", pid=child.pid)
        stream = child.stdout.fileno()
        while chunk := os.read(stream, 65536):
            log.write(chunk)
        code = child.wait()
    except BaseException:
        if child is not None and child.poll() is None:  # Never leave an unsupervised child.
            child.send_signal(term_signal)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child is not None:
            child.stdout.close()
    log.event("CHILD_EXITED", code=code, dropped_log_bytes=log.dropped or None)
    return code if code >= 0 else 128 - code


def database_ready(dsn):
    """One bounded connection probe; the DSN and driver messages are never logged."""
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=2, autocommit=True) as connection:
            connection.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def wait_for_database(dsn, *, max_seconds, probe=database_ready, sleep=time.sleep,
                      clock=time.monotonic):
    """Exponential backoff (1, 2, 4 … capped at 15 s) bounded by ``max_seconds`` in total."""
    started, delay, attempts = clock(), 1.0, 0
    while True:
        attempts += 1
        if probe(dsn):
            return {"database": "READY", "attempts": attempts,
                    "waited_seconds": round(clock() - started, 3)}
        remaining = max_seconds - (clock() - started)
        if remaining <= 0:
            raise ValueError("DATABASE_UNAVAILABLE_AFTER_BOUNDED_WAIT")
        sleep(min(delay, remaining))
        delay = min(delay * 2, DATABASE_BACKOFF_CAP_SECONDS)


def backup_once(config, *, now=None):
    """The daily backup component (plan 4.6): one read-only backup of the marked account ledger
    into ``backup.directory``, then retention. It never restores, migrates or writes the ledger;
    the restore drill stays an owner command (``python -m catalyst_lab.ledger_ops drill``)."""
    from catalyst_lab import ledger_ops

    ledger, policy = config["ledger"], config["backup"]
    root = Path(ledger["marker_file"]).parent
    # ledger_ops derives the cluster and socket paths from the ledger root.
    if (Path(ledger["data_directory"]) != root / "postgres"
            or Path(ledger["socket_directory"]) != root / "socket"):
        raise ValueError("LEDGER_LAYOUT_UNSUPPORTED_FOR_BACKUP")
    try:
        # The configuration names the owner ledger by its marker file, so the flag is deliberate.
        taken = ledger_ops.backup(root, policy["directory"], allow_owner_ledger=True, now=now)
        verified = ledger_ops.verify_manifest(
            taken["backup"], expected_manifest_sha256=taken["manifest_sha256"]
        )
        pruned = ledger_ops.prune(policy["directory"], policy["retention_days"], now=now)
    except ledger_ops.LedgerOpsError as error:
        raise ValueError(str(error)) from None
    return {"backup": taken, "verify": verified, "prune": pruned}


def private_standard_streams():
    """launchd may create StandardOut/ErrorPath files world-readable; tighten them to 0600."""
    for fd in (1, 2):
        try:
            info = os.fstat(fd)
            if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) & 0o077):
                os.fchmod(fd, 0o600)
        except OSError:
            pass


def _refusal_code(exc):
    text = str(exc)
    return text if REFUSAL_CODE.fullmatch(text) else type(exc).__name__.upper()


def launch_component(config_path, component, *, execute=None, probe=database_ready,
                     sleep=time.sleep, clock=time.time, monotonic=time.monotonic,
                     package_dir=None):
    """Start one component of the private paper stack from the verified release.

    Order: crash-loop breaker, v2 configuration, rotating private log, release hash, then per
    component the ledger's ACCOUNT_LEDGER marker, role tokens, and a bounded wait for the
    database before the app or Muse. Tests inject ``execute`` instead of running a child.
    """
    if component not in COMPONENTS:
        raise ValueError("UNKNOWN_PRIVATE_COMPONENT")
    breaker = crash_loop_guard(config_path, component, now=clock())
    if breaker["tripped"]:
        print(" ".join(breaker["alarms"]), file=sys.stderr)
        return breaker
    config = load_private_config(config_path)
    if config.get("config_version") != CONFIG_VERSION:
        raise ValueError("PRIVATE_CONFIG_V2_REQUIRED")
    logs = config["logs"]
    log = RotatingLog(Path(logs["directory"]) / f"{component}.log",
                      max_bytes=logs["max_bytes"], backups=logs["backups"])
    try:
        release = verify_release(config, package_dir=package_dir)
        log.event("START", component=component, release_commit=release["release_commit"],
                  source_sha256=release["source_sha256"])
        env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
               if k in os.environ}
        term_signal = signal.SIGTERM
        if component == "ledger":
            from catalyst_lab.localdb import ledger_role

            ledger = config["ledger"]
            if ledger_role(Path(ledger["marker_file"]).parent) != "ACCOUNT_LEDGER":
                raise ValueError("LEDGER_ACCOUNT_MARKER_REQUIRED")
            # Foreground postmaster from the same installation as pg_ctl; SIGINT = fast shutdown.
            argv = [str(Path(ledger["pg_ctl"]).with_name("postgres")),
                    "-D", ledger["data_directory"], "-k", ledger["socket_directory"]]
            term_signal = signal.SIGINT
        elif component in {"app", "muse"}:
            from catalyst_lab.localdb import ledger_retirement

            # A retired ledger (moved to the cloud, package cloud) must never get an executor
            # again: a second executor there would trade the same paper account.
            if ledger_retirement(Path(config["ledger"]["marker_file"]).parent) is not None:
                raise ValueError("LEDGER_RETIRED")
            tokens = role_tokens(config) if component == "app" else None
            agents = agent_tokens(config, tokens) if component == "app" else None
            dsn = config["environment"].get("MANAGED_DATABASE_URL")
            if not dsn or _placeholder(dsn):  # An empty DSN would probe libpq's defaults.
                raise ValueError("MANAGED_DATABASE_URL_REQUIRED")
            waited = wait_for_database(dsn, max_seconds=config["ledger"]["wait_seconds"],
                                       probe=probe, sleep=sleep, clock=monotonic)
            log.event("DATABASE_READY", attempts=waited["attempts"],
                      waited_seconds=waited["waited_seconds"])
            if component == "app":
                env.update(config["environment"])
                env.update(MANAGED_API_TOKEN=tokens["muse"], MANAGED_STATUS_TOKEN=tokens["status"],
                           MANAGED_OPERATOR_TOKEN=tokens["operator"])
                if agents:
                    env["MANAGED_AGENT_TOKENS_JSON"] = json.dumps(agents, sort_keys=True)
                # Re-read once the app holds the executor lease (package cloud-hardening).
                env["CATALYST_LEDGER_MARKER"] = config["ledger"]["marker_file"]
                argv = [sys.executable, "-m", "catalyst_lab.managed_app"]
            else:
                muse = config["muse"]
                argv = [sys.executable, "-m", "catalyst_lab.muse_worker", "--api", muse["api"],
                        "--token-file", muse["token_file"], "--spool", muse["spool"],
                        "--us-interval-seconds", str(muse["us_interval_seconds"]),
                        "--crypto-interval-seconds", str(muse["crypto_interval_seconds"])]
                argv.extend("--command=" + arg for arg in muse["command"])
        elif component == "watchdog":
            while True:
                watchdog_once(config, config_path=config_path)
                sleep(config["watchdog"]["interval_seconds"])
        elif component == "audit":
            repository = readonly_audit_repository(config["audit"]["database_url"])
            repository.check_role()
            result = export_checkpoint(repository, config["audit"]["directory"])
            log.event("AUDIT_EXPORTED", event_count=result["event_count"])
            return result
        else:
            return backup_once(config)
        os.umask(0o077)
        if execute is not None:
            return execute(argv[0], argv, env)
        return run_logged(argv, env, log, term_signal=term_signal)
    except Exception as exc:
        log.event("REFUSED", component=component, code=_refusal_code(exc))
        raise
    finally:
        log.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["operator"]:
        return operator_main(argv[1:])
    parser = argparse.ArgumentParser(
        description=__doc__, epilog="Operator controls: managed_ops operator --help"
    )
    parser.add_argument("command", choices=("preflight", "render-supervisor", "template",
                                             "watchdog-once", "verify-checkpoint", "restore-audit",
                                             "clear-protection-latch", "backup"))
    parser.add_argument("--config")
    parser.add_argument("--checkout", help="ignored: code identity is the imported package")
    parser.add_argument("--destination")
    parser.add_argument("--file")
    parser.add_argument("--expected-head")
    parser.add_argument("--check-database", action="store_true")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            result = config_template(args.destination)
        elif args.command == "verify-checkpoint":
            result = verify_checkpoint(args.file, expected_head=args.expected_head)
        elif args.command == "restore-audit":
            if not args.expected_head:
                raise ValueError("INDEPENDENT_HEAD_REQUIRED")
            result = restore_audit_copy(
                args.file, args.destination, expected_head=args.expected_head
            )
        else:
            config = load_private_config(args.config)
            if args.command == "preflight":
                repository = readonly_audit_repository(config["audit"]["database_url"]) \
                    if args.check_database else None
                result = preflight(config, repository=repository)
            elif args.command == "render-supervisor":
                result = render_supervisor(args.config, config, args.destination)
            elif args.command == "clear-protection-latch":
                result = clear_protection_latch(config, args.reason)
            elif args.command == "backup":
                result = backup_once(config)
            else:
                result = watchdog_once(config, config_path=args.config)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        # Only this module's own refusal codes are shown; paths, DSNs and values never are.
        code = str(exc)
        raise SystemExit("PRIVATE_OPERATIONS_FAILED" + (
            ": " + code if REFUSAL_CODE.fullmatch(code) else "")) from None


if __name__ == "__main__":
    main()
