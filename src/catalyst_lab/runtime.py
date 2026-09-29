"""Opt-in Alpaca observer with an optional, independently authorized risk runtime."""

import hashlib
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from catalyst_lab.alpaca import STREAM_ENDPOINTS, FixedStreamConnection
from catalyst_lab.market import NY, MarketDataError, Observation, symbol

WIRE_LOG = logging.Logger("catalyst-market-wire", level=logging.CRITICAL + 1)
WIRE_LOG.propagate = False
WIRE_LOG.addHandler(logging.NullHandler())


def paper_account_identity(client):
    """Match the managed executor lock without persisting the raw account identifier."""
    raw = client._get("account")
    identity = raw.get("id") if isinstance(raw, dict) else None
    if not isinstance(identity, str) or not identity.strip():
        raise MarketDataError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
    return hashlib.sha256(("ALPACA_PAPER:" + identity).encode()).hexdigest()


class MarketRuntime:
    def __init__(
        self,
        adapter,
        watcher,
        diagnostic_symbols=(),
        *,
        connector=FixedStreamConnection,
        clock=None,
        broker_monitor=None,
        measurements=None,
        executor_lease=None,
    ):
        self.adapter, self.watcher = adapter, watcher
        self.broker_monitor = broker_monitor
        self.measurements = measurements
        self.executor_lease = executor_lease
        if broker_monitor is not None:
            self.watcher.reconciliation_gate = broker_monitor.ready
        self.diagnostic_symbols = {symbol(s) for s in diagnostic_symbols}
        self.connector = connector
        self.now = clock or (lambda: datetime.now(UTC))
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.sessions = {}
        self.account = None
        self.rest_at = None
        self.error = None
        self.connected = False
        self.authenticated = False
        self.subscribed = set()
        self.quotes = {}
        self.received_count = 0
        self.threads = []
        self.socket = None
        self.lease = None
        self.risk_runtime = None
        self.admission_provider = None

    def start(self):
        if self.threads or self.stop_event.is_set():
            raise RuntimeError("MARKET_RUNTIME_RESTART_REQUIRES_NEW_INSTANCE")
        if self.executor_lease is not None:
            # AccountExecutorLease also holds the legacy observer lock. Taking it
            # again on another connection would conflict with our own executor.
            self.executor_lease.acquire()
        else:
            self.lease = self.watcher.repo.connect()
            acquired = self.lease.execute(
                "SELECT pg_try_advisory_lock(719172027) AS acquired"
            ).fetchone()["acquired"]
            if not acquired:
                self.lease.close()
                self.lease = None
                raise RuntimeError("A market observer is already running for this database")
            self.lease.commit()
        try:
            self._check_ownership()
            self.watcher.tick({}, self.now(), set(), recovering=True)
            self.watcher.system_event(
                "MARKET_OBSERVER_START", {
                    "feed": self.adapter.feed, "execution_enabled": self.risk_runtime is not None,
                    "executor_fenced": self.executor_lease is not None,
                }
            )
            if self.broker_monitor is not None:
                self.broker_monitor.start()
            for target in (self._rest_loop, self._stream_loop):
                thread = threading.Thread(target=target, daemon=True)
                self.threads.append(thread)
                thread.start()
        except Exception:
            self.stop()
            raise

    def _check_ownership(self):
        if self.executor_lease is None:
            return
        try:
            self.executor_lease.assert_owned()
        except Exception:
            self.stop_event.set()
            if self.broker_monitor is not None:
                self.broker_monitor.stop_event.set()
            self._record_failure("EXECUTOR_OWNERSHIP_LOST")
            raise

    def ownership_ready(self):
        return self.executor_lease is None or (
            self.executor_lease.connection is not None and not self.executor_lease.lost
            and not self.stop_event.is_set()
        )

    def stop(self):
        self.stop_event.set()
        if self.broker_monitor is not None:
            # Risk tick and trade-update consumption belong to these threads.
            self.broker_monitor.stop_event.set()
        with self.lock:
            socket = self.socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(timeout=25)
        if self.broker_monitor is not None:
            self.broker_monitor.stop()
        workers = [*self.threads, *getattr(self.broker_monitor, "threads", ())]
        if any(thread.is_alive() for thread in workers):
            with self.lock:
                self.error = "EXECUTOR_SHUTDOWN_INCOMPLETE"
            # A timed-out risk worker may still send. Keep the transport and the
            # exclusive lease alive until it actually stops or the process exits.
            return
        try:
            self.adapter.close()
        finally:
            if self.executor_lease is not None:
                self.executor_lease.close()
            if self.lease is not None:
                self.lease.close()
                self.lease = None

    def status(self, *, private=False):
        now = self.now()
        with self.lock:
            current = self.sessions.get(now.astimezone(NY).date())
            result = {
                "enabled": True,
                "feed": self.adapter.feed,
                "data_provider": "ALPACA",
                "feed_coverage": "IEX_ONLY" if self.adapter.feed == "iex" else "CONSOLIDATED",
                "rest_verified_at": self.rest_at.isoformat() if self.rest_at else None,
                "broker_connected": bool(
                    self.rest_at and 0 <= (now - self.rest_at).total_seconds() <= 90
                ),
                "stream_connected": self.connected,
                "stream_authenticated": self.authenticated,
                "subscribed_symbol_count": len(self.subscribed),
                "observations_received": self.received_count,
                "fresh_quote_count": sum(
                    0 <= (now - q.at).total_seconds() <= 5 for q in self.quotes.values()
                ),
                "session_open": bool(current and current.contains(now)),
                "official_close": current.closes.isoformat() if current else None,
                "flatten_time": current.flatten_time.isoformat() if current else None,
                "error": self.error,
                "execution_enabled": self.risk_runtime is not None,
                "executor_ownership": "EXCLUSIVE" if self.executor_lease is not None
                and self.ownership_ready() else "UNHELD",
                "workers_alive": len(self.threads) == 2 and all(t.is_alive() for t in self.threads),
                "admission_gate": (
                    "LIVE_EVIDENCE_REQUIRED" if self.admission_provider
                    else "VALIDATION_CONTEXT_INCOMPLETE"
                )
                if self.broker_monitor and self.broker_monitor.ready()
                else "STARTUP_RECONCILIATION_REQUIRED",
                "broker_monitor": self.broker_monitor.status() if self.broker_monitor else None,
            }
            if private:
                result.update(
                    account=self.account,
                    quotes=[q.to_json() for q in self.quotes.values()],
                    subscribed_symbols=sorted(self.subscribed),
                )
            return result

    def validation_evidence(self, candidate, now):
        from catalyst_lab.validation import EvidenceUnavailable

        status = self.status()
        if (not status["stream_authenticated"] or not status["broker_connected"]
                or not self.ownership_ready()):
            return None
        if self.risk_runtime:
            with self.watcher.repo.connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s",
                    (now.astimezone(NY).date(),),
                ).fetchone():
                    raise EvidenceUnavailable("DAILY_RISK_HALT", "Session loss halt is active")
        if self.broker_monitor is None or not self.broker_monitor.ready():
            raise EvidenceUnavailable(
                "STARTUP_RECONCILIATION_REQUIRED",
                "Current-process broker reconciliation is incomplete or halted",
            )
        if self.admission_provider is not None:
            return self.admission_provider(candidate, now)
        raise EvidenceUnavailable(
            "VALIDATION_CONTEXT_INCOMPLETE",
            "Production admission requires complete asset, liquidity and policy evidence",
        )

    def _record_failure(self, code):
        if self.executor_lease is not None and self.executor_lease.lost:
            code = "EXECUTOR_OWNERSHIP_LOST"
        with self.lock:
            different = self.error != code
            self.error = code
        if different:
            self.watcher.system_event(
                "DATA_FEED_FAILURE",
                {"reason": code, "feed": self.adapter.feed, "execution_enabled": False},
            )

    def _rest_loop(self):
        while not self.stop_event.is_set():
            try:
                self._check_ownership()
                now = self.now()
                dates = [r["session_date"] for r in self.watcher.active()]
                if self.measurements:
                    with self.watcher.repo.connect() as conn:
                        dates += [
                            r["session_date"]
                            for r in conn.execute(
                                "SELECT DISTINCT session_date FROM lab.candidates"
                            ).fetchall()
                        ]
                today = now.astimezone(NY).date()
                account = self.adapter.account()
                sessions = self.adapter.calendar(min([today, *dates]), today + timedelta(days=7))
                if self.measurements:
                    self.measurements.record_sessions(sessions)
                with self.lock:
                    self.account = account
                    self.sessions = {s.session_date: s for s in sessions}
                    self.rest_at = self.now()
                if self.broker_monitor is not None:
                    self.broker_monitor.set_sessions(self.sessions)
            except MarketDataError as exc:
                self._record_failure(str(exc))
            except Exception:
                self._record_failure("MARKET_REST_WORKER_FAILURE")
            self.stop_event.wait(45)

    def _heartbeat(self):
        if self.stop_event.is_set():
            return
        self._check_ownership()
        with self.lock:
            sessions = dict(self.sessions)
            healthy = set(self.subscribed) if self.connected and self.authenticated else set()
        self.watcher.tick(sessions, self.now(), healthy)

    @staticmethod
    def _messages(socket, timeout):
        raw = socket.recv(timeout=timeout)
        try:
            messages = json.loads(raw)
            if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
                raise ValueError
            for message in messages:
                if message.get("T") == "error":
                    code = message.get("code")
                    raise MarketDataError(
                        f"ALPACA_STREAM_{code}" if isinstance(code, int) else "ALPACA_STREAM_ERROR"
                    )
            return messages
        except (ValueError, TypeError):
            raise MarketDataError("INVALID_STREAM_MESSAGE") from None

    def _success(self, socket, expected):
        if not any(
            m.get("T") == "success" and m.get("msg") == expected for m in self._messages(socket, 8)
        ):
            raise MarketDataError("STREAM_HANDSHAKE_FAILED")

    def _stream_session(self):
        # Auth is sent only after a TLS connection to this fixed destination succeeds.
        with self.connector(
            STREAM_ENDPOINTS[self.adapter.feed],
            proxy=None,
            open_timeout=8,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
            max_queue=64,
            logger=WIRE_LOG,
        ) as socket:
            with self.lock:
                self.socket = socket
            self._success(socket, "connected")
            socket.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self.adapter.credentials.key_id,
                        "secret": self.adapter.credentials.secret,
                    }
                )
            )
            self._success(socket, "authenticated")
            with self.lock:
                self.connected = self.authenticated = True
                self.error = None
            self.watcher.system_event("WEBSOCKET_RECONNECT", {"feed": self.adapter.feed})
            requested, last_tick, pending_since = set(), 0, None
            while not self.stop_event.is_set():
                monotonic = time.monotonic()
                if monotonic - last_tick >= 1:
                    self._heartbeat()
                    desired = self.diagnostic_symbols | {r["ticker"] for r in self.watcher.active()}
                    if self.measurements:
                        desired |= {r["ticker"] for r in self.measurements.active(self.now())}
                    if desired != requested:
                        for action, names in (
                            ("subscribe", desired - requested),
                            ("unsubscribe", requested - desired),
                        ):
                            if names:
                                socket.send(
                                    json.dumps(
                                        {
                                            "action": action,
                                            "trades": sorted(names),
                                            "quotes": sorted(names),
                                            "bars": sorted(names),
                                        }
                                    )
                                )
                        requested, pending_since = desired, monotonic
                    last_tick = monotonic
                if pending_since and monotonic - pending_since > 8:
                    raise MarketDataError("SUBSCRIPTION_ACK_TIMEOUT")
                try:
                    messages = self._messages(socket, 1)
                except TimeoutError:
                    continue
                for message in messages:
                    kind = message.get("T")
                    if kind == "subscription":
                        acknowledged = (
                            set(message.get("trades", []))
                            & set(message.get("quotes", []))
                            & set(message.get("bars", []))
                        )
                        with self.lock:
                            self.subscribed = acknowledged & requested
                        if acknowledged == requested:
                            pending_since = None
                        self._heartbeat()  # Watch starts before processing any subsequent print.
                    elif kind in {"q", "t", "b", "u"}:
                        obs = Observation.from_wire(message, self.adapter.feed)
                        with self.lock:
                            if obs.ticker not in self.subscribed:
                                continue
                            if obs.kind == "quote":
                                old = self.quotes.get(obs.ticker)
                                if old is None or obs.ns >= old.ns:
                                    self.quotes[obs.ticker] = obs
                            self.received_count += 1
                            sessions, healthy = dict(self.sessions), set(self.subscribed)
                        self.watcher.tick(sessions, self.now(), healthy, obs)
                        if self.measurements:
                            self.measurements.observe(obs, self.now())
                    elif kind in {"c", "x"}:
                        # A correction cannot silently rewrite the print used by the strategy.
                        self.watcher.system_event(
                            "MARKET_TRADE_CORRECTION",
                            {
                                "message_type": kind,
                                "feed": self.adapter.feed,
                                "ticker": symbol(message["S"]),
                                "provider_record": message,
                            },
                        )

    def _stream_loop(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self._stream_session()
            except MarketDataError as exc:
                self._record_failure(str(exc))
            except Exception:
                self._record_failure("MARKET_STREAM_DISCONNECTED")
            finally:
                with self.lock:
                    self.connected = self.authenticated = False
                    self.subscribed = set()
                    self.socket = None
                    self.quotes = {}
            self.watcher.system_event("WEBSOCKET_DISCONNECT", {"feed": self.adapter.feed})
            for _ in range(backoff):
                self._heartbeat()
                if self.stop_event.wait(1):
                    break
            backoff = min(backoff * 2, 30)
