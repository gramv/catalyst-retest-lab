"""HISTORY_TEST_V1: the history tester on the shared strategy core (package strategy-c2,
2026-10-03).

Fixture evidence only: hand-built bars, canned public bars, a temporary bar cache, per-test
disposable PostgreSQL databases (the shadow parity test) and mock HTTP transports. No broker,
provider, network or owner-ledger contact; nothing here can place an order.
"""

import json
import math
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from catalyst_lab import account_risk, cli, history_stats, strategies
from catalyst_lab import history_test as ht
from catalyst_lab import strategy_shadow as ss
from catalyst_lab.history_bars import (
    BarCache,
    CoinbaseCandleReader,
    HistoryBarError,
    write_cache_rows,
)
from catalyst_lab.pick_outcomes import Bar
from catalyst_lab.strategies import core
from catalyst_lab.strategies.base import MECHANICAL, SHADOW, Strategy
from catalyst_lab.strategies.breakout_7d_vol2x_v1 import BREAKOUT_7D_VOL2X_V1, HISTORY_VARIANTS
from tests.learning_fixtures import FakeBars, learning  # noqa: F401 -- fixture
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_strategy_shadow import NOW, UNTIL, events, reader, universe

H = timedelta(hours=1)
M = timedelta(minutes=1)
T0 = datetime(2026, 6, 1, tzinfo=UTC)


def bar(start, o, h, low, c, v="10"):
    return Bar(start=start, open=D(str(o)), high=D(str(h)), low=D(str(low)), close=D(str(c)),
               volume=D(str(v)))


def row(start, o, h, low, c, v="10"):
    return {"t": start.isoformat(), "o": str(o), "h": str(h), "l": str(low), "c": str(c),
            "v": str(v)}


# --- Core parity: the tester runs the plug-in's own functions -------------------------------------


def breakout_hours(start, count, signal_index, *, close="101", volume="400"):
    bars, price = [], D(100)
    for n in range(count):
        at = start + n * H
        if n == signal_index:
            bars.append(bar(at, price, close, price, close, volume))
            price = D(close)
        else:
            bars.append(bar(at, price, price, price, price))
    return bars


def test_the_registered_variant_is_the_registered_rule():
    assert core.BreakoutRule.for_variant(7, "2") == core.BREAKOUT_V1_RULE
    assert core.BREAKOUT_V1_RULE.history_hours == core.BREAKOUT_HISTORY_HOURS
    assert [v["variant_id"] for v in HISTORY_VARIANTS] == [
        f"LOOKBACK_{d}D_VOL_{m}X" for d in (5, 7, 10) for m in ("1.5", "2", "3")]
    assert core.BreakoutRule.for_variant(5, "1.5").min_prior_bars == 86  # ceil(120 x 120 / 168)
    assert core.BreakoutRule.for_variant(10, "3").min_prior_bars == 172
    bars = breakout_hours(T0, 400, 300)
    context = {"symbol": "AAA/USD", "since": T0, "until": T0 + 400 * H}
    plain = BREAKOUT_7D_VOL2X_V1.signals(bars, context)
    variant = BREAKOUT_7D_VOL2X_V1.signals(bars, {**context, "parameters": {
        "prior_days": 7, "volume_multiple": "2"}})
    assert len(plain) == 1 and [{k: v for k, v in p.items() if k != "parameters"}
                                for p in variant] == plain
    assert plain[0]["facts"]["volume_ratio"] == "2.6250"  # The V1 formula, unchanged.
    assert ht.is_registered(BREAKOUT_7D_VOL2X_V1, {"variant_id": "LOOKBACK_7D_VOL_2X"})
    assert not ht.is_registered(BREAKOUT_7D_VOL2X_V1, {"variant_id": "LOOKBACK_5D_VOL_2X"})


def test_variants_change_only_the_declared_numbers():
    bars = breakout_hours(T0, 400, 300, volume="300")  # (23 x 10 + 300) x 7 / 1680 = 2.208x.
    context = {"symbol": "AAA/USD", "since": T0, "until": T0 + 400 * H}

    def found(days, multiple):
        return len(BREAKOUT_7D_VOL2X_V1.signals(bars, {**context, "parameters": {
            "prior_days": days, "volume_multiple": multiple}}))
    assert (found(7, "2"), found(7, "3"), found(7, "1.5")) == (1, 0, 1)
    assert found(5, "2") == 1 and found(10, "2") == 1


