import os
from dataclasses import dataclass, field
from decimal import Decimal

STRATEGY_VERSION = "CATALYST_RETEST_V1"
PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
MUSE_SCOPES = frozenset({"candidate:create", "candidate:read", "analytics:read"})
AUTHORIZATION_TTL_SECONDS = 5
BASELINE_SOURCE = "BROKER_PREVIOUS_CLOSE"
SCHEMA_VERSION = 25
# Official R remains unset until the user resolves the Phase 5 denominator.
REPORTING_R_METHOD = None


@dataclass(frozen=True)
class Settings:
    database_url: str
    muse_api_token: str = field(repr=False)
    read_only_market_data: bool = False
    market_data_feed: str = "iex"
    diagnostic_symbols: tuple[str, ...] = ()
    max_spread_bps: Decimal = Decimal("10")
    feed_failure_tolerance_seconds: Decimal = Decimal("5")
    risk_database_url: str | None = field(default=None, repr=False)
    risk_authorization_ttl_seconds: int = AUTHORIZATION_TTL_SECONDS
    risk_baseline_source: str = BASELINE_SOURCE
    risk_max_per_sector: int = 1
    risk_max_per_theme: int = 1
    us_admission_policy: str | None = None
    jev_paper_policy: str | None = None
    jev_admission_poll_seconds: Decimal | None = None

    def __post_init__(self):
        if self.us_admission_policy is not None:
            from catalyst_lab.us_admission import admission_profile

            admission_profile(self.us_admission_policy)
            if not self.read_only_market_data or not self.risk_database_url:
                raise ValueError(
                    "US admission requires current broker monitoring and risk baseline"
                )
        if self.jev_paper_policy is not None:
            if self.jev_paper_policy != "JEV_US_SELECTED_FIXED_TEST_V1":
                raise ValueError("Unknown Jev paper policy")
            if not self.us_admission_policy or self.jev_admission_poll_seconds is None:
                raise ValueError(
                    "Jev paper admission requires explicit evidence policy and polling"
                )
            if not self.jev_admission_poll_seconds.is_finite() or not (
                Decimal("0.1") <= self.jev_admission_poll_seconds <= 5
            ):
                raise ValueError("Invalid Jev admission poll interval")

    @classmethod
    def from_env(cls) -> "Settings":
        for name in ("ALPACA_BASE_URL", "APCA_API_BASE_URL"):
            if os.environ.get(name, PAPER_ENDPOINT) != PAPER_ENDPOINT:
                raise ValueError("Only the fixed Alpaca Paper endpoint is permitted")
        database_url = os.environ.get("DATABASE_URL", "")
        token = os.environ.get("MUSE_API_TOKEN", "")
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        if len(token) < 32 or token.startswith("replace-"):
            raise ValueError("Set a random MUSE_API_TOKEN of at least 32 characters")
        enabled = os.environ.get("ALPACA_READ_ONLY_ENABLED", "0")
        if enabled not in {"0", "1"}:
            raise ValueError("ALPACA_READ_ONLY_ENABLED must be 0 or 1")
        return cls(
            database_url,
            token,
            enabled == "1",
            os.environ.get("ALPACA_DATA_FEED", "iex"),
            tuple(
                s.strip() for s in os.environ.get("MARKET_DATA_SYMBOLS", "").split(",") if s.strip()
            ),
            Decimal(os.environ.get("TRIGGER_MAX_SPREAD_BPS", "10")),
            Decimal(os.environ.get("FEED_FAILURE_TOLERANCE_SECONDS", "5")),
            os.environ.get("RISK_DATABASE_URL"),
            int(os.environ["RISK_AUTHORIZATION_TTL_SECONDS"])
            if os.environ.get("RISK_AUTHORIZATION_TTL_SECONDS")
            else AUTHORIZATION_TTL_SECONDS,
            os.environ.get("RISK_BASELINE_SOURCE") or BASELINE_SOURCE,
            int(os.environ.get("RISK_MAX_PER_SECTOR", "1")),
            int(os.environ.get("RISK_MAX_PER_THEME", "1")),
            os.environ.get("US_ADMISSION_POLICY") or None,
            os.environ.get("JEV_PAPER_POLICY") or None,
            Decimal(os.environ["JEV_ADMISSION_POLL_SECONDS"])
            if os.environ.get("JEV_ADMISSION_POLL_SECONDS")
            else None,
        )
