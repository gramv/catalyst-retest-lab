"""Muse's morning lessons (plan section 5): the research context's own ``lessons``
(``RESEARCH_LESSONS_V1``, context V2) read plainly, then turned into ordering hints.

``summary_lines`` prints what the scorecard says about this agent's own research: how often
price came back to entries at each distance, how each timeframe, level rule and pick kind
did, the outlook's hit rate and calibration, the misses, and the post-mortems still owed.

``derive_hints`` turns the same numbers into ``emphasis.json``: ordering hints only, such as
"rank setups closer than 2% first". The guardrails are the plan's (section 2):

* a hint only reorders. ``load_emphasis`` refuses any hint key other than the known ones,
  so no hint can carry a filter, a limit or a trading rule;
* ``build`` applies its 30-pick cap in its own order before any hint, so the set of coins
  in a report never depends on a hint; only their order does, and each promoted pick says
  so in its ``why_over_peers``;
* a hint needs evidence: at least ``MIN_SAMPLE`` resolved picks in two or more buckets and a
  spread of at least ``MIN_SPREAD`` between the best and the worst. Otherwise there is no
  hint, and the text says there was not enough data.

The wording never names the agent, and never writes the literal level-rule tags the
scorecard reads from ``why_over_peers`` ("rule-" followed by the rule letter), so a lesson
about one rule cannot be mistaken for the pick's own rule.
"""

from __future__ import annotations

from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal

EMPHASIS_SCHEMA = "RESEARCH_AGENT_EMPHASIS_V1"
LESSONS_VERSION = "RESEARCH_LESSONS_V1"
DISTANCE_BUCKETS = ("0-1%", "1-2%", "2-3%", "3%+")
TIMEFRAME_WORDS = {"1h": "1-hour", "2h": "2-hour", "4h": "4-hour", "6h": "6-hour", "1d": "daily"}
RULE_WORDS = {"A": "the stop under the whole window's low",
              "B": "the stop under a held lower pivot"}
KIND_WORDS = {"CHART": "chart-only picks", "NEWS": "news-only picks",
              "BOTH": "picks with a fresh catalyst"}
DIMENSIONS = {
    "distance_bucket": {"values": DISTANCE_BUCKETS, "lines": "fill_rate_by_distance",
                        "metric": "shadow_trigger_rate", "count": "shadow_recorded"},
    "timeframe": {"values": tuple(TIMEFRAME_WORDS), "lines": "results_by_timeframe",
                  "metric": "shadow_hit_rate", "count": "shadow_r_count"},
    "rule": {"values": tuple(RULE_WORDS), "lines": "results_by_rule",
             "metric": "shadow_hit_rate", "count": "shadow_r_count"},
    "kind": {"values": tuple(KIND_WORDS), "lines": "results_by_kind",
             "metric": "shadow_hit_rate", "count": "shadow_r_count"},
}
WINDOWS = ("7d", "30d")  # The 30-day lines only when the 7-day ones lack the evidence.
WINDOW_WORDS = {"1d": "the last day", "7d": "the last 7 days", "30d": "the last 30 days"}
MIN_SAMPLE = 5
MIN_SPREAD = Decimal("0.20")
HINT_KEYS = frozenset({"dimension", "prefer", "window", "metric", "evidence", "text"})
MAX_HINT_TEXT = 200
USE = ("Ordering hints only: build ranks picks by them after its own pick limit, and never "
       "drops a coin because of them.")


class LessonsError(Exception):
    """An emphasis file build must refuse; the message starts with a code."""


def _decimal(value):
    return None if value is None else Decimal(str(value))


def _text(value):
    return "n/a" if value is None else str(Decimal(str(value)).quantize(Decimal("0.01"),
                                                                        ROUND_HALF_UP))


def _count(rate, total):
    return int((rate * total).to_integral_value(ROUND_HALF_UP))


# --- Pick attributes, as the scorecard buckets them ------------------------------------------

def distance_bucket(mid, entry):
    """The scorecard's distance bucket: abs(price - entry) / price, in percent."""
    pct = abs(Decimal(mid) - Decimal(entry)) / Decimal(mid) * 100
    if pct < 1:
        return "0-1%"
    if pct < 2:
        return "1-2%"
    if pct < 3:
        return "2-3%"
    return "3%+"


def pick_attributes(*, mid, setup, kind):
    return {"distance_bucket": distance_bucket(mid, setup.entry), "timeframe": setup.timeframe,
            "rule": setup.rule, "kind": kind}


