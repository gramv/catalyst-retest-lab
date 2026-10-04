"""``HISTORY_TEST_V1``: the history tester on the shared strategy core (package strategy-c2, plan
phase C step 2; docs/TRADING-QUALITY-PLAN.md section 5, the promotion ladder's first rung;
guide docs/STRATEGY-PLUGINS.md).

**Offline measurement. No orders, no broker, no Jev, no ledger.** It reads keyless public bars
(``history_bars``: Alpaca's public crypto bars or Coinbase's public candles, cached on disk),
runs a mechanical strategy's own ``signals`` and ``simulate`` -- the plug-in's functions on
``strategies.core``, exactly what the strategy shadow calls -- over a date range for every
declared history variant, and writes a report folder (``REPORT.md``, ``results.json``,
``trials.jsonl``) plus one line per variant in a cumulative trials ledger.

What a run measures, per strategy:

* **Every declared variant** (``Strategy.history_variants``, fixed before the run; every one is a
  trial in the ledger) over the whole range: trades, gross / fee / slippage / net R per trade,
  a day-bootstrap 90% interval of net R per trade, win rate, max drawdown in R, daily Sharpe.
* **Walk-forward**: fixed rolling windows (default 60 days in-sample, 30 out-of-sample). In each
  window the variant with the best in-sample mean net R per trade (at least ``min_is_trades``
  trades whose exits were known by the window's end; else the registered rule) is traded in
  the next 30 days. The out-of-sample trades, concatenated, are the honest estimate.
* **Overfitting**: the deflated Sharpe ratio (Bailey and Lopez de Prado) with the number of
  distinct variants ever tried for the strategy (the trials ledger), and the probability of
  backtest overfitting by CSCV across the variants (``history_stats``).
* **Breakdowns** of the registered rule and of the walk-forward trades: by window, by month, by
  ``MARKET_REGIME_V1`` tag of the day before the fill (known at entry; computed offline from the
  same hourly bars by ``market_regime.day_regime_from``) and its components, and by coin.
* **Account simulation** of the same trades under ``JEV_MANAGED_RISK_V4``'s crypto sizing
  (``account_risk.slice_size``: 10% equity slices capped at 0.5% risk per trade, stops >= 2%),
  the 2% crypto open-risk cluster cap, three open trades per sector, one open trade per coin, the
  2% daily soft limit on realized P&L (``account_risk.soft_limit_reached``) and
  ``CRYPTO_ENTRY_PACING_V1``'s rate (2 entries per 30 minutes) and median-coin drop gate
  (-2% over the signal hour, at least 3 coins). A pacing or drop wait skips the signal (a
  marketable breakout has no later entry). Not modelled: the 3% hard halt's flatten, the macro
  calendar window, Jev, the live trigger's quote checks.

Costs: the fee model's rate on both legs (default Alpaca tier-1 taker, 0.25%) and
``core.slippage_estimate`` (``HALF_SPREAD_PLUS_VOLATILITY_V1``), the model the strategy shadow
records, charged on both legs. Fills: the open of the first 1-minute bar within 15 minutes of
the signal (``core.simulate_marketable``), exits on 1-minute bars (``core.walk_to_exit``).

``promotion_check`` reads a history report and a shadow scorecard cell and says which rung of
the ladder the strategy meets. Record-only text: nothing promotes itself.

``--source synthetic`` (package oss-packaging) runs on ``sample_market``'s generated sample bars:
no network, for tutorials and tests. Such a report is labelled ``SYNTHETIC``, its promotion
check is ``NONE`` whatever the figures say (``SYNTHETIC_DATA_IS_NOT_EVIDENCE``), its trials go to
a separate ledger (``TRIALS-synthetic.jsonl``) and ``strategy_paper.promote`` refuses it.
The default report and cache folders can be moved with ``CATALYST_HISTORY_TEST_OUT`` and
``CATALYST_HISTORY_CACHE_DIR`` (the local Docker stack sets both).
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

from catalyst_lab import account_risk, history_stats, market_regime, regime_gate, strategies
from catalyst_lab.history_bars import (
    DEFAULT_CACHE_DIR,
    SOURCE_LABELS,
    SOURCES,
    SYNTHETIC,
    BarCache,
)
from catalyst_lab.managed_classification import ALPACA_CRYPTO_SECTOR_OF
from catalyst_lab.market import NY
from catalyst_lab.pick_outcomes import TAKER_FEE_TIER1
from catalyst_lab.repository import json_value
from catalyst_lab.strategies import core
from catalyst_lab.strategy_shadow import ready_at

D = Decimal
VERSION = "HISTORY_TEST_V1"
LABEL = ("HISTORY TEST: simulated on public historical bars with assumed fees and slippage; "
         "no order was placed. Not a fill, not a forecast.")
DEFAULT_OUT_ROOT = Path.home() / ".local/share/catalyst-handoff/history-tests"
OUT_ENV, CACHE_ENV = "CATALYST_HISTORY_TEST_OUT", "CATALYST_HISTORY_CACHE_DIR"
SYNTHETIC_LABEL = ("SYNTHETIC SAMPLE BARS: a tutorial run on generated bars "
                   "(SYNTHETIC_SAMPLE_BARS_V1); not market data and never evidence for a "
                   "strategy. No order was placed.")
SYNTHETIC_NOT_EVIDENCE = "SYNTHETIC_DATA_IS_NOT_EVIDENCE"
SYNTHETIC_TRIALS = "TRIALS-synthetic.jsonl"
DEFAULT_DAYS = 365
DEFAULT_IS_DAYS, DEFAULT_OOS_DAYS = 60, 30
MIN_IS_TRADES = 10
PBO_MIN_VARIANTS = 4
PBO_BLOCKS = 16
BOOTSTRAP_RESAMPLES = 5000
HISTORY_PAD = timedelta(days=120)  # MARKET_REGIME_V1's 110 days of Bitcoin closes, and more.
TRADED = frozenset({core.STOP, core.TARGET, core.HOLD_EXIT})
FOUR = D("0.0001")

# Fee models (both legs). Alpaca: the tier-1 schedule the shadow assumes. Coinbase Advanced
# (US), the schedule announced 2026-09-16 as recorded in the 2026-09-29 owner decision notes
# (30-day volume tiers); not read from any account -- verify before relying on it.
FEE_MODELS = {
    "alpaca-taker": (TAKER_FEE_TIER1, "Alpaca crypto tier-1 taker 0.25% per leg"),
    "alpaca-maker": (D("0.0015"), "Alpaca crypto tier-1 maker 0.15% per leg"),
    "coinbase-taker-under-10k": (D("0.009"), "Coinbase Advanced taker, < $10k/30d, 0.90%"),
    "coinbase-maker-under-10k": (D("0.005"), "Coinbase Advanced maker, < $10k/30d, 0.50%"),
    "coinbase-taker-10k-50k": (D("0.006"), "Coinbase Advanced taker, $10k-50k/30d, 0.60%"),
    "coinbase-taker-50k-100k": (D("0.004"), "Coinbase Advanced taker, $50k-100k/30d, 0.40%"),
    "coinbase-taker-100k-1m": (D("0.0025"), "Coinbase Advanced taker, $100k-1M/30d, 0.25%"),
}

# JEV_MANAGED_RISK_V4 (migration 026) and its CRYPTO terms, as the account simulation's policy.
# tests/test_history_test.py pins these against the migration's own INSERT statements.
V4_POLICY = account_risk.AccountRiskPolicy(
    policy_id=account_risk.MANAGED_RISK_V4_POLICY_ID, engine="MANAGED", risk_pct=D("0.005"),
    account_cap_pct=D("0.05"),
    market_caps={"CRYPTO": D("0.02"), "FOREX": D("0"), "US_STOCKS": D("0.03")},
    max_per_sector=2, max_per_theme=1, sector_limited_markets=("US_STOCKS",),
    leverage_allowed=True, intraday_buying_power_multiple=D(2), capacity_cooldown_seconds=60,
    fixed_exit_arm_pct=30, owner_ruling_ref="TRADING-QUALITY-PLAN A1 (history-test mirror)",
    market_terms={"CRYPTO": account_risk.MarketTerms(
        policy_id=account_risk.MANAGED_RISK_V4_POLICY_ID, market="CRYPTO",
        sizing_method=account_risk.EQUITY_SLICE_SIZING, notional_pct=D("0.10"),
        max_per_theme=3, min_stop_fraction=D("0.02"),
        owner_ruling_ref="TRADING-QUALITY-PLAN A1 (history-test mirror)")},
    daily_limits=account_risk.DailyLimits(
        policy_id=account_risk.MANAGED_RISK_V4_POLICY_ID, hard_loss_pct=D("0.03"),
        hard_action=account_risk.HARD_LOSS_ACTION, soft_loss_pct=D("0.02"),
        soft_action=account_risk.SOFT_LOSS_ACTION,
        owner_ruling_ref="TRADING-QUALITY-PLAN A1 (history-test mirror)"),
)
SIZE_INCREMENT = D("0.000000001")
DEFAULT_EQUITY = D(10000)

# Account-simulation skip reasons.
SKIP_COIN_OPEN = "COIN_ALREADY_OPEN"
SKIP_SECTOR = "SECTOR_LIMIT"
SKIP_CLUSTER = "MARKET_RISK_CAP"
SKIP_SOFT = account_risk.SOFT_LIMIT_REASON
SKIP_ZERO = "ZERO_SIZE"


def _q(value, step=FOUR):
    if value is None:
        return None
    rounded = D(value).quantize(step, rounding=ROUND_HALF_EVEN)
    return abs(rounded) if rounded == 0 else rounded


def _f(value, digits=6):
    return None if value is None else round(float(value), digits)


def _mean(values):
    if not values:
        return None
    with localcontext() as context:
        context.prec = 40
        return sum(values, D(0)) / len(values)


def universe_for(name):
    """``alpaca`` (the 33 Alpaca USD pairs, ``ALPACA_CRYPTO_SECTORS_V1``), ``coinbase`` (those
    with a Coinbase USD product, ``coinbase_feed.COINBASE_USD_PRODUCTS``) or a comma list."""
    if name == "alpaca":
        return sorted(ALPACA_CRYPTO_SECTOR_OF)
    if name == "coinbase":
        from catalyst_lab.coinbase_feed import COINBASE_USD_PRODUCTS

        return sorted(p.replace("-USD", "/USD") for p in COINBASE_USD_PRODUCTS)
    symbols = sorted({s.strip().upper() for s in name.split(",") if s.strip()})
    if not symbols or any(not s.endswith("/USD") for s in symbols):
        raise ValueError("UNIVERSE_INVALID")
    return symbols


def sector_of(symbol):
    return ALPACA_CRYPTO_SECTOR_OF.get(symbol, "CRYPTO_OTHER")


# --- Trades from a strategy's own signals and simulation ------------------------------------------


def variants_of(strategy):
    """The declared history variants; a strategy without any is its registered rule only."""
    return list(strategy.history_variants) or [{"variant_id": "REGISTERED"}]


def is_registered(strategy, variant):
    """Whether ``variant`` is the registered rule (its parameters reproduce the strategy)."""
    if variant["variant_id"] == "REGISTERED":
        return True
    module = sys.modules.get(strategy.signals.__module__)
    return getattr(module, "DEFAULT_VARIANT_ID", None) == variant["variant_id"]


def _params(variant):
    params = {k: v for k, v in variant.items() if k != "variant_id"}
    return params or None


def trade_from(variant_id, proposal, result, *, symbol, slippage_on):
    """One simulated outcome as a trade row (Decimals), or None when it did not trade."""
    if result.get("outcome") not in TRADED or result.get("net_r") is None:
        return None
    gross, net = D(str(result["gross_r"])), D(str(result["net_r"]))
    slip = D(str(result["slippage_r"])) if result.get("slippage_r") is not None else D(0)
    if slippage_on and result.get("slippage_r") is None:
        return None  # No slippage estimate: refused rather than counted free (fail-closed).
    plan = result["plan"]
    return {
        "variant_id": variant_id, "symbol": symbol, "sector": sector_of(symbol),
        "signal_at": datetime.fromisoformat(proposal["signal_at"]),
        "fill_at": result["fill_at"], "exit_at": result["exit_at"],
        "outcome": result["outcome"], "entry": result["fill_price"],
        "stop": plan["stop"], "target": plan["target"], "exit": result["exit_price"],
        "gross_r": gross, "fee_r": gross - net, "slippage_r": slip if slippage_on else D(0),
        "r": net - slip if slippage_on else net,
    }


class Simulator:
    """Runs a strategy's ``simulate`` once per (coin, signal, inputs), on cached minute bars."""

    def __init__(self, cache, strategy, *, fee_rate, log):
        self.cache, self.strategy, self.fee_rate, self.log = cache, strategy, fee_rate, log
        self.memo, self.errors = {}, Counter()

    def __call__(self, symbol, proposal):
        key = (symbol, proposal["signal_at"], proposal.get("hourly_range"),
               json.dumps(proposal.get("slippage"), sort_keys=True))
        if key not in self.memo:
            at = datetime.fromisoformat(proposal["signal_at"])
            try:
                bars = self.cache.minutes(symbol, at, ready_at(at))
                self.memo[key] = self.strategy.simulate(proposal, bars, fee_rate=self.fee_rate)
            except Exception as exc:  # noqa: BLE001 -- counted, never silently traded.
                self.errors[type(exc).__name__ + ":" + str(exc)[:60]] += 1
                self.memo[key] = {"outcome": "BARS_UNAVAILABLE", "fill_price": None}
        return self.memo[key]


