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
* since package learning-loop2 (2026-10-03, plan L2) every dimension ranks by **net R per
  resolved pick** (``RANKING``): the shadow net R after the assumed fee summed over a bucket's
  resolved picks, a pick the price never reached counting 0 R, divided by those picks -- what
  sending a pick of that bucket earned. The trigger rate (how often price reached the entry) is
  kept beside it as the secondary figure; it no longer decides, since it favoured easy fills
  over profitable ones. A scorecard written before the per-pick figure existed is read from its
  mean net R x the triggered count / the resolved count (the same quantity, rounded).

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
    "distance_bucket": {"values": DISTANCE_BUCKETS, "lines": "fill_rate_by_distance"},
    "timeframe": {"values": tuple(TIMEFRAME_WORDS), "lines": "results_by_timeframe"},
    "rule": {"values": tuple(RULE_WORDS), "lines": "results_by_rule"},
    "kind": {"values": tuple(KIND_WORDS), "lines": "results_by_kind"},
}
RANKING = "NET_R_PER_RESOLVED_PICK_V1"  # Package learning-loop2 (L2).
METRIC = "shadow_r_net_per_resolved_pick"
SECONDARY = "shadow_trigger_rate"
WINDOWS = ("7d", "30d")  # The 30-day lines only when the 7-day ones lack the evidence.
WINDOW_WORDS = {"1d": "the last day", "7d": "the last 7 days", "30d": "the last 30 days"}
MIN_SAMPLE = 5
MIN_SPREAD = Decimal("0.10")  # R per pick between the best and the worst bucket.
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


def _signed(value):
    text = _text(value)
    return text if text.startswith("-") or text == "n/a" else f"+{text}"


def net_per_pick(row):
    """``(net R per resolved pick, resolved picks, trigger rate)`` of one scorecard bucket, or
    ``(None, n, rate)``. Read from ``shadow_r_net_per_resolved_pick`` / ``shadow_resolved``;
    a scorecard without them gives mean x triggered / recorded over ``shadow_recorded``."""
    rate = _decimal(row.get(SECONDARY))
    if row.get(METRIC) is not None:
        return _decimal(row[METRIC]), int(row.get("shadow_resolved") or 0), rate
    recorded, mean = int(row.get("shadow_recorded") or 0), _decimal(row.get("mean_shadow_r_net"))
    count = int(row.get("shadow_r_count") or 0)
    if not recorded:
        return None, 0, rate
    if mean is None:
        return (Decimal(0), recorded, rate) if count == 0 else (None, recorded, rate)
    return mean * count / recorded, recorded, rate


def _hint_text(dimension, eligible, prefer, window):
    best, worst = eligible[0], eligible[-1]

    def part(row):
        value, net, total, rate = row
        reached = f" (entry reached {_count(rate, total)})" if rate is not None else ""
        return f"{_describe(dimension, value)} {_signed(net)}R a pick over {total}{reached}"

    first = ", ".join(_describe(dimension, value) for value in prefer)
    text = f"{part(best)}; {part(worst)}; {WINDOW_WORDS[window]}: rank {first} first"
    if len(text) > MAX_HINT_TEXT:  # The short form keeps the decision and the figures.
        text = (f"{_describe(dimension, best[0])} {_signed(best[1])}R a pick over {best[2]}, "
                f"{_describe(dimension, worst[0])} {_signed(worst[1])}R over {worst[2]}, "
                f"{window}: rank {first} first")
    return text[:MAX_HINT_TEXT]


def derive_hint(dimension, windows):
    """One dimension's hint from the scorecard windows, or ``None`` without evidence: the
    buckets ranked by net R per resolved pick (``RANKING``), the trigger rate beside it."""
    spec = DIMENSIONS[dimension]
    for window in WINDOWS:
        buckets = ((windows or {}).get(window) or {}).get(spec["lines"]) or {}
        eligible = []
        for value in spec["values"]:
            net, total, rate = net_per_pick(buckets.get(value) or {})
            if total >= MIN_SAMPLE and net is not None:
                eligible.append((value, net, total, rate))
        if len(eligible) < 2:
            continue  # Not enough evidence in this window; a longer one may have it.
        eligible.sort(key=lambda row: (-row[1], spec["values"].index(row[0])))
        if eligible[0][1] - eligible[-1][1] < MIN_SPREAD:
            return None  # Enough evidence, and no difference worth ranking by.
        prefer = [row[0] for row in eligible if row[1] > eligible[-1][1]]
        return {
            "dimension": dimension, "prefer": prefer, "window": window, "metric": METRIC,
            "evidence": {value: {"n": total, "net_r_per_pick": _text(net),
                                 "trigger_rate": _text(rate) if rate is not None else None}
                         for value, net, total, rate in eligible},
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
                  "windows": list(WINDOWS), "ranking": RANKING, "metric": METRIC,
                  "secondary": SECONDARY},
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


def brief_lines(brief):
    """The app's latest daily brief (``DAILY_BRIEF_V1``'s agent view, package learning-loop2):
    the market, the movers and tomorrow's research focus. Attention only: no hint comes from it,
    and nothing in it narrows coverage."""
    if not brief:
        return ["Daily brief: none recorded yet (the nightly jobs write it after the day's "
                "market reality)."]
    market = brief.get("market") or {}
    out = [f"Daily brief {brief.get('day')}: {market.get('regime_tag')}. "
           f"{market.get('words') or ''}".rstrip()]
    for cluster in brief.get("sector_clusters") or []:
        out.append(f"  cluster: {cluster.get('sector')} {cluster.get('direction')} "
                   f"({', '.join(cluster.get('symbols') or [])})")
    for mover in brief.get("movers") or []:
        tradeable = mover.get("tradeable") or {}
        best = tradeable.get("best_net_r")
        out.append(f"  mover {mover.get('symbol')} {mover.get('return_pct')}% "
                   f"({mover.get('sector')}): {mover.get('knowability')}; mechanical signals "
                   f"{tradeable.get('signals', 0)}"
                   + (f", best simulated net R {best}" if best is not None else "")
                   + (" (hold pending)" if tradeable.get("pending") else ""))
    previous = brief.get("previous_day_missed_tradeable")
    if previous:
        out.append(f"  missed tradeable {previous.get('day')}: {previous.get('missed_tradeable')} "
                   f"of {previous.get('movers')} movers (mean simulated net R "
                   f"{previous.get('mean_net_r')})")
    out.append("Research focus for today (attention only; every coin is still covered):")
    out.extend(f"  {item.get('kind')}: {item.get('text')}"
               for item in brief.get("research_focus") or [])
    if not brief.get("research_focus"):
        out.append("  none from the brief")
    return out


def queue_lines(queue):
    """The post-mortems that still need an agent (``post_mortem_queue``)."""
    if not queue:
        return []
    return [f"Post-mortem queue ({queue.get('days')} days): {queue.get('movers_total', 0)} "
            f"movers and {len(queue.get('trades') or [])} notable trades need an agent's cited "
            "web research; the app computed the rest (market reality, regime, brief, paths)."]


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
    out.extend(queue_lines(lessons.get("post_mortem_queue")))
    if "daily_brief" in lessons:
        out.extend(brief_lines(lessons.get("daily_brief")))
    out.append("Emphasis for today (ordering only, by net R per resolved pick; every coin is "
               "still covered):")
    out.extend(f"  {hint['text']}" for hint in hints)
    if not hints:
        out.append(f"  none: no dimension has {MIN_SAMPLE}+ resolved picks in two buckets with a "
                   f"spread of {MIN_SPREAD}R a pick or more")
    return out
