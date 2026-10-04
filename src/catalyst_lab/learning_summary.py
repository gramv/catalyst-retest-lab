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


def _reasons(counts):
    return (": " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))) if counts else ""


def dimension_lines(dimensions, *, label):
    """The measured cells of ``dimensions`` (``result_dimensions``), else one line saying how
    many trades the cells hold and that they are below the minimum."""
    if not dimensions:
        return []
    measured = []
    for dimension, cells in dimensions["by_regime"].items():
        measured += [f"{dimension}={name} {c['mean_r_net']} R over {c['r_net_count']} of "
                     f"{c['closed']}" for name, c in cells.items() if c["status"] == "MEASURED"]
    measured += [f"{key} {c['mean_r_net']} R over {c['r_net_count']} of {c['closed']}"
                 for key, c in dimensions["by_a_versions"].items() if c["status"] == "MEASURED"]
    if measured:
        return [f"{label} (net R, fee-verified only): " + "; ".join(measured) + "."]
    return [f"{label}: NOT_ENOUGH_DATA in every cell (minimum {dimensions['minimum']} "
            f"fee-verified trades; {dimensions['tagged']} trades tagged)."]


def calibration_lines(section):
    """The weekly review's ``jev_calibration`` (JEV_CALIBRATION_V1) as text lines."""
    if not section:
        return []
    records, pending = section["records"], section["pending"]
    lines = [f"Jev calibration ({section['calibration_version']}): {records['selection']} pick "
             f"and {records['maintenance']} review records ({pending['selection']} picks and "
             f"{pending['maintenance']} reviews pending)."]
    for q in section["questions"]:
        bins = ", ".join(f"{b['bin']}: {b['count']}"
                         + (f" p {b['mean_p']} obs {b['observed']}" if b["count"] else "")
                         for b in q["reliability"])
        lines.append(
            f"  {q['source'].lower()} {q['question']} ({q['version']}) -> {q['event']}: "
            f"{q['status']}, {q['with_outcome']} with outcomes; Brier {_value(q['brier'])} vs "
            f"base rate {_value(q['base_rate_brier'])} (base rate {_value(q['base_rate'])}); "
            f"{bins}.")
    lift = section["ranking_lift"]
    line = (f"  Ranking lift ({lift['status']}, {lift['cycles']} complete cycles): Jev top K "
            f"{_value(lift['jev_top_k']['mean_net_r_per_pick'])} R per pick "
            f"({lift['jev_top_k']['picks']}), agent's top K "
            f"{_value(lift['agent_top_k']['mean_net_r_per_pick'])}, all picks "
            f"{_value(lift['random_k_expected']['mean_net_r_per_pick'])}")
    mechanical = lift["mechanical"]
    if mechanical.get("omitted"):
        line += f"; mechanical baseline omitted: {mechanical['reason']}"
    else:
        line += (f"; mechanical {mechanical['method']} top K "
                 f"{_value(mechanical['mechanical_top_k']['mean_net_r_per_pick'])} vs Jev "
                 f"{_value(mechanical['jev_top_k_same_cycles']['mean_net_r_per_pick'])} on "
                 f"{mechanical['cycles']} cycles")
    lines.append(line + ".")
    for exit_line in section["confirmed_exits"]:
        lines.append(f"  Confirmed exits {exit_line['question']} ({exit_line['version']}): "
                     f"{exit_line['count']}, mean exit minus hold "
                     f"{_value(exit_line['mean_exit_minus_hold_r'])} R.")
    lines += ["  " + text for text in section["thresholds_suggestion"]["text"]]
    return lines


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
            f"R after fees {_value(r['mean_r_net'])} mean over {r['r_net_count']} of "
            f"{r['trades_closed']} closed (fee-verified only; {r['fees_unverified']} awaiting "
            f"fee evidence{_reasons(r.get('fees_unverified_by_reason'))})"
            + (f"; gross R {_value(r['mean_gross_r'])} mean over {r['gross_r_count']}"
               if "mean_gross_r" in r else "") + ".")
        excluded = overall.get("stats_exclusions") or {}
        if excluded.get("excluded_trades"):
            x = overall["results_excluding_exclusions"]
            lines.append(
                f"  Excluding {excluded['excluded_trades']} trades of owner-excluded days "
                f"({', '.join(excluded['days'])}; STATS_EXCLUSION_V1): {x['trades_closed']} "
                f"closed, {x['wins']} won; R after fees {_value(x['mean_r_net'])} mean over "
                f"{x['r_net_count']}.")
        chosen = _value(s["selected"]["mean_shadow_r_net"])
        lines.append(
            f"  Jev's selection (shadow net R): selected {chosen}"
            f" ({s['selected']['shadow_r_count']}), passed "
            f"{_value(s['passed']['mean_shadow_r_net'])} ({s['passed']['shadow_r_count']}), "
            f"vetoed {_value(s['vetoed']['mean_shadow_r_net'])} ({s['vetoed']['shadow_r_count']}).")
    day = body["windows"]["1d"]["overall"]
    if day.get("regime"):
        lines.append(f"Regime (day): {day['regime']['tag']} ({day['regime']['regime_version']}).")
    late = day.get("late_fee_settlements")
    if late and late["count"]:
        lines.append(f"Fees settled since their day was scored: {late['count']} trades, mean R "
                     f"after fees {_value(late['mean_r_net'])} ("
                     + ", ".join(f"{i['symbol']} {i['r_net']}" for i in late["items"]) + ").")
    calibration = day.get("jev")
    if calibration:
        lines.append("Jev probabilities (day): " + "; ".join(
            f"{name} {c['stated']} stated, {c['outcomes_recorded']} with outcomes, "
            f"{c['outcomes_pending']} pending" for name, c in (
                ("selection", calibration["selection"]),
                ("maintenance", calibration["maintenance"])))
            + f"; {calibration['records_to_date']} calibration records to date.")
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
    lines += dimension_lines(body["windows"]["30d"]["overall"].get("dimensions"),
                             label="By regime and version (30 days)")
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
    regime = body.get("regime")
    if regime:
        lines.append(f"Regime: {regime.get('tag') or regime.get('status')} "
                     f"({regime.get('regime_version')}).")
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
        "regime": body.get("regime"),
    }
    return "\n".join(lines), data