def strategy_trades(strategy, hour_bars, simulator, *, start, end, slippage_on, log):
    """``{variant_id: {"trades": [...], "outcomes": Counter, "signals": n}}`` over
    ``(start, end]`` signal times."""
    out = {}
    for variant in variants_of(strategy):
        params = _params(variant)
        trades, outcomes, signals = [], Counter(), 0
        for symbol, bars in sorted(hour_bars.items()):
            context = {"symbol": symbol, "since": start, "until": end}
            if params is not None:
                context["parameters"] = params
            for proposal in strategy.signals(bars, context):
                signals += 1
                result = simulator(symbol, proposal)
                outcomes[result.get("outcome")] += 1
                trade = trade_from(variant["variant_id"], proposal, result, symbol=symbol,
                                   slippage_on=slippage_on)
                if trade is not None:
                    trades.append(trade)
                elif result.get("outcome") in TRADED:
                    outcomes["NO_SLIPPAGE_ESTIMATE"] += 1
        trades.sort(key=lambda t: (t["fill_at"], t["symbol"]))
        out[variant["variant_id"]] = {"variant": dict(variant), "trades": trades,
                                      "outcomes": dict(sorted(outcomes.items())),
                                      "signals": signals}
        log(f"  {variant['variant_id']}: {signals} signals, {len(trades)} trades")
    return out


