"""Local executable composition for authenticated Muse input and supervised workers.

Run ``python -m catalyst_lab.managed_app`` with explicit injected configuration.
This command never provisions/migrates a database and cannot bind a public host.
"""

import asyncio
import os
import re
from collections.abc import Mapping
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from catalyst_lab.agent_identity import LEGACY_AGENT_ID, validated_agent_tokens
from catalyst_lab.ai_mode import NO_AI_MODE
from catalyst_lab.jev_contract import strict_json
from catalyst_lab.learning_intake import LearningIntake, LearningPolicy
from catalyst_lab.managed_classification import (
    BUCKET_CLASSIFICATION_POLICY,
    CLASSIFICATION_POLICIES,
    CRYPTO_BUCKET_POLICIES,
    DEFAULT_CRYPTO_BUCKET,
    crypto_bucket_config,
    initialize_managed_classifications,
)
from catalyst_lab.managed_runtime import build_runtime_from_env, install_fatal_exit
from catalyst_lab.managed_service import create_managed_app
from catalyst_lab.position_news import PositionNewsService
from catalyst_lab.research_context import CryptoAssetReader, ResearchContextService
from catalyst_lab.research_report_v3 import ResearchCapabilityUnavailable
from catalyst_lab.research_schedule import ResearchSchedule
from catalyst_lab.research_validate import ReportValidator
from catalyst_lab.research_withdrawal import ResearchWithdrawals
from catalyst_lab.scan_sources import AlpacaMarketSource
from catalyst_lab.trade_review import TradeReviewService


