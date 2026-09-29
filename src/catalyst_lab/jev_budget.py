"""``JEV_SPEND_METER_V1`` and ``JEV_SPEND_GUARD_V1``: the owner's monthly Jev budget (package
jev-budget, 2026-09-28).

Owner, 2026-09-28: the monthly budget for the whole system is $70 at most, of which Jev may use
``JEV_MONTHLY_BUDGET_USD`` (the cloud's value is 50; Railway has its own $15 alert and $20 hard
limit). The owner also wants Jev to review every open trade every minute
(``CRYPTO_MAINTENANCE_V2``). This module meters what Jev costs and decides, as a named version,
how often routine maintenance reviews may run so the month stays inside the budget. Nothing
here touches a broker, a stop, a target, selection or a 24-hour review.

**The meter (``JEV_SPEND_METER_V1``)** is derived, never written: every TypeSafe call is already
recorded exactly, as its request (``lab.jev_requests.request_json``, the exact bytes posted:
``jev_review`` sends ``request_json.encode()``, and ``encoded`` writes ASCII only) and one receipt
per attempt (``lab.jev_receipts``). A receipt is a provider call when it has an HTTP status or
its outcome is ``TRANSPORT_FAILURE`` (a timeout may have reached the provider); a receipt
without one (``CIRCUIT_OPEN``, ``CREDENTIAL_UNAVAILABLE``, an ``EXPIRED`` attempt that never
started) sent nothing. Each call is priced from its exact request bytes:
``tokens = bytes / JEV_BYTES_PER_TOKEN`` (default 3: a conservative estimate for JSON) and
``cost = tokens x JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD / 1,000,000`` (default 0.042, TypeSafe's
published jev-1.13 price; output is free and the context counts once per request). Retries,
HTTP errors and rate-limit refusals count as calls: an over-estimate, never an under-estimate.
A call belongs to the New York day and calendar month of its receipt's ``started_at``. The
tables are append-only and every row is in the audit chain, so a restart rebuilds exactly the
same totals: at start the meter reads this month's receipts (and the last 24 hours) once,
newest first by ``event_seq``, then only receipts with a higher ``event_seq`` (appended under
the audit lock, so a later receipt is never visible before an earlier one). Changing the price
or bytes per token re-prices the whole month (a calibration, not new spend); the guard records
each configuration it runs with (``JEV_SPEND_GUARD_CONFIGURED``).

**The guard (``JEV_SPEND_GUARD_V1``)** turns the meter into a tier, evaluated at most every
``REFRESH_SECONDS`` and recorded as ``JEV_BUDGET_TIER_CHANGED`` whenever it changes. With B the
budget, MTD the month-to-date spend and P1 the *per-minute projection*: MTD plus the last 24
hours' spend re-weighted to the per-minute cadence (a routine review made every k minutes
stands for k per-minute reviews; ``routine_weight`` in its request's identity), times the days
left in the month. P1 is what the month would cost if every guarded trade were reviewed every
minute from now on at the last day's demand, so throttling does not lower it and the tier does
not oscillate:

* ``EXHAUSTED`` when MTD >= B x 0.98 (the last 2% is a reserve for selection, 24-hour reviews
  and early exits, which the guard never stops): no maintenance reviews at all.
* Otherwise throttling is on when P1 > B x 0.98, and once on it stays on until P1 <= B x 0.93
  (hysteresis). Throttling on: ``TIGHT`` (a routine review every 15 completed minutes) when
  MTD >= B x 0.90, else ``THROTTLED`` (every 5 completed minutes).
* Otherwise ``NORMAL``: the per-minute cadence of ``CRYPTO_MAINTENANCE_V2``.

Event-triggered reviews (milestones, near target or stop, news, a Bitcoin shock) run in every
tier below ``EXHAUSTED``. Only setups whose state records ``CRYPTO_MAINTENANCE_V3`` are governed
(``crypto_maintenance``); ``V1`` and ``V2`` setups keep their recorded cadence, and their spend
counts. When the meter cannot be read the last decision is used for up to ``STALE_SECONDS``,
then the tier is ``UNAVAILABLE``: guarded trades are not reviewed (fail closed on spend) and the
watchdog alarms. Protection, stops and targets never depend on the guard.
"""