# --- Summaries ----------------------------------------------------------------------------------


def all_days(start, end):
    days, at = [], start.date()
    while at < end.date() or (at == end.date() and end.time() != datetime.min.time()):
        days.append(at)
        at += timedelta(days=1)
    return days


def daily_series(trades, days):
    sums = defaultdict(lambda: D(0))
    for t in trades:
        sums[t["fill_at"].date()] += t["r"]
    return [sums[d] for d in days]


def summarize(trades, *, days=None, resamples=BOOTSTRAP_RESAMPLES):
    """Net R per trade (after fees and slippage), its day-bootstrap 90% interval, costs, win
    rate, drawdown in R and (with ``days``) the daily Sharpe ratio."""
    r = [t["r"] for t in trades]
    out = {"trades": len(trades), "mean_net_r": _q(_mean(r)),
           "mean_gross_r": _q(_mean([t["gross_r"] for t in trades])),
           "mean_fee_r": _q(_mean([t["fee_r"] for t in trades])),
           "mean_slippage_r": _q(_mean([t["slippage_r"] for t in trades])),
           "sum_net_r": _q(sum(r, D(0))) if r else None,
           "win_rate": _q(D(sum(1 for v in r if v > 0)) / len(r)) if r else None,
           "outcomes": dict(sorted(Counter(t["outcome"] for t in trades).items())),
           "ci90_mean_net_r": None, "max_drawdown_r": None, "cost_share_of_mean_abs_gross": None}
    if r:
        ci = history_stats.day_bootstrap_ci([(t["fill_at"].date(), t["r"]) for t in trades],
                                            resamples=resamples)
        out["ci90_mean_net_r"] = None if ci is None else [_f(ci[0], 4), _f(ci[1], 4)]
        ordered = sorted(trades, key=lambda t: (t["exit_at"], t["symbol"]))
        out["max_drawdown_r"] = _f(history_stats.max_drawdown([t["r"] for t in ordered]), 4)
        abs_gross = _mean([abs(t["gross_r"]) for t in trades])
        if abs_gross:
            out["cost_share_of_mean_abs_gross"] = _q(
                (_mean([t["fee_r"] + t["slippage_r"] for t in trades])) / abs_gross)
    if days is not None:
        sr = history_stats.sharpe(daily_series(trades, days))
        out["daily_sharpe"] = _f(sr)
        out["annualized_sharpe"] = None if sr is None else _f(sr * 365 ** 0.5, 4)
        out["days"] = len(days)
    return out


