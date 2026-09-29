"""Per-account broker read budget, Retry-After handling and one shared read snapshot.

Alpaca Paper serves about 200 REST requests per minute per account. The managed
runtime used to read the account, positions and capital activities on every
protection tick (at most one second apart) plus per-setup order, position,
asset and calendar reads, which approached that limit with no setups at all.

This module keeps the reads of one account inside an explicit budget:

* ``TokenBucket`` budgets every GET to the trading host (default 150 per minute)
  with a trailing-minute ceiling and four priority classes: protective reads and
  recovery > account snapshot > reconciliation > research/liquidity reads. A lower
  class keeps a reserve free for the classes above it.
* An HTTP 429 honours ``Retry-After`` (or Alpaca's reset header): reads wait,
  the bucket is emptied, and the runtime reports REST_DEGRADED.
* A broker mutation is never delayed, rejected or changed here. POST, PATCH and
  DELETE already crossed the exact one-use risk authorization above this
  transport; they are counted and they invalidate the shared snapshot, nothing
  else. A refused read raises before any I/O, so it can never strand a claim.
* ``BrokerSnapshot`` (open orders with nested legs, positions, account, capital
  activities) is taken at most once per ``snapshot_seconds`` and shared by the
  account-safety tick and every per-setup broker view in that interval. Any trade
  update or mutation invalidates it, so the next reader sees fresh state.
* Terminal orders are cached by id, capital activities for 60 seconds, asset
  metadata for 60 seconds and the trading calendar per New York date.

Nothing here opens a connection; tests inject a clock and an httpx mock transport.
"""

import contextvars
import copy
import math
import threading
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime

import httpx

from catalyst_lab.market import NY, MarketDataError

PROTECTIVE, ACCOUNT, RECONCILIATION, RESEARCH = range(4)
PRIORITY_NAMES = (
    "PROTECTIVE_AND_RECOVERY",
    "ACCOUNT_SNAPSHOT",
    "RECONCILIATION",
    "RESEARCH_AND_LIQUIDITY",
)
TRADING_HOST = "paper-api.alpaca.markets"
TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}
)

# Threads and tasks that never declare a class are treated as research reads.
_PRIORITY = contextvars.ContextVar("catalyst_broker_request_priority", default=RESEARCH)


@contextmanager
def request_priority(priority):
    """Declare the class of every broker read made inside this block."""
    if isinstance(priority, bool) or priority not in range(len(PRIORITY_NAMES)):
        raise ValueError("UNKNOWN_BROKER_REQUEST_PRIORITY")
    token = _PRIORITY.set(priority)
    try:
        yield
    finally:
        _PRIORITY.reset(token)


def current_priority():
    return _PRIORITY.get()


class BrokerRateLimited(MarketDataError):
    """A read refused before any I/O by the local budget or a Retry-After window."""

    def __init__(self, code, retry_after_seconds=None):
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class BudgetPolicy:
    # Token refill rate, and the trailing-minute ceiling for every non-protective read.
    requests_per_minute: int = 150
    burst_capacity: int = 150
    # Protective reads may use part of the broker's remaining headroom (limit ~200/min).
    protective_requests_per_minute: int = 180
    snapshot_seconds: float = 5.0
    activity_cache_seconds: float = 60.0
    asset_cache_seconds: float = 60.0
    default_retry_after_seconds: float = 5.0
    max_retry_after_seconds: float = 60.0
    # Fraction of the burst each class must leave for the classes above it.
    reserve_fractions: tuple = (0.0, 0.1, 0.2, 0.4)
    terminal_order_cache_size: int = 5000

    def __post_init__(self):
        seconds = (
            self.snapshot_seconds,
            self.activity_cache_seconds,
            self.asset_cache_seconds,
            self.default_retry_after_seconds,
            self.max_retry_after_seconds,
        )
        reserves = self.reserve_fractions
        if (
            type(self.requests_per_minute) is not int
            or not 1 <= self.requests_per_minute <= 200
            or type(self.burst_capacity) is not int
            or not 1 <= self.burst_capacity <= self.requests_per_minute
            or type(self.protective_requests_per_minute) is not int
            or not self.requests_per_minute <= self.protective_requests_per_minute <= 200
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or v <= 0
                for v in seconds
            )
            or not 1 <= self.snapshot_seconds <= 30
            or self.activity_cache_seconds > 60
            or self.asset_cache_seconds > 300
            or self.default_retry_after_seconds > self.max_retry_after_seconds
            or self.max_retry_after_seconds > 300
            or not isinstance(reserves, tuple)
            or len(reserves) != len(PRIORITY_NAMES)
            or reserves[0] != 0
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 1
                for v in reserves
            )
            or list(reserves) != sorted(reserves)
            or type(self.terminal_order_cache_size) is not int
            or not 100 <= self.terminal_order_cache_size <= 100000
        ):
            raise ValueError("EXPLICIT_BROKER_BUDGET_POLICY_REQUIRED")


