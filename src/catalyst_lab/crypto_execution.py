"""Crypto request construction and deterministic recovery proposals; no broker I/O.

The supervisor persists a proposal and obtains an exact, one-use risk decision before
dispatch. A proposed mutation is not an order acknowledgement. Snapshot quantities
come from broker reconciliation, including fees; cumulative fills are not inventory.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from catalyst_lab.config import PAPER_ENDPOINT
from catalyst_lab.market import NY

TERMINAL = frozenset({"filled", "canceled", "cancelled", "expired", "rejected", "replaced"})
PENDING = frozenset({"pending_cancel", "pending_replace"})


@dataclass(frozen=True)
class CryptoDayPolicy:
    """Explicit opt-in NY account-day exits; historical setups retain their policy.

    The two cutoffs are measured back from the next local midnight, not from an
    assumed 24-hour UTC day. Maximum holding time is elapsed time from the first
    broker fill, including partial fills and time while the worker was offline.
    """

    policy_id: str
    entry_cutoff_minutes_before_midnight: int
    flatten_minutes_before_midnight: int
    max_hold_seconds: int

    def __post_init__(self):
        values = (
            self.entry_cutoff_minutes_before_midnight,
            self.flatten_minutes_before_midnight,
            self.max_hold_seconds,
        )
        if (
            self.policy_id != "CRYPTO_NY_DAY_PAPER_V1"
            or any(type(value) is not int for value in values)
            or not 0 < self.flatten_minutes_before_midnight
            < self.entry_cutoff_minutes_before_midnight <= 60
            or not 1 <= self.max_hold_seconds <= 86400
        ):
            raise ValueError("EXPLICIT_CRYPTO_DAY_POLICY_REQUIRED")

    @staticmethod
    def _midnight(at):
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError("AWARE_CRYPTO_FILL_TIMESTAMP_REQUIRED")
        tomorrow = at.astimezone(NY).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time.min, NY).astimezone(UTC)

    def entry_deadline(self, at):
        return self._midnight(at) - timedelta(
            minutes=self.entry_cutoff_minutes_before_midnight
        )

    def exit_deadline(self, first_fill_at):
        return min(
            first_fill_at.astimezone(UTC) + timedelta(seconds=self.max_hold_seconds),
            self.session_flat_at(first_fill_at),
        )

    def session_flat_at(self, at):
        return self._midnight(at) - timedelta(minutes=self.flatten_minutes_before_midnight)


class CryptoExecutionError(ValueError):
    pass


def decimal(value, *, positive=False):
    if isinstance(value, (bool, float)):
        raise CryptoExecutionError("EXACT_DECIMAL_REQUIRED")
    try:
        result = Decimal(value)
    except (ValueError, TypeError, ArithmeticError):
        raise CryptoExecutionError("INVALID_DECIMAL") from None
    if not result.is_finite() or (positive and result <= 0):
        raise CryptoExecutionError("INVALID_DECIMAL")
    return result


def text(value):
    return format(value, "f")


@dataclass(frozen=True)
class CryptoAsset:
    symbol: str
    min_order_size: Decimal
    min_trade_increment: Decimal
    price_increment: Decimal

    def __post_init__(self):
        import re

        if not re.fullmatch(r"[A-Z0-9]{1,16}/USD", self.symbol):
            raise CryptoExecutionError("USD_CRYPTO_PAIR_REQUIRED")
        for key in ("min_order_size", "min_trade_increment", "price_increment"):
            object.__setattr__(self, key, decimal(getattr(self, key), positive=True))

    @classmethod
    def from_broker(cls, payload):
        if (
            payload.get("class") != "crypto"
            or payload.get("status") != "active"
            or payload.get("tradable") is not True
            or payload.get("fractionable") is not True
        ):
            raise CryptoExecutionError("TRADABLE_CRYPTO_METADATA_REQUIRED")
        try:
            return cls(**{k: payload[k] for k in cls.__dataclass_fields__})
        except KeyError:
            raise CryptoExecutionError("CRYPTO_INCREMENT_METADATA_REQUIRED") from None

    def quantity(self, requested):
        requested = decimal(requested, positive=True)
        qty = (requested / self.min_trade_increment).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.min_trade_increment
        if qty < self.min_order_size:
            raise CryptoExecutionError("QUANTITY_BELOW_BROKER_MINIMUM")
        return qty

    def price(self, value):
        value = decimal(value, positive=True)
        if value % self.price_increment:
            raise CryptoExecutionError("PRICE_OFF_BROKER_INCREMENT")
        return value

    def price_at_or_above(self, value):
        """Lowest broker-grid price not below ``value``; an on-grid value is returned as is.

        Raising a sell stop to the grid tightens it and never loosens it.
        """
        value = decimal(value, positive=True)
        try:
            if value % self.price_increment:
                value = (value // self.price_increment + 1) * self.price_increment
            return self.price(value)
        except ArithmeticError:
            raise CryptoExecutionError("PRICE_GRID_UNAVAILABLE") from None


LEVEL_NAMES = ("entry_trigger", "max_entry_price", "stop", "target")


def off_grid_levels(price_increment, levels):
    """Return ``{name: price}`` for levels that are not multiples of the broker increment.

    Absent level names are ignored. Values must be exact decimals, never floats.
    """
    increment = decimal(price_increment, positive=True)
    found = {}
    for name in LEVEL_NAMES:
        if name not in levels:
            continue
        value = decimal(levels[name], positive=True)
        try:
            if value % increment:
                found[name] = value
        except ArithmeticError:
            raise CryptoExecutionError("PRICE_GRID_UNAVAILABLE") from None
    return found


def client_id(operation_key, action):
    if not operation_key or not action:
        raise CryptoExecutionError("PERSISTED_OPERATION_KEY_REQUIRED")
    return "cl-crypto-" + uuid5(NAMESPACE_URL, f"{operation_key}:{action}").hex


def _body(asset, qty, side, kind, operation_key):
    return {
        "symbol": asset.symbol,
        "qty": text(asset.quantity(qty)),
        "side": side,
        "type": kind,
        "time_in_force": "gtc",
        "client_order_id": client_id(operation_key, kind + "-" + side),
    }


def build_limit_entry(asset, qty, maximum_entry, *, operation_key):
    """Risk computes quantity; this builder only rounds down to venue precision."""
    payload = _body(asset, qty, "buy", "limit", operation_key)
    payload["limit_price"] = text(asset.price(maximum_entry))
    return payload


def build_stop_limit(asset, qty, stop, limit, *, operation_key):
    stop, limit = asset.price(stop), asset.price(limit)
    if limit > stop:
        raise CryptoExecutionError("SELL_STOP_LIMIT_ABOVE_STOP")
    payload = _body(asset, qty, "sell", "stop_limit", operation_key)
    payload.update(stop_price=text(stop), limit_price=text(limit))
    return payload


def build_market_exit(asset, qty_available, *, operation_key):
    return _body(asset, qty_available, "sell", "market", operation_key)


def native_stop_levels(asset, stop):
    """The native stop-limit prices protection sends for a desired ``stop``: the stop and its
    limit one increment below (never below one increment), each raised to the broker grid,
    exactly as ``ManagedExecution.manage`` and ``plan_crypto_recovery`` derive them."""
    stop = decimal(stop, positive=True)
    limit = max(asset.price_increment, stop - asset.price_increment)
    return asset.price_at_or_above(stop), asset.price_at_or_above(limit)


@dataclass(frozen=True)
class MutationProposal:
    action: Literal[
        "CRYPTO_ENTRY", "CRYPTO_PROTECT", "CRYPTO_CANCEL", "CRYPTO_EXIT", "CRYPTO_AMEND"
    ]
    method: Literal["POST", "DELETE", "PATCH"]
    path: str
    payload: dict
    reason: str
    endpoint: str = PAPER_ENDPOINT

    def __post_init__(self):
        import re

        if self.endpoint != PAPER_ENDPOINT:
            raise CryptoExecutionError("PAPER_ENDPOINT_REQUIRED")
        order_path = re.fullmatch(r"/v2/orders/[A-Za-z0-9-]{1,128}", self.path)
        valid = (self.method == "POST" and self.path == "/v2/orders") or (
            self.method == "DELETE" and order_path
        ) or (
            # CRYPTO_MAINTENANCE_V1 only: a price-only replace of an owned stop-limit.
            self.method == "PATCH" and order_path and self.action == "CRYPTO_AMEND"
            and self.payload and set(self.payload) <= {"stop_price", "limit_price"}
        )
        if not valid or (self.method == "DELETE" and self.payload) or (
            self.action == "CRYPTO_AMEND" and self.method != "PATCH"
        ):
            raise CryptoExecutionError("INVALID_CRYPTO_MUTATION")


@dataclass(frozen=True)
class CryptoOrder:
    broker_id: str
    client_order_id: str
    role: Literal["ENTRY", "PROTECT", "EXIT"]
    qty: Decimal
    filled_qty: Decimal
    status: str
    owned: bool
    stop_price: Decimal | None = None
    limit_price: Decimal | None = None
    # The broker ID of the order this one replaces (Alpaca's ``replaces``), if any.
    replaces: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "qty", decimal(self.qty, positive=True))
        object.__setattr__(self, "filled_qty", decimal(self.filled_qty))
        if self.role not in {"ENTRY", "PROTECT", "EXIT"}:
            raise CryptoExecutionError("INVALID_CRYPTO_ORDER_ROLE")
        if not 0 <= self.filled_qty <= self.qty:
            raise CryptoExecutionError("INVALID_BROKER_FILL_QUANTITY")
        for key in ("stop_price", "limit_price"):
            if getattr(self, key) is not None:
                object.__setattr__(self, key, decimal(getattr(self, key), positive=True))

    @property
    def active(self):
        return self.status not in TERMINAL

    @property
    def remaining(self):
        return self.qty - self.filled_qty if self.active else Decimal(0)


@dataclass(frozen=True)
class CryptoSnapshot:
    """One reconciled symbol snapshot; IDs and revision must survive restarts.

    `lookup_complete` may include an unknown submission only after GET by stable
    client-order-ID and fresh open-order/position reads. A timeout alone is not absence.
    `position_qty` and `qty_available` are broker quantities, not sums of fills.
    """

    revision: str
    observed_at: datetime
    position_qty: Decimal
    qty_available: Decimal
    orders: tuple[CryptoOrder, ...]
    reconciled: bool
    uncertain_client_ids: tuple[str, ...]
    lookup_complete: tuple[str, ...]
    bid: Decimal | None
    quote_at: datetime | None
    stop_breached_at: datetime | None

    def __post_init__(self):
        object.__setattr__(self, "position_qty", decimal(self.position_qty))
        object.__setattr__(self, "qty_available", decimal(self.qty_available))
        if not self.revision or self.observed_at.tzinfo is None:
            raise CryptoExecutionError("TIMESTAMPED_BROKER_REVISION_REQUIRED")
        if self.bid is not None:
            object.__setattr__(self, "bid", decimal(self.bid, positive=True))


@dataclass(frozen=True)
class CryptoProtectionPolicy:
    """All timing is caller-supplied and versioned; no implicit crypto session rule."""

    snapshot_max_age_seconds: Decimal
    quote_max_age_seconds: Decimal
    stop_limit_timeout_seconds: Decimal

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            object.__setattr__(self, key, decimal(getattr(self, key), positive=True))


@dataclass(frozen=True)
class RecoveryPlan:
    state: str
    reason: str
    proposals: tuple[MutationProposal, ...] = ()
    residual_qty: Decimal = Decimal(0)
    # Audit facts, e.g. a stop raised to the broker grid; empty for on-grid levels.
    details: dict = field(default_factory=dict)


def _age(now, earlier):
    if now.tzinfo is None or earlier is None or earlier.tzinfo is None:
        return None
    return Decimal(str((now - earlier).total_seconds()))


def _cancel(order, reason):
    return MutationProposal("CRYPTO_CANCEL", "DELETE", "/v2/orders/" + order.broker_id, {}, reason)


def plan_crypto_recovery(
    asset: CryptoAsset,
    snapshot: CryptoSnapshot,
    policy: CryptoProtectionPolicy,
    *,
    now: datetime,
    operation_key: str,
    stop: Decimal,
    stop_limit: Decimal,
    target: Decimal,
    exit_requested: bool,
    exit_deadline: datetime,
    time_exit_reason: str = "TIME_EXIT",
    retain_entry: bool = False,
    entry_cancel_reason: str = "CANCEL_REMAINING_ENTRY",
    replace_stop: str = "CANCEL",
    protect_after_entries: bool = False,
):
    """Plan recovery from broker truth without submitting or changing authoritative state.

    One reconciled view cannot authorize future inventory. The caller serializes
    per-symbol plans, persists unknown dispatches, and obtains fresh risk rows for
    each proposal. Cancel acknowledgements do not free reserved sell quantities.

    Only the native stop-limit is sent to the broker, so only its prices must sit on
    the broker price grid. The app-managed target is compared with quotes and is never
    grid-checked. Exits are planned before any grid logic, so an off-grid level cannot
    block a time, target, stop-grace or authorized exit. An off-grid stop (or stop
    limit) is raised to the next grid price, which tightens and never loosens it, and
    ``details`` records both values. If the raised stop would already be at or above a
    fresh bid, or at or above the target, the plan is HALTED as
    ``CRYPTO_STOP_UNSNAPPABLE`` instead of sending a stop that triggers at once.

    ``time_exit_reason`` names the exit once ``exit_deadline`` has passed: ``TIME_EXIT``, or
    ``HOLD_24H_EXIT`` for a setup under ``CRYPTO_24H_HOLD_V1`` (crypto_holding.py).

    ``CRYPTO_PARTIAL_ENTRY_V1`` and ``CRYPTO_MAINTENANCE_V1`` (crypto_maintenance.py) only; the
    defaults are every other setup's plan, unchanged. ``retain_entry``: a partly filled entry
    keeps working while its filled quantity is protected (an exit still cancels it);
    ``entry_cancel_reason`` names the cancel of an entry remainder outside an exit.
    ``replace_stop="PATCH"``: a raised stop replaces each resting stop-limit in place (one
    price-only PATCH each, ``REPLACE_STOP``) instead of cancelling it (``TIGHTEN_STOP``).
    ``protect_after_entries``: new protection waits until a working entry remainder is
    cancelled (after the broker refused protection while it worked).
    """
    if replace_stop not in {"CANCEL", "PATCH"}:
        raise CryptoExecutionError("INVALID_STOP_REPLACE_MODE")
    stop, stop_limit, target = (decimal(v, positive=True) for v in (stop, stop_limit, target))
    if stop_limit > stop or target <= stop:
        raise CryptoExecutionError("INVALID_EXIT_GEOMETRY")
    if now.tzinfo is None or exit_deadline.tzinfo is None:
        raise CryptoExecutionError("AWARE_DEADLINE_REQUIRED")
    age = _age(now, snapshot.observed_at)
    if not snapshot.reconciled or age is None or not 0 <= age <= policy.snapshot_max_age_seconds:
        return RecoveryPlan("RECONCILE_REQUIRED", "BROKER_SNAPSHOT_NOT_CURRENT")
    if set(snapshot.uncertain_client_ids) - set(snapshot.lookup_complete):
        return RecoveryPlan("RECONCILE_REQUIRED", "UNKNOWN_SUBMISSION_LOOKUP_REQUIRED")
    located = {o.client_order_id for o in snapshot.orders}
    if set(snapshot.uncertain_client_ids) - located:
        # Recovery of an absent timed-out request must retain the ORIGINAL payload
        # and client ID. A new snapshot revision must not manufacture a second ID.
        return RecoveryPlan("RECONCILE_REQUIRED", "UNKNOWN_NOT_LOCATED_RETRY_ORIGINAL_REQUEST_ONLY")
    if snapshot.position_qty < 0:
        return RecoveryPlan(
            "HALTED", "UNEXPECTED_SHORT_POSITION", residual_qty=snapshot.position_qty
        )
    if snapshot.qty_available < 0 or snapshot.qty_available > snapshot.position_qty:
        return RecoveryPlan("HALTED", "INVALID_BROKER_AVAILABLE_QUANTITY")
    active = tuple(o for o in snapshot.orders if o.active)
    # A price-only PATCH makes Alpaca create a new order naming the old one in ``replaces``;
    # until the old one turns ``replaced``, a snapshot can list both. The old order is not a
    # second sell reservation: plan from its replacement. On 2026-09-28 (UNI, 15:31 UTC) the
    # doubled reservation cancelled the new stop-limit and left the position without a stop
    # order for about 6 s. A replacement that is no longer active supersedes nothing.
    superseded = {o.replaces for o in active if o.replaces}
    active = tuple(o for o in active if o.broker_id not in superseded)
    if any(not o.owned for o in active):
        return RecoveryPlan("HALTED", "UNEXPLAINED_BROKER_ORDER")
    entries = tuple(o for o in active if o.role == "ENTRY")
    sells = tuple(o for o in active if o.role != "ENTRY")
    reserved = sum((o.remaining for o in sells), Decimal(0))
    if reserved > snapshot.position_qty:
        cancels = tuple(
            _cancel(o, "SELL_RESERVATION_EXCEEDS_POSITION")
            for o in active
            if o.status not in PENDING
        )
        return RecoveryPlan(
            "CANCELING", "SELL_RESERVATION_EXCEEDS_POSITION", cancels, snapshot.position_qty
        )
    if snapshot.qty_available + reserved > snapshot.position_qty:
        return RecoveryPlan("RECONCILE_REQUIRED", "AVAILABLE_QUANTITY_DISAGREES_WITH_ORDERS")
    if snapshot.position_qty == 0:
        has_fills = any(o.filled_qty > 0 for o in snapshot.orders)
        if entries and not sells and not has_fills and not exit_requested and now < exit_deadline:
            return RecoveryPlan("ENTRY_WORKING", "AWAIT_FIRST_FILL")
        if active:
            cancels = tuple(
                _cancel(o, "FLAT_CANCEL_WORKING_ORDERS") for o in active if o.status not in PENDING
            )
            return RecoveryPlan("CANCELING", "FLAT_WITH_WORKING_ORDERS", cancels)
        return RecoveryPlan("FLAT", "ZERO_BROKER_POSITION_AND_ORDERS")

    quote_age = _age(now, snapshot.quote_at)
    fresh_quote = quote_age is not None and 0 <= quote_age <= policy.quote_max_age_seconds
    target_touched = fresh_quote and snapshot.bid is not None and snapshot.bid >= target
    breach_age = _age(now, snapshot.stop_breached_at)
    stop_stuck = breach_age is not None and breach_age >= policy.stop_limit_timeout_seconds
    protection_rejected = any(
        o.role == "PROTECT" and o.status == "rejected" for o in snapshot.orders
    )
    exit_reason = (
        "PROTECTION_REJECTED"
        if protection_rejected
        else "STOP_LIMIT_NOT_FILLED"
        if stop_stuck
        else time_exit_reason
        if now >= exit_deadline
        else "AUTHORIZED_EXIT"
        if exit_requested
        else "TARGET_EXIT"
        if target_touched
        else None
    )
    exit_entry_cancels = tuple(
        _cancel(o, "CANCEL_REMAINING_ENTRY") for o in entries if o.status not in PENDING
    )
    # Outside an exit: the entry remainder is cancelled (today), or keeps working while
    # CRYPTO_PARTIAL_ENTRY_V1 retains it; an exit always cancels it (exit_entry_cancels).
    cancel_entries = () if retain_entry else tuple(
        _cancel(o, entry_cancel_reason) for o in entries if o.status not in PENDING
    )
    if exit_reason:
        cancel_protection = tuple(
            _cancel(o, exit_reason)
            for o in sells
            if o.role == "PROTECT" and o.status not in PENDING
        )
        if entries or any(o.role == "PROTECT" for o in sells):
            return RecoveryPlan(
                "CANCELING", exit_reason, exit_entry_cancels + cancel_protection,
                snapshot.position_qty,
            )
        if any(o.role == "EXIT" for o in sells):
            return RecoveryPlan(
                "EXIT_WORKING", "AWAIT_EXISTING_EXIT", residual_qty=snapshot.position_qty
            )
        if snapshot.qty_available != snapshot.position_qty:
            return RecoveryPlan("RECONCILE_REQUIRED", "SELL_RESERVATION_NOT_RELEASED")
        try:
            payload = build_market_exit(
                asset,
                snapshot.qty_available,
                operation_key=operation_key + ":exit:" + snapshot.revision,
            )
        except CryptoExecutionError as error:
            if str(error) != "QUANTITY_BELOW_BROKER_MINIMUM":
                raise
            return RecoveryPlan(
                "HALTED", "RESIDUAL_BELOW_BROKER_MINIMUM", residual_qty=snapshot.position_qty
            )
        proposal = MutationProposal("CRYPTO_EXIT", "POST", "/v2/orders", payload, exit_reason)
        return RecoveryPlan("EXIT_REQUIRED", exit_reason, (proposal,), snapshot.position_qty)

    if any(o.role == "EXIT" for o in sells):
        return RecoveryPlan(
            "EXIT_WORKING", "AWAIT_EXISTING_EXIT", residual_qty=snapshot.position_qty
        )
    protection = tuple(o for o in sells if o.role == "PROTECT")
    # A desired trail never loosens a stop. Raising it uses cancel/reconcile/new,
    # because fractional PATCH support is not assumed and old sells reserve quantity.
    if any(o.stop_price is None or o.limit_price is None for o in protection):
        return RecoveryPlan("HALTED", "PROTECTION_PRICE_UNKNOWN")
    # The native stop-limit is the only price sent to the broker: raise it to the grid.
    requested = {
        "requested_stop": stop,
        "requested_stop_limit": stop_limit,
        "price_increment": asset.price_increment,
    }
    try:
        native_stop = asset.price_at_or_above(stop)
        native_limit = asset.price_at_or_above(stop_limit)
    except CryptoExecutionError:
        return RecoveryPlan(
            "HALTED", "CRYPTO_STOP_UNSNAPPABLE", cancel_entries, snapshot.position_qty, requested
        )
    details = {}
    if (native_stop, native_limit) != (stop, stop_limit):
        details = {
            "stop_snapped_to_grid": True,
            **requested,
            "native_stop": native_stop,
            "native_stop_limit": native_limit,
        }
    bid = snapshot.bid if fresh_quote else None
    # A raised stop at or above the bid would trigger at once; never send one.
    unsnappable = native_stop != stop and (
        native_stop >= target or (bid is not None and native_stop >= bid)
    )
    if unsnappable:
        details.update(bid=bid, target=target)
    stop, stop_limit = native_stop, native_limit
    if any(o.stop_price > stop or o.limit_price > stop_limit for o in protection):
        return RecoveryPlan("HALTED", "STOP_WIDENING_REFUSED", details=details)
    if any(o.stop_price != stop or o.limit_price != stop_limit for o in protection):
        if unsnappable:
            # Keep the existing lower native stop rather than cancel it for one we cannot send.
            return RecoveryPlan(
                "HALTED",
                "CRYPTO_STOP_UNSNAPPABLE",
                cancel_entries,
                snapshot.position_qty,
                details,
            )
        if replace_stop == "PATCH":
            # CRYPTO_MAINTENANCE_V1: replace each resting stop-limit in place; the old order
            # protects until the broker acknowledges its replacement.
            amends = tuple(
                MutationProposal(
                    "CRYPTO_AMEND", "PATCH", "/v2/orders/" + o.broker_id,
                    {"stop_price": text(stop), "limit_price": text(stop_limit)}, "REPLACE_STOP",
                )
                for o in protection
                if o.status not in PENDING
                and (o.stop_price != stop or o.limit_price != stop_limit)
            )
            return RecoveryPlan(
                "REPLACING", "REPLACE_STOP", cancel_entries + amends, snapshot.position_qty,
                details,
            )
        cancels = tuple(_cancel(o, "TIGHTEN_STOP") for o in protection if o.status not in PENDING)
        return RecoveryPlan(
            "CANCELING", "TIGHTEN_STOP", cancel_entries + cancels, snapshot.position_qty, details
        )
    if any(o.status in PENDING for o in sells):
        return RecoveryPlan(
            "RECONCILE_REQUIRED",
            "BROKER_MUTATION_PENDING",
            residual_qty=snapshot.position_qty,
            details=details,
        )
    uncovered = snapshot.position_qty - reserved
    if uncovered == 0:
        return RecoveryPlan(
            "PROTECTED", "NATIVE_STOP_LIMIT_PRESENT", cancel_entries, snapshot.position_qty, details
        )
    if snapshot.qty_available < uncovered:
        return RecoveryPlan(
            "RECONCILE_REQUIRED",
            "UNEXPLAINED_SELL_RESERVATION",
            residual_qty=uncovered,
            details=details,
        )
    if unsnappable:
        return RecoveryPlan("HALTED", "CRYPTO_STOP_UNSNAPPABLE", cancel_entries, uncovered, details)
    if protect_after_entries and entries:
        # CRYPTO_PARTIAL_ENTRY_V1: the broker refused protection while the entry remainder
        # worked, so the remainder is cancelled first and protection follows once it is gone.
        return RecoveryPlan(
            "CANCELING", "CANCEL_ENTRY_BEFORE_PROTECT", cancel_entries, uncovered, details
        )
    try:
        payload = build_stop_limit(
            asset,
            uncovered,
            stop,
            stop_limit,
            operation_key=operation_key + ":protect:" + snapshot.revision,
        )
    except CryptoExecutionError as error:
        if str(error) != "QUANTITY_BELOW_BROKER_MINIMUM":
            raise
        return RecoveryPlan(
            "HALTED",
            "UNPROTECTED_RESIDUAL_BELOW_BROKER_MINIMUM",
            cancel_entries,
            uncovered,
            details,
        )
    proposal = MutationProposal(
        "CRYPTO_PROTECT", "POST", "/v2/orders", payload, "PROTECT_BROKER_INVENTORY"
    )
    return RecoveryPlan(
        "PROTECTION_REQUIRED",
        "UNCOVERED_BROKER_INVENTORY",
        (proposal,) + cancel_entries,
        uncovered,
        details,
    )
