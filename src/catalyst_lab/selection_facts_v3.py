"""``COMPARATIVE_FACTS_V1``: the code-computed buckets of ``JEV_TOP_K_SELECTION_V3``'s one
comparative review (package jev-b2). Read-only: no order, no broker account, no Jev.

Jev judges; code measures (pinned TypeSafe skill: "Keep known rules, calculations, exact
lookups, and execution in code"). Every number reaches Jev as a worded bucket, never as
arithmetic to do. Per candidate:

* ``setup_type``: the pick's kind and its entry type (``BREAKOUT_ENTRY``: the entry trigger is
  above the price the agent saw; else ``RETEST_ENTRY``).
* ``catalyst``: one line, the first CATALYST claim's text, else the thesis, cut to 160
  characters at a word.
* ``checks``: stage 1's own result in words (coin-specific catalyst YES/NO/UNCLEAR at
  0.70/0.30; factual claims supported "k of n").
* ``trend_20d``: the last completed 1-hour close against the close 20 days earlier
  (STRONG_DOWN <= -10%, DOWN <= -2%, FLAT < 2%, UP < 10%, else STRONG_UP).
* ``volume_vs_30d``: the last 24 hours' volume against the mean 24-hour volume of the 30 days
  before them (at least 20 days of bars): LOW < 0.5x, BELOW_AVERAGE < 1x, ABOVE_AVERAGE < 2x,
  HIGH < 4x, else VERY_HIGH.
* ``stop_width``: ``CRYPTO_TRADE_PLAN_V1``'s stop (the lower of the research stop and the
  entry trigger minus 2 hourly ranges) in hourly ranges below the trigger: AT_PLAN_MINIMUM
  (exactly the rule's two), 2_TO_3 (under 3), 3_TO_5 (up to 5), else OVER_5. The hourly
  range is the mean high minus low of the completed 1-hour bars that ended in the 24 hours
  before (at least 20), as ``crypto_maintenance.hourly_range`` defines it.
* ``fees_in_r``: the assumed round trip (``pick_outcomes.FEE_ASSUMPTION``, 0.25% taker on both
  legs) at max entry over max entry minus that stop: UNDER_0.10R, 0.10R_TO_0.25R,
  0.25R_TO_0.50R, else 0.50R_OR_MORE.
* ``recent_results``: the coin's closed paper trades in this system in the last 30 days
  (engineering excluded) with their test R: count, how many ended above 0R, and the last one.

``market``: ``btc_regime_yesterday`` (the previous New York day's recorded ``MARKET_REGIME_V1``
tag), ``btc_last_4h`` (MARKET_REGIME_V1's 4-hour bucket on the last completed hours) and
``entry_pacing`` (``CRYPTO_ENTRY_PACING_V1``: OPEN or the rule that holds new entries now).

A failed read or too few bars gives ``UNKNOWN`` for that bucket (recorded with the reason in
the evidence); a comparison is never blocked by a missing fact. Every read is one public,
keyless bar request (``public_crypto_bars``) through the reader passed in.
"""

from datetime import timedelta
from decimal import Decimal, localcontext

from catalyst_lab import market_regime, regime_gate
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_measurement import managed_measurement
from catalyst_lab.market import NY
from catalyst_lab.pick_outcomes import TAKER_FEE_TIER1, parse_bars

D = Decimal
FACTS_VERSION = "COMPARATIVE_FACTS_V1"
UNKNOWN = "UNKNOWN"
HOUR = timedelta(hours=1)
TREND_DAYS, VOLUME_DAYS, VOLUME_MIN_DAYS = 20, 30, 20
RANGE_HOURS, RANGE_MIN_BARS = 24, 20
PLAN_RANGES = D(2)
RESULT_DAYS = 30
CATALYST_CHARS = 160
YES_AT, NO_AT = D("0.70"), D("0.30")
_PRECISION = 80


def _ratio(numerator, denominator):
    with localcontext() as context:
        context.prec = _PRECISION
        return numerator / denominator


def completed_hours(bars, now):
    """One-hour bars that had closed by ``now``, oldest first."""
    return [bar for bar in bars if bar.start + HOUR <= now]


def trend_bucket(bars, now):
    bars = completed_hours(bars, now)
    if not bars:
        return UNKNOWN, None
    last = bars[-1]
    anchor = [bar for bar in bars if bar.start <= last.start - timedelta(days=TREND_DAYS)]
    if not anchor or anchor[-1].start < last.start - timedelta(days=TREND_DAYS, hours=6):
        return UNKNOWN, None
    pct = (_ratio(last.close, anchor[-1].close) - 1) * 100
    if pct <= -10:
        label = "STRONG_DOWN: fell more than 10% over 20 days"
    elif pct <= -2:
        label = "DOWN: fell 2% to 10% over 20 days"
    elif pct < 2:
        label = "FLAT: within 2% of its price 20 days ago"
    elif pct < 10:
        label = "UP: rose 2% to 10% over 20 days"
    else:
        label = "STRONG_UP: rose more than 10% over 20 days"
    return label, pct