def retry_after_seconds(headers, now):
    """Seconds from ``Retry-After`` (delta or HTTP date), else ``X-RateLimit-Reset``."""
    value = headers.get("Retry-After")
    if value is not None:
        text = value.strip()
        try:
            seconds = float(text)
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            try:
                when = parsedate_to_datetime(text)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - now).total_seconds())
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    reset = headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            epoch = float(reset.strip())
            if math.isfinite(epoch):
                return max(0.0, epoch - now.timestamp())
        except (ValueError, OverflowError):
            pass
    return None


class TokenBucket:
    """Priority token bucket with a trailing-minute ceiling; thread-safe."""

    def __init__(self, policy, clock):
        self.policy, self.clock = policy, clock
        self._lock = threading.Lock()
        self._tokens = float(policy.burst_capacity)
        self._refilled_at = None
        self._recent = deque()
        self._cooldown_until = None
        self.sent = dict.fromkeys(PRIORITY_NAMES, 0)
        self.denied = dict.fromkeys(PRIORITY_NAMES, 0)
        self.mutations = 0
        self.rate_limited_responses = 0

    def _advance(self, now):
        if self._refilled_at is not None:
            elapsed = (now - self._refilled_at).total_seconds()
            if elapsed > 0:
                self._tokens = min(
                    float(self.policy.burst_capacity),
                    self._tokens + elapsed * self.policy.requests_per_minute / 60,
                )
        if self._refilled_at is None or now > self._refilled_at:
            self._refilled_at = now
        horizon = now - timedelta(seconds=60)
        while self._recent and self._recent[0] <= horizon:
            self._recent.popleft()

    def acquire(self, priority):
        now = self.clock()
        name = PRIORITY_NAMES[priority]
        with self._lock:
            self._advance(now)
            if self._cooldown_until is not None and now < self._cooldown_until:
                self.denied[name] += 1
                raise BrokerRateLimited(
                    "BROKER_RETRY_AFTER", (self._cooldown_until - now).total_seconds()
                )
            ceiling = (
                self.policy.protective_requests_per_minute
                if priority == PROTECTIVE
                else self.policy.requests_per_minute
            )
            reserve = self.policy.burst_capacity * self.policy.reserve_fractions[priority]
            available = min(self._tokens, float(ceiling - len(self._recent)))
            if available < reserve + 1:
                self.denied[name] += 1
                raise BrokerRateLimited("BROKER_BUDGET_EXHAUSTED")
            self._tokens -= 1
            self._recent.append(now)
            self.sent[name] += 1

    def record_mutation(self):
        """Count an already-authorized mutation; it is never refused or delayed."""
        now = self.clock()
        with self._lock:
            self._advance(now)
            self._tokens = max(0.0, self._tokens - 1)
            self._recent.append(now)
            self.mutations += 1

    def record_rate_limited(self, seconds):
        now = self.clock()
        if seconds is None:
            seconds = self.policy.default_retry_after_seconds
        seconds = min(self.policy.max_retry_after_seconds, max(0.0, seconds))
        with self._lock:
            self._advance(now)
            until = now + timedelta(seconds=seconds)
            if self._cooldown_until is None or until > self._cooldown_until:
                self._cooldown_until = until
            # The broker counts requests this process cannot see; start again from empty.
            self._tokens = 0.0
            self.rate_limited_responses += 1

    def cooldown_remaining(self):
        now = self.clock()
        with self._lock:
            if self._cooldown_until is None or now >= self._cooldown_until:
                return 0.0
            return (self._cooldown_until - now).total_seconds()

    def status(self):
        now = self.clock()
        with self._lock:
            self._advance(now)
            cooldown = 0.0
            if self._cooldown_until is not None and now < self._cooldown_until:
                cooldown = (self._cooldown_until - now).total_seconds()
            return {
                "tokens": round(self._tokens, 3),
                "requests_last_minute": len(self._recent),
                "reads_sent": dict(self.sent),
                "reads_denied": dict(self.denied),
                "mutations_counted": self.mutations,
                "rate_limited_responses": self.rate_limited_responses,
                "cooldown_remaining_seconds": round(cooldown, 3),
            }


