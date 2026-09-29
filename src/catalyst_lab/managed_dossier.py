"""MANAGED_POSITION_DOSSIER_V1: the size-capped state a management review sends to Jev.

Pure and deterministic: identical inputs give a byte-identical state and manifest. There
is no clock, database, broker or provider access here; callers pass observations that
``managed_review`` has already validated and the option prices it has already derived.

The encoded state must fit ``state_byte_budget`` (the approved profile uses 11,000 bytes;
``jev_review`` still refuses anything over 12,000). What is sent:

* the thesis and disproof once, and the original research compactly: catalyst, levels,
  the economic relationship and the proposer's technical analysis clipped with hash
  markers, the code-computed technical metrics from selection time, and the proposer's
  selection rationale (claims with their cited source/bar IDs, why now, why these
  levels, what would change the proposer's mind). ``agent_confidence`` is never sent;
* the selection judgments as ``{choice, top_p}`` per question;
* the last management-history rows ``{seq, action, reason, stop, target}``;
* position, quote and protection facts plus code-computed R and tick distances, a
  descriptive regime label and a remaining-time bucket;
* completed bars once, column-encoded under one provider/feed header. Recent bars
  (refs ``R..``) supply stop and target options; older structural bars (refs ``S..``)
  supply target options only. The structural set is marked by ref, never copied twice;
* every code-eligible option, never truncated;
* news with adverse/withdrawn items first: up to ``max_news`` current and
  ``ORIGINAL_NEWS_LIMIT`` original items, each excerpt clipped to ``NEWS_EXCERPT_CHARS``.

When the state is over budget a fixed ladder (``_Compiler.ladder``) reduces the least
decision-relevant evidence first, one step at a time: the technical prose, the economic
relationship, supporting excerpts, history rows, whole supporting items, structural bars
that back no option, rationale texts. Adverse or withdrawn items are never removed; as
the very last step, once no supporting item remains, their excerpts are shortened.
Options, the bars behind them, recent bars, thesis and disproof are never touched. If
the ladder cannot reach the budget the compiler raises ``ContextBudgetUnsatisfiable``:
the review is skipped, never sent oversized and never cut further.

The manifest records every input, whether it was included, truncated or omitted and
why, with hashes and character counts, plus the budget and the bytes used. It is
stored with the review request as the record of what Jev saw; full copies stay in the
append-only ledger and are referenced there, never duplicated here.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from urllib.parse import urlparse

from catalyst_lab.jev_contract import digest, encoded

DOSSIER_VERSION = "MANAGED_POSITION_DOSSIER_V1"
METRICS_VERSION = "POSITION_METRICS_V2"
JEV_STATE_CAP_BYTES = 12_000  # jev_review._privacy_check refuses larger encoded states.
MAX_INPUT_SOURCES = 8  # position_news keeps at most eight; research items carry at most eight.
MAX_INPUT_BARS = 64
ORIGINAL_NEWS_LIMIT = 2
NEWS_EXCERPT_CHARS = 600
REDUCED_EXCERPT_CHARS = 300
ECONOMIC_CHARS = 300
REDUCED_ECONOMIC_CHARS = 120
TECHNICAL_ANALYSIS_CHARS = 300
REDUCED_RATIONALE_CHARS = 160
HASH_MARKER_HEX = 16
NEAR_TARGET_PROGRESS = Decimal("0.8")  # The ruled near-target fraction; labels only.
NEAR_STOP_R = Decimal("0.5")
CLEAR_R = Decimal("1")
ADVERSE = frozenset({"ADVERSE", "WITHDRAWN"})
BUDGET_REASON = "STATE_BYTE_BUDGET"
_METRIC_KEYS = (
    "last_completed_close", "sma_5", "sma_20", "last_volume_vs_prior_19_mean",
    "observed_dollar_volume_20", "spread_bps", "gap_count",
)
_HISTORY_KINDS = {
    "MANAGED_JEV_JUDGMENT": None, "MANAGEMENT_PLAN_AUTHORIZED": "PLAN_AUTHORIZED",
    "POSITION_REVIEW_OBSOLETE": "REVIEW_OBSOLETE", "POSITION_REVIEW_RECOVERED": "REVIEW_RECOVERED",
    "AMENDMENT_REJECTED": "AMENDMENT_REJECTED", "MANAGEMENT_EXPIRED": "MANAGEMENT_EXPIRED",
}


class ContextBudgetUnsatisfiable(ValueError):
    """No permitted reduction fits the budget; the caller skips this review."""

    code = "CONTEXT_BUDGET_UNSATISFIABLE"

    def __init__(self, state_bytes, budget, manifest):
        super().__init__(self.code)
        self.state_bytes, self.budget, self.manifest = state_bytes, budget, manifest


@dataclass(frozen=True)
class CompiledDossier:
    state: dict
    manifest: dict


def encoded_bytes(value):
    """Bytes exactly as sent: ``encoded`` escapes non-ASCII text (6 or 12 bytes a char)."""
    return len(encoded(value).encode())


def plain(value):
    """Fixed-point text without exponent or trailing zeros; exact for any finite Decimal."""
    if value is None:
        return None
    value = Decimal(value)
    if not value.is_finite():
        raise ValueError("FINITE_DECIMAL_REQUIRED")
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def brief(value, places):
    """Display rounding (half-even) of a code-computed ratio; never used for prices."""
    if value is None:
        return None
    value = Decimal(value)
    try:
        return plain(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        return plain(value)


def _aware(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return value


def _text_time(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _seconds(now, at):
    return math.floor((now - _aware(at)).total_seconds())


def _clip(text, cap):
    """``(kept, marker)``; the marker carries a hash prefix, the manifest the full hash."""
    if len(text) <= cap:
        return text, None
    return text[:cap], {
        "kept_chars": cap, "total_chars": len(text),
        "sha256_prefix": digest(text)[:HASH_MARKER_HEX],
    }


def _entry(section, status, *, item=None, text=None, sha256=None, kept=None, reason=None,
           **extra):
    row = {"section": section, "status": status}
    if item is not None:
        row["item"] = item
    if text is not None:
        row.update(sha256=digest(text), total_chars=len(text),
                   kept_chars=len(text) if kept is None else kept)
    elif sha256 is not None:
        row["sha256"] = sha256
    if reason:
        row["reason"] = reason
    row.update(extra)
    return row


def position_metrics(*, entry, initial_stop, bid, ask, stop, target, tick_size, remaining_qty,
                     opened_at, hard_exit_at, now, recent_ranges):
    """POSITION_METRICS_V2: V1's facts plus R and tick distances, regime and time bucket.

    R is the initial risk per unit, entry fill minus the original research stop. The
    regime and time bucket are descriptive labels for the reviewer; they schedule nothing.
    """
    risk = entry - initial_stop if initial_stop is not None and entry > initial_stop else None
    span = target - entry
    progress = (bid - entry) / span if span > 0 else None
    total = (hard_exit_at - opened_at).total_seconds()
    remaining = (hard_exit_at - now).total_seconds()
    fraction = Decimal(str(remaining)) / Decimal(str(total)) if total > 0 else None

    def in_r(value):
        return brief(value / risk, 2) if risk else None

    def in_ticks(value):
        return brief(value / tick_size, 1)

    to_stop, to_target = bid - stop, target - bid
    if risk is None:
        regime = "UNKNOWN"
    elif (progress is not None and progress >= NEAR_TARGET_PROGRESS) or (
        to_stop <= NEAR_STOP_R * risk
    ):
        regime = "NEAR"
    elif to_stop >= CLEAR_R * risk and to_target >= CLEAR_R * risk:
        regime = "CLEAR"
    else:
        regime = "NORMAL"
    if fraction is None:
        bucket = None
    elif fraction > Decimal("0.5"):
        bucket = "OVER_50_PCT_LEFT"
    elif fraction > Decimal("0.25"):
        bucket = "25_TO_50_PCT_LEFT"
    elif fraction > Decimal("0.1"):
        bucket = "10_TO_25_PCT_LEFT"
    else:
        bucket = "UNDER_10_PCT_LEFT"
    mean_range = sum(recent_ranges, Decimal(0)) / len(recent_ranges) if recent_ranges else None
    return {
        "calculation_version": METRICS_VERSION,
        "price_basis": "CURRENT_BID",
        "initial_risk_per_unit": plain(risk),
        "distance_to_stop": plain(to_stop),
        "distance_to_stop_ticks": in_ticks(to_stop),
        "distance_to_stop_r": in_r(to_stop),
        "distance_to_target": plain(to_target),
        "distance_to_target_ticks": in_ticks(to_target),
        "distance_to_target_r": in_r(to_target),
        "unrealized_r": in_r(bid - entry),
        "stop_locked_in_r": in_r(stop - entry),
        "target_reward_r": in_r(target - entry),
        "target_progress_fraction": brief(progress, 3),
        "recent_bar_range_r": in_r(mean_range) if mean_range is not None else None,
        "regime": regime,
        "remaining_holding_seconds": math.floor(remaining),
        "elapsed_holding_seconds": _seconds(now, opened_at),
        "remaining_time_fraction": brief(fraction, 3),
        "time_bucket": bucket,
        "gross_unrealized_pnl_at_bid": brief((bid - entry) * remaining_qty, 2),
        "remaining_loss_to_stop_from_entry": brief(
            max(Decimal(0), entry - stop) * remaining_qty, 2
        ),
        "mark_to_stop_exposure": brief(to_stop * remaining_qty, 2),
        "spread_bps": brief((ask - bid) / ((ask + bid) / 2) * 10000, 1),
        "fees_and_slippage_included": False,
    }


def option_distances(kind, price, *, bid, tick_size, entry, risk):
    """Code-computed distances shown with an option; the price itself is never derived.

    ``ticks``/``r``: from the current bid down to a stop or up to a target.
    ``r_from_entry``: the R locked in (stop) or the reward (target) at the level.
    """
    gap = bid - price if kind == "stop" else price - bid
    return {
        "ticks": brief(gap / tick_size, 1),
        "r": brief(gap / risk, 2) if risk else None,
        "r_from_entry": brief((price - entry) / risk, 2) if risk else None,
    }


def _answer(answer):
    probabilities = answer.get("probabilities") or {}
    if answer.get("type") == "choice":
        chosen = answer["choice"]
    else:  # Score answers: the most probable level; ties resolve to the lowest level.
        chosen = max(sorted(probabilities, key=lambda k: int(k)), key=lambda k: probabilities[k])
    return {"choice": str(chosen), "top_p": brief(Decimal(str(probabilities[chosen])), 2)}


def _judgments(selection):
    state, entries = {}, []
    for name in ("eligibility", "quality"):
        judgment = (selection or {}).get(name)
        section = "selection_judgments." + name
        if not judgment:
            state[name] = None
            entries.append(_entry(section, "ABSENT"))
            continue
        answers = judgment["answers"]
        state[name] = {key: _answer(answers[key]) for key in sorted(answers)}
        entries.append(_entry(
            section, "INCLUDED", sha256=digest(encoded(answers)),
            receipt_id=judgment.get("receipt_id"),
            question_set_version=judgment.get("question_set_version"),
            reduced_to="CHOICE_AND_TOP_PROBABILITY",
        ))
    return state, entries


def _history(rows, limit):
    """Fold events into ``{seq, action, reason, stop, target}``; a plan joins its judgment."""
    folded, by_context = [], {}
    for row in sorted(rows, key=lambda r: r["event_seq"]):
        kind = row["kind"]
        body = row.get("decision", row.get("body")) or {}
        if kind not in _HISTORY_KINDS:
            continue
        # Levels exist only on an authorized plan and on a reverted amendment.
        if kind == "MANAGEMENT_PLAN_AUTHORIZED":
            levels = body
        elif kind in {"AMENDMENT_REJECTED", "MANAGEMENT_EXPIRED"}:
            levels = body.get("reverted_to") or {}
        else:
            levels = {}
        item = {
            "seq": row["event_seq"],
            "action": body.get("action") if kind == "MANAGED_JEV_JUDGMENT"
            else _HISTORY_KINDS[kind],
            "reason": body.get("reason"),
            "stop": plain(levels["stop"]) if levels.get("stop") is not None else None,
            "target": plain(levels["target"]) if levels.get("target") is not None else None,
        }
        judged = by_context.get(body.get("context_hash"))
        if kind == "MANAGEMENT_PLAN_AUTHORIZED" and judged and judged["stop"] is None \
                and judged["target"] is None:
            judged["stop"], judged["target"] = item["stop"], item["target"]
            judged["merged_seqs"].append(row["event_seq"])
            continue
        item["merged_seqs"] = [row["event_seq"]]
        if kind == "MANAGED_JEV_JUDGMENT" and body.get("context_hash"):
            by_context[body["context_hash"]] = item
        folded.append(item)
    return folded[-limit:] if limit else [], folded[:-limit] if limit else folded


def _rationale(block, cap):
    """Compact claims form; texts clipped only by the ladder's reduction step."""
    kept_chars = total_chars = 0

    def text(value):
        nonlocal kept_chars, total_chars
        value = value if isinstance(value, str) else ""
        kept = value if cap is None else value[:cap]
        kept_chars, total_chars = kept_chars + len(kept), total_chars + len(value)
        return kept

    claims = []
    for claim in block.get("claims") or []:
        support = claim.get("supported_by") or {}
        row = {"id": claim.get("claim_id"), "kind": claim.get("kind"),
               "text": text(claim.get("text"))}
        if support.get("source_ids"):
            row["sources"] = list(support["source_ids"])
        if support.get("bar_ids"):
            row["bars"] = list(support["bar_ids"])
        claims.append(row)
    compact = {
        "status": block.get("status") or "UNVERIFIED_PROPOSER_CLAIMS",
        "claims": claims,
        **{key: text(block.get(key)) for key in (
            "why_now", "why_these_levels", "what_would_change_my_mind",
        )},
    }
    if kept_chars < total_chars:
        compact["texts_truncated"] = {
            "chars_per_text": cap, "kept_chars": kept_chars, "total_chars": total_chars,
            "sha256_prefix": digest(encoded(block))[:HASH_MARKER_HEX],
        }
    return compact, kept_chars, total_chars