def volume_bucket(bars, now):
    bars = completed_hours(bars, now)
    if not bars:
        return UNKNOWN, None
    end = bars[-1].start + HOUR
    recent = [b for b in bars if b.start >= end - timedelta(hours=24)]
    prior = [b for b in bars if end - timedelta(days=VOLUME_DAYS + 1) <= b.start
             < end - timedelta(hours=24)]
    days = len({(b.start - (end - timedelta(hours=24))).days for b in prior})
    if len(recent) < RANGE_MIN_BARS or days < VOLUME_MIN_DAYS:
        return UNKNOWN, None
    mean_day = _ratio(sum((b.volume for b in prior), D(0)), D(len(prior))) * 24
    if mean_day <= 0:
        return UNKNOWN, None
    ratio = _ratio(sum((b.volume for b in recent), D(0)), mean_day)
    if ratio < D("0.5"):
        label = "LOW: under half its 30-day average"
    elif ratio < 1:
        label = "BELOW_AVERAGE: half to one times its 30-day average"
    elif ratio < 2:
        label = "ABOVE_AVERAGE: one to two times its 30-day average"
    elif ratio < 4:
        label = "HIGH: two to four times its 30-day average"
    else:
        label = "VERY_HIGH: four times its 30-day average or more"
    return label, ratio


def hourly_range(bars, now):
    """The mean high minus low of the completed 1-hour bars that ended in the 24 hours before
    ``now`` (at least 20), else None."""
    hours = [b for b in completed_hours(bars, now)
             if now - timedelta(hours=RANGE_HOURS) < b.start + HOUR <= now]
    if len(hours) < RANGE_MIN_BARS:
        return None
    return _ratio(sum((b.high - b.low for b in hours), D(0)), D(len(hours)))


def plan_stop(levels, hr):
    """``CRYPTO_TRADE_PLAN_V1``'s stop before grid rounding; the research stop without HR."""
    stop = D(str(levels["stop"]))
    if hr is None:
        return stop
    return min(stop, D(str(levels["entry_trigger"])) - PLAN_RANGES * hr)


def stop_bucket(levels, hr):
    if hr is None or hr <= 0:
        return UNKNOWN, None
    width = _ratio(D(str(levels["entry_trigger"])) - plan_stop(levels, hr), hr)
    if width <= PLAN_RANGES:
        label = "AT_PLAN_MINIMUM: 2 hourly ranges below the entry"
    elif width < 3:
        label = "2_TO_3: 2 to 3 hourly ranges below the entry"
    elif width <= 5:
        label = "3_TO_5: 3 to 5 hourly ranges below the entry"
    else:
        label = "OVER_5: more than 5 hourly ranges below the entry"
    return label, width


def fees_bucket(levels, hr):
    max_entry = D(str(levels["max_entry_price"]))
    risk = max_entry - plan_stop(levels, hr)
    if risk <= 0:
        return UNKNOWN, None
    fees = _ratio(2 * TAKER_FEE_TIER1 * max_entry, risk)
    if fees < D("0.10"):
        label = "UNDER_0.10R"
    elif fees < D("0.25"):
        label = "0.10R_TO_0.25R"
    elif fees < D("0.50"):
        label = "0.25R_TO_0.50R"
    else:
        label = "0.50R_OR_MORE"
    return label + ("" if hr is not None else " (on the research stop; hourly range unknown)"), (
        fees)


def setup_type(state):
    kind = {"NEWS": "news catalyst", "CHART": "chart setup",
            "BOTH": "news catalyst with a chart setup"}.get(state.get("kind"), UNKNOWN)
    try:
        breakout = D(str(state["levels"]["entry_trigger"])) > D(str(state["agent_current_price"]))
    except (KeyError, TypeError, ArithmeticError, ValueError):
        return kind
    entry = ("BREAKOUT_ENTRY: the entry trigger is above the price when picked" if breakout
             else "RETEST_ENTRY: the entry trigger is at or below the price when picked")
    return f"{kind}; {entry}"


def one_line(text, limit=CATALYST_CHARS):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " ..."


def catalyst_line(state):
    for claim in ((state.get("rationale") or {}).get("claims") or []):
        if isinstance(claim, dict) and claim.get("kind") == "CATALYST" and claim.get("text"):
            return one_line(claim["text"])
    return one_line(state.get("thesis"))


def checks_words(checks):
    """Stage 1's result in words for the comparative state (code thresholds 0.70/0.30)."""
    p = checks.catalyst_p
    catalyst = UNKNOWN if p is None else (
        "YES" if p >= YES_AT else "NO" if p <= NO_AT else "UNCLEAR")
    supported = checks.factual_claims - checks.unsupported_claims
    claims = ("no factual claims" if not checks.factual_claims
              else f"{supported} of {checks.factual_claims} factual claims supported")
    return {"coin_specific_catalyst": catalyst, "claims": claims}


