"""Derivatives context for the daily run's picks (``docs/RESEARCH-LOOP-V2.md`` 3.6, package
research-loop-kit).

For every coin with a level setup, two free public sources, both reachable from the Mac
(Binance and Bybit refuse US connections):

* OKX's open-interest history for the coin's USDT perpetual swap, ``<COIN>-USDT-SWAP``:
  ``GET /api/v5/rubik/stat/contracts/open-interest-history`` in 15-minute rows
  ``[ts, oi, oiCcy, oiUsd]`` (milliseconds; contracts, coins and US dollars). A coin counts
  as having a swap only when OKX's own instrument list (``GET /api/v5/public/instruments``,
  ``instType=SWAP``) shows that exact ID live.
* Hyperliquid's funding history for the coin's perpetual: ``POST /info``, ``fundingHistory``.
  Hyperliquid pays funding every hour, so each entry's ``fundingRate`` is an hourly rate. The
  perpetual's name comes from Hyperliquid's own ``meta`` listing: the coin itself, or its
  thousand-unit contract (``kPEPE`` for 1,000 PEPE) when that is how Hyperliquid lists it.

The measures are the open-interest change over 4 and 24 hours in the coin's own units
(``oiCcy``, so a price move alone does not change it) and the latest hourly funding.
``build --derivatives`` adds them to each pick as cited sources, every figure exactly as
fetched: crowded long positioning (the latest funding at or above 0.01% per hour while open
interest rose over 24 hours) as a ``RISK`` claim; anything else as one sentence of the
thesis. The same-day study of 2026-09-28 found that open interest and funding did not flag
movers before they moved, so this is context for Jev only: nothing here chooses a coin, orders
the picks or sets a level, and a coin without an OKX swap or a Hyperliquid perpetual is left
out, never guessed.

Only ``fetch_derivatives`` touches the network or a clock; every other function is pure.
Both calls are public data reads with no key or account (the Hyperliquid read is a POST
because ``/info`` only answers POSTs); nothing here writes anywhere.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

import httpx

SCHEMA = "RESEARCH_AGENT_DERIVATIVES_V1"
OKX_BASE = "https://www.okx.com"
OKX_INSTRUMENTS_URL = OKX_BASE + "/api/v5/public/instruments"
# Cited without its query (instId, period, limit): the app's source model accepts only a
# query-free https URL, and the excerpt names the instrument and period instead, as the
# kit's technical evidence cites Coinbase's candles endpoint.
OKX_OPEN_INTEREST_URL = OKX_BASE + "/api/v5/rubik/stat/contracts/open-interest-history"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
OI_PERIOD = "15m"
OI_PERIOD_MS = 15 * 60 * 1000
OI_LIMIT = 100  # 100 rows of 15 minutes: 25 hours, enough for the 24-hour change.
HOUR_MS = 3600 * 1000
FUNDING_LOOKBACK = timedelta(hours=6)
# OKX's rubik statistics allow 5 requests per 2 seconds; Hyperliquid weighs each info
# request (1,200 per minute).
OKX_SLEEP_SECONDS = 0.45
HYPERLIQUID_SLEEP_SECONDS = 0.2
# Crowded long positioning: the latest hourly funding at or above 0.01% (eight times the
# 0.00125% per hour baseline) while open interest in coin terms rose over 24 hours.
CROWDED_FUNDING_RATE = Decimal("0.0001")
CROWDED_RULE = ("the latest Hyperliquid hourly funding at or above 0.01% (0.0001) while OKX "
                "open interest in coin terms (oiCcy) rose over 24 hours")
CROWDED_LABEL = "Crowded longs: funding at or above 0.01% per hour, open interest rising"
MAX_SOURCES = 8  # catalyst_lab.research_report_v3.AgentPick.sources
MAX_CLAIMS = 8  # catalyst_lab.muse_reports.SelectionRationale.claims
MAX_KNOWN_RISKS = 5
MAX_CLAIM_CHARS = 300
MAX_THESIS_CHARS = 1000
MAX_WHY_NOW_CHARS = 600
MAX_RISKS_CHARS = 600
PERCENT = Decimal("0.01")


class DerivativesError(Exception):
    """A sanitized, code-like failure; never a response body, header or full URL."""


class DerivativesFormatError(Exception):
    """``derivatives.json`` is not the file ``derivatives`` writes; the message names where."""


def _when(moment):
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _utc_ms(value):
    return datetime.fromtimestamp(value / 1000, UTC)


def _plain(value):
    """A Decimal as a fixed-point string, as ``build.plain`` writes prices."""
    value = Decimal(value)
    return (format(value.normalize(), "f") if value != value.to_integral()
            else format(value.quantize(Decimal(1)), "f"))


# --- The network layer ------------------------------------------------------------------------

def _json(response, provider):
    if response.status_code != 200:
        raise DerivativesError(f"{provider}_HTTP_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise DerivativesError(f"{provider}_INVALID_JSON") from None


def _okx_get(client, url, params):
    try:
        response = client.get(url, params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise DerivativesError(f"OKX_CONNECTION_ERROR: {type(exc).__name__}") from None
    body = _json(response, "OKX")
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise DerivativesError("OKX_UNEXPECTED_SHAPE")
    if str(body.get("code")) != "0":
        code = str(body.get("code"))
        raise DerivativesError(f"OKX_CODE_{code if code.isdigit() else 'UNKNOWN'}")
    return body["data"]


def _hyperliquid(client, payload):
    try:
        response = client.post(HYPERLIQUID_INFO_URL, json=payload,
                               headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise DerivativesError(f"HYPERLIQUID_CONNECTION_ERROR: {type(exc).__name__}") from None
    return _json(response, "HYPERLIQUID")


def okx_swaps(data):
    """The live instrument IDs of OKX's ``instType=SWAP`` listing."""
    return frozenset(row["instId"] for row in data
                     if isinstance(row, dict) and isinstance(row.get("instId"), str)
                     and row.get("state") == "live")


