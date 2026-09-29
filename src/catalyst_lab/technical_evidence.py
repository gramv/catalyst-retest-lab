"""Reproducible external research observations, separate from executable quotes.

Code computes descriptive facts. Muse owns source choice and hypotheses; these
observations never establish broker eligibility or authorize an order.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.repository import json_safe

D = Decimal


class ObservedBar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bar_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    started_at: AwareDatetime
    open: D = Field(gt=0, allow_inf_nan=False)
    high: D = Field(gt=0, allow_inf_nan=False)
    low: D = Field(gt=0, allow_inf_nan=False)
    close: D = Field(gt=0, allow_inf_nan=False)
    volume: D = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def price_order(self):
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("INVALID_OBSERVED_OHLC")
        return self


class ObservedQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: AwareDatetime
    bid: D = Field(gt=0, allow_inf_nan=False)
    ask: D = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def uncrossed(self):
        if self.bid > self.ask:
            raise ValueError("CROSSED_RESEARCH_QUOTE")
        return self


class LevelEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bar_id: str
    field: Literal["open", "high", "low", "close"]
    rationale: str = Field(min_length=1, max_length=300)


class TechnicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["MUSE_OBSERVED_TECHNICALS_V1"]
    provider: str = Field(min_length=1, max_length=80)
    venue: str = Field(min_length=1, max_length=80)
    feed: str = Field(min_length=1, max_length=80)
    source_url: str = Field(pattern=r"^https://[^\s]+$", max_length=1000)
    retrieved_at: AwareDatetime
    timeframe_seconds: int = Field(ge=60, le=86400, strict=True)
    bars: list[ObservedBar] = Field(min_length=20, max_length=64)
    quote: ObservedQuote | None = None
    level_references: dict[str, LevelEvidence]

    @model_validator(mode="after")
    def complete_observations(self):
        if len({bar.bar_id for bar in self.bars}) != len(self.bars):
            raise ValueError("DUPLICATE_OBSERVATION_ID")
        if any(a.started_at >= b.started_at
               for a, b in zip(self.bars, self.bars[1:], strict=False)):
            raise ValueError("OBSERVATION_ORDER_INVALID")
        if any(bar.started_at + timedelta(seconds=self.timeframe_seconds) > self.retrieved_at
               for bar in self.bars):
            raise ValueError("INCOMPLETE_OBSERVED_BAR")
        if self.quote and self.quote.observed_at > self.retrieved_at:
            raise ValueError("FUTURE_RESEARCH_QUOTE")
        required = {"entry_trigger", "stop", "target"}
        if set(self.level_references) != required:
            raise ValueError("OBSERVED_LEVEL_REFERENCES_REQUIRED")
        if any(ref.bar_id not in {bar.bar_id for bar in self.bars}
               for ref in self.level_references.values()):
            raise ValueError("LEVEL_OBSERVATION_MISSING")
        return self

    def computed(self, *, now, levels):
        if self.retrieved_at > now:
            raise ValueError("FUTURE_TECHNICAL_EVIDENCE")
        by_id = {bar.bar_id: bar for bar in self.bars}
        for name, ref in self.level_references.items():
            if getattr(by_id[ref.bar_id], ref.field) != getattr(levels, name):
                raise ValueError("LEVEL_OBSERVATION_MISMATCH")
        closes = [bar.close for bar in self.bars]
        prior_volume = sum((bar.volume for bar in self.bars[-20:-1]), D(0)) / 19
        quote = self.quote
        data = self.model_dump(mode="json")
        return json_safe({
            "schema_version": self.schema_version,
            "origin": "EXTERNAL_RESEARCH_OBSERVATIONS",
            "execution_authority": False,
            "observations": data,
            "observations_hash": digest(encoded(data)),
            "metrics": {
                "last_completed_close": closes[-1],
                "sma_5": sum(closes[-5:]) / 5,
                "sma_20": sum(closes[-20:]) / 20,
                "last_volume_vs_prior_19_mean": self.bars[-1].volume / prior_volume
                if prior_volume else None,
                "observed_dollar_volume_20": sum(
                    (bar.close * bar.volume for bar in self.bars[-20:]), D(0)
                ),
                "spread_bps": (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000
                if quote else None,
                "last_bar_age_seconds": D(str((now - self.bars[-1].started_at
                    - timedelta(seconds=self.timeframe_seconds)).total_seconds())),
                "quote_age_seconds": D(str((now - quote.observed_at).total_seconds()))
                if quote else None,
                "gap_count": sum(
                    b.started_at - a.started_at != timedelta(seconds=self.timeframe_seconds)
                    for a, b in zip(self.bars, self.bars[1:], strict=False)
                ),
            },
            "limitations": [
                "Submitted research observations are not independently authenticated broker data.",
                "Dollar volume is close times base-unit volume; gaps remain explicit.",
                "Fresh broker quotes, liquidity checks and the original trigger remain mandatory.",
            ],
        })

    def dossier_facts(self, *, now, levels, bar_ids=()):
        """``computed`` carrying only the bars the levels or the rationale cite.

        REVIEW_DOSSIER_V1 input: metrics and ``observations_hash`` still cover every
        retained bar; uncited bars stay in the stored report and are listed by the
        dossier manifest instead of being sent to the reviewer.
        """
        facts = self.computed(now=now, levels=levels)
        cited = {ref.bar_id for ref in self.level_references.values()} | set(bar_ids)
        if not cited <= {bar.bar_id for bar in self.bars}:
            raise ValueError("CITED_OBSERVATION_MISSING")
        observations = dict(facts["observations"])
        bars = observations.pop("bars")
        observations.update(
            bars=[bar for bar in bars if bar["bar_id"] in cited],
            bar_count=len(bars),
            bar_selection="CITED_BY_LEVELS_OR_RATIONALE",
            first_bar_started_at=bars[0]["started_at"],
            last_bar_started_at=bars[-1]["started_at"],
        )
        return {**facts, "observations": observations}
