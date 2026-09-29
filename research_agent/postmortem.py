"""The evening post-mortem (plan sections 5b and 5c): a worksheet for every post-mortem the
app says is still owed, and the ``POST_MORTEM_V1`` notes it becomes
(``docs/API-CONTRACT.md``, "Learning loop").

The subjects are exactly the context's ``lessons.pending_post_mortems``: the agent's notable
closed trades, and the movers of the latest New York day the app recorded (its
``MARKET_REALITY_V1``, written by the nightly job at 05:30 UTC). The app refuses a mover it
did not record (``NOT_A_RECORDED_MOVER``), so the kit never adds subjects of its own, and
``require_reality`` stops before building anything when the day asked for is not recorded
yet, rather than build a partial worksheet.

For each subject the worksheet holds the price window (Coinbase 1-hour bars), the bar that
holds the app's reference time, the kit's own measures of the move, and the technical
state just before it, computed from the bars with a plain-text draft. The research session
adds the cause, ``knowable_before_move``, the cited sources, ``pre_move_technicals`` and,
for the research checklist, the factors behind the cause.

The knowable-before-move helper compares each cited source's ``published_at`` with the
subject's reference time: the app's ``move_start_at`` for a mover, the first entry fill for
a trade, the same times the app judges ``knowable_before_move: true`` against
(``KNOWABLE_BEFORE_MOVE_UNSUPPORTED``). It suggests ``true`` only when a source was
published at or before that time; it never guesses a missing time. The session decides.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from research_agent import checklist, movers, technicals
from research_agent.technicals import HOUR, between, floor_hour, pct_text

WORKSHEET_SCHEMA = "RESEARCH_AGENT_POSTMORTEM_WORKSHEET_V1"
POST_MORTEM_SCHEMA = "POST_MORTEM_V1"
CAUSES = ("COIN_NEWS", "MARKET_WIDE", "NO_NEWS", "SURPRISE")
SOURCED_CAUSES = frozenset({"COIN_NEWS", "MARKET_WIDE"})
UNDECIDED = "TO_FILL"
MAX_ITEMS, MAX_SOURCES = 30, 4
LIMITS = {"summary": 400, "pre_move_technicals": 300}
MAX_BODY_BYTES = 524_288
LOOKBACK = timedelta(days=7)
NEW_YORK = ZoneInfo("America/New_York")
ANSWERS = ("cause", "knowable_before_move", "summary", "sources", "pre_move_technicals",
           "factors")
SOURCE_KEYS = frozenset({"url", "excerpt", "published_at"})
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
HOW_TO_FILL = [
    "cause: COIN_NEWS (a hack, a listing, an unlock, a lawsuit), MARKET_WIDE (a Bitcoin or "
    "macro move, regulation, an exchange outage), NO_NEWS (the setup, technicals or flows "
    "alone) or SURPRISE (nothing was knowable).",
    "knowable_before_move: true, false or null (unknown). true needs a cited source published "
    "at or before facts.reference_at: the app's move_start_at for a mover, the first entry "
    "fill for a trade. Run postmortem-check for the helper's suggestion; you decide.",
    "summary: 1-400 characters. pre_move_technicals: 1-300 characters (facts."
    "pre_move_technicals_draft is the kit's computed start; add the chart read).",
    "sources: at most 4 {url, excerpt, published_at}; COIN_NEWS and MARKET_WIDE need one. The "
    "excerpt exactly as printed; published_at from the page's own metadata or null; never "
    "invent a time.",
    "factors (for the research checklist, never sent): [{kind, key, description}], kind "
    "NEWS_TYPE, EVENT, MARKET_FACTOR or SOURCE_QUALITY, key like EXCHANGE_LISTING.",
]


class PostmortemError(Exception):
    """A refused build or send; the message starts with a code."""


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else None


def yesterday(now):
    return now.astimezone(NEW_YORK).date() - timedelta(days=1)


def require_reality(lessons, day):
    """Notes to show, or ``PostmortemError`` when the context cannot carry ``day``'s
    post-mortems yet: no lessons, ``day`` not among ``lessons.recent_days``, or an outlook
    section this kit does not know."""
    if not lessons:
        raise PostmortemError(
            "LESSONS_MISSING: this context carries no lessons (RESEARCH_CONTEXT_V2 with the "
            "agent's own token is needed); read the context again")
    recorded = [entry.get("day") for entry in lessons.get("recent_days") or []]
    if day.isoformat() not in recorded:
        raise PostmortemError(
            f"REALITY_DAY_NOT_RECORDED: the app has not recorded {day.isoformat()}'s market "
            f"reality in this context (latest recorded: {recorded[0] if recorded else 'none'}). "
            "Its nightly job runs at 05:30 UTC (01:30 New York in summer, 00:30 in winter): "
            "read the context again after 02:00 New York, then build the worksheet.")
    outlook = lessons.get("outlook") or {}
    if outlook.get("status") not in ("GRADED", "NO_GRADED_OUTLOOK_YET"):
        raise PostmortemError("LESSONS_OUTLOOK_UNKNOWN: lessons.outlook.status is "
                              f"{outlook.get('status')!r}; read the context again")
    notes = []
    if outlook.get("status") == "GRADED" and outlook.get("day") != day.isoformat():
        notes.append(f"No outlook was graded on {day.isoformat()}: the latest graded outlook "
                     f"was graded on {outlook.get('day')}.")
    return notes


# --- Subjects ---------------------------------------------------------------------------------

def _ceil_hour(moment):
    floored = floor_hour(moment)
    return floored if floored == moment else floored + HOUR


def _decimal_or_none(value):
    """``value`` as a finite Decimal, or ``None`` (a null, a bool, text that is no number)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _trade(item, as_of):
    entry_at, exit_at = _time(item.get("entry_at")), _time(item.get("exit_at"))
    notes = []
    if entry_at is None:
        notes.append("The app recorded no entry fill time for this trade: no reference time "
                     "and no price window.")
    if exit_at is None:
        notes.append("The app recorded no exit fill for this trade (for example, flattened "
                     "outside the engine): its window ends when the lessons were read, and the "
                     "kit does not measure the move.")
    end = exit_at or as_of
    window = ((floor_hour(entry_at), _ceil_hour(end))
              if entry_at is not None and end is not None and end > entry_at else None)
    moment = exit_at or entry_at
    notable = bool(item.get("notable_reasons"))
    return {"subject": {"kind": "TRADE", "setup_id": item["setup_id"]},
            "symbol": item.get("symbol"),
            "day": moment.astimezone(NEW_YORK).date().isoformat() if moment else None,
            "reference": entry_at, "window": window, "item": item, "notable": notable,
            "exit_known": exit_at is not None, "notes": notes}


