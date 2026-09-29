"""Read-only research conversion and execution quality from retained event timestamps."""

from collections import Counter
from datetime import datetime
from decimal import Decimal as D

from catalyst_lab.agent_identity import attribution
from catalyst_lab.managed_analytics import LABEL
from catalyst_lab.managed_engineering import ENGINEERING_PURPOSE, is_engineering
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market import NY
from catalyst_lab.repository import json_safe

AGENT_GROUP_KEYS = ("agent_id", "agent_version", "guidelines_sha256")
ADDITIVE_COUNTS = ("cycle_count", "contender_count", "intake_rejected_count", "selected_count",
                   "admitted_count", "entered_count", "review_receipt_count",
                   "rationale_packet_count")
CLAIM_SUPPORT_NOTE = (
    "UNAVAILABLE: no Jev answer judges rationale claims under SKEPTIC_QUESTIONS_V1. "
    "unsupported_inference covers the whole packet, not the proposer's claims; a claim-level "
    "rationale_support answer arrives with approval rule V3 (plan 1.7)."
)


def agent_groups(items):
    """Funnel counts per agent, agent version and guideline hash over the given cycles.

    Legacy cycles form one LEGACY_UNATTRIBUTED bucket (null agent fields), never Muse's.
    Counts are additive, so groups from successive pages can be summed.
    """
    groups = {}
    for item in items:
        key = tuple(item[k] for k in AGENT_GROUP_KEYS)
        group = groups.setdefault(key, {
            "attribution": item["attribution"],
            **{k: item[k] for k in AGENT_GROUP_KEYS},
            **dict.fromkeys(ADDITIVE_COUNTS, 0),
            "market_counts": Counter(), "latest_revision_outcomes": Counter(),
            "reason_counts": Counter(),
            "rationale_claim_support_rate": None,
            "rationale_claim_support_note": CLAIM_SUPPORT_NOTE,
        })
        group["cycle_count"] += 1
        for field in ADDITIVE_COUNTS[1:]:
            group[field] += item[field]
        for field in ("market_counts", "latest_revision_outcomes", "reason_counts"):
            group[field].update(item[field])
    return [groups[key] for key in sorted(groups, key=lambda k: tuple(v or "" for v in k))]