def test_a_shadow_outcome_and_a_history_replay_of_the_same_signal_agree(learning, tmp_path):  # noqa: F811
    store = learning.store
    universe(store, ["AAA/USD", "BBB/USD", "QQQ/USD"])
    canned = reader()
    ss.run_strategy_shadow(store, canned, now=NOW)
    shadow = {o["symbol"]: o["result"] for o in events(store, ss.OUTCOME_EVENT)}
    signal = next(s for s in events(store, ss.SIGNAL_EVENT) if s["symbol"] == "AAA/USD")
    assert signal["proposal"]["slippage"]["model"] == core.SLIPPAGE_MODEL
    assert D(shadow["AAA/USD"]["net_r_after_slippage"]) < D(shadow["AAA/USD"]["net_r"])

    cache = BarCache("alpaca", cache_dir=tmp_path, reader=canned, min_interval=0, now=NOW)
    report = ht.run(strategy_ids=["BREAKOUT_7D_VOL2X_V1"],
                    universe=["AAA/USD", "BBB/USD", "QQQ/USD"], start=UNTIL - ss.LOOKBACK,
                    end=UNTIL, source="alpaca", fee_model="alpaca-taker", cache=cache,
                    is_days=10, oos_days=10, resamples=50, now=NOW)
    variant = report["strategies"]["BREAKOUT_7D_VOL2X_V1"]["variants"]["LOOKBACK_7D_VOL_2X"]
    assert variant["registered_rule"] and variant["signals"] == 3
    assert variant["simulated_outcomes"] == {"NOT_FILLED_NO_PRINT": 2, "TARGET": 1}
    simulator = ht.Simulator(cache, BREAKOUT_7D_VOL2X_V1, fee_rate=ht.TAKER_FEE_TIER1,
                             log=print)
    replay = simulator("AAA/USD", signal["proposal"])
    for key in ("outcome", "gross_r", "net_r", "slippage_r", "net_r_after_slippage"):
        assert str(replay[key]) == str(shadow["AAA/USD"][key]), key
    assert D(variant["mean_net_r"]) == D(shadow["AAA/USD"]["net_r_after_slippage"]).quantize(
        D("0.0001"))
    assert shadow["BBB/USD"]["outcome"] == "NOT_FILLED_NO_PRINT"
    # The shadow cell reports the slippage-net mean beside the fee-net one.
    cell = ss.shadow_cells(store.repo, end=NOW)["BREAKOUT_7D_VOL2X_V1"]
    assert cell["slippage_trades"] == 1 and D(cell["mean_net_r_after_slippage"]) == D(
        shadow["AAA/USD"]["net_r_after_slippage"]).quantize(D("0.0001"))


def test_a_signal_recorded_before_the_slippage_model_simulates_as_before():
    bars = breakout_hours(T0, 400, 300)
    proposal = BREAKOUT_7D_VOL2X_V1.signals(bars, {"symbol": "AAA/USD", "since": T0,
                                                   "until": T0 + 400 * H})[0]
    at = datetime.fromisoformat(proposal["signal_at"])
    minutes = [bar(at + n * M, 101 + n, D(101 + n) + D("0.5"), 101 + n, D(101 + n) + D("0.5"))
               for n in range(6)]
    old = {k: v for k, v in proposal.items() if k != "slippage"}
    before = BREAKOUT_7D_VOL2X_V1.simulate(old, minutes, fee_rate=D("0.0025"))
    after = BREAKOUT_7D_VOL2X_V1.simulate(proposal, minutes, fee_rate=D("0.0025"))
    assert "net_r_after_slippage" not in before
    assert (before["gross_r"], before["net_r"]) == (after["gross_r"], after["net_r"])


# --- Fee and slippage math ------------------------------------------------------------------------