def _mover(item):
    notes, window = [], None
    try:
        window = movers.day_window(date.fromisoformat(item.get("day")))
    except (TypeError, ValueError):
        notes.append("The app's day for this mover could not be read: no price window.")
    reference = _time(item.get("move_start_at"))
    if reference is None:
        notes.append("The app recorded no move_start_at for this mover, so knowable_before_move "
                     "true is never supported.")
    return {"subject": {"kind": "MOVER", "symbol": item.get("symbol"), "day": item.get("day")},
            "symbol": item.get("symbol"), "day": item.get("day"), "reference": reference,
            "window": window, "item": item, "notable": True, "exit_known": None,
            "notes": notes}


def subjects_from(lessons):
    """The owed post-mortems, one record each (``subject``, ``symbol``, ``day``,
    ``reference``: the time knowability is judged against, ``window``: ``(start, end)`` or
    ``None``, ``item``: the app's pending entry, ``notable``, ``exit_known``, ``notes``).

    Notable trades come first, then the movers (misses first, largest move first), then the
    trades with no notable reason, which are optional (guidelines V6 asks for every notable
    trade and the day's movers). A pending entry with a missing or malformed field keeps
    its subject, with a note: one subject never refuses the whole worksheet. Only an entry
    that cannot name a subject at all (no setup ID, no symbol) is left out."""
    pending = (lessons or {}).get("pending_post_mortems") or {}
    as_of = _time((lessons or {}).get("as_of"))
    trades = [_trade(item, as_of) for item in pending.get("trades") or []
              if isinstance(item, dict) and item.get("setup_id")]
    trades.sort(key=lambda row: (not row["notable"], str(row["item"].get("exit_at") or "")))
    found = [row for row in trades if row["notable"]]
    rows = [_mover(item) for item in pending.get("movers") or []
            if isinstance(item, dict) and item.get("symbol")]
    rows.sort(key=lambda row: (not row["item"].get("was_miss"),
                               -abs(_decimal_or_none(row["item"].get("return_pct")) or 0),
                               str(row["symbol"])))
    return found + rows + [row for row in trades if not row["notable"]]


def subject_id(subject):
    """The app's ``subject_key``: ``TRADE:<setup_id>`` or ``MOVER:<day>:<symbol>``."""
    if subject["kind"] == "TRADE":
        return f"TRADE:{subject['setup_id']}"
    return f"MOVER:{subject['day']}:{subject['symbol']}"


