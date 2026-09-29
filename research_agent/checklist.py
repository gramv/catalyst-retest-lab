"""The research checklist: Muse's own memory of what to check first (plan section 5c).

Kept outside the app, in an owner-only state folder (``--state-dir``, default
``~/.local/share/catalyst-retest-lab/muse-state/``). Each pattern records its ID,
description, kind, occurrences, hits, first and last day seen, status and the evidence
behind it (days and subjects). The rules are guidelines V6's (coordinator's decisions of
2026-09-28, sent to package learning-app with the same text):

* **TECHNICAL** patterns are scored every night on the whole universe from one New York
  day's ``movers.json``. An occurrence is a state tag the coin had at the day's start
  (``movers``' ``pre_window``, from the 7 days before it); a hit is that the coin was one
  of the app's movers that day (``lessons.recent_days``, never recomputed by the kit). Only
  a few coins are movers on any day, so a fixed hit-rate bar would never be met: these are
  judged by **lift**, the hit rate over the mover share of the universe on the same days
  (``hits / sum of each occurrence's day mover_share``, the app's figure).
  ACTIVE at 10+ occurrences with lift at least 2.0 overall and over the last 10; RETIRED
  when an ACTIVE pattern's lift over its last 10 falls below 1.25.
* **NEWS_TYPE, EVENT, MARKET_FACTOR and SOURCE_QUALITY** patterns come from the
  post-mortems the app ACCEPTED, as ``postmortem-submit.json`` records them (never the raw
  worksheet: an item the app refused, or never received, is not evidence). An occurrence is
  a factor the session recorded as the cause of a mover or a notable trade (a trade with no
  notable reason does not count); a hit is that the cause was public before the move
  started (``knowable_before_move: true`` as accepted), so checking it in the morning would
  have caught the move. When the app accepted a later note on the same subject, the latest
  counts, as for the app's own lessons. ACTIVE at 10+ occurrences with a hit rate of at
  least 50% overall and over the last 10; RETIRED when an ACTIVE pattern's hit rate over its
  last 10 falls below 40%.
* Only an ACTIVE pattern can be RETIRED, for every kind; a candidate simply stays a
  candidate. A RETIRED pattern becomes ACTIVE again only under the same rule. Every status
  change is logged, with the day and the numbers behind it.
* "The last 10 occurrences" are counted in whole days, newest first, until they hold at
  least 10 (``recent``): a day's occurrences have no order among themselves, so a day is
  never cut in two. This is the kit's reading of V6, approved by the coordinator on
  2026-09-28.
* It never narrows coverage. An item only sets what is checked first: there is no field
  that removes a coin, a source or a check, and the export says so.

Evidence is append-only and audited. An occurrence is keyed by pattern, day and subject,
and the entry appended last for a key is the one that counts (``effective``). Re-applying
the same file changes nothing. Each input describes its scope completely: a movers.json
every TECHNICAL occurrence of its day, an accepted post-mortem item every factor of its
subject. So an occurrence that input now gives a different hit is corrected, and one it no
longer lists is withdrawn, each by appending a new entry that says so; no entry is ever
edited or removed. Every update is logged in the state file.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from research_agent import technicals

CHECKLIST_SCHEMA = "RESEARCH_AGENT_CHECKLIST_V1"
EXPORT_SCHEMA = "RESEARCH_AGENT_CHECKLIST_EXPORT_V1"
MOVERS_SCHEMA = "RESEARCH_AGENT_MOVERS_V1"
POSTMORTEM_SCHEMA = "RESEARCH_AGENT_POSTMORTEM_WORKSHEET_V1"
SUBMISSIONS_SCHEMA = "RESEARCH_AGENT_POSTMORTEM_SUBMISSIONS_V1"
STATE_FILE = "checklist.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "catalyst-retest-lab" / "muse-state"

KINDS = ("TECHNICAL", "NEWS_TYPE", "EVENT", "MARKET_FACTOR", "SOURCE_QUALITY")
TECHNICAL = "TECHNICAL"
FACTOR_KINDS = KINDS[1:]  # What a post-mortem may record; TECHNICAL comes from movers only.
CANDIDATE, ACTIVE, RETIRED = "CANDIDATE", "ACTIVE", "RETIRED"
MIN_OCCURRENCES = 10
ROLLING = 10
TECHNICAL_ACTIVATE_LIFT = Decimal("2.0")
TECHNICAL_RETIRE_LIFT = Decimal("1.25")
FACTOR_ACTIVATE_HIT_RATE = Decimal("0.5")
FACTOR_RETIRE_HIT_RATE = Decimal("0.4")
KEY = re.compile(r"[A-Z][A-Z0-9_]{1,47}")
USE = ("Order and emphasize checks only. Every coin in the universe is still researched: no "
       "item removes a coin, a source or a check.")


class ChecklistError(Exception):
    """A refused state folder or input file; the message starts with a code."""


# --- The owner-only state file ----------------------------------------------------------------

def _private_dir(state_dir):
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(state_dir)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ChecklistError(
            f"CHECKLIST_STATE_DIR_NOT_PRIVATE: {state_dir} must be a directory of this user "
            "that nobody else can read (chmod 700)")
    return state_dir


def empty_state():
    return {"schema": CHECKLIST_SCHEMA, "rules": rules(), "patterns": {}, "log": []}


def load(state_dir):
    """The checklist in ``state_dir``, or an empty one. A file that is a link, belongs to
    someone else or can be read by anyone else is refused, never read."""
    path = _private_dir(state_dir) / STATE_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return empty_state()
    except OSError:
        raise ChecklistError(f"CHECKLIST_STATE_UNREADABLE: {path}") from None
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise ChecklistError(f"CHECKLIST_STATE_NOT_PRIVATE: {path} must be this user's, mode "
                             "0600 (chmod 600)")
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        state = json.load(stream)
    if state.get("schema") != CHECKLIST_SCHEMA:
        raise ChecklistError(f"CHECKLIST_STATE_UNKNOWN_SCHEMA: {path}")
    return state


def save(state_dir, state):
    """Written whole to a new owner-only file, then renamed over the old one, so a crash
    never leaves half a checklist."""
    folder = _private_dir(state_dir)
    target = folder / STATE_FILE
    temporary = folder / f".{STATE_FILE}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


# --- Rules --------------------------------------------------------------------------------------

def rules():
    return {
        "min_occurrences": MIN_OCCURRENCES, "rolling_occurrences": ROLLING,
        "technical": {
            "measure": "LIFT", "activate_at_least": str(TECHNICAL_ACTIVATE_LIFT),
            "retire_below": str(TECHNICAL_RETIRE_LIFT),
            "occurrence": "a state tag at the New York day's start (movers.json pre_window)",
            "hit": "the coin was one of the app's movers that day (lessons.recent_days)",
            "lift": "hits / the sum over occurrences of that day's mover_share "
                    "(lessons.recent_days)",
        },
        "factors": {
            "kinds": list(FACTOR_KINDS),
            "measure": "HIT_RATE", "activate_at_least": str(FACTOR_ACTIVATE_HIT_RATE),
            "retire_below": str(FACTOR_RETIRE_HIT_RATE),
            "occurrence": "a factor recorded as the cause in a filled post-mortem",
            "hit": "knowable_before_move is true",
        },
        "coverage": USE,
    }


def _thresholds(kind):
    if kind == TECHNICAL:
        return "lift", TECHNICAL_ACTIVATE_LIFT, TECHNICAL_RETIRE_LIFT
    return "hit rate", FACTOR_ACTIVATE_HIT_RATE, FACTOR_RETIRE_HIT_RATE


def measure(kind, entries):
    """What a pattern's rule compares over ``entries``: the lift for TECHNICAL (hits over
    the sum of each occurrence's day mover share), the hit rate otherwise; ``None`` when it
    cannot be computed."""
    if not entries:
        return None
    hits = Decimal(sum(entry["hit"] for entry in entries))
    if kind != TECHNICAL:
        return hits / Decimal(len(entries))
    expected = sum((Decimal(entry["day_mover_share"]) for entry in entries), Decimal(0))
    return hits / expected if expected > 0 else None


def _text(value):
    return None if value is None else str(value.quantize(Decimal("0.01"), ROUND_HALF_UP))


def effective(evidence):
    """The occurrences that count, by day then subject: for each (day, subject), the entry
    appended last, unless that entry withdrew it. Earlier entries stay in the file,
    unedited."""
    latest = {}
    for entry in evidence:
        latest[(entry["day"], entry["subject"])] = entry
    return sorted((entry for entry in latest.values() if not entry.get("withdrawn")),
                  key=lambda entry: (entry["day"], entry["subject"]))


def recent(evidence):
    """The "last 10 occurrences": whole days, newest first, until they hold at least
    ``ROLLING``. Occurrences of one day have no order among themselves (a TECHNICAL day
    brings many at once, all as of the day's start), so a day is never cut in two."""
    days = sorted({entry["day"] for entry in evidence}, reverse=True)
    kept, total = set(), 0
    for day in days:
        if total >= ROLLING:
            break
        kept.add(day)
        total += sum(entry["day"] == day for entry in evidence)
    return [entry for entry in evidence if entry["day"] in kept]


def next_status(pattern):
    """The status the rules give ``pattern`` now, and why."""
    evidence = effective(pattern["evidence"])
    latest = recent(evidence)
    name, activate, retire = _thresholds(pattern["kind"])
    overall, rolling = measure(pattern["kind"], evidence), measure(pattern["kind"], latest)
    status = pattern["status"]
    if status in (CANDIDATE, RETIRED):
        if (len(evidence) >= MIN_OCCURRENCES and overall is not None and rolling is not None
                and overall >= activate and rolling >= activate):
            return ACTIVE, (f"{name} {_text(overall)} over {len(evidence)} occurrences and "
                            f"{_text(rolling)} over the last {len(latest)}: at least {activate}")
    elif (status == ACTIVE and len(latest) >= ROLLING and rolling is not None
          and rolling < retire):
        return RETIRED, (f"{name} {_text(rolling)} over the last {len(latest)} occurrences: "
                         f"below {retire}")
    return status, None


def _refresh(pattern):
    evidence = effective(pattern["evidence"])
    hits = sum(entry["hit"] for entry in evidence)
    pattern.update({
        "occurrences": len(evidence), "hits": hits,
        "hit_rate": _text(Decimal(hits) / len(evidence)) if evidence else None,
        "measure": "LIFT" if pattern["kind"] == TECHNICAL else "HIT_RATE",
        "value": _text(measure(pattern["kind"], evidence)),
        "rolling_value": _text(measure(pattern["kind"], recent(evidence))),
        "first_seen": evidence[0]["day"] if evidence else None,
        "last_seen": evidence[-1]["day"] if evidence else None,
    })


# --- Evidence from movers.json and a filled postmortem.json ------------------------------------

def pattern_id(kind, key):
    if kind not in KINDS:
        raise ChecklistError(f"CHECKLIST_KIND_INVALID: {kind!r} is not one of {list(KINDS)}")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ChecklistError(f"CHECKLIST_KEY_INVALID: {key!r} must match {KEY.pattern}")
    return f"{kind}:{key}"


def movers_evidence(movers_doc, lessons):
    """``(found, scopes)``: ``[(kind, key, description, entry)]`` for TECHNICAL patterns, and
    the day they describe completely, from one New York day's
    ``movers.json`` (each coin's start-of-day state tags) and the app's record of that day
    in ``lessons.recent_days`` (its movers are the hits, its ``mover_share`` the base the
    lift divides by). Misses and mover shares are never recomputed here, so the kit and the
    app agree on them."""
    if movers_doc.get("schema") != MOVERS_SCHEMA:
        raise ChecklistError("CHECKLIST_INPUT_UNKNOWN: not a movers.json")
    window = movers_doc.get("window") or {}
    if window.get("kind") != "NEW_YORK_DAY":
        raise ChecklistError("CHECKLIST_NEEDS_A_NEW_YORK_DAY: score whole New York days only "
                             "(movers --day), so no hour is counted twice")
    day = window["day"]
    recorded = {entry.get("day"): entry for entry in (lessons or {}).get("recent_days") or []}
    reality = recorded.get(day)
    if reality is None:
        raise ChecklistError(
            f"CHECKLIST_REALITY_DAY_NOT_RECORDED: the context's lessons.recent_days has no "
            f"record of {day}. Read the context again after the app's nightly job (after 02:00 "
            "New York) and pass it with --lessons")
    if reality.get("mover_share") is None:
        raise ChecklistError(f"CHECKLIST_NO_MOVER_SHARE: the app recorded {day} without a mover "
                             "share (no coin measured), so no TECHNICAL lift can be scored")
    app_movers = {row["symbol"] for row in reality.get("movers") or []}
    share = str(Decimal(str(reality["mover_share"])))
    found, scopes = [], [("TECHNICAL_DAY", day)]
    for coin, record in sorted((movers_doc.get("coins") or {}).items()):
        symbol = record.get("symbol") or f"{coin}/USD"
        for tag in (record.get("pre_window") or {}).get("tags") or []:
            meaning = technicals.TAG_MEANINGS.get(tag, tag)
            found.append((TECHNICAL, tag,
                          f"Coins with {meaning} at the start of the New York day",
                          {"day": day, "subject": symbol, "hit": symbol in app_movers,
                           "source": "movers+recent_days", "day_mover_share": share}))
    return found, scopes


def submission_evidence(submissions):
    """``(found, scopes, left_out)`` from ``postmortem-submit.json``: the factors of the
    items the app ACCEPTED, the latest accepted item of a subject counting (later attempts
    and notes are later). A trade with no notable reason is left out. ``scopes`` are the
    subjects whose factors the file now describes completely."""
    if submissions.get("schema") != SUBMISSIONS_SCHEMA:
        raise ChecklistError("CHECKLIST_INPUT_UNKNOWN: not a postmortem-submit.json")
    latest = {}
    for attempt in submissions.get("attempts") or []:
        for note in attempt.get("notes") or []:
            for item in note.get("items") or []:
                if item.get("status") == "ACCEPTED":
                    latest[item["subject_id"]] = {**item, "note_id": note.get("note_id")}
    found, scopes, left_out = [], [], []
    for sid, item in latest.items():
        if (item.get("subject") or {}).get("kind") == "TRADE" and not item.get("notable"):
            left_out.append(sid)
            continue
        scopes.append(("SUBJECT", sid))
        for factor in check_factors(sid, item.get("factors")):
            found.append((factor["kind"], factor["key"], factor["description"].strip()[:200],
                          {"day": item["day"], "subject": sid,
                           "hit": item.get("knowable_before_move") is True,
                           "source": "postmortem", "note_id": item.get("note_id")}))
    return found, scopes, left_out


def check_factors(subject_id, factors):
    """A post-mortem subject's ``factors``, checked: a list of ``{kind, key, description}``
    with a factor kind (TECHNICAL patterns come from movers.json only) and a key like
    ``EXCHANGE_LISTING``."""
    if factors is None:
        return []
    if not isinstance(factors, list):
        raise ChecklistError(f"CHECKLIST_FACTOR_INVALID: {subject_id} factors must be a list")
    for index, factor in enumerate(factors):
        where = f"{subject_id} factors[{index}]"
        if not isinstance(factor, dict) or not set(factor) <= {"kind", "key", "description"}:
            raise ChecklistError(f"CHECKLIST_FACTOR_INVALID: {where} must be an object with "
                                 "kind, key and description only")
        if factor.get("kind") not in FACTOR_KINDS:
            raise ChecklistError(
                f"CHECKLIST_FACTOR_INVALID: {where}.kind must be one of "
                f"{list(FACTOR_KINDS)} (TECHNICAL patterns come from movers.json; describe "
                "the chart in pre_move_technicals)")
        description = factor.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ChecklistError(f"CHECKLIST_FACTOR_INVALID: {where}.description is required")
        pattern_id(factor["kind"], factor.get("key"))
    return factors


def _in_scope(kind, entry, scopes):
    for scope, value in scopes:
        if scope == "TECHNICAL_DAY" and kind == TECHNICAL and entry["day"] == value:
            return True
        if scope == "SUBJECT" and kind != TECHNICAL and entry["subject"] == value:
            return True
    return False


def apply(state, found, *, now, inputs, scopes=()):
    """Reconciles the state with ``found`` inside ``scopes`` and re-evaluates every touched
    pattern; logs the update. Returns ``{"added", "corrected", "withdrawn", "unchanged",
    "new_patterns", "status_changes"}``. Nothing recorded is edited: a correction or a
    withdrawal is a new entry, which ``effective`` reads as the current one."""
    now_text = now.astimezone(UTC).isoformat()
    patterns = state["patterns"]
    report = {"added": 0, "corrected": 0, "withdrawn": 0, "unchanged": 0, "new_patterns": [],
              "status_changes": []}
    wanted = {}
    for kind, key, description, entry in found:
        wanted[(pattern_id(kind, key), entry["day"], entry["subject"])] = (kind, key,
                                                                          description, entry)
    current = {pid: {(e["day"], e["subject"]): e for e in effective(p["evidence"])}
               for pid, p in patterns.items()}
    touched = set()
    for pid, pattern in patterns.items():
        for (day, subject), entry in sorted(current[pid].items()):
            if (pid, day, subject) not in wanted and _in_scope(pattern["kind"], entry, scopes):
                pattern["evidence"].append({"day": day, "subject": subject, "withdrawn": True,
                                            "at": now_text, "inputs": list(inputs)})
                report["withdrawn"] += 1
                touched.add(pid)
    for (pid, day, subject), (kind, key, description, entry) in wanted.items():
        if pid not in patterns:
            patterns[pid] = {"id": pid, "kind": kind, "key": key, "description": description,
                             "status": CANDIDATE, "evidence": [],
                             "history": [{"at": now_text, "day": day, "status": CANDIDATE,
                                          "reason": "first seen"}]}
            current[pid] = {}
            report["new_patterns"].append(pid)
        known = current[pid].get((day, subject))
        if known is not None and known["hit"] == entry["hit"]:
            report["unchanged"] += 1
            continue
        record = {**entry, "at": now_text}
        if known is not None:
            record["corrects"] = known.get("at")
            report["corrected"] += 1
        else:
            report["added"] += 1
        patterns[pid]["evidence"].append(record)
        current[pid][(day, subject)] = record
        touched.add(pid)
    for pid in sorted(touched):
        pattern = patterns[pid]
        _refresh(pattern)
        status, reason = next_status(pattern)
        if status != pattern["status"]:
            pattern["history"].append({"at": now_text, "day": pattern["last_seen"],
                                       "status": status, "reason": reason})
            report["status_changes"].append(f"{pid}: {pattern['status']} -> {status} ({reason})")
            pattern["status"] = status
    state["rules"] = rules()
    state["log"].append({"at": now_text, "inputs": list(inputs),
                         **{k: report[k] for k in ("added", "corrected", "withdrawn",
                                                   "unchanged")},
                         "status_changes": report["status_changes"]})
    return report


# --- Reading it back ---------------------------------------------------------------------------

def _item(pattern):
    return {key: pattern.get(key) for key in (
        "id", "kind", "key", "description", "occurrences", "hits", "hit_rate", "measure",
        "value", "rolling_value", "first_seen", "last_seen")}


def export(state, *, now):
    """The ACTIVE items the morning research checks first, strongest first within a kind."""
    active = [p for p in state["patterns"].values() if p["status"] == ACTIVE]
    active.sort(key=lambda p: (KINDS.index(p["kind"]), -Decimal(p["value"] or 0), p["id"]))
    return {"schema": EXPORT_SCHEMA, "exported_at": now.astimezone(UTC).isoformat(),
            "use": USE, "rules": rules(), "items": [_item(p) for p in active]}


def show_lines(state):
    lines = [
        f"research checklist: {len(state['patterns'])} patterns. ACTIVE needs "
        f"{MIN_OCCURRENCES}+ occurrences with, overall and over the last {ROLLING}: "
        f"TECHNICAL lift >= {TECHNICAL_ACTIVATE_LIFT} (retired below {TECHNICAL_RETIRE_LIFT}), "
        f"other kinds hit rate >= {FACTOR_ACTIVATE_HIT_RATE} (retired below "
        f"{FACTOR_RETIRE_HIT_RATE}).",
    ]
    for status in (ACTIVE, CANDIDATE, RETIRED):
        rows = sorted((p for p in state["patterns"].values() if p["status"] == status),
                      key=lambda p: (KINDS.index(p["kind"]), p["id"]))
        lines.append(f"{status} ({len(rows)})")
        lines.extend(
            f"  {p['id']}: {p['hits']}/{p['occurrences']} hits, {p['measure'].lower()} "
            f"{p['value']} (last {ROLLING}: {p['rolling_value']}), {p['first_seen']} to "
            f"{p['last_seen']}. {p['description']}" for p in rows)
    return lines