def learning_lines(section):
    """The weekly review's ``learning`` section (LEARNING_LOOP_WEEKLY_V1) as text lines."""
    if not section:
        return []
    patterns = section["patterns"]
    missed = section["missed_tradeable_by_regime"]["all"]
    lines = [f"Learning ({section['learning_version']}, propose only): "
             f"{len(patterns['reached'])} pattern(s) at {patterns['minimum']}+ occurrences; "
             f"missed tradeable {missed['missed_tradeable']} of {missed['movers']} movers "
             f"({missed['simulated_entries']} simulated entries, mean net R "
             f"{_value(missed['mean_net_r'])}); {section['stats_exclusions']['excluded_trades']} "
             "excluded trade(s) left out."]
    lines += [f"  Pattern {p['pattern']}: {p['occurrences']} of {p['base']}."
              for p in patterns["reached"]]
    for sid, pair in sorted(section["shadow_vs_live"].items()):
        live, shadow = pair["live_paper"], pair["shadow"]
        lines.append(f"  {sid}: live {live['r_net_count']} trades mean net R "
                     f"{_value(live['mean_r_net'])} ({live['status']}); shadow "
                     f"{shadow['trades']} mean net R {_value(shadow['mean_net_r'])} "
                     f"({shadow['status']}).")
    for item in section["proposals"]:
        lines.append(f"  PROPOSED (not applied) {item['proposal_id']}: {item['text']}")
    if not section["proposals"]:
        lines.append("  No proposal: no measured cell meets a proposal rule.")
    return lines


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
    fees = body.get("fee_verification")
    if fees:
        total, week = fees["all_before_week_end"], fees["week"]
        lines.append(
            f"Net R rests on fee-verified trades only: {total['fee_verified']} of "
            f"{total['closed']} closed to date, {week['fee_verified']} of {week['closed']} "
            "this week.")
    lines += dimension_lines(body.get("dimensions"), label="By regime and version (to date)")
    lines += calibration_lines(body.get("jev_calibration"))
    lines += learning_lines(body.get("learning"))
    data = {k: body[k] for k in ("review_version", "week_start", "week_end", "computed_at",
                                 "tests", "method")}
    data.update({k: body[k] for k in ("fee_verification", "dimensions", "jev_calibration",
                                      "learning", "tests_excluding_exclusions")
                 if k in body})
    data.update(recorded=recorded, event_seq=event_seq)
    return "\n".join(lines), data


__all__ = ["learning_lines", "reality_summary", "review_summary", "scorecard_summary"]
