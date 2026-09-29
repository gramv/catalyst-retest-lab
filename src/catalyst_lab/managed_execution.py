"""Opt-in shared-account paper controller for reviewed stock and crypto setups.

Model decisions are inputs. This controller alone computes risk and records an
exact five-second decision before a transport may mutate the paper broker.
"""

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN
from decimal import Decimal as D
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab import crypto_holding, crypto_maintenance, crypto_trigger, gap_resume, stale_print
from catalyst_lab.account_risk import (
    ARM_METHOD,
    CAPACITY_REASONS,
    LEGACY_MANAGED_POLICY_ID,
    VENUE,
    account_risk_failure,
    assign_arm,
    binding_constraint,
    classification_known,
    load_policy,
    slice_size,
)
from catalyst_lab.broker_budget import RESEARCH, BudgetedBroker, request_priority
from catalyst_lab.broker_ledger import (
    ENTRY_NOT_FILLED,
    PROTECTED,
    TRANSITION_GRACE_SECONDS,
    TRANSITIONING,
    classify_bracket,
    within_transition_grace,
)
from catalyst_lab.crypto_execution import (
    CryptoAsset,
    CryptoDayPolicy,
    CryptoExecutionError,
    build_limit_entry,
    native_stop_levels,
    off_grid_levels,
)
from catalyst_lab.crypto_holding import (
    CRYPTO_24H_HOLD,
    HOLD_EXIT_REASON,
    REVIEW_DEADLINE_EXIT_REASON,
    recorded_hold_policy,
    time_exit_reason,
)
from catalyst_lab.execution import SubmissionDisabled, system_event
from catalyst_lab.execution import halt as persist_execution_halt
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.managed_eligibility import validate_managed_eligibility
from catalyst_lab.managed_engineering import ENGINEERING_SELECTION_POLICY
from catalyst_lab.managed_store import COHORT, POLICY, TERMINAL, ManagedStore
from catalyst_lab.market import NY
from catalyst_lab.paper_execution import BrokerMutationRejected, BrokerMutationUnknown
from catalyst_lab.repository import json_safe
from catalyst_lab.risk_math import broker_amount, buying_power_check
from catalyst_lab.system_check import (
    LIVE_PRICE_UNAVAILABLE,
    REFUSED,
    REFUSED_EVENT,
    REFUSED_KEY,
    SUPERSEDED_BY_NEW_RESEARCH,
    SUPERSESSION_VERSION,
    AdmissionRefused,
    LivePriceUnavailable,
    evaluate,
    is_v3_packet,
    newest_v3_run_slot,
    v3_facts,
)
from catalyst_lab.us_admission import admission_profile

LEDGER_BINDING_VERSION = "LEDGER_ACCOUNT_BINDING_V1"
# Margin evidence copied into every entry decision; never the account identifier.
MARGIN_FIELDS = (
    "multiplier", "buying_power", "regt_buying_power", "non_marginable_buying_power",
    "initial_margin", "maintenance_margin", "shorting_enabled", "crypto_status", "cash",
)
UNEVALUABLE = "UNEVALUABLE:"
BROKER_TERMINAL = frozenset({"filled", "canceled", "cancelled", "expired", "rejected", "replaced"})
# REST backfill: a crypto position below its fill-explained quantity is explained by an in-kind
# fee only up to 1% of the quantity bought; Alpaca's largest crypto fee is well under that.
CRYPTO_IN_KIND_FEE_TOLERANCE = D("0.01")
_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
# Fee backfill (package fees-net-r, plan phase 0): fees have no push/stream source, so the
# window is anchored to the last completed run, not a broker-evidence gap.
FEE_BACKFILL_LOOKBACK = timedelta(hours=1)
FEE_BACKFILL_EVENT = "ALPACA_FEE_BACKFILL_COMPLETED"
# A run whose counts match the last recorded run's is recorded only once this much time has
# passed since it (2026-09-28: every 30 s run was recorded, 20% of the ledger's managed events).
FEE_BACKFILL_REFRESH = timedelta(minutes=30)
FEE_BACKFILL_COUNTS = ("activities_read", "matched", "already_recorded", "unmatched",
                       "invalid", "conflicting")

# Refused closes (plan phase 0, 2026-09-26), both markets, in or out of an operator flatten.
# After the n-th consecutive broker refusal of a setup's close (market sell), its next close
# may be authorized no earlier than min(BASE * 2**(n-1), CAP) seconds after that refusal:
# 1, 2, 4, 8, 16, 32, then every 60 s. The refusal records one new state revision, so the
# retry gets a fresh client order ID, and the retry needs its own one-use five-second
# authorization. The THRESHOLD-th consecutive refusal of a working setup records a durable
# EXIT_REFUSAL_ALARM, and the runtime status reports the setup until a close is accepted.
EXIT_RETRY_BASE_SECONDS = 1
EXIT_RETRY_CAP_SECONDS = 60
EXIT_REFUSAL_ALARM_THRESHOLD = 5


def error_code(exc):
    """The exception's own UPPER_SNAKE code, else its class name; free text is never kept."""
    text = str(exc)
    return text if len(text) <= 80 and _CODE.fullmatch(text) else type(exc).__name__


def exit_retry_delay(refusals):
    """Seconds between the ``refusals``-th consecutive refused close and the next close."""
    if type(refusals) is not int or refusals < 1:
        raise ValueError("EXIT_REFUSAL_COUNT_REQUIRED")
    return min(EXIT_RETRY_BASE_SECONDS * 2 ** min(refusals - 1, 16), EXIT_RETRY_CAP_SECONDS)


@dataclass(frozen=True)
class ExecutionPolicy:
    policy_id: str
    quote_seconds: int
    spread_bps: D
    crypto_max_hold_seconds: int
    reconcile_seconds: int

    def __post_init__(self):
        if (
            self.policy_id != POLICY
            or self.quote_seconds != 5
            or self.spread_bps != D(10)
            or not 1 <= self.crypto_max_hold_seconds <= 86400
            or not 30 <= self.reconcile_seconds <= 60
        ):
            raise ValueError("EXPLICIT_ENGINEERING_EXECUTION_POLICY_REQUIRED")


def engineering_execution_policy():
    return ExecutionPolicy(POLICY, 5, D(10), 86400, 30)


def num(v):
    result = D(str(v))
    if not result.is_finite():
        raise ValueError("NONFINITE_MARKET_NUMBER")
    return result


def normalized_symbol(value):
    return value.replace("/", "")


def explicit_fill_fee_usd(payload):
    """Only event-level, explicitly USD fees; missing/non-USD amounts stay unknown.

    An order's cumulative fee is not an incremental execution fee. Keep the raw
    sanitized broker event as evidence; subsequent corrections are append-only.
    """
    body = payload.get("data", payload) if isinstance(payload, dict) else {}
    candidates = []
    if "fee_usd" in body:
        if body.get("fee_currency", "USD") != "USD":
            return None
        candidates.append(body["fee_usd"])
    if "fee" in body and body.get("fee_currency") == "USD":
        candidates.append(body["fee"])
    if not candidates:
        return None
    try:
        if any(isinstance(value, (bool, float)) or value is None for value in candidates):
            return None
        fees = [num(value) for value in candidates]
        if any(value < 0 or value != fees[0] for value in fees):
            return None
        return fees[0]
    except (ValueError, TypeError, ArithmeticError):
        return None


