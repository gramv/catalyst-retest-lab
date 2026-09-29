"""Off-host alerts (plan package 4.2): a dead-man ping and alarm forwarding to an
owner-provided Healthchecks.io-style ping URL, with the account choice left to the owner.

Scope and safety properties, all deliberate:

- The notifier can never influence trading. Every failure here becomes the watchdog alarm
  code ``NOTIFY_FAILED`` in the existing local alarm file; it never becomes a halt, a retry
  against the broker, or any other change to a trading decision.
- Payloads carry codes and counts only: alarm codes, an audit sequence number, an audit
  head hash, a release commit and a timestamp. Never evidence text, symbols, prices,
  credentials or the ping URL itself. :func:`build_payload` and the redaction check inside
  :class:`Notifier` are the two points that enforce this.
- The ping URL lives in an owner-only mode-0600 file named by ``config["notify"]``, read
  fresh through :func:`catalyst_lab.managed_ops.private_bytes` on every send (never cached
  across ticks), so rotating the file takes effect on the next tick. Its content must be
  exactly one ``https`` URL whose host is in the configured allowlist; anything else is
  refused, both when the private configuration loads (:func:`validate_notify_section`,
  called from ``managed_ops._validate_v2``) and again on every send.
- An empty ``{}`` section is a fully valid, complete configuration: it means "no off-host
  notifier" and disables this module entirely. Nothing is read or written.
- All local state (last known alarm codes, last reminder time, last daily-head date) lives
  in one mode-0600 JSON file beside the watchdog's alarm file. A missing or corrupt state
  file resets safely to "no prior alarms, never notified".
"""

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from catalyst_lab.jev_contract import strict_json
from catalyst_lab.managed_ops import (
    COMMIT_HEX,
    REFUSAL_CODE,
    SHA256_HEX,
    atomic_private_json,
    private_bytes,
)

NOTIFY_KEYS = frozenset({
    "provider", "ping_url_file", "allowed_hosts", "reminder_minutes",
    "daily_head_hour_utc", "timeout_seconds", "local_notification",
})
# The Railway ops service (package cloud): the URL arrives as a secret variable instead of a
# 0600 file, and a container has no desktop to raise a local banner on.
ENVIRONMENT_URL_KEYS = NOTIFY_KEYS - {"ping_url_file", "local_notification"}
PROVIDERS = frozenset({"HEALTHCHECKS"})
ALARM_CODE = REFUSAL_CODE  # Same shape: one upper-case word, digits/underscore, <=64 chars.
HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+\Z"
)
PAYLOAD_KEYS = frozenset({"alarms", "audit_seq", "audit_head_hash", "release_commit", "at"})
PAYLOAD_BYTES_LIMIT = 2048
PING_URL_FILE_LIMIT = 2048
NOTIFICATION_TITLE = "Catalyst Watchdog"
REMINDER_MINUTES_BOUNDS = (1, 1440)
DAILY_HOUR_BOUNDS = (0, 23)
TIMEOUT_SECONDS_BOUNDS = (1, 30)


class NotifyError(ValueError):
    """A refusal code. Never carries the ping URL, evidence text or a credential."""


def validate_notify_section(section, *, environment_url=False):
    """Exact keys, typed and bounded. ``{}`` is valid: no off-host notifier is configured.

    Called both from ``managed_ops._validate_v2`` (private configuration load) and again
    from every send in this module, so a section that was valid at load but is damaged on
    disk later (or a URL file that fails its own check) is still refused at send time.
    ``environment_url`` (the Railway ops service) takes the URL from a secret variable: the
    section then has neither ``ping_url_file`` nor ``local_notification``.
    """
    if not isinstance(section, dict):
        raise NotifyError("NOTIFY_CONFIG_INVALID")
    if not section:
        return section
    if set(section) != (ENVIRONMENT_URL_KEYS if environment_url else NOTIFY_KEYS):
        raise NotifyError("NOTIFY_CONFIG_INVALID")
    if section["provider"] not in PROVIDERS:
        raise NotifyError("NOTIFY_PROVIDER_INVALID")
    if not environment_url:
        path = section["ping_url_file"]
        if (not isinstance(path, str) or not path.startswith("/") or "\x00" in path
                or path != path.strip()):
            raise NotifyError("NOTIFY_PING_URL_FILE_INVALID")
    hosts = section["allowed_hosts"]
    if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 10
            or any(not isinstance(h, str) or not HOSTNAME.fullmatch(h) for h in hosts)
            or len({h.lower() for h in hosts}) != len(hosts)):
        raise NotifyError("NOTIFY_ALLOWED_HOSTS_INVALID")
    for key, (low, high) in (("reminder_minutes", REMINDER_MINUTES_BOUNDS),
                             ("daily_head_hour_utc", DAILY_HOUR_BOUNDS),
                             ("timeout_seconds", TIMEOUT_SECONDS_BOUNDS)):
        if type(section[key]) is not int or not low <= section[key] <= high:
            raise NotifyError("NOTIFY_CONFIG_INVALID")
    if not environment_url and type(section["local_notification"]) is not bool:
        raise NotifyError("NOTIFY_CONFIG_INVALID")
    return section


