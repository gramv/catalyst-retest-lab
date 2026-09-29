"""``GET /api/v1/lab/research-context``, with an offline development fallback.

The real endpoint (``catalyst_lab.research_context``, ``docs/API-CONTRACT.md``) needs
the agent's own bearer token and a running app; ``fetch_context`` calls it exactly that
way. For development without the app running, ``offline_universe`` reproduces the
universe research3/alpaca_probe.py used on 2026-09-27: Alpaca's public latest-crypto-
quotes endpoint (no key), kept only when the quote is fresh (at most an hour old) and
the coin is not a stablecoin. Its result is clearly *not* a real research context — it
has no schedule, report format or open trades/outcomes — so ``build`` can tell the two
apart (``is_offline``) and refuses to produce a submittable report from the fallback.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx

from research_agent.submit import BaseUrlRefused, checked_base_url

CONTEXT_ROUTE = "/api/v1/lab/research-context"
CONTEXT_VERSION = "RESEARCH_CONTEXT_V1"
# V2 (package learning-app, 2026-09-28) is every V1 field unchanged plus ``lessons``.
CONTEXT_VERSIONS = frozenset({CONTEXT_VERSION, "RESEARCH_CONTEXT_V2"})
OFFLINE_CONTEXT_VERSION = "RESEARCH_AGENT_OFFLINE_CONTEXT_V1"
ALPACA_QUOTES_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
# Owner list (2026-09-26, research_context.py): USD-pegged and euro stablecoins are
# never picks.
STABLECOINS = frozenset({"USDC", "USDT", "USDG", "DAI", "PYUSD", "USDP", "TUSD", "FDUSD", "EURC"})
FRESH_QUOTE_SECONDS = 3600
# The base-asset part of Alpaca's crypto symbol pattern (its 400 message: ^[A-Z]+x?/[A-Z]+$).
ALPACA_SYMBOL_BASE = re.compile(r"[A-Z]+x?")

# The candidate list research3/alpaca_probe.py probed on 2026-09-27. Only a starting
# point for the offline fallback: the real context's own tradable-asset list is
# authoritative whenever the app is reachable.
DEFAULT_CANDIDATES = tuple(sorted(set("""
BTC ETH SOL AVAX DOT XTZ XRP LTC BCH DOGE SHIB PEPE TRUMP UNI AAVE CRV SUSHI YFI MKR LINK GRT
ADA XLM HBAR ALGO ETC ATOM NEAR APT SUI SEI TIA INJ FET RENDER RNDR ARB OP POL MATIC FIL LDO
ONDO HYPE SKY BONK WIF TON TRX BAT ENA JUP PYTH W AXL IMX SAND MANA APE CHZ COMP SNX 1INCH
QNT KSM ZEC DASH XMR ICP STX KAVA ROSE FLOW EGLD THETA VET GALA ENS BLUR WLD STRK ZRO JTO
TAO KAS BNB CRO LEO OKB PENGU FARTCOIN VIRTUAL SPX POPCAT MEW GIGA MOODENG PNUT ACT GOAT
XYO PAXG HNT ORCA RAY JASMY AUDIO AMP ANKR CELO CTSI DYDX EIGEN ETHFI ENJ GRASS IO KAITO LPT
MASK MINA MORPHO NOT OM PRIME RSR SAFE SUPER SYRUP TRB TURBO UMA USUAL WOO ZK ZRX
""".split())))


class ContextError(Exception):
    """A context read failed. Callers must fail closed: never fabricate a context."""


def fetch_context(base_url, token, *, client=None, timeout=30.0):
    """GET the real research context with the agent's own bearer token.

    ``client`` is an injectable ``httpx.Client`` (tests use one on ``httpx.MockTransport``;
    a real client is opened and closed when omitted). Raises ``ContextError`` for any
    non-200 response or unexpected body; never returns anything but the parsed JSON of
    an actual ``RESEARCH_CONTEXT_V1`` or ``RESEARCH_CONTEXT_V2`` response.
    """
    try:
        base_url = checked_base_url(base_url)
    except BaseUrlRefused as exc:
        raise ContextError(str(exc)) from None
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = client.get(
                base_url + CONTEXT_ROUTE,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ContextError(
                f"RESEARCH_CONTEXT_CONNECTION_ERROR: {type(exc).__name__}"
            ) from None
    finally:
        if owns_client:
            client.close()
    if response.status_code != 200:
        raise ContextError(f"RESEARCH_CONTEXT_HTTP_{response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise ContextError("RESEARCH_CONTEXT_INVALID_JSON") from None
    if not isinstance(body, dict) or body.get("context_version") not in CONTEXT_VERSIONS:
        raise ContextError("RESEARCH_CONTEXT_UNEXPECTED_SHAPE")
    return body


NO_LESSONS = "NO_LESSONS"
LESSONS_UNAVAILABLE = "LESSONS_UNAVAILABLE"


def lessons_of(context):
    """The context's ``lessons`` (``RESEARCH_LESSONS_V1``), or ``None``: a V1 context, the
    offline fallback and the status credential's view (``lessons: null``) carry none."""
    lessons = context.get("lessons") if isinstance(context, dict) else None
    return lessons if isinstance(lessons, dict) else None