def hyperliquid_names(meta):
    """Hyperliquid's perpetual names from ``meta``, delisted ones left out."""
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list):
        raise DerivativesError("HYPERLIQUID_UNEXPECTED_SHAPE")
    return frozenset(row["name"] for row in universe
                     if isinstance(row, dict) and isinstance(row.get("name"), str)
                     and not row.get("isDelisted"))


def hyperliquid_name(coin, names):
    """The coin's perpetual on Hyperliquid: the coin itself, or its thousand-unit contract
    (``k`` + coin, Hyperliquid's own naming) when only that is listed; else ``None``."""
    if coin in names:
        return coin
    thousand = "k" + coin
    return thousand if thousand in names else None


def fetch_derivatives(coins, *, client=None, clock=None, timeout=20.0,
                      okx_sleep=OKX_SLEEP_SECONDS, hyperliquid_sleep=HYPERLIQUID_SLEEP_SECONDS):
    """OKX open-interest history and Hyperliquid funding for each of ``coins`` (base tickers).

    ``client`` is an injectable ``httpx.Client`` (tests use ``httpx.MockTransport``; a real
    client is opened and closed when omitted). ``clock`` returns the instant each answer was
    received, recorded as that source's ``retrieved_at`` (a real read, never an invented
    time). A coin without the market, or whose read failed, is listed under ``omitted`` with
    the reason; one coin's failure never stops the others.
    """
    clock = clock or (lambda: datetime.now(UTC))
    started = clock()
    result = {}
    if not coins:  # No setup, no picks: nothing to read.
        return {"schema": SCHEMA, "fetched_at": started.isoformat(),
                "crowded_rule": CROWDED_RULE, "coins": result}
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        try:
            swaps, okx_problem = okx_swaps(_okx_get(client, OKX_INSTRUMENTS_URL,
                                                    {"instType": "SWAP"})), None
        except DerivativesError as exc:
            swaps, okx_problem = frozenset(), f"OKX_INSTRUMENTS_UNAVAILABLE: {exc}"
        try:
            names, hl_problem = hyperliquid_names(_hyperliquid(client, {"type": "meta"})), None
        except DerivativesError as exc:
            names, hl_problem = frozenset(), f"HYPERLIQUID_META_UNAVAILABLE: {exc}"
        for coin in sorted(set(coins)):
            entry = {"okx": None, "hyperliquid": None, "omitted": {}}
            inst_id = f"{coin}-USDT-SWAP"
            if okx_problem:
                entry["omitted"]["okx"] = okx_problem
            elif inst_id not in swaps:
                entry["omitted"]["okx"] = f"NO_OKX_USDT_SWAP: OKX lists no live {inst_id}"
            else:
                try:
                    rows = _okx_get(client, OKX_OPEN_INTEREST_URL,
                                    {"instId": inst_id, "period": OI_PERIOD,
                                     "limit": str(OI_LIMIT)})
                    entry["okx"] = {"inst_id": inst_id, "period": OI_PERIOD,
                                    "url": OKX_OPEN_INTEREST_URL,
                                    "retrieved_at": clock().isoformat(),
                                    "history": [list(map(str, row[:4])) for row in rows
                                                if isinstance(row, list)]}
                    _okx_rows(entry["okx"]["history"])  # Refused here, not at build time.
                except DerivativesError as exc:
                    entry["okx"], entry["omitted"]["okx"] = None, str(exc)
                time.sleep(okx_sleep)
            name = hyperliquid_name(coin, names)
            if hl_problem:
                entry["omitted"]["hyperliquid"] = hl_problem
            elif name is None:
                entry["omitted"]["hyperliquid"] = (
                    f"NO_HYPERLIQUID_PERPETUAL: Hyperliquid lists neither {coin} nor k{coin}")
            else:
                since = int((clock() - FUNDING_LOOKBACK).timestamp() * 1000)
                try:
                    history = _hyperliquid(client, {"type": "fundingHistory", "coin": name,
                                                    "startTime": since})
                    entry["hyperliquid"] = {"coin": name, "url": HYPERLIQUID_INFO_URL,
                                            "retrieved_at": clock().isoformat(),
                                            "history": history}
                    _funding_rows(history, name)
                except DerivativesError as exc:
                    entry["hyperliquid"], entry["omitted"]["hyperliquid"] = None, str(exc)
                time.sleep(hyperliquid_sleep)
            result[coin] = entry
    finally:
        if owns_client:
            client.close()
    return {"schema": SCHEMA, "fetched_at": started.isoformat(), "crowded_rule": CROWDED_RULE,
            "coins": result}


