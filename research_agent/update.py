"""Update runs under ``RESEARCH_SCHEDULE_V2`` (``docs/RESEARCH-LOOP-V2.md`` 3.5, package
research-loop-kit).

When the schedule names a daily full run, every other run is an update run, not a full batch.
The agent's own setups that are still WATCHING get reviewed against the market now. A setup
whose levels are no longer found is withdrawn. A setup whose plan has changed gets an
adjusted pick. Coins that now qualify go to Jev as new picks. This module is the review, and
it is pure: it takes the research context, the market data and ``levels.json`` as data. The
``update`` command in ``run.py`` fetches that data, builds the report with
``build.build_report`` (the same picks, checks and app models as every other report) and
writes the files.

The agent's own WATCHING setups come from the context's ``watching_setups``
(``RESEARCH_CONTEXT_V3``: ``setup_id``, ``symbol``, ``levels``, ``run_slot``, ``expires_at``,
``signal_id``). Rows of ``open_trades`` in state WATCHING are accepted too, as a fallback. A
context without ``watching_setups`` has none to review, and ``update-notes.json`` says so.
Every coin is reviewed once, with all of its WATCHING rows:

* **Kept, not re-checked.** The setup has expired, its levels cannot be read, or there are
  no market data or live quote for the coin. The kit cannot find it again, so it neither
  withdraws it nor replaces it.
* **Kept, price at the entry.** Price is at or below the entry, or above it by less than the
  profile's entry-band minimum. The app's trigger decides now. Withdrawing or replacing the
  setup would race it, and a fresh search could not propose an entry that close to price.
* **Kept, found again.** One of the profile's rule, timeframe and window combinations still
  shows the setup: entry within 0.25%, stop and target within 0.5%. Every combination is
  searched around the setup's own entry, not just the first to qualify, so a setup found
  with other bars (the daily run's 4-hour setup under the intraday profile) is not churned
  just because another setup comes first in the search.
* **Adjusted.** It is not found again, but the profile's search (``levels.json``, exactly
  what a fresh run finds) finds a setup now. That setup becomes an adjusted pick, a normal
  report-V3 pick. The app replaces the old setup only if Jev selects the adjusted pick
  (``RESEARCH_RUN_SUPERSESSION_V2``).
* **Withdrawn.** No setup is found now. The reason is the first check that failed. A coin
  that has left the research context's tradable universe is withdrawn too.

New coins are coins with no non-closed setup (none in ``open_trades``, none WATCHING) and a
qualifying setup now. Adjusted picks come first, then new coins in ``build``'s usual order:
rule A first, then the highest reward:risk. ``--max-picks`` (default 8) caps the total. An
adjusted pick that does not fit is recorded, and its WATCHING setup stays as it is.

The same rules as every run apply. The levels come from ``levels.find_setup`` alone, so every
entry, stop and target is a bar's own low or high, as the cited bars show it (Jev's
bar-support check). Nothing here computes a level that no bar shows, and nothing loosens the
app's 2% minimum stop or 2R rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from catalyst_lab.research_report_v3 import SYMBOL_PATTERN
from catalyst_lab.research_schedule import SCHEDULE_VERSION_V2, UPDATE_RUN, ResearchSchedule
from research_agent import build, levels, market, records
from research_agent import context as context_module
from research_agent import evidence as evidence_module

WITHDRAWAL_SCHEMA = "AGENT_RESEARCH_WITHDRAWAL_V1"
WITHDRAWAL_FILE = "withdrawal.json"
WITHDRAWAL_RESULT_FILE = "withdrawal-submit.json"
NOTES_FILE = "update-notes.json"
# Written first by every update: a folder holding other files without it belongs to another
# run (the daily run's runs/<date>), which an update must never overwrite.
MARKER_FILE = "update-run.json"
NOTES_SCHEMA = "RESEARCH_AGENT_UPDATE_NOTES_V1"
WITHDRAWAL_BODY_LIMIT = 1_048_576  # The report route's body limit (design 3.3).
MAX_WITHDRAWAL_ITEMS = 30
MAX_REASON_CHARS = 300
DEFAULT_MAX_PICKS = 8
# "Materially different" (design 3.5): the entry trigger moved at least 0.25%, or the stop
# or the target at least 0.5%, each relative to the WATCHING setup's own level.
ENTRY_MOVE = Decimal("0.0025")
LEVEL_MOVE = Decimal("0.005")
THRESHOLDS = (("entry_trigger", "entry", ENTRY_MOVE), ("stop", "stop", LEVEL_MOVE),
              ("target", "target", LEVEL_MOVE))
LEVEL_FIELDS = ("entry_trigger", "max_entry_price", "stop", "target")
KEPT, ADJUSTED, WITHDRAWN = "KEPT", "ADJUSTED", "WITHDRAWN"
_SYMBOL = re.compile(SYMBOL_PATTERN)


class UpdateRefused(Exception):
    """The context does not allow an update run: its schedule is not V2, or the slot this
    run answers is the daily full run; or the run folder belongs to another run."""


def check_folder(run_dir):
    """Refuses a run folder that holds another run's files: an update writes market.json,
    levels.json and report.json, so it gets a folder of its own. That is an empty one, one
    holding only the ``context.json`` it is to review (or that a refused update read), or one
    an earlier update in it marked with ``MARKER_FILE``, written once an update passes its
    checks."""
    others = sorted(path.name for path in run_dir.iterdir()
                    if path.name not in {MARKER_FILE, "context.json"})
    if others and not (run_dir / MARKER_FILE).exists():
        raise UpdateRefused(
            f"UPDATE_RUN_DIR_IN_USE: {run_dir} holds another run's files ("
            f"{', '.join(others[:4])}{', ...' if len(others) > 4 else ''}); give each update "
            "run its own folder, runs/<YYYY-MM-DD>/update-<HHMM>. Nothing was changed.")


# --- The slot this update answers ------------------------------------------------------------

def answered_slot(schedule, now):
    """``(run_slot, valid_until_limit)`` (RFC3339 strings) for an update generated at
    ``now``. Raises ``UpdateRefused`` unless the schedule is ``RESEARCH_SCHEDULE_V2`` and that
    slot is an update run. The slot is ``build.run_slot_for``'s, the one ``build_report``
    declares: the next run once ``now`` is within the grace before it. Its kind comes from the
    app's own ``ResearchSchedule.run_kind``. The context's ``current_run_kind`` describes the
    latest run at or before ``as_of``, which can be a different run."""
    schedule = schedule or {}
    if schedule.get("version") != SCHEDULE_VERSION_V2 and "daily" not in schedule:
        raise UpdateRefused(
            "UPDATE_NEEDS_SCHEDULE_V2: the research context's schedule is "
            f"{schedule.get('version') or 'not configured'}, not {SCHEDULE_VERSION_V2}. "
            "Update runs exist only when the schedule names a daily full run; under V1 every "
            "run is a full run (DAILY_PROCEDURE.md, 'Intraday run')")
    if not (schedule.get("timezone") and schedule.get("runs") and schedule.get("daily")):
        raise UpdateRefused("UPDATE_SCHEDULE_INCOMPLETE: the context's V2 schedule lacks its "
                            "timezone, runs or daily run, so the answered slot's kind is "
                            "unknown")
    try:
        definition = ResearchSchedule(schedule["timezone"], tuple(schedule["runs"]),
                                      schedule.get("grace_minutes", 60), schedule["daily"])
    except ValueError:
        raise UpdateRefused("UPDATE_SCHEDULE_INVALID: the context's schedule does not parse "
                            "as the app's ResearchSchedule") from None
    slot, limit = build.run_slot_for(schedule, now)
    if definition.run_kind(datetime.fromisoformat(slot)) != UPDATE_RUN:
        raise UpdateRefused(
            f"UPDATE_SLOT_IS_FULL_RUN: a report sent now answers the {slot} run, the daily full "
            f"run ({schedule['daily']}). Run the daily procedure for it (DAILY_PROCEDURE.md, "
            "Morning), not an update")
    return slot, limit


# --- The agent's own WATCHING setups -----------------------------------------------------------

def watching_rows(ctx):
    """``(rows, source, notes)``: the context's ``watching_setups`` plus any ``open_trades``
    row in state WATCHING (a fallback; none exist while ``open_trades`` lists filled trades
    only), deduplicated by ``setup_id``."""
    rows, notes = [], []
    listed = ctx.get("watching_setups")
    if isinstance(listed, list):
        rows.extend(row for row in listed if isinstance(row, dict))
        source = "watching_setups"
    else:
        source = None
        notes.append("The research context has no watching_setups (RESEARCH_CONTEXT_V3 lists "
                     "the agent's own WATCHING setups there): none were reviewed from it.")
    seen = {row.get("setup_id") for row in rows}
    extra = [row for row in ctx.get("open_trades") or [] if isinstance(row, dict)
             and (row.get("watching") is True or row.get("state") == "WATCHING")
             and row.get("setup_id") not in seen]
    if extra:
        rows.extend(extra)
        notes.append(f"{len(extra)} open_trades rows in state WATCHING were reviewed too.")
        source = f"{source}+open_trades" if source else "open_trades"
    return rows, source, notes


open_trade_symbols = context_module.open_trade_symbols  # Shared with build (the daily run).


def parse_levels(raw):
    """The four levels as positive Decimals, or ``None`` when any is missing or unreadable."""
    if not isinstance(raw, dict):
        return None
    try:
        parsed = {field: Decimal(str(raw[field])) for field in LEVEL_FIELDS}
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    if any(not value.is_finite() or value <= 0 for value in parsed.values()):
        return None
    return parsed


def moves(old, setup):
    """``{level: fraction}``: how far each compared level moved, relative to the old one."""
    return {name: abs(getattr(setup, attribute) - old[name]) / old[name]
            for name, attribute, _threshold in THRESHOLDS}


def material(old, setup):
    """The levels that moved materially: the entry at least 0.25%, the stop or target at
    least 0.5%."""
    moved = moves(old, setup)
    return {name: moved[name] for name, _attribute, threshold in THRESHOLDS
            if moved[name] >= threshold}


def _pct(fraction):
    return f"{(fraction * 100).quantize(Decimal('0.01'))}%"


def _describe(setup):
    return f"{setup.timeframe}/{setup.window} rule {setup.rule}"


def find_again(series_by_timeframe, *, mid, increment, old, profile):
    """The first of the profile's rule/timeframe/window combinations whose setup at the
    WATCHING entry (its bar low within 0.25% of it) has the same stop and target (within
    0.5%), or ``None``. The entry band is narrowed around that entry: this recognizes the
    setup the agent already sent, it proposes nothing new. Its lower end is at most the price
    (an entry above price has been crossed); its upper end is at most the profile's own band
    maximum."""
    entry = old["entry_trigger"]
    low = max(Decimal(0), (mid - entry * (1 + ENTRY_MOVE)) / mid)
    high = min(profile.entry_band_max, (mid - entry * (1 - ENTRY_MOVE)) / mid)
    if high < low:
        return None
    narrow = replace(profile, entry_band_min=low, entry_band_max=high)
    for rule in levels.RULES:
        for timeframe in profile.timeframes:
            for window in profile.windows:
                setup, _tried = levels.find_setup(
                    series_by_timeframe, mid=mid, increment=increment, rules=(rule,),
                    timeframes=(timeframe,), windows=(window,), profile=narrow)
                if setup is not None and not material(old, setup):
                    return setup
    return None


def coin_inputs(ctx, raw_market, symbol, profile):
    """``(series_by_timeframe, mid, increment)`` for ``symbol``, exactly as ``levels`` reads
    them, or a reason string when the coin has no market data or live quote now."""
    coin = symbol.split("/")[0]
    raw_coin = (raw_market.get("coinbase") or {}).get(coin)
    quote = context_module.coin_quote(ctx, symbol)
    mid = context_module.mid_price(quote) if quote else None
    if not raw_coin:
        return (raw_market.get("excluded") or {}).get(coin) or "no Coinbase market data"
    if mid is None:
        return "no live quote in the research context"
    retrieved_at = datetime.fromisoformat(raw_market["retrieved_at"])
    try:
        series = market.all_series(raw_coin, retrieved_at=retrieved_at,
                                   timeframes=profile.timeframes)
    except market.MarketDataError as exc:
        return str(exc)
    increment = (quote or {}).get("price_increment") or raw_coin["quote_increment"]
    return series, mid, increment


# --- The review -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    symbol: str
    action: str  # KEPT, ADJUSTED or WITHDRAWN
    reason: str
    setup_ids: tuple
    old_levels: dict | None = None  # the first reviewed row's levels (Decimals)
    setup: levels.LevelSetup | None = None  # the adjusted pick's setup
    tried: tuple = ()
    checked: bool = True  # False: kept because it could not be re-checked

    @property
    def coin(self):
        return self.symbol.split("/")[0]


@dataclass(frozen=True)
class Review:
    decisions: tuple  # Decision, one per reviewed symbol, by symbol
    new_coins: dict  # coin -> (setup, tried): no non-closed setup, a qualifying setup now
    skipped_coins: dict  # coin -> (None, tried): candidates without a setup (report skipped)
    open_symbols: tuple
    source: str | None
    notes: tuple

    def of(self, action):
        return [decision for decision in self.decisions if decision.action == action]


def _expired(row, now):
    value = row.get("expires_at")
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.tzinfo is not None and moment <= now


def _withdraw_reason(tried, profile):
    first = tried[0] if tried else "no completed Coinbase bar series"
    return f"No {profile.name} setup found now; first failing check: {first}"[:MAX_REASON_CHARS]


def _review_row(row, *, inputs, fresh, tried, profile):
    """``(action, reason, setup, checked)`` for one live WATCHING row of a coin with data."""
    old = parse_levels(row.get("levels"))
    if old is None:
        return KEPT, "Not re-checked: its levels cannot be read.", None, False
    series, mid, increment = inputs
    distance = (mid - old["entry_trigger"]) / mid
    if distance < profile.entry_band_min:
        where = (f"{_pct(distance)} above the entry {build.plain(old['entry_trigger'])}, under "
                 f"the profile's {_pct(profile.entry_band_min)} minimum" if distance > 0 else
                 f"at or below the entry {build.plain(old['entry_trigger'])}")
        return KEPT, (f"Price {build.plain(mid)} is {where}: left to the app's trigger."), \
            None, True
    again = find_again(series, mid=mid, increment=increment, old=old, profile=profile)
    if again is None and fresh is not None and not material(old, fresh):
        again = fresh
    if again is not None:
        moved = moves(old, again)
        return KEPT, (f"Found again on {_describe(again)}: entry {_pct(moved['entry_trigger'])}, "
                      f"stop {_pct(moved['stop'])}, target {_pct(moved['target'])} from the "
                      "WATCHING levels."), None, True
    if fresh is not None:
        moved = moves(old, fresh)
        changed = ", ".join(f"{name.replace('_trigger', '')} {_pct(moved[name])}"
                            for name, _attribute, _threshold in THRESHOLDS)
        return ADJUSTED, (f"Not found again; the {profile.name} setup now ({_describe(fresh)}) "
                          f"moves {changed}."), fresh, True
    return WITHDRAWN, _withdraw_reason(tried, profile), None, True


def review(ctx, raw_market, levels_doc, *, profile, now):
    """The update's decisions from the context, ``market.json`` and ``levels.json`` (the
    profile's search for every universe coin). Pure."""
    rows, source, notes = watching_rows(ctx)
    notes = list(notes)
    universe = set(context_module.coin_symbols(ctx))
    open_symbols = open_trade_symbols(ctx)
    by_symbol = {}
    for row in rows:
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol:
            by_symbol.setdefault(symbol, []).append(row)
        else:
            notes.append(f"A WATCHING row without a symbol was ignored (setup "
                         f"{row.get('setup_id')}).")
    decisions = []
    for symbol in sorted(by_symbol):
        rows_of_symbol = by_symbol[symbol]
        group = [row for row in rows_of_symbol if not _expired(row, now)]
        setup_ids = tuple(str(row.get("setup_id")) for row in rows_of_symbol)
        if not group:
            decisions.append(Decision(symbol, KEPT, "Expired at "
                                      f"{rows_of_symbol[0].get('expires_at')}: left to the app.",
                                      setup_ids, parse_levels(rows_of_symbol[0].get("levels")),
                                      checked=False))
            continue
        old = parse_levels(group[0].get("levels"))
        if symbol not in universe:
            decisions.append(Decision(symbol, WITHDRAWN, "Not in the research context's "
                                      "tradable universe now.", setup_ids, old))
            continue
        coin = symbol.split("/")[0]
        row_doc = levels_doc.get(coin) or {}
        fresh = levels.setup_from_json(row_doc["setup"]) if row_doc.get("setup") else None
        tried = tuple(row_doc.get("tried") or ())
        inputs = coin_inputs(ctx, raw_market, symbol, profile)
        if isinstance(inputs, str):
            decisions.append(Decision(symbol, KEPT, f"Not re-checked: {inputs}; the setup "
                                      "stays.", setup_ids, old, checked=False))
            continue
        outcomes = [_review_row(row, inputs=inputs, fresh=fresh, tried=tried, profile=profile)
                    for row in group]
        kept = [outcome for outcome in outcomes if outcome[0] == KEPT]
        chosen = kept[0] if kept else outcomes[0]
        action, reason, setup, checked = chosen
        if kept and len(outcomes) > len(kept):
            notes.append(f"{symbol}: {len(outcomes) - len(kept)} of its {len(outcomes)} "
                         "WATCHING setups were not found again; they stay, since a withdrawal "
                         "would remove the one that was.")
        decisions.append(Decision(symbol, action, reason, setup_ids, old, setup, tried,
                                  checked))
    watching = set(by_symbol)
    new_coins, skipped_coins = {}, {}
    for symbol in context_module.coin_symbols(ctx):
        if symbol in watching or symbol in open_symbols:
            continue
        coin = symbol.split("/")[0]
        row_doc = levels_doc.get(coin) or {}
        tried = tuple(row_doc.get("tried") or ())
        if row_doc.get("setup"):
            new_coins[coin] = (levels.setup_from_json(row_doc["setup"]), list(tried))
        else:
            skipped_coins[coin] = (None, list(tried))
    return Review(decisions=tuple(decisions), new_coins=new_coins, skipped_coins=skipped_coins,
                  open_symbols=tuple(sorted(open_symbols)), source=source, notes=tuple(notes))