def breakdown(trades, key, *, resamples=1000):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {str(k): {"trades": len(v), "mean_net_r": _q(_mean([t["r"] for t in v])),
                     "sum_net_r": _q(sum((t["r"] for t in v), D(0))),
                     "mean_gross_r": _q(_mean([t["gross_r"] for t in v])),
                     "win_rate": _q(D(sum(1 for t in v if t["r"] > 0)) / len(v))}
            for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


# --- Walk-forward -------------------------------------------------------------------------------


def walk_forward(variant_trades, *, start, end, is_days, oos_days, default_id,
                 min_is_trades=MIN_IS_TRADES):
    """Rolling selection: per window, the best in-sample mean net R per trade among variants
    with at least ``min_is_trades`` in-sample trades closed by the window's end (else the
    registered rule), traded out of sample."""
    folds, oos = [], []
    order = list(variant_trades)
    for n, (i0, i1, o0, o1) in enumerate(history_stats.walk_forward_folds(
            start, end, is_days=is_days, oos_days=oos_days)):
        scores = {}
        for vid in order:
            sample = [t["r"] for t in variant_trades[vid] if i0 <= t["fill_at"]
                      and t["exit_at"] is not None and t["exit_at"] <= i1]
            scores[vid] = (len(sample), _mean(sample))
        eligible = [v for v in order if scores[v][0] >= min_is_trades]
        chosen = (max(eligible, key=lambda v: (scores[v][1], -order.index(v))) if eligible
                  else default_id)
        out = [t for t in variant_trades[chosen] if o0 <= t["fill_at"] < o1]
        default_out = [t for t in variant_trades[default_id] if o0 <= t["fill_at"] < o1]
        oos.extend(out)
        folds.append({
            "fold": n + 1, "in_sample": [i0.date(), i1.date()],
            "out_of_sample": [o0.date(), o1.date()], "selected": chosen,
            "selection": "BEST_IN_SAMPLE" if eligible else "FALLBACK_REGISTERED_RULE",
            "in_sample_trades": scores[chosen][0],
            "in_sample_mean_net_r": _q(scores[chosen][1]),
            "out_of_sample_trades": len(out),
            "out_of_sample_mean_net_r": _q(_mean([t["r"] for t in out])),
            "out_of_sample_sum_net_r": _q(sum((t["r"] for t in out), D(0))),
            "registered_rule_oos_trades": len(default_out),
            "registered_rule_oos_mean_net_r": _q(_mean([t["r"] for t in default_out])),
        })
    return folds, oos


# --- Account simulation (JEV_MANAGED_RISK_V4 sizing, cluster cap, pacing) -------------------------


def hour_medians(hour_bars):
    """``{hour start: (median 1-hour return fraction, coins)}`` across the universe."""
    by_hour = defaultdict(list)
    for bars in hour_bars.values():
        for b in bars:
            with localcontext() as context:
                context.prec = 40
                by_hour[b.start].append((b.close - b.open) / b.open)
    return {h: (regime_gate.median(v), len(v)) for h, v in by_hour.items()}


def simulate_account(trades, medians, *, equity=DEFAULT_EQUITY, policy=V4_POLICY):
    """The trades in fill order through the account's rules; realized-equity curve."""
    terms = policy.terms("CRYPTO")
    cap = policy.market_cap("CRYPTO")
    open_, entries, taken = [], [], []
    skipped = Counter()
    binding = Counter()
    cash = equity
    curve = [(None, equity)]
    day_pnl, day_start = {}, {}
    fees = slippage = D(0)

    def realize(until):
        nonlocal equity, cash, fees, slippage
        for pos in sorted([p for p in open_ if p["exit_at"] <= until],
                          key=lambda p: (p["exit_at"], p["symbol"])):
            open_.remove(pos)
            t, qty = pos["trade"], pos["qty"]
            with localcontext() as context:
                context.prec = 40
                risk = t["entry"] - t["stop"]
                pnl = qty * risk * t["r"]
                fees += qty * risk * t["fee_r"]
                slippage += qty * risk * t["slippage_r"]
            equity += pnl
            cash += qty * t["entry"] + pnl
            day = pos["exit_at"].astimezone(NY).date()
            day_pnl[day] = day_pnl.get(day, D(0)) + pnl
            curve.append((pos["exit_at"], equity))

    for t in sorted(trades, key=lambda x: (x["fill_at"], x["symbol"])):
        now = t["fill_at"]
        realize(now)
        day = now.astimezone(NY).date()
        day_start.setdefault(day, equity)
        if account_risk.soft_limit_reached(policy, day_pnl.get(day, D(0)), day_start[day]):
            skipped[SKIP_SOFT] += 1
            continue
        if sum(1 for e in entries if now - regime_gate.RATE_WINDOW < e <= now) >= (
                regime_gate.MAX_ENTRIES):
            skipped[regime_gate.RATE_LIMIT] += 1
            continue
        median, coins = medians.get(t["signal_at"] - core.HOUR, (None, 0))
        if median is None or coins < regime_gate.MIN_COINS:
            skipped[regime_gate.BREADTH_UNAVAILABLE] += 1
            continue
        if median <= regime_gate.DROP_THRESHOLD:
            skipped[regime_gate.MARKET_DROP] += 1
            continue
        if any(p["symbol"] == t["symbol"] for p in open_):
            skipped[SKIP_COIN_OPEN] += 1
            continue
        if sum(1 for p in open_ if p["trade"]["sector"] == t["sector"]) >= terms.max_per_theme:
            skipped[SKIP_SECTOR] += 1
            continue
        try:
            size = account_risk.slice_size(policy, terms, equity=equity, max_entry=t["entry"],
                                           stop=t["stop"], available=cash,
                                           increment=SIZE_INCREMENT)
        except ValueError as exc:
            skipped[str(exc)] += 1
            continue
        if size.qty <= 0:
            skipped[SKIP_ZERO] += 1
            continue
        open_risk = sum((p["planned"] for p in open_), D(0))
        if open_risk + size.planned_risk > cap * equity:
            skipped[SKIP_CLUSTER] += 1
            continue
        binding[size.binding] += 1
        cash -= size.notional
        entries.append(now)
        open_.append({"symbol": t["symbol"], "exit_at": t["exit_at"], "qty": size.qty,
                      "planned": size.planned_risk, "trade": t})
        taken.append({**t, "qty": size.qty, "planned_risk_pct": size.planned_risk / equity})
    realize(datetime.max.replace(tzinfo=UTC))
    peak, worst = curve[0][1], D(0)
    for _, value in curve:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    start_equity = curve[0][1]
    return {
        "policy": policy.policy_id, "start_equity": start_equity,
        "end_equity": _q(equity, D("0.01")),
        "return_pct": _q((equity - start_equity) / start_equity * 100),
        "max_drawdown_pct": _q(worst * 100), "trades_taken": len(taken),
        "trades_skipped": dict(sorted(skipped.items())), "binding_constraint": dict(binding),
        "mean_planned_risk_pct": _q(_mean([t["planned_risk_pct"] for t in taken]) * 100
                                    if taken else None),
        "fees_usd": _q(fees, D("0.01")), "slippage_usd": _q(slippage, D("0.01")),
        "mean_net_r_taken": _q(_mean([t["r"] for t in taken])),
        "not_modelled": ["3% hard daily halt flatten", "macro calendar window",
                         "unrealized P&L in the soft limit", "broker increments and minimums"],
    }


# --- Trials ledger --------------------------------------------------------------------------------


def trial_key(strategy_id, variant):
    return hashlib.sha256(json.dumps([strategy_id, variant], sort_keys=True).encode()).hexdigest()


def read_trials(path):
    if path is None or not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def append_trials(path, rows):
    if path is None:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=json_value, sort_keys=True) + "\n")


# --- Regime tags ----------------------------------------------------------------------------------


def regime_tags(hour_bars, universe, days):
    """``{New York day: MARKET_REGIME_V1 day record}`` for ``days`` (pure)."""
    btc = hour_bars.get(market_regime.BTC)
    if not btc:
        return {}
    btc_closes = market_regime.daily_closes(btc)
    coin_closes = {s: market_regime.daily_closes(b) for s, b in hour_bars.items()}
    indexed = market_regime.hour_index(hour_bars)
    return {day: market_regime.day_regime_from(day, btc_closes, coin_closes, indexed, universe)
            for day in days}


def _regime_key(tags, component):
    def key(t):
        prior = t["fill_at"].astimezone(NY).date() - timedelta(days=1)
        record = tags.get(prior)
        if record is None:
            return market_regime.UNKNOWN
        return record["tag"] if component == "tag" else record[component]["label"]
    return key


def _fold_label(folds):
    fold_of = {}
    for f in folds:
        for d in all_days(datetime.combine(f["out_of_sample"][0], datetime.min.time(), UTC),
                          datetime.combine(f["out_of_sample"][1], datetime.min.time(), UTC)):
            fold_of[d] = f"fold {f['fold']:02d} {f['out_of_sample'][0]}"

    def key(trade):
        return fold_of.get(trade["fill_at"].date(), "in-sample only (before the first fold)")
    return key


