"""``RESEARCH_LESSONS_V1``: a research agent's own learning record, the ``lessons`` section of
``RESEARCH_CONTEXT_V2`` (package learning-app, 2026-09-28; plan ``docs/LEARNING-LOOP-PLAN.md``
section 5, owner decision of 2026-09-28: "Muse sees only its own sanitized lessons").

Read-only, at request time, from the immutable learning records: the caller's own lines of the
latest ``DAILY_SCORECARD_V1`` (1, 7 and 30 days), its outlooks' forward-window grades from the
``MARKET_REALITY_V1`` records, the last seven reality days' movers and factors (market data, the
same for every agent) and its trades and movers still without an accepted post-mortem.

Sanitized: never another agent's lines, grades, notes or trades; never a Jev answer, receipt,
the randomized arm, costs or an ``ENGINEERING_TEST`` setup. Lessons are served to research agents
only and never reach Jev.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from catalyst_lab.learning_intake import POST_MORTEM_EVENT
from catalyst_lab.managed_engineering import ENGINEERING_PURPOSE
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.market_reality import CALIBRATION_BUCKETS, REALITY_EVENT
from catalyst_lab.repository import json_safe
from catalyst_lab.scorecard import SCORECARD_EVENT, exit_kind, notable_reasons, rate

LESSONS_VERSION = "RESEARCH_LESSONS_V1"
WINDOW_KEYS = ("1d", "7d", "30d")
PENDING_TRADE_DAYS = 7
RECENT_DAYS = 7
GRADE_WINDOWS = (("7d", 7), ("30d", 30))
MAX_PENDING_TRADES = 50


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return parsed.astimezone(UTC)


def _owned(alias):
    """The caller's records (research_context's rule): its agent's, and unattributed legacy
    records for the legacy credential only."""
    return (f"(({alias}->'agent'->>'agent_id')=%(agent)s OR "
            f"(%(legacy)s AND ({alias}->'agent'->>'agent_id') IS NULL))")


def scorecard_lines(conn, agent_id):
    """``(day, {1d, 7d, 30d: lines})`` of the latest recorded scorecard; lines are the caller's
    own (``None`` for a window it has none in)."""
    row = conn.execute(
        """SELECT body->>'day' AS day,
        body->'windows'->'1d'->'agents'->%(agent)s AS d1,
        body->'windows'->'7d'->'agents'->%(agent)s AS d7,
        body->'windows'->'30d'->'agents'->%(agent)s AS d30
        FROM lab.managed_events WHERE kind=%(kind)s AND setup_id IS NULL
        ORDER BY body->>'day' DESC, event_seq DESC LIMIT 1""",
        {"agent": agent_id, "kind": SCORECARD_EVENT},
    ).fetchone()
    if row is None:
        return None, None
    return row["day"], {"1d": row["d1"], "7d": row["d7"], "30d": row["d30"]}


def reality_days(conn, agent_id, since):
    """Every recorded reality day from ``since`` on, newest first, with only the caller's
    grades and the market-wide fields the lessons show."""
    return conn.execute(
        """SELECT body->>'day' AS day, body->'universe'->'count' AS universe_count,
        body->'measured_count' AS measured_count, body->'mover_share' AS mover_share,
        body->'movers' AS movers, body->'factors' AS factors,
        body->'outlook_agents' AS outlook_agents,
        (SELECT coalesce(jsonb_agg(g ORDER BY g->>'received_at'), '[]'::jsonb)
         FROM jsonb_array_elements(body->'grades') g WHERE g->>'agent_id'=%(agent)s) AS grades
        FROM lab.managed_events WHERE kind=%(kind)s AND setup_id IS NULL
        AND body->>'day' >= %(since)s ORDER BY body->>'day' DESC""",
        {"agent": agent_id, "kind": REALITY_EVENT, "since": since.isoformat()},
    ).fetchall()


def noted_subjects(conn, agent_id):
    rows = conn.execute(
        """SELECT body->'items' AS items FROM lab.managed_events
        WHERE kind=%s AND setup_id IS NULL AND body->'agent'->>'agent_id'=%s""",
        (POST_MORTEM_EVENT, agent_id),
    ).fetchall()
    return {item["subject_key"] for row in rows for item in row["items"] or []}


def _calibration(grades):
    totals = {label: {"count": 0, "hits": 0, "confidence_sum": D(0)}
              for label, _, _ in CALIBRATION_BUCKETS}
    for grade in grades:
        for row in grade.get("calibration") or []:
            entry = totals.get(row["bucket"])
            if entry is None:
                continue
            entry["count"] += row["count"]
            entry["hits"] += row["hits"]
            entry["confidence_sum"] += D(str(row.get("confidence_sum") or 0))
    return [{"bucket": label, "count": entry["count"], "hits": entry["hits"],
             "hit_rate": rate(entry["hits"], entry["count"]),
             "mean_confidence": rate(entry["confidence_sum"], entry["count"])}
            for label, entry in totals.items()]


def _public_calibration(rows):
    return [{k: row[k] for k in ("bucket", "count", "hits", "hit_rate", "mean_confidence")}
            for row in rows or []]


def _brief(item):
    return {k: item.get(k) for k in ("symbol", "return_pct", "move_start_at",
                                     "outlook_direction", "outlook_confidence")}


def outlook_section(days):
    """The caller's forward-window grades: the latest outlook in full, the last 7 days' graded
    outlooks and 7- and 30-day sums (by grading day)."""
    grades = [grade for day in days for grade in day["grades"] or []]
    if not grades:
        return {"status": "NO_GRADED_OUTLOOK_YET", "day": None, "graded_outlooks": [],
                "by_window": {name: None for name, _ in GRADE_WINDOWS}}
    grades.sort(key=lambda g: (g["grading_day"], g["received_at"]), reverse=True)
    latest = grades[0]
    newest = date.fromisoformat(latest["grading_day"])

    def within(grade, days_back):
        return date.fromisoformat(grade["grading_day"]) > newest - timedelta(days=days_back)

    by_window = {}
    for name, days_back in GRADE_WINDOWS:
        members = [g for g in grades if within(g, days_back)]
        compared = sum(g["compared"] for g in members)
        hits = sum(g["hits"] for g in members)
        by_window[name] = {
            "outlooks_graded": len(members), "compared": compared, "hits": hits,
            "hit_rate": rate(hits, compared),
            "misses": sum(len(g["misses"]) for g in members),
            "false_alarms": sum(len(g["false_alarms"]) for g in members),
            "calibration": _calibration(members),
        }
    return {
        "status": "GRADED", "day": latest["grading_day"], "outlook_id": latest["outlook_id"],
        "received_at": latest["received_at"], "window_start": latest["window_start"],
        "window_end": latest["window_end"], "compared": latest["compared"],
        "hits": latest["hits"], "hit_rate": latest["hit_rate"], "skipped": latest["skipped"],
        "calibration": _public_calibration(latest["calibration"]),
        "misses": [_brief(m) for m in latest["misses"]],
        "false_alarms": [_brief(f) for f in latest["false_alarms"]],
        "graded_outlooks": [
            {"outlook_id": g["outlook_id"], "received_at": g["received_at"],
             "window_start": g["window_start"], "window_end": g["window_end"],
             "grading_day": g["grading_day"], "compared": g["compared"], "hits": g["hits"],
             "hit_rate": g["hit_rate"], "misses": len(g["misses"]),
             "false_alarms": len(g["false_alarms"])}
            for g in grades if within(g, RECENT_DAYS)],
        "by_window": by_window,
    }


def _mover(mover):
    return {k: mover.get(k) for k in ("symbol", "return_pct", "move_start_at", "top_up",
                                      "top_down", "big_move")}


def recent_days_section(days):
    result = []
    for day in days[:RECENT_DAYS]:
        factors = day["factors"] or {}
        result.append({
            "day": day["day"], "universe_count": day["universe_count"],
            "measured_count": day["measured_count"], "mover_share": day["mover_share"],
            "movers": [_mover(m) for m in day["movers"] or []],
            "factors": {k: factors.get(k) for k in ("btc_return_pct", "eth_return_pct",
                                                    "sectors", "total_volume_usd",
                                                    "total_volume_vs_7d_avg")},
        })
    return result


def was_miss(mover, agent_id, outlook_agents):
    """A big mover counts against the caller when its covering outlook said FLAT, the other way
    or SKIPPED, or when the caller had no outlook at all (``MARKET_REALITY_V1``)."""
    if not mover.get("big_move"):
        return False
    if agent_id in (outlook_agents or []):
        return agent_id in (mover.get("missed_by") or [])
    return True


def pending_movers(days, agent_id, noted):
    if not days:
        return []
    latest = days[0]
    return [{"symbol": m["symbol"], "day": latest["day"], "return_pct": m["return_pct"],
             "move_start_at": m["move_start_at"],
             "was_miss": was_miss(m, agent_id, latest["outlook_agents"])}
            for m in latest["movers"] or []
            if f"MOVER:{latest['day']}:{m['symbol']}" not in noted]


def pending_trades(repository, conn, agent_id, legacy, noted, now):
    rows = conn.execute(
        f"""SELECT s.setup_id, s.symbol, t.body AS state
        FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
        WHERE t.body->>'state'='CLOSED'
        AND (CASE WHEN t.body ? 'closed_at' THEN (t.body->>'closed_at')::timestamptz END)
            >= %(since)s
        AND EXISTS(SELECT 1 FROM lab.managed_fills f WHERE f.setup_id=s.setup_id
                   AND f.side='buy')
        AND coalesce(s.record_json->>'purpose','')<>%(engineering)s
        AND {_owned('s.record_json')}
        ORDER BY s.event_seq DESC LIMIT %(limit)s""",
        {"agent": agent_id, "legacy": legacy, "engineering": ENGINEERING_PURPOSE,
         "since": now - timedelta(days=PENDING_TRADE_DAYS), "limit": MAX_PENDING_TRADES},
    ).fetchall()
    trades = []
    for row in rows:
        if f"TRADE:{row['setup_id']}" in noted:
            continue
        state = row["state"] or {}
        measured = managed_measurement(repository, row["setup_id"])
        tainted = any(item.get("source") == "LAB_FIXTURE"
                      for item in measured.get("cost_evidence") or [])
        r_net = None if tainted or measured.get("official_r") is None \
            else D(str(measured["official_r"]))
        reason = state.get("reason") or state.get("exit_requested")
        gross = measured.get("gross_realized_pnl")
        risk = measured.get("planned_filled_risk")
        notable_r = r_net if r_net is not None else (
            D(str(gross)) / D(str(risk)) if gross is not None and risk else None)
        fills = conn.execute(
            """SELECT min(filled_at) FILTER(WHERE side='buy') AS entry_at,
            max(filled_at) FILTER(WHERE side='sell') AS exit_at
            FROM lab.managed_fills WHERE setup_id=%s""", (row["setup_id"],),
        ).fetchone()
        trades.append({
            "setup_id": row["setup_id"], "symbol": row["symbol"],
            "entry_at": fills["entry_at"], "entry_price": measured.get("entry_fill_average"),
            "exit_at": fills["exit_at"], "exit_price": measured.get("exit_fill_average"),
            "exit_reason": reason, "r_net": r_net,
            "notable_reasons": notable_reasons(exit_kind(reason), notable_r),
        })
    return trades


def safe_lessons(repository, agent_id, *, legacy=False, now):
    """``lessons_for``, or, when a learning record cannot be read into it (a record this code
    does not understand), ``{lessons_version, agent_id, as_of, available: false, code}``: the
    research context keeps serving and research continues. A database error is not hidden: it
    answers 503 like every other part of the context."""
    import psycopg

    try:
        return lessons_for(repository, agent_id, legacy=legacy, now=now)
    except psycopg.Error:
        raise
    except Exception:  # noqa: BLE001 -- the lessons never block the research context.
        return json_safe({"lessons_version": LESSONS_VERSION, "agent_id": agent_id,
                          "as_of": _aware(now), "available": False,
                          "code": "LESSONS_UNAVAILABLE"})


def lessons_for(repository, agent_id, *, legacy=False, now):
    """The caller's ``RESEARCH_LESSONS_V1`` section as of ``now``."""
    now = _aware(now)
    with repository.connect() as conn:
        day, windows = scorecard_lines(conn, agent_id)
        since = now.date() - timedelta(days=GRADE_WINDOWS[-1][1] + 2)
        days = reality_days(conn, agent_id, since)
        noted = noted_subjects(conn, agent_id)
        trades = pending_trades(repository, conn, agent_id, legacy, noted, now)
    return json_safe({
        "lessons_version": LESSONS_VERSION, "agent_id": agent_id, "as_of": now,
        "available": True, "scorecard_day": day, "windows": windows,
        "outlook": outlook_section(days),
        "recent_days": recent_days_section(days),
        "pending_post_mortems": {"trades": trades,
                                 "movers": pending_movers(days, agent_id, noted)},
    })


__all__ = ["LESSONS_VERSION", "lessons_for", "outlook_section", "safe_lessons", "was_miss"]