def read_ping_url(ping_url_file, allowed_hosts):
    """The one ``https`` URL a 0600 file may hold; its host must be in ``allowed_hosts``.

    Read fresh through ``private_bytes`` (owner, mode 0600, regular file, no symlink,
    bounded size) on every call: a rotated file is picked up on the next send, without a
    watchdog restart. Anything else about the file's content is refused, never guessed.
    """
    try:
        text = private_bytes(ping_url_file, limit=PING_URL_FILE_LIMIT).decode("ascii").strip()
    except (ValueError, UnicodeDecodeError):
        raise NotifyError("NOTIFY_PING_URL_FILE_INVALID") from None
    if not text or "\n" in text or "\r" in text or " " in text or "\x00" in text:
        raise NotifyError("NOTIFY_PING_URL_FILE_INVALID")
    return check_ping_url(text, allowed_hosts)


def check_ping_url(text, allowed_hosts):
    """One ``https`` URL on an allowed host, without credentials, query or fragment."""
    if (not isinstance(text, str) or not text or len(text) > PING_URL_FILE_LIMIT
            or any(c.isspace() or c == "\x00" for c in text)):
        raise NotifyError("NOTIFY_PING_URL_INVALID")
    try:
        url = urlsplit(text)
        hostname = url.hostname
    except ValueError:
        raise NotifyError("NOTIFY_PING_URL_INVALID") from None
    allowed = {h.lower() for h in allowed_hosts}
    if (url.scheme != "https" or not hostname or hostname.lower() not in allowed
            or url.username or url.password or url.query or url.fragment):
        raise NotifyError("NOTIFY_PING_URL_INVALID")
    return text.rstrip("/")


def _assert_redacted(payload):
    """The one gate every outgoing payload passes through: codes and counts only.

    A dedicated test proves this refuses free text smuggled into any field, including a
    forged extra key, an over-length alarms list, or a non-code string inside it.
    """
    if not isinstance(payload, dict) or set(payload) - PAYLOAD_KEYS:
        raise NotifyError("NOTIFY_PAYLOAD_INVALID")
    if "alarms" in payload:
        codes = payload["alarms"]
        if (not isinstance(codes, list) or len(codes) > 64
                or any(not isinstance(c, str) or not ALARM_CODE.fullmatch(c) for c in codes)):
            raise NotifyError("NOTIFY_PAYLOAD_INVALID")
    if "audit_seq" in payload and (
        type(payload["audit_seq"]) is not int or payload["audit_seq"] < 0
    ):
        raise NotifyError("NOTIFY_PAYLOAD_INVALID")
    if "audit_head_hash" in payload and not SHA256_HEX.fullmatch(
        payload["audit_head_hash"] or ""
    ):
        raise NotifyError("NOTIFY_PAYLOAD_INVALID")
    if "release_commit" in payload and not COMMIT_HEX.fullmatch(
        payload["release_commit"] or ""
    ):
        raise NotifyError("NOTIFY_PAYLOAD_INVALID")
    if "at" in payload:
        try:
            if datetime.fromisoformat(payload["at"]).tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise NotifyError("NOTIFY_PAYLOAD_INVALID") from None
    if len(json.dumps(payload, sort_keys=True)) > PAYLOAD_BYTES_LIMIT:
        raise NotifyError("NOTIFY_PAYLOAD_INVALID")
    return payload