import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.market import NY
from catalyst_lab.repository import json_safe

D = Decimal
VERSION = "JEV_SPEND_GUARD_V1"
METER_VERSION = "JEV_SPEND_METER_V1"
TIER_EVENT = "JEV_BUDGET_TIER_CHANGED"
CONFIG_EVENT = "JEV_SPEND_GUARD_CONFIGURED"
NORMAL, THROTTLED, TIGHT, EXHAUSTED = "NORMAL", "THROTTLED", "TIGHT", "EXHAUSTED"
UNAVAILABLE = "UNAVAILABLE"  # Not a tier: the guard could not decide (guarded reviews wait).
TIERS = (NORMAL, THROTTLED, TIGHT, EXHAUSTED)

# --- The version's numbers (JEV_SPEND_GUARD_V1) ------------------------------------------------
RESERVE_FRACTION = D("0.02")  # EXHAUSTED from 98% of the budget.
THROTTLE_ABOVE_FRACTION = D("0.98")  # Throttling starts when P1 exceeds 98% of the budget ...
NORMAL_AT_OR_BELOW_FRACTION = D("0.93")  # ... and ends only when P1 is back at or below 93%.
TIGHT_FROM_FRACTION = D("0.90")  # While throttling, TIGHT from 90% of the budget spent.
WINDOW_SECONDS = 86400  # The demand behind the projections: the last 24 hours.
REVIEW_BAR_SECONDS = {NORMAL: 60, THROTTLED: 300, TIGHT: 900, EXHAUSTED: None}
REFRESH_SECONDS = 5  # A decision is re-evaluated at most this often.
STALE_SECONDS = 120  # An unreadable meter keeps the last decision this long, then UNAVAILABLE.
ALERT_SECONDS = 1800  # The watchdog alerts a tier change for 30 minutes (managed_ops).
TIMEZONE = "America/New_York"

# --- Configuration (deploy example, railway.ts, cloud_config) -----------------------------------
BUDGET_ENV = "JEV_MONTHLY_BUDGET_USD"
PRICE_ENV = "JEV_PRICE_PER_MILLION_INPUT_TOKENS_USD"
BYTES_PER_TOKEN_ENV = "JEV_BYTES_PER_TOKEN"
ENV_NAMES = (BUDGET_ENV, PRICE_ENV, BYTES_PER_TOKEN_ENV)
DEFAULT_PRICE = D("0.042")  # USD per million input tokens, jev-1.13 (TypeSafe's pricing guide).
DEFAULT_BYTES_PER_TOKEN = D("3")  # One token per three request bytes: conservative for JSON.
_PLAIN = re.compile(r"[0-9]{1,9}(\.[0-9]{1,9})?")
_BOUNDS = {  # name: (low, high, maximum decimal places), inclusive
    BUDGET_ENV: (D("0.01"), D("100000"), 2),
    PRICE_ENV: (D("0.000001"), D("1000"), 6),
    BYTES_PER_TOKEN_ENV: (D("1"), D("10"), 3),
}

# Jev request kinds, by question set version (``lab.jev_requests.question_set_version``).
KIND_PREFIXES = (
    ("JEV_MANAGED_POSITION_QUESTIONS_", "MAINTENANCE"),
    ("JEV_DAY_REVIEW_QUESTIONS_", "DAY_REVIEW"),
    ("JEV_EARLY_EXIT_QUESTIONS_", "EARLY_EXIT"),
    ("MUSE_JEV_COMPARATIVE_QUALITY_", "QUALITY"),
    ("SKEPTIC_QUESTIONS_", "SELECTION"),
    ("PROVIDER_HEALTH_", "HEALTH_PROBE"),
)
KINDS = ("SELECTION", "QUALITY", "MAINTENANCE", "DAY_REVIEW", "EARLY_EXIT", "HEALTH_PROBE",
         "OTHER")
MAX_ROUTINE_WEIGHT = 15  # The longest routine interval (TIGHT) in minutes.
LOAD_CHUNK = 5000
LOAD_MARGIN = timedelta(hours=1)  # Receipts land within one review deadline of their start.
_ROWS = """SELECT r.event_seq,r.started_at,octet_length(q.request_json) AS request_bytes,
    q.question_set_version,
    q.evidence_identity->'spend_guard'->>'routine_weight' AS routine_weight,
    (r.http_status IS NOT NULL OR r.outcome='TRANSPORT_FAILURE') AS provider_call
    FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)"""


