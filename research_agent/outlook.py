"""The morning outlook (plan section 5c): a worksheet with one entry per universe coin, and
the ``MARKET_OUTLOOK_V1`` it becomes (``docs/API-CONTRACT.md``, "Learning loop").

``build_worksheet`` writes ``outlook.json`` with the facts the kit already has for every
coin the context lists: price, the day's level setup and its distances, recent return and
volume and the technical state, the ``news.json`` items cited for it, and the matching
entries of the research checklist. Direction, confidence and reasons are left for the
research session. Rebuilding keeps every answer already filled and adds or drops coins as
the universe changed, so it is safe to run again.

``check_worksheet`` refuses what the app would refuse, plus the kit's own rules:

* every universe coin needs an entry: UP, DOWN or FLAT with a confidence, or SKIPPED with
  a reason. A coin left unfilled refuses the whole outlook (the app refuses an outlook
  missing a coin: ``OUTLOOK_COINS_INCOMPLETE``);
* a NEWS or EVENT reason must cite its source (the app makes sources optional; the kit
  does not, so a news claim is never sent uncited);
* the agent's ID may not appear in agent-written text (``AGENT_IDENTITY_IN_OUTLOOK``).

Every cited source is re-fetched before sending (``sources.check_sources``): the excerpt
must verify exactly and a ``published_at`` must be one of the page's own metadata times.
The outlook is graded on the 24 hours after the app receives it, so it is sent as soon as
the morning's research is done.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from catalyst_lab.research_report_v3 import MAX_PICKS, MAX_SKIPPED
from research_agent import context as context_module
from research_agent import lessons, levels, technicals

WORKSHEET_SCHEMA = "RESEARCH_AGENT_OUTLOOK_WORKSHEET_V1"
OUTLOOK_SCHEMA = "MARKET_OUTLOOK_V1"
HORIZON_HOURS = 24
DIRECTIONS = ("UP", "DOWN", "FLAT")
SKIPPED = "SKIPPED"
REASON_KINDS = ("NEWS", "EVENT", "TECHNICAL", "FUNDAMENTAL", "MARKET")
CITED_KINDS = frozenset({"NEWS", "EVENT"})
MAX_FACTORS, MAX_EVENTS, MAX_REASONS = 12, 20, 4
# The app's limits (package learning-app, 99d961d): the most coins one report can name, and
# a body that 230 cited coins fit in. tests/test_research_agent_outlook.py compares them with
# catalyst_lab.learning_intake once both packages are merged.
MAX_COINS = MAX_PICKS + MAX_SKIPPED
MAX_BODY_BYTES = 2_097_152
LIMITS = {"summary": 600, "factor_name": 80, "factor_note": 300, "event_what": 300,
          "skip_reason": 200, "reason_text": 200}
CONFIDENCE_PLACES, EXPECTED_MOVE_PLACES = 10, 4
SOURCE_KEYS = frozenset({"url", "excerpt", "published_at"})
COIN_ANSWERS = ("direction", "confidence", "expected_move_pct", "skip_reason", "reasons")
MARKET_ANSWERS = ("summary", "btc", "eth", "factors", "events")
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
HOW_TO_FILL = [
    "For every coin: direction UP, DOWN or FLAT over the 24 hours after this outlook is "
    "received, with a confidence from 0 to 1; or SKIPPED with a skip_reason (1-200 "
    "characters) and nothing else.",
    "expected_move_pct is optional: the expected size of the move in percent.",
    "reasons: at most 4, each {kind, text, source}; kind NEWS, EVENT, TECHNICAL, FUNDAMENTAL "
    "or MARKET; text 1-200 characters. NEWS and EVENT reasons need a source.",
    "A source is {url, excerpt, published_at}: a public https page, the excerpt cut exactly "
    "as printed, published_at from the page's own metadata or null. Never invent a time. "
    "The kit re-fetches every source and adds source_id and retrieved_at.",
    "market: summary (1-600 characters); btc and eth {direction UP/DOWN/FLAT, confidence}; "
    "at most 12 factors {name, note, source} and 20 events {at, what, source}.",
    "The facts are the kit's, for reading; only the answer fields are sent.",
]


class OutlookError(Exception):
    """A refused worksheet input; the message starts with a code."""


# --- Building the worksheet --------------------------------------------------------------------

def _coin_row(ctx, symbol):
    for row in (ctx.get("universe") or {}).get("coins") or []:
        if isinstance(row, dict) and row.get("symbol") == symbol:
            return row
    return {}


def _news_items(news_doc, coin):
    doc = ((news_doc or {}).get("coins") or {}).get(coin) or {}
    items = []
    for bucket in ("catalysts", "risks", "fundamentals"):
        for item in doc.get(bucket) or []:
            if isinstance(item, dict):
                source = item.get("source") or {}
                items.append({"bucket": bucket, "kind": item.get("kind"),
                              "claim": item.get("claim"), "url": source.get("url"),
                              "excerpt": source.get("excerpt"),
                              "published_at": source.get("published_at")})
    return items


def _setup_facts(row, mid):
    if not row or not row.get("setup"):
        tried = (row or {}).get("tried") or []
        return None, (tried[-1] if tried else "no level data for this coin")
    setup = levels.setup_from_json(row["setup"])
    pct = technicals.pct_text
    return {
        "rule": setup.rule, "timeframe": setup.timeframe, "window": setup.window,
        "entry": technicals.plain(setup.entry), "max_entry": technicals.plain(setup.max_entry),
        "stop": technicals.plain(setup.stop), "target": technicals.plain(setup.target),
        "reward_risk": pct(setup.reward_risk),
        "distance_to_entry_pct": pct(abs(mid - setup.entry) / mid * 100) if mid else None,
        "distance_bucket": lessons.distance_bucket(mid, setup.entry) if mid else None,
        "stop_below_price_pct": pct((mid - setup.stop) / mid * 100) if mid else None,
        "target_above_price_pct": pct((setup.target - mid) / mid * 100) if mid else None,
    }, None


def _state(raw_coin, as_of, retrieved_at):
    if not raw_coin:
        return {"tags": [], "unavailable": "NO_COINBASE_MARKET_DATA"}
    bars = technicals.complete_hourly_bars(raw_coin.get("candles_1h") or [],
                                           retrieved_at=retrieved_at)
    return technicals.technical_state(bars, as_of=as_of)


def _checklist_parts(checklist_doc):
    items = (checklist_doc or {}).get("items") or []
    technical = {item["key"]: item for item in items if item.get("kind") == "TECHNICAL"}
    general = [{key: item.get(key) for key in ("id", "kind", "description", "measure", "value")}
               for item in items if item.get("kind") != "TECHNICAL"]
    return technical, general


def coin_facts(symbol, *, ctx, market_data, levels_doc, news_doc, technical_items):
    coin = symbol.split("/")[0]
    quote = context_module.coin_quote(ctx, symbol)
    mid = context_module.mid_price(quote) if quote else None
    row = _coin_row(ctx, symbol)
    retrieved_at = datetime.fromisoformat(market_data["retrieved_at"])
    state = _state((market_data.get("coinbase") or {}).get(coin),
                   technicals.floor_hour(retrieved_at.astimezone(UTC)), retrieved_at)
    setup, setup_note = _setup_facts((levels_doc or {}).get(coin), mid)
    return {
        "mid": technicals.plain(mid) if mid is not None else None,
        "quote_at": (quote or {}).get("quote_at"),
        "spread_bps": row.get("spread_bps"),
        "volume_24h_usd": (row.get("volume_24h") or {}).get("usd"),
        "technical": state,
        "setup": setup,
        "setup_note": setup_note,
        "news": _news_items(news_doc, coin),
        "checklist_matches": [
            {key: technical_items[tag].get(key) for key in ("id", "description", "value")}
            for tag in state.get("tags") or [] if tag in technical_items],
    }


def _empty_coin(symbol, facts):
    return {"symbol": symbol, "facts": facts, "direction": None, "confidence": None,
            "expected_move_pct": None, "skip_reason": None, "reasons": []}


def _empty_market(facts):
    return {"facts": facts, "summary": "", "btc": {"direction": None, "confidence": None},
            "eth": {"direction": None, "confidence": None}, "factors": [], "events": []}


def build_worksheet(*, ctx, market_data, levels_doc, news_doc=None, checklist_doc=None,
                    previous=None, now):
    """``outlook.json``: one entry per coin of ``ctx``'s universe, facts filled in, the
    answers empty, except those already given in ``previous`` (an earlier worksheet)."""
    symbols = context_module.coin_symbols(ctx)
    if not symbols:
        raise OutlookError("OUTLOOK_NO_UNIVERSE: the context lists no coins")
    technical_items, general_items = _checklist_parts(checklist_doc)
    retrieved_at = datetime.fromisoformat(market_data["retrieved_at"])
    as_of = technicals.floor_hour(retrieved_at.astimezone(UTC))
    market_facts = {name.lower(): _state((market_data.get("coinbase") or {}).get(name),
                                         as_of, retrieved_at) for name in ("BTC", "ETH")}
    kept = {row.get("symbol"): row for row in ((previous or {}).get("coins") or [])
            if isinstance(row, dict)}
    coins = []
    for symbol in symbols:
        entry = _empty_coin(symbol, coin_facts(
            symbol, ctx=ctx, market_data=market_data, levels_doc=levels_doc,
            news_doc=news_doc, technical_items=technical_items))
        for key in COIN_ANSWERS:
            if symbol in kept and key in kept[symbol]:
                entry[key] = kept[symbol][key]
        coins.append(entry)
    market = _empty_market(market_facts)
    for key in MARKET_ANSWERS:
        if key in ((previous or {}).get("market") or {}):
            market[key] = previous["market"][key]
    return {
        "schema": WORKSHEET_SCHEMA, "contract": OUTLOOK_SCHEMA,
        "outlook_id": (previous or {}).get("outlook_id") or str(uuid4()),
        "built_at": now.astimezone(UTC).isoformat(),
        "context_as_of": ctx.get("as_of"),
        "market_retrieved_at": market_data["retrieved_at"],
        "horizon_hours": HORIZON_HOURS,
        "how_to_fill": HOW_TO_FILL,
        "checklist": {"use": (checklist_doc or {}).get("use"),
                      "check_for_every_coin": general_items,
                      "technical_items": sorted(technical_items)} if checklist_doc else None,
        "market": market,
        "coins": coins,
        "dropped_since_last_build": sorted(set(kept) - set(symbols)),
    }


# --- Checking a filled worksheet (no network) --------------------------------------------------

def _decimal(value, *, low, high, places):
    """``value`` as a Decimal within [low, high] with at most ``places`` decimals, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or number < low or number > high:
        return None
    if -number.normalize().as_tuple().exponent > places:
        return None
    return number


