from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from catalyst_lab.config import STRATEGY_VERSION


class State(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    REJECTED = "REJECTED"
    VALIDATED = "VALIDATED"
    WATCHING = "WATCHING"
    INVALIDATED = "INVALIDATED"
    EXPIRED_UNTRIGGERED = "EXPIRED_UNTRIGGERED"
    TRIGGER_CONFIRMED = "TRIGGER_CONFIRMED"
    RISK_CHECK = "RISK_CHECK"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    BROKER_REJECTED = "BROKER_REJECTED"
    CANCELED = "CANCELED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    OPEN = "OPEN"
    TARGET_EXIT = "TARGET_EXIT"
    STOP_EXIT = "STOP_EXIT"
    TIME_EXIT = "TIME_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    CLOSED = "CLOSED"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    strategy_version: Literal[STRATEGY_VERSION]
    signal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    market: Literal["US"]
    ticker: str = Field(min_length=1, max_length=15, pattern=r"^[A-Z][A-Z0-9.-]*$")
    entry_trigger: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=6)
    max_entry_price: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=6)
    stop: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=6)
    target: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=6)
    risk_reward: str | None = Field(default=None, max_length=32)
    catalyst: Literal[
        "EARNINGS",
        "GUIDANCE",
        "M&A",
        "CONTRACT",
        "REGULATORY",
        "PRODUCT",
        "ANALYST",
        "MACRO",
        "LEGAL",
        "MANAGEMENT",
        "SYMPATHY",
        "PARTNERSHIP",
        "LISTING",
        "FLOWS",
        "ENGINEERING_TEST",
    ]
    thesis: str = Field(min_length=1, max_length=1000)
    disproof: str = Field(min_length=1, max_length=1000)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def engineering_label_requires_test_signal(self):
        if self.catalyst == "ENGINEERING_TEST" and not self.signal_id.startswith("TEST-"):
            raise ValueError("Engineering candidates require the reserved TEST- prefix")
        return self

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class BatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_version: str = Field(min_length=1, max_length=64)
    date: date
    candidates: list = Field(min_length=1, max_length=50)

    @field_validator("date", mode="before")
    @classmethod
    def strict_iso_date(cls, value):
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Require an ISO YYYY-MM-DD date")
        return date.fromisoformat(value)


@dataclass(frozen=True)
class Policy:
    # Operational thresholds are mandatory; the frozen plan supplies no numbers.
    quote_max_age_seconds: Decimal
    max_spread_bps: Decimal
    min_average_daily_dollar_volume: Decimal
    evidence_max_age_seconds: Decimal

    def __post_init__(self):
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("Policy thresholds must be positive")


@dataclass(frozen=True)
class Evidence:
    """Internal server evidence. Never accepted in a Muse request body."""

    source: Literal["LAB_FIXTURE", "ALPACA_PAPER"]
    observed_at: datetime
    session_date: date
    official_open: datetime
    official_close: datetime
    calendar_provider: str
    ticker: str
    asset_class: str
    tradable: bool
    data_provider: str
    data_feed: str
    quote_timestamp: datetime
    bid: Decimal
    ask: Decimal
    average_daily_dollar_volume: Decimal
    feed_healthy: bool
    reconciled_session: date | None
    unexplained_positions: bool
    sector: str | None
    theme: str | None
    open_sectors: frozenset[str]
    open_themes: frozenset[str]
    equity: Decimal
    start_of_day_equity: Decimal
    realized_pnl_today: Decimal
    open_unrealized_pnl: Decimal
    open_planned_risk: Decimal
    daily_halted: bool
    admission_metadata: dict | None = None


@dataclass(frozen=True)
class Decision:
    passed: bool
    failed_rule: str | None
    reason: str
    expires_at: datetime | None = None
    computed_qty: int | None = None
    planned_risk: Decimal | None = None