class SpendConfigError(ValueError):
    """A refused setting; carries the variable name only, never a value."""

    def __init__(self, code, name):
        super().__init__(code)
        self.code, self.name = code, name


def kind_of(question_set_version):
    version = question_set_version or ""
    if "_PICK_QUESTIONS_" in version:
        return "SELECTION"
    for prefix, kind in KIND_PREFIXES:
        if version.startswith(prefix):
            return kind
    return "OTHER"


def _setting(environ, name, default):
    raw = environ.get(name)
    if raw is None or raw == "":
        if default is None:
            raise SpendConfigError("JEV_SPEND_SETTING_MISSING", name)
        return default
    return parse_setting(name, raw)


def parse_setting(name, raw):
    """One setting as an exact Decimal within its bounds; the name in any refusal."""
    low, high, places = _BOUNDS[name]
    if not isinstance(raw, str) or not _PLAIN.fullmatch(raw):
        raise SpendConfigError("JEV_SPEND_SETTING_INVALID", name)
    value = D(raw)
    if not low <= value <= high or -value.as_tuple().exponent > places:
        raise SpendConfigError("JEV_SPEND_SETTING_INVALID", name)
    return value


@dataclass(frozen=True)
class SpendConfig:
    """The deployment's budget and price estimate (not part of the version: the owner can raise
    the budget or recalibrate the estimate; each configuration a runtime runs with is recorded)."""

    monthly_budget_usd: Decimal
    price_per_million_input_tokens_usd: Decimal = DEFAULT_PRICE
    bytes_per_token: Decimal = DEFAULT_BYTES_PER_TOKEN

    def __post_init__(self):
        for name, value in ((BUDGET_ENV, self.monthly_budget_usd),
                            (PRICE_ENV, self.price_per_million_input_tokens_usd),
                            (BYTES_PER_TOKEN_ENV, self.bytes_per_token)):
            low, high, _ = _BOUNDS[name]
            if type(value) is not Decimal or not value.is_finite() or not low <= value <= high:
                raise SpendConfigError("JEV_SPEND_SETTING_INVALID", name)

    @classmethod
    def from_env(cls, environ, *, required):
        """The three settings, or None when the budget is absent and not ``required``. A
        missing budget (when required) or any invalid value raises, naming the variable; the
        price and bytes per token default to 0.042 and 3 when absent."""
        if not required and not environ.get(BUDGET_ENV):
            for name in (PRICE_ENV, BYTES_PER_TOKEN_ENV):  # Still never silently ignored.
                if environ.get(name):
                    parse_setting(name, environ[name])
            return None
        return cls(_setting(environ, BUDGET_ENV, None),
                   _setting(environ, PRICE_ENV, DEFAULT_PRICE),
                   _setting(environ, BYTES_PER_TOKEN_ENV, DEFAULT_BYTES_PER_TOKEN))

    def record(self):
        return {"monthly_budget_usd": str(self.monthly_budget_usd),
                "price_per_million_input_tokens_usd": str(self.price_per_million_input_tokens_usd),
                "bytes_per_token": str(self.bytes_per_token)}

    def tokens(self, request_bytes):
        with localcontext() as context:
            context.prec = 50
            return D(request_bytes) / self.bytes_per_token

    def cost(self, request_bytes):
        """The estimated USD cost of ``request_bytes`` sent to TypeSafe (exact Decimal)."""
        with localcontext() as context:
            context.prec = 50
            return (D(request_bytes) * self.price_per_million_input_tokens_usd
                    / (self.bytes_per_token * D(1_000_000)))


def policy_record():
    """``JEV_SPEND_GUARD_V1``'s fixed numbers, as recorded in its events and the contract."""
    return {
        "policy_id": VERSION, "meter_version": METER_VERSION, "timezone": TIMEZONE,
        "reserve_fraction": str(RESERVE_FRACTION),
        "throttle_above_fraction": str(THROTTLE_ABOVE_FRACTION),
        "normal_at_or_below_fraction": str(NORMAL_AT_OR_BELOW_FRACTION),
        "tight_from_fraction": str(TIGHT_FROM_FRACTION),
        "window_seconds": WINDOW_SECONDS,
        "review_bar_seconds": dict(REVIEW_BAR_SECONDS),
        "provider_call": "HTTP_STATUS_PRESENT_OR_TRANSPORT_FAILURE",
        "request_bytes": "OCTET_LENGTH_OF_REQUEST_JSON",
        "attribution": "RECEIPT_STARTED_AT_NEW_YORK_MONTH",
    }