def pre_move_draft(state, reference_at):
    """A plain-text start for ``pre_move_technicals`` from computed facts only (<= 300)."""
    if not state or state.get("unavailable"):
        return None
    tags = ", ".join(state.get("tags") or []) or "none"
    text = (f"Before {reference_at:%Y-%m-%d %H:%M} UTC: close {state['close']}, "
            f"{state['distance_from_7d_high_pct']}% from the 7-day high and "
            f"{state['distance_from_7d_low_pct']}% from the 7-day low; "
            f"{state['return_7d_pct']}% over 7 days, {state['return_24h_pct']}% over 24 hours; "
            f"24-hour volume {state['volume_ratio_24h_vs_7d']}x its 7-day average; "
            f"24-hour range {state['range_24h_pct']}%. Tags: {tags}.")
    return text[:LIMITS["pre_move_technicals"]]


def _facts(record, bars_for):
    subject, symbol, item = record["subject"], record["symbol"], record["item"]
    reference_at, window = record["reference"], record["window"]
    coin = str(symbol).split("/")[0]
    facts = {"from_app": item,
             "reference_at": reference_at.isoformat() if reference_at else None,
             "reference_basis": ("FIRST_ENTRY_FILL" if subject["kind"] == "TRADE"
                                 else "MOVE_START_AT"),
             "notes": record["notes"], "window": None, "kit_measure": None}
    if window is None:
        facts["market_data"] = {"unavailable": "NO_PRICE_WINDOW"}
        return facts
    start, end = window
    facts["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    bars, reason, retrieved_at = bars_for(symbol, start - LOOKBACK, end)
    if bars is None:
        facts["market_data"] = {"unavailable": reason}
        return facts
    facts["market_data"] = {"provider": "COINBASE_EXCHANGE_PUBLIC_API", "feed": "1-hour candles",
                            "retrieved_at": retrieved_at}
    window_bars = between(bars, start, end)
    facts["window"].update({"bars_used": len(window_bars),
                            "bars": [technicals.bar_json(bar, coin) for bar in window_bars]})
    if subject["kind"] == "MOVER":
        move = technicals.window_move(window_bars)
        facts["kit_measure"] = None if move is None else {
            "return_pct": pct_text(move.return_pct),
            "up_excursion_pct": pct_text(move.up_excursion_pct),
            "down_excursion_pct": pct_text(move.down_excursion_pct),
            "move_start_1h": technicals.move_bar_json(move.move_start, coin, window_start=start,
                                                      reference=move.open),
            "half_move_bar": technicals.move_bar_json(move.half_move, coin, window_start=start,
                                                      reference=move.open),
        }
    else:
        entry = _decimal_or_none(item.get("entry_price"))
        exit_ = _decimal_or_none(item.get("exit_price"))
        if record["exit_known"] and entry is not None and exit_ is not None and entry > 0:
            half = technicals.find_half_move(window_bars, reference=entry, final=exit_)
            facts["kit_measure"] = {
                "return_pct": pct_text(technicals.change_pct(exit_, entry)),
                "half_move_bar": technicals.move_bar_json(half, coin, window_start=start,
                                                          reference=entry)}
    if reference_at is not None:
        bar = technicals.bar_at(bars, reference_at)
        facts["reference_bar"] = technicals.bar_json(bar, coin) if bar else None
        state = technicals.technical_state(bars, as_of=floor_hour(reference_at))
        facts["pre_move_technicals_computed"] = state
        facts["pre_move_technicals_draft"] = pre_move_draft(state, floor_hour(reference_at))
    return facts


def build_worksheet(*, ctx, lessons, day, bars_for, previous=None, now):
    """``postmortem.json`` for the owed post-mortems. ``bars_for(symbol, start, end)`` returns
    ``(bars or None, reason, retrieved_at)``. Answers already given in ``previous`` are kept."""
    notes = require_reality(lessons, day)
    kept = {row.get("subject_id"): row for row in (previous or {}).get("subjects") or []}
    subjects = []
    for record in subjects_from(lessons):
        subject, item = record["subject"], record["item"]
        sid = subject_id(subject)
        row = {"subject": subject, "subject_id": sid, "symbol": record["symbol"],
               "day": record["day"] or day.isoformat(),
               "required": record["notable"],
               "was_miss": item.get("was_miss") if subject["kind"] == "MOVER" else None,
               "notable_reasons": item.get("notable_reasons") if subject["kind"] == "TRADE"
               else None,
               "facts": _facts(record, bars_for),
               "cause": None, "knowable_before_move": UNDECIDED, "summary": "", "sources": [],
               "pre_move_technicals": "", "factors": [], "knowable_helper": None}
        for key in ANSWERS:
            if sid in kept and key in kept[sid]:
                row[key] = kept[sid][key]
        subjects.append(row)
    outlook = lessons.get("outlook") or {}
    return {
        "schema": WORKSHEET_SCHEMA, "contract": POST_MORTEM_SCHEMA, "day": day.isoformat(),
        "built_at": now.astimezone(UTC).isoformat(), "context_as_of": ctx.get("as_of"),
        "lessons_as_of": lessons.get("as_of"),
        "reality": {"recorded_days": [entry.get("day") for entry in lessons.get("recent_days")
                                      or []],
                    "outlook_status": outlook.get("status"), "outlook_day": outlook.get("day"),
                    "notes": notes},
        "how_to_fill": HOW_TO_FILL,
        "subjects": subjects,
        "dropped_since_last_build": sorted(set(kept) - {row["subject_id"] for row in subjects}),
    }


def empty_worksheet(*, ctx, day, now, code):
    """The worksheet of a run whose lessons the app could not serve (``available: false``):
    no subjects, and the reason. The owed post-mortems stay pending for the next run."""
    return {
        "schema": WORKSHEET_SCHEMA, "contract": POST_MORTEM_SCHEMA, "day": day.isoformat(),
        "built_at": now.astimezone(UTC).isoformat(), "context_as_of": ctx.get("as_of"),
        "lessons_as_of": (ctx.get("lessons") or {}).get("as_of"),
        "reality": {"recorded_days": [], "outlook_status": None, "outlook_day": None,
                    "notes": [f"Lessons unavailable this run ({code}): no subjects."]},
        "how_to_fill": HOW_TO_FILL, "subjects": [], "dropped_since_last_build": [],
    }


# --- The knowable-before-move helper ---------------------------------------------------------

def knowable_helper(row):
    """The helper's suggestion for one subject, from its cited sources' ``published_at``
    against its reference time. Never guesses a missing time; the session decides."""
    reference = _time((row.get("facts") or {}).get("reference_at"))
    positions, supporting, after = [], [], 0
    sources_ = row.get("sources") if isinstance(row.get("sources"), list) else []
    for index, source in enumerate(sources_):
        source = source if isinstance(source, dict) else {}
        published = _time(source.get("published_at"))
        if published is None:
            position = "NO_PUBLISH_TIME"
        elif reference is None:
            position = "NO_REFERENCE_TIME"
        elif published <= reference:
            position = "AT_OR_BEFORE_REFERENCE"
            supporting.append(index)
        else:
            position = "AFTER_REFERENCE"
            after += 1
        positions.append({"source": index, "published_at": source.get("published_at"),
                          "position": position})
    if reference is None:
        suggestion, why = None, ("The app recorded no reference time for this subject, so "
                                 "knowable_before_move true is never supported.")
    elif supporting:
        suggestion, why = True, (f"Source {supporting[0]} was published at or before the "
                                 f"reference time {reference.isoformat()}.")
    elif positions and after == len(positions):
        suggestion, why = False, ("Every cited source was published after the reference time "
                                  f"{reference.isoformat()}.")
    else:
        suggestion, why = None, ("No cited source has a publish time at or before the "
                                 f"reference time {reference.isoformat() if reference else ''}; "
                                 "the helper never guesses a missing time.")
    return {"reference_at": reference.isoformat() if reference else None,
            "sources": positions, "suggestion": suggestion, "why": why,
            "label": _label(row)}


def _label(row):
    """Plan 5c's reading of a decided mover: cause, knowability and the miss flag."""
    cause, knowable = row.get("cause"), row.get("knowable_before_move")
    if row["subject"]["kind"] != "MOVER" or cause is None or isinstance(knowable, str):
        return None
    if cause in ("SURPRISE", "NO_NEWS"):
        return cause
    if knowable is True:
        return "KNOWABLE_AND_MISSED" if row.get("was_miss") else "KNOWABLE_AND_EXPECTED"
    return "KNOWABLE_NOT_ACTIONABLE" if knowable is False else "KNOWABILITY_UNKNOWN"


# --- Checks and the notes ---------------------------------------------------------------------

def _identity_word(agent_id):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(agent_id) + r"(?![A-Za-z0-9_])",
                      re.IGNORECASE)