def _flatten_orders(rows):
    """Every order and nested leg, outermost first; malformed entries are skipped."""
    pending = deque((row, 0) for row in rows if isinstance(row, dict))
    while pending:
        order, depth = pending.popleft()
        yield order
        if depth < 4:
            pending.extend(
                (leg, depth + 1) for leg in order.get("legs") or [] if isinstance(leg, dict)
            )


class BrokerSnapshot:
    """One read of open orders, positions, account and activities; accessors copy."""

    def __init__(self, taken_at, session_date, account, positions, open_orders, activities):
        self.taken_at, self.session_date = taken_at, session_date
        self._account = copy.deepcopy(account)
        self._positions = tuple(copy.deepcopy(list(positions)))
        self._orders = tuple(copy.deepcopy(list(open_orders)))
        self._activities = tuple(copy.deepcopy(list(activities)))
        by_id, by_client = {}, {}
        for order in _flatten_orders(self._orders):
            if isinstance(order.get("id"), str):
                by_id.setdefault(order["id"], order)
            if isinstance(order.get("client_order_id"), str):
                by_client.setdefault(order["client_order_id"], order)
        self._by_id, self._by_client = by_id, by_client

    @property
    def account(self):
        return copy.deepcopy(self._account)

    def position_rows(self):
        return copy.deepcopy(list(self._positions))

    def open_order_rows(self):
        return copy.deepcopy(list(self._orders))

    def capital_activity_rows(self):
        return copy.deepcopy(list(self._activities))

    def order(self, order_id):
        found = self._by_id.get(order_id)
        return copy.deepcopy(found) if found is not None else None

    def order_by_client_id(self, client_order_id):
        found = self._by_client.get(client_order_id)
        return copy.deepcopy(found) if found is not None else None

    def indexed_orders(self):
        return list(self._by_id.values())


