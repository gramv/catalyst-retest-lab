"""Supervised research-agent session over the real HTTP intake, with a simulated paper venue.

THIS IS A SUPERVISED LOCAL SESSION TOOL, NOT THE PRODUCTION LAUNCHER (that is
``python -m catalyst_lab.managed_app``). It deliberately imports the test fixtures' paper venue
(``tests.test_managed_execution.ManagedVenue``), exactly as ``scripts/prove_managed_wiring.py``
imports test helpers: there are no broker credentials, no broker network call and no market
stream. Every synthetic print, quote, completed bar, crypto asset-metadata row, order and fill
it creates is labelled ``SESSION_SIMULATION``. The research side is real: an external agent
(``fable``) submits an ``AGENT_RESEARCH_REPORT_V2`` report (or, under the top-K rule, an
``AGENT_RESEARCH_REPORT_V3`` report over the session's schedule and simulated crypto universe)
with its own token over loopback HTTP, ``ResearchCycle`` records it, the selection Jev reviews
it (a scripted fixture transport, or the real pinned TypeSafe model behind a hard call cap),
evidence tasks are answered over HTTP (top-K has none: it ranks the run once), and selections
cross the unchanged admission SQL and risk gate (``JEV_MANAGED_RISK_V2``).

The session ledger is a NEW disposable PostgreSQL cluster in ``/tmp/catalyst-session-*``;
this tool refuses any other path or an existing directory, and never opens an owner ledger.

Commands (full procedure: docs/OPERATIONS-RUNBOOK.md, "Supervised research-agent session"):

  start   create the ledger and tokens, serve the intake on 127.0.0.1:PORT and a control
          socket; runs until ``stop`` or SIGINT (then exports and stops the cluster)
  tick    run every open cycle's review tick and publication; print dispositions, the B1
          shadow (or, for a B2 cycle, the B2 components and dissent), receipts, provider
          latency, open evidence tasks and Jev calls used
  execute admit every selected, unexpired packet (a report V3 pick's system check reads a
          SESSION_SIMULATION quote at the pick's own current price); with --simulate-prints
          drive each WATCHING setup through a SESSION_SIMULATION trigger, entry, protection,
          one management review and a target exit (a report-V3 crypto setup in the
          JEV_MANAGED arm gets one CRYPTO_MAINTENANCE_V2 review instead: price moves to +1R,
          the milestone triggers the review, its answer is checked and applied, a raised stop
          is replaced at the simulated venue)
          (with --hold-open a report-V3 crypto trade in the JEV_MANAGED arm stays open after
          its maintenance review, for the 24-hour review below; with --maintenance-minutes N
          (2-10) that trade gets N-1 further reviews, one per completed minute on the
          session's maintenance clock, which moves one minute before each)
  review  CRYPTO_24H_REVIEW_V2 (package day-review; V2's answer rule: package answer-rules):
          move the session's review clock (--at request: 30 minutes before the earliest open
          trade's T; --at review: T; --minutes N: N minutes on) and run one pass of the
          24-hour reviews and early exits, then let the protection loop act on the
          decisions (a raised stop is replaced, an exit sells at the simulated bid); prints
          each trade's review, the agent's pending requests and the Jev calls used. The
          review clock stays where a step puts it (it does not move while the agent or the
          operator answers); only --at and --minutes move it, forward only. The venue stays
          on the wall clock
  export  events (with audit verification), decisions, research funnel, selection replay
          and acceptance-evidence manifests
  status  session configuration and counts
  stop    export (unless --no-export) and shut down (the cluster too, unless --keep)
  submit / answer / unavailable
          the agent's client helpers: HTTP calls with the fable token
  reviews / review-answer / flag-answer / exit-flag
          the agent's review helpers (package day-review), HTTP calls with the fable token:
          list its pending 24-hour reviews and Jev exit flags, answer a review round
          (AGENT_REVIEW_ANSWER_V1), answer a Jev exit flag, raise its own exit flag
          (AGENT_EXIT_FLAG_V1)

There is no background loop: Jev is called only by ``tick`` (selection), by
``execute --simulate-prints`` (one management or maintenance review per simulated position) and
by ``review`` (24-hour review and early-exit questions).
A selection stays admissible only inside the cycle's review window: 30 minutes from the
report's receipt
(deploy/private-paper.example.json's ``review_validity_seconds``), never extended by an
evidence revision. Tokens are written to mode-0600
files and never printed; ``--jev typesafe`` spends provider credits and is run only by the
coordinator or the owner.
"""

import argparse
import asyncio
import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from catalyst_lab import crypto_holding, crypto_maintenance, day_review, jev_budget
from catalyst_lab import research_selection_topk as topk_rule
from catalyst_lab.jev_contract import (
    BOTH_PICK_QUESTIONS,
    CHART_PICK_QUESTIONS,
    INSUFFICIENT,
    JEV_MODEL,
    NEWS_PICK_QUESTIONS,
    SKEPTIC,
    SKEPTIC_V2,
    digest,
    encoded,
    strict_json,
)
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy, ReviewResult
from catalyst_lab.managed_classification import DEFAULT_CRYPTO_BUCKET
from catalyst_lab.managed_execution import ManagedExecution
from catalyst_lab.market import NY
from catalyst_lab.repository import json_safe
from catalyst_lab.research_context import STABLECOINS
from catalyst_lab.research_ranking import QUALITY, QUALITY_V2, QUALITY_V3
from catalyst_lab.research_schedule import ResearchSchedule
from catalyst_lab.research_selection_b1 import (
    B1_POLICY,
    B2_POLICY,
    SHADOW_KIND,
    V2_POLICY,
    SelectionRule,
)
from catalyst_lab.research_selection_b2 import COMPONENTS as B2_COMPONENTS

