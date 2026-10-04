"""Opt-in app-owned paper worker; research latency cannot stop protection ticks.

The executable does not migrate databases, obtain research evidence, or assume a
Muse-hosted process. A supervisor must run it. Muse creates durable ResearchCycle
inputs and consumes durable outputs; all restarts reload those rows. Real-provider
acceptance and deployment supervision remain separate from unit-test proof.

Losing the account executor lease is terminal for the process (plan phase 0,
2026-09-26): the loss is recorded, every loop stops and the process exits with
EXECUTOR_OWNERSHIP_LOST_EXIT_CODE, so the supervisor restarts it through normal
startup (lease, reconciliation, protection). The lease is never re-acquired in-process.
"""

import asyncio
import json
import math
import os
import re
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from catalyst_lab import (
    ai_mode,
    coinbase_feed,
    coinbase_trigger,
    crypto_holding,
    crypto_maintenance,
    crypto_trigger,
    day_review,
    execution_setting,
    gap_resume,
    jev_budget,
    regime_gate,
    stale_print,
    stop_breach,
    strategies,
    strategy_paper,
    trade_plan,
)
from catalyst_lab.alpaca import (
    CRYPTO_STREAM_ENDPOINT,
    STREAM_ENDPOINTS,
    TRADE_STREAM_ENDPOINT,
    AlpacaCredentials,
    FixedStreamConnection,
)
from catalyst_lab.broker_budget import (
    ACCOUNT,
    PROTECTIVE,
    RECONCILIATION,
    RESEARCH,
    BudgetedBroker,
    request_priority,
)
from catalyst_lab.execution import SubmissionDisabled
from catalyst_lab.managed_execution import EXIT_REFUSAL_ALARM_THRESHOLD
from catalyst_lab.managed_latches import (
    ACCOUNT_SAFETY,
    AUDIT,
    AUDIT_CATEGORY,
    OPERATOR_CLEAR_EVENT,
    PERSISTENT,
    PROTECTION,
    PROTECTION_PHASES,
    REST,
    REST_CATEGORY,
    RUNTIME_SCOPE,
    TRIGGER_SCOPE,
    LatchBook,
    LatchPolicy,
    TickRecord,
    classify_failure,
    position_possibly_open,
)
from catalyst_lab.managed_store import POLICY
from catalyst_lab.market import NY
from catalyst_lab.position_monitor import (
    MANAGEMENT_REVIEWS_ENABLED,
    MANAGEMENT_REVIEWS_SETTINGS,
)
from catalyst_lab.public_crypto_bars import PublicCryptoBarReader
from catalyst_lab.repository import json_safe
from catalyst_lab.research_selection_topk import TOPK_POLICIES
from catalyst_lab.runtime import WIRE_LOG
from catalyst_lab.selection_facts_v3 import ComparisonFacts
from catalyst_lab.system_check import (
    ADMISSION_DECLINED_KEY,
    NOT_REPLACED,
    SUPERSEDED_BY_NEW_RESEARCH,
    SUPERSESSION_VERSION,
    SUPERSESSION_VERSION_V2,
    AdmissionRefused,
    LivePriceReader,
    LivePriceUnavailable,
    is_v3_packet,
    newer_v3_selections,
    newest_v3_run_slot,
    run_slot_of,
    superseding_run_slot,
    supersession_version,
)
from catalyst_lab.system_check import PERMANENT_REFUSALS as V3_PERMANENT_REFUSALS

# A lost executor lease ends the process with this status (sysexits EX_TEMPFAIL), so the
# supervisor (launchd KeepAlive SuccessfulExit=false, through the private launcher) starts a
# new process. After FATAL_EXIT_GRACE_SECONDS without a finished graceful shutdown the process
# exits anyway.
EXECUTOR_OWNERSHIP_LOST_EXIT_CODE = 75
FATAL_EXIT_GRACE_SECONDS = 30
EXECUTOR_OWNERSHIP_LOST_EVENT = "RUNTIME_EXECUTOR_OWNERSHIP_LOST"


@dataclass(frozen=True)
class RuntimePolicy:
    profile: str
    execution_tick_seconds: float
    market_poll_seconds: float
    research_poll_seconds: float
    heartbeat_seconds: float
    reconcile_seconds: float
    stream_open_timeout_seconds: float
    stream_read_timeout_seconds: float
    reconnect_seconds: float
    max_reconnect_seconds: float
    shutdown_timeout_seconds: float
    market_queue_capacity: int

    def __post_init__(self):
        values = [v for k, v in asdict(self).items() if k != "profile"]
        if (
            self.profile != POLICY
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or v <= 0
                for v in values
            )
            or self.execution_tick_seconds > 1
            or self.market_poll_seconds > 5
            or self.heartbeat_seconds > 5
            or self.research_poll_seconds > 5
            or not 30 <= self.reconcile_seconds <= 60
            or self.reconnect_seconds > self.max_reconnect_seconds
            or not isinstance(self.market_queue_capacity, int)
            or not 1 <= self.market_queue_capacity <= 10000
        ):
            raise ValueError("EXPLICIT_MANAGED_RUNTIME_POLICY_REQUIRED")


def engineering_runtime_policy():
    return RuntimePolicy(POLICY, 1, 1, 1, 5, 30, 8, 1, 1, 30, 5, 5000)


_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
# ADMISSION_DECLINED_KEY ("research:admission-declined:") is system_check's, shared with the
# research withdrawals (package research-loop-app).
# Refusals the same stored selection can never overcome: its packet, receipts and
# research rows are immutable and time only moves forward. Every other refusal (halts,
# pending exits, the crypto entry window, an actively managed symbol, database or
# unclassified errors) is retried each tick and audited once per runtime and reason.
PERMANENT_ADMISSION_REFUSALS = frozenset(
    {
        # ManagedExecution.admit
        "APP_REVIEWED_PACKET_REQUIRED",
        "DURABLE_SELECTION_BINDING_REQUIRED",
        "INVALID_OR_EXPIRED_SETUP",
        "NONFINITE_MARKET_NUMBER",
        "TICKER_ALREADY_ATTEMPTED",
        # JevStore.verify of the selection receipt
        "AUDIT_ENVELOPE_MISSING",
        "INPUT_HASH_MISMATCH",
        "JUDGMENT_SET_MISMATCH",
        "RECEIPT_AUDIT_MISMATCH",
        "RECEIPT_MISSING",
        "REQUEST_HASH_MISMATCH",
        "RESPONSE_HASH_MISMATCH",
        "TEMPLATE_HASH_MISMATCH",
        # lab.managed_review_failure, migrations 013 and 014
        "JEV_SELECTION_REQUIRED",
        "QUALITY_POLICY_REQUIRED",
        "QUALITY_RECEIPT_BINDING_FAILURE",
        "QUALITY_RECEIPT_REQUIRED",
        "QUALITY_SCORE_MISMATCH",
        "RECEIPT_BINDING_FAILURE",
        "RESEARCH_CONTENT_BINDING_FAILURE",
        "REVIEW_EXPIRED",
        "REVIEW_SUPERSEDED_OR_UNBOUND",
        "SELECTION_INTEGRITY_FAILURE",
        "SELECTION_QUESTION_POLICY_MISMATCH",
        # lab.managed_engineering_failure, migration 017 (operator ENGINEERING_TEST packets);
        # ENGINEERING_TEST_ALREADY_ACTIVE is transient: the other engineering setup may close.
        "ENGINEERING_ENROLLMENT_BINDING_FAILURE",
        "ENGINEERING_ENROLLMENT_EXPIRED",
        "ENGINEERING_LEVELS_INVALID",
        "MIN_REWARD_RISK",
        # lab.managed_review_failure, migration 018 (selection rule B1)
        "B1_COMPONENTS_REQUIRED",
        "QUALITY_FLOOR_NOT_MET",
        "SELECTION_RULE_NOT_ACTIVATED",
        # lab.managed_review_failure_b2, migration 020 (selection rule B2)
        "B2_COMPONENTS_REQUIRED",
        "RATIONALE_REQUIRED",
        # lab.managed_review_failure_topk, migration 021 (selection rule JEV_TOP_K_SELECTION_V1),
        # and lab.managed_review_failure_topk_v2, migration 023 (JEV_TOP_K_SELECTION_V2)
        "TOPK_RANKING_BINDING_FAILURE",
        "TOPK_SCORE_MISMATCH",
        "TOPK_VETOED",
        # lab.managed_review_failure_topk_v3, migration 030 (JEV_TOP_K_SELECTION_V3)
        "TOPK_BELOW_THRESHOLD",
        "TOPK_COMPARISON_BINDING_FAILURE",
    }
)

# Package system-check (plan phase 3a, 2026-09-27; system_check.py): the report-V3 system
# check's refusals (STOP_DISTANCE_BELOW_MINIMUM, PRICE_MISMATCH, STOP_ALREADY_HIT,
# BREAKOUT_NOT_ENABLED) and SUPERSEDED_BY_NEW_RESEARCH. LIVE_PRICE_UNAVAILABLE stays transient.
# Package research-loop-app: WITHDRAWN_BY_RESEARCH, a selection its agent withdrew while it
# was being admitted (already declined by the withdrawal, so declining again writes nothing).
PERMANENT_ADMISSION_REFUSALS = PERMANENT_ADMISSION_REFUSALS | V3_PERMANENT_REFUSALS

# Package replacement (plan phase 3b, 2026-09-27). The broker price-grid refusals are final for
# their receipt (one CRYPTO_ADMISSION_REFUSED, re-raised without another broker read), yet every
# rule before top-K keeps retrying them each tick, unchanged. A top-K selection refused with one
# is declined like any permanent refusal, so TOPK_REPLACEMENT_V1 replaces it.
TOPK_PERMANENT_ADMISSION_REFUSALS = PERMANENT_ADMISSION_REFUSALS | {
    "CRYPTO_LEVEL_OFF_PRICE_GRID",
    "CRYPTO_PRECISION_UNAVAILABLE",
}
# Package crypto-size-hold (plan phase 4b, 2026-09-27; plan section 5, "same coin picked again
# while its trade is open: new pick skipped"). A top-K pick for a coin that already has an
# active managed setup is declined at once, and so replaced by Jev's next-ranked pick, instead
# of holding its place until the trade closes. V2, B1 and B2 selections keep retrying it.
TOPK_PERMANENT_ADMISSION_REFUSALS = TOPK_PERMANENT_ADMISSION_REFUSALS | {
    "ACTIVE_SYMBOL_ALREADY_MANAGED",
}
REPLACEMENT_FAULT_EVENT = "RUNTIME_REPLACEMENT_FAULT"
# STRATEGY_PAPER_PATH_V1 (package plugin-c3): how often the signal pass looks for a new hour.
STRATEGY_SIGNAL_SECONDS = 60


def permanent_refusal(packet, reason):
    """Whether a selection refused with ``reason`` is declined (never offered again)."""
    if strategies.is_signal_packet(packet):
        # STRATEGY_PAPER_PATH_V1 (package plugin-c3): a marketable signal has no later entry, so
        # every final code of a top-K pick, and the strategy path's own, declines it.
        return reason in TOPK_PERMANENT_ADMISSION_REFUSALS | strategy_paper.PERMANENT_REFUSALS
    if packet.get("selection_policy") in TOPK_POLICIES:
        return reason in TOPK_PERMANENT_ADMISSION_REFUSALS
    return reason in PERMANENT_ADMISSION_REFUSALS


def replaces_on_decline(packet, reason, research):
    """TOPK_REPLACEMENT_V1 (V2 for a cycle accepted under RESEARCH_SCHEDULE_V2) applies: a
    top-K selection declined for anything but supersession (the run is over) or its agent's
    withdrawal, with a research cycle that can decide its replacement."""
    return (
        packet.get("selection_policy") in TOPK_POLICIES
        and reason not in NOT_REPLACED
        and callable(getattr(research, "replace_declined", None))
    )


def decline_selection(store, research, body, key):
    """The selection's one RESEARCH_ADMISSION_DECLINED (``body`` under ``key``) and its one
    TOPK_REPLACEMENT_V1 decision, committed together in one ledger transaction under the
    shared lock that also serializes every publication (package replacement).

    A decline already recorded under ``key`` is kept as recorded and its reason decides, so a
    repeated call or a restart never adds a second decline or replacement. Returns ``(decline
    row, RESEARCH_REPLACEMENT row or None)``. A refused decision (``ValueError``) or a database
    error rolls both back and propagates: nothing is written and the selection stays offered.
    """
    with store.transaction() as conn:
        decline = conn.execute(
            "SELECT * FROM lab.managed_events WHERE idempotency_key=%s", (key,)
        ).fetchone()
        if decline is None:
            decline = store.event(conn, "RESEARCH_ADMISSION_DECLINED", body, key=key)
        return decline, research.replace_declined(conn, decline)


def research_v3(packet):
    """A report-V3 research pick: the packets run supersession reads. A promoted strategy's
    signal (STRATEGY_PAPER_PATH_V1) carries the report-V3 packet shape but answers no research
    run, so it neither supersedes nor is superseded."""
    return is_v3_packet(packet) and not strategies.is_signal_packet(packet)


# The streams' session clock (a module name, so a test can stand in for it).
_monotonic = time.monotonic


def reconnect_wait(previous, session_seconds, policy):
    """The wait before the next stream connection: the policy's first wait
    (``reconnect_seconds``) after a session that stayed up at least ``max_reconnect_seconds``,
    so only drops in a row keep doubling it; else ``previous``. (Until 2026-09-29 it never
    reset, so a process that had seen five drops waited the 30-second cap after every drop.)"""
    if session_seconds >= policy.max_reconnect_seconds:
        return policy.reconnect_seconds
    return previous


def failure_code(exc):
    """The exception's own UPPER_SNAKE code, else its class name; free text is never kept."""
    text = str(exc)
    return text if len(text) <= 80 and _FAILURE_CODE.fullmatch(text) else type(exc).__name__


def close_codes(exc):
    """``WS_CLOSED_<received>_<sent>`` for a closed WebSocket (websockets' ``ConnectionClosed``
    keeps both close frames, each with its code; NONE when absent), else None: a keepalive
    timeout of ours reads ``WS_CLOSED_NONE_1011``, a close by the provider carries its code."""
    if not hasattr(exc, "rcvd") or not hasattr(exc, "sent"):
        return None
    codes = []
    for frame in (exc.rcvd, exc.sent):
        code = getattr(frame, "code", None)
        codes.append(str(code) if type(code) is int and 0 <= code < 10000 else "NONE")
    return "WS_CLOSED_" + "_".join(codes)


def _run_slot_or_none(record):
    """A V3 record's aware ``run_slot``, or None when it has none (admission refuses such a
    packet for good, APP_REVIEWED_PACKET_REQUIRED)."""
    try:
        slot = run_slot_of(record)
    except (KeyError, TypeError, ValueError):
        return None
    return slot if slot.tzinfo is not None else None


# The gap every startup records for both markets; for CRYPTO_GAP_RESUME_V1 its window starts at
# the previous runtime's last recorded observation of the market (package gap-resume).
RESTART_GAP_REASON = "RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION"
# Alpaca stamps each stream message on its own servers, and this container's clock can run a
# little behind them: a message stamped up to this long after our clock is still current. Beyond
# it the message is refused as before (INVALID_MARKET_TIMESTAMP ends the session). 2026-09-29:
# with no tolerance the crypto stream was dropped 18 times in 22 minutes from 17:06 UTC and no
# entry could confirm. The Coinbase reference feed allows the same (CLOCK_TOLERANCE_SECONDS).
MARKET_CLOCK_TOLERANCE_SECONDS = 3
GAP_STATUS_LIMIT = 50  # Held setups listed in the status (the count is always complete).
# CRYPTO_STREAM_CAPACITY_V1 (2026-09-29). Alpaca serves at most 30 trade and quote channels on
# one market-stream connection, and every coin takes both, so the crypto stream carries at most
# 15 coins. A subscribe beyond that is answered with an error message (405, symbol limit
# exceeded), and any error message ends the session: at 23:13 UTC one request widening the
# subscription from 13 to 19 coins stopped the data of all 13, every 30 s. The stream plan
# (``ManagedRuntime._stream_plan``) keeps every active setup's coin, and holds back the offered
# picks that do not fit until a slot frees up.
CRYPTO_STREAM_CAPACITY_VERSION = "CRYPTO_STREAM_CAPACITY_V1"
# Frames the market stream's connection buffers (2026-09-30). With 64, a reader that fell behind
# stopped reading the socket, the keepalive's pong went unread and the connection closed about
# 40 s later: the crypto stream dropped every 45-100 s from 12:30 UTC, when quote traffic rose,
# while the Coinbase feed (MAX_QUEUE 1024, for the same reason) stayed up.
MARKET_STREAM_MAX_QUEUE = 1024
CRYPTO_STREAM_SYMBOL_CAPACITY = 15


@dataclass
class GapCheck:
    """One setup of CRYPTO_GAP_RESUME_V1 held after a market gap until its one bar check.

    ``bound`` is the gap's start (None: unknown, so the window starts at the admission);
    ``body`` its GAP_RESUME_PENDING once computed; ``stream_back_at`` the first protection tick
    at which the setup's market stream was back for its symbol, in gap ``generation``.
    """

    setup_id: str
    market: str
    symbol: str
    pending_since: datetime
    bound: datetime | None
    reason: str
    basis: dict
    body: dict | None = None
    recorded: bool = False
    stream_back_at: datetime | None = None
    generation: int | None = None

    @property
    def window_start(self):
        return datetime.fromisoformat(self.body["window_start"]) if self.body else None

    def public(self):
        due = gap_resume.due_at(self.stream_back_at) if self.stream_back_at else None
        return {
            "setup_id": self.setup_id, "market": self.market, "symbol": self.symbol,
            "gap_reason": self.reason, "pending_since": self.pending_since.isoformat(),
            "window_start": self.body["window_start"] if self.body else None,
            "recorded": self.recorded,
            "stream_back_at": self.stream_back_at.isoformat() if self.stream_back_at else None,
            "due_at": due.isoformat() if due else None,
        }


class MarketStreamProviderError(ValueError):
    """An ``error`` message from the market stream's provider. ``str()`` stays
    MARKET_STREAM_PROVIDER_ERROR (the gap's reason). ``code`` keeps only the provider's numeric
    code, as ``ALPACA_STREAM_<n>`` like ``runtime.MarketRuntime``, never its message text: the
    number is what tells a symbol limit (405) from a connection limit (406) or a refused
    subscription (410)."""

    def __init__(self, frame):
        super().__init__("MARKET_STREAM_PROVIDER_ERROR")
        number = frame.get("code")
        self.code = (f"ALPACA_STREAM_{number}" if type(number) is int and 0 <= number < 10000
                     else "ALPACA_STREAM_ERROR")


