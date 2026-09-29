"""Plain summaries and compact JSON of the learning records for the owner's commands (package
learning-app): ``cloud_runtime scorecard``, ``market-review`` and ``weekly-review``. Pure: each
function takes a recorded (or previewed) body and returns ``(text, data)``. The morning chat
message the owner receives is built from these outputs; the public page is unchanged.
"""

WINDOW_LABELS = (("1d", "Day"), ("7d", "7 days"), ("30d", "30 days"))


def _value(value, missing="n/a"):
    return missing if value is None else str(value)


def _compact_overall(overall):
    """A window's overall sections without per-trade, per-change or per-cause item lists."""
    data = {k: v for k, v in overall.items() if k not in {"trades"}}
    if isinstance(data.get("maintenance"), dict):
        data["maintenance"] = {k: v for k, v in data["maintenance"].items() if k != "items"}
    if isinstance(data.get("causes"), dict):
        data["causes"] = {k: v for k, v in data["causes"].items() if k != "items"}
    return data


def scorecard_summary(body, *, recorded, event_seq=None):
    lines = [f"Scorecard {body['day']} ({body['scorecard_version']}, "
             f"{'recorded' if recorded else 'preview, not recorded'}, computed "
             f"{body['computed_at']})."]
    for key, label in WINDOW_LABELS:
        overall = body["windows"][key]["overall"]
        f, r, s = overall["funnel"], overall["results"], overall["selection"]
        lines.append(
            f"{label}: {f['cycles']} runs, {f['picks_sent']} picks sent, {f['accepted']} "
            f"accepted, {f['ranked']} ranked, {f['vetoed']} vetoed, {f['selected']} selected, "
            f"{f['admitted']} admitted, {f['filled']} filled, {f['closed']} closed.")
        lines.append(
            f"  Trades: {r['trades_closed']} closed, {r['wins']} won, {r['losses']} lost; "
            f"R after fees {_value(r['mean_r_net'])} mean over {r['r_net_count']} verified "
            f"({r['fees_unverified']} awaiting fee evidence).")
        chosen = _value(s["selected"]["mean_shadow_r_net"])
        lines.append(
            f"  Jev's selection (shadow net R): selected {chosen}"
            f" ({s['selected']['shadow_r_count']}), passed "
            f"{_value(s['passed']['mean_shadow_r_net'])} ({s['passed']['shadow_r_count']}), "
            f"vetoed {_value(s['vetoed']['mean_shadow_r_net'])} ({s['vetoed']['shadow_r_count']}).")
    day = body["windows"]["1d"]["overall"]
    jev, fees = day["costs"]["jev"], day["costs"]["fees"]
    lines.append(f"Costs (day): Jev {jev['calls']} calls, about ${_value(jev['estimated_usd'])}; "
                 f"fees ${_value(fees['verified_cash_fee_usd'], '0')} verified on "
                 f"{fees['verified_trades']} of {fees['closed_trades']} closed trades.")
    arms = {k: v for k, v in day["arms"].items() if isinstance(v, dict)}
    if arms:
        lines.append("Arms (day): " + "; ".join(
            f"{name} {a['closed']} closed, mean R {_value(a['mean_r_net'])}"
            for name, a in sorted(arms.items())) + ".")
    causes = day["causes"]
    lines.append(f"Notable trades (day): {causes['notable_trades']}, "
                 f"{causes['with_post_mortem']} with a post-mortem.")
    week = body["windows"]["7d"]["overall"]["maintenance"]
    moved = [f"{name} {m['changes']} (mean R difference {_value(m['mean_r_difference'])})"
             for name, m in week.items() if isinstance(m, dict) and m.get("changes")]
    lines.append("Maintenance (7 days): " + ("; ".join(moved) if moved else "no replayed change")
                 + ".")
    data = {
        "scorecard_version": body["scorecard_version"], "day": body["day"],
        "computed_at": body["computed_at"], "recorded": recorded, "event_seq": event_seq,
        "windows": {key: _compact_overall(body["windows"][key]["overall"])
                    for key, _ in WINDOW_LABELS},
        "agents": sorted(body["windows"]["30d"]["agents"]),
    }
    return "\n".join(lines), data


def reality_summary(body, *, recorded, event_seq=None):
    factors = body["factors"]
    lines = [f"Market {body['day']} ({body['reality_version']}, "
             f"{'recorded' if recorded else 'preview, not recorded'}): "
             f"{body['measured_count']} of {body['universe']['count']} coins measured; "
             f"Bitcoin {_value(factors.get('btc_return_pct'))}%, Ether "
             f"{_value(factors.get('eth_return_pct'))}%, volume vs 7-day average "
             f"{_value(factors.get('total_volume_vs_7d_avg'))}."]
    movers = body["movers"]
    if movers:
        lines.append("Movers: " + ", ".join(
            f"{m['symbol']} {m['return_pct']}%" + (" (missed by " + ", ".join(m['missed_by'])
                                                   + ")" if m.get("missed_by") else "")
            for m in movers) + ".")
    sectors = factors.get("sectors") or []
    if sectors:
        lines.append("Sectors: " + ", ".join(f"{s['sector']} {s['mean_return_pct']}%"
                                             for s in sectors) + ".")
    for grade in body["grades"]:
        lines.append(
            f"Outlook of {grade['agent_id']} ({grade['window_start']} to {grade['window_end']}):"
            f" {grade['hits']} of {grade['compared']} directions right "
            f"({_value(grade['hit_rate'])}), {len(grade['misses'])} misses, "
            f"{len(grade['false_alarms'])} false alarms, {grade['skipped']} skipped.")
    if not body["grades"]:
        lines.append("No outlook was graded this day.")
    data = {
        "reality_version": body["reality_version"], "day": body["day"], "recorded": recorded,
        "event_seq": event_seq, "measured_count": body["measured_count"],
        "unmeasured": body["unmeasured"], "movers": movers, "mover_share": body["mover_share"],
        "factors": factors, "outlook_agents": body["outlook_agents"],
        "grades": [{k: v for k, v in g.items() if k != "coins"} for g in body["grades"]],
    }
    return "\n".join(lines), data


def review_summary(body, *, recorded, event_seq=None):
    lines = [f"Weekly review {body['week_start']} to {body['week_end']} "
             f"({body['review_version']}, {'recorded' if recorded else 'preview, not recorded'},"
             f" computed {body['computed_at']})."]
    for test in body["tests"]:
        interval = test["interval_90"]
        span = (f", 90% interval {interval['lower']} to {interval['upper']}" if interval
                else "")
        samples = ", ".join(f"{k} {v}" for k, v in test["samples"].items())
        line = (f"{test['test_id']}: {test['verdict']} (effect {_value(test['effect'])}{span}; "
                f"samples {samples})")
        if test["proposal"]:
            line += f"; proposes {test['proposal']['version']}"
        lines.append(line + ".")
    data = {k: body[k] for k in ("review_version", "week_start", "week_end", "computed_at",
                                 "tests", "method")}
    data.update(recorded=recorded, event_seq=event_seq)
    return "\n".join(lines), data


__all__ = ["reality_summary", "review_summary", "scorecard_summary"]