# --- Hints ---------------------------------------------------------------------------------------

def _describe(dimension, value):
    if dimension == "distance_bucket":
        return f"entries {value} from price"
    if dimension == "timeframe":
        return f"{TIMEFRAME_WORDS[value]} setups"
    if dimension == "rule":
        return f"setups with {RULE_WORDS[value]}"
    return KIND_WORDS[value]


def _hint_text(dimension, eligible, prefer, window):
    verb = "reached the entry" if dimension == "distance_bucket" else "hit"
    best, worst = eligible[0], eligible[-1]
    evidence = (f"{_describe(dimension, best[0])} {verb} {_count(best[1], best[2])} of "
                f"{best[2]} times, {_describe(dimension, worst[0])} "
                f"{_count(worst[1], worst[2])} of {worst[2]}, in {WINDOW_WORDS[window]}")
    first = ", ".join(_describe(dimension, value) for value in prefer)
    return f"{evidence}: rank {first} first"[:MAX_HINT_TEXT]


def derive_hint(dimension, windows):
    """One dimension's hint from the scorecard windows, or ``None`` without evidence."""
    spec = DIMENSIONS[dimension]
    for window in WINDOWS:
        buckets = ((windows or {}).get(window) or {}).get(spec["lines"]) or {}
        eligible = []
        for value in spec["values"]:
            row = buckets.get(value) or {}
            total, rate = int(row.get(spec["count"]) or 0), _decimal(row.get(spec["metric"]))
            if total >= MIN_SAMPLE and rate is not None:
                eligible.append((value, rate, total))
        if len(eligible) < 2:
            continue  # Not enough evidence in this window; a longer one may have it.
        eligible.sort(key=lambda row: (-row[1], spec["values"].index(row[0])))
        if eligible[0][1] - eligible[-1][1] < MIN_SPREAD:
            return None  # Enough evidence, and no difference worth ranking by.
        prefer = [value for value, rate, _n in eligible if rate > eligible[-1][1]]
        return {
            "dimension": dimension, "prefer": prefer, "window": window,
            "metric": spec["metric"],
            "evidence": {value: {"n": total, "rate": _text(rate)}
                         for value, rate, total in eligible},
            "text": _hint_text(dimension, eligible, prefer, window),
        }
    return None


def derive_hints(lessons):
    windows = (lessons or {}).get("windows") or {}
    return [hint for hint in (derive_hint(dimension, windows) for dimension in DIMENSIONS)
            if hint is not None]


def emphasis_doc(lessons, *, context_as_of, now, unavailable=None):
    """``emphasis.json``. Without usable lessons (``unavailable`` names why) it holds no
    hints, and ``build --lessons`` builds exactly as without it."""
    hints = derive_hints(lessons)
    return {
        "schema": EMPHASIS_SCHEMA, "derived_at": now.astimezone(UTC).isoformat(),
        "context_as_of": context_as_of,
        "lessons_as_of": (lessons or {}).get("as_of"),
        "scorecard_day": (lessons or {}).get("scorecard_day"),
        "unavailable": unavailable,
        "rules": {"min_sample": MIN_SAMPLE, "min_spread": str(MIN_SPREAD),
                  "windows": list(WINDOWS)},
        "use": USE,
        "hints": hints,
    }


def load_emphasis(doc):
    """The hints of an ``emphasis.json``, checked: known dimensions and values, no key but
    the known ones, so a hint can only reorder."""
    if not isinstance(doc, dict) or doc.get("schema") != EMPHASIS_SCHEMA:
        raise LessonsError(f"EMPHASIS_UNKNOWN: not a {EMPHASIS_SCHEMA} file")
    hints = doc.get("hints")
    if not isinstance(hints, list):
        raise LessonsError("EMPHASIS_INVALID: hints must be a list")
    seen = set()
    for index, hint in enumerate(hints):
        where = f"hints[{index}]"
        if not isinstance(hint, dict) or not set(hint) <= HINT_KEYS:
            raise LessonsError(f"EMPHASIS_INVALID: {where} may only hold {sorted(HINT_KEYS)} "
                               "(ordering hints only)")
        spec = DIMENSIONS.get(hint.get("dimension"))
        prefer = hint.get("prefer")
        if spec is None or hint["dimension"] in seen:
            raise LessonsError(f"EMPHASIS_INVALID: {where}.dimension unknown or repeated")
        if (not isinstance(prefer, list) or not prefer or len(set(prefer)) != len(prefer)
                or any(value not in spec["values"] for value in prefer)):
            raise LessonsError(f"EMPHASIS_INVALID: {where}.prefer must list distinct values "
                               f"of {list(spec['values'])}")
        text = hint.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_HINT_TEXT:
            raise LessonsError(f"EMPHASIS_INVALID: {where}.text must be 1-{MAX_HINT_TEXT} "
                               "characters")
        if "rule-" in text.lower():
            raise LessonsError(f"EMPHASIS_INVALID: {where}.text may not write a level-rule "
                               "tag; the scorecard reads those from why_over_peers")
        seen.add(hint["dimension"])
    return tuple(hints)


