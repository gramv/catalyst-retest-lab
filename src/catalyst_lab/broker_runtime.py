"""Paper broker stream, independent reconciliation and calendar-exit clocks.

Before the trade-updates stream is marked connected, the fills and order changes it may
have missed are read from REST (plan 4.4) and recorded once; a failed read keeps the
stream unready (``REST_DEGRADED`` for rate limits and timeouts) and never halts.
"""

import json
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime

from catalyst_lab.alpaca import TRADE_STREAM_ENDPOINT, FixedStreamConnection
from catalyst_lab.broker_ledger import BrokerLedger
from catalyst_lab.execution import ExecutionService, TimeExits
from catalyst_lab.managed_latches import REST_CATEGORY, classify_failure
from catalyst_lab.market import NY, MarketDataError
from catalyst_lab.reconciliation import Reconciler
from catalyst_lab.runtime import WIRE_LOG


class BrokerMonitor:
    def __init__(
        self,
        repo,
        client,
        *,
        clock=None,
        connector=FixedStreamConnection,
        reconciliation_interval=45,
    ):
        self.repo, self.client = repo, client
        self.now = clock or (lambda: datetime.now(UTC))
        self.connector = connector
        self.reconciler = Reconciler(
            repo, client, clock=self.now, interval_seconds=reconciliation_interval
        )
        self.ledger = BrokerLedger(repo)
        self.exits = TimeExits(ExecutionService(repo, client))
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.connected = False
        self.socket = None
        self.sessions = {}
        self.threads = []
        self.error = None
        self.risk_runtime = None

    def start(self):
        for target in (self._reconciliation_loop, self._stream_loop, self._timer_loop):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            socket = self.socket
        if socket is not None:
            socket.close()
        for thread in self.threads:
            thread.join(timeout=30)

    def set_sessions(self, sessions):
        with self.lock:
            self.sessions = dict(sessions)

    def ready(self):
        return (
            self.connected
            and self.reconciler.ready()
            and (self.risk_runtime is None or self.risk_runtime.last_error is None)
        )

    def status(self):
        return {
            "trade_updates_connected": self.connected,
            "trading_enabled": self.risk_runtime is not None,
            "reconciliation": self.reconciler.status(),
            "watch_permitted": self.ready(),
            "error": self.error,
            "workers_alive": len(self.threads) == 3 and all(t.is_alive() for t in self.threads),
            "risk_engine_configured": self.risk_runtime is not None,
        }

    def _event(self, kind, payload):
        from catalyst_lab.execution import system_event

        with self.repo.connect() as conn:
            system_event(self.repo, conn, kind, {"stream": "trade_updates", **payload})

    def _reconciliation_loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.reconciler.run_once()
            except Exception:
                self.reconciler.last_clean = False
                self.error = "RECONCILIATION_WORKER_FAILURE"
                self._event("BROKER_MISMATCH", {"reason": self.error})
            self.stop_event.wait(
                max(0, self.reconciler.interval_seconds - (time.monotonic() - started))
            )

    def _timer_loop(self):
        while not self.stop_event.is_set():
            now = self.now()
            with self.lock:
                session = self.sessions.get(now.astimezone(NY).date())
            try:
                if self.risk_runtime:
                    self.risk_runtime.tick()
                else:
                    self.exits.tick(session, now)
            except Exception:
                self.error = "TIME_EXIT_WORKER_FAILURE"
                self._event("TIME_EXIT_FAILURE", {"reason": self.error})
            self.stop_event.wait(1)

    @staticmethod
    def _message(socket, timeout):
        try:
            data = json.loads(socket.recv(timeout=timeout))
            if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
                raise ValueError
            return data
        except (ValueError, TypeError):
            raise MarketDataError("INVALID_TRADE_UPDATE_FRAME") from None

    def _evidence_ledger(self):
        """The broker evidence ledger and the lock its stream consumer holds."""
        dispatcher = getattr(self.ledger, "dispatcher", None)  # A configured RiskRuntime.
        if dispatcher is not None:
            return dispatcher.ledger, dispatcher.lock, dispatcher.safety
        return self.ledger, nullcontext(), None

    def _backfill(self):
        """REST fill backfill for the gap before this connection (plan 4.4).

        Reads the non-terminal linked orders, then the FILL activities since the last
        recorded broker timestamp less five minutes (all pages), and records what the
        stream missed exactly once. Any read failure raises: the stream stays unready,
        with ``REST_DEGRADED`` for rate limits and timeouts, and nothing is halted.
        """
        ledger, lock, safety = self._evidence_ledger()
        after, open_ids = ledger.backfill_window()
        if after is None:
            return {"after": None, "orders_read": 0, "activities_read": 0}
        try:
            orders = [order for order in map(self.client.order, open_ids) if order]
            activities = self.client.fill_activities_since(after)
        except Exception as exc:
            if classify_failure(exc) == REST_CATEGORY:
                raise MarketDataError("REST_DEGRADED") from None
            raise MarketDataError("FILL_BACKFILL_UNAVAILABLE") from None
        with lock:
            result = ledger.record_rest_backfill(activities, orders, self.now(), after=after)
            if safety is not None:
                for candidate_id in result["candidates"]:
                    safety.check_protection(candidate_id)
        return {
            "after": after.isoformat(),
            "orders_read": len(orders),
            "activities_read": len(activities),
            **{k: v for k, v in result.items() if k != "candidates"},
        }

    def _stream_session(self):
        with self.connector(
            TRADE_STREAM_ENDPOINT,
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
            socket.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self.client.credentials.key_id,
                        "secret": self.client.credentials.secret,
                    }
                )
            )
            message = self._message(socket, 8)
            if (
                message.get("stream") != "authorization"
                or message["data"].get("status") != "authorized"
            ):
                raise MarketDataError("PAPER_TRADE_STREAM_AUTH_FAILED")
            socket.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
            deadline = time.monotonic() + 8
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MarketDataError("TRADE_UPDATES_SUBSCRIPTION_FAILED")
                message = self._message(socket, remaining)
                if message.get("stream") == "trade_updates":
                    self.ledger.consume(message["data"], self.now())
                    continue
                if message.get("stream") != "listening" or message["data"].get("streams") != [
                    "trade_updates"
                ]:
                    raise MarketDataError("TRADE_UPDATES_SUBSCRIPTION_FAILED")
                break
            # Fills and cancels of the gap come from REST before the stream counts as live.
            backfill = self._backfill()
            self.connected, self.error = True, None
            self._event("WEBSOCKET_RECONNECT", {"paper": True, "rest_backfill": backfill})
            while not self.stop_event.is_set():
                try:
                    message = self._message(socket, 1)
                except TimeoutError:
                    continue
                if message.get("stream") == "trade_updates":
                    self.ledger.consume(message["data"], self.now())
                elif message.get("stream") in {"authorization", "listening"}:
                    raise MarketDataError("TRADE_UPDATES_AUTH_OR_SUBSCRIPTION_CHANGED")
                else:
                    raise MarketDataError("UNEXPECTED_TRADE_UPDATE_FRAME")

    def _stream_loop(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self._stream_session()
            except Exception as exc:
                self.error = (
                    str(exc)
                    if isinstance(exc, MarketDataError)
                    else "PAPER_TRADE_STREAM_DISCONNECTED"
                )
            finally:
                with self.lock:
                    self.connected, self.socket = False, None
            self._event(
                "WEBSOCKET_DISCONNECT",
                {"reason": "SHUTDOWN" if self.stop_event.is_set() else self.error},
            )
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 30)