class ManagedRuntime:
    def __init__(
        self,
        execution,
        research_cycle,
        market_source,
        credentials,
        policy,
        *,
        clock,
        reviewer_heartbeat,
        monitor_tick=None,
        executor_lease=None,
        account_safety=None,
        connector=FixedStreamConnection,
        broker_budget=None,
        latch_policy=None,
        management_reviews=MANAGEMENT_REVIEWS_ENABLED,
        gate1=None,
        maintenance=None,
        day_reviews=None,
        ledger_marker=None,
        spend_guard=None,
    ):
        self.execution, self.research = execution, research_cycle
        # The Mac app's LEDGER.json (CATALYST_LEDGER_MARKER from the private launcher; never
        # set in the cloud): re-read once the lease is held (package cloud-hardening).
        self.ledger_marker = Path(ledger_marker) if ledger_marker else None
        self.source, self.credentials, self.policy = market_source, credentials, policy
        self.now, self.reviewer_heartbeat = clock, reviewer_heartbeat
        self.monitor_tick, self.connector = monitor_tick, connector
        # The worker's own Gate1Runtime, wired in explicitly (package jev-breaker): lets the
        # managed research loop run the same synthetic health probe ReviewWorker.tick runs,
        # so a tripped breaker can recover even though no standalone worker process is
        # polling this scope. None (most engineering fixtures) means this runtime never
        # probes; the standalone review_worker remains the only recovery path for it.
        self.gate1 = gate1
        # The status must never claim reviews the monitor withholds: both carry one setting.
        if management_reviews not in MANAGEMENT_REVIEWS_SETTINGS:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_REQUIRED")
        monitor = getattr(monitor_tick, "monitor", None)
        if getattr(monitor, "management_reviews", management_reviews) != management_reviews:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_MISMATCH")
        self.management_reviews = management_reviews
        # CRYPTO_MAINTENANCE_V1 (package maintenance): the monitoring Jev for maintained trades
        # (``trade_maintenance.TradeMaintenance``); the same staging switch governs it.
        if getattr(maintenance, "management_reviews", management_reviews) != management_reviews:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_MISMATCH")
        self.maintenance = maintenance
        # CRYPTO_24H_REVIEW_V1 and EARLY_EXIT_AGREEMENT_V1 (package day-review): the 24-hour
        # reviews and early exits (``trade_review.DayReviews``); the same staging switch.
        if getattr(day_reviews, "management_reviews", management_reviews) != management_reviews:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_MISMATCH")
        self.day_reviews = day_reviews
        # JEV_SPEND_GUARD_V1 (package jev-budget): the monthly Jev budget's meter and tier, for
        # the status (``jev_budget``); the maintenance component asks the same guard.
        self.spend_guard = spend_guard
        # STRATEGY_PAPER_PATH_V1 (package plugin-c3): the promoted strategies' hourly signal pass
        # (``strategy_paper.StrategySignalSource``); None unless MANAGED_STRATEGIES_JSON lists
        # one. Set by the factory after construction, before ``start``.
        self.strategy_source = None
        # Bitcoin's last 15 minutes from the authenticated crypto stream (the 3% shock trigger),
        # subscribed while a maintained setup is active.
        self.benchmark = crypto_maintenance.BenchmarkWindow()
        self.executor_lease, self.account_safety = executor_lease, account_safety
        self.runtime_id = str(uuid4())
        if hasattr(account_safety, "runtime_id"):
            account_safety.runtime_id = self.runtime_id  # Operator flatten completions name it.
        # Set once a terminal failure (a lost executor lease) ends this runtime; ``on_fatal``
        # is the entry point's process-exit hook (``install_fatal_exit``), None in tests.
        self.exit_code = None
        self.on_fatal = None
        self.stop_event = threading.Event()
        # Set when a print is queued, so the protection pass evaluates it at once (pass-speed).
        self.print_queued = threading.Event()
        self.lock = threading.RLock()
        self.reconcile_lock = threading.Lock()
        self.owned_resources = ()
        self.connected = False
        self.reconciled_at = None
        # When the last reconciliation pass completed clean (package learning-app): what the
        # status reports as ``last_reconciliation_at``. ``reconciled_at`` above is cleared at the
        # start of every pass and on every gap or failure, so a status read inside a pass (about
        # 0.3 s every 30 s) showed null and the watchdog raised a false RECONCILIATION_STALE.
        # Only ``reconciled_at`` gates entries (``ready``); this one never does.
        self.last_clean_reconciliation_at = None
        self.research_healthy = False
        # Per-cause, per-setup latches replace the former single sticky error field.
        broker = getattr(execution, "broker", None)
        self.broker_budget = broker_budget if broker_budget is not None else (
            broker.governor if isinstance(broker, BudgetedBroker) else None
        )
        self.latch_policy = latch_policy if latch_policy is not None else LatchPolicy()
        self.latches = LatchBook(self.latch_policy, self.runtime_id)
        self._latch_maintenance = threading.Lock()
        self._open_scopes = set()
        self.account_safety_healthy = account_safety is None
        self.socket = None
        self.observations = {}
        self.threads = []
        # The number of worker loops start() launched; status() compares with it, so adding a
        # loop can never again make a healthy runtime report its workers stopped (package
        # fees-net-r added the eighth loop while status() still expected seven).
        self.expected_workers = None
        self.last_market_poll = None
        self.last_protection_tick = None
        self.last_research_tick = None
        self.market_connected = {"US": False, "CRYPTO": False}
        self.market_subscriptions = {"US": set(), "CRYPTO": set()}
        self.market_sockets = {}
        self.market_gaps = set()
        self.stock_feed = market_source.policy.stock_feed
        if self.stock_feed not in STREAM_ENDPOINTS:
            raise ValueError("EXPLICIT_STOCK_FEED_REQUIRED")
        # Report-V3 system check (package system-check): the stream observation, else one
        # bounded REST latest-quote read through this runtime's market source.
        self.live_prices = LivePriceReader(market_source, clock=clock)
        # CRYPTO_TRADE_PLAN_V1 (package trade-plan): the coin's hourly range for admission, one
        # bounded GET of 1-hour bars spending this tick's market-data read budget.
        self.hourly_ranges = trade_plan.HourlyRangeReader(
            market_source, clock=clock, spend=self.live_prices.spend_read)
        # CRYPTO_ALPACA_TRIGGER_V1 (package crypto-trigger): when each stream quote was received
        # (freshness by read time; kept apart from the observation rows, which MARKET_PRINT
        # records, so every other setup's records are unchanged) and the waits this runtime
        # already recorded, so a lasting wait costs no ledger access each tick.
        self.quote_received = {}
        self._trigger_waits = {}
        # CRYPTO_GAP_RESUME_V1 (package gap-resume): setups held for their bar check, by setup
        # id; each market's unprocessed gap (start, reason, basis) and unobserved period (its
        # start, None when unknown), and a count of its gaps, so a check read across a new gap
        # is discarded.
        self.gap_checks = {}
        self._gap_bounds = {}
        self._unobserved = {}
        self._gap_generation = {"US": 0, "CRYPTO": 0}
        # RESEARCH_RUN_SUPERSESSION_V2 (package research-loop-app): the V3 selections this
        # tick's supersession pass read, the only V3 selections admission may take this tick;
        # None under V1, which never defers one.
        self._supersession_checked = None
        # CRYPTO_COINBASE_TRIGGER_V1 / CRYPTO_STOP_BREACH_V3: the Coinbase products the reference
        # feed serves, as (monotonic time, products) of their last read (at most one a second),
        # and since when one of them has not been healthy (the watchdog's
        # REFERENCE_FEED_UNHEALTHY).
        self._reference_wanted = None
        self._reference_unhealthy_since = None
        # CRYPTO_STREAM_CAPACITY_V1: the offered selections whose capacity wait this runtime
        # recorded, so a lasting wait costs no ledger write each tick.
        self._capacity_waits = set()

    @property
    def reference_feed(self):
        """Coinbase's public feed (``coinbase_feed.CoinbaseFeed``), the one the execution reads
        for its admissions and stop breaches; None without it."""
        return getattr(self.execution, "reference_feed", None)

    @property
    def error(self):
        """The most severe active latch cause; ``None`` when nothing is latched."""
        return self.latches.error()

    def management_reviews_active(self):
        """Whether management reviews can reach Jev: a monitor is wired and the staging
        switch is ENABLED. Never true while MANAGED_MANAGEMENT_REVIEWS is DISABLED."""
        return self.monitor_tick is not None and (
            self.management_reviews == MANAGEMENT_REVIEWS_ENABLED
        )

    def _write_event(self, kind, body, *, key=None, setup_id=None):
        options = {"key": key} if key is not None else {}
        if setup_id is not None:
            options["setup_id"] = setup_id
        return self.execution._event(kind, body, **options)

    def _audit_unavailable(self, exc):
        with self.lock:
            self.reconciled_at = None
            self.execution.reconciled_at = None
        self.latches.record(
            AUDIT, RUNTIME_SCOPE, AUDIT_CATEGORY, failure_code(exc), type(exc).__name__,
            "AUDIT", self.now(),
        )

    def _event(self, kind, body, *, key=None, setup_id=None):
        try:
            self._write_event(
                kind, {"runtime_id": self.runtime_id, **body}, key=key, setup_id=setup_id
            )
        except Exception as exc:
            self._audit_unavailable(exc)
            return False
        self.latches.note_audit_success()
        return True

    def ready(self):
        with self.lock:
            return (
                self.connected
                and (self.executor_lease is None or (
                    self.executor_lease.connection is not None and not self.executor_lease.lost
                ))
                and self.account_safety_healthy
                and any(self.market_connected.values())
                and not self.market_gaps
                and self.research_healthy
                and self.reconciled_at is not None
                and self.execution.reconciled_at is not None
                and 0
                <= (self.now() - self.reconciled_at).total_seconds()
                <= self.policy.reconcile_seconds
                and not self.latches.blocking()
            )

    def _jev_health(self):
        """Breaker state and today's call volume for ``status()``. Read-only visibility:
        the owner set no daily cap (docs/CONTRACT-RESOLUTIONS.md, 2026-09-26). ``None`` when
        no ``gate1`` is configured, or when either read fails, so a database hiccup on this
        secondary read degrades the two fields instead of the whole status payload.
        """
        if self.gate1 is None:
            return None, None
        try:
            raw = self.gate1.state()
            breaker = {k: raw[k] for k in ("state", "epoch", "blocked_until")}
            # The live review policy whose runtime scope owns this breaker (V1, or V2 from
            # migration 025): how the owner sees that a JEV_REVIEW_POLICY_JSON switch took hold.
            inputs = getattr(self.gate1, "inputs", None)
            if isinstance(inputs, dict) and isinstance(inputs.get("version"), str):
                breaker["policy_version"] = inputs["version"]
        except Exception:
            breaker = None
        try:
            calls = self.gate1.calls_today(self.now())
        except Exception:
            calls = None
        return breaker, calls

    def _execution_halt_status(self):
        """Every halt refusing entries: unreleased ``lab.execution_halts`` rows and today's
        New York ``lab.daily_risk_halts`` row (``DAILY_RISK_HALT``). ``ready()`` does not read
        them (entry SQL and the authorization gate refuse on them), so the status must show
        them. An unreadable ledger reports ``available: false``; the watchdog fails closed."""
        repo = getattr(self.execution, "repo", None)
        if repo is None:
            return None  # A test double without a ledger.
        try:
            with repo.connect() as conn:
                rows = conn.execute(
                    "SELECT event_seq,reason FROM lab.execution_halts ORDER BY event_seq"
                ).fetchall()
                daily = conn.execute(
                    "SELECT event_seq FROM lab.daily_risk_halts WHERE session_date=%s",
                    (self.now().astimezone(NY).date(),),
                ).fetchone()
        except Exception:
            return {"available": False, "count": None, "kinds": []}
        kinds = {row["reason"] for row in rows} | ({"DAILY_RISK_HALT"} if daily else set())
        return {
            "available": True,
            "count": len(rows) + (1 if daily else 0),
            "kinds": sorted(kinds),
            "oldest_halt_seq": rows[0]["event_seq"] if rows else None,
        }

    @staticmethod
    def _exit_refusal_alarms(active):
        """Working setups whose close the broker refused EXIT_REFUSAL_ALARM_THRESHOLD or more
        times in a row; an accepted close or the setup closing clears one. Each keeps
        retrying under the refused-close backoff (``ManagedExecution._exit_refused``)."""
        alarms = []
        for setup in active:
            state = setup.get("state") or {}
            refusals = state.get("exit_refusals")
            if type(refusals) is int and refusals >= EXIT_REFUSAL_ALARM_THRESHOLD:
                alarms.append({
                    "setup_id": str(setup["setup_id"]),
                    "symbol": setup["symbol"],
                    "market": setup["market"],
                    "refusals": refusals,
                    "exit_requested": state.get("exit_requested"),
                    "retry_after": state.get("exit_retry_after"),
                })
        return alarms

    def status(self):
        from catalyst_lab.config import SCHEMA_VERSION
        from catalyst_lab.jev_contract import digest, encoded
        active = self.execution.store.active()
        required_markets = sorted({"US" if s["market"] == "US_STOCKS" else s["market"]
                                   for s in active})
        exit_refusal_alarms = self._exit_refusal_alarms(active)
        execution_halts = self._execution_halt_status()
        flatten_status = getattr(self.account_safety, "flatten_status", None)
        jev_breaker, jev_calls_today = self._jev_health()
        trade_maintenance = self._maintenance_status(active)
        day_reviews = self._day_review_status(active)
        jev_budget_status = self._jev_budget_status()
        reference_feed = self._reference_status()
        crypto_stream = self._crypto_stream_status()
        with self.lock:
            return {
                "runtime_id": self.runtime_id,
                "schema_version": SCHEMA_VERSION,
                "code_version": getattr(self, "code_version", "PAPER_WIRING_20260920_V1"),
                "release_commit": getattr(self, "release_commit", None),
                "configuration_hash": getattr(self, "configuration_hash",
                                              digest(encoded(asdict(self.policy)))),
                "required_market_streams": required_markets,
                "profile": self.policy.profile,
                "paper_only": True,
                "entry_ready": self.ready(),
                "account_safety_healthy": self.account_safety_healthy,
                # Pending operator flatten requests, read by the last account-safety tick.
                "operator_flatten": flatten_status() if callable(flatten_status) else None,
                # Unreleased halts refusing entries, and closes the broker keeps refusing.
                "execution_halts": execution_halts,
                "exit_refusal_alarms": exit_refusal_alarms,
                # CRYPTO_MAINTENANCE_V1: maintained trades, pending exit flags, failing reviews.
                "trade_maintenance": trade_maintenance,
                # CRYPTO_24H_REVIEW_V1: 24-hour reviews in progress, failing ones, flags by side.
                "day_reviews": day_reviews,
                # JEV_SPEND_GUARD_V1: the month's Jev spend, projections, budget and tier.
                "jev_budget": jev_budget_status,
                # CRYPTO_GAP_RESUME_V1: setups held for their gap check, and each market's
                # observation as of one instant (the next runtime's restart window).
                "gap_resume": self._gap_resume_status(),
                # CRYPTO_COINBASE_TRIGGER_V1 / CRYPTO_STOP_BREACH_V3: Coinbase's public feed.
                "reference_feed": reference_feed,
                # CRYPTO_STREAM_CAPACITY_V1: the coins the crypto stream wants, its capacity and
                # the offered picks held back until a slot frees up.
                "crypto_stream": crypto_stream,
                # STRATEGY_PAPER_PATH_V1 (package plugin-c3), only when a strategy is configured.
                **({"strategy_paper": {
                    "version": strategy_paper.PATH_VERSION,
                    "strategies": list(self.strategy_source.strategy_ids),
                    "last_pass": dict(self.strategy_source.last)}}
                   if self.strategy_source is not None else {}),
                "trade_updates_connected": self.connected,
                "research_healthy": self.research_healthy,
                "jev_breaker": jev_breaker,
                "jev_calls_today": jev_calls_today,
                "reconciled_at": self.reconciled_at.isoformat()
                if self.reconciled_at is not None else None,
                "last_clean_reconciliation_at": self.last_clean_reconciliation_at.isoformat()
                if self.last_clean_reconciliation_at is not None else None,
                "position_jev_configured": self.management_reviews_active(),
                "management_reviews": self.management_reviews,
                "market_streams": dict(self.market_connected),
                "market_data_authority": "ALPACA_STREAM",
                "last_market_poll": self.last_market_poll,
                "last_protection_tick": self.last_protection_tick,
                "last_research_tick": self.last_research_tick,
                "error": self.error,
                "latches": self.latches.public(),
                "protection_critical": self.latches.critical(),
                "broker_budget": self.broker_budget.status()
                if self.broker_budget is not None else None,
                "workers_alive": bool(self.threads) and len(self.threads) == self.expected_workers
                and all(t.is_alive() for t in self.threads),
                "operation_claim": "SUPERVISED_PAPER_TEST",
                "executor_ownership": "EXCLUSIVE" if self.executor_lease is not None
                and self.executor_lease.connection is not None and not self.executor_lease.lost
                else "UNHELD",
            }

    def _gap_resume_status(self):
        """CRYPTO_GAP_RESUME_V1's status, taken under the runtime lock (the caller holds it):
        the held setups (oldest first; the watchdog raises GAP_RESUME_CHECK_OVERDUE when the
        oldest has waited too long) and, as of that instant, whether each market is observed
        (connected, subscriptions acknowledged, no unprocessed gap) or since when it is not
        (``unobserved_since``; null when unknown). RUNTIME_HEARTBEAT records it, so a restart
        bounds its window by the last observation the previous runtime recorded."""
        held = sorted(self.gap_checks.values(), key=lambda c: (c.pending_since, c.setup_id))
        markets = {}
        for market in ("US", "CRYPTO"):
            since = self._unobserved.get(market)
            markets[market] = {
                "observed": market not in self._unobserved
                and self.market_connected.get(market, False) and market not in self.market_gaps,
                "unobserved_since": since.isoformat() if since is not None else None,
            }
        return {
            "version": gap_resume.GAP_RESUME_VERSION,
            "as_of": self.now().isoformat(),
            "markets": markets,
            "pending_count": len(held),
            "oldest_pending_since": held[0].pending_since.isoformat() if held else None,
            "overdue_after_seconds": gap_resume.CHECK_OVERDUE_SECONDS,
            "pending": [check.public() for check in held[:GAP_STATUS_LIMIT]],
        }

    def _reference_status(self):
        """The Coinbase reference feed's status (``None`` without the feed; ``available: false``
        when it cannot be read), with the products the setups and offered picks need
        (``wanted``, as the feed loop last read them) and since when one of them has not been
        healthy (``unhealthy_since``; the watchdog raises REFERENCE_FEED_UNHEALTHY after 300 s)."""
        feed = self.reference_feed
        if feed is None:
            return None
        now = self.now()
        try:
            status = feed.status(now)
        except Exception:
            return {"provider": coinbase_feed.PROVIDER, "available": False}
        wanted = sorted(self._reference_wanted[1]) if self._reference_wanted else []
        failing = set(wanted) - set(status.get("healthy") or ())
        with self.lock:
            if not failing:
                self._reference_unhealthy_since = None
            elif self._reference_unhealthy_since is None:
                self._reference_unhealthy_since = now
            since = self._reference_unhealthy_since
        return {**status, "available": True, "wanted": wanted,
                "unhealthy_since": since.isoformat() if since is not None else None}

    def _crypto_stream_status(self):
        """CRYPTO_STREAM_CAPACITY_V1's status: ``wanted``, the number of coins the crypto
        stream's plan subscribes, against ``capacity``, and ``held_back``, the offered picks'
        coins that wait for a slot (selection order). ``available: false`` when the plan cannot
        be read."""
        section = {"version": CRYPTO_STREAM_CAPACITY_VERSION,
                   "capacity": CRYPTO_STREAM_SYMBOL_CAPACITY}
        try:
            wanted, held_back = self._stream_plan("CRYPTO")
        except Exception:
            return {**section, "available": False}
        return {**section, "wanted": len(wanted), "held_back": held_back}

    def _maintenance_status(self, active):
        """The maintenance component's status (``None`` without one; ``available: false`` when
        its read fails, which the watchdog then reports)."""
        if self.maintenance is None:
            return None
        try:
            return self.maintenance.status(active)
        except Exception:
            return {"available": False}

    def _jev_budget_status(self):
        """JEV_SPEND_GUARD_V1 (package jev-budget): month-to-date spend, today, the projections,
        the budget and the tier (``None`` without a guard; ``available: false`` when it cannot
        be read, which the watchdog reports)."""
        if self.spend_guard is None:
            return None
        try:
            return self.spend_guard.status(self.now())
        except Exception:
            return {"version": jev_budget.VERSION, "available": False,
                    "tier": jev_budget.UNAVAILABLE}

    def _day_review_status(self, active):
        """The day-review component's status (``None`` without one; ``available: false`` when
        its read fails, which the watchdog then reports)."""
        if self.day_reviews is None:
            return None
        try:
            return self.day_reviews.status(active)
        except Exception:
            return {"available": False}

    def _cycle_ids(self):
        with self.execution.repo.connect() as conn:
            return [
                row["cycle_id"]
                for row in conn.execute(
                    """SELECT DISTINCT body->>'cycle_id' AS cycle_id
                FROM lab.managed_events WHERE kind='RESEARCH_STARTED'
                AND (body->>'expires_at')::timestamptz>clock_timestamp()
                ORDER BY cycle_id""",
                ).fetchall()
            ]

    def _selected_packets(self):
        # Declined selections are excluded before the limit so they cannot crowd out
        # admittable ones or keep driving market-data subscriptions.
        with self.execution.repo.connect() as conn:
            rows = conn.execute(
                """SELECT e.event_seq,e.body->'packet' AS packet
                FROM lab.managed_events e WHERE e.kind='RESEARCH_SELECTED'
                AND (e.body->'packet'->>'expires_at')::timestamptz>clock_timestamp()
                AND NOT EXISTS(SELECT 1 FROM lab.managed_setups s
                    WHERE s.receipt_id::text=e.body->'packet'->>'receipt_id')
                AND NOT EXISTS(SELECT 1 FROM lab.managed_setups s WHERE s.receipt_id IS NULL
                    AND s.record_json->>'enrollment_event_seq'
                        =e.body->'packet'->>'enrollment_event_seq')
                AND NOT EXISTS(SELECT 1 FROM lab.managed_setups s WHERE s.receipt_id IS NULL
                    AND s.record_json->>'signal_event_seq'
                        =e.body->'packet'->>'signal_event_seq')
                AND NOT EXISTS(SELECT 1 FROM lab.managed_events d
                    WHERE d.idempotency_key=%s||e.event_seq::text)
                ORDER BY e.event_seq LIMIT 30""",
                (ADMISSION_DECLINED_KEY,),
            ).fetchall()
        return [{**r["packet"], "selection_event_seq": r["event_seq"]} for r in rows]

    def _audit_once(self, kind, body, key, *, setup_id=None):
        """Deterministic-key notice; a failed write latches like any runtime audit event."""
        try:
            self.execution._event(kind, body, setup_id=setup_id, key=key)
        except Exception as exc:
            # An earlier notice (possibly an older body shape) already holds this key.
            if not (isinstance(exc, ValueError) and str(exc) == "IDEMPOTENCY_CONTENT_MISMATCH"):
                self._audit_unavailable(exc)
                return False
        else:
            self.latches.note_audit_success()
        return True

    def _stream_capacity_wait(self, packet):
        """CRYPTO_STREAM_CAPACITY_V1: a pick the crypto stream holds back is not admitted; one
        CRYPTO_STREAM_CAPACITY_WAIT per selection records it. It is admitted as usual once a
        slot frees up, and expires as before otherwise."""
        seq = packet.get("selection_event_seq")
        if seq in self._capacity_waits:
            return
        if self._audit_once(
            "CRYPTO_STREAM_CAPACITY_WAIT",
            {"version": CRYPTO_STREAM_CAPACITY_VERSION, "symbol": packet["symbol"],
             "capacity": CRYPTO_STREAM_SYMBOL_CAPACITY, "selection_event_seq": seq},
            f"stream-capacity:{seq}",
        ):
            self._capacity_waits.add(seq)

    def _admission_refused(self, packet, exc):
        """Audit a refusal once per runtime, selection and reason; decline permanent ones.

        A report-V3 refusal (``AdmissionRefused``) adds its evidence to both bodies: the
        system check's ``system_check`` or the supersession's run slots. Other bodies are
        unchanged.
        """
        reason, seq = failure_code(exc), packet.get("selection_event_seq")
        evidence = json_safe(exc.details) if isinstance(exc, AdmissionRefused) else {}
        self._audit_once(
            "RUNTIME_ADMISSION_REFUSED",
            {"runtime_id": self.runtime_id, "selection_event_seq": seq, "reason": reason,
             **evidence},
            f"runtime:{self.runtime_id}:admission-refused:{seq}:{reason}",
        )
        if permanent_refusal(packet, reason):
            self._decline(packet, seq, reason, evidence)

    def _decline(self, packet, seq, reason, evidence=None):
        """The selection's one RESEARCH_ADMISSION_DECLINED; it is never offered again.

        A top-K selection declined for anything but supersession gets its one replacement
        decision (TOPK_REPLACEMENT_V1, ``ResearchCycle.replace_declined``) in the decline's own
        transaction (``decline_selection``). When that decision is refused (a cycle-level code)
        nothing is written, the selection stays offered and every tick retries both; the fault
        is audited once per runtime, selection and code, without a latch.
        """
        body = {
            "cycle_id": packet.get("cycle_id"),
            "item_key": packet.get("item_key"),
            "revision": packet.get("revision"),
            "receipt_id": packet.get("receipt_id"),
            "selection_event_seq": seq,
            "reason": reason,
            **(evidence or {}),
        }
        key = f"{ADMISSION_DECLINED_KEY}{seq}"
        if not replaces_on_decline(packet, reason, self.research):
            return self._audit_once("RESEARCH_ADMISSION_DECLINED", body, key)
        try:
            decline_selection(self.execution.store, self.research, body, key)
        except ValueError as exc:
            code = failure_code(exc)
            self._audit_once(
                REPLACEMENT_FAULT_EVENT,
                {"runtime_id": self.runtime_id, "cycle_id": packet.get("cycle_id"),
                 "item_key": packet.get("item_key"), "selection_event_seq": seq,
                 "declined_code": reason, "code": code},
                f"runtime:{self.runtime_id}:replacement-fault:{seq}:{code}",
            )
            return False
        except Exception as exc:
            self._audit_unavailable(exc)
            return False
        self.latches.note_audit_success()
        return True

    def _live_quote(self, symbol):
        """Bid, ask and last for the V3 system check (``LivePriceReader``); may raise
        ``LivePriceUnavailable``."""
        with self.lock:
            row = self.observations.get(("CRYPTO", symbol))
            row = dict(row) if row else None
        return self.live_prices.read(symbol, row)

    # --- CRYPTO_ALPACA_TRIGGER_V1 (package crypto-trigger; rules in crypto_trigger.py) --------

    def _crypto_trigger_quote(self, symbol):
        """The freshest quote the crypto trigger version may use for ``symbol``, by read time.

        ``(quote, attempts, row, received)``: the ``LiveQuote`` (the stream quote when this
        runtime received it at most five seconds ago, else one REST latest-quote read under the
        reader's per-tick and per-symbol limits, shared with admission), or None and the
        reader's attempts; with a copy of the stream row and its quote's receipt time.
        """
        key = ("CRYPTO", symbol)
        with self.lock:
            row = self.observations.get(key)
            row = dict(row) if row else None
            received = self.quote_received.get(key)
        try:
            quote = self.live_prices.read(symbol, row, by_read_time=True, received_at=received)
        except LivePriceUnavailable as exc:
            return None, exc.attempts, row, received
        return quote, None, row, received

    def _crypto_print_observation(self, setup, printed):
        """A queued print of a setup of this version, with the fresh quote when the print is at
        or below the entry trigger. A print above it can neither touch nor invalidate, so no
        quote is read for it."""
        trigger = Decimal(str(setup["record_json"]["levels"]["entry_trigger"]))
        if Decimal(str(printed["trade_price"])) > trigger:
            with self.lock:
                row = self.observations.get(("CRYPTO", setup["symbol"]))
                row = dict(row) if row else None
            return crypto_trigger.observation(row=row, printed=printed)
        quote, attempts, row, received = self._crypto_trigger_quote(setup["symbol"])
        return crypto_trigger.observation(
            quote=quote, row=row, received_at=received, printed=printed, attempts=attempts
        )

    def _quote_read_order(self, setup):
        """Sort key: the symbol whose quote was read longest ago (stream receipt or REST
        attempt) first and never-read symbols before all, so the tick's one REST read rotates
        over every stale symbol instead of serving the first few forever."""
        with self.lock:
            received = self.quote_received.get(("CRYPTO", setup["symbol"]))
        attempted = self.live_prices.rest_attempted_at(setup["symbol"])
        known = [at for at in (received, attempted) if at is not None]
        return max(known) if known else datetime.min.replace(tzinfo=UTC)

    def _evaluate_quote_triggers(self, tick, active):
        """Quote-driven evaluation of CRYPTO_ALPACA_TRIGGER_V1, every protection tick.

        Quote touches do not arrive as prints, so each WATCHING setup of this version is
        evaluated against its symbol's latest fresh quote (``_crypto_trigger_quote``), the
        symbol read longest ago first. A fresh bid at or below the stop invalidates the setup
        whenever its market stream is ready (a ledger write only); a touch goes to
        ``observe_trigger`` only while entries are ready and outside a capacity cooldown. No
        quote is read and nothing is touched when no setup of this version is WATCHING, and
        nothing is written while no fresh quote touches or crosses a level; a wait is handed on
        once per setup, reason and minute. A fault latches entries (trigger scope) and ends
        the pass; the protection of every setup still runs.
        """
        candidates = [
            setup for setup in active
            if (setup.get("state") or {}).get("state") == "WATCHING"
            # CRYPTO_COINBASE_TRIGGER_V1 too (``_evaluate_reference_trigger``).
            and (crypto_trigger.active(setup.get("state"))
                 or coinbase_trigger.active(setup.get("state")))
            # CRYPTO_GAP_RESUME_V1: a setup held for its gap check is not evaluated at all.
            and not self._gap_pending(setup["setup_id"])
        ]
        if self._trigger_waits:
            watching = {str(setup["setup_id"]) for setup in candidates}
            self._trigger_waits = {
                key: minute for key, minute in self._trigger_waits.items() if key[0] in watching
            }
        for setup in sorted(candidates, key=self._quote_read_order):
            if not self._market_ready(setup["market"], setup["symbol"]):
                continue
            try:
                if coinbase_trigger.active(setup["state"]):
                    self._evaluate_reference_trigger(setup)
                else:
                    self._evaluate_quote_trigger(setup)
            except Exception as exc:
                self._record_failure(
                    tick, "TRIGGER", TRIGGER_SCOPE, exc, setup_id=setup["setup_id"]
                )
                return

    def _evaluate_quote_trigger(self, setup):
        quote, _, row, received = self._crypto_trigger_quote(setup["symbol"])
        if quote is None:
            return  # No fresh quote, no evaluation; a print touch records its own wait.
        observed = crypto_trigger.observation(quote=quote, row=row, received_at=received)
        state, now = setup["state"], self.now()
        levels = {k: Decimal(str(v)) for k, v in setup["record_json"]["levels"].items()}
        verdict = crypto_trigger.evaluate(
            levels, observed, now=now, admitted_at=datetime.fromisoformat(state["admitted_at"])
        )
        if verdict.outcome == crypto_trigger.INVALIDATE:
            self.execution.observe_trigger(setup["setup_id"], observed)
            return
        if verdict.outcome not in {crypto_trigger.CONFIRM, crypto_trigger.WAIT}:
            return
        if not self.ready():
            return
        until = state.get("capacity_deferred_until")
        if until and now < datetime.fromisoformat(until):
            return  # As authorize_entry: the capacity cooldown skips triggers, reading nothing.
        noted = minute = None
        if verdict.outcome == crypto_trigger.WAIT:
            noted = (str(setup["setup_id"]), verdict.reason)
            minute = now.astimezone(UTC).replace(second=0, microsecond=0)
            if self._trigger_waits.get(noted) == minute:
                return
        self.execution.observe_trigger(setup["setup_id"], observed)
        if noted is not None:
            self._trigger_waits[noted] = minute

    # --- CRYPTO_COINBASE_TRIGGER_V1 and CRYPTO_STOP_BREACH_V3 (Coinbase as the reference) ------

    def _evaluate_reference_trigger(self, setup):
        """CRYPTO_COINBASE_TRIGGER_V1 (coinbase_trigger.py), every protection tick.

        The coin's Coinbase view alone decides whether anything happened: a stop (invalidated
        whatever the runtime's readiness, a ledger write only), an unhealthy feed (a wait) or a
        touch. Only a touch reads Alpaca's freshest quote (the tick's shared reader, by read
        time) for the confirmation; nothing is read or written without one. A wait or a
        confirmed touch goes to ``observe_trigger`` only while entries are ready and outside a
        capacity cooldown, a wait at most once per setup, reason and minute (as V1)."""
        state, now = setup["state"], self.now()
        admitted = datetime.fromisoformat(state["admitted_at"])
        levels = {k: Decimal(str(v)) for k, v in setup["record_json"]["levels"].items()}
        view = coinbase_feed.read_view(
            self.reference_feed,
            state.get("reference_product") or coinbase_feed.product_id(setup["symbol"]), now)
        reference = coinbase_trigger.reference_record(view, levels, admitted_at=admitted, now=now)
        first = coinbase_trigger.reference_verdict(levels, reference, now=now,
                                                   admitted_at=admitted)
        if first.outcome == crypto_trigger.NO_TOUCH:
            return
        if first.outcome == coinbase_trigger.TOUCH:
            quote, attempts, row, received = self._crypto_trigger_quote(setup["symbol"])
            observed = coinbase_trigger.observation(
                reference=reference, quote=quote, row=row, received_at=received, attempts=attempts)
        else:
            observed = coinbase_trigger.observation(reference=reference)
        verdict = coinbase_trigger.evaluate(levels, observed, now=now, admitted_at=admitted)
        if verdict.outcome == crypto_trigger.INVALIDATE:
            self.execution.observe_trigger(setup["setup_id"], observed)
            return
        if verdict.outcome not in {crypto_trigger.CONFIRM, crypto_trigger.WAIT}:
            return
        if not self.ready():
            return
        until = state.get("capacity_deferred_until")
        if until and now < datetime.fromisoformat(until):
            return
        noted = minute = None
        if verdict.outcome == crypto_trigger.WAIT:
            noted = (str(setup["setup_id"]), verdict.reason)
            minute = now.astimezone(UTC).replace(second=0, microsecond=0)
            if self._trigger_waits.get(noted) == minute:
                return
        self.execution.observe_trigger(setup["setup_id"], observed)
        if noted is not None:
            self._trigger_waits[noted] = minute

    def _reference_admission_ready(self, packet):
        """A pick admitted under CRYPTO_COINBASE_TRIGGER_V1 waits until Coinbase's feed is healthy
        for its coin (subscription acknowledged on every channel, a current heartbeat), besides
        the Alpaca stream. Every other pick, and every pick without the feed, is unaffected."""
        feed = self.reference_feed
        if feed is None or not coinbase_trigger.applies(packet):
            return True
        try:
            return feed.health(coinbase_feed.product_id(packet["symbol"]), self.now())[0] is True
        except Exception:
            return False

    def _reference_products(self):
        """The Coinbase products the feed serves: each active crypto setup of the Coinbase
        versions and each offered pick CRYPTO_COINBASE_TRIGGER_V1 will admit. Read at most once
        a second; a failed read keeps the last products (the subscriptions stay)."""
        clock, cached = _monotonic(), self._reference_wanted
        if cached is not None and 0 <= clock - cached[0] < self.policy.market_poll_seconds:
            return cached[1]
        try:
            products = {
                s["state"].get("reference_product") or coinbase_feed.product_id(s["symbol"])
                for s in self.execution.store.active()
                if s["market"] == "CRYPTO" and (
                    coinbase_trigger.active(s["state"]) or stop_breach.active_v3(s["state"]))
            } | {
                coinbase_feed.product_id(p["symbol"]) for p in self._selected_packets()
                if coinbase_trigger.applies(p)
            }
            products = frozenset(p for p in products if p is not None)
        except Exception:
            products = cached[1] if cached is not None else frozenset()
        self._reference_wanted = (clock, products)
        return products

    def _reference_connected(self, products, refused):
        self._event("RUNTIME_REFERENCE_CONNECTED", {
            "provider": coinbase_feed.PROVIDER, "channels": list(coinbase_feed.CHANNELS),
            "products": list(products), "refused": refused})

    def _reference_stream_loop(self):
        """Coinbase's public feed while any setup or offered pick reads it, reconnected with the
        Alpaca streams' bounded backoff (``reconnect_wait``). A session that fails records
        RUNTIME_REFERENCE_GAP with its code; meanwhile the coins' products are unhealthy, so
        their entries wait and their stop breaches fall back to CRYPTO_STOP_BREACH_V2's
        evidence."""
        backoff = self.policy.reconnect_seconds
        while not self.stop_event.is_set():
            if not self._reference_products():
                self.stop_event.wait(self.policy.market_poll_seconds)
                continue
            started, code = _monotonic(), None
            try:
                ended = self.reference_feed.session(
                    wanted=self._reference_products, stop_event=self.stop_event,
                    open_timeout=self.policy.stream_open_timeout_seconds,
                    read_timeout=self.policy.stream_read_timeout_seconds,
                    on_acknowledged=self._reference_connected,
                )
            except Exception as exc:
                ended, code = "REFERENCE_FEED_DISCONNECTED", failure_code(exc)
            if self.stop_event.is_set():
                break
            if ended == coinbase_feed.NOTHING_WANTED:
                backoff = self.policy.reconnect_seconds
                continue
            body = {"provider": coinbase_feed.PROVIDER, "reason": ended}
            if code is not None:
                body["code"] = code
            self._event("RUNTIME_REFERENCE_GAP", body)
            backoff = reconnect_wait(backoff, _monotonic() - started, self.policy)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, self.policy.max_reconnect_seconds)

    def _retire_superseded_research(self, tick=None):
        """RESEARCH_RUN_SUPERSESSION_V1 (plan 4.4, package system-check), every tick.

        Once a report-V3 selection of a newer ``run_slot`` is published, each older V3 setup
        still WATCHING is revoked ``SUPERSEDED_BY_NEW_RESEARCH`` through the revoke path (one
        keyed REVOKE, only while still WATCHING), and each older V3 selection still offered for
        admission (unexpired, not admitted, not declined) gets its one
        RESEARCH_ADMISSION_DECLINED with the same reason. Positions, working entries and V2
        cycles are never touched; with nothing to retire nothing is written. The newest run is
        read from the ledger only while a V3 setup watches or a V3 selection waits, so a tick
        without V3 work adds no ledger read. A failure latches entries like a failed market-gap
        revocation; protection still runs.

        While the configured schedule is RESEARCH_SCHEDULE_V2, RESEARCH_RUN_SUPERSESSION_V2
        decides instead (``_retire_superseded_v2``, package research-loop-app).
        """
        if supersession_version(self._research_schedule()) == SUPERSESSION_VERSION_V2:
            return self._retire_superseded_v2(tick)
        try:
            watching = [
                setup for setup in self.execution.store.active()
                if (setup.get("state") or {}).get("state") == "WATCHING"
                and research_v3(setup.get("record_json"))
            ]
            pending = [packet for packet in self._selected_packets() if research_v3(packet)]
            if not watching and not pending:
                return
            with self.execution.repo.connect() as conn:
                newest = newest_v3_run_slot(conn)
        except Exception as exc:
            self._record_failure(tick, "TRIGGER", TRIGGER_SCOPE, exc)
            return
        if newest is None:
            return
        evidence = {"superseded_by_run_slot": newest.isoformat(),
                    "supersession_rule": SUPERSESSION_VERSION}
        for setup in watching:
            try:
                slot = setup["record_json"]["run_slot"]
                if run_slot_of(setup["record_json"]) < newest:
                    self.execution.revoke(
                        setup["setup_id"], SUPERSEDED_BY_NEW_RESEARCH, watching_only=True,
                        details={"run_slot": slot, **evidence},
                        key=f"research:superseded:setup:{setup['setup_id']}",
                    )
            except Exception as exc:
                self._record_failure(
                    tick, "TRIGGER", TRIGGER_SCOPE, exc, setup_id=setup["setup_id"]
                )
        for packet in pending:
            try:
                superseded = run_slot_of(packet) < newest
            except (KeyError, TypeError, ValueError):
                continue  # Admission refuses it for good (APP_REVIEWED_PACKET_REQUIRED).
            if superseded:
                self._decline(packet, packet["selection_event_seq"], SUPERSEDED_BY_NEW_RESEARCH,
                              {"run_slot": packet["run_slot"], **evidence})

    def _research_schedule(self):
        """``MANAGED_RESEARCH_SCHEDULE_JSON`` as the app configured it on the execution (None
        when absent, and in fixtures without one)."""
        return getattr(self.execution, "research_schedule", None)

    def _retire_superseded_v2(self, tick=None):
        """RESEARCH_RUN_SUPERSESSION_V2 (docs/RESEARCH-LOOP-V2.md 3.2, package
        research-loop-app), every tick while the configured schedule is RESEARCH_SCHEDULE_V2.

        V1's pass with V2's rule: a V3 setup still WATCHING, or a V3 selection still offered
        for admission, that answers run ``r`` is retired only when a published V3 selection of a
        later run is from a full run (any symbol) or is for the same symbol (any run kind;
        ``system_check.superseding_run_slot``). Everything else stays until its own expiry, so
        an adjusted pick Jev does not select supersedes nothing. The same revoke path, keys,
        decline and ordering as V1, recording ``supersession_rule`` V2. The selections read
        are only those whose run is later than the oldest watched or offered one.

        Only the V3 selections this pass read may be admitted in this tick
        (``_supersession_checked``): one published while the tick runs waits for the next
        pass, so an adjusted pick never meets the setup it replaces at admission (where
        ACTIVE_SYMBOL_ALREADY_MANAGED would decline it). A failure admits no V3 selection.
        """
        schedule = self._research_schedule()
        self._supersession_checked = frozenset()
        try:
            watching = [
                setup for setup in self.execution.store.active()
                if (setup.get("state") or {}).get("state") == "WATCHING"
                and research_v3(setup.get("record_json"))
            ]
            pending = [packet for packet in self._selected_packets() if research_v3(packet)]
            if not watching and not pending:
                return
            slots = [slot for slot in (_run_slot_or_none(record) for record in (
                *(setup["record_json"] for setup in watching), *pending)) if slot is not None]
            newer = []
            if slots:
                with self.execution.repo.connect() as conn:
                    newer = newer_v3_selections(conn, min(slots))
        except Exception as exc:
            self._record_failure(tick, "TRIGGER", TRIGGER_SCOPE, exc)
            return
        for setup in watching:
            try:
                record = setup["record_json"]
                newest = superseding_run_slot(schedule, run_slot_of(record), setup["symbol"],
                                              newer)
                if newest is not None:
                    self.execution.revoke(
                        setup["setup_id"], SUPERSEDED_BY_NEW_RESEARCH, watching_only=True,
                        details={"run_slot": record["run_slot"],
                                 "superseded_by_run_slot": newest.isoformat(),
                                 "supersession_rule": SUPERSESSION_VERSION_V2},
                        key=f"research:superseded:setup:{setup['setup_id']}",
                    )
            except Exception as exc:
                self._record_failure(
                    tick, "TRIGGER", TRIGGER_SCOPE, exc, setup_id=setup["setup_id"]
                )
        checked = []
        for packet in pending:
            try:
                newest = superseding_run_slot(schedule, run_slot_of(packet), packet["symbol"],
                                              newer)
            except (KeyError, TypeError, ValueError):
                newest = None  # Admission refuses it for good (APP_REVIEWED_PACKET_REQUIRED).
            if newest is None:
                checked.append(packet["selection_event_seq"])
                continue
            self._decline(packet, packet["selection_event_seq"], SUPERSEDED_BY_NEW_RESEARCH,
                          {"run_slot": packet["run_slot"],
                           "superseded_by_run_slot": newest.isoformat(),
                           "supersession_rule": SUPERSESSION_VERSION_V2})
        self._supersession_checked = frozenset(checked)

    def _admission_due(self, packet):
        """Whether admission may take ``packet`` in this tick: always, except a V3 selection
        that this tick's RESEARCH_RUN_SUPERSESSION_V2 pass did not read (next tick)."""
        checked = self._supersession_checked
        return checked is None or not research_v3(packet) or (
            packet["selection_event_seq"] in checked)

    def reconcile_once(self):
        with self.reconcile_lock:
            return self._reconcile_once_locked()

    def _backfill_gap(self):
        """REST fill backfill for the gap before this trade-updates connection (plan 4.4).

        Runs in the protective-and-recovery budget class. A failure keeps reconciliation,
        and so entries, unready until a later reconciliation backfills successfully: rate
        limits and timeouts latch REST_DEGRADED; nothing is halted.
        """
        backfill = getattr(self.execution, "rest_backfill", None)
        if backfill is None:
            return True
        try:
            with request_priority(PROTECTIVE):
                backfill()
        except Exception as exc:
            if classify_failure(exc) == REST_CATEGORY:
                self._record_failure(None, "RECONCILIATION", RUNTIME_SCOPE, exc)
            self._event(
                "RUNTIME_RECONCILIATION_FAILED",
                {"reason": "FILL_BACKFILL_UNAVAILABLE", "code": failure_code(exc)},
            )
            return False
        return True

    def fee_import_once(self):
        """Best-effort periodic Alpaca fee read (package fees-net-r).

        Bounded (``ManagedExecution.fee_backfill``'s own window) and read-only; a
        failure here is only ever logged by ``_loop`` (``RUNTIME_WORKER_FAILURE``,
        worker ``fee_import``) and never blocks trading, protection or reconciliation.
        A double without ``fee_backfill`` (e.g. a minimal test execution object) is
        simply skipped, same as an unbackfillable execution in ``_backfill_gap``.
        """
        backfill = getattr(self.execution, "fee_backfill", None)
        if backfill is not None:
            with request_priority(RESEARCH):
                backfill()

    def _reconcile_once_locked(self):
        with self.lock:
            self.reconciled_at = None
            self.execution.reconciled_at = None
            # A new trade-updates connection is backfilled once before it reconciles.
            socket = self.socket if self.connected else None
        if socket is not None and socket is not getattr(self, "_backfilled_socket", None):
            if not self._backfill_gap():
                return False
            self._backfilled_socket = socket
        try:
            with request_priority(RECONCILIATION):
                result = self.execution.reconcile()
            with self.lock:
                if result.get("clean"):
                    self.reconciled_at = self.now()
                    self.last_clean_reconciliation_at = self.reconciled_at
        except Exception as exc:
            if classify_failure(exc) == REST_CATEGORY:
                self._record_failure(None, "RECONCILIATION", RUNTIME_SCOPE, exc)
            self._event("RUNTIME_RECONCILIATION_FAILED", {"reason": "BROKER_STATE_UNAVAILABLE"})
            return False
        if result.get("clean"):
            # Latch clear rules require a clean reconciliation completed after the failure.
            self.latches.note_clean_reconciliation()
            self._maintain_latches()
        return bool(result.get("clean"))

    def _market_ready(self, market, symbol):
        market = "US" if market == "US_STOCKS" else market
        with self.lock:
            return (
                self.market_connected.get(market, False)
                and symbol in self.market_subscriptions.get(market, set())
                and market not in self.market_gaps
            )

    def _desired_symbols(self, market):
        return self._stream_plan(market)[0]

    def _stream_plan(self, market, packets=None):
        """``(wanted, held_back)``: the symbols ``market``'s stream subscribes, and the offered
        picks' symbols it holds back (selection order, each once). ``packets`` is the caller's
        ``_selected_packets()`` read, so a tick reads it once; None reads it here.

        US: every active setup's and offered pick's symbol; nothing is held back.
        CRYPTO (CRYPTO_STREAM_CAPACITY_V1): every active setup's coin always, and Bitcoin while
        a maintained setup is active, even beyond the capacity (after this version they cannot
        exceed it: admission needs an acknowledged subscription). Then the offered picks in
        selection order while the plan stays within CRYPTO_STREAM_SYMBOL_CAPACITY; one slot is
        kept for Bitcoin while it is not wanted (admitting a maintained pick adds it), and a pick
        for a coin already wanted takes no slot. The others are held back.
        """
        label = "US_STOCKS" if market == "US" else market
        active = self.execution.store.active()
        offered = [p["symbol"] for p in (
            self._selected_packets() if packets is None else packets) if p["market"] == label]
        wanted = {s["symbol"] for s in active if s["market"] == label}
        if market != "CRYPTO":
            return wanted | set(offered), []
        if any(crypto_maintenance.active(s["state"]) for s in active):
            # CRYPTO_MAINTENANCE_V1: Bitcoin's price feeds the 3%-in-15-minutes shock trigger,
            # with or without a Bitcoin trade.
            wanted.add(crypto_maintenance.BENCHMARK_SYMBOL)
        held_back = []
        for symbol in offered:
            if symbol in wanted or symbol in held_back:
                continue
            bitcoin_wanted = crypto_maintenance.BENCHMARK_SYMBOL in (wanted | {symbol})
            if len(wanted) + (1 if bitcoin_wanted else 2) <= CRYPTO_STREAM_SYMBOL_CAPACITY:
                wanted.add(symbol)
            else:
                held_back.append(symbol)
        return wanted, held_back

    def market_gap(self, market, reason, *, code=None):
        """Invalidate queued work immediately; the execution lane records setup revocations.
        ``code`` is the sanitized failure code of the stream session that ended on an error
        (``failure_code``); the gap event carries it beside ``reason`` (package ops-alarms)."""
        now = self.now()
        # CRYPTO_GAP_RESUME_V1: where a held setup's unobserved window starts. At startup that
        # is the previous runtime's last recorded observation of the market (from the ledger).
        bound, basis = (
            self._restart_bound(market, now) if reason == RESTART_GAP_REASON
            else (now, {"basis": gap_resume.RUNTIME_GAP})
        )
        with self.lock:
            self.market_connected[market] = False
            self.market_subscriptions[market] = set()
            self.market_gaps.add(market)
            self.reconciled_at = None
            self.execution.reconciled_at = None
            self.observations = {k: v for k, v in self.observations.items() if k[0] != market}
            self.quote_received = {
                k: v for k, v in self.quote_received.items() if k[0] != market
            }
            self._gap_generation[market] = self._gap_generation.get(market, 0) + 1
            self._gap_bounds.setdefault(market, (bound, reason, basis))  # The earliest one.
            self._unobserved.setdefault(market, bound)
        if market == "CRYPTO":
            self.benchmark.reset()  # The missing interval cannot be reconstructed.
            pacing = getattr(self.execution, "entry_pacing", None)
            if pacing is not None:
                pacing.refresh_due()  # CRYPTO_ENTRY_PACING_V1: re-seed from bars at once.
        body = {"market": market, "reason": reason}
        if code is not None:
            body["code"] = code
        self._event("RUNTIME_MARKET_GAP", body)

    def _restart_bound(self, market, now):
        """``(bound, basis)``: the previous runtime's last recorded observation of ``market``
        (``gap_resume.restart_bound``); no bound when there is none or the ledger is unreadable,
        so a held setup's window then starts at its admission."""
        repo = getattr(self.execution, "repo", None)
        if repo is None:
            return None, {"basis": gap_resume.LEDGER_UNAVAILABLE}
        try:
            with repo.connect() as conn:
                row = gap_resume.previous_heartbeat(conn, self.runtime_id)
        except Exception as exc:
            return None, {"basis": gap_resume.LEDGER_UNAVAILABLE, "code": failure_code(exc)}
        return gap_resume.restart_bound(row, market, now)

    def _invalidate_market_gaps(self, tick=None):
        """Revoke the WATCHING setups of every gapped market, each setup in isolation.

        A setup of CRYPTO_GAP_RESUME_V1 is held for its gap check instead
        (``_hold_for_gap_check``); every other setup is revoked exactly as before. A market's
        gap (which keeps entries blocked) is cleared only once all its WATCHING setups are
        revoked or held with their GAP_RESUME_PENDING recorded; a setup whose revocation or
        record fails is retried next tick. No failure here stops the other setups, or the
        protection pass of this tick.
        """
        with self.lock:
            gaps = set(self.market_gaps)
            bounds = {market: self._gap_bounds.get(market) for market in gaps}
        if not gaps:
            return
        try:
            active = self.execution.store.active()
        except Exception as exc:
            self._record_failure(tick, "TRIGGER", TRIGGER_SCOPE, exc)
            return
        unresolved = set()
        for setup in active:
            market = "US" if setup["market"] == "US_STOCKS" else setup["market"]
            if market in gaps and setup["state"]["state"] == "WATCHING":
                try:
                    if gap_resume.applies(setup["state"]):
                        self._hold_for_gap_check(setup, market, bounds[market])
                    else:
                        self.execution.revoke(setup["setup_id"], "DATA_FEED_FAILURE")
                except Exception as exc:
                    unresolved.add(market)
                    self._record_failure(
                        tick, "TRIGGER", TRIGGER_SCOPE, exc, setup_id=setup["setup_id"]
                    )
        with self.lock:
            self.market_gaps.difference_update(gaps - unresolved)
            for market in gaps - unresolved:
                self._gap_bounds.pop(market, None)

    # --- CRYPTO_GAP_RESUME_V1 (package gap-resume; rules in gap_resume.py) -------------------

    def _gap_pending(self, setup_id):
        with self.lock:
            return str(setup_id) in self.gap_checks

    def _release_gap_check(self, check):
        with self.lock:
            if self.gap_checks.get(check.setup_id) is check:
                del self.gap_checks[check.setup_id]

    def _hold_for_gap_check(self, setup, market, gap):
        """Hold a WATCHING setup of this version for its one bar check instead of revoking it.

        From here on no trigger is evaluated for it and its queued prints are consumed
        unevaluated. Its window starts at the gap's bound, never later than an earlier gap still
        open for it in the ledger or than its earliest queued print never evaluated, never
        before its admission. One GAP_RESUME_PENDING per hold (a held setup that meets another
        gap keeps its window); a failed read or write raises and is retried next tick.
        """
        key = str(setup["setup_id"])
        bound, reason, basis = gap if gap is not None else (
            None, "GAP_BOUND_UNKNOWN", {"basis": "GAP_BOUND_UNKNOWN"})
        with self.lock:
            check = self.gap_checks.get(key)
            if check is None:
                check = self.gap_checks[key] = GapCheck(
                    key, market, setup["symbol"], self.now(), bound, reason, basis
                )
        if check.recorded:
            return
        if check.body is None:
            with self.execution.repo.connect() as conn:
                open_start = gap_resume.open_pending_start(conn, key)
                unconsumed = gap_resume.earliest_unconsumed_print(conn, key)
            start = gap_resume.window_start(
                setup["state"]["admitted_at"], check.bound, open_start=open_start,
                unconsumed_print_at=unconsumed,
            )
            check.body = json_safe({
                "version": gap_resume.GAP_RESUME_VERSION,
                "runtime_id": self.runtime_id,
                "market": market,
                "symbol": check.symbol,
                "gap_reason": check.reason,
                "gap_started_at": check.bound,
                "basis": check.basis,
                "open_gap_window_start": open_start,
                "unconsumed_print_at": unconsumed,
                "admitted_at": setup["state"]["admitted_at"],
                "window_start": start,
                "pending_since": check.pending_since,
            })
        pending_key = f"gap-resume:pending:{key}:{self.runtime_id}:{check.body['pending_since']}"
        if gap_resume.record_pending(self.execution.store, key, check.body, pending_key):
            check.recorded = True
        else:
            self._release_gap_check(check)  # It ended meanwhile; nothing is held.

    def _gap_check_ready(self, now):
        """The trade-updates stream is connected and this process's reconciliation is clean."""
        with self.lock:
            return (
                self.connected
                and self.reconciled_at is not None
                and self.execution.reconciled_at is not None
                and 0 <= (now - self.reconciled_at).total_seconds() <= self.policy.reconcile_seconds
            )

    def _run_gap_checks(self, tick=None):
        """CRYPTO_GAP_RESUME_V1, every protection tick: each held setup gets its one bar check.

        A check waits until its market stream is back with the symbol acknowledged (and no
        newer gap), the trade-updates stream is connected and this process's reconciliation is
        clean, and until the minute in which the stream came back has closed and settled. It
        takes the tick's one market-data REST read (shared with admission and the crypto
        trigger), so at most one check runs per tick; a gap that begins during its read
        discards the result and the setup waits for the stream again. A setup that ended
        another way (expiry, supersession) is released; one whose entry window has closed is
        left to ``manage`` (it expires exactly as today). A fault latches entries (trigger
        scope) and the setup stays held for the next tick.
        """
        now = self.now()
        with self.lock:
            for market in list(self._unobserved):
                if self.market_connected.get(market, False) and market not in self.market_gaps:
                    del self._unobserved[market]  # Observed again: the next gap starts afresh.
            checks = list(self.gap_checks.values())
        if not checks:
            return
        try:
            active = {str(s["setup_id"]): s for s in self.execution.store.active()}
        except Exception as exc:
            self._record_failure(tick, "TRIGGER", TRIGGER_SCOPE, exc)
            return
        for check in sorted(checks, key=lambda c: (c.pending_since, c.setup_id)):
            setup = active.get(check.setup_id)
            if setup is None or (setup.get("state") or {}).get("state") != "WATCHING":
                self._release_gap_check(check)
                continue
            if not check.recorded:
                continue  # Its GAP_RESUME_PENDING is retried by the gap pass first.
            with self.lock:
                generation = self._gap_generation.get(check.market, 0)
                if not self._market_ready(check.market, check.symbol):
                    check.stream_back_at = check.generation = None
                    continue
                if check.stream_back_at is None or check.generation != generation:
                    check.stream_back_at, check.generation = now, generation
                    continue
            if now < gap_resume.due_at(check.stream_back_at) or not self._gap_check_ready(now):
                continue
            deadline = setup["expires_at"]
            if setup["state"].get("crypto_entry_deadline"):
                deadline = min(deadline, datetime.fromisoformat(
                    setup["state"]["crypto_entry_deadline"]))
            if now >= deadline:
                continue  # manage() expires it this tick, exactly as today.
            if not self.live_prices.spend_read():
                return  # This tick's market-data read is spent; the next tick continues.
            try:
                self._gap_check(setup, check, now)
            except Exception as exc:
                self._record_failure(
                    tick, "TRIGGER", TRIGGER_SCOPE, exc, setup_id=setup["setup_id"]
                )

    def _gap_bars(self, market, symbol, start, end):
        """The window's completed one-minute bars through the read-only market source (GET);
        any failure is an unavailable read (``(bars, issue codes)``), never an exception."""
        read = getattr(self.source, "window_bars", None)
        if read is None:
            return (), ("WINDOW_BARS_UNAVAILABLE",)
        try:
            with request_priority(RESEARCH):
                return read(market, symbol, start=start, end=end)
        except Exception as exc:
            return (), (failure_code(exc),)

    def _refresh_entry_pacing(self):
        """CRYPTO_ENTRY_PACING_V1: seed the market window from completed one-minute bars (the
        same read-only ``window_bars`` route as the gap check), at most once a minute, for the
        streamed coins and, while fewer than three are streamed, the reference basket. Never
        raises: a failed read leaves the window as it was, so paced entries keep waiting."""
        pacing = getattr(self.execution, "entry_pacing", None)
        if pacing is None:
            return None
        with self.lock:
            streamed = set(self.market_subscriptions.get("CRYPTO", ()))
        try:
            return regime_gate.refresh(
                pacing, lambda symbol, start, end: self._gap_bars("CRYPTO", symbol, start, end),
                streamed, self.now())
        except Exception as exc:
            pacing.last_refresh = {"at": self.now().isoformat(), "error": failure_code(exc)}
            return None

    def _gap_check(self, setup, check, now):
        """One held setup's bar check (``gap_resume.decide``) and its record."""
        generation, window_start = check.generation, check.window_start
        bars_start, bars_end = gap_resume.bar_window(window_start, now)
        bars, issues = self._gap_bars(check.market, check.symbol, bars_start, bars_end)
        with self.execution.repo.connect() as conn:
            prints = gap_resume.recorded_prints(conn, check.setup_id)
        verdict = gap_resume.decide(
            setup["record_json"]["levels"],
            window={"start": window_start, "bars_start": bars_start, "bars_end": bars_end,
                    "stream_back_at": check.stream_back_at},
            bars=bars, issues=issues, prints=prints, checked_at=now,
            context={"reason": check.reason, "started_at": check.body["gap_started_at"],
                     "basis": check.basis, "pending_since": check.body["pending_since"],
                     "runtime_id": self.runtime_id},
        )
        with self.lock:
            if (self._gap_generation.get(check.market, 0) != generation
                    or not self._market_ready(check.market, check.symbol)):
                check.stream_back_at = check.generation = None
                return None  # A new gap began during the read: wait for the stream again.
        key = (f"gap-resume:{verdict.decision.lower()}:{check.setup_id}:{self.runtime_id}:"
               f"{check.body['pending_since']}")
        gap_resume.record_verdict(self.execution, setup["setup_id"], verdict, key)
        self._release_gap_check(check)
        return verdict

    # Audit volume (plan 4.7). A print above the entry trigger cannot trigger: the trigger
    # check returns before any decision for ``price > entry_trigger``. When it also arrives
    # fresh, with a quote, inside the entry window and while the runtime is ready, it cannot
    # invalidate the setup either, so it joins one MARKET_PRINT_SUMMARY per symbol and
    # minute. Every other print (at or below the trigger, older than
    # QUIET_PRINT_MAX_AGE_SECONDS on arrival so that its evaluation could still reach the
    # five-second deadline, quote-less, or arriving while entries are blocked) is written
    # and evaluated on its own exactly as before; a stale one still revokes the setup.
    coalesce_quiet_prints = True
    QUIET_PRINT_MAX_AGE_SECONDS = 2
    PRINT_SUMMARY_SECONDS = 60

    def _quiet_print(self, setup, observation):
        """True only when this print can neither trigger nor invalidate ``setup``."""
        if not self.coalesce_quiet_prints:
            return False
        try:
            state = setup["state"]
            levels = setup["record_json"]["levels"]
            if state.get("state") != "WATCHING" or not (
                Decimal(str(observation["trade_price"])) > Decimal(str(levels["entry_trigger"]))
            ):
                return False
            now = self.now()
            trade_at = datetime.fromisoformat(observation["trade_at"])
            admitted = state.get("admitted_at")
            if admitted is None or trade_at < datetime.fromisoformat(admitted):
                return False
            if not 0 <= (now - trade_at).total_seconds() <= self.QUIET_PRINT_MAX_AGE_SECONDS:
                return False
            if not (crypto_trigger.active(state) or coinbase_trigger.active(state)) and not all(
                observation.get(k) for k in ("bid", "ask", "quote_at")
            ):
                # The per-print path revokes a quote-less print. CRYPTO_ALPACA_TRIGGER_V1 never
                # does (a thin coin's print may come before any quote), so for it a print above
                # the trigger is quiet with or without a quote. CRYPTO_COINBASE_TRIGGER_V1 never
                # evaluates an Alpaca print at all.
                return False
            deadline = setup["expires_at"]
            if state.get("crypto_entry_deadline"):
                deadline = min(deadline, datetime.fromisoformat(state["crypto_entry_deadline"]))
            if now >= deadline:
                return False  # The per-print path records the expiry.
            if setup["market"] == "US_STOCKS" and not self._inside_entry_session(now, trade_at):
                return False
            return self.ready() and self._market_ready(setup["market"], setup["symbol"])
        except Exception:
            return False  # Anything unclear is written and evaluated on its own.

    def _inside_entry_session(self, now, trade_at):
        """``observe_trigger``'s US session test: both instants in session, before flatten."""
        from catalyst_lab.market import NY

        day = now.astimezone(NY).date()
        cached = getattr(self, "_print_sessions", None)
        if cached is None or cached[0] != day:  # One calendar read per New York date.
            cached = (day, tuple(self.execution.broker.calendar(day, day)))
            self._print_sessions = cached
        session = next((s for s in cached[1] if s.contains(now)), None)
        return (
            session is not None
            and session.contains(now)
            and session.contains(trade_at)
            and now < session.flatten_time
        )

    def _coalesce_print(self, setup, observation):
        market = "US" if setup["market"] == "US_STOCKS" else setup["market"]
        trade_at = datetime.fromisoformat(observation["trade_at"])
        minute = trade_at.astimezone(UTC).replace(second=0, microsecond=0)
        price = Decimal(str(observation["trade_price"]))
        identity = (str(observation.get("trade_id")), observation["trade_at"])
        due = []
        with self.lock:
            book = self.__dict__.setdefault("_print_summaries", {})
            current = book.get((market, setup["symbol"]))
            if current is not None and current["minute"] != minute:
                due.append(book.pop((market, setup["symbol"])))
                current = None
            if current is None:
                current = book[(market, setup["symbol"])] = {
                    "market": market, "symbol": setup["symbol"], "minute": minute, "count": 0,
                    "first_trade_id": identity[0], "first_trade_at": identity[1],
                    "high": price, "low": price, "setup_ids": set(), "identity": None,
                    "data_provider": observation.get("data_provider"),
                    "data_feed": observation.get("data_feed"),
                }
            if current["identity"] != identity:  # One print may be quiet for several setups.
                current["identity"] = identity
                current["count"] += 1
                current["last_trade_id"], current["last_trade_at"] = identity
                current["high"], current["low"] = max(current["high"], price), min(
                    current["low"], price
                )
            current["setup_ids"].add(str(setup["setup_id"]))
        for summary in due:
            self._write_print_summary(summary)

    def _flush_print_summaries(self, *, force=False):
        """Write every summary whose minute can no longer receive a quiet print."""
        now = self.now()
        closed = timedelta(seconds=self.PRINT_SUMMARY_SECONDS + self.QUIET_PRINT_MAX_AGE_SECONDS)
        with self.lock:
            book = self.__dict__.setdefault("_print_summaries", {})
            retry = self.__dict__.setdefault("_print_summary_retry", [])
            due = retry[:] + [
                book.pop(key) for key, summary in list(book.items())
                if force or now - summary["minute"] >= closed
            ]
            retry.clear()
        for summary in due:
            self._write_print_summary(summary)

    def _write_print_summary(self, summary):
        body = {
            "runtime_id": self.runtime_id,
            "market": summary["market"],
            "symbol": summary["symbol"],
            "minute": summary["minute"].isoformat(),
            "count": summary["count"],
            "first_trade_id": summary["first_trade_id"],
            "first_trade_at": summary["first_trade_at"],
            "last_trade_id": summary["last_trade_id"],
            "last_trade_at": summary["last_trade_at"],
            "high": str(summary["high"]),
            "low": str(summary["low"]),
            "setup_ids": sorted(summary["setup_ids"]),
            "data_provider": summary["data_provider"],
            "data_feed": summary["data_feed"],
            "reason": "ABOVE_ENTRY_TRIGGER_NO_DECISION",
            "quiet_print_max_age_seconds": self.QUIET_PRINT_MAX_AGE_SECONDS,
        }
        key = (
            f"market-print-summary:{self.runtime_id}:{summary['market']}:{summary['symbol']}:"
            f"{body['minute']}:{summary['first_trade_id']}:{summary['first_trade_at']}"
        )
        if not self._audit_once("MARKET_PRINT_SUMMARY", body, key):
            with self.lock:  # Retried unchanged (same key and body) at the next flush.
                self.__dict__.setdefault("_print_summary_retry", []).append(summary)

    def _append_trade(self, setup, observation):
        if self._quiet_print(setup, observation):
            self._coalesce_print(setup, observation)
            return
        key = (
            f"market-print:{setup['setup_id']}:{observation['trade_id']}:{observation['trade_at']}"
        )
        with self.execution.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone():
                return
            pending = conn.execute("""SELECT count(*) AS n FROM lab.managed_events e
                WHERE kind='MARKET_PRINT' AND NOT EXISTS(SELECT 1 FROM lab.managed_events c
                WHERE c.idempotency_key='market-consumed:'||e.event_seq::text)""").fetchone()["n"]
            if pending >= self.policy.market_queue_capacity:
                raise ValueError("MARKET_QUEUE_OVERLOAD")
            self.execution.store.event(
                conn, "MARKET_PRINT", observation, setup_id=setup["setup_id"], key=key
            )
        self.print_queued.set()  # Committed: the protection pass need not sleep out its second.

    def _pending_trades(self):
        with self.execution.repo.connect() as conn:
            return conn.execute(
                """SELECT e.* FROM lab.managed_events e
                WHERE kind='MARKET_PRINT' AND NOT EXISTS(SELECT 1 FROM lab.managed_events c
                WHERE c.idempotency_key='market-consumed:'||e.event_seq::text)
                ORDER BY e.event_seq LIMIT %s""",
                (self.policy.market_queue_capacity,),
            ).fetchall()

    def _consume_trade(self, row, reason):
        with self.execution.store.transaction() as conn:
            self.execution.store.event(
                conn,
                "MARKET_PRINT_CONSUMED",
                {
                    "print_event_seq": row["event_seq"],
                    "reason": reason,
                },
                setup_id=row["setup_id"],
                key="market-consumed:" + str(row["event_seq"]),
            )

    def market_message(self, market, message):
        kind, symbol = message.get("T"), message.get("S")
        with self.lock:
            subscribed = self.market_connected.get(
                market, False
            ) and symbol in self.market_subscriptions.get(market, set())
        if kind not in {"q", "t"} or not subscribed:
            raise ValueError("UNSUBSCRIBED_MARKET_MESSAGE")
        stamp = datetime.fromisoformat(message["t"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > self.now() + timedelta(
                seconds=MARKET_CLOCK_TOLERANCE_SECONDS):
            raise ValueError("INVALID_MARKET_TIMESTAMP")
        feed = self.stock_feed if market == "US" else "CRYPTO_US"
        with self.lock:
            received = self.now()
            previous = self.observations.get((market, symbol), {})
            row = {
                **previous,
                "data_provider": "ALPACA",
                "data_feed": feed,
                "feed_healthy": True,
                "retrieved_at": received.isoformat(),
            }
            if kind == "q":
                bid, ask = Decimal(str(message["bp"])), Decimal(str(message["ap"]))
                if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
                    raise ValueError("INVALID_MARKET_QUOTE")
                if previous.get("quote_at") and stamp < datetime.fromisoformat(
                    previous["quote_at"]
                ):
                    raise ValueError("OUT_OF_ORDER_MARKET_QUOTE")
                row.update(bid=str(bid), ask=str(ask), quote_at=stamp.isoformat())
                self.quote_received[(market, symbol)] = received
            else:
                price = Decimal(str(message["p"]))
                if not price.is_finite() or price <= 0 or message.get("i") is None:
                    raise ValueError("INVALID_MARKET_TRADE")
                if previous.get("trade_at") and stamp < datetime.fromisoformat(
                    previous["trade_at"]
                ):
                    raise ValueError("OUT_OF_ORDER_MARKET_TRADE")
                row.update(
                    trade_price=str(price), trade_at=stamp.isoformat(), trade_id=str(message["i"])
                )
            self.observations[(market, symbol)] = row
            self.last_market_poll = self.now().isoformat()
        if market == "CRYPTO" and symbol == crypto_maintenance.BENCHMARK_SYMBOL:
            # The Bitcoin window of CRYPTO_MAINTENANCE_V1: quote mids and trade prints.
            self.benchmark.observe(
                (Decimal(row["bid"]) + Decimal(row["ask"])) / 2 if kind == "q"
                else Decimal(row["trade_price"]), stamp,
            )
        pacing = getattr(self.execution, "entry_pacing", None)
        if market == "CRYPTO" and pacing is not None:
            # CRYPTO_ENTRY_PACING_V1 (package risk-pacing): every streamed coin's quote mids and
            # trade prints feed the market window of the -2% median 1-hour gate.
            pacing.window.observe(
                symbol, (Decimal(row["bid"]) + Decimal(row["ask"])) / 2 if kind == "q"
                else Decimal(row["trade_price"]), stamp,
            )
        if kind == "t":
            # Every print survives later ticks; no latest-price overwrite can erase a stop touch.
            for setup in self.execution.store.active():
                label = "US" if setup["market"] == "US_STOCKS" else setup["market"]
                if (
                    label == market
                    and setup["symbol"] == symbol
                    and setup["state"]["state"] == "WATCHING"
                ):
                    self._append_trade(setup, row)

    def observation(self, setup):
        market = "US" if setup["market"] == "US_STOCKS" else setup["market"]
        with self.lock:
            value = self.observations.get((market, setup["symbol"]))
            return dict(value) if value else None

    def execution_once(self):
        if self.exit_code is not None:
            return  # Terminal: this process no longer owns the account.
        tick = TickRecord()
        try:
            with request_priority(PROTECTIVE):
                self._execution_once(tick)
        except Exception as exc:
            self._record_failure(tick, "TICK", RUNTIME_SCOPE, exc)
        lost = getattr(self.executor_lease, "lost_code", None)
        if lost is not None:
            # A claim or send inside this tick found the lease gone (it sent nothing).
            self._ownership_lost(lost)
        if self.exit_code is not None:
            return
        self._finish_tick(tick)

    OWNERSHIP_LOSS_EVENT_ATTEMPTS = 3
    OWNERSHIP_LOSS_EVENT_PAUSE_SECONDS = 0.5

    def _ownership_lost(self, code):
        """End this runtime after its executor lease is lost (plan phase 0, 2026-09-26).

        Terminal and never re-acquired in-process: a second executor may already hold the
        account, and every broker send re-checks the lost lease, so nothing more is sent. The
        loss is recorded durably (bounded retries, since the database may be the cause), then
        every loop stops and ``on_fatal`` hands the process to its supervisor with
        EXECUTOR_OWNERSHIP_LOST_EXIT_CODE; the new process restarts through normal startup.
        """
        with self.lock:
            if self.exit_code is not None:
                return
            self.exit_code = EXECUTOR_OWNERSHIP_LOST_EXIT_CODE
            self.connected = False
            self.reconciled_at = None
            self.execution.reconciled_at = None
        body = {
            "runtime_id": self.runtime_id,
            "code": code,
            "action": "PROCESS_EXIT",
            "exit_code": EXECUTOR_OWNERSHIP_LOST_EXIT_CODE,
            "detected_at": self.now().isoformat(),
        }
        recorded = False
        for attempt in range(self.OWNERSHIP_LOSS_EVENT_ATTEMPTS):
            try:
                self._write_event(
                    EXECUTOR_OWNERSHIP_LOST_EVENT, body,
                    key=f"runtime:{self.runtime_id}:executor-ownership-lost",
                )
                recorded = True
                break
            except Exception:
                if attempt + 1 < self.OWNERSHIP_LOSS_EVENT_ATTEMPTS:
                    time.sleep(self.OWNERSHIP_LOSS_EVENT_PAUSE_SECONDS)
        # Codes only; the private launcher writes this line to the rotating component log.
        print(f"{EXECUTOR_OWNERSHIP_LOST_EVENT} code={code} "
              f"exit_code={EXECUTOR_OWNERSHIP_LOST_EXIT_CODE} recorded={recorded}",
              file=sys.stderr, flush=True)
        self.stop_event.set()
        if self.on_fatal is not None:
            self.on_fatal(EXECUTOR_OWNERSHIP_LOST_EXIT_CODE)

    def _record_failure(self, tick, phase, scope, exc, *, setup_id=None):
        """Latch one failure by cause; entry blocking applies before this returns."""
        category = classify_failure(exc)
        if category == REST_CATEGORY:
            cause, latch_scope, latch_setup = REST, RUNTIME_SCOPE, None
        elif phase == "SAFETY":
            cause, latch_scope, latch_setup = ACCOUNT_SAFETY, RUNTIME_SCOPE, None
        else:
            cause, latch_scope, latch_setup = PROTECTION, scope, setup_id
        self.latches.record(
            cause, latch_scope, category, failure_code(exc), type(exc).__name__, phase,
            self.now(), setup_id=latch_setup,
        )
        if tick is not None:
            tick.protection_failed |= phase in PROTECTION_PHASES
            tick.rest_failed |= category == REST_CATEGORY
            if cause == PROTECTION and phase in {"TICK", "MANAGE"}:
                tick.failed_scopes[scope] = setup_id
        with self.lock:
            if phase == "SAFETY":
                self.account_safety_healthy = False
            if category != REST_CATEGORY:
                # A rate limit says nothing about broker state; every other failure might.
                self.reconciled_at = None
                self.execution.reconciled_at = None

    def _finish_tick(self, tick):
        if tick.completed:
            tick.clean_scopes.add(RUNTIME_SCOPE)
        if tick.active_loaded:
            self._open_scopes = {
                scope for scope, setup in tick.setups.items()
                if position_possibly_open(setup.get("state"))
            }
        self.latches.finish_tick(tick, self.now(), self._open_scopes)
        self._maintain_latches()

    def _maintain_latches(self):
        """Apply clear rules, record durable halts, then append set/clear events in order."""
        with self._latch_maintenance:
            self._apply_operator_clears()
            self.latches.close_due(
                self.broker_budget.cooldown_remaining() if self.broker_budget is not None else 0
            )
            for latch in self.latches.halts_pending():
                self._record_persistent_halt(latch)
            for _ in range(64):
                item = self.latches.next_event()
                if item is None:
                    break
                try:
                    row = self._write_event(
                        item.kind, {"runtime_id": self.runtime_id, **item.body},
                        key=item.key, setup_id=item.setup_id,
                    )
                except Exception as exc:
                    self._audit_unavailable(exc)  # Retried with the same key and body.
                    break
                self.latches.event_written(item, row)

    def _apply_operator_clears(self):
        floor = self.latches.operator_clear_floor()
        if floor is None:
            return
        try:
            with self.execution.repo.connect() as conn:
                row = conn.execute(
                    """SELECT max(event_seq) AS seq FROM lab.managed_events
                    WHERE setup_id IS NULL AND kind=%s AND event_seq>%s""",
                    (OPERATOR_CLEAR_EVENT, floor),
                ).fetchone()
        except Exception:
            return  # The latch stays set until the operator's event can be read.
        if row and row["seq"] is not None:
            self.latches.operator_clear(row["seq"])

    def _record_persistent_halt(self, latch):
        details = {
            "scope": latch.scope,
            "runtime_id": self.runtime_id,
            "episode": latch.episode,
            "first_failure_at": latch.first_failure_at,
            "code": latch.code,
        }
        if latch.setup_id is not None:
            details["setup_id"] = str(latch.setup_id)
        try:
            with self.execution.store.transaction() as conn:
                self.execution._latch_execution_halt(conn, PERSISTENT, details)
        except Exception:
            return False  # Retried next tick; entries stay blocked by the latch meanwhile.
        with self.lock:
            self.reconciled_at = None
            self.execution.reconciled_at = None
        self.latches.halt_recorded(latch)
        return True

    def _execution_once(self, tick=None):
        tick = tick if tick is not None else TickRecord()
        # Never run Jev here. REST protective recovery and deadlines remain independent.
        if self.executor_lease is not None:
            try:
                self.executor_lease.assert_owned()
            except SubmissionDisabled as exc:
                # Without ownership nothing of this tick may run: end the process instead of
                # looping forever with protection stopped (the supervisor restarts it).
                self._ownership_lost(failure_code(exc))
                return
        if self.account_safety is not None:
            try:
                with request_priority(ACCOUNT):
                    self.account_safety.tick()
            except Exception as exc:
                self._record_failure(tick, "SAFETY", RUNTIME_SCOPE, exc)
            else:
                with self.lock:
                    self.account_safety_healthy = True
                self.latches.note_safety_success()
        # One live-price budget per tick, shared by admission and the crypto trigger version.
        self.live_prices.new_tick()
        self._invalidate_market_gaps(tick)
        self._retire_superseded_research(tick)
        self._run_gap_checks(tick)  # CRYPTO_GAP_RESUME_V1: held setups' bar checks.
        self._refresh_entry_pacing()  # CRYPTO_ENTRY_PACING_V1: the market window's bar seed.
        if self.ready():
            packets = self._selected_packets()
            # CRYPTO_STREAM_CAPACITY_V1: the picks the crypto stream holds back, from this read.
            held_back = set(self._stream_plan("CRYPTO", packets)[1])
            self._capacity_waits &= {p.get("selection_event_seq") for p in packets}
            for packet in packets:
                if packet["market"] == "CRYPTO" and packet["symbol"] in held_back:
                    self._stream_capacity_wait(packet)
                    continue
                if packet["market"] in {"US_STOCKS", "CRYPTO"} and self._market_ready(
                    packet["market"], packet["symbol"]
                ) and self._reference_admission_ready(packet) and self._admission_due(packet):
                    try:
                        if is_v3_packet(packet) and getattr(
                                self.execution, "trade_plan_active", False):
                            # CRYPTO_TRADE_PLAN_V1 also needs the coin's hourly range.
                            self.execution.admit(packet, live_quote=self._live_quote,
                                                 hourly_range=self.hourly_ranges)
                        elif is_v3_packet(packet):  # The system check needs the live price.
                            self.execution.admit(packet, live_quote=self._live_quote)
                        else:
                            self.execution.admit(packet)
                    except Exception as exc:
                        self._admission_refused(packet, exc)
        try:
            with request_priority(ACCOUNT):  # Entry-time reads keep the account class.
                self._process_queued_prints()
        except Exception as exc:
            # A trigger fault never skips the protection of the setups managed below.
            self._record_failure(tick, "TRIGGER", TRIGGER_SCOPE, exc)
        active = self.execution.store.active()
        tick.active_loaded = True
        try:
            with request_priority(ACCOUNT):  # Entry-time reads keep the account class.
                self._evaluate_quote_triggers(tick, active)
        except Exception as exc:
            self._record_failure(tick, "TRIGGER", TRIGGER_SCOPE, exc)
        for setup in active:
            scope = str(setup["setup_id"])
            tick.setups[scope] = setup
            try:
                observation = self.observation(setup)
                self.execution.manage(setup["setup_id"], observation)
            except Exception as exc:
                self._record_failure(tick, "MANAGE", scope, exc, setup_id=setup["setup_id"])
            else:
                tick.clean_scopes.add(scope)
        tick.completed = True
        with self.lock:
            self.last_protection_tick = self.now().isoformat()

    def _process_queued_prints(self):
        """Evaluate every queued print, each in isolation.

        A print whose evaluation raises is recorded as RUNTIME_TRIGGER_FAILURE (setup,
        print and code), its setup is revoked TRIGGER_EVALUATION_FAILED and the print is
        consumed; the next print and every setup's protection still run. Only a failure to
        read the queue, or to record, revoke or consume, escapes to the tick's trigger latch.
        """
        active = {str(s["setup_id"]): s for s in self.execution.store.active()}
        for queued in self._pending_trades():
            try:
                self._process_print(queued, active)
            except Exception as exc:
                self._print_failed(queued, exc)

    def _print_failed(self, queued, exc):
        setup_id, seq = queued["setup_id"], queued["event_seq"]
        self._audit_once(
            "RUNTIME_TRIGGER_FAILURE",
            {
                "runtime_id": self.runtime_id,
                "setup_id": str(setup_id),
                "print_event_seq": seq,
                "code": failure_code(exc),
            },
            f"runtime:{self.runtime_id}:trigger-failure:{seq}",
            setup_id=setup_id,
        )
        self.execution.revoke(setup_id, "TRIGGER_EVALUATION_FAILED")
        self._consume_trade(queued, "TRIGGER_EVALUATION_FAILED")

    def _process_print(self, queued, active):
        setup = active.get(str(queued["setup_id"]))
        if not setup or setup["state"]["state"] != "WATCHING":
            self._consume_trade(queued, "SETUP_NO_LONGER_WATCHING")
            return
        printed = queued["body"]
        admitted_at = setup["state"].get("admitted_at")
        if admitted_at is not None and datetime.fromisoformat(
            printed["trade_at"]
        ) < datetime.fromisoformat(admitted_at):
            self._consume_trade(queued, "PRINT_PRECEDES_ADMISSION")
            return
        if self._gap_pending(setup["setup_id"]):
            # CRYPTO_GAP_RESUME_V1: held for its gap check, which reads this print from the
            # ledger; it is neither evaluated nor aged into a revocation meanwhile.
            self._consume_trade(queued, gap_resume.PRINT_CONSUMED_REASON)
            return
        if coinbase_trigger.active(setup["state"]):
            # CRYPTO_COINBASE_TRIGGER_V1: an Alpaca print is neither a touch nor an invalidation
            # (the trigger reads Coinbase every tick), so it is consumed unevaluated, whatever
            # its age; the gap check still reads it from the ledger.
            self._consume_trade(queued, coinbase_trigger.ALPACA_PRINT_CONSUMED_REASON)
            return
        age = (self.now() - datetime.fromisoformat(printed["trade_at"])).total_seconds()
        if age < 0 or age > 5:
            if age > 5 and stale_print.harmless(setup["state"], setup["record_json"]["levels"],
                                                printed):
                # STALE_PRINT_ABOVE_TRIGGER_V1: a late print strictly above the entry trigger
                # can neither trigger nor invalidate this setup; it keeps watching.
                self._consume_trade(queued, stale_print.CONSUMED_REASON)
                return
            self.execution.revoke(setup["setup_id"], "DATA_FEED_FAILURE")
            self._consume_trade(queued, "PRINT_PROCESSING_DEADLINE")
            return
        if not self.ready() or not self._market_ready(setup["market"], setup["symbol"]):
            return
        current = self.observation(setup)
        # Stop invalidation does not require an executable quote. Other entries need one.
        if Decimal(printed["trade_price"]) <= Decimal(setup["record_json"]["levels"]["stop"]):
            self.execution.observe_trigger(setup["setup_id"], printed)
            self._consume_trade(queued, "STOP_PRINT_EVALUATED")
            return
        if crypto_trigger.active(setup["state"]):
            # CRYPTO_ALPACA_TRIGGER_V1: a print touch is confirmed on a quote fresh by its read
            # time; without one the setup waits (recorded) instead of being revoked.
            self.execution.observe_trigger(
                setup["setup_id"], self._crypto_print_observation(setup, printed)
            )
            self._consume_trade(queued, "TRIGGER_CHECKED")
            return
        if not current or not all(k in current for k in ("bid", "ask", "quote_at")):
            self.execution.revoke(setup["setup_id"], "DATA_FEED_FAILURE")
            self._consume_trade(queued, "QUOTE_UNAVAILABLE_AT_PRINT")
            return
        # The print is immutable; current quote is re-read immediately before authorization.
        observation = {
            **current,
            **{k: printed[k] for k in ("trade_price", "trade_at", "trade_id")},
        }
        self.execution.observe_trigger(setup["setup_id"], observation)
        self._consume_trade(queued, "TRIGGER_CHECKED")

    async def research_once(self):
        if not self.research_healthy:
            return
        await asyncio.gather(self._research_pass(), self._position_pass(),
                             self._day_review_pass())
        with self.lock:
            self.last_research_tick = self.now().isoformat()

    async def probe_once(self):
        """Mirror ReviewWorker.tick's synthetic health-probe step (review_worker.py:164-165)
        so the managed engine also recovers Jev's circuit breaker, not only a standalone
        review worker polling the same scope. Gated on ``research_healthy`` exactly like
        ``ReviewWorker.tick`` gates its probe on its own heartbeat/clock-health result, and
        on ``gate1`` being configured at all. A probe still needs this worker's own
        ``lab.review_worker_status`` row to read RUNNING, which ``heartbeat_once`` maintains
        by calling the same ``reviewer_heartbeat`` this health gate already depends on.
        """
        if not self.research_healthy or self.gate1 is None:
            return
        now = self.now()
        if self.gate1.probe_due(now):
            await self.gate1.probe(self.research.reviewer, now)

    async def _research_pass(self):
        for cycle_id in self._cycle_ids():
            try:
                await self.research.tick(cycle_id)
                self.research.approved_packets(cycle_id)  # Durable publication only.
            except Exception as exc:
                # A faulting cycle is recorded once per code; later cycles still run.
                code = failure_code(exc)
                self._audit_once(
                    "RESEARCH_CYCLE_FAULT",
                    {"cycle_id": str(cycle_id), "code": code},
                    f"research:{cycle_id}:fault:{code}",
                )

    def maintenance_observation(self, setup):
        """The stream row of a maintained trade's symbol with its quote's receipt time."""
        key = ("CRYPTO", setup["symbol"])
        with self.lock:
            row = self.observations.get(key)
            received = self.quote_received.get(key)
        if not row or not self._market_ready(setup["market"], setup["symbol"]):
            return None
        return {**row, "quote_received_at": received}

    async def _maintenance_pass(self, maintained):
        """CRYPTO_MAINTENANCE_V1's reviews; without a maintenance component a maintained trade
        is never reviewed by another version (one POSITION_REVIEW_SKIPPED per lifecycle)."""
        if not maintained:
            return
        if self.maintenance is None:
            for setup in maintained:
                lifecycle = setup["state"].get("lifecycle_id")
                self._audit_once(
                    "POSITION_REVIEW_SKIPPED",
                    {"reason": "MAINTENANCE_NOT_CONFIGURED", "lifecycle_id": lifecycle},
                    f"position-review-skipped:{setup['setup_id']}:{lifecycle}:"
                    "MAINTENANCE_NOT_CONFIGURED",
                    setup_id=setup["setup_id"],
                )
            return
        try:
            await self.maintenance.run_pass(
                maintained, self.maintenance_observation, benchmark=self.benchmark,
                benchmark_ready=self._market_ready("CRYPTO", crypto_maintenance.BENCHMARK_SYMBOL),
            )
        except Exception as exc:
            code = failure_code(exc)
            self._audit_once(
                "POSITION_REVIEW_UNAVAILABLE",
                {"runtime_id": self.runtime_id, "reason": "MAINTENANCE_PASS_FAILED", "code": code},
                f"runtime:{self.runtime_id}:maintenance-failure:{code}",
            )

    async def _day_review_pass(self):
        """CRYPTO_24H_REVIEW_V1's 24-hour reviews and early exits (package day-review), in their
        own periodic task so a Jev call at a review never delays a maintenance review."""
        if self.day_reviews is None:
            return
        active = [s for s in self.execution.store.active() if s["state"]["state"] == "OPEN"]
        try:
            await self.day_reviews.run_pass(active, self.maintenance_observation)
        except Exception as exc:
            code = failure_code(exc)
            self._audit_once(
                "DAY_REVIEW_UNAVAILABLE",
                {"runtime_id": self.runtime_id, "reason": "DAY_REVIEW_PASS_FAILED", "code": code},
                f"runtime:{self.runtime_id}:day-review-failure:{code}",
            )

    async def _position_pass(self):
        if self.monitor_tick is None and self.maintenance is None:
            return
        active = self.execution.store.active()
        # CRYPTO_MAINTENANCE_V1 trades go to the maintenance component only; every other open
        # position keeps today's monitor. Both run concurrently.
        maintained = [s for s in active if s["state"]["state"] == "OPEN"
                      and crypto_maintenance.active(s["state"])]
        others = [s for s in active if s["state"]["state"] == "OPEN"
                  and not crypto_maintenance.active(s["state"])]
        await asyncio.gather(self._maintenance_pass(maintained), self._monitor_pass(others))

    async def _monitor_pass(self, active):
        if self.monitor_tick is not None:
            pending, reviewed = [], []
            for setup in active:
                if setup["state"]["state"] == "OPEN":
                    observation = self.observation(setup)
                    if observation:
                        reviewed.append(setup)
                        pending.append(self.monitor_tick(
                            setup, observation, lambda current=setup: self.observation(current)
                        ))
            results = await asyncio.gather(*pending, return_exceptions=True)
            for setup, result in zip(reviewed, results, strict=True):
                if isinstance(result, Exception):
                    # One event per runtime, setup and code, not one per failing pass.
                    code = failure_code(result)
                    self._audit_once(
                        "POSITION_REVIEW_UNAVAILABLE",
                        {
                            "runtime_id": self.runtime_id,
                            "reason": "MONITOR_TICK_FAILED",
                            "setup_id": str(setup["setup_id"]),
                            "code": code,
                        },
                        f"runtime:{self.runtime_id}:monitor-failure:{setup['setup_id']}:{code}",
                        setup_id=setup["setup_id"],
                    )

    def ingest(self, raw):
        try:
            if self.account_safety is None or not self.account_safety.ingest(raw):
                self.execution.ingest(raw)
        finally:
            if self.broker_budget is not None:
                # A fill or order change makes the shared snapshot stale: refresh early.
                event = raw.get("event") if isinstance(raw, dict) else None
                self.broker_budget.invalidate(
                    "TRADE_UPDATE_FILL" if event in {"fill", "partial_fill"} else "TRADE_UPDATE"
                )

    # Plan 4.7: the heartbeat still runs every ``heartbeat_seconds`` (research health is
    # refreshed each time) but RUNTIME_HEARTBEAT is written only when the status changed,
    # ignoring timestamps and counters, or HEARTBEAT_EVENT_SECONDS after the last one.
    HEARTBEAT_EVENT_SECONDS = 60
    # jev_calls_today is a running count, not a meaningful-change signal (like the
    # timestamps above); jev_breaker is kept stable so an actual state/epoch transition
    # still writes a heartbeat event.
    _HEARTBEAT_VOLATILE = frozenset({
        "last_market_poll", "last_protection_tick", "last_research_tick", "reconciled_at",
        "last_clean_reconciliation_at", "broker_budget", "latches", "jev_calls_today",
        "jev_budget",
    })

    @classmethod
    def _heartbeat_signature(cls, status):
        stable = {k: v for k, v in status.items() if k not in cls._HEARTBEAT_VOLATILE}
        stable["reconciled"] = status.get("reconciled_at") is not None
        stable["latches"] = sorted(
            [latch.get(k) for k in ("cause", "scope", "episode", "category", "state",
                                    "recorded", "durable_halt_pending")]
            for latch in (status.get("latches") or {}).get("active", [])
        )
        budget = status.get("broker_budget") or {}
        stable["rest_cooldown"] = bool(budget.get("cooldown_remaining_seconds"))
        gap = status.get("gap_resume")
        if isinstance(gap, dict):  # Its instant is a timestamp, not a change.
            stable["gap_resume"] = {k: v for k, v in gap.items() if k != "as_of"}
        # A pass's own time is a timestamp too. The day-review pass stamped it every heartbeat,
        # so a RUNTIME_HEARTBEAT was written every 5 s instead of every minute (2026-09-28:
        # about 17,000 a day, the ledger's largest writer).
        for name in ("day_reviews", "trade_maintenance"):
            section = status.get(name)
            if isinstance(section, dict):
                stable[name] = {k: v for k, v in section.items() if k != "last_pass_at"}
        budget = status.get("jev_budget")
        if isinstance(budget, dict):  # Spend moves every call; only the tier is a change.
            stable["jev_budget_tier"] = budget.get("tier")
        feed = status.get("reference_feed")
        if isinstance(feed, dict):  # Ages and the instant move every call; health is a change.
            stable["reference_feed"] = {k: feed.get(k) for k in (
                "available", "connected", "wanted", "requested", "acknowledged", "refused",
                "healthy", "unhealthy", "unhealthy_since")}
        return json.dumps(stable, sort_keys=True, default=str)

    def heartbeat_once(self):
        try:
            healthy = self.reviewer_heartbeat()
        except Exception:
            healthy = False
        with self.lock:
            self.research_healthy = healthy is True
        self._flush_print_summaries()
        status = self.status()
        signature, now = self._heartbeat_signature(status), self.now()
        last = getattr(self, "_heartbeat_written", None)
        if (
            last is not None
            and last[0] == signature
            and 0 <= (now - last[1]).total_seconds() < self.HEARTBEAT_EVENT_SECONDS
        ):
            return
        if self._event("RUNTIME_HEARTBEAT", status):
            self._heartbeat_written = (signature, now)

    @staticmethod
    def _frame(socket, timeout):
        try:
            frame = json.loads(socket.recv(timeout=timeout))
            if not isinstance(frame, dict) or not isinstance(frame.get("data"), dict):
                raise ValueError
            return frame
        except (ValueError, TypeError):
            raise ValueError("INVALID_TRADE_UPDATE_FRAME") from None

    def stream_session(self):
        with self.connector(
            TRADE_STREAM_ENDPOINT,
            proxy=None,
            open_timeout=self.policy.stream_open_timeout_seconds,
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
                        "key": self.credentials.key_id,
                        "secret": self.credentials.secret,
                    }
                )
            )
            frame = self._frame(socket, self.policy.stream_open_timeout_seconds)
            if (
                frame.get("stream") != "authorization"
                or frame["data"].get("status") != "authorized"
            ):
                raise ValueError("TRADE_STREAM_AUTH_FAILED")
            socket.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
            deadline = time.monotonic() + self.policy.stream_open_timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("TRADE_STREAM_SUBSCRIPTION_FAILED")
                frame = self._frame(socket, remaining)
                if frame.get("stream") == "trade_updates":
                    self.ingest(frame["data"])
                    continue
                if frame.get("stream") != "listening" or frame["data"].get("streams") != [
                    "trade_updates"
                ]:
                    raise ValueError("TRADE_STREAM_SUBSCRIPTION_FAILED")
                break
            with self.lock:
                self.reconciled_at = None
                self.execution.reconciled_at = None
                self.connected = True
            # Reconnect invalidates the old ready flag and reconciles before new entries.
            self.reconcile_once()
            self._event("RUNTIME_STREAM_CONNECTED", {"stream": "trade_updates"})
            while not self.stop_event.is_set():
                try:
                    frame = self._frame(socket, self.policy.stream_read_timeout_seconds)
                except TimeoutError:
                    continue
                if frame.get("stream") != "trade_updates":
                    raise ValueError("TRADE_STREAM_SUBSCRIPTION_CHANGED")
                self.ingest(frame["data"])

    def _stream_loop(self):
        backoff = self.policy.reconnect_seconds
        while not self.stop_event.is_set():
            started = _monotonic()
            try:
                self.stream_session()
            except Exception:
                pass  # State is made unready in finally before any audit or retry work.
            finally:
                with self.lock:
                    self.connected = False
                    self.socket = None
                    self.reconciled_at = None
                    self.execution.reconciled_at = None
            self._event("RUNTIME_STREAM_DISCONNECTED", {"reason": "PAPER_STREAM_UNAVAILABLE"})
            backoff = reconnect_wait(backoff, _monotonic() - started, self.policy)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, self.policy.max_reconnect_seconds)

    @staticmethod
    def _market_frame(socket, timeout):
        value = json.loads(socket.recv(timeout=timeout), parse_float=Decimal)
        if not isinstance(value, list) or not value or any(not isinstance(v, dict) for v in value):
            raise ValueError("INVALID_MARKET_STREAM_FRAME")
        error = next((v for v in value if v.get("T") == "error"), None)
        if error is not None:
            raise MarketStreamProviderError(error)
        return value

    def market_stream_session(self, market):
        endpoint = STREAM_ENDPOINTS[self.stock_feed] if market == "US" else CRYPTO_STREAM_ENDPOINT
        with self.connector(
            endpoint,
            proxy=None,
            open_timeout=self.policy.stream_open_timeout_seconds,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
            max_queue=MARKET_STREAM_MAX_QUEUE,
            logger=WIRE_LOG,
        ) as socket:
            with self.lock:
                self.market_sockets[market] = socket
            if self._market_frame(socket, self.policy.stream_open_timeout_seconds) != [
                {"T": "success", "msg": "connected"}
            ]:
                raise ValueError("MARKET_STREAM_CONNECT_FAILED")
            socket.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self.credentials.key_id,
                        "secret": self.credentials.secret,
                    }
                )
            )
            if self._market_frame(socket, self.policy.stream_open_timeout_seconds) != [
                {"T": "success", "msg": "authenticated"}
            ]:
                raise ValueError("MARKET_STREAM_AUTH_FAILED")
            requested, acknowledged, deadline = set(), set(), None
            planned_at = None
            while not self.stop_event.is_set():
                # The plan (two ledger reads) is read at most once per market_poll_seconds while
                # frames arrive, and again after every idle read (2026-09-30). Read after every
                # frame, it capped how fast the stream was read; around US market events the
                # provider cut the lagging connection every 2-5 minutes (WS_CLOSED_NONE_NONE).
                clock = time.monotonic()
                if planned_at is None or not 0 <= clock - planned_at < (
                        self.policy.market_poll_seconds):
                    wanted, planned_at = self._desired_symbols(market), clock
                # One subscription change in flight (2026-09-30): the next change waits for the
                # provider to acknowledge the previous one. Sent while it was unacknowledged,
                # its late answer listed a coin no longer requested, and the session ended
                # (MARKET_STREAM_SUBSCRIPTION_MISMATCH twice at 01:11 UTC, when a research
                # update retired, re-offered and admitted several coins within seconds).
                if wanted != requested and deadline is None:
                    adding, removing = wanted - requested, requested - wanted
                    # CRYPTO_STREAM_CAPACITY_V1: the unsubscribe goes first, so a coin replacing
                    # another at the capacity never asks the provider for one coin more.
                    if removing:
                        socket.send(
                            json.dumps(
                                {
                                    "action": "unsubscribe",
                                    "trades": sorted(removing),
                                    "quotes": sorted(removing),
                                }
                            )
                        )
                    if adding:
                        socket.send(
                            json.dumps(
                                {
                                    "action": "subscribe",
                                    "trades": sorted(adding),
                                    "quotes": sorted(adding),
                                }
                            )
                        )
                    requested = wanted
                    deadline = time.monotonic() + self.policy.stream_open_timeout_seconds
                if deadline is not None and time.monotonic() >= deadline:
                    raise ValueError("MARKET_STREAM_SUBSCRIPTION_TIMEOUT")
                try:
                    frame = self._market_frame(socket, self.policy.stream_read_timeout_seconds)
                except TimeoutError:
                    planned_at = None  # Idle: the next pass reads the plan again.
                    continue
                for message in frame:
                    if message.get("T") == "subscription":
                        trades, quotes = (
                            set(message.get("trades", [])),
                            set(message.get("quotes", [])),
                        )
                        if trades != quotes or not trades.issubset(requested | acknowledged):
                            raise ValueError("MARKET_STREAM_SUBSCRIPTION_MISMATCH")
                        acknowledged = trades
                        with self.lock:
                            self.market_subscriptions[market] = acknowledged
                            self.market_connected[market] = True
                        if acknowledged == requested:
                            deadline = None
                            self.reconcile_once()
                            self._event(
                                "RUNTIME_MARKET_CONNECTED",
                                {
                                    "market": market,
                                    "feed": self.stock_feed if market == "US" else "CRYPTO_US",
                                    "symbols": sorted(acknowledged),
                                },
                            )
                    else:
                        self.market_message(market, message)

    def _market_stream_loop(self, market):
        backoff = self.policy.reconnect_seconds
        while not self.stop_event.is_set():
            if not self._desired_symbols(market):
                self.stop_event.wait(self.policy.market_poll_seconds)
                continue
            reason, code = "MARKET_STREAM_DISCONNECTED_OR_GAP", None
            started = _monotonic()
            try:
                self.market_stream_session(market)
            except Exception as exc:
                # The session still ends (fail-closed); its cause is recorded with the gap, so a
                # malformed message outside the known reasons is no longer only a disconnect.
                # A provider error records the provider's numeric code (ALPACA_STREAM_<n>).
                # A closed connection records its close codes (who closed it, and why).
                code = (exc.code if isinstance(exc, MarketStreamProviderError)
                        else close_codes(exc) or failure_code(exc))
                if str(exc) in {
                    "MARKET_QUEUE_OVERLOAD",
                    "OUT_OF_ORDER_MARKET_TRADE",
                    "OUT_OF_ORDER_MARKET_QUOTE",
                    "MARKET_STREAM_SUBSCRIPTION_MISMATCH",
                    "MARKET_STREAM_SUBSCRIPTION_TIMEOUT",
                    "MARKET_STREAM_PROVIDER_ERROR",
                }:
                    reason = str(exc)
            finally:
                with self.lock:
                    self.market_sockets.pop(market, None)
                self.market_gap(market, reason, code=code)
            backoff = reconnect_wait(backoff, _monotonic() - started, self.policy)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, self.policy.max_reconnect_seconds)

    def strategy_signals_once(self):
        """STRATEGY_PAPER_PATH_V1's signal pass (package plugin-c3); its reads are research
        class (public bars and the broker's asset grid). Signals become selections that the
        protection pass admits through the same admission and risk gate as research picks."""
        if self.strategy_source is None:
            return None
        with request_priority(RESEARCH):
            return self.strategy_source.run_once()

    def _loop(self, callback, interval, name):
        while not self.stop_event.is_set():
            try:
                callback()
            except Exception:
                self._event("RUNTIME_WORKER_FAILURE", {"worker": name})
            self.stop_event.wait(interval)

    def _protection_loop(self):
        """The protection pass every ``execution_tick_seconds``, and at once after a print is
        queued (package pass-speed, 2026-09-28). The pass evaluates queued prints, and a print
        that waited out the sleep could pass the five-second deadline only because of it. A
        print queued during a pass starts the next pass immediately."""
        while not self.stop_event.is_set():
            self.print_queued.clear()
            try:
                self.execution_once()
            except Exception:
                self._event("RUNTIME_WORKER_FAILURE", {"worker": "protection"})
            self.print_queued.wait(self.policy.execution_tick_seconds)

    def _research_loop(self):
        async def periodic(callback, name):
            while not self.stop_event.is_set():
                try:
                    if self.research_healthy:
                        await callback()
                        if name == "research":
                            self.last_research_tick = self.now().isoformat()
                except Exception:
                    self._event("RUNTIME_WORKER_FAILURE", {"worker": name})
                await asyncio.to_thread(self.stop_event.wait, self.policy.research_poll_seconds)

        async def research_tick():
            # Same order as ReviewWorker.tick: a due probe runs before this tick's shortlist
            # work, so a tripped breaker gets a chance to recover before the next selection.
            await self.probe_once()
            await self._research_pass()

        async def run():
            # Independent periodic tasks: a slow shortlist does not delay the next
            # near-target position check after the current position review completes.
            await asyncio.gather(periodic(research_tick, "research"),
                                 periodic(self._position_pass, "position_review"),
                                 periodic(self._day_review_pass, "day_review"))

        asyncio.run(run())

    def acquire_ownership(self):
        if self.executor_lease is not None:
            self.executor_lease.acquire()
            self._refuse_a_retired_ledger()

    def _refuse_a_retired_ledger(self):
        """A Mac executor re-reads its ledger's marker once it holds the lease (package
        cloud-hardening). ``ledger-retire`` holds the same lock while it writes the retirement,
        so an app that passed the launcher's check before it either cannot take the lease or
        sees the retirement here, releases the lease and stops. An unreadable marker fails
        closed."""
        if self.ledger_marker is None:
            return
        from catalyst_lab.localdb import ledger_retirement

        try:
            retired = ledger_retirement(self.ledger_marker.parent) is not None
        except RuntimeError:
            retired = True
        if retired:
            self.executor_lease.close()
            raise SubmissionDisabled("LEDGER_RETIRED")

    def _record_selection_rule(self):
        """Audit an owner-activated selection rule (B1 or B2) before any report is accepted.

        A failed write leaves the rule unactivated, so its new reports are refused and
        admission SQL refuses its packets; protection of open positions is never stopped by it.
        """
        record = getattr(self.research, "record_selection_rule", None)
        if record is None:
            return
        try:
            record(runtime_id=self.runtime_id)
        except Exception as exc:
            self._audit_unavailable(exc)

    def start(self):
        if self.threads:
            raise RuntimeError("RUNTIME_ALREADY_STARTED")
        self.acquire_ownership()
        for market in ("US", "CRYPTO"):
            self.market_gap(market, "RUNTIME_RESTART_REQUIRES_FRESH_OBSERVATION")
        self._invalidate_market_gaps()
        self.reconcile_once()
        self._event(
            "RUNTIME_STARTED",
            {
                "profile": self.policy.profile,
                "position_jev_configured": self.management_reviews_active(),
                "management_reviews": self.management_reviews,
            },
        )
        self._record_selection_rule()
        targets = [
            (self._stream_loop, ()),
            (self._research_loop, ()),
            (self._protection_loop, ()),
            (self._market_stream_loop, ("US",)),
            (self._market_stream_loop, ("CRYPTO",)),
            (self._loop, (self.reconcile_once, self.policy.reconcile_seconds, "reconciliation")),
            (self._loop, (self.heartbeat_once, self.policy.heartbeat_seconds, "heartbeat")),
            # Fees have no push/stream source (package fees-net-r); reuse the existing
            # reconciliation cadence rather than adding a new policy field for them.
            (self._loop, (self.fee_import_once, self.policy.reconcile_seconds, "fee_import")),
        ]
        if callable(getattr(self.reference_feed, "session", None)):
            # Coinbase's public feed (CRYPTO_COINBASE_TRIGGER_V1 / CRYPTO_STOP_BREACH_V3).
            targets.append((self._reference_stream_loop, ()))
        if self.strategy_source is not None:
            # STRATEGY_PAPER_PATH_V1: the promoted strategies' signals, checked every minute
            # (each completed hour is scanned once).
            targets.append((self._loop, (self.strategy_signals_once, STRATEGY_SIGNAL_SECONDS,
                                         "strategy_signals")))
        self.expected_workers = len(targets)
        for target, args in targets:
            thread = threading.Thread(target=target, args=args, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        self.print_queued.set()  # The protection loop is not left waiting out its interval.
        with self.lock:
            socket = self.socket
            self.connected = False
            self.reconciled_at = None
        if socket is not None:
            socket.close()
        with self.lock:
            market_sockets = list(self.market_sockets.values())
        for market_socket in market_sockets:
            market_socket.close()
        if callable(getattr(self.reference_feed, "close", None)):
            self.reference_feed.close()  # Coinbase's public connection, when open.
        for thread in self.threads:
            thread.join(timeout=self.policy.shutdown_timeout_seconds)
        self._flush_print_summaries(force=True)  # The last partial minute of quiet prints.
        self._event(
            "RUNTIME_STOPPED",
            {
                "workers_stopped": all(not t.is_alive() for t in self.threads),
            },
        )
        if self.executor_lease is not None and all(not t.is_alive() for t in self.threads):
            self.executor_lease.close()


class PositionMonitorLoop:
    """Second Jev role with a separate read-only bar connection and minute coalescing."""

    def __init__(self, monitor, source, scan_policy, *, clock):
        self.monitor, self.source, self.scan_policy, self.now = monitor, source, scan_policy, clock
        self._bar_cache = {}

    async def __call__(self, setup, observation, fresh_observation):
        # Staging switch DISABLED or an ENGINEERING_TEST setup (plan 0.10): record the skip
        # once and fetch no bars, since nothing will be sent to Jev.
        withheld = getattr(self.monitor, "review_withheld", None)
        if withheld is not None and withheld(setup["setup_id"]):
            return None
        market = "US" if setup["market"] == "US_STOCKS" else setup["market"]
        now = self.now()
        minute = now.replace(second=0, microsecond=0)
        key = (market, setup["symbol"], minute)
        if key not in self._bar_cache:
            bars, issues = await asyncio.to_thread(
                self.source.completed_bars, market, setup["symbol"], self.scan_policy
            )
            if issues:
                self.monitor.execution._event(
                    "POSITION_REVIEW_UNAVAILABLE",
                    {
                        "reason": "COMPLETED_BARS_UNAVAILABLE",
                        "codes": [i.code for i in issues],
                    },
                    setup["setup_id"],
                )
                return None
            self._bar_cache = {k: v for k, v in self._bar_cache.items() if k[2] == minute}
            self._bar_cache[key] = bars
            # Persist every bar a review can use, so every option-backing bar is in the
            # ledger; the content key lets the monitor reference this exact row.
            from catalyst_lab.position_monitor import review_bar_window, review_bars_event

            body, bars_key = review_bars_event(
                review_bar_window(bars, bars, self.monitor.policy), setup["setup_id"]
            )
            self.monitor.execution._event(
                "POSITION_REVIEW_BARS", body, setup["setup_id"], bars_key
            )
        fresh = fresh_observation()
        if not fresh:
            self.monitor.execution._event(
                "POSITION_REVIEW_UNAVAILABLE",
                {
                    "reason": "CURRENT_QUOTE_UNAVAILABLE",
                },
                setup["setup_id"],
            )
            return None
        return await self.monitor.review(
            setup["setup_id"], fresh, self._bar_cache[key], fresh_observation=fresh_observation,
            structural_bars=self._bar_cache[key]
        )


def engineering_monitor_policy():
    """The one accepted position-review profile: V3, 11,000-byte state budget."""
    from catalyst_lab.managed_review import CONTEXT_VERSION, STATE_BYTE_BUDGET, ManagedPolicy

    return ManagedPolicy(
        5, 60, 5, 20, 4, max_structural_bars=64, max_history=4,
        context_version=CONTEXT_VERSION, state_byte_budget=STATE_BYTE_BUDGET,
    )


def build_runtime_from_env(*, monitor_tick=None):
    """Explicit launch configuration only; no schema install or credential migration."""
    from catalyst_lab.authorization import AuthorizationGate, RiskRepository
    from catalyst_lab.broker_budget import BrokerBudget
    from catalyst_lab.crypto_execution import CryptoDayPolicy
    from catalyst_lab.crypto_liquidity import CryptoLiquidityPolicy, CryptoLiquidityReader
    from catalyst_lab.executor_lease import AccountExecutorLease, FencedAuthorizationGate
    from catalyst_lab.jev_budget import SpendConfig, SpendGuard
    from catalyst_lab.managed_account_safety import ManagedAccountSafety
    from catalyst_lab.managed_broker import ManagedPaperBroker
    from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
    from catalyst_lab.managed_review import ManagedPolicy
    from catalyst_lab.managed_store import ManagedAuthorizationGate
    from catalyst_lab.paper_execution import RiskAuthorizedPaperClient
    from catalyst_lab.position_monitor import MonitorTriggerPolicy, PositionMonitor
    from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
    from catalyst_lab.research_selection_topk import selection_rule_from_env
    from catalyst_lab.review_config import Gate1Inputs
    from catalyst_lab.review_worker import ReviewWorker, WorkerSettings
    from catalyst_lab.scan_sources import AlpacaMarketSource, BarContextWindow, SourcePolicy
    from catalyst_lab.trade_maintenance import TradeMaintenance
    from catalyst_lab.trade_review import DayReviews

    # AI_MODE_SETTING_V1 (package oss-packaging): absent is JEV_AI_MODE_V1, the reference
    # deployment, unchanged; NO_AI_MODE_V1 must be fully configured (its own refusal code).
    mode = ai_mode.require(os.environ)
    try:
        if os.environ["MANAGED_ENVIRONMENT"] not in {"local_test", "supervised_paper"}:
            raise ValueError
        policy = RuntimePolicy(**json.loads(os.environ["MANAGED_RUNTIME_POLICY_JSON"]))
        source_policy = SourcePolicy(**json.loads(os.environ["MANAGED_SOURCE_POLICY_JSON"]))
        cycle_policy = CyclePolicy(**json.loads(os.environ["MANAGED_CYCLE_POLICY_JSON"]))
        # Owner switch: V2 unless MANAGED_SELECTION_RULE names B1 or B2 with its required floor,
        # or JEV_TOP_K_SELECTION_V1 / _V2 with no floor and K from MANAGED_TOPK_SELECTION_JSON.
        selection_rule = selection_rule_from_env(os.environ)
        gate1 = Gate1Inputs(json.loads(os.environ["JEV_REVIEW_POLICY_JSON"]))
        monitor_policy = ManagedPolicy(**json.loads(os.environ["MANAGED_POSITION_POLICY_JSON"]))
        monitor_seconds = int(os.environ["MANAGED_POSITION_REVIEW_SECONDS"])
        crypto_day_policy = CryptoDayPolicy(**json.loads(os.environ[
            "MANAGED_CRYPTO_DAY_POLICY_JSON"
        ])) if os.environ.get("MANAGED_CRYPTO_DAY_POLICY_JSON") else None
        trigger_raw = json.loads(os.environ["MANAGED_MONITOR_TRIGGER_POLICY_JSON"]) \
            if os.environ.get("MANAGED_MONITOR_TRIGGER_POLICY_JSON") else None
        if trigger_raw is not None and trigger_raw.get("near_target_progress_fraction") is not None:
            trigger_raw["near_target_progress_fraction"] = Decimal(
                str(trigger_raw["near_target_progress_fraction"])
            )
        trigger_policy = MonitorTriggerPolicy(**trigger_raw) if trigger_raw is not None else None
        liquidity_raw = json.loads(os.environ["MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON"]) \
            if os.environ.get("MANAGED_CRYPTO_LIQUIDITY_POLICY_JSON") else None
        if liquidity_raw is not None:
            for key in ("minimum_dollar_volume", "maximum_participation",
                        "assumed_round_trip_cost_bps", "maximum_cost_to_risk"):
                liquidity_raw[key] = Decimal(str(liquidity_raw[key]))
        liquidity_policy = CryptoLiquidityPolicy(**liquidity_raw) if liquidity_raw else None
        # MANAGED_CRYPTO_WINDOW_JSON (package review-window): absent admits the 24-hour
        # versions exactly as before; present, it is parsed strictly and new report-V3 crypto
        # admissions record CRYPTO_WINDOW_REVIEW_V1 / CRYPTO_WINDOW_HOLD_V1 with its window.
        crypto_window = crypto_holding.window_setting_from_env(os.environ)
        # MANAGED_CRYPTO_EXECUTION_JSON (package exec-d): absent, neither phase-D execution
        # version is admitted (CRYPTO_STOP_EXECUTION_V1, CRYPTO_MAKER_ENTRY_V1).
        crypto_execution = execution_setting.from_env(os.environ)
        # MANAGED_STRATEGIES_JSON (package plugin-c3, STRATEGY_PAPER_PATH_V1): absent or empty,
        # no strategy signal is sought or admitted (the default).
        paper_strategies = strategy_paper.configured_strategies(os.environ)
        if monitor_policy != engineering_monitor_policy() or monitor_seconds != 10:
            raise ValueError
        # Staging switch (plan 0.10 / R4): required, exactly ENABLED or DISABLED, no default.
        management_reviews = os.environ["MANAGED_MANAGEMENT_REVIEWS"]
        if management_reviews not in MANAGEMENT_REVIEWS_SETTINGS:
            raise ValueError
        # JEV_SPEND_GUARD_V1 (package jev-budget): the monthly Jev budget is required whenever
        # Jev reviews open trades (CRYPTO_MAINTENANCE_V3 trades wait without it) and in railway
        # mode (cloud_config refuses it there by name first); price and bytes per token default.
        spend_config = SpendConfig.from_env(
            os.environ, required=management_reviews == MANAGEMENT_REVIEWS_ENABLED
            or os.environ.get("CATALYST_ENVIRONMENT") == "railway")
        # Names a MANAGED row of lab.account_risk_policies; ManagedExecution loads and checks it.
        risk_policy_id = os.environ["MANAGED_RISK_POLICY_ID"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", risk_policy_id):
            raise ValueError
        worker_settings = WorkerSettings(
            os.environ["JEV_WORKER_DATABASE_URL"],
            gate1,
            os.environ["JEV_CREDENTIAL_SLOT"],
            int(os.environ["JEV_WORKER_MAX_INFLIGHT"]),
            float(os.environ["JEV_WORKER_POLL_SECONDS"]),
        )
        database_url = os.environ["MANAGED_DATABASE_URL"]
        if not database_url:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("REQUIRED_MANAGED_CONFIGURATION_MISSING_OR_INVALID") from None

    def now():
        return datetime.now(UTC)

    credentials = AlpacaCredentials.from_env()
    # Reused connections (package pass-speed): the executor's passes no longer pay a new TLS
    # connection per query. The lease still holds its own connection for its session locks.
    repository = RiskRepository(database_url, reuse_connections=True)
    repository.check_role()
    # ReviewWorker uses the approved .env/Keychain/runtime secret loader and narrow Jev DB role.
    worker = ReviewWorker(worker_settings)
    repository.require_same_database(worker.store)
    # One request budget per paper account, below both clients' risk gates. It budgets
    # reads only; every mutation still needs its exact one-use authorization.
    broker_budget = BrokerBudget(clock=now)
    lease = AccountExecutorLease(repository, lambda: broker.account_identity())
    broker = ManagedPaperBroker(credentials, FencedAuthorizationGate(
        ManagedAuthorizationGate(repository), lease
    ), feed=source_policy.stock_feed, transport=broker_budget.transport())
    legacy_client = RiskAuthorizedPaperClient(credentials, FencedAuthorizationGate(
        AuthorizationGate(repository), lease
    ), feed=source_policy.stock_feed, transport=broker_budget.transport())
    source = AlpacaMarketSource(credentials, source_policy, now)
    liquidity_source = (
        AlpacaMarketSource(credentials, source_policy, now) if liquidity_policy else None
    )
    # Coinbase's public market data (no key, no credential; read-only): the reference market of
    # CRYPTO_COINBASE_TRIGGER_V1 and CRYPTO_STOP_BREACH_V3 (owner approval 2026-09-29).
    reference_feed = coinbase_feed.CoinbaseFeed(clock=now)
    execution = ManagedExecution(
        repository,
        broker_budget.wrap(broker),
        policy=engineering_execution_policy(),
        clock=now,
        review_store=worker.store,
        crypto_day_policy=crypto_day_policy,
        crypto_liquidity_reader=CryptoLiquidityReader(liquidity_source, liquidity_policy, clock=now)
        if liquidity_policy else None,
        risk_policy_id=risk_policy_id,
        crypto_window=crypto_window,
        reference_feed=reference_feed,
        # CRYPTO_ENTRY_PACING_V1 (owner approval 2026-10-02, package risk-pacing): new crypto
        # admissions are paced; the stream feeds its market window, the calendar is the file's.
        entry_pacing=regime_gate.EntryPacing(),
        crypto_execution=crypto_execution,
        paper_strategies=paper_strategies,
        ai_mode=mode,
    )
    # JEV_TOP_K_SELECTION_V3 (package jev-b2): its comparative review's code facts, read through
    # Alpaca's keyless public bars (read-only) and the engine's entry-pacing state.
    research = ResearchCycle(
        repository, worker.reviewer, cycle_policy, clock=now, selection=selection_rule,
        comparison_facts=ComparisonFacts(repository, PublicCryptoBarReader(),
                                         pacing=getattr(execution, "entry_pacing", None)),
    )
    monitor_source = AlpacaMarketSource(credentials, source_policy, now)
    position_monitor = PositionMonitor(
        execution, worker.reviewer, policy=monitor_policy,
        review_seconds=monitor_seconds, clock=now,
        management_reviews=management_reviews,
        trigger_policy=trigger_policy,
    )
    configured_monitor = monitor_tick or PositionMonitorLoop(
        position_monitor, monitor_source, BarContextWindow(60, 60), clock=now
    )
    # JEV_SPEND_GUARD_V1 (package jev-budget): the meter reads the Jev tables through the risk
    # role and records tier changes in the managed ledger; V3 trades take their cadence from it.
    spend_guard = SpendGuard(spend_config, execution.store, clock=now) if spend_config else None
    # CRYPTO_MAINTENANCE_V1 (package maintenance): maintained trades read 15-minute and 1-hour
    # bars (and a bounded REST quote) through the same read-only position source; V2 (package
    # answer-rules) also the last 60 1-minute bars. A pass's Jev calls share the research
    # cycle's own in-flight limit.
    maintenance = TradeMaintenance(
        execution, worker.reviewer, bars=monitor_source, clock=now,
        management_reviews=management_reviews, max_inflight=cycle_policy.max_inflight,
        spend_guard=spend_guard,
    )
    # CRYPTO_24H_REVIEW_V1 (package day-review): the same bars; the app sets its agents.
    day_reviews = DayReviews(
        execution, worker.reviewer, bars=monitor_source, clock=now,
        management_reviews=management_reviews,
    )
    runtime = ManagedRuntime(
        execution,
        research,
        source,
        credentials,
        policy,
        clock=now,
        reviewer_heartbeat=worker.heartbeat,
        monitor_tick=configured_monitor,
        executor_lease=lease,
        account_safety=ManagedAccountSafety(execution, legacy_client),
        broker_budget=broker_budget,
        management_reviews=management_reviews,
        gate1=worker.runtime,
        maintenance=maintenance,
        day_reviews=day_reviews,
        ledger_marker=os.environ.get("CATALYST_LEDGER_MARKER") or None,
        spend_guard=spend_guard,
    )
    from catalyst_lab.jev_contract import digest, encoded
    from catalyst_lab.managed_ops import code_version
    from catalyst_lab.repository import json_safe
    runtime.configuration_hash = digest(encoded(json_safe({
        "scheduler": asdict(policy), "source": asdict(source_policy),
        "cycle": asdict(cycle_policy), "monitor": asdict(monitor_policy),
        "selection_rule": asdict(selection_rule),
        "monitor_trigger": asdict(trigger_policy) if trigger_policy else None,
        "execution": asdict(engineering_execution_policy()),
        "risk_policy_id": risk_policy_id,
        "management_reviews": management_reviews,
        # The versions admission records (package answer-rules: V2 of both; package
        # jev-budget: maintenance V3; package review-window: the window review and its window
        # while MANAGED_CRYPTO_WINDOW_JSON is set) and the monthly Jev budget's guard.
        "maintenance": crypto_maintenance.ADMITTED_MAINTENANCE.record(),
        "jev_spend_guard": jev_budget.guard_summary(spend_config),
        "day_review": crypto_holding.admission_policy(
            True, crypto_holding.JEV_MANAGED_ARM, crypto_window).record(),
        "early_exit": day_review.EARLY_EXIT_AGREEMENT.record(),
        # The Coinbase reference versions admission records, and the feed they read.
        "reference": {
            "trigger": coinbase_trigger.COINBASE_TRIGGER_VERSION,
            "stop_breach": stop_breach.STOP_BREACH_VERSION_V3,
            "provider": coinbase_feed.PROVIDER, "endpoint": coinbase_feed.ENDPOINT,
            "channels": list(coinbase_feed.CHANNELS),
            "products": sorted(coinbase_feed.COINBASE_USD_PRODUCTS),
            "products_verified_on": coinbase_feed.PRODUCTS_VERIFIED_ON,
            "heartbeat_max_age_seconds": coinbase_feed.HEARTBEAT_MAX_AGE_SECONDS,
            "clock_tolerance_seconds": coinbase_feed.CLOCK_TOLERANCE_SECONDS,
            "tape_gap_hold_seconds": coinbase_feed.TAPE_GAP_HOLD_SECONDS,
        },
        "crypto_day": asdict(crypto_day_policy) if crypto_day_policy else None,
        # Package exec-d: the execution versions admission records (both null: off).
        "crypto_execution": crypto_execution.record(),
        "crypto_liquidity": asdict(liquidity_policy) if liquidity_policy else None,
        # Package plugin-c3: only when a strategy is configured (otherwise the hash is as before).
        **({"paper_strategies": {"version": strategy_paper.PATH_VERSION,
                                 "strategies": list(paper_strategies)}}
           if paper_strategies else {}),
        # Package oss-packaging: only in NO_AI_MODE_V1 (the reference deployment's hash is as
        # before).
        **({"ai_mode": ai_mode.record(mode)} if mode == ai_mode.NO_AI_MODE else {}),
        "review_reliability": gate1.values,
        "broker_budget": asdict(broker_budget.policy),
        "latches": asdict(runtime.latch_policy),
    })))
    # The imported package, not a checkout: a release reports its commit from release.json.
    identity = code_version()
    runtime.code_version = identity["source_sha256"]
    runtime.release_commit = identity["release_commit"]
    runtime.position_monitor = position_monitor
    runtime.trade_maintenance = maintenance
    runtime.ai_mode = mode
    if paper_strategies:
        # STRATEGY_PAPER_PATH_V1: drop-in plug-ins join the registry, then the promoted ones'
        # signals are read from Alpaca's keyless public bars (the shadow's source).
        strategies.ensure_plugins_loaded()
        strategy_reader = PublicCryptoBarReader()
        runtime.strategy_source = strategy_paper.StrategySignalSource(
            execution.store, strategy_reader, paper_strategies, now,
            execution._crypto_price_increment,
            # NO_AI_MODE_V1 records no research universe (no report is taken): its strategies
            # scan the fixed Alpaca USD pairs of ALPACA_CRYPTO_SECTORS_V1 instead.
            universe=ai_mode.strategy_universe if mode == ai_mode.NO_AI_MODE else None)
    runtime.owned_resources = (source, monitor_source, broker, legacy_client) + (
        (liquidity_source,) if liquidity_source is not None else ()
    ) + ((runtime.strategy_source.reader,) if runtime.strategy_source is not None else ())
    return runtime


def _hard_exit(code):
    os._exit(code)  # Replaced in tests; never reached while a graceful shutdown completes.


def install_fatal_exit(runtime, request_shutdown=None, *, grace_seconds=None):
    """Wire a terminal runtime failure to process exit (plan phase 0, 2026-09-26).

    ``request_shutdown`` starts the entry point's graceful shutdown (the uvicorn server's
    ``should_exit`` for the app); the entry point then exits with ``runtime.exit_code``. A
    daemon timer ends the process with the same status if that shutdown has not finished
    within ``grace_seconds`` (FATAL_EXIT_GRACE_SECONDS), so a hung shutdown can never
    leave a live process whose protection loop has stopped.
    """
    grace = FATAL_EXIT_GRACE_SECONDS if grace_seconds is None else grace_seconds

    def on_fatal(code):
        if request_shutdown is not None:
            request_shutdown()
        timer = threading.Timer(grace, lambda: _hard_exit(code))
        timer.daemon = True
        timer.start()
        runtime.fatal_exit_timer = timer

    runtime.on_fatal = on_fatal
    return on_fatal


def main():
    try:
        runtime = build_runtime_from_env()
    except Exception:
        raise SystemExit("MANAGED_RUNTIME_STARTUP_FAILED") from None

    def shutdown(*_):
        runtime.stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, shutdown)
    install_fatal_exit(runtime)  # A lost lease sets the stop event this function waits on.
    try:
        runtime.start()
        runtime.stop_event.wait()
    finally:
        runtime.stop()
        for resource in runtime.owned_resources:
            resource.close()
    if runtime.exit_code is not None:
        raise SystemExit(runtime.exit_code)


if __name__ == "__main__":
    main()