def rank_key(attributes, hints):
    """Where a pick sorts under ``hints``: its place in each hint's ``prefer`` list, in hint
    order, anything not preferred after every preferred value."""
    return tuple(hint["prefer"].index(attributes[hint["dimension"]])
                 if attributes[hint["dimension"]] in hint["prefer"] else len(hint["prefer"])
                 for hint in hints)


def promoted_by(attributes, hints):
    return [hint for hint in hints if attributes[hint["dimension"]] in hint["prefer"]]


def lesson_sentence(hints):
    """The ``why_over_peers`` addition for a pick a lesson ranked early (plan 5, V6)."""
    if not hints:
        return None
    return ("Ranked early by a lesson from recent results: "
            + "; ".join(hint["text"] for hint in hints) + ".")


# --- The plain summary ------------------------------------------------------------------------

def _rate_line(label, buckets, *, count, metric, values):
    parts = []
    for value in values:
        row = (buckets or {}).get(value)
        if row:
            parts.append(f"{value} {_text(row.get(metric))} over {row.get(count, 0)}")
    return f"  {label}: " + ("; ".join(parts) if parts else "no picks yet")


def _window_lines(name, lines):
    if not lines:
        return [f"{WINDOW_WORDS[name].capitalize()}: no scorecard yet"]
    funnel, results = lines.get("funnel") or {}, lines.get("results") or {}
    out = [
        f"{WINDOW_WORDS[name].capitalize()} ({lines.get('start')} to {lines.get('end')}):",
        (f"  funnel: {funnel.get('picks_sent', 0)} picks sent, {funnel.get('accepted', 0)} "
         f"accepted, {funnel.get('vetoed', 0)} vetoed, {funnel.get('selected', 0)} selected, "
         f"{funnel.get('admitted', 0)} admitted, {funnel.get('triggered', 0)} triggered, "
         f"{funnel.get('filled', 0)} filled, {funnel.get('closed', 0)} closed"),
        (f"  results: {results.get('trades_closed', 0)} trades closed, {results.get('wins', 0)} "
         f"wins, {results.get('losses', 0)} losses, mean R after fees "
         f"{_text(results.get('mean_r_net'))} over {results.get('r_net_count', 0)}"),
        _rate_line("price reached the entry, by distance", lines.get("fill_rate_by_distance"),
                   count="shadow_recorded", metric="shadow_trigger_rate",
                   values=(*DISTANCE_BUCKETS, "UNKNOWN")),
        _rate_line("real fills over admitted, by distance", lines.get("fill_rate_by_distance"),
                   count="admitted", metric="fill_rate", values=DISTANCE_BUCKETS),
        _rate_line("shadow hit rate by timeframe", lines.get("results_by_timeframe"),
                   count="shadow_r_count", metric="shadow_hit_rate",
                   values=(*TIMEFRAME_WORDS, "OTHER", "NONE")),
        _rate_line("shadow hit rate by level rule", lines.get("results_by_rule"),
                   count="shadow_r_count", metric="shadow_hit_rate",
                   values=("A", "B", "UNDECLARED")),
        _rate_line("shadow hit rate by kind", lines.get("results_by_kind"),
                   count="shadow_r_count", metric="shadow_hit_rate", values=tuple(KIND_WORDS)),
    ]
    for key, label, part in (("excerpt_drop_rate", "excerpts refused at intake", "dropped"),
                             ("stale_news_vetoes", "stale-news vetoes", "vetoed"),
                             ("dossier_size_rejections", "dossier-size rejections", "rejected")):
        row = lines.get(key) or {}
        total = row.get("picks_sent", row.get("news_picks_ranked", 0))
        out.append(f"  {label}: {row.get(part, 0)} of {total} (rate {_text(row.get('rate'))})")
    causes = lines.get("causes") or {}
    out.append(f"  notable trades: {causes.get('notable_trades', 0)}, with a post-mortem "
               f"{causes.get('with_post_mortem', 0)}; causes {causes.get('by_cause') or {}}; "
               f"knowable before the move {causes.get('knowable_before_move') or {}} "
               "(craft lessons should leave out MARKET_WIDE and SURPRISE cases)")
    return out


