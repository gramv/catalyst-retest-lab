"""Statistics of the history tester (``HISTORY_TEST_V1``, package strategy-c2). Pure, no I/O.

Trade arithmetic (prices, R, P&L) stays in ``Decimal`` in ``strategies.core`` and
``history_test``; the inferential statistics here (Sharpe ratios, the normal distribution, the
deflated Sharpe ratio, CSCV, bootstrap percentiles) take those exact values and compute in binary
floating point, which is what they are defined in. Every figure they return is a measurement for
a report, never a trading decision.

* ``walk_forward_folds``: fixed rolling windows (in-sample ``is_days``, out-of-sample
  ``oos_days``, stepped by ``oos_days``), so out-of-sample windows tile without overlap.
* ``deflated_sharpe``: Bailey and Lopez de Prado (2014), "The Deflated Sharpe Ratio":
  ``DSR = PSR(SR0)`` with ``SR0 = sqrt(V[SR_n]) ((1 - g) Z^-1(1 - 1/N) + g Z^-1(1 - 1/(N e)))``
  (``g`` the Euler-Mascheroni constant, ``N`` the number of trials, ``V[SR_n]`` the variance of
  the trials' Sharpe ratios) and ``PSR(SR*) = Z((SR - SR*) sqrt(T - 1) /
  sqrt(1 - skew SR + (kurtosis - 1) / 4 SR^2))`` on the non-annualized Sharpe ratio of ``T``
  observations (kurtosis not in excess).
* ``pbo_cscv``: Bailey, Borwein, Lopez de Prado and Zhu (2015), "The Probability of Backtest
  Overfitting": the rows (days) of the trials' performance matrix are cut into ``S`` blocks;
  for every half of the blocks as in-sample, the in-sample best trial's out-of-sample relative
  rank ``w`` gives ``logit = ln(w / (1 - w))``; PBO is the share of logits at or below zero.
  Performance in a block set is the mean of the daily values (documented choice; the paper
  allows any metric).
* ``day_bootstrap_ci``: resample whole days with replacement (trades on one day move together)
  and take the percentiles of the mean R per trade; seeded, so a re-run gives the same interval.
"""

import math
import random
import statistics
from datetime import timedelta
from itertools import combinations

EULER_GAMMA = 0.5772156649015329
NORMAL = statistics.NormalDist()


def walk_forward_folds(start, end, *, is_days, oos_days):
    """``[(is_start, is_end, oos_start, oos_end)]``: rolling windows whose out-of-sample parts
    tile ``[start + is_days, end)`` without overlap; a last partial window is dropped."""
    if is_days <= 0 or oos_days <= 0:
        raise ValueError("WALK_FORWARD_WINDOWS_INVALID")
    folds, at = [], start
    while at + timedelta(days=is_days + oos_days) <= end:
        is_end = at + timedelta(days=is_days)
        folds.append((at, is_end, is_end, is_end + timedelta(days=oos_days)))
        at += timedelta(days=oos_days)
    return folds


def mean(values):
    return sum(values) / len(values) if values else None


def sharpe(values):
    """Mean over the sample standard deviation; None with fewer than 2 values or no spread."""
    values = [float(v) for v in values]
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return None if sd == 0 else statistics.fmean(values) / sd


def moments(values):
    """``(skewness, kurtosis)`` (population, kurtosis not in excess); a normal sample is
    about ``(0, 3)``."""
    values = [float(v) for v in values]
    n = len(values)
    mu = statistics.fmean(values)
    m2 = sum((v - mu) ** 2 for v in values) / n
    if m2 == 0:
        return 0.0, 3.0
    m3 = sum((v - mu) ** 3 for v in values) / n
    m4 = sum((v - mu) ** 4 for v in values) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def probabilistic_sharpe(sr, benchmark, observations, skew, kurtosis):
    denominator = 1 - skew * sr + (kurtosis - 1) / 4 * sr ** 2
    if observations < 2 or denominator <= 0:
        return None
    return NORMAL.cdf((sr - benchmark) * math.sqrt(observations - 1) / math.sqrt(denominator))


def expected_max_sharpe(trials, variance):
    """``SR0``: the expected maximum Sharpe ratio of ``trials`` unskilled trials whose Sharpe
    ratios have ``variance`` (0 for a single trial)."""
    if trials < 2 or variance <= 0:
        return 0.0
    return math.sqrt(variance) * (
        (1 - EULER_GAMMA) * NORMAL.inv_cdf(1 - 1 / trials)
        + EULER_GAMMA * NORMAL.inv_cdf(1 - 1 / (trials * math.e)))