SIMULATION = "SESSION_SIMULATION"
AGENT_ID = "fable"
SESSION_FORMAT = "AGENT_RESEARCH_SESSION_V1"
ROOT_PARENT = Path("/tmp")
# Short on purpose: PostgreSQL's socket path (root/socket/.s.PGSQL.55437) must stay under the
# 103-byte Unix-socket limit.
ROOT_NAME = re.compile(r"catalyst-session-[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
SESSION_FILE = "session.json"
STOPPED_FILE = "stopped.json"
CONTROL_SOCKET = "control.sock"
TOKEN_ROLES = ("legacy_muse", "status", "operator", AGENT_ID)
CAP_CODE = "JEV_CALL_CAP_REACHED"
RISK_POLICY_ID = "JEV_MANAGED_RISK_V2"
# CyclePolicy(selection_limit, review_deadline_seconds, claim_lease_seconds,
# max_packet_age_seconds, max_inflight); review_validity_seconds follows
# deploy/private-paper.example.json (1800 s), so evidence tasks are answerable as in production.
CYCLE_POLICY = (10, 10, 15, 60, 30)
REVIEW_VALIDITY_SECONDS = 1800
# The reviewer deadline equals the cycle's review deadline (ResearchCycle refuses a longer
# one); attempts, backoff and breaker mirror the approved Gate 1 numbers.
RELIABILITY = (10, 3, 0.25, 3, 30)
CRYPTO_BUCKETS = {
    "L1": ("BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOT/USD", "XTZ/USD"),
    "PAYMENTS": ("XRP/USD", "LTC/USD", "BCH/USD", "DOGE/USD", "SHIB/USD", "PEPE/USD", "TRUMP/USD"),
    "DEFI": ("UNI/USD", "AAVE/USD", "CRV/USD", "SUSHI/USD", "YFI/USD", "MKR/USD"),
    "INFRA": ("LINK/USD", "GRT/USD"),
}
CRYPTO_PAIRS = tuple(sorted(symbol for pairs in CRYPTO_BUCKETS.values() for symbol in pairs))
# --extra-crypto-pairs: further Alpaca USD pairs the research agent may pick, classified in the
# CRYPTO_OTHER default bucket exactly as production classifies unlisted Alpaca pairs.
EXTRA_PAIR = re.compile(r"[A-Z0-9]{2,15}/USD")
MAX_EXTRA_PAIRS = 200
SIMULATED_INCREMENT = "0.00000001"
HALF_SPREAD = D("0.0005")  # Synthetic quotes are 5 bps wide, inside the 10 bps entry limit.
# Top-K reviews every pick twice (pick questions, then QUALITY_V3): 20 picks need 40 calls
# before any retry, so the old default of 40 stopped a full report short (2026-09-27 run).
DEFAULT_MAX_JEV_CALLS = 100
DEFAULT_REPORT_MAX_SECONDS = 1800  # deploy/private-paper.example.json's value.
MANAGEMENT_ACTIONS = ("HOLD", "TIGHTEN_STOP", "EXTEND_TARGET", "TIGHTEN_AND_EXTEND")
MANAGEMENT_QUESTIONS = frozenset({"thesis_status", "action", "stop_option", "target_option"})
# CRYPTO_MAINTENANCE_V1 (JEV_MANAGED_POSITION_QUESTIONS_V4): the fixture script's
# "maintenance" answer for a maintained report-V3 crypto trade.
MAINTENANCE_ACTIONS = crypto_maintenance.ACTIONS
MAINTENANCE_QUESTIONS = frozenset({"trade_reason", "action", "stop_option", "target_option"})
# Package day-review: JEV_DAY_REVIEW_QUESTIONS_V1 (agent_case only when the agent answered) and
# JEV_EARLY_EXIT_QUESTIONS_V1, answered by the fixture script's "day_review",
# "day_review_final" and "early_exit" labels.
DAY_REVIEW_QUESTIONS = frozenset({"trade_reason", "decision", "stop_option", "target_option"})
EARLY_EXIT_QUESTIONS = frozenset({"trade_reason", "agent_case", "exit_now"})
DAY_REVIEW_DEFAULT = {"trade_reason": "INTACT", "agent_case": "HOLDS", "decision": "CONTINUE",
                      "stop_option": "KEEP", "target_option": "KEEP"}
EARLY_EXIT_DEFAULT = {"trade_reason": "WEAKENED", "agent_case": "HOLDS", "exit_now": "EXIT"}
QUALITY_LEVELS = {"WEAK": 0, "ADEQUATE": 1, "STRONG": 2}
# Report V3 (top-K sessions): the deploy example's RESEARCH_SCHEDULE_V1 and the session's
# simulated crypto universe (the owner-bucket pairs, the only classified symbols).
SESSION_SCHEDULE = ResearchSchedule("America/New_York", ("08:00",), 60)
# CRYPTO_MAINTENANCE_V3 (package jev-budget): the session's spend guard runs with the deploy
# example's monthly Jev budget over the session's own database, which holds only the session's
# own calls (cents), so its tier stays NORMAL: V2's per-minute cadence.
SESSION_SPEND = jev_budget.SpendConfig(D("50"))
PICK_SETS = {"TOPK_NEWS": NEWS_PICK_QUESTIONS, "TOPK_CHART": CHART_PICK_QUESTIONS,
             "TOPK_BOTH": BOTH_PICK_QUESTIONS}
# The published packet's top-K fields, exported in decisions.json.
TOPK_SELECTION_FIELDS = ("rank", "agent_rank", "adjusted_score", "quality_score", "uncertain",
                         "dissent_tied", "quality_policy", "quality_receipt_id",
                         "ranking_event_id", "ranking_event_seq", "k", "replacement_for")
PICK_PASSING = {"news_stale": "NO", "already_priced": "LOW", "mechanism_contradicted": "NO",
                "factual_claims_supported": "SUPPORTED", "prices_consistent": "YES",
                "levels_supported_by_bars": "YES", "setup_already_broken": "NO",
                "verdict": "NEEDS_REVIEW"}
CLAIMANT = AGENT_ID + "-session"
TERMINAL_ORDERS = frozenset({"filled", "canceled", "cancelled", "expired", "rejected", "replaced"})
TERMINAL_SETUPS = frozenset({"CLOSED", "INVALIDATED", "EXPIRED_UNTRIGGERED", "RISK_REJECTED",
                             "REJECTED"})
_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_UNAVAILABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


class SessionRefused(Exception):
    """A refusal whose message is always an UPPER_SNAKE code, never a submitted value."""


def _code(exc):
    text = str(exc)
    return text if len(text) <= 80 and _CODE.fullmatch(text) else type(exc).__name__


def _utcnow():
    return datetime.now(UTC)


def _text(value):
    """Plain decimal text; never exponent notation for small crypto prices."""
    return format(value, "f")


def _stamp():
    return _utcnow().strftime("%Y%m%dT%H%M%S%fZ")


# --- Session directory, private files ------------------------------------------------------


def _session_path(value):
    raw = Path(value).expanduser()
    if (
        not raw.is_absolute()
        or not ROOT_NAME.fullmatch(raw.name)
        or raw.parent.resolve() != ROOT_PARENT.resolve()
    ):
        raise SessionRefused("SESSION_ROOT_MUST_BE_TMP_CATALYST_SESSION")
    return raw.parent.resolve() / raw.name


def new_session_root(value):
    """``/tmp/catalyst-session-*`` that does not exist yet; never an existing directory."""
    root = _session_path(value)
    if os.path.lexists(root):
        raise SessionRefused("SESSION_ROOT_ALREADY_EXISTS")
    return root


def existing_session_root(value):
    root = _session_path(value)
    try:
        info = root.lstat()
    except OSError:
        raise SessionRefused("SESSION_ROOT_MISSING") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SessionRefused("SESSION_ROOT_NOT_PRIVATE")
    return root


def _private_opener(path, flags):
    return os.open(path, flags, 0o600)


def _write_private(path, text):
    with open(path, "x", encoding="utf-8", opener=_private_opener) as stream:
        stream.write(text)


def _json_text(value):
    return json.dumps(json_safe(value), indent=2, sort_keys=True) + "\n"


def _append_private(path, value):
    with open(path, "a", encoding="utf-8", opener=_private_opener) as stream:
        stream.write(json.dumps(json_safe(value), sort_keys=True) + "\n")


def _read_private(path):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise SessionRefused("SESSION_FILE_MISSING") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SessionRefused("SESSION_FILE_NOT_PRIVATE")
    return path.read_text(encoding="utf-8")


def _private_directory(path):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_tokens(root):
    """One fresh token per role, each in its own mode-0600 file; values are never printed."""
    directory = _private_directory(root / "tokens")
    tokens, files = {}, {}
    for role in TOKEN_ROLES:
        token = secrets.token_urlsafe(48)
        path = directory / f"{role}.token"
        _write_private(path, token + "\n")
        tokens[role], files[role] = token, str(path)
    return tokens, files


def role_urls(root):
    from catalyst_lab import localdb

    return {
        role: localdb.connection_url(root, role)
        for role in ("catalyst_app", "catalyst_risk", "catalyst_jev", "catalyst_review")
    }


# --- Test helpers and scripts this session imports deliberately ----------------------------


@functools.cache
def _script(name):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("agent_session_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def fixture_helpers():
    """The fixture venue, observation shape and fixture key; never the launcher's components."""
    repository = str(Path(__file__).resolve().parents[1])
    if repository not in sys.path:
        sys.path.insert(0, repository)
    from tests.test_jev_review import FIXTURE_KEY
    from tests.test_managed_execution import ManagedVenue, observation
    from tests.test_research_cycle import quality_response

    return SimpleNamespace(
        ManagedVenue=ManagedVenue, observation=observation, FIXTURE_KEY=FIXTURE_KEY,
        quality_response=quality_response, replay=_script("replay_selection_policy"),
    )


def session_venue(increment):
    """``ManagedVenue`` on the wall clock; every crypto pair gets SESSION_SIMULATION metadata.

    Levels are research inputs quoted with up to eight decimals, so the simulated price and
    quantity grids are 1e-8 unless ``--sim-price-increment`` says otherwise. The real broker's
    grids are a separate concern of the owner's session.
    """
    base = fixture_helpers().ManagedVenue
    known_gets = re.compile(
        r"/v2/(?:account|positions|orders|account/activities|calendar|assets/.+"
        r"|orders:by_client_order_id|orders/[A-Za-z0-9-]{1,128})"
    )

    class SessionVenue(base):
        price_increment = increment

        @property
        def now(self):
            return _utcnow()

        @now.setter
        def now(self, value):
            # The fixture constructor's own start time is ignored: the session is live.
            return None

        def handle(self, request):
            path, host = request.url.path, request.url.host
            if host == "data.alpaca.markets" and path not in {
                "/v2/stocks/quotes/latest", "/v2/stocks/bars",
            }:
                return httpx.Response(404, json={"message": SIMULATION + "_NO_MARKET_DATA"})
            if request.method == "GET" and host != "data.alpaca.markets":
                if not known_gets.fullmatch(path):
                    return httpx.Response(404, json={"message": SIMULATION + "_UNKNOWN_ROUTE"})
                name = path.removeprefix("/v2/assets/")
                if path.startswith("/v2/assets/") and "/" in name:
                    self.calls.append((request.method, path, request.content))
                    return httpx.Response(200, json={
                        "id": str(uuid5(NAMESPACE_URL, SIMULATION + ":asset:" + name)),
                        "symbol": name, "class": "crypto", "status": "active",
                        "tradable": True, "fractionable": True,
                        "min_order_size": self.price_increment,
                        "min_trade_increment": self.price_increment,
                        "price_increment": self.price_increment,
                        "metadata_source": SIMULATION,
                    })
            response = super().handle(request)
            if request.method in {"POST", "PATCH"} and response.status_code in {200, 201}:
                order = self.orders[response.json()["id"]]
                order["simulation"] = SIMULATION
                for leg in order.get("legs") or ():
                    leg["simulation"] = SIMULATION
                response = httpx.Response(response.status_code, json=order)
            return response

    return SessionVenue()


class SessionExecution(ManagedExecution):
    """ManagedExecution whose CRYPTO_ASSET_METADATA rows name the simulated venue as source."""

    def _record_crypto_metadata(self, conn, symbol, increment, now):
        # The engine's event and key; intake and admission read only price_increment.
        self.store.event(
            conn,
            "CRYPTO_ASSET_METADATA",
            {"symbol": symbol, "price_increment": increment, "source": SIMULATION},
            key=f"crypto-asset-metadata:{symbol}:{now.astimezone(NY).date()}:{increment}",
        )


# --- The Jev call cap --------------------------------------------------------------------


class JevCallCapReached(httpx.TransportError):
    """Raised in place of a provider call once the session cap is spent; nothing is sent."""


def _request_kind(questions, state):
    """``(question set, symbol)`` of one Jev request body."""
    keys = set(questions)
    if keys == set(SKEPTIC.questions):
        return "SKEPTIC", state.get("symbol")
    if keys == set(SKEPTIC_V2.questions):
        return "SKEPTIC_V2", state.get("symbol")
    if keys == set(QUALITY_V2.questions):
        return "QUALITY_V2", (state.get("candidate") or {}).get("symbol")
    if keys == set(QUALITY.questions):
        return "QUALITY", (state.get("candidate") or {}).get("symbol")
    for kind, question_set in PICK_SETS.items():
        if keys == set(question_set.questions):
            return kind, state.get("symbol")
    if keys == set(QUALITY_V3.questions):  # Top-K: the pick's own state, no wrapper.
        return "QUALITY_V3", state.get("symbol")
    if keys == MANAGEMENT_QUESTIONS:
        return "MANAGEMENT", (state.get("position") or {}).get("symbol")
    if keys == MAINTENANCE_QUESTIONS:
        return "MAINTENANCE", state.get("symbol")
    if keys in (DAY_REVIEW_QUESTIONS, DAY_REVIEW_QUESTIONS | {"agent_case"}):
        return "DAY_REVIEW", state.get("symbol")
    if keys == EARLY_EXIT_QUESTIONS:
        return "EARLY_EXIT", state.get("symbol")
    return "UNKNOWN", None


def _request_label(content):
    try:
        body = strict_json(content)
        kind, symbol = _request_kind(body["questions"], body["state"])
        return f"{kind}:{symbol}"
    except (ValueError, TypeError, KeyError, AttributeError):
        return "UNREADABLE_REQUEST"


class CallMeter:
    """Every provider HTTP call (each attempt, retries included) counts against the cap."""

    def __init__(self, cap):
        if type(cap) is not int or not 1 <= cap <= 1000:
            raise SessionRefused("JEV_CALL_CAP_INVALID")
        self.cap, self.used, self.refusals = cap, 0, []
        self._lock = threading.Lock()

    def exhausted(self):
        with self._lock:
            return self.used >= self.cap

    def spend(self):
        with self._lock:
            if self.used >= self.cap:
                return False
            self.used += 1
            return True

    def refuse(self, layer, reference):
        with self._lock:
            self.refusals.append({"layer": layer, "reference": str(reference),
                                  "at": _utcnow().isoformat(), "code": CAP_CODE})

    def snapshot(self):
        with self._lock:
            return {"cap": self.cap, "used": self.used, "remaining": self.cap - self.used,
                    "refused": len(self.refusals), "refusals": list(self.refusals[-20:]),
                    "refusal_code": CAP_CODE}


class _ClosingStream(httpx.AsyncByteStream):
    """A response body that closes its one-call transport once it has been read."""

    def __init__(self, stream, transport):
        self._stream, self._transport = stream, transport

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self):
        try:
            await self._stream.aclose()
        finally:
            await self._transport.aclose()


class CappedTransport(httpx.AsyncBaseTransport):
    """The hard cap: the (cap+1)th call raises JEV_CALL_CAP_REACHED here and is never sent.

    ``inner`` is the fixture's mock transport, or with ``per_request`` a factory of real
    transports: every review opens its own client (and every ``tick`` its own event loop),
    so each real call gets a fresh transport, closed with its response.
    """

    def __init__(self, meter, inner, *, per_request=False):
        self.meter, self._inner, self._per_request = meter, inner, per_request

    async def handle_async_request(self, request):
        if not self.meter.spend():
            self.meter.refuse("TRANSPORT", _request_label(request.content))
            raise JevCallCapReached(CAP_CODE, request=request)
        if not self._per_request:
            return await self._inner.handle_async_request(request)
        transport = self._inner()
        try:
            response = await transport.handle_async_request(request)
        except BaseException:
            await transport.aclose()
            raise
        return httpx.Response(
            response.status_code, headers=response.headers,
            stream=_ClosingStream(response.stream, transport), extensions=response.extensions,
        )

    async def aclose(self):
        return None  # Each real call's transport is closed with its own response.


class CappedJevReviewer(JevReviewer):
    """A spent cap is a clear JEV_CALL_CAP_REACHED result, before any request row is written.

    The transport stays the hard guarantee: a retry, or a review that passes this check while
    another takes the last call, is refused there and recorded as a transport failure.
    """

    def __init__(self, *args, meter, **kwargs):
        super().__init__(*args, **kwargs)
        self.meter = meter

    async def jev_review(self, **kwargs):
        if self.meter.exhausted():
            self.meter.refuse("REVIEWER", kwargs.get("request_id"))
            return ReviewResult(str(kwargs["request_id"]), "NEEDS_REVIEW", CAP_CODE, (), {})
        return await super().jev_review(**kwargs)


# --- Scripted fixture Jev --------------------------------------------------------------------


def _label_valid(question, label):
    criteria = question["criteria"]
    if not isinstance(label, str):
        return False
    if label.startswith("TIE:"):
        parts = label[4:].split("/")
        return len(parts) == 2 and parts[0] != parts[1] and all(p in criteria for p in parts)
    return label in criteria


SKEPTIC_V2_LABELS = ("news_stale", "already_priced", "mechanism_contradicted",
                     "inference_labelled", "factual_claims_supported", "verdict")


class FixtureScript:
    """Scripted mock TypeSafe answers by symbol; it never opens a connection.

    ``{symbol: [verdict, news_stale, unsupported_inference, already_priced]}`` with an optional
    fifth element, the QUALITY_V2 category; or ``{symbol: {"skeptic": [...] or [[...], ...],
    "skeptic_v2": [...] or [[...], ...], "quality": CATEGORY, "management": ACTION}}``, where
    a list of lists answers successive reviews of that symbol (evidence revisions) and the
    last entry repeats. ``skeptic`` answers SKEPTIC_QUESTIONS_V1 (rules V2 and B1);
    ``skeptic_v2`` answers SKEPTIC_QUESTIONS_V2 (rule B2) as six labels: news_stale,
    already_priced, mechanism_contradicted, inference_labelled, factual_claims_supported,
    verdict. ``"*"`` replaces the default for unlisted symbols: APPROVE/NO/NO/LOW,
    NO/LOW/NO/YES/SUPPORTED/NEEDS_REVIEW, ADEQUATE, HOLD. Labels may be ``TIE:A/B``. A
    scripted amendment without an eligible option is answered as HOLD.

    Top-K (``AGENT_RESEARCH_REPORT_V3`` picks): ``"pick"`` maps question names of the pick
    question sets (NEWS, CHART or BOTH_PICK_QUESTIONS_V1) to labels, every other component
    passing and the verdict NEEDS_REVIEW by default; each review uses the names of the set the
    pick's kind is asked with. ``"quality"`` is also the QUALITY_V3 category and
    ``"quality_levels"`` its four 0-2 score levels (default 2, 1 or 0 for STRONG, ADEQUATE or
    WEAK, else 1): a score of 12.5 times their sum.

    Maintenance (``CRYPTO_MAINTENANCE_V1``, JEV_MANAGED_POSITION_QUESTIONS_V4): ``"maintenance"``
    is the action (HOLD, RAISE_STOP, RAISE_TARGET, RAISE_STOP_AND_TARGET or FLAG_EARLY_EXIT;
    default HOLD); a raise takes the first code option (a raise without one is answered as
    HOLD), and FLAG_EARLY_EXIT answers the trade reason BROKEN.

    The 24-hour review (package day-review, JEV_DAY_REVIEW_QUESTIONS_V1): ``"day_review"`` maps
    ``trade_reason``, ``agent_case``, ``decision``, ``stop_option`` and ``target_option`` to
    labels for the first round (default INTACT, HOLDS, CONTINUE, KEEP, KEEP; an option label is
    KEEP, ``first`` or ``last`` of the code options, KEEP when there is none), and
    ``"day_review_final"`` the same for the final round (default: ``day_review``). An agent's
    early-exit flag (JEV_EARLY_EXIT_QUESTIONS_V1): ``"early_exit"`` maps ``trade_reason``,
    ``agent_case`` and ``exit_now`` (default WEAKENED, HOLDS, EXIT).
    """

    DEFAULT = {"skeptic": (("APPROVE", "NO", "NO", "LOW"),),
               "skeptic_v2": (("NO", "LOW", "NO", "YES", "SUPPORTED", "NEEDS_REVIEW"),),
               "quality": "ADEQUATE", "management": "HOLD", "pick": {},
               "quality_levels": None, "maintenance": "HOLD",
               "day_review": DAY_REVIEW_DEFAULT, "day_review_final": None,
               "early_exit": EARLY_EXIT_DEFAULT}

    def __init__(self, raw=None, *, source=None):
        self.source = source
        self.entries = {}
        self.calls = []
        self._served = Counter()
        self._lock = threading.Lock()
        if raw is None:
            raw = {}
        if not isinstance(raw, dict) or len(raw) > 500:
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        for symbol, value in raw.items():
            if not isinstance(symbol, str) or not 1 <= len(symbol) <= 32:
                raise SessionRefused("FIXTURE_SCRIPT_INVALID")
            self.entries[symbol] = self._entry(value)

    @classmethod
    def load(cls, path):
        try:
            raw = strict_json(Path(path).read_bytes())
        except (OSError, ValueError, TypeError, UnicodeError):
            raise SessionRefused("FIXTURE_SCRIPT_UNREADABLE") from None
        return cls(raw, source=str(Path(path).resolve()))

    def _entry(self, value):
        entry = dict(self.DEFAULT)
        if isinstance(value, list):
            if len(value) not in {4, 5}:
                raise SessionRefused("FIXTURE_SCRIPT_INVALID")
            entry["skeptic"] = (tuple(value[:4]),)
            if len(value) == 5:
                entry["quality"] = value[4]
        elif isinstance(value, dict) and value and set(value) <= set(self.DEFAULT):
            for key in ("skeptic", "skeptic_v2"):
                rows = value.get(key, self.DEFAULT[key])
                if isinstance(rows, list | tuple) and rows and all(
                    isinstance(row, list | tuple) for row in rows
                ):
                    entry[key] = tuple(tuple(row) for row in rows)
                elif isinstance(rows, list):
                    entry[key] = (tuple(rows),)
                else:
                    raise SessionRefused("FIXTURE_SCRIPT_INVALID")
            entry["quality"] = value.get("quality", entry["quality"])
            entry["management"] = value.get("management", entry["management"])
            entry["pick"] = value.get("pick", entry["pick"])
            entry["quality_levels"] = value.get("quality_levels", entry["quality_levels"])
            entry["maintenance"] = value.get("maintenance", entry["maintenance"])
            for key, default in (("day_review", DAY_REVIEW_DEFAULT),
                                 ("early_exit", EARLY_EXIT_DEFAULT)):
                labels = value.get(key, {})
                if not isinstance(labels, dict) or set(labels) - set(default):
                    raise SessionRefused("FIXTURE_SCRIPT_INVALID")
                entry[key] = {**default, **labels}
            final = value.get("day_review_final")
            if final is not None:
                if not isinstance(final, dict) or set(final) - set(DAY_REVIEW_DEFAULT):
                    raise SessionRefused("FIXTURE_SCRIPT_INVALID")
                entry["day_review_final"] = {**entry["day_review"], **final}
        else:
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        questions = BOTH_PICK_QUESTIONS.questions  # Every pick question, NEWS's and CHART's.
        if not isinstance(entry["pick"], dict) or not all(
            name in questions and _label_valid(questions[name], label)
            for name, label in entry["pick"].items()
        ):
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        levels = entry["quality_levels"]
        if levels is not None and (
            not isinstance(levels, list) or len(levels) != 4
            or any(type(level) is not int or not 0 <= level <= 2 for level in levels)
        ):
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        for key, question_set, names in (
            ("skeptic", SKEPTIC,
             ("verdict", "news_stale", "unsupported_inference", "already_priced")),
            ("skeptic_v2", SKEPTIC_V2, SKEPTIC_V2_LABELS),
        ):
            if not 1 <= len(entry[key]) <= 20 or any(
                len(row) != len(names)
                or not all(_label_valid(question_set.questions[name], label)
                           for name, label in zip(names, row, strict=True))
                for row in entry[key]
            ):
                raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        if not _label_valid(QUALITY_V2.questions["quality_category"], entry["quality"]):
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        if entry["management"] not in MANAGEMENT_ACTIONS:
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        if entry["maintenance"] not in MAINTENANCE_ACTIONS:
            raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        from catalyst_lab.day_review_dossier import EARLY_EXIT_QUESTIONS as EXIT_SET

        exit_questions = EXIT_SET.questions
        decision = {"criteria": {"CONTINUE": "", "EXIT": "", INSUFFICIENT: ""}}
        for labels in (entry["day_review"], entry["day_review_final"] or {}, entry["early_exit"]):
            for name, label in labels.items():
                question = decision if name == "decision" else exit_questions.get(name)
                if question is not None:
                    valid = _label_valid(question, label)
                else:  # stop_option / target_option
                    valid = label in {"KEEP", "first", "last"}
                if not valid:
                    raise SessionRefused("FIXTURE_SCRIPT_INVALID")
        return entry

    def spec(self, symbol):
        return self.entries.get(symbol) or self.entries.get("*") or self.DEFAULT

    def describe(self):
        return {"source": self.source, "symbols": sorted(self.entries),
                "default": self.spec("*") if "*" in self.entries else self.DEFAULT}

    def __call__(self, request):
        helpers = fixture_helpers()
        body = strict_json(request.content)
        questions, state = body["questions"], body["state"]
        kind, symbol = _request_kind(questions, state)
        spec = self.spec(symbol)
        with self._lock:
            self.calls.append(f"{kind}:{symbol}")
            served = self._served[(kind, symbol)]
            self._served[(kind, symbol)] += 1
        if kind == "SKEPTIC":
            labels = spec["skeptic"][min(served, len(spec["skeptic"]) - 1)]
            reply = helpers.replay.skeptic_reply(*labels)
        elif kind == "SKEPTIC_V2":
            labels = spec["skeptic_v2"][min(served, len(spec["skeptic_v2"]) - 1)]
            reply = helpers.replay.skeptic_v2_reply(*labels)
        elif kind == "QUALITY_V2":
            reply = helpers.replay.quality_v2_reply(spec["quality"])
        elif kind == "QUALITY":
            reply = helpers.quality_response(QUALITY_LEVELS.get(spec["quality"], 1))
        elif kind in PICK_SETS:
            chosen = {**PICK_PASSING, **spec["pick"]}
            reply = {"model": JEV_MODEL,
                     "answers": {name: helpers.replay.choice_answer(question, chosen[name])
                                 for name, question in questions.items()},
                     "usage": {"input_tokens": 10, "output_tokens": 5}}
        elif kind == "QUALITY_V3":
            reply = self._quality_v3(spec, helpers)
        elif kind == "MANAGEMENT":
            reply = self._management(questions, spec["management"], helpers)
        elif kind == "MAINTENANCE":
            reply = self._maintenance(questions, spec["maintenance"], helpers)
        elif kind == "DAY_REVIEW":
            final = (state.get("review") or {}).get("round") == "FINAL"
            labels = (spec["day_review_final"] or spec["day_review"]) if final else (
                spec["day_review"])
            reply = self._labelled(questions, labels, helpers)
        elif kind == "EARLY_EXIT":
            reply = self._labelled(questions, spec["early_exit"], helpers)
        else:
            return httpx.Response(400, json={"error": "FIXTURE_QUESTION_SET_UNKNOWN"})
        return httpx.Response(200, json=reply)

    @staticmethod
    def _quality_v3(spec, helpers):
        level = QUALITY_LEVELS.get(spec["quality"], 1)
        levels = spec["quality_levels"] or [level] * 4
        answers = {}
        for name, value in zip(("evidence_support", "timing_specificity", "level_rationale",
                                "disproof_quality"), levels, strict=True):
            question = QUALITY_V3.questions[name]
            answers[name] = {
                "type": "score", "score": value,
                "legend": {str(i): text for i, text in enumerate(question["criteria"])},
                "confidence": 1.0,
                "probabilities": {str(i): float(i == value) for i in range(3)},
            }
        answers["quality_category"] = helpers.replay.choice_answer(
            QUALITY_V3.questions["quality_category"], spec["quality"])
        return {"model": JEV_MODEL, "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 5}}

    @staticmethod
    def _management(questions, action, helpers):
        options = {
            kind: [k for k in questions[kind + "_option"]["criteria"]
                   if k not in {"KEEP", INSUFFICIENT}]
            for kind in ("stop", "target")
        }
        wants = {"stop": action in {"TIGHTEN_STOP", "TIGHTEN_AND_EXTEND"},
                 "target": action in {"EXTEND_TARGET", "TIGHTEN_AND_EXTEND"}}
        if any(wants[kind] and not options[kind] for kind in wants):
            action, wants = "HOLD", {"stop": False, "target": False}
        labels = {
            "thesis_status": "INTACT", "action": action,
            "stop_option": options["stop"][0] if wants["stop"] else "KEEP",
            "target_option": options["target"][0] if wants["target"] else "KEEP",
        }
        return {
            "model": JEV_MODEL,
            "answers": {name: helpers.replay.choice_answer(question, labels[name])
                        for name, question in questions.items()},
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


    @staticmethod
    def _labelled(questions, labels, helpers):
        """Answers by label; ``first``/``last`` pick a code option (KEEP when there is none)."""
        answers = {}
        for name, question in questions.items():
            label = labels[name]
            if label in {"first", "last"}:
                codes = [k for k in question["criteria"] if k not in {"KEEP", INSUFFICIENT}]
                label = (codes[0] if label == "first" else codes[-1]) if codes else "KEEP"
            answers[name] = helpers.replay.choice_answer(question, label)
        return {"model": JEV_MODEL, "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 5}}

    @staticmethod
    def _maintenance(questions, action, helpers):
        options = {
            kind: [k for k in questions[kind + "_option"]["criteria"]
                   if k not in {"KEEP", INSUFFICIENT}]
            for kind in ("stop", "target")
        }
        wants = {"stop": action in {"RAISE_STOP", "RAISE_STOP_AND_TARGET"},
                 "target": action in {"RAISE_TARGET", "RAISE_STOP_AND_TARGET"}}
        if any(wants[kind] and not options[kind] for kind in wants):
            action, wants = "HOLD", {"stop": False, "target": False}
        labels = {
            "trade_reason": "BROKEN" if action == "FLAG_EARLY_EXIT" else "INTACT",
            "action": action,
            "stop_option": options["stop"][0] if wants["stop"] else "KEEP",
            "target_option": options["target"][0] if wants["target"] else "KEEP",
        }
        return {
            "model": JEV_MODEL,
            "answers": {name: helpers.replay.choice_answer(question, labels[name])
                        for name, question in questions.items()},
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


# --- Session configuration -----------------------------------------------------------------


@dataclass(frozen=True)
class SessionConfig:
    jev: str
    max_jev_calls: int = DEFAULT_MAX_JEV_CALLS
    selection_rule: str = "V2"
    quality_floor: str | None = None
    topk_k: int | None = None  # TOPK only: K, 5-10 (default 10).
    management_reviews: str = "ENABLED"
    report_max_seconds: int = DEFAULT_REPORT_MAX_SECONDS
    sim_price_increment: str = SIMULATED_INCREMENT
    extra_crypto_pairs: tuple = ()

    def __post_init__(self):
        if self.jev not in {"fixture", "typesafe"}:
            raise SessionRefused("JEV_PROVIDER_INVALID")
        extra = self.extra_crypto_pairs
        if (
            not isinstance(extra, tuple)
            or len(extra) > MAX_EXTRA_PAIRS
            or len(set(extra)) != len(extra)
            or any(
                not isinstance(pair, str)
                or not EXTRA_PAIR.fullmatch(pair)
                or pair.split("/")[0] in STABLECOINS
                or pair in CRYPTO_PAIRS
                for pair in extra
            )
        ):
            raise SessionRefused("EXTRA_CRYPTO_PAIRS_INVALID")
        if type(self.max_jev_calls) is not int or not 1 <= self.max_jev_calls <= 1000:
            raise SessionRefused("JEV_CALL_CAP_INVALID")
        if self.management_reviews not in {"ENABLED", "DISABLED"}:
            raise SessionRefused("MANAGEMENT_REVIEWS_SETTING_INVALID")
        if type(self.report_max_seconds) is not int or not 1 <= self.report_max_seconds <= 86400:
            raise SessionRefused("REPORT_MAX_SECONDS_INVALID")
        try:
            increment = D(self.sim_price_increment)
            valid = increment.is_finite() and 0 < increment <= 1
        except (ArithmeticError, TypeError, ValueError):
            valid = False
        if not valid:
            raise SessionRefused("SIMULATED_PRICE_INCREMENT_INVALID")
        try:
            _selection_rule(self.selection_rule, self.quality_floor, self.topk_k)
        except ValueError as exc:
            raise SessionRefused(_code(exc)) from None

    @property
    def rule(self):
        return _selection_rule(self.selection_rule, self.quality_floor, self.topk_k)

    @property
    def universe_pairs(self):
        """The owner-bucket pairs plus any extra pairs: the session's classified universe."""
        return tuple(sorted(set(CRYPTO_PAIRS) | set(self.extra_crypto_pairs)))


def _selection_rule(name, floor, k=None):
    """``V2`` (default, no floor), ``B1`` or ``B2`` with the owner's QUALITY floor, or ``TOPK``
    (``JEV_TOP_K_SELECTION_V1``) or ``TOPK2`` (``JEV_TOP_K_SELECTION_V2``) with K and no floor."""
    topk_policies = {"TOPK": topk_rule.TOPK_POLICY, "TOPK2": topk_rule.TOPK_POLICY_V2}
    if name in topk_policies:
        if floor is not None:
            raise ValueError(topk_rule.FLOOR_NOT_APPLICABLE)
        return topk_rule.TopKRule(topk_policies[name], topk_rule.DEFAULT_K if k is None else k)
    policies = {"V2": V2_POLICY, "B1": B1_POLICY, "B2": B2_POLICY}
    if name not in policies:
        raise ValueError("UNKNOWN_SELECTION_RULE")
    if k is not None:
        raise ValueError("SELECTION_TOPK_NOT_APPLICABLE")
    return SelectionRule(policies[name], floor)


# --- Simulated market rows ------------------------------------------------------------------


def _grid(value, increment, rounding):
    return (value / increment).to_integral_value(rounding=rounding) * increment


def _below(price, floor):
    """A synthetic bid under ``price``: 5 bps, or half the way to ``floor`` if that is closer."""
    return price - min((price - floor) / 2, price * HALF_SPREAD)


def _above(price, ceiling):
    return price + min((ceiling - price) / 2, price * HALF_SPREAD)


def simulated_bars(*, now, key, entry, stop, target, increment, count=30):
    """Completed one-minute SESSION_SIMULATION bars around the entry, oldest first.

    The newest bar ends on the current minute. Lows stay between the stop and the entry
    (stop options) and two highs sit above the target (target options). Invented data,
    labelled as such in every row's provider and feed.
    """
    from catalyst_lab.managed_review import CompletedBar

    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    up, down = target - entry, entry - stop
    bars = []
    for index in range(count):
        ends = end - timedelta(minutes=count - 1 - index)
        base = entry + up * D("0.05") * (index % 4)
        high = target + up * D("0.1") if index in {count // 3, 2 * count // 3} else (
            base + up * D("0.1")
        )
        low = _grid(base - down * D("0.4"), increment, ROUND_FLOOR)
        high = _grid(high, increment, ROUND_CEILING)
        first = min(max(_grid(base, increment, ROUND_HALF_EVEN), low), high)
        last = min(max(_grid(base + up * D("0.02"), increment, ROUND_HALF_EVEN), low), high)
        bars.append(CompletedBar(
            str(uuid5(NAMESPACE_URL, f"{SIMULATION}:{key}:{ends.isoformat()}")),
            ends - timedelta(minutes=1), ends, first, high, low, last, D(1000 + 10 * index),
            SIMULATION, SIMULATION,
        ))
    return tuple(bars)


class SessionBars:
    """SESSION_SIMULATION 15-minute, 1-hour and (CRYPTO_MAINTENANCE_V2) 1-minute bars for
    maintenance reviews.

    Built around each simulated trade's own levels (``levels[symbol] = (entry, stop, target,
    increment)``): 15-minute swing lows between the stop and the entry, and 1-hour swing highs
    above the target, so the code has stop and target options to offer. Bitcoin (and any
    symbol without levels) is flat. No REST quote exists (the review reads the simulated
    stream row). Invented data, labelled in every bar's provider, feed and source ID.
    """

    def __init__(self, clock=None):
        self.levels = {}
        self.calls = []
        self.clock = clock or _utcnow  # The 24-hour review's own clock (``review``).

    def timeframe_bars(self, market, symbol, *, timeframe, count):
        from catalyst_lab.setup_scan import CompletedBar

        self.calls.append((market, symbol, timeframe, count))
        seconds = crypto_maintenance.TIMEFRAMES[timeframe]
        now = self.clock()
        last = crypto_maintenance.floor_time(now, seconds)
        entry, stop, target, increment = self.levels.get(
            symbol, (D("60000"), D("59000"), D("61000"), D("0.01")))
        risk = entry - stop
        bars = []
        for index in range(count):
            back = count - 1 - index
            end = last - timedelta(seconds=seconds * back)
            base = entry + risk * D("0.6")
            low, high = base - risk * D("0.05"), base + risk * D("0.05")
            if timeframe == "15Min" and back in {3, 9, 15}:
                low = entry - risk * D("0.1") * (back // 3 + 1)  # Swing lows above the stop.
            if timeframe == "1Hour" and back in {30, 60, 90}:
                high = target + risk * D("0.2") * (back // 30)  # Swing highs above the target.
            low = _grid(low, increment, ROUND_FLOOR)
            high = _grid(high, increment, ROUND_CEILING)
            middle = min(max(_grid(base, increment, ROUND_HALF_EVEN), low), high)
            bars.append(CompletedBar(
                end - timedelta(seconds=seconds), end, middle, high, low, middle, D(1000),
                SIMULATION, SIMULATION, f"{SIMULATION}:{symbol}:{timeframe}:{end.isoformat()}",
                True,
            ))
        return tuple(bars), ()

    def _latest(self, market, symbols, kind):
        from catalyst_lab.scan_sources import SourceIssue

        return {}, [SourceIssue(market, s, SIMULATION + "_NO_REST_QUOTE", kind)
                    for s in symbols]


def _risk_view(decision):
    if not decision:
        return None
    payload, context = decision.get("payload") or {}, decision.get("context") or {}
    created, expires = decision.get("created_at"), decision.get("expires_at")
    return {
        "decision_id": str(decision["decision_id"]), "action": decision["action"],
        "outcome": decision["outcome"], "reason": decision["reason"],
        "qty": payload.get("qty"), "limit_price": payload.get("limit_price"),
        "equity": decision.get("equity"), "budget": context.get("budget"),
        "binding_constraint": context.get("binding_constraint"),
        "risk_policy_id": context.get("risk_policy_id"),
        "capacity_deferred_until": context.get("capacity_deferred_until"),
        "ttl_seconds": (expires - created).total_seconds() if created and expires else None,
    }


# --- The session ------------------------------------------------------------------------------


class AgentResearchSession:
    """One supervised session: the real intake app and research cycle, a capped Jev and the
    SESSION_SIMULATION venue. Every command runs under one lock."""

    def __init__(self, *, urls, config, tokens, fixture=None, root=None):
        from catalyst_lab.alpaca import AlpacaCredentials
        from catalyst_lab.authorization import RiskRepository
        from catalyst_lab.jev_store import JevStore
        from catalyst_lab.managed_broker import ManagedPaperBroker
        from catalyst_lab.managed_classification import (
            BUCKET_CLASSIFICATION_POLICY,
            initialize_managed_classifications,
        )
        from catalyst_lab.managed_execution import engineering_execution_policy
        from catalyst_lab.managed_runtime import engineering_monitor_policy
        from catalyst_lab.managed_service import create_managed_app
        from catalyst_lab.managed_store import ManagedAuthorizationGate
        from catalyst_lab.position_monitor import PositionMonitor
        from catalyst_lab.position_news import PositionNewsService
        from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle

        self.helpers = fixture_helpers()
        self.config, self.urls, self.root = config, dict(urls), root
        self.tokens = dict(tokens)
        self.session_id = str(uuid4())
        self.lock = threading.RLock()
        self.last_tick_at = None
        self.venue = session_venue(config.sim_price_increment)
        repository = RiskRepository(self.urls["catalyst_risk"])
        repository.check_role()
        self.reviews = JevStore(self.urls["catalyst_jev"])
        # Fixture paper-key shape, never a credential: every request stays in the mock venue.
        self.broker = ManagedPaperBroker(
            AlpacaCredentials("PKFIXTURE000000000001", "fixture-session-no-provider-secret"),
            ManagedAuthorizationGate(repository, clock=self.now),
            transport=httpx.MockTransport(self.venue.handle),
        )
        try:
            self.engine = SessionExecution(
                repository, self.broker, policy=engineering_execution_policy(), clock=self.now,
                review_store=self.reviews, risk_policy_id=RISK_POLICY_ID,
            )
            self.repo = self.engine.repo
            if not self.engine.reconcile()["clean"]:
                raise SessionRefused("SESSION_RECONCILIATION_NOT_CLEAN")
            self.buckets = {
                "buckets": {k: list(v) for k, v in CRYPTO_BUCKETS.items()},
                "unlisted": DEFAULT_CRYPTO_BUCKET if config.extra_crypto_pairs else "REFUSE",
            }
            self.classification = initialize_managed_classifications(
                repository, self.broker, policy_id=BUCKET_CLASSIFICATION_POLICY, us_mapping=(),
                crypto_symbols=list(config.universe_pairs), crypto_buckets=self.buckets,
            )
            self.meter = CallMeter(config.max_jev_calls)
            if config.jev == "fixture":
                self.fixture = fixture if fixture is not None else FixtureScript()
                transport = CappedTransport(self.meter, httpx.MockTransport(self.fixture))
                key_provider = self._fixture_key
            else:
                from catalyst_lab.jev_secrets import typesafe_key

                self.fixture = None
                # The reviewer's own default transport, one per call: no environment proxies.
                transport = CappedTransport(
                    self.meter, functools.partial(httpx.AsyncHTTPTransport, trust_env=False),
                    per_request=True,
                )
                key_provider = typesafe_key
            self.reliability = ReliabilityPolicy(
                f"AGENT_RESEARCH_SESSION_{config.jev.upper()}_V1", *RELIABILITY
            )
            self.reviewer = CappedJevReviewer(
                self.reviews, self.reliability, meter=self.meter, transport=transport,
                key_provider=key_provider, clock=self.now,
            )
            self.cycle_policy = CyclePolicy(
                *CYCLE_POLICY, review_validity_seconds=REVIEW_VALIDITY_SECONDS
            )
            self.cycle = ResearchCycle(
                repository, self.reviewer, self.cycle_policy, clock=self.now,
                selection=config.rule,
            )
            # B1, B2 and top-K need their audited activation before a report is accepted; V2
            # records none.
            self.activation = self.cycle.record_selection_rule(runtime_id=self.session_id)
            self.v3 = self._v3_intake()
            self.monitor_policy = engineering_monitor_policy()
            self.monitor = PositionMonitor(
                self.engine, self.reviewer, policy=self.monitor_policy, review_seconds=10,
                clock=self.now, management_reviews=config.management_reviews,
            )
            # CRYPTO_MAINTENANCE_V1/V2 (packages maintenance and answer-rules): maintained
            # report-V3 crypto trades are reviewed by the monitoring Jev over SESSION_SIMULATION
            # bars and quotes, on the session's maintenance clock (the wall clock until
            # ``execute --maintenance-minutes`` moves it a minute at a time), with the same
            # capped Jev transport and the cycle's in-flight limit.
            from catalyst_lab.system_check import LivePriceReader
            from catalyst_lab.trade_maintenance import TradeMaintenance

            self.maintenance_offset = timedelta(0)
            self.maintenance_bars = SessionBars(clock=self.maintenance_now)
            self.maintenance_reviewer = CappedJevReviewer(
                self.reviews, self.reliability, meter=self.meter, transport=transport,
                key_provider=key_provider, clock=self.maintenance_now,
            )
            self.spend_guard = jev_budget.SpendGuard(SESSION_SPEND, self.engine.store,
                                                     clock=self.maintenance_now)
            self.maintenance = TradeMaintenance(
                self.engine, self.maintenance_reviewer, bars=self.maintenance_bars,
                clock=self.maintenance_now, management_reviews=config.management_reviews,
                live_prices=LivePriceReader(self.maintenance_bars, clock=self.maintenance_now),
                max_inflight=self.cycle_policy.max_inflight, spend_guard=self.spend_guard,
            )
            # CRYPTO_24H_REVIEW_V1 (package day-review): the 24-hour reviews and early exits on
            # the session's own review clock (``review`` moves it; the venue stays on the wall
            # clock), with the same capped Jev transport and the agent's routes on the app.
            from catalyst_lab.trade_review import DayReviews, TradeReviewService

            self.review_pin = None  # Where the last ``review --at``/``--minutes`` put the clock.
            self.sim_quotes = {}
            self.day_bars = SessionBars(clock=self.review_now)
            self.day_reviewer = CappedJevReviewer(
                self.reviews, self.reliability, meter=self.meter, transport=transport,
                key_provider=key_provider, clock=self.review_now,
            )
            agents = frozenset({AGENT_ID, "muse"})
            self.day_reviews = DayReviews(
                self.engine, self.day_reviewer, bars=self.day_bars, clock=self.review_now,
                management_reviews=config.management_reviews, agents=agents,
                live_prices=LivePriceReader(self.day_bars, clock=self.review_now),
            )
            self.trade_reviews = TradeReviewService(self.engine.store, clock=self.review_now,
                                                    agents=agents)
            self.configuration_hash = digest(encoded(json_safe(self._configuration())))
            self.engine._event(
                "AGENT_RESEARCH_SESSION_STARTED",
                {"session_id": self.session_id, "venue": SIMULATION,
                 "configuration": self._configuration(),
                 "configuration_hash": self.configuration_hash},
                key=f"agent-research-session:{self.session_id}:started",
            )
            self.app = create_managed_app(
                self.cycle, self.engine.store, api_token=self.tokens["legacy_muse"],
                status_token=self.tokens["status"], operator_token=self.tokens["operator"],
                agent_tokens={AGENT_ID: self.tokens[AGENT_ID]},
                runtime_status=self.runtime_status, report_submit=self._report_submit,
                position_news=PositionNewsService(self.engine.store, clock=self.now),
                trade_reviews=self.trade_reviews,
            )
        except BaseException:
            self.broker.close()
            raise

    @staticmethod
    def now():
        return _utcnow()

    def review_now(self):
        """The 24-hour review's clock (package answer-rules): the wall clock until a ``review
        --at``/``--minutes`` step moves it ahead, then exactly where the last step put it until
        the next one, never behind the wall clock. It no longer follows the wall clock between
        steps, so an agent's answer window cannot close while the agent or the operator is
        answering (round 5 of 2026-09-27 lost two answer windows that way)."""
        wall = _utcnow()
        return wall if self.review_pin is None else max(self.review_pin, wall)

    def maintenance_now(self):
        """Maintenance's clock: the wall clock plus the minutes ``execute
        --maintenance-minutes`` has moved it (package answer-rules)."""
        return _utcnow() + self.maintenance_offset

    def _fixture_key(self):
        return self.helpers.FIXTURE_KEY

    def _report_submit(self, raw):
        return asyncio.to_thread(
            self.cycle.start_report, raw, max_seconds=self.config.report_max_seconds,
            v3=self.v3,
        )

    def _v3_intake(self):
        """Report V3 over the session schedule and its simulated universe: the owner-bucket
        pairs plus any extra pairs, all of which the session classifies (V2 and legacy bodies
        never read it)."""
        from catalyst_lab.research_report_v3 import UniverseSnapshot, V3Intake

        pairs = frozenset(self.config.universe_pairs)

        def universe():
            return UniverseSnapshot(pairs, self.now(), SIMULATION + "_UNIVERSE")

        return V3Intake(SESSION_SCHEDULE, universe)

    def close(self):
        self.broker.close()

    def _configuration(self):
        return {
            "format": SESSION_FORMAT, "agent_id": AGENT_ID,
            "jev_provider": self.config.jev, "jev_model": JEV_MODEL,
            "max_jev_calls": self.config.max_jev_calls,
            "reliability_policy": asdict(self.reliability),
            "selection_rule": self.config.rule.policy,
            "quality_floor": self.config.rule.quality_floor,
            "topk_k": self.config.rule.k if topk_rule.is_topk(self.config.rule) else None,
            "research_schedule": SESSION_SCHEDULE.as_dict(),
            "report_v3_universe": {"source": SIMULATION + "_UNIVERSE",
                                   "symbols": list(self.config.universe_pairs)},
            "extra_crypto_pairs": list(self.config.extra_crypto_pairs),
            "management_reviews": self.config.management_reviews,
            "cycle_policy": asdict(self.cycle_policy),
            "report_max_seconds": self.config.report_max_seconds,
            "risk_policy_id": RISK_POLICY_ID,
            "monitor_policy": asdict(self.monitor_policy),
            # The versions admission records (package answer-rules: V2 of both; package
            # jev-budget: maintenance V3) and the session's spend guard.
            "maintenance_policy": crypto_maintenance.ADMITTED_MAINTENANCE.record(),
            "jev_spend_guard": jev_budget.guard_summary(SESSION_SPEND),
            "day_review_policy": crypto_holding.ADMITTED_REVIEW.record(),
            "early_exit_policy": day_review.EARLY_EXIT_AGREEMENT.record(),
            "classification_policy": self.classification["policy_id"],
            "crypto_buckets": self.buckets,
            "venue": {"label": SIMULATION,
                      "crypto_price_increment": self.config.sim_price_increment,
                      "crypto_min_trade_increment": self.config.sim_price_increment,
                      "crypto_min_order_size": self.config.sim_price_increment,
                      "broker_network": "NONE_IN_MEMORY_MOCK_TRANSPORT"},
            "fixture_script": self.fixture.describe() if self.fixture else None,
        }

    def runtime_status(self):
        from catalyst_lab.config import SCHEMA_VERSION

        return {
            "worker_state": "SUPERVISED_SESSION_NO_WORKER_LOOPS",
            "mode": "AGENT_RESEARCH_SESSION_" + SIMULATION, "entry_ready": False,
            "research_healthy": True,
            "management_review_enabled": self.config.management_reviews == "ENABLED",
            "trade_stream_connected": False, "market_feed_connected": False,
            "market_streams": {"US": False, "CRYPTO": False}, "required_market_streams": [],
            "market_data_authority": SIMULATION, "last_cycle_at": self.last_tick_at,
            "last_reconciliation_at": self.engine.reconciled_at,
            "schema_version": SCHEMA_VERSION, "configuration_hash": self.configuration_hash,
            "executor_ownership": SIMULATION, "account_safety_healthy": False,
            "error_code": None,
        }

    # --- ledger reads ------------------------------------------------------------------

    def _rows(self, sql, params=()):
        with self.repo.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _cycle_ids(self, *, open_only):
        clause = "AND (body->>'expires_at')::timestamptz>clock_timestamp()" if open_only else ""
        return [row["cycle_id"] for row in self._rows(
            f"""SELECT body->>'cycle_id' AS cycle_id,min(event_seq) AS seq
            FROM lab.managed_events WHERE kind='RESEARCH_STARTED' {clause}
            GROUP BY 1 ORDER BY 2"""
        )]

    def _cycle_events(self, cycle_id):
        return self._rows(
            """SELECT event_seq,kind,body,recorded_at,idempotency_key FROM lab.managed_events
            WHERE body->>'cycle_id'=%s AND kind LIKE 'RESEARCH_%%' ORDER BY event_seq""",
            (cycle_id,),
        )

    def _shadows(self, cycle_id):
        return {
            (row["body"]["item_key"], row["body"]["revision"]): row["body"]
            for row in self._rows(
                """SELECT body FROM lab.managed_events WHERE kind=%s
                AND body->>'research_cycle_id'=%s ORDER BY event_seq""",
                (SHADOW_KIND, cycle_id),
            )
        }

    def _receipts(self, receipt_ids):
        if not receipt_ids:
            return {}
        with self.reviews.connect() as conn:
            rows = conn.execute(
                """SELECT receipt_id,request_id,attempt,outcome,http_status,latency_ms,
                error_code,actual_model,started_at,completed_at,response_hash
                FROM lab.jev_receipts WHERE receipt_id=ANY(%s::uuid[])""",
                (list(receipt_ids),),
            ).fetchall()
        return {str(row["receipt_id"]): json_safe(row) for row in rows}

    def _latest_event(self, setup_id, kind):
        rows = self._rows(
            """SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind=%s
            ORDER BY event_seq DESC LIMIT 1""",
            (setup_id, kind),
        )
        return rows[0]["body"] if rows else None

    def _state_path(self, setup_id):
        path = []
        for row in self._rows(
            """SELECT body->>'state' AS state FROM lab.managed_events
            WHERE setup_id=%s AND kind='STATE' ORDER BY event_seq""",
            (setup_id,),
        ):
            if not path or path[-1] != row["state"]:
                path.append(row["state"])
        return path

    @staticmethod
    def _parse(events):
        parsed = SimpleNamespace(started={}, packets={}, decisions={}, quality={}, selected={},
                                 tasks=[], resolved={}, claims={}, rejected=[], ranking=None,
                                 skips=[], replacements=[])
        for event in events:
            kind, body = event["kind"], event["body"]
            if kind == "RESEARCH_STARTED":
                parsed.started = body
            elif kind == "RESEARCH_PACKET":
                parsed.packets.setdefault(body["item_key"], []).append(body)
            elif kind == "RESEARCH_DECISION":
                parsed.decisions[(body["item_key"], body["revision"])] = body
            elif kind == "RESEARCH_QUALITY":
                parsed.quality[(body["item_key"], body["revision"])] = body
            elif kind == "RESEARCH_SELECTED":
                packet = body["packet"]
                parsed.selected[(packet["item_key"], packet["revision"])] = {
                    **packet, "selection_event_seq": event["event_seq"],
                }
            elif kind == "RESEARCH_EVIDENCE_TASK":
                parsed.tasks.append(body)
            elif kind == "RESEARCH_EVIDENCE_RESOLVED":
                parsed.resolved[body["task_id"]] = body
            elif kind == "RESEARCH_EVIDENCE_CLAIM":
                parsed.claims.setdefault(body["task_id"], []).append(body)
            elif kind == "RESEARCH_ITEM_REJECTED_AT_INTAKE":
                parsed.rejected.append(body)
            elif kind == topk_rule.RANKING_KIND and event.get("idempotency_key") == (
                topk_rule.ranking_key_for(body.get("cycle_id"))
            ):  # The cycle's one ranking; no other event of that kind is read as it.
                parsed.ranking = {**body, "event_seq": event["event_seq"]}
            elif kind == topk_rule.SKIPPED_KIND:
                parsed.skips.append(body)
            elif kind == topk_rule.REPLACEMENT_KIND:  # TOPK_REPLACEMENT_V1 decisions.
                parsed.replacements.append(body)
        return parsed

    def _v2_view(self, packet, decision, policy):
        """V2 as recorded, or for a B1 cycle recomputed from the retained receipts.

        A B2 cycle's receipts answer SKEPTIC_QUESTIONS_V2, which has no
        ``unsupported_inference``: V2 is not applicable to them and is never invented.
        """
        from catalyst_lab.research_cycle import selection_disposition

        if decision is None:
            return None
        if policy == B2_POLICY:
            return {"disposition": "NOT_APPLICABLE",
                    "reason": "REPLAY_NOT_APPLICABLE_QUESTION_SET",
                    "source": SKEPTIC_V2.version + "_RECEIPTS"}
        if policy in topk_rule.TOPK_POLICIES:
            return {"disposition": "NOT_APPLICABLE",
                    "reason": "REPLAY_NOT_APPLICABLE_QUESTION_SET",
                    "source": (decision.get("question_set_version") or "PICK_QUESTIONS")
                    + "_RECEIPTS"}
        if policy != B1_POLICY or decision["disposition"] in {"SUPERSEDED", "EXPIRED"}:
            return {"disposition": decision["disposition"], "reason": decision["reason"],
                    "source": "RECORDED_DECISION"}
        result = self.cycle._existing_result(packet, decision["request_id"])
        if result is None:
            return {"disposition": "NEEDS_REVIEW", "reason": decision["reason"],
                    "source": "NO_REVIEW_REQUEST"}
        result = self.cycle._verify_result(packet, decision["request_id"], result)
        disposition, reason = selection_disposition(result)
        return {"disposition": disposition, "reason": reason, "source": "RECEIPTS_UNDER_V2"}

    @staticmethod
    def _shadow_view(shadow):
        if shadow is None:
            return None
        return {k: shadow.get(k) for k in (
            "disposition", "reasons", "dissent", "dissent_tied", "policy", "active_policy",
            "receipt_id",
        )}

    @staticmethod
    def _b2_view(decision):
        """A B2 decision as recorded: disposition, reasons, the five component labels and the
        verdict as dissent; B1's shadow is not applicable to its receipts."""
        if decision is None or decision.get("selection_policy") != B2_POLICY:
            return None
        answers = decision.get("answers") or {}
        return {
            "disposition": decision["disposition"], "reason": decision["reason"],
            "reasons": decision.get("reasons"), "dissent": decision.get("dissent"),
            "dissent_tied": decision.get("dissent_tied"),
            "components": {name: (answers.get(name) or {}).get("choice")
                           for name in B2_COMPONENTS},
            "question_set_version": decision.get("question_set_version"),
            "shadow": decision.get("shadow"),
        }

    @staticmethod
    def _topk_view(decision, entry):
        """A top-K pick: its ranking entry once the cycle is ranked, else its review."""
        if entry is not None:
            return {k: entry.get(k) for k in (
                "status", "rank", "adjusted_score", "quality_score", "quality_category",
                "veto_reasons", "uncertain", "dissent", "dissent_tied", "reason", "kind",
                "question_set_version", "receipt_id", "quality_receipt_id")}
        if decision is None or decision.get("selection_policy") not in topk_rule.TOPK_POLICIES:
            return None
        return {"status": "AWAITING_RANKING", "review": decision["disposition"],
                "reason": decision["reason"], "veto_reasons": decision.get("veto_reasons"),
                "uncertain": decision.get("uncertain"), "dissent": decision.get("dissent"),
                "dissent_tied": decision.get("dissent_tied"), "kind": decision.get("pick_kind"),
                "question_set_version": decision.get("question_set_version")}

    @staticmethod
    def _entries(parsed):
        return {e["item_key"]: e for e in (parsed.ranking or {}).get("entries") or ()}

    def _item_row(self, packet, parsed, shadows, receipts, policy, now):
        key = (packet["item_key"], packet["revision"])
        decision = parsed.decisions.get(key)
        receipt_ids = (decision or {}).get("receipt_ids") or []
        final = receipt_ids[-1] if receipt_ids else None
        receipt = receipts.get(final) if final else None
        quality = parsed.quality.get(key)
        # ResearchCycle._review_deadline: the report expiry or receipt + validity, if earlier;
        # a report V3 pick's review lasts until its own expiry (PACKET_EXPIRY).
        valid_until = datetime.fromisoformat(packet["expires_at"])
        if packet.get("review_validity") != "PACKET_EXPIRY":
            valid_until = min(
                valid_until,
                datetime.fromisoformat(packet.get("received_at", packet["created_at"]))
                + timedelta(seconds=REVIEW_VALIDITY_SECONDS),
            )
        selection = parsed.selected.get(key)
        return {
            "item_key": packet["item_key"], "symbol": packet["symbol"],
            "market": packet["market"], "revision": packet["revision"],
            "signal_id": packet.get("signal_id"), "rank": packet.get("rank"),
            "decision": None if decision is None else {
                "disposition": decision["disposition"], "reason": decision["reason"],
                "policy": decision.get("selection_policy", V2_POLICY),
            },
            "v2": self._v2_view(packet, decision, policy),
            "b1_shadow": self._shadow_view(shadows.get(key)),
            "b2": self._b2_view(decision),
            "topk": self._topk_view(decision, self._entries(parsed).get(packet["item_key"])),
            "quality": None if quality is None else {
                k: quality.get(k) for k in ("quality_policy", "status", "score", "category")
            },
            "selected": selection is not None,
            "selection_event_seq": selection["selection_event_seq"] if selection else None,
            "receipt_id": final, "receipt_count": len(receipt_ids),
            "receipt_outcome": receipt["outcome"] if receipt else None,
            "latency_ms": receipt["latency_ms"] if receipt else None,
            "review_valid_until": valid_until,
            "review_seconds_left": max(0.0, (valid_until - now).total_seconds()),
        }

    def _open_tasks(self, cycle_id, parsed, shadows, now):
        tasks = []
        for task in parsed.tasks:
            revisions = parsed.packets.get(task["item_key"]) or [{}]
            if (
                task["task_id"] in parsed.resolved
                or datetime.fromisoformat(task["expires_at"]) <= now
                or revisions[-1].get("revision") != task["revision"]
            ):
                continue
            key = (task["item_key"], task["revision"])
            decision, shadow = parsed.decisions.get(key) or {}, shadows.get(key)
            leases = parsed.claims.get(task["task_id"]) or []
            # A B1 or B2 decision records its own reasons (B1's equal its shadow's); a V2
            # decision has none, so its B1 shadow names the objections, as before.
            if decision.get("reasons"):
                objections = list(decision["reasons"])
            elif shadow and shadow.get("reasons"):
                objections = list(shadow["reasons"])
            else:
                objections = [decision.get("reason")]
            requirements = task.get("requirements") or ()
            tasks.append({
                "cycle_id": cycle_id, "task_id": task["task_id"], "item_key": task["item_key"],
                "symbol": revisions[-1].get("symbol"), "revision": task["revision"],
                "answer_revision": task["revision"] + 1, "expires_at": task["expires_at"],
                "requirements": [r["task"] for r in requirements],
                "required_fields": {r["task"]: r.get("required_fields") for r in requirements},
                "instructions": {r["task"]: r["instructions"] for r in requirements
                                 if r.get("instructions")},
                "objection_codes": objections,
                "decision_reason": decision.get("reason"),
                "claimed_until": leases[-1]["lease_until"] if leases else None,
            })
        return tasks

    def _cycle_view(self, cycle_id, now):
        parsed = self._parse(self._cycle_events(cycle_id))
        shadows = self._shadows(cycle_id)
        policy = parsed.started.get("selection_policy", V2_POLICY)
        receipt_ids = {r for d in parsed.decisions.values() for r in d.get("receipt_ids") or ()}
        receipts = self._receipts(receipt_ids)
        items = [
            self._item_row(revisions[-1], parsed, shadows, receipts, policy, now)
            for revisions in parsed.packets.values()
        ]
        return {
            "cycle_id": cycle_id, "agent": parsed.started.get("agent"),
            "selection_policy": parsed.started.get("selection_policy", V2_POLICY),
            "quality_floor": (parsed.started.get("selection_rule") or {}).get("quality_floor"),
            "topk_k": (parsed.started.get("selection_rule") or {}).get("k"),
            "report_schema_version": parsed.started.get("report_schema_version"),
            "run_slot": parsed.started.get("run_slot"),
            "ranking": None if parsed.ranking is None else {
                k: parsed.ranking.get(k) for k in ("event_seq", "k", "complete", "counts")},
            "selection_skips": [{k: s.get(k) for k in ("item_key", "rank", "reason",
                                                      "selected_in_cycle_id")}
                                for s in parsed.skips],
            "expires_at": parsed.started.get("expires_at"),
            "contender_count": parsed.started.get("contender_count"),
            "intake_rejections": [
                {k: r.get(k) for k in ("index", "signal_id", "code", "errors")}
                for r in parsed.rejected
            ],
            "items": items, "open_tasks": self._open_tasks(cycle_id, parsed, shadows, now),
        }

    def research_view(self, *, open_only, faults=None):
        now = self.now()
        cycles, tasks = [], []
        for cycle_id in self._cycle_ids(open_only=open_only):
            view = self._cycle_view(cycle_id, now)
            view["fault"] = (faults or {}).get(cycle_id)
            tasks.extend(view.pop("open_tasks"))
            cycles.append(view)
        return {"at": now, "session_id": self.session_id, "jev_calls": self.meter.snapshot(),
                "review_validity_seconds": REVIEW_VALIDITY_SECONDS, "cycles": cycles,
                "open_evidence_tasks": tasks}

    # --- commands --------------------------------------------------------------------

    def tick(self):
        """One review tick and publication for every open cycle, then the item table."""
        with self.lock:
            faults = {}
            for cycle_id in self._cycle_ids(open_only=True):
                try:
                    asyncio.run(self.cycle.tick(cycle_id))
                    self.cycle.approved_packets(cycle_id)  # Durable publication only.
                except Exception as exc:
                    faults[cycle_id] = _code(exc)
            self.last_tick_at = self.now()
            return self.research_view(open_only=True, faults=faults)

    def _audit_once(self, kind, body, key):
        try:
            self.engine._event(kind, body, key=key)
        except ValueError as exc:
            if str(exc) != "IDEMPOTENCY_CONTENT_MISMATCH":
                raise

    def _refused(self, packet, code):
        """The runtime's own audit of a refusal; a permanent one declines the selection.

        As in the runtime (package replacement), a top-K pick declined for anything but
        supersession gets its one TOPK_REPLACEMENT_V1 decision in the decline's own transaction;
        a refused decision writes neither, is audited once and leaves the pick offered.
        """
        from catalyst_lab.managed_runtime import (
            ADMISSION_DECLINED_KEY,
            REPLACEMENT_FAULT_EVENT,
            decline_selection,
            permanent_refusal,
            replaces_on_decline,
        )

        seq = packet.get("selection_event_seq")
        self._audit_once(
            "RUNTIME_ADMISSION_REFUSED",
            {"runtime_id": self.session_id, "selection_event_seq": seq, "reason": code},
            f"runtime:{self.session_id}:admission-refused:{seq}:{code}",
        )
        result = {"declined": False, "replacement": None, "replacement_fault": None}
        if not permanent_refusal(packet, code):
            return result
        body = {"cycle_id": packet.get("cycle_id"), "item_key": packet.get("item_key"),
                "revision": packet.get("revision"), "receipt_id": packet.get("receipt_id"),
                "selection_event_seq": seq, "reason": code}
        key = f"{ADMISSION_DECLINED_KEY}{seq}"
        if not replaces_on_decline(packet, code, self.cycle):
            self._audit_once("RESEARCH_ADMISSION_DECLINED", body, key)
            return {**result, "declined": True}
        try:
            _, replacement = decline_selection(self.engine.store, self.cycle, body, key)
        except ValueError as exc:
            fault = _code(exc)
            self._audit_once(
                REPLACEMENT_FAULT_EVENT,
                {"runtime_id": self.session_id, "cycle_id": packet.get("cycle_id"),
                 "item_key": packet.get("item_key"), "selection_event_seq": seq,
                 "declined_code": code, "code": fault},
                f"runtime:{self.session_id}:replacement-fault:{seq}:{fault}",
            )
            return {**result, "replacement_fault": fault}
        return {**result, "declined": True, "replacement": None if replacement is None else {
            k: replacement["body"].get(k) for k in (
                "outcome", "code", "replacement_item_key", "replacement_rank", "passed_over")}}

    def _selected_packets(self):
        """The runtime's admission queue: unexpired, not admitted, not declined."""
        from catalyst_lab.managed_runtime import ADMISSION_DECLINED_KEY

        return [{**row["packet"], "selection_event_seq": row["event_seq"]} for row in self._rows(
            """SELECT e.event_seq,e.body->'packet' AS packet FROM lab.managed_events e
            WHERE e.kind='RESEARCH_SELECTED'
            AND (e.body->'packet'->>'expires_at')::timestamptz>clock_timestamp()
            AND NOT EXISTS(SELECT 1 FROM lab.managed_setups s
                WHERE s.receipt_id::text=e.body->'packet'->>'receipt_id')
            AND NOT EXISTS(SELECT 1 FROM lab.managed_events d
                WHERE d.idempotency_key=%s||e.event_seq::text)
            ORDER BY e.event_seq""",
            (ADMISSION_DECLINED_KEY,),
        )]

    def execute(self, *, simulate_prints, hold_open=False, maintenance_minutes=1):
        if type(maintenance_minutes) is not int or not 1 <= maintenance_minutes <= 10:
            raise SessionRefused("MAINTENANCE_MINUTES_INVALID")
        with self.lock:
            publication = {}
            for cycle_id in self._cycle_ids(open_only=True):
                try:
                    self.cycle.approved_packets(cycle_id)
                except Exception as exc:
                    publication[cycle_id] = _code(exc)
            reconciliation = self.engine.reconcile()
            report = {"at": self.now(), "simulate_prints": simulate_prints,
                      "hold_open": hold_open, "maintenance_minutes": maintenance_minutes,
                      "publication_faults": publication,
                      "reconciliation": {"clean": reconciliation["clean"],
                                         "mismatches": reconciliation["mismatches"]},
                      "admissions": [], "lifecycles": []}
            if not reconciliation["clean"]:
                report["error"] = "RECONCILIATION_NOT_CLEAN"
                return report
            for packet in self._selected_packets():
                entry = {k: packet.get(k) for k in (
                    "cycle_id", "item_key", "revision", "symbol", "market",
                    "selection_event_seq", "receipt_id", "review_valid_until",
                )}
                if packet["market"] not in {"US_STOCKS", "CRYPTO"}:
                    entry["admission"] = {"outcome": "NOT_ADMITTED", "code": "RESEARCH_ONLY_MARKET"}
                else:
                    try:
                        setup_id = self.engine.admit(
                            packet, live_quote=lambda _symbol, pick=packet: self._live_quote(pick)
                        )
                        entry["admission"] = {"outcome": "ADMITTED", "setup_id": str(setup_id)}
                    except Exception as exc:
                        code = _code(exc)
                        entry["admission"] = {"outcome": "REFUSED", "code": code,
                                              **self._refused(packet, code)}
                report["admissions"].append(entry)
            if simulate_prints:
                for setup in self.engine.store.active():
                    if setup["state"]["state"] == "WATCHING":
                        report["lifecycles"].append(
                            self._simulate(setup, hold_open, maintenance_minutes))
            report["jev_calls"] = self.meter.snapshot()
            return report

    # --- the simulated lifecycle ------------------------------------------------------

    def _live_quote(self, packet):
        """The live price ``SYSTEM_CHECK_V1`` needs to admit a report V3 pick (package
        system-check): the session's simulated market for the symbol before any print, quoted
        as a simulated print is quoted (the ask at the price, the bid 5 bps under it, on the
        simulated grid) at the pick's own ``agent_current_price`` and read now, with
        ``SESSION_SIMULATION`` as the quote source the check records. A session has no real
        market; the check applies every threshold unchanged to this quote. Other packets never
        read it."""
        from catalyst_lab.system_check import LiveQuote

        increment = D(self.config.sim_price_increment)
        price = D(str(packet["state"]["agent_current_price"]))
        ask = _grid(price, increment, ROUND_CEILING)
        bid = min(_grid(price * (1 - HALF_SPREAD), increment, ROUND_FLOOR), ask)
        now = self.now()
        return LiveQuote(packet["symbol"], bid, ask, now, SIMULATION, now)

    def _observation(self, *, bid, ask, trade_price=None):
        """The test helper's observation shape, every value labelled SESSION_SIMULATION."""
        return self.helpers.observation(
            (None, self.venue, None),
            trade_price=_text(trade_price if trade_price is not None else bid),
            bid=_text(bid), ask=_text(ask), data_provider=SIMULATION, data_feed=SIMULATION,
            trade_id=f"{SIMULATION}-{uuid4().hex}",
        )

    def _fill(self, order, qty, price):
        update = self.venue.fill(order["id"], _text(qty), price=_text(price))
        update["simulation"] = SIMULATION
        self.engine.ingest(update)
        return {"order_id": order["id"], "side": order["side"], "type": order["type"],
                "qty": _text(qty), "price": _text(price),
                "execution_id": update["execution_id"], "simulation": SIMULATION}

    def _protection(self, setup_id, symbol):
        state = self.engine._load(setup_id)[1]
        inventory = self.venue.inventory.get(symbol, D(0))
        stops = [
            o for o in self.venue.orders.values()
            if o["symbol"] == symbol and o["side"] == "sell"
            and o["type"] in {"stop_limit", "stop"} and o["status"] not in TERMINAL_ORDERS
        ]
        pending = [o for o in stops if o["status"] in {"pending_cancel", "pending_replace"}]
        protected = (
            state.get("state") == "OPEN" and inventory > 0 and len(stops) == 1 and not pending
            and D(stops[0]["qty"]) - D(stops[0]["filled_qty"]) == inventory
            and D(stops[0]["stop_price"]) == D(state["stop"])
        )
        return {"state": state.get("state"), "protected": protected,
                "stop": stops[0].get("stop_price") if len(stops) == 1 else None,
                "stop_limit": stops[0].get("limit_price") if len(stops) == 1 else None,
                "target": state.get("target"), "position_qty": _text(inventory)}

    def _protect(self, setup_id, symbol, fill_price, attempts=6):
        for _ in range(attempts):
            view = self._protection(setup_id, symbol)
            if view["protected"] or view["state"] in TERMINAL_SETUPS:
                return view
            stop = D(self.engine._load(setup_id)[1]["stop"])
            self.engine.manage(setup_id, self._observation(
                bid=_below(fill_price, stop), ask=fill_price,
            ))
        return self._protection(setup_id, symbol)

    def _review(self, setup_id, fill_price):
        """One management review with SESSION_SIMULATION bars and quotes, through the
        unchanged position monitor; counted against the Jev cap."""
        state = self.engine._load(setup_id)[1]
        target = D(state["target"])
        bars = simulated_bars(
            now=self.now(), key=str(setup_id), entry=fill_price, stop=D(state["stop"]),
            target=target, increment=D(self.config.sim_price_increment),
        )
        ask = _above(fill_price, target)

        def fresh():
            return self._observation(bid=fill_price, ask=ask)

        used = self.meter.snapshot()["used"]
        view = {"bars": len(bars), "bar_provider": SIMULATION}
        try:
            result = asyncio.run(self.monitor.review(
                setup_id, fresh(), bars, fresh_observation=fresh, structural_bars=bars,
            ))
        except Exception as exc:
            view.update(outcome="REVIEW_FAILED", code=_code(exc))
            result = None
        view["jev_calls"] = self.meter.snapshot()["used"] - used
        if result is None and "outcome" not in view:
            skipped = self._latest_event(setup_id, "POSITION_REVIEW_SKIPPED")
            view.update(outcome="SKIPPED" if skipped else "NOT_TRIGGERED",
                        reason=skipped.get("reason") if skipped else None)
        elif result is not None:
            judgment = self._latest_event(setup_id, "MANAGED_JEV_JUDGMENT") or {}
            plan = self._latest_event(setup_id, "MANAGEMENT_PLAN_AUTHORIZED")
            view.update(outcome="JUDGMENT", status=result.status, reason=result.reason,
                        receipt_ids=list(result.receipt_ids), action=judgment.get("action"),
                        judgment_reason=judgment.get("reason"),
                        plan={"stop": plan.get("stop"), "target": plan.get("target")}
                        if plan else None)
        return view

    def _maintain(self, setup_id, symbol, fill_price, levels, *, minutes=1):
        """One maintenance review through ``TradeMaintenance`` (the setup's recorded version,
        CRYPTO_MAINTENANCE_V3 for new setups, whose session guard stays NORMAL: V2's cadence):
        SESSION_SIMULATION price moves to +1R above the
        entry (the milestone trigger), the monitoring Jev answers (the fixture script's
        ``maintenance``, or the real model), the answer is checked and applied, and a raised
        stop is replaced at the simulated venue before the target exit. ``minutes`` > 1 then
        runs ``minutes`` - 1 further passes, the maintenance clock one minute on before each
        (``minute_reviews``: V2 reviews every completed minute)."""
        increment = D(self.config.sim_price_increment)
        risk = levels["max_entry_price"] - levels["stop"]
        bid = _grid(fill_price + risk, increment, ROUND_CEILING)
        ask = _grid(bid * (1 + HALF_SPREAD), increment, ROUND_CEILING)
        self.maintenance_bars.levels[symbol] = (fill_price, levels["stop"], levels["target"],
                                                increment)
        self.day_bars.levels[symbol] = self.maintenance_bars.levels[symbol]
        self.sim_quotes[str(setup_id)] = (bid, ask)  # The 24-hour review's simulated quote.
        self.engine.manage(setup_id, self._observation(bid=bid, ask=ask))

        def observe(_setup):
            # The simulated stream row, quoted and received now on the maintenance clock.
            now = self.maintenance_now()
            row = self._observation(bid=bid, ask=ask)
            return {**row, "quote_at": now.isoformat(), "trade_at": now.isoformat(),
                    "quote_received_at": now}

        used = self.meter.snapshot()["used"]
        view = {"bid": _text(bid), "ask": _text(ask), "bar_provider": SIMULATION,
                "policy_id": crypto_maintenance.recorded_policy_id(
                    self.engine._load(setup_id)[1])}
        try:
            active = [s for s in self.engine.store.active() if s["setup_id"] == setup_id]
            view["request_ids"] = asyncio.run(self.maintenance.run_pass(active, observe))
        except Exception as exc:
            view.update(outcome="REVIEW_FAILED", code=_code(exc))
        view["jev_calls"] = self.meter.snapshot()["used"] - used
        request = self._latest_event(setup_id, "POSITION_REVIEW_REQUEST")
        decision = self._latest_event(setup_id, "MAINTENANCE_DECISION")
        skipped = self._latest_event(setup_id, "POSITION_REVIEW_SKIPPED")
        if decision:
            view.update(outcome=decision["outcome"], code=decision.get("code"),
                        action=decision.get("action"), stop=decision.get("stop"),
                        target=decision.get("target"),
                        receipt_ids=decision.get("receipt_ids"))
        elif "outcome" not in view:
            view.update(outcome="SKIPPED" if skipped else "NOT_TRIGGERED",
                        reason=skipped.get("reason") if skipped else None)
        if request and request.get("context"):
            view["trigger_reasons"] = request["trigger"]["reasons"]
            view["options"] = {kind: [(o["option_id"], o["price"], o["bases"])
                                      for o in request["context"]["options"][kind]]
                               for kind in ("stop", "target")}
            view["state_bytes"] = request["context"]["manifest"]["state_bytes"]
        for _ in range(4):  # A raised stop is replaced at the venue by the protection loop.
            if not self.engine._load(setup_id)[1].get("stop_replace"):
                break
            self.engine.manage(setup_id, self._observation(bid=bid, ask=ask))
        replaced = self._latest_event(setup_id, "STOP_REPLACED")
        view["stop_replaced"] = ({"path": replaced["path"], "to_stop": replaced["to_stop"]}
                                 if replaced else None)
        state = self.engine._load(setup_id)[1]
        view["levels_after"] = {"stop": state.get("stop"), "target": state.get("target")}
        if minutes > 1:
            view["minute_reviews"] = self._minute_reviews(setup_id, observe, bid, ask,
                                                          minutes - 1)
        return view

    def _minute_reviews(self, setup_id, observe, bid, ask, count):
        """``count`` further maintenance passes, each after the maintenance clock moved one
        minute on (a completed 1-minute bar, BAR_1M under CRYPTO_MAINTENANCE_V2), at the same
        simulated quote; a raised stop is replaced before the next minute."""
        reviews = []
        for minute in range(1, count + 1):
            self.maintenance_offset += timedelta(seconds=60)
            used = self.meter.snapshot()["used"]
            entry = {"minute": minute, "clock": self.maintenance_now().isoformat()}
            try:
                active = [s for s in self.engine.store.active() if s["setup_id"] == setup_id]
                entry["request_ids"] = asyncio.run(self.maintenance.run_pass(active, observe))
            except Exception as exc:
                entry.update(outcome="REVIEW_FAILED", code=_code(exc))
            entry["jev_calls"] = self.meter.snapshot()["used"] - used
            for request_id in entry.get("request_ids") or ():
                decision = self._event_by_key("maintenance-decision:" + request_id)
                request = self._event_by_key_prefix(setup_id, request_id)
                if decision:
                    entry.update(outcome=decision["outcome"], code=decision.get("code"),
                                 action=decision.get("action"),
                                 trigger_reasons=decision.get("trigger_reasons"),
                                 receipt_ids=decision.get("receipt_ids"))
                if request:
                    entry["context_version"] = request["context"]["context_version"]
                    entry["review_history"] = len(
                        request["context"]["state"].get("review_history") or ())
            if "outcome" not in entry:
                entry["outcome"] = "NOT_DUE"
            skipped = self._latest_event(setup_id, "MAINTENANCE_REVIEW_SKIPPED")
            if skipped:
                entry["last_skipped_minute"] = skipped.get("bar_end")
            for _ in range(4):  # A raised stop is replaced before the next minute.
                if not self.engine._load(setup_id)[1].get("stop_replace"):
                    break
                self.engine.manage(setup_id, self._observation(bid=bid, ask=ask))
            state = self.engine._load(setup_id)[1]
            entry["levels_after"] = {"stop": state.get("stop"), "target": state.get("target")}
            reviews.append(entry)
        return reviews

    def _event_by_key(self, key):
        rows = self._rows("SELECT body FROM lab.managed_events WHERE idempotency_key=%s",
                          (key,))
        return rows[0]["body"] if rows else None

    def _event_by_key_prefix(self, setup_id, request_id):
        rows = self._rows(
            """SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind='POSITION_REVIEW_REQUEST' AND body->>'request_id'=%s""",
            (setup_id, request_id))
        return rows[0]["body"] if rows else None

    def _target_exit(self, setup_id, symbol):
        fills, target = [], None
        for _ in range(10):
            state = self.engine._load(setup_id)[1]
            if state.get("state") in TERMINAL_SETUPS:
                break
            target = D(state["target"])
            self.engine.manage(setup_id, self._observation(
                bid=target, ask=target * (1 + HALF_SPREAD),
            ))
            for order in list(self.venue.orders.values()):
                if (
                    (order["symbol"], order["side"], order["type"]) == (symbol, "sell", "market")
                    and order["status"] not in TERMINAL_ORDERS
                ):
                    remaining = D(order["qty"]) - D(order["filled_qty"])
                    fills.append(self._fill(order, remaining, target))
        final = self.engine._load(setup_id)[1]
        return {"target": _text(target) if target is not None else None, "fills": fills,
                "state": final.get("state"),
                "reason": final.get("reason") or final.get("exit_requested")}

    def _residual(self, setup_id, symbol):
        quantity = self.venue.inventory.get(symbol, D(0))
        orders = [o["id"] for o in self.venue.orders.values()
                  if o["symbol"] == symbol and o["status"] not in TERMINAL_ORDERS]
        reserved = bool(self._rows(
            "SELECT 1 FROM lab.managed_active_reservations WHERE setup_id=%s", (setup_id,)
        ))
        return {"venue_position_qty": _text(quantity), "open_orders": len(orders),
                "active_reservation": reserved,
                "zero": quantity == 0 and not orders and not reserved}

    def _simulate(self, setup, hold_open=False, maintenance_minutes=1):
        setup_id, symbol, state = setup["setup_id"], setup["symbol"], setup["state"]
        record = {"setup_id": str(setup_id), "symbol": symbol, "market": setup["market"],
                  "arm": state.get("arm"), "risk_policy_id": state.get("risk_policy_id"),
                  "simulation": SIMULATION}
        if setup["market"] != "CRYPTO":
            # Only crypto is classified in a session (the owner buckets); no stock is admitted.
            record.update(outcome="NOT_SIMULATED", reason="US_STOCK_SIMULATION_NOT_SUPPORTED")
            return record
        try:
            self._simulate_crypto(setup, record, hold_open, maintenance_minutes)
        except Exception as exc:
            record["error"] = _code(exc)
        final = self.engine._load(setup_id)[1]
        record["final_state"] = final.get("state")
        record["final_reason"] = final.get("reason") or final.get("exit_requested")
        record["state_path"] = self._state_path(setup_id)
        record["residual"] = self._residual(setup_id, symbol)
        return record

    def _simulate_crypto(self, setup, record, hold_open=False, maintenance_minutes=1):
        setup_id, symbol = setup["setup_id"], setup["symbol"]
        levels = {k: D(v) for k, v in setup["record_json"]["levels"].items()}
        trigger, stop = levels["entry_trigger"], levels["stop"]
        printed = self._observation(trade_price=trigger, bid=_below(trigger, stop), ask=trigger)
        record["trigger_print"] = {k: printed[k] for k in (
            "trade_price", "bid", "ask", "trade_at", "data_provider", "data_feed", "trade_id",
        )}
        decision = self.engine.observe_trigger(setup_id, printed)
        record["risk_decision"] = _risk_view(decision)
        if not decision or decision["outcome"] != "APPROVED":
            record["outcome"] = "NOT_ENTERED"
            return
        client_id = (decision["payload"] or {}).get("client_order_id")
        order = next((o for o in self.venue.orders.values()
                      if o.get("client_order_id") == client_id), None)
        if order is None:
            record["outcome"] = "ENTRY_ORDER_NOT_AT_VENUE"
            return
        fill_price = min(D(printed["ask"]), levels["max_entry_price"])
        record["entry_fill"] = self._fill(order, D(order["qty"]) - D(order["filled_qty"]),
                                          fill_price)
        record["protection"] = self._protect(setup_id, symbol, fill_price)
        if crypto_maintenance.active(self.engine._load(setup_id)[1]):
            record["maintenance_review"] = self._maintain(setup_id, symbol, fill_price, levels,
                                                          minutes=maintenance_minutes)
        else:
            record["management_review"] = self._review(setup_id, fill_price)
        record["protection_after_review"] = self._protect(setup_id, symbol, fill_price)
        if hold_open and crypto_holding.review_active(self.engine._load(setup_id)[1]):
            # CRYPTO_24H_REVIEW_V1: the trade stays open for ``review`` (package day-review).
            record["held_open"] = True
            record["outcome"] = self.engine._load(setup_id)[1].get("state")
            return
        record["exit"] = self._target_exit(setup_id, symbol)
        reconciliation = self.engine.reconcile()
        record["reconciliation_after_exit"] = {"clean": reconciliation["clean"],
                                               "mismatches": reconciliation["mismatches"]}
        record["outcome"] = self.engine._load(setup_id)[1].get("state")

    # --- the 24-hour review and early exits (package day-review) -------------------------

    def _reviewed(self):
        return [s for s in self.engine.store.active() if s["state"]["state"] == "OPEN"
                and crypto_holding.review_active(s["state"])]

    def _sim_quote(self, setup, bid=None):
        """The simulated quote a review reads: ``bid`` when given, else the trade's last
        simulated quote (+1R after its maintenance review), else +1R above the max entry."""
        increment = D(self.config.sim_price_increment)
        if bid is None:
            found = self.sim_quotes.get(str(setup["setup_id"]))
            if found:
                return found
            levels = {k: D(v) for k, v in setup["record_json"]["levels"].items()}
            bid = levels["max_entry_price"] * 2 - levels["stop"]
        price = _grid(D(bid), increment, ROUND_CEILING)
        return price, _grid(price * (1 + HALF_SPREAD), increment, ROUND_CEILING)

    def review(self, *, at=None, minutes=None, bid=None):
        """Move the review clock (forward only; ``at`` and ``minutes`` only, after validating
        every option) and run one pass of the day reviews; then the protection loop, on the wall
        clock, acts on what was decided. The clock then stays there until the next move."""
        from catalyst_lab.agent_identity import Principal

        with self.lock:
            reviewed = self._reviewed()
            target = self.review_now()
            if at is not None:
                if at not in {"request", "review"}:
                    raise SessionRefused("REVIEW_AT_INVALID")
                times = [datetime.fromisoformat(s["state"]["day_review_at"])
                         for s in reviewed if s["state"].get("day_review_at")]
                if not times:
                    raise SessionRefused("NO_TRADE_UNDER_REVIEW")
                target = max(target, min(times) + (
                    timedelta(seconds=1) if at == "review"
                    else timedelta(minutes=-30, seconds=1)))
            if minutes is not None:
                if type(minutes) is not int or not 1 <= minutes <= 1440:
                    raise SessionRefused("REVIEW_MINUTES_INVALID")
                target += timedelta(minutes=minutes)
            if bid is not None:
                try:
                    valid = D(str(bid)).is_finite() and D(str(bid)) > 0
                except (ArithmeticError, ValueError):
                    valid = False
                if not valid:
                    raise SessionRefused("REVIEW_BID_INVALID")
            if at is not None or minutes is not None:
                self.review_pin = target  # Held there until the next such step.
            now = self.review_now()

            def observe(setup):
                price, ask = self._sim_quote(setup, bid)
                stamp = self.review_now()
                return {"bid": _text(price), "ask": _text(ask), "trade_price": _text(price),
                        "quote_at": stamp.isoformat(), "trade_at": stamp.isoformat(),
                        "quote_received_at": stamp, "trade_id": f"{SIMULATION}-{uuid4().hex}",
                        "feed_healthy": True, "data_provider": SIMULATION,
                        "data_feed": SIMULATION}

            used = self.meter.snapshot()["used"]
            asyncio.run(self.day_reviews.run_pass(reviewed, observe))
            acted = {str(s["setup_id"]): self._after_review(s, bid) for s in reviewed}
            pending = self.trade_reviews.pending(Principal("muse", AGENT_ID))["items"]
            return {
                "review_now": now, "simulation": SIMULATION,
                "review_offset_seconds": (now - _utcnow()).total_seconds(),
                "review_clock_held": self.review_pin is not None and self.review_pin >= now,
                "jev_calls_used": self.meter.snapshot()["used"] - used,
                "jev_calls": self.meter.snapshot(),
                "trades": [self._review_view(s, acted.get(str(s["setup_id"])))
                           for s in reviewed],
                "pending": [{k: item.get(k) for k in (
                    "kind", "review_id", "flag_id", "setup_id", "symbol", "round",
                    "answer_due_at")} for item in pending],
            }

    def _after_review(self, setup, bid):
        """The protection loop on a decision: an exit (review or early exit) cancels the
        stop-limit and sells at the simulated bid; a raised stop is replaced at the venue."""
        setup_id, symbol = setup["setup_id"], setup["symbol"]
        price, ask = self._sim_quote(setup, bid)
        state = self.engine._load(setup_id)[1]
        if state.get("state") == "OPEN" and state.get("exit_requested"):
            fills = []
            for _ in range(10):
                if self.engine._load(setup_id)[1].get("state") in TERMINAL_SETUPS:
                    break
                self.engine.manage(setup_id, self._observation(bid=price, ask=ask))
                for order in list(self.venue.orders.values()):
                    if ((order["symbol"], order["side"], order["type"]) == (
                            symbol, "sell", "market") and order["status"] not in TERMINAL_ORDERS):
                        fills.append(self._fill(order, D(order["qty"]) - D(order["filled_qty"]),
                                                price))
            final = self.engine._load(setup_id)[1]
            # As ``execute`` does after a target exit: a reconciliation after the exit, so the
            # ledger shows the account clean after it (acceptance check
            # ZERO_RESIDUAL_CLEAN_RECONCILIATION_AFTER_EXIT).
            reconciliation = self.engine.reconcile()
            return {"exit": {"state": final.get("state"),
                             "reason": final.get("reason") or final.get("exit_requested"),
                             "fills": fills, "residual": self._residual(setup_id, symbol),
                             "reconciliation_after_exit": {
                                 "clean": reconciliation["clean"],
                                 "mismatches": reconciliation["mismatches"]}}}
        if state.get("stop_replace"):
            for _ in range(4):
                if not self.engine._load(setup_id)[1].get("stop_replace"):
                    break
                self.engine.manage(setup_id, self._observation(bid=price, ask=ask))
            replaced = self._latest_event(setup_id, "STOP_REPLACED")
            return {"stop_replaced": {"path": replaced["path"], "to_stop": replaced["to_stop"]}
                    if replaced else None}
        return None

    def _review_view(self, setup, acted):
        setup_id = setup["setup_id"]
        state = self.engine._load(setup_id)[1]
        rows = self._rows(
            """SELECT kind,body FROM lab.managed_events WHERE setup_id=%s AND kind=ANY(%s)
            ORDER BY event_seq""",
            (setup_id, list(day_review.REVIEW_KINDS + day_review.FLAG_KINDS)))

        def of(kind, fields):
            return [{k: r["body"].get(k) for k in fields} for r in rows if r["kind"] == kind]

        return {
            "setup_id": str(setup_id), "symbol": setup["symbol"], "state": state.get("state"),
            "review_at": state.get("day_review_at"), "continuations": state.get("continuations"),
            "stop": state.get("stop"), "target": state.get("target"),
            "exit_requested": state.get("exit_requested"),
            "requests": of(day_review.REQUESTED, ("review_id", "review_number", "review_at",
                                                  "addressee")),
            "agent_answers": [{"round": r["body"]["round"],
                               "decision": r["body"]["answer"]["decision"]}
                              for r in rows if r["kind"] == day_review.AGENT_ANSWER],
            "jev_results": of(day_review.JEV_RESULT, ("round", "status", "code", "answer")),
            "discussions": of(day_review.DISCUSSION_OPENED, ("review_id", "reply_due_at")),
            "decisions": of(day_review.DECISION, (
                "review_number", "outcome", "code", "level_change", "level_change_code",
                "levels_before", "levels_after", "next_review_at", "measurement")),
            "flags": of(day_review.FLAG_RAISED, ("flag_id", "side", "raised_at",
                                                 "answer_due_at")),
            "early_exit_decisions": of(day_review.EARLY_EXIT_DECISION, (
                "flag_id", "outcome", "code", "sides", "exit_requested")),
            "acted": acted,
        }

    # --- export -------------------------------------------------------------------------

    def _agent_declarations(self):
        if self.root is None:
            return {}
        path = self.root / "agent" / "unavailable.jsonl"
        declarations = {}
        if path.exists():
            for line in _read_private(path).splitlines():
                if line.strip():
                    row = json.loads(line)
                    declarations.setdefault(row["task_id"], []).append(row)
        return declarations

    def decisions_view(self):
        """Per item and revision: V2 and B1 (or B2), receipts, evidence tasks and answers,
        admission."""
        now = self.now()
        declarations = self._agent_declarations()
        setups = {
            str(row["receipt_id"]): {
                "setup_id": str(row["setup_id"]), "state": (row["state"] or {}).get("state"),
                "reason": (row["state"] or {}).get("reason"),
                "arm": (row["state"] or {}).get("arm"),
                "risk_policy_id": (row["state"] or {}).get("risk_policy_id"),
            }
            for row in self._rows("""SELECT s.setup_id,s.receipt_id,t.body AS state
                FROM lab.managed_setups s LEFT JOIN lab.managed_states t USING(setup_id)""")
        }
        refusals = {}
        for row in self._rows("""SELECT kind,body FROM lab.managed_events
                WHERE kind IN ('RUNTIME_ADMISSION_REFUSED','RESEARCH_ADMISSION_DECLINED',
                               'RUNTIME_REPLACEMENT_FAULT')
                ORDER BY event_seq"""):
            refusals.setdefault(row["body"].get("selection_event_seq"), []).append(
                # A replacement fault names its own code (the refusal is ``declined_code``).
                {"kind": row["kind"], "reason": row["body"].get("reason", row["body"].get("code"))}
            )
        cycles = []
        for cycle_id in self._cycle_ids(open_only=False):
            parsed = self._parse(self._cycle_events(cycle_id))
            shadows = self._shadows(cycle_id)
            policy = parsed.started.get("selection_policy", V2_POLICY)
            receipts = self._receipts(
                {r for d in parsed.decisions.values() for r in d.get("receipt_ids") or ()}
                | {r for q in parsed.quality.values() for r in q.get("receipt_ids") or ()}
            )
            items = []
            ranked = self._entries(parsed)
            for item_key, revisions in parsed.packets.items():
                entries = []
                for packet in revisions:
                    key = (item_key, packet["revision"])
                    decision, selection = parsed.decisions.get(key), parsed.selected.get(key)
                    quality = parsed.quality.get(key)
                    tasks = []
                    for task in parsed.tasks:
                        if (task["item_key"], task["revision"]) != key:
                            continue
                        resolution = parsed.resolved.get(task["task_id"])
                        tasks.append({
                            **task, "claims": parsed.claims.get(task["task_id"], []),
                            "resolved_by_revision": resolution["resolved_by_revision"]
                            if resolution else None,
                            "agent_declarations": declarations.get(task["task_id"], []),
                        })
                    entries.append({
                        "revision": packet["revision"], "evidence_hash": packet["evidence_hash"],
                        "source_content_hash": packet.get("source_content_hash"),
                        "created_at": packet.get("created_at"),
                        "received_at": packet.get("received_at"),
                        "sources": packet["state"].get("sources"),
                        "decision": decision,
                        "v2": self._v2_view(packet, decision, policy),
                        "b1_shadow": self._shadow_view(shadows.get(key)),
                        "b2": self._b2_view(decision),
                        "selection_rationale": packet.get("selection_rationale"),
                        "quality": quality,
                        "receipts": [receipts[r] for r in (decision or {}).get("receipt_ids") or ()
                                     if r in receipts],
                        "quality_receipts": [receipts[r] for r in (quality or {}).get(
                            "receipt_ids") or () if r in receipts],
                        "selection": None if selection is None else {
                            k: selection.get(k) for k in (
                                "selection_event_seq", "receipt_id", "review_valid_until",
                                "quality_required", "quality_rank", "quality_category",
                                "quality_floor", "dissent", "selection_policy",
                                "question_set_version",
                            ) + (TOPK_SELECTION_FIELDS if selection.get("selection_policy")
                                 in topk_rule.TOPK_POLICIES else ())
                        },
                        **({"topk": self._topk_view(decision, ranked.get(item_key))}
                           if policy in topk_rule.TOPK_POLICIES else {}),
                        "setup": setups.get(selection["receipt_id"]) if selection else None,
                        "admission_refusals": refusals.get(selection["selection_event_seq"], [])
                        if selection else [],
                        "evidence_tasks": tasks,
                    })
                items.append({"item_key": item_key, "symbol": revisions[-1]["symbol"],
                              "market": revisions[-1]["market"],
                              "signal_id": revisions[-1].get("signal_id"),
                              "rank": revisions[-1].get("rank"), "revisions": entries})
            started = parsed.started
            cycles.append({
                "cycle_id": cycle_id, "agent": started.get("agent"),
                "report_hash": started.get("report_hash"),
                "selection_policy": started.get("selection_policy", V2_POLICY),
                "selection_rule": started.get("selection_rule"),
                "expires_at": started.get("expires_at"),
                "contender_count": started.get("contender_count"),
                "submitted_count": started.get("submitted_count"),
                "rejected_count": started.get("rejected_count"),
                "intake_rejections": parsed.rejected, "items": items,
                # Top-K: the run's one ranking (every pick, with rank, scores, vetoes and
                # uncertain components), the picks skipped at publication and the replacement
                # decision of each pick declined at admission.
                **({"report_schema_version": started.get("report_schema_version"),
                    "run_slot": started.get("run_slot"), "ranking": parsed.ranking,
                    "selection_skips": parsed.skips, "replacements": parsed.replacements}
                   if started.get("selection_policy") in topk_rule.TOPK_POLICIES else {}),
            })
        return {"format": "AGENT_RESEARCH_SESSION_DECISIONS_V1", "generated_at": now,
                "session_id": self.session_id, "jev_calls": self.meter.snapshot(),
                "cycles": cycles}

    def _funnel(self):
        """``/api/v1/lab/analytics/research`` through the app itself, with the status token."""
        from fastapi.testclient import TestClient

        pages, after = [], 0
        client = TestClient(self.app)
        try:
            while True:
                response = client.get(
                    f"/api/v1/lab/analytics/research?after={after}&limit=500",
                    headers={"Authorization": "Bearer " + self.tokens["status"]},
                )
                response.raise_for_status()
                page = response.json()
                pages.append(page)
                if not page["items"] or page["next_cursor"] == after:
                    break
                after = page["next_cursor"]
        finally:
            client.close()
        return {"route": "/api/v1/lab/analytics/research", "credential_role": "status",
                "pages": pages}

    def _replay(self):
        replay = self.helpers.replay
        return replay.replay(replay.read_export(self.urls["catalyst_review"]), source="DATABASE")

    def _acceptance(self, directory):
        """``scripts/managed_acceptance_evidence.py`` as catalyst_review, per closed setup."""
        tool = _script("managed_acceptance_evidence")
        _private_directory(directory)
        results = {}
        for row in self._rows("""SELECT s.setup_id FROM lab.managed_setups s
                JOIN lab.managed_states t USING(setup_id)
                WHERE t.body->>'state'='CLOSED' ORDER BY s.event_seq"""):
            setup_id = str(row["setup_id"])
            target = directory / setup_id
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = tool.main(["--database-url", self.urls["catalyst_review"],
                                      "--setup-id", setup_id, "--output", str(target)])
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
            entry = {"exit_code": code, "summary": out.getvalue().splitlines(),
                     "stderr": err.getvalue().splitlines()}
            if (target / "evidence.json").exists():
                manifest = json.loads((target / "evidence.json").read_text())
                entry["evidence_sha256"] = (target / "evidence.sha256").read_text().strip()
                entry["checks"] = {c["name"]: c["passed"] for c in manifest["checks"]}
                entry["unknowns"] = sorted(manifest.get("unknowns") or ())
            results[setup_id] = entry
        return {"tool": "scripts/managed_acceptance_evidence.py", "role": "catalyst_review",
                "exit_codes": "0 all checks passed, 2 collected with a failed check, 1 refused",
                "setups": results}

    def _refuse_tokens(self, text):
        if any(token in text for token in self.tokens.values()):
            raise SessionRefused("EXPORT_CONTAINS_SESSION_TOKEN")

    def export(self, output):
        """Write every artifact into a new private directory; nothing but tokens is withheld."""
        from catalyst_lab.audit import verify_events

        with self.lock:
            output = Path(output)
            if not output.is_absolute():
                raise SessionRefused("EXPORT_OUTPUT_MUST_BE_ABSOLUTE")
            if os.path.lexists(output):
                raise SessionRefused("EXPORT_OUTPUT_EXISTS")
            output.mkdir(parents=True, mode=0o700)
            os.chmod(output, 0o700)
            files = {}

            def put(name, value):
                text = _json_text(value)
                self._refuse_tokens(text)
                _write_private(output / name, text)
                files[name] = {"sha256": hashlib.sha256(text.encode()).hexdigest(),
                               "bytes": len(text.encode())}

            events = self.repo.export_events()
            verification = verify_events(events)
            put("events.json", {"format": "AGENT_RESEARCH_SESSION_EVENTS_V1",
                                "session_id": self.session_id, "verification": verification,
                                "events": events})
            put("decisions.json", self.decisions_view())
            put("research-funnel.json", self._funnel())
            put("selection-replay.json", self._replay())
            acceptance = self._acceptance(output / "acceptance")
            put("acceptance-summary.json", acceptance)
            manifest = {
                "format": "AGENT_RESEARCH_SESSION_EXPORT_V1", "session_id": self.session_id,
                "exported_at": self.now(), "output": str(output),
                "labels": {"market_data_and_fills": SIMULATION,
                           "research_and_reviews": "REAL_HTTP_INTAKE_" + (
                               "FIXTURE_JEV" if self.config.jev == "fixture" else "TYPESAFE_JEV")},
                "configuration": self._configuration(),
                "audit": verification, "event_count": len(events),
                "jev_calls": self.meter.snapshot(),
                "acceptance_exit_codes": {k: v["exit_code"]
                                          for k, v in acceptance["setups"].items()},
                "acceptance_failed_checks": {
                    k: sorted(name for name, passed in (v.get("checks") or {}).items()
                              if not passed)
                    for k, v in acceptance["setups"].items()
                },
                "files": dict(files),
            }
            put("manifest.json", manifest)
            return {**manifest, "files": files}

    def status(self):
        with self.lock:
            states = Counter(
                (row["state"] or {}).get("state") for row in self._rows(
                    """SELECT t.body AS state FROM lab.managed_setups s
                    LEFT JOIN lab.managed_states t USING(setup_id)"""
                )
            )
            return {"session_id": self.session_id, "configuration": self._configuration(),
                    "configuration_hash": self.configuration_hash,
                    "jev_calls": self.meter.snapshot(), "last_tick_at": self.last_tick_at,
                    "cycles": len(self._cycle_ids(open_only=False)),
                    "open_cycles": len(self._cycle_ids(open_only=True)),
                    "setups_by_state": dict(states)}


# --- Rendering ------------------------------------------------------------------------------


def _table(header, rows, indent="  "):
    widths = [max(len(str(row[i])) for row in (header, *rows)) for i in range(len(header))]
    return [indent + "  ".join(str(value).ljust(width) for value, width in zip(
        row, widths, strict=True)).rstrip() for row in (header, *rows)]


def _calls_line(calls):
    return (f"Jev calls: {calls['used']} used of {calls['cap']} ({calls['remaining']} remaining;"
            f" {calls['refused']} refused as {calls['refusal_code']})")


def _outcome(view, key="reason"):
    if not view:
        return "PENDING"
    return f"{view['disposition']} {view.get(key) or ''}".rstrip()


def render_tick(report):
    lines = [_calls_line(report["jev_calls"])]
    if not report["cycles"]:
        lines.append("No open research cycles.")
    for cycle in report["cycles"]:
        agent = cycle["agent"] or {}
        floor = f" floor {cycle['quality_floor']}" if cycle["quality_floor"] else ""
        lines.append(
            f"cycle {cycle['cycle_id']}  agent {agent.get('agent_id')} "
            f"{agent.get('agent_version')}  rule {cycle['selection_policy']}{floor}  "
            f"expires {cycle['expires_at']}" + (f"  FAULT {cycle['fault']}" if cycle["fault"]
                                               else "")
        )
        for rejection in cycle["intake_rejections"]:
            lines.append(f"  intake rejected: item {rejection['index']} "
                         f"{rejection['signal_id']} {rejection['code']}")
        if cycle["selection_policy"] == B2_POLICY:
            lines += _b2_table(cycle)
            continue
        if cycle["selection_policy"] in topk_rule.TOPK_POLICIES:
            lines += _topk_table(cycle)
            continue
        rows = []
        for item in cycle["items"]:
            shadow = item["b1_shadow"]
            b1 = "NONE"
            if shadow:
                b1 = f"{shadow['disposition']} {','.join(shadow['reasons'] or ())}"
            latency = item["latency_ms"]
            rows.append((
                item["symbol"], item["revision"], _outcome(item["v2"]), b1,
                (shadow or {}).get("dissent") or "-", "yes" if item["selected"] else "no",
                item["receipt_id"] or "-",
                f"{D(str(latency)):.1f}" if latency is not None else "-",
                int(item["review_seconds_left"]),
            ))
        lines += _table(("SYMBOL", "REV", "V2 (REASON)", "B1 SHADOW (REASONS)", "DISSENT", "SEL",
                         "RECEIPT", "LATENCY_MS", "WINDOW_S"), rows)
        if cycle["selection_policy"] == B1_POLICY:
            lines.append("  (B1 active: its decisions are the B1 column; V2 is recomputed "
                         "from the receipts)")
    tasks = report["open_evidence_tasks"]
    lines.append("Open evidence tasks:" if tasks else "Open evidence tasks: none")
    for task in tasks:
        lines.append(
            f"  task {task['task_id']}  cycle {task['cycle_id']}  {task['symbol']} "
            f"rev {task['revision']} -> answer as rev {task['answer_revision']}  "
            f"expires {task['expires_at']}"
        )
        lines.append(f"    objections {', '.join(map(str, task['objection_codes']))}; "
                     f"requirements {', '.join(task['requirements'])}")
        for name, text in (task.get("instructions") or {}).items():
            lines.append(f"    {name}: {text}")
    lines.append(f"Selections must be admitted within {report['review_validity_seconds']} s "
                 "of the report's receipt (WINDOW_S).")
    return "\n".join(lines)


# Column labels for the five B2 components, in research_selection_b2.COMPONENTS order.
B2_COMPONENT_LABELS = {"news_stale": "NS", "already_priced": "AP",
                       "mechanism_contradicted": "MC", "inference_labelled": "IL",
                       "factual_claims_supported": "FC"}


def _b2_table(cycle):
    """A B2 cycle: its own decisions, the five components and the verdict as dissent."""
    rows = []
    for item in cycle["items"]:
        b2 = item.get("b2")
        decision = item["decision"]
        outcome = "PENDING"
        if b2:
            outcome = f"{b2['disposition']} {','.join(b2['reasons'] or ())}".rstrip()
        elif decision:
            outcome = f"{decision['disposition']} {decision['reason'] or ''}".rstrip()
        components = " ".join(
            f"{label}={(b2 or {}).get('components', {}).get(name) or '-'}"
            for name, label in B2_COMPONENT_LABELS.items()
        ) if b2 else "-"
        dissent = (b2 or {}).get("dissent") or "-"
        if b2 and b2.get("dissent_tied"):
            dissent += " (tied)"
        latency = item["latency_ms"]
        rows.append((
            item["symbol"], item["revision"], outcome, components, dissent,
            "yes" if item["selected"] else "no", item["receipt_id"] or "-",
            f"{D(str(latency)):.1f}" if latency is not None else "-",
            int(item["review_seconds_left"]),
        ))
    lines = _table(("SYMBOL", "REV", "B2 (REASONS)", "COMPONENTS", "DISSENT", "SEL", "RECEIPT",
                    "LATENCY_MS", "WINDOW_S"), rows)
    lines.append("  (B2 active: SKEPTIC_QUESTIONS_V2; the verdict is dissent and never blocks; "
                 "V2 and the B1 shadow do not apply to its receipts)")
    return lines


def _topk_table(cycle):
    """A top-K cycle: every pick with its rank, scores, vetoes and uncertain components."""
    rows = []
    items = sorted(cycle["items"], key=lambda item: (
        (item.get("topk") or {}).get("rank") or 10**6, item.get("rank") or 0))
    for item in items:
        view = item.get("topk") or {}
        codes = ",".join((view.get("veto_reasons") or []) + (view.get("uncertain") or [])) or "-"
        dissent = view.get("dissent") or "-"
        if view.get("dissent_tied"):
            dissent += " (tied)"
        rows.append((
            view.get("rank") or "-", item["symbol"], view.get("kind") or "-",
            view.get("status") or "PENDING", view.get("adjusted_score") or "-",
            view.get("quality_score") or "-", view.get("quality_category") or "-", codes,
            view.get("reason") or "-", dissent, "yes" if item["selected"] else "no",
        ))
    lines = _table(("RANK", "SYMBOL", "KIND", "STATUS", "ADJUSTED", "QUALITY", "CATEGORY",
                    "VETO / UNCERTAIN", "CODE", "DISSENT", "SEL"), rows)
    ranking = cycle.get("ranking")
    if ranking:
        counts = ranking["counts"]
        lines.append(f"  (top-K: K {ranking['k']}; {counts['RANKED']} ranked, "
                     f"{counts['VETOED']} vetoed, {counts['NOT_RANKED']} not ranked"
                     + ("" if ranking["complete"] else "; ranked at the review deadline")
                     + "; the verdict is dissent and never blocks)")
    else:
        lines.append(f"  (top-K: K {cycle.get('topk_k')}; ranking pending until every pick has "
                     "both reviews or the review deadline passes)")
    for skip in cycle.get("selection_skips") or ():
        lines.append(f"  skipped {skip['item_key']} (rank {skip['rank']}): {skip['reason']}")
    return lines


def render_execute(report):
    reconciliation = report["reconciliation"]
    lines = [f"Reconciliation: {'clean' if reconciliation['clean'] else 'NOT CLEAN'}"]
    if report.get("error"):
        lines.append(f"Stopped: {report['error']}")
    rows = []
    for item in report["admissions"]:
        admission = item["admission"]
        rows.append((item["symbol"], item["revision"], admission["outcome"],
                     admission.get("setup_id") or admission.get("code")))
    lines.append("Admissions:" if rows else "Admissions: no selected, unexpired packet")
    if rows:
        lines += _table(("SYMBOL", "REV", "OUTCOME", "SETUP / CODE"), rows)
    for life in report["lifecycles"]:
        lines.append(f"Setup {life['setup_id']}  {life['symbol']}  arm {life['arm']}  "
                     f"risk policy {life['risk_policy_id']}  [{SIMULATION}]")
        if life.get("reason"):
            lines.append(f"  {life.get('outcome')}: {life['reason']}")
            continue
        printed = life.get("trigger_print") or {}
        lines.append(f"  trigger print   trade {printed.get('trade_price')} bid "
                     f"{printed.get('bid')} ask {printed.get('ask')}")
        risk = life.get("risk_decision")
        if risk:
            lines.append(
                f"  risk decision   {risk['outcome']} {risk['reason']}  qty {risk['qty']} "
                f"limit {risk['limit_price']}  budget {risk['budget']}  binding "
                f"{risk['binding_constraint']}  ttl {risk['ttl_seconds']} s"
            )
        else:
            lines.append("  risk decision   none (the print did not trigger)")
        if life.get("entry_fill"):
            fill = life["entry_fill"]
            lines.append(f"  entry fill      {fill['qty']} @ {fill['price']}")
        if life.get("protection"):
            protection = life["protection"]
            lines.append(f"  protection      {'placed' if protection['protected'] else 'MISSING'}"
                         f"  stop {protection['stop']}")
        review = life.get("management_review")
        if review:
            detail = review.get("action") or review.get("reason") or review.get("code")
            lines.append(f"  management      {review['outcome']} {detail or ''}  "
                         f"(Jev calls {review.get('jev_calls', 0)})".rstrip())
        maintained = life.get("maintenance_review")
        if maintained:
            detail = maintained.get("action") or maintained.get("reason") or ""
            code = maintained.get("code")
            lines.append(f"  maintenance     {maintained.get('outcome')} {detail}"
                         f"{' ' + code if code else ''}  reasons "
                         f"{','.join(maintained.get('trigger_reasons') or ()) or '-'}  "
                         f"(Jev calls {maintained.get('jev_calls', 0)})")
            after = maintained.get("levels_after") or {}
            replaced = maintained.get("stop_replaced") or {}
            lines.append(f"  levels after    stop {after.get('stop')} target "
                         f"{after.get('target')}"
                         f"{'  replaced by ' + replaced['path'] if replaced else ''}")
            for minute in maintained.get("minute_reviews") or ():
                code = minute.get("code")
                lines.append(
                    f"  minute +{minute['minute']:<6} {minute.get('outcome')} "
                    f"{minute.get('action') or ''}{' ' + code if code else ''}  reasons "
                    f"{','.join(minute.get('trigger_reasons') or ()) or '-'}  "
                    f"history {minute.get('review_history', '-')}  "
                    f"(Jev calls {minute.get('jev_calls', 0)})")
        if life.get("held_open"):
            lines.append("  held open       for the 24-hour review (review --at request)")
        exit_ = life.get("exit")
        if exit_:
            price = exit_["fills"][-1]["price"] if exit_["fills"] else "-"
            lines.append(f"  exit            {exit_['reason']} @ {price}")
        lines.append(f"  state path      {' -> '.join(life.get('state_path') or ())}")
        residual = life.get("residual") or {}
        lines.append(f"  residual        {'zero' if residual.get('zero') else 'NOT ZERO'}  "
                     f"(position {residual.get('venue_position_qty')}, open orders "
                     f"{residual.get('open_orders')}, reservation "
                     f"{residual.get('active_reservation')})")
        if life.get("error"):
            lines.append(f"  error           {life['error']}")
    if "jev_calls" in report:
        lines.append(_calls_line(report["jev_calls"]))
    return "\n".join(lines)


def render_review(report):
    held = ", held until the next --at/--minutes" if report.get("review_clock_held") else ""
    lines = [f"Review clock {report['review_now']} (offset "
             f"{report['review_offset_seconds']:.0f} s{held})  [{SIMULATION}]"]
    if not report["trades"]:
        lines.append("No open trade under the 24-hour review (CRYPTO_24H_REVIEW_V1/V2).")
    for trade in report["trades"]:
        lines.append(f"Setup {trade['setup_id']}  {trade['symbol']}  {trade['state']}  "
                     f"T {trade['review_at']}  continuations {trade['continuations']}  stop "
                     f"{trade['stop']}  target {trade['target']}")
        for request in trade["requests"]:
            addressee = request["addressee"] or {}
            lines.append(f"  request #{request['review_number']}  {request['review_id']}  to "
                         f"{addressee.get('agent_id')} ({addressee.get('basis')})")
        for answer in trade["agent_answers"]:
            lines.append(f"  agent {answer['round']:<10} {answer['decision']}")
        for result in trade["jev_results"]:
            decision = (result.get("answer") or {}).get("decision")
            lines.append(f"  jev {result['round']:<12} {result['status']} "
                         f"{decision or result.get('code') or ''}".rstrip())
        for discussion in trade["discussions"]:
            lines.append(f"  discussion     agent reply due {discussion['reply_due_at']}")
        for decision in trade["decisions"]:
            refused = decision.get("level_change_code")
            lines.append(f"  decision #{decision['review_number']}   {decision['outcome']} "
                         f"{decision['code']}  levels {decision.get('level_change')}"
                         f"{' ' + refused if refused else ''}"
                         f"  after {decision.get('levels_after')}")
        for flag in trade["flags"]:
            lines.append(f"  exit flag      {flag['side']}  {flag['flag_id']}  due "
                         f"{flag['answer_due_at']}")
        for decision in trade["early_exit_decisions"]:
            lines.append(f"  early exit     {decision['outcome']} {decision['code'] or ''}"
                         .rstrip())
        acted = trade.get("acted") or {}
        if acted.get("exit"):
            reconciled = acted["exit"].get("reconciliation_after_exit") or {}
            lines.append(f"  exit           {acted['exit']['reason']}  {acted['exit']['state']}"
                         + ("  reconciliation " + ("clean" if reconciled["clean"] else
                                                   "NOT CLEAN") if reconciled else ""))
        if acted.get("stop_replaced"):
            lines.append(f"  stop replaced  {acted['stop_replaced']['path']} to "
                         f"{acted['stop_replaced']['to_stop']}")
    lines.append("Pending for the agent: " + (", ".join(
        f"{item['kind']} {item.get('review_id') or item.get('flag_id')} "
        f"{item.get('round') or ''} due {item['answer_due_at']}".replace("  ", " ")
        for item in report["pending"]) or "none"))
    lines.append(_calls_line(report["jev_calls"]))
    return "\n".join(lines)


def render_export(manifest):
    audit = manifest["audit"]
    lines = [f"Exported to {manifest['output']}",
             f"Audit chain: {'valid' if audit.get('valid') else 'INVALID'} "
             f"({manifest['event_count']} events)"]
    lines += _table(("FILE", "SHA256", "BYTES"), [
        (name, meta["sha256"], meta["bytes"]) for name, meta in sorted(manifest["files"].items())
    ])
    failed = manifest.get("acceptance_failed_checks") or {}
    for setup_id, code in sorted(manifest["acceptance_exit_codes"].items()):
        checks = ", ".join(failed.get(setup_id) or ()) or "none"
        lines.append(f"Acceptance evidence {setup_id}: exit {code} (failed checks: {checks})")
    if failed:
        lines.append("  A session has no market or trade-updates stream, so "
                     "SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER cannot pass.")
    lines.append(_calls_line(manifest["jev_calls"]))
    return "\n".join(lines)


def render_status(report):
    configuration = report["configuration"]
    return "\n".join([
        f"Session {report['session_id']}  Jev {configuration['jev_provider']}  rule "
        f"{configuration['selection_rule']}  management reviews "
        f"{configuration['management_reviews']}",
        f"Cycles {report['cycles']} ({report['open_cycles']} open)  setups "
        f"{report['setups_by_state']}  last tick {report['last_tick_at']}",
        _calls_line(report["jev_calls"]),
    ])


# --- Control socket (the start process) -------------------------------------------------------


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline(1 << 20) or b"{}")
            response = self.server.dispatch(request)
        except SessionRefused as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            # The client gets the code; the session's own stderr gets the traceback.
            traceback.print_exc()
            response = {"ok": False, "error": _code(exc)}
        self.wfile.write(json.dumps(json_safe(response)).encode() + b"\n")


class ControlServer(socketserver.UnixStreamServer):
    """One command at a time, from the owner's own processes (socket mode 0600, root 0700)."""

    def __init__(self, path, session, *, stop_event, keep):
        self.session, self.stop_event, self.keep = session, stop_event, keep
        self.stop_request = None
        super().__init__(str(path), _ControlHandler)
        os.chmod(path, 0o600)

    def dispatch(self, request):
        command = request.get("command")
        options = request.get("options") or {}
        session = self.session
        print(f"[{_utcnow().isoformat()}] control: {command}", flush=True)
        if command == "tick":
            report = session.tick()
            return {"ok": True, "report": report, "text": render_tick(json_safe(report))}
        if command == "execute":
            report = session.execute(simulate_prints=bool(options.get("simulate_prints")),
                                     hold_open=bool(options.get("hold_open")),
                                     maintenance_minutes=options.get("maintenance_minutes")
                                     or 1)
            return {"ok": True, "report": report, "text": render_execute(json_safe(report))}
        if command == "review":
            report = session.review(at=options.get("at"), minutes=options.get("minutes"),
                                    bid=options.get("bid"))
            return {"ok": True, "report": report, "text": render_review(json_safe(report))}
        if command == "export":
            manifest = session.export(Path(options["output"]))
            return {"ok": True, "report": manifest, "text": render_export(json_safe(manifest))}
        if command == "status":
            report = session.status()
            return {"ok": True, "report": report, "text": render_status(json_safe(report))}
        if command == "stop":
            manifest = None
            if not options.get("no_export"):
                output = options.get("output") or str(
                    session.root / "exports" / ("stop-" + _stamp())
                )
                manifest = session.export(Path(output))
            keep = bool(options.get("keep")) or self.keep
            self.stop_request = {"reason": "STOP_COMMAND", "keep": keep,
                                 "exported": manifest is not None}
            self.stop_event.set()
            report = {"export": manifest, "keep_cluster": keep}
            text = (render_export(json_safe(manifest)) + "\n" if manifest else "") + (
                "Stopping the session" + (" (cluster kept running)." if keep else "."))
            return {"ok": True, "report": report, "text": text}
        raise SessionRefused("UNKNOWN_CONTROL_COMMAND")


def control_call(root, command, options=None, *, timeout=900):
    path = root / CONTROL_SOCKET
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
    except OSError:
        connection.close()
        raise SessionRefused("SESSION_NOT_RUNNING") from None
    with connection:
        connection.sendall(json.dumps({"command": command, "options": options or {}}).encode()
                           + b"\n")
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(1 << 16):
            chunks.append(chunk)
    return json.loads(b"".join(chunks) or b"{}")


# --- The start command ------------------------------------------------------------------------


def serve_http(app, port):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="off",
    ))
    thread = threading.Thread(target=server.run, daemon=True, name="session-http")
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            raise SessionRefused("SESSION_HTTP_PORT_UNAVAILABLE")
        time.sleep(0.05)
    return server, thread, server.servers[0].sockets[0].getsockname()[1]


def run_start(args):
    root = new_session_root(args.root)
    if args.port != 0 and not 1024 <= args.port <= 65535:
        raise SessionRefused("SESSION_PORT_INVALID")
    config = SessionConfig(
        args.jev, max_jev_calls=args.max_jev_calls, selection_rule=args.selection_rule,
        quality_floor=args.quality_floor, topk_k=args.topk_k,
        management_reviews=args.management_reviews,
        report_max_seconds=args.report_max_seconds, sim_price_increment=args.sim_price_increment,
        extra_crypto_pairs=tuple(
            pair.strip() for pair in (args.extra_crypto_pairs or "").split(",") if pair.strip()
        ),
    )
    fixture = None
    if args.jev == "fixture":
        fixture = FixtureScript.load(args.fixture_script) if args.fixture_script else None
    else:
        if args.fixture_script:
            raise SessionRefused("FIXTURE_SCRIPT_REQUIRES_FIXTURE_JEV")
        from catalyst_lab.jev_secrets import typesafe_key

        try:
            typesafe_key()  # Fail before any setup; the value is never printed or stored.
        except Exception:
            raise SessionRefused("TYPESAFE_CREDENTIAL_UNAVAILABLE") from None
    from catalyst_lab import localdb

    root.mkdir(mode=0o700)  # Atomic: an existing directory is refused here as well.
    os.chmod(root, 0o700)
    stop_event = threading.Event()
    session = server = thread = control = serving = None
    keep, final = args.keep, None
    try:
        localdb.start(root)
        tokens, token_files = write_tokens(root)
        _private_directory(root / "agent")
        _private_directory(root / "exports")
        session = AgentResearchSession(
            urls=role_urls(root), config=config, tokens=tokens, fixture=fixture, root=root,
        )
        server, thread, port = serve_http(session.app, args.port)
        control = ControlServer(root / CONTROL_SOCKET, session, stop_event=stop_event,
                                keep=args.keep)
        serving = threading.Thread(target=control.serve_forever, daemon=True,
                                   name="session-control")
        serving.start()
        descriptor = {
            "format": SESSION_FORMAT, "session_id": session.session_id,
            "created_at": _utcnow(), "pid": os.getpid(), "root": str(root),
            "ledger": {"root": str(root), "postgres": str(root / "postgres"),
                       "kind": "DISPOSABLE_SESSION_CLUSTER"},
            "http": {"host": "127.0.0.1", "port": port, "base_url": f"http://127.0.0.1:{port}"},
            "control_socket": str(root / CONTROL_SOCKET),
            "agent": {"agent_id": AGENT_ID, **_guidelines()},
            "token_files": token_files, "configuration": session._configuration(),
            "configuration_hash": session.configuration_hash,
            "keep_cluster_on_stop": args.keep,
            "notes": [
                "Tokens are in the token files only; this file never contains one.",
                "Every print, quote, bar, asset-metadata row, order and fill of the venue is "
                + SIMULATION + ".",
                f"A selection must be admitted within {REVIEW_VALIDITY_SECONDS} s of the "
                "report's receipt.",
            ],
        }
        _write_private(root / SESSION_FILE, _json_text(descriptor))
        print(_json_text({
            "status": "SESSION_READY", "session_id": session.session_id, "root": str(root),
            "base_url": descriptor["http"]["base_url"], "session_file": str(root / SESSION_FILE),
            "control_socket": descriptor["control_socket"], "agent_id": AGENT_ID,
            "token_files": token_files, "jev_provider": config.jev,
            "max_jev_calls": config.max_jev_calls, "selection_rule": config.rule.policy,
            "quality_floor": config.rule.quality_floor,
            "topk_k": config.rule.k if topk_rule.is_topk(config.rule) else None,
            "management_reviews": config.management_reviews,
            "review_validity_seconds": REVIEW_VALIDITY_SECONDS,
        }), end="", flush=True)

        def stop(*_):
            stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, stop)
        while not stop_event.wait(0.5):
            pass
        request = control.stop_request or {"reason": "SIGNAL", "keep": args.keep,
                                           "exported": False}
        keep = request["keep"]
        if not request["exported"]:
            final = session.export(root / "exports" / ("final-" + _stamp()))["output"]
    finally:
        if serving is not None:
            control.shutdown()  # Waits for a command in progress to answer its client.
        if control is not None:
            control.server_close()
            with contextlib.suppress(OSError):
                (root / CONTROL_SOCKET).unlink()
        if server is not None:
            server.should_exit = True
            thread.join(timeout=10)
        if session is not None:
            session.close()
        cluster = "KEPT_RUNNING" if keep else "STOPPED"
        # A failed migration can leave the new cluster running: stop whatever is running.
        if not keep and (root / "postgres" / "postmaster.pid").exists():
            try:
                localdb.stop(root)
            except Exception:
                cluster = "STOP_FAILED"
        if (root / "postgres").exists():
            stopped = {"stopped_at": _utcnow(), "cluster": cluster, "final_export": final}
            with contextlib.suppress(OSError):
                _write_private(root / STOPPED_FILE, _json_text(stopped))
            print(_json_text({"status": "SESSION_STOPPED", **stopped}), end="", flush=True)
    return 0


def _guidelines():
    from catalyst_lab.muse_guidelines import MUSE_GUIDELINES_SHA256, MUSE_GUIDELINES_VERSION

    return {"guidelines_version": MUSE_GUIDELINES_VERSION,
            "guidelines_sha256": MUSE_GUIDELINES_SHA256}


# --- Client commands ------------------------------------------------------------------------


def load_session(value):
    root = existing_session_root(value)
    return root, json.loads(_read_private(root / SESSION_FILE))


def _agent_client(descriptor):
    token = _read_private(descriptor["token_files"][AGENT_ID]).strip()
    return httpx.Client(
        base_url=descriptor["http"]["base_url"], timeout=60, trust_env=False,
        headers={"Authorization": "Bearer " + token},
    )


def _body(response):
    try:
        return response.json()
    except ValueError:
        return {"detail": "NON_JSON_RESPONSE"}


def _emit(value):
    print(_json_text(value), end="")


def _control_command(args, command, options=None):
    root = existing_session_root(args.root)
    response = control_call(root, command, options)
    if not response.get("ok"):
        raise SessionRefused(response.get("error") or "CONTROL_COMMAND_FAILED")
    if args.json:
        _emit(response["report"])
    else:
        print(response["text"])
    return root, response


def run_stop(args):
    options = {"keep": args.keep, "no_export": args.no_export}
    if args.output:
        options["output"] = str(Path(args.output).expanduser().resolve())
    root, _ = _control_command(args, "stop", options)
    deadline = time.monotonic() + 120
    while not (root / STOPPED_FILE).exists():
        if time.monotonic() > deadline:
            raise SessionRefused("SESSION_STOP_NOT_CONFIRMED")
        time.sleep(0.2)
    stopped = json.loads(_read_private(root / STOPPED_FILE))
    print(f"Session stopped; cluster {stopped['cluster']}.")
    return 0


def run_submit(args):
    from catalyst_lab.muse_reports import REPORT_SCHEMA_V2
    from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3

    _, descriptor = load_session(args.root)
    try:
        report = strict_json(Path(args.report).expanduser().read_bytes())
    except (OSError, ValueError, TypeError, UnicodeError):
        raise SessionRefused("REPORT_FILE_UNREADABLE") from None
    if not isinstance(report, dict):
        raise SessionRefused("REPORT_FILE_INVALID")
    now = _utcnow()
    report.setdefault("report_id", str(uuid4()))
    report.setdefault("schema_version", REPORT_SCHEMA_V2)
    report["generated_at"] = now.isoformat()
    if args.valid_minutes is not None:
        if not 1 <= args.valid_minutes <= 1440:
            raise SessionRefused("VALID_MINUTES_INVALID")
        report["valid_until"] = (now + timedelta(minutes=args.valid_minutes)).isoformat()
    if report["schema_version"] == REPORT_SCHEMA_V3:
        # The run this report answers under the session's schedule (the latest one at or
        # before now) and, unless the file sets them, when the context was read and how long
        # the report is valid (at most until the next run plus the grace, and 24 hours).
        schedule = ResearchSchedule.from_json(json.dumps({
            k: v for k, v in descriptor["configuration"]["research_schedule"].items()
            if k != "version"}))
        slot = schedule.latest_at_or_before(now)
        report.setdefault("run_slot", schedule.local(slot).isoformat())
        report.setdefault("context_as_of", now.isoformat())
        report.setdefault("valid_until", min(
            now + timedelta(hours=20), schedule.validity_limit(slot)).isoformat())
    if isinstance(report.get("agent"), dict) and not report["agent"].get("run_id"):
        report["agent"]["run_id"] = str(uuid4())
    with _agent_client(descriptor) as client:
        response = client.post("/api/v1/lab/research-reports", json=report)
    _emit({"http_status": response.status_code, "report_id": report["report_id"],
           "body": _body(response)})
    return 0 if response.status_code == 202 else 1


def _find_task(client, cycle_id, task_id):
    """The task, whether it is resolved, and its latest lease, from the cycle's outputs."""
    after, task, resolved, lease = 0, None, False, None
    while True:
        response = client.get(f"/api/v1/lab/cycles/{cycle_id}/outputs",
                              params={"after": after, "limit": 1000})
        if response.status_code != 200:
            raise SessionRefused("CYCLE_OUTPUTS_UNAVAILABLE")
        page = response.json()
        for event in page["items"]:
            body = event["body"]
            if body.get("task_id") == task_id:
                if event["kind"] == "RESEARCH_EVIDENCE_TASK":
                    task = body
                elif event["kind"] == "RESEARCH_EVIDENCE_RESOLVED":
                    resolved = True
                elif event["kind"] == "RESEARCH_EVIDENCE_CLAIM":
                    lease = {"claimant": body.get("claimant"),
                             "lease_until": body.get("lease_until")}
        if not page["items"] or page["next_cursor"] == after:
            break
        after = page["next_cursor"]
    if task is None:
        raise SessionRefused("EVIDENCE_TASK_NOT_FOUND")
    return task, resolved, lease


def _claim(client, cycle_id, task_id, lease_seconds):
    """Claim the cycle's unleased open tasks (the API claims in bulk, never one task), then
    report this task's current lease: every task of a cycle belongs to the same agent."""
    response = client.post(f"/api/v1/lab/cycles/{cycle_id}/evidence-tasks/claim",
                           json={"claimant": CLAIMANT, "lease_seconds": lease_seconds,
                                 "limit": 30})
    body = _body(response)
    task, resolved, lease = _find_task(client, cycle_id, task_id)
    return task, resolved, {
        "http_status": response.status_code,
        "claimed_task_ids": [t["task_id"] for t in body.get("tasks", [])]
        if response.status_code == 200 else [],
        "detail": body.get("detail"), "task_lease": lease,
    }


def run_answer(args):
    _, descriptor = load_session(args.root)
    try:
        evidence = strict_json(Path(args.evidence).expanduser().read_bytes())
    except (OSError, ValueError, TypeError, UnicodeError):
        raise SessionRefused("EVIDENCE_FILE_UNREADABLE") from None
    # ``selection_rationale`` revises the reviewed rationale (selection rule B2 cycles only).
    fields = {"sources", "thesis", "disproof", "economic_relationship", "technical_facts",
              "selection_rationale"}
    if not isinstance(evidence, dict) or set(evidence) - fields - {"item_key", "revision",
                                                                   "task_id"}:
        raise SessionRefused("EVIDENCE_FILE_INVALID")
    with _agent_client(descriptor) as client:
        task, resolved, claim = _claim(client, args.cycle, args.task, 120)
        body = {"task_id": args.task, "item_key": task["item_key"],
                "revision": task["revision"] + 1,
                **{k: evidence[k] for k in fields if k in evidence}}
        if any(k in evidence and evidence[k] != body[k] for k in ("item_key", "revision",
                                                                  "task_id")):
            raise SessionRefused("EVIDENCE_FILE_TASK_MISMATCH")
        response = client.post(f"/api/v1/lab/cycles/{args.cycle}/evidence", json=body)
    _emit({"task_id": args.task, "item_key": task["item_key"], "revision": body["revision"],
           "task_already_resolved": resolved, "claim": claim,
           "evidence": {"http_status": response.status_code, "body": _body(response)}})
    return 0 if response.status_code == 200 else 1


def run_unavailable(args):
    root, descriptor = load_session(args.root)
    if not _UNAVAILABLE_CODE.fullmatch(args.code):
        raise SessionRefused("UNAVAILABLE_CODE_INVALID")
    with _agent_client(descriptor) as client:
        task, resolved, claim = _claim(client, args.cycle, args.task, 300)
    declaration = {
        "task_id": args.task, "cycle_id": args.cycle, "item_key": task["item_key"],
        "revision": task["revision"], "code": args.code, "declared_at": _utcnow(),
        "agent_id": AGENT_ID, "claim": claim,
        "server_record": "RESEARCH_EVIDENCE_CLAIM lease only: the managed API has no "
                         "unavailable route, so the task stays open until it expires.",
    }
    _append_private(root / "agent" / "unavailable.jsonl", declaration)
    _emit({"status": "UNAVAILABLE_RECORDED_IN_SESSION", "task_already_resolved": resolved,
           **declaration, "trade_authorized": False})
    return 0


def _review_texts(args):
    body = {"what_changed": args.what_changed, "next_24h": args.next_24h,
            "proves_wrong": args.proves_wrong}
    if args.sources:
        try:
            sources = strict_json(Path(args.sources).expanduser().read_bytes())
        except (OSError, ValueError, TypeError, UnicodeError):
            raise SessionRefused("SOURCES_FILE_UNREADABLE") from None
        if not isinstance(sources, list):
            raise SessionRefused("SOURCES_FILE_INVALID")
        body["sources"] = sources
    return body


def _post(descriptor, path, body):
    with _agent_client(descriptor) as client:
        response = client.post(path, json=body)
    _emit({"http_status": response.status_code, "body": _body(response)})
    return 0 if response.status_code == 200 else 1


def run_reviews(args):
    """The agent's pending 24-hour reviews and Jev exit flags (GET /api/v1/lab/reviews)."""
    _, descriptor = load_session(args.root)
    with _agent_client(descriptor) as client:
        response = client.get("/api/v1/lab/reviews")
    _emit({"http_status": response.status_code, "body": _body(response)})
    return 0 if response.status_code == 200 else 1


def run_review_answer(args):
    """AGENT_REVIEW_ANSWER_V1 for a 24-hour review round."""
    _, descriptor = load_session(args.root)
    body = {"schema_version": "AGENT_REVIEW_ANSWER_V1", "answer_id": str(uuid4()),
            "decision": args.decision, **_review_texts(args)}
    for key in ("suggested_stop", "suggested_target"):
        if getattr(args, key):
            body[key] = getattr(args, key)
    return _post(descriptor, f"/api/v1/lab/reviews/{args.review_id}/answer", body)


def run_flag_answer(args):
    """AGENT_REVIEW_ANSWER_V1 (EXIT or CONTINUE) for a Jev exit flag."""
    _, descriptor = load_session(args.root)
    body = {"schema_version": "AGENT_REVIEW_ANSWER_V1", "answer_id": str(uuid4()),
            "decision": args.decision, **_review_texts(args)}
    return _post(descriptor, f"/api/v1/lab/exit-flags/{args.flag_id}/answer", body)


def run_exit_flag(args):
    """AGENT_EXIT_FLAG_V1: the agent's own early-exit flag (its lifecycle from the position)."""
    _, descriptor = load_session(args.root)
    with _agent_client(descriptor) as client:
        context = client.get(f"/api/v1/lab/positions/{args.setup_id}/news")
    if context.status_code != 200:
        raise SessionRefused("POSITION_CONTEXT_UNAVAILABLE")
    body = {"schema_version": "AGENT_EXIT_FLAG_V1", "flag_ref": str(uuid4()),
            "lifecycle_id": context.json()["lifecycle_id"], **_review_texts(args)}
    return _post(descriptor, f"/api/v1/lab/positions/{args.setup_id}/exit-flag", body)


# --- CLI -------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agent_research_session.py", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="create the session and serve it until stop")
    start.add_argument("--root", required=True, help="new /tmp/catalyst-session-* directory")
    start.add_argument("--port", type=int, required=True, help="127.0.0.1 port; 0 = ephemeral")
    start.add_argument("--jev", choices=("fixture", "typesafe"), required=True)
    start.add_argument("--max-jev-calls", type=int, default=DEFAULT_MAX_JEV_CALLS)
    start.add_argument("--fixture-script", help="scripted fixture answers (fixture only)")
    start.add_argument("--selection-rule", choices=("V2", "B1", "B2", "TOPK", "TOPK2"),
                       default="V2")
    start.add_argument("--quality-floor", choices=("WEAK", "ADEQUATE", "STRONG"))
    start.add_argument("--topk-k", type=int, help="TOPK only: picks kept per report, 5-10")
    start.add_argument("--management-reviews", choices=("ENABLED", "DISABLED"),
                       default="ENABLED")
    start.add_argument("--report-max-seconds", type=int, default=DEFAULT_REPORT_MAX_SECONDS)
    start.add_argument("--sim-price-increment", default=SIMULATED_INCREMENT,
                       help="simulated crypto price, lot and minimum-order grid")
    start.add_argument("--extra-crypto-pairs", help="comma-separated further Alpaca USD pairs "
                       "(e.g. ARB/USD,BONK/USD) in the CRYPTO_OTHER bucket")
    start.add_argument("--keep", action="store_true", help="leave the cluster running on stop")
    for name, text in (("tick", "review and publish every open cycle"),
                       ("execute", "admit selections; optionally simulate their lifecycle"),
                       ("export", "write every session artifact"),
                       ("status", "session configuration and counts"),
                       ("stop", "export and shut the session down"),
                       ("review", "move the review clock and run the 24-hour reviews")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--root", required=True)
        command.add_argument("--json", action="store_true", help="print the full JSON report")
        if name == "execute":
            command.add_argument("--simulate-prints", action="store_true")
            command.add_argument("--hold-open", action="store_true",
                                 help="keep maintained report-V3 crypto trades open for review")
            command.add_argument("--maintenance-minutes", type=int, default=1,
                                 help="1-10 maintenance passes per maintained trade, one per "
                                      "completed minute of the maintenance clock")
        if name == "review":
            command.add_argument("--at", choices=("request", "review"),
                                 help="request: 30 min before T; review: T")
            command.add_argument("--minutes", type=int, help="move the review clock N minutes")
            command.add_argument("--bid", help="the simulated bid (default: the trade's last)")
        if name == "export":
            command.add_argument("--output", required=True, help="new directory")
        if name == "stop":
            command.add_argument("--output", help="new export directory (default in root)")
            command.add_argument("--keep", action="store_true")
            command.add_argument("--no-export", action="store_true")
    submit = commands.add_parser("submit", help="POST a report as agent " + AGENT_ID)
    submit.add_argument("--root", required=True)
    submit.add_argument("--report", required=True)
    submit.add_argument("--valid-minutes", type=int, help="set valid_until to now + N minutes")
    answer = commands.add_parser("answer", help="claim a task and submit an evidence revision")
    unavailable = commands.add_parser("unavailable", help="declare a task's evidence unavailable")
    for command in (answer, unavailable):
        command.add_argument("--root", required=True)
        command.add_argument("--cycle", required=True)
        command.add_argument("--task", required=True)
    answer.add_argument("--evidence", required=True)
    unavailable.add_argument("--code", required=True)
    reviews = commands.add_parser("reviews", help="the agent's pending reviews and exit flags")
    reviews.add_argument("--root", required=True)
    review_answer = commands.add_parser("review-answer", help="answer a 24-hour review round")
    review_answer.add_argument("--review-id", required=True)
    flag_answer = commands.add_parser("flag-answer", help="answer a Jev exit flag")
    flag_answer.add_argument("--flag-id", required=True)
    exit_flag = commands.add_parser("exit-flag", help="raise the agent's own exit flag")
    exit_flag.add_argument("--setup-id", required=True)
    for command in (review_answer, flag_answer, exit_flag):
        command.add_argument("--root", required=True)
        command.add_argument("--what-changed", required=True)
        command.add_argument("--next-24h", required=True)
        command.add_argument("--proves-wrong", required=True)
        command.add_argument("--sources", help="a JSON file of 0-8 sources")
    for command in (review_answer, flag_answer):
        command.add_argument("--decision", choices=("CONTINUE", "EXIT"), required=True)
    review_answer.add_argument("--suggested-stop")
    review_answer.add_argument("--suggested-target")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            return run_start(args)
        if args.command == "stop":
            return run_stop(args)
        if args.command in {"tick", "status"}:
            _control_command(args, args.command)
            return 0
        if args.command == "execute":
            _control_command(args, "execute", {"simulate_prints": args.simulate_prints,
                                               "hold_open": args.hold_open,
                                               "maintenance_minutes": args.maintenance_minutes})
            return 0
        if args.command == "review":
            _control_command(args, "review", {"at": args.at, "minutes": args.minutes,
                                              "bid": args.bid})
            return 0
        if args.command == "export":
            _control_command(args, "export",
                             {"output": str(Path(args.output).expanduser().resolve())})
            return 0
        return {"submit": run_submit, "answer": run_answer, "unavailable": run_unavailable,
                "reviews": run_reviews, "review-answer": run_review_answer,
                "flag-answer": run_flag_answer, "exit-flag": run_exit_flag}[args.command](args)
    except SessionRefused as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except httpx.HTTPError:
        print(json.dumps({"error": "SESSION_HTTP_UNAVAILABLE"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