# --- Calendar (New York) -------------------------------------------------------------------------


def month_start(now):
    local = now.astimezone(NY)
    return datetime(local.year, local.month, 1, tzinfo=NY)


def next_month_start(now):
    local = now.astimezone(NY)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=NY)


def day_start(now):
    local = now.astimezone(NY)
    return datetime(local.year, local.month, local.day, tzinfo=NY)


def month_label(now):
    local = now.astimezone(NY)
    return f"{local.year:04d}-{local.month:02d}"


# --- The tier (pure) -----------------------------------------------------------------------------


def throttling(*, projection_per_minute, budget, previous_tier):
    """Whether routine reviews are throttled: P1 above 98% of the budget, or, once throttled
    (any tier other than NORMAL recorded last), until P1 is back at or below 93%."""
    with localcontext() as context:
        context.prec = 50
        if previous_tier in (None, NORMAL):
            return projection_per_minute > budget * THROTTLE_ABOVE_FRACTION
        return projection_per_minute > budget * NORMAL_AT_OR_BELOW_FRACTION


def tier_for(*, month_to_date, projection_per_minute, budget, previous_tier):
    """``(tier, rule)`` of ``JEV_SPEND_GUARD_V1`` (Decimal USD amounts, exact comparisons)."""
    with localcontext() as context:
        context.prec = 50
        if month_to_date >= budget * (1 - RESERVE_FRACTION):
            return EXHAUSTED, "MONTH_TO_DATE_AT_RESERVE"
        if not throttling(projection_per_minute=projection_per_minute, budget=budget,
                          previous_tier=previous_tier):
            return NORMAL, "PER_MINUTE_PROJECTION_WITHIN_BUDGET"
        if month_to_date >= budget * TIGHT_FROM_FRACTION:
            return TIGHT, "MONTH_TO_DATE_NINETY_PERCENT_AND_OVER_PROJECTION"
        return THROTTLED, "PER_MINUTE_PROJECTION_OVER_BUDGET"


def thresholds(budget):
    with localcontext() as context:
        context.prec = 50
        return {"throttle_above_usd": budget * THROTTLE_ABOVE_FRACTION,
                "normal_at_or_below_usd": budget * NORMAL_AT_OR_BELOW_FRACTION,
                "tight_from_usd": budget * TIGHT_FROM_FRACTION,
                "exhausted_from_usd": budget * (1 - RESERVE_FRACTION)}