@dataclass(frozen=True)
class AppSettings:
    port: int
    token: str = field(repr=False)
    report_seconds: int
    crypto_symbols: tuple[str, ...] | None
    classification_policy: str
    us_mapping: tuple[dict, ...]
    # Private config v2 role tokens (``token`` is then Muse's); None keeps the single token.
    status_token: str | None = field(default=None, repr=False)
    operator_token: str | None = field(default=None, repr=False)
    # Owner crypto buckets; required by the bucket policy, optional with the built-in sectors
    # (ALPACA_CRYPTO_SECTORS_V1, which they then override) and refused with any other policy.
    crypto_buckets: dict | None = None
    # Plan 1.3: one research-agent credential per agent ID ({agent_id: token}); ``token``
    # remains the legacy identity (agent ``muse``). None or empty configures no agent token.
    agent_tokens: Mapping[str, str] | None = field(default=None, repr=False)
    # MANAGED_RESEARCH_SCHEDULE_JSON (RESEARCH_SCHEDULE_V1 or _V2). None: report V3 is refused
    # (503). The execution's run supersession follows its version (package research-loop-app).
    research_schedule: ResearchSchedule | None = None

    def __post_init__(self):
        if (
            type(self.port) is not int
            or not 1024 <= self.port <= 65535
            or not isinstance(self.token, str)
            or len(self.token) < 32
            or any(c.isspace() for c in self.token)
            or type(self.report_seconds) is not int
            or not 1 <= self.report_seconds <= 86400
            or self.classification_policy not in CLASSIFICATION_POLICIES
            or not isinstance(self.us_mapping, tuple)
            or (self.classification_policy == BUCKET_CLASSIFICATION_POLICY
                and self.crypto_buckets is None)
            or (self.crypto_buckets is not None
                and self.classification_policy not in CRYPTO_BUCKET_POLICIES)
            or not (self.research_schedule is None
                    or isinstance(self.research_schedule, ResearchSchedule))
        ):
            raise ValueError("REQUIRED_MANAGED_APP_CONFIGURATION_INVALID")
        if self.crypto_buckets is not None:
            crypto_bucket_config(self.crypto_buckets)
        if self.crypto_symbols is not None and (
            not isinstance(self.crypto_symbols, tuple)
            or len(self.crypto_symbols) > 1000
            or len(set(self.crypto_symbols)) != len(self.crypto_symbols)
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{1,16}/USD", s)
                   for s in self.crypto_symbols)
        ):
            raise ValueError("CRYPTO_CLASSIFICATION_SYMBOL_INVALID")
        configured = set()
        for row in self.us_mapping:
            if (
                not isinstance(row, dict)
                or set(row) != {"ticker", "sector", "theme"}
                or any(not isinstance(value, str) for value in row.values())
            ):
                raise ValueError("OPERATOR_US_CLASSIFICATION_REQUIRED")
            if (
                row["ticker"] in configured
                or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", row["ticker"])
                or any(
                    not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", row[k]) for k in ("sector", "theme")
                )
            ):
                raise ValueError("OPERATOR_US_CLASSIFICATION_REQUIRED")
            configured.add(row["ticker"])
        if (self.status_token, self.operator_token) != (None, None) and (
            any(not isinstance(t, str) or len(t) < 32 or any(c.isspace() for c in t)
                for t in (self.status_token, self.operator_token))
            or len({self.token, self.status_token, self.operator_token}) != 3
        ):
            raise ValueError("SEPARATE_ROLE_TOKENS_REQUIRED")
        if self.agent_tokens is not None:
            tokens = validated_agent_tokens(
                self.agent_tokens, reserved=(self.token, self.status_token, self.operator_token)
            )
            object.__setattr__(self, "agent_tokens", MappingProxyType(tokens))

    @classmethod
    def from_env(cls):
        try:
            symbols = strict_json(os.environ["MANAGED_CRYPTO_CLASSIFICATIONS_JSON"])
            if symbols is not None and not isinstance(symbols, list):
                raise ValueError
            buckets = os.environ.get("MANAGED_CRYPTO_BUCKETS_JSON")
            return cls(
                int(os.environ["MANAGED_HTTP_PORT"]),
                os.environ["MANAGED_API_TOKEN"],
                int(os.environ["MANAGED_REPORT_MAX_SECONDS"]),
                tuple(symbols) if symbols is not None else None,
                os.environ["MANAGED_CLASSIFICATION_POLICY"],
                tuple(strict_json(os.environ["MANAGED_US_CLASSIFICATIONS_JSON"])),
                crypto_buckets=strict_json(buckets) if buckets else None,
                # Optional; present but invalid refuses startup like every other setting.
                research_schedule=ResearchSchedule.from_env(os.environ),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("REQUIRED_MANAGED_APP_CONFIGURATION_MISSING_OR_INVALID") from None


def research_context_for(runtime, settings, source_factory=AlpacaMarketSource, *, reviews=None):
    """The research-context service for this runtime; it opens no client until first use.

    Asset metadata is read with the runtime's paper credentials through the read-only paper
    transport over the account's request-budget governor (``runtime.broker_budget``); quotes,
    trades and hourly bars through a separate read-only market-data source. Without the
    governor or paper credentials the universe is unavailable (HTTP 503), never unbudgeted.
    """
    from catalyst_lab.alpaca import AlpacaCredentials
    from catalyst_lab.broker_budget import BrokerBudget

    budget = getattr(runtime, "broker_budget", None)
    credentials = getattr(runtime, "credentials", None)

    def assets():
        if not isinstance(budget, BrokerBudget) or not isinstance(credentials, AlpacaCredentials):
            raise ResearchCapabilityUnavailable("RESEARCH_UNIVERSE_UNAVAILABLE")
        return CryptoAssetReader(credentials, transport=budget.transport())

    def market():
        return source_factory(credentials, runtime.source.policy, clock=runtime.now)

    policy = getattr(getattr(runtime, "research", None), "policy", None)
    return ResearchContextService(
        runtime.execution.repo, clock=runtime.now, schedule=settings.research_schedule,
        asset_reader=assets, market_source=market,
        report_format={
            "max_report_age_seconds": getattr(policy, "max_packet_age_seconds", None),
            "report_max_seconds": settings.report_seconds,
        },
        reviews=reviews,
    )


def learning_intake_for(runtime, settings, context):
    """The research agents' outlook and post-mortem intake (package learning-app): the report
    freshness bound of the research cycle's policy, the research schedule and the research
    context's cached, budgeted universe. None (the routes answer 503) without a cycle policy."""
    policy = getattr(getattr(runtime, "research", None), "policy", None)
    max_age = getattr(policy, "max_packet_age_seconds", None)
    if max_age is None:
        return None
    return LearningIntake(
        runtime.execution.store, clock=runtime.now,
        policy=LearningPolicy(max_age_seconds=max_age, schedule=settings.research_schedule),
        universe=getattr(context, "universe_snapshot", None),
    )


def configured_agents(settings):
    """The agent IDs that can answer reviews: the legacy credential's agent and every research
    agent credential (package day-review routes requests to them, plan 4.6.7)."""
    return frozenset({LEGACY_AGENT_ID, *(settings.agent_tokens or {})})


def public_status(raw):
    """The runtime's raw status as ``GET /api/v1/lab/status`` serves it (allowlisted names)."""
    return {
        "worker_state": "RUNNING"
        if raw.get("workers_alive", raw.get("running", False))
        else "STOPPED",
        "last_cycle_at": raw.get("last_research_tick"),
        # The last reconciliation that completed clean (package learning-app): never null
        # merely because a pass is running. Entry readiness is ``entry_ready``.
        "last_reconciliation_at": raw.get("last_clean_reconciliation_at"),
        "trade_stream_connected": raw.get("trade_updates_connected", raw.get("stream_ready")),
        "market_feed_connected": all(
            raw.get("market_streams", {}).get(market, False)
            for market in raw.get("required_market_streams", raw.get("market_streams", {}))
        ),
        "market_streams": raw.get("market_streams", {}),
        "market_data_authority": raw.get("market_data_authority"),
        "management_review_enabled": raw.get("position_jev_configured") is True,
        "jev_breaker": raw.get("jev_breaker"),
        "jev_calls_today": raw.get("jev_calls_today"),
        "error_code": raw.get("error"),
        "mode": "SUPERVISED_PAPER_TEST",
        "entry_ready": raw.get("entry_ready") is True,
        "research_healthy": raw.get("research_healthy") is True,
        "last_protection_tick": raw.get("last_protection_tick"),
        "executor_ownership": raw.get("executor_ownership"),
        "required_market_streams": raw.get("required_market_streams", []),
        "schema_version": raw.get("schema_version"),
        "code_version": raw.get("code_version"),
        "configuration_hash": raw.get("configuration_hash"),
        "release_commit": raw.get("release_commit"),
        "account_safety_healthy": raw.get("account_safety_healthy") is True,
        # Pending operator flatten requests; the watchdog alarms FLATTEN_PENDING after 60 s.
        "operator_flatten": raw.get("operator_flatten"),
        # Unreleased halts (EXECUTION_HALT_ACTIVE) and closes refused repeatedly
        # (EXIT_REFUSED_REPEATEDLY), both raised by the watchdog.
        "execution_halts": raw.get("execution_halts"),
        "exit_refusal_alarms": raw.get("exit_refusal_alarms"),
        # CRYPTO_MAINTENANCE_V1: pending exit flags (EXIT_FLAG_PENDING) and maintained trades
        # whose last review failed (MAINTENANCE_REVIEW_FAILING), both raised by the watchdog.
        "trade_maintenance": raw.get("trade_maintenance"),
        # CRYPTO_24H_REVIEW_V1 (package day-review): reviews in progress and failing ones
        # (DAY_REVIEW_JEV_FAILING, raised by the watchdog).
        "day_reviews": raw.get("day_reviews"),
        # CRYPTO_GAP_RESUME_V1: setups held for their gap check (GAP_RESUME_CHECK_OVERDUE).
        "gap_resume": raw.get("gap_resume"),
        # JEV_SPEND_GUARD_V1 (package jev-budget): the month's Jev spend, projections,
        # budget and tier (JEV_BUDGET_THROTTLED / _TIGHT / _EXHAUSTED on a change).
        "jev_budget": raw.get("jev_budget"),
        # CRYPTO_COINBASE_TRIGGER_V1 / CRYPTO_STOP_BREACH_V3: Coinbase's public feed, the products
        # it serves and each unhealthy product's code (null without the feed).
        "reference_feed": raw.get("reference_feed"),
        # CRYPTO_STREAM_CAPACITY_V1: the coins the crypto stream wants against its capacity
        # (15) and the offered picks held back until a slot frees up.
        "crypto_stream": raw.get("crypto_stream"),
    }


def create_application(runtime, settings, *, source_factory=AlpacaMarketSource,
                       research_context=None, health_check_database=True):

    def status():
        return public_status(runtime.status())

    # Package day-review: the agent routes, and the runtime's reviews ask the configured agents.
    agents = configured_agents(settings)
    reviews = TradeReviewService(runtime.execution.store, clock=runtime.now, agents=agents)
    if getattr(runtime, "day_reviews", None) is not None:
        runtime.day_reviews.agents = agents
    context = research_context if research_context is not None else research_context_for(
        runtime, settings, source_factory, reviews=reviews
    )
    # Package research-loop-app: admission and the runtime's retirement pass supersede by the
    # version of the schedule intake uses (RESEARCH_RUN_SUPERSESSION_V2 under
    # RESEARCH_SCHEDULE_V2), set before the runtime starts.
    configure = getattr(runtime.execution, "configure_research_schedule", None)
    if callable(configure):
        configure(settings.research_schedule)
    # Package agent-api: report-V3 intake's dry run with admission warnings (writes nothing).
    validator = ReportValidator(runtime.research, context, repo=runtime.execution.repo,
                                max_seconds=settings.report_seconds)
    # NO_AI_MODE_V1 (package oss-packaging, ai_mode.py): no research report can wait on a judge,
    # so the report intake and its dry run are not served (503 MUSE_REPORT_INTAKE_NOT_CONFIGURED
    # and RESEARCH_REPORT_VALIDATE_NOT_CONFIGURED).
    reports_served = getattr(runtime, "ai_mode", None) != NO_AI_MODE

    def submit_report(raw):
        return asyncio.to_thread(runtime.research.start_report, raw,
                                 max_seconds=settings.report_seconds, v3=context.intake())

    app = create_managed_app(
        runtime.research,
        runtime.execution.store,
        api_token=settings.token,
        status_token=settings.status_token,
        operator_token=settings.operator_token,
        agent_tokens=settings.agent_tokens,
        runtime_status=status,
        # Report V3 bodies also receive the schedule and the tradable universe; V2 and legacy
        # bodies never read them.
        report_submit=submit_report if reports_served else None,
        position_news=PositionNewsService(runtime.execution.store, clock=runtime.now),
        research_context=context,
        trade_reviews=reviews,
        health_check_database=health_check_database,
        # Package learning-app: the agents' outlooks and post-mortems (never sent to Jev).
        learning_intake=learning_intake_for(runtime, settings, context),
        # Package research-loop-app: an agent withdraws its own unfilled picks (no broker call).
        research_withdrawals=ResearchWithdrawals(runtime.execution.store, clock=runtime.now),
        report_validate=((lambda raw: asyncio.to_thread(validator.validate, raw))
                         if reports_served else None),
    )

    def initialize_classifications():
        # Broker eligibility/classification only. Never discover/rank opportunities.
        crypto_symbols = set(settings.crypto_symbols or ())
        # Only the V1 shared theme and the CRYPTO_OTHER default bucket classify discovered
        # symbols; with REFUSE the owner's buckets are the complete crypto universe.
        discover = settings.crypto_buckets is None or (
            crypto_bucket_config(settings.crypto_buckets)["unlisted"] == DEFAULT_CRYPTO_BUCKET
        )
        if settings.crypto_symbols is None and discover:
            source = source_factory(runtime.credentials, runtime.source.policy, clock=runtime.now)
            try:
                metadata, issues = source._metadata("CRYPTO")
                if issues:
                    raise ValueError("CRYPTO_CLASSIFICATION_UNIVERSE_UNAVAILABLE")
                crypto_symbols.update(
                    symbol
                    for symbol, asset in metadata.items()
                    if symbol.endswith("/USD")
                    and asset.get("status") == "active"
                    and asset.get("tradable") is True
                    and asset.get("class") == "crypto"
                )
            finally:
                source.close()
        return initialize_managed_classifications(
            runtime.execution.repo,
            runtime.execution.broker,
            policy_id=settings.classification_policy,
            us_mapping=settings.us_mapping,
            crypto_symbols=sorted(crypto_symbols),
            crypto_buckets=settings.crypto_buckets,
        )

    @asynccontextmanager
    async def lifespan(app):
        try:
            if hasattr(runtime, "acquire_ownership"):
                await asyncio.to_thread(runtime.acquire_ownership)
            app.state.classification_checkpoint = await asyncio.to_thread(
                initialize_classifications
            )
            await asyncio.to_thread(runtime.start)
            yield
        finally:
            try:
                await asyncio.to_thread(runtime.stop)
            finally:
                resources = (
                    *getattr(runtime, "owned_resources", ()),
                    runtime.source,
                    runtime.execution.broker,
                    context,  # Closes only the clients the context service opened.
                )
                seen = set()
                with ExitStack() as cleanup:
                    for resource in resources:
                        if id(resource) not in seen:
                            cleanup.callback(resource.close)
                            seen.add(id(resource))

    app.router.lifespan_context = lifespan
    app.state.managed_runtime = runtime
    app.state.research_context = context
    return app


def build_app_from_env(*, runtime_builder=build_runtime_from_env,
                       source_factory=AlpacaMarketSource, health_check_database=True):
    """The app from the process environment; the Railway trader passes
    ``health_check_database=False`` (``create_managed_app``)."""
    settings = AppSettings.from_env()
    if "MANAGED_STATUS_TOKEN" in os.environ or "MANAGED_OPERATOR_TOKEN" in os.environ:
        # Injected by the private v2 launcher from separate owner-only token files.
        settings = replace(settings, status_token=os.environ.get("MANAGED_STATUS_TOKEN"),
                           operator_token=os.environ.get("MANAGED_OPERATOR_TOKEN"))
    if "MANAGED_AGENT_TOKENS_JSON" in os.environ:
        # {agent_id: token}, injected by the private v2 launcher from per-agent token files.
        try:
            agent_tokens = strict_json(os.environ["MANAGED_AGENT_TOKENS_JSON"])
        except (TypeError, ValueError):
            # A decode error object keeps the document; never let the tokens propagate.
            raise ValueError("SEPARATE_AGENT_TOKENS_REQUIRED") from None
        settings = replace(settings, agent_tokens=agent_tokens)
    runtime = runtime_builder()
    return create_application(runtime, settings, source_factory=source_factory,
                              health_check_database=health_check_database), settings


LOOPBACK = "127.0.0.1"


def serve(app, settings, *, host=LOOPBACK, sockets=None):
    """Run the app until shutdown and exit with its status (shared with the Railway trader).

    Loopback is the default everywhere; only ``catalyst_lab.cloud_runtime`` passes the
    already-bound dual-stack socket of a process that is actually running on Railway.
    """
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=settings.port, access_log=False, log_level="warning"
    ))
    runtime = getattr(getattr(app, "state", None), "managed_runtime", None)
    if runtime is not None:
        # A lost executor lease stops the server, then this process exits non-zero so the
        # supervisor restarts it through normal startup (plan phase 0, 2026-09-26).
        install_fatal_exit(runtime, lambda: setattr(server, "should_exit", True))
    try:
        if sockets is None:
            server.run()
        else:
            server.run(sockets=sockets)
    except KeyboardInterrupt:
        pass
    code = getattr(runtime, "exit_code", None)
    if code is not None:
        raise SystemExit(code)
    if not server.started:
        raise SystemExit(3)  # uvicorn's STARTUP_FAILURE, as uvicorn.run exits.


def main():
    try:
        app, settings = build_app_from_env()
    except Exception:
        raise SystemExit("Managed paper app configuration or startup validation failed.") from None
    serve(app, settings)


if __name__ == "__main__":
    main()