def deflated_sharpe(returns, *, trials, trial_sharpes):
    """The deflated Sharpe ratio of ``returns`` (one observation per period) after ``trials``
    trials whose per-period Sharpe ratios are ``trial_sharpes`` (their variance estimates
    ``V[SR_n]``). Returns a dict; ``dsr`` is None when it cannot be computed."""
    sr = sharpe(returns)
    finite = [s for s in trial_sharpes if s is not None]
    variance = statistics.variance(finite) if len(finite) >= 2 else 0.0
    benchmark = expected_max_sharpe(trials, variance)
    out = {"sharpe": sr, "observations": len(returns), "trials": trials,
           "trial_sharpe_variance": variance, "expected_max_sharpe": benchmark,
           "skew": None, "kurtosis": None, "dsr": None, "psr_vs_zero": None}
    if sr is None:
        return out
    skew, kurtosis = moments(returns)
    out.update(skew=skew, kurtosis=kurtosis,
               dsr=probabilistic_sharpe(sr, benchmark, len(returns), skew, kurtosis),
               psr_vs_zero=probabilistic_sharpe(sr, 0.0, len(returns), skew, kurtosis))
    return out


def _ranks(values):
    """Average ranks, 1 = lowest."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def pbo_cscv(matrix, *, blocks=16):
    """PBO by combinatorially symmetric cross-validation. ``matrix``: rows are periods (days),
    columns trials; values are each trial's result in the period. Returns a dict with ``pbo``
    (None when fewer than 2 trials or fewer rows than blocks)."""
    rows = len(matrix)
    trials = len(matrix[0]) if rows else 0
    if trials < 2 or blocks < 2 or blocks % 2 or rows < blocks:
        return {"pbo": None, "trials": trials, "blocks": blocks, "rows": rows,
                "combinations": 0}
    size = rows // blocks
    used = matrix[: size * blocks]
    parts = [used[b * size:(b + 1) * size] for b in range(blocks)]
    sums = [[sum(float(row[n]) for row in part) for n in range(trials)] for part in parts]
    logits, selected = [], {}
    for chosen in combinations(range(blocks), blocks // 2):
        rest = [b for b in range(blocks) if b not in chosen]
        in_sample = [sum(sums[b][n] for b in chosen) / (size * len(chosen))
                     for n in range(trials)]
        out_sample = [sum(sums[b][n] for b in rest) / (size * len(rest)) for n in range(trials)]
        best = max(range(trials), key=lambda n: (in_sample[n], -n))
        selected[best] = selected.get(best, 0) + 1
        w = _ranks(out_sample)[best] / (trials + 1)
        logits.append(math.log(w / (1 - w)))
    pbo = sum(1 for v in logits if v <= 0) / len(logits)
    return {"pbo": pbo, "trials": trials, "blocks": blocks, "rows": rows,
            "rows_used": size * blocks, "combinations": len(logits),
            "median_logit": statistics.median(logits), "in_sample_best_counts": selected}


def day_bootstrap_ci(trades, *, resamples=5000, level=0.90, seed=20261003):
    """``(low, high)`` of the mean R per trade over day-resampled trades. ``trades``:
    ``[(day, r)]``. None with fewer than 2 days."""
    by_day = {}
    for day, r in trades:
        by_day.setdefault(day, []).append(float(r))
    days = [by_day[d] for d in sorted(by_day)]
    if len(days) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        total = count = 0
        for _ in range(len(days)):
            values = days[rng.randrange(len(days))]
            total += sum(values)
            count += len(values)
        means.append(total / count)
    means.sort()
    tail = (1 - level) / 2
    low = means[max(0, math.floor(tail * resamples))]
    high = means[min(resamples - 1, math.ceil((1 - tail) * resamples) - 1)]
    return low, high


def max_drawdown(values):
    """The largest peak-to-trough fall of the running sum of ``values`` (a non-negative
    number in the values' unit) starting from zero."""
    peak = level = worst = 0.0
    for v in values:
        level += float(v)
        peak = max(peak, level)
        worst = max(worst, peak - level)
    return worst


__all__ = ["day_bootstrap_ci", "deflated_sharpe", "expected_max_sharpe", "max_drawdown",
           "moments", "pbo_cscv", "probabilistic_sharpe", "sharpe", "walk_forward_folds"]