def test_cost_r_is_the_fee_form_on_both_legs():
    assert core.cost_r(D(100), D(98), D(103), D("0.0025")) == D("0.0025") * D(203) / D(2)
    gross, net = core.r_values(D(100), D(98), D(103), fee_rate=D("0.0025"))
    assert gross - net == core.cost_r(D(100), D(98), D(103), D("0.0025"))
    with pytest.raises(core.ShadowDataError):
        core.cost_r(D(100), D(100), D(101), D("0.001"))


def test_the_slippage_estimate_on_known_bars():
    at = T0 + 24 * H
    flat = [bar(T0 + n * H, 100, 100, 100, 100) for n in range(24)]
    estimate = core.slippage_estimate(flat, at, D(100))
    assert (D(estimate["spread_proxy"]), D(estimate["half_spread"]),
            D(estimate["volatility_term"]), D(estimate["per_leg"])) == (
        D(0), core.HALF_SPREAD_FLOOR, D(0), core.HALF_SPREAD_FLOOR)
    # Bars closing at the high then opening the next bar's range lower: a positive estimate.
    bounce = [bar(T0 + n * H, 100, 101 if n % 2 == 0 else 100, 99 if n % 2 == 0 else 98,
                  101 if n % 2 == 0 else 98) for n in range(24)]
    estimate = core.slippage_estimate(bounce, at, D(100))
    logs = [((math.log(float(b.high)) + math.log(float(b.low))) / 2, math.log(float(b.close)))
            for b in bounce]
    terms = [4 * (logs[t][1] - logs[t][0]) * (logs[t][1] - logs[t + 1][0]) for t in range(23)]
    expected_spread = math.sqrt(max(0.0, sum(terms) / len(terms)))
    assert abs(float(estimate["spread_proxy"]) - expected_spread) < 1e-9
    half = min(max(expected_spread / 2, 0.0001), 0.005)
    volatility = 0.5 * (sum(float(b.high - b.low) for b in bounce) / 24 / 100) / math.sqrt(60)
    assert abs(float(estimate["volatility_term"]) - volatility) < 1e-9
    assert abs(float(estimate["per_leg"]) - (half + volatility)) < 1e-9
    assert core.slippage_estimate(flat[:19], at, D(100)) is None  # Fewer than 20 bars.
    wild = [bar(T0 + n * H, 100, 200, 50, 200 if n % 2 == 0 else 50) for n in range(24)]
    assert D(core.slippage_estimate(wild, at, D(100))["half_spread"]) == core.HALF_SPREAD_CAP


def test_simulate_marketable_charges_slippage_on_both_legs():
    at = T0
    minutes = [bar(at + n * M, 100 + n, 100 + n, 100 + n, 100 + n) for n in range(6)]
    result = core.simulate_marketable(at, minutes, hourly_range=D("0.5"),
                                      stop_fraction=core.MIN_STOP_FRACTION, hold=H,
                                      fee_rate=D("0.0025"), slippage_fraction=D("0.001"))
    assert result["outcome"] == core.TARGET  # Stop 98, target 103.
    risk = D(2)
    assert result["net_r_after_slippage"] == result["net_r"] - D("0.001") * D(203) / risk
    assert result["slippage_r"] == D("0.001") * D(203) / risk


def test_fee_models_name_their_rates():
    assert ht.FEE_MODELS["alpaca-taker"][0] == D("0.0025")
    assert ht.FEE_MODELS["alpaca-maker"][0] == D("0.0015")
    assert ht.FEE_MODELS["coinbase-taker-under-10k"][0] == D("0.009")


# --- Walk-forward ---------------------------------------------------------------------------------


def test_walk_forward_windows_tile_out_of_sample_without_overlap():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    folds = history_stats.walk_forward_folds(start, start + timedelta(days=365),
                                             is_days=60, oos_days=30)
    assert len(folds) == 10
    assert folds[0] == (start, start + timedelta(days=60), start + timedelta(days=60),
                        start + timedelta(days=90))
    for a, b in zip(folds, folds[1:], strict=False):
        assert a[3] == b[2] and b[0] - a[0] == timedelta(days=30)
    assert folds[-1][3] <= start + timedelta(days=365)
    with pytest.raises(ValueError):
        history_stats.walk_forward_folds(start, start, is_days=0, oos_days=30)