def _text(value, limit):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else None


def _identity_word(agent_id):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(agent_id) + r"(?![A-Za-z0-9_])",
                      re.IGNORECASE)


class _Problems:
    def __init__(self, agent_id):
        self.items, self.word = [], _identity_word(agent_id) if agent_id else None

    def add(self, path, code):
        self.items.append(f"{path}: {code}")

    def agent_text(self, path, value):
        if self.word and isinstance(value, str) and self.word.search(value):
            self.add(path, "AGENT_IDENTITY_IN_OUTLOOK (the agent's ID in text it wrote)")


def _check_source(problems, path, source, *, required):
    if source is None:
        if required:
            problems.add(path, "SOURCE_REQUIRED (NEWS and EVENT reasons cite their source)")
        return
    if not isinstance(source, dict) or not set(source) <= SOURCE_KEYS or not {
            "url", "excerpt"} <= set(source):
        problems.add(path, "SOURCE_INVALID: {url, excerpt, published_at} only")
        return
    if not isinstance(source["url"], str) or not source["url"].startswith("https://"):
        problems.add(f"{path}.url", "PUBLIC_HTTPS_SOURCE_REQUIRED")
    if not isinstance(source["excerpt"], str) or not source["excerpt"].strip():
        problems.add(f"{path}.excerpt", "SOURCE_EXCERPT_REQUIRED")
    published = source.get("published_at")
    if published is not None and not (isinstance(published, str)
                                      and RFC3339.fullmatch(published)):
        problems.add(f"{path}.published_at", "RFC3339_TIMESTAMP_OR_NULL_REQUIRED")