def breakdowns(trades, folds, tags):
    """Per-window, per-month, per-regime (the prior New York day's MARKET_REGIME_V1 tag and
    components), per-coin and per-outcome groups."""
    return {
        "by_period": breakdown(trades, _fold_label(folds)),
        "by_month": breakdown(trades, lambda t: t["fill_at"].strftime("%Y-%m")),
        "by_regime_prior_day_tag": breakdown(trades, _regime_key(tags, "tag")),
        "by_btc_trend": breakdown(trades, _regime_key(tags, "btc_trend")),
        "by_btc_volatility": breakdown(trades, _regime_key(tags, "btc_volatility")),
        "by_alt_breadth": breakdown(trades, _regime_key(tags, "alt_breadth")),
        "by_selloff": breakdown(trades, _regime_key(tags, "selloff")),
        "by_coin": breakdown(trades, lambda t: t["symbol"]),
        "by_outcome": breakdown(trades, lambda t: t["outcome"]),
    }


# --- The run --------------------------------------------------------------------------------------


def run(*, strategy_ids, universe, start, end, source, fee_model, slippage_on=True,
        equity=DEFAULT_EQUITY, is_days=DEFAULT_IS_DAYS, oos_days=DEFAULT_OOS_DAYS,
        cache, trials_path=None, min_is_trades=MIN_IS_TRADES, resamples=BOOTSTRAP_RESAMPLES,
        log=lambda message: None, now=None):
    """One history test; returns the report (plain data). Appends the variants to the trials
    ledger at ``trials_path`` (None: no ledger)."""
    now = now or datetime.now(UTC)
    fee_rate, fee_text = FEE_MODELS[fee_model]
    run_id = f"{VERSION}:{now.strftime('%Y%m%dT%H%M%SZ')}"
    fetch_symbols = sorted(set(universe) | {market_regime.BTC})
    hour_bars, coin_errors = {}, {}
    log(f"hourly bars: {len(fetch_symbols)} coins from {(start - HISTORY_PAD).date()}")
    for symbol in fetch_symbols:
        try:
            hour_bars[symbol] = cache.hourly(symbol, start - HISTORY_PAD, end)
        except Exception as exc:  # noqa: BLE001 -- one coin's failure is reported, not fatal.
            coin_errors[symbol] = f"{type(exc).__name__}:{exc}"[:120]
    traded_bars = {s: b for s, b in hour_bars.items() if s in universe and b}
    days = all_days(start, end)
    ny_days = sorted({d - timedelta(days=1) for d in days} | set(days))
    log("market regime tags")
    tags = regime_tags(hour_bars, [s for s in universe if s in hour_bars], ny_days)
    medians = hour_medians(traded_bars)
    prior_trials = read_trials(trials_path)
    report = {
        "version": VERSION, "label": SYNTHETIC_LABEL if source == SYNTHETIC else LABEL,
        "run_id": run_id, "generated_at": now,
        "inputs": {
            "strategies": list(strategy_ids), "universe": list(universe),
            "start": start, "end": end, "source": SOURCE_LABELS[source],
            "fee_model": fee_model, "fee_rate_per_leg": fee_rate, "fee_basis": fee_text,
            "slippage": core.SLIPPAGE_DESCRIPTION if slippage_on else "OFF",
            "slippage_model": core.SLIPPAGE_MODEL if slippage_on else None,
            "equity": equity, "walk_forward": {"in_sample_days": is_days,
                                               "out_of_sample_days": oos_days,
                                               "min_in_sample_trades": min_is_trades},
            "risk_policy": V4_POLICY.evidence(),
            "pacing": {"version": regime_gate.VERSION, "max_entries": regime_gate.MAX_ENTRIES,
                       "window_minutes": int(regime_gate.RATE_WINDOW.total_seconds() // 60),
                       "median_coin_drop": str(regime_gate.DROP_THRESHOLD),
                       "min_coins": regime_gate.MIN_COINS},
        },
        "data": {"hourly_bars": {s: len(b) for s, b in sorted(hour_bars.items())},
                 "coin_errors": coin_errors},
        "strategies": {}, "skipped": {},
    }
    new_trials = []
    for strategy_id in strategy_ids:
        strategy = strategies.get(strategy_id)
        if strategy.signals is None or strategy.simulate is None:
            report["skipped"][strategy_id] = {
                "reason": "NO_MECHANICAL_PROXY_IN_CORE",
                "detail": "The strategy's entries come from research agents' picks and Jev's "
                          "selection; core has no mechanical signal for it, so there is "
                          "nothing to replay on bars without inventing a rule."}
            continue
        log(f"{strategy_id}: signals and simulations")
        simulator = Simulator(cache, strategy, fee_rate=fee_rate, log=log)
        per_variant = strategy_trades(strategy, traded_bars, simulator, start=start, end=end,
                                      slippage_on=slippage_on, log=log)
        default_id = next((v for v in per_variant
                           if is_registered(strategy, per_variant[v]["variant"])),
                          next(iter(per_variant)))
        variants = {}
        sharpes = {}
        for vid, item in per_variant.items():
            summary = summarize(item["trades"], days=days, resamples=resamples)
            sharpes[vid] = summary["daily_sharpe"]
            variants[vid] = {"variant": item["variant"], "registered_rule": vid == default_id,
                             "signals": item["signals"], "simulated_outcomes": item["outcomes"],
                             **summary}
            new_trials.append({
                "run_id": run_id, "recorded_at": now, "strategy_id": strategy_id,
                "variant_id": vid, "variant": item["variant"],
                "trial_key": trial_key(strategy_id, item["variant"]),
                "source": SOURCE_LABELS[source], "start": start, "end": end,
                "fee_model": fee_model, "slippage": slippage_on, "universe_size": len(universe),
                "trades": summary["trades"], "mean_net_r": summary["mean_net_r"],
                "daily_sharpe": summary["daily_sharpe"]})
        ledger_keys = {row["trial_key"] for row in prior_trials
                       if row.get("strategy_id") == strategy_id}
        ledger_keys |= {trial_key(strategy_id, item["variant"])
                        for item in per_variant.values()}
        trials_n = len(ledger_keys)
        folds, oos = walk_forward({v: per_variant[v]["trades"] for v in per_variant},
                                  start=start, end=end, is_days=is_days, oos_days=oos_days,
                                  default_id=default_id, min_is_trades=min_is_trades)
        oos_days_list = [d for f in folds for d in all_days(
            datetime.combine(f["out_of_sample"][0], datetime.min.time(), UTC),
            datetime.combine(f["out_of_sample"][1], datetime.min.time(), UTC))]
        oos_start = (datetime.combine(folds[0]["out_of_sample"][0], datetime.min.time(), UTC)
                     if folds else end)
        registered_oos = [t for t in per_variant[default_id]["trades"]
                          if t["fill_at"] >= oos_start
                          and t["fill_at"].date() in set(oos_days_list)]
        trial_sharpes = list(sharpes.values())
        best_id = max(per_variant, key=lambda v: (sharpes[v] if sharpes[v] is not None
                                                  else float("-inf")))
        dsr = {
            "method": "Bailey & Lopez de Prado (2014); daily net R series, zero days included",
            "trials_this_run": len(per_variant), "trials_in_ledger": trials_n,
            "registered_rule": _dsr(daily_series(per_variant[default_id]["trades"], days),
                                    trials_n, trial_sharpes),
            "best_variant": {"variant_id": best_id, **_dsr(
                daily_series(per_variant[best_id]["trades"], days), trials_n, trial_sharpes)},
            "walk_forward_oos": _dsr(daily_series(oos, oos_days_list), trials_n,
                                     trial_sharpes),
        }
        matrix = [list(row) for row in zip(*(daily_series(per_variant[v]["trades"], days)
                                             for v in per_variant), strict=True)]
        pbo = (history_stats.pbo_cscv(matrix, blocks=PBO_BLOCKS)
               if len(per_variant) >= PBO_MIN_VARIANTS
               else {"pbo": None, "reason": f"FEWER_THAN_{PBO_MIN_VARIANTS}_VARIANTS"})
        if pbo.get("in_sample_best_counts"):
            ids = list(per_variant)
            pbo["in_sample_best_counts"] = {ids[k]: v for k, v in
                                            sorted(pbo["in_sample_best_counts"].items())}
        if pbo.get("pbo") is not None:
            pbo["pbo"] = _f(pbo["pbo"], 4)
            pbo["median_logit"] = _f(pbo["median_logit"], 4)
        registered = per_variant[default_id]["trades"]

        report["strategies"][strategy_id] = {
            "strategy": strategy.record(), "registered_variant": default_id,
            "variants": variants,
            "simulation_errors": dict(simulator.errors),
            "walk_forward": {"folds": folds,
                             "out_of_sample": summarize(oos, days=oos_days_list,
                                                        resamples=resamples),
                             "registered_rule_same_windows": summarize(
                                 registered_oos, days=oos_days_list, resamples=resamples)},
            "deflated_sharpe": dsr, "pbo": pbo,
            "breakdowns": {"registered_rule": breakdowns(registered, folds, tags),
                           "walk_forward_oos": breakdowns(oos, folds, tags)},
            "account_simulation": {
                "registered_rule_full_period": simulate_account(registered, medians,
                                                                equity=equity),
                "walk_forward_oos": simulate_account(oos, medians, equity=equity)},
        }
        report["strategies"][strategy_id]["promotion"] = promotion_check(
            report["strategies"][strategy_id])
        if source == SYNTHETIC:
            report["strategies"][strategy_id]["promotion"] = synthetic_check(
                report["strategies"][strategy_id]["promotion"])
    append_trials(trials_path, new_trials)
    report["trials"] = {"ledger": str(trials_path) if trials_path else None,
                        "recorded_this_run": len(new_trials),
                        "rows": new_trials}
    report["data"]["cache"] = dict(cache.stats)
    return report


def _dsr(series, trials, trial_sharpes):
    out = history_stats.deflated_sharpe(series, trials=trials, trial_sharpes=trial_sharpes)
    return {k: (_f(v) if isinstance(v, float) else v) for k, v in out.items()}


# --- The promotion ladder (record-only) -----------------------------------------------------------

LADDER_HISTORY_MIN_TRADES = 30
LADDER_DSR_MIN = 0.95
LADDER_PBO_MAX = 0.5
LADDER_SHADOW_MIN_TRADES = 30  # result_dimensions.CELL_MINIMUM, the shadow ladder's minimum.
RUNG_NONE, RUNG_HISTORY, RUNG_SHADOW = "NONE", "HISTORY_TEST_PASSED", "SHADOW_MINIMUM_MET"
RUNG_OWNER = "ELIGIBLE_FOR_OWNER_PAPER_REVIEW"


def promotion_check(history, shadow=None):
    """Which rung of the ladder a strategy meets, from its history report (one entry of
    ``report["strategies"]``) and its shadow scorecard cell (``strategy_shadow.shadow_cell``).
    Record-only text: it never changes a stage.

    History test passes when the walk-forward out-of-sample trades number at least 30, their
    mean net R and the lower end of its 90% interval are above zero, the walk-forward deflated
    Sharpe ratio is at least 0.95 and the PBO (when computed) is at most 0.5. Shadow passes with
    at least 30 simulated trades and a positive mean net R (after slippage when recorded).
    Paper allocation always needs a named ruling and the owner's yes."""
    checks = []
    wf = (history or {}).get("walk_forward", {}).get("out_of_sample", {})
    trades = wf.get("trades") or 0
    mean_r = wf.get("mean_net_r")
    ci = wf.get("ci90_mean_net_r")
    dsr = (history or {}).get("deflated_sharpe", {}).get("walk_forward_oos", {}).get("dsr")
    pbo = (history or {}).get("pbo", {}).get("pbo")
    checks.append(("history: >= 30 out-of-sample trades", trades >= LADDER_HISTORY_MIN_TRADES,
                   trades))
    checks.append(("history: mean net R > 0", mean_r is not None and D(str(mean_r)) > 0,
                   mean_r))
    checks.append(("history: 90% interval lower end > 0", bool(ci) and ci[0] > 0, ci))
    checks.append(("history: deflated Sharpe >= 0.95", dsr is not None and dsr >= LADDER_DSR_MIN,
                   dsr))
    checks.append(("history: PBO <= 0.5 (or not computed)", pbo is None or pbo <= LADDER_PBO_MAX,
                   pbo))
    history_ok = all(ok for _, ok, _ in checks)
    shadow_trades = (shadow or {}).get("trades") or 0
    shadow_mean = (shadow or {}).get("mean_net_r_after_slippage")
    if shadow_mean is None:
        shadow_mean = (shadow or {}).get("mean_net_r")
    shadow_checks = [
        ("shadow: >= 30 simulated trades", shadow_trades >= LADDER_SHADOW_MIN_TRADES,
         shadow_trades),
        ("shadow: mean net R > 0", shadow_mean is not None and D(str(shadow_mean)) > 0,
         shadow_mean),
    ]
    shadow_ok = all(ok for _, ok, _ in shadow_checks)
    rung = (RUNG_OWNER if history_ok and shadow_ok else RUNG_HISTORY if history_ok
            else RUNG_NONE)
    if not history_ok and shadow_ok:
        note = "Shadow figures meet the minimum, but the history test does not: rung 1 first."
    elif rung == RUNG_OWNER:
        note = ("History test and shadow minimum met: the strategy may be proposed to the owner "
                "for paper allocation as a named ruling. Nothing is promoted by this check.")
    elif rung == RUNG_HISTORY:
        note = ("History test met; it stays in shadow until 30 simulated trades with a "
                "positive mean net R.")
    else:
        note = "History test not met: the strategy stays where it is; nothing is promoted."
    return {"rung": rung, "record_only": True, "note": note,
            "checks": [{"check": c, "met": ok, "value": v}
                       for c, ok, v in checks + shadow_checks]}


def synthetic_check(check):
    """A synthetic run's promotion check: rung ``NONE`` whatever its figures say."""
    return {**check, "rung": RUNG_NONE, "synthetic": True, "reason": SYNTHETIC_NOT_EVIDENCE,
            "note": "Synthetic sample bars: a tutorial run, never evidence. Run the history "
                    "test on real public bars (--source alpaca or coinbase) before any rung."}


# --- Report ---------------------------------------------------------------------------------------


def _cell(value):
    return "—" if value is None else str(value)


def markdown(report):
    i = report["inputs"]
    lines = [
        f"# History test — {', '.join(i['strategies'])}",
        "",
        f"**{report['label']}**",
        "",
        f"- Version `{report['version']}`, run `{report['run_id']}`.",
        f"- Range {str(i['start'])[:10]} → {str(i['end'])[:10]} (signal times; UTC); "
        f"source `{i['source']}`; "
        f"{len(i['universe'])} coins.",
        f"- Fees: {i['fee_basis']} (both legs). Slippage: `{i['slippage_model'] or 'OFF'}`.",
        f"- Walk-forward: {i['walk_forward']['in_sample_days']} days in-sample / "
        f"{i['walk_forward']['out_of_sample_days']} days out-of-sample, rolling.",
        f"- Trials recorded this run: {report['trials']['recorded_this_run']} "
        f"(ledger `{report['trials']['ledger']}`).",
        "",
    ]
    if report["data"]["coin_errors"]:
        lines += [f"Coins without data: {', '.join(sorted(report['data']['coin_errors']))}", ""]
    for sid, why in report["skipped"].items():
        lines += [f"## {sid}: skipped", "", f"`{why['reason']}`. {why['detail']}", ""]
    for sid, s in report["strategies"].items():
        wf = s["walk_forward"]
        dsr, pbo = s["deflated_sharpe"], s["pbo"]
        lines += [
            f"## {sid}", "",
            f"Registered rule: `{s['registered_variant']}`. Promotion check: "
            f"**{s['promotion']['rung']}** — {s['promotion']['note']}", "",
            "### Every variant (full range)", "",
            "| Variant | Signals | Trades | Mean gross R | Fee R | Slip R | Mean net R | "
            "90% CI | Win | Max DD (R) | Ann. Sharpe |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|",
        ]
        for vid, v in s["variants"].items():
            mark = " (registered)" if v["registered_rule"] else ""
            lines.append(
                f"| `{vid}`{mark} | {v['signals']} | {v['trades']} | {_cell(v['mean_gross_r'])} | "
                f"{_cell(v['mean_fee_r'])} | {_cell(v['mean_slippage_r'])} | "
                f"{_cell(v['mean_net_r'])} | {_cell(v['ci90_mean_net_r'])} | "
                f"{_cell(v['win_rate'])} | {_cell(v['max_drawdown_r'])} | "
                f"{_cell(v['annualized_sharpe'])} |")
        o, ro = wf["out_of_sample"], wf["registered_rule_same_windows"]
        lines += [
            "", "### Walk-forward (out-of-sample only)", "",
            f"- Selected-per-window trades: {o['trades']}, mean net R {_cell(o['mean_net_r'])} "
            f"(90% CI {_cell(o['ci90_mean_net_r'])}), sum {_cell(o['sum_net_r'])} R, "
            f"win rate {_cell(o['win_rate'])}, max DD {_cell(o['max_drawdown_r'])} R.",
            f"- Registered rule in the same windows: {ro['trades']} trades, mean net R "
            f"{_cell(ro['mean_net_r'])} (90% CI {_cell(ro['ci90_mean_net_r'])}), sum "
            f"{_cell(ro['sum_net_r'])} R.", "",
            "| Fold | Out-of-sample | Selected | How | IS trades | IS mean R | OOS trades | "
            "OOS mean R | Registered OOS mean R |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|",
        ]
        for f in wf["folds"]:
            lines.append(
                f"| {f['fold']} | {f['out_of_sample'][0]} → {f['out_of_sample'][1]} | "
                f"`{f['selected']}` | {f['selection']} | {f['in_sample_trades']} | "
                f"{_cell(f['in_sample_mean_net_r'])} | {f['out_of_sample_trades']} | "
                f"{_cell(f['out_of_sample_mean_net_r'])} | "
                f"{_cell(f['registered_rule_oos_mean_net_r'])} |")
        lines += [
            "", "### Overfitting checks", "",
            f"- Trials: {dsr['trials_this_run']} this run, {dsr['trials_in_ledger']} distinct "
            "in the ledger (the deflation uses the ledger count).",
            f"- Deflated Sharpe, registered rule: {_cell(dsr['registered_rule']['dsr'])} "
            f"(daily SR {_cell(dsr['registered_rule']['sharpe'])}, expected max of "
            f"unskilled trials {_cell(dsr['registered_rule']['expected_max_sharpe'])}).",
            f"- Deflated Sharpe, best variant `{dsr['best_variant']['variant_id']}`: "
            f"{_cell(dsr['best_variant']['dsr'])}.",
            f"- Deflated Sharpe, walk-forward out-of-sample: "
            f"{_cell(dsr['walk_forward_oos']['dsr'])}.",
            f"- PBO (CSCV, {pbo.get('blocks', '—')} blocks, {pbo.get('combinations', 0)} "
            f"splits): {_cell(pbo.get('pbo'))}.", "",
            "### Account simulation (JEV_MANAGED_RISK_V4 sizing, 2% cluster cap, pacing)", "",
            "| Trade set | Taken | Return % | Max DD % | Mean planned risk % | Fees $ | "
            "Slippage $ | Skipped |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for name, a in s["account_simulation"].items():
            lines.append(
                f"| {name} | {a['trades_taken']} | {_cell(a['return_pct'])} | "
                f"{_cell(a['max_drawdown_pct'])} | {_cell(a['mean_planned_risk_pct'])} | "
                f"{_cell(a['fees_usd'])} | {_cell(a['slippage_usd'])} | "
                f"{json.dumps(a['trades_skipped'])} |")
        b = s["breakdowns"]["registered_rule"]
        for title, key in (("By prior-day BTC trend", "by_btc_trend"),
                           ("By prior-day BTC volatility", "by_btc_volatility"),
                           ("By prior-day alt breadth", "by_alt_breadth"),
                           ("By month", "by_month"), ("By coin", "by_coin")):
            lines += ["", f"### Registered rule — {title}", "",
                      "| Group | Trades | Mean net R | Sum net R | Win |",
                      "|---|---:|---:|---:|---:|"]
            for k, v in b[key].items():
                lines.append(f"| {k} | {v['trades']} | {_cell(v['mean_net_r'])} | "
                             f"{_cell(v['sum_net_r'])} | {_cell(v['win_rate'])} |")
        lines += ["", "### Promotion checks (record-only)", ""]
        for c in s["promotion"]["checks"]:
            lines.append(f"- {'met' if c['met'] else 'NOT met'}: {c['check']} "
                         f"(value {_cell(c['value'])})")
        lines.append("")
    return "\n".join(lines)


def write_report(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(report, default=json_value, indent=1, sort_keys=False))
    (out_dir / "REPORT.md").write_text(markdown(report))
    with open(out_dir / "trials.jsonl", "w") as handle:
        for row in report["trials"]["rows"]:
            handle.write(json.dumps(row, default=json_value, sort_keys=True) + "\n")
    return out_dir


# --- CLI ----------------------------------------------------------------------------------------


def _day(text):
    return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="catalyst-lab history-test",
        description="HISTORY_TEST_V1: offline walk-forward history test of mechanical strategy "
                    "plug-ins on keyless public bars (no orders, no broker, no ledger).")
    parser.add_argument("--strategy", action="append", dest="strategies",
                        help="strategy id (repeatable; default: every registered strategy)")
    parser.add_argument("--universe",
                        help="alpaca | coinbase | comma list of X/USD pairs (default: alpaca; "
                             "with --source synthetic, the sample coins)")
    parser.add_argument("--start", help="first UTC day (default: end - 365 days)")
    parser.add_argument("--end", help="UTC day the signals stop (default: 2 days ago)")
    parser.add_argument("--source", choices=SOURCES, default="alpaca")
    parser.add_argument("--fees", choices=sorted(FEE_MODELS), default="alpaca-taker")
    parser.add_argument("--no-slippage", action="store_true")
    parser.add_argument("--equity", default=str(DEFAULT_EQUITY))
    parser.add_argument("--is-days", type=int, default=DEFAULT_IS_DAYS)
    parser.add_argument("--oos-days", type=int, default=DEFAULT_OOS_DAYS)
    parser.add_argument("--min-is-trades", type=int, default=MIN_IS_TRADES)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(os.environ[CACHE_ENV]) if os.environ.get(CACHE_ENV)
                        else DEFAULT_CACHE_DIR)
    parser.add_argument("--out", type=Path, help="report folder (default: "
                        "~/.local/share/catalyst-handoff/history-tests/<today>/)")
    parser.add_argument("--trials-ledger", type=Path,
                        help="cumulative trials ledger (default: <out>/../TRIALS.jsonl)")
    parser.add_argument("--offline", action="store_true", help="cache only; fail on a miss")
    parser.add_argument("--min-interval", type=float, help="seconds between requests")
    parser.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    parser.add_argument("--shadow-cell", type=Path,
                        help="a shadow scorecard cell (JSON) for the promotion check")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--plugins-dir", type=Path,
                        help="a folder of drop-in strategy plug-ins (STRATEGY_SDK_V1; default "
                             "$CATALYST_STRATEGY_PLUGINS_DIR) to test beside the built-ins")
    args = parser.parse_args(argv)
    # Drop-in plug-ins (package plugin-c3): a named folder must load cleanly (strict); the
    # configured folder and installed entry points join as the runtime and the shadow load them.
    if args.plugins_dir is not None:
        try:
            strategies.load_plugins(folder=args.plugins_dir, strict=True)
        except ValueError as exc:
            parser.error(f"plug-in refused: {exc}")
    else:
        strategies.ensure_plugins_loaded()
    now = datetime.now(UTC)
    end = _day(args.end) if args.end else _day((now - timedelta(days=2)).date().isoformat())
    start = _day(args.start) if args.start else end - timedelta(days=DEFAULT_DAYS)
    if not start < end:
        parser.error("--start must be before --end")
    ids = args.strategies or sorted(strategies.REGISTRY)
    for sid in ids:
        if sid not in strategies.REGISTRY:
            parser.error(f"unknown strategy {sid}")
    out_root = Path(os.environ[OUT_ENV]) if os.environ.get(OUT_ENV) else DEFAULT_OUT_ROOT
    out = args.out or out_root / now.date().isoformat()
    # Synthetic tutorial runs never add trials to the real ledger the deflated Sharpe counts.
    ledger = args.trials_ledger or Path(out).parent / (
        SYNTHETIC_TRIALS if args.source == SYNTHETIC else "TRIALS.jsonl")

    def log(message):
        if not args.quiet:
            print(f"[history-test] {message}", file=sys.stderr, flush=True)

    cache = BarCache(args.source, cache_dir=args.cache_dir, offline=args.offline,
                     min_interval=args.min_interval, now=now)
    try:
        if args.universe:
            universe = universe_for(args.universe)
        elif args.source == SYNTHETIC:
            from catalyst_lab.sample_market import SYMBOLS

            universe = list(SYMBOLS)
        else:
            universe = universe_for("alpaca")
        report = run(strategy_ids=ids, universe=universe, start=start,
                     end=end, source=args.source, fee_model=args.fees,
                     slippage_on=not args.no_slippage, equity=D(args.equity),
                     is_days=args.is_days, oos_days=args.oos_days, cache=cache,
                     trials_path=ledger, min_is_trades=args.min_is_trades,
                     resamples=args.resamples, log=log, now=now)
    finally:
        cache.close()
    if args.shadow_cell:
        cell = json.loads(args.shadow_cell.read_text())
        for sid, s in report["strategies"].items():
            s["promotion"] = promotion_check(s, cell.get(sid, cell))
    write_report(report, out)
    print(json.dumps({"report": str(Path(out) / "REPORT.md"),
                      "results": str(Path(out) / "results.json"),
                      "trials_recorded": report["trials"]["recorded_this_run"],
                      "orders": "NONE_HISTORY_TEST_ONLY"}))
    return report


if __name__ == "__main__":
    main()