def build_payload(alarms, *, now, audit_seq=None, audit_head_hash=None, release_commit=None):
    """Compact JSON of codes and counts only: ``{alarms, audit_seq, release_commit, at}``.

    ``audit_head_hash`` is added only for the once-per-day payload (plan 4.2 step 3).
    Every field is redaction-checked before return, so a caller cannot build a payload
    that :class:`Notifier` would later refuse.
    """
    payload = {"alarms": sorted(set(alarms)), "at": now.isoformat()}
    if audit_seq is not None:
        payload["audit_seq"] = audit_seq
    if audit_head_hash is not None:
        payload["audit_head_hash"] = audit_head_hash
    if release_commit is not None:
        payload["release_commit"] = release_commit
    return _assert_redacted(payload)


def _default_sender(timeout_seconds, transport):
    def send(url, payload):
        with httpx.Client(timeout=timeout_seconds, trust_env=False, follow_redirects=False,
                          transport=transport) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()

    return send


class Notifier:
    """POSTs compact, code-only payloads to a Healthchecks.io-style ping URL.

    ``sender(url, payload)`` performs the actual POST; the default builds a private
    ``httpx.Client`` per call (the configured timeout, no redirects, no environment
    proxies — the same shape as ``managed_ops.watchdog_once``'s status client). Tests pass
    ``sender`` directly, or ``transport`` for ``httpx.MockTransport``; never a real network
    call. Every failure — transport, HTTP status, or the redaction check — raises
    :class:`NotifyError` uniformly; callers never see the underlying httpx exception.
    """

    def __init__(self, url, *, timeout_seconds, sender=None, transport=None):
        self._url = url.rstrip("/")
        self._sender = sender or _default_sender(timeout_seconds, transport)

    def ping_ok(self, payload):
        self._send(self._url, payload)

    def ping_fail(self, payload):
        self._send(self._url + "/fail", payload)

    def ping_start(self):
        """Optional: signals the start of a monitored run. Not called by the watchdog."""
        self._send(self._url + "/start", {})

    def _send(self, url, payload):
        _assert_redacted(payload)
        try:
            self._sender(url, payload)
        except NotifyError:
            raise
        except Exception as exc:
            raise NotifyError("NOTIFY_FAILED") from exc


def local_notify(codes, *, runner=None):
    """A macOS Notification Center alert with the same code-only text as the off-host ping.

    ``codes`` must already be alarm codes (matching ``ALARM_CODE`); never evidence text,
    symbols, prices or the ping URL. ``runner`` defaults to ``subprocess.run`` and is
    always called with the same three-element argv shape, so a test can inject a fake and
    assert on it without spawning a real process.
    """
    if not codes or any(not isinstance(c, str) or not ALARM_CODE.fullmatch(c) for c in codes):
        raise NotifyError("NOTIFY_LOCAL_TEXT_INVALID")
    text = ",".join(sorted(set(codes)))
    argv = ["osascript", "-e", f'display notification "{text}" with title "{NOTIFICATION_TITLE}"']
    try:
        (runner or subprocess.run)(argv, check=True, capture_output=True, timeout=5)
    except NotifyError:
        raise
    except Exception as exc:
        raise NotifyError("NOTIFY_LOCAL_FAILED") from exc


def _state_path(alarm_file):
    """The notifier's own mode-0600 state file, beside the watchdog's alarm file."""
    return Path(alarm_file).with_suffix(".notify-state.json")


def _load_state(path):
    try:
        raw = strict_json(private_bytes(path))
        last_alarms = raw.get("last_alarms", []) if isinstance(raw, dict) else None
        if not isinstance(last_alarms, list) or any(not isinstance(c, str) for c in last_alarms):
            raise ValueError
    except ValueError:
        raw = {}
        last_alarms = []
    return {"last_alarms": sorted(set(last_alarms)),
            "last_fail_sent_at": raw.get("last_fail_sent_at"),
            "last_daily_head_date": raw.get("last_daily_head_date")}


def _save_state(path, state):
    atomic_private_json(path, state)