# --- Method v9: the evidence premise of a watched setup ----------------------------------------

PREMISE_PREFIX = "Evidence premise no longer holds: "


def premise_reason(ctx, raw_market, symbol, evidence):
    """Why a watched setup of ``symbol`` no longer passes the operator's evidence rules
    (``build.EvidenceFilter``: --exclude, --min-alpaca-volume-usd, --trend-floor-pct), or
    None. The entry-distance and sell-off rules are not applied: the trigger is a fixed
    level the app decides, and a sell-off withdraws nothing by itself (the operator's
    RISK_OFF path does). Pure; the same facts and texts as a new pick's checks."""
    coin = symbol.split("/")[0]
    if coin in evidence.exclude:
        return build.EXCLUDED_SKIP_REASON
    if evidence.min_alpaca_volume_usd is not None:
        volume = evidence_module.alpaca_volume_usd(ctx, symbol)
        fills, _last_fill, window = evidence_module.fill_history(ctx, symbol)
        if fills == 0 and (volume is None or volume < evidence.min_alpaca_volume_usd):
            return evidence.volume_reason(volume, window)
    if evidence.trend_floor is not None:
        raw_coin = (raw_market.get("coinbase") or {}).get(coin) or {}
        quote = context_module.coin_quote(ctx, symbol)
        mid = context_module.mid_price(quote) if quote else None
        retrieved_at = datetime.fromisoformat(raw_market["retrieved_at"])
        trend = evidence_module.trend_vs_average(raw_coin, mid, retrieved_at=retrieved_at)
        if trend is None or trend < evidence.trend_floor:
            return evidence.trend_reason(trend)
    return None