class BrokerBudget:
    """Shared per-account governor: budget, Retry-After, snapshot and read caches."""

    def __init__(self, policy=None, *, clock):
        self.policy = policy if policy is not None else BudgetPolicy()
        if not isinstance(self.policy, BudgetPolicy) or not callable(clock):
            raise ValueError("EXPLICIT_BROKER_BUDGET_POLICY_REQUIRED")
        self.clock = clock
        self.bucket = TokenBucket(self.policy, clock)
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._snapshot = None
        self._generation = 0
        self._snapshot_generation = -1
        self._terminal = OrderedDict()
        self._terminal_clients = {}
        self._activities = None
        self._assets = {}
        self._calendar_day = None
        self._calendar = {}
        self.snapshot_refreshes = 0
        self.invalidations = 0
        self.last_invalidation = None
        self.timeouts = 0

    # --- Wiring -------------------------------------------------------------

    def transport(self, delegate=None):
        """The delegate transport for a paper client; place it below the risk gate."""
        return BudgetTransport(self, delegate)

    def wrap(self, broker):
        return BudgetedBroker(broker, self)

    # --- Transport hooks ----------------------------------------------------

    def before_read(self):
        self.bucket.acquire(current_priority())

    def before_mutation(self):
        try:
            self.bucket.record_mutation()
        except Exception:
            pass  # Accounting must never stop an authorized protective mutation.

    def after_mutation(self):
        try:
            self.invalidate("BROKER_MUTATION")
        except Exception:
            pass

    def observe(self, response):
        try:
            if response.status_code == 429:
                self.bucket.record_rate_limited(
                    retry_after_seconds(response.headers, self.clock())
                )
        except Exception:
            pass

    def note_timeout(self):
        with self._lock:
            self.timeouts += 1

    def cooldown_remaining(self):
        return self.bucket.cooldown_remaining()

    def invalidate(self, reason):
        """The next snapshot reader refreshes (trade update, mutation or operator)."""
        with self._lock:
            self._generation += 1
            self.invalidations += 1
            self.last_invalidation = reason

    # --- Shared snapshot ----------------------------------------------------

    def snapshot(self, broker, *, max_age=None):
        """Reuse a snapshot younger than ``max_age`` (default: the policy interval).

        ``max_age=0`` forces a fresh read, which also becomes the shared snapshot.
        A trade update or mutation during the reads triggers one immediate re-read.
        """
        limit = self.policy.snapshot_seconds if max_age is None else max_age
        with self._refresh_lock:
            now = self.clock()
            with self._lock:
                current = self._snapshot
                valid = current is not None and self._snapshot_generation == self._generation
            if valid:
                age = (now - current.taken_at).total_seconds()
                if 0 <= age < limit and current.session_date == now.astimezone(NY).date():
                    return current
            snapshot = None
            for _ in range(2):
                with self._lock:
                    generation = self._generation
                # Orders before positions, as the per-order reads were ordered before.
                orders = broker.open_orders()
                positions = broker.positions()
                account = broker.account()
                day = self.clock().astimezone(NY).date()
                activities = self.capital_activities(broker, day)
                snapshot = BrokerSnapshot(
                    self.clock(), day, account, positions, orders, activities
                )
                for order in snapshot.indexed_orders():
                    self._remember_terminal(order)
                with self._lock:
                    self._snapshot = snapshot
                    self._snapshot_generation = generation
                    self.snapshot_refreshes += 1
                    raced = generation != self._generation
                if not raced:
                    break
            return snapshot

    # --- Read caches --------------------------------------------------------

    def _remember_terminal(self, order):
        if not isinstance(order, dict) or order.get("status") not in TERMINAL_ORDER_STATUSES:
            return
        order_id = order.get("id")
        if not isinstance(order_id, str):
            return
        with self._lock:
            self._terminal[order_id] = copy.deepcopy(order)
            self._terminal.move_to_end(order_id)
            if isinstance(order.get("client_order_id"), str):
                self._terminal_clients[order["client_order_id"]] = order_id
            while len(self._terminal) > self.policy.terminal_order_cache_size:
                old_id, old = self._terminal.popitem(last=False)
                if self._terminal_clients.get(old.get("client_order_id")) == old_id:
                    del self._terminal_clients[old["client_order_id"]]

    def order(self, broker, order_id):
        with self._lock:
            cached = self._terminal.get(order_id)
            if cached is not None:
                return copy.deepcopy(cached)
        order = broker.order(order_id)
        self._remember_terminal(order)
        return order

    def order_by_client_id(self, broker, client_order_id):
        with self._lock:
            cached = self._terminal.get(self._terminal_clients.get(client_order_id))
            if cached is not None:
                return copy.deepcopy(cached)
        order = broker.order_by_client_id(client_order_id)
        self._remember_terminal(order)
        return order

    def capital_activities(self, broker, session_date):
        now = self.clock()
        with self._lock:
            if self._activities is not None:
                day, fetched_at, rows = self._activities
                age = (now - fetched_at).total_seconds()
                if day == session_date and 0 <= age < self.policy.activity_cache_seconds:
                    return copy.deepcopy(list(rows))
        rows = broker.capital_activities(session_date)
        with self._lock:
            self._activities = (session_date, now, tuple(copy.deepcopy(list(rows))))
        return rows

    def calendar(self, broker, start, end):
        today = self.clock().astimezone(NY).date()
        with self._lock:
            if self._calendar_day != today:
                self._calendar_day, self._calendar = today, {}
            cached = self._calendar.get((start, end))
            if cached is not None:
                return list(cached)
        sessions = broker.calendar(start, end)
        with self._lock:
            if self._calendar_day == today:
                self._calendar[(start, end)] = tuple(sessions)
        return list(sessions)

    def asset(self, broker, symbol):
        now = self.clock()
        with self._lock:
            cached = self._assets.get(symbol)
            if cached is not None and 0 <= (now - cached[0]).total_seconds() < (
                self.policy.asset_cache_seconds
            ):
                return copy.deepcopy(cached[1])
        row = broker.asset(symbol)
        if isinstance(row, dict):
            with self._lock:
                self._assets[symbol] = (now, copy.deepcopy(row))
        return row

    # --- Status -------------------------------------------------------------

    def status(self):
        """Compact counters (the heartbeat stores this every few seconds); the full
        policy is part of the runtime's configuration hash."""
        now = self.clock()
        with self._lock:
            snapshot = self._snapshot
            age = (now - snapshot.taken_at).total_seconds() if snapshot is not None else None
            result = {
                "requests_per_minute": self.policy.requests_per_minute,
                "snapshot_seconds": self.policy.snapshot_seconds,
                "snapshot_age_seconds": round(age, 3) if age is not None else None,
                "snapshot_invalidated": snapshot is not None
                and self._snapshot_generation != self._generation,
                "snapshot_refreshes": self.snapshot_refreshes,
                "invalidations": self.invalidations,
                "last_invalidation": self.last_invalidation,
                "terminal_orders_cached": len(self._terminal),
                "timeouts": self.timeouts,
            }
        return {**result, **self.bucket.status()}