def trade(fill_at, r, *, symbol="AAA/USD", exit_after=H, entry="100", stop="98", vid="A",
          signal_at=None, sector=None):
    fill_at = fill_at if fill_at.tzinfo else fill_at.replace(tzinfo=UTC)
    return {"variant_id": vid, "symbol": symbol, "sector": sector or ht.sector_of(symbol),
            "signal_at": signal_at or fill_at, "fill_at": fill_at,
            "exit_at": fill_at + exit_after, "outcome": "TARGET", "entry": D(entry),
            "stop": D(stop), "target": D("103"), "exit": D("103"), "gross_r": D(str(r)),
            "fee_r": D(0), "slippage_r": D(0), "r": D(str(r))}


def test_walk_forward_selects_on_closed_in_sample_trades_only():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    is_end = start + timedelta(days=10)
    variants = {
        "REG": [trade(start + timedelta(days=d), "0.1", vid="REG") for d in range(3)]
        + [trade(is_end + timedelta(days=1), "-1", vid="REG")],
        # B looks best in-sample only through a trade that exits after the window ends.
        "B": [trade(start + timedelta(days=d), "0.2", vid="B") for d in range(3)]
        + [trade(is_end - H, "9", vid="B", exit_after=2 * H),
           trade(is_end + timedelta(days=2), "0.5", vid="B")],
    }
    folds, oos = ht.walk_forward(variants, start=start, end=start + timedelta(days=15),
                                 is_days=10, oos_days=5, default_id="REG", min_is_trades=3)
    assert folds[0]["selected"] == "B" and folds[0]["in_sample_trades"] == 3
    assert D(folds[0]["in_sample_mean_net_r"]) == D("0.2")
    assert [t["r"] for t in oos] == [D("0.5")]
    assert folds[0]["registered_rule_oos_mean_net_r"] == D("-1")
    folds, oos = ht.walk_forward(variants, start=start, end=start + timedelta(days=15),
                                 is_days=10, oos_days=5, default_id="REG", min_is_trades=4)
    assert folds[0]["selection"] == "FALLBACK_REGISTERED_RULE" and folds[0]["selected"] == "REG"


# --- Deflated Sharpe and PBO ----------------------------------------------------------------------


def test_the_deflated_sharpe_reproduces_the_papers_example():
    # Bailey & Lopez de Prado (2014), section 5: annualized SR 2.5 over 1,250 days, 100 trials
    # with an annualized SR variance of 0.5, skew -3 and kurtosis 10 give DSR = 0.9004.
    sr, variance = 2.5 / math.sqrt(250), 0.5 / 250
    benchmark = history_stats.expected_max_sharpe(100, variance)
    assert abs(benchmark * math.sqrt(250) - 1.7894) < 1e-3
    dsr = history_stats.probabilistic_sharpe(sr, benchmark, 1250, -3, 10)
    assert abs(dsr - 0.9004) < 5e-4
    assert history_stats.expected_max_sharpe(1, variance) == 0.0


def test_deflated_sharpe_on_a_series():
    series = [D("0.5"), D("-0.2"), D("0.3"), D(0), D("0.4"), D("-0.1")] * 20
    one = history_stats.deflated_sharpe(series, trials=1, trial_sharpes=[0.2])
    many = history_stats.deflated_sharpe(series, trials=50, trial_sharpes=[0.0, 0.1, 0.3, 0.2])
    assert one["dsr"] == one["psr_vs_zero"] and one["expected_max_sharpe"] == 0.0
    assert many["dsr"] < one["dsr"]  # More trials, more deflation.
    flat = history_stats.deflated_sharpe([D(0)] * 10, trials=9, trial_sharpes=[0.1, 0.2])
    assert flat["dsr"] is None and flat["sharpe"] is None


def test_pbo_is_zero_for_a_dominant_trial_and_one_for_a_reversing_pair():
    dominant = [[1, 0, 0.5] for _ in range(32)]
    assert history_stats.pbo_cscv(dominant, blocks=16)["pbo"] == 0.0
    x = [1, 2, 3, 4, 5, 6, 7, 8, -1, -2, -3, -4, -5, -6, -7, -8]
    reversing = [[v, -v] for v in x]
    result = history_stats.pbo_cscv(reversing, blocks=16)
    assert result["pbo"] == 1.0 and result["combinations"] == 12870
    assert history_stats.pbo_cscv([[1] for _ in range(32)])["pbo"] is None
    assert history_stats.pbo_cscv([[1, 2]] * 8, blocks=16)["pbo"] is None


