"""The research-agent CLI: ``python -m research_agent.run <command> ...``.

Morning commands: ``context``, ``lessons``, ``checklist export``, ``market``, ``levels``,
``outlook``, ``outlook-check``, ``outlook-submit``, ``build`` (``--lessons``), ``validate``,
``submit``, and ``all`` for the deterministic report steps (context through validate —
never ``submit``, which stays an explicit, separate step; see ``submit.py``). Evening
commands: ``movers``, ``postmortem``, ``postmortem-check``, ``postmortem-submit`` and
``checklist update``; ``checklist show`` prints the checklist. The three ``*submit``
commands and ``answer`` (below) are the only network writes. Every command reads and writes
plain JSON files in one run folder (``--run-dir``, default ``runs/<NY date>``, or
``runs/<day>/evening`` for the evening commands given a day), so a whole run is inspectable
afterwards. See
``research_agent/DAILY_PROCEDURE.md`` for the full daily sequence and
``docs/OPERATIONS-RUNBOOK.md`` for how to run this by hand.

``--profile`` on ``market``, ``levels``, ``build`` and ``all`` picks the research profile
(``levels.PROFILES``): ``daily`` (``DAILY_V1``, the default: the 08:00 run), ``intraday``
(``INTRADAY_V2``: the 2-hourly runs, DAILY_PROCEDURE.md "Intraday run") or ``intraday-v1``
(``INTRADAY_V1``, the first intraday profile, kept for comparison). ``market.json``,
``levels.json`` (per coin) and ``build-notes.json`` record the profile; ``levels`` refuses
market data fetched for a profile that lacks what it needs, and ``build`` refuses a profile
other than the one ``levels.json`` was built with.

``build --session`` is a separate mode for testing against the supervised session
harness (``scripts/agent_research_session.py``), which has no research-context route:
it accepts ``context --offline``'s output and writes a report the harness's own
``submit --report`` completes and sends. This CLI's own ``submit`` refuses to send a
session-mode report to a live app (``build.SESSION_MARKER_FILENAME`` in the run folder).

Research loop V2 (``docs/RESEARCH-LOOP-V2.md``, package research-loop-kit): ``update`` is
the update run of ``RESEARCH_SCHEDULE_V2`` (every run but the daily one; ``update.py``). It
reads the context, market and levels as the other runs do, reviews the agent's own WATCHING
setups, and writes ``withdrawal.json``, ``report.json`` and ``update-notes.json``; ``validate``
checks both files and ``submit`` sends the withdrawal first, then the report. ``update
--lessons`` (package learning-loop2) orders its new picks by the lessons' hints, derived from
the context it reads (or from an ``emphasis.json``); adjusted picks stay first. ``derivatives``
writes the daily run's derivatives context (``derivatives.py``) for ``build --derivatives``.

``answer`` (package kit-answers, 2026-09-29) answers the app's pending window reviews and Jev
early-exit flags under ``MUSE_ANSWER_RULES_V1`` (``answers.py``), for an operator's watch loop
every 2-5 minutes: one GET when nothing is pending; each item's record in the run folder's
``items/`` and each run's line in ``polls/``. ``--dry-run`` writes the answers without sending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from research_agent import (
    answers,
    build,
    checklist,
    context,
    derivatives,
    lessons,
    levels,
    market,
    movers,
    outlook,
    postmortem,
    records,
    submit,
    technicals,
    token,
    update,
)

RUN_TIMEZONE = "America/New_York"


# --- Small shared helpers --------------------------------------------------------------------

def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _read_json(path):
    if not path.exists():
        raise SystemExit(f"error: {path} does not exist (run an earlier step first)")
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_now(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def default_run_dir(now=None):
    """``runs/<YYYY-MM-DD>`` in the schedule's own zone (America/New_York), matching the
    owner's daily run so a morning's whole run lands in one folder by date."""
    now = now or datetime.now(UTC)
    return Path("runs") / now.astimezone(ZoneInfo(RUN_TIMEZONE)).date().isoformat()


def evening_dir(day):
    """The evening review's own folder for a New York day, beside that morning's files, so
    its fresh context never overwrites the one the morning's report was built from."""
    return Path("runs") / day.isoformat() / "evening"


def _run_dir(args, *, evening_of=None):
    """``--run-dir``, else ``runs/<day>/evening`` for an evening step about a New York day
    (they run after midnight, when today's default folder is already the next day's), else
    ``runs/<today in New York>``."""
    if getattr(args, "run_dir", None):
        path = Path(args.run_dir)
    elif evening_of is not None:
        path = evening_dir(evening_of)
    else:
        path = default_run_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_optional(path):
    return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else None