# --- Pure: parsing and the measures -----------------------------------------------------------

@dataclass(frozen=True)
class OIRow:
    ts: int  # the 15-minute period's start, in milliseconds
    raw: tuple  # the row's four values exactly as OKX returned them: ts, oi, oiCcy, oiUsd
    oi_ccy: Decimal

    @property
    def at(self):
        return _utc_ms(self.ts)


def _okx_rows(history):
    if not isinstance(history, list) or not history:
        raise DerivativesError("OKX_NO_OPEN_INTEREST_ROWS")
    rows = []
    for raw in history:
        if not isinstance(raw, list) or len(raw) < 4:
            raise DerivativesError("OKX_UNEXPECTED_ROW")
        try:
            ts = int(str(raw[0]))
            oi_ccy = Decimal(str(raw[2]))
            Decimal(str(raw[1])), Decimal(str(raw[3]))
        except (InvalidOperation, ValueError):
            raise DerivativesError("OKX_UNEXPECTED_ROW") from None
        if not oi_ccy.is_finite():
            raise DerivativesError("OKX_UNEXPECTED_ROW")
        if oi_ccy <= 0:
            # OKX serves a period it has not computed yet as 0 (2026-10-01: the newest one to
            # three rows of ten swaps). That is no figure: read as a 100% fall it would be false.
            continue
        rows.append(OIRow(ts=ts, raw=tuple(str(value) for value in raw[:4]), oi_ccy=oi_ccy))
    return sorted(rows, key=lambda row: row.ts)