def results_words(rows):
    """``rows``: the coin's closed trades, newest first, as ``(closed_at, test_r)``."""
    if not rows:
        return "NONE: no closed trades of this coin in 30 days"
    known = [r for _, r in rows if r is not None]
    above = sum(1 for r in known if r > 0)
    last = rows[0][1]
    last_words = ("R unknown" if last is None
                  else "above 0R" if last > 0 else "at or below 0R")
    return (f"{len(rows)} closed in 30 days, {above} of {len(known)} with known R above 0R; "
            f"the last ended {last_words}")


class ComparisonFacts:
    """The runtime's fact reader for one V3 comparison (read-only)."""

    def __init__(self, repository, bars_reader, *, pacing=None):
        self.repo, self.bars_reader, self.pacing = repository, bars_reader, pacing

    def _hours(self, symbol, now, days):
        end = now.replace(minute=0, second=0, microsecond=0)
        rows = self.bars_reader.bars(symbol, end - timedelta(days=days) - HOUR, end, "1Hour")
        return parse_bars(rows)

    def market(self, now):
        evidence, market = {}, {}
        day = (now.astimezone(NY).date() - timedelta(days=1))
        with self.repo.connect() as conn:
            regime = market_regime.recorded_regimes(conn, [day]).get(day.isoformat())
            if self.pacing is None:
                pacing = UNKNOWN
            else:
                blocked = regime_gate.block(self.pacing, conn, now)
                pacing = ("OPEN: new entries are allowed now" if blocked is None
                          else f"{blocked[0]}: new entries wait now")
        market["btc_regime_yesterday"] = regime["tag"] if regime else UNKNOWN
        try:
            bars = completed_hours(self._hours(market_regime.BTC, now, 1), now)
            last, first = bars[-1], bars[-4]
            pct = (_ratio(last.close, first.open) - 1) * 100
            market["btc_last_4h"] = market_regime.bucket(pct, market_regime.FOUR_HOUR_EDGES)
            evidence["btc_last_4h_pct"] = str(pct.quantize(D("0.0001")))
        except Exception as exc:  # noqa: BLE001 -- a missing fact is UNKNOWN, recorded.
            market["btc_last_4h"] = UNKNOWN
            evidence["btc_last_4h_error"] = type(exc).__name__
        market["entry_pacing"] = pacing
        evidence["regime_day"] = day.isoformat()
        return market, evidence

    def _results(self, symbol, now):
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT s.setup_id, s.record_json,
                coalesce((t.body->>'closed_at')::timestamptz,t.recorded_at) AS closed_at
                FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
                WHERE s.symbol=%s AND t.body->>'state'='CLOSED'
                AND coalesce((t.body->>'closed_at')::timestamptz,t.recorded_at)>=%s
                AND EXISTS(SELECT 1 FROM lab.managed_fills f
                           WHERE f.setup_id=s.setup_id AND f.side='buy')
                ORDER BY 3 DESC LIMIT 20""",
                (symbol, now - timedelta(days=RESULT_DAYS))).fetchall()
        result = []
        for row in rows:
            if is_engineering(row["record_json"]):
                continue
            r = managed_measurement(self.repo, row["setup_id"]).get("test_r")
            result.append((row["closed_at"], None if r is None else D(str(r))))
        return result

    def candidate(self, packet, checks, now):
        """``(candidate, evidence)`` for one survivor; ``checks`` is its stage-1 outcome."""
        state, levels = packet["state"], packet["levels"]
        evidence = {}
        candidate = {"symbol": packet["symbol"], "setup_type": setup_type(state),
                     "catalyst": catalyst_line(state), "checks": checks_words(checks)}
        hr = None
        try:
            bars = self._hours(packet["symbol"], now, VOLUME_DAYS + 1)
            trend, pct = trend_bucket(bars, now)
            volume, ratio = volume_bucket(bars, now)
            hr = hourly_range(bars, now)
            evidence.update({"bars": len(bars), "trend_pct": None if pct is None else str(
                pct.quantize(D("0.0001"))), "volume_ratio": None if ratio is None else str(
                ratio.quantize(D("0.0001"))), "hourly_range": None if hr is None else str(hr)})
        except Exception as exc:  # noqa: BLE001 -- a missing fact is UNKNOWN, recorded.
            trend = volume = UNKNOWN
            evidence["bars_error"] = type(exc).__name__
        candidate["trend_20d"] = trend
        candidate["volume_vs_30d"] = volume
        candidate["stop_width"], width = stop_bucket(levels, hr)
        candidate["fees_in_r"], fees = fees_bucket(levels, hr)
        evidence["stop_width_ranges"] = None if width is None else str(width.quantize(
            D("0.0001")))
        evidence["fees_r"] = None if fees is None else str(fees.quantize(D("0.0001")))
        try:
            candidate["recent_results"] = results_words(self._results(packet["symbol"], now))
        except Exception as exc:  # noqa: BLE001
            candidate["recent_results"] = UNKNOWN
            evidence["results_error"] = type(exc).__name__
        return candidate, evidence