def _day_arg(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD") from None


def _load_token(args):
    env = dict(os.environ)
    if getattr(args, "token_file", None):
        env[token.TOKEN_FILE_ENV] = args.token_file
        env.pop(token.TOKEN_ENV, None)
    return token.load_token(env=env)


def _profile(args):
    """The research profile ``--profile`` names (``daily`` when the command has none)."""
    return levels.PROFILES[getattr(args, "profile", None) or "daily"]


def _profile_key(profile):
    return next(key for key, value in levels.PROFILES.items() if value == profile)


def _market_profile(raw_market):
    """The profile ``market.json`` was fetched for, or ``None`` for an unknown name. A file
    without one predates profiles: it holds DAILY_V1's fetch (300 hours, 60 days)."""
    return levels.PROFILES_BY_NAME.get(raw_market.get("profile", levels.DAILY_V1.name))


def _levels_profiles(raw_levels):
    """The profile names ``levels.json`` records; a row without one predates profiles
    (DAILY_V1)."""
    return {row.get("profile", levels.DAILY_V1.name) for row in raw_levels.values()}


# --- Commands ---------------------------------------------------------------------------------

def cmd_context(args):
    run_dir = _run_dir(args)
    now = _parse_now(args.now)
    if args.offline:
        result = context.offline_universe(now=now)
        print(f"offline fallback: {len(result['universe'])} fresh non-stablecoin Alpaca "
             f"quotes, {len(result['excluded'])} excluded (development only; 'build' "
             "refuses this context)")
    else:
        if not args.base_url:
            print("error: --base-url is required without --offline", file=sys.stderr)
            return 2
        try:
            agent_token = _load_token(args)
        except token.TokenUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            result = context.fetch_context(args.base_url, agent_token)
        except context.ContextError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        coins = (result.get("universe") or {}).get("count")
        print(f"context as_of {result.get('as_of')}: {coins} tradable coins")
    _write_json(run_dir / "context.json", result)
    print(f"wrote {run_dir / 'context.json'}")
    return 0


def cmd_market(args):
    run_dir = _run_dir(args)
    ctx = _read_json(run_dir / "context.json")
    coins = sorted({symbol.split("/")[0] for symbol in context.coin_symbols(ctx)})
    if not coins:
        print("error: the context lists no coins (run 'context' first)", file=sys.stderr)
        return 1
    now = _parse_now(args.now)
    profile = _profile(args)
    result = market.fetch_coinbase(coins, now=now, hours=profile.hourly_lookback_hours,
                                   days=profile.daily_lookback_days)
    result = {"profile": profile.name, **result}
    _write_json(run_dir / "market.json", result)
    print(f"wrote {run_dir / 'market.json'}: {len(result['coinbase'])} of {len(coins)} coins "
         f"have Coinbase market data, {len(result['excluded'])} excluded ({profile.name})")
    return 0


def cmd_levels(args):
    run_dir = _run_dir(args)
    ctx = _read_json(run_dir / "context.json")
    raw_market = _read_json(run_dir / "market.json")
    profile = _profile(args)
    fetched_for = _market_profile(raw_market)
    if fetched_for is None or not fetched_for.fetch_covers(profile):
        print(f"error: MARKET_DATA_PROFILE_MISMATCH: market.json was fetched for "
              f"{raw_market.get('profile')}, which lacks the bars {profile.name} needs; run "
              f"'market --profile {_profile_key(profile)}' first", file=sys.stderr)
        return 2
    retrieved_at = datetime.fromisoformat(raw_market["retrieved_at"])
    results, qualifying = {}, 0
    for symbol in context.coin_symbols(ctx):
        coin = symbol.split("/")[0]
        raw_coin = (raw_market.get("coinbase") or {}).get(coin)
        quote = context.coin_quote(ctx, symbol)
        mid = context.mid_price(quote) if quote else None
        if not raw_coin or mid is None:
            results[coin] = {"profile": profile.name, "setup": None,
                             "tried": ["no Coinbase market data or no live quote for this coin"]}
            continue
        series_by_timeframe = market.all_series(raw_coin, retrieved_at=retrieved_at,
                                                timeframes=profile.timeframes)
        increment = (quote or {}).get("price_increment") or raw_coin["quote_increment"]
        setup, tried = levels.find_setup(series_by_timeframe, mid=mid, increment=increment,
                                         profile=profile)
        results[coin] = {"profile": profile.name,
                         "setup": levels.setup_to_json(setup) if setup else None, "tried": tried}
        qualifying += setup is not None
    _write_json(run_dir / "levels.json", results)
    print(f"wrote {run_dir / 'levels.json'}: {qualifying}/{len(results)} coins have a "
         f"qualifying setup ({profile.name})")
    return 0


def _add_evidence(parser):
    """The operator's evidence filter (build.EvidenceFilter) on build, all and update."""
    parser.add_argument("--exclude", type=build.EvidenceFilter.parse_exclude, default=frozenset(),
                        help="Coins never offered as new picks, comma-separated (e.g. "
                        "POL,LDO,WIF: never filled on Alpaca paper). An update also "
                        "withdraws a watched setup of such a coin (method v9).")
    parser.add_argument("--max-entry-distance-pct", type=build.EvidenceFilter.parse_distance_pct,
                        default=None, help="Leave out a new pick whose maximum entry sits "
                        "further under the live Alpaca mid than this percent (e.g. 2.5).")
    # Method v8 (research_agent/evidence.py) for new picks; since method v9 an update also
    # re-checks the watched setups it keeps or adjusts against --exclude,
    # --min-alpaca-volume-usd and --trend-floor-pct and withdraws one whose premise no
    # longer holds (update.premise_review). The distance and sell-off rules stay new-pick only.
    parser.add_argument("--min-alpaca-volume-usd", type=build.EvidenceFilter.parse_usd,
                        default=None, help="Leave out a new pick whose coin's Alpaca 24 h USD "
                        "volume is under this unless the agent filled a trade of it in the "
                        "context's window (e.g. 5000).")
    parser.add_argument("--trend-floor-pct", type=build.EvidenceFilter.parse_trend_floor_pct,
                        default=None, help="Leave out a new pick whose coin trades further "
                        "under its 20-day average than this percent (0: at or above it).")
    parser.add_argument("--selloff-pct", type=build.EvidenceFilter.parse_selloff_pct,
                        default=None, help="Send no new picks while the median coin or BTC is "
                        "down at least this percent over two hours (e.g. 2).")


def _nonnegative_int(text):
    value = int(text)
    if value < 0:
        raise ValueError("NEGATIVE")
    return value


def _evidence(args):
    """``build.EvidenceFilter`` from the evidence options, or None when none is given."""
    exclude = getattr(args, "exclude", None) or frozenset()
    fields = {"max_entry_distance": getattr(args, "max_entry_distance_pct", None),
              "min_alpaca_volume_usd": getattr(args, "min_alpaca_volume_usd", None),
              "trend_floor": getattr(args, "trend_floor_pct", None),
              "selloff": getattr(args, "selloff_pct", None)}
    if not exclude and all(value is None for value in fields.values()):
        return None
    return build.EvidenceFilter(exclude=frozenset(exclude), **fields)


def cmd_build(args):
    run_dir = _run_dir(args)
    ctx = _read_json(run_dir / "context.json")
    raw_market = _read_json(run_dir / "market.json")
    raw_levels = _read_json(run_dir / "levels.json")
    profile = _profile(args)
    recorded = _levels_profiles(raw_levels)
    if recorded - {profile.name}:
        print(f"error: LEVELS_PROFILE_MISMATCH: levels.json was built with "
              f"{', '.join(sorted(recorded))}, not {profile.name}; give 'levels' and 'build' "
              f"the same --profile (run 'levels --profile {_profile_key(profile)}' again, or "
              "build with the profile levels.json names)", file=sys.stderr)
        return 2
    levels_by_coin = {
        coin: (levels.setup_from_json(row["setup"]) if row["setup"] else None, row["tried"])
        for coin, row in raw_levels.items()
    }
    news_by_coin = {}
    if args.news:
        try:
            news_by_coin = build.parse_news(_read_json(Path(args.news)))
        except build.NewsFormatError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    emphasis = None
    if getattr(args, "lessons", None):
        try:
            emphasis = lessons.load_emphasis(_read_json(Path(args.lessons)))
        except lessons.LessonsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    derivatives_doc = summaries = None
    if getattr(args, "derivatives", None):
        derivatives_doc = _read_json(Path(args.derivatives))
        try:
            summaries = derivatives.load(derivatives_doc)
        except derivatives.DerivativesFormatError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    now = _parse_now(args.now)
    try:
        result = build.build_report(
            context=ctx, market_data=raw_market, levels_by_coin=levels_by_coin,
            news_by_coin=news_by_coin, agent_id=args.agent_id,
            agent_version=args.agent_version, now=now, max_picks=args.max_picks,
            session=args.session, emphasis=emphasis, run_id=records.run_id(run_dir),
            profile=profile, derivatives=summaries, evidence=_evidence(args),
        )
    except build.BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    marker_path = run_dir / build.SESSION_MARKER_FILENAME
    marker_path.unlink(missing_ok=True)  # Clear a stale marker from an earlier build here.
    build_notes = {
        "profile": profile.name,  # Every pick of this build came from this profile's setups.
        "accepted": [outcome.symbol for outcome in result.accepted],
        "rejected": [{"symbol": o.symbol, "error": o.error} for o in result.rejected],
        "skipped": list(result.skipped), "notes": list(result.notes),
        "lessons": ({"emphasis_file": args.lessons, **result.lessons} if result.lessons
                    else None),
    }
    if result.derivatives is not None:  # Only with --derivatives: other builds' notes as before.
        build_notes["derivatives"] = {"file": args.derivatives, **result.derivatives,
                                      "omitted": derivatives.omitted(derivatives_doc)}
    _write_json(run_dir / "build-notes.json", build_notes)
    if result.report is None:
        print("no picks survived; report.json was not written (see build-notes.json)")
        return 1
    _write_json(run_dir / "report.json", result.report)
    session_only = "run_slot" not in result.report
    if session_only:
        marker_path.write_text(
            "This report.json was built with --session: run_slot/context_as_of/"
            "valid_until are deliberately omitted for the session harness's own "
            "'submit --report' to fill (scripts/agent_research_session.py's run_submit).\n"
            "research_agent's own 'submit' (to a live app) refuses to send this file "
            "while this marker is here. See research_agent/DAILY_PROCEDURE.md.\n"
        )
        print(f"SESSION-ONLY report written (no run_slot/context_as_of/valid_until); "
             f"marked with {marker_path.name}. Submit it with the session harness's own "
             "'submit --report', not this CLI's 'submit'.")
    print(f"wrote {run_dir / 'report.json'}: {len(result.accepted)} picks, "
         f"{len(result.rejected)} rejected, {len(result.skipped)} skipped")
    if result.derivatives is not None:
        record = result.derivatives
        crowded = sum(bool(row.get("crowded")) for row in record["attached"])
        print(f"derivatives context: attached to {len(record['attached'])} picks ({crowded} "
              f"crowded, as RISK claims), left out of {len(record['left_out'])}, no figures "
              f"for {len(record['missing'])} (build-notes.json)")
    return 0


def _withdrawal_check(run_dir):
    """``(withdrawal.json's body or None, problems)``: the update's withdrawal, checked."""
    path = run_dir / update.WITHDRAWAL_FILE
    if not path.exists():
        return None, []
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}, [f"{path.name}: not JSON"]
    return body, update.withdrawal_problems(body)


def cmd_validate(args):
    run_dir = _run_dir(args)
    withdrawal, problems = _withdrawal_check(run_dir)
    if not (run_dir / "report.json").exists() and (
            withdrawal is not None or (run_dir / update.NOTES_FILE).exists()):
        # An update run with no picks: only its withdrawal (if any) to check.
        doc = {"accepted": [], "rejected": [], "dossier_bytes": {}}
        if withdrawal is not None:
            doc["withdrawal"] = {"items": len(withdrawal.get("items") or []),
                                 "problems": problems}
        _write_json(run_dir / "validate.json", doc)
        _print_problems(problems, [])
        print("validate: no report.json in this update run; "
              + (f"withdrawal.json: {len(withdrawal.get('items') or [])} items, "
                 f"{len(problems)} problems" if withdrawal is not None
                 else "nothing to check (the update changed nothing)"))
        return 0 if not problems else 1
    report = _read_json(run_dir / "report.json")
    now = _parse_now(args.now) or datetime.now(UTC)
    default_valid_until = None
    if (run_dir / build.SESSION_MARKER_FILENAME).exists():
        default_valid_until = now + build.DEFAULT_VALIDITY
    result = build.validate_report(report, now=now, default_valid_until=default_valid_until)
    doc = {
        "accepted": [outcome.symbol for outcome in result.accepted],
        "rejected": [{"symbol": o.symbol, "error": o.error} for o in result.rejected],
        "dossier_bytes": {o.symbol: o.dossier_bytes for o in result.accepted},
    }
    if withdrawal is not None:  # An update run's withdrawal, checked beside its report.
        doc["withdrawal"] = {"items": len(withdrawal.get("items") or []), "problems": problems}
    _write_json(run_dir / "validate.json", doc)
    print(f"validate: {len(result.accepted)} ok, {len(result.rejected)} rejected")
    if withdrawal is not None:
        _print_problems(problems, [])
        print(f"validate: withdrawal.json: {len(withdrawal.get('items') or [])} items, "
              f"{len(problems)} problems")
    return 0 if not result.rejected and not problems else 1