def row_at_or_before(rows, target_ms):
    """The row at ``target_ms``, or the one just before it when that period is missing (at
    most one period earlier); ``None`` when the history does not reach back that far."""
    earlier = [row for row in rows if row.ts <= target_ms]
    if not earlier or target_ms - earlier[-1].ts > OI_PERIOD_MS:
        return None
    return earlier[-1]


def change_pct(latest, earlier):
    """The percent change in open interest (coin terms) from ``earlier`` to ``latest``,
    rounded to 0.01 (half even): the figure the words state and the crowded rule reads."""
    if earlier is None or earlier.oi_ccy <= 0:
        return None
    return ((latest.oi_ccy / earlier.oi_ccy - 1) * 100).quantize(PERCENT, ROUND_HALF_EVEN)


@dataclass(frozen=True)
class OpenInterest:
    inst_id: str
    period: str
    retrieved_at: str
    latest: OIRow
    h4: OIRow | None
    h24: OIRow | None

    @property
    def change_4h(self):
        return change_pct(self.latest, self.h4)

    @property
    def change_24h(self):
        return change_pct(self.latest, self.h24)


@dataclass(frozen=True)
class Funding:
    name: str
    retrieved_at: str
    time_ms: int
    rate: Decimal  # hourly
    rate_text: str  # exactly as Hyperliquid returned it
    premium_text: str | None

    @property
    def at(self):
        return _utc_ms(self.time_ms)

    @property
    def pct_per_hour(self):
        return self.rate * 100


def _funding_rows(history, name):
    if not isinstance(history, list) or not history:
        raise DerivativesError("HYPERLIQUID_NO_FUNDING_ROWS")
    rows = []
    for row in history:
        if not isinstance(row, dict) or row.get("coin") != name:
            raise DerivativesError("HYPERLIQUID_UNEXPECTED_ROW")
        try:
            time_ms = int(row["time"])
            rate = Decimal(str(row["fundingRate"]))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise DerivativesError("HYPERLIQUID_UNEXPECTED_ROW") from None
        if not rate.is_finite():
            raise DerivativesError("HYPERLIQUID_UNEXPECTED_ROW")
        rows.append((time_ms, rate, row))
    return sorted(rows, key=lambda item: item[0])


@dataclass(frozen=True)
class Summary:
    coin: str
    oi: OpenInterest | None
    funding: Funding | None

    @property
    def crowded(self):
        """Crowded long positioning (``CROWDED_RULE``); ``False`` whenever a figure is
        missing: without both, it cannot be shown."""
        if self.oi is None or self.funding is None or self.oi.change_24h is None:
            return False
        return self.funding.rate >= CROWDED_FUNDING_RATE and self.oi.change_24h > 0


def _aware(text, path):
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        raise DerivativesFormatError(f"{path}: not an RFC3339 time") from None
    if moment.tzinfo is None:
        raise DerivativesFormatError(f"{path}: the time has no UTC offset")
    return moment