def is_decided(row):
    return row.get("cause") is not None and not isinstance(row.get("knowable_before_move"), str)


def undecided(worksheet, *, required_only=True):
    """The subjects still to decide: by default only the required ones (every notable trade
    and every mover); a trade with no notable reason is optional."""
    return [row["subject_id"] for row in worksheet.get("subjects") or []
            if not is_decided(row) and (row.get("required", True) or not required_only)]


def check_subject(row, *, agent_id):
    """Problems that refuse this subject's item, as the app would refuse it (and the kit's
    own rules: a checked factor list, sources as {url, excerpt, published_at})."""
    problems, sid = [], row.get("subject_id")
    word = _identity_word(agent_id) if agent_id else None
    if row.get("cause") not in CAUSES:
        problems.append(f"{sid}.cause: one of {list(CAUSES)}")
    knowable = row.get("knowable_before_move")
    if knowable is not True and knowable is not False and knowable is not None:
        # Only JSON true, false or null, as the app takes it: never 1, 0 or text.
        problems.append(f"{sid}.knowable_before_move: decide true, false or null")
    for key in ("summary", "pre_move_technicals"):
        value = row.get(key)
        if not isinstance(value, str) or not 0 < len(value.strip()) <= LIMITS[key]:
            problems.append(f"{sid}.{key}: 1-{LIMITS[key]} characters required")
        elif word and word.search(value):
            problems.append(f"{sid}.{key}: AGENT_IDENTITY_IN_POST_MORTEM")
    sources_ = row.get("sources") or []
    if not isinstance(sources_, list) or len(sources_) > MAX_SOURCES:
        problems.append(f"{sid}.sources: at most {MAX_SOURCES}")
        sources_ = []
    for index, source in enumerate(sources_):
        where = f"{sid}.sources[{index}]"
        if (not isinstance(source, dict) or not set(source) <= SOURCE_KEYS
                or not {"url", "excerpt"} <= set(source)):
            problems.append(f"{where}: {{url, excerpt, published_at}} only")
            continue
        if not isinstance(source["url"], str) or not source["url"].startswith("https://"):
            problems.append(f"{where}.url: PUBLIC_HTTPS_SOURCE_REQUIRED")
        if not isinstance(source["excerpt"], str) or not source["excerpt"].strip():
            problems.append(f"{where}.excerpt: SOURCE_EXCERPT_REQUIRED")
        published = source.get("published_at")
        if published is not None and not (isinstance(published, str)
                                          and RFC3339.fullmatch(published)):
            problems.append(f"{where}.published_at: RFC3339_TIMESTAMP_OR_NULL_REQUIRED")
    if row.get("cause") in SOURCED_CAUSES and not sources_:
        problems.append(f"{sid}.sources: POST_MORTEM_SOURCES_REQUIRED for {row.get('cause')}")
    if knowable is True and knowable_helper(row)["suggestion"] is not True:
        problems.append(f"{sid}.knowable_before_move: KNOWABLE_BEFORE_MOVE_UNSUPPORTED (true "
                        "needs a cited source published at or before facts.reference_at)")
    try:
        checklist.check_factors(sid, row.get("factors"))
    except checklist.ChecklistError as exc:
        problems.append(f"{sid}.factors: {exc}")
    return problems