def premise_review(result, ctx, raw_market, evidence):
    """The review with every re-checked KEPT or ADJUSTED setup whose evidence premise no
    longer holds turned into WITHDRAWN (method v9, owner direction 2026-10-02: the 2-hourly
    review of the setups in the system). A setup the review could not re-check (no market
    data, unreadable levels, expired) is left as it is; one already WITHDRAWN is unchanged."""
    if evidence is None:
        return result
    decisions = []
    for decision in result.decisions:
        reason = None
        if decision.action in (KEPT, ADJUSTED) and decision.checked:
            reason = premise_reason(ctx, raw_market, decision.symbol, evidence)
        if reason is None:
            decisions.append(decision)
            continue
        decisions.append(Decision(decision.symbol, WITHDRAWN,
                                  (PREMISE_PREFIX + reason)[:MAX_REASON_CHARS],
                                  decision.setup_ids, decision.old_levels, None,
                                  decision.tried, True))
    return Review(decisions=tuple(decisions), new_coins=result.new_coins,
                  skipped_coins=result.skipped_coins, open_symbols=result.open_symbols,
                  source=result.source, notes=result.notes)


# --- AGENT_RESEARCH_WITHDRAWAL_V1 --------------------------------------------------------------

def withdrawal_body(decisions, *, agent_id, agent_version, withdrawal_id=None):
    """The withdrawal body (design 3.3; the app's contract): exactly ``schema_version``,
    ``withdrawal_id`` (a UUID4), ``agent`` (``agent_id`` and ``agent_version`` only, not the
    report's agent block) and 1-30 ``items`` of ``{symbol, reason}`` with unique symbols."""
    build.check_agent(agent_id, agent_version)
    return {"schema_version": WITHDRAWAL_SCHEMA,
            "withdrawal_id": withdrawal_id or str(uuid4()),
            "agent": {"agent_id": agent_id, "agent_version": agent_version},
            "items": [{"symbol": decision.symbol, "reason": decision.reason[:MAX_REASON_CHARS]}
                      for decision in decisions[:MAX_WITHDRAWAL_ITEMS]]}