def test_bootstrap_and_drawdown():
    same = [(T0.date() + timedelta(days=d), D(1)) for d in range(5)]
    assert history_stats.day_bootstrap_ci(same, resamples=200) == (1.0, 1.0)
    mixed = [(T0.date() + timedelta(days=d), D(r)) for d, r in enumerate([-1, 2, -1, 2, 0.5])]
    low, high = history_stats.day_bootstrap_ci(mixed, resamples=500)
    assert -1 <= low < 0.5 < high <= 2
    assert history_stats.day_bootstrap_ci(mixed, resamples=500) == (low, high)  # Seeded.
    assert history_stats.day_bootstrap_ci(same[:1]) is None
    assert history_stats.max_drawdown([1, -2, 1, -1]) == 2
    assert history_stats.max_drawdown([1, 1]) == 0


# --- Account simulation: sizing, cluster cap, pacing ---------------------------------------------


def test_the_policy_mirror_matches_migration_026():
    sql = (Path(ht.__file__).parent / "migrations" / "026_risk_v4.sql").read_text()
    assert re.search(r"VALUES\('JEV_MANAGED_RISK_V4','MANAGED',0\.005,0\.05,"
                     r"'\{\"US_STOCKS\":0\.03,\"CRYPTO\":0\.02,\"FOREX\":0\}',\s*2,1,", sql)
    assert "VALUES('JEV_MANAGED_RISK_V4','CRYPTO','EQUITY_SLICE_RISK_CAPPED_V1',0.10,3,0.02," in sql
    assert "VALUES('JEV_MANAGED_RISK_V4',0.03,'CANCEL_AND_FLATTEN',0.02,'NO_NEW_ENTRIES'," in sql
    policy = ht.V4_POLICY
    assert (policy.risk_pct, policy.market_cap("CRYPTO"), policy.daily_soft_loss_pct(),
            policy.daily_hard_loss_pct()) == (D("0.005"), D("0.02"), D("0.02"), D("0.03"))
    terms = policy.terms("CRYPTO")
    assert (terms.notional_pct, terms.max_per_theme, terms.min_stop_fraction) == (
        D("0.10"), 3, D("0.02"))


def medians_for(trades, value="0"):
    return {t["signal_at"] - H: (D(value), 10) for t in trades}


def test_sizing_uses_the_slice_and_the_risk_cap():
    t0 = datetime(2026, 6, 1, 14, tzinfo=UTC)
    trades = [trade(t0, "1"), trade(t0 + timedelta(hours=3), "1", symbol="ETH/USD",
                                    entry="100", stop="80")]
    result = ht.simulate_account(trades, medians_for(trades), equity=D(10000))
    assert result["trades_taken"] == 2
    assert result["binding_constraint"] == {"NOTIONAL": 1, "RISK": 1}
    # Slice: 1,000 notional = 10 units x 2 risk = 20 (0.2%); risk cap: 50 / 20 = 2.5 units, +1R.
    assert result["end_equity"] == D("10070.1")  # 20 + 2.505 x 20 (0.5% of 10,020).
    assert result["mean_planned_risk_pct"] == D("0.3500")