def _check_view(problems, path, view):
    view = view if isinstance(view, dict) else {}
    if view.get("direction") not in DIRECTIONS:
        problems.add(f"{path}.direction", "UP, DOWN or FLAT required")
    if _decimal(view.get("confidence"), low=0, high=1, places=CONFIDENCE_PLACES) is None:
        problems.add(f"{path}.confidence", "a decimal from 0 to 1 (at most 10 places) required")


def _check_market(problems, market):
    market = market if isinstance(market, dict) else {}
    if _text(market.get("summary"), LIMITS["summary"]) is None:
        problems.add("market.summary", f"1-{LIMITS['summary']} characters required")
    problems.agent_text("market.summary", market.get("summary"))
    _check_view(problems, "market.btc", market.get("btc"))
    _check_view(problems, "market.eth", market.get("eth"))
    factors, events = market.get("factors") or [], market.get("events") or []
    if not isinstance(factors, list) or len(factors) > MAX_FACTORS:
        problems.add("market.factors", f"at most {MAX_FACTORS} factors")
        factors = []
    for index, factor in enumerate(factors):
        path, factor = f"market.factors[{index}]", factor if isinstance(factor, dict) else {}
        if _text(factor.get("name"), LIMITS["factor_name"]) is None:
            problems.add(f"{path}.name", f"1-{LIMITS['factor_name']} characters required")
        if _text(factor.get("note"), LIMITS["factor_note"]) is None:
            problems.add(f"{path}.note", f"1-{LIMITS['factor_note']} characters required")
        problems.agent_text(f"{path}.name", factor.get("name"))
        problems.agent_text(f"{path}.note", factor.get("note"))
        _check_source(problems, f"{path}.source", factor.get("source"), required=False)
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        problems.add("market.events", f"at most {MAX_EVENTS} events")
        events = []
    for index, event in enumerate(events):
        path, event = f"market.events[{index}]", event if isinstance(event, dict) else {}
        at = event.get("at")
        if at is not None and not (isinstance(at, str) and RFC3339.fullmatch(at)):
            problems.add(f"{path}.at", "RFC3339_TIMESTAMP_OR_NULL_REQUIRED")
        if _text(event.get("what"), LIMITS["event_what"]) is None:
            problems.add(f"{path}.what", f"1-{LIMITS['event_what']} characters required")
        problems.agent_text(f"{path}.what", event.get("what"))
        _check_source(problems, f"{path}.source", event.get("source"), required=False)