def withdrawal_problems(body):
    """Every refusal the kit can see in a withdrawal body, before it is sent: the app's
    contract (design 3.3), its credential and address screen, and the body limit."""
    if not isinstance(body, dict):
        return ["withdrawal: must be a JSON object"]
    problems = []
    keys = {"schema_version", "withdrawal_id", "agent", "items"}
    if set(body) != keys:
        problems.append(f"withdrawal: keys must be exactly {sorted(keys)}")
    if body.get("schema_version") != WITHDRAWAL_SCHEMA:
        problems.append(f"schema_version: must be {WITHDRAWAL_SCHEMA}")
    withdrawal_id = body.get("withdrawal_id")
    try:
        parsed = UUID(str(withdrawal_id))
        if parsed.version != 4 or str(parsed) != withdrawal_id:
            raise ValueError
    except ValueError:
        problems.append("withdrawal_id: must be a UUID4 in its canonical form")
    agent = body.get("agent")
    if not isinstance(agent, dict) or set(agent) != {"agent_id", "agent_version"}:
        problems.append("agent: must hold exactly agent_id and agent_version")
    else:
        try:
            build.check_agent(agent.get("agent_id"), agent.get("agent_version"))
        except build.BuildError as exc:
            problems.append(f"agent: {exc}")
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_WITHDRAWAL_ITEMS:
        problems.append(f"items: 1-{MAX_WITHDRAWAL_ITEMS} required")
        items = items if isinstance(items, list) else []
    seen = set()
    for index, item in enumerate(items):
        path = f"items[{index}]"
        if not isinstance(item, dict) or set(item) != {"symbol", "reason"}:
            problems.append(f"{path}: must hold exactly symbol and reason")
            continue
        symbol, reason = item["symbol"], item["reason"]
        if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or not symbol.endswith("/USD") or symbol.count("/") != 1):
            problems.append(f"{path}.symbol: must be a XXX/USD symbol")
        elif symbol in seen:
            problems.append(f"{path}.symbol: {symbol} is listed twice")
        seen.add(symbol)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= MAX_REASON_CHARS:
            problems.append(f"{path}.reason: 1-{MAX_REASON_CHARS} characters required")
    return problems + records.screen(body, max_bytes=WITHDRAWAL_BODY_LIMIT)