def summarize(coin, entry):
    """One coin's measures from the rows ``derivatives`` stored (``derivatives.json``), or
    ``DerivativesFormatError`` naming what is wrong. Everything is computed here, from the
    stored rows, never read from a stored result."""
    if not isinstance(entry, dict):
        raise DerivativesFormatError(f"coins.{coin}: must be an object")
    oi = funding = None
    okx = entry.get("okx")
    if okx is not None:
        try:
            rows = _okx_rows(okx.get("history"))
        except (DerivativesError, AttributeError):
            raise DerivativesFormatError(f"coins.{coin}.okx.history: not OKX's rows") from None
        _aware(okx.get("retrieved_at"), f"coins.{coin}.okx.retrieved_at")
        if not isinstance(okx.get("inst_id"), str) or not isinstance(okx.get("period"), str):
            raise DerivativesFormatError(f"coins.{coin}.okx: inst_id and period are required")
        if rows:  # None published (every row 0): the coin has no open-interest figure.
            latest = rows[-1]
            oi = OpenInterest(inst_id=okx["inst_id"], period=okx["period"],
                              retrieved_at=okx["retrieved_at"], latest=latest,
                              h4=row_at_or_before(rows, latest.ts - 4 * HOUR_MS),
                              h24=row_at_or_before(rows, latest.ts - 24 * HOUR_MS))
    hyper = entry.get("hyperliquid")
    if hyper is not None:
        name = hyper.get("coin") if isinstance(hyper, dict) else None
        try:
            rows = _funding_rows(hyper.get("history"), name)
        except (DerivativesError, AttributeError):
            raise DerivativesFormatError(
                f"coins.{coin}.hyperliquid.history: not Hyperliquid's funding rows") from None
        _aware(hyper.get("retrieved_at"), f"coins.{coin}.hyperliquid.retrieved_at")
        time_ms, rate, row = rows[-1]
        premium = row.get("premium")
        funding = Funding(name=name, retrieved_at=hyper["retrieved_at"], time_ms=time_ms,
                          rate=rate, rate_text=str(row["fundingRate"]),
                          premium_text=None if premium is None else str(premium))
    return Summary(coin=coin, oi=oi, funding=funding)


def load(doc):
    """``{coin: Summary}`` from ``derivatives.json``; ``DerivativesFormatError`` otherwise."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise DerivativesFormatError(f"derivatives.json: not a {SCHEMA} file (run "
                                     "'derivatives' to write one)")
    coins = doc.get("coins")
    if not isinstance(coins, dict):
        raise DerivativesFormatError("derivatives.json: 'coins' must be an object")
    return {coin: summarize(coin, entry) for coin, entry in coins.items()}


def omitted(doc):
    """``{coin: {provider: reason}}``: what ``derivatives`` could not read, and why."""
    return {coin: entry.get("omitted") for coin, entry in (doc.get("coins") or {}).items()
            if isinstance(entry, dict) and entry.get("omitted")}


def readable(summary):
    """The measures as plain strings, for ``derivatives.json`` and the printed summary."""
    oi, funding = summary.oi, summary.funding

    def pct(value):
        return None if value is None else str(value)

    return {
        "oi_change_4h_pct": pct(oi.change_4h) if oi else None,
        "oi_change_24h_pct": pct(oi.change_24h) if oi else None,
        "oi_latest_at": oi.latest.at.isoformat() if oi else None,
        "funding_pct_per_hour": _plain(funding.pct_per_hour) if funding else None,
        "funding_at": funding.at.isoformat() if funding else None,
        "crowded": summary.crowded,
    }


# --- Pure: the cited sources and the words -----------------------------------------------------

def _seconds(text):
    """An RFC3339 time to the whole second (sub-seconds dropped, never rounded up): the
    dossier budget is tight, and a second is precise enough for these sources."""
    return datetime.fromisoformat(text).replace(microsecond=0).isoformat()


def oi_source(source_id, oi, coin):
    """The OKX source: open interest in coin terms (``oiCcy``) of the latest row and of the
    rows the 4- and 24-hour changes compare with, each value exactly as OKX returned it.
    ``published_at`` is the latest row's own time. Kept short: the review dossier's budget
    (11,000 bytes) leaves the daily run's picks about 1,000 bytes."""
    rows = [row for row in (oi.latest, oi.h4, oi.h24) if row is not None]
    figures = "; ".join(f"{row.raw[2]} at {_when(row.at)}" for row in rows)
    return {"source_id": source_id, "url": OKX_OPEN_INTEREST_URL,
            "excerpt": f"OKX {oi.inst_id} open interest in {coin} (oiCcy, {oi.period} rows): "
                       f"{figures}.",
            "published_at": _seconds(oi.latest.at.isoformat()),
            "retrieved_at": _seconds(oi.retrieved_at)}