def _reminder_due(last_sent_iso, now, reminder_minutes):
    if not isinstance(last_sent_iso, str):
        return True
    try:
        last = datetime.fromisoformat(last_sent_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        return True
    return now - last >= timedelta(minutes=reminder_minutes)


def due_for_daily_head(section, alarm_file, now):
    """Cheap, read-only pre-check: should this tick fetch the audit head for the daily
    payload? Never raises: a malformed section or a corrupt state file just means "skip",
    since ``watchdog_tick`` independently validates and reports ``NOTIFY_FAILED`` anyway.
    """
    if not section:
        return False
    hour = section.get("daily_head_hour_utc")
    if type(hour) is not int or hour != now.hour:
        return False
    state = _load_state(_state_path(alarm_file))
    return state.get("last_daily_head_date") != now.date().isoformat()


def _build_notifier(section, sender, transport, ping_url=None):
    url = (check_ping_url(ping_url, section["allowed_hosts"]) if ping_url is not None
           else read_ping_url(section["ping_url_file"], section["allowed_hosts"]))
    return Notifier(url, timeout_seconds=section["timeout_seconds"], sender=sender,
                    transport=transport)


def watchdog_tick(section, *, alarms, now, alarm_file, audit_head=None, release_commit=None,
                  transport=None, sender=None, runner=None, ping_url=None):
    """One watchdog tick's off-host alerting step (plan 4.2).

    ``section`` is ``config["notify"]``; an empty section disables everything and this
    reads or writes nothing. Otherwise, every tick with a clean status sends the success
    ping (the dead-man signal: Healthchecks' own missing-ping alarm covers a dead Mac, a
    dead app or dead Wi-Fi, none of which this process could report itself). A new alarm
    code since the last tick sends ``/fail`` immediately; while alarms persist unchanged,
    ``/fail`` repeats only every ``reminder_minutes``. Once per UTC day, at
    ``daily_head_hour_utc``, the success payload also carries ``audit_head`` (an optional
    ``(seq, hash)`` pair the caller reads read-only; only when it is actually that day's
    first clean tick at that hour, per the caller's own :func:`due_for_daily_head` check).

    Returns ``(extra_alarms, sent)``: ``extra_alarms`` is ``["NOTIFY_FAILED"]`` when any
    step raised, else ``[]`` — the caller merges this into its own alarm file; it is never
    turned into a halt or a trading change. ``sent`` names what was attempted (``"OK"``,
    ``"FAIL"``, ``"LOCAL"``), for tests. Every exception is caught here.

    ``ping_url`` (the Railway ops service, package cloud) supplies the URL from a secret
    variable instead of ``ping_url_file``; the section then has the environment-URL keys.
    """
    if not section:
        return [], []
    state_path = _state_path(alarm_file)
    state = _load_state(state_path)
    codes = sorted(set(alarms))
    sent = []
    try:
        validate_notify_section(section, environment_url=ping_url is not None)
        notifier = _build_notifier(section, sender, transport, ping_url)
        if not codes:
            due_daily = (audit_head is not None
                        and due_for_daily_head(section, alarm_file, now))
            extras = ({"audit_seq": audit_head[0], "audit_head_hash": audit_head[1]}
                     if due_daily else {})
            notifier.ping_ok(build_payload(codes, now=now, release_commit=release_commit,
                                           **extras))
            sent.append("OK")
            state["last_alarms"] = []
            if due_daily:
                state["last_daily_head_date"] = now.date().isoformat()
            _save_state(state_path, state)
        else:
            transitioned = codes != state.get("last_alarms")
            due = transitioned or _reminder_due(state.get("last_fail_sent_at"), now,
                                                section["reminder_minutes"])
            if due:
                notifier.ping_fail(build_payload(codes, now=now, release_commit=release_commit))
                sent.append("FAIL")
                state["last_fail_sent_at"] = now.isoformat()
            state["last_alarms"] = codes
            _save_state(state_path, state)
            if due and section.get("local_notification") is True:
                local_notify(codes, runner=runner)
                sent.append("LOCAL")
        return [], sent
    except NotifyError:
        return ["NOTIFY_FAILED"], sent
    except Exception:
        return ["NOTIFY_FAILED"], sent