def test_the_cluster_cap_the_sector_limit_and_one_trade_per_coin():
    t0 = datetime(2026, 6, 1, 14, tzinfo=UTC)
    symbols = ["BTC/USD", "ADA/USD", "BCH/USD", "DOGE/USD", "AAVE/USD"]  # Five sectors.
    wide = [trade(t0 + n * timedelta(minutes=31), "1", symbol=s, stop="90",
                  exit_after=timedelta(days=1)) for n, s in enumerate(symbols)]
    result = ht.simulate_account(wide, medians_for(wide), equity=D(10000))
    # Each risks 0.5% (risk cap binds at a 10% stop): four fill the 2% cluster cap.
    assert result["trades_taken"] == 4 and result["trades_skipped"] == {"MARKET_RISK_CAP": 1}
    same_sector = [trade(t0 + n * timedelta(minutes=31), "1", symbol=s,
                         exit_after=timedelta(days=1))
                   for n, s in enumerate(["DOGE/USD", "PEPE/USD", "SHIB/USD", "WIF/USD"])]
    result = ht.simulate_account(same_sector, medians_for(same_sector), equity=D(10000))
    assert result["trades_skipped"] == {"SECTOR_LIMIT": 1}
    twice = [trade(t0, "1", exit_after=timedelta(days=1)),
             trade(t0 + timedelta(hours=2), "1", exit_after=timedelta(days=1))]
    assert ht.simulate_account(twice, medians_for(twice))["trades_skipped"] == {
        "COIN_ALREADY_OPEN": 1}


def test_pacing_the_drop_gate_and_the_soft_limit():
    t0 = datetime(2026, 6, 1, 14, tzinfo=UTC)
    burst = [trade(t0 + n * timedelta(minutes=5), "1", symbol=s)
             for n, s in enumerate(["BTC/USD", "ADA/USD", "BCH/USD"])]
    result = ht.simulate_account(burst, medians_for(burst))
    assert result["trades_skipped"] == {"ENTRY_RATE_LIMIT": 1}
    falling = [trade(t0, "1")]
    assert ht.simulate_account(falling, medians_for(falling, "-0.02"))["trades_skipped"] == {
        "MARKET_DROP": 1}
    assert ht.simulate_account(falling, medians_for(falling, "-0.0199"))["trades_taken"] == 1
    assert ht.simulate_account(falling, {})["trades_skipped"] == {
        "MARKET_BREADTH_UNAVAILABLE": 1}
    # A -2% realized New York day: 20% stops, so the risk cap binds and -1R is -0.5% each.
    losses = [trade(t0 + n * timedelta(minutes=31), "-1", symbol=s, stop="80", exit_after=M)
              for n, s in enumerate(["BTC/USD", "ADA/USD", "BCH/USD", "DOGE/USD", "AAVE/USD",
                                     "LINK/USD"])]
    result = ht.simulate_account(losses, medians_for(losses), equity=D(10000))
    # -50, -49.75, -49.50, -49.26 (-1.99%), -49.01: the sixth waits for the next day.
    assert result["trades_taken"] == 5 and result["trades_skipped"] == {
        account_risk.SOFT_LIMIT_REASON: 1}
    assert D(result["max_drawdown_pct"]) > D("1.9")


# --- The plug-in declaration ----------------------------------------------------------------------


def test_history_variants_are_validated_on_the_record():
    base = dict(strategy_id="X_V1", name="X", version=1, description="d",
                entry_types=frozenset({core.BREAKOUT}), trigger_rule="t",
                plan_rules="CRYPTO_TRADE_PLAN_V1", jev_questions=(),
                sources=frozenset({MECHANICAL}), stage=SHADOW, signals=lambda b, c: [],
                simulate=lambda p, b, **k: {})
    assert Strategy(**base, history_variants=({"variant_id": "A"},)).record()[
        "history_variants"] == [{"variant_id": "A"}]
    for bad in (({"variant_id": "A"}, {"variant_id": "A"}), ({},), [{"variant_id": "A"}]):
        with pytest.raises(ValueError, match="STRATEGY_HISTORY_VARIANTS_INVALID"):
            Strategy(**base, history_variants=bad)
    assert strategies.PULLBACK_V1.history_variants == ()


def test_promotion_check_is_record_only_text():
    passing = {"walk_forward": {"out_of_sample": {"trades": 40, "mean_net_r": D("0.2"),
                                                  "ci90_mean_net_r": [0.05, 0.4]}},
               "deflated_sharpe": {"walk_forward_oos": {"dsr": 0.97}}, "pbo": {"pbo": 0.2}}
    assert ht.promotion_check(passing)["rung"] == ht.RUNG_HISTORY
    shadow = {"trades": 31, "mean_net_r": "0.1", "mean_net_r_after_slippage": "0.05"}
    full = ht.promotion_check(passing, shadow)
    assert full["rung"] == ht.RUNG_OWNER and full["record_only"]
    assert "Nothing is promoted" in full["note"]
    weak = {**passing, "pbo": {"pbo": 0.7}}
    result = ht.promotion_check(weak, shadow)
    assert result["rung"] == ht.RUNG_NONE and "rung 1 first" in result["note"]
    assert ht.promotion_check({}, None)["rung"] == ht.RUNG_NONE
    negative_shadow = {**shadow, "mean_net_r_after_slippage": "-0.01"}
    assert ht.promotion_check(passing, negative_shadow)["rung"] == ht.RUNG_HISTORY