def funding_source(source_id, funding, coin):
    """The Hyperliquid source: the latest funding entry's rate exactly as returned.
    ``published_at`` is the entry's own time."""
    unit = f" (1,000 {coin})" if funding.name != coin else ""
    return {"source_id": source_id, "url": HYPERLIQUID_INFO_URL,
            "excerpt": (f"Hyperliquid {funding.name}{unit} fundingHistory, latest entry: "
                        f"fundingRate {funding.rate_text} at {_when(funding.at)}."),
            "published_at": _seconds(funding.at.isoformat()),
            "retrieved_at": _seconds(funding.retrieved_at)}


def _change_words(value):
    if value > 0:
        return f"rose {value}%"
    if value < 0:
        return f"fell {-value}%"
    return "was unchanged (0.00%)"


def _span_words(latest, earlier):
    """"over 4 hours", or the exact span when the history missed that period's row."""
    minutes = (latest.ts - earlier.ts) // 60_000
    hours, minutes = divmod(minutes, 60)
    return f"over {hours} hours" + (f" {minutes} minutes" if minutes else "")


def _oi_words(oi, coin):
    parts = [f"{_change_words(change)} {_span_words(oi.latest, earlier)}"
             for change, earlier in ((oi.change_4h, oi.h4), (oi.change_24h, oi.h24))
             if change is not None]
    if not parts:
        return f"OKX open interest in {coin} has no row 4 or 24 hours earlier to compare"
    return (f"OKX open interest in {coin} {' and '.join(parts)} (computed from the cited "
            "figures)")


def _funding_words(funding):
    return f"the latest Hyperliquid funding was {_plain(funding.pct_per_hour)}% per hour"


def neutral_sentence(summary, source_ids, *, oi=True, funding=True):
    """The thesis sentence for figures that do not show crowded positioning."""
    parts = []
    if oi and summary.oi is not None:
        parts.append(_oi_words(summary.oi, summary.coin))
    if funding and summary.funding is not None:
        parts.append(_funding_words(summary.funding))
    return f"Derivatives context (sources {', '.join(source_ids)}): {'; '.join(parts)}."


def crowded_claim(summary):
    """The RISK claim's text for crowded long positioning; the 4-hour change is left out
    when the whole would pass the claim limit."""
    oi, funding = summary.oi, summary.funding
    head = (f"Crowded long positioning: Hyperliquid funding was {_plain(funding.pct_per_hour)}% "
            f"per hour at {_when(funding.at)} (at or above 0.01%) while OKX open interest in "
            f"{summary.coin} {_change_words(oi.change_24h)} {_span_words(oi.latest, oi.h24)}")
    tail = " (computed from the cited figures)."
    if oi.change_4h is not None:
        longer = (f"{head} and {_change_words(oi.change_4h)} "
                  f"{_span_words(oi.latest, oi.h4)}{tail}")
        if len(longer) <= MAX_CLAIM_CHARS:
            return longer
    return f"{head}{tail}"


def _next_claim_id(claims):
    taken = {claim.get("claim_id") for claim in claims}
    number = len(claims) + 1
    while f"C{number}" in taken:
        number += 1
    return f"C{number}"


def _with_sources(pick, sources):
    """A copy of ``pick`` citing ``sources`` too, or ``(None, reason)``."""
    for source in sources:
        if (datetime.fromisoformat(source["published_at"])
                > datetime.fromisoformat(source["retrieved_at"])):
            return None, f"{source['source_id']}: its data time is after its retrieval"
    new = copy.deepcopy(pick)
    taken = {source["source_id"] for source in new.get("sources") or []}
    if taken & {source["source_id"] for source in sources}:
        return None, "the pick already cites a source with the same ID"
    if len(new.get("sources") or []) + len(sources) > MAX_SOURCES:
        return None, f"no room for {len(sources)} more sources ({MAX_SOURCES} at most)"
    new.setdefault("sources", []).extend(sources)
    return new, None