def _check_coin(problems, index, entry):
    path, direction = f"coins[{index}] {entry.get('symbol')}", entry.get("direction")
    if direction == SKIPPED:
        if _text(entry.get("skip_reason"), LIMITS["skip_reason"]) is None:
            problems.add(f"{path}.skip_reason", "SKIP_REASON_REQUIRED (1-200 characters)")
        if (entry.get("confidence") is not None or entry.get("expected_move_pct") is not None
                or entry.get("reasons")):
            problems.add(path, "SKIPPED_COIN_FIELDS_NOT_ALLOWED (no confidence, expected move "
                               "or reasons on a skipped coin)")
        problems.agent_text(f"{path}.skip_reason", entry.get("skip_reason"))
        return
    if direction not in DIRECTIONS:
        problems.add(f"{path}.direction", "UP, DOWN, FLAT or SKIPPED required")
        return
    if _decimal(entry.get("confidence"), low=0, high=1, places=CONFIDENCE_PLACES) is None:
        problems.add(f"{path}.confidence", "CONFIDENCE_REQUIRED (0 to 1, at most 10 places)")
    if entry.get("skip_reason") is not None:
        problems.add(f"{path}.skip_reason", "SKIP_REASON_NOT_ALLOWED")
    move = entry.get("expected_move_pct")
    if move is not None and _decimal(move, low=0, high=100, places=EXPECTED_MOVE_PLACES) is None:
        problems.add(f"{path}.expected_move_pct", "0 to 100, at most 4 places, or null")
    reasons = entry.get("reasons") or []
    if not isinstance(reasons, list) or len(reasons) > MAX_REASONS:
        problems.add(f"{path}.reasons", f"at most {MAX_REASONS} reasons")
        return
    for number, reason in enumerate(reasons):
        where, reason = f"{path}.reasons[{number}]", reason if isinstance(reason, dict) else {}
        if not set(reason) <= {"kind", "text", "source"}:
            problems.add(where, "only kind, text and source")
        if reason.get("kind") not in REASON_KINDS:
            problems.add(f"{where}.kind", f"one of {list(REASON_KINDS)}")
        if _text(reason.get("text"), LIMITS["reason_text"]) is None:
            problems.add(f"{where}.text", f"1-{LIMITS['reason_text']} characters required")
        problems.agent_text(f"{where}.text", reason.get("text"))
        _check_source(problems, f"{where}.source", reason.get("source"),
                      required=reason.get("kind") in CITED_KINDS)