def cmd_submit(args):
    run_dir = _run_dir(args)
    marker_path = run_dir / build.SESSION_MARKER_FILENAME
    if marker_path.exists():
        print(
            f"error: SESSION_REPORT_REFUSED: {run_dir / 'report.json'} was built with "
            "--session for the session harness (scripts/agent_research_session.py) and "
            f"is marked {marker_path.name}; this command never sends it to a live app. "
            "Use the harness's own command instead: python scripts/agent_research_session.py "
            f"submit --root <SESSION_ROOT> --report {run_dir / 'report.json'}",
            file=sys.stderr,
        )
        return 2
    if (run_dir / update.WITHDRAWAL_FILE).exists():
        return _submit_update(args, run_dir)
    if not (run_dir / "report.json").exists() and (run_dir / update.NOTES_FILE).exists():
        print("submit: nothing to send: this update run changed nothing (update-notes.json)")
        return 0
    return _submit_report(args, run_dir)


def _submit_update(args, run_dir):
    """An update run's sends: withdrawal.json first, then report.json when there is one.
    Any answer but 200 to the withdrawal stops before the report is sent."""
    body, problems = _withdrawal_check(run_dir)
    if problems:
        _print_problems(problems, [])
        print(f"error: {update.WITHDRAWAL_FILE} not sent, and the report neither: "
              f"{len(problems)} problems", file=sys.stderr)
        return 2
    try:
        agent_token = _load_token(args)
    except token.TokenUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    code = _send_withdrawal(args, run_dir, body, agent_token)
    if code != 0:
        return code
    if not (run_dir / "report.json").exists():
        print("submit: this update run has no report.json (no adjusted or new picks); only "
              "the withdrawal was sent")
        return 0
    return _submit_report(args, run_dir, agent_token)


def _send_withdrawal(args, run_dir, body, agent_token):
    """POST withdrawal.json unchanged (a resend of the same withdrawal_id with the same body
    is a replay at the app); its answer goes to withdrawal-submit.json, never the token."""
    result_path = run_dir / update.WITHDRAWAL_RESULT_FILE
    recorded = _read_optional(result_path) or {}
    if (recorded.get("withdrawal_id") == body["withdrawal_id"]
            and recorded.get("status_code") == 200):
        print(f"submit: withdrawal {body['withdrawal_id']} is already recorded "
              f"({result_path.name}); not sent again")
        return 0
    try:
        result = submit.submit_withdrawal(args.base_url, agent_token, body)
    except submit.SubmitError as exc:
        _write_json(result_path, {"withdrawal_id": body["withdrawal_id"], "error": str(exc)})
        print(f"error: {exc}; whether the withdrawal was recorded is unknown. Run submit "
              "again: it resends the same withdrawal first. The report was not sent.",
              file=sys.stderr)
        return 1
    _write_json(result_path, {"withdrawal_id": body["withdrawal_id"],
                              "status_code": result.status_code, "body": result.body})
    answer = result.body or {}
    if result.status_code != 200:
        codes = ", ".join(str(error.get("code")) for error in answer.get("errors") or []
                          if isinstance(error, dict))
        print(f"error: withdrawal: HTTP {result.status_code} {answer.get('detail')}"
              + (f" ({codes})" if codes else "") + "; the report was not sent",
              file=sys.stderr)
        return 1
    results = [row for row in answer.get("results") or [] if isinstance(row, dict)]
    print(f"submit: withdrawal HTTP 200 {answer.get('status')}"
          + (" (a replay: already recorded)" if answer.get("idempotent_replay") else "")
          + ": " + (", ".join(f"{row.get('symbol')} {row.get('result')}" for row in results)
                    or "no results listed"))
    return 0


def _submit_report(args, run_dir, agent_token=None):
    report = _read_json(run_dir / "report.json")
    if agent_token is None:
        try:
            agent_token = _load_token(args)
        except token.TokenUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    # Stamped just before sending: the intake refuses a report whose generated_at is more
    # than max_report_age_seconds (60 in the deployed settings) older than its clock
    # (RESEARCH_REPORT_STALE_OR_FUTURE), and build, validate and a human review take longer.
    # Saved first, so report.json is exactly what was sent; a resend after a lost reply keeps
    # its report_id, so the server replays or refuses it and never opens a second cycle.
    report = {**report, "generated_at": datetime.now(UTC).isoformat()}
    _write_json(run_dir / "report.json", report)
    try:
        result = submit.submit_report(args.base_url, agent_token, report)
    except submit.SubmitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write_json(run_dir / "submit.json", {"status_code": result.status_code, "body": result.body})
    print(f"submit: HTTP {result.status_code}" + (" (accepted)" if result.accepted else ""))
    return 0 if result.accepted else 1


def cmd_movers(args):
    """The evening's first step: what moved over one New York day (or the last hours)."""
    now = _parse_now(args.now) or datetime.now(UTC)
    day = args.day
    try:
        if day is not None:
            start, end = movers.day_window(day)
            movers.check_day_over(end, now, day)
        else:
            start, end = movers.hours_window(now, args.hours)
    except movers.MoversError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_dir = _run_dir(args, evening_of=day)
    ctx = _read_json(run_dir / "context.json")
    symbols = context.coin_symbols(ctx)
    if not symbols:
        print("error: the context lists no coins (run 'context' first)", file=sys.stderr)
        return 1
    coins = sorted({symbol.split("/")[0] for symbol in symbols})
    try:
        fetched = market.fetch_hourly_span(coins, start=start - movers.BASELINE, end=end,
                                           now=now)
    except market.MarketDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = movers.build_movers(symbols, fetched, start=start, end=end, day=day)
    _write_json(run_dir / "movers.json", result)
    print(movers.summary_line(result))
    print(f"wrote {run_dir / 'movers.json'}")
    return 0


def cmd_lessons(args):
    """The morning's lessons, printed plainly, and emphasis.json (ordering hints only)."""
    run_dir = _run_dir(args)
    ctx = _read_json(run_dir / "context.json")
    now = _parse_now(args.now) or datetime.now(UTC)
    found, missing = context.usable_lessons(ctx)
    doc = lessons.emphasis_doc(found, context_as_of=ctx.get("as_of"), now=now,
                               unavailable=missing)
    print("\n".join(lessons.summary_lines(found, doc["hints"], unavailable=missing)))
    _write_json(run_dir / "emphasis.json", doc)
    print(f"wrote {run_dir / 'emphasis.json'}: {len(doc['hints'])} ordering hints "
          "(pass it to 'build --lessons')")
    return 0


# --- The morning outlook -----------------------------------------------------------------------

def cmd_outlook(args):
    """outlook.json: one entry per universe coin, facts filled in, answers kept on rebuild."""
    run_dir = _run_dir(args)
    now = _parse_now(args.now) or datetime.now(UTC)
    news_path = Path(args.news) if args.news else run_dir / "news.json"
    checklist_path = Path(args.checklist) if args.checklist else run_dir / "checklist-export.json"
    previous = _read_optional(run_dir / "outlook.json")
    try:
        worksheet = outlook.build_worksheet(
            ctx=_read_json(run_dir / "context.json"),
            market_data=_read_json(run_dir / "market.json"),
            levels_doc=_read_json(run_dir / "levels.json"),
            news_doc=_read_optional(news_path), checklist_doc=_read_optional(checklist_path),
            previous=previous, now=now)
    except outlook.OutlookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write_json(run_dir / "outlook.json", worksheet)
    filled, skipped, empty = outlook.filled_counts(worksheet)
    print(f"outlook worksheet: {len(worksheet['coins'])} coins, {filled} with a direction, "
          f"{skipped} skipped, {empty} to fill" + (
              f"; left the universe since the last build: "
              f"{', '.join(worksheet['dropped_since_last_build'])}"
              if worksheet["dropped_since_last_build"] else ""))
    print(f"wrote {run_dir / 'outlook.json'}")
    return 0


def _outlook_payload(worksheet, *, universe, ctx, args, run_dir):
    """``(payload or None, problems, notes)``: every check, the sources fetched again, the
    agent block, and the app's own screens. The payload's generated_at is stamped last."""
    problems, notes = outlook.check_worksheet(worksheet, universe=universe,
                                              agent_id=args.agent_id)
    if problems:
        return None, problems, notes
    verified, problems = records.verify_citations(outlook.citations(worksheet, universe))
    if problems:
        return None, problems, notes
    try:
        agent = records.agent_block(agent_id=args.agent_id, agent_version=args.agent_version,
                                    context=ctx, run=records.run_id(run_dir))
        sent_at = datetime.now(UTC)
        payload = outlook.assemble(worksheet, universe=universe, verified=verified, agent=agent,
                                   run_slot=records.run_slot(ctx, sent_at), generated_at=sent_at)
    except (records.RecordError, build.BuildError) as exc:
        return None, [str(exc)], notes
    problems = (records.screen(payload, max_bytes=outlook.MAX_BODY_BYTES)
                + records.app_model_problems(payload, model="MarketOutlook"))
    return (None if problems else payload), problems, notes


def _print_problems(problems, notes):
    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)