class BudgetTransport(httpx.BaseTransport):
    """Below the paper allowlist and the risk gate; it never alters a request."""

    def __init__(self, budget, delegate=None):
        if not isinstance(budget, BrokerBudget):
            raise ValueError("BROKER_BUDGET_REQUIRED")
        self.budget = budget
        self.delegate = delegate or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        trading = request.url.host == TRADING_HOST
        if request.method == "GET":
            if trading:
                self.budget.before_read()  # Raises before any I/O when refused.
            try:
                response = self.delegate.handle_request(request)
            except httpx.TimeoutException:
                self.budget.note_timeout()
                raise
            if trading:
                self.budget.observe(response)
            return response
        # Authorized mutation: counted, never delayed or refused, then snapshot invalidated.
        if trading:
            self.budget.before_mutation()
        try:
            response = self.delegate.handle_request(request)
        finally:
            if trading:
                self.budget.after_mutation()
        if trading:
            self.budget.observe(response)
        return response

    def close(self):
        self.delegate.close()


class BudgetedBroker:
    """Read-side proxy for a paper broker. Everything else is the wrapped broker.

    ``mutate`` and every other attribute resolve to the wrapped object, so the
    exact one-use authorization path is untouched. Account, positions and open
    orders read through this proxy are always fresh; the shared snapshot is used
    only where a caller asks for it explicitly.
    """

    def __init__(self, broker, governor):
        if not isinstance(governor, BrokerBudget) or isinstance(broker, BudgetedBroker):
            raise ValueError("BROKER_BUDGET_REQUIRED")
        self._broker = broker
        self.governor = governor

    def __getattr__(self, name):
        if name == "_broker":
            raise AttributeError(name)
        return getattr(self._broker, name)

    @property
    def wrapped(self):
        return self._broker

    def snapshot(self, *, max_age=None):
        return self.governor.snapshot(self._broker, max_age=max_age)

    def order(self, broker_order_id):
        return self.governor.order(self._broker, broker_order_id)

    def order_by_client_id(self, client_order_id):
        return self.governor.order_by_client_id(self._broker, client_order_id)

    def capital_activities(self, session_date):
        return self.governor.capital_activities(self._broker, session_date)

    def calendar(self, start, end):
        return self.governor.calendar(self._broker, start, end)

    def asset(self, symbol):
        return self.governor.asset(self._broker, symbol)
