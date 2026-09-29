"""Off-host alerts (plan 4.2): config validation, payload redaction, the Healthchecks-style
client, the local macOS notification, and the per-tick alerting state machine. No real
network call or subprocess anywhere: every test injects a ``sender``/``runner`` or, for the
one test proving the default transport wiring, ``httpx.MockTransport``."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from catalyst_lab.notify import (
    Notifier,
    NotifyError,
    build_payload,
    due_for_daily_head,
    local_notify,
    read_ping_url,
    validate_notify_section,
    watchdog_tick,
)
from tests.test_managed_ops import private_file


def base_section(root, *, hosts=("hc-ping.com",), reminder=30, daily_hour=6, timeout=5,
                 local=False):
    return {
        "provider": "HEALTHCHECKS",
        "ping_url_file": str(root / "ping-url"),
        "allowed_hosts": list(hosts),
        "reminder_minutes": reminder,
        "daily_head_hour_utc": daily_hour,
        "timeout_seconds": timeout,
        "local_notification": local,
    }


def ping_file(root, url="https://hc-ping.com/11111111-1111-1111-1111-111111111111"):
    return private_file(root / "ping-url", url)


def make_recording_sender():
    calls = []

    def sender(url, payload):
        calls.append((url, payload))

    return sender, calls


# --- config validation --------------------------------------------------------------------


def test_validate_notify_section_accepts_empty_and_full_and_rejects_bad_shapes(tmp_path):
    assert validate_notify_section({}) == {}
    full = base_section(tmp_path)
    assert validate_notify_section(dict(full)) == full
    for mutate in (
        lambda s: s.pop("provider"),
        lambda s: s.update(provider="PUSHOVER"),
        lambda s: s.update(ping_url_file="relative/path"),
        lambda s: s.update(ping_url_file=str(tmp_path / "x\x00y")),
        lambda s: s.update(allowed_hosts=[]),
        lambda s: s.update(allowed_hosts=["not a host!"]),
        lambda s: s.update(allowed_hosts=["hc-ping.com", "HC-PING.COM"]),
        lambda s: s.update(allowed_hosts="hc-ping.com"),
        lambda s: s.update(reminder_minutes=0),
        lambda s: s.update(reminder_minutes=1441),
        lambda s: s.update(reminder_minutes="30"),
        lambda s: s.update(daily_head_hour_utc=24),
        lambda s: s.update(daily_head_hour_utc=-1),
        lambda s: s.update(timeout_seconds=0),
        lambda s: s.update(timeout_seconds=31),
        lambda s: s.update(local_notification="true"),
        lambda s: s.update(local_notification=1),
        lambda s: s.update(unexpected_extra_key="value"),
    ):
        broken = dict(full)
        mutate(broken)
        with pytest.raises(NotifyError, match="NOTIFY_"):
            validate_notify_section(broken)


def test_validate_notify_section_rejects_non_dict_and_partial_key_sets(tmp_path):
    for bad in (None, [], "notify", 0, {"provider": "HEALTHCHECKS"}):
        with pytest.raises(NotifyError):
            validate_notify_section(bad)


# --- ping-url file content -----------------------------------------------------------------


def test_read_ping_url_requires_https_exact_allowed_host_and_single_line(tmp_path):
    path = tmp_path / "ping-url"
    for content in (
        "http://hc-ping.com/uuid",                                  # not https
        "https://evil.example/uuid",                                # host not allowlisted
        "https://hc-ping.com/uuid\nhttps://hc-ping.com/other",      # more than one line
        "https://user:pass@hc-ping.com/uuid",                       # userinfo
        "https://hc-ping.com/uuid?x=1",                             # query string
        "https://hc-ping.com/uuid#frag",                            # fragment
        "not a url",
        "",
    ):
        private_file(path, content)
        with pytest.raises(NotifyError, match="NOTIFY_PING_URL"):
            read_ping_url(path, ["hc-ping.com"])
    # A trailing newline (``echo url > file``) and allowlist case are forgiven; a trailing
    # slash is normalized away so ``/fail``/``/start`` are never doubled.
    private_file(path, "https://HC-Ping.com/uuid/\n")
    assert read_ping_url(path, ["hc-ping.com"]) == "https://HC-Ping.com/uuid"
    # The private_bytes contract still applies: world-readable is refused.
    path.chmod(0o644)
    with pytest.raises(NotifyError, match="NOTIFY_PING_URL_FILE_INVALID"):
        read_ping_url(path, ["hc-ping.com"])


# --- payload shape and redaction ------------------------------------------------------------


def test_build_payload_shape_and_bounds():
    now = datetime(2026, 9, 24, 6, 0, 0, tzinfo=UTC)
    payload = build_payload(["WORKER_STOPPED", "WORKER_STOPPED"], now=now, audit_seq=42,
                            audit_head_hash="a" * 64, release_commit="b" * 40)
    assert payload == {"alarms": ["WORKER_STOPPED"], "at": now.isoformat(), "audit_seq": 42,
                       "audit_head_hash": "a" * 64, "release_commit": "b" * 40}
    assert build_payload([], now=now) == {"alarms": [], "at": now.isoformat()}
    for bad in (
        lambda: build_payload(["not a valid alarm code"], now=now),
        lambda: build_payload(["OK"], now=now),  # too short for ALARM_CODE
        lambda: build_payload(["WORKER_STOPPED"], now=now, audit_seq=-1),
        lambda: build_payload(["WORKER_STOPPED"], now=now, audit_seq="42"),
        lambda: build_payload(["WORKER_STOPPED"], now=now, release_commit="not-hex"),
        lambda: build_payload(["WORKER_STOPPED"], now=now, audit_head_hash="too-short"),
    ):
        with pytest.raises(NotifyError):
            bad()


def test_notifier_refuses_any_payload_shape_build_payload_would_not_produce():
    """The redaction gate: no evidence text, symbol, price or forged field ever reaches
    ``sender`` — proven by trying to sneak each shape past it directly."""
    sent = []
    notifier = Notifier("https://hc-ping.com/uuid", timeout_seconds=5,
                        sender=lambda url, payload: sent.append((url, payload)))
    for bad in (
        {"alarms": [], "evidence": "AAPL touched 101.20, thesis: breakout confirmed"},
        {"alarms": ["OK bad code with spaces"]},
        {"alarms": ["WORKER_STOPPED"] * 65},
        {"alarms": [], "at": "not-a-timestamp"},
        {"symbol": "AAPL", "price": "101.20"},
        {"alarms": [], "ping_url": "https://hc-ping.com/uuid"},
    ):
        with pytest.raises(NotifyError):
            notifier.ping_ok(bad)
    assert sent == []


# --- Notifier -------------------------------------------------------------------------------


def test_notifier_ping_ok_fail_start_use_expected_urls_and_code_only_bodies():
    sender, calls = make_recording_sender()
    notifier = Notifier("https://hc-ping.com/uuid/", timeout_seconds=5, sender=sender)
    now = datetime(2026, 9, 24, 6, 0, 0, tzinfo=UTC)
    notifier.ping_ok(build_payload([], now=now))
    notifier.ping_fail(build_payload(["WORKER_STOPPED"], now=now))
    notifier.ping_start()
    assert calls[0] == ("https://hc-ping.com/uuid", {"alarms": [], "at": now.isoformat()})
    assert calls[1] == ("https://hc-ping.com/uuid/fail",
                        {"alarms": ["WORKER_STOPPED"], "at": now.isoformat()})
    assert calls[2] == ("https://hc-ping.com/uuid/start", {})


def test_notifier_wraps_any_sender_failure_as_notify_error():
    def broken(url, payload):
        raise RuntimeError("connection refused")

    notifier = Notifier("https://hc-ping.com/uuid", timeout_seconds=5, sender=broken)
    with pytest.raises(NotifyError, match="NOTIFY_FAILED"):
        notifier.ping_ok(build_payload([], now=datetime.now(UTC)))


def test_notifier_default_sender_uses_mocktransport_no_redirects_and_raises_on_http_error():
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith("/fail"):
            return httpx.Response(500, text="unavailable")
        return httpx.Response(200)

    notifier = Notifier("https://hc-ping.com/uuid", timeout_seconds=5,
                        transport=httpx.MockTransport(respond))
    now = datetime.now(UTC)
    notifier.ping_ok(build_payload([], now=now))
    with pytest.raises(NotifyError, match="NOTIFY_FAILED"):
        notifier.ping_fail(build_payload(["X_ALARM_CODE"], now=now))
    assert seen[0].method == "POST" and seen[0].url.path == "/uuid"
    assert json.loads(seen[0].content) == {"alarms": [], "at": now.isoformat()}
    assert seen[1].url.path == "/uuid/fail"


# --- local macOS notification ---------------------------------------------------------------


def test_local_notify_runs_fixed_argv_with_code_only_text():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))

    local_notify(["WORKER_STOPPED", "BROKER_STREAM_LOST"], runner=runner)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "osascript" and argv[1] == "-e" and len(argv) == 3
    assert argv[2] == ('display notification "BROKER_STREAM_LOST,WORKER_STOPPED" '
                       'with title "Catalyst Watchdog"')
    assert kwargs == {"check": True, "capture_output": True, "timeout": 5}


def test_local_notify_rejects_non_code_text_before_running_and_wraps_runner_failure():
    with pytest.raises(NotifyError, match="NOTIFY_LOCAL_TEXT_INVALID"):
        local_notify(["not a code"], runner=lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(NotifyError, match="NOTIFY_LOCAL_TEXT_INVALID"):
        local_notify([], runner=lambda *a, **k: pytest.fail("must not run"))

    def failing_runner(argv, **kwargs):
        raise OSError("no display attached")

    with pytest.raises(NotifyError, match="NOTIFY_LOCAL_FAILED"):
        local_notify(["WORKER_STOPPED"], runner=failing_runner)


# --- the per-tick state machine (watchdog_tick) ----------------------------------------------


def test_watchdog_tick_empty_section_is_a_total_noop(tmp_path):
    alarm_file = tmp_path / "alarms.json"
    result = watchdog_tick({}, alarms=["WORKER_STOPPED"], now=datetime.now(UTC),
                           alarm_file=alarm_file)
    assert result == ([], [])
    assert not (tmp_path / "alarms.notify-state.json").exists()


def test_watchdog_tick_clean_ticks_ping_ok_every_time_the_deadman_signal(tmp_path):
    section = base_section(tmp_path)
    ping_file(tmp_path)
    sender, calls = make_recording_sender()
    alarm_file = tmp_path / "alarms.json"
    now = datetime(2026, 9, 24, 3, 0, 0, tzinfo=UTC)
    for _ in range(3):
        extra, sent = watchdog_tick(section, alarms=[], now=now, alarm_file=alarm_file,
                                    sender=sender)
        assert (extra, sent) == ([], ["OK"])
    assert len(calls) == 3
    assert all(url == "https://hc-ping.com/11111111-1111-1111-1111-111111111111"
              for url, _ in calls)


def test_watchdog_tick_transition_then_reminder_then_recovery(tmp_path):
    section = base_section(tmp_path, reminder=30)
    ping_file(tmp_path)
    sender, calls = make_recording_sender()
    alarm_file = tmp_path / "alarms.json"
    t0 = datetime(2026, 9, 24, 3, 0, 0, tzinfo=UTC)

    # A transition from "no prior alarms" sends exactly one /fail.
    extra, sent = watchdog_tick(section, alarms=["WORKER_STOPPED"], now=t0,
                               alarm_file=alarm_file, sender=sender)
    assert (extra, sent) == ([], ["FAIL"])
    assert len(calls) == 1 and calls[0][0].endswith("/fail")
    assert calls[0][1]["alarms"] == ["WORKER_STOPPED"]

    # Same alarm, well inside the reminder window: nothing more is sent.
    extra, sent = watchdog_tick(section, alarms=["WORKER_STOPPED"],
                               now=t0 + timedelta(minutes=5), alarm_file=alarm_file,
                               sender=sender)
    assert (extra, sent) == ([], [])
    assert len(calls) == 1

    # The reminder interval elapsed: it repeats.
    extra, sent = watchdog_tick(section, alarms=["WORKER_STOPPED"],
                               now=t0 + timedelta(minutes=31), alarm_file=alarm_file,
                               sender=sender)
    assert sent == ["FAIL"] and len(calls) == 2

    # A new code, even before the next reminder, is its own transition.
    extra, sent = watchdog_tick(section, alarms=["WORKER_STOPPED", "BROKER_STREAM_LOST"],
                               now=t0 + timedelta(minutes=32), alarm_file=alarm_file,
                               sender=sender)
    assert sent == ["FAIL"] and len(calls) == 3
    assert calls[2][1]["alarms"] == ["BROKER_STREAM_LOST", "WORKER_STOPPED"]

    # Recovery: a success ping, not /fail.
    extra, sent = watchdog_tick(section, alarms=[], now=t0 + timedelta(minutes=33),
                               alarm_file=alarm_file, sender=sender)
    assert sent == ["OK"] and len(calls) == 4
    assert calls[3][0] == "https://hc-ping.com/11111111-1111-1111-1111-111111111111"


def test_watchdog_tick_includes_daily_head_exactly_once_per_utc_day(tmp_path):
    section = base_section(tmp_path, daily_hour=6)
    ping_file(tmp_path)
    sender, calls = make_recording_sender()
    alarm_file = tmp_path / "alarms.json"
    at_six = datetime(2026, 9, 24, 6, 0, 0, tzinfo=UTC)

    watchdog_tick(section, alarms=[], now=at_six, alarm_file=alarm_file, sender=sender,
                 audit_head=(42, "a" * 64))
    assert calls[-1][1]["audit_seq"] == 42 and calls[-1][1]["audit_head_hash"] == "a" * 64

    # A second tick in the same UTC hour/day does not repeat it.
    watchdog_tick(section, alarms=[], now=at_six + timedelta(minutes=20), alarm_file=alarm_file,
                 sender=sender, audit_head=(43, "b" * 64))
    assert "audit_seq" not in calls[-1][1]

    # A different hour never carries it, even when offered.
    watchdog_tick(section, alarms=[], now=at_six + timedelta(hours=1), alarm_file=alarm_file,
                 sender=sender, audit_head=(44, "c" * 64))
    assert "audit_seq" not in calls[-1][1]

    # The next day at the same hour includes it again.
    watchdog_tick(section, alarms=[], now=at_six + timedelta(days=1), alarm_file=alarm_file,
                 sender=sender, audit_head=(45, "d" * 64))
    assert calls[-1][1]["audit_seq"] == 45

    # Without an audit_head offered at all, the success ping still goes out, bare.
    watchdog_tick(section, alarms=[], now=at_six + timedelta(days=2), alarm_file=alarm_file,
                 sender=sender, audit_head=None)
    assert "audit_seq" not in calls[-1][1]


def test_watchdog_tick_notifier_failure_returns_only_notify_failed_and_nothing_else(tmp_path):
    section = base_section(tmp_path)
    ping_file(tmp_path)
    alarm_file = tmp_path / "alarms.json"

    def broken(url, payload):
        raise RuntimeError("network unreachable")

    extra, sent = watchdog_tick(section, alarms=[], now=datetime.now(UTC),
                               alarm_file=alarm_file, sender=broken)
    assert extra == ["NOTIFY_FAILED"] and sent == []

    extra, sent = watchdog_tick(section, alarms=["WORKER_STOPPED"], now=datetime.now(UTC),
                               alarm_file=alarm_file, sender=broken)
    assert extra == ["NOTIFY_FAILED"]


def test_watchdog_tick_misconfigured_section_is_notify_failed_not_a_crash(tmp_path):
    # Structurally invalid (never validated by _validate_v2, e.g. hand-built at runtime).
    broken = {**base_section(tmp_path), "provider": "PUSHOVER"}
    extra, sent = watchdog_tick(broken, alarms=[], now=datetime.now(UTC),
                               alarm_file=tmp_path / "alarms.json")
    assert extra == ["NOTIFY_FAILED"] and sent == []
    # Valid shape, but the ping-url file was never created.
    missing_file = base_section(tmp_path)
    extra, sent = watchdog_tick(missing_file, alarms=[], now=datetime.now(UTC),
                               alarm_file=tmp_path / "alarms2.json")
    assert extra == ["NOTIFY_FAILED"] and sent == []


def test_watchdog_tick_local_notification_fires_only_when_enabled(tmp_path):
    ping_file(tmp_path)
    sender, _ = make_recording_sender()
    alarm_file = tmp_path / "alarms.json"
    now = datetime.now(UTC)
    argv_calls = []

    off = base_section(tmp_path, local=False)
    extra, sent = watchdog_tick(off, alarms=["WORKER_STOPPED"], now=now, alarm_file=alarm_file,
                               sender=sender, runner=lambda *a, **k: argv_calls.append(a))
    assert sent == ["FAIL"] and argv_calls == []

    on = base_section(tmp_path, local=True)
    extra, sent = watchdog_tick(on, alarms=["BROKER_STREAM_LOST"],
                               now=now + timedelta(minutes=1), alarm_file=alarm_file,
                               sender=sender, runner=lambda *a, **k: argv_calls.append(a))
    assert sent == ["FAIL", "LOCAL"] and len(argv_calls) == 1
    assert argv_calls[0][0][:2] == ["osascript", "-e"]
    assert extra == []


def test_state_file_corrupt_or_missing_resets_safely(tmp_path):
    section = base_section(tmp_path)
    ping_file(tmp_path)
    alarm_file = tmp_path / "alarms.json"
    state_path = tmp_path / "alarms.notify-state.json"
    private_file(state_path, "{not json")
    sender, calls = make_recording_sender()
    extra, sent = watchdog_tick(section, alarms=["WORKER_STOPPED"], now=datetime.now(UTC),
                               alarm_file=alarm_file, sender=sender)
    assert extra == [] and sent == ["FAIL"]  # a corrupt file reads as "no prior alarms"
    assert json.loads(state_path.read_text())["last_alarms"] == ["WORKER_STOPPED"]
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_due_for_daily_head_checks_hour_section_and_once_per_day(tmp_path):
    alarm_file = tmp_path / "alarms.json"
    now = datetime(2026, 9, 24, 6, 30, 0, tzinfo=UTC)
    assert due_for_daily_head({}, alarm_file, now) is False
    section = base_section(tmp_path, daily_hour=6)
    assert due_for_daily_head(section, alarm_file, now) is True
    assert due_for_daily_head(section, alarm_file, now.replace(hour=7)) is False
    private_file(Path(alarm_file).with_suffix(".notify-state.json"), json.dumps({
        "last_alarms": [], "last_fail_sent_at": None,
        "last_daily_head_date": now.date().isoformat()}))
    assert due_for_daily_head(section, alarm_file, now) is False