def research_funnel(repository, *, after=0, limit=100):
    """Per-cycle conversion (ascending research-start cursor) plus ``agent_groups`` for the
    same page, grouped by agent ID, agent version and guideline hash."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("INVALID_ANALYTICS_CURSOR")
    with repository.connect() as conn:
        cycles = conn.execute("""SELECT event_seq,body,recorded_at FROM lab.managed_events
            WHERE kind='RESEARCH_STARTED' AND event_seq>%s ORDER BY event_seq LIMIT %s""",
            (after, limit)).fetchall()
        items = []
        for cycle in cycles:
            cid = cycle["body"]["cycle_id"]
            events = conn.execute("""SELECT kind,body,event_seq FROM lab.managed_events
                WHERE body->>'cycle_id'=%s ORDER BY event_seq""", (cid,)).fetchall()
            packets, decisions, selected, intake_rejected = {}, {}, {}, 0
            for event in events:
                body = event["body"]
                if event["kind"] == "RESEARCH_PACKET":
                    packets[body["item_key"]] = body
                elif event["kind"] == "RESEARCH_DECISION":
                    decisions[(body["item_key"], body["revision"])] = body
                elif event["kind"] == "RESEARCH_SELECTED":
                    p = body["packet"]
                    selected[(p["item_key"], p["revision"])] = p
                elif event["kind"] == "RESEARCH_ITEM_REJECTED_AT_INTAKE":
                    intake_rejected += 1
            outcomes, reasons = Counter(), Counter()
            for key, packet in packets.items():
                decision = decisions.get((key, packet["revision"]))
                outcomes[decision["disposition"] if decision else "PENDING"] += 1
                if decision and decision.get("reason"):
                    reasons[decision["reason"]] += 1
            # Operator ENGINEERING_TEST setups (plan 0.10) never count as research conversion.
            admitted = conn.execute("""SELECT count(*) AS setups,
                count(*) FILTER(WHERE EXISTS(SELECT 1 FROM lab.managed_fills f
                    WHERE f.setup_id=s.setup_id AND f.side='buy')) AS entered
                FROM lab.managed_setups s WHERE cycle_id=%s
                AND coalesce(s.record_json->>'purpose','')<>%s""",
                (cid, ENGINEERING_PURPOSE)).fetchone()
            latency = conn.execute("""SELECT count(*) AS receipt_count,
                avg(r.latency_ms) AS mean_ms,max(r.latency_ms) AS max_ms
                FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
                WHERE q.evidence_identity->>'cycle_id'=%s""", (cid,)).fetchone()
            items.append({
                "cycle_id": cid, "cursor_event_seq": cycle["event_seq"],
                "day": cycle["recorded_at"].astimezone(NY).date(),
                "market_counts": Counter(p["market"] for p in packets.values()),
                "contender_count": len(packets), "latest_revision_outcomes": outcomes,
                "reason_counts": reasons, "selected_count": sum(
                    (key, packet["revision"]) in selected for key, packet in packets.items()
                ),
                "admitted_count": admitted["setups"], "entered_count": admitted["entered"],
                "review_latency": latency,
                "origin": cycle["body"].get("research_origin", "LEGACY_APP_SCAN"),
                **attribution(cycle["body"].get("agent")),
                "intake_rejected_count": intake_rejected,
                "review_receipt_count": latency["receipt_count"],
                "rationale_packet_count": sum(
                    p.get("selection_rationale") is not None for p in packets.values()
                ),
            })
    return json_safe({"cohort": COHORT, "result_label": LABEL, "items": items,
                      "agent_groups": agent_groups(items), "agent_group_scope": "PAGE",
                      "next_cursor": cycles[-1]["event_seq"] if cycles else after})


def execution_quality(repository, setup_id):
    with repository.connect() as conn:
        setup = conn.execute("SELECT record_json FROM lab.managed_setups WHERE setup_id=%s",
                             (setup_id,)).fetchone()
        if not setup:
            raise ValueError("SETUP_NOT_FOUND")
        fills = conn.execute("""SELECT sum(qty*price)/sum(qty) AS average,min(filled_at) AS first
            FROM lab.managed_fills WHERE setup_id=%s AND side='buy'""", (setup_id,)).fetchone()
        authorization = conn.execute("""SELECT min(created_at) AS first
            FROM lab.managed_risk_decisions
            WHERE setup_id=%s AND action='ENTRY' AND outcome='APPROVED'""", (setup_id,)).fetchone()
        trigger = conn.execute("""SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind='TRIGGER_CONFIRMED' ORDER BY event_seq LIMIT 1""", (setup_id,)).fetchone()
    levels = setup["record_json"]["levels"]
    average = fills["average"]
    def duration(start):
        if not start or not fills["first"]:
            return None
        elapsed = (fills["first"] - start).total_seconds()
        return elapsed if elapsed >= 0 else None
    printed = datetime.fromisoformat(trigger["body"]["trade_at"]) \
        if trigger and trigger["body"].get("trade_at") else None
    return json_safe({
        "engineering": is_engineering(setup["record_json"]),
        "entry_fill_average": average,
        "slippage_vs_planned_trigger_bps": (average / D(levels["entry_trigger"]) - 1) * 10000
        if average else None,
        "slippage_vs_max_entry_bps": (average / D(levels["max_entry_price"]) - 1) * 10000
        if average else None,
        "trigger_to_first_fill_seconds": duration(printed),
        "authorization_to_first_fill_seconds": duration(authorization["first"]),
        "limitations": "Cross-clock negative intervals stay unknown. Planned-level slippage "
                        "is not broker execution shortfall or a counterfactual benchmark.",
    })