def _crowded(pick, summary, sources, *, label):
    new, reason = _with_sources(pick, sources)
    if new is None:
        return None, reason
    ids = [source["source_id"] for source in sources]
    rationale, reasoning, text = new["selection_rationale"], new["reasoning"], \
        crowded_claim(summary)
    if len(rationale["claims"]) < MAX_CLAIMS:
        rationale["claims"].append({"claim_id": _next_claim_id(rationale["claims"]),
                                    "kind": "RISK", "text": text,
                                    "supported_by": {"source_ids": ids, "bar_ids": []}})
        if label and len(rationale["known_risks"]) < MAX_KNOWN_RISKS:
            rationale["known_risks"].append(CROWDED_LABEL)
        return new, {"placed": "RISK_CLAIM", "crowded": True, "source_ids": ids}
    risks = f"{reasoning['risks']} {text} (sources {', '.join(ids)})"
    if len(risks) <= MAX_RISKS_CHARS:  # Eight claims already: the risks text says it.
        reasoning["risks"] = risks
        return new, {"placed": "RISKS_TEXT", "crowded": True, "source_ids": ids}
    return None, "no room for the crowded-positioning claim or its risks sentence"


def _neutral(pick, summary, sources, *, oi, funding):
    new, reason = _with_sources(pick, sources)
    if new is None:
        return None, reason
    ids = [source["source_id"] for source in sources]
    sentence = neutral_sentence(summary, ids, oi=oi, funding=funding)
    reasoning = new["reasoning"]
    for field, limit, placed in (("thesis", MAX_THESIS_CHARS, "THESIS_TEXT"),
                                 ("why_now", MAX_WHY_NOW_CHARS, "WHY_NOW_TEXT")):
        text = f"{reasoning[field]} {sentence}"
        if len(text) <= limit:
            reasoning[field] = text
            return new, {"placed": placed, "crowded": False, "source_ids": ids}
    return None, "no room for the derivatives sentence in the thesis or why_now"


def variants(pick, summary):
    """``(candidates, reasons)``: the pick with its derivatives context, richest first, for
    ``build`` to take the first the app's own models accept. The pick is copied, never
    changed in place; its symbol, kind, levels, bars and every other field stay as built.

    Crowded positioning is only ever the RISK claim citing both sources (with, then without,
    its ``known_risks`` label): it is never reduced to half of its figures. Otherwise both
    sources and the sentence; then open interest alone; then funding alone (the dossier
    budget can leave room for one source and not two)."""
    key = summary.coin.lower()
    oi = oi_source(f"{key}-oi", summary.oi, summary.coin) if summary.oi else None
    funding = (funding_source(f"{key}-funding", summary.funding, summary.coin)
               if summary.funding else None)
    if oi is None and funding is None:
        return [], ["no OKX or Hyperliquid figures for this coin"]
    if summary.crowded:
        attempts = [lambda label=label: _crowded(pick, summary, [oi, funding], label=label)
                    for label in (True, False)]
    else:
        attempts = []
        if oi and funding:
            attempts.append(lambda: _neutral(pick, summary, [oi, funding], oi=True,
                                             funding=True))
        if oi:
            attempts.append(lambda: _neutral(pick, summary, [oi], oi=True, funding=False))
        if funding:
            attempts.append(lambda: _neutral(pick, summary, [funding], oi=False, funding=True))
    candidates, reasons = [], []
    for attempt in attempts:
        new, info = attempt()
        if new is None:
            reasons.append(info)
        else:
            candidates.append((new, info))
    return candidates, reasons