def _fresh_context(args, run_dir, agent_token):
    """The research context read again and saved as the run folder's context.json, so the
    universe the outlook is checked against is the one 'outlook' rebuilds from, and 'build'
    reads the fresher quotes."""
    fresh = context.fetch_context(args.base_url, agent_token)
    _write_json(run_dir / "context.json", fresh)
    print(f"saved the research context read now (as_of {fresh.get('as_of')}) to "
          f"{run_dir / 'context.json'}")
    return fresh


def cmd_outlook_check(args):
    """Every check outlook-submit makes, sources fetched again; nothing is sent. With
    --base-url it reads the research context again first (and saves it, like
    outlook-submit); without, it checks against context.json. outlook-check.json holds the
    result and the would-be body."""
    run_dir = _run_dir(args)
    worksheet = _read_json(run_dir / "outlook.json")
    if args.base_url:
        try:
            ctx = _fresh_context(args, run_dir, _load_token(args))
        except (token.TokenUnavailable, context.ContextError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        ctx = _read_json(run_dir / "context.json")
    payload, problems, notes = _outlook_payload(
        worksheet, universe=context.coin_symbols(ctx), ctx=ctx, args=args, run_dir=run_dir)
    _write_json(run_dir / "outlook-check.json",
                {"problems": problems, "notes": notes, "payload": payload})
    _print_problems(problems, notes)
    print(f"outlook-check: {'ready to send' if not problems else f'{len(problems)} problems'}")
    return 0 if not problems else 1


# The state of a run folder's outlook send, kept in outlook-submit.json:
# RECORDED: the app recorded it (202, or an idempotency refusal: this outlook_id is taken);
# REFUSED: the app refused it and stored nothing (fix it, then send again, same outlook_id);
# UNKNOWN: no answer (a transport error, or a 5xx other than 503): outlook-sent.json holds
# the exact bytes, which are sent again unchanged first.
OUTLOOK_SENT, OUTLOOK_RESULT = "outlook-sent.json", "outlook-submit.json"
RECORDED, REFUSED, UNKNOWN = "RECORDED", "REFUSED", "UNKNOWN"


def _outlook_state(result):
    if result.status_code == 202:
        return RECORDED
    detail = (result.body or {}).get("detail")
    if detail == "OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH":
        return RECORDED
    if result.status_code >= 500 and result.status_code != 503:
        return UNKNOWN
    return REFUSED


def _post_outlook(args, agent_token, body_bytes, run_dir, history, kind):
    """One POST of ``body_bytes``; its outcome is written to outlook-submit.json whatever it
    is, a transport error included. Returns ``(state, detail)``."""
    attempt = {"at": datetime.now(UTC).isoformat(), "kind": kind,
               "outlook_id": json.loads(body_bytes)["outlook_id"]}
    try:
        result = submit.submit_outlook(args.base_url, agent_token, body_bytes)
    except submit.SubmitError as exc:
        attempt.update(error=str(exc), state=UNKNOWN)
    else:
        attempt.update(status_code=result.status_code, body=result.body,
                       detail=(result.body or {}).get("detail"),
                       state=_outlook_state(result))
    history["attempts"].append(attempt)
    history["state"] = attempt["state"]
    _write_json(run_dir / OUTLOOK_RESULT, history)
    return attempt["state"], attempt.get("detail"), attempt


def _report_outlook(state, attempt):
    body = attempt.get("body") or {}
    if state == RECORDED and attempt.get("status_code") == 202:
        print(f"outlook-submit: HTTP 202 {body.get('status')}, outlook {body.get('outlook_id')}, "
              f"{body.get('coin_count')} coins ({body.get('skipped_count')} skipped), graded on "
              f"{body.get('window_start')} to {body.get('window_end')} in the reality of "
              f"{body.get('grading_day')}" + (" (a replay: it was already recorded)"
                                             if body.get("idempotent_replay") else ""))
        return 0
    if state == RECORDED:
        print("outlook-submit: HTTP 422 OUTLOOK_IDEMPOTENCY_CONTENT_MISMATCH: an outlook with "
              "this outlook_id is already recorded, from an earlier send that reached the app. "
              "It is graded as recorded; nothing more to send.", file=sys.stderr)
        return 1
    if state == UNKNOWN:
        print(f"outlook-submit: no answer ({attempt.get('error') or attempt.get('status_code')}); "
              "whether it was recorded is unknown. Run outlook-submit again: it resends the "
              "same bytes first.", file=sys.stderr)
        return 1
    print(f"outlook-submit: HTTP {attempt.get('status_code')} {attempt.get('detail')}: refused, "
          "nothing stored. Fix it and send again.", file=sys.stderr)
    return 1


def cmd_outlook_submit(args):
    """POST the filled outlook as MARKET_OUTLOOK_V1, checked against the universe the app
    serves right now (the research context is read again first, and saved).

    Resends are safe: the exact bytes of every send are saved in outlook-sent.json before
    the POST. When the last send got no answer, those bytes go out again unchanged first: a
    202 (a replay if it was recorded) settles it, and only MARKET_OUTLOOK_STALE_OR_FUTURE,
    proof the first send never landed, leads to a rebuild under the same outlook_id. An
    outlook already recorded is never sent again, except deliberately with --new-id, which
    records a second, separately graded outlook."""
    run_dir = _run_dir(args)
    worksheet = _read_json(run_dir / "outlook.json")
    try:
        agent_token = _load_token(args)
    except token.TokenUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    history = _read_optional(run_dir / OUTLOOK_RESULT) or {"attempts": [], "state": None}
    state = history.get("state") or (UNKNOWN if (run_dir / OUTLOOK_SENT).exists() else None)
    if args.new_id:
        print("warning: --new-id sends this outlook under a new outlook_id: the app records "
              "it as a second outlook and grades it separately.", file=sys.stderr)
        worksheet["outlook_id"] = str(uuid4())
        _write_json(run_dir / "outlook.json", worksheet)
        state = None
    elif state == RECORDED:
        print(f"outlook-submit: outlook {worksheet.get('outlook_id')} is already recorded (see "
              f"{OUTLOOK_RESULT}); nothing sent. Only a deliberately changed outlook goes "
              "out again, with --new-id, as a second graded outlook.")
        return 0
    elif state == UNKNOWN:
        state, detail, attempt = _post_outlook(args, agent_token,
                                               (run_dir / OUTLOOK_SENT).read_bytes(), run_dir,
                                               history, "RESEND")
        if detail != "MARKET_OUTLOOK_STALE_OR_FUTURE":
            return _report_outlook(state, attempt)
        print("note: the earlier send never landed (the app found its copy stale); "
              "rebuilding it under the same outlook_id")
    try:
        fresh = _fresh_context(args, run_dir, agent_token)
    except context.ContextError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload, problems, notes = _outlook_payload(
        worksheet, universe=context.coin_symbols(fresh), ctx=fresh, args=args, run_dir=run_dir)
    if problems:
        _print_problems(problems, notes)
        print(f"error: outlook not sent: {len(problems)} problems", file=sys.stderr)
        return 2
    _print_problems([], notes)
    payload["generated_at"] = datetime.now(UTC).isoformat()  # Just before the POST.
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    (run_dir / OUTLOOK_SENT).write_bytes(body_bytes)  # Saved before the POST, exactly.
    history["state"] = UNKNOWN  # Until an answer says otherwise, even if this process dies.
    _write_json(run_dir / OUTLOOK_RESULT, history)
    state, _detail, attempt = _post_outlook(args, agent_token, body_bytes, run_dir, history,
                                            "SEND")
    return _report_outlook(state, attempt)


# --- The evening post-mortem -------------------------------------------------------------------

def _bars_for(now):
    def fetch(symbol, start, end):
        coin = symbol.split("/")[0]
        try:
            fetched = market.fetch_hourly_span([coin], start=start, end=end, now=now)
        except market.MarketDataError as exc:
            return None, str(exc), None
        raw = (fetched.get("coinbase") or {}).get(coin)
        if raw is None:
            return (None, (fetched.get("excluded") or {}).get(coin, "NO_COINBASE_DATA"),
                    fetched["retrieved_at"])
        return (technicals.complete_hourly_bars(raw["candles_1h"], retrieved_at=now), None,
                fetched["retrieved_at"])
    return fetch


def cmd_postmortem(args):
    """postmortem.json for the post-mortems the app says are owed, once it has recorded the
    New York day (--day, default yesterday) in the context's lessons."""
    now = _parse_now(args.now) or datetime.now(UTC)
    day = args.day or postmortem.yesterday(now)
    run_dir = _run_dir(args, evening_of=day)
    ctx = _read_json(run_dir / "context.json")
    found, missing = context.usable_lessons(ctx)
    previous = _read_optional(run_dir / "postmortem.json")
    if missing == context.LESSONS_UNAVAILABLE:
        # No subjects can be listed this run; the owed ones stay pending for the next.
        print(f"note: lessons unavailable this run ({missing}): no post-mortem subjects; "
              "the owed ones stay pending for the next run")
        if previous is not None:
            print(f"kept {run_dir / 'postmortem.json'} as it was (it holds your answers)")
            return 0
        worksheet = postmortem.empty_worksheet(ctx=ctx, day=day, now=now, code=missing)
    else:
        try:
            worksheet = postmortem.build_worksheet(
                ctx=ctx, lessons=found, day=day, bars_for=_bars_for(now), previous=previous,
                now=now)
        except postmortem.PostmortemError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    _write_json(run_dir / "postmortem.json", worksheet)
    for note in worksheet["reality"]["notes"]:
        print(f"note: {note}")
    print(postmortem.summary_line(worksheet))
    print(f"wrote {run_dir / 'postmortem.json'}")
    return 0


def _postmortem_problems(rows, agent_id):
    """Every refusal the kit can see for ``rows``, the sources fetched again for the rows
    whose structure passed; returns ``(problems, verified)``."""
    problems, clean = [], []
    for row in rows:
        found = postmortem.check_subject(row, agent_id=agent_id)
        problems.extend(found)
        if not any(".sources" in problem for problem in found):
            clean.append(row)
    cited = [pair for row in clean for pair in postmortem.citations(row)]
    verified, source_problems = records.verify_citations(cited)
    return problems + source_problems, verified


def cmd_postmortem_check(args):
    """The helper's suggestions written into postmortem.json, and every problem the send
    would refuse; nothing is sent."""
    now = _parse_now(args.now) or datetime.now(UTC)
    run_dir = _run_dir(args, evening_of=args.day or postmortem.yesterday(now))
    worksheet = _read_json(run_dir / "postmortem.json")
    rows = worksheet.get("subjects") or []
    for row in rows:
        row["knowable_helper"] = postmortem.knowable_helper(row)
    _write_json(run_dir / "postmortem.json", worksheet)
    open_ = postmortem.undecided(worksheet)
    optional = [row["subject_id"] for row in rows
                if not postmortem.is_decided(row) and not row.get("required", True)]
    problems, _verified = _postmortem_problems([row for row in rows if postmortem.is_decided(row)],
                                               args.agent_id)
    for row in rows:
        helper = row["knowable_helper"]
        print(f"{row['subject_id']}: helper suggests knowable_before_move "
              f"{json.dumps(helper['suggestion'])} ({helper['why']})"
              + (f"; reads as {helper['label']}" if helper["label"] else ""))
    _write_json(run_dir / "postmortem-check.json",
                {"undecided": open_, "optional_undecided": optional, "problems": problems})
    notes = [f"{len(open_)} subjects still to decide: {', '.join(open_)}"] if open_ else []
    notes += ([f"{len(optional)} optional trades (no notable reason) not decided, not sent: "
               f"{', '.join(optional)}"] if optional else [])
    _print_problems(problems, notes)
    ready = not problems and not open_
    print(f"postmortem-check: {'ready to send' if ready else 'not ready'}")
    return 0 if ready else 1


SUBMISSIONS = "postmortem-submit.json"


def _submissions(run_dir, day):
    return _read_optional(run_dir / SUBMISSIONS) or {
        "schema": checklist.SUBMISSIONS_SCHEMA, "day": day, "attempts": []}


def _accepted_before(history):
    return {item["subject_id"] for attempt in history.get("attempts") or []
            for note in attempt.get("notes") or [] for item in note.get("items") or []
            if item.get("status") == "ACCEPTED"}


def _sent_item(row, index):
    """What the kit records of one sent item: the app's answer is added to it. The factors
    (never sent) are kept here, so the research checklist counts only accepted items."""
    return {"index": index, "subject_id": row["subject_id"], "subject": row["subject"],
            "day": row["day"], "notable": row.get("required", True),
            "cause": row["cause"], "knowable_before_move": row["knowable_before_move"],
            "factors": row.get("factors") or [], "status": None, "code": None}


def cmd_postmortem_submit(args):
    """POST the decided post-mortems as POST_MORTEM_V1 notes of up to 30 items each.
    postmortem-submit.json records every note: what was sent, the factors behind it, and
    the app's answer item by item (the checklist's only source for post-mortem evidence)."""
    now = _parse_now(args.now) or datetime.now(UTC)
    run_dir = _run_dir(args, evening_of=args.day or postmortem.yesterday(now))
    worksheet = _read_json(run_dir / "postmortem.json")
    ctx = _read_json(run_dir / "context.json")
    open_ = postmortem.undecided(worksheet)
    if open_:
        print(f"error: {len(open_)} subjects still to decide: {', '.join(open_)}",
              file=sys.stderr)
        return 2
    try:
        agent_token = _load_token(args)
        agent = records.agent_block(agent_id=args.agent_id, agent_version=args.agent_version,
                                    context=ctx, run=records.run_id(run_dir))
    except (token.TokenUnavailable, records.RecordError, build.BuildError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    history = _submissions(run_dir, worksheet.get("day"))
    done = _accepted_before(history)
    subjects = worksheet.get("subjects") or []
    optional = [row["subject_id"] for row in subjects if not postmortem.is_decided(row)]
    rows = [row for row in subjects
            if postmortem.is_decided(row) and row["subject_id"] not in done]
    if done:
        print(f"note: {len(done)} subjects already accepted by an earlier send, not resent")
    if optional:
        print(f"note: {len(optional)} optional trades (no notable reason) not decided, not "
              f"sent: {', '.join(optional)}")
    if not rows:
        print("postmortem-submit: nothing left to send")
        return 0
    problems, verified = _postmortem_problems(rows, args.agent_id)
    if problems:
        _print_problems(problems, [])
        print(f"error: post-mortems not sent: {len(problems)} problems", file=sys.stderr)
        return 2
    attempt = {"at": datetime.now(UTC).isoformat(), "notes": []}
    history["attempts"].append(attempt)
    all_accepted = True
    for batch in postmortem.batches(rows):
        body = postmortem.note([postmortem.item(row, verified) for row in batch], agent=agent,
                               generated_at=datetime.now(UTC))
        record = {"note_id": body["note_id"],
                  "items": [_sent_item(row, index) for index, row in enumerate(batch)]}
        attempt["notes"].append(record)
        screened = (records.screen(body, max_bytes=postmortem.MAX_BODY_BYTES)
                    + records.app_model_problems(body, model="PostMortemEnvelope",
                                                 items_model="PostMortemItem"))
        if screened:
            record["refused_locally"] = screened
            all_accepted = False
            _print_problems(screened, [])
            break
        try:
            result = submit.submit_post_mortem(args.base_url, agent_token, body)
        except submit.SubmitError as exc:
            record["error"] = str(exc)
            all_accepted = False
            print(f"error: {exc}", file=sys.stderr)
            break
        answer = result.body or {}
        record.update(status_code=result.status_code, detail=answer.get("detail"))
        for entry in answer.get("item_results") or []:
            index = entry.get("index")
            if isinstance(index, int) and 0 <= index < len(batch):
                record["items"][index].update(status=entry.get("status"), code=entry.get("code"),
                                              subject_key=entry.get("subject_key"))
        accepted = sum(item["status"] == "ACCEPTED" for item in record["items"])
        all_accepted &= result.accepted and accepted == len(batch)
        print(f"postmortem-submit: note {body['note_id']}: HTTP {result.status_code}, "
              f"{accepted} of {len(batch)} items accepted")
        for item in record["items"]:
            if item["status"] != "ACCEPTED":
                print(f"  {item['subject_id']}: {item['code'] or answer.get('detail')}",
                      file=sys.stderr)
    _write_json(run_dir / SUBMISSIONS, history)
    return 0 if all_accepted else 1


def _state_dir(args):
    return (Path(args.state_dir).expanduser() if getattr(args, "state_dir", None)
            else checklist.DEFAULT_STATE_DIR)


def cmd_checklist(args):
    """The research checklist in the owner-only state folder: show, update or export."""
    now = _parse_now(getattr(args, "now", None)) or datetime.now(UTC)
    state_dir = _state_dir(args)
    try:
        state = checklist.load(state_dir)
        if args.action == "show":
            print("\n".join(checklist.show_lines(state)))
            return 0
        if args.action == "export":
            exported = checklist.export(state, now=now)
            out = Path(args.out) if args.out else _run_dir(args) / "checklist-export.json"
            _write_json(out, exported)
            print(f"exported {len(exported['items'])} ACTIVE checklist items to {out} "
                  "(they set what is checked first; every coin is still researched)")
            return 0
        if not args.inputs:
            print("error: checklist update needs at least one --from movers.json or "
                  "postmortem.json", file=sys.stderr)
            return 2
        found, scopes, lessons, missing = [], [], None, None
        for name in args.inputs:
            doc = _read_json(Path(name))
            if doc.get("schema") == checklist.MOVERS_SCHEMA:
                if lessons is None:
                    lessons_path = (Path(args.lessons) if args.lessons
                                    else _run_dir(args) / "context.json")
                    lessons, missing = context.usable_lessons(_read_json(lessons_path))
                if missing:
                    print(f"note: lessons unavailable this run ({missing}): the TECHNICAL "
                          f"patterns of {name} are not scored (they need the app's record of "
                          "the day)")
                    continue
                items, covered = checklist.movers_evidence(doc, lessons)
            elif doc.get("schema") == checklist.SUBMISSIONS_SCHEMA:
                items, covered, left_out = checklist.submission_evidence(doc)
                if left_out:
                    print(f"note: {len(left_out)} accepted trades with no notable reason are "
                          f"not counted: {', '.join(left_out)}")
            elif doc.get("schema") == checklist.POSTMORTEM_SCHEMA:
                print(f"error: CHECKLIST_WORKSHEET_NOT_EVIDENCE: {name} is the worksheet; pass "
                      "postmortem-submit.json, where only the items the app accepted count",
                      file=sys.stderr)
                return 2
            else:
                print(f"error: CHECKLIST_INPUT_UNKNOWN: {name} is neither a movers.json nor a "
                      "postmortem-submit.json", file=sys.stderr)
                return 2
            found.extend(items)
            scopes.extend(covered)
        report = checklist.apply(state, found, now=now, inputs=args.inputs, scopes=scopes)
        checklist.save(state_dir, state)
    except checklist.ChecklistError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"checklist update: {report['added']} occurrences added, {report['corrected']} "
          f"corrected, {report['withdrawn']} withdrawn, {report['unchanged']} already "
          f"recorded, {len(report['new_patterns'])} new patterns")
    for line in report["status_changes"]:
        print(f"  {line}")
    return 0


# --- Research loop V2: update runs and the daily run's derivatives context --------------------

# Written by an earlier update in the same folder and replaced by this one's.
_UPDATE_OUTPUTS = (update.WITHDRAWAL_FILE, "report.json", "validate.json", update.NOTES_FILE,
                   build.SESSION_MARKER_FILENAME, "emphasis.json")


LESSONS_FROM_CONTEXT = "context"


def _update_emphasis(args, run_dir, ctx, now):
    """``(hints or None, record for update-notes.json or None)`` for ``update --lessons``
    (package learning-loop2, plan L3): ``--lessons`` alone derives the emphasis from this run's
    research context (written to the run folder's ``emphasis.json``, as ``lessons`` writes it);
    ``--lessons PATH`` reads an ``emphasis.json``. Hints only reorder; raises ``LessonsError``."""
    source = getattr(args, "lessons", None)
    if not source:
        return None, None
    if source == LESSONS_FROM_CONTEXT:
        found, missing = context.usable_lessons(ctx)
        doc = lessons.emphasis_doc(found, context_as_of=ctx.get("as_of"),
                                   now=now or datetime.now(UTC), unavailable=missing)
        _write_json(run_dir / "emphasis.json", doc)
        for line in lessons.summary_lines(found, doc["hints"], unavailable=missing)[-12:]:
            print(f"lessons: {line}")
        origin = "context.json"
    else:
        doc = _read_json(Path(source))
        origin = source
    hints = lessons.load_emphasis(doc)
    brief = ((context.usable_lessons(ctx)[0] or {}).get("daily_brief") or {})
    return hints, {"source": origin, "hints": [hint["text"] for hint in hints],
                   "scorecard_day": doc.get("scorecard_day"),
                   "research_focus": [item.get("text") for item in
                                      brief.get("research_focus") or []],
                   "use": lessons.USE}


def _update_build(args, run_dir, ctx, raw_market, result, profile, now, emphasis=None):
    """``(report or None, build notes, symbols in the report, {symbol: why left out})`` for
    the update's adjusted picks (built first) and new coins."""
    adjusted = result.of(update.ADJUSTED)
    levels_by_coin = {**result.new_coins, **result.skipped_coins,
                      **{decision.coin: (decision.setup, list(decision.tried))
                         for decision in adjusted}}
    if not any(setup is not None for setup, _tried in levels_by_coin.values()):
        return None, None, set(), {}
    max_picks = args.max_picks
    max_new = getattr(args, "max_new_picks", None)
    if max_new is not None:
        # --max-new-picks: the adjusted picks (built first) plus at most this many new coins.
        max_picks = min(max_picks, sum(1 for d in adjusted if d.setup is not None) + max_new)
    built = build.build_report(
        context=ctx, market_data=raw_market, levels_by_coin=levels_by_coin,
        agent_id=args.agent_id, agent_version=args.agent_version, now=now,
        max_picks=max_picks, run_id=records.run_id(run_dir), profile=profile,
        first_coins=frozenset(decision.coin for decision in adjusted),
        evidence=_evidence(args), emphasis=emphasis)
    with_setup = {f"{coin}/USD" for coin, (setup, _t) in levels_by_coin.items() if setup}
    left_out = {outcome.symbol: f"refused by the app's own models ({outcome.error})"
                for outcome in built.rejected}
    left_out.update({row["symbol"]: row["reason"] for row in built.skipped
                     if row["symbol"] in with_setup})
    notes = {"accepted": [outcome.symbol for outcome in built.accepted],
             "rejected": [{"symbol": o.symbol, "error": o.error} for o in built.rejected],
             "skipped": list(built.skipped), "notes": list(built.notes)}
    if built.lessons is not None:
        notes["lessons"] = built.lessons
    return built.report, notes, {outcome.symbol for outcome in built.accepted}, left_out


def cmd_update(args):
    """An update run of RESEARCH_SCHEDULE_V2 (update.py): the research context (read now
    with --base-url, else the run folder's), market and levels exactly as 'market' and
    'levels' fetch and find them, the review of the agent's own WATCHING setups, then
    withdrawal.json, report.json and update-notes.json, checked as 'validate' checks them.
    It never sends anything: 'submit' sends the withdrawal first, then the report."""
    run_dir = _run_dir(args)
    now = _parse_now(args.now) or datetime.now(UTC)
    profile = _profile(args)
    try:
        update.check_folder(run_dir)
        build.check_agent(args.agent_id, args.agent_version)
    except (update.UpdateRefused, build.BuildError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.base_url:
        code = cmd_context(args)
        if code != 0:
            return code
    else:
        print(f"note: no --base-url: reviewing against {run_dir / 'context.json'} as it is")
    ctx = _read_json(run_dir / "context.json")
    try:
        if context.is_offline(ctx):
            raise update.UpdateRefused("REAL_RESEARCH_CONTEXT_REQUIRED: an update reviews the "
                                       "agent's own setups, which only the app's research "
                                       "context lists")
        run_slot, _limit = update.answered_slot(ctx.get("schedule"), now)
    except update.UpdateRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write_json(run_dir / update.MARKER_FILE, {"command": "update", "profile": profile.name,
                                               "run_slot": run_slot,
                                               "started_at": now.isoformat()})
    for name in _UPDATE_OUTPUTS:  # An earlier update's outputs in this folder, replaced now.
        (run_dir / name).unlink(missing_ok=True)
    print(f"update: answering the {run_slot} run (an update run) under {profile.name}")
    for step in (cmd_market, cmd_levels):
        code = step(args)
        if code != 0:
            return code
    raw_market = _read_json(run_dir / "market.json")
    result = update.review(ctx, raw_market, _read_json(run_dir / "levels.json"),
                           profile=profile, now=now)
    # Method v9: the watched setups the review keeps or adjusts are re-checked against the
    # same evidence rules as a new pick (update.premise_review); one whose premise no longer
    # holds is withdrawn with the reason, before the adjusted picks are built.
    result = update.premise_review(result, ctx, raw_market, _evidence(args))
    try:
        emphasis, lessons_record = _update_emphasis(args, run_dir, ctx, _parse_now(args.now))
    except lessons.LessonsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # The report is stamped when it is built, after the market read, exactly as 'build' stamps
    # it. Stamped with the update's start (``now``), every bar fetched since would read as
    # retrieved after the report, and the app's own models would refuse each pick
    # FUTURE_TECHNICAL_EVIDENCE (the live dry run of 2026-09-28). ``--now`` still fixes both.
    try:
        report, build_notes, in_report, left_out = _update_build(
            args, run_dir, ctx, raw_market, result, profile, _parse_now(args.now),
            emphasis=emphasis)
    except build.BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if report is not None and (datetime.fromisoformat(report["run_slot"])
                               != datetime.fromisoformat(run_slot)):
        print(f"error: UPDATE_SLOT_CHANGED: the report was built for the {report['run_slot']} "
              f"run, not the {run_slot} run this update reviewed; nothing was written. Run the "
              "update again in a new folder.", file=sys.stderr)
        return 2
    files = {"withdrawal": None, "report": None}
    withdrawn = result.of(update.WITHDRAWN)
    over_limit = {decision.symbol for decision in withdrawn[update.MAX_WITHDRAWAL_ITEMS:]}
    if withdrawn:
        _write_json(run_dir / update.WITHDRAWAL_FILE, update.withdrawal_body(
            withdrawn, agent_id=args.agent_id, agent_version=args.agent_version))
        files["withdrawal"] = update.WITHDRAWAL_FILE
    if report is not None:
        _write_json(run_dir / "report.json", report)
        files["report"] = "report.json"
    notes = update.notes_doc(
        result, profile=profile, run_slot=run_slot, context_as_of=ctx.get("as_of"),
        market_retrieved_at=raw_market.get("retrieved_at"), now=now,
        max_picks=args.max_picks, in_report=in_report, left_out=left_out,
        build_notes=build_notes, files=files, withdrawn_left_out=over_limit)
    notes["lessons"] = lessons_record  # update --lessons (package learning-loop2), else None.
    _write_json(run_dir / update.NOTES_FILE, notes)
    for note in result.notes:
        print(f"note: {note}")
    for decision in result.decisions:
        print(f"  {decision.action} {decision.symbol}: {decision.reason}")
    for symbol, reason in sorted(left_out.items()):
        print(f"  LEFT OUT {symbol}: {reason}")
    print(f"update: {len(notes['kept'])} kept, {len(notes['adjusted'])} adjusted, "
          f"{len(notes['withdrawn'])} withdrawn, {len(notes['new'])} new picks; "
          f"{len(in_report)} picks in report.json; {len(result.open_symbols)} coins with an "
          f"open trade left alone (update-notes.json)")
    if notes["nothing_to_send"]:
        print("update: nothing to change: no withdrawal and no picks, so nothing to send")
        return 0
    code = cmd_validate(args)
    if code == 0:
        print("update: review " + " and ".join(name for name in files.values() if name)
              + ", then run 'submit': it sends the withdrawal first, then the report")
    return code


def cmd_derivatives(args):
    """derivatives.json: OKX open interest and Hyperliquid funding for the coins with a
    setup in levels.json (the would-be picks), for 'build --derivatives'. Context for Jev
    only (derivatives.py): it chooses no coin and sets no level."""
    run_dir = _run_dir(args)
    raw_levels = _read_json(run_dir / "levels.json")
    coins = sorted(coin for coin, row in raw_levels.items() if row.get("setup"))
    now = _parse_now(args.now)
    result = derivatives.fetch_derivatives(coins, clock=(lambda: now) if now else None)
    crowded = []
    for coin, entry in result["coins"].items():
        entry["measures"] = derivatives.readable(derivatives.summarize(coin, entry))
        if entry["measures"]["crowded"]:
            crowded.append(coin)
    _write_json(run_dir / "derivatives.json", result)
    with_oi = sum(entry["okx"] is not None for entry in result["coins"].values())
    with_funding = sum(entry["hyperliquid"] is not None for entry in result["coins"].values())
    for coin, reasons in sorted(derivatives.omitted(result).items()):
        print(f"  {coin}: " + "; ".join(f"{name}: {why}" for name, why in reasons.items()))
    print(f"wrote {run_dir / 'derivatives.json'}: {len(coins)} coins with a setup; OKX open "
          f"interest for {with_oi}, Hyperliquid funding for {with_funding}; crowded long "
          f"positioning: {', '.join(crowded) or 'none'} (pass it to 'build --derivatives')")
    return 0


# --- Answering the app's reviews and exit flags (MUSE_ANSWER_RULES_V1) ------------------------

def cmd_answer(args):
    """The app's pending window reviews and Jev early-exit flags (``GET /api/v1/lab/reviews``),
    each decided under MUSE_ANSWER_RULES_V1 (``answers.py``) from Coinbase's 5-minute candles
    and answered with one AGENT_REVIEW_ANSWER_V1 to its own route. Every item's record (the item,
    the candles and bar used, the decision and why, the body, every response) is
    ``<run-dir>/items/<item>.json``; every run adds its line to ``<run-dir>/polls/<UTC
    day>.jsonl``. Safe to repeat every few minutes: an item is decided once and its body resent
    unchanged until the app answers; nothing pending is one GET. ``--dry-run`` writes the
    answers without sending them."""
    if not getattr(args, "run_dir", None):
        print("error: answer needs --run-dir: one folder kept for every run (for example "
              "runs/answers), where each item's answer is fixed before it is sent",
              file=sys.stderr)
        return 2
    try:
        build.check_agent(args.agent_id, args.agent_version)
        submit.checked_base_url(args.base_url)
    except (build.BuildError, submit.BaseUrlRefused) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_dir = _run_dir(args)
    fixed_now = _parse_now(args.now)
    with answers.run_lock(run_dir) as held:
        if not held:
            print(f"answer: another answer run holds {run_dir / answers.LOCK_FILE}; this one "
                  "does nothing")
            return 0
        try:
            agent_token = _load_token(args)
        except token.TokenUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        now = fixed_now or datetime.now(UTC)
        record = {"now": now, "dry_run": args.dry_run, "agent_id": args.agent_id,
                  "agent_version": args.agent_version}
        try:
            pending = answers.fetch_pending(args.base_url, agent_token)
        except answers.AnswerError as exc:
            answers.append_poll(run_dir, answers.poll_line(pending=None, outcomes=[],
                                                           error=str(exc), **record), now)
            print(f"error: {exc}; nothing was answered", file=sys.stderr)
            return 1
        outcomes = answers.answer_pending(
            pending, run_dir=run_dir, agent_id=args.agent_id,
            agent_version=args.agent_version, now=now, dry_run=args.dry_run,
            fetch=lambda coin: answers.read_candles(coin, now=fixed_now),
            send=lambda kind, item_id, body: submit.submit_answer(
                args.base_url, agent_token, kind, item_id, body))
        path = answers.append_poll(run_dir, answers.poll_line(pending=pending,
                                                              outcomes=outcomes, **record), now)
    if not outcomes:
        print(f"answer: nothing pending (as of {pending.get('as_of')}); {path.name} updated")
        return 0
    for outcome in outcomes:
        print(outcome.line(), file=sys.stderr if outcome.state in answers.FAILED_STATES
              or outcome.action in (answers.UNREADABLE, answers.RECORD_UNREADABLE)
              else sys.stdout)
    print(f"answer: {len(outcomes)} pending item(s) under {answers.RULES_VERSION}"
          + (" (dry run: nothing sent)" if args.dry_run else "")
          + f"; records in {run_dir / answers.ITEMS_DIR}")
    return answers.exit_code(outcomes)


def cmd_all(args):
    """The deterministic steps only: context, market, levels, build, validate. Never
    submit — that stays an explicit, separate step (see the module docstring)."""
    for step in (cmd_context, cmd_market, cmd_levels, cmd_build, cmd_validate):
        code = step(args)
        if code != 0:
            return code
    print("all: deterministic steps done. Review report.json, then run 'submit' "
         "explicitly to send it.")
    return 0


# --- Argument parsing --------------------------------------------------------------------------

def _add_now(parser):
    parser.add_argument("--now", help="Override 'now' (RFC3339), for reproducible runs.")


def _add_token_file(parser):
    parser.add_argument("--token-file", help="Path to a file holding the agent's bearer token "
                        f"(else {token.TOKEN_FILE_ENV}/{token.TOKEN_ENV}).")


def _add_profile(parser):
    described = "; ".join(
        f"{key} = {profile.name} ({', '.join(profile.timeframes)}, entries "
        f"{profile.band_text} below the mid)" for key, profile in levels.PROFILES.items())
    parser.add_argument(
        "--profile", choices=tuple(levels.PROFILES), default="daily",
        help=("The research profile (default daily, the 08:00 run; intraday for the 2-hourly "
              f"runs): {described}. Use the same one for market, levels and build.")
        .replace("%", "%%"),
    )


def _add_session(parser):
    parser.add_argument(
        "--session", action="store_true",
        help="Build for the supervised session harness "
             "(scripts/agent_research_session.py), which has no research-context route: "
             "accepts an offline (context --offline) or otherwise schedule-less context, "
             "and omits run_slot/context_as_of/valid_until for the harness's own "
             "'submit --report' to fill. Use agent-id 'fable' (the harness's fixed "
             "agent identity) for the harness to accept the report. Marks the run folder "
             "SESSION_ONLY: this CLI's own 'submit' then refuses to send it to a live "
             "app. See 'testing against a session harness' in DAILY_PROCEDURE.md.",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m research_agent.run",
        description="The daily research-agent toolkit: research context, market data, level "
                    "rules, report building and submission.",
    )
    parser.add_argument("--run-dir", help="One run's folder (default: runs/<NY date>).")
    sub = parser.add_subparsers(dest="command", required=True)

    context_parser = sub.add_parser("context", help="Read the research context, or the "
                                    "offline development fallback.")
    _add_now(context_parser)
    context_parser.add_argument("--base-url", help="The app's base URL, e.g. "
                                "http://127.0.0.1:8000 (required without --offline).")
    context_parser.add_argument("--offline", action="store_true", help="Use the "
                                "Alpaca-quotes-only development fallback instead of the app.")
    _add_token_file(context_parser)
    context_parser.set_defaults(func=cmd_context)

    market_parser = sub.add_parser("market", help="Fetch Coinbase public candles for the "
                                   "context's coins (what --profile needs).")
    _add_now(market_parser)
    _add_profile(market_parser)
    market_parser.set_defaults(func=cmd_market)

    levels_parser = sub.add_parser("levels", help="Find a qualifying level setup for each coin "
                                   "under --profile's rules.")
    _add_profile(levels_parser)
    levels_parser.set_defaults(func=cmd_levels)

    build_parser_ = sub.add_parser("build", help="Build the AGENT_RESEARCH_REPORT_V3 from "
                                   "context, market, levels and (optionally) news.json.")
    _add_now(build_parser_)
    build_parser_.add_argument("--news", help="Path to news.json (omit for CHART-only picks).")
    build_parser_.add_argument("--agent-id", required=True)
    build_parser_.add_argument("--agent-version", required=True)
    build_parser_.add_argument("--max-picks", type=int, default=None)
    build_parser_.add_argument("--lessons", help="emphasis.json from 'lessons': rank picks by "
                               "its ordering hints (never drops a coin).")
    build_parser_.add_argument("--derivatives", help="derivatives.json from 'derivatives': add "
                               "each pick's OKX open interest and Hyperliquid funding as cited "
                               "sources (context only: the same picks, order and levels).")
    _add_evidence(build_parser_)
    _add_session(build_parser_)
    _add_profile(build_parser_)
    build_parser_.set_defaults(func=cmd_build)

    derivatives_parser = sub.add_parser(
        "derivatives", help="derivatives.json: OKX open interest (4 and 24 hours) and the latest "
        "Hyperliquid funding for the coins with a setup in levels.json, for 'build "
        "--derivatives'. Public data reads only; context for Jev, never a selection input.")
    _add_now(derivatives_parser)
    derivatives_parser.set_defaults(func=cmd_derivatives)

    update_parser = sub.add_parser(
        "update", help="An update run of RESEARCH_SCHEDULE_V2 (every run but the daily one): "
        "review the agent's own WATCHING setups, then write withdrawal.json, report.json "
        "(adjusted and new picks) and update-notes.json, and validate them. Refuses a V1 "
        "schedule and the daily full run's slot. Never submits.")
    _add_now(update_parser)
    update_parser.add_argument("--base-url", help="The app's base URL: read the research "
                               "context now (without it, the run folder's context.json).")
    _add_token_file(update_parser)
    update_parser.add_argument("--agent-id", required=True)
    update_parser.add_argument("--agent-version", required=True)
    update_parser.add_argument("--max-picks", type=int, default=update.DEFAULT_MAX_PICKS,
                               help="Adjusted and new picks together, adjusted first "
                               f"(default {update.DEFAULT_MAX_PICKS}).")
    update_parser.add_argument("--max-new-picks", type=_nonnegative_int, default=None,
                               help="At most this many new coins in the report, after the "
                               "adjusted picks (within --max-picks).")
    update_parser.add_argument(
        "--lessons", nargs="?", const=LESSONS_FROM_CONTEXT, default=None,
        help="Apply the lessons' ordering hints to the new picks (adjusted picks stay first; "
        "never drops a coin): alone, derived from this run's research context and written to "
        "emphasis.json; with a path, that emphasis.json.")
    _add_evidence(update_parser)
    _add_profile(update_parser)
    update_parser.set_defaults(func=cmd_update, offline=False)

    answer_parser = sub.add_parser(
        "answer", help="Answer the app's pending window reviews and Jev early-exit flags under "
        f"{answers.RULES_VERSION} (GET /api/v1/lab/reviews; one AGENT_REVIEW_ANSWER_V1 per "
        "item, decided from Coinbase's last completed 5-minute bar). Safe to run every 2-5 "
        "minutes with the same --run-dir: nothing pending is one GET, and an item's answer is "
        "fixed before it is sent.")
    _add_now(answer_parser)
    answer_parser.add_argument("--base-url", required=True, help="The app's base URL.")
    _add_token_file(answer_parser)
    answer_parser.add_argument("--agent-id", required=True)
    answer_parser.add_argument("--agent-version", required=True)
    answer_parser.add_argument("--dry-run", action="store_true", help="Decide and write each "
                               "answer in the run folder without sending it (the pending items "
                               "are still read from the app).")
    answer_parser.set_defaults(func=cmd_answer)

    lessons_parser = sub.add_parser("lessons", help="Print the context's lessons plainly and "
                                    "write emphasis.json (ordering hints for 'build').")
    _add_now(lessons_parser)
    lessons_parser.set_defaults(func=cmd_lessons)

    outlook_parser = sub.add_parser("outlook", help="outlook.json: the morning outlook "
                                    "worksheet, one entry per universe coin (answers kept on "
                                    "rebuild).")
    _add_now(outlook_parser)
    outlook_parser.add_argument("--news", help="news.json (default: the run folder's).")
    outlook_parser.add_argument("--checklist", help="checklist-export.json (default: the run "
                                "folder's).")
    outlook_parser.set_defaults(func=cmd_outlook)

    for name, func, help_text in (
        ("outlook-check", cmd_outlook_check, "Every check outlook-submit makes (sources "
         "fetched again); sends nothing. With --base-url, against the context read now."),
        ("outlook-submit", cmd_outlook_submit, "POST the filled outlook as MARKET_OUTLOOK_V1, "
         "after reading the research context again; a send with no answer is resent "
         "byte for byte first."),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--agent-id", required=True)
        command.add_argument("--agent-version", required=True)
        command.add_argument("--base-url", required=name == "outlook-submit")
        _add_token_file(command)
        if name == "outlook-submit":
            command.add_argument("--new-id", action="store_true", help="Deliberately send a "
                                 "changed outlook under a new outlook_id: a second outlook, "
                                 "graded separately.")
        command.set_defaults(func=func)

    postmortem_parser = sub.add_parser("postmortem", help="postmortem.json: the post-mortems "
                                       "the context's lessons say are owed (evening).")
    _add_now(postmortem_parser)
    postmortem_parser.add_argument("--day", type=_day_arg, help="The New York day reviewed "
                                   "(default: yesterday). The run folder defaults to "
                                   "runs/<day>/evening.")
    postmortem_parser.set_defaults(func=cmd_postmortem)

    for name, func, help_text in (
        ("postmortem-check", cmd_postmortem_check, "The knowable-before-move helper and every "
         "check postmortem-submit makes; sends nothing."),
        ("postmortem-submit", cmd_postmortem_submit, "POST the decided post-mortems as "
         "POST_MORTEM_V1 notes (up to 30 items each)."),
    ):
        command = sub.add_parser(name, help=help_text)
        _add_now(command)
        command.add_argument("--day", type=_day_arg, help="The New York day reviewed (default: "
                             "yesterday), for the default run folder.")
        command.add_argument("--agent-id", required=True)
        if name == "postmortem-submit":
            command.add_argument("--agent-version", required=True)
            command.add_argument("--base-url", required=True)
            _add_token_file(command)
        command.set_defaults(func=func)

    validate_parser = sub.add_parser("validate", help="Re-check report.json with the app's "
                                     "own models and dossier compiler.")
    _add_now(validate_parser)
    validate_parser.set_defaults(func=cmd_validate)

    submit_parser = sub.add_parser("submit", help="POST report.json to the app; after an "
                                   "'update', withdrawal.json first (any answer but 200 stops "
                                   "before the report). Refuses a --session report (see "
                                   "'build --session' help).")
    submit_parser.add_argument("--base-url", required=True)
    _add_token_file(submit_parser)
    submit_parser.set_defaults(func=cmd_submit)

    movers_parser = sub.add_parser("movers", help="What moved over one New York day (or the "
                                   "last hours), for every coin in the context: movers.json.")
    _add_now(movers_parser)
    window = movers_parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--day", type=_day_arg, help="The New York day, YYYY-MM-DD (refused "
                        "until it has ended). The run folder defaults to runs/<day>.")
    window.add_argument("--hours", type=int, help="The last N whole hours instead.")
    movers_parser.set_defaults(func=cmd_movers)

    checklist_parser = sub.add_parser(
        "checklist", help="The research checklist (Muse's own memory, outside the app): "
        "show it, update it from movers.json and postmortem-submit.json (the accepted "
        "post-mortems), or export the ACTIVE items for the morning.")
    checklist_parser.add_argument("action", choices=("show", "update", "export"))
    checklist_parser.add_argument("--state-dir", help="Owner-only state folder (default "
                                  f"{checklist.DEFAULT_STATE_DIR}).")
    checklist_parser.add_argument("--from", dest="inputs", action="append", default=[],
                                  help="update: a movers.json or a postmortem-submit.json "
                                  "(repeatable).")
    checklist_parser.add_argument("--lessons", help="update: the context.json whose "
                                  "lessons.recent_days records the movers' day (default: the "
                                  "run folder's context.json).")
    checklist_parser.add_argument("--out", help="export: where to write (default: the run "
                                  "folder's checklist-export.json).")
    _add_now(checklist_parser)
    checklist_parser.set_defaults(func=cmd_checklist)

    all_parser = sub.add_parser("all", help="context, market, levels, build, validate "
                                "(never submit).")
    _add_now(all_parser)
    all_parser.add_argument("--base-url")
    all_parser.add_argument("--offline", action="store_true")
    _add_token_file(all_parser)
    all_parser.add_argument("--news")
    all_parser.add_argument("--agent-id", required=True)
    all_parser.add_argument("--agent-version", required=True)
    all_parser.add_argument("--max-picks", type=int, default=None)
    _add_evidence(all_parser)
    _add_session(all_parser)
    _add_profile(all_parser)
    all_parser.set_defaults(func=cmd_all)

    # --run-dir is accepted after the command too (DAILY_PROCEDURE.md writes it there): the
    # subcommand's copy is suppressed when absent, so the value given before the command stays.
    for command_parser in sub.choices.values():
        command_parser.add_argument("--run-dir", default=argparse.SUPPRESS,
                                    help="One run's folder (default: runs/<NY date>).")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