# --- Readers and the cache ------------------------------------------------------------------------


def test_the_coinbase_reader_is_keyless_and_pages_ascending():
    seen = []

    def handler(request):
        seen.append(request)
        start = datetime.fromisoformat(request.url.params["start"])
        candles = [[int((start + n * H).timestamp()), 1, 3, 2, 2.5, 7] for n in range(300)]
        return httpx.Response(200, json=list(reversed(candles)))

    coinbase = CoinbaseCandleReader(transport=httpx.MockTransport(handler))
    rows = coinbase.bars("BTC/USD", T0, T0 + 400 * H, "1Hour")
    assert len(rows) == 400 and rows[0]["t"] == T0.isoformat()
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)
    assert rows[0] == {"t": T0.isoformat(), "o": "2", "h": "3", "l": "1", "c": "2.5", "v": "7"}
    assert len(seen) == 2 and all(r.url.path == "/products/BTC-USD/candles" for r in seen)
    assert all("authorization" not in {k.lower() for k in r.headers} for r in seen)
    blocked = CoinbaseCandleReader(transport=httpx.MockTransport(handler))
    blocked._client.headers["Authorization"] = "x"
    with pytest.raises(HistoryBarError, match="KEYLESS"):
        blocked.bars("BTC/USD", T0, T0 + H, "1Hour")
    with pytest.raises(HistoryBarError, match="NOT_ALLOWED"):
        coinbase._client.get("https://example.com/products/BTC-USD/candles")


def test_the_cache_keeps_only_ended_chunks_and_each_chunks_own_bars(tmp_path):
    day = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [row(day + n * M, 1, 1, 1, 1) for n in range(1441)]  # One bar past the day.
    fake = FakeBars(minutes={"AAA/USD": rows})
    fake.bars = lambda s, a, b, tf: rows  # A source whose end is inclusive.
    cache = BarCache("alpaca", cache_dir=tmp_path, reader=fake, min_interval=0,
                     now=day + timedelta(days=1, hours=1))
    assert len(cache.minutes("AAA/USD", day, day + timedelta(days=1))) == 1440
    assert cache.stats["cache_writes"] == 1
    again = BarCache("alpaca", cache_dir=tmp_path, offline=True)
    assert len(again.minutes("AAA/USD", day, day + timedelta(days=1))) == 1440
    with pytest.raises(HistoryBarError, match="HISTORY_CACHE_MISS"):
        again.minutes("AAA/USD", day + timedelta(days=1), day + timedelta(days=2))
    running = BarCache("alpaca", cache_dir=tmp_path / "b", reader=FakeBars(
        minutes={"AAA/USD": rows}), min_interval=0, now=day + timedelta(hours=5))
    running.minutes("AAA/USD", day, day + timedelta(days=1))
    assert running.stats["cache_writes"] == 0  # The day has not ended: read, never cached.


def test_a_rate_limit_is_retried_politely(tmp_path):
    calls, sleeps = [], []

    class Limited:
        def bars(self, symbol, start, end, timeframe):
            calls.append(1)
            if len(calls) < 3:
                from catalyst_lab.public_crypto_bars import PublicCryptoBarError
                raise PublicCryptoBarError("PUBLIC_BAR_HTTP_429")
            return []

    cache = BarCache("alpaca", cache_dir=tmp_path, reader=Limited(), min_interval=0,
                     sleep=sleeps.append, now=datetime(2026, 10, 1, tzinfo=UTC))
    cache.hourly("AAA/USD", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC))
    assert len(calls) == 3 and sleeps == [2, 4] and cache.stats["retries"] == 2


# --- CLI smoke test on cached fixture bars (no network) -------------------------------------------


