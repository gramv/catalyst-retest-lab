"""Local, append-only matched-opportunity evaluation; never an execution authority.

The frozen crypto trigger is ManagedExecution._trigger_failure. It is a pure
method, invoked unbound without constructing a broker/controller. Entry fills
require a later fresh quote and displayed ask size. Exit bars are an explicitly
labelled simulation assumption; overlapping or double-touch bars stay ambiguous.
No account allocator, queue-position model, or second-Jev management is implied.
"""

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Literal

from catalyst_lab.managed_execution import ManagedExecution

VERSION = "JEV_SHADOW_MATCHED_V1"
TRIGGER_VERSION = "CRYPTO_STRUCTURAL_RETEST_TEST_V1"
FILL_MODEL = "NEXT_QUOTE_SIZE_AND_FIXED_EXITS_V1"
ZERO = D(0)


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return value.astimezone(UTC)


def _number(value, *, positive=False):
    if isinstance(value, (float, bool)):
        raise ValueError("DECIMAL_INPUT_REQUIRED")
    value = D(value)
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError("FINITE_NONNEGATIVE_NUMBER_REQUIRED")
    return value


def _safe(value):
    if isinstance(value, (D, datetime)):
        return str(value) if isinstance(value, D) else value.isoformat()
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def _json(value):
    return json.dumps(_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class ShadowPolicy:
    """All tunable assumptions are supplied explicitly and frozen with each case.

    fee_bps_per_side=None means unknown fees, not zero. A numeric value is an
    assumption, never verified broker fees. Price ceiling and liquidity floors
    belong to this declared experiment, not newly invented strategy defaults.
    """

    price_ceiling: D
    minimum_dollar_volume: D
    maximum_participation: D
    assumed_round_trip_cost_bps: D
    maximum_cost_to_risk: D
    slippage_bps: D
    fee_bps_per_side: D | None
    entry_latency_ms: int
    policy_id: str = VERSION
    fill_model: str = FILL_MODEL

    def __post_init__(self):
        for name in ("price_ceiling", "minimum_dollar_volume", "maximum_participation",
                     "maximum_cost_to_risk"):
            object.__setattr__(self, name, _number(getattr(self, name), positive=True))
        for name in ("assumed_round_trip_cost_bps", "slippage_bps", "fee_bps_per_side"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, _number(getattr(self, name)))
        if (self.assumed_round_trip_cost_bps is None or self.slippage_bps is None
                or self.maximum_participation > D(".01") or self.maximum_cost_to_risk > 1
                or self.slippage_bps >= 10000
                or self.policy_id != VERSION or self.fill_model != FILL_MODEL
                or type(self.entry_latency_ms) is not int or self.entry_latency_ms < 0):
            raise ValueError("INVALID_SHADOW_POLICY")


@dataclass(frozen=True)
class ShadowEvidence:
    evidence_id: str
    available_at: datetime
    event_at: datetime
    content_json: str

    def __post_init__(self):
        object.__setattr__(self, "available_at", _time(self.available_at))
        object.__setattr__(self, "event_at", _time(self.event_at))
        object.__setattr__(self, "content_json", _json(json.loads(self.content_json)))
        if not self.evidence_id or self.event_at > self.available_at:
            raise ValueError("INVALID_EVIDENCE")

    @property
    def source_hash(self):
        return _hash(asdict(self))


@dataclass(frozen=True)
class ShadowCase:
    case_id: str
    symbol: str
    venue: str
    as_of: datetime
    decision_at: datetime
    expires_at: datetime
    exit_deadline: datetime
    entry_trigger: D
    max_entry_price: D
    stop: D
    target: D
    quantity: D
    bid: D
    ask: D
    quote_at: datetime
    observed_dollar_volume: D
    liquidity_at: datetime
    policy: ShadowPolicy
    evidence: tuple[ShadowEvidence, ...]
    level_evidence_ids: tuple[str, ...]
    jev_decision: Literal["ACCEPT", "REJECT", "UNCERTAIN", "SERVICE_FAILURE"]
    jev_policy_version: str
    jev_receipt_hash: str
    session_id: str
    event_id: str
    cohort: Literal["ENGINEERING", "PROSPECTIVE"] = "ENGINEERING"
    market: str = "CRYPTO"
    strategy_version: str = TRIGGER_VERSION

    def __post_init__(self):
        for name in ("as_of", "decision_at", "expires_at", "exit_deadline", "quote_at",
                     "liquidity_at"):
            object.__setattr__(self, name, _time(getattr(self, name)))
        for name in ("entry_trigger", "max_entry_price", "stop", "target", "quantity",
                     "bid", "ask", "observed_dollar_volume"):
            object.__setattr__(self, name, _number(getattr(self, name)))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "level_evidence_ids", tuple(self.level_evidence_ids))
        if (not all((self.case_id, self.symbol, self.venue,
                     self.jev_policy_version, self.jev_receipt_hash,
                     self.session_id, self.event_id))
                or self.market != "CRYPTO" or self.strategy_version != TRIGGER_VERSION
                or self.cohort not in {"ENGINEERING", "PROSPECTIVE"}
                or self.jev_decision not in {"ACCEPT", "REJECT", "UNCERTAIN", "SERVICE_FAILURE"}
                or not isinstance(self.policy, ShadowPolicy)
                or any(not isinstance(e, ShadowEvidence) for e in self.evidence)):
            raise ValueError("INVALID_SHADOW_CASE")
        if not self.as_of <= self.decision_at < self.expires_at <= self.exit_deadline:
            raise ValueError("INVALID_CASE_TIMELINE")
        if (self.quote_at > self.as_of or self.liquidity_at > self.as_of
                or any(e.available_at > self.as_of for e in self.evidence)):
            raise ValueError("FUTURE_CASE_EVIDENCE")
        ids = [e.evidence_id for e in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("DUPLICATE_EVIDENCE_ID")

    @classmethod
    def from_dict(cls, raw):
        raw = dict(raw)
        raw["policy"] = ShadowPolicy(**raw["policy"])
        evidence = []
        for item in raw["evidence"]:
            item = dict(item)
            if "content" in item:
                item["content_json"] = _json(item.pop("content"))
            evidence.append(ShadowEvidence(**item))
        raw["evidence"] = tuple(evidence)
        return cls(**raw)

    def to_dict(self):
        return _safe(asdict(self))

    @property
    def case_hash(self):
        return _hash(self.to_dict())


@dataclass(frozen=True)
class ShadowObservation:
    observation_id: str
    case_id: str
    symbol: str
    venue: str
    kind: Literal["tick", "bar"]
    at: datetime
    received_at: datetime
    trade_price: D | None = None
    bid: D | None = None
    ask: D | None = None
    quote_at: datetime | None = None
    bid_size: D | None = None
    ask_size: D | None = None
    feed_healthy: bool = True
    start_at: datetime | None = None
    open: D | None = None
    high: D | None = None
    low: D | None = None
    close: D | None = None
    volume: D | None = None

    def __post_init__(self):
        for name in ("at", "received_at", "quote_at", "start_at"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, _time(getattr(self, name)))
        for name in ("trade_price", "bid", "ask", "bid_size", "ask_size", "open", "high",
                     "low", "close", "volume"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, _number(getattr(self, name)))
        if (not all((self.observation_id, self.case_id, self.symbol, self.venue))
                or self.kind not in {"tick", "bar"} or type(self.feed_healthy) is not bool
                or self.at > self.received_at
                or (self.quote_at is not None and self.quote_at > self.received_at)):
            raise ValueError("INVALID_OBSERVATION_TIMELINE")
        if self.kind == "tick":
            if (None in (self.trade_price, self.bid, self.ask, self.quote_at)
                    or self.trade_price <= 0 or self.bid <= 0 or self.ask < self.bid):
                raise ValueError("INVALID_TICK")
        elif (None in (self.start_at, self.open, self.high, self.low, self.close, self.volume)
              or not self.start_at < self.at
              or not 0 < self.low <= min(self.open, self.close)
              <= max(self.open, self.close) <= self.high):
            raise ValueError("INVALID_COMPLETED_BAR")

    @classmethod
    def from_dict(cls, raw):
        return cls(**raw)

    def to_dict(self):
        return _safe(asdict(self))


# Short alias for callers that import only this module.
Observation = ShadowObservation


@dataclass(frozen=True)
class Qualification:
    eligible: bool
    reasons: tuple[str, ...]
    reward_risk_at_max: D | None
    assumed_cost_to_risk: D | None


def prequalify(case: ShadowCase) -> Qualification:
    """Numerical packet checks, independent of Jev; not an account risk grant."""
    reasons = []
    t, m, s, p = case.entry_trigger, case.max_entry_price, case.stop, case.target
    valid = 0 < s < t <= m < p
    risk = m - s
    rr = (p - m) / risk if valid else None
    cost = m * case.policy.assumed_round_trip_cost_bps / 10000
    cost_r = cost / risk if valid else None
    if not valid:
        reasons.append("INVALID_LEVELS")
    elif rr < 2:
        reasons.append("MIN_REWARD_RISK")
    if max(m, case.ask) > case.policy.price_ceiling:
        reasons.append("PRICE_CEILING")
    if case.bid <= 0 or case.ask < case.bid:
        reasons.append("INVALID_QUOTE")
    elif (case.ask - case.bid) / ((case.ask + case.bid) / 2) * 10000 > 10:
        reasons.append("MAX_SPREAD")
    if not 0 <= (case.decision_at - case.quote_at).total_seconds() <= 5:
        reasons.append("STALE_QUOTE")
    if not 0 <= (case.decision_at - case.liquidity_at).total_seconds() <= 90:
        reasons.append("STALE_LIQUIDITY")
    if case.observed_dollar_volume < case.policy.minimum_dollar_volume:
        reasons.append("CRYPTO_VENUE_LIQUIDITY_INSUFFICIENT")
    if case.quantity <= 0:
        reasons.append("ZERO_QUANTITY")
    if case.quantity * m > case.observed_dollar_volume * case.policy.maximum_participation:
        reasons.append("CRYPTO_VENUE_PARTICIPATION_LIMIT")
    if cost_r is not None and cost_r > case.policy.maximum_cost_to_risk:
        reasons.append("CRYPTO_ASSUMED_COST_EXCEEDS_POLICY")
    ids = {e.evidence_id for e in case.evidence}
    if not case.level_evidence_ids or not set(case.level_evidence_ids) <= ids:
        reasons.append("LEVEL_PROVENANCE_MISSING")
    return Qualification(not reasons, tuple(reasons), rr, cost_r)


@dataclass(frozen=True)
class ShadowOutcome:
    state: str
    reason: str
    triggered_at: datetime | None = None
    entered_at: datetime | None = None
    closed_at: datetime | None = None
    entered_quantity: D = ZERO
    exited_quantity: D = ZERO
    remaining_quantity: D = ZERO
    entry_notional: D = ZERO
    exit_notional: D = ZERO
    gross_pnl: D | None = None
    assumed_fees: D | None = None
    net_pnl: D | None = None
    net_r: D | None = None
    fee_status: str = "UNKNOWN"
    exposure_quantity_seconds: D = ZERO


@dataclass(frozen=True)
class PairedOutcome:
    case_id: str
    case_hash: str
    as_of: datetime
    qualification: Qualification
    baseline: ShadowOutcome
    treatment: ShadowOutcome | None
    treatment_disposition: str
    session_id: str
    event_id: str
    cohort: str
    scope: str = "MATCHED_OPPORTUNITIES_NOT_PORTFOLIO"

    def to_dict(self):
        return _safe(asdict(self))


def _ordered(case, observations):
    seen, previous, previous_bar = {}, None, None
    result = []
    for obs in observations:
        if not isinstance(obs, ShadowObservation):
            raise ValueError("OBSERVATION_REQUIRED")
        body = obs.to_dict()
        if obs.observation_id in seen:
            if seen[obs.observation_id] != body:
                raise ValueError("OBSERVATION_ID_CONFLICT")
            continue
        if (obs.case_id != case.case_id or obs.symbol != case.symbol or obs.venue != case.venue
                or obs.at < case.decision_at
                or (obs.kind == "bar" and obs.start_at < case.as_of)):
            raise ValueError("OBSERVATION_CASE_MISMATCH_OR_PREDECISION")
        if previous and (obs.at < previous.at or obs.received_at < previous.received_at):
            raise ValueError("OUT_OF_ORDER_OBSERVATION")
        if obs.kind == "bar":
            if previous_bar and obs.start_at < previous_bar.at:
                raise ValueError("OVERLAPPING_BAR_OBSERVATIONS")
            previous_bar = obs
        seen[obs.observation_id], previous = body, obs
        result.append(obs)
    return result


def _trigger_failure(case, obs):
    # This existing function does arithmetic only and does not use self. Never
    # construct ManagedExecution or call authorize_entry from a shadow evaluator.
    return ManagedExecution._trigger_failure(
        None,
        {"market": case.market, "record_json": {"levels": {
            "entry_trigger": case.entry_trigger, "max_entry_price": case.max_entry_price,
            "stop": case.stop, "target": case.target}}},
        {"admitted_at": case.decision_at.isoformat()},
        {"trade_at": obs.at.isoformat(), "quote_at": obs.quote_at.isoformat(),
         "trade_price": obs.trade_price, "bid": obs.bid, "ask": obs.ask,
         "feed_healthy": obs.feed_healthy}, None, obs.received_at,
    )


def evaluate(case, observations, *, as_of):
    """Replay only evidence received by as_of. Rejected eligible paths also replay.

    Exactly the same quantities, cost, spread, entry/exit prices and observations
    apply to both arms. None treatment means a veto or unscored service failure;
    it never means an imaginary losing trade. No confidence is interpreted as P(win).
    """
    as_of = _time(as_of)
    if as_of < case.decision_at:
        raise ValueError("EVALUATION_BEFORE_DECISION")
    ordered = _ordered(case, observations)
    qualification = prequalify(case)
    outcome = (_simulate(case, [o for o in ordered if o.received_at <= as_of], as_of)
               if qualification.eligible else ShadowOutcome("INELIGIBLE", ",".join(
                   qualification.reasons)))
    return PairedOutcome(
        case.case_id, case.case_hash, as_of, qualification, outcome,
        outcome if case.jev_decision == "ACCEPT" else None, case.jev_decision,
        case.session_id, case.event_id, case.cohort,
    )


def _simulate(case, observations, as_of):
    triggered = entered = closed = last_exposure_at = None
    bought = sold = entry_cash = exit_cash = exposure = ZERO
    used_entry_quotes, used_exit_quotes = set(), set()
    reason, ambiguous, exit_requested = "AWAIT_TRIGGER", False, None
    slip = case.policy.slippage_bps / 10000
    for obs in observations:
        now = obs.received_at
        if last_exposure_at is not None:
            exposure += (bought - sold) * D(str((now - last_exposure_at).total_seconds()))
        last_exposure_at = now
        if triggered is None:
            if now >= case.expires_at:
                reason = "EXPIRED_UNTRIGGERED"
                break
            if obs.kind != "tick":
                continue  # A candle low is not a fresh printed-touch observation.
            failure = _trigger_failure(case, obs)
            if failure is None:
                triggered, reason = now, "AWAIT_LIMIT_FILL"
            elif failure in {"STOP_TRADED_BEFORE_TRIGGER", "DATA_FEED_FAILURE",
                             "PRICE_BEYOND_MAX_ENTRY"}:
                reason = failure
                break
            continue  # Never fill using the quote that caused the trigger.
        if obs.kind == "tick":
            fresh = (obs.feed_healthy
                     and 0 <= (now - obs.quote_at).total_seconds() <= 5
                     and 0 <= (now - obs.at).total_seconds() <= 5)
            if not fresh:
                continue
            quote_key = (obs.venue, obs.quote_at, obs.bid, obs.ask)
            if bought == 0 and obs.trade_price <= case.stop:
                reason = "STOP_BEFORE_LIMIT_FILL"
                break
            if bought > sold:
                if obs.trade_price <= case.stop or obs.bid <= case.stop:
                    exit_requested = "STOP"
                elif obs.bid >= case.target:
                    exit_requested = "TARGET"
                elif now >= case.exit_deadline:
                    exit_requested = "TIME_EXIT"
                if (exit_requested and (exit_requested != "TARGET" or obs.bid >= case.target)
                        and obs.bid_size is not None
                        and quote_key not in used_exit_quotes):
                    used_exit_quotes.add(quote_key)
                    qty = min(bought - sold, obs.bid_size)
                    if qty > 0:
                        # Fixed target is a resting limit; do not promise improvement.
                        price = (case.target if exit_requested == "TARGET"
                                 else obs.bid * (1 - slip))
                        sold += qty
                        exit_cash += qty * price
                        reason = exit_requested
                        if sold == bought:
                            closed = now
                            break
            ready = triggered + timedelta(milliseconds=case.policy.entry_latency_ms)
            spread = (obs.ask - obs.bid) / ((obs.bid + obs.ask) / 2) * 10000
            if (not exit_requested and now < case.expires_at and now > triggered
                    and obs.quote_at >= ready and obs.quote_at > triggered
                    and bought < case.quantity and quote_key not in used_entry_quotes
                    and obs.ask_size is not None and spread <= 10):
                price = obs.ask * (1 + slip)
                if obs.trade_price <= case.stop:
                    reason = "STOP_BEFORE_LIMIT_FILL"
                    if bought == 0:
                        break
                elif price <= case.max_entry_price:
                    used_entry_quotes.add(quote_key)
                    qty = min(case.quantity - bought, obs.ask_size)
                    if qty > 0:
                        bought += qty
                        entry_cash += qty * price
                        entered = entered or now
                        reason = "OPEN_PARTIAL" if bought < case.quantity else "OPEN"
        elif bought > sold:
            stop_hit, target_hit = obs.low <= case.stop, obs.high >= case.target
            if (stop_hit or target_hit) and obs.start_at < entered:
                ambiguous, reason = True, "BAR_OVERLAPS_ENTRY"
                break
            if stop_hit and target_hit:
                ambiguous, reason = True, "BAR_TOUCHED_STOP_AND_TARGET"
                break
            if exit_requested == "STOP":
                reason = "PENDING_STOP_EXIT_QUOTE_REQUIRED"
                continue
            if now >= case.exit_deadline:
                # An interval close is not a executable quote at the deadline.
                reason = "TIME_EXIT_QUOTE_REQUIRED"
                continue
            if stop_hit or target_hit:
                exit_requested = "STOP" if stop_hit else "TARGET"
                qty = min(bought - sold, obs.volume * case.policy.maximum_participation)
                if qty > 0:
                    price = (min(case.stop, obs.open) * (1 - slip)
                             if stop_hit else case.target)
                    sold += qty
                    exit_cash += qty * price
                    reason = exit_requested + "_BAR_MODEL"
                    if sold == bought:
                        closed = now
                        break
    if bought > sold and last_exposure_at is not None and not ambiguous:
        exposure += (bought - sold) * D(str((as_of - last_exposure_at).total_seconds()))
    state = ("AMBIGUOUS" if ambiguous else "CLOSED" if closed else "OPEN" if bought
             else "UNFILLED" if triggered else "UNTRIGGERED")
    if not bought and as_of >= case.expires_at and reason in {"AWAIT_TRIGGER", "AWAIT_LIMIT_FILL"}:
        reason = "EXPIRED_UNFILLED" if triggered else "EXPIRED_UNTRIGGERED"
    elif bought > sold and as_of >= case.exit_deadline and not ambiguous:
        reason = "EXIT_EVIDENCE_MISSING" if not exit_requested else "EXIT_LIQUIDITY_INCOMPLETE"
    gross = exit_cash - entry_cash if closed else None
    fee_bps = case.policy.fee_bps_per_side
    fees = ((entry_cash + exit_cash) * fee_bps / 10000 if closed and fee_bps is not None else None)
    net = gross - fees if gross is not None and fees is not None else None
    risk = bought * (case.max_entry_price - case.stop)
    return ShadowOutcome(
        state, reason, triggered, entered, closed, bought, sold, bought - sold,
        entry_cash, exit_cash, gross, fees, net, net / risk if net is not None else None,
        "ASSUMED" if fee_bps is not None else "UNKNOWN", exposure,
    )


class ShadowStore:
    """An isolated SQLite event log. UPDATE/DELETE are denied; replay verifies hashes.

    This is tamper-evident relative to a retained head, not protection from an
    administrator rewriting the entire file. No owner trading tables are used.
    """

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        if created:
            os.chmod(self.path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS shadow_events (
                seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, case_id TEXT NOT NULL, payload TEXT NOT NULL,
                recorded_at TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS shadow_no_update BEFORE UPDATE ON shadow_events
                BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY_SHADOW_LOG'); END;
            CREATE TRIGGER IF NOT EXISTS shadow_no_delete BEFORE DELETE ON shadow_events
                BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY_SHADOW_LOG'); END;
        """)
        self.verify()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def verify(self):
        previous = "0" * 64
        rows = self.conn.execute("SELECT * FROM shadow_events ORDER BY seq")
        for expected, row in enumerate(rows, 1):
            data = dict(row)
            actual = data.pop("hash")
            if (data["seq"] != expected or data["previous_hash"] != previous
                    or _hash(data) != actual):
                raise ValueError("SHADOW_LOG_INTEGRITY_FAILURE")
            previous = actual
        return previous

    def _write(self, event_id, kind, case_id, payload, validate):
        body = _json(payload)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old = self.conn.execute(
                "SELECT * FROM shadow_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if old:
                if old["payload"] != body or old["kind"] != kind or old["case_id"] != case_id:
                    raise ValueError("IMMUTABLE_EVENT_CONFLICT")
                self.conn.execute("COMMIT")
                return False
            validate()
            last = self.conn.execute(
                "SELECT * FROM shadow_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            recorded_at = _time(self.clock())
            if last and recorded_at < _time(last["recorded_at"]):
                raise ValueError("RECORDING_CLOCK_REGRESSION")
            data = dict(seq=last["seq"] + 1 if last else 1, event_id=event_id, kind=kind,
                        case_id=case_id, payload=body, recorded_at=recorded_at.isoformat(),
                        previous_hash=last["hash"] if last else "0" * 64)
            self.conn.execute(
                "INSERT INTO shadow_events VALUES(?,?,?,?,?,?,?,?)",
                (*data.values(), _hash(data)),
            )
            self.conn.execute("COMMIT")
            return True
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def register(self, case):
        if not isinstance(case, ShadowCase):
            raise ValueError("SHADOW_CASE_REQUIRED")

        def validate():
            if case.decision_at > _time(self.clock()):
                raise ValueError("FUTURE_CASE_DECISION")

        return self._write("case:" + case.case_id, "CASE", case.case_id, case.to_dict(), validate)

    def cases(self):
        return [ShadowCase.from_dict(json.loads(row["payload"])) for row in self.conn.execute(
            "SELECT payload FROM shadow_events WHERE kind='CASE' ORDER BY seq")]

    def case(self, case_id):
        row = self.conn.execute(
            "SELECT payload FROM shadow_events WHERE kind='CASE' AND case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise ValueError("UNKNOWN_SHADOW_CASE")
        return ShadowCase.from_dict(json.loads(row["payload"]))

    def observations(self, case_id):
        rows = self.conn.execute(
            "SELECT payload FROM shadow_events WHERE kind='OBSERVATION' AND case_id=? ORDER BY seq",
            (case_id,))
        return [ShadowObservation.from_dict(json.loads(row["payload"])) for row in rows]

    def append(self, observation):
        if not isinstance(observation, ShadowObservation):
            raise ValueError("OBSERVATION_REQUIRED")

        def validate():
            case = self.case(observation.case_id)
            if observation.received_at > _time(self.clock()):
                raise ValueError("FUTURE_OBSERVATION_RECEIPT")
            if case.cohort == "PROSPECTIVE":
                frozen = self.conn.execute(
                    "SELECT recorded_at FROM shadow_events WHERE event_id=?",
                    ("case:" + case.case_id,),
                ).fetchone()
                if observation.at < _time(frozen["recorded_at"]):
                    raise ValueError("PROSPECTIVE_OBSERVATION_PRECEDES_CASE_FREEZE")
            _ordered(case, [*self.observations(case.case_id), observation])

        key = "observation:" + _json([observation.case_id, observation.observation_id])
        return self._write(key, "OBSERVATION", observation.case_id,
                           observation.to_dict(), validate)

    def evaluate(self, case_id, *, as_of):
        self.verify()
        case = self.case(case_id)
        if case.cohort == "PROSPECTIVE":
            frozen = self.conn.execute(
                "SELECT recorded_at FROM shadow_events WHERE event_id=?", ("case:" + case_id,)
            ).fetchone()
            if _time(as_of) < _time(frozen["recorded_at"]):
                raise ValueError("EVALUATION_BEFORE_PROSPECTIVE_CASE_FREEZE")
        return evaluate(case, self.observations(case_id), as_of=as_of)

    def export_metrics(self, *, as_of):
        self.verify()
        return export_metrics([self.evaluate(c.case_id, as_of=as_of) for c in self.cases()])


def export_metrics(results):
    """Descriptive completed-case summaries; incomplete/unknown outcomes stay out of net means."""
    results = list(results)
    if len({r.case_id for r in results}) != len(results):
        raise ValueError("DUPLICATE_METRIC_CASE")

    def arm(outcomes):
        outcomes = list(outcomes)
        closed = [o for o in outcomes if o.state == "CLOSED"]
        known = [o for o in closed if o.net_pnl is not None]
        triggered_count = sum(o.triggered_at is not None for o in outcomes)
        entered_count = sum(o.entered_quantity > 0 for o in outcomes)
        return {
            "case_count": len(outcomes), "states": dict(Counter(o.state for o in outcomes)),
            "triggered_cases": triggered_count, "entered_cases": entered_count,
            "fill_rate_of_triggered": (D(entered_count) / triggered_count
                                       if triggered_count else None),
            "closed_cases": len(closed), "net_known_closed_cases": len(known),
            "unknown_fee_closed_cases": sum(o.fee_status == "UNKNOWN" for o in closed),
            "gross_pnl_closed_subset": (sum((o.gross_pnl for o in closed), ZERO)
                                        if closed else None),
            "net_pnl_known_subset": sum((o.net_pnl for o in known), ZERO) if known else None,
            "mean_net_r_known_closed_subset": (sum((o.net_r for o in known), ZERO) / len(known)
                                               if known else None),
            "assumed_fees_known_closed_subset": (sum((o.assumed_fees for o in known), ZERO)
                                                 if known else None),
            "turnover_notional": sum((o.entry_notional + o.exit_notional for o in outcomes), ZERO),
            "exposure_quantity_seconds": sum((o.exposure_quantity_seconds for o in outcomes), ZERO),
        }

    cohorts = {}
    for cohort in sorted({r.cohort for r in results}):
        rows = [r for r in results if r.cohort == cohort]
        qualified = [r for r in rows if r.qualification.eligible]
        paired = [r for r in qualified if r.treatment_disposition in {"ACCEPT", "REJECT"}
                  and r.baseline.state == "CLOSED" and r.baseline.net_r is not None]
        differences = [ZERO if r.treatment_disposition == "ACCEPT" else -r.baseline.net_r
                       for r in paired]
        cohorts[cohort] = {
            "cases": len(rows), "eligible_cases": len(qualified),
            "jev_dispositions": dict(Counter(r.treatment_disposition for r in rows)),
            "rules_only": arm(r.baseline for r in rows),
            "jev_filtered": arm(r.treatment for r in rows if r.treatment is not None),
            "paired_known_closed_cases": len(paired),
            "mean_treatment_minus_baseline_net_r": (sum(differences, ZERO) / len(differences)
                                                   if differences else None),
            "session_groups": len({r.session_id for r in paired}),
            "event_groups": len({r.event_id for r in paired}),
        }
    return _safe({"version": VERSION, "scope": "MATCHED_OPPORTUNITIES_NOT_PORTFOLIO",
                  "cohorts": cohorts, "portfolio_return": None, "portfolio_drawdown": None,
                  "uncertainty_interval": None, "limitations": [
                      "No shared cash, correlation, concurrency or account allocator is simulated.",
                      "Displayed liquidity and bar participation are fill assumptions, not fills.",
                      "Fee assumptions are not verified costs; unknown fees exclude net metrics.",
                      "Closed subsets exclude open, ambiguous and unfilled opportunities.",
                      "Group counts are descriptive; no independence or confidence interval claim.",
                  ]})