def citations(row):
    return [(f"{row['subject_id']}.sources[{index}]", source)
            for index, source in enumerate(row.get("sources") or [])]


def item(row, verified):
    return {"subject": row["subject"], "cause": row["cause"],
            "knowable_before_move": row["knowable_before_move"],
            "summary": row["summary"].strip(),
            "sources": [verified[path] for path, _source in citations(row)],
            "pre_move_technicals": row["pre_move_technicals"].strip()}


def batches(rows):
    return [rows[start:start + MAX_ITEMS] for start in range(0, len(rows), MAX_ITEMS)]


def note(items, *, agent, generated_at, note_id=None):
    return {"schema_version": POST_MORTEM_SCHEMA, "note_id": note_id or str(uuid4()),
            "generated_at": generated_at.astimezone(UTC).isoformat(), "agent": agent,
            "items": items}


def summary_line(worksheet):
    rows = worksheet.get("subjects") or []
    trades = sum(row["subject"]["kind"] == "TRADE" for row in rows)
    optional = sum(not row.get("required", True) for row in rows)
    misses = sum(bool(row.get("was_miss")) for row in rows)
    return (f"post-mortem worksheet for {worksheet['day']}: {trades} trades and "
            f"{len(rows) - trades} movers ({misses} misses) owed; {len(undecided(worksheet))} "
            f"still to decide" + (f"; {optional} trades with no notable reason are optional"
                                  if optional else ""))