def write_fixture_cache(root, start, end):
    """Hourly month files for four coins (BTC flat-ish; AAA/BBB with breakouts) and the minute
    days of AAA's signals; nothing else, so a miss is counted, never fetched."""
    pad_start = start - ht.HISTORY_PAD
    signals = {"AAA/USD": [start + timedelta(days=12, hours=5), start + timedelta(days=30)],
               "BBB/USD": [start + timedelta(days=20, hours=9)]}
    for symbol in ("BTC/USD", "AAA/USD", "BBB/USD", "CCC/USD"):
        price, rows, at = D(100), [], pad_start
        marks = {s - H for s in signals.get(symbol, [])}
        n = 0
        while at < end:
            wiggle = D(n % 5) / 10
            if at in marks:
                close = price * D("1.05")
                rows.append(row(at, price, close, price, close, "500"))
                price = close
            else:
                rows.append(row(at, price, price + wiggle, price - wiggle, price, "10"))
            at += H
            n += 1
        month = pad_start.replace(day=1)
        while month < end:
            nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            write_cache_rows(root, "alpaca", "1Hour", symbol, month.strftime("%Y-%m"),
                             [r for r in rows if month <= datetime.fromisoformat(r["t"]) < nxt],
                             month, nxt)
            month = nxt
    for at in signals["AAA/USD"]:
        price = D(105)
        for offset in range(2):
            day = (at + timedelta(days=offset)).replace(hour=0)
            minute_rows = [row(day + k * M, price, price + D("0.01") * (k % 7),
                               price - D("0.01") * (k % 5), price) for k in range(1440)]
            write_cache_rows(root, "alpaca", "1Min", "AAA/USD", day.strftime("%Y-%m-%d"),
                             minute_rows, day, day + timedelta(days=1))
    return signals


def test_the_cli_runs_offline_on_cached_fixture_bars(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("NETWORK_USED")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    start, end = datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 11, tzinfo=UTC)
    write_fixture_cache(tmp_path / "cache", start, end)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "catalyst-lab", "history-test", "--offline", "--cache-dir", str(tmp_path / "cache"),
        "--universe", "AAA/USD,BBB/USD,CCC/USD,BTC/USD", "--start", "2026-06-01",
        "--end", "2026-07-11", "--is-days", "10", "--oos-days", "5", "--resamples", "100",
        "--out", str(out), "--quiet"])
    cli.main()
    results = json.loads((out / "results.json").read_text())
    assert results["version"] == "HISTORY_TEST_V1"
    assert results["skipped"]["PULLBACK_V1"]["reason"] == "NO_MECHANICAL_PROXY_IN_CORE"
    breakout = results["strategies"]["BREAKOUT_7D_VOL2X_V1"]
    assert len(breakout["variants"]) == 9 and results["trials"]["recorded_this_run"] == 9
    assert breakout["deflated_sharpe"]["trials_in_ledger"] == 9
    registered = breakout["variants"]["LOOKBACK_7D_VOL_2X"]
    assert registered["registered_rule"] and registered["signals"] == 3
    # AAA's two signals had cached minute bars (flat: each held 24 h); BBB's did not.
    assert registered["simulated_outcomes"] == {"BARS_UNAVAILABLE": 1, "HOLD_24H_EXIT": 2}
    assert registered["trades"] == 2 and D(registered["mean_net_r"]) < 0  # Costs only.
    assert len(breakout["walk_forward"]["folds"]) == 6
    assert breakout["promotion"]["rung"] == "NONE" and breakout["promotion"]["record_only"]
    assert (out / "REPORT.md").read_text().startswith("# History test")
    ledger = (tmp_path / "TRIALS.jsonl").read_text().splitlines()
    assert len(ledger) == 9 and (out / "trials.jsonl").exists()
    # A second run of the same variants adds rows but no new distinct trials.
    cli.main()
    again = json.loads((out / "results.json").read_text())
    assert again["strategies"]["BREAKOUT_7D_VOL2X_V1"]["deflated_sharpe"][
        "trials_in_ledger"] == 9
    assert len((tmp_path / "TRIALS.jsonl").read_text().splitlines()) == 18