# --- update-notes.json -----------------------------------------------------------------------

def _levels_text(values):
    return {field: build.plain(values[field]) for field in LEVEL_FIELDS} if values else None


def _setup_levels(setup):
    return {"entry_trigger": build.plain(setup.entry),
            "max_entry_price": build.plain(setup.max_entry), "stop": build.plain(setup.stop),
            "target": build.plain(setup.target)}


def notes_doc(result, *, profile, run_slot, context_as_of, market_retrieved_at, now,
              max_picks, in_report, left_out, build_notes, files, withdrawn_left_out):
    """``update-notes.json``: what was kept, adjusted, withdrawn and new, with the reasons.
    ``in_report`` is the set of symbols in ``report.json``; ``left_out`` maps a symbol that
    had a setup for the report but is not in it to why. Nothing is sent yet: ``submit``
    records what the app answered (``withdrawal-submit.json``, ``submit.json``)."""
    adjusted = []
    for decision in result.of(ADJUSTED):
        adjusted.append({
            "symbol": decision.symbol, "setup_ids": list(decision.setup_ids),
            "reason": decision.reason, "old_levels": _levels_text(decision.old_levels),
            "new_levels": _setup_levels(decision.setup),
            "new_setup": _describe(decision.setup),
            "moved_pct": {name: str((value * 100).quantize(Decimal("0.01")))
                          for name, value in moves(decision.old_levels, decision.setup).items()}
            if decision.old_levels else None,
            "in_report": decision.symbol in in_report,
            "left_out_reason": None if decision.symbol in in_report else left_out.get(
                decision.symbol, "not built"),
        })
    new = [{"symbol": f"{coin}/USD", "levels": _setup_levels(setup),
            "setup": _describe(setup)}
           for coin, (setup, _tried) in sorted(result.new_coins.items())
           if f"{coin}/USD" in in_report]
    return {
        "schema": NOTES_SCHEMA,
        "generated_at": now.isoformat(),
        "profile": profile.name,
        "run_slot": run_slot, "run_kind": UPDATE_RUN,
        "context_as_of": context_as_of, "market_retrieved_at": market_retrieved_at,
        "watching_source": result.source,
        "reviewed": len(result.decisions),
        "kept": [{"symbol": d.symbol, "setup_ids": list(d.setup_ids), "reason": d.reason,
                  "checked": d.checked} for d in result.of(KEPT)],
        "adjusted": adjusted,
        "withdrawn": [{"symbol": d.symbol, "setup_ids": list(d.setup_ids), "reason": d.reason,
                       "in_withdrawal": d.symbol not in withdrawn_left_out}
                      for d in result.of(WITHDRAWN)],
        "new": new,
        "left_out": [{"symbol": symbol, "reason": reason}
                     for symbol, reason in sorted(left_out.items())],
        "open_trades": list(result.open_symbols),
        "max_picks": max_picks,
        "files": files,
        "nothing_to_send": not any(files.values()),
        "build": build_notes,
        "notes": list(result.notes),
    }