def check_worksheet(worksheet, *, universe, agent_id):
    """``(problems, notes)`` for a filled worksheet against the ``universe`` it will be sent
    for. Any problem refuses the send; notes (coins that left the universe, dropped from the
    payload) do not."""
    if worksheet.get("schema") != WORKSHEET_SCHEMA:
        return ["outlook.json: not a RESEARCH_AGENT_OUTLOOK_WORKSHEET_V1 worksheet"], []
    problems, notes = _Problems(agent_id), []
    if len(universe) > MAX_COINS:
        problems.add("coins", f"OUTLOOK_TOO_MANY_COINS: the universe lists {len(universe)} coins "
                              f"and the app takes at most {MAX_COINS} entries "
                              "(learning_intake.MAX_OUTLOOK_COINS), so any outlook would be "
                              "refused; tell the owner")
    rows = worksheet.get("coins") if isinstance(worksheet.get("coins"), list) else []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.add(f"coins[{index}]", "must be an object")
    if len(rows) != sum(isinstance(row, dict) for row in rows):
        return problems.items, notes
    entries = rows
    listed = [row.get("symbol") for row in entries]
    duplicates = sorted({symbol for symbol in listed if listed.count(symbol) > 1})
    if duplicates:
        problems.add("coins", f"DUPLICATE_COIN_IN_OUTLOOK: {', '.join(duplicates)}")
    missing = [symbol for symbol in universe if symbol not in listed]
    if missing:
        problems.add("coins", "OUTLOOK_UNIVERSE_CHANGED: no entry for " + ", ".join(missing)
                     + ". The research context read now is saved as context.json: run "
                     "'outlook' again (it keeps every answer already given) and fill them.")
    extra = sorted(set(listed) - set(universe))
    if extra:
        notes.append("left the tradable universe, not sent: " + ", ".join(extra))
    unfilled = [row.get("symbol") for row in entries
                if row.get("symbol") in universe and row.get("direction") is None]
    if unfilled:
        problems.add("coins", f"UNFILLED ({len(unfilled)}): " + ", ".join(unfilled)
                     + ". Give each a direction and confidence, or SKIPPED with a reason.")
    for index, entry in enumerate(entries):
        if entry.get("symbol") in universe and entry.get("direction") is not None:
            _check_coin(problems, index, entry)
    _check_market(problems, worksheet.get("market"))
    return problems.items, notes


# --- The payload -------------------------------------------------------------------------------

def citations(worksheet, universe):
    """``[(path, source)]`` for every source the payload would carry, in payload order."""
    found = []
    market = worksheet.get("market") or {}
    for key in ("factors", "events"):
        for index, item in enumerate(market.get(key) or []):
            if isinstance(item, dict) and item.get("source"):
                found.append((f"market.{key}[{index}]", item["source"]))
    for index, entry in enumerate(worksheet.get("coins") or []):
        if entry.get("symbol") not in universe or entry.get("direction") == SKIPPED:
            continue
        for number, reason in enumerate(entry.get("reasons") or []):
            if isinstance(reason, dict) and reason.get("source"):
                found.append((f"coins[{index}].reasons[{number}]", reason["source"]))
    return found


def _number(value):
    return technicals.plain(Decimal(str(value).strip()))


def _with_source(body, path, verified):
    return {**body, "source": verified[path]} if path in verified else body


def assemble(worksheet, *, universe, verified, agent, run_slot, generated_at):
    """The ``MARKET_OUTLOOK_V1`` body. ``verified`` maps a citation path to its finished
    source object (with ``source_id`` and the re-fetch's ``retrieved_at``)."""
    market = worksheet["market"]
    by_symbol = {row["symbol"]: (index, row) for index, row in enumerate(worksheet["coins"])}
    coins = []
    for symbol in universe:
        index, entry = by_symbol[symbol]
        if entry["direction"] == SKIPPED:
            coins.append({"symbol": symbol, "direction": SKIPPED,
                          "skip_reason": entry["skip_reason"].strip()})
            continue
        body = {"symbol": symbol, "direction": entry["direction"],
                "confidence": _number(entry["confidence"])}
        if entry.get("expected_move_pct") is not None:
            body["expected_move_pct"] = _number(entry["expected_move_pct"])
        body["reasons"] = [
            _with_source({"kind": reason["kind"], "text": reason["text"].strip()},
                         f"coins[{index}].reasons[{number}]", verified)
            for number, reason in enumerate(entry.get("reasons") or [])]
        coins.append(body)
    return {
        "schema_version": OUTLOOK_SCHEMA,
        "outlook_id": worksheet["outlook_id"],
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "run_slot": run_slot,
        "horizon_hours": HORIZON_HOURS,
        "agent": agent,
        "market": {
            "summary": market["summary"].strip(),
            "btc": {"direction": market["btc"]["direction"],
                    "confidence": _number(market["btc"]["confidence"])},
            "eth": {"direction": market["eth"]["direction"],
                    "confidence": _number(market["eth"]["confidence"])},
            "factors": [_with_source({"name": item["name"].strip(), "note": item["note"].strip()},
                                     f"market.factors[{index}]", verified)
                        for index, item in enumerate(market.get("factors") or [])],
            "events": [_with_source({"at": item.get("at"), "what": item["what"].strip()},
                                    f"market.events[{index}]", verified)
                       for index, item in enumerate(market.get("events") or [])],
        },
        "coins": coins,
    }


def filled_counts(worksheet):
    entries = worksheet.get("coins") or []
    skipped = sum(row.get("direction") == SKIPPED for row in entries)
    filled = sum(row.get("direction") in DIRECTIONS for row in entries)
    return filled, skipped, len(entries) - filled - skipped