@dataclass
class _Plan:
    """The mutable reductions; each ladder step changes exactly one field."""

    technical_analysis: bool = True
    economic_cap: int = ECONOMIC_CHARS
    rationale_cap: int | None = None
    supporting_cap: int = NEWS_EXCERPT_CHARS
    adverse_cap: int = NEWS_EXCERPT_CHARS
    omit_news: set = field(default_factory=set)
    omit_history: set = field(default_factory=set)
    omit_bars: set = field(default_factory=set)
    steps: list = field(default_factory=list)


class _Compiler:
    def __init__(self, *, now, budget, max_news, max_history, position, quote, protection,
                 thesis, disproof, research, selection, history, excursions, current_news,
                 original_news, bars, options, trigger, mechanical_checks):
        self.now, self.budget = now, budget
        self.position, self.quote, self.protection = position, quote, protection
        self.thesis, self.disproof = thesis, disproof
        self.research = research or {}
        self.trigger, self.checks = trigger, mechanical_checks
        self._research_parts()
        self.judgments, judgment_entries = _judgments(selection)
        self.history, older = _history(history or (), max_history)
        self.history_entries = [
            _entry("management_history", "OMITTED", item=row["seq"], reason="SECTION_LIMIT",
                   merged_seqs=row["merged_seqs"]) for row in older
        ]
        self._excursions(excursions)
        self._news(current_news, original_news, max_news)
        self._bars(bars)
        self._options(options)
        self.metrics = position_metrics(
            entry=position["entry_fill_price"], initial_stop=self.initial_stop,
            bid=quote["bid"], ask=quote["ask"], stop=protection["stop"]["price"],
            target=protection["target"]["price"], tick_size=position["tick_size"],
            remaining_qty=position["remaining_qty"], opened_at=position["opened_at"],
            hard_exit_at=position["hard_exit_at"], now=now,
            recent_ranges=[b["high"] - b["low"] for b in self.bars if b["role"] == "RECENT"],
        )
        self.judgment_entries = judgment_entries

    # -- static preparation -------------------------------------------------------------

    def _research_parts(self):
        research = self.research
        levels = research.get("levels") or None
        self.levels = {k: plain(v) for k, v in sorted(levels.items())} if levels else None
        try:
            stop = Decimal(str(levels["stop"])) if levels else None
            self.initial_stop = stop if stop is not None and stop.is_finite() and stop > 0 \
                else None
        except (KeyError, InvalidOperation, TypeError, ValueError):
            self.initial_stop = None
        self.catalyst = research.get("catalyst") if isinstance(
            research.get("catalyst"), str) else None
        economic = research.get("economic_relationship")
        self.economic = economic if isinstance(economic, str) and economic else None
        context = research.get("technical_context") or {}
        legacy = research.get("technical_facts") or {}
        prose = context.get("analysis") if isinstance(context, dict) else None
        if not prose and isinstance(legacy, dict):
            prose = legacy.get("summary")
        self.analysis = prose if isinstance(prose, str) and prose else None
        facts = context.get("observed_facts") if isinstance(context, dict) else None
        metrics = facts.get("metrics") if isinstance(facts, dict) else None
        self.observations_hash = facts.get("observations_hash") if isinstance(facts, dict) \
            else None
        self.technical_metrics = {
            key: brief(Decimal(str(metrics[key])), 6) if metrics.get(key) is not None else None
            for key in _METRIC_KEYS if key in metrics
        } if isinstance(metrics, dict) else None
        block = research.get("rationale")
        self.rationale_input = block if isinstance(block, dict) else None
        self.rationale_confidence = bool(self.rationale_input) and (
            "agent_confidence" in self.rationale_input
        )
        if self.rationale_input is not None:
            # Never a review input, even if a caller passes the stored analytics block.
            self.rationale_input = {
                k: v for k, v in self.rationale_input.items()
                if k not in {"agent_confidence", "schema_version"}
            }

    def _excursions(self, measured):
        if not measured or not measured.get("sample_count"):
            self.excursions = {"samples": 0}
            return
        last = measured.get("last_sample_at")
        self.excursions = {
            "samples": measured["sample_count"],
            "max_gap_seconds": brief(Decimal(str(measured["max_observation_gap_seconds"])), 1)
            if measured.get("max_observation_gap_seconds") is not None else None,
            "mfe_usd": brief(Decimal(str(measured["observed_mfe_pnl"])), 2)
            if measured.get("observed_mfe_pnl") is not None else None,
            "mae_usd": brief(Decimal(str(measured["observed_mae_pnl"])), 2)
            if measured.get("observed_mae_pnl") is not None else None,
            "last_sample_age_seconds": _seconds(self.now, _aware(last)) if last else None,
        }

    def _news(self, current, original, max_news):
        def ordered(items):
            seen, result = set(), []
            rows = sorted(enumerate(items), key=lambda row: (
                row[1].get("stance") not in ADVERSE, row[0],
            ))
            for _, item in rows:
                if item["content_hash"] not in seen:
                    seen.add(item["content_hash"])
                    result.append(item)
            return result

        self.news_entries, self.current, self.original = [], [], []
        current_items = ordered(current or ())
        for index, item in enumerate(current_items):
            if index < max_news:
                self.current.append(("C" + str(len(self.current) + 1), item))
            else:
                self.news_entries.append(self._news_entry(
                    "news.current", item, None, "OMITTED", "SECTION_LIMIT"))
        shown = {item["content_hash"] for _, item in self.current}
        for item in ordered(original or ()):
            if item["content_hash"] in shown:
                self.news_entries.append(self._news_entry(
                    "news.original", item, None, "OMITTED", "SAME_EXCERPT_IN_CURRENT"))
            elif len(self.original) < ORIGINAL_NEWS_LIMIT:
                self.original.append(("O" + str(len(self.original) + 1), item))
            else:
                self.news_entries.append(self._news_entry(
                    "news.original", item, None, "OMITTED", "SECTION_LIMIT"))

    @staticmethod
    def _news_entry(section, item, ref, status, reason=None, kept=None):
        if kept is None:
            kept = 0 if status == "OMITTED" else len(item["excerpt"])
        return _entry(
            section, status, item=ref, sha256=item["content_hash"], reason=reason,
            source_id=item["source_id"], stance=item.get("stance"), kept_chars=kept,
            total_chars=len(item["excerpt"]),
        )

    def _bars(self, bars):
        ordered = sorted(bars, key=lambda b: (_aware(b["ends_at"]), b["observation_id"]))
        counters = {"RECENT": 0, "STRUCTURAL": 0}
        self.bars, self.bar_ref = [], {}
        for bar in ordered:
            counters[bar["role"]] += 1
            ref = ("R" if bar["role"] == "RECENT" else "S") + f"{counters[bar['role']]:02d}"
            self.bar_ref[bar["observation_id"]] = ref
            self.bars.append({**bar, "ref": ref})
        durations = {(_aware(b["ends_at"]) - _aware(b["starts_at"])) for b in self.bars}
        self.bar_seconds = None
        if len(durations) == 1:
            seconds = next(iter(durations)).total_seconds()
            self.bar_seconds = int(seconds) if seconds == int(seconds) else None
        for bar in self.bars:
            cells = [bar["ref"]]
            if self.bar_seconds is None:
                cells.append(str(_seconds(self.now, _aware(bar["starts_at"]))))
            cells.append(str(_seconds(self.now, _aware(bar["ends_at"]))))
            cells.extend(plain(bar[k]) for k in ("open", "high", "low", "close", "volume"))
            bar["row"] = "|".join(cells)
        self.bars_sha256 = digest(encoded([
            {k: plain(v) if isinstance(v, Decimal) else _text_time(v)
             for k, v in sorted(bar.items()) if k not in {"row", "ref", "role"}}
            for bar in self.bars
        ]))

    def _options(self, options):
        entry = self.position["entry_fill_price"]
        risk = entry - self.initial_stop \
            if self.initial_stop is not None and entry > self.initial_stop else None
        self.option_bars, self.option_sources = set(), {}
        self.options = {"stop": [], "target": []}
        for kind in ("stop", "target"):
            for option in options.get(kind, ()):
                ref = self.bar_ref[option["observation_id"]]
                self.option_bars.add(option["observation_id"])
                self.option_sources[option["option_id"]] = {
                    "bar": ref, "observation_id": option["observation_id"],
                    "basis": option["basis"], **option_distances(
                        kind, option["price"], bid=self.quote["bid"],
                        tick_size=self.position["tick_size"], entry=entry, risk=risk,
                    ),
                }
                # Readable provenance and distances go into the option's question text
                # (managed_review); the kind and the bar ref (R recent, S structural)
                # determine the basis. Options are never truncated or removed.
                self.options[kind].append({
                    "option_id": option["option_id"], "price": str(option["price"]), "bar": ref,
                })

    # -- the ladder ---------------------------------------------------------------------

    def ladder(self, plan):
        """Yield one reduction at a time, least decision-relevant evidence first.

        Supporting news is shortened and then removed before any adverse or withdrawn
        item is touched; adverse items are only ever shortened, as the last step.
        Options, recent bars, bars that back an option, the thesis and disproof, the
        position facts and the selection judgments are never reduced.
        """
        news = (*self.current, *self.original)

        def longer(items, cap):
            return any(len(item["excerpt"]) > cap for _, item in items)

        if self.analysis:
            yield "OMIT_TECHNICAL_ANALYSIS", lambda: setattr(plan, "technical_analysis", False)
        if self.economic and len(self.economic) > REDUCED_ECONOMIC_CHARS:
            yield "REDUCE_ECONOMIC_RELATIONSHIP", lambda: setattr(
                plan, "economic_cap", REDUCED_ECONOMIC_CHARS)
        supporting = [(ref, item) for ref, item in news if item.get("stance") not in ADVERSE]
        if longer(supporting, REDUCED_EXCERPT_CHARS):
            yield "REDUCE_SUPPORTING_NEWS", lambda: setattr(
                plan, "supporting_cap", REDUCED_EXCERPT_CHARS)
        for row in self.history:  # Oldest first.
            yield "OMIT_MANAGEMENT_HISTORY", lambda seq=row["seq"]: plan.omit_history.add(seq)
        # Original before current, lowest priority first.
        for group in (self.original, self.current):
            for ref, item in reversed(group):
                if item.get("stance") not in ADVERSE:
                    yield "OMIT_SUPPORTING_NEWS", lambda ref=ref: plan.omit_news.add(ref)
        for bar in self.bars:  # Oldest first; bars that back an option stay.
            if bar["role"] == "STRUCTURAL" and bar["observation_id"] not in self.option_bars:
                yield "OMIT_STRUCTURAL_BAR", lambda ref=bar["ref"]: plan.omit_bars.add(ref)
        if self.rationale_input:
            yield "REDUCE_RATIONALE", lambda: setattr(plan, "rationale_cap",
                                                      REDUCED_RATIONALE_CHARS)
        if self.economic:
            yield "OMIT_ECONOMIC_RELATIONSHIP", lambda: setattr(plan, "economic_cap", 0)
        adverse = [(ref, item) for ref, item in news if item.get("stance") in ADVERSE]
        if longer(adverse, REDUCED_EXCERPT_CHARS):
            yield "REDUCE_ADVERSE_NEWS", lambda: setattr(
                plan, "adverse_cap", REDUCED_EXCERPT_CHARS)

    # -- rendering ----------------------------------------------------------------------

    @staticmethod
    def _excerpt_cap(item, plan):
        return plan.adverse_cap if item.get("stance") in ADVERSE else plan.supporting_cap

    def _news_item(self, ref, item, plan):
        excerpt, marker = _clip(item["excerpt"], self._excerpt_cap(item, plan))
        published, retrieved = item.get("published_at"), item.get("retrieved_at")
        row = {
            "ref": ref, "source_id": item["source_id"],
            "host": urlparse(item["url"]).hostname if item.get("url") else None,
            "published_age_minutes": _seconds(self.now, _aware(published)) // 60
            if published else None,
            "retrieved_age_minutes": _seconds(self.now, _aware(retrieved)) // 60,
            "excerpt": excerpt,
        }
        # Provenance flags appear only when supplied; research sources usually have none.
        for key in ("stance", "novelty", "primary_source", "asset_relevant"):
            if item.get(key) is not None:
                row[key] = item[key]
        if marker:
            row["excerpt_truncated"] = marker
        return row

    def render(self, plan):
        research = {"catalyst": self.catalyst, "levels": self.levels}
        if self.economic:
            if plan.economic_cap:
                text, marker = _clip(self.economic, plan.economic_cap)
                research["economic_relationship"] = text
                if marker:
                    research["economic_relationship_truncated"] = marker
            else:
                research["economic_relationship"] = None
                research["economic_relationship_omitted"] = {
                    "total_chars": len(self.economic),
                    "sha256_prefix": digest(self.economic)[:HASH_MARKER_HEX],
                }
        else:
            research["economic_relationship"] = None
        technical = {"code_computed_at_selection": self.technical_metrics}
        if self.analysis:
            if plan.technical_analysis:
                text, marker = _clip(self.analysis, TECHNICAL_ANALYSIS_CHARS)
                technical["analysis"] = text
                if marker:
                    technical["analysis_truncated"] = marker
            else:
                technical["analysis"] = None
                technical["analysis_omitted"] = {
                    "total_chars": len(self.analysis),
                    "sha256_prefix": digest(self.analysis)[:HASH_MARKER_HEX],
                }
        research["technical"] = technical
        research["rationale"] = _rationale(self.rationale_input, plan.rationale_cap)[0] \
            if self.rationale_input else None
        rows = [bar["row"] for bar in self.bars if bar["ref"] not in plan.omit_bars]
        uneven = self.bar_seconds is None and self.bars  # Mixed durations: show each start.
        columns = ["ref"] + (["start_age_seconds"] if uneven else []) + [
            "end_age_seconds", "open", "high", "low", "close", "volume",
        ]
        return {
            "context_version": None,  # Set by managed_review; keeps key order stable here.
            "as_of": self.now.isoformat(),
            "position": {
                **{k: self.position[k] for k in (
                    "market", "symbol", "strategy_version", "exit_policy", "cohort", "state",
                )},
                "entry_fill_price": plain(self.position["entry_fill_price"]),
                "initial_stop": plain(self.initial_stop),
                "filled_qty": plain(self.position["filled_qty"]),
                "remaining_qty": plain(self.position["remaining_qty"]),
                "tick_size": plain(self.position["tick_size"]),
            },
            "quote": {
                "bid": plain(self.quote["bid"]), "ask": plain(self.quote["ask"]),
                "age_seconds": _seconds(self.now, self.quote["quote_at"]),
                "provider": self.quote["provider"], "feed": self.quote["feed"],
                "feed_healthy": self.quote["feed_healthy"],
            },
            "protection": {
                name: {"price": plain(order["price"]), "status": order["status"],
                       "source": order["source"]}
                for name, order in sorted(self.protection.items())
            },
            "computed_position": self.metrics,
            "thesis": self.thesis,
            "disproof": self.disproof,
            "original_research": research,
            "selection_judgments": self.judgments,
            "management_history": [
                {k: row[k] for k in ("seq", "action", "reason", "stop", "target")}
                for row in self.history if row["seq"] not in plan.omit_history
            ],
            "sampled_excursions": self.excursions,
            "news": {
                "current": [self._news_item(ref, item, plan) for ref, item in self.current
                            if ref not in plan.omit_news],
                "original": [self._news_item(ref, item, plan) for ref, item in self.original
                             if ref not in plan.omit_news],
            },
            "bars": {
                "provider": self.quote["provider"], "feed": self.quote["feed"],
                "bar_seconds": self.bar_seconds, "columns": "|".join(columns), "rows": rows,
                "omitted_rows": len(self.bars) - len(rows),
            },
            "eligible_options": self.options,
            "review_trigger": self.trigger,
            "mechanical_checks": self.checks,
        }

    # -- manifest -----------------------------------------------------------------------

    def manifest(self, plan, state, size):
        entries = [
            _entry("thesis", "INCLUDED", text=self.thesis),
            _entry("disproof", "INCLUDED", text=self.disproof),
        ]
        if self.catalyst is not None:
            entries.append(_entry("original_research.catalyst", "INCLUDED", text=self.catalyst))
        entries.append(
            _entry("original_research.levels", "INCLUDED" if self.levels else "ABSENT",
                   sha256=digest(encoded(self.levels)) if self.levels else None)
        )
        if self.economic:
            kept = min(len(self.economic), plan.economic_cap)
            status = "OMITTED" if kept == 0 else (
                "TRUNCATED" if kept < len(self.economic) else "INCLUDED")
            reason = None if status == "INCLUDED" else (
                BUDGET_REASON if plan.economic_cap < ECONOMIC_CHARS else "SECTION_CAP")
            entries.append(_entry("original_research.economic_relationship", status,
                                  text=self.economic, kept=kept, reason=reason))
        else:
            entries.append(_entry("original_research.economic_relationship", "ABSENT"))
        if self.analysis:
            kept = min(len(self.analysis), TECHNICAL_ANALYSIS_CHARS) \
                if plan.technical_analysis else 0
            status = "OMITTED" if kept == 0 else (
                "TRUNCATED" if kept < len(self.analysis) else "INCLUDED")
            reason = BUDGET_REASON if not plan.technical_analysis else (
                "SECTION_CAP" if status == "TRUNCATED" else None)
            entries.append(_entry("original_research.technical.analysis", status,
                                  text=self.analysis, kept=kept, reason=reason))
        else:
            entries.append(_entry("original_research.technical.analysis", "ABSENT"))
        entries.append(_entry(
            "original_research.technical.code_computed",
            "INCLUDED" if self.technical_metrics is not None else "ABSENT",
            sha256=digest(encoded(self.technical_metrics))
            if self.technical_metrics is not None else None,
        ))
        if self.observations_hash:
            entries.append(_entry(
                "original_research.technical.observations", "OMITTED",
                sha256=self.observations_hash, reason="CODE_COMPUTED_METRICS_SENT_INSTEAD",
            ))
        if self.rationale_input is not None:
            _, kept, total = _rationale(self.rationale_input, plan.rationale_cap)
            entries.append(_entry(
                "original_research.rationale", "TRUNCATED" if kept < total else "INCLUDED",
                sha256=digest(encoded(self.rationale_input)), kept_chars=kept,
                total_chars=total, reason=BUDGET_REASON if kept < total else None,
                claim_count=len(self.rationale_input.get("claims") or []),
            ))
            for key in ("why_over_peers", "known_risks"):
                if self.rationale_input.get(key) is not None:
                    value = self.rationale_input[key]
                    entries.append(_entry(
                        "original_research.rationale." + key, "OMITTED",
                        sha256=digest(encoded(value)), reason="NOT_MANAGEMENT_EVIDENCE",
                    ))
            if self.rationale_confidence:
                entries.append(_entry(
                    "original_research.rationale.agent_confidence", "OMITTED",
                    reason="ANALYTICS_ONLY_NEVER_REVIEW_INPUT",
                ))
        else:
            entries.append(_entry("original_research.rationale", "ABSENT"))
        entries.extend(self.judgment_entries)
        for row in self.history:
            omitted = row["seq"] in plan.omit_history
            entries.append(_entry(
                "management_history", "OMITTED" if omitted else "INCLUDED", item=row["seq"],
                reason=BUDGET_REASON if omitted else None, merged_seqs=row["merged_seqs"],
            ))
        entries.extend(self.history_entries)
        entries.append(_entry("sampled_excursions", "INCLUDED",
                              sha256=digest(encoded(self.excursions))))
        for section, group in (("news.current", self.current), ("news.original", self.original)):
            for ref, item in group:
                if ref in plan.omit_news:
                    entries.append(self._news_entry(section, item, ref, "OMITTED",
                                                    BUDGET_REASON, kept=0))
                    continue
                cap = self._excerpt_cap(item, plan)
                kept = min(cap, len(item["excerpt"]))
                truncated = kept < len(item["excerpt"])
                reason = None if not truncated else (
                    BUDGET_REASON if cap < NEWS_EXCERPT_CHARS else "EXCERPT_CAP")
                entries.append(self._news_entry(
                    section, item, ref, "TRUNCATED" if truncated else "INCLUDED", reason, kept))
        entries.extend(self.news_entries)
        omitted_bars = sorted(plan.omit_bars)
        entries.append(_entry(
            "bars", "PARTIAL" if omitted_bars else "INCLUDED", sha256=self.bars_sha256,
            total_count=len(self.bars), kept_count=len(self.bars) - len(omitted_bars),
            refs={bar["ref"]: bar["observation_id"] for bar in self.bars},
            structural_refs=[b["ref"] for b in self.bars if b["role"] == "STRUCTURAL"],
            omitted_refs=omitted_bars, reason=BUDGET_REASON if omitted_bars else None,
        ))
        entries.append(_entry(
            "eligible_options", "INCLUDED", sha256=digest(encoded(self.options)),
            count=sum(len(v) for v in self.options.values()), sources=self.option_sources,
        ))
        for section in ("position", "quote", "protection", "computed_position",
                        "review_trigger", "mechanical_checks"):
            entries.append(_entry(section, "INCLUDED", sha256=digest(encoded(state[section]))))
        return {
            "dossier_version": DOSSIER_VERSION,
            "state_byte_budget": self.budget,
            "jev_state_cap_bytes": JEV_STATE_CAP_BYTES,
            "state_bytes": size,
            "state_sha256": digest(encoded(state)),
            "within_budget": size <= self.budget,
            "budget_steps": list(dict.fromkeys(plan.steps)),
            "budget_step_count": len(plan.steps),
            "entries": entries,
        }


def compile_position_dossier(*, context_version, budget, **inputs):
    """Compile to ``budget`` bytes or raise ContextBudgetUnsatisfiable; deterministic.

    ``inputs`` are the keyword arguments of ``_Compiler``. Reductions are applied one
    at a time and only while the encoded state is over budget.
    """
    if type(budget) is not int or not 1_000 <= budget <= JEV_STATE_CAP_BYTES:
        raise ValueError("EXPLICIT_STATE_BYTE_BUDGET_REQUIRED")
    compiler = _Compiler(budget=budget, **inputs)
    plan = _Plan()

    def render():
        state = compiler.render(plan)
        state["context_version"] = context_version
        return state, encoded_bytes(state)

    state, size = render()
    for label, apply in compiler.ladder(plan):
        if size <= budget:
            break
        apply()
        plan.steps.append(label)
        state, size = render()
    manifest = compiler.manifest(plan, state, size)
    if size > budget:
        raise ContextBudgetUnsatisfiable(size, budget, manifest)
    return CompiledDossier(state, manifest)
