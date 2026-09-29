"""Opt-in paper eligibility using independent venue observations and explicit cost assumptions."""

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal as D

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.repository import json_safe
from catalyst_lab.scan_sources import BarContextWindow


@dataclass(frozen=True)
class CryptoLiquidityPolicy:
    policy_id: str
    lookback_bars: int
    minimum_dollar_volume: D
    maximum_participation: D
    assumed_round_trip_cost_bps: D
    maximum_cost_to_risk: D

    def __post_init__(self):
        if (self.policy_id != "CRYPTO_LIQUIDITY_PAPER_V1"
            or type(self.lookback_bars) is not int or not 20 <= self.lookback_bars <= 120
            or any(not isinstance(v, D) or not v.is_finite() or v <= 0 for v in (
                self.minimum_dollar_volume, self.maximum_participation,
                self.assumed_round_trip_cost_bps, self.maximum_cost_to_risk))
            or self.maximum_participation > D("0.01") or self.maximum_cost_to_risk > 1):
            raise ValueError("EXPLICIT_CRYPTO_LIQUIDITY_POLICY_REQUIRED")


class CryptoLiquidityReader:
    def __init__(self, source, policy, *, clock):
        self.source, self.policy, self.clock = source, policy, clock

    def __call__(self, symbol, levels):
        now = self.clock()
        try:
            bars, issues = self.source.completed_bars(
                "CRYPTO", symbol, BarContextWindow(self.policy.lookback_bars, 60)
            )
            now = self.clock()
            if issues:
                raise ValueError("CRYPTO_LIQUIDITY_UNAVAILABLE")
            bars = list(bars[-self.policy.lookback_bars:])
            if len(bars) != self.policy.lookback_bars or any(
                not bar.completed or bar.provider != "ALPACA" or bar.feed != "CRYPTO_US"
                or not bar.source_id or not bar.start_at.tzinfo or not bar.end_at.tzinfo
                or bar.end_at - bar.start_at != timedelta(minutes=1)
                or bar.end_at > now or not 0 < bar.low <= min(bar.open, bar.close)
                <= max(bar.open, bar.close) <= bar.high or bar.volume < 0
                for bar in bars
            ) or len({bar.source_id for bar in bars}) != len(bars) or any(
                a.end_at != b.start_at for a, b in zip(bars, bars[1:], strict=False)
            ) or not 0 <= (now - bars[-1].end_at).total_seconds() <= 90:
                raise ValueError("CRYPTO_LIQUIDITY_INCOMPLETE")
            volume = sum((b.volume * b.close for b in bars), D(0))
            m, stop = D(str(levels["max_entry_price"])), D(str(levels["stop"]))
            cost = m * self.policy.assumed_round_trip_cost_bps / 10000
            if volume < self.policy.minimum_dollar_volume:
                reason = "CRYPTO_VENUE_LIQUIDITY_INSUFFICIENT"
            elif m <= stop or cost > (m - stop) * self.policy.maximum_cost_to_risk:
                reason = "CRYPTO_ASSUMED_COST_EXCEEDS_POLICY"
            else:
                reason = None
            observations = json_safe([asdict(bar) for bar in bars])
            return json_safe({
                "passed": reason is None, "reason": reason, "observed_at": now,
                "policy": asdict(self.policy), "symbol": symbol,
                "observations": observations, "observations_hash": digest(encoded(observations)),
                "observed_dollar_volume": volume,
                "maximum_entry_notional": volume * self.policy.maximum_participation,
                "assumed_round_trip_cost_per_unit": cost,
                "cost_scope": "PAPER_ELIGIBILITY_ASSUMPTION_NOT_VERIFIED_BROKER_FEE",
            })
        except Exception:
            return {"passed": False, "reason": "CRYPTO_LIQUIDITY_UNAVAILABLE",
                    "observed_at": now.isoformat(), "policy": json_safe(asdict(self.policy))}