def _outlook_lines(outlook):
    outlook = outlook or {}
    if outlook.get("status") != "GRADED":
        return ["Outlook: no graded outlook yet (each is graded on the 24 hours after it is "
                "received, the night after that window ends)"]
    out = [(f"Outlook graded on {outlook.get('day')} (received {outlook.get('received_at')}, "
            f"window {outlook.get('window_start')} to {outlook.get('window_end')}): "
            f"{outlook.get('hits', 0)} direction hits of {outlook.get('compared', 0)} compared "
            f"(hit rate {_text(outlook.get('hit_rate'))}), {outlook.get('skipped', 0)} skipped")]
    for bucket in outlook.get("calibration") or []:
        out.append(f"  confidence {bucket.get('bucket')}: {bucket.get('hits', 0)} of "
                   f"{bucket.get('count', 0)} (mean confidence "
                   f"{_text(bucket.get('mean_confidence'))})")
    for label, rows in (("misses", outlook.get("misses")),
                        ("false alarms", outlook.get("false_alarms"))):
        listed = ", ".join(f"{row.get('symbol')} {row.get('return_pct')}% (said "
                           f"{row.get('outlook_direction')})" for row in rows or [])
        out.append(f"  {label}: {listed or 'none'}")
    for name, window in sorted((outlook.get("by_window") or {}).items()):
        out.append(f"  {name}: {window.get('hits', 0)} hits of {window.get('compared', 0)} over "
                   f"{window.get('outlooks_graded', 0)} graded outlooks (hit rate "
                   f"{_text(window.get('hit_rate'))}), {window.get('misses', 0)} misses, "
                   f"{window.get('false_alarms', 0)} false alarms")
    return out


def _pending_lines(pending):
    pending = pending or {}
    trades, movers = pending.get("trades") or [], pending.get("movers") or []
    out = [f"Post-mortems owed: {len(trades)} trades, {len(movers)} movers"]
    out.extend(f"  trade {row.get('symbol')} {row.get('setup_id')}: {row.get('exit_reason')}, "
               f"R after fees {row.get('r_net')}, notable for "
               f"{', '.join(row.get('notable_reasons') or []) or 'n/a'}" for row in trades)
    out.extend(f"  mover {row.get('symbol')} {row.get('day')}: {row.get('return_pct')}%"
               + (" (a miss)" if row.get("was_miss") else "") for row in movers)
    return out


def summary_lines(lessons, hints, *, unavailable=None):
    if unavailable and unavailable != "NO_LESSONS":
        return [f"Lessons unavailable this run ({unavailable}): the app could not read a "
                "learning record into lessons, and served the rest of the context. No "
                "emphasis today; research the whole market as usual."]
    if not lessons:
        return ["No lessons in this context (a V1 context, the offline fallback, or a "
                "credential that reads lessons: null). Nothing to change today."]
    out = [f"Lessons ({lessons.get('lessons_version')}) as of {lessons.get('as_of')}, "
           f"scorecard day {lessons.get('scorecard_day')}"]
    windows = lessons.get("windows") or {}
    for name in ("1d", "7d", "30d"):
        out.extend(_window_lines(name, windows.get(name)))
    out.extend(_outlook_lines(lessons.get("outlook")))
    days = lessons.get("recent_days") or []
    out.append("Recorded market days: " + (", ".join(
        f"{day.get('day')} ({len(day.get('movers') or [])} movers, share "
        f"{_text(day.get('mover_share'))})" for day in days) or "none yet"))
    out.extend(_pending_lines(lessons.get("pending_post_mortems")))
    out.append("Emphasis for today (ordering only; every coin is still covered):")
    out.extend(f"  {hint['text']}" for hint in hints)
    if not hints:
        out.append(f"  none: no dimension has {MIN_SAMPLE}+ resolved picks in two buckets with a "
                   f"spread of {MIN_SPREAD} or more")
    return out