def usable_lessons(context):
    """``(lessons, None)`` when the context carries lessons this run can use, else ``(None,
    code)``: ``NO_LESSONS`` without any (see ``lessons_of``), or the app's own code
    (``LESSONS_UNAVAILABLE``) when it served ``available: false``, a learning record it
    could not read (package learning-app, 65b5e17). Lessons without ``available`` predate
    that change and count as available."""
    lessons = lessons_of(context)
    if lessons is None:
        return None, NO_LESSONS
    if lessons.get("available") is False:
        return None, str(lessons.get("code") or LESSONS_UNAVAILABLE)
    return lessons, None


def _quote_time(quote):
    value = quote.get("t") if isinstance(quote, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def offline_universe(*, client=None, candidates=DEFAULT_CANDIDATES, now=None, timeout=30.0):
    """Alpaca's public latest crypto quotes (no key), fresh (<=1h) and non-stablecoin
    only — the offline development fallback for ``context``, not a real research
    context. Raises ``ContextError`` on any transport or parsing failure."""
    now = now or datetime.now(UTC)
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    # Alpaca refuses the whole request (HTTP 400) when one symbol does not match its own
    # pattern ^[A-Z]+x?/[A-Z]+$ (for example 1INCH/USD), so such names are left out here.
    symbols = ",".join(f"{coin}/USD" for coin in candidates
                       if coin not in STABLECOINS and ALPACA_SYMBOL_BASE.fullmatch(coin))
    try:
        try:
            response = client.get(ALPACA_QUOTES_URL, params={"symbols": symbols},
                                  headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ContextError(
                f"ALPACA_QUOTES_CONNECTION_ERROR: {type(exc).__name__}"
            ) from None
    finally:
        if owns_client:
            client.close()
    if response.status_code != 200:
        raise ContextError(f"ALPACA_QUOTES_HTTP_{response.status_code}")
    try:
        quotes = (response.json() or {}).get("quotes") or {}
    except ValueError:
        raise ContextError("ALPACA_QUOTES_INVALID_JSON") from None
    universe, excluded = {}, {}
    for symbol, quote in sorted(quotes.items()):
        base = symbol.split("/")[0]
        if base in STABLECOINS:
            continue
        at = _quote_time(quote)
        if at is None:
            excluded[symbol] = "UNPARSEABLE_QUOTE_TIME"
            continue
        age = (now - at).total_seconds()
        if age > FRESH_QUOTE_SECONDS:
            excluded[symbol] = f"quote is {age / 3600:.1f} hours old"
            continue
        try:
            bid, ask = Decimal(str(quote["bp"])), Decimal(str(quote["ap"]))
        except (KeyError, InvalidOperation, TypeError):
            excluded[symbol] = "UNPARSEABLE_QUOTE_PRICE"
            continue
        universe[symbol] = {"bid": bid, "ask": ask, "quote_at": at.isoformat()}
    return {
        "context_version": OFFLINE_CONTEXT_VERSION,
        "as_of": now.isoformat(),
        "universe": universe,
        "excluded": excluded,
    }


# --- Shape-agnostic accessors (real RESEARCH_CONTEXT_V1 or the offline fallback) -------------

def is_offline(context):
    return context.get("context_version") == OFFLINE_CONTEXT_VERSION


def _decimal_or_none(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def coin_symbols(context):
    """Every tradable symbol ("SOL/USD") the context lists, either shape."""
    if is_offline(context):
        return sorted(context.get("universe") or {})
    coins = (context.get("universe") or {}).get("coins") or []
    return [coin["symbol"] for coin in coins if isinstance(coin, dict) and coin.get("symbol")]


def coin_quote(context, symbol):
    """``{"bid", "ask", "quote_at", "price_increment"}`` for ``symbol`` (Decimals, or
    ``None`` fields when unavailable), or ``None`` when the context does not list it."""
    if is_offline(context):
        row = (context.get("universe") or {}).get(symbol)
        if row is None:
            return None
        return {"bid": _decimal_or_none(row.get("bid")), "ask": _decimal_or_none(row.get("ask")),
                "quote_at": row.get("quote_at"), "price_increment": None}
    for coin in (context.get("universe") or {}).get("coins") or []:
        if coin.get("symbol") == symbol:
            return {
                "bid": _decimal_or_none(coin.get("bid")), "ask": _decimal_or_none(coin.get("ask")),
                "quote_at": coin.get("quote_at"),
                "price_increment": _decimal_or_none(coin.get("price_increment")),
            }
    return None


def mid_price(quote):
    """The mid of a ``coin_quote(...)`` result, or ``None`` without both sides."""
    if not quote or quote.get("bid") is None or quote.get("ask") is None:
        return None
    return (quote["bid"] + quote["ask"]) / 2