def usd(value):
    """Display rounding for the status and events (half-even, 0.0001 USD); decisions use the
    exact values."""
    if value is None:
        return None
    try:
        return str(D(value).quantize(D("0.0001"), rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        return str(value)


# --- The meter -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One provider call: its receipt's sequence and start, the exact request bytes, its kind
    and its per-minute weight (``routine_weight``: k for a routine review made every k
    minutes, else 1)."""

    event_seq: int
    started_at: datetime
    request_bytes: int
    kind: str
    weight: int


def _weight(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= MAX_ROUTINE_WEIGHT else 1


@dataclass
class _Totals:
    calls: int = 0
    request_bytes: int = 0
    by_kind: dict = field(default_factory=dict)

    def add(self, call):
        self.calls += 1
        self.request_bytes += call.request_bytes
        calls, size = self.by_kind.get(call.kind, (0, 0))
        self.by_kind[call.kind] = (calls + 1, size + call.request_bytes)

    def merge(self, other):
        self.calls += other.calls
        self.request_bytes += other.request_bytes
        for kind, (calls, size) in other.by_kind.items():
            mine = self.by_kind.get(kind, (0, 0))
            self.by_kind[kind] = (mine[0] + calls, mine[1] + size)


@dataclass(frozen=True)
class Snapshot:
    """The meter at one instant, in bytes and Decimal USD."""

    as_of: datetime
    month: str
    month_calls: int
    month_request_bytes: int
    month_by_kind: dict
    today_request_bytes: int
    window_request_bytes: int
    window_weighted_request_bytes: int
    remaining_days: Decimal
    month_to_date: Decimal
    today: Decimal
    projection: Decimal
    projection_per_minute: Decimal
    receipt_watermark: int

    def public(self, config):
        tokens = config.tokens(self.month_request_bytes)
        return {
            "month": self.month, "month_to_date_usd": usd(self.month_to_date),
            "today_usd": usd(self.today), "projection_usd": usd(self.projection),
            "projection_per_minute_usd": usd(self.projection_per_minute),
            "month_calls": self.month_calls, "month_request_bytes": self.month_request_bytes,
            "month_estimated_input_tokens": str(tokens.to_integral_value()),
            "month_by_kind_usd": {kind: usd(config.cost(size))
                                  for kind, (_, size) in sorted(self.month_by_kind.items())},
            "last_24h_request_bytes": self.window_request_bytes,
            "last_24h_per_minute_equivalent_bytes": self.window_weighted_request_bytes,
            "days_left_in_month": str(self.remaining_days.quantize(D("0.0001"))),
            "receipt_watermark": self.receipt_watermark,
        }


class SpendMeter:
    """Derived Jev spend (``JEV_SPEND_METER_V1``): read-only, rebuilt from the ledger at start,
    then incremental. Any role with SELECT on ``lab.jev_requests`` and ``lab.jev_receipts``
    (``catalyst_risk``, ``catalyst_app``, ``catalyst_jev``) can read it."""

    def __init__(self, repo, *, chunk=LOAD_CHUNK):
        self.repo, self.chunk = repo, chunk
        self.watermark = None
        self._window = []  # Calls of the trailing window (and a little older), any order.
        self._days = {}  # New York date -> _Totals.

    @staticmethod
    def _call(row):
        return Call(row["event_seq"], row["started_at"], int(row["request_bytes"]),
                    kind_of(row["question_set_version"]), _weight(row["routine_weight"]))

    def _add(self, call):
        self._window.append(call)
        day = call.started_at.astimezone(NY).date()
        self._days.setdefault(day, _Totals()).add(call)

    def _load(self, conn, since):
        """This month's and the last day's calls, newest first by receipt sequence, stopping one
        margin before ``since`` (receipts are appended when their attempt ends)."""
        before, newest = None, 0
        while True:
            rows = conn.execute(
                _ROWS + " WHERE (%(before)s::bigint IS NULL OR r.event_seq<%(before)s)"
                " ORDER BY r.event_seq DESC LIMIT %(limit)s",
                {"before": before, "limit": self.chunk},
            ).fetchall()
            for row in rows:
                newest = max(newest, row["event_seq"])
                if row["provider_call"] and row["started_at"] >= since:
                    self._add(self._call(row))
            if len(rows) < self.chunk or min(r["started_at"] for r in rows) < since - LOAD_MARGIN:
                return newest
            before = rows[-1]["event_seq"]

    def refresh(self, now):
        """Read every receipt appended since the last read (all of them the first time)."""
        now = now.astimezone(UTC)  # Instants in UTC: wall-clock arithmetic ignores DST.
        with self.repo.connect() as conn:
            if self.watermark is None:
                since = min(month_start(now), now - timedelta(seconds=WINDOW_SECONDS))
                self.watermark = self._load(conn, since)
            else:
                while True:
                    rows = conn.execute(
                        _ROWS + " WHERE r.event_seq>%(after)s ORDER BY r.event_seq"
                        " LIMIT %(limit)s",
                        {"after": self.watermark, "limit": self.chunk},
                    ).fetchall()
                    for row in rows:
                        if row["provider_call"]:
                            self._add(self._call(row))
                        self.watermark = max(self.watermark, row["event_seq"])
                    if len(rows) < self.chunk:
                        break
        self._prune(now)

    def _prune(self, now):
        oldest = min(month_start(now), now - timedelta(seconds=WINDOW_SECONDS))
        self._window = [c for c in self._window
                        if c.started_at > now - timedelta(seconds=WINDOW_SECONDS)]
        keep_from = (oldest - timedelta(days=1)).astimezone(NY).date()
        self._days = {day: totals for day, totals in self._days.items() if day >= keep_from}

    def snapshot(self, now, config):
        """Month-to-date, today, the last 24 hours and both month-end projections at ``now``."""
        now = now.astimezone(UTC)
        first = month_start(now).date()
        last = (next_month_start(now) - timedelta(days=1)).date()
        month = _Totals()
        for day, totals in self._days.items():
            if first <= day <= last:
                month.merge(totals)
        today = self._days.get(day_start(now).date(), _Totals())
        start = now - timedelta(seconds=WINDOW_SECONDS)
        window = [c for c in self._window if c.started_at > start]
        window_bytes = sum(c.request_bytes for c in window)
        weighted = sum(c.request_bytes * c.weight for c in window)
        with localcontext() as context:
            context.prec = 50
            left = next_month_start(now).astimezone(UTC) - now  # Real seconds, DST included.
            remaining = max(_seconds(left) / D(86400), D(0))
            mtd = config.cost(month.request_bytes)
            projection = mtd + config.cost(window_bytes) * remaining
            per_minute = mtd + config.cost(weighted) * remaining
        return Snapshot(now, month_label(now), month.calls, month.request_bytes,
                        dict(month.by_kind), today.request_bytes, window_bytes, weighted,
                        remaining, mtd, config.cost(today.request_bytes), projection, per_minute,
                        self.watermark or 0)


# --- The guard -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardDecision:
    """What the guard decided at ``evaluated_at``. ``review_bar_seconds`` is the routine cadence
    of guarded trades (None: no guarded review at all)."""

    tier: str
    rule: str | None
    evaluated_at: datetime
    snapshot: Snapshot | None = None
    tier_event_seq: int | None = None
    tier_since: datetime | None = None
    code: str | None = None

    @property
    def available(self):
        return self.tier in TIERS

    @property
    def review_bar_seconds(self):
        return REVIEW_BAR_SECONDS.get(self.tier)

    def facts(self):
        """What a guarded review request records about the guard."""
        return {"version": VERSION, "tier": self.tier, "tier_event_seq": self.tier_event_seq,
                "review_bar_seconds": self.review_bar_seconds}


def unavailable(now, code):
    return GuardDecision(UNAVAILABLE, None, now, code=code)


class SpendGuard:
    """``JEV_SPEND_GUARD_V1`` over a ``SpendMeter``, with its tier events in the managed ledger.

    ``store`` is the managed store (``catalyst_risk``): the meter reads the Jev tables through
    its repository and the tier and configuration events are appended to ``lab.managed_events``
    (no setup). Thread-safe: the maintenance pass and the status route share it."""

    def __init__(self, config, store, *, clock, meter=None, refresh_seconds=REFRESH_SECONDS,
                 stale_seconds=STALE_SECONDS):
        if not isinstance(config, SpendConfig):
            raise ValueError("JEV_SPEND_CONFIG_REQUIRED")
        self.config, self.store, self.now = config, store, clock
        self._meter = meter  # Built on first use: construction never touches the ledger.
        self.refresh_seconds, self.stale_seconds = refresh_seconds, stale_seconds
        self._lock = threading.RLock()
        self._last = None
        self._tier = None  # The last recorded tier event: (event_seq, tier, its time).
        self._configured = False

    @property
    def meter(self):
        if self._meter is None:
            self._meter = SpendMeter(self.store.repo)
        return self._meter

    # --- ledger ------------------------------------------------------------------------------

    @staticmethod
    def _latest(conn, kind):
        return conn.execute(
            """SELECT event_seq,body,recorded_at FROM lab.managed_events
            WHERE setup_id IS NULL AND kind=%s ORDER BY event_seq DESC LIMIT 1""", (kind,),
        ).fetchone()

    def _record_configuration(self, now):
        config = {"policy": policy_record(), "estimate": self.config.record()}
        with self.store.transaction() as conn:
            last = self._latest(conn, CONFIG_EVENT)
            if last is None or last["body"].get("configuration") != config:
                previous = last["event_seq"] if last else 0
                self.store.event(conn, CONFIG_EVENT, {
                    "version": VERSION, "configuration": config,
                    "previous_event_seq": previous, "at": now.isoformat(),
                }, key=f"jev-spend-guard-configured:{previous}:{digest(encoded(config))[:24]}")
        self._configured = True

    @staticmethod
    def _tier_of(row):
        """(event_seq, tier, when the guard decided it) of a recorded tier event."""
        return (row["event_seq"], row["body"]["tier"], datetime.fromisoformat(row["body"]["at"]))

    def _read_tier(self):
        with self.store.repo.connect() as conn:
            row = self._latest(conn, TIER_EVENT)
        self._tier = self._tier_of(row) if row else None

    def _record_tier(self, tier, rule, snapshot, now):
        """Append JEV_BUDGET_TIER_CHANGED unless the last recorded tier is already ``tier``."""
        with self.store.transaction() as conn:
            last = self._latest(conn, TIER_EVENT)
            if last is not None and last["body"].get("tier") == tier:
                self._tier = self._tier_of(last)
                return
            previous = last["event_seq"] if last else None
            body = {
                "version": VERSION, "tier": tier, "rule": rule,
                "previous_tier": last["body"].get("tier") if last else None,
                "previous_event_seq": previous, "at": now.isoformat(),
                "review_bar_seconds": REVIEW_BAR_SECONDS[tier],
                "budget_usd": str(self.config.monthly_budget_usd),
                "thresholds_usd": {k: usd(v) for k, v in
                                   thresholds(self.config.monthly_budget_usd).items()},
                "estimate": self.config.record(),
                **snapshot.public(self.config),
            }
            row = self.store.event(conn, TIER_EVENT, json_safe(body),
                                   key=f"jev-budget-tier:{previous or 0}:{tier}")
        self._tier = self._tier_of(row)

    # --- decisions ---------------------------------------------------------------------------

    def evaluate(self, now=None):
        """The current decision (re-evaluated at most every ``refresh_seconds``)."""
        now = (now or self.now()).astimezone(UTC)
        with self._lock:
            last = self._last
            if last is not None and last.available and (
                    0 <= (now - last.evaluated_at).total_seconds() < self.refresh_seconds):
                return last
            try:
                if not self._configured:
                    self._record_configuration(now)
                if self._tier is None:
                    self._read_tier()
                self.meter.refresh(now)
                snapshot = self.meter.snapshot(now, self.config)
                tier, rule = tier_for(
                    month_to_date=snapshot.month_to_date,
                    projection_per_minute=snapshot.projection_per_minute,
                    budget=self.config.monthly_budget_usd,
                    previous_tier=self._tier[1] if self._tier else None)
                if self._tier is None or self._tier[1] != tier:
                    self._record_tier(tier, rule, snapshot, now)
                decision = GuardDecision(tier, rule, now, snapshot, self._tier[0],
                                         self._tier[2])
            except Exception as exc:
                if last is not None and last.available and (
                        0 <= (now - last.evaluated_at).total_seconds() <= self.stale_seconds):
                    return last  # A short outage keeps the last decision.
                code = str(exc) if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", str(exc)) \
                    else type(exc).__name__
                decision = unavailable(now, code)
            self._last = decision
            return decision

    def status(self, now=None):
        """The ``jev_budget`` section of the runtime status: tier, money, projections."""
        now = (now or self.now()).astimezone(UTC)
        decision = self.evaluate(now)
        base = {"version": VERSION, "available": decision.available, "tier": decision.tier,
                "routine_review_seconds": decision.review_bar_seconds,
                "budget_usd": str(self.config.monthly_budget_usd),
                "estimate": self.config.record(),
                "evaluated_at": decision.evaluated_at.isoformat()}
        if not decision.available:
            return {**base, "code": decision.code}
        return {
            **base, "rule": decision.rule,
            "tier_since": _iso(decision.tier_since),
            "tier_event_seq": decision.tier_event_seq,
            "thresholds_usd": {k: usd(v) for k, v in
                               thresholds(self.config.monthly_budget_usd).items()},
            **decision.snapshot.public(self.config),
        }


def _seconds(delta):
    """A timedelta in exact Decimal seconds."""
    return D(delta.days * 86400 + delta.seconds) + D(delta.microseconds) / D(1_000_000)


def _iso(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def guard_summary(config):
    """The guard's version and configuration, for configuration hashes."""
    return {"policy": policy_record(), "estimate": config.record()} if config else None