class ManagedExecution:
    def __init__(self, repository, broker, *, policy, clock, review_store, crypto_day_policy=None,
                 crypto_liquidity_reader=None, risk_policy_id=LEGACY_MANAGED_POLICY_ID,
                 crypto_window=None):
        if crypto_day_policy is not None and not isinstance(crypto_day_policy, CryptoDayPolicy):
            raise ValueError("EXPLICIT_CRYPTO_DAY_POLICY_REQUIRED")
        if crypto_window is not None and not isinstance(
                crypto_window, crypto_holding.CryptoWindowSetting):
            raise ValueError("EXPLICIT_CRYPTO_WINDOW_REQUIRED")
        self.store = ManagedStore(repository)
        self.repo, self.broker, self.policy, self.now = repository, broker, policy, clock
        self.review_store = review_store
        self.reconciled_at = None
        self.process_run_id = uuid4()
        self.crypto_day_policy = crypto_day_policy
        self.crypto_liquidity_reader = crypto_liquidity_reader
        # MANAGED_CRYPTO_WINDOW_JSON (package review-window): None admits the 24-hour versions
        # exactly as before; a setting admits CRYPTO_WINDOW_REVIEW_V1 / CRYPTO_WINDOW_HOLD_V1
        # with its window. Each setup keeps what it recorded.
        self.crypto_window = crypto_window
        # New admissions use this MANAGED policy row; each setup keeps the one it was admitted
        # under (legacy setups without one are MUSE_JEV_MANAGED_TEST_V1).
        self._account_hash = None
        with repository.connect() as conn:
            self.risk_policy = load_policy(conn, risk_policy_id, engine="MANAGED")
            bound = conn.execute(
                "SELECT account_hash FROM lab.ledger_account_binding WHERE venue=%s", (VENUE,)
            ).fetchone()
        self._risk_policies = {self.risk_policy.policy_id: self.risk_policy}
        if bound and bound["account_hash"] != self._account_binding_hash():
            # A ledger bound to one paper account never starts against another.
            raise ValueError("LEDGER_ACCOUNT_BINDING_MISMATCH")

    def _account_binding_hash(self):
        """Domain-separated hash of the broker's opaque account identity, read once."""
        if self._account_hash is None:
            identity = self.broker.account_identity()
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise ValueError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
            self._account_hash = hashlib.sha256(
                (LEDGER_BINDING_VERSION + ":" + identity).encode()
            ).hexdigest()
        return self._account_hash

    def _risk_policy(self, policy_id):
        policy = self._risk_policies.get(policy_id)
        if policy is None:  # Rows are immutable: one read per policy and process.
            with self.repo.connect() as conn:
                policy = load_policy(conn, policy_id, engine="MANAGED")
            self._risk_policies[policy_id] = policy
        return policy

    def _event(self, kind, body, setup_id=None, key=None):
        with self.store.transaction() as conn:
            return self.store.event(conn, kind, body, setup_id=setup_id, key=key)

    def _load(self, setup_id):
        with self.repo.connect() as conn:
            return self.store.setup(conn, setup_id), self.store.state(conn, setup_id)

    def _latch_execution_halt(self, conn, reason, details):
        if not conn.execute(
            "SELECT 1 FROM lab.execution_halts WHERE reason=%s",
            (reason,),
        ).fetchone():
            persist_execution_halt(self.repo, conn, reason, details)

    def admit(self, packet, *, live_quote=None):
        """Accept only a locally recorded research-selection packet, never a Muse approval flag.

        Crypto levels must also sit on the broker's live price grid. That broker read
        happens only for a packet that passes every ledger check, outside the shared
        lock; the checks then repeat under the lock. A refusal based on broker metadata
        is final for the receipt and recorded, so retries never repeat the read.

        An ``AGENT_RESEARCH_REPORT_V3`` packet (package system-check, plan 4.3) also needs
        ``live_quote(symbol)`` (the runtime's ``LivePriceReader``) and passes, after the grid
        and before the slot is spent, the independent ``SYSTEM_CHECK_V1`` and the run
        supersession check (``system_check.py``); its admitted state records the check's
        evidence and entry type. Every other packet is admitted exactly as before and never
        reads a live price.
        """
        required = {
            "cycle_id",
            "revision",
            "symbol",
            "market",
            "levels",
            "thesis",
            "disproof",
            "sources",
            "receipt_id",
            "evidence_hash",
            "expires_at",
            "selection_event_seq",
        }
        if not required <= set(packet) or packet["market"] not in {"US_STOCKS", "CRYPTO"}:
            raise ValueError("APP_REVIEWED_PACKET_REQUIRED")
        v3 = is_v3_packet(packet)
        if v3:
            v3_facts(packet)  # A V3 packet without its agent price or run slot is refused.
        if packet.get("selection_policy") == ENGINEERING_SELECTION_POLICY:
            # Operator ENGINEERING_TEST enrollment (plan 0.10): there is no Jev receipt to
            # verify; lab.managed_review_failure binds the packet to its audited enrollment.
            if packet["receipt_id"] is not None or "enrollment_event_seq" not in packet:
                raise ValueError("APP_REVIEWED_PACKET_REQUIRED")
        else:
            self.review_store.verify(packet["receipt_id"])
        now = self.now()
        expiry = datetime.fromisoformat(packet["expires_at"])
        t, m, s, p = (
            num(packet["levels"][k]) for k in ("entry_trigger", "max_entry_price", "stop", "target")
        )
        if not 0 < s < t <= m < p or p - m < 2 * (m - s) or expiry <= now:
            raise ValueError("INVALID_OR_EXPIRED_SETUP")
        # CRYPTO_24H_HOLD_V1 (crypto_holding.py): a report-V3 crypto pick has no New York entry
        # cutoff or midnight flatten; it may enter until its expiry and holds up to 24 hours
        # (or its window under the window versions; the recorded policy is chosen below).
        hold = CRYPTO_24H_HOLD if v3 and packet["market"] == "CRYPTO" else None
        day_policy = self.crypto_day_policy if packet["market"] == "CRYPTO" and not hold else None
        crypto_deadline = day_policy.entry_deadline(now) if day_policy else None
        if crypto_deadline is not None and now >= crypto_deadline:
            raise ValueError("CRYPTO_ENTRY_WINDOW_CLOSED")
        increment = check = None
        if packet["market"] == "CRYPTO":
            with self.repo.connect() as conn:
                admitted = self._admitted(conn, packet)
                if not admitted:
                    self._admission_checks(conn, packet, now)
                    refused = self._recorded_crypto_refusal(conn, packet)
                    if refused:
                        raise ValueError(refused)
                    if v3:
                        self._v3_ledger_checks(conn, packet)
            if not admitted:
                increment = self._crypto_price_increment(packet["symbol"])
                off_grid = {} if increment is None else off_grid_levels(increment, packet["levels"])
                if increment is None or off_grid:
                    self._refuse_crypto_admission(packet, now, increment, off_grid)
                elif v3:
                    check = self._system_check(packet, live_quote)
        with self.store.transaction() as conn:
            old = self._admitted(conn, packet)
            if old:
                return old["setup_id"]
            self._admission_checks(conn, packet, now)
            if increment is not None:
                refused = self._recorded_crypto_refusal(conn, packet)
                if refused:
                    raise ValueError(refused)  # Once refused, a receipt is never admitted.
                self._record_crypto_metadata(conn, packet["symbol"], increment, now)
            if v3:
                self._v3_ledger_checks(conn, packet)
                if check is None:  # Only a concurrently admitted receipt gets here.
                    raise AdmissionRefused(LIVE_PRICE_UNAVAILABLE)
            sid = uuid4()
            strategy = (
                "CATALYST_RETEST_V1"
                if packet["market"] == "US_STOCKS"
                else "CRYPTO_STRUCTURAL_RETEST_TEST_V1"
            )
            conn.execute(
                """INSERT INTO lab.managed_setups(setup_id,cycle_id,revision,symbol,
                market,strategy_version,policy_id,cohort,receipt_id,evidence_hash,expires_at,
                record_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    sid,
                    packet["cycle_id"],
                    packet["revision"],
                    packet["symbol"],
                    packet["market"],
                    strategy,
                    POLICY,
                    COHORT,
                    packet["receipt_id"],
                    packet["evidence_hash"],
                    expiry,
                    Jsonb(json_safe(packet)),
                ),
            )
            # The account-risk policy and the randomized control arm are fixed in the admitting
            # transaction and never change: FIXED_EXIT gets no management reviews (the original
            # bracket and mechanical exits only), JEV_MANAGED may be reviewed.
            arm = assign_arm(sid, self.risk_policy.fixed_exit_arm_pct)
            # CRYPTO_24H_REVIEW_V1 (package day-review): the maintained arm's 24-hour
            # continue-or-exit review; the control arm keeps CRYPTO_24H_HOLD_V1 (plan 4.6.6).
            # With MANAGED_CRYPTO_WINDOW_JSON (package review-window): CRYPTO_WINDOW_REVIEW_V1
            # and CRYPTO_WINDOW_HOLD_V1, the window recorded beside the policy.
            holding = crypto_holding.admission_policy(bool(hold), arm, self.crypto_window)
            self.store.transition(
                conn,
                sid,
                "WATCHING",
                stop=str(s),
                target=str(p),
                qty="0",
                lifecycle_id=str(uuid4()),
                admitted_at=now.isoformat(),
                receipt_id=packet["receipt_id"],
                exit_requested=None,
                crypto_day_policy=asdict(day_policy) if day_policy else None,
                crypto_entry_deadline=crypto_deadline.isoformat() if crypto_deadline else None,
                crypto_flat_deadline=day_policy.session_flat_at(now).isoformat()
                if day_policy else None,
                risk_policy_id=self.risk_policy.policy_id,
                arm=arm,
                arm_method=ARM_METHOD,
                fixed_exit_arm_pct=self.risk_policy.fixed_exit_arm_pct,
                **({"holding_policy": holding.record()} if holding else {}),
                # The window versions only: holding_window_seconds, so the trade keeps the
                # window it started with (every other state is unchanged).
                **crypto_holding.window_fields(holding),
                # V3 only: the passed SYSTEM_CHECK_V1 and its entry type, for the later
                # trigger versions and measurement. Other packets' states are unchanged.
                **({"system_check": check, "entry_type": check["entry_type"]} if v3 else {}),
                # Crypto from report V3 only: CRYPTO_ALPACA_TRIGGER_V1 (package crypto-trigger)
                # decides this setup's trigger; every other setup keeps today's trigger.
                **({"trigger_version": crypto_trigger.CRYPTO_TRIGGER_VERSION}
                   if crypto_trigger.applies(packet) else {}),
                # The same setups only: CRYPTO_GAP_RESUME_V1 (package gap-resume) holds the
                # setup for a bar check on a market gap instead of revoking it.
                **gap_resume.admission_fields(packet),
                # The same setups only: STALE_PRINT_ABOVE_TRIGGER_V1 (owner approval 2026-09-28)
                # consumes a late print above the entry trigger instead of revoking the setup.
                **stale_print.admission_fields(packet),
                # Crypto from report V3 in the JEV_MANAGED arm only (package maintenance, plan
                # 4.6): CRYPTO_MAINTENANCE_V1 maintains the trade and CRYPTO_PARTIAL_ENTRY_V1
                # handles a partial entry fill. The control arm and every other setup keep
                # today's behaviour.
                **crypto_maintenance.admission_fields(packet, arm),
            )
            return sid

    def _admission_checks(self, conn, packet, now):
        """Raise the first ledger reason this packet cannot be admitted; never writes."""
        if conn.execute("SELECT 1 FROM lab.execution_halts LIMIT 1").fetchone():
            raise ValueError("RISK_HALT")
        if conn.execute(
            "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s",
            (now.astimezone(NY).date(),),
        ).fetchone():
            raise ValueError("DAILY_RISK_HALT")
        if conn.execute("SELECT 1 FROM lab.pending_risk_exits LIMIT 1").fetchone():
            raise ValueError("ACCOUNT_EXIT_PENDING")
        selection = conn.execute(
            "SELECT * FROM lab.managed_events WHERE event_seq=%s",
            (packet["selection_event_seq"],),
        ).fetchone()
        if (
            not selection
            or selection["kind"] != "RESEARCH_SELECTED"
            or selection["body"].get("packet")
            != json_safe({k: v for k, v in packet.items() if k != "selection_event_seq"})
        ):
            raise ValueError("DURABLE_SELECTION_BINDING_REQUIRED")
        failure = conn.execute(
            "SELECT lab.managed_review_failure(%s) AS reason", (Jsonb(json_safe(packet)),)
        ).fetchone()["reason"]
        if failure:
            raise ValueError(failure)
        # Refuse before spending the attempt: an unclassified stock, or a crypto symbol the
        # owner's buckets do not list, could never pass the account-risk check anyway.
        classification = conn.execute(
            "SELECT sector,theme FROM lab.current_classifications WHERE ticker=%s",
            (packet["symbol"],),
        ).fetchone()
        if not classification_known(classification):
            raise ValueError("CORRELATION_UNKNOWN")
        # Admission spends the attempt even if the setup later expires or
        # closes. The shared risk lock serializes competing review receipts
        # and the legacy engine's final entry authorization.
        if packet["market"] == "US_STOCKS":
            day = now.astimezone(NY).date()
            legacy_attempt = conn.execute(
                """SELECT 1 FROM lab.ticker_day_claims
                WHERE upper(ticker)=upper(%s) AND session_date=%s LIMIT 1""",
                (packet["symbol"], day),
            ).fetchone()
            managed_attempt = conn.execute(
                """SELECT 1 FROM lab.managed_setups s
                JOIN lab.managed_states t USING(setup_id)
                WHERE s.market='US_STOCKS' AND upper(s.symbol)=upper(%s)
                AND ((t.body->>'admitted_at')::timestamptz
                     AT TIME ZONE 'America/New_York')::date=%s LIMIT 1""",
                (packet["symbol"], day),
            ).fetchone()
            if legacy_attempt or managed_attempt:
                raise ValueError("TICKER_ALREADY_ATTEMPTED")
        elif conn.execute(
            """SELECT 1 FROM lab.managed_setups s
            LEFT JOIN lab.managed_states t USING(setup_id)
            WHERE s.market='CRYPTO'
              AND upper(replace(s.symbol,'/',''))=upper(replace(%s,'/',''))
              AND coalesce(t.body->>'state','')<>ALL(%s) LIMIT 1""",
            (packet["symbol"], sorted(TERMINAL)),
        ).fetchone():
            raise ValueError("ACTIVE_SYMBOL_ALREADY_MANAGED")

    def _crypto_price_increment(self, symbol):
        """Live broker price increment, or None when the broker has no usable metadata.

        Transport failures propagate unrecorded, so the next runtime tick retries.
        """
        asset = self.broker.asset(symbol)
        try:
            increment = num(asset["price_increment"])
            if asset.get("symbol") != symbol or asset.get("class") != "crypto" or increment <= 0:
                return None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        return increment

    def _record_crypto_metadata(self, conn, symbol, increment, now):
        # One row per symbol, NY day and increment; intake reads it to refuse off-grid items.
        self.store.event(
            conn,
            "CRYPTO_ASSET_METADATA",
            {"symbol": symbol, "price_increment": increment, "source": "ALPACA_PAPER_ASSET"},
            key=f"crypto-asset-metadata:{symbol}:{now.astimezone(NY).date()}:{increment}",
        )

    @staticmethod
    def _admitted(conn, packet):
        """The setup already admitted for this packet: by its Jev receipt, or for an operator
        ENGINEERING_TEST enrollment, which has no receipt, by its enrollment event."""
        if packet.get("selection_policy") == ENGINEERING_SELECTION_POLICY:
            return conn.execute(
                """SELECT * FROM lab.managed_setups WHERE receipt_id IS NULL
                AND record_json->>'enrollment_event_seq'=%s""",
                (str(packet["enrollment_event_seq"]),),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM lab.managed_setups WHERE receipt_id=%s", (packet["receipt_id"],)
        ).fetchone()

    @staticmethod
    def _crypto_refusal_key(packet):
        if packet.get("selection_policy") == ENGINEERING_SELECTION_POLICY:
            return f"crypto-admission-refused:engineering:{packet['enrollment_event_seq']}"
        return "crypto-admission-refused:" + str(packet["receipt_id"])

    @classmethod
    def _recorded_crypto_refusal(cls, conn, packet):
        row = conn.execute(
            "SELECT body FROM lab.managed_events WHERE idempotency_key=%s",
            (cls._crypto_refusal_key(packet),),
        ).fetchone()
        return row["body"]["reason"] if row else None

    def _refuse_crypto_admission(self, packet, now, increment, off_grid):
        """Record a final broker-metadata refusal for this receipt, then raise its code.

        Returns without recording only when a concurrent admission of the same receipt
        already won; the caller then returns that setup.
        """
        reason = (
            "CRYPTO_PRECISION_UNAVAILABLE" if increment is None else "CRYPTO_LEVEL_OFF_PRICE_GRID"
        )
        with self.store.transaction() as conn:
            if self._admitted(conn, packet):
                return
            prior = self._recorded_crypto_refusal(conn, packet)
            if prior is None:
                if increment is not None:
                    self._record_crypto_metadata(conn, packet["symbol"], increment, now)
                self.store.event(
                    conn,
                    "CRYPTO_ADMISSION_REFUSED",
                    {
                        "reason": reason,
                        "receipt_id": packet["receipt_id"],
                        "selection_event_seq": packet["selection_event_seq"],
                        "cycle_id": packet["cycle_id"],
                        "symbol": packet["symbol"],
                        "price_increment": increment,
                        "off_grid_levels": off_grid,
                    },
                    key=self._crypto_refusal_key(packet),
                )
        raise ValueError(prior or reason)

    # --- Report V3: supersession and SYSTEM_CHECK_V1 (package system-check) ----------------

    @staticmethod
    def _recorded_system_check(conn, packet):
        row = conn.execute(
            "SELECT body FROM lab.managed_events WHERE idempotency_key=%s",
            (REFUSED_KEY + str(packet["receipt_id"]),),
        ).fetchone()
        return row["body"] if row else None

    def _v3_ledger_checks(self, conn, packet):
        """Raise a V3 packet's recorded system-check refusal, or its run's supersession.

        Read under the shared lock in the admitting transaction, which also serializes
        selection publication, so a pick from a run older than any published V3 selection is
        never admitted (RESEARCH_RUN_SUPERSESSION_V1). Never writes.
        """
        prior = self._recorded_system_check(conn, packet)
        if prior is not None:
            raise AdmissionRefused(prior["reason"], {"system_check": prior["system_check"]})
        _, slot = v3_facts(packet)
        newest = newest_v3_run_slot(conn)
        if newest is not None and slot < newest:
            raise AdmissionRefused(SUPERSEDED_BY_NEW_RESEARCH, {
                "run_slot": packet["run_slot"],
                "superseded_by_run_slot": newest.isoformat(),
                "supersession_rule": SUPERSESSION_VERSION,
            })

    def _system_check(self, packet, live_quote):
        """SYSTEM_CHECK_V1 evidence of a passed V3 pick; a refusal raises its code.

        Runs outside the shared lock after every ledger check and the broker price grid,
        before the setup row that spends the symbol slot. The level check comes first, so a
        pick the levels refuse never costs a live-price read. A permanent refusal is final
        for the receipt (``_refuse_system_check``); LIVE_PRICE_UNAVAILABLE is transient and
        recorded only by the runtime's once-per-reason refusal audit. None only when a
        concurrent admission of the same receipt won.
        """
        levels_only = evaluate(packet, None, now=self.now())
        if levels_only["result"] == REFUSED:
            return self._refuse_system_check(packet, levels_only)
        try:
            if live_quote is None:
                raise LivePriceUnavailable([{"source": "NONE", "code": "LIVE_PRICE_NOT_WIRED"}])
            quote = live_quote(packet["symbol"])
        except LivePriceUnavailable as exc:
            attempts = exc.attempts
        except Exception as exc:  # A reader fault is a missing price, never a pass.
            attempts = [{"source": "LIVE_PRICE_READER", "code": error_code(exc)}]
        else:
            evidence = evaluate(packet, quote, now=self.now())
            if evidence["result"] == REFUSED:
                return self._refuse_system_check(packet, evidence)
            return evidence
        evidence = evaluate(packet, None, now=self.now(), attempts=attempts)
        raise AdmissionRefused(LIVE_PRICE_UNAVAILABLE, {"system_check": evidence})

    def _refuse_system_check(self, packet, evidence):
        """Record the receipt's final SYSTEM_CHECK_REFUSED once, then raise its code.

        Returns None without recording only when a concurrent admission of the same receipt
        already won; the caller then returns that setup. Nothing here spends a slot.
        """
        with self.store.transaction() as conn:
            if self._admitted(conn, packet):
                return None
            prior = self._recorded_system_check(conn, packet)
            if prior is None:
                self.store.event(
                    conn,
                    REFUSED_EVENT,
                    {
                        "reason": evidence["code"],
                        "receipt_id": packet["receipt_id"],
                        "selection_event_seq": packet["selection_event_seq"],
                        "cycle_id": packet["cycle_id"],
                        "item_key": packet.get("item_key"),
                        "symbol": packet["symbol"],
                        "run_slot": packet["run_slot"],
                        "system_check": evidence,
                    },
                    key=REFUSED_KEY + str(packet["receipt_id"]),
                )
        recorded = prior or {"reason": evidence["code"], "system_check": evidence}
        raise AdmissionRefused(recorded["reason"], {"system_check": recorded["system_check"]})

    def revoke(self, setup_id, reason, *, watching_only=False, details=None, key=None):
        """Revoke a setup's research evidence: a WATCHING setup is INVALIDATED; a working one
        keeps its state marked revoked, which the reservation SQL and dispatch refuse.

        ``watching_only`` (research-run supersession) revokes only a setup still WATCHING under
        the shared lock and returns whether it did; ``details`` adds audit fields to the REVOKE
        body and ``key`` makes the event idempotent. Existing callers are unchanged.
        """
        with self.store.transaction() as conn:
            if watching_only and self.store.state(conn, setup_id).get("state") != "WATCHING":
                return False
            self.store.event(
                conn, "REVOKE", {"reason": reason, **(details or {})}, setup_id=setup_id, key=key
            )
            state = self.store.state(conn, setup_id)
            if state["state"] not in TERMINAL:
                self.store.transition(
                    conn,
                    setup_id,
                    "INVALIDATED" if state["state"] == "WATCHING" else state["state"],
                    revoked=True,
                    revocation_reason=reason,
                )
        return True

    def reconcile(self):
        """Every startup begins unready. Unknown broker inventory/orders block admission."""
        self.reconciled_at = None
        account_hash = self._account_binding_hash()
        positions, orders = self.broker.positions(), self.broker.open_orders()
        with self.store.transaction() as conn:
            bound = conn.execute(
                "SELECT account_hash FROM lab.ledger_account_binding WHERE venue=%s", (VENUE,)
            ).fetchone()
            if bound and bound["account_hash"] != account_hash:
                raise ValueError("LEDGER_ACCOUNT_BINDING_MISMATCH")
            known = conn.execute("""SELECT s.symbol,e.body->>'qty' AS qty FROM
                lab.managed_setups s JOIN LATERAL(SELECT body FROM lab.managed_events
                  WHERE setup_id=s.setup_id AND kind='BROKER_POSITION'
                  ORDER BY (body->>'occurred_at')::timestamptz DESC NULLS LAST,
                           event_seq DESC LIMIT 1) e ON true""").fetchall()
            expected = {}
            for row in known:
                key = normalized_symbol(row["symbol"])
                expected[key] = expected.get(key, D(0)) + num(row["qty"])
            for row in conn.execute("SELECT ticker,qty FROM lab.strategy_positions").fetchall():
                key = normalized_symbol(row["ticker"])
                expected[key] = expected.get(key, D(0)) + num(row["qty"])
            actual = {normalized_symbol(p["symbol"]): num(p["qty"]) for p in positions}
            owned = {
                normalized_symbol(row["symbol"])
                for row in conn.execute(
                    """SELECT s.symbol FROM lab.managed_active_reservations r
                    JOIN lab.managed_setups s USING(setup_id)
                    UNION SELECT c.ticker FROM lab.active_reservations r
                    JOIN lab.candidates c USING(candidate_id)"""
                ).fetchall()
            }
            unexplained = [symbol for symbol, qty in actual.items() if qty and symbol not in owned]
            if unexplained:
                self._latch_execution_halt(
                    conn,
                    "MANAGED_UNEXPLAINED_BROKER_POSITION",
                    {"symbols": unexplained},
                )
            if any(qty < 0 for qty in actual.values()):
                self._latch_execution_halt(
                    conn,
                    "MANAGED_UNEXPECTED_SHORT_POSITION",
                    {"reason": "OPERATOR_RECONCILIATION_REQUIRED"},
                )
            mismatches = [
                {
                    "type": "POSITION_QUANTITY_MISMATCH",
                    "symbol": symbol,
                    "broker_qty": str(actual.get(symbol, D(0))),
                    "local_qty": str(expected.get(symbol, D(0))),
                }
                for symbol in sorted(set(expected) | set(actual))
                if expected.get(symbol, D(0)) != actual.get(symbol, D(0))
            ]
            decisions = conn.execute(
                "SELECT payload FROM lab.managed_risk_decisions WHERE outcome='APPROVED'"
            ).fetchall()
            known_ids = {r["payload"].get("client_order_id") for r in decisions}
            acks = conn.execute(
                "SELECT body FROM lab.managed_events WHERE kind='BROKER_ACK'"
            ).fetchall()
            owned_ids = set()

            def collect_order(order):
                if order.get("id"):
                    owned_ids.add(order["id"])
                if order.get("replaced_by"):
                    owned_ids.add(order["replaced_by"])
                for child in order.get("legs", []) or []:
                    collect_order(child)

            for ack in acks:
                collect_order(ack["body"].get("order", {}))
            for order in orders:
                if order.get("client_order_id") in known_ids or order.get("id") in owned_ids:
                    collect_order(order)
            legacy_ids = {
                r["alpaca_order_id"]
                for r in conn.execute("SELECT alpaca_order_id FROM lab.orders").fetchall()
            }
            for order in orders:
                if (
                    order.get("client_order_id") not in known_ids
                    and order.get("id") not in legacy_ids
                    and order.get("id") not in owned_ids
                ):
                    mismatches.append({"type": "UNKNOWN_ORDER", "order_id": order.get("id")})
            self.store.event(
                conn,
                "BROKER_RECONCILIATION",
                {
                    "clean": not mismatches,
                    "mismatches": mismatches,
                    "position_count": len(positions),
                    "order_count": len(orders),
                },
            )
            # Shared NY account day: first reconciled previous-close equity wins, never reset.
            account = self.broker.account()
            now = self.now()
            if not mismatches:
                prior = num(account["last_equity"])
                if prior <= 0:
                    raise ValueError("PREVIOUS_CLOSE_EQUITY_REQUIRED")
                event = system_event(
                    self.repo,
                    conn,
                    "MANAGED_STARTUP_RECONCILIATION",
                    {"clean": True, "baseline_source": "ALPACA_LAST_EQUITY"},
                )
                conn.execute(
                    """INSERT INTO lab.reconciliation_runs(event_seq,process_run_id,
                    session_date,started_at,completed_at,startup,clean,discrepancies,broker_snapshot)
                    VALUES(%s,%s,%s,%s,%s,true,true,'[]','{}')""",
                    (event["seq"], self.process_run_id, now.astimezone(NY).date(), now, now),
                )
                conn.execute(
                    """INSERT INTO lab.risk_sessions(session_date,day_start_equity,
                    source,observed_at,event_seq,reconciliation_seq) VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(session_date) DO NOTHING""",
                    (
                        now.astimezone(NY).date(),
                        prior,
                        "ALPACA_LAST_EQUITY",
                        now,
                        event["seq"],
                        event["seq"],
                    ),
                )
                if not bound:  # Written once, at the ledger's first clean reconciliation.
                    conn.execute(
                        """INSERT INTO lab.ledger_account_binding(venue,account_hash,
                        binding_version,reconciliation_seq) VALUES(%s,%s,%s,%s)""",
                        (VENUE, account_hash, LEDGER_BINDING_VERSION, event["seq"]),
                    )
        if not mismatches:
            self.reconciled_at = self.now()
        return {"clean": not mismatches, "mismatches": mismatches}

    def _account_halt(self, conn, account, positions, cashflow):
        day = self.now().astimezone(NY).date()
        base = conn.execute(
            "SELECT * FROM lab.risk_sessions WHERE session_date=%s", (day,)
        ).fetchone()
        if not base:
            return "STARTUP_RECONCILIATION_REQUIRED"
        unrealized = sum((num(p["unrealized_pl"]) for p in positions), D(0))
        total = num(account["equity"]) - base["day_start_equity"] - cashflow
        already = conn.execute(
            "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s", (day,)
        ).fetchone()
        if total <= -D(".03") * base["day_start_equity"] and not already:
            event = system_event(
                self.repo,
                conn,
                "DAILY_RISK_HALT",
                {"total_pnl": total, "action": "CANCEL_AND_FLATTEN", "cohort": COHORT},
            )
            conn.execute(
                """INSERT INTO lab.daily_risk_halts(session_date,realized_pnl,
                unrealized_pnl,threshold,event_seq) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(session_date) DO NOTHING""",
                (
                    day,
                    total - unrealized,
                    unrealized,
                    -D(".03") * base["day_start_equity"],
                    event["seq"],
                ),
            )
        if conn.execute(
            "SELECT 1 FROM lab.daily_risk_halts WHERE session_date=%s", (day,)
        ).fetchone():
            return "DAILY_RISK_HALT"
        return None

    def _shared_snapshot(self, *, max_age=None):
        """The request-governed broker snapshot, or None for a directly used broker."""
        if isinstance(self.broker, BudgetedBroker):
            return self.broker.snapshot(max_age=max_age)
        return None

    @staticmethod
    def _capital_cashflow(activities):
        cashflow = D(0)
        for a in activities:
            if a.get("activity_type") not in {"CSD", "CSW", "ACATC"}:
                raise ValueError("UNSUPPORTED_CAPITAL_ACTIVITY")
            cashflow += num(a["net_amount"])
        return cashflow

    def account_snapshot(self, *, shared=False):
        """Account, positions, same-day capital cash flow and the time they were read.

        The default is a fresh read (entry authorization requires it to be at most five
        seconds old). ``shared=True`` lets protection ticks reuse the governed snapshot
        taken within the budget's snapshot interval; with an ungoverned broker both
        forms read the broker directly.
        """
        snapshot = self._shared_snapshot(max_age=None if shared else 0)
        if snapshot is not None:
            cashflow = self._capital_cashflow(snapshot.capital_activity_rows())
            account, positions = snapshot.account, snapshot.position_rows()
            observed = snapshot.taken_at
        else:
            now = self.now()
            activities = self.broker.capital_activities(now.astimezone(NY).date())
            cashflow = self._capital_cashflow(activities)
            positions = self.broker.positions()
            account = self.broker.account()
            observed = None
        if (
            account.get("status") != "ACTIVE"
            or account.get("currency") != "USD"
            or any(
                account.get(k) is not False
                for k in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
            )
        ):
            raise ValueError("BROKER_ACCOUNT_BLOCKED")
        return account, positions, cashflow, observed if observed is not None else self.now()

    def _account_evidence(self):
        """``(None, (account, positions, cashflow))`` or ``("UNEVALUABLE:<code>", None)``.

        For protection ticks only. ``account_snapshot`` stays strict (entry authorization
        and account safety still refuse on the same evidence), but here a failed account
        read, an unsupported capital activity or a blocked account only makes account risk
        unevaluable for this tick; it never raises out of, or exits, a managed position.
        """
        try:
            account, positions, cashflow, _ = self.account_snapshot(shared=True)
        except Exception as exc:
            return UNEVALUABLE + error_code(exc), None
        return None, (account, positions, cashflow)

    def _note_account_risk(self, conn, setup_id, state, account_risk):
        """Record ``account_risk`` in the setup state; one event per code and New York day.

        ``UNEVALUABLE:<code>`` blocks model amendments (``accept_management`` and the start
        of an accepted plan). The state changes only when the value changes, so an ongoing
        condition adds no event or revision per tick.
        """
        if account_risk:
            day = self.now().astimezone(NY).date()
            code = account_risk.removeprefix(UNEVALUABLE)
            self.store.event(
                conn,
                "ACCOUNT_RISK_UNEVALUABLE",
                {
                    "code": code,
                    "session_date": day,
                    "blocks": ["ENTRY", "MODEL_AMENDMENT"],
                    "mechanical_protection": "CONTINUES",
                },
                key=f"account-risk-unevaluable:{day}:{code}",
            )
        if state.get("account_risk") != account_risk:
            state = self.store.transition(
                conn, setup_id, state["state"], account_risk=account_risk
            )
        return state

    def _decision(
        self,
        conn,
        setup,
        state,
        action,
        payload,
        context,
        *,
        equity,
        method="POST",
        path="/v2/orders",
        reason="RISK_APPROVED",
        approved=True,
    ):
        return conn.execute(
            """INSERT INTO lab.managed_risk_decisions(decision_id,setup_id,
            action,outcome,reason,method,path,payload,context,equity,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '5 seconds')
            RETURNING *""",
            (
                uuid4(),
                setup["setup_id"],
                action,
                "APPROVED" if approved else "REJECTED",
                reason,
                method if approved else "NONE",
                path,
                Jsonb(json_safe(payload)),
                Jsonb(json_safe({**context, "state_revision": state["revision"]})),
                equity,
            ),
        ).fetchone()

    def observe_trigger(self, setup_id, observation):
        setup, state = self._load(setup_id)
        if state["state"] != "WATCHING":
            return None
        now = self.now()
        entry_deadline = min(
            setup["expires_at"],
            datetime.fromisoformat(state["crypto_entry_deadline"])
            if state.get("crypto_entry_deadline") else setup["expires_at"],
        )
        if now >= entry_deadline:
            with self.store.transaction() as conn:
                self.store.transition(conn, setup_id, "EXPIRED_UNTRIGGERED", reason="SETUP_EXPIRED")
            return None
        if crypto_trigger.active(state):
            return self._observe_crypto_trigger(setup, state, observation, now)
        levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
        price = num(observation["trade_price"])
        trade_at = datetime.fromisoformat(observation["trade_at"])
        if trade_at < datetime.fromisoformat(state["admitted_at"]) or trade_at > now:
            return None
        if setup["market"] == "US_STOCKS":
            sessions = self.broker.calendar(now.astimezone(NY).date(), now.astimezone(NY).date())
            session = next((s for s in sessions if s.contains(now)), None)
            if not session or not session.contains(trade_at):
                return None
            if now >= session.flatten_time:
                with self.store.transaction() as conn:
                    self.store.transition(
                        conn, setup_id, "EXPIRED_UNTRIGGERED", reason="FLATTEN_DEADLINE"
                    )
                return None
        else:
            session = None
        if price <= levels["stop"] or not observation["feed_healthy"]:
            reason = (
                "STOP_TRADED_BEFORE_TRIGGER" if price <= levels["stop"] else "DATA_FEED_FAILURE"
            )
            with self.store.transaction() as conn:
                self.store.transition(conn, setup_id, "INVALIDATED", reason=reason)
            return None
        quote_at = datetime.fromisoformat(observation["quote_at"])
        reason = None
        if price > levels["entry_trigger"]:
            return None
        elif not (
            0 <= (now - quote_at).total_seconds() <= 5
            and 0 <= (now - trade_at).total_seconds() <= 5
        ):
            return None
        elif num(observation["ask"]) > levels["max_entry_price"]:
            reason = "PRICE_BEYOND_MAX_ENTRY"
        bid, ask = num(observation["bid"]), num(observation["ask"])
        if (
            bid <= 0
            or ask < bid
            or (ask - bid) / ((ask + bid) / 2) * 10000 > self.policy.spread_bps
        ):
            return None
        if reason:
            with self.store.transaction() as conn:
                self.store.transition(conn, setup_id, "INVALIDATED", reason=reason)
            return None
        return self.authorize_entry(setup_id, observation, session)

    def _trigger_failure(self, setup, state, observation, session, now):
        if crypto_trigger.active(state):
            return self._crypto_trigger_failure(setup, state, observation, now)
        try:
            levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
            trade_at = datetime.fromisoformat(observation["trade_at"])
            quote_at = datetime.fromisoformat(observation["quote_at"])
            price, bid, ask = (num(observation[k]) for k in ("trade_price", "bid", "ask"))
            if not observation["feed_healthy"]:
                return "DATA_FEED_FAILURE"
            if (
                not 0 <= (now - quote_at).total_seconds() <= 5
                or not 0 <= (now - trade_at).total_seconds() <= 5
            ):
                return "STALE_MARKET_OBSERVATION"
            if trade_at < datetime.fromisoformat(state["admitted_at"]):
                return "TRIGGER_BEFORE_ADMISSION"
            if price <= levels["stop"]:
                return "STOP_TRADED_BEFORE_TRIGGER"
            if price > levels["entry_trigger"]:
                return "TRIGGER_NOT_CONFIRMED"
            if ask > levels["max_entry_price"]:
                return "PRICE_BEYOND_MAX_ENTRY"
            if bid <= 0 or ask < bid or (ask - bid) / ((bid + ask) / 2) * 10000 > 10:
                return "MAX_SPREAD"
            if setup["market"] == "US_STOCKS" and (
                not session
                or not session.contains(now)
                or not session.contains(trade_at)
                or now >= session.flatten_time
            ):
                return "ENTRY_WINDOW_CLOSED"
            if state.get("crypto_entry_deadline") and now >= datetime.fromisoformat(
                state["crypto_entry_deadline"]
            ):
                return "CRYPTO_ENTRY_WINDOW_CLOSED"
            if levels["target"] - levels["max_entry_price"] < 2 * (
                levels["max_entry_price"] - levels["stop"]
            ):
                return "MIN_REWARD_RISK"
            return None
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return "INVALID_MARKET_EVIDENCE"

    # --- CRYPTO_ALPACA_TRIGGER_V1 (package crypto-trigger; rules in crypto_trigger.py) --------

    def _crypto_verdict(self, setup, state, observation, now):
        levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
        return crypto_trigger.evaluate(
            levels, observation, now=now, admitted_at=datetime.fromisoformat(state["admitted_at"])
        )

    def _observe_crypto_trigger(self, setup, state, observation, now):
        """A WATCHING setup of this version: invalidated, waiting (recorded) or authorized.

        A print or fresh bid at or below the stop, or a touch whose fresh ask is above the max
        entry, invalidates the setup (only while it is still WATCHING under the shared lock,
        with the evaluation as ``crypto_trigger``). A touch without a fresh quote or with the
        spread above the cap waits for the next touch: one CRYPTO_TRIGGER_WAIT per setup,
        reason and minute. A confirmed touch goes to ``authorize_entry`` (limit at max entry).
        """
        verdict = self._crypto_verdict(setup, state, observation, now)
        if verdict.outcome == crypto_trigger.INVALIDATE:
            with self.store.transaction() as conn:
                if self.store.state(conn, setup["setup_id"]).get("state") == "WATCHING":
                    self.store.transition(
                        conn, setup["setup_id"], "INVALIDATED",
                        reason=verdict.reason, crypto_trigger=verdict.evidence,
                    )
            return None
        if verdict.outcome == crypto_trigger.WAIT:
            with self.store.transaction() as conn:
                self._note_trigger_wait(conn, setup["setup_id"], verdict, now)
            return None
        if verdict.outcome != crypto_trigger.CONFIRM:
            return None  # No touch, or a print this version never evaluates (as today).
        return self.authorize_entry(setup["setup_id"], observation, None)

    def _locked_crypto_verdict(self, setup, state, observation, now):
        """The verdict re-taken under the shared lock after the entry-time broker reads. Only
        time has moved since ``observe_trigger``, so a touch can only have aged out: that
        waits (TOUCH_NOT_CURRENT) instead of ending the setup."""
        verdict = self._crypto_verdict(setup, state, observation, now)
        if verdict.outcome == crypto_trigger.NO_TOUCH:
            return crypto_trigger.Verdict(
                crypto_trigger.WAIT, crypto_trigger.TOUCH_NOT_CURRENT, None, verdict.evidence
            )
        return verdict

    def _crypto_trigger_failure(self, setup, state, observation, now):
        """``_trigger_failure`` for this version: None when confirmed; a WAIT_REASONS code
        (``authorize_entry`` then records the wait and makes no decision); else a final code
        (invalidation reasons, the crypto entry window, reward-to-risk at max entry)."""
        try:
            verdict = self._locked_crypto_verdict(setup, state, observation, now)
            levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
            if verdict.outcome != crypto_trigger.CONFIRM:
                return verdict.reason
            if state.get("crypto_entry_deadline") and now >= datetime.fromisoformat(
                state["crypto_entry_deadline"]
            ):
                return "CRYPTO_ENTRY_WINDOW_CLOSED"
            if levels["target"] - levels["max_entry_price"] < 2 * (
                levels["max_entry_price"] - levels["stop"]
            ):
                return "MIN_REWARD_RISK"
            return None
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return "INVALID_MARKET_EVIDENCE"

    def _record_trigger_wait(self, conn, setup, state, observation, now):
        """A wait found under the lock: recorded like any wait; no decision, still WATCHING."""
        self._note_trigger_wait(
            conn, setup["setup_id"],
            self._locked_crypto_verdict(setup, state, observation, now), now,
        )
        return None

    def _note_trigger_wait(self, conn, setup_id, verdict, now):
        """One CRYPTO_TRIGGER_WAIT per setup, reason and UTC minute, while still WATCHING."""
        minute = now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
        key = f"{crypto_trigger.WAIT_KEY}{setup_id}:{verdict.reason}:{minute}"
        if conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
        ).fetchone() or self.store.state(conn, setup_id).get("state") != "WATCHING":
            return
        self.store.event(
            conn,
            crypto_trigger.WAIT_EVENT,
            {
                "reason": verdict.reason,
                "touch": verdict.touch,
                "trigger_version": crypto_trigger.CRYPTO_TRIGGER_VERSION,
                "crypto_trigger": verdict.evidence,
            },
            setup_id=setup_id,
            key=key,
        )

    def _trigger_confirmed(self, setup, state, observation, now):
        """The TRIGGER_CONFIRMED body: the observation (today), or under this version the
        observation with ``trigger_version`` and the evaluation at confirmation."""
        if not crypto_trigger.active(state):
            return observation
        try:
            verdict = self._crypto_verdict(setup, state, observation, now)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            verdict = None
        return crypto_trigger.body(observation, verdict)

    @staticmethod
    def _capacity_deferred(state, now):
        until = state.get("capacity_deferred_until")
        return bool(until) and now < datetime.fromisoformat(until)

    @staticmethod
    def _unfilled_reserved_notional(conn, reservations):
        """Cash already promised to open reservations and not yet spent at the broker: each
        managed reservation's quantity not yet bought (its recorded buy fills) at its maximum
        entry, and every frozen reservation in full. A bought quantity has left the account's
        cash, so it is not counted a second time."""
        managed = [r["reference_id"] for r in reservations if r["source"] == "MANAGED"]
        bought = {}
        if managed:
            bought = {
                row["setup_id"]: row["qty"]
                for row in conn.execute(
                    """SELECT setup_id,sum(qty) AS qty FROM lab.managed_fills
                    WHERE side='buy' AND setup_id=ANY(%s) GROUP BY setup_id""",
                    (managed,),
                ).fetchall()
            }
        total = D(0)
        for r in reservations:
            filled = bought.get(r["reference_id"], D(0)) if r["source"] == "MANAGED" else D(0)
            total += max(D(0), r["qty"] - filled) * r["max_entry"]
        return total

    def _slice_entry(self, conn, policy, terms, setup, classification, account, reservations,
                     asset, equity, m, s, reason):
        """Size a crypto entry in equity slices (``EQUITY_SLICE_RISK_CAPPED_V1``, migration 022).

        The notional is at most the terms' slice of current equity and the planned risk
        qty x (M - S) at most the row's ``risk_pct`` of equity, rounded down to the coin's
        quantity increment (``account_risk.slice_size``, the mirror of
        ``lab.slice_sizing_failure``), within the cash available to crypto: the broker's
        non-marginable buying power and the cash not already promised to an unfilled
        reservation. A stop closer than the terms' minimum is refused. The one SQL account-risk
        check then runs on the actual planned risk, which the reservation holds as its budget.

        Returns ``(qty, budget, binding, sizing evidence, asset, reason, capacity)``; ``reason``
        is terminal, ``capacity`` a deferrable ``CAPACITY_REASONS`` code.
        """
        capacity, ca, sizing = None, None, None
        cash_only = broker_amount(account.get("non_marginable_buying_power"))
        unfilled = self._unfilled_reserved_notional(conn, reservations)
        available = max(D(0), min(
            equity,
            num(account["cash"]) - unfilled,
            cash_only if cash_only is not None else D(0),
        ))
        if m - s < terms.min_stop_fraction * m:
            return D(0), D(0), None, None, None, reason or "STOP_DISTANCE_BELOW_MINIMUM", None
        try:
            ca = CryptoAsset.from_broker(asset)
            sizing = slice_size(policy, terms, equity=equity, max_entry=m, stop=s,
                                available=available, increment=ca.min_trade_increment)
        except ValueError as error:  # Venue metadata, precision or sizing input refuses it.
            return D(0), D(0), None, None, None, reason or str(error), None
        evidence = {**sizing.evidence, "unfilled_reserved_notional": unfilled,
                    "min_order_size": ca.min_order_size}
        # The reservation's budget and planned risk, computed exactly as its insert computes it.
        planned = sizing.qty * (m - s)
        if sizing.qty < ca.min_order_size:
            if sizing.policy_qty >= ca.min_order_size:
                capacity = "INSUFFICIENT_BUYING_POWER"  # Capital-limited.
            else:
                reason = reason or "ZERO_SHARE_SIZE"
        elif not reason:
            capacity = account_risk_failure(
                conn,
                policy.policy_id,
                setup["market"],
                classification["sector"],
                classification["theme"],
                equity,
                planned,
            )
            if capacity is not None and capacity not in CAPACITY_REASONS:
                reason, capacity = capacity, None
        return sizing.qty, planned, sizing.binding, evidence, ca, reason, capacity

    def authorize_entry(self, setup_id, observation, session):
        setup, current = self._load(setup_id)
        if current.get("state") == "WATCHING" and self._capacity_deferred(current, self.now()):
            return None  # Capacity cooldown: triggers are skipped before any broker read.
        # Each setup is evaluated under the policy recorded at its admission; setups admitted
        # before migration 016 carry none and keep the schema-13 numbers.
        policy = self._risk_policy(current.get("risk_policy_id") or LEGACY_MANAGED_POLICY_ID)
        eligibility = validate_managed_eligibility(
            self.broker,
            setup["record_json"],
            self.now,
            admission_profile("US_PAPER_ADMISSION_TEST_V1"),
        )
        liquidity = None
        if setup["market"] == "CRYPTO" and self.crypto_liquidity_reader is not None:
            liquidity = self.crypto_liquidity_reader(
                setup["symbol"], setup["record_json"]["levels"]
            )
        account, positions, cashflow, observed = self.account_snapshot()
        equity = num(account["equity"])
        asset = self.broker.asset(setup["symbol"])
        with self.store.transaction() as conn:
            state = self.store.state(conn, setup_id)
            if state["state"] != "WATCHING":
                return None
            now = self.now()
            if self._capacity_deferred(state, now):
                return None
            reason = self._trigger_failure(setup, state, observation, session, now)
            if reason in crypto_trigger.WAIT_REASONS:  # CRYPTO_ALPACA_TRIGGER_V1 only.
                return self._record_trigger_wait(conn, setup, state, observation, now)
            reservations = conn.execute("SELECT * FROM lab.account_risk_reservations").fetchall()
            classification = conn.execute(
                "SELECT * FROM lab.current_classifications WHERE ticker=%s", (setup["symbol"],)
            ).fetchone()
            if not self.reconciled_at or (now - self.reconciled_at).total_seconds() > 60:
                reason = "STARTUP_RECONCILIATION_REQUIRED"
            elif now >= setup["expires_at"] or (now - observed).total_seconds() > 5:
                reason = "STALE_RISK_EVIDENCE"
            elif state.get("revoked"):
                reason = "MATERIAL_EVIDENCE_REVOKED"
            elif not classification_known(classification):
                reason = "CORRELATION_UNKNOWN"
            elif conn.execute("SELECT 1 FROM lab.execution_halts LIMIT 1").fetchone():
                reason = "RISK_HALT"
            daily_failure = self._account_halt(conn, account, positions, cashflow)
            review_failure = conn.execute(
                "SELECT lab.managed_review_failure(%s) AS reason", (Jsonb(setup["record_json"]),)
            ).fetchone()["reason"]
            # ``reason`` is terminal. ``capacity`` (the account is full right now) keeps the
            # setup WATCHING under a policy with a cooldown, and only when nothing else fails.
            reason = daily_failure or reason or eligibility["reason"] or review_failure
            levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
            m, s = levels["max_entry_price"], levels["stop"]
            # JEV_MANAGED_RISK_V3 crypto (migration 022) sizes in equity slices and checks the
            # account on the actual planned risk (``_slice_entry``); other markets and policies
            # keep the row's fixed budget below.
            terms = policy.terms(setup["market"])
            budget = equity * policy.risk_pct
            capacity = None
            if not reason and terms is None:
                # The one SQL account-risk check; the reservation trigger asks the same question.
                capacity = account_risk_failure(
                    conn,
                    policy.policy_id,
                    setup["market"],
                    classification["sector"],
                    classification["theme"],
                    equity,
                    budget,
                )
                if capacity is not None and capacity not in CAPACITY_REASONS:
                    reason, capacity = capacity, None
            # A US entry is a day position: a DAY bracket whose position is flattened at the
            # official close minus five minutes. Only then may the policy's intraday multiple
            # apply; crypto is always cash-only.
            day_position = (
                setup["market"] == "US_STOCKS"
                and session is not None
                and session.flatten_time == session.closes - timedelta(minutes=5)
                and now < session.flatten_time
            )
            multiple = (
                policy.stock_multiple(day_position) if setup["market"] == "US_STOCKS" else D(1)
            )
            # Capital is reserved conservatively across both engines' reservations.
            reserved_notional = sum((r["qty"] * r["max_entry"] for r in reservations), D(0))
            if multiple > 1:
                available = max(D(0), multiple * equity - reserved_notional)
            else:
                available = max(D(0), min(equity, num(account["cash"]) - reserved_notional))
            if setup["market"] == "CRYPTO":
                cash_only = broker_amount(account.get("non_marginable_buying_power"))
                available = min(available, cash_only if cash_only is not None else D(0))
            risk_qty = budget / (m - s)
            qty = min(risk_qty, available / m)
            binding = "RISK" if risk_qty <= available / m else "CAPITAL"
            sizing = slice_asset = None
            if terms is not None:
                qty, budget, binding, sizing, slice_asset, reason, capacity = self._slice_entry(
                    conn, policy, terms, setup, classification, account, reservations, asset,
                    equity, m, s, reason,
                )
            if liquidity is not None:
                if (liquidity.get("passed") is not True or not 0 <= (now - datetime.fromisoformat(
                        liquidity["observed_at"])).total_seconds() <= 5):
                    reason = reason or liquidity.get("reason") or "CRYPTO_LIQUIDITY_STALE"
                elif qty * m > num(liquidity["maximum_entry_notional"]):
                    reason = reason or "CRYPTO_VENUE_PARTICIPATION_LIMIT"
                self.store.event(conn, "CRYPTO_LIQUIDITY", liquidity, setup_id=setup_id)
            payload = {}
            if setup["market"] == "CRYPTO" and terms is not None:
                if not reason and not capacity:
                    try:  # Already on the coin's quantity increment (``_slice_entry``).
                        payload = build_limit_entry(slice_asset, qty, m,
                                                    operation_key=str(setup_id))
                    except CryptoExecutionError as error:
                        reason = str(error)
            elif setup["market"] == "CRYPTO":
                try:
                    ca = CryptoAsset.from_broker(asset)
                    risk_lots = (risk_qty / ca.min_trade_increment).to_integral_value(
                        rounding=ROUND_DOWN
                    ) * ca.min_trade_increment
                    qty = (qty / ca.min_trade_increment).to_integral_value(
                        rounding=ROUND_DOWN
                    ) * ca.min_trade_increment
                    if qty < ca.min_order_size:
                        if risk_lots >= ca.min_order_size:
                            capacity = capacity or "INSUFFICIENT_BUYING_POWER"  # Capital-limited.
                        else:
                            reason = reason or "ZERO_SHARE_SIZE"
                    if not reason and not capacity:
                        payload = build_limit_entry(ca, qty, m, operation_key=str(setup_id))
                except CryptoExecutionError as error:
                    # Venue metadata or precision refuses this entry; it never escapes the tick.
                    reason = reason or str(error)
                    payload = {}
            else:
                qty = qty.to_integral_value(rounding=ROUND_DOWN)
                if qty < 1:
                    if risk_qty >= 1:
                        capacity = capacity or "INSUFFICIENT_BUYING_POWER"  # Capital-limited.
                    else:
                        reason = reason or "ZERO_SHARE_SIZE"
                if asset.get("class") != "us_equity" or asset.get("tradable") is not True:
                    reason = reason or "ASSET_NOT_TRADABLE"
                if not reason and not capacity:
                    payload = {
                        "symbol": setup["symbol"],
                        "qty": str(qty),
                        "side": "buy",
                        "type": "limit",
                        "limit_price": str(m),
                        "time_in_force": "day",
                        "order_class": "bracket",
                        "stop_loss": {"stop_price": str(s)},
                        "take_profit": {"limit_price": str(levels["target"])},
                        "client_order_id": "cl-managed-" + str(setup_id).replace("-", ""),
                    }
            buying_power = None
            if not reason and not capacity:
                if multiple > 1 and (
                    payload.get("time_in_force") != "day" or payload.get("order_class") != "bracket"
                ):
                    reason = "DAY_POSITION_REQUIRED"
                else:
                    # After sizing: reject, never resize, against the broker's own figures.
                    buying_power = buying_power_check(
                        account,
                        market=setup["market"],
                        notional=qty * m,
                        equity=equity,
                        multiple=multiple,
                    )
                    if buying_power.failure in CAPACITY_REASONS:
                        capacity = buying_power.failure
                    else:
                        reason = buying_power.failure
            defer = (
                not reason
                and capacity is not None
                and policy.capacity_cooldown_seconds is not None
            )
            reason = reason or capacity
            if reason:
                payload = {}
                binding = binding_constraint(reason)
            until = now + timedelta(seconds=policy.capacity_cooldown_seconds) if defer else None
            self.store.event(
                conn, "TRIGGER_CONFIRMED", self._trigger_confirmed(setup, state, observation, now),
                setup_id=setup_id,
            )
            self.store.event(conn, "ENTRY_ELIGIBILITY", eligibility, setup_id=setup_id)
            self.store.event(conn, "RISK_CHECK", {"equity": equity, "qty": qty}, setup_id=setup_id)
            if defer:
                # Non-terminal: the setup keeps WATCHING and its ticker/day attempt; the frozen
                # touch and ask-at-or-below-M conditions still bind any later entry.
                state = self.store.transition(
                    conn,
                    setup_id,
                    "WATCHING",
                    capacity_deferred_until=until.isoformat(),
                    capacity_deferred_reason=reason,
                )
            else:
                cleared = (
                    {"capacity_deferred_until": None, "capacity_deferred_reason": None}
                    if state.get("capacity_deferred_until")
                    else {}
                )
                state = self.store.transition(
                    conn,
                    setup_id,
                    "RISK_REJECTED" if reason else "ENTRY_PENDING",
                    reason=reason,
                    requested_qty=str(qty),
                    entry_client_id=payload.get("client_order_id"),
                    quote=observation,
                    **cleared,
                )
            context = {
                # CRYPTO_ALPACA_TRIGGER_V1: the read time, with both times; others unchanged.
                **crypto_trigger.decision_quote(state, observation),
                "crypto_liquidity": liquidity,
                "session_date": now.astimezone(NY).date(),
                "entry_deadline": min(
                    setup["expires_at"],
                    session.flatten_time if session else setup["expires_at"],
                    datetime.fromisoformat(state["crypto_entry_deadline"])
                    if state.get("crypto_entry_deadline") else setup["expires_at"],
                ),
                "session_open": session.opens if session else None,
                "market": setup["market"],
                "risk_policy_id": policy.policy_id,
                "venue": VENUE,
                "budget": budget,
                **({"sizing": sizing} if sizing is not None else {}),
                "day_position": day_position,
                "buying_power_multiple": multiple,
                "binding_constraint": binding,
                "buying_power": buying_power.evidence if buying_power else None,
                "account_margin": {k: account.get(k) for k in MARGIN_FIELDS},
                "capacity_deferred_until": until,
            }
            decision = self._decision(
                conn,
                setup,
                state,
                "ENTRY",
                payload,
                context,
                equity=equity,
                approved=not reason,
                reason=reason or "RISK_APPROVED",
            )
            if defer:
                # Once per reason and cooldown window; the skip above makes a repeat impossible.
                window = int(now.timestamp()) // policy.capacity_cooldown_seconds
                key = f"capacity-deferred:{setup_id}:{reason}:{window}"
                if not conn.execute(
                    "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
                ).fetchone():
                    self.store.event(
                        conn,
                        "RISK_CAPACITY_DEFERRED",
                        {
                            "reason": reason,
                            "until": until,
                            "decision_id": decision["decision_id"],
                            "risk_policy_id": policy.policy_id,
                            "cooldown_seconds": policy.capacity_cooldown_seconds,
                        },
                        setup_id=setup_id,
                        key=key,
                    )
            if not reason:
                conn.execute(
                    """INSERT INTO lab.managed_reservations(setup_id,decision_id,budget,
                    planned_risk,qty,max_entry,sector,theme) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        setup_id,
                        decision["decision_id"],
                        budget,
                        qty * (m - s),
                        qty,
                        m,
                        classification["sector"],
                        classification["theme"],
                    ),
                )
        if not reason:
            self.dispatch(decision)
            self._after_entry_dispatch(decision)
        return decision

    def _after_entry_dispatch(self, decision):
        """A broker margin refusal of an entry forces re-reconciliation before any further
        entry; it never latches a halt by itself (plan 2.3, recommended ruling 16)."""
        with self.repo.connect() as conn:
            row = conn.execute(
                """SELECT body FROM lab.managed_events WHERE setup_id=%s
                AND kind='BROKER_REJECTED' AND body->>'decision_id'=%s
                ORDER BY event_seq DESC LIMIT 1""",
                (decision["setup_id"], str(decision["decision_id"])),
            ).fetchone()
        if row and row["body"].get("reason") == "BROKER_MARGIN_REJECTED":
            self.reconciled_at = None
            self._event(
                "MARGIN_REJECTION_RECONCILE_REQUIRED",
                {"decision_id": str(decision["decision_id"]), "reason": "BROKER_MARGIN_REJECTED"},
                decision["setup_id"],
                key="margin-reconcile:" + str(decision["decision_id"]),
            )

    def dispatch(self, decision):
        """Recover pending decisions without issuing a new client order ID.

        A gate refusal sent nothing: it is recorded as AUTHORIZATION_NOT_CLAIMED, never
        latches, and the next tick decides again from fresh broker state. A rejected price
        amendment reverts the desired levels to the broker-acknowledged ones and never
        requests an exit; only a rejected protective order (which is proposed solely for
        inventory without protection) requests one. A rejected close schedules its retry
        (``_exit_refused``).
        """
        if decision["outcome"] != "APPROVED":
            return None
        try:
            response = self.broker.mutate(
                decision["method"], decision["path"], decision["payload"], decision["decision_id"]
            )
        except SubmissionDisabled as exc:
            return self._authorization_not_claimed(decision, exc)
        except BrokerMutationUnknown:
            self._event(
                "BROKER_UNKNOWN", {"decision_id": decision["decision_id"]}, decision["setup_id"]
            )
            return self.recover(decision)
        except BrokerMutationRejected as exc:
            # The transport's classified reason; this controller never parses broker bodies.
            reason = getattr(exc, "reason", None) or str(exc)
            self._event(
                "BROKER_REJECTED",
                {"decision_id": decision["decision_id"], "reason": reason},
                decision["setup_id"],
            )
            if decision["action"] == "AMEND":
                self._amendment_rejected(decision, reason)
            elif decision["action"] == "PROTECT":
                with self.store.transaction() as conn:
                    current = self.store.state(conn, decision["setup_id"])
                    if self._partial_entry_protection_refused(conn, decision, current, reason):
                        return None
                    self.store.transition(
                        conn,
                        decision["setup_id"],
                        current["state"],
                        exit_requested="PROTECTION_REJECTED",
                    )
            elif decision["action"] == "EXIT":
                self._exit_refused(decision, reason)
            if decision["action"] == "ENTRY":
                with self.store.transaction() as conn:
                    # State name unchanged; the reason is the classified broker code.
                    self.store.transition(conn, decision["setup_id"], "REJECTED", reason=reason)
                    self._release(conn, decision["setup_id"], "BROKER_REJECTED")
            return None
        self._ack(decision, response)
        if decision["action"] == "ENTRY":
            with self.store.transaction() as conn:
                state = self.store.state(conn, decision["setup_id"])
                if state["state"] == "ENTRY_PENDING":
                    self.store.transition(conn, decision["setup_id"], "ORDER_SUBMITTED")
        return response

    def _authorization_not_claimed(self, decision, exc):
        """The gate refused before any broker request: record it, never latch or resend it.

        A concurrent dispatcher that already claimed the decision owns it, so its broker
        state is recovered instead. An unclaimed entry is final (its reservation is
        released). An unclaimed POST spent its client order ID (unique per approved POST),
        so a new state revision gives the next attempt a fresh one; a PATCH or DELETE is
        simply authorized again by the next tick.
        """
        sid, decision_id = decision["setup_id"], str(decision["decision_id"])
        with self.repo.connect() as conn:
            claimed = conn.execute(
                "SELECT 1 FROM lab.managed_claims WHERE decision_id=%s", (decision_id,)
            ).fetchone()
        if claimed:
            return self.recover(decision)
        code = error_code(exc)
        with self.store.transaction() as conn:
            self.store.event(
                conn,
                "AUTHORIZATION_NOT_CLAIMED",
                {"decision_id": decision_id, "action": decision["action"], "code": code},
                setup_id=sid,
                key="not-claimed:" + decision_id,
            )
            current = self.store.state(conn, sid)
            if decision["action"] == "ENTRY":
                if current.get("state") == "ENTRY_PENDING":
                    self.store.transition(conn, sid, "RISK_REJECTED", reason=code)
                    self._release(conn, sid, "AUTHORIZATION_NOT_CLAIMED")
            elif decision["method"] == "POST" and current.get("state") not in TERMINAL:
                self.store.transition(
                    conn, sid, current["state"], authorization_not_claimed=code
                )
        return None

    def _exit_refused(self, decision, reason):
        """Schedule the retry of a close the broker refused (plan phase 0, 2026-09-26).

        One transaction: an EXIT_REFUSED event, the durable EXIT_REFUSAL_ALARM at the
        EXIT_REFUSAL_ALARM_THRESHOLD-th consecutive refusal of a working setup, and one new
        state revision carrying ``exit_refusals``, ``exit_retry_after`` and ``exit_retry_of``.
        That revision gives the retry a fresh client order ID (an approved POST's ID is never
        reused). No close is authorized before ``exit_retry_after``; the retry then needs its
        own one-use five-second authorization. Nothing is sent here.
        """
        sid, decision_id = decision["setup_id"], str(decision["decision_id"])
        key = "exit-refused:" + decision_id
        now = self.now()
        with self.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone():
                return
            state = self.store.state(conn, sid)
            working = state.get("state") not in TERMINAL
            refusals = int(state.get("exit_refusals") or 0) + 1
            delay = exit_retry_delay(refusals)
            retry_after = now + timedelta(seconds=delay)
            body = {
                "decision_id": decision_id,
                "reason": reason,
                "exit_requested": state.get("exit_requested"),
                "refusals": refusals,
                "retry_delay_seconds": delay,
                "retry_after": retry_after.isoformat(),
            }
            self.store.event(conn, "EXIT_REFUSED", body, setup_id=sid, key=key)
            if not working:
                return
            if refusals == EXIT_REFUSAL_ALARM_THRESHOLD:
                self.store.event(
                    conn,
                    "EXIT_REFUSAL_ALARM",
                    {
                        **body,
                        "alarm": "EXIT_REFUSED_REPEATEDLY",
                        "threshold": EXIT_REFUSAL_ALARM_THRESHOLD,
                    },
                    setup_id=sid,
                    key="exit-refusal-alarm:" + decision_id,
                )
            self.store.transition(
                conn,
                sid,
                state["state"],
                exit_refusals=refusals,
                exit_retry_after=retry_after.isoformat(),
                exit_retry_of=decision_id,
            )

    def _exit_accepted(self, setup_id):
        """An accepted close ends its setup's refusal streak, and with it the status alarm."""
        with self.store.transaction() as conn:
            state = self.store.state(conn, setup_id)
            if state.get("exit_refusals") and state.get("state") not in TERMINAL:
                self.store.transition(
                    conn, setup_id, state["state"], exit_refusals=0, exit_retry_after=None
                )

    def _close_permitted(self, setup_id, client_order_id, id_revision):
        """Whether a close (market sell) under ``client_order_id`` may be authorized now.

        Never before ``exit_retry_after`` (the refused-close backoff). An ID that an approved
        POST already used can never be sent again: when it is the current revision's (a
        close accepted and later cancelled or expired, one never sent, or one refused before
        the backoff existed) one new state revision is recorded instead, so the close goes out
        on a later tick under a fresh ID and its own authorization. Nothing is sent here.
        """
        now = self.now()
        with self.store.transaction() as conn:
            state = self.store.state(conn, setup_id)
            if state.get("state") in TERMINAL:
                return False
            retry_after = state.get("exit_retry_after")
            if retry_after and now < datetime.fromisoformat(retry_after):
                return False
            spent = conn.execute(
                """SELECT decision_id FROM lab.managed_risk_decisions WHERE outcome='APPROVED'
                AND method='POST' AND payload->>'client_order_id'=%s""",
                (client_order_id,),
            ).fetchone()
            if spent is None:
                return True
            if state.get("revision") == id_revision:
                self.store.transition(
                    conn, setup_id, state["state"], exit_retry_of=str(spent["decision_id"])
                )
        return False

    def _acknowledged_levels(self, conn, setup, state):
        """The stop and target the broker last acknowledged for this setup.

        Stock levels come from the bracket legs of the entry acknowledgement, then from
        each acknowledged price amendment (the broker's replacement order). The crypto stop
        is the latest acknowledged native stop-limit; the app-managed crypto target has no
        broker order, so the level in force before a pending amendment stands.
        """
        stop = num(state.get("amendment_previous_stop") or state["stop"])
        target = num(state.get("amendment_previous_target") or state["target"])
        rows = conn.execute(
            """SELECT d.action,d.payload,e.body->'order' AS acknowledged
            FROM lab.managed_risk_decisions d JOIN lab.managed_events e
              ON e.idempotency_key='ack:'||d.decision_id::text
            WHERE d.setup_id=%s AND d.outcome='APPROVED' AND d.action IN ('ENTRY','PROTECT','AMEND')
            ORDER BY e.event_seq""",
            (setup["setup_id"],),
        ).fetchall()
        for row in rows:
            payload, order = row["payload"], row["acknowledged"] or {}
            if row["action"] == "ENTRY" and payload.get("order_class") == "bracket":
                legs = {
                    leg.get("type"): leg for leg in order.get("legs") or [] if isinstance(leg, dict)
                }
                stop = num(
                    legs.get("stop", {}).get("stop_price") or payload["stop_loss"]["stop_price"]
                )
                target = num(
                    legs.get("limit", {}).get("limit_price")
                    or payload["take_profit"]["limit_price"]
                )
            elif row["action"] == "PROTECT":
                stop = num(order.get("stop_price") or payload["stop_price"])
            elif row["action"] == "AMEND":
                if "stop_price" in payload:
                    stop = num(order.get("stop_price") or payload["stop_price"])
                if "limit_price" in payload:
                    target = num(order.get("limit_price") or payload["limit_price"])
        return stop, target

    def _amendment_rejected(self, decision, reason):
        """A rejected price amendment is final: no retry and no exit.

        The desired levels revert to the broker-acknowledged ones, so the next tick finds
        nothing to amend, and the model plan that asked for it is closed.
        """
        sid = decision["setup_id"]
        with self.store.transaction() as conn:
            current = self.store.state(conn, sid)
            if self._stop_replace_refused(conn, decision, current, reason):
                return
            setup = self.store.setup(conn, sid)
            stop, target = self._acknowledged_levels(conn, setup, current)
            self.store.event(
                conn,
                "AMENDMENT_REJECTED",
                {
                    "decision_id": str(decision["decision_id"]),
                    "path": decision["path"],
                    "requested": decision["payload"],
                    "reason": reason,
                    "context_hash": current.get("amendment_context_hash"),
                    "reverted_to": {"stop": stop, "target": target},
                },
                setup_id=sid,
                key="amendment-rejected:" + str(decision["decision_id"]),
            )
            if current.get("state") not in TERMINAL:
                self.store.transition(
                    conn,
                    sid,
                    current["state"],
                    stop=str(stop),
                    target=str(target),
                    amendment_expires_at=None,
                    amendment_context_hash=None,
                )

    def _ack(self, decision, response):
        from catalyst_lab.managed_broker import sanitize_broker_payload

        safe, _ = sanitize_broker_payload(response)
        with self.repo.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                ("ack:" + str(decision["decision_id"]),),
            ).fetchone():
                return
        self._event(
            "BROKER_ACK",
            {
                "decision_id": str(decision["decision_id"]),
                "action": decision["action"],
                "order": safe,
            },
            decision["setup_id"],
            key="ack:" + str(decision["decision_id"]),
        )
        if decision["action"] == "EXIT":
            self._exit_accepted(decision["setup_id"])

    def _follow_replacement(self, order, fetch=None):
        seen = set()
        while order and order.get("replaced_by"):
            next_id = order["replaced_by"]
            if next_id in seen or len(seen) >= 10:
                raise ValueError("BROKER_REPLACEMENT_CHAIN_INVALID")
            seen.add(next_id)
            latest = (fetch or self.broker.order)(next_id)
            if not latest:
                raise ValueError("BROKER_REPLACEMENT_NOT_RECONCILED")
            order = latest
        return order

    def recover(self, decision):
        if decision["method"] in {"DELETE", "PATCH"}:
            order = self.broker.order(decision["path"].rsplit("/", 1)[1])
        else:
            order = self.broker.order_by_client_id(decision["payload"].get("client_order_id"))
        if order:
            self._ack(decision, order)
            return order
        self._event(
            "BROKER_RECONCILE_REQUIRED",
            {
                "decision_id": str(decision["decision_id"]),
                "reason": "UNKNOWN_SUBMISSION_NO_RESUBMIT",
            },
            decision["setup_id"],
            key="reconcile-required:" + str(decision["decision_id"]),
        )
        return None

    def _recover_change(self, decision, claimed):
        """Resolve an unacknowledged PATCH or DELETE after a crash or an unknown response.

        A claimed one may have reached the broker: its order is looked up and acknowledged
        as found, never resent. An unclaimed one never left the gate: once its five-second
        authorization has expired it is closed, so the same change may be authorized again.
        """
        if claimed:
            self.recover(decision)
        elif decision["expires_at"] <= self.now():
            self._event(
                "UNSENT_AUTHORIZATION_EXPIRED",
                {"decision_id": decision["decision_id"]},
                decision["setup_id"],
                key="unsent:" + str(decision["decision_id"]),
            )

    def _release(self, conn, setup_id, reason):
        conn.execute(
            """INSERT INTO lab.managed_releases(setup_id,reason)
            SELECT setup_id,%s FROM lab.managed_active_reservations WHERE setup_id=%s
            ON CONFLICT(setup_id) DO NOTHING""",
            (reason, setup_id),
        )

    def ingest(self, raw):
        from catalyst_lab.managed_broker import normalize_trade_update

        update = normalize_trade_update(raw)
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM lab.managed_setups WHERE replace(symbol,'/','')=%s",
                (normalized_symbol(update.symbol),),
            ).fetchall()
            matched = None
            for row in rows:
                decisions = conn.execute(
                    "SELECT payload FROM lab.managed_risk_decisions WHERE setup_id=%s",
                    (row["setup_id"],),
                ).fetchall()
                clients = {r["payload"].get("client_order_id") for r in decisions}
                acks = conn.execute(
                    "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind='BROKER_ACK'",
                    (row["setup_id"],),
                ).fetchall()
                ids = {
                    r["body"]["broker_order_id"]
                    for r in conn.execute(
                        "SELECT body FROM lab.managed_events WHERE setup_id=%s "
                        "AND kind='BROKER_ORDER_LINK'",
                        (row["setup_id"],),
                    ).fetchall()
                }
                for ack in acks:
                    order = ack["body"].get("order", {})
                    ids.add(order.get("id"))
                    ids.update(x.get("id") for x in order.get("legs", []) or [])
                if update.client_order_id in clients or update.broker_order_id in ids:
                    matched = row
            if not matched:
                self.store.event(
                    conn,
                    "UNMATCHED_BROKER_EVENT",
                    {
                        "event_id": update.event_id,
                        "broker_order_id": update.broker_order_id,
                        "payload": update.payload,
                        "redacted_paths": list(update.redacted_paths),
                    },
                    key="unmatched:" + update.event_id,
                )
                return False
            sid = matched["setup_id"]
            self.store.event(
                conn, "BROKER_EVENT", update.payload, setup_id=sid, key="broker:" + update.event_id
            )
            if update.position_qty is not None:
                self.store.event(
                    conn,
                    "BROKER_POSITION",
                    {
                        "qty": str(update.position_qty),
                        "symbol": update.symbol,
                        "broker_event_id": update.event_id,
                        "occurred_at": update.occurred_at.isoformat(),
                    },
                    setup_id=sid,
                    key="inventory:" + update.event_id,
                )
            if update.fill_qty is not None:
                existing = conn.execute(
                    "SELECT * FROM lab.managed_fills WHERE fill_id=%s", (update.execution_id,)
                ).fetchone()
                if existing and (
                    existing["qty"] != update.fill_qty or existing["price"] != update.fill_price
                ):
                    raise ValueError("CONFLICTING_BROKER_EXECUTION")
                # FILL activities carry no execution id: a fill already recorded at this
                # (broker order id, cumulative quantity), e.g. by the REST backfill after a
                # stream gap, is this execution delivered again, not a new one.
                covered = None if existing else self._fill_at_cumulative(
                    conn, sid, update.broker_order_id, update.cumulative_fill_qty
                )
                if covered is not None and (
                    covered["qty"] != update.fill_qty or covered["price"] != update.fill_price
                ):
                    raise ValueError("CONFLICTING_BROKER_EXECUTION")
                if not existing and covered is None:
                    conn.execute(
                        """INSERT INTO lab.managed_fills(fill_id,setup_id,broker_order_id,
                        side,qty,price,filled_at,fee_usd,source)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'ALPACA_PAPER')""",
                        (
                            update.execution_id,
                            sid,
                            update.broker_order_id,
                            update.side,
                            update.fill_qty,
                            update.fill_price,
                            update.occurred_at,
                            explicit_fill_fee_usd(update.payload),
                        ),
                    )
        return True

    def replay_unmatched(self):
        with self.repo.connect() as conn:
            pending = conn.execute("""SELECT body FROM lab.managed_events e
                WHERE kind='UNMATCHED_BROKER_EVENT' AND NOT EXISTS(
                  SELECT 1 FROM lab.managed_events x WHERE x.idempotency_key=
                    'broker:'||(e.body->>'event_id')) ORDER BY event_seq LIMIT 500""").fetchall()
        return sum(self.ingest(r["body"]["payload"]) for r in pending)

    # --- REST fill backfill after a trade-updates gap (plan 4.4) -------------

    @staticmethod
    def _fill_at_cumulative(conn, setup_id, broker_order_id, cumulative):
        """The recorded fill that brought ``broker_order_id`` to ``cumulative``, if any.

        REST fills are keyed by that pair; a stream fill's cumulative quantity is the
        order's ``filled_qty`` in the same BROKER_EVENT (compared as Decimal, never cast).
        """
        from catalyst_lab.broker_ledger import rest_fill_id

        rest = conn.execute(
            "SELECT * FROM lab.managed_fills WHERE fill_id=%s",
            (rest_fill_id(broker_order_id, cumulative),),
        ).fetchone()
        if rest is not None:
            return rest
        rows = conn.execute(
            """SELECT f.*,coalesce(e.body->'data',e.body)->'order'->>'filled_qty' AS cumulative
            FROM lab.managed_events e JOIN lab.managed_fills f
              ON f.fill_id=coalesce(e.body->'data',e.body)->>'execution_id'
            WHERE e.setup_id=%s AND e.kind='BROKER_EVENT' AND f.broker_order_id=%s""",
            (setup_id, broker_order_id),
        ).fetchall()
        for row in rows:
            try:
                if D(str(row["cumulative"])) == cumulative:
                    return row
            except (ArithmeticError, ValueError, TypeError):
                continue
        return None

    def _backfill_scope(self, lookback):
        """``(after, order→setup links, known order states, open linked order ids)``.

        ``after`` is the last recorded broker evidence less ``lookback``, or ``None`` when
        there is none (nothing can have been missed). Known states are the (order id,
        status, filled quantity) triples already recorded for active setups.
        """
        with self.repo.connect() as conn:
            anchor = conn.execute(
                """SELECT max(recorded_at) AS at FROM lab.managed_events WHERE kind IN
                ('BROKER_EVENT','UNMATCHED_BROKER_EVENT','BROKER_REST_BACKFILL')"""
            ).fetchone()["at"]
            if anchor is None:
                anchor = conn.execute(
                    "SELECT min(recorded_at) AS at FROM lab.managed_events WHERE kind='BROKER_ACK'"
                ).fetchone()["at"]
            links = {}
            for row in conn.execute(
                """SELECT setup_id,kind,body FROM lab.managed_events
                WHERE kind IN ('BROKER_ORDER_LINK','BROKER_ACK') ORDER BY event_seq"""
            ).fetchall():
                body = row["body"] if isinstance(row["body"], dict) else {}
                if row["kind"] == "BROKER_ORDER_LINK":
                    ids = [body.get("broker_order_id")]
                else:
                    order = body.get("order") if isinstance(body.get("order"), dict) else {}
                    legs = [leg for leg in order.get("legs") or [] if isinstance(leg, dict)]
                    ids = [order.get("id"), order.get("replaced_by"), *(x.get("id") for x in legs)]
                for order_id in ids:
                    if isinstance(order_id, str) and order_id:
                        links.setdefault(order_id, row["setup_id"])
            active = [s["setup_id"] for s in self.store.active()]
            evidence = conn.execute(
                """SELECT setup_id,kind,body FROM lab.managed_events
                WHERE setup_id=ANY(%s::uuid[])
                AND kind IN ('BROKER_EVENT','BROKER_ACK','BROKER_REST_BACKFILL')
                ORDER BY event_seq""",
                (active,),
            ).fetchall()
        seen, latest = set(), {}

        def note(order, status=None, filled=None):
            if not isinstance(order, dict) or not isinstance(order.get("id"), str):
                return
            status = status or order.get("status")
            try:
                filled = D(str(filled if filled is not None else order.get("filled_qty", "0")))
            except (ArithmeticError, ValueError, TypeError):
                return
            seen.add((order["id"], status, filled))
            latest[order["id"]] = status

        for row in evidence:
            body = row["body"] if isinstance(row["body"], dict) else {}
            if row["kind"] == "BROKER_EVENT":
                inner = body.get("data") if isinstance(body.get("data"), dict) else body
                note(inner.get("order"))
            elif row["kind"] == "BROKER_ACK":
                order = body.get("order")
                note(order)
                for leg in (order or {}).get("legs") or []:
                    note(leg)
            elif isinstance(body.get("order"), dict):
                note(body["order"])
            elif isinstance(body.get("activity"), dict):
                activity = body["activity"]
                note({"id": activity.get("order_id")}, activity.get("order_status"),
                     activity.get("cum_qty"))
        active_ids = {str(sid) for sid in active}
        open_ids = [
            order_id for order_id, setup_id in links.items()
            if str(setup_id) in active_ids and latest.get(order_id) not in BROKER_TERMINAL
        ]
        after = anchor - lookback if anchor is not None else None
        return after, links, seen, open_ids

    def rest_backfill(self, *, lookback=None):
        """Record what the trade-updates stream missed, from REST, before reconciliation.

        Reads (the caller declares the protective-and-recovery budget class): the
        non-terminal linked orders of active setups, then every FILL activity since the
        last recorded broker evidence less five minutes, then positions when a fill was
        recorded. Writes one BROKER_REST_BACKFILL event per new fill (with its
        ``managed_fills`` row, deduplicated on broker order id and cumulative quantity
        against stream fills) and per new order state, and a REST-sourced BROKER_POSITION
        for each setup whose recorded fills explain the broker position. Nothing is
        adopted: activities of unknown orders are left to reconciliation. Any read or
        content failure raises before reconciliation, which then stays unready.
        """
        from catalyst_lab.broker_ledger import (
            BACKFILL_LOOKBACK,
            REST_BACKFILL_EVENT,
            REST_FILL_SOURCE,
            REST_ORDER_SOURCE,
            normalize_fill_activity,
            quantity_key,
            rest_fill_id,
        )
        from catalyst_lab.managed_broker import sanitize_broker_payload

        lookback = BACKFILL_LOOKBACK if lookback is None else lookback
        after, links, seen, open_ids = self._backfill_scope(lookback)
        summary = {"window_after": after, "orders_read": 0, "activities_read": 0,
                   "fills_recorded": 0, "fills_known": 0, "orders_recorded": 0,
                   "positions_recorded": 0, "positions_unexplained": [],
                   "unlinked_activities": 0}
        if after is None:
            return summary
        backfill_id = str(uuid4())
        orders = [order for order in map(self.broker.order, open_ids) if order]
        activities = self.broker.fill_activities_since(after)
        summary.update(backfill_id=backfill_id, orders_read=len(orders),
                       activities_read=len(activities))
        owned = []
        for raw in activities:
            if not isinstance(raw, dict) or raw.get("order_id") not in links:
                summary["unlinked_activities"] += 1  # Not ours: reconciliation decides.
                continue
            owned.append((raw, normalize_fill_activity(raw)))
        owned.sort(key=lambda pair: (pair[1]["at_ns"], pair[1]["cumulative"]))
        changed, recorded = {}, {}
        for raw, item in owned:
            setup_id, order_id = links[item["order_id"]], item["order_id"]
            with self.store.transaction() as conn:
                setup = self.store.setup(conn, setup_id)
                if normalized_symbol(item["symbol"]) != normalized_symbol(setup["symbol"]):
                    raise ValueError("BACKFILL_ACTIVITY_SYMBOL_MISMATCH")
                fill_id = rest_fill_id(order_id, item["cumulative"])
                covered = self._fill_at_cumulative(conn, setup_id, order_id, item["cumulative"])
                if covered is not None:
                    if covered["qty"] != item["qty"] or covered["price"] != item["price"]:
                        raise ValueError("CONFLICTING_BROKER_EXECUTION")
                    summary["fills_known"] += 1
                    recorded[order_id] = max(recorded.get(order_id, D(0)), item["cumulative"])
                    continue
                safe, _ = sanitize_broker_payload(raw)
                self.store.event(
                    conn,
                    REST_BACKFILL_EVENT,
                    {"source": REST_FILL_SOURCE, "backfill_id": backfill_id,
                     "window_after": after.isoformat(), "broker_order_id": order_id,
                     "cumulative_qty": str(item["cumulative"]), "fill_id": fill_id,
                     "activity": safe},
                    setup_id=setup_id,
                    key=fill_id,
                )
                conn.execute(
                    """INSERT INTO lab.managed_fills(fill_id,setup_id,broker_order_id,
                    side,qty,price,filled_at,fee_usd,source)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,'ALPACA_PAPER_REST_BACKFILL')""",
                    (fill_id, setup_id, order_id, item["side"], item["qty"], item["price"],
                     item["at"]),
                )
            summary["fills_recorded"] += 1
            recorded[order_id] = max(recorded.get(order_id, D(0)), item["cumulative"])
            seen.add((order_id, item["order_status"], item["cumulative"]))
            entry = changed.setdefault(setup_id, {"signed": D(0), "bought": D(0), "at": None})
            entry["signed"] += item["qty"] if item["side"] == "buy" else -item["qty"]
            entry["bought"] += item["qty"] if item["side"] == "buy" else D(0)
            entry["at"] = item["at"]
        for order in orders:
            order_id, status = order.get("id"), order.get("status")
            try:
                filled = D(str(order.get("filled_qty", "0")))
            except (ArithmeticError, ValueError, TypeError):
                raise ValueError("INVALID_BACKFILL_ORDER") from None
            if (
                order_id not in links
                or not isinstance(status, str)
                or (order_id, status, filled) in seen
                or filled < recorded.get(order_id, D(0))  # Older than recorded fills.
            ):
                continue
            state = {k: order.get(k) for k in (
                "id", "client_order_id", "symbol", "side", "type", "qty", "filled_qty",
                "status", "limit_price", "stop_price", "updated_at", "replaced_by",
            )}
            key = f"rest-order:{order_id}:{status}:{quantity_key(filled)}"
            with self.store.transaction() as conn:
                if conn.execute(
                    "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
                ).fetchone():
                    continue  # This state was recorded by an earlier backfill.
                self.store.event(
                    conn,
                    REST_BACKFILL_EVENT,
                    {"source": REST_ORDER_SOURCE, "backfill_id": backfill_id,
                     "window_after": after.isoformat(), "order": state},
                    setup_id=links[order_id],
                    key=key,
                )
            summary["orders_recorded"] += 1
        if changed:
            summary["positions_recorded"], summary["positions_unexplained"] = (
                self._rest_positions(changed, backfill_id)
            )
        if summary["fills_recorded"] or summary["orders_recorded"] or (
            summary["positions_unexplained"]
        ):
            self._event(
                "BROKER_REST_BACKFILL_COMPLETED", summary, key=f"rest-backfill:{backfill_id}"
            )
        return summary

    def _backfill_unrecorded_entry_fill(self, setup_id):
        """One REST fill backfill (``rest_backfill``) when the broker holds the setup's coins but
        no buy fill of it is recorded yet; without it, or on any failure, the fail-closed
        first-fill rule applies exactly as before (``CRYPTO_FIRST_FILL_TIME_UNAVAILABLE``)."""
        with self.repo.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM lab.managed_fills WHERE setup_id=%s AND side='buy' LIMIT 1",
                (setup_id,),
            ).fetchone():
                return None
        try:
            return self.rest_backfill()
        except Exception:
            return None

    def _rest_positions(self, changed, backfill_id):
        """REST-sourced BROKER_POSITION for each setup whose new fills explain the broker
        position; the rest stay unexplained, so reconciliation keeps entries blocked."""
        positions = self.broker.positions()  # Read after the activities; never a snapshot.
        recorded, unexplained = 0, []
        for setup_id, change in changed.items():
            with self.store.transaction() as conn:
                setup = self.store.setup(conn, setup_id)
                previous = conn.execute(
                    """SELECT body->>'qty' AS qty FROM lab.managed_events
                    WHERE setup_id=%s AND kind='BROKER_POSITION'
                    ORDER BY (body->>'occurred_at')::timestamptz DESC NULLS LAST,
                             event_seq DESC LIMIT 1""",
                    (setup_id,),
                ).fetchone()
                explained = (num(previous["qty"]) if previous else D(0)) + change["signed"]
                broker_qty = sum(
                    (num(p["qty"]) for p in positions
                     if normalized_symbol(p["symbol"]) == normalized_symbol(setup["symbol"])),
                    D(0),
                )
                shortfall = explained - broker_qty
                if setup["market"] == "CRYPTO" and change["bought"] > 0:
                    # A crypto buy's fee may be taken in the asset: less, never more, and never
                    # by more than a fee could be. A larger gap stays unexplained.
                    consistent = D(0) <= shortfall <= (
                        change["bought"] * CRYPTO_IN_KIND_FEE_TOLERANCE
                    )
                else:
                    consistent = shortfall == 0
                if not consistent:
                    unexplained.append({"setup_id": str(setup_id), "symbol": setup["symbol"],
                                        "broker_qty": str(broker_qty),
                                        "fill_explained_qty": str(explained)})
                    continue
                self.store.event(
                    conn,
                    "BROKER_POSITION",
                    {"qty": str(broker_qty), "symbol": setup["symbol"],
                     "source": "ALPACA_REST_POSITIONS", "backfill_id": backfill_id,
                     "fill_explained_qty": str(explained),
                     "occurred_at": change["at"].isoformat()},
                    setup_id=setup_id,
                    key=f"rest-position:{backfill_id}:{setup_id}",
                )
                recorded += 1
        return recorded, unexplained

    # --- Fee backfill from Alpaca's account activities (package fees-net-r) --

    def fee_backfill(self, *, lookback=None, refresh=None):
        """Read Alpaca's CFEE/FEE account activities and append matched cost evidence.

        Read-only and best-effort: bounded to the window since the last completed run
        (``lookback`` widens that window defensively; nothing is read before the
        earliest recorded fill on the first run), and a failure here is recorded but
        never blocks trading, protection or reconciliation. Matching a fee to a fill
        (by broker order id and time) and the append-only import happen in
        ``managed_analytics.import_alpaca_fee_activities``; this method only owns the
        bounded broker read and the window.

        The completion event is appended only when this run's counts differ from the last
        recorded run's, or ``refresh`` (``FEE_BACKFILL_REFRESH``) after it, in database
        time, so an idle account adds two an hour instead of one per run; the window stays
        at most ``lookback`` plus ``refresh``.
        """
        from catalyst_lab.managed_analytics import import_alpaca_fee_activities

        lookback = FEE_BACKFILL_LOOKBACK if lookback is None else lookback
        refresh = FEE_BACKFILL_REFRESH if refresh is None else refresh
        with self.repo.connect() as conn:
            last = conn.execute(
                """SELECT recorded_at, body, now() - recorded_at >= %s AS due
                FROM lab.managed_events WHERE kind=%s ORDER BY event_seq DESC LIMIT 1""",
                (refresh, FEE_BACKFILL_EVENT),
            ).fetchone()
            anchor = last["recorded_at"] if last else None
            if anchor is None:
                anchor = conn.execute(
                    "SELECT min(filled_at) AS at FROM lab.managed_fills"
                ).fetchone()["at"]
        after = anchor - lookback if anchor is not None else None
        summary = {
            "window_after": after, "activities_read": 0, "matched": 0,
            "already_recorded": 0, "unmatched": 0, "invalid": 0, "conflicting": 0,
        }
        if after is None:
            return summary  # No fill has ever been recorded; nothing to bind a fee to yet.
        with request_priority(RESEARCH):
            activities = self.broker.fee_activities_since(after)
        with self.repo.connect() as conn:
            fills = conn.execute(
                "SELECT * FROM lab.managed_fills WHERE filled_at>=%s", (after,)
            ).fetchall()
        summary.update(
            import_alpaca_fee_activities(self.store, fills, activities, recorded_at=self.now())
        )
        unchanged = last is not None and all(
            last["body"].get(name) == summary[name] for name in FEE_BACKFILL_COUNTS
        )
        if not unchanged or last["due"]:
            self._event(FEE_BACKFILL_EVENT, summary)
        return summary

    def _broker_view(self, setup_id, *, read_only=False):
        """Reconcile known client IDs before any additional submission after restart/timeout.

        With a request-governed broker, open orders (with nested legs) and positions come
        from the shared snapshot; only orders absent from it are read by ID, and terminal
        ones are then served from the budget's cache.

        ``read_only`` (review snapshots) reads the same broker state but never dispatches,
        acknowledges, links, ingests or expires anything: an unsent or unlocated submission
        is simply uncertain. Otherwise a PATCH or DELETE without an acknowledgement is
        recovered here too: a claimed one is looked up (never resent) and an unclaimed one
        is closed once expired, so it cannot block the same change forever.
        """
        setup, state = self._load(setup_id)
        snapshot = self._shared_snapshot()

        def fetch(order_id):
            found = snapshot.order(order_id) if snapshot is not None else None
            return found if found is not None else self.broker.order(order_id)

        def fetch_client(client_order_id):
            found = snapshot.order_by_client_id(client_order_id) if snapshot is not None else None
            return found if found is not None else self.broker.order_by_client_id(client_order_id)

        with self.repo.connect() as conn:
            decisions = conn.execute(
                """SELECT * FROM lab.managed_risk_decisions
                WHERE setup_id=%s AND outcome='APPROVED' ORDER BY event_seq""",
                (setup_id,),
            ).fetchall()
            ack_rows = conn.execute(
                "SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind='BROKER_ACK'",
                (setup_id,),
            ).fetchall()
            closed = conn.execute(
                "SELECT kind,body FROM lab.managed_events WHERE setup_id=%s "
                "AND (kind='BROKER_REJECTED' OR kind='AUTHORIZATION_NOT_CLAIMED')",
                (setup_id,),
            ).fetchall()
            claims = {
                str(r["decision_id"])
                for r in conn.execute("SELECT decision_id FROM lab.managed_claims").fetchall()
            }
        # A rejected decision reached the broker and was refused; an unclaimed refusal never
        # left the gate. A claim, however, always means the request may have been sent.
        rejected = {
            str(r["body"]["decision_id"])
            for r in closed
            if r["kind"] == "BROKER_REJECTED" or str(r["body"]["decision_id"]) not in claims
        }
        acks = {r["body"]["decision_id"]: r["body"]["order"] for r in ack_rows}
        orders, roles, uncertain = {}, {}, []
        for d in decisions:
            if str(d["decision_id"]) in rejected:
                continue
            if d.get("method") in {"PATCH", "DELETE"}:
                if not read_only and str(d["decision_id"]) not in acks:
                    self._recover_change(d, str(d["decision_id"]) in claims)
                continue
            client_id = d["payload"]["client_order_id"]
            ack = acks.get(str(d["decision_id"]))
            order = fetch(ack["id"]) if ack and ack.get("id") else fetch_client(client_id)
            if not order:
                if str(d["decision_id"]) in claims:
                    uncertain.append(client_id)
                elif d["expires_at"] > self.now():
                    # Crash before sending: same unconsumed row, no new reservation or order ID.
                    if not read_only:
                        self.dispatch(d)
                    uncertain.append(client_id)
                elif not read_only:
                    self._event(
                        "UNSENT_AUTHORIZATION_EXPIRED",
                        {"decision_id": d["decision_id"]},
                        setup_id,
                        key="unsent:" + str(d["decision_id"]),
                    )
                continue
            if not ack and not read_only:
                self._ack(d, order)
            order = self._follow_replacement(order, fetch)
            orders[order["id"]] = order
            roles[order["id"]] = d["action"]
            for child in order.get("legs", []) or []:
                latest = self._follow_replacement(fetch(child["id"]), fetch)
                if latest:
                    orders[latest["id"]] = latest
                    roles[latest["id"]] = "PROTECT" if latest.get("stop_price") else "TARGET"
        for ack in ack_rows:
            if ack["body"].get("action") == "AMEND" and ack["body"].get("order", {}).get("id"):
                latest = self._follow_replacement(fetch(ack["body"]["order"]["id"]), fetch)
                if latest:
                    orders[latest["id"]] = latest
                    roles[latest["id"]] = "PROTECT" if latest.get("stop_price") else "TARGET"
        if not read_only:
            with self.store.transaction() as conn:
                for oid in orders:
                    self.store.event(
                        conn,
                        "BROKER_ORDER_LINK",
                        {"broker_order_id": oid, "role": roles[oid], "symbol": setup["symbol"]},
                        setup_id=setup_id,
                        key="order-link:" + oid,
                    )
            self.replay_unmatched()
        positions = snapshot.position_rows() if snapshot is not None else self.broker.positions()
        position = next(
            (
                p
                for p in positions
                if normalized_symbol(p["symbol"]) == normalized_symbol(setup["symbol"])
            ),
            None,
        )
        return setup, state, list(orders.values()), roles, position, uncertain

    def _authorize_mutation(self, setup_id, action, method, path, payload, *, reason):
        setup, state, orders, roles, position, uncertain = self._broker_view(setup_id)
        if uncertain:
            return None
        qty = num(position["qty"]) if position else D(0)
        now = self.now()
        open_orders = [
            o
            for o in orders
            if o["status"]
            not in {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}
        ]
        by_id = {o["id"]: o for o in open_orders}
        if method == "DELETE":
            if path.rsplit("/", 1)[1] not in by_id:
                return None
        elif method == "PATCH":
            order = by_id.get(path.rsplit("/", 1)[1])
            if (
                not order
                or order.get("side") != "sell"
                or set(payload) - {"stop_price", "limit_price"}
            ):
                raise ValueError("OWNED_PROTECTION_REQUIRED")
            if "stop_price" in payload and num(payload["stop_price"]) < num(order["stop_price"]):
                raise ValueError("STOP_WIDENING_REFUSED")
            if "limit_price" in payload and num(payload["limit_price"]) < num(order["limit_price"]):
                raise ValueError("TARGET_REDUCTION_REFUSED")
            if setup["market"] == "CRYPTO":
                # CRYPTO_MAINTENANCE_V1 only: a price-only replace of an owned resting
                # stop-limit, carrying the native levels of the setup's desired stop.
                if (not crypto_maintenance.active(state) or roles.get(order["id"]) != "PROTECT"
                        or set(payload) != {"stop_price", "limit_price"}):
                    raise ValueError("OWNED_PROTECTION_REQUIRED")
                native_stop, native_limit = native_stop_levels(
                    CryptoAsset.from_broker(self.broker.asset(setup["symbol"])), num(state["stop"])
                )
                desired = {"stop_price": native_stop, "limit_price": native_limit}
            else:
                desired = {"stop_price": state["stop"], "limit_price": state["target"]}
            if any(num(value) != num(desired[key]) for key, value in payload.items()):
                return None  # The desired level moved (a rejected amendment reverted it).
        elif method == "POST":
            if payload.get("side") != "sell" or num(payload["qty"]) > qty or qty <= 0:
                raise ValueError("EXIT_MUST_REDUCE_BROKER_INVENTORY")
            available = num(position.get("qty_available", position["qty"]))
            if num(payload["qty"]) > available:
                return None
            if action == "EXIT" and any(o.get("side") == "sell" for o in open_orders):
                return None
            if action == "PROTECT":
                # While an accepted plan waits to start (stale quote, unevaluable account
                # risk), the stop in force is the one before it; re-protecting there is not
                # a widening. A started plan's protection is always built at its new stop.
                floor = num(state["stop"])
                if state.get("amendment_context_hash") and state.get("amendment_previous_stop"):
                    floor = min(floor, num(state["amendment_previous_stop"]))
                if num(payload["stop_price"]) < floor:
                    raise ValueError("STOP_WIDENING_REFUSED")
        else:
            raise ValueError("MUTATION_NOT_ALLOWED")
        with self.store.transaction() as conn:
            latest = self.store.state(conn, setup_id)
            if latest["revision"] != state["revision"] or latest["state"] in TERMINAL:
                return None
            # Exact same mutation already outstanding: recover its broker state, never duplicate.
            if (
                method == "POST"
                and conn.execute(
                    "SELECT 1 FROM lab.managed_risk_decisions WHERE outcome='APPROVED' "
                    "AND method='POST' AND payload->>'client_order_id'=%s",
                    (payload["client_order_id"],),
                ).fetchone()
            ):
                return None
            pending = conn.execute(
                """SELECT d.* FROM lab.managed_risk_decisions d
                WHERE setup_id=%s AND outcome='APPROVED' AND method=%s AND path=%s AND payload=%s
                AND NOT EXISTS(SELECT 1 FROM lab.managed_events e WHERE e.kind IN
                   ('BROKER_ACK','BROKER_REJECTED','UNSENT_AUTHORIZATION_EXPIRED',
                    'AUTHORIZATION_NOT_CLAIMED')
                   AND e.body->>'decision_id'=d.decision_id::text)
                ORDER BY event_seq DESC LIMIT 1""",
                (setup_id, method, path, Jsonb(payload)),
            ).fetchone()
            if pending:
                return None
            decision = self._decision(
                conn,
                setup,
                latest,
                action,
                payload,
                {"position_qty": qty, "observed_at": now, "reason": reason},
                equity=D(0),
                method=method,
                path=path,
                reason=reason,
            )
        return self.dispatch(decision)

    def _fresh_management_plan(self, setup, state, observation):
        if not state.get("amendment_expires_at") or not state.get("amendment_context_hash"):
            return state
        with self.store.transaction() as conn:
            # Decide on the latest recorded plan: a review may have replaced it meanwhile.
            state = self.store.state(conn, setup["setup_id"])
            deadline = state.get("amendment_expires_at")
            context_hash = state.get("amendment_context_hash")
            if not deadline or not context_hash:
                return state
            started = conn.execute(
                """SELECT event_seq FROM lab.managed_events WHERE setup_id=%s
                AND kind='MANAGEMENT_STARTED' AND body->>'context_hash'=%s""",
                (setup["setup_id"], context_hash),
            ).fetchone()
            now = self.now()
            if started:
                # Finish an already committed protection replacement across restart, but
                # only the part dispatched before the review deadline.
                if now < datetime.fromisoformat(deadline):
                    return state
                return self._close_started_amendment(conn, setup, state, started["event_seq"])
            fresh = (
                observation
                and observation.get("feed_healthy")
                and 0
                <= (now - datetime.fromisoformat(observation["quote_at"])).total_seconds()
                <= 5
            )
            expired = now >= datetime.fromisoformat(deadline)
            crossed = fresh and (
                num(observation["bid"]) >= num(state["amendment_previous_target"])
                or num(observation["bid"]) <= num(state["stop"])
            )
            if expired or crossed:
                self.store.event(
                    conn,
                    "MANAGEMENT_EXPIRED",
                    {
                        "context_hash": context_hash,
                        "reason": "REVIEW_EXPIRED" if expired else "PRICE_CHANGED_BEFORE_AMENDMENT",
                    },
                    setup_id=setup["setup_id"],
                )
                return self.store.transition(
                    conn,
                    setup["setup_id"],
                    state["state"],
                    stop=state["amendment_previous_stop"],
                    target=state["amendment_previous_target"],
                    amendment_expires_at=None,
                    amendment_context_hash=None,
                )
            if not fresh or state.get("account_risk"):
                # A stale quote or unevaluable account risk defers the model plan; protection
                # meanwhile keeps the levels in force before it, and the deadline still applies.
                return {
                    **state,
                    "stop": state["amendment_previous_stop"],
                    "target": state["amendment_previous_target"],
                }
            self.store.event(
                conn,
                "MANAGEMENT_STARTED",
                {
                    "context_hash": context_hash,
                    "expires_at": deadline,
                    "quote_at": observation["quote_at"],
                    "reason": "FRESH_STATE_REVALIDATED",
                },
                setup_id=setup["setup_id"],
                key="management-start:" + context_hash,
            )
            return state

    def _close_started_amendment(self, conn, setup, state, started_seq):
        """Close a started amendment at its review deadline.

        A level whose broker change was dispatched (claimed) before the deadline stands and
        protection completes it. A level never dispatched reverts to the broker-acknowledged
        one as MANAGEMENT_EXPIRED(NOT_DISPATCHED), so a stale model choice cannot reach the
        broker later. The app-managed crypto target took effect when the plan started. A
        dispatched change that is still unresolved is recovered before anything reverts.
        """
        sid = setup["setup_id"]
        rows = conn.execute(
            """SELECT d.action,d.method,d.reason,d.payload,
              EXISTS(SELECT 1 FROM lab.managed_events a
                WHERE a.idempotency_key='ack:'||d.decision_id::text) AS acknowledged,
              EXISTS(SELECT 1 FROM lab.managed_events x WHERE x.setup_id=d.setup_id
                AND x.kind='BROKER_REJECTED' AND x.body->>'decision_id'=d.decision_id::text)
                AS rejected
            FROM lab.managed_risk_decisions d JOIN lab.managed_claims c USING(decision_id)
            WHERE d.setup_id=%s AND d.outcome='APPROVED' AND d.event_seq>%s""",
            (sid, started_seq),
        ).fetchall()
        acknowledged_stop, acknowledged_target = self._acknowledged_levels(conn, setup, state)
        if any(
            r["method"] in {"PATCH", "DELETE"} and not r["acknowledged"] and not r["rejected"]
            for r in rows
        ):
            # Past the deadline nothing new may be sent: hold the acknowledged levels in
            # memory until recovery resolves the dispatched change, then close the plan.
            return {**state, "stop": str(acknowledged_stop), "target": str(acknowledged_target)}
        dispatched = set()
        for row in rows:
            if row["rejected"]:
                continue
            if row["action"] == "AMEND":
                dispatched.update(
                    name
                    for field, name in (("stop_price", "stop"), ("limit_price", "target"))
                    if field in row["payload"]
                )
            elif row["action"] == "PROTECT" or (
                row["action"] == "CANCEL" and row["reason"] == "TIGHTEN_STOP"
            ):
                dispatched.add("stop")
        reverted = {}
        if "stop" not in dispatched and num(state["stop"]) > acknowledged_stop:
            reverted["stop"] = acknowledged_stop
        if (
            setup["market"] != "CRYPTO"
            and "target" not in dispatched
            and num(state["target"]) > acknowledged_target
        ):
            reverted["target"] = acknowledged_target
        if reverted:
            self.store.event(
                conn,
                "MANAGEMENT_EXPIRED",
                {
                    "context_hash": state["amendment_context_hash"],
                    "reason": "NOT_DISPATCHED",
                    "reverted_to": reverted,
                },
                setup_id=sid,
            )
        return self.store.transition(
            conn,
            sid,
            state["state"],
            stop=str(reverted["stop"]) if "stop" in reverted else state["stop"],
            target=str(reverted["target"]) if "target" in reverted else state["target"],
            amendment_expires_at=None,
            amendment_context_hash=None,
        )

    @staticmethod
    def _entry_filled(conn, setup_id, state, orders, roles):
        """True once any entry quantity has filled: the setup opened, the ledger recorded a buy
        fill, or the broker reports a filled quantity on the entry order."""
        return bool(
            state.get("opened_at")
            or any(
                roles.get(o["id"]) == "ENTRY" and num(o.get("filled_qty") or "0") > 0
                for o in orders
            )
            or conn.execute(
                "SELECT 1 FROM lab.managed_fills WHERE setup_id=%s AND side='buy' LIMIT 1",
                (setup_id,),
            ).fetchone()
        )

    def manage(self, setup_id, observation=None):
        """Mechanical protection runs independently of every model request."""
        from catalyst_lab.crypto_execution import (
            CryptoOrder,
            CryptoProtectionPolicy,
            CryptoSnapshot,
            plan_crypto_recovery,
        )

        setup, state, orders, roles, position, uncertain = self._broker_view(setup_id)
        from catalyst_lab.managed_measurement import record_position_snapshot

        record_position_snapshot(
            self.store, setup, state, position, observation, received_at=self.now()
        )
        if state["state"] in TERMINAL:
            return state
        if state["state"] == "WATCHING":
            now = self.now()
            deadline = setup["expires_at"]
            if state.get("crypto_entry_deadline"):
                deadline = min(deadline, datetime.fromisoformat(state["crypto_entry_deadline"]))
            if setup["market"] == "US_STOCKS":
                sessions = self.broker.calendar(
                    now.astimezone(NY).date(), now.astimezone(NY).date()
                )
                today = next(
                    (s for s in sessions if s.session_date == now.astimezone(NY).date()), None
                )
                if today:
                    deadline = min(deadline, today.flatten_time)
            if now >= deadline:
                with self.store.transaction() as conn:
                    self.store.transition(
                        conn, setup_id, "EXPIRED_UNTRIGGERED", reason="ENTRY_DEADLINE"
                    )
                return self._load(setup_id)[1]
            with self.repo.connect() as conn:
                failure = conn.execute(
                    "SELECT lab.managed_review_failure(%s) AS reason",
                    (Jsonb(setup["record_json"]),),
                ).fetchone()["reason"]
            if failure:
                self.revoke(setup_id, failure)
            return state
        if uncertain:
            self._event("RECOVERY_PENDING", {"client_ids": uncertain}, setup_id)
            return state
        # Account data problems are never exit reasons for open positions: they block new
        # entries and model amendments while the mechanical protection below keeps running.
        account_risk, evidence = self._account_evidence()
        with self.store.transaction() as conn:
            halt = None
            if evidence is not None:
                try:
                    halt = self._account_halt(conn, *evidence)
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    # Unreadable account numbers (parsed before any halt is written).
                    account_risk = UNEVALUABLE + error_code(exc)
            if halt == "STARTUP_RECONCILIATION_REQUIRED":  # No same-day baseline yet.
                account_risk, halt = UNEVALUABLE + halt, None
            state = self._note_account_risk(conn, setup_id, state, account_risk)
        state = self._fresh_management_plan(setup, state, observation)
        now = self.now()
        qty = num(position["qty"]) if position else D(0)
        active = [
            o
            for o in orders
            if o["status"]
            not in {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}
        ]
        if qty < 0:
            with self.store.transaction() as conn:
                self._latch_execution_halt(
                    conn,
                    "MANAGED_UNEXPECTED_SHORT_POSITION",
                    {"setup_id": str(setup_id)},
                )
            # Cancel owned residual orders to prevent worsening a reversal. Buying to cover
            # an unexplained short is deliberately not an invented long-only policy.
            for order in active:
                self._authorize_mutation(
                    setup_id,
                    "CANCEL",
                    "DELETE",
                    "/v2/orders/" + order["id"],
                    {},
                    reason="UNEXPECTED_SHORT_POSITION",
                )
            self.reconciled_at = None
            return state
        if qty > 0 and not state.get("opened_at") and gap_resume.applies(state):
            # CRYPTO_GAP_RESUME_V1 (package gap-resume, plan section 5 "fetch fills missed while
            # down"): an entry fill the trade-updates stream never delivered (an outage or a
            # restart) is read from Alpaca's fill activities before the first-fill rule below.
            self._backfill_unrecorded_entry_fill(setup_id)
        with self.store.transaction() as conn:
            if state["state"] in {"ENTRY_PENDING", "ORDER_SUBMITTED"}:
                failed_review = conn.execute(
                    "SELECT lab.managed_review_failure(%s) AS reason",
                    (Jsonb(setup["record_json"]),),
                ).fetchone()["reason"]
                if failed_review:
                    self.store.event(conn, "REVOKE", {"reason": failed_review}, setup_id=setup_id)
                    state = self.store.transition(
                        conn,
                        setup_id,
                        state["state"],
                        revoked=True,
                        revocation_reason=failed_review,
                    )
            first_fill = None
            crypto_deadline = None
            # CRYPTO_24H_HOLD_V1 (crypto_holding.py): 24 hours from the first fill, no midnight;
            # the window versions use the window the setup recorded at admission.
            hold = recorded_hold_policy(state)
            if qty > 0 and (state.get("crypto_day_policy") or hold is not None):
                day_policy = None if hold else CryptoDayPolicy(**state["crypto_day_policy"])
                first_fill = conn.execute(
                    "SELECT min(filled_at) AS first_fill FROM lab.managed_fills "
                    "WHERE setup_id=%s AND side='buy'", (setup_id,),
                ).fetchone()["first_fill"]
                if first_fill is None or first_fill > now:
                    # Do not grant a fresh holding window when reconnect found inventory
                    # without its entry-time evidence. Keep recovery active and exit.
                    halt = "CRYPTO_FIRST_FILL_TIME_UNAVAILABLE"
                    crypto_deadline = now
                    self._latch_execution_halt(
                        conn, halt, {"setup_id": str(setup_id)}
                    )
                elif hold is not None:
                    # CRYPTO_24H_REVIEW_V1: the current review's fail-safe (package day-review).
                    crypto_deadline = crypto_holding.hard_exit_deadline(hold, first_fill, state)
                else:
                    crypto_deadline = min(
                        day_policy.exit_deadline(first_fill),
                        datetime.fromisoformat(state["crypto_flat_deadline"]),
                    )
            if qty > 0 and not state.get("opened_at"):
                opened = first_fill or now
                deadline = crypto_deadline or now + timedelta(
                    seconds=self.policy.crypto_max_hold_seconds
                )
                if setup["market"] == "US_STOCKS":
                    sessions = self.broker.calendar(
                        now.astimezone(NY).date(), now.astimezone(NY).date()
                    )
                    session = next((s for s in sessions if s.contains(now)), None)
                    if not session:
                        halt = "CALENDAR_UNAVAILABLE"
                        deadline = now
                    else:
                        deadline = session.flatten_time
                state = self.store.transition(
                    conn,
                    setup_id,
                    "OPEN",
                    qty=str(qty),
                    fill_price=str(position["avg_entry_price"]),
                    opened_at=opened.isoformat(),
                    hard_exit_at=deadline.isoformat(),
                    # CRYPTO_24H_REVIEW_V1 only: T and the continuation count (none otherwise).
                    **(crypto_holding.review_fields(hold, first_fill, state)
                       if first_fill is not None and first_fill <= now else {}),
                )
            elif str(qty) != state.get("qty"):
                state = self.store.transition(conn, setup_id, state["state"], qty=str(qty))
            if crypto_deadline is not None and state.get("hard_exit_at") and (
                crypto_deadline < datetime.fromisoformat(state["hard_exit_at"])
            ):
                # A delayed earlier fill can tighten, never restart or extend, the clock.
                state = self.store.transition(
                    conn, setup_id, state["state"],
                    hard_exit_at=crypto_deadline.isoformat(),
                    opened_at=(first_fill or now).isoformat(),
                    **(crypto_holding.review_fields(hold, first_fill, state)
                       if first_fill is not None and first_fill <= now else {}),
                )
            # A pre-fill revocation and the entry window (packet expiry, crypto entry cutoff)
            # cancel an entry that never filled. Zero broker quantity alone does not show that:
            # exit fills empty a filled position too, which then closes with the exit request
            # that emptied it, never as an untriggered expiry (2026-09-25 attribution defect).
            unfilled_entry = state["state"] in {"ENTRY_PENDING", "ORDER_SUBMITTED"} and not (
                self._entry_filled(conn, setup_id, state, orders, roles)
            )
            exit_reason = state.get("exit_requested")
            if halt:
                exit_reason = halt
            elif state.get("hard_exit_at") and now >= datetime.fromisoformat(state["hard_exit_at"]):
                # HOLD_24H_EXIT under the 24-hour hold and under CRYPTO_WINDOW_HOLD_V1.
                exit_reason = time_exit_reason(state)
            elif unfilled_entry and state.get("revoked") and qty == 0:
                exit_reason = "REVIEW_REVOKED"
            elif unfilled_entry and qty == 0 and now >= min(
                setup["expires_at"],
                datetime.fromisoformat(state["crypto_entry_deadline"])
                if state.get("crypto_entry_deadline") else setup["expires_at"],
            ):
                exit_reason = "ENTRY_EXPIRED"
            if exit_reason and exit_reason != state.get("exit_requested"):
                state = self.store.transition(
                    conn, setup_id, state["state"], exit_requested=exit_reason
                )
        if qty == 0 and not active:
            with self.store.transaction() as conn:
                self.store.transition(
                    conn,
                    setup_id,
                    "CLOSED",
                    qty="0",
                    closed_at=now.isoformat(),
                    reason=state.get("exit_requested") or "BROKER_EXIT",
                )
                self._release(conn, setup_id, "BROKER_AND_ORDERS_FLAT")
            return self._load(setup_id)[1]
        if setup["market"] == "CRYPTO":
            asset = CryptoAsset.from_broker(self.broker.asset(setup["symbol"]))
            stop = num(state["stop"])
            stop_limit = max(asset.price_increment, stop - asset.price_increment)
            quote_fresh = (
                observation
                and observation.get("feed_healthy")
                and 0
                <= (now - datetime.fromisoformat(observation["quote_at"])).total_seconds()
                <= 5
            )
            bid = num(observation["bid"]) if quote_fresh else None
            breached_at = state.get("stop_breached_at")
            if bid is not None and bid <= stop and not breached_at:
                breached_at = now.isoformat()
                with self.store.transaction() as conn:
                    state = self.store.transition(
                        conn, setup_id, state["state"], stop_breached_at=breached_at
                    )
            crypto_orders = tuple(
                CryptoOrder(
                    o["id"],
                    o.get("client_order_id", ""),
                    "ENTRY"
                    if roles[o["id"]] == "ENTRY"
                    else "PROTECT"
                    if roles[o["id"]] == "PROTECT"
                    else "EXIT",
                    num(o["qty"]),
                    num(o.get("filled_qty", "0")),
                    o["status"],
                    True,
                    num(o["stop_price"]) if o.get("stop_price") else None,
                    num(o["limit_price"]) if o.get("limit_price") else None,
                    replaces=o.get("replaces") or None,
                )
                for o in orders
            )
            # CRYPTO_PARTIAL_ENTRY_V1 and CRYPTO_MAINTENANCE_V1 (package maintenance) only; every
            # other setup plans exactly as before (the planner's defaults).
            retain, entry_reason, replace = False, "CANCEL_REMAINING_ENTRY", "CANCEL"
            if crypto_maintenance.active(state) and qty > 0:
                self._maintenance_open_records(setup, state, crypto_orders, position, now)
                state = self._maintenance_stop_watch(setup, state, crypto_orders, asset, bid, now)
                path = (state.get("stop_replace") or {}).get("path")
                if path == crypto_maintenance.PATCH_REPLACE:
                    replace = "PATCH"
            if crypto_maintenance.partial_entry_active(state) and qty > 0:
                state, retain, entry_reason = self._partial_entry(
                    setup, state, crypto_orders, observation if quote_fresh else None, now
                )
            reserved = sum((o.remaining for o in crypto_orders if o.role != "ENTRY"), D(0))
            snap = CryptoSnapshot(
                str(state["revision"]),
                now,
                qty,
                num(position.get("qty_available", str(max(D(0), qty - reserved))))
                if position
                else D(0),
                crypto_orders,
                True,
                (),
                (),
                bid,
                datetime.fromisoformat(observation["quote_at"]) if quote_fresh else None,
                datetime.fromisoformat(breached_at) if breached_at else None,
            )
            plan = plan_crypto_recovery(
                asset,
                snap,
                CryptoProtectionPolicy(D(5), D(5), D(2)),
                now=now,
                operation_key=str(setup_id),
                stop=stop,
                stop_limit=stop_limit,
                target=num(state["target"]),
                exit_requested=bool(state.get("exit_requested")),
                exit_deadline=datetime.fromisoformat(
                    state.get("hard_exit_at", setup["expires_at"].isoformat())
                ),
                time_exit_reason=time_exit_reason(state),
                retain_entry=retain,
                entry_cancel_reason=entry_reason,
                replace_stop=replace,
                protect_after_entries=state.get("partial_entry_cancel")
                == crypto_maintenance.PARTIAL_ENTRY_PROTECTION_REFUSED,
            )
            with self.store.transaction() as conn:
                # Details (e.g. a stop raised to the broker grid) exist only for off-grid levels.
                body = {"state": plan.state, "reason": plan.reason}
                if plan.details:
                    body["details"] = plan.details
                # One event per distinct plan in a lifecycle, not one per tick: the key is
                # the digest of everything the plan decides (and the body it records).
                plan_digest = digest(encoded(json_safe({
                    **body,
                    "proposals": [asdict(proposal) for proposal in plan.proposals],
                    "residual_qty": plan.residual_qty,
                })))
                self.store.event(
                    conn, "PROTECTION_PLAN", body, setup_id=setup_id,
                    key=f"protection-plan:{setup_id}:{state.get('lifecycle_id')}:{plan_digest}",
                )
                if plan.state == "HALTED":
                    self._latch_execution_halt(
                        conn,
                        "MANAGED_CRYPTO_" + plan.reason,
                        {
                            "setup_id": str(setup_id),
                            "residual_qty": str(plan.residual_qty),
                            **plan.details,
                        },
                    )
                    current = self.store.state(conn, setup_id)
                    if current.get("protection_state") != "HALTED" or (
                        current.get("last_error") != plan.reason
                    ):
                        self.store.transition(
                            conn, setup_id, current["state"],
                            protection_state="HALTED", last_error=plan.reason,
                        )
            if plan.state == "HALTED":
                self.reconciled_at = None
            if not state.get("exit_requested") and plan.reason in {
                "TARGET_EXIT",
                "STOP_LIMIT_NOT_FILLED",
                "PROTECTION_REJECTED",
                "TIME_EXIT",
                HOLD_EXIT_REASON,
                REVIEW_DEADLINE_EXIT_REASON,
            }:
                with self.store.transaction() as conn:
                    self.store.transition(
                        conn, setup_id, state["state"], exit_requested=plan.reason
                    )
            for proposal in plan.proposals:
                # A close follows the refused-close backoff and never reuses a spent ID.
                if proposal.action == "CRYPTO_EXIT" and not self._close_permitted(
                    setup_id, proposal.payload["client_order_id"], int(snap.revision)
                ):
                    continue
                self._authorize_mutation(
                    setup_id,
                    proposal.action.removeprefix("CRYPTO_"),
                    proposal.method,
                    proposal.path,
                    proposal.payload,
                    reason=proposal.reason,
                )
            return plan
        return self._manage_stock(setup, state, orders, roles, position, observation)

    def _authorized_levels(self, setup_id):
        """Every stop and target price this setup's own decisions put on its bracket legs."""
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT action,payload FROM lab.managed_risk_decisions WHERE setup_id=%s
                AND outcome='APPROVED' AND action IN ('ENTRY','AMEND')""",
                (setup_id,),
            ).fetchall()
        stops, targets = set(), set()
        for row in rows:
            payload = row["payload"]
            if row["action"] == "ENTRY":
                stops.add((payload.get("stop_loss") or {}).get("stop_price"))
                targets.add((payload.get("take_profit") or {}).get("limit_price"))
            else:
                stops.add(payload.get("stop_price"))
                targets.add(payload.get("limit_price"))
        return stops - {None}, targets - {None}

    def _bracket_protection(self, setup, state, orders, roles, qty):
        """Rule A over the owned bracket, bounded by the transition grace.

        Owned legs in a transitioning state (our own replacement, a leg not yet released)
        count as PROTECTION_TRANSITIONING for TRANSITION_GRACE_SECONDS from the first tick
        that saw them, recorded durably in ``protection_transitioning_since``; afterwards
        they are UNPROTECTED and the existing cancel-and-flatten path applies.
        """
        sid = setup["setup_id"]
        stops, targets = self._authorized_levels(sid)
        result = classify_bracket(
            next((o for o in orders if roles[o["id"]] == "ENTRY"), None),
            [
                ("STOP" if roles[o["id"]] == "PROTECT" else "TARGET", o)
                for o in orders
                if roles[o["id"]] in {"PROTECT", "TARGET"}
            ],
            position_qty=qty,
            stop_prices=stops,
            target_prices=targets,
        )
        since = state.get("protection_transitioning_since")
        now = self.now()
        if result.status == TRANSITIONING:
            if since is None:
                since = now.isoformat()
                with self.store.transaction() as conn:
                    self.store.event(
                        conn,
                        "PROTECTION_TRANSITIONING",
                        {
                            "reason": result.reason,
                            "since": since,
                            "grace_seconds": TRANSITION_GRACE_SECONDS,
                            "legs": {
                                o["id"]: o.get("status")
                                for o in orders
                                if roles[o["id"]] in {"PROTECT", "TARGET"}
                            },
                        },
                        setup_id=sid,
                        key=f"protection-transitioning:{sid}:{since}",
                    )
                    self.store.transition(
                        conn, sid, state["state"], protection_transitioning_since=since
                    )
            if within_transition_grace(datetime.fromisoformat(since), now):
                return result, True
        elif result.status == PROTECTED and since is not None:
            with self.store.transaction() as conn:
                self.store.transition(
                    conn, sid, state["state"], protection_transitioning_since=None
                )
        return result, False

    def _manage_stock(self, setup, state, orders, roles, position, observation):
        sid = setup["setup_id"]
        qty = num(position["qty"]) if position else D(0)
        active = [o for o in orders if o["status"] not in BROKER_TERMINAL]
        reason = state.get("exit_requested")
        protection, in_grace = None, False
        if qty > 0:
            protection, in_grace = self._bracket_protection(setup, state, orders, roles, qty)
            if protection.status != PROTECTED and not in_grace:
                reason = reason or (
                    "PARTIAL_ENTRY_SAFETY_FLATTEN"
                    if protection.reason == ENTRY_NOT_FILLED
                    else "INCOMPLETE_BRACKET_PROTECTION"
                )
            if in_grace and not reason:
                return "PROTECTION_TRANSITIONING"  # No mutation or flatten inside the grace.
        if qty > 0 and any(roles[o["id"]] == "ENTRY" for o in active):
            reason = reason or "PARTIAL_ENTRY_SAFETY_FLATTEN"
        if reason or (qty == 0 and not any(roles[o["id"]] == "ENTRY" for o in active)):
            if state.get("exit_requested") != (reason or "FLAT_CANCEL_REMAINDER"):
                # One revision when the exit reason changes, never one per tick: the close's
                # client order ID follows the revision, which moves once per retry.
                with self.store.transaction() as conn:
                    state = self.store.transition(
                        conn, sid, state["state"], exit_requested=reason or "FLAT_CANCEL_REMAINDER"
                    )
            if active:
                for order in active:
                    if order["status"] not in {"pending_cancel", "pending_replace"}:
                        self._authorize_mutation(
                            sid,
                            "CANCEL",
                            "DELETE",
                            "/v2/orders/" + order["id"],
                            {},
                            reason=reason or "FLAT_CANCEL_REMAINDER",
                        )
                return "CANCELING"
            if qty > 0:
                payload = {
                    "symbol": setup["symbol"],
                    "qty": str(qty),
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": "cl-exit-"
                    + str(sid).replace("-", "")
                    + "-"
                    + str(state["revision"]),
                }
                if not self._close_permitted(sid, payload["client_order_id"], state["revision"]):
                    return "EXIT_RETRY_PENDING"  # Refused-close backoff, or a fresh ID next tick.
                self._authorize_mutation(sid, "EXIT", "POST", "/v2/orders", payload, reason=reason)
                return "EXIT_PENDING"
        if qty > 0 and observation and observation["feed_healthy"]:
            now = self.now()
            if (
                not 0
                <= (now - datetime.fromisoformat(observation["quote_at"])).total_seconds()
                <= 5
            ):
                return "STALE_QUOTE_PROTECTION_UNCHANGED"
            # Model amendments touch only a PROTECTED bracket, never a transitioning leg.
            for kind, order in (("PROTECT", protection.stop), ("TARGET", protection.target)):
                field, desired = (
                    ("stop_price", state["stop"])
                    if kind == "PROTECT"
                    else ("limit_price", state["target"])
                )
                if num(desired) > num(order[field]):
                    if (kind == "PROTECT" and num(desired) >= num(observation["bid"])) or (
                        kind == "TARGET" and num(desired) <= num(observation["ask"])
                    ):
                        continue
                    self._authorize_mutation(
                        sid,
                        "AMEND",
                        "PATCH",
                        "/v2/orders/" + order["id"],
                        {field: desired},
                        reason="JEV_BOUNDED_AMENDMENT",
                    )
        return "PROTECTED" if qty > 0 else "ENTRY_WORKING"

    # --- CRYPTO_PARTIAL_ENTRY_V1 and CRYPTO_MAINTENANCE_V1 (package maintenance, plan 4.6) -----

    def _maintenance_open_records(self, setup, state, orders, position, now):
        """What a maintained trade records at open (plan 4.6.1), once per lifecycle:
        MAINTENANCE_OPENED at the first fill and MAINTENANCE_ENTRY_COMPLETED once no entry
        order works (filled, or its remainder cancelled). Evidence only; nothing is sent."""
        if state.get("state") != "OPEN" or not state.get("opened_at"):
            return
        sid, lifecycle = setup["setup_id"], state.get("lifecycle_id")
        memo = self.__dict__.setdefault("_maintenance_memo", set())
        working = any(o.role == "ENTRY" and o.active for o in orders)
        wanted = [("MAINTENANCE_OPENED", "maintenance-opened")]
        if not working:
            wanted.append(("MAINTENANCE_ENTRY_COMPLETED", "maintenance-entry-completed"))
        pending = [(kind, f"{prefix}:{sid}:{lifecycle}") for kind, prefix in wanted
                   if (str(sid), lifecycle, kind) not in memo]
        if not pending:
            return
        levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
        risk = crypto_maintenance.r_per_coin(levels)
        with self.store.transaction() as conn:
            fills = conn.execute(
                """SELECT qty,price,filled_at FROM lab.managed_fills WHERE setup_id=%s
                AND side='buy' ORDER BY filled_at,event_seq""", (sid,),
            ).fetchall()
            filled = sum((f["qty"] for f in fills), D(0))
            average = (
                sum((f["qty"] * f["price"] for f in fills), D(0)) / filled if filled else None
            )
            for kind, key in pending:
                if not conn.execute(
                    "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
                ).fetchone():
                    body = {
                        # The setup's recorded version (V1, or V2 from package answer-rules).
                        "policy_id": crypto_maintenance.recorded_policy(state).policy_id,
                        "lifecycle_id": lifecycle,
                        "avg_entry": average,
                        "filled_qty": filled,
                        "position_qty": position.get("qty") if position else None,
                        "broker_avg_entry_price": position.get("avg_entry_price")
                        if position else None,
                        "recorded_at": now,
                    }
                    if kind == "MAINTENANCE_OPENED":
                        entry = average or num(state["fill_price"])
                        body.update(
                            opened_at=state["opened_at"],
                            first_fill_at=fills[0]["filled_at"] if fills else None,
                            initial_stop=levels["stop"], initial_target=levels["target"],
                            entry_trigger=levels["entry_trigger"],
                            max_entry=levels["max_entry_price"], risk_per_coin=risk,
                            reward_risk_at_max_entry=crypto_maintenance.in_r(
                                levels["target"] - levels["max_entry_price"], risk),
                            target_r_from_entry=crypto_maintenance.in_r(
                                levels["target"] - entry, risk),
                            # CRYPTO_24H_REVIEW_V1 records T; the hold's T is its exit.
                            first_24h_review_at=state.get("day_review_at")
                            or state.get("hard_exit_at"),
                            entry_order_working=working,
                        )
                    else:
                        requested = num(state.get("requested_qty") or "0")
                        body.update(
                            requested_qty=requested,
                            outcome="FILLED" if requested and filled >= requested
                            else "REMAINDER_CANCELLED",
                            cancel_reason=state.get("partial_entry_cancel"),
                        )
                    self.store.event(conn, kind, body, setup_id=sid, key=key)
                memo.add((str(sid), lifecycle, kind))

    def _partial_entry(self, setup, state, orders, observation, now):
        """CRYPTO_PARTIAL_ENTRY_V1: ``(state, retain, cancel reason)`` of a partly filled entry.

        The filled quantity is protected at once (the planner); the rest of the entry keeps
        working until ten minutes after the first fill, or until a fresh quote leaves the entry
        zone (ask above the max entry, bid at or below the stop), and is then cancelled under its
        own one-use authorization (the planner's cancel, with the reason). Once decided, the
        cancel is final for the lifecycle; an exit cancels the remainder anyway.
        """
        working = [o for o in orders if o.role == "ENTRY" and o.active]
        if not working or state.get("state") != "OPEN" or state.get("exit_requested"):
            return state, False, "CANCEL_REMAINING_ENTRY"
        reason = state.get("partial_entry_cancel")
        if reason is None:
            levels = {k: num(v) for k, v in setup["record_json"]["levels"].items()}
            reason = crypto_maintenance.partial_entry_cancel_reason(
                opened_at=datetime.fromisoformat(state["opened_at"])
                if state.get("opened_at") else None,
                now=now,
                bid=num(observation["bid"]) if observation else None,
                ask=num(observation["ask"]) if observation else None,
                max_entry=levels["max_entry_price"], stop=num(state["stop"]),
            )
        if reason is None and state.get("partial_entry_retained_at"):
            return state, True, "CANCEL_REMAINING_ENTRY"  # Already recorded: nothing to write.
        if reason is not None and state.get("partial_entry_cancel") == reason:
            return state, False, reason
        sid = setup["setup_id"]
        remaining = sum((o.remaining for o in working), D(0))
        evidence = {
            "policy_id": crypto_maintenance.PARTIAL_ENTRY_VERSION,
            "lifecycle_id": state.get("lifecycle_id"),
            "first_fill_at": state.get("opened_at"),
            "deadline": (datetime.fromisoformat(state["opened_at"]) + timedelta(
                seconds=crypto_maintenance.PARTIAL_ENTRY_MAX_SECONDS)).isoformat()
            if state.get("opened_at") else None,
            "entry_order_ids": [o.broker_id for o in working],
            "remaining_qty": remaining,
            "bid": observation.get("bid") if observation else None,
            "ask": observation.get("ask") if observation else None,
            "quote_at": observation.get("quote_at") if observation else None,
            "decided_at": now,
        }
        with self.store.transaction() as conn:
            current = self.store.state(conn, sid)
            if current.get("revision") != state.get("revision"):
                # The state moved meanwhile: a recorded cancel stands, otherwise the remainder
                # keeps working and the next tick decides again.
                recorded = current.get("partial_entry_cancel")
                return (current, False, recorded) if recorded else (
                    current, True, "CANCEL_REMAINING_ENTRY")
            if reason is None:
                if not current.get("partial_entry_retained_at"):
                    self.store.event(conn, "PARTIAL_ENTRY_RETAINED", evidence, setup_id=sid,
                                     key=f"partial-entry-retained:{sid}:{state.get('lifecycle_id')}")
                    current = self.store.transition(
                        conn, sid, current["state"], partial_entry_retained_at=now.isoformat()
                    )
                return current, True, "CANCEL_REMAINING_ENTRY"
            if current.get("partial_entry_cancel") != reason:
                self.store.event(
                    conn, "PARTIAL_ENTRY_REMAINDER_CANCEL", {**evidence, "reason": reason},
                    setup_id=sid,
                    key=f"partial-entry-cancel:{sid}:{state.get('lifecycle_id')}:{reason}",
                )
                current = self.store.transition(
                    conn, sid, current["state"], partial_entry_cancel=reason
                )
        return current, False, reason

    def _partial_entry_protection_refused(self, conn, decision, current, reason):
        """A protection the broker refused while a retained entry remainder worked: the
        remainder is cancelled first and protection is sent again once it is gone, instead of
        today's PROTECTION_REJECTED flatten (which still applies when no remainder works)."""
        if not (
            crypto_maintenance.partial_entry_active(current)
            and current.get("partial_entry_retained_at")
            and not current.get("partial_entry_cancel")
            and current.get("state") not in TERMINAL
        ):
            return False
        sid, decision_id = decision["setup_id"], str(decision["decision_id"])
        self.store.event(conn, "PARTIAL_ENTRY_PROTECTION_REFUSED", {
            "decision_id": decision_id, "reason": reason,
            "requested_qty": decision["payload"].get("qty"),
            "fallback": "CANCEL_ENTRY_REMAINDER_THEN_PROTECT",
        }, setup_id=sid, key="partial-entry-protection-refused:" + decision_id)
        self.store.transition(
            conn, sid, current["state"],
            partial_entry_cancel=crypto_maintenance.PARTIAL_ENTRY_PROTECTION_REFUSED,
        )
        return True

    def _maintenance_stop_watch(self, setup, state, orders, asset, bid, now):
        """A raised stop not yet at the broker (``stop_replace``), every protection tick.

        Once every resting stop-limit carries the raised stop's native levels and covers the
        position, STOP_REPLACED records the path that ran (PATCH_REPLACE or CANCEL_THEN_PLACE)
        and the replace closes. Until then the app enforces the raised stop itself: a fresh bid
        at or below it requests an exit at once (STOP_CROSSED_DURING_REPLACE), which cancels the
        resting protection and sells at market, each under its own one-use authorization.
        """
        from catalyst_lab.crypto_execution import PENDING

        replace = state.get("stop_replace")
        if not replace or state.get("state") != "OPEN" or state.get("exit_requested"):
            return state
        sid = setup["setup_id"]
        native_stop, native_limit = native_stop_levels(asset, num(state["stop"]))
        protection = [o for o in orders if o.role == "PROTECT" and o.active]
        position = num(state.get("qty") or "0")
        settled = bool(protection) and all(
            o.status not in PENDING and o.stop_price == native_stop
            and o.limit_price == native_limit for o in protection
        ) and sum((o.remaining for o in protection), D(0)) >= position > 0
        crossed = bid is not None and bid <= num(state["stop"])
        if not settled and not crossed:
            return state
        with self.store.transaction() as conn:
            current = self.store.state(conn, sid)
            if current.get("stop_replace") != replace or current.get("exit_requested"):
                return current
            body = {**replace, "native_stop": native_stop, "native_stop_limit": native_limit,
                    "orders": [o.broker_id for o in protection], "at": now}
            if settled:
                self.store.event(conn, "STOP_REPLACED", body, setup_id=sid,
                                 key=f"stop-replaced:{sid}:{replace['change_id']}")
                return self.store.transition(conn, sid, current["state"], stop_replace=None)
            self.store.event(
                conn, crypto_maintenance.STOP_CROSSED_DURING_REPLACE, {**body, "bid": bid},
                setup_id=sid, key=f"stop-crossed-during-replace:{sid}:{replace['change_id']}",
            )
            return self.store.transition(
                conn, sid, current["state"],
                exit_requested=crypto_maintenance.STOP_CROSSED_DURING_REPLACE,
            )

    def _stop_replace_refused(self, conn, decision, current, reason):
        """The broker refused a PATCH replace of a raised stop: fall back to cancel-then-place
        (the raised stop stays; the app enforces it until the new stop-limit rests)."""
        replace = current.get("stop_replace")
        if not (
            crypto_maintenance.active(current) and replace
            and replace.get("path") == crypto_maintenance.PATCH_REPLACE
            and "stop_price" in decision["payload"]
        ):
            return False
        sid, decision_id = decision["setup_id"], str(decision["decision_id"])
        refusal = {"decision_id": decision_id, "reason": reason, "at": self.now().isoformat()}
        self.store.event(conn, "STOP_REPLACE_FALLBACK", {
            "change_id": replace.get("change_id"), "path": decision["path"],
            "requested": decision["payload"], "reason": reason,
            "from_path": crypto_maintenance.PATCH_REPLACE,
            "to_path": crypto_maintenance.CANCEL_THEN_PLACE,
        }, setup_id=sid, key="stop-replace-fallback:" + decision_id)
        if current.get("state") not in TERMINAL:
            self.store.transition(conn, sid, current["state"], stop_replace={
                **replace, "path": crypto_maintenance.CANCEL_THEN_PLACE, "patch_refused": refusal,
            })
        return True

    def accept_management(self, setup_id, context, result, fresh_snapshot):
        """Receipts and fresh position binding are rechecked before recording desired levels."""
        from catalyst_lab.managed_review import evaluate_managed_result, verify_managed_receipts

        result = verify_managed_receipts(self.review_store, context, result)
        decision = evaluate_managed_result(context, result, fresh_snapshot, now=self.now())
        for receipt in result.receipt_ids:
            self.review_store.verify(receipt)
        with self.store.transaction() as conn:
            state = self.store.state(conn, setup_id)
            if conn.execute(
                """SELECT 1 FROM lab.managed_events WHERE setup_id=%s
                AND kind='MANAGED_JEV_JUDGMENT' AND body->>'request_id'=%s""",
                (setup_id, str(result.request_id)),
            ).fetchone():
                return False
            self.store.event(
                conn,
                "MANAGED_JEV_JUDGMENT",
                {
                    "context_hash": context.context_hash,
                    "action": decision.action,
                    "reason": decision.reason,
                    "receipt_ids": result.receipt_ids,
                    "request_id": str(result.request_id),
                },
                setup_id=setup_id,
            )
            if (
                not decision.requires_risk_authorization
                or state["state"] != "OPEN"
                or str(fresh_snapshot.lifecycle_id) != state["lifecycle_id"]
                or fresh_snapshot.context_revision != state["revision"]
                or state.get("exit_requested")
                or state.get("account_risk")  # UNEVALUABLE account risk blocks amendments.
            ):
                return False
            stop = decision.proposed_stop or num(state["stop"])
            target = decision.proposed_target or num(state["target"])
            if stop < num(state["stop"]) or target < num(state["target"]):
                raise ValueError("PROTECTION_WIDENING_REFUSED")
            self.store.event(
                conn,
                "MANAGEMENT_PLAN_AUTHORIZED",
                {
                    "stop": stop,
                    "target": target,
                    "context_hash": context.context_hash,
                    "receipt_ids": result.receipt_ids,
                    "expires_at": decision.expires_at,
                },
                setup_id=setup_id,
            )
            self.store.transition(
                conn,
                setup_id,
                "OPEN",
                stop=str(stop),
                target=str(target),
                amendment_expires_at=decision.expires_at.isoformat(),
                amendment_previous_stop=state["stop"],
                amendment_previous_target=state["target"],
                amendment_context_hash=context.context_hash,
            )
        return True
